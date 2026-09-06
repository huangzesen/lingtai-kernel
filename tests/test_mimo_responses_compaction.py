"""Tests for MiMo's native OpenAI Responses wire, its default wire selection,
the explicit Chat Completions escape hatch, and standalone
``/responses/compact`` compaction with its hard-failure policy.

Live wire evidence (2026-07-14): MiMo's official Responses API
(``POST https://api.xiaomimimo.com/v1/responses``) accepts a two-turn
function_call + exact prior output item + function_call_output replay,
confirming stateless full-history/raw-output-item replay is provider-correct.
The docs (``https://mimo.mi.com/static/docs/api/chat/responses.md``) mark
``previous_response_id`` and ``context_management`` as explicitly
incompatible, support ``function_call_output``, and require callers to
manage context manually. ``store`` and ``conversation`` are likewise
unsupported. MiMo's standalone ``POST /v1/responses/compact`` endpoint
currently returns a provider error on the live API — this suite locks in
that a MiMo standalone-compaction failure is a HARD failure (propagates),
unlike Codex's non-fatal policy (see ``tests/test_codex_standalone_compaction.py``).

These are pure/mock tests — no network, no credentials.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lingtai.llm.mimo.adapter import (
    MimoAdapter,
    MimoChatSession,
    MimoCompactionHardFailure,
    MimoResponsesSession,
)
from lingtai.kernel.llm.interface import ChatInterface, ToolResultBlock


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _usage(input_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=5,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _message_response(text: str, input_tokens: int, resp_id: str = "resp_1"):
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        usage=_usage(input_tokens),
        id=resp_id,
    )


def _tool_call_response(call_id: str, name: str, input_tokens: int, resp_id: str = "resp_tc"):
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                call_id=call_id,
                name=name,
                arguments="{}",
            )
        ],
        usage=_usage(input_tokens),
        id=resp_id,
    )


class FakeCompactItem:
    """Mimics an SDK model object with ``.model_dump()``."""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self, mode: str = "json", exclude_none: bool = True):
        return dict(self._data)


_OPAQUE_MARKER = "OPAQUE_ENCRYPTED_CONTENT_MUST_NEVER_BE_LOGGED"


def _default_compact_output() -> list[FakeCompactItem]:
    return [
        FakeCompactItem({
            "type": "message",
            "id": "msg_compacted",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "compacted summary text"}],
        }),
        FakeCompactItem({
            "type": "compaction_summary",
            "id": "cs_1",
            "encrypted_content": _OPAQUE_MARKER,
        }),
    ]


class ScriptedResponses:
    """Fake ``client.responses`` — a queue of scripted turn responses plus
    ``compact()``. Each ``create()`` consumes the next scripted response."""

    def __init__(self, turns: list, compact_output=None, compact_error: Exception | None = None):
        self._turns = list(turns)
        self._idx = 0
        self.create_calls: list[dict] = []
        self.compact_calls: list[dict] = []
        self._compact_output = compact_output if compact_output is not None else _default_compact_output()
        self._compact_error = compact_error

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        assert self._idx < len(self._turns), (
            f"ScriptedResponses ran out of scripted turns at call {self._idx + 1}"
        )
        resp = self._turns[self._idx]
        self._idx += 1
        return resp

    def compact(self, **kwargs):
        self.compact_calls.append(kwargs)
        if self._compact_error is not None:
            raise self._compact_error
        return SimpleNamespace(output=list(self._compact_output))


class FakeClient:
    def __init__(self, turns, compact_output=None, compact_error=None):
        self.responses = ScriptedResponses(turns, compact_output, compact_error)


def _make_adapter(**kwargs) -> MimoAdapter:
    return MimoAdapter(api_key="fake", base_url="https://api.xiaomimimo.com", **kwargs)


def _override_projected_provider_tokens(session, values: list[int]) -> None:
    """Make compaction state-machine tests independent of the active tokenizer.

    These tests exercise boundary/replay/failure behavior, not the estimator's
    arithmetic.  The real local tokenizer (tiktoken, sentencepiece, or a
    fallback) is therefore intentionally *not* the trigger oracle here:
    state-machine tests inject deterministic projected provider-token values
    with comfortable headroom.  Dedicated projection/calibration tests should
    not use this seam, so they continue to exercise the real logic.
    """
    projected = iter(values)

    def deterministic_projection(_prebuilt_items=None):
        try:
            return next(projected)
        except StopIteration as exc:  # Catch an accidentally unplanned request.
            raise AssertionError("test consumed more projected-token values than scripted") from exc

    session._projected_provider_tokens = deterministic_projection


def _make_session(
    *,
    context_window: int = 1_000_000,
    compact_token_limit: int | None = None,
    turns: list | None = None,
    compact_output=None,
    compact_error=None,
    projected_provider_tokens: list[int] | None = None,
) -> tuple[MimoResponsesSession, ScriptedResponses]:
    adapter = _make_adapter(compact_token_limit=compact_token_limit)
    client = FakeClient(turns or [], compact_output, compact_error)
    adapter._client = client
    session = adapter.create_chat(
        "mimo-v2.5-pro", "system prompt", tools=None, context_window=context_window,
    )
    if projected_provider_tokens is not None:
        _override_projected_provider_tokens(session, projected_provider_tokens)
    return session, client.responses


# ---------------------------------------------------------------------------
# Default session/wire selection
# ---------------------------------------------------------------------------


def test_default_wire_is_responses():
    adapter = _make_adapter()
    assert adapter._wire_api == "responses"
    assert adapter._should_use_responses() is True


def test_default_create_chat_builds_mimo_responses_session():
    adapter = _make_adapter()
    adapter._client = FakeClient([_message_response("hi", 10)])
    session = adapter.create_chat("mimo-v2.5-pro", "system prompt", tools=None)
    assert isinstance(session, MimoResponsesSession)


def test_responses_session_never_stateful():
    """No store/previous_response_id/conversation on an ordinary send —
    MiMo's Responses API is documented-stateless."""
    session, fake = _make_session(turns=[_message_response("hi", 10)])
    session.send("hello")
    sent = fake.create_calls[0]
    assert "store" not in sent
    assert "previous_response_id" not in sent
    assert "conversation" not in sent


