"""``soul``'s migration onto the generic ToolFamily infra: action separation
without any change to what soul actually does.

The public tool name (``soul``) and its existing six action values (``inquiry``,
``flow``, ``config``, ``voice``, ``dismiss``, ``manual``) are unchanged; the
generic read-only ``settings`` action is additive. What moved in the original
migration is only the *argument shape*: the pre-migration flat root advertised
every action's parameters to every action at once (``inquiry`` sat next to
``delay_seconds`` next to ``prompt``), so nothing at the schema level said
which parameter belonged to which action. Post-migration each action owns one
strict, closed ``input`` object, and both the model-facing schema and dispatch
are generated from that same child registry.

These tests use an isolated temp working directory, an isolated fake agent, and
a mocked soul engine. Nothing here touches a live agent's init.json, timer,
notification state, or LLM.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.tools import soul
from lingtai.tools.soul import handle
from lingtai.tools.tool_family import ToolFamilyError

_ACTIONS = (
    "inquiry", "flow", "config", "voice", "dismiss", "settings", "manual",
)


# ---------------------------------------------------------------------------
# Isolated fixtures — no live agent, no live config, no live soul engine.
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self) -> None:
        self.consultation_past_count = 2
        self.soul_voice = "inner"
        self.soul_voice_prompt = ""
        self.language = "en"


class _FakeAgent:
    """Isolated stand-in for a real Agent.

    Owns its own temp working dir + init.json, its own fire lock, and records
    every ``_log`` event. It never starts a timer, never publishes a real
    notification, and never reaches an LLM.
    """

    def __init__(self, tmp_path: Path, *, shutdown: bool = True) -> None:
        self._working_dir = tmp_path
        (tmp_path / "init.json").write_text(
            json.dumps({"manifest": {}}), encoding="utf-8"
        )
        self._config = _FakeConfig()
        self._soul_delay = 120.0
        self._soul_fire_lock = threading.Lock()
        self._shutdown = MagicMock()
        self._shutdown.is_set.return_value = shutdown
        self.events: list[tuple[str, dict]] = []
        self.timer_restart_count = 0

    def _log(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def _start_soul_timer(self) -> None:
        self.timer_restart_count += 1

    def event_types(self) -> list[str]:
        return [name for name, _ in self.events]

    def soul_block(self) -> dict:
        data = json.loads((self._working_dir / "init.json").read_text(encoding="utf-8"))
        return data["manifest"].get("soul", {})


@pytest.fixture
def agent(tmp_path: Path) -> _FakeAgent:
    return _FakeAgent(tmp_path)


@pytest.fixture(autouse=True)
def _flow_disabled_by_default(monkeypatch):
    """Soul flow is opt-in. Every test starts from the DISABLED default, and
    the env var is only ever set inside this process's monkeypatched env —
    never on a live agent."""
    monkeypatch.delenv("LINGTAI_SOUL_FLOW_ENABLED", raising=False)


def _branch(action: str) -> dict:
    schema = soul.get_schema("en")
    return next(
        b for b in schema["properties"]["input"]["anyOf"]
        if b["title"]
        == ("settings inventory input" if action == "settings" else f"{action} input")
    )


# ---------------------------------------------------------------------------
# Schemas — all seven children, strict and action-scoped.
# ---------------------------------------------------------------------------


def test_root_is_the_closed_ltp_v2_envelope():
    schema = soul.get_schema("en")
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    # ``reasoning`` is REQUIRED Host InvocationContext/audit metadata declared
    # by the family's own schema. Agent composition (``_build_tool_schemas``)
    # only merges ``properties``, never ``required``, so a family that does not
    # declare it here would ship a model-visible-optional ``reasoning``.
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["reasoning"]["type"] == "string"
    assert schema["properties"]["summarize"]["type"] == "boolean"


def test_public_actions_include_the_additive_settings_child():
    assert soul.get_schema("en")["properties"]["action"]["enum"] == list(_ACTIONS)


def test_compact_description_retains_first_call_safety_and_routes_depth():
    description = soul.get_description()
    assert len(description) < 1500
    for required in (
        "LINGTAI_SOUL_FLOW_ENABLED",
        "disabled",
        "do not retry",
        "config tunes cadence/count only and cannot enable or disable flow",
        "inquiry is on-demand self-reflection",
        "flow is mechanical, asynchronous periodic consultation",
        "voice reads or sets the flow-voice profile",
        "cost and privacy",
    ):
        assert required in description
    # Strict child schemas own these first-call details; the root prose routes
    # to the relevant action instead of repeating them a second time.
    assert "config sends both nullable knobs with at least one non-null" not in description
    assert "voice sends both nullable fields, where null/null reads" not in description


def test_manual_router_discloses_packaged_soul_references():
    manual_dir = Path(soul.__file__).with_name("manual")
    body = manual_dir.joinpath("SKILL.md").read_text(encoding="utf-8")
    routes = {
        "reference/flow.md": "# Soul flow and opt-in gate",
        "reference/configuration.md": "# Soul configuration and voices",
        "reference/consultation.md": "# Soul consultation mechanics",
    }
    assert len(body) < 7000
    assert "## Actions and routes" in body
    assert "## Choose a path" not in body
    assert "## Reference map" not in body
    for relative, heading in routes.items():
        assert relative in body
        reference = manual_dir.joinpath(relative).read_text(encoding="utf-8")
        assert heading in reference


def test_every_action_has_its_own_strict_closed_input_branch():
    schema = soul.get_schema("en")
    branches = schema["properties"]["input"]["anyOf"]
    assert [b["title"] for b in branches] == [
        "settings inventory input" if action == "settings" else f"{action} input"
        for action in _ACTIONS
    ]
    for branch in branches:
        assert branch["type"] == "object"
        assert branch["additionalProperties"] is False
        # Envelope controls never leak into a child's input.
        assert "reasoning" not in branch["properties"]
        assert "_reasoning" not in branch["properties"]
        assert "summarize" not in branch["properties"]
        assert "action" not in branch["properties"]
        assert "_tc_id" not in branch["properties"]


def test_action_input_correlation_is_declared_at_the_schema_root():
    # One ``if``/``then`` per action correlates the sibling ``action`` const
    # with that exact action's ``input`` shape, so a provider that enforces
    # ``allOf`` can reject a cross-action pairing before the call is made.
    schema = soul.get_schema("en")
    conditions = schema["allOf"]
    assert len(conditions) == len(_ACTIONS)
    for action, condition in zip(_ACTIONS, conditions):
        assert condition["if"]["properties"]["action"]["const"] == action
        assert condition["if"]["required"] == ["action"]
        then_input = condition["then"]["properties"]["input"]
        # Same canonical child schema the ``anyOf`` branch discloses (the
        # branch only adds the presentational ``title``), so the correlation
        # layer and the disclosure layer cannot drift apart.
        assert then_input == {
            k: v for k, v in _branch(action).items() if k != "title"
        }
        assert then_input["additionalProperties"] is False
    # The inquiry condition constrains input to inquiry's own shape only.
    inquiry_then = conditions[0]["then"]["properties"]["input"]
    assert set(inquiry_then["properties"]) == {"inquiry"}
    config_then = conditions[2]["then"]["properties"]["input"]
    assert set(config_then["properties"]) == {
        "delay_seconds", "consultation_past_count",
    }


def test_action_parameters_are_no_longer_advertised_to_every_action():
    # The whole point of the migration: pre-migration these four sat at the
    # flat root, visible to (and accepted by) every action.
    root_props = soul.get_schema("en")["properties"]
    for leaked in ("inquiry", "delay_seconds", "consultation_past_count", "set", "prompt"):
        assert leaked not in root_props
    assert set(_branch("inquiry")["properties"]) == {"inquiry"}
    assert set(_branch("voice")["properties"]) == {"set", "prompt"}
    for no_input_action in ("flow", "dismiss", "settings", "manual"):
        assert _branch(no_input_action)["properties"] == {}


def test_inquiry_branch_requires_inquiry_text_at_the_schema_level():
    branch = _branch("inquiry")
    assert branch["required"] == ["inquiry"]
    assert branch["properties"]["inquiry"]["type"] == "string"


def test_optional_knobs_use_the_provider_compatible_nullable_shape():
    # Strict provider schemas express an optional field as a REQUIRED nullable
    # property; ``_strip_nulls`` turns a null back into "absent" at dispatch.
    config = _branch("config")
    assert config["required"] == ["delay_seconds", "consultation_past_count"]
    assert config["properties"]["delay_seconds"]["type"] == ["number", "null"]
    assert config["properties"]["consultation_past_count"]["type"] == ["integer", "null"]
    voice = _branch("voice")
    assert voice["required"] == ["set", "prompt"]
    assert voice["properties"]["set"]["type"] == ["string", "null"]
    assert voice["properties"]["prompt"]["type"] == ["string", "null"]
    assert voice["properties"]["prompt"]["maxLength"] == soul.SOUL_VOICE_PROMPT_MAX


# ---------------------------------------------------------------------------
# Handlers — one per action, exact preserved semantics.
# ---------------------------------------------------------------------------


def test_flow_disabled_returns_stable_status_and_burns_no_thread(agent, monkeypatch):
    """Disabled is expected config state, not an error — and the gate returns
    BEFORE the lock is touched or a fire thread is spawned."""
    started: list = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: started.append(k) or MagicMock())
    # Hold the fire lock: the gate must return without ever waiting on it.
    agent._soul_fire_lock.acquire()
    try:
        result = handle(agent, {"action": "flow", "input": {}})
    finally:
        agent._soul_fire_lock.release()

    assert result["status"] == "disabled"
    assert result["enabled"] is False
    assert result["env_var"] == "LINGTAI_SOUL_FLOW_ENABLED"
    assert "opt-in" in result["message"]
    assert "error" not in result
    assert started == []
    assert agent.event_types() == ["soul_flow_voluntary_disabled"]


def test_no_action_or_input_field_can_enable_flow(agent):
    """There is no model-reachable enable path: the gate is the operator's env
    var, and no child input schema carries an enable-shaped field."""
    schema = soul.get_schema("en")
    every_input_field = {
        field
        for branch in schema["properties"]["input"]["anyOf"]
        for field in branch["properties"]
    }
    assert every_input_field == {
        "inquiry", "delay_seconds", "consultation_past_count", "set", "prompt",
    }
    # config tunes cadence and truthfully reports that flow is still disabled.
    result = handle(
        agent,
        {"action": "config", "input": {"delay_seconds": 300, "consultation_past_count": None}},
    )
    assert result["status"] == "ok"
    assert result["soul_flow_enabled"] is False
    assert "LINGTAI_SOUL_FLOW_ENABLED" in result["note"]
    # And flow is still disabled afterwards.
    assert handle(agent, {"action": "flow", "input": {}})["status"] == "disabled"


def test_flow_enabled_acknowledges_and_defers_the_voices(agent, monkeypatch):
    monkeypatch.setenv("LINGTAI_SOUL_FLOW_ENABLED", "1")
    spawned: list = []

    class _FakeThread:
        def __init__(self, **kwargs):
            spawned.append(kwargs["name"])

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", lambda **kw: _FakeThread(**kw))
    result = handle(agent, {"action": "flow", "input": {}})

    assert result["status"] == "ok"
    assert "Soul flow triggered" in result["message"]
    assert spawned == ["soul-flow-voluntary"]
    assert "soul_flow_voluntary_triggered" in agent.event_types()


def test_flow_rejects_a_second_fire_while_one_is_in_flight(agent, monkeypatch):
    monkeypatch.setenv("LINGTAI_SOUL_FLOW_ENABLED", "1")
    agent._soul_fire_lock.acquire()
    try:
        result = handle(agent, {"action": "flow", "input": {}})
    finally:
        agent._soul_fire_lock.release()

    assert result == {"error": "soul flow ongoing, request rejected"}
    assert "soul_flow_voluntary_rejected" in agent.event_types()


def test_inquiry_runs_the_mocked_engine_and_persists_the_entry(agent, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        soul, "soul_inquiry", lambda _agent, text: calls.append(text) or {"voice": "look up"},
    )
    agent._persist_soul_entry = MagicMock()

    result = handle(agent, {"action": "inquiry", "input": {"inquiry": "  am I stuck?  "}})

    assert result == {"status": "ok", "voice": "look up"}
    assert calls == ["am I stuck?"]
    assert agent._persist_soul_entry.call_args.kwargs == {"mode": "inquiry"}
    assert agent.event_types() == ["soul_inquiry", "soul_inquiry_done"]


def test_inquiry_silence_is_still_a_success(agent, monkeypatch):
    monkeypatch.setattr(soul, "soul_inquiry", lambda _agent, _text: None)
    result = handle(agent, {"action": "inquiry", "input": {"inquiry": "hello?"}})
    assert result == {"status": "ok", "voice": "(silence)"}


@pytest.mark.parametrize("bad_input", [{}, {"inquiry": "   "}, {"inquiry": None}])
def test_inquiry_requires_inquiry_text(agent, monkeypatch, bad_input):
    monkeypatch.setattr(
        soul, "soul_inquiry",
        lambda *_a, **_k: pytest.fail("no LLM call for a missing inquiry"),
    )
    result = handle(agent, {"action": "inquiry", "input": bad_input})
    assert result == {"error": "inquiry is required — what do you want to reflect on?"}
    assert agent.events == []


def test_config_requires_at_least_one_knob(agent):
    both_null = handle(
        agent,
        {"action": "config", "input": {"delay_seconds": None, "consultation_past_count": None}},
    )
    assert both_null == {
        "error": "config requires at least one of: delay_seconds, consultation_past_count.",
    }
    assert agent._soul_delay == 120.0
    assert agent.timer_restart_count == 0
    assert agent.soul_block() == {}


@pytest.mark.parametrize(
    "action_input, unchanged",
    [
        ({"delay_seconds": 29.9, "consultation_past_count": None}, "delay"),
        ({"delay_seconds": "fast", "consultation_past_count": None}, "delay"),
        ({"delay_seconds": float("nan"), "consultation_past_count": None}, "delay"),
        ({"delay_seconds": None, "consultation_past_count": 6}, "count"),
        ({"delay_seconds": None, "consultation_past_count": -1}, "count"),
        ({"delay_seconds": None, "consultation_past_count": "two"}, "count"),
    ],
)
def test_config_validates_ranges_and_types_without_persisting(agent, action_input, unchanged):
    result = handle(agent, {"action": "config", "input": action_input})
    assert "error" in result
    assert agent._soul_delay == 120.0
    assert agent._config.consultation_past_count == 2
    assert agent.timer_restart_count == 0
    assert agent.soul_block() == {}


def test_config_persists_through_the_existing_owner(agent):
    result = handle(
        agent,
        {"action": "config", "input": {"delay_seconds": 300, "consultation_past_count": 4}},
    )
    assert result["status"] == "ok"
    assert result["old"] == {"delay_seconds": 120.0, "consultation_past_count": 2}
    assert result["new"] == {"delay_seconds": 300.0, "consultation_past_count": 4}
    assert agent._soul_delay == 300.0
    assert agent._config.consultation_past_count == 4
    # Persisted by ``config._persist_soul_config`` into ``manifest.soul``, the
    # same owner as before the migration.
    assert agent.soul_block() == {"delay": 300.0, "consultation_past_count": 4}


def test_config_restarts_the_timer_only_when_delay_changed(tmp_path):
    live = _FakeAgent(tmp_path, shutdown=False)
    handle(live, {"action": "config", "input": {"delay_seconds": 300, "consultation_past_count": None}})
    assert live.timer_restart_count == 1
    handle(live, {"action": "config", "input": {"delay_seconds": None, "consultation_past_count": 1}})
    assert live.timer_restart_count == 1


def test_voice_read_reports_current_profile_without_changing_it(agent):
    result = handle(agent, {"action": "voice", "input": {"set": None, "prompt": None}})
    assert result["status"] == "ok"
    assert result["current"] == "inner"
    assert result["available"] == list(soul.SOUL_VOICE_BUILTINS)
    assert isinstance(result["prompt"], str) and result["prompt"]
    assert agent._config.soul_voice == "inner"
    assert agent.soul_block() == {}


def test_voice_set_switches_preset_and_persists(agent):
    result = handle(agent, {"action": "voice", "input": {"set": "observer", "prompt": None}})
    assert result == {"status": "ok", "old": "inner", "new": "observer"}
    assert agent._config.soul_voice == "observer"
    assert agent.soul_block() == {"voice": "observer"}


def test_voice_custom_requires_a_prompt_and_persists_it(agent):
    missing = handle(agent, {"action": "voice", "input": {"set": "custom", "prompt": None}})
    assert "requires a non-empty 'prompt'" in missing["error"]
    assert agent._config.soul_voice == "inner"

    result = handle(
        agent, {"action": "voice", "input": {"set": "custom", "prompt": "speak plainly"}},
    )
    assert result == {"status": "ok", "old": "inner", "new": "custom"}
    assert agent._config.soul_voice_prompt == "speak plainly"
    assert agent.soul_block() == {"voice": "custom", "voice_prompt": "speak plainly"}

    # Switching back to a preset clears the stored custom prompt so it cannot
    # silently re-activate later.
    handle(agent, {"action": "voice", "input": {"set": "inner", "prompt": None}})
    assert agent._config.soul_voice_prompt == ""
    assert agent.soul_block() == {"voice": "inner"}


@pytest.mark.parametrize(
    "action_input",
    [
        {"set": "nope", "prompt": None},
        {"set": "   ", "prompt": None},
        {"set": "custom", "prompt": "x" * (soul.SOUL_VOICE_PROMPT_MAX + 1)},
    ],
)
def test_voice_rejects_invalid_sets_without_persisting(agent, action_input):
    result = handle(agent, {"action": "voice", "input": action_input})
    assert "error" in result
    assert agent._config.soul_voice == "inner"
    assert agent.soul_block() == {}


def test_dismiss_clears_only_the_soul_channel(agent, monkeypatch):
    seen: list = []

    def _fake_dismiss(agent_, channel, *, invoked_by, **kwargs):
        seen.append((channel, invoked_by, kwargs))
        return {"status": "ok", "channel": channel, "cleared": True}

    monkeypatch.setattr("lingtai.kernel.notifications.dismiss_channel", _fake_dismiss)
    result = handle(agent, {"action": "dismiss", "input": {}})

    assert seen == [("soul", "soul", {})]
    assert result["status"] == "ok"
    assert result["channel"] == "soul"
    assert result["message"] == "Soul flow notification dismissed."


def test_dismiss_does_not_overwrite_a_message_from_the_shared_helper(agent, monkeypatch):
    monkeypatch.setattr(
        "lingtai.kernel.notifications.dismiss_channel",
        lambda *_a, **_k: {"status": "ok", "channel": "soul", "message": "already clear"},
    )
    result = handle(agent, {"action": "dismiss", "input": {}})
    assert result["message"] == "already clear"


# ---------------------------------------------------------------------------
# Manual — full body, host-local path, no soul operation, no double wrap.
# ---------------------------------------------------------------------------


def _install_manual(agent: _FakeAgent, body: str) -> Path:
    path = (
        agent._working_dir / ".library" / "intrinsic" / "capabilities"
        / "soul-manual" / "SKILL.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path



def _bare_manual_installer(tmp_path):
    """Minimal owner for exercising Agent's file-only manual installation seam."""
    from lingtai.agent import Agent

    agent = object.__new__(Agent)
    agent._working_dir = tmp_path / "agent"
    agent._capabilities = []
    return agent


