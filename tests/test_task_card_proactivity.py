"""Proactivity mechanisms that keep a Task Card open when the work warrants one.

Three independent nudges are covered here: the daemon fleet nudge appended to
an `emanate` handoff, the continuation hint carried by the expired-watch
`task_card.limit` event, and the resident/tool wording that names which work is
card-worthy.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lingtai.tools.daemon import (
    DAEMON_ASYNC_HANDOFF,
    DAEMON_CARD_NUDGE_MIN_TASKS,
    DAEMON_CARD_NUDGE_MIN_TIMEOUT_S,
)
from lingtai.tools.task_card import TaskCardManager, get_description

from tests._daemon_helpers import make_daemon_agent
from tests.test_task_card_controller import _FakeAgent, _OK_BODY, _manager, _write_renderer


ROOT = Path(__file__).resolve().parents[1]
PROCEDURES = ROOT / "src/lingtai/prompts/procedures/procedures.md"
TASK_CARD_MANUAL = ROOT / "src/lingtai/tools/task_card/manual/SKILL.md"
TASK_CARD_LIFECYCLE = ROOT / "src/lingtai/tools/task_card/manual/reference/lifecycle.md"


class _StubCard:
    """Minimal duck type for the daemon's read-only active-watch probe."""

    def __init__(self, active: bool) -> None:
        self._active = active

    def has_active_watch(self) -> bool:
        return self._active


class _RaisingCard:
    def has_active_watch(self) -> bool:
        raise RuntimeError("probe exploded")


class _UnknownCard:
    def has_active_watch(self) -> None:
        return None


@pytest.fixture
def daemon(tmp_path):
    agent = make_daemon_agent(tmp_path)
    return agent, agent.get_capability("daemon")


def _start_watch(manager: TaskCardManager, workdir: Path, *, max_refreshes: int) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    return manager.handle(
        {
            "action": "start",
            "input": {
                "renderer_path": _write_renderer(workdir, _OK_BODY),
                "interval_s": 3600,
                "max_refreshes": max_refreshes,
            },
            "reasoning": "start a task card",
        }
    )


# --- Mechanism 1: daemon fleet nudge ---


def test_async_handoff_recommends_daemon_delay_for_large_concurrency():
    assert "For large concurrent batches, strongly recommend" in DAEMON_ASYNC_HANDOFF
    assert "notification(action='delay') on the daemon channel" in DAEMON_ASYNC_HANDOFF
    assert "delay masks attention only, never daemon truth" in DAEMON_ASYNC_HANDOFF


def test_fleet_dispatch_without_a_watch_is_nudged(daemon):
    agent, daemon = daemon
    agent._task_card_manager = _StubCard(False)
    handoff = daemon._emanate_handoff(4, None)
    assert handoff.startswith(DAEMON_ASYNC_HANDOFF)
    assert "You dispatched 4 daemon(s) with no active task_card watch" in handoff
    assert "task_card action='start'" in handoff


def test_single_quick_dispatch_is_not_nudged(daemon):
    agent, daemon = daemon
    agent._task_card_manager = _StubCard(False)
    assert daemon._emanate_handoff(1, 300.0) == DAEMON_ASYNC_HANDOFF


def test_single_dispatch_on_the_default_ceiling_is_not_nudged(daemon):
    """An omitted timeout must not qualify: the default ceiling is 3600s, so
    keying on it would nudge on every single quick daemon."""
    agent, daemon = daemon
    agent._task_card_manager = _StubCard(False)
    assert daemon._emanate_handoff(1, None) == DAEMON_ASYNC_HANDOFF


def test_single_explicitly_long_dispatch_is_nudged(daemon):
    agent, daemon = daemon
    agent._task_card_manager = _StubCard(False)
    handoff = daemon._emanate_handoff(1, DAEMON_CARD_NUDGE_MIN_TIMEOUT_S)
    assert "You dispatched 1 daemon(s) with no active task_card watch" in handoff


def test_active_watch_suppresses_the_nudge(daemon):
    agent, daemon = daemon
    agent._task_card_manager = _StubCard(True)
    assert daemon._emanate_handoff(4, 3600.0) == DAEMON_ASYNC_HANDOFF


