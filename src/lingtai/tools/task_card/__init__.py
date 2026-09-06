"""Intrinsic declarative Task Card capability.

One model-facing root ``task_card`` owns a single agent-local Task Card artifact
under ``<workdir>/taskcard/``:

- ``status``     — exact text ``active`` or ``inactive``
- ``taskcard.md`` — the rendered card body
- ``watch.json`` — persisted active-watch descriptor for restart resume

The producer writes only those files. Channels consume/project them
independently. The watch descriptor survives ``refresh``/molt/agent-stop so a
restart rehydrates the active watch; ``stop``/``remove``/refresh exhaustion
clear it because those are deliberate terminal ends.

This is an official declared host plugin. Its static declaration is built at
import; renderer/watch behavior binds only to narrow workdir, shutdown,
lifecycle, and Task Card notification ports, never a whole Agent.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from lingtai.kernel import notifications
from lingtai.kernel._fsutil import atomic_write_json, read_json
from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration

from ..tool_family import ChildTool, SettingRow, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child

if TYPE_CHECKING:
    from lingtai.kernel.tool_plugin import ToolPluginHost

# Built-in fallback defaults, used when no configured value applies (missing
# config file, missing field, or an invalid field). ``interval_s`` is a pure
# cadence default with no ceiling — only ``_MIN_INTERVAL_S`` bounds it, in
# either direction. ``timeout_s``/``max_refreshes`` are safety ceilings: a
# configured value lowers the effective ceiling for both the default (used
# when a watch omits the field) and the maximum an explicit per-watch value
# may request.
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_INTERVAL_S = 5.0
_MIN_INTERVAL_S = 1.0
_MIN_TIMEOUT_S = 0.1
_DEFAULT_MAX_REFRESHES = 2000
_DEFAULT_REMINDER_TURNS = 10
_TASKCARD_DIR = "taskcard"
# Hard ceiling for the rendered card body (Jason #taskcard-resident). A renderer
# output longer than this is REFUSED, never truncated, so the resident
# ``_meta.agent_meta.taskcard`` projection can stay a bounded high-attention
# goal. Complex progress belongs in files behind the card.
_MAX_BODY_CHARS = 2000
_STATUS_FILENAME = "status"
_BODY_FILENAME = "taskcard.md"
_CONFIG_FILENAME = "taskcard.json"
_WATCH_FILENAME = "watch.json"
# One-way migration source only: the retired Telegram-owned reverse-channel
# design persisted its own refresh ceiling here. Consulted only when this
# capability's own config file has never been created; that first resolution
# is always persisted (migrated or not), so this path is never read again
# afterward (see ``TaskCardManager._migrate_legacy_config``).
_LEGACY_CONFIG_DIR = "telegram"
# ``TelegramService``'s own untouched default/ceiling for that legacy field
# (see ``mcp_servers/telegram/service.py:_TASKCARD_DEFAULT_MAX_REFRESHES``).
# The ordinary ``/taskcard on|off|N`` commands persist that file's three
# fields together purely to toggle unrelated presentation settings, so a
# ``max_refreshes`` sitting at exactly this value carries no migration
# signal — only a value that actually differs proves a human, or an earlier
# now-removed interface, once chose it on purpose. Migrating this untouched
# value forward would silently cap most real agents below the new built-in
# default instead of leaving them on it.
_LEGACY_UNTOUCHED_MAX_REFRESHES = 1000


class _Config(NamedTuple):
    """Resolved agent-wide Task Card defaults/ceilings for a new watch."""

    interval_s: float
    timeout_s: float
    max_refreshes: int
    reminder_turns: int
    max_body_chars: int


_BUILTIN_CONFIG = _Config(
    _DEFAULT_INTERVAL_S,
    _DEFAULT_TIMEOUT_S,
    _DEFAULT_MAX_REFRESHES,
    _DEFAULT_REMINDER_TURNS,
    _MAX_BODY_CHARS,
)


@dataclass(frozen=True, slots=True)
class TaskCardErrorNotification:
    """Producer-owned error event accepted by the notification adapter.

    ``source``, ``channel``, and arbitrary publisher fields intentionally do
    not exist here. The adapter supplies the fixed Task Card source and
    system-channel policy, while the producer supplies the event body and
    idempotency identity.
    """

    watch_id: str
    body: str
    code: str
    retryable: bool | Literal["unknown"]
    idempotency_key: str
    last_valid_body_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskCardRecoveredNotification:
    """Producer-owned recovered event with a fixed Task Card source."""

    watch_id: str
    body: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class TaskCardLimitNotification:
    """Producer-owned refresh-limit event with bounded extra fields."""

    watch_id: str
    body: str
    idempotency_key: str
    used: int
    max_refreshes: int
    last_valid_body_at: str | None = None


#: The five closed operations the kernel ``TaskCardNotificationsPort`` grants.
#: There is deliberately no generic ``enqueue`` name in this tuple: a port that
#: offers one is not the native port and is refused at the family boundary.
_NATIVE_NOTIFICATION_OPERATIONS = (
    "publish_error",
    "publish_recovered",
    "publish_limit",
    "submit_reminder",
    "clear_reminder",
)


class TaskCardNotificationsAdapter:
    """Family-local bridge from typed producer events to the native port.

    The granted ``TaskCardNotificationsPort`` exposes only five closed,
    scalar-signature operations; the production adapter pins source, channel,
    priority, idempotency, and the bounded ``extra`` projection behind them.
    This class keeps the producer's immutable typed event forms as the sole
    family API and forwards each event's fields positionally by name to the
    matching native operation. It consumes nothing but those five operations:
    a port carrying a generic publisher (``enqueue_system_notification`` or any
    ``**kwargs`` vocabulary) is refused, and the manager retains only this
    typed view — never a host, a generic publisher, or a service locator.
    """

    __slots__ = ("_error", "_recovered", "_limit", "_submit", "_clear")

    def __init__(self, port: Any) -> None:
        operations = {}
        for name in _NATIVE_NOTIFICATION_OPERATIONS:
            operation = getattr(port, name, None)
            if not callable(operation):
                raise TypeError(
                    f"Task Card notification port lacks native operation {name!r}"
                )
            operations[name] = operation
        if callable(getattr(port, "enqueue_system_notification", None)):
            raise TypeError(
                "Task Card notification port must be operation-native, not a generic publisher"
            )
        self._error = operations["publish_error"]
        self._recovered = operations["publish_recovered"]
        self._limit = operations["publish_limit"]
        self._submit = operations["submit_reminder"]
        self._clear = operations["clear_reminder"]

    def publish_error(self, event: TaskCardErrorNotification) -> None:
        if not isinstance(event, TaskCardErrorNotification):
            raise TypeError("publish_error requires TaskCardErrorNotification")
        self._error(
            watch_id=event.watch_id,
            body=event.body,
            code=event.code,
            retryable=event.retryable,
            idempotency_key=event.idempotency_key,
            last_valid_body_at=event.last_valid_body_at,
        )

    def publish_recovered(self, event: TaskCardRecoveredNotification) -> None:
        if not isinstance(event, TaskCardRecoveredNotification):
            raise TypeError("publish_recovered requires TaskCardRecoveredNotification")
        # The native port keeps the established wire source: recovered is a
        # state on the Task Card error stream, distinguished by extra.state and
        # its recovered idempotency key.
        self._recovered(
            watch_id=event.watch_id,
            body=event.body,
            idempotency_key=event.idempotency_key,
        )

    def publish_limit(self, event: TaskCardLimitNotification) -> None:
        if not isinstance(event, TaskCardLimitNotification):
            raise TypeError("publish_limit requires TaskCardLimitNotification")
        self._limit(
            watch_id=event.watch_id,
            body=event.body,
            idempotency_key=event.idempotency_key,
            used=event.used,
            max_refreshes=event.max_refreshes,
            last_valid_body_at=event.last_valid_body_at,
        )

    def submit_reminder(self, turns: int) -> None:
        if type(turns) is not int or turns <= 0:
            raise TypeError("Task Card reminder turns must be a positive integer")
        self._submit(turns)

    def clear_reminder(self) -> None:
        self._clear()


@dataclass(frozen=True, slots=True)
class _TaskCardRuntime:
    """Manager-only host view: workdir, shutdown, and the typed notification bridge."""

    workdir: Any
    shutdown: Any
    task_card_notifications: TaskCardNotificationsAdapter


def _task_card_runtime(host: Any) -> _TaskCardRuntime:
    return _TaskCardRuntime(
        workdir=host.workdir,
        shutdown=host.shutdown,
        task_card_notifications=TaskCardNotificationsAdapter(host.task_card_notifications),
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required is not None:
        value["required"] = required
    return value


_START_INPUT_SCHEMA = _object(
    {
        "renderer_path": {"type": "string"},
        "interval_s": {"type": "number", "minimum": 1},
        "timeout_s": {"type": "number", "minimum": 0.1},
        "max_refreshes": {"type": "integer", "minimum": 1},
    },
    required=["renderer_path"],
)
_WATCH_INPUT_SCHEMA = _object({"watch_id": {"type": "string"}}, required=["watch_id"])
_REMOVE_INPUT_SCHEMA = _object({}, required=[])

_DECLARED_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "start": _START_INPUT_SCHEMA,
    "inspect": _WATCH_INPUT_SCHEMA,
    "retry": _WATCH_INPUT_SCHEMA,
    "stop": _WATCH_INPUT_SCHEMA,
    "remove": _REMOVE_INPUT_SCHEMA,
}

_DESCRIPTION = (
    "Maintain one channel-neutral Task Card per agent. start runs an existing "
    "Python renderer inside the working directory; non-empty stdout is the full "
    "body written atomically to taskcard/taskcard.md, then exact active is written "
    "to taskcard/status. One watch only. Keep output truthful for meaningful "
    "long-running, multi-step, or parallel work, not quick ritual updates. stop "
    "pauses/preserves; remove retires then deletes. Consumers project state "
    "independently. Restart after expiry; use settings and manual for detail."
)

_ACTION_DESCRIPTION = (
    "Task Card action: start/run, inspect/read, retry/refresh, stop/pause, "
    "remove/retire, settings/SHOW, or manual/router."
)


def get_schema() -> dict[str, Any]:
    """Return the declaration-composed public schema, before or after binding."""
    schema = _FAMILY.build_schema()
    schema["properties"]["action"]["description"] = _ACTION_DESCRIPTION
    return schema


def get_description() -> str:
    """The declaration's single model-facing description."""
    return DECLARATION.description


