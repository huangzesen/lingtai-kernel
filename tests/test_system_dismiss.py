"""Tests for intrinsics.system._dismiss — voluntary notification dismissal.

These tests use a stub agent that mimics the BaseAgent attributes the
dismiss handler reads (_tc_inbox, _session.chat, _pending_mail_notifications,
_log)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lingtai_kernel.intrinsics import system as sys_intrinsic
from lingtai_kernel.llm.interface import (
    ChatInterface, ToolCallBlock, ToolResultBlock,
)
from lingtai_kernel.tc_inbox import TCInbox, InvoluntaryToolCall


@dataclass
class _StubSession:
    chat: ChatInterface


@dataclass
class _StubAgent:
    _tc_inbox: TCInbox = field(default_factory=TCInbox)
    _session: _StubSession = field(default=None)  # set in __post_init__
    _pending_mail_notifications: dict[str, str] = field(default_factory=dict)
    _logs: list[tuple[str, dict]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self._session is None:
            self._session = _StubSession(chat=ChatInterface())

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def _enqueue_notification(agent: _StubAgent, notif_id: str, ref_id: str = "mail_abc",
                          source: str = "email") -> str:
    """Helper: enqueue one notification on tc_inbox, splice it into chat,
    and update the pending dict — mimics what _enqueue_system_notification
    + the next drain pass would do."""
    call_id = f"call_{notif_id}"
    call = ToolCallBlock(
        id=call_id,
        name="system",
        args={
            "action": "notification",
            "notif_id": notif_id,
            "source": source,
            "ref_id": ref_id,
        },
    )
    result = ToolResultBlock(id=call_id, name="system", content="...")
    agent._session.chat.add_assistant_message(content=[call])
    agent._session.chat.add_tool_results([result])
    if source == "email":
        agent._pending_mail_notifications[ref_id] = notif_id
    return call_id


def test_dismiss_removes_from_chat():
    agent = _StubAgent()
    _enqueue_notification(agent, "notif_xxx", ref_id="mail_a")
    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_xxx"]})
    assert res["status"] == "ok"
    assert res["results"] == {"notif_xxx": "dismissed"}
    assert len(agent._session.chat.conversation_entries()) == 0
    assert "mail_a" not in agent._pending_mail_notifications


def test_dismiss_only_in_queue_not_chat():
    """Notification still in tc_inbox, not yet spliced — dismiss removes
    from queue and reports dismissed."""
    agent = _StubAgent()
    call = ToolCallBlock(
        id="call_001",
        name="system",
        args={
            "action": "notification",
            "notif_id": "notif_yyy",
            "source": "email",
            "ref_id": "mail_b",
        },
    )
    result = ToolResultBlock(id="call_001", name="system", content="...")
    agent._tc_inbox.enqueue(InvoluntaryToolCall(
        call=call, result=result,
        source="system.notification:notif_yyy",
        enqueued_at=0.0, coalesce=False, replace_in_history=False,
    ))
    agent._pending_mail_notifications["mail_b"] = "notif_yyy"

    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_yyy"]})
    assert res["results"] == {"notif_yyy": "dismissed"}
    assert len(agent._tc_inbox) == 0
    assert "mail_b" not in agent._pending_mail_notifications


def test_dismiss_unknown_id_returns_not_found():
    agent = _StubAgent()
    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_does_not_exist"]})
    assert res["status"] == "ok"
    assert res["results"] == {"notif_does_not_exist": "not_found"}


def test_dismiss_mixed_ids():
    agent = _StubAgent()
    _enqueue_notification(agent, "notif_a", ref_id="mail_a")
    _enqueue_notification(agent, "notif_b", ref_id="mail_b")
    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_a", "notif_does_not_exist", "notif_b"]})
    assert res["results"] == {
        "notif_a": "dismissed",
        "notif_does_not_exist": "not_found",
        "notif_b": "dismissed",
    }
    assert len(agent._session.chat.conversation_entries()) == 0
    assert agent._pending_mail_notifications == {}


def test_dismiss_empty_list_errors():
    agent = _StubAgent()
    res = sys_intrinsic._dismiss(agent, {"ids": []})
    assert res["status"] == "error"


def test_dismiss_missing_ids_errors():
    agent = _StubAgent()
    res = sys_intrinsic._dismiss(agent, {})
    assert res["status"] == "error"


def test_dismiss_string_id_coerced_to_list():
    """Defensive: agent passes a single id as a string instead of [string]."""
    agent = _StubAgent()
    _enqueue_notification(agent, "notif_single", ref_id="mail_x")
    res = sys_intrinsic._dismiss(agent, {"ids": "notif_single"})
    assert res["results"] == {"notif_single": "dismissed"}


def test_dismiss_invalid_element_in_list():
    agent = _StubAgent()
    res = sys_intrinsic._dismiss(agent, {"ids": [123, "notif_real"]})
    assert res["results"]["123"] == "invalid_id"
    assert res["results"]["notif_real"] == "not_found"


def test_handle_rejects_notification_action():
    """Defense-in-depth: agent cannot call system(action='notification', ...)
    via public dispatch even if the LLM hallucinates that action name."""
    agent = _StubAgent()
    res = sys_intrinsic.handle(agent, {"action": "notification", "notif_id": "x"})
    assert res["status"] == "error"
    assert "kernel" in res["message"].lower() or "synthesized" in res["message"].lower()
