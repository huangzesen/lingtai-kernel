"""Protocol-neutral, turn-scoped tool lifecycle observation."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ToolLifecycleState(str, Enum):
    """A bounded lifecycle fact emitted by Core tool dispatch."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ToolLifecycleEvent:
    """Safe tool identity and state; arguments and results are intentionally absent."""

    tool_call_id: str
    tool_name: str
    state: ToolLifecycleState


@dataclass(frozen=True, slots=True)
class ToolResultsCommittedEvent:
    """One tool result durably reached the provider context (metadata only).

    Like :class:`ToolLifecycleEvent`, arguments and results are intentionally
    absent: the event carries only the tool-call id and the receipt-possession
    ``binding`` (``sha256(tool_call_id ‖ 0x00 ‖ raw_receipt)``).  This is the
    reliable committed-fact signal, emitted at a caller settle point after the
    receipt-bearing result is present on the wire — distinct from the fail-open
    terminal ``tool_call_update`` which fires at tool COMPLETED, before commit.
    """

    tool_call_id: str
    binding: str


class TurnToolObserver(Protocol):
    """Optional outbound Port for observing one turn's tool lifecycle."""

    def on_tool_lifecycle(self, event: ToolLifecycleEvent) -> None: ...

    def on_tool_results_committed(
        self, event: ToolResultsCommittedEvent
    ) -> None:
        """Observe the reliable committed-fact event.

        Optional: the default is a no-op so existing observers need no change.
        ``notify_tool_results_committed`` dispatches via ``getattr`` so a purely
        structural implementer that does not define this method is also fine.
        """
        return None


_CURRENT: ContextVar[TurnToolObserver | None] = ContextVar(
    "lingtai_turn_tool_observer", default=None
)


def current_turn_tool_observer() -> TurnToolObserver | None:
    return _CURRENT.get()


def bind_turn_tool_observer(
    observer: TurnToolObserver | None,
) -> Token[TurnToolObserver | None]:
    return _CURRENT.set(observer)


def reset_turn_tool_observer(token: Token[TurnToolObserver | None]) -> None:
    _CURRENT.reset(token)


def clear_turn_tool_observer() -> None:
    """Clear observer scope at a terminal run-loop boundary."""

    _CURRENT.set(None)


def notify_tool_lifecycle(event: ToolLifecycleEvent) -> None:
    """Notify the active observer without allowing it to affect tool execution."""

    observer = _CURRENT.get()
    if observer is None:
        return
    try:
        observer.on_tool_lifecycle(event)
    except Exception:
        pass


def notify_tool_results_committed(event: ToolResultsCommittedEvent) -> bool:
    """Deliver the reliable committed-fact event; report delivery.

    This is the RELIABLE path.  It is still guarded so a misbehaving observer
    cannot raise into Core, but — unlike :func:`notify_tool_lifecycle` — it does
    not hide non-delivery from the caller: it returns ``True`` only when the
    active observer's handler ran to completion, and ``False`` when there is no
    observer or the handler raised.  The caller logs a bounded event on
    ``False`` so a dropped fact is visible rather than silent.  Reliability of
    the wire emit itself lives in the ACP observer's handler, which refuses the
    cosmetic fail-open suppression used for lifecycle updates and treats only
    genuine session teardown as legitimate non-delivery.
    """

    observer = _CURRENT.get()
    if observer is None:
        return False
    handler = getattr(observer, "on_tool_results_committed", None)
    if handler is None:
        return False
    try:
        handler(event)
    except Exception:
        return False
    return True


__all__ = [
    "ToolLifecycleEvent",
    "ToolLifecycleState",
    "ToolResultsCommittedEvent",
    "TurnToolObserver",
    "bind_turn_tool_observer",
    "clear_turn_tool_observer",
    "current_turn_tool_observer",
    "notify_tool_lifecycle",
    "notify_tool_results_committed",
    "reset_turn_tool_observer",
]
