"""ToolFamily envelope for the canonical ``daemon`` tool.

The public/model-facing ``daemon`` tool is migrated to the LingTai Tool
Protocol v2 action-separated shape (``action``/``input``/``reasoning``/
``summarize``, see ``../CONTRACT.md``) using the generic, optional
``tool_family`` infrastructure (``../tool_family/__init__.py``). ``emanate``,
``list``, ``ask``, ``check``, ``reclaim``, the opted-in read-only
``settings`` action, and the reserved ``manual`` become seven ``ChildTool``s
with their own strict per-action ``input`` schemas —
``tasks``/``backend``/``max_turns``/``timeout`` live only in ``emanate``'s
branch; ``contains``/``status``/``include_done`` only in ``list``'s;
``id``/``message`` only in ``ask``'s; ``id``/``last``/``truncate`` only in
``check``'s; ``reclaim``, ``settings``, and ``manual`` take the canonical
strict-empty ``input``. The pre-migration flat root advertised all thirteen fields to all
six actions at once (``tasks`` sat next to ``truncate`` next to ``message``),
so nothing at the schema level said which field belonged to which action.

``DaemonManager`` (``__init__.py``) is the unchanged execution engine: its
``handle()`` still accepts the historical flat legacy shape
(``{"action": "check", "id": ..., "last": ...}``) and its full lifecycle —
batch emanation, backend routing, run directories, the detached supervisor,
``daemon_common`` completion signaling, cancellation, timeouts, terminal
notifications, and result/error persistence — is untouched. This module only
translates the new envelope into that flat shape before delegating, so every
existing ``DaemonManager`` test keeps exercising the exact same code path.
That flat shape is internal only — this module owns the package's single
public ``get_schema``/``get_description`` pair, re-exported from
``daemon/__init__.py``.

This is the same division ``shell`` (``../bash/_tool_family.py``) uses between
``ToolFamily.handle()`` (envelope validation/dispatch) and a legacy engine:
the *inner* engine keeps the flat shape and the *outer* envelope is new.
Children never consume a second model tool slot — ``daemon`` remains exactly
one registered public tool.

Two behaviors deliberately do NOT move here:

* ``manual`` is served by the shared ``build_manual_child(agent, "daemon")``
  reserved child, which reads the same installed
  ``.library/intrinsic/capabilities/daemon/SKILL.md`` body/path the
  pre-migration ``DaemonManager.handle({"action": "manual"})`` branch read via
  ``load_installed_manual``. It returns the canonical
  ``content``/``structuredContent`` shape and performs no daemon operation.
* The legacy flat root ``summary`` boolean is replaced by the canonical root
  ``summarize`` control, which ``ToolFamily.build_schema`` advertises and
  ``ToolFamily.handle`` strips before any child handler runs. ``daemon`` joins
  ``kernel/tool_result_summary.py``'s ``_LTP_V2_MIGRATED_FAMILIES`` in the
  same change so that spelling is actually honored rather than silently
  ignored; the legacy ``summary`` spelling stays accepted there for any
  historical/pending call, exactly as ``shell``'s migration left it.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..tool_family import ChildTool, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child

# Re-exported engine constants the child schemas pin. Imported lazily inside
# ``_emanate_input_schema`` would hide them from readers; a module-level import
# from the package ``__init__`` would be circular (``__init__`` imports this
# module), so the two literals are restated here with an import-time assertion
# in ``__init__.py`` proving they still match the engine's own values.
DEFAULT_MAX_TURNS = 5000
CHECK_LAST_MAX = 1000
LIST_DEFAULT_LAST = 1000

#: Daemon's operational actions.  The static host declaration appends the
#: kernel-reserved ``manual`` action; this retained export keeps existing schema
#: and migration callers on the same public inventory.
DAEMON_DECLARED_ACTIONS: tuple[str, ...] = (
    "emanate",
    "list",
    "ask",
    "check",
    "reclaim",
)
DAEMON_ACTIONS: tuple[str, ...] = (*DAEMON_DECLARED_ACTIONS, "settings", "manual")


def _backend_option_env_schema() -> dict[str, Any]:
    """Schema for the reserved CLI subprocess environment overlay."""
    return {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "propertyNames": {"pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
        "description": (
            "Reserved `env` overlay: environment-name to string values for the "
            "spawned CLI; it emits no argv flag. Names must match "
            "[A-Za-z_][A-Za-z0-9_]*."
        ),
    }


def _backend_option_value_schema() -> dict[str, Any]:
    """Return a fresh schema for one generic CLI option value."""
    return {
        "anyOf": [
            {"type": "boolean"},
            {"type": "string"},
            {"type": "integer"},
            {"type": "number"},
            {"type": "null"},
            {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "integer"},
                        {"type": "number"},
                    ],
                },
            },
        ],
    }


def _emanate_task_schema() -> dict[str, Any]:
    """One task object inside ``emanate``'s ``tasks`` array."""
    return {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "tools": {"type": "array", "items": {"type": "string"}},
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional skill directories or SKILL.md paths; relative paths "
                    "use the parent working directory. Runtime injects a compact "
                    "frontmatter catalog; the worker reads selected skills."
                ),
            },
            "mcp": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Optional one-run stdio/http registrations; env/headers are "
                    "redacted, LingTai mounts task clients, and exposed tool names "
                    "must be unique."
                ),
            },
            "plugin": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional plugin directories/search roots relative to the parent; "
                    "manifests merge skills/MCP for the run. Missing paths are ignored."
                ),
            },
            "preset": {
                "type": "string",
                "description": (
                    "Optional authorized .json/.jsonc path from "
                    "system(action='presets'); use its full path. Omission inherits "
                    "the parent's regular tools; task MCP remains explicit."
                ),
            },
            "backend_options": {
                "type": "object",
                "properties": {
                    "env": _backend_option_env_schema(),
                    "config": {
                        **_backend_option_value_schema(),
                        "description": "CLI config override using the generic scalar/list option contract.",
                    },
                },
                "additionalProperties": _backend_option_value_schema(),
                "description": (
                    "CLI-only options (ignored by lingtai): booleans emit flags, "
                    "scalars values, lists repeat flags, and false/null omit them; "
                    "`env` is a string environment overlay. Unsafe/reserved keys fail "
                    "preflight; use only at emanate. Verify --help (for example "
                    "`opencode run --help`) before passing options."
                ),
            },
            "task_files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "label": {"type": "string"},
                        "role": {"type": "string"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "description": (
                    "Optional UTF-8 {path, label?, role?} files under the parent. "
                    "Dispatch snapshots bytes into an immutable store and exposes only "
                    "metadata/snapshot paths; malformed, missing, out-of-root, "
                    "non-UTF-8, or oversize entries fail the batch."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    'Optional first LingTai user message. Blank/omitted means exactly '
                    '"Begin the assigned daemon task."; external CLIs reject it.'
                ),
            },
            "context_token_limit": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Positive provider-compaction threshold, not a spend or window "
                    "setting. Applies only to supported native LingTai providers; "
                    "external CLI backends and other providers ignore it. Omission "
                    "uses the resolved session window. Native failure behavior and "
                    "window resolution live in the built-in LingTai child/manual."
                ),
            },
        },
        "required": ["task", "tools"],
    }


