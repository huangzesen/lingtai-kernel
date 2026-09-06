"""Daemon-owned proofs for the read-only five-field settings inventory."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lingtai.kernel.tool_plugin import OFFICIAL_TOOL_PLUGIN_NAMES
from lingtai.tools.daemon import DECLARATION, get_schema
from lingtai.tools.daemon.settings import DAEMON_SETTING_KEYS, daemon_setting_rows
from lingtai.tools.tool_family import ChildTool, ToolFamily
from tests._daemon_helpers import make_daemon_agent

_EMPTY = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
_SETTING_ENVS = (
    "LINGTAI_DAEMON_MAX_TURNS",
    "LINGTAI_DAEMON_MANAGER_POOL_SIZE",
    "LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS",
)
_DEFAULT_INPUT = object()


@pytest.fixture(autouse=True)
def _clear_daemon_setting_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SETTING_ENVS:
        monkeypatch.delenv(name, raising=False)


def _show(agent, action_input: object = _DEFAULT_INPUT) -> dict:
    if action_input is _DEFAULT_INPUT:
        action_input = {}
    return agent._tool_handlers["daemon"](
        {"action": "settings", "input": action_input, "reasoning": "inspect"}
    )


def _configured_agent(tmp_path: Path):
    return make_daemon_agent(
        tmp_path,
        {
            "daemon": {
                "max_turns": 123,
                "manager_pool_size": 7,
                "system_prompt_budget_chars": 21_000,
                "timeout": 42,
            }
        },
    )


def test_daemon_declaration_places_settings_immediately_before_manual() -> None:
    assert DECLARATION.settings is True
    assert DECLARATION.public_actions[-2:] == ("settings", "manual")


def test_daemon_is_an_official_tool_plugin() -> None:
    assert "daemon" in OFFICIAL_TOOL_PLUGIN_NAMES


def test_exact_row_keys_values_defaults_flags_and_comment_targets(
    tmp_path: Path,
) -> None:
    result = _show(_configured_agent(tmp_path))

    assert result == {
        "settings": [
            {
                "key": "max_turns",
                "current": 123,
                "default": 5_000,
                "configurable": True,
                "comment": "daemon-manual#max-turns",
            },
            {
                "key": "manager_pool_size",
                "current": 7,
                "default": 100,
                "configurable": True,
                "comment": "daemon-manual#manager-pool-size",
            },
            {
                "key": "system_prompt_budget_chars",
                "current": 21_000,
                "default": 20_000,
                "configurable": True,
                "comment": "daemon-manual#system-prompt-budget-chars",
            },
            {
                "key": "timeout",
                "current": 42,
                "default": 3_600.0,
                "configurable": True,
                "comment": "daemon-manual#timeout",
            },
        ]
    }
    assert tuple(row["key"] for row in result["settings"]) == DAEMON_SETTING_KEYS
    assert all(
        tuple(row) == ("key", "current", "default", "configurable", "comment")
        for row in result["settings"]
    )


def test_show_reads_fresh_effective_manager_truth_not_live_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINGTAI_DAEMON_MAX_TURNS", "321")
    monkeypatch.setenv("LINGTAI_DAEMON_MANAGER_POOL_SIZE", "3")
    monkeypatch.setenv("LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS", "23456")
    agent = _configured_agent(tmp_path)
    manager = agent.get_capability("daemon")

    monkeypatch.setenv("LINGTAI_DAEMON_MAX_TURNS", "654")
    rows = {row["key"]: row for row in _show(agent)["settings"]}

    assert rows["max_turns"]["current"] == manager._max_turns == 321
    assert rows["manager_pool_size"]["current"] == manager._manager_pool_size == 3
    assert rows["system_prompt_budget_chars"]["current"] == 23_456
    manager._max_turns = 777
    assert _show(agent)["settings"][0]["current"] == 777


def test_timeout_has_only_capability_setup_source(tmp_path: Path) -> None:
    default_agent = make_daemon_agent(tmp_path)
    assert {row["key"]: row for row in _show(default_agent)["settings"]}[
        "timeout"
    ]["current"] == 3_600.0

    explicit_agent = make_daemon_agent(
        tmp_path,
        {"daemon": {"timeout": 42}},
        working_dir_name="daemon-explicit",
    )
    assert {row["key"]: row for row in _show(explicit_agent)["settings"]}[
        "timeout"
    ]["current"] == 42


@pytest.mark.parametrize(
    "value",
    (
        pytest.param(True, id="boolean"),
        pytest.param("30", id="string"),
        pytest.param(4.5, id="below-public-floor"),
    ),
)
def test_timeout_setup_passes_invalid_json_finite_values_through(
    tmp_path: Path, value: object
) -> None:
    agent = make_daemon_agent(
        tmp_path,
        {"daemon": {"timeout": value}},
        working_dir_name="daemon-invalid-finite-timeout",
    )

    manager = agent.get_capability("daemon")
    current = {row["key"]: row for row in _show(agent)["settings"]}[
        "timeout"
    ]["current"]

    assert manager._timeout is value
    assert current is value


@pytest.mark.parametrize(
    "value",
    (
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ),
)
def test_timeout_non_finite_setup_value_makes_show_unavailable(
    tmp_path: Path, value: float
) -> None:
    agent = make_daemon_agent(
        tmp_path,
        {"daemon": {"timeout": value}},
        working_dir_name="daemon-non-finite-timeout",
    )

    assert agent.get_capability("daemon")._timeout is value
    assert _show(agent) == {
        "status": "failed",
        "error_code": "SETTINGS_UNAVAILABLE",
        "message": "settings inventory is unavailable",
    }


def test_unavailable_current_fails_the_whole_inventory_without_partial_rows() -> None:
    manager = SimpleNamespace(
        _max_turns=1,
        _manager_pool_size=2,
        _system_prompt_budget_chars=3,
        # Deliberately no _timeout: current truth is unavailable.
    )
    family = ToolFamily(
        "daemon",
        [ChildTool("manual", _EMPTY, lambda _value: {"manual": "ok"})],
        settings_provider=lambda: daemon_setting_rows(manager),
    )

    assert family.handle(
        {"action": "settings", "input": {}, "reasoning": "inspect"}
    ) == {
        "status": "failed",
        "error_code": "SETTINGS_UNAVAILABLE",
        "message": "settings inventory is unavailable",
    }


def test_settings_is_strict_empty_read_only_and_list_is_unchanged(
    tmp_path: Path,
) -> None:
    agent = _configured_agent(tmp_path)
    owner_path = agent._working_dir / "daemon" / "daemon.json"
    before = owner_path.read_bytes() if owner_path.exists() else None

    schema = get_schema()
    assert schema["properties"]["action"]["enum"][-2:] == ["settings", "manual"]
    settings_branch = next(
        branch
        for branch in schema["properties"]["input"]["anyOf"]
        if branch["title"] == "settings inventory input"
    )
    assert settings_branch == {"title": "settings inventory input", **_EMPTY}
    for invalid in (None, [], {"set": "max_turns"}, {"reset": True}):
        result = _show(agent, invalid)
        assert result["status"] == "failed"
        assert "settings" not in result
    assert (owner_path.read_bytes() if owner_path.exists() else None) == before

    listed = agent._tool_handlers["daemon"](
        {
            "action": "list",
            "input": {
                "contains": None,
                "status": None,
                "include_done": None,
                "last": None,
            },
            "reasoning": "unchanged basic action",
        }
    )
    assert listed["emanations"] == []
    assert listed["manager_pool_size"] == 7
    assert listed["index"] == "dispatch_ledger"


def test_every_comment_resolves_to_an_exact_daemon_manual_heading() -> None:
    manual = (
        Path(__file__).parents[1]
        / "src"
        / "lingtai"
        / "tools"
        / "daemon"
        / "manual"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    for heading in (
        "### Max turns",
        "### Manager pool size",
        "### System prompt budget chars",
        "### Timeout",
    ):
        assert heading in manual
    for anchor in (
        "daemon-manual#max-turns",
        "daemon-manual#manager-pool-size",
        "daemon-manual#system-prompt-budget-chars",
        "daemon-manual#timeout",
    ):
        assert manual.count(anchor) == 1
    assert manual.count("`daemon.settings`") == 4
    assert "precedence and setup details belong to the Contract" in manual
    assert "SETTINGS_UNAVAILABLE" in manual
    assert "SHOW does not mutate it" in manual
