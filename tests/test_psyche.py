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
    """'anima' alias was removed — should raise ValueError."""
    import pytest
    with pytest.raises(ValueError, match="Unknown capability"):
        Agent(
            service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
            capabilities=["anima"],
        )


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
# Molt (system-initiated — no agent-callable tool surface)
# ---------------------------------------------------------------------------


def test_molt_happens_via_context_forget(tmp_path):
    """The agent has no tool action to molt. System-initiated molt via
    eigen.context_forget still works and seeds the fresh session with a
    localized post-molt notice."""
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

        # Fresh session should carry the localized post-molt notice as its
        # opening user message — points the agent at logs/events.jsonl.
        iface = agent._session._chat.interface
        user_entries = [e for e in iface.entries if e.role == "user"]
        assert any("events.jsonl" in str(e.content) for e in user_entries)
    finally:
        agent.stop()


def test_psyche_rejects_context_object(tmp_path):
    """Post-removal: psyche no longer exposes a context/molt surface.
    Calling it should return an error, not silently delegate."""
    from lingtai_kernel.llm.interface import ChatInterface

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        mock_chat.interface = ChatInterface()
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    agent.start()
    try:
        mgr = agent.get_capability("psyche")
        result = mgr.handle({"object": "context", "action": "molt"})
        assert "error" in result
        assert "context" in result["error"].lower() or "unknown object" in result["error"].lower()
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Schema checks
# ---------------------------------------------------------------------------


def test_psyche_schema_has_correct_objects():
    from lingtai.capabilities.psyche import get_schema
    SCHEMA = get_schema("en")
    objects = SCHEMA["properties"]["object"]["enum"]
    # context/molt was removed — molt now happens to the agent via
    # eigen.context_forget, not as a user-callable tool action.
    assert set(objects) == {"lingtai", "pad"}


def test_psyche_schema_has_correct_actions():
    from lingtai.capabilities.psyche import get_schema
    SCHEMA = get_schema("en")
    actions = SCHEMA["properties"]["action"]["enum"]
    assert set(actions) == {"update", "load", "edit", "append"}


def test_psyche_schema_has_files_field():
    from lingtai.capabilities.psyche import get_schema
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
