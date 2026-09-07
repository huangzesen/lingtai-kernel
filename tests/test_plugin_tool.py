"""End-to-end tests for the ``plugin`` capability (Agent Plugins v1.0.0).

Mirrors ``tests/test_mcp_capability.py`` and
``tests/test_tool_family_mcp_migration_parity.py``: the plugin capability is the
structural twin of ``mcp``, so the same facts are pinned here — family schema,
``info`` snapshot, unknown-action envelope, strict-empty action input, manual
adaptation — plus Plugin's redacted settings inventory and the facts unique to
this standard: §4.1 path containment
(``../`` and symlink escapes) and the two-tier mount contract.

That contract is the thing to keep straight while reading this file. A plugin
**declared** in ``manifest.plugins`` (or its alias
``manifest.capabilities.plugin.paths``) is *registered*: its validated skills
are listed in the protected Plugin field, not the vanilla skills catalog, and
its ``mcp.json`` servers join ``mcp_registry.jsonl`` stamped
``source="plugin:<name>"``. A plugin merely found on an inherited
``manifest.capabilities.skills.paths`` directory is *discovered*: listed in the
protected Plugin field, and nothing is registered. ``test_discovery_only_plugin_installs_nothing`` pins the second
half; the registration tests pin the first.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lingtai.agent import Agent
from lingtai.services.plugin_registry import (
    MCP_SCHEMA_URL,
    PLUGIN_SCHEMA_URL,
    declared_plugin_paths,
    plugin_source,
    read_plugin,
    read_plugins,
    register_plugins,
    resolve_contained,
    resolve_server_spec,
    validate_manifest,
)
from tests._service_helpers import make_gemini_mock_service as make_mock_service


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def _write_plugin(
    root: Path,
    name: str,
    *,
    manifest: dict | None = None,
    skills: list[str] | None = None,
    mcp_servers: dict | None = None,
) -> Path:
    """Materialize one Agent Plugins v1.0.0 plugin directory on disk."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    payload = manifest if manifest is not None else {
        "$schema": PLUGIN_SCHEMA_URL,
        "name": name,
        "version": "1.2.3",
        "description": f"{name} does a thing",
    }
    (plugin_dir / "plugin.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    for skill in skills or []:
        skill_dir = plugin_dir / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill}\ndescription: test skill\n---\nbody\n",
            encoding="utf-8",
        )
    if mcp_servers is not None:
        (plugin_dir / "mcp.json").write_text(
            json.dumps({"$schema": MCP_SCHEMA_URL, "mcpServers": mcp_servers}),
            encoding="utf-8",
        )
    return plugin_dir


def _mk_agent(
    tmp_path: Path,
    plugin_paths: list[str] | None = None,
    *,
    plugins: list[str] | None = None,
    skills_paths: list[str] | None = None,
    workdir: Path | None = None,
):
    """Build an agent. ``plugin_paths``/``plugins`` declare; ``skills_paths`` does not."""
    workdir = workdir if workdir is not None else tmp_path / "agent"
    caps: dict = {"plugin": {}}
    if plugin_paths is not None:
        caps["plugin"] = {"paths": plugin_paths}
    if skills_paths is not None:
        caps["skills"] = {"paths": skills_paths}
    return Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=caps,
        plugins=plugins,
    ), workdir


def _registry_lines(workdir: Path) -> list[dict]:
    path = workdir / "mcp_registry.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _info(agent) -> dict:
    return agent._tool_handlers["plugin"]({
        "action": "info", "input": {}, "reasoning": "inspect plugin state",
    })


def _settings(agent, action_input: dict | None = None) -> dict:
    return agent._tool_handlers["plugin"]({
        "action": "settings",
        "input": {} if action_input is None else action_input,
        "reasoning": "inspect plugin settings",
    })


def _refresh_plugins(agent, plugins: list[str]) -> None:
    """Re-run boot registration with a new declaration list, as refresh does.

    An agent holds an exclusive lease on its working directory, so a second
    ``Agent`` on the same directory cannot be constructed. This is the honest
    stand-in anyway: ``_setup_from_init`` calls exactly this before capability
    setup, which is precisely how an install or uninstall takes effect.
    """
    agent._register_declared_plugins(plugins, None)


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def test_validate_manifest_accepts_minimal_required_pair():
    ok, err = validate_manifest({"$schema": PLUGIN_SCHEMA_URL, "name": "my-plugin"})
    assert ok, err


def test_validate_manifest_rejects_missing_schema():
    ok, err = validate_manifest({"name": "my-plugin"})
    assert not ok
    assert "$schema" in err


def test_validate_manifest_rejects_unsupported_schema_version():
    ok, err = validate_manifest({
        "$schema": "https://agent-plugins.org/schemas/2.0.0/plugin.schema.json",
        "name": "my-plugin",
    })
    assert not ok
    assert "unsupported $schema" in err


def test_validate_manifest_rejects_missing_name():
    ok, err = validate_manifest({"$schema": PLUGIN_SCHEMA_URL})
    assert not ok
    assert "name" in err


@pytest.mark.parametrize("bad", ["My-Plugin", "-leading", "trailing-", "a--b", "a..b", ""])
def test_validate_manifest_rejects_names_outside_the_v1_grammar(bad):
    ok, err = validate_manifest({"$schema": PLUGIN_SCHEMA_URL, "name": bad})
    assert not ok, bad
    assert err


@pytest.mark.parametrize("good", ["my-plugin", "acme.tools", "lint3r", "a"])
def test_validate_manifest_accepts_names_inside_the_v1_grammar(good):
    ok, err = validate_manifest({"$schema": PLUGIN_SCHEMA_URL, "name": good})
    assert ok, (good, err)


def test_validate_manifest_type_checks_optional_fields():
    ok, err = validate_manifest({
        "$schema": PLUGIN_SCHEMA_URL, "name": "p", "keywords": "not-a-list",
    })
    assert not ok
    assert "keywords" in err


# ---------------------------------------------------------------------------
# §4.1 path containment
# ---------------------------------------------------------------------------

def test_containment_requires_dot_slash_prefix(tmp_path):
    resolved, err = resolve_contained(tmp_path, "data")
    assert resolved is None
    assert "must start with './'" in err


