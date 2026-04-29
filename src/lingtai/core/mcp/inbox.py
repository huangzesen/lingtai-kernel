"""LICC v1 — LingTai Inbox Callback Contract.

A filesystem-based inbox that lets out-of-process MCP servers push events
into the agent's inbox. The contract:

- MCPs write JSON event files to:
      <agent_working_dir>/.mcp_inbox/<mcp_name>/<event_id>.json
- Atomic write: write to ``<event_id>.json.tmp``, fsync, then rename.
- Schema (v1):
      {
        "licc_version": 1,
        "from": "human-readable sender (required)",
        "subject": "one-line summary (required, max 200 chars)",
        "body": "full message body (required)",
        "metadata": {...optional},
        "wake": true,                 // optional, default true
        "received_at": "ISO 8601"     // optional, kernel fills in if missing
      }
- The kernel polls all subdirs at the same cadence as the mailbox listener
  (0.5s), validates each file, dispatches to the agent's inbox via
  _make_message, calls _wake_nap when wake=true, and deletes the file.
- Invalid files (parse error, missing required fields, unknown version)
  are moved to a sibling ``.dead/`` directory with a ``.error.json``
  describing the failure. Dead-letters are never auto-deleted.

The MCP subprocess learns where to write via two env vars injected by the
kernel's MCP loader:
    LINGTAI_AGENT_DIR  — absolute path of the agent's working directory.
    LINGTAI_MCP_NAME   — the MCP's registry name.

Both vars are injected automatically for every MCP the kernel spawns, so
any MCP that wants to use LICC just needs to read them and write files.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

log = logging.getLogger(__name__)

INBOX_DIRNAME = ".mcp_inbox"
DEAD_DIRNAME = ".dead"
TMP_SUFFIX = ".json.tmp"
EVENT_SUFFIX = ".json"
LICC_VERSION = 1

POLL_INTERVAL = 0.5  # seconds; matches FilesystemMailService poll cadence
MAX_EVENTS_PER_CYCLE = 100
_MAX_SUBJECT_LEN = 200


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_event(event: dict) -> tuple[bool, str | None]:
    """Validate a parsed LICC event. Returns (is_valid, error)."""
    if not isinstance(event, dict):
        return False, "event must be a JSON object"

    version = event.get("licc_version", LICC_VERSION)
    if version != LICC_VERSION:
        return False, f"unsupported licc_version: {version!r} (this kernel speaks {LICC_VERSION})"

    sender = event.get("from")
    if not isinstance(sender, str) or not sender:
        return False, "missing or empty field: from"

    subject = event.get("subject")
    if not isinstance(subject, str) or not subject:
        return False, "missing or empty field: subject"
    if len(subject) > _MAX_SUBJECT_LEN:
        return False, f"subject too long ({len(subject)} > {_MAX_SUBJECT_LEN} chars)"

    body = event.get("body")
    if not isinstance(body, str):
        return False, "missing or non-string field: body"

    metadata = event.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return False, "metadata must be a JSON object when present"

    wake = event.get("wake", True)
    if not isinstance(wake, bool):
        return False, "wake must be a boolean when present"

    return True, None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _format_notification(mcp_name: str, event: dict) -> str:
    """Render an LICC event into the [system] notification body that lands
    in the agent's inbox. Mirrors the prose IMAPMailManager used to use
    pre-MCP, so agent-side prompts and habits keep working."""
    sender = event["from"]
    subject = event["subject"]
    body = event["body"]
    preview = body[:200].replace("\n", " ")
    return (
        f"[system] New event from MCP '{mcp_name}'.\n"
        f"  From: {sender}\n"
        f"  Subject: {subject}\n"
        f"  {preview}{'...' if len(body) > 200 else ''}"
    )


def _dispatch_event(agent: "BaseAgent", mcp_name: str, event: dict) -> None:
    """Hand a validated event to the agent's inbox."""
    from lingtai_kernel.message import _make_message, MSG_REQUEST

    notification = _format_notification(mcp_name, event)
    msg = _make_message(MSG_REQUEST, "system", notification)
    agent.inbox.put(msg)

    if event.get("wake", True):
        agent._wake_nap("mcp_event")

    agent._log(
        "mcp_inbox_event",
        mcp=mcp_name,
        sender=event["from"],
        subject=event["subject"],
        wake=event.get("wake", True),
    )


