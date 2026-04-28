"""Tests for one-shot legacy state cleanup."""
from __future__ import annotations

import json
from pathlib import Path

from lingtai.addons.imap._migrate import migrate_legacy_state


def test_no_state_file_is_noop(tmp_path: Path) -> None:
    # Should not raise, should not create files.
    migrate_legacy_state(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_legacy_processed_uids_file_deleted(tmp_path: Path) -> None:
    # Old shape: dict-of-list-of-int, no "last_delivered_uid" key.
    legacy = tmp_path / "alice@gmail.com.state.json"
    legacy.write_text(json.dumps({"INBOX": [101, 102, 103]}))

    deleted = migrate_legacy_state(tmp_path)
    assert deleted == [legacy]
    assert not legacy.exists()


def test_new_shape_state_is_preserved(tmp_path: Path) -> None:
    # New shape has last_delivered_uid — leave it alone.
    new = tmp_path / "alice@gmail.com.state.json"
    new.write_text(json.dumps({
        "INBOX": {"uidvalidity": 1, "last_delivered_uid": 100}
    }))
    deleted = migrate_legacy_state(tmp_path)
    assert deleted == []
    assert new.exists()


def test_idempotent_on_second_run(tmp_path: Path) -> None:
    legacy = tmp_path / "alice@gmail.com.state.json"
    legacy.write_text(json.dumps({"INBOX": [101]}))
    migrate_legacy_state(tmp_path)
    # Second run with nothing to do
    assert migrate_legacy_state(tmp_path) == []


def test_unrelated_files_ignored(tmp_path: Path) -> None:
    (tmp_path / "contacts.json").write_text("[]")
    (tmp_path / "INBOX").mkdir()
    deleted = migrate_legacy_state(tmp_path)
    assert deleted == []
    assert (tmp_path / "contacts.json").exists()
