"""Defense against stub-divergence regression in the dismiss path.

In production, ``agent._session.chat`` is a ChatSession (e.g.
OpenAIChatSession) and the chat-side helper ``remove_pair_by_notif_id``
lives one level deeper at ``.interface``. Earlier stubs put ChatInterface
directly on ``_session.chat`` so unit tests passed against a buggy
implementation that called ``agent._session.chat.remove_pair_by_notif_id(...)``.
Production saw AttributeError: ``OpenAIChatSession has no attribute
remove_pair_by_notif_id``.

These tests defend against recurrence by:

1. Simulating the production error precisely — using a stub whose
   ``_session.chat`` does NOT have ``remove_pair_by_notif_id`` (it's the
   real ChatSession shape) — then asserting that ``_dismiss`` correctly
   reaches through ``.interface`` to find the helper.

2. Asserting that ``_dismiss`` does NOT crash with AttributeError when
   given a chat-shaped object lacking ``.remove_pair_by_notif_id`` directly.

3. Asserting via ``hasattr`` checks that the production access path is
   ``_session.chat.interface.remove_pair_by_notif_id`` and that the
   alternative path (``_session.chat.remove_pair_by_notif_id``) is NOT
   what should be reached for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lingtai_kernel.intrinsics import system as sys_intrinsic
from lingtai_kernel.llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock
from lingtai_kernel.tc_inbox import TCInbox


class _ProductionLikeChatSession:
    """Models the real ChatSession surface: has ``.interface`` (the
    ChatInterface) but does NOT itself have ``remove_pair_by_notif_id``.
    A bug that goes via ``_session.chat.X`` instead of ``_session.chat.interface.X``
    will AttributeError against this stub, surfacing the production bug
    in unit tests."""

    def __init__(self, interface: ChatInterface):
        self.interface = interface

    # Deliberately not implementing remove_pair_by_notif_id, add_assistant_message,
    # add_tool_results, conversation_entries, etc. The production ChatSession
    # delegates these to .interface; bugs that bypass .interface AttributeError.


@dataclass
class _ProductionLikeSession:
    chat: _ProductionLikeChatSession


@dataclass
class _ProductionLikeAgent:
    _tc_inbox: TCInbox = field(default_factory=TCInbox)
    _session: _ProductionLikeSession = field(default=None)
    _pending_mail_notifications: dict[str, str] = field(default_factory=dict)
    _logs: list[tuple[str, dict]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self._session is None:
            self._session = _ProductionLikeSession(
                chat=_ProductionLikeChatSession(ChatInterface())
            )

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def _splice_via_interface(agent, notif_id: str, call_id: str = "call_x"):
    """Splice using the production access path: _session.chat.interface."""
    iface: ChatInterface = agent._session.chat.interface
    call = ToolCallBlock(
        id=call_id,
        name="system",
        args={
            "action": "notification",
            "notif_id": notif_id,
            "source": "email",
            "ref_id": "mail_x",
        },
    )
    result = ToolResultBlock(id=call_id, name="system", content="...")
    iface.add_assistant_message(content=[call])
    iface.add_tool_results([result])


def test_dismiss_reaches_through_session_chat_interface():
    """The dismiss handler must access remove_pair_by_notif_id via
    _session.chat.interface, NOT _session.chat directly. Using a stub whose
    .chat lacks remove_pair_by_notif_id, the dismiss must still work
    (because it goes through .interface), and conversely, a bug that
    bypasses .interface would AttributeError on this stub."""
    agent = _ProductionLikeAgent()
    _splice_via_interface(agent, "notif_through_interface")

    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_through_interface"]})

    # If _dismiss reaches through .interface correctly, this works.
    # If a future regression accesses chat.X directly, the test stub will
    # AttributeError before this assertion runs.
    assert res["status"] == "ok"
    assert res["results"] == {"notif_through_interface": "dismissed"}
    assert len(agent._session.chat.interface.conversation_entries()) == 0


def test_dismiss_does_not_crash_when_chat_session_lacks_helper_directly():
    """Sanity: confirm _ProductionLikeChatSession really does NOT have
    remove_pair_by_notif_id directly. If it did, this stub would not
    actually defend against the bug it's meant to defend against."""
    agent = _ProductionLikeAgent()
    chat_session = agent._session.chat

    # The chat-session surface itself does NOT carry the helper.
    assert not hasattr(chat_session, "remove_pair_by_notif_id"), (
        "ChatSession should NOT carry remove_pair_by_notif_id directly — "
        "production bug surfaced because code called chat.X instead of "
        "chat.interface.X. This test stub must mirror that constraint or "
        "it would silently accept the bug like the original test stub did."
    )

    # The interface, accessed via .interface, DOES carry it.
    assert hasattr(chat_session.interface, "remove_pair_by_notif_id"), (
        "ChatInterface must carry remove_pair_by_notif_id."
    )


def test_dismiss_unknown_id_handles_production_chat_session_shape():
    """For the not_found case, dismiss must not crash even when chat is
    the production-like ChatSession with no direct helper method."""
    agent = _ProductionLikeAgent()

    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_does_not_exist"]})

    assert res["status"] == "ok"
    assert res["results"] == {"notif_does_not_exist": "not_found"}
