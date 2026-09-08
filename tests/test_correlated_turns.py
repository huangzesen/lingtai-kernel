"""Settlement tests for the protocol-neutral correlated BaseAgent turn API."""
from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from lingtai.kernel import message as message_module
from lingtai.kernel.base_agent import turn
from lingtai.kernel.execution_workspace import (
    ExecutionWorkspace,
    current_execution_workspace,
)
from lingtai.kernel.message import (
    MESSAGE_TYPES,
    MSG_CORRELATED_TURN,
    MSG_REQUEST,
    MSG_TC_WAKE,
    MSG_USER_INPUT,
    _make_message,
)
from lingtai.kernel.state import AgentState
from lingtai.kernel.turns import (
    TurnOrigin,
    TurnOutcome,
    begin_turn,
    cancel_all_turns,
    control_from_message,
    correlated_message_text,
    settle_turn,
    submit_turn,
)
from lingtai.adapters.acp.puffo_v0 import RUNTIME_POLICY
from lingtai.kernel.turn_events import current_turn_tool_observer
from lingtai.kernel.turn_permissions import current_turn_permission_broker
from lingtai.kernel.provider_admission import current_provider_admission


def test_canonical_message_type_inventory_is_complete():
    """Adding a ``MSG_*`` inbox kind must update the closed admission surface."""

    declared_message_types = {
        value
        for name, value in vars(message_module).items()
        if name.startswith("MSG_") and isinstance(value, str)
    }

    assert message_module.MESSAGE_TYPES == frozenset(declared_message_types)


class _Interface:
    def has_pending_tool_calls(self):
        return False

    def close_pending_tool_calls(self, **_kwargs):
        return None


class _LoopAgent:
    def __init__(self, working_dir):
        self._working_dir = working_dir
        self.agent_name = "turn-test"
        self._state = AgentState.IDLE
        self._asleep = threading.Event()
        self._shutdown = threading.Event()
        self._cancel_event = threading.Event()
        self._inbox_timeout = 0.01
        self.inbox = queue.Queue()
        self._chat = None
        self._session = SimpleNamespace(
            chat=SimpleNamespace(interface=_Interface()),
            _rebuild_session=lambda _interface: None,
        )
        self._config = SimpleNamespace(
            insights_interval=0,
            max_aed_attempts=10,
            language="en",
            time_awareness=True,
            timezone_awareness=True,
        )
        self._notification_store = SimpleNamespace(snapshot=lambda _predicate: {})
        self._notification_fp = ()
        self._preset_fallback_attempted = False
        self._task_card_manager = None
        self._llm_worker_interface_poisoned = False
        self._insight_turn_counter = 0
        self.logs = []

    def _log(self, name, **fields):
        self.logs.append((name, fields))

    def _set_state(self, state, reason=""):
        self._state = state

    def _cancel_soul_timer(self):
        return None

    def _reset_uptime(self):
        return None

    def _save_chat_history(self, *args, **kwargs):
        return None

    def _can_fallback_preset(self):
        return False

    def _request_turn_cancel(self):
        self._cancel_event.set()

    def _wake_nap(self, _reason):
        return None


def _agent(tmp_path):
    return _LoopAgent(tmp_path)


