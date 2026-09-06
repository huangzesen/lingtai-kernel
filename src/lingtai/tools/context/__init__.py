"""Context intrinsic — the department that owns the agent's context.

An LTP v2 family (``../CONTRACT.md``): one model-facing root ``context`` with
four fixed canonical action children, each owning its own strict ``input``
object::

    molt      -> shed the conversation, keep the durable stores
    summarize -> record compact replacements in runtime history (record-only)
    rebuild   -> recompose the full prompt, apply summaries, replay provider context
    manual    -> return the installed context-manual skill

This package replaces the former ``psyche`` family, which mixed two unrelated
concerns behind one root: the context lifecycle (molt) and the agent's name.
``psyche`` no longer exists at any model-visible or registry level, and there
is no compatibility alias — ``psyche(...)`` is an unknown tool and the two name
actions now live on ``system`` (``lingtai.tools.system.name``). What arrived
here from elsewhere is the context-hygiene half of ``system``: the public
``system(action='summarize')`` action became the two explicit actions
``context.summarize`` and ``context.rebuild``, and is gone from ``system``.

The molt semantics are moved, not redesigned: summary/session-journal gating,
refusal-before-shed, keep_tool_calls/keep_last, archive/snapshot/rebuild,
``_tc_id`` transport handling, the post-molt notification, forced system molt,
and every durable-store path are exactly what they were. Only the public root
and action name changed (``psyche.context_molt`` -> ``context.molt``). The
durable molt *event* key is deliberately NOT renamed; see
``kernel/agent_session.py`` ``MOLT_BOUNDARY_EVENT``.

Two names spelled ``summarize`` coexist at different envelope levels, and they
are unrelated:

  * ``context(action='summarize')`` — this family's record-only ACTION;
  * the optional root ``summarize`` boolean — the cross-cutting a-priori
    result-summarization presentation control every LTP v2 family advertises
    (``kernel/tool_result_summary.py``).

The root boolean is stripped by the generic dispatcher and is never domain
input; no child here declares a ``summarize`` property. ``context.summarize``
records and never rebuilds; ``context.rebuild`` is the only active operation
that first recomposes every canonical prompt source, then applies pending/new
summaries, then requests provider replay. The public action — not a boolean —
is the discriminator.

Per-action behavior, inputs, and result/error shapes live in ``CONTRACT.md``;
the model-facing text lives in the schema descriptions below and in the
``context-manual`` skill. Neither is restated here.

Sub-modules:
    _snapshots.py — Snapshot and summary persistence for the molt machinery.
    _molt.py      — Context molt core and the system-initiated forced molt.
"""
from __future__ import annotations

from typing import Any, Mapping

from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration, ToolPluginHost

# --- Re-exports from sub-modules for backward compatibility ---

# Snapshots (used by consultation, inquiry, etc.)
from ._snapshots import SNAPSHOT_SCHEMA_VERSION, _write_molt_snapshot, _write_molt_summary  # noqa: F401

# Molt (the public surface and kernel-facing forced-molt hook)
from ._molt import _context_molt, context_forget  # noqa: F401
from .._manual import load_installed_manual  # noqa: F401
from ..tool_family import (
    TRIGGER_UNSUPPORTED_INPUT_FIELD,
    ChildTool,
    DiagnosticDescriptor,
    ToolFamily,
)
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child

# The summarize/rebuild engine remains private implementation. The official
# plugin binds only the narrow context-runtime port that invokes it.
from ..system.summarize import _summarize as _summarize_engine

# ---------------------------------------------------------------------------
# Canonical child input schemas — one strict, closed object per action.
# ---------------------------------------------------------------------------
#
# Each action's own ``input`` is declared exactly once here. ``ToolFamily``
# composes the model-facing schema and the dispatch allow-list from these same
# objects, so the two can never drift: the child's canonical name IS the public
# ``action`` value IS the dispatch key.

