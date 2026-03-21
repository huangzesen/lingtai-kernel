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
            agent_name="test",
            base_dir=tmp_path,
        )
        assert agent._heartbeat == 0
        assert agent._heartbeat_thread is None
        assert agent._cpr_start is None
        assert agent._aed_pending is False

    def test_heartbeat_in_status(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._heartbeat = 42
        status = agent.status()
        assert status["heartbeat"] == 42


class TestHeartbeatBeating:

    def test_heartbeat_increments(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        time.sleep(2.5)
        agent._stop_heartbeat()
        assert agent._heartbeat >= 2

    def test_no_aed_on_idle(self, tmp_path):
        """Heartbeat does NOT AED when agent is IDLE."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
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


class TestAED:

    def test_aed_resets_session_on_error(self, tmp_path):
        """AED resets the LLM session when agent is in ERROR."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.ERROR)
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
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.ERROR)

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
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.ERROR)

        # Wait for heartbeat to detect and AED
        deadline = time.monotonic() + 3.0
        while agent.inbox.empty() and time.monotonic() < deadline:
            time.sleep(0.1)

        agent._stop_heartbeat()
        assert not agent.inbox.empty()
        msg = agent.inbox.get_nowait()
        assert "reviving" in msg.content
        assert "error" in msg.content


class TestHeartbeatDead:

    def test_aed_timeout_triggers_dead(self, tmp_path):
        """After AED timeout, agent is pronounced DEAD."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.ERROR)
        # Simulate AED started 21 minutes ago (exceeds 20 min window)
        agent._cpr_start = time.monotonic() - 1260

        agent._start_heartbeat()
        time.sleep(1.5)
        agent._stop_heartbeat()

        assert agent._state == AgentState.DEAD
        assert agent._shutdown.is_set()

    def test_dead_state_in_status(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            base_dir=tmp_path,
        )
        agent._state = AgentState.DEAD
        status = agent.status()
        assert status["state"] == "dead"