def _patched_manual_roots(tmp_path, monkeypatch):
    import lingtai.intrinsic_skills as intrinsic_skills_pkg
    import lingtai.tools as tools_pkg

    tools_root = tmp_path / "tools"
    skills_root = tmp_path / "intrinsic_skills"
    monkeypatch.setattr(tools_pkg, "__file__", str(tools_root / "__init__.py"))
    monkeypatch.setattr(
        intrinsic_skills_pkg, "__file__", str(skills_root / "__init__.py")
    )
    return tools_root, skills_root


def _write_skill_dir(path: Path, body: str) -> None:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(body, encoding="utf-8")



def test_installer_rejects_second_operational_soul_owner(tmp_path, monkeypatch):
    """Two operational sources for the soul-manual destination fail loudly."""
    tools_root, skills_root = _patched_manual_roots(tmp_path, monkeypatch)
    _write_skill_dir(tools_root / "soul" / "manual", "canonical soul\n")
    _write_skill_dir(
        skills_root / "soul-manual", "A second operational soul manual.\n"
    )

    with pytest.raises(RuntimeError, match="collision"):
        _bare_manual_installer(tmp_path)._install_intrinsic_manuals()

def test_manual_returns_the_full_body_and_host_local_path(agent):
    body = "# soul manual\n\nfull body, not a summary.\n"
    path = _install_manual(agent, body)

    result = handle(agent, {"action": "manual", "input": {}})

    assert result == {"status": "ok", "manual": body, "manual_path": str(path)}


