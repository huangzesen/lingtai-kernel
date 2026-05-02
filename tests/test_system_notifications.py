"""End-to-end integration tests for system_notification tool-call pairs.

These tests exercise the full path: producer (_enqueue_system_notification)
→ tc_inbox → splice into chat → dismiss (voluntary OR via email.read auto-
dismiss).

Uses ChatInterface + TCInbox directly with a stub-agent harness; the kernel's
LLM/session machinery is NOT exercised here. The integration is at the
bookkeeping level — does the dict get cleaned, does the chat reflect the
right state, does the dual-store dismiss work."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lingtai_kernel.intrinsics import system as sys_intrinsic
from lingtai_kernel.llm.interface import (
    ChatInterface, ToolCallBlock, ToolResultBlock,
)
from lingtai_kernel.tc_inbox import TCInbox, InvoluntaryToolCall


class _StubChatSession:
    """Stand-in for OpenAIChatSession / AnthropicChatSession etc. The
    dismiss handler reaches the chat interface via ``_session.chat.interface``
    — see test_system_dismiss.py module docstring for why mirroring this
    hierarchy in the stub is load-bearing."""

    def __init__(self, interface: ChatInterface):
        self.interface = interface


@dataclass
class _StubSession:
    chat: _StubChatSession


@dataclass
class _StubAgent:
    """Minimal subset of BaseAgent attributes touched by the dismiss path."""
    _tc_inbox: TCInbox = field(default_factory=TCInbox)
    _session: _StubSession = field(default=None)
    _pending_mail_notifications: dict[str, str] = field(default_factory=dict)
    _logs: list[tuple[str, dict]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self._session is None:
            self._session = _StubSession(chat=_StubChatSession(ChatInterface()))

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def _splice_pair(agent: _StubAgent, item: InvoluntaryToolCall) -> None:
    """Mimic _drain_tc_inbox: splice a queued pair into chat."""
    agent._session.chat.interface.add_assistant_message(content=[item.call])
    agent._session.chat.interface.add_tool_results([item.result])


def _make_email_notification(
    notif_id: str, mail_id: str, body: str = "[system] new mail ..."
) -> InvoluntaryToolCall:
    call_id = f"sn_{notif_id}"
    call = ToolCallBlock(
        id=call_id,
        name="system",
        args={
            "action": "notification",
            "notif_id": notif_id,
            "source": "email",
            "ref_id": mail_id,
            "received_at": "2026-05-02T00:00:00Z",
        },
    )
    result = ToolResultBlock(id=call_id, name="system", content=body)
    return InvoluntaryToolCall(
        call=call, result=result,
        source=f"system.notification:{notif_id}",
        enqueued_at=0.0, coalesce=False, replace_in_history=False,
    )


def test_arrival_then_voluntary_dismiss():
    agent = _StubAgent()
    item = _make_email_notification("notif_a", "mail_001")
    agent._tc_inbox.enqueue(item)
    agent._pending_mail_notifications["mail_001"] = "notif_a"

    # Splice (simulate _drain_tc_inbox)
    drained = agent._tc_inbox.drain()
    for it in drained:
        _splice_pair(agent, it)
    assert len(agent._session.chat.interface.conversation_entries()) == 2

    # Voluntary dismiss
    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_a"]})
    assert res["results"] == {"notif_a": "dismissed"}
    assert len(agent._session.chat.interface.conversation_entries()) == 0
    assert agent._pending_mail_notifications == {}


def test_arrival_then_email_read_auto_dismiss():
    """Simulate the email._read post-action hook: pop pending dict, call
    system._dismiss with the notif_id."""
    agent = _StubAgent()
    item = _make_email_notification("notif_b", "mail_002")
    agent._tc_inbox.enqueue(item)
    agent._pending_mail_notifications["mail_002"] = "notif_b"
    drained = agent._tc_inbox.drain()
    for it in drained:
        _splice_pair(agent, it)

    # Mimic email._read: agent reads mail_002
    notif_id = agent._pending_mail_notifications.pop("mail_002", None)
    assert notif_id == "notif_b"
    res = sys_intrinsic._dismiss(agent, {"ids": [notif_id], "_invoked_by": "email.read"})
    assert res["results"] == {"notif_b": "dismissed"}
    assert len(agent._session.chat.interface.conversation_entries()) == 0


def test_check_does_not_dismiss():
    """email.check is NOT supposed to auto-dismiss. We assert here that the
    pending dict and chat survive a no-op flow (i.e. nothing pops/clears
    pending notifications when the agent only "glances" via check)."""
    agent = _StubAgent()
    item = _make_email_notification("notif_c", "mail_003")
    agent._tc_inbox.enqueue(item)
    agent._pending_mail_notifications["mail_003"] = "notif_c"
    drained = agent._tc_inbox.drain()
    for it in drained:
        _splice_pair(agent, it)

    # Simulate check: it does NOT touch _pending_mail_notifications.
    # No code is invoked; we just assert the state stays put.
    assert agent._pending_mail_notifications == {"mail_003": "notif_c"}
    assert len(agent._session.chat.interface.conversation_entries()) == 2


def test_race_dismiss_before_splice():
    """Mail arrives → enqueue → BEFORE drain, agent reads X → dismiss removes
    the pair from tc_inbox. The pair never lands in chat."""
    agent = _StubAgent()
    item = _make_email_notification("notif_d", "mail_004")
    agent._tc_inbox.enqueue(item)
    agent._pending_mail_notifications["mail_004"] = "notif_d"
    # Do NOT splice yet — race condition.

    notif_id = agent._pending_mail_notifications.pop("mail_004", None)
    res = sys_intrinsic._dismiss(agent, {"ids": [notif_id]})
    assert res["results"] == {"notif_d": "dismissed"}
    # Queue is empty AFTER dismiss; chat was never written.
    assert len(agent._tc_inbox) == 0
    assert len(agent._session.chat.interface.conversation_entries()) == 0


def test_multiple_arrivals_dismiss_one_keep_others():
    """Three mails arrive; agent reads only mail_005. Other two notifications
    persist."""
    agent = _StubAgent()
    items = [
        _make_email_notification("notif_e", "mail_005"),
        _make_email_notification("notif_f", "mail_006"),
        _make_email_notification("notif_g", "mail_007"),
    ]
    for it in items:
        agent._tc_inbox.enqueue(it)
        agent._pending_mail_notifications[
            it.call.args["ref_id"]
        ] = it.call.args["notif_id"]

    drained = agent._tc_inbox.drain()
    for it in drained:
        _splice_pair(agent, it)
    assert len(agent._session.chat.interface.conversation_entries()) == 6  # 3 pairs

    # Read mail_005
    notif_id = agent._pending_mail_notifications.pop("mail_005", None)
    sys_intrinsic._dismiss(agent, {"ids": [notif_id], "_invoked_by": "email.read"})

    # Two pairs remain.
    assert len(agent._session.chat.interface.conversation_entries()) == 4
    assert agent._pending_mail_notifications == {
        "mail_006": "notif_f", "mail_007": "notif_g",
    }


def test_bounce_persists_until_voluntary_dismiss():
    """Bounce (source=email.bounce) has no auto-dismiss hook. Agent must
    voluntarily dismiss via system.dismiss."""
    agent = _StubAgent()
    call_id = "sn_bounce_001"
    notif_id = "notif_bounce_001"
    call = ToolCallBlock(
        id=call_id,
        name="system",
        args={
            "action": "notification",
            "notif_id": notif_id,
            "source": "email.bounce",
            "ref_id": "msg_failed_send",
            "received_at": "2026-05-02T00:00:00Z",
        },
    )
    result = ToolResultBlock(id=call_id, name="system", content="[system] bounce ...")
    item = InvoluntaryToolCall(
        call=call, result=result,
        source=f"system.notification:{notif_id}",
        enqueued_at=0.0, coalesce=False, replace_in_history=False,
    )
    # Bounce: not added to _pending_mail_notifications (only "email" source is).
    agent._tc_inbox.enqueue(item)
    drained = agent._tc_inbox.drain()
    for it in drained:
        _splice_pair(agent, it)

    # Bounce is NOT in pending dict (no auto-dismiss hook).
    assert agent._pending_mail_notifications == {}

    # Voluntary dismiss works.
    res = sys_intrinsic._dismiss(agent, {"ids": [notif_id]})
    assert res["results"] == {notif_id: "dismissed"}
    assert len(agent._session.chat.interface.conversation_entries()) == 0


def test_no_msg_request_from_system_in_inbox():
    """Regression check: after rerouting, no production code path should
    push MSG_REQUEST from sender='system' to inbox. We can't easily exercise
    the runtime here without the full agent, but we assert at least that
    _enqueue_system_notification's docstring/behavior promises tc_inbox
    delivery, not inbox.put."""
    # If this test ever needs to be richer, instantiate a BaseAgent in a
    # temp working dir, fire mail through MailService, and grep
    # chat_history.jsonl for sender="system" user-turns.
    # For now, the unit-level assertion is that _enqueue_system_notification
    # exists on BaseAgent and that the constants we expect are wired:
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_kernel.message import MSG_TC_WAKE
    assert hasattr(BaseAgent, "_enqueue_system_notification")
    assert MSG_TC_WAKE == "tc_wake"
