"""The declared official ``file`` host plugin.

``file`` remains one public LTP-v2 family with five operational actions, the
generic opt-in ``settings`` action, and ``manual``. This module
now declares that surface statically and binds it only through the kernel's
least-privilege host facade: the current workdir, File's narrow I/O service
port, a bounded immutable File construction snapshot, and no whole Agent.
The operation modules retain their real behavior and
raw result shapes; this module only composes them and hands the bound plugin to
the host-owned official registrar.
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Callable, Mapping

from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration

from .._manual import load_installed_manual
from ..tool_family import ChildTool, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA
from . import _edit, _glob, _grep, _read, _write
from .settings import (
    FILE_IO_CONSTRUCTION_SNAPSHOT_KEY,
    FileSettingsProvider,
)

if TYPE_CHECKING:
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.tool_plugin import ToolPluginHost


PROVIDERS = {"providers": [], "default": "builtin"}

_READ_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Absolute path to the file to read; a relative path resolves under the agent working directory.",
        },
        "offset": {
            "type": ["integer", "null"],
            "description": "Line number to start from (1-based), or null for the default 1.",
        },
        "limit": {
            "type": ["integer", "null"],
            "description": "Max lines to read, or null for the default 2000.",
        },
        "max_chars": {
            "type": ["integer", "null"],
            "description": "Per-call character budget for read content, or null for the default 100 000. Values above the non-configurable runtime hard cap are clamped to 200 000.",
        },
    },
    "required": ["file_path", "offset", "limit", "max_chars"],
    "additionalProperties": False,
}

_WRITE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Absolute path to the file to write; parent directories are created automatically.",
        },
        "content": {"type": "string", "description": "Full content to write."},
    },
    "required": ["file_path", "content"],
    "additionalProperties": False,
}

_EDIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "Absolute path to the file to edit."},
        "old_string": {"type": "string", "description": "The exact text to find and replace."},
        "new_string": {"type": "string", "description": "The replacement text."},
        "replace_all": {
            "type": ["boolean", "null"],
            "description": "Replace all occurrences, or null for the default false.",
        },
    },
    "required": ["file_path", "old_string", "new_string", "replace_all"],
    "additionalProperties": False,
}

_GREP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern to search for."},
        "path": {
            "type": ["string", "null"],
            "description": "File or directory to search in, or null for the agent working directory.",
        },
        "glob": {
            "type": ["string", "null"],
            "description": "File glob filter (e.g. '*.py'), or null for the default '*' (no filter).",
        },
        "max_matches": {
            "type": ["integer", "null"],
            "description": "Maximum matches to return, or null for the default 200.",
        },
    },
    "required": ["pattern", "path", "glob", "max_matches"],
    "additionalProperties": False,
}

_GLOB_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py'); use '**/' for recursive search."},
        "path": {
            "type": ["string", "null"],
            "description": "Directory to search in, or null for the agent working directory.",
        },
    },
    "required": ["pattern", "path"],
    "additionalProperties": False,
}

# This family owns five operational actions. The kernel appends the reserved
# manual slot from ``DECLARATION.manual_input_schema`` exactly once and last.
_DECLARED_ACTIONS: tuple[str, ...] = ("read", "write", "edit", "glob", "grep")
_DECLARED_SCHEMAS_BY_ACTION: dict[str, dict[str, Any]] = {
    "read": _READ_INPUT_SCHEMA,
    "write": _WRITE_INPUT_SCHEMA,
    "edit": _EDIT_INPUT_SCHEMA,
    "glob": _GLOB_INPUT_SCHEMA,
    "grep": _GREP_INPUT_SCHEMA,
}
if tuple(_DECLARED_SCHEMAS_BY_ACTION) != _DECLARED_ACTIONS:
    raise AssertionError("File action order and input-schema inventory diverged")

_OPERATION_MODULES = (_read, _write, _edit, _glob, _grep)

# ``file-manual`` is the established public installation and result path.
_LEGACY_MANUAL_SKILL = "file-manual"


def _load_file_manual(source: Any) -> dict[str, Any]:
    """Load File's installed manual from its established public path."""
    return load_installed_manual(source, _LEGACY_MANUAL_SKILL)


