"""WorkerStillRunning: never retry in-process; redo the interrupted turn in the
fresh process from the unfinished-turn artifact's compact ``redo`` block.

Runyuan 2026-09-08: at 300 s + grace the worker was still alive, the process
was relaunched, and (#1663) the relaunch stayed asleep, so the interrupted
call was never redone. Lifecycle pinned here:
``pending -> provider_started -> completed | abandoned`` (or born
``unavailable``); at-most-once per recorded provider start.
"""
from __future__ import annotations

import json
import queue
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lingtai.kernel.base_agent import turn, worker_recovery as wr
from lingtai.kernel.llm_utils import WorkerStillRunningError
from lingtai.kernel.message import (
    MSG_CORRELATED_TURN, MSG_REQUEST, MSG_TC_WAKE, MSG_USER_INPUT, Message, _make_message,
)
from tests.test_aed_recovery import _make_run_loop_agent

AID = "worker_still_running_20260908T113814Z_abc123"
TEXT = "[2026-09-08T04:33:00Z] original ask"


def _hang():
    return WorkerStillRunningError(elapsed=300.0, grace=5.0, agent_name="test")


def _stop_on_sleep(monkeypatch):
    import lingtai.tools.soul.flow as soul_flow

    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda a: a._shutdown.set())


def _load(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def _artifact(agent) -> dict:
    return _load(agent._working_dir / agent._llm_worker_poison_artifact)


def _run_hang(tmp_path, monkeypatch, msg, *, persisted=False):
    agent = _make_run_loop_agent(tmp_path)
    while not agent.inbox.empty():
        agent.inbox.get_nowait()
    agent.inbox.put(msg)
    calls = []

    def handle(a, m):
        calls.append(m)
        a._llm_worker_turn_request_persisted = persisted
        raise _hang()

    monkeypatch.setattr(turn, "_handle_message", handle)
    _stop_on_sleep(monkeypatch)
    turn._run_loop(agent)
    return agent, calls


def _seed(wd: Path, *, mode="request", status="pending", attempt=1, mutate=None) -> Path:
    message = {"type": MSG_REQUEST, "sender": "jason", "id": "msg_original0001"}
    if mode == "request":
        message.update(content=TEXT, content_chars=len(TEXT), content_sha256=wr._sha256(TEXT))
    elif mode == "tc_wake":
        message["type"] = MSG_TC_WAKE
    payload = {
        "schema_version": 1, "type": "worker_still_running_recovery", "status": "open",
        "created_at": "2026-09-08T11:38:14Z",
        "recovery": {"notification_ref_id": f"worker_still_running:{AID}"},
        "redo": {"status": status, "mode": mode, "attempt": attempt, "max_attempts": 3, "message": message},
        "privacy": {"redo_request_text_included": mode == "request"},
    }
    if mutate:
        mutate(payload)
    d = wd / "history" / "unfinished_turns"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{AID}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _BootAgent:
    def __init__(self, wd):
        self._working_dir, self.inbox, self.logs, self.send_calls = wd, queue.Queue(), [], []
        self._config = SimpleNamespace(language="en", max_aed_attempts=3, time_awareness=True, timezone_awareness=True)
        self._llm_worker_interface_poisoned, self._llm_worker_poison_artifact = False, None
        self._shutdown, self._asleep = threading.Event(), threading.Event()
        self._shutdown.set()
        self._state = self._venv_path = None

    start = stop = lambda self, *a, **k: None

    def send(self, content, sender=None, **k):
        self.send_calls.append((content, sender))

    def _wake_nap(self, reason):
        pass

    def _log(self, event, **fields):
        self.logs.append((event, fields))


def _boot(monkeypatch, agent, *, refresh=True):
    from lingtai import cli

    if refresh:
        (agent._working_dir / ".refresh.taken").write_text("")
    monkeypatch.setattr(cli, "_check_duplicate_process", lambda wd: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda wd: None)
    monkeypatch.setattr(cli, "load_init", lambda wd: {"venv_path": "/fake/venv"})
    monkeypatch.setattr("lingtai.venv_resolve.resolve_venv", lambda data: Path("/fake/venv"))
    monkeypatch.setattr(cli, "build_agent", lambda data, wd: agent)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda wd, a: None)
    cli.run(agent._working_dir)
    return [f.get("reason") for e, f in agent.logs if e == "refresh_kickstart_deferred"]


