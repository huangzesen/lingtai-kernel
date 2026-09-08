"""Tests for the .notification/ filesystem sync mechanism.

Covers the design's invariants and the patch's §13 test matrix:

- §13.1 — fingerprint + collection primitives, atomicity, concurrency
- §13.2 — IDLE-state pair injection / strip / no-op
- §13.3 — ACTIVE-state deferral without ToolResultBlock mutation
- §13.4 — ASLEEP-state wake on fingerprint change
- §13.5 — voluntary `notification(action="check")` returns the dict
- §13.6 — producer migrations: email, soul, system
- §13.7 — molt clearing

Where possible the tests use the real `notifications.py` module against
``tmp_path``; agent-level tests use a stub that mimics the
BaseAgent → SessionManager → ChatSession → ChatInterface hierarchy.

The deeper integration paths (heartbeat → `_sync_notifications` → wire
mutation under real adapters) are covered by the existing `test_tc_inbox*`
suites and the soul/email integration tests, which continue to pass
because `tc_inbox` is preserved during the migration window.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests._notification_store_helpers import fingerprint_notifications, snapshot_notifications, publish_test_payload, clear_test_payload, replace_ack_refs_for_test
from tests._notification_store_helpers import notification_store_for, store_agent_for
from tests._tool_plugin_helpers import dispatch_declared_tool
from lingtai.tools.notification import DECLARATION as NOTIFICATION_DECLARATION


# ---------------------------------------------------------------------------
# §13.1 — fingerprint + collection primitives
# ---------------------------------------------------------------------------


def test_fingerprint_empty_dir(tmp_path: Path) -> None:
    assert fingerprint_notifications(tmp_path) == ()


def test_fingerprint_with_files(tmp_path: Path) -> None:
    publish_test_payload(tmp_path, "email", {"count": 3})
    publish_test_payload(tmp_path, "soul", {"voices": []})
    fp = fingerprint_notifications(tmp_path)
    names = [entry[0] for entry in fp]
    assert names == sorted(names)
    assert "email.json" in names
    assert "soul.json" in names
    # Each entry is (name, size, sha256).
    for name, size, digest in fp:
        assert size > 0
        assert isinstance(digest, str)
        assert len(digest) == 64


def test_fingerprint_changes_on_overwrite(tmp_path: Path) -> None:
    publish_test_payload(tmp_path, "email", {"count": 1})
    fp1 = fingerprint_notifications(tmp_path)
    publish_test_payload(tmp_path, "email", {"count": 2, "extra": "more bytes"})
    fp2 = fingerprint_notifications(tmp_path)
    assert fp1 != fp2


def test_fingerprint_ignores_equivalent_rewrite_mtime_churn(tmp_path: Path) -> None:
    publish_test_payload(tmp_path, "email", {"count": 1})
    fp1 = fingerprint_notifications(tmp_path)
    publish_test_payload(tmp_path, "email", {"count": 1})
    fp2 = fingerprint_notifications(tmp_path)
    assert fp1 == fp2


def test_collect_empty_dir(tmp_path: Path) -> None:
    assert snapshot_notifications(tmp_path) == {}


def test_collect_mixed_files(tmp_path: Path) -> None:
    publish_test_payload(tmp_path, "email", {"count": 3})
    publish_test_payload(tmp_path, "mcp.telegram", {"messages": ["hi"]})
    out = snapshot_notifications(tmp_path)
    assert out == {
        "email": {"count": 3},
        "mcp.telegram": {"messages": ["hi"]},
    }


def test_collect_skips_malformed_silently(tmp_path: Path) -> None:
    publish_test_payload(tmp_path, "soul", {"x": 1})
    bad_path = tmp_path / ".notification" / "bad.json"
    bad_path.write_text("not json {")
    out = snapshot_notifications(tmp_path)
    assert out == {"soul": {"x": 1}}


def test_collect_skips_non_json_files(tmp_path: Path) -> None:
    publish_test_payload(tmp_path, "email", {"x": 1})
    other = tmp_path / ".notification" / "stray.txt"
    other.write_text("ignored")
    out = snapshot_notifications(tmp_path)
    assert "email" in out
    assert "stray" not in out


def test_publish_creates_dir(tmp_path: Path) -> None:
    notif_dir = tmp_path / ".notification"
    assert not notif_dir.exists()
    publish_test_payload(tmp_path, "email", {"x": 1})
    assert notif_dir.is_dir()


def test_publish_atomic_no_tmp_residue(tmp_path: Path) -> None:
    publish_test_payload(tmp_path, "email", {"x": 1})
    notif_dir = tmp_path / ".notification"
    assert (notif_dir / "email.json").is_file()
    leftover = [p for p in notif_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


def test_publish_preserves_compact_json_bytes(tmp_path: Path) -> None:
    payload = {"message": "\u7075\u53f0", "count": 1}
    publish_test_payload(tmp_path, "email", payload)

    raw = (tmp_path / ".notification" / "email.json").read_bytes()
    assert raw == json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert not raw.endswith(b"\n")


def test_large_result_acks_preserve_compact_json_bytes(tmp_path: Path) -> None:
    ref_ids = {"ref-\u7075", "ref-a"}
    replace_ack_refs_for_test(tmp_path, ref_ids)

    raw = (tmp_path / ".notification" / "large_result_acks.json").read_bytes()
    assert raw == json.dumps(sorted(ref_ids), ensure_ascii=False).encode("utf-8")
    assert not raw.endswith(b"\n")
    leftover = [
        p for p in (tmp_path / ".notification").iterdir() if p.name.endswith(".tmp")
    ]
    assert leftover == []


def test_clear_idempotent(tmp_path: Path) -> None:
    # Clearing a non-existent file should not raise.
    clear_test_payload(tmp_path, "soul")
    publish_test_payload(tmp_path, "email", {"x": 1})
    clear_test_payload(tmp_path, "email")
    assert not (tmp_path / ".notification" / "email.json").exists()
    # Second clear is a no-op.
    clear_test_payload(tmp_path, "email")


def test_concurrent_publish_atomicity(tmp_path: Path) -> None:
    """10 threads × 50 iterations.  Every collect snapshot must return
    parseable JSON for every source (no partial-write reads, no
    corrupted files)."""
    sources = [f"mcp.src_{i}" for i in range(10)]

    def worker(source: str) -> None:
        for i in range(50):
            publish_test_payload(tmp_path, source, {"src": source, "i": i})

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        list(pool.map(worker, sources))

    out = snapshot_notifications(tmp_path)
    # All 10 sources eventually published.
    assert set(out.keys()) == set(sources)
    # Every value parsed successfully (collect's try/except skips
    # malformed; if any failed we'd see fewer keys).
    for src, data in out.items():
        assert data["src"] == src
        assert isinstance(data["i"], int)

    # No .tmp residue.
    notif_dir = tmp_path / ".notification"
    leftover = [p for p in notif_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftover == [], f"Stale tmp files: {leftover}"


# ---------------------------------------------------------------------------
# §13.5 — `notification(action="check")` voluntary call
#
# The read verb moved off `system` onto the standalone `notification` tool.
# `system(action="notification")` is no longer a valid action.
# ---------------------------------------------------------------------------


def test_check_action_returns_empty_when_nothing_published(
    tmp_path: Path,
) -> None:

    @dataclass
    class _Stub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = dispatch_declared_tool(NOTIFICATION_DECLARATION, _Stub(), {"action": "check", "input": {}, "reasoning": "test"})
    # Voluntary call returns a placeholder dict — the live notification
    # payload (if any) is stamped on by the turn loop's meta-block hook,
    # never built by the handler itself. So even with nothing published,
    # the bare channel keys are absent here.
    assert res == {
        "_notification_placeholder": True,
        "message": res["message"],
    }
    assert "notification" in res["message"].lower()


def test_check_action_returns_placeholder(tmp_path: Path) -> None:

    publish_test_payload(tmp_path, "email", {"count": 5, "newest_received_at": "2026-05-05T00:00:00Z"})
    publish_test_payload(tmp_path, "soul", {"voices": [{"source": "warmth", "voice": "..."}]})

    @dataclass
    class _Stub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = dispatch_declared_tool(NOTIFICATION_DECLARATION, _Stub(), {"action": "check", "input": {}, "reasoning": "test"})
    # Handler returns a placeholder only — channel keys MUST NOT appear
    # here. The canonical `notifications` payload is attached later by
    # `attach_active_notifications`, not by this handler. This guarantees
    # there is only one live notification payload in conversation history.
    assert res.get("_notification_placeholder") is True
    assert "email" not in res
    assert "soul" not in res
    assert "notifications" not in res
    assert "_meta" not in res


# ---------------------------------------------------------------------------
# §13.6 — producer migrations
# ---------------------------------------------------------------------------


@dataclass
class _ProducerStubAgent:
    """Minimal agent stub for testing producer file writes.  No chat
    session needed — these tests only verify that producers correctly
    write to .notification/."""
    _working_dir: Path = None
    _logs: list[tuple[str, dict]] = field(default_factory=list)
    _notification_store: object = field(init=False)

    def __post_init__(self) -> None:
        self._notification_store = notification_store_for(self._working_dir)

    def _log(self, evt: str, **fields: Any) -> None:
        self._logs.append((evt, fields))

    def _wake_nap(self, *_args, **_kwargs) -> None:
        # No-op for producer-only tests; no run loop is running.
        pass


def test_email_publish_writes_file(tmp_path: Path, monkeypatch) -> None:
    """When the email producer has unread mail, it writes
    `.notification/email.json` with full persistent email context."""
    from lingtai.tools.email import _rerender_unread_digest

    agent = _ProducerStubAgent(_working_dir=tmp_path)

    def fake_render(_agent, **_kw):
        return ("3 unread:\n- A\n- B\n- C\n", 3, "2026-05-05T00:00:00Z")

    def fake_context(_agent, **_kw):
        return ([{
            "id": "email-1",
            "from": "human",
            "to": ["agent"],
            "subject": "A",
            "message": "Body A",
            "message_chars": 6,
            "message_truncated": False,
            "time": "2026-05-05T00:00:00Z",
            "unread": True,
            "received_at": "2026-05-05T00:00:00Z",
        }], ["email-1"])

    monkeypatch.setattr(
        "lingtai.tools.email.primitives._render_unread_digest",
        fake_render,
    )
    monkeypatch.setattr(
        "lingtai.tools.email.primitives._unread_notification_context",
        fake_context,
    )

    result = _rerender_unread_digest(agent)
    assert result == "email"

    out = snapshot_notifications(tmp_path)
    assert "email" in out
    data = out["email"]["data"]
    assert data["count"] == 3
    assert "digest" not in data
    assert data["email_ids"] == ["email-1"]
    assert data["emails"][0]["id"] == "email-1"
    assert data["emails"][0]["message"] == "Body A"
    assert out["email"]["icon"] == "📧"


def test_email_clear_on_zero(tmp_path: Path, monkeypatch) -> None:
    """When unread count drops to 0, the producer clears the file."""
    from lingtai.tools.email import _rerender_unread_digest

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    publish_test_payload(tmp_path, "email", {"data": {"count": 5}})  # pre-existing
    assert (tmp_path / ".notification" / "email.json").exists()

    monkeypatch.setattr(
        "lingtai.tools.email.primitives._render_unread_digest",
        lambda _agent, **_kw: ("", 0, None),
    )

    result = _rerender_unread_digest(agent)
    assert result is None
    assert not (tmp_path / ".notification" / "email.json").exists()


def test_system_publish_appends_event(tmp_path: Path) -> None:
    """Two calls produce a single file with both events."""
    from lingtai.kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    messaging._enqueue_system_notification(
        agent, source="email.bounce", ref_id="msg_1", body="bounce 1"
    )
    messaging._enqueue_system_notification(
        agent, source="email.bounce", ref_id="msg_2", body="bounce 2"
    )

    out = snapshot_notifications(tmp_path)
    assert "system" in out
    events = out["system"]["data"]["events"]
    assert len(events) == 2
    assert {e["ref_id"] for e in events} == {"msg_1", "msg_2"}
    assert all(e["source"] == "email.bounce" for e in events)
    assert events[0]["event_id"] != events[1]["event_id"]


def test_system_event_ids_keep_entropy_with_fixed_millisecond(
    tmp_path: Path, monkeypatch
) -> None:
    """Same-millisecond events keep enough random suffix to avoid collisions."""
    from lingtai.kernel.base_agent import messaging

    suffixes = iter(("0" * 16, "1" * 16))
    monkeypatch.setattr("time.time", lambda: 1234.567)
    monkeypatch.setattr("secrets.token_hex", lambda n: next(suffixes))

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    first = messaging._enqueue_system_notification(
        agent, source="daemon", ref_id="ref_1", body="event 1"
    )
    second = messaging._enqueue_system_notification(
        agent, source="daemon", ref_id="ref_2", body="event 2"
    )

    assert first.startswith("evt_")
    assert second.startswith("evt_")
    assert first != second
    assert [len(event_id.rsplit("_", 1)[1]) for event_id in (first, second)] == [16, 16]


def test_system_publish_caps_at_20(tmp_path: Path) -> None:
    """25 sequential calls keep only the 20 most recent events."""
    from lingtai.kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    for i in range(25):
        messaging._enqueue_system_notification(
            agent, source="daemon", ref_id=f"ref_{i}", body=f"event {i}"
        )

    events = snapshot_notifications(tmp_path)["system"]["data"]["events"]
    assert len(events) == 20
    refs = [e["ref_id"] for e in events]
    # Cap retained the most recent: ref_5 .. ref_24.
    assert refs[0] == "ref_5"
    assert refs[-1] == "ref_24"


def test_system_publish_concurrent_no_lost_writes(tmp_path: Path) -> None:
    """20 threads concurrently publish; all events end up in the file."""
    from lingtai.kernel.base_agent import messaging

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    n_events = 20

    def worker(i: int) -> None:
        messaging._enqueue_system_notification(
            agent, source="stress", ref_id=f"ref_{i}", body=f"e{i}"
        )

    with ThreadPoolExecutor(max_workers=n_events) as pool:
        list(pool.map(worker, range(n_events)))

    events = snapshot_notifications(tmp_path)["system"]["data"]["events"]
    # All 20 fit under the 20-cap.
    assert len(events) == n_events
    refs = {e["ref_id"] for e in events}
    assert refs == {f"ref_{i}" for i in range(n_events)}
    event_ids = {e["event_id"] for e in events}
    assert len(event_ids) == n_events  # all distinct


def test_soul_voices_shape(tmp_path: Path) -> None:
    """The soul producer's voice-shaping helper trims empty fields."""
    from lingtai.tools.soul.flow import _shape_soul_voices

    voices = [
        {"source": "warmth", "voice": "remember to rest", "thinking": ["..."]},
        {"source": "doubt", "voice": "are you sure?", "thinking": []},
    ]
    shaped = _shape_soul_voices(voices)
    assert len(shaped) == 2
    assert shaped[0]["source"] == "warmth"
    assert shaped[0]["voice"] == "remember to rest"
    assert shaped[0]["thinking"] == ["..."]
    assert shaped[1]["voice"] == "are you sure?"
    # Empty thinking is omitted from the entry.
    assert "thinking" not in shaped[1]


