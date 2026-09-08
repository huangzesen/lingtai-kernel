"""LLMAdapter ABC — abstract interface for LLM provider adapters.

Moved from lingtai-kernel to lingtai: adapters are an implementation concern,
not a kernel protocol type.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from lingtai.kernel.llm.base import ChatSession, FunctionSchema, LLMResponse
from lingtai.kernel.llm.interface import ChatInterface, ToolResultBlock

from .api_gate import APICallGate


class _GatedSession:
    """Thin proxy that routes session.send / send_stream through the gate.

    Hoisted from the MiniMax adapter so every provider gets rate gating
    by inheritance. The proxy is transparent in *both* directions:
    attribute reads fall through to the inner session via ``__getattr__``
    and attribute writes forward to the inner session via ``__setattr__``
    (except the proxy's own two slots, ``_inner`` and ``_gate``). That
    write-forwarding is load-bearing: the inner session reads its own
    attributes internally — every adapter's ``send()``/``send_stream()``
    fires ``self.pre_request_hook(...)`` and ``ChatSession.get_state()``
    reads ``session_id``/``_agent_type``/``_tracked``. If writes landed on
    the proxy, those internal reads would see stale class defaults and the
    kernel drain hook would silently never fire under rate gating.
    """

    # The only state that genuinely belongs to the proxy, not the inner
    # session. Everything else set through the proxy goes to the inner.
    _PROXY_SLOTS = frozenset({"_inner", "_gate"})

    def __init__(self, inner: ChatSession, gate: "APICallGate"):
        # Use object.__setattr__ to avoid triggering any subclass __setattr__
        # and to land these on the proxy itself, not the inner.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_gate", gate)

    def __setattr__(self, name, value):
        if name in self._PROXY_SLOTS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    def __delattr__(self, name):
        if name in self._PROXY_SLOTS:
            object.__delattr__(self, name)
        else:
            delattr(self._inner, name)

    @property
    def interface(self):
        return self._inner.interface

    @property
    def pre_request_hook(self):
        # Named getter/setter so the hook lives on the inner adapter — the
        # object whose send() reads ``self.pre_request_hook`` before the API
        # call. A plain write would land in the proxy __dict__ and leave the
        # inner's hook None, so the kernel drain hook would silently never
        # fire under rate gating. The proxy never invokes the hook itself;
        # the inner adapter continues to own request timing.
        return self._inner.pre_request_hook

    @pre_request_hook.setter
    def pre_request_hook(self, hook):
        self._inner.pre_request_hook = hook

    def send(self, message):
        return self._gate.submit(lambda: self._inner.send(message))

    def send_stream(self, message, on_chunk=None, on_output_chars=None):
        kwargs = {"on_chunk": on_chunk}
        if on_output_chars is not None:
            try:
                params = inspect.signature(self._inner.send_stream).parameters.values()
            except (TypeError, ValueError):
                params = ()
            if any(
                param.name == "on_output_chars"
                or param.kind is inspect.Parameter.VAR_KEYWORD
                for param in params
            ):
                kwargs["on_output_chars"] = on_output_chars
        return self._gate.submit(
            lambda: self._inner.send_stream(message, **kwargs)
        )

    def adapter_comment(self):
        comment_fn = getattr(self._inner, "adapter_comment", None)
        if not callable(comment_fn):
            return None
        return comment_fn()

    def static_adapter_comment(self):
        static_comment = getattr(self._inner, "static_adapter_comment", None)
        if callable(static_comment):
            return static_comment()
        return None

    def dynamic_adapter_comment(self):
        dynamic_comment = getattr(self._inner, "dynamic_adapter_comment", None)
        if callable(dynamic_comment):
            return dynamic_comment()
        return None
    def on_history_summarized(self, summarized_ids):
        hook = getattr(self._inner, "on_history_summarized", None)
        if callable(hook):
            hook(summarized_ids)

    def on_notification_dismissed(self, channel=None):
        hook = getattr(self._inner, "on_notification_dismissed", None)
        if callable(hook):
            hook(channel)

    def __getattr__(self, name):
        # Only fires when normal attribute lookup on the proxy fails.
        return getattr(self._inner, name)


class LLMAdapter(ABC):
    """Abstract interface that every LLM provider adapter must implement."""

    _gate: APICallGate | None = None

    def _setup_gate(self, max_rpm: int) -> None:
        """Set up rate-limiting gate for this adapter.

        Args:
            max_rpm: Maximum requests per minute. 0 disables.
        """
        if max_rpm > 0:
            self._gate = APICallGate(max_rpm)

    def _gated_call(self, fn: Callable[[], Any]) -> Any:
        """Run fn through the gate if configured, otherwise call directly."""
        if self._gate is not None:
            return self._gate.submit(fn)
        return fn()

    def _wrap_with_gate(self, session: ChatSession) -> ChatSession:
        """Return *session* wrapped in a gate proxy if a gate is configured,
        otherwise return *session* unchanged.

        Every adapter's ``create_chat`` implementation should pass its
        return value through this helper so per-call rate limiting is
        applied uniformly across providers without per-adapter code.
        """
        if self._gate is None:
            return session
        return _GatedSession(session, self._gate)  # type: ignore[return-value]

    def static_adapter_comment(self) -> dict[str, Any] | None:
        """Return static, prompt-safe adapter guidance before chat creation.

        Dynamic per-turn state belongs on the concrete ``ChatSession``.  This
        adapter-level hook is for rule-like guidance that the prompt builder may
        need while constructing the very first system prompt, before a session
        object exists.
        """
        return None

    @abstractmethod
    def create_chat(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        *,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        interaction_id: str | None = None,
        context_window: int = 0,
    ) -> ChatSession:
        """Create a new multi-turn chat session.

        Args:
            model: Model identifier (e.g. ``"gemini-3-flash-preview"``).
            system_prompt: System instruction for the session.
            tools: Tool/function schemas available to the model.
            json_schema: If set, enforce JSON output conforming to this schema.
            force_tool_call: If True, force the model to call a tool (Gemini
                ``mode="ANY"``).
            interface: Previously saved ChatInterface to restore.
                The session inherits this interface instance and converts
                it to provider format for the initial API state.
            thinking: Thinking level — ``"low"``, ``"high"``, or ``"default"``
                (adapter decides).
            interaction_id: Gemini Interactions API session ID for server-side
                history resume.  Ignored by providers that don't support it.
            context_window: Total context window in tokens for this model.
                0 = unknown.  Provided by LLMService.
        """

    @abstractmethod
    def generate(
        self,
        model: str,
        contents: str | list,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        """One-shot generation (no chat history).

        Used for memory analysis, follow-up suggestions, document extraction,
        and other single-turn calls.
        """

    @abstractmethod
    def make_tool_result_message(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None
    ) -> ToolResultBlock:
        """Build a canonical ToolResultBlock.

        Args:
            tool_name: The name of the tool that was called.
            result: The result dict returned by the tool executor.
            tool_call_id: Provider-assigned tool-call ID from ToolCall.id.
        """

    @abstractmethod
    def is_quota_error(self, exc: Exception) -> bool:
        """Return True if ``exc`` represents a quota/rate-limit error (429)."""