def test_containment_rejects_dotdot_escape(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    resolved, err = resolve_contained(root, "./../outside/server")
    assert resolved is None
    assert "escapes plugin root" in err


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation is privileged on Windows")
def test_containment_rejects_symlink_escape(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    resolved, err = resolve_contained(root, "./escape")
    assert resolved is None, "a symlink whose target leaves the plugin root must be rejected"
    assert "escapes plugin root" in err


def test_containment_accepts_a_path_inside_the_root(tmp_path):
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    resolved, err = resolve_contained(root, "./bin/server")
    assert err is None
    assert resolved == (root / "bin" / "server").resolve()


def test_containment_accepts_the_root_itself(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    resolved, err = resolve_contained(root, "./")
    assert err is None
    assert resolved == root.resolve()


# ---------------------------------------------------------------------------
# Failure boundaries
# ---------------------------------------------------------------------------

def test_invalid_manifest_rejects_whole_plugin(tmp_path):
    plugin_dir = _write_plugin(tmp_path, "broken", manifest={"name": "broken"})
    record, problems = read_plugin(plugin_dir)
    assert record is None, "a manifest missing $schema must reject the whole plugin"
    assert len(problems) == 1
    assert "$schema" in problems[0]["error"]


def test_unparseable_manifest_rejects_whole_plugin(tmp_path):
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text("{not json", encoding="utf-8")
    record, problems = read_plugin(plugin_dir)
    assert record is None
    assert "cannot read plugin.json" in problems[0]["error"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation is privileged on Windows")
def test_escaping_skill_is_skipped_but_plugin_survives(tmp_path):
    """Skipped must mean *not represented*, not merely reported as skipped.

    The escaping skill carries valid frontmatter on purpose: a `SKILL.md` the
    catalog would happily mount is the only version of this test that can tell
    "the containment gate held" apart from "the catalog rejected it for an
    unrelated reason".
    """
    plugin_dir = _write_plugin(tmp_path, "mixed", skills=["good"])
    outside = tmp_path / "elsewhere"
    (outside / "evil").mkdir(parents=True)
    (outside / "evil" / "SKILL.md").write_text(
        "---\nname: evil\ndescription: leaked from outside the plugin root\n---\n",
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "evil").symlink_to(outside / "evil", target_is_directory=True)

    record, problems = read_plugin(plugin_dir)
    assert record is not None, "a per-component escape must not reject the plugin"
    assert record["skills"] == ["good"]
    assert record["skill_count"] == 1
    assert any("evil" in p["error"] and "escapes plugin root" in p["error"] for p in problems)

    # The report is only worth as much as the mount it describes: the rejected
    # skill contributes no scan path, so it cannot re-enter through the parent.
    assert record["skill_paths"] == [str(plugin_dir / "skills" / "good")]
    assert not any("evil" in p for p in record["skill_paths"])


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation is privileged on Windows")
def test_escaping_skill_is_absent_from_the_plugin_field(tmp_path):
    """The F1 assertion the original test was missing: drive the protected field.

    A skill reported in `skipped` and injected into the system prompt anyway is
    worse than no boundary at all, because the snapshot an operator audits says
    the opposite of what the model can see.
    """
    from lingtai.tools import skills as skillsmod

    plugin_dir = _write_plugin(tmp_path / "plugins", "mixed", skills=["good"])
    outside = tmp_path / "elsewhere"
    (outside / "evil").mkdir(parents=True)
    (outside / "evil" / "SKILL.md").write_text(
        "---\nname: leaked\ndescription: read from outside the plugin root\n---\n",
        encoding="utf-8",
    )
    (plugin_dir / "skills" / "evil").symlink_to(outside / "evil", target_is_directory=True)

    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)], skills_paths=[])
    entry = _info(agent)["registered"][0]
    assert entry["skills"] == ["good"]
    assert any("escapes plugin root" in s["reason"] for s in entry["skipped"])

    # Drive the real reconcile — the plugin field is a closed namespace,
    # not the vanilla skills catalog. Plugin skills stay inside the plugin;
    # only the protected field lists them (as <skill_names>).
    skillsmod._reconcile(agent, [])
    plugin_body = _prompt_section_named(agent, "plugin")
    assert "<name>mixed</name>" in plugin_body
    assert "<skill_names>good</skill_names>" in plugin_body
    assert "leaked" not in plugin_body, "a skipped skill must not reach the prompt"
    assert str(outside) not in plugin_body

    skills_body = _prompt_section_named(agent, "skills")
    assert "name: good" not in skills_body, "plugin skills stay out of the vanilla catalog"


def test_nested_skills_are_reported_exactly_as_they_are_listed(tmp_path):
    """F2: `skills/group/nested/SKILL.md` is reported in the plugin field."""
    plugin_dir = _write_plugin(tmp_path / "plugins", "deep")
    nested = plugin_dir / "skills" / "group" / "nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: nested\ndescription: a nested skill\n---\n", encoding="utf-8",
    )
    top = plugin_dir / "skills" / "top"
    top.mkdir(parents=True)
    (top / "SKILL.md").write_text(
        "---\nname: top\ndescription: a top-level skill\n---\n", encoding="utf-8",
    )

    record, problems = read_plugin(plugin_dir)
    assert record is not None
    assert record["skills"] == ["group/nested", "top"]
    assert record["skill_count"] == 2, "the count must match what the protected field lists"
    assert problems == []

    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)], skills_paths=[])
    body = _prompt_section_named(agent, "plugin")
    assert "<skill_names>group/nested, top</skill_names>" in body
    assert "<skills>2</skills>" in body
    entry = _info(agent)["registered"][0]
    assert entry["skill_count"] == 2


def test_escaping_mcp_command_is_skipped_but_plugin_survives(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path,
        "mixed",
        mcp_servers={
            "ok": {"type": "stdio", "command": "./bin/server"},
            "evil": {"type": "stdio", "command": "./../../bin/server"},
        },
    )
    record, problems = read_plugin(plugin_dir)
    assert record is not None
    assert record["mcp_servers"] == ["ok"]
    assert any("evil" in p["error"] for p in problems)


def test_mcp_json_with_wrong_schema_yields_a_problem_not_a_rejection(tmp_path):
    plugin_dir = _write_plugin(tmp_path, "p")
    (plugin_dir / "mcp.json").write_text(
        json.dumps({"$schema": "https://example.com/other.json", "mcpServers": {}}),
        encoding="utf-8",
    )
    record, problems = read_plugin(plugin_dir)
    assert record is not None
    assert record["mcp_server_count"] == 0
    assert any("unsupported mcp.json $schema" in p["error"] for p in problems)


# ---------------------------------------------------------------------------
# Multi-path scanning
# ---------------------------------------------------------------------------