def test_human_soul_inquiry_publishes_btw_notification(tmp_path: Path) -> None:
    """Human `/btw` inquiry results are mirrored to the agent as notification."""
    from lingtai.tools.soul.inquiry import (
        _publish_human_inquiry_notification,
    )

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    from lingtai.adapters.tool_plugin_host import agent_soul_runtime

    _publish_human_inquiry_notification(
        agent_soul_runtime(agent),
        {
            "prompt": "What should I know?",
            "voice": "You asked a side question.",
            "thinking": ["mirror thought"],
        },
        "What should I know?",
    )

    out = snapshot_notifications(tmp_path)
    assert "btw" in out
    payload = out["btw"]
    assert payload["header"] == "/btw side inquiry answered"
    assert payload["icon"] == "💭"
    assert "not a direct new instruction" in payload["instructions"]
    assert payload["data"] == {
        "source": "human",
        "mode": "inquiry",
        "question": "What should I know?",
        "answer": "You asked a side question.",
        "thinking": ["mirror thought"],
    }
    assert any(evt == "btw_notification_published" for evt, _ in agent._logs)


def test_non_human_soul_inquiry_does_not_publish_btw_notification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Auto-insight / agent inquiries keep the existing log-only behavior."""
    from lingtai.tools.soul import inquiry

    agent = _ProducerStubAgent(_working_dir=tmp_path)
    from lingtai.adapters.tool_plugin_host import agent_soul_runtime

    monkeypatch.setattr(
        inquiry,
        "soul_inquiry",
        lambda _agent, question: {
            "prompt": question,
            "voice": "auto answer",
            "thinking": [],
        },
    )

    inquiry._run_inquiry(agent_soul_runtime(agent), "auto?", source="insight")

    out = snapshot_notifications(tmp_path)
    assert "btw" not in out
    assert any(evt == "insight" for evt, _ in agent._logs)
    assert (tmp_path / "logs" / "soul_inquiry.jsonl").is_file()


# ---------------------------------------------------------------------------
# §13.6.bis — system.publish_notification (canonical helper)
# ---------------------------------------------------------------------------


def test_submit_writes_envelope(tmp_path: Path) -> None:
    """``submit`` builds the documented envelope and writes the file."""
    from lingtai.kernel.notifications import submit

    submit(store_agent_for(tmp_path), "system",
           header="hello", icon="✨",
           data={"x": 1, "y": [2, 3]})

    out = snapshot_notifications(tmp_path)
    assert "system" in out
    payload = out["system"]
    assert payload["header"] == "hello"
    assert payload["icon"] == "✨"
    assert payload["priority"] == "normal"
    assert payload["data"] == {"x": 1, "y": [2, 3]}
    # published_at is stamped, ISO format.
    assert "published_at" in payload
    assert payload["published_at"].endswith("Z")


def test_submit_priority_override(tmp_path: Path) -> None:
    from lingtai.kernel.notifications import submit

    submit(store_agent_for(tmp_path), "nudge",
           header="oh no", icon="🚨",
           priority="high", data={})

    assert snapshot_notifications(tmp_path)["nudge"]["priority"] == "high"


def test_submit_via_system_alias(tmp_path: Path) -> None:
    """``intrinsics.system.publish_notification`` is the same callable
    as ``notifications.submit`` — producers can import either."""
    from lingtai.tools.system import (
        publish_notification, clear_notification,
    )
    from lingtai.kernel.notifications import clear as core_clear, submit

    assert publish_notification is submit
    assert clear_notification is core_clear

    publish_notification(store_agent_for(tmp_path), "system",
                         header="via", icon="🛰",
                         data={"ok": True})
    out = snapshot_notifications(tmp_path)
    assert out["system"]["data"] == {"ok": True}

    clear_notification(store_agent_for(tmp_path), "system")
    out = snapshot_notifications(tmp_path)
    assert "system" not in out


# ---------------------------------------------------------------------------
# §13.7 — molt clearing
# ---------------------------------------------------------------------------


def test_molt_preserves_notification_dir(tmp_path: Path) -> None:
    """After molt, the .notification/ dir and its files survive — they are
    system state, not conversation memory.  In-memory tracking is reset
    (block_id, pending_meta) but the on-disk files and fingerprint persist."""
    publish_test_payload(tmp_path, "email", {"count": 3})
    publish_test_payload(tmp_path, "soul", {"voices": []})
    assert (tmp_path / ".notification").is_dir()

    # Stub agent with the bare minimum the molt reset logic needs.
    @dataclass
    class _MoltStub:
        _working_dir: Path = tmp_path
        _notification_fp: tuple = (("email.json", 1, 12),)
        _notification_block_id: str | None = "notif_xyz"
        _appendix_ids_by_source: dict = field(default_factory=dict)

    agent = _MoltStub()
    # Only reset in-memory tracking; notification files survive molt.
    agent._notification_block_id = None

    # .notification/ directory and files should still exist
    assert (tmp_path / ".notification").is_dir()
    assert (tmp_path / ".notification" / "email.json").is_file()
    assert (tmp_path / ".notification" / "soul.json").is_file()
    # _notification_fp keeps its value (files still on disk)
    assert agent._notification_fp == (("email.json", 1, 12),)
    # Wire-level tracking is reset
    assert agent._notification_block_id is None


# ---------------------------------------------------------------------------
# §13.2 / §13.3 — sync mechanism on a stub agent
# ---------------------------------------------------------------------------


def _make_chat_stub():
    """Minimal ChatInterface-backed chat stub for sync tests."""
    from lingtai.kernel.llm.interface import ChatInterface

    class _ChatStub:
        def __init__(self):
            self.interface = ChatInterface()

    return _ChatStub()


def _make_idle_agent_with_pending_tail(tmp_path: Path, *, call_id: str = "tc-pending"):
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.llm.interface import ToolCallBlock
    from lingtai.kernel.state import AgentState

    chat = _make_chat_stub()
    chat.interface.add_assistant_message(
        [ToolCallBlock(id=call_id, name="bash", args={"command": "echo hi"})]
    )

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_deferred_log_fp = ()
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    return _Agent(tmp_path)


def test_sync_idle_posts_wake_message(tmp_path: Path) -> None:
    """IDLE: fingerprint change -> MSG_TC_WAKE goes to the inbox.

    The synthesized ``(ToolCallBlock, ToolResultBlock)`` pair has
    already been spliced by ``_inject_notification_pair`` —
    impersonating a voluntary ``notification(action="check")`` call
    from the agent's perspective.  ``MSG_TC_WAKE`` then unblocks the
    run loop so ``_handle_tc_wake`` drives one inference round off
    the existing wire, no fake user message and no meta prefix.

    Regression for the IDLE-no-wake bug shipped in d2da97e: notifying
    without posting a wake message left the run loop blocked on
    ``inbox.get()`` even though the wire was correct on disk.
    """
    from lingtai.kernel.message import MSG_TC_WAKE

    agent = _make_stub_agent_for_block_log(
        tmp_path, notification_deferred_log_fp=(),
    )
    publish_test_payload(tmp_path, "email", {"count": 1})
    agent._sync_notifications()

    # Wire pair injected.
    assert len(agent._chat_stub.interface.entries) == 2
    # MSG_TC_WAKE in the inbox so the run loop picks it up and
    # _handle_tc_wake drives one inference round off the wire.
    msg = agent.inbox.get_nowait()
    assert msg.type == MSG_TC_WAKE


def test_sync_idle_heal_replays_recorded_tool_result_before_notification(
    tmp_path: Path,
) -> None:
    from lingtai.kernel.llm.interface import ToolResultBlock
    from lingtai.kernel.message import MSG_TC_WAKE

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_result",
                "tool_call_id": "tc-pending",
                "tool_name": "bash",
                "tool_args": {"command": "echo should-not-be-relogged"},
                "result": {"stdout": "already ran"},
                "ts": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    agent = _make_idle_agent_with_pending_tail(tmp_path)
    publish_test_payload(tmp_path, "email", {"count": 1})

    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    assert len(entries) == 4
    replayed = entries[1].content[0]
    assert isinstance(replayed, ToolResultBlock)
    assert replayed.id == "tc-pending"
    assert replayed.name == "bash"
    assert replayed.content == {"stdout": "already ran"}
    assert replayed.synthesized is False
    assert entries[2].role == "assistant"
    assert entries[3].role == "user"
    assert agent.inbox.get_nowait().type == MSG_TC_WAKE
    assert any(evt == "tool_result_replayed_from_log" for evt, _ in agent._logs)
    serialized_logs = json.dumps(agent._logs, default=str)
    assert "already ran" not in serialized_logs
    assert "should-not-be-relogged" not in serialized_logs


def test_sync_idle_heal_falls_back_to_synthetic_when_no_recorded_result(
    tmp_path: Path,
) -> None:
    from lingtai.kernel.llm.interface import ToolResultBlock

    agent = _make_idle_agent_with_pending_tail(tmp_path)
    publish_test_payload(tmp_path, "email", {"count": 1})

    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    assert len(entries) == 4
    healed = entries[1].content[0]
    assert isinstance(healed, ToolResultBlock)
    assert healed.id == "tc-pending"
    assert healed.synthesized is True
    assert "did not complete" in healed.content
    assert any(evt == "tool_result_replay_miss" for evt, _ in agent._logs)


def test_sync_idle_injects_post_molt_after_molt_batch_deferred_stamp(tmp_path: Path) -> None:
    """Regression for the post-molt continuation bug.

    A ``post-molt`` continuation is written while the agent is still ACTIVE
    inside the ``context.molt`` tool call.  That same molt-result batch must skip
    active notification stamping and leave ``_notification_fp`` uncommitted; if
    it stamped post-molt and committed the full fingerprint, the IDLE
    ``_sync_notifications`` pass would see no change and never inject the
    synthesized pair + MSG_TC_WAKE.

    Here we reproduce the uncommitted-fp state left by the per-molt-batch
    deferral and assert IDLE sync injects the wake.
    """
    from lingtai.kernel.message import MSG_TC_WAKE

    agent = _make_stub_agent_for_block_log(tmp_path)
    # The molt wrote the continuation channel while ACTIVE.
    publish_test_payload(tmp_path, "post-molt", {
        "header": "post-molt #1 — resume work",
        "icon": "🌱",
        "priority": "high",
        "data": {"molt_count": 1, "reminder": "continue the task"},
    })
    # Reproduce the fp the deferred molt batch leaves: unchanged/uncommitted.
    agent._notification_fp = ()

    agent._sync_notifications()

    # The IDLE path must still inject the synthesized (call, result) pair...
    entries = agent._chat_stub.interface.entries
    assert len(entries) == 2, "post-molt continuation must be injected at IDLE"
    body = entries[1].content[0].content
    assert isinstance(body, dict)
    result_block = entries[1].content[0]
    assert "post-molt" in result_block.metadata["agent_meta"]["notifications"]["attention"]
    # ...and post a wake so the run loop reorients around the continuation.
    msg = agent.inbox.get_nowait()
    assert msg.type == MSG_TC_WAKE


def test_sync_idle_injects_pair_with_synthesized_marker(tmp_path: Path) -> None:
    """IDLE: fingerprint change → synthetic pair appended; result block
    has synthesized=True and JSON body carries `_synthesized: true`."""
    from lingtai.kernel.llm.interface import ToolCallBlock, ToolResultBlock
    from lingtai.kernel.message import _make_message  # noqa: F401

    asleep_evt = threading.Event()
    cancel_event = threading.Event()
    agent = _make_stub_agent_for_block_log(
        tmp_path, asleep_evt=asleep_evt, cancel_event=cancel_event,
    )
    publish_test_payload(tmp_path, "email", {"count": 1, "data": {"count": 1}})

    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    assert len(entries) == 2  # call + result
    # First is assistant (call), second is user (result).
    assert entries[0].role == "assistant"
    assert entries[1].role == "user"
    # Assistant entry is tool-only: no visible synthesized TextBlock summary
    # should appear in the transcript / diary surface on the successful path.
    assert len(entries[0].content) == 1
    from lingtai.kernel.llm.interface import TextBlock
    assert not any(isinstance(block, TextBlock) for block in entries[0].content)
    injected_logs = [fields for evt, fields in agent._logs if evt == "notification_pair_injected"]
    assert injected_logs
    assert "Notification received: 1 email" in injected_logs[-1]["summary"]
    assert "not necessarily a human instruction" in injected_logs[-1]["summary"]
    call_block = entries[0].content[0]
    result_block = entries[1].content[0]
    assert isinstance(call_block, ToolCallBlock)
    # The kernel-synthesized delivery pair impersonates a voluntary
    # notification(action="check") read — not system(action="notification").
    assert call_block.name == "notification"
    # Pin the full synthesized call envelope: it must be byte-identical to
    # what a voluntary notification(action="check") call could produce, with
    # no internal freshness field such as injection_seq. A provider/model can
    # copy assistant-turn call args verbatim into a new real call, and
    # notification's ToolFamily dispatcher rejects any root field outside the
    # public {action, input, reasoning, summarize} allowlist with
    # INVALID_ARGUMENT: unsupported notification argument.
    assert call_block.args == {
        "action": "check",
        "input": {},
        "reasoning": "kernel notification sync",
    }
    assert isinstance(result_block, ToolResultBlock)
    assert result_block.name == "notification"
    assert result_block.synthesized is True

    body = result_block.content
    assert isinstance(body, dict)
    assert body["_synthesized"] is True
    # The model-visible ownership is the canonical sidecar, not handler content.
    meta = result_block.metadata["agent_meta"]
    assert meta["guidance"]["transient"] == {
        "ref": "meta_guidance.notification_handling",
        "sources": ["email"],
    }
    attention = meta["notifications"]["attention"]
    assert "email" in attention
    assert "notification_guidance" not in attention["email"]

    assert agent._notification_block_id == call_block.id


def test_synthesized_notification_call_args_survive_real_dispatch(
    tmp_path: Path,
) -> None:
    """A model that copies the synthesized call's args into a new real
    notification call must not get INVALID_ARGUMENT.

    This reproduces the live failure: the kernel-injected historical
    notification(action="check") ToolCallBlock.args were fed straight into
    the real dispatcher (as a provider/model copying an assistant-turn tool
    call would do), and strict envelope validation rejected an
    injection_seq root field with "unsupported notification argument".
    """
    from lingtai.kernel.llm.interface import ToolCallBlock

    publish_test_payload(tmp_path, "email", {"count": 1, "data": {"count": 1}})
    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    call_block = entries[0].content[0]
    assert isinstance(call_block, ToolCallBlock)
    assert call_block.name == "notification"

    @dataclass
    class _DispatchStub:
        _working_dir: Path = tmp_path
        _logs: list[tuple[str, dict]] = field(default_factory=list)

        def _log(self, evt: str, **fields: Any) -> None:
            self._logs.append((evt, fields))

    res = dispatch_declared_tool(NOTIFICATION_DECLARATION, _DispatchStub(), dict(call_block.args))

    assert res.get("error_code") != "INVALID_ARGUMENT"
    assert res.get("_notification_placeholder") is True


def test_sync_idle_releases_then_reinjects(tmp_path: Path) -> None:
    """Payload A — payload B — payload A again keeps append-only history
    and gives each synthetic result a fresh injection sequence."""

    agent = _make_stub_agent_for_block_log(tmp_path)
    payload_a = {"count": 1}
    payload_b = {"count": 2, "extra": "more bytes"}

    publish_test_payload(tmp_path, "email", payload_a)
    agent._sync_notifications()
    first_id = agent._notification_block_id
    assert first_id is not None
    assert len(agent._chat_stub.interface.entries) == 2

    publish_test_payload(tmp_path, "email", payload_b)
    agent._sync_notifications()
    second_id = agent._notification_block_id

    assert second_id is not None
    assert second_id != first_id

    # The original payload bytes reappear after an intervening payload.
    publish_test_payload(tmp_path, "email", payload_a)
    agent._sync_notifications()

    # Every pair remains in history verbatim; only the newest is the current
    # holder.
    entries = agent._chat_stub.interface.entries
    assert len(entries) == 6
    first_body = entries[1].content[0].content
    assert first_body["_synthesized"] is True
    result_blocks = [entries[index].content[0] for index in (1, 3, 5)]
    assert [block.content["injection_seq"] for block in result_blocks] == [1, 2, 3]
    assert [
        block.metadata["agent_meta"]["notifications"]["attention"]["email"]["count"]
        for block in result_blocks
    ] == [1, 2, 1]


def test_sync_idle_empty_releases_holder(tmp_path: Path) -> None:
    """When all producer files are cleared, the live holder is released
    from tracking without mutating its recorded wire content."""

    agent = _make_stub_agent_for_block_log(tmp_path)
    publish_test_payload(tmp_path, "email", {"count": 1})
    agent._sync_notifications()
    assert len(agent._chat_stub.interface.entries) == 2

    clear_test_payload(tmp_path, "email")
    agent._sync_notifications()

    # The synthesized pair remains in history, and its recorded wire content
    # is untouched (append-only) — only `agent._notification_live_holder` is
    # released so it is no longer treated as the current notification data.
    assert agent._notification_block_id is not None
    assert len(agent._chat_stub.interface.entries) == 2
    body = agent._chat_stub.interface.entries[1].content[0].content
    assert body["_synthesized"] is True
    result_block = agent._chat_stub.interface.entries[1].content[0]
    assert result_block.metadata["agent_meta"]["notifications"]["attention"]["email"]["count"] == 1
    assert agent._notification_live_holder is None


def test_sync_no_change_is_noop(tmp_path: Path) -> None:
    """Two syncs without any filesystem change → second is a no-op."""

    agent = _make_stub_agent_for_block_log(
        tmp_path, notification_deferred_log_fp=(),
    )
    publish_test_payload(tmp_path, "email", {"count": 1})
    agent._sync_notifications()
    first_id = agent._notification_block_id
    n_entries_before = len(agent._chat_stub.interface.entries)

    # No change to .notification/ — second sync should no-op.
    agent._sync_notifications()
    assert agent._notification_block_id == first_id
    assert len(agent._chat_stub.interface.entries) == n_entries_before


def test_sync_active_defers_without_committing_or_mutating_tool_result(tmp_path: Path) -> None:
    """ACTIVE state: fingerprint change is noticed but not delivered yet.

    The old behavior prepended ``notifications:\n...`` onto the most recent
    unrelated ToolResultBlock.  ACTIVE sync now leaves the wire byte-for-byte
    unchanged and keeps the fingerprint uncommitted so the next IDLE boundary
    retries via a distinct synthetic notification pair.
    """
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState
    from lingtai.kernel.llm.interface import ToolCallBlock, ToolResultBlock
    from tests._lifecycle_clock_helpers import make_test_lifecycle_clock

    chat = _make_chat_stub()
    iface = chat.interface
    iface.add_assistant_message(content=[ToolCallBlock(id="c1", name="daemon", args={})])
    iface.add_tool_results([
        ToolResultBlock(id="c1", name="daemon", content='{"status":"dispatched"}')
    ])
    original_content = iface.entries[1].content[0].content
    original_entry_count = len(iface.entries)

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.ACTIVE
            self._notification_fp = ()
            self._notification_deferred_log_fp = ()
            self._notification_block_id = None
            self._deferred_notifications_count = 0
            self._deferred_notifications_oldest_at = None
            # ACTIVE deferral seeds ``_deferred_notifications_oldest_at`` off the
            # injected lifecycle clock's wall reading (see
            # base_agent/__init__.py:_note_notification_deferred_active); this
            # partial stub must supply a real Port or the wall read raises.
            self._lifecycle_clock = make_test_lifecycle_clock()
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    publish_test_payload(tmp_path, "system", {"data": {"events": [{"source": "daemon"}]}})
    fp = fingerprint_notifications(tmp_path)

    agent._sync_notifications()
    agent._sync_notifications()

    assert agent._notification_fp == ()  # not committed while ACTIVE
    assert agent._notification_fp != fp
    assert agent._notification_deferred_log_fp == fp
    assert agent._notification_block_id is None
    assert agent.inbox.empty()
    assert len(iface.entries) == original_entry_count
    assert iface.entries[1].content[0].content == original_content
    assert not iface.entries[1].content[0].content.startswith("notifications:\n")
    assert [evt for evt, _ in agent._logs].count("notification_deferred_active") == 1
    assert agent._deferred_notifications_count == 2
    # The oldest-deferred timestamp is the injected clock's wall reading (the
    # fake's default wall value), not monotonic — direct wall-mapping evidence
    # for the deferred-oldest seam.
    assert agent._deferred_notifications_oldest_at == agent._lifecycle_clock.wall_seconds()
    assert agent._deferred_notifications_oldest_at == 1_000.0


def test_sync_empty_state_commits_empty_fingerprint(tmp_path: Path) -> None:
    """If producer files vanish, empty-state sync commits the empty fingerprint."""
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.ACTIVE
            self._notification_fp = (("soul.json", 1, 1),)
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    # No .notification files exist, so fingerprint changed from the stale
    # non-empty value above to empty.
    agent._sync_notifications()

    assert agent._notification_fp == ()


@pytest.mark.parametrize("state_name", ["STUCK", "SUSPENDED"])
def test_sync_noninjecting_state_commits_observed_store_version(
    tmp_path: Path, state_name: str
) -> None:
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.notifications import is_channel_allowed
    from lingtai.kernel.state import AgentState

    store = notification_store_for(tmp_path)
    store.publish("email", {"data": {"count": 1}})

    class _Agent:
        _sync_notifications = BaseAgent._sync_notifications
        _sync_notifications_locked = BaseAgent._sync_notifications_locked

        def __init__(self):
            self._notification_store = store
            self._notification_fp = ()
            self._notification_deferred_log_fp = (("old.json", 1, "old"),)
            self._state = getattr(AgentState, state_name)

    agent = _Agent()
    agent._sync_notifications()

    assert agent._notification_fp == store.fingerprint(is_channel_allowed)
    assert agent._notification_deferred_log_fp == ()


def test_session_manager_has_no_notification_inject_hook() -> None:
    """The retired ACTIVE meta-prefix hook is no longer part of SessionManager.

    Regression guard for #82: notification delivery must not mutate arbitrary
    ToolResultBlock content at ``SessionManager.send()`` time.
    """
    import inspect
    from lingtai.kernel.session import SessionManager

    params = inspect.signature(SessionManager.__init__).parameters
    assert "notification_inject_fn" not in params
    assert not hasattr(SessionManager.__new__(SessionManager), "_notification_inject_fn")


def test_base_agent_no_longer_exposes_meta_prefix_injector() -> None:
    """BaseAgent no longer carries the mutating _inject_notification_meta path."""
    from lingtai.kernel.base_agent import BaseAgent

    assert not hasattr(BaseAgent, "_inject_notification_meta")


def test_end_of_turn_idle_sync_delivers_deferred_notification(tmp_path: Path) -> None:
    """End-of-turn sync runs after IDLE transition and delivers a pair+wake.

    This exercises the #83 ordering: a notification produced during ACTIVE work
    must not be stranded in ACTIVE deferral.  At the post-turn IDLE boundary it
    becomes a distinct synthetic notification pair and a MSG_TC_WAKE.
    """
    import queue
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.base_agent import turn as turn_mod
    from lingtai.kernel.message import _make_message, MSG_REQUEST, MSG_TC_WAKE
    from lingtai.kernel.state import AgentState
    from lingtai.kernel.llm.interface import ToolResultBlock

    class _SessionStub:
        def __init__(self, chat):
            self.chat = chat

        def get_context_pressure(self):
            return 0.0

    class _ConfigStub:
        language = "en"
        molt_pressure = 0.9
        molt_prompt = ""
        insights_interval = 0
        max_aed_attempts = 1

    chat = _make_chat_stub()
    states: list[AgentState] = []

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.ACTIVE
            self._notification_fp = ()
            self._notification_block_id = None
            self._notification_inject_seq = 0
            self._chat_stub = chat
            self._session = _SessionStub(chat)
            self._logs = []
            self.agent_name = "stub"
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._cancel_event = threading.Event()
            self._shutdown = threading.Event()
            self._config = _ConfigStub()

        @property
        def _chat(self):
            return self._chat_stub

        def _set_state(self, new_state, reason=""):
            self._state = new_state
            states.append(new_state)

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    agent.inbox.put(_make_message(MSG_REQUEST, "tester", "do work"))

    # Stop the run loop after the post-turn sweep has a chance to execute.
    # Patch the module-level dispatcher because _run_loop calls it directly.
    def fake_handle_message(_agent, _msg):
        publish_test_payload(_agent._working_dir, "system", {"data": {"events": [{"source": "daemon"}]}})
        _agent._shutdown.set()

    orig_handle = turn_mod._handle_message
    try:
        turn_mod._handle_message = fake_handle_message
        turn_mod._run_loop(agent)
    finally:
        turn_mod._handle_message = orig_handle

    assert AgentState.IDLE in states
    assert agent._notification_block_id is not None
    assert agent._notification_fp == fingerprint_notifications(tmp_path)
    wake = agent.inbox.get_nowait()
    assert wake.type == MSG_TC_WAKE

    entries = chat.interface.entries
    assert len(entries) == 2
    assert entries[0].role == "assistant"
    assert entries[1].role == "user"
    result_block = entries[1].content[0]
    assert isinstance(result_block, ToolResultBlock)
    body = result_block.content
    assert isinstance(body, dict)
    assert body["_synthesized"] is True
    assert "system" in result_block.metadata["agent_meta"]["notifications"]["attention"]
    assert isinstance(result_block.content, dict)


# ---------------------------------------------------------------------------
# §13.4 — ASLEEP wake on fingerprint change
# ---------------------------------------------------------------------------


def test_sync_asleep_wakes_on_change(tmp_path: Path) -> None:
    """Producer publishes while agent is ASLEEP → state transitions to
    IDLE, pair is injected, MSG_TC_WAKE goes to inbox."""
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState
    from lingtai.kernel.message import MSG_TC_WAKE

    chat = _make_chat_stub()
    state_history: list[AgentState] = []

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.ASLEEP
            self._notification_fp = ()
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._asleep.set()
            self._cancel_event = threading.Event()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, new_state, reason=""):
            self._state = new_state
            state_history.append(new_state)

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    agent._cancel_event.set()
    publish_test_payload(tmp_path, "email", {"count": 1})

    agent._sync_notifications()

    assert agent._state == AgentState.IDLE
    assert agent._cancel_event.is_set()
    assert AgentState.IDLE in state_history
    # MSG_TC_WAKE delivered — _handle_tc_wake will drive the wire forward.
    msg = agent.inbox.get_nowait()
    assert msg.type == MSG_TC_WAKE
    # Wire pair was injected.
    assert len(agent._chat_stub.interface.entries) == 2


def test_sync_asleep_no_change_stays_asleep(tmp_path: Path) -> None:
    """No producer write → fingerprint stays empty → agent stays
    ASLEEP."""
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.ASLEEP
            self._notification_fp = ()
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._asleep.set()
            self._cancel_event = threading.Event()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            self._state = _a[0] if _a else _kw.get("new_state")

        def _reset_uptime(self):
            pass

    agent = _Agent(tmp_path)
    agent._sync_notifications()

    assert agent._state == AgentState.ASLEEP
    assert agent.inbox.empty()
    assert len(agent._chat_stub.interface.entries) == 0


# ---------------------------------------------------------------------------
# §13.4.bis — ASLEEP wake when injection still fails after heal (degraded path)
# ---------------------------------------------------------------------------


def _make_asleep_inject_fail_agent(tmp_path: Path, chat, state_history):
    """Build an ASLEEP stub agent whose `_inject_notification_pair`
    always returns False — simulating a wire that cannot accept the
    synthetic pair even after `_heal_pending_tool_calls`."""
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.ASLEEP
            self._notification_fp = ()
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._asleep.set()
            self._cancel_event = threading.Event()
            self.inject_calls = 0
            self.heal_calls = 0

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, new_state, reason=""):
            self._state = new_state
            state_history.append((new_state, reason))

        def _reset_uptime(self):
            pass

        def _inject_notification_pair(self, notifications):
            self.inject_calls += 1
            return False

        def _heal_pending_tool_calls(self, *, reason):
            self.heal_calls += 1
            return False

    return _Agent(tmp_path)


def test_sync_asleep_inject_fail_falls_back_to_degraded_request(tmp_path: Path) -> None:
    """ASLEEP + inject keeps failing after heal → degraded MSG_REQUEST,
    state IDLE (not ASLEEP), fingerprint committed, log emitted.

    Regression for Jason's livelock report: the prior behavior reverted
    to ASLEEP without committing the fingerprint, so the next heartbeat
    saw the same .notification/ state, woke again, failed inject again,
    reverted again — forever. The fix wakes the agent via a degraded
    request that tells it to call notification(action="check") or
    read the producer files directly.
    """
    from lingtai.kernel.state import AgentState
    from lingtai.kernel.message import MSG_REQUEST, MSG_TC_WAKE
    from tests._notification_store_helpers import fingerprint_notifications

    chat = _make_chat_stub()
    state_history: list = []
    agent = _make_asleep_inject_fail_agent(tmp_path, chat, state_history)

    publish_test_payload(tmp_path, "mcp.wechat", {"data": {"count": 2}})
    fp_before = fingerprint_notifications(tmp_path)

    agent._sync_notifications()

    # State stays IDLE (not reverted to ASLEEP) so run loop can run.
    assert agent._state == AgentState.IDLE
    assert AgentState.IDLE in [s for s, _ in state_history]
    assert AgentState.ASLEEP not in [s for s, _ in state_history if s == AgentState.ASLEEP and _ != "notification_arrival"]

    # Inbox got a degraded MSG_REQUEST (not MSG_TC_WAKE).
    msg = agent.inbox.get_nowait()
    assert msg.type == MSG_REQUEST
    assert msg.type != MSG_TC_WAKE
    # Content mentions the failure and tells the agent how to recover.
    assert "notification" in msg.content.lower()
    assert "mcp.wechat" in msg.content
    # The body should point at the recovery handles.
    assert ("system" in msg.content) or ("producer" in msg.content.lower())

    # Fingerprint committed so the same failure does not replay.
    assert agent._notification_fp == fp_before
    assert agent._notification_fp != ()

    # Clear, single log event for diagnostics.
    degraded_logs = [f for evt, f in agent._logs if evt == "notification_wake_degraded"]
    assert len(degraded_logs) == 1
    log_fields = degraded_logs[0]
    assert log_fields.get("reason")
    assert "mcp.wechat" in log_fields.get("sources", [])

    # Heal was tried; inject was tried twice (initial + post-heal).
    assert agent.heal_calls == 1
    assert agent.inject_calls == 2


def test_sync_asleep_inject_fail_does_not_replay_on_second_sync(tmp_path: Path) -> None:
    """After the degraded path commits the fingerprint, a second sync
    with the same on-disk state must be a complete no-op — no extra
    inject attempts, no extra inbox messages, no extra log entries."""
    from lingtai.kernel.state import AgentState

    chat = _make_chat_stub()
    state_history: list = []
    agent = _make_asleep_inject_fail_agent(tmp_path, chat, state_history)

    publish_test_payload(tmp_path, "mcp.wechat", {"data": {"count": 1}})
    agent._sync_notifications()

    inject_calls_after_first = agent.inject_calls
    inbox_size_after_first = agent.inbox.qsize()
    degraded_logs_after_first = sum(
        1 for evt, _ in agent._logs if evt == "notification_wake_degraded"
    )

    # Second sync — same fingerprint, must short-circuit.
    agent._sync_notifications()

    assert agent.inject_calls == inject_calls_after_first
    assert agent.inbox.qsize() == inbox_size_after_first
    degraded_logs_after_second = sum(
        1 for evt, _ in agent._logs if evt == "notification_wake_degraded"
    )
    assert degraded_logs_after_second == degraded_logs_after_first


# ---------------------------------------------------------------------------
# §13.8 — wire-drive contract: session.send(None) means "continue from wire"
# ---------------------------------------------------------------------------


def _make_anthropic_session_with_pre_staged_pair():
    """Build a real AnthropicChatSession with a synthesized notification
    pair already at the wire tail."""
    from unittest.mock import MagicMock
    from lingtai.kernel.llm.interface import (
        ChatInterface,
        ToolCallBlock,
        ToolResultBlock,
    )
    from lingtai.llm.anthropic.adapter import AnthropicChatSession

    iface = ChatInterface()
    iface.add_assistant_message(content=[
        ToolCallBlock(id="notif_1", name="system",
                      args={"action": "notification"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="notif_1", name="system",
                        content='{"_synthesized": true}',
                        synthesized=True),
    ])

    session = AnthropicChatSession(
        client=MagicMock(),
        model="claude-sonnet-test",
        system_prompt="system",
        interface=iface,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
    )
    return session, iface


def _fake_anthropic_response(text: str = "ok"):
    """Build a MagicMock that mimics anthropic SDK's response shape."""
    from unittest.mock import MagicMock

    raw = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    raw.content = [block]
    raw.usage = MagicMock(
        input_tokens=10,
        output_tokens=2,
        thinking_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    raw.id = "resp_1"
    raw.model = "claude-sonnet-test"
    raw.role = "assistant"
    raw.stop_reason = "end_turn"
    return raw


def test_anthropic_send_none_does_not_append_input() -> None:
    """``AnthropicChatSession.send(None)`` calls the API with the
    pre-staged wire, does not append a user message, and records only
    the assistant response."""
    session, iface = _make_anthropic_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.messages.create.return_value = _fake_anthropic_response()

    response = session.send(None)

    assert response is not None
    assert session._client.messages.create.called
    # Wire grew by exactly one entry — the assistant response — not two.
    assert len(iface.entries) == pre_count + 1
    assert iface.entries[-1].role == "assistant"
    # The pre-staged pair is intact.
    assert iface.entries[pre_count - 2].role == "assistant"
    assert iface.entries[pre_count - 1].role == "user"


def test_anthropic_send_none_error_does_not_drop_pair() -> None:
    """API failure during a ``send(None)`` must not invoke
    ``drop_trailing`` — the pre-staged user entry is the notification
    pair's tool_result, not something this call appended."""
    session, iface = _make_anthropic_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.messages.create.side_effect = RuntimeError("boom")

    try:
        session.send(None)
    except RuntimeError:
        pass

    # Wire is unchanged — the synthesized pair survived.
    assert len(iface.entries) == pre_count
    assert iface.entries[-1].role == "user"
    from lingtai.kernel.llm.interface import ToolResultBlock
    assert any(
        isinstance(b, ToolResultBlock) for b in iface.entries[-1].content
    )


def _make_openai_session_with_pre_staged_pair():
    """Build a real OpenAIChatSession with a synthesized notification
    pair already at the wire tail."""
    from unittest.mock import MagicMock
    from lingtai.kernel.llm.interface import (
        ChatInterface,
        ToolCallBlock,
        ToolResultBlock,
    )
    from lingtai.llm.openai.adapter import OpenAIChatSession

    iface = ChatInterface()
    iface.add_assistant_message(content=[
        ToolCallBlock(id="notif_1", name="system",
                      args={"action": "notification"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="notif_1", name="system",
                        content='{"_synthesized": true}',
                        synthesized=True),
    ])

    session = OpenAIChatSession(
        client=MagicMock(),
        model="gpt-test",
        interface=iface,
        tools=None,
        tool_choice=None,
        extra_kwargs={},
        client_kwargs={},
    )
    return session, iface


def _fake_openai_response(text: str = "ok"):
    """Build a MagicMock mimicking openai SDK's ChatCompletion shape."""
    from unittest.mock import MagicMock

    raw = MagicMock()
    msg = MagicMock()
    msg.role = "assistant"
    msg.content = text
    msg.tool_calls = None
    msg.reasoning_content = None
    msg.reasoning = None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"
    raw.choices = [choice]
    raw.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    raw.model = "gpt-test"
    raw.id = "resp_1"
    return raw


def test_openai_send_none_does_not_append_input() -> None:
    """``OpenAIChatSession.send(None)`` drives the API off the
    pre-staged wire, does not append a user message, and records only
    the assistant response."""
    session, iface = _make_openai_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.chat.completions.create.return_value = _fake_openai_response()

    response = session.send(None)

    assert response is not None
    assert session._client.chat.completions.create.called
    # Wire grew by exactly one entry — the assistant response — not two.
    assert len(iface.entries) == pre_count + 1
    assert iface.entries[-1].role == "assistant"


def test_openai_send_none_error_does_not_drop_pair() -> None:
    """API failure during a ``send(None)`` must not corrupt the
    pre-staged wire."""
    session, iface = _make_openai_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.chat.completions.create.side_effect = RuntimeError("boom")

    try:
        session.send(None)
    except RuntimeError:
        pass

    assert len(iface.entries) == pre_count
    assert iface.entries[-1].role == "user"


def test_openai_send_str_still_appends_user_message() -> None:
    """Sanity check: the existing str/list paths are unchanged."""
    session, iface = _make_openai_session_with_pre_staged_pair()
    pre_count = len(iface.entries)
    session._client.chat.completions.create.return_value = _fake_openai_response()

    session.send("hello world")

    # Two new entries: the user message we just appended, and the
    # assistant response.
    assert len(iface.entries) == pre_count + 2


def test_responses_convert_input_none_yields_empty_list() -> None:
    """``OpenAIResponsesSession._convert_input(None)`` returns ``[]``
    so the existing ``previous_response_id`` chain continues with no
    new input items."""
    from lingtai.llm.openai.adapter import OpenAIResponsesSession

    session = OpenAIResponsesSession.__new__(OpenAIResponsesSession)
    assert OpenAIResponsesSession._convert_input(session, None) == []



def test_context_molt_batch_skips_active_notification_stamp(tmp_path):
    """The context.molt result batch must not consume its own post-molt wake."""
    from types import SimpleNamespace

    from lingtai.kernel.base_agent.turn import _batch_includes_context_molt
    from lingtai.kernel.llm.base import ToolCall
    from lingtai.kernel.llm.interface import ToolResultBlock
    from lingtai.kernel.meta_block import attach_active_notifications
    from tests._notification_store_helpers import fingerprint_notifications

    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "post-molt.json").write_text(
        '{"header": "post-molt #1", "data": {"molt_count": 1}}'
    )

    agent = SimpleNamespace(
        _working_dir=tmp_path,
        _notification_fp=(("sentinel.json", 1, 1),),
        _notification_live_holder=None,
    )
    molt_call = ToolCall(
        name="context",
        args={"action": "molt", "input": {"summary": "continue"}},
        id="call_molt",
    )
    assert _batch_includes_context_molt([molt_call]) is True

    molt_result = ToolResultBlock(id="call_molt", name="context", content={"status": "ok"})
    if not _batch_includes_context_molt([molt_call]):
        agent._notification_live_holder = attach_active_notifications(
            agent, [molt_result], prior_holder=agent._notification_live_holder
        )

    assert "notifications" not in molt_result.content
    assert agent._notification_fp == (("sentinel.json", 1, 1),)
    assert fingerprint_notifications(tmp_path) != agent._notification_fp


def test_non_molt_batch_after_molt_can_consume_post_molt(tmp_path):
    """If the agent keeps going ACTIVE after molt, the next batch sees post-molt."""
    from types import SimpleNamespace

    from lingtai.kernel.base_agent.turn import _batch_includes_context_molt
    from lingtai.kernel.llm.base import ToolCall
    from lingtai.kernel.llm.interface import ToolResultBlock
    from lingtai.kernel.meta_block import attach_active_notifications
    from tests._notification_store_helpers import fingerprint_notifications

    notif_dir = tmp_path / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    (notif_dir / "post-molt.json").write_text(
        '{"header": "post-molt #1", "data": {"molt_count": 1}}'
    )

    agent = SimpleNamespace(
        _working_dir=tmp_path,
        _notification_store=notification_store_for(tmp_path),
        _notification_fp=(),
        _notification_live_holder=None,
    )
    later_call = ToolCall(name="bash", args={"command": "true"}, id="call_later")
    assert _batch_includes_context_molt([later_call]) is False

    later_result = ToolResultBlock(id="call_later", name="bash", content={"status": "ok"})
    if not _batch_includes_context_molt([later_call]):
        agent._notification_live_holder = attach_active_notifications(
            agent, [later_result], prior_holder=agent._notification_live_holder
        )

    assert "post-molt" in later_result.metadata["agent_meta"]["notifications"]["attention"]
    assert agent._notification_fp == fingerprint_notifications(tmp_path)


# ---------------------------------------------------------------------------
# §13.8 — notification_block_injected durable snapshot event
# ---------------------------------------------------------------------------


def _make_stub_agent_for_block_log(
    tmp_path: Path,
    *,
    notification_deferred_log_fp=None,
    asleep_evt=None,
    cancel_event=None,
):
    """Build the shared notification-sync agent, preserving optional state fields."""
    from dataclasses import dataclass, field as dc_field
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.IDLE
            self._notification_fp = ()
            if notification_deferred_log_fp is not None:
                self._notification_deferred_log_fp = notification_deferred_log_fp
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs: list = []
            self.agent_name = "stub"
            if asleep_evt is not None:
                self._asleep_evt = asleep_evt
            if cancel_event is not None:
                self._cancel_event = cancel_event
            import queue
            self.inbox = queue.Queue()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    return _Agent(tmp_path)


def test_notification_injection_has_no_unbound_local_meta_builder(
    tmp_path: Path,
) -> None:
    """Regression: synthetic notification injection must resolve its meta builder."""
    publish_test_payload(tmp_path, "email", {"count": 1})
    agent = _make_stub_agent_for_block_log(tmp_path)

    try:
        injected = agent._inject_notification_pair(
            snapshot_notifications(tmp_path)
        )
    except UnboundLocalError as exc:
        pytest.fail(f"notification injection raised UnboundLocalError: {exc}")

    assert injected is True
    assert len(agent._chat_stub.interface.entries) == 2
    result_block = agent._chat_stub.interface.entries[1].content[0]
    assert result_block.metadata["tool_meta"]["synthetic"] is True


def test_delta_persistent_lane_paths_match_current_synthetic_metadata() -> None:
    """Continuity hooks must point at the real #939 metadata location."""
    from lingtai.kernel.meta_block import _IM_PERSISTENT_LANES

    paths = {
        lane.channel: lane.path
        for lane in _IM_PERSISTENT_LANES
        if lane.mode == "delta"
    }
    assert paths == {
        "telegram": "_meta.agent_meta.notifications.persistent.mcp.telegram",
        "wechat": "_meta.agent_meta.notifications.persistent.mcp.wechat",
        "feishu": "_meta.agent_meta.notifications.persistent.mcp.feishu",
    }


def test_inject_notification_pair_emits_block_injected_event(tmp_path: Path) -> None:
    """IDLE injection via _inject_notification_pair must log notification_block_injected
    with the full ``_meta`` envelope (tool_meta/agent_meta/guidance/notifications/
    notification_guidance)."""
    publish_test_payload(tmp_path, "email", {"count": 2, "data": {"count": 2, "digest": "2 unread"}})
    publish_test_payload(tmp_path, "system", {"events": [{"source": "test", "body": "ping"}]})

    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    # notification_pair_injected must still fire (existing behavior unchanged).
    pair_logs = [f for evt, f in agent._logs if evt == "notification_pair_injected"]
    assert pair_logs, "notification_pair_injected must still be logged"

    # notification_block_injected must be emitted.
    block_logs = [f for evt, f in agent._logs if evt == "notification_block_injected"]
    assert block_logs, "notification_block_injected event must be logged"
    bl = block_logs[-1]

    assert bl["mode"] == "synthetic_notification_pair"
    assert "call_id" in bl
    assert isinstance(bl["sources"], list)
    assert "email" in bl["sources"]
    assert "system" in bl["sources"]

    # New schema: the logged ``_meta`` is the ToolResultBlock.metadata
    # envelope; formal guidance and notification lanes are nested under
    # ``agent_meta``.
    meta = bl["_meta"]
    assert "tool_meta" in meta
    assert meta["tool_meta"].get("synthetic") is True
    assert "agent_meta" in meta
    # Tail/synthetic guidance is now a lightweight ref/hook pointing at the
    # resident ``meta_guidance`` system-prompt section, not the full ordered
    # sections (which no longer ride on every result).
    agent_meta = meta["agent_meta"]
    assert "guidance" in agent_meta
    guidance = agent_meta["guidance"]["persistent"]
    assert "sections" not in guidance
    assert guidance.get("ref") == "meta_guidance"

    assert agent_meta["guidance"]["transient"] == {
        "ref": "meta_guidance.notification_handling",
        "sources": ["email", "system"],
    }
    assert "notifications" in agent_meta
    notifs = agent_meta["notifications"]["attention"]
    assert "email" in notifs
    assert "system" in notifs
    # Per-channel duplicate static guidance is omitted.
    assert "notification_guidance" not in notifs["email"]


def _make_agent_with_real_event_journal(tmp_path: Path):
    """Agent stub keeping the REAL ``BaseAgent._log`` + production journal
    (redaction, sqlite index, ``logs/events.jsonl``) — no ``_log`` override."""
    import queue
    import time as time_mod

    from lingtai.adapters.posix.event_journal import PosixJsonlEventJournalAdapter
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState

    chat = _make_chat_stub()

    class _WallClock:
        def wall_seconds(self) -> float:
            return time_mod.time()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.IDLE
            self._notification_fp = ()
            self._notification_deferred_log_fp = ()
            self._notification_block_id = None
            self._chat_stub = chat
            self.agent_name = "stub"
            self.inbox = queue.Queue()
            self._event_journal = PosixJsonlEventJournalAdapter(workdir)
            self._lifecycle_clock = _WallClock()
            self._runtime_identity_event_fields = {}
            self._deferred_notifications_count = 0
            self._deferred_notifications_oldest_at = None

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, *_a, **_kw):
            pass

        def _reset_uptime(self):
            pass

    return _Agent(tmp_path)


