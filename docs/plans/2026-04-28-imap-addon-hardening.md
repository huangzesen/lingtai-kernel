# IMAP Addon Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `imaplib`-based IMAP addon with `imapclient`-based implementation that reliably wakes the agent on new mail (UIDNEXT watermark, robust IDLE loop, real `connected` status).

**Architecture:** Two `imapclient.IMAPClient` connections per account (lock-protected tool-call connection + dedicated listener connection). Listener uses 9-min IDLE slices with a 29-min hard re-issue cap, NOOP keep-alive on silent slices, reconcile-on-(re)connect, UIDNEXT watermark per (account, folder) persisted atomically. Bootstrap delivers UNSEEN once; thereafter SEEN flag is never read.

**Tech Stack:** Python 3.11+, `imapclient>=3.0`, `smtplib` (unchanged), `pytest` with `unittest.mock` for mocked-IMAPClient tests.

**Spec:** `docs/specs/2026-04-28-imap-addon-hardening.md`

**Working directory:** `~/Documents/GitHub/lingtai-kernel/` (sibling repo, not the `lingtai` TUI repo where this plan was authored).

---

## File Structure

| File | Purpose | Status |
|---|---|---|
| `pyproject.toml` | add `imapclient>=3.0` dep | modify |
| `src/lingtai/addons/imap/__init__.py` | unchanged surface (`setup()`) | unchanged |
| `src/lingtai/addons/imap/service.py` | unchanged surface (`IMAPMailService`) | unchanged |
| `src/lingtai/addons/imap/manager.py` | minor — `_accounts()` returns richer status | modify |
| `src/lingtai/addons/imap/account.py` | full rewrite — imapclient + watermark + robust loop | rewrite |
| `src/lingtai/addons/imap/_watermark.py` | NEW — UID watermark state load/save (~80 lines) | create |
| `src/lingtai/addons/imap/_migrate.py` | NEW — one-shot legacy state cleanup (~40 lines) | create |
| `src/lingtai/addons/imap/manual/SKILL.md` | drop "known display bug" caveat | modify |
| `tests/test_addon_imap_watermark.py` | NEW — pure unit tests for `_watermark.py` | create |
| `tests/test_addon_imap_migrate.py` | NEW — pure unit tests for `_migrate.py` | create |
| `tests/test_addon_imap_account.py` | rewrite — mocked-IMAPClient tests | rewrite |
| `tests/test_addon_imap_manager.py` | minor — `_accounts()` shape change | modify |
| `tests/test_addon_imap_service.py` | unchanged | unchanged |
| `tests/test_addon_imap_live.py` | NEW — end-to-end live test, gated by `IMAP_LIVE_TEST=1` | create |

The boundary between `account.py` and `_watermark.py`: `account.py` knows IMAP, threads, and connection lifecycle; `_watermark.py` knows nothing but JSON files and integers. `_migrate.py` runs once at addon load and is import-time stateless.

---

## Task 1: Add `imapclient` dependency and verify install

**Files:**
- Modify: `pyproject.toml` (the `dependencies = [...]` list)

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`. Inside the existing `dependencies = [...]` list, append `"imapclient>=3.0",`. The current list ends with `"httpx>=0.27",` (duplicate of the first entry — leave as-is, not our cleanup). Final list:

```toml
dependencies = [
    "httpx>=0.27",
    "openai>=1.0",
    "anthropic>=0.40",
    "google-genai>=1.0",
    "mcp>=1.0",
    "ddgs>=7.0",
    "trafilatura>=2.0",
    "filelock>=3.0",
    "httpx>=0.27",
    "imapclient>=3.0",
]
```

- [ ] **Step 2: Install in editable mode**

Run from `~/Documents/GitHub/lingtai-kernel/`:

```bash
pip install -e .
```

Expected: install completes with `imapclient` listed among installed packages. No errors.

- [ ] **Step 3: Smoke import**

Run:

```bash
python -c "import imapclient; print(imapclient.__version__)"
```

Expected: prints `3.0.x` or higher. No `ModuleNotFoundError`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat(imap): add imapclient dependency for hardened addon"
```

---

## Task 2: Watermark state — failing test

**Files:**
- Test: `tests/test_addon_imap_watermark.py` (create)

- [ ] **Step 1: Write the first failing test**

Create `tests/test_addon_imap_watermark.py` with:

```python
"""Tests for the per-(account, folder) UIDNEXT watermark store."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lingtai.addons.imap._watermark import WatermarkStore


def test_load_returns_empty_when_no_file(tmp_path: Path) -> None:
    store = WatermarkStore(tmp_path / "missing.json")
    assert store.load() == {}


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = WatermarkStore(path)
    store.save({"INBOX": {"uidvalidity": 12345, "last_delivered_uid": 4821}})

    reloaded = WatermarkStore(path).load()
    assert reloaded == {"INBOX": {"uidvalidity": 12345, "last_delivered_uid": 4821}}


def test_save_is_atomic_no_partial_file_on_crash(tmp_path: Path, monkeypatch) -> None:
    """If os.replace fails, the original file must remain untouched."""
    path = tmp_path / "state.json"
    store = WatermarkStore(path)
    store.save({"INBOX": {"uidvalidity": 1, "last_delivered_uid": 100}})

    import os
    original_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save({"INBOX": {"uidvalidity": 1, "last_delivered_uid": 200}})

    # Original file is unchanged
    monkeypatch.setattr(os, "replace", original_replace)
    assert WatermarkStore(path).load() == {
        "INBOX": {"uidvalidity": 1, "last_delivered_uid": 100}
    }
    # No leftover .tmp files
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_load_corrupt_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")
    assert WatermarkStore(path).load() == {}
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_watermark.py -v
```

Expected: 4 failures with `ModuleNotFoundError: No module named 'lingtai.addons.imap._watermark'`.

---

## Task 3: Watermark state — implementation

**Files:**
- Create: `src/lingtai/addons/imap/_watermark.py`

- [ ] **Step 1: Write the implementation**

Create `src/lingtai/addons/imap/_watermark.py`:

```python
"""Per-(account, folder) UIDNEXT watermark store.

State file shape::

    {
      "<folder>": {
        "uidvalidity": <int>,
        "last_delivered_uid": <int>
      },
      ...
    }

Atomic on POSIX and Windows via tmp-file + os.replace.
Corrupt or missing files are treated as empty — the addon will rebootstrap.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class WatermarkStore:
    """Tiny JSON-on-disk persistence for UID watermarks."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, dict]:
        """Return the persisted dict, or {} if missing/corrupt."""
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, state: dict[str, dict]) -> None:
        """Atomically replace the state file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp",
        )
        try:
            os.write(fd, json.dumps(state, indent=2).encode("utf-8"))
            os.close(fd)
            os.replace(tmp, str(self._path))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise
```

- [ ] **Step 2: Run tests, expect pass**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_watermark.py -v
```

Expected: 4 passed.

- [ ] **Step 3: Smoke import**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "from lingtai.addons.imap._watermark import WatermarkStore; print(WatermarkStore)"
```

