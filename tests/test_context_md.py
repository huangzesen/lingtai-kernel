"""Tests for context.md system prompt cache overhaul."""
import pytest
from lingtai_kernel.prompt import SystemPromptManager


class TestContextSection:
    def test_context_is_last_section(self):
        pm = SystemPromptManager()
        pm.write_section("pad", "my notes")
        pm.write_section("context", "### user [2026-04-20T10:00:00Z]\nhello")
        rendered = pm.render()
        pad_pos = rendered.index("## pad")
        context_pos = rendered.index("## context")
        assert context_pos > pad_pos

    def test_context_empty_not_rendered(self):
        pm = SystemPromptManager()
        pm.write_section("pad", "my notes")
        rendered = pm.render()
        assert "## context" not in rendered

    def test_context_deleted_disappears(self):
        pm = SystemPromptManager()
        pm.write_section("context", "some content")
        pm.delete_section("context")
        rendered = pm.render()
        assert "## context" not in rendered


import json
from lingtai_kernel.context_serializer import serialize_context_md


class TestSerializeContextMd:
    def test_basic_user_assistant(self):
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 1713600000.0},
            {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hi there"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "### user [" in md
        assert "### assistant [" in md
        assert "hello" in md
        assert "hi there" in md

    def test_thinking_blocks_included(self):
        entries = [
            {"id": 0, "role": "assistant", "content": [
                {"type": "thinking", "text": "let me think about this"},
                {"type": "text", "text": "here's my answer"},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert "let me think about this" in md
        assert "<thinking>" in md
        assert "here's my answer" in md

    def test_tool_call_full_args(self):
        long_args = {"content": "x" * 5000}
        entries = [
            {"id": 0, "role": "assistant", "content": [
                {"type": "tool_call", "id": "tc1", "name": "write", "args": long_args},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert "x" * 5000 in md

    def test_tool_result_full_content(self):
        long_result = "line\n" * 2000
        entries = [
            {"id": 0, "role": "user", "content": [
                {"type": "tool_result", "id": "tc1", "name": "read", "content": long_result},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert long_result in md

    def test_system_entries_included(self):
        entries = [
            {"id": 0, "role": "system", "system": "You are a helpful agent.", "timestamp": 1713600000.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "You are a helpful agent." in md
        assert "### system [" in md

    def test_molt_boundary_skipped(self):
        entries = [
            {"type": "molt_boundary", "molt_count": 1, "timestamp": 1713600000.0, "summary": "old stuff"},
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "fresh start"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "molt_boundary" not in md
        assert "old stuff" not in md
        assert "fresh start" in md

    def test_empty_entries(self):
        md = serialize_context_md([])
        assert md == ""

    def test_timestamp_format(self):
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        # Should have ISO format timestamp
        assert "2024-04-20T" in md

    def test_turn_separator(self):
        """System entries mark turn boundaries — render --- between turns."""
        entries = [
            {"id": 0, "role": "system", "system": "prompt v1", "timestamp": 1.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "turn 1"}], "timestamp": 2.0},
            {"id": 2, "role": "assistant", "content": [{"type": "text", "text": "reply 1"}], "timestamp": 3.0},
            {"id": 0, "role": "system", "system": "prompt v2", "timestamp": 4.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "turn 2"}], "timestamp": 5.0},
            {"id": 2, "role": "assistant", "content": [{"type": "text", "text": "reply 2"}], "timestamp": 6.0},
        ]
        md = serialize_context_md(entries)
        assert "turn 1" in md
        assert "turn 2" in md
        assert "\n---\n" in md or "\n\n---\n\n" in md
