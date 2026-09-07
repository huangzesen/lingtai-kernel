"""Focused acceptance for the action-separated public ``vision`` family.

``vision`` keeps its public tool name and operational actions while generic
composition inserts ``settings`` immediately before ``manual`` in the LTP v2 envelope
(``action``/``input``/``reasoning``/``summarize``) composed and dispatched by
the generic ``lingtai.tools.tool_family`` infrastructure. These tests pin the
migration's own promises: exactly one public model root, every child schema and
handlers, envelope/cross-action rejection strictly *before* any provider I/O,
a manual route that constructs and calls no provider, the exact preserved
success/failure result shapes, and Chat/Responses wire parity with no double
wrapping of the manual child's canonical result.
"""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

from lingtai.services.vision import VisionService
from lingtai.tools import vision as vision_tool
from lingtai.tools.vision import VisionManager, get_schema, setup


class _StubAgent:
    """Minimal controlled host for the Vision declaration tests."""

    def __init__(self, working_dir: Path):
        self._working_dir = working_dir
        self.service = None
        self.tools: dict[str, dict] = {}
        self._official_tool_plugins: dict[str, object] = {}

    @property
    def working_dir(self) -> Path:
        return self._working_dir

    @property
    def official_tool_plugins(self):
        return MappingProxyType(self._official_tool_plugins)

    def update_system_prompt(self, *_args, **_kwargs) -> None:
        return None

    def _authorize_official_tool_declaration(self, _declaration) -> None:
        return None

    def _record_official_tool_binding(self, _declaration, _plugin) -> None:
        return None

    def _mount_official_tool(self, transaction) -> None:
        transaction.consume()
        plugin = transaction.plugin
        self.add_tool(
            plugin.name,
            schema=plugin.schema,
            handler=plugin.handler,
            description=plugin.description,
            glossary_package=plugin.glossary_package,
        )
        transaction.mark_mounted(self)

    def _claim_official_tool(self, transaction) -> None:
        self._official_tool_plugins[transaction.declaration.name] = transaction.declaration

    def add_tool(self, name: str, **kwargs) -> None:
        self.tools[name] = kwargs


class _Workdir:
    def __init__(self, path: Path):
        self.path = path


class _ActiveProvider:
    def __init__(self, agent: _StubAgent):
        self._agent = agent

    @property
    def service(self):
        return self._agent.service


def _bound_manager(agent: _StubAgent, service=None, manual_reason: str = "") -> VisionManager:
    return VisionManager(
        _Workdir(agent.working_dir),
        _ActiveProvider(agent),
        vision_service=service,
        manual_reason=manual_reason,
    )


def _install_manual(workdir: Path) -> tuple[str, Path]:
    path = workdir / ".library" / "intrinsic" / "capabilities" / "vision" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "---\nname: vision-manual\n---\n\n# vision sentinel\n"
    path.write_text(body, encoding="utf-8")
    return body, path


def _never_called_service() -> MagicMock:
    """A vision service that fails the test if any provider I/O is attempted."""
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.side_effect = AssertionError(
        "provider I/O must not run for this call"
    )
    return svc


def _manager(tmp_path: Path, service=None) -> VisionManager:
    return _bound_manager(_StubAgent(tmp_path), service=service)


# ---------------------------------------------------------------------------
# Public registration: one model root, unchanged public name
# ---------------------------------------------------------------------------


def test_setup_registers_exactly_one_public_vision_root(tmp_path):
    agent = _StubAgent(tmp_path)
    setup(agent, vision_service=MagicMock(spec=VisionService))

    assert list(agent.tools) == ["vision"]
    assert agent.tools["vision"]["schema"] == get_schema()
    assert agent.tools["vision"]["glossary_package"] == "lingtai.tools.vision"


def test_public_actions_insert_settings_immediately_before_manual():
    schema = get_schema()
    assert schema["properties"]["action"]["enum"] == [
        "analyze", "check", "list", "settings", "manual"
    ]


def test_module_docs_distinguish_preserved_actions_from_new_settings_action():
    assert "operational action values are unchanged" in vision_tool.__doc__
    assert "new\nreserved ``settings`` action" in vision_tool.__doc__
    assert "public tool name and action values are unchanged" not in vision_tool.__doc__


# ---------------------------------------------------------------------------
# Root schema: strict envelope with action <-> input correlation
# ---------------------------------------------------------------------------


def test_root_schema_is_the_strict_ltp_v2_envelope():
    schema = get_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["action", "input", "reasoning"]
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert schema["properties"]["reasoning"]["type"] == "string"
    assert schema["properties"]["summarize"]["type"] == "boolean"


def test_root_schema_correlates_each_action_const_with_its_own_input():
    schema = get_schema()
    conditions = {
        cond["if"]["properties"]["action"]["const"]: cond["then"]["properties"]["input"]
        for cond in schema["allOf"]
    }
    assert set(conditions) == {"analyze", "check", "list", "settings", "manual"}
    assert set(conditions["analyze"]["properties"]) == {"image_path", "question", "preset"}
    assert conditions["list"]["properties"] == {}
    assert conditions["manual"]["properties"] == {}
    for cond in schema["allOf"]:
        # A strict ``if`` with a missing property matches vacuously; the guard
        # keeps each branch scoped to its own action.
        assert cond["if"]["required"] == ["action"]


def test_all_child_input_schemas_are_exposed_before_invocation():
    branches = get_schema()["properties"]["input"]["anyOf"]
    assert [b["title"] for b in branches] == [
        "analyze input",
        "check input",
        "list input",
        "settings inventory input",
        "manual input",
    ]

    analyze_branch, check_branch, list_branch, settings_branch, manual_branch = branches
    assert analyze_branch["required"] == ["image_path", "question"]
    assert analyze_branch["additionalProperties"] is False
    assert analyze_branch["properties"]["image_path"]["type"] == "string"
    # Optional ``question`` is a required nullable property, matching the
    # strict-object convention the other migrated families use.
    assert analyze_branch["properties"]["question"]["type"] == ["string", "null"]

    # Optional ``preset`` on analyze is a nullable property.
    assert analyze_branch["properties"]["preset"]["type"] == ["string", "null"]

    # check takes only the optional preset; no image fields.
    assert set(check_branch["properties"]) == {"preset"}
    assert check_branch["additionalProperties"] is False

    # list is a strict empty object: no fields, nothing to smuggle in.
    assert list_branch["properties"] == {}
    assert list_branch["additionalProperties"] is False

    assert settings_branch["properties"] == {}
    assert settings_branch["required"] == []
    assert settings_branch["additionalProperties"] is False

    assert manual_branch["properties"] == {}
    assert manual_branch["additionalProperties"] is False