Expected: prints `<class 'lingtai.addons.imap._watermark.WatermarkStore'>`. No errors.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/_watermark.py tests/test_addon_imap_watermark.py
git commit -m "feat(imap): add WatermarkStore for atomic UIDNEXT persistence"
```

---

## Task 4: Legacy-state migration — failing test

**Files:**
- Test: `tests/test_addon_imap_migrate.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_addon_imap_migrate.py`:

```python
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
```

- [ ] **Step 2: Run test, verify failure**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_migrate.py -v
```

Expected: failures with `ModuleNotFoundError: No module named 'lingtai.addons.imap._migrate'`.

---

## Task 5: Legacy-state migration — implementation

**Files:**
- Create: `src/lingtai/addons/imap/_migrate.py`

- [ ] **Step 1: Write the implementation**

Create `src/lingtai/addons/imap/_migrate.py`:

```python
"""One-shot legacy state cleanup for the IMAP addon.

The pre-rewrite addon persisted a `_processed_uids` dict-of-set per account.
After the rewrite, the new shape is a dict-of-(uidvalidity, last_delivered_uid).
This module deletes legacy state files at addon load. Idempotent.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_legacy_state(state_dir: Path | str) -> list[Path]:
    """Delete any legacy `<address>.state.json` files in `state_dir`.

    A file is "legacy" iff it parses as JSON and none of its top-level
    folder entries have a `last_delivered_uid` key. New-shape files are
    preserved, unparseable files are preserved (treated as opaque user
    data — better safe than sorry).

    Returns the list of paths that were deleted.
    """
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return []

    deleted: list[Path] = []
    for path in state_dir.iterdir():
        if not path.is_file():
            continue
        if not path.name.endswith(".state.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        # New shape: every value is a dict with last_delivered_uid.
        is_new_shape = all(
            isinstance(v, dict) and "last_delivered_uid" in v
            for v in data.values()
        )
        if is_new_shape and data:
            continue
        try:
            path.unlink()
            deleted.append(path)
            logger.info("imap: removed legacy state file %s", path)
        except OSError:
            pass
    return deleted
```

- [ ] **Step 2: Run tests, expect pass**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_migrate.py -v
```

Expected: 5 passed.

- [ ] **Step 3: Smoke import**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "from lingtai.addons.imap._migrate import migrate_legacy_state; print(migrate_legacy_state)"
```

Expected: prints `<function migrate_legacy_state at ...>`. No errors.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/_migrate.py tests/test_addon_imap_migrate.py
git commit -m "feat(imap): one-shot cleanup of pre-rewrite state files"
```

---

## Task 6: New `IMAPAccount` skeleton — failing test for connection lifecycle

**Files:**
- Test: `tests/test_addon_imap_account.py` (rewrite — preserve only the file path, replace content)

> Note: the existing `tests/test_addon_imap_account.py` is 1009 lines of `imaplib`-shaped mocks. We replace it wholesale because the test scaffolding cannot survive the library swap.

- [ ] **Step 1: Stash the old file** (so it's preserved in git history but out of the test runner's way during the rewrite):

```bash
cd ~/Documents/GitHub/lingtai-kernel
git mv tests/test_addon_imap_account.py tests/test_addon_imap_account.legacy.py.bak
git commit -m "chore(imap): stash legacy imaplib-shaped account tests pre-rewrite"
```

- [ ] **Step 2: Create the new test file with the first connection-lifecycle test**

Create `tests/test_addon_imap_account.py`:

```python
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
```

- [ ] **Step 3: Run, verify failure**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v
```

Expected: errors / failures — either `ImportError` if `IMAPClient` isn't imported in `account.py` yet, or `AttributeError` because the methods we're calling don't exist in the imapclient form yet.

---

## Task 7: New `IMAPAccount` — connection lifecycle implementation

**Files:**
- Modify: `src/lingtai/addons/imap/account.py`

> This is the largest single task in the plan. We replace the entire `IMAPAccount` class. The first replacement covers __init__, connect/disconnect, capability/folder discovery, and the new `connected` property. Tool-call methods and the listener loop come in subsequent tasks.

- [ ] **Step 1: Replace the file**

Overwrite `src/lingtai/addons/imap/account.py` with the new skeleton. (Full file shown — long but complete.)

