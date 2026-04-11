import json
import os
from pathlib import Path

from lingtai.addons.wechat.manager import WechatManager, _chunk_text


def test_chunk_text_short():
    assert _chunk_text("hello", 4000) == ["hello"]


def test_chunk_text_long():
    text = "a" * 8500
    chunks = _chunk_text(text, 4000)
    assert len(chunks) == 3
    assert chunks[0] == "a" * 4000
    assert chunks[1] == "a" * 4000
    assert chunks[2] == "a" * 500
    assert "".join(chunks) == text


def test_handle_contacts_empty(tmp_path):
    """Manager contacts start empty and can be added."""

    class FakeAgent:
        _working_dir = str(tmp_path)
        inbox = None
        def _wake_nap(self, reason): pass
        def add_tool(self, *a, **kw): pass

    mgr = WechatManager(
        agent=FakeAgent(),
        token="fake_token",
        user_id="bot@im.bot",
        working_dir=tmp_path,
    )

    result = mgr.handle({"action": "contacts"})
    assert result == {"contacts": {}}


def test_handle_add_remove_contact(tmp_path):
    class FakeAgent:
        _working_dir = str(tmp_path)
        inbox = None
        def _wake_nap(self, reason): pass
        def add_tool(self, *a, **kw): pass

    mgr = WechatManager(
        agent=FakeAgent(),
        token="fake_token",
        user_id="bot@im.bot",
        working_dir=tmp_path,
    )

    # Add contact
    result = mgr.handle({
        "action": "add_contact",
        "user_id": "wxid_abc@im.wechat",
        "alias": "Alice",
    })
    assert result["status"] == "ok"

    # Verify in contacts
    result = mgr.handle({"action": "contacts"})
    assert "Alice" in result["contacts"]
    assert result["contacts"]["Alice"]["user_id"] == "wxid_abc@im.wechat"

    # Remove contact
    result = mgr.handle({"action": "remove_contact", "alias": "Alice"})
    assert result["status"] == "ok"

    result = mgr.handle({"action": "contacts"})
    assert result == {"contacts": {}}


def test_handle_check_empty(tmp_path):
    class FakeAgent:
        _working_dir = str(tmp_path)
        inbox = None
        def _wake_nap(self, reason): pass
        def add_tool(self, *a, **kw): pass

    mgr = WechatManager(
        agent=FakeAgent(),
        token="fake_token",
        user_id="bot@im.bot",
        working_dir=tmp_path,
    )

    result = mgr.handle({"action": "check"})
    assert result == {"conversations": []}


def test_handle_unknown_action(tmp_path):
    class FakeAgent:
        _working_dir = str(tmp_path)
        inbox = None
        def _wake_nap(self, reason): pass
        def add_tool(self, *a, **kw): pass

    mgr = WechatManager(
        agent=FakeAgent(),
        token="fake_token",
        user_id="bot@im.bot",
        working_dir=tmp_path,
    )

    result = mgr.handle({"action": "invalid"})
    assert "error" in result


def test_handle_send_missing_user_id(tmp_path):
    class FakeAgent:
        _working_dir = str(tmp_path)
        inbox = None
        def _wake_nap(self, reason): pass
        def add_tool(self, *a, **kw): pass

    mgr = WechatManager(
        agent=FakeAgent(),
        token="fake_token",
        user_id="bot@im.bot",
        working_dir=tmp_path,
    )

    result = mgr.handle({"action": "send", "text": "hello"})
    assert "error" in result
    assert "user_id" in result["error"]


def test_state_persistence(tmp_path):
    class FakeAgent:
        _working_dir = str(tmp_path)
        inbox = None
        def _wake_nap(self, reason): pass
        def add_tool(self, *a, **kw): pass

    mgr = WechatManager(
        agent=FakeAgent(),
        token="fake_token",
        user_id="bot@im.bot",
        working_dir=tmp_path,
    )

    # Add contact and save
    mgr.handle({
        "action": "add_contact",
        "user_id": "wxid@im.wechat",
        "alias": "Bob",
    })
    mgr._save_state()

    # Create new manager — should load persisted contacts
    mgr2 = WechatManager(
        agent=FakeAgent(),
        token="fake_token",
        user_id="bot@im.bot",
        working_dir=tmp_path,
    )
    result = mgr2.handle({"action": "contacts"})
    assert "Bob" in result["contacts"]