def test_manual_result_is_not_double_wrapped(agent):
    _install_manual(agent, "# soul manual\n")
    result = handle(agent, {"action": "manual", "input": {}})
    # Soul's public manual shape is the flat pre-migration one; the generic
    # child's canonical ``content``/``structuredContent`` envelope is adapted
    # by the Host after dispatch, never nested inside another result.
    assert "content" not in result
    assert "structuredContent" not in result
    assert not any(isinstance(v, dict) and "status" in v for v in result.values())


def test_manual_reports_a_missing_manual_truthfully(agent):
    result = handle(agent, {"action": "manual", "input": {}})
    assert result["status"] == "degraded"
    assert result["manual"] == ""
    assert result["manual_path"].endswith("soul-manual/SKILL.md")
    assert "manual missing" in result["error"]


def test_manual_performs_no_soul_operation(agent, monkeypatch):
    _install_manual(agent, "# soul manual\n")
    monkeypatch.setattr(
        soul, "soul_inquiry", lambda *_a, **_k: pytest.fail("manual must not consult"),
    )
    monkeypatch.setattr(
        "lingtai.kernel.notifications.dismiss_channel",
        lambda *_a, **_k: pytest.fail("manual must not dismiss"),
    )
    before_delay = agent._soul_delay
    before_voice = agent._config.soul_voice

    handle(agent, {"action": "manual", "input": {}})

    # No timer restart, no config/voice mutation, no init.json write, no log
    # event, and the fire lock is untouched.
    assert agent.timer_restart_count == 0
    assert agent._soul_delay == before_delay
    assert agent._config.soul_voice == before_voice
    assert agent.soul_block() == {}
    assert agent.events == []
    assert agent._soul_fire_lock.acquire(blocking=False)
    agent._soul_fire_lock.release()


