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
        _sp = self._state_path()
        self._watermark = WatermarkStore(_sp) if _sp else None

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
            self._tool_imap = None
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
        if self._tool_imap is not None:
            return
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
            data = imap.fetch(
                recent,
                ["FLAGS", "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]"],
            )
        return [self._envelope_from_fetch(uid, info, folder)
                for uid, info in sorted(data.items())]

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
        flags = [
            f.decode("ascii", errors="replace") if isinstance(f, bytes)
            else str(f)
            for f in flags_raw
        ]
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
        flags = [
            f.decode("ascii", errors="replace") if isinstance(f, bytes)
            else str(f)
            for f in flags_raw
        ]
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
                try:
                    d = datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    logger.warning(
                        "imap search: invalid date %r for since:, skipping",
                        val,
                    )
                    continue
                criteria += [b"SINCE", d.strftime("%d-%b-%Y").encode()]
            elif key == "before" and val:
                try:
                    d = datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    logger.warning(
                        "imap search: invalid date %r for before:, skipping",
                        val,
                    )
                    continue
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
        try:
            flag_bytes = [f.encode("ascii") for f in flags]
        except UnicodeEncodeError:
            logger.warning(
                "imap store_flags: non-ASCII flag in %r, refusing", flags,
            )
            return False
        if action not in ("+FLAGS", "-FLAGS", "FLAGS"):
            logger.warning(
                "imap store_flags: unknown action %r, refusing", action,
            )
            return False
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
                    if self._has_uidplus:
                        imap.uid_expunge([int(uid)])
                    else:
                        # No UIDPLUS — bare EXPUNGE removes ALL \Deleted msgs
                        # in this folder. Acceptable risk only because the
                        # server lacks both MOVE and UIDPLUS, which is rare.
                        logger.warning(
                            "imap: %s lacks MOVE+UIDPLUS; EXPUNGE may "
                            "affect other \\Deleted messages",
                            self._email_address,
                        )
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
                if self._has_uidplus:
                    imap.uid_expunge([int(uid)])
                else:
                    logger.warning(
                        "imap: %s lacks UIDPLUS; EXPUNGE may affect "
                        "other \\Deleted messages",
                        self._email_address,
                    )
                    imap.expunge()
                return True
            except IMAPClientError:
                return False

    # -- Listener (added in subsequent tasks) ------------------------------

    def start_listening(self, on_message: Callable[[list[dict]], None]) -> None:
        raise NotImplementedError("filled in Task 11")

    def stop_listening(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._bg_thread is not None:
            self._bg_thread.join(timeout=15.0)
            self._bg_thread = None
