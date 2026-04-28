"""Tests for the imapclient-based IMAPAccount.

Mock IMAPClient at the class level so we can drive the listener loop
with synthetic IDLE responses.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai.addons.imap.account import IMAPAccount


@pytest.fixture
def mock_imapclient_class():
    """Patch IMAPClient at the location it's imported in account.py."""
    with patch("lingtai.addons.imap.account.IMAPClient") as cls:
        yield cls


@pytest.fixture
def account(tmp_path: Path) -> IMAPAccount:
    return IMAPAccount(
        email_address="alice@example.com",
        email_password="appsecret",
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=587,
        working_dir=tmp_path,
        poll_interval=30,
    )


def test_connect_logs_in_and_caches_capabilities(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IMAP4REV1", b"IDLE", b"MOVE", b"UIDPLUS")
    instance.list_folders.return_value = [
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
    ]

    account.connect()

    mock_imapclient_class.assert_called_once_with(
        "imap.example.com", port=993, ssl=True,
    )
    instance.login.assert_called_once_with("alice@example.com", "appsecret")
    assert account.has_idle is True
    assert account.has_move is True
    assert account.has_uidplus is True
    assert account.connected is True


def test_disconnect_logs_out_and_marks_disconnected(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    account.connect()

    account.disconnect()

    instance.logout.assert_called_once()
    assert account.connected is False


def test_connected_reports_false_when_noop_fails(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    account.connect()

    instance.noop.side_effect = OSError("connection reset")

    assert account.connected is False


def test_connect_is_idempotent(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """Double connect() must not open a second client."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []

    account.connect()
    account.connect()  # second call

    # IMAPClient(...) called exactly once, login called exactly once
    assert mock_imapclient_class.call_count == 1
    instance.login.assert_called_once()


def test_connected_clears_dead_pointer_so_next_call_reconnects(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """After NOOP fails, _tool_imap must be cleared so _ensure_connected reconnects."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []

    account.connect()
    assert mock_imapclient_class.call_count == 1

    # Simulate dead connection
    instance.noop.side_effect = OSError("connection reset")
    assert account.connected is False

    # Next access via _ensure_connected should trigger a fresh connect
    instance.noop.side_effect = None  # let the next NOOP succeed
    account.connect()
    assert mock_imapclient_class.call_count == 2


def test_fetch_envelopes_returns_n_most_recent(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.search.return_value = [101, 102, 103, 104, 105]
    instance.fetch.return_value = {
        103: {
            b"FLAGS": (b"\\Seen",),
            b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
                b"From: a@b.com\r\nTo: alice@example.com\r\n"
                b"Subject: hello\r\nDate: Mon, 1 Jan 2026 00:00:00 +0000\r\n",
        },
        104: {
            b"FLAGS": (),
            b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
                b"From: c@d.com\r\nSubject: world\r\n",
        },
        105: {
            b"FLAGS": (b"\\Flagged",),
            b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
                b"From: e@f.com\r\nSubject: !\r\n",
        },
    }
    account.connect()

    envelopes = account.fetch_envelopes("INBOX", n=3)

    instance.select_folder.assert_called_with("INBOX", readonly=True)
    instance.search.assert_called_with("ALL")
    # Fetch was called with last 3 UIDs
    fetch_call = instance.fetch.call_args
    assert sorted(fetch_call[0][0]) == [103, 104, 105]
    # Result includes uid, from, subject, flags, email_id
    assert len(envelopes) == 3
    by_uid = {e["uid"]: e for e in envelopes}
    assert by_uid["103"]["from"] == "a@b.com"
    assert by_uid["103"]["subject"] == "hello"
    assert "\\Seen" in by_uid["103"]["flags"]
    assert by_uid["103"]["email_id"] == "alice@example.com:INBOX:103"


def test_fetch_envelopes_handles_empty_folder(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.search.return_value = []
    account.connect()

    assert account.fetch_envelopes("INBOX", n=10) == []


def test_fetch_headers_by_uids(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.fetch.return_value = {
        42: {
            b"FLAGS": (b"\\Seen",),
            b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
                b"From: x@y.com\r\nSubject: hi\r\n",
        },
    }
    account.connect()

    out = account.fetch_headers_by_uids("INBOX", ["42"])
    assert len(out) == 1
    assert out[0]["uid"] == "42"
    assert out[0]["from"] == "x@y.com"


def test_envelope_handles_non_ascii_keyword_flag(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """Custom keyword flags with non-ASCII bytes must not crash the parser."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.fetch.return_value = {
        7: {
            b"FLAGS": (b"\\Seen", b"\xe2\x98\x85important"),  # star-prefixed UTF-8
            b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
                b"From: a@b.com\r\nSubject: hi\r\n",
        },
    }
    account.connect()

    out = account.fetch_headers_by_uids("INBOX", ["7"])
    # Did not raise. Flag list contains the seen flag and a (possibly
    # mojibake'd) keyword flag — both are strings, no exception.
    assert len(out) == 1
    assert "\\Seen" in out[0]["flags"]
    assert all(isinstance(f, str) for f in out[0]["flags"])


def test_fetch_full_returns_body_and_attachments(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    raw = (
        b"From: a@b.com\r\nTo: alice@example.com\r\n"
        b"Subject: hello\r\nDate: Mon, 1 Jan 2026 00:00:00 +0000\r\n"
        b"Message-ID: <abc@xyz>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Hello body.\r\n"
    )
    instance.fetch.return_value = {
        42: {b"FLAGS": (b"\\Seen",), b"RFC822": raw},
    }
    account.connect()

    full = account.fetch_full("INBOX", "42")
    assert full is not None
    assert full["uid"] == "42"
    assert full["from"] == "a@b.com"
    assert full["body"].strip() == "Hello body."
    assert full["message_id"] == "<abc@xyz>"
    assert full["flags"] == ["\\Seen"]
    assert full["email_id"] == "alice@example.com:INBOX:42"


def test_fetch_full_returns_none_when_uid_missing(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.fetch.return_value = {}
    account.connect()
    assert account.fetch_full("INBOX", "42") is None


def test_search_returns_uids(mock_imapclient_class, account: IMAPAccount) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.search.return_value = [10, 20, 30]
    account.connect()

    uids = account.search("INBOX", "from:bob@x.com unseen")
    instance.search.assert_called_with([b"FROM", b"bob@x.com", b"UNSEEN"])
    assert uids == ["10", "20", "30"]


def test_store_flags_add_seen(mock_imapclient_class, account: IMAPAccount) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    account.connect()

    assert account.store_flags("INBOX", "42", ["\\Seen"]) is True
    instance.add_flags.assert_called_with([42], [b"\\Seen"])


def test_store_flags_remove_seen(mock_imapclient_class, account: IMAPAccount) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    account.connect()

    assert account.store_flags("INBOX", "42", ["\\Seen"], action="-FLAGS") is True
    instance.remove_flags.assert_called_with([42], [b"\\Seen"])


def test_move_message_uses_move_when_supported(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE", b"MOVE")
    instance.list_folders.return_value = []
    account.connect()

    assert account.move_message("INBOX", "42", "Archive") is True
    instance.move.assert_called_with([42], "Archive")


def test_move_message_falls_back_to_copy_delete(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)  # no MOVE
    instance.list_folders.return_value = []
    account.connect()

    assert account.move_message("INBOX", "42", "Archive") is True
    instance.copy.assert_called_with([42], "Archive")
    instance.add_flags.assert_called_with([42], [b"\\Deleted"])
    instance.expunge.assert_called()


def test_delete_message_moves_to_trash_when_available(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE", b"MOVE")
    instance.list_folders.return_value = [
        ((b"\\Trash",), b"/", "Trash"),
    ]
    account.connect()

    assert account.delete_message("INBOX", "42") is True
    instance.move.assert_called_with([42], "Trash")


def test_fetch_full_handles_non_ascii_keyword_flag(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """fetch_full must not crash on non-ASCII keyword flags."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.fetch.return_value = {
        7: {
            b"FLAGS": (b"\\Seen", b"\xe2\x98\x85important"),
            b"RFC822": b"From: a@b.com\r\nSubject: hi\r\n\r\nbody\r\n",
        },
    }
    account.connect()

    full = account.fetch_full("INBOX", "7")
    assert full is not None
    assert "\\Seen" in full["flags"]
    assert all(isinstance(f, str) for f in full["flags"])


def test_move_message_uses_uid_expunge_when_uidplus_available(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """When MOVE absent but UIDPLUS present, fallback uses uid_expunge."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE", b"UIDPLUS")  # no MOVE
    instance.list_folders.return_value = []
    account.connect()

    assert account.move_message("INBOX", "42", "Archive") is True
    instance.copy.assert_called_with([42], "Archive")
    instance.add_flags.assert_called_with([42], [b"\\Deleted"])
    instance.uid_expunge.assert_called_with([42])
    instance.expunge.assert_not_called()


def test_delete_message_uses_uid_expunge_when_uidplus_in_trash(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """delete_message in trash with UIDPLUS uses uid_expunge."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE", b"UIDPLUS")
    # Make INBOX the only folder and trash absent so we hit the in-place path.
    instance.list_folders.return_value = []
    account.connect()

    assert account.delete_message("INBOX", "42") is True
    instance.add_flags.assert_called_with([42], [b"\\Deleted"])
    instance.uid_expunge.assert_called_with([42])
    instance.expunge.assert_not_called()


def test_search_invalid_date_skipped(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """Malformed since: date is logged and skipped, not raised."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.search.return_value = []
    account.connect()

    # Malformed date — should not raise
    out = account.search("INBOX", "since:not-a-date from:bob@x.com")
    # Only the from: term made it into the criteria
    instance.search.assert_called_with([b"FROM", b"bob@x.com"])
    assert out == []


def test_store_flags_rejects_unknown_action(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    account.connect()

    assert account.store_flags("INBOX", "42", ["\\Seen"], action="bogus") is False
    instance.add_flags.assert_not_called()
    instance.remove_flags.assert_not_called()
    instance.set_flags.assert_not_called()


def test_store_flags_rejects_non_ascii_flag(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    account.connect()

    assert account.store_flags("INBOX", "42", ["重要"]) is False
    instance.add_flags.assert_not_called()
