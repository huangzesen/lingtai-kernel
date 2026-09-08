"""Mimo (Xiaomi) adapter — defaults to the OpenAI Responses wire with
stateless full-history replay, and preserves an explicit Chat Completions
escape hatch that satisfies MiMo thinking-mode's reasoning_content
round-trip contract (analogous to DeepSeek).

Live wire evidence (2026-07-14): MiMo's official Responses API
(``POST https://api.xiaomimimo.com/v1/responses``, docs at
``https://mimo.mi.com/static/docs/api/chat/responses.md``) accepts a
two-turn function_call + exact prior output item + function_call_output
replay, preserving a unique tool marker end-to-end. MiMo is stateless there:
the docs say only documented parameters are processed, explicitly mark
``previous_response_id`` and ``context_management`` as incompatible, support
``function_call_output``, and require callers to manage context manually
(retaining prior reasoning items themselves). ``store`` and ``conversation``
are likewise unsupported. So the default MiMo Responses session never sends
``store``/``previous_response_id``/``conversation``/generic
``context_management`` — every turn replays the full raw canonical
interface (or, once standalone compaction has fired, the opaque compacted
prefix plus a strict-additive delta).

MiMo's standalone ``POST /v1/responses/compact`` endpoint currently returns a
provider error on the live API. Unlike Codex (which treats a standalone
compact failure as non-fatal and simply skips compaction for that turn),
MiMo's failure policy is a HARD failure: a standalone-compaction invocation,
parse, or provider failure on the MiMo Responses wire always propagates —
never silently swallowed, never falls back to continuing the original full
history, and never silently drops to Chat Completions. See
``MimoResponsesSession._compact_now``.

Once thinking mode has been invoked by an assistant ``tool_calls`` turn on
the CHAT COMPLETIONS wire, MiMo (like DeepSeek) requires every subsequent
assistant turn — tool-call AND plain-text — to carry ``reasoning_content`` on
replay. Assistant turns BEFORE the first tool_call must NOT carry it. This
only applies to the explicit ``wire_api="chat_completions"`` escape hatch
(``MimoChatSession``) — the default Responses wire replays reasoning as
native ``reasoning`` items instead (see ``to_responses_input``).

Earlier, this adapter stripped ``reasoning_content`` from every replayed
assistant turn as a workaround for a model loop: when the *same* thinking
block was echoed back unchanged on every turn, MiMo treated it as
authoritative and parroted it verbatim, eventually tripping the 120s LLM
hang watchdog. Real per-turn reasoning from ``ThinkingBlock``s is
byte-different by construction (and so is the per-turn-unique fallback
below), which avoids that pathology while satisfying the protocol.

Real reasoning is preserved end-to-end now: ``OpenAIChatSession`` captures
``reasoning_content`` into a ``ThinkingBlock`` on each assistant turn, and
``interface_converters.to_openai`` emits the block back as
``reasoning_content`` on replay. This adapter only injects a per-turn-unique
fallback for rehydrated/historical assistant turns that have no captured
``ThinkingBlock`` (e.g. ``chat_history.jsonl`` entries written before this
fix, or turns where the provider returned no reasoning text).
"""
from __future__ import annotations

from typing import Any

from lingtai.kernel.llm.base import FunctionSchema, LLMResponse
from lingtai.kernel.llm.interface import ChatInterface
from ..interface_converters import to_responses_input
from ..openai.adapter import (
    OpenAIAdapter,
    OpenAIChatSession,
    OpenAIResponsesSession,
    _StandaloneCompactionMixin,
    _validate_codex_compact_token_limit,
)


def _fallback_reasoning_for(msg: dict, turn_idx: int) -> str:
    """Build a per-turn-unique reasoning stub for an assistant message.

    The string must be byte-different per turn — a constant placeholder
    re-introduces the original loop pathology (model echoes the same
    thinking block back). Inlining tool names, call ids, and a content
    snippet keeps the stub naturally unique per turn.
    """
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        names = ",".join(
            (tc.get("function", {}) or {}).get("name", "") for tc in tool_calls
        )
        ids = ",".join(tc.get("id", "") for tc in tool_calls)
        return f"call {names} [{ids}] (turn {turn_idx})"
    content = msg.get("content") or ""
    snippet = content[:64].replace("\n", " ")
    return f"reply [{snippet}] (turn {turn_idx})"


class MimoChatSession(OpenAIChatSession):
    """Chat session that satisfies MiMo's reasoning_content round-trip contract.

    Real ``reasoning_content`` produced by ``interface_converters.to_openai``
    from captured ``ThinkingBlock``s is preserved verbatim. Assistant turns
    after the first tool_call that lack a ThinkingBlock get a per-turn-unique
    fallback. Pre-tool-call plain-text assistant turns are left alone.
    """

    def _build_messages(self) -> list[dict]:
        messages = super()._build_messages()
        seen_tool_call = False
        turn_idx = 0
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            if msg.get("tool_calls"):
                seen_tool_call = True
            if seen_tool_call:
                turn_idx += 1
                if not msg.get("reasoning_content"):
                    msg["reasoning_content"] = _fallback_reasoning_for(msg, turn_idx)
        return messages


