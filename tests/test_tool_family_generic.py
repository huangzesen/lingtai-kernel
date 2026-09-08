"""Generic ToolFamily/ChildTool infrastructure — proven with a fake family.

This fake ``widget`` family (actions ``spin`` and ``manual``) has no relation
to ``web``. It exists solely to prove the composition/dispatch/registration
boilerplate in ``lingtai.tools.tool_family`` is generic, not Web-specific.
"""
from __future__ import annotations

import json

import pytest

from lingtai.tools.tool_family import (
    RESERVED_MANUAL_NAME,
    TRIGGER_UNSUPPORTED_INPUT_FIELD,
    ChildTool,
    DiagnosticDescriptor,
    ToolFamily,
    ToolFamilyError,
)


def _spin_child(calls: list[dict]) -> ChildTool:
    def handler(input_):
        calls.append(dict(input_))
        return {"status": "ok", "action": "spin", "speed": input_.get("speed")}

    return ChildTool(
        name="spin",
        input_schema={
            "type": "object",
            "properties": {"speed": {"type": "integer"}},
            "required": ["speed"],
            "additionalProperties": False,
        },
        handler=handler,
        title="spin input",
    )


def _manual_child() -> ChildTool:
    def handler(_input):
        return {"status": "ok", "manual": "widget manual body", "manual_path": "/fake/manual_path"}

    return ChildTool(
        name=RESERVED_MANUAL_NAME,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
        title="manual input",
    )


def _widget_family(calls: list[dict] | None = None) -> ToolFamily:
    calls = calls if calls is not None else []
    return ToolFamily("widget", [_spin_child(calls), _manual_child()])


def test_registration_is_deterministic_and_ordered():
    fam = _widget_family()
    assert fam.child_names == ("spin", "manual")
    assert fam.has_manual()


def test_duplicate_child_name_fails_loudly():
    calls: list[dict] = []
    with pytest.raises(ToolFamilyError, match="duplicate child name 'spin'"):
        ToolFamily("widget", [_spin_child(calls), _spin_child(calls)])


def test_manual_reserved_name_collision_fails_loudly():
    """A repeated reserved ``manual`` child is caught by the duplicate-name check."""

    def handler(_input):
        return {"status": "ok"}

    second_manual = ChildTool(
        name=RESERVED_MANUAL_NAME,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )
    with pytest.raises(ToolFamilyError, match=f"duplicate child name '{RESERVED_MANUAL_NAME}'"):
        ToolFamily("widget", [_manual_child(), second_manual])


def test_empty_registry_fails_loudly():
    with pytest.raises(ToolFamilyError):
        ToolFamily("widget", [])


def test_schema_composes_action_input_reasoning_summarize_root():
    fam = _widget_family()
    schema = fam.build_schema()
    assert schema["type"] == "object"
    # ``reasoning`` is Host InvocationContext/audit metadata and is REQUIRED
    # — the family schema declares it itself so it is required even before
    # Agent schema composition re-injects the identical property text (that
    # injection only touches ``properties``, never ``required``).
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert schema["properties"]["action"]["enum"] == ["spin", "manual"]
    assert schema["properties"]["input"]["type"] == "object"
    assert schema["properties"]["reasoning"]["type"] == "string"
    assert schema["properties"]["summarize"]["type"] == "boolean"
    branches = schema["properties"]["input"]["anyOf"]
    assert [b["title"] for b in branches] == ["spin input", "manual input"]
    spin_branch, manual_branch = branches
    assert spin_branch["required"] == ["speed"]
    assert spin_branch["additionalProperties"] is False
    assert manual_branch["properties"] == {}
    for branch in branches:
        assert branch["additionalProperties"] is False
        assert "reasoning" not in branch.get("properties", {})
        assert "_reasoning" not in branch.get("properties", {})
        assert "summarize" not in branch.get("properties", {})


def test_schema_never_uses_generic_unconstrained_input_object():
    fam = _widget_family()
    schema = fam.build_schema()
    for branch in schema["properties"]["input"]["anyOf"]:
        assert branch.get("type") == "object"
        assert "properties" in branch


def test_dispatch_selects_child_by_action_and_passes_only_input():
    calls: list[dict] = []
    fam = _widget_family(calls)
    result = fam.handle({"action": "spin", "input": {"speed": 5}, "reasoning": "why"})
    assert result == {"status": "ok", "action": "spin", "speed": 5}
    assert calls == [{"speed": 5}]


