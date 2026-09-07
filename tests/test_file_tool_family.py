"""Focused evidence for the unified ``file`` LTP v2 family migration.

Covers the migration's own promises: exactly one public ``file`` root with the
seven canonical children, the closed root envelope, exact action/input
correlation on both the Chat and Responses wires, per-child dispatch/result/
error behavior, the family-owned no-I/O manual, read continuation and
line-truncation through the family, verbatim write/edit receipts, grep/glob,
cross-action rejection before handler I/O, and the ``summarize`` control and
risk posture.

Per-operation depth (cap math, traversal budgets, sidecar behavior) stays with
the retained operation packages' own suites — ``tests/test_layers_file.py`` and
``tests/test_read_continuation.py`` — which this migration keeps green.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lingtai.agent import Agent
from lingtai.kernel.llm.base import FunctionSchema
from lingtai.kernel.tool_result_summary import summary_requested
from lingtai.tools.file import get_description, get_schema
from tests._service_helpers import make_gemini_mock_service as make_mock_service

CANONICAL_ACTIONS = [
    "read", "write", "edit", "glob", "grep", "settings", "manual"
]


@pytest.fixture
def file_agent(tmp_path):
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "test",
        capabilities=["file"],
    )
    yield agent
    agent.stop(timeout=1.0)


def _call(agent, action, input_, **root):
    return agent._tool_handlers["file"](
        {"action": action, "input": input_, "reasoning": "focused test", **root}
    )


def _read_input(file_path, offset=None, limit=None, max_chars=None):
    return {
        "file_path": file_path,
        "offset": offset,
        "limit": limit,
        "max_chars": max_chars,
    }


# ---------------------------------------------------------------------------
# Registration: tool count and names
# ---------------------------------------------------------------------------

def test_only_file_root_is_registered(file_agent):
    """One public ``file`` root; the five old model-facing roots are gone."""
    assert "file" in file_agent._tool_handlers
    for retired in ("read", "write", "edit", "glob", "grep"):
        assert retired not in file_agent._tool_handlers, f"{retired} must not be a public root"

    schema_names = [s.name for s in file_agent._tool_schemas]
    assert schema_names.count("file") == 1, "file must be registered exactly once"
    assert not ({"read", "write", "edit", "glob", "grep"} & set(schema_names))


def test_legacy_capability_names_resolve_to_one_file_root(tmp_path):
    """An old preset naming the five capabilities still yields one ``file`` tool."""
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path / "legacy",
        capabilities=["read", "write", "edit", "glob", "grep"],
    )
    try:
        assert "file" in agent._tool_handlers
        for retired in ("read", "write", "edit", "glob", "grep"):
            assert retired not in agent._tool_handlers
        assert [s.name for s in agent._tool_schemas].count("file") == 1
    finally:
        agent.stop(timeout=1.0)


def test_description_names_every_action(file_agent):
    description = get_description()
    for action in CANONICAL_ACTIONS:
        assert f"action='{action}'" in description


# ---------------------------------------------------------------------------
# Schema: closed root, per-child branches, action/input correlation
# ---------------------------------------------------------------------------

def test_schema_root_is_closed_action_input_reasoning_summarize():
    schema = get_schema()
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["action"]["enum"] == CANONICAL_ACTIONS
    assert schema["properties"]["summarize"]["type"] == "boolean"
    assert schema["properties"]["reasoning"]["type"] == "string"


def test_no_branch_admits_envelope_fields():
    """``reasoning``/``_reasoning``/``summarize`` never leak into child input."""
    schema = get_schema()
    for branch in schema["properties"]["input"]["anyOf"]:
        for leaked in ("reasoning", "_reasoning", "summarize", "action"):
            assert leaked not in branch["properties"], f"{branch['title']} leaks {leaked}"
        assert branch["additionalProperties"] is False


def test_every_child_schema_is_exact():
    """Each action's declared input is exactly its canonical field set."""
    expected = {
        "read input": ({"file_path", "offset", "limit", "max_chars"}, {"file_path"}),
        "write input": ({"file_path", "content"}, {"file_path", "content"}),
        "edit input": (
            {"file_path", "old_string", "new_string", "replace_all"},
            {"file_path", "old_string", "new_string"},
        ),
        "glob input": ({"pattern", "path"}, {"pattern"}),
        "grep input": ({"pattern", "path", "glob", "max_matches"}, {"pattern"}),
        "settings inventory input": (set(), set()),
        "manual input": (set(), set()),
    }
    branches = {b["title"]: b for b in get_schema()["properties"]["input"]["anyOf"]}
    assert set(branches) == set(expected)
    for title, (properties, non_nullable) in expected.items():
        branch = branches[title]
        assert set(branch["properties"]) == properties, title
        # Optional fields use the provider-compatible nullable representation:
        # declared required, typed ["T", "null"].
        assert set(branch["required"]) == properties, title
        for name, prop in branch["properties"].items():
            if name in non_nullable:
                assert prop["type"] == "string", f"{title}.{name}"
            else:
                assert "null" in prop["type"], f"{title}.{name} must be nullable"