def _emanate_input_schema(backend_enum: list[str]) -> dict[str, Any]:
    """Return ``emanate``'s strict input schema."""
    return {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": _emanate_task_schema(),
                "description": (
                    "Required task objects: `task` is the complete parent-controlled "
                    "instruction and `tools` its capability list. Optional fields select "
                    "skills, MCP, plugins, presets, files, prompt, limits, or CLI options; "
                    "parent MCP tools are not inherited."
                ),
            },
            "backend": {
                "type": ["string", "null"],
                "enum": [*backend_enum, None],
                "description": (
                    "Execution backend: `lingtai` is the default in-process session; "
                    "other enum values select external CLIs (aliases accepted). "
                    "Support, MCP/completion status, and constraints are in the "
                    "daemon-manual CLI reference. Null uses lingtai."
                ),
            },
            "max_turns": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": DEFAULT_MAX_TURNS,
                "description": (
                    "Positive LLM tool-loop turns per emanation, capped at 5000; "
                    "null uses the manager default/ceiling."
                ),
            },
            "timeout": {
                "type": ["number", "null"],
                "minimum": 5,
                "description": (
                    "Batch wall-clock seconds (minimum 5, no upper bound); null uses "
                    "the manager default of 3600 seconds and the watchdog terminates "
                    "overruns."
                ),
            },
        },
        "required": ["tasks", "backend", "max_turns", "timeout"],
        "additionalProperties": False,
    }


