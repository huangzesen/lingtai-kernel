from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from lingtai.kernel import nudge as nudge_mod
from lingtai.kernel.notifications import dismiss_channel
from lingtai.kernel.nudge import ENTRY_CHANNEL_STORAGE_SIZE
from lingtai.kernel.nudge import event_journal_count
from tests._notification_store_helpers import notification_store_for, snapshot_notifications


class _Agent:
    def __init__(self, workdir: Path) -> None:
        self._working_dir = workdir
        self._notification_store = notification_store_for(workdir)
        self._notification_fp = ()
        self.logs: list[tuple[str, dict]] = []

    def _log(self, event: str, **fields) -> None:
        self.logs.append((event, fields))


def _entries(workdir: Path) -> list[dict]:
    return snapshot_notifications(workdir).get("nudge", {}).get("data", {}).get("nudges", [])


def _events_path(workdir: Path, data: bytes = b"seed\n") -> Path:
    path = workdir / "logs" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch) -> None:
    monkeypatch.setattr(event_journal_count, "_today_utc", lambda: "2026-08-16")


def test_exact_threshold_emits_human_discussion_advisory(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    path = _events_path(tmp_path)
    monkeypatch.setattr(
        event_journal_count,
        "_count_newline_records",
        lambda _: event_journal_count._THRESHOLD_RECORDS,
    )

    event_journal_count.check(agent)

    entry = _entries(tmp_path)[0]
    assert entry["kind"] == "event_journal_line_count"
    assert entry["nudge_channel"] == ENTRY_CHANNEL_STORAGE_SIZE
    assert entry["active_events_path"] == str(path)
    assert entry["threshold_records"] == event_journal_count._THRESHOLD_RECORDS
    assert entry["cadence"] == "once per UTC day for an unchanged file; immediate re-count after identity change or shrink"
    assert "Discuss handling with your human" in entry["detail"]
    for boundary in ("rename", "create a new file", "archive", "delete", "compress"):
        assert boundary in entry["detail"]


def test_under_threshold_clears_stale_finding(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    _events_path(tmp_path)
    nudge_mod.upsert(agent, "event_journal_line_count", {"title": "stale", "detail": "old"})
    monkeypatch.setattr(event_journal_count, "_count_newline_records", lambda _: 999_999)

    event_journal_count.check(agent)

    assert _entries(tmp_path) == []


def test_missing_file_is_quiet_no_finding(tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    nudge_mod.upsert(agent, "event_journal_line_count", {"title": "stale", "detail": "old"})

    event_journal_count.check(agent)

    assert _entries(tmp_path) == []


def test_unchanged_file_does_not_repeat_direct_count(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    path = _events_path(tmp_path)
    calls: list[int] = []

    def count(fd: int) -> int:
        calls.append(fd)
        return 0

    monkeypatch.setattr(event_journal_count, "_count_newline_records", count)
    event_journal_count.check(agent)
    event_journal_count.check(agent)

    assert len(calls) == 1
    assert path.exists()


def test_utc_date_rollover_recounts_unchanged_file(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    _events_path(tmp_path)
    day = ["2026-08-16"]
    calls: list[int] = []
    monkeypatch.setattr(event_journal_count, "_today_utc", lambda: day[0])
    monkeypatch.setattr(event_journal_count, "_count_newline_records", lambda fd: calls.append(fd) or 0)

    event_journal_count.check(agent)
    day[0] = "2026-08-17"
    event_journal_count.check(agent)

    assert len(calls) == 2


def test_same_size_replacement_recounts_immediately(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    path = _events_path(tmp_path, b"first\n")
    counts = iter([event_journal_count._THRESHOLD_RECORDS, 0])
    monkeypatch.setattr(event_journal_count, "_count_newline_records", lambda _: next(counts))

    event_journal_count.check(agent)
    assert len(_entries(tmp_path)) == 1
    replacement = path.with_name("replacement.jsonl")
    replacement.write_bytes(b"other\n")
    replacement.replace(path)
    event_journal_count.check(agent)

    assert _entries(tmp_path) == []


def test_shrink_recounts_same_day_and_clears_stale_observation(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    path = _events_path(tmp_path, b"a\n" * 20)
    counts = iter([event_journal_count._THRESHOLD_RECORDS, 0])
    monkeypatch.setattr(event_journal_count, "_count_newline_records", lambda _: next(counts))

    event_journal_count.check(agent)
    assert len(_entries(tmp_path)) == 1
    path.write_bytes(b"small\n")
    event_journal_count.check(agent)

    assert _entries(tmp_path) == []


def test_symlink_is_quiet_and_never_counts(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    path = _events_path(tmp_path)
    target = tmp_path / "outside.jsonl"
    target.write_bytes(b"outside\n")
    path.unlink()
    path.symlink_to(target)
    monkeypatch.setattr(event_journal_count, "_count_newline_records", lambda _: pytest.fail("must not count a link"))

    event_journal_count.check(agent)

    assert _entries(tmp_path) == []


def test_fifo_is_quiet_and_never_blocks_or_counts(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    path = _events_path(tmp_path)
    path.unlink()
    os.mkfifo(path)
    monkeypatch.setattr(event_journal_count, "_count_newline_records", lambda _: pytest.fail("must not count a FIFO"))

    event_journal_count.check(agent)

    assert _entries(tmp_path) == []


def test_unavailable_open_is_quiet_no_finding(monkeypatch, tmp_path: Path) -> None:
    agent = _Agent(tmp_path)
    _events_path(tmp_path)
    nudge_mod.upsert(agent, "event_journal_line_count", {"title": "stale", "detail": "old"})
    monkeypatch.setattr(event_journal_count, "_open_regular_event_file", lambda _: None)

    event_journal_count.check(agent)

    assert _entries(tmp_path) == []


def test_dismissal_racing_an_already_authorized_upsert_does_not_republish_same_fingerprint(
    monkeypatch, tmp_path: Path
) -> None:
    """Same-fingerprint dismissal/upsert TOCTOU race.

    Nothing about the finding changes: same kind, title, detail, source and
    ``nudge_channel``; same effective 24h policy; the persisted threshold
    observation is untouched. A heartbeat evaluation re-upserts that unchanged
    finding. Its upsert checks ``_dismissed_until`` (no dismissal yet), passes,
    and is then paused at the Nudge-owned store-update seam. Meanwhile the agent
    dismisses the visible finding through the real Notification path, which
    records a *future* dismissal for the exact same fingerprint and clears the
    channel. When the paused upsert resumes it must not republish the finding
    the agent just dismissed: the channel stays empty and the future dismissal
    record stays in force.
    """
    kind = "event_journal_line_count"
    agent = _Agent(tmp_path)
    _events_path(tmp_path)
    monkeypatch.setattr(
        event_journal_count,
        "_count_newline_records",
        lambda _: event_journal_count._THRESHOLD_RECORDS,
    )
    event_journal_count.check(agent)
    (displayed,) = _entries(tmp_path)
    assert displayed["kind"] == kind
    assert displayed["policy"]["repeat_after_dismiss"] == "24h"
    fingerprint = nudge_mod._finding_fingerprint(kind, displayed)
    state_path = tmp_path / ".notification" / ".nudge_state.json"

    reached_store_update = threading.Event()
    release_store_update = threading.Event()
    original_modify = nudge_mod._modify
    worker_ident: list[int] = []
    armed = [True]

    def paused_modify(agent_, mutate):
        # One-shot: pause only the worker's first (upsert) store update. Every
        # other call — including any from the main thread — delegates at once.
        if armed[0] and worker_ident and threading.get_ident() == worker_ident[0]:
            armed[0] = False
            reached_store_update.set()
            if not release_store_update.wait(timeout=10):
                raise RuntimeError("worker upsert was never released")
        return original_modify(agent_, mutate)

    monkeypatch.setattr(nudge_mod, "_modify", paused_modify)

    worker_errors: list[BaseException] = []

    def run_evaluation() -> None:
        worker_ident.append(threading.get_ident())
        try:
            event_journal_count.evaluate(agent)
        except BaseException as exc:  # propagated to the test thread below
            worker_errors.append(exc)

    worker = threading.Thread(target=run_evaluation, name="race-upsert")
    worker.start()
    try:
        assert reached_store_update.wait(timeout=10), "worker upsert never reached the store seam"

        # Worker has already passed its dismissal check; now the agent dismisses.
        result = dismiss_channel(agent, "nudge", invoked_by="notification", force=True)
        assert result["status"] == "ok"
        assert result["cleared"] is True
        assert _entries(tmp_path) == []
        state = json.loads(state_path.read_text(encoding="utf-8"))
        record = state["dismissed"][fingerprint]
        assert record["kind"] == kind
        assert record["until"] > time.time()
    finally:
        release_store_update.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    if worker_errors:
        raise worker_errors[0]

    state_after = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_after["dismissed"][fingerprint]["until"] > time.time()
    reappeared = [
        nudge_mod._finding_fingerprint(str(entry.get("kind") or ""), entry)
        for entry in _entries(tmp_path)
    ]
    assert reappeared == [], (
        "an already-authorized upsert republished the same fingerprint "
        f"{fingerprint} right after the agent dismissed it with a future "
        "repeat expiry still in force"
    )


def test_counter_uses_physical_newlines_without_json_interpretation(tmp_path: Path) -> None:
    path = _events_path(tmp_path, b"not-json\n{also-not-json}\nunterminated")
    opened = event_journal_count._open_regular_event_file(path)
    assert opened is not None
    fd, _ = opened
    try:
        assert event_journal_count._count_newline_records(fd) == 2
    finally:
        os.close(fd)