# --- poison time ---------------------------------------------------------------


def test_initial_send_hang_persists_exact_request_redo_and_never_retries(tmp_path, monkeypatch):
    secret = "sk-" + "a" * 40
    content = f"{TEXT} token={secret}"
    msg = _make_message(MSG_USER_INPUT, "jason", content)
    agent, calls = _run_hang(tmp_path, monkeypatch, msg)
    assert len(calls) == 1 and len(agent.refresh_calls) == 1
    artifact = _artifact(agent)
    redo = artifact["redo"]
    assert redo["status"] == "pending" and redo["mode"] == "request" and redo["attempt"] == 1
    assert redo["message"] == {"type": MSG_USER_INPUT, "sender": "jason", "id": msg.id, "content": content,
                               "content_chars": len(content), "content_sha256": wr._sha256(content)}
    assert secret not in artifact["request"]["content_preview_redacted"]
    assert artifact["privacy"]["redo_request_text_included"] is True
    path = tmp_path / agent._llm_worker_poison_artifact
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [path.name]


@pytest.mark.parametrize("msg, persisted, mode", [
    (_make_message(MSG_REQUEST, "u", "do it"), True, "continuation"),
    (_make_message(MSG_TC_WAKE, "system", ""), True, "continuation"),
    (_make_message(MSG_TC_WAKE, "system", ""), False, "tc_wake"),
], ids=["request-continuation", "tc_wake-continuation", "tc_wake-first-send"])
def test_saved_round_trip_or_tc_wake_hang_plans_without_request_copy(tmp_path, monkeypatch, msg, persisted, mode):
    agent, _ = _run_hang(tmp_path, monkeypatch, msg, persisted=persisted)
    redo = _artifact(agent)["redo"]
    assert redo["status"] == "pending" and redo["mode"] == mode
    assert "content" not in redo["message"] and redo["message"]["id"] == msg.id


