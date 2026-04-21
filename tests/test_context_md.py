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


from pathlib import Path
from unittest.mock import MagicMock
from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.llm.interface import ChatInterface, TextBlock


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session
    return svc


class TestFlushContextToPrompt:
    def _make_agent(self, tmp_path):
        return BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )

    def test_flush_rebuilds_context_from_jsonl(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()

        # Simulate a completed turn — append to JSONL manually
        history_dir = tmp_path / "test" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 1713600000.0},
            {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hi"}], "timestamp": 1713600001.0},
        ]
        with open(history_dir / "chat_history.jsonl", "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        agent._rebuild_context_md()

        ctx = agent._prompt_manager.read_section("context")
        assert ctx is not None
        assert "hello" in ctx
        assert "hi" in ctx
        agent.stop(timeout=2.0)

    def test_flush_wipes_chat_interface(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("hello")
        iface.add_assistant_message([TextBlock(text="hi")])

        agent._flush_context_to_prompt()

        assert agent._session.chat is None
        agent.stop(timeout=2.0)

    def test_flush_persists_context_md_to_disk(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("persist test")
        iface.add_assistant_message([TextBlock(text="done")])

        agent._flush_context_to_prompt()

        context_file = tmp_path / "test" / "system" / "context.md"
        assert context_file.exists()
        assert "persist test" in context_file.read_text()
        agent.stop(timeout=2.0)

    def test_flush_only_reads_since_last_molt(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()

        history_dir = tmp_path / "test" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "before molt"}], "timestamp": 1.0},
            {"type": "molt_boundary", "molt_count": 1, "timestamp": 2.0, "summary": "x"},
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "after molt"}], "timestamp": 3.0},
        ]
        with open(history_dir / "chat_history.jsonl", "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        agent._rebuild_context_md()

        ctx = agent._prompt_manager.read_section("context")
        assert "before molt" not in ctx
        assert "after molt" in ctx
        agent.stop(timeout=2.0)

    def test_flush_noop_when_no_jsonl(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()
        agent._rebuild_context_md()
        ctx = agent._prompt_manager.read_section("context")
        assert ctx is None
        agent.stop(timeout=2.0)


class TestStartupContextLoad:
    def test_startup_loads_context_md_into_prompt(self, tmp_path):
        work_dir = tmp_path / "test"
        work_dir.mkdir(parents=True)
        system_dir = work_dir / "system"
        system_dir.mkdir()
        (system_dir / "context.md").write_text("### user [2026-04-20T10:00:00Z]\nprevious conversation")

        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=work_dir,
        )
        ctx = agent._prompt_manager.read_section("context")
        assert ctx is not None
        assert "previous conversation" in ctx

    def test_startup_does_not_restore_chat_interface(self, tmp_path):
        work_dir = tmp_path / "test"
        work_dir.mkdir(parents=True)
        history_dir = work_dir / "history"
        history_dir.mkdir()
        entry = {"id": 0, "role": "user", "content": [{"type": "text", "text": "old msg"}], "timestamp": 1.0}
        (history_dir / "chat_history.jsonl").write_text(json.dumps(entry) + "\n")

        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=work_dir,
        )
        agent.start()
        # No session should exist — we don't restore from JSONL anymore
        assert agent._session.chat is None
        agent.stop(timeout=2.0)

    def test_startup_no_context_md_means_no_section(self, tmp_path):
        work_dir = tmp_path / "test"
        work_dir.mkdir(parents=True)
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=work_dir,
        )
        assert agent._prompt_manager.read_section("context") is None


class TestMoltClearsContext:
    def test_molt_deletes_context_section(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )
        agent.start()
        agent._prompt_manager.write_section("context", "old context content")
        context_file = tmp_path / "test" / "system" / "context.md"
        context_file.parent.mkdir(exist_ok=True)
        context_file.write_text("old context content")

        # Create a session for molt to work
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("trigger molt")

        from lingtai_kernel.intrinsics.eigen import _context_molt
        result = _context_molt(agent, {"summary": "test summary"})

        assert result["status"] == "ok"
        assert agent._prompt_manager.read_section("context") is None
        assert not context_file.exists()
        agent.stop(timeout=2.0)

    def test_molt_writes_boundary_marker(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("pre-molt msg")

        from lingtai_kernel.intrinsics.eigen import _context_molt
        _context_molt(agent, {"summary": "molt summary"})

        audit_file = tmp_path / "test" / "history" / "chat_history.jsonl"
        assert audit_file.exists()
        lines = [json.loads(l) for l in audit_file.read_text().splitlines() if l.strip()]
        boundary = lines[-1]
        assert boundary["type"] == "molt_boundary"
        assert boundary["molt_count"] == 1
        assert boundary["summary"] == "molt summary"
        agent.stop(timeout=2.0)


class TestEndToEnd:
    def test_full_lifecycle(self, tmp_path):
        """start -> message -> idle -> message -> idle -> molt -> post-molt -> restart"""
        work_dir = tmp_path / "agent"
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="e2e",
            working_dir=work_dir,
        )
        agent.start()

        # --- Turn 1 ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("first message")
        iface.add_assistant_message([TextBlock(text="first reply")])
        agent._flush_context_to_prompt()
        agent._save_chat_history()

        assert agent._session.chat is None
        ctx = agent._prompt_manager.read_section("context")
        assert "first message" in ctx
        assert "first reply" in ctx
        assert (work_dir / "system" / "context.md").exists()

        # --- Turn 2 ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("second message")
        iface.add_assistant_message([TextBlock(text="second reply")])
        agent._flush_context_to_prompt()
        agent._save_chat_history()

        ctx = agent._prompt_manager.read_section("context")
        assert "first message" in ctx
        assert "second message" in ctx

        # --- Molt ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("about to molt")
        from lingtai_kernel.intrinsics.eigen import _context_molt
        _context_molt(agent, {"summary": "I learned things"})

        assert agent._prompt_manager.read_section("context") is None
        assert not (work_dir / "system" / "context.md").exists()

        # Audit log has everything including boundary
        audit_lines = (work_dir / "history" / "chat_history.jsonl").read_text().splitlines()
        entries = [json.loads(l) for l in audit_lines if l.strip()]
        boundaries = [e for e in entries if e.get("type") == "molt_boundary"]
        assert len(boundaries) == 1
        assert boundaries[0]["molt_count"] == 1

        # --- Post-molt turn ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("new life")
        iface.add_assistant_message([TextBlock(text="fresh start")])
        agent._flush_context_to_prompt()

        ctx = agent._prompt_manager.read_section("context")
        assert "new life" in ctx
        assert "first message" not in ctx  # pre-molt content gone from context

        # --- Restart simulation ---
        agent.stop(timeout=2.0)
        agent2 = BaseAgent(
            service=make_mock_service(),
            agent_name="e2e",
            working_dir=work_dir,
        )
        ctx2 = agent2._prompt_manager.read_section("context")
        assert "new life" in ctx2
        assert "first message" not in ctx2
