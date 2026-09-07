"""Manager for the LingTai WhatsApp MCP (personal-account mode).

The manager owns the bridge lifecycle, message persistence, LICC inbound
notification, and the validated tool dispatch. The Meta Cloud API surface
(accounts/webhook/templates) is gone; this module talks to the local Node
bridge (whatsapp-web.js) over the newline-JSON protocol.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from . import client as bridge_client
from . import redaction
from .licc import push_inbox_event


log = logging.getLogger(__name__)

from lingtai.kernel._frontmatter import strip_frontmatter  # noqa: E402
from lingtai.mcp_servers import _config, _identity, _skill  # noqa: E402
from ._family import WHATSAPP_ACTIONS, WHATSAPP_SCHEMA  # noqa: E402
from .plugin import WHATSAPP_PLUGIN  # noqa: E402


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_notification_header_template() -> str:
    text = resources.files(__package__).joinpath("notification_header.md").read_text(encoding="utf-8")
    return strip_frontmatter(text)


# Shared cross-channel preview/responsiveness guidance prepended to every
# inbound LICC notification body, exactly as telegram/feishu/wechat do.
_NOTIFICATION_HEADER_TEMPLATE = _load_notification_header_template()

# The package's plugin descriptor already loaded and validated SKILL.md;
# these names alias its single copy rather than re-reading the file.
_SKILL_NAME = WHATSAPP_PLUGIN.skill_name
_SKILL_FRONTMATTER = WHATSAPP_PLUGIN.skill_frontmatter
_SKILL_BODY = WHATSAPP_PLUGIN.skill_body
_SKILL_PATH = WHATSAPP_PLUGIN.skill_path

SCHEMA = WHATSAPP_SCHEMA

DESCRIPTION = (
    "Personal-account WhatsApp Web messaging through a local whatsapp-web.js "
    "bridge (one linked session; not the Meta Cloud API). Send, reply, and "
    "react perform real external actions; check/read/search inspect bounded "
    "conversation data, while contacts, pairing, status, and logout cover "
    "local account operation. Inbound messages may wake the agent through LICC. "
    "Call action='manual' for message IDs, bridge media, allowlists/settings, "
    "lifecycle, and safety guidance."
)

DEFAULT_STORE = "whatsapp"

# whatsapp-web.js message types that carry no usable text body. Every other
# type — notably ``chat``, the value the library emits for a plain text
# message — is treated as text. The bridge already normalizes ``chat`` to
# ``text``; this set is the Python-side backstop for stored history written
# by an older bridge and for types the bridge does not know about.
_MEDIA_TYPES = frozenset({
    "image", "video", "audio", "ptt", "document", "sticker", "gif",
    "location", "vcard", "multi_vcard", "contact_card", "contact_card_multi",
})

# Bounded FIFO window for the inbound replay guard (inbox_seen.json). Sized
# well above a single bridge redelivery horizon: on reconnect or process
# restart the bridge re-emits the same stable message id, so a redelivery never
# falls outside this window while it could still be re-delivered. Bounds
# memory/disk for the persisted seen-set.
SEEN_KEYS_MAX = 5000

# Filesystem-safety cap for a single path component built from remote input.
_MAX_COMPONENT = 120


def _safe_component(value: Any, fallback: str = "unknown") -> str:
    """Reduce an untrusted id to one safe path component.

    Message ids are chosen by the *sending* client, so ``msg.id._serialized``
    is remote input and must never reach the filesystem verbatim (it can carry
    ``/`` and ``..``). Same treatment for wa_ids.
    """
    text = str(value or "").strip()
    if not text:
        return fallback
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", text)[:_MAX_COMPONENT]
    # ``.``/``..`` survive the character filter but still mean "parent dir".
    if not safe.strip("."):
        return fallback
    return safe


def normalize_wa_id(value: Any) -> str:
    """Canonicalize a wa_id so config and inbound JIDs compare equal.

    ``15551234567`` (the form ``send`` accepts and the form humans write in
    ``allowed_wa_ids``) and ``15551234567@c.us`` (the form the bridge always
    reports) are the same user.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "@" in text:
        return text
    digits = re.sub(r"[^0-9]", "", text)
    return f"{digits}@c.us" if digits else text


def _is_text_type(msg_type: Any) -> bool:
    """True when a message type carries a readable body."""
    if not msg_type:
        return True
    return str(msg_type).lower() not in _MEDIA_TYPES