def test_dispatch_unknown_action_fails_with_action_required():
    fam = _widget_family()
    result = fam.handle({"action": "nope", "input": {}})
    assert result["status"] == "failed"
    assert result["error_code"] == "ACTION_REQUIRED"


def test_dispatch_missing_action_fails_with_action_required():
    fam = _widget_family()
    result = fam.handle({"input": {}})
    assert result["status"] == "failed"
    assert result["error_code"] == "ACTION_REQUIRED"


def test_dispatch_unhashable_action_fails_without_raising():
    """Invalid JSON can make ``action`` unhashable (issue #513 blocker class).

    Such an action matches no child and must render the stable typed envelope
    failure, exactly as ``kernel/tool_dispatch.py`` does — not raise
    ``TypeError`` out of the dispatcher.
    """
    calls: list[dict] = []
    fam = _widget_family(calls)
    for unhashable in ([], {}, ["spin"], {"spin": 1}, set()):
        result = fam.handle({"action": unhashable, "input": {}, "reasoning": "why"})
        assert result["status"] == "failed", unhashable
        assert result["error_code"] == "ACTION_REQUIRED", unhashable
    assert calls == []


def test_dispatch_non_object_input_fails_with_invalid_argument():
    fam = _widget_family()
    result = fam.handle({"action": "spin", "input": "not-an-object"})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_dispatch_unknown_root_field_fails_with_invalid_argument():
    fam = _widget_family()
    result = fam.handle({"action": "spin", "input": {"speed": 1}, "bogus": True})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_dispatch_relocates_root_field_that_matches_selected_actions_own_schema_when_absent_from_input():
    """A root-level key that IS a declared property of the selected action,
    and is entirely absent from ``input``, is relocated into ``input``
    rather than rejected — the fix for a calling model leaking its own
    native flat tool shape (e.g. ``Edit(..., replace_all)``) into an
    enveloped family's root instead of nesting it under ``input``."""
    calls: list[dict] = []
    fam = _widget_family(calls)
    result = fam.handle({"action": "spin", "input": {}, "speed": 7})
    assert result["status"] == "ok"
    assert result["speed"] == 7
    assert calls == [{"speed": 7}]


def test_dispatch_rejects_duplicate_root_field_matching_key_already_in_input():
    """The relocation exception does NOT cover a root-level key that
    duplicates a name already present in ``input`` — even with an identical
    value. Two conflicting-or-redundant places for the same value stays a
    rejected hygiene violation, unchanged from before."""
    fam = _widget_family()
    result = fam.handle({"action": "spin", "input": {"speed": 1}, "speed": 1})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_dispatch_still_rejects_root_field_belonging_to_a_different_actions_schema():
    """A root-level key that matches some OTHER child's input property (not
    the selected action's) is not relocated — the exception is scoped to the
    selected action's own schema only, so cross-branch keys are still
    rejected exactly as before."""
    fam = _widget_family()
    # "manual_path" belongs to no widget action's input schema at all, and
    # even a name matching another action's own declared property (there is
    # none to collide with here, since `manual`'s input schema is empty)
    # would not be relocated for `spin`'s dispatch — this proves the plain
    # not-in-any-schema case remains rejected under the new code path too.
    result = fam.handle({"action": "spin", "input": {"speed": 1}, "manual_path": "/x"})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_dispatch_rejects_reasoning_or_summarize_leaking_into_input():
    fam = _widget_family()
    result = fam.handle({"action": "spin", "input": {"speed": 1, "summarize": True}})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"

    result2 = fam.handle({"action": "spin", "input": {"speed": 1, "reasoning": "x"}})
    assert result2["status"] == "failed"
    assert result2["error_code"] == "INVALID_ARGUMENT"


def test_dispatch_non_boolean_summarize_fails_loudly():
    fam = _widget_family()
    result = fam.handle({"action": "spin", "input": {"speed": 1}, "summarize": "yes"})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def test_dispatch_strips_summarize_and_reasoning_before_child_sees_input():
    calls: list[dict] = []
    fam = _widget_family(calls)
    fam.handle({"action": "spin", "input": {"speed": 3}, "reasoning": "r", "summarize": True})
    assert calls == [{"speed": 3}]