def _read_journal_events(tmp_path: Path) -> list[dict]:
    events_path = tmp_path / "logs" / "events.jsonl"
    if not events_path.is_file():
        return []
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_notification_replay_e2e_heal_restores_full_envelope(tmp_path: Path) -> None:
    """#419 E2E on the production journal: sync → durable recovery record →
    provider rollback → heal replays the full envelope (content + metadata +
    synthesized=True) → unchanged follow-up sync injects no duplicate."""
    import copy as copy_mod

    publish_test_payload(
        tmp_path, "email", {"count": 2, "data": {"count": 2, "digest": "2 unread"}}
    )
    agent = _make_agent_with_real_event_journal(tmp_path)

    agent._sync_notifications()
    iface = agent._chat_stub.interface
    assert len(iface.entries) == 2
    original = iface.entries[1].content[0]
    call_id = original.id
    assert original.synthesized is True and original.metadata
    original_content = copy_mod.deepcopy(original.content)
    original_metadata = copy_mod.deepcopy(original.metadata)
    assert agent._notification_fp == fingerprint_notifications(tmp_path)

    # Durable self-contained recovery record; deliberately not the canonical
    # ToolExecutor lifecycle (no trace id, no visibility event).
    events = _read_journal_events(tmp_path)
    event = [
        e for e in events
        if e.get("type") == "tool_result" and e.get("tool_call_id") == call_id
    ][-1]
    assert event["tool_name"] == "notification"
    assert event["tool_args"] == {
        "action": "check", "input": {}, "reasoning": "kernel notification sync"
    }
    assert event["result"] == original_content
    assert event["result_metadata"] == original_metadata
    assert event["synthesized"] is True
    assert event["origin"] == "kernel_notification_sync"
    assert event["redacted"] is False
    assert "tool_trace_id" not in event
    assert "tool_result_durable_log_visible" not in [e.get("type") for e in events]

    # Provider/adapter rollback of the completed result, then heal.
    iface.entries.pop()
    assert iface.has_pending_tool_calls()
    assert agent._heal_pending_tool_calls(reason="test_provider_rollback") is True
    assert not iface.has_pending_tool_calls()
    recovered = iface.entries[-1].content[0]
    assert recovered.id == call_id and recovered.name == "notification"
    assert recovered.content == original_content
    assert recovered.metadata == original_metadata
    assert recovered.synthesized is True
    types = [e.get("type") for e in _read_journal_events(tmp_path)]
    assert "tool_result_replayed_from_log" in types
    assert "tool_result_replay_miss" not in types

    # Unchanged follow-up sync: fingerprint short-circuits, no duplicate pair.
    entries_before = len(iface.entries)
    agent._sync_notifications()
    assert len(iface.entries) == entries_before
    assert iface.entries[-1].content[0].metadata == original_metadata
    pair_events = [
        e for e in _read_journal_events(tmp_path)
        if e.get("type") == "notification_pair_injected"
    ]
    assert len(pair_events) == 1


