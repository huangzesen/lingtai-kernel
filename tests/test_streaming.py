"""Tests for StreamingAccumulator."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lingtai.kernel.llm.streaming import (
    OutputProgress,
    StreamingAccumulator,
    output_length,
    output_values,
    response_output_chars,
)
from lingtai.kernel.llm.base import LLMResponse, ToolCall, UsageMetadata


# -- Text accumulation ------------------------------------------------------

def test_text_accumulation():
    acc = StreamingAccumulator()
    acc.add_text("Hello")
    acc.add_text(" world")
    result = acc.finalize()
    assert result.text == "Hello world"
    assert result.tool_calls == []
    assert result.thoughts == []


def test_empty_finalize():
    acc = StreamingAccumulator()
    result = acc.finalize()
    assert result.text == ""
    assert result.tool_calls == []
    assert result.thoughts == []


# -- Sequential tool calls (Anthropic / OpenAI Responses) -------------------

def test_sequential_tool_single():
    acc = StreamingAccumulator()
    acc.start_tool(id="toolu_1", name="read_file")
    acc.add_tool_args('{"path":')
    acc.add_tool_args(' "foo.py"}')
    acc.finish_tool()
    result = acc.finalize()
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.args == {"path": "foo.py"}
    assert tc.id == "toolu_1"


def test_sequential_tool_multiple():
    acc = StreamingAccumulator()
    acc.start_tool(id="t1", name="read")
    acc.add_tool_args('{"a": 1}')
    acc.finish_tool()
    acc.start_tool(id="t2", name="write")
    acc.add_tool_args('{"b": 2}')
    acc.finish_tool()
    result = acc.finalize()
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].name == "read"
    assert result.tool_calls[1].name == "write"


def test_sequential_tool_empty_args():
    acc = StreamingAccumulator()
    acc.start_tool(id="t1", name="noop")
    acc.finish_tool()
    result = acc.finalize()
    assert result.tool_calls[0].args == {}


def test_finalize_auto_closes_pending_sequential_tool(caplog):
    acc = StreamingAccumulator()
    args_json = '{"path": "foo.py"}'
    acc.start_tool(id="toolu_1", name="read_file")
    acc.add_tool_args(args_json)

    with caplog.at_level("WARNING", logger="lingtai.kernel.llm.streaming"):
        result = acc.finalize()

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.args == {"path": "foo.py"}
    assert tc.id == "toolu_1"
    assert "pending sequential tool call" in caplog.text
    assert "auto-finalizing" in caplog.text
    assert "read_file" in caplog.text
    assert "toolu_1" in caplog.text
    assert f"args_len={len(args_json)}" in caplog.text


def test_finalize_auto_closes_pending_sequential_tool_with_malformed_json(caplog):
    acc = StreamingAccumulator()
    args_json = '{"path":'
    acc.start_tool(id="toolu_bad", name="read_file")
    acc.add_tool_args(args_json)

    with caplog.at_level("WARNING", logger="lingtai.kernel.llm.streaming"):
        result = acc.finalize()

    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.id == "toolu_bad"
    assert tc.args == {}
    assert "pending sequential tool call" in caplog.text
    assert "auto-finalizing" in caplog.text
    assert "streamed tool-call args JSON parse failed" in caplog.text
    assert "defaulting to args={}" in caplog.text
    assert "read_file" in caplog.text
    assert "toolu_bad" in caplog.text
    assert f"args_len={len(args_json)}" in caplog.text


def test_finalize_after_finished_sequential_tool_emits_no_pending_warning(caplog):
    acc = StreamingAccumulator()
    acc.start_tool(id="toolu_1", name="read_file")
    acc.add_tool_args('{"path": "foo.py"}')
    acc.finish_tool()

    with caplog.at_level("WARNING", logger="lingtai.kernel.llm.streaming"):
        result = acc.finalize()

    assert len(result.tool_calls) == 1
    assert "pending sequential tool call" not in caplog.text


def test_finalize_preserves_finished_then_pending_tool_order_and_is_idempotent(caplog):
    acc = StreamingAccumulator()
    acc.start_tool(id="toolu_1", name="read_file")
    acc.add_tool_args('{"path": "foo.py"}')
    acc.finish_tool()
    acc.start_tool(id="toolu_2", name="write_file")
    acc.add_tool_args('{"path": "bar.py"}')

    with caplog.at_level("WARNING", logger="lingtai.kernel.llm.streaming"):
        first_result = acc.finalize()

    assert [tc.id for tc in first_result.tool_calls] == ["toolu_1", "toolu_2"]
    assert [tc.name for tc in first_result.tool_calls] == ["read_file", "write_file"]
    assert "pending sequential tool call" in caplog.text
    assert "toolu_2" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger="lingtai.kernel.llm.streaming"):
        second_result = acc.finalize()

    assert [tc.id for tc in second_result.tool_calls] == ["toolu_1", "toolu_2"]
    assert "pending sequential tool call" not in caplog.text


def test_sequential_tool_malformed_json(caplog):
    acc = StreamingAccumulator()
    args_json = "{not valid json"
    acc.start_tool(id="t1", name="broken")
    acc.add_tool_args(args_json)
    with caplog.at_level("WARNING", logger="lingtai.kernel.llm.streaming"):
        acc.finish_tool()
    result = acc.finalize()
    assert result.tool_calls[0].args == {}
    assert "streamed tool-call args JSON parse failed" in caplog.text
    assert "defaulting to args={}" in caplog.text
    assert "broken" in caplog.text
    assert "t1" in caplog.text
    assert f"args_len={len(args_json)}" in caplog.text


def test_finish_tool_noop_when_no_pending():
    """finish_tool() is safe to call when there's no pending tool."""
    acc = StreamingAccumulator()
    acc.finish_tool()  # should not raise
    result = acc.finalize()
    assert result.tool_calls == []