def test_root_allof_correlates_each_action_to_its_input():
    """Root ``allOf`` if/then pins each action const to that child's own input."""
    schema = get_schema()
    conditions = schema["allOf"]
    assert len(conditions) == len(CANONICAL_ACTIONS)
    branches = {b["title"]: b for b in schema["properties"]["input"]["anyOf"]}

    for action, condition in zip(CANONICAL_ACTIONS, conditions):
        assert condition["if"]["properties"]["action"]["const"] == action
        # A strict ``if`` with a missing property matches vacuously; the guard
        # is what makes the correlation real.
        assert condition["if"]["required"] == ["action"]
        then_input = condition["then"]["properties"]["input"]
        title = (
            "settings inventory input" if action == "settings" else f"{action} input"
        )
        branch = branches[title]
        assert set(then_input["properties"]) == set(branch["properties"])
        assert then_input["additionalProperties"] is False


def test_schema_branches_do_not_share_mutable_state():
    """Two calls, and the two surfaces, never alias one container."""
    first, second = get_schema(), get_schema()
    first["properties"]["input"]["anyOf"][0]["properties"].pop("file_path")
    assert "file_path" in second["properties"]["input"]["anyOf"][0]["properties"]
    assert "file_path" in first["allOf"][0]["then"]["properties"]["input"]["properties"]


# ---------------------------------------------------------------------------
# Wire parity: Chat Completions and Responses
# ---------------------------------------------------------------------------

