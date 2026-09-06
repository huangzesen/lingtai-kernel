"""Daemon's LTP v2 action-separated migration: schema, dispatch, and parity.

Proves the public/model-facing ``daemon`` tool is the ToolFamily-composed
``action``/``input``/``reasoning``/``summarize`` envelope over the canonical
children ``emanate | list | ask | check | reclaim | settings | manual``, while
``DaemonManager`` — batch emanation, backend routing, run directories, the
detached supervisor, ``daemon_common`` completion signaling, cancellation,
timeouts, terminal notifications, and result/error persistence — stays the
exact, untouched engine the pre-migration suites (``test_daemon.py``,
``test_daemon_check.py``, ``test_daemon_backend_options.py``,
``test_daemon_detached_supervisor.py``, …) already exercise through its legacy
flat call shape.

The invariants under test come from ``src/lingtai/tools/CONTRACT.md``
(Envelope, Dispatch and actions), ``src/lingtai/tools/tool_family/CONTRACT.md``,
and the Daemon migration acceptance line: exactly one model tool slot, strict
action/input correlation, read-only (``list``/``check``/``settings``/
``manual``) vs
side-effectful (``emanate``/``ask``/``reclaim``) receipt truth, the reserved
``manual`` performing no daemon operation with no double wrap, and the complete
nested ``emanate`` task schema preserved field-for-field.

Every test here uses an isolated temp working directory and a mock-service
Agent. Nothing dispatches a real emanation, touches a live peer, or reaches an
LLM: the side-effect actions are proven through their pre-run validation
receipts and through a recording stub manager, never by starting a run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._daemon_helpers import (
    daemon_action_input_schema,
    daemon_emanate_task_schema,
    make_daemon_agent,
)
from lingtai.tools import daemon as daemon_pkg
from lingtai.tools.daemon import get_description, get_schema, setup
from lingtai.tools.daemon._tool_family import (
    ASK_INPUT_SCHEMA,
    CHECK_INPUT_SCHEMA,
    DAEMON_ACTIONS,
    LIST_INPUT_SCHEMA,
    RECLAIM_INPUT_SCHEMA,
    DaemonFamilyDispatcher,
)
from lingtai.tools.tool_family import ToolFamilyError
from lingtai.tools.tool_family.manual import MANUAL_INPUT_SCHEMA

_ACTIONS = list(DAEMON_ACTIONS)
_ACTION_TITLES = [
    "settings inventory input" if action == "settings" else f"{action} input"
    for action in _ACTIONS
]
_READ_ONLY = ("list", "check", "settings", "manual")
_SIDE_EFFECTFUL = ("emanate", "ask", "reclaim")

# One minimal, schema-valid ``input`` per action — every optional explicitly
# null, which is how a strict provider fills a required-nullable property.
_MINIMAL_INPUT: dict[str, dict[str, Any]] = {
    "emanate": {"tasks": [], "backend": None, "max_turns": None, "timeout": None},
    "list": {"contains": None, "status": None, "include_done": None, "last": None},
    "ask": {"id": "em-1", "message": "hello"},
    "check": {"id": "em-1", "last": None, "truncate": None},
    "reclaim": {},
    "settings": {},
    "manual": {},
}


class _RecordingManager:
    """Stands in for ``DaemonManager``, recording the flat call it receives.

    The migration's whole claim is that the dispatcher translates the envelope
    into the engine's unchanged legacy flat shape. Recording that exact flat
    dict is the direct proof, and it starts no process, writes no run
    directory, and sends no notification.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._max_turns = 5_000
        self._manager_pool_size = 100
        self._system_prompt_budget_chars = 20_000
        self._timeout = 3_600.0

    def handle(self, args: dict) -> dict:
        self.calls.append(dict(args))
        return {"status": "ok", "echo": dict(args)}


def _dispatcher(tmp_path: Path) -> tuple[DaemonFamilyDispatcher, _RecordingManager]:
    agent = MagicMock()
    agent._working_dir = tmp_path
    mgr = _RecordingManager()
    return (
        DaemonFamilyDispatcher(mgr, agent, list(daemon_pkg._BACKEND_SCHEMA_ENUM)),
        mgr,
    )


