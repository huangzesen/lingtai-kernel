"""Regression tests for AED recovery paths: WorkerStillRunningError fail-closed
handling in the run loop, plus transient provider-error retry budget.

The previous `.llm_hang` watchdog/sentinel system was removed; this file replaces
`test_worker_still_running_recovery.py`. The remaining safety property is that
when `WorkerStillRunningError` raises out of `_handle_message`, the run loop
puts the agent ASLEEP without saving chat history (the worker may still be
mutating ChatInterface) — no filesystem sentinel involved.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from lingtai.kernel.base_agent import turn
from lingtai.kernel.llm import LLMResponse
from lingtai.kernel.llm.base import (
    LLMReplayTerminalError,
    UsageMetadata,
    llm_replay_terminal_flags,
    mark_llm_replay_terminal,
)
from lingtai.kernel.llm_utils import WorkerStillRunningError, send_with_timeout_stream
from lingtai.kernel.message import _make_message, MSG_REQUEST
from lingtai.kernel.state import AgentState
from lingtai.auth.codex_account_source import AccountCandidate, NoCandidateError
from lingtai.llm.openai.adapter import CodexOpenAIAdapter


@dataclass
class _FakeAgent:
    _working_dir: object
    _state: AgentState = AgentState.ACTIVE
    _asleep: threading.Event = field(default_factory=threading.Event)
    _logs: list[tuple[str, dict]] = field(default_factory=list)
    _states: list[AgentState] = field(default_factory=list)
    _notifications: list[dict] = field(default_factory=list)
    refresh_calls: list[dict] = field(default_factory=list)
    # ``_chat`` is read by ``_run_loop`` when ``_asleep`` is set (to heal
    # dangling tool_calls before sleeping). Default to None — fake agents
    # in this suite never have a live chat session.
    _chat: object = None

    def _log(self, event_type: str, **fields):
        self._logs.append((event_type, fields))

    def _cancel_soul_timer(self):
        # Mirror BaseAgent._cancel_soul_timer's delegation to the soul flow hook.
        # These tests monkeypatch ``lingtai.tools.soul.flow._cancel_soul_timer`` (e.g. to
        # use it as a shutdown signal), so route through that module attribute.
        import lingtai.tools.soul.flow as soul_flow
        soul_flow._cancel_soul_timer(self)

    def _set_state(self, new_state: AgentState, reason: str = ""):
        self._state = new_state
        self._states.append(new_state)
        self._log("agent_state", new=new_state.value, reason=reason)

    def _enqueue_system_notification(
        self,
        *,
        source: str,
        ref_id: str,
        body: str,
        priority: str = "normal",
        extra: dict | None = None,
        skip_if_ref_id_exists: bool = False,
    ):
        event_id = f"evt_{len(self._notifications) + 1}"
        self._notifications.append({
            "event_id": event_id,
            "source": source,
            "ref_id": ref_id,
            "body": body,
            "priority": priority,
            "extra": extra or {},
        })
        return event_id

    def _perform_refresh(self, *, skip_chat_history_save=False, skip_save_reason=None):
        self.refresh_calls.append({
            "skip_chat_history_save": skip_chat_history_save,
            "skip_save_reason": skip_save_reason,
        })


# ---------------------------------------------------------------------------
# WorkerStillRunningError fail-closed handling in the AED loop
# ---------------------------------------------------------------------------


def test_run_loop_skips_chat_history_save_after_worker_still_running(tmp_path, monkeypatch):
    """When _handle_message raises WorkerStillRunningError, the AED loop
    puts the agent ASLEEP with skip_post_turn_save=True so the in-process
    ChatInterface is not mutated while the worker future is still alive.
    No sentinel file is written."""
    agent = _make_run_loop_agent(tmp_path)
    agent.saves = 0
    agent._save_chat_history = lambda *a, **kw: setattr(agent, "saves", agent.saves + 1)

    def fake_handle(_agent, _msg):
        raise WorkerStillRunningError(elapsed=300.0, grace=5.0, agent_name="test")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert agent.saves == 0
    assert any(name == "chat_history_save_skipped" for name, _ in agent._logs)
    assert any(name == "llm_worker_still_running" for name, _ in agent._logs)
    # Interface is poisoned, a recovery artifact is written, a high-priority
    # notification is published, and a skip-save refresh is requested.
    assert agent._llm_worker_interface_poisoned is True
    assert agent._llm_worker_poison_artifact
    artifact = tmp_path / agent._llm_worker_poison_artifact
    assert artifact.is_file()
    artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert artifact_payload["type"] == "worker_still_running_recovery"
    assert artifact_payload["status"] == "open"
    assert artifact_payload["recovery"]["chat_history_saved_after_error"] is False
    assert artifact_payload["recovery"]["notification_ref_id"].startswith("worker_still_running:")
    assert agent._notifications
    assert agent._notifications[-1]["priority"] == "high"
    assert agent.refresh_calls[-1] == {
        "skip_chat_history_save": True,
        "skip_save_reason": "worker_still_running_interface_unsafe",
    }
    assert agent._asleep.is_set()
    # Both STUCK and ASLEEP must be written to .agent.json so the TUI's
    # state read is accurate and the heartbeat AED timeout doesn't see a
    # bare STUCK agent (which would trigger redundant recovery).
    assert AgentState.STUCK in agent._states
    assert AgentState.ASLEEP in agent._states
    assert not (tmp_path / ".llm_hang").exists()


def test_worker_hang_request_artifact_is_bounded_and_redacted(tmp_path):
    """The recovery artifact must bound and redact the request body — no
    secrets, no unbounded prompt, and explicit privacy flags."""
    from lingtai.kernel.base_agent.worker_recovery import (
        build_worker_hang_context,
        write_worker_hang_artifact,
    )

    agent = _make_run_loop_agent(tmp_path)
    secret = "sk-" + ("a" * 40)
    content = f"please use token={secret}\n" + ("x" * 2000)
    msg = _make_message(MSG_REQUEST, "human", content)
    exc = WorkerStillRunningError(elapsed=300.0, grace=5.0, agent_name="test")

    context = build_worker_hang_context(agent, msg, exc)
    relpath = write_worker_hang_artifact(agent, exc, context)

    assert relpath is not None
    artifact = json.loads((tmp_path / relpath).read_text(encoding="utf-8"))
    request = artifact["request"]
    assert request["content_chars"] == len(content)
    assert len(request["content_preview_redacted"]) <= 500
    assert secret not in request["content_preview_redacted"]
    assert request["content_sha256"]
    # The exact text is carried only in the declared `redo` block (the fresh
    # process's AED redo of this never-saved request), never in the preview.
    assert artifact["redo"]["message"]["content"] == content
    assert artifact["privacy"] == {
        "raw_chat_history_included": False,
        "raw_tool_args_included": False,
        "raw_tool_results_included": False,
        "previews_redacted": True,
        "max_preview_chars": 500,
        "redo_request_text_included": True,
    }


# ---------------------------------------------------------------------------
# AED transient provider retry
# ---------------------------------------------------------------------------


class _FakeInterface:
    def __init__(self):
        self.heals: list[tuple[str, bool]] = []

    def has_pending_tool_calls(self):
        return False

    def close_pending_tool_calls(self, *, reason: str, tool_completed: bool = False):
        self.heals.append((reason, tool_completed))


def _make_run_loop_agent(tmp_path):
    agent = _FakeAgent(tmp_path)
    agent.agent_name = "test"
    agent._shutdown = threading.Event()
    agent._cancel_event = threading.Event()
    agent._inbox_timeout = 0.01
    agent._reset_uptime = lambda: None
    agent._save_chat_history = lambda *a, **kw: None
    agent._config = SimpleNamespace(
        insights_interval=0,
        max_aed_attempts=10,
        language="en",
        time_awareness=True,
        timezone_awareness=True,
    )
    iface = _FakeInterface()
    agent._session = SimpleNamespace(
        chat=SimpleNamespace(interface=iface),
        _rebuild_session=lambda interface: setattr(agent, "rebuilds", getattr(agent, "rebuilds", 0) + 1),
    )
    agent.inbox = queue.Queue()
    agent.inbox.put(_make_message(MSG_REQUEST, "human", "go"))
    agent._preset_fallback_attempted = False
    agent._can_fallback_preset = lambda: False
    return agent


@pytest.mark.parametrize("initial_state", [AgentState.IDLE, AgentState.ASLEEP])
def test_fresh_dequeue_clears_only_stale_turn_cancel_latch(
    tmp_path, monkeypatch, initial_state
):
    """Both dequeue branches clear a prior turn's latch before ACTIVE."""
    agent = _make_run_loop_agent(tmp_path)
    agent._state = initial_state
    if initial_state is AgentState.ASLEEP:
        agent._asleep.set()
    agent._cancel_event.set()
    observed = []

    def fake_handle(current, _msg):
        observed.append((current._state, current._cancel_event.is_set()))
        current._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _agent: None)

    turn._run_loop(agent)

    assert observed == [(AgentState.ACTIVE, False)]
    assert not agent._cancel_event.is_set()


