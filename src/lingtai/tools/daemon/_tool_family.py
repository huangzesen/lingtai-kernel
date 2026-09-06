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
    """JSON schema for the reserved ``backend_options.env`` overlay.

    Matches the runtime validator: an object whose keys are env-var names
    (``[A-Za-z_][A-Za-z0-9_]*``) and whose values are plain strings.  It is
    declared explicitly so validating tool wires can submit the overlay even
    though ``backend_options``' generic ``additionalProperties`` only accepts
    scalar/array flag values.
    """
    return {
        "type": "object",
        "additionalProperties": {
            "type": "string",
        },
        "propertyNames": {
            "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
        },
        "description": (
            "Reserved: a JSON object of env-var-name -> string injected into "
            "the spawned CLI subprocess environment (never an argv flag); e.g. "
            "{\"CLAUDE_CONFIG_DIR\": \"...\"} for claude-p/claude-code profile "
            "selection. Names must match [A-Za-z_][A-Za-z0-9_]* and values must "
            "be strings."
        ),
    }


def _backend_option_value_schema() -> dict[str, Any]:
    """Return a fresh JSON schema for one generic CLI option value.

    ``backend_options`` remains an open-ended argv passthrough.  A factory keeps
    its explicit provider-visible properties and ``additionalProperties`` on
    the same validation contract without sharing mutable schema nodes.
    """
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
    """One task object inside ``emanate``'s ``tasks`` array.

    Byte-for-byte the pre-migration nested task schema (``task``, ``tools``,
    ``skills``, ``mcp``, ``preset``, ``backend_options``, ``prompt``,
    ``context_token_limit``, ``required: ["task", "tools"]``). The nested
    object is deliberately NOT closed with ``additionalProperties: false``:
    the engine's own strict per-task validation in ``_handle_emanate`` owns
    that boundary and returns domain-specific errors (e.g. the ``system_prompt``
    obsolescence message) that a schema-level rejection would replace with a
    generic one. A factory keeps every call's nested nodes unshared.
    """
    return {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "tools": {"type": "array", "items": {"type": "string"}},
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional skill context for this daemon task. Array of paths; each item may be a skill directory (containing SKILL.md) or a direct SKILL.md file path. Relative paths resolve against the parent agent working directory. The daemon runtime parses each skill frontmatter and injects a compact YAML skill list into this run's system prompt.",
            },
            "mcp": {
                "type": "array",
                "items": {"type": "object"},
                "description": 'Optional one-run MCP registrations for this daemon task. Array of full MCP registration objects: {name, transport/type: stdio|http, command+args+env for stdio or url+headers for http}. The registrations are serialized into the daemon prompt as YAML; LingTai backend also starts them as task-scoped MCP clients and exposes their tools for this run. CLI backends receive the same serialized registrations as context and may load them if their runtime supports MCP. Secret env/header values are redacted in prompts. Exposed tool names must be unique across this task: a plugin mcp.json server and a task-level `mcp` registration that expose the same tool name fail at dispatch with a duplicate-MCP-tool-name error (rename or dedupe one registration).',
            },
            "plugin": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional Agent Plugins for this daemon task. Array of paths; each item may be a plugin directory (containing plugin.json) or a plugin search root whose immediate children are plugin directories. Relative paths resolve against the parent agent working directory. The daemon runtime reads each plugin manifest and injects a compact plugin section (name, summary, skills list, mcp list) into this run's system prompt; plugin skills are also injected as skill context and plugin mcp.json servers are registered as task-scoped MCP clients for the LingTai backend, exactly like the main agent mounts plugins. CLI backends that cannot mount plugins receive the plugin's skills and MCP registrations separately as normal skill/mcp context.",
            },
            "preset": {
                "type": "string",
                "description": "Optional preset file path. Must be a .json/.jsonc path as returned by system(action='presets'). Do NOT use shorthand names — use the full 'name' field from the presets listing. Example: '~/.lingtai-tui/presets/saved/cheap.json'. Omit to inherit the parent's regular (non-MCP) tool surface; provide task MCP registrations separately with `mcp`.",
            },
            "backend_options": {
                "type": "object",
                "properties": {
                    "env": _backend_option_env_schema(),
                    "config": {
                        **_backend_option_value_schema(),
                        "description": (
                            "Explicitly declared so constrained tool-schema "
                            "providers can generate repeated --config overrides; "
                            "uses the same scalar/list contract as generic "
                            "backend_options."
                        ),
                    },
                },
                "additionalProperties": _backend_option_value_schema(),
                "description": 'Optional free-form CLI options for \'claude-code\' / \'codex\' / \'opencode\' / \'mimocode\' / \'qwen-code\' / \'oh-my-pi\' / \'kimicode\' / \'deepseek\' / \'cursor\' backends ONLY (ignored by lingtai). JSON object mapping flag names to values: true → flag only (e.g. {"search": true} → --search); string/int/float → \'--flag <value>\'; list of scalars → \'--flag <v1> --flag <v2>\'; false/null omits the flag. Underscores in keys become dashes; unsafe keys are rejected. One reserved key is not a flag: `env` (a JSON object of string->string) injects environment variables into the spawned CLI subprocess (e.g. {"env": {"CLAUDE_CONFIG_DIR": "..."}} for claude-p/claude-code profile selection); its names must match [A-Za-z_][A-Za-z0-9_]* and its values must be strings. All other nested objects are rejected. Applies only when starting the emanation (not to `ask`). Discover supported flags by running \'claude --help\', \'codex exec --help\', \'opencode run --help\', \'mimo run --help\', \'qwen --help\', \'omp --help\', \'kimi --help\', \'dsh --help\', or \'agent --help\' in bash — the CLI\'s flag list changes between versions; this field is intentionally a passthrough rather than a fixed list. See daemon-manual.',
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
                "description": 'Optional per-task input files for this daemon run. Array of {path, label?, role?}: path is a UTF-8 text file under the parent agent working directory (absolute or relative; relative paths resolve against the parent working directory). At dispatch the parent resolves every path, validates UTF-8 text and size limits, snapshots the bytes content-addressed into an immutable read-only input store, and the daemon receives only a compact manifest pointing at the snapshot paths \u2014 never the file contents and never the mutable original paths. Malformed, out-of-root, missing, non-UTF-8, or oversize entries refuse the whole batch before any run starts.',
            },
            "prompt": {
                "type": "string",
                "description": 'Optional first ordinary user message for a LingTai daemon. Blank or omitted uses exactly "Begin the assigned daemon task.". Unsupported by external CLI backends.',
            },
            "context_token_limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Provider-context compaction threshold for this daemon task (rendered/provider-context tokens, not cumulative spend). This threshold does not set daemon context. Effective only for backend='lingtai' tasks whose resolved provider is Codex ('codex'/'codex-pool') or the native 'mimo' provider; every other provider and external CLI backend ignores it. When the provider-visible input-token count reaches the limit, the runtime compacts provider context and continues the same tool loop: a Codex compaction failure skips that attempt (loop continues on full history), while a 'mimo' compaction failure is a hard failure that propagates to the caller. Omit to use the daemon session's own resolved context window as this separate provider-compaction threshold: for an explicit preset, valid canonical manifest.llm.context_limit or 272,000 fallback; for implicit/no-preset, a valid inherited parent effective window or 272,000 fallback. See daemon-manual for exact resolution. Positive integer.",
            },
        },
        "required": ["task", "tools"],
    }


