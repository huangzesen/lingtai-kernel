"""
Shared LLM utilities used by BaseAgent and its subclasses.

All functions are stateless (operate on passed-in state dicts).
"""

import contextvars
import inspect
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from .llm import LLMResponse
from .llm.base import llm_replay_terminal_flags
from .logging import get_logger

_logger = get_logger()

# LLM API call timeout thresholds (seconds)
_LLM_WARN_INTERVAL = 20  # log a warning every N seconds while waiting

# Grace period after retry_timeout expires: the worker's HTTP timeout should
# fire at the same moment, its except-block runs drop_trailing synchronously,
# then the future settles. We wait this long for that cleanup to complete
# before raising TimeoutError to AED. If the worker is still running after
# grace, AED must not retry against the shared ChatInterface.
_WORKER_SETTLE_GRACE = 5.0


class WorkerStillRunningError(TimeoutError):
    """The LLM worker is still alive after the main timeout + settle grace.

    This is stronger than an ordinary provider timeout: the provider adapter
    may still hold and mutate the shared ChatInterface, so AED must not repair
    or retry against that interface in-process.
    """

    def __init__(self, *, elapsed: float, grace: float, agent_name: str,
                 future: Future | None = None):
        self.elapsed = elapsed
        self.grace = grace
        self.agent_name = agent_name
        self.future = future
        super().__init__(
            f"LLM worker still running after {elapsed:.0f}s + {grace:.0f}s grace; "
            "ChatInterface is unsafe for AED retry"
        )


def _send(
    submit_fn,
    timeout_pool: ThreadPoolExecutor,
    retry_timeout: float,
    agent_name: str,
) -> LLMResponse:
    """Send a message to the LLM. Single attempt with timeout.

    Note: on Python >= 3.11, ``except TimeoutError`` alone cannot distinguish a
    wait-slice expiry from a worker-raised builtin ``TimeoutError`` (``socket
    .timeout`` / ``asyncio.TimeoutError`` are aliases of it); ``future.done()``
    is the discriminator and must stay part of this loop.
    """
    future: Future = submit_fn()
    t0 = time.monotonic()
    while True:
        elapsed = time.monotonic() - t0
        remaining = retry_timeout - elapsed
        if remaining <= 0:
            settled = _wait_for_worker_settle(future, elapsed, agent_name)
            if settled is not None:
                return settled
            raise TimeoutError(f"LLM API call timed out after {elapsed:.0f}s")
        wait = min(_LLM_WARN_INTERVAL, remaining)
        try:
            return future.result(timeout=wait)
        except TimeoutError:
            if future.done():
                # The worker settled: either it raised its own TimeoutError
                # (socket.timeout / asyncio.TimeoutError / concurrent.futures
                # TimeoutError are all the builtin TimeoutError on >=3.11) or it
                # finished during the race after our wait expired. Either way
                # the worker is gone and its cleanup already ran — surface the
                # real outcome instead of misreading it as "not responding".
                return future.result(timeout=0)
            elapsed = time.monotonic() - t0
            if elapsed >= retry_timeout:
                settled = _wait_for_worker_settle(future, elapsed, agent_name)
                if settled is not None:
                    return settled
                raise TimeoutError(f"LLM API call timed out after {elapsed:.0f}s")
            _logger.warning(
                "[%s] LLM API not responding after %.0fs...",
                agent_name, elapsed,
            )


def _wait_for_worker_settle(
    future: Future, elapsed: float, agent_name: str
) -> LLMResponse | None:
    """Wait briefly for the worker future to finish after the main-thread
    watchdog expires. The worker's HTTP timeout should fire at (or near) the
    same moment via the per-call ``timeout`` plumbed down to the SDK, letting
    its except-block run ``drop_trailing`` on the shared ChatInterface
    synchronously before we propagate. Without this wait, AED's recovery
    races with the worker's in-progress mutations.

    If the worker COMPLETED SUCCESSFULLY during grace, return its LLMResponse
    and the caller must use it instead of raising: visible chunks may already
    be delivered and the assistant response committed to the canonical
    interface, so discarding the success as a plain transient TimeoutError
    would reopen transient/AED replay after committed output. Returns None
    when the worker failed with an ordinary (replayable) exception.

    If the worker is still running after the grace period, raise a distinct
    WorkerStillRunningError. AED must not treat this as an ordinary timeout
    because the provider worker may still mutate the shared ChatInterface.

    A worker that settled with its own builtin ``TimeoutError`` (``socket
    .timeout`` / ``asyncio.TimeoutError`` / ``concurrent.futures.TimeoutError``
    are all the same class on >= 3.11) is NOT still running: its except-block
    already ran ``drop_trailing``. Use ``future.done()`` to tell the two apart
    rather than ``except TimeoutError`` alone.
    """
    try:
        result = future.result(timeout=_WORKER_SETTLE_GRACE)
    except TimeoutError:
        if future.done():
            # The worker settled with its own TimeoutError (socket.timeout,
            # asyncio.TimeoutError, and concurrent.futures.TimeoutError are all
            # the builtin TimeoutError on >=3.11). Its except-block already ran
            # drop_trailing — same as any other worker error.
            return
        _logger.error(
            "[%s] LLM worker thread still running after %.0fs + %.0fs grace — "
            "interface state may be inconsistent. Refusing AED retry.",
            agent_name, elapsed, _WORKER_SETTLE_GRACE,
        )
        raise WorkerStillRunningError(
            elapsed=elapsed,
            grace=_WORKER_SETTLE_GRACE,
            agent_name=agent_name,
            future=future,
        )
    except Exception as worker_exc:
        # Worker raised something other than timeout — its except-block
        # already ran drop_trailing. An exact kernel replay-terminal wrapper
        # means visible output was delivered or a provider-owned recovery
        # budget was consumed; replacing it with a plain TimeoutError would
        # reopen transient retries/AED past that budget, so rethrow the exact
        # wrapper. Ordinary worker exceptions are still swallowed and the
        # main thread re-raises its own TimeoutError.
        partial_stream, no_aed_retry = llm_replay_terminal_flags(worker_exc)
        if partial_stream or no_aed_retry:
            raise
        return None
    _logger.warning(
        "[%s] LLM worker completed successfully during the settle grace after "
        "the %.0fs watchdog expired; using the completed response",
        agent_name, elapsed,
    )
    return result