def _call(dispatcher: DaemonFamilyDispatcher, action: str, **overrides) -> dict:
    action_input = dict(_MINIMAL_INPUT[action])
    action_input.update(overrides)
    return dispatcher.handle(
        {"action": action, "input": action_input, "reasoning": "migration test"}
    )


def _install_daemon_manual(working_dir: Path, body: str) -> Path:
    """Install a daemon manual exactly where the real initializer puts it.

    ``Agent._install_intrinsic_manuals`` maps the package to the model-facing
    destination name ``daemon`` under ``.library/intrinsic/capabilities/`` — so
    ``build_manual_child(agent, "daemon")`` reads the same body/path the
    pre-migration ``DaemonManager.handle({"action": "manual"})`` branch did via
    ``load_installed_manual``.
    """
    manual_dir = working_dir / ".library" / "intrinsic" / "capabilities" / "daemon"
    manual_dir.mkdir(parents=True, exist_ok=True)
    manual_path = manual_dir / "SKILL.md"
    manual_path.write_text(body, encoding="utf-8")
    return manual_path


# ---------------------------------------------------------------------------
# Public identity: one tool slot, one name, the family schema
# ---------------------------------------------------------------------------

def test_public_model_facing_name_is_daemon_with_the_family_schema(tmp_path):
    """The registered public name stays exactly ``daemon`` and the registered
    schema is the action-separated family envelope, not the legacy flat shape."""
    agent = MagicMock()
    agent._working_dir = tmp_path

    setup(agent)

    assert agent.add_tool.call_args.args[0] == "daemon"
    schema = agent.add_tool.call_args.kwargs["schema"]
    assert schema == get_schema()
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    # Every legacy flat field is gone from the public root.
    for legacy_flat_field in (
        "tasks", "id", "message", "last", "truncate", "contains", "status",
        "include_done", "summary", "max_turns", "timeout", "backend",
    ):
        assert legacy_flat_field not in schema["properties"]


def test_setup_registers_exactly_one_model_facing_daemon_tool(tmp_path):
    """The seven children consume no extra model tool slot: a real Agent with the
    daemon capability exposes exactly one ``daemon`` tool and no per-action tool."""
    agent = make_daemon_agent(tmp_path)

    from lingtai.kernel.base_agent.tools import _build_tool_schemas

    model_tools = [schema.name for schema in _build_tool_schemas(agent)]

    assert model_tools.count("daemon") == 1
    for action in _ACTIONS:
        assert f"daemon_{action}" not in model_tools
        assert f"daemon.{action}" not in model_tools
    # No second model-facing root and no compatibility alias was registered.
    assert [name for name in model_tools if "daemon" in name] == ["daemon"]


def test_registered_handler_is_the_envelope_dispatcher_not_the_flat_engine(tmp_path):
    """The registered handler must be the family dispatcher. The engine's own
    ``handle`` is retained as the internal flat shape, not the model surface."""
    agent = MagicMock()
    agent._working_dir = tmp_path

    mgr = setup(agent)
    handler = agent.add_tool.call_args.kwargs["handler"]

    assert handler != mgr.handle
    assert isinstance(handler.__self__, DaemonFamilyDispatcher)


def test_registered_description_is_a_concise_first_use_contract():
    """The resident description owns first-use safety, not routing detail."""
    description = get_description()

    assert len(description) < 943
    assert "Read the daemon manual before first use" in description
    assert "complete objective" in description
    assert "tools grant capability only" in description
    assert "Terminal outcomes are push-notified" in description
    assert "do not poll for completion" in description
    assert "check" in description and "durable result/error" in description
    assert "compact(action='run'" in description
    assert "daemon(action='" not in description
    assert "lingtai-agent" not in description
    assert "reference/cli-backends" not in description


# ---------------------------------------------------------------------------
# Root envelope and child schemas
# ---------------------------------------------------------------------------

def test_root_is_strict_action_input_reasoning_summarize():
    schema = get_schema()

    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    # ``reasoning`` is REQUIRED Host audit metadata declared by the family
    # itself: Agent composition only merges ``properties``, never ``required``.
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["reasoning"]["type"] == "string"
    assert schema["properties"]["summarize"]["type"] == "boolean"


def test_canonical_children_are_the_seven_daemon_actions():
    schema = get_schema()

    assert schema["properties"]["action"]["enum"] == _ACTIONS
    assert [b["title"] for b in schema["properties"]["input"]["anyOf"]] == (
        _ACTION_TITLES
    )


