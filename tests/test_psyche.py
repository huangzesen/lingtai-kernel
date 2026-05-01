"""Tests for psyche capability — identity, pad, and context management."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai_kernel.base_agent import BaseAgent


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def test_psyche_setup_removes_eigen_intrinsic(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    assert "eigen" not in agent._intrinsics
    assert "psyche" in agent._tool_handlers
    agent.stop(timeout=1.0)


def test_psyche_manager_accessible(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    assert mgr is not None
    agent.stop(timeout=1.0)


def test_anima_alias_removed(tmp_path):
    """'anima' alias was removed — agent skips it (unknown capabilities are logged, not raised)."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["anima"],
    )
    assert agent.get_capability("anima") is None
    assert "anima" not in [name for name, _ in agent._capabilities]
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Character actions
# ---------------------------------------------------------------------------


def test_character_update_writes_character(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        covenant="You are helpful",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    result = mgr.handle({"object": "lingtai", "action": "update", "content": "I am a PDF specialist"})
    assert result["status"] == "ok"
    character = (agent.working_dir / "system" / "lingtai.md").read_text()
    assert character == "I am a PDF specialist"
    agent.stop(timeout=1.0)


def test_character_update_empty_clears_character(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    mgr.handle({"object": "lingtai", "action": "update", "content": "something"})
    mgr.handle({"object": "lingtai", "action": "update", "content": ""})
    character = (agent.working_dir / "system" / "lingtai.md").read_text()
    assert character == ""
    agent.stop(timeout=1.0)


def test_character_load_combines_covenant_and_character(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        covenant="You are helpful",
        capabilities=["psyche"],
    )
    agent.start()
    try:
        mgr = agent.get_capability("psyche")
        mgr.handle({"object": "lingtai", "action": "update", "content": "I specialize in PDFs"})
        mgr.handle({"object": "lingtai", "action": "load"})
        section = agent._prompt_manager.read_section("covenant")
        assert "You are helpful" in section
        assert "I specialize in PDFs" in section
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Pad edit (upgraded with files support)
# ---------------------------------------------------------------------------


def test_pad_edit_content_only(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    result = mgr.handle({"object": "pad", "action": "edit", "content": "my notes"})
    assert result["status"] == "ok"
    md = (agent.working_dir / "system" / "pad.md").read_text()
    assert "my notes" in md
    agent.stop(timeout=1.0)


def test_pad_edit_with_files(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    # Create files to import
    (agent.working_dir / "export1.txt").write_text("knowledge from export 1")
    (agent.working_dir / "export2.txt").write_text("knowledge from export 2")

    mgr = agent.get_capability("psyche")
    result = mgr.handle({
        "object": "pad", "action": "edit",
        "content": "My working notes.",
        "files": ["export1.txt", "export2.txt"],
    })
    assert result["status"] == "ok"
    md = (agent.working_dir / "system" / "pad.md").read_text()
    assert "My working notes." in md
    assert "[file-1]" in md
    assert "knowledge from export 1" in md
    assert "[file-2]" in md
    assert "knowledge from export 2" in md
    agent.stop(timeout=1.0)


def test_pad_edit_files_only(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    (agent.working_dir / "data.txt").write_text("file data")

    mgr = agent.get_capability("psyche")
    result = mgr.handle({
        "object": "pad", "action": "edit",
        "files": ["data.txt"],
    })
    assert result["status"] == "ok"
    md = (agent.working_dir / "system" / "pad.md").read_text()
    assert "[file-1]" in md
    assert "file data" in md
    agent.stop(timeout=1.0)


def test_pad_edit_missing_file_errors(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    result = mgr.handle({
        "object": "pad", "action": "edit",
        "content": "notes",
        "files": ["nonexistent.txt"],
    })
    assert "error" in result
    assert "nonexistent.txt" in result["error"]
    agent.stop(timeout=1.0)


def test_pad_edit_empty_errors(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    result = mgr.handle({"object": "pad", "action": "edit"})
    assert "error" in result
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Pad load (delegates to eigen)
# ---------------------------------------------------------------------------


def test_pad_load_delegates_to_eigen(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    agent.start()
    try:
        mgr = agent.get_capability("psyche")
        system_dir = agent._working_dir / "system"
        system_dir.mkdir(exist_ok=True)
        (system_dir / "pad.md").write_text("loaded via eigen")

        result = mgr.handle({"object": "pad", "action": "load"})
        assert result["status"] == "ok"
        section = agent._prompt_manager.read_section("pad")
        assert "loaded via eigen" in section
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Molt (agent-callable via psyche, delegates to eigen)
# ---------------------------------------------------------------------------


def test_molt_delegates_to_eigen(tmp_path):
    """psyche(context, molt, summary) delegates to eigen, replays the molt's
    own ToolCallBlock as the opening assistant entry of the fresh session
    (the summary lives in args.summary), and returns a faint-memory result."""
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message(
            [TextBlock(text="Hi there.")],
        )
        # Simulate the assistant turn that emitted the molt — it must already
        # be in the live interface by the time eigen runs (base_agent's
        # adapter records the assistant message before dispatching tool calls).
        molt_wire_id = "toolu_test_molt_001"
        molt_summary = "Key findings: X=42. Current task: analyze dataset Z."
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=molt_wire_id,
                name="psyche",
                args={"object": "context", "action": "molt", "summary": molt_summary},
            ),
        ])

        mgr = agent.get_capability("psyche")
        result = mgr.handle({
            "object": "context",
            "action": "molt",
            "summary": molt_summary,
            "_tc_id": molt_wire_id,
        })

        assert result["status"] == "ok"
        # The agent's summary now lives in the replayed ToolCallBlock's
        # args.summary on the FRESH interface, not in a user message.
        iface = agent._session._chat.interface
        assistant_entries = [e for e in iface.entries if e.role == "assistant"]
        assert assistant_entries, "fresh session should contain the replayed molt tool_call"
        last = assistant_entries[-1]
        molt_calls = [b for b in last.content if isinstance(b, ToolCallBlock)]
        assert molt_calls, "last assistant entry should carry the molt ToolCallBlock"
        assert molt_calls[0].id == molt_wire_id
        assert molt_calls[0].args.get("summary") == molt_summary
    finally:
        agent.stop()


def test_molt_via_system_context_forget_still_works(tmp_path):
    """System-initiated molt (base_agent calls this when the warning ladder
    is exhausted) uses the localized default summary and succeeds without
    any agent-provided summary."""
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock
    from lingtai_kernel.intrinsics.eigen import context_forget

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message(
            [TextBlock(text="Hi there.")],
        )

        result = context_forget(agent)
        assert result.get("status") == "ok"
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Schema checks
# ---------------------------------------------------------------------------


def test_psyche_schema_has_correct_objects():
    from lingtai.core.psyche import get_schema
    SCHEMA = get_schema("en")
    objects = SCHEMA["properties"]["object"]["enum"]
    assert set(objects) == {"lingtai", "pad", "context"}


def test_psyche_schema_has_correct_actions():
    from lingtai.core.psyche import get_schema
    SCHEMA = get_schema("en")
    # Top-level action has no enum (constrained conditionally per object).
    assert "enum" not in SCHEMA["properties"]["action"]
    # Verify allOf constraints carry the correct per-object action sets.
    action_sets = {}
    for rule in SCHEMA["allOf"]:
        obj = rule["if"]["properties"]["object"]["const"]
        acts = set(rule["then"]["properties"]["action"]["enum"])
        action_sets[obj] = acts
    assert action_sets == {
        "lingtai": {"update", "load"},
        "pad": {"edit", "load", "append"},
        "context": {"molt"},
    }


def test_psyche_schema_has_files_field():
    from lingtai.core.psyche import get_schema
    SCHEMA = get_schema("en")
    assert "files" in SCHEMA["properties"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_invalid_object(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    result = mgr.handle({"object": "bogus", "action": "diff"})
    assert "error" in result
    agent.stop(timeout=1.0)


def test_invalid_action_for_object(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    mgr = agent.get_capability("psyche")
    result = mgr.handle({"object": "lingtai", "action": "submit"})
    assert "error" in result
    assert "update" in result["error"]
    agent.stop(timeout=1.0)


def test_psyche_stop_does_not_overwrite_pad_md(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    pad_file = agent.working_dir / "system" / "pad.md"
    pad_file.parent.mkdir(exist_ok=True)
    pad_file.write_text("previous session pad")
    agent.stop()
    assert pad_file.read_text() == "previous session pad"
