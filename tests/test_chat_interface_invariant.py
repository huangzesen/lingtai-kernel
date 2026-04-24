"""Tests for ChatInterface tool-pairing invariant.

DeepSeek V4 and strict OpenAI reject chat-completions requests where an
assistant message with tool_calls is not immediately followed by matching
tool messages. These tests verify the canonical ChatInterface enforces
that invariant at construction time.
"""
from __future__ import annotations

import pytest

from lingtai_kernel.llm.interface import (
    ChatInterface,
    PendingToolCallsError,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _iface_with_pending_tool_calls() -> ChatInterface:
    """Build an interface whose tail is assistant[tool_calls] with no results."""
    iface = ChatInterface()
    iface.add_system("system prompt")
    iface.add_user_message("hi")
    iface.add_assistant_message(
        [
            TextBlock(text="checking"),
            ToolCallBlock(id="call_A", name="noop", args={}),
        ],
    )
    return iface


class TestHasPendingToolCalls:
    def test_false_on_empty_interface(self):
        iface = ChatInterface()
        assert iface.has_pending_tool_calls() is False

    def test_false_when_tail_is_system(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        assert iface.has_pending_tool_calls() is False

    def test_false_when_tail_is_user(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("hi")
        assert iface.has_pending_tool_calls() is False

    def test_false_when_tail_is_plain_assistant(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello")])
        assert iface.has_pending_tool_calls() is False

    def test_true_when_tail_is_assistant_with_tool_calls(self):
        iface = _iface_with_pending_tool_calls()
        assert iface.has_pending_tool_calls() is True

    def test_false_after_tool_results_appended(self):
        iface = _iface_with_pending_tool_calls()
        iface.add_tool_results([ToolResultBlock(id="call_A", name="noop", content="done")])
        assert iface.has_pending_tool_calls() is False


class TestClosePendingToolCalls:
    def test_noop_on_empty_interface(self):
        iface = ChatInterface()
        iface.close_pending_tool_calls("test")
        assert len(iface.entries) == 0

    def test_noop_when_tail_clean(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("hi")
        before = len(iface.entries)
        iface.close_pending_tool_calls("test")
        assert len(iface.entries) == before

    def test_synthesizes_results_for_pending(self):
        iface = ChatInterface()
        iface.add_system("prompt")
        iface.add_user_message("go")
        iface.add_assistant_message(
            [
                TextBlock(text="running"),
                ToolCallBlock(id="call_A", name="tool1", args={}),
                ToolCallBlock(id="call_B", name="tool2", args={"k": 1}),
            ],
        )
        assert iface.has_pending_tool_calls() is True
        iface.close_pending_tool_calls("network timeout")
        # Now tail should be a user entry with two ToolResultBlocks.
        assert iface.has_pending_tool_calls() is False
        tail = iface.entries[-1]
        assert tail.role == "user"
        assert len(tail.content) == 2
        result_A, result_B = tail.content
        assert isinstance(result_A, ToolResultBlock)
        assert result_A.id == "call_A"
        assert result_A.name == "tool1"
        assert "aborted" in result_A.content
        assert "network timeout" in result_A.content
        assert isinstance(result_B, ToolResultBlock)
        assert result_B.id == "call_B"
        assert result_B.name == "tool2"

    def test_idempotent(self):
        iface = _iface_with_pending_tool_calls()
        iface.close_pending_tool_calls("r1")
        entries_after_first = len(iface.entries)
        iface.close_pending_tool_calls("r2")
        # Second call is a no-op because tail is now clean.
        assert len(iface.entries) == entries_after_first