_MOLT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": 'Required retrospective for the next session. Tend all four durable stores and write the session-journal entry before calling molt. See context-manual and its molt reference.',
        },
        "session_journal_path": {
            "type": "string",
            "description": 'REQUIRED pre-molt path: knowledge/session-journal/<entry>/KNOWLEDGE.md, a per-segment child inside the workdir (not the parent index). It must be non-empty UTF-8 with `name`/`description` frontmatter and `type: session-journal` or `session_journal: true`; missing or invalid input is refused before shedding. See context-manual and its molt reference.',
        },
        "keep_tool_calls": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": 'Optional ordered prior tool-call IDs to replay; unknown IDs refuse before shedding. Pass null to keep none. Keep short because durable stores are primary persistence. See context-manual\'s molt reference.',
        },
        "keep_last": {
            "type": ["integer", "null"],
            "description": 'Optional minimum recent entries to replay (null defaults to 20; 0 archives all). A retained suffix may expand to keep one assistant tool-call/result batch whole, and overlaps with keep_tool_calls are deduplicated. See context-manual\'s molt reference.',
        },
    },
    "required": ["summary", "session_journal_path", "keep_tool_calls", "keep_last"],
    "additionalProperties": False,
}

# ``molt``'s own static, mechanical diagnostic for a foreign ``input`` field
# (cross-action, e.g. a ``summarize``/``rebuild`` key, or wholly unknown, e.g.
# a smuggled ``files``): declared once, adjacent to ``_MOLT_INPUT_SCHEMA``,
# per ``tool_family/CONTRACT.md`` "Diagnostics sidecar". The generic
# dispatcher only ever supplies the structural ``location`` around this
# verbatim text — it does not (and must not) claim `session_journal_path`
# has to be relative; the existing in-workdir-absolute-normalizes-to-relative
# policy is unchanged and unrelated to this diagnostic.
_MOLT_UNSUPPORTED_INPUT_DIAGNOSTIC = DiagnosticDescriptor(
    code="CTX_MOLT_UNSUPPORTED_INPUT_FIELD",
    expected_form=(
        "an input object containing only summary, session_journal_path, "
        "keep_tool_calls, and keep_last"
    ),
    reason="molt rejects foreign action input before it can shed context",
    fix="remove the foreign field or choose the action that owns it",
)

#: Per-child diagnostic sidecars, keyed by child name then structural
#: trigger. Only ``molt`` opts in today; a child absent here (``summarize``,
#: ``rebuild``, ``manual``) gets exactly the generic dispatcher's legacy
#: three-key failure for a foreign ``input`` field, unchanged.
_CHILD_DIAGNOSTICS: dict[str, Mapping[str, DiagnosticDescriptor]] = {
    "molt": {TRIGGER_UNSUPPORTED_INPUT_FIELD: _MOLT_UNSUPPORTED_INPUT_DIAGNOSTIC},
}

#: One item of the summarize/rebuild ``items`` array. Declared once and reused
#: by both schemas below so the two branches cannot drift.
_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_call_id": {
            "type": "string",
            "description": "The id of the prior tool-result block to summarize.",
        },
        "summary": {
            "type": "string",
            "description": "Your agent-authored summary of that tool result.",
        },
    },
    "required": ["tool_call_id", "summary"],
    "additionalProperties": False,
}

_SUMMARIZE_ITEMS_DESCRIPTION = (
    "REQUIRED, non-empty list of {tool_call_id, summary} items. Each ID names a "
    "prior tool-result block and each summary is agent-authored. The raw event "
    "remains retrievable; this action records only and does not rebuild. See "
    "context-manual/reference/summarize-manual."
)

_REBUILD_ITEMS_DESCRIPTION = (
    "Optional list of the same {tool_call_id, summary} items; omit it for the "
    "ordinary bare input={} call (explicit null is equivalent). Rebuild composes "
    "all canonical prompt sources, then applies pending/new summaries, then "
    "requests provider replay. See context-manual/reference/summarize-manual."
)

_SUMMARIZE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": _SUMMARIZE_ITEMS_DESCRIPTION,
            "items": _ITEM_SCHEMA,
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

