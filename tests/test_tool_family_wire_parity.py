"""Chat Completions / Responses wire parity for the generic ToolFamily schema.

Proves the composed overlap-tolerant ``anyOf``-over-``input`` schema survives both
provider wire shapes at the smallest existing adapter seam
(``lingtai.llm.openai.adapter._build_tools`` /
``_build_responses_tools``) without any adapter code change — a fake
``widget`` family, unrelated to ``web``, is the fixture.
"""
from __future__ import annotations

import json

from lingtai.kernel.llm.base import FunctionSchema
from lingtai.tools.tool_family import (
    TRIGGER_UNSUPPORTED_INPUT_FIELD,
    ChildTool,
    DiagnosticDescriptor,
    ToolFamily,
)


def _widget_family() -> ToolFamily:
    def spin_handler(input_):
        return {"status": "ok", "action": "spin", "speed": input_.get("speed")}

    def manual_handler(_input):
        return {"status": "ok", "manual": "widget manual", "manual_path": "/fake/manual_path"}

    return ToolFamily(
        "widget",
        [
            ChildTool(
                "spin",
                {
                    "type": "object",
                    "properties": {"speed": {"type": "integer"}},
                    "required": ["speed"],
                    "additionalProperties": False,
                },
                spin_handler,
                title="spin input",
            ),
            ChildTool(
                "manual",
                {"type": "object", "properties": {}, "additionalProperties": False},
                manual_handler,
                title="manual input",
            ),
        ],
    )


