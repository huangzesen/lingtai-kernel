"""ToolFamily envelope for the canonical ``shell`` tool.

The public/model-facing ``shell`` tool is migrated to the LingTai Tool
Protocol v2 action-separated shape (``action``/``input``/``reasoning``/
``summarize``, see ``../CONTRACT.md``) using the generic, optional
``tool_family`` infrastructure (``../tool_family/__init__.py``). ``run``,
``poll``, ``cancel``, and the reserved ``settings``/``manual`` actions become
five ``ChildTool``s
with their own strict per-action ``input`` schemas — run-only fields
(``command``, ``timeout``, ``working_dir``, ``async``, ``reminder``) live only
in ``run``'s branch; ``job_id`` lives only in ``poll``/``cancel``'s branches.

``ShellManager`` (``__init__.py``) is the unchanged execution engine: its
``handle()`` still accepts the historical flat legacy shape
(``{"action": "run", "command": ..., ...}``, ``action`` defaulting to
``"run"``) and its full async lifecycle (leases, durable state, reminders,
completion notifications, cancellation) is untouched — this module only
translates the new envelope into that flat shape before delegating, so every
existing ``ShellManager`` test keeps exercising the exact same code path. That
flat shape is internal only — this module owns the package's single public
``get_schema``/``get_description`` pair, re-exported from ``__init__.py``.
This is the same division ``web`` uses between
``ToolFamily.handle()`` (envelope validation/dispatch) and its own
Host/presentation adaptation, applied in the opposite direction: here the
*inner* engine is the legacy shape and the *outer* envelope is new.
"""
from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING, Any, Mapping

from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration

from ..tool_family import ChildTool, SettingRow, SettingsProvider, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA as _MANUAL_INPUT_SCHEMA, build_manual_child
from ._shell_dialect import ShellKind, posix_shell_display_name

if TYPE_CHECKING:
    from lingtai.kernel.tool_plugin import ToolPluginHost

_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_ASYNC_REMINDER_SECONDS = 1800.0

# Hard ceiling for the sync ``run`` ``timeout`` parameter (Jason 2026-08-10
# tool-timeout redesign).  The default timeout stays 30s; a tool call may set
# ``timeout`` at most to this cap; work that needs more must be launched with
# ``async=true``.  The cap is an environment variable (not an arbitrary
# per-config value), so it is enforced uniformly and can be tuned without
# touching the schema.
TIMEOUT_MAX_ENV = "LINGTAI_TOOL_TIMEOUT_MAX_SECONDS"
_DEFAULT_TIMEOUT_MAX_SECONDS = 120.0