# ``items`` is genuinely OPTIONAL here — it is deliberately absent from
# ``required``, unlike every other optional field in this package.
#
# The usual LTP v2 convention is "REQUIRED but nullable", because a strict
# provider schema has no other way to express an optional field. That
# convention is wrong for this action: ``context(action='rebuild', input={})``
# is the *ordinary* call (apply the already-pending summaries), not an edge
# case, so the model-visible schema must accept a bare ``{}``. Listing ``items``
# in ``required`` would advertise a contract the handler does not have — the
# handler accepts ``{}`` — and would make the documented ordinary call
# schema-invalid.
#
# ``type`` stays ``["array", "null"]`` so an explicit ``{"items": null}`` from a
# provider that always materializes declared properties is still accepted;
# ``_strip_nulls`` turns that back into "absent" before the engine sees it. So
# ``{}`` and ``{"items": null}`` are the same ordinary pure-rebuild call, and
# both are schema-valid.
_REBUILD_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": ["array", "null"],
            "description": _REBUILD_ITEMS_DESCRIPTION,
            "items": _ITEM_SCHEMA,
        },
    },
    "required": [],
    "additionalProperties": False,
}


def _summarize_action(agent, args: dict) -> dict:
    """Record Context summaries through the unchanged private engine."""
    return _summarize_engine(agent, {**args, "rebuild": False})


def _rebuild_action(agent, args: dict) -> dict:
    """Perform the contractually ordered active full reconstruction.

    This remains the one place that translates a reconstruction failure into the
    established Context result vocabulary. The declared plugin never receives the
    Agent: the production context-runtime adapter calls this function through its
    narrow ``rebuild(args)`` operation.
    """
    reconstruct = getattr(agent, "_reconstruct_context", None)
    if not callable(reconstruct):
        return {
            "status": "error",
            "reason": "context_reconstruction_unavailable",
            "message": "This agent does not provide the canonical context reconstruction hook.",
        }
    try:
        reconstruct()
    except Exception as exc:
        try:
            agent._log("context_reconstruction_failed", error=type(exc).__name__)
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "context_reconstruction_failed",
            "message": f"Canonical prompt reconstruction failed: {type(exc).__name__}.",
        }

    result = _summarize_engine(agent, {**args, "rebuild": True})
    if isinstance(result, dict):
        result["prompt_reconstructed"] = True
        result["prompt_reconstruction"] = (
            "All canonical prompt sources were re-read and recomposed before "
            "summary processing."
        )
    return result


# The action/schema inventory is the declaration's source data. Runtime
# operations are deliberately *not* stored beside it: the binder receives the
# host's ContextRuntimePort and maps these action names to that port's three
# narrow operations, so the family cannot reach a live Agent body directly.
_CHILD_SPECS: tuple[tuple[str, dict[str, Any], Any], ...] = (
    ("molt", _MOLT_INPUT_SCHEMA, _context_molt),
    ("summarize", _SUMMARIZE_INPUT_SCHEMA, _summarize_action),
    ("rebuild", _REBUILD_INPUT_SCHEMA, _rebuild_action),
)

# The one Context-only transport boundary. ``_tc_id`` remains metadata injected
# by BaseAgent, never a public root or action-input field; only the molt port
# receives it because replay requires the precise live ToolCallBlock.
_MOLT_ENVELOPE_KEYS = ("_tc_id", "_reasoning", "reasoning", "_initiator")