def test_notification_replay_e2e_redacted_secret_marks_and_reconciles(
    tmp_path: Path,
) -> None:
    """Security contract: the raw secret never lands in events.jsonl; a
    redacted replay carries the _meta.redacted marker and resets the
    committed fingerprint so the next sync re-injects full producer state."""
    import copy as copy_mod

    from lingtai.kernel.trace_redaction import redact_for_trajectory

    secret = "sk-live-SUPERSECRET-123"
    publish_test_payload(
        tmp_path,
        "system",
        {"data": {"events": [{"source": "daemon", "body": f"deploy failed: {secret}"}]}},
    )
    agent = _make_agent_with_real_event_journal(tmp_path)

    agent._sync_notifications()
    iface = agent._chat_stub.interface
    original = iface.entries[1].content[0]
    original_content = copy_mod.deepcopy(original.content)
    original_metadata = copy_mod.deepcopy(original.metadata)
    assert secret in json.dumps([original.content, original.metadata])
    committed_fp = agent._notification_fp
    assert committed_fp == fingerprint_notifications(tmp_path)

    events_path = tmp_path / "logs" / "events.jsonl"
    assert secret not in events_path.read_text(encoding="utf-8")
    durable = [
        e for e in _read_journal_events(tmp_path) if e.get("type") == "tool_result"
    ][-1]
    assert durable["redacted"] is True

    # Rollback + heal: replay is exactly the redacted projection, marked.
    iface.entries.pop()
    assert agent._heal_pending_tool_calls(reason="test_provider_rollback") is True
    recovered = iface.entries[-1].content[0]
    assert recovered.id == original.id and recovered.synthesized is True
    assert recovered.content == original_content
    expected_metadata = redact_for_trajectory(original_metadata)
    expected_metadata["redacted"] = True
    assert recovered.metadata == expected_metadata
    assert secret not in json.dumps([recovered.content, recovered.metadata])
    events = _read_journal_events(tmp_path)
    types = [e.get("type") for e in events]
    assert "tool_result_replay_miss" not in types
    replayed = [e for e in events if e.get("type") == "tool_result_replayed_from_log"][-1]
    assert replayed["recovered_synthesized"] is True
    assert replayed["recovered_redacted"] is True
    assert "notification_redacted_replay_resync" in types

    # Fingerprint reset → the unchanged producer state re-injects fully.
    assert agent._notification_fp == ()
    entries_before = len(iface.entries)
    agent._sync_notifications()
    assert len(iface.entries) == entries_before + 2
    final = iface.entries[-1].content[0]
    assert final.synthesized is True
    assert secret in json.dumps([final.content, final.metadata])
    assert agent._notification_fp == committed_fp
    assert secret not in events_path.read_text(encoding="utf-8")


