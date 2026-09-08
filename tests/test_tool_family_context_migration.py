"""Context's LTP v2 / ToolFamily evidence.

One family's local evidence, in the sense `src/lingtai/tools/CONTRACT.md`
"Contract tests" permits — not a universal conformance suite. Chosen for this
family's own risks: `context` owns the molt — the single most irreversible
operation any agent can perform — plus the record/apply pair that rewrites what
the provider actually sees. So the emphasis here is on *refusal before
mutation*: a mis-shaped envelope must be rejected with nothing written, nothing
shed, and no molt consumed.

This family replaces `psyche`, which mixed the context lifecycle with the
agent's name. The two name actions moved to `system` (evidence in
`tests/test_tool_family_system_migration.py`), the pad/lingtai roots split out
earlier (`tests/test_pad_lingtai_split.py`), and the public
`system(action='summarize')` moved *in* as the explicit `summarize`/`rebuild`
action pair. The proof that no `psyche` root survives anywhere is below.

Context is also the one family that genuinely *consumes* the kernel-injected
`_tc_id` rather than merely dropping it — `molt` needs that wire id to locate
and replay its own ToolCallBlock. The isolation tests below pin that boundary.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai.tools import context as context_tool
from lingtai.tools.context import ACTION_ORDER, get_schema
from tests._service_helpers import make_gemini_mock_service as make_mock_service


_VALID_JOURNAL = """\
---
name: 2026-07-27-molt-1-test
description: A test session journal entry for the molt gate.
date: 2026-07-27
molt_count: 1
type: session-journal
---