def test_child_handlers_receive_only_input_never_reasoning_or_summarize():
    """Direct proof for every registered child, not just one action: the
    handler's sole argument is the selected child's own ``input`` mapping.
    ``reasoning`` (required Host audit metadata) and ``summarize`` (optional
    Host presentation control) never reach any child, regardless of whether
    they are present, absent, or combined on one call."""
    seen_args: list[dict] = []

    def spin_handler(input_):
        seen_args.append(dict(input_))
        return {"status": "ok"}

    def manual_handler(input_):
        seen_args.append(dict(input_))
        return {"status": "ok"}

    fam = ToolFamily(
        "widget",
        [
            ChildTool(
                "spin",
                {"type": "object", "properties": {"speed": {"type": "integer"}}, "required": ["speed"], "additionalProperties": False},
                spin_handler,
            ),
            ChildTool(
                RESERVED_MANUAL_NAME,
                {"type": "object", "properties": {}, "additionalProperties": False},
                manual_handler,
            ),
        ],
    )
    fam.handle({"action": "spin", "input": {"speed": 1}, "reasoning": "r1"})
    fam.handle({"action": "spin", "input": {"speed": 2}, "reasoning": "r2", "summarize": True})
    fam.handle({"action": "manual", "input": {}, "reasoning": "r3", "summarize": False})
    assert seen_args == [{"speed": 1}, {"speed": 2}, {}]
    for args in seen_args:
        assert "reasoning" not in args
        assert "_reasoning" not in args
        assert "summarize" not in args


def test_canonical_child_input_schema_never_declares_reasoning_or_summarize():
    """Static proof at the schema level (distinct from the dispatch-time
    behavioral proof above): no child's own canonical ``input_schema`` — the
    schema a family author writes — declares ``reasoning``, ``_reasoning``,
    or ``summarize`` as one of its own properties. Those are envelope-only
    fields owned exclusively by the family root."""
    fam = _widget_family()
    for name in fam.child_names:
        child = fam._children[name]
        props = child.input_schema.get("properties", {})
        assert "reasoning" not in props
        assert "_reasoning" not in props
        assert "summarize" not in props


def test_dispatch_child_result_is_not_double_wrapped():
    calls: list[dict] = []
    fam = _widget_family(calls)
    result = fam.handle({"action": "spin", "input": {"speed": 9}, "reasoning": "r"})
    # The child's own raw return value is the family's return value verbatim.
    assert result == {"status": "ok", "action": "spin", "speed": 9}
    assert "result" not in result and "data" not in result


def test_manual_child_dispatch_returns_full_manual_shape():
    fam = _widget_family()
    result = fam.handle({"action": "manual", "input": {}, "reasoning": "load manual"})
    assert result == {
        "status": "ok",
        "manual": "widget manual body",
        "manual_path": "/fake/manual_path",
    }


def _minimal_evaluate_if_then(condition: dict, action: str, input_value: dict) -> bool | None:
    """Tiny, dependency-free structural evaluator for exactly the ``if``/
    ``then`` shape :meth:`ToolFamily.build_schema` generates — not a general
    JSON Schema validator. No JSON Schema library is installed in this repo,
    and the task authorizes a minimal local evaluator instead of adding one.

    Returns ``True`` if ``action``/``input_value`` satisfy this condition's
    ``then`` branch, ``False`` if ``if`` matches but ``then`` is violated,
    and ``None`` if ``if`` does not match at all (condition inapplicable —
    ``allOf`` treats a non-matching ``if`` as vacuously satisfied, mirroring
    real JSON Schema ``if``/``then`` semantics without ``else``).
    """
    if_clause = condition["if"]
    if action != if_clause["properties"]["action"]["const"]:
        return None
    then_clause = condition["then"]
    input_schema = then_clause["properties"]["input"]
    allowed = set(input_schema.get("properties", {}))
    required = set(input_schema.get("required", []))
    if not required.issubset(input_value):
        return False
    if input_schema.get("additionalProperties") is False and set(input_value) - allowed:
        return False
    return True