@pytest.mark.parametrize(
    "action,expected_fields",
    [
        ("emanate", {"tasks", "backend", "max_turns", "timeout"}),
        ("list", {"contains", "status", "include_done", "last"}),
        ("ask", {"id", "message"}),
        ("check", {"id", "last", "truncate"}),
        ("reclaim", set()),
        ("settings", set()),
        ("manual", set()),
    ],
)
def test_each_action_owns_exactly_its_own_fields(action, expected_fields):
    """The pre-migration flat root advertised all thirteen fields to all six
    actions; each field now lives in exactly the branch that consumes it."""
    branch = daemon_action_input_schema(action)

    assert set(branch["properties"]) == expected_fields


def test_no_field_leaks_across_action_branches():
    """Cross-action isolation stated positively: a field belonging to one
    action must not appear in any other action's branch."""
    owners = {
        "tasks": "emanate", "backend": "emanate", "max_turns": "emanate",
        "timeout": "emanate", "contains": "list", "status": "list",
        "include_done": "list", "message": "ask", "truncate": "check",
    }
    for field, owner in owners.items():
        for action in _ACTIONS:
            present = field in daemon_action_input_schema(action)["properties"]
            assert present == (action == owner), (field, action)
    # ``id`` and ``last`` are the two deliberately shared spellings.
    assert {a for a in _ACTIONS if "id" in daemon_action_input_schema(a)["properties"]} == {"ask", "check"}
    assert {a for a in _ACTIONS if "last" in daemon_action_input_schema(a)["properties"]} == {"list", "check"}


def test_every_child_input_schema_is_closed_and_free_of_host_fields():
    for action in _ACTIONS:
        branch = daemon_action_input_schema(action)
        assert branch["additionalProperties"] is False
        for host_field in ("reasoning", "_reasoning", "summarize", "summary", "action"):
            assert host_field not in branch["properties"], (action, host_field)


def test_reclaim_settings_and_manual_take_the_canonical_strict_empty_input():
    for action in ("reclaim", "settings", "manual"):
        branch = daemon_action_input_schema(action)
        assert branch["properties"] == {}
        assert branch["required"] == []
        assert branch["additionalProperties"] is False
    assert RECLAIM_INPUT_SCHEMA == MANUAL_INPUT_SCHEMA


def test_root_allof_correlates_each_action_const_to_its_own_input_schema():
    schema = get_schema()

    assert [c["if"]["properties"]["action"]["const"] for c in schema["allOf"]] == _ACTIONS
    for condition in schema["allOf"]:
        action = condition["if"]["properties"]["action"]["const"]
        assert condition["if"]["required"] == ["action"]
        branch = daemon_action_input_schema(action)
        correlated = condition["then"]["properties"]["input"]
        # Same canonical child schema the ``anyOf`` branch embeds, minus the
        # presentational ``title`` the branch adds.
        assert correlated == {k: v for k, v in branch.items() if k != "title"}


def test_composed_schema_branches_are_mutation_isolated():
    """A caller mutating one composed schema must not corrupt the next call's."""
    first = get_schema()
    first["properties"]["input"]["anyOf"][0]["properties"].clear()
    first["allOf"][0]["then"]["properties"]["input"]["properties"].clear()

    second = get_schema()

    assert set(second["properties"]["input"]["anyOf"][0]["properties"]) == {
        "tasks", "backend", "max_turns", "timeout"
    }


# ---------------------------------------------------------------------------
# The complete nested emanate task schema is preserved
# ---------------------------------------------------------------------------

def test_emanate_nested_task_schema_preserves_every_field():
    task = daemon_emanate_task_schema()

    assert set(task["properties"]) == {
        "task", "tools", "skills", "mcp", "plugin", "preset", "backend_options",
        "prompt", "context_token_limit", "task_files",
    }
    assert task["required"] == ["task", "tools"]
    # The nested task object stays OPEN: the engine's own strict per-task
    # validation owns that boundary and returns domain-specific errors (e.g.
    # the ``system_prompt`` obsolescence message) a schema rejection would
    # replace with a generic one.
    assert "additionalProperties" not in task
    assert task["properties"]["tools"]["items"]["type"] == "string"
    assert task["properties"]["mcp"]["items"]["type"] == "object"
    assert task["properties"]["context_token_limit"]["minimum"] == 1


