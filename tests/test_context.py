"""Tests for the `context` intrinsic — molt, forced molt, and its public schema.

Context behavior only. The pad and 灵台 identity operations that used to live
behind this root are their own families now and are tested by their owners
(`tests/test_pad.py`, `tests/test_pad_lingtai_split.py`); the two name actions
moved to `system` (`tests/test_tool_family_system_migration.py`).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai.kernel.base_agent import BaseAgent
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def _call(agent, args: dict) -> dict:
    """Dispatch a context tool call directly through the registered intrinsic."""
    return agent._intrinsics["context"](args)


_VALID_JOURNAL = """\
---
name: 2026-06-19-molt-1-test
description: A test session journal entry for the molt gate.
date: 2026-06-19
molt_count: 1
type: session-journal
---

## What this segment was about
Testing.

## Accomplishments
Wrote a valid session journal.
"""


def _write_session_journal(agent, rel="knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md"):
    """Write a valid session-journal entry so an agent-initiated molt passes
    the session-journal gate (issue #350). Returns the workdir-relative path."""
    path = agent._working_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_VALID_JOURNAL, encoding="utf-8")
    return rel


# ---------------------------------------------------------------------------
# Setup / registration
# ---------------------------------------------------------------------------


def test_context_is_intrinsic(tmp_path):
    """Context is an intrinsic, not a capability — always registered."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    assert "context" in agent._intrinsics
    assert "eigen" not in agent._intrinsics
    agent.stop(timeout=1.0)


def test_psyche_capability_input_is_tolerated_because_psyche_is_intrinsic(tmp_path):
    """A redundant capabilities=['psyche'] entry must not break boot.

    The capability entry is filtered because the durable Psyche family is
    already intrinsic. Both Psyche and Context therefore remain visible
    independently of the capability list.
    """
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    try:
        # The stale name is filtered out of the resolved capability set...
        assert "psyche" not in [name for name, _ in agent._capabilities]
        # ...while the canonical intrinsic remains present and public.
        assert "psyche" in agent._intrinsics
        assert "psyche" in [s.name for s in agent._build_tool_schemas()]
        # Context is unaffected — it is intrinsic, not capability-gated.
        assert "context" in agent._intrinsics
        assert "context" in [s.name for s in agent._build_tool_schemas()]
    finally:
        agent.stop(timeout=1.0)


def test_anima_alias_removed(tmp_path):
    """'anima' alias was removed — agent skips it (unknown capabilities are logged, not raised)."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["anima"],
    )
    assert "anima" not in [name for name, _ in agent._capabilities]
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Molt (agent-initiated)
# ---------------------------------------------------------------------------


def test_molt_returns_faint_memory(tmp_path):
    """psyche(context, molt, summary) replays the molt's own ToolCallBlock as
    the opening assistant entry of the fresh session, and returns a faint-
    memory result dict."""
    from lingtai.kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi there.")])

        journal_path = _write_session_journal(agent)
        molt_wire_id = "toolu_test_molt_001"
        molt_summary = "Key findings: X=42. Current task: analyze dataset Z."
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=molt_wire_id,
                name="context",
                args={
                    "action": "molt",
                    "input": {
                        "summary": molt_summary,
                        "session_journal_path": journal_path,
                        "keep_tool_calls": None,
                        "keep_last": None,
                    },
                },
            ),
        ])

        result = _call(agent, {
            "action": "molt",
            "input": {
                "summary": molt_summary,
                "session_journal_path": journal_path,
                "keep_tool_calls": None,
                "keep_last": None,
            },
            "_tc_id": molt_wire_id,
        })

        assert result["status"] == "ok"
        iface = agent._session._chat.interface
        assistant_entries = [e for e in iface.entries if e.role == "assistant"]
        assert assistant_entries, "fresh session should contain the replayed molt tool_call"
        last = assistant_entries[-1]
        molt_calls = [b for b in last.content if isinstance(b, ToolCallBlock)]
        assert molt_calls, "last assistant entry should carry the molt ToolCallBlock"
        assert molt_calls[0].id == molt_wire_id
        # The agent's briefing is replayed verbatim from its own envelope —
        # under `input`, where the migrated schema puts it.
        assert molt_calls[0].args["input"]["summary"] == molt_summary
    finally:
        agent.stop()