def _strip_nulls(action_input: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the established absent/null equivalence for Context operations."""
    return {key: value for key, value in action_input.items() if value is not None}


def _build_family(
    host: ToolPluginHost | None,
    envelope: Mapping[str, Any] | None = None,
) -> ToolFamily:
    """Compose Context from its declaration and a least-privilege host.

    ``None`` builds only the import-time schema family. A real host contributes
    two earned capabilities: a read-only workdir for the installed manual and a
    ContextRuntimePort with exactly ``molt``, ``summarize``, and ``rebuild``.
    No child closes over, receives, or reaches for the Agent.
    """
    if host is None:
        def _unused(_input: Mapping[str, Any]) -> dict:
            raise AssertionError("the module-level schema-only Context family never dispatches")

        children = [
            ChildTool(
                name,
                schema,
                _unused,
                title=f"{name} input",
                diagnostics=_CHILD_DIAGNOSTICS.get(name),
            )
            for name, schema, _handler in _CHILD_SPECS
        ]
        children.append(ChildTool("manual", MANUAL_INPUT_SCHEMA, _unused, title="manual input"))
        return ToolFamily(DECLARATION.name, children)

    runtime = host.context_runtime
    extra = dict(envelope or {})
    operations = {
        "molt": runtime.molt,
        "summarize": runtime.summarize,
        "rebuild": runtime.rebuild,
    }

    def _dispatch(name: str, action_input: Mapping[str, Any]) -> dict:
        args = _strip_nulls(action_input)
        if name == "molt":
            # Metadata cannot overwrite the strict, validated input object.
            for key, value in extra.items():
                args.setdefault(key, value)
        return operations[name](args)

    children = [
        ChildTool(
            name,
            schema,
            lambda action_input, _name=name: _dispatch(_name, action_input),
            title=f"{name} input",
            diagnostics=_CHILD_DIAGNOSTICS.get(name),
        )
        for name, schema, _handler in _CHILD_SPECS
    ]
    # The manual reads the installed copy from the workdir port. Its destination
    # is declaration-owned, so package/manual/ and runtime mount cannot drift.
    children.append(build_manual_child(host.workdir, DECLARATION.manual))
    return ToolFamily(DECLARATION.name, children)

# ---------------------------------------------------------------------------
# Schema / description
# ---------------------------------------------------------------------------

#: This family's own per-action routing prose. The generic composer writes a
#: neutral "Required operation within the context family." description; the
#: guidance the model actually needs to pick an action replaces it in
#: :func:`get_schema` rather than being lost.
_ACTION_ENUM_DESCRIPTION = (
    "Required operation. "
    "molt sheds conversation while retaining durable stores; it requires a "
    "retrospective and valid session-journal path. See context-manual.\n"
    "summarize records compact prior tool-result replacements only; it does not "
    "rebuild provider context.\n"
    "rebuild recomposes canonical prompt sources, then applies summaries, then "
    "requests provider replay; bare input={} is valid. Make one tactical call.\n"
    "manual returns the installed context-manual without a lifecycle operation. "
    "Name actions belong to system."
)


def get_description(lang: str = "en") -> str:
    return (
        "Context lifecycle: molt sheds conversation while retaining durable stores; "
        "summarize records compact tool-result replacements only; rebuild "
        "recomposes canonical prompt sources, applies summaries, and requests "
        "provider replay; manual returns context-manual. Read the manual before "
        "molt/rebuild. The action named summarize is unrelated to the optional "
        "root summarize boolean presentation control; leave that boolean false "
        "for Context results."
    )


def get_schema(lang: str = "en") -> dict:
    # Composed by the generic ToolFamily infra from each child's own canonical
    # ``input_schema`` above, rather than hand-assembled: root ``action`` +
    # per-action ``input`` + required ``reasoning`` + optional ``summarize``,
    # with a root ``allOf`` correlating each ``action`` const to that exact
    # action's ``input`` shape on both the Chat and Responses wires.
    #
    # ``lang`` is accepted for source compatibility and ignored: schema prose
    # is canonical English and language-independent.
    schema = _FAMILY.build_schema()
    schema["properties"]["action"]["description"] = _ACTION_ENUM_DESCRIPTION
    return schema


# ---------------------------------------------------------------------------
# Declared host-plugin binding and dispatch
# ---------------------------------------------------------------------------


def _adapt_manual_result(mcp_result: dict) -> dict:
    """Keep Context's historical flat manual result after generic dispatch."""
    flat: dict[str, Any] = {
        "status": mcp_result.get("status", "ok"),
        "manual": mcp_result["content"][0]["text"],
        "manual_path": mcp_result["structuredContent"]["manual_path"],
    }
    if "error" in mcp_result:
        flat["error"] = mcp_result["error"]
    return flat


def _bind(host: ToolPluginHost) -> BoundToolPlugin:
    """Bind Context to its granted ports; composition itself has no side effect."""
    def handle_context(args: dict) -> dict:
        raw = dict(args or {})
        envelope = {key: raw.pop(key) for key in _MOLT_ENVELOPE_KEYS if key in raw}
        # The generic family accepts the public root reasoning spelling. The molt
        # operation additionally receives its private metadata copy through the
        # ContextRuntimePort, never through child input.
        for key in ("reasoning", "_reasoning"):
            if key in envelope:
                raw[key] = envelope[key]

        action = raw.get("action")
        result = _build_family(host, envelope).handle(raw)
        if action == "manual" and "content" in result:
            return _adapt_manual_result(result)
        if result.get("error_code") == "ACTION_REQUIRED":
            return {
                "error": (
                    f"Unknown {DECLARATION.name} action: "
                    f"{action if action is not None else ''}. "
                    f"Must be one of: {', '.join(DECLARATION.public_actions)}."
                )
            }
        return result

    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=handle_context,
        description=get_description(),
        glossary_package=__package__,
    )