def test_schema_correlates_action_const_with_exact_child_input_via_root_all_of():
    """Real schema-level correlation, generated purely from the child
    registry: one ``allOf`` condition per child, each ``if`` testing
    ``action`` via ``const`` against exactly that child's own registry name,
    each ``then`` constraining ``input`` to that exact child's canonical
    ``input_schema`` — not a mapping table, not a second name list."""
    fam = _widget_family()
    schema = fam.build_schema()
    conditions = schema["allOf"]
    assert [c["if"]["properties"]["action"]["const"] for c in conditions] == list(fam.child_names)
    for child_name, condition in zip(fam.child_names, conditions):
        assert condition["if"]["required"] == ["action"]
        then_input = condition["then"]["properties"]["input"]
        child = fam._children[child_name]
        assert then_input == dict(child.input_schema)


def test_schema_all_of_rejects_mismatched_action_input_pairing_structurally():
    """Direct proof the schema *itself* now correlates ``action``/``input``:
    using a minimal local ``if``/``then`` evaluator (no JSON Schema
    dependency added), a spin-shaped ``input`` fails the ``manual`` action's
    ``allOf`` condition, and an empty ``input`` fails the ``spin`` action's
    condition — the mismatch is caught by schema structure, not only by
    ``handle()`` at dispatch. ``handle()`` remains the always-authoritative,
    fail-closed second layer regardless of what the schema alone proves."""
    fam = _widget_family()
    schema = fam.build_schema()
    conditions = {c["if"]["properties"]["action"]["const"]: c for c in schema["allOf"]}

    manual_condition = conditions["manual"]
    assert _minimal_evaluate_if_then(manual_condition, "manual", {"speed": 1}) is False
    assert _minimal_evaluate_if_then(manual_condition, "manual", {}) is True

    spin_condition = conditions["spin"]
    assert _minimal_evaluate_if_then(spin_condition, "spin", {}) is False
    assert _minimal_evaluate_if_then(spin_condition, "spin", {"speed": 1}) is True

    # A condition for a different action does not apply at all (vacuous).
    assert _minimal_evaluate_if_then(manual_condition, "spin", {"speed": 1}) is None


def test_handle_remains_authoritative_fail_closed_even_though_schema_now_correlates():
    """Dispatch is a second, always-authoritative enforcement layer: a
    mismatched ``action``/``input`` pairing is still rejected by
    ``handle()`` even though the schema now also correlates them — this is
    not a guess/fallback, it is the same exact-key check as before, kept
    unconditionally regardless of what any provider's schema-side validation
    does or does not enforce."""
    fam = _widget_family()
    result = fam.handle({"action": "manual", "input": {"speed": 1}, "reasoning": "r"})
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"


def _minimal_json_schema_valid(instance, schema):
    """Faithful subset evaluator for generated ToolFamily schemas.

    The test uses ``jsonschema`` when that already-available validator can be
    imported; this fallback covers only the JSON-Schema keywords emitted by
    ``ToolFamily.build_schema`` so the regression stays dependency-free in
    environments without that optional test dependency.  In particular,
    ``oneOf`` counts successful branches and ignores annotation keywords such as
    ``title`` and ``description``, as the standard requires.
    """
    if "const" in schema and instance != schema["const"]:
        return False
    if "enum" in schema and instance not in schema["enum"]:
        return False
    schema_type = schema.get("type")
    if schema_type == "object" and not isinstance(instance, dict):
        return False
    if schema_type == "string" and not isinstance(instance, str):
        return False
    if schema_type == "boolean" and not isinstance(instance, bool):
        return False
    if isinstance(instance, dict):
        required = set(schema.get("required", ()))
        if not required.issubset(instance):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(instance) - set(properties):
            return False
        if any(
            key in instance and not _minimal_json_schema_valid(instance[key], subschema)
            for key, subschema in properties.items()
        ):
            return False
    if "allOf" in schema and not all(
        _minimal_json_schema_valid(instance, branch) for branch in schema["allOf"]
    ):
        return False
    if "anyOf" in schema and not any(
        _minimal_json_schema_valid(instance, branch) for branch in schema["anyOf"]
    ):
        return False
    if "oneOf" in schema and sum(
        _minimal_json_schema_valid(instance, branch) for branch in schema["oneOf"]
    ) != 1:
        return False
    if "if" in schema:
        if_matches = _minimal_json_schema_valid(instance, schema["if"])
        if if_matches and "then" in schema and not _minimal_json_schema_valid(instance, schema["then"]):
            return False
    return True