def test_read_plugins_reports_per_path_health(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha", skills=["a", "b"])
    _write_plugin(root, "beta", mcp_servers={"s": {"type": "stdio", "command": "node"}})

    records, problems, report = read_plugins(tmp_path, [str(root), str(tmp_path / "missing")])
    assert [r["name"] for r in records] == ["alpha", "beta"]
    assert records[0]["skill_count"] == 2
    assert records[1]["mcp_server_count"] == 1
    assert not problems
    assert report[str(root)] == {"resolved": str(root), "exists": True, "plugins": 2}
    assert report[str(tmp_path / "missing")]["exists"] is False


def test_read_plugins_treats_a_manifest_bearing_path_as_one_plugin(tmp_path):
    plugin_dir = _write_plugin(tmp_path, "solo")
    records, _problems, report = read_plugins(tmp_path, [str(plugin_dir)])
    assert [r["name"] for r in records] == ["solo"]
    assert report[str(plugin_dir)]["plugins"] == 1


def test_read_plugins_flags_duplicate_names_first_wins(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    _write_plugin(first, "dup")
    _write_plugin(second, "dup")
    records, problems, _report = read_plugins(tmp_path, [str(first), str(second)])
    assert len(records) == 1
    assert records[0]["source"] == str(first / "dup")
    assert any("duplicate plugin name" in p["error"] for p in problems)


# ---------------------------------------------------------------------------
# Capability wiring / prompt rendering
# ---------------------------------------------------------------------------

def test_plugin_is_registered_and_default_on():
    from lingtai.tools.registry import BUILTIN_TOOLS, CORE_DEFAULTS

    assert BUILTIN_TOOLS["plugin"] == "lingtai.tools.plugin"
    assert "plugin" in CORE_DEFAULTS


def _prompt_section(agent) -> str:
    section = agent._prompt_manager._sections.get("plugin")
    assert section is not None
    return section["content"] if isinstance(section, dict) else str(section)


def test_plugin_capability_renders_catalog_into_prompt(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha", skills=["a"], mcp_servers={"s": {"type": "stdio", "command": "node"}})
    agent, _workdir = _mk_agent(tmp_path, [str(root)])

    body = _prompt_section(agent)
    assert "<registered_plugin>" in body
    assert "<name>alpha</name>" in body
    assert "<version>1.2.3</version>" in body
    assert "<skills>1</skills>" in body
    assert "<mcp_servers>1</mcp_servers>" in body
    # Declared, so mounted — and the prompt says which servers actually took a
    # registry record, not just how many the manifest declared.
    assert "<mount>registered</mount>" in body
    assert "<mcp_registered>s</mcp_registered>" in body
    # The preamble must state both halves of the two-tier contract, including
    # the one thing registration deliberately does NOT do.
    assert "NOT running" in body
    assert "NOT registered" in body


def test_prompt_marks_an_inherited_plugin_as_discovered_only(tmp_path):
    root = tmp_path / "shared"
    _write_plugin(root, "seen", skills=["a"], mcp_servers={"s": {"type": "stdio", "command": "node"}})
    agent, _workdir = _mk_agent(tmp_path, skills_paths=[str(root)])

    # Split the preamble off: it names both mount values by construction, so
    # only the catalog body can answer which tier this plugin landed in.
    catalog = _prompt_section(agent).split("<registered_plugin>", 1)[1]
    assert "<name>seen</name>" in catalog
    assert "<mount>discovered</mount>" in catalog
    assert "<mount>registered</mount>" not in catalog
    # A discovered plugin advertises no registered servers at all.
    assert "<mcp_registered>" not in catalog


def test_empty_catalog_renders_a_resident_section(tmp_path):
    """The plugins field is resident: with nothing declared it still explains itself."""
    agent, _workdir = _mk_agent(tmp_path, [str(tmp_path / "nothing-here")])
    section = agent._prompt_manager._sections.get("plugin")
    assert section is not None
    body = section["content"] if isinstance(section, dict) else str(section)
    assert "No Agent Plugins" in body
    assert "plugin/<name>/plugin.json" in body


def test_plugin_inherits_skills_capability_paths_for_discovery_only(tmp_path):
    """A directory declared for skills is scanned for plugins — but not mounted."""
    root = tmp_path / "shared"
    _write_plugin(root, "inherited")
    agent, _workdir = _mk_agent(tmp_path, skills_paths=[str(root)])

    result = _info(agent)
    # Visible, because plugins land where skills land...
    assert [r["name"] for r in result["discovered"]] == ["inherited"]
    assert str(root) in result["paths"]
    # ...but never registered: inheritance is not a declaration.
    assert result["registered"] == []
    assert result["declared"] == []


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

def test_description_states_the_two_tier_mount_contract():
    from lingtai.tools.plugin import get_description

    desc = get_description()
    assert desc.startswith("READ-ONLY:")
    assert "agent-plugins.org" in desc
    assert "<registered_plugin>" in desc
    # Both tiers, the registry stamp, and the boundary registration stops at.
    assert "manifest.plugins" in desc
    assert 'source="plugin:<name>"' in desc
    assert "registered but NOT running" in desc
    assert "discovered" in desc
    # Install and uninstall are both declaration edits, and the description
    # must say so — there is no install action to reach for.
    assert 'system(action="refresh")' in desc
    assert "uninstall" in desc


def test_schema_exposes_exact_public_actions_and_envelope():
    from lingtai.tools.plugin import get_schema

    schema = get_schema()
    assert schema["properties"]["action"]["enum"] == [
        "info", "settings", "manual",
    ]
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema.get("additionalProperties") is False
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert [b["title"] for b in schema["properties"]["input"]["anyOf"]] == [
        "info input", "settings inventory input", "manual input",
    ]
    action_desc = schema["properties"]["action"]["description"]
    assert "info: read-only action" in action_desc
    assert "No action registers or unregisters anything" in action_desc


def test_all_actions_declare_strict_empty_input():
    from lingtai.tools.plugin import get_schema
    from lingtai.tools.tool_family.manual import MANUAL_INPUT_SCHEMA

    schema = get_schema()
    branches = {
        branch["title"]: branch
        for branch in schema["properties"]["input"]["anyOf"]
    }
    for title in ("info input", "settings inventory input", "manual input"):
        branch = branches[title]
        assert branch["type"] == "object"
        assert branch.get("properties", {}) == MANUAL_INPUT_SCHEMA.get("properties", {})
        assert branch.get("additionalProperties") is False
    assert branches["settings inventory input"]["required"] == []


def test_schema_only_and_dispatching_families_declare_identical_children(tmp_path):
    from lingtai.adapters.tool_plugin_host import agent_host_ports
    from lingtai.kernel.tool_plugin import ToolPluginHost
    from lingtai.tools.plugin import DECLARATION, _FAMILY, _build_family

    agent, _workdir = _mk_agent(tmp_path)
    host = ToolPluginHost.grant(
        DECLARATION, agent_host_ports(agent, DECLARATION.name)
    )
    assert _build_family(host).child_names == _FAMILY.child_names == (
        "info", "settings", "manual",
    )


def test_plugin_catalog_state_is_detached_per_read():
    """A caller cannot mutate the host registration snapshot through a read."""
    from lingtai.adapters.tool_plugin_host import AgentPluginCatalogAdapter

    registration = {
        "declared": ["./alpha"],
        "plugins": [{"name": "alpha", "skills": ["greeter"]}],
    }
    capabilities = [
        ("plugin", {"paths": ["./plugins"]}),
        ("skills", {"paths": ["./skills"]}),
    ]
    adapter = AgentPluginCatalogAdapter(lambda: registration, lambda: capabilities)

    first = adapter.read_state()
    first.registration["declared"].append("./mutated")
    first.registration["plugins"][0]["name"] = "mutated"
    second = adapter.read_state()

    assert second.registration == registration
    assert second.configured_paths == ("./plugins",)
    assert second.skill_paths == ("./skills",)
    assert second.skills_enabled is True


def test_settings_exact_redacted_inventory_failure_and_unchanged_info(
    tmp_path, monkeypatch,
):
    from lingtai.adapters.tool_plugin_host import AgentPluginCatalogAdapter
    from lingtai.services import plugin_registry
    from lingtai.tools.plugin.settings import plugin_setting_rows
    from lingtai.tools.tool_family import SettingRow

    workdir = tmp_path / "agent"
    _write_plugin(workdir / "plugin", "automatic")
    canonical = _write_plugin(
        tmp_path / "packages",
        "canonical",
        mcp_servers={
            "canonical-server": {"type": "stdio", "command": "node"},
        },
    )
    alias = _write_plugin(tmp_path / "aliases", "alias")
    agent, _workdir = _mk_agent(
        tmp_path,
        plugin_paths=[str(canonical), str(alias)],
        plugins=[str(canonical)],
        workdir=workdir,
    )
    registry_before = _registry_lines(workdir)

    catalog = AgentPluginCatalogAdapter(
        lambda: agent._plugin_registration,
        lambda: agent._capabilities,
    )
    expected_configured = [str(canonical), str(alias)]
    assert plugin_setting_rows(catalog) == (
        SettingRow(
            "manifest.plugins",
            expected_configured,
            [],
            True,
            "plugin-manual#plugin-registration-roots",
            _sensitive=True,
        ),
    )

    with monkeypatch.context() as patch:
        def unexpected_scan(*_args, **_kwargs):
            raise AssertionError("settings must not scan plugin paths")

        patch.setattr(plugin_registry, "read_plugins", unexpected_scan)
        result = _settings(agent)

    expected_row = {
        "key": "manifest.plugins",
        "current": "<redacted>",
        "default": "<redacted>",
        "configurable": True,
        "comment": "plugin-manual#plugin-registration-roots",
    }
    assert result == {"settings": [expected_row]}
    assert list(result["settings"][0]) == [
        "key", "current", "default", "configurable", "comment",
    ]
    assert agent._plugin_registration["configured_declared"] == expected_configured
    assert str(workdir / "plugin") in agent._plugin_registration["declared"]
    assert _registry_lines(workdir) == registry_before

    info = _info(agent)
    assert info["status"] == "ok"
    assert {item["name"] for item in info["registered"]} == {
        "automatic", "canonical", "alias",
    }

    invalid = _settings(agent, {"set": "manifest.plugins"})
    assert invalid["status"] == "failed"
    assert invalid["error_code"] == "INVALID_ARGUMENT"

    agent._plugin_registration = {"declared": [str(canonical)]}
    assert _settings(agent) == {
        "status": "failed",
        "error_code": "SETTINGS_UNAVAILABLE",
        "message": "settings inventory is unavailable",
    }
    assert _registry_lines(workdir) == registry_before


def test_info_returns_catalog_snapshot(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha", skills=["a"])
    agent, _workdir = _mk_agent(tmp_path, [str(root)])

    result = agent._tool_handlers["plugin"]({
        "action": "info", "input": {}, "reasoning": "list plugins",
    })
    assert result["status"] == "ok"
    assert result["registered_count"] == 1
    entry = result["registered"][0]
    assert entry["name"] == "alpha"
    assert entry["version"] == "1.2.3"
    assert entry["skill_count"] == 1
    assert entry["source"] == str(root / "alpha")
    assert result["problems"] == []
    assert "plugin_manual" not in result


def test_manual_returns_the_installed_body(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    result = agent._tool_handlers["plugin"]({
        "action": "manual", "input": {}, "reasoning": "load plugin guidance",
    })
    manual_path = (
        workdir / ".library" / "intrinsic" / "capabilities" / "plugin" / "SKILL.md"
    )
    assert result["status"] == "ok"
    assert result["manual_path"] == str(manual_path)
    assert result["plugin_manual"] == manual_path.read_text(encoding="utf-8")
    # The Host adapter runs after dispatch: no canonical child fields leak out.
    assert "content" not in result and "structuredContent" not in result


def test_unknown_action_returns_error_envelope(tmp_path):
    agent, _workdir = _mk_agent(tmp_path)
    handler = agent._tool_handlers["plugin"]

    assert handler({"action": "install"}) == {
        "status": "error",
        "message": "unknown action: 'install', only 'info' or 'settings' or 'manual' is supported",
    }
    # Missing action key renders the empty-string default, not None.
    assert handler({}) == {
        "status": "error",
        "message": "unknown action: '', only 'info' or 'settings' or 'manual' is supported",
    }
    # Invalid JSON can make `action` unhashable (issue #513): the router must
    # render the unknown-action envelope, not raise TypeError.
    assert handler({"action": []}) == {
        "status": "error",
        "message": "unknown action: [], only 'info' or 'settings' or 'manual' is supported",
    }
    assert handler({"action": {}}) == {
        "status": "error",
        "message": "unknown action: {}, only 'info' or 'settings' or 'manual' is supported",
    }


def test_extra_input_field_is_rejected(tmp_path):
    agent, _workdir = _mk_agent(tmp_path)
    result = agent._tool_handlers["plugin"]({
        "action": "info", "input": {"name": "alpha"}, "reasoning": "smuggle a field",
    })
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_non_boolean_summarize_is_rejected(tmp_path):
    agent, _workdir = _mk_agent(tmp_path)
    result = agent._tool_handlers["plugin"]({
        "action": "info", "input": {}, "reasoning": "check", "summarize": "yes",
    })
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# Declaration parsing — the canonical key and its alias
# ---------------------------------------------------------------------------

def test_manifest_plugins_is_the_canonical_declaration_key():
    assert declared_plugin_paths(["/a", "/b"]) == ["/a", "/b"]


def test_capability_paths_alias_is_still_honored():
    """The key PR #1232 shipped keeps working and means the same thing."""
    assert declared_plugin_paths(None, {"plugin": {"paths": ["/legacy"]}}) == ["/legacy"]


def test_canonical_key_comes_first_and_duplicates_collapse():
    declared = declared_plugin_paths(["/a", "/dup"], {"plugin": {"paths": ["/dup", "/b"]}})
    assert declared == ["/a", "/dup", "/b"]


@pytest.mark.parametrize("junk", [None, "not-a-list", [1, 2], [""], {}])
def test_declaration_parsing_ignores_junk(junk):
    assert declared_plugin_paths(junk, {"plugin": junk}) == []


def test_manifest_plugins_is_a_known_manifest_field():
    from lingtai.init_schema import MANIFEST_KNOWN, MANIFEST_OPTIONAL

    assert MANIFEST_OPTIONAL["plugins"] is list
    assert "plugins" in MANIFEST_KNOWN


def _init_data(**manifest_extra) -> dict:
    manifest = {"llm": {"provider": "gemini", "model": "gemini-2.5-flash"}}
    manifest.update(manifest_extra)
    return {"manifest": manifest, "covenant": "be good", "pad": ""}


def test_a_well_formed_plugins_list_raises_no_warning():
    from lingtai.init_schema import validate_init

    warnings = validate_init(_init_data(plugins=["./plugins/hello"]))
    assert not [w for w in warnings if "plugins" in w]


def test_non_string_plugin_entries_warn_without_failing_boot():
    from lingtai.init_schema import validate_init

    warnings = validate_init(_init_data(plugins=[7]))
    assert any("manifest.plugins" in w for w in warnings)


# ---------------------------------------------------------------------------
# Registration — protected Plugin field
# ---------------------------------------------------------------------------

def _prompt_section_named(agent, name: str) -> str:
    section = agent._prompt_manager._sections.get(name)
    if section is None:
        return ""
    return section["content"] if isinstance(section, dict) else str(section)


def test_declared_plugin_skill_appears_in_the_plugin_field(tmp_path):
    """The mount that matters: the skill shows up in the plugins field."""
    plugin_dir = _write_plugin(tmp_path / "plugins", "alpha", skills=["greeter"])
    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)], skills_paths=[])

    body = _prompt_section_named(agent, "plugin")
    assert "<name>alpha</name>" in body
    assert "<skill_names>greeter</skill_names>" in body
    # Closed namespace: the vanilla skills catalog does not compose it in.
    assert "name: greeter" not in _prompt_section_named(agent, "skills")
    # The plugin stays the visible source; <source> points inside the plugin.
    assert str(plugin_dir) in body


def test_an_inherited_plugins_skill_stays_out_of_the_catalog(tmp_path):
    root = tmp_path / "shared"
    _write_plugin(root, "alpha", skills=["greeter"])
    # The skills path is the *collection* dir's parent, so the plugin's skills/
    # is not itself a declared scan root — only registration would reach it.
    agent, _workdir = _mk_agent(tmp_path, skills_paths=[str(tmp_path / "elsewhere")])
    assert "name: greeter" not in _prompt_section_named(agent, "skills")


def test_registration_records_each_validated_skill_dir_on_the_agent(tmp_path):
    """Per-skill paths, never the parent — the parent would re-admit a reject."""
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha", skills=["greeter", "farewell"],
    )
    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    assert agent._plugin_skill_paths == [
        str(plugin_dir / "skills" / "farewell"),
        str(plugin_dir / "skills" / "greeter"),
    ]
    assert str(plugin_dir / "skills") not in agent._plugin_skill_paths


def test_a_skill_less_plugin_contributes_no_scan_path(tmp_path):
    plugin_dir = _write_plugin(tmp_path / "plugins", "bare")
    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    assert agent._plugin_skill_paths == []


def test_snapshot_reports_skills_unmounted_when_the_capability_is_off(tmp_path):
    plugin_dir = _write_plugin(tmp_path / "plugins", "alpha", skills=["greeter"])
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"plugin": {}, "skills": None},
        plugins=[str(plugin_dir)],
    )
    entry = _info(agent)["registered"][0]
    assert entry["skills"] == ["greeter"]
    assert entry["skills_mounted"] is False
    assert any(s["component"] == "skills/" for s in entry["skipped"])


