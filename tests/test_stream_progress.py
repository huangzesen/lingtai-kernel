"""Contract tests for the Core-owned stream-progress Port.

Covers the Port shape and Core technology-neutrality, the shared discovery
known vectors (pinned identically in the Go client), the memory-only
generation-bound state lifecycle (an old generation's late deltas/end never
touch a newer active snapshot), `SessionManager` bracketing (begin-before-wait,
every output fragment's length from the count-only `on_output_chars` callback
bound to the generation `begin` returned, `finally`-clear of that same
generation on success and failure, fail-open, explicit `streaming=False`, and
unchanged no-Port call shape), the `ChatSession`/`send_with_timeout_stream`
boundary, every streaming adapter feeding the one count seam, the System v2
runtime-policy sources, `BaseAgent` factory injection (never called for an
explicit `streaming=False`), and the loopback read-only endpoint.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lingtai.adapters.stream_progress import (
    LoopbackStreamProgressPublisher,
    loopback_stream_progress_factory,
)
from lingtai.kernel.base_agent import BaseAgent
from lingtai.kernel.config import AgentConfig
from lingtai.kernel.config_resolve import parse_jsonc
from lingtai.kernel.llm.base import ChatSession, LLMResponse, ToolCall, UsageMetadata
from lingtai.kernel.llm.interface import ChatInterface
from lingtai.kernel.llm.streaming import OutputProgress
from lingtai.kernel.llm_utils import send_with_timeout_stream
from lingtai.kernel.session import SessionManager
from lingtai.kernel.stream_progress import (
    STREAM_PROGRESS_PATH,
    STREAM_PROGRESS_SCHEMA,
    StreamProgressPort,
    StreamProgressSnapshot,
    StreamProgressState,
    candidate_ports,
    discovery_seed,
)
from lingtai.tools.registry import INTRINSICS as _TEST_INTRINSICS

from tests._agent_presence_helpers import make_test_presence_store
from tests._lifecycle_clock_helpers import make_test_lifecycle_clock
from tests._notification_store_helpers import notification_store_for
from tests._service_helpers import make_tool_result_mock_service as make_mock_service
from tests._snapshot_helpers import make_test_snapshot_port, make_test_source_revision_port
from tests._workdir_lease_helpers import make_test_lease

ROOT = Path(__file__).resolve().parents[1]

# Pinned byte-for-byte with tui/internal/streamprogress/client_test.go.
KNOWN_VECTORS: dict[str, tuple[int, list[int]]] = {
    "20260826-120000-abcd": (58026, [59026, 46945, 54864, 42783, 50702, 58621, 46540, 54459]),
    "orch": (4407, [45407, 53326, 41245, 49164, 57083, 45002, 52921, 60840]),
    "": (29159, [50159, 58078, 45997, 53916, 41835, 49754, 57673, 45592]),
    "器灵-01": (38923, [59923, 47842, 55761, 43680, 51599, 59518, 47437, 55356]),
}

SNAPSHOT_FIELDS = {
    "schema", "agent_id", "generation", "active", "streamed_chars", "updated_unix_ms", "pid",
}


# ---------------------------------------------------------------------------
# Port shape / Core neutrality / discovery
# ---------------------------------------------------------------------------

def test_port_is_three_operations_and_abstract() -> None:
    assert StreamProgressPort.__abstractmethods__ == frozenset({"begin", "add_chars", "end"})
    with pytest.raises(TypeError):
        StreamProgressPort()  # type: ignore[abstract]


def test_core_module_imports_no_transport_or_filesystem_modules() -> None:
    source = (ROOT / "src/lingtai/kernel/stream_progress/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint({"http", "json", "os", "pathlib", "socket"})


@pytest.mark.parametrize("agent_id", sorted(KNOWN_VECTORS))
def test_candidate_ports_known_vectors(agent_id: str) -> None:
    seed, ports = KNOWN_VECTORS[agent_id]
    assert discovery_seed(agent_id) == seed
    assert candidate_ports(agent_id) == ports


def test_candidate_ports_are_eight_in_documented_range() -> None:
    ports = candidate_ports("any-agent")
    assert len(ports) == 8
    assert all(41000 <= p < 61000 for p in ports)


def test_schema_and_path_constants() -> None:
    assert STREAM_PROGRESS_SCHEMA == "lingtai.stream-progress/v1"
    assert STREAM_PROGRESS_PATH == "/v1/stream-progress"


# ---------------------------------------------------------------------------
# Memory-only state
# ---------------------------------------------------------------------------

def _clock(values):
    it = iter(values)
    return lambda: next(it)


def test_state_lifecycle_begin_delta_end() -> None:
    state = StreamProgressState("a1", pid=4242, now_ms=_clock([1, 2, 3, 4, 5, 6]))
    s0 = state.snapshot()
    assert (s0.generation, s0.active, s0.streamed_chars, s0.updated_unix_ms, s0.pid) == (0, False, 0, 1, 4242)

    gen = state.begin()
    assert gen == 1
    s1 = state.snapshot()
    assert (s1.generation, s1.active, s1.streamed_chars, s1.updated_unix_ms) == (1, True, 0, 2)

    state.add_chars(gen, 5)
    state.add_chars(gen, 0)
    state.add_chars(gen, 7)
    s2 = state.snapshot()
    assert (s2.generation, s2.active, s2.streamed_chars, s2.updated_unix_ms) == (1, True, 12, 4)

    state.end(gen)
    s3 = state.snapshot()
    assert (s3.generation, s3.active, s3.streamed_chars, s3.updated_unix_ms) == (1, False, 0, 5)

    assert state.begin() == 2
    assert state.snapshot().generation == 2


def test_state_ignores_deltas_outside_an_active_response() -> None:
    state = StreamProgressState("a1", pid=1)
    state.add_chars(0, 9)
    state.add_chars(1, 9)
    assert state.snapshot().streamed_chars == 0
    gen = state.begin()
    state.add_chars(gen, 3)
    state.end(gen)
    state.add_chars(gen, 11)  # late delta from an abandoned worker
    state.end(gen)  # and a repeated end: idempotent, still cleared
    snap = state.snapshot()
    assert (snap.generation, snap.active, snap.streamed_chars) == (1, False, 0)


def test_state_old_generation_after_new_begin_never_alters_newer_snapshot() -> None:
    # Regression: a timed-out provider worker (generation 1) is abandoned but
    # keeps emitting after the session has begun generation 2. Its late deltas
    # and its late ``end`` must be ignored — the newer active snapshot keeps
    # exactly the characters generation 2 published and stays active.
    state = StreamProgressState("a1", pid=1, now_ms=_clock(range(1, 100)))
    old = state.begin()
    state.add_chars(old, 4)
    new = state.begin()  # old worker timed out; a new response begins
    assert (old, new) == (1, 2)
    fresh = state.snapshot()
    assert (fresh.generation, fresh.active, fresh.streamed_chars) == (2, True, 0)

    state.add_chars(old, 100)  # late old delta
    state.add_chars(new, 6)
    state.end(old)  # late old end
    state.add_chars(old, 100)
    snap = state.snapshot()
    assert (snap.generation, snap.active, snap.streamed_chars) == (2, True, 6)
    assert snap.updated_unix_ms == state.snapshot().updated_unix_ms  # ignored ops did not touch the clock

    # A future/unknown generation is ignored just like a stale one.
    state.add_chars(new + 1, 50)
    state.end(new + 1)
    assert (state.snapshot().active, state.snapshot().streamed_chars) == (True, 6)

    state.end(new)
    done = state.snapshot()
    assert (done.generation, done.active, done.streamed_chars) == (2, False, 0)


def test_snapshot_dict_has_exactly_documented_fields_and_no_text() -> None:
    snap = StreamProgressSnapshot(
        agent_id="a", generation=3, active=True, streamed_chars=40, updated_unix_ms=7, pid=9
    )
    body = snap.to_dict()
    assert set(body) == SNAPSHOT_FIELDS
    assert body["schema"] == STREAM_PROGRESS_SCHEMA
    assert all(isinstance(body[k], int) and not isinstance(body[k], bool)
               for k in ("generation", "streamed_chars", "updated_unix_ms", "pid"))
    assert body["active"] is True
    assert "text" not in json.dumps(body)


# ---------------------------------------------------------------------------
# SessionManager bracketing
# ---------------------------------------------------------------------------

class _RecorderPort(StreamProgressPort):
    """Records the exact operation sequence; optionally raises on chosen ops.

    ``begin`` hands out generations 1, 2, ... exactly like the real state so
    the recorded ``add``/``end`` entries carry the token they were bound to.
    """

    def __init__(self, *, raise_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple] = []
        self.raise_on = raise_on
        self.generation = 0

    def _record(self, *entry) -> None:
        self.calls.append(entry)
        if entry[0] in self.raise_on:
            raise RuntimeError("publisher exploded")

    def begin(self) -> int:
        self.generation += 1
        self._record("begin")
        return self.generation

    def add_chars(self, generation: int, count: int) -> None:
        self._record("add", generation, count)

    def end(self, generation: int) -> None:
        self._record("end", generation)


class _ProbingState(StreamProgressState):
    """Real state that also captures a snapshot after every delta."""

    def __init__(self) -> None:
        super().__init__("probe", pid=1)
        self.after_delta: list[StreamProgressSnapshot] = []

    def add_chars(self, generation: int, count: int) -> None:
        super().add_chars(generation, count)
        self.after_delta.append(self.snapshot())


# Provider output the fake session receives: ``(fragment, visible)`` pairs.
# Every fragment reaches the count-only seam; only visible text reaches the
# legacy ``on_chunk``.
MIXED_OUTPUT = (("héllo", True), ('{"path":"foo.py"}', False), ("sig-bytes", False),
                ("", True), ({"q": "x"}, False), ("器灵", True))


def _make_session(port, *, streaming: bool = True, deltas=(), output=None,
                  fail: Exception | None = None, probe=None):
    """A ``SessionManager`` over a fake session that streams like an adapter:
    it hands every output fragment to a real ``OutputProgress`` and records
    exactly which callbacks the session passed."""
    output = output if output is not None else tuple((d, True) for d in deltas)
    svc = MagicMock()
    svc.model = "test-model"
    chat = MagicMock()
    chat.context_window.return_value = 100000
    chat.interface.estimate_context_tokens.return_value = 5000
    chat.interface.current_system_prompt = "test prompt"
    response = MagicMock(
        text="".join(f for f, visible in output if visible), tool_calls=[], thoughts=[],
        usage=MagicMock(input_tokens=100, output_tokens=50, thinking_tokens=10,
                        cached_tokens=20, extra={}),
    )
    calls: list[dict] = []

    def send_stream(message, on_chunk=None, on_output_chars=None):
        calls.append({"on_chunk": on_chunk is not None, "on_output_chars": on_output_chars is not None,
                      "probe": probe() if probe else None, "count_fn": on_output_chars})
        progress = OutputProgress(on_output_chars)
        for fragment, visible in output:
            progress.add(fragment)
            if visible and on_chunk is not None:
                on_chunk(fragment)
        if fail is not None:
            raise fail
        return response

    chat.send_stream = send_stream
    chat.send.return_value = response
    svc.create_session.return_value = chat
    sm = SessionManager(
        llm_service=svc,
        config=AgentConfig(),
        agent_name="test",
        streaming=streaming,
        build_system_prompt_fn=lambda: "test prompt",
        build_tool_schemas_fn=lambda: [],
        logger_fn=None,
        stream_progress=port,
    )
    return sm, chat, calls, response


def test_session_begins_before_the_provider_wait_and_passes_only_the_count_callback() -> None:
    state = StreamProgressState("s", pid=1)
    sm, _, calls, _ = _make_session(state, deltas=("hi",), probe=state.snapshot)
    sm.send("hello")
    seen = calls[0]["probe"]
    assert (calls[0]["on_output_chars"], calls[0]["on_chunk"]) == (True, False)
    assert (seen.generation, seen.active, seen.streamed_chars) == (1, True, 0)


def test_session_publishes_every_output_fragment_length_bound_to_generation() -> None:
    # Whatever the provider outputs — text, tool JSON, a signature, a
    # structured payload — its length is added; an empty fragment adds nothing.
    recorder = _RecorderPort()
    sm, _, calls, _ = _make_session(recorder, output=MIXED_OUTPUT)
    sm.send("hello")
    expected = [5, 17, 9, 9, 2]
    assert recorder.calls == [("begin",), *[("add", 1, n) for n in expected], ("end", 1)]
    for bogus in (0, -3, True, "abc", None, 2.5):  # non-positive / non-int: nothing published
        calls[0]["count_fn"](bogus)
    assert len(recorder.calls) == 7
    # The second response carries the generation ``begin`` returned for it.
    sm.send("again")
    assert recorder.calls[7:] == [("begin",), *[("add", 2, n) for n in expected], ("end", 2)]

    probing = _ProbingState()
    sm, _, _, _ = _make_session(probing, output=MIXED_OUTPUT)
    sm.send("hello")
    assert [s.streamed_chars for s in probing.after_delta] == [5, 22, 31, 40, 42]
    assert all(s.active and s.generation == 1 for s in probing.after_delta)


def test_session_clears_in_finally_on_success_and_preserves_response_semantics() -> None:
    state = StreamProgressState("s", pid=1)
    sm, _, _, response = _make_session(state, deltas=("abc", "def"))
    out = sm.send("hello")
    assert out is response
    snap = state.snapshot()
    assert (snap.generation, snap.active, snap.streamed_chars) == (1, False, 0)
    assert sm._text_already_streamed is True
    assert sm._intermediate_text_streamed is False


def test_session_clears_in_finally_on_failure() -> None:
    state = StreamProgressState("s", pid=1)
    recorder = _RecorderPort()
    sm, _, _, _ = _make_session(state, deltas=("abc",), fail=RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        sm.send("hello")
    snap = state.snapshot()
    assert (snap.generation, snap.active, snap.streamed_chars) == (1, False, 0)

    sm, _, _, _ = _make_session(recorder, deltas=("abc",), fail=RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        sm.send("hello")
    assert recorder.calls == [("begin",), ("add", 1, 3), ("end", 1)]


def test_session_abandoned_old_worker_cannot_contaminate_newer_generation() -> None:
    # Regression: the first provider call times out (raises) but its worker
    # thread is still alive and keeps invoking the on_chunk closure it was
    # given. Once the session has begun the next response, those late calls —
    # and the old ``end`` — must not alter the new generation's snapshot.
    state = StreamProgressState("s", pid=1)
    sm, _, calls, _ = _make_session(state, deltas=("ab",), fail=TimeoutError("provider timeout"))
    with pytest.raises(TimeoutError):
        sm.send("first")
    old_count = calls[0]["count_fn"]
    assert (state.snapshot().generation, state.snapshot().active) == (1, False)

    # Late count after the old response was cleared: still cleared.
    old_count(14)
    assert (state.snapshot().active, state.snapshot().streamed_chars) == (False, 0)

    # A new response begins on the same session while the old worker lives on.
    seen: list[StreamProgressSnapshot] = []

    def send_stream(message, on_chunk=None, on_output_chars=None):
        assert on_chunk is None
        on_output_chars(6)
        old_count(10)  # abandoned generation-1 worker emits mid-stream
        seen.append(state.snapshot())
        state.end(1)  # and its late ``end`` lands
        seen.append(state.snapshot())
        on_output_chars(2)
        return MagicMock(text="xxxxxxyy", tool_calls=[], thoughts=[],
                         usage=MagicMock(input_tokens=1, output_tokens=1, thinking_tokens=0,
                                         cached_tokens=0, extra={}))

    sm._chat.send_stream = send_stream
    sm.send("second")
    assert [(s.generation, s.active, s.streamed_chars) for s in seen] == [(2, True, 6), (2, True, 6)]
    final = state.snapshot()
    assert (final.generation, final.active, final.streamed_chars) == (2, False, 0)


def test_session_is_fail_open_when_the_port_raises(caplog) -> None:
    # add/end raise: every op is still attempted, the response is returned,
    # and the session warns exactly once.
    recorder = _RecorderPort(raise_on=frozenset({"add", "end"}))
    sm, _, _, response = _make_session(recorder, deltas=("abc", "de"))
    with caplog.at_level("WARNING"):
        out = sm.send("hello")
    assert out is response
    assert recorder.calls == [("begin",), ("add", 1, 3), ("add", 1, 2), ("end", 1)]
    assert sum("stream_progress_publish_failed" in r.getMessage() for r in caplog.records) == 1

    # begin raises: no generation exists, so nothing further is published for
    # that call (no unbound add/end), the provider is still called with no
    # progress callback, and the response is returned.
    caplog.clear()
    recorder = _RecorderPort(raise_on=frozenset({"begin"}))
    sm, _, calls, response = _make_session(recorder, deltas=("abc",))
    with caplog.at_level("WARNING"):
        assert sm.send("hello") is response
    assert recorder.calls == [("begin",)]
    assert (calls[0]["on_output_chars"], calls[0]["on_chunk"]) == (False, False)
    assert sum("stream_progress_publish_failed" in r.getMessage() for r in caplog.records) == 1


def test_session_treats_non_int_generation_from_begin_as_publish_failure(caplog) -> None:
    class _LegacyPort(_RecorderPort):
        def begin(self):  # type: ignore[override]
            self._record("begin")
            return None

    port = _LegacyPort()
    sm, _, calls, response = _make_session(port, deltas=("abc",))
    with caplog.at_level("WARNING"):
        assert sm.send("hello") is response
    assert port.calls == [("begin",)]
    assert (calls[0]["on_output_chars"], calls[0]["on_chunk"]) == (False, False)
    assert any("stream_progress_publish_failed" in r.getMessage() for r in caplog.records)


def test_session_explicit_streaming_false_uses_send_and_never_touches_port() -> None:
    recorder = _RecorderPort()
    sm, chat, calls, response = _make_session(recorder, streaming=False, deltas=("abc",))
    assert sm.streaming is False
    assert sm.send("hello") is response
    chat.send.assert_called_once()
    assert calls == []
    assert recorder.calls == []


def test_session_without_port_keeps_pre_existing_call_shape() -> None:
    sm, _, calls, _ = _make_session(None, deltas=("abc",))
    assert sm.stream_progress is None
    sm.send("hello")
    assert calls == [{"on_chunk": False, "on_output_chars": False, "probe": None, "count_fn": None}]


# ---------------------------------------------------------------------------
# ChatSession / send_with_timeout_stream boundary
# ---------------------------------------------------------------------------

def test_send_with_timeout_stream_omits_the_progress_callback_a_session_cannot_accept() -> None:
    """A legacy override without ``on_output_chars`` is called once, without it,
    and returns normally (fail-open: no progress, no TypeError, no retry);
    sessions that accept the keyword — named or via ``**kwargs`` — receive it."""
    response = LLMResponse(text="", tool_calls=[], usage=UsageMetadata())
    seen: list[dict] = []

    class _Legacy:
        def send_stream(self, message, on_chunk=None):
            seen.append({"chat": "legacy", "on_chunk": on_chunk})
            return response

    class _Supporting:
        def send_stream(self, message, on_chunk=None, on_output_chars=None):
            seen.append({"chat": "supporting", "on_output_chars": on_output_chars})
            return response

    count_fn = lambda count: None  # noqa: E731
    with ThreadPoolExecutor(max_workers=1) as pool:
        for chat in (_Legacy(), _Supporting(), MagicMock(**{"send_stream.return_value": response})):
            assert send_with_timeout_stream(chat, "m", pool, 5.0, "agent", None, on_output_chars=count_fn) is response
    assert seen == [{"chat": "legacy", "on_chunk": None}, {"chat": "supporting", "on_output_chars": count_fn}]
    chat.send_stream.assert_called_once_with("m", on_output_chars=count_fn)  # ``**kwargs`` accepts it


class _NonStreamingSession(ChatSession):
    """Implements only ``send``; inherits the non-streaming fallback."""

    def __init__(self, response: LLMResponse) -> None:
        self._response, self._interface = response, ChatInterface()

    @property
    def interface(self):
        return self._interface

    def send(self, message):
        return self._response


def test_chat_session_fallback_keeps_legacy_on_chunk_and_reports_the_whole_response_once() -> None:
    response = LLMResponse(text="hi", tool_calls=[ToolCall(name="x", args={"a": 1}, id="id1")],
                           usage=UsageMetadata(), thoughts=["think"])
    chunks, counts = [], []
    assert _NonStreamingSession(response).send_stream("m", on_chunk=chunks.append, on_output_chars=counts.append) is response
    assert chunks == ["hi"]  # legacy on_chunk: visible text, exactly as before
    assert counts == [2 + 5 + 1 + 3 + len('{"a":1}')]  # text, thought, name, id, args — once
    empty = LLMResponse(text="", tool_calls=[], usage=UsageMetadata())
    assert _NonStreamingSession(empty).send_stream("m", on_output_chars=counts.append) is empty
    assert counts[1:] == []  # nothing arrived: never invoked
    assert _NonStreamingSession(response).send_stream("m") is response  # legacy call shape


# ---------------------------------------------------------------------------
# Streaming adapters feed the one seam: whatever the provider outputs counts
# ---------------------------------------------------------------------------

ns = SimpleNamespace


def _responses_cases():
    from tests.test_openai_responses_streaming import (
        Event, _completed, _function_call_delta_only_events, _function_call_done_only_events,
        _function_call_events, _reasoning_events,
    )

    name = len("report_answer")
    return {
        # The live codex-pool case: a tool-call-only generation advances
        # progress by its identity and every argument delta; on_chunk gets nothing.
        "tool_call_deltas": (_function_call_delta_only_events(), [name + len("call_delta_only"), 9, 6], []),
        # Only terminal payloads deliver the arguments: counted once, though two carry them.
        "terminal_args_only": (_function_call_done_only_events(), [name + len("call_spark"), 15], []),
        # Deltas delivered the arguments: the terminal echoes add nothing.
        "terminal_args_after_deltas": (_function_call_events(), [name + len("call_fake123"), 9, 6], []),
        # Reasoning item id and summary deltas count; the done/item echoes add nothing.
        "reasoning_summary": (_reasoning_events(), [len("rs_fake"), len("I should call "), len("the report tool.")], []),
        # Opaque output counts like any other output.
        "encrypted_reasoning": ([
            Event("response.output_item.added", item=ns(type="reasoning", id="rs_enc")),
            Event("response.output_item.done", item=ns(type="reasoning", id="rs_enc", summary=[], content=[],
                                                       encrypted_content="opaque-blob")),
            Event("response.output_item.done", item=ns(type="reasoning", id="rs_enc", summary=[], content=[],
                                                       encrypted_content="opaque-blob")),
        ], [len("rs_enc"), len("opaque-blob")], []),
        # Any other delta form counts; a terminal item repeating streamed raw reasoning adds nothing.
        "other_delta_forms": ([
            Event("response.refusal.delta", delta="no"),
            Event("response.reasoning_text.delta", delta="raw", item_id="rs_raw"),
            Event("response.output_item.done", item=ns(type="reasoning", id="rs_raw", summary=[],
                                                       content=[ns(type="reasoning_text", text="raw")])),
        ], [2, 3], []),
        "visible_text": ([Event("response.output_text.delta", delta="héllo"),
                          Event("response.output_text.delta", delta=" 🙂")], [5, 2], ["héllo", " 🙂"]),
    }, _completed


@pytest.mark.parametrize("session_kind", ["generic_responses", "codex"])
@pytest.mark.parametrize("case", ["tool_call_deltas", "terminal_args_only", "terminal_args_after_deltas",
                                  "reasoning_summary", "encrypted_reasoning", "other_delta_forms", "visible_text"])
def test_responses_adapters_count_all_output_once(session_kind, case) -> None:
    from tests.test_openai_responses_streaming import _create_codex_session, _create_openai_responses_session

    cases, completed = _responses_cases()
    events, expected_counts, expected_chunks = cases[case]
    make = _create_codex_session if session_kind == "codex" else _create_openai_responses_session
    chunks, counts = [], []
    make(events + [completed()]).send_stream("go", on_chunk=chunks.append, on_output_chars=counts.append)
    assert (counts, chunks) == (expected_counts, expected_chunks)


def _sys_interface():
    interface = ChatInterface()
    interface.add_system("sys")
    return interface


def _anthropic_session():
    from lingtai.llm.anthropic.adapter import AnthropicChatSession

    start = lambda **b: ns(type="content_block_start", content_block=ns(**b))  # noqa: E731
    delta = lambda **f: ns(type="content_block_delta", delta=ns(**f))  # noqa: E731
    stop = ns(type="content_block_stop")
    events = [
        start(type="thinking"), delta(type="thinking_delta", thinking="Let me "), delta(type="thinking_delta", thinking="look."),
        delta(type="signature_delta", signature="sigsig"), stop, start(type="redacted_thinking", data="opaque-redacted"), stop,
        start(type="text"), delta(type="text_delta", text="Reading "), stop, start(type="tool_use", id="toolu_1", name="read_file"),
        delta(type="input_json_delta", partial_json='{"path":'), delta(type="input_json_delta", partial_json=' "foo.py"}'), stop,
    ]
    final = ns(usage=None, content=[ns(type="text", text="Reading "),
                                    ns(type="tool_use", id="toolu_1", name="read_file", input={"path": "foo.py"})])
    class _Stream:
        def __iter__(self):
            return iter(events)

        def get_final_message(self):
            return final

    @contextmanager
    def messages_stream(**kwargs):
        yield _Stream()

    return AnthropicChatSession(client=ns(messages=ns(stream=messages_stream)), model="m", system_prompt="sys",
                                interface=_sys_interface(), tools=None, tool_choice=None, extra_kwargs={})


def _chat_completions_session():
    from lingtai.llm.openai.adapter import OpenAIChatSession

    empty = {"content": None, "refusal": None, "reasoning": None, "reasoning_content": None, "tool_calls": None}
    chunk = lambda **d: ns(choices=[ns(delta=ns(**{**empty, **d}))], usage=None)  # noqa: E731
    tc = lambda index, id=None, name=None, arguments=None: ns(index=index, id=id, function=ns(name=name, arguments=arguments))  # noqa: E731
    client = MagicMock()
    client.chat.completions.create.return_value = [
        chunk(reasoning="hmm"), chunk(content="ok "), chunk(refusal="nope"),
        chunk(tool_calls=[tc(0, id="call_1", name="search", arguments='{"q":')]),
        chunk(tool_calls=[tc(0, arguments='"x"}'), tc(1, id="call_2", name="noop")]), chunk(content=""),
    ]
    return OpenAIChatSession(client=client, model="m", interface=_sys_interface(), tools=None, tool_choice=None,
                             extra_kwargs={}, client_kwargs={})


def _gemini_session():
    from lingtai.llm.gemini.adapter import InteractionsChatSession

    events = [
        ns(event_type="interaction.created", interaction=ns(id="int_1")),
        ns(event_type="step.start", step=ns(type="thought", summary=[ns(type="text", text="plan")])),
        ns(event_type="step.delta", delta=ns(type="text", text="Sure")),
        ns(event_type="step.start", step=ns(type="function_call", name="default_api:search", arguments={"q": "x"}, id="fc_1")),
        ns(event_type="interaction.completed", interaction=ns(id="int_1", usage=None)),
    ]
    return InteractionsChatSession(client=ns(interactions=ns(create=lambda **kw: iter(events))), model="m", config_kwargs={})


@pytest.mark.parametrize(
    ("make", "expected_counts", "expected_chunks"),
    [
        # thinking, thinking, signature, redacted data, text, tool id+name, json, json
        (_anthropic_session, [7, 5, 6, 15, 8, 16, 8, 10], ["Reading "]),
        # reasoning, content, refusal, id+name+args, args, id+name (empty content adds nothing)
        (_chat_completions_session, [3, 3, 4, 17, 4, 10], ["ok "]),
        # thought text, text delta, function id+name+args (once)
        (_gemini_session, [4, 4, len("fc_1") + len("default_api:search") + len('{"q":"x"}')], ["Sure"]),
    ],
    ids=["anthropic", "chat_completions", "gemini_interactions"],
)
def test_other_streaming_adapters_count_every_output_fragment_once(make, expected_counts, expected_chunks) -> None:
    chunks, counts = [], []
    result = make().send_stream("go", on_chunk=chunks.append, on_output_chars=counts.append)
    assert (counts, chunks) == (expected_counts, expected_chunks)
    assert result.tool_calls[0].args  # the normalized response is unaffected by counting


# ---------------------------------------------------------------------------
# System-owned streaming policy and explicit opt-out
# ---------------------------------------------------------------------------

def _make_agent(tmp_path, **kwargs):
    workdir = tmp_path / "sp_agent"
    return BaseAgent(
        intrinsics=_TEST_INTRINSICS,
        service=make_mock_service(),
        working_dir=workdir,
        workdir_lease=make_test_lease(),
        agent_presence=make_test_presence_store(),
        lifecycle_clock=make_test_lifecycle_clock(),
        snapshot_port=make_test_snapshot_port(),
        source_revision_port=make_test_source_revision_port(),
        notification_store=notification_store_for(workdir),
        **kwargs,
    )


def test_baseagent_streaming_defaults_off_and_explicit_false_stays_false(tmp_path) -> None:
    params = inspect.signature(BaseAgent.__init__).parameters
    assert params["streaming"].default is False
    assert params["stream_progress_factory"].default is None
    assert _make_agent(tmp_path)._session.streaming is False
    assert _make_agent(tmp_path / "off", streaming=False)._session.streaming is False


def test_agent_wrapper_passes_streaming_default_through(tmp_path) -> None:
    from lingtai import Agent

    captured: dict = {}

    class _Captured(Exception):
        pass

    def fake_init(self, *args, **kwargs):
        captured.update(kwargs)
        raise _Captured

    with patch.object(BaseAgent, "__init__", fake_init):
        with pytest.raises(_Captured):
            Agent(make_mock_service(), working_dir=tmp_path / "sp-wrapper")
    assert "streaming" not in captured  # BaseAgent's default (False) applies
    assert "stream_progress_factory" not in captured  # wrapper composes no endpoint


def _write_init(tmp_path: Path, manifest_overrides: dict | None = None) -> dict:
    manifest = {
        "agent_name": "test-agent",
        "language": "en",
        "llm": {"provider": "anthropic", "model": "test-model", "api_key": "test-key", "base_url": None},
        "capabilities": {},
        "soul": {"delay": 30},
        "stamina": 60,
        "context_limit": None,
        "max_turns": 10,
        "admin": {"karma": True},
    }
    manifest.update(manifest_overrides or {})
    data = {"manifest": manifest, "principle": "", "covenant": "Be helpful.", "pad": "", "lingtai": ""}
    (tmp_path / "init.json").write_text(json.dumps(data), encoding="utf-8")
    from lingtai.cli import load_init

    return load_init(tmp_path)


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_cli_streaming_env_overrides_system_default_and_composes_loopback_factory(
    mock_mail, mock_agent, mock_llm, tmp_path, monkeypatch
) -> None:
    from lingtai.cli import build_agent
    from lingtai.tools.system.settings import STREAMING_ENV

    # Valid env wins over the fixed-false System v2 default.
    monkeypatch.setenv(STREAMING_ENV, "on")
    data = _write_init(tmp_path)
    assert "streaming" not in data["manifest"]
    build_agent(data, tmp_path)
    kwargs = mock_agent.call_args.kwargs
    assert kwargs["streaming"] is True
    assert kwargs["stream_progress_factory"] is loopback_stream_progress_factory


@patch("lingtai.cli.LLMService")
@patch("lingtai.cli.Agent")
@patch("lingtai.cli.PosixFilesystemMailAdapter")
def test_cli_legacy_manifest_streaming_is_ignored(mock_mail, mock_agent, mock_llm, tmp_path, monkeypatch) -> None:
    from lingtai.cli import build_agent
    from lingtai.tools.system.settings import STREAMING_ENV

    monkeypatch.delenv(STREAMING_ENV, raising=False)
    data = _write_init(tmp_path, {"streaming": True})
    build_agent(data, tmp_path)
    assert mock_agent.call_args.kwargs["streaming"] is False
    assert mock_agent.call_args.kwargs["stream_progress_factory"] is loopback_stream_progress_factory


def test_canonical_init_template_does_not_declare_system_owned_streaming() -> None:
    data = parse_jsonc((ROOT / "src/lingtai/init.jsonc").read_text(encoding="utf-8"))
    assert "streaming" not in data["manifest"]


# ---------------------------------------------------------------------------
# BaseAgent factory injection
# ---------------------------------------------------------------------------

def test_baseagent_calls_factory_once_with_stable_agent_id_and_binds_port(tmp_path) -> None:
    seen: list[str] = []
    port = _RecorderPort()

    def factory(agent_id: str):
        seen.append(agent_id)
        return port

    agent = _make_agent(tmp_path, streaming=True, stream_progress_factory=factory)
    assert seen == [agent.agent_id]
    assert agent.agent_id
    assert agent._stream_progress is port
    assert agent._session.stream_progress is port


def test_baseagent_without_factory_has_no_port(tmp_path) -> None:
    agent = _make_agent(tmp_path)
    assert agent._stream_progress is None
    assert agent._session.stream_progress is None


def test_explicit_streaming_false_never_calls_factory_and_uses_non_stream_send(tmp_path) -> None:
    # BaseAgent: an explicit opt-out composes no publisher at all — the
    # factory is never invoked, so no unused endpoint is ever bound.
    seen: list[str] = []

    def factory(agent_id: str):
        seen.append(agent_id)
        return _RecorderPort()

    agent = _make_agent(tmp_path, streaming=False, stream_progress_factory=factory)
    assert seen == []
    assert agent._stream_progress is None
    assert agent._session.streaming is False
    assert agent._session.stream_progress is None

    # lingtai.Agent wrapper: the explicit False and the factory both pass
    # through to BaseAgent untouched, where the same opt-out applies.
    from lingtai import Agent

    seen_wrapper: list[str] = []
    wrapped = Agent(
        make_mock_service(),
        working_dir=tmp_path / "wrapper-off",
        streaming=False,
        stream_progress_factory=lambda agent_id: seen_wrapper.append(agent_id) or _RecorderPort(),
    )
    assert seen_wrapper == []
    assert wrapped._stream_progress is None
    assert wrapped._session.streaming is False
    # (SessionManager's own non-stream ``send`` path with streaming=False is
    # pinned by test_session_explicit_streaming_false_uses_send_and_never_touches_port.)


def test_baseagent_factory_failure_is_fail_open(tmp_path) -> None:
    def factory(agent_id: str):
        raise OSError("no loopback today")

    agent = _make_agent(tmp_path, streaming=True, stream_progress_factory=factory)
    assert agent._stream_progress is None
    assert agent._session.streaming is True


# ---------------------------------------------------------------------------
# Loopback endpoint
# ---------------------------------------------------------------------------

AGENT_ID = "20260826-120000-abcd"


def _get(port: int, path: str = STREAM_PROGRESS_PATH):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
        return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))


def _probe(agent_id: str, ports: list[int]) -> int | None:
    """The documented reader algorithm: first valid v1 body with matching identity."""
    for port in ports:
        try:
            status, _, body = _get(port)
        except Exception:
            continue
        if status == 200 and body.get("schema") == STREAM_PROGRESS_SCHEMA and body.get("agent_id") == agent_id:
            return port
    return None


@pytest.fixture
def publisher():
    pub = LoopbackStreamProgressPublisher(AGENT_ID)
    assert pub.start() is True
    try:
        yield pub
    finally:
        pub.close()


def test_endpoint_is_loopback_only_on_a_discovery_candidate_with_schema_identity_no_store(publisher) -> None:
    assert publisher.port in candidate_ports(AGENT_ID)
    assert publisher._server.server_address[0] == "127.0.0.1"
    status, headers, body = _get(publisher.port)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"].startswith("application/json")
    assert set(body) == SNAPSHOT_FIELDS
    assert body["schema"] == STREAM_PROGRESS_SCHEMA
    assert body["agent_id"] == AGENT_ID
    assert body["pid"] == os.getpid()
    assert body["active"] is False and body["streamed_chars"] == 0


def test_endpoint_reflects_live_transitions(publisher) -> None:
    gen = publisher.begin()
    assert gen == 1
    publisher.add_chars(gen, len("héllo wörld"))
    _, _, body = _get(publisher.port)
    assert (body["generation"], body["active"], body["streamed_chars"]) == (1, True, 11)
    publisher.add_chars(gen + 1, 99)  # wrong generation: ignored by the adapter too
    publisher.end(gen + 1)
    _, _, body = _get(publisher.port)
    assert (body["generation"], body["active"], body["streamed_chars"]) == (1, True, 11)
    publisher.end(gen)
    _, _, body = _get(publisher.port)
    assert (body["generation"], body["active"], body["streamed_chars"]) == (1, False, 0)


def test_endpoint_other_paths_404_and_non_get_405(publisher) -> None:
    with pytest.raises(urllib.error.HTTPError) as not_found:
        _get(publisher.port, "/v1/other")
    assert not_found.value.code == 404
    assert not_found.value.headers["Cache-Control"] == "no-store"

    req = urllib.request.Request(
        f"http://127.0.0.1:{publisher.port}{STREAM_PROGRESS_PATH}", data=b"{}", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as not_allowed:
        urllib.request.urlopen(req, timeout=2)
    assert not_allowed.value.code == 405
    assert not_allowed.value.headers["Allow"] == "GET"


def test_second_publisher_binds_next_free_candidate_and_reader_rejects_foreign_identity(publisher) -> None:
    candidates = candidate_ports(AGENT_ID)
    # A foreign agent squatting on this agent's candidate list must be skipped
    # by the documented reader algorithm, and a second publisher for the same
    # id must move to the next free candidate rather than share a port.
    foreign = LoopbackStreamProgressPublisher("someone-else", candidates=candidates)
    same = LoopbackStreamProgressPublisher(AGENT_ID)
    try:
        assert foreign.start() is True
        assert same.start() is True
        ports = {publisher.port, foreign.port, same.port}
        assert len(ports) == 3
        assert ports <= set(candidates)
        assert candidates.index(foreign.port) > candidates.index(publisher.port)
        assert candidates.index(same.port) > candidates.index(foreign.port)
        assert _probe(AGENT_ID, candidates) == publisher.port
        assert _probe("someone-else", candidates) == foreign.port
        # Reattach after the first publisher goes away: the reader rescans and
        # lands on the next publisher for the same identity.
        publisher.close()
        assert _probe(AGENT_ID, candidates) == same.port
    finally:
        foreign.close()
        same.close()


def test_bind_failure_is_fail_open_and_factory_still_returns_a_port() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    occupied = blocker.getsockname()[1]
    try:
        pub = LoopbackStreamProgressPublisher("blocked", candidates=[occupied])
        assert pub.start() is False
        assert pub.port is None
        gen = pub.begin()
        pub.add_chars(gen, 4)
        assert pub.state.snapshot().streamed_chars == 4
        pub.end(gen)
        pub.close()

        with patch("lingtai.adapters.stream_progress.candidate_ports", return_value=[occupied]):
            port = loopback_stream_progress_factory("blocked")
        assert isinstance(port, LoopbackStreamProgressPublisher)
        assert port.port is None
        port.end(port.begin())
    finally:
        blocker.close()


def test_factory_starts_a_publisher_on_a_candidate_and_close_is_idempotent() -> None:
    port = loopback_stream_progress_factory("factory-agent")
    try:
        assert isinstance(port, LoopbackStreamProgressPublisher)
        assert port.port in candidate_ports("factory-agent")
        assert _probe("factory-agent", candidate_ports("factory-agent")) == port.port
    finally:
        port.close()
        port.close()
    assert port.port is None
