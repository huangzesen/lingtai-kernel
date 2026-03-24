"""Tests for FilesystemMailService — filesystem-based mail delivery."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _make_agent_dir(base: Path, name: str) -> Path:
    """Create a minimal agent working dir with .agent.json and fresh heartbeat."""
    d = base / name
    d.mkdir()
    (d / ".agent.json").write_text(json.dumps({
        "agent_name": "test",
        "admin": {},
    }))
    (d / ".agent.heartbeat").write_text(str(time.time()))
    return d


class TestSend:

    def test_send_creates_message(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello", "subject": "test"})
        assert result is None  # success

        inbox = recip_dir / "mailbox" / "inbox"
        msgs = list(inbox.iterdir())
        assert len(msgs) == 1
        data = json.loads((msgs[0] / "message.json").read_text())
        assert data["message"] == "hello"
        assert data["subject"] == "test"

    def test_send_injects_mailbox_metadata(self, tmp_path):
        """send() must inject _mailbox_id and received_at for mail intrinsic."""
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello"})
        assert result is None

        inbox = recip_dir / "mailbox" / "inbox"
        msg_dir = list(inbox.iterdir())[0]
        data = json.loads((msg_dir / "message.json").read_text())
        assert "_mailbox_id" in data
        assert data["_mailbox_id"] == msg_dir.name  # UUID matches dir name
        assert "received_at" in data
        assert data["received_at"].endswith("Z")  # UTC format

    def test_send_copies_attachments(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        # Create a file to attach
        att = sender_dir / "report.txt"
        att.write_text("data")

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {
            "message": "see attached",
            "attachments": [str(att)],
        })
        assert result is None

        inbox = recip_dir / "mailbox" / "inbox"
        msg_dir = list(inbox.iterdir())[0]
        att_dir = msg_dir / "attachments"
        assert att_dir.exists()
        assert (att_dir / "report.txt").read_text() == "data"

        # The message.json should reference the recipient-local copy
        data = json.loads((msg_dir / "message.json").read_text())
        assert len(data["attachments"]) == 1
        assert "report.txt" in data["attachments"][0]

    def test_send_fails_no_agent_json(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        bad_dir = tmp_path / "noagent"
        bad_dir.mkdir()

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(bad_dir), {"message": "hello"})
        assert result is not None  # error string
        assert "no agent" in result.lower()

    def test_send_fails_stale_heartbeat(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)
        # Write stale heartbeat
        (recip_dir / ".agent.heartbeat").write_text(str(time.time() - 10))

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello"})
        assert result is not None
        assert "not running" in result.lower()

    def test_send_self(self, tmp_path):
        """Send to own address should work (self-send)."""
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        (agent_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        result = svc.send(str(agent_dir), {"message": "note to self"})
        assert result is None

        inbox = agent_dir / "mailbox" / "inbox"
        msgs = list(inbox.iterdir())
        assert len(msgs) == 1
        data = json.loads((msgs[0] / "message.json").read_text())
        assert data["message"] == "note to self"

    def test_send_fails_missing_attachment(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {
            "message": "see attached",
            "attachments": ["/nonexistent/file.txt"],
        })
        assert result is not None
        assert "attachment" in result.lower()

    def test_send_to_human_skips_heartbeat(self, tmp_path):
        """Human recipients (admin=null) don't need a heartbeat."""
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        human_dir = tmp_path / "human01"
        human_dir.mkdir()
        (human_dir / ".agent.json").write_text(json.dumps({
            "agent_name": "human",
            "admin": None,
        }))
        (human_dir / "mailbox" / "inbox").mkdir(parents=True)
        # No .agent.heartbeat file — should still deliver

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(human_dir), {"message": "hello human"})
        assert result is None  # success

        inbox = human_dir / "mailbox" / "inbox"
        entries = list(inbox.iterdir())
        assert len(entries) == 1
        msg = json.loads((entries[0] / "message.json").read_text())
        assert msg["message"] == "hello human"

    def test_send_fails_no_heartbeat_file(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = tmp_path / "recip01"
        recip_dir.mkdir()
        (recip_dir / ".agent.json").write_text(json.dumps({
            "agent_name": "test",
            "admin": {},
        }))
        # No .agent.heartbeat file

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        result = svc.send(str(recip_dir), {"message": "hello"})
        assert result is not None
        assert "not running" in result.lower()

    def test_send_atomic_write(self, tmp_path):
        """Verify no .tmp file is left behind after successful send."""
        from lingtai_kernel.services.mail import FilesystemMailService

        sender_dir = _make_agent_dir(tmp_path, "sender01")
        recip_dir = _make_agent_dir(tmp_path, "recip01")
        (recip_dir / "mailbox" / "inbox").mkdir(parents=True)

        svc = FilesystemMailService(sender_dir, mailbox_rel="mailbox")
        svc.send(str(recip_dir), {"message": "hello"})

        inbox = recip_dir / "mailbox" / "inbox"
        msg_dir = list(inbox.iterdir())[0]
        assert (msg_dir / "message.json").exists()
        assert not (msg_dir / "message.json.tmp").exists()


class TestListen:

    def test_listen_detects_new_message(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        (agent_dir / "mailbox" / "inbox").mkdir(parents=True)

        received = []
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        svc.listen(on_message=lambda p: received.append(p))

        # Simulate incoming mail (another agent writes to our inbox)
        msg_dir = agent_dir / "mailbox" / "inbox" / "test-uuid-1"
        msg_dir.mkdir()
        (msg_dir / "message.json").write_text(json.dumps({
            "from": "/tmp/other",
            "message": "hi",
        }))

        time.sleep(1.0)
        svc.stop()
        assert len(received) == 1
        assert received[0]["message"] == "hi"

    def test_listen_ignores_existing_messages(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        inbox = agent_dir / "mailbox" / "inbox"
        inbox.mkdir(parents=True)

        # Pre-existing message
        old = inbox / "old-uuid"
        old.mkdir()
        (old / "message.json").write_text(json.dumps({"message": "old"}))

        received = []
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        svc.listen(on_message=lambda p: received.append(p))
        time.sleep(1.0)
        svc.stop()
        assert len(received) == 0

    def test_listen_detects_multiple_messages(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        (agent_dir / "mailbox" / "inbox").mkdir(parents=True)

        received = []
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        svc.listen(on_message=lambda p: received.append(p))

        for i in range(3):
            msg_dir = agent_dir / "mailbox" / "inbox" / f"uuid-{i}"
            msg_dir.mkdir()
            (msg_dir / "message.json").write_text(json.dumps({
                "message": f"msg-{i}",
            }))

        time.sleep(1.5)
        svc.stop()
        assert len(received) == 3
        messages = sorted(r["message"] for r in received)
        assert messages == ["msg-0", "msg-1", "msg-2"]

    def test_stop_is_idempotent(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        # stop without listen should not raise
        svc.stop()
        svc.stop()


class TestAddress:

    def test_address_returns_working_dir(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        assert svc.address == str(agent_dir)

    def test_address_is_str(self, tmp_path):
        from lingtai_kernel.services.mail import FilesystemMailService

        agent_dir = _make_agent_dir(tmp_path, "agent01")
        svc = FilesystemMailService(agent_dir, mailbox_rel="mailbox")
        assert isinstance(svc.address, str)