# -- Done-only / terminal fallback tool args -------------------------------


def test_set_tool_args_if_empty_reconstructs_done_only_arguments():
    """A complete terminal value fills a pending tool with no deltas."""
    acc = StreamingAccumulator()
    acc.start_tool(id="call_done", name="echo_value")
    acc.set_tool_args_if_empty('{"value":"done"}')
    acc.finish_tool()

    result = acc.finalize()
    assert result.tool_calls[0].args == {"value": "done"}
    assert result.tool_calls[0].id == "call_done"


def test_set_tool_args_if_empty_is_first_non_empty_wins():
    """A terminal fallback never appends to or overwrites accumulated deltas."""
    acc = StreamingAccumulator()
    acc.start_tool(id="call_first", name="echo_value")
    acc.add_tool_args('{"value":"delta"}')
    acc.set_tool_args_if_empty('{"value":"done"}')
    acc.set_tool_args_if_empty("{malformed")
    acc.finish_tool()

    result = acc.finalize()
    assert result.tool_calls[0].args == {"value": "delta"}


def test_set_tool_args_if_empty_is_idempotent_across_terminal_sources():
    """Args-done followed by item-done does not duplicate the complete JSON."""
    acc = StreamingAccumulator()
    acc.start_tool(id="call_terminal", name="echo_value")
    acc.set_tool_args_if_empty('{"value":"done"}')
    acc.set_tool_args_if_empty('{"value":"done"}')
    acc.finish_tool()

    result = acc.finalize()
    assert result.tool_calls[0].args == {"value": "done"}


def test_set_tool_args_if_empty_ignores_empty_or_missing_terminal_values():
    """Empty terminal values cannot clobber a real delta buffer."""
    acc = StreamingAccumulator()
    acc.start_tool(id="call_empty", name="echo_value")
    acc.add_tool_args('{"value":"delta"}')
    acc.set_tool_args_if_empty("")
    acc.set_tool_args_if_empty(None)
    acc.finish_tool()

    result = acc.finalize()
    assert result.tool_calls[0].args == {"value": "delta"}


def test_done_only_sequential_tools_preserve_order_and_ids():
    """Multiple terminal-only tools retain order, IDs, names, and arguments."""
    acc = StreamingAccumulator()
    for call_id, name, args in [
        ("call_1", "first", '{"order":1}'),
        ("call_2", "second", '{"order":2}'),
    ]:
        acc.start_tool(id=call_id, name=name)
        acc.set_tool_args_if_empty(args)
        acc.finish_tool()

    result = acc.finalize()
    assert [(tool.id, tool.name, tool.args) for tool in result.tool_calls] == [
        ("call_1", "first", {"order": 1}),
        ("call_2", "second", {"order": 2}),
    ]


