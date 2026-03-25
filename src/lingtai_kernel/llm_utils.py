"""
Shared LLM utilities used by BaseAgent and its subclasses.

All functions are stateless (operate on passed-in state dicts).
"""

import contextvars
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from .llm import LLMResponse
from .logging import get_logger

_logger = get_logger()

# LLM API call timeout thresholds (seconds)
_LLM_WARN_INTERVAL = 20  # log a warning every N seconds while waiting
def _send(
    submit_fn,
    timeout_pool: ThreadPoolExecutor,
    retry_timeout: float,
    agent_name: str,
) -> LLMResponse:
    """Send a message to the LLM. Single attempt with timeout."""
    future: Future = submit_fn()
    t0 = time.monotonic()
    while True:
        elapsed = time.monotonic() - t0
        remaining = retry_timeout - elapsed
        if remaining <= 0:
            future.cancel()
            raise TimeoutError(f"LLM API call timed out after {elapsed:.0f}s")
        wait = min(_LLM_WARN_INTERVAL, remaining)
        try:
            return future.result(timeout=wait)
        except TimeoutError:
            elapsed = time.monotonic() - t0
            if elapsed >= retry_timeout:
                future.cancel()
                raise TimeoutError(f"LLM API call timed out after {elapsed:.0f}s")
            _logger.warning(
                "[%s] LLM API not responding after %.0fs...",
                agent_name, elapsed,
            )


class _SubmitFn:
    """Callable that wraps chat.send or chat.send_stream for _send."""

    __slots__ = ("chat", "message", "_pool", "_method", "_extra_args")

    def __init__(self, pool, chat, message, method: str, extra_args: tuple = ()):
        self._pool = pool
        self.chat = chat
        self.message = message
        self._method = method
        self._extra_args = extra_args

    def __call__(self) -> Future:
        fn = getattr(self.chat, self._method)
        return self._pool.submit(fn, self.message, *self._extra_args)


def send_with_timeout(
    chat,
    message,
    timeout_pool: ThreadPoolExecutor,
    retry_timeout: float,
    agent_name: str,
    logger,
) -> LLMResponse:
    """Send a message to the LLM with periodic warnings. Single attempt, no retry."""
    submit_fn = _SubmitFn(timeout_pool, chat, message, "send")
    return _send(submit_fn, timeout_pool, retry_timeout, agent_name)


def send_with_timeout_stream(
    chat,
    message,
    timeout_pool: ThreadPoolExecutor,
    retry_timeout: float,
    agent_name: str,
    logger,
    on_chunk=None,
) -> LLMResponse:
    """Like ``send_with_timeout`` but uses ``chat.send_stream()`` for incremental text.

    ``on_chunk`` is called from the thread-pool thread as text deltas arrive.
    """
    extra_args = (on_chunk,) if on_chunk is not None else ()
    submit_fn = _SubmitFn(timeout_pool, chat, message, "send_stream", extra_args)
    return _send(submit_fn, timeout_pool, retry_timeout, agent_name)


def track_llm_usage(
    response: LLMResponse,
    token_state: dict,
    agent_name: str,
    last_tool_context: str,
    *,
    system_tokens: int = 0,
    tools_tokens: int = 0,
):
    """Accumulate token usage from an LLMResponse.

    Shared implementation used by BaseAgent and its subclasses.

    Args:
        response: The LLMResponse to extract usage from.
        token_state: Mutable dict with keys 'input', 'output', 'thinking',
            'cached', 'api_calls'. Updated in-place.
        agent_name: Label for log messages.
        last_tool_context: Tool context string for the token log.
        system_tokens: Approximate token count of the system prompt (0 = unknown).
        tools_tokens: Approximate token count of tool declarations (0 = unknown).
    """
    usage = response.usage
    token_state["input"] += usage.input_tokens
    token_state["output"] += usage.output_tokens
    token_state["thinking"] += usage.thinking_tokens
    token_state["cached"] += usage.cached_tokens
    token_state["api_calls"] += 1


def execute_tools_batch(
    function_calls: list,
    tool_executor,
    parallel_safe_tools: set[str],
    parallel_enabled: bool,
    max_workers: int,
    agent_name: str,
    logger,
) -> list[tuple[str | None, str, dict, dict]]:
    """Execute tool calls, parallelizing when all are in the safe set.

    Shared implementation used by OrchestratorAgent.

    Returns list of (tool_call_id, tool_name, tool_args, result) in original order.
    """
    parsed = [
        (
            getattr(fc, "id", None),
            fc.name,
            fc.args
            if isinstance(fc.args, dict)
            else (dict(fc.args) if fc.args else {}),
        )
        for fc in function_calls
    ]

    all_safe = (
        parallel_enabled
        and len(parsed) > 1
        and all(name in parallel_safe_tools for _, name, _ in parsed)
    )

    if not all_safe:
        return [
            (tc_id, name, args, tool_executor(name, args, tc_id))
            for tc_id, name, args in parsed
        ]

    workers = min(len(parsed), max_workers)
    results_by_idx: dict[int, dict] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                contextvars.copy_context().run, tool_executor, name, args, tc_id
            ): idx
            for idx, (tc_id, name, args) in enumerate(parsed)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results_by_idx[idx] = future.result()
            except Exception as e:
                results_by_idx[idx] = {
                    "status": "error",
                    "message": f"Parallel execution error: {e}",
                }

    return [
        (parsed[i][0], parsed[i][1], parsed[i][2], results_by_idx[i])
        for i in range(len(parsed))
    ]