@pytest.mark.parametrize("reasoning", ["chat-wire", "responses-wire"])
def test_reasoning_and_summarize_never_leak_into_child_input(tmp_path, reasoning):
    schema = get_schema()
    for branch in schema["properties"]["input"]["anyOf"]:
        assert not {"reasoning", "_reasoning", "summarize", "action"} & set(
            branch["properties"]
        )
    for cond in schema["allOf"]:
        assert not {"reasoning", "_reasoning", "summarize", "action"} & set(
            cond["then"]["properties"]["input"]["properties"]
        )

    svc = MagicMock(spec=VisionService)
    svc.analyze_image.return_value = "same answer"
    img = tmp_path / "x.png"
    img.write_bytes(b"fake")

    result = _manager(tmp_path, svc).handle(
        {
            "action": "analyze",
            "input": {"image_path": str(img), "question": "Q"},
            "reasoning": reasoning,
            "summarize": True,
        }
    )
    assert result == {"status": "ok", "analysis": "same answer"}
    svc.analyze_image.assert_called_once_with(str(img), prompt="Q")


# ---------------------------------------------------------------------------
# Dispatch: envelope/cross-action failures land before any provider I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"reasoning": "r"}, id="missing-action"),
        pytest.param({"action": "look", "input": {}, "reasoning": "r"}, id="unknown-action"),
        pytest.param(
            {"action": "analyze", "input": "not-an-object", "reasoning": "r"},
            id="input-not-object",
        ),
        pytest.param(
            {"action": "analyze", "input": {"image_path": "x.png", "question": None}, "reasoning": "r", "engine": "x"},
            id="unknown-root-field",
        ),
        pytest.param(
            {"action": "analyze", "input": {"image_path": "x.png", "question": None}, "reasoning": "r", "summarize": "yes"},
            id="non-boolean-summarize",
        ),
    ],
)
def test_invalid_envelope_fails_before_provider_io(tmp_path, args):
    mgr = _manager(tmp_path, _never_called_service())
    result = mgr.handle(args)
    assert result["status"] == "error"
    assert isinstance(result["message"], str) and result["message"]


def test_cross_action_input_is_rejected_before_provider_io(tmp_path):
    """``manual``'s strict-empty input cannot smuggle in analyze's fields."""
    mgr = _manager(tmp_path, _never_called_service())
    result = mgr.handle(
        {"action": "manual", "input": {"image_path": "x.png"}, "reasoning": "r"}
    )
    assert result["status"] == "error"
    assert "manual" not in result  # never reached the manual child either


def test_unknown_analyze_input_field_is_rejected_before_provider_io(tmp_path):
    mgr = _manager(tmp_path, _never_called_service())
    result = mgr.handle(
        {
            "action": "analyze",
            "input": {"image_path": "x.png", "question": None, "action": "manual"},
            "reasoning": "r",
        }
    )
    assert result["status"] == "error"


def test_root_summarize_is_stripped_and_never_reaches_the_child(tmp_path):
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.return_value = "a chart"
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG fake")

    mgr = _manager(tmp_path, svc)
    result = mgr.handle(
        {
            "action": "analyze",
            "input": {"image_path": str(img), "question": "What is this?"},
            "reasoning": "read the chart",
            "summarize": True,
        }
    )
    assert result == {"status": "ok", "analysis": "a chart"}
    svc.analyze_image.assert_called_once_with(str(img), prompt="What is this?")


# ---------------------------------------------------------------------------
# analyze: exact preserved success/failure shapes and routing
# ---------------------------------------------------------------------------


def test_analyze_success_shape_is_exact(tmp_path):
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.return_value = "A dog in the park"
    img = tmp_path / "dog.jpg"
    img.write_bytes(b"\xff\xd8\xff fake jpeg")

    result = _manager(tmp_path, svc).handle(
        {
            "action": "analyze",
            "input": {"image_path": str(img), "question": "What animal?"},
            "reasoning": "identify the animal",
        }
    )
    assert result == {"status": "ok", "analysis": "A dog in the park"}
    svc.analyze_image.assert_called_once_with(str(img), prompt="What animal?")


def test_analyze_null_question_uses_the_unchanged_default_prompt(tmp_path):
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.return_value = "An image"
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG fake")

    _manager(tmp_path, svc).handle(
        {
            "action": "analyze",
            "input": {"image_path": "photo.png", "question": None},
            "reasoning": "describe it",
        }
    )
    svc.analyze_image.assert_called_once_with(
        str(img), prompt="Describe what you see in this image."
    )


@pytest.mark.parametrize(
    ("image_path", "expected_fragment"),
    [
        pytest.param("", "Provide image_path", id="empty-path"),
        pytest.param("/nonexistent/img.png", "Image file not found", id="missing-file"),
    ],
)
def test_analyze_input_failures_keep_their_exact_messages(
    tmp_path, image_path, expected_fragment
):
    svc = _never_called_service()
    result = _manager(tmp_path, svc).handle(
        {
            "action": "analyze",
            "input": {"image_path": image_path, "question": None},
            "reasoning": "r",
        }
    )
    assert result["status"] == "error"
    assert expected_fragment in result["message"]


@pytest.mark.parametrize(
    ("provider_error", "forbidden_fragments"),
    [
        pytest.param(
            "token=secret https://user:pw@example.test/v1",
            ("secret", "example.test"),
            id="secret-shaped-error",
        ),
        pytest.param("API down", ("API down",), id="plain-provider-error"),
    ],
)
def test_analyze_request_failure_is_sanitized_and_points_to_manual(
    tmp_path, provider_error, forbidden_fragments
):
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.side_effect = RuntimeError(provider_error)
    img = tmp_path / "x.png"
    img.write_bytes(b"fake")

    result = _manager(tmp_path, svc).handle(
        {"action": "analyze", "input": {"image_path": str(img), "question": None}, "reasoning": "r"}
    )
    assert result["status"] == "error"
    assert "RuntimeError" in result["message"]
    # The taught pointer must be the full accepted envelope, not the
    # pre-migration bare shorthand the dispatcher now rejects.
    assert "vision(action='manual', input={}" in result["message"]
    assert "reasoning=" in result["message"]
    for fragment in forbidden_fragments:
        assert fragment not in result["message"]