def test_emanate_backend_options_passthrough_contract_is_unchanged():
    backend_options = daemon_emanate_task_schema()["properties"]["backend_options"]

    assert backend_options["type"] == "object"
    for schema in (
        backend_options["additionalProperties"],
        backend_options["properties"]["config"],
    ):
        types = {branch.get("type") for branch in schema["anyOf"]}
        assert types == {"boolean", "string", "integer", "number", "null", "array"}


def test_emanate_backend_enum_and_engine_ceilings_agree():
    emanate = daemon_action_input_schema("emanate")["properties"]

    # Every routable backend, plus the null that means "use the default".
    assert emanate["backend"]["enum"] == [*daemon_pkg._BACKEND_SCHEMA_ENUM, None]
    for hidden in ("claude", "claude-interactive"):
        assert hidden not in emanate["backend"]["enum"]
    assert emanate["max_turns"]["maximum"] == daemon_pkg.DEFAULT_MAX_TURNS
    assert emanate["timeout"]["minimum"] == 5


def test_check_last_ceiling_and_list_default_match_the_engine():
    check_last = daemon_action_input_schema("check")["properties"]["last"]
    assert check_last["maximum"] == daemon_pkg.DaemonManager._CHECK_LAST_MAX
    assert check_last["minimum"] == 1

    list_last = daemon_action_input_schema("list")["properties"]["last"]
    assert "maximum" not in list_last
    assert list_last["minimum"] == 1
    assert "newest 1000" in list_last["description"]
    assert daemon_pkg.DaemonManager._LIST_DEFAULT_LAST == 1000

    assert daemon_action_input_schema("check")["properties"]["truncate"]["minimum"] == 0


# ---------------------------------------------------------------------------
# Dispatch: action/input correlation reaches the engine unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["emanate", "list", "ask", "check", "reclaim"])
def test_each_action_reaches_the_engine_with_its_own_flat_action(tmp_path, action):
    dispatcher, mgr = _dispatcher(tmp_path)

    _call(dispatcher, action)

    assert len(mgr.calls) == 1
    assert mgr.calls[0]["action"] == action


def test_ask_forwards_exactly_id_and_message(tmp_path):
    dispatcher, mgr = _dispatcher(tmp_path)

    _call(dispatcher, "ask", id="em-7", message="status?")

    assert mgr.calls[0] == {"action": "ask", "id": "em-7", "message": "status?"}


def test_check_forwards_id_last_and_truncate(tmp_path):
    dispatcher, mgr = _dispatcher(tmp_path)

    _call(dispatcher, "check", id="em-3", last=5, truncate=0)

    assert mgr.calls[0] == {"action": "check", "id": "em-3", "last": 5, "truncate": 0}


def test_emanate_forwards_the_full_nested_task_batch_verbatim(tmp_path):
    """The complex nested ``emanate`` input must reach the engine unaltered —
    every task key, in full, with the batch-level controls alongside it."""
    dispatcher, mgr = _dispatcher(tmp_path)
    tasks = [
        {
            "task": "audit the parser",
            "tools": ["file", "shell"],
            "skills": ["./skills/audit"],
            "mcp": [{"name": "svc", "transport": "stdio", "command": "x"}],
            "preset": "~/.lingtai-tui/presets/saved/cheap.json",
            "backend_options": {"config": ["a=1", "b=2"], "search": True},
            "prompt": "Begin now.",
            "context_token_limit": 120000,
        }
    ]

    _call(dispatcher, "emanate", tasks=tasks, backend="codex", max_turns=25, timeout=90)

    assert mgr.calls[0] == {
        "action": "emanate",
        "tasks": tasks,
        "backend": "codex",
        "max_turns": 25,
        "timeout": 90,
    }


def test_null_optionals_are_dropped_so_engine_defaults_apply(tmp_path):
    """Strict schemas spell optionals as required-nullable. Null must mean
    *absent* to the engine, so its own unchanged defaults still apply."""
    dispatcher, mgr = _dispatcher(tmp_path)

    _call(dispatcher, "list")
    _call(dispatcher, "check", id="em-1")

    assert mgr.calls[0] == {"action": "list"}
    assert mgr.calls[1] == {"action": "check", "id": "em-1"}