def _emanate_input_schema(backend_enum: list[str]) -> dict[str, Any]:
    """``emanate``'s own strict input.

    ``backend_enum`` is the engine's ``_BACKEND_SCHEMA_ENUM``, passed in rather
    than imported to keep this module free of a circular package import; the
    one public schema builder below supplies it.

    Optionals are required-nullable because that is what strict OpenAI-style
    validators demand of a closed object; null means "absent" to the engine
    (see ``_strip_nulls``), which then applies its own unchanged default —
    ``max_turns=None``/``timeout=None`` and ``backend="lingtai"`` exactly as a
    legacy flat caller that omitted the field got.
    """
    return {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": _emanate_task_schema(),
                "description": "List of task objects for 'emanate'. Each: {task: str (required — instructions including where to save work), tools: list[str] (required — capability names, e.g. ['file', 'shell']), skills: list[str] (optional — skill directory or SKILL.md paths to render into the daemon prompt), mcp: list[object] (optional — full one-run MCP registrations to serialize into daemon context; LingTai backend also loads them as task-scoped MCP tools), preset: str (optional — preset file path, use name from system(action='presets') output). Omit preset to inherit the parent's regular tool surface. Parent MCP tools are not auto-inherited; provide complete task mcp registrations when needed. See daemon-manual for preset inheritance and capability resolution details.",
            },
            "backend": {
                "type": ["string", "null"],
                "enum": [*backend_enum, None],
                "description": (
                    "Execution backend: 'lingtai' (default — parallel LLM reasoning, uses your current model), "
                    "'claude-p' (Claude Code print-mode backend; 'claude-code' is a compatibility alias), "
                    "'codex' (coding tasks via OpenAI Codex CLI), "
                    "'opencode' (multi-provider open-source agent via the opencode-ai CLI), "
                    "'mimocode' / 'mimo' (MiMo Code CLI), "
                    "'qwen-code' / 'qwen' (Qwen Code CLI), "
                    "'oh-my-pi' / 'omp' (Oh-My-Pi pi-coding-agent CLI), "
                    "'kimicode' / 'kimi' (MoonshotAI Kimi Code CLI; ask/resume not supported yet), "
                    "'deepseek' (DeepSeek Harness dsh CLI; ask/resume not supported yet), "
                    "'cursor' (coding tasks via Cursor Agent CLI). "
                    "CLI backends use external tools with no LLM overhead from the parent. "
                    "Null for the default 'lingtai'."
                ),
            },
            "max_turns": {
                "type": ["integer", "null"],
                "minimum": 1,
                "maximum": DEFAULT_MAX_TURNS,
                "description": "For 'emanate': max LLM tool-loop turns per emanation. Default: parent ceiling (5000, or the agent's daemon/daemon.json max_turns when set). Use a smaller value to keep simple emanations bounded. Null for the default.",
            },
            "timeout": {
                "type": ["number", "null"],
                "minimum": 5,
                "description": "For 'emanate': max wall-clock seconds for the whole batch. Default when omitted: 3600s. Watchdog kills any emanation still running when the timer fires. Null for the default.",
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
            "description": "For 'list': case-insensitive substring search across daemon task, prompt preview, result preview, backend, status, group_id, run_id, and visible call parameters. Null or empty for no search filter.",
        },
        "status": {
            "type": ["string", "null"],
            "description": "For 'list': optional status filter such as running, done, failed, cancelled, timeout, or all. Null for the default 'all'.",
        },
        "include_done": {
            "type": ["boolean", "null"],
            "description": "For 'list': include completed historical daemon run directories as well as currently tracked runs. Defaults to true. Null for the default.",
        },
        "last": {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": "For 'list': positive maximum number of list entries to show after filtering. Omitted or null returns the newest 1000 entries; pass a positive value explicitly to request a different count (including more than 1000).",
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
            "description": "Subagent ID for 'ask' action (e.g. 'em-1')",
        },
        "message": {
            "type": "string",
            "description": "Follow-up message for 'ask' action",
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
            "description": "Subagent ID to inspect (e.g. 'em-1'). Accepts a currently tracked emanation or a historical run id.",
        },
        "last": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": CHECK_LAST_MAX,
            "description": "For 'check': how many most-recent events to return from events.jsonl. Default 20. Null for the default.",
        },
        "truncate": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "For 'check': maximum length of any string field in returned events; longer fields get an ellipsis suffix. Default 500. Set to 0 to disable. Null for the default.",
        },
    },
    "required": ["id", "last", "truncate"],
    "additionalProperties": False,
}

#: ``reclaim`` takes no input — the canonical strict-empty object, the same
#: spelling ``MANUAL_INPUT_SCHEMA`` uses, restated as its own literal because
#: it is a distinct child (a shared mutable object would let one child's
#: schema edit the other's).
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
