"""Regression: a stuck LLM worker (WorkerStillRunning poison) must not strand
the agent by keeping the old process alive.

When ``WorkerStillRunningError`` fires, the AED loop poisons the in-process
ChatInterface and requests a refresh/relaunch (see ``test_aed_recovery.py``).
The refresh watcher spawned by ``_perform_refresh`` relaunches a fresh process,
and ``_stop`` withdraws ``.agent.heartbeat`` / ``.agent.lock`` so the watcher
can proceed. But the wedged worker thread lives in the session's non-daemon
``_timeout_pool`` ThreadPoolExecutor. ``session.close()`` can only call
``shutdown(wait=False)`` — it cannot reclaim a thread stuck inside the LLM call.
That thread then blocks interpreter exit through ``concurrent.futures``' atexit
join, leaving a heartbeat-less, lock-free but ``ps``-visible ``lingtai run``
process. The relaunch's duplicate-process guard (``_check_duplicate_process``)
sees that lingering process and refuses to boot, so the agent is stranded as a
stale ``asleep`` marker with no working process — exactly the production
incident.

``_stop`` already reclaims daemon ThreadPoolExecutor workers / CLI process
groups for the same reason ("keep this interpreter visible in ps after
heartbeat/lock are gone, which makes refresh watchers race the
duplicate-process guard"). A wedged LLM worker is the one resource it cannot
reclaim, so the CLI process owner must hard-exit after the graceful ``stop()``
teardown when the interface was poisoned.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lingtai import cli
from lingtai.kernel.state import AgentState


class _ForceExit(Exception):
    """Sentinel raised by the patched os._exit so tests can observe it."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _FakeAgent:
    def __init__(self, poisoned: bool, working_dir: Path | None = None):
        self._llm_worker_interface_poisoned = poisoned
        self._llm_worker_poison_artifact = (
            "history/unfinished_turns/worker_still_running_x.json" if poisoned else None
        )
        self._working_dir = working_dir
        self._config = SimpleNamespace(language="en")
        self._shutdown = threading.Event()
        self._shutdown.set()  # run()'s _shutdown.wait() returns immediately
        self._asleep = threading.Event()
        self._state = None
        self._venv_path = None
        self.started = False
        self.stop_calls = 0
        self.send_calls: list[tuple[str, str | None]] = []
        self.logs: list[tuple[str, dict]] = []

    def start(self):
        self.started = True

    def stop(self, timeout: float | None = None):
        # Graceful teardown ran here in production (heartbeat unlinked, lock
        # released, watcher already spawned). The wedged worker thread is NOT
        # reclaimed — modeled by leaving the poison flag set after stop().
        self.stop_calls += 1

    def send(self, content, sender=None, **k):
        # Only the refresh-boot kick-start reaches this in `cli.run`.
        self.send_calls.append((content, sender))

    def _log(self, event_type: str, **fields):
        self.logs.append((event_type, fields))


def _write_worker_hang_artifact(
    working_dir: Path,
    artifact_id: str = "worker_still_running_20260908T094700Z_abc123",
    *,
    status: str = "open",
    resolved_at: str | None = None,
    prompt_injected_at: str | None = None,
) -> Path:
    """Mirror the bounded artifact `write_worker_hang_artifact` persists."""
    directory = working_dir / "history" / "unfinished_turns"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "type": "worker_still_running_recovery",
        "status": status,
        "created_at": "2026-09-08T09:47:00Z",
        "recovery": {"notification_ref_id": f"worker_still_running:{artifact_id}"},
    }
    if resolved_at is not None:
        payload["resolved_at"] = resolved_at
    if prompt_injected_at is not None:
        payload["prompt_injected_at"] = prompt_injected_at
    path = directory / f"{artifact_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _patch_run_dependencies(monkeypatch, tmp_path: Path, agent: _FakeAgent):
    monkeypatch.setattr(cli, "_check_duplicate_process", lambda wd: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda wd: None)
    monkeypatch.setattr(cli, "load_init", lambda wd: {"venv_path": "/fake/venv"})
    monkeypatch.setattr(
        "lingtai.venv_resolve.resolve_venv", lambda data: Path("/fake/venv")
    )
    monkeypatch.setattr(cli, "build_agent", lambda data, wd: agent)
    monkeypatch.setattr(cli, "_install_signal_handlers", lambda wd, a: None)

    exits: list[int] = []

    def _fake_exit(code):
        exits.append(code)
        raise _ForceExit(code)

    monkeypatch.setattr("os._exit", _fake_exit)
    return exits


def test_run_force_exits_when_worker_interface_poisoned(tmp_path, monkeypatch):
    """A poisoned worker at shutdown must hard-exit the process so the wedged
    thread cannot keep the old process alive and block the relaunch."""
    agent = _FakeAgent(poisoned=True)
    exits = _patch_run_dependencies(monkeypatch, tmp_path, agent)

    with pytest.raises(_ForceExit) as excinfo:
        cli.run(tmp_path)

    assert excinfo.value.code == 0
    assert exits == [0]
    assert agent.stop_calls == 1  # graceful teardown runs BEFORE the hard exit