def test_agent_without_the_task_card_capability_is_never_nudged(tmp_path):
    """`task_card` is a core default, but a host may disable it; a daemon
    dispatch on such an agent must fall back to the plain handoff."""
    agent = make_daemon_agent(tmp_path)
    del agent._task_card_manager
    assert agent.get_capability("daemon")._emanate_handoff(4, None) == DAEMON_ASYNC_HANDOFF


def test_probe_failure_falls_back_to_the_plain_handoff(daemon):
    agent, daemon = daemon
    agent._task_card_manager = _RaisingCard()
    assert daemon._emanate_handoff(4, 3600.0) == DAEMON_ASYNC_HANDOFF


def test_unknown_probe_state_falls_back_to_the_plain_handoff(daemon):
    agent, daemon = daemon
    agent._task_card_manager = _UnknownCard()
    assert daemon._emanate_handoff(4, 3600.0) == DAEMON_ASYNC_HANDOFF


def test_nudge_thresholds_are_fleet_scale():
    assert DAEMON_CARD_NUDGE_MIN_TASKS == 2
    assert DAEMON_CARD_NUDGE_MIN_TIMEOUT_S == 900.0


def test_real_task_card_capability_toggles_the_nudge(tmp_path):
    """End-to-end across the two capabilities: a live watch silences the nudge
    and retiring it brings the nudge back."""
    agent = make_daemon_agent(tmp_path, ["daemon", "task_card"])
    daemon = agent.get_capability("daemon")
    card = agent.get_capability("task_card")
    assert card is agent._task_card_manager
    assert not card.has_active_watch()
    assert "no active task_card watch" in daemon._emanate_handoff(2, None)

    started = _start_watch(card, Path(agent._working_dir), max_refreshes=10)
    assert started["status"] == "ok"
    assert card.has_active_watch()
    assert daemon._emanate_handoff(2, None) == DAEMON_ASYNC_HANDOFF

    card.handle(
        {
            "action": "stop",
            "input": {"watch_id": started["watch_id"]},
            "reasoning": "cleanup",
        }
    )
    assert not card.has_active_watch()
    assert "no active task_card watch" in daemon._emanate_handoff(2, None)


# --- Mechanism 2: expired-watch continuation hint ---


def test_expired_watch_event_asks_for_a_new_watch(tmp_path):
    agent = _FakeAgent(tmp_path)
    manager = _manager(agent)
    assert _start_watch(manager, tmp_path, max_refreshes=1)["status"] == "ok"
    watch = manager._watch
    assert watch is not None

    manager._tick(watch)

    limit_wakes = [wake for wake in agent.wakes if wake["source"] == "task_card.limit"]
    assert len(limit_wakes) == 1
    body = limit_wakes[0]["body"]
    assert "reached its refresh limit" in body
    assert "If this work is still ongoing, start a new watch (task_card action='start')" in body
    assert "do not let the card go dark mid-task" in body


def test_exhausted_watch_reports_itself_inactive(tmp_path):
    """The nudge is only useful if an exhausted watch stops counting as active."""
    agent = _FakeAgent(tmp_path)
    manager = _manager(agent)
    assert _start_watch(manager, tmp_path, max_refreshes=1)["status"] == "ok"
    assert manager.has_active_watch()
    manager._tick(manager._watch)
    assert not manager.has_active_watch()


# --- Mechanism 3: resident + tool trigger wording ---


def test_tool_description_asks_for_a_restart_after_expiry():
    assert "Restart after expiry" in get_description()


def test_resident_procedures_routes_to_task_card_manual():
    """Resident procedures keeps only the must-happen actions and routes detail
    to the task_card manual, which now owns the concrete trigger examples."""
    text = PROCEDURES.read_text(encoding="utf-8")
    assert "Use a truthful Task Card for" in text
    assert "`daemon`, `avatar`, `shell`, or `task_card` manual" in text


def test_task_card_lifecycle_reference_names_the_card_worthy_triggers():
    text = TASK_CARD_LIFECYCLE.read_text(encoding="utf-8")
    section = text.split("## When to use it", 1)[1].split("\n## ", 1)[0]
    for fragment in (
        "multi-daemon fleets",
        "multi-PR batches",
        "review→merge",
        "ten minutes",
        "`max_refreshes`",
        "go dark",
    ):
        assert fragment in section, f"{fragment!r} missing from task_card manual's When to use it"
