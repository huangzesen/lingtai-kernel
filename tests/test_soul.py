"""Tests for stoai_kernel.intrinsics.soul."""
from unittest.mock import MagicMock

from stoai_kernel.intrinsics import soul


def _make_mock_agent():
    agent = MagicMock()
    agent._soul_flow = True
    agent._soul_delay = 120.0
    agent._soul_prompt = ""
    agent._soul_oneshot = False
    return agent


class TestSoulHandle:

    def test_inquiry_sets_oneshot(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "What am I missing?"})
        assert result["status"] == "ok"
        assert result["mode"] == "inquiry"
        assert agent._soul_prompt == "What am I missing?"
        assert agent._soul_oneshot is True

    def test_inquiry_requires_text(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "inquiry"})
        assert "error" in result

    def test_inquiry_rejects_empty(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "   "})
        assert "error" in result

    def test_delay_action_updates_delay(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "delay", "delay": 30})
        assert result["status"] == "ok"
        assert result["delay"] == 30.0
        assert agent._soul_delay == 30.0

    def test_delay_must_be_positive(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "delay", "delay": -5})
        assert "error" in result

    def test_delay_allows_very_large_values(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "delay", "delay": 999999})
        assert result["status"] == "ok"
        assert agent._soul_delay == 999999.0

    def test_non_numeric_delay_rejected(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "delay", "delay": "fast"})
        assert "error" in result

    def test_none_delay_rejected(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "delay", "delay": None})
        assert "error" in result

    def test_unknown_action(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on"})
        assert "error" in result

    def test_inquiry_works_without_flow(self):
        """Inquiry is independent of flow mode."""
        agent = _make_mock_agent()
        agent._soul_flow = False
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "Am I stuck?"})
        assert result["status"] == "ok"
        assert agent._soul_oneshot is True


from stoai_kernel.intrinsics.soul import whisper


class TestWhisper:

    def _make_whisper_agent(self, soul_prompt=""):
        from stoai_kernel.llm.interface import ChatInterface, TextBlock

        agent = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are a test agent.")
        iface.add_user_message("Hello")
        iface.add_assistant_message([TextBlock(text="Hi there!")])

        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat
        agent._build_system_prompt = MagicMock(return_value="You are a test agent.")
        agent._soul_prompt = soul_prompt

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "You should check your notes."
        mock_session.send.return_value = mock_response
        agent.service.create_session.return_value = mock_session
        agent._config = MagicMock()
        agent._config.language = "en"
        agent._config.provider = None
        agent._config.model = None
        agent.service.model = "test-model"

        return agent, mock_session

    def test_whisper_flow_mode(self):
        """Flow mode: [Current time: ...] + ponder word."""
        agent, mock_session = self._make_whisper_agent(soul_prompt="")
        result = whisper(agent)
        assert result == "You should check your notes."
        sent_msg = mock_session.send.call_args[0][0]
        assert "[Current time:" in sent_msg
        assert "Ponder." in sent_msg

    def test_whisper_inquiry_mode(self):
        """Inquiry mode: [Current time: ...] + question."""
        agent, mock_session = self._make_whisper_agent(soul_prompt="What am I missing?")
        result = whisper(agent)
        assert result == "You should check your notes."
        sent_msg = mock_session.send.call_args[0][0]
        assert "[Current time:" in sent_msg
        assert "What am I missing?" in sent_msg

    def test_whisper_same_pattern_flow_and_inquiry(self):
        """Flow and inquiry use the same [Current time: ...] pattern."""
        import re
        ts_pattern = re.compile(r"^\[Current time: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\n\n.+", re.DOTALL)

        agent_flow, session_flow = self._make_whisper_agent(soul_prompt="")
        whisper(agent_flow)
        msg_flow = session_flow.send.call_args[0][0]

        agent_inq, session_inq = self._make_whisper_agent(soul_prompt="Am I stuck?")
        whisper(agent_inq)
        msg_inq = session_inq.send.call_args[0][0]

        assert ts_pattern.match(msg_flow)
        assert ts_pattern.match(msg_inq)

    def test_whisper_flow_mode_chinese(self):
        """Chinese config uses Chinese ponder word and timestamp."""
        agent, mock_session = self._make_whisper_agent(soul_prompt="")
        agent._config.language = "zh"
        result = whisper(agent)
        sent_msg = mock_session.send.call_args[0][0]
        assert "当前时间" in sent_msg
        assert "沉思。" in sent_msg

    def test_whisper_returns_none_when_no_chat(self):
        agent = MagicMock()
        agent._chat = None
        result = whisper(agent)
        assert result is None

    def test_whisper_returns_none_on_empty_interface(self):
        from stoai_kernel.llm.interface import ChatInterface

        agent = MagicMock()
        iface = ChatInterface()
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat
        result = whisper(agent)
        assert result is None

    def test_whisper_returns_none_on_api_error(self):
        from stoai_kernel.llm.interface import ChatInterface, TextBlock

        agent = MagicMock()
        iface = ChatInterface()
        iface.add_user_message("Hello")
        iface.add_assistant_message([TextBlock(text="Hi")])

        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat
        agent._build_system_prompt = MagicMock(return_value="test")
        agent._soul_prompt = ""
        agent._config = MagicMock()
        agent._config.language = "en"
        agent._config.provider = None
        agent._config.model = None
        agent.service.model = "test-model"
        agent.service.create_session.side_effect = RuntimeError("API down")

        result = whisper(agent)
        assert result is None


import threading
import time

from stoai_kernel.config import AgentConfig


def make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