# ---------------------------------------------------------------------------
# Registration — mcp.json mount
# ---------------------------------------------------------------------------

def test_declared_plugin_mcp_server_lands_in_the_registry(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={"greeter": {"type": "stdio", "command": "./bin/serve", "args": ["--x"]}},
    )
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])

    records = _registry_lines(workdir)
    assert len(records) == 1
    record = records[0]
    assert record["name"] == "greeter"
    assert record["transport"] == "stdio"
    # The source stamp is what makes uninstall mechanical.
    assert record["source"] == plugin_source("alpha") == "plugin:alpha"
    # §4.1 paths are resolved to absolute, so the record is usable as written.
    assert record["command"] == str(plugin_dir / "bin" / "serve")
    assert record["args"] == ["--x"]

    entry = _info(agent)["registered"][0]
    assert entry["mcp_registered"] == ["greeter"]


def test_registration_is_idempotent(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={"greeter": {"type": "stdio", "command": "node"}},
    )
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    _refresh_plugins(agent, [str(plugin_dir)])
    _refresh_plugins(agent, [str(plugin_dir)])
    assert [r["name"] for r in _registry_lines(workdir)] == ["greeter"]


def test_a_changed_spec_replaces_the_record_rather_than_duplicating_it(tmp_path):
    plugins_root = tmp_path / "plugins"
    _write_plugin(plugins_root, "alpha", mcp_servers={"greeter": {"type": "stdio", "command": "node"}})
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugins_root / "alpha")])

    _write_plugin(plugins_root, "alpha", mcp_servers={"greeter": {"type": "stdio", "command": "deno"}})
    _refresh_plugins(agent, [str(plugins_root / "alpha")])

    records = _registry_lines(workdir)
    assert len(records) == 1
    assert records[0]["command"] == "deno"