def test_analyze_empty_response_is_an_error(tmp_path):
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.return_value = ""
    img = tmp_path / "x.png"
    img.write_bytes(b"fake")

    result = _manager(tmp_path, svc).handle(
        {"action": "analyze", "input": {"image_path": str(img), "question": None}, "reasoning": "r"}
    )
    assert result == {
        "status": "error",
        "message": "Vision analysis returned no response.",
    }


def test_analyze_without_a_direct_route_returns_the_setup_manual_reason(tmp_path):
    reason = (
        "No direct vision provider was configured; use vision(action='manual', "
        "input={}, reasoning='no direct vision provider is configured')."
    )
    mgr = _bound_manager(_StubAgent(tmp_path), service=None, manual_reason=reason)
    result = mgr.handle(
        {"action": "analyze", "input": {"image_path": "x.png", "question": None}, "reasoning": "r"}
    )
    # The impl appends the consent/setup guidance to the manual reason so the
    # message remains actionable without performing side effects.
    assert result["status"] == "error"
    assert result["message"].startswith(reason)
    assert "load the vision manual skill" in result["message"]


# ---------------------------------------------------------------------------
# manual: family-owned, no provider construction or call, exact body/path
# ---------------------------------------------------------------------------


def test_manual_returns_the_exact_installed_body_and_path(tmp_path):
    body, path = _install_manual(tmp_path)
    mgr = _manager(tmp_path, _never_called_service())

    result = mgr.handle({"action": "manual", "input": {}, "reasoning": "load guidance"})
    assert result == {
        "status": "ok",
        "action": "manual",
        "manual": body,
        "manual_path": str(path),
    }


def test_manual_result_is_not_double_wrapped(tmp_path):
    _install_manual(tmp_path)
    result = _manager(tmp_path, _never_called_service()).handle(
        {"action": "manual", "input": {}, "reasoning": "r"}
    )
    # The canonical MCP child result is adapted, never nested inside another
    # action result.
    assert "content" not in result
    assert "structuredContent" not in result
    assert not any(isinstance(value, dict) for value in result.values())


def test_missing_installed_manual_degrades_without_side_effects(tmp_path):
    expected = tmp_path / ".library" / "intrinsic" / "capabilities" / "vision" / "SKILL.md"
    result = _manager(tmp_path, _never_called_service()).handle(
        {"action": "manual", "input": {}, "reasoning": "r"}
    )
    assert result == {
        "status": "degraded",
        "action": "manual",
        "manual": "",
        "manual_path": str(expected),
        "error": (
            "vision manual missing — initializer may have failed or "
            "capability not installed correctly"
        ),
    }
    assert not (tmp_path / ".library").exists()


def test_manual_constructs_no_provider_and_calls_no_service(tmp_path):
    _install_manual(tmp_path)
    agent = _StubAgent(tmp_path)
    with patch("lingtai.services.vision.create_vision_service") as mock_factory:
        mgr = setup(agent, provider="not-a-real-provider")
        result = agent.tools["vision"]["handler"](
            {"action": "manual", "input": {}, "reasoning": "r"}
        )
    mock_factory.assert_not_called()
    assert mgr._vision_service is None
    assert result["status"] == "ok"
    assert result["action"] == "manual"


def test_manual_works_even_when_a_configured_service_would_fail(tmp_path):
    """Manual performs no analyze operation, so a broken provider is irrelevant."""
    body, path = _install_manual(tmp_path)
    svc = _never_called_service()
    result = _manager(tmp_path, svc).handle(
        {"action": "manual", "input": {}, "reasoning": "r"}
    )
    assert result["manual"] == body
    assert result["manual_path"] == str(path)
    svc.analyze_image.assert_not_called()


def test_manual_method_matches_the_dispatched_manual_action(tmp_path):
    _install_manual(tmp_path)
    mgr = _manager(tmp_path, _never_called_service())
    assert mgr.manual() == mgr.handle(
        {"action": "manual", "input": {}, "reasoning": "r"}
    )


def test_every_taught_manual_pointer_round_trips_through_the_dispatcher(tmp_path):
    """The guidance strings must teach a call the dispatcher actually accepts.

    Before the envelope migration, `vision(action='manual')` was a complete
    valid call. It no longer is — `input` is required — so any in-result string
    still teaching the bare shorthand would send an agent into a rejection loop.
    This walks every guidance string vision can emit, extracts the taught call,
    and proves the literal shape it teaches dispatches successfully.
    """
    import re

    _install_manual(tmp_path)
    mgr = _manager(tmp_path, _never_called_service())

    # Collect the guidance strings from every source that emits one: the
    # setup-failure builder, both analyze failure paths, and every
    # `manual_reason` literal assigned in setup().
    source = Path("src/lingtai/tools/vision/__init__.py").read_text(encoding="utf-8")
    taught = re.findall(r"vision\(action='manual'([^)]*)\)", source)
    assert len(taught) >= 18, f"expected every guidance string, found {len(taught)}"

    for suffix in taught:
        assert "input={}" in suffix.replace("{{}}", "{}"), (
            f"taught pointer omits input: vision(action='manual'{suffix})"
        )
        assert "reasoning=" in suffix, (
            f"taught pointer omits reasoning: vision(action='manual'{suffix})"
        )

    # The taught shape, executed literally, must succeed through dispatch.
    assert mgr.handle(
        {"action": "manual", "input": {}, "reasoning": "load vision guidance"}
    )["status"] == "ok"
    # ...and the pre-migration bare shorthand must NOT, which is exactly why
    # the strings had to change.
    assert mgr.handle({"action": "manual"})["status"] == "error"


