"""SessionManager — LLM session lifecycle, token tracking, and compaction.

Extracted from BaseAgent to isolate LLM communication concerns.
BaseAgent delegates all session operations here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .config import AgentConfig
from .llm import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
    LLMService,
)
from .llm_utils import (
    send_with_timeout,
    send_with_timeout_stream,
    track_llm_usage,
)
from .logging import get_logger
from .token_counter import count_tokens, count_tool_tokens

logger = get_logger()


class SessionManager:
    """Manages LLM session lifecycle, token tracking, and context compaction.

    Receives callback functions for building system prompts and tool schemas
    so it has no reference to BaseAgent.
    """

    def __init__(
        self,
        *,
        llm_service: LLMService,
        config: AgentConfig,
        agent_name: str | None = None,
        streaming: bool,
        build_system_prompt_fn: Callable[[], str],
        build_tool_schemas_fn: Callable[[], list[FunctionSchema]],
        logger_fn: Callable[..., None] | None,
    ):
        self._llm_service = llm_service
        self._config = config
        self._agent_name = agent_name
        self._display_name = agent_name or "agent"
        self._streaming = streaming
        self._build_system_prompt_fn = build_system_prompt_fn
        self._build_tool_schemas_fn = build_tool_schemas_fn
        self._logger_fn = logger_fn

        # Persistent LLM session
        self._chat: ChatSession | None = None
        self._interaction_id: str | None = None

        # Token tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_thinking_tokens = 0
        self._total_cached_tokens = 0
        self._api_calls = 0
        self._last_tool_context = "send_message"
        self._system_prompt_tokens = 0
        self._tools_tokens = 0
        self._token_decomp_dirty = True
        self._token_fallback_warned = False
        self._latest_input_tokens = 0

        # Compaction pressure tracking
        self._compaction_warnings: int = 0

        # Streaming state
        self._text_already_streamed = False
        self._intermediate_text_streamed = False
        self._message_seq = 0

        # Timeout pool for LLM calls
        self._timeout_pool = ThreadPoolExecutor(max_workers=1)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def chat(self) -> ChatSession | None:
        """The current LLM chat session (or None if not yet created)."""
        return self._chat

    @chat.setter
    def chat(self, value: ChatSession | None) -> None:
        self._chat = value

    @property
    def token_decomp_dirty(self) -> bool:
        return self._token_decomp_dirty

    @token_decomp_dirty.setter
    def token_decomp_dirty(self, value: bool) -> None:
        self._token_decomp_dirty = value

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def interaction_id(self) -> str | None:
        return self._interaction_id

    @interaction_id.setter
    def interaction_id(self, value: str | None) -> None:
        self._interaction_id = value

    @property
    def intermediate_text_streamed(self) -> bool:
        return self._intermediate_text_streamed

    @intermediate_text_streamed.setter
    def intermediate_text_streamed(self, value: bool) -> None:
        self._intermediate_text_streamed = value

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, event_type: str, **fields) -> None:
        """Delegate logging to the injected logger function."""
        if self._logger_fn is not None:
            self._logger_fn(event_type, **fields)

    # ------------------------------------------------------------------
    # LLM communication
    # ------------------------------------------------------------------

    def ensure_session(self) -> ChatSession:
        """Ensure a persistent LLM session exists, creating one if needed."""
        if self._chat is None:
            self._chat = self._llm_service.create_session(
                system_prompt=self._build_system_prompt_fn(),
                tools=self._build_tool_schemas_fn() or None,
                model=self._config.model or self._llm_service.model,
                thinking="high",
                agent_type=self._display_name,
                tracked=True,
                interaction_id=self._interaction_id,
                provider=self._config.provider,
            )
        return self._chat

    def _rebuild_session(
        self, interface: "ChatInterface", tracked: bool = True,
    ) -> None:
        """Create a new chat session with current config, preserving history."""
        self._chat = self._llm_service.create_session(
            system_prompt=self._build_system_prompt_fn(),
            tools=self._build_tool_schemas_fn() or None,
            model=self._config.model or self._llm_service.model,
            thinking="high",
            agent_type=self._display_name,
            tracked=tracked,
            provider=self._config.provider,
            interface=interface,
        )

    def send(self, message: Any) -> LLMResponse:
        """Send a message to the LLM, reusing the persistent chat session.

        Single attempt — no retry. Raises on any failure; the caller
        (BaseAgent._run_loop AED loop) handles recovery.
        """
        self.ensure_session()

        # Rebuild system prompt and tools every turn — they may have changed
        # (e.g. memory loaded, identity updated, capabilities added after refresh).
        # If the content is identical, this is a no-op at the LLM level.
        self._chat.update_system_prompt(self._build_system_prompt_fn())
        self._chat.update_tools(self._build_tool_schemas_fn() or None)

        self._log(
            "llm_call",
            model=self._config.model or self._llm_service.model or "unknown",
        )

        retry_timeout = self._config.retry_timeout

        if self._streaming:
            response = self._send_streaming(message, retry_timeout)
        else:
            response = send_with_timeout(
                chat=self._chat,
                message=message,
                timeout_pool=self._timeout_pool,
                retry_timeout=retry_timeout,
                agent_name=self._display_name,
                logger=logger,
            )

        self._track_usage(response)
        # Preserve interaction ID for session reuse
        if hasattr(self._chat, "interaction_id") and self._chat.interaction_id:
            self._interaction_id = self._chat.interaction_id
        return response

    def _send_streaming(
        self, message: Any, retry_timeout: float
    ) -> LLMResponse:
        """Streaming LLM send via send_stream."""
        self._message_seq += 1

        response = send_with_timeout_stream(
            chat=self._chat,
            message=message,
            timeout_pool=self._timeout_pool,
            retry_timeout=retry_timeout,
            agent_name=self._display_name,
            logger=logger,
        )

        if response.text:
            if response.tool_calls:
                self._intermediate_text_streamed = True
            else:
                self._text_already_streamed = True

        return response

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def get_context_pressure(self) -> float:
        """Return context usage as fraction (0.0 to 1.0). Returns 0.0 if unknown."""
        if self._chat is None:
            return 0.0
        # Use configured context_limit if set, otherwise model default
        ctx_window = self._config.context_limit or self._chat.context_window()
        if ctx_window <= 0:
            return 0.0
        # Prefer server-reported input tokens (authoritative).
        # Fall back to local estimate only before the first API response.
        tokens = self._latest_input_tokens
        if tokens <= 0:
            tokens = self._chat.interface.estimate_context_tokens()
        return tokens / ctx_window if tokens > 0 else 0.0

    # ------------------------------------------------------------------
    # Token tracking
    # ------------------------------------------------------------------

    def _update_token_decomposition(self) -> None:
        """Recompute cached system prompt and tools token counts."""
        self._system_prompt_tokens = count_tokens(self._build_system_prompt_fn())
        self._tools_tokens = count_tool_tokens(self._build_tool_schemas_fn())
        self._token_decomp_dirty = False

    def _track_usage(self, response: LLMResponse) -> None:
        """Accumulate token usage from an LLMResponse.

        If the provider returns all-zero usage, falls back to the local
        tokenizer (tiktoken / gemini / char estimate) and sets
        ``token_fallback_used`` so the TUI can warn the user.
        """
        if self._token_decomp_dirty:
            self._update_token_decomposition()

        # Detect zero-usage responses and estimate locally
        usage = response.usage
        fallback = False
        if usage and usage.input_tokens == 0 and usage.output_tokens == 0:
            estimated_output = count_tokens(response.text or "")
            # Estimate input from interface history (last user message + system prompt)
            estimated_input = self._system_prompt_tokens + self._tools_tokens
            if self._chat and self._chat.interface:
                last_entries = self._chat.interface.entries[-2:]  # last user + assistant
                for entry in last_entries:
                    for block in entry.content:
                        if hasattr(block, "text"):
                            estimated_input += count_tokens(block.text)
            from .llm.base import UsageMetadata
            usage = UsageMetadata(
                input_tokens=estimated_input,
                output_tokens=estimated_output,
            )
            response = LLMResponse(
                text=response.text,
                tool_calls=response.tool_calls,
                usage=usage,
                thoughts=response.thoughts,
                raw=response.raw,
            )
            fallback = True
            if not self._token_fallback_warned:
                self._token_fallback_warned = True
                logger.warning(
                    f"[{self._display_name}] Provider returned 0 tokens — "
                    f"using local tokenizer estimate"
                )
                self._log("token_fallback", reason="provider returned 0 tokens")

        token_state = {
            "input": self._total_input_tokens,
            "output": self._total_output_tokens,
            "thinking": self._total_thinking_tokens,
            "cached": self._total_cached_tokens,
            "api_calls": self._api_calls,
        }
        track_llm_usage(
            response=response,
            token_state=token_state,
            agent_name=self._display_name,
            last_tool_context=self._last_tool_context,
            system_tokens=self._system_prompt_tokens,
            tools_tokens=self._tools_tokens,
        )
        self._total_input_tokens = token_state["input"]
        self._total_output_tokens = token_state["output"]
        self._total_thinking_tokens = token_state["thinking"]
        self._total_cached_tokens = token_state["cached"]
        self._api_calls = token_state["api_calls"]
        if response.usage:
            self._latest_input_tokens = response.usage.input_tokens
            self._log(
                "llm_response",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                thinking_tokens=response.usage.thinking_tokens,
                cached_tokens=response.usage.cached_tokens,
                estimated=fallback,
            )

    def get_token_usage(self) -> dict:
        """Return token usage summary."""
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "thinking_tokens": self._total_thinking_tokens,
            "cached_tokens": self._total_cached_tokens,
            "total_tokens": (
                self._total_input_tokens
                + self._total_output_tokens
                + self._total_thinking_tokens
            ),
            "api_calls": self._api_calls,
            "ctx_system_tokens": self._system_prompt_tokens,
            "ctx_tools_tokens": self._tools_tokens,
            "ctx_history_tokens": max(
                0,
                self._latest_input_tokens
                - self._system_prompt_tokens
                - self._tools_tokens,
            ),
            "ctx_total_tokens": self._latest_input_tokens,
        }

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def get_chat_state(self) -> dict:
        """Serialize current chat session for persistence."""
        if self._chat is None:
            return {}
        try:
            return {"messages": self._chat.interface.to_dict()}
        except Exception:
            return {}

    def restore_chat(self, state: dict) -> None:
        """Restore chat history with current system prompt and tools."""
        from .llm.interface import ChatInterface
        messages = state.get("messages")
        if messages:
            try:
                interface = ChatInterface.from_dict(messages)
                self._rebuild_session(interface)
                return
            except Exception as e:
                logger.warning(
                    f"[{self._display_name}] Failed to restore chat: {e}. Starting fresh.",
                    exc_info=True,
                )
        self.ensure_session()

    def restore_token_state(self, state: dict) -> None:
        """Restore cumulative token counters from a saved session."""
        self._total_input_tokens = state.get("input_tokens", 0)
        self._total_output_tokens = state.get("output_tokens", 0)
        self._total_thinking_tokens = state.get("thinking_tokens", 0)
        self._total_cached_tokens = state.get("cached_tokens", 0)
        self._api_calls = state.get("api_calls", 0)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Shut down the timeout pool."""
        self._timeout_pool.shutdown(wait=False)
