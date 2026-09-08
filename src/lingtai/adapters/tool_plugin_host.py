"""Production adapters translating the live Agent body into host plugin ports.

`lingtai.kernel.tool_plugin` owns the Ports; this module is the one production
Adapter set that satisfies them for a running ``BaseAgent``, and it lives
outside the kernel package so the dependency still points inward
(``Adapter -> Port <- Core``, root ``CONTRACT.md`` rules 2-3).

Each adapter is constructed from one narrow callable — a bound method of the
agent, or a single-expression read closure — never from the agent object. That
is a real constraint on this file rather than a security boundary: an adapter
here cannot reach a second Agent API by accident, because it never holds the
Agent. Deep reachability through a bound method's ``__self__`` or a closure
cell is not prevented and is not claimed to be — the promise the contract makes
is about the declared argument surface handed to a plugin.

Which declarations get registered, and when, stays in the Composition Root
(``src/lingtai/agent.py`` and the capability ``setup()`` it drives). This module
only builds the ports.
"""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

from lingtai.kernel.llm.base import FunctionSchema
from lingtai.kernel.time_veil import now_iso as render_now_iso

if TYPE_CHECKING:
    from lingtai.kernel.tool_plugin import PsycheSettingsSnapshotPort
    from lingtai.tools.email import EmailResult, EmailRuntimeRequest

from lingtai.kernel import notifications
from lingtai.kernel.tool_plugin import (
    BoundToolPlugin,
    FileGrepMatch,
    FileTraversalStats,
    PluginCatalogState,
    ToolPluginDeclaration,
    register_official_tool_plugins,
)

__all__ = [
    "AgentWorkdirAdapter",
    "AgentPsycheSettingsAdapter",
    "AgentActiveProviderAdapter",
    "AgentProviderIdentityAdapter",
    "AgentPromptSectionAdapter",
    "AgentFileIOAdapter",
    "AgentContextRuntimeAdapter",
    "AgentAvatarParentAdapter",
    "AgentDaemonRuntimeAdapter",
    "AgentEmailRuntimeAdapter",
    "AgentPluginCatalogAdapter",
    "AgentNotificationStateAdapter",
    "AgentNotificationAdapter",
    "StaticConfigurationAdapter",
    "AgentSoulRuntimeAdapter",
    "agent_soul_runtime",
    "AgentSystemRuntimeAdapter",
    "AgentIdentityAdapter",
    "agent_system_runtime",
    "AgentShutdownAdapter",
    "AgentTaskCardLifecycleAdapter",
    "AgentTaskCardNotificationsAdapter",
    "agent_task_card_ports",
    "agent_host_ports",
    "daemon_runtime_for_agent",
    "register_agent_tool_plugins",
]


class AgentWorkdirAdapter:
    """:class:`~lingtai.kernel.tool_plugin.WorkdirPort` over ``Agent.working_dir``.

    Reads through on every access rather than snapshotting, so a plugin holding
    this port across a refresh never renders a stale directory.
    """

    __slots__ = ("_read",)

    def __init__(self, read: Callable[[], Path]) -> None:
        self._read = read

    @property
    def path(self) -> Path:
        return self._read()


class AgentPsycheSettingsAdapter:
    """``PsycheSettingsPort`` over the last applied reconstruction snapshot."""

    __slots__ = ("_read",)

    def __init__(
        self,
        read: Callable[[], "PsycheSettingsSnapshotPort"],
    ) -> None:
        self._read = read

    def read_snapshot(self) -> "PsycheSettingsSnapshotPort":
        """Return the current immutable Psyche owner-input snapshot."""
        return self._read()


class AgentActiveProviderAdapter:
    """``ActiveProviderPort`` over the Agent's current provider service.

    This is a read-only route to the one active service. It deliberately does
    not expose the Agent, its capability map, tool surface, or provider
    registry; Vision consumes exactly this active-provider identity to retain
    its direct-route semantics.
    """

    __slots__ = ("_read",)

    def __init__(self, read: Callable[[], Any]) -> None:
        self._read = read

    @property
    def service(self) -> Any:
        return self._read()


class AgentProviderIdentityAdapter:
    """``ProviderIdentityPort`` over the Agent's current provider *label*.

    Narrower than :class:`AgentActiveProviderAdapter`: it holds one read
    closure and exposes only a string (or ``None``). Web consumes exactly this
    label for its explicit Anthropic/Gemini eligibility gate; it never sees the
    provider service, credentials, model configuration, or the Agent. A
    non-string read is reported as ``None`` rather than coerced.
    """

    __slots__ = ("_read",)

    def __init__(self, read: Callable[[], Any]) -> None:
        self._read = read

    @property
    def provider(self) -> str | None:
        value = self._read()
        return value if isinstance(value, str) else None


class AgentPromptSectionAdapter:
    """:class:`~lingtai.kernel.tool_plugin.PromptSectionPort` for one section.

    Bound at construction to the declaring plugin's own section name and to
    ``protected=True``. The plugin passes only a body, so it can neither
    address another plugin's section nor downgrade its own to unprotected.
    """

    __slots__ = ("_section", "_write")

    def __init__(
        self,
        section: str,
        write: Callable[..., None],
    ) -> None:
        self._section = section
        self._write = write

    def write_protected_section(self, body: str) -> None:
        self._write(self._section, body, protected=True)


class AgentNotificationAdapter:
    """The narrow durable-notification port over one live Agent's store.

    Constructed from the canonical system-event method plus a store reader,
    rather than from an Agent object.  Its system-event fallback and latest
    channel publication deliberately preserve the pre-plugin Shell manager's
    compare-and-update semantics; the Shell family sees only these two typed
    operations and cannot reach any other Agent API.
    """

    __slots__ = ("_enqueue", "_store")

    def __init__(
        self,
        enqueue: Callable[..., Any],
        store: Callable[[], Any],
    ) -> None:
        self._enqueue = enqueue
        self._store = store

    def publish_system(
        self,
        *,
        source: str,
        ref_id: str,
        body: str,
        skip_if_ref_id_exists: bool = False,
    ) -> bool:
        try:
            self._enqueue(
                source=source,
                ref_id=ref_id,
                body=body,
                skip_if_ref_id_exists=skip_if_ref_id_exists,
            )
            # The historical Shell path considers a duplicate-suppressed event
            # a successful idempotent publication too.
            return True
        except Exception:
            pass
        try:
            import secrets
            import time
            from datetime import datetime, timezone

            from lingtai.kernel.notification_store import UNCONDITIONAL

            store = self._store()
            event_id = f"evt_{int(time.time()*1000):x}_{secrets.token_hex(8)}"
            received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            def mutate(current_payload: dict) -> tuple[dict | None, bool, str]:
                current = current_payload if isinstance(current_payload, dict) else {}
                events = list(current.get("data", {}).get("events", []))
                if skip_if_ref_id_exists and any(
                    isinstance(event, dict) and event.get("ref_id") == ref_id
                    for event in events
                ):
                    return current_payload, False, ""
                events.append({
                    "event_id": event_id,
                    "source": source,
                    "ref_id": ref_id,
                    "body": body,
                    "at": received_at,
                })
                events = events[-20:]
                return ({
                    "header": f"{len(events)} system notification{'s' if len(events) != 1 else ''}",
                    "icon": "🔔",
                    "priority": "normal",
                    "published_at": received_at,
                    "data": {"events": events},
                }, True, event_id)

            store.compare_update_channel("system", UNCONDITIONAL, mutate)
            return True
        except Exception:
            return False

    def publish_channel(
        self,
        channel: str,
        payload: Mapping[str, Any],
        *,
        ref_id: str,
    ) -> bool:
        try:
            store = self._store()
            if hasattr(store, "compare_update_channel"):
                from lingtai.kernel.notification_store import UNCONDITIONAL

                def mutate(current_payload: dict) -> tuple[dict | None, bool, bool]:
                    current = current_payload if isinstance(current_payload, dict) else {}
                    data = current.get("data")
                    if isinstance(data, dict) and data.get("ref_id") == ref_id:
                        return current_payload, False, True
                    return dict(payload), True, True

                result = store.compare_update_channel(channel, UNCONDITIONAL, mutate)
                return bool(result.value)
            store.publish(channel, dict(payload))
            return True
        except Exception:
            return False