def _display_text(msg: dict[str, Any]) -> str:
    """Body for text messages, ``[<type>]`` placeholder for media."""
    if _is_text_type(msg.get("type")):
        return msg.get("body") or ""
    return f"[{msg.get('type') or 'media'}]"


class WhatsAppManager:
    """Personal-account WhatsApp manager.

    ``config`` is a dict with optional keys:
      - node_path: Node executable (default: node on PATH)
      - bridge_dir: path to the Node bridge directory (default: bundled)
      - session_dir: path to store the whatsapp-web.js session (default: <workdir>/.wwebjs_auth)
      - store_dir: path to store message/contact metadata (default: <workdir>/whatsapp)
      - allowed_wa_ids: optional allowlist of wa_ids / @c.us ids for inbound push
      - allowed_users: legacy alias for allowed_wa_ids
      - autostart: start the Node bridge on construction (default: true)

    Public ``settings`` is plugin-owned rather than a manager action. Its
    provider reads these startup facts without changing them.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        working_dir: str | Path | None = None,
        *,
        config_path: str | Path | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.config_path = Path(config_path) if config_path is not None else None
        self.working_dir = Path(working_dir or os.environ.get("LINGTAI_AGENT_DIR", os.getcwd()))
        self.store_dir = Path(self.config.get("store_dir") or self.working_dir / DEFAULT_STORE)
        self.session_dir = Path(self.config.get("session_dir") or self.working_dir / ".wwebjs_auth")
        self.autostart = bool(self.config.get("autostart", True))
        # Both sides of the allow-list go through one normalizer so bare
        # digits in config match the ``@c.us`` JIDs the bridge reports. The
        # issue's name is canonical; retain ``allowed_users`` as a compatibility
        # alias for configs written before this option was documented.
        raw_allowed = (
            self.config.get("allowed_wa_ids")
            if "allowed_wa_ids" in self.config
            else self.config.get("allowed_users")
        )
        self.allowed_wa_ids: set[str] | None = (
            {
                normalize_wa_id(value)
                for value in raw_allowed
                if normalize_wa_id(value)
            }
            if raw_allowed
            else None
        )
        self.allowed_users = self.allowed_wa_ids
        self.contacts_path = self.store_dir / "contacts.json"
        self._contacts: dict[str, dict[str, Any]] = {}
        self._load_contacts()
        # Inbound replay guard state (see _stable_key/_is_replay/_record_seen).
        # The bridge re-emits a message with the same stable id on reconnect or
        # restart; persisted to store_dir/inbox_seen.json so the guard survives
        # restarts. Personal mode has a single account, so keys are namespaced
        # by sender.
        self._seen_keys: dict[str, str] = {}
        self._seen_order: list[str] = []
        self._seen_lock = threading.Lock()
        self._load_seen()
        # Public, non-secret identity facts for the shared MCP identity file.
        self._me: str | None = None
        self._last_verified_at: str | None = None
        self.bridge = bridge_client.WhatsAppBridge(
            node_path=self.config.get("node_path"),
            bridge_dir=self.config.get("bridge_dir"),
            session_dir=self.session_dir,
            on_event=self._on_bridge_event,
        )
        # Inbound push must not depend on the agent happening to call an
        # unrelated action first: start the listener eagerly. A missing Node /
        # bridge install is a degraded state, not a construction failure — the
        # error resurfaces on the first action that needs the bridge.
        if self.autostart:
            try:
                self.bridge.start()
            except Exception as e:
                log.warning("whatsapp bridge autostart failed (%s); will retry on first action", e)

    # ------------------------------------------------------------------ helpers

    def _load_contacts(self) -> None:
        if self.contacts_path.is_file():
            try:
                loaded = json.loads(self.contacts_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            # A list-shaped contacts.json would blow up later on .pop/index.
            self._contacts = loaded if isinstance(loaded, dict) else {}

    def _save_contacts(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.contacts_path.write_text(json.dumps(self._contacts, ensure_ascii=False, indent=2), encoding="utf-8")

    def _conversation_dir(self, wa_id: str) -> Path:
        return self.store_dir / _safe_component(wa_id)

    def _message_path(self, wa_id: str, direction: str, msg_id: str) -> Path:
        # Every component is remote-influenced; sanitize all of them.
        return (
            self._conversation_dir(wa_id)
            / _safe_component(direction, "inbox")
            / f"{_safe_component(msg_id)}.json"
        )

    def _store_message(self, wa_id: str, direction: str, msg: dict[str, Any]) -> str:
        msg_id = msg.get("id") or uuid.uuid4().hex
        path = self._message_path(wa_id, direction, msg_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = dict(msg)
        payload["id"] = msg_id
        payload["direction"] = direction
        payload["stored_at"] = time.time()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            # The archive is plaintext personal correspondence.
            path.chmod(0o600)
        except OSError:
            pass
        return msg_id

    @staticmethod
    def _sort_key(entry: tuple[Path, dict[str, Any]]) -> tuple[float, str]:
        """Order by the message's own timestamp, file name as tiebreaker.

        mtime is wrong twice over: re-writing a file (the dedup mechanism)
        would jump an old message to the end of history, and mtime ties would
        silently place every inbox message before every sent one.
        """
        path, msg = entry
        raw = msg.get("timestamp")
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            ts = float(msg.get("stored_at") or 0.0)
        return (ts, path.name)

    def _read_dir(self, base: Path, direction: str | None) -> list[tuple[Path, dict[str, Any]]]:
        directions = (direction,) if direction else ("inbox", "sent")
        out: list[tuple[Path, dict[str, Any]]] = []
        for d in directions:
            sub = base / _safe_component(d, "inbox")
            if not sub.is_dir():
                continue
            for p in sub.glob("*.json"):
                try:
                    loaded = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(loaded, dict):
                    out.append((p, loaded))
        return out

    def _iter_messages(self, wa_id: str, direction: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if not wa_id:
            # A conversation scan needs a conversation; the store is laid out
            # as store_dir/<wa_id>/{inbox,sent}/. Use _iter_all_messages for a
            # cross-conversation scan.
            return []
        base = self._conversation_dir(wa_id)
        if not base.is_dir():
            return []
        entries = sorted(self._read_dir(base, direction), key=self._sort_key)
        return [msg for _path, msg in entries[-limit:]]

    def _iter_all_messages(self, direction: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Scan every conversation directory under the store."""
        if not self.store_dir.is_dir():
            return []
        entries: list[tuple[Path, dict[str, Any]]] = []
        for conversation in self.store_dir.iterdir():
            if conversation.is_dir():
                entries += self._read_dir(conversation, direction)
        entries.sort(key=self._sort_key)
        return [msg for _path, msg in entries[-limit:]]

    def _conversation_context(self, wa_id: str, latest: dict[str, Any]) -> dict[str, Any]:
        history = self._iter_messages(wa_id, limit=10)
        recent = []
        for m in history:
            recent.append({
                "id": m.get("id"),
                "fromMe": m.get("direction") == "sent",
                "text": _display_text(m)[:500],
                "timestamp": m.get("timestamp"),
            })
        latest_text = _display_text(latest)
        return {
            "platform": "whatsapp",
            "conversation_ref": f"whatsapp:{wa_id}",
            "recent_messages": recent[-10:],
            "latest_incoming": {
                "id": latest.get("id"),
                "from": latest.get("from"),
                "text": (latest_text or "")[:500],
                "timestamp": latest.get("timestamp"),
                "type": latest.get("type"),
            },
        }

    # -------------------------------------------------------- inbound replay guard

    @staticmethod
    def _stable_key(from_id: str, msg: dict[str, Any]) -> str | None:
        """Derive a stable, replay-resistant signature for an inbound message.

        whatsapp-web.js assigns a globally stable wamid to every message
        (``msg.id._serialized``) and repeats it verbatim on redelivery, so it
        is the primary key (the bridge normalizes it into ``msg["id"]``). The
        key is namespaced by sender so a collision across senders cannot
        suppress a real message. Returns ``None`` when the message carries no
        stable upstream id — the caller must land it unconditionally rather
        than key on a local ``uuid4`` placeholder.
        """
        mid = msg.get("id")
        if mid:
            return f"{from_id}|mid:{mid}"
        return None

    def _is_replay(self, key: str) -> bool:
        """True if this stable key was already landed (replay guard hit)."""
        with self._seen_lock:
            return key in self._seen_keys

    def _record_seen(self, key: str, local_id: str) -> None:
        """Persist that ``key`` was landed under ``local_id`` (atomic)."""
        with self._seen_lock:
            if key in self._seen_keys:
                return
            self._seen_keys[key] = local_id
            self._seen_order.append(key)
            # Evict oldest beyond the window to bound the state file.
            while len(self._seen_order) > SEEN_KEYS_MAX:
                evicted = self._seen_order.pop(0)
                self._seen_keys.pop(evicted, None)
        self._save_seen()

    def _save_seen(self) -> None:
        with self._seen_lock:
            payload = {
                "version": 1,
                "order": list(self._seen_order),
                "keys": dict(self._seen_keys),
            }
        self.store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.store_dir / "inbox_seen.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.store_dir / "inbox_seen.json")

    def _load_seen(self) -> None:
        """Load the persisted replay guard; corrupt/missing state degrades to empty."""
        seen_file = self.store_dir / "inbox_seen.json"
        if not seen_file.is_file():
            return
        try:
            seen = json.loads(seen_file.read_text(encoding="utf-8"))
            self._seen_keys = dict(seen.get("keys", {}))
            self._seen_order = [
                k for k in seen.get("order", []) if k in self._seen_keys
            ]
        except (ValueError, AttributeError, OSError) as e:
            # Corrupt index degrades to "no guard", never crashes boot.
            log.warning("Failed to load inbox_seen.json (ignoring): %s", e)
            self._seen_keys = {}
            self._seen_order = []

    # ------------------------------------------------------------------ bridge events

    def _on_bridge_event(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            log.debug("whatsapp bridge: dropped non-dict event frame")
            return
        etype = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            # A malformed payload is a clean drop, not a logged traceback.
            if data is not None:
                log.debug("whatsapp bridge: dropped %s event with non-object data", etype)
            data = {}
        if etype == "message":
            self._handle_incoming(data)
        elif etype == "qr":
            log.info("whatsapp bridge: QR available (type=%s)", "data-url" if data.get("qr_base64") else "ascii")
        elif etype == "ready":
            self._me = data.get("me")
            self._last_verified_at = _utcnow()
            log.info("whatsapp bridge: ready me=%s", data.get("me"))
        elif etype == "disconnected":
            log.warning("whatsapp bridge disconnected: %s", data.get("reason"))
        elif etype == "error":
            log.error("whatsapp bridge error: %s", data.get("error"))
        else:
            log.debug("whatsapp bridge event: %s", etype)

    def _is_allowed_sender(self, wa_id: Any) -> bool:
        """Return whether an inbound sender may reach the agent.

        ``None`` means the option was omitted or empty, preserving the
        historical allow-all behavior. When configured, compare normalized
        values before any message is stored or pushed to the inbox.
        """
        if self.allowed_wa_ids is None:
            return True
        return normalize_wa_id(wa_id) in self.allowed_wa_ids

    def _handle_incoming(self, msg: dict[str, Any]) -> None:
        if not isinstance(msg, dict):
            return
        from_id = msg.get("from")
        if not from_id:
            return
        if not self._is_allowed_sender(from_id):
            log.info("whatsapp: ignored inbound from non-allowed %s", from_id)
            return
        # Replay guard. Bridge redelivery is at-least-once: on reconnect or
        # restart the same message (same stable id) is re-emitted, and without
        # this guard every redelivery would wake the agent again. Messages
        # without a stable upstream id are landed unconditionally.
        stable_key = self._stable_key(from_id, msg)
        if stable_key is not None and self._is_replay(stable_key):
            log.debug("whatsapp: suppressed replayed inbound %s", stable_key)
            return
        if stable_key is not None:
            # Provenance for the replay guard, stored with the message.
            msg = dict(msg)
            msg["stable_key"] = stable_key
        msg_id = self._store_message(from_id, "inbox", msg)
        if stable_key is not None:
            # Recorded only after the write landed: a failed store must be
            # retryable, not silently suppressed as a "replay".
            self._record_seen(stable_key, msg_id)
        ctx = self._conversation_context(from_id, msg)
        header = _NOTIFICATION_HEADER_TEMPLATE.format(channel="WhatsApp").rstrip("\n")
        newest = (_display_text(msg) or f"[{msg.get('type')}]")[:2000]
        push_inbox_event(
            sender="whatsapp",
            subject=f"whatsapp message from {from_id}",
            body=f"{header}\n\n**Newest WhatsApp message**\n{newest}",
            # Downstream LICC code may use the event id as a file name, so the
            # remote-controlled components are sanitized here too.
            event_id=f"wa:{_safe_component(from_id)}:{_safe_component(msg_id)}",
            metadata={
                "conversation_ref": ctx["conversation_ref"],
                "message_id": msg_id,
                "from": from_id,
                "timestamp": msg.get("timestamp"),
                "type": msg.get("type"),
                "recent_messages": ctx["recent_messages"],
                "latest_incoming": ctx["latest_incoming"],
            },
        )

    # ------------------------------------------------------------------ identity

    def allowed_wa_ids_count(self) -> int | None:
        """Return the configured sender count without exposing identifiers."""
        if self.allowed_wa_ids is None:
            return None
        return len(self.allowed_wa_ids)

    def account_details(self) -> list[dict[str, Any]]:
        """Non-secret public identity details for the paired personal account.

        Personal mode has exactly one "account" — the linked device session —
        so this is a one-element list. No credential-bearing field exists in
        this mode (the session lives in ``session_dir``, never in the payload).
        """
        item: dict[str, Any] = {
            "alias": "personal",
            "backend": "personal_account_whatsapp_web",
            "wa_id": self._me,
            "contact_count": len(self._contacts),
            "allowed_wa_ids_count": self.allowed_wa_ids_count(),
            "last_verified_at": self._last_verified_at,
        }
        return [{k: v for k, v in item.items() if v is not None}]

    def identity_payload(self) -> dict[str, Any]:
        """Build the non-secret MCP identity document for this service."""
        return _identity.identity_payload(
            "whatsapp", self.account_details(), generated_at=_utcnow()
        )

    def identity_path(self) -> Path:
        return _identity.identity_path(self.working_dir, "whatsapp")

    def write_identity_file(self) -> Path:
        """Atomically write public, non-secret MCP identity metadata."""
        return _identity.write_identity_file(
            self.identity_path(), self.identity_payload()
        )

    # ------------------------------------------------------------------ actions

    def handle(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Legacy manager entrypoint used by ``_family.build_whatsapp_family``.

        The family passes the flattened ``{"action": ..., **input}`` shape;
        the LTP-v2 envelope has already been validated by ``handle_whatsapp``.
        """
        raw = dict(args or {})
        name = raw.pop("action", None)
        if not isinstance(name, str):
            raise ValueError("whatsapp action is required")
        return self.action(name, raw)

    def action(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if name not in WHATSAPP_ACTIONS:
            raise ValueError(f"unknown whatsapp action: {name}")
        method = getattr(self, f"_{name}", None)
        if method is None:
            raise ValueError(f"unknown whatsapp action: {name}")
        return method(args or {})

    def _manual(self, args: dict[str, Any]) -> dict[str, Any]:
        return _skill.manual_payload(
            _SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH, _SKILL_NAME
        )

    def _status(self, args: dict[str, Any]) -> dict[str, Any]:
        alive = self.bridge.alive
        status = {"bridge_alive": alive, "session_dir": str(self.session_dir)}
        if alive:
            try:
                # Short deadline: status is also served as an MCP resource.
                st = self.bridge.request("status", timeout=10)
                status.update({"ready": st.get("ready", False), "me": st.get("me"), "qr_available": st.get("qr_available", False)})
            except Exception as e:
                status["error"] = str(e)
        else:
            status["ready"] = False
        allowed_count = self.allowed_wa_ids_count()
        if allowed_count is not None:
            status["allowed_wa_ids_count"] = allowed_count
        return redaction.safe_status(status)

    def _get_qr(self, args: dict[str, Any]) -> dict[str, Any]:
        self.bridge.start()
        try:
            st = self.bridge.request("status")
            if st.get("ready"):
                return {"ready": True, "me": st.get("me")}
        except Exception:
            pass
        try:
            result = self.bridge.request("get_qr")
        except Exception as e:
            return {"ready": False, "error": str(e), "hint": "If no QR yet, wait for the bridge to emit a qr event, then retry."}
        return {"ready": False, "qr_base64": result.get("qr_base64")}

    def _send(self, args: dict[str, Any]) -> dict[str, Any]:
        to = args.get("to") or args.get("wa_id")
        if not to:
            raise ValueError("send requires to or wa_id")
        result = self.bridge.request("send", {"to": to, "text": args.get("text"), "media": args.get("media")})
        media = args.get("media") if isinstance(args.get("media"), dict) else None
        sent_type = "text" if args.get("text") else ((media or {}).get("type") or "media")
        msg = {"id": result.get("id"), "to": result.get("wa_id"), "body": args.get("text"), "type": sent_type, "timestamp": time.time(), "fromMe": True}
        self._store_message(result.get("wa_id") or to, "sent", msg)
        return result

    def _check(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 10)
        result = self.bridge.request("read", {"limit": limit})
        return result

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        wa_id = args.get("wa_id") or args.get("to")
        if wa_id:
            # Normalize so the documented bare-digits wa_id (the same form the
            # caller just used for send) finds the JID-keyed store directory.
            msgs = self._iter_messages(normalize_wa_id(wa_id), limit=int(args.get("limit") or 50))
            return {"messages": [{"id": m.get("id"), "fromMe": m.get("direction") == "sent", "body": m.get("body"), "type": m.get("type"), "timestamp": m.get("timestamp")} for m in msgs]}
        limit = int(args.get("limit") or 20)
        return self.bridge.request("read", {"limit": limit})

    def _reply(self, args: dict[str, Any]) -> dict[str, Any]:
        message_id = args.get("message_id")
        to = args.get("to") or args.get("wa_id")
        text = args.get("text")
        if not message_id or not text:
            raise ValueError("reply requires message_id and text")
        if not to:
            # Recover the conversation by scanning every stored conversation
            # directory (store_dir/<wa_id>/{inbox,sent}/) for the quoted id.
            for msg in reversed(self._iter_all_messages(limit=500)):
                if msg.get("id") == message_id:
                    to = msg.get("from") or msg.get("to")
                    break
        if not to:
            raise ValueError("reply requires to or wa_id (message not found in store)")
        result = self.bridge.request("reply", {"to": to, "message_id": message_id, "text": text})
        self._store_message(to, "sent", {"id": result.get("id"), "to": to, "body": text, "type": "text", "timestamp": time.time(), "fromMe": True})
        return result

    def _react(self, args: dict[str, Any]) -> dict[str, Any]:
        message_id = args.get("message_id")
        emoji = args.get("emoji")
        if not message_id or not emoji:
            raise ValueError("react requires message_id and emoji")
        return self.bridge.request("react", {"message_id": message_id, "emoji": emoji})

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        if not query:
            raise ValueError("search requires query")
        limit = int(args.get("limit") or 20)
        return self.bridge.request("search", {"query": query, "limit": limit})

    def _contacts(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self.bridge.request("contacts")
        contacts = result.get("contacts") or []
        self._contacts = {c.get("id"): c for c in contacts if c.get("id")}
        self._save_contacts()
        return {"contacts": contacts[: int(args.get("limit") or 500)]}

    def _add_contact(self, args: dict[str, Any]) -> dict[str, Any]:
        wa_id = args.get("wa_id") or args.get("to")
        name = args.get("name")
        if not wa_id:
            raise ValueError("add_contact requires wa_id")
        self._contacts[wa_id] = {"id": wa_id, "name": name}
        self._save_contacts()
        return {"ok": True, "wa_id": wa_id}

    def _remove_contact(self, args: dict[str, Any]) -> dict[str, Any]:
        wa_id = args.get("wa_id") or args.get("to")
        if not wa_id:
            raise ValueError("remove_contact requires wa_id")
        self._contacts.pop(wa_id, None)
        self._save_contacts()
        return {"ok": True, "wa_id": wa_id}

    def _logout(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.bridge.alive:
            try:
                self.bridge.request("logout", timeout=10)
            except Exception:
                pass
        # An unauthenticated bridge (and its Chromium) has nothing left to do;
        # the next action restarts it.
        self.bridge.stop()
        return {"ok": True}

    def close(self) -> None:
        self.bridge.stop()


# --------------------------------------------------------------------------- config

def load_config() -> tuple[dict[str, Any], Path]:
    """Load LINGTAI_WHATSAPP_CONFIG (JSON file) into ``(config, path)``.

    Shares the curated-MCP loader contract: a missing env var is a ``ValueError``
    and a bad path a ``FileNotFoundError``, both with the shared message shape.
    Personal mode works without a config file, so ``build_manager`` treats the
    missing-env case as "use defaults" rather than a boot failure.
    """
    return _config.load_config_file(
        "LINGTAI_WHATSAPP_CONFIG",
        label="WhatsApp",
        missing_env_msg="LINGTAI_WHATSAPP_CONFIG env var not set",
    )