```python
"""IMAP account — imapclient-based, multi-connection, watermark-driven.

One IMAPAccount owns:
  - a tool-call IMAPClient (lock-protected, used by manager actions)
  - a listener IMAPClient (dedicated thread, IDLE loop)
  - a WatermarkStore (per-(account, folder) UIDNEXT)

The on-message callback for new arrivals is registered via
``start_listening(on_message)`` and invoked from the listener thread.
"""
from __future__ import annotations

import email as email_mod
import email.policy as email_policy
import logging
import mimetypes
import re
import smtplib
import socket
import threading
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Callable

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from ._watermark import WatermarkStore

logger = logging.getLogger(__name__)

_SPECIAL_USE_ROLES = {
    b"\\Trash": "trash",
    b"\\Sent": "sent",
    b"\\Drafts": "drafts",
    b"\\Junk": "junk",
    b"\\All": "archive",
    b"\\Archive": "archive",
}
_NAME_HEURISTICS = {
    "trash": "trash", "deleted": "trash", "[gmail]/trash": "trash",
    "sent": "sent", "[gmail]/sent mail": "sent",
    "drafts": "drafts", "[gmail]/drafts": "drafts",
    "spam": "junk", "junk": "junk", "[gmail]/spam": "junk",
    "archive": "archive", "[gmail]/all mail": "archive",
}


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_text_body(msg: email_mod.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    return part.get_content()
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return _strip_html_tags(part.get_content())
                except Exception:
                    pass
        return ""
    try:
        body = msg.get_content()
    except Exception:
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html_tags(body)
    return body or ""


def _strip_html_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def _extract_attachments(msg: email_mod.message.Message) -> list[dict]:
    attachments: list[dict] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        filename = part.get_filename() or "attachment"
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        attachments.append({
            "filename": _decode_header_value(filename),
            "content_type": part.get_content_type(),
            "data": data,
        })
    return attachments


class IMAPAccount:
    """One IMAP/SMTP account."""

    def __init__(
        self,
        email_address: str,
        email_password: str,
        *,
        imap_host: str = "imap.gmail.com",
        imap_port: int = 993,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
        working_dir: Path | str | None = None,
        allowed_senders: list[str] | None = None,
        poll_interval: int = 30,
    ) -> None:
        self._email_address = email_address
        self._email_password = email_password
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._working_dir = Path(working_dir) if working_dir else None
        self._allowed_senders = allowed_senders
        self._poll_interval = poll_interval

        # Tool-call connection
        self._tool_imap: IMAPClient | None = None
        self._lock = threading.Lock()

        # Listener connection (background thread only)
        self._listen_imap: IMAPClient | None = None
        self._listen_in_idle = False

        # Capabilities
        self._capabilities: set[bytes] = set()
        self._has_idle = False
        self._has_move = False
        self._has_uidplus = False

        # Folder discovery
        self._folders: dict[str, str | None] = {}
        self._folder_by_role: dict[str, str] = {}

        # Watermark
        self._watermark = WatermarkStore(self._state_path()) if self._state_path() else None

        # Reconnect backoff
        self._backoff_steps = [1, 2, 5, 10, 60]
        self._backoff_index = 0

        # Listener thread
        self._bg_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    # -- Properties ---------------------------------------------------------

    @property
    def address(self) -> str:
        return self._email_address

    @property
    def capabilities(self) -> set[str]:
        return {c.decode("ascii") if isinstance(c, bytes) else str(c)
                for c in self._capabilities}

    @property
    def has_idle(self) -> bool:
        return self._has_idle

    @property
    def has_move(self) -> bool:
        return self._has_move

    @property
    def has_uidplus(self) -> bool:
        return self._has_uidplus

    @property
    def folders(self) -> dict[str, str | None]:
        return dict(self._folders)

    @property
    def connected(self) -> bool:
        """True iff the tool-call connection is alive (NOOP succeeds)."""
        if self._tool_imap is None:
            return False
        try:
            with self._lock:
                self._tool_imap.noop()
            return True
        except Exception:
            return False

    @property
    def listening(self) -> bool:
        """True iff the listener thread is alive AND currently inside IDLE."""
        return (
            self._bg_thread is not None
            and self._bg_thread.is_alive()
            and self._listen_in_idle
        )

    # -- Connection lifecycle ----------------------------------------------

    def connect(self) -> None:
        """Open the tool-call connection, parse capabilities, discover folders."""
        client = IMAPClient(self._imap_host, port=self._imap_port, ssl=True)
        client.login(self._email_address, self._email_password)
        self._tool_imap = client
        self._fetch_capabilities()
        self._discover_folders()
        logger.info("IMAP connected: %s (%s)", self._email_address, self._imap_host)

    def disconnect(self) -> None:
        if self._tool_imap is not None:
            try:
                self._tool_imap.logout()
            except Exception:
                pass
            self._tool_imap = None

    def _ensure_connected(self) -> IMAPClient:
        if self._tool_imap is None:
            self.connect()
        assert self._tool_imap is not None
        return self._tool_imap

    def _fetch_capabilities(self) -> None:
        assert self._tool_imap is not None
        caps = set(self._tool_imap.capabilities())
        self._capabilities = caps
        self._has_idle = b"IDLE" in caps
        self._has_move = b"MOVE" in caps
        self._has_uidplus = b"UIDPLUS" in caps

    def _discover_folders(self) -> None:
        assert self._tool_imap is not None
        folders: dict[str, str | None] = {}
        folder_by_role: dict[str, str] = {}
        for entry in self._tool_imap.list_folders():
            attrs, _delim, name = entry
            role: str | None = None
            for attr in attrs:
                if attr in _SPECIAL_USE_ROLES:
                    role = _SPECIAL_USE_ROLES[attr]
                    break
            if not role:
                role = _NAME_HEURISTICS.get(name.lower())
            folders[name] = role
            if role and role not in folder_by_role:
                folder_by_role[role] = name
        self._folders = folders
        self._folder_by_role = folder_by_role

    def get_folder_by_role(self, role: str) -> str | None:
        return self._folder_by_role.get(role)

    # -- Watermark state ----------------------------------------------------

    def _state_path(self) -> Path | None:
        if self._working_dir is None:
            return None
        # Per-account file: working_dir/imap/<address>.state.json
        return self._working_dir / "imap" / f"{self._email_address}.state.json"

    # -- Tool-call methods (added in subsequent tasks) ---------------------

    # -- Listener (added in subsequent tasks) ------------------------------

    def start_listening(self, on_message: Callable[[list[dict]], None]) -> None:
        raise NotImplementedError("filled in Task 11")

    def stop_listening(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._bg_thread is not None:
            self._bg_thread.join(timeout=15.0)
            self._bg_thread = None
```

- [ ] **Step 2: Run the connection-lifecycle tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v
```

Expected: 3 passed (`test_connect_logs_in_and_caches_capabilities`, `test_disconnect_logs_out_and_marks_disconnected`, `test_connected_reports_false_when_noop_fails`).

- [ ] **Step 3: Smoke import the addon module**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "from lingtai.addons.imap.account import IMAPAccount; print(IMAPAccount)"
```

Expected: prints class. No errors.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/account.py tests/test_addon_imap_account.py
git commit -m "feat(imap): rewrite IMAPAccount on imapclient — connection lifecycle"
```

---

## Task 8: Header / envelope fetch — failing test

**Files:**
- Test: `tests/test_addon_imap_account.py` (append)

- [ ] **Step 1: Append tests for header fetch**

Add to the bottom of `tests/test_addon_imap_account.py`:

```python
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
```

- [ ] **Step 2: Run, expect failure**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v -k "fetch"
```

Expected: 3 failures, `AttributeError: 'IMAPAccount' object has no attribute 'fetch_envelopes'` (or similar).

---

## Task 9: Header / envelope fetch — implementation

**Files:**
- Modify: `src/lingtai/addons/imap/account.py`

- [ ] **Step 1: Add the methods**

Inside the `IMAPAccount` class, replace the `# -- Tool-call methods (added in subsequent tasks) --` placeholder section with:

```python
    # -- Tool-call methods --------------------------------------------------

    _HEADER_FETCH_KEY = b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]"

    def fetch_envelopes(self, folder: str, n: int = 20) -> list[dict]:
        """Return headers for the N most recent UIDs in `folder`."""
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder, readonly=True)
            all_uids = imap.search("ALL")
        if not all_uids:
            return []
        recent = all_uids[-n:] if n > 0 else all_uids
        return self.fetch_headers_by_uids(folder, [str(u) for u in recent])

    def fetch_headers_by_uids(
        self, folder: str, uids: list[str],
    ) -> list[dict]:
        """Fetch headers for explicit UIDs."""
        if not uids:
            return []
        int_uids = [int(u) for u in uids]
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder, readonly=True)
            data = imap.fetch(int_uids, ["FLAGS", "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]"])
        return [self._envelope_from_fetch(uid, info, folder)
                for uid, info in sorted(data.items())]

    def _envelope_from_fetch(
        self, uid: int, info: dict, folder: str,
    ) -> dict:
        flags_raw = info.get(b"FLAGS", ())
        flags = [f.decode("ascii") if isinstance(f, bytes) else str(f)
                 for f in flags_raw]
        header_bytes = info.get(self._HEADER_FETCH_KEY, b"") or b""
        msg = email_mod.message_from_bytes(
            header_bytes, policy=email_policy.default,
        )
        return {
            "uid": str(uid),
            "from": _decode_header_value(msg.get("From", "")),
            "to": _decode_header_value(msg.get("To", "")),
            "subject": _decode_header_value(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "flags": flags,
            "email_id": f"{self._email_address}:{folder}:{uid}",
        }
```

