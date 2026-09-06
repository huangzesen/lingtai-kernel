"""Focused LTP v2 evidence for the one public ``psyche`` root.

Covers the exact public inventory, strict-empty inputs, five-field settings,
pre-I/O rejection, unchanged manual routing, read-only behavior, and removal of
the four former public roots and their retired actions.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from lingtai.agent import Agent
from lingtai.tools import psyche as psyche_tool
from tests._service_helpers import make_gemini_mock_service as make_mock_service


MANUAL_ACTIONS = ["pad", "lingtai", "knowledge", "skills", "manual"]
EXPECTED_ACTIONS = ["pad", "lingtai", "knowledge", "skills", "settings", "manual"]

#: Which installed manual each action must return, by its `.library` directory.
EXPECTED_MANUAL_DIR = {
    "pad": "pad-manual",
    "lingtai": "lingtai-manual",
    "knowledge": "knowledge",
    "skills": "skills",
    "manual": "psyche-manual",
}


def _agent(tmp_path, **kwargs):
    return Agent(
        service=make_mock_service(), agent_name="test",
        working_dir=tmp_path / "test", **kwargs,
    )


def _call(agent, action, action_input=None, **root):
    args = {"action": action, "input": {} if action_input is None else action_input}
    args.setdefault("reasoning", "why")
    args.update(root)
    return agent._intrinsics["psyche"](args)


def _settings_provider(agent):
    from lingtai.adapters.tool_plugin_host import AgentPsycheSettingsAdapter
    from lingtai.tools.psyche.settings import build_settings_provider

    return build_settings_provider(
        AgentPsycheSettingsAdapter(lambda: agent._psyche_settings_snapshot)
    )


def _fs_digest(root):
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(root)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public inventory and schema
# ---------------------------------------------------------------------------

def test_exact_public_action_inventory():
    assert psyche_tool.ACTION_ORDER == tuple(EXPECTED_ACTIONS)
    assert psyche_tool.get_schema()["properties"]["action"]["enum"] == EXPECTED_ACTIONS


def test_root_is_the_closed_strict_ltp_v2_envelope():
    schema = psyche_tool.get_schema()
    assert schema["type"] == "object"
    assert sorted(schema["properties"]) == ["action", "input", "reasoning", "summarize"]
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False


def test_every_child_input_is_the_canonical_strict_empty_object():
    schema = psyche_tool.get_schema()
    branches = schema["properties"]["input"]["anyOf"]
    assert [b["title"] for b in branches] == [
        "pad input",
        "lingtai input",
        "knowledge input",
        "skills input",
        "settings inventory input",
        "manual input",
    ]
    for branch in branches:
        assert branch["type"] == "object"
        assert branch["properties"] == {}
        assert branch["additionalProperties"] is False


def test_root_allof_correlates_each_action_const_with_its_input_schema():
    conditions = psyche_tool.get_schema()["allOf"]
    assert len(conditions) == len(EXPECTED_ACTIONS)
    for action, cond in zip(EXPECTED_ACTIONS, conditions):
        assert cond["if"]["properties"]["action"]["const"] == action
        assert cond["if"]["required"] == ["action"]
        assert cond["then"]["properties"]["input"]["additionalProperties"] is False


def test_registered_exactly_once_as_a_mandatory_official_plugin(tmp_path):
    from lingtai.adapters.tool_plugin_host import agent_host_ports
    from lingtai.kernel.tool_plugin import OFFICIAL_TOOL_PLUGIN_NAMES
    from lingtai.tools.registry import BUILTIN_TOOLS, INTRINSICS

    assert INTRINSICS["psyche"] == {
        "module": psyche_tool,
        "official_plugin": True,
    }
    assert "psyche" in OFFICIAL_TOOL_PLUGIN_NAMES
    assert "psyche" not in BUILTIN_TOOLS

    agent = _agent(tmp_path)
    try:
        assert psyche_tool.DECLARATION.requires == ("workdir", "psyche_settings")
        assert set(agent_host_ports(agent, "psyche")) == {"workdir", "psyche_settings"}
        assert agent.official_tool_plugins["psyche"] is psyche_tool.DECLARATION
        assert [s.name for s in agent._tool_schemas].count("psyche") == 1
        assert list(name for name in agent._tool_handlers if name == "psyche") == [
            "psyche"
        ]
        assert [s.name for s in agent._build_tool_schemas()].count("psyche") == 1
    finally:
        agent.stop(timeout=1.0)


def test_psyche_family_reaches_both_provider_wires(tmp_path):
    """The composed schema survives Chat and Responses adapter scrubbing."""
    from lingtai.llm.openai.adapter import _scrub_responses_schema

    schema = psyche_tool.get_schema()
    scrubbed = _scrub_responses_schema(schema)
    assert scrubbed["properties"]["action"]["enum"] == EXPECTED_ACTIONS
    assert scrubbed["additionalProperties"] is False


def test_psyche_is_a_migrated_ltp_v2_family_for_summarize():
    from lingtai.kernel.tool_result_summary import (
        _LTP_V2_MIGRATED_FAMILIES,
        summary_requested,
    )

    assert "psyche" in _LTP_V2_MIGRATED_FAMILIES
    assert summary_requested({"summarize": True}, "psyche") is True
    assert summary_requested({"summarize": False}, "psyche") is False


def test_psyche_is_blacklisted_for_emanations():
    from lingtai.tools.daemon import EMANATION_BLACKLIST

    assert "psyche" in EMANATION_BLACKLIST


# ---------------------------------------------------------------------------
# Each action returns its own intended manual
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", MANUAL_ACTIONS)
def test_each_action_returns_its_intended_manual(tmp_path, action):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, action)
        assert result["status"] == "ok"
        assert result["manual"].strip()
        path = result["manual_path"]
        assert path.endswith(
            f".library/intrinsic/capabilities/{EXPECTED_MANUAL_DIR[action]}/SKILL.md"
        ), path
    finally:
        agent.stop(timeout=1.0)


def test_the_five_manuals_are_all_distinct(tmp_path):
    agent = _agent(tmp_path)
    try:
        bodies = {a: _call(agent, a)["manual"] for a in MANUAL_ACTIONS}
        assert len(set(bodies.values())) == len(MANUAL_ACTIONS)
    finally:
        agent.stop(timeout=1.0)


def test_router_manual_is_a_routing_table_naming_all_four_domains(tmp_path):
    agent = _agent(tmp_path)
    try:
        body = _call(agent, "manual")["manual"]
        for domain in ("pad", "lingtai", "knowledge", "skills"):
            assert f'action="{domain}"' in body
        # It teaches the shared mutation/rebuild model rather than duplicating
        # the domain manuals it points at.
        assert "file.write" in body and "file.edit" in body
        assert 'context(action="rebuild"' in body
    finally:
        agent.stop(timeout=1.0)


def test_router_manual_discloses_depth_and_first_call_guard(tmp_path):
    """The resident router points to depth and keeps the safe first call."""
    agent = _agent(tmp_path)
    try:
        result = _call(agent, "manual")
        body = result["manual"]
        assert "reference/settings/SKILL.md" in body
        assert "reference/network-rules/SKILL.md" in body
        assert "strict empty `input`" in body
        assert "manual first" in body
        assert len(body) < 10000
        manual_path = Path(result["manual_path"])
        assert (manual_path.parent / "reference/settings/SKILL.md").is_file()
        assert (manual_path.parent / "reference/network-rules/SKILL.md").is_file()
    finally:
        agent.stop(timeout=1.0)


def test_router_keeps_settings_anchors_as_reference_pointers(tmp_path):
    """R2 keeps stable anchors while moving settings depth to its owner page."""
    import re

    agent = _agent(tmp_path)
    try:
        result = _call(agent, "manual")
        body = result["manual"]
        anchors = (
            "pad", "pad-file", "base-prompt", "base-prompt-file",
            "covenant", "covenant-file", "comment", "comment-file",
        )
        for anchor in anchors:
            heading = f"### Setting {anchor.replace('-', ' ')}"
            assert len(re.findall(rf"^{re.escape(heading)}$", body, flags=re.M)) == 1
            assert len(
                re.findall(
                    rf"reference/settings/SKILL\.md#setting-{re.escape(anchor)}\)",
                    body,
                )
            ) == 1

        assert "## Which domain am I in?" not in body
        settings_reference = (
            Path(result["manual_path"]).parent / "reference/settings/SKILL.md"
        ).read_text(encoding="utf-8")
        assert "key`, `current`, `default`, `configurable`, and `comment`" in settings_reference
        assert '"schema_version": 1' in settings_reference
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Owner settings discovery
# ---------------------------------------------------------------------------

def test_settings_provider_has_exact_applied_reconstruction_rows(tmp_path):
    agent = _agent(tmp_path)
    try:
        pad_source = agent._working_dir / "pad-source.md"
        pad_source.write_text("PAD FROM FILE", encoding="utf-8")
        (agent._working_dir / "init.json").write_text(
            json.dumps({
                "manifest": {
                    "llm": {"provider": "openai", "model": "gpt-4o"},
                },
                "covenant": "operator contract",
                "pad": "inline Pad loses",
                "pad_file": pad_source.name,
            }),
            encoding="utf-8",
        )
        agent._reconstruct_context()

        provider = _settings_provider(agent)
        rows = provider()
        assert [row.key for row in rows] == [
            "pad", "pad_file", "base_prompt", "base_prompt_file",
            "covenant", "covenant_file", "comment", "comment_file",
        ]
        assert [row.current for row in rows] == [
            "PAD FROM FILE",
            str(pad_source),
            "", None, "", None, "", None,
        ]
        assert [row.default for row in rows] == ["", None, "", None, "", None, "", None]
        assert all(row.configurable is True for row in rows)
        assert all(row._sensitive is True for row in rows)
        assert [row.comment for row in rows] == [
            "psyche-manual#setting-pad",
            "psyche-manual#setting-pad-file",
            "psyche-manual#setting-base-prompt",
            "psyche-manual#setting-base-prompt-file",
            "psyche-manual#setting-covenant",
            "psyche-manual#setting-covenant-file",
            "psyche-manual#setting-comment",
            "psyche-manual#setting-comment-file",
        ]

        pad_source.write_text("FRESH PAD FROM FILE", encoding="utf-8")
        assert _settings_provider(agent)()[0].current == "PAD FROM FILE"
        agent._reconstruct_context()
        assert _settings_provider(agent)()[0].current == "FRESH PAD FROM FILE"

        (agent._working_dir / "init.json").write_text(
            json.dumps({
                "manifest": {
                    "llm": {"provider": "openai", "model": "gpt-4o"},
                },
                "covenant": "operator contract",
                "pad": "INLINE FALLBACK",
                "pad_file": "missing-pad-source.md",
            }),
            encoding="utf-8",
        )
        assert [row.current for row in _settings_provider(agent)()][:2] == [
            "FRESH PAD FROM FILE",
            str(pad_source),
        ]
        agent._reconstruct_context()
        fallback_rows = _settings_provider(agent)()
        assert [row.current for row in fallback_rows][:2] == [
            "INLINE FALLBACK",
            str(agent._working_dir / "missing-pad-source.md"),
        ]

        manual = _call(agent, "manual")["manual"]
        assert "### Setting pad" in manual
        assert "### Setting pad file" in manual
    finally:
        agent.stop(timeout=1.0)


def test_full_setup_binds_resolved_pad_snapshot_and_prompt(tmp_path):
    agent = _agent(tmp_path)
    try:
        pad_source = agent._working_dir / "setup-pad-source.md"
        pad_source.write_text("SETUP PAD", encoding="utf-8")
        (agent._working_dir / "init.json").write_text(
            json.dumps({
                "manifest": {
                    "llm": {
                        "provider": "gemini",
                        "model": "gemini-test",
                        "api_key": "test-only",
                    },
                },
                "covenant": "operator contract",
                "pad_file": pad_source.name,
            }),
            encoding="utf-8",
        )

        agent._setup_from_init()

        assert [row.current for row in _settings_provider(agent)()][:2] == [
            "SETUP PAD",
            str(pad_source),
        ]
        assert "SETUP PAD" in agent._build_system_prompt()
    finally:
        agent.stop(timeout=1.0)


def test_settings_success_is_exactly_eight_projected_rows_and_redacted(tmp_path):
    agent = _agent(tmp_path)
    try:
        source = agent._working_dir / "private-pad-source.md"
        source.write_text("PRIVATE PAD CONTENT", encoding="utf-8")
        (agent._working_dir / "init.json").write_text(
            json.dumps({
                "manifest": {
                    "llm": {"provider": "openai", "model": "gpt-4o"},
                },
                "covenant": "operator contract",
                "pad_file": source.name,
            }),
            encoding="utf-8",
        )
        agent._reconstruct_context()
        result = _call(agent, "settings")
        rows = result["settings"]
        assert [row["key"] for row in rows] == [
            "pad", "pad_file", "base_prompt", "base_prompt_file",
            "covenant", "covenant_file", "comment", "comment_file",
        ]
        assert all(row["current"] == "<redacted>" for row in rows)
        assert all(row["default"] == "<redacted>" for row in rows)
        assert all(row["configurable"] is True for row in rows)
        assert [row["comment"] for row in rows] == [
            "psyche-manual#setting-pad",
            "psyche-manual#setting-pad-file",
            "psyche-manual#setting-base-prompt",
            "psyche-manual#setting-base-prompt-file",
            "psyche-manual#setting-covenant",
            "psyche-manual#setting-covenant-file",
            "psyche-manual#setting-comment",
            "psyche-manual#setting-comment-file",
        ]
        assert list(result["settings"][0]) == [
            "key", "current", "default", "configurable", "comment",
        ]
        assert "PRIVATE PAD CONTENT" not in repr(result)
        assert str(source) not in repr(result)
        rejected = _call(agent, "settings", {"set": "pad"})
        assert rejected["error_code"] == "INVALID_ARGUMENT"
    finally:
        agent.stop(timeout=1.0)


def test_malformed_ambient_init_preserves_applied_settings_snapshot(tmp_path):
    agent = _agent(tmp_path)
    try:
        (agent._working_dir / "init.json").write_text(
            json.dumps({
                "manifest": {
                    "llm": {"provider": "openai", "model": "gpt-4o"},
                },
                "covenant": "operator contract",
                "pad": "APPLIED PRIVATE CONTENT",
            }),
            encoding="utf-8",
        )
        agent._reconstruct_context()
        assert _settings_provider(agent)()[0].current == "APPLIED PRIVATE CONTENT"

        (agent._working_dir / "init.json").write_text(
            '{"pad":"private content"', encoding="utf-8",
        )
        result = _call(agent, "settings")
        assert [row["key"] for row in result["settings"]] == [
            "pad", "pad_file", "base_prompt", "base_prompt_file",
            "covenant", "covenant_file", "comment", "comment_file",
        ]
        assert "private content" not in repr(result)
        assert _settings_provider(agent)()[0].current == "APPLIED PRIVATE CONTENT"

        with pytest.raises(RuntimeError, match="init.json exists but could not be read"):
            agent._reconstruct_context()
        assert _settings_provider(agent)()[0].current == "APPLIED PRIVATE CONTENT"
        assert "settings" in _call(agent, "settings")
    finally:
        agent.stop(timeout=1.0)


def test_configured_pad_seeds_only_an_empty_durable_body(tmp_path):
    agent = _agent(tmp_path)
    try:
        pad_path = agent._working_dir / "system" / "pad.md"
        assert pad_path.read_text(encoding="utf-8") == ""
        agent._reload_prompt_sections({"pad": "INITIAL PAD", "lingtai": ""})
        assert pad_path.read_text(encoding="utf-8") == "INITIAL PAD"
        assert "INITIAL PAD" in agent._build_system_prompt()

        pad_path.write_text("DURABLE PAD", encoding="utf-8")
        agent._reload_prompt_sections({"pad": "REPLACEMENT", "lingtai": ""})
        assert pad_path.read_text(encoding="utf-8") == "DURABLE PAD"
        assert "DURABLE PAD" in agent._build_system_prompt()
        assert "REPLACEMENT" not in agent._build_system_prompt()
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Returned-body accuracy (live-Agent lesson, head f575ec6a)
# ---------------------------------------------------------------------------
#
# A live agent called psyche(action='knowledge'|'skills') and got manuals
# that still taught retired public roots, a retired two-action surface, and
# pre-#1073/#1078 call shapes. Schema-level tests cannot catch that: the wire is
# correct while the *returned text* is wrong. These pin the returned bodies.

#: Substrings that must never reappear in a returned manual body, with the
#: reason each one is wrong. Keyed by the psyche action that returns it.
STALE_BODY_CLAIMS = {
    "knowledge": [
        # A. the retired public root, claimed in the present tense
        ("registers exactly one", "claims knowledge still registers a public tool"),
        ("tool, also named `knowledge`", "names a retired public knowledge root"),
        # A. remnants of the retired info|manual two-action surface
        ("both actions", "assumes the retired two-action surface"),
        ("settings/knowledge.", "names a retired per-domain settings address"),
    ],
    "skills": [
        # B. pre-current call shapes that would fail if a model copied them
        ("system({", "teaches the pre-LTP-v2 flat system call shape"),
        ("shell({", "teaches the pre-LTP-v2 flat shell call shape"),
        # B. an intrinsic bundle that no longer exists
        ("psyche/", "names the dissolved psyche intrinsic"),
        # B. refresh-only apply path, superseded by active context.rebuild
        ("ends with a refresh.", "teaches refresh as the only apply path"),
        ("settings/skills.json", "names a retired per-domain settings address"),
    ],
}

#: Substrings that must be present, proving the corrected current route.
REQUIRED_BODY_ROUTES = {
    "knowledge": [
        'psyche(action="knowledge"',
        'file(action="write"',
        'file(action="read"',
        'context(action="rebuild"',
    ],
    "skills": [
        'psyche(action="skills"',
        'context(action="rebuild"',
        'shell(action="run"',
        'file(action="read"',
        # The config-owner path keeps the exact refresh envelope.
        'system(action="refresh"',
        '"revert_preset": null',
    ],
}


#: Every ``file`` child's exact required input fields, read from the live schema
#: rather than hardcoded, so a schema change breaks this test instead of silently
#: letting an undispatchable snippet ship.
def _file_required_fields():
    from lingtai.tools.file import get_schema

    branches = get_schema()["properties"]["input"]["anyOf"]
    return {b["title"].split()[0]: list(b.get("required", [])) for b in branches}


def _file_snippets(body):
    """Yield ``(action, snippet)`` for every inline ``file(action="...")`` call.

    A snippet runs to the closing ``reasoning="..."`` so a call wrapped across
    source lines is still captured whole.
    """
    import re

    for match in re.finditer(
        r'file\(action="(\w+)",\s*(.*?)reasoning=', body, flags=re.S
    ):
        yield match.group(1), match.group(0)


@pytest.mark.parametrize("action", ["knowledge", "skills"])
def test_returned_manual_file_calls_are_dispatchable(tmp_path, action):
    """Every displayed ``file(...)`` call carries that child's full strict input.

    The `file` children declare every field as required — including the nullable
    `offset`/`limit`/`max_chars` on `read` — so an abbreviated snippet a model
    copies verbatim is rejected before dispatch. A manual that ships one is
    teaching a call that cannot work.
    """
    required = _file_required_fields()

    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        body = _call(agent, action)["manual"]
        snippets = list(_file_snippets(body))
        assert snippets, f"{action} manual shows no file(...) call to check"

        for file_action, snippet in snippets:
            assert file_action in required, (
                f"{action} manual shows unknown file action {file_action!r}"
            )
            for field in required[file_action]:
                assert f'"{field}"' in snippet, (
                    f"{action} manual's file(action={file_action!r}) snippet omits "
                    f"required input field {field!r}: {snippet.strip()!r}"
                )
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("action", ["knowledge", "skills"])
def test_returned_manual_has_no_abbreviated_file_calls(tmp_path, action):
    """Reject the ``file(action="grep", ...)`` elision that hid missing fields."""
    import re

    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        body = _call(agent, action)["manual"]
        elided = re.findall(r'file\(action="\w+",\s*\.\.\.', body)
        assert not elided, (
            f"{action} manual abbreviates a file call instead of showing its "
            f"full strict input: {elided}"
        )
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("action", sorted(STALE_BODY_CLAIMS))
def test_returned_manual_body_has_no_stale_claims(tmp_path, action):
    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        body = _call(agent, action)["manual"]
        for needle, why in STALE_BODY_CLAIMS[action]:
            assert needle not in body, f"{action} manual {why}: {needle!r}"
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("action", sorted(REQUIRED_BODY_ROUTES))
def test_returned_manual_body_teaches_the_current_routes(tmp_path, action):
    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        body = _call(agent, action)["manual"]
        for needle in REQUIRED_BODY_ROUTES[action]:
            assert needle in body, f"{action} manual lost the current route {needle!r}"
    finally:
        agent.stop(timeout=1.0)


def test_no_returned_manual_teaches_a_retired_public_root(tmp_path):
    """No body may show a retired root as an executable call."""
    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        for action in MANUAL_ACTIONS:
            body = _call(agent, action)["manual"]
            for retired in ("pad", "lingtai", "knowledge", "skills"):
                assert f"{retired}(action=" not in body, (
                    f"{action} manual teaches the retired {retired}(action=...) root"
                )
            assert "pad.append" not in body
            assert "knowledge.info" not in body
            assert "skills.info" not in body
    finally:
        agent.stop(timeout=1.0)


def test_manual_result_has_no_double_wrap(tmp_path):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, "manual")
        assert sorted(result) == ["manual", "manual_path", "status"]
        assert "content" not in result and "structuredContent" not in result
    finally:
        agent.stop(timeout=1.0)


def test_missing_manual_degrades_with_the_exact_loader_message(tmp_path):
    agent = _agent(tmp_path)
    try:
        installed = (
            agent._working_dir / ".library" / "intrinsic" / "capabilities"
            / "psyche-manual" / "SKILL.md"
        )
        installed.unlink()
        result = _call(agent, "manual")
        assert result["status"] == "degraded"
        assert result["manual"] == ""
        assert "psyche-manual manual missing" in result["error"]
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Read-only: nothing mutates disk or prompt
# ---------------------------------------------------------------------------

def test_no_action_mutates_disk_or_prompt(tmp_path):
    agent = _agent(tmp_path)
    try:
        before_fs = _fs_digest(agent._working_dir)
        before_prompt = agent._build_system_prompt()
        before_sections = dict(agent._prompt_manager._sections)

        for action in EXPECTED_ACTIONS:
            _call(agent, action)

        assert _fs_digest(agent._working_dir) == before_fs
        assert agent._build_system_prompt() == before_prompt
        assert dict(agent._prompt_manager._sections) == before_sections
    finally:
        agent.stop(timeout=1.0)


def test_manual_actions_never_scan_a_catalog(tmp_path, monkeypatch):
    """No psyche action may reach a catalog scan or the legacy migration."""
    from lingtai.tools import knowledge as knowledge_tool
    from lingtai.tools import skills as skills_tool

    agent = _agent(tmp_path)
    try:
        def _boom(*_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("a psyche manual action scanned a catalog")

        monkeypatch.setattr(knowledge_tool, "_compose_catalog", _boom)
        monkeypatch.setattr(knowledge_tool, "_reconcile", _boom)
        monkeypatch.setattr(knowledge_tool, "_migrate_legacy_json", _boom)
        monkeypatch.setattr(skills_tool, "_reconcile", _boom)

        for action in MANUAL_ACTIONS:
            assert _call(agent, action)["status"] == "ok"
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Fail-closed dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "action", ["append", "info", "edit", "load", "update", "", None, [], {}]
)
def test_unknown_or_retired_action_is_rejected(tmp_path, action):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, action)
        assert "Unknown psyche action" in result["error"]
        assert "pad, lingtai, knowledge, skills, settings, manual" in result["error"]
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("action", EXPECTED_ACTIONS)
def test_any_input_key_is_rejected_before_the_selected_child_runs(
    tmp_path, action, monkeypatch
):
    from lingtai.tools import _manual as manual_loader

    agent = _agent(tmp_path)
    try:
        def _boom(*_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("selected Psyche child ran despite invalid input")

        from lingtai.adapters.tool_plugin_host import AgentPsycheSettingsAdapter

        monkeypatch.setattr(manual_loader, "load_installed_manual", _boom)
        monkeypatch.setattr(AgentPsycheSettingsAdapter, "read_snapshot", _boom)
        result = _call(agent, action, {"files": ["x"]})
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert result["message"] == "unsupported psyche input field"
    finally:
        agent.stop(timeout=1.0)


def test_non_object_input_and_bad_summarize_are_rejected(tmp_path):
    agent = _agent(tmp_path)
    try:
        assert _call(agent, "manual", "nope")["error_code"] == "INVALID_ARGUMENT"
        bad = _call(agent, "manual", summarize="yes")
        assert bad["error_code"] == "INVALID_ARGUMENT"
        assert bad["message"] == "summarize must be a boolean"
    finally:
        agent.stop(timeout=1.0)


def test_unknown_root_field_is_rejected(tmp_path):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, "manual", parameters={"x": 1})
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert result["message"] == "unsupported psyche argument"
    finally:
        agent.stop(timeout=1.0)


def test_envelope_metadata_never_reaches_a_child(tmp_path):
    """``reasoning``/``_reasoning``/``summarize``/``_tc_id`` are not child input."""
    agent = _agent(tmp_path)
    try:
        result = _call(
            agent, "manual",
            reasoning="r", _reasoning="ir", summarize=False, _tc_id="tc-1",
        )
        assert result["status"] == "ok"
        assert result["manual"].strip()
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# The four former public roots are gone, with no alias
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("root", ["pad", "lingtai", "knowledge", "skills"])
def test_retired_public_roots_are_not_registered(tmp_path, root):
    from lingtai.tools.registry import INTRINSICS

    assert root not in INTRINSICS
    agent = _agent(tmp_path)
    try:
        assert root not in agent._intrinsics
        assert root not in agent._tool_handlers
        assert root not in [s.name for s in agent._build_tool_schemas()]
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("pkg", ["pad", "lingtai", "knowledge", "skills"])
def test_retired_domain_packages_expose_no_public_tool_surface(pkg):
    """They survive as private domain owners only — no schema, no dispatch."""
    module = importlib.import_module(f"lingtai.tools.{pkg}")
    for attribute in ("get_schema", "get_description", "handle", "ACTION_ORDER"):
        assert not hasattr(module, attribute), f"{pkg}.{attribute}"


def test_retired_actions_have_no_implementation_left():
    """``pad.append`` is gone as an action AND as a handler."""
    from lingtai.tools import pad as pad_tool

    assert not hasattr(pad_tool, "_pad_append")
    from lingtai.tools.pad import _pad

    assert not hasattr(_pad, "_pad_append")
    assert not hasattr(_pad, "_save_append_list")


@pytest.mark.parametrize("name", ["pad", "lingtai", "knowledge", "skills"])
def test_retired_roots_are_not_ltp_v2_summarize_families(name):
    from lingtai.kernel.tool_result_summary import _LTP_V2_MIGRATED_FAMILIES

    assert name not in _LTP_V2_MIGRATED_FAMILIES


# ---------------------------------------------------------------------------
# Fail-loud full reconstruction (tools/context/CONTRACT.md)
# ---------------------------------------------------------------------------
#
# The Skills and Knowledge catalogs are two of the four durable domains the one
# reconstruction contract must recompose. A composer failure must abort the whole
# rebuild: `context.rebuild` returns `context_reconstruction_failed` and applies
# no summaries and requests no provider replay. Catching and continuing would
# publish a prompt missing a durable section while reporting success.

@pytest.mark.parametrize(
    "module_name,attr",
    [("lingtai.tools.skills", "_reconcile"),
     ("lingtai.tools.knowledge", "_compose_catalog")],
)
def test_catalog_composer_failure_fails_the_whole_rebuild(
    tmp_path, monkeypatch, module_name, attr
):
    import importlib

    from lingtai.tools import context as context_tool

    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        agent._prompt_manager.write_section("comment", "CURRENT-COMMENT")
        module = importlib.import_module(module_name)

        def _boom(*_a, **_k):
            raise RuntimeError("catalog scan exploded")

        monkeypatch.setattr(module, attr, _boom)

        # Instrument the two stages that must NOT be reached. `_flush_system_prompt`
        # is the single final publish (contract step 3); `_summarize_engine` is the
        # entry point for recording/applying summaries and requesting provider
        # replay (contract steps 4-5). Neither may run when a composer raises.
        publishes: list[int] = []
        original_flush = agent._flush_system_prompt
        agent._flush_system_prompt = (
            lambda *a, **k: (publishes.append(1), original_flush(*a, **k))[1]
        )

        summarize_calls: list[dict] = []

        def _sentinel_engine(_agent, args):  # pragma: no cover - must never run
            summarize_calls.append(args)
            raise AssertionError(
                "summary processing/provider replay was reached despite a "
                "reconstruction failure"
            )

        monkeypatch.setattr(context_tool, "_summarize_engine", _sentinel_engine)

        # Nonempty `items` makes the skipped stage unambiguous: with pending
        # summaries supplied, reaching step 4 at all would call the engine.
        result = context_tool.handle(
            agent,
            {
                "action": "rebuild",
                "input": {"items": [{"tool_call_id": "tc-1", "summary": "s"}]},
                "reasoning": "r",
            },
        )
        agent._flush_system_prompt = original_flush

        # Fail loud: the typed reconstruction error and no success marker.
        assert result["status"] == "error"
        assert result["reason"] == "context_reconstruction_failed"
        assert "prompt_reconstructed" not in result
        # Ordering proof: no final publish, and the summary/replay stage never ran.
        assert publishes == []
        assert summarize_calls == []
        # And no partially composed prompt was left behind.
        assert agent._prompt_manager.read_section("comment") == "CURRENT-COMMENT"
    finally:
        agent.stop(timeout=1.0)


def test_successful_rebuild_still_publishes_exactly_once(tmp_path):
    """The fail-loud path must not disturb the single-publish invariant."""
    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        system = agent._working_dir / "system"
        system.mkdir(exist_ok=True)
        (system / "pad.md").write_text("PAD-BODY", encoding="utf-8")
        (system / "lingtai.md").write_text("I AM", encoding="utf-8")

        publishes = []
        original = agent._flush_system_prompt

        def _counting(*args, **kwargs):
            publishes.append(1)
            return original(*args, **kwargs)

        agent._flush_system_prompt = _counting
        agent._reconstruct_context()
        agent._flush_system_prompt = original

        assert publishes == [1]
        read = agent._prompt_manager.read_section
        assert "PAD-BODY" in read("pad")
        assert "I AM" in read("character")
        assert read("skills")
        assert "knowledge" in agent._prompt_manager._sections
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# g6 — public `psyche` final naming (Telegram 1568/1570)
# ---------------------------------------------------------------------------
#
# The human contract is the equation `pad + lingtai + knowledge + skills =
# psyche`. Two things must hold at once and are easy to conflate: the ROOT name
# is reused, and the OLD family's ACTIONS are still gone. Root reuse is not
# action compatibility.

#: The OLD psyche family's actions. None may be reachable on the current root.
RETIRED_PSYCHE_ACTIONS = [
    "lingtai_update", "lingtai_load", "pad_edit", "pad_load", "pad_append",
    "context_molt", "name_set", "name_nickname", "molt", "summarize", "rebuild",
]


def test_public_root_is_psyche_and_substrate_is_absent(tmp_path):
    from lingtai.tools.registry import BUILTIN_TOOLS, INTRINSICS

    assert "psyche" in INTRINSICS
    assert "substrate" not in INTRINSICS
    assert "substrate" not in BUILTIN_TOOLS
    with pytest.raises(ImportError):
        importlib.import_module("lingtai.tools.substrate")

    agent = _agent(tmp_path)
    try:
        names = [s.name for s in agent._build_tool_schemas()]
        assert names.count("psyche") == 1
        assert "substrate" not in names
        assert "substrate" not in agent._intrinsics
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("retired", RETIRED_PSYCHE_ACTIONS)
def test_old_psyche_actions_are_rejected_on_the_new_root(tmp_path, retired):
    """Root reuse grants the dissolved family's actions nothing."""
    agent = _agent(tmp_path)
    try:
        assert retired not in psyche_tool.ACTION_ORDER
        assert retired not in psyche_tool.get_schema()["properties"]["action"]["enum"]
        result = _call(agent, retired)
        assert "Unknown psyche action" in result["error"]
    finally:
        agent.stop(timeout=1.0)