def test_streamable_http_and_sse_map_onto_the_registry_http_transport(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={
            "remote": {"type": "streamable-http", "url": "https://example.com/mcp"},
            "streamy": {"type": "sse", "url": "https://example.com/sse"},
        },
    )
    _agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    by_name = {r["name"]: r for r in _registry_lines(workdir)}
    assert by_name["remote"]["transport"] == "http"
    assert by_name["streamy"]["transport"] == "http"
    assert by_name["remote"]["url"] == "https://example.com/mcp"


def test_a_server_name_outside_the_registry_grammar_is_skipped_with_a_reason(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={
            "Greeter": {"type": "stdio", "command": "node"},
            "ok": {"type": "stdio", "command": "node"},
        },
    )
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])

    assert [r["name"] for r in _registry_lines(workdir)] == ["ok"]
    entry = _info(agent)["registered"][0]
    assert entry["mcp_registered"] == ["ok"]
    assert any(
        s["component"] == "mcp:Greeter" and "invalid name" in s["reason"]
        for s in entry["skipped"]
    )


def test_registration_never_overwrites_a_record_it_does_not_own(tmp_path):
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    (workdir / "mcp_registry.jsonl").write_text(
        json.dumps({
            "name": "greeter", "summary": "hand written", "transport": "stdio",
            "command": "mine", "args": [], "source": "human",
        }) + "\n",
        encoding="utf-8",
    )
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={"greeter": {"type": "stdio", "command": "node"}},
    )
    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)], workdir=workdir)

    records = _registry_lines(workdir)
    assert len(records) == 1
    assert records[0]["command"] == "mine", "a hand-written record must survive"
    entry = _info(agent)["registered"][0]
    assert entry["mcp_registered"] == []
    assert any("already registered by 'human'" in s["reason"] for s in entry["skipped"])


