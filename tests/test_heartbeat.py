"""Tests for heartbeat — always-on agent health monitor with AED."""
import time
from unittest.mock import MagicMock


def make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


class TestHeartbeatInit:

    def test_heartbeat_counter_initialized(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        assert agent._heartbeat == 0.0
        assert agent._heartbeat_thread is None
        assert agent._cpr_start is None
        assert agent._aed_pending is False

    def test_heartbeat_in_status(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._heartbeat = 1234567890.123
        status = agent.status()
        assert isinstance(status["heartbeat"], float)
        assert status["heartbeat"] == 1234567890.123


class TestHeartbeatBeating:

    def test_heartbeat_increments(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        time.sleep(2.5)
        agent._stop_heartbeat()
        assert agent._heartbeat > 0
        assert time.time() - agent._heartbeat < 2.0

    def test_no_aed_on_idle(self, tmp_path):
        """Heartbeat does NOT AED when agent is IDLE."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE)

        time.sleep(2.0)
        agent._stop_heartbeat()
        assert agent.inbox.empty()
        assert agent._cpr_start is None


class TestHeartbeatFile:

    def test_heartbeat_writes_file(self, tmp_path):
        """Heartbeat file exists while running, deleted after stop."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        time.sleep(1.5)
        assert hb_file.exists()
        agent._stop_heartbeat()
        assert not hb_file.exists()

    def test_heartbeat_file_written_while_running(self, tmp_path):
        """While ACTIVE, heartbeat file exists with a fresh timestamp."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        time.sleep(1.5)

        assert hb_file.exists()
        ts = float(hb_file.read_text())
        assert time.time() - ts < 2.0

        agent._stop_heartbeat()

    def test_heartbeat_file_stale_when_dormant(self, tmp_path):
        """After DORMANT + shutdown, heartbeat file is gone or stale."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        time.sleep(1.5)
        assert hb_file.exists()

        # Simulate DORMANT via AED timeout
        agent._set_state(AgentState.STUCK)
        agent._cpr_start = time.monotonic() - 1260  # exceeded 20 min
        time.sleep(2.0)  # heartbeat detects and sets DORMANT + shutdown

        assert agent._state == AgentState.DORMANT
        # After DORMANT, the heartbeat loop exits and _stop_heartbeat
        # would clean the file. The loop itself stops writing once DORMANT.
        # The file may still exist with a stale timestamp from the last
        # living tick, or may be gone if _stop_heartbeat was called.
        if hb_file.exists():
            ts = float(hb_file.read_text())
            assert time.time() - ts > 1.0  # stale — not recently updated


class TestAED:

    def test_aed_resets_session_on_error(self, tmp_path):
        """AED resets the LLM session when agent is STUCK."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.STUCK)
        agent._session._chat = MagicMock()  # has a session

        agent._perform_aed()

        assert agent._session.chat is None  # session reset
        assert not agent.inbox.empty()
        msg = agent.inbox.get_nowait()
        assert "reviving" in msg.content
        assert msg.sender == "system"

    def test_aed_fires_once_per_error(self, tmp_path):
        """AED fires once, then waits — does NOT flood inbox."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.STUCK)

        time.sleep(3.0)
        agent._stop_heartbeat()

        # Should have only ONE revive message
        count = 0
        while not agent.inbox.empty():
            agent.inbox.get_nowait()
            count += 1
        assert count == 1

    def test_aed_pending_resets_on_recovery(self, tmp_path):
        """When agent recovers to ACTIVE, _aed_pending resets."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._aed_pending = True
        agent._cpr_start = time.monotonic()

        # Simulate recovery
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="revive")
        agent._set_state(AgentState.IDLE)

        time.sleep(1.5)
        agent._stop_heartbeat()

        assert agent._aed_pending is False
        assert agent._cpr_start is None

    def test_aed_on_error_via_heartbeat(self, tmp_path):
        """Full cycle: error → heartbeat detects → AED → revive message."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.STUCK)

        # Wait for heartbeat to detect and AED
        deadline = time.monotonic() + 3.0
        while agent.inbox.empty() and time.monotonic() < deadline:
            time.sleep(0.1)

        agent._stop_heartbeat()
        assert not agent.inbox.empty()
        msg = agent.inbox.get_nowait()
        assert "reviving" in msg.content
        assert "stuck" in msg.content


class TestHeartbeatDead:

    def test_aed_timeout_triggers_dormant(self, tmp_path):
        """After AED timeout, agent goes DORMANT."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.STUCK)
        # Simulate AED started 21 minutes ago (exceeds 20 min window)
        agent._cpr_start = time.monotonic() - 1260

        agent._start_heartbeat()
        time.sleep(1.5)
        agent._stop_heartbeat()

        assert agent._state == AgentState.DORMANT
        assert agent._shutdown.is_set()

    def test_dormant_state_in_status(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_id="test",
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._state = AgentState.DORMANT
        status = agent.status()
        assert status["state"] == "dormant"