@pytest.mark.parametrize("msg, reason", [
    (_make_message(MSG_CORRELATED_TURN, "acp", "ask"), "correlated_turn_settled"),
    (_make_message(MSG_REQUEST, "u", "x" * (getattr(wr, "MAX_REDO_CONTENT_CHARS", 200_000) + 1)), "request_too_large"),
    (_make_message(MSG_REQUEST, "u", "汉" * (getattr(wr, "MAX_REDO_CONTENT_BYTES", 1_000_000) // 3 + 1)), "request_too_large"),
    (_make_message(MSG_REQUEST, "u", {"not": "text"}), "non_text_request"),
], ids=["correlated", "chars", "bytes", "non_text"])
def test_unsafe_replays_are_unavailable(tmp_path, msg, reason):
    agent = _make_run_loop_agent(tmp_path)
    context = wr.build_worker_hang_context(agent, msg, _hang())
    redo = _load(tmp_path / wr.write_worker_hang_artifact(agent, _hang(), context))["redo"]
    assert redo["status"] == "unavailable" and redo["reason"] == reason and "message" not in redo


# --- boot ----------------------------------------------------------------------


def test_refresh_boot_enqueues_exact_request_instead_of_refresh_success(tmp_path, monkeypatch):
    _seed(tmp_path)
    agent = _BootAgent(tmp_path)
    assert _boot(monkeypatch, agent) == ["worker_hang_redo_enqueued"]
    assert agent.send_calls == []
    m = agent.inbox.get_nowait()
    assert (m.type, m.sender, m.content, m.id) == (MSG_REQUEST, "jason", TEXT, "msg_original0001")
    assert agent.inbox.empty() and agent._llm_worker_redo_in_flight["message_id"] == m.id


def test_tc_wake_and_continuation_redo_messages(tmp_path, monkeypatch):
    from lingtai.kernel.i18n import t

    _seed(tmp_path, mode="tc_wake")
    a = _BootAgent(tmp_path)
    _boot(monkeypatch, a)
    m = a.inbox.get_nowait()
    assert (m.type, m.id, m.content) == (MSG_TC_WAKE, "msg_original0001", "")

    _seed(tmp_path, mode="continuation")
    a = _BootAgent(tmp_path)
    _boot(monkeypatch, a, refresh=False)  # any boot enqueues a pending redo
    m = a.inbox.get_nowait()
    assert m.type == MSG_REQUEST and m.sender == "system"
    assert m.content.endswith(t("en", "system.stuck_revive", ts="X", err_desc="LLM worker still running after timeout plus grace").split("X", 1)[1])


def test_provider_started_at_boot_fails_closed_and_stays_visible(tmp_path, monkeypatch):
    path = _seed(tmp_path, status="provider_started")
    agent = _BootAgent(tmp_path)
    assert _boot(monkeypatch, agent) == ["pending_worker_hang_recovery"]
    assert agent.inbox.empty() and agent.send_calls == []
    redo = _load(path)["redo"]
    assert redo["status"] == "abandoned" and redo["reason"] == "provider_started_before_crash"
    assert "content" not in redo["message"] and _load(path)["status"] == "open"


@pytest.mark.parametrize("mutate, reason", [
    (lambda p: p["redo"]["message"].__setitem__("content", TEXT[:-1] + "!"), "invalid:content_hash"),
    (lambda p: p["redo"].__setitem__("attempt", 4), "invalid:attempt"),
    (lambda p: p["redo"]["message"].__setitem__("id", "x" * 65), "invalid:message_id"),
    (lambda p: p["redo"].__setitem__("status", "queued"), "invalid:status"),
], ids=["hash", "attempt", "id", "status"])
def test_tampered_redo_is_abandoned_not_replayed(tmp_path, monkeypatch, mutate, reason):
    path = _seed(tmp_path, mutate=mutate)
    agent = _BootAgent(tmp_path)
    _boot(monkeypatch, agent)
    assert agent.inbox.empty()
    assert _load(path)["redo"]["status"] == "abandoned" and _load(path)["redo"]["reason"] == reason
    assert TEXT not in path.read_text()


def test_discovery_failure_never_becomes_ordinary_kickstart(tmp_path, monkeypatch):
    monkeypatch.setattr(wr, "_open_artifacts", lambda a: (_ for _ in ()).throw(OSError("disk")))
    agent = _BootAgent(tmp_path)
    assert _boot(monkeypatch, agent) == ["recovery_discovery_failed"]
    assert agent.send_calls == []


def test_ordinary_refresh_and_unreplayable_recovery_keep_base_behavior(tmp_path, monkeypatch):
    from lingtai.kernel.i18n import t

    plain = _BootAgent(tmp_path / "plain")
    (tmp_path / "plain").mkdir()
    assert _boot(monkeypatch, plain) == []
    assert plain.send_calls == [(t("en", "system.refresh_successful"), "system")]
    _seed(tmp_path, mode="unavailable", status="unavailable")
    agent = _BootAgent(tmp_path)
    assert _boot(monkeypatch, agent) == ["pending_worker_hang_recovery"]
    assert agent.send_calls == [] and agent.inbox.empty()


def test_artifact_ids_are_containment_validated(tmp_path):
    agent = _BootAgent(tmp_path)
    assert wr._artifact_by_id(agent, "../../etc/passwd") is None
    d = tmp_path / "history" / "unfinished_turns"
    d.mkdir(parents=True)
    (d / "worker_still_running_rogue.json").write_text(json.dumps(
        {"type": "worker_still_running_recovery", "status": "open", "redo": {"status": "pending", "mode": "request"}}))
    assert wr.redrive_worker_hang_redo(agent) == "none"


# --- the redo turn ---------------------------------------------------------------


def _claimed(tmp_path, monkeypatch, *, attempt=1, mode="request"):
    agent = _make_run_loop_agent(tmp_path)
    while not agent.inbox.empty():
        agent.inbox.get_nowait()
    agent._config.max_aed_attempts = 3
    path = _seed(tmp_path, attempt=attempt, mode=mode)
    assert wr.redrive_worker_hang_redo(agent) == "enqueued"
    _stop_on_sleep(monkeypatch)
    return agent, path


def test_redo_turn_marks_provider_started_before_the_call_then_settles(tmp_path, monkeypatch):
    agent, path = _claimed(tmp_path, monkeypatch)
    seen = []

    def handle(a, m):
        assert wr.mark_worker_hang_redo_provider_started(a)  # what _handle_request does pre-send
        seen.append((m.content, _load(path)["redo"]["status"]))
        a._shutdown.set()
        return {"text": "ok", "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", handle)
    turn._run_loop(agent)
    assert seen == [(TEXT, "provider_started")]
    redo = _load(path)["redo"]
    assert redo["status"] == "completed" and redo["outcome"] == "completed"
    assert "content" not in redo["message"] and redo["message"]["content_sha256"] == wr._sha256(TEXT)
    assert agent._llm_worker_redo_in_flight is None


def test_redo_never_reaching_provider_settles_no_provider_call_and_mark_failure_fails_closed(tmp_path, monkeypatch):
    agent, path = _claimed(tmp_path, monkeypatch, mode="tc_wake")
    monkeypatch.setattr(turn, "_handle_message", lambda a, m: a._shutdown.set())
    turn._run_loop(agent)
    assert _load(path)["redo"]["outcome"] == "no_provider_call"

    agent, path = _claimed(tmp_path / "b", monkeypatch)
    wr.match_in_flight_worker_hang_redo(agent, agent.inbox.get_nowait())
    monkeypatch.setattr(wr, "_write_json_atomic", lambda p, d: (_ for _ in ()).throw(OSError("full")))
    assert wr.mark_worker_hang_redo_provider_started(agent) is False
    assert _load(path)["redo"]["status"] == "pending"


def test_repeat_hang_counts_attempts_and_exhausts_budget(tmp_path, monkeypatch):
    agent, path = _claimed(tmp_path, monkeypatch, attempt=2)

    def handle(a, m):
        assert wr.mark_worker_hang_redo_provider_started(a)
        raise _hang()

    monkeypatch.setattr(turn, "_handle_message", handle)
    turn._run_loop(agent)
    assert _load(path)["redo"]["outcome"] == "worker_still_running_again"
    new = _artifact(agent)["redo"]
    assert new["status"] == "pending" and new["attempt"] == 3

    agent, _ = _claimed(tmp_path / "c", monkeypatch, attempt=3)
    turn._run_loop(agent)
    assert _artifact(agent)["redo"] == {**_artifact(agent)["redo"], "status": "unavailable",
                                        "reason": "aed_redo_budget_exhausted", "attempt": 4}


def test_binding_survives_concatenation_and_ignores_unrelated_turns(tmp_path, monkeypatch):
    agent, path = _claimed(tmp_path, monkeypatch)
    agent.inbox.put(_make_message(MSG_REQUEST, "user", "and also this"))
    seen = []
    monkeypatch.setattr(turn, "_handle_message", lambda a, m: (seen.append(m), a._shutdown.set()) and None)
    turn._run_loop(agent)
    assert seen[0].id == "msg_original0001" and "and also this" in seen[0].content
    assert _load(path)["redo"]["status"] == "completed"

    agent, path = _claimed(tmp_path / "u", monkeypatch)
    agent.inbox.get_nowait()
    agent.inbox.put(_make_message(MSG_TC_WAKE, "system", ""))
    turn._run_loop(agent)
    assert _load(path)["redo"]["status"] == "pending" and agent._llm_worker_redo_in_flight


# --- durability barrier before provider I/O ---------------------------------------


def test_write_barrier_fsyncs_parent_directory_after_replace_on_posix(tmp_path, monkeypatch):
    import os

    events, real_fsync, real_replace = [], os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode))), real_fsync(fd)))
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append(("replace", None)), real_replace(a, b)))
    path = tmp_path / "history" / "unfinished_turns" / f"{AID}.json"
    wr._write_json_atomic(path, {"k": "v"})
    assert events == [("fsync", False), ("replace", None), ("fsync", True)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_windows_boundary_does_not_open_a_directory(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(os, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("directory open")))
    wr._fsync_directory(tmp_path, os_name="nt")  # portable boundary: file fsync + replace only


def test_barrier_failure_fails_the_provider_start_mark_closed(tmp_path, monkeypatch):
    agent, path = _claimed(tmp_path, monkeypatch)
    wr.match_in_flight_worker_hang_redo(agent, agent.inbox.get_nowait())
    monkeypatch.setattr(wr, "_fsync_directory", lambda d, **k: (_ for _ in ()).throw(OSError("barrier")))
    assert wr.mark_worker_hang_redo_provider_started(agent) is False
    assert [f for n, f in agent._logs if n == "worker_hang_redo_mark_failed"]
    # Whatever the directory entry shows after the failed barrier, the record
    # is never treated as durably started: a later boot never replays it.
    monkeypatch.setattr(wr, "_fsync_directory", lambda d, **k: None)
    assert wr.redrive_worker_hang_redo(_BootAgent(tmp_path)) == "none"


# --- legacy tc_inbox items: mark before the first send ----------------------------


class _LegacyItem:
    def __init__(self, source):
        self.source, self.call, self.result, self.replace_in_history = source, SimpleNamespace(id=f"call_{source}"), object(), False


class _Inbox:
    def __init__(self, items):
        self.items = list(items)

    def drain(self):
        drained, self.items = self.items, []
        return drained

    def enqueue(self, item):
        self.items.append(item)


class _Iface:
    def __init__(self):
        self.appended, self.entries = [], []

    def has_pending_tool_calls(self):
        return False

    def add_assistant_message(self, *, content):
        self.appended.append(content)


def _tc_wake_agent(tmp_path, monkeypatch, items):
    """Minimal agent for `_handle_tc_wake`'s legacy loop; send records the
    on-disk redo status and stops the loop."""
    agent = _make_run_loop_agent(tmp_path)
    while not agent.inbox.empty():
        agent.inbox.get_nowait()
    agent._tc_inbox, agent._chat, agent._appendix_ids_by_source = _Inbox(items), SimpleNamespace(interface=_Iface()), {}
    monkeypatch.setattr(turn, "_make_tool_executor", lambda a, g: None)
    monkeypatch.setattr(turn, "_restore_tool_results_after_continuation_failure", lambda *a, **k: False)
    monkeypatch.setattr(turn, "scan_and_emit_committed_facts", lambda a: None)
    return agent


def test_bound_tc_wake_with_legacy_items_marks_provider_started_before_first_send(tmp_path, monkeypatch):
    items = [_LegacyItem("a"), _LegacyItem("b")]
    agent = _tc_wake_agent(tmp_path, monkeypatch, items)
    path = _seed(tmp_path, mode="tc_wake")
    assert wr.redrive_worker_hang_redo(agent) == "enqueued"
    msg = agent.inbox.get_nowait()
    wr.match_in_flight_worker_hang_redo(agent, msg)
    sends = []
    agent._session.send = lambda payload: (sends.append(_load(path)["redo"]["status"]), (_ for _ in ()).throw(RuntimeError("stop")))[1]
    with pytest.raises(RuntimeError, match="stop"):
        turn._handle_tc_wake(agent, msg)
    assert sends == ["provider_started"], "the mark precedes the very first legacy send"
    assert agent._chat.interface.appended, "the legacy splice happened after the mark"


def test_bound_tc_wake_mark_failure_requeues_legacy_items_in_order_and_sends_nothing(tmp_path, monkeypatch):
    items = [_LegacyItem("a"), _LegacyItem("b")]
    agent = _tc_wake_agent(tmp_path, monkeypatch, items)
    _seed(tmp_path, mode="tc_wake")
    assert wr.redrive_worker_hang_redo(agent) == "enqueued"
    msg = agent.inbox.get_nowait()
    wr.match_in_flight_worker_hang_redo(agent, msg)
    monkeypatch.setattr(wr, "_fsync_directory", lambda d, **k: (_ for _ in ()).throw(OSError("barrier")))
    agent._session.send = lambda payload: (_ for _ in ()).throw(AssertionError("provider called"))
    turn._handle_tc_wake(agent, msg)
    assert agent._tc_inbox.items == items and not agent._chat.interface.appended
    assert [f for n, f in agent._logs if n == "tc_wake_noop"][-1]["reason"] == "worker_hang_redo_mark_failed"


def test_barrier_failure_after_replace_settles_no_provider_call_through_the_run_loop(tmp_path, monkeypatch):
    """The replace can expose `provider_started` on disk before the directory
    barrier fails; the run loop must still settle `no_provider_call`."""
    agent, path = _claimed(tmp_path, monkeypatch)
    barrier_calls, sends = [], []

    def failing_barrier(d, **k):
        barrier_calls.append(d)
        if len(barrier_calls) == 1:
            raise OSError("barrier")

    monkeypatch.setattr(wr, "_fsync_directory", failing_barrier)
    agent._session.send = lambda payload: sends.append(payload)

    def handle(a, m):  # what _handle_request does: mark, fail closed, no send
        if not wr.mark_worker_hang_redo_provider_started(a):
            a._shutdown.set()
            return {"text": "", "failed": True, "errors": ["worker hang redo state could not be recorded"]}
        a._session.send(m.content)
        a._shutdown.set()
        return {"text": "ok", "failed": False, "errors": []}

    monkeypatch.setattr(turn, "_handle_message", handle)
    turn._run_loop(agent)
    assert sends == [] and _load(path)["redo"]["status"] == "completed"
    assert _load(path)["redo"]["outcome"] == "no_provider_call"
    assert "content" not in _load(path)["redo"]["message"]
    assert wr.redrive_worker_hang_redo(_BootAgent(tmp_path)) == "none", "no replay follows"


def test_bound_legacy_tc_wake_mark_failure_settles_no_provider_call_through_the_run_loop(tmp_path, monkeypatch):
    items = [_LegacyItem("a"), _LegacyItem("b")]
    agent = _tc_wake_agent(tmp_path, monkeypatch, items)
    path = _seed(tmp_path, mode="tc_wake")
    assert wr.redrive_worker_hang_redo(agent) == "enqueued"
    barrier_calls = []

    def failing_barrier(d, **k):
        barrier_calls.append(d)
        if len(barrier_calls) == 1:
            raise OSError("barrier")

    monkeypatch.setattr(wr, "_fsync_directory", failing_barrier)
    agent._session.send = lambda payload: (_ for _ in ()).throw(AssertionError("provider called"))
    original_handle = turn._handle_tc_wake

    def handle(a, m):
        original_handle(a, m)
        a._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", handle)
    _stop_on_sleep(monkeypatch)
    turn._run_loop(agent)
    assert agent._tc_inbox.items == items and not agent._chat.interface.appended
    redo = _load(path)["redo"]
    assert redo["status"] == "completed" and redo["outcome"] == "no_provider_call"
    assert wr.redrive_worker_hang_redo(_BootAgent(tmp_path)) == "none"


def test_unbound_legacy_tc_wake_is_unchanged(tmp_path, monkeypatch):
    agent = _tc_wake_agent(tmp_path, monkeypatch, [_LegacyItem("a")])
    sends = []
    agent._session.send = lambda payload: (sends.append(payload), (_ for _ in ()).throw(RuntimeError("stop")))[1]
    with pytest.raises(RuntimeError, match="stop"):
        turn._handle_tc_wake(agent, _make_message(MSG_TC_WAKE, "system", ""))
    assert len(sends) == 1 and agent._chat.interface.appended


# --- dismissal ------------------------------------------------------------------


def test_dismissal_abandons_pending_redo_and_is_serialized_with_boot(tmp_path):
    path = _seed(tmp_path)
    agent = _BootAgent(tmp_path)
    assert wr.resolve_worker_hang_artifact(agent, f"worker_still_running:{AID}")
    payload = _load(path)
    assert payload["status"] == "resolved" and payload["redo"]["status"] == "abandoned"
    assert TEXT not in path.read_text()
    assert wr.redrive_worker_hang_redo(_BootAgent(tmp_path)) == "none"

    path = _seed(tmp_path, status="provider_started")
    assert wr.resolve_worker_hang_artifact(_BootAgent(tmp_path), f"worker_still_running:{AID}")
    assert _load(path)["redo"]["status"] == "provider_started", "in-flight redo is left for the run loop"

    for i in range(10):
        wd = tmp_path / f"race{i}"
        path, agent = _seed(wd), _BootAgent(wd)
        barrier, out = threading.Barrier(2), {}
        threads = [
            threading.Thread(target=lambda: (barrier.wait(), out.__setitem__("r", wr.resolve_worker_hang_artifact(agent, f"worker_still_running:{AID}")))),
            threading.Thread(target=lambda: (barrier.wait(), out.__setitem__("b", wr.redrive_worker_hang_redo(agent)))),
        ]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        status = _load(path)["redo"]["status"]
        assert out["r"] and status in ("pending", "abandoned")
        if out["b"] == "enqueued":
            # Enqueued first: a dismissal that follows abandons the record, so
            # the enqueued turn fails closed at its provider-start mark, and
            # its settlement never rewrites the terminal record.
            assert not agent.inbox.empty()
            ref = wr.match_in_flight_worker_hang_redo(agent, agent.inbox.get_nowait())
            assert wr.mark_worker_hang_redo_provider_started(agent) is (status == "pending")
            if status == "abandoned":
                assert wr.settle_worker_hang_redo(agent, ref, outcome="completed") is False
                assert _load(path)["redo"]["reason"] == "artifact_resolved"
        else:
            assert status == "abandoned" and agent.inbox.empty()


def test_dismissal_before_provider_start_is_authoritative_over_settlement(tmp_path, monkeypatch):
    """pending -> boot enqueue -> dismissal -> bind -> mark fails closed ->
    post-turn settlement: the record stays resolved/abandoned, no provider call."""
    agent, path = _claimed(tmp_path, monkeypatch)
    assert wr.resolve_worker_hang_artifact(agent, f"worker_still_running:{AID}")
    marks = []

    def handle(a, m):  # what _handle_request does before the provider send
        marks.append(wr.mark_worker_hang_redo_provider_started(a))
        a._shutdown.set()
        return {"text": "", "failed": True, "errors": ["worker hang redo state could not be recorded"]}

    monkeypatch.setattr(turn, "_handle_message", handle)
    turn._run_loop(agent)
    assert marks == [False]
    payload = _load(path)
    assert payload["status"] == "resolved"
    assert payload["redo"]["status"] == "abandoned" and payload["redo"]["reason"] == "artifact_resolved"
    assert TEXT not in path.read_text()
    settled = [f for n, f in agent._logs if n == "worker_hang_redo_settled"]
    assert settled[-1]["outcome"] == "already_abandoned" and settled[-1]["persisted"] is False
    assert agent._llm_worker_redo_in_flight is None and agent._llm_worker_redo_turn is None


def test_prompt_marking_cannot_resurrect_a_concurrent_dismissal(tmp_path, monkeypatch):
    """Deterministic interleaving: the notice mark has read the open/pending
    payload and is about to write when a dismissal arrives. Under the lock the
    dismissal waits and then wins; without it the stale write would resurrect
    the open artifact and its replay text."""
    path, agent = _seed(tmp_path), _BootAgent(tmp_path)
    real_write, fired = wr._write_json_atomic, []
    dismissal = threading.Thread(
        target=lambda: wr.resolve_worker_hang_artifact(agent, f"worker_still_running:{AID}"))

    def racing_write(p, payload):
        if payload.get("prompt_injected_at") and not fired:
            fired.append(True)
            dismissal.start()
            dismissal.join(0.3)  # blocked by the lock (fix) or completes inside the window (bug)
        real_write(p, payload)

    monkeypatch.setattr(wr, "_write_json_atomic", racing_write)
    text = wr.maybe_prepend_worker_hang_recovery_prompt(agent, "ask")
    dismissal.join(5)
    assert fired and text.startswith("[Kernel recovery notice]") and not dismissal.is_alive()
    payload = _load(path)
    assert payload["status"] == "resolved" and payload["prompt_injected_at"]
    assert payload["redo"]["status"] == "abandoned" and payload["redo"]["reason"] == "artifact_resolved"
    assert TEXT not in path.read_text()
