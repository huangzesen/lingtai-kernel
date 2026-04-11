"""MailService — abstract message transport backing the mail intrinsic.

Implementation: FilesystemMailService (directory-based inbox delivery).

Design principles:
- Fire-and-forget: send() returns immediately, no request/response coupling
- Inbox model: listener polls for new messages in the agent's inbox directory
- No registry: the caller must know the address (discovery is external)
- Address = working directory name (relative basename, e.g. "本我")
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from ..handshake import is_agent, is_alive, manifest, resolve_address


class MailService(ABC):
    """Abstract message transport service.

    Backs the mail intrinsic. Implementations provide the actual
    transport mechanism.
    """

    @abstractmethod
    def send(
        self,
        address: str,
        message: dict,
    ) -> str | None:
        """Send a message to an address. Returns None on success, error string on failure.

        Fire-and-forget — does not wait for a response.
        The address format is transport-specific (filesystem path for FilesystemMailService).

        Parameters
        ----------
        address:
            Recipient's address (working directory name or absolute path).
        message:
            Payload dict to deliver.
        """
        ...

    @abstractmethod
    def listen(self, on_message: Callable[[dict], None]) -> None:
        """Start listening for incoming messages.

        on_message is called for each received message.
        This should be non-blocking (start a background thread).
        """
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and clean up resources."""
        ...

    @property
    @abstractmethod
    def address(self) -> str:
        """This service's address (the agent's working directory name)."""
        ...


class FilesystemMailService(MailService):
    """Filesystem-based mail delivery.

    Delivers messages by writing files directly to the recipient's inbox
    directory.  Monitors its own inbox via polling.

    Address = working directory name (relative basename).  Example::

        svc = FilesystemMailService(Path("/agents/abc123"))
        svc.listen(on_message=lambda msg: print(msg))  # poll own inbox
        svc.send("def456", {"message": "hello"})  # write to sibling agent
    """

    def __init__(
        self,
        working_dir: str | Path,
        mailbox_rel: str = "mailbox",
    ) -> None:
        self._working_dir = Path(working_dir)
        self._mailbox_rel = mailbox_rel
        self._mailbox_dir = self._working_dir / mailbox_rel
        self._inbox_dir = self._mailbox_dir / "inbox"
        self._inbox_dir.mkdir(parents=True, exist_ok=True)

        # Polling state
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._seen: set[str] = set()

    # ------------------------------------------------------------------
    # address
    # ------------------------------------------------------------------

    @property
    def address(self) -> str:
        """Return the working directory name as this agent's mail address."""
        return self._working_dir.name

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    def send(
        self,
        address: str,
        message: dict,
    ) -> str | None:
        """Deliver *message* to the agent at *address*.

        Handshake:
        1. ``{address}/.agent.json`` must exist.
        2. ``{address}/.agent.heartbeat`` must be fresh (< 2 s).

        Then write ``message.json`` atomically into the recipient's inbox
        and copy any attachment files.
        """
        base_dir = self._working_dir.parent  # .lingtai/ directory
        recipient_dir = resolve_address(address, base_dir)

        # --- handshake ------------------------------------------------
        if not is_agent(recipient_dir):
            return f"No agent at {address}"

        if not is_alive(recipient_dir):
            return f"Agent at {address} is not running"

        # --- create inbox entry ---------------------------------------
        msg_id = str(uuid.uuid4())
        inbox_dir = recipient_dir / self._mailbox_rel / "inbox"
        msg_dir = inbox_dir / msg_id

        # Inject mailbox metadata (required by mail intrinsic for
        # message tracking, read/unread, reply, archive, delete).
        from datetime import datetime, timezone
        message = {
            **message,
            "_mailbox_id": msg_id,
            "received_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }

        # Handle attachments
        attachment_paths = message.get("attachments")
        if attachment_paths:
            att_dir = msg_dir / "attachments"
            att_dir.mkdir(parents=True, exist_ok=True)
            local_copies: list[str] = []
            for fpath in attachment_paths:
                src = Path(fpath)
                if not src.is_file():
                    return f"Attachment not found: {fpath}"
                dst = att_dir / src.name
                shutil.copy2(src, dst)
                local_copies.append(str(dst))
            # Replace original paths with recipient-local paths
            message = {**message, "attachments": local_copies}
        else:
            msg_dir.mkdir(parents=True, exist_ok=True)

        # Atomic write: tmp → rename
        tmp_path = msg_dir / "message.json.tmp"
        final_path = msg_dir / "message.json"
        try:
            tmp_path.write_text(
                json.dumps(message, indent=2, ensure_ascii=False, default=str)
            )
            os.replace(str(tmp_path), str(final_path))
        except OSError as e:
            return f"Failed to write message: {e}"

        return None

    # ------------------------------------------------------------------
    # listen / stop
    # ------------------------------------------------------------------

    def listen(self, on_message: Callable[[dict], None]) -> None:
        """Start polling the inbox for new messages.

        Existing messages are recorded in ``_seen`` so they are not
        re-delivered.  New directories that appear with a ``message.json``
        trigger *on_message*.
        """
        # Snapshot existing inbox entries so we don't re-notify
        if self._inbox_dir.is_dir():
            for entry in self._inbox_dir.iterdir():
                if entry.is_dir():
                    self._seen.add(entry.name)

        self._poll_stop.clear()

        def _poll_loop() -> None:
            while not self._poll_stop.is_set():
                try:
                    if self._inbox_dir.is_dir():
                        for entry in self._inbox_dir.iterdir():
                            if not entry.is_dir():
                                continue
                            if entry.name in self._seen:
                                continue
                            msg_file = entry / "message.json"
                            if msg_file.is_file():
                                try:
                                    payload = json.loads(msg_file.read_text())
                                    on_message(payload)
                                except (json.JSONDecodeError, OSError):
                                    pass
                                self._seen.add(entry.name)
                except OSError:
                    pass
                self._poll_stop.wait(0.5)

        self._poll_thread = threading.Thread(target=_poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        """Stop the polling thread."""
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3.0)
        self._poll_thread = None
