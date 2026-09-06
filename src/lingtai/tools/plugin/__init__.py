"""Plugin capability — Agent Plugins catalog and registration snapshot.

This official model-facing ``plugin`` family reports third-party Agent Plugins
(agent-plugins.org v1.0.0); it is not itself an Agent Plugin.  Its public
``info``/``settings``/``manual`` surface is read-only presentation:

- **Declared → registered.** ``manifest.plugins`` and its compatibility alias
  name package directories.  The Agent registers those packages at boot, before
  capability setup: their validated skill names become visible in the protected Plugin
  prompt field and their ``mcp.json`` declarations become ``mcp_registry.jsonl`` records
  stamped ``source="plugin:<name>"``.
- **Inherited → discovered only.** Plugin packages found on an inherited skills
  path are listed, but cannot mount skills or register MCP servers.  Discovery
  is never an implicit install.

The service owns validation, containment, registration, and pruning.  This
family owns only the presentation adapter over those facts.  Its static
:data:`DECLARATION` binds through exactly three narrow host ports: ``workdir``
for service/manual paths, ``prompt_section`` for its own protected prompt
section, and ``plugin_catalog`` for a read-only projection of the registration
snapshot and discovery inputs.  It never receives a whole ``Agent`` and cannot
register, prune, activate, launch, or mount itself.

Usage: ``Agent(plugins=[...])`` or
``Agent(capabilities={"plugin": {"paths": [...]}})``, or via init.json.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from lingtai.kernel.tool_plugin import BoundToolPlugin, PluginCatalogState, ToolPluginDeclaration

from ..tool_family import ChildTool, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child
from .settings import plugin_setting_rows

if TYPE_CHECKING:
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.tool_plugin import ToolPluginHost

PROVIDERS = {"providers": [], "default": "builtin"}


# ---------------------------------------------------------------------------
# Catalog projection — read-only presentation over the host's narrow state port
# ---------------------------------------------------------------------------

def _collect_paths(state: PluginCatalogState) -> list[str]:
    """Union configured, declared, and inherited paths for discovery only.

    The registration snapshot is host-owned boot state.  The configured paths
    are the ``manifest.capabilities.plugin.paths`` alias; the snapshot's
    ``declared`` entries represent canonical ``manifest.plugins``; inherited
    skills paths remain visible but never register anything.  Order and
    de-duplication retain the pre-declaration behavior.
    """
    ordered = list(state.configured_paths)
    declared = state.registration.get("declared", [])
    if isinstance(declared, (list, tuple)):
        ordered.extend(path for path in declared if isinstance(path, str))
    ordered.extend(state.skill_paths)
    seen: set[str] = set()
    return [path for path in ordered if not (path in seen or seen.add(path))]


def _catalog_entry(record: dict) -> dict:
    """Project one discovered record to catalog facts, not its full manifest."""
    entry = {
        "name": record["name"],
        "version": record["version"],
        "summary": record["summary"],
        "skill_count": record["skill_count"],
        "mcp_server_count": record["mcp_server_count"],
        "source": record["source"],
    }
    if record.get("homepage"):
        entry["homepage"] = record["homepage"]
    return entry


def _registered_entries(state: PluginCatalogState) -> list[dict]:
    """Project the boot registration snapshot into the registered tier."""
    entries: list[dict] = []
    plugins = state.registration.get("plugins", [])
    if not isinstance(plugins, (list, tuple)):
        return entries
    for plugin in plugins:
        if not isinstance(plugin, Mapping):
            continue
        entry = dict(plugin)
        entry.pop("skill_paths", None)
        entry["skipped"] = list(plugin.get("skipped") or [])
        # Closed namespace: a plugin's skills remain in its own catalog field;
        # they are not injected into the vanilla skills prompt catalog.
        entry["skills_mounted"] = bool(plugin.get("skills")) and state.skills_enabled
        if plugin.get("skills") and not state.skills_enabled:
            entry["skipped"].append({
                "component": "skills/",
                "reason": (
                    "skills capability is not enabled on this agent, so the "
                    "plugin's skills are listed in the plugins field without a "
                    "skills-catalog composition"
                ),
            })
        entries.append(entry)
    return entries


def _reconcile(host: "ToolPluginHost") -> dict:
    """Re-scan configured paths, render this plugin's prompt, return snapshot.

    This is presentation only.  It reads the host's detached catalog state and
    working directory, then writes only the granted ``plugin`` prompt section.
    It never re-runs ``register_plugins``; a changed declaration takes effect
    through the host's ``system(action=\"refresh\")`` boot path.
    """
    from lingtai.services.plugin_registry import _build_registry_xml, read_plugins

    state = host.plugin_catalog.read_state()
    registered = _registered_entries(state)
    registered_names = {entry["name"] for entry in registered}
    records, problems, report = read_plugins(host.workdir.path, _collect_paths(state))
    discovered = [
        _catalog_entry(record) for record in records if record["name"] not in registered_names
    ]

    host.prompt_section.write_protected_section(
        _build_registry_xml(registered, discovered)
    )
    return {
        "status": "ok",
        "declared": list(state.registration.get("declared", []) or []),
        "registered_count": len(registered),
        "registered": registered,
        "discovered_count": len(discovered),
        "discovered": discovered,
        "mcp_appended": list(state.registration.get("mcp_appended", []) or []),
        "mcp_pruned": list(state.registration.get("mcp_pruned", []) or []),
        "paths": report,
        "problems": problems,
    }


def _flatten_manual_result(plugin_result: dict) -> dict:
    """Adapt the canonical manual child result to plugin's flat public shape."""
    flat = {
        "status": plugin_result.get("status", "ok"),
        "plugin_manual": plugin_result["content"][0]["text"],
        "manual_path": plugin_result["structuredContent"]["manual_path"],
    }
    if "error" in plugin_result:
        flat["error"] = plugin_result["error"]
    return flat


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