def test_falsy_but_present_values_are_preserved_not_dropped(tmp_path):
    """``truncate: 0`` and ``include_done: False`` are meaningful values, not
    absences — dropping them would silently change daemon behavior."""
    dispatcher, mgr = _dispatcher(tmp_path)

    _call(dispatcher, "check", id="em-1", truncate=0)
    _call(dispatcher, "list", include_done=False, contains="")

    assert mgr.calls[0]["truncate"] == 0
    assert mgr.calls[1]["include_done"] is False
    assert mgr.calls[1]["contains"] == ""


def test_reasoning_and_summarize_never_reach_the_engine(tmp_path):
    """Envelope metadata is Host-owned: a child receives only its own input."""
    dispatcher, mgr = _dispatcher(tmp_path)

    dispatcher.handle(
        {
            "action": "list",
            "input": dict(_MINIMAL_INPUT["list"]),
            "reasoning": "why",
            "_reasoning": "why",
            "summarize": True,
        }
    )

    for forbidden in ("reasoning", "_reasoning", "summarize", "summary"):
        assert forbidden not in mgr.calls[0]


# ---------------------------------------------------------------------------
# Envelope validation is fail-closed before any engine I/O
# ---------------------------------------------------------------------------

def test_unknown_action_fails_with_daemons_own_seven_action_message(tmp_path):
    dispatcher, mgr = _dispatcher(tmp_path)

    result = dispatcher.handle({"action": "kill", "input": {}, "reasoning": "r"})

    assert result["status"] == "failed"
    assert result["error_code"] == "ACTION_REQUIRED"
    assert result["message"] == (
        "action must be one of emanate, list, ask, check, reclaim, settings, or manual"
    )
    assert mgr.calls == []


@pytest.mark.parametrize("action", [[], {}, None, 7])
def test_unhashable_or_wrong_typed_action_is_rejected_not_raised(tmp_path, action):
    """Invalid JSON can make ``action`` unhashable (issue #513): it must render
    the typed envelope failure, never raise ``TypeError`` out of dispatch."""
    dispatcher, mgr = _dispatcher(tmp_path)

    result = dispatcher.handle({"action": action, "input": {}, "reasoning": "r"})

    assert result["error_code"] == "ACTION_REQUIRED"
    assert mgr.calls == []


@pytest.mark.parametrize(
    "action,foreign_input",
    [
        ("list", {"id": "em-1"}),
        ("check", {"tasks": []}),
        ("ask", {"truncate": 10}),
        ("emanate", {"message": "hi"}),
        ("reclaim", {"id": "em-1"}),
        ("settings", {"last": 5}),
        ("manual", {"last": 5}),
    ],
)
def test_cross_action_input_fields_are_rejected_before_engine_io(
    tmp_path, action, foreign_input
):
    """Schema conformance is not the sole boundary: dispatch rejects any
    ``input`` key outside the selected child's own declared schema."""
    dispatcher, mgr = _dispatcher(tmp_path)

    result = dispatcher.handle(
        {"action": action, "input": foreign_input, "reasoning": "r"}
    )

    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "unsupported daemon input field"
    assert mgr.calls == []


def test_unknown_root_field_and_non_object_input_are_rejected(tmp_path):
    dispatcher, mgr = _dispatcher(tmp_path)

    unknown_root = dispatcher.handle(
        {"action": "list", "input": {}, "reasoning": "r", "tasks": []}
    )
    bad_input = dispatcher.handle(
        {"action": "list", "input": "everything", "reasoning": "r"}
    )

    assert unknown_root["message"] == "unsupported daemon argument"
    assert bad_input["message"] == "input must be an object"
    assert mgr.calls == []


def test_non_boolean_summarize_is_rejected_before_dispatch(tmp_path):
    dispatcher, mgr = _dispatcher(tmp_path)

    result = dispatcher.handle(
        {"action": "list", "input": {}, "reasoning": "r", "summarize": "yes"}
    )

    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "summarize must be a boolean"
    assert mgr.calls == []


def test_legacy_flat_call_no_longer_reaches_the_engine(tmp_path):
    """A pre-migration flat call is refused at the envelope, not silently
    honored — there is no compatibility alias on the model-facing surface."""
    dispatcher, mgr = _dispatcher(tmp_path)

    result = dispatcher.handle({"action": "check", "id": "em-1", "last": 5})

    assert result["status"] == "failed"
    assert mgr.calls == []