# -- Index-keyed tool calls (OpenAI Completions) ----------------------------

def test_index_keyed_single():
    acc = StreamingAccumulator()
    acc.add_tool_delta(0, id="call_1", name="search", args_delta='{"q":')
    acc.add_tool_delta(0, args_delta=' "hello"}')
    acc.finish_all_tools()
    result = acc.finalize()
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search"
    assert result.tool_calls[0].args == {"q": "hello"}
    assert result.tool_calls[0].id == "call_1"


def test_index_keyed_concurrent():
    acc = StreamingAccumulator()
    acc.add_tool_delta(0, id="c1", name="read", args_delta='{"p')
    acc.add_tool_delta(1, id="c2", name="write", args_delta='{"')
    acc.add_tool_delta(0, args_delta='ath": "a.py"}')
    acc.add_tool_delta(1, args_delta='path": "b.py"}')
    acc.finish_all_tools()
    result = acc.finalize()
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].id == "c1"
    assert result.tool_calls[0].args == {"path": "a.py"}
    assert result.tool_calls[1].id == "c2"
    assert result.tool_calls[1].args == {"path": "b.py"}


def test_index_keyed_late_id():
    """ID may arrive in a later delta than the first one."""
    acc = StreamingAccumulator()
    acc.add_tool_delta(0, name="foo", args_delta='{"x": 1}')
    acc.add_tool_delta(0, id="late_id")
    acc.finish_all_tools()
    result = acc.finalize()
    assert result.tool_calls[0].id == "late_id"


def test_finalize_does_not_auto_close_index_keyed_tools():
    acc = StreamingAccumulator()
    acc.add_tool_delta(0, id="call_1", name="search", args_delta='{"q": "hello"}')

    result = acc.finalize()

    assert result.tool_calls == []


# -- Atomic tool calls (Gemini Interactions) --------------------------------

def test_atomic_tool():
    acc = StreamingAccumulator()
    acc.add_tool(ToolCall(name="search", args={"q": "test"}, id="g1"))
    result = acc.finalize()
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search"


# -- Thoughts ---------------------------------------------------------------

def test_thought_single_block():
    acc = StreamingAccumulator()
    acc.add_thought("Let me ")
    acc.add_thought("think...")
    acc.finish_thought()
    result = acc.finalize()
    assert result.thoughts == ["Let me think..."]


def test_thought_multiple_blocks():
    """Multiple thought blocks get consolidated into one entry."""
    acc = StreamingAccumulator()
    acc.add_thought("First thought.")
    acc.finish_thought()
    acc.add_thought("Second thought.")
    acc.finish_thought()
    result = acc.finalize()
    assert result.thoughts == ["First thought.Second thought."]


def test_thought_auto_closed_on_finalize():
    """Unfinished thought block is closed by finalize()."""
    acc = StreamingAccumulator()
    acc.add_thought("unfinished")
    result = acc.finalize()
    assert result.thoughts == ["unfinished"]


def test_finish_thought_noop_when_empty():
    """finish_thought() is safe when no thought deltas accumulated."""
    acc = StreamingAccumulator()
    acc.finish_thought()  # should not raise
    result = acc.finalize()
    assert result.thoughts == []


# -- Mixed content ----------------------------------------------------------

def test_text_tools_and_thoughts():
    acc = StreamingAccumulator()
    acc.add_thought("thinking...")
    acc.finish_thought()
    acc.add_text("Here is ")
    acc.start_tool(id="t1", name="search")
    acc.add_tool_args('{"q": "test"}')
    acc.finish_tool()
    acc.add_text("the answer")
    result = acc.finalize()
    assert result.text == "Here is the answer"
    assert result.thoughts == ["thinking..."]
    assert len(result.tool_calls) == 1


# -- Usage ------------------------------------------------------------------

def test_usage_passthrough():
    acc = StreamingAccumulator()
    acc.add_text("hi")
    usage = UsageMetadata(input_tokens=100, output_tokens=50, thinking_tokens=10, cached_tokens=20)
    result = acc.finalize(usage=usage)
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.usage.thinking_tokens == 10
    assert result.usage.cached_tokens == 20