_DESCRIPTION = (
    "READ-ONLY: this tool itself installs and runs nothing. `info` re-scans "
    "the configured plugin paths and returns the boot registration snapshot; "
    "`settings` shows the redacted manifest.plugins registration roots and "
    "routes changes to the plugin manual; `manual` returns the plugin-manual "
    "body. No action mounts, unmounts, or launches anything. "
    "Your protected per-agent Agent Plugins catalog (agent-plugins.org, "
    "v1.0.0): the <registered_plugin> section in your system prompt lists "
    "every visible plugin with a <mount> stamp. `registered` = declared in "
    "init.json manifest.plugins and registered at boot — its validated "
    "skills are in the protected Plugin field (registered[].skills), not "
    "the vanilla skills catalog, and its mcp.json servers hold "
    "mcp_registry.jsonl records with source=\"plugin:<name>\" (registered but "
    "NOT running; activation still needs an init.json top-level mcp entry). "
    "`discovered` = merely found on an inherited skills path — its metadata "
    "is in the protected Plugin field, but no skills enter the vanilla "
    "skills catalog and no MCP servers enter mcp_registry.jsonl. Before "
    "inspecting, authoring, installing, or uninstalling a plugin, read the "
    "`plugin-manual` skill (call `manual` for its body: plugin.json "
    "contract, path containment, registration/uninstall flow) and call "
    "`info` for the current snapshot including every skipped component and "
    "why; no exceptions. To install, add the plugin's directory to "
    "manifest.plugins in init.json and call system(action=\"refresh\"); to "
    "uninstall, remove it from that list and refresh."
)

_EMPTY_INPUT: dict[str, Any] = MANUAL_INPUT_SCHEMA

_ACTION_DESCRIPTION = (
    "info: read-only action; re-scans the configured plugin paths and returns "
    "the boot registration snapshot (registered plugins, their registration "
    "facts, skipped reasons, discovered-only plugins, per-path report, "
    "problems) without the manual body. settings: show the redacted "
    "manifest.plugins declaration and its exact manual guidance. manual: "
    "return only the plugin-manual skill body. No action registers or "
    "unregisters anything — registration happens at boot from init.json "
    "manifest.plugins, so a newly declared plugin needs "
    "system(action=\"refresh\")."
)


def _build_family(host: "ToolPluginHost | None") -> ToolFamily:
    """Build the fixed ``info``/``settings``/``manual`` declared family."""
    info_input = DECLARATION.input_schemas["info"]
    manual_input = DECLARATION.manual_input_schema
    if host is None:
        def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
            raise AssertionError("the module-level schema-only ToolFamily never dispatches")

        info_handler: Any = _unused
        manual_child = ChildTool("manual", manual_input, _unused, title="manual input")
    else:
        info_handler = lambda _input: _reconcile(host)  # noqa: E731
        manual_child = build_manual_child(host.workdir, DECLARATION.manual)
    return ToolFamily(
        DECLARATION.name,
        [
            ChildTool("info", info_input, info_handler, title="info input"),
            manual_child,
        ],
        settings_provider=lambda: plugin_setting_rows(
            host.plugin_catalog if host is not None else None
        ),
    )


def get_description(lang: str = "en") -> str:
    return _DESCRIPTION


def get_schema(lang: str = "en") -> dict:
    schema = _FAMILY.build_schema()
    schema["properties"]["action"]["description"] = _ACTION_DESCRIPTION
    return schema


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Compose Plugin against only its declared host ports; mount nothing."""
    family = _build_family(host)

    def handle_plugin(args: dict) -> dict:
        # Preserve Plugin's pre-declaration unknown-action result including its
        # unhashable-action behavior, before generic ToolFamily dict lookup.
        action = args.get("action", "") if isinstance(args, Mapping) else ""
        if action not in family.child_names:
            supported = " or ".join(repr(name) for name in DECLARATION.public_actions)
            return {
                "status": "error",
                "message": f"unknown action: {action!r}, only {supported} is supported",
            }
        result = family.handle(args)
        if action == "manual" and "content" in result:
            return _flatten_manual_result(result)
        return result

    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=handle_plugin,
        description=DECLARATION.description,
        glossary_package=DECLARATION.glossary_package,
        activate=lambda: _reconcile(host),
    )


#: The static declaration of the official ``plugin`` tool family.  ``manual`` is
#: the package-owned installed-manual destination and is appended by the kernel,
#: never listed in ``actions``.  All composed identity values above derive from
#: this declaration so public schema/manual/name cannot silently drift.
DECLARATION = ToolPluginDeclaration(
    name="plugin",
    actions=("info",),
    input_schemas={"info": _EMPTY_INPUT},
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="plugin",
    description=_DESCRIPTION,
    binder=_bind,
    requires=("workdir", "prompt_section", "plugin_catalog"),
    glossary_package=__package__,
    settings=True,
)


#: Schema-only import-time composition validates the fixed child registry before
#: any Agent exists; host-bound composition uses the exact same declaration.
_FAMILY = _build_family(None)


def setup(agent: "BaseAgent", **_ignored) -> None:
    """Wire this static declaration through the host-owned registrar only."""
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    register_agent_tool_plugins(agent, [DECLARATION])
