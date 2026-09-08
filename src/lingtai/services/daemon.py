"""Standalone composition boundary for the existing daemon engine.

``DaemonService`` owns one caller-chosen filesystem state root.  It is not an
Agent and does not construct one; native LingTai requests instead name the
preset path that supplies their LLM and capability configuration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from lingtai.adapters.posix.notification_store import PosixNotificationStoreAdapter
from lingtai.kernel.base_agent.messaging import _enqueue_system_notification
from lingtai.kernel.provider_admission import require_derived_launch_admission
from lingtai.kernel.risky_action_gate import build_risky_action_check
from lingtai.kernel.tool_call_guard import ToolCallGuard


class DaemonServiceError(ValueError):
    """A caller-visible standalone service configuration error."""


class _StateRoot:
    """Daemon's narrow workdir port over a standalone state root."""

    def __init__(self, path: Path) -> None:
        self.path = path


class _StandaloneDaemonRuntime:
    """Daemon runtime operations that do not grant Agent ownership or policy."""

    def __init__(self, state_root: Path, manager_options: Mapping[str, Any]) -> None:
        self._working_dir = state_root
        self._manager_options = dict(manager_options)
        self._notification_store = PosixNotificationStoreAdapter(state_root)
        self._tool_call_guard = ToolCallGuard([build_risky_action_check(state_root)])
        self._file_io = None
        self._config = SimpleNamespace(language="en", max_aed_attempts=3)
        self._session = None
        self._intrinsics: dict[str, Any] = {}
        self._intrinsic_modules: dict[str, Any] = {}
        self._mcp_tool_names: set[str] = set()
        self._manager: Any = None

    @property
    def service(self) -> Any:
        raise RuntimeError(
            "standalone LingTai daemon requests require an explicit preset"
        )

    @property
    def tool_schemas(self) -> tuple[Any, ...]:
        return ()

    @property
    def tool_handlers(self) -> Mapping[str, Callable[[dict], dict]]:
        return {}

    @property
    def mcp_tool_names(self) -> frozenset[str]:
        return frozenset()

    @property
    def language(self) -> str:
        return "en"

    @property
    def max_aed_attempts(self) -> int:
        return 3

    @property
    def tool_call_guard(self) -> ToolCallGuard:
        return self._tool_call_guard

    @property
    def manager_options(self) -> Mapping[str, Any]:
        return dict(self._manager_options)

    @property
    def requires_derived_launch_admission(self) -> bool:
        return False

    def authorize_derived_launch(self, capability: Any) -> Any:
        return require_derived_launch_admission(None, capability, required=False)

    def setup_preset_capability(
        self, name: str, kwargs: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Callable[[dict], dict]]]:
        from lingtai.tools.daemon import _ToolCollector
        from lingtai.tools.registry import setup_capability

        if self._file_io is None and name == "file":
            from lingtai.services.file_io_sidecar import default_file_io_service

            self._file_io = default_file_io_service(self._working_dir)
        collector = _ToolCollector(self)
        setup_capability(collector, name, **dict(kwargs))
        return collector.schemas, collector.handlers

    @property
    def requires_explicit_preset(self) -> bool:
        return True

    def is_preset_authorized(self, name: str, working_dir: Any) -> bool:
        # Direct caller selection is the standalone authority. Load/validation
        # still happens through the canonical read-only preset loader.
        return isinstance(name, str) and bool(name.strip())

    def load_preset(self, name: str) -> dict:
        from lingtai.agent import load_preset

        return load_preset(name, working_dir=self._working_dir)

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
        _enqueue_system_notification(
            self,
            source=source,
            ref_id=ref_id,
            body=body,
            idempotency_key=idempotency_key,
            skip_if_idempotency_key_exists=skip_if_idempotency_key_exists,
            extra=dict(extra),
            channel=channel,
        )

    def has_active_task_card_watch(self) -> bool | None:
        return None

    def attach_daemon_manager(self, manager: Any) -> None:
        self._manager = manager

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def log(self, _event_type: str, **_fields: Any) -> None:
        return None

    def _log(self, event_type: str, **fields: Any) -> None:
        self.log(event_type, **fields)

    def _wake_nap(self, _reason: str) -> None:
        return None


