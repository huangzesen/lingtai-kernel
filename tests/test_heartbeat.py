"""Tests for heartbeat — always-on agent health monitor with AED timeout."""
import time
from lingtai.adapters.lifecycle_clock import SystemLifecycleClockAdapter
from lingtai.tools.registry import INTRINSICS as _TEST_INTRINSICS
from unittest.mock import MagicMock
from tests._service_helpers import make_tool_result_mock_service as make_mock_service
from tests._workdir_lease_helpers import make_test_lease
from tests._snapshot_helpers import make_test_snapshot_port, make_test_source_revision_port
from tests._lifecycle_clock_helpers import make_test_lifecycle_clock
from lingtai.adapters.posix.agent_presence import PosixAgentPresenceStoreAdapter
from tests._notification_store_helpers import notification_store_for




class TestHeartbeatInit:

    def test_heartbeat_counter_initialized(self, tmp_path):
        from lingtai.kernel import BaseAgent
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        assert agent._heartbeat == 0.0
        assert agent._heartbeat_thread is None
        assert agent._aed_start is None

    def test_heartbeat_attribute_present(self, tmp_path):
        """The agent carries a ``_heartbeat`` float attribute. The
        live-runtime ``status()`` no longer surfaces it directly — the
        canonical liveness signal is the ``.agent.heartbeat`` file on
        disk (observed through the Agent Presence Store and Core policy)."""
        from lingtai.kernel import BaseAgent
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._heartbeat = 1234567890.123
        assert isinstance(agent._heartbeat, float)
        assert agent._heartbeat == 1234567890.123
        status = agent.status()
        assert status["identity"]["agent_id"] == agent._agent_id
        assert status["runtime"]["pid"] > 0
        assert status["runtime"]["running"] is False
        assert status["runtime"]["last_heartbeat"] == 1234567890.123


class TestHeartbeatBeating:

    def test_heartbeat_increments(self, tmp_path):
        from lingtai.kernel import BaseAgent
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=SystemLifecycleClockAdapter(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._start_heartbeat()
        time.sleep(2.5)
        agent._stop_heartbeat()
        assert agent._heartbeat > 0
        assert time.time() - agent._heartbeat < 2.0

    def test_no_aed_on_idle(self, tmp_path):
        """Heartbeat does NOT set _aed_start when agent is IDLE."""
        from lingtai.kernel import BaseAgent, AgentState
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE)

        time.sleep(2.0)
        agent._stop_heartbeat()
        assert agent._aed_start is None