def _stop_loop(agent, worker):
    agent._shutdown.set()
    agent.inbox.put(_make_message(MSG_TC_WAKE, "system", ""))
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_correlated_turn_settles_normal_with_collected_text(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    handle = submit_turn(agent, "hello", correlation_id="turn-normal")

    def fake_handle(current, msg):
        assert msg.content == "hello"
        current._shutdown.set()
        return {"text": "answer", "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    turn._run_loop(agent)

    assert handle.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert handle.result().text == "answer"


def test_consecutive_correlated_turns_reset_execution_workspace(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = submit_turn(
        agent,
        "workspace",
        correlation_id="turn-workspace",
        execution_workspace=ExecutionWorkspace(workspace),
    )
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def fake_handle(current, msg):
        scope = current_execution_workspace()
        seen.append((msg.content, None if scope is None else scope.root))
        if msg.content == "workspace":
            entered.set()
            assert release.wait(timeout=5)
        else:
            current._shutdown.set()
        return {"text": msg.content, "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    worker = threading.Thread(target=turn._run_loop, args=(agent,))
    worker.start()
    assert entered.wait(timeout=5)
    second = submit_turn(agent, "unscoped", correlation_id="turn-unscoped")
    release.set()

    assert first.result(timeout=5).outcome is TurnOutcome.NORMAL
    assert second.result(timeout=5).outcome is TurnOutcome.NORMAL
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert seen == [
        ("workspace", workspace.resolve()),
        ("unscoped", None),
    ]


def test_consecutive_correlated_turns_reset_tool_observer(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    observer = SimpleNamespace(on_tool_lifecycle=lambda _event: None)
    first = submit_turn(
        agent,
        "observed",
        correlation_id="turn-observed",
        tool_observer=observer,
    )
    second = submit_turn(agent, "plain", correlation_id="turn-plain")
    seen = []

    def fake_handle(current, msg):
        seen.append((msg.content, current_turn_tool_observer()))
        if msg.content == "plain":
            current._shutdown.set()
        return {"text": msg.content, "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    turn._run_loop(agent)

    assert first.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert second.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert seen == [("observed", observer), ("plain", None)]
    assert current_turn_tool_observer() is None


def test_correlated_turn_binds_admission_witness_scope_on_production_path(
    tmp_path, monkeypatch,
):
    """Pin the PRODUCTION admission-witness bind/teardown on the real loop path.

    Every test in ``test_puffo_admission_witness.py`` assembles the witness
    scope by hand (``begin_admission_witness_scope(agent)``) and then drives
    ``_process_response`` directly, so none of them traverse the correlated-turn
    loop where the production bind actually lives (``turn.py`` ~1227) or its
    teardown (~1856).  Deleting either line therefore leaves that whole suite
    green while a real ACP turn silently emits zero committed facts:
    ``scan_and_emit_committed_facts`` early-returns when
    ``_puffo_admission_emitted is None`` (a fail-silent liveness bug — Puffo
    never admits).  This test drives the real ``_run_loop`` across two
    consecutive correlated turns with NO manual ``begin_admission_witness_scope``
    anywhere, so:

    * deleting the bind (~1227) makes the in-handler scope ``"UNSET"`` -> red;
    * deleting the teardown (~1856) leaves the scope open after the loop -> red;
    * hoisting the bind OUT of the loop ("bind once per session") hands turn two
      turn one's scope object -> a per-turn re-bind probe reddens it.

    Scope of the re-bind probe: it proves the bind RUNS afresh each turn (so a
    hoisted "bind once" regression reddens it); it does NOT prove the watermark's
    numeric boundary is recomputed correctly (a ``_current_last_entry_id``
    regression to a constant/stale id would keep this green).  That value-level
    non-refire property is pinned separately by
    ``test_puffo_admission_witness.py::test_iv_two_turn_watermark_no_refire``,
    which uses a real interface with growing entries (but a hand-assembled
    scope).  The two together cover both halves; neither claims the other's.
    """
    rebind_probe = "__rebind_probe__"
    agent = _agent(tmp_path)
    first = submit_turn(agent, "one", correlation_id="turn-one")
    second = submit_turn(agent, "two", correlation_id="turn-two")
    seen = []

    def fake_handle(current, msg):
        # ``begin_admission_witness_scope`` runs at ~1227, before
        # ``_handle_message`` at ~1315: on the production path the scope must be
        # OPEN and FRESH here every turn (a new empty ``emitted`` set + a
        # watermark).
        live = getattr(current, "_puffo_admission_emitted", "UNSET")
        is_scope = isinstance(live, set)
        seen.append({
            "content": msg.content,
            # Snapshot the scope's contents at entry (a copy, so stamping the
            # probe below cannot rewrite what we recorded for this turn).
            "emitted": set(live) if is_scope else live,
            "watermark": getattr(current, "_puffo_admission_watermark", "UNSET"),
            # Did this turn's scope object already carry the PRIOR turn's probe?
            # True only if turn two was handed the same object (no re-bind).
            "saw_prior_probe": (rebind_probe in live) if is_scope else False,
        })
        # Stamp the live scope so the next turn can tell a fresh object (re-bind)
        # from a reused one (bind hoisted out of the loop).
        if is_scope:
            live.add(rebind_probe)
        if msg.content == "two":
            current._shutdown.set()
        return {"text": msg.content, "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    turn._run_loop(agent)

    assert first.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert second.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert [r["content"] for r in seen] == ["one", "two"]
    for r in seen:
        # Bind ran this turn: a scope object exists (not the "UNSET" sentinel).
        # Deleting begin_admission_witness_scope(~1227) makes this "UNSET" -> red.
        assert r["emitted"] != "UNSET", f"scope not bound for {r['content']!r}"
        assert r["watermark"] != "UNSET", f"watermark not set for {r['content']!r}"
        # The scope was FRESH this turn: empty at entry and not carrying the
        # previous turn's probe.  Hoisting the bind out of the loop hands turn
        # two turn one's object -> both of these redden.
        assert r["emitted"] == set(), f"scope not fresh for {r['content']!r}: {r['emitted']!r}"
        assert not r["saw_prior_probe"], (
            f"scope reused across turns for {r['content']!r}: bind is not per-turn"
        )
    # After the loop tears the last turn down, the production teardown (~1856)
    # has closed the scope.  Deleting the teardown leaves the last turn's set in
    # place and reddens this.
    assert getattr(agent, "_puffo_admission_emitted", "UNSET") is None
    assert getattr(agent, "_puffo_admission_watermark", "UNSET") == -1


def test_consecutive_correlated_turns_reset_permission_broker(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    broker = SimpleNamespace(request_permission=lambda _request: None)
    first = submit_turn(
        agent,
        "brokered",
        correlation_id="turn-brokered",
        permission_broker=broker,
    )
    second = submit_turn(agent, "plain", correlation_id="turn-plain")
    seen = []

    def fake_handle(current, msg):
        seen.append((msg.content, current_turn_permission_broker()))
        if msg.content == "plain":
            current._shutdown.set()
        return {"text": msg.content, "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    turn._run_loop(agent)

    assert first.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert second.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert seen == [("brokered", broker), ("plain", None)]
    assert current_turn_permission_broker() is None


def test_matching_active_cancel_wins_before_terminal_settlement(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    handle = submit_turn(agent, "wait", correlation_id="turn-active")
    entered = threading.Event()
    release = threading.Event()

    def fake_handle(current, _msg):
        entered.set()
        assert release.wait(timeout=5)
        current._shutdown.set()
        return {"text": "late answer", "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    worker = threading.Thread(target=turn._run_loop, args=(agent,))
    worker.start()
    assert entered.wait(timeout=5)
    assert handle.cancel() is True
    assert agent._cancel_event.is_set()
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    result = handle.result(timeout=1)
    assert result.outcome is TurnOutcome.CANCELLED
    assert result.text == ""
    assert handle.cancel() is False


def test_pending_cancel_is_correlated_and_does_not_cancel_turn_ahead(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    first = submit_turn(agent, "first", correlation_id="turn-first")
    second = submit_turn(agent, "second", correlation_id="turn-second")
    entered = threading.Event()
    release = threading.Event()
    seen: list[str] = []

    def fake_handle(_current, msg):
        seen.append(msg.content)
        entered.set()
        assert release.wait(timeout=5)
        return {"text": "first done", "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    worker = threading.Thread(target=turn._run_loop, args=(agent,))
    worker.start()
    assert entered.wait(timeout=5)

    assert second.cancel() is True
    assert not agent._cancel_event.is_set(), "pending cancel must not affect current"
    release.set()

    assert first.result(timeout=5).outcome is TurnOutcome.NORMAL
    assert second.result(timeout=5).outcome is TurnOutcome.CANCELLED
    assert seen == ["first"], "pre-cancelled second turn must not reach provider"
    _stop_loop(agent, worker)


def test_terminal_aed_failure_settles_failed_not_hanging(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    agent._config.max_aed_attempts = 1
    handle = submit_turn(agent, "fail", correlation_id="turn-failed")

    def fake_handle(_current, _msg):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    worker = threading.Thread(target=turn._run_loop, args=(agent,))
    worker.start()

    result = handle.result(timeout=5)
    assert result.outcome is TurnOutcome.FAILED
    assert "provider unavailable" in (result.error or "")
    _stop_loop(agent, worker)


def test_run_loop_shutdown_settles_queued_handles_cancelled(tmp_path):
    agent = _agent(tmp_path)
    handle = submit_turn(agent, "queued", correlation_id="turn-queued")
    agent._shutdown.set()

    turn._run_loop(agent)

    assert handle.result(timeout=1).outcome is TurnOutcome.CANCELLED


def test_pre_begin_run_loop_failure_terminally_cancels_registered_handle(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    handle = submit_turn(agent, "dequeued", correlation_id="turn-pre-begin-crash")

    def fail_state_transition(_state, reason=""):
        raise RuntimeError("pre-begin crash")

    monkeypatch.setattr(agent, "_set_state", fail_state_transition)

    with pytest.raises(RuntimeError, match="pre-begin crash"):
        turn._run_loop(agent)

    result = handle.result(timeout=1)
    assert result.outcome is TurnOutcome.CANCELLED
    assert result.error == "agent run loop stopped"
    assert agent._turn_controls == {}


def test_post_provider_pre_settlement_failure_settles_current_handle_failed(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    handle = submit_turn(agent, "handled", correlation_id="turn-post-provider-crash")

    def fake_handle(current, _msg):
        current._shutdown.set()
        return {"text": "provider completed", "failed": False, "errors": []}

    def fail_settlement(*_args, **_kwargs):
        raise RuntimeError("post-provider settlement crash")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn, "_settle_correlated_after_turn", fail_settlement)

    with pytest.raises(RuntimeError, match="post-provider settlement crash"):
        turn._run_loop(agent)

    result = handle.result(timeout=1)
    assert result.outcome is TurnOutcome.FAILED
    assert "post-provider settlement crash" in (result.error or "")
    assert agent._turn_controls == {}


def test_submit_rechecks_shutdown_at_registration_boundary(tmp_path):
    agent = _agent(tmp_path)

    class _ShutdownRace:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 2

    # Model stop winning after submit's fast precheck but before registration.
    agent._shutdown = _ShutdownRace()

    with pytest.raises(RuntimeError, match="agent is stopping"):
        submit_turn(agent, "too late", correlation_id="turn-after-stop")

    assert agent._turn_controls == {}
    assert agent.inbox.empty()


def test_submit_rejects_empty_id_and_survives_best_effort_wake_failure(tmp_path):
    agent = _agent(tmp_path)

    with pytest.raises(ValueError, match="correlation_id"):
        submit_turn(agent, "bad", correlation_id="")

    def fail_wake(_reason):
        raise RuntimeError("wake optimization failed")

    agent._wake_nap = fail_wake
    handle = submit_turn(agent, "queued", correlation_id="turn-wake-failure")
    assert agent.inbox.qsize() == 1

    agent._shutdown.set()
    turn._run_loop(agent)
    assert handle.result(timeout=1).outcome is TurnOutcome.CANCELLED


def test_claimed_correlated_envelope_is_skipped_without_provider_dispatch(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    handle = submit_turn(agent, "stale", correlation_id="turn-stale-envelope")
    assert cancel_all_turns(agent, reason="test stop race") == 1

    def must_not_dispatch(*_args, **_kwargs):
        raise AssertionError("a terminal correlated envelope reached the provider")

    monkeypatch.setattr(turn, "_handle_message", must_not_dispatch)
    worker = threading.Thread(target=turn._run_loop, args=(agent,))
    worker.start()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(name == "correlated_turn_envelope_skipped" for name, _ in agent.logs):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("run loop did not consume the terminal envelope")

    assert handle.result(timeout=1).outcome is TurnOutcome.CANCELLED
    _stop_loop(agent, worker)


@pytest.mark.parametrize(
    "message_type", sorted(MESSAGE_TYPES - {MSG_CORRELATED_TURN})
)
def test_profile_rejects_untrusted_event_before_any_provider_dispatch(
    tmp_path, monkeypatch, message_type
):
    """An inbox event cannot obtain provider execution merely by its message type."""

    agent = _agent(tmp_path)
    agent._turn_origin_policy = RUNTIME_POLICY

    def must_not_dispatch(*_args, **_kwargs):
        raise AssertionError("an untrusted event reached provider dispatch")

    monkeypatch.setattr(turn, "_handle_request", must_not_dispatch)
    monkeypatch.setattr(turn, "_handle_tc_wake", must_not_dispatch)
    result = turn._handle_message(agent, _make_message(message_type, "mail", "wake"))

    assert result is None
    assert any(name == "turn_origin_rejected" for name, _ in agent.logs)


def test_profile_binds_root_provider_admission_only_for_the_admitted_turn(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    agent._turn_origin_policy = RUNTIME_POLICY
    handle = submit_turn(
        agent,
        "hello",
        correlation_id="turn-provider-admission",
        origin=TurnOrigin.AUTHENTICATED_ADAPTER,
    )
    seen = []

    def fake_handle(current, _msg):
        parent = current_provider_admission()
        assert parent is not None
        seen.append((parent.correlation_id, parent.policy_version))
        current._shutdown.set()
        return {"text": "ok", "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    turn._run_loop(agent)

    assert handle.result(timeout=1).outcome is TurnOutcome.NORMAL
    assert seen == [("turn-provider-admission", RUNTIME_POLICY.policy_version)]
    assert current_provider_admission() is None


def test_cancel_and_terminal_settlement_linearize_exactly_once(tmp_path):
    for index in range(50):
        agent = _agent(tmp_path)
        handle = submit_turn(
            agent,
            "race",
            correlation_id=f"turn-settle-race-{index}",
        )
        message = agent.inbox.get_nowait()
        control = control_from_message(message)
        assert control is not None
        barrier = threading.Barrier(3)
        cancel_receipts: list[bool] = []
        settle_receipts: list[bool] = []

        def request_cancel():
            barrier.wait()
            cancel_receipts.append(handle.cancel())

        def complete_turn():
            barrier.wait()
            settle_receipts.append(
                settle_turn(
                    agent,
                    control,
                    outcome=TurnOutcome.NORMAL,
                    text="done",
                )
            )

        cancel_worker = threading.Thread(target=request_cancel)
        settle_worker = threading.Thread(target=complete_turn)
        cancel_worker.start()
        settle_worker.start()
        barrier.wait()
        cancel_worker.join(timeout=5)
        settle_worker.join(timeout=5)

        assert not cancel_worker.is_alive() and not settle_worker.is_alive()
        assert settle_receipts == [True]
        assert cancel_receipts in ([True], [False])
        result = handle.result(timeout=1)
        assert result.outcome in (TurnOutcome.NORMAL, TurnOutcome.CANCELLED)
        assert handle.cancel() is False
        assert agent._turn_controls == {}


def test_worker_hang_context_treats_correlated_text_as_a_request(tmp_path):
    from lingtai.kernel.base_agent.worker_recovery import build_worker_hang_context

    agent = _agent(tmp_path)
    submit_turn(agent, "safe preview", correlation_id="turn-worker-context")
    message = agent.inbox.get_nowait()
    assert begin_turn(agent, message) is not None
    message = correlated_message_text(message)

    context = build_worker_hang_context(agent, message, RuntimeError("hung"))

    assert context["turn"]["entry"] == "request"
    assert context["request"]["content_preview_redacted"] == "safe preview"
    cancel_all_turns(agent)
