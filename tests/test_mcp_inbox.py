"""Tests for the LICC v1 contract and the mcp_inbox poller.

Covers: validation; one-shot scan dispatching valid events; dead-letter
of invalid events; .tmp file ignored; wake=false skipping _wake_nap;
multiple MCPs with separate subdirs.

The poller-as-thread is exercised lightly via start/stop and a manual
event injection; correctness is mostly proved by the synchronous _scan_once
path so we don't depend on timing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai.core.mcp.inbox import (
    DEAD_DIRNAME,
    INBOX_DIRNAME,
    MCPInboxPoller,
    POLL_INTERVAL,
    TMP_SUFFIX,
    _scan_once,
    validate_event,
)


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _mk_agent(tmp_path: Path):
    workdir = tmp_path / "agent"
    return Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"mcp": {}},
    ), workdir


def _write_event(workdir: Path, mcp_name: str, event_id: str, event: dict) -> Path:
    """Atomic LICC write — caller is the simulated MCP."""
    target_dir = workdir / INBOX_DIRNAME / mcp_name
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f"{event_id}{TMP_SUFFIX}"
    final = target_dir / f"{event_id}.json"
    tmp.write_text(json.dumps(event), encoding="utf-8")
    tmp.rename(final)
    return final


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_validator_accepts_minimal_event():
    ok, err = validate_event({"from": "alice", "subject": "hi", "body": "hello"})
    assert ok, err


def test_validator_accepts_full_event():
    ok, err = validate_event({
        "licc_version": 1,
        "from": "alice", "subject": "hi", "body": "hello",
        "metadata": {"k": "v"}, "wake": False,
        "received_at": "2026-04-29T12:00:00Z",
    })
    assert ok, err


def test_validator_rejects_missing_from():
    ok, err = validate_event({"subject": "hi", "body": "hello"})
    assert not ok and "from" in err


def test_validator_rejects_missing_subject():
    ok, err = validate_event({"from": "a", "body": "hello"})
    assert not ok and "subject" in err


def test_validator_rejects_missing_body():
    ok, err = validate_event({"from": "a", "subject": "hi"})
    assert not ok and "body" in err


def test_validator_rejects_long_subject():
    ok, err = validate_event({"from": "a", "subject": "x" * 250, "body": "b"})
    assert not ok and "subject too long" in err


def test_validator_rejects_unknown_version():
    ok, err = validate_event({"from": "a", "subject": "hi", "body": "b", "licc_version": 99})
    assert not ok and "licc_version" in err


def test_validator_rejects_non_bool_wake():
    ok, err = validate_event({"from": "a", "subject": "hi", "body": "b", "wake": "yes"})
    assert not ok and "wake" in err


def test_validator_rejects_non_dict_metadata():
    ok, err = validate_event({"from": "a", "subject": "hi", "body": "b", "metadata": "no"})
    assert not ok and "metadata" in err


# ---------------------------------------------------------------------------
# _scan_once dispatch
# ---------------------------------------------------------------------------

def test_scan_dispatches_valid_event_to_inbox(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "hi", "body": "hello",
    })

    inbox_root = workdir / INBOX_DIRNAME
    dispatched = _scan_once(agent, inbox_root)
    assert dispatched == 1

    # Message landed in agent inbox.
    assert agent.inbox.qsize() >= 1
    msg = agent.inbox.get_nowait()
    assert "telegram" in msg.content
    assert "alice" in msg.content
    assert "hi" in msg.content
    assert "hello" in msg.content

    # File was deleted on success.
    assert not (inbox_root / "telegram" / "ev1.json").exists()


def test_scan_calls_wake_nap_by_default(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "hi", "body": "hello",
    })

    with pytest.MonkeyPatch.context() as mp:
        called = []
        mp.setattr(agent, "_wake_nap", lambda reason: called.append(reason))
        _scan_once(agent, workdir / INBOX_DIRNAME)

    assert called == ["mcp_event"]


def test_scan_skips_wake_when_wake_false(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "hi", "body": "hello", "wake": False,
    })

    with pytest.MonkeyPatch.context() as mp:
        called = []
        mp.setattr(agent, "_wake_nap", lambda reason: called.append(reason))
        _scan_once(agent, workdir / INBOX_DIRNAME)

    assert called == []
    # But still delivered to inbox.
    assert agent.inbox.qsize() >= 1


def test_scan_dead_letters_invalid_json(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    target_dir = workdir / INBOX_DIRNAME / "telegram"
    target_dir.mkdir(parents=True)
    bad = target_dir / "bad.json"
    bad.write_text("not-json", encoding="utf-8")

    _scan_once(agent, workdir / INBOX_DIRNAME)

    # Original moved into .dead/ with sibling .error.json.
    assert not bad.exists()
    dead = target_dir / DEAD_DIRNAME / "bad.json"
    err = target_dir / DEAD_DIRNAME / "bad.error.json"
    assert dead.is_file()
    assert err.is_file()
    err_doc = json.loads(err.read_text())
    assert "invalid JSON" in err_doc["error"]


def test_scan_dead_letters_missing_required_field(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {"from": "a", "subject": "hi"})

    _scan_once(agent, workdir / INBOX_DIRNAME)

    dead_dir = workdir / INBOX_DIRNAME / "telegram" / DEAD_DIRNAME
    assert (dead_dir / "ev1.json").is_file()
    err = json.loads((dead_dir / "ev1.error.json").read_text())
    assert "body" in err["error"]


def test_scan_ignores_tmp_files(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    target_dir = workdir / INBOX_DIRNAME / "telegram"
    target_dir.mkdir(parents=True)
    half = target_dir / f"ev1{TMP_SUFFIX}"
    half.write_text(json.dumps({"from": "a", "subject": "s", "body": "b"}))

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 0
    assert half.exists()  # untouched


def test_scan_handles_multiple_mcps(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "t1", {
        "from": "a", "subject": "via tg", "body": "x",
    })
    _write_event(workdir, "feishu", "f1", {
        "from": "b", "subject": "via fs", "body": "y",
    })

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 2
    assert agent.inbox.qsize() == 2


def test_scan_skips_dotted_subdirs(tmp_path):
    """The .dead/ subdir contains processed-failed files; don't reprocess."""
    agent, workdir = _mk_agent(tmp_path)
    target_dir = workdir / INBOX_DIRNAME / "telegram" / DEAD_DIRNAME
    target_dir.mkdir(parents=True)
    (target_dir / "old.json").write_text(json.dumps({
        "from": "a", "subject": "s", "body": "b",
    }))

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 0  # .dead/ is skipped


# ---------------------------------------------------------------------------
# Poller lifecycle
# ---------------------------------------------------------------------------

def test_poller_start_creates_inbox_root(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    poller = MCPInboxPoller(agent)
    poller.start()
    try:
        assert (workdir / INBOX_DIRNAME).is_dir()
    finally:
        poller.stop()


def test_poller_dispatches_events_async(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    poller = MCPInboxPoller(agent)
    poller.start()
    try:
        _write_event(workdir, "telegram", "ev1", {
            "from": "alice", "subject": "async", "body": "hello",
        })
        # Wait up to 2 poll cycles for delivery.
        deadline = time.monotonic() + (POLL_INTERVAL * 4 + 0.5)
        while time.monotonic() < deadline and agent.inbox.qsize() == 0:
            time.sleep(0.05)
        assert agent.inbox.qsize() >= 1
    finally:
        poller.stop()


def test_poller_stop_idempotent(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    poller = MCPInboxPoller(agent)
    poller.start()
    poller.stop()
    poller.stop()  # second stop must not raise