def _json_schema_valid(instance, schema):
    """Use an installed standards validator, with the faithful local fallback."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return _minimal_json_schema_valid(instance, schema)
    return not list(Draft202012Validator(schema).iter_errors(instance))


def _overlapping_poll_cancel_family() -> ToolFamily:
    """No-settings fake whose poll/cancel branches overlap semantically."""
    def _schema(description: str) -> dict:
        return {
            "type": "object",
            "description": description,
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": f"{description} job identifier",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        }

    def _handler(input_):
        return {"status": "ok", "job_id": input_["job_id"]}

    return ToolFamily(
        "jobs",
        [
            ChildTool("poll", _schema("poll operation"), _handler, title="Poll a job"),
            ChildTool("cancel", _schema("cancel operation"), _handler, title="Cancel a job"),
            _manual_child(),
        ],
    )


def test_no_settings_overlapping_input_branches_validate_with_root_correlation():
    """A valid action/input call remains valid when input branches overlap.

    ``poll`` and ``cancel`` have identical validation constraints but distinct
    annotations.  A valid poll call validates against both input branches;
    JSON Schema ``oneOf`` rejects it, even though the matching root
    ``allOf``/``if``/``then`` condition validates the selected poll input.
    The production fix must therefore use a union that permits overlap.
    """
    family = _overlapping_poll_cancel_family()
    schema = family.build_schema()
    payload = {"action": "poll", "input": {"job_id": "job-1"}, "reasoning": "inspect"}
    input_schema = schema["properties"]["input"]
    assert "oneOf" not in input_schema
    branches = input_schema["anyOf"]

    assert branches[0]["title"] != branches[1]["title"]
    assert branches[0]["description"] != branches[1]["description"]
    assert branches[0]["properties"]["job_id"]["description"] != branches[1]["properties"]["job_id"]["description"]
    assert sum(_json_schema_valid(payload["input"], branch) for branch in branches) == 2

    conditions = {
        condition["if"]["properties"]["action"]["const"]: condition
        for condition in schema["allOf"]
    }
    poll_input_schema = conditions["poll"]["then"]["properties"]["input"]
    assert _json_schema_valid(payload["input"], poll_input_schema)
    assert _json_schema_valid(payload, schema)


def test_no_settings_non_overlapping_strict_branches_still_reject_mismatches():
    """The overlap fix does not weaken strict branch/action correlation."""
    schema = _widget_family().build_schema()
    valid_spin = {"action": "spin", "input": {"speed": 1}, "reasoning": "run"}
    missing_spin_input = {"action": "spin", "input": {}, "reasoning": "run"}
    spin_input_for_manual = {
        "action": "manual", "input": {"speed": 1}, "reasoning": "read"
    }
    assert _json_schema_valid(valid_spin, schema)
    assert not _json_schema_valid(missing_spin_input, schema)
    assert not _json_schema_valid(spin_input_for_manual, schema)


def test_settings_enabled_family_keeps_anyof_and_validates_settings_action():
    """The explicit settings opt-in remains an anyOf family and dispatches."""
    family = ToolFamily(
        "widget",
        [_spin_child([]), _manual_child()],
        settings_provider=lambda: (),
    )
    schema = family.build_schema()
    input_schema = schema["properties"]["input"]
    assert "oneOf" not in input_schema
    assert len(input_schema["anyOf"]) == 3
    payload = {"action": "settings", "input": {}, "reasoning": "inspect"}
    assert _json_schema_valid(payload, schema)
    assert family.handle(payload) == {"settings": []}


def test_build_schema_does_not_leak_shared_mutable_child_schema_by_reference():
    """Regression test: ``build_schema()`` must not embed a child's own
    ``input_schema`` (or its nested containers) by reference into the
    returned schema such that mutating one call's result corrupts a later,
    independent call. A shallow ``dict.update`` only copies the top level;
    nested dicts (e.g. ``properties``) stay shared with whatever object the
    caller's ``input_schema`` points at."""
    calls: list[dict] = []
    fam = _widget_family(calls)
    first = fam.build_schema()
    first_spin_branch = next(
        b for b in first["properties"]["input"]["anyOf"] if b["title"] == "spin input"
    )
    # Mutate the nested ``properties`` container reachable from the first
    # returned schema.
    first_spin_branch["properties"]["speed"]["type"] = "string"

    second = fam.build_schema()
    second_spin_branch = next(
        b for b in second["properties"]["input"]["anyOf"] if b["title"] == "spin input"
    )
    assert second_spin_branch["properties"]["speed"]["type"] == "integer"