def test_two_plugins_claiming_one_server_name_skip_the_second(tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha", mcp_servers={"greeter": {"type": "stdio", "command": "a"}})
    _write_plugin(root, "beta", mcp_servers={"greeter": {"type": "stdio", "command": "b"}})
    agent, workdir = _mk_agent(tmp_path, plugins=[str(root / "alpha"), str(root / "beta")])

    records = _registry_lines(workdir)
    assert len(records) == 1
    assert records[0]["source"] == "plugin:alpha"
    beta = [p for p in _info(agent)["registered"] if p["name"] == "beta"][0]
    assert beta["mcp_registered"] == []
    assert any("already claimed by plugin 'alpha'" in s["reason"] for s in beta["skipped"])


# ---------------------------------------------------------------------------
# Registration — §4.1 containment is enforced at registration, not just display
# ---------------------------------------------------------------------------

def test_path_escape_is_rejected_at_registration_time(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={
            "ok": {"type": "stdio", "command": "node"},
            "evil": {"type": "stdio", "command": "./../../usr/bin/evil"},
        },
    )
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])

    # The escaping server never reaches the registry at all.
    assert [r["name"] for r in _registry_lines(workdir)] == ["ok"]
    result = _info(agent)
    assert result["registered"][0]["mcp_registered"] == ["ok"]
    assert any("escapes plugin root" in p["error"] for p in result["problems"])


def test_containment_covers_args_not_just_command(tmp_path):
    """A server can smuggle a path through `args`; §4.1 must cover it too."""
    root = tmp_path / "plugin"
    root.mkdir()
    resolved, err = resolve_server_spec(
        root, {"type": "stdio", "command": "node", "args": ["./../escape.js"]}
    )
    assert resolved is None
    assert "args[0]" in err and "escapes plugin root" in err


@pytest.mark.parametrize("escape", ["../../bin/sh", "..", "sub/../../../bin/sh"])
def test_containment_is_not_bypassed_by_omitting_the_dot_slash(tmp_path, escape):
    """F5: `../x` must be gated exactly like `./../x`, not passed through."""
    root = tmp_path / "plugin"
    root.mkdir()
    resolved, err = resolve_server_spec(root, {"type": "stdio", "command": escape})
    assert resolved is None, f"{escape!r} escaped the containment gate"
    assert "escapes plugin root" in err
    assert repr(escape) in err, "the reason must name the value as declared"

    in_args, err = resolve_server_spec(
        root, {"type": "stdio", "command": "node", "args": [escape]}
    )
    assert in_args is None
    assert "args[0]" in err and "escapes plugin root" in err


def test_relative_dotdot_that_stays_inside_resolves_rather_than_being_rejected(tmp_path):
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    resolved, err = resolve_server_spec(
        root, {"type": "stdio", "command": "bin/../bin/serve"}
    )
    assert err is None
    assert resolved["command"] == str(root / "bin" / "serve")


@pytest.mark.parametrize("passthrough", ["node", "-c", "/usr/bin/env", "${HOME}/x"])
def test_values_that_are_not_plugin_relative_paths_still_pass_through(tmp_path, passthrough):
    """The stated rule, pinned: no `..` segment and not `./` means untouched."""
    root = tmp_path / "plugin"
    root.mkdir()
    resolved, err = resolve_server_spec(
        root, {"type": "stdio", "command": "node", "args": [passthrough]}
    )
    assert err is None
    assert resolved["args"] == [passthrough]


def test_env_and_cwd_are_carried_into_the_registry_record(tmp_path):
    """F4: a field the manual tells authors to write must survive to the record."""
    plugin_dir = _write_plugin(tmp_path / "plugins", "alpha", mcp_servers={
        "greeter": {
            "type": "stdio",
            "command": "${PLUGIN_ROOT}/run.sh",
            "env": {"TOKEN": "x"},
            "cwd": "./sub",
        },
    })
    (plugin_dir / "sub").mkdir()
    _agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])

    record = _registry_lines(workdir)[0]
    assert record["env"] == {"TOKEN": "x"}
    assert record["cwd"] == str(plugin_dir / "sub"), "cwd is resolved, then carried"
    assert record["command"] == str(plugin_dir / "run.sh")


def test_http_headers_are_validated_and_carried(tmp_path):
    plugin_dir = _write_plugin(tmp_path / "plugins", "alpha", mcp_servers={
        "remote": {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer x"},
        },
    })
    _agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    assert _registry_lines(workdir)[0]["headers"] == {"Authorization": "Bearer x"}


@pytest.mark.parametrize("field", ["env", "headers"])
def test_non_string_env_or_header_values_skip_the_server(tmp_path, field):
    root = tmp_path / "plugin"
    root.mkdir()
    spec = {"type": "stdio", "command": "node", field: {"A": 1}}
    resolved, err = resolve_server_spec(root, spec)
    assert resolved is None
    assert f"{field} must map strings to strings" == err


def test_carried_fields_do_not_break_idempotence(tmp_path):
    """A record with extra keys must still compare equal on the next boot."""
    plugin_dir = _write_plugin(tmp_path / "plugins", "alpha", mcp_servers={
        "greeter": {"type": "stdio", "command": "node", "env": {"A": "b"}},
    })
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    _refresh_plugins(agent, [str(plugin_dir)])
    assert agent._plugin_registration["mcp_appended"] == []
    assert agent._plugin_registration["mcp_pruned"] == []
    assert len(_registry_lines(workdir)) == 1


def test_plugin_root_placeholder_expands_and_is_contained(tmp_path):
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    resolved, err = resolve_server_spec(
        root, {"type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/bin/serve.py"]}
    )
    assert err is None
    assert resolved["args"] == [str(root / "bin" / "serve.py")]

    escaping, err = resolve_server_spec(
        root, {"type": "stdio", "command": "${PLUGIN_ROOT}/../outside"}
    )
    assert escaping is None
    assert "escapes plugin root" in err