def test_default_usage():
    acc = StreamingAccumulator()
    result = acc.finalize()
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0


# -- Properties -------------------------------------------------------------

def test_text_property_during_accumulation():
    acc = StreamingAccumulator()
    acc.add_text("a")
    acc.add_text("b")
    assert acc.text == "ab"


def test_tool_calls_property_during_accumulation():
    acc = StreamingAccumulator()
    acc.start_tool(id="t1", name="foo")
    acc.add_tool_args("{}")
    acc.finish_tool()
    assert len(acc.tool_calls) == 1


def test_thoughts_property_includes_unfinished():
    acc = StreamingAccumulator()
    acc.add_thought("done")
    acc.finish_thought()
    acc.add_thought("in progress")
    assert acc.thoughts == ["done", "in progress"]


# -- Terminal-args adoption signal ------------------------------------------

def test_set_tool_args_if_empty_reports_whether_the_terminal_value_was_adopted():
    acc = StreamingAccumulator()
    acc.start_tool(id="c1", name="echo")
    assert acc.set_tool_args_if_empty("") is False
    assert acc.set_tool_args_if_empty('{"v":1}') is True  # sole delivery
    assert acc.set_tool_args_if_empty('{"v":1}') is False  # echo of the adopted value
    acc.start_tool(id="c2", name="echo")
    acc.add_tool_args('{"v":')
    assert acc.set_tool_args_if_empty('{"v":2}') is False  # deltas already delivered it


# -- OutputProgress: the count-only seam ------------------------------------
#
# One rule: provider output arrived, add the length of what was delivered.

class _Model:
    def model_dump(self, exclude_none=True):
        return {"k": "vv"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("héllo 🙂 器灵", 10), (b"\x00\x01\x02", 3), ("", 0), (None, 0), (7, 0), (True, 0),
     ({"b": 1, "a": "é"}, len('{"a":"é","b":1}')), ({}, 0), ([], 0), (_Model(), len('{"k":"vv"}')),
     ({1: "x", "a": 2}, 0)],  # unsortable keys: never raises
)
def test_output_length_is_the_length_of_the_delivered_representation(value, expected):
    assert output_length(value) == expected


def test_output_progress_publishes_positive_summed_lengths_and_counts_terminal_echoes_once():
    published: list[int] = []
    progress = OutputProgress(published.append)
    assert progress.add("ab", None, {"k": "v"}, b"xyz") == 2 + 9 + 3
    assert progress.add("", None) == 0  # nothing arrived: nothing published
    progress.add_stream(("delta", "item_1"), "streamed")
    assert progress.add_final(("delta", "item_1"), "echo of streamed") == 0
    assert progress.add_final(("delta", "item_2"), "sole delivery") == 13
    assert published == [14, 8, 13]
    assert OutputProgress(None).add("never published") == 0
    assert output_values(SimpleNamespace(type="x", text="ab", _private="no", n=3)) == ["ab", 3]
    assert output_values({"type": "t", "signature": "sig"}) == ["sig"] and output_values(None) == []
    response = LLMResponse(text="hi", tool_calls=[ToolCall(name="x", args={"a": 1}, id="id1")],
                           usage=UsageMetadata(), thoughts=["think"])
    assert response_output_chars(response) == 2 + 5 + 1 + 3 + len('{"a":1}')


@pytest.mark.parametrize(
    ("streamed", "finals", "expected"),
    [(None, ["payload"], [7]),  # None delta, then the terminal payload: counted once
     ("", ["payload"], [7]),  # empty delta never suppresses the terminal payload
     (None, ["payload", "payload"], [7]),  # terminal-only payload repeated: once
     (None, ["", "payload"], [7]),  # empty terminal never poisons the real one
     ("delta", ["payload"], [5])],  # a real delta suppresses the terminal echo
)
def test_terminal_dedupe_remembers_a_key_only_after_a_positive_count(streamed, finals, expected):
    published: list[int] = []
    progress = OutputProgress(published.append)
    progress.add_stream("k", streamed)
    for final in finals:
        progress.add_final("k", final)
    assert published == expected