- [ ] **Step 2: Run tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v -k "fetch"
```

Expected: 3 passed.

- [ ] **Step 3: Smoke import**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "from lingtai.addons.imap.account import IMAPAccount; a = IMAPAccount.__init__; print('OK')"
```

Expected: prints `OK`.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/account.py tests/test_addon_imap_account.py
git commit -m "feat(imap): fetch_envelopes and fetch_headers_by_uids on imapclient"
```

---

## Task 10: Full-message fetch + search + flag/move/delete — failing tests

**Files:**
- Test: `tests/test_addon_imap_account.py` (append)

- [ ] **Step 1: Append the tests**

```python
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
```

- [ ] **Step 2: Run, expect failures**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v
```

Expected: 8 new failures (`fetch_full`, `search`, `store_flags`, `move_message`, `delete_message` missing).

---

## Task 11: Full-message + search + flag/move/delete — implementation

**Files:**
- Modify: `src/lingtai/addons/imap/account.py`

- [ ] **Step 1: Add the methods**

Append to the `IMAPAccount` class after `_envelope_from_fetch`:

```python
    def fetch_full(self, folder: str, uid: str) -> dict | None:
        """Fetch the full message for a single UID."""
        uid_int = int(uid)
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder, readonly=True)
            data = imap.fetch([uid_int], ["FLAGS", "RFC822"])
        info = data.get(uid_int)
        if not info:
            return None

        flags_raw = info.get(b"FLAGS", ())
        flags = [f.decode("ascii") if isinstance(f, bytes) else str(f)
                 for f in flags_raw]
        raw_email = info.get(b"RFC822", b"")
        msg = email_mod.message_from_bytes(raw_email, policy=email_policy.default)

        from_raw = msg.get("From", "")
        _, from_addr = parseaddr(from_raw)
        attachments = _extract_attachments(msg)
        attachment_info = [{
            "filename": a["filename"],
            "content_type": a["content_type"],
            "size": len(a["data"]),
        } for a in attachments]

        return {
            "uid": str(uid_int),
            "from": _decode_header_value(from_raw),
            "from_address": from_addr,
            "to": _decode_header_value(msg.get("To", "")),
            "subject": _decode_header_value(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "body": _extract_text_body(msg),
            "attachments": attachment_info,
            "attachments_raw": attachments,
            "flags": flags,
            "message_id": msg.get("Message-ID", ""),
            "in_reply_to": msg.get("In-Reply-To", ""),
            "references": msg.get("References", ""),
            "email_id": f"{self._email_address}:{folder}:{uid_int}",
        }

    def search(self, folder: str, query: str) -> list[str]:
        """Server-side IMAP SEARCH with our DSL."""
        criteria = self._build_search_criteria(query)
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder, readonly=True)
            uids = imap.search(criteria)
        return [str(u) for u in uids]

    @staticmethod
    def _build_search_criteria(query: str) -> list[bytes]:
        """Translate our query DSL into imapclient SEARCH criteria.

        Supports: from:<addr> subject:<text> since:YYYY-MM-DD
                  before:YYYY-MM-DD flagged unseen seen
        Multiple terms AND-ed.
        """
        from datetime import datetime
        criteria: list[bytes] = []
        # Split on whitespace, but keep "key:value" tokens together
        tokens = re.findall(r'(\w+):"([^"]+)"|(\w+):(\S+)|(\S+)', query.strip())
        for grp in tokens:
            if grp[0] and grp[1]:
                key, val = grp[0].lower(), grp[1]
            elif grp[2] and grp[3]:
                key, val = grp[2].lower(), grp[3]
            else:
                key, val = grp[4].lower(), ""
            if key == "from" and val:
                criteria += [b"FROM", val.encode()]
            elif key == "to" and val:
                criteria += [b"TO", val.encode()]
            elif key == "subject" and val:
                criteria += [b"SUBJECT", val.encode()]
            elif key == "since" and val:
                d = datetime.strptime(val, "%Y-%m-%d")
                criteria += [b"SINCE", d.strftime("%d-%b-%Y").encode()]
            elif key == "before" and val:
                d = datetime.strptime(val, "%Y-%m-%d")
                criteria += [b"BEFORE", d.strftime("%d-%b-%Y").encode()]
            elif key == "flagged":
                criteria.append(b"FLAGGED")
            elif key == "unseen":
                criteria.append(b"UNSEEN")
            elif key == "seen":
                criteria.append(b"SEEN")
        return criteria or [b"ALL"]

    def store_flags(
        self, folder: str, uid: str, flags: list[str], action: str = "+FLAGS",
    ) -> bool:
        flag_bytes = [f.encode("ascii") for f in flags]
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder)
            try:
                if action == "+FLAGS":
                    imap.add_flags([int(uid)], flag_bytes)
                elif action == "-FLAGS":
                    imap.remove_flags([int(uid)], flag_bytes)
                else:
                    imap.set_flags([int(uid)], flag_bytes)
                return True
            except IMAPClientError:
                return False

    def mark_seen(self, folder: str, uid: str) -> bool:
        return self.store_flags(folder, uid, ["\\Seen"])

    def mark_unseen(self, folder: str, uid: str) -> bool:
        return self.store_flags(folder, uid, ["\\Seen"], action="-FLAGS")

    def mark_flagged(self, folder: str, uid: str) -> bool:
        return self.store_flags(folder, uid, ["\\Flagged"])

    def list_folders(self) -> dict[str, str]:
        return {k: v for k, v in self._folders.items() if v is not None}

    def move_message(self, folder: str, uid: str, dest_folder: str) -> bool:
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder)
            try:
                if self._has_move:
                    imap.move([int(uid)], dest_folder)
                else:
                    imap.copy([int(uid)], dest_folder)
                    imap.add_flags([int(uid)], [b"\\Deleted"])
                    imap.expunge()
                return True
            except IMAPClientError as e:
                logger.warning("move failed: %s", e)
                return False

    def delete_message(self, folder: str, uid: str) -> bool:
        trash = self.get_folder_by_role("trash")
        if trash and folder != trash:
            return self.move_message(folder, uid, trash)
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder)
            try:
                imap.add_flags([int(uid)], [b"\\Deleted"])
                imap.expunge()
                return True
            except IMAPClientError:
                return False
```

- [ ] **Step 2: Run all account tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v
```

Expected: all passed.

- [ ] **Step 3: Smoke import**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "from lingtai.addons.imap.account import IMAPAccount; print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/account.py tests/test_addon_imap_account.py
git commit -m "feat(imap): full message fetch, search DSL, flag/move/delete on imapclient"
```

---

## Task 12: SMTP `send_email` — failing test, then port from old code

**Files:**
- Test: `tests/test_addon_imap_account.py` (append)
- Modify: `src/lingtai/addons/imap/account.py`

> SMTP code is unchanged in semantics — pure copy from the old `account.py`. The test exists to lock the surface so future changes don't break it.

