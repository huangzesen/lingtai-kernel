"""Email intrinsic — filesystem-based mailbox with search, contacts, schedules.

Replaces the older mail intrinsic (which only had send/check/read/search/delete
on inbox). Email adds: cc/bcc, reply/reply_all, archive folder, contacts book,
recurring schedules, attachments, identity injection in summaries.

Storage layout:
    working_dir/mailbox/inbox/{uuid}/message.json     — received
    working_dir/mailbox/sent/{uuid}/message.json      — sent
    working_dir/mailbox/archive/{uuid}/message.json   — archived from inbox
    working_dir/mailbox/read.json                     — read tracking
    working_dir/mailbox/contacts.json                 — contact book
    working_dir/mailbox/schedules/{id}/schedule.json  — recurring sends

Internal:
    boot(agent) — instantiates EmailManager on agent._email_manager and starts
        the scheduler thread. Called from base_agent during agent construction.
    handle(agent, args) — module-level dispatcher; delegates to the manager.
    _new_mailbox_id, mode_field — re-exported for cross-module use.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ..i18n import t
from ..message import _make_message, MSG_REQUEST
from ..time_veil import scrub_time_fields
from ..token_counter import count_tokens

if TYPE_CHECKING:
    from ..base_agent import BaseAgent


# ---------------------------------------------------------------------------
# Mailbox primitives — moved here from the former mail intrinsic. Kept as
# module-level functions so other code can still import them by name.
# ---------------------------------------------------------------------------

def _new_mailbox_id() -> str:
    """Build a sortable, human-scannable mailbox id.

    Format: ``<YYYYMMDDTHHMMSS>-<4 hex>`` — 20 chars total.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:4]}"


def mode_field(lang: str = "en") -> dict:
    """Schema field for the address-mode parameter."""
    return {
        "type": "string",
        "enum": ["peer", "abs"],
        "description": t(lang, "email.mode"),
    }


def _mailbox_dir(agent) -> Path:
    return agent._working_dir / "mailbox"


def _inbox_dir(agent) -> Path:
    return _mailbox_dir(agent) / "inbox"


def _outbox_dir(agent) -> Path:
    return _mailbox_dir(agent) / "outbox"


def _sent_dir(agent) -> Path:
    return _mailbox_dir(agent) / "sent"


