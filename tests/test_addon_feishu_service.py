"""Tests for lingtai.addons.feishu.service — FeishuService."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai.addons.feishu.service import FeishuService


def _service(tmp_path: Path, accounts: list[dict] | None = None) -> FeishuService:
    if accounts is None:
        accounts = [
            {
                "alias": "bot1",
                "app_id": "cli_aaa",
                "app_secret": "s1",
                "allowed_users": None,
            }
        ]
    on_msg = MagicMock()
    return FeishuService(
        working_dir=tmp_path,
        accounts_config=accounts,
        on_message=on_msg,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_single_account(tmp_path):
    svc = _service(tmp_path)
    assert svc.list_accounts() == ["bot1"]
    assert svc.default_account.alias == "bot1"


def test_multi_account(tmp_path):
    svc = _service(tmp_path, accounts=[
        {"alias": "a1", "app_id": "cli_aaa", "app_secret": "s1",
         "allowed_users": None},
        {"alias": "a2", "app_id": "cli_bbb", "app_secret": "s2",
         "allowed_users": None},
    ])
    accounts = svc.list_accounts()
    assert accounts == ["a1", "a2"]


def test_get_account_found(tmp_path):
    svc = _service(tmp_path)
    acct = svc.get_account("bot1")
    assert acct.alias == "bot1"


def test_get_account_missing(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(KeyError):
        svc.get_account("nonexistent")


def test_default_account_is_first(tmp_path):
    svc = _service(tmp_path, accounts=[
        {"alias": "first", "app_id": "cli_111", "app_secret": "s",
         "allowed_users": None},
        {"alias": "second", "app_id": "cli_222", "app_secret": "s",
         "allowed_users": None},
    ])
    assert svc.default_account.alias == "first"


# ---------------------------------------------------------------------------
# Lifecycle delegation
# ---------------------------------------------------------------------------

def test_start_delegates_to_all_accounts(tmp_path):
    svc = _service(tmp_path, accounts=[
        {"alias": "a1", "app_id": "cli_aaa", "app_secret": "s1",
         "allowed_users": None},
        {"alias": "a2", "app_id": "cli_bbb", "app_secret": "s2",
         "allowed_users": None},
    ])
    for acct in svc._accounts.values():
        acct.start = MagicMock()
    svc.start()
    for acct in svc._accounts.values():
        acct.start.assert_called_once()


def test_stop_delegates_to_all_accounts(tmp_path):
    svc = _service(tmp_path, accounts=[
        {"alias": "a1", "app_id": "cli_aaa", "app_secret": "s1",
         "allowed_users": None},
        {"alias": "a2", "app_id": "cli_bbb", "app_secret": "s2",
         "allowed_users": None},
    ])
    for acct in svc._accounts.values():
        acct.stop = MagicMock()
    svc.stop()
    for acct in svc._accounts.values():
        acct.stop.assert_called_once()


# ---------------------------------------------------------------------------
# on_message routing
# ---------------------------------------------------------------------------

def test_on_message_callback_stored(tmp_path):
    received = []
    svc = FeishuService(
        working_dir=tmp_path,
        accounts_config=[
            {"alias": "bot1", "app_id": "cli_aaa", "app_secret": "s",
             "allowed_users": None}
        ],
        on_message=lambda alias, event: received.append((alias, event)),
    )
    # Simulate direct invocation of the callback stored in the account
    fake_event = MagicMock()
    svc.get_account("bot1")._on_message("bot1", fake_event)
    assert len(received) == 1
    assert received[0][0] == "bot1"


# ---------------------------------------------------------------------------
# State directory isolation
# ---------------------------------------------------------------------------

def test_state_dirs_are_isolated(tmp_path):
    """Each account gets its own subdirectory under working_dir/feishu/."""
    svc = _service(tmp_path, accounts=[
        {"alias": "a1", "app_id": "cli_aaa", "app_secret": "s1",
         "allowed_users": None},
        {"alias": "a2", "app_id": "cli_bbb", "app_secret": "s2",
         "allowed_users": None},
    ])
    state_a1 = svc._accounts["a1"]._state_dir
    state_a2 = svc._accounts["a2"]._state_dir
    assert state_a1 != state_a2
    assert state_a1 == tmp_path / "feishu" / "a1"
    assert state_a2 == tmp_path / "feishu" / "a2"
