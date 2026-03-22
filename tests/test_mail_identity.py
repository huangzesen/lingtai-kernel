"""Tests for mail identity card — every sent message carries sender's manifest."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.intrinsics import mail as mail_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(tmp_path: Path, *, agent_name: str | None = None, admin: dict | None = None):
    """Build a minimal mock agent with working dir and manifest."""
    agent = MagicMock()
    agent.agent_id = "aabbccddee01"
    agent.agent_name = agent_name
    agent._working_dir = tmp_path
    agent._config = MagicMock()
    agent._config.language = "en"
    agent._admin = admin or {}
    agent._started_at = "2026-03-21T10:00:00Z"
    agent._mail_service = MagicMock()
    agent._mail_service.address = str(tmp_path)
    agent._mail_arrived = threading.Event()
    agent.inbox = MagicMock()

    def _build_manifest():
        data = {
            "agent_id": agent.agent_id,
            "agent_name": agent.agent_name,
            "started_at": agent._started_at,
            "working_dir": str(agent._working_dir),
            "admin": agent._admin,
            "language": agent._config.language,
            "address": agent._mail_service.address,
        }
        return data

    agent._build_manifest = _build_manifest
    agent._log = MagicMock()
    return agent


def _deliver_message(agent, payload: dict) -> str:
    """Persist a message directly to agent's inbox, return msg_id."""
    return mail_mod._persist_to_inbox(agent, payload)


# ---------------------------------------------------------------------------
# Tests — identity attached on send
# ---------------------------------------------------------------------------

class TestIdentityOnSend:
    """Verify _send attaches sender identity to outgoing mail."""

    def test_send_payload_contains_identity(self, tmp_path):
        """The payload written to outbox should include an identity dict."""
        agent = _make_agent(tmp_path, agent_name="alice", admin={"karma": True, "nirvana": False})

        # Capture the payload written to outbox
        written_payloads = []
        original_persist = mail_mod._persist_to_outbox

        def capture_persist(ag, payload, deliver_at):
            written_payloads.append(payload)
            return "fake-id"

        with patch.object(mail_mod, "_persist_to_outbox", side_effect=capture_persist):
            with patch.object(mail_mod, "threading"):  # don't spawn real threads
                mail_mod._send(agent, {
                    "action": "send",
                    "address": "/other/agent",
                    "message": "hello",
                    "subject": "test",
                })

        assert len(written_payloads) == 1
        payload = written_payloads[0]
        assert "identity" in payload

        identity = payload["identity"]
        assert identity["agent_id"] == "aabbccddee01"
        assert identity["agent_name"] == "alice"
        assert identity["admin"] == {"karma": True, "nirvana": False}
        assert identity["language"] == "en"
        assert identity["address"] == str(tmp_path)

    def test_send_identity_no_name(self, tmp_path):
        """Identity works when agent_name is None."""
        agent = _make_agent(tmp_path, agent_name=None, admin={})

        written_payloads = []

        def capture_persist(ag, payload, deliver_at):
            written_payloads.append(payload)
            return "fake-id"

        with patch.object(mail_mod, "_persist_to_outbox", side_effect=capture_persist):
            with patch.object(mail_mod, "threading"):
                mail_mod._send(agent, {
                    "action": "send",
                    "address": "/other/agent",
                    "message": "hi",
                })

        identity = written_payloads[0]["identity"]
        assert identity["agent_name"] is None
        assert identity["agent_id"] == "aabbccddee01"


# ---------------------------------------------------------------------------
# Tests — identity surfaced in check
# ---------------------------------------------------------------------------

