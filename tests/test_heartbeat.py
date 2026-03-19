"""Tests for heartbeat — always-on agent health monitor."""
import time
from unittest.mock import MagicMock


def make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


class TestHeartbeatInit:

    def test_heartbeat_counter_initialized(self, tmp_path):
        from stoai_kernel import BaseAgent
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        assert agent._heartbeat == 0
        assert agent._heartbeat_thread is None
        assert agent._cpr_start is None
        assert agent._sleep_reason == "idle"

    def test_heartbeat_in_status(self, tmp_path):
        from stoai_kernel import BaseAgent
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._heartbeat = 42
        status = agent.status()
        assert status["heartbeat"] == 42


class TestSleepReason:

    def test_sleep_reason_idle(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="idle")
        assert agent._sleep_reason == "idle"

    def test_sleep_reason_stuck(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="stuck")
        assert agent._sleep_reason == "stuck"

    def test_sleep_reason_error(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="error")
        assert agent._sleep_reason == "error"


class TestHeartbeatBeating:

    def test_heartbeat_increments(self, tmp_path):
        from stoai_kernel import BaseAgent
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        time.sleep(2.5)
        agent._stop_heartbeat()
        assert agent._heartbeat >= 2

    def test_no_cpr_on_idle(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="idle")

        time.sleep(2.0)
        agent._stop_heartbeat()
        assert agent.inbox.empty()
        assert agent._cpr_start is None


class TestHeartbeatCPR:

    def test_cpr_on_stuck(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="stuck")

        deadline = time.monotonic() + 3.0
        while agent.inbox.empty() and time.monotonic() < deadline:
            time.sleep(0.1)

        agent._stop_heartbeat()
        assert not agent.inbox.empty()
        msg = agent.inbox.get_nowait()
        assert "CPR" in msg.content
        assert "stuck" in msg.content
        assert msg.sender == "system"

    def test_cpr_on_error(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="error")

        deadline = time.monotonic() + 3.0
        while agent.inbox.empty() and time.monotonic() < deadline:
            time.sleep(0.1)

        agent._stop_heartbeat()
        assert not agent.inbox.empty()
        msg = agent.inbox.get_nowait()
        assert "CPR" in msg.content
        assert "error" in msg.content

    def test_cpr_resets_on_recovery(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._cpr_start = time.monotonic()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="idle")

        agent._start_heartbeat()
        time.sleep(1.5)
        agent._stop_heartbeat()
        assert agent._cpr_start is None


class TestHeartbeatDead:

    def test_cpr_timeout_triggers_dead_and_shutdown(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.SLEEPING, reason="stuck")
        # Simulate CPR started 21 minutes ago (exceeds 20 min window)
        agent._cpr_start = time.monotonic() - 1260

        agent._start_heartbeat()
        time.sleep(1.5)
        agent._stop_heartbeat()

        assert agent._state == AgentState.DEAD
        assert agent._shutdown.is_set()

    def test_dead_state_in_status(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._state = AgentState.DEAD
        status = agent.status()
        assert status["state"] == "dead"
