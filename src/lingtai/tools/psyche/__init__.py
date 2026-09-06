"""Psyche's declared official host-plugin slice.

``psyche`` is the mandatory, model-visible LTP v2 root for the four durable
self domains: ``pad + lingtai + knowledge + skills = psyche``. Its exact action
set remains ``pad | lingtai | knowledge | skills | settings | manual``:

- ``pad``, ``lingtai``, ``knowledge``, and ``skills`` each return that domain's
  own installed manual;
- ``settings`` returns eight fully redacted Psyche-owned prompt-configuration
  rows (Pad plus the three configurable prompt pairs);
- ``manual`` returns the psyche routing-table manual, which explains the four
  durable domains and their shared mutation/rebuild model.

Every child takes the canonical strict-empty ``input``. No public action mutates
disk, prompt state, configuration, or a catalog. The static declaration binds
the family only to ``workdir`` plus a read-only view of the last successfully
applied Pad settings snapshot; it receives no Agent or prompt mutation surface.

This root replaces the four former public roots ``pad``, ``lingtai``,
``knowledge``, and ``skills``. That was a clean break: those roots, the
``pad.append`` action, and the ``skills.info`` / ``knowledge.info`` actions have
no alias, wrapper, or compatibility path and now fail as unknown tools/actions.
The name ``psyche`` was also used by a long-dissolved family; reusing the root
name grants none of that family's actions.

The capabilities themselves remain private lifecycle owners:

- durable text mutation is ``file.write`` / ``file.edit``;
- Pad and LingTai keep their private canonical prompt composers;
- Skills/Knowledge keep their capability-owned catalog composition; and
- one explicit ``context.rebuild`` or passive refresh/molt reconstruction makes
  durable content prompt-visible.

The static-plan ``substrate`` prompt contribution is separate from this family;
the kernel's render slot and mechanics remain unchanged.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration

from ..tool_family import ChildTool, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child
from .settings import build_settings_provider

if TYPE_CHECKING:
    from lingtai.kernel.tool_plugin import ToolPluginHost

__all__ = [
    "ACTION_ORDER",
    "DECLARATION",
    "DOMAIN_MANUALS",
    "INPUT_SCHEMAS",
    "get_description",
    "get_schema",
]

#: The one canonical child registry, as ``(action, installed manual name)``.
DOMAIN_MANUALS: tuple[tuple[str, str], ...] = (
    ("pad", "pad-manual"),
    ("lingtai", "lingtai-manual"),
    ("knowledge", "knowledge"),
    ("skills", "skills"),
)

#: The psyche routing-table manual: its own installed skill bundle.
_ROUTER_MANUAL = "psyche-manual"
_DROPPED_ENVELOPE_KEYS = ("_tc_id",)
_DECLARED_ACTIONS = tuple(action for action, _manual in DOMAIN_MANUALS)
_DECLARED_INPUT_SCHEMAS = {
    action: dict(MANUAL_INPUT_SCHEMA) for action in _DECLARED_ACTIONS
}

_ACTION_ENUM_DESCRIPTION = (
    "Required Psyche operation. Every action takes an empty input object and "
    "is strictly read-only — none of them writes a file, "
    "reloads the prompt, or rescans a catalog.\n"
    "pad: return pad-manual (the sketchboard body at system/pad.md and its "
    "pinned read-only references).\n"
    "lingtai: return lingtai-manual (your 灵台 / character at system/lingtai.md).\n"
    "knowledge: return the knowledge manual (private durable entries under "
    "knowledge/<name>/KNOWLEDGE.md).\n"
    "skills: return the skills manual (your catalog under .library/ plus any "
    "configured skills paths).\n"
    "settings: return the redacted inventory of Psyche-owned Pad and "
    "prompt-owner configuration.\n"
    "manual: return the psyche routing table — which action loads which "
    "domain manual, and the shared mutation/rebuild model.\n"
    "To CHANGE any durable source, use file.write for a full rewrite or "
    "file.edit for exact replacement, then apply it with one explicit "
    "context.rebuild; file mutation never hot-loads the prompt."
)


def _build_children(workdir: Any) -> list[ChildTool]:
    """Build the fixed manual-child registry for schema or bound dispatch."""
    children = [
        ChildTool(
            name=action,
            input_schema=dict(MANUAL_INPUT_SCHEMA),
            handler=build_manual_child(workdir, manual_name).handler,
            title=f"{action} input",
        )
        for action, manual_name in DOMAIN_MANUALS
    ]
    return children + [build_manual_child(workdir, _ROUTER_MANUAL)]


def _build_family(host: "ToolPluginHost | None") -> ToolFamily:
    """Build the schema-only or least-privilege host-bound family."""
    if host is None:
        return ToolFamily("psyche", _build_children(None), settings_provider=tuple)
    return ToolFamily(
        "psyche",
        _build_children(host.workdir),
        settings_provider=build_settings_provider(host.psyche_settings),
    )


# Import-time schema-only composition catches duplicate/reserved child defects.
_FAMILY = _build_family(None)


def get_description(lang: str = "en") -> str:
    return (
        "SIGNPOST ONLY: your psyche is your four durable domains — pad, "
        "lingtai (灵台), knowledge, and skills. Domain actions "
        "(pad/lingtai/knowledge/skills, input={}) return that domain's "
        "manual; settings (input={}) returns one compact redacted inventory "
        "of Psyche-owned Pad and prompt settings; manual (input={}) returns "
        "the routing table that says which action you want and how the "
        "domains relate. It never authors, edits, pins, installs, rescans, "
        "or loads anything. Durable content is changed with file.write (full "
        "rewrite) or file.edit (exact replacement) on the domain's own "
        "source, and becomes visible only after an explicit context.rebuild "
        "or passive refresh/molt reconstruction. Read the relevant manual "
        "before acting on a domain you do not already know. Results are "
        "exact guidance — leave root summarize false."
    )


def get_schema(lang: str = "en") -> dict[str, Any]:
    """Return the declaration-derived composed public Psyche schema."""
    schema = _FAMILY.build_schema()
    schema["properties"]["action"]["description"] = _ACTION_ENUM_DESCRIPTION
    return schema


def _adapt_manual_result(mcp_result: dict[str, Any]) -> dict[str, Any]:
    """Flatten one dispatched manual child to Psyche's pinned public shape."""
    flat: dict[str, Any] = {
        "status": mcp_result.get("status", "ok"),
        "manual": mcp_result["content"][0]["text"],
        "manual_path": mcp_result["structuredContent"]["manual_path"],
    }
    if "error" in mcp_result:
        flat["error"] = mcp_result["error"]
    return flat


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Compose Psyche against only its declared read-only host facade."""
    family = _build_family(host)

    def handle_psyche(args: dict[str, Any]) -> dict[str, Any]:
        raw = dict(args or {})
        for key in _DROPPED_ENVELOPE_KEYS:
            raw.pop(key, None)

        action = raw.get("action")
        result = family.handle(raw)
        if "content" in result:
            return _adapt_manual_result(result)
        if result.get("error_code") == "ACTION_REQUIRED":
            return {
                "error": (
                    f"Unknown psyche action: {action if action is not None else ''}. "
                    f"Must be one of: {', '.join(ACTION_ORDER)}."
                )
            }
        return result

    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=handle_psyche,
        description=get_description(),
        glossary_package=__package__,
    )


#: Static official declaration created before any Agent exists.
DECLARATION = ToolPluginDeclaration(
    name="psyche",
    actions=_DECLARED_ACTIONS,
    input_schemas=_DECLARED_INPUT_SCHEMAS,
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual=_ROUTER_MANUAL,
    description=get_description(),
    binder=_bind,
    requires=("workdir", "psyche_settings"),
    glossary_package=__package__,
    settings=True,
)

# Public compatibility views are derived from the one declaration.
ACTION_ORDER = DECLARATION.public_actions
INPUT_SCHEMAS = DECLARATION.public_input_schemas()


def boot(agent: Any) -> None:
    """Compose private Pad/LingTai state, then mount the official Psyche family.

    The two domain composers remain lifecycle operations owned by their packages.
    Registration then binds only the declaration's narrow public host facade.
    """
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    from ..lingtai import _lingtai_load
    from ..pad import _pad_load

    _pad_load(agent, {})
    _lingtai_load(agent, {})
    register_agent_tool_plugins(agent, [DECLARATION])