class TestIdentityInCheck:
    """Verify _check shows agent_name from identity in summaries."""

    def test_check_shows_agent_name(self, tmp_path):
        """When identity has agent_name, check shows 'name (address)' format."""
        agent = _make_agent(tmp_path)
        payload = {
            "from": "/sender/path",
            "subject": "hello",
            "message": "body",
            "identity": {
                "agent_id": "sender123456",
                "agent_name": "bob",
                "admin": {"karma": False, "nirvana": False},
            },
        }
        _deliver_message(agent, payload)

        result = mail_mod._check(agent, {})
        assert result["total"] == 1
        msg = result["messages"][0]
        assert msg["from"] == "bob (/sender/path)"

    def test_check_no_identity(self, tmp_path):
        """Messages without identity show plain from address."""
        agent = _make_agent(tmp_path)
        payload = {
            "from": "/sender/path",
            "subject": "old mail",
            "message": "no identity",
        }
        _deliver_message(agent, payload)

        result = mail_mod._check(agent, {})
        msg = result["messages"][0]
        assert msg["from"] == "/sender/path"

    def test_check_identity_no_name(self, tmp_path):
        """Identity with agent_name=None shows plain from address."""
        agent = _make_agent(tmp_path)
        payload = {
            "from": "/sender/path",
            "subject": "nameless",
            "message": "body",
            "identity": {
                "agent_id": "sender123456",
                "agent_name": None,
                "admin": {},
            },
        }
        _deliver_message(agent, payload)

        result = mail_mod._check(agent, {})
        msg = result["messages"][0]
        assert msg["from"] == "/sender/path"


# ---------------------------------------------------------------------------
# Tests — identity surfaced in read
# ---------------------------------------------------------------------------

class TestIdentityInRead:
    """Verify _read returns the full identity dict."""

    def test_read_includes_identity(self, tmp_path):
        """Full read should include the identity card."""
        agent = _make_agent(tmp_path)
        identity = {
            "agent_id": "sender123456",
            "agent_name": "carol",
            "admin": {"karma": True, "nirvana": False},
            "language": "zh",
            "address": "/sender/path",
        }
        payload = {
            "from": "/sender/path",
            "subject": "important",
            "message": "content here",
            "identity": identity,
        }
        msg_id = _deliver_message(agent, payload)

        result = mail_mod._read(agent, {"id": [msg_id]})
        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert msg["identity"] == identity

    def test_read_no_identity_backward_compat(self, tmp_path):
        """Old messages without identity still read fine."""
        agent = _make_agent(tmp_path)
        payload = {
            "from": "/sender/path",
            "subject": "legacy",
            "message": "old format",
        }
        msg_id = _deliver_message(agent, payload)

        result = mail_mod._read(agent, {"id": [msg_id]})
        msg = result["messages"][0]
        assert "identity" not in msg


# ---------------------------------------------------------------------------
# Tests — identity in notification
# ---------------------------------------------------------------------------

class TestIdentityInNotification:
    """Verify _on_normal_mail uses agent_name from identity."""

    def test_notification_shows_agent_name(self, tmp_path):
        """Push notification should show 'name (address)' when identity has agent_name."""
        from lingtai_kernel.base_agent import BaseAgent

        svc = MagicMock()
        svc.address = str(tmp_path)
        agent = BaseAgent.__new__(BaseAgent)
        agent._config = MagicMock()
        agent._config.language = "en"
        agent._mail_arrived = threading.Event()
        agent._mailbox_name = "mail box"
        agent._mailbox_tool = "mail"
        agent.inbox = MagicMock()
        agent._log = MagicMock()

        payload = {
            "_mailbox_id": "test-id-123",
            "from": "/sender/path",
            "subject": "greetings",
            "message": "hello from dave",
            "identity": {
                "agent_id": "dave12345678",
                "agent_name": "dave",
                "admin": {"karma": False, "nirvana": False},
            },
        }

        agent._on_normal_mail(payload)

        # Check the notification was queued
        agent.inbox.put.assert_called_once()
        msg = agent.inbox.put.call_args[0][0]
        assert "dave (/sender/path)" in msg.content

    def test_notification_no_identity(self, tmp_path):
        """Push notification without identity shows plain address."""
        from lingtai_kernel.base_agent import BaseAgent

        agent = BaseAgent.__new__(BaseAgent)
        agent._config = MagicMock()
        agent._config.language = "en"
        agent._mail_arrived = threading.Event()
        agent._mailbox_name = "mail box"
        agent._mailbox_tool = "mail"
        agent.inbox = MagicMock()
        agent._log = MagicMock()

        payload = {
            "_mailbox_id": "test-id-456",
            "from": "/sender/path",
            "subject": "old msg",
            "message": "no identity here",
        }

        agent._on_normal_mail(payload)

        msg = agent.inbox.put.call_args[0][0]
        assert "/sender/path" in msg.content
        assert "None" not in msg.content