def test_no_returned_manual_teaches_a_public_substrate_root(tmp_path):
    """The retired public `substrate` root must not survive in any body."""
    agent = _agent(tmp_path, capabilities={"knowledge": {}, "skills": {}})
    try:
        for action in MANUAL_ACTIONS:
            body = _call(agent, action)["manual"]
            assert "substrate(action=" not in body, (
                f"{action} manual teaches the retired substrate(action=...) root"
            )
            assert "settings/substrate." not in body
    finally:
        agent.stop(timeout=1.0)


def test_substrate_prompt_section_is_untouched_by_the_rename(tmp_path):
    """The kernel-owned `substrate` prompt SECTION is a different concept.

    It keeps its name, its packaged source, its on-disk mirror, and its place in
    the composed prompt. Renaming the tool family must not have moved it.
    """
    agent = _agent(tmp_path)
    try:
        # Still a kernel-owned section in the canonical render order, between
        # `tools` and `procedures`.
        order = agent._prompt_manager._DEFAULT_ORDER
        assert "substrate" in order
        assert order.index("tools") < order.index("substrate") < order.index("procedures")
        # One full reconstruction composes it from the packaged source and
        # mirrors it to disk, exactly as before the family rename.
        agent._reconstruct_context()
        body = agent._prompt_manager.read_section("substrate")
        assert body and body.strip()
        mirror = agent._working_dir / "system" / "substrate.md"
        assert mirror.is_file() and mirror.read_text(encoding="utf-8").strip()
        assert body in agent._build_system_prompt()

        # A second rebuild recomposes and publishes it byte-identically.
        agent._reconstruct_context()
        assert agent._prompt_manager.read_section("substrate") == body
    finally:
        agent.stop(timeout=1.0)