class TestHeartbeatFile:

    def test_heartbeat_writes_file(self, tmp_path):
        """Heartbeat file exists while running, deleted after stop."""
        from lingtai.kernel import BaseAgent, AgentState
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        time.sleep(1.5)
        assert hb_file.exists()
        status_file = agent._working_dir / ".status.json"
        assert status_file.exists()
        assert '"running": true' in status_file.read_text()
        agent._stop_heartbeat()
        assert not hb_file.exists()

    def test_heartbeat_file_written_while_running(self, tmp_path):
        """While ACTIVE, heartbeat file exists with a fresh timestamp."""
        from lingtai.kernel import BaseAgent, AgentState
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=SystemLifecycleClockAdapter(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        time.sleep(1.5)

        assert hb_file.exists()
        ts = float(hb_file.read_text())
        assert time.time() - ts < 2.0

        agent._stop_heartbeat()

    def test_heartbeat_file_alive_when_asleep(self, tmp_path):
        """ASLEEP is a living sleep — heartbeat keeps ticking."""
        from lingtai.kernel import BaseAgent, AgentState
        from lingtai.kernel.config import AgentConfig
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
            config=AgentConfig(aed_timeout=1.0), workdir_lease=make_test_lease(),  # very short timeout
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=SystemLifecycleClockAdapter(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        time.sleep(1.5)
        assert hb_file.exists()

        # Simulate STUCK — heartbeat will enforce aed_timeout → ASLEEP
        agent._set_state(AgentState.STUCK)
        time.sleep(3.0)  # wait for aed_timeout (1s) to elapse

        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        # Heartbeat keeps ticking in ASLEEP (living sleep) — file is fresh
        if hb_file.exists():
            ts = float(hb_file.read_text())
            assert time.time() - ts < 2.0  # still fresh
        agent._stop_heartbeat()


class TestHeartbeatAEDTimeout:
    """Heartbeat enforces aed_timeout as a safety net — forces ASLEEP if STUCK too long."""

    def test_aed_timeout_triggers_asleep(self, tmp_path):
        """After aed_timeout in STUCK, agent goes ASLEEP."""
        from lingtai.kernel import BaseAgent, AgentState
        from lingtai.kernel.config import AgentConfig
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
            config=AgentConfig(aed_timeout=1.0), workdir_lease=make_test_lease(),  # 1 second timeout
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=SystemLifecycleClockAdapter(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.STUCK)

        agent._start_heartbeat()
        time.sleep(3.0)  # wait for aed_timeout to elapse
        agent._stop_heartbeat()

        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        assert not agent._shutdown.is_set()

    def test_aed_start_resets_on_recovery(self, tmp_path):
        """When agent recovers from STUCK, _aed_start resets."""
        from lingtai.kernel import BaseAgent, AgentState
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._aed_start = time.monotonic()

        # Simulate recovery
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE)

        time.sleep(1.5)
        agent._stop_heartbeat()

        assert agent._aed_start is None

    def test_asleep_state_in_status(self, tmp_path):
        from lingtai.kernel import BaseAgent, AgentState
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._state = AgentState.ASLEEP
        status = agent.status()
        # State now lives under the "runtime" sub-dict (status() was reshaped
        # to group identity / runtime / tokens cleanly for the TUI).
        assert status["runtime"]["state"] == "asleep"


class TestSleepFile:

    def test_sleep_file_triggers_asleep_not_shutdown(self, tmp_path):
        """When .sleep is detected, agent goes ASLEEP and _asleep is set, _shutdown is NOT set."""
        from lingtai.kernel import BaseAgent, AgentState
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")

        # Write .sleep file for heartbeat to detect
        (agent._working_dir / ".sleep").write_text("")
        time.sleep(2.0)
        agent._stop_heartbeat()

        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        assert not agent._shutdown.is_set()


class TestSuspendFile:

    def test_suspend_file_triggers_shutdown(self, tmp_path):
        """When .suspend is detected, agent goes SUSPENDED and _shutdown IS set."""
        from lingtai.kernel import BaseAgent, AgentState
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")

        # Write .suspend file for heartbeat to detect
        (agent._working_dir / ".suspend").write_text("")
        time.sleep(2.0)
        agent._stop_heartbeat()

        assert agent._state == AgentState.SUSPENDED
        assert agent._shutdown.is_set()


class TestSelfSleep:

    def test_self_sleep_no_karma_required(self, tmp_path):
        """Any agent can self-sleep to ASLEEP without admin.karma."""
        from lingtai.kernel import BaseAgent, AgentState
        from lingtai.tools.system import handle
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=make_test_lifecycle_clock(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        agent._set_state(AgentState.ACTIVE, reason="test")

        # Self-sleep: action=sleep with no address
        result = handle(agent, {"action": "sleep", "input": {}})

        assert result["status"] == "ok"
        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        assert not agent._shutdown.is_set()


class TestHeartbeatNeverBlocksOnNetwork:

    def test_slow_kernel_version_fetch_does_not_stall_heartbeat(self, tmp_path, monkeypatch):
        """#730 regression: the kernel_version remote probe runs off the
        heartbeat thread, so a fetch exceeding this test's 2s heartbeat-cadence
        responsiveness budget never makes a live agent look dead. That budget is
        independent of the configurable ``is_alive`` liveness policy."""
        import time
        from lingtai.kernel import BaseAgent
        from lingtai.kernel.nudge import kernel_version as kv
        agent = BaseAgent(
            intrinsics=_TEST_INTRINSICS,
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent", workdir_lease=make_test_lease(),
        agent_presence=PosixAgentPresenceStoreAdapter(tmp_path / "test_agent"), snapshot_port=make_test_snapshot_port(), lifecycle_clock=SystemLifecycleClockAdapter(), source_revision_port=make_test_source_revision_port(), notification_store=notification_store_for(tmp_path / "test_agent"),
        )
        # Force the nudge remote path: non-dev runtime with installed == running.
        monkeypatch.setattr(
            kv,
            "_runtime_info",
            lambda: kv._RuntimeInfo("0.17.0", "0.17.0", None),
        )
        # A fetch slower than this test's 2s heartbeat-cadence responsiveness budget.
        monkeypatch.setattr(
            kv,
            "_fetch_latest_version",
            lambda: (time.sleep(3.0), "0.17.0")[1],
        )

        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()

        samples = []
        deadline = time.monotonic() + 4.5
        while time.monotonic() < deadline:
            if hb_file.exists():
                try:
                    samples.append((time.time(), float(hb_file.read_text())))
                except ValueError:
                    pass
            time.sleep(0.2)

        pending = kv._fetch_slot(agent)
        if pending is not None:
            pending.thread.join()
        agent._stop_heartbeat()

        assert len(samples) >= 3
        stale = [(wall, ts) for wall, ts in samples if wall - ts >= 2.0]
        assert not stale, f"heartbeat went stale during the slow fetch (#730): {stale[:3]}"


def test_shutdown_gate_consumes_pending_suspend_and_persists_once(tmp_path, monkeypatch):
    """A signal may set shutdown before the heartbeat sees `.suspend`."""
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    from lingtai.kernel import AgentState
    from lingtai.kernel.base_agent import lifecycle

    class _Stop:
        def wait(self, _timeout):
            fake._heartbeat_thread = None

    fake = SimpleNamespace(
        _heartbeat_thread=object(),
        _heartbeat_runtime_ready=True,
        _shutdown=threading.Event(),
        _heartbeat_stop=_Stop(),
        _working_dir=tmp_path,
        _request_turn_cancel=Mock(),
        _set_state=Mock(),
        _log=Mock(),
    )
    fake._shutdown.set()  # exact signal-handler ordering
    marker = tmp_path / ".suspend"
    marker.write_text("")
    monkeypatch.setattr(lifecycle, "_write_heartbeat_tick", lambda _agent: None)

    lifecycle._heartbeat_loop(fake)

    assert not marker.exists()
    fake._set_state.assert_called_once_with(AgentState.SUSPENDED, reason="suspend signal")
    fake._request_turn_cancel.assert_called_once_with()
    fake._log.assert_any_call("suspend_received", source="signal_file")


def test_suspend_consumption_is_one_shot_even_if_marker_reappears(tmp_path):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    from lingtai.kernel import AgentState
    from lingtai.kernel.base_agent import lifecycle

    agent = SimpleNamespace(
        _working_dir=tmp_path,
        _request_turn_cancel=Mock(),
        _set_state=Mock(),
        _log=Mock(),
        _shutdown=threading.Event(),
    )
    marker = tmp_path / ".suspend"
    marker.write_text("")

    assert lifecycle._consume_suspend_signal(agent) is True
    marker.write_text("")
    assert lifecycle._consume_suspend_signal(agent) is False
    agent._set_state.assert_called_once_with(AgentState.SUSPENDED, reason="suspend signal")
    agent._request_turn_cancel.assert_called_once_with()
    agent._log.assert_called_once_with("suspend_received", source="signal_file")


def test_internal_shutdown_without_suspend_does_not_publish_suspended(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    from lingtai.kernel.base_agent import lifecycle

    class _Stop:
        def wait(self, _timeout):
            agent._heartbeat_thread = None

    agent = SimpleNamespace(
        _heartbeat_thread=object(),
        _heartbeat_runtime_ready=True,
        _shutdown=threading.Event(),
        _heartbeat_stop=_Stop(),
        _working_dir=tmp_path,
        _set_state=Mock(),
        _request_turn_cancel=Mock(),
        _log=Mock(),
    )
    agent._shutdown.set()  # internal teardown with no signal marker
    monkeypatch.setattr(lifecycle, "_write_heartbeat_tick", lambda _agent: None)

    lifecycle._heartbeat_loop(agent)

    agent._set_state.assert_not_called()
    agent._request_turn_cancel.assert_not_called()
    agent._log.assert_not_called()


class TestHeartbeatTickConstant:

    def test_heartbeat_loop_uses_kernel_tick_constant(self, monkeypatch):
        """The loop waits HEARTBEAT_TICK_SECONDS, not a local 1.0 literal."""
        from types import SimpleNamespace

        from lingtai.kernel import config
        from lingtai.kernel.base_agent import lifecycle

        waits: list[float] = []

        class _FakeStop:
            def wait(self, timeout: float) -> None:
                waits.append(timeout)
                # End the loop after one beat so the test terminates.
                _FakeAgent._heartbeat_thread = None

        class _FakeAgent:
            _heartbeat_thread = object()
            _shutdown = SimpleNamespace(is_set=lambda: True)
            _heartbeat_stop = _FakeStop()

        monkeypatch.setattr(lifecycle, "_write_heartbeat_tick", lambda agent: None)
        lifecycle._heartbeat_loop(_FakeAgent())

        assert waits == [config.HEARTBEAT_TICK_SECONDS]


def test_spawned_process_sigterm_and_sigint_persist_one_suspended_transition(tmp_path):
    """External POSIX stop signals reach the real handler and heartbeat gate.

    The child uses the production CLI handler, BaseAgent state writer, and
    lifecycle heartbeat. It keeps runtime startup gated so only the pending
    `.suspend` consume runs after the signal, making ordering deterministic
    without a provider or network.
    """
    import json
    import os
    import signal
    import subprocess
    import sys

    import pytest

    if os.name == "nt":
        pytest.skip("POSIX SIGTERM/SIGINT process contract")

    repo_root = __file__
    for _ in range(2):
        repo_root = os.path.dirname(repo_root)
    child = r'''
import json
import sys
import threading
import time
from pathlib import Path

from lingtai.cli import _install_signal_handlers
from lingtai.kernel import BaseAgent, AgentState
from lingtai.kernel.base_agent import lifecycle
from lingtai.tools.registry import INTRINSICS
from lingtai.adapters.lifecycle_clock import SystemLifecycleClockAdapter
from lingtai.adapters.posix.agent_presence import PosixAgentPresenceStoreAdapter
from tests._notification_store_helpers import notification_store_for
from tests._service_helpers import make_tool_result_mock_service
from tests._snapshot_helpers import make_test_snapshot_port, make_test_source_revision_port
from tests._workdir_lease_helpers import make_test_lease

workdir = Path(sys.argv[1])
agent = BaseAgent(
    intrinsics=INTRINSICS,
    service=make_tool_result_mock_service(),
    agent_name="signal-child",
    working_dir=workdir,
    workdir_lease=make_test_lease(),
    agent_presence=PosixAgentPresenceStoreAdapter(workdir),
    lifecycle_clock=SystemLifecycleClockAdapter(),
    snapshot_port=make_test_snapshot_port(),
    source_revision_port=make_test_source_revision_port(),
    notification_store=notification_store_for(workdir),
)
lifecycle.HEARTBEAT_TICK_SECONDS = 0.01
agent._heartbeat_runtime_ready = False
_install_signal_handlers(workdir, agent)
heartbeat = threading.Thread(target=lifecycle._heartbeat_loop, args=(agent,), daemon=True)
agent._heartbeat_thread = heartbeat
heartbeat.start()
print("READY", flush=True)

deadline = time.monotonic() + 5
while agent._state is not AgentState.SUSPENDED and time.monotonic() < deadline:
    time.sleep(0.01)
if agent._state is not AgentState.SUSPENDED:
    raise SystemExit("signal did not persist SUSPENDED")
manifest = json.loads((workdir / ".agent.json").read_text(encoding="utf-8"))
if manifest.get("state") != AgentState.SUSPENDED.value:
    raise SystemExit(f"manifest state was {manifest.get('state')!r}")
(workdir / "signal-result.json").write_text(
    json.dumps({"state": manifest["state"], "marker_exists": (workdir / ".suspend").exists()}),
    encoding="utf-8",
)
# Normal final teardown must not create another SUSPENDED transition.
agent.stop(timeout=2.0)
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(repo_root, "src"), repo_root, env.get("PYTHONPATH", "")]
    )
    for signum in (signal.SIGTERM, signal.SIGINT):
        workdir = tmp_path / signum.name
        workdir.mkdir()
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(workdir)],
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "READY"
            os.kill(process.pid, signum)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        result = json.loads((workdir / "signal-result.json").read_text())
        assert result == {"state": "suspended", "marker_exists": False}
        manifest = json.loads((workdir / ".agent.json").read_text())
        assert manifest["state"] == "suspended"