def test_run_clean_exit_when_worker_not_poisoned(tmp_path, monkeypatch):
    """The normal (non-poisoned) shutdown path must never hard-exit — it must
    return cleanly so ordinary refresh/stop semantics are unchanged."""
    agent = _FakeAgent(poisoned=False)
    exits = _patch_run_dependencies(monkeypatch, tmp_path, agent)

    cli.run(tmp_path)  # returns normally, no _ForceExit

    assert exits == []
    assert agent.stop_calls == 1
    assert agent._state == AgentState.ASLEEP


# ---------------------------------------------------------------------------
# Refresh boot after WorkerStillRunning poison recovery must NOT self-wake.
#
# Production chronology (mimo-2-5-pro, kernel 1d642f5): 300 s timeout + 5 s
# grace -> poison -> STUCK -> ASLEEP -> forced relaunch. The relaunch exists
# only to discard the unsafe in-process interface. But `cli.run` treated the
# relaunch like any user refresh and sent `system.refresh_successful`, so the
# new process went ASLEEP -> ACTIVE ("woke from asleep: request") and started
# a fresh LLM call 250 ms after boot with no human/external request at all.
# Jason: "if llm fail at 300s, it should returned to asleep and wait for next
# wake up".
# ---------------------------------------------------------------------------


def _refresh_success_text() -> str:
    from lingtai.kernel.i18n import t

    return t("en", "system.refresh_successful")


def test_refresh_boot_with_pending_worker_recovery_stays_asleep_without_kickstart(
    tmp_path, monkeypatch
):
    """`.refresh.taken` + an open, not-yet-prompted worker recovery artifact
    means this relaunch is poison recovery: no refresh-success kick-start,
    the agent remains ASLEEP until a genuinely later wake, and the artifact
    is left untouched for `maybe_prepend_worker_hang_recovery_prompt`."""
    (tmp_path / ".refresh.taken").write_text("", encoding="utf-8")
    artifact = _write_worker_hang_artifact(tmp_path)
    before = artifact.read_text(encoding="utf-8")
    agent = _FakeAgent(poisoned=False, working_dir=tmp_path)  # fresh process
    exits = _patch_run_dependencies(monkeypatch, tmp_path, agent)

    cli.run(tmp_path)

    assert agent.started
    assert agent.send_calls == [], \
        "poison-recovery relaunch must not synthesize a refresh-success request"
    assert agent._state == AgentState.ASLEEP
    assert agent._asleep.is_set()
    assert exits == []
    assert not (tmp_path / ".refresh.taken").exists()
    assert artifact.read_text(encoding="utf-8") == before
    deferred = [f for evt, f in agent.logs if evt == "refresh_kickstart_deferred"]
    assert len(deferred) == 1
    assert deferred[0]["reason"] == "pending_worker_hang_recovery"


def test_refresh_boot_without_pending_recovery_still_kickstarts(tmp_path, monkeypatch):
    """An ordinary user/System refresh (no open worker recovery) must keep the
    exact pre-existing kick-start: one localized refresh-success request from
    the system sender."""
    (tmp_path / ".refresh.taken").write_text("", encoding="utf-8")
    agent = _FakeAgent(poisoned=False, working_dir=tmp_path)
    _patch_run_dependencies(monkeypatch, tmp_path, agent)

    cli.run(tmp_path)

    assert agent.send_calls == [(_refresh_success_text(), "system")]
    assert not any(evt == "refresh_kickstart_deferred" for evt, _ in agent.logs)


@pytest.mark.parametrize(
    "artifact_kwargs",
    [
        {"prompt_injected_at": "2026-09-08T09:50:00Z"},
        {"status": "resolved", "resolved_at": "2026-09-08T09:50:00Z"},
    ],
    ids=["already_prompted", "resolved"],
)
def test_refresh_boot_gate_applies_only_to_pending_recovery(tmp_path, monkeypatch, artifact_kwargs):
    """The discriminator is exactly the pending one-shot recovery prompt: an
    artifact whose notice was already delivered, or one already resolved, is
    history — a later refresh is an ordinary refresh and kick-starts."""
    (tmp_path / ".refresh.taken").write_text("", encoding="utf-8")
    _write_worker_hang_artifact(tmp_path, **artifact_kwargs)
    agent = _FakeAgent(poisoned=False, working_dir=tmp_path)
    _patch_run_dependencies(monkeypatch, tmp_path, agent)

    cli.run(tmp_path)

    assert agent.send_calls == [(_refresh_success_text(), "system")]


def test_non_refresh_boot_with_pending_recovery_never_sends(tmp_path, monkeypatch):
    """Without `.refresh.taken` there is no kick-start at all, pending
    recovery or not (unchanged behavior; pins the gate is refresh-scoped)."""
    _write_worker_hang_artifact(tmp_path)
    agent = _FakeAgent(poisoned=False, working_dir=tmp_path)
    _patch_run_dependencies(monkeypatch, tmp_path, agent)

    cli.run(tmp_path)

    assert agent.send_calls == []
    assert not any(evt == "refresh_kickstart_deferred" for evt, _ in agent.logs)