class MimoCompactionHardFailure(RuntimeError):
    """Raised when standalone MiMo ``/responses/compact`` invocation, the
    provider call itself, or parsing its output fails.

    Unlike Codex (compaction failure is non-fatal — see
    ``CodexResponsesSession._compact_now``), MiMo's documented Responses API
    gives LingTai no generic ``context_management`` fallback and no
    server-side state to lean on (``store``/``previous_response_id``/
    ``conversation`` are all unsupported) — a MiMo session that silently kept
    replaying full, ever-growing history past its configured
    ``context_token_limit`` would eventually exceed the provider's context
    window with no warning. So a MiMo standalone-compaction failure always
    surfaces to the caller instead.
    """


class MimoResponsesSession(_StandaloneCompactionMixin, OpenAIResponsesSession):
    """Stateless Responses session for MiMo's native Responses wire.

    Every turn replays the full raw canonical interface via
    ``to_responses_input`` (no ``store``, no ``previous_response_id``, no
    ``conversation`` — all three are documented-unsupported on MiMo's
    Responses API) — or, once standalone compaction has fired, the opaque
    compacted prefix plus the strict-additive delta since the boundary (see
    ``_StandaloneCompactionMixin``). The generic OpenAI Responses
    ``context_management`` auto-compaction is never used for MiMo: the
    session is always constructed with ``compact_threshold=None`` (see
    ``MimoAdapter._create_responses_session``), and MiMo's docs mark
    ``context_management`` explicitly incompatible.

    Standalone compaction failure is a HARD failure for MiMo — see
    ``MimoCompactionHardFailure`` and ``_compact_now``. This is the one
    behavioral difference from ``CodexResponsesSession``, which treats the
    same failure as non-fatal.
    """

    def __init__(self, *args, compact_token_limit: int | None = None, **kwargs):
        kwargs["stateless_replay"] = True
        super().__init__(*args, **kwargs)
        self._init_standalone_compaction(compact_token_limit)
        # The exact wire representation used for the request currently being
        # built by ``_replay_input_items()`` — captured there (not
        # recomputed after the call returns) because ``send``/``send_stream``
        # record the assistant's reply onto ``self._interface`` before
        # returning, which would make a post-hoc recomputation include a turn
        # the actual request never carried.
        self._pending_request_representation: list[dict[str, Any]] | None = None

    def _replay_input_items(self) -> list[dict]:
        self._maybe_compact_before_send()
        compacted_replay = self._compacted_replay_input()
        items = (
            compacted_replay
            if compacted_replay is not None
            else to_responses_input(self._interface)
        )
        self._pending_request_representation = items
        return items

    def _record_calibration_sample(self, response: LLMResponse) -> None:
        """Capture the local/provider calibration pair used by the
        projected-token compaction trigger (mirrors Codex's own capture in
        ``CodexResponsesSession.send_stream``, extracted here as MiMo's own
        post-response hook since MiMo's ``send``/``send_stream`` are the
        plain ``OpenAIResponsesSession`` implementations, not a MiMo-specific
        override)."""
        try:
            provider_input_tokens = int(response.usage.input_tokens or 0)
        except Exception:
            provider_input_tokens = 0
        self._last_provider_input_tokens = (
            provider_input_tokens if provider_input_tokens > 0 else None
        )
        representation = self._pending_request_representation
        if self._last_provider_input_tokens is not None and representation is not None:
            try:
                from ..openai.adapter import _estimate_responses_input_tokens

                self._last_local_estimate_tokens = _estimate_responses_input_tokens(
                    self._instructions, self._tools, representation,
                )
            except Exception:
                self._last_local_estimate_tokens = None
        else:
            self._last_local_estimate_tokens = None

    def send(self, message) -> LLMResponse:
        response = super().send(message)
        self._record_calibration_sample(response)
        return response

    def send_stream(self, message, on_chunk=None, on_output_chars=None) -> LLMResponse:
        response = super().send_stream(
            message, on_chunk=on_chunk, on_output_chars=on_output_chars
        )
        self._record_calibration_sample(response)
        return response

    def _compaction_prefix_input(self, entries: list[Any]) -> list[dict[str, Any]]:
        """Plain conversion — MiMo has no per-session tool-output freeze map
        (that machinery exists only for Codex's WebSocket incremental delta
        path, which MiMo never uses)."""
        if not entries:
            return []
        temp_interface = ChatInterface()
        temp_interface.entries.extend(entries)
        return to_responses_input(temp_interface)

    def _compact_now(self) -> None:
        """Call standalone MiMo compaction and store the opaque replay basis.

        Unlike ``CodexResponsesSession._compact_now``, EVERY failure here —
        no safe boundary yet is NOT a failure and stays a no-op exactly like
        Codex, but a provider call raising, the call returning no usable
        output, or the output failing to normalize — is a hard failure: it
        raises ``MimoCompactionHardFailure`` rather than silently skipping
        compaction for this turn. Never logs or persists
        ``encrypted_content``/opaque summary text; only structural item
        types/counts are safe to record.
        """
        prepared = self._prepare_compact_request()
        if prepared is None:
            # No safe boundary yet (e.g. not enough history) — not a failure,
            # exactly like Codex: there is nothing to compact.
            return
        boundary_index, full_input = prepared
        try:
            compacted = self._client.responses.compact(
                model=self._model,
                input=full_input,
                instructions=self._instructions,
                # No ``store`` kwarg: the standalone ``responses.compact`` SDK
                # method is keyword-only with no ``store`` parameter at all.
                prompt_cache_key=self._prompt_cache_key,
            )
        except Exception as exc:
            raise MimoCompactionHardFailure(
                f"MiMo standalone /responses/compact failed: {type(exc).__name__}"
            ) from exc
        output_items = list(getattr(compacted, "output", None) or [])
        if not output_items:
            raise MimoCompactionHardFailure(
                "MiMo standalone /responses/compact returned no output items"
            )
        normalized: list[dict[str, Any]] = []
        for item in output_items:
            if hasattr(item, "model_dump"):
                normalized.append(item.model_dump(mode="json", exclude_none=True))
            elif isinstance(item, dict):
                normalized.append(dict(item))
        if not normalized:
            raise MimoCompactionHardFailure(
                "MiMo standalone /responses/compact output failed to normalize"
            )
        self._compacted_items = normalized
        self._compacted_at_entry_count = boundary_index