def test_no_bare_manual_pointer_survives_in_any_vision_owned_surface():
    """No `vision(action='manual')` without `input=` anywhere vision owns."""
    import re

    owned = [
        Path("src/lingtai/tools/vision/__init__.py"),
        Path("src/lingtai/tools/vision/CONTRACT.md"),
        Path("src/lingtai/tools/vision/manual/SKILL.md"),
        Path("src/lingtai/services/vision/openai.py"),
    ]
    bare = re.compile(r"vision\(action=[\"']manual[\"']\s*\)")
    for path in owned:
        text = path.read_text(encoding="utf-8")
        assert not bare.search(text), f"stale bare manual pointer in {path}"


def test_progressive_disclosure_references_are_owned_by_governed_docs():
    references = (
        "src/lingtai/tools/vision/manual/reference/actions.md",
        "src/lingtai/tools/vision/manual/reference/backends.md",
        "src/lingtai/tools/vision/manual/reference/routing.md",
        "src/lingtai/tools/vision/manual/reference/settings.md",
    )
    for owner in (
        Path("src/lingtai/tools/vision/ANATOMY.md"),
        Path("src/lingtai/tools/vision/CONTRACT.md"),
    ):
        text = owner.read_text(encoding="utf-8")
        for reference in references:
            assert reference in text, f"{owner} does not govern {reference}"

    router = Path("src/lingtai/tools/vision/manual/SKILL.md").read_text(
        encoding="utf-8"
    )
    for reference in references:
        relative = reference.removeprefix("src/lingtai/tools/vision/manual/")
        assert f"]({relative})" in router, f"manual router omits {relative}"

    actions = Path(references[0]).read_text(encoding="utf-8")
    assert "## Result shapes" in actions
    for result_key in ("analysis", "route", "presets", "settings", "manual_path"):
        assert f'"{result_key}"' in actions
    assert "comment section below" not in actions
    assert "this guidance" not in actions


def test_family_registers_the_reserved_manual_child_once(tmp_path):
    mgr = _manager(tmp_path)
    assert mgr._family.child_names == (
        "analyze", "check", "list", "settings", "manual"
    )
    assert mgr._family.has_manual()


def test_wire_and_dispatch_families_come_from_one_child_declaration(tmp_path):
    """The schema-only family and the manager's family cannot drift apart.

    Both are built by `_build_family`, so child names, order, and per-action
    input schemas are identical objects/values on the wire surface and at the
    dispatch boundary — the duplication that a second hand-written registry
    would allow is structurally impossible.
    """
    from lingtai.tools.vision import _FAMILY

    mgr = _manager(tmp_path, _never_called_service())
    assert mgr._family.child_names == _FAMILY.child_names
    assert mgr._family.build_schema() == _FAMILY.build_schema() == get_schema()


def test_manual_child_input_schema_is_the_generic_owners_object(tmp_path):
    """Vision registers the generic manual schema, never a local near-copy.

    A restated copy previously carried `"required": []` while the generic
    owner's omitted the key, so the wire schema and the dispatch-side schema
    for the same child were two different objects. The generic owner now
    states `"required": []` explicitly too (byte-for-byte comparability
    across every consumer), so the wire branch is checked against it.
    """
    from lingtai.tools.tool_family.manual import MANUAL_INPUT_SCHEMA

    manual_branch = next(
        b
        for b in get_schema()["properties"]["input"]["anyOf"]
        if b["title"] == "manual input"
    )
    assert manual_branch["properties"] == MANUAL_INPUT_SCHEMA["properties"]
    assert manual_branch["additionalProperties"] is False
    assert manual_branch.get("required") == MANUAL_INPUT_SCHEMA["required"] == []


# ---------------------------------------------------------------------------
# Wire parity: Chat Completions and Responses
# ---------------------------------------------------------------------------


