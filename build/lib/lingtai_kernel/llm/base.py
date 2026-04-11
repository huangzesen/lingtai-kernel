"""Provider-agnostic types and session ABC for the LLM protocol layer.

All agent code should depend on these types, never on provider-specific SDKs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .interface import ChatInterface, ToolResultBlock


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
    """Normalized token counts."""

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0


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


@dataclass
class FunctionSchema:
    """Wraps a tool/function schema dict for type clarity.

    The ``parameters`` dict is already JSON-schema-shaped and provider-agnostic.
    """

    name: str
    description: str
    parameters: dict
    system_prompt: str = ""

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
    ) -> LLMResponse:
        """Send a message with optional streaming callback for text chunks.

        If the session supports streaming, calls ``on_chunk(text_delta)``
        as text tokens arrive.  Always returns the complete ``LLMResponse``
        at the end.

        Default implementation falls back to non-streaming ``send()``.
        """
        response = self.send(message)
        if on_chunk and response.text:
            on_chunk(response.text)
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