class TestSoulTimer:

    def test_soul_attributes_initialized_flow_on(self, tmp_path):
        """BaseAgent with default config has flow enabled."""
        from stoai_kernel import BaseAgent
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        assert agent._soul_flow is True
        assert agent._soul_delay == 120.0
        assert agent._soul_prompt == ""
        assert agent._soul_oneshot is False
        assert agent._soul_timer is None

    def test_soul_attributes_initialized_flow_off(self, tmp_path):
        """BaseAgent with flow=False."""
        from stoai_kernel import BaseAgent
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            config=AgentConfig(flow=False),
            base_dir=tmp_path,
        )
        assert agent._soul_flow is False

    def test_soul_timer_starts_on_idle_when_flow_enabled(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._soul_delay = 1.0
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is not None
        assert agent._soul_timer.is_alive()
        agent._soul_timer.cancel()

    def test_soul_timer_does_not_start_when_flow_disabled(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            config=AgentConfig(flow=False),
            base_dir=tmp_path,
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is None

    def test_soul_timer_starts_on_idle_for_inquiry(self, tmp_path):
        """Inquiry fires timer even when flow is off."""
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            config=AgentConfig(flow=False),
            base_dir=tmp_path,
        )
        agent._soul_oneshot = True
        agent._soul_prompt = "Am I stuck?"
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is not None
        agent._soul_timer.cancel()

    def test_soul_timer_cancelled_on_wake(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._soul_delay = 300.0
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is not None
        agent._set_state(AgentState.ACTIVE, reason="new mail")
        assert agent._soul_timer is None

    def test_flow_delay_from_config(self, tmp_path):
        """flow_delay in config sets initial _soul_delay."""
        from stoai_kernel import BaseAgent
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            config=AgentConfig(flow_delay=60.0),
            base_dir=tmp_path,
        )
        assert agent._soul_delay == 60.0

    def test_flow_delay_clamped_to_min(self, tmp_path):
        """flow_delay below 1 is clamped to 1."""
        from stoai_kernel import BaseAgent
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            config=AgentConfig(flow_delay=-10.0),
            base_dir=tmp_path,
        )
        assert agent._soul_delay == 1.0


class TestSoulCleanup:

    def test_stop_cancels_soul_timer(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._soul_delay = 300.0
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is not None
        agent.stop()
        assert agent._soul_timer is None


class TestSoulIntegration:

    def test_flow_injects_inner_voice_into_inbox(self, tmp_path):
        """Flow mode: timer fires, whisper runs, inbox gets message."""
        from stoai_kernel import BaseAgent, AgentState
        from stoai_kernel.llm.interface import ChatInterface, TextBlock

        svc = make_mock_service()
        agent = BaseAgent(
            agent_name="test",
            service=svc,
            base_dir=tmp_path,
        )

        iface = ChatInterface()
        iface.add_system("You are a test agent.")
        iface.add_user_message("Research quantum computing.")
        iface.add_assistant_message([TextBlock(text="I'll start researching.")])

        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        mock_soul_session = MagicMock()
        mock_soul_response = MagicMock()
        mock_soul_response.text = "Have you considered the energy implications?"
        mock_soul_session.send.return_value = mock_soul_response
        svc.create_session.return_value = mock_soul_session

        agent._soul_delay = 0.1

        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")

        deadline = time.monotonic() + 2.0
        while agent.inbox.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not agent.inbox.empty()
        msg = agent.inbox.get_nowait()
        assert "[inner voice]" in msg.content
        assert "energy implications" in msg.content
        assert msg.sender == "soul"

        # Flow stays enabled (it's immutable)
        assert agent._soul_flow is True

        # Verify soul.jsonl was written
        import json
        soul_file = tmp_path / "test" / "system" / "soul.jsonl"
        assert soul_file.is_file()
        entry = json.loads(soul_file.read_text().strip())
        assert entry["inquiry"] == ""
        assert entry["voice"] == "Have you considered the energy implications?"

    def test_inquiry_clears_after_firing(self, tmp_path):
        """Inquiry mode: fires once, then oneshot clears (flow unaffected)."""
        from stoai_kernel import BaseAgent, AgentState
        from stoai_kernel.llm.interface import ChatInterface, TextBlock

        svc = make_mock_service()
        agent = BaseAgent(
            agent_name="test",
            service=svc,
            config=AgentConfig(flow=False),
            base_dir=tmp_path,
        )

        iface = ChatInterface()
        iface.add_system("You are a test agent.")
        iface.add_user_message("Hello")
        iface.add_assistant_message([TextBlock(text="Hi")])

        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        mock_soul_session = MagicMock()
        mock_soul_response = MagicMock()
        mock_soul_response.text = "Consider the edge cases."
        mock_soul_session.send.return_value = mock_soul_response
        svc.create_session.return_value = mock_soul_session

        agent._soul_delay = 0.1
        agent._soul_prompt = "What am I missing?"
        agent._soul_oneshot = True

        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")

        deadline = time.monotonic() + 2.0
        while agent.inbox.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not agent.inbox.empty()
        msg = agent.inbox.get_nowait()
        assert "[inner voice]" in msg.content

        # Inquiry clears after firing
        assert agent._soul_oneshot is False
        assert agent._soul_prompt == ""

    def test_empty_whisper_does_not_inject(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState

        svc = make_mock_service()
        agent = BaseAgent(
            agent_name="test",
            service=svc,
            base_dir=tmp_path,
        )
        agent._soul_delay = 0.1

        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")

        time.sleep(0.3)
        assert agent.inbox.empty()

    def test_soul_timer_not_started_during_shutdown(self, tmp_path):
        from stoai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            agent_name="test",
            service=make_mock_service(),
            base_dir=tmp_path,
        )
        agent._soul_delay = 1.0
        agent._shutdown.set()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is None