class TaskCardError(Exception):
    """Synchronous, user-visible Task Card error."""


class _Watch:
    __slots__ = (
        "watch_id",
        "renderer_path",
        "interval_s",
        "timeout_s",
        "thread",
        "stop_event",
        "lock",
        "last_valid_body",
        "last_valid_at",
        "error",
        "error_key",
        "error_epoch",
        "stopping",
        "max_refreshes",
        "refreshes_used",
        "attempt_lock",
        "limit_notified",
        "stop_reason",
        "terminated",
    )

    def __init__(
        self,
        watch_id: str,
        renderer_path: Path,
        interval_s: float,
        timeout_s: float,
        max_refreshes: int,
    ) -> None:
        self.watch_id = watch_id
        self.renderer_path = renderer_path
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.last_valid_body: str | None = None
        self.last_valid_at: str | None = None
        self.error: dict[str, Any] | None = None
        self.error_key: str | None = None
        self.error_epoch = 0
        self.stopping = False
        self.max_refreshes = max_refreshes
        self.refreshes_used = 0
        self.attempt_lock = threading.Lock()
        self.limit_notified = False
        self.stop_reason: str | None = None
        self.terminated = False


class TaskCardManager:
    """Own the intrinsic Task Card watch and atomic writer contract."""

    def __init__(self, host: "ToolPluginHost") -> None:
        self._host = _task_card_runtime(host)
        self._lock = threading.RLock()
        self._counter = 0
        self._completed_text_turns = 0
        self._watch: _Watch | None = None
        self._set_paths(self._host.workdir.path)

    def rebind(self, host: "ToolPluginHost") -> None:
        """Keep this current-Agent manager across refresh with fresh narrow ports."""
        self._host = _task_card_runtime(host)
        self._set_paths(self._host.workdir.path)

    def _set_paths(self, workdir: Path) -> None:
        self._taskcard_dir = Path(workdir) / _TASKCARD_DIR
        self._status_path = self._taskcard_dir / _STATUS_FILENAME
        self._body_path = self._taskcard_dir / _BODY_FILENAME
        self._config_path = self._taskcard_dir / _CONFIG_FILENAME
        self._watch_path = self._taskcard_dir / _WATCH_FILENAME
        self._legacy_config_path = self._taskcard_dir.parent / _LEGACY_CONFIG_DIR / _CONFIG_FILENAME

    def handle(self, args: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return self._family().handle(args or {})
        except TaskCardError as exc:
            return {"status": "failed", "message": str(exc)}

    def _family(self) -> ToolFamily:
        return _build_family(self._host, self)

    def _start_child(self, input_: dict[str, Any]) -> dict[str, Any]:
        renderer_path = self._validate_renderer_path(input_.get("renderer_path"))
        config = self._load_config()
        # interval_s is a cadence default, not a safety ceiling: an omitted
        # value uses the configured cadence, and an explicit value is never
        # clamped down to it — only the absolute floor applies either way.
        interval_s = self._coerce_positive(
            input_.get("interval_s", config.interval_s), "interval_s", _MIN_INTERVAL_S
        )
        # timeout_s and max_refreshes are safety ceilings: an omitted value
        # uses the configured ceiling, and an explicit value may only lower
        # it (min-clamped), never exceed it.
        requested_timeout = input_.get("timeout_s")
        if requested_timeout is None:
            timeout_s = config.timeout_s
        else:
            timeout_s = min(
                self._coerce_positive(requested_timeout, "timeout_s", _MIN_TIMEOUT_S),
                config.timeout_s,
            )
        requested_max = input_.get("max_refreshes")
        if requested_max is None:
            effective_max = config.max_refreshes
        elif type(requested_max) is int and requested_max > 0:
            effective_max = min(requested_max, config.max_refreshes)
        else:
            raise TaskCardError("max_refreshes must be a positive integer")
        with self._lock:
            if self._watch is not None:
                raise TaskCardError("only one Task Card watch may be active per agent")
            self._counter += 1
            watch = _Watch(
                f"tc_{self._counter}",
                renderer_path,
                interval_s,
                timeout_s,
                effective_max,
            )
            self._watch = watch
        try:
            body = self._run_renderer(renderer_path, timeout_s)
            self._publish_active(body)
            self._clear_reminder()
        except Exception:
            with self._lock:
                if self._watch is watch:
                    self._watch = None
            try:
                self._write_status("inactive")
            except OSError:
                pass
            raise
        with watch.lock:
            watch.last_valid_body = body
            watch.last_valid_at = _utc_now_iso()
        # Persist the descriptor before spawning the thread: the updater could
        # otherwise exhaust and clear it in the gap, resurrecting a dead watch.
        # Persistence is best-effort here — a start that already succeeded must
        # not fail because the descriptor write hit ENOSPC/EROFS/EACCES.
        try:
            self._persist_watch(watch)
        except OSError:
            pass
        self._spawn(watch)
        return {
            "status": "ok",
            "watch_id": watch.watch_id,
            "state": "watching",
            **self._paths_payload(),
            **self._status_payload("active"),
            **self._refresh_fields(watch),
        }

    def _inspect_child(self, input_: dict[str, Any]) -> dict[str, Any]:
        watch = self._require_watch(input_.get("watch_id"))
        with watch.lock:
            if watch.stopping:
                state = "stop_failed" if watch.error else "stopping"
                status_value = "inactive"
            elif watch.error:
                state = "error"
                status_value = "active"
            else:
                state = "watching"
                status_value = "active"
            return {
                "status": "ok",
                "watch_id": watch.watch_id,
                "state": state,
                "last_valid_body": watch.last_valid_body,
                "last_valid_body_at": watch.last_valid_at,
                "error": watch.error,
                **self._paths_payload(),
                **self._status_payload(status_value),
                **self._refresh_fields(watch),
            }

    def _retry_child(self, input_: dict[str, Any]) -> dict[str, Any]:
        watch = self._require_watch(input_.get("watch_id"))
        if watch.stopping:
            return self._stop_watch(watch)
        self._tick(watch)
        return self._inspect_child({"watch_id": watch.watch_id})

    def _stop_child(self, input_: dict[str, Any]) -> dict[str, Any]:
        return self._stop_watch(self._require_watch(input_.get("watch_id")))

    def _stop_watch(self, watch: _Watch) -> dict[str, Any]:
        with watch.lock:
            watch.stopping = True
        try:
            self._write_status("inactive")
        except OSError as exc:
            error = {
                "code": "stop_finalize_failed",
                "retryable": True,
                "message": f"failed to write inactive status: {type(exc).__name__}",
            }
            with watch.lock:
                watch.error = error
            return {
                "status": "error",
                "watch_id": watch.watch_id,
                "state": "stop_failed",
                "error": error,
                **self._paths_payload(),
                **self._status_payload("inactive"),
                **self._refresh_fields(watch),
            }
        watch.stop_event.set()
        thread = watch.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=watch.timeout_s + 1.0)
        if thread is not None and thread.is_alive():
            error = {
                "code": "stop_thread_alive",
                "retryable": True,
                "message": "the watcher thread has not stopped yet; retry stop once quiescent",
            }
            with watch.lock:
                watch.error = error
            return {
                "status": "error",
                "watch_id": watch.watch_id,
                "state": "stop_failed",
                "error": error,
                **self._paths_payload(),
                **self._status_payload("inactive"),
                **self._refresh_fields(watch),
            }
        with self._lock:
            if self._watch is watch:
                self._watch = None
        with watch.lock:
            watch.terminated = True
        self._clear_watch_descriptor()
        return {
            "status": "ok",
            "watch_id": watch.watch_id,
            "state": "stopped",
            **self._paths_payload(),
            **self._status_payload("inactive"),
            **self._refresh_fields(watch),
        }

    def _remove_child(self, input_: dict[str, Any]) -> dict[str, Any]:
        """Terminal lifecycle cleanup: retire any active watch, then delete the body.

        Unlike ``stop``, ``remove`` takes no ``watch_id`` — it targets this
        agent's one artifact, not a specific watch, so it stays useful even
        after a restart lost the in-memory watch handle. If a watch is
        active it is retired exactly like ``stop`` (write ``inactive`` before
        the updater joins), so the updater cannot race a deleted body back
        into existence; only once that retirement is confirmed is the body
        actually removed. A stop failure (thread still running) blocks
        removal and is returned verbatim under ``state: "remove_blocked"`` so
        the caller can retry once the watch is quiescent.
        """
        with self._lock:
            watch = self._watch
        if watch is not None:
            stopped = self._stop_watch(watch)
            if stopped["status"] != "ok":
                return {**stopped, "state": "remove_blocked"}
        return self._finalize_remove()

    def _finalize_remove(self) -> dict[str, Any]:
        try:
            self._write_status("inactive")
        except OSError as exc:
            return {
                "status": "error",
                "state": "remove_failed",
                "error": {
                    "code": "remove_finalize_failed",
                    "retryable": True,
                    "message": f"failed to write inactive status: {type(exc).__name__}",
                },
                **self._paths_payload(),
                **self._status_payload("inactive"),
            }
        body_removed, delete_error = self._delete_body()
        if delete_error is not None:
            return {
                "status": "error",
                "state": "remove_failed",
                "error": {
                    "code": "remove_body_failed",
                    "retryable": True,
                    "message": f"failed to remove task card body: {type(delete_error).__name__}",
                },
                **self._paths_payload(),
                **self._status_payload("inactive"),
            }
        self._clear_watch_descriptor()
        # A concurrent agent-shutdown may still hold this watch: mark it
        # terminated so shutdown does not re-persist the descriptor after
        # ``remove`` deliberately retired it.
        with self._lock:
            watch = self._watch
        if watch is not None:
            with watch.lock:
                watch.terminated = True
        self._clear_reminder()
        return {
            "status": "ok",
            "state": "removed",
            "body_removed": body_removed,
            **self._paths_payload(),
            **self._status_payload("inactive"),
        }

    def _delete_body(self) -> tuple[bool, OSError | None]:
        try:
            self._body_path.unlink()
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            return False, exc
        return True, None

    def _spawn(self, watch: _Watch) -> None:
        watch.thread = threading.Thread(
            target=self._loop,
            args=(watch,),
            daemon=True,
            name=f"task-card-watch-{watch.watch_id}",
        )
        watch.thread.start()

    def _loop(self, watch: _Watch) -> None:
        while not watch.stop_event.is_set():
            if self._host.shutdown.is_set():
                return
            if watch.stop_event.wait(timeout=watch.interval_s):
                return
            if self._host.shutdown.is_set():
                return
            try:
                self._tick(watch)
            except Exception:
                # A background watch must never die without a trace: mark the
                # error (and notify) instead of letting the thread vanish.
                self._mark_error(
                    watch,
                    {
                        "code": "watch_crash",
                        "retryable": True,
                        "message": "task card watch crashed in refresh loop",
                    },
                    emit_notification=True,
                )
                return

    def _tick(self, watch: _Watch) -> None:
        with watch.attempt_lock:
            with watch.lock:
                if watch.stopping or watch.refreshes_used >= watch.max_refreshes:
                    return
                watch.refreshes_used += 1
                exhausted = watch.refreshes_used >= watch.max_refreshes
            try:
                body = self._run_renderer(watch.renderer_path, watch.timeout_s)
            except TaskCardError as exc:
                if self._stop_requested(watch):
                    return
                self._mark_error(watch, self._error_from_exc(exc), emit_notification=not exhausted)
                if exhausted:
                    self._exhaust(watch)
                return
            if self._stop_requested(watch):
                return
            try:
                self._write_body(body)
            except TaskCardError as exc:
                self._mark_error(
                    watch,
                    {
                        "code": "body_too_large",
                        "retryable": False,
                        "message": str(exc),
                    },
                    emit_notification=not exhausted,
                )
                if exhausted:
                    self._exhaust(watch)
                return
            except OSError as exc:
                self._mark_error(
                    watch,
                    {
                        "code": "write_failed",
                        "retryable": True,
                        "message": f"failed to update task card body: {type(exc).__name__}",
                    },
                    emit_notification=not exhausted,
                )
                if exhausted:
                    self._exhaust(watch)
                return
            self._mark_recovered(watch, body)
            self._clear_reminder()
            if exhausted and not watch.stopping:
                self._exhaust(watch)

    def _exhaust(self, watch: _Watch) -> None:
        with watch.lock:
            if watch.stop_reason == "max_refreshes":
                return
            watch.stop_reason = "max_refreshes"
            watch.stopping = True
        try:
            self._write_status("inactive")
        except OSError as exc:
            with watch.lock:
                watch.error = {
                    "code": "stop_finalize_failed",
                    "retryable": True,
                    "message": f"failed to write inactive status: {type(exc).__name__}",
                }
            self._emit_limit_event(watch)
            return
        with watch.lock:
            watch.terminated = True
        watch.stop_event.set()
        self._emit_limit_event(watch)
        with self._lock:
            if self._watch is watch:
                self._watch = None
        self._clear_watch_descriptor()

    def _stop_requested(self, watch: _Watch) -> bool:
        if watch.stop_event.is_set():
            return True
        return self._host.shutdown.is_set()

    def _publish_active(self, body: str) -> None:
        self._write_body(body)
        self._write_status("active")

    def _write_body(self, body: str) -> None:
        limit = self._load_config().max_body_chars
        if len(body) > limit:
            raise TaskCardError(
                f"taskcard body exceeds the {limit}-char cap "
                f"({len(body)} chars); keep the card a progressive-disclosure "
                "summary and move complex progress into files"
            )
        self._atomic_write_text(self._body_path, body, trailing_newline=False)

    def _write_status(self, status: str) -> None:
        if status not in {"active", "inactive"}:
            raise ValueError(f"invalid task card status: {status}")
        self._atomic_write_text(self._status_path, status, trailing_newline=False)

    @staticmethod
    def _atomic_write_text(path: Path, text: str, *, trailing_newline: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = text if not trailing_newline else f"{text}\n"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _run_renderer(self, path: Path, timeout_s: float) -> str:
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(self._host.workdir.path),
            )
        except subprocess.TimeoutExpired as exc:
            raise TaskCardError(f"renderer timed out after {timeout_s}s") from exc
        except OSError as exc:
            raise TaskCardError("renderer could not be executed") from exc
        if proc.returncode != 0:
            raise TaskCardError(f"renderer exited with status {proc.returncode}")
        return self._validate_body(proc.stdout)

    @staticmethod
    def _validate_body(stdout: str) -> str:
        if not isinstance(stdout, str) or not stdout.strip():
            raise TaskCardError("renderer produced no output")
        return stdout

    @staticmethod
    def _error_from_exc(exc: TaskCardError) -> dict[str, Any]:
        message = str(exc)
        if "timed out" in message:
            code = "renderer_timeout"
        elif "status" in message:
            code = "renderer_nonzero_exit"
        else:
            code = "renderer_failed"
        return {"code": code, "message": message, "retryable": True}

    def _mark_error(
        self,
        watch: _Watch,
        error: dict[str, Any],
        *,
        emit_notification: bool = True,
    ) -> None:
        key = str(error.get("code"))
        with watch.lock:
            if watch.error is None:
                watch.error_epoch += 1
            already = watch.error_key == key
            watch.error = error
            watch.error_key = key
            epoch = watch.error_epoch
            last_valid_at = watch.last_valid_at
        if already or not emit_notification:
            return
        self._emit_event(watch, error, last_valid_at, epoch=epoch, recovered=False)

    def _mark_recovered(self, watch: _Watch, body: str) -> None:
        with watch.lock:
            was_errored = watch.error is not None
            epoch = watch.error_epoch
            watch.error = None
            watch.error_key = None
            watch.last_valid_body = body
            watch.last_valid_at = _utc_now_iso()
        if was_errored:
            self._emit_event(watch, None, None, epoch=epoch, recovered=True)

    def _emit_event(
        self,
        watch: _Watch,
        error: dict[str, Any] | None,
        last_valid_at: str | None,
        *,
        epoch: int,
        recovered: bool,
    ) -> None:
        try:
            if recovered:
                self._host.task_card_notifications.publish_recovered(
                    TaskCardRecoveredNotification(
                        watch_id=watch.watch_id,
                        body=f"Task Card watch {watch.watch_id} recovered.",
                        idempotency_key=f"task_card.recovered:{watch.watch_id}:{epoch}",
                    )
                )
            else:
                code = str((error or {}).get("code", "error"))
                self._host.task_card_notifications.publish_error(
                    TaskCardErrorNotification(
                        watch_id=watch.watch_id,
                        body=(
                            f"Task Card watch {watch.watch_id} failed: "
                            f"{(error or {}).get('message', code)}"
                        ),
                        code=code,
                        retryable=(error or {}).get("retryable", "unknown"),
                        idempotency_key=f"task_card.error:{watch.watch_id}:{epoch}:{code}",
                        last_valid_body_at=last_valid_at,
                    )
                )
        except Exception:
            pass

    def _emit_limit_event(self, watch: _Watch) -> None:
        with watch.lock:
            if watch.limit_notified:
                return
            watch.limit_notified = True
            used = watch.refreshes_used
            maximum = watch.max_refreshes
            last_valid_at = watch.last_valid_at
        try:
            self._host.task_card_notifications.publish_limit(
                TaskCardLimitNotification(
                    watch_id=watch.watch_id,
                    body=(
                        f"Task Card watch {watch.watch_id} reached its refresh limit. "
                        "Refresh or reinspect the underlying task state, and start a new "
                        "watch only if useful. If this work is still ongoing, start a new "
                        "watch (task_card action='start') — do not let the card go dark "
                        "mid-task."
                    ),
                    idempotency_key=f"task_card.limit:{watch.watch_id}:{maximum}",
                    used=used,
                    max_refreshes=maximum,
                    last_valid_body_at=last_valid_at,
                )
            )
        except Exception:
            pass

    def has_active_watch(self) -> bool:
        """True while exactly one watch is running and not yet retiring.

        Read-only cross-capability probe: the daemon fleet nudge asks this
        before suggesting a card, so an agent that already keeps one is never
        told to start another.
        """
        with self._lock:
            watch = self._watch
        if watch is None:
            return False
        with watch.lock:
            return not (watch.stopping or watch.terminated)

    def _require_watch(self, watch_id: Any) -> _Watch:
        if not isinstance(watch_id, str):
            raise TaskCardError("watch_id is required")
        with self._lock:
            watch = self._watch
        if watch is None or watch.watch_id != watch_id:
            raise TaskCardError(f"unknown watch_id: {watch_id}")
        return watch

    def on_completed_work_turn(self) -> None:
        threshold = self._reminder_turns()
        with self._lock:
            self._completed_text_turns += 1
            if self._completed_text_turns < threshold:
                return
            self._completed_text_turns = 0
        self._host.task_card_notifications.submit_reminder(threshold)

    def _clear_reminder(self) -> None:
        with self._lock:
            self._completed_text_turns = 0
        try:
            self._host.task_card_notifications.clear_reminder()
        except AttributeError:
            pass

    def shutdown_for_agent_stop(self, *, reason: str = "") -> None:
        del reason
        self._clear_reminder()
        with self._lock:
            watch = self._watch
            self._watch = None
        if watch is None:
            return
        with watch.lock:
            watch.stopping = True
        try:
            self._write_status("inactive")
        except OSError:
            pass
        watch.stop_event.set()
        if watch.thread is not None and watch.thread.is_alive():
            watch.thread.join(timeout=watch.timeout_s + 1.0)
        # Carry the live refresh budget into the persisted descriptor so a
        # restart resume honors it instead of starting over at zero. A watch
        # already deliberately terminated (stop/remove/exhaust racing this
        # shutdown) must not be resurrected: re-check the flag under
        # ``watch.lock``, which also serializes against the descriptor write.
        with watch.lock:
            if watch.terminated:
                return
            try:
                self._persist_watch(watch)
            except OSError:
                pass

    def _persist_watch(self, watch: _Watch) -> None:
        """Persist the active watch descriptor so a restart can resume it.

        Written atomically on a successful ``start``. Kept across
        ``shutdown_for_agent_stop`` (refresh/molt/agent-stop) so the next
        process can rehydrate the watch; cleared on ``stop``, ``remove``, or
        refresh-limit exhaustion because those are deliberate terminal ends
        of the watch, not process-transient stops.
        """
        with watch.lock:
            workdir = Path(self._host.workdir.path).resolve()
            try:
                renderer_rel = str(watch.renderer_path.relative_to(workdir))
            except ValueError:
                # Path not under the workdir (should not happen given
                # validation); fall back to the absolute path.
                renderer_rel = str(watch.renderer_path)
            payload = {
                "watch_id": watch.watch_id,
                "renderer_path": renderer_rel,
                "interval_s": watch.interval_s,
                "timeout_s": watch.timeout_s,
                "max_refreshes": watch.max_refreshes,
                "refreshes_used": watch.refreshes_used,
                "started_at": _utc_now_iso(),
            }
        atomic_write_json(self._watch_path, payload, fsync=True)

    def _clear_watch_descriptor(self) -> None:
        try:
            self._watch_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A stale descriptor is harmless; a later start/resume overwrites it.
            pass

    def resume_persisted_watch(self) -> dict[str, Any] | None:
        """Rehydrate the persisted watch after a process restart.

        Called from ``setup`` on every boot. If ``taskcard/watch.json`` exists
        and is valid, re-creates the watch (same id/params/refresh budget),
        writes the current renderer output to the body, marks ``active``, and
        spawns the updater thread. Returns the start-style payload on success
        or ``None`` when there is nothing to resume.

        A missing/corrupt descriptor, a renderer that no longer exists, an
        invalid path, or an already-exhausted refresh budget are treated as
        stale: the descriptor is cleared and the card left ``inactive`` so a
        boot never silently resurrects a dead watch.
        """
        with self._lock:
            if self._watch is not None:
                return None
        try:
            payload = read_json(self._watch_path, expect=dict)
        except (OSError, ValueError, TypeError):
            # Corrupt descriptor: the contract promises stale descriptors are
            # cleared on boot, not left to wedge every future boot.
            self._clear_watch_descriptor()
            return None
        if not payload:
            # Valid-JSON but empty (e.g. ``{}``): can never resume; clear it.
            self._clear_watch_descriptor()
            return None
        watch_id = payload.get("watch_id")
        if not isinstance(watch_id, str):
            self._clear_watch_descriptor()
            return None
        # Carry the watch-id counter before any stale-clear path: a discard
        # must not reset the counter and let a later start reuse ``tc_1``
        # (notification idempotency keys embed the watch id).
        with self._lock:
            try:
                counter = int(str(watch_id).split("_")[-1])
                self._counter = max(self._counter, counter)
            except (ValueError, IndexError):
                pass
        renderer_raw = payload.get("renderer_path")
        if not isinstance(renderer_raw, str):
            self._clear_watch_descriptor()
            return None
        try:
            renderer_path = self._validate_renderer_path(renderer_raw)
        except TaskCardError:
            self._clear_watch_descriptor()
            return None
        config = self._load_config()
        try:
            interval_s = self._coerce_positive(
                payload.get("interval_s", config.interval_s), "interval_s", _MIN_INTERVAL_S
            )
            requested_timeout = payload.get("timeout_s")
            if requested_timeout is None:
                timeout_s = config.timeout_s
            else:
                timeout_s = min(
                    self._coerce_positive(requested_timeout, "timeout_s", _MIN_TIMEOUT_S),
                    config.timeout_s,
                )
        except TaskCardError:
            # Non-numeric cadence/ceiling in the descriptor: treat as stale.
            self._clear_watch_descriptor()
            return None
        requested_max = payload.get("max_refreshes")
        if type(requested_max) is int and requested_max > 0:
            effective_max = min(requested_max, config.max_refreshes)
        else:
            effective_max = config.max_refreshes
        refreshes_used = payload.get("refreshes_used", 0)
        if type(refreshes_used) is not int or refreshes_used < 0:
            refreshes_used = 0
        if refreshes_used >= effective_max:
            # Budget already exhausted while away: retire the stale card.
            self._clear_watch_descriptor()
            return None
        watch = _Watch(
            watch_id,
            renderer_path,
            interval_s,
            timeout_s,
            effective_max,
        )
        watch.refreshes_used = refreshes_used
        with self._lock:
            self._watch = watch
        try:
            body = self._run_renderer(renderer_path, timeout_s)
            self._publish_active(body)
            self._clear_reminder()
        except Exception:
            # A transient renderer failure at boot must not kill the card:
            # keep the last body, mark active unconditionally (the watch IS
            # live and the returned payload says so), and let the thread retry.
            try:
                body = self._body_path.read_text(encoding="utf-8")
            except OSError:
                body = None
            try:
                self._write_status("active")
            except OSError:
                pass
            with watch.lock:
                watch.last_valid_body = body
                watch.last_valid_at = _utc_now_iso() if body is not None else None
        else:
            with watch.lock:
                watch.last_valid_body = body
                watch.last_valid_at = _utc_now_iso()
        self._spawn(watch)
        return {
            "status": "ok",
            "watch_id": watch.watch_id,
            "state": "watching",
            "resumed": True,
            **self._paths_payload(),
            **self._status_payload("active"),
            **self._refresh_fields(watch),
        }

    def _load_config(self) -> _Config:
        """Load this agent's persisted Task Card defaults/ceilings.

        Each field falls back to its own built-in default independently, so
        one invalid field never discards a valid sibling. A config file that
        has never been created (not merely empty/invalid) triggers one-time
        resolution against legacy state instead of the plain built-in
        defaults; that resolution is persisted unconditionally, so this
        branch is taken at most once per agent (see
        ``_migrate_legacy_config``). If the config file already exists but
        cannot be read as a JSON object — missing, malformed, undecodable, or
        the wrong top-level type — it remains the sole owner: this falls back
        to built-in defaults without ever consulting legacy state.
        """
        if not self._config_path.is_file():
            return self._migrate_legacy_config()
        return self._read_owner_config()

    def _read_owner_config(self) -> _Config:
        """Read the intrinsic owner document with the runtime's per-field fallbacks."""
        try:
            data = read_json(self._config_path, expect=dict)
        except (OSError, ValueError, TypeError):
            return _BUILTIN_CONFIG
        return _Config(
            self._config_number(data.get("interval_s"), _MIN_INTERVAL_S, _DEFAULT_INTERVAL_S),
            self._config_number(data.get("timeout_s"), _MIN_TIMEOUT_S, _DEFAULT_TIMEOUT_S),
            self._config_max_refreshes(data.get("max_refreshes")),
            self._config_reminder_turns(data.get("reminder_turns")),
            self._config_max_body_chars(data.get("max_body_chars")),
        )

    def _legacy_config(self) -> _Config:
        """Resolve the one-time legacy ceiling without writing intrinsic state."""
        try:
            legacy = read_json(self._legacy_config_path, expect=dict)
        except (OSError, ValueError, TypeError):
            legacy = None
        legacy_max = legacy.get("max_refreshes") if legacy is not None else None
        if (
            type(legacy_max) is not int
            or legacy_max <= 0
            or legacy_max == _LEGACY_UNTOUCHED_MAX_REFRESHES
        ):
            return _BUILTIN_CONFIG
        return _Config(
            _DEFAULT_INTERVAL_S,
            _DEFAULT_TIMEOUT_S,
            legacy_max,
            _DEFAULT_REMINDER_TURNS,
            _MAX_BODY_CHARS,
        )

    def settings_rows(self) -> tuple[SettingRow, ...]:
        """Return fresh five-field owner facts without triggering migration."""
        config = (
            self._read_owner_config()
            if self._config_path.is_file()
            else self._legacy_config()
        )
        return (
            SettingRow(
                "interval_s",
                config.interval_s,
                _DEFAULT_INTERVAL_S,
                True,
                "task_card-manual#interval-s",
            ),
            SettingRow(
                "timeout_s",
                config.timeout_s,
                _DEFAULT_TIMEOUT_S,
                True,
                "task_card-manual#timeout-s",
            ),
            SettingRow(
                "max_refreshes",
                config.max_refreshes,
                _DEFAULT_MAX_REFRESHES,
                True,
                "task_card-manual#max-refreshes",
            ),
            SettingRow(
                "reminder_turns",
                config.reminder_turns,
                _DEFAULT_REMINDER_TURNS,
                True,
                "task_card-manual#reminder-turns",
            ),
            SettingRow(
                "max_body_chars",
                config.max_body_chars,
                _MAX_BODY_CHARS,
                True,
                "task_card-manual#max-body-chars",
            ),
        )

    def _migrate_legacy_config(self) -> _Config:
        """One-time resolution against legacy state; always persisted.

        Reads ``<workdir>/telegram/taskcard.json`` (the retired Telegram
        controller's persisted ``max_refreshes`` fuse) only because this
        capability's own config file has never been created. A valid value
        that actually differs from Telegram's own untouched default is
        migrated into the resolved config; an absent, invalid, undecodable,
        or untouched-default legacy value resolves to the plain built-in
        defaults instead, exactly as if no legacy state existed — this is
        what keeps an ordinary ``/taskcard on|off|N`` user (who never
        customized the refresh ceiling, but whose settings file still
        carries that field at its own default) on the new built-in default
        instead of an incidental, non-chosen 1000.

        Either way, the resolved config is written into the intrinsic config
        file unconditionally. This is the only way that file is ever
        created, and creating it regardless of whether anything actually
        migrated is what makes the legacy read genuinely one-way and
        one-time: once it exists, ``_load_config`` never calls this method
        again for this agent, so a later change to the Telegram-owned file
        cannot alter intrinsic policy.
        """
        resolved = self._legacy_config()
        try:
            atomic_write_json(
                self._config_path,
                {
                    "interval_s": resolved.interval_s,
                    "timeout_s": resolved.timeout_s,
                    "max_refreshes": resolved.max_refreshes,
                },
                fsync=True,
            )
        except OSError:
            pass
        return resolved

    @staticmethod
    def _config_number(value: Any, minimum: float, default: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        numeric = float(value)
        return numeric if math.isfinite(numeric) and numeric >= minimum else default

    @staticmethod
    def _config_max_refreshes(value: Any) -> int:
        return value if type(value) is int and value > 0 else _DEFAULT_MAX_REFRESHES

    @staticmethod
    def _config_reminder_turns(value: Any) -> int:
        return value if type(value) is int and value > 0 else _DEFAULT_REMINDER_TURNS

    @staticmethod
    def _config_max_body_chars(value: Any) -> int:
        return value if type(value) is int and value >= 100 else _MAX_BODY_CHARS

    def _reminder_turns(self) -> int:
        try:
            return self._config_reminder_turns(read_json(self._config_path, expect=dict).get("reminder_turns"))
        except (OSError, ValueError, TypeError):
            return _DEFAULT_REMINDER_TURNS

    def _validate_renderer_path(self, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise TaskCardError("renderer_path is required for start")
        workdir = Path(self._host.workdir.path)
        candidate = raw.strip()
        try:
            wd = workdir.resolve()
            joined = Path(candidate) if Path(candidate).is_absolute() else (workdir / candidate)
            resolved = joined.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise TaskCardError(f"renderer_path could not be resolved ({exc})") from exc
        try:
            resolved.relative_to(wd)
        except ValueError as exc:
            raise TaskCardError(
                "renderer_path must be inside the agent working directory "
                "(no path traversal, no absolute escape)"
            ) from exc
        if not resolved.is_file():
            raise TaskCardError("renderer_path must be an existing regular file")
        return resolved

    @staticmethod
    def _coerce_positive(value: Any, name: str, minimum: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TaskCardError(f"{name} must be a number")
        numeric = float(value)
        if numeric < minimum:
            raise TaskCardError(f"{name} must be at least {minimum}")
        return numeric

    @staticmethod
    def _refresh_fields(watch: _Watch) -> dict[str, Any]:
        with watch.lock:
            used = watch.refreshes_used
            maximum = watch.max_refreshes
            reason = watch.stop_reason
        return {
            "refreshes_used": used,
            "max_refreshes": maximum,
            "refreshes_remaining": max(0, maximum - used),
            "stop_reason": reason,
        }

    def _paths_payload(self) -> dict[str, str]:
        return {
            "taskcard_dir": str(self._taskcard_dir),
            "status_path": str(self._status_path),
            "body_path": str(self._body_path),
            "watch_path": str(self._watch_path),
        }

    @staticmethod
    def _status_payload(status_value: str) -> dict[str, str]:
        return {"status_value": status_value}


def _build_family(
    host: "ToolPluginHost | None",
    manager: TaskCardManager | None,
) -> ToolFamily:
    """Compose the declaration-derived family for schema or live dispatch."""
    if manager is None:
        def _unused(_input: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("the schema-only Task Card family never dispatches")

        handlers: dict[str, Any] = {action: _unused for action in DECLARATION.actions}
        manual_child = ChildTool(
            "manual", DECLARATION.manual_input_schema, _unused, title="manual input"
        )
    else:
        handlers = {
            "start": manager._start_child,
            "inspect": manager._inspect_child,
            "retry": manager._retry_child,
            "stop": manager._stop_child,
            "remove": manager._remove_child,
        }
        assert host is not None
        manual_child = build_manual_child(host.workdir, DECLARATION.manual)
    return ToolFamily(
        DECLARATION.name,
        [
            *(
                ChildTool(
                    action,
                    DECLARATION.input_schemas[action],
                    handlers[action],
                    title=f"{action} input",
                )
                for action in DECLARATION.actions
            ),
            manual_child,
        ],
        settings_provider=manager.settings_rows if manager is not None else tuple,
    )


def _activate(manager: TaskCardManager, host: "ToolPluginHost") -> None:
    """Resume the actual current-Agent watch after the official bind succeeds."""
    try:
        manager.resume_persisted_watch()
    except Exception as exc:
        host.task_card_lifecycle.report_resume_failure(str(exc))


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Bind one Task Card family without receiving or mounting an Agent."""
    lifecycle = host.task_card_lifecycle
    manager = lifecycle.current_manager()
    if not isinstance(manager, TaskCardManager):
        manager = TaskCardManager(host)
        lifecycle.retain_manager(manager)
    else:
        manager.rebind(host)
    _build_family(host, manager)  # fail closed on malformed live composition
    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=manager.handle,
        description=get_description(),
        glossary_package=None,
        activate=lambda: _activate(manager, host),
    )


DECLARATION = ToolPluginDeclaration(
    name="task_card",
    actions=("start", "inspect", "retry", "stop", "remove"),
    input_schemas=_DECLARED_INPUT_SCHEMAS,
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="task_card",
    description=_DESCRIPTION,
    binder=_bind,
    # Every requirement is exercised by the current lifecycle: workdir-owned
    # artifacts/manual, shutdown polling, the retained manager, and its notices.
    requires=("workdir", "shutdown", "task_card_lifecycle", "task_card_notifications"),
    settings=True,
)

# Retain the producer's existing notification channel, derived from the one
# declaration that owns the public family name.
notifications.register_notification_channel(DECLARATION.name)
# Declaration-derived schema surface, constructed without an Agent at import.
_FAMILY = _build_family(None, None)


def setup(agent: Any, **_ignored: Any) -> TaskCardManager:
    """Register Task Card through the declared host-plugin route only."""
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    register_agent_tool_plugins(agent, [DECLARATION])
    # Preserve the established setup return value without handing the Agent to
    # the binder: the lifecycle port already retained this exact manager.
    manager = getattr(agent, "_task_card_manager", None)
    if not isinstance(manager, TaskCardManager):  # pragma: no cover - host wiring defect
        raise RuntimeError("task_card declaration did not retain its lifecycle manager")
    return manager
