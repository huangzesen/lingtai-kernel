"""Streaming accumulator for LLM response deltas.

Collects text, tool-call, and thought fragments during streaming and
finalizes them into an LLMResponse.  Provider-agnostic — each adapter
feeds deltas through the accumulator's methods, then calls finalize().

``OutputProgress`` (bottom) is the separate count-only output-progress seam:
adapters hand it every output fragment they receive; it publishes lengths only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .base import LLMResponse, ToolCall, UsageMetadata

logger = logging.getLogger(__name__)


class StreamingAccumulator:
    """Collects streaming deltas and finalizes into an LLMResponse.

    Supports two tool-call styles:

    **Sequential** (Anthropic, OpenAI Responses) — one tool at a time::

        acc.start_tool(id="toolu_1", name="read_file")
        acc.add_tool_args('{"path":')
        acc.add_tool_args(' "foo.py"}')
        acc.finish_tool()

    **Index-keyed** (OpenAI Completions) — concurrent tools by index::

        acc.add_tool_delta(index=0, id="call_1", name="read", args_delta='{"p')
        acc.add_tool_delta(index=1, id="call_2", name="write", args_delta='{"')
        acc.add_tool_delta(index=0, args_delta='ath": "a"}')
        acc.add_tool_delta(index=1, args_delta='path": "b"}')
        acc.finish_all_tools()

    Text and thought deltas are simple appends.
    """

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._thought_parts: list[str] = []
        self._thoughts: list[str] = []
        self._tool_calls: list[ToolCall] = []

        # Sequential tool state (Anthropic / OpenAI Responses)
        self._pending_tool: dict[str, str] | None = None

        # Index-keyed tool state (OpenAI Completions)
        self._pending_tools_by_index: dict[int, dict[str, str]] = {}

    # -- Text ---------------------------------------------------------------

    def add_text(self, delta: str) -> None:
        """Append a text delta."""
        self._text_parts.append(delta)

    # -- Thoughts -----------------------------------------------------------

    def add_thought(self, delta: str) -> None:
        """Append a thinking/reasoning delta to the current thought block."""
        self._thought_parts.append(delta)

    def finish_thought(self) -> None:
        """Close the current thought block (e.g. on content_block_stop)."""
        if self._thought_parts:
            self._thoughts.append("".join(self._thought_parts))
            self._thought_parts = []

    # -- Sequential tool calls (Anthropic, OpenAI Responses) ----------------

    def start_tool(self, *, id: str, name: str) -> None:
        """Begin accumulating a new tool call."""
        self._pending_tool = {"id": id, "name": name, "args_json": ""}

    def add_tool_args(self, delta: str) -> None:
        """Append JSON argument fragment to the current pending tool."""
        if self._pending_tool is not None:
            self._pending_tool["args_json"] += delta

    def set_tool_args_if_empty(self, full: str | None) -> bool:
        """Set complete terminal args only when no fragment was accumulated.

        Responses providers can emit a tool call's complete JSON only on a
        terminal ``*.done`` event.  A non-empty delta buffer remains
        authoritative so terminal values are never appended twice or clobber
        an already assembled call.  Returns ``True`` only when ``full`` was
        adopted (i.e. it is the sole delivery of those arguments).
        """
        if not full:
            return False
        if self._pending_tool is not None and not self._pending_tool["args_json"]:
            self._pending_tool["args_json"] = full
            return True
        return False

    def finish_tool(self) -> None:
        """Finalize the current pending tool call."""
        if self._pending_tool is not None:
            self._tool_calls.append(_finalize_tool(self._pending_tool))
            self._pending_tool = None

    # -- Index-keyed tool calls (OpenAI Completions) ------------------------

    def add_tool_delta(
        self,
        index: int,
        *,
        id: str | None = None,
        name: str | None = None,
        args_delta: str | None = None,
    ) -> None:
        """Feed an index-keyed tool-call delta (OpenAI Completions style)."""
        if index not in self._pending_tools_by_index:
            self._pending_tools_by_index[index] = {
                "id": id or "",
                "name": name or "",
                "args_json": "",
            }
        entry = self._pending_tools_by_index[index]
        if id and not entry["id"]:
            entry["id"] = id
        if name and not entry["name"]:
            entry["name"] = name
        if args_delta:
            entry["args_json"] += args_delta

    def finish_all_tools(self) -> None:
        """Finalize all index-keyed pending tool calls."""
        for idx in sorted(self._pending_tools_by_index):
            self._tool_calls.append(
                _finalize_tool(self._pending_tools_by_index[idx])
            )
        self._pending_tools_by_index.clear()

    # -- Atomic tool call (Gemini Interactions) -----------------------------

    def add_tool(self, tool_call: ToolCall) -> None:
        """Add a fully-formed tool call (no accumulation needed)."""
        self._tool_calls.append(tool_call)

    # -- Finalization -------------------------------------------------------

    @property
    def text(self) -> str:
        """Joined text accumulated so far."""
        return "".join(self._text_parts)

    @property
    def tool_calls(self) -> list[ToolCall]:
        """Tool calls accumulated so far."""
        return self._tool_calls

    @property
    def thoughts(self) -> list[str]:
        """Completed thought blocks. Includes any unfinished block."""
        result = list(self._thoughts)
        if self._thought_parts:
            result.append("".join(self._thought_parts))
        return result

    def finalize(
        self,
        usage: UsageMetadata | None = None,
        raw: Any = None,
    ) -> LLMResponse:
        """Build the final LLMResponse from all accumulated deltas.

        Any pending thought block is automatically closed. Any pending
        sequential tool call is also auto-finalized.
        Index-keyed tools are NOT auto-finalized — call finish_all_tools()
        explicitly before finalize() if using the index-keyed style.
        """
        # Close any open thought block
        self.finish_thought()

        if self._pending_tool is not None:
            logger.warning(
                "stream ended with pending sequential tool call; "
                "auto-finalizing name=%r id=%r args_len=%d",
                self._pending_tool["name"],
                self._pending_tool["id"] or None,
                len(self._pending_tool["args_json"]),
            )
            self.finish_tool()

        # Consolidate thoughts into a single entry if multiple deltas
        thoughts = self._thoughts
        if len(thoughts) > 1:
            thoughts = ["".join(thoughts)]

        return LLMResponse(
            text=self.text,
            tool_calls=self._tool_calls,
            usage=usage or UsageMetadata(),
            thoughts=thoughts,
            raw=raw,
        )


def _finalize_tool(pending: dict[str, str]) -> ToolCall:
    """Parse a pending tool dict into a ToolCall."""
    args_json = pending["args_json"]
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError:
        logger.warning(
            "streamed tool-call args JSON parse failed; "
            "name=%r id=%r args_len=%d; defaulting to args={}",
            pending["name"],
            pending["id"] or None,
            len(args_json),
        )
        args = {}
    return ToolCall(name=pending["name"], args=args, id=pending["id"] or None)


# -- Output progress: the count-only seam -------------------------------------
#
# One rule: provider output arrived, so add its length.  No taxonomy of output
# kinds lives here; only lengths leave this seam and content is never kept.


def output_length(value: Any) -> int:
    """Length of one output fragment as delivered; never raises.

    ``str``/``bytes`` count their own length; a structured fragment (mapping,
    sequence, SDK model) counts its canonical JSON text; ``None``, numbers,
    booleans, and empty containers are not output and count 0.
    """
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        return len(value)
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(exclude_none=True)
        except Exception:
            return 0
    if isinstance(value, (dict, list, tuple)) and value:
        try:
            return len(json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
            ))
        except Exception:
            return 0
    return 0


def output_values(obj: Any, exclude: tuple[str, ...] = ("type",)) -> list[Any]:
    """Public field values of an event/block (object or mapping), minus the
    protocol labels in ``exclude`` — count a fragment shape whole."""
    items = obj.items() if isinstance(obj, dict) else vars(obj).items() if hasattr(obj, "__dict__") else ()
    return [v for k, v in items if not k.startswith("_") and k not in exclude]


def response_output_chars(response: LLMResponse) -> int:
    """Length of a whole normalized ``LLMResponse`` — the non-streaming fallback's one count."""
    fragments: list[Any] = [response.text, *(response.thoughts or [])]
    for tool_call in response.tool_calls or []:
        fragments += [tool_call.name, tool_call.id, tool_call.args]
    return sum(output_length(f) for f in fragments)