def _to_manual_result(loaded: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical reserved-child result without a generic re-wrap."""
    result: dict[str, Any] = {
        "status": loaded.get("status", "ok"),
        "content": [{"type": "text", "text": loaded.get("manual", "")}],
        "structuredContent": {"manual_path": loaded.get("manual_path", "")},
    }
    if "error" in loaded:
        result["error"] = loaded["error"]
    return result


def _build_manual_child(source: Any) -> ChildTool:
    """Build File's manual child from its established installed-manual path."""
    def handler(_input: Mapping[str, Any]) -> dict[str, Any]:
        return _to_manual_result(_load_file_manual(source))

    return ChildTool(
        name="manual",
        input_schema=copy.deepcopy(MANUAL_INPUT_SCHEMA),
        handler=handler,
        title="manual input",
    )


def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
    raise AssertionError("the module-level schema-only ToolFamily never dispatches")


def _build_family(host: "ToolPluginHost | None") -> ToolFamily:
    """Compose File's declared children with or without a granted host.

    The import-time schema-only build and the per-agent dispatching build share
    this one ordered declaration, so public action names and strict input
    schemas cannot drift from the family that actually dispatches. A real host
    grants precisely the ports the operation modules consume; no operation
    receives the live Agent or a generic service object.
    """
    children: list[ChildTool] = []
    if host is None:
        handlers: tuple[Callable[[Mapping[str, Any]], dict[str, Any]], ...] = (
            _unused,
            _unused,
            _unused,
            _unused,
            _unused,
        )
    else:
        # ToolFamily validates the strict nullable shape first. Only the child
        # adapter translates its declared nulls to absent operation arguments,
        # preserving the historic per-operation defaults without weakening the
        # public schema.
        handlers = tuple(
            lambda action_input, operation=module.build_operation(
                host.workdir, host.file_io
            ): operation(_strip_nulls(action_input))
            for module in _OPERATION_MODULES
        )
    for action, handler in zip(_DECLARED_ACTIONS, handlers, strict=True):
        children.append(
            ChildTool(
                action,
                DECLARATION.input_schemas[action],
                handler,
                title=f"{action} input",
            )
        )
    if host is None:
        children.append(
            ChildTool(
                "manual",
                DECLARATION.manual_input_schema,
                _unused,
                title="manual input",
            )
        )
    else:
        children.append(_build_manual_child(host.workdir))
    if host is None:
        settings_provider = FileSettingsProvider(None, None)
    else:
        settings_provider = FileSettingsProvider(
            host.file_io,
            host.configuration.values.get(FILE_IO_CONSTRUCTION_SNAPSHOT_KEY),
        )
    return ToolFamily(
        DECLARATION.name,
        children,
        settings_provider=settings_provider,
    )


def get_description(lang: str = "en") -> str:
    return (
        "Unified file capability over one working tree. Use "
        "file(action='read', input={'file_path': '/abs/path', 'offset': "
        "null, 'limit': null, 'max_chars': null}) to read numbered lines of "
        "a text file; a successful read can still be truncated, so check "
        "truncated, next_offset, remaining_lines_estimate, and "
        "line_truncated and continue from next_offset until done. Use "
        "file(action='write', ...) to create or "
        "overwrite a whole file and file(action='edit', ...) for an exact "
        "string replacement in an existing file — both mutate the working "
        "tree and return a receipt, but neither reloads the current system "
        "prompt; after changing a durable prompt source, call "
        "context(action='rebuild', input={}, ...) only when it must take "
        "effect now. Use file(action='glob', ...) to find files by pattern "
        "and file(action='grep', ...) to search file contents by regex. "
        "Text files only — this tool cannot read binary, images, or audio. "
        "Use file(action='settings', input={}) to show the exact "
        "read/search limits and UTF-8 policy; source, precedence, and "
        "change procedures live only in file-manual. Use "
        "file(action='manual', input={}) once for the installed "
        "file-manual, which also routes to read-manual for pagination depth "
        "and a bash/Python metadata workflow for content that cannot page "
        "cleanly. After the manual result continue the original operation "
        "instead of repeating manual, because repeated identical manual "
        "calls are an error loop."
    )


def get_schema(lang: str = "en") -> dict[str, Any]:
    # Generic ToolFamily composition owns the LTP envelope and action/input
    # correlation. ``_FAMILY`` is schema-only; the registrar binds the real
    # same declaration to a narrow host later.
    return _FAMILY.build_schema()


def _strip_nulls(action_input: Mapping[str, Any]) -> dict[str, Any]:
    """Translate strict-schema nulls back to absent operation arguments."""
    return {key: value for key, value in action_input.items() if value is not None}


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Purely compose File against its granted ports; mount nothing."""
    family = _build_family(host)

    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=family.handle,
        description=get_description(),
        glossary_package=__package__,
    )


DECLARATION = ToolPluginDeclaration(
    name="file",
    actions=_DECLARED_ACTIONS,
    input_schemas=_DECLARED_SCHEMAS_BY_ACTION,
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    # The package-owned manual is installed at the established
    # ``capabilities/file-manual`` path.
    manual=_LEGACY_MANUAL_SKILL,
    description=get_description(),
    binder=_bind,
    requires=("workdir", "file_io", "configuration"),
    glossary_package=__package__,
    settings=True,
)

# Compatibility aliases for internal callers; both are derived from the one
# static declaration rather than restated tool identity.
ACTIONS = DECLARATION.public_actions
FAMILY_MANUAL_SKILL = DECLARATION.manual

# Construct after DECLARATION because the builder derives every public fact from
# it. The kernel validates the matching advertised enum again on each bind.
_FAMILY = _build_family(None)


def setup(agent: "BaseAgent", **_ignored) -> None:
    """Register File through the official host-plugin route.

    The registrar reserves ``file``, grants only its workdir/file-I/O ports and
    immutable factory snapshot, binds this declaration, and mounts the
    resulting family. Re-running setup on refresh is idempotent for this exact
    declaration.
    """
    from lingtai.adapters.tool_plugin_host import (
        AgentFileIOAdapter,
        StaticConfigurationAdapter,
        register_agent_tool_plugins,
    )
    from lingtai.services.file_io_sidecar import file_io_construction_snapshot

    # Capture only the concrete runtime objects and immutable construction fact
    # File consumes. The adapters store bound operations/narrow facts, never
    # the Agent; the private sidecar value is repr-hidden and SHOW-redacted.
    file_io = agent._file_io
    executor = getattr(agent, "_executor", None)

    def _max_result_chars() -> int | None:
        value = getattr(executor, "_max_result_chars", None)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    file_io_port = AgentFileIOAdapter(
        read=file_io.read,
        write=file_io.write,
        glob=file_io.glob,
        grep=file_io.grep,
        last_traversal=lambda: getattr(file_io, "last_traversal", None),
        max_result_chars=_max_result_chars,
    )
    configuration = StaticConfigurationAdapter(
        {
            FILE_IO_CONSTRUCTION_SNAPSHOT_KEY: file_io_construction_snapshot(
                file_io
            )
        }
    )
    register_agent_tool_plugins(
        agent,
        [DECLARATION],
        extra_ports_for=lambda declaration: (
            {"file_io": file_io_port, "configuration": configuration}
            if declaration is DECLARATION
            else {}
        ),
    )