def test_build_schema_all_of_conditions_are_mutation_isolated():
    """Companion to the ``anyOf``-branch mutation-isolation regression test,
    for the new ``allOf`` conditions: mutating one call's
    ``then.properties.input`` must not corrupt a later, independent call, the
    child's own canonical ``input_schema``, or the sibling ``anyOf`` branch —
    each surface is built from its own deep copy."""
    calls: list[dict] = []
    fam = _widget_family(calls)
    original_spin_schema = dict(fam._children["spin"].input_schema)

    first = fam.build_schema()
    spin_condition = next(c for c in first["allOf"] if c["if"]["properties"]["action"]["const"] == "spin")
    spin_condition["then"]["properties"]["input"]["properties"]["speed"]["type"] = "string"

    second = fam.build_schema()
    second_spin_condition = next(c for c in second["allOf"] if c["if"]["properties"]["action"]["const"] == "spin")
    assert second_spin_condition["then"]["properties"]["input"]["properties"]["speed"]["type"] == "integer"

    # The child's own canonical schema is untouched.
    assert fam._children["spin"].input_schema["properties"]["speed"]["type"] == "integer"
    assert dict(fam._children["spin"].input_schema) == original_spin_schema

    # The sibling ``anyOf`` branch from the SAME first call is untouched —
    # allOf.then.input and the anyOf branch do not share a container either.
    first_spin_branch = next(b for b in first["properties"]["input"]["anyOf"] if b["title"] == "spin input")
    assert first_spin_branch["properties"]["speed"]["type"] == "integer"


# ---------------------------------------------------------------------------
# Diagnostic sidecar — compiler-style hints for a recognized structural
# failure (``tool_family/CONTRACT.md`` "Diagnostics sidecar").
#
# ``ChildTool.diagnostics: Mapping[str, DiagnosticDescriptor] | None`` is
# keyed by *structural trigger* (today only ``TRIGGER_UNSUPPORTED_INPUT_FIELD``),
# not by field name — an opted-in child owns exactly one static
# ``DiagnosticDescriptor`` (``code``/``expected_form``/``reason``/``fix``) per
# trigger and that same text is reused for EVERY safe foreign field name
# ``handle()`` encounters for that trigger; there is no per-field
# registration. This is a passive sidecar declared adjacent to a child's
# existing name/input_schema/handler — never part of ``input_schema`` itself,
# so it can never reach ``build_schema()``'s wire output (see
# ``test_tool_family_wire_parity.py``). For the *selected* action's own
# ``set(action_input) - allowed`` rejection (the "unsupported <family> input
# field" structural failure), ``ToolFamily.handle()`` emits one additive
# ``diagnostics`` list entry per offending field whose *label* passes the
# generic safety check (conventional-identifier-shaped, no secret-shaped
# substring) — never guessing, never parsing prose, never consulting a
# central tool-name table. Each entry is
# ``{"location": "<family>/<action>/input.<field>", **descriptor-fields}``,
# where ``location`` is the only value ``ToolFamily`` computes itself. Legacy
# ``status``/``error_code``/``message`` are unchanged either way. No
# ``diagnostics`` key is added at all when the child never opted in for the
# trigger, or when every offending field's label fails the safety check.
# ---------------------------------------------------------------------------

_SPIN_DIAGNOSTIC = DiagnosticDescriptor(
    code="WIDGET_SPIN_UNSUPPORTED_INPUT_FIELD",
    expected_form="an input object containing only speed",
    reason="spin rejects foreign action input before it can spin",
    fix="remove the foreign field or choose the action that owns it",
)


def _spin_child_with_diagnostics(
    calls: list[dict], descriptor: DiagnosticDescriptor = _SPIN_DIAGNOSTIC
) -> ChildTool:
    def handler(input_):
        calls.append(dict(input_))
        return {"status": "ok", "action": "spin", "speed": input_.get("speed")}

    return ChildTool(
        name="spin",
        input_schema={
            "type": "object",
            "properties": {"speed": {"type": "integer"}},
            "required": ["speed"],
            "additionalProperties": False,
        },
        handler=handler,
        title="spin input",
        diagnostics={TRIGGER_UNSUPPORTED_INPUT_FIELD: descriptor},
    )