# ---------------------------------------------------------------------------
# Envelope enforcement — dispatch is the authoritative boundary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action, foreign_input",
    [
        ("inquiry", {"delay_seconds": 300}),
        ("inquiry", {"set": "observer"}),
        ("config", {"inquiry": "hi"}),
        ("config", {"prompt": "x"}),
        ("voice", {"consultation_past_count": 1}),
        ("flow", {"delay_seconds": 300}),
        ("dismiss", {"inquiry": "hi"}),
        ("settings", {"set": "voice"}),
        ("manual", {"inquiry": "hi"}),
    ],
)
def test_cross_action_input_is_rejected_before_any_handler_io(agent, monkeypatch, action, foreign_input):
    monkeypatch.setattr(
        soul, "soul_inquiry", lambda *_a, **_k: pytest.fail("no handler I/O on a rejected call"),
    )
    monkeypatch.setattr(
        "lingtai.kernel.notifications.dismiss_channel",
        lambda *_a, **_k: pytest.fail("no handler I/O on a rejected call"),
    )
    result = handle(agent, {"action": action, "input": foreign_input})

    assert result == {
        "status": "failed",
        "error_code": "INVALID_ARGUMENT",
        "message": "unsupported soul input field",
    }
    # Nothing ran: no state change, no persistence, no log event.
    assert agent._soul_delay == 120.0
    assert agent._config.soul_voice == "inner"
    assert agent.timer_restart_count == 0
    assert agent.soul_block() == {}
    assert agent.events == []