# ---------------------------------------------------------------------------
# Dead-letter
# ---------------------------------------------------------------------------

def _dead_letter(file_path: Path, error: str) -> None:
    """Move *file_path* into a ``.dead/`` subdir with a sibling error file."""
    dead_dir = file_path.parent / DEAD_DIRNAME
    try:
        dead_dir.mkdir(parents=True, exist_ok=True)
        target = dead_dir / file_path.name
        error_target = dead_dir / (file_path.stem + ".error.json")
        # Move the bad event, write the error report alongside it.
        shutil.move(str(file_path), str(target))
        error_target.write_text(
            json.dumps({"error": error, "timestamp": _now_iso()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.warning("mcp_inbox: dead-lettered %s: %s", target, error)
    except OSError as e:
        log.error("mcp_inbox: failed to dead-letter %s: %s (original error: %s)",
                  file_path, e, error)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scan_once(agent: "BaseAgent", inbox_root: Path) -> int:
    """One sweep through .mcp_inbox/<mcp_name>/*.json. Returns count dispatched."""
    if not inbox_root.is_dir():
        return 0

    dispatched = 0
    for mcp_dir in inbox_root.iterdir():
        if not mcp_dir.is_dir():
            continue
        if mcp_dir.name.startswith("."):
            # Skip our own .dead and any other dotted dirs.
            continue

        mcp_name = mcp_dir.name
        # Bound work per cycle to avoid pathological backlog blocking.
        events_this_mcp = 0
        for entry in sorted(mcp_dir.iterdir()):
            if events_this_mcp >= MAX_EVENTS_PER_CYCLE:
                break
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(TMP_SUFFIX):
                # Half-written; ignore until rename completes.
                continue
            if not name.endswith(EVENT_SUFFIX):
                continue

            # Read + parse + validate.
            try:
                raw = entry.read_text(encoding="utf-8")
            except OSError as e:
                _dead_letter(entry, f"read failed: {e}")
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError as e:
                _dead_letter(entry, f"invalid JSON: {e}")
                continue

            ok, err = validate_event(event)
            if not ok:
                _dead_letter(entry, f"validation failed: {err}")
                continue

            # Dispatch and delete on success.
            try:
                _dispatch_event(agent, mcp_name, event)
            except Exception as e:
                _dead_letter(entry, f"dispatch failed: {e}")
                continue

            try:
                entry.unlink()
            except OSError as e:
                # Won't double-deliver because the file's still there for
                # next cycle to re-attempt; we logged the dispatch already
                # though. Edge case worth noting in the agent log.
                log.warning("mcp_inbox: dispatched but failed to delete %s: %s", entry, e)

            dispatched += 1
            events_this_mcp += 1

    return dispatched


# ---------------------------------------------------------------------------
# Poller — owned by the Agent
# ---------------------------------------------------------------------------

class MCPInboxPoller:
    """Polls the agent's .mcp_inbox/ tree. One per agent. Daemon thread."""

    def __init__(self, agent: "BaseAgent"):
        self._agent = agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inbox_root = Path(agent._working_dir) / INBOX_DIRNAME

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # Pre-create the root so MCPs can write before any event lands.
        try:
            self._inbox_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("mcp_inbox: cannot create %s: %s", self._inbox_root, e)
            return

        agent_label = self._agent.agent_name or self._agent._working_dir.name

        def _loop() -> None:
            while not self._stop.is_set():
                try:
                    _scan_once(self._agent, self._inbox_root)
                except Exception as e:
                    log.error("mcp_inbox: scan failed for %s: %s", agent_label, e)
                self._stop.wait(POLL_INTERVAL)

        self._thread = threading.Thread(
            target=_loop,
            daemon=True,
            name=f"mcp-inbox-{agent_label}",
        )
        self._thread.start()
        log.info("mcp_inbox: poller started for %s (root=%s)",
                 agent_label, self._inbox_root)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