def _expected_entry(family: str, action: str, field: str, descriptor: DiagnosticDescriptor) -> dict:
    return {
        "location": f"{family}/{action}/input.{field}",
        "code": descriptor.code,
        "expected_form": descriptor.expected_form,
        "reason": descriptor.reason,
        "fix": descriptor.fix,
    }


def test_opted_in_child_yields_owner_defined_descriptor_and_mechanical_location():
    """An opted-in child's own static descriptor is emitted verbatim,
    additive to the exact legacy failure, at a mechanically derived
    ``family/action/input.field`` location."""
    calls: list[dict] = []
    child = _spin_child_with_diagnostics(calls)
    fam = ToolFamily("widget", [child, _manual_child()])

    result = fam.handle({"action": "spin", "input": {"speed": 1, "turbo": True}, "reasoning": "r"})

    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "unsupported widget input field"
    assert result["diagnostics"] == [_expected_entry("widget", "spin", "turbo", _SPIN_DIAGNOSTIC)]
    assert calls == []  # no handler I/O on a rejected call


def test_opted_in_child_emits_its_descriptor_for_every_safe_foreign_field_sorted():
    """There is no per-field registration: an opted-in child's ONE static
    descriptor is reused for every safe foreign field name present, one
    diagnostics entry per field, in sorted field-name order (matching
    ``handle()``'s own ``sorted(fields)`` iteration)."""
    calls: list[dict] = []
    child = _spin_child_with_diagnostics(calls)
    fam = ToolFamily("widget", [child, _manual_child()])

    result = fam.handle({"action": "spin", "input": {"speed": 1, "zeta": True, "alpha": True}})

    assert result["diagnostics"] == [
        _expected_entry("widget", "spin", "alpha", _SPIN_DIAGNOSTIC),
        _expected_entry("widget", "spin", "zeta", _SPIN_DIAGNOSTIC),
    ]
    assert calls == []


def test_opted_out_child_yields_exact_legacy_three_key_failure():
    """A child that never opts in (default ``diagnostics=None``) renders the
    exact pre-existing three-key failure — no additive key, no behavior
    change from before this feature existed."""
    calls: list[dict] = []
    fam = _widget_family(calls)  # plain ``_spin_child`` — no diagnostics sidecar

    result = fam.handle({"action": "spin", "input": {"speed": 1, "turbo": True}})

    assert result == {
        "status": "failed",
        "error_code": "INVALID_ARGUMENT",
        "message": "unsupported widget input field",
    }
    assert "diagnostics" not in result
    assert calls == []


def test_secret_shaped_field_label_is_dropped_from_diagnostics_and_never_leaked():
    """A foreign field whose *label itself* looks secret-shaped (contains an
    unsafe substring like ``token``) must never be surfaced in a diagnostic,
    even on an opted-in child — the generic safety check, not molt-specific
    logic. Legacy failure stays exact, no ``diagnostics`` key, and neither
    the label nor its raw value ever leaks into the serialized result."""
    calls: list[dict] = []
    child = _spin_child_with_diagnostics(calls)
    fam = ToolFamily("widget", [child, _manual_child()])

    result = fam.handle({"action": "spin", "input": {"speed": 1, "api_token": "sk-should-never-leak"}})

    assert result == {
        "status": "failed",
        "error_code": "INVALID_ARGUMENT",
        "message": "unsupported widget input field",
    }
    assert "diagnostics" not in result
    dumped = json.dumps(result)
    assert "api_token" not in dumped
    assert "sk-should-never-leak" not in dumped
    assert calls == []


def test_non_identifier_shaped_field_label_is_dropped_from_diagnostics():
    """A foreign field whose label is not conventional-identifier-shaped
    (punctuation/whitespace) is dropped the same way as an unsafe one — the
    exact legacy failure, no ``diagnostics`` key, no leak."""
    calls: list[dict] = []
    child = _spin_child_with_diagnostics(calls)
    fam = ToolFamily("widget", [child, _manual_child()])

    result = fam.handle({"action": "spin", "input": {"speed": 1, "not an identifier!": "x"}})

    assert result == {
        "status": "failed",
        "error_code": "INVALID_ARGUMENT",
        "message": "unsupported widget input field",
    }
    assert "diagnostics" not in result
    assert "not an identifier!" not in json.dumps(result)
    assert calls == []