class _ReadOnlyDaemonView:
    """Bind ledger-driven read handlers without manager startup mutations."""

    def __init__(self, runtime: _StandaloneDaemonRuntime, workdir: _StateRoot) -> None:
        from lingtai.tools.daemon import DaemonManager

        self._runtime = runtime
        self._workdir = workdir
        self._emanations: dict[str, Any] = {}
        self._manager_pool_size = 100
        self._manager_type = DaemonManager

    def __getattr__(self, name: str) -> Any:
        manager = self.__dict__.get("_manager_type")
        if manager is not None:
            attr = getattr(manager, name, None)
            if callable(attr):
                raw = manager.__dict__.get(name)
                if isinstance(raw, staticmethod):
                    return attr
                return lambda *args, **kwargs: attr(self, *args, **kwargs)
            if attr is not None:
                return attr
        raise AttributeError(name)


class DaemonService:
    """Reusable standalone API over LingTai's existing daemon orchestration.

    Args:
        state_root: Existing caller-owned directory for daemon runs, manager
            records, task snapshots, control spools, and notifications.
    """

    def __init__(self, state_root: str | Path) -> None:
        root = Path(state_root).expanduser().resolve()
        if not root.is_dir():
            raise DaemonServiceError(f"state root is not a directory: {root}")
        self.state_root = root
        self._workdir = _StateRoot(root)

        from lingtai.tools.daemon import _load_config

        config = _load_config(root)
        options = {
            "max_turns": config.max_turns,
            "timeout": 3600.0,
            "notify_threshold": 20,
            "manager_pool_size": config.manager_pool_size,
            "system_prompt_budget_chars": config.system_prompt_budget_chars,
        }
        self._runtime = _StandaloneDaemonRuntime(root, options)
        self._manager: Any = None
        self._write_dispatcher: Any = None
        self._read_dispatcher: Any = None

    def _dispatcher(self, *, read_only: bool) -> Any:
        from lingtai.tools.daemon import DECLARATION, DaemonManager, _BACKEND_SCHEMA_ENUM
        from lingtai.tools.daemon._tool_family import DaemonFamilyDispatcher

        if read_only:
            if self._read_dispatcher is None:
                view = _ReadOnlyDaemonView(self._runtime, self._workdir)
                self._read_dispatcher = DaemonFamilyDispatcher(
                    view,
                    self._workdir,
                    list(_BACKEND_SCHEMA_ENUM),
                    declaration=DECLARATION,
                )
            return self._read_dispatcher
        if self._write_dispatcher is None:
            options = self._runtime.manager_options
            self._manager = DaemonManager(
                self._runtime,
                max_turns=options["max_turns"],
                timeout=options["timeout"],
                notify_threshold=options["notify_threshold"],
                manager_pool_size=options["manager_pool_size"],
                system_prompt_budget_chars=options["system_prompt_budget_chars"],
                workdir=self._workdir,
            )
            self._runtime.attach_daemon_manager(self._manager)
            self._write_dispatcher = DaemonFamilyDispatcher(
                self._manager,
                self._workdir,
                list(_BACKEND_SCHEMA_ENUM),
                declaration=DECLARATION,
            )
        return self._write_dispatcher

    def _invoke(self, action: str, action_input: Mapping[str, Any], *, read_only: bool) -> dict:
        return self._dispatcher(read_only=read_only).handle(
            {
                "action": action,
                "input": dict(action_input),
                "reasoning": "standalone daemon service request",
            }
        )

    def emanate(
        self,
        tasks: Sequence[Mapping[str, Any]],
        *,
        backend: str = "lingtai",
        max_turns: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Validate and dispatch a batch through the canonical daemon family."""
        return self._invoke(
            "emanate",
            {
                "tasks": [dict(task) for task in tasks],
                "backend": backend,
                "max_turns": max_turns,
                "timeout": timeout,
            },
            read_only=False,
        )

    def list(self, *, status: str = "all", last: int | None = None) -> dict:
        """Read the standalone root's bounded durable daemon index."""
        return self._invoke(
            "list",
            {"contains": "", "status": status, "include_done": True, "last": last},
            read_only=True,
        )

    def check(self, daemon_id: str, *, last: int = 20, truncate: int = 500) -> dict:
        """Read one durable daemon state and its bounded event tail."""
        return self._invoke(
            "check",
            {"id": daemon_id, "last": last, "truncate": truncate},
            read_only=True,
        )

    def reclaim(self) -> dict:
        """Request cancellation of active work owned by this state root."""
        return self._invoke("reclaim", {}, read_only=False)


__all__ = ["DaemonService", "DaemonServiceError"]