LIST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contains": {
            "type": ["string", "null"],
            "description": "Case-insensitive search over the bounded visible daemon index. Null or empty disables filtering.",
        },
        "status": {
            "type": ["string", "null"],
            "description": "Optional status filter: running, done, failed, cancelled, timeout, or all. Null uses all.",
        },
        "include_done": {
            "type": ["boolean", "null"],
            "description": "Include completed historical entries as well as tracked runs; defaults to true. Null uses the default.",
        },
        "last": {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": "Positive result limit. Null uses the newest 1000 ledger entries; explicit values, including above 1000, are honored.",
        },
    },
    "required": ["contains", "status", "include_done", "last"],
    "additionalProperties": False,
}


ASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "Emanation id or run id to follow up (for example, `em-a1b2`).",
        },
        "message": {
            "type": "string",
            "description": "Follow-up message; delivery is backend-specific and may be asynchronous or checkpoint-queued.",
        },
    },
    "required": ["id", "message"],
    "additionalProperties": False,
}


CHECK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "Emanation id or exact historical run id to inspect.",
        },
        "last": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": CHECK_LAST_MAX,
            "description": "Number of newest events from events.jsonl; defaults to 20 and is capped at 1000. Null uses the default.",
        },
        "truncate": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Maximum returned event-string length; defaults to 500. Zero disables truncation; null uses the default.",
        },
    },
    "required": ["id", "last", "truncate"],
    "additionalProperties": False,
}


#: ``reclaim`` takes no input — the canonical strict-empty object.
RECLAIM_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

def _child_specs(backend_enum: list[str]) -> tuple[tuple[str, dict[str, Any]], ...]:
    """The one child registry source: canonical action name → strict input schema.

    Both the module-level schema-only family and each dispatcher's
    handler-bound family are built from this single source, so the composed
    schema and the dispatch registry cannot drift apart.
    """
    return (
        ("emanate", _emanate_input_schema(backend_enum)),
        ("list", LIST_INPUT_SCHEMA),
        ("ask", ASK_INPUT_SCHEMA),
        ("check", CHECK_INPUT_SCHEMA),
        ("reclaim", RECLAIM_INPUT_SCHEMA),
        ("manual", MANUAL_INPUT_SCHEMA),
    )


def declared_input_schemas(backend_enum: list[str]) -> dict[str, dict[str, Any]]:
    """Daemon's operational input schemas, in declaration action order."""
    specs = dict(_child_specs(backend_enum))
    return {name: specs[name] for name in DAEMON_DECLARED_ACTIONS}


def build_schema(
    backend_enum: list[str], lang: str = "en", declaration: Any | None = None,
) -> dict[str, Any]:
    """Compose the action-separated public ``daemon`` schema.

    Generated purely from the child registry by the generic ``ToolFamily``
    infra (root ``allOf`` correlation plus composed ``input`` disclosure) — this
    is the schema registered for the public ``daemon`` tool, and the only one
    the package defines. Constructing the family here is also the registry's
    duplicate/reserved-``manual``-collision check.
    """
    _ = lang  # The daemon tool surface is canonical English.

    def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("the schema-only ToolFamily never dispatches")

    if declaration is None:
        name = "daemon"
        specs = _child_specs(backend_enum)
    else:
        name = declaration.name
        declared = declared_input_schemas(backend_enum)
        specs = tuple(
            (action, declared[action]) for action in declaration.actions
        ) + (("manual", declaration.manual_input_schema),)
    family = ToolFamily(
        name,
        [
            ChildTool(action, schema, _unused, title=f"{action} input")
            for action, schema in specs
        ],
        settings_provider=(
            tuple
            if declaration is not None and declaration.settings
            else None
        ),
    )
    return family.build_schema()


