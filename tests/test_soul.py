"""Tests for stoai_kernel.intrinsics.soul."""
from unittest.mock import MagicMock

from stoai_kernel.intrinsics import soul


def _make_mock_agent():
    agent = MagicMock()
    agent._soul_active = False
    agent._soul_delay = 120.0
    agent._soul_prompt = ""
    return agent


class TestSoulHandle:

    def test_on_requires_prompt(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on"})
        assert "error" in result

    def test_on_activates_soul(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on", "prompt": "reflect on my progress"})
        assert result["status"] == "ok"
        assert agent._soul_active is True
        assert agent._soul_prompt == "reflect on my progress"

    def test_on_with_delay(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on", "delay": 30, "prompt": "whisper"})
        assert result["status"] == "ok"
        assert agent._soul_active is True
        assert agent._soul_delay == 30.0

    def test_on_updates_prompt(self):
        """Calling on again changes the prompt."""
        agent = _make_mock_agent()
        soul.handle(agent, {"action": "on", "prompt": "first"})
        soul.handle(agent, {"action": "on", "prompt": "second"})
        assert agent._soul_prompt == "second"

    def test_off_deactivates_soul(self):
        agent = _make_mock_agent()
        agent._soul_active = True
        result = soul.handle(agent, {"action": "off"})
        assert result["status"] == "ok"
        assert agent._soul_active is False

    def test_unknown_action(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "dance"})
        assert "error" in result

    def test_delay_must_be_positive(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on", "delay": -5, "prompt": "x"})
        assert "error" in result

    def test_delay_capped_at_3600(self):
        agent = _make_mock_agent()
        soul.handle(agent, {"action": "on", "delay": 99999, "prompt": "x"})
        assert agent._soul_delay == 3600.0

    def test_empty_prompt_rejected(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on", "prompt": "   "})
        assert "error" in result


from stoai_kernel.intrinsics.soul import whisper


class TestWhisper:

    def test_whisper_returns_inner_voice_text(self):
        """whisper() clones interface, sends the agent's prompt, returns text."""
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
        agent._soul_prompt = "What am I missing?"

        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "You should check your notes."
        mock_session.send.return_value = mock_response
        agent.service.create_session.return_value = mock_session
        agent._config = MagicMock()
        agent._config.provider = None
        agent._config.model = None
        agent.service.model = "test-model"

        result = whisper(agent)
        assert result == "You should check your notes."

        # Verify create_session was called with no tools
        call_kwargs = agent.service.create_session.call_args
        assert call_kwargs.kwargs.get("tools") is None

        # Verify the agent's prompt was sent
        sent_msg = mock_session.send.call_args[0][0]
        assert "What am I missing?" in sent_msg

    def test_whisper_returns_none_when_no_chat(self):
        """whisper() returns None if agent has no active chat."""
        agent = MagicMock()
        agent._chat = None
        result = whisper(agent)
        assert result is None

    def test_whisper_returns_none_on_empty_interface(self):
        """whisper() returns None if interface has no conversation entries."""
        from stoai_kernel.llm.interface import ChatInterface

        agent = MagicMock()
        iface = ChatInterface()
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat
        result = whisper(agent)
        assert result is None

    def test_whisper_returns_none_on_api_error(self):
        """whisper() returns None if the LLM call fails."""
        from stoai_kernel.llm.interface import ChatInterface, TextBlock

        agent = MagicMock()
        iface = ChatInterface()
        iface.add_user_message("Hello")
        iface.add_assistant_message([TextBlock(text="Hi")])

        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat
        agent._build_system_prompt = MagicMock(return_value="test")
        agent._soul_prompt = "reflect"
        agent._config = MagicMock()
        agent._config.provider = None
        agent._config.model = None
        agent.service.model = "test-model"
        agent.service.create_session.side_effect = RuntimeError("API down")

        result = whisper(agent)
        assert result is None