def test_vision_schema_survives_both_provider_wires():
    from lingtai.kernel.llm.base import FunctionSchema
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    schema = FunctionSchema(
        name="vision",
        description=vision_tool.get_description(),
        parameters=get_schema(),
    )
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    for wire, combinator in ((chat, "anyOf"), (responses, "anyOf")):
        assert wire["type"] == "object"
        assert wire["required"] == ["action", "input", "reasoning"]
        assert wire["additionalProperties"] is False
        assert set(wire["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert wire["properties"]["action"]["enum"] == [
            "analyze", "check", "list", "settings", "manual"
        ]
        branches = wire["properties"]["input"][combinator]
        assert [b["title"] for b in branches] == [
            "analyze input",
            "check input",
            "list input",
            "settings inventory input",
            "manual input",
        ]
        for branch in branches:
            assert branch["additionalProperties"] is False
            assert not {"reasoning", "_reasoning", "summarize"} & set(
                branch["properties"]
            )
        # Root action<->input correlation must survive the wire, not just the
        # composed schema: each action const keeps its own `then.input`.
        correlated = {
            cond["if"]["properties"]["action"]["const"]: cond["then"]["properties"]["input"]
            for cond in wire["allOf"]
        }
        assert set(correlated) == {
            "analyze", "check", "list", "settings", "manual"
        }
        assert set(correlated["analyze"]["properties"]) == {"image_path", "question", "preset"}
        assert correlated["list"]["properties"] == {}
        assert correlated["manual"]["properties"] == {}


def test_root_summarize_reaches_the_single_centralized_summarizer(tmp_path):
    """``vision`` is a migrated LTP v2 family, so its canonical root
    ``summarize`` boolean is recognized by the one existing centralized
    a-priori summarizer — no second summarizer, and the raw result is still
    durably logged before the visible replacement."""
    from lingtai.kernel.llm.base import ToolCall
    from lingtai.kernel.loop_guard import LoopGuard
    from lingtai.kernel.tool_executor import ToolExecutor
    from lingtai.kernel.tool_result_summary import (
        APRIORI_SUMMARY_MARKER,
        summary_requested,
    )

    assert summary_requested({"summarize": True}, "vision") is True
    # An unmigrated tool's own ``summarize``-named field is still never
    # reinterpreted as this cross-cutting control.
    assert summary_requested({"summarize": True}, "some_unmigrated_tool") is False

    events: list[tuple] = []
    raw = {"status": "ok", "analysis": "VISIONRAW-marker long description"}
    executor = ToolExecutor(
        dispatch_fn=lambda tc: raw,
        make_tool_result_fn=lambda name, result, **kw: {"content": result, **kw},
        guard=LoopGuard(),
        known_tools={"vision"},
        parallel_safe_tools=set(),
        logger_fn=lambda event_type, **fields: events.append((event_type, fields)),
        working_dir=tmp_path,
        summarizer_fn=lambda sp, up, tn, cid: "SUMMARY: a chart",
    )
    results, _, _ = executor.execute(
        [
            ToolCall(
                name="vision",
                args={
                    "action": "analyze",
                    "input": {"image_path": "x.png", "question": None},
                    "reasoning": "read the chart",
                    "summarize": True,
                },
                id="v1",
            )
        ]
    )
    content = results[0]["content"]
    assert content["artifact"] == APRIORI_SUMMARY_MARKER
    assert content["generated_summary"] == "SUMMARY: a chart"
    assert "VISIONRAW-marker" not in str(content)
    assert any(
        event_type == "tool_result" and "VISIONRAW-marker" in str(fields.get("result"))
        for event_type, fields in events
    )


# ---------------------------------------------------------------------------
# preset borrowing: one call may borrow another allowed preset's vision service
# ---------------------------------------------------------------------------


def _write_preset_borrow_fixture(tmp_path: Path) -> dict:
    """Write init.json (allowed preset) plus a borrowable codex-pool preset file.

    Returns the manifest dict mirrored in init.json for assertions.
    """
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir(parents=True, exist_ok=True)
    (preset_dir / "codex-pool.json").write_text(
        """{
          "name": "codex-pool",
          "description": {"summary": "fixture preset with gpt-5.6 vision"},
          "manifest": {
            "llm": {
              "provider": "codex-pool",
              "model": "gpt-5.6",
              "base_url": "https://example.test/v1"
            },
            "capabilities": {
              "vision": {"provider": "codex-pool"}
            }
          }
        }
        """,
        encoding="utf-8",
    )
    manifest = {
        "preset": {"allowed": ["presets/codex-pool.json"]},
    }
    (tmp_path / "init.json").write_text(
        '{"manifest": ' + __import__("json").dumps(manifest) + "}",
        encoding="utf-8",
    )
    return manifest


def test_preset_borrow_rejects_a_preset_not_in_manifest_allowed(tmp_path):
    # No init.json at all -> cannot resolve allowed, borrow fails closed.
    mgr = _manager(tmp_path, _never_called_service())
    svc, reason, identity = mgr._build_service_from_preset("presets/codex-pool.json")
    assert svc is None
    assert identity == {}
    assert "manifest.preset.allowed" in reason


def test_preset_borrow_rejects_unlisted_preset_when_init_exists(tmp_path):
    _write_preset_borrow_fixture(tmp_path)
    mgr = _manager(tmp_path, _never_called_service())
    svc, reason, identity = mgr._build_service_from_preset("presets/other.json")
    assert svc is None
    assert identity == {}
    assert "not in manifest.preset.allowed" in reason


def test_preset_borrow_resolves_the_listed_presets_own_identity(tmp_path):
    """The borrowed preset's llm/capabilities feed an identity shim, so the
    codex-pool preset selects its own route instead of the active provider's."""
    _write_preset_borrow_fixture(tmp_path)

    borrowed = MagicMock(spec=VisionService)
    borrowed.analyze_image.return_value = "borrowed gpt-5.6 answer"

    with patch(
        "lingtai.tools.vision._resolve_direct_service",
        return_value=(borrowed, "", vision_tool._VisionRouteProvenance()),
    ) as mock_resolve:
        mgr = _manager(tmp_path, _never_called_service())
        svc, reason, identity = mgr._build_service_from_preset("presets/codex-pool.json")

    assert svc is borrowed
    assert reason == ""
    assert identity == {"provider": "codex-pool", "model": "gpt-5.6", "base_url": "https://example.test/v1"}
    mock_resolve.assert_called_once()
    kwargs = mock_resolve.call_args.kwargs
    identity = kwargs["identity_service"]
    # ``provider`` is consumed positionally; the capability's provider copy is
    # dropped before the call (regression: duplicate keyword -> TypeError).
    assert mock_resolve.call_args.args[2] == "codex-pool"
    assert "provider" not in kwargs
    assert identity.provider == "codex-pool"
    assert identity._model == "gpt-5.6"
    assert identity._base_url == "https://example.test/v1"


def test_analyze_with_preset_option_uses_the_borrowed_service(tmp_path):
    """An analyze call carrying the optional preset field dispatches through the
    borrowed service, not the default (never-called) service."""
    _write_preset_borrow_fixture(tmp_path)
    img = tmp_path / "photo.png"
    img.write_bytes(b"fake")
    borrowed = MagicMock(spec=VisionService)
    borrowed.analyze_image.return_value = "borrowed answer"

    with patch(
        "lingtai.tools.vision._resolve_direct_service",
        return_value=(borrowed, "", vision_tool._VisionRouteProvenance()),
    ):
        mgr = _manager(tmp_path, _never_called_service())
        result = mgr.handle(
            {
                "action": "analyze",
                "input": {
                    "image_path": str(img),
                    "question": "What color?",
                    "preset": "presets/codex-pool.json",
                },
                "reasoning": "borrow codex-pool vision",
            }
        )
    assert result == {"status": "ok", "analysis": "borrowed answer"}
    borrowed.analyze_image.assert_called_once_with(str(img), prompt="What color?")


def test_analyze_preset_borrow_failure_is_sanitized_and_teaches_manual(tmp_path):
    """A borrow failure (e.g. preset not authorized) returns the explicit error
    and points to the full accepted manual envelope."""
    _write_preset_borrow_fixture(tmp_path)
    img = tmp_path / "photo.png"
    img.write_bytes(b"fake")

    mgr = _manager(tmp_path, _never_called_service())
    result = mgr.handle(
        {
            "action": "analyze",
            "input": {
                "image_path": str(img),
                "question": None,
                "preset": "presets/other.json",
            },
            "reasoning": "borrow unlisted preset",
        }
    )
    assert result["status"] == "error"
    assert "not in manifest.preset.allowed" in result["message"]
    assert "vision(action='manual', input={}" in result["message"]
    assert "reasoning=" in result["message"]


def test_analyze_without_preset_still_uses_the_default_route(tmp_path):
    """Omitting the preset option leaves the pre-existing default routing intact."""
    _write_preset_borrow_fixture(tmp_path)
    img = tmp_path / "photo.png"
    img.write_bytes(b"fake")
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.return_value = "default answer"

    with patch("lingtai.tools.vision._resolve_direct_service") as mock_resolve:
        mgr = _manager(tmp_path, svc)
        result = mgr.handle(
            {
                "action": "analyze",
                "input": {"image_path": str(img), "question": "Q"},
                "reasoning": "default route",
            }
        )
    mock_resolve.assert_not_called()
    assert result == {"status": "ok", "analysis": "default answer"}

def test_check_default_route_reports_ok_without_image(tmp_path):
    """check with no preset reports the default route and never sends an image."""
    svc = MagicMock(spec=VisionService)
    svc.analyze_image.side_effect = AssertionError("check must not call analyze_image")
    agent = _StubAgent(tmp_path)
    agent.service = MagicMock()
    agent.service.provider = "codex-pool"
    agent.service.model = "gpt-5.6"
    mgr = _bound_manager(agent, service=svc)

    result = mgr.handle(
        {"action": "check", "input": {"preset": None}, "reasoning": "which vision works"}
    )
    assert result == {
        "status": "ok",
        "route": "default",
        "provider": "codex-pool",
        "model": "gpt-5.6",
    }
    svc.analyze_image.assert_not_called()


def test_check_no_default_route_returns_manual_reason(tmp_path):
    reason = (
        "No direct vision provider was configured; use vision(action='manual', "
        "input={}, reasoning='no direct vision provider is configured')."
    )
    mgr = _bound_manager(_StubAgent(tmp_path), service=None, manual_reason=reason)
    result = mgr.handle(
        {"action": "check", "input": {"preset": None}, "reasoning": "r"}
    )
    assert result["status"] == "error"
    assert "No direct vision provider" in result["message"]


def test_check_preset_reports_identity_without_image(tmp_path):
    """check with a preset borrows its service and reports provider/model."""
    _write_preset_borrow_fixture(tmp_path)
    borrowed = MagicMock(spec=VisionService)
    borrowed.analyze_image.side_effect = AssertionError("check must not analyze")

    with patch(
        "lingtai.tools.vision._resolve_direct_service",
        return_value=(borrowed, "", vision_tool._VisionRouteProvenance()),
    ):
        mgr = _manager(tmp_path, _never_called_service())
        result = mgr.handle(
            {
                "action": "check",
                "input": {"preset": "presets/codex-pool.json"},
                "reasoning": "check codex-pool vision",
            }
        )
    assert result["status"] == "ok"
    assert result["route"] == "preset:presets/codex-pool.json"
    assert result["provider"] == "codex-pool"
    assert result["model"] == "gpt-5.6"
    borrowed.analyze_image.assert_not_called()


def test_check_unlisted_preset_fails_sanitized(tmp_path):
    """check fails closed when the preset is not authorized."""
    _write_preset_borrow_fixture(tmp_path)
    mgr = _manager(tmp_path, _never_called_service())
    result = mgr.handle(
        {
            "action": "check",
            "input": {"preset": "presets/other.json"},
            "reasoning": "r",
        }
    )
    assert result["status"] == "error"
    assert "not in manifest.preset.allowed" in result["message"]
    assert "vision(action='manual', input={}" in result["message"]


# ---------------------------------------------------------------------------
# borrow regression: capabilities.vision declares provider (real codex-pool
# preset shape) and list action: mechanical route enumeration
# ---------------------------------------------------------------------------


def test_preset_borrow_with_vision_capability_provider_does_not_raise_type_error(
    tmp_path,
):
    """A preset whose ``capabilities.vision`` declares ``provider`` (the real
    codex-pool preset shape) must be borrowable without a TypeError.

    Regression: ``_build_service_from_preset`` copied ``vision_cap`` into
    ``kwargs`` and then called ``_resolve_direct_service(provider, ...,
    **kwargs)``, so ``provider`` was bound twice and every borrow of such a
    preset raised ``TypeError: _resolve_direct_service() got multiple values
    for argument 'provider'``. The real resolver must run here (only the
    low-level factory and pool selector are stubbed), proving the fix inside
    ``_build_service_from_preset`` is exercised end to end.
    """
    from lingtai.auth.codex_account_source import AccountCandidate

    _write_preset_borrow_fixture(tmp_path)
    borrowed = MagicMock(spec=VisionService)
    borrowed.analyze_image.return_value = "borrowed gpt-5.6 answer"
    selected = AccountCandidate(
        auth_ref="/tmp/borrow-pool.json",
        source_ref="pool.json",
        source_index=0,
        weight=1,
    )

    with patch(
        "lingtai.services.vision.create_vision_service", return_value=borrowed
    ) as mock_factory, patch(
        "lingtai.auth.codex_account_source.WeightedAccountSource.select",
        return_value=selected,
    ):
        mgr = _manager(tmp_path, _never_called_service())
        svc, reason, identity = mgr._build_service_from_preset("presets/codex-pool.json")

        assert svc is borrowed
        assert reason == ""
        assert identity == {
            "provider": "codex-pool",
            "model": "gpt-5.6",
            "base_url": "https://example.test/v1",
        }
        # The factory must not receive the capability's provider copy: it is
        # consumed positionally by ``_resolve_direct_service``.
        assert mock_factory.call_args.args == ("codex",)
        assert "provider" not in mock_factory.call_args.kwargs

        # check through the public dispatcher resolves the borrowed route.
        check_result = mgr.handle(
            {
                "action": "check",
                "input": {"preset": "presets/codex-pool.json"},
                "reasoning": "verify the borrowed route",
            }
        )
        assert check_result == {
            "status": "ok",
            "route": "preset:presets/codex-pool.json",
            "provider": "codex-pool",
            "model": "gpt-5.6",
        }
        borrowed.analyze_image.assert_not_called()

        # analyze through the public dispatcher runs one request on the
        # borrowed service.
        img = tmp_path / "photo.png"
        img.write_bytes(b"fake")
        analyze_result = mgr.handle(
            {
                "action": "analyze",
                "input": {
                    "image_path": str(img),
                    "question": None,
                    "preset": "presets/codex-pool.json",
                },
                "reasoning": "borrow codex-pool vision",
            }
        )
        assert analyze_result == {"status": "ok", "analysis": "borrowed gpt-5.6 answer"}
        borrowed.analyze_image.assert_called_once_with(
            str(img), prompt="Describe what you see in this image."
        )


def _write_list_fixture(tmp_path: Path) -> None:
    """Write init.json (one absolute allowed ref) plus two presets on disk: a
    vision-capable codex-pool preset that is allowed, and a text-only preset
    that is NOT in manifest.preset.allowed and must never be enumerated (the
    mechanical ``list`` never reaches past the authorization boundary)."""
    vision_preset = tmp_path / "presets" / "codex-pool.json"
    vision_preset.parent.mkdir(parents=True, exist_ok=True)
    vision_preset.write_text(
        """{
          "name": "codex-pool",
          "description": {"summary": "fixture preset with gpt-5.6 vision"},
          "manifest": {
            "llm": {
              "provider": "codex-pool",
              "model": "gpt-5.6"
            },
            "capabilities": {
              "vision": {"provider": "codex-pool"}
            }
          }
        }
        """,
        encoding="utf-8",
    )
    text_preset = tmp_path / "presets" / "text-only.json"
    text_preset.write_text(
        """{
          "name": "text-only",
          "description": {"summary": "fixture preset without vision"},
          "manifest": {
            "llm": {
              "provider": "gemini",
              "model": "gemini-2.5-pro"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    manifest = {"preset": {"allowed": [str(vision_preset)]}}
    (tmp_path / "init.json").write_text(
        '{"manifest": ' + __import__("json").dumps(manifest) + "}",
        encoding="utf-8",
    )


def test_list_action_enumerates_default_route_and_vision_capable_presets(tmp_path):
    """``vision(action='list')`` is mechanical: no provider call, one entry per
    vision-capable allowed preset (provider/model/endpoint/responses_vision),
    and a classified default route."""
    _write_list_fixture(tmp_path)
    agent = _StubAgent(tmp_path)
    agent.service = MagicMock()
    agent.service.provider = "codex"
    agent.service._model = "gpt-5.5"
    mgr = _bound_manager(agent, service=None)

    with patch("lingtai.services.vision.create_vision_service") as mock_factory:
        result = mgr.handle(
            {"action": "list", "input": {}, "reasoning": "enumerate vision routes"}
        )
    mock_factory.assert_not_called()

    assert result["status"] == "ok"
    assert result["default"] == {
        "provider": "codex",
        "model": "gpt-5.5",
        "configured": False,
        "supports_vision": True,
        "endpoint": "responses",
        "responses_vision": True,
    }
    # Exactly the vision-capable allowed preset is enumerated; the text-only
    # preset on disk is not in manifest.preset.allowed and never appears.
    assert len(result["presets"]) == 1
    entry = result["presets"][0]
    assert entry["preset"].endswith("presets/codex-pool.json")
    assert entry["provider"] == "codex-pool"
    assert entry["model"] == "gpt-5.6"
    assert entry["endpoint"] == "responses"
    assert entry["responses_vision"] is True
    assert result["count"] == 1


def test_list_action_dedupes_tilde_and_absolute_aliases_of_one_preset(
    tmp_path, monkeypatch
):
    """One physical allowed preset is one ``list`` row, however it is spelled.

    Production allowlists use ``~/...`` spellings. ``list`` must not report the
    raw ``~`` reference and its expanded absolute path as two presets (12
    declared presets were enumerated as 24 rows), nor double-count an operator
    who lists the same file under both spellings. Rows keep the declared
    manifest spelling, which is exactly what ``analyze``/``check`` accept.
    """
    home = tmp_path / "home"
    preset = home / "presets" / "codex-pool.json"
    preset.parent.mkdir(parents=True)
    preset.write_text(
        """{
          "name": "codex-pool",
          "description": {"summary": "fixture preset with gpt-5.6 vision"},
          "manifest": {
            "llm": {"provider": "codex-pool", "model": "gpt-5.6"},
            "capabilities": {"vision": {"provider": "codex-pool"}}
          }
        }
        """,
        encoding="utf-8",
    )
    # ``Path.expanduser`` reads HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    tilde_ref = "~/presets/codex-pool.json"
    assert Path(tilde_ref).expanduser() == preset
    manifest = {"preset": {"allowed": [tilde_ref, str(preset)]}}
    (tmp_path / "init.json").write_text(
        '{"manifest": ' + __import__("json").dumps(manifest) + "}",
        encoding="utf-8",
    )
    agent = _StubAgent(tmp_path)
    agent.service = MagicMock()
    agent.service.provider = "codex"
    agent.service._model = "gpt-5.5"
    mgr = _bound_manager(agent, service=None)

    with patch("lingtai.services.vision.create_vision_service") as mock_factory:
        result = mgr.handle(
            {"action": "list", "input": {}, "reasoning": "enumerate vision routes"}
        )
    mock_factory.assert_not_called()

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert len(result["presets"]) == 1
    entry = result["presets"][0]
    # The surviving row is the first declared spelling of that physical file.
    assert entry["preset"] == tilde_ref
    assert entry["provider"] == "codex-pool"
    assert entry["model"] == "gpt-5.6"
    assert entry["endpoint"] == "responses"


@pytest.mark.parametrize(
    ("provider", "expected_endpoint", "expected_responses"),
    [
        ("codex", "responses", True),
        ("codex-pool", "responses", True),
        ("codex_pool", "responses", True),
        ("claude-code", "claude-cli", False),
        ("claude-p", "claude-cli", False),
        ("local", "openai-compatible-local", False),
        ("mlx", "mlx-on-device", False),
        ("openai", "provider-service", False),
        ("gemini", "provider-service", False),
        ("GEMINI", "provider-service", False),
        (None, "unknown", False),
        ("", "unknown", False),
    ],
)
def test_vision_endpoint_classification(
    provider, expected_endpoint, expected_responses
):
    """The ``list`` endpoint/responses classification is pure string mapping."""
    from lingtai.tools.vision import _responses_vision, _vision_endpoint

    assert _vision_endpoint(provider) == expected_endpoint
    assert _responses_vision(provider) is expected_responses


# ---------------------------------------------------------------------------
# Owner evidence: explicit preset credentials, no automatic fallback, and ports
# ---------------------------------------------------------------------------


def _write_credential_preset_fixture(tmp_path: Path) -> None:
    """Write one allowed preset whose own credential is resolved for a borrow.

    The fixture carries only an environment-variable *name*. The test supplies
    a disposable sentinel through pytest's environment monkeypatch and never
    writes or prints a real credential.
    """
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir(parents=True, exist_ok=True)
    (preset_dir / "borrowed-openai.json").write_text(
        """{
          "name": "borrowed-openai",
          "description": {"summary": "fixture with an explicitly allowed key route"},
          "manifest": {
            "llm": {
              "provider": "openai",
              "model": "borrowed-vision-model",
              "base_url": "https://borrowed.example/v1"
            },
            "capabilities": {
              "vision": {
                "provider": "openai",
                "api_key_env": "VISION_ALLOWED_PRESET_KEY"
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )
    (tmp_path / "init.json").write_text(
        '{"manifest": {"preset": {"allowed": ["presets/borrowed-openai.json"]}}}',
        encoding="utf-8",
    )


def test_allowed_preset_resolves_its_own_credential_not_active_preset(
    tmp_path, monkeypatch
):
    """An explicit allowed borrow uses that preset's env credential identity.

    This is intentionally a family-local resolver test: the serialized host
    fixture remains prohibited in the parallel lane, while the Vision-side
    contract is proved end to end through the real preset loader/resolver.
    """
    _write_credential_preset_fixture(tmp_path)
    monkeypatch.setenv("VISION_ALLOWED_PRESET_KEY", "allowed-key-sentinel")

    active = MagicMock()
    active.provider = "openai"
    active._model = "active-text-model"
    active._base_url = "https://active.example/v1"
    active.api_key = "active-key-sentinel"
    agent = _StubAgent(tmp_path)
    agent.service = active
    mgr = _bound_manager(agent, service=_never_called_service())

    borrowed = MagicMock(spec=VisionService)
    with patch(
        "lingtai.services.vision.openai.OpenAIVisionService",
        return_value=borrowed,
    ) as factory:
        service, reason, identity = mgr._build_service_from_preset(
            "presets/borrowed-openai.json"
        )

    assert service is borrowed
    assert reason == ""
    assert identity == {
        "provider": "openai",
        "model": "borrowed-vision-model",
        "base_url": "https://borrowed.example/v1",
    }
    assert factory.call_args.kwargs == {
        "api_key": "allowed-key-sentinel",
        "model": "borrowed-vision-model",
        "base_url": "https://borrowed.example/v1",
        "wire_api": "chat_completions",
    }
    # The active preset's credential is not used for this explicitly selected
    # borrow; the test never exposes it to the provider factory.
    assert factory.call_args.kwargs["api_key"] != active.api_key


def test_default_request_failure_does_not_auto_borrow_or_invoke_fallback(tmp_path):
    """A failed default request returns guidance without a hidden fallback."""
    service = MagicMock(spec=VisionService)
    service.analyze_image.side_effect = RuntimeError("provider unavailable")
    image = tmp_path / "photo.png"
    image.write_bytes(b"fake")
    mgr = _manager(tmp_path, service)

    with patch.object(mgr, "_build_service_from_preset") as borrow, patch(
        "lingtai.services.vision.create_vision_service"
    ) as factory:
        result = mgr.handle(
            {
                "action": "analyze",
                "input": {"image_path": str(image), "question": None},
                "reasoning": "prove no automatic fallback",
            }
        )

    borrow.assert_not_called()
    factory.assert_not_called()
    service.analyze_image.assert_called_once()
    assert result["status"] == "error"
    assert "Alternative vision may be available" in result["message"]
    assert "MCP" in result["message"]
    # The MCP/provider alternative is guidance only; this manager has no
    # capability-install or MCP invocation side effect.


class _ExplodingActiveProvider:
    @property
    def service(self):
        raise AssertionError("manual must not read active_provider.service")


def test_manual_does_not_read_provider_or_configuration_route(tmp_path):
    """Manual can load its installed body with an unreadable provider port."""
    body, path = _install_manual(tmp_path)
    mgr = VisionManager(
        _Workdir(tmp_path),
        _ExplodingActiveProvider(),
        vision_service=None,
        manual_reason="route should not be inspected",
    )

    result = mgr.handle(
        {"action": "manual", "input": {}, "reasoning": "load only the manual"}
    )
    assert result == {
        "status": "ok",
        "action": "manual",
        "manual": body,
        "manual_path": str(path),
    }


def test_vision_owner_docs_cover_all_actions_and_truthful_preset_boundary():
    """Owner docs/LABTs cannot regress to the old analyze/manual contract."""
    root = Path("src/lingtai/tools/vision")
    anatomy = (root / "ANATOMY.md").read_text(encoding="utf-8")
    contract = (root / "CONTRACT.md").read_text(encoding="utf-8")
    behaviors = (root / "BEHAVIORS.md").read_text(encoding="utf-8")
    manual = (root / "manual" / "SKILL.md").read_text(encoding="utf-8")

    for action in ("analyze", "check", "list", "settings", "manual"):
        assert action in anatomy
        assert action in contract
        assert action in manual
    assert "two canonical child input schemas" not in anatomy
    assert "both children's" not in contract
    assert "preset" in contract
    assert "allowed preset's own" in manual
    assert "reads another preset's secret" not in manual
    assert "auto-invokes MCP" in manual
    for labt in (
        "VN001", "VN002", "VN003", "VN004", "VN005", "VN006", "VN007"
    ):
        assert labt in behaviors
        assert f"{labt}](BEHAVIORS.md#behavior-{labt.lower()}" in contract or labt in contract


def test_vision_declaration_requires_only_family_narrow_ports():
    """The local declaration records the shared integration port requirement."""
    from lingtai.tools.vision import DECLARATION

    assert DECLARATION.requires == ("workdir", "active_provider", "configuration")