def test_unknown_action_preserves_souls_own_error(agent):
    result = handle(agent, {"action": "meditate", "input": {}})
    assert result == {
        "error": (
            "Unknown soul action: meditate. Use inquiry, config, voice, "
            "dismiss, settings, manual, or wait for flow (mechanical)."
        )
    }


def test_missing_action_preserves_souls_own_error(agent):
    assert "Unknown soul action" in handle(agent, {"input": {}})["error"]


def test_unknown_root_field_is_rejected(agent):
    result = handle(agent, {"action": "flow", "input": {}, "engine": "x"})
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "unsupported soul argument"


def test_input_must_be_an_object(agent):
    result = handle(agent, {"action": "flow", "input": "everything"})
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "input must be an object"


def test_summarize_is_type_checked_and_never_reaches_a_handler(agent, monkeypatch):
    bad = handle(agent, {"action": "flow", "input": {}, "summarize": "yes"})
    assert bad["error_code"] == "INVALID_ARGUMENT"
    assert bad["message"] == "summarize must be a boolean"

    seen: list = []
    monkeypatch.setattr(
        soul, "soul_inquiry",
        lambda _agent, text: seen.append(text) or {"voice": "ok"},
    )
    agent._persist_soul_entry = MagicMock()
    ok = handle(
        agent,
        {"action": "inquiry", "input": {"inquiry": "why?"}, "summarize": True, "reasoning": "r"},
    )
    assert ok == {"status": "ok", "voice": "ok"}
    assert seen == ["why?"]