def test_cancel_requested_during_awake_concat_survives_turn(
    tmp_path, monkeypatch
):
    """The awake dequeue reset precedes merge, so a merge-time cancel survives."""
    agent = _make_run_loop_agent(tmp_path)
    agent._state = AgentState.IDLE
    agent._cancel_event.set()
    concat_entered = threading.Event()
    allow_concat_return = threading.Event()
    observed = []
    errors = []

    def blocking_concat(current, msg):
        assert not current._cancel_event.is_set()
        concat_entered.set()
        assert allow_concat_return.wait(timeout=5)
        return msg

    def fake_handle(current, _msg):
        observed.append(current._cancel_event.is_set())
        current._shutdown.set()

    def run():
        try:
            turn._run_loop(agent)
        except BaseException as exc:  # surfaced in the asserting thread below
            errors.append(exc)

    monkeypatch.setattr(turn, "_concat_queued_messages", blocking_concat)
    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    worker = threading.Thread(target=run, name="cancel-during-concat-test")
    worker.start()
    assert concat_entered.wait(timeout=5)
    agent._cancel_event.set()
    allow_concat_return.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert observed == [True]
    assert agent._cancel_event.is_set()


def test_partial_stream_marker_stops_before_transient_or_aed_retry(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    agent.saves = 0
    agent._save_chat_history = lambda *a, **kw: setattr(agent, "saves", agent.saves + 1)
    calls = {"n": 0}

    class _UnrenderableProviderError(RuntimeError):
        def __str__(self):
            raise RuntimeError("provider __str__ failed")

        def __repr__(self):
            raise RuntimeError("provider __repr__ failed")

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        _agent._shutdown.set()
        exc = _UnrenderableProviderError(
            "usage_limit_reached after visible output"
        )
        raise mark_llm_replay_terminal(
            exc,
            partial_stream=True,
            no_aed_retry=True,
            message="Visible provider output cannot be replayed",
        )

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert calls["n"] == 1
    assert getattr(agent, "rebuilds", 0) == 0
    assert agent.saves == 0
    assert any(name == "llm_partial_stream_terminal" for name, _ in agent._logs)
    assert not any(name == "llm_no_aed_retry_terminal" for name, _ in agent._logs)
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)


def test_provider_recovery_terminal_marker_stops_before_aed_rebuild(
    tmp_path, monkeypatch
):
    agent = _make_run_loop_agent(tmp_path)
    agent.reports = []
    agent._report_task_card_api_error = lambda exc, **kw: agent.reports.append(
        (exc, kw)
    )
    calls = {"n": 0}

    class _UnrenderableProviderError(RuntimeError):
        def __str__(self):
            raise RuntimeError("provider __str__ failed")

        def __repr__(self):
            raise RuntimeError("provider __repr__ failed")

    original_error = _UnrenderableProviderError(
        "bounded provider recovery exhausted"
    )
    terminal_error = mark_llm_replay_terminal(
        original_error,
        no_aed_retry=True,
        message="Provider recovery failed after bounded retry",
    )

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        _agent._shutdown.set()
        raise terminal_error

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert calls["n"] == 1
    assert getattr(agent, "rebuilds", 0) == 0
    assert agent._asleep.is_set()
    assert agent.reports and agent.reports[-1][0] is terminal_error
    assert type(terminal_error) is LLMReplayTerminalError
    assert terminal_error.original is original_error
    assert llm_replay_terminal_flags(terminal_error) == (False, True)
    assert agent.reports[-1][1]["terminal"] is True
    terminal_logs = [
        fields for name, fields in agent._logs
        if name == "llm_no_aed_retry_terminal"
    ]
    assert terminal_logs
    assert terminal_logs[-1]["error"] == "Provider recovery failed after bounded retry"
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)


def test_codex_provider_retry_budget_is_terminal_in_run_loop(tmp_path, monkeypatch):
    """One provider retry is the whole logical-request budget, including AED."""

    class _TokenExpired(Exception):
        status_code = 401
        body = {"error": {"code": "token_expired"}}

    class _Responses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise _TokenExpired("expired")
            raise RuntimeError("network failed after provider-owned retry")

    candidate = AccountCandidate("one.json", "account.json", 0, 1)
    source = SimpleNamespace(
        snapshot=lambda: [candidate],
        quota_targets=lambda exclude=None, snapshot=None: [
            (candidate.auth_ref, candidate.auth_path_sha8)
        ],
        select=lambda exclude=None, quota_left_snapshot=None, snapshot=None: candidate,
    )

    class _Manager:
        def __init__(self):
            self.refresh_calls = []
            self.token = "secret-one"

        def get_access_token(self):
            return self.token

        def get_account_id(self):
            return "acct-one"

        def refresh_access_token(self, rejected_access_token):
            self.refresh_calls.append(rejected_access_token)
            self.token = "recovered-one"
            return self.token

    manager = _Manager()
    responses = _Responses()
    adapter = CodexOpenAIAdapter(
        api_key="boot",
        base_url="http://codex.test",
        use_responses=True,
        force_responses=True,
        codex_account_source=source,
        codex_token_manager_factory=lambda **_kwargs: manager,
        codex_fallback_auth_path="one.json",
    )
    adapter._client = SimpleNamespace(responses=responses, api_key="boot")
    chat = adapter.create_chat("gpt-5.5", "system")
    agent = _make_run_loop_agent(tmp_path)
    agent._session.chat = chat
    agent._chat = chat
    agent.reports = []
    agent._report_task_card_api_error = lambda exc, **kw: agent.reports.append(
        (exc, kw)
    )
    handler_calls = {"n": 0}
    surfaced_errors = []

    def fake_handle(_agent, _msg):
        handler_calls["n"] += 1
        _agent._shutdown.set()
        try:
            chat.send("hello")
        except Exception as exc:
            surfaced_errors.append(exc)
            raise

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert handler_calls["n"] == 1
    assert len(responses.calls) == 2
    assert manager.refresh_calls == ["secret-one"]
    assert getattr(agent, "rebuilds", 0) == 0
    # ``_asleep`` is the run-loop's terminal ASLEEP boundary; the fake also sets
    # shutdown to end this synchronous test, so final state persistence is skipped.
    assert agent._asleep.is_set()
    assert agent.reports and agent.reports[-1][1]["terminal"] is True
    assert any(name == "llm_no_aed_retry_terminal" for name, _ in agent._logs)
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)


