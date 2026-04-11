"""WeChat addon manager — tool dispatch, message persistence, bridge."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .types import (
    MessageItemType, WeixinMessage, MessageItem, TextItem,
    msg_from_dict, msg_to_dict,
)
from . import api
from . import media as media_mod

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

log = logging.getLogger(__name__)

TEXT_CHUNK_LIMIT = 4000
SESSION_EXPIRED_ERRCODE = -14

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "send", "check", "read", "reply", "search",
                "contacts", "add_contact", "remove_contact",
            ],
            "description": (
                "send: send a message to a WeChat user "
                "(user_id, text; optional media_path for file/image/voice/video). "
                "check: list recent conversations with unread counts. "
                "read: read messages from a user (user_id; optional limit). "
                "reply: reply to a specific message "
                "(message_id from read results, text). "
                "search: search inbox messages by regex "
                "(query; optional user_id). "
                "contacts: list saved contacts. "
                "add_contact: save a contact (user_id, alias). "
                "remove_contact: remove a contact (alias or user_id)."
            ),
        },
        "user_id": {
            "type": "string",
            "description": "WeChat user ID (e.g. wxid_abc123@im.wechat)",
        },
        "text": {
            "type": "string",
            "description": "Message text content",
        },
        "media_path": {
            "type": "string",
            "description": (
                "Absolute path to a file to send as media. "
                "Type detected from extension: "
                ".jpg/.png=image, .mp4=video, .wav/.mp3=voice, other=file."
            ),
        },
        "message_id": {
            "type": "string",
            "description": "Message ID from read results (for reply action)",
        },
        "limit": {
            "type": "integer",
            "description": "Max messages to return (for read, default 10)",
            "default": 10,
        },
        "query": {
            "type": "string",
            "description": "Search query (regex pattern)",
        },
        "alias": {
            "type": "string",
            "description": "Human-friendly contact alias",
        },
    },
    "required": ["action"],
}

DESCRIPTION = (
    "WeChat client — interact with WeChat users via iLink Bot API. "
    "Supports text, images, voice, video, and files. "
    "Use 'send' for outgoing messages (text and/or media_path). "
    "'check' to see recent conversations with unread counts. "
    "'read' to read messages from a user. "
    "'reply' to respond to a message. "
    "'search' to find messages by keyword or regex. "
    "'contacts' to manage saved contacts."
)


class WechatManager:
    """Manages WeChat addon lifecycle, tool dispatch, and message storage."""

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        base_url: str = api.DEFAULT_BASE_URL,
        cdn_base_url: str = api.CDN_BASE_URL,
        token: str,
        user_id: str,
        poll_interval: float = 1.0,
        allowed_users: list[str] | None = None,
        working_dir: Path,
    ) -> None:
        self._agent = agent
        self._base_url = base_url
        self._cdn_base_url = cdn_base_url
        self._token = token
        self._user_id = user_id
        self._poll_interval = poll_interval
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._working_dir = working_dir

        # Filesystem dirs
        self._wechat_dir = working_dir / "wechat"
        self._inbox_dir = self._wechat_dir / "inbox"
        self._sent_dir = self._wechat_dir / "sent"
        self._media_dir = self._wechat_dir / "media"
        for d in (self._inbox_dir, self._sent_dir, self._media_dir):
            d.mkdir(parents=True, exist_ok=True)

        # State
        self._get_updates_buf = ""
        self._context_tokens: dict[str, str] = {}  # user_id -> context_token
        self._contacts: dict[str, dict] = {}  # alias -> {user_id, name}
        self._read_ids: set[str] = set()
        self._lock = threading.Lock()  # guards shared mutable state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll_thread: threading.Thread | None = None
        self._running = False

        # Load persisted state
        self._load_state()

    def start(self) -> None:
        """Start the long-poll loop on a dedicated daemon thread."""
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._poll_thread = threading.Thread(
            target=self._loop.run_until_complete,
            args=(self._poll_loop(),),
            daemon=True,
            name="wechat-poll",
        )
        self._poll_thread.start()
        log.info("WeChat addon started for %s", self._user_id)

    def stop(self) -> None:
        """Stop the long-poll loop and join the thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=40.0)  # long-poll is 35s
        self._save_state()
        log.info("WeChat addon stopped")

    # ── Poll loop ──────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                resp = await api.get_updates(
                    self._base_url, self._token, self._get_updates_buf,
                )

                # Check for session expiry
                if resp.errcode == SESSION_EXPIRED_ERRCODE:
                    log.warning("WeChat session expired (errcode -14)")
                    self._notify_session_expired()
                    self._running = False
                    return

                if resp.get_updates_buf:
                    self._get_updates_buf = resp.get_updates_buf

                for msg in resp.msgs:
                    await self._on_incoming(msg)

            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("WeChat poll error: %s", e)

            await asyncio.sleep(self._poll_interval)

    def _notify_session_expired(self) -> None:
        """Send internal notification that WeChat session expired."""
        try:
            from lingtai_kernel.message import _make_message, MSG_REQUEST
            notification = (
                "[system] WeChat session expired. "
                "Please ask me to re-login to WeChat."
            )
            msg = _make_message(MSG_REQUEST, "system", notification)
            self._agent.inbox.put(msg)
            self._agent._wake_nap("message_received")
        except Exception as e:
            log.error("Failed to notify session expiry: %s", e)

    # ── Incoming message processing ────────────────────────────

    async def _on_incoming(self, msg: WeixinMessage) -> None:
        """Process an incoming WeChat message."""
        from_user = msg.from_user_id or ""

        # Skip messages from the bot itself (echo prevention)
        if from_user == self._user_id:
            return

        # Filter by allowed_users
        if self._allowed_users and from_user not in self._allowed_users:
            return

        # Cache context token (lock for cross-thread safety)
        if msg.context_token:
            with self._lock:
                self._context_tokens[from_user] = msg.context_token

        # Build text representation
        body_parts: list[str] = []
        for item in msg.item_list:
            item_type = item.type or 0
            if item_type == MessageItemType.TEXT:
                if item.text_item and item.text_item.text:
                    body_parts.append(item.text_item.text)

            elif item_type == MessageItemType.IMAGE:
                if item.image_item and item.image_item.media:
                    try:
                        ext = ".jpg"
                        fname = f"{uuid.uuid4().hex}{ext}"
                        path = await media_mod.download_media(
                            item.image_item.media, self._media_dir, fname,
                        )
                        body_parts.append(f"[Image: {path}]")
                    except Exception as e:
                        body_parts.append(f"[Image: download failed — {e}]")

            elif item_type == MessageItemType.VOICE:
                if item.voice_item:
                    transcription = item.voice_item.text or ""
                    audio_path = ""
                    if item.voice_item.media:
                        try:
                            silk_name = f"{uuid.uuid4().hex}.silk"
                            silk_path = await media_mod.download_media(
                                item.voice_item.media, self._media_dir, silk_name,
                            )
                            wav_path = silk_path.replace(".silk", ".wav")
                            audio_path = media_mod.decode_voice(silk_path, wav_path)
                        except Exception as e:
                            audio_path = f"download failed — {e}"
                    if transcription and audio_path:
                        body_parts.append(
                            f'[Voice: "{transcription}" (audio: {audio_path})]'
                        )
                    elif transcription:
                        body_parts.append(f'[Voice: "{transcription}"]')
                    elif audio_path:
                        body_parts.append(f"[Voice: (audio: {audio_path})]")

            elif item_type == MessageItemType.FILE:
                if item.file_item and item.file_item.media:
                    try:
                        fname = item.file_item.file_name or f"{uuid.uuid4().hex}"
                        path = await media_mod.download_media(
                            item.file_item.media, self._media_dir, fname,
                        )
                        body_parts.append(f"[File: {fname} ({path})]")
                    except Exception as e:
                        body_parts.append(f"[File: download failed — {e}]")

            elif item_type == MessageItemType.VIDEO:
                if item.video_item and item.video_item.media:
                    try:
                        fname = f"{uuid.uuid4().hex}.mp4"
                        path = await media_mod.download_media(
                            item.video_item.media, self._media_dir, fname,
                        )
                        body_parts.append(f"[Video: {path}]")
                    except Exception as e:
                        body_parts.append(f"[Video: download failed — {e}]")

        body = "\n".join(body_parts) if body_parts else "(empty message)"

        # Persist to inbox
        msg_id = str(uuid.uuid4())
        msg_dir = self._inbox_dir / msg_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        msg_data = {
            "id": msg_id,
            "from_user_id": from_user,
            "body": body,
            "date": datetime.now(timezone.utc).isoformat(),
            "raw_item_types": [item.type for item in msg.item_list],
        }
        (msg_dir / "message.json").write_text(
            json.dumps(msg_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        # Notify agent
        try:
            from lingtai_kernel.message import _make_message, MSG_REQUEST
            contact = self._find_contact_by_user_id(from_user)
            display = contact.get("alias", from_user) if contact else from_user
            notification = f"[system] New WeChat message from {display}: {body[:200]}"
            kernel_msg = _make_message(MSG_REQUEST, "system", notification)
            self._agent.inbox.put(kernel_msg)
            self._agent._wake_nap("message_received")
        except Exception as e:
            log.error("Failed to notify agent: %s", e)

    # ── Tool handler dispatch ──────────────────────────────────

    def handle(self, args: dict) -> dict:
        action = args.get("action")
        try:
            if action == "send":
                return self._handle_send(args)
            elif action == "check":
                return self._handle_check(args)
            elif action == "read":
                return self._handle_read(args)
            elif action == "reply":
                return self._handle_reply(args)
            elif action == "search":
                return self._handle_search(args)
            elif action == "contacts":
                return self._handle_contacts()
            elif action == "add_contact":
                return self._handle_add_contact(args)
            elif action == "remove_contact":
                return self._handle_remove_contact(args)
            else:
                return {"error": f"Unknown wechat action: {action!r}"}
        except Exception as e:
            return {"error": str(e)}

    # ── Action handlers ────────────────────────────────────────

    def _handle_send(self, args: dict) -> dict:
        user_id = args.get("user_id")
        text = args.get("text", "")
        media_path = args.get("media_path")

        if not user_id:
            return {"error": "user_id is required for send"}
        if not text and not media_path:
            return {"error": "text or media_path is required"}

        # Validate media_path before sending text to avoid partial sends
        if media_path and not Path(media_path).is_file():
            return {"error": f"File not found: {media_path}"}

        results = []

        # Snapshot context token under lock (poll thread may update it)
        with self._lock:
            ctx_token = self._context_tokens.get(user_id)

        # Send text (chunked if needed)
        if text:
            chunks = _chunk_text(text, TEXT_CHUNK_LIMIT)
            for chunk in chunks:
                msg = WeixinMessage(
                    to_user_id=user_id,
                    context_token=ctx_token,
                    item_list=[MessageItem(
                        type=int(MessageItemType.TEXT),
                        text_item=TextItem(text=chunk),
                    )],
                )
                self._run_async(
                    api.send_message(self._base_url, self._token, msg)
                )
                results.append(f"text ({len(chunk)} chars)")

        # Send media (already validated above)
        if media_path:
            path = Path(media_path)
            cdn_media = self._run_async(
                media_mod.upload_media(path, self._base_url, self._token, user_id)
            )
            media_item = media_mod.make_media_item(cdn_media, path)
            msg = WeixinMessage(
                to_user_id=user_id,
                context_token=ctx_token,
                item_list=[media_item],
            )
            self._run_async(
                api.send_message(self._base_url, self._token, msg)
            )
            results.append(f"media ({path.name})")

        # Persist to sent
        msg_id = str(uuid.uuid4())
        msg_dir = self._sent_dir / msg_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        sent_data = {
            "id": msg_id,
            "to_user_id": user_id,
            "text": text,
            "media_path": media_path,
            "date": datetime.now(timezone.utc).isoformat(),
        }
        (msg_dir / "message.json").write_text(
            json.dumps(sent_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        return {"status": "ok", "sent": results, "message_id": msg_id}

    def _handle_check(self, args: dict) -> dict:
        """List conversations with unread counts."""
        all_msgs = self._load_inbox_messages()
        conversations: dict[str, dict] = {}
        for data in all_msgs:
            user = data.get("from_user_id", "unknown")
            msg_id = data.get("id", "")
            if user not in conversations:
                contact = self._find_contact_by_user_id(user)
                conversations[user] = {
                    "user_id": user,
                    "alias": contact.get("alias", user) if contact else user,
                    "total": 0,
                    "unread": 0,
                    "latest": data.get("body", "")[:100],
                    "date": data.get("date", ""),
                }
            conversations[user]["total"] += 1
            if msg_id not in self._read_ids:
                conversations[user]["unread"] += 1
            # Don't overwrite latest — messages are sorted newest-first,
            # so the first entry per user (set in the if-block above) is correct.

        return {"conversations": list(conversations.values())}

    def _handle_read(self, args: dict) -> dict:
        user_id = args.get("user_id")
        limit = args.get("limit", 10)
        if not user_id:
            return {"error": "user_id is required for read"}

        all_msgs = self._load_inbox_messages()
        messages = []
        for data in all_msgs:
            if data.get("from_user_id") != user_id:
                continue
            msg_id = data.get("id", "")
            self._read_ids.add(msg_id)
            messages.append(data)
            if len(messages) >= limit:
                break

        self._save_read()
        return {"messages": messages}

    def _handle_reply(self, args: dict) -> dict:
        message_id = args.get("message_id")
        text = args.get("text", "")
        if not message_id or not text:
            return {"error": "message_id and text are required for reply"}

        # Find the original message to get user_id
        msg_file = self._inbox_dir / message_id / "message.json"
        if not msg_file.is_file():
            return {"error": f"Message not found: {message_id}"}
        data = json.loads(msg_file.read_text(encoding="utf-8"))
        user_id = data.get("from_user_id")
        if not user_id:
            return {"error": "Cannot determine user_id from message"}

        return self._handle_send({"user_id": user_id, "text": text})

    def _handle_search(self, args: dict) -> dict:
        query = args.get("query", "")
        user_id_filter = args.get("user_id")
        if not query:
            return {"error": "query is required for search"}

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        all_msgs = self._load_inbox_messages()
        matches = []
        for data in all_msgs:
            if user_id_filter and data.get("from_user_id") != user_id_filter:
                continue
            body = data.get("body", "")
            if pattern.search(body):
                matches.append(data)
            if len(matches) >= 20:
                break

        return {"matches": matches}

    def _handle_contacts(self) -> dict:
        return {"contacts": self._contacts}

    def _handle_add_contact(self, args: dict) -> dict:
        user_id = args.get("user_id")
        alias = args.get("alias")
        if not user_id or not alias:
            return {"error": "user_id and alias are required"}
        self._contacts[alias] = {
            "user_id": user_id,
            "name": args.get("name", alias),
        }
        self._save_contacts()
        return {"status": "ok", "alias": alias}

    def _handle_remove_contact(self, args: dict) -> dict:
        alias = args.get("alias")
        user_id = args.get("user_id")
        if alias and alias in self._contacts:
            del self._contacts[alias]
        elif user_id:
            self._contacts = {
                k: v for k, v in self._contacts.items()
                if v.get("user_id") != user_id
            }
        else:
            return {"error": "alias or user_id required"}
        self._save_contacts()
        return {"status": "ok"}

    # ── Helpers ─────────────────────────────────────────────────

    def _load_inbox_messages(self) -> list[dict]:
        """Load all inbox messages, sorted by date (newest first). Skips corrupt files."""
        messages = []
        if not self._inbox_dir.is_dir():
            return messages
        for msg_dir in self._inbox_dir.iterdir():
            msg_file = msg_dir / "message.json"
            if not msg_file.is_file():
                continue
            try:
                data = json.loads(msg_file.read_text(encoding="utf-8"))
                messages.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        messages.sort(key=lambda m: m.get("date", ""), reverse=True)
        return messages

    # ── State persistence ──────────────────────────────────────

    def _load_state(self) -> None:
        contacts_file = self._wechat_dir / "contacts.json"
        if contacts_file.is_file():
            self._contacts = json.loads(
                contacts_file.read_text(encoding="utf-8")
            )
        read_file = self._wechat_dir / "read.json"
        if read_file.is_file():
            self._read_ids = set(
                json.loads(read_file.read_text(encoding="utf-8"))
            )
        state_file = self._wechat_dir / "state.json"
        if state_file.is_file():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self._get_updates_buf = state.get("get_updates_buf", "")
            self._context_tokens = state.get("context_tokens", {})

    def _save_state(self) -> None:
        state = {
            "get_updates_buf": self._get_updates_buf,
            "context_tokens": self._context_tokens,
        }
        self._atomic_write(
            self._wechat_dir / "state.json",
            json.dumps(state, ensure_ascii=False, indent=2),
        )

    def _save_contacts(self) -> None:
        self._atomic_write(
            self._wechat_dir / "contacts.json",
            json.dumps(self._contacts, ensure_ascii=False, indent=2),
        )

    def _save_read(self) -> None:
        self._atomic_write(
            self._wechat_dir / "read.json",
            json.dumps(list(self._read_ids), ensure_ascii=False),
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write content to path atomically via tempfile + os.replace."""
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _find_contact_by_user_id(self, user_id: str) -> dict | None:
        for alias, data in self._contacts.items():
            if data.get("user_id") == user_id:
                return {"alias": alias, **data}
        return None

    def _run_async(self, coro):
        """Run an async coroutine from the sync tool handler thread.

        Schedules onto the poll loop's event loop via run_coroutine_threadsafe.
        Raises RuntimeError if the addon has not been started.
        """
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("WeChat addon not started — call start() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most `limit` characters."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