def test_reasoning_and_tc_id_never_reach_a_child_handler(agent, monkeypatch):
    """``reasoning`` is Host audit metadata; ``_tc_id`` is kernel transport
    metadata injected into every intrinsic's args by ``_dispatch_tool``.
    Neither is action input, and neither may trip the closed-root check."""
    captured: list[dict] = []

    def _capture(_agent, action_input):
        captured.append(dict(action_input))
        return {"status": "ok", "old": {}, "new": {}}

    monkeypatch.setattr(soul, "_handle_config", _capture)
    result = handle(
        agent,
        {
            "action": "config",
            "input": {"delay_seconds": 300, "consultation_past_count": None},
            "reasoning": "tune cadence",
            "_reasoning": "internal",
            "summarize": False,
            "_tc_id": "tc_42",
        },
    )

    assert result["status"] == "ok"
    assert captured == [{"delay_seconds": 300}]


def test_synthesized_involuntary_flow_pair_uses_the_current_envelope(agent, monkeypatch):
    """The involuntary flow pair is replayed to the provider as an assistant
    tool_use block — a model-visible example of how to call ``soul``. If it
    carried the pre-migration flat ``{"action": "flow"}``, a model imitating
    its own history would send a shape the closed schema now rejects."""
    from lingtai.tools.soul import build_consultation_pair
    from lingtai.tools.soul.consultation import INVOLUNTARY_FLOW_REASONING

    agent._config.language = "en"
    call, result = build_consultation_pair(agent, [{"source": "x", "voice": "y"}])

    assert call.name == "soul"
    assert call.args == {
        "action": "flow",
        "input": {},
        "reasoning": INVOLUNTARY_FLOW_REASONING,
    }
    assert call.id == result.id
    # The synthesized args validate against the live schema's root contract.
    schema = soul.get_schema("en")
    assert set(call.args) <= set(schema["properties"])
    for required in schema["required"]:
        assert required in call.args
    # And they survive the real dispatcher rather than failing INVALID_ARGUMENT
    # the way the flat shape now would.
    monkeypatch.delenv("LINGTAI_SOUL_FLOW_ENABLED", raising=False)
    dispatched = handle(agent, dict(call.args))
    assert dispatched["status"] == "disabled"
    assert dispatched.get("error_code") != "INVALID_ARGUMENT"