def test_codex_terminal_wrapper_survives_watchdog_settle_boundary(
    tmp_path, monkeypatch
):
    """A no-AED terminal wrapper that settles during the watchdog grace window
    must stay the escaping exception — never replaced by a plain transient
    TimeoutError that would reopen retries/AED past the consumed budget."""

    class _TokenExpired(Exception):
        status_code = 401
        body = {"error": {"code": "token_expired"}}

    class _Responses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise _TokenExpired("expired")
            # Cross the 50ms main-thread watchdog, then fail while the main
            # thread waits inside the settle grace window.
            time.sleep(0.08)
            raise RuntimeError("network failed after provider-owned retry")

    candidate = AccountCandidate("one.json", "account.json", 0, 1)
    source = SimpleNamespace(
        snapshot=lambda: [candidate],
        quota_targets=lambda exclude=None, snapshot=None: [
            (candidate.auth_ref, candidate.auth_path_sha8)
        ],
        select=lambda exclude=None, quota_left_snapshot=None, snapshot=None: candidate,
    )

    class _Manager:
        def __init__(self):
            self.refresh_calls = []
            self.token = "secret-one"

        def get_access_token(self):
            return self.token

        def get_account_id(self):
            return "acct-one"

        def refresh_access_token(self, rejected_access_token):
            self.refresh_calls.append(rejected_access_token)
            self.token = "recovered-one"
            return self.token

    manager = _Manager()
    responses = _Responses()
    adapter = CodexOpenAIAdapter(
        api_key="boot",
        base_url="http://codex.test",
        use_responses=True,
        force_responses=True,
        codex_account_source=source,
        codex_token_manager_factory=lambda **_kwargs: manager,
        codex_fallback_auth_path="one.json",
    )
    adapter._client = SimpleNamespace(responses=responses, api_key="boot")
    chat = adapter.create_chat("gpt-5.5", "system")
    agent = _make_run_loop_agent(tmp_path)
    agent._session.chat = chat
    agent._chat = chat
    agent.reports = []
    agent._report_task_card_api_error = lambda exc, **kw: agent.reports.append(
        (exc, kw)
    )
    handler_calls = {"n": 0}
    surfaced_errors = []
    pool = ThreadPoolExecutor(max_workers=1)

    def fake_handle(_agent, _msg):
        handler_calls["n"] += 1
        _agent._shutdown.set()
        try:
            send_with_timeout_stream(chat, "hello", pool, 0.05, "test", None)
        except Exception as exc:
            surfaced_errors.append(exc)
            raise

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    try:
        turn._run_loop(agent)
    finally:
        pool.shutdown(wait=True)

    assert handler_calls["n"] == 1
    assert len(responses.calls) == 2
    assert manager.refresh_calls == ["secret-one"]
    escaped = surfaced_errors[0]
    assert type(escaped) is LLMReplayTerminalError
    assert llm_replay_terminal_flags(escaped) == (False, True)
    assert type(escaped.original) is RuntimeError
    # The watchdog-aligned wire timeout reached both provider calls.
    assert all("timeout" in call for call in responses.calls)
    assert getattr(agent, "rebuilds", 0) == 0
    assert agent._asleep.is_set()
    assert agent.reports and agent.reports[-1][0] is escaped
    assert agent.reports[-1][1]["terminal"] is True
    assert any(name == "llm_no_aed_retry_terminal" for name, _ in agent._logs)
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)


@pytest.mark.parametrize(
    ("scenario", "expected_flags", "expected_log", "expected_calls"),
    [
        ("stateful_no_aed_getter", (False, True), "llm_no_aed_retry_terminal", 2),
        ("raising_no_aed_getter", (False, True), "llm_no_aed_retry_terminal", 2),
        ("stateful_dict_descriptor", (False, True), "llm_no_aed_retry_terminal", 2),
        ("silent_partial_setter", (True, False), "llm_partial_stream_terminal", 1),
        ("raising_partial_account_callback", (True, False), "llm_partial_stream_terminal", 1),
    ],
)
def test_codex_adapter_run_loop_uses_non_dispatching_replay_markers(
    tmp_path,
    monkeypatch,
    scenario,
    expected_flags,
    expected_log,
    expected_calls,
):
    """Provider hooks cannot change marker truth between adapter and run loop."""

    class _TokenExpired(Exception):
        status_code = 401
        body = {"error": {"code": "token_expired"}}

    class _HookedMarkerError(RuntimeError):
        def __init__(self, message):
            super().__init__(message)
            object.__setattr__(self, "marker_reads", 0)
            object.__setattr__(self, "marker_writes", 0)

        def __getattribute__(self, name):
            if name == "_lingtai_no_aed_retry":
                reads = object.__getattribute__(self, "marker_reads")
                object.__setattr__(self, "marker_reads", reads + 1)
                mode = object.__getattribute__(self, "scenario")
                if mode == "raising_no_aed_getter":
                    raise RuntimeError("provider marker getter failed")
                if mode == "stateful_no_aed_getter":
                    return reads == 0
            return super().__getattribute__(name)

        def __setattr__(self, name, value):
            if name == "_lingtai_partial_stream":
                writes = object.__getattribute__(self, "marker_writes")
                object.__setattr__(self, "marker_writes", writes + 1)
                if object.__getattribute__(self, "scenario") == "silent_partial_setter":
                    return
            super().__setattr__(name, value)

        def __str__(self):
            raise RuntimeError("provider __str__ failed")

        def __repr__(self):
            raise RuntimeError("provider __repr__ failed")

    class _StatefulDictError(_HookedMarkerError):
        def __init__(self, message):
            super().__init__(message)
            object.__setattr__(self, "dict_reads", 0)

        @property
        def __dict__(self):
            reads = object.__getattribute__(self, "dict_reads")
            object.__setattr__(self, "dict_reads", reads + 1)
            return {"_lingtai_no_aed_retry": reads == 0}

    error_type = (
        _StatefulDictError
        if scenario == "stateful_dict_descriptor"
        else _HookedMarkerError
    )
    terminal_error = error_type("provider terminal failure")
    object.__setattr__(terminal_error, "scenario", scenario)

    class _Responses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if scenario not in {
                "silent_partial_setter",
                "raising_partial_account_callback",
            }:
                if len(self.calls) == 1:
                    raise _TokenExpired("expired")
                raise terminal_error

            def partial_events():
                yield SimpleNamespace(
                    type="response.output_text.delta",
                    delta="visible once",
                )
                raise terminal_error

            return partial_events()

    candidate = AccountCandidate("one.json", "account.json", 0, 1)
    source = SimpleNamespace(
        snapshot=lambda: [candidate],
        quota_targets=lambda exclude=None, snapshot=None: [
            (candidate.auth_ref, candidate.auth_path_sha8)
        ],
        select=lambda exclude=None, quota_left_snapshot=None, snapshot=None: candidate,
    )

    class _Manager:
        def __init__(self):
            self.refresh_calls = []
            self.token = "secret-one"

        def get_access_token(self):
            return self.token

        def get_account_id(self):
            return "acct-one"

        def refresh_access_token(self, rejected_access_token):
            self.refresh_calls.append(rejected_access_token)
            self.token = "recovered-one"
            return self.token

    manager = _Manager()
    responses = _Responses()
    adapter = CodexOpenAIAdapter(
        api_key="boot",
        base_url="http://codex.test",
        use_responses=True,
        force_responses=True,
        codex_account_source=source,
        codex_token_manager_factory=lambda **_kwargs: manager,
        codex_fallback_auth_path="one.json",
    )
    adapter._client = SimpleNamespace(responses=responses, api_key="boot")
    chat = adapter.create_chat("gpt-5.5", "system")
    callback_calls = []
    if scenario == "raising_partial_account_callback":
        def raise_account_callback(exc, partial_output):
            callback_calls.append((exc, partial_output))
            raise RuntimeError("secondary account callback failed")

        chat._codex_account_error_callback = raise_account_callback

    agent = _make_run_loop_agent(tmp_path)
    agent._session.chat = chat
    agent._chat = chat
    agent.reports = []
    agent._report_task_card_api_error = lambda exc, **kw: agent.reports.append(
        (exc, kw)
    )
    handler_calls = {"n": 0}
    surfaced_errors = []

    def fake_handle(_agent, _msg):
        handler_calls["n"] += 1
        _agent._shutdown.set()
        try:
            chat.send("hello")
        except Exception as exc:
            surfaced_errors.append(exc)
            raise

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(
        soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set()
    )

    turn._run_loop(agent)

    assert handler_calls["n"] == 1
    assert len(responses.calls) == expected_calls
    assert manager.refresh_calls == (
        ["secret-one"] if expected_calls == 2 else []
    )
    assert getattr(agent, "rebuilds", 0) == 0
    assert len(surfaced_errors) == 1
    surfaced = surfaced_errors[0]
    assert type(surfaced) is LLMReplayTerminalError
    assert surfaced.original is terminal_error
    assert llm_replay_terminal_flags(surfaced) == expected_flags
    assert llm_replay_terminal_flags(terminal_error) == (False, False)
    if expected_log == "llm_partial_stream_terminal":
        # Visible output ends only this turn; the agent stays available and the
        # partial terminal is not duplicated onto the Task Card.
        assert not agent._asleep.is_set()
        assert agent.reports == []
    else:
        assert agent._asleep.is_set()
        assert agent.reports and agent.reports[-1][0] is surfaced
        assert agent.reports[-1][1]["terminal"] is True
    assert object.__getattribute__(terminal_error, "marker_reads") == 0
    assert object.__getattribute__(terminal_error, "marker_writes") == 0
    if scenario == "stateful_dict_descriptor":
        assert object.__getattribute__(terminal_error, "dict_reads") == 0
    if scenario == "raising_partial_account_callback":
        assert callback_calls == [(terminal_error, True)]
    else:
        assert callback_calls == []
    assert any(name == expected_log for name, _ in agent._logs)
    other_terminal = (
        "llm_partial_stream_terminal"
        if expected_log == "llm_no_aed_retry_terminal"
        else "llm_no_aed_retry_terminal"
    )
    assert not any(name == other_terminal for name, _ in agent._logs)
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)