def _load_message(agent, msg_id: str) -> dict | None:
    """Load a single inbox message by ID, or None if not found."""
    msg_file = _inbox_dir(agent) / msg_id / "message.json"
    if not msg_file.is_file():
        return None
    try:
        return json.loads(msg_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _list_inbox(agent) -> list[dict]:
    """List all inbox messages, sorted newest first (by received_at)."""
    inbox = _inbox_dir(agent)
    if not inbox.is_dir():
        return []
    messages = []
    for msg_dir in inbox.iterdir():
        if not msg_dir.is_dir():
            continue
        msg_file = msg_dir / "message.json"
        if not msg_file.is_file():
            continue
        try:
            msg = json.loads(msg_file.read_text())
            messages.append(msg)
        except (json.JSONDecodeError, OSError):
            continue
    messages.sort(key=lambda m: m.get("received_at", ""), reverse=True)
    return messages


def _read_ids_path(agent) -> Path:
    return _mailbox_dir(agent) / "read.json"


def _read_ids(agent) -> set[str]:
    """Load set of read message IDs from read.json."""
    path = _read_ids_path(agent)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def _save_read_ids(agent, ids: set[str]) -> None:
    """Atomically write read IDs to read.json."""
    path = _read_ids_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    os.replace(str(tmp), str(path))


def _mark_read(agent, msg_id: str) -> None:
    """Mark a message as read."""
    ids = _read_ids(agent)
    ids.add(msg_id)
    _save_read_ids(agent, ids)


def _summary_to_list(raw) -> list[str]:
    """Best-effort coercion of to/cc for display."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw if isinstance(x, str)]


def _message_summary(msg: dict, read_ids: set[str], truncate: int = 500) -> dict:
    """Build a summary dict for check output."""
    msg_id = msg.get("_mailbox_id", "")
    body = msg.get("message", "")
    if truncate > 0 and len(body) > truncate:
        preview = body[:truncate] + f"... ({len(body) - truncate} more chars)"
    else:
        preview = body
    identity = msg.get("identity")
    sender = msg.get("from", "")
    if identity and identity.get("agent_name"):
        sender = f"{identity['agent_name']} ({sender})"
    return {
        "id": msg_id,
        "from": sender,
        "to": _summary_to_list(msg.get("to")),
        "subject": msg.get("subject", ""),
        "preview": preview,
        "time": msg.get("received_at", ""),
        "unread": msg_id not in read_ids,
    }


def _is_self_send(agent, address: str) -> bool:
    """Check if the address matches this agent."""
    if address == agent._working_dir.name:
        return True
    if address == str(agent._working_dir):
        return True
    if agent._mail_service is not None and agent._mail_service.address:
        if address == agent._mail_service.address:
            return True
    return False


def _persist_to_inbox(agent, payload: dict) -> str:
    """Persist a message directly to mailbox/inbox/{uuid}/message.json."""
    msg_id = _new_mailbox_id()
    msg_dir = _inbox_dir(agent) / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["_mailbox_id"] = msg_id
    payload["received_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (msg_dir / "message.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    return msg_id


def _persist_to_outbox(agent, payload: dict, deliver_at: datetime) -> str:
    """Write a message to outbox/{uuid}/message.json."""
    msg_id = _new_mailbox_id()
    msg_dir = _outbox_dir(agent) / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload.pop("_mode", None)
    payload.pop("_dispatch_to", None)
    payload["_mailbox_id"] = msg_id
    payload["deliver_at"] = deliver_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    (msg_dir / "message.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    return msg_id


def _move_to_sent(agent, msg_id: str, sent_at: str, status: str) -> None:
    """Move outbox/{uuid}/ → sent/{uuid}/, enriching with sent_at and status."""
    src = _outbox_dir(agent) / msg_id
    dst = _sent_dir(agent) / msg_id
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        return
    msg_file = src / "message.json"
    if msg_file.is_file():
        try:
            data = json.loads(msg_file.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        data["sent_at"] = sent_at
        data["status"] = status
        msg_file.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    shutil.move(str(src), str(dst))


def _mailman(agent, msg_id: str, payload: dict, deliver_at: datetime,
             *, skip_sent: bool = False) -> None:
    """Daemon thread — one per message. Waits, dispatches, archives to sent."""
    import time as _time

    wait = (deliver_at - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        _time.sleep(wait)

    address = payload.get("_dispatch_to") or payload.get("to", "")
    if isinstance(address, list):
        address = address[0] if address else ""

    mode = payload.pop("_mode", "peer")

    err = None
    try:
        if _is_self_send(agent, address):
            _persist_to_inbox(agent, payload)
            agent._wake_nap("mail_arrived")
            status = "delivered"
        elif agent._mail_service is not None:
            err = agent._mail_service.send(address, payload, mode=mode)
            status = "delivered" if err is None else "refused"
        else:
            err = "No mail service configured"
            status = "refused"
    except Exception as exc:
        err = str(exc)
        status = "refused"

    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not skip_sent:
        _move_to_sent(agent, msg_id, sent_at, status)
    else:
        outbox_entry = _outbox_dir(agent) / msg_id
        if outbox_entry.is_dir():
            shutil.rmtree(outbox_entry)

    agent._log("mail_sent", address=address, subject=payload.get("subject", ""),
               status=status, message=payload.get("message", ""))

    # Bounce notification — synthesized as a system(action="notification") pair
    # via tc_inbox. Source "email.bounce" has no auto-dismiss hook (bounces are
    # not directly actionable like reads); the agent can voluntarily dismiss
    # via system(action="dismiss", ids=[...]) or wait for molt to clear them.
    if status == "refused" and err:
        notification = t(
            agent._config.language, "system.mail_bounce",
            error=err, address=address,
            subject=payload.get("subject", "(no subject)"),
        )
        agent._enqueue_system_notification(
            source="email.bounce",
            ref_id=msg_id,
            body=notification,
        )


# ---------------------------------------------------------------------------
# Email-specific helpers
# ---------------------------------------------------------------------------

def _coerce_address_list(raw) -> list[str]:
    """Normalize an address arg into a clean list[str]."""
    if raw is None:
        return []
    if isinstance(raw, str):
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if x]
            except (json.JSONDecodeError, ValueError):
                pass
        return [raw] if raw else []
    return [str(x) for x in raw if x]


def _preview(body: str, limit: int = 500) -> str:
    if limit <= 0:
        return body
    if len(body) > limit:
        return body[:limit] + f"... ({len(body) - limit} more chars)"
    return body


def _email_time(e: dict) -> str:
    """Extract the best timestamp from an email dict for filtering."""
    return e.get("received_at") or e.get("sent_at") or e.get("time") or ""


# ---------------------------------------------------------------------------
# Schema / description
# ---------------------------------------------------------------------------

def get_description(lang: str = "en") -> str:
    return t(lang, "email.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "send", "check", "read", "reply", "reply_all", "search",
                    "archive", "delete",
                    "contacts", "add_contact", "remove_contact", "edit_contact",
                ],
                "description": t(lang, "email.action"),
            },
            "address": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": t(lang, "email.address"),
            },
            "cc": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.cc"),
            },
            "bcc": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.bcc"),
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.attachments"),
            },
            "subject": {"type": "string", "description": t(lang, "email.subject")},
            "message": {"type": "string", "description": t(lang, "email.message")},
            "email_id": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "email.email_id"),
            },
            "n": {
                "type": "integer",
                "description": t(lang, "email.n"),
                "default": 10,
            },
            "query": {
                "type": "string",
                "description": t(lang, "email.query"),
            },
            "folder": {
                "type": "string",
                "enum": ["inbox", "sent", "archive"],
                "description": t(lang, "email.folder"),
            },
            "delay": {
                "type": "integer",
                "description": t(lang, "email.delay"),
            },
            "mode": mode_field(lang),
            "type": {
                "type": "string",
                "enum": ["normal"],
                "description": t(lang, "email.type"),
            },
            "name": {
                "type": "string",
                "description": t(lang, "email.name"),
            },
            "note": {
                "type": "string",
                "description": t(lang, "email.note"),
            },
            "filter": {
                "type": "object",
                "description": t(lang, "email.filter"),
                "properties": {
                    "sort": {
                        "type": "string",
                        "enum": ["newest", "oldest"],
                        "description": t(lang, "email.filter_sort"),
                    },
                    "from": {
                        "type": "string",
                        "description": t(lang, "email.filter_from"),
                    },
                    "subject": {
                        "type": "string",
                        "description": t(lang, "email.filter_subject"),
                    },
                    "contains": {
                        "type": "string",
                        "description": t(lang, "email.filter_contains"),
                    },
                    "after": {
                        "type": "string",
                        "description": t(lang, "email.filter_after"),
                    },
                    "before": {
                        "type": "string",
                        "description": t(lang, "email.filter_before"),
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": t(lang, "email.filter_unread_only"),
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": t(lang, "email.filter_has_attachments"),
                    },
                    "truncate": {
                        "type": "integer",
                        "description": t(lang, "email.filter_truncate"),
                        "default": 500,
                    },
                },
            },
            "schedule": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "cancel", "list", "reactivate"],
                        "description": t(lang, "email.schedule_action"),
                    },
                    "interval": {
                        "type": "integer",
                        "description": t(lang, "email.schedule_interval"),
                    },
                    "count": {
                        "type": "integer",
                        "description": t(lang, "email.schedule_count"),
                    },
                    "schedule_id": {
                        "type": "string",
                        "description": t(lang, "email.schedule_id"),
                    },
                },
            },
        },
        "required": [],
    }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class EmailManager:
    """Filesystem-based email manager — reads/writes mailbox/ directory."""

    def __init__(self, agent: "BaseAgent"):
        self._agent = agent
        # Track consecutive identical sends per recipient to block loops.
        self._last_sent: dict[str, tuple[str, int]] = {}
        self._dup_free_passes = 2  # allow this many identical sends
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    def start_scheduler(self) -> None:
        """Start the background scheduler thread."""
        if self._scheduler_thread is not None:
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name=f"scheduler-{self._agent._working_dir.name}",
            daemon=True,
        )
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        """Stop the scheduler thread cleanly."""
        self._stop_event.set()
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=5.0)
            self._scheduler_thread = None

    @property
    def _mailbox_path(self) -> Path:
        return _mailbox_dir(self._agent)

    @property
    def _schedules_dir(self) -> Path:
        return self._mailbox_path / "schedules"

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _load_email(self, email_id: str) -> dict | None:
        """Load a single email by ID. Checks inbox, then sent/, then archive/."""
        msg = _load_message(self._agent, email_id)
        if msg is not None:
            msg["_folder"] = "inbox"
            msg.setdefault("_mailbox_id", email_id)
            return msg
        path = self._mailbox_path / "sent" / email_id / "message.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return None
            data["_folder"] = "sent"
            data.setdefault("_mailbox_id", email_id)
            return data
        path = self._mailbox_path / "archive" / email_id / "message.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                return None
            data["_folder"] = "archive"
            data.setdefault("_mailbox_id", email_id)
            return data
        return None

    def _list_emails(self, folder: str) -> list[dict]:
        """Load all emails from a folder, sorted by time (newest first)."""
        if folder == "inbox":
            messages = _list_inbox(self._agent)
            for m in messages:
                m["_folder"] = "inbox"
                m.setdefault("_mailbox_id", m.get("_mailbox_id", ""))
            return messages
        folder_dir = self._mailbox_path / folder
        if not folder_dir.is_dir():
            return []
        emails = []
        for msg_dir in folder_dir.iterdir():
            msg_file = msg_dir / "message.json"
            if msg_dir.is_dir() and msg_file.is_file():
                try:
                    data = json.loads(msg_file.read_text())
                    data["_folder"] = folder
                    data.setdefault("_mailbox_id", msg_dir.name)
                    emails.append(data)
                except (json.JSONDecodeError, OSError):
                    continue
        emails.sort(key=_email_time, reverse=True)
        return emails

    def _email_summary(self, e: dict, read_set: set[str] | None = None, truncate: int = 500) -> dict:
        """Build a summary dict from a raw email dict."""
        if read_set is None:
            read_set = _read_ids(self._agent)
        if e.get("_folder") == "inbox":
            summary = _message_summary(e, read_set, truncate=truncate)
            summary["folder"] = "inbox"
            if e.get("cc"):
                summary["cc"] = e["cc"]
            self._inject_identity(summary, e)
            return summary
        if e.get("_folder") == "archive":
            summary = _message_summary(e, read_set, truncate=truncate)
            summary["folder"] = "archive"
            if e.get("cc"):
                summary["cc"] = e["cc"]
            self._inject_identity(summary, e)
            return summary
        eid = e.get("_mailbox_id", "")
        entry = {
            "id": eid,
            "from": e.get("from", ""),
            "to": e.get("to", []),
            "subject": e.get("subject", "(no subject)"),
            "preview": _preview(e.get("message", ""), limit=truncate),
            "time": e.get("received_at") or e.get("sent_at") or e.get("time") or "",
            "folder": e.get("_folder", ""),
        }
        if e.get("cc"):
            entry["cc"] = e["cc"]
        return entry

    @staticmethod
    def _inject_identity(summary: dict, raw: dict) -> None:
        """Surface identity card fields in check/read results."""
        identity = raw.get("identity")
        if not identity or not isinstance(identity, dict):
            return
        summary["is_human"] = identity.get("admin") is None
        summary["sender_name"] = identity.get("agent_name", "")
        summary["sender_nickname"] = identity.get("nickname", "")
        summary["sender_agent_id"] = identity.get("agent_id", "")
        summary["sender_language"] = identity.get("language", "")
        loc = identity.get("location")
        if isinstance(loc, dict) and loc.get("timezone"):
            summary["sender_location"] = {
                "city": loc.get("city", ""),
                "region": loc.get("region", ""),
                "timezone": loc.get("timezone", ""),
            }

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def handle(self, args: dict) -> dict:
        schedule = args.get("schedule")
        if schedule is not None:
            return self._handle_schedule(args, schedule)
        action = args.get("action")
        if not action:
            return {"error": "action is required (or pass a schedule object)"}
        if action == "send":
            return self._send(args)
        elif action == "check":
            return self._check(args)
        elif action == "read":
            return self._read(args)
        elif action == "reply":
            return self._reply(args)
        elif action == "reply_all":
            return self._reply_all(args)
        elif action == "search":
            return self._search(args)
        elif action == "archive":
            return self._archive(args)
        elif action == "delete":
            return self._delete(args)
        elif action == "contacts":
            return self._contacts()
        elif action == "add_contact":
            return self._add_contact(args)
        elif action == "remove_contact":
            return self._remove_contact(args)
        elif action == "edit_contact":
            return self._edit_contact(args)
        else:
            return {"error": f"Unknown email action: {action}"}

    # ------------------------------------------------------------------
    # Schedule dispatch
    # ------------------------------------------------------------------

    def _handle_schedule(self, args: dict, schedule: dict) -> dict:
        action = schedule.get("action")
        if action == "create":
            return self._schedule_create(args, schedule)
        elif action == "cancel":
            return self._schedule_cancel(schedule)
        elif action == "list":
            return self._schedule_list()
        elif action == "reactivate":
            return self._schedule_reactivate(schedule)
        else:
            return {"error": f"Unknown schedule action: {action}"}

    def _schedule_create(self, args: dict, schedule: dict) -> dict:
        interval = schedule.get("interval")
        count = schedule.get("count")
        if interval is None or count is None:
            return {"error": "schedule.interval and schedule.count are required"}
        if interval <= 0 or count <= 0:
            return {"error": "schedule.interval and schedule.count must be positive"}

        raw_address = args.get("address", "")
        to_list = _coerce_address_list(raw_address)
        if not to_list:
            return {"error": "address is required"}

        send_payload = {
            "address": args.get("address"),
            "subject": args.get("subject", ""),
            "message": args.get("message", ""),
            "cc": args.get("cc") or [],
            "bcc": args.get("bcc") or [],
            "type": args.get("type", "normal"),
        }
        if args.get("attachments"):
            send_payload["attachments"] = args["attachments"]

        schedule_id = uuid4().hex[:12]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "schedule_id": schedule_id,
            "send_payload": send_payload,
            "interval": interval,
            "count": count,
            "sent": 0,
            "created_at": now,
            "last_sent_at": None,
            "status": "active",
        }

        sched_dir = self._schedules_dir / schedule_id
        sched_dir.mkdir(parents=True, exist_ok=True)
        self._write_schedule(sched_dir / "schedule.json", record)

        return {"status": "scheduled", "schedule_id": schedule_id, "interval": interval, "count": count}

    def _schedule_cancel(self, schedule: dict) -> dict:
        schedule_id = schedule.get("schedule_id")

        if not schedule_id:
            schedules_dir = self._schedules_dir
            if not schedules_dir.is_dir():
                return {"status": "paused", "message": "No schedules to cancel"}
            for sched_dir in schedules_dir.iterdir():
                if not sched_dir.is_dir():
                    continue
                sched_file = sched_dir / "schedule.json"
                if not sched_file.is_file():
                    continue
                try:
                    record = json.loads(sched_file.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                status = record.get("status", "active")
                if status in ("inactive", "completed"):
                    continue
                record["status"] = "inactive"
                try:
                    self._write_schedule(sched_file, record)
                except OSError:
                    continue
            return {"status": "paused", "message": "All active schedules paused"}

        record = self._read_schedule(schedule_id)
        if record is None:
            return {"error": f"Schedule not found: {schedule_id}"}
        status = record.get("status", "active")
        if status == "inactive":
            return {"status": "already_inactive", "schedule_id": schedule_id}
        if status == "completed":
            return {"status": "already_completed", "schedule_id": schedule_id}
        self._set_schedule_status(schedule_id, "inactive")
        return {"status": "paused", "schedule_id": schedule_id}

    def _schedule_reactivate(self, schedule: dict) -> dict:
        schedule_id = schedule.get("schedule_id")
        if not schedule_id:
            return {"error": "schedule_id is required for reactivate"}
        record = self._read_schedule(schedule_id)
        if record is None:
            return {"error": f"Schedule not found: {schedule_id}"}
        status = record.get("status", "active")
        if status == "completed":
            return {"error": "Cannot reactivate a completed schedule"}
        if status == "active":
            return {"status": "already_active", "schedule_id": schedule_id}
        sent = record.get("sent", 0)
        count = record.get("count", 0)
        if sent >= count:
            record["status"] = "completed"
            sched_file = self._schedules_dir / schedule_id / "schedule.json"
            self._write_schedule(sched_file, record)
            return {"error": "Cannot reactivate a completed schedule"}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record["status"] = "active"
        record["last_sent_at"] = now
        sched_file = self._schedules_dir / schedule_id / "schedule.json"
        self._write_schedule(sched_file, record)
        return {"status": "reactivated", "schedule_id": schedule_id}

    def _schedule_list(self) -> dict:
        schedules_dir = self._schedules_dir
        if not schedules_dir.is_dir():
            return {"status": "ok", "schedules": []}

        entries = []
        for sched_dir in schedules_dir.iterdir():
            if not sched_dir.is_dir():
                continue
            sched_file = sched_dir / "schedule.json"
            if not sched_file.is_file():
                continue
            try:
                record = json.loads(sched_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            payload = record.get("send_payload", {})
            address = payload.get("address", "")
            if isinstance(address, list):
                address = ", ".join(address)

            sent = record.get("sent", 0)
            count = record.get("count", 0)

            entries.append(scrub_time_fields(self._agent, {
                "schedule_id": record.get("schedule_id", sched_dir.name),
                "to": address,
                "subject": payload.get("subject", ""),
                "interval": record.get("interval", 0),
                "count": count,
                "sent": sent,
                "status": record.get("status", "active"),
                "created_at": record.get("created_at", ""),
                "last_sent_at": record.get("last_sent_at"),
            }))

        entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        return {"status": "ok", "schedules": entries}

    # ------------------------------------------------------------------
    # Schedule helpers
    # ------------------------------------------------------------------

    def _write_schedule(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(record, indent=2, default=str).encode())
            os.close(fd)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _read_schedule(self, schedule_id: str) -> dict | None:
        path = self._schedules_dir / schedule_id / "schedule.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _set_schedule_status(self, schedule_id: str, status: str) -> bool:
        record = self._read_schedule(schedule_id)
        if record is None:
            return False
        record["status"] = status
        sched_file = self._schedules_dir / schedule_id / "schedule.json"
        self._write_schedule(sched_file, record)
        return True

    def _reconcile_schedules_on_startup(self) -> None:
        """Flip every non-completed schedule to inactive on agent startup."""
        schedules_dir = self._schedules_dir
        if not schedules_dir.is_dir():
            return
        for sched_dir in schedules_dir.iterdir():
            if not sched_dir.is_dir():
                continue
            sched_file = sched_dir / "schedule.json"
            if not sched_file.is_file():
                continue
            try:
                record = json.loads(sched_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            status = record.get("status", "active")
            if status == "completed":
                continue
            if status == "inactive":
                continue
            record["status"] = "inactive"
            try:
                self._write_schedule(sched_file, record)
            except OSError:
                continue

    def _scheduler_loop(self) -> None:
        """Single polling loop that drives all schedules from disk state."""
        while not self._stop_event.is_set():
            try:
                self._scheduler_tick()
            except Exception:
                pass
            self._stop_event.wait(timeout=1.0)

    def _scheduler_tick(self) -> None:
        """One scan of all schedule folders."""
        schedules_dir = self._schedules_dir
        if not schedules_dir.is_dir():
            return

        now = datetime.now(timezone.utc)

        for sched_dir in schedules_dir.iterdir():
            if not sched_dir.is_dir():
                continue

            sched_file = sched_dir / "schedule.json"
            if not sched_file.is_file():
                continue

            try:
                record = json.loads(sched_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            status = record.get("status", "active")
            if status != "active":
                continue

            sent = record.get("sent", 0)
            count = record.get("count", 0)
            if sent >= count:
                continue

            last_sent_at = record.get("last_sent_at")
            if last_sent_at is not None:
                try:
                    last_dt = datetime.strptime(last_sent_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                interval = record.get("interval", 0)
                due_at = last_dt + timedelta(seconds=interval)
                if now < due_at:
                    continue

            seq = sent + 1
            record["sent"] = seq
            self._write_schedule(sched_file, record)

            send_payload = record.get("send_payload", {})
            remaining = count - seq
            interval = record.get("interval", 0)
            estimated_finish = (now + timedelta(seconds=remaining * interval)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            schedule_meta = {
                "schedule_id": record.get("schedule_id", sched_dir.name),
                "seq": seq,
                "total": count,
                "interval": interval,
                "scheduled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "estimated_finish": estimated_finish,
            }
            send_args = {**send_payload, "_schedule": schedule_meta}
            result = self._send(send_args)

            if result.get("error") or result.get("status") == "blocked":
                record["last_sent_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                self._write_schedule(sched_file, record)
                continue

            to_label = send_payload.get("address", "")
            subj_label = send_payload.get("subject", "(no subject)")
            ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if seq < count:
                next_at = (now + timedelta(seconds=interval)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                note = (
                    f"[schedule {seq}/{count}] sent to {to_label} "
                    f"| subject: {subj_label} "
                    f"| sent at {ts} "
                    f"| next at {next_at} "
                    f"| ends ~{estimated_finish}"
                )
            else:
                note = (
                    f"[schedule {seq}/{count}] sent to {to_label} "
                    f"| subject: {subj_label} "
                    f"| sent at {ts} "
                    f"| schedule complete"
                )
            self._agent._log(
                "schedule_send", schedule_id=schedule_meta["schedule_id"],
                seq=seq, total=count, to=to_label, subject=subj_label,
            )
            msg = _make_message(MSG_REQUEST, "system", note)
            self._agent.inbox.put(msg)

            record["last_sent_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if seq >= count:
                record["status"] = "completed"
            self._write_schedule(sched_file, record)

    # ------------------------------------------------------------------
    # Send — deliver + save to sent/
    # ------------------------------------------------------------------

    def _send(self, args: dict) -> dict:
        raw_address = args.get("address", "")
        subject = args.get("subject", "")
        message_text = args.get("message", "")
        mail_type = args.get("type", "normal")
        cc = args.get("cc") or []
        bcc = args.get("bcc") or []
        delay = args.get("delay", 0)
        mode = args.get("mode", "peer")

        to_list = _coerce_address_list(raw_address)

        if not to_list:
            return {"error": "address is required"}
        if mode not in ("peer", "abs"):
            return {"error": f"invalid mode: {mode!r} (must be peer or abs)"}

        all_targets = to_list + cc + bcc
        if args.get("_schedule"):
            duplicates = []
        else:
            duplicates = [
                addr for addr in all_targets
                if (prev := self._last_sent.get(addr)) is not None
                and prev[0] == message_text
                and prev[1] >= self._dup_free_passes
            ]
        if duplicates:
            return {
                "status": "blocked",
                "warning": (
                    "Identical message already sent to: "
                    f"{', '.join(duplicates)}. "
                    "This looks like a repetitive loop — "
                    "think twice before sending."
                ),
            }

        sender = (self._agent._mail_service.address
                  if self._agent._mail_service is not None and self._agent._mail_service.address
                  else self._agent._working_dir.name)

        base_payload = {
            "from": sender,
            "to": to_list,
            "subject": subject,
            "message": message_text,
            "type": mail_type,
            "identity": self._agent._build_manifest(),
        }
        if cc:
            base_payload["cc"] = cc
        attachments = args.get("attachments", [])
        if attachments:
            base_payload["attachments"] = attachments

        deliver_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        all_recipients = to_list + cc + bcc

        for addr in all_recipients:
            dispatch_payload = dict(base_payload)
            dispatch_payload["_dispatch_to"] = addr
            dispatch_payload["_mode"] = mode
            msg_id = _persist_to_outbox(self._agent, dispatch_payload, deliver_at)
            tt = threading.Thread(
                target=_mailman,
                args=(self._agent, msg_id, dispatch_payload, deliver_at),
                kwargs={"skip_sent": True},
                name=f"mailman-{msg_id[:8]}",
                daemon=True,
            )
            tt.start()

        sent_id = _new_mailbox_id()
        sent_dir = self._mailbox_path / "sent" / sent_id
        sent_dir.mkdir(parents=True, exist_ok=True)
        sent_record = {
            **base_payload,
            "_mailbox_id": sent_id,
            "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delay": delay,
        }
        if bcc:
            sent_record["bcc"] = bcc
        if args.get("_schedule"):
            sent_record["_schedule"] = args["_schedule"]
        (sent_dir / "message.json").write_text(
            json.dumps(sent_record, indent=2, default=str)
        )

        for addr in all_recipients:
            prev = self._last_sent.get(addr)
            if prev is not None and prev[0] == message_text:
                self._last_sent[addr] = (message_text, prev[1] + 1)
            else:
                self._last_sent[addr] = (message_text, 1)

        self._agent._log(
            "email_sent", to=to_list, cc=cc, bcc=bcc,
            subject=subject, message=message_text, delay=delay,
        )

        return {"status": "sent", "to": to_list, "cc": cc, "bcc": bcc, "delay": delay}

    # ------------------------------------------------------------------
    # Check / Read / Reply / Search / Archive / Delete
    # ------------------------------------------------------------------

    def _check(self, args: dict) -> dict:
        folder = args.get("folder", "inbox")
        n = args.get("n", 10)
        f = args.get("filter") or {}
        sort = f.get("sort", "newest")
        truncate = f.get("truncate", 500)

        emails = self._list_emails(folder)
        read_set = _read_ids(self._agent)

        if f.get("from"):
            ff = f["from"].lower()
            emails = [e for e in emails if ff in (e.get("from") or "").lower()]
        if f.get("subject"):
            sf = f["subject"].lower()
            emails = [e for e in emails if sf in (e.get("subject") or "").lower()]
        if f.get("contains"):
            cf = f["contains"].lower()
            emails = [e for e in emails if cf in (e.get("message") or "").lower()]
        if f.get("after"):
            emails = [e for e in emails if _email_time(e) >= f["after"]]
        if f.get("before"):
            emails = [e for e in emails if _email_time(e) <= f["before"]]
        if f.get("unread_only"):
            emails = [e for e in emails if e.get("_mailbox_id", "") not in read_set]
        if f.get("has_attachments"):
            emails = [e for e in emails if e.get("attachments")]

        if sort == "oldest":
            emails = list(reversed(emails))

        total = len(emails)
        recent = emails[:n] if n > 0 else emails
        summaries = [scrub_time_fields(self._agent, self._email_summary(e, read_set, truncate=truncate)) for e in recent]

        result = {"status": "ok", "total": total, "showing": len(summaries), "emails": summaries}
        tokens = count_tokens(json.dumps(result, ensure_ascii=False))
        if tokens > 10_000:
            while summaries and count_tokens(json.dumps(result, ensure_ascii=False)) > 10_000:
                summaries.pop()
                result["emails"] = summaries
                result["showing"] = len(summaries)
            result["truncated_by_budget"] = total - len(summaries)

        return result

    def _read(self, args: dict) -> dict:
        ids = args.get("email_id", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return {"error": "email_id is required"}

        folder = args.get("folder")

        results = []
        errors = []
        for eid in ids:
            if folder:
                path = self._mailbox_path / folder / eid / "message.json"
                if path.is_file():
                    try:
                        data = json.loads(path.read_text())
                        data["_folder"] = folder
                        data.setdefault("_mailbox_id", eid)
                    except (json.JSONDecodeError, OSError):
                        errors.append(eid)
                        continue
                else:
                    errors.append(eid)
                    continue
            else:
                data = self._load_email(eid)
                if data is None:
                    errors.append(eid)
                    continue
            if data.get("_folder") == "inbox":
                _mark_read(self._agent, eid)
            entry = {
                "id": eid,
                "from": data.get("from", ""),
                "to": data.get("to", []),
                "subject": data.get("subject", "(no subject)"),
                "message": data.get("message", ""),
                "time": data.get("received_at") or data.get("sent_at") or data.get("time") or "",
                "folder": data.get("_folder", ""),
            }
            if data.get("cc"):
                entry["cc"] = data["cc"]
            if data.get("attachments"):
                entry["attachments"] = data["attachments"]
            self._inject_identity(entry, data)
            results.append(scrub_time_fields(self._agent, entry))

        result = {"status": "ok", "emails": results}
        if errors:
            result["not_found"] = errors
        return result

    def _lookup(self, email_id: str) -> dict | None:
        return self._load_email(email_id)

    def _reply(self, args: dict) -> dict:
        email_id = args.get("email_id", "")
        if isinstance(email_id, list):
            email_id = email_id[0] if email_id else ""
        if not email_id:
            return {"error": "email_id is required for reply"}
        message_text = args.get("message", "")
        if not message_text:
            return {"error": "message is required for reply"}

        original = self._lookup(email_id)
        if original is None:
            return {"error": f"Email not found: {email_id}"}

        orig_subject = original.get("subject", "")
        subject = args.get("subject") or (
            orig_subject if orig_subject.startswith("Re: ") else f"Re: {orig_subject}"
        )

        return self._send({
            "address": original["from"],
            "subject": subject,
            "message": message_text,
            "cc": args.get("cc") or [],
            "bcc": args.get("bcc") or [],
        })

    def _reply_all(self, args: dict) -> dict:
        email_id = args.get("email_id", "")
        if isinstance(email_id, list):
            email_id = email_id[0] if email_id else ""
        if not email_id:
            return {"error": "email_id is required for reply_all"}
        message_text = args.get("message", "")
        if not message_text:
            return {"error": "message is required for reply_all"}

        original = self._lookup(email_id)
        if original is None:
            return {"error": f"Email not found: {email_id}"}

        my_address = (
            self._agent._mail_service.address
            if self._agent._mail_service
            else self._agent._working_dir.name
        )

        reply_to = original["from"]
        orig_to = original.get("to") or []
        if isinstance(orig_to, str):
            orig_to = [orig_to]
        orig_cc = original.get("cc") or []
        other_recipients = [
            addr for addr in orig_to + orig_cc
            if addr != my_address and addr != reply_to
        ]

        extra_cc = args.get("cc") or []
        extra_bcc = args.get("bcc") or []

        orig_subject = original.get("subject", "")
        subject = args.get("subject") or (
            orig_subject if orig_subject.startswith("Re: ") else f"Re: {orig_subject}"
        )

        return self._send({
            "address": reply_to,
            "subject": subject,
            "message": message_text,
            "cc": other_recipients + extra_cc,
            "bcc": extra_bcc,
        })

    def _search(self, args: dict) -> dict:
        query = args.get("query", "")
        if not query:
            return {"error": "query is required for search"}

        folder = args.get("folder")
        folders = [folder] if folder else ["inbox", "sent"]

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        matches = []
        read_set = _read_ids(self._agent)
        for f in folders:
            for email in self._list_emails(f):
                searchable = " ".join([
                    email.get("from", ""),
                    email.get("subject", ""),
                    email.get("message", ""),
                ])
                if pattern.search(searchable):
                    matches.append(self._email_summary(email, read_set))

        return {"status": "ok", "total": len(matches), "emails": matches}

    def _archive(self, args: dict) -> dict:
        ids = args.get("email_id", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return {"error": "email_id is required"}

        archived = []
        not_found = []
        archive_dir = self._mailbox_path / "archive"
        inbox_dir = self._mailbox_path / "inbox"

        for eid in ids:
            src = inbox_dir / eid
            if not src.is_dir():
                not_found.append(eid)
                continue
            dst = archive_dir / eid
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            archived.append(eid)

        if archived:
            read_set = _read_ids(self._agent)
            read_set -= set(archived)
            _save_read_ids(self._agent, read_set)

        result: dict = {"status": "ok", "archived": archived}
        if not_found:
            result["not_found"] = not_found
        return result

    def _delete(self, args: dict) -> dict:
        ids = args.get("email_id", [])
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return {"error": "email_id is required"}

        folder = args.get("folder", "inbox")
        if folder not in ("inbox", "archive"):
            return {"error": f"Cannot delete from folder: {folder}"}

        folder_dir = self._mailbox_path / folder
        deleted = []
        not_found = []

        for eid in ids:
            target = folder_dir / eid
            if target.is_dir():
                shutil.rmtree(target)
                deleted.append(eid)
            else:
                not_found.append(eid)

        if deleted:
            read_set = _read_ids(self._agent)
            read_set -= set(deleted)
            _save_read_ids(self._agent, read_set)

        result: dict = {"status": "ok", "deleted": deleted}
        if not_found:
            result["not_found"] = not_found
        return result

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    @property
    def _contacts_path(self) -> Path:
        return self._mailbox_path / "contacts.json"

    def _load_contacts(self) -> list[dict]:
        if self._contacts_path.is_file():
            try:
                return json.loads(self._contacts_path.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_contacts(self, contacts: list[dict]) -> None:
        self._mailbox_path.mkdir(parents=True, exist_ok=True)
        target = self._contacts_path
        fd, tmp = tempfile.mkstemp(dir=str(self._mailbox_path), suffix=".tmp")
        try:
            os.write(fd, json.dumps(contacts, indent=2).encode())
            os.close(fd)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _contacts(self) -> dict:
        return {"status": "ok", "contacts": self._load_contacts()}

    def _add_contact(self, args: dict) -> dict:
        address = args.get("address", "")
        name = args.get("name", "")
        if not address:
            return {"error": "address is required"}
        if not name:
            return {"error": "name is required"}
        note = args.get("note", "")
        contacts = self._load_contacts()
        for c in contacts:
            if c["address"] == address:
                c["name"] = name
                c["note"] = note
                self._save_contacts(contacts)
                return {"status": "updated", "contact": c}
        entry: dict = {"address": address, "name": name, "note": note}
        contacts.append(entry)
        self._save_contacts(contacts)
        return {"status": "added", "contact": entry}

    def _remove_contact(self, args: dict) -> dict:
        address = args.get("address", "")
        if not address:
            return {"error": "address is required"}
        contacts = self._load_contacts()
        new_contacts = [c for c in contacts if c["address"] != address]
        if len(new_contacts) == len(contacts):
            return {"error": f"Contact not found: {address}"}
        self._save_contacts(new_contacts)
        return {"status": "removed", "address": address}

    def _edit_contact(self, args: dict) -> dict:
        address = args.get("address", "")
        if not address:
            return {"error": "address is required"}
        contacts = self._load_contacts()
        for c in contacts:
            if c["address"] == address:
                if "name" in args:
                    c["name"] = args["name"]
                if "note" in args:
                    c["note"] = args["note"]
                self._save_contacts(contacts)
                return {"status": "updated", "contact": c}
        return {"error": f"Contact not found: {address}"}


# ---------------------------------------------------------------------------
# Module-level intrinsic protocol — handle() + boot()
# ---------------------------------------------------------------------------


def handle(agent, args: dict) -> dict:
    """Module-level dispatcher — delegates to the agent's EmailManager.

    Boot must have run first to instantiate the manager. If not (e.g. someone
    calls handle() before boot() in a test harness), return a clear error.
    """
    mgr = getattr(agent, "_email_manager", None)
    if mgr is None:
        return {"error": "Internal: email manager not initialized. boot() was not called."}
    return mgr.handle(args)


def boot(agent) -> None:
    """Boot-time hook: instantiate manager, set agent fields, start scheduler.

    The intrinsic registration (add_tool with schema/handler/description) is
    done by _wire_intrinsics + ALL_INTRINSICS — this hook does the runtime
    setup that the registry can't: create the manager, wire it into the
    agent so module-level handle() can find it, and kick the scheduler.
    """
    mgr = EmailManager(agent)
    agent._email_manager = mgr
    agent._mailbox_name = "email box"
    agent._mailbox_tool = "email"
    mgr._reconcile_schedules_on_startup()
    mgr.start_scheduler()