def test_flat_pre_migration_flow_args_are_now_rejected(agent):
    """Guards the reason B1 mattered: the shape the old synthesized pair
    taught is exactly the shape the closed envelope now refuses."""
    assert handle(agent, {"action": "flow"})["error_code"] == "INVALID_ARGUMENT"


def test_schema_and_dispatch_come_from_one_registry(agent):
    """There is no second child list to drift: the module-level schema-only
    family and the per-call agent-bound family are built by the same
    declaration plus reserved settings/manual children."""

    schema_names = tuple(
        (
            "settings"
            if b["title"] == "settings inventory input"
            else b["title"].removesuffix(" input")
        )
        for b in soul.get_schema("en")["properties"]["input"]["anyOf"]
    )
    dispatch_names = tuple(child.name for child in soul._build_children(agent))
    assert schema_names == dispatch_names == _ACTIONS


def test_reserved_manual_schema_is_owned_by_tool_family(agent):
    """Soul declares no manual input schema of its own — the generic builder
    owns both that schema and the handler that serves it."""
    from lingtai.tools.tool_family.manual import MANUAL_INPUT_SCHEMA

    assert not hasattr(soul, "_MANUAL_INPUT_SCHEMA")
    branch = _branch("manual")
    assert {k: v for k, v in branch.items() if k != "title"} == MANUAL_INPUT_SCHEMA