#: Static official declaration. The three operational schemas, root identity,
#: and installed manual destination below are then read back by the composed
#: family; ``manual`` remains reserved and is appended exactly once by this
#: declaration rather than being independently declared by Context.
DECLARATION = ToolPluginDeclaration(
    name="context",
    actions=tuple(name for name, _schema, _handler in _CHILD_SPECS),
    input_schemas={name: schema for name, schema, _handler in _CHILD_SPECS},
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="context-manual",
    description=get_description(),
    binder=_bind,
    requires=("workdir", "context_runtime"),
    glossary_package=__package__,
)

#: Import-time schema-only composition catches a bad child registry before any
#: Agent exists. The kernel separately verifies this public action inventory at
#: every official bind.
_FAMILY = _build_family(None)

#: Public order comes only from the declaration (operational actions + manual).
ACTION_ORDER: tuple[str, ...] = DECLARATION.public_actions
_MANUAL_SKILL_NAME = DECLARATION.manual


def _runtime_port(agent):
    """Compose the production ContextRuntimePort from narrow bound callbacks.

    This is composition wiring, not a plugin API: each callback preserves the
    existing live-Agent engine intact while the bound family receives only the
    adapter's three operation methods.
    """
    from lingtai.adapters.tool_plugin_host import AgentContextRuntimeAdapter

    def invoke(action: str, args: dict) -> dict:
        # Look up at call time: Context's narrow focused tests can replace one
        # implementation operation without bypassing the declared host route.
        for name, _schema, operation in _CHILD_SPECS:
            if name == action:
                return operation(agent, args)
        raise AssertionError(f"unknown Context runtime action: {action}")

    return AgentContextRuntimeAdapter(
        molt=lambda args: invoke("molt", args),
        summarize=lambda args: invoke("summarize", args),
        rebuild=lambda args: invoke("rebuild", args),
    )


def _build_children(agent, envelope: Mapping[str, Any] | None = None) -> list[ChildTool]:
    """Compatibility child-list view for direct in-process family tests.

    Normal Agent dispatch never uses this view; it is the declared family bound
    to the same production adapters, returned as children for legacy callers
    that construct their own ``ToolFamily`` around Context.
    """
    from lingtai.adapters.tool_plugin_host import AgentWorkdirAdapter

    host = ToolPluginHost.grant(
        DECLARATION,
        {
            "workdir": AgentWorkdirAdapter(
                lambda: agent.working_dir if hasattr(agent, "working_dir") else agent._working_dir
            ),
            "context_runtime": _runtime_port(agent),
        },
    )
    return list(_build_family(host, envelope)._children.values())


def setup(agent) -> None:
    """Register the mandatory Context family through the official host route."""
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    register_agent_tool_plugins(
        agent,
        [DECLARATION],
        extra_ports={"context_runtime": _runtime_port(agent)},
    )


# Context is mandatory rather than manifest-gated. BaseAgent invokes ``boot``
# after it has a SessionManager and again through its generic official-intrinsic
# refresh hook; ``setup`` remains the sole registrar wiring.
boot = setup


def handle(agent, args: dict) -> dict:
    """Compatibility dispatch for direct in-process callers.

    A live Agent has already mounted the official handler, so this merely routes
    to it. Narrow test doubles that exercise the historic direct function get the
    same declared family through production adapters; neither path hands the
    bound plugin the Agent itself.
    """
    mounted = getattr(agent, "_tool_handlers", {}).get(DECLARATION.name)
    if mounted is not None:
        return mounted(args)

    from lingtai.adapters.tool_plugin_host import AgentWorkdirAdapter

    host = ToolPluginHost.grant(
        DECLARATION,
        {
            "workdir": AgentWorkdirAdapter(
                lambda: agent.working_dir if hasattr(agent, "working_dir") else agent._working_dir
            ),
            "context_runtime": _runtime_port(agent),
        },
    )
    return _bind(host).handler(args)