# ---------------------------------------------------------------------------
# Explicit Chat Completions escape hatch
# ---------------------------------------------------------------------------


def test_explicit_chat_completions_wire_api_selects_chat_session():
    adapter = _make_adapter(wire_api="chat_completions")
    assert adapter._wire_api == "chat_completions"
    assert adapter._should_use_responses() is False
    adapter._client = SimpleNamespace()
    session = adapter.create_chat("mimo-v2.5-pro", "system prompt", tools=None)
    assert isinstance(session, MimoChatSession)


def test_factory_forwards_explicit_wire_api_from_defaults():
    from lingtai.llm._register import register_all_adapters
    from lingtai.llm.service import LLMService

    register_all_adapters()
    factory = LLMService._adapter_registry["mimo"]
    adapter = factory(
        model="mimo-v2.5-pro",
        defaults={"wire_api": "chat_completions"},
        api_key="test",
        base_url="https://api.xiaomimimo.com",
    )
    assert adapter._wire_api == "chat_completions"


def test_factory_omitted_wire_api_defaults_to_responses():
    from lingtai.llm._register import register_all_adapters
    from lingtai.llm.service import LLMService

    register_all_adapters()
    factory = LLMService._adapter_registry["mimo"]
    adapter = factory(
        model="mimo-v2.5-pro", defaults=None, api_key="test",
        base_url="https://api.xiaomimimo.com",
    )
    assert adapter._wire_api == "responses"


# ---------------------------------------------------------------------------
# No context_management on ordinary MiMo Responses requests
# ---------------------------------------------------------------------------


def test_never_sends_context_management():
    session, fake = _make_session(
        compact_token_limit=100,
        # Cross the standalone threshold deterministically; the single turn
        # still has no safe boundary, and generic context_management remains
        # absent regardless of tokenizer behavior.
        projected_provider_tokens=[250],
        turns=[_message_response("ok", 10)],
    )
    assert session._compact_threshold is None
    session.send("hello")
    assert "context_management" not in fake.create_calls[0]


