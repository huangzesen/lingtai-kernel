"""Provider-agnostic types and session ABC for the LLM protocol layer.

All agent code should depend on these types, never on provider-specific SDKs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lingtai.kernel.config import tool_prose_section_enabled
from lingtai.kernel.logging import get_logger

from .interface import ChatInterface
from .reasoning_effort import (
    UNAVAILABLE_CAPABILITY,
    ReasoningEffortCapability,
    ReasoningEffortSnapshot,
)

logger = get_logger()


_PARTIAL_STREAM_MARKER = "_lingtai_partial_stream"
_NO_AED_RETRY_MARKER = "_lingtai_no_aed_retry"


def safe_exception_description(exc: BaseException) -> str:
    """Render a third-party exception without letting its hooks escape."""
    for render in (str, repr):
        try:
            text = render(exc)
        except BaseException:
            continue
        if text:
            return text
    try:
        name = type(exc).__name__
    except BaseException:
        name = "Exception"
    return f"<unrenderable {name}>"


class LLMReplayTerminalError(Exception):
    """Trusted wrapper when a provider exception cannot carry replay metadata.

    Provider exception attribute hooks are untrusted.  This exact kernel-owned
    type stores only constant booleans and the original exception; its message
    must be supplied by the adapter without rendering ``original``.
    """

    def __init__(
        self,
        original: Exception,
        *,
        partial_stream: bool,
        no_aed_retry: bool,
        message: str,
    ):
        self.original = original
        self._lingtai_partial_stream = partial_stream is True
        self._lingtai_no_aed_retry = no_aed_retry is True
        super().__init__(message)


def llm_replay_terminal_flags(exc: BaseException) -> tuple[bool, bool]:
    """Read replay-safety flags only from the exact kernel-owned wrapper.

    Provider exception classes control their attribute machinery, including a
    subclass ``__dict__`` descriptor, so no provider-owned storage is trusted.
    Exact type and exact ``True`` checks avoid subclass hooks and coercion.
    """
    if type(exc) is not LLMReplayTerminalError:
        return False, False
    return (
        object.__getattribute__(exc, _PARTIAL_STREAM_MARKER) is True,
        object.__getattribute__(exc, _NO_AED_RETRY_MARKER) is True,
    )


def mark_llm_replay_terminal(
    exc: Exception,
    *,
    partial_stream: bool = False,
    no_aed_retry: bool = False,
    message: str = "Provider failure cannot be replayed safely",
) -> Exception:
    """Return the exact trusted wrapper carrying merged replay metadata.

    Provider-owned storage is never read or mutated.  Re-marking an exact
    kernel wrapper merges flags in place; every other exception is wrapped.
    This makes the representation stable across adapter and BaseAgent reads.
    """
    current_partial, current_no_aed = llm_replay_terminal_flags(exc)
    want_partial = current_partial or partial_stream is True
    want_no_aed = current_no_aed or no_aed_retry is True

    if type(exc) is LLMReplayTerminalError:
        object.__setattr__(exc, _PARTIAL_STREAM_MARKER, want_partial)
        object.__setattr__(exc, _NO_AED_RETRY_MARKER, want_no_aed)
        return exc

    return LLMReplayTerminalError(
        exc,
        partial_stream=want_partial,
        no_aed_retry=want_no_aed,
        message=message,
    )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single function/tool invocation extracted from the LLM response.

    Attributes:
        name: Tool/function name.
        args: Parsed arguments dict.
        id: Provider-assigned call ID (e.g. ``call_xxxxx`` for OpenAI,
            ``toolu_xxxxx`` for Anthropic).  None for Gemini which doesn't
            use explicit tool-call IDs.
    """

    name: str
    args: dict
    id: str | None = None


@dataclass
class UsageMetadata:
    """Normalized token counts plus optional per-call ledger metadata."""

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    # Optional safe, provider-specific metadata to merge into token_ledger.jsonl.
    # Do not place request bodies, API keys, or other secrets here.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Provider-agnostic response from an LLM call.

    Attributes:
        text: Concatenated text output (excludes thinking text).
        tool_calls: Extracted function/tool calls.
        usage: Token usage for this call.
        thoughts: List of thinking/reasoning text blocks (for verbose logging).
        raw: The original provider-specific response object. Use for escape
            hatches (e.g. Gemini grounding metadata, multimodal parts).
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: UsageMetadata = field(default_factory=UsageMetadata)
    thoughts: list[str] = field(default_factory=list)
    raw: Any = None
    # Stable identifier for this kernel-level LLM API round-trip.
    # SessionManager assigns it before logging llm_call/llm_response;
    # BaseAgent/ToolExecutor propagate it to every tool event produced from
    # the same assistant response so UI/replay code can group tool batches.
    api_call_id: str | None = None


