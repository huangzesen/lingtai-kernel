"""Psyche's declared official host-plugin slice.

``psyche`` is the mandatory model-visible LTP v2 root for the four durable
self-domains: ``pad + lingtai + knowledge + skills = psyche``. Its six actions
are four manual routes, redacted ``settings``, and the ``manual`` router; every
child uses the canonical strict-empty input and is read-only. The declaration
binds only ``workdir`` and the applied Psyche settings snapshot.

The former public domain roots and dissolved Psyche actions remain retired
without aliases. Domain lifecycle/composition stays private to its owners, and
the static-plan ``substrate`` prompt section remains a separate kernel concept.
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
    "Choose one Psyche operation. Every action takes strict input={} and is "
    "read-only; load the returned manual before acting on an unfamiliar domain.\n"
    "pad: return pad-manual for system/pad.md and pinned Pad references.\n"
    "lingtai: return lingtai-manual for system/lingtai.md (灵台 / character).\n"
    "knowledge: return the knowledge manual for durable KNOWLEDGE.md entries.\n"
    "skills: return the skills manual for the configured .library catalog.\n"
    "settings: show Psyche's fully redacted settings view.\n"
    "manual: return the psyche routing table."
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
        "SIGNPOST ONLY: psyche routes four durable domains — pad, lingtai "
        "(灵台), knowledge, and skills. Every action takes strict input={} and is "
        "read-only; it never authors, edits, pins, installs, rescans, or loads "
        "anything. Call manual first for an unfamiliar domain. Durable changes "
        "use file.write for a full rewrite or file.edit for an exact replacement, "
        "then one explicit context.rebuild (or passive refresh/molt); file "
        "mutation never hot-loads the prompt. Psyche owns no lifecycle action: "
        "context owns rebuild/molt and system owns identity. Results are exact; "
        "leave root summarize false."
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