def test_codex_post_recovery_tail_failure_is_terminal_in_run_loop(
    tmp_path, monkeypatch
):
    """Bookkeeping after a successful provider retry cannot reopen AED."""

    class _TokenExpired(Exception):
        status_code = 401
        body = {"error": {"code": "token_expired"}}

    class _Responses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise _TokenExpired("expired")
            usage = SimpleNamespace(
                input_tokens=10,
                output_tokens=2,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
            return iter([
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(id="resp", usage=usage),
                ),
            ])

    candidate = AccountCandidate("one.json", "account.json", 0, 1)
    source = SimpleNamespace(
        snapshot=lambda: [candidate],
        quota_targets=lambda exclude=None, snapshot=None: [
            (candidate.auth_ref, candidate.auth_path_sha8)
        ],
        select=lambda exclude=None, quota_left_snapshot=None, snapshot=None: candidate,
    )

    class _Manager:
        def __init__(self):
            self.refresh_calls = []
            self.token = "secret-one"

        def get_access_token(self):
            return self.token

        def get_account_id(self):
            return "acct-one"

        def refresh_access_token(self, rejected_access_token):
            self.refresh_calls.append(rejected_access_token)
            self.token = "recovered-one"
            return self.token

    manager = _Manager()
    responses = _Responses()
    adapter = CodexOpenAIAdapter(
        api_key="boot",
        base_url="http://codex.test",
        use_responses=True,
        force_responses=True,
        codex_account_source=source,
        codex_token_manager_factory=lambda **_kwargs: manager,
        codex_fallback_auth_path="one.json",
    )
    adapter._client = SimpleNamespace(responses=responses, api_key="boot")
    chat = adapter.create_chat("gpt-5.5", "system")

    class _SilentMarkerUnrenderableError(RuntimeError):
        @property
        def __dict__(self):
            raise RuntimeError("provider __dict__ failed")

        def __setattr__(self, name, value):
            if name == "_lingtai_no_aed_retry":
                return
            super().__setattr__(name, value)

        def __str__(self):
            raise RuntimeError("provider __str__ failed")

        def __repr__(self):
            raise RuntimeError("provider __repr__ failed")

    tail_error = _SilentMarkerUnrenderableError(
        "baseline bookkeeping failed after provider retry"
    )

    def fail_baseline():
        raise tail_error

    chat._ws_record_baseline_from_interface = fail_baseline

    agent = _make_run_loop_agent(tmp_path)
    agent._session.chat = chat
    agent._chat = chat
    agent.reports = []
    agent._report_task_card_api_error = lambda exc, **kw: agent.reports.append(
        (exc, kw)
    )
    handler_calls = {"n": 0}

    def fake_handle(_agent, _msg):
        handler_calls["n"] += 1
        _agent._shutdown.set()
        chat.send("hello")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert handler_calls["n"] == 1
    assert len(responses.calls) == 2
    assert manager.refresh_calls == ["secret-one"]
    assert getattr(agent, "rebuilds", 0) == 0
    assert agent._asleep.is_set()
    assert agent.reports
    reported_error = agent.reports[-1][0]
    assert reported_error is not tail_error
    assert getattr(reported_error, "_lingtai_no_aed_retry", False) is True
    assert getattr(reported_error, "original", None) is tail_error
    assert reported_error.__cause__ is tail_error
    assert str(reported_error) == "Provider recovery failed after bounded retry"
    assert agent.reports[-1][1]["terminal"] is True
    assert any(name == "llm_no_aed_retry_terminal" for name, _ in agent._logs)
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)
    assert [entry.role for entry in chat.interface.entries] == ["system", "user"]
    assert chat._ws_epoch_reset_reason_pending == "provider_recovery_terminal"
    assert chat._ws_session.last_response is None
    assert chat._response_id is None