def test_generic_family_schema_survives_chat_and_responses_wires():
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    fam = _widget_family()
    schema = FunctionSchema(name="widget", description="widget", parameters=fam.build_schema())
    chat = _build_tools([schema])[0]["function"]["parameters"]
    responses = _build_responses_tools([schema])[0]["parameters"]

    for wire, combinator in ((chat, "anyOf"), (responses, "anyOf")):
        assert wire["type"] == "object"
        # ``reasoning`` is REQUIRED Host InvocationContext/audit metadata,
        # declared by the family schema itself (see ToolFamily.build_schema).
        assert wire["required"] == ["action", "input", "reasoning"]
        assert wire["additionalProperties"] is False
        assert set(wire["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert wire["properties"]["reasoning"]["type"] == "string"
        input_schema = wire["properties"]["input"]
        assert input_schema["type"] == "object"
        branches = input_schema[combinator]
        assert [b["title"] for b in branches] == ["spin input", "manual input"]
        for branch in branches:
            assert branch["additionalProperties"] is False
            assert "reasoning" not in branch["properties"]
            assert "_reasoning" not in branch["properties"]
            assert "summarize" not in branch["properties"]

    assert fam.handle(
        {"action": "spin", "input": {"speed": 4}, "reasoning": "r"}
    ) == {"status": "ok", "action": "spin", "speed": 4}


def test_real_agent_startup_builds_web_family_schema_on_both_wires(tmp_path):
    """Integration proof at the real Agent composition boundary (not just the
    unit-level FunctionSchema construction above): a fresh Agent with the web
    capability produces one ``web`` tool whose schema composes correctly and
    reaches both provider builders unchanged from ``_build_tool_schemas``."""
    from lingtai.agent import Agent
    from lingtai.kernel.base_agent.tools import _build_tool_schemas
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools
    from tests._service_helpers import make_gemini_mock_service as make_mock_service

    agent = Agent(
        service=make_mock_service(),
        agent_name="wire-parity-test",
        working_dir=tmp_path,
        capabilities={"web": {"provider": "duckduckgo"}},
    )
    try:
        schemas = _build_tool_schemas(agent)
        web = next(s for s in schemas if s.name == "web")
        chat = _build_tools([web])[0]["function"]["parameters"]
        responses = _build_responses_tools([web])[0]["parameters"]
        assert set(chat["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert set(responses["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert chat["required"] == ["action", "input", "reasoning"]
        assert responses["required"] == ["action", "input", "reasoning"]
        assert chat["properties"]["input"]["type"] == "object"
        assert responses["properties"]["input"]["type"] == "object"
        assert len(chat["properties"]["input"]["anyOf"]) == 4
        assert len(responses["properties"]["input"]["anyOf"]) == 4
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Provider-wire blindness regression (SONNET_DIAGNOSTIC_CONTRACT.md "Required
# design" #1/#3): the diagnostic sidecar (assumed ``ChildTool.diagnostics``,
# see ``test_tool_family_generic.py``) is passive and dispatch-time-only. It
# must never be reachable through ``build_schema()`` or either provider wire
# builder — those only ever read a child's ``input_schema``/``title``.
# ---------------------------------------------------------------------------


def test_diagnostics_sidecar_never_reaches_either_provider_wire():
    """A fake family's child opts into a diagnostic sidecar (keyed by
    structural trigger, per ``ChildTool.diagnostics: Mapping[str,
    DiagnosticDescriptor]``) with distinctive descriptor text; neither the
    composed schema nor either provider wire representation may contain that
    text, or even the word "diagnostics"."""
    secret_marker = "WIDGET_SPIN_UNSUPPORTED_INPUT_FIELD_should_never_reach_a_wire"
    descriptor = DiagnosticDescriptor(
        code=secret_marker,
        expected_form="an input object containing only speed",
        reason="spin rejects foreign action input before it can spin",
        fix="remove the foreign field or choose the action that owns it",
    )

    def spin_handler(input_):
        return {"status": "ok"}

    child = ChildTool(
        "spin",
        {
            "type": "object",
            "properties": {"speed": {"type": "integer"}},
            "required": ["speed"],
            "additionalProperties": False,
        },
        spin_handler,
        title="spin input",
        diagnostics={TRIGGER_UNSUPPORTED_INPUT_FIELD: descriptor},
    )
    fam = ToolFamily("widget", [child])
    schema = fam.build_schema()

    dumped_schema = json.dumps(schema)
    assert "diagnostics" not in dumped_schema
    assert secret_marker not in dumped_schema
    assert descriptor.reason not in dumped_schema
    assert descriptor.fix not in dumped_schema
    assert descriptor.expected_form not in dumped_schema

    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    function_schema = FunctionSchema(name="widget", description="widget", parameters=schema)
    chat = _build_tools([function_schema])[0]
    responses = _build_responses_tools([function_schema])[0]
    for wire in (chat, responses):
        dumped_wire = json.dumps(wire)
        assert "diagnostics" not in dumped_wire
        assert secret_marker not in dumped_wire
        assert descriptor.reason not in dumped_wire
        assert descriptor.fix not in dumped_wire


def test_context_molt_diagnostic_descriptor_never_reaches_either_provider_wire(tmp_path):
    """Real-family regression: once ``context.molt`` opts into its own local
    diagnostic descriptor for a foreign ``files`` input field
    (`tools/context/__init__.py`, per the shared contract's "Required
    design" #6), that descriptor's own code/text must still never appear on
    either provider wire for the real, live-agent-composed ``context``
    schema — the sidecar is dispatch-time-only and never schema-composed."""
    from lingtai.agent import Agent
    from lingtai.kernel.base_agent.tools import _build_tool_schemas
    from tests._service_helpers import make_gemini_mock_service as make_mock_service

    agent = Agent(
        service=make_mock_service(), agent_name="wire-blindness-test",
        working_dir=tmp_path,
    )
    try:
        from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

        schemas = [s for s in _build_tool_schemas(agent) if s.name == "context"]
        chat = _build_tools(schemas)[0]
        responses = _build_responses_tools(schemas)[0]
        for wire in (chat, responses):
            dumped = json.dumps(wire)
            assert "diagnostics" not in dumped
            assert "CTX_MOLT_UNSUPPORTED_INPUT_FIELD" not in dumped
            assert "molt rejects foreign action input" not in dumped
    finally:
        agent.stop(timeout=1.0)