## What this segment was about
Testing the context ToolFamily contract.
"""

_JOURNAL_REL = "knowledge/session-journal/2026-07-27-molt-1-test/KNOWLEDGE.md"


def _agent(tmp_path, **kwargs):
    return Agent(
        service=make_mock_service(), agent_name="test",
        working_dir=tmp_path / "test", **kwargs,
    )


def _write_journal(agent, rel: str = _JOURNAL_REL) -> str:
    path = agent._working_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_VALID_JOURNAL, encoding="utf-8")
    return rel


def _call(agent, args: dict) -> dict:
    return agent._intrinsics["context"](args)


def _molt_input(journal_path, summary="briefing", **over):
    payload = {
        "summary": summary,
        "session_journal_path": journal_path,
        "keep_tool_calls": None,
        "keep_last": None,
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------------------
# One public root; the exact preserved operation inventory.
# ---------------------------------------------------------------------------


def test_one_public_context_root_with_the_exact_action_inventory():
    """The four actions, in the exact public order, and nothing else.

    `molt` is the lifecycle operation carried over from `psyche.context_molt`;
    `summarize`/`rebuild` are the explicit pair that replaced the former
    `system(action='summarize', rebuild=<bool>)` boolean discriminator.
    """
    schema = get_schema("en")
    assert schema["properties"]["action"]["enum"] == [
        "molt",                    # shed the conversation
        "summarize",               # record only
        "rebuild",                 # apply pending summaries
        "manual",                  # root manual (family-owned)
    ]
    assert len(ACTION_ORDER) == 4


def test_context_is_registered_exactly_once_as_an_intrinsic():
    from lingtai.tools.registry import BUILTIN_TOOLS, INTRINSICS

    assert INTRINSICS["context"]["module"] is context_tool
    # Not also a capability, and no second model-facing root or alias.
    assert "context" not in BUILTIN_TOOLS
    # ``psyche`` is a separate intrinsic — never a second context root and
    # never a context alias. The former ``pad``/``lingtai`` roots it replaced
    # are not intrinsics at all any more.
    assert INTRINSICS["psyche"]["module"] is not context_tool
    assert "pad" not in INTRINSICS
    assert "lingtai" not in INTRINSICS


def test_no_old_psyche_action_survives_anywhere():
    """The dissolved family leaves no action, alias, or context spelling.

    A public root named ``psyche`` exists again — five manuals plus redacted Pad
    settings for the four durable domains — but it grants the old family's
    actions nothing. Root reuse is not action compatibility, so this pins the
    ACTIONS, not the name.
    """
    from lingtai.tools import psyche as psyche_tool
    from lingtai.tools.registry import BUILTIN_TOOLS, INTRINSICS

    assert "psyche" not in BUILTIN_TOOLS  # an intrinsic, never a capability
    assert "anima" not in BUILTIN_TOOLS

    # The current root has five manual loaders plus read-only settings SHOW.
    assert psyche_tool.ACTION_ORDER == (
        "pad", "lingtai", "knowledge", "skills", "settings", "manual",
    )
    for retired in (
        "lingtai_update", "lingtai_load", "pad_edit", "pad_load", "pad_append",
        "context_molt", "name_set", "name_nickname",
    ):
        assert retired not in psyche_tool.ACTION_ORDER
        assert retired not in _actions_of(psyche_tool)
        # ...and they did not leak onto `context` either.
        assert retired not in get_schema("en")["properties"]["action"]["enum"]
    assert INTRINSICS["psyche"]["module"] is psyche_tool


def _actions_of(module):
    return module.get_schema()["properties"]["action"]["enum"]


def test_the_root_is_the_closed_ltp_v2_envelope():
    schema = get_schema("en")
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["summarize"]["type"] == "boolean"
    # The pre-migration `object` key is gone entirely — no alias, no shim.
    assert "object" not in schema["properties"]


def test_schema_and_dispatch_come_from_one_registry():
    """A child cannot be schema-advertised but dispatch-rejected."""
    schema = get_schema("en")
    advertised = list(schema["properties"]["action"]["enum"])
    branch_titles = [b["title"] for b in schema["properties"]["input"]["anyOf"]]
    correlated = [
        c["if"]["properties"]["action"]["const"] for c in schema["allOf"]
    ]
    assert advertised == list(ACTION_ORDER)
    assert correlated == advertised
    assert branch_titles == [f"{name} input" for name in advertised]


def test_each_action_advertises_only_its_own_input():
    """Per-action fields no longer leak onto every other action.

    Each field belongs only to the action that consumes it: the molt fields to
    `molt`, and `items` to the two context-hygiene actions. `content` — the
    former name-action field — belongs to no context action at all now that
    naming lives on `system`.
    """
    schema = get_schema("en")
    props = {
        c["if"]["properties"]["action"]["const"]:
            set(c["then"]["properties"]["input"]["properties"])
        for c in schema["allOf"]
    }
    assert props["molt"] == {
        "summary", "session_journal_path", "keep_tool_calls", "keep_last",
    }
    assert props["summarize"] == {"items"}
    assert props["rebuild"] == {"items"}
    assert props["manual"] == set()
    # Naming left for ``system``; no context action advertises ``content``.
    assert not any("content" in fields for fields in props.values())
    # The root presentation bool is never domain input at any action.
    assert not any("summarize" in fields for fields in props.values())
    # Every branch is closed.
    for cond in schema["allOf"]:
        assert cond["then"]["properties"]["input"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Strict schema / dispatch rejection for every action.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", [a for a in ACTION_ORDER])
def test_unknown_root_field_is_rejected_for_every_action(tmp_path, action):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, {
            "action": action, "input": {}, "reasoning": "why", "bogus": 1,
        })
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALID_ARGUMENT"
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("action", [a for a in ACTION_ORDER])
def test_non_bool_summarize_is_rejected_for_every_action(tmp_path, action):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, {
            "action": action, "input": {}, "summarize": "yes",
        })
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert "summarize must be a boolean" in result["message"]
    finally:
        agent.stop(timeout=1.0)


@pytest.mark.parametrize("bad_action", [[], {}, None, 3, "context_forget"])
def test_unhashable_or_unknown_action_renders_the_stable_error(tmp_path, bad_action):
    """Invalid JSON can make `action` unhashable (issue #513) — never a TypeError.

    `context_forget` is included deliberately: it is a real internal function
    but was never an agent-callable action, and must not become one.
    """
    agent = _agent(tmp_path)
    try:
        result = _call(agent, {"action": bad_action, "input": {}})
        assert "error" in result
        assert "Unknown context action" in result["error"]
    finally:
        agent.stop(timeout=1.0)


def test_wrong_branch_input_is_rejected_before_any_handler_io(tmp_path):
    """A cross-action smuggle writes nothing and sheds nothing."""
    agent = _agent(tmp_path)
    try:
        # `summary` is a molt field; it belongs to no summarize/rebuild input.
        result = _call(agent, {
            "action": "summarize",
            "input": {"items": [], "summary": "smuggled"},
        })
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert "unsupported context input field" in result["message"]

        # And the reverse: a foreign field cannot reach the molt. `files`
        # belongs to the split-out pad family and to no context action at all.
        before = agent._molt_count
        result = _call(agent, {
            "action": "molt",
            "input": _molt_input(_JOURNAL_REL, files=["x.txt"]),
        })
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert agent._molt_count == before
    finally:
        agent.stop(timeout=1.0)


def test_non_object_input_is_rejected(tmp_path):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, {"action": "manual", "input": "notanobject"})
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert "input must be an object" in result["message"]
    finally:
        agent.stop(timeout=1.0)


def test_reasoning_and_summarize_never_reach_a_handler(tmp_path):
    """Envelope controls are not action input, and never appear in a branch."""
    schema = get_schema("en")
    for cond in schema["allOf"]:
        branch = cond["then"]["properties"]["input"]["properties"]
        for reserved in ("reasoning", "_reasoning", "summarize", "_tc_id"):
            assert reserved not in branch

    agent = _agent(tmp_path)
    seen: list[dict] = []
    try:
        import lingtai.tools.context as mod

        def spy(agent_arg, args):
            seen.append(dict(args))
            return {"status": "ok"}

        saved = mod._CHILD_SPECS
        mod._CHILD_SPECS = tuple(
            (n, s, spy if n == "rebuild" else h) for n, s, h in saved
        )
        try:
            result = _call(agent, {
                "action": "rebuild", "input": {"items": None},
                "reasoning": "check", "summarize": True, "_tc_id": "toolu_x",
            })
            assert result["status"] == "ok"
            # The spy really ran — otherwise the assertions below are vacuous.
            assert len(seen) == 1, "rebuild handler was not the dispatched child"
            for reserved in ("reasoning", "_reasoning", "summarize", "_tc_id"):
                assert reserved not in seen[0]
        finally:
            mod._CHILD_SPECS = saved
    finally:
        agent.stop(timeout=1.0)


def test_tc_id_is_isolated_to_the_molt_handler(tmp_path):
    """`_tc_id` is stripped from the closed root but still reaches the molt.

    Context is the one migrated family that genuinely *consumes* this transport
    key rather than merely dropping it (`soul`/`notification`/`system` drop
    it). It must therefore neither break the closed-root check nor leak to any
    other action.
    """
    agent = _agent(tmp_path)
    try:
        # It does not trip the unknown-root-field check on an unrelated action.
        result = _call(agent, {
            "action": "summarize", "input": {"items": []},
            "_tc_id": "toolu_abc",
        })
        assert "error_code" not in result

        # And a molt without it is refused with the internal-guard message
        # rather than shedding context, once the journal gate has passed.
        _write_journal(agent)
        agent._session.ensure_session()
        before = agent._molt_count
        result = _call(agent, {
            "action": "molt", "input": _molt_input(_JOURNAL_REL),
        })
        assert "error" in result
        assert "_tc_id" in result["error"]
        assert agent._molt_count == before
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Destructive / read-only risk semantics.
# ---------------------------------------------------------------------------






def test_read_only_actions_mutate_no_durable_state(tmp_path):
    """`manual` is psyche's only remaining pure read.

    ``lingtai_load``/``pad_load`` left with their families; the equivalent
    assertion for them lives in ``tests/test_pad_lingtai_split.py``. The pad
    and identity files are still checked here, because a psyche `manual` call
    must not disturb another family's durable state either.
    """
    agent = _agent(tmp_path)
    agent.start()
    try:
        system = agent._working_dir / "system"
        (system / "pad.md").write_text("keep me", encoding="utf-8")
        (system / "lingtai.md").write_text("identity", encoding="utf-8")
        before_molt = agent._molt_count

        _call(agent, {"action": "manual", "input": {}})

        assert (agent._working_dir / "system" / "pad.md").read_text() == "keep me"
        assert (agent._working_dir / "system" / "lingtai.md").read_text() == "identity"
        assert agent._molt_count == before_molt
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Molt: refusal before shed, and a successful disposable lifecycle.
# ---------------------------------------------------------------------------


def _molt_agent(tmp_path):
    """An agent with a real ChatInterface so a molt can actually run."""
    from lingtai.kernel.llm.interface import ChatInterface, TextBlock

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        chat.interface = iface
        chat.context_window.return_value = 100_000
        return chat

    svc.create_session.side_effect = fake_create_session
    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    agent._session.ensure_session()
    agent._session._chat.interface.add_user_message("Hello")
    agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])
    return agent


def _emit_molt_call(agent, wire_id, action_input):
    """Append the agent's own molt call to history, in the full strict shape.

    `_context_molt` replays this exact block into the fresh session, where it
    becomes model-visible history. So the fixture must emit what a real
    provider call looks like — every key `molt`'s strict schema marks
    required, with unused optionals as explicit null — not a convenient partial
    dict. Emitting a partial here would let the replay assertions pass against
    a shape the advertised schema rejects.
    """
    from lingtai.kernel.llm.interface import ToolCallBlock

    payload = _molt_input(
        action_input.get("session_journal_path"),
        summary=action_input.get("summary"),
        keep_tool_calls=action_input.get("keep_tool_calls"),
        keep_last=action_input.get("keep_last"),
    )
    agent._session._chat.interface.add_assistant_message([
        ToolCallBlock(
            id=wire_id, name="context",
            args={"action": "molt", "input": payload},
        ),
    ])


@pytest.mark.parametrize("journal,reason", [
    (None, "missing"),
    ("knowledge/session-journal/KNOWLEDGE.md", "parent index"),
    ("tmp/scratch.md", "scratch path"),
])
def test_molt_refuses_before_shedding_on_an_invalid_journal(tmp_path, journal, reason):
    """The gate runs before snapshot/archive/wipe/molt_count — fail closed."""
    agent = _molt_agent(tmp_path)
    try:
        wire = "toolu_psyche_gate"
        _emit_molt_call(agent, wire, {"summary": "briefing"})
        before_count = agent._molt_count
        before_chat = agent._session._chat

        result = _call(agent, {
            "action": "molt",
            "input": _molt_input(journal),
            "_tc_id": wire,
        })

        assert "error" in result, reason
        assert agent._molt_count == before_count
        assert agent._session._chat is before_chat
        # Nothing shed: no snapshot and no archive were written.
        assert not (agent._working_dir / "history" / "snapshots").exists()
        assert not (agent._working_dir / "history" / "chat_history_archive.jsonl").exists()
    finally:
        agent.stop()


def test_molt_refuses_an_empty_summary_before_the_journal_gate(tmp_path):
    agent = _molt_agent(tmp_path)
    try:
        _write_journal(agent)
        before = agent._molt_count
        result = _call(agent, {
            "action": "molt",
            "input": _molt_input(_JOURNAL_REL, summary=""),
            "_tc_id": "toolu_psyche_empty",
        })
        assert "empty" in result["error"].lower()
        assert agent._molt_count == before
    finally:
        agent.stop()


def test_successful_molt_lifecycle_in_a_disposable_workdir(tmp_path):
    """A full molt on a pytest tmp_path agent — never a live agent directory."""
    agent = _molt_agent(tmp_path)
    try:
        journal = _write_journal(agent)
        wire = "toolu_psyche_molt_ok"
        summary = "Findings: X=42. Next: analyze Z."
        action_input = {"summary": summary, "session_journal_path": journal}
        _emit_molt_call(agent, wire, action_input)

        result = _call(agent, {
            "action": "molt",
            "input": _molt_input(journal, summary=summary),
            "_tc_id": wire,
            "reasoning": "context is full",
        })

        # Outer result fields are exactly the pre-migration set.
        assert result["status"] == "ok"
        assert result["molt_count"] == 1
        assert result["session_journal_path"] == journal
        for key in ("note", "tokens_before", "tokens_after", "tokens_shed",
                    "kept_tool_calls", "kept_last", "archive_path", "summary_path"):
            assert key in result

        # Durable stores: summary written with the journal recorded.
        summary_file = agent._working_dir / result["summary_path"]
        assert "source: agent" in summary_file.read_text()
        assert journal in summary_file.read_text()

        # Snapshot persisted for past-self consultation.
        snapshots = sorted((agent._working_dir / "history" / "snapshots").glob("*.json"))
        assert len(snapshots) == 1
        assert json.loads(snapshots[0].read_text())

        # The molt's own call was replayed into the fresh session, verbatim,
        # in the migrated envelope — so history teaches a shape dispatch accepts.
        from lingtai.kernel.llm.interface import ToolCallBlock
        iface = agent._session._chat.interface
        last = [e for e in iface.entries if e.role == "assistant"][-1]
        replayed = [b for b in last.content if isinstance(b, ToolCallBlock)][0]
        assert replayed.id == wire
        assert replayed.name == "context"
        assert replayed.args["action"] == "molt"
        assert replayed.args["input"]["summary"] == summary
        # The replayed block is model-visible history, so it must satisfy the
        # strict schema this family advertises — same obligation the synthesized
        # forced-molt pair carries.
        molt_schema = next(
            c["then"]["properties"]["input"] for c in get_schema("en")["allOf"]
            if c["if"]["properties"]["action"]["const"] == "molt"
        )
        assert set(replayed.args["input"]) == set(molt_schema["required"])
        assert not set(replayed.args["input"]) - set(molt_schema["properties"])

        # Post-molt reminder published.
        assert (agent._working_dir / ".notification" / "post-molt.json").is_file()
    finally:
        agent.stop()


def test_system_forced_molt_synthesizes_the_current_envelope(tmp_path):
    """`context_forget`'s synthesized pair is model-visible history.

    It is replayed to the provider as an assistant `tool_use` block, so it must
    carry the exact envelope the schema advertises: tool name `context`, action
    `molt`, closed input, Host-authored reasoning. A model imitating a stale
    `psyche`/`context_molt` shape would now be rejected.
    """
    from lingtai.kernel.llm.interface import ToolCallBlock
    from lingtai.tools.context import context_forget
    from lingtai.tools.context._molt import SYSTEM_FORCED_MOLT_REASONING

    agent = _molt_agent(tmp_path)
    try:
        result = context_forget(agent, source="warning_ladder")
        assert result["status"] == "ok"
        assert result["_initiator"] == "system"

        iface = agent._session._chat.interface
        synth = [
            b for e in iface.entries if e.role == "assistant"
            for b in e.content if isinstance(b, ToolCallBlock)
        ][-1]
        assert synth.args["action"] == "molt"
        assert synth.args["reasoning"] == SYSTEM_FORCED_MOLT_REASONING

        # The replayed `input` must satisfy the strict schema this family
        # advertises — every required key present, with the three the forced
        # path does not use spelled as explicit null (the provider-compatible
        # representation of "absent"). A partial object here would teach a
        # model imitating its own history a call the schema rejects.
        molt_schema = next(
            c["then"]["properties"]["input"] for c in get_schema("en")["allOf"]
            if c["if"]["properties"]["action"]["const"] == "molt"
        )
        action_input = synth.args["input"]
        assert set(action_input) == set(molt_schema["required"])
        assert set(action_input) == {
            "summary", "session_journal_path", "keep_tool_calls", "keep_last",
        }
        assert action_input["summary"]
        assert action_input["session_journal_path"] is None
        assert action_input["keep_tool_calls"] is None
        assert action_input["keep_last"] is None
        # No key outside the branch's own declared properties.
        assert not set(action_input) - set(molt_schema["properties"])

        # Provenance stays OUTSIDE input — it is not action input.
        assert synth.name == "context"
        assert synth.args["_initiator"] == "system"
        assert "_initiator" not in action_input
        assert "_source" not in action_input
        assert "object" not in synth.args
    finally:
        agent.stop()


def test_kernel_detects_the_migrated_molt_call_shape():
    """The turn loop's molt-batch deferral reads the migrated spelling."""
    from lingtai.kernel.base_agent.turn import _batch_includes_context_molt
    from lingtai.kernel.llm.base import ToolCall

    migrated = ToolCall(
        name="context",
        args={"action": "molt", "input": {"summary": "s"}},
        id="call_molt",
    )
    assert _batch_includes_context_molt([migrated]) is True
    # A non-molt context call must not trigger the deferral.
    other = ToolCall(
        name="context", args={"action": "summarize", "input": {"items": []}},
        id="call_sum",
    )
    assert _batch_includes_context_molt([other]) is False
    # Neither may a stale `psyche` name, which no longer exists as a tool,
    # nor the old `context_molt` action spelling on the current root.
    stale_root = ToolCall(
        name="psyche", args={"action": "context_molt", "input": {}}, id="call_old",
    )
    assert _batch_includes_context_molt([stale_root]) is False
    stale_action = ToolCall(
        name="context", args={"action": "context_molt", "input": {}}, id="call_old2",
    )
    assert _batch_includes_context_molt([stale_action]) is False


# ---------------------------------------------------------------------------
# Reserved manual: no double wrap, and no context operation.
# ---------------------------------------------------------------------------


def test_manual_child_returns_the_canonical_result_unwrapped(tmp_path):
    """`ToolFamily.handle()` returns the reserved child's result verbatim."""
    from lingtai.tools.context import _build_children
    from lingtai.tools.tool_family import ToolFamily

    agent = _agent(tmp_path)
    try:
        family = ToolFamily("psyche", _build_children(agent))
        raw = family.handle({"action": "manual", "input": {}})
        # Canonical ManualTool shape — full body + host-local path, no flatten.
        assert raw["content"][0]["text"]
        assert raw["structuredContent"]["manual_path"]
        assert "manual" not in raw  # not double-wrapped into psyche's flat shape
    finally:
        agent.stop(timeout=1.0)


def test_manual_public_result_is_flattened_post_dispatch(tmp_path):
    """Psyche's own Host layer restores its pinned flat public shape."""
    agent = _agent(tmp_path)
    try:
        result = _call(agent, {"action": "manual", "input": {}})
        assert result["status"] in ("ok", "degraded")
        assert result["manual"]
        assert result["manual_path"]
        # The canonical fields do not leak into the public result.
        assert "content" not in result
        assert "structuredContent" not in result
    finally:
        agent.stop(timeout=1.0)


def test_manual_rejects_any_input_key(tmp_path):
    agent = _agent(tmp_path)
    try:
        result = _call(agent, {"action": "manual", "input": {"content": "x"}})
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALID_ARGUMENT"
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Kernel allowlist + wire parity.
# ---------------------------------------------------------------------------


def test_context_is_on_the_ltp_v2_summarize_allowlist():
    """Advertising root `summarize` obliges joining the kernel allowlist."""
    from lingtai.kernel.tool_result_summary import summary_requested

    assert summary_requested({"summarize": True}, tool_name="context") is True
    assert summary_requested({"summarize": False}, tool_name="context") is False
    # `summary` is context's own molt domain field, never this control.
    assert summary_requested({"summary": "a briefing"}, tool_name="context") is False
    # Distinguish the two things the name ``psyche`` now refers to. The
    # DISSOLVED family's action inventory is gone (pinned in
    # ``test_no_old_psyche_action_survives_anywhere``). The INSTALLED family of
    # that name — the manual-only durable-domain root — is a real migrated LTP
    # v2 family, so its root ``summarize`` IS recognized here. Recognizing the
    # control says nothing about the old actions.
    assert summary_requested({"summarize": True}, tool_name="psyche") is True


def test_one_context_root_survives_both_wires_with_action_input_correlation(tmp_path):
    """Exactly one public root, closed, `reasoning` required, on both wires.

    Also proves the root `allOf` action/input correlation reaches the provider
    intact — including that `session_journal_path` stays bound to `molt`'s
    branch, which is what lets a provider reject a mis-paired molt before it is
    ever dispatched.
    """
    from lingtai.kernel.base_agent.tools import _build_tool_schemas
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    live = _agent(tmp_path)
    try:
        schemas = _build_tool_schemas(live)
        context_schemas = [s for s in schemas if s.name == "context"]
        assert len(context_schemas) == 1, "exactly one public context root"
        # A ``psyche`` root exists again — five durable-domain manuals plus
        # redacted Pad settings — but it is never a second context root/alias.
        psyche_schemas = [s for s in schemas if s.name == "psyche"]
        assert len(psyche_schemas) == 1
        assert psyche_schemas[0].parameters["properties"]["action"]["enum"] == [
            "pad", "lingtai", "knowledge", "skills", "settings", "manual",
        ]

        chat = _build_tools(context_schemas)[0]["function"]["parameters"]
        responses = _build_responses_tools(context_schemas)[0]["parameters"]
        for wire, combinator in ((chat, "anyOf"), (responses, "anyOf")):
            assert set(wire["properties"]) == {
                "action", "input", "reasoning", "summarize",
            }
            # Agent composition re-injects the identical `reasoning` property
            # text but never touches `required` — the family's own composed
            # schema is what keeps it required.
            assert wire["required"] == ["action", "input", "reasoning"]
            assert wire["properties"]["action"]["enum"] == list(ACTION_ORDER)
            branches = wire["properties"]["input"][combinator]
            assert [b["title"] for b in branches] == [
                f"{a} input" for a in ACTION_ORDER
            ]
            assert len(wire["allOf"]) == len(ACTION_ORDER)
            molt = next(
                c["then"]["properties"]["input"] for c in wire["allOf"]
                if c["if"]["properties"]["action"]["const"] == "molt"
            )
            assert "session_journal_path" in molt["properties"]
    finally:
        live.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# summarize / rebuild — the record-vs-apply split that replaced the old
# ``system(action='summarize', rebuild=<bool>)`` boolean discriminator.
# ---------------------------------------------------------------------------


class _SummarizeStubAgent:
    """Disposable double for the record/apply engine — never a live agent."""

    def __init__(self, working_dir):
        self._working_dir = working_dir
        self.agent_name = "stub"
        self.events: list = []
        self._chat = None
        self._summarize_notification_threshold = 3000

    def _log(self, event, **fields):
        self.events.append((event, fields))

    def _reconstruct_context(self):
        self.events.append(("context_reconstructed", {}))


def _summarize_call(agent, args: dict) -> dict:
    return context_tool.handle(agent, args)


def test_summarize_requires_items_and_never_rebuilds(tmp_path):
    """The record-only action refuses an empty call instead of rebuilding.

    Under the old boolean surface ``rebuild=false`` with no items was the
    ``missing_items`` invalid no-op. The explicit action keeps exactly that
    behavior — it must not silently become a pure rebuild.
    """
    agent = _SummarizeStubAgent(tmp_path)
    result = _summarize_call(agent, {"action": "summarize", "input": {"items": []}})
    assert result["status"] == "error"
    assert result["reason"] == "missing_items"
    assert result["notification_threshold_chars"] == 3000

    # ``items`` is REQUIRED on this action — it is not the rebuild branch.
    assert context_tool._SUMMARIZE_INPUT_SCHEMA["required"] == ["items"]
    assert "rebuild" not in context_tool._SUMMARIZE_INPUT_SCHEMA["properties"]


def test_rebuild_items_is_optional_at_the_schema_and_wire_contract():
    """`context(action='rebuild', input={})` must be schema-VALID, not just accepted.

    The ordinary rebuild call carries no items. If ``items`` were listed in the
    branch's ``required`` (the usual "required but nullable" convention this
    package uses elsewhere), the schema would forbid the exact call the manual
    and Contract document, and a strict provider could reject it before dispatch
    ever ran. So this one field is genuinely optional at the family layer, and
    the generated wire branch must agree with the source of truth.
    """
    # Source of truth: the canonical child schema.
    assert context_tool._REBUILD_INPUT_SCHEMA["required"] == []
    # Still closed, and an explicit null is still accepted for providers that
    # materialize every declared property.
    assert context_tool._REBUILD_INPUT_SCHEMA["additionalProperties"] is False
    assert "null" in context_tool._REBUILD_INPUT_SCHEMA["properties"]["items"]["type"]

    # Generated model-facing branch: composed by the generic ToolFamily from the
    # schema above, so it must not reintroduce the requirement.
    rebuild_branch = next(
        cond["then"]["properties"]["input"] for cond in get_schema("en")["allOf"]
        if cond["if"]["properties"]["action"]["const"] == "rebuild"
    )
    assert rebuild_branch.get("required", []) == []
    # And the record-only action is unaffected: it still demands items.
    summarize_branch = next(
        cond["then"]["properties"]["input"] for cond in get_schema("en")["allOf"]
        if cond["if"]["properties"]["action"]["const"] == "summarize"
    )
    assert summarize_branch["required"] == ["items"]


def test_rebuild_items_stays_optional_on_both_provider_wires(tmp_path):
    """The optional-`items` contract must survive Chat and Responses alike."""
    from lingtai.kernel.base_agent.tools import _build_tool_schemas
    from lingtai.llm.openai.adapter import _build_responses_tools, _build_tools

    live = _agent(tmp_path)
    try:
        schemas = [s for s in _build_tool_schemas(live) if s.name == "context"]
        chat = _build_tools(schemas)[0]["function"]["parameters"]
        responses = _build_responses_tools(schemas)[0]["parameters"]
        for wire, combinator in ((chat, "anyOf"), (responses, "anyOf")):
            branches = {
                b["title"]: b for b in wire["properties"]["input"][combinator]
            }
            assert branches["rebuild input"].get("required", []) == []
            assert branches["summarize input"]["required"] == ["items"]
    finally:
        live.stop(timeout=1.0)


def test_rebuild_with_no_items_is_the_ordinary_pure_rebuild(tmp_path):
    """``{}`` and an explicit null are the same no-new-items rebuild call."""
    agent = _SummarizeStubAgent(tmp_path)
    # No chat session — the pure-rebuild path reports that honestly, exactly as
    # ``system(action='summarize', rebuild=true)`` did.
    for action_input in ({}, {"items": None}):
        result = _summarize_call(agent, {"action": "rebuild", "input": action_input})
        assert result["status"] == "error"
        assert result["reason"] == "no_chat_session"
        assert result["notification_threshold_chars"] == 3000


def test_summarize_records_a_pending_marker_and_does_not_rebuild(tmp_path):
    from lingtai.kernel.llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock
    from lingtai.tools.system.summarize import SUMMARIZE_MARKER, SUMMARY_STATUS_PENDING

    iface = ChatInterface()
    iface.add_assistant_message([ToolCallBlock(id="tc-1", name="bash", args={})])
    iface.add_tool_results([ToolResultBlock(id="tc-1", name="bash", content="X" * 400)])

    requested: list = []

    class _Chat:
        interface = iface

        def request_history_rebuild(self, reason: str = "") -> bool:
            requested.append(reason)
            return True

    agent = _SummarizeStubAgent(tmp_path)
    agent._chat = _Chat()

    result = _summarize_call(agent, {
        "action": "summarize",
        "input": {"items": [{"tool_call_id": "tc-1", "summary": "digested"}]},
    })
    assert result["status"] == "ok"
    assert result["mode"] == "summarize"
    assert result["summarized"] == 1
    assert result["failed"] == 0
    assert "pending_summary_totals" in result
    assert "reconstruction" in result
    assert result["notification_threshold_chars"] == 3000

    block = iface._entries[1].content[0]
    assert block.content["artifact"] == SUMMARIZE_MARKER
    assert block.content["status"] == SUMMARY_STATUS_PENDING
    # Record-only: the action must NOT have asked for a provider rebuild.
    assert requested == []


def test_rebuild_with_items_records_and_applies_in_one_call(tmp_path):
    from lingtai.kernel.llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock
    from lingtai.tools.system.summarize import SUMMARY_STATUS_DONE

    iface = ChatInterface()
    iface.add_assistant_message([ToolCallBlock(id="tc-2", name="bash", args={})])
    iface.add_tool_results([ToolResultBlock(id="tc-2", name="bash", content="Y" * 400)])

    requested: list = []

    class _Chat:
        interface = iface

        def request_history_rebuild(self, reason: str = "") -> bool:
            requested.append(reason)
            return True

    agent = _SummarizeStubAgent(tmp_path)
    agent._chat = _Chat()

    result = _summarize_call(agent, {
        "action": "rebuild",
        "input": {"items": [{"tool_call_id": "tc-2", "summary": "digested"}]},
    })
    assert result["mode"] == "rebuild"
    assert result["rebuild_requested"] is True
    assert result["marked_done"] == ["tc-2"]
    assert requested == ["summarize_rebuild_only"]
    assert iface._entries[1].content[0].content["status"] == SUMMARY_STATUS_DONE


def test_pending_summaries_survive_to_a_later_bare_rebuild(tmp_path):
    """Record now, apply later — the two-call flow the split exists for."""
    from lingtai.kernel.llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock
    from lingtai.tools.system.summarize import SUMMARY_STATUS_DONE, SUMMARY_STATUS_PENDING

    iface = ChatInterface()
    iface.add_assistant_message([ToolCallBlock(id="tc-3", name="bash", args={})])
    iface.add_tool_results([ToolResultBlock(id="tc-3", name="bash", content="Z" * 400)])

    requested: list = []

    class _Chat:
        interface = iface

        def request_history_rebuild(self, reason: str = "") -> bool:
            requested.append(reason)
            return True

    agent = _SummarizeStubAgent(tmp_path)
    agent._chat = _Chat()

    _summarize_call(agent, {
        "action": "summarize",
        "input": {"items": [{"tool_call_id": "tc-3", "summary": "digested"}]},
    })
    assert iface._entries[1].content[0].content["status"] == SUMMARY_STATUS_PENDING
    assert requested == []

    result = _summarize_call(agent, {"action": "rebuild", "input": {"items": None}})
    assert result["mode"] == "rebuild"
    assert result["marked_done"] == ["tc-3"]
    assert requested == ["summarize_rebuild_only"]
    assert iface._entries[1].content[0].content["status"] == SUMMARY_STATUS_DONE


# ---------------------------------------------------------------------------
# Diagnostic sidecar — the first concrete local declaration
# (SONNET_DIAGNOSTIC_CONTRACT.md "Required design" #6): ``context.molt``
# opts a static descriptor in for its own foreign-input-field structural
# failure (example field ``files``, already exercised without the
# diagnostic in ``test_wrong_branch_input_is_rejected_before_any_handler_io``
# above). These tests pin the additive ``diagnostics`` payload verbatim
# against the shared contract's own candidate shape, that molt state is
# untouched (no I/O), and that an action which does NOT opt in (summarize,
# receiving molt's own ``session_journal_path``) keeps the exact
# pre-existing legacy failure with no additive key at all.
# ---------------------------------------------------------------------------


def test_molt_foreign_files_field_yields_the_documented_local_diagnostic(tmp_path):
    """A foreign ``files`` field on molt's own input must produce the
    documented additive ``diagnostics`` entry, on top of the exact legacy
    three-key failure, while never touching molt state (no snapshot/archive/
    shed) — the sidecar only adds explanation, never relaxes fail-closed I/O
    ordering. It must also never echo the raw rejected value anywhere."""
    agent = _agent(tmp_path)
    try:
        before_count = agent._molt_count
        result = _call(agent, {
            "action": "molt",
            "input": _molt_input(_JOURNAL_REL, files=["x.txt"]),
        })

        # Exact legacy compatibility: status/error_code/message unchanged.
        assert result["status"] == "failed"
        assert result["error_code"] == "INVALID_ARGUMENT"
        assert "unsupported context input field" in result["message"]

        # The documented additive diagnostic, verbatim per the shared
        # contract's candidate shape — molt safely states its own allowed
        # input field set and explains itself, without claiming anything
        # about `session_journal_path` needing to be relative/absolute.
        assert result["diagnostics"] == [{
            "location": "context/molt/input.files",
            "code": "CTX_MOLT_UNSUPPORTED_INPUT_FIELD",
            "expected_form": (
                "an input object containing only summary, "
                "session_journal_path, keep_tool_calls, and keep_last"
            ),
            "reason": "molt rejects foreign action input before it can shed context",
            "fix": "remove the foreign field or choose the action that owns it",
        }]

        # No molt state changed — this is explanation, not relaxed I/O.
        assert agent._molt_count == before_count
        assert not (agent._working_dir / "history" / "snapshots").exists()
        assert not (agent._working_dir / "history" / "chat_history_archive.jsonl").exists()

        # No raw rejected value, path, or exception string leaked anywhere.
        dumped = json.dumps(result)
        assert "x.txt" not in dumped
    finally:
        agent.stop(timeout=1.0)


def test_cross_action_session_journal_path_on_summarize_keeps_the_undiagnosed_legacy_failure(tmp_path):
    """A foreign field cross-smuggled onto a DIFFERENT action than the one
    that owns a descriptor gets that selected action's own behavior. Only
    ``context.molt`` opts into a local descriptor this slice ("Required
    design" #1: "A selected action must own static safe descriptor(s) for
    the structural trigger it opts into"), so ``context.summarize``
    receiving molt's own ``session_journal_path`` field stays the exact
    pre-existing legacy three-key failure, with no additive ``diagnostics``
    key at all."""
    agent = _agent(tmp_path)
    try:
        result = _call(agent, {
            "action": "summarize",
            "input": {"items": [], "session_journal_path": _JOURNAL_REL},
        })
        assert result == {
            "status": "failed",
            "error_code": "INVALID_ARGUMENT",
            "message": "unsupported context input field",
        }
        assert "diagnostics" not in result
    finally:
        agent.stop(timeout=1.0)


def test_molt_secret_shaped_unsafe_field_label_is_dropped_from_diagnostics(tmp_path):
    """``molt`` is opted in for ``TRIGGER_UNSUPPORTED_INPUT_FIELD``, but a
    foreign field whose *label itself* looks secret-shaped (contains an
    unsafe substring like ``token``) must never be surfaced in a diagnostic
    — the generic ``tool_family`` safety check, not molt-specific logic.
    Legacy failure stays exact, no ``diagnostics`` key at all, the label and
    its raw value never leak into the serialized result, and molt state is
    untouched (fail-closed, no I/O)."""
    agent = _agent(tmp_path)
    try:
        before_count = agent._molt_count
        result = _call(agent, {
            "action": "molt",
            "input": _molt_input(_JOURNAL_REL, api_token="sk-should-never-leak"),
        })

        assert result == {
            "status": "failed",
            "error_code": "INVALID_ARGUMENT",
            "message": "unsupported context input field",
        }
        assert "diagnostics" not in result
        dumped = json.dumps(result)
        assert "api_token" not in dumped
        assert "sk-should-never-leak" not in dumped

        assert agent._molt_count == before_count
        assert not (agent._working_dir / "history" / "snapshots").exists()
        assert not (agent._working_dir / "history" / "chat_history_archive.jsonl").exists()
    finally:
        agent.stop(timeout=1.0)


def test_molt_non_identifier_shaped_field_label_is_dropped_from_diagnostics(tmp_path):
    """A foreign field whose label is not conventional-identifier-shaped
    (contains punctuation/whitespace) is dropped the same way as an unsafe
    one — the exact legacy failure, no ``diagnostics`` key, no leak of
    either the label or its raw value, and molt state untouched
    (fail-closed, no I/O)."""
    agent = _agent(tmp_path)
    try:
        before_count = agent._molt_count
        sentinel_value = "molt-non-identifier-label-sentinel-2c9f7e1b"
        payload = _molt_input(_JOURNAL_REL)
        payload["not an identifier!"] = sentinel_value
        result = _call(agent, {"action": "molt", "input": payload})

        assert result == {
            "status": "failed",
            "error_code": "INVALID_ARGUMENT",
            "message": "unsupported context input field",
        }
        assert "diagnostics" not in result
        dumped = json.dumps(result)
        assert "not an identifier!" not in dumped
        assert sentinel_value not in dumped

        assert agent._molt_count == before_count
        assert not (agent._working_dir / "history" / "snapshots").exists()
        assert not (agent._working_dir / "history" / "chat_history_archive.jsonl").exists()
    finally:
        agent.stop(timeout=1.0)


def test_root_summarize_bool_is_never_domain_input_of_the_summarize_action(tmp_path):
    """The ACTION named ``summarize`` and the ROOT bool named ``summarize``.

    They coexist at different envelope levels and must never be conflated: the
    root bool is the cross-cutting presentation control the generic dispatcher
    strips, and no child declares a ``summarize`` property.
    """
    schema = get_schema("en")
    assert schema["properties"]["summarize"]["type"] == "boolean"
    for cond in schema["allOf"]:
        assert "summarize" not in cond["then"]["properties"]["input"]["properties"]

    # Passing the root bool alongside the summarize action reaches the engine
    # as a normal record-only call — it is not read as domain input.
    agent = _SummarizeStubAgent(tmp_path)
    result = _summarize_call(agent, {
        "action": "summarize", "input": {"items": []},
        "reasoning": "why", "summarize": True,
    })
    assert result["reason"] == "missing_items"
