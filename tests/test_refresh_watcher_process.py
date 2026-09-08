"""Behavioral evidence for the watcher-local process-mechanism Port."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from lingtai.kernel.refresh_watcher import (
    RefreshWatcherProcessHandle,
    RefreshWatcherProcessObservation,
    RefreshWatcherProcessPort,
    RefreshWatcherRequest,
)
from lingtai.kernel.refresh_watcher.watcher_program import render_watcher_script


class FakeWatcherProcess(RefreshWatcherProcessPort):
    """Small deterministic mechanism used only to exercise Core policy."""

    def __init__(
        self,
        working_dir: Path,
        *,
        force: bool,
        include_duplicate_pid: bool = True,
    ) -> None:
        self.working_dir = working_dir
        self.force = force
        self.include_duplicate_pid = include_duplicate_pid
        self.calls: list[tuple[str, int | None]] = []
        self.launches = 0
        self.duplicate_alive = True
        self.observation = RefreshWatcherProcessObservation(
            pid=4242,
            command_line=f"{sys.executable} -m lingtai run {working_dir}",
        )

    def observe(self, pid: int) -> RefreshWatcherProcessObservation | None:
        self.calls.append(("observe", pid))
        return self.observation if pid == self.observation.pid else None

    def is_alive(
        self,
        process: RefreshWatcherProcessHandle | RefreshWatcherProcessObservation,
    ) -> bool:
        self.calls.append(("is_alive", process.pid))
        return process.pid == self.observation.pid and self.duplicate_alive

    def start_agent(self, cmd, stderr_log: str) -> RefreshWatcherProcessHandle:
        self.launches += 1
        self.calls.append(("start_agent", self.launches))
        if self.launches == 1:
            with open(stderr_log, "a", encoding="utf-8") as stream:
                stream.write("another lingtai agent is already running\n")
                if self.include_duplicate_pid:
                    stream.write("PID 4242: stale duplicate\n")
        else:
            (self.working_dir / ".agent.heartbeat").write_text(
                "0", encoding="utf-8"
            )
            # The generated policy compares this timestamp with its current
            # wall clock; use the actual value after the call is entered.
            import time

            (self.working_dir / ".agent.heartbeat").write_text(
                str(time.time()), encoding="utf-8"
            )
        return RefreshWatcherProcessHandle(pid=9000 + self.launches)

    def graceful_stop(self, process) -> None:
        self.calls.append(("graceful_stop", process.pid))
        if not self.force:
            self.duplicate_alive = False

    def force_stop(self, process) -> None:
        self.calls.append(("force_stop", process.pid))
        self.duplicate_alive = False


def _prepare_working_dir(tmp_path: Path, slug: str) -> Path:
    working_dir = tmp_path / slug
    (working_dir / "logs").mkdir(parents=True)
    (working_dir / ".refresh.taken").touch()
    return working_dir


def _fast_policy_script(
    working_dir: Path,
    *,
    max_attempts: str = "2",
    health_check_wait: str = "0.01",
    health_check_budget: str = "1",
    poll_interval: str = "0.005",
    duplicate_exit_wait: str = "0.05",
) -> str:
    """Render the policy with every wall-clock constant shrunk for tests.

    The renderer keeps its timing policy in module-top constants that are
    embedded as plain assignments, so tests substitute the assignment text
    rather than reaching into the generated program's runtime state.
    """
    request = RefreshWatcherRequest(
        taken_path=str(working_dir / ".refresh.taken"),
        lock_path=str(working_dir / ".agent.lock"),
        events_path=str(working_dir / "logs" / "events.jsonl"),
        stderr_log=str(working_dir / "logs" / "refresh_relaunch.log"),
        working_dir=str(working_dir),
        cmd=(sys.executable, "-m", "lingtai", "run", str(working_dir)),
        agent_name="alice",
        address=str(working_dir),
    )
    return (
        render_watcher_script(request)
        .replace("MAX_ATTEMPTS = 12", f"MAX_ATTEMPTS = {max_attempts}")
        .replace("HEALTH_CHECK_WAIT = 10", f"HEALTH_CHECK_WAIT = {health_check_wait}")
        .replace(
            "HEALTH_CHECK_BUDGET = 60", f"HEALTH_CHECK_BUDGET = {health_check_budget}"
        )
        .replace(
            "WATCHER_POLL_INTERVAL = 0.5", f"WATCHER_POLL_INTERVAL = {poll_interval}"
        )
        .replace(
            "DUPLICATE_EXIT_WAIT = 15", f"DUPLICATE_EXIT_WAIT = {duplicate_exit_wait}"
        )
        .replace("deadline = time.time() + 60", "deadline = time.time() + 1")
        .replace("deadline = time.time() + 5", "deadline = time.time() + 0.02")
        .replace("time.sleep(0.2)", "time.sleep(0.005)")
    )


def _read_events(working_dir: Path) -> list[dict]:
    raw = (working_dir / "logs" / "events.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _run_policy(
    tmp_path: Path,
    *,
    force: bool,
    include_duplicate_pid: bool = True,
) -> FakeWatcherProcess:
    working_dir = _prepare_working_dir(tmp_path, "force" if force else "graceful")
    script = _fast_policy_script(working_dir)
    mechanism = FakeWatcherProcess(
        working_dir,
        force=force,
        include_duplicate_pid=include_duplicate_pid,
    )
    namespace = {"PROCESS_MECHANISM": mechanism}
    with pytest.raises(SystemExit) as exit_info:
        exec(compile(script, "<refresh-policy>", "exec"), namespace)
    assert exit_info.value.code == 0
    return mechanism


def test_refresh_watcher_selector_fails_loudly_on_unsupported_platform(monkeypatch):
    import lingtai.adapters.refresh_watcher as selector

    monkeypatch.setattr(selector.sys, "platform", "haiku1")
    monkeypatch.setattr(selector.os, "name", "java")
    with pytest.raises(NotImplementedError, match="refresh-watcher adapter"):
        selector.select_refresh_watcher()


def test_refresh_watcher_selector_returns_windows_adapter_on_win32(monkeypatch):
    import lingtai.adapters.refresh_watcher as selector
    from lingtai.adapters.windows.refresh_watcher import WindowsRefreshWatcherAdapter

    monkeypatch.setattr(selector.sys, "platform", "win32")
    assert isinstance(selector.select_refresh_watcher(), WindowsRefreshWatcherAdapter)


def test_core_policy_chooses_process_port_operations_without_keywords(tmp_path):
    graceful = _run_policy(tmp_path, force=False)
    assert graceful.launches == 2
    # One observation to decide the duplicate is this same agent, plus one to
    # re-check the same-agent guard after termination — the policy will not
    # start the next attempt into a duplicate that is still holding the
    # working directory.
    assert [name for name, _ in graceful.calls].count("observe") == 2
    assert ("graceful_stop", 4242) in graceful.calls
    assert not any(name == "force_stop" for name, _ in graceful.calls)
    assert ("start_agent", 1) in graceful.calls
    assert ("start_agent", 2) in graceful.calls

    forced = _run_policy(tmp_path, force=True)
    assert forced.launches == 2
    assert ("graceful_stop", 4242) in forced.calls
    assert ("force_stop", 4242) in forced.calls
    assert ("start_agent", 1) in forced.calls
    assert ("start_agent", 2) in forced.calls


def test_core_policy_does_not_observe_missing_duplicate_pid(tmp_path):
    mechanism = _run_policy(
        tmp_path,
        force=False,
        include_duplicate_pid=False,
    )

    assert mechanism.launches == 2
    assert not any(name == "observe" for name, _ in mechanism.calls)
    assert not any(name == "graceful_stop" for name, _ in mechanism.calls)
    assert not any(name == "force_stop" for name, _ in mechanism.calls)


# ---------------------------------------------------------------------------
# Slow boot vs. duplicate starvation (production incident 2026-08-19)
#
# `logs/refresh_failed_permanent.json` for the spiritual-bliss-attractor codex
# agent recorded 12 exhausted relaunch attempts with the heartbeat missing. The
# relaunch log showed each attempt either booting MCP stdio servers (imap and
# telegram slow to connect, so the first heartbeat landed well after the 10s
# health-check sleep) or colliding with the previous not-yet-dead process,
# whose SIGKILL the loop never waited on before retrying. The three fakes below
# reproduce each half of that starvation against the generated Core policy.
# ---------------------------------------------------------------------------


class SlowBootProcess(RefreshWatcherProcessPort):
    """A relaunch whose first heartbeat lands well after HEALTH_CHECK_WAIT."""

    def __init__(self, working_dir: Path, *, boot_delay: float) -> None:
        self.working_dir = working_dir
        self.boot_delay = boot_delay
        self.launches = 0
        self.timers: list[threading.Timer] = []

    def _write_heartbeat(self) -> None:
        (self.working_dir / ".agent.heartbeat").write_text(
            str(time.time()), encoding="utf-8"
        )

    def observe(self, pid):  # pragma: no cover - never reached in this scenario
        raise AssertionError("a healthy slow boot must not inspect a duplicate")

    def is_alive(self, process) -> bool:  # pragma: no cover - not reached
        raise AssertionError("a healthy slow boot must not probe liveness")

    def start_agent(self, cmd, stderr_log: str) -> RefreshWatcherProcessHandle:
        self.launches += 1
        timer = threading.Timer(self.boot_delay, self._write_heartbeat)
        timer.daemon = True
        self.timers.append(timer)
        timer.start()
        return RefreshWatcherProcessHandle(pid=9100 + self.launches)

    def graceful_stop(self, process) -> None:  # pragma: no cover - not reached
        raise AssertionError("a healthy slow boot must not be terminated")

    def force_stop(self, process) -> None:  # pragma: no cover - not reached
        raise AssertionError("a healthy slow boot must not be killed")


def test_slow_booting_relaunch_is_accepted_within_the_bounded_poll_window(tmp_path):
    """A first heartbeat arriving after HEALTH_CHECK_WAIT is still a success.

    The previous policy slept exactly HEALTH_CHECK_WAIT once and read the
    heartbeat a single time, so an agent still spawning MCP stdio servers was
    declared dead and retried until the attempts ran out. The health check now
    polls until HEALTH_CHECK_BUDGET expires, so one launch is enough.
    """
    working_dir = _prepare_working_dir(tmp_path, "slow-boot")
    script = _fast_policy_script(
        working_dir,
        health_check_wait="0.05",
        health_check_budget="5",
        poll_interval="0.01",
    )
    mechanism = SlowBootProcess(working_dir, boot_delay=0.4)

    with pytest.raises(SystemExit) as exit_info:
        exec(compile(script, "<refresh-policy>", "exec"), {"PROCESS_MECHANISM": mechanism})

    assert exit_info.value.code == 0
    assert mechanism.launches == 1, "a slow boot must not cost a second attempt"

    success = [e for e in _read_events(working_dir) if e["type"] == "refresh_watcher_success"]
    assert len(success) == 1
    # The heartbeat genuinely arrived later than the old single sleep would
    # have waited, so this is evidence of the poll and not of a lucky sample.
    assert success[0]["heartbeat_wait"] > 0.05
    assert not (working_dir / "logs" / "refresh_failed_permanent.json").exists()


class UnkillableDuplicateProcess(RefreshWatcherProcessPort):
    """Every relaunch collides with a same-agent duplicate that never dies."""

    DUPLICATE_PID = 4242

    def __init__(self, working_dir: Path) -> None:
        self.working_dir = working_dir
        self.launches = 0
        self.calls: list[tuple[str, int | None]] = []
        self.observation = RefreshWatcherProcessObservation(
            pid=self.DUPLICATE_PID,
            command_line=f"{sys.executable} -m lingtai run {working_dir}",
        )

    def observe(self, pid: int):
        self.calls.append(("observe", pid))
        return self.observation if pid == self.DUPLICATE_PID else None

    def is_alive(self, process) -> bool:
        self.calls.append(("is_alive", process.pid))
        return process.pid == self.DUPLICATE_PID

    def start_agent(self, cmd, stderr_log: str) -> RefreshWatcherProcessHandle:
        self.launches += 1
        self.calls.append(("start_agent", self.launches))
        with open(stderr_log, "a", encoding="utf-8") as stream:
            stream.write("another lingtai agent is already running\n")
            stream.write(f"PID {self.DUPLICATE_PID}: stale duplicate\n")
        return RefreshWatcherProcessHandle(pid=9200 + self.launches)

    def graceful_stop(self, process) -> None:
        self.calls.append(("graceful_stop", process.pid))

    def force_stop(self, process) -> None:
        self.calls.append(("force_stop", process.pid))


def test_undying_duplicate_records_still_alive_and_fails_permanently(tmp_path):
    """An unkillable duplicate must be reported honestly, not retried forever.

    Every attempt terminates the duplicate, waits DUPLICATE_EXIT_WAIT for it to
    release the working directory, and gives up on that wait rather than
    hanging. After MAX_ATTEMPTS the terminal artifact says exactly that.
    """
    working_dir = _prepare_working_dir(tmp_path, "undying-duplicate")
    script = _fast_policy_script(working_dir, max_attempts="3")
    mechanism = UnkillableDuplicateProcess(working_dir)

    # The loop runs to exhaustion, publishes the terminal alert, settles the
    # marker, and exits nonzero: permanent failure is never a 0 exit.
    with pytest.raises(SystemExit) as exit_info:
        exec(compile(script, "<refresh-policy>", "exec"), {"PROCESS_MECHANISM": mechanism})
    assert exit_info.value.code == 1

    assert mechanism.launches == 3
    assert ("force_stop", 4242) in mechanism.calls

    artifact = json.loads(
        (working_dir / "logs" / "refresh_failed_permanent.json").read_text(
            encoding="utf-8"
        )
    )
    metadata = artifact["metadata"]
    assert artifact["type"] == "refresh_failed_permanent"
    assert metadata["attempts"] == 3
    assert metadata["last_duplicate_pid"] == 4242
    assert metadata["last_cleanup_action"] == "terminate_stale_duplicate"
    assert metadata["last_cleanup_result"] == "still_alive"

    types = [event["type"] for event in _read_events(working_dir)]
    assert types.count("refresh_watcher_stale_duplicate_still_alive") == 3
    assert types.count("refresh_watcher_success") == 0
    assert types[-1] == "refresh_failed_permanent"


class DelayedDeathDuplicateProcess(RefreshWatcherProcessPort):
    """The duplicate only leaves the process table some time after force_stop."""

    DUPLICATE_PID = 4242

    def __init__(self, working_dir: Path, *, death_delay: float) -> None:
        self.working_dir = working_dir
        self.death_delay = death_delay
        self.launches = 0
        self.death_time: float | None = None
        self.start_times: list[float] = []
        self.observation = RefreshWatcherProcessObservation(
            pid=self.DUPLICATE_PID,
            command_line=f"{sys.executable} -m lingtai run {working_dir}",
        )

    def _dead(self) -> bool:
        return self.death_time is not None and time.time() >= self.death_time

    def observe(self, pid: int):
        return self.observation if pid == self.DUPLICATE_PID else None

    def is_alive(self, process) -> bool:
        return process.pid == self.DUPLICATE_PID and not self._dead()

    def start_agent(self, cmd, stderr_log: str) -> RefreshWatcherProcessHandle:
        self.launches += 1
        self.start_times.append(time.time())
        if self.launches == 1:
            with open(stderr_log, "a", encoding="utf-8") as stream:
                stream.write("another lingtai agent is already running\n")
                stream.write(f"PID {self.DUPLICATE_PID}: stale duplicate\n")
        else:
            (self.working_dir / ".agent.heartbeat").write_text(
                str(time.time()), encoding="utf-8"
            )
        return RefreshWatcherProcessHandle(pid=9300 + self.launches)

    def graceful_stop(self, process) -> None:
        """Requested, but this duplicate ignores the graceful stop."""

    def force_stop(self, process) -> None:
        self.death_time = time.time() + self.death_delay


def test_next_attempt_waits_for_the_killed_duplicate_to_actually_exit(tmp_path):
    """The retry must not start into a duplicate that is still holding the dir.

    `force_stop` only *requests* the exit. Starting the next relaunch before it
    completes is what reproduced 'another lingtai agent is already running' on
    attempt after attempt in the incident.
    """
    working_dir = _prepare_working_dir(tmp_path, "delayed-death")
    script = _fast_policy_script(working_dir, duplicate_exit_wait="5")
    mechanism = DelayedDeathDuplicateProcess(working_dir, death_delay=0.3)

    with pytest.raises(SystemExit) as exit_info:
        exec(compile(script, "<refresh-policy>", "exec"), {"PROCESS_MECHANISM": mechanism})

    assert exit_info.value.code == 0
    assert mechanism.launches == 2
    assert mechanism.death_time is not None
    # The second launch happened only after the duplicate was actually gone.
    assert mechanism.start_times[1] >= mechanism.death_time

    types = [event["type"] for event in _read_events(working_dir)]
    assert "refresh_watcher_stale_duplicate_killed" in types
    assert "refresh_watcher_stale_duplicate_still_alive" not in types
    assert "refresh_watcher_success" in types
