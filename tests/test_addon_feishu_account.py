"""Tests for lingtai.addons.feishu.account — FeishuAccount."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sys

from lingtai.addons.feishu.account import FeishuAccount


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_acct(
    tmp_path: Path,
    allowed_users: list[str] | None = None,
    on_message=None,
) -> FeishuAccount:
    return FeishuAccount(
        alias="test",
        app_id="cli_test",
        app_secret="secret",
        allowed_users=allowed_users,
        on_message=on_message or (lambda a, d: None),
        state_dir=tmp_path,
    )


def _fake_lark():
    """Return a namespace of mocks that replaces lark_oapi in account.py."""
    lark = MagicMock()
    # ws.Client is a class; its instance has a start() method
    ws_instance = MagicMock()
    ws_instance.start = MagicMock()
    ws_instance.stop = MagicMock()
    lark.ws.Client.return_value = ws_instance

    # EventDispatcherHandler builder chain
    handler_builder = MagicMock()
    handler_builder.register_p2_im_message_receive_v1.return_value = handler_builder
    handler_builder.build.return_value = MagicMock()
    lark.EventDispatcherHandler.builder.return_value = handler_builder

    # Client builder chain (REST)
    rest_builder = MagicMock()
    rest_builder.app_id.return_value = rest_builder
    rest_builder.app_secret.return_value = rest_builder
    rest_builder.build.return_value = MagicMock()
    lark.Client.builder.return_value = rest_builder

    return lark


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_construction(tmp_path):
    acct = _make_acct(tmp_path)
    assert acct.alias == "test"
    assert acct._app_id == "cli_test"
    assert acct._ws_thread is None


def test_allowed_users_none_allows_all(tmp_path):
    acct = _make_acct(tmp_path, allowed_users=None)
    assert acct._allowed_users is None


def test_allowed_users_set(tmp_path):
    acct = _make_acct(tmp_path, allowed_users=["ou_aaa", "ou_bbb"])
    assert "ou_aaa" in acct._allowed_users
    assert "ou_ccc" not in acct._allowed_users


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_save_and_load_state(tmp_path):
    acct = _make_acct(tmp_path)
    acct._bot_info = {"app_id": "cli_test"}
    acct._save_state()

    state_file = tmp_path / "state.json"
    assert state_file.is_file()
    data = json.loads(state_file.read_text())
    assert data["bot_info"]["app_id"] == "cli_test"

    # New account loads persisted state
    acct2 = _make_acct(tmp_path)
    assert acct2._bot_info is not None
    assert acct2._bot_info.get("app_id") == "cli_test"


def test_load_state_missing_file(tmp_path):
    """FeishuAccount with no state file should not raise."""
    acct = _make_acct(tmp_path / "nonexistent")
    assert acct._bot_info is None


def test_save_state_no_state_dir():
    """FeishuAccount with state_dir=None should skip save silently."""
    acct = FeishuAccount(
        alias="no_state",
        app_id="cli_x",
        app_secret="s",
        allowed_users=None,
        state_dir=None,
    )
    acct._bot_info = {"app_id": "cli_x"}
    acct._save_state()  # should not raise


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------

def test_start_creates_ws_thread(tmp_path):
    acct = _make_acct(tmp_path)
    fake_lark = _fake_lark()
    import lingtai.addons.feishu.account as mod

    original = mod.lark
    mod.lark = fake_lark
    try:
        acct.start()
        assert acct._ws_thread is not None
        assert acct._ws_thread.daemon is True
        acct.stop()
    finally:
        mod.lark = original


def test_start_idempotent(tmp_path):
    """Calling start() twice should not spawn a second thread."""
    acct = _make_acct(tmp_path)
    fake_lark = _fake_lark()
    import lingtai.addons.feishu.account as mod

    original = mod.lark
    mod.lark = fake_lark
    try:
        acct.start()
        first_thread = acct._ws_thread
        acct.start()
        assert acct._ws_thread is first_thread
        acct.stop()
    finally:
        mod.lark = original


# ---------------------------------------------------------------------------
# _process_event — allowed_users filtering
# ---------------------------------------------------------------------------

def _make_event(open_id: str) -> MagicMock:
    event = MagicMock()
    sender_id = MagicMock()
    sender_id.open_id = open_id
    event.sender.sender_id = sender_id
    data = MagicMock()
    data.event = event
    return data


def test_process_event_allowed(tmp_path):
    received = []
    acct = _make_acct(
        tmp_path,
        allowed_users=["ou_allowed"],
        on_message=lambda a, d: received.append(d),
    )
    acct._process_event(_make_event("ou_allowed"))
    assert len(received) == 1


def test_process_event_blocked(tmp_path):
    received = []
    acct = _make_acct(
        tmp_path,
        allowed_users=["ou_allowed"],
        on_message=lambda a, d: received.append(d),
    )
    acct._process_event(_make_event("ou_stranger"))
    assert len(received) == 0


def test_process_event_no_filter(tmp_path):
    """With allowed_users=None everyone is allowed."""
    received = []
    acct = _make_acct(
        tmp_path,
        allowed_users=None,
        on_message=lambda a, d: received.append(d),
    )
    acct._process_event(_make_event("ou_anyone"))
    assert len(received) == 1


def test_process_event_no_event_attr(tmp_path):
    """Data without .event should be silently ignored."""
    acct = _make_acct(tmp_path)
    data = MagicMock(spec=[])  # no .event
    acct._process_event(data)  # should not raise


# ---------------------------------------------------------------------------
# send_text
# ---------------------------------------------------------------------------

def _make_rest_client(message_id="msg_001", chat_id="oc_001"):
    client = MagicMock()
    response = MagicMock()
    response.success.return_value = True
    data = MagicMock()
    data.message_id = message_id
    data.chat_id = chat_id
    data.create_time = "1700000000000"
    response.data = data
    client.im.v1.message.create.return_value = response
    client.im.v1.message.reply.return_value = response
    return client


def _mock_im_v1(success: bool = True, message_id: str = "msg_001", chat_id: str = "oc_001"):
    """Build a fake lark_oapi.api.im.v1 module replacing the dynamic import inside send_text/reply_text."""
    response = MagicMock()
    response.success.return_value = success
    if not success:
        response.code = 99991400
        response.msg = "permission denied"
    data = MagicMock()
    data.message_id = message_id
    data.chat_id = chat_id
    data.create_time = "1700000000000"
    response.data = data

    # Builder chain shared by CreateMessageRequest and ReplyMessageRequest
    def _builder_chain(response):
        inner = MagicMock()
        inner.build.return_value = MagicMock()
        inner.receive_id.return_value = inner
        inner.msg_type.return_value = inner
        inner.content.return_value = inner
        outer = MagicMock()
        outer.receive_id_type.return_value = outer
        outer.request_body.return_value = outer
        outer.message_id.return_value = outer
        outer.build.return_value = MagicMock()
        cls = MagicMock()
        cls.builder.return_value = outer
        return cls

    im_mod = MagicMock()
    im_mod.CreateMessageRequest = _builder_chain(response)
    im_mod.CreateMessageRequestBody = _builder_chain(response)
    im_mod.ReplyMessageRequest = _builder_chain(response)
    im_mod.ReplyMessageRequestBody = _builder_chain(response)
    return im_mod, response


def test_send_text(tmp_path):
    acct = _make_acct(tmp_path)
    im_mod, response = _mock_im_v1(success=True, message_id="msg_001", chat_id="oc_001")
    acct._rest_client = MagicMock()
    acct._rest_client.im.v1.message.create.return_value = response

    with patch.dict("sys.modules", {"lark_oapi.api.im.v1": im_mod}):
        result = acct.send_text("ou_xxx", "open_id", "Hello")
    assert result["message_id"] == "msg_001"
    acct._rest_client.im.v1.message.create.assert_called_once()


def test_send_text_failure(tmp_path):
    acct = _make_acct(tmp_path)
    im_mod, response = _mock_im_v1(success=False)
    acct._rest_client = MagicMock()
    acct._rest_client.im.v1.message.create.return_value = response

    with patch.dict("sys.modules", {"lark_oapi.api.im.v1": im_mod}):
        with pytest.raises(RuntimeError, match="send_text failed"):
            acct.send_text("ou_xxx", "open_id", "Hello")