class StaticConfigurationAdapter:
    """Immutable, setup-selected values for one declared plugin binding."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = MappingProxyType(dict(values or {}))

    @property
    def values(self) -> Mapping[str, Any]:
        return self._values


class _FileGlobOperation(Protocol):
    def __call__(self, pattern: str, root: str | None = None) -> list[str]: ...


class _FileGrepOperation(Protocol):
    def __call__(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
        *,
        glob_filter: str | None = None,
    ) -> list[FileGrepMatch]: ...


class AgentFileIOAdapter:
    """``FileIOPort`` assembled from only File's consumed host callables.

    The adapter owns no Agent, has no generic forwarding or dispatch operation,
    and never publishes the backing FileIOService. It receives individual
    service methods plus two read-only fact readers and forwards only the exact
    vocabulary the declared ``file`` family consumes. Workdir remains a separate
    port, and model-facing mounting remains registrar-only.
    """

    __slots__ = (
        "_read",
        "_write",
        "_glob",
        "_grep",
        "_last_traversal",
        "_max_result_chars",
    )

    def __init__(
        self,
        *,
        read: Callable[[str], str],
        write: Callable[[str, str], None],
        glob: _FileGlobOperation,
        grep: _FileGrepOperation,
        last_traversal: Callable[[], FileTraversalStats | None],
        max_result_chars: Callable[[], int | None],
    ) -> None:
        self._read = read
        self._write = write
        self._glob = glob
        self._grep = grep
        self._last_traversal = last_traversal
        self._max_result_chars = max_result_chars

    def read(self, path: str) -> str:
        return self._read(path)

    def write(self, path: str, content: str) -> None:
        self._write(path, content)

    def glob(self, pattern: str, root: str | None = None) -> list[str]:
        return self._glob(pattern, root=root)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
        *,
        glob_filter: str | None = None,
    ) -> list[FileGrepMatch]:
        return self._grep(
            pattern,
            path=path,
            max_results=max_results,
            glob_filter=glob_filter,
        )

    @property
    def last_traversal(self) -> FileTraversalStats | None:
        return self._last_traversal()

    @property
    def max_result_chars(self) -> int | None:
        return self._max_result_chars()


class AgentNotificationStateAdapter:
    """Bind Notification Core's real agent-scoped operations to one narrow port.

    The adapter retains callbacks only. It never exposes the Agent, Store,
    notification fingerprints, or producer state to a plugin. Each callback
    still enters the existing Core function with the live Agent bound by the
    composition root, so producer guards, stale-delivery checks,
    acknowledgement, timers, hook manifests, and Store semantics remain in
    :mod:`lingtai.kernel.notifications`.
    """

    __slots__ = (
        "_dismiss",
        "_delay",
        "_add",
        "_drop",
        "_edit",
        "_list",
        "_read_settings",
        "_log",
    )

    def __init__(
        self,
        *,
        dismiss: Callable[..., dict[str, Any]],
        delay: Callable[[str, int], dict[str, Any]],
        add_hook: Callable[[dict[str, Any]], dict[str, Any]],
        drop_hook: Callable[[str], dict[str, Any]],
        edit_hook: Callable[[str, dict[str, Any]], dict[str, Any]],
        list_hooks: Callable[[], list[dict[str, Any]] | dict[str, Any]],
        read_settings: Callable[[], tuple[int, int]],
        log: Callable[..., None],
    ) -> None:
        self._dismiss = dismiss
        self._delay = delay
        self._add = add_hook
        self._drop = drop_hook
        self._edit = edit_hook
        self._list = list_hooks
        self._read_settings = read_settings
        self._log = log

    def dismiss(
        self,
        channel: str,
        *,
        force: bool,
        reason: str | None,
        event_id: str | None = None,
        ref_id: str | None = None,
    ) -> dict[str, Any]:
        return self._dismiss(
            channel,
            force=force,
            reason=reason,
            event_id=event_id,
            ref_id=ref_id,
        )

    def delay(self, channel: str, seconds: int) -> dict[str, Any]:
        return self._delay(channel, seconds)

    def add_hook(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return self._add(manifest)

    def drop_hook(self, name: str) -> dict[str, Any]:
        return self._drop(name)

    def edit_hook(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._edit(name, fields)

    def list_hooks(self) -> list[dict[str, Any]] | dict[str, Any]:
        return self._list()

    def read_settings(self) -> tuple[int, int]:
        return self._read_settings()

    def log(self, event_type: str, **fields: Any) -> None:
        self._log(event_type, **fields)


class AgentContextRuntimeAdapter:
    """``ContextRuntimePort`` over three bound Context operations.

    It stores only its narrow callbacks, never the Agent. The Context
    composition root supplies callbacks that retain the established live molt,
    summary, and reconstruction engines; the declared family receives only these
    three capability-native methods.
    """

    __slots__ = ("_molt", "_summarize", "_rebuild")

    def __init__(
        self,
        *,
        molt: Callable[[dict], dict],
        summarize: Callable[[dict], dict],
        rebuild: Callable[[dict], dict],
    ) -> None:
        self._molt = molt
        self._summarize = summarize
        self._rebuild = rebuild

    def molt(self, args: dict) -> dict:
        return self._molt(args)

    def summarize(self, args: dict) -> dict:
        return self._summarize(args)

    def rebuild(self, args: dict) -> dict:
        return self._rebuild(args)


class AgentAvatarParentAdapter:
    """Avatar's narrow parent-context port over the live Agent.

    The adapter exposes only the two current-Agent facts Avatar already uses:
    parent identity for the first prompt and an optional venv inheritance
    value.  It owns no Agent object; each value is read through its one
    narrow closure when Avatar asks for it.
    """

    __slots__ = (
        "_parent_name",
        "_venv_path",
        "_authorize_derived_launch",
    )

    def __init__(
        self,
        parent_name: Callable[[], str],
        venv_path: Callable[[], str | None],
        authorize_derived_launch: Callable[[Any], Any],
    ) -> None:
        self._parent_name = parent_name
        self._venv_path = venv_path
        self._authorize_derived_launch = authorize_derived_launch

    @property
    def parent_name(self) -> str:
        return self._parent_name()

    @property
    def venv_path(self) -> str | None:
        return self._venv_path()

    def authorize_derived_launch(self, capability: Any) -> Any:
        return self._authorize_derived_launch(capability)



class AgentEmailRuntimeAdapter:
    """Email's narrow live-manager and applied-settings read port.

    The adapter owns no Agent and never dispatches through an intrinsic or an
    official tool handler.  It validates the Email-owned action set before it
    reads the current manager, then invokes that manager exactly once with the
    legacy flat payload it already owns.  The read callback is deliberately
    evaluated per request so refresh or reconstruction can replace the manager
    without leaving an already-bound declared family stale.
    """

    __slots__ = ("_read_manager", "_read_pseudo_agent_subscriptions")

    def __init__(
        self,
        read_manager: Callable[[], Any],
        read_pseudo_agent_subscriptions: Callable[[], Any] | None = None,
    ) -> None:
        self._read_manager = read_manager
        self._read_pseudo_agent_subscriptions = read_pseudo_agent_subscriptions

    def handle_email(self, request: "EmailRuntimeRequest") -> "EmailResult":
        # Keep the action source of truth in Email's static declaration without
        # adding a family import edge at host-module import time.
        from lingtai.tools.email import DECLARATION as EMAIL_DECLARATION

        if request.action not in EMAIL_DECLARATION.actions:
            raise ValueError(f"unsupported Email runtime action: {request.action!r}")
        manager = self._read_manager()
        if manager is None:
            return {"error": "Internal: email manager not initialized. boot() was not called."}
        return manager.handle({"action": request.action, **dict(request.input)})

    def read_pseudo_agent_subscriptions(self) -> tuple[str, ...]:
        """Read the mail adapter's effective, construction-time path snapshot."""
        read = self._read_pseudo_agent_subscriptions
        if read is None:
            raise RuntimeError("Email pseudo-agent subscription snapshot is unavailable")
        value = read()
        if not isinstance(value, tuple) or not all(
            isinstance(item, str) for item in value
        ):
            raise RuntimeError("Email pseudo-agent subscription snapshot is invalid")
        return value