def test_codex_post_recovery_snapshot_failure_is_terminal_in_run_loop(
    tmp_path, monkeypatch
):
    """A failing rollback snapshot cannot reopen AED after visible recovery."""

    class _TokenExpired(Exception):
        status_code = 401
        body = {"error": {"code": "token_expired"}}

    class _Responses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise _TokenExpired("expired")
            usage = SimpleNamespace(
                input_tokens=10,
                output_tokens=2,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
                output_tokens_details=SimpleNamespace(reasoning_tokens=0),
            )
            return iter([
                SimpleNamespace(
                    type="response.output_text.delta",
                    delta="visible recovered output",
                ),
                SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(id="resp", usage=usage),
                ),
            ])

    candidate = AccountCandidate("one.json", "account.json", 0, 1)
    source = SimpleNamespace(
        snapshot=lambda: [candidate],
        quota_targets=lambda exclude=None, snapshot=None: [
            (candidate.auth_ref, candidate.auth_path_sha8)
        ],
        select=lambda exclude=None, quota_left_snapshot=None, snapshot=None: candidate,
    )

    class _Manager:
        def __init__(self):
            self.refresh_calls = []
            self.token = "secret-one"

        def get_access_token(self):
            return self.token

        def get_account_id(self):
            return "acct-one"

        def refresh_access_token(self, rejected_access_token):
            self.refresh_calls.append(rejected_access_token)
            self.token = "recovered-one"
            return self.token

    manager = _Manager()
    responses = _Responses()
    adapter = CodexOpenAIAdapter(
        api_key="boot",
        base_url="http://codex.test",
        use_responses=True,
        force_responses=True,
        codex_account_source=source,
        codex_token_manager_factory=lambda **_kwargs: manager,
        codex_fallback_auth_path="one.json",
    )
    adapter._client = SimpleNamespace(responses=responses, api_key="boot")
    chat = adapter.create_chat("gpt-5.5", "system")

    # Request conversion tolerates this mutable legacy entry shape, while the
    # post-finalize frozenset snapshot raises TypeError after recovered output.
    chat.interface.entries[0].id = []

    agent = _make_run_loop_agent(tmp_path)
    agent._session.chat = chat
    agent._chat = chat
    agent.reports = []
    agent._report_task_card_api_error = lambda exc, **kw: agent.reports.append(
        (exc, kw)
    )
    handler_calls = {"n": 0}
    surfaced_errors = []

    def fake_handle(_agent, _msg):
        handler_calls["n"] += 1
        _agent._shutdown.set()
        try:
            chat.send("hello")
        except Exception as exc:
            surfaced_errors.append(exc)
            raise

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert handler_calls["n"] == 1
    assert len(responses.calls) == 2
    assert manager.refresh_calls == ["secret-one"]
    assert getattr(agent, "rebuilds", 0) == 0
    assert len(surfaced_errors) == 1
    surfaced = surfaced_errors[0]
    assert type(surfaced) is LLMReplayTerminalError
    assert type(surfaced.original) is TypeError
    assert surfaced.__cause__ is surfaced.original
    assert llm_replay_terminal_flags(surfaced) == (True, True)
    assert not agent._asleep.is_set()
    assert agent.reports == []
    assert any(name == "llm_partial_stream_terminal" for name, _ in agent._logs)
    assert not any(name == "llm_no_aed_retry_terminal" for name, _ in agent._logs)
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)
    assert [entry.role for entry in chat.interface.entries] == ["system", "user"]
    assert chat._ws_epoch_reset_reason_pending == "provider_recovery_terminal"
    assert chat._ws_session.last_response is None
    assert chat._response_id is None