- [ ] **Step 1: Add a smoke test that locks the SMTP signature**

Append to `tests/test_addon_imap_account.py`:

```python
def test_send_email_rejects_empty(account: IMAPAccount) -> None:
    err = account.send_email(to=["x@y.com"], subject="", body="")
    assert err is not None and "empty" in err.lower()


def test_send_email_signature(account: IMAPAccount) -> None:
    """Validate kwargs accepted by send_email — guards against accidental signature drift."""
    import inspect
    sig = inspect.signature(account.send_email)
    params = sig.parameters
    assert "to" in params
    assert "subject" in params
    assert "body" in params
    assert "cc" in params
    assert "bcc" in params
    assert "attachments" in params
    assert "in_reply_to" in params
    assert "references" in params
```

- [ ] **Step 2: Run, expect failure**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v -k "send_email"
```

Expected: failures (`AttributeError: 'IMAPAccount' object has no attribute 'send_email'`).

- [ ] **Step 3: Add the method**

Append to `IMAPAccount`:

```python
    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        *,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> str | None:
        if not subject and not body and not attachments:
            return "Cannot send empty email (no subject, no body, and no attachments)"
        if attachments:
            for filepath in attachments:
                if not Path(filepath).is_file():
                    return f"Attachment not found: {filepath}"
        try:
            if attachments:
                mime_msg = MIMEMultipart()
                mime_msg.attach(MIMEText(body, "plain", "utf-8"))
                for filepath in attachments:
                    path = Path(filepath)
                    content_type, _ = mimetypes.guess_type(str(path))
                    if content_type is None:
                        content_type = "application/octet-stream"
                    maintype, subtype = content_type.split("/", 1)
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(path.read_bytes())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition", "attachment", filename=path.name,
                    )
                    mime_msg.attach(part)
            else:
                mime_msg = MIMEText(body, "plain", "utf-8")

            mime_msg["From"] = formataddr(("", self._email_address))
            mime_msg["To"] = ", ".join(to)
            mime_msg["Subject"] = subject
            mime_msg["Date"] = formatdate(localtime=True)
            mime_msg["Message-ID"] = make_msgid()
            if cc:
                mime_msg["CC"] = ", ".join(cc)
            if in_reply_to:
                mime_msg["In-Reply-To"] = in_reply_to
            if references:
                mime_msg["References"] = references

            all_recipients = list(to)
            if cc:
                all_recipients.extend(cc)
            if bcc:
                all_recipients.extend(bcc)

            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.starttls()
                server.login(self._email_address, self._email_password)
                server.sendmail(
                    self._email_address, all_recipients, mime_msg.as_string(),
                )
            return None
        except Exception as e:
            error = f"SMTP send failed: {e}"
            logger.error(error)
            return error
```

- [ ] **Step 4: Run all tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/lingtai/addons/imap/account.py tests/test_addon_imap_account.py
git commit -m "feat(imap): port send_email — unchanged semantics"
```

---

## Task 13: UID-watermark reconcile — failing tests

**Files:**
- Test: `tests/test_addon_imap_account.py` (append)

- [ ] **Step 1: Append the reconcile tests**

```python
def test_reconcile_first_run_bootstraps_from_uidnext_and_unseen(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """First run: no state file. Bootstrap delivers current UNSEEN once,
    then sets watermark to current UIDNEXT. UNSEEN flag is consulted only here."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 12345, b"UIDNEXT": 5000}
    instance.search.return_value = [4998, 4999]  # currently UNSEEN
    instance.fetch.return_value = {
        4998: {b"FLAGS": (), b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
               b"From: a@b.com\r\nSubject: one\r\n"},
        4999: {b"FLAGS": (), b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
               b"From: c@d.com\r\nSubject: two\r\n"},
    }
    account.connect()

    delivered = account.reconcile("INBOX")

    instance.search.assert_called_with(b"UNSEEN")
    assert {e["uid"] for e in delivered} == {"4998", "4999"}
    # Watermark persisted at UIDNEXT - 1
    assert account._watermark.load() == {
        "INBOX": {"uidvalidity": 12345, "last_delivered_uid": 4999}
    }


def test_reconcile_normal_path_uses_uid_range(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """Subsequent runs: watermark exists, search by UID range, deliver new only."""
    # Pre-seed watermark
    account._watermark.save({"INBOX": {"uidvalidity": 12345, "last_delivered_uid": 4999}})

    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 12345, b"UIDNEXT": 5003}
    instance.search.return_value = [5001, 5002]
    instance.fetch.return_value = {
        5001: {b"FLAGS": (), b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
               b"From: a@b.com\r\nSubject: new1\r\n"},
        5002: {b"FLAGS": (), b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
               b"From: c@d.com\r\nSubject: new2\r\n"},
    }
    account.connect()

    delivered = account.reconcile("INBOX")

    instance.search.assert_called_with([b"UID", b"5000:*"])
    assert {e["uid"] for e in delivered} == {"5001", "5002"}
    assert account._watermark.load()["INBOX"]["last_delivered_uid"] == 5002


def test_reconcile_uidvalidity_change_resets_and_delivers_nothing(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """UIDVALIDITY mismatch → reset watermark, deliver nothing this round."""
    account._watermark.save({"INBOX": {"uidvalidity": 12345, "last_delivered_uid": 5000}})

    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 99999, b"UIDNEXT": 200}
    account.connect()

    delivered = account.reconcile("INBOX")

    assert delivered == []
    assert account._watermark.load() == {
        "INBOX": {"uidvalidity": 99999, "last_delivered_uid": 199}
    }


def test_reconcile_no_new_mail_is_noop(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    account._watermark.save({"INBOX": {"uidvalidity": 12345, "last_delivered_uid": 5000}})

    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 12345, b"UIDNEXT": 5001}
    instance.search.return_value = []
    account.connect()

    assert account.reconcile("INBOX") == []
    # Watermark untouched
    assert account._watermark.load()["INBOX"]["last_delivered_uid"] == 5000
```