def test_a_bare_executable_token_passes_through_untouched(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    resolved, err = resolve_server_spec(root, {"type": "stdio", "command": "node"})
    assert err is None
    assert resolved["command"] == "node"


def test_an_invalid_manifest_registers_nothing(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "broken",
        manifest={"name": "broken"},
        mcp_servers={"greeter": {"type": "stdio", "command": "node"}},
    )
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    assert _registry_lines(workdir) == []
    result = _info(agent)
    assert result["registered"] == []
    assert any("$schema" in p["error"] for p in result["problems"])


# ---------------------------------------------------------------------------
# Uninstall — removing the declaration removes the mount
# ---------------------------------------------------------------------------

def test_undeclaring_a_plugin_prunes_its_registry_records(tmp_path):
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        skills=["greeter"],
        mcp_servers={"greeter": {"type": "stdio", "command": "node"}},
    )
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)])
    assert [r["name"] for r in _registry_lines(workdir)] == ["greeter"]

    # Uninstall == drop it from manifest.plugins and refresh. No uninstall
    # action exists, and none is needed.
    _refresh_plugins(agent, [])
    assert _registry_lines(workdir) == []
    assert agent._plugin_skill_paths == []
    assert agent._plugin_registration["mcp_pruned"] == ["greeter"]


def test_uninstalling_removes_the_skill_from_the_plugin_field(tmp_path):
    plugin_dir = _write_plugin(tmp_path / "plugins", "alpha", skills=["greeter"])
    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)], skills_paths=[])
    assert "<skill_names>greeter</skill_names>" in _prompt_section_named(agent, "plugin")

    _refresh_plugins(agent, [])
    _info(agent)
    assert "greeter" not in _prompt_section_named(agent, "plugin")


def test_pruning_leaves_records_the_plugin_system_does_not_own(tmp_path):
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    keep = json.dumps({
        "name": "curated", "summary": "an addon", "transport": "stdio",
        "command": "node", "args": [], "source": "catalog",
    })
    (workdir / "mcp_registry.jsonl").write_text(keep + "\n", encoding="utf-8")
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={"greeter": {"type": "stdio", "command": "node"}},
    )
    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)], workdir=workdir)
    assert len(_registry_lines(workdir)) == 2

    _refresh_plugins(agent, [])
    assert [r["name"] for r in _registry_lines(workdir)] == ["curated"]


def test_pruning_preserves_unparseable_lines_rather_than_rewriting_the_file(tmp_path):
    """Pruning is a targeted removal; a human's broken line is not ours to drop."""
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    (workdir / "mcp_registry.jsonl").write_text("{broken json\n", encoding="utf-8")
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        mcp_servers={"greeter": {"type": "stdio", "command": "node"}},
    )
    agent, _workdir = _mk_agent(tmp_path, plugins=[str(plugin_dir)], workdir=workdir)
    _refresh_plugins(agent, [])

    text = (workdir / "mcp_registry.jsonl").read_text(encoding="utf-8")
    assert "{broken json" in text
    assert "greeter" not in text


def test_dropping_a_server_from_mcp_json_prunes_just_that_record(tmp_path):
    plugins_root = tmp_path / "plugins"
    _write_plugin(plugins_root, "alpha", mcp_servers={
        "one": {"type": "stdio", "command": "node"},
        "two": {"type": "stdio", "command": "node"},
    })
    agent, workdir = _mk_agent(tmp_path, plugins=[str(plugins_root / "alpha")])
    assert len(_registry_lines(workdir)) == 2

    _write_plugin(plugins_root, "alpha", mcp_servers={"one": {"type": "stdio", "command": "node"}})
    _refresh_plugins(agent, [str(plugins_root / "alpha")])
    assert [r["name"] for r in _registry_lines(workdir)] == ["one"]


def test_registering_nothing_on_a_clean_agent_creates_no_registry(tmp_path):
    _agent, workdir = _mk_agent(tmp_path)
    assert not (workdir / "mcp_registry.jsonl").exists()


# ---------------------------------------------------------------------------
# Boot flow — one registration per boot, not a prune followed by a re-append
# ---------------------------------------------------------------------------

def _plugin_record_line(workdir: Path) -> None:
    (workdir / "mcp_registry.jsonl").write_text(
        json.dumps({
            "name": "greeter", "summary": "from a plugin", "transport": "stdio",
            "command": "node", "args": [], "source": "plugin:alpha",
        }) + "\n",
        encoding="utf-8",
    )


def test_minimal_construction_does_not_wipe_plugin_records(tmp_path):
    """F3: a caller who declared nothing has made no uninstall decision.

    This direct ``Agent`` construction supplies neither ``capabilities=`` nor
    ``plugins=``. The CLI instead creates a private shell before its configured
    ``_setup_from_init`` pass. Treating this direct caller's silence as "no
    plugins are declared" pruned every plugin-owned record on the way to
    re-registering it, one boot at a time.
    """
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    _plugin_record_line(workdir)

    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=workdir)
    assert [r["name"] for r in _registry_lines(workdir)] == ["greeter"]
    assert agent._plugin_registration == {}
    assert agent._plugin_skill_paths == []


def test_declaring_capabilities_without_plugins_still_uninstalls(tmp_path):
    """The gate must not cost the uninstall semantic it protects."""
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    _plugin_record_line(workdir)

    agent, _workdir = _mk_agent(tmp_path, plugins=None, workdir=workdir)
    assert _registry_lines(workdir) == []
    assert agent._plugin_registration["mcp_pruned"] == ["greeter"]


def _events(workdir: Path, event_type: str) -> list[dict]:
    path = workdir / "logs" / "events.jsonl"
    if not path.is_file():
        return []
    return [
        e for e in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if e.get("type") == event_type
    ]


