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
        """`role: user` renders as `### Input`, `role: assistant` as `### You`.
        The user/assistant API vocabulary contradicts lingtai's model —
        users in lingtai are humans who reach the agent via email, never
        by typing into the API's user role. Rename at serialization so
        the agent's own history reads as autobiography, not a chat log."""
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 1713600000.0},
            {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hi there"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "### Input [" in md
        assert "### You [" in md
        # Old labels must not appear.
        assert "### user [" not in md
        assert "### assistant [" not in md
        assert "hello" in md
        assert "hi there" in md

    def test_thinking_blocks_are_dropped(self):
        """Thinking blocks are the agent's private scratchpad — not durable
        history. Keep the text response, drop the reasoning trail."""
        entries = [
            {"id": 0, "role": "assistant", "content": [
                {"type": "thinking", "text": "let me think about this"},
                {"type": "text", "text": "here's my answer"},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert "let me think about this" not in md
        assert "<thinking>" not in md
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
        # All 2000 lines must be preserved (no truncation). Each is
        # indented by the serializer, so we check line count rather than
        # raw-string containment.
        long_result = "line\n" * 2000
        entries = [
            {"id": 0, "role": "user", "content": [
                {"type": "tool_result", "id": "tc1", "name": "read", "content": long_result},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert md.count("    line") == 2000

    def test_tool_call_rendered_as_diamond_narrative(self):
        """Past tool calls must NOT use any syntax that mimics a provider's
        tool-call protocol (no [tool_use:...], no <tool_use> tags, no
        name(args) function syntax, no ```json fences). They must use the
        ◆ past-tense narrative format that no provider trains the model
        to emit."""
        entries = [
            {"id": 0, "role": "assistant", "content": [
                {"type": "tool_call", "id": "tc1", "name": "web_search",
                 "args": {"query": "伊朗局势"}},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        # No provider-native tool-call syntaxes.
        assert "[tool_use:" not in md
        assert "<tool_use" not in md
        assert "<invoke" not in md
        assert "<parameter" not in md
        assert "```json" not in md
        assert "web_search(" not in md
        # The diamond + narrative format.
        assert "◆ called tool `web_search` with arguments:" in md
        assert "伊朗局势" in md

    def test_tool_call_no_args_renders_compact(self):
        """A tool call with no args drops the 'with arguments' tail and
        ends with a period — no empty JSON block."""
        entries = [
            {"id": 0, "role": "assistant", "content": [
                {"type": "tool_call", "id": "tc1", "name": "refresh", "args": {}},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert "◆ called tool `refresh`." in md
        assert "with arguments" not in md

    def test_tool_result_rendered_as_diamond_narrative(self):
        entries = [
            {"id": 0, "role": "user", "content": [
                {"type": "tool_result", "id": "tc1", "name": "email",
                 "content": "ok: delivered"},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert "[tool_result(" not in md
        assert "<tool_result" not in md
        assert "```" not in md
        assert "◆ tool `email` returned:" in md
        assert "ok: delivered" in md

    def test_tool_result_body_is_indented_not_fenced(self):
        """Tool result bodies are indented 4 spaces (markdown code block)
        not wrapped in ``` fences — fences look too much like an
        invocation block."""
        entries = [
            {"id": 0, "role": "user", "content": [
                {"type": "tool_result", "id": "tc1", "name": "read",
                 "content": "line1\nline2\nline3"},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        # Each content line is indented 4 spaces.
        assert "    line1" in md
        assert "    line2" in md
        assert "    line3" in md
        assert "```" not in md

    def test_banner_present(self):
        """Context.md opens with a banner marking it as serialized history,
        so the LLM does not confuse it with the live conversation."""
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1.0},
        ]
        md = serialize_context_md(entries)
        assert md.startswith("# Chat History (serialized)")

    def test_system_entries_skipped(self):
        # System entries in chat_history.jsonl are audit records of the full
        # assembled system prompt. Re-serializing them into context.md would
        # nest the system prompt recursively inside itself (context.md is
        # injected as the ## context section of the system prompt).
        entries = [
            {"id": 0, "role": "system", "system": "You are a helpful agent.", "timestamp": 1713600000.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "You are a helpful agent." not in md
        assert "### system [" not in md
        assert "hi" in md

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

    def test_system_entries_do_not_leak_between_turns(self):
        """System entries between user/assistant turns are audit-only — they
        must not appear in the rendered context, and their content must not
        smuggle itself into the output."""
        entries = [
            {"id": 0, "role": "system", "system": "prompt v1 LEAK", "timestamp": 1.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "turn 1"}], "timestamp": 2.0},
            {"id": 2, "role": "assistant", "content": [{"type": "text", "text": "reply 1"}], "timestamp": 3.0},
            {"id": 0, "role": "system", "system": "prompt v2 LEAK", "timestamp": 4.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "turn 2"}], "timestamp": 5.0},
            {"id": 2, "role": "assistant", "content": [{"type": "text", "text": "reply 2"}], "timestamp": 6.0},
        ]
        md = serialize_context_md(entries)
        assert "turn 1" in md
        assert "turn 2" in md
        assert "reply 1" in md
        assert "reply 2" in md
        assert "LEAK" not in md
        assert "### system [" not in md


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

    def test_append_chat_audit_is_idempotent(self, tmp_path):
        """Back-to-back calls with no new interface activity are a no-op —
        the second call must not re-write the same entries."""
        agent = self._make_agent(tmp_path)
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("only msg")
        iface.add_assistant_message([TextBlock(text="only reply")])

        agent._append_chat_audit()
        jsonl = tmp_path / "test" / "history" / "chat_history.jsonl"
        first_count = len([l for l in jsonl.read_text().splitlines() if l.strip()])

        agent._append_chat_audit()  # no new entries — must no-op
        second_count = len([l for l in jsonl.read_text().splitlines() if l.strip()])

        assert second_count == first_count
        assert first_count == len(agent._session.get_chat_state().get("messages") or [])
        agent.stop(timeout=2.0)

    def test_append_chat_audit_writes_only_new_entries(self, tmp_path):
        """Adding interface entries and appending again increments jsonl by
        exactly the number of new entries — no re-dumps, no duplicates."""
        agent = self._make_agent(tmp_path)
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface

        iface.add_user_message("first")
        iface.add_assistant_message([TextBlock(text="first reply")])
        agent._append_chat_audit()
        jsonl = tmp_path / "test" / "history" / "chat_history.jsonl"
        count_after_turn1 = len([l for l in jsonl.read_text().splitlines() if l.strip()])

        iface.add_user_message("second")
        iface.add_assistant_message([TextBlock(text="second reply")])
        agent._append_chat_audit()
        lines_after_turn2 = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]

        # Added 2 new interface entries, so jsonl grew by exactly 2.
        assert len(lines_after_turn2) == count_after_turn1 + 2
        # Every entry in jsonl equals the corresponding interface entry.
        interface_messages = agent._session.get_chat_state().get("messages") or []
        assert lines_after_turn2 == interface_messages
        agent.stop(timeout=2.0)

    def test_watermark_resets_after_flush(self, tmp_path):
        """When _flush_context_to_prompt wipes the ChatInterface, the next
        session's entries must be treated as new (watermark = 0), so jsonl
        grows by the new session's full interface length — not from the
        middle of a stale watermark."""
        agent = self._make_agent(tmp_path)
        agent.start()

        # Session 1
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("turn 1")
        iface.add_assistant_message([TextBlock(text="reply 1")])
        agent._flush_context_to_prompt()

        assert agent._chat_audit_watermark == 0
        jsonl = tmp_path / "test" / "history" / "chat_history.jsonl"
        count_after_session1 = len([l for l in jsonl.read_text().splitlines() if l.strip()])

        # Session 2 — fresh interface; its full contents are new to jsonl.
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("turn 2")
        iface.add_assistant_message([TextBlock(text="reply 2")])
        session2_interface_len = len(agent._session.get_chat_state().get("messages") or [])
        agent._flush_context_to_prompt()

        count_after_session2 = len([l for l in jsonl.read_text().splitlines() if l.strip()])
        assert count_after_session2 == count_after_session1 + session2_interface_len
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

    def test_molt_archives_pre_molt_history(self, tmp_path):
        """On molt, chat_history.jsonl is moved into chat_history_archive.jsonl
        with a molt_boundary separator. The current jsonl starts empty for the
        new molt. Pre-molt interface entries that have not yet been written
        to jsonl must be flushed by molt itself — otherwise they'd vanish."""
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("pre-molt msg")
        # Deliberately do NOT call _append_chat_audit — molt must flush on its own.

        from lingtai_kernel.intrinsics.eigen import _context_molt
        _context_molt(agent, {"summary": "molt summary"})

        current = tmp_path / "test" / "history" / "chat_history.jsonl"
        archive = tmp_path / "test" / "history" / "chat_history_archive.jsonl"

        # Current jsonl is gone (fresh molt starts empty).
        assert not current.exists()

        # Archive contains the pre-molt entries + the boundary marker.
        assert archive.exists()
        lines = [json.loads(l) for l in archive.read_text().splitlines() if l.strip()]
        boundary = lines[-1]
        assert boundary["type"] == "molt_boundary"
        assert boundary["molt_count"] == 1
        assert boundary["summary"] == "molt summary"
        # Pre-molt user message is preserved in the archive.
        assert any(
            e.get("role") == "user"
            and any(b.get("text") == "pre-molt msg" for b in e.get("content", []))
            for e in lines
        )
        agent.stop(timeout=2.0)

    def test_molt_concatenates_multiple_molts_in_archive(self, tmp_path):
        """Two successive molts append to the archive, preserving both
        boundary markers in order."""
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )
        agent.start()
        from lingtai_kernel.intrinsics.eigen import _context_molt

        # Molt 1
        agent._session.ensure_session()
        agent._session.chat.interface.add_user_message("molt 1 body")
        _context_molt(agent, {"summary": "first molt"})

        # Molt 2
        agent._session.ensure_session()
        agent._session.chat.interface.add_user_message("molt 2 body")
        _context_molt(agent, {"summary": "second molt"})

        archive = tmp_path / "test" / "history" / "chat_history_archive.jsonl"
        lines = [json.loads(l) for l in archive.read_text().splitlines() if l.strip()]
        boundaries = [e for e in lines if e.get("type") == "molt_boundary"]
        assert [b["molt_count"] for b in boundaries] == [1, 2]
        assert [b["summary"] for b in boundaries] == ["first molt", "second molt"]
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

        # Current jsonl is gone; archive holds the pre-molt history + boundary.
        assert not (work_dir / "history" / "chat_history.jsonl").exists()
        archive_lines = (work_dir / "history" / "chat_history_archive.jsonl").read_text().splitlines()
        entries = [json.loads(l) for l in archive_lines if l.strip()]
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
