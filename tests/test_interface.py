"""Tests for ChatInterface.pop_orphan_tool_call()."""
from __future__ import annotations

from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_pop_orphan_tool_call_removes_trailing_assistant_with_tool_calls():
    """Trailing assistant entry with ToolCallBlocks should be popped."""
    iface = ChatInterface()
    iface.add_system("prompt")
    iface.add_user_message("hello")
    iface.add_assistant_message([
        TextBlock(text="Let me check."),
        ToolCallBlock(id="tc1", name="bash", args={"command": "ls"}),
    ])
    assert len(iface.entries) == 3

    removed = iface.pop_orphan_tool_call()

    assert removed is True
    assert len(iface.entries) == 2  # system, user


def test_pop_orphan_tool_call_also_removes_trailing_tool_results():
    """If tool results follow the orphan assistant, pop both."""
    iface = ChatInterface()
    iface.add_system("prompt")
    iface.add_user_message("hello")
    iface.add_assistant_message([
        ToolCallBlock(id="tc1", name="bash", args={"command": "ls"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="tc1", name="bash", content="file.txt"),
    ])
    assert len(iface.entries) == 4

    removed = iface.pop_orphan_tool_call()

    assert removed is True
    assert len(iface.entries) == 2  # system, user


def test_pop_orphan_tool_call_noop_when_clean():
    """No orphan -- should not pop anything."""
    iface = ChatInterface()
    iface.add_system("prompt")
    iface.add_user_message("hello")
    iface.add_assistant_message([TextBlock(text="Hi there!")])

    removed = iface.pop_orphan_tool_call()

    assert removed is False
    assert len(iface.entries) == 3


def test_pop_orphan_tool_call_noop_on_empty():
    """Empty interface -- should not crash."""
    iface = ChatInterface()

    removed = iface.pop_orphan_tool_call()

    assert removed is False
