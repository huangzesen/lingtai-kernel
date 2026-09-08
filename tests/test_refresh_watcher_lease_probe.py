"""Refresh watcher after a poisoned hard exit (Runyuan, 2026-09-08).

The dead owner left the `.agent.lock` pathname (OS lease released by process
death) and a heartbeat only seconds old; the old watcher trusted the pathname,
timed out `phase=lock`, and left `.refresh.taken`, so the TUI showed
`Refreshing` for hours. Pinned: lease truth over pathname truth, heartbeat
advancement over heartbeat age, marker settlement at every terminal outcome,
and truthful exit status with once-only failure reporting.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from lingtai.kernel.refresh_watcher import (
    RefreshWatcherProcessHandle,
    RefreshWatcherProcessObservation,
    RefreshWatcherProcessPort,
    RefreshWatcherRequest,
    encode_request,
)
from lingtai.kernel.refresh_watcher import watcher_program
from lingtai.kernel.refresh_watcher.watcher_program import render_watcher_script

# Read defensively so the suite collects (and fails) against the exact base.
WATCHER_HANDLED_ATTR = getattr(watcher_program, "WATCHER_HANDLED_ATTR", "_lingtai_watcher_handled")
from tests._workdir_lease_helpers import FakeWorkdirLease, make_test_lease

ENTRYPOINTS = [
    "lingtai.adapters.posix.refresh_watcher_entrypoint",
    "lingtai.adapters.windows.refresh_watcher_entrypoint",
]


def _wd(tmp_path, slug, *, stale_lock=False, heartbeat_age=None, taken=True):
    wd = tmp_path / slug
    (wd / "logs").mkdir(parents=True)
    if taken:
        (wd / ".refresh.taken").touch()
    if stale_lock:
        (wd / ".agent.lock").touch()  # pathname only: no holder, no OS lock
    if heartbeat_age is not None:
        (wd / ".agent.heartbeat").write_text(str(time.time() - heartbeat_age))
    return wd


def _request(wd: Path) -> RefreshWatcherRequest:
    return RefreshWatcherRequest(
        taken_path=str(wd / ".refresh.taken"), lock_path=str(wd / ".agent.lock"),
        events_path=str(wd / "logs" / "events.jsonl"),
        stderr_log=str(wd / "logs" / "refresh_relaunch.log"), working_dir=str(wd),
        cmd=(sys.executable, "-m", "lingtai", "run", str(wd)), agent_name="alice", address=str(wd),
    )


def _script(wd, *, max_attempts="2", deadline="1", observe="0.05"):
    return (
        render_watcher_script(_request(wd))
        .replace("MAX_ATTEMPTS = 12", f"MAX_ATTEMPTS = {max_attempts}")
        .replace("HEALTH_CHECK_WAIT = 10", "HEALTH_CHECK_WAIT = 0.01")
        .replace("HEALTH_CHECK_BUDGET = 60", "HEALTH_CHECK_BUDGET = 0.3")
        .replace("WATCHER_POLL_INTERVAL = 0.5", "WATCHER_POLL_INTERVAL = 0.005")
        .replace("DUPLICATE_EXIT_WAIT = 15", "DUPLICATE_EXIT_WAIT = 0.05")
        .replace("ALREADY_ALIVE_OBSERVE = 3.0", f"ALREADY_ALIVE_OBSERVE = {observe}")
        .replace("deadline = time.time() + 60", f"deadline = time.time() + {deadline}")
        .replace("deadline = time.time() + 5", "deadline = time.time() + 0.02")
        .replace("time.sleep(0.5)", "time.sleep(0.01)")
        .replace("time.sleep(0.2)", "time.sleep(0.005)")
    )


def _events(wd):
    path = wd / "logs" / "events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def _types(wd):
    return [e["type"] for e in _events(wd)]


def _run(wd, mechanism, lease=None, **kw):
    """Exec the policy; return the exit code (None = fell through)."""
    ns = {"PROCESS_MECHANISM": mechanism}
    if lease is not None:
        ns["WORKDIR_LEASE"] = lease
    try:
        exec(compile(_script(wd, **kw), "<refresh-policy>", "exec"), ns)
    except SystemExit as e:
        return e.code
    return None


class Healthy(RefreshWatcherProcessPort):
    """Boots at once; optionally consumes the marker like `cli.run` does."""

    def __init__(self, wd, *, consume_marker=False):
        self.wd, self.consume_marker, self.launches = wd, consume_marker, 0

    def observe(self, pid):
        return None

    def is_alive(self, process):
        return False

    def start_agent(self, cmd, stderr_log):
        self.launches += 1
        if self.consume_marker:
            (self.wd / ".refresh.taken").unlink(missing_ok=True)
        (self.wd / ".agent.heartbeat").write_text(str(time.time()))
        return RefreshWatcherProcessHandle(pid=9500 + self.launches)

    def graceful_stop(self, process):
        raise AssertionError("no termination expected")

    force_stop = graceful_stop


class NeverBoots(Healthy):
    """Every relaunch dies without a heartbeat; the child PID keeps matching."""

    def observe(self, pid):
        return RefreshWatcherProcessObservation(pid=pid, command_line=f"{sys.executable} -m lingtai run {self.wd}")

    def is_alive(self, process):
        return True

    def start_agent(self, cmd, stderr_log):
        self.launches += 1
        return RefreshWatcherProcessHandle(pid=9600 + self.launches)


class DuplicateThenHealthy(Healthy):
    """Attempt 1 is refused by a stale same-agent duplicate; attempt 2 boots."""

    def __init__(self, wd):
        super().__init__(wd)
        self.calls, self.dup_alive = [], True
        self.obs = RefreshWatcherProcessObservation(pid=4242, command_line=f"{sys.executable} -m lingtai run {wd}")

    def observe(self, pid):
        return self.obs if pid == 4242 else None

    def is_alive(self, process):
        return process.pid == 4242 and self.dup_alive

    def start_agent(self, cmd, stderr_log):
        self.launches += 1
        if self.launches == 1:
            with open(stderr_log, "a") as f:
                f.write("another lingtai agent is already running\nPID 4242: stale duplicate\n")
        else:
            (self.wd / ".agent.heartbeat").write_text(str(time.time()))
        return RefreshWatcherProcessHandle(pid=9700 + self.launches)

    def graceful_stop(self, process):
        self.calls.append(("graceful_stop", process.pid))
        self.dup_alive = False

    force_stop = graceful_stop


class Ticker:
    """A live owner this watcher did not launch: advances the heartbeat."""

    def __init__(self, wd):
        self.path, self.stop = wd / ".agent.heartbeat", threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            self.path.write_text(str(time.time()))
            time.sleep(0.01)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(1)


class ReleaseRaises(FakeWorkdirLease):
    def __init__(self, key, exc):
        super().__init__(key)
        self.exc = exc

    def release(self):
        super().release()
        raise self.exc


# --- lease truth -------------------------------------------------------------


def test_stale_lock_pathname_with_free_lease_relaunches(tmp_path):
    wd = _wd(tmp_path, "stale", stale_lock=True)
    m = Healthy(wd)
    assert _run(wd, m, make_test_lease(wd)) == 0
    assert m.launches == 1
    assert "refresh_watcher_timeout" not in _types(wd)
    assert [e["via"] for e in _events(wd) if e["type"] == "refresh_watcher_lock_released"] == ["lease_probe"]


def test_real_posix_lease_bare_pathname_and_held_flock(tmp_path):
    from lingtai.adapters.posix.workdir_lease import PosixWorkdirLeaseAdapter

    wd = _wd(tmp_path, "posix", stale_lock=True)
    assert _run(wd, Healthy(wd), PosixWorkdirLeaseAdapter(wd)) == 0
    assert "refresh_watcher_timeout" not in _types(wd)

    wd = _wd(tmp_path, "posix-held", stale_lock=True)
    holder = PosixWorkdirLeaseAdapter(wd)
    holder.acquire(0)
    release_at = time.time() + 0.2
    threading.Timer(0.2, holder.release).start()
    launched = []
    m = Healthy(wd)
    start = m.start_agent
    m.start_agent = lambda cmd, log: (launched.append(time.time()), start(cmd, log))[1]
    assert _run(wd, m, PosixWorkdirLeaseAdapter(wd), deadline="3") == 0
    assert launched[0] >= release_at - 0.05, "the relaunch waits for the OS lease"


def test_held_lease_times_out_and_settles_marker(tmp_path):
    wd = _wd(tmp_path, "held", stale_lock=True)
    holder = make_test_lease(wd)
    holder.acquire(0)
    m = Healthy(wd)
    assert _run(wd, m, make_test_lease(wd), deadline="0.2") == 1
    assert m.launches == 0
    assert [e["phase"] for e in _events(wd) if e["type"] == "refresh_watcher_timeout"] == ["lock"]
    assert not (wd / ".refresh.taken").exists()
    assert [e["reason"] for e in _events(wd) if e["type"] == "refresh_taken_marker_cleared"] == ["lock_timeout"]
    holder.release()


def test_lock_path_without_injected_lease_fails_loudly_and_settles(tmp_path):
    wd = _wd(tmp_path, "no-lease", stale_lock=True)
    with pytest.raises(RuntimeError, match="workdir lease"):
        _run(wd, Healthy(wd))
    assert not (wd / ".refresh.taken").exists()


# --- heartbeat truth ---------------------------------------------------------


def test_young_dead_heartbeat_cannot_suppress_relaunch(tmp_path):
    """The recurrence: free lease, lingering lock path, heartbeat 5 s old."""
    wd = _wd(tmp_path, "young", stale_lock=True, heartbeat_age=5.0)
    old_ts = float((wd / ".agent.heartbeat").read_text())
    m = Healthy(wd)
    assert _run(wd, m, make_test_lease(wd)) == 0
    assert m.launches == 1
    assert "refresh_watcher_already_alive" not in _types(wd)
    assert "refresh_watcher_success" in _types(wd)
    assert float((wd / ".agent.heartbeat").read_text()) > old_ts
    assert not (wd / ".refresh.taken").exists()


def test_pre_refresh_heartbeat_is_never_the_child_success(tmp_path):
    wd = _wd(tmp_path, "no-advance", stale_lock=True, heartbeat_age=2.0)
    m = NeverBoots(wd)
    assert _run(wd, m, make_test_lease(wd)) == 1
    assert m.launches == 2
    assert "refresh_watcher_success" not in _types(wd)


def test_advancing_heartbeat_is_already_alive_and_settles_marker(tmp_path):
    wd = _wd(tmp_path, "live", heartbeat_age=0.0)
    m = Healthy(wd)
    with Ticker(wd):
        assert _run(wd, m, make_test_lease(wd), observe="1") == 0
    assert m.launches == 0
    assert "refresh_watcher_already_alive" in _types(wd)
    assert not (wd / ".refresh.taken").exists()


def test_young_dead_heartbeat_does_not_starve_duplicate_cleanup(tmp_path):
    wd = _wd(tmp_path, "dup", stale_lock=True, heartbeat_age=3.0)
    m = DuplicateThenHealthy(wd)
    assert _run(wd, m, make_test_lease(wd)) == 0
    assert m.launches == 2
    assert ("graceful_stop", 4242) in m.calls
    assert "refresh_watcher_duplicate_alive" not in _types(wd)


# --- terminal outcomes settle the marker with truthful status ---------------


def test_ack_timeout_exits_one_without_marker(tmp_path):
    wd = _wd(tmp_path, "ack", taken=False)
    assert _run(wd, Healthy(wd), make_test_lease(wd), deadline="0.1") == 1
    assert [e["phase"] for e in _events(wd) if e["type"] == "refresh_watcher_timeout"] == ["ack"]


def test_permanent_failure_exits_one_and_clears_marker_even_with_matching_child(tmp_path):
    wd = _wd(tmp_path, "permanent")
    assert _run(wd, NeverBoots(wd), make_test_lease(wd)) == 1
    assert (wd / "logs" / "refresh_failed_permanent.json").exists()
    assert not (wd / ".refresh.taken").exists()
    types = _types(wd)
    assert types.index("refresh_failed_permanent_alert_published") < types.index(
        "refresh_taken_marker_cleared") < types.index("refresh_failed_permanent")
    assert types[-1] == "refresh_failed_permanent" and "refresh_watcher_exception" not in types


def test_success_settles_only_an_unconsumed_marker(tmp_path):
    wd = _wd(tmp_path, "unconsumed")
    assert _run(wd, Healthy(wd), make_test_lease(wd)) == 0
    assert not (wd / ".refresh.taken").exists()
    assert [e["reason"] for e in _events(wd) if e["type"] == "refresh_taken_marker_cleared"] == ["relaunch_success"]
    wd = _wd(tmp_path, "consumed")
    assert _run(wd, Healthy(wd, consume_marker=True), make_test_lease(wd)) == 0
    assert "refresh_taken_marker_cleared" not in _types(wd)


def test_unexpected_exception_settles_marker_once_and_is_tagged(tmp_path):
    wd = _wd(tmp_path, "exc", stale_lock=True)
    with pytest.raises(OSError, match="boom") as info:
        _run(wd, Healthy(wd), ReleaseRaises(wd, OSError("boom")))
    assert getattr(info.value, WATCHER_HANDLED_ATTR)
    assert not (wd / ".refresh.taken").exists()
    crashed = [e for e in _events(wd) if e["type"] == "refresh_watcher_exception"]
    assert len(crashed) == 1 and crashed[0]["phase"] == "policy" and crashed[0]["exception"] == "OSError"


@pytest.mark.parametrize("code, expected", [(17, 17), (0, 1), (None, 1)], ids=["17", "zero", "none"])
def test_unexpected_system_exit_from_mechanism_is_terminal_failure(tmp_path, code, expected):
    """Reviewer reproducer: `WORKDIR_LEASE.release()` raising SystemExit."""
    wd = _wd(tmp_path, f"exit-{code}", stale_lock=True)
    with pytest.raises(SystemExit) as info:
        exec(compile(_script(wd), "<refresh-policy>", "exec"),
             {"PROCESS_MECHANISM": Healthy(wd), "WORKDIR_LEASE": ReleaseRaises(wd, SystemExit(code))})
    assert info.value.code == expected and getattr(info.value, WATCHER_HANDLED_ATTR)
    assert not (wd / ".refresh.taken").exists()
    crashed = [e for e in _events(wd) if e["type"] == "refresh_watcher_exception"]
    assert len(crashed) == 1 and crashed[0]["exit_code"] == repr(code)


# --- entrypoints -------------------------------------------------------------


def _main(module, wd, script):
    entrypoint = importlib.import_module(module)
    with patch.object(entrypoint, "render_watcher_script", lambda _r: script):
        return entrypoint.main([encode_request(_request(wd))])


@pytest.mark.parametrize("module", ENTRYPOINTS)
def test_entrypoint_settles_unhandled_failures_once_with_truthful_status(tmp_path, module):
    # Render/setup failure: one entrypoint event, marker cleared, exception preserved.
    wd = _wd(tmp_path, "entry-exc")
    entrypoint = importlib.import_module(module)
    with patch.object(entrypoint, "render_watcher_script", lambda _r: (_ for _ in ()).throw(RuntimeError("render"))):
        with pytest.raises(RuntimeError, match="render"):
            entrypoint.main([encode_request(_request(wd))])
    assert not (wd / ".refresh.taken").exists()
    assert [e["phase"] for e in _events(wd) if e["type"] == "refresh_watcher_exception"] == ["entrypoint"]
    # Untagged SystemExit before the policy handler: 17 preserved; 0/None -> 1.
    for code, expected in ((17, 17), (0, 1), (None, 1)):
        wd = _wd(tmp_path, f"entry-exit-{code}")
        with pytest.raises(SystemExit) as info:
            _main(module, wd, f"import sys\nsys.exit({code!r})\n")
        assert info.value.code == expected, "an unexpected terminal failure never reports success"
        assert not (wd / ".refresh.taken").exists()
        assert len([e for e in _events(wd) if e["type"] == "refresh_watcher_exception"]) == 1
    # A failure the policy already handled passes through untouched.
    wd = _wd(tmp_path, "entry-handled")
    with pytest.raises(RuntimeError, match="handled"):
        _main(module, wd, f"e = RuntimeError('handled')\nsetattr(e, {WATCHER_HANDLED_ATTR!r}, True)\nraise e\n")
    assert (wd / ".refresh.taken").exists() and not _events(wd)


def test_decode_failure_creates_no_cleanup_authority(tmp_path):
    import lingtai.adapters.posix.refresh_watcher_entrypoint as entrypoint

    wd = _wd(tmp_path, "decode")
    with pytest.raises(ValueError):
        entrypoint.main(["{not json"])
    assert (wd / ".refresh.taken").exists() and not _events(wd)


@pytest.mark.parametrize("module, adapter", [
    (ENTRYPOINTS[0], "lingtai.adapters.posix.workdir_lease.PosixWorkdirLeaseAdapter"),
    (ENTRYPOINTS[1], "lingtai.adapters.windows.workdir_lease.WindowsWorkdirLeaseAdapter"),
])
def test_entrypoint_composes_platform_lease_bound_to_working_dir(tmp_path, module, adapter):
    mod_name, cls_name = adapter.rsplit(".", 1)
    cls = getattr(importlib.import_module(mod_name), cls_name)
    captured = {}
    real_exec = exec
    with patch("builtins.exec", lambda code, ns: (real_exec(code, ns), captured.update(ns))):
        assert _main(module, tmp_path, "L = WORKDIR_LEASE") == 0
    assert isinstance(captured["L"], cls) and captured["L"]._layout.agent_lock == tmp_path / ".agent.lock"


@pytest.mark.skipif(os.name != "nt", reason="native Windows workdir-lease mechanism")
def test_real_windows_lease_probe(tmp_path):
    from lingtai.adapters.windows.workdir_lease import WindowsWorkdirLeaseAdapter

    wd = _wd(tmp_path, "win", stale_lock=True)
    assert _run(wd, Healthy(wd), WindowsWorkdirLeaseAdapter(wd)) == 0


def test_policy_names_no_adapter_and_exits_only_through_exit_helper():
    script = render_watcher_script(_request(Path("/wd")))
    assert "lingtai.adapters" not in script and "msvcrt" not in script
    assert script.count("sys.exit(") == 1