def test_generic_compact_threshold_stays_none_even_if_adapter_default_would_set_one():
    """MimoAdapter forces compact_threshold=None for its Responses session
    regardless of the base OpenAIAdapter's normal 100k default."""
    adapter = _make_adapter()
    adapter._client = FakeClient([_message_response("ok", 10)])
    session = adapter.create_chat("mimo-v2.5-pro", "system prompt", tools=None)
    assert session._compact_threshold is None


# ---------------------------------------------------------------------------
# Stateless full-history / raw-output-item replay
# ---------------------------------------------------------------------------


def test_stateless_full_history_replay_across_turns():
    session, fake = _make_session(
        turns=[_message_response("first", 10), _message_response("second", 15)],
    )
    session.send("turn one")
    session.send("turn two")
    second_input = fake.create_calls[1]["input"]
    joined = str(second_input)
    assert "turn one" in joined
    assert "turn two" in joined
    # The assistant's own prior reply must also replay (true statelessness —
    # MiMo has no server-side memory of what it said).
    assert "first" in joined


def test_tool_call_and_result_replay_pairing():
    session, fake = _make_session(
        turns=[
            _tool_call_response("call_1", "search", 20),
            _message_response("done", 25),
        ],
    )
    response = session.send("please search")
    assert response.tool_calls and response.tool_calls[0].id == "call_1"

    tool_result = ToolResultBlock(id="call_1", name="search", content={"result": "found"})
    session.send([tool_result])

    sent = fake.create_calls[1]["input"]
    types = [item.get("type") for item in sent if isinstance(item, dict)]
    assert "function_call" in types and "function_call_output" in types
    assert types.index("function_call") < types.index("function_call_output")


# ---------------------------------------------------------------------------
# Daemon threshold propagation to MiMo
# ---------------------------------------------------------------------------


def test_daemon_provider_defaults_injects_mimo_compact_token_limit():
    from lingtai.tools.daemon import DaemonManager
    from pathlib import Path

    mgr = DaemonManager.__new__(DaemonManager)
    run_dir = SimpleNamespace(path=Path("/tmp/fake-daemon-run-mimo-compaction"))

    with_limit = mgr._daemon_provider_defaults("mimo", {}, run_dir, context_token_limit=12_345)
    assert with_limit["mimo"]["mimo_compact_token_limit"] == 12_345

    without_limit = mgr._daemon_provider_defaults("mimo", {}, run_dir, context_token_limit=None)
    assert without_limit is None

    # Unrelated provider unaffected.
    non_mimo = mgr._daemon_provider_defaults("anthropic", {}, run_dir, context_token_limit=99_999)
    assert non_mimo is None or "mimo_compact_token_limit" not in non_mimo.get("anthropic", {})


def test_factory_forwards_mimo_compact_token_limit_from_defaults():
    from lingtai.llm._register import register_all_adapters
    from lingtai.llm.service import LLMService

    register_all_adapters()
    factory = LLMService._adapter_registry["mimo"]
    adapter = factory(
        model="mimo-v2.5-pro",
        defaults={"mimo_compact_token_limit": 777},
        api_key="test",
        base_url="https://api.xiaomimimo.com",
    )
    assert adapter._mimo_compact_token_limit == 777


def test_explicit_task_limit_overrides_context_window():
    session, _ = _make_session(context_window=1_000_000, compact_token_limit=500)
    assert session._effective_compact_token_limit() == 500


def test_omitted_task_limit_inherits_context_window():
    session, _ = _make_session(context_window=42_000, compact_token_limit=None)
    assert session._effective_compact_token_limit() == 42_000


# ---------------------------------------------------------------------------
# Standalone compact success / replay ordering / boundary
# ---------------------------------------------------------------------------