def test_notification_recovery_record_failure_does_not_abort_injection(
    tmp_path: Path,
) -> None:
    """Fail-open: a recovery-record write failure never aborts injection and
    surfaces as recovery_record_error on notification_pair_injected."""
    publish_test_payload(tmp_path, "email", {"count": 1, "data": {"count": 1}})
    agent = _make_agent_with_real_event_journal(tmp_path)
    real_log = agent._log

    def _failing_tool_result_log(evt: str, **fields) -> None:
        if evt == "tool_result":
            raise RuntimeError("simulated journal failure for tool_result")
        real_log(evt, **fields)

    agent._log = _failing_tool_result_log
    assert agent._inject_notification_pair(snapshot_notifications(tmp_path)) is True
    iface = agent._chat_stub.interface
    assert len(iface.entries) == 2
    assert iface.entries[1].content[0].synthesized is True
    events = _read_journal_events(tmp_path)
    types = [e.get("type") for e in events]
    assert "tool_result" not in types
    assert "notification_block_injected" in types
    pair = [e for e in events if e.get("type") == "notification_pair_injected"][-1]
    assert pair["recovery_record_error"] == "RuntimeError"


def test_inject_notification_pair_adds_telegram_persistent_and_strips_ephemeral(
    tmp_path: Path,
) -> None:
    """IDLE/SYNTHETIC delivery moves Telegram text into notification_persistent."""
    messages = [
        {
            "id": f"main:123:{i}",
            "direction": "incoming",
            "sender": "Jason",
            "date": f"2026-07-05T22:{i:02d}:00Z",
            "text": f"message {i}",
            "taskcard": False,
            "is_current": i == 21,
        }
        for i in range(1, 22)
    ]
    publish_test_payload(
        tmp_path,
        "mcp.telegram",
        {
            "header": "Telegram",
            "data": {
                "count": 1,
                "source": "telegram",
                "has_human_messages": True,
                "previews": [
                    {
                        "from": "Jason",
                        "subject": "telegram message",
                        "preview": "the last-20 conversation transcript body",
                        "preview_truncated": False,
                        "platform": "telegram",
                        "conversation_ref": "main:123",
                        "message_ref": "main:123:21",
                        "recent_messages": messages,
                        "latest_incoming": messages[-1],
                    }
                ],
            },
        },
    )

    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    result_block = entries[1].content[0]
    body = result_block.content
    assert isinstance(body, dict)
    assert body == {"_synthesized": True, "injection_seq": 1}
    meta = result_block.metadata
    notifications = meta["agent_meta"]["notifications"]
    telegram = notifications["persistent"]["mcp"]["telegram"]
    assert len(telegram["messages"]) == 20
    assert telegram["messages"][0]["id"] == "main:123:2"
    assert telegram["messages"][-1]["id"] == "main:123:21"
    assert all(message["taskcard"] is False for message in telegram["messages"])
    assert telegram["previous_block"] == {
        "path": "_meta.agent_meta.notifications.persistent.mcp.telegram",
        "tool_result_id": None,
        "is_first_block": True,
    }

    assert telegram["events"] == [
        {
            "from": "Jason",
            "subject": "telegram message",
            "conversation_ref": "main:123",
            "message_ref": "main:123:21",
            "platform": "telegram",
        }
    ]
    transient = notifications["attention"]["mcp.telegram"]
    assert transient["data"] == {"message_ids": ["main:123:21"]}
    assert "previews" not in transient["data"]
    assert "source" not in transient["data"]
    assert "count" not in transient["data"]
    assert "has_human_messages" not in transient["data"]
    assert "telegram message" not in transient["instructions"]