- [ ] **Step 2: Run, expect failures**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v -k "reconcile"
```

Expected: 4 failures, `'IMAPAccount' object has no attribute 'reconcile'`.

---

## Task 14: UID-watermark reconcile — implementation

**Files:**
- Modify: `src/lingtai/addons/imap/account.py`

- [ ] **Step 1: Add the reconcile method**

Append to the `IMAPAccount` class (just before the listener placeholders):

```python
    # -- UID watermark reconcile --------------------------------------------

    def reconcile(self, folder: str = "INBOX") -> list[dict]:
        """Detect and return new headers since last watermark.

        Returns a list of envelope dicts (same shape as fetch_headers_by_uids)
        for messages that have not yet been delivered. Updates the watermark
        atomically before returning.

        On UIDVALIDITY change, resets state for the folder and returns [].
        On first call (no state), bootstraps by delivering current UNSEEN
        once and setting the watermark to UIDNEXT-1.
        """
        if self._watermark is None:
            return []

        with self._lock:
            imap = self._ensure_connected()
            status = imap.folder_status(
                folder, [b"UIDVALIDITY", b"UIDNEXT"],
            )
        uidvalidity = int(status[b"UIDVALIDITY"])
        uidnext = int(status[b"UIDNEXT"])

        state = self._watermark.load()
        folder_state = state.get(folder)

        # Bootstrap: no state for this folder
        if folder_state is None:
            unseen_envelopes = self._bootstrap_deliver_unseen(folder)
            state[folder] = {
                "uidvalidity": uidvalidity,
                "last_delivered_uid": max(
                    (int(e["uid"]) for e in unseen_envelopes),
                    default=uidnext - 1,
                ),
            }
            self._watermark.save(state)
            return unseen_envelopes

        # UIDVALIDITY change: reset, deliver nothing
        if folder_state.get("uidvalidity") != uidvalidity:
            logger.warning(
                "UIDVALIDITY changed for %s/%s (%s -> %s); resetting watermark",
                self._email_address, folder,
                folder_state.get("uidvalidity"), uidvalidity,
            )
            state[folder] = {
                "uidvalidity": uidvalidity,
                "last_delivered_uid": uidnext - 1,
            }
            self._watermark.save(state)
            return []

        # Normal path
        last = int(folder_state["last_delivered_uid"])
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder, readonly=True)
            new_uids = imap.search([b"UID", f"{last+1}:*".encode()])
        # IMAP semantics: when range start > UIDNEXT the server returns
        # the highest existing UID. Filter that out.
        new_uids = [u for u in new_uids if int(u) > last]
        if not new_uids:
            return []

        envelopes = self.fetch_headers_by_uids(folder, [str(u) for u in new_uids])
        if envelopes:
            new_high = max(int(e["uid"]) for e in envelopes)
            state[folder] = {
                "uidvalidity": uidvalidity,
                "last_delivered_uid": new_high,
            }
            self._watermark.save(state)
        return envelopes

    def _bootstrap_deliver_unseen(self, folder: str) -> list[dict]:
        """Bootstrap path: deliver currently-UNSEEN messages once."""
        with self._lock:
            imap = self._ensure_connected()
            imap.select_folder(folder, readonly=True)
            uids = imap.search(b"UNSEEN")
        if not uids:
            return []
        return self.fetch_headers_by_uids(folder, [str(u) for u in uids])
```

- [ ] **Step 2: Run reconcile tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v -k "reconcile"
```

Expected: 4 passed.

- [ ] **Step 3: Run the full account test file**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v
```

Expected: all passed.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/account.py tests/test_addon_imap_account.py
git commit -m "feat(imap): UIDNEXT-watermark reconcile with bootstrap and UIDVALIDITY reset"
```

---

## Task 15: Listener loop — failing tests

**Files:**
- Test: `tests/test_addon_imap_account.py` (append)

> The listener loop runs in a background thread. We test it by:
> 1. Building a synthetic-IDLE script (a list of `idle_check` return values),
> 2. Running the loop on the *current* thread with a tight stop-event,
> 3. Asserting on calls to `idle_done`/`reconcile`/`noop`/reconnect.

- [ ] **Step 1: Append listener tests**

```python
def test_listener_idle_exists_triggers_reconcile(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    """One IDLE slice returns EXISTS → loop calls idle_done, reconcile, idle again."""
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 1, b"UIDNEXT": 100}
    instance.search.side_effect = [
        [],            # bootstrap UNSEEN search → empty
        [99],          # second search: UID range query for new mail (filtered)
    ]
    # idle_check returns: first call → EXISTS; second call → empty (slice expired)
    instance.idle_check.side_effect = [
        [(99, b"EXISTS")],
        [],
    ]
    instance.fetch.return_value = {
        99: {b"FLAGS": (), b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]":
             b"From: a@b.com\r\nSubject: hi\r\n"},
    }

    received: list[dict] = []
    stop = threading.Event()

    def on_message(headers: list[dict]) -> None:
        received.extend(headers)
        stop.set()  # exit loop after first delivery

    account._stop_event = stop
    account._run_listener_loop(folder="INBOX", on_message=on_message,
                               max_iterations=3)

    instance.idle.assert_called()
    instance.idle_done.assert_called()
    assert any(e["uid"] == "99" for e in received)


def test_listener_silent_slice_runs_noop_keepalive(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 1, b"UIDNEXT": 100}
    instance.search.return_value = []
    # idle_check returns nothing → slice expired silently
    instance.idle_check.side_effect = [[], []]

    stop = threading.Event()

    def on_message(headers: list[dict]) -> None:
        pass

    account._stop_event = stop
    # Stop after a tight loop
    threading.Timer(0.05, stop.set).start()
    account._run_listener_loop(folder="INBOX", on_message=on_message,
                               max_iterations=3)

    instance.noop.assert_called()


def test_listener_reconnects_on_idle_check_error(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 1, b"UIDNEXT": 100}
    instance.search.return_value = []
    # First idle_check raises, second returns empty
    instance.idle_check.side_effect = [
        socket.error("connection reset"),
        [],
    ]

    stop = threading.Event()

    def on_message(headers: list[dict]) -> None:
        pass

    threading.Timer(0.1, stop.set).start()
    account._stop_event = stop
    account._run_listener_loop(folder="INBOX", on_message=on_message,
                               max_iterations=3, backoff_override=0.0)

    # connect was called more than once (initial + reconnect)
    assert mock_imapclient_class.call_count >= 2


def test_listener_stop_event_exits_cleanly(
    mock_imapclient_class, account: IMAPAccount,
) -> None:
    instance = mock_imapclient_class.return_value
    instance.capabilities.return_value = (b"IDLE",)
    instance.list_folders.return_value = []
    instance.folder_status.return_value = {b"UIDVALIDITY": 1, b"UIDNEXT": 100}
    instance.search.return_value = []
    instance.idle_check.return_value = []

    stop = threading.Event()
    stop.set()  # already set — loop should not enter even one full slice
    account._stop_event = stop

    account._run_listener_loop(folder="INBOX", on_message=lambda h: None,
                               max_iterations=3)

    # idle_check should NOT have been called when stop was set before entry
    instance.idle_check.assert_not_called()
```

You'll need `import threading` and `import socket` at the top of the test file. Add them to the existing imports if not present.

- [ ] **Step 2: Run, expect failures**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v -k "listener"
```

Expected: failures (`'IMAPAccount' object has no attribute '_run_listener_loop'`).

---

## Task 16: Listener loop — implementation

**Files:**
- Modify: `src/lingtai/addons/imap/account.py`

- [ ] **Step 1: Replace the listener placeholder section**

Replace the `# -- Listener (added in subsequent tasks) --` block at the bottom of `IMAPAccount` with:

```python
    # -- Listener loop ------------------------------------------------------

    _IDLE_SLICE_SEC = 540          # 9 min slice
    _IDLE_CYCLE_SEC = 29 * 60      # 29 min hard cap

    def start_listening(self, on_message: Callable[[list[dict]], None]) -> None:
        if self._bg_thread is not None:
            return
        self._stop_event = threading.Event()
        self._bg_thread = threading.Thread(
            target=self._run_listener_loop,
            args=("INBOX", on_message),
            daemon=True,
        )
        self._bg_thread.start()

    def _run_listener_loop(
        self,
        folder: str,
        on_message: Callable[[list[dict]], None],
        *,
        max_iterations: int | None = None,
        backoff_override: float | None = None,
    ) -> None:
        """The listener body. Connect, IDLE in 9-min slices for up to 29 min,
        reconcile on EXISTS/RECENT, NOOP on silent slice, reconnect on error.

        ``max_iterations`` and ``backoff_override`` exist for testability —
        production calls leave them None.
        """
        assert self._stop_event is not None
        iterations = 0
        while not self._stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                return
            iterations += 1
            try:
                self._connect_listener(folder)
                self._backoff_index = 0
                # Catch up on anything that arrived while we were down
                envelopes = self.reconcile(folder)
                if envelopes:
                    on_message(envelopes)
                if self._stop_event.is_set():
                    return
                self._idle_session(folder, on_message)
            except (socket.error, OSError, IMAPClientError) as e:
                logger.warning(
                    "listener error on %s: %s", self._email_address, e,
                )
                self._disconnect_listener()
                delay = (
                    backoff_override
                    if backoff_override is not None
                    else self._backoff_steps[
                        min(self._backoff_index, len(self._backoff_steps) - 1)
                    ]
                )
                self._backoff_index += 1
                if self._stop_event.wait(delay):
                    return
        self._disconnect_listener()

    def _connect_listener(self, folder: str) -> None:
        """Open the dedicated listener IMAPClient and select INBOX."""
        if self._listen_imap is not None:
            try:
                self._listen_imap.logout()
            except Exception:
                pass
        client = IMAPClient(self._imap_host, port=self._imap_port, ssl=True)
        client.login(self._email_address, self._email_password)
        client.select_folder(folder)
        self._listen_imap = client

    def _disconnect_listener(self) -> None:
        if self._listen_imap is not None:
            try:
                self._listen_imap.logout()
            except Exception:
                pass
            self._listen_imap = None
        self._listen_in_idle = False

    def _idle_session(
        self,
        folder: str,
        on_message: Callable[[list[dict]], None],
    ) -> None:
        """One full IDLE session: re-issue every 29 min, slice every 9 min,
        NOOP probe on silent slices."""
        assert self._listen_imap is not None
        assert self._stop_event is not None
        imap = self._listen_imap
        cycle_deadline = time.monotonic() + self._IDLE_CYCLE_SEC
        imap.idle()
        self._listen_in_idle = True
        try:
            while time.monotonic() < cycle_deadline:
                if self._stop_event.is_set():
                    return
                responses = imap.idle_check(timeout=self._IDLE_SLICE_SEC)
                interesting = [
                    r for r in responses
                    if isinstance(r, tuple) and len(r) >= 2
                    and r[1] in (b"EXISTS", b"RECENT")
                ]
                if interesting:
                    imap.idle_done()
                    self._listen_in_idle = False
                    envelopes = self.reconcile(folder)
                    if envelopes:
                        on_message(envelopes)
                    if self._stop_event.is_set():
                        return
                    imap.idle()
                    self._listen_in_idle = True
                elif not responses:
                    # Silent slice — probe socket
                    imap.idle_done()
                    self._listen_in_idle = False
                    imap.noop()
                    if self._stop_event.is_set():
                        return
                    imap.idle()
                    self._listen_in_idle = True
                # else: keep-alive or unrelated event, stay in IDLE
        finally:
            try:
                if self._listen_in_idle:
                    imap.idle_done()
            except Exception:
                pass
            self._listen_in_idle = False
```

- [ ] **Step 2: Run listener tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v -k "listener"
```

Expected: 4 passed.

- [ ] **Step 3: Run the full account test file**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py -v
```

Expected: all passed.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/account.py tests/test_addon_imap_account.py
git commit -m "feat(imap): robust listener loop — 9min slices, 29min cap, NOOP keep-alive"
```

---

## Task 17: Wire migration into addon load

**Files:**
- Modify: `src/lingtai/addons/imap/__init__.py`

- [ ] **Step 1: Add migration call**

In `src/lingtai/addons/imap/__init__.py`, find the line:

```python
working_dir = Path(agent._working_dir)
bridge_dir = working_dir / "imap_bridge"
bridge_dir.mkdir(parents=True, exist_ok=True)
```

Immediately above it, add:

```python
    # One-shot legacy state cleanup (pre-rewrite _processed_uids files)
    from ._migrate import migrate_legacy_state
    state_dir = Path(agent._working_dir) / "imap"
    if state_dir.is_dir():
        migrate_legacy_state(state_dir)
```

- [ ] **Step 2: Smoke import**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "from lingtai.addons.imap import setup; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Run all imap tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py tests/test_addon_imap_watermark.py tests/test_addon_imap_migrate.py -v
```

Expected: all passed.

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/addons/imap/__init__.py
git commit -m "feat(imap): run legacy state cleanup at addon load"
```

---

## Task 18: Manager — richer `_accounts()` status — failing test

**Files:**
- Test: `tests/test_addon_imap_manager.py` (modify)

- [ ] **Step 1: Inspect existing `_accounts()` test**

```bash
cd ~/Documents/GitHub/lingtai-kernel && grep -n "_accounts\|accounts" tests/test_addon_imap_manager.py | head -20
```

- [ ] **Step 2: Add a test for the new shape**

Append to `tests/test_addon_imap_manager.py`:

```python
def test_accounts_returns_richer_status_per_account(tmp_path):
    """`accounts` action returns tool_connected, listener_connected, listening."""
    from unittest.mock import MagicMock
    from lingtai.addons.imap.manager import IMAPMailManager
    from lingtai.addons.imap.service import IMAPMailService

    mock_account = MagicMock()
    mock_account.address = "alice@example.com"
    mock_account.connected = True
    mock_account.listening = True
    # connection of listener: alive thread + alive client
    mock_account._listen_imap = MagicMock()
    mock_account._bg_thread = MagicMock()
    mock_account._bg_thread.is_alive.return_value = True

    mock_service = MagicMock(spec=IMAPMailService)
    mock_service.accounts = [mock_account]
    mock_service.default_account = mock_account
    mock_service.get_account.return_value = mock_account

    mock_agent = MagicMock()
    mock_agent._working_dir = str(tmp_path)

    mgr = IMAPMailManager(mock_agent, service=mock_service, tcp_alias="bridge")
    result = mgr.handle({"action": "accounts"})

    assert "accounts" in result
    a = result["accounts"][0]
    assert a["address"] == "alice@example.com"
    assert a["tool_connected"] is True
    assert a["listener_connected"] is True
    assert a["listening"] is True
```

- [ ] **Step 3: Run, expect failure**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_manager.py::test_accounts_returns_richer_status_per_account -v
```