def test_compaction_fires_and_replays_opaque_prefix_with_live_trailing_turn():
    session, fake = _make_session(
        context_window=1_000_000,
        compact_token_limit=150,
        # Keep this state-machine trigger independent of tiktoken/sentencepiece:
        # three comfortably-below estimates, then a comfortably-above one.
        projected_provider_tokens=[100, 100, 100, 250],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
            _message_response("r4", 60),
        ],
    )
    for i in range(3):
        session.send(f"warmup {i}")
    assert session._compacted_items is None

    session.send("trigger")

    assert session._compacted_items is not None
    sent_input = fake.create_calls[-1]["input"]
    types = {item.get("type") for item in sent_input if isinstance(item, dict)}
    assert "compaction_summary" in types
    # The live turn that triggered compaction must ride as the trailing item,
    # never folded into the opaque summary.
    assert sent_input[-1] == {"role": "user", "content": "trigger"}
    compact_input = fake.compact_calls[0]["input"]
    assert not any(
        isinstance(item, dict) and "trigger" in str(item.get("content", ""))
        for item in compact_input
    )


def test_compact_request_shape_no_store_no_context_management():
    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=150,
        # Tokenizer-independent state-machine trigger; 250 is deliberate
        # headroom above the unchanged 150-token contract threshold.
        projected_provider_tokens=[100, 100, 100, 250],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
            _message_response("r4", 60),
        ],
    )
    for i in range(3):
        session.send(f"warmup {i}")
    session.send("trigger")

    assert len(fake.compact_calls) == 1
    call = fake.compact_calls[0]
    assert call["model"] == "mimo-v2.5-pro"
    assert "store" not in call
    assert "context_management" not in call
    assert "previous_response_id" not in call
    assert "conversation" not in call
    assert "input" in call


def test_compact_request_kwargs_bind_against_real_sdk_signature():
    import inspect

    from openai.resources.responses import Responses

    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=150,
        # The SDK-signature assertion needs a deterministic state-machine
        # trigger; it is not a tokenizer calibration test.
        projected_provider_tokens=[100, 100, 100, 250],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
            _message_response("r4", 60),
        ],
    )
    for i in range(3):
        session.send(f"warmup {i}")
    session.send("trigger")

    call = fake.compact_calls[0]
    sig = inspect.signature(Responses.compact)
    sig.bind(None, **call)  # raises TypeError if any kwarg is invalid


def test_additive_delta_after_compaction_only_appends_new_entries():
    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=150,
        # The fifth value keeps the post-compaction delta below the threshold;
        # all trigger decisions are deterministic rather than tokenizer-driven.
        projected_provider_tokens=[100, 100, 100, 250, 100],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
            _message_response("r4", 60), _message_response("r5", 60),
        ],
    )
    for i in range(3):
        session.send(f"warmup {i}")
    session.send("trigger")
    assert session._compacted_items is not None
    opaque_prefix = list(session._compacted_items)

    session.send("post-compaction message")
    replay = session._compacted_replay_input()

    # The opaque compacted prefix rides verbatim at the head of the replay —
    # additive, not re-derived from scratch.
    assert replay[: len(opaque_prefix)] == opaque_prefix
    assert any(
        isinstance(item, dict) and item.get("content") == "post-compaction message"
        for item in replay
    )


def test_no_encrypted_content_logged(caplog):
    import logging

    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=150,
        # Logging assertions still need a real compaction, but must not depend
        # on whichever tokenizer happens to be installed in the test runner.
        projected_provider_tokens=[100, 100, 100, 250],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
            _message_response("r4", 60),
        ],
    )
    for i in range(3):
        session.send(f"warmup {i}")
    with caplog.at_level(logging.DEBUG):
        session.send("trigger")
    assert session._compacted_items is not None
    for record in caplog.records:
        assert _OPAQUE_MARKER not in record.getMessage()


def test_no_compaction_strictly_below_threshold():
    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=100_000,
        # Explicitly exercise the strict-below branch without making its
        # no-trigger result depend on the installed tokenizer.
        projected_provider_tokens=[100, 100, 100, 100, 100, 100],
        turns=[_message_response(f"r{i}", 20) for i in range(6)],
    )
    for i in range(6):
        session.send(f"turn {i}: short message")
    assert session._compacted_items is None
    assert len(fake.compact_calls) == 0


