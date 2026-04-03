"""Tests for lingtai.addons.feishu.manager — FeishuManager."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai.addons.feishu.manager import FeishuManager
from lingtai.addons.feishu.service import FeishuService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent() -> MagicMock:
    agent = MagicMock()
    agent._working_dir = "/tmp"
    agent.inbox = MagicMock()
    agent._wake_nap = MagicMock()
    agent._log = MagicMock()
    return agent


def _make_manager(tmp_path: Path, accounts: list[dict] | None = None):
    if accounts is None:
        accounts = [
            {"alias": "default", "app_id": "cli_test",
             "app_secret": "sec", "allowed_users": None}
        ]
    agent = _agent()
    mgr_ref: list[FeishuManager | None] = [None]
    svc = FeishuService(
        working_dir=tmp_path,
        accounts_config=accounts,
        on_message=lambda alias, event: mgr_ref[0].on_incoming(alias, event),
    )
    mgr = FeishuManager(agent=agent, service=svc, working_dir=tmp_path)
    mgr_ref[0] = mgr
    return mgr, agent


# ---------------------------------------------------------------------------
# handle dispatch
# ---------------------------------------------------------------------------

def test_handle_unknown_action(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "fly_to_moon"})
    assert "error" in result


def test_handle_accounts(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "accounts"})
    assert result["status"] == "ok"
    assert "default" in result["accounts"]


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def test_check_empty_inbox(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "check"})
    assert result["status"] == "ok"
    assert result["total"] == 0
    assert result["conversations"] == []


def test_check_after_incoming(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path, open_id="ou_alice", chat_id="oc_001", text="Hi")
    result = mgr.handle({"action": "check"})
    assert result["total"] == 1
    assert len(result["conversations"]) == 1
    assert result["conversations"][0]["unread"] == 1


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def test_read_missing_chat_id(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "read"})
    assert "error" in result


def test_read_marks_messages_read(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path, open_id="ou_alice", chat_id="oc_001", text="Hey")
    mgr.handle({"action": "read", "chat_id": "oc_001"})
    # After read, check shows unread == 0
    check = mgr.handle({"action": "check"})
    assert check["conversations"][0]["unread"] == 0


def test_read_returns_messages(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path, open_id="ou_alice", chat_id="oc_001", text="Hello")
    result = mgr.handle({"action": "read", "chat_id": "oc_001"})
    assert result["status"] == "ok"
    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "Hello"


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def test_send_missing_receive_id(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "send", "text": "hi"})
    assert "error" in result


def test_send_missing_text(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "send", "receive_id": "ou_xxx"})
    assert "error" in result


def test_send_success(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    acct = mgr._service.default_account
    acct.send_text = MagicMock(return_value={
        "message_id": "om_001", "chat_id": "oc_001", "create_time": ""
    })
    result = mgr.handle({
        "action": "send",
        "receive_id": "ou_alice",
        "receive_id_type": "open_id",
        "text": "Hello, Alice!",
    })
    assert result["status"] == "sent"
    assert "message_id" in result
    acct.send_text.assert_called_once_with("ou_alice", "open_id", "Hello, Alice!")


def test_send_duplicate_blocked(tmp_path):
    """Sending the same message more than dup_free_passes times should be blocked."""
    mgr, _ = _make_manager(tmp_path)
    acct = mgr._service.default_account
    acct.send_text = MagicMock(return_value={"message_id": "om_x", "chat_id": "oc_y"})

    args = {"action": "send", "receive_id": "ou_bob", "text": "dup"}
    for _ in range(mgr._dup_free_passes):
        mgr.handle(args)
    result = mgr.handle(args)
    assert result["status"] == "blocked"


# ---------------------------------------------------------------------------
# reply
# ---------------------------------------------------------------------------

def test_reply_missing_message_id(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "reply", "text": "hi"})
    assert "error" in result


def test_reply_missing_text(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "reply", "message_id": "default:oc_001:om_001"})
    assert "error" in result


def test_reply_invalid_compound_id(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "reply", "message_id": "badformat", "text": "hi"})
    assert "error" in result


def test_reply_success(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    acct = mgr._service.default_account
    acct.reply_text = MagicMock(return_value={"message_id": "om_reply", "chat_id": "oc_001"})
    result = mgr.handle({
        "action": "reply",
        "message_id": "default:oc_001:om_original",
        "text": "Sure!",
    })
    assert result["status"] == "sent"
    acct.reply_text.assert_called_once_with("om_original", "Sure!")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_missing_query(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "search"})
    assert "error" in result


def test_search_no_results(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "search", "query": "zzz_not_here"})
    assert result["status"] == "ok"
    assert result["total"] == 0


def test_search_finds_match(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path, open_id="ou_alice", chat_id="oc_001",
                     text="Order tracking update")
    result = mgr.handle({"action": "search", "query": "tracking"})
    assert result["total"] == 1
    assert "tracking" in result["messages"][0]["text"].lower()


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------

def test_contacts_empty(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "contacts"})
    assert result["status"] == "ok"
    assert result["contacts"] == {}


def test_add_and_list_contact(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({
        "action": "add_contact",
        "open_id": "ou_alice",
        "alias": "alice",
        "name": "Alice",
    })
    assert result["status"] == "added"
    contacts_result = mgr.handle({"action": "contacts"})
    assert "ou_alice" in contacts_result["contacts"]


def test_remove_contact_by_open_id(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    mgr.handle({"action": "add_contact", "open_id": "ou_bob", "alias": "bob"})
    result = mgr.handle({"action": "remove_contact", "open_id": "ou_bob"})
    assert result["status"] == "removed"
    contacts_result = mgr.handle({"action": "contacts"})
    assert "ou_bob" not in contacts_result["contacts"]


def test_remove_contact_by_alias(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    mgr.handle({"action": "add_contact", "open_id": "ou_carol", "alias": "carol"})
    result = mgr.handle({"action": "remove_contact", "alias": "carol"})
    assert result["status"] == "removed"


def test_remove_contact_not_found(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    result = mgr.handle({"action": "remove_contact", "open_id": "ou_nobody"})
    assert "error" in result


# ---------------------------------------------------------------------------
# on_incoming — notification dispatch
# ---------------------------------------------------------------------------

def _make_event_data(
    open_id: str = "ou_alice",
    chat_id: str = "oc_001",
    text: str = "Hello",
    feishu_msg_id: str = "om_111",
):
    """Build a fake lark_oapi event data object."""
    event = MagicMock()
    event.message.message_id = feishu_msg_id
    event.message.chat_id = chat_id
    event.message.chat_type = "p2p"
    event.message.message_type = "text"
    event.message.content = json.dumps({"text": text})
    event.message.create_time = "1700000000000"
    event.message.parent_id = ""
    event.sender.sender_id.open_id = open_id
    data = MagicMock()
    data.event = event
    return data


def _deliver_message(
    mgr: FeishuManager,
    tmp_path: Path,
    open_id="ou_alice",
    chat_id="oc_001",
    text="Hello",
    feishu_msg_id="om_111",
):
    data = _make_event_data(open_id, chat_id, text, feishu_msg_id)
    # _make_message is imported inside on_incoming, patch at the source module
    with patch("lingtai_kernel.message._make_message") as mock_make:
        mock_make.return_value = MagicMock()
        mgr.on_incoming("default", data)


def test_on_incoming_writes_to_inbox_dir(tmp_path):
    mgr, agent = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path)
    inbox_dir = tmp_path / "feishu" / "default" / "inbox"
    assert inbox_dir.is_dir()
    msg_dirs = list(inbox_dir.iterdir())
    assert len(msg_dirs) == 1
    msg_file = msg_dirs[0] / "message.json"
    assert msg_file.is_file()
    payload = json.loads(msg_file.read_text())
    assert payload["text"] == "Hello"
    assert payload["from_open_id"] == "ou_alice"


def test_on_incoming_notifies_agent(tmp_path):
    mgr, agent = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path)
    agent._wake_nap.assert_called_once_with("message_received")
    agent.inbox.put.assert_called_once()


def test_on_incoming_upserts_contact(tmp_path):
    mgr, _ = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path, open_id="ou_alice", chat_id="oc_001")
    contacts = mgr._load_contacts("default")
    assert "ou_alice" in contacts


def test_on_incoming_logs_event(tmp_path):
    mgr, agent = _make_manager(tmp_path)
    _deliver_message(mgr, tmp_path, text="Test log")
    agent._log.assert_called_once()
    log_kwargs = agent._log.call_args
    assert "feishu_received" in str(log_kwargs)


def test_on_incoming_no_event_attr(tmp_path):
    """Data without .event should be silently ignored."""
    mgr, agent = _make_manager(tmp_path)
    data = MagicMock(spec=[])
    mgr.on_incoming("default", data)
    agent.inbox.put.assert_not_called()