class OutputProgress:
    """Count-only output-progress seam shared by every streaming adapter.

    ``add(*fragments)`` publishes their summed length as one positive ``int``
    to ``on_output_chars`` (nothing for 0) and never the fragments.
    ``add_stream(key, fragment)`` also remembers ``key`` once something was
    actually published for it; ``add_final(key, *fragments)`` counts only if
    ``key`` is not yet remembered and then remembers it, so a terminal event
    cannot re-count output delivered as deltas or by an earlier terminal
    event, while an empty delta or empty terminal payload never suppresses a
    later real payload.  No callback: no-op.
    """

    __slots__ = ("_on_output_chars", "_streamed")

    def __init__(self, on_output_chars: Callable[[int], None] | None = None) -> None:
        self._on_output_chars = on_output_chars
        self._streamed: set[Any] = set()

    def add(self, *fragments: Any) -> int:
        if self._on_output_chars is None:
            return 0
        count = sum(output_length(f) for f in fragments)
        if count > 0:
            self._on_output_chars(count)
        return count

    def add_stream(self, key: Any, fragment: Any) -> int:
        return self._remember(key, self.add(fragment))

    def add_final(self, key: Any, *fragments: Any) -> int:
        if key is not None and key in self._streamed:
            return 0
        return self._remember(key, self.add(*fragments))

    def _remember(self, key: Any, count: int) -> int:
        if count > 0 and key is not None:
            self._streamed.add(key)
        return count
