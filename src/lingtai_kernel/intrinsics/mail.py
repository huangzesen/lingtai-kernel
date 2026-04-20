"""Mail intrinsic — disk-backed mailbox with 5 actions.

Actions:
    send   — fire-and-forget message to an address (self-send short-circuits TCP)
    check  — list inbox summaries (newest first, with unread flags)
    read   — load full message(s) by ID, mark as read
    search — regex search across from/subject/message fields
    delete — remove message(s) from disk
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..time_veil import scrub_time_fields

def get_description(lang: str = "en") -> str:
    from ..i18n import t
    return t(lang, "mail.description")


def get_schema(lang: str = "en") -> dict:
    from ..i18n import t
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "check", "read", "search", "delete"],
                "description": t(lang, "mail.action_description"),
            },
            "address": {
                "type": "string",
                "description": t(lang, "mail.address_description"),
            },
            "subject": {"type": "string", "description": t(lang, "mail.subject_description")},
            "message": {"type": "string", "description": t(lang, "mail.message_description")},
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "mail.attachments_description"),
            },
            "type": {
                "type": "string",
                "enum": ["normal"],
                "description": t(lang, "mail.type_description"),
            },
            "delay": {
                "type": "integer",
                "description": t(lang, "mail.delay_description"),
            },
            "id": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "mail.id_description"),
            },
            "n": {
                "type": "integer",
                "description": t(lang, "mail.n_description"),
            },
            "query": {
                "type": "string",
                "description": t(lang, "mail.query_description"),
            },
            "mode": {
                "type": "string",
                "enum": ["rel", "abs", "ssh"],
                "description": t(lang, "mail.mode_description"),
            },
        },
        "required": ["action"],
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def handle(agent, args: dict) -> dict:
    """Handle mail tool — dispatch to action handler."""
    action = args.get("action", "send")
    handler = {
        "send": _send,
        "check": _check,
        "read": _read,
        "search": _search,
        "delete": _delete,
    }.get(action)
    if handler is None:
        return {"error": f"Unknown mail action: {action}"}
    return handler(agent, args)


# ---------------------------------------------------------------------------
# Mailbox helpers
# ---------------------------------------------------------------------------

def _mailbox_dir(agent) -> Path:
    """Return the mailbox root directory."""
    return agent._working_dir / "mailbox"


def _inbox_dir(agent) -> Path:
    """Return the inbox directory."""
    return _mailbox_dir(agent) / "inbox"


def _load_message(agent, msg_id: str) -> dict | None:
    """Load a single message by ID, or None if not found."""
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
    # Sort newest first by received_at
    messages.sort(key=lambda m: m.get("received_at", ""), reverse=True)
    return messages


def _read_ids_path(agent) -> Path:
    """Path to the read.json tracking file."""
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
    import os
    os.replace(str(tmp), str(path))


def _mark_read(agent, msg_id: str) -> None:
    """Mark a message as read."""
    ids = _read_ids(agent)
    ids.add(msg_id)
    _save_read_ids(agent, ids)


def _summary_to_list(raw) -> list[str]:
    """Best-effort coercion of to/cc for display.

    Minimal — only ensures list shape so the display doesn't iterate a
    string char-by-char. Does not JSON-unwrap (the reader-side
    _normalize_address_list handles that for delivery; display only
    needs list shape).
    """
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


# ---------------------------------------------------------------------------
# Self-send helpers
# ---------------------------------------------------------------------------

def _is_self_send(agent, address: str) -> bool:
    """Check if the address matches this agent (by directory name or full path)."""
    # Match by directory name (relative address)
    if address == agent._working_dir.name:
        return True
    # Match by full working directory path (legacy absolute)
    if address == str(agent._working_dir):
        return True
    # Match by mail service address
    if agent._mail_service is not None and agent._mail_service.address:
        if address == agent._mail_service.address:
            return True
    return False


def _persist_to_inbox(agent, payload: dict) -> str:
    """Persist a message directly to mailbox/inbox/{uuid}/message.json.

    Returns the message ID.
    """
    msg_id = str(uuid.uuid4())
    msg_dir = _inbox_dir(agent) / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)

    payload = dict(payload)  # shallow copy
    payload["_mailbox_id"] = msg_id
    payload["received_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    (msg_dir / "message.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    return msg_id


def _outbox_dir(agent) -> Path:
    """Return the outbox directory."""
    return _mailbox_dir(agent) / "outbox"


def _sent_dir(agent) -> Path:
    """Return the sent directory."""
    return _mailbox_dir(agent) / "sent"


def _persist_to_outbox(agent, payload: dict, deliver_at: datetime) -> str:
    """Write a message to outbox/{uuid}/message.json. Returns the message ID."""
    msg_id = str(uuid.uuid4())
    msg_dir = _outbox_dir(agent) / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)

    payload = dict(payload)  # shallow copy
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


def _deliver_ssh(address: str, payload: dict, msg_id: str) -> str | None:
    """Deliver a message to a remote agent's inbox via SSH.

    Address format: user@host:/path/to/.lingtai/agent_name

    Writes message.json into the remote agent's mailbox/inbox/{msg_id}/
    using ssh + cat. Returns None on success, error string on failure.
    """
    import subprocess

    # Parse user@host:/path from address
    if ":" not in address:
        return (f"SSH delivery failed — address must be user@host:/path/to/.lingtai/agent_name, "
                f"got {address!r}. Check that you are using mode='ssh' with a remote address.")
    ssh_target, remote_path = address.split(":", 1)
    if not ssh_target or not remote_path:
        return f"SSH delivery failed — invalid address: {address!r}"
    if "@" not in ssh_target:
        return (f"SSH delivery failed — address must include user@host, "
                f"got {ssh_target!r}. Format: user@host:/path/to/.lingtai/agent_name")

    remote_inbox = f"{remote_path}/mailbox/inbox/{msg_id}"
    remote_file = f"{remote_inbox}/message.json"

    # Inject mailbox metadata
    payload = {
        **payload,
        "_mailbox_id": msg_id,
        "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Remove internal fields
    payload.pop("_mode", None)
    payload.pop("_dispatch_to", None)

    msg_json = json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    try:
        # Create remote directory and write message in one ssh call
        cmd = (
            f"mkdir -p {remote_inbox} && "
            f"cat > {remote_file}"
        )
        result = subprocess.run(
            ["ssh", ssh_target, cmd],
            input=msg_json,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return (f"SSH delivery failed to {ssh_target} — {stderr}. "
                    f"Check: 1) SSH key is set up (ssh {ssh_target} should work without password), "
                    f"2) remote path {remote_path} exists, "
                    f"3) remote agent has a mailbox/inbox/ directory.")
        return None
    except subprocess.TimeoutExpired:
        return (f"SSH delivery timed out connecting to {ssh_target}. "
                f"Check: 1) host is reachable, 2) SSH is running on remote, "
                f"3) no firewall blocking port 22.")
    except FileNotFoundError:
        return "SSH delivery failed — 'ssh' command not found. Is OpenSSH installed?"
    except OSError as e:
        return f"SSH delivery failed — OS error: {e}"


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

    mode = payload.pop("_mode", "rel")

    err = None
    try:
        if mode == "ssh":
            err = _deliver_ssh(address, payload, msg_id)
            status = "delivered" if err is None else "refused"
        elif _is_self_send(agent, address):
            _persist_to_inbox(agent, payload)
            agent._wake_nap("mail_arrived")
            status = "delivered"
        elif agent._mail_service is not None:
            err = agent._mail_service.send(address, payload, mode=mode)
            status = "delivered" if err is None else "refused"
        else:
            err = f"No mail service configured"
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

    # Bounce notification — tell the agent delivery failed
    if status == "refused" and err:
        from ..i18n import t as _t
        from ..message import _make_message, MSG_REQUEST

        notification = _t(
            agent._config.language, "system.mail_bounce",
            error=err, address=address,
            subject=payload.get("subject", "(no subject)"),
        )
        msg = _make_message(MSG_REQUEST, "system", notification)
        agent.inbox.put(msg)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _send(agent, args: dict) -> dict:
    """Send a message — validate, write to outbox, spawn mailman."""
    address = args.get("address", "")
    subject = args.get("subject", "")
    message_text = args.get("message", "")
    mail_type = args.get("type", "normal")
    delay = args.get("delay", 0)
    mode = args.get("mode", "rel")

    if not address:
        return {"error": "address is required"}
    if mode not in ("rel", "abs", "ssh"):
        return {"error": f"invalid mode: {mode!r} (must be rel, abs, or ssh)"}

    payload = {
        "from": (agent._mail_service.address if agent._mail_service is not None and agent._mail_service.address else agent._working_dir.name),
        "to": [address] if address else [],
        "subject": subject,
        "message": message_text,
        "type": mail_type,
        "identity": agent._build_manifest(),
        "_mode": mode,  # rel/abs/ssh — consumed by _mailman, not persisted
    }

    attachments = args.get("attachments", [])
    if attachments:
        resolved = []
        for p in attachments:
            path = Path(p)
            if not path.is_absolute():
                path = agent._working_dir / path
            if not path.is_file():
                return {"error": f"Attachment not found: {path}"}
            resolved.append(str(path))
        payload["attachments"] = resolved

    deliver_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    msg_id = _persist_to_outbox(agent, payload, deliver_at)

    t = threading.Thread(
        target=_mailman,
        args=(agent, msg_id, payload, deliver_at),
        name=f"mailman-{msg_id[:8]}",
        daemon=True,
    )
    t.start()

    return {"status": "sent", "to": address, "delay": delay}


def _check(agent, args: dict) -> dict:
    """List inbox summaries with unread flags."""
    messages = _list_inbox(agent)
    read_set = _read_ids(agent)
    total = len(messages)

    n = args.get("n")
    shown = messages[:n] if n is not None else messages

    summaries = [scrub_time_fields(agent, _message_summary(m, read_set)) for m in shown]
    unread_count = sum(1 for m in messages if m.get("_mailbox_id", "") not in read_set)

    return {
        "total": total,
        "unread": unread_count,
        "shown": len(summaries),
        "messages": summaries,
    }


def _read(agent, args: dict) -> dict:
    """Load full message(s) by ID, mark as read."""
    ids = args.get("id", [])
    if not ids:
        return {"error": "id is required (array of message IDs)"}

    results = []
    not_found = []
    for msg_id in ids:
        msg = _load_message(agent, msg_id)
        if msg is None:
            not_found.append(msg_id)
        else:
            _mark_read(agent, msg_id)
            results.append(msg)

    scrubbed_results = [scrub_time_fields(agent, m) for m in results]
    response: dict = {"messages": scrubbed_results}
    if not_found:
        response["not_found"] = not_found
    return response


def _search(agent, args: dict) -> dict:
    """Regex search across from/subject/message fields."""
    query = args.get("query", "")
    if not query:
        return {"error": "query is required"}

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}

    messages = _list_inbox(agent)
    read_set = _read_ids(agent)
    matches = []
    for msg in messages:
        fields = [
            msg.get("from", ""),
            msg.get("subject", ""),
            msg.get("message", ""),
        ]
        if any(pattern.search(f) for f in fields):
            matches.append(_message_summary(msg, read_set))

    scrubbed_matches = [scrub_time_fields(agent, m) for m in matches]
    return {"total": len(scrubbed_matches), "messages": scrubbed_matches}


def _delete(agent, args: dict) -> dict:
    """Remove message(s) from disk and clean read tracking."""
    ids = args.get("id", [])
    if not ids:
        return {"error": "id is required (array of message IDs)"}

    deleted = []
    not_found = []
    for msg_id in ids:
        msg_dir = _inbox_dir(agent) / msg_id
        if msg_dir.is_dir():
            shutil.rmtree(msg_dir)
            deleted.append(msg_id)
        else:
            not_found.append(msg_id)

    # Clean read tracking
    if deleted:
        read_set = _read_ids(agent)
        read_set -= set(deleted)
        _save_read_ids(agent, read_set)

    response: dict = {"deleted": deleted}
    if not_found:
        response["not_found"] = not_found
    return response