def test_no_candidate_error_is_terminal_without_aed_retry(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    agent.reports = []
    agent._report_task_card_api_error = lambda exc, **kw: agent.reports.append((exc, kw))
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        _agent._shutdown.set()
        raise NoCandidateError(
            "No eligible account remaining",
            diagnostics={
                "codex_account_pool_size": 2,
                "codex_account_quota_read_error_count": 1,
                "secret_path": "/tmp/token.json",
                "no_candidate_token": "secret-token-value",
            },
        )

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert calls["n"] == 1
    assert getattr(agent, "rebuilds", 0) == 0
    assert agent._asleep.is_set()
    logs = [fields for name, fields in agent._logs if name == "no_candidate_terminal"]
    assert len(logs) == 1
    assert logs[0]["codex_account_pool_size"] == 2
    assert logs[0]["codex_account_quota_read_error_count"] == 1
    assert "/tmp/token.json" not in repr(logs[0])
    assert "secret-token-value" not in repr(logs[0])
    assert not any(name == "aed_attempt" for name, _ in agent._logs)
    assert len(agent.reports) == 1
    assert agent.reports[0][0].args == ("No eligible account remaining",)
    assert agent.reports[0][1] == {
        "attempt": None,
        "max_attempts": None,
        "terminal": True,
    }


def test_ordinary_exception_keeps_aed_rebuild_behavior(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 3
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        if calls["n"] == 3:
            _agent._shutdown.set()
            return
        raise ValueError("ordinary failure")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert calls["n"] == 3
    assert getattr(agent, "rebuilds", 0) == 2
    assert [name for name, _ in agent._logs].count("aed_attempt") == 2


def test_transient_provider_error_retries_before_aed_count(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("An error occurred while processing your request")
        _agent._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert calls["n"] == 3
    assert [name for name, _ in agent._logs].count("aed_transient_retry") == 2
    assert not any(name == "aed_attempt" for name, _ in agent._logs)
    assert getattr(agent, "rebuilds", 0) == 0
    assert all(tool_completed for _, tool_completed in agent._session.chat.interface.heals)


def test_transient_provider_error_counts_as_aed_after_retry_budget(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        raise RuntimeError("peer closed connection without sending complete message body")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert calls["n"] == turn._TRANSIENT_AED_RETRY_LIMIT + 1
    assert [name for name, _ in agent._logs].count("aed_transient_retry") == turn._TRANSIENT_AED_RETRY_LIMIT
    assert any(name == "aed_transient_exhausted" for name, _ in agent._logs)
    assert any(name == "aed_attempt" and fields["attempt"] == 1 for name, fields in agent._logs)
    assert any(name == "aed_exhausted" for name, _ in agent._logs)
    assert agent._asleep.is_set()


def test_aed_exhaust_sends_one_sanitized_notice_to_origin(tmp_path, monkeypatch):
    """lingtai#672: when AED exhausts (deterministic provider failure), the
    run loop sends exactly ONE sanitized, user-safe Telegram message to the
    originating conversation through the existing telegram tool handler
    before going ASLEEP. The notice must never embed the raw error text."""
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1

    sent: list[dict] = []

    def fake_telegram(args: dict) -> dict:
        sent.append(args)
        return {"status": "sent", "message_id": "acct:123:1"}

    agent._tool_handlers = {"telegram": fake_telegram}

    class _FakeNotificationStore:
        def snapshot(self, _allow):
            return {
                "mcp.telegram": {
                    "data": {
                        "previews": [
                            {"message_ref": "acct:123:456"},
                        ],
                    },
                },
            }

    agent._notification_store = _FakeNotificationStore()

    def fake_handle(_agent, _msg):
        # Deterministic non-429 provider failure so the generic AED exhausted
        # branch is exercised (429 has its own rate-limit branch, #1299).
        raise _StatusError(500)

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    # Exactly one sanitized send, targeting the origin chat, plain text.
    assert len(sent) == 1
    args = sent[0]
    assert args["action"] == "send"
    assert args["input"]["account"] == "acct"
    assert args["input"]["chat_id"] == 123
    assert args["input"]["rendering_mode"] == "plain_text"
    # Sanitized: no raw provider error text, no secrets, no paths.
    assert "429" not in args["input"]["text"]
    assert "Too Many" not in args["input"]["text"]
    assert "tmp_path" not in args["input"]["text"]
    assert any(name == "aed_exhausted" for name, _ in agent._logs)
    assert any(name == "aed_exhausted_notice" for name, _ in agent._logs)
    # P1-2: the observable agent state is ASLEEP, not a terminal STUCK, and
    # the sleep event is set.
    assert agent._asleep.is_set()
    assert agent._state == AgentState.ASLEEP
    assert AgentState.ASLEEP in agent._states


def test_aed_exhaust_notice_uses_origin_captured_at_dequeue(tmp_path, monkeypatch):
    """P1-1: the AED notice consumes the origin captured when the turn was
    dequeued, NOT a live re-read of the notification snapshot. If a later chat
    replaces the snapshot during the failed turn, the notice still goes to the
    original chat exactly once."""
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1

    sent: list[dict] = []

    def fake_telegram(args: dict) -> dict:
        sent.append(args)
        return {"status": "sent", "message_id": "acct:111:1"}

    agent._tool_handlers = {"telegram": fake_telegram}

    # Snapshot state AT DEQUEUE: origin chat 111.
    dequeue_snapshot = {
        "mcp.telegram": {
            "data": {"previews": [{"message_ref": "acct:111:456"}]},
        },
    }
    # Snapshot state AT EXHAUSTION: a later chat (222) replaced it.
    later_snapshot = {
        "mcp.telegram": {
            "data": {"previews": [{"message_ref": "acct:222:789"}]},
        },
    }
    state = {"n": 0}

    class _FakeNotificationStore:
        def snapshot(self, _allow):
            state["n"] += 1
            # First read = dequeue-time capture; any later read = live snapshot.
            return dequeue_snapshot if state["n"] == 1 else later_snapshot

    agent._notification_store = _FakeNotificationStore()

    def fake_handle(_agent, _msg):
        raise _StatusError(500)

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    # Exactly one notice to the ORIGINAL chat, never to the later one.
    assert len(sent) == 1
    assert sent[0]["input"]["account"] == "acct"
    assert sent[0]["input"]["chat_id"] == 111


def test_aed_exhaust_notice_rejects_malformed_route(tmp_path, monkeypatch):
    """P1-1: ambiguous/malformed Telegram routes (empty account, missing
    message-id, extra segment, synthetic updates bucket, non-numeric ids) are
    rejected; no notice is sent to a fabricated chat and AED still exhausts."""
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1

    sent: list[dict] = []

    def fake_telegram(args: dict) -> dict:
        sent.append(args)
        return {"status": "sent"}

    agent._tool_handlers = {"telegram": fake_telegram}

    malformed_refs = [
        ":123:456",        # empty account
        "acct:123",         # missing message-id
        "acct:123:456:789", # extra segment
        "acct:updates:456", # synthetic events bucket
        "acct:abc:456",     # non-numeric chat id
        "acct:123:xyz",     # non-numeric message id
        "acct:123:-1",      # negative message id
        "",                 # empty ref
        12345,              # non-string
    ]

    # Run one iteration per malformed ref: use a fresh agent each time so the
    # sleep event / shutdown state cannot leak across runs.
    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    for ref in malformed_refs:
        agent = _make_run_loop_agent(tmp_path)
        agent._config.max_aed_attempts = 1
        agent._tool_handlers = {"telegram": fake_telegram}
        sent.clear()

        class _PerRefStore:
            def snapshot(self, _allow):
                return {
                    "mcp.telegram": {
                        "data": {"previews": [{"message_ref": ref}]},
                    },
                }

        agent._notification_store = _PerRefStore()

        def fake_handle(_agent, _msg):
            # Deterministic non-429 provider failure (429 has its own branch).
            raise _StatusError(500)

        monkeypatch.setattr(turn, "_handle_message", fake_handle)
        turn._run_loop(agent)

        assert not sent, f"malformed ref {ref!r} produced a send"
        assert agent._asleep.is_set()
        assert agent._state == AgentState.ASLEEP


def test_aed_exhaust_notice_records_returned_error_as_failed(tmp_path, monkeypatch):
    """P2-1: when the telegram handler RETURNS an error mapping (instead of
    raising), the notice is recorded as failed — not falsely as sent — and the
    ASLEEP transition still happens fail-open."""
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1

    def fake_telegram(args: dict) -> dict:
        return {"status": "error", "error": "send failed: token=sk-secret /tmp/private"}

    agent._tool_handlers = {"telegram": fake_telegram}

    class _FakeNotificationStore:
        def snapshot(self, _allow):
            return {
                "mcp.telegram": {
                    "data": {"previews": [{"message_ref": "acct:123:456"}]},
                },
            }

    agent._notification_store = _FakeNotificationStore()

    def fake_handle(_agent, _msg):
        raise _StatusError(500)

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert any(
        name == "aed_exhausted_notice" and fields.get("status") == "failed"
        for name, fields in agent._logs
    )
    assert not any(
        name == "aed_exhausted_notice" and fields.get("status") == "sent"
        for name, fields in agent._logs
    )
    assert agent._asleep.is_set()
    assert agent._state == AgentState.ASLEEP


def test_aed_exhaust_notice_fail_open_when_telegram_handler_missing(tmp_path, monkeypatch):
    """lingtai#672: without a telegram tool handler (or origin route), the
    AED-exhaust notice is skipped fail-open and the ASLEEP transition still
    happens; the AED decision is never affected by the notice."""
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1
    agent._tool_handlers = {}  # no telegram

    def fake_handle(_agent, _msg):
        raise _StatusError(500)

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert any(name == "aed_exhausted" for name, _ in agent._logs)
    assert not any(name == "aed_exhausted_notice" for name, _ in agent._logs)
    assert agent._asleep.is_set()


def test_structural_error_skips_transient_retry(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1

    def fake_handle(_agent, _msg):
        raise ValueError("bad schema")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert any(name == "aed_attempt" and fields["attempt"] == 1 for name, fields in agent._logs)













# ---------------------------------------------------------------------------
# Issue #593: 429 rate-limit backoff (Retry-After aware) and 4xx fail-fast
# ---------------------------------------------------------------------------


class _StatusError(Exception):
    """Minimal provider-shaped error exposing status_code + optional headers/body."""

    def __init__(self, status_code: int, *, headers=None, body=None, message=None):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(
            headers=headers or {},
            status_code=status_code,
        )
        if body is not None:
            self.body = body


class _UnrenderableHostileError(RuntimeError):
    """Hostile renderer: str() raises, exactly the P1 threat model."""

    def __str__(self):
        raise RuntimeError("provider __str__ failed")


def test_rate_limit_classifier():
    assert turn._is_rate_limit_error(_StatusError(429)) is True
    assert turn._is_rate_limit_error(_StatusError(400)) is False
    assert turn._is_rate_limit_error(_StatusError(503)) is False
    assert turn._is_rate_limit_error(RuntimeError("usage_limit_reached")) is True
    assert turn._is_rate_limit_error(RuntimeError("rate limit exceeded")) is True
    assert turn._is_rate_limit_error(RuntimeError("boom")) is False
    # P1 regression: hostile __str__ must not escape the classifier (render-safe).
    assert turn._is_rate_limit_error(_UnrenderableHostileError()) is False


def test_client_error_classifier():
    assert turn._is_client_error(_StatusError(400)) is True
    assert turn._is_client_error(_StatusError(401)) is True
    assert turn._is_client_error(_StatusError(429)) is False
    assert turn._is_client_error(_StatusError(503)) is False
    assert turn._is_client_error(RuntimeError("400 bad request")) is True
    assert turn._is_client_error(RuntimeError("messages_parameter_illegal")) is True
    assert turn._is_client_error(RuntimeError("boom")) is False
    # P1 regression: hostile __str__ must not escape the classifier (render-safe).
    assert turn._is_client_error(_UnrenderableHostileError()) is False


def test_retry_after_extraction_shapes():
    assert turn._rate_limit_retry_after_seconds(
        _StatusError(429, headers={"Retry-After": "42"})
    ) == 42.0
    assert turn._rate_limit_retry_after_seconds(
        _StatusError(429, headers={"retry-after": "120"})
    ) == 120.0
    assert turn._rate_limit_retry_after_seconds(
        _StatusError(429, body={"resets_in_seconds": 24896})
    ) == 24896.0
    err = RuntimeError("usage limit reached; resets_in_seconds: 15")
    assert turn._rate_limit_retry_after_seconds(err) == 15.0
    assert turn._rate_limit_retry_after_seconds(_StatusError(429)) is None
    assert turn._rate_limit_retry_after_seconds(
        _StatusError(429, headers={"X-Other": "1"})
    ) is None
    # P1 regression: hostile __str__ must not escape the regex fallback (render-safe).
    assert turn._rate_limit_retry_after_seconds(_UnrenderableHostileError()) is None


def test_retry_after_http_date_parsing():
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(seconds=90)
    http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = turn._rate_limit_retry_after_seconds(
        _StatusError(429, headers={"Retry-After": http_date})
    )
    assert parsed is not None and 80 <= parsed <= 100


def test_rate_limit_error_retries_honoring_retry_after(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    calls = {"n": 0}
    waits: list[float] = []

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _StatusError(429, headers={"Retry-After": "3"})
        _agent._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(agent._shutdown, "wait", lambda s: waits.append(s) or False)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert calls["n"] == 3
    rate_logs = [f for name, f in agent._logs if name == "aed_rate_limit_retry"]
    assert len(rate_logs) == 2
    assert all(f["retry_after_s"] == 3.0 for f in rate_logs)
    assert all(f["backoff_s"] == 3.0 for f in rate_logs)
    assert waits == [3.0, 3.0]
    assert not any(name == "aed_attempt" for name, _ in agent._logs)
    # Rate-limit recovery must not compact history (quota is the problem,
    # not the wire) and must not consume the transient backoff budget.
    assert not any(name == "aed_history_compacted" for name, _ in agent._logs)
    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)


def test_rate_limit_error_without_retry_after_uses_exponential_backoff(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    calls = {"n": 0}
    waits: list[float] = []

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        raise _StatusError(429)

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(agent._shutdown, "wait", lambda s: waits.append(s) or False)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert calls["n"] == turn._RATE_LIMIT_RETRY_LIMIT + 1
    rate_logs = [f for name, f in agent._logs if name == "aed_rate_limit_retry"]
    assert len(rate_logs) == turn._RATE_LIMIT_RETRY_LIMIT
    assert [f["backoff_s"] for f in rate_logs] == [5.0, 10.0, 20.0]
    assert [f["retry_after_s"] for f in rate_logs] == [None, None, None]
    assert waits == [5.0, 10.0, 20.0]
    assert any(name == "aed_rate_limit_exhausted" for name, _ in agent._logs)
    assert agent._asleep.is_set()
    assert not any(name == "aed_history_compacted" for name, _ in agent._logs)
    assert not any(name == "aed_attempt" for name, _ in agent._logs)


def test_client_error_fails_fast_when_compaction_cannot_change_wire(tmp_path, monkeypatch):
    from lingtai.kernel.tool_result_artifacts import CompactionStats

    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 3
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        raise _StatusError(400, message="messages_parameter_illegal")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(
        turn,
        "_compact_history_before_retry",
        lambda _agent, *, source: CompactionStats(compacted_blocks=0),
    )

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    # One API attempt only: compaction changed nothing, so the remaining AED
    # budget is not burned on an identical failing wire.
    assert calls["n"] == 1
    assert any(name == "aed_client_error_noop" for name, _ in agent._logs)
    assert any(name == "aed_attempt" and f["attempt"] == 1 for name, f in agent._logs)
    assert not any(name == "aed_attempt" and f["attempt"] == 2 for name, f in agent._logs)
    assert agent._asleep.is_set()
    assert getattr(agent, "rebuilds", 0) == 0


def test_client_error_retries_when_compaction_changed_wire(tmp_path, monkeypatch):
    from lingtai.kernel.tool_result_artifacts import CompactionStats

    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 3
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _StatusError(400, message="messages_parameter_illegal")
        _agent._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(
        turn,
        "_compact_history_before_retry",
        lambda _agent, *, source: CompactionStats(compacted_blocks=2),
    )

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert calls["n"] == 2
    assert not any(name == "aed_client_error_noop" for name, _ in agent._logs)
    assert getattr(agent, "rebuilds", 0) == 1


def test_over_window_zero_progress_compaction_aborts_aed_retry_loop(tmp_path, monkeypatch):
    """Issue #713: an over-window error followed by a zero-progress
    retroactive compaction is a doomed retry — the rebuilt wire is the same
    size (plus the retry prompt), so further AED attempts can never succeed.
    The run loop must abort the wake after the first attempt instead of
    burning the full AED budget (max_aed_attempts can be configured as high
    as 99, which produced an 11-day zombie / 34.9M wasted input tokens)."""
    from lingtai.kernel.tool_result_artifacts import CompactionStats

    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 10
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        raise RuntimeError("Your input exceeds the context window of this model.")

    def fake_compact(_agent, *, source):
        return CompactionStats(scanned_blocks=87, compacted_blocks=0)

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn, "_compact_history_before_retry", fake_compact)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert calls["n"] == 1
    assert getattr(agent, "rebuilds", 0) == 0
    assert agent._asleep.is_set()
    assert [name for name, _ in agent._logs].count("aed_attempt") == 1
    assert any(
        name == "aed_zero_progress_abort" and fields["scanned_blocks"] == 87
        for name, fields in agent._logs
    )
    assert any(name == "aed_exhausted" for name, _ in agent._logs)


def test_over_window_zero_progress_abort_reports_terminal_to_task_card(tmp_path, monkeypatch):
    """Issue #713: the task-card error row must be flipped terminal when the
    wake is aborted on zero-progress compaction — the turn is observably
    done (the same wire will fail identically), not left as a retrying row."""
    from lingtai.kernel.tool_result_artifacts import CompactionStats

    agent = _make_run_loop_agent(tmp_path)
    agent.reports = []
    agent._report_task_card_api_error = (
        lambda exc, **kw: agent.reports.append((exc, kw))
    )

    def fake_handle(_agent, _msg):
        raise RuntimeError("prompt is too long: maximum context length exceeded")

    def fake_compact(_agent, *, source):
        return CompactionStats(scanned_blocks=1, compacted_blocks=0)

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn, "_compact_history_before_retry", fake_compact)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert any(name == "aed_zero_progress_abort" for name, _ in agent._logs)
    terminal_reports = [kw for _, kw in agent.reports if kw.get("terminal")]
    assert terminal_reports
    assert terminal_reports[-1]["attempt"] == 1
    assert terminal_reports[-1]["max_attempts"] == 10


def test_over_window_compaction_with_progress_keeps_aed_retry(tmp_path, monkeypatch):
    """Issue #713 regression guard: when retroactive compaction actually frees
    blocks, the AED retry must still proceed — the wire shrank, so the next
    attempt has a real chance of succeeding."""
    from lingtai.kernel.tool_result_artifacts import CompactionStats

    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 10
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Your input exceeds the context window of this model.")
        _agent._shutdown.set()

    def fake_compact(_agent, *, source):
        return CompactionStats(
            scanned_blocks=87,
            compacted_blocks=3,
            original_chars_total=15000,
            replacement_chars_total=600,
        )

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn, "_compact_history_before_retry", fake_compact)

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert calls["n"] == 2
    assert getattr(agent, "rebuilds", 0) == 1
    assert [name for name, _ in agent._logs].count("aed_attempt") == 1
    assert not any(name == "aed_zero_progress_abort" for name, _ in agent._logs)
    assert not agent._asleep.is_set()


def test_empty_llm_response_is_classified_transient():
    err = turn.EmptyLLMResponseError(ledger_source="main", in_tool_loop=False)
    assert turn._is_transient_provider_error(err) is True


def test_status_code_classifier_treats_only_5xx_as_transient():
    class StatusError(Exception):
        def __init__(self, status_code: int):
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    assert turn._is_transient_provider_error(StatusError(503)) is True
    assert turn._is_transient_provider_error(StatusError(429)) is False
    assert turn._is_transient_provider_error(StatusError(400)) is False


def test_empty_llm_response_error_carries_provider_diagnostics():
    logs: list[tuple[str, dict]] = []
    agent = SimpleNamespace(
        _cancel_event=threading.Event(),
        _executor=SimpleNamespace(guard=SimpleNamespace()),
        _log=lambda event, **fields: logs.append((event, fields)),
    )
    raw = SimpleNamespace(
        id="resp_123",
        model="gpt-test",
        choices=[SimpleNamespace(finish_reason="stop")],
    )
    response = LLMResponse(
        text="",
        tool_calls=[],
        thoughts=[],
        usage=UsageMetadata(output_tokens=0, thinking_tokens=0),
        raw=raw,
        api_call_id="api_123",
    )

    with pytest.raises(turn.EmptyLLMResponseError) as excinfo:
        turn._process_response(agent, response, ledger_source="tc_wake")

    err = excinfo.value
    assert err.ledger_source == "tc_wake"
    assert err.in_tool_loop is False
    assert err.response_id == "resp_123"
    assert err.response_model == "gpt-test"
    assert err.finish_reason == "stop"
    assert err.api_call_id == "api_123"
    assert err.diagnostic_fields() == {
        "ledger_source": "tc_wake",
        "in_tool_loop": False,
        "response_id": "resp_123",
        "response_model": "gpt-test",
        "finish_reason": "stop",
        "api_call_id": "api_123",
    }
    assert any(
        event == "empty_llm_response"
        and fields["response_id"] == "resp_123"
        and fields["api_call_id"] == "api_123"
        for event, fields in logs
    )




def test_empty_llm_response_allows_missing_usage_metadata():
    logs: list[tuple[str, dict]] = []
    agent = SimpleNamespace(
        _cancel_event=threading.Event(),
        _executor=SimpleNamespace(guard=SimpleNamespace()),
        _log=lambda event, **fields: logs.append((event, fields)),
    )
    response = LLMResponse(
        text="",
        tool_calls=[],
        thoughts=[],
        usage=None,
        raw=None,
        api_call_id="api_no_usage",
    )

    with pytest.raises(turn.EmptyLLMResponseError) as excinfo:
        turn._process_response(agent, response, ledger_source="tc_wake")

    assert excinfo.value.api_call_id == "api_no_usage"
    assert logs == [
        (
            "empty_llm_response",
            {
                "ledger_source": "tc_wake",
                "in_tool_loop": False,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "api_call_id": "api_no_usage",
            },
        )
    ]

def test_tc_wake_error_logs_empty_response_diagnostics(tmp_path):
    from lingtai.kernel.llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock
    from lingtai.kernel.message import _make_message, MSG_TC_WAKE

    iface = ChatInterface()
    iface.add_assistant_message([ToolCallBlock(id="call_notification", name="system", args={})])
    iface.add_user_blocks([
        ToolResultBlock(id="call_notification", name="system", content={"ok": True})
    ])

    logs: list[tuple[str, dict]] = []
    raw = SimpleNamespace(
        id="resp_tc",
        model="gpt-test",
        choices=[SimpleNamespace(finish_reason="stop")],
    )
    response = LLMResponse(
        text="",
        tool_calls=[],
        thoughts=[],
        usage=UsageMetadata(output_tokens=0, thinking_tokens=0),
        raw=raw,
        api_call_id="api_tc",
    )

    agent = SimpleNamespace(
        _chat=SimpleNamespace(interface=iface),
        _tc_inbox=SimpleNamespace(drain=lambda: [], enqueue=lambda item: None),
        _appendix_ids_by_source={},
        _dispatch_tool=lambda *a, **kw: None,
        service=SimpleNamespace(make_tool_result=lambda *a, **kw: None),
        _config=SimpleNamespace(provider="test", language="en"),
        _intrinsics={},
        _tool_handlers={},
        _PARALLEL_SAFE_TOOLS=set(),
        _working_dir=tmp_path,
        _cancel_event=threading.Event(),
        _session=SimpleNamespace(send=lambda message: response),
        _save_chat_history=lambda *a, **kw: None,
        _log=lambda event, **fields: logs.append((event, fields)),
    )

    with pytest.raises(turn.EmptyLLMResponseError):
        turn._handle_tc_wake(agent, _make_message(MSG_TC_WAKE, "system", ""))

    event, fields = logs[-1]
    assert event == "tc_wake_error"
    assert fields["ledger_source"] == "tc_wake"
    assert fields["in_tool_loop"] is False
    assert fields["response_id"] == "resp_tc"
    assert fields["response_model"] == "gpt-test"
    assert fields["finish_reason"] == "stop"
    assert fields["api_call_id"] == "api_tc"


# ---------------------------------------------------------------------------


# Issue #655: post-turn _save_chat_history / _run_inquiry must never kill the
# run loop (they sit outside the AED try/except; an uncaught exception there
# would propagate out of _run_loop and silently kill the daemon thread).
# ---------------------------------------------------------------------------


def _run_loop_with_post_turn_error(tmp_path, monkeypatch, *, save_error=None,
                                   inquiry_error=None):
    """Drive one full message turn through _run_loop with an optional
    post-turn save/inquiry failure, then a second turn that succeeds and
    sets _shutdown so the loop exits."""
    agent = _make_run_loop_agent(tmp_path)
    agent.saves = 0
    agent.handle_calls = 0

    def fake_handle(_agent, _msg):
        _agent.handle_calls += 1
        if _agent.handle_calls == 1:
            # Queue the second turn from inside the first _handle_message
            # (a pre-queued MSG_REQUEST would be merged by
            # _concat_queued_messages instead of becoming its own turn).
            _agent.inbox.put(_make_message(MSG_REQUEST, "human", "second"))
        else:
            _agent._shutdown.set()

    def fake_save(*a, **kw):
        agent.saves += 1
        if save_error is not None and agent.saves == 1:
            raise save_error

    def fake_inquiry(*a, **kw):
        if inquiry_error is not None:
            raise inquiry_error

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    agent._save_chat_history = fake_save
    if inquiry_error is not None:
        agent._config.insights_interval = 1
        agent._insight_turn_counter = 0
        agent._run_inquiry = fake_inquiry

    import lingtai.tools.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)
    return agent


def test_run_loop_save_error_logged_not_propagated(tmp_path, monkeypatch):
    """An OSError raised by post-turn _save_chat_history is logged as
    post_turn_error and must NOT propagate out of _run_loop (which would
    silently kill the daemon run-loop thread)."""
    agent = _run_loop_with_post_turn_error(
        tmp_path, monkeypatch, save_error=OSError("disk full")
    )

    assert agent.handle_calls == 2  # second message still processed
    assert agent.saves == 2  # save retried on the next turn
    assert any(name == "post_turn_error" for name, _ in agent._logs)
    error_log = next(
        (fields for name, fields in agent._logs if name == "post_turn_error"),
        None,
    )
    assert error_log is not None
    assert error_log["exception"] == "OSError"
    assert "disk full" in error_log["error"]


def test_run_loop_inquiry_error_logged_not_propagated(tmp_path, monkeypatch):
    """An exception raised by post-turn _run_inquiry (auto-insight) is logged
    as post_turn_error and must NOT propagate out of _run_loop either."""
    agent = _run_loop_with_post_turn_error(
        tmp_path, monkeypatch, inquiry_error=RuntimeError("provider down")
    )

    assert agent.handle_calls == 2
    assert any(name == "post_turn_error" for name, _ in agent._logs)
    error_log = next(
        (fields for name, fields in agent._logs if name == "post_turn_error"),
        None,
    )
    assert error_log is not None
    assert error_log["exception"] == "RuntimeError"
    assert "provider down" in error_log["error"]