# ---------------------------------------------------------------------------
# Read-only vs side-effectful receipt truth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", _READ_ONLY)
def test_read_only_actions_start_no_run_and_cancel_nothing(tmp_path, action):
    """``list``/``check``/``settings``/``manual`` must be pure reads:
    no emanation is created, nothing is cancelled, no run dir appears."""
    agent = make_daemon_agent(tmp_path, working_dir_name=f"ro-{action}")
    handler = agent._tool_handlers["daemon"]
    mgr = agent._capability_managers["daemon"]
    daemons_root = agent._working_dir / "daemons"
    before = sorted(p.name for p in daemons_root.glob("*")) if daemons_root.is_dir() else []

    result = handler(
        {"action": action, "input": dict(_MINIMAL_INPUT[action]), "reasoning": "r"}
    )

    assert result is not None
    assert mgr._emanations == {}
    # No run directory was created and nothing was cancelled.
    after = sorted(p.name for p in daemons_root.glob("*")) if daemons_root.is_dir() else []
    assert after == before
    assert getattr(mgr, "_last_reclaim_natural_terminal", 0) == 0


@pytest.mark.parametrize("action", _SIDE_EFFECTFUL)
def test_side_effect_actions_are_the_only_ones_that_can_mutate(tmp_path, action):
    """``emanate``/``ask``/``reclaim`` are the mutating three. Each is proven
    here at its own pre-run refusal/receipt boundary — no real run is started,
    no live peer is touched, and no shared runtime is polluted."""
    agent = make_daemon_agent(tmp_path, working_dir_name=f"se-{action}")
    handler = agent._tool_handlers["daemon"]
    mgr = agent._capability_managers["daemon"]

    result = handler(
        {"action": action, "input": dict(_MINIMAL_INPUT[action]), "reasoning": "r"}
    )

    if action == "emanate":
        # Empty batch: the engine's own validation refuses before any run dir.
        assert result == {"status": "error", "message": "No tasks provided"}
    elif action == "ask":
        assert result == {"status": "error", "message": "Unknown emanation: em-1"}
    else:
        # ``reclaim`` is always a real receipt, and truthfully reports zero.
        assert result["status"] == "reclaimed"
        assert result["cancelled"] == 0
    assert mgr._emanations == {}


def test_reclaim_receipt_reaches_the_engine_with_no_input(tmp_path):
    dispatcher, mgr = _dispatcher(tmp_path)

    result = _call(dispatcher, "reclaim")

    assert mgr.calls[0] == {"action": "reclaim"}
    # The engine's own raw result is returned verbatim — no second envelope.
    assert result["echo"] == {"action": "reclaim"}


def test_emanate_validation_refusal_is_returned_verbatim(tmp_path):
    """The engine's exact pre-run validation errors survive the new envelope,
    including the ``system_prompt`` obsolescence message the open nested task
    schema deliberately leaves to the engine."""
    agent = make_daemon_agent(tmp_path, working_dir_name="emanate-validation")
    handler = agent._tool_handlers["daemon"]

    result = handler(
        {
            "action": "emanate",
            "input": {
                "tasks": [{"task": "t", "tools": ["file"], "system_prompt": "old"}],
                "backend": None, "max_turns": None, "timeout": None,
            },
            "reasoning": "r",
        }
    )

    assert result["status"] == "error"
    assert "tasks[0].system_prompt is obsolete" in result["message"]
    assert agent._capability_managers["daemon"]._emanations == {}


# ---------------------------------------------------------------------------
# The reserved manual child
# ---------------------------------------------------------------------------

def test_manual_returns_the_canonical_body_and_path_without_double_wrap(tmp_path):
    agent = make_daemon_agent(tmp_path, working_dir_name="manual-agent")
    body = "# daemon manual\n\nInspection patterns and polling cadence.\n"
    manual_path = _install_daemon_manual(agent._working_dir, body)

    result = agent._tool_handlers["daemon"](
        {"action": "manual", "input": {}, "reasoning": "r"}
    )

    assert result["status"] == "ok"
    assert result["content"] == [{"type": "text", "text": body}]
    assert result["structuredContent"] == {"manual_path": str(manual_path)}
    # No second wrapper: the child's canonical result IS the dispatch result.
    assert "result" not in result and "manual" not in result


