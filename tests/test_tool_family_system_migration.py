"""``system``'s own LTP v2 family-migration evidence.

This is one family's local evidence set (``tools/CONTRACT.md`` "Contract
tests"), chosen for ``system``'s specific risks rather than as a conformance
suite: it is the runtime/lifecycle family, so the things worth pinning are the
privilege classes (self vs karma vs nirvana), the address rules, the exact
per-action input partition, and the preserved receipts of ``refresh`` /
``presets``.

The action surface changed once since the migration: the public ``summarize``
action LEFT for ``context`` (which split it into the explicit
``summarize``/``rebuild`` pair — evidence in
``tests/test_tool_family_context_migration.py``), and the two name actions
ARRIVED from the dissolved ``psyche``. System later opted into the generic
reserved ``settings`` action immediately before ``manual`` without changing
the operational order. Those changes are pinned below.

Every lifecycle test here drives a disposable stub or a temporary directory —
no live agent is slept, suspended, cleared, or destroyed.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from lingtai.kernel.presets import home_shortened
from lingtai.tools import system as system_tool
from lingtai.tools.system.schema import ACTION_ORDER, INPUT_SCHEMAS
from lingtai.tools.tool_family.manual import MANUAL_INPUT_SCHEMA


# ---------------------------------------------------------------------------
# Baseline inventory — the pre-migration action surface, pinned verbatim
# ---------------------------------------------------------------------------

# The exact current public enum order. Every lifecycle/preset/admin action
# keeps its pre-migration spelling and position; ``summarize`` left for
# ``context`` and the two name actions arrived from ``psyche``.
_LEGACY_ACTIONS = (
    "refresh", "sleep", "lull", "interrupt", "suspend", "cpr", "clear",
    "nirvana", "presets", "name_set", "name_nickname", "manual",
)
_PUBLIC_ACTIONS = (*_LEGACY_ACTIONS[:-1], "settings", "manual")

# The pre-migration flat sibling fields that remain, and the action each
# belonged to. Under LTP v2 each lives in exactly that action's own strict
# ``input``. ``rebuild``/``items`` left with the summarize action.
_LEGACY_FLAT_FIELDS = ("reason", "address", "preset", "revert_preset")


class _StubAgent:
    """Disposable agent double — never a live peer, never a real process."""

    def __init__(self, working_dir: Path, *, karma: bool = False, nirvana: bool = False):
        self._working_dir = working_dir
        self._admin = {"karma": karma, "nirvana": nirvana}
        self.agent_name = "stub"
        self.events: list[tuple[str, dict]] = []
        self._chat = None
        self._summarize_notification_threshold = 3000

    def _log(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _schema() -> dict:
    return system_tool.get_schema()


# ---------------------------------------------------------------------------
# 1. One model-facing root, fixed canonical children
# ---------------------------------------------------------------------------


def test_public_tool_name_and_action_inventory_adds_only_reserved_settings() -> None:
    """Operational order stays fixed; settings is inserted before manual."""
    schema = _schema()
    assert schema["properties"]["action"]["enum"] == list(_PUBLIC_ACTIONS)
    assert system_tool.DECLARATION.public_actions == _PUBLIC_ACTIONS
    assert ACTION_ORDER == _LEGACY_ACTIONS
    # Operational/manual schemas remain one source; declaration opt-in owns the
    # mechanically inserted settings branch.
    assert tuple(INPUT_SCHEMAS) == _LEGACY_ACTIONS


def test_root_envelope_is_exactly_the_four_ltp_v2_fields() -> None:
    schema = _schema()
    assert sorted(schema["properties"]) == ["action", "input", "reasoning", "summarize"]
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False
    # No legacy flat sibling survives at root.
    for field in _LEGACY_FLAT_FIELDS:
        assert field not in schema["properties"], field


def test_every_action_has_one_strict_closed_input_branch() -> None:
    schema = _schema()
    branches = schema["properties"]["input"]["anyOf"]
    public_schemas = system_tool.DECLARATION.public_input_schemas()
    assert len(branches) == len(_PUBLIC_ACTIONS)
    for action in _PUBLIC_ACTIONS:
        child = public_schemas[action]
        assert child["type"] == "object"
        assert child["additionalProperties"] is False, action
        # ``reasoning``/``summarize`` are root-only envelope controls and must
        # never appear as action input (``tools/CONTRACT.md`` Envelope).
        for reserved in ("reasoning", "_reasoning", "summarize", "action"):
            assert reserved not in child["properties"], (action, reserved)


def test_root_allof_correlates_each_action_with_its_own_input() -> None:
    schema = _schema()
    conditions = schema["allOf"]
    public_schemas = system_tool.DECLARATION.public_input_schemas()
    assert len(conditions) == len(_PUBLIC_ACTIONS)
    for action, condition in zip(_PUBLIC_ACTIONS, conditions):
        assert condition["if"]["properties"]["action"]["const"] == action
        assert condition["if"]["required"] == ["action"]
        assert condition["then"]["properties"]["input"] == public_schemas[action]


def test_children_consume_no_model_tool_slots() -> None:
    """Thirteen actions, one model-facing tool — the whole point of a family."""
    from lingtai.tools.registry import INTRINSICS

    assert "system" in INTRINSICS
    # The intrinsic registry maps one name to one module; the children are not
    # separately registered tools.
    assert not any(action in INTRINSICS for action in _PUBLIC_ACTIONS if action != "system")


# ---------------------------------------------------------------------------
# 2. Per-action input partition — each legacy flat field lands in exactly the
#    actions that actually read it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected",
    [
        ("refresh", {"reason", "preset", "revert_preset"}),
        # ``force`` was live and tested pre-migration (kernel#112 escape hatch)
        # but never advertised in the flat schema. A strict child input must
        # declare it or dispatch would reject the call that today succeeds.
        ("sleep", {"reason", "force", "delay"}),
        ("lull", {"address", "reason"}),
        ("interrupt", {"address", "reason"}),
        ("suspend", {"address", "reason"}),
        ("cpr", {"address", "reason"}),
        ("clear", {"address", "reason"}),
        ("nirvana", {"address", "reason"}),
        ("presets", set()),
        ("name_set", {"content"}),
        ("name_nickname", {"content"}),
        ("manual", set()),
    ],
)
def test_action_input_fields_match_what_the_handler_reads(action, expected) -> None:
    assert set(INPUT_SCHEMAS[action]["properties"]) == expected


def test_manual_uses_the_one_canonical_strict_empty_input() -> None:
    """The reserved child references the exported literal, not a local copy."""
    assert INPUT_SCHEMAS["manual"] == MANUAL_INPUT_SCHEMA


# ---------------------------------------------------------------------------
# 3. Dispatch — cross-action rejection happens before any handler runs
# ---------------------------------------------------------------------------


def test_unknown_action_keeps_the_pre_migration_error_verbatim(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    assert system_tool.handle(agent, {"action": "bogus", "input": {}}) == {
        "status": "error",
        "message": "Unknown system action: bogus",
    }


def test_absent_action_keeps_the_pre_migration_error_verbatim(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    assert system_tool.handle(agent, {"input": {}}) == {
        "status": "error",
        "message": "Unknown system action: None",
    }


def test_unhashable_action_does_not_raise(tmp_path: Path) -> None:
    """Issue #513 blocker class: invalid JSON can make ``action`` a list."""
    agent = _StubAgent(tmp_path)
    result = system_tool.handle(agent, {"action": [], "input": {}})
    assert result["status"] == "error"
    assert "Unknown system action" in result["message"]