class _SubmitFn:
    """Callable that wraps chat.send or chat.send_stream for _send.

    Before submitting to the thread pool, sets ``chat._request_timeout`` to
    ``retry_timeout`` so the adapter passes a matching per-call timeout to
    the HTTP client. This aligns worker and main-thread timeouts: when the
    watchdog raises in _send, the worker is already cleaning up or about
    to, not mid-HTTP-request.
    """

    __slots__ = ("chat", "message", "_pool", "_method", "_extra_args", "_extra_kwargs",
                 "_retry_timeout")

    def __init__(self, pool, chat, message, method: str, extra_args: tuple = (),
                 retry_timeout: float | None = None, extra_kwargs: dict | None = None):
        self._pool = pool
        self.chat = chat
        self.message = message
        self._method = method
        self._extra_args = extra_args
        self._extra_kwargs = dict(extra_kwargs or {})
        self._retry_timeout = retry_timeout

    def __call__(self) -> Future:
        fn = getattr(self.chat, self._method)
        if self._retry_timeout is not None and hasattr(self.chat, "_request_timeout"):
            self.chat._request_timeout = self._retry_timeout
        # ``ContextVar`` state is thread-local. Provider admission is bound by
        # the Agent turn on its run-loop thread, while the concrete send runs
        # in this timeout worker; copy at submission so a valid root admission
        # reaches the actual provider-I/O boundary rather than failing closed.
        context = contextvars.copy_context()
        return self._pool.submit(
            context.run, fn, self.message, *self._extra_args, **self._extra_kwargs
        )


def send_with_timeout(
    chat,
    message,
    timeout_pool: ThreadPoolExecutor,
    retry_timeout: float,
    agent_name: str,
    logger,
) -> LLMResponse:
    """Send a message to the LLM with periodic warnings. Single attempt, no retry."""
    submit_fn = _SubmitFn(timeout_pool, chat, message, "send",
                          retry_timeout=retry_timeout)
    return _send(submit_fn, timeout_pool, retry_timeout, agent_name)


def _accepts_keyword(fn, name: str) -> bool:
    """True when ``fn`` takes keyword ``name`` (named or via ``**kwargs``).

    ``False`` when ``fn`` is missing or its signature cannot be inspected, so
    an optional keyword is simply omitted rather than risked.
    """
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    param = params.get(name)
    if param is not None and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
        return True
    return any(p.kind is p.VAR_KEYWORD for p in params.values())


def send_with_timeout_stream(
    chat,
    message,
    timeout_pool: ThreadPoolExecutor,
    retry_timeout: float,
    agent_name: str,
    logger,
    on_chunk=None,
    on_output_chars=None,
) -> LLMResponse:
    """Like ``send_with_timeout`` but uses ``chat.send_stream()`` for incremental text.

    ``on_chunk`` is called from the thread-pool thread as visible text deltas
    arrive.  ``on_output_chars`` is the count-only output-progress callback
    (``ChatSession.send_stream``): it receives the length of every provider
    output fragment, never content.  A callback that is ``None`` is not
    passed at all, so with neither callback the ``send_stream(message)``
    call shape is byte-for-byte the pre-existing one.  ``on_output_chars`` is
    also omitted — fail-open, no progress, never a ``TypeError`` — when the
    session's ``send_stream`` does not accept that keyword (a legacy override
    without it) or its signature cannot be inspected; it is never retried.
    """
    extra_args = (on_chunk,) if on_chunk is not None else ()
    extra_kwargs = None
    if on_output_chars is not None:
        if _accepts_keyword(getattr(chat, "send_stream", None), "on_output_chars"):
            extra_kwargs = {"on_output_chars": on_output_chars}
        else:
            _logger.debug("[%s] send_stream does not accept on_output_chars; "
                          "streaming without output progress", agent_name)
    submit_fn = _SubmitFn(timeout_pool, chat, message, "send_stream", extra_args,
                          retry_timeout=retry_timeout, extra_kwargs=extra_kwargs)
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