def test_manual_missing_is_reported_degraded_not_silently_empty(tmp_path):
    """A real Agent installs the manual at startup, so the degraded branch is
    exercised against a working dir where it was never installed — the loader
    must say so truthfully rather than return an empty body as ``ok``."""
    dispatcher, _ = _dispatcher(tmp_path)

    result = _call(dispatcher, "manual")

    assert result["status"] == "degraded"
    assert "daemon manual missing" in result["error"]
    assert result["content"][0]["text"] == ""
    assert result["structuredContent"]["manual_path"].endswith(
        "/.library/intrinsic/capabilities/daemon/SKILL.md"
    )


def test_manual_performs_no_daemon_operation(tmp_path):
    """The reserved ``manual`` reaches ``build_manual_child``'s loader only —
    never the engine — so reading documentation cannot dispatch or cancel."""
    dispatcher, mgr = _dispatcher(tmp_path)
    _install_daemon_manual(tmp_path, "# daemon manual\n")

    _call(dispatcher, "manual")

    assert mgr.calls == []


def test_family_manual_worked_examples_use_the_registered_envelope():
    """The manual is mandated reading served by ``action="manual"``. Every
    worked ``daemon(...)`` call across the manual tree must teach the envelope
    the registered schema actually accepts, not the pre-migration flat shape."""
    import re

    manual_root = (
        Path(__file__).resolve().parents[1]
        / "src" / "lingtai" / "tools" / "daemon" / "manual"
    )
    files = sorted(manual_root.rglob("SKILL.md"))
    assert files, "expected the installed daemon manual tree"

    actions_seen: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        for call in re.findall(r"daemon\(action=[^)]*\)", text):
            assert "input=" in call, f"{path.name} still teaches the flat shape: {call}"
            match = re.search(r"""daemon\(action=['"]?(\w+)""", call)
            if match:
                actions_seen.add(match.group(1))

    # The manual demonstrates the actions an operator actually calls.
    assert {"emanate", "list", "ask", "check", "reclaim"} <= actions_seen
    assert actions_seen <= set(_ACTIONS) | {"list"}