# ---------------------------------------------------------------------------
# MiMo hard failure propagation (the key behavioral divergence from Codex)
# ---------------------------------------------------------------------------


def test_compact_provider_error_is_a_hard_failure():
    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=150,
        # Force only the fourth request over the threshold with ample margin;
        # this failure-path test intentionally does not ask a tokenizer to
        # decide whether the state machine reaches ``compact()``.
        projected_provider_tokens=[100, 100, 100, 250],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
        ],
        compact_error=RuntimeError("simulated MiMo /responses/compact provider error"),
    )
    for i in range(3):
        session.send(f"warmup {i}")
    entries_before = len(session._interface.entries)

    with pytest.raises(MimoCompactionHardFailure):
        session.send("trigger")

    # Hard failure must not silently continue on full history: the just
    # staged trailing entry is rolled back (base OpenAIResponsesSession
    # stateless-replay error path), same as any other failed stateless send.
    assert len(session._interface.entries) == entries_before
    assert session._compacted_items is None


def test_compact_empty_output_is_a_hard_failure():
    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=150,
        # Deterministic trigger with safe headroom; empty-output handling is
        # deliberately tested after the boundary logic has been reached.
        projected_provider_tokens=[100, 100, 100, 250],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
        ],
        compact_output=[],
    )
    for i in range(3):
        session.send(f"warmup {i}")

    with pytest.raises(MimoCompactionHardFailure):
        session.send("trigger")


def test_hard_failure_does_not_fall_back_to_chat_completions_wire():
    """A compact failure must propagate — never silently retry the same
    turn on the Chat Completions session/wire instead."""
    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=150,
        # Deterministic trigger with safe headroom keeps this wire/fallback
        # assertion independent of the installed tokenizer.
        projected_provider_tokens=[100, 100, 100, 250],
        turns=[
            _message_response("r0", 50), _message_response("r1", 80),
            _message_response("r2", 110), _message_response("r3", 200),
        ],
        compact_error=RuntimeError("boom"),
    )
    for i in range(3):
        session.send(f"warmup {i}")

    with pytest.raises(MimoCompactionHardFailure):
        session.send("trigger")
    # Still a MimoResponsesSession — no silent session-type swap occurred.
    assert isinstance(session, MimoResponsesSession)


def test_no_safe_boundary_yet_is_not_a_hard_failure():
    """Distinct from an actual compact failure: when there simply isn't
    enough history for a safe boundary, this is a no-op, not an error."""
    session, fake = _make_session(
        context_window=1_000_000, compact_token_limit=1,
        # Cross the threshold deterministically, then prove that the missing
        # boundary (not tokenizer arithmetic) makes compaction a no-op.
        projected_provider_tokens=[2],
        turns=[_message_response("only turn", 999_999)],
        compact_error=RuntimeError("must not be called"),
    )
    session.send("turn 0")
    assert session._compacted_items is None
    assert len(fake.compact_calls) == 0


# ---------------------------------------------------------------------------
# Unchanged Codex non-fatal behavior (regression guard against the mixin
# extraction accidentally coupling the two failure policies)
# ---------------------------------------------------------------------------