class DaemonFamilyDispatcher:
    """Adapts the ``action``/``input`` envelope to ``DaemonManager``'s legacy flat call shape.

    Built per-agent, bound to one live ``DaemonManager``. ``handle()`` is the
    public ``daemon`` tool's registered handler: ``ToolFamily.handle()``
    validates the envelope and dispatches to exactly one of the seven
    ``ChildTool`` handlers below, each of which flattens its own validated
    ``input`` mapping (injecting the matching ``action`` key, mirroring the
    legacy dispatch contract in ``DaemonManager.handle``) and calls
    ``DaemonManager.handle()`` unchanged — so every side effect, receipt,
    process interaction, notification, and error string stays exactly what the
    engine already produces.

    ``manual`` is registered directly, unwrapped, from ``build_manual_child``
    — the shared reserved-name contract every LingTai family uses — so
    ``ToolFamily.handle()`` returns its canonical
    ``content``/``structuredContent`` result verbatim with no double wrap. It
    is the family's sole manual surface and reaches no engine method, so
    ``manual`` performs no daemon operation. ``DaemonManager.handle``'s own
    ``action == "manual"`` branch is retained for historical/internal flat
    callers but is never the registered model-facing path.
    """

    def __init__(
        self,
        manager: Any,
        manual_source: Any,
        backend_enum: list[str],
        declaration: Any | None = None,
    ) -> None:
        self._manager = manager
        if declaration is None:
            from . import DECLARATION
            declaration = DECLARATION
        if declaration.settings:
            from .settings import daemon_setting_rows

            settings_provider = lambda: daemon_setting_rows(manager)
        else:
            settings_provider = None
        specs = declared_input_schemas(backend_enum)
        self._family = ToolFamily(
            declaration.name,
            [
                ChildTool("emanate", specs["emanate"], self._dispatch_emanate, title="emanate input"),
                ChildTool("list", specs["list"], self._dispatch_list, title="list input"),
                ChildTool("ask", specs["ask"], self._dispatch_ask, title="ask input"),
                ChildTool("check", specs["check"], self._dispatch_check, title="check input"),
                ChildTool("reclaim", specs["reclaim"], self._dispatch_reclaim, title="reclaim input"),
                build_manual_child(manual_source, declaration.manual),
            ],
            settings_provider=settings_provider,
        )

    @staticmethod
    def _strip_nulls(action_input: Mapping[str, Any]) -> dict[str, Any]:
        # Strict schemas express optional fields as required nullable
        # properties. Null means *absent* to ``DaemonManager``, which then
        # applies its own unchanged runtime default — dropping the key is what
        # makes ``args.get("last", 20)`` / ``args.get("include_done", True)``
        # behave exactly as they did for a legacy flat caller that simply
        # omitted the field. A falsy-but-present value (``truncate: 0``,
        # ``include_done: False``, ``contains: ""``) is preserved verbatim,
        # never dropped.
        return {key: value for key, value in action_input.items() if value is not None}

    def _dispatch_emanate(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        flat = self._strip_nulls(action_input)
        flat["action"] = "emanate"
        flat.setdefault("tasks", [])
        return self._manager.handle(flat)

    def _dispatch_list(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        flat = self._strip_nulls(action_input)
        flat["action"] = "list"
        return self._manager.handle(flat)

    def _dispatch_ask(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        return self._manager.handle(
            {
                "action": "ask",
                "id": action_input.get("id", ""),
                "message": action_input.get("message", ""),
            }
        )

    def _dispatch_check(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        flat = self._strip_nulls(action_input)
        flat["action"] = "check"
        flat.setdefault("id", "")
        return self._manager.handle(flat)

    def _dispatch_reclaim(self, _action_input: Mapping[str, Any]) -> dict[str, Any]:
        return self._manager.handle({"action": "reclaim"})

    def handle(self, args: Mapping[str, Any] | None) -> dict[str, Any]:
        # ``ToolFamily.handle`` validates the envelope, strips/type-checks
        # root ``summarize``, rejects unknown root fields and cross-action
        # ``input`` keys, then returns the selected child's own raw canonical
        # result verbatim — no double wrap, no Host envelope. The one Host
        # normalization below narrows the generic dispatcher's action list to
        # Daemon's exact seven; the generic canonical error shape
        # (``status``/``error_code``/``message``) is left untouched.
        result = self._family.handle(args)
        if result.get("error_code") == "ACTION_REQUIRED":
            result["message"] = (
                "action must be one of emanate, list, ask, check, reclaim, settings, or manual"
            )
        return result