def resolve_timeout_max_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Resolve the hard sync-timeout ceiling from the environment.

    Reads ``LINGTAI_TOOL_TIMEOUT_MAX_SECONDS``; missing, empty, non-numeric,
    non-positive, or non-finite values fall back to ``120.0``.  The returned
    ceiling is never below the default sync timeout (30), so an operator who
    sets the variable to e.g. 10 does not disable every default sync run
    (fable r1 BLOCKING-1).  Reads at each ``run`` call (no restart required),
    mirroring the Nudge policy controls.
    """
    raw = (os.environ if environ is None else environ).get(TIMEOUT_MAX_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_TIMEOUT_MAX_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_MAX_SECONDS
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_TIMEOUT_MAX_SECONDS
    return max(value, float(_DEFAULT_TIMEOUT_SECONDS))


def _shell_setting_rows(manager: Any) -> tuple[SettingRow, ...]:
    """Read applied Shell facts without changing owner or process state."""
    shell_kind = manager.shell_kind
    if not isinstance(shell_kind, ShellKind):
        raise RuntimeError("current shell kind is unavailable")
    max_output = manager._max_output
    if type(max_output) is not int or max_output <= 0:
        raise RuntimeError("current result limit is unavailable")
    policy = manager._policy
    if not callable(getattr(policy, "is_allowed", None)):
        raise RuntimeError("current command policy is unavailable")
    return (
        SettingRow(
            "shell_kind",
            shell_kind.value,
            None,
            True,
            "shell-manual#shell-kind",
        ),
        SettingRow(
            "sync_timeout_default_seconds",
            _DEFAULT_TIMEOUT_SECONDS,
            _DEFAULT_TIMEOUT_SECONDS,
            False,
            "shell-manual#sync-timeout-default",
        ),
        SettingRow(
            "sync_timeout_max_seconds",
            resolve_timeout_max_seconds(),
            _DEFAULT_TIMEOUT_MAX_SECONDS,
            True,
            "shell-manual#sync-timeout-ceiling",
        ),
        SettingRow(
            "result_max_chars",
            max_output,
            50_000,
            True,
            "shell-manual#result-size-limit",
        ),
        SettingRow(
            "async_default",
            False,
            False,
            False,
            "shell-manual#async-default",
        ),
        SettingRow(
            "async_reminder_default_seconds",
            _DEFAULT_ASYNC_REMINDER_SECONDS,
            _DEFAULT_ASYNC_REMINDER_SECONDS,
            False,
            "shell-manual#async-reminder-default",
        ),
        SettingRow(
            "command_policy",
            "configured",
            "platform-packaged",
            True,
            "shell-manual#command-policy",
            _sensitive=True,
        ),
    )

RUN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The shell command to execute",
        },
        "timeout": {
            "type": ["number", "null"],
            "description": (
                "Timeout in seconds, or null for the default 30. Only for sync "
                "execution. Hard ceiling: "
                f"{TIMEOUT_MAX_ENV} (default {_DEFAULT_TIMEOUT_MAX_SECONDS:g}; "
                "the effective ceiling is read from the environment at call "
                "time, floored at 30); a value above the ceiling is refused \u2014 "
                "use async=true instead for work that may need longer."
            ),
            "default": _DEFAULT_TIMEOUT_SECONDS,
        },
        "working_dir": {
            "type": ["string", "null"],
            "description": (
                "Working directory for the command; use null (or an empty "
                "string) to use the agent working directory. Must be inside "
                "the agent working directory sandbox; paths outside it are "
                "rejected. For external repos/paths, keep working_dir at the "
                "agent dir and put an explicit cd in command, e.g. "
                "cd /absolute/path && ..."
            ),
        },
        "async": {
            "type": ["boolean", "null"],
            "description": (
                "Run command in background and return immediately with a "
                "job_id, or null for the default false."
            ),
            "default": False,
        },
        "reminder": {
            "type": ["number", "null"],
            "description": (
                "Last-resort async wake delay in seconds, or null for the "
                "default 1800. Only "
                "meaningful when async is true: if the job is still non-terminal "
                "when the durable deadline expires, publish a system "
                "notification reminding you to poll it; exact completion "
                "suppresses this stale watchdog and publishes the shell "
                "completion wake instead."
            ),
            "default": _DEFAULT_ASYNC_REMINDER_SECONDS,
        },
    },
    # Strict OpenAI schemas express an optional field as a REQUIRED nullable
    # property (the same convention ``web``'s ``browse`` child uses): every
    # run-only field is listed here, and null means "absent" to
    # ``ShellManager``, which then applies its own unchanged runtime default
    # (timeout 30, working_dir = the agent sandbox, async false, reminder
    # 1800). ``command`` is the one genuinely required field and is
    # non-nullable.
    "required": ["command", "timeout", "working_dir", "async", "reminder"],
    "additionalProperties": False,
}

POLL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "Job ID for the poll action (returned by an async run)",
        },
    },
    "required": ["job_id"],
    "additionalProperties": False,
}

CANCEL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "Job ID for the cancel action (returned by an async run)",
        },
    },
    "required": ["job_id"],
    "additionalProperties": False,
}

# The reserved child's one strict-empty schema.  The static declaration below
# holds this exact source, and every schema-only/dispatching family reads it
# back from that declaration; no Shell-local manual spelling can drift.
MANUAL_INPUT_SCHEMA: dict[str, Any] = _MANUAL_INPUT_SCHEMA

# Shell's operational actions and schemas are declared once.  ``manual`` is
# deliberately absent: ToolPluginDeclaration appends it from the shared manual
# child and refuses a family that tries to claim the reserved slot itself.
_DECLARED_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "run": RUN_INPUT_SCHEMA,
    "poll": POLL_INPUT_SCHEMA,
    "cancel": CANCEL_INPUT_SCHEMA,
}


def get_description(
    lang: str = "en",
    dialect: str = "posix",
    host_os: str | None = None,
    shell_kind: "ShellKind | str | None" = None,
) -> str:
    host = f" Host OS: {host_os}." if host_os else ""
    kind = ShellKind.coerce(shell_kind) or ShellKind.coerce(dialect)
    display_name = (
        posix_shell_display_name() if kind is ShellKind.POSIX
        else kind.display_name if kind is not None else None
    )
    shell_prose = (
        f" Active shell: {display_name}. {kind.sequencing_guidance}"
        if kind is not None else ""
    )
    return (
        f"Execute a shell command and return stdout/stderr. Active shell dialect: {dialect}.{shell_prose}{host} "
        "The dialect and host OS are detected at setup time; calls cannot choose them. Any system program — scripts, git, curl, pip, or data pipelines. "
        "Before ordinary shell work, read the manual: shell(action='manual', input={}, reasoning='...'). "
        "For a first call use shell(action='run', input={'command': '...'}, reasoning='...'); "
        "async runs return a job_id for shell(action='poll', input={'job_id': '...'}, reasoning='...') or cancel. "
        "Completed results include exit_code, ok, command_status ('success'/'failed'), and possibly warning. "
        "The top-level status only says the shell spawned the command, even when it fails; always check exit_code/ok and warning. "
        "Sync runs honor the timeout ceiling; on Windows a kill-on-close Job Object terminates surviving descendants, so use input.async=true for work that must outlive the command. "
        "Prefer `rg --files` over broad recursive scans and parse JSONL line-by-line. See the manual references for async lifecycle, wakeups, and scheduling."
    )


class ShellFamilyDispatcher:
    """Adapt the public family envelope to ShellManager's retained flat engine.

    The three execution children flatten only their selected, validated input
    and delegate unchanged to ``ShellManager.handle``.  ``manual`` is composed
    separately from the declaration's installed destination, so it never
    reaches the execution engine or a whole Agent.
    """

    def __init__(self, manager: Any, manual_source: Any) -> None:
        self._manager = manager
        self._family = _build_dispatching_family(self, manual_source)

    @property
    def manager(self) -> Any:
        """The retained execution manager, for setup's compatibility return."""
        return self._manager

    @staticmethod
    def _strip_nulls(action_input: Mapping[str, Any]) -> dict[str, Any]:
        # Required-but-nullable optional fields use null to mean omitted; retain
        # false/zero/empty-string values exactly as the pre-plugin engine did.
        return {key: value for key, value in action_input.items() if value is not None}

    def _dispatch_run(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        flat = self._strip_nulls(action_input)
        flat["action"] = "run"
        flat.setdefault("command", "")
        return self._manager.handle(flat)

    def _dispatch_poll(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        return self._manager.handle({"action": "poll", "job_id": action_input.get("job_id", "")})

    def _dispatch_cancel(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        return self._manager.handle({"action": "cancel", "job_id": action_input.get("job_id", "")})

    def handle(self, args: Mapping[str, Any] | None) -> dict[str, Any]:
        result = self._family.handle(args)
        if result.get("error_code") == "ACTION_REQUIRED":
            actions = ", ".join(DECLARATION.public_actions[:-1])
            result["message"] = f"action must be one of {actions}, or {DECLARATION.public_actions[-1]}"
        return result


def _build_family() -> ToolFamily:
    """Compose the import-time schema-only family from DECLARATION."""
    def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("the module-level schema-only ToolFamily never dispatches")

    children = [
        ChildTool(action, DECLARATION.input_schemas[action], _unused, title=f"{action} input")
        for action in DECLARATION.actions
    ]
    children.append(
        ChildTool("manual", DECLARATION.manual_input_schema, _unused, title="manual input")
    )
    def _schema_only_settings() -> tuple[SettingRow, ...]:
        return ()

    return ToolFamily(
        DECLARATION.name,
        children,
        settings_provider=_schema_only_settings,
    )


def _build_dispatching_family(dispatcher: ShellFamilyDispatcher, manual_source: Any) -> ToolFamily:
    """Build the granted family with dispatcher-owned handlers and one manual."""
    settings_provider: SettingsProvider = lambda: _shell_setting_rows(dispatcher.manager)
    return ToolFamily(
        DECLARATION.name,
        [
            ChildTool("run", DECLARATION.input_schemas["run"], dispatcher._dispatch_run, title="run input"),
            ChildTool("poll", DECLARATION.input_schemas["poll"], dispatcher._dispatch_poll, title="poll input"),
            ChildTool("cancel", DECLARATION.input_schemas["cancel"], dispatcher._dispatch_cancel, title="cancel input"),
            build_manual_child(manual_source, DECLARATION.manual),
        ],
        settings_provider=settings_provider,
    )


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Bind Shell against only its declared workdir/config/notification ports."""
    from . import (
        ShellManager,
        ShellPolicy,
        _DEFAULT_POLICY_FILE,
        _POWERSHELL_POLICY_FILE,
        _DETACHED_DAEMON_ASYNC_HANDOFF,
        _DetachedDaemonShellBinding,
        _describe_host_os,
        _resolve_shell_kind,
        _select_shell_dialect,
    )

    values = host.configuration.values
    kind = _resolve_shell_kind(ShellKind.coerce(values.get("shell_kind")))
    dialect = _select_shell_dialect(kind)
    policy_file = values.get("policy_file")
    if values.get("yolo", False):
        policy = ShellPolicy.yolo()
    elif policy_file is not None:
        policy = ShellPolicy.from_file(policy_file)
    else:
        default_policy = (
            _POWERSHELL_POLICY_FILE if dialect.state_key() == "powershell" else _DEFAULT_POLICY_FILE
        )
        policy = ShellPolicy.from_file(str(default_policy))

    detached_binding = values.get("_detached_daemon_shell")
    detached = (
        detached_binding if isinstance(detached_binding, _DetachedDaemonShellBinding) else None
    )
    manager = ShellManager(
        policy=policy,
        working_dir=str(host.workdir.path),
        dialect=dialect,
        shell_kind=kind,
        notification_port=host.notifications,
        async_handoff=_DETACHED_DAEMON_ASYNC_HANDOFF if detached is not None else None,
        async_jobs_dir=detached.jobs_dir if detached is not None else None,
        retry_failed_publications=detached is not None,
        rehydrate=False,
    )
    dispatcher = ShellFamilyDispatcher(manager, host.workdir)
    description = get_description(
        dialect=dialect.state_key(), host_os=_describe_host_os(), shell_kind=kind,
    )
    policy_summary = policy.describe()
    if policy_summary:
        description = f"{description}\n\n{policy_summary}"
    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=dispatcher.handle,
        description=description,
        glossary_package=__package__,
        activate=manager.activate,
    )


#: Static official Shell declaration.  The action inventory, strict schemas,
#: installed manual destination, and required narrow ports are all set before an
#: Agent exists; composition reads them back rather than restating them.
DECLARATION = ToolPluginDeclaration(
    name="shell",
    actions=tuple(_DECLARED_INPUT_SCHEMAS),
    input_schemas=_DECLARED_INPUT_SCHEMAS,
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="shell",
    description=get_description(),
    binder=_bind,
    requires=("workdir", "notifications", "configuration"),
    glossary_package=__package__,
    settings=True,
)


def _schema_only_family() -> ToolFamily:
    return _build_family()


_FAMILY = _schema_only_family()


def get_schema(lang: str = "en") -> dict[str, Any]:
    """Compose Shell's sole public LTP-v2 schema from its declaration."""
    return _FAMILY.build_schema()