def test_codex_standalone_compaction_failure_remains_non_fatal():
    from lingtai.llm.openai.adapter import CodexOpenAIAdapter

    class ExplodingScriptedResponses:
        def __init__(self, turns):
            self._turns = list(turns)
            self._idx = 0
            self.compact_calls = []
            self.create_calls = []

        def create(self, **kwargs):
            self.create_calls.append(kwargs)
            events = self._turns[self._idx]
            self._idx += 1
            return iter(events)

        def compact(self, **kwargs):
            self.compact_calls.append(kwargs)
            raise RuntimeError("simulated compact endpoint failure")

    from dataclasses import dataclass

    @dataclass
    class Event:
        type: str
        response: object | None = None

    def text_turn(tokens):
        return [Event("response.completed", response=SimpleNamespace(id="r", usage=_usage(tokens)))]

    adapter = CodexOpenAIAdapter(
        api_key="fake", base_url="http://fake", use_responses=True,
        force_responses=True, codex_compact_token_limit=150,
    )
    client = SimpleNamespace()
    client.responses = ExplodingScriptedResponses(
        [text_turn(50), text_turn(80), text_turn(110), text_turn(200), text_turn(60)],
    )
    adapter._client = client
    session = adapter.create_chat("gpt-5.6-sol", "system prompt", tools=None, context_window=1000)
    # This is a Codex failure-policy regression guard, not a tokenizer test:
    # drive its state machine with the same deterministic trigger seam so the
    # assertion remains portable across tokenizer backends.
    _override_projected_provider_tokens(session, [100, 100, 100, 100, 250])
    for i in range(4):
        session.send(f"warmup {i}")

    # Must NOT raise — Codex compaction failure is non-fatal.
    session.send("trigger")
    assert session._compacted_items is None
    assert len(client.responses.compact_calls) == 1


# ---------------------------------------------------------------------------
# Unrelated providers ignored
# ---------------------------------------------------------------------------


def test_unrelated_providers_never_receive_mimo_compact_token_limit():
    from lingtai.tools.daemon import DaemonManager
    from pathlib import Path

    mgr = DaemonManager.__new__(DaemonManager)
    run_dir = SimpleNamespace(path=Path("/tmp/fake-daemon-run-unrelated"))

    for provider in ("anthropic", "gemini", "openai", "deepseek", "openrouter"):
        result = mgr._daemon_provider_defaults(provider, {}, run_dir, context_token_limit=42)
        assert result is None or "mimo_compact_token_limit" not in result.get(provider, {})
        assert result is None or "codex_compact_token_limit" not in result.get(provider, {})


# ---------------------------------------------------------------------------
# Doc consistency: the first-level daemon manual (SKILL.md) and the daemon
# tool-surface CONTRACT.md must accurately describe native `mimo` and its
# hard-failure divergence from Codex — not just CONTRACT.md/ANATOMY.md
# (which already had dedicated coverage). These are plain text-content
# assertions, no imports of daemon internals, so they fail loudly the moment
# either doc regresses to Codex-only wording regardless of what the code does.
# ---------------------------------------------------------------------------

import re as _re
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[1]
_DAEMON_MANUAL = _REPO_ROOT / "src/lingtai/tools/daemon/manual/SKILL.md"
_DAEMON_LINGTAI_CHILD = (
    _REPO_ROOT
    / "src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/lingtai/SKILL.md"
)
_DAEMON_TOOL_CONTRACT = _REPO_ROOT / "src/lingtai/tools/daemon/CONTRACT.md"


def _context_token_limit_paragraph(text: str) -> str:
    """Extract the prose paragraph(s) describing `context_token_limit`.

    Scoped to the paragraph so a stray unrelated mention of "Codex" elsewhere
    in the document (e.g. the backend alias table) can't produce a false
    negative/positive for these assertions.
    """
    anchors = (
        "- `context_token_limit`:",
        "Per-task `context_token_limit`",
        "## `context_token_limit`",
    )
    idx = next(text.index(anchor) for anchor in anchors if anchor in text)
    # Grab a generous window from the normative description — enough to span the
    # whole descriptive paragraph in both docs without over-fitting to one
    # doc's exact paragraph length.
    return text[idx: idx + 2500]


def test_daemon_lingtai_child_context_token_limit_covers_native_mimo():
    text = _DAEMON_LINGTAI_CHILD.read_text(encoding="utf-8")
    para = _context_token_limit_paragraph(text)
    assert "mimo" in para, "daemon manual context_token_limit prose must mention native mimo"
    assert "Codex" in para, "daemon manual context_token_limit prose must still mention Codex"
    # Must mention the Responses wire / stateless replay default.
    assert "Responses" in para and ("stateless" in para or "full-history" in para)
    # Must never generically claim context_management works for MiMo.
    assert "context_management" in para


