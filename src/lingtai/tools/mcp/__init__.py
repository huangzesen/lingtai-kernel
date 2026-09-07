"""MCP capability — per-agent registry of MCP servers (pure presentation).

Symmetric to the ``knowledge`` / ``skills`` capabilities:

- Per-agent registry lives at ``<agent>/mcp_registry.jsonl`` (sibling to
  ``init.json``). One JSON record per line.
- The capability scans the registry on setup, validates each line, and renders
  the registry as XML into the system prompt's ``mcp`` section.
- Boot-time decompression: any name in ``init.json``'s ``addons: [...]`` list
  that isn't already in the registry gets appended from the kernel-shipped
  catalog (``lingtai/mcp_catalog.json``). Append-only, idempotent.
- All registry mutations (register, deregister, update) happen via file
  operations from the agent (``write``, ``edit``). The capability provides
  guidance via the umbrella SKILL.md, with ``info`` re-rendering the prompt
  section and reporting health while ``manual`` returns the manual body.

Tool surface: ``info`` returns the current registry and a runtime health
snapshot without the manual body; ``settings`` shows the two MCP-owned
top-level init settings; ``manual`` returns the umbrella manual body on demand.
All three are action children of one LTP v2 ``ToolFamily`` (see
``lingtai/tools/CONTRACT.md`` "Envelope"): the public tool name stays ``mcp`` and
the public action values are ``info``/``settings``/``manual``, carried in the
canonical
``action`` + ``input`` + ``reasoning`` + ``summarize`` envelope with a strict
empty ``input`` per action. The existing actions' observable results are
unchanged.

Ownership: this module is the agent-callable *tool* slice only. The registry
machinery it renders (validation, JSONL I/O, catalog load, identity projection,
addon decompression, XML build) is a service and lives at
``lingtai/services/mcp_registry.py``; it is imported lazily inside ``setup`` and
the handlers, per the ``lingtai.tools → lingtai`` lazy-back-edge rule.

Declared host plugin: ``mcp`` is the first official family to recut onto the
kernel-owned declared host-plugin contract
(``lingtai/kernel/tool_plugin/CONTRACT.md``). :data:`DECLARATION` is a static
``ToolPluginDeclaration`` built at import, before any Agent exists; ``mcp`` is a
reserved official name in ``lingtai.kernel.tool_plugin``, so a second
declaration of it is refused before anything binds or mounts. The family no
longer receives the whole ``Agent``: :func:`_bind` gets a ``ToolPluginHost``
granting exactly the two ports this capability actually consumes — ``workdir``
(read the registry and the installed manual) and ``prompt_section`` (rewrite
its own protected ``mcp`` section). Nothing about the public tool — name,
``["info", "settings", "manual"]`` action enum, strict-empty inputs, result shapes
including the tool-specific ``mcp_manual`` body key — changed with it.

Usage: ``Agent(capabilities=["mcp"])`` or via init.json.

The source package is also an Agent Plugins v1.0.0 *documentation package*: its
``plugin.json`` and sole owned ``skills/mcp-manual/`` skill are validated by the
existing plugin-registry reader when Agent installs intrinsic manuals. That
packaging does not select, register, or activate an external plugin and never
creates an ``mcp_registry.jsonl`` record; the static declaration above remains
the only route that mounts this model-facing tool.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration

from ..tool_family import ChildTool, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child
from .settings import MCPSettingsProvider

if TYPE_CHECKING:
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.tool_plugin import ToolPluginHost

PROVIDERS = {"providers": [], "default": "builtin"}


# ---------------------------------------------------------------------------
# Reconciliation (shared by setup and the ``info`` action)
# ---------------------------------------------------------------------------

def _registered_entry(record: dict, identity: dict | None) -> dict:
    """Build one ``registered`` entry, attaching identity only when present."""
    entry = {"name": record["name"], "summary": record["summary"]}
    if identity and identity.get("accounts"):
        entry["identity"] = identity
    return entry


def _reconcile(host: "ToolPluginHost") -> dict:
    """Read registry, render into prompt, return health snapshot.

    Reached through the two granted host ports, never through the Agent: the
    registry and the installed manual are resolved below ``host.workdir.path``
    (formerly the private ``agent._working_dir``), and the rendered XML goes to
    ``host.prompt_section``, which is bound to this plugin's own protected
    ``mcp`` section and cannot address another's.
    """
    from lingtai.services.mcp_registry import (
        read_registry,
        read_identities,
        _build_registry_xml,
        _registry_path,
    )

    working_dir = host.workdir.path
    records, problems = read_registry(working_dir)
    identities = read_identities(working_dir)

    xml = _build_registry_xml(records, identities)
    host.prompt_section.write_protected_section(xml)

    # Health: the umbrella manual must be present.
    intrinsic_dir = working_dir / ".library" / "intrinsic"
    manual_path = intrinsic_dir / "capabilities" / "mcp" / "SKILL.md"
    result = {
        "status": "ok",
        "registry_path": str(_registry_path(working_dir)),
        "registered_count": len(records),
        "registered": [
            _registered_entry(r, identities.get(r["name"]))
            for r in records
        ],
        "problems": problems,
    }
    return result


def _flatten_manual_result(mcp_result: dict) -> dict:
    """Adapt the canonical ``manual`` child result to mcp's public flat shape.

    ``ToolFamily.handle()`` has already dispatched to the registered ``manual``
    child (``build_manual_child``) and returned its canonical result *verbatim*
    (no double wrap) — full body at ``content[0].text``, host-local path at
    ``structuredContent.manual_path``, plus the loader's truthful
    ``status``/``error`` facts. mcp's own public result shape predates that
    generic contract and must stay exactly ``status``/``mcp_manual``/
    ``manual_path`` (note the tool-specific body key), so this Host-owned
    adapter runs strictly *after* dispatch — never inside a registered child,
    and never on the ``info`` path.
    """
    flat = {
        "status": mcp_result.get("status", "ok"),
        "mcp_manual": mcp_result["content"][0]["text"],
        "manual_path": mcp_result["structuredContent"]["manual_path"],
    }
    if "error" in mcp_result:
        flat["error"] = mcp_result["error"]
    return flat


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

_DESCRIPTION = (
    "SIGNPOST ONLY: this read-only tool does not register, activate, configure, "
    "or troubleshoot MCP servers. `info` only re-reads the registry and returns "
    "health; `settings` shows MCP-owned configuration; `manual` returns the "
    "mcp-manual body. Before registering, deregistering, updating, or "
    "troubleshooting, read `mcp-manual` (call `manual` for its body), call "
    "`info` for the current health snapshot, and obtain explicit authorization. "
    "Registry changes use "
    "write/edit on mcp_registry.jsonl followed by system(action=\"refresh\")."
)

# The owner-defined action and reserved manual action take no arguments; the
# injected settings child is strict-empty too. Reuse the manual literal for the
# declared actions so schema-only and dispatching families cannot drift.
# ``ToolFamily.build_schema`` deep-copies per child, and dispatch reads only
# ``properties``, so one shared object is safe.
_EMPTY_INPUT: dict[str, Any] = MANUAL_INPUT_SCHEMA

_ACTION_DESCRIPTION = (
    "info: signpost-only action; re-reads the registry and returns a runtime "
    "health snapshot (registry contents, problems, registry path) without the "
    "manual body. settings: show the MCP-owned init.addons and fully redacted "
    "init.mcp configuration rows. manual: return only the mcp-manual skill "
    "body. No action mutates MCP configuration."
)


def _build_family(host: "ToolPluginHost | None") -> ToolFamily:
    """Build the three-child ``mcp`` family; the registry is declared exactly once.

    With a granted ``host``, children are bound to real handlers for dispatch.
    With ``None``, the module-level schema-only family is built: its handlers
    raise if ever called, and constructing it at import time proves the fixed
    registry has no duplicate or reserved-name collision (``ToolFamilyError``
    raises here rather than shipping silently). Both paths declare the same
    ordered children, so the composed schema and the dispatching family can
    never drift apart.

    Every identity this composition needs is *derived* from :data:`DECLARATION`
    rather than restated: the public tool name, the per-action ``input``
    schemas, and the installed-manual destination the reserved ``manual`` child
    reads. ``src/lingtai/tools/CONTRACT.md`` requires the declared identity,
    action list, and manual to agree with what actually ships; deriving them
    makes the two structurally the same value instead of two literals that can
    silently diverge. (``DECLARATION`` is defined below this function and read
    at call time — the schema-only build is the first call, and it runs after
    the declaration is constructed.)
    """
    manual_input = DECLARATION.manual_input_schema
    info_input = DECLARATION.input_schemas["info"]
    if host is None:
        def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
            raise AssertionError("the module-level schema-only ToolFamily never dispatches")

        info_handler: Any = _unused
        manual_child = ChildTool("manual", manual_input, _unused, title="manual input")
    else:
        info_handler = lambda _input: _reconcile(host)  # noqa: E731
        # Registered directly, unwrapped: ``ToolFamily.handle()`` must dispatch
        # this child's own canonical MCP-compatible result verbatim (no double
        # wrap). mcp's flat public shape is reconstructed from that canonical
        # result strictly *after* dispatch, in ``handle_mcp`` — never inside a
        # registered child.
        # Only the workdir port is handed over: the manual child reads one
        # installed ``SKILL.md`` below the agent working directory and needs
        # nothing else from the host.
        manual_child = build_manual_child(host.workdir, DECLARATION.manual)
    return ToolFamily(
        DECLARATION.name,
        [
            ChildTool("info", info_input, info_handler, title="info input"),
            manual_child,
        ],
        settings_provider=MCPSettingsProvider(host.workdir.path if host else None),
    )


def get_description(lang: str = "en") -> str:
    return _DESCRIPTION


def get_schema(lang: str = "en") -> dict:
    # Composed by the generic ToolFamily infra from each child's own canonical
    # ``input_schema``. The settings child is injected immediately before the
    # reserved manual child by the generic family seam.
    schema = _FAMILY.build_schema()
    schema["properties"]["action"]["description"] = _ACTION_DESCRIPTION
    return schema


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Compose the ``mcp`` family against a granted host. Mounts nothing.

    Pure composition, per the declared host-plugin contract: it builds the
    per-host ``ToolFamily`` and the Host-layer dispatch wrapper and returns
    them. The boot presentation — the first registry reconcile that writes the
    protected ``mcp`` prompt section — is the separately declared ``activate``
    step below, which the kernel registrar runs only after every official
    name check has passed.
    """
    family = _build_family(host)

    def handle_mcp(args: dict) -> dict:
        # The generic ``ToolFamily`` dispatcher validates ``action``,
        # type-checks and strips root ``summarize``, rejects unknown root
        # fields, and rejects any ``input`` key outside the selected action's
        # own declared schema — all actions declare a strict empty input, so
        # any extra input field fails here, before ``_reconcile`` re-reads the
        # registry or the manual child touches the filesystem.
        #
        # mcp's existing unknown-action envelope mechanics stay in the Host
        # layer rather than changing the generic dispatcher's canonical error
        # shape. Two pre-migration facts the generic
        # dispatcher does not reproduce on its own are restored before
        # delegating, both proven by ``test_mcp_show_unknown_action_returns_error``:
        # a missing ``action`` key renders the empty-string default (not
        # ``None``), and invalid JSON can make ``action`` unhashable (``[]`` /
        # ``{}``, issue #513's explicit blocker). Membership is tested against
        # ``child_names``, a tuple, which compares by ``==`` and never hashes,
        # so an unhashable value simply does not match — whereas
        # ``ToolFamily.handle``'s ``action not in self._children`` dict lookup
        # would raise ``TypeError`` on it. That is precisely why this routing
        # exists ahead of the delegation below.
        action = args.get("action", "") if isinstance(args, Mapping) else ""
        if action not in family.child_names:
            choices = ", ".join(repr(name) for name in family.child_names[:-1])
            return {
                "status": "error",
                "message": (
                    f"unknown action: {action!r}, only {choices}, or "
                    f"{family.child_names[-1]!r} is supported"
                ),
            }
        result = family.handle(args)
        if action == "manual" and "content" in result:
            return _flatten_manual_result(result)
        return result

    return BoundToolPlugin(
        # Derived, never restated: the kernel checks this against the
        # declaration it reserved the name for, and the family has exactly one
        # spelling of its own identity.
        name=DECLARATION.name,
        schema=get_schema(),
        handler=handle_mcp,
        description=get_description(),
        glossary_package=__package__,
        activate=lambda: _reconcile(host),
    )


