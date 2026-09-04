"""ACP v1 newline-delimited JSON-RPC adapter over local stdio."""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, TextIO
from uuid import uuid4

from lingtai.kernel.turns import TurnHandle, TurnOutcome
from lingtai.kernel.execution_workspace import ExecutionWorkspace
from lingtai.kernel.turn_events import (
    ToolLifecycleEvent,
    ToolLifecycleState,
)
from lingtai.kernel.turn_permissions import (
    PermissionDecision,
    ToolPermissionRequest,
)
from lingtai.services.session_mcp import StdioMCPServerConfig


JSONRPC_VERSION = "2.0"
ACP_PROTOCOL_VERSION = 1
_REQUEST_ID_MIN = -(1 << 63)
_REQUEST_ID_MAX = (1 << 63) - 1

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32011
SESSION_NOT_FOUND = -32001
SESSION_BUSY = -32010
UNSUPPORTED = -32004


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class _ActivePrompt:
    request_id: str | int | None
    session_id: str
    handle: TurnHandle
    generation: int
    thread: threading.Thread | None = None
    terminal_claimed: bool = False


@dataclass(frozen=True, slots=True)
class _OutboundBatch:
    """One FIFO output unit tied to the generation that accepted it."""

    generation: int
    wires: tuple[str, ...]
    active: _ActivePrompt | None = None
    settles_active: bool = False
    permission: _PendingPermission | None = None


@dataclass(slots=True)
class _PendingPermission:
    request_id: str
    active: _ActivePrompt
    generation: int
    event: threading.Event
    observer: _PromptToolObserver
    tool_call_id: str
    publication_lock: threading.Lock = field(default_factory=threading.Lock)
    published: bool = False
    decision: PermissionDecision | None = None


class _PromptToolObserver:
    """Project one Core turn's bounded tool facts onto ACP v1 updates."""

    def __init__(
        self,
        server: AcpStdioServer,
        session_id: str,
        correlation_id: str,
        generation: int,
    ) -> None:
        self._server = server
        self._session_id = session_id
        self._correlation_id = correlation_id
        self._generation = generation
        self._active: _ActivePrompt | None = None
        self._announced: set[str] = set()
        self._started: set[str] = set()
        self._terminal: set[str] = set()
        self._publication_locks: dict[str, threading.Lock] = {}

    def bind_active(self, active: _ActivePrompt) -> None:
        self._active = active

    def _track_permission(
        self, tool_call_id: str, publication_lock: threading.Lock
    ) -> None:
        """Associate lifecycle with a pending wire publication.

        Caller owns the server state lock. Lifecycle acquires this per-call lock
        before the state lock, matching the writer and response paths.
        """

        self._publication_locks[tool_call_id] = publication_lock

    def _mark_announced(
        self, tool_call_id: str, *, terminal: bool = False
    ) -> None:
        """Record a physically flushed initial record and optional terminal."""

        self._announced.add(tool_call_id)
        if terminal:
            self._terminal.add(tool_call_id)
            self._publication_locks.pop(tool_call_id, None)

    def on_tool_lifecycle(self, event: ToolLifecycleEvent) -> None:
        server = self._server
        tool_call_id = f"{self._correlation_id}:{event.tool_call_id}"
        with server._state_lock:
            publication_lock = self._publication_locks.get(tool_call_id)
        if publication_lock is not None and not publication_lock.acquire(
            blocking=False
        ):
            # A timeout/cancel can produce DENIED while client stdout is still
            # blocked in the initial permission batch. Lifecycle observation is
            # fail-open: suppress rather than pin the Core tool thread behind
            # an unbounded client write or emit an orphan update.
            with server._state_lock:
                self._publication_locks.pop(tool_call_id, None)
            return
        try:
            with server._state_lock:
                active = self._active
                if (
                    active is None
                    or server._active is not active
                    or active.terminal_claimed
                    or server._closing
                    or server._generation != self._generation
                ):
                    self._publication_locks.pop(tool_call_id, None)
                    return
                if tool_call_id in self._terminal:
                    return
                if event.state is ToolLifecycleState.STARTED:
                    if tool_call_id in self._started:
                        return
                    self._started.add(tool_call_id)
                    self._publication_locks.pop(tool_call_id, None)
                    if tool_call_id in self._announced:
                        update = {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": tool_call_id,
                            "status": "in_progress",
                        }
                    else:
                        self._announced.add(tool_call_id)
                        update = {
                            "sessionUpdate": "tool_call",
                            "toolCallId": tool_call_id,
                            "title": event.tool_name,
                            "status": "in_progress",
                        }
                else:
                    failed = event.state in {
                        ToolLifecycleState.FAILED,
                        ToolLifecycleState.DENIED,
                    }
                    status = "failed" if failed else "completed"
                    self._terminal.add(tool_call_id)
                    self._publication_locks.pop(tool_call_id, None)
                    if tool_call_id in self._announced:
                        update = {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": tool_call_id,
                            "status": status,
                        }
                    else:
                        self._announced.add(tool_call_id)
                        update = {
                            "sessionUpdate": "tool_call",
                            "toolCallId": tool_call_id,
                            "title": event.tool_name,
                            "status": status,
                        }
                server._enqueue_messages(
                    ({
                        "jsonrpc": JSONRPC_VERSION,
                        "method": "session/update",
                        "params": {
                            "sessionId": self._session_id,
                            "update": update,
                        },
                    },),
                    generation=self._generation,
                    active=active,
                )
        finally:
            if publication_lock is not None:
                publication_lock.release()

    def request_permission(
        self, request: ToolPermissionRequest
    ) -> PermissionDecision:
        return self._server._request_tool_permission(
            observer=self,
            tool_call_id=f"{self._correlation_id}:{request.tool_call_id}",
            tool_name=request.tool_name,
            generation=self._generation,
        )