def test_soul_is_a_migrated_ltp_v2_family_for_the_root_summarize_control():
    from lingtai.kernel.tool_result_summary import summary_requested

    assert summary_requested({"summarize": True}, tool_name="soul") is True
    assert summary_requested({"summarize": False}, tool_name="soul") is False
    # Scoped by name: an unmigrated tool's own ``summarize``-named domain field
    # is never reinterpreted as this cross-cutting control. ``psyche`` and
    # ``system`` were each the example here until they migrated onto the same
    # envelope and joined the allowlist; every currently registered family is
    # now migrated, so this negative control names a non-allowlisted tool
    # directly rather than tracking whichever family migrates next.
    assert summary_requested({"summarize": True}, tool_name="unmigrated_tool") is False


# ---------------------------------------------------------------------------
# Wiring — one public tool, both provider wires, no double registration.
# ---------------------------------------------------------------------------


def test_soul_is_registered_exactly_once_as_an_intrinsic():
    from lingtai.tools.registry import INTRINSICS

    assert INTRINSICS["soul"]["module"] is soul
    assert sum(1 for name in INTRINSICS if name == "soul") == 1
    # The migration adds no second model-facing root for any soul action.
    from lingtai.tools.registry import BUILTIN_TOOLS

    for action in _ACTIONS:
        assert action not in BUILTIN_TOOLS
    assert "soul" not in BUILTIN_TOOLS


def test_agent_composition_keeps_reasoning_required_on_both_wires(tmp_path):
    from lingtai.agent import Agent
    from lingtai.kernel.base_agent.tools import _build_tool_schemas
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools
    from tests._service_helpers import make_gemini_mock_service as make_mock_service

    live = Agent(
        service=make_mock_service(),
        agent_name="soul-family-wire-test",
        working_dir=tmp_path,
    )
    try:
        schemas = _build_tool_schemas(live)
        soul_schemas = [s for s in schemas if s.name == "soul"]
        assert len(soul_schemas) == 1
        chat = _build_tools(soul_schemas)[0]["function"]["parameters"]
        responses = _build_responses_tools(soul_schemas)[0]["parameters"]
        prompt = live._build_system_prompt()
        assert isinstance(prompt, str) and prompt
        for wire in (chat, responses):
            assert set(wire["properties"]) == {
                "action", "input", "reasoning", "summarize",
            }
            # Agent composition re-injects the identical ``reasoning``
            # property text but never touches ``required`` — the family's own
            # schema is what keeps it required.
            assert wire["required"] == ["action", "input", "reasoning"]
            assert wire["properties"]["action"]["enum"] == list(_ACTIONS)
            branches = wire["properties"]["input"]["anyOf"]
            assert [b["title"] for b in branches] == [
                "settings inventory input" if action == "settings" else f"{action} input"
                for action in _ACTIONS
            ]
    finally:
        live.stop(timeout=1.0)
