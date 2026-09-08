"""Core-owned outbound Port for RAM-resident LLM stream progress.

This boundary lets the kernel session publish *how much* of a provider
response has streamed so far — never *what* streamed — without knowing how a
consumer reads it. It owns three things:

- ``StreamProgressPort`` — the three-operation Port ``SessionManager`` brackets
  every streaming provider call with (``begin() -> generation`` /
  ``add_chars(generation, n)`` / ``end(generation)``); the generation token
  binds every callback to the response it belongs to.
- ``StreamProgressState`` — the thread-safe, memory-only implementation of
  that Port plus the typed ``StreamProgressSnapshot`` it exposes to a publisher.
  There is no file, JSONL record, transcript, partial text, or credential here.
- ``candidate_ports`` — the deterministic discovery arithmetic shared by the
  producer and every consumer, so a consumer can reattach to a living agent
  process without any shared on-disk state.

The concrete loopback HTTP publisher lives entirely in an outside adapter
(``lingtai.adapters.stream_progress``) that Core never imports or names; this
module deliberately carries no socket, HTTP, thread-server, or filesystem
vocabulary. Consumers estimate tokens as ``streamed_chars // 4``.
"""
from __future__ import annotations

import hashlib
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

#: JSON ``schema`` string every v1 snapshot carries.
STREAM_PROGRESS_SCHEMA = "lingtai.stream-progress/v1"
#: The one read-only resource path a publisher serves.
STREAM_PROGRESS_PATH = "/v1/stream-progress"

#: Discovery arithmetic — byte-for-byte shared with every consumer.
DISCOVERY_PORT_BASE = 41000
DISCOVERY_PORT_SPAN = 20000
DISCOVERY_PORT_STRIDE = 7919
DISCOVERY_CANDIDATE_COUNT = 8


def discovery_seed(agent_id: str) -> int:
    """``uint16_be(SHA256(schema + "\\0" + UTF8(agent_id))[0:2])``."""
    digest = hashlib.sha256(
        STREAM_PROGRESS_SCHEMA.encode("utf-8") + b"\x00" + agent_id.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:2], "big")


def candidate_ports(agent_id: str) -> list[int]:
    """Deterministic ordered loopback port candidates for ``agent_id``.

    Candidate ``i`` for ``i = 0..7`` is ``41000 + ((seed + i * 7919) mod 20000)``.
    A publisher binds the first candidate that is free; a reader probes them
    in order and accepts only a valid v1 response whose ``agent_id`` matches.
    """
    seed = discovery_seed(agent_id)
    return [
        DISCOVERY_PORT_BASE + ((seed + i * DISCOVERY_PORT_STRIDE) % DISCOVERY_PORT_SPAN)
        for i in range(DISCOVERY_CANDIDATE_COUNT)
    ]


@dataclass(frozen=True)
class StreamProgressSnapshot:
    """One point-in-time reading of the RAM-resident progress state.

    Exactly the seven documented v1 fields; there is no text field and none
    may be added to this schema version.
    """

    agent_id: str
    generation: int
    active: bool
    streamed_chars: int
    updated_unix_ms: int
    pid: int
    schema: str = STREAM_PROGRESS_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "agent_id": self.agent_id,
            "generation": int(self.generation),
            "active": bool(self.active),
            "streamed_chars": int(self.streamed_chars),
            "updated_unix_ms": int(self.updated_unix_ms),
            "pid": int(self.pid),
        }


class StreamProgressPort(ABC):
    """Three-operation, generation-bound progress boundary owned by Core.

    ``SessionManager`` calls ``begin()`` before it starts waiting on the
    provider and captures the returned generation token; its worker-thread
    count-only callback calls ``add_chars(generation, n)`` for every provider
    output fragment; and ``end(generation)`` runs in
    a ``finally`` so success and failure both clear the snapshot. The token
    is what makes an abandoned (timed-out) provider worker harmless: its late
    deltas and its late ``end`` carry an older generation and are ignored, so
    they can never contaminate the response that began after it. Publication
    failures are the caller's to swallow: a raising implementation must never
    fail the LLM call.
    """

    @abstractmethod
    def begin(self) -> int:
        """A new provider response starts: ``generation += 1``, active, zero
        chars. Returns the new generation token the caller binds its
        ``add_chars``/``end`` calls to."""
        ...

    @abstractmethod
    def add_chars(self, generation: int, count: int) -> None:
        """Provider output with representation length ``count`` arrived for
        ``generation``; ignored unless that generation is the active one."""
        ...

    @abstractmethod
    def end(self, generation: int) -> None:
        """The response for ``generation`` finished (success or failure):
        inactive, zero chars — only if that generation is still the active one."""
        ...


class StreamProgressState(StreamProgressPort):
    """Memory-only, thread-safe implementation of the Port.

    ``add_chars`` is called from the LLM worker thread while ``begin``/``end``
    run on the session thread and a publisher may read ``snapshot()`` from any
    thread, so every access takes one lock. Every mutation after ``begin`` is
    bound to the generation token ``begin`` returned: a delta or ``end`` for
    any other generation — the typical case being an abandoned timed-out
    worker that keeps emitting after a newer response has begun — is ignored,
    so a cleared snapshot stays cleared and a newer active snapshot is never
    altered by an older response.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        pid: int,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._pid = int(pid)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._generation = 0
        self._active = False
        self._streamed_chars = 0
        self._updated_unix_ms = self._now_ms()

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def begin(self) -> int:
        with self._lock:
            self._generation += 1
            self._active = True
            self._streamed_chars = 0
            self._updated_unix_ms = self._now_ms()
            return self._generation

    def add_chars(self, generation: int, count: int) -> None:
        count = int(count)
        if count <= 0:
            return
        with self._lock:
            if not self._active or generation != self._generation:
                return
            self._streamed_chars += count
            self._updated_unix_ms = self._now_ms()

    def end(self, generation: int) -> None:
        with self._lock:
            if not self._active or generation != self._generation:
                return
            self._active = False
            self._streamed_chars = 0
            self._updated_unix_ms = self._now_ms()

    def snapshot(self) -> StreamProgressSnapshot:
        with self._lock:
            return StreamProgressSnapshot(
                agent_id=self._agent_id,
                generation=self._generation,
                active=self._active,
                streamed_chars=self._streamed_chars,
                updated_unix_ms=self._updated_unix_ms,
                pid=self._pid,
            )


__all__ = [
    "DISCOVERY_CANDIDATE_COUNT",
    "DISCOVERY_PORT_BASE",
    "DISCOVERY_PORT_SPAN",
    "DISCOVERY_PORT_STRIDE",
    "STREAM_PROGRESS_PATH",
    "STREAM_PROGRESS_SCHEMA",
    "StreamProgressPort",
    "StreamProgressSnapshot",
    "StreamProgressState",
    "candidate_ports",
    "discovery_seed",
]