class _DaemonPresetToolCollector:
    """Host-private sandbox used by one daemon preset capability setup.

    It is created only inside the adapter's one ``setup_preset_capability``
    operation.  Daemon receives the resulting schema/handler dictionaries, not
    this collector and not the Agent it forwards to for established capability
    setup compatibility.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self.schemas: dict[str, FunctionSchema] = {}
        self.handlers: dict[str, Callable[[dict], dict]] = {}
        self._official_tool_plugins: dict[str, Any] = {}
        self._official_tool_declarations: dict[str, Any] = {}
        self._official_tool_bindings: dict[str, Any] = {}

    @property
    def official_tool_plugins(self):
        return MappingProxyType(self._official_tool_plugins)

    def _authorize_official_tool_declaration(self, declaration) -> None:
        from lingtai.kernel.base_agent import BaseAgent

        BaseAgent._authorize_official_tool_declaration(self, declaration)

    def _record_official_tool_binding(self, declaration, plugin) -> None:
        from lingtai.kernel.base_agent import BaseAgent

        BaseAgent._record_official_tool_binding(self, declaration, plugin)

    def _claim_official_tool(self, transaction) -> None:
        from lingtai.kernel.base_agent import BaseAgent

        BaseAgent._claim_official_tool(self, transaction)

    def _mount_official_tool(self, transaction) -> None:
        from lingtai.kernel.tool_plugin import (
            OFFICIAL_TOOL_PLUGIN_NAMES,
            _OfficialMountTransaction,
        )

        if not isinstance(transaction, _OfficialMountTransaction):
            raise PermissionError(
                "official tool mounting requires a registrar transaction"
            )
        declaration = transaction.declaration
        plugin = transaction.plugin
        name = declaration.name
        if (
            name not in OFFICIAL_TOOL_PLUGIN_NAMES
            or plugin.name != name
            or self._official_tool_declarations.get(name) is not declaration
            or self._official_tool_bindings.get(name) is not plugin
        ):
            raise PermissionError(
                "official mount transaction is not the canonical declaration/bind result"
            )
        live = self._official_tool_plugins.get(name)
        if live is not None and live is not declaration:
            raise PermissionError("official mount transaction is not for the live claim")
        transaction.consume()
        self.add_tool(
            name,
            schema=dict(plugin.schema),
            handler=plugin.handler,
            description=plugin.description,
            glossary_package=plugin.glossary_package,
        )
        transaction.mark_mounted(self)

    def add_tool(
        self,
        name: str,
        *,
        schema: dict | None = None,
        handler: Callable[[dict], dict] | None = None,
        description: str = "",
        system_prompt: str = "",
        glossary_package: str | None = None,
    ) -> None:
        if handler is not None:
            self.handlers[name] = handler
        if schema is not None:
            self.schemas[name] = FunctionSchema(
                name=name,
                description=description,
                parameters=schema,
                system_prompt=system_prompt,
                glossary_package=glossary_package,
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


class AgentDaemonRuntimeAdapter:
    """``DaemonRuntimePort`` assembled from narrow Agent operation closures.

    The port is intentionally Daemon-specific: it exposes exactly the parent
    facts Daemon's existing manager consumes (model/service, regular tool
    surface, preset sandbox/load operations, notifications, logging, and
    resolved manager construction options).  It has no generic attribute
    forwarding and no model-facing mount operation.
    """

    __slots__ = (
        "_read_service",
        "_read_schemas",
        "_read_handlers",
        "_read_mcp_names",
        "_read_language",
        "_read_max_aed_attempts",
        "_read_tool_call_guard",
        "_requires_derived_launch_admission",
        "_authorize_derived_launch",
        "_manager_options",
        "_setup_preset_capability",
        "_requires_explicit_preset",
        "_is_preset_authorized",
        "_load_preset",
        "_enqueue_notification",
        "_read_task_card_watch",
        "_now_iso",
        "_log",
        "_manager",
    )

    def __init__(
        self,
        *,
        read_service: Callable[[], Any],
        read_schemas: Callable[[], tuple[Any, ...]],
        read_handlers: Callable[[], Mapping[str, Callable[[dict], dict]]],
        read_mcp_names: Callable[[], frozenset[str]],
        read_language: Callable[[], str],
        read_max_aed_attempts: Callable[[], int],
        read_tool_call_guard: Callable[[], Any],
        requires_derived_launch_admission: Callable[[], bool],
        authorize_derived_launch: Callable[[Any], Any],
        manager_options: Mapping[str, Any],
        setup_preset_capability: Callable[[str, Mapping[str, Any]], tuple[dict[str, Any], dict[str, Callable[[dict], dict]]]],
        requires_explicit_preset: Callable[[], bool],
        is_preset_authorized: Callable[[str, Any], bool],
        load_preset: Callable[[str], dict],
        enqueue_notification: Callable[..., None],
        read_task_card_watch: Callable[[], bool | None],
        now_iso: Callable[[], str],
        log: Callable[..., None],
    ) -> None:
        self._read_service = read_service
        self._read_schemas = read_schemas
        self._read_handlers = read_handlers
        self._read_mcp_names = read_mcp_names
        self._read_language = read_language
        self._read_max_aed_attempts = read_max_aed_attempts
        self._read_tool_call_guard = read_tool_call_guard
        self._requires_derived_launch_admission = requires_derived_launch_admission
        self._authorize_derived_launch = authorize_derived_launch
        self._manager_options = dict(manager_options)
        self._setup_preset_capability = setup_preset_capability
        self._requires_explicit_preset = requires_explicit_preset
        self._is_preset_authorized = is_preset_authorized
        self._load_preset = load_preset
        self._enqueue_notification = enqueue_notification
        self._read_task_card_watch = read_task_card_watch
        self._now_iso = now_iso
        self._log = log
        self._manager: Any = None

    @property
    def service(self) -> Any:
        return self._read_service()

    @property
    def tool_schemas(self) -> tuple[Any, ...]:
        return self._read_schemas()

    @property
    def tool_handlers(self) -> Mapping[str, Callable[[dict], dict]]:
        return self._read_handlers()

    @property
    def mcp_tool_names(self) -> frozenset[str]:
        return self._read_mcp_names()

    @property
    def language(self) -> str:
        return self._read_language()

    @property
    def max_aed_attempts(self) -> int:
        return self._read_max_aed_attempts()

    @property
    def tool_call_guard(self) -> Any:
        return self._read_tool_call_guard()

    @property
    def requires_derived_launch_admission(self) -> bool:
        return self._requires_derived_launch_admission()

    def authorize_derived_launch(self, capability: Any) -> Any:
        return self._authorize_derived_launch(capability)

    @property
    def manager_options(self) -> Mapping[str, Any]:
        return dict(self._manager_options)

    def setup_preset_capability(
        self, name: str, kwargs: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Callable[[dict], dict]]]:
        return self._setup_preset_capability(name, kwargs)

    @property
    def requires_explicit_preset(self) -> bool:
        return self._requires_explicit_preset()

    def is_preset_authorized(self, name: str, working_dir: Any) -> bool:
        return self._is_preset_authorized(name, working_dir)

    def load_preset(self, name: str) -> dict:
        return self._load_preset(name)

    def enqueue_daemon_notification(
        self,
        *,
        source: str,
        ref_id: str,
        body: str,
        idempotency_key: str | None,
        skip_if_idempotency_key_exists: bool,
        extra: Mapping[str, Any],
        channel: str,
    ) -> None:
        self._enqueue_notification(
            source=source,
            ref_id=ref_id,
            body=body,
            idempotency_key=idempotency_key,
            skip_if_idempotency_key_exists=skip_if_idempotency_key_exists,
            extra=dict(extra),
            channel=channel,
        )

    def has_active_task_card_watch(self) -> bool | None:
        return self._read_task_card_watch()

    def attach_daemon_manager(self, manager: Any) -> None:
        self._manager = manager

    def now_iso(self) -> str:
        return self._now_iso()

    @property
    def daemon_manager(self) -> Any:
        return self._manager

    def log(self, event_type: str, **fields: Any) -> None:
        self._log(event_type, **fields)


def daemon_runtime_for_agent(
    agent: Any, manager_options: Mapping[str, Any]
) -> AgentDaemonRuntimeAdapter:
    """Build Daemon's adapter from the current Agent's narrow operations."""

    def _setup_preset_capability(
        name: str, kwargs: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Callable[[dict], dict]]]:
        from lingtai.tools.registry import setup_capability

        collector = _DaemonPresetToolCollector(agent)
        setup_capability(collector, name, **dict(kwargs))
        return collector.schemas, collector.handlers

    def _read_language() -> str:
        value = getattr(getattr(agent, "_config", None), "language", "en")
        return value if isinstance(value, str) else "en"

    def _read_max_aed_attempts() -> int:
        value = getattr(getattr(agent, "_config", None), "max_aed_attempts", 3)
        return value if isinstance(value, int) and not isinstance(value, bool) else 3

    def _is_preset_authorized(name: str, working_dir: Any) -> bool:
        from lingtai.kernel.presets import _preset_ref_in

        read = getattr(agent, "_read_preset_from_init", None)
        if not callable(read):
            return False
        try:
            value = read()
        except Exception:
            return False
        allowed = value.get("allowed") if isinstance(value, Mapping) else None
        return _preset_ref_in(name, allowed, working_dir=working_dir)

    def _read_task_card_watch() -> bool | None:
        check = getattr(getattr(agent, "_task_card_manager", None), "has_active_watch", None)
        if not callable(check):
            return None
        try:
            active = check()
        except Exception:
            return None
        return active if isinstance(active, bool) else None

    def _authorize_derived_launch(capability: Any) -> Any:
        from lingtai.kernel.provider_admission import require_derived_launch_admission

        return require_derived_launch_admission(
            getattr(agent, "_derived_launch_admission_port", None),
            capability,
            required=bool(
                getattr(agent, "_requires_derived_launch_admission_port", False)
            ),
        )

    def _log(event_type: str, **fields: Any) -> None:
        log = getattr(agent, "_log", None)
        if callable(log):
            log(event_type, **fields)

    def _missing_load_preset(name: str) -> dict:
        raise KeyError(f"preset loading is unavailable for {name!r} on this daemon host")

    def _missing_notification(**_kwargs: Any) -> None:
        raise RuntimeError("daemon notifications are unavailable on this daemon host")

    def _enqueue_notification_live(**kwargs: Any) -> None:
        """Invoke the host's current notification route at publish time.

        Refresh/reconstruction may replace the route after this Daemon runtime
        port is bound. Looking it up here makes a replaced failing route report
        publication failure, so terminal receipt state remains retryable rather
        than acknowledging a stale callback's earlier success.
        """
        notify = getattr(agent, "_enqueue_system_notification", None)
        if not callable(notify):
            _missing_notification(**kwargs)
        notify(**kwargs)

    # Daemon's accepted email route is its task-scoped daemon_email MCP server,
    # explicitly requested per emanation.  Email's parent official declaration
    # must not turn that into inherited parent communication authority merely by
    # appearing in the regular tool surface; keep the pre-existing Daemon filter
    # at this composition boundary for both schema and dispatch views.
    def _read_daemon_schemas() -> tuple[Any, ...]:
        return tuple(
            schema
            for schema in getattr(agent, "_tool_schemas", ())
            if getattr(schema, "name", None) != "email"
        )

    def _read_daemon_handlers() -> Mapping[str, Callable[[dict], dict]]:
        return {
            name: handler
            for name, handler in dict(getattr(agent, "_tool_handlers", {})).items()
            if name != "email"
        }

    return AgentDaemonRuntimeAdapter(
        read_service=lambda: agent.service,
        read_schemas=_read_daemon_schemas,
        read_handlers=_read_daemon_handlers,
        read_mcp_names=lambda: frozenset(
            name for name in getattr(agent, "_mcp_tool_names", set()) if isinstance(name, str)
        ),
        read_language=_read_language,
        read_max_aed_attempts=_read_max_aed_attempts,
        read_tool_call_guard=lambda: getattr(agent, "_tool_call_guard", None),
        requires_derived_launch_admission=lambda: bool(
            getattr(agent, "_requires_derived_launch_admission_port", False)
        ),
        authorize_derived_launch=_authorize_derived_launch,
        manager_options=manager_options,
        setup_preset_capability=_setup_preset_capability,
        requires_explicit_preset=lambda: False,
        is_preset_authorized=_is_preset_authorized,
        load_preset=getattr(agent, "load_preset", _missing_load_preset),
        enqueue_notification=_enqueue_notification_live,
        read_task_card_watch=_read_task_card_watch,
        now_iso=lambda: render_now_iso(agent),
        log=_log,
    )


class AgentPluginCatalogAdapter:
    """Read-only :class:`PluginCatalogPort` over the current Agent state.

    It holds only two narrow readers rather than an ``Agent``.  Each read creates
    a detached value projection: mutating a tool result cannot alter the Agent's
    registration snapshot or capability configuration, and the adapter grants no
    registration, prune, launch, or prompt operation.
    """

    __slots__ = ("_read_registration", "_read_capabilities")

    def __init__(
        self,
        read_registration: Callable[[], Any],
        read_capabilities: Callable[[], Any],
    ) -> None:
        self._read_registration = read_registration
        self._read_capabilities = read_capabilities

    def read_state(self) -> PluginCatalogState:
        registration = self._read_registration()
        snapshot = deepcopy(dict(registration)) if isinstance(registration, Mapping) else {}

        configured_paths: tuple[str, ...] = ()
        skill_paths: tuple[str, ...] = ()
        skills_enabled = False
        capabilities = self._read_capabilities()
        if isinstance(capabilities, (list, tuple)):
            for item in capabilities:
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                name, kwargs = item
                if name == "skills":
                    skills_enabled = True
                    if isinstance(kwargs, Mapping):
                        raw_paths = kwargs.get("paths", [])
                        if isinstance(raw_paths, (list, tuple)):
                            skill_paths = tuple(
                                path for path in raw_paths if isinstance(path, str)
                            )
                elif name == "plugin" and isinstance(kwargs, Mapping):
                    raw_paths = kwargs.get("paths", [])
                    if isinstance(raw_paths, (list, tuple)):
                        configured_paths = tuple(
                            path for path in raw_paths if isinstance(path, str)
                        )
        return PluginCatalogState(
            registration=snapshot,
            configured_paths=configured_paths,
            skill_paths=skill_paths,
            skills_enabled=skills_enabled,
        )


class AgentSoulRuntimeAdapter:
    """``SoulRuntimePort`` over the exact live-self operations Soul consumes.

    The adapter stores individual getters, setters, and bound operations rather
    than an Agent. Its explicit surface covers Soul's real conversation,
    cadence, lock, and notification semantics without granting an unrelated
    tool, mount, or generic Agent API.
    """

    __slots__ = (
        "_working_dir", "_config", "_service", "_chat", "_session",
        "_agent_name", "_state", "_idle_event", "_shutdown", "_soul_delay",
        "_set_soul_delay", "_soul_timer", "_set_soul_timer", "_fire_lock",
        "_notification_store", "_notification_fingerprint", "_appendix_ids",
        "_log", "_restart_soul_timer", "_run_consultation_fire",
        "_sync_notifications", "_wake_nap", "_persist_soul_entry",
        "_append_soul_flow_record", "_publish_notification",
        "_clear_notification", "_dismiss_notification",
    )

    def __init__(
        self,
        *,
        working_dir: Callable[[], Path],
        config: Callable[[], Any],
        service: Callable[[], Any],
        chat: Callable[[], Any],
        session: Callable[[], Any],
        agent_name: Callable[[], str],
        state: Callable[[], Any],
        idle_event: Callable[[], Any],
        shutdown: Callable[[], Any],
        soul_delay: Callable[[], float],
        set_soul_delay: Callable[[float], None],
        soul_timer: Callable[[], Any],
        set_soul_timer: Callable[[Any], None],
        fire_lock: Callable[[], Any],
        notification_store: Callable[[], Any],
        notification_fingerprint: Callable[[], Any],
        appendix_ids: Callable[[], dict[str, str]],
        log: Callable[..., None],
        restart_soul_timer: Callable[[], None],
        run_consultation_fire: Callable[[], None],
        sync_notifications: Callable[[], None],
        wake_nap: Callable[[str], None],
        persist_soul_entry: Callable[..., None],
        append_soul_flow_record: Callable[[dict], None],
        publish_notification: Callable[..., None],
        clear_notification: Callable[[str], None],
        dismiss_notification: Callable[..., dict],
    ) -> None:
        self._working_dir = working_dir
        self._config = config
        self._service = service
        self._chat = chat
        self._session = session
        self._agent_name = agent_name
        self._state = state
        self._idle_event = idle_event
        self._shutdown = shutdown
        self._soul_delay = soul_delay
        self._set_soul_delay = set_soul_delay
        self._soul_timer = soul_timer
        self._set_soul_timer = set_soul_timer
        self._fire_lock = fire_lock
        self._notification_store = notification_store
        self._notification_fingerprint = notification_fingerprint
        self._appendix_ids = appendix_ids
        self._log = log
        self._restart_soul_timer = restart_soul_timer
        self._run_consultation_fire = run_consultation_fire
        self._sync_notifications = sync_notifications
        self._wake_nap = wake_nap
        self._persist_soul_entry = persist_soul_entry
        self._append_soul_flow_record = append_soul_flow_record
        self._publish_notification = publish_notification
        self._clear_notification = clear_notification
        self._dismiss_notification = dismiss_notification

    @property
    def working_dir(self) -> Path:
        return self._working_dir()

    @property
    def config(self) -> Any:
        return self._config()

    @property
    def service(self) -> Any:
        return self._service()

    @property
    def chat(self) -> Any:
        return self._chat()

    @property
    def session(self) -> Any:
        return self._session()

    @property
    def agent_name(self) -> str:
        return self._agent_name()

    @property
    def state(self) -> Any:
        return self._state()

    @property
    def idle_event(self) -> Any:
        return self._idle_event()

    @property
    def shutdown(self) -> Any:
        return self._shutdown()

    @property
    def soul_delay(self) -> float:
        return self._soul_delay()

    @soul_delay.setter
    def soul_delay(self, value: float) -> None:
        self._set_soul_delay(value)

    @property
    def soul_timer(self) -> Any:
        return self._soul_timer()

    @soul_timer.setter
    def soul_timer(self, value: Any) -> None:
        self._set_soul_timer(value)

    @property
    def fire_lock(self) -> Any:
        return self._fire_lock()

    @property
    def notification_store(self) -> Any:
        return self._notification_store()

    @property
    def notification_fingerprint(self) -> Any:
        return self._notification_fingerprint()

    @property
    def appendix_ids_by_source(self) -> dict[str, str]:
        return self._appendix_ids()

    def log(self, event: str, **fields: Any) -> None:
        self._log(event, **fields)

    def restart_soul_timer(self) -> None:
        self._restart_soul_timer()

    def run_consultation_fire(self) -> None:
        self._run_consultation_fire()

    def sync_notifications(self) -> None:
        self._sync_notifications()

    def wake_nap(self, reason: str) -> None:
        self._wake_nap(reason)

    def persist_soul_entry(self, result: dict, mode: str = "flow", source: str = "agent") -> None:
        # Preserve the existing call shape for the default agent-source path;
        # source is an additive override used only by the /btw runner.
        if source == "agent":
            self._persist_soul_entry(result, mode=mode)
        else:
            self._persist_soul_entry(result, mode=mode, source=source)

    def append_soul_flow_record(self, record: dict) -> None:
        self._append_soul_flow_record(record)

    def publish_notification(self, channel: str, **kwargs: Any) -> None:
        self._publish_notification(channel, **kwargs)

    def clear_notification(self, channel: str) -> None:
        self._clear_notification(channel)

    def dismiss_notification(self, channel: str, *, invoked_by: str) -> dict:
        return self._dismiss_notification(channel, invoked_by=invoked_by)


def agent_soul_runtime(agent: Any) -> AgentSoulRuntimeAdapter:
    """Bind Soul's explicit runtime port to one live Agent.

    This is composition-only: each value is a single read closure or bound
    operation. The resulting adapter retains no Agent attribute, and callers
    can reach only its declared SoulRuntimePort vocabulary.
    """
    from lingtai.kernel.notifications import clear, dismiss_channel, submit

    return AgentSoulRuntimeAdapter(
        working_dir=lambda: agent.working_dir if isinstance(getattr(type(agent), "working_dir", None), property) else agent._working_dir,
        config=lambda: getattr(agent, "_config", None),
        service=lambda: getattr(agent, "service", None),
        chat=lambda: getattr(agent, "_chat", None),
        session=lambda: getattr(agent, "_session", None),
        agent_name=lambda: getattr(agent, "agent_name", ""),
        state=lambda: agent.state if isinstance(getattr(type(agent), "state", None), property) else getattr(agent, "_state", None),
        idle_event=lambda: getattr(agent, "_idle", None),
        shutdown=lambda: getattr(agent, "_shutdown", None),
        soul_delay=lambda: getattr(agent, "_soul_delay", 0.0),
        set_soul_delay=lambda value: setattr(agent, "_soul_delay", value),
        soul_timer=lambda: getattr(agent, "_soul_timer", None),
        set_soul_timer=lambda value: setattr(agent, "_soul_timer", value),
        fire_lock=lambda: getattr(agent, "_soul_fire_lock", None),
        notification_store=lambda: getattr(agent, "_notification_store", None),
        notification_fingerprint=lambda: getattr(agent, "_notification_fp", None),
        appendix_ids=lambda: getattr(agent, "_appendix_ids_by_source", {}),
        log=getattr(agent, "_log", lambda *_args, **_kwargs: None),
        restart_soul_timer=getattr(agent, "_start_soul_timer", lambda: None),
        run_consultation_fire=getattr(agent, "_run_consultation_fire", lambda: None),
        sync_notifications=getattr(agent, "_sync_notifications", lambda: None),
        wake_nap=getattr(agent, "_wake_nap", lambda _reason: None),
        persist_soul_entry=getattr(agent, "_persist_soul_entry", lambda *_args, **_kwargs: None),
        append_soul_flow_record=getattr(agent, "_append_soul_flow_record", lambda _record: None),
        publish_notification=lambda channel, **kwargs: submit(agent, channel, **kwargs),
        clear_notification=lambda channel: clear(agent, channel),
        dismiss_notification=lambda channel, *, invoked_by: dismiss_channel(
            agent, channel, invoked_by=invoked_by
        ),
    )


class AgentSystemRuntimeAdapter:
    """SystemRuntimePort composed from narrow Agent callbacks, never an Agent.

    Translation-only by contract (System's one sleep *policy* lives in
    ``lingtai.tools.system.karma.sleep_use_case``): the four sleep members
    below expose attention evidence, the ASLEEP transition, and the persisted
    one-shot alarm effect, and this adapter owns no fingerprint comparison,
    refusal/force branch, receipt, or audit decision of its own.
    """

    __slots__ = (
        "_admin", "_language", "_log", "_token_usage", "_load_preset",
        "_activate_preset", "_activate_default_preset", "_retry_failed_mcps",
        "_perform_refresh", "_resuscitate", "_sleep_attention_fingerprints",
        "_transition_to_asleep", "_sleep_alarm_lock", "_arm_sleep_alarm",
    )

    def __init__(
        self,
        *,
        admin: Callable[[], Mapping[str, Any]],
        language: Callable[[], str],
        log: Callable[..., None],
        token_usage: Callable[[], Mapping[str, Any]],
        load_preset: Callable[[str], dict],
        activate_preset: Callable[[str], None],
        activate_default_preset: Callable[[], None],
        retry_failed_mcps: Callable[[], Mapping[str, Any]],
        perform_refresh: Callable[[], None],
        resuscitate: Callable[[str], Any],
        sleep_attention_fingerprints: Callable[[], tuple[tuple, tuple]],
        transition_to_asleep: Callable[[], None],
        sleep_alarm_lock: Callable[[], Any],
        arm_sleep_alarm: Callable[[Any], str],
    ) -> None:
        self._admin = admin
        self._language = language
        self._log = log
        self._token_usage = token_usage
        self._load_preset = load_preset
        self._activate_preset = activate_preset
        self._activate_default_preset = activate_default_preset
        self._retry_failed_mcps = retry_failed_mcps
        self._perform_refresh = perform_refresh
        self._resuscitate = resuscitate
        self._sleep_attention_fingerprints = sleep_attention_fingerprints
        self._transition_to_asleep = transition_to_asleep
        self._sleep_alarm_lock = sleep_alarm_lock
        self._arm_sleep_alarm = arm_sleep_alarm

    @property
    def admin(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self._admin() or {}))

    @property
    def language(self) -> str:
        return self._language()

    def log(self, event: str, **fields: Any) -> None:
        self._log(event, **fields)

    def token_usage(self) -> Mapping[str, Any]:
        return self._token_usage()

    def load_preset(self, name: str) -> dict:
        return self._load_preset(name)

    def activate_preset(self, name: str) -> None:
        self._activate_preset(name)

    def activate_default_preset(self) -> None:
        self._activate_default_preset()

    def retry_failed_mcps(self) -> Mapping[str, Any]:
        return self._retry_failed_mcps()

    def perform_refresh(self) -> None:
        self._perform_refresh()

    def resuscitate(self, address: str) -> Any:
        return self._resuscitate(address)

    def sleep_attention_fingerprints(self) -> tuple[tuple, tuple]:
        return self._sleep_attention_fingerprints()

    def transition_to_asleep(self) -> None:
        self._transition_to_asleep()

    def sleep_alarm_lock(self) -> Any:
        return self._sleep_alarm_lock()

    def arm_sleep_alarm(self, delay_seconds: Any) -> str:
        return self._arm_sleep_alarm(delay_seconds)


class AgentIdentityAdapter:
    """IdentityPort over exactly the System naming surface."""

    __slots__ = ("_name", "_set_name", "_set_nickname")

    def __init__(
        self,
        name: Callable[[], str | None],
        set_name: Callable[[str], None],
        set_nickname: Callable[[str], None],
    ) -> None:
        self._name = name
        self._set_name = set_name
        self._set_nickname = set_nickname

    @property
    def name(self) -> str | None:
        return self._name()

    def set_name(self, name: str) -> None:
        self._set_name(name)

    def set_nickname(self, nickname: str) -> None:
        self._set_nickname(nickname)


def agent_system_runtime(agent: Any) -> AgentSystemRuntimeAdapter:
    """Bind System's explicit runtime port to one live Agent.

    Composition-only: each value is one deferred read closure or bound
    operation, so an agent that never invokes a given System action never
    needs the corresponding attribute.  The sleep evidence/effect closures
    reuse the kernel's coherent attention reader and persisted sleep-alarm
    helpers verbatim — no second sleep decision tree is created here.
    """
    from lingtai.kernel.base_agent.lifecycle import (
        _arm_sleep_alarm,
        _sleep_alarm_lock,
    )
    from lingtai.kernel.notifications import (
        _workdir_key,
        attention_fingerprint,
        is_channel_allowed,
    )
    from lingtai.kernel.state import AgentState

    def _sleep_attention_fingerprints() -> tuple[tuple, tuple]:
        workdir = _workdir_key(agent)
        pending = attention_fingerprint(
            agent._notification_store,
            lambda channel: is_channel_allowed(channel, workdir=workdir),
            workdir,
        )
        return pending, tuple(agent._notification_fp or ())

    def _transition_to_asleep() -> None:
        agent._set_state(AgentState.ASLEEP, reason="self-sleep")
        agent._asleep.set()
        agent._request_turn_cancel()

    return AgentSystemRuntimeAdapter(
        admin=lambda: getattr(agent, "_admin", {}) or {},
        language=lambda: agent._config.language,
        log=lambda event, **fields: agent._log(event, **fields),
        token_usage=lambda: agent.get_token_usage(),
        load_preset=lambda name: agent.load_preset(name),
        activate_preset=lambda name: agent._activate_preset(name),
        activate_default_preset=lambda: agent._activate_default_preset(),
        retry_failed_mcps=lambda: getattr(agent, "_retry_failed_mcps", lambda: {})(),
        perform_refresh=lambda: agent._perform_refresh(),
        resuscitate=lambda address: agent._cpr_agent(address),
        sleep_attention_fingerprints=_sleep_attention_fingerprints,
        transition_to_asleep=_transition_to_asleep,
        sleep_alarm_lock=lambda: _sleep_alarm_lock(agent),
        arm_sleep_alarm=lambda delay_seconds: _arm_sleep_alarm(agent, delay_seconds),
    )


class AgentShutdownAdapter:
    """One-predicate ``ShutdownPort`` over the Agent shutdown event."""

    __slots__ = ("_is_set",)

    def __init__(self, is_set: Callable[[], bool]) -> None:
        self._is_set = is_set

    def is_set(self) -> bool:
        return self._is_set()


class AgentTaskCardLifecycleAdapter:
    """The one current-Agent manager slot Task Card needs across refreshes."""

    __slots__ = ("_current", "_retain", "_report")

    def __init__(
        self,
        current: Callable[[], Any | None],
        retain: Callable[[Any], None],
        report: Callable[[str], None],
    ) -> None:
        self._current = current
        self._retain = retain
        self._report = report

    def current_manager(self) -> Any | None:
        return self._current()

    def retain_manager(self, manager: Any) -> None:
        self._retain(manager)

    def report_resume_failure(self, error: str) -> None:
        self._report(error)


class AgentTaskCardNotificationsAdapter:
    """``TaskCardNotificationsPort`` over the Agent's system-event publisher.

    Five closed operations only.  The Agent's generic publisher is held as a
    private callable and is never exposed: every wire field other than the
    producer's body, identity, and bounded facts — ``source``, ``channel``,
    priority, idempotency skip, and the ``extra`` projection — is pinned here
    to the Task Card producer's established forms.  A holder therefore cannot
    publish a foreign source, address another channel, or smuggle generic
    notification fields through this port.
    """

    __slots__ = ("_enqueue", "_submit", "_clear")

    #: The established error stream; ``recovered`` is a state on it.
    _ERROR_SOURCE = "task_card.error"
    _LIMIT_SOURCE = "task_card.limit"
    _CHANNEL = "system"

    def __init__(
        self,
        enqueue: Callable[..., Any],
        submit: Callable[[int], None],
        clear: Callable[[], None],
    ) -> None:
        self._enqueue = enqueue
        self._submit = submit
        self._clear = clear

    def publish_error(
        self,
        watch_id: str,
        body: str,
        code: str,
        retryable: bool | str,
        idempotency_key: str,
        last_valid_body_at: str | None = None,
    ) -> None:
        extra: dict[str, Any] = {
            "watch_id": watch_id,
            "state": "error",
            "code": code,
            "retryable": retryable,
        }
        if last_valid_body_at:
            extra["last_valid_body_at"] = last_valid_body_at
        self._enqueue(
            source=self._ERROR_SOURCE,
            channel=self._CHANNEL,
            ref_id=watch_id,
            body=body,
            idempotency_key=idempotency_key,
            skip_if_idempotency_key_exists=True,
            priority="high",
            extra=extra,
        )

    def publish_recovered(self, watch_id: str, body: str, idempotency_key: str) -> None:
        self._enqueue(
            source=self._ERROR_SOURCE,
            channel=self._CHANNEL,
            ref_id=watch_id,
            body=body,
            idempotency_key=idempotency_key,
            skip_if_idempotency_key_exists=True,
            priority="normal",
            extra={"watch_id": watch_id, "state": "recovered"},
        )

    def publish_limit(
        self,
        watch_id: str,
        body: str,
        idempotency_key: str,
        used: int,
        max_refreshes: int,
        last_valid_body_at: str | None = None,
    ) -> None:
        extra: dict[str, Any] = {
            "watch_id": watch_id,
            "state": "stopped",
            "reason": "max_refreshes",
            "used": used,
            "max": max_refreshes,
        }
        if last_valid_body_at:
            extra["last_valid_body_at"] = last_valid_body_at
        self._enqueue(
            source=self._LIMIT_SOURCE,
            channel=self._CHANNEL,
            ref_id=watch_id,
            body=body,
            idempotency_key=idempotency_key,
            skip_if_idempotency_key_exists=True,
            priority="normal",
            extra=extra,
        )

    def submit_reminder(self, turns: int) -> None:
        self._submit(turns)

    def clear_reminder(self) -> None:
        self._clear()


def agent_task_card_ports(agent: Any) -> dict[str, Any]:
    """Bind Task Card's three earned ports to one live Agent.

    Composition-only: the lifecycle closures read/replace the Agent's retained
    ``_task_card_manager`` slot (the same object ``BaseAgent`` lifecycle stops
    and the Daemon runtime's ``has_active_task_card_watch`` probes), the
    shutdown predicate is the Agent's own event, and the notification adapter
    holds the Agent's system-event publisher privately behind its five closed
    operations.  Nothing here hands the family the Agent.
    """

    def _retain_task_card_manager(manager: Any) -> None:
        agent._task_card_manager = manager

    def _report_task_card_resume_failure(error: str) -> None:
        log = getattr(agent, "_log", None)
        if callable(log):
            log("task_card_resume_failed", error=error)

    def _submit_task_card_reminder(turns: int) -> None:
        notifications.submit(
            agent,
            "task_card",
            data={"source": "task_card.reminder", "turns": turns},
            header="Task Card reminder",
            instructions=(
                "Check whether the Task Card is absent or stale; update or issue one only if useful."
            ),
        )

    def _clear_task_card_reminder() -> None:
        notifications.clear(agent, "task_card")

    return {
        "shutdown": AgentShutdownAdapter(agent._shutdown.is_set),
        "task_card_lifecycle": AgentTaskCardLifecycleAdapter(
            lambda: getattr(agent, "_task_card_manager", None),
            _retain_task_card_manager,
            _report_task_card_resume_failure,
        ),
        "task_card_notifications": AgentTaskCardNotificationsAdapter(
            agent._enqueue_system_notification,
            _submit_task_card_reminder,
            _clear_task_card_reminder,
        ),
    }


def agent_host_ports(
    agent: Any,
    plugin_name: str,
    extra_ports: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete grantable table for one declaration on *agent*.

    The table preserves the landed MCP, Avatar, Plugin, Psyche, Context, Daemon,
    Email, and File wiring while constructing only each declaration's earned adapter.
    Psyche receives only its read-through applied Pad settings snapshot;
    Notification receives its narrow state port at this composition boundary, and
    Shell receives its narrow durable-notification port here too; Shell's
    setup-selected ``configuration`` port arrives through ``extra_ports``.
    Soul receives its explicit live-self ``soul_runtime`` port here as well,
    and System receives its ``system_runtime`` lifecycle vocabulary plus the
    durable naming ``identity`` port. Task Card receives its ``shutdown``
    predicate, current-Agent ``task_card_lifecycle`` slot, and closed
    ``task_card_notifications`` operations from ``agent_task_card_ports``.
    Vision receives its read-through ``active_provider`` identity here; its
    setup-selected ``configuration`` snapshot arrives through ``extra_ports``
    exactly as Shell's does. Web receives its narrow read-only
    ``provider_identity`` label here; its Web-owned typed ``web_runtime``
    composition value arrives through ``extra_ports`` from ``web.setup`` alone.
    The registrar grants just ``requires``, never this whole map.
    """
    def _authorize_derived_launch(capability: Any) -> Any:
        from lingtai.kernel.provider_admission import require_derived_launch_admission

        return require_derived_launch_admission(
            getattr(agent, "_derived_launch_admission_port", None),
            capability,
            required=bool(
                getattr(agent, "_requires_derived_launch_admission_port", False)
            ),
        )

    ports = {"workdir": AgentWorkdirAdapter(lambda: agent.working_dir)}
    # Construct only the declaration's earned standard adapter. Lightweight Core
    # test agents need not implement MCP or Avatar APIs when Notification is being
    # granted its own narrow ports.
    if plugin_name in ("mcp", "plugin"):
        ports["prompt_section"] = AgentPromptSectionAdapter(
            plugin_name, agent.update_system_prompt
        )
    if plugin_name == "avatar":
        ports["avatar_parent"] = AgentAvatarParentAdapter(
            lambda: agent.agent_name or agent.working_dir.name,
            lambda: getattr(agent, "_venv_path", None),
            _authorize_derived_launch,
        )
    elif plugin_name == "plugin":
        ports["plugin_catalog"] = AgentPluginCatalogAdapter(
            lambda: getattr(agent, "_plugin_registration", {}),
            lambda: getattr(agent, "_capabilities", ()),
        )
    elif plugin_name == "psyche":
        ports["psyche_settings"] = AgentPsycheSettingsAdapter(
            lambda: getattr(agent, "_psyche_settings_snapshot", None)
        )
    elif plugin_name == "notification":
        # Import Notification Core lazily at the composition-root boundary. The
        # adapter binds Core policy to this live Agent without passing through
        # Agent/Store state.
        from lingtai.kernel.notifications import (
            add_hook,
            delay_notification_channel,
            dismiss_channel,
            drop_hook,
            edit_hook,
            list_hooks,
            notification_delay_max_seconds,
        )
        from lingtai.kernel.meta_block import _notification_persistent_max_chars

        ports["notification_state"] = AgentNotificationStateAdapter(
            dismiss=partial(dismiss_channel, agent, invoked_by="notification"),
            delay=partial(delay_notification_channel, agent),
            add_hook=partial(add_hook, agent),
            drop_hook=partial(drop_hook, agent),
            edit_hook=partial(edit_hook, agent),
            list_hooks=partial(list_hooks, agent),
            read_settings=lambda: (
                _notification_persistent_max_chars(agent),
                notification_delay_max_seconds(),
            ),
            log=agent._log,
        )
    elif plugin_name == "shell":
        ports["notifications"] = AgentNotificationAdapter(
            agent._enqueue_system_notification,
            lambda: agent._notification_store,
        )
    elif plugin_name == "soul":
        ports["soul_runtime"] = agent_soul_runtime(agent)
    elif plugin_name == "system":
        ports["system_runtime"] = agent_system_runtime(agent)
        ports["identity"] = AgentIdentityAdapter(
            lambda: getattr(agent, "agent_name", None),
            lambda name: agent.set_name(name),
            lambda nickname: agent.set_nickname(nickname),
        )
    elif plugin_name == "task_card":
        ports.update(agent_task_card_ports(agent))
    elif plugin_name == "vision":
        # Live read-through to the one active provider service, never the
        # Agent, its capability map, or its provider registry.
        ports["active_provider"] = AgentActiveProviderAdapter(
            lambda: getattr(agent, "service", None)
        )
    elif plugin_name == "web":
        # One read-through label only: the canonical provider name of the
        # current service, never the service, its credentials, or the Agent.
        ports["provider_identity"] = AgentProviderIdentityAdapter(
            lambda: getattr(getattr(agent, "service", None), "provider", None)
        )
    if extra_ports:
        ports.update(extra_ports)
    return ports


def register_agent_tool_plugins(
    agent: Any,
    declarations: Sequence[ToolPluginDeclaration],
    *,
    extra_ports: Mapping[str, Any] | None = None,
    extra_ports_for: Callable[[ToolPluginDeclaration], Mapping[str, Any]] | None = None,
) -> tuple[BoundToolPlugin, ...]:
    """Wire *declarations* onto *agent* through the kernel registrar.

    One declaration per call is the shipped shape today (one family recuts at a
    time). The registrar's name check is batch-wide and runs before the first
    bind, so a **name conflict** is refused as a unit: nothing in the batch
    binds, activates, or mounts. That is the exact scope of the promise — a
    failure raised later, by a binder or by a missing host port on member *N*,
    leaves members 1..*N*-1 mounted and claimed and propagates, because
    unmounting is not a capability this component owns.

    ``extra_ports`` remains the current Context compatibility seam. Daemon,
    Email, File, Shell, Vision, and Web use ``extra_ports_for`` so each can earn
    its runtime or setup-selected port; Notification, Shell, Vision, and Web
    receive their dedicated Agent-derived ports in ``agent_host_ports``,
    without granting them to every declaration. Both maps are merged per
    declaration; conflicting keys from the factory intentionally win only for
    that declaration.

    The port table is built per declaration, on demand, because
    :class:`AgentPromptSectionAdapter` is bound to the declaring plugin's own
    section name. The mount seam is deliberately constructed inside this
    registrar call: it accepts only the kernel's one-use declaration/bound
    transaction, never a caller-supplied plugin or token. Claims are observed
    through the public read-only view and changed through BaseAgent's narrow
    internal claim hook.
    """

    class _InternalMount:
        def mount_tool(self, transaction) -> None:
            agent._mount_official_tool(transaction)

    return register_official_tool_plugins(
        list(declarations),
        ports_for=lambda declaration: agent_host_ports(
            agent,
            declaration.name,
            {
                **dict(extra_ports or {}),
                **(
                    dict(extra_ports_for(declaration))
                    if extra_ports_for is not None
                    else {}
                ),
            },
        ),
        mount=_InternalMount(),
        claimed=agent.official_tool_plugins,
        claim=agent._claim_official_tool,
        authorize=agent._authorize_official_tool_declaration,
        record_bound=agent._record_official_tool_binding,
    )
