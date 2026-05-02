"""Tests for lingtai_kernel.intrinsics.soul.

After the past-self-consultation refactor, this file covers only:
- The agent-callable surface (``handle``): inquiry action, flow rejection,
  unknown-action error.
- The wall-clock soul timer (``_start_soul_timer`` / ``_cancel_soul_timer``)
  that drives consultation cadence.

The legacy diary+mirror-session machinery (``soul_flow``,
``_collect_new_diary``, ``_ensure_soul_session``, ``_save_soul_session``,
``_trim_soul_session``, ``reset_soul_session``, ``enqueue_flow_voice``,
``_soul_history_path``, ``_soul_cursor_path``) has been removed; tests for
it are gone with it. The new mechanism is covered in
``tests/test_soul_consultation.py``.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.intrinsics import soul


def _make_mock_agent():
    """Tiny mock for direct ``handle`` calls — no real LLM, no real chat."""
    agent = MagicMock()
    agent._soul_delay = 120.0
    return agent


def _make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


# ---------------------------------------------------------------------------
# soul.handle — agent-callable surface
# ---------------------------------------------------------------------------


class TestSoulHandle:

    def test_inquiry_returns_voice(self):
        agent = _make_mock_agent()
        agent._config.retry_timeout = 30.0
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "What am I missing?"})
        assert result["status"] == "ok"
        assert "voice" in result

    def test_inquiry_requires_text(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "inquiry"})
        assert "error" in result

    def test_inquiry_rejects_empty(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "   "})
        assert "error" in result

    def test_flow_action_rejected_manually(self):
        """flow fires mechanically — agent cannot invoke it directly."""
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "flow"})
        assert "error" in result
        assert "cannot be invoked manually" in result["error"]

    def test_unknown_action_returns_error(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on"})
        assert "error" in result

    def test_inquiry_works_with_large_delay(self):
        """Inquiry is independent of soul_delay value — no timer interaction."""
        agent = _make_mock_agent()
        agent._soul_delay = 999999.0
        agent._config.retry_timeout = 30.0
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "Am I stuck?"})
        assert result["status"] == "ok"
        assert "voice" in result


# ---------------------------------------------------------------------------
# soul.get_schema — public schema shape
# ---------------------------------------------------------------------------


class TestSoulSchema:

    def test_schema_exposes_three_actions(self):
        schema = soul.get_schema("en")
        # Three actions are agent-visible: inquiry (manual self-Q&A),
        # flow (mechanical, fires only on the wall clock / turn counter —
        # agent cannot invoke), and config (agent adjusts cadence + K
        # at runtime).
        assert schema["properties"]["action"]["enum"] == [
            "inquiry", "flow", "config",
        ]

    def test_schema_inquiry_property_present(self):
        schema = soul.get_schema("en")
        assert "inquiry" in schema["properties"]

    def test_schema_config_properties_present(self):
        # config parameters — delay_seconds (number, min 30s),
        # consultation_interval (integer, 0 or >=5), consultation_past_count
        # (integer, [0, 5]).
        schema = soul.get_schema("en")
        assert "delay_seconds" in schema["properties"]
        assert schema["properties"]["delay_seconds"]["type"] == "number"
        assert schema["properties"]["delay_seconds"]["minimum"] == 30.0
        assert "consultation_interval" in schema["properties"]
        assert schema["properties"]["consultation_interval"]["type"] == "integer"
        assert "consultation_past_count" in schema["properties"]
        assert schema["properties"]["consultation_past_count"]["type"] == "integer"

    def test_schema_required_is_action(self):
        assert soul.get_schema("en")["required"] == ["action"]


# ---------------------------------------------------------------------------
# Soul timer — wall-clock cadence that drives _run_consultation_fire
# ---------------------------------------------------------------------------


class TestSoulTimer:

    def test_soul_attributes_initialized_default(self, tmp_path):
        """BaseAgent with default config has soul_delay=120."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        assert agent._soul_delay == 120.0
        assert agent._soul_timer is None

    def test_soul_timer_runs_perpetual_regardless_of_state(self, tmp_path):
        """Timer is not state-gated — runs from boot, perpetually rescheduling
        in _soul_whisper finally. State transitions don't kick the timer."""
        from lingtai_kernel import AgentState, BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._soul_delay = 300.0

        # State transitions must NOT touch the timer.
        agent._set_state(AgentState.ACTIVE, reason="test")
        assert agent._soul_timer is None
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is None

        # Explicit start (the path normally taken by start() at boot).
        agent._start_soul_timer()
        assert agent._soul_timer is not None
        assert agent._soul_timer.is_alive()

        # Going active does NOT cancel the timer — cadence persists.
        agent._set_state(AgentState.ACTIVE, reason="new mail")
        assert agent._soul_timer is not None

        agent._cancel_soul_timer()

    def test_soul_timer_not_started_when_shutdown(self, tmp_path):
        """_start_soul_timer is a no-op when _shutdown is set."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._soul_delay = 1.0
        agent._shutdown.set()
        agent._start_soul_timer()
        assert agent._soul_timer is None

    def test_soul_delay_from_config(self, tmp_path):
        """soul_delay in config sets initial _soul_delay."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            config=AgentConfig(soul_delay=60.0),
            working_dir=tmp_path / "test_agent",
        )
        assert agent._soul_delay == 60.0

    def test_soul_delay_clamped_to_min(self, tmp_path):
        """soul_delay below 1 is clamped to 1."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            config=AgentConfig(soul_delay=-10.0),
            working_dir=tmp_path / "test_agent",
        )
        assert agent._soul_delay == 1.0

    def test_stop_cancels_soul_timer(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._soul_delay = 300.0
        agent._start_soul_timer()
        assert agent._soul_timer is not None
        agent.stop()
        assert agent._soul_timer is None