def test_context_forget_still_works(tmp_path):
    """System-initiated molt (base_agent calls this when the warning ladder
    is exhausted) uses the localized default summary and succeeds without
    any agent-provided summary."""
    from lingtai.kernel.llm.interface import ChatInterface, TextBlock
    from lingtai.tools.context import context_forget

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi there.")])

        result = context_forget(agent)
        assert result.get("status") == "ok"
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Schema checks
# ---------------------------------------------------------------------------


def test_context_schema_exposes_the_exact_action_surface():
    """The retained (object, action) pairs survive as flat actions, unrenamed.

    The LTP v2 migration collapsed the two-key matrix into the single-`action`
    envelope without adding, dropping, renaming, or merging an operation. The
    pad/lingtai split then *removed* five actions to their own roots — see
    ``tests/test_pad_lingtai_split.py`` — leaving the rest untouched in both
    spelling and order.
    """
    from lingtai.tools.context import get_schema
    SCHEMA = get_schema("en")
    assert SCHEMA["properties"]["action"]["enum"] == [
        "molt",
        "summarize", "rebuild",
        "manual",
    ]


def test_context_schema_is_the_closed_ltp_v2_envelope():
    # The pre-migration schema was deliberately flat with no combinators
    # (#114). The generic ToolFamily composer now supplies the closed root
    # plus the `allOf` action/input correlation adopted after the 2026-07-27
    # Responses probe, so both of those old assertions are inverted here.
    from lingtai.tools.context import get_schema
    SCHEMA = get_schema("en")
    assert set(SCHEMA["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert SCHEMA["required"] == ["action", "input", "reasoning"]
    assert SCHEMA["additionalProperties"] is False
    assert len(SCHEMA["allOf"]) == len(SCHEMA["properties"]["action"]["enum"])
    # `object` is gone as a public root field — no compatibility alias.
    assert "object" not in SCHEMA["properties"]


def test_context_schema_has_no_files_field_after_the_pad_split():
    """`files` left with the pad family; no psyche action advertises it."""
    from lingtai.tools.context import get_schema
    SCHEMA = get_schema("en")
    with_files = {
        cond["if"]["properties"]["action"]["const"]
        for cond in SCHEMA["allOf"]
        if "files" in cond["then"]["properties"]["input"]["properties"]
    }
    assert with_files == set()


def test_context_schema_has_session_journal_path_only_on_molt():
    """Issue #350: molt requires a structured session_journal_path arg.

    It lives in `molt`'s own strict input, so no other action advertises it.
    """
    from lingtai.tools.context import get_schema
    SCHEMA = get_schema("en")
    owners = {}
    for cond in SCHEMA["allOf"]:
        action = cond["if"]["properties"]["action"]["const"]
        owners[action] = cond["then"]["properties"]["input"]["properties"]
    assert "session_journal_path" not in owners["summarize"]
    assert "session_journal_path" not in owners["rebuild"]
    prop = owners["molt"]["session_journal_path"]
    assert prop["type"] == "string"
    assert prop["description"]
    assert "session_journal_path" in owners["molt"]
    # Look the branch up by its `action` const rather than by position — the
    # pad/lingtai split shifted every index, and position was never the fact
    # under test.
    molt_branch = next(
        cond for cond in SCHEMA["allOf"]
        if cond["if"]["properties"]["action"]["const"] == "molt"
    )
    molt_required = molt_branch["then"]["properties"]["input"]["required"]
    assert "summary" in molt_required and "session_journal_path" in molt_required


def test_context_manual_is_a_short_router_to_focused_references():
    """The first manual response routes depth instead of duplicating recipes."""
    manual_path = Path(__file__).resolve().parents[1] / "src/lingtai/tools/context/manual/SKILL.md"
    body = manual_path.read_text(encoding="utf-8")
    assert len(body) < 8_000
    assert "context(action=\"manual\", input={}, reasoning=\"load context guidance\")" in body
    assert "reference/molt-manual/SKILL.md" in body
    assert "reference/summarize-manual/SKILL.md" in body
    assert "assets/session-journal-entry-template.md" in body
    assert "assets/molt-template.md" in body

    molt_reference = manual_path.parent / "reference" / "molt-manual" / "SKILL.md"
    detail = molt_reference.read_text(encoding="utf-8")
    assert "## 2. Write the session-journal child first" in detail
    assert "## Input field dictionary" in detail
    assert "## Pre-molt checklist" in detail
    assert "type: session-journal" in detail


def test_context_schema_keeps_first_call_safety_signposts():
    """Schema prose retains only the facts needed to choose a safe first call."""
    from lingtai.tools.context import get_schema

    schema = get_schema("en")
    root_description = schema["properties"]["action"]["description"]
    assert "molt" in root_description and "summarize" in root_description
    assert "rebuild" in root_description and "manual" in root_description
    assert "bare input={}" in root_description

    branches = {
        branch["if"]["properties"]["action"]["const"]: branch["then"]["properties"]["input"]["properties"]
        for branch in schema["allOf"]
    }
    assert "before shedding" in branches["molt"]["session_journal_path"]["description"]
    assert "not the parent index" in branches["molt"]["session_journal_path"]["description"]
    assert "does not rebuild" in branches["summarize"]["items"]["description"]
    assert "bare input={}" in branches["rebuild"]["items"]["description"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unknown_action_is_rejected(tmp_path):
    """The former `Unknown object:` guard is now one unknown-action guard."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = _call(agent, {"action": "bogus", "input": {}})
    assert "error" in result
    assert "Unknown context action" in result["error"]
    agent.stop(timeout=1.0)


def test_pre_migration_object_action_shape_is_rejected(tmp_path):
    """The flat (object, action) call shape is gone — no compatibility alias.

    A model imitating pre-migration history fails loudly rather than silently
    dispatching or no-op'ing.
    """
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = _call(agent, {"object": "lingtai", "action": "submit"})
    assert "error" in result
    assert "Unknown context action" in result["error"]
    # And the pad on disk was never touched by the refused call.
    assert not (agent.working_dir / "system" / "lingtai.md").exists()
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Molt summary persistence (system/summaries/)
# ---------------------------------------------------------------------------


def test_molt_writes_summary_file_for_agent_path(tmp_path):
    """Agent-initiated molt persists summary to system/summaries/ with source=agent."""
    from lingtai.kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])

        journal_path = _write_session_journal(agent)
        molt_id = "toolu_test_summary_001"
        molt_summary = "Worked on dataset Z analysis. Found anomaly in column foo."
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=molt_id,
                name="context",
                args={
                    "action": "molt",
                    "input": {
                        "summary": molt_summary,
                        "session_journal_path": journal_path,
                        "keep_tool_calls": None,
                        "keep_last": None,
                    },
                },
            ),
        ])

        result = _call(agent, {
            "action": "molt",
            "input": {
                "summary": molt_summary,
                "session_journal_path": journal_path,
                "keep_tool_calls": None,
                "keep_last": None,
            },
            "_tc_id": molt_id,
        })

        assert result["status"] == "ok"
        assert result.get("summary_path") is not None

        summary_file = agent._working_dir / result["summary_path"]
        assert summary_file.is_file()
        content = summary_file.read_text()
        # Frontmatter present
        assert content.startswith("---\n")
        assert "molt_count: 1" in content
        assert "source: agent" in content
        assert "tokens_shed:" in content
        # Summary body present after frontmatter
        assert molt_summary in content
    finally:
        agent.stop()


def test_context_forget_writes_summary_file_for_system_path(tmp_path):
    """System-initiated molt also persists summary; source field reflects trigger."""
    from lingtai.kernel.llm.interface import ChatInterface, TextBlock
    from lingtai.tools.context import context_forget

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])

        result = context_forget(agent, source="warning_ladder")
        assert result.get("status") == "ok"
        assert result.get("summary_path") is not None

        summary_file = agent._working_dir / result["summary_path"]
        assert summary_file.is_file()
        content = summary_file.read_text()
        assert "source: warning_ladder" in content
        assert "molt_count: 1" in content
    finally:
        agent.stop()


def test_summary_write_failure_does_not_block_molt(tmp_path, monkeypatch):
    """If summary write fails, molt still completes; summary_path is None."""
    from lingtai.kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock
    from lingtai.tools import context as psyche_mod

    monkeypatch.setattr(psyche_mod, "_write_molt_summary", lambda *a, **kw: None)

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])

        journal_path = _write_session_journal(agent)
        molt_id = "toolu_test_failguard_001"
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=molt_id, name="context",
                args={
                    "action": "molt",
                    "input": {
                        "summary": "test",
                        "session_journal_path": journal_path,
                        "keep_tool_calls": None,
                        "keep_last": None,
                    },
                },
            ),
        ])

        result = _call(agent, {
            "action": "molt",
            "input": {
                "summary": "test",
                "session_journal_path": journal_path,
                "keep_tool_calls": None,
                "keep_last": None,
            },
            "_tc_id": molt_id,
        })

        # Molt succeeded
        assert result["status"] == "ok"
        # But summary_path is None (write was forced to fail)
        assert result.get("summary_path") is None
    finally:
        agent.stop()