def _package_version() -> str:
    try:
        return version("lingtai")
    except PackageNotFoundError:  # source-tree tests without installed metadata
        return "0+unknown"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


class AcpStdioServer:
    """One-process, one-session ACP v1 driving adapter.

    The reader remains live while a prompt worker waits on the Core TurnHandle,
    so ``session/cancel`` can request cooperative cancellation of that exact
    turn. Serialized output crosses one bounded FIFO queue; only its disposable
    daemon writer may touch the potentially blocking client stream.
    """

    _OUTBOUND_QUEUE_BATCHES = 64
    _PERMISSION_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        agent,
        input_stream: TextIO,
        output_stream: TextIO,
        *,
        fixed_execution_workspace: ExecutionWorkspace | None = None,
        allow_session_mcp: bool = True,
        session_mcp_validator: Callable[[Any], tuple[StdioMCPServerConfig, ...]] | None = None,
    ):
        self._agent = agent
        self._input = input_stream
        self._output = output_stream
        self._state_lock = threading.RLock()
        self._initialized = False
        self._session_id: str | None = None
        self._session_pending = False
        self._execution_workspace: ExecutionWorkspace | None = None
        self._fixed_execution_workspace = fixed_execution_workspace
        self._allow_session_mcp = allow_session_mcp
        self._session_mcp_validator = session_mcp_validator
        self._session_mcp_lease = None
        self._active: _ActivePrompt | None = None
        self._closing = False
        self._aborted = False
        self._generation = 0
        self._prompt_threads: set[threading.Thread] = set()
        self._permission_counter = 0
        self._pending_permissions: dict[str, _PendingPermission] = {}
        self._outbound: queue.Queue[_OutboundBatch] = queue.Queue(
            maxsize=self._OUTBOUND_QUEUE_BATCHES
        )
        self._writer = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="acp-stdio-writer",
        )
        self._writer.start()

    def serve(self) -> None:
        """Read frames until EOF, close, interrupt, or Agent shutdown.

        Text streams can block indefinitely in ``readline``. A daemon reader owns
        that blocking edge while this coordinator polls the Agent shutdown latch,
        so refresh/stop can release the workdir lease without waiting for a client
        to close stdin. The reader never parses or writes protocol frames.
        """

        incoming: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=64)

        def _read_input() -> None:
            try:
                for raw_line in self._input:
                    incoming.put(("line", raw_line))
            except BaseException as exc:
                incoming.put(("error", exc))
            finally:
                incoming.put(("eof", None))

        reader = threading.Thread(
            target=_read_input,
            daemon=True,
            name="acp-stdio-reader",
        )
        reader.start()
        try:
            while True:
                with self._state_lock:
                    if self._closing:
                        break
                shutdown = getattr(self._agent, "_shutdown", None)
                if shutdown is not None and shutdown.is_set():
                    break
                try:
                    kind, payload = incoming.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "eof":
                    break
                if kind == "error":
                    if isinstance(payload, UnicodeError):
                        self._write_error(None, PARSE_ERROR, "Parse error")
                        break
                    raise payload

                raw_line = payload
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                try:
                    message = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError):
                    self._write_error(None, PARSE_ERROR, "Parse error")
                    continue
                self._dispatch(message)
        finally:
            self.close()

    def close(self) -> None:
        """Invalidate queued output, stop accepting work, and cancel the prompt.

        The writer is deliberately never joined: an OS-level stdout write may be
        stuck forever after the client stops reading. Generation invalidation
        makes every batch that has not crossed the writer's start check
        disposable while Agent/lease teardown proceeds independently.
        """

        with self._state_lock:
            if self._closing:
                return
            self._closing = True
            self._generation += 1
            active = self._active
            self._active = None
            pending = self._drain_permissions_locked()
        self._wake_permissions(pending)
        if active is not None:
            active.handle.cancel()
        lease = self._session_mcp_lease
        self._session_mcp_lease = None
        if lease is not None:
            lease.close()

    def _abort_transport(self) -> None:
        """Fail every queued batch closed after a fatal framing/write failure."""

        with self._state_lock:
            if self._aborted:
                return
            self._aborted = True
            self._closing = True
            self._generation += 1
            active = self._active
            self._active = None
            pending = self._drain_permissions_locked()
        self._wake_permissions(pending)
        if active is not None:
            active.handle.cancel()
        lease = self._session_mcp_lease
        self._session_mcp_lease = None
        if lease is not None:
            lease.close()

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            self._write_error(None, INVALID_REQUEST, "Invalid Request")
            return

        if "method" not in message and self._dispatch_permission_response(message):
            return

        has_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        if (
            message.get("jsonrpc") != JSONRPC_VERSION
            or not isinstance(method, str)
            or (has_id and not self._valid_id(request_id))
        ):
            self._write_error(
                request_id if self._valid_id(request_id) else None,
                INVALID_REQUEST,
                "Invalid Request",
            )
            return

        params = message.get("params", {})
        try:
            if method in {"initialize", "session/new", "session/prompt"} and not has_id:
                raise _RpcError(INVALID_REQUEST, f"{method} must be a request")
            if method == "session/cancel" and has_id:
                raise _RpcError(INVALID_REQUEST, "session/cancel must be a notification")

            if method == "initialize":
                result = self._initialize(params)
            elif method == "session/new":
                self._require_initialized()
                result = self._new_session(params)
            elif method == "session/prompt":
                self._require_initialized()
                self._prompt(params, request_id)
                return
            elif method == "session/cancel":
                self._require_initialized()
                result = self._cancel(params)
            else:
                raise _RpcError(METHOD_NOT_FOUND, "Method not found")
        except _RpcError as exc:
            if has_id:
                self._write_error(request_id, exc.code, exc.message)
            return
        except Exception:
            if has_id:
                self._write_error(request_id, INTERNAL_ERROR, "Internal error")
            return

        if has_id:
            self._write_result(request_id, result)

    @staticmethod
    def _valid_id(value: Any) -> bool:
        # ACP v1 permits string, signed-int64, or explicit null request ids.
        # Reject bool (an int subclass), fractional numbers, and out-of-range ints.
        return (
            value is None
            or isinstance(value, str)
            or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and _REQUEST_ID_MIN <= value <= _REQUEST_ID_MAX
            )
        )

    @staticmethod
    def _params_object(params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _RpcError(INVALID_PARAMS, "params must be an object")
        return params

    def _initialize(self, params: Any) -> dict[str, Any]:
        params = self._params_object(params)
        protocol_version = params.get("protocolVersion")
        if not isinstance(protocol_version, int) or isinstance(protocol_version, bool):
            raise _RpcError(INVALID_PARAMS, "protocolVersion must be an integer")
        with self._state_lock:
            self._initialized = True
        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "agentCapabilities": {},
            "agentInfo": {
                "name": "lingtai",
                "title": "LingTai",
                "version": _package_version(),
            },
            "authMethods": [],
        }

    def _require_initialized(self) -> None:
        with self._state_lock:
            initialized = self._initialized
        if not initialized:
            raise _RpcError(SERVER_NOT_INITIALIZED, "server is not initialized")

    @staticmethod
    def _stdio_mcp_configs(value: Any) -> tuple[StdioMCPServerConfig, ...]:
        if not isinstance(value, list):
            raise _RpcError(INVALID_PARAMS, "mcpServers must be an array")
        configs: list[StdioMCPServerConfig] = []
        names: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise _RpcError(INVALID_PARAMS, "mcpServers entries must be objects")
            if "type" in item:
                raise _RpcError(INVALID_PARAMS, "only stdio MCP servers are supported")
            if set(item) - {"name", "command", "args", "env", "_meta"}:
                raise _RpcError(INVALID_PARAMS, "unknown stdio MCP server field")
            name = item.get("name")
            command = item.get("command")
            args = item.get("args")
            env = item.get("env")
            meta = item.get("_meta")
            if not isinstance(name, str) or not name:
                raise _RpcError(INVALID_PARAMS, "MCP server name must be non-empty")
            if name in names:
                raise _RpcError(INVALID_PARAMS, "duplicate MCP server name")
            if (
                not isinstance(command, str)
                or not command
                or not Path(command).is_absolute()
            ):
                raise _RpcError(INVALID_PARAMS, "MCP command must be an absolute path")
            if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
                raise _RpcError(INVALID_PARAMS, "MCP args must be an array of strings")
            if not isinstance(env, list):
                raise _RpcError(INVALID_PARAMS, "MCP env must be an array")
            if meta is not None and not isinstance(meta, dict):
                raise _RpcError(INVALID_PARAMS, "MCP _meta must be an object or null")
            env_pairs: list[tuple[str, str]] = []
            env_names: set[str] = set()
            for variable in env:
                if not isinstance(variable, dict) or set(variable) - {"name", "value", "_meta"}:
                    raise _RpcError(INVALID_PARAMS, "MCP env entries must be name/value objects")
                key = variable.get("name")
                val = variable.get("value")
                var_meta = variable.get("_meta")
                if not isinstance(key, str) or not key or not isinstance(val, str):
                    raise _RpcError(INVALID_PARAMS, "MCP env names/values must be strings")
                if key in env_names:
                    raise _RpcError(INVALID_PARAMS, "duplicate MCP environment name")
                if var_meta is not None and not isinstance(var_meta, dict):
                    raise _RpcError(INVALID_PARAMS, "MCP env _meta must be an object or null")
                env_names.add(key)
                env_pairs.append((key, val))
            names.add(name)
            configs.append(StdioMCPServerConfig(name, command, tuple(args), tuple(env_pairs)))
        return tuple(configs)

    def _new_session(self, params: Any) -> dict[str, str]:
        params = self._params_object(params)
        cwd = params.get("cwd")
        mcp_servers = params.get("mcpServers")
        if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
            raise _RpcError(INVALID_PARAMS, "cwd must be an absolute path")
        try:
            resolved_cwd = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _RpcError(INVALID_PARAMS, "cwd must exist") from exc
        if not resolved_cwd.is_dir():
            raise _RpcError(INVALID_PARAMS, "cwd must be a directory")
        if self._fixed_execution_workspace is not None:
            if resolved_cwd != self._fixed_execution_workspace.root:
                raise _RpcError(
                    INVALID_PARAMS,
                    "cwd must match the profile's fixed execution workspace",
                )
        if self._session_mcp_validator is not None:
            configs = self._session_mcp_validator(mcp_servers)
        elif not self._allow_session_mcp:
            if mcp_servers != []:
                raise _RpcError(
                    INVALID_PARAMS,
                    "mcpServers must be an empty array for this profile",
                )
            configs: tuple[StdioMCPServerConfig, ...] = ()
        else:
            configs = self._stdio_mcp_configs(mcp_servers)
        additional_directories = params.get("additionalDirectories")
        if additional_directories not in (None, []):
            raise _RpcError(
                UNSUPPORTED,
                "additionalDirectories are not supported by this local adapter",
            )
        with self._state_lock:
            if self._closing:
                raise _RpcError(UNSUPPORTED, "adapter is closing")
            if self._session_id is not None or self._session_pending:
                raise _RpcError(
                    UNSUPPORTED,
                    "this local adapter supports one session per process",
                )
            self._session_pending = True

        lease = None
        try:
            try:
                lease = self._agent.mount_session_mcp_stdio(configs) if configs else None
            except ValueError as exc:
                raise _RpcError(INVALID_PARAMS, str(exc)) from exc
            except Exception as exc:
                raise _RpcError(INTERNAL_ERROR, "session MCP startup failed") from exc

            workspace = self._fixed_execution_workspace or ExecutionWorkspace(resolved_cwd)
            with self._state_lock:
                if self._closing:
                    raise _RpcError(UNSUPPORTED, "adapter is closing")
                self._execution_workspace = workspace
                self._session_mcp_lease = lease
                self._session_id = f"session_{uuid4().hex}"
                return {"sessionId": self._session_id}
        except Exception:
            if lease is not None:
                lease.close()
            raise
        finally:
            with self._state_lock:
                self._session_pending = False

    def _validate_session(self, params: dict[str, Any]) -> str:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise _RpcError(INVALID_PARAMS, "sessionId must be a non-empty string")
        with self._state_lock:
            expected = self._session_id
        if expected is None or session_id != expected:
            raise _RpcError(SESSION_NOT_FOUND, "session not found")
        return session_id

    def _prompt(self, params: Any, request_id: str | int | None) -> None:
        params = self._params_object(params)
        session_id = self._validate_session(params)
        prompt = params.get("prompt")
        if not isinstance(prompt, list) or not prompt:
            raise _RpcError(INVALID_PARAMS, "prompt must be a non-empty array")
        parts: list[str] = []
        for block in prompt:
            if not isinstance(block, dict):
                raise _RpcError(INVALID_PARAMS, "prompt blocks must be objects")
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise _RpcError(INVALID_PARAMS, "Text block text must be a string")
                parts.append(text)
                continue
            if block_type == "resource_link":
                uri = block.get("uri")
                name = block.get("name")
                if not isinstance(uri, str) or not uri:
                    raise _RpcError(
                        INVALID_PARAMS,
                        "ResourceLink uri must be a non-empty string",
                    )
                if not isinstance(name, str) or not name:
                    raise _RpcError(
                        INVALID_PARAMS,
                        "ResourceLink name must be a non-empty string",
                    )
                projected: dict[str, Any] = {
                    "type": "resource_link",
                    "uri": uri,
                    "name": name,
                }
                for field_name in ("mimeType", "title", "description"):
                    value = block.get(field_name)
                    if value is not None:
                        if not isinstance(value, str):
                            raise _RpcError(
                                INVALID_PARAMS,
                                f"ResourceLink {field_name} must be a string",
                            )
                        projected[field_name] = value
                size = block.get("size")
                if size is not None:
                    if (
                        not isinstance(size, int)
                        or isinstance(size, bool)
                        or size < 0
                    ):
                        raise _RpcError(
                            INVALID_PARAMS,
                            "ResourceLink size must be a non-negative integer",
                        )
                    projected["size"] = size
                parts.append(
                    "\n\n"
                    + json.dumps(
                        projected,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n\n"
                )
                continue
            raise _RpcError(
                UNSUPPORTED,
                "this adapter accepts Text and ResourceLink prompt blocks only",
            )
        content = "".join(parts)
        if not content:
            raise _RpcError(INVALID_PARAMS, "prompt content must not be empty")

        with self._state_lock:
            if self._closing:
                raise _RpcError(INTERNAL_ERROR, "server is closing")
            if self._active is not None:
                raise _RpcError(SESSION_BUSY, "session already has an active prompt")
            correlation_id = f"acp_{uuid4().hex}"
            generation = self._generation
            observer = _PromptToolObserver(
                self,
                session_id,
                correlation_id,
                generation,
            )
            try:
                from lingtai.kernel.turns import TurnOrigin

                handle = self._agent.submit_turn(
                    content,
                    sender="user",
                    correlation_id=correlation_id,
                    execution_workspace=self._execution_workspace,
                    tool_observer=observer,
                    permission_broker=observer,
                    origin=TurnOrigin.AUTHENTICATED_ADAPTER,
                )
            except (TypeError, ValueError) as exc:
                raise _RpcError(INVALID_PARAMS, str(exc)) from exc
            except RuntimeError as exc:
                raise _RpcError(INTERNAL_ERROR, "agent cannot accept the turn") from exc
            active = _ActivePrompt(request_id, session_id, handle, generation)
            observer.bind_active(active)
            worker = threading.Thread(
                target=self._await_prompt,
                args=(active,),
                daemon=True,
                name=f"acp-prompt-{handle.correlation_id[-12:]}",
            )
            active.thread = worker
            self._active = active
            self._prompt_threads.add(worker)
            try:
                worker.start()
            except BaseException:
                self._active = None
                self._prompt_threads.discard(worker)
                handle.cancel()
                raise

    def _cancel(self, params: Any) -> None:
        params = self._params_object(params)
        session_id = self._validate_session(params)
        with self._state_lock:
            active = self._active
            if active is not None and active.session_id == session_id:
                # Keep adapter settlement ordering linear: the prompt worker also
                # takes this lock before emitting its terminal response, so a
                # received cancel reaches the Core handle before that response.
                pending = self._drain_permissions_locked(active=active)
                self._wake_permissions(pending)
                active.handle.cancel()
        return None

    def _drain_permissions_locked(
        self, *, active: _ActivePrompt | None = None
    ) -> list[_PendingPermission]:
        drained = []
        for request_id, pending in list(self._pending_permissions.items()):
            if active is None or pending.active is active:
                self._pending_permissions.pop(request_id, None)
                pending.decision = PermissionDecision.DENY
                drained.append(pending)
        return drained

    @staticmethod
    def _wake_permissions(pending: list[_PendingPermission]) -> None:
        for entry in pending:
            entry.event.set()

    def _dispatch_permission_response(self, message: dict[str, Any]) -> bool:
        request_id = message.get("id")
        if not self._valid_id(request_id):
            return False
        with self._state_lock:
            pending = self._pending_permissions.get(request_id)
            if pending is None:
                # Unknown, duplicate, and late response objects are ignored, but
                # a method-less object without a result/error remains an invalid
                # client request and must follow the ordinary JSON-RPC error path.
                return "result" in message or "error" in message
            # Capture arrival under the same state lock that the writer uses to
            # publish the bit after a successful request-frame flush. If this
            # response entered before that boundary it remains DENY even when it
            # is descheduled until after publication or waits on the wire lock.
            arrived_before_publish = not pending.published

        with pending.publication_lock:
            with self._state_lock:
                if self._pending_permissions.get(request_id) is not pending:
                    return True
                decision = PermissionDecision.DENY
                if not arrived_before_publish and pending.published and (
                    message.get("jsonrpc") == JSONRPC_VERSION
                    and set(message) == {"jsonrpc", "id", "result"}
                    and isinstance(message.get("result"), dict)
                ):
                    result = message["result"]
                    result_fields = set(result) - {"_meta"}
                    meta = result.get("_meta")
                    meta_valid = (
                        "_meta" not in result
                        or meta is None
                        or isinstance(meta, dict)
                    )
                    if (
                        meta_valid
                        and result_fields == {"outcome"}
                        and isinstance(result.get("outcome"), dict)
                    ):
                        outcome = result["outcome"]
                        outcome_fields = set(outcome) - {"_meta"}
                        outcome_meta = outcome.get("_meta")
                        outcome_meta_valid = (
                            "_meta" not in outcome
                            or outcome_meta is None
                            or isinstance(outcome_meta, dict)
                        )
                        if (
                            outcome_meta_valid
                            and outcome_fields == {"outcome", "optionId"}
                            and outcome.get("outcome") == "selected"
                            and outcome.get("optionId") == "allow_once"
                        ):
                            decision = PermissionDecision.ALLOW
                self._pending_permissions.pop(request_id, None)
                pending.decision = decision
        pending.event.set()
        return True

    def _request_tool_permission(
        self,
        *,
        observer: _PromptToolObserver,
        tool_call_id: str,
        tool_name: str,
        generation: int,
    ) -> PermissionDecision:
        with self._state_lock:
            active = observer._active
            if (
                active is None
                or self._active is not active
                or active.terminal_claimed
                or (
                    callable(getattr(active.handle, "cancel_requested", None))
                    and active.handle.cancel_requested()
                )
                or self._closing
                or generation != self._generation
            ):
                return PermissionDecision.DENY
            self._permission_counter += 1
            request_id = f"lingtai-permission-{self._permission_counter}"
            pending = _PendingPermission(
                request_id=request_id,
                active=active,
                generation=generation,
                event=threading.Event(),
                observer=observer,
                tool_call_id=tool_call_id,
            )
            observer._track_permission(tool_call_id, pending.publication_lock)

        tool_call = {
            "toolCallId": tool_call_id,
            "title": tool_name,
            "status": "pending",
        }
        update = {"sessionUpdate": "tool_call", **tool_call}
        messages = ({
            "jsonrpc": JSONRPC_VERSION,
            "method": "session/update",
            "params": {"sessionId": active.session_id, "update": update},
        }, {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": active.session_id,
                "toolCall": tool_call,
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        })
        accepted = self._enqueue_messages(
            messages,
            generation=generation,
            active=active,
            permission=pending,
        )
        if not accepted:
            return PermissionDecision.DENY
        pending.event.wait(timeout=self._PERMISSION_TIMEOUT_SECONDS)
        with self._state_lock:
            if self._pending_permissions.get(request_id) is pending:
                self._pending_permissions.pop(request_id, None)
                pending.decision = PermissionDecision.DENY
            return pending.decision or PermissionDecision.DENY

    def _await_prompt(self, active: _ActivePrompt) -> None:
        accepted = False
        try:
            result = active.handle.result()
        except Exception:
            result = None
        try:
            with self._state_lock:
                if self._active is not active:
                    return
                if self._closing or self._shutdown_requested():
                    self._active = None
                    return
                # Close/cancel and terminal ownership linearize under one lock.
                # The captured generation is re-checked when the atomic terminal
                # batch enters the queue and before each physical write begins.
                active.terminal_claimed = True
                generation = active.generation

            messages: list[dict[str, Any]] = []
            if result is None or result.outcome is TurnOutcome.FAILED:
                messages.append({
                    "jsonrpc": JSONRPC_VERSION,
                    "id": active.request_id,
                    "error": {
                        "code": INTERNAL_ERROR,
                        "message": "LingTai turn failed",
                    },
                })
            elif result.outcome is TurnOutcome.CANCELLED:
                messages.append({
                    "jsonrpc": JSONRPC_VERSION,
                    "id": active.request_id,
                    "result": {"stopReason": "cancelled"},
                })
            else:
                if result.text:
                    messages.append({
                        "jsonrpc": JSONRPC_VERSION,
                        "method": "session/update",
                        "params": {
                            "sessionId": active.session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": result.text,
                                },
                            },
                        },
                    })
                messages.append({
                    "jsonrpc": JSONRPC_VERSION,
                    "id": active.request_id,
                    "result": {"stopReason": "end_turn"},
                })
            accepted = self._enqueue_messages(
                messages,
                generation=generation,
                active=active,
                settles_active=True,
            )
        except Exception:
            # Serialization is deterministic for the fixed shapes above. If an
            # injected value still surprises us, fail the whole transport closed
            # without a fallback frame or a worker traceback.
            self.close()
        finally:
            thread = threading.current_thread()
            with self._state_lock:
                if not accepted and self._active is active:
                    self._active = None
                self._prompt_threads.discard(thread)

    def _shutdown_requested(self) -> bool:
        shutdown = getattr(self._agent, "_shutdown", None)
        return shutdown is not None and shutdown.is_set()

    def _write_result(self, request_id: Any, result: Any) -> bool:
        return self._enqueue_messages(({
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": result,
        },))

    def _write_error(self, request_id: Any, code: int, message: str) -> bool:
        return self._enqueue_messages(({
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        },))

    def _write_notification(self, method: str, params: dict[str, Any]) -> bool:
        return self._enqueue_messages(({
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params,
        },))

    def _enqueue_messages(
        self,
        messages,
        *,
        generation: int | None = None,
        active: _ActivePrompt | None = None,
        settles_active: bool = False,
        permission: _PendingPermission | None = None,
    ) -> bool:
        try:
            wires: list[str] = []
            for message in messages:
                wire = json.dumps(
                    message,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                if "\n" in wire or "\r" in wire:
                    raise ValueError("JSON-RPC frame contains an embedded newline")
                wires.append(wire + "\n")
        except Exception:
            self._abort_transport()
            return False

        active_to_cancel = None
        accepted = True
        with self._state_lock:
            if self._closing or self._shutdown_requested():
                return False
            if generation is None:
                generation = self._generation
            elif generation != self._generation:
                return False
            if permission is not None:
                if (
                    permission.active is not active
                    or permission.generation != generation
                    or self._active is not active
                    or active is None
                    or active.terminal_claimed
                    or (
                        callable(getattr(active.handle, "cancel_requested", None))
                        and active.handle.cancel_requested()
                    )
                ):
                    return False
                self._pending_permissions[permission.request_id] = permission
            try:
                self._outbound.put_nowait(
                    _OutboundBatch(
                        generation,
                        tuple(wires),
                        active,
                        settles_active,
                        permission,
                    )
                )
            except queue.Full:
                # A non-reading client may never release the writer. Bound memory
                # and fail the whole transport closed rather than blocking the
                # coordinator or leaking a partial prompt terminal batch.
                accepted = False
                self._aborted = True
                self._closing = True
                self._generation += 1
                active_to_cancel = self._active
                self._active = None
                pending = self._drain_permissions_locked()
            else:
                pending = []
        self._wake_permissions(pending)
        if active_to_cancel is not None:
            active_to_cancel.handle.cancel()
        return accepted

    def _writer_loop(self) -> None:
        """Serialize queued batches without ever becoming teardown authority."""

        while True:
            try:
                batch = self._outbound.get(timeout=0.1)
            except queue.Empty:
                with self._state_lock:
                    if self._closing:
                        return
                continue
            try:
                for wire_index, wire in enumerate(batch.wires):
                    publication_lock = (
                        batch.permission.publication_lock
                        if batch.permission is not None
                        else None
                    )
                    if publication_lock is not None:
                        publication_lock.acquire()
                    try:
                        with self._state_lock:
                            if self._aborted or (
                                batch.active is not None
                                and (
                                    self._closing
                                    or batch.generation != self._generation
                                    or self._shutdown_requested()
                                )
                            ) or (
                                batch.permission is not None
                                and self._pending_permissions.get(
                                    batch.permission.request_id
                                ) is not batch.permission
                            ):
                                break
                            # This state check is the start linearization point.
                            # Close can invalidate every frame that has not crossed
                            # it, but no Python API can revoke an OS write already
                            # in progress. Permission publication uses its own lock
                            # so global teardown never waits on client stdout.
                        written = self._output.write(wire)
                        if written != len(wire):
                            raise OSError("short ACP stdout write")
                        self._output.flush()
                        deferred_terminal_wire = None
                        if batch.permission is not None:
                            with self._state_lock:
                                registered = self._pending_permissions.get(
                                    batch.permission.request_id
                                ) is batch.permission
                                if wire_index == len(batch.wires) - 1 and registered:
                                    # Publish under the same state lock used when
                                    # a response captures its arrival. The flush
                                    # has completed, and the wire lock stays held.
                                    batch.permission.published = True
                                if wire_index == 0:
                                    # Cancel/timeout may drain and deny while this
                                    # already-started pending write is blocked.
                                    # Once the frame physically lands it needs an
                                    # adjacent terminal even though the request
                                    # frame will be suppressed.
                                    close_started_record = (
                                        not registered
                                        and batch.permission.decision
                                        is PermissionDecision.DENY
                                        and not self._aborted
                                        and not self._closing
                                        and batch.permission.generation
                                        == self._generation
                                        and self._active
                                        is batch.permission.active
                                    )
                                    batch.permission.observer._mark_announced(
                                        batch.permission.tool_call_id,
                                        terminal=close_started_record,
                                    )
                                    if close_started_record:
                                        deferred_terminal_wire = json.dumps(
                                            {
                                                "jsonrpc": JSONRPC_VERSION,
                                                "method": "session/update",
                                                "params": {
                                                    "sessionId": batch.permission.active.session_id,
                                                    "update": {
                                                        "sessionUpdate": "tool_call_update",
                                                        "toolCallId": batch.permission.tool_call_id,
                                                        "status": "failed",
                                                    },
                                                },
                                            },
                                            ensure_ascii=False,
                                            separators=(",", ":"),
                                            allow_nan=False,
                                        ) + "\n"
                        if deferred_terminal_wire is not None:
                            written = self._output.write(deferred_terminal_wire)
                            if written != len(deferred_terminal_wire):
                                raise OSError("short ACP stdout write")
                            self._output.flush()
                            break
                    finally:
                        if publication_lock is not None:
                            publication_lock.release()
            except Exception:
                self._abort_transport()
                return
            finally:
                with self._state_lock:
                    if (
                        batch.settles_active
                        and batch.active is not None
                        and self._active is batch.active
                    ):
                        self._active = None
                self._outbound.task_done()


__all__ = ["ACP_PROTOCOL_VERSION", "AcpStdioServer"]