def is_all_empty_response(response: LLMResponse) -> bool:
    """Return whether a response has no text, tool calls, or thoughts.

    This is the deliberately pure semantic-empty contract shared by the main
    and daemon recovery loops.  It intentionally follows provider-normalized
    truthiness: whitespace text is a response, and thoughts-only responses are
    not empty because they still carry model output.
    """
    return not response.text and not response.tool_calls and not response.thoughts


# The pointer description for registered ``FunctionSchema`` tools, used ONLY
# while the resident ``## tools`` prose section is opted in — it is the pointer
# that section is the target of. Parameter/property descriptions inside
# ``parameters`` are never touched.
WIRE_TOOL_DESCRIPTION = "See the system prompt for tool usage guidance."


def wire_tool_description(description: str | None) -> str:
    """Return the top-level wire description a provider payload should carry.

    Exactly one copy of a tool's prose reaches the model per turn:

    * ``LINGTAI_TOOL_PROSE_SECTION_ENABLED`` truthy — the resident ``## tools``
      section carries the prose, so the wire carries the
      :data:`WIRE_TOOL_DESCRIPTION` pointer at it. This is the historical
      behavior, restored byte-for-byte.
    * default (unset/falsey) — the ``## tools`` section is not rendered at all
      (``base_agent/tools.py:_refresh_tool_inventory_section``), so the pointer
      would dangle; the wire carries the full ``FunctionSchema.description``
      instead. Nothing is shortened or dropped — the prose simply moves to the
      one surface that survives.

    ``description`` falls back to the pointer when empty so a schema registered
    without prose still gets a non-empty wire description, exactly as before.
    """
    if tool_prose_section_enabled():
        return WIRE_TOOL_DESCRIPTION
    return description or WIRE_TOOL_DESCRIPTION


@dataclass
class FunctionSchema:
    """Wraps a tool/function schema dict for type clarity.

    The ``parameters`` dict is already JSON-schema-shaped and provider-agnostic.

    ``description`` holds the full tool prose. It is rendered into the system
    prompt's ``## tools`` section and stored in canonical ChatInterface tool
    snapshots; provider wire payloads carry ``WIRE_TOOL_DESCRIPTION`` instead.

    ``glossary_package`` is an optional non-wire metadata field naming the
    importable resource package that owns the tool's ``glossary-{lang}.md``
    files.  The ``## tools`` renderer uses it to append a localized terminology
    body; it is never serialized into provider payloads (``to_dict`` excludes
    it alongside ``system_prompt``).
    """

    name: str
    description: str
    parameters: dict
    system_prompt: str = ""
    glossary_package: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    @staticmethod
    def list_to_dicts(schemas: list[FunctionSchema] | None) -> list[dict] | None:
        """Convert a list of FunctionSchema to dicts, or None if empty/None."""
        if not schemas:
            return None
        return [s.to_dict() for s in schemas]

    @classmethod
    def from_dicts(cls, dicts: list[dict] | None) -> list["FunctionSchema"] | None:
        """Convert tool dicts (as stored in ChatInterface) back to FunctionSchema objects."""
        if not dicts:
            return None
        return [
            cls(
                name=d["name"],
                description=d.get("description", ""),
                parameters=d.get("parameters", {}),
            )
            for d in dicts
        ]


# ---------------------------------------------------------------------------
# ChatSession ABC
# ---------------------------------------------------------------------------