def _cli_shaped_init(plugins: list[str]) -> dict:
    """A minimal valid init.json declaring plugins, shaped as `cli.run` reads it."""
    return {
        "manifest": {
            "agent_name": "test-agent",
            "language": "en",
            "llm": {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "test-key",
                "base_url": None,
            },
            "capabilities": {"plugin": {}, "skills": {}},
            "plugins": plugins,
            "soul": {"delay": 60},
            "stamina": 3600,
            "context_limit": None,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 100,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": "",
        "covenant": "",
        "pad": "",
        "lingtai": "",
        "soul": "",
    }


def test_cli_shaped_boot_registers_once_and_is_idempotent(tmp_path):
    """The shipped boot flow, not just a repeated call within one path."""
    plugin_dir = _write_plugin(
        tmp_path / "plugins", "alpha",
        skills=["greeter"],
        mcp_servers={"greeter": {"type": "stdio", "command": "node"}},
    )
    workdir = tmp_path / "agent"
    workdir.mkdir(parents=True)
    (workdir / "init.json").write_text(
        json.dumps(_cli_shaped_init([str(plugin_dir)])), encoding="utf-8",
    )

    # Direct-Agent analogue: minimal construction, then _setup_from_init; the
    # CLI reaches that configured pass through its private shell.
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=workdir)
    assert agent._plugin_registration == {}, "construction defers to _setup_from_init"
    agent._setup_from_init()
    assert agent._plugin_registration["mcp_appended"] == ["greeter"]
    assert agent._plugin_registration["mcp_pruned"] == []
    assert len(_events(workdir, "plugin_register")) == 1, "one event per boot"

    # A second boot over the same working dir appends and prunes nothing.
    agent._setup_from_init()
    assert agent._plugin_registration["mcp_appended"] == []
    assert agent._plugin_registration["mcp_pruned"] == []
    assert [r["name"] for r in _registry_lines(workdir)] == ["greeter"]
    assert len(_events(workdir, "plugin_register")) == 2
    assert "<skill_names>greeter</skill_names>" in _prompt_section_named(agent, "plugin")


# ---------------------------------------------------------------------------
# The flagpost promise — still exactly true for the discovery tier
# ---------------------------------------------------------------------------

def test_discovery_only_plugin_installs_nothing(tmp_path):
    """Inheritance is not declaration: a plugin found on a skills path is inert."""
    root = tmp_path / "shared"
    _write_plugin(
        root, "alpha",
        skills=["a-skill"],
        mcp_servers={"s": {"type": "stdio", "command": "node"}},
    )
    agent, workdir = _mk_agent(tmp_path, skills_paths=[str(root)])
    _info(agent)

    # Nothing was copied into the agent's own library.
    library = workdir / ".library"
    assert not list(library.rglob("a-skill"))
    # Nothing was registered — the file is not even created.
    assert not (workdir / "mcp_registry.jsonl").exists()
    # The plugin's skills stay out of the vanilla catalog entirely.
    assert agent._plugin_skill_paths == []
    # The plugin directory itself is untouched.
    assert sorted(p.name for p in (root / "alpha").iterdir()) == [
        "mcp.json", "plugin.json", "skills",
    ]


def test_info_reports_registration_but_never_performs_it(tmp_path):
    """`info` is read-only: a plugin added after boot shows up discovered."""
    root = tmp_path / "plugins"
    _write_plugin(root, "alpha")
    agent, workdir = _mk_agent(tmp_path, [str(root)])

    _write_plugin(root, "beta", mcp_servers={"late": {"type": "stdio", "command": "node"}})
    result = _info(agent)

    assert [p["name"] for p in result["registered"]] == ["alpha"]
    assert [p["name"] for p in result["discovered"]] == ["beta"]
    assert "late" not in {r["name"] for r in _registry_lines(workdir)}


# ---------------------------------------------------------------------------
# The shipped example plugin
# ---------------------------------------------------------------------------

EXAMPLE_PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "docs/examples/agent-plugins/hello-lingtai"
)


def test_the_shipped_example_plugin_registers_end_to_end(tmp_path):
    """The example exists to be live-tested; it must not rot into an invalid one."""
    agent, workdir = _mk_agent(
        tmp_path, plugins=[str(EXAMPLE_PLUGIN)], skills_paths=[]
    )

    entry = _info(agent)["registered"][0]
    assert entry["name"] == "hello-lingtai"
    assert entry["skills"] == ["hello-lingtai"]
    assert entry["skills_mounted"] is True
    assert entry["mcp_registered"] == ["hello-lingtai"]
    assert entry["skipped"] == []

    # Its skill is a plugin-field entry, located inside the plugin.
    body = _prompt_section_named(agent, "plugin")
    assert "<skill_names>hello-lingtai</skill_names>" in body
    assert str(EXAMPLE_PLUGIN) in body

    # Its server holds a registry record whose ${PLUGIN_ROOT} arg resolved to
    # the real on-disk script, so the record is runnable as written.
    record = [r for r in _registry_lines(workdir) if r["name"] == "hello-lingtai"][0]
    assert record["source"] == "plugin:hello-lingtai"
    assert record["args"] == [str(EXAMPLE_PLUGIN / "server.py")]
    assert Path(record["args"][0]).is_file()


def test_the_example_server_is_dependency_free(tmp_path):
    """It stands in for third-party code, so it may not import the kernel or an SDK."""
    source = (EXAMPLE_PLUGIN / "server.py").read_text(encoding="utf-8")
    for forbidden in ("import lingtai", "from lingtai", "import mcp", "from mcp"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Packaged resources
# ---------------------------------------------------------------------------

def test_glossary_resources_present_and_valid():
    from lingtai.tools.glossary_validator import validate_package

    pkg_root = Path(__file__).resolve().parents[1] / "src/lingtai/tools/plugin"
    for lang in ("en", "zh", "wen"):
        assert (pkg_root / f"glossary-{lang}.md").is_file(), lang
    assert validate_package("plugin") == []


def test_manual_ships_with_the_package():
    manual = (
        Path(__file__).resolve().parents[1]
        / "src/lingtai/tools/plugin/manual/SKILL.md"
    )
    assert manual.is_file()
    body = manual.read_text(encoding="utf-8")
    assert "name: plugin-manual" in body
    assert "agent-plugins.org" in body


def test_default_plugin_root_is_scanned_without_declaration(tmp_path):
    """<workdir>/plugin/<name>/plugin.json registers with no init.json entry."""
    agent, workdir = _mk_agent(tmp_path, plugins=[])
    root = workdir / "plugin"
    _write_plugin(root, "auto", skills=["hi"], mcp_servers={"s": {"type": "stdio", "command": "node"}})
    _refresh_plugins(agent, [])
    _info(agent)

    assert "<name>auto</name>" in _prompt_section_named(agent, "plugin")
    assert "<skill_names>hi</skill_names>" in _prompt_section_named(agent, "plugin")
    assert "<mcp_names>s</mcp_names>" in _prompt_section_named(agent, "plugin")
    # The MCP is registered (runnable) but hidden from the vanilla mcp field.
    assert [r["name"] for r in _registry_lines(workdir)] == ["s"]
    assert "<name>s</name>" not in _prompt_section_named(agent, "mcp")


def test_mcp_field_is_resident_when_no_standalone_servers(tmp_path):
    """The mcp field explains itself even with an empty registry."""
    agent, _workdir = _mk_agent(tmp_path, plugins=[])
    body = _prompt_section_named(agent, "mcp")
    assert "No standalone MCP servers" in body