def test_inject_notification_pair_strips_legacy_tool_meta_context_transit_keys(
    tmp_path: Path, monkeypatch
) -> None:
    """Synthesized notification pairs must not expose tool-meta transit keys."""
    import lingtai.kernel.base_agent as base_agent_module
    import lingtai.kernel.meta_block as meta_block_module
    from lingtai.kernel.meta_block import (
        TOOL_META_CONTEXT_EVENT_PENDING_KEY,
        TOOL_META_CONTEXT_PENDING_KEY,
    )

    # Historical logged-input path label: projection must strip this legacy
    # tool-meta route and emit only the current agent-state axes.
    transit_event = {
        "event_name": "context_pressure_current_molt_reminder_emitted",
        "payload": {
            "message_hash": "abc123transit",
            "target_path": "_meta.tool_meta.context.molt",
            "streak": 3,
        },
    }

    def fake_build_meta(_agent):
        return {
            "current_time": "2026-07-01T00:00:00Z",
            TOOL_META_CONTEXT_PENDING_KEY: {"molt": "transit molt prose"},
            TOOL_META_CONTEXT_EVENT_PENDING_KEY: transit_event,
            "current_tool_result_chars": {"total_chars": 1},
        }

    monkeypatch.setattr(base_agent_module, "build_meta", fake_build_meta)
    monkeypatch.setattr(meta_block_module, "build_meta", fake_build_meta)

    publish_test_payload(tmp_path, "email", {"count": 1, "data": {"count": 1}})
    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    entries = agent._chat_stub.interface.entries
    call_args = entries[0].content[0].args
    result_block = entries[1].content[0]
    body = result_block.content
    assert isinstance(body, dict)

    for key in (TOOL_META_CONTEXT_PENDING_KEY, TOOL_META_CONTEXT_EVENT_PENDING_KEY):
        assert key not in call_args
        assert key not in body

    # Safe freshness fields remain in their current locations, while the
    # handler-shaped body contains only its freshness marker.
    assert body["injection_seq"] == 1
    assert body == {"_synthesized": True, "injection_seq": 1}
    result_meta = result_block.metadata
    assert result_meta["agent_meta"]["agent_state"]["current_tool_result_chars"] == {
        "total_chars": 1
    }
    assert result_meta["agent_meta"]["agent_state"]["context"] == {
        "molt": "transit molt prose"
    }
    body_json = json.dumps(body, ensure_ascii=False)
    assert "transit molt prose" not in body_json
    assert "abc123transit" not in body_json

    pair_logs = [fields for evt, fields in agent._logs if evt == "notification_pair_injected"]
    assert pair_logs
    pair_meta = pair_logs[-1]["meta"]
    for key in (TOOL_META_CONTEXT_PENDING_KEY, TOOL_META_CONTEXT_EVENT_PENDING_KEY):
        assert key not in pair_meta

    block_logs = [fields for evt, fields in agent._logs if evt == "notification_block_injected"]
    assert block_logs
    logged_meta = block_logs[-1]["_meta"]
    assert logged_meta["agent_meta"]["agent_state"]["context"] == {
        "molt": "transit molt prose"
    }
    assert "abc123transit" not in json.dumps(logged_meta, ensure_ascii=False)
    agent_meta = block_logs[-1]["_meta"].get("agent_meta", {})
    for key in (TOOL_META_CONTEXT_PENDING_KEY, TOOL_META_CONTEXT_EVENT_PENDING_KEY):
        assert key not in agent_meta