def test_manual_documents_the_read_only_versus_side_effect_split():
    manual = (
        Path(__file__).resolve().parents[1]
        / "src" / "lingtai" / "tools" / "daemon" / "manual" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "`list`, `check`, `settings`, and `manual` are read-only" in manual
    assert "`emanate`, `ask`, and `reclaim`" in manual
    # The envelope itself is taught, including the summarize rename.
    assert 'daemon(action="reclaim", input={}' in manual
    assert "The optional root `summarize` boolean replaces the" in manual
    assert "former flat `summary` field." in manual


def test_the_engines_flat_manual_branch_is_not_the_model_facing_path(tmp_path):
    """``DaemonManager.handle``'s own ``action='manual'`` branch is retained for
    historical/internal flat callers, but the registered surface serves the
    canonical ManualTool shape instead — the two must not both be model-facing."""
    agent = make_daemon_agent(tmp_path, working_dir_name="manual-split")
    body = "# daemon manual\n"
    _install_daemon_manual(agent._working_dir, body)
    mgr = agent._capability_managers["daemon"]

    engine_result = mgr.handle({"action": "manual"})
    model_result = agent._tool_handlers["daemon"](
        {"action": "manual", "input": {}, "reasoning": "r"}
    )

    assert engine_result["manual"] == body           # legacy flat shape
    assert "content" not in engine_result
    assert model_result["content"][0]["text"] == body  # canonical shape
    assert "manual" not in model_result


# ---------------------------------------------------------------------------
# Chat/Responses wire parity
# ---------------------------------------------------------------------------

def test_daemon_family_schema_survives_chat_and_responses_wires():
    from lingtai.kernel.llm.base import FunctionSchema
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    schema = FunctionSchema(name="daemon", description="daemon", parameters=get_schema())
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    for wire, combinator in ((chat, "anyOf"), (responses, "anyOf")):
        assert wire["type"] == "object"
        assert wire["required"] == ["action", "input", "reasoning"]
        assert wire["additionalProperties"] is False
        assert set(wire["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert wire["properties"]["action"]["enum"] == _ACTIONS
        branches = wire["properties"]["input"][combinator]
        assert [b["title"] for b in branches] == _ACTION_TITLES
        for branch in branches:
            assert branch["additionalProperties"] is False
            for host_field in ("reasoning", "_reasoning", "summarize"):
                assert host_field not in branch["properties"]


def test_responses_wire_preserves_the_root_action_input_correlation():
    """The root ``allOf``/``if``/``then`` correlation must survive the Responses
    scrubber, so a mismatched action/input pairing is refusable schema-side on
    both wires — dispatch remains authoritative regardless."""
    from lingtai.kernel.llm.base import FunctionSchema
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    schema = FunctionSchema(name="daemon", description="daemon", parameters=get_schema())
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    for wire in (chat, responses):
        assert [c["if"]["properties"]["action"]["const"] for c in wire["allOf"]] == _ACTIONS
        emanate_condition = wire["allOf"][0]["then"]["properties"]["input"]
        assert set(emanate_condition["properties"]) == {
            "tasks", "backend", "max_turns", "timeout"
        }
        ask_condition = wire["allOf"][2]["then"]["properties"]["input"]
        assert set(ask_condition["properties"]) == {"id", "message"}
        # The complex nested emanate task object survives both wires intact.
        task_items = emanate_condition["properties"]["tasks"]["items"]
        assert set(task_items["properties"]) == {
            "task", "tools", "skills", "mcp", "plugin", "preset", "backend_options",
            "prompt", "context_token_limit", "task_files",
        }


def test_nested_emanate_task_schema_is_identical_on_both_wires():
    from lingtai.kernel.llm.base import FunctionSchema
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    schema = FunctionSchema(name="daemon", description="daemon", parameters=get_schema())
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    chat_task = chat["properties"]["input"]["anyOf"][0]["properties"]["tasks"]["items"]
    responses_task = (
        responses["properties"]["input"]["anyOf"][0]["properties"]["tasks"]["items"]
    )

    assert chat_task["properties"]["backend_options"]["description"] == (
        responses_task["properties"]["backend_options"]["description"]
    )
    assert chat_task["required"] == responses_task["required"] == ["task", "tools"]


# ---------------------------------------------------------------------------
# Cross-cutting Host surfaces
# ---------------------------------------------------------------------------

def test_daemon_is_a_migrated_family_for_the_single_central_summarizer():
    """``summarize`` is recognized for ``daemon`` by the one existing central
    summarizer — no second summarizer and no daemon-specific mechanism. A
    family advertising ``summarize`` without joining that allowlist would ship
    a model-visible control the kernel silently ignores."""
    from lingtai.kernel.tool_result_summary import summary_requested

    assert summary_requested({"summarize": True}, "daemon") is True
    assert summary_requested({"summarize": False}, "daemon") is False
    assert summary_requested({}, "daemon") is False
    # The legacy flat spelling stays honored for any historical/pending call.
    assert summary_requested({"summary": True}, "daemon") is True


def test_schema_no_longer_advertises_the_legacy_flat_summary_boolean():
    """The result-summarization control is the canonical root ``summarize``;
    the pre-migration flat ``summary`` field is off the public schema."""
    schema = get_schema()

    assert "summary" not in schema["properties"]
    for action in _ACTIONS:
        assert "summary" not in daemon_action_input_schema(action)["properties"]


def test_child_schema_ceilings_match_the_engine_constants():
    """The two engine ceilings ``_tool_family`` restates are import-time
    asserted; prove the assertion's subjects are actually equal."""
    from lingtai.tools.daemon import _tool_family

    assert _tool_family.DEFAULT_MAX_TURNS == daemon_pkg.DEFAULT_MAX_TURNS
    assert _tool_family.CHECK_LAST_MAX == daemon_pkg.DaemonManager._CHECK_LAST_MAX


def test_module_level_child_specs_are_the_single_registry_source():
    """The schema-only family and the dispatcher's handler-bound family must be
    built from one spec source, so composition and dispatch cannot drift."""
    dispatcher, _ = _dispatcher(Path("/tmp"))

    assert dispatcher._family.child_names == DAEMON_ACTIONS
    assert dispatcher._family.has_manual()
    for action, schema in (
        ("list", LIST_INPUT_SCHEMA), ("ask", ASK_INPUT_SCHEMA),
        ("check", CHECK_INPUT_SCHEMA), ("reclaim", RECLAIM_INPUT_SCHEMA),
    ):
        assert daemon_action_input_schema(action) == {
            "title": f"{action} input", **schema
        }