class ChatSession(ABC):
    """Abstract multi-turn chat session."""

    # lingtai-assigned session ID, set by LLMService
    session_id: str = ""
    # Session metadata for get_state()
    _agent_type: str = ""
    _tracked: bool = True

    # Optional pre-request hook fired after the message is committed to the
    # canonical ChatInterface but before the API call is made. The kernel
    # installs ``_drain_tc_inbox`` here so involuntary tool-call pairs
    # (mail notifications, soul.flow voices) splice into the wire chat
    # mid-turn — between tool rounds within a single _handle_request —
    # rather than waiting for the outer turn to finish.
    #
    # Wire-state contract: at the moment the hook fires, the interface
    # tail must be ``user[tool_results]`` or ``user[text]`` — i.e.
    # ``has_pending_tool_calls()`` must return False, so the splicer can
    # safely append a new ``(call, result)`` pair without violating the
    # provider's strict pair-validation invariant.
    #
    # Sessions that don't use the canonical ChatInterface for wire
    # serialization (OpenAIResponsesSession, GeminiChatSession via
    # genai SDK) still call the hook for the agent-side drain, but the
    # spliced pair is only visible to the LLM on the *next* turn (when
    # the agent re-syncs from interface). For canonical-interface
    # adapters (anthropic, openai-CC, codex-Responses, deepseek), the
    # spliced pair is visible in the same API call as the triggering
    # tool_results.
    #
    # Default ``None`` — adapters that don't install a hook treat the
    # call as a no-op, preserving the legacy zero-hook behavior.
    pre_request_hook: "Callable[[ChatInterface], None] | None" = None

    # -- Runtime reasoning-effort port ------------------------------------
    # Separate from pre_request_hook: providers may need both seams at once.
    def reasoning_effort_capability(self) -> ReasoningEffortCapability:
        """Describe the active route's exact live-effort capability."""
        return UNAVAILABLE_CAPABILITY

    def set_reasoning_effort_policy(
        self,
        provider: "Callable[[], ReasoningEffortSnapshot | None] | None",
    ) -> bool:
        """Install a per-dispatch immutable policy provider, if supported."""
        return False

    def last_reasoning_effort_dispatch(self) -> dict | None:
        """Return safe evidence for the latest effort dispatch, if supported."""
        return None

    def adapter_comment(self):
        """Optional legacy combined adapter note for ``_meta.agent_meta``.

        New adapters should prefer the explicit partitioned methods below:
        ``static_adapter_comment`` for resident, rule-like system-prompt text
        and ``dynamic_adapter_comment`` for per-turn tail state.  This legacy
        method remains for compatibility with adapters/tests that still expose
        one combined note.
        """
        return None

    def static_adapter_comment(self):
        """Optional adapter-authored static note for resident ``meta_guidance``.

        Return only durable/rule-like system-prompt content here.  Dynamic
        counters, ledgers, run state, and per-turn measurements belong in
        ``dynamic_adapter_comment`` so the kernel does not have to guess which
        adapter keys are static.
        """
        return None

    def dynamic_adapter_comment(self):
        """Optional adapter-authored dynamic note for tail ``_meta.agent_meta``.

        Return only per-turn/runtime state here.  The kernel may still perform
        generic size trimming (for example, dropping verbose ledger rows), but
        it should not need adapter-specific static-key blocklists.
        """
        return None
    def on_history_summarized(self, summarized_ids: list[str]) -> None:
        """Hook called after `context(action='summarize')` mutates chat history."""

        return None

    def request_history_rebuild(self, reason: str = "summarize_rebuild_only") -> bool:
        """Request a provider-context rebuild without mutating chat history.

        Used by ``context(action='rebuild')`` (the ``reason``
        default remains the internal ``summarize_rebuild_only`` epoch-reset label).
        Adapters with continuation/cache state can start a fresh full replay on the
        next model request and return True; adapters that always rebuild or have no
        such state may leave the default False.
        """

        return False

    def take_pending_reconstruction_event(self) -> dict | None:
        """Pop the one-shot delayed-summarize reconstruction event, if any.

        Adapters that perform an automatic provider-context rebuild when
        summarized history is pending and context crosses the reconstruction
        threshold (codex's ``_reset_ws_epoch("summarize_delayed")``) record a
        compact before-context (A) event here. The kernel consumes it exactly
        once and attaches it to the next visible tool result's
        ``_meta.tool_meta`` (permanent evidence). Default: no reconstruction
        machinery, so no event. One-shot semantics: returns the event and clears
        it, so a second call returns ``None``.
        """
        return None

    def context_overflow_status(self) -> dict | None:
        """Return the persistent hard-boundary overflow status, or ``None``.

        A session with an automatic one-shot forced provider-context rebuild
        (codex's ``_reset_ws_epoch("summarize_delayed")``) returns a small status
        dict ``{"usage": <float>}`` when that rebuild has already fired for the
        current continuous provider-usage ``>= 1.0`` episode, its first
        post-rebuild provider response has been observed, and current
        provider-reported usage is still STRICTLY above ``1.0`` — i.e. the forced
        rebuild failed to clear the overflow. The kernel renders the fixed
        human-authored ``Forced Rebuilt Failed`` warning from ``usage`` and keeps
        it on every ``_meta.agent_meta.agent_state.context.molt`` result while active (a pure,
        idempotent read — unlike the one-shot
        :meth:`take_pending_reconstruction_event`). Default: no such machinery, so
        ``None``.
        """
        return None

    def on_notification_dismissed(self, channel: str | None = None) -> None:
        """Hook called after a notification dismiss/cleanup mutates the surface.

        A dismiss rewrites the resident notification meta on prior tool results,
        so — like ``on_history_summarized`` — adapters that reuse remote state
        (e.g. Codex WS) use this to start a fresh ws_full epoch. Default no-op.
        """

        return None

    @property
    @abstractmethod
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""

    @abstractmethod
    def send(self, message) -> LLMResponse:
        """Send a user message or tool results and return the model response.

        ``message`` can be:
        - A string (user text message)
        - A list of ToolResultBlock (canonical tool results)
        """

    def reset_provider_turn_state(self) -> None:
        """Reset transient provider turn state before a new user text turn.

        Most providers have no extra turn-scoped transport state. Adapters that
        do (for example Codex Responses-over-WebSocket turn-state headers) may
        override this hook. The kernel calls it only before string user-text
        messages, not before tool-result continuations.
        """

    def get_history(self) -> list[dict]:
        """Return serializable conversation history (canonical format)."""
        return self.interface.to_dict()

    def get_state(self) -> dict:
        """Return the full session state dict.

        Format: {"session_id": str, "messages": [...], "metadata": {...}}
        """
        return {
            "session_id": self.session_id,
            "messages": self.interface.to_dict(),
            "metadata": {
                "agent_type": self._agent_type,
                "created_at": self.interface.entries[0].timestamp if self.interface.entries else 0.0,
                "tracked": self._tracked,
            },
        }

    def total_usage(self) -> dict:
        """Sum tokens and count API calls across all messages."""
        return self.interface.total_usage()

    def usage_by_model(self) -> dict[str, dict]:
        """Breakdown of usage per model name."""
        return self.interface.usage_by_model()

    def send_stream(
        self,
        message,
        on_chunk: Callable[[str], None] | None = None,
        on_output_chars: Callable[[int], None] | None = None,
    ) -> LLMResponse:
        """Send a message with optional streaming callbacks.

        If the session supports streaming, calls ``on_chunk(text_delta)``
        as **visible text** tokens arrive — never tool-argument JSON or
        reasoning — and always returns the complete ``LLMResponse`` at the
        end.

        ``on_output_chars(count)`` is the count-only output-progress
        callback: whatever the provider outputs, its length is added — it
        receives positive ``int`` counts and never any content.  Streaming
        sessions feed it through ``OutputProgress``; both callbacks are
        optional and independent.

        Default implementation falls back to non-streaming ``send()`` and,
        when asked, reports the whole response's length once after the call
        returns — it never claims temporal streaming.
        """
        response = self.send(message)
        if on_chunk and response.text:
            on_chunk(response.text)
        if on_output_chars is not None:
            from .streaming import response_output_chars

            count = response_output_chars(response)
            if count > 0:
                on_output_chars(count)
        return response

    def commit_tool_results(self, tool_results: list) -> None:
        """Append tool results to history without an API call.

        Used when tool execution is intercepted (e.g., clarification_needed
        terminal tool) but the tool_use/tool_result pairing must be preserved
        in history for subsequent messages.

        Default is a no-op for adapters that don't need it (e.g., server-managed
        history).
        """

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        """Replace the tool schemas for subsequent calls in this session.

        Used by the tool-store pattern: the orchestrator starts with
        meta-tools only and dynamically loads more as the model requests.

        Default: no-op. Override in session types that support it.
        """

    def update_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt for subsequent calls in this session.

        Default: no-op. Override in session types that support it.
        """

    def update_system_prompt_batches(self, batches: list[str]) -> None:
        """Replace the system prompt using mutation-frequency batches.

        ``batches`` is the ordered output of
        ``build_system_prompt_batches``: each element is a contiguous
        chunk whose content tends to change at a different cadence
        (e.g. immovable / rarely-mutated / per-idle). Adapters that
        support per-block prompt caching (Anthropic's ``cache_control``)
        can place cache breakpoints at batch boundaries so only the
        volatile tail pays for re-caching.

        Default: concatenate to a string and delegate to
        ``update_system_prompt`` — providers without per-block caching
        see no behaviour change.
        """
        joined = "\n\n".join(b for b in batches if b)
        self.update_system_prompt(joined)

    def reset(self) -> None:
        """Reset the session's HTTP connection while preserving conversation state.

        Called after persistent API errors (e.g. 3+ consecutive 500s) to get a
        fresh connection.  History, tools, and system prompt are preserved —
        only the underlying HTTP client is recreated.

        Default: no-op.  Override in session types backed by a persistent
        HTTP client (Anthropic, OpenAI).  Gemini sessions with server-side
        state (Interactions API) cannot be meaningfully reset this way.
        """

    @property
    def interaction_id(self) -> str | None:
        """Return the current Interactions API interaction ID, or None.

        Only meaningful for Gemini ``InteractionsChatSession`` which chains
        calls via ``previous_interaction_id``.  Other session types return None.
        """
        return None

    def context_window(self) -> int:
        """Total context window in tokens for this session's model. 0 = unknown."""
        return 0

    # -----------------------------------------------------------------------
    # Context-overflow auto-recovery (shared across all providers)
    # -----------------------------------------------------------------------
    #
    # When the provider rejects a request because the context exceeds its
    # hard token limit, the session trims ~10% of the oldest non-system
    # entries from the canonical ChatInterface and retries — up to
    # ``_OVERFLOW_MAX_ROUNDS`` times.  Each provider only needs to
    # implement ``_is_context_overflow_error()`` to opt in.
    #
    # This lives on ChatSession (not LLMAdapter) because the trim
    # operates on ``self._interface._entries`` and the retry wraps the
    # provider-specific ``send()`` / ``send_stream()`` call.

    _OVERFLOW_MAX_ROUNDS: int = 10
    _OVERFLOW_DROP_FRACTION: float = 0.10

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        """Return True if *exc* is a provider context-length-exceeded error.

        Default returns False (no recovery).  Override in subclasses that
        want overflow auto-recovery.
        """
        return False

    def _trim_context_one_round(self) -> int:
        """Drop ~``_OVERFLOW_DROP_FRACTION`` of non-system entries from the
        **front** of the canonical interface.

        Snaps the cut point forward so we never split an
        ``assistant[ToolCallBlock]`` from its matching
        ``user[ToolResultBlock]`` — the resulting wire payload would be
        invalid for strict providers.

        Returns the number of entries dropped (0 if none could be dropped
        — caller should treat that as terminal).
        """
        from .interface import ToolCallBlock, ToolResultBlock

        entries = self._interface._entries  # canonical list, mutated in place
        if not entries:
            return 0
        # Index of first non-system entry.
        first_conv = 0
        if entries[0].role == "system":
            first_conv = 1
        conv_len = len(entries) - first_conv
        if conv_len <= 1:
            return 0
        drop_n = max(1, int(conv_len * self._OVERFLOW_DROP_FRACTION))
        cut = first_conv + drop_n  # entries[first_conv:cut] get dropped

        # Snap cut forward past any assistant[tool_calls] / user[tool_results]
        # boundary so we never strand a tool_call without its result.
        max_cut = len(entries)
        while cut < max_cut:
            # If the entry just *before* the cut is assistant[tool_calls],
            # advance until we're past its matching user[tool_results].
            if cut == 0:
                break
            prev = entries[cut - 1]
            if prev.role == "assistant" and any(
                isinstance(b, ToolCallBlock) for b in prev.content
            ):
                cut += 1
                continue
            # If the entry at the cut is a user[tool_results-only], advance
            # past it so we don't leave dangling results without their call.
            cur = entries[cut]
            if cur.role == "user" and cur.content and all(
                isinstance(b, ToolResultBlock) for b in cur.content
            ):
                cut += 1
                continue
            break

        if cut >= max_cut:
            # Snap consumed everything — refuse to drop the entire conversation.
            return 0

        dropped = cut - first_conv
        # Mutate in place: keep system + everything from cut onward.
        del entries[first_conv:cut]
        return dropped

    def _inject_overflow_notice(self, total_dropped: int, rounds: int) -> None:
        """Append a single user-role kernel notice after successful recovery.

        We use the user role (universally supported) with an explicit
        ``[kernel]`` prefix — same pattern as our synthesized tool aborts.
        The notice strongly recommends molting since context pressure is
        now demonstrably above the model's hard ceiling.
        """
        from .interface import TextBlock

        notice = (
            f"[kernel] Context exceeded the provider's hard token limit. "
            f"To recover, the kernel dropped {total_dropped} oldest entries "
            f"across {rounds} retry round(s). Detail from earlier turns may "
            f"be lost — re-read recent context before acting on it. "
            f"**Strongly recommend triggering a molt soon** — the conversation "
            f"is past the model's safe limit and further growth will overflow "
            f"again."
        )
        self._interface._append("user", [TextBlock(text=notice)])

    def _inject_overflow_notice_after_error(self, exc: Exception) -> None:
        """Surface the trim on the terminal-failure path, mirroring the notice.

        ``_run_with_overflow_recovery`` attaches ``_overflow_trim_stats`` to
        the re-raised provider exception when retry rounds were exhausted (or
        trimming stalled) after entries had already been deleted. Adapters
        call this from their ``except`` block **after** the ``drop_trailing``
        revert — the revert would otherwise strip the just-injected
        user-role notice. No-op when the exception carries no stats, i.e.
        nothing was trimmed to report.
        """
        stats = getattr(exc, "_overflow_trim_stats", None)
        if not stats:
            return
        total_dropped, rounds = stats
        if total_dropped > 0:
            self._inject_overflow_notice(total_dropped=total_dropped, rounds=rounds)

    @staticmethod
    def _attach_overflow_trim_stats(
        exc: Exception, total_dropped: int, rounds: int,
    ) -> None:
        """Best-effort: record trim stats on a re-raised overflow error.

        Provider exception objects normally accept attribute assignment;
        tolerate anything that does not so recovery behavior is unchanged.
        """
        if total_dropped <= 0:
            return
        try:
            exc._overflow_trim_stats = (total_dropped, rounds)
        except Exception:
            pass

    def _run_with_overflow_recovery(self, do_call):
        """Run an API call with context-overflow auto-recovery.

        ``do_call`` is a zero-arg callable performing one full attempt
        (build kwargs from current interface state + invoke the API). It
        is re-called after each trim so the request reflects the post-trim
        canonical interface.

        Returns ``(result, total_dropped, rounds)``. ``total_dropped`` is 0
        and ``rounds`` is 0 when no recovery was needed. On non-overflow
        errors, re-raises immediately. On terminal failure (cannot trim
        further, or max rounds hit), re-raises the original error.
        """
        total_dropped = 0
        rounds = 0
        while True:
            try:
                result = do_call()
                return result, total_dropped, rounds
            except Exception as exc:
                if not self._is_context_overflow_error(exc):
                    raise
                if rounds >= self._OVERFLOW_MAX_ROUNDS:
                    logger.warning(
                        "[overflow-recovery] giving up after %d rounds "
                        "(dropped %d entries total) — re-raising provider error.",
                        rounds, total_dropped,
                    )
                    # Entries trimmed in earlier rounds are already deleted;
                    # record the loss on the exception so adapters can surface
                    # it after their error-revert step (issue #653).
                    self._attach_overflow_trim_stats(exc, total_dropped, rounds)
                    raise
                dropped = self._trim_context_one_round()
                if dropped == 0:
                    logger.warning(
                        "[overflow-recovery] cannot trim further "
                        "(dropped %d entries across %d rounds) — re-raising.",
                        total_dropped, rounds,
                    )
                    self._attach_overflow_trim_stats(exc, total_dropped, rounds)
                    raise
                total_dropped += dropped
                rounds += 1
                logger.warning(
                    "[overflow-recovery] round %d: dropped %d entries "
                    "(running total %d). Retrying.",
                    rounds, dropped, total_dropped,
                )
                continue



