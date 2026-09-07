"""Tests for async mode of the bash capability."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path

import pytest

from tests._notification_store_helpers import notification_store_for, snapshot_notifications
from lingtai.adapters.posix.bash_process import _alive as _posix_process_is_alive
from lingtai.tools.bash import BashManager, BashPolicy, get_schema
from lingtai.tools.bash import _async_supervisor
from lingtai.tools.bash._async_process import ProcessObservation, ProcessRef


class TestBashAsync:
    """Tests for async run / poll / cancel."""

    def _make_manager(self, tmp_path: Path) -> BashManager:
        agent = SimpleNamespace(_notification_store=notification_store_for(tmp_path))
        return BashManager(
            policy=BashPolicy.yolo(), working_dir=str(tmp_path), agent=agent
        )

    def test_retained_v3_state_without_dialect_fields_executes_posix_script(
        self, tmp_path
    ):
        manager = self._make_manager(tmp_path)
        job_id = "job-10000000000000000000000000000101"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = manager._initial_async_state(job_id, "printf legacy-marker", str(tmp_path), 30)
        state.pop("shell_dialect")
        state.pop("invocation")
        token = state["supervisor_start_lease"]["token"]
        _async_supervisor.write_initial_state(job_dir, state)

        assert _async_supervisor.supervise(job_dir, token) == 0
        recovered = json.loads((job_dir / "state.json").read_text())
        assert recovered["status"] == "completed"
        assert recovered["exit_code"] == 0
        assert (job_dir / "stdout.log").read_text() == "legacy-marker"

    @pytest.mark.parametrize(
        "state_update",
        [
            lambda state: state.pop("invocation"),
            lambda state: state.update({"invocation": {"script": 42}}),
            lambda state: state.update({
                "invocation": {
                    **state["invocation"], "executable": None, "argv": ["-Command"],
                },
            }),
            lambda state: state.update({
                "invocation": {**state["invocation"], "unknown": True},
            }),
            lambda state: state.update({"shell_dialect": ""}),
        ],
        ids=[
            "missing-invocation", "malformed-invocation", "argv-without-executable",
            "unknown-invocation-key", "empty-dialect",
        ],
    )
    def test_malformed_new_state_is_unrecoverable_without_raw_fallback(
        self, tmp_path, monkeypatch, state_update
    ):
        manager = self._make_manager(tmp_path)
        job_id = "job-10000000000000000000000000000102"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = manager._initial_async_state(job_id, "printf should-not-run", str(tmp_path), 30)
        state_update(state)
        token = state["supervisor_start_lease"]["token"]
        _async_supervisor.write_initial_state(job_dir, state)
        monkeypatch.setattr(
            "lingtai.adapters.shell_process.select_shell_async_process",
            lambda: pytest.fail("malformed durable state selected a process adapter"),
        )

        assert _async_supervisor.supervise(job_dir, token) == 2
        recovered = json.loads((job_dir / "state.json").read_text())
        assert recovered["status"] == "unrecoverable"
        assert recovered["exit_status_known"] is False
        assert "durable state" in recovered["supervisor_error"]

    # 1. async run returns job_id and pid
    def test_async_run_returns_job_id_and_pid(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo hello", "async": True})
        assert result["status"] == "ok"
        assert result["job_id"].startswith("job-")
        assert isinstance(result["pid"], int)
        assert "poll" in result["message"]
        # Allow process to finish and clean up
        time.sleep(0.3)

    # 2. poll returns 'running' while command is executing
    def test_poll_returns_running(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "sleep 5", "async": True})
        job_id = result["job_id"]

        poll = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll["status"] == "running"
        assert poll["job_id"] == job_id

        # Clean up
        mgr.handle({"action": "cancel", "command": "", "job_id": job_id})

    # 3. poll returns 'done' with output after command finishes
    def test_poll_returns_done_with_output(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo async-output", "async": True})
        job_id = result["job_id"]

        # Wait for the fast command to finish
        time.sleep(0.5)

        poll = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll["status"] == "done"
        assert "async-output" in poll["stdout"]
        assert "exit_code" in poll
        # Fidelity fields apply to the async poll path too.
        assert poll["ok"] is True
        assert poll["command_status"] == "success"

    # 3b. poll on a failed async job surfaces the failure explicitly
    def test_poll_nonzero_exit_is_flagged_failed(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "exit 7", "async": True})
        job_id = result["job_id"]

        time.sleep(0.5)

        poll = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll["status"] == "done"
        assert poll["exit_code"] == 7
        assert poll["ok"] is False
        assert poll["command_status"] == "failed"
        assert "exited with code 7" in poll["warning"]

    # 3c. a still-running poll has no exit_code, so no fidelity fields
    def test_poll_running_has_no_fidelity_fields(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "sleep 5", "async": True})
        job_id = result["job_id"]

        poll = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll["status"] == "running"
        assert "ok" not in poll
        assert "command_status" not in poll

        mgr.handle({"action": "cancel", "command": "", "job_id": job_id})

    # 4. cancel kills the process
    def test_cancel_kills_process(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "sleep 60", "async": True})
        job_id = result["job_id"]
        pid = result["pid"]

        cancel = mgr.handle({"action": "cancel", "command": "", "job_id": job_id})
        assert cancel["status"] == "cancelled"
        assert cancel["job_id"] == job_id

        # Verify process is dead
        time.sleep(0.2)
        import os
        with pytest.raises(OSError):
            os.kill(pid, 0)

    # 5. policy still applies to async commands
    def test_policy_applies_to_async(self, tmp_path):
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps({"deny": ["rm"]}))
        policy = BashPolicy.from_file(str(policy_file))
        agent = SimpleNamespace(_notification_store=notification_store_for(tmp_path))
        mgr = BashManager(policy=policy, working_dir=str(tmp_path), agent=agent)

        result = mgr.handle({"command": "rm -rf /", "async": True})
        assert result["status"] == "error"
        assert "not allowed" in result["message"]

    # 6. working_dir validation still applies
    def test_working_dir_validation_applies_to_async(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({
            "command": "echo hi",
            "async": True,
            "working_dir": "/etc",
        })
        assert result["status"] == "error"
        assert "working_dir" in result["message"]

    # 7. missing job_id returns error
    def test_poll_missing_job_id(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"action": "poll", "command": ""})
        assert result["status"] == "error"
        assert "job_id is required" in result["message"]

    def test_cancel_missing_job_id(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"action": "cancel", "command": ""})
        assert result["status"] == "error"
        assert "job_id is required" in result["message"]

    # 8. double-poll after completion returns error (durable record is consumed)
    def test_double_poll_after_completion(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo done", "async": True})
        job_id = result["job_id"]

        time.sleep(0.5)

        # First poll succeeds and durably marks the terminal result consumed.
        poll1 = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll1["status"] == "done"

        # The record remains for relaunch evidence, but the public result is one-shot.
        poll2 = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll2["status"] == "error"
        assert "already finished" in poll2["message"].lower()

    def test_poll_nonexistent_job(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"action": "poll", "command": "", "job_id": "job-ffffffffffffffffffffffffffffffff"})
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_cancel_nonexistent_job(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"action": "cancel", "command": "", "job_id": "job-ffffffffffffffffffffffffffffffff"})
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    # Sync path unchanged — default action='run', async=false
    def test_sync_path_unchanged(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo sync-test"})
        assert result["status"] == "ok"
        assert result["exit_code"] == 0
        assert "sync-test" in result["stdout"]

    def test_sync_ignores_reminder_field(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo sync-test", "reminder": float("nan")})
        assert result["status"] == "ok"
        assert result["exit_code"] == 0
        assert "sync-test" in result["stdout"]

    def test_async_stderr_captured(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo err >&2", "async": True})
        job_id = result["job_id"]

        time.sleep(0.5)

        poll = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll["status"] == "done"
        assert "err" in poll["stderr"]

    def test_schema_requires_reminder_with_runtime_default(self, tmp_path):
        # ``reminder`` is run-only: it exists solely in the ``run`` child's
        # input on the migrated envelope, and never on poll/cancel.
        branches = {
            b["title"]: b for b in get_schema()["properties"]["input"]["anyOf"]
        }
        run_branch = branches["run input"]
        assert run_branch["properties"]["reminder"]["default"] == 1800.0
        for action in ("poll input", "cancel input"):
            assert "reminder" not in branches[action]["properties"]

        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo compat", "async": True})
        assert result["status"] == "ok"
        assert result["handoff"] == (
            "While waiting, go idle or call system(action='sleep'); the terminal result "
            "will arrive and wake you as a notification; read shell-manual and "
            "notification-manual for details. If Telegram is connected and a Task Card "
            "is available for the current turn, use it to report progress; call "
            "`telegram(action='manual')` and follow its `Programmable Task Card` "
            "section for details."
        )
        mgr.handle({"action": "cancel", "command": "", "job_id": result["job_id"]})

    def test_reminder_deadline_starts_at_successful_async_return(self, tmp_path, monkeypatch):
        mgr = self._make_manager(tmp_path)
        real_popen = subprocess.Popen
        startup_delay = 0.2
        reminder = 0.15

        def delayed_supervisor_start(*args, **kwargs):
            time.sleep(startup_delay)
            return real_popen(*args, **kwargs)

        monkeypatch.setattr("lingtai.tools.bash.subprocess.Popen", delayed_supervisor_start)
        started = mgr.handle({"command": "sleep 5", "async": True, "reminder": reminder})
        returned_at = time.time()
        assert started["status"] == "ok"

        state = json.loads(
            (tmp_path / "system" / "jobs" / started["job_id"] / "state.json").read_text()
        )
        deadline = state["reminder"]["deadline_at"]
        # The initial persisted deadline is only the crash fallback. A successful
        # return gives the caller the requested interval after supervisor startup.
        assert deadline - state["created_at"] >= startup_delay + reminder * 0.5
        assert deadline - returned_at >= reminder * 0.5
        mgr.handle({"action": "cancel", "job_id": started["job_id"]})

    @pytest.mark.parametrize(
        "value",
        [
            -1,
            "soon",
            object(),
            True,
            float("nan"),
            float("inf"),
            -float("inf"),
            threading.TIMEOUT_MAX + 1,
        ],
    )
    def test_async_reminder_rejects_invalid_values(self, tmp_path, value):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo nope", "async": True, "reminder": value})
        assert result["status"] == "error"
        assert "reminder" in result["message"]

    def test_completed_unpolled_job_uses_completion_wake_without_stale_reminder(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo finished", "async": True, "reminder": 0.05})
        job_id = result["job_id"]

        time.sleep(0.3)

        notifications = snapshot_notifications(tmp_path)
        assert "system" not in notifications
        assert notifications["bash"]["data"]["job_id"] == job_id
        assert notifications["bash"]["data"]["exit_code"] == 0

        poll = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll["status"] == "done"

    def test_async_reminder_does_not_overwrite_close_due_jobs(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        first = mgr.handle({"command": "sleep 1", "async": True, "reminder": 0.05})
        second = mgr.handle({"command": "sleep 1", "async": True, "reminder": 0.05})
        job_ids = {first["job_id"], second["job_id"]}

        time.sleep(0.3)

        events = snapshot_notifications(tmp_path)["system"]["data"]["events"]
        assert len(events) == 2
        assert {event["ref_id"] for event in events} == {
            f"bash.reminder:{job_id}" for job_id in job_ids
        }
        assert len({event["event_id"] for event in events}) == 2

        for job_id in job_ids:
            mgr.handle({"action": "cancel", "command": "", "job_id": job_id})

    def test_terminal_poll_suppresses_async_reminder(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "echo handled", "async": True, "reminder": 0.3})
        job_id = result["job_id"]

        time.sleep(0.1)
        poll = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert poll["status"] == "done"
        time.sleep(0.3)

        assert "system" not in snapshot_notifications(tmp_path)

    def test_successful_cancel_suppresses_async_reminder(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "sleep 5", "async": True, "reminder": 0.2})
        job_id = result["job_id"]

        cancel = mgr.handle({
            "action": "cancel",
            "command": "",
            "job_id": job_id,
            "reminder": float("nan"),
        })
        assert cancel["status"] == "cancelled"
        consumed = mgr.handle({"action": "poll", "command": "", "job_id": job_id})
        assert consumed["status"] == "error"
        assert "cancelled" in consumed["message"].lower()
        time.sleep(0.3)

        assert "system" not in snapshot_notifications(tmp_path)

    def test_poll_ignores_reminder_field(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.handle({"command": "sleep 5", "async": True, "reminder": 10})
        job_id = result["job_id"]

        poll = mgr.handle({
            "action": "poll",
            "command": "",
            "job_id": job_id,
            "reminder": float("nan"),
        })
        assert poll["status"] == "running"

        mgr.handle({"action": "cancel", "command": "", "job_id": job_id})

    def test_terminal_pop_before_deadline_claim_suppresses_reminder(self, tmp_path, monkeypatch):
        mgr = self._make_manager(tmp_path)
        job_id = "job-race-terminal"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        cancel_event = threading.Event()
        with mgr._reminder_lock:
            mgr._reminder_cancel_events[job_id] = cancel_event

        published = []
        monkeypatch.setattr(mgr, "_publish_async_reminder", published.append)

        mgr._cancel_reminder_timer(job_id)
        mgr._run_reminder_timer(job_id, job_dir, 0, cancel_event)

        assert published == []
        assert job_id not in mgr._reminder_cancel_events

    def test_deadline_claim_before_terminal_pop_publishes_once(self, tmp_path, monkeypatch):
        mgr = self._make_manager(tmp_path)
        job_id = "job-race-deadline"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        cancel_event = threading.Event()
        with mgr._reminder_lock:
            mgr._reminder_cancel_events[job_id] = cancel_event

        published = []

        def fake_publish(claimed_job_id):
            published.append(claimed_job_id)
            mgr._cancel_reminder_timer(claimed_job_id)

        monkeypatch.setattr(mgr, "_publish_async_reminder", fake_publish)

        mgr._run_reminder_timer(job_id, job_dir, 0, cancel_event)

        assert published == [job_id]
        assert job_id not in mgr._reminder_cancel_events

    def test_agent_exception_fallback_uses_injected_store(self, tmp_path):
        store = notification_store_for(tmp_path)
        agent = SimpleNamespace(_notification_store=store)

        def failing_enqueue(**_kwargs):
            raise RuntimeError("boom")

        agent._enqueue_system_notification = failing_enqueue
        mgr = BashManager(
            policy=BashPolicy.yolo(), working_dir=str(tmp_path), agent=agent
        )

        mgr._publish_async_reminder("job-fallback")

        events = store.snapshot(lambda channel: channel == "system")["system"][
            "data"
        ]["events"]
        assert events[0]["ref_id"] == "bash.reminder:job-fallback"

    def test_direct_manager_fallback_is_serialized_by_shared_store(self, tmp_path):
        agent = SimpleNamespace(_notification_store=notification_store_for(tmp_path))
        first = BashManager(
            policy=BashPolicy.yolo(), working_dir=str(tmp_path), agent=agent
        )
        second = BashManager(
            policy=BashPolicy.yolo(), working_dir=str(tmp_path), agent=agent
        )
        managers = [first, second]
        n_events = 20

        def worker(i: int) -> None:
            managers[i % 2]._append_system_notification_fallback(
                source="bash.reminder",
                ref_id=f"bash.reminder:job-{i}",
                body=f"poll job-{i}",
            )

        with ThreadPoolExecutor(max_workers=n_events) as pool:
            list(pool.map(worker, range(n_events)))

        events = snapshot_notifications(tmp_path)["system"]["data"]["events"]
        assert len(events) == n_events
        assert {event["ref_id"] for event in events} == {
            f"bash.reminder:job-{i}" for i in range(n_events)
        }
        assert len({event["event_id"] for event in events}) == n_events

    def test_direct_manager_fallback_event_ids_keep_entropy_with_fixed_millisecond(
        self, tmp_path, monkeypatch
    ):
        mgr = self._make_manager(tmp_path)
        suffixes = iter(("a" * 16, "b" * 16))
        monkeypatch.setattr("time.time", lambda: 1234.567)
        monkeypatch.setattr("secrets.token_hex", lambda n: next(suffixes))

        first = mgr._append_system_notification_fallback(
            source="bash.reminder",
            ref_id="bash.reminder:job-a",
            body="poll job-a",
        )
        second = mgr._append_system_notification_fallback(
            source="bash.reminder",
            ref_id="bash.reminder:job-b",
            body="poll job-b",
        )

        assert first.startswith("evt_")
        assert second.startswith("evt_")
        assert first != second
        assert [len(event_id.rsplit("_", 1)[1]) for event_id in (first, second)] == [16, 16]


class TestBashAsyncRelaunchDurability:
    """Regression coverage for jobs surviving a fresh BashManager instance."""

    @staticmethod
    def _manager(tmp_path: Path) -> BashManager:
        return BashManager(
            policy=BashPolicy.yolo(),
            working_dir=str(tmp_path),
            agent=SimpleNamespace(_notification_store=notification_store_for(tmp_path)),
        )

    @staticmethod
    def _wait_for(predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("timed out waiting for durable async state")

    @staticmethod
    def _durable_state(
        tmp_path: Path,
        job_id: str,
        *,
        status: str = "completed",
        deadline: float | None = None,
        pid: int | None = None,
        pid_identity: str | None = None,
    ) -> Path:
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        now = time.time()
        (job_dir / "state.json").write_text(json.dumps({
            "version": 1,
            "job_id": job_id,
            "command": "echo durable",
            "cwd": str(tmp_path),
            "status": status,
            "created_at": now - 1,
            "started_at": now - 1,
            "finished_at": now if status == "completed" else None,
            "pid": pid,
            "pid_identity": pid_identity,
            "pid_start_time": pid_identity,
            "exit_status_known": status == "completed",
            "exit_code": 0 if status == "completed" else None,
            "terminal_polled": False,
            "reminder": {
                "deadline_at": deadline,
                "state": "pending",
                "ref_id": f"bash.reminder:{job_id}",
            },
            "completion": {
                "state": "pending",
                "ref_id": f"bash.completion:{job_id}",
            },
        }))
        (job_dir / "stdout.log").write_text("durable output\n")
        (job_dir / "stderr.log").write_text("")
        return job_dir

    @pytest.mark.parametrize("command, expected", [("exit 0", 0), ("exit 23", 23)])
    def test_fresh_manager_recovers_exact_supervisor_exit_code(
        self, tmp_path, command, expected
    ):
        first = self._manager(tmp_path)
        started = first.handle({"command": command, "async": True, "reminder": 30})
        job_id = started["job_id"]
        job_dir = tmp_path / "system" / "jobs" / job_id

        self._wait_for(lambda: (job_dir / "state.json").exists())
        self._wait_for(
            lambda: json.loads((job_dir / "state.json").read_text())["status"] == "completed"
        )

        # A new manager has no Popen handle and must use the supervisor's durable
        # terminal result rather than inferring a false -1 failure from a dead PID.
        relaunched = self._manager(tmp_path)
        poll = relaunched.handle({"action": "poll", "job_id": job_id})
        assert poll["status"] == "done"
        assert poll["exit_status_known"] is True
        assert poll["exit_code"] == expected
        assert poll["ok"] is (expected == 0)

    def test_supervisor_survives_actual_manager_process_exit(self, tmp_path):
        launcher = """
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from tests._notification_store_helpers import notification_store_for
from lingtai.tools.bash import BashManager, BashPolicy