Expected: failure (the existing `_accounts()` returns a different shape).

---

## Task 19: Manager — richer `_accounts()` status — implementation

**Files:**
- Modify: `src/lingtai/addons/imap/manager.py`

- [ ] **Step 1: Read the existing `_accounts()` method**

```bash
cd ~/Documents/GitHub/lingtai-kernel && grep -n -A 10 "def _accounts" src/lingtai/addons/imap/manager.py
```

- [ ] **Step 2: Replace the method**

Locate `def _accounts(self, args: dict) -> dict:` and replace its body with:

```python
    def _accounts(self, args: dict) -> dict:
        out: list[dict] = []
        for acct in self._service.accounts:
            listener_connected = (
                getattr(acct, "_bg_thread", None) is not None
                and acct._bg_thread.is_alive()
                and getattr(acct, "_listen_imap", None) is not None
            )
            out.append({
                "address": acct.address,
                "tool_connected": acct.connected,
                "listener_connected": listener_connected,
                "listening": getattr(acct, "listening", False),
            })
        return {"accounts": out}
```

- [ ] **Step 3: Run manager tests**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_manager.py -v
```

Expected: all passed (the new test passes; any preexisting `_accounts()` tests that asserted on the old shape will need to be updated — if the test runner reports failures, update those assertions to match the new shape and re-run).

- [ ] **Step 4: Smoke import**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "from lingtai.addons.imap.manager import IMAPMailManager; print('OK')"
```

Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/lingtai/addons/imap/manager.py tests/test_addon_imap_manager.py
git commit -m "feat(imap): _accounts() returns tool/listener/listening status"
```

---

## Task 20: Update the addon manual

**Files:**
- Modify: `src/lingtai/addons/imap/manual/SKILL.md`

- [ ] **Step 1: Remove the "known display bug" caveat**

In `src/lingtai/addons/imap/manual/SKILL.md`, find this line:

```markdown
- **Status caveat:** after refresh, `imap(action="accounts")` may show `connected: false` even when IMAP is working. This is a known display bug. Always verify with `imap(action="check")` — if it returns emails, the connection is working regardless of what `connected` says.
```

Replace it with:

```markdown
- **Status:** `imap(action="accounts")` returns three flags per account: `tool_connected` (the on-demand connection used by tool calls), `listener_connected` (the background listener's connection), and `listening` (whether the listener is currently in IDLE waiting for new mail). All three should be `true` for a healthy account.
```

- [ ] **Step 2: Smoke check the SKILL.md still parses**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "
import re
text = open('src/lingtai/addons/imap/manual/SKILL.md').read()
fm = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
assert fm, 'frontmatter missing'
print('frontmatter OK')
"
```

Expected: prints `frontmatter OK`.

- [ ] **Step 3: Commit**

```bash
git add src/lingtai/addons/imap/manual/SKILL.md
git commit -m "docs(imap): drop display-bug caveat, document new status fields"
```

---

## Task 21: End-to-end live smoke test

**Files:**
- Create: `tests/test_addon_imap_live.py`

> This test runs against a real Gmail account. It is skipped unless `IMAP_LIVE_TEST=1` is set, so CI never hits it. Before running locally you must set:
> ```
> IMAP_LIVE_TEST=1
> IMAP_LIVE_EMAIL=<a real test gmail>
> IMAP_LIVE_PASSWORD=<16-char app password>
> ```

- [ ] **Step 1: Create the live smoke test**

```python
"""End-to-end live smoke test for the IMAP addon.

Runs only when IMAP_LIVE_TEST=1. Requires a real Gmail (or compatible)
test account with IMAP enabled and an app password.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

LIVE = os.getenv("IMAP_LIVE_TEST") == "1"
EMAIL = os.getenv("IMAP_LIVE_EMAIL", "")
PASSWORD = os.getenv("IMAP_LIVE_PASSWORD", "")

pytestmark = pytest.mark.skipif(
    not (LIVE and EMAIL and PASSWORD),
    reason="set IMAP_LIVE_TEST=1, IMAP_LIVE_EMAIL, IMAP_LIVE_PASSWORD",
)


def test_connect_and_check_inbox(tmp_path: Path) -> None:
    from lingtai.addons.imap.account import IMAPAccount

    acct = IMAPAccount(
        email_address=EMAIL,
        email_password=PASSWORD,
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
        working_dir=tmp_path,
    )
    acct.connect()
    assert acct.connected
    envelopes = acct.fetch_envelopes("INBOX", n=3)
    print(f"{len(envelopes)} envelopes fetched")
    acct.disconnect()


def test_reconcile_round_trip(tmp_path: Path) -> None:
    """Reconcile twice — second call should find zero new (no test mail sent)."""
    from lingtai.addons.imap.account import IMAPAccount

    acct = IMAPAccount(
        email_address=EMAIL,
        email_password=PASSWORD,
        working_dir=tmp_path,
    )
    acct.connect()
    first = acct.reconcile("INBOX")
    print(f"bootstrap delivered {len(first)} envelopes")
    second = acct.reconcile("INBOX")
    assert second == [], "second reconcile should be empty (no new mail expected)"
    acct.disconnect()
```

- [ ] **Step 2: Verify it's skipped by default**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_live.py -v
```

Expected: 2 skipped.

- [ ] **Step 3: Commit**

```bash
git add tests/test_addon_imap_live.py
git commit -m "test(imap): gated live smoke test for connect + reconcile"
```

---

## Task 22: Final verification — full suite + manual smoke

**Files:** none

- [ ] **Step 1: Run the entire affected test suite**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest tests/test_addon_imap_account.py tests/test_addon_imap_watermark.py tests/test_addon_imap_migrate.py tests/test_addon_imap_manager.py tests/test_addon_imap_service.py -v
```

Expected: all passed.

- [ ] **Step 2: Run the full kernel test suite**

```bash
cd ~/Documents/GitHub/lingtai-kernel && pytest -q
```

Expected: same pass rate as before this branch (any unrelated failures predate this change).

- [ ] **Step 3: Smoke import the addon as it would load in production**

```bash
cd ~/Documents/GitHub/lingtai-kernel && python -c "
from lingtai.addons.imap import setup
from lingtai.addons.imap.account import IMAPAccount
from lingtai.addons.imap._watermark import WatermarkStore
from lingtai.addons.imap._migrate import migrate_legacy_state
print('all imports OK')
"
```

Expected: `all imports OK`.

- [ ] **Step 4: (Optional but recommended) live test**

```bash
cd ~/Documents/GitHub/lingtai-kernel && IMAP_LIVE_TEST=1 IMAP_LIVE_EMAIL='your-test@gmail.com' IMAP_LIVE_PASSWORD='xxxx xxxx xxxx xxxx' pytest tests/test_addon_imap_live.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Final commit (if any cleanup is needed)**

```bash
git status   # should be clean
```

If clean, done. If anything is uncommitted, commit it as `chore(imap): post-rewrite cleanup`.
