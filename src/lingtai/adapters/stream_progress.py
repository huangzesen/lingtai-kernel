"""Portable loopback HTTP publisher for the Core-owned stream-progress Port.

``LoopbackStreamProgressPublisher`` implements
``lingtai.kernel.stream_progress.StreamProgressPort`` by delegating every
transition to a memory-only ``StreamProgressState`` and serving that state's
snapshot as JSON on ``GET /v1/stream-progress`` from a daemon thread bound
only to ``127.0.0.1``. It is the sole production adapter for that Port.

The adapter is portable, not POSIX-specific: Python's ``http.server`` and a
loopback TCP socket are the concrete mechanism, but there is no filesystem,
``fcntl``, or platform selection here — so it lives at the top of the
``lingtai.adapters`` package like the lifecycle clock. Its structure is mapped
by ``src/lingtai/ANATOMY.md`` and owned by the stream-progress governed twins
(``src/lingtai/kernel/stream_progress/CONTRACT.md`` and its paired
``ANATOMY.md``); it has no dedicated anatomy of its own.

Everything here is fail-open. A bind failure on every discovery candidate
leaves the adapter counting in RAM with no endpoint; a request-handling error
answers that one request and nothing else; nothing here can fail the LLM call.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable

from lingtai.kernel.stream_progress import (
    STREAM_PROGRESS_PATH,
    StreamProgressPort,
    StreamProgressState,
    candidate_ports,
)

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"


class _StreamProgressServer(ThreadingHTTPServer):
    """Loopback server carrying the state its handler serializes."""

    # Never share a candidate port with another live publisher (and never let
    # Windows' permissive SO_REUSEADDR steal one): the first *free* candidate
    # is the contract.
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: StreamProgressState) -> None:
        self.state = state
        super().__init__(address, _StreamProgressHandler)


class _StreamProgressHandler(BaseHTTPRequestHandler):
    """``GET /v1/stream-progress`` → JSON snapshot; other paths 404; non-GET 405."""

    server: _StreamProgressServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # silent: no request log, no stderr chatter

    def _write_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != STREAM_PROGRESS_PATH:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            payload = self.server.state.snapshot().to_dict()
        except Exception:  # pragma: no cover - defensive; state never raises
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "unavailable"})
            return
        self._write_json(HTTPStatus.OK, payload)

    def _method_not_allowed(self) -> None:
        body = b'{"error":"method_not_allowed"}'
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = _method_not_allowed  # noqa: N815
    do_POST = _method_not_allowed  # noqa: N815
    do_PUT = _method_not_allowed  # noqa: N815
    do_PATCH = _method_not_allowed  # noqa: N815
    do_DELETE = _method_not_allowed  # noqa: N815
    do_OPTIONS = _method_not_allowed  # noqa: N815


class LoopbackStreamProgressPublisher(StreamProgressPort):
    """Serve one agent's RAM-resident stream progress on loopback.

    Construction is side-effect free. ``start()`` binds the first free
    discovery candidate for ``agent_id`` on ``127.0.0.1`` and serves from a
    daemon thread; it returns ``False`` (and logs once) when no candidate can
    be bound, leaving the publisher counting with no endpoint. The endpoint's
    lifetime is the agent process; ``close()`` is a best-effort explicit stop.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        state: StreamProgressState | None = None,
        host: str = LOOPBACK_HOST,
        candidates: Iterable[int] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._state = state or StreamProgressState(agent_id, pid=os.getpid())
        self._host = host
        self._candidates = list(candidates) if candidates is not None else candidate_ports(agent_id)
        self._server: _StreamProgressServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None

    # --- Port -------------------------------------------------------------

    def begin(self) -> int:
        return self._state.begin()

    def add_chars(self, generation: int, count: int) -> None:
        self._state.add_chars(generation, count)

    def end(self, generation: int) -> None:
        self._state.end(generation)

    # --- Endpoint ---------------------------------------------------------

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def state(self) -> StreamProgressState:
        return self._state

    @property
    def port(self) -> int | None:
        """The bound loopback port, or ``None`` while no endpoint is served."""
        return self._port

    @property
    def candidates(self) -> list[int]:
        return list(self._candidates)

    def start(self) -> bool:
        if self._server is not None:
            return True
        for port in self._candidates:
            try:
                server = _StreamProgressServer((self._host, port), self._state)
            except OSError:
                continue
            self._server = server
            self._port = server.server_address[1]
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.5},
                name=f"stream-progress:{self._agent_id}",
                daemon=True,
            )
            self._thread.start()
            return True
        logger.warning(
            "stream_progress_bind_failed agent_id=%s candidates=%s",
            self._agent_id,
            self._candidates,
        )
        return False

    def close(self) -> None:
        server, self._server = self._server, None
        self._port = None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            logger.debug("stream_progress_close_failed", exc_info=True)


def loopback_stream_progress_factory(agent_id: str) -> StreamProgressPort:
    """Composition-root factory: build, start (fail-open), and return the Port.

    ``BaseAgent`` calls this with its stable ``agent_id`` once identity is
    known. Bind failure is logged by ``start()`` and never raised; the agent
    boots either way.
    """
    publisher = LoopbackStreamProgressPublisher(agent_id)
    try:
        publisher.start()
    except Exception:
        logger.warning("stream_progress_start_failed agent_id=%s", agent_id, exc_info=True)
    return publisher


__all__ = [
    "LOOPBACK_HOST",
    "LoopbackStreamProgressPublisher",
    "loopback_stream_progress_factory",
]
