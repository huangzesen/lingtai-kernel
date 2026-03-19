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