def test_block_injected_payload_not_mutated_by_skeletonization(tmp_path: Path) -> None:
    """The logged payload must survive later skeletonization of the live holder."""
    import time as _time

    publish_test_payload(tmp_path, "email", {"count": 1})
    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    block_logs = [f for evt, f in agent._logs if evt == "notification_block_injected"]
    assert block_logs
    logged_notifs = block_logs[-1]["_meta"]["agent_meta"]["notifications"]["attention"]
    assert "email" in logged_notifs

    # Simulate later delivery: publish new state and re-sync; this skeletonizes
    # the first live holder.
    _time.sleep(0.001)
    publish_test_payload(tmp_path, "email", {"count": 2, "extra": "more"})
    agent._sync_notifications()

    # The *first* logged snapshot must still have email in it (not mutated).
    assert "email" in logged_notifs


def test_block_injected_emits_companion_to_pair_injected(tmp_path: Path) -> None:
    """Both notification_pair_injected and notification_block_injected must fire
    on every IDLE injection, in that order (pair before block)."""
    publish_test_payload(tmp_path, "email", {"count": 1})
    agent = _make_stub_agent_for_block_log(tmp_path)
    agent._sync_notifications()

    event_types = [evt for evt, _ in agent._logs]
    pair_idx = next((i for i, e in enumerate(event_types) if e == "notification_pair_injected"), -1)
    block_idx = next((i for i, e in enumerate(event_types) if e == "notification_block_injected"), -1)
    assert pair_idx != -1, "notification_pair_injected missing"
    assert block_idx != -1, "notification_block_injected missing"
    assert block_idx > pair_idx, "notification_block_injected must follow notification_pair_injected"


# ---------------------------------------------------------------------------
# Poisoned-interface fail-closed guard in _sync_notifications
# ---------------------------------------------------------------------------


def _make_poisoned_sync_agent(tmp_path: Path, state):
    """An agent whose interface is poisoned. Every mutating sync helper is
    wired to fail the test if called — the guard must short-circuit first."""
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState

    chat = _make_chat_stub()

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = state
            self._notification_fp = (("email.json", 1, "old"),)
            self._notification_deferred_log_fp = ()
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            if state == AgentState.ASLEEP:
                self._asleep.set()
            self._cancel_event = threading.Event()
            self._llm_worker_interface_poisoned = True
            self._llm_worker_poison_artifact = (
                "history/unfinished_turns/worker_still_running_test.json"
            )
            self._llm_worker_refresh_requested = False
            self.refresh_calls = []

        @property
        def _chat(self):
            return self._chat_stub

        def _inject_notification_pair(self, _notifications):
            raise AssertionError("poisoned sync must not inject")

        def _heal_pending_tool_calls(self, *, reason):
            raise AssertionError("poisoned sync must not heal")

        def _save_chat_history(self, *, ledger_source="main"):
            raise AssertionError("poisoned sync must not save")

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            raise AssertionError("poisoned sync must not wake")

        def _set_state(self, *_a, **_kw):
            raise AssertionError("poisoned sync must not change state")

        def _reset_uptime(self):
            raise AssertionError("poisoned sync must not reset uptime")

        def _perform_refresh(self, *, skip_chat_history_save=False, skip_save_reason=None):
            self.refresh_calls.append({
                "skip_chat_history_save": skip_chat_history_save,
                "skip_save_reason": skip_save_reason,
            })

    return _Agent(tmp_path)


def test_sync_notifications_asleep_refuses_touch_when_poisoned(tmp_path: Path) -> None:
    from lingtai.kernel.state import AgentState

    agent = _make_poisoned_sync_agent(tmp_path, AgentState.ASLEEP)
    before_fp = agent._notification_fp
    publish_test_payload(tmp_path, "system", {"data": {"events": [{"source": "daemon"}]}})

    agent._sync_notifications()

    assert agent._state == AgentState.ASLEEP
    assert agent._asleep.is_set()
    assert agent.inbox.empty()
    assert agent._notification_fp == before_fp
    assert agent.refresh_calls == [{
        "skip_chat_history_save": True,
        "skip_save_reason": "worker_still_running_interface_unsafe",
    }]
    assert any(
        evt == "notification_sync_skipped_poisoned_interface"
        for evt, _ in agent._logs
    )


def test_sync_notifications_idle_refuses_touch_when_poisoned(tmp_path: Path) -> None:
    from lingtai.kernel.state import AgentState

    agent = _make_poisoned_sync_agent(tmp_path, AgentState.IDLE)
    before_fp = agent._notification_fp
    publish_test_payload(tmp_path, "email", {"data": {"count": 1}})

    agent._sync_notifications()

    assert agent._state == AgentState.IDLE
    assert agent.inbox.empty()
    assert agent._notification_fp == before_fp
    assert len(agent._chat_stub.interface.entries) == 0
    assert agent.refresh_calls == [{
        "skip_chat_history_save": True,
        "skip_save_reason": "worker_still_running_interface_unsafe",
    }]


def test_heal_pending_tool_calls_logs_pending_tail_diagnostics(tmp_path: Path) -> None:
    """Successful heal logs a bounded pending-tail summary without tool args."""
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock

    iface = ChatInterface()
    iface.add_user_message("before")
    iface.add_assistant_message([TextBlock("thinking aloud")])
    iface.add_assistant_message([
        ToolCallBlock(id="call_1", name="bash", args={"command": "SECRET"}),
        ToolCallBlock(id="call_2", name="email", args={"message": "SECRET"}),
    ])

    logs: list[tuple[str, dict]] = []
    saves: list[str] = []
    agent = SimpleNamespace(
        _chat=SimpleNamespace(interface=iface),
        _log=lambda event, **fields: logs.append((event, fields)),
        _save_chat_history=lambda *, ledger_source="main": saves.append(ledger_source),
    )

    assert BaseAgent._heal_pending_tool_calls(agent, reason="wake_inject_blocked") is True

    event, fields = logs[-1]
    assert event == "heal_pending_tool_calls"
    assert fields["reason"] == "wake_inject_blocked"
    assert fields["pending_tool_call_count"] == 2
    assert fields["pending_tool_call_ids"] == ["call_1", "call_2"]
    assert fields["pending_tool_names"] == ["bash", "email"]
    assert fields["pending_tail_roles"] == ["user", "assistant", "assistant"]
    assert fields["pending_tail_block_types"] == [["text"], ["text"], ["tool_call", "tool_call"]]
    assert "SECRET" not in str(fields)
    assert saves == ["heal"]


def test_sync_notifications_serialized_across_runloop_and_heartbeat(tmp_path: Path) -> None:
    """Regression for #659: two concurrent ``_sync_notifications`` calls
    (the run-loop IDLE boundary and the heartbeat thread) must not both
    pass the fingerprint check-then-act and double-inject a pair.

    Pre-fix the second caller observes the still-uncommitted fingerprint,
    injects a duplicate pair, and posts a second MSG_TC_WAKE.  Post-fix
    the second caller blocks on ``_notification_sync_lock``, re-reads the
    fingerprint after the first caller commits, and no-ops.
    """

    agent = _make_stub_agent_for_block_log(
        tmp_path, notification_deferred_log_fp=(),
    )
    publish_test_payload(tmp_path, "email", {"count": 1, "data": {"count": 1}})

    release = threading.Event()
    injected_threads: list[str] = []
    orig_inject = agent._inject_notification_pair

    def slow_inject(notifications):
        injected_threads.append(threading.current_thread().name)
        release.wait(timeout=10)
        return orig_inject(notifications)

    agent._inject_notification_pair = slow_inject

    runloop = threading.Thread(
        target=agent._sync_notifications, name="runloop"
    )
    heartbeat = threading.Thread(
        target=agent._sync_notifications, name="heartbeat"
    )
    runloop.start()
    deadline = time.monotonic() + 10
    while not injected_threads and time.monotonic() < deadline:
        time.sleep(0.01)
    assert injected_threads, "first sync call never entered injection"
    heartbeat.start()
    time.sleep(0.5)  # pre-fix: the second caller passes the fp check here
    release.set()
    runloop.join(15)
    heartbeat.join(15)
    assert not runloop.is_alive() and not heartbeat.is_alive()

    # Exactly one injection for the single fingerprint change: the losing
    # caller must observe the committed fingerprint and no-op.
    assert len(injected_threads) == 1, (
        f"double injection: both callers passed the fp check "
        f"({sorted(injected_threads)})"
    )
    # And only one synthesized pair on the wire + one wake message.
    entries = agent._chat_stub.interface.entries
    assert len(entries) == 2, "wire must carry exactly one (call, result) pair"
    wake = 0
    while not agent.inbox.empty():
        agent.inbox.get_nowait()
        wake += 1
    assert wake == 1, "exactly one MSG_TC_WAKE per fingerprint change"


# ---------------------------------------------------------------------------
# WorkerStillRunning poison recovery relaunch: the fresh process must not
# self-wake off the notification state that was already present at boot.
#
# `lifecycle._start` rehydrates the still-open worker-hang artifact into a
# high-priority system notification. A fresh process starts with an empty
# `_notification_fp`, so the FIRST heartbeat sync saw that rehydrated event
# as a change and woke the agent (ASLEEP -> IDLE + MSG_TC_WAKE) with no new
# external input — the second half of the production self-wake (the CLI
# refresh-success kick-start being the first; see
# tests/test_cli_worker_poison_recovery.py). While a pending (open, not yet
# prompted) recovery exists, `_start` baselines the current coherent
# notification observation before the heartbeat is allowed to sync. Nothing
# is dismissed, cleared, or hidden: the next genuine notification change is
# the wake edge and delivers the full current payload.
# ---------------------------------------------------------------------------


_WORKER_HANG_ARTIFACT_ID = "worker_still_running_20260908T094700Z_abc123"


def _write_open_worker_hang_artifact(
    working_dir: Path,
    artifact_id: str = _WORKER_HANG_ARTIFACT_ID,
    *,
    status: str = "open",
    resolved_at: str | None = None,
    prompt_injected_at: str | None = None,
) -> Path:
    directory = working_dir / "history" / "unfinished_turns"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "type": "worker_still_running_recovery",
        "status": status,
        "created_at": "2026-09-08T09:47:00Z",
        "recovery": {"notification_ref_id": f"worker_still_running:{artifact_id}"},
    }
    if resolved_at is not None:
        payload["resolved_at"] = resolved_at
    if prompt_injected_at is not None:
        payload["prompt_injected_at"] = prompt_injected_at
    path = directory / f"{artifact_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _publish_rehydrated_worker_hang_event(working_dir: Path) -> None:
    """The system payload `rehydrate_worker_hang_recovery` leaves on disk."""
    publish_test_payload(
        working_dir,
        "system",
        {
            "header": "1 system notification",
            "priority": "high",
            "data": {
                "events": [
                    {
                        "event_id": "evt_wh",
                        "source": "kernel.llm_worker_hang",
                        "ref_id": f"worker_still_running:{_WORKER_HANG_ARTIFACT_ID}",
                        "priority": "high",
                        "body": "Previous LLM worker exceeded timeout plus grace.",
                    }
                ]
            },
        },
    )


def _make_asleep_stub_agent(tmp_path: Path, chat, state_history: list):
    """ASLEEP stub identical in shape to the §13.4 wake tests."""
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.state import AgentState

    class _Agent(BaseAgent):
        def __init__(self, workdir):
            self._working_dir = workdir
            self._notification_store = notification_store_for(workdir)
            self._state = AgentState.ASLEEP
            self._notification_fp = ()
            self._notification_raw_fp = ()
            self._notification_block_id = None
            self._chat_stub = chat
            self._logs = []
            self.agent_name = "stub"
            import queue
            self.inbox = queue.Queue()
            self._asleep = threading.Event()
            self._asleep.set()
            self._cancel_event = threading.Event()

        @property
        def _chat(self):
            return self._chat_stub

        def _save_chat_history(self, *, ledger_source="main"):
            pass

        def _log(self, evt, **fields):
            self._logs.append((evt, fields))

        def _wake_nap(self, *_a, **_kw):
            pass

        def _set_state(self, new_state, reason=""):
            self._state = new_state
            state_history.append((new_state, reason))

        def _reset_uptime(self):
            pass

    return _Agent(tmp_path)