def test_mixed_offending_fields_only_the_safe_identifier_label_is_surfaced():
    """A safe identifier-shaped field, a secret-shaped-label field, and a
    non-identifier-shaped field arrive together: only the safe one gets a
    diagnostics entry, and the other two never appear anywhere — not their
    label, not any value — in the serialized result. Proves the drop is
    per-field, not all-or-nothing."""
    calls: list[dict] = []
    child = _spin_child_with_diagnostics(calls)
    fam = ToolFamily("widget", [child, _manual_child()])

    result = fam.handle({
        "action": "spin",
        "input": {
            "speed": 1,
            "turbo": True,              # safe, identifier-shaped
            "api_token": "sk-secret",   # unsafe: secret-shaped label
            "not valid!": "x",          # unsafe: non-identifier-shaped
        },
    })

    assert result["diagnostics"] == [_expected_entry("widget", "spin", "turbo", _SPIN_DIAGNOSTIC)]
    dumped = json.dumps(result)
    assert "api_token" not in dumped
    assert "sk-secret" not in dumped
    assert "not valid!" not in dumped
    assert calls == []


def test_diagnostics_never_echoes_a_raw_rejected_value():
    """A secret-shaped raw value submitted for a safe-labeled field must
    never appear anywhere in the diagnostic payload — only the static
    descriptor text and the mechanically computed location are ever
    emitted."""
    calls: list[dict] = []
    child = _spin_child_with_diagnostics(calls)
    fam = ToolFamily("widget", [child, _manual_child()])
    secret = "sk-super-secret-token-should-never-leak"

    result = fam.handle({"action": "spin", "input": {"speed": 1, "turbo": secret}})

    assert secret not in json.dumps(result)


def test_diagnostics_sidecar_is_owner_defined_not_a_generic_central_table():
    """Two unrelated families, each opting in their OWN distinct descriptor
    text for the same-named offending field, each get back their own text
    verbatim — proving ``ToolFamily`` holds no central tool-name/message
    table and does not guess a tool-specific reason itself."""
    calls_a: list[dict] = []
    calls_b: list[dict] = []
    descriptor_a = DiagnosticDescriptor(
        code="WIDGET_A_UNSUPPORTED_INPUT_FIELD",
        expected_form="an input object containing only speed",
        reason="family A's own reason",
        fix="family A's own fix",
    )
    descriptor_b = DiagnosticDescriptor(
        code="WIDGET_B_UNSUPPORTED_INPUT_FIELD",
        expected_form="an input object containing only speed",
        reason="family B's own reason",
        fix="family B's own fix",
    )
    fam_a = ToolFamily(
        "widget_a",
        [_spin_child_with_diagnostics(calls_a, descriptor_a), _manual_child()],
    )
    fam_b = ToolFamily(
        "widget_b",
        [_spin_child_with_diagnostics(calls_b, descriptor_b), _manual_child()],
    )

    result_a = fam_a.handle({"action": "spin", "input": {"speed": 1, "turbo": True}})
    result_b = fam_b.handle({"action": "spin", "input": {"speed": 1, "turbo": True}})

    assert result_a["diagnostics"] == [_expected_entry("widget_a", "spin", "turbo", descriptor_a)]
    assert result_b["diagnostics"] == [_expected_entry("widget_b", "spin", "turbo", descriptor_b)]


def test_diagnostics_do_not_relocate_extra_fields_or_weaken_the_allowed_set():
    """The sidecar only adds explanatory payload to an existing rejection —
    it must never coerce/relocate the offending field into the handler's
    input, and must never widen what the handler ends up receiving."""
    calls: list[dict] = []
    child = _spin_child_with_diagnostics(calls)
    fam = ToolFamily("widget", [child, _manual_child()])

    result = fam.handle({"action": "spin", "input": {"speed": 1, "turbo": True}})

    assert result["status"] == "failed"
    assert calls == []  # the handler never ran — no relocation, no I/O