#: The static declaration of the official ``mcp`` tool plugin.
#:
#: Constructed at import, with no Agent in existence: the kernel validates the
#: shape here (one operational action, no attempt to declare the reserved
#: ``manual`` action, one strict input schema per action, only grantable host
#: ports required), so a packaging defect fails at import rather than at boot.
#:
#: ``actions`` holds the *operational* actions only. The reserved ``manual`` is
#: appended by ``DECLARATION.public_actions`` and owned by this family's own
#: ``build_manual_child``; ``manual="mcp"`` names the installed manual
#: destination that child reads, matching ``Agent._install_intrinsic_manuals``.
#:
#: This declaration is the family's single source of its own identity. The
#: composition below reads ``name``, ``input_schemas``, ``manual_input_schema``,
#: and ``manual`` back out of it rather than repeating any of them, so there is
#: no second literal to drift; the kernel additionally rejects, at every
#: ``bind()``, a bound plugin advertising an action inventory other than
#: ``public_actions``.
DECLARATION = ToolPluginDeclaration(
    name="mcp",
    actions=("info",),
    input_schemas={"info": _EMPTY_INPUT},
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="mcp",
    description=_DESCRIPTION,
    binder=_bind,
    settings=True,
    # Earned from this slice, not enumerated: ``workdir`` replaces the private
    # ``agent._working_dir`` read, ``prompt_section`` replaces the
    # ``agent.update_system_prompt("mcp", ..., protected=True)`` call. ``mcp``
    # needs nothing else from the live Agent body, so it is granted nothing
    # else — mounting included, which stays host-only.
    requires=("workdir", "prompt_section"),
    glossary_package=__package__,
)


#: The module-level schema-only family, built from the declaration above. It is
#: constructed at import (after ``DECLARATION``, which it reads) so a duplicate
#: or reserved-name collision in the fixed child registry raises here rather
#: than shipping silently; ``get_schema()`` composes the public schema from it.
_FAMILY = _build_family(None)


def setup(agent: "BaseAgent", **_ignored) -> None:
    """Set up the mcp capability through its declared host-plugin route.

    The capability is pure presentation: it reads the registry from disk and
    renders it into the system prompt. Decompression of init.json's addons:
    field happens in the Agent initializer via
    ``lingtai.services.mcp_registry.decompress_addons()`` before setup is called.

    This function is now only composition wiring. It builds the production host
    adapters for this Agent and hands :data:`DECLARATION` to the kernel
    registrar, which reserves the official ``mcp`` name, binds against a
    least-privilege host, runs the boot reconcile, and mounts the tool — in that
    order, so a name conflict is refused before the live tool surface is
    touched. Registering the same declaration again on refresh is idempotent.
    """
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    register_agent_tool_plugins(agent, [DECLARATION])