def test_chat_and_responses_wire_parity():
    """Both wires keep the closed root, the action enum, and the correlation."""
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    schema = FunctionSchema(name="file", description="file", parameters=get_schema())
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    for wire in (chat, responses):
        assert wire["type"] == "object"
        assert wire["required"] == ["action", "input", "reasoning"]
        assert wire["additionalProperties"] is False
        assert set(wire["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert wire["properties"]["action"]["enum"] == CANONICAL_ACTIONS
        branches = wire["properties"]["input"]["anyOf"]
        assert [b["title"] for b in branches] == [
            "read input", "write input", "edit input", "glob input",
            "grep input", "settings inventory input", "manual input",
        ]
        # Root ``allOf`` correlation survives on both wires.
        assert len(wire["allOf"]) == len(CANONICAL_ACTIONS)
        for action, condition in zip(CANONICAL_ACTIONS, wire["allOf"]):
            assert condition["if"]["properties"]["action"]["const"] == action


# ---------------------------------------------------------------------------
# Dispatch: per-action success
# ---------------------------------------------------------------------------

def test_write_and_edit_receipts_are_verbatim(file_agent):
    """Mutating actions return their own canonical receipt, unwrapped."""
    target = str(file_agent.working_dir / "scratch" / "a.txt")

    written = _call(file_agent, "write", {"file_path": target, "content": "one\ntwo\n"})
    assert written == {"status": "ok", "path": target, "bytes": 8}
    assert Path(target).read_text() == "one\ntwo\n"

    edited = _call(
        file_agent,
        "edit",
        {"file_path": target, "old_string": "two", "new_string": "2", "replace_all": None},
    )
    assert edited == {"status": "ok", "replacements": 1}
    assert Path(target).read_text() == "one\n2\n"


def test_edit_replace_all_receipt_counts_every_replacement(file_agent):
    target = str(file_agent.working_dir / "many.txt")
    _call(file_agent, "write", {"file_path": target, "content": "x x x\n"})
    result = _call(
        file_agent,
        "edit",
        {"file_path": target, "old_string": "x", "new_string": "y", "replace_all": True},
    )
    assert result == {"status": "ok", "replacements": 3}
    assert Path(target).read_text() == "y y y\n"


def test_read_returns_numbered_lines(file_agent):
    target = str(file_agent.working_dir / "n.txt")
    _call(file_agent, "write", {"file_path": target, "content": "alpha\nbeta\n"})
    result = _call(file_agent, "read", _read_input(target))
    assert result["content"] == "1\talpha\n2\tbeta\n"
    assert result["total_lines"] == 2
    assert result["lines_shown"] == 2
    assert "truncated" not in result


def test_glob_returns_sorted_matches(file_agent):
    root = file_agent.working_dir / "g"
    for name in ("b.py", "a.py", "c.txt"):
        _call(file_agent, "write", {"file_path": str(root / name), "content": "pass\n"})
    result = _call(file_agent, "glob", {"pattern": "*.py", "path": str(root)})
    names = [Path(p).name for p in result["matches"]]
    assert names == sorted(names)
    assert set(names) == {"a.py", "b.py"}
    assert result["count"] == 2


def test_grep_returns_path_and_line_results(file_agent):
    root = file_agent.working_dir / "r"
    _call(file_agent, "write", {"file_path": str(root / "s.py"), "content": "no\nneedle here\n"})
    result = _call(
        file_agent,
        "grep",
        {"pattern": "needle", "path": str(root), "glob": None, "max_matches": None},
    )
    assert result["count"] == 1
    match = result["matches"][0]
    assert Path(match["file"]).name == "s.py"
    assert match["line"] == 2
    assert match["text"] == "needle here"
    assert result["truncated"] is False


def test_grep_max_matches_cap_is_honored(file_agent):
    root = file_agent.working_dir / "cap"
    _call(file_agent, "write", {"file_path": str(root / "m.txt"), "content": "hit\nhit\nhit\n"})
    result = _call(
        file_agent,
        "grep",
        {"pattern": "hit", "path": str(root), "glob": "*.txt", "max_matches": 2},
    )
    assert result["count"] == 2
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# Read continuation and line truncation through the family
# ---------------------------------------------------------------------------

def test_read_continuation_through_family(file_agent):
    """``max_chars`` paging and the continuation contract survive migration."""
    target = str(file_agent.working_dir / "big.txt")
    _call(file_agent, "write", {"file_path": target, "content": "".join(f"line{i}\n" for i in range(20))})

    first = _call(file_agent, "read", _read_input(target, max_chars=30))
    assert first["truncated"] is True
    assert first["cap_chars"] == 30
    assert first["returned_chars"] == len(first["content"])
    assert first["requested_offset"] == 1
    assert first["remaining_lines_estimate"] > 0
    assert "line_truncated" not in first

    resumed = _call(file_agent, "read", _read_input(target, offset=first["next_offset"]))
    assert resumed["content"].startswith(f"{first['next_offset']}\t")
    assert "truncated" not in resumed


def test_read_line_truncated_marks_a_prefix(file_agent):
    """A single over-cap line returns a bounded prefix, flagged truthfully."""
    target = str(file_agent.working_dir / "long.txt")
    _call(file_agent, "write", {"file_path": target, "content": "z" * 500 + "\nnext\n"})

    result = _call(file_agent, "read", _read_input(target, max_chars=50))
    assert result["truncated"] is True
    assert result["line_truncated"] is True
    assert len(result["content"]) == 50
    # ``next_offset`` skips to the next physical line; the hidden tail is not
    # recoverable by continuing, exactly as before the migration.
    assert result["next_offset"] == 2


def test_read_offset_and_limit_window(file_agent):
    target = str(file_agent.working_dir / "win.txt")
    _call(file_agent, "write", {"file_path": target, "content": "".join(f"L{i}\n" for i in range(10))})
    result = _call(file_agent, "read", _read_input(target, offset=3, limit=2))
    assert result["content"] == "3\tL2\n4\tL3\n"
    assert result["total_lines"] == 10


# ---------------------------------------------------------------------------
# Manual: strict empty input, no target I/O, canonical result
# ---------------------------------------------------------------------------

def test_manual_returns_body_and_path_without_double_wrapping(file_agent):
    result = _call(file_agent, "manual", {})
    assert result["status"] == "ok"
    assert set(result) == {"status", "content", "structuredContent"}
    body = result["content"][0]["text"]
    manual_path = result["structuredContent"]["manual_path"]
    assert "name: file-manual" in body
    assert manual_path.endswith("capabilities/file-manual/SKILL.md")
    assert Path(manual_path).read_text(encoding="utf-8") == body
    # No nested child-result envelope inside the action result.
    assert "content" not in result["structuredContent"]


def test_manual_performs_no_target_io(file_agent, monkeypatch):
    """``manual`` touches no path but the manual's own."""
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("manual must not perform target file I/O")

    monkeypatch.setattr(file_agent._file_io, "read", explode)
    monkeypatch.setattr(file_agent._file_io, "write", explode)
    monkeypatch.setattr(file_agent._file_io, "glob", explode)
    monkeypatch.setattr(file_agent._file_io, "grep", explode)

    result = _call(file_agent, "manual", {})
    assert result["status"] == "ok"
    assert result["content"][0]["text"]


def test_manual_rejects_any_input_field(file_agent):
    result = _call(file_agent, "manual", {"file_path": "/etc/passwd"})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_read_manual_is_a_nested_reference_not_an_action():
    """Read pagination depth stays nested under the one family manual."""
    assert "read-manual" not in get_schema()["properties"]["action"]["enum"]
    body = Path("src/lingtai/tools/file/manual/SKILL.md").read_text(encoding="utf-8")
    assert "read-manual" in body, "file-manual must point at the nested read reference"


def test_file_disclosure_router_keeps_first_call_guard_and_exact_read_route():
    """The concise entry still teaches safe defaults and names the deep owner."""
    description = get_description()
    for required in (
        "file_path", "offset", "limit", "max_chars", "100 000", "2000",
        "truncated", "next_offset", "remaining_lines_estimate", "line_truncated",
        "UTF-8", "text-only", "context(action='rebuild'", "file-manual",
    ):
        assert required in description

    manual = Path("src/lingtai/tools/file/manual/SKILL.md").read_text(encoding="utf-8")
    assert "[read-manual](../../../intrinsic_skills/read-manual/SKILL.md)" in manual
    assert "A JSON `null` optional value means" in manual
    assert "Neither action reloads or changes the current system" in manual


# ---------------------------------------------------------------------------
# Failure modes: unknown action, cross-action input, per-action errors
# ---------------------------------------------------------------------------

def test_unknown_action_fails_with_stable_typed_error(file_agent):
    result = _call(file_agent, "delete", {})
    assert result["status"] == "failed"
    assert result["error_code"] == "ACTION_REQUIRED"
    assert "read, write, edit, glob, grep, settings, manual" in result["message"]


@pytest.mark.parametrize(
    "action,bad_input",
    [
        ("read", {"file_path": "x", "offset": None, "limit": None, "max_chars": None, "content": "smuggled"}),
        ("read", {"old_string": "a"}),
        ("write", {"file_path": "x", "content": "y", "pattern": "*.py"}),
        ("edit", {"file_path": "x", "old_string": "a", "new_string": "b", "max_matches": 5}),
        ("glob", {"pattern": "*", "path": None, "content": "smuggled"}),
        ("grep", {"pattern": "*", "path": None, "glob": None, "max_matches": None, "file_path": "x"}),
    ],
)
def test_cross_action_input_is_rejected_before_io(file_agent, action, bad_input, monkeypatch):
    """A key from another action's branch fails before any handler runs."""
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("dispatch must reject before handler I/O")

    for method in ("read", "write", "glob", "grep"):
        monkeypatch.setattr(file_agent._file_io, method, explode)

    result = _call(file_agent, action, bad_input)
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "unsupported file input field"


def test_non_object_input_is_rejected(file_agent):
    result = file_agent._tool_handlers["file"](
        {"action": "read", "input": "not-an-object", "reasoning": "r"}
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_unknown_root_field_is_rejected(file_agent):
    result = file_agent._tool_handlers["file"](
        {"action": "manual", "input": {}, "reasoning": "r", "parameters": {}}
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_per_action_error_results_stay_canonical(file_agent):
    """Each operation's own error dict is returned unchanged."""
    missing = str(file_agent.working_dir / "nope.txt")

    read_error = _call(file_agent, "read", _read_input(missing))
    assert read_error == {"status": "error", "message": f"File not found: {missing}"}

    edit_missing = _call(
        file_agent,
        "edit",
        {"file_path": missing, "old_string": "a", "new_string": "b", "replace_all": None},
    )
    assert edit_missing == {"status": "error", "message": f"File not found: {missing}"}

    target = str(file_agent.working_dir / "ambig.txt")
    _call(file_agent, "write", {"file_path": target, "content": "dup dup\n"})
    ambiguous = _call(
        file_agent,
        "edit",
        {"file_path": target, "old_string": "dup", "new_string": "x", "replace_all": None},
    )
    assert ambiguous["status"] == "error"
    assert "found 2 times" in ambiguous["message"]
    # The ambiguous edit must not have mutated the file.
    assert Path(target).read_text() == "dup dup\n"

    not_found = _call(
        file_agent,
        "edit",
        {"file_path": target, "old_string": "absent", "new_string": "x", "replace_all": None},
    )
    assert not_found == {"status": "error", "message": f"old_string not found in {target}"}


def test_missing_required_input_is_reported_by_the_operation(file_agent):
    """A schema-required field omitted at runtime still fails safely."""
    assert _call(file_agent, "read", {})["message"] == "file_path is required"
    assert _call(file_agent, "write", {"file_path": "x"})["message"] == "content is required"
    assert _call(file_agent, "glob", {})["message"] == "pattern is required"
    assert _call(file_agent, "grep", {})["message"] == "pattern is required"


# ---------------------------------------------------------------------------
# Summarize control and risk posture
# ---------------------------------------------------------------------------

def test_summarize_is_recognized_for_file_and_stripped_before_dispatch(file_agent):
    """Root ``summarize`` is the canonical control and never reaches a handler."""
    assert summary_requested({"summarize": True}, "file") is True
    assert summary_requested({"summarize": True}, "read") is False, \
        "the retired root must not be treated as a migrated family"
    assert summary_requested({"summarize": False}, "file") is False
    assert summary_requested({"summary": True}, "file") is True

    target = str(file_agent.working_dir / "sum.txt")
    written = _call(file_agent, "write", {"file_path": target, "content": "hi\n"}, summarize=True)
    # The receipt is unchanged by the control: summarization is a Host
    # presentation concern applied after the raw result is recorded.
    assert written == {"status": "ok", "path": target, "bytes": 3}


def test_non_boolean_summarize_is_rejected(file_agent):
    result = _call(file_agent, "read", _read_input("x"), summarize="yes")
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "summarize must be a boolean"


def test_family_declares_mixed_read_write_risk():
    """The family must not advertise itself as read-only."""
    contract = Path("src/lingtai/tools/file/CONTRACT.md").read_text(encoding="utf-8")
    assert "mixed read/write family" in contract
    # write/edit are truthfully named as mutating.
    assert "`write` and `edit` mutate the working tree" in contract