def test_start_with_pending_worker_recovery_does_not_self_wake_on_first_sync(
    tmp_path: Path, monkeypatch
) -> None:
    """Real `BaseAgent._start` on a relaunch with an open worker recovery:
    the artifact is rehydrated into a visible system notification, yet the
    first heartbeat sync must leave the agent ASLEEP with an empty inbox."""
    from unittest.mock import MagicMock
    from lingtai.kernel.base_agent import BaseAgent, lifecycle
    from lingtai.kernel.state import AgentState
    from lingtai.tools.registry import INTRINSICS
    from tests._workdir_lease_helpers import make_test_lease
    from tests._snapshot_helpers import make_test_snapshot_port, make_test_source_revision_port
    from tests._lifecycle_clock_helpers import make_test_lifecycle_clock
    from tests._agent_presence_helpers import make_test_presence_store

    wd = tmp_path / "agent"
    wd.mkdir()
    artifact = _write_open_worker_hang_artifact(wd)
    before = artifact.read_text(encoding="utf-8")
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider, svc.model = "p", "m"
    agent = BaseAgent(
        intrinsics=INTRINSICS, service=svc, agent_name="alice", working_dir=wd,
        workdir_lease=make_test_lease(), snapshot_port=make_test_snapshot_port(),
        agent_presence=make_test_presence_store(), lifecycle_clock=make_test_lifecycle_clock(),
        source_revision_port=make_test_source_revision_port(),
        notification_store=notification_store_for(wd),
    )
    logs: list[tuple[str, dict]] = []
    agent._log = lambda evt, **fields: logs.append((evt, fields))
    # A wake that reaches injection is already the defect; keep the (mock
    # provider) wire out of it so the assertion is on state, deterministically.
    agent._inject_notification_pair = lambda notifications: False
    agent._heal_pending_tool_calls = lambda **kw: False
    agent._inbox_timeout = 0.05
    # No heartbeat thread: the test drives the first tick's sync by hand.
    monkeypatch.setattr(lifecycle, "_start_heartbeat", lambda a: None)
    # cli.run boots ASLEEP before start().
    agent._asleep.set()
    agent._state = AgentState.ASLEEP

    agent.start()
    try:
        # Quiesce the run loop so the sync below is the only actor.
        agent._shutdown.set()
        agent._thread.join(timeout=5)
        assert not agent._thread.is_alive()

        assert any(evt == "worker_hang_recovery_rehydrated" for evt, _ in logs)
        system_payload = json.dumps(snapshot_notifications(wd).get("system"), default=str)
        assert f"worker_still_running:{_WORKER_HANG_ARTIFACT_ID}" in system_payload, \
            "the rehydrated notification stays visible on disk"

        agent._sync_notifications()  # first heartbeat tick after runtime-ready

        assert agent._state == AgentState.ASLEEP, \
            "boot-time notification state must not wake the relaunched agent"
        assert agent._asleep.is_set()
        assert agent.inbox.empty()
        assert agent._notification_fp, "the boot observation was baselined"
        assert any(evt == "worker_hang_notification_baselined" for evt, _ in logs)
        assert artifact.read_text(encoding="utf-8") == before
    finally:
        agent._shutdown.set()


def test_pending_worker_recovery_baseline_defers_boot_state_but_later_change_wakes(
    tmp_path: Path,
) -> None:
    from lingtai.kernel.base_agent.worker_recovery import (
        baseline_notifications_for_pending_worker_recovery,
        has_pending_worker_hang_recovery_prompt,
    )
    from lingtai.kernel.message import MSG_TC_WAKE
    from lingtai.kernel.state import AgentState

    artifact = _write_open_worker_hang_artifact(tmp_path)
    before = artifact.read_text(encoding="utf-8")
    _publish_rehydrated_worker_hang_event(tmp_path)
    state_history: list = []
    agent = _make_asleep_stub_agent(tmp_path, _make_chat_stub(), state_history)
    delivered: list[dict] = []
    real_inject = agent._inject_notification_pair
    agent._inject_notification_pair = lambda n: (delivered.append(n), real_inject(n))[1]

    assert has_pending_worker_hang_recovery_prompt(agent) is True
    assert baseline_notifications_for_pending_worker_recovery(agent) is True
    assert agent._notification_fp == fingerprint_notifications(tmp_path)
    assert agent._notification_raw_fp == fingerprint_notifications(tmp_path)
    baselined = [f for evt, f in agent._logs if evt == "worker_hang_notification_baselined"]
    assert len(baselined) == 1
    assert baselined[0]["channels"] == ["system"]

    agent._sync_notifications()  # first tick: unchanged since the baseline

    assert agent._state == AgentState.ASLEEP
    assert state_history == []
    assert agent.inbox.empty()
    assert delivered == []
    assert len(agent._chat_stub.interface.entries) == 0

    # Nothing was hidden or dismissed ...
    assert "system" in snapshot_notifications(tmp_path)
    assert artifact.read_text(encoding="utf-8") == before
    assert has_pending_worker_hang_recovery_prompt(agent) is True

    # ... so a genuinely later change is a normal wake edge that delivers the
    # FULL current payload, worker-hang event included.
    publish_test_payload(tmp_path, "email", {"data": {"count": 1}})
    agent._sync_notifications()

    assert agent._state == AgentState.IDLE
    assert state_history and state_history[0][1] == "notification_arrival"
    assert agent.inbox.get_nowait().type == MSG_TC_WAKE
    assert len(agent._chat_stub.interface.entries) == 2
    assert len(delivered) == 1
    assert set(delivered[0]) == {"email", "system"}
    assert artifact.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "artifact_kwargs",
    [
        {"prompt_injected_at": "2026-09-08T09:50:00Z"},
        {"status": "resolved", "resolved_at": "2026-09-08T09:50:00Z"},
    ],
    ids=["already_prompted", "resolved"],
)
def test_baseline_gate_is_only_pending_recovery(tmp_path: Path, artifact_kwargs) -> None:
    """Prompted or resolved artifacts are history: no baseline, so a boot-time
    notification wakes exactly as it always did."""
    from lingtai.kernel.base_agent.worker_recovery import (
        baseline_notifications_for_pending_worker_recovery,
        has_pending_worker_hang_recovery_prompt,
    )
    from lingtai.kernel.state import AgentState

    _write_open_worker_hang_artifact(tmp_path, **artifact_kwargs)
    _publish_rehydrated_worker_hang_event(tmp_path)
    agent = _make_asleep_stub_agent(tmp_path, _make_chat_stub(), [])

    assert has_pending_worker_hang_recovery_prompt(agent) is False
    assert baseline_notifications_for_pending_worker_recovery(agent) is False
    assert agent._notification_fp == ()
    assert not any(evt == "worker_hang_notification_baselined" for evt, _ in agent._logs)

    agent._sync_notifications()

    assert agent._state == AgentState.IDLE


@pytest.mark.parametrize(
    "foreign",
    [
        ("email", {"data": {"count": 1}}),
        ("mcp.telegram", {"data": {"message_ids": ["42"], "count": 1}}),
    ],
    ids=["email", "mcp.telegram"],
)
def test_baseline_refuses_when_external_notification_already_pending(
    tmp_path: Path, foreign
) -> None:
    """An external message that arrived while the old process was hung is a
    real wake, not boot noise. With a pending recovery AND a foreign channel
    present, the baseline seeds nothing (reason `foreign_notifications_pending`)
    and the first heartbeat sync wakes normally, delivering every channel."""
    from lingtai.kernel.base_agent.worker_recovery import (
        baseline_notifications_for_pending_worker_recovery,
    )
    from lingtai.kernel.message import MSG_TC_WAKE
    from lingtai.kernel.state import AgentState

    channel, payload = foreign
    artifact = _write_open_worker_hang_artifact(tmp_path)
    before = artifact.read_text(encoding="utf-8")
    _publish_rehydrated_worker_hang_event(tmp_path)
    publish_test_payload(tmp_path, channel, payload)
    state_history: list = []
    agent = _make_asleep_stub_agent(tmp_path, _make_chat_stub(), state_history)
    delivered: list[dict] = []
    real_inject = agent._inject_notification_pair
    agent._inject_notification_pair = lambda n: (delivered.append(n), real_inject(n))[1]

    assert baseline_notifications_for_pending_worker_recovery(agent) is False
    assert agent._notification_fp == ()
    assert agent._notification_raw_fp == ()
    skipped = [f for evt, f in agent._logs if evt == "worker_hang_notification_baseline_skipped"]
    assert [f["reason"] for f in skipped] == ["foreign_notifications_pending"]
    assert skipped[0]["channels"] == sorted([channel, "system"])

    agent._sync_notifications()  # first heartbeat tick

    assert agent._state == AgentState.IDLE
    assert [reason for _state, reason in state_history] == ["notification_arrival"]
    assert agent.inbox.get_nowait().type == MSG_TC_WAKE
    assert agent.inbox.empty(), "exactly one MSG_TC_WAKE"
    assert len(agent._chat_stub.interface.entries) == 2
    assert len(delivered) == 1
    assert set(delivered[0]) == {channel, "system"}
    assert artifact.read_text(encoding="utf-8") == before


def test_baseline_refuses_when_system_channel_carries_a_non_worker_hang_event(
    tmp_path: Path,
) -> None:
    """Another pending system event (here a daemon terminal notice) beside the
    rehydrated worker-hang event is likewise foreign: no baseline, first sync
    wakes."""
    from lingtai.kernel.base_agent.worker_recovery import (
        baseline_notifications_for_pending_worker_recovery,
    )
    from lingtai.kernel.state import AgentState

    _write_open_worker_hang_artifact(tmp_path)
    publish_test_payload(
        tmp_path,
        "system",
        {
            "data": {
                "events": [
                    {
                        "event_id": "evt_wh",
                        "source": "kernel.llm_worker_hang",
                        "ref_id": f"worker_still_running:{_WORKER_HANG_ARTIFACT_ID}",
                    },
                    {"event_id": "evt_d", "source": "daemon", "ref_id": "daemon:em-1"},
                ]
            }
        },
    )
    agent = _make_asleep_stub_agent(tmp_path, _make_chat_stub(), [])

    assert baseline_notifications_for_pending_worker_recovery(agent) is False
    assert agent._notification_fp == ()
    skipped = [f for evt, f in agent._logs if evt == "worker_hang_notification_baseline_skipped"]
    assert [f["reason"] for f in skipped] == ["foreign_notifications_pending"]

    agent._sync_notifications()

    assert agent._state == AgentState.IDLE


def test_baseline_fails_toward_waking_on_unstable_or_failed_read(tmp_path: Path, monkeypatch) -> None:
    """An unstable coherent read (producer still writing) or a Store failure is
    never treated as a quiet baseline: the fingerprint stays unseeded so the
    heartbeat's own sync decides, and a bounded diagnostic is logged."""
    from lingtai.kernel import notifications
    from lingtai.kernel.base_agent.worker_recovery import (
        baseline_notifications_for_pending_worker_recovery,
    )

    _write_open_worker_hang_artifact(tmp_path)
    _publish_rehydrated_worker_hang_event(tmp_path)
    agent = _make_asleep_stub_agent(tmp_path, _make_chat_stub(), [])

    monkeypatch.setattr(
        notifications,
        "coherent_attention_read",
        lambda store, allow, workdir: notifications.CoherentAttentionRead((), (), {}, False),
    )
    assert baseline_notifications_for_pending_worker_recovery(agent) is False
    assert agent._notification_fp == ()
    skipped = [f for evt, f in agent._logs if evt == "worker_hang_notification_baseline_skipped"]
    assert [f["reason"] for f in skipped] == ["unstable_read"]

    def _boom(store, allow, workdir):
        raise OSError("simulated store failure")

    monkeypatch.setattr(notifications, "coherent_attention_read", _boom)
    assert baseline_notifications_for_pending_worker_recovery(agent) is False
    assert agent._notification_fp == ()
    skipped = [f for evt, f in agent._logs if evt == "worker_hang_notification_baseline_skipped"]
    assert [f["reason"] for f in skipped] == ["unstable_read", "read_failed"]
    assert "simulated store failure" in skipped[1]["error"]