def test_notification_verbs_remain_unknown_actions(tmp_path: Path) -> None:
    """``system`` owns no notification verb — before or after the migration."""
    agent = _StubAgent(tmp_path)
    for verb in ("check", "dismiss", "dismiss_channel", "dismiss_event", "dismiss_ref", "notification"):
        result = system_tool.handle(agent, {"action": verb, "input": {}})
        assert result == {"status": "error", "message": f"Unknown system action: {verb}"}
        assert verb not in _schema()["properties"]["action"]["enum"]


def test_cross_action_input_is_rejected_before_any_lifecycle_io(tmp_path: Path) -> None:
    """``address`` belongs to the karma verbs; ``sleep`` must not accept it.

    This is the safety-relevant case: a smuggled ``address`` on a non-karma
    action must fail at the envelope, with no signal file written anywhere.
    """
    target = tmp_path / "peer"
    target.mkdir()
    agent = _StubAgent(tmp_path / "self", karma=True)
    (tmp_path / "self").mkdir()

    result = system_tool.handle(
        agent, {"action": "sleep", "input": {"address": str(target)}}
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert not (target / ".sleep").exists()
    assert not (target / ".suspend").exists()
    assert agent.events == []


def test_departed_summarize_fields_are_rejected_on_every_action(tmp_path: Path) -> None:
    """``items``/``rebuild`` belong to ``context`` now, to no system action."""
    agent = _StubAgent(tmp_path)
    result = system_tool.handle(
        agent, {"action": "presets", "input": {"items": [], "rebuild": True}}
    )
    assert result["error_code"] == "INVALID_ARGUMENT"
    for schema in INPUT_SCHEMAS.values():
        assert "items" not in schema["properties"]
        assert "rebuild" not in schema["properties"]


def test_public_summarize_action_is_gone_and_fails_loudly(tmp_path: Path) -> None:
    """No compatibility alias: the old public call is an unknown action."""
    agent = _StubAgent(tmp_path)
    assert "summarize" not in ACTION_ORDER
    assert "summarize" not in INPUT_SCHEMAS
    result = system_tool.handle(
        agent,
        {"action": "summarize", "input": {"items": [], "rebuild": None}},
    )
    assert result["status"] == "error"
    assert result["message"] == "Unknown system action: summarize"


def test_name_actions_preserve_identity_semantics(tmp_path: Path) -> None:
    """The two moved actions keep their exact pre-move behavior.

    They update live in-memory identity, persist ``.agent.json``, and rewrite
    the protected prompt ``identity`` section — and they never touch the
    agent's address or working directory, which is a separate operator
    migration entirely.
    """
    from lingtai.agent import Agent
    from tests._service_helpers import make_gemini_mock_service

    workdir = tmp_path / "named"
    agent = Agent(service=make_gemini_mock_service(), working_dir=workdir)
    try:
        before_dir = agent._working_dir

        assert system_tool.handle(
            agent, {"action": "name_set", "input": {"content": "悟空"}}
        )["name"] == "悟空"
        # Set-once: a second true name is refused with the public recovery action.
        second_name = system_tool.handle(
            agent, {"action": "name_set", "input": {"content": "八戒"}}
        )
        assert "system(action='name_nickname'" in second_name["error"]
        assert "set_nickname()" not in second_name["error"]
        # Empty is refused rather than clearing the true name.
        assert "error" in system_tool.handle(
            agent, {"action": "name_set", "input": {"content": ""}}
        )

        # Nickname updates freely and clears on empty.
        for value, expected in (("monkey", "monkey"), ("king", "king"), ("", None)):
            assert system_tool.handle(
                agent, {"action": "name_nickname", "input": {"content": value}}
            )["nickname"] == expected

        # Durable identity, not just memory.
        manifest = json.loads((workdir / ".agent.json").read_text())
        assert manifest["agent_name"] == "悟空"
        assert manifest["nickname"] is None
        # The protected prompt ``identity`` section reflects the live name.
        assert "悟空" in agent._prompt_manager.read_section("identity")
        # Address / workdir are untouched by either name action — naming is
        # NOT the physical agent rename.
        assert agent._working_dir == before_dir
        assert manifest["address"] == before_dir.name
    finally:
        agent.stop(timeout=1.0)


def test_unknown_root_field_is_rejected(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    result = system_tool.handle(
        agent, {"action": "presets", "input": {}, "address": "/tmp/x"}
    )
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_non_boolean_summarize_is_rejected(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    envelope = {"action": "presets", "input": {}, "reasoning": "list them"}
    assert system_tool.handle(agent, dict(envelope, summarize=False)) == (
        system_tool.handle(agent, dict(envelope))
    )

    result = system_tool.handle(
        agent, {"action": "presets", "input": {}, "summarize": "yes"}
    )
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "summarize must be a boolean"


def test_tc_id_is_stripped_at_the_intrinsic_boundary(tmp_path: Path) -> None:
    """``base_agent._dispatch_tool`` injects ``_tc_id`` into every intrinsic."""
    agent = _StubAgent(tmp_path)
    result = system_tool.handle(
        agent, {"action": "manual", "input": {}, "_tc_id": "toolu_x"}
    )
    # Reaches the manual child rather than failing the closed-root check.
    assert result["status"] == "degraded"


# ---------------------------------------------------------------------------
# 3b. Chat Completions / Responses wire parity
# ---------------------------------------------------------------------------


def test_family_schema_survives_both_provider_wires() -> None:
    """The closed root and every action branch reach both OpenAI wires."""
    from lingtai.kernel.llm.base import FunctionSchema
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    schema = FunctionSchema(
        name="system", description=system_tool.get_description(), parameters=_schema()
    )
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    for wire in (chat, responses):
        assert wire["type"] == "object"
        assert wire["required"] == ["action", "input", "reasoning"]
        assert wire["additionalProperties"] is False
        assert set(wire["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert wire["properties"]["action"]["enum"] == list(_PUBLIC_ACTIONS)
        branches = wire["properties"]["input"]["anyOf"]
        assert [branch["title"] for branch in branches] == [
            "settings inventory input" if action == "settings" else f"{action} input"
            for action in _PUBLIC_ACTIONS
        ]
        for branch in branches:
            assert branch["additionalProperties"] is False
            for reserved in ("reasoning", "_reasoning", "summarize"):
                assert reserved not in branch["properties"]


def test_root_allof_correlation_survives_both_provider_wires() -> None:
    from lingtai.kernel.llm.base import FunctionSchema
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    schema = FunctionSchema(
        name="system", description=system_tool.get_description(), parameters=_schema()
    )
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    for wire in (chat, responses):
        conditions = wire["allOf"]
        public_schemas = system_tool.DECLARATION.public_input_schemas()
        assert len(conditions) == len(_PUBLIC_ACTIONS)
        for action, condition in zip(_PUBLIC_ACTIONS, conditions):
            assert condition["if"]["properties"]["action"]["const"] == action
            assert condition["if"]["required"] == ["action"]
            constrained = condition["then"]["properties"]["input"]
            assert set(constrained["properties"]) == set(
                public_schemas[action]["properties"]
            ), action


# ---------------------------------------------------------------------------
# 4. Privilege classes and address rules
# ---------------------------------------------------------------------------

_KARMA_VERBS = ("lull", "interrupt", "suspend", "cpr", "clear")


@pytest.mark.parametrize("action", _KARMA_VERBS)
def test_karma_verbs_refuse_without_karma(tmp_path: Path, action: str) -> None:
    agent = _StubAgent(tmp_path, karma=False)
    result = system_tool.handle(
        agent, {"action": action, "input": {"address": str(tmp_path / "peer")}}
    )
    assert result["error"] is True
    assert "requires admin.karma=True" in result["message"]


def test_nirvana_requires_both_karma_and_nirvana(tmp_path: Path) -> None:
    karma_only = _StubAgent(tmp_path, karma=True, nirvana=False)
    result = system_tool.handle(
        karma_only, {"action": "nirvana", "input": {"address": str(tmp_path / "peer")}}
    )
    assert result["error"] is True
    assert "admin.karma=True AND admin.nirvana=True" in result["message"]


@pytest.mark.parametrize("action", _KARMA_VERBS + ("nirvana",))
def test_privileged_verbs_require_an_address(tmp_path: Path, action: str) -> None:
    agent = _StubAgent(tmp_path, karma=True, nirvana=True)
    result = system_tool.handle(agent, {"action": action, "input": {}})
    assert result["error"] is True
    assert result["message"] == f"{action} requires an address"


@pytest.mark.parametrize("action", _KARMA_VERBS + ("nirvana",))
def test_privileged_verbs_refuse_self_target(tmp_path: Path, action: str) -> None:
    workdir = tmp_path / "self"
    workdir.mkdir()
    agent = _StubAgent(workdir, karma=True, nirvana=True)
    result = system_tool.handle(
        agent, {"action": action, "input": {"address": str(workdir)}}
    )
    assert result["error"] is True
    assert result["message"] == f"Cannot {action} self"


def test_self_actions_need_no_karma(tmp_path: Path) -> None:
    """Ordinary lifecycle, naming, settings, and manual are self actions."""
    from lingtai.tools.system.karma import _KARMA_ACTIONS, _NIRVANA_ACTIONS

    gated = _KARMA_ACTIONS | _NIRVANA_ACTIONS
    self_actions = {
        "sleep", "refresh", "presets", "name_set", "name_nickname", "settings", "manual",
    }
    assert self_actions.isdisjoint(gated)
    assert gated == {"lull", "interrupt", "suspend", "cpr", "clear", "nirvana"}


# ---------------------------------------------------------------------------
# 5. The reserved ``manual`` child — family-owned, discoverable, no double wrap
# ---------------------------------------------------------------------------


def test_manual_returns_the_pinned_flat_public_shape(tmp_path: Path) -> None:
    path = (
        tmp_path / ".library" / "intrinsic" / "capabilities" / "system-manual" / "SKILL.md"
    )
    path.parent.mkdir(parents=True)
    body = "# system sentinel\n"
    path.write_text(body, encoding="utf-8")

    agent = _StubAgent(tmp_path)
    assert system_tool.handle(agent, {"action": "manual", "input": {}}) == {
        "status": "ok",
        "manual": body,
        "manual_path": str(path),
    }


def test_manual_degrades_without_side_effects(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    expected_path = (
        tmp_path / ".library" / "intrinsic" / "capabilities" / "system-manual" / "SKILL.md"
    )
    assert system_tool.handle(agent, {"action": "manual", "input": {}}) == {
        "status": "degraded",
        "manual": "",
        "manual_path": str(expected_path),
        "error": (
            "system-manual manual missing — initializer may have failed or "
            "capability not installed correctly"
        ),
    }
    assert not (tmp_path / ".library").exists()


def test_manual_child_is_registered_unwrapped(tmp_path: Path) -> None:
    """``ToolFamily.handle()`` returns the canonical child result verbatim.

    The flat public shape above is a Host/presentation adaptation applied
    strictly after dispatch — proven here by calling the family directly.
    """
    path = (
        tmp_path / ".library" / "intrinsic" / "capabilities" / "system-manual" / "SKILL.md"
    )
    path.parent.mkdir(parents=True)
    path.write_text("body\n", encoding="utf-8")

    agent = _StubAgent(tmp_path)
    family = system_tool._build_family(agent)
    canonical = family.handle({"action": "manual", "input": {}})
    assert canonical == {
        "status": "ok",
        "content": [{"type": "text", "text": "body\n"}],
        "structuredContent": {"manual_path": str(path)},
    }
    # No double wrap: the canonical result is not nested under another envelope.
    assert "manual" not in canonical


def test_manual_is_reserved_and_family_owned() -> None:
    from lingtai.tools.tool_family import RESERVED_MANUAL_NAME

    assert RESERVED_MANUAL_NAME == "manual"
    family = system_tool._build_family(None)
    assert family.has_manual()
    assert family.child_names == _PUBLIC_ACTIONS


def test_duplicate_child_registration_fails_loud() -> None:
    from lingtai.tools.tool_family import ChildTool, ToolFamily, ToolFamilyError

    children = system_tool._build_children(None)
    extra = ChildTool("manual", dict(MANUAL_INPUT_SCHEMA), lambda _i: {})
    with pytest.raises(ToolFamilyError):
        ToolFamily("system", list(children) + [extra])


# ---------------------------------------------------------------------------
# 6. Risk annotations stay truthful
# ---------------------------------------------------------------------------


def test_description_states_the_three_privilege_classes() -> None:
    description = system_tool.get_description()
    assert "sleep" in description and "refresh" in description
    assert "admin.karma=True" in description
    assert "admin.nirvana=True" in description
    # Notification verbs are still explicitly disclaimed.
    assert "notification" in description.lower()


def test_action_enum_description_marks_destructive_and_readonly_actions() -> None:
    enum_description = _schema()["properties"]["action"]["description"]
    # nirvana is the irreversible one and must say so.
    assert "destroy" in enum_description
    # manual is the read-only progressive-disclosure action.
    assert "without changing runtime state" in enum_description


def test_system_is_on_the_kernel_summarize_allowlist() -> None:
    """A family advertising root ``summarize`` must be honored by the kernel."""
    from lingtai.kernel.tool_result_summary import (
        _LTP_V2_MIGRATED_FAMILIES,
        summary_requested,
    )

    assert "system" in _LTP_V2_MIGRATED_FAMILIES
    assert summary_requested({"summarize": True}, "system") is True
    assert summary_requested({"summarize": True}, "unmigrated_tool") is False


# ---------------------------------------------------------------------------
# 7. refresh / presets / summarize receipts survive the envelope unchanged
# ---------------------------------------------------------------------------


def test_refresh_preset_and_revert_conflict_is_preserved(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    result = system_tool.handle(
        agent,
        {"action": "refresh", "input": {"preset": "~/p.json", "revert_preset": True}},
    )
    assert result == {
        "status": "error",
        "message": "cannot specify both 'preset' and 'revert_preset' — choose one",
    }


def test_refresh_null_optionals_behave_as_absent(tmp_path: Path) -> None:
    """Strict provider schemas send explicit nulls for optional fields.

    ``preset: null`` must mean "no swap requested", not "swap to preset None".
    """
    calls: list[str] = []

    class _Agent(_StubAgent):
        def _perform_refresh(self) -> None:
            calls.append("refresh")

    agent = _Agent(tmp_path)
    agent._config = type("C", (), {"language": "en"})()
    result = system_tool.handle(
        agent,
        {
            "action": "refresh",
            "input": {"reason": None, "preset": None, "revert_preset": None},
        },
    )
    assert result["status"] == "ok"
    assert calls == ["refresh"]


_ACTIONABLE_VENV_ERROR = (
    "Configured venv_path is not usable: /x/.venv/bin: venv_path names the "
    "venv's 'bin' executable directory, but it must name the venv root (the "
    "directory that contains 'bin'); no python executable exists at "
    "/x/.venv/bin/bin/python. Set venv_path to /x/.venv if that is the venv root"
)


def test_refresh_launch_error_propagates_and_never_returns_ok(tmp_path: Path) -> None:
    """Human contract (Telegram 15222): an invalid configured ``venv_path``
    must surface as an actionable error from ``system(action="refresh")``;
    the family must not swallow it or masquerade success.

    ``_refresh`` returns its ``{status: "ok"}`` receipt only after the bound
    ``_perform_refresh()`` returned normally. When that call raises the
    actionable configured-venv ``RuntimeError`` (the wrapper's
    ``resolve_venv`` precheck reached through ``_build_launch_cmd``), the
    exception propagates out of the public family handler unchanged, and at
    the canonical ``ToolExecutor`` boundary it becomes a ``status: "error"``
    result whose message retains the correction guidance.
    """
    from lingtai.kernel.llm.base import ToolCall
    from lingtai.kernel.loop_guard import LoopGuard
    from lingtai.kernel.tool_executor import ToolExecutor

    calls: list[str] = []

    class _Agent(_StubAgent):
        def _perform_refresh(self) -> None:
            calls.append("refresh")
            raise RuntimeError(_ACTIONABLE_VENV_ERROR)

    agent = _Agent(tmp_path)
    agent._config = type("C", (), {"language": "en"})()

    # 1. The public family handler cannot return the success receipt.
    with pytest.raises(RuntimeError, match="must name the venv root"):
        system_tool.handle(agent, {"action": "refresh", "input": {"reason": "fix"}})
    assert calls == ["refresh"]

    # 2. Through the canonical executor the raise is a truthful error result
    #    that keeps the operator-facing correction message.
    executor = ToolExecutor(
        dispatch_fn=lambda tc: system_tool.handle(agent, tc.args),
        make_tool_result_fn=lambda name, result, **kw: result,
        guard=LoopGuard(),
        known_tools={"system"},
        parallel_safe_tools=set(),
    )
    errors: list[str] = []
    results, intercepted, _ = executor.execute(
        [ToolCall(name="system", args={"action": "refresh", "input": {}}, id="c1")],
        collected_errors=errors,
    )

    assert calls == ["refresh", "refresh"]
    assert intercepted is False
    assert len(results) == 1
    payload = results[0]
    assert payload["status"] == "error"
    assert payload["message"] == _ACTIONABLE_VENV_ERROR
    assert "Set venv_path to /x/.venv" in payload["message"]
    assert payload["error_type"] == "RuntimeError"
    assert errors == [f"system: {_ACTIONABLE_VENV_ERROR}"]


def test_presets_reports_unreadable_init_json(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    result = system_tool.handle(agent, {"action": "presets", "input": {}})
    assert result["status"] == "error"
    assert "failed to read init.json" in result["message"]


def test_presets_lists_the_allowed_library(tmp_path: Path) -> None:
    preset_path = tmp_path / "cheap.json"
    preset_body = {
        "description": {"en": "cheap"},
        "manifest": {"llm": {"provider": "openai", "model": "gpt-x"}, "capabilities": {}},
    }
    preset_path.write_text(json.dumps(preset_body), encoding="utf-8")
    (tmp_path / "init.json").write_text(
        json.dumps(
            {
                "manifest": {
                    "preset": {"active": "cheap", "allowed": [str(preset_path)]}
                }
            }
        ),
        encoding="utf-8",
    )

    class _Agent(_StubAgent):
        def load_preset(self, name):
            return preset_body

    result = system_tool.handle(_Agent(tmp_path), {"action": "presets", "input": {}})
    assert result["status"] == "ok"
    assert result["active"] == "cheap"
    assert [entry["name"] for entry in result["available"]] == [home_shortened(preset_path)]
    # Credentials are never echoed.
    assert "api_key" not in json.dumps(result)


@pytest.mark.parametrize("action", _LEGACY_ACTIONS)
def test_envelope_metadata_never_reaches_a_child_handler(
    tmp_path: Path, action: str
) -> None:
    """Root ``reasoning``/``_reasoning``/``summarize``/``_tc_id`` are not input.

    Observes the *real* registered children rather than a patched module
    attribute: each child's handler is replaced with a recorder, so what the
    generic dispatcher actually passes down is what is asserted.
    """
    seen: list[dict] = []

    from lingtai.tools.tool_family import ChildTool, ToolFamily

    children = [
        ChildTool(
            child.name,
            child.input_schema,
            lambda action_input, _seen=seen: (
                _seen.append(dict(action_input)) or {"status": "ok"}
            ),
            title=child.title,
        )
        for child in system_tool._build_children(_StubAgent(tmp_path))
    ]

    ToolFamily("system", children).handle(
        {
            "action": action,
            "input": {},
            "reasoning": "why",
            "_reasoning": "internal",
            "summarize": True,
        }
    )

    assert seen, f"the {action} child must have been dispatched"
    for reserved in ("action", "reasoning", "_reasoning", "summarize", "_tc_id"):
        assert reserved not in seen[0], (action, reserved)


# ---------------------------------------------------------------------------
# 8. Controlled, disposable lifecycle evidence — no live peer is ever touched
# ---------------------------------------------------------------------------


def _make_disposable_peer(root: Path, name: str) -> Path:
    """Create a throwaway directory that presents as an agent to the gate."""
    peer = root / name
    (peer / ".lingtai").mkdir(parents=True)
    (peer / "init.json").write_text("{}", encoding="utf-8")
    return peer


@pytest.mark.parametrize(
    "action,signal,expected_status",
    [
        ("lull", ".sleep", "asleep"),
        ("suspend", ".suspend", "suspended"),
        ("interrupt", ".interrupt", "interrupted"),
        ("clear", ".clear", "cleared"),
    ],
)
def test_karma_verbs_write_their_signal_file_on_a_disposable_target(
    tmp_path: Path, monkeypatch, action: str, signal: str, expected_status: str
) -> None:
    import lingtai.tools.system.karma as karma

    workdir = tmp_path / "agents" / "self"
    workdir.mkdir(parents=True)
    peer = _make_disposable_peer(tmp_path / "agents", "peer")

    monkeypatch.setattr(karma, "_is_agent", lambda target: True)
    monkeypatch.setattr(karma, "_is_alive", lambda target, threshold=2.0: True)

    agent = _StubAgent(workdir, karma=True)
    result = system_tool.handle(
        agent, {"action": action, "input": {"address": str(peer), "reason": "test"}}
    )

    assert result["status"] == expected_status
    assert result["address"] == str(peer)
    assert (peer / signal).is_file()
    # Only the intended signal is written.
    for other in (".sleep", ".suspend", ".interrupt", ".clear"):
        if other != signal:
            assert not (peer / other).exists(), other


def test_clear_records_the_reason_as_the_recovery_source(
    tmp_path: Path, monkeypatch
) -> None:
    import lingtai.tools.system.karma as karma

    workdir = tmp_path / "agents" / "self"
    workdir.mkdir(parents=True)
    peer = _make_disposable_peer(tmp_path / "agents", "peer")
    monkeypatch.setattr(karma, "_is_agent", lambda target: True)
    monkeypatch.setattr(karma, "_is_alive", lambda target, threshold=2.0: True)

    agent = _StubAgent(workdir, karma=True)
    result = system_tool.handle(
        agent,
        {"action": "clear", "input": {"address": str(peer), "reason": "  drifted  "}},
    )
    assert result["source"] == "drifted"
    assert (peer / ".clear").read_text(encoding="utf-8") == "drifted"


def test_clear_falls_back_to_the_caller_name_without_a_reason(
    tmp_path: Path, monkeypatch
) -> None:
    import lingtai.tools.system.karma as karma

    workdir = tmp_path / "agents" / "self"
    workdir.mkdir(parents=True)
    peer = _make_disposable_peer(tmp_path / "agents", "peer")
    monkeypatch.setattr(karma, "_is_agent", lambda target: True)
    monkeypatch.setattr(karma, "_is_alive", lambda target, threshold=2.0: True)

    agent = _StubAgent(workdir, karma=True)
    result = system_tool.handle(
        agent, {"action": "clear", "input": {"address": str(peer), "reason": None}}
    )
    assert result["source"] == "stub"


def test_cpr_refuses_a_running_target(tmp_path: Path, monkeypatch) -> None:
    import lingtai.tools.system.karma as karma

    workdir = tmp_path / "agents" / "self"
    workdir.mkdir(parents=True)
    peer = _make_disposable_peer(tmp_path / "agents", "peer")
    monkeypatch.setattr(karma, "_is_agent", lambda target: True)
    monkeypatch.setattr(karma, "_is_alive", lambda target, threshold=2.0: True)

    agent = _StubAgent(workdir, karma=True)
    result = system_tool.handle(
        agent, {"action": "cpr", "input": {"address": str(peer)}}
    )
    assert result["error"] is True
    assert "already running" in result["message"]


def test_nirvana_destroys_only_the_disposable_target(
    tmp_path: Path, monkeypatch
) -> None:
    """The one irreversible action, exercised against a throwaway directory."""
    import lingtai.tools.system.karma as karma

    workdir = tmp_path / "agents" / "self"
    workdir.mkdir(parents=True)
    peer = _make_disposable_peer(tmp_path / "agents", "doomed")
    sibling = _make_disposable_peer(tmp_path / "agents", "bystander")

    monkeypatch.setattr(karma, "_is_agent", lambda target: True)
    monkeypatch.setattr(karma, "_is_alive", lambda target, threshold=2.0: False)

    agent = _StubAgent(workdir, karma=True, nirvana=True)
    result = system_tool.handle(
        agent, {"action": "nirvana", "input": {"address": str(peer)}}
    )
    assert result == {"status": "nirvana", "address": str(peer)}
    assert not peer.exists()
    # Blast radius is exactly one directory.
    assert sibling.exists()
    assert workdir.exists()


def test_sleep_refuses_with_pending_notifications_and_force_overrides(
    tmp_path: Path,
) -> None:
    """``force`` is declared input, so both branches survive the envelope."""
    from lingtai.kernel.state import AgentState

    class _Store:
        def fingerprint(self, _allowed):
            return ("email",)

        def snapshot(self, _allowed):
            return {"email": {"header": "pending"}}

    class _Agent(_StubAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._notification_store = _Store()
            self._notification_fp = ()
            self._config = type("C", (), {"language": "en"})()
            self.state = None
            self._asleep = type("E", (), {"set": lambda s: setattrs(s), "_v": False})()
            self._cancel_event = threading.Event()
            self._slept = False

        def _request_turn_cancel(self) -> None:
            self._cancel_event.set()

        def _set_state(self, state, reason=""):
            self.state = state

    def setattrs(_s):
        return None

    refused = _Agent(tmp_path)
    result = system_tool.handle(
        refused, {"action": "sleep", "input": {"reason": "tired", "force": None, "delay": None}}
    )
    assert result["status"] == "ok"
    assert refused.state is None, "must not transition with mail waiting"

    forced = _Agent(tmp_path)
    result = system_tool.handle(
        forced, {"action": "sleep", "input": {"reason": "tired", "force": True, "delay": None}}
    )
    assert result["status"] == "ok"
    assert forced.state == AgentState.ASLEEP
    assert forced._cancel_event.is_set()
