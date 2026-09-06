"""Focused coverage for the intrinsic declarative ``task_card`` capability."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lingtai.adapters.tool_plugin_host import AgentTaskCardNotificationsAdapter
from lingtai.kernel import notifications
from lingtai.kernel.tool_plugin import ToolPluginHost
from lingtai.tools.task_card import DECLARATION, TaskCardManager, get_description, get_schema


class _FakeAgent:
    def __init__(self, working_dir: Path) -> None:
        self._working_dir = working_dir
        self._shutdown = threading.Event()
        self.wakes: list[dict] = []
        self.added_tools: list[tuple] = []

    def _enqueue_system_notification(self, **kwargs):
        self.wakes.append(kwargs)
        return "notif-id"

    def add_tool(
        self,
        name,
        *,
        schema=None,
        handler=None,
        description="",
        glossary_package="__unset__",
        **_,
    ):
        self.added_tools.append((name, schema, handler, description, glossary_package))


class _Lifecycle:
    def __init__(self, agent: _FakeAgent) -> None:
        self._agent = agent

    def current_manager(self):
        return getattr(self._agent, "_task_card_manager", None)

    def retain_manager(self, manager) -> None:
        self._agent._task_card_manager = manager

    def report_resume_failure(self, error: str) -> None:
        self._agent.resume_errors = getattr(self._agent, "resume_errors", []) + [error]


def _task_card_notifications(agent: _FakeAgent) -> AgentTaskCardNotificationsAdapter:
    """The production operation-native port over the fake agent's publisher.

    The wire assertions below therefore exercise the real adapter's pinned
    source/channel/priority/idempotency/extra projection, not a test double.
    """

    def _submit(turns: int) -> None:
        notifications.submit(
            agent,
            "task_card",
            data={"source": "task_card.reminder", "turns": turns},
            header="Task Card reminder",
            instructions="Check whether the Task Card is absent or stale; update or issue one only if useful.",
        )

    return AgentTaskCardNotificationsAdapter(
        agent._enqueue_system_notification,
        _submit,
        lambda: notifications.clear(agent, "task_card"),
    )


def _task_card_host(agent: _FakeAgent) -> ToolPluginHost:
    return ToolPluginHost.grant(
        DECLARATION,
        {
            "workdir": SimpleNamespace(path=agent._working_dir),
            "shutdown": agent._shutdown,
            "task_card_lifecycle": _Lifecycle(agent),
            "task_card_notifications": _task_card_notifications(agent),
        },
    )


def _manager(agent: _FakeAgent) -> TaskCardManager:
    return TaskCardManager(_task_card_host(agent))


def _write_renderer(workdir: Path, body: str, name: str = "renderer.py") -> str:
    path = workdir / name
    path.write_text(body, encoding="utf-8")
    return str(path)


_OK_BODY = "print('# Task Card\\n\\n- first\\n- second')"


@pytest.fixture
def agent(tmp_path):
    return _FakeAgent(tmp_path)


@pytest.fixture
def manager(agent):
    value = _manager(agent)
    yield value
    value.shutdown_for_agent_stop()


def test_declaration_binds_the_intrinsic_task_card_tool_through_narrow_ports(agent):
    host = _task_card_host(agent)
    bound = DECLARATION.bind(host)
    mgr = agent._task_card_manager
    assert isinstance(mgr, TaskCardManager)
    assert bound.name == "task_card"
    assert bound.schema == get_schema()
    assert bound.description == get_description()
    assert callable(bound.handler)
    assert DECLARATION.requires == (
        "workdir", "shutdown", "task_card_lifecycle", "task_card_notifications"
    )


def test_description_routes_to_manual_and_file_contract():
    desc = get_description()
    assert "taskcard/status" in desc
    assert "taskcard/taskcard.md" in desc
    assert "manual" in desc.lower()


def test_description_manual_and_contract_encourage_proactive_use():
    """The schema, short router, and Contract Behavior all teach when to (not) use it."""
    use_fragments = ["long-running", "multi-step", "parallel"]
    schema_safety_fragments = ["ritual", "truthful"]
    manual_safety_fragments = ["single-step", "ritual", "truthful"]

    desc = get_description()
    for fragment in use_fragments + schema_safety_fragments:
        assert fragment in desc, f"{fragment!r} missing from get_description()"

    root = Path(__file__).resolve().parents[1]
    manual_body = (root / "src/lingtai/tools/task_card/manual/SKILL.md").read_text(
        encoding="utf-8"
    )
    for fragment in use_fragments + manual_safety_fragments:
        assert fragment in manual_body, f"{fragment!r} missing from manual/SKILL.md"

    contract_body = (root / "src/lingtai/tools/task_card/CONTRACT.md").read_text(
        encoding="utf-8"
    )
    for fragment in use_fragments + manual_safety_fragments:
        assert fragment in contract_body, f"{fragment!r} missing from CONTRACT.md"


def test_manual_routes_focused_references_and_keeps_first_call_guard():
    root = Path(__file__).resolve().parents[1]
    manual_path = root / "src/lingtai/tools/task_card/manual/SKILL.md"
    manual = manual_path.read_text(encoding="utf-8")
    for relative in (
        "reference/lifecycle.md",
        "reference/settings.md",
        "reference/notifications.md",
    ):
        reference = manual_path.parent / relative
        assert reference.is_file(), relative
        assert f"]({relative})" in manual, f"manual does not route to {relative}"
        assert reference.read_text(encoding="utf-8").startswith("---")

    description = get_description().lower()
    for fragment in (
        "renderer",
        "working directory",
        "non-empty stdout",
        "taskcard/taskcard.md",
        "taskcard/status",
        "one watch",
        "stop",
        "remove",
    ):
        assert fragment in description, fragment
    start_input = get_schema()["properties"]["input"]["anyOf"][0]
    assert "renderer_path" in start_input["required"]
    assert len(manual) < 6000


def test_start_writes_body_before_active_and_reports_exact_paths(agent, manager, monkeypatch):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    order: list[str] = []
    real_write_body = manager._write_body
    real_write_status = manager._write_status

    def record_body(body: str) -> None:
        order.append("body")
        real_write_body(body)

    def record_status(value: str) -> None:
        order.append(f"status:{value}")
        real_write_status(value)

    monkeypatch.setattr(manager, "_write_body", record_body)
    monkeypatch.setattr(manager, "_write_status", record_status)

    result = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )

    assert result["status"] == "ok"
    assert result["state"] == "watching"
    assert result["status_value"] == "active"
    assert order == ["body", "status:active"]
    assert Path(result["status_path"]).read_text(encoding="utf-8") == "active"
    assert Path(result["body_path"]).read_text(encoding="utf-8") == "# Task Card\n\n- first\n- second\n"
    manager.handle({"action": "stop", "input": {"watch_id": result["watch_id"]}, "reasoning": "cleanup"})


def test_start_persists_watch_descriptor_for_restart_resume(agent, manager):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch_path = Path(started["watch_path"])
    assert watch_path.is_file()
    payload = json.loads(watch_path.read_text(encoding="utf-8"))
    assert payload["watch_id"] == started["watch_id"]
    # The renderer path is persisted relative to the working directory so a
    # relocated workdir can still resume the watch.
    assert payload["renderer_path"] == "renderer.py"
    assert payload["interval_s"] == 3600
    assert payload["max_refreshes"] == 2000
    assert payload["refreshes_used"] == 0
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "cleanup"})
    assert not watch_path.exists()


def test_resume_persisted_watch_after_shutdown_rehydrates_watch(agent, manager):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch_id = started["watch_id"]
    watch_path = Path(started["watch_path"])
    assert watch_path.is_file()

    # Simulate a process restart: shutdown stops the thread but keeps the
    # descriptor; a fresh manager rehydrates the watch on setup.
    manager.shutdown_for_agent_stop(reason="agent_stop")
    assert manager._watch is None
    assert watch_path.is_file()
    assert Path(started["status_path"]).read_text(encoding="utf-8") == "inactive"

    fresh = _manager(agent)
    resumed = fresh.resume_persisted_watch()
    assert resumed is not None
    assert resumed["status"] == "ok"
    assert resumed["resumed"] is True
    assert resumed["watch_id"] == watch_id
    assert resumed["state"] == "watching"
    assert resumed["status_value"] == "active"
    assert fresh._watch is not None
    assert fresh._watch.watch_id == watch_id
    assert fresh._watch.thread is not None and fresh._watch.thread.is_alive()
    assert Path(started["status_path"]).read_text(encoding="utf-8") == "active"
    assert Path(started["body_path"]).read_text(encoding="utf-8") == "# Task Card\n\n- first\n- second\n"
    fresh.shutdown_for_agent_stop()


def test_resume_persisted_watch_is_idempotent_with_active_watch(agent, manager):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    # A second resume while the watch is live is a no-op, not a duplicate.
    assert manager.resume_persisted_watch() is None
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "cleanup"})


def test_resume_missing_or_corrupt_descriptor_is_noop(agent, manager):
    assert manager.resume_persisted_watch() is None
    watch_path = manager._taskcard_dir / "watch.json"
    watch_path.parent.mkdir(parents=True, exist_ok=True)
    watch_path.write_text("{not json", encoding="utf-8")
    assert manager.resume_persisted_watch() is None
    # The contract promises stale descriptors are cleared, not left to wedge
    # every future boot (CONTRACT Behavior rule 14).
    assert not watch_path.exists(), "corrupt descriptor must be cleared on boot"
    # A valid-JSON but empty descriptor is equally stale.
    watch_path.write_text("{}", encoding="utf-8")
    assert manager.resume_persisted_watch() is None
    assert not watch_path.exists(), "empty descriptor must be cleared on boot"


def test_resume_clears_descriptor_when_renderer_no_longer_exists(agent, manager):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch_path = Path(started["watch_path"])
    Path(renderer).unlink()
    manager.shutdown_for_agent_stop(reason="agent_stop")

    fresh = _manager(agent)
    assert fresh.resume_persisted_watch() is None
    assert not watch_path.exists()
    assert fresh._watch is None
    assert Path(started["status_path"]).read_text(encoding="utf-8") == "inactive"


def test_resume_respects_carried_refresh_budget(agent, manager):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600, "max_refreshes": 3},
            "reasoning": "publish a task card",
        }
    )
    watch = manager._watch
    assert watch is not None
    watch.refreshes_used = 3  # budget exhausted
    manager.shutdown_for_agent_stop(reason="agent_stop")

    fresh = _manager(agent)
    assert fresh.resume_persisted_watch() is None
    assert not Path(started["watch_path"]).exists()
    assert fresh._watch is None


def test_resume_renderer_failure_writes_active_and_preserves_body(agent, manager):
    """A transient renderer failure at resume keeps the card visible.

    CONTRACT Behavior rule 14: resume marks ``active`` unconditionally, keeps
    the last body on disk, and lets the updater thread retry; a later
    successful tick must not leave the card stuck ``inactive``.
    """
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    status_path = Path(started["status_path"])
    body_path = Path(started["body_path"])
    manager.shutdown_for_agent_stop(reason="agent_stop")
    assert status_path.read_text(encoding="utf-8") == "inactive"
    assert body_path.read_text(encoding="utf-8") == "# Task Card\n\n- first\n- second\n"

    # The renderer now fails transiently (nonzero exit); the last body stays
    # on disk, exactly the case that used to leave status stuck inactive.
    Path(renderer).write_text("import sys; sys.exit(3)", encoding="utf-8")
    fresh = _manager(agent)
    resumed = fresh.resume_persisted_watch()
    try:
        assert resumed is not None
        assert resumed["status_value"] == "active"
        assert fresh._watch is not None
        assert fresh._watch.last_valid_body == "# Task Card\n\n- first\n- second\n"
        assert fresh._watch.last_valid_at is not None
        assert status_path.read_text(encoding="utf-8") == "active"
        assert body_path.read_text(encoding="utf-8") == "# Task Card\n\n- first\n- second\n"

        # Renderer recovers: a later successful tick keeps the card active.
        Path(renderer).write_text(_OK_BODY, encoding="utf-8")
        fresh._tick(fresh._watch)
        assert fresh._watch.error is None
        assert status_path.read_text(encoding="utf-8") == "active"
        assert fresh._watch.last_valid_body == "# Task Card\n\n- first\n- second\n"
    finally:
        fresh.shutdown_for_agent_stop()


def test_resume_carries_partial_refresh_budget_into_live_watch(agent, manager):
    """A partial refresh budget survives shutdown and a live resume."""
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600, "max_refreshes": 5},
            "reasoning": "publish a task card",
        }
    )
    watch = manager._watch
    assert watch is not None
    watch.refreshes_used = 3  # partial budget: 2 refreshes remain
    manager.shutdown_for_agent_stop(reason="agent_stop")
    payload = json.loads(Path(started["watch_path"]).read_text(encoding="utf-8"))
    assert payload["refreshes_used"] == 3

    fresh = _manager(agent)
    resumed = fresh.resume_persisted_watch()
    try:
        assert resumed is not None
        assert resumed["refreshes_used"] == 3
        assert resumed["max_refreshes"] == 5
        assert resumed["refreshes_remaining"] == 2
        assert fresh._watch is not None
        assert fresh._watch.refreshes_used == 3
        assert fresh._watch.max_refreshes == 5
    finally:
        fresh.shutdown_for_agent_stop()


def test_declaration_activation_resumes_a_persisted_watch_on_boot(agent, manager):
    """The fresh declaration bind/activation rehydrates a persisted watch."""
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch_path = Path(started["watch_path"])
    assert watch_path.is_file()
    manager.shutdown_for_agent_stop(reason="agent_stop")

    fresh_agent = _FakeAgent(agent._working_dir)
    bound = DECLARATION.bind(_task_card_host(fresh_agent))
    assert bound.activate is not None
    bound.activate()
    mgr = fresh_agent._task_card_manager
    try:
        assert isinstance(mgr, TaskCardManager)
        assert mgr._watch is not None
        assert mgr._watch.watch_id == started["watch_id"]
        assert mgr._watch.thread is not None and mgr._watch.thread.is_alive()
        assert Path(started["status_path"]).read_text(encoding="utf-8") == "active"
        assert callable(bound.handler)
    finally:
        mgr.shutdown_for_agent_stop()


def test_stop_clears_persisted_descriptor(agent, manager):
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY), "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch_path = Path(started["watch_path"])
    assert watch_path.exists()
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "deactivate"})
    assert not watch_path.exists()
    assert manager.resume_persisted_watch() is None


def test_remove_clears_persisted_descriptor(agent, manager):
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY), "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch_path = Path(started["watch_path"])
    assert watch_path.exists()
    manager.handle({"action": "remove", "input": {}, "reasoning": "work is done"})
    assert not watch_path.exists()
    assert manager.resume_persisted_watch() is None


def test_retry_replaces_only_the_body_and_preserves_active_status(agent, manager, monkeypatch):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    Path(renderer).write_text("print('# Updated\\n\\n- replacement')", encoding="utf-8")
    writes: list[str] = []
    real_write_body = manager._write_body
    real_write_status = manager._write_status

    def record_body(body: str) -> None:
        writes.append("body")
        real_write_body(body)

    def record_status(value: str) -> None:
        writes.append(f"status:{value}")
        real_write_status(value)

    monkeypatch.setattr(manager, "_write_body", record_body)
    monkeypatch.setattr(manager, "_write_status", record_status)

    result = manager.handle(
        {
            "action": "retry",
            "input": {"watch_id": started["watch_id"]},
            "reasoning": "refresh now",
        }
    )

    assert result["status"] == "ok"
    assert result["last_valid_body"] == "# Updated\n\n- replacement\n"
    assert Path(result["status_path"]).read_text(encoding="utf-8") == "active"
    assert Path(result["body_path"]).read_text(encoding="utf-8") == "# Updated\n\n- replacement\n"
    assert writes == ["body"]
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "cleanup"})


def test_stop_writes_inactive_before_removing_the_watch(agent, manager, monkeypatch):
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY), "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    writes: list[str] = []
    real_write_status = manager._write_status

    def record_status(value: str) -> None:
        writes.append(value)
        real_write_status(value)

    monkeypatch.setattr(manager, "_write_status", record_status)
    result = manager.handle(
        {
            "action": "stop",
            "input": {"watch_id": started["watch_id"]},
            "reasoning": "deactivate",
        }
    )

    assert result["status"] == "ok"
    assert result["state"] == "stopped"
    assert result["status_value"] == "inactive"
    assert Path(result["status_path"]).read_text(encoding="utf-8") == "inactive"
    assert writes == ["inactive"]


def test_remove_after_stop_deletes_the_preserved_body(agent, manager):
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY), "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "pause"})
    assert Path(started["body_path"]).exists()

    result = manager.handle({"action": "remove", "input": {}, "reasoning": "work is done"})

    assert result["status"] == "ok"
    assert result["state"] == "removed"
    assert result["body_removed"] is True
    assert result["status_value"] == "inactive"
    assert not Path(result["body_path"]).exists()
    assert Path(result["status_path"]).read_text(encoding="utf-8") == "inactive"
    assert manager._watch is None


def test_remove_while_active_retires_the_watch_before_deleting_the_body(agent, manager, monkeypatch):
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY), "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch = manager._watch
    assert watch.thread is not None and watch.thread.is_alive()

    order: list[str] = []
    real_stop_watch = manager._stop_watch
    real_delete_body = manager._delete_body

    def record_stop(w):
        order.append("stop")
        return real_stop_watch(w)

    def record_delete():
        order.append("delete")
        return real_delete_body()

    monkeypatch.setattr(manager, "_stop_watch", record_stop)
    monkeypatch.setattr(manager, "_delete_body", record_delete)

    result = manager.handle({"action": "remove", "input": {}, "reasoning": "abandon this work"})

    assert order == ["stop", "delete"]
    assert result["status"] == "ok"
    assert result["state"] == "removed"
    assert result["body_removed"] is True
    assert not Path(started["body_path"]).exists()
    assert Path(started["status_path"]).read_text(encoding="utf-8") == "inactive"
    assert manager._watch is None
    # The updater thread is fully joined before the body is unlinked, so it
    # cannot race a deleted body back into existence.
    assert not watch.thread.is_alive()


def test_remove_blocks_when_the_watch_will_not_stop_and_does_not_delete_the_body(
    agent, manager, monkeypatch
):
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY), "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch = manager._watch
    stop_failure = {
        "status": "error",
        "watch_id": watch.watch_id,
        "state": "stop_failed",
        "error": {"code": "stop_thread_alive", "retryable": True, "message": "still running"},
        **manager._paths_payload(),
        **manager._status_payload("inactive"),
        **manager._refresh_fields(watch),
    }
    delete_calls: list[str] = []
    monkeypatch.setattr(manager, "_stop_watch", lambda w: stop_failure)
    monkeypatch.setattr(manager, "_delete_body", lambda: delete_calls.append("delete"))

    result = manager.handle({"action": "remove", "input": {}, "reasoning": "abandon this work"})

    assert result["status"] == "error"
    assert result["state"] == "remove_blocked"
    assert result["error"]["code"] == "stop_thread_alive"
    assert delete_calls == []
    assert Path(started["body_path"]).exists()
    # ``stop``'s own retryable contract still applies: the watch is retained
    # so a later remove can retry once the updater actually quiesces.
    assert manager._watch is watch

    monkeypatch.undo()
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "cleanup"})


def test_remove_is_idempotent_when_never_started_or_already_removed(agent, manager):
    never_started = manager.handle({"action": "remove", "input": {}, "reasoning": "cleanup just in case"})

    assert never_started["status"] == "ok"
    assert never_started["state"] == "removed"
    assert never_started["body_removed"] is False
    assert never_started["status_value"] == "inactive"
    assert Path(never_started["status_path"]).read_text(encoding="utf-8") == "inactive"
    assert not Path(never_started["body_path"]).exists()

    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    manager.handle(
        {"action": "start", "input": {"renderer_path": renderer, "interval_s": 3600}, "reasoning": "start"}
    )
    first = manager.handle({"action": "remove", "input": {}, "reasoning": "cleanup"})
    assert first["body_removed"] is True

    second = manager.handle({"action": "remove", "input": {}, "reasoning": "cleanup again"})
    assert second["status"] == "ok"
    assert second["state"] == "removed"
    assert second["body_removed"] is False
    assert manager._watch is None


def test_remove_rejects_unknown_input_fields(agent, manager):
    result = manager.handle(
        {
            "action": "remove",
            "input": {"watch_id": "tc_1"},
            "reasoning": "probe schema",
        }
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_only_one_watch_may_be_active_per_agent(agent, manager):
    renderer = _write_renderer(agent._working_dir, _OK_BODY)
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "first watch",
        }
    )
    blocked = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": renderer, "interval_s": 3600},
            "reasoning": "second watch",
        }
    )

    assert started["status"] == "ok"
    assert blocked["status"] == "failed"
    assert "only one Task Card watch" in blocked["message"]
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "cleanup"})


def test_start_rejects_renderer_path_outside_workdir(agent, manager):
    result = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": "../../etc/passwd"},
            "reasoning": "probe path escape",
        }
    )
    assert result["status"] == "failed"
    assert "working directory" in result["message"]


@pytest.mark.parametrize(
    "body,kwargs",
    [
        ("pass", {}),
        ("import sys; sys.exit(3)", {}),
        ("import time; time.sleep(5)", {"timeout_s": 0.2}),
    ],
)
def test_start_failures_create_no_watch(agent, manager, body, kwargs):
    result = manager.handle(
        {
            "action": "start",
            "input": {
                "renderer_path": _write_renderer(agent._working_dir, body, "bad.py"),
                **kwargs,
            },
            "reasoning": "probe failure",
        }
    )
    assert result["status"] == "failed"
    assert manager._watch is None


def test_renderer_failure_preserves_last_valid_body_and_emits_error_then_recovery(agent, manager):
    renderer = Path(_write_renderer(agent._working_dir, _OK_BODY))
    started = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": str(renderer), "interval_s": 3600},
            "reasoning": "publish a task card",
        }
    )
    watch = manager._watch
    assert watch is not None

    renderer.write_text("import sys; sys.exit(1)", encoding="utf-8")
    manager._tick(watch)
    inspected = manager.handle(
        {
            "action": "inspect",
            "input": {"watch_id": started["watch_id"]},
            "reasoning": "inspect failure",
        }
    )
    assert inspected["state"] == "error"
    assert inspected["last_valid_body"] == "# Task Card\n\n- first\n- second\n"
    assert any(wake["source"] == "task_card.error" for wake in agent.wakes)

    renderer.write_text("print('# Recovered')", encoding="utf-8")
    manager._tick(watch)
    assert any(wake["source"] == "task_card.error" and wake["extra"]["state"] == "recovered" for wake in agent.wakes)
    manager.handle({"action": "stop", "input": {"watch_id": started["watch_id"]}, "reasoning": "cleanup"})


# -- persisted agent-wide config: <workdir>/taskcard/taskcard.json ---------


def _write_config(agent, payload: dict) -> Path:
    path = agent._working_dir / "taskcard" / "taskcard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_legacy_telegram_config(agent, payload: dict) -> Path:
    path = agent._working_dir / "telegram" / "taskcard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_config_paths_are_exact_and_agent_local(agent, manager):
    assert manager._config_path == agent._working_dir / "taskcard" / "taskcard.json"
    assert manager._legacy_config_path == agent._working_dir / "telegram" / "taskcard.json"


_SETTING_DEFAULTS = {
    "interval_s": 5.0,
    "timeout_s": 10.0,
    "max_refreshes": 2000,
    "reminder_turns": 10,
    "max_body_chars": 2000,
}
_SETTING_COMMENTS = {
    key: f"task_card-manual#{key.replace('_', '-')}" for key in _SETTING_DEFAULTS
}


def _show_settings(manager: TaskCardManager, input_: dict | None = None) -> dict:
    return manager.handle(
        {
            "action": "settings",
            "input": {} if input_ is None else input_,
            "reasoning": "inspect Task Card owner policy",
        }
    )


def test_settings_inventory_is_exact_ordered_and_read_only_by_default(agent, manager):
    assert DECLARATION.settings is True
    assert DECLARATION.public_actions == (
        "start",
        "inspect",
        "retry",
        "stop",
        "remove",
        "settings",
        "manual",
    )
    schema = get_schema()
    settings_index = DECLARATION.public_actions.index("settings")
    assert schema["properties"]["input"]["anyOf"][settings_index] == {
        "title": "settings inventory input",
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    assert _show_settings(manager) == {
        "settings": [
            {
                "key": key,
                "current": value,
                "default": value,
                "configurable": True,
                "comment": _SETTING_COMMENTS[key],
            }
            for key, value in _SETTING_DEFAULTS.items()
        ]
    }
    assert not manager._config_path.exists(), "SHOW must not trigger migration"

    manual = (
        Path(__file__).parents[1]
        / "src/lingtai/tools/task_card/manual/SKILL.md"
    ).read_text(encoding="utf-8")
    for pointer in _SETTING_COMMENTS.values():
        manual_name, anchor = pointer.split("#", 1)
        assert manual_name == "task_card-manual"
        assert f"### {anchor}\n" in manual


def test_settings_current_is_fresh_effective_owner_truth_without_unknown_field_leak(
    agent, manager
):
    secret = "private-task-card-owner-field"
    path = _write_config(
        agent,
        {
            "interval_s": 7,
            "timeout_s": 2,
            "max_refreshes": 11,
            "reminder_turns": 4,
            "max_body_chars": 150,
            "credential_path": secret,
        },
    )
    original = path.read_bytes()
    first = _show_settings(manager)
    assert [row["current"] for row in first["settings"]] == [7.0, 2.0, 11, 4, 150]
    assert path.read_bytes() == original
    assert secret not in json.dumps(first)
    assert all(list(row) == [
        "key", "current", "default", "configurable", "comment"
    ] for row in first["settings"])

    path.write_text(
        json.dumps(
            {
                "interval_s": 9,
                "timeout_s": 0.01,
                "max_refreshes": 12,
                "reminder_turns": 0,
                "max_body_chars": 99,
            }
        ),
        encoding="utf-8",
    )
    second_bytes = path.read_bytes()
    second = _show_settings(manager)
    assert [row["current"] for row in second["settings"]] == [
        9.0,
        10.0,
        12,
        10,
        2000,
    ]
    assert path.read_bytes() == second_bytes


def test_settings_previews_legacy_ceiling_and_rejects_mutation_input_without_writing(
    agent, manager
):
    legacy = _write_legacy_telegram_config(
        agent, {"taskcard": True, "normal_rows": 1, "max_refreshes": 37}
    )
    legacy_bytes = legacy.read_bytes()

    rows = {row["key"]: row["current"] for row in _show_settings(manager)["settings"]}
    assert rows == {**_SETTING_DEFAULTS, "max_refreshes": 37}
    assert not manager._config_path.exists()
    assert legacy.read_bytes() == legacy_bytes

    assert _show_settings(manager, {"set": "max_refreshes"}) == {
        "status": "failed",
        "error_code": "INVALID_ARGUMENT",
        "message": "unsupported task_card input field",
    }
    assert not manager._config_path.exists()
    assert legacy.read_bytes() == legacy_bytes


def test_settings_provider_failure_is_one_fixed_no_row_result(manager, monkeypatch):
    def unavailable():
        raise OSError("private owner read detail")

    monkeypatch.setattr(manager, "settings_rows", unavailable)
    assert _show_settings(manager) == {
        "status": "failed",
        "error_code": "SETTINGS_UNAVAILABLE",
        "message": "settings inventory is unavailable",
    }


def test_load_config_defaults_to_5_10_2000_when_no_config_file_exists(agent, manager):
    config = manager._load_config()
    assert config.interval_s == 5.0
    assert config.timeout_s == 10.0
    assert config.max_refreshes == 2000
    assert config.reminder_turns == 10

    # The one-way gate closes on this very first resolution: the intrinsic
    # file is written even though nothing migrated, so no legacy state ever
    # existed for this agent.
    assert manager._config_path.is_file()
    persisted = json.loads(manager._config_path.read_text(encoding="utf-8"))
    assert persisted == {"interval_s": 5.0, "timeout_s": 10.0, "max_refreshes": 2000}


def test_completed_work_turn_publishes_configured_soft_reminder(agent, manager, monkeypatch):
    published = []
    monkeypatch.setattr(
        "lingtai.tools.task_card.notifications.submit", lambda *args, **kwargs: published.append(kwargs)
    )
    _write_config(agent, {"reminder_turns": 2})

    manager.on_completed_work_turn()
    assert published == []
    manager.on_completed_work_turn()
    assert published[0]["data"] == {"source": "task_card.reminder", "turns": 2}


def test_start_with_no_config_file_uses_builtin_defaults(agent, manager):
    result = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY)},
            "reasoning": "no explicit values, no config file",
        }
    )
    assert result["status"] == "ok"
    watch = manager._watch
    assert watch.interval_s == 5.0
    assert watch.timeout_s == 10.0
    assert watch.max_refreshes == 2000
    assert manager._config_path.is_file()
    manager.handle({"action": "stop", "input": {"watch_id": result["watch_id"]}, "reasoning": "cleanup"})


def test_start_with_omitted_values_uses_configured_defaults(agent, manager):
    _write_config(agent, {"interval_s": 42, "timeout_s": 3, "max_refreshes": 77})
    result = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY)},
            "reasoning": "omit everything, adopt configured defaults",
        }
    )
    assert result["status"] == "ok"
    watch = manager._watch
    assert watch.interval_s == 42
    assert watch.timeout_s == 3
    assert watch.max_refreshes == 77
    manager.handle({"action": "stop", "input": {"watch_id": result["watch_id"]}, "reasoning": "cleanup"})


def test_explicit_safety_values_may_lower_but_never_exceed_configured_ceilings(agent, manager):
    _write_config(agent, {"timeout_s": 5, "max_refreshes": 50})

    over = manager.handle(
        {
            "action": "start",
            "input": {
                "renderer_path": _write_renderer(agent._working_dir, _OK_BODY),
                "interval_s": 3600,
                "timeout_s": 999,
                "max_refreshes": 999,
            },
            "reasoning": "request above the configured ceiling",
        }
    )
    assert over["status"] == "ok"
    assert manager._watch.timeout_s == 5
    assert over["max_refreshes"] == 50
    manager.handle({"action": "stop", "input": {"watch_id": over["watch_id"]}, "reasoning": "cleanup"})

    under = manager.handle(
        {
            "action": "start",
            "input": {
                "renderer_path": _write_renderer(agent._working_dir, _OK_BODY),
                "interval_s": 3600,
                "timeout_s": 1,
                "max_refreshes": 3,
            },
            "reasoning": "request below the configured ceiling",
        }
    )
    assert under["status"] == "ok"
    assert manager._watch.timeout_s == 1
    assert under["max_refreshes"] == 3
    manager.handle({"action": "stop", "input": {"watch_id": under["watch_id"]}, "reasoning": "cleanup"})


def test_slower_interval_is_never_clamped_to_the_configured_default(agent, manager):
    _write_config(agent, {"interval_s": 2})

    result = manager.handle(
        {
            "action": "start",
            "input": {
                "renderer_path": _write_renderer(agent._working_dir, _OK_BODY),
                "interval_s": 3600,
            },
            "reasoning": "much slower than the configured default cadence",
        }
    )
    assert result["status"] == "ok"
    assert manager._watch.interval_s == 3600
    manager.handle({"action": "stop", "input": {"watch_id": result["watch_id"]}, "reasoning": "cleanup"})


def test_explicit_interval_still_enforces_the_absolute_minimum_regardless_of_config(agent, manager):
    _write_config(agent, {"interval_s": 2})

    result = manager.handle(
        {
            "action": "start",
            "input": {
                "renderer_path": _write_renderer(agent._working_dir, _OK_BODY),
                "interval_s": 0.5,
            },
            "reasoning": "below the absolute floor",
        }
    )
    assert result["status"] == "failed"
    assert "interval_s must be at least 1.0" in result["message"]
    assert manager._watch is None


_VALID_INTERVAL, _VALID_TIMEOUT, _VALID_MAX = 42.0, 3.0, 77
_BUILTIN_BY_FIELD = {"interval_s": 5.0, "timeout_s": 10.0, "max_refreshes": 2000}


@pytest.mark.parametrize(
    "bad_field,payload",
    [
        ("interval_s", {"interval_s": True, "timeout_s": _VALID_TIMEOUT, "max_refreshes": _VALID_MAX}),
        ("interval_s", {"interval_s": 0.5, "timeout_s": _VALID_TIMEOUT, "max_refreshes": _VALID_MAX}),
        ("interval_s", {"interval_s": float("nan"), "timeout_s": _VALID_TIMEOUT, "max_refreshes": _VALID_MAX}),
        ("interval_s", {"interval_s": "fast", "timeout_s": _VALID_TIMEOUT, "max_refreshes": _VALID_MAX}),
        ("timeout_s", {"interval_s": _VALID_INTERVAL, "timeout_s": False, "max_refreshes": _VALID_MAX}),
        ("timeout_s", {"interval_s": _VALID_INTERVAL, "timeout_s": float("inf"), "max_refreshes": _VALID_MAX}),
        ("timeout_s", {"interval_s": _VALID_INTERVAL, "timeout_s": 0.01, "max_refreshes": _VALID_MAX}),
        ("max_refreshes", {"interval_s": _VALID_INTERVAL, "timeout_s": _VALID_TIMEOUT, "max_refreshes": True}),
        ("max_refreshes", {"interval_s": _VALID_INTERVAL, "timeout_s": _VALID_TIMEOUT, "max_refreshes": 0}),
        ("max_refreshes", {"interval_s": _VALID_INTERVAL, "timeout_s": _VALID_TIMEOUT, "max_refreshes": -5}),
        ("max_refreshes", {"interval_s": _VALID_INTERVAL, "timeout_s": _VALID_TIMEOUT, "max_refreshes": "2000"}),
        ("max_refreshes", {"interval_s": _VALID_INTERVAL, "timeout_s": _VALID_TIMEOUT, "max_refreshes": 12.5}),
    ],
)
def test_invalid_config_field_falls_back_alone_and_preserves_valid_siblings(
    agent, manager, bad_field, payload
):
    _write_config(agent, payload)

    config = manager._load_config()

    expected = {
        "interval_s": _VALID_INTERVAL,
        "timeout_s": _VALID_TIMEOUT,
        "max_refreshes": _VALID_MAX,
    }
    expected[bad_field] = _BUILTIN_BY_FIELD[bad_field]
    assert config.interval_s == expected["interval_s"]
    assert config.timeout_s == expected["timeout_s"]
    assert config.max_refreshes == expected["max_refreshes"]


@pytest.mark.parametrize("raw", ["not json{{{", "[1, 2, 3]", '"just a string"', "42", ""])
def test_unparsable_or_non_object_config_file_falls_back_to_full_builtin_defaults(
    agent, manager, raw
):
    path = agent._working_dir / "taskcard" / "taskcard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")

    config = manager._load_config()
    assert config.interval_s == 5.0
    assert config.timeout_s == 10.0
    assert config.max_refreshes == 2000


def test_legacy_telegram_max_refreshes_migrates_once_into_intrinsic_config(agent, manager):
    _write_legacy_telegram_config(agent, {"taskcard": True, "normal_rows": 2, "max_refreshes": 37})

    config = manager._load_config()
    assert config.max_refreshes == 37
    assert config.interval_s == 5.0
    assert config.timeout_s == 10.0

    assert manager._config_path.is_file()
    persisted = json.loads(manager._config_path.read_text(encoding="utf-8"))
    assert persisted == {"interval_s": 5.0, "timeout_s": 10.0, "max_refreshes": 37}

    # One-way: once the intrinsic file exists, the legacy path is never
    # consulted again, even if it changes underneath.
    _write_legacy_telegram_config(agent, {"taskcard": True, "normal_rows": 2, "max_refreshes": 999})
    assert manager._load_config().max_refreshes == 37


def test_migrated_ceiling_governs_a_real_start_end_to_end(agent, manager):
    _write_legacy_telegram_config(agent, {"taskcard": True, "normal_rows": 1, "max_refreshes": 4})

    result = manager.handle(
        {
            "action": "start",
            "input": {
                "renderer_path": _write_renderer(agent._working_dir, _OK_BODY),
                "interval_s": 3600,
                "max_refreshes": 999,
            },
            "reasoning": "requested ceiling exceeds the migrated legacy value",
        }
    )
    assert result["status"] == "ok"
    assert result["max_refreshes"] == 4
    manager.handle({"action": "stop", "input": {"watch_id": result["watch_id"]}, "reasoning": "cleanup"})


@pytest.mark.parametrize(
    "legacy_payload",
    [
        None,
        {"taskcard": True, "normal_rows": 2},
        {"taskcard": True, "normal_rows": 2, "max_refreshes": True},
        {"taskcard": True, "normal_rows": 2, "max_refreshes": 0},
        {"taskcard": True, "normal_rows": 2, "max_refreshes": -1},
        {"taskcard": True, "normal_rows": 2, "max_refreshes": "10"},
        # Telegram's own untouched default/ceiling: carries no migration
        # signal (see the dedicated /taskcard-off test below for why).
        {"taskcard": True, "normal_rows": 2, "max_refreshes": 1000},
    ],
)
def test_missing_or_invalid_legacy_value_never_migrates_but_writes_builtin_defaults(
    agent, manager, legacy_payload
):
    if legacy_payload is not None:
        _write_legacy_telegram_config(agent, legacy_payload)

    config = manager._load_config()
    assert config.interval_s == 5.0
    assert config.timeout_s == 10.0
    assert config.max_refreshes == 2000

    # The gate closes even when nothing migrated: the intrinsic file is
    # written with the plain built-in defaults, so a later legacy change
    # (even a genuinely customized one) is never consulted again.
    assert manager._config_path.is_file()
    persisted = json.loads(manager._config_path.read_text(encoding="utf-8"))
    assert persisted == {"interval_s": 5.0, "timeout_s": 10.0, "max_refreshes": 2000}

    _write_legacy_telegram_config(agent, {"taskcard": True, "normal_rows": 2, "max_refreshes": 42})
    assert manager._load_config().max_refreshes == 2000


def test_ordinary_taskcard_off_toggle_never_migrates_its_incidental_default(agent, manager):
    # /taskcard off persists ALL THREE legacy fields together
    # (TelegramService._persist_taskcard_state), including max_refreshes at
    # its own never-customized default. An ordinary user who only ever
    # toggled delivery off must land on the new built-in default (2000), not
    # be silently capped at the old incidental value (1000).
    _write_legacy_telegram_config(agent, {"taskcard": False, "normal_rows": 1, "max_refreshes": 1000})

    config = manager._load_config()
    assert config.max_refreshes == 2000

    assert manager._config_path.is_file()
    persisted = json.loads(manager._config_path.read_text(encoding="utf-8"))
    assert persisted == {"interval_s": 5.0, "timeout_s": 10.0, "max_refreshes": 2000}

    # One-way: once the intrinsic file exists (built-in or migrated), the
    # legacy path is never consulted again, even if it changes underneath.
    _write_legacy_telegram_config(agent, {"taskcard": False, "normal_rows": 1, "max_refreshes": 3})
    assert manager._load_config().max_refreshes == 2000


def test_undecodable_legacy_config_writes_intrinsic_defaults_and_is_never_read_again(
    agent, manager
):
    legacy_path = agent._working_dir / "telegram" / "taskcard.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(b'\xff{"max_refreshes": 7}')

    config = manager._load_config()
    assert config.interval_s == 5.0
    assert config.timeout_s == 10.0
    assert config.max_refreshes == 2000

    assert manager._config_path.is_file()
    persisted = json.loads(manager._config_path.read_text(encoding="utf-8"))
    assert persisted == {"interval_s": 5.0, "timeout_s": 10.0, "max_refreshes": 2000}

    # Gate now closed: repairing the legacy file's bytes afterward changes
    # nothing, exactly like any other post-resolution legacy mutation.
    _write_legacy_telegram_config(agent, {"taskcard": True, "normal_rows": 2, "max_refreshes": 55})
    assert manager._load_config().max_refreshes == 2000


def test_undecodable_intrinsic_config_falls_back_without_consulting_legacy(agent, manager):
    config_path = agent._working_dir / "taskcard" / "taskcard.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b'\xff{"max_refreshes": 9}')

    # A genuinely customized legacy value is present too, to prove it is
    # never consulted once the intrinsic path exists, even though this
    # intrinsic file itself cannot be decoded.
    _write_legacy_telegram_config(agent, {"taskcard": True, "normal_rows": 2, "max_refreshes": 41})

    config = manager._load_config()
    assert config.interval_s == 5.0
    assert config.timeout_s == 10.0
    assert config.max_refreshes == 2000

    # The malformed intrinsic file remains the untouched owner: it is not
    # silently overwritten or repaired from the legacy value.
    assert config_path.read_bytes().startswith(b"\xff")

    result = manager.handle(
        {
            "action": "start",
            "input": {"renderer_path": _write_renderer(agent._working_dir, _OK_BODY)},
            "reasoning": "undecodable intrinsic config must not raise",
        }
    )
    assert result["status"] == "ok"
    assert result["max_refreshes"] == 2000
    manager.handle({"action": "stop", "input": {"watch_id": result["watch_id"]}, "reasoning": "cleanup"})