def test_daemon_lingtai_child_context_token_limit_distinguishes_hard_failure():
    text = _DAEMON_LINGTAI_CHILD.read_text(encoding="utf-8")
    para = _context_token_limit_paragraph(text)
    assert "non-fatal" in para or "non fatal" in para
    assert "HARD failure" in para or "hard failure" in para.lower()
    # The CLI-backend `mimo`/`mimocode` alias must not be conflated with the
    # native LLM provider `mimo` this capability actually applies to.
    assert "mimocode" in para, (
        "daemon manual must preserve the distinction from the external "
        "mimo/mimocode CLI backend alias"
    )


def test_daemon_manual_context_token_limit_not_codex_only_regression():
    """Locks against regressing to the exact stale Codex-only sentence."""
    text = _DAEMON_MANUAL.read_text(encoding="utf-8")
    assert (
        "Effective only for `backend=\"lingtai\"` tasks whose resolved provider is Codex "
        "(`codex`/`codex-pool`) — every other provider and every external CLI backend ignores it."
    ) not in text


def test_daemon_tool_contract_context_token_limit_covers_native_mimo():
    text = _DAEMON_TOOL_CONTRACT.read_text(encoding="utf-8")
    para = _context_token_limit_paragraph(text)
    assert "mimo" in para, "daemon CONTRACT.md context_token_limit prose must mention native mimo"
    assert "Codex" in para, "daemon CONTRACT.md context_token_limit prose must still mention Codex"
    assert "Responses" in para and ("stateless" in para or "full-history" in para)
    assert "context_management" in para


def test_daemon_tool_contract_context_token_limit_distinguishes_hard_failure():
    text = _DAEMON_TOOL_CONTRACT.read_text(encoding="utf-8")
    para = _context_token_limit_paragraph(text)
    assert "non-fatal" in para or "non fatal" in para
    assert "HARD failure" in para or "hard failure" in para.lower()
    assert "mimocode" in para, (
        "daemon CONTRACT.md must preserve the distinction from the external "
        "mimo/mimocode CLI backend alias"
    )


def test_daemon_tool_contract_verification_matrix_covers_native_mimo():
    """The verification-matrix row and anchored-claims row must both mention
    mimo, not just Codex — regression guard for the specific table rows the
    parent review flagged (anchored claims ~149, verification matrix ~162)."""
    text = _DAEMON_TOOL_CONTRACT.read_text(encoding="utf-8")
    # Anchored-claims row: locate the row mentioning _daemon_provider_defaults
    # and context_token_limit validation.
    claims_row_match = _re.search(
        r"\| `context_token_limit` is validated[^\n]*\|",
        text,
    )
    assert claims_row_match is not None, "anchored-claims row for context_token_limit not found"
    assert "mimo" in claims_row_match.group(0)

    # Verification matrix row.
    matrix_row_match = _re.search(
        r"\| `context_token_limit`[^\n]*\|[^\n]*\|[^\n]*\|[^\n]*\|",
        text,
    )
    assert matrix_row_match is not None, "verification-matrix row for context_token_limit not found"
    assert "mimo" in matrix_row_match.group(0).lower()
    assert "codex-only" not in matrix_row_match.group(0).lower()


def test_daemon_tool_contract_test_command_includes_mimo_compaction_tests():
    text = _DAEMON_TOOL_CONTRACT.read_text(encoding="utf-8")
    assert "tests/test_mimo_responses_compaction.py" in text


def test_daemon_contract_docs_do_not_conflate_cli_mimo_with_native_provider():
    """Both docs must keep the external `mimo`/`mimocode` CLI backend alias
    (an entirely different daemon capability — see the Scope/backend enum
    section) distinguishable from the native `mimo` LLM provider this
    capability boundary actually applies to."""
    for path in (_DAEMON_LINGTAI_CHILD, _DAEMON_TOOL_CONTRACT):
        text = path.read_text(encoding="utf-8")
        para = _context_token_limit_paragraph(text)
        assert "manifest.llm.provider" in para or "LLM provider" in para, (
            f"{path} must clarify context_token_limit applies to the native "
            "mimo LLM provider, not the mimo/mimocode CLI backend alias"
        )