class MimoAdapter(OpenAIAdapter):
    """OpenAI-compat adapter pinned to MiMo.

    Defaults to the native OpenAI Responses wire (``MimoResponsesSession`` —
    stateless full-history/opaque-compacted replay, no ``store``/
    ``previous_response_id``/``conversation``, never generic
    ``context_management``). An explicit ``wire_api="chat_completions"``
    still selects the Chat Completions escape hatch (``MimoChatSession``,
    the ``reasoning_content`` round-trip session) — the canonical
    ``wire_api`` selector inherited from ``OpenAIAdapter`` already supports
    this; only the default changes here.
    """

    _session_class = MimoChatSession

    def __init__(self, *args, wire_api: str | None = None, compact_token_limit: int | None = None, **kwargs):
        # Default -> Responses (native MiMo wire). Explicit "chat_completions"
        # (or "auto"/"responses") from the caller still wins verbatim.
        kwargs.setdefault("compact_threshold", None)
        kwargs.setdefault("responses_stateless_replay", True)
        super().__init__(*args, wire_api=wire_api or "responses", **kwargs)
        # Standalone MiMo compaction threshold (daemon task
        # ``context_token_limit``), the same axis as Codex's
        # ``codex_compact_token_limit`` but MiMo-scoped. ``None`` -> the
        # session falls back to its own resolved ``context_window()`` at
        # check time. Validated eagerly so an invalid daemon task value fails
        # at adapter construction, before any request.
        self._mimo_compact_token_limit = _validate_codex_compact_token_limit(
            compact_token_limit
        )

    def _default_prompt_cache_key(self, model: str) -> str:
        # Fixed provider identity — use a clean ``lingtai-mimo`` namespace
        # rather than the base_url host. MiMo accepts ``prompt_cache_key``
        # (compat probe) on both wires; a stable key lets successive turns
        # hit the cross-request prompt cache.
        return f"lingtai-mimo:{model}:v1"

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        context_window: int = 0,
    ) -> MimoResponsesSession:
        from ..openai.adapter import _build_responses_tools, _responses_reasoning_kwargs

        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        mimo_tools = _build_responses_tools(tools)
        tool_choice: str | None = None
        if force_tool_call and mimo_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}
        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }
        extra_kwargs.update(_responses_reasoning_kwargs(thinking))

        return MimoResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=mimo_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            # Never the generic OpenAI Responses auto-compaction for MiMo —
            # its docs mark ``context_management`` explicitly incompatible.
            compact_threshold=None,
            interface=interface,
            prompt_cache_key=self._resolve_prompt_cache_key(model),
            context_window=context_window,
            stateless_replay=True,
            compact_token_limit=self._mimo_compact_token_limit,
        )