root = Path(sys.argv[1])
manager = BashManager(
    policy=BashPolicy.yolo(),
    working_dir=str(root),
    agent=SimpleNamespace(_notification_store=notification_store_for(root)),
)
print(json.dumps(manager.handle({
    "command": "sleep 0.2; exit 17",
    "async": True,
    "reminder": 30,
})), flush=True)
"""
        launched = subprocess.run(
            [sys.executable, "-c", launcher, str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        started = json.loads(launched.stdout.strip())
        job_id = started["job_id"]
        job_dir = tmp_path / "system" / "jobs" / job_id
        self._wait_for(
            lambda: (job_dir / "state.json").exists()
            and json.loads((job_dir / "state.json").read_text())["status"] == "completed"
        )

        relaunched = self._manager(tmp_path)
        poll = relaunched.handle({"action": "poll", "job_id": job_id})
        assert poll["status"] == "done"
        assert poll["exit_status_known"] is True
        assert poll["exit_code"] == 17
        assert poll["ok"] is False

    def test_rehydrate_future_and_overdue_reminders_publish_once(self, tmp_path):
        future = "job-20000000000000000000000000000001"
        overdue = "job-20000000000000000000000000000002"
        self._durable_state(
            tmp_path, future, status="running", deadline=time.time() + 0.12
        )
        self._durable_state(
            tmp_path, overdue, status="running", deadline=time.time() - 1
        )

        self._manager(tmp_path)
        self._wait_for(
            lambda: len(
                snapshot_notifications(tmp_path).get("system", {}).get("data", {}).get("events", [])
            ) == 2
        )
        # A second rehydrate is a retry/relaunch, not a duplicate publication.
        self._manager(tmp_path)
        time.sleep(0.15)

        events = snapshot_notifications(tmp_path)["system"]["data"]["events"]
        assert {event["ref_id"] for event in events} == {
            f"bash.reminder:{future}", f"bash.reminder:{overdue}"
        }
        assert len(events) == 2

    def test_rehydrated_terminal_poll_and_cancel_suppress_reminders(self, tmp_path):
        first = self._manager(tmp_path)
        completed = first.handle({"command": "exit 0", "async": True, "reminder": 0.2})
        cancelled = first.handle({"command": "sleep 5", "async": True, "reminder": 0.2})
        completed_dir = tmp_path / "system" / "jobs" / completed["job_id"]

        self._wait_for(
            lambda: (completed_dir / "state.json").exists()
            and json.loads((completed_dir / "state.json").read_text())["status"] == "completed"
        )

        relaunched = self._manager(tmp_path)
        poll = relaunched.handle({"action": "poll", "job_id": completed["job_id"]})
        assert poll["status"] == "done"
        cancel = relaunched.handle({"action": "cancel", "job_id": cancelled["job_id"]})
        assert cancel["status"] == "cancelled"

        time.sleep(0.3)
        assert "system" not in snapshot_notifications(tmp_path)

    def test_legacy_dead_pid_is_explicitly_unknown_not_false_failure(self, tmp_path):
        import subprocess

        dead = subprocess.Popen(["sh", "-c", "exit 0"])
        dead.wait()
        job_id = "job-deadbeef"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "command").write_text("exit 0")
        (job_dir / "status").write_text("running")
        (job_dir / "pid").write_text(str(dead.pid))
        (job_dir / "stdout.log").write_text("")
        (job_dir / "stderr.log").write_text("")

        poll = self._manager(tmp_path).handle({"action": "poll", "job_id": job_id})
        assert poll["status"] == "done"
        assert poll["exit_status_known"] is False
        assert poll["exit_code"] is None
        assert "ok" not in poll
        assert "command_status" not in poll

    def test_legacy_live_pid_remains_running_and_uncancellable(self, tmp_path):
        import os

        proc = subprocess.Popen(["sh", "-c", "sleep 5"])
        try:
            job_id = "job-feedface"
            job_dir = tmp_path / "system" / "jobs" / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "command").write_text("sleep 5")
            (job_dir / "status").write_text("running")
            (job_dir / "pid").write_text(str(proc.pid))
            (job_dir / "stdout.log").write_text("")
            (job_dir / "stderr.log").write_text("")

            manager = self._manager(tmp_path)
            poll = manager.handle({"action": "poll", "job_id": job_id})
            assert poll["status"] == "running"
            assert poll["pid"] == proc.pid
            assert "cancellation is unavailable" in poll["message"]

            cancel = manager.handle({"action": "cancel", "job_id": job_id})
            assert cancel["status"] == "error"
            assert "legacy" in cancel["message"].lower()
            os.kill(proc.pid, 0)
        finally:
            proc.terminate()
            proc.wait(timeout=2)

    def test_pid_identity_mismatch_refuses_process_group_signal(self, tmp_path):
        import os
        import subprocess

        proc = subprocess.Popen(["sh", "-c", "sleep 5"], start_new_session=True)
        try:
            job_id = "job-11111111111111111111111111111111"
            job_dir = self._durable_state(
                tmp_path,
                job_id,
                status="running",
                deadline=time.time() + 30,
                pid=proc.pid,
                pid_identity="not-the-saved-process",
            )
            # Compatibility files make sure an old manager would have signalled it.
            (job_dir / "status").write_text("running")
            (job_dir / "pid").write_text(str(proc.pid))

            result = self._manager(tmp_path).handle({"action": "cancel", "job_id": job_id})
            assert result["status"] == "error"
            assert "identity" in result["message"].lower()
            os.kill(proc.pid, 0)
        finally:
            proc.terminate()
            proc.wait(timeout=2)

    def test_running_result_prefers_neutral_command_ref_over_legacy_fields(self, tmp_path):
        observed = []

        class RecordingProcessPort:
            @staticmethod
            def observe(process):
                observed.append(process)
                return ProcessObservation("same")

        manager = BashManager(
            policy=BashPolicy.yolo(),
            working_dir=str(tmp_path),
            agent=SimpleNamespace(_notification_store=notification_store_for(tmp_path)),
            async_process=RecordingProcessPort(),
        )
        neutral = ProcessRef(77, "adapter-owned-incarnation")
        result = manager._running_result("job-neutral-ref", {
            "command_process": neutral.to_dict(),
            "pid": 88,
            "pid_identity": "legacy-incarnation",
        })
        assert result == {"status": "running", "job_id": "job-neutral-ref", "pid": 77}
        assert observed == [neutral]

    def test_cancel_refuses_unverifiable_neutral_supervisor_ref(self, tmp_path):
        class UnknownProcessPort:
            @staticmethod
            def observe(process):
                return ProcessObservation("unknown")

        manager = BashManager(
            policy=BashPolicy.yolo(),
            working_dir=str(tmp_path),
            agent=SimpleNamespace(_notification_store=notification_store_for(tmp_path)),
            async_process=UnknownProcessPort(),
        )
        job_id = "job-11111111111111111111111111111112"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = manager._initial_async_state(job_id, "sleep 1", str(tmp_path), 30)
        state.update({
            "status": "running",
            "supervisor_pid": 77,
            "supervisor_identity": None,
            "supervisor_process": ProcessRef(77, None).to_dict(),
        })
        _async_supervisor.write_initial_state(job_dir, state)

        result = manager.handle({"action": "cancel", "job_id": job_id})
        assert result["status"] == "error"
        assert "cannot be verified" in result["message"]
        recovered = json.loads((job_dir / "state.json").read_text())
        assert "cancel_requested_at" not in recovered

    def test_rehydrated_completion_is_published_to_bash_notification_channel(self, tmp_path):
        job_id = "job-20000000000000000000000000000003"
        self._durable_state(tmp_path, job_id, deadline=time.time() + 30)

        self._manager(tmp_path)
        self._wait_for(lambda: "bash" in snapshot_notifications(tmp_path))
        payload = snapshot_notifications(tmp_path)["bash"]
        assert payload["data"]["job_id"] == job_id
        assert payload["data"]["exit_code"] == 0
        assert payload["data"]["ref_id"] == f"bash.completion:{job_id}"

    def test_completion_retry_is_idempotent_at_notification_store(self, tmp_path):
        class CountingStore:
            def __init__(self):
                self.current = {}
                self.writes = 0

            def compare_update_channel(self, channel, _expected, mutate):
                payload, changed, value = mutate(self.current.get(channel))
                if changed:
                    self.current[channel] = payload
                    self.writes += 1
                return SimpleNamespace(value=value)

        job_id = "job-20000000000000000000000000000004"
        job_dir = self._durable_state(tmp_path, job_id, deadline=time.time() + 30)
        store = CountingStore()
        manager = BashManager(
            policy=BashPolicy.yolo(),
            working_dir=str(tmp_path),
            agent=SimpleNamespace(_notification_store=store),
        )
        assert store.writes == 1

        # Simulate a crash window where durable state is retried after the sink
        # already contains the same stable completion reference.
        state = json.loads((job_dir / "state.json").read_text())
        assert manager._publish_async_completion(job_id, job_dir, state) is True
        assert store.writes == 1


def _wait_for_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


def _observe_single_process(pid: int) -> dict | None:
    """Observe exactly one PID: parent, process group, start time, command."""
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=,pgid=,lstart=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.strip().split(None, 7)
    if result.returncode != 0 or len(fields) < 7:
        return None
    return {
        "ppid": int(fields[0]), "pgid": int(fields[1]),
        "started": " ".join(fields[2:7]),
        "command": fields[7] if len(fields) > 7 else "",
    }


def _reap_owned_descendant(tmp_path: Path, marker: str) -> None:
    """Test-owned teardown (issue #943): reap only the TERM-ignoring descendant
    this invocation launched, after proving it is that exact process.  TERM, a
    bounded grace, KILL only if the identical process survived, then a bounded
    wait for the outer shell's own reap.  Only this one PID is observed or
    signalled, and nothing here raises over the test's own failure.
    """
    _wait_for_file(tmp_path / "descendant.ready", 2.0)
    pid_file = tmp_path / "descendant.pid"
    pid = int(pid_file.read_text().strip() or 0) if pid_file.exists() else 0
    claimed = _observe_single_process(pid) if pid else None
    # Ownership: this invocation's marker is on the command line and the parent
    # is the job's own group leader (the outer shell).  Every later check
    # requires the identical observation (parent, group, start time, command),
    # so a reused PID is never signalled.
    if not (claimed and marker in claimed["command"] and claimed["ppid"] == claimed["pgid"]):
        return
    for sig, grace in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 2.0)):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if _observe_single_process(pid) != claimed:
                return
            time.sleep(0.02)


class TestBashAsyncTerminalRaces:
    """Deterministic coverage for durable terminal/cancellation linearization."""

    @staticmethod
    def _manager(tmp_path: Path) -> BashManager:
        return BashManager(
            policy=BashPolicy.yolo(),
            working_dir=str(tmp_path),
            agent=SimpleNamespace(_notification_store=notification_store_for(tmp_path)),
        )

    @staticmethod
    def _completed_job(tmp_path: Path, job_id: str) -> Path:
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        now = time.time()
        _async_supervisor.write_initial_state(job_dir, {
            "version": 2, "job_id": job_id, "command": "echo durable",
            "cwd": str(tmp_path), "status": "completed", "created_at": now - 1,
            "started_at": now - 1, "finished_at": now, "pid": None,
            "pid_identity": None, "exit_status_known": True, "exit_code": 0,
            "terminal_polled": False,
            "reminder": {"deadline_at": now + 30, "state": "pending", "ref_id": f"bash.reminder:{job_id}"},
            "completion": {"state": "pending", "ref_id": f"bash.completion:{job_id}"},
        })
        (job_dir / "stdout.log").write_text("durable output\n")
        (job_dir / "stderr.log").write_text("")
        return job_dir

    def test_poll_does_not_consume_unknown_during_supervisor_precommit_window(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        job_id = "job-10000000000000000000000000000001"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        initial_state = manager._initial_async_state(job_id, "exit 23", str(tmp_path), 30)
        _async_supervisor.write_initial_state(job_dir, initial_state)
        start_token = initial_state["supervisor_start_lease"]["token"]
        before_commit = threading.Event()
        release_commit = threading.Event()
        real_update_state = _async_supervisor.update_state

        def paused_update(job_path, mutate):
            if getattr(mutate, "__name__", "") == "mark_completed":
                before_commit.set()
                assert release_commit.wait(2)
            return real_update_state(job_path, mutate)

        monkeypatch.setattr(_async_supervisor, "update_state", paused_update)
        worker = threading.Thread(
            target=_async_supervisor.supervise, args=(job_dir, start_token)
        )
        worker.start()
        assert before_commit.wait(2)

        # The command has exited, but its live recorded supervisor is paused before
        # the exact wait status commit.  This must remain recoverable, not consume
        # an unknown terminal response.
        pending = manager.handle({"action": "poll", "job_id": job_id})
        assert pending["status"] == "running"
        assert json.loads((job_dir / "state.json").read_text())["terminal_polled"] is False

        release_commit.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        terminal = manager.handle({"action": "poll", "job_id": job_id})
        assert terminal["status"] == "done"
        assert terminal["exit_status_known"] is True
        assert terminal["exit_code"] == 23

    def test_term_ignoring_group_is_killed_by_supervisor_before_cancelled(self, tmp_path):
        manager = self._manager(tmp_path)
        started = manager.handle({
            "command": "trap '' TERM; while :; do sleep 1; done",
            "async": True,
            "reminder": 30,
        })
        began = time.monotonic()
        cancelled = manager.handle({"action": "cancel", "job_id": started["job_id"]})
        elapsed = time.monotonic() - began
        assert cancelled == {"status": "cancelled", "job_id": started["job_id"]}
        # The supervisor grants TERM a bounded interval before escalating to KILL.
        assert elapsed >= 0.35
        state = json.loads(
            (tmp_path / "system" / "jobs" / started["job_id"] / "state.json").read_text()
        )
        assert state["status"] == "completed"
        assert state["exit_status_known"] is True
        assert state["terminal_polled"] is True
        assert state["reminder"]["state"] == "suppressed"

    def test_outer_shell_exit_does_not_leave_term_ignoring_descendant(self, tmp_path):
        manager = self._manager(tmp_path)
        # A no-op marker on the inner shell's own command line ties that exact
        # process to this invocation for the test-owned teardown below.
        marker = f"lingtai-943-{os.urandom(8).hex()}"
        started = manager.handle({
            "command": (
                f"sh -c ': {marker}; trap \"\" TERM; echo $$ > descendant.pid; "
                ": > descendant.ready; while :; do sleep 1; done' & wait $!"
            ),
            "async": True,
            "reminder": 30,
        })
        # From here on this test owns a TERM-ignoring descendant; every exit
        # path (assertion failure, interruption, normal) reaps that exact child.
        try:
            descendant_file = tmp_path / "descendant.pid"
            # The child writes this acknowledgement only after installing its
            # ignored-TERM trap, so cancellation necessarily exercises the
            # intended survivor.
            assert _wait_for_file(tmp_path / "descendant.ready", 2.0)
            assert descendant_file.exists()
            descendant_pid = int(descendant_file.read_text().strip())

            cancelled = manager.handle({"action": "cancel", "job_id": started["job_id"]})
            assert cancelled == {"status": "cancelled", "job_id": started["job_id"]}
            deadline = time.monotonic() + 2
            while _posix_process_is_alive(descendant_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _posix_process_is_alive(descendant_pid)
            state = json.loads(
                (tmp_path / "system" / "jobs" / started["job_id"] / "state.json").read_text()
            )
            assert state["exit_status_known"] is True
            assert state["cancellation_outcome"] == "group_cancelled"
        finally:
            _reap_owned_descendant(tmp_path, marker)

    def test_pre_cancel_failure_reaps_term_ignoring_descendant(self, tmp_path, monkeypatch):
        # Regression for Lingtai-AI/lingtai#943: a failure after the sibling test
        # launched its TERM-ignoring descendant but before its cancel must not
        # leak that exact child, and the (here injected) cancel path must never
        # be the cleanup mechanism.
        real_handle = BashManager.handle

        def fail_before_cancel(manager, args):
            if args.get("action") == "cancel":
                raise AssertionError("injected pre-cancel failure")
            return real_handle(manager, args)

        monkeypatch.setattr(BashManager, "handle", fail_before_cancel)
        with pytest.raises(AssertionError, match="injected pre-cancel failure"):
            self.test_outer_shell_exit_does_not_leave_term_ignoring_descendant(tmp_path)

        descendant_pid = int((tmp_path / "descendant.pid").read_text().strip())
        deadline = time.monotonic() + 2
        while _posix_process_is_alive(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _posix_process_is_alive(descendant_pid)
        # With the descendant gone, the outer shell's wait returns and the
        # supervisor records the job's terminal outcome on its own.
        (job_dir,) = (tmp_path / "system" / "jobs").iterdir()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads((job_dir / "state.json").read_text())
            if state["exit_status_known"]:
                break
            time.sleep(0.05)
        assert state["exit_status_known"] is True, state

    def test_owned_parent_reaper_terminalizes_early_supervisor_exit(self, tmp_path):
        manager = self._manager(tmp_path)
        job_id = "job-10000000000000000000000000000004"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        _async_supervisor.write_initial_state(
            job_dir, manager._initial_async_state(job_id, "sleep 1", str(tmp_path), 30)
        )

        class ExitedSupervisorPort:
            @staticmethod
            def wait_supervisor(owned):
                assert owned is owned_supervisor
                return 17

        owned_supervisor = object()
        manager._async_process = ExitedSupervisorPort()
        manager._reap_supervisor(owned_supervisor, job_dir)
        state = json.loads((job_dir / "state.json").read_text())
        assert state["status"] == "unrecoverable"
        assert state["exit_status_known"] is False
        assert "exited with code 17" in state["supervisor_error"]

    def test_owned_parent_reaps_actual_supervisor_exit_before_start_claim(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        real_popen = subprocess.Popen

        def launch_with_wrong_start_token(args, *popen_args, **popen_kwargs):
            actual_args = list(args)
            if "lingtai.tools.bash._async_supervisor" in actual_args:
                actual_args[-1] = "wrong-start-token"
            return real_popen(actual_args, *popen_args, **popen_kwargs)

        monkeypatch.setattr(
            "lingtai.tools.bash.subprocess.Popen", launch_with_wrong_start_token
        )
        started = manager.handle({
            "command": "echo must-not-run", "async": True, "reminder": 30,
        })
        assert started["status"] == "error"
        job_dirs = list((tmp_path / "system" / "jobs").iterdir())
        assert len(job_dirs) == 1
        state = json.loads((job_dirs[0] / "state.json").read_text())
        assert state["status"] == "unrecoverable"
        assert state["pid"] is None
        assert state["exit_status_known"] is False
        assert "owned supervisor exited with code 2" in state["supervisor_error"]

    def test_fresh_manager_recovers_actual_preclaim_exit_after_owner_loss(
        self, tmp_path
    ):
        seed = self._manager(tmp_path)
        job_id = "job-1000000000000000000000000000000b"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = seed._initial_async_state(job_id, "echo must-not-run", str(tmp_path), 30)
        _async_supervisor.write_initial_state(job_dir, state)
        wrong_token = "wrong-start-token"
        supervisor = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lingtai.tools.bash._async_supervisor",
                str(job_dir),
                wrong_token,
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def record_without_identity(current):
            current["supervisor_pid"] = supervisor.pid
            return current

        _async_supervisor.update_state(job_dir, record_without_identity)
        assert supervisor.wait(timeout=2) == 2
        self._manager(tmp_path)
        recovered = json.loads((job_dir / "state.json").read_text())
        assert recovered["status"] == "unrecoverable"
        assert recovered["pid"] is None
        assert recovered["exit_status_known"] is False
        assert "definitively gone" in recovered["supervisor_error"]

    def test_fresh_manager_proves_dead_supervisor_without_identity(self, tmp_path):
        seed = self._manager(tmp_path)
        job_id = "job-10000000000000000000000000000005"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = seed._initial_async_state(job_id, "sleep 1", str(tmp_path), 30)
        state["supervisor_pid"] = 999_999_999
        state["supervisor_start_lease"].update({
            "state": "claimed", "supervisor_pid": 999_999_999,
        })
        _async_supervisor.write_initial_state(job_dir, state)

        self._manager(tmp_path)
        recovered = json.loads((job_dir / "state.json").read_text())
        assert recovered["status"] == "unrecoverable"
        assert "definitively gone" in recovered["supervisor_error"]

    def test_fresh_manager_expires_unclaimed_supervisor_start_lease(self, tmp_path):
        seed = self._manager(tmp_path)
        job_id = "job-10000000000000000000000000000006"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = seed._initial_async_state(job_id, "sleep 1", str(tmp_path), 30)
        state["supervisor_start_lease"]["deadline_at"] = time.time() - 1
        _async_supervisor.write_initial_state(job_dir, state)

        self._manager(tmp_path)
        recovered = json.loads((job_dir / "state.json").read_text())
        assert recovered["status"] == "unrecoverable"
        assert "start lease expired" in recovered["supervisor_error"]

    @pytest.mark.parametrize("terminal", [False, True])
    def test_supervisor_refuses_expired_or_terminal_start_lease(
        self, tmp_path, monkeypatch, terminal
    ):
        manager = self._manager(tmp_path)
        job_id = (
            "job-10000000000000000000000000000007"
            if not terminal
            else "job-10000000000000000000000000000008"
        )
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = manager._initial_async_state(job_id, "echo forbidden", str(tmp_path), 30)
        token = state["supervisor_start_lease"]["token"]
        if terminal:
            state.update({"status": "unrecoverable", "finished_at": time.time()})
        else:
            state["supervisor_start_lease"]["deadline_at"] = time.time() - 1
        _async_supervisor.write_initial_state(job_dir, state)
        monkeypatch.setattr(
            "lingtai.adapters.shell_process.select_shell_async_process",
            lambda: pytest.fail("expired/terminal supervisor selected a process adapter"),
        )

        assert _async_supervisor.supervise(job_dir, token) == 2
        recovered = json.loads((job_dir / "state.json").read_text())
        assert recovered["status"] == "unrecoverable"

    def test_stale_pre_return_reminder_timer_defers_to_latest_deadline(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        job_id = "job-10000000000000000000000000000009"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = manager._initial_async_state(job_id, "sleep 1", str(tmp_path), 30)
        state["reminder"]["deadline_at"] = time.time() - 1
        _async_supervisor.write_initial_state(job_dir, state)
        captured = []

        class CapturedThread:
            def __init__(self, *, target, args, daemon):
                captured.append((target, args, daemon))

            def start(self):
                return None

        monkeypatch.setattr("lingtai.tools.bash.threading.Thread", CapturedThread)
        published = []
        monkeypatch.setattr(
            manager, "_publish_async_reminder", lambda value: published.append(value) or True
        )
        manager._start_reminder_timer(job_id, job_dir)
        assert len(captured) == 1

        latest_deadline = time.time() + 30

        def retime(current):
            current["reminder"]["deadline_at"] = latest_deadline
            return current

        _async_supervisor.update_state(job_dir, retime)
        target, args, _ = captured[0]
        target(*args)

        recovered = json.loads((job_dir / "state.json").read_text())
        assert published == []
        assert recovered["reminder"]["state"] == "pending"
        assert recovered["reminder"]["deadline_at"] == latest_deadline
        assert "claim_token" not in recovered["reminder"]
        assert len(captured) == 2

    def test_return_handoff_blocks_fallback_while_parent_popen_is_delayed(
        self, tmp_path, monkeypatch
    ):
        manager_a = self._manager(tmp_path)
        reminder = 0.08
        parent_popen_entered = threading.Event()
        release_parent_popen = threading.Event()
        real_popen = subprocess.Popen

        def gated_supervisor_popen(*args, **kwargs):
            parent_popen_entered.set()
            assert release_parent_popen.wait(2)
            return real_popen(*args, **kwargs)

        monkeypatch.setattr("lingtai.tools.bash.subprocess.Popen", gated_supervisor_popen)
        published = []
        monkeypatch.setattr(
            BashManager,
            "_publish_async_reminder",
            lambda self, job_id: published.append(job_id) or True,
        )
        claim_attempted = threading.Event()
        real_claim = BashManager._claim_reminder_timer

        def observed_claim(manager, *args):
            result = real_claim(manager, *args)
            claim_attempted.set()
            return result

        monkeypatch.setattr(BashManager, "_claim_reminder_timer", observed_claim)
        outcome = {}

        def start_job():
            outcome["result"] = manager_a.handle({
                "command": "sleep 5", "async": True, "reminder": reminder,
            })
            outcome["returned_at"] = time.time()

        starter = threading.Thread(target=start_job)
        starter.start()
        assert parent_popen_entered.wait(2)
        job_dirs = list((tmp_path / "system" / "jobs").iterdir())
        assert len(job_dirs) == 1
        job_dir = job_dirs[0]

        # A second manager rehydrates the old crash-fallback deadline while the
        # first manager is still live but has not launched its supervisor.  Its
        # due timer must observe the durable return handoff and defer.
        self._manager(tmp_path)
        assert claim_attempted.wait(2)
        assert published == []
        pending = json.loads((job_dir / "state.json").read_text())
        assert pending["status"] == "launching"
        assert pending["return_handoff"]["state"] == "pending"
        assert pending["reminder"]["state"] == "pending"

        release_parent_popen.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert outcome["result"]["status"] == "ok"
        armed = json.loads((job_dir / "state.json").read_text())
        returned_at = armed["return_handoff"]["returned_at"]
        assert armed["return_handoff"]["state"] == "armed"
        assert armed["reminder"]["deadline_at"] == pytest.approx(
            returned_at + reminder
        )
        assert returned_at <= outcome["returned_at"]
        manager_a.handle({"action": "cancel", "job_id": outcome["result"]["job_id"]})

    def test_return_handoff_blocks_fallback_after_running_before_return_arm(
        self, tmp_path, monkeypatch
    ):
        manager_a = self._manager(tmp_path)
        reminder = 0.05
        before_return_arm = threading.Event()
        release_return_arm = threading.Event()
        real_update_state = _async_supervisor.update_state

        def paused_manager_update(job_dir, mutate):
            if getattr(mutate, "__name__", "") == "arm_from_return":
                before_return_arm.set()
                assert release_return_arm.wait(2)
            return real_update_state(job_dir, mutate)

        monkeypatch.setattr("lingtai.tools.bash.update_state", paused_manager_update)
        published = []
        monkeypatch.setattr(
            BashManager,
            "_publish_async_reminder",
            lambda self, job_id: published.append(job_id) or True,
        )
        claim_attempted = threading.Event()
        real_claim = BashManager._claim_reminder_timer

        def observed_claim(manager, *args):
            result = real_claim(manager, *args)
            claim_attempted.set()
            return result

        monkeypatch.setattr(BashManager, "_claim_reminder_timer", observed_claim)
        outcome = {}

        def start_job():
            outcome["result"] = manager_a.handle({
                "command": "sleep 5", "async": True, "reminder": reminder,
            })
            outcome["returned_at"] = time.time()

        starter = threading.Thread(target=start_job)
        starter.start()
        assert before_return_arm.wait(2)
        job_dirs = list((tmp_path / "system" / "jobs").iterdir())
        assert len(job_dirs) == 1
        job_dir = job_dirs[0]
        pre_arm = json.loads((job_dir / "state.json").read_text())
        assert pre_arm["status"] == "running"
        assert pre_arm["return_handoff"]["state"] == "pending"
        sleep_for = pre_arm["reminder"]["deadline_at"] - time.time() + 0.02
        if sleep_for > 0:
            time.sleep(sleep_for)

        self._manager(tmp_path)
        assert claim_attempted.wait(2)
        assert published == []
        still_pending = json.loads((job_dir / "state.json").read_text())
        assert still_pending["return_handoff"]["state"] == "pending"
        assert still_pending["reminder"]["state"] == "pending"

        release_return_arm.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert outcome["result"]["status"] == "ok"
        armed = json.loads((job_dir / "state.json").read_text())
        returned_at = armed["return_handoff"]["returned_at"]
        assert armed["return_handoff"]["state"] == "armed"
        assert armed["reminder"]["deadline_at"] == pytest.approx(
            returned_at + reminder
        )
        assert returned_at <= outcome["returned_at"]
        manager_a.handle({"action": "cancel", "job_id": outcome["result"]["job_id"]})

    def test_owner_resuming_after_handoff_expiry_cannot_report_start_success(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "lingtai.tools.bash._RETURN_HANDOFF_LEASE_SECONDS", 0.2
        )
        manager_a = self._manager(tmp_path)
        before_return_arm = threading.Event()
        release_return_arm = threading.Event()
        fallback_published = threading.Event()
        real_update_state = _async_supervisor.update_state

        def paused_manager_update(job_dir, mutate):
            if getattr(mutate, "__name__", "") == "arm_from_return":
                before_return_arm.set()
                assert release_return_arm.wait(2)
            return real_update_state(job_dir, mutate)

        monkeypatch.setattr("lingtai.tools.bash.update_state", paused_manager_update)
        published = []

        def record_publish(manager, job_id):
            published.append(job_id)
            fallback_published.set()
            return True

        monkeypatch.setattr(BashManager, "_publish_async_reminder", record_publish)
        outcome = {}

        def start_job():
            outcome["result"] = manager_a.handle({
                "command": "sleep 5", "async": True, "reminder": 0.02,
            })

        starter = threading.Thread(target=start_job)
        starter.start()
        assert before_return_arm.wait(2)
        job_dirs = list((tmp_path / "system" / "jobs").iterdir())
        assert len(job_dirs) == 1
        job_dir = job_dirs[0]
        pre_expiry = json.loads((job_dir / "state.json").read_text())
        assert pre_expiry["status"] == "running"
        assert pre_expiry["return_handoff"]["state"] == "pending"

        # B is now entitled to the bounded crash fallback because A has not
        # completed its return transition before the durable handoff deadline.
        self._manager(tmp_path)
        try:
            assert fallback_published.wait(2)
            # The sink callback runs while the state lock is held; its event can
            # become visible just before publishing -> published is fsynced.
            publish_deadline = time.monotonic() + 2
            while time.monotonic() < publish_deadline:
                expired = json.loads((job_dir / "state.json").read_text())
                if expired["reminder"]["state"] == "published":
                    break
                time.sleep(0.01)
            assert expired["return_handoff"]["state"] == "expired"
            assert expired["reminder"]["state"] == "published"
        finally:
            release_return_arm.set()
            starter.join(timeout=2)
        assert not starter.is_alive()
        late = outcome["result"]
        assert late["status"] == "error"
        assert late["job_id"] == job_dir.name
        assert isinstance(late["pid"], int)
        assert "remains pollable" in late["message"]
        final = json.loads((job_dir / "state.json").read_text())
        assert final["return_handoff"]["state"] == "expired"
        assert final["reminder"]["state"] == "published"
        assert published == [job_dir.name]
        manager_a.handle({"action": "cancel", "job_id": job_dir.name})

    def test_expired_suppressing_reminder_recovers_after_manager_crash(
        self, tmp_path, monkeypatch
    ):
        manager = self._manager(tmp_path)
        job_id = "job-1000000000000000000000000000000a"
        job_dir = tmp_path / "system" / "jobs" / job_id
        job_dir.mkdir(parents=True)
        state = manager._initial_async_state(job_id, "sleep 5", str(tmp_path), 30)
        state.update({"status": "running", "pid": None})
        state["return_handoff"].update({
            "state": "armed", "returned_at": time.time() - 2,
        })
        state["reminder"].update({
            "state": "suppressing",
            "deadline_at": time.time() - 1,
            "suppressing_at": time.time() - 2,
            "suppressing_until": time.time() - 1,
        })
        _async_supervisor.write_initial_state(job_dir, state)
        published = []
        monkeypatch.setattr(
            manager,
            "_publish_async_reminder",
            lambda value: published.append(value) or True,
        )
        cancel_event = threading.Event()
        with manager._reminder_lock:
            manager._reminder_cancel_events[job_id] = cancel_event

        claim_token = manager._claim_reminder_timer(job_id, job_dir, cancel_event)
        assert isinstance(claim_token, str)
        assert manager._publish_claimed_reminder(job_id, job_dir, claim_token) is True
        recovered = json.loads((job_dir / "state.json").read_text())
        assert published == [job_id]
        assert recovered["reminder"]["state"] == "published"
        assert "suppressing_until" not in recovered["reminder"]

    def test_concurrent_managers_have_one_terminal_consumer(self, tmp_path):
        job_id = "job-10000000000000000000000000000002"
        self._completed_job(tmp_path, job_id)
        managers = [self._manager(tmp_path), self._manager(tmp_path)]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda manager: manager.handle({"action": "poll", "job_id": job_id}),
                managers,
            ))

        assert [result["status"] for result in results].count("done") == 1
        assert [result["status"] for result in results].count("error") == 1

    def test_terminal_suppression_wins_over_stale_reminder_publish(self, tmp_path, monkeypatch):
        job_id = "job-10000000000000000000000000000003"
        manager = self._manager(tmp_path)
        # Create the terminal fixture after construction so rehydration cannot
        # pre-suppress it; this test needs a stale claim acquired before poll wins.
        job_dir = self._completed_job(tmp_path, job_id)

        def make_due(current):
            current["reminder"]["deadline_at"] = time.time() - 1
            return current

        _async_supervisor.update_state(job_dir, make_due)
        cancel_event = threading.Event()
        with manager._reminder_lock:
            manager._reminder_cancel_events[job_id] = cancel_event
        claim_token = manager._claim_reminder_timer(job_id, job_dir, cancel_event)
        assert isinstance(claim_token, str)

        published = []
        monkeypatch.setattr(manager, "_publish_async_reminder", lambda value: published.append(value) or True)
        assert manager._terminal_result(job_id, job_dir)["status"] == "done"
        # The token was acquired before the terminal action, but final
        # publication rechecks it under the same durable state lock.
        assert manager._publish_claimed_reminder(job_id, job_dir, claim_token) is False
        assert published == []
        state = json.loads((job_dir / "state.json").read_text())
        assert state["reminder"]["state"] == "suppressed"

    def test_full_ids_are_strict_and_collision_safe(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path)
        existing = "a" * 32
        replacement = "b" * 32
        (tmp_path / "system" / "jobs" / f"job-{existing}").mkdir(parents=True)
        values = iter((SimpleNamespace(hex=existing), SimpleNamespace(hex=replacement)))
        monkeypatch.setattr("lingtai.tools.bash.uuid.uuid4", lambda: next(values))

        started = manager.handle({"command": "exit 0", "async": True, "reminder": 30})
        assert started["job_id"] == f"job-{replacement}"
        assert len(started["job_id"]) == 36
        assert manager._validate_job_id(started["job_id"]) is None
        # The old eight-hex retained layout remains readable only as an explicit
        # migration form; all other text, including traversal-shaped input, fails.
        assert manager._validate_job_id("job-deadbeef") is None
        for invalid in ("job-" + "c" * 31, "job-" + "C" * 32, "job-../escape"):
            assert manager._validate_job_id(invalid)["status"] == "error"
