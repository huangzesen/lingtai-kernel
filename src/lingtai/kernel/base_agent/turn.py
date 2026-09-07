"""Turn engine — main loop, message dispatch, LLM send, tool-call processing.

The core message lifecycle: receive → route → LLM → process → persist.
"""
from __future__ import annotations

import json
import queue
import time

from ..message import (
    Message,
    _make_message,
    MSG_CORRELATED_TURN,
    MSG_REQUEST,
    MSG_USER_INPUT,
    MSG_TC_WAKE,
    MESSAGE_TYPES,
)
from ..i18n import t as _t
from ..logging import get_logger
from ..loop_guard import LoopGuard
from ..safety_limits import (
    ACTIVE_TURN_TOOL_CALL_EMERGENCY_LIMIT,
)
from ..tool_executor import ToolExecutor
from ..tool_call_guard import ToolCallGuard
from ..risky_action_gate import build_risky_action_check
from ..turn_permissions import broker_permission_check
from ..tool_result_artifacts import CompactionStats, compact_oversized_history
from ..llm.base import (
    is_all_empty_response,
    llm_replay_terminal_flags,
    safe_exception_description,
)
from ..meta_block import (
    attach_active_notifications,
    attach_active_runtime,
    attach_active_taskcard,
    finalize_two_axis_sidecars,
    build_meta,
    build_reconstruction_tool_meta,
    render_meta,
)
from ..sent_message_tracker import SEND_TOOLS, SEND_ACTIONS, CHECK_ACTIONS
from ..time_veil import now_iso
from ..token_ledger import append_token_entry, safe_codex_pool_usage_extra
from .worker_recovery import is_worker_interface_poisoned

logger = get_logger()

# Token-ledger ``source`` label for the one-shot a-priori (``summary=true``)
# summarizer call. Flat snake_case to match the existing source taxonomy
# (``notification_sync``, ``retroactive_compaction``, …); see
# ``token_ledger.py`` module docstring.
APRIORI_SUMMARY_LEDGER_SOURCE = "summarize_apriori"


class EmptyLLMResponseError(RuntimeError):
    """The LLM returned a response with no text, no tool_calls, and no thoughts.

    A degenerate response indistinguishable from "task complete" by structure
    but actually a model failure (heavy context, provider hiccup, mid-tool
    notification injection confusing the model, etc). Raising this routes the
    failure into the AED recovery loop in ``_run_loop`` instead of silently
    transitioning to IDLE and abandoning the in-progress task.
    """

    def __init__(
        self,
        *,
        ledger_source: str,
        in_tool_loop: bool,
        response_id: str | None = None,
        response_model: str | None = None,
        finish_reason: str | None = None,
        api_call_id: str | None = None,
    ):
        self.ledger_source = ledger_source
        self.in_tool_loop = in_tool_loop
        self.response_id = response_id
        self.response_model = response_model
        self.finish_reason = finish_reason
        self.api_call_id = api_call_id
        where = "after tool results" if in_tool_loop else "on initial send"
        super().__init__(
            f"LLM returned empty response (no text, no tool_calls, no thoughts) "
            f"{where}; ledger={ledger_source}"
        )

    def diagnostic_fields(self) -> dict:
        return {
            "ledger_source": self.ledger_source,
            "in_tool_loop": self.in_tool_loop,
            "response_id": self.response_id,
            "response_model": self.response_model,
            "finish_reason": self.finish_reason,
            "api_call_id": self.api_call_id,
        }


_TRANSIENT_AED_RETRY_LIMIT = 3
_TRANSIENT_EXC_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "ServerError",
    "ServiceUnavailableError",
    "ReadError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "IncompleteRead",
    "ConnectionResetError",
    "TimeoutError",
}
_TRANSIENT_MSG_FRAGMENTS = (
    "an error occurred while processing your request",
    "peer closed connection",
    "incomplete chunked read",
    "connection reset",
    "read timed out",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)

# Issue #593: 429 / quota / rate-limit errors are transient along the *time*
# axis — the same wire succeeds once the limit resets — so they must be
# retried with backoff (ideally honoring the ``Retry-After`` header) instead of
# being funneled through the generic AED path, which retries with no delay and
# compacts history that has nothing to do with the rate limit.
_RATE_LIMIT_RETRY_LIMIT = 3
_RATE_LIMIT_BACKOFF_START_S = 5.0
_RATE_LIMIT_BACKOFF_MAX_S = 60.0
_RATE_LIMIT_MSG_FRAGMENTS = (
    "rate limit",
    "rate_limit",
    "rate-limit",
    "too many requests",
    "quota",
    "usage limit",
    "usage_limit",
    "usage_limit_reached",
    "resets_in_seconds",
    "throttl",
    "429",
)

# Deterministic 4xx client errors (excluding 429, which is handled above).
# A 400/401/403/404-style error means the request itself is wrong on our side:
# retrying the identical wire is guaranteed to fail again, so the AED branch
# must only retry when retroactive compaction actually changes the wire.
_CLIENT_ERROR_MSG_FRAGMENTS = (
    "bad request",
    "400 bad request",
    "messages_parameter_illegal",
    "invalid request",
    "invalid parameter",
    "malformed request",
)


def _exception_status_code(exc: Exception) -> int | None:
    """Best-effort HTTP-ish status extraction across SDK exception shapes."""
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _report_api_error_to_task_card(
    agent, exc: Exception, *, attempt=None, max_attempts=None, terminal=False,
) -> None:
    """Observe-only: surface a provider API error to the automatic Task Card.

    Defensive at the call site as well as inside the method: the reporting hook
    is optional (absent on lightweight agents/test doubles) and must NEVER affect
    the retry/fallback decision, the eventual success/failure, or token
    accounting — so a missing method or any failure here is swallowed.
    """
    report = getattr(agent, "_report_task_card_api_error", None)
    if report is None:
        return
    try:
        report(exc, attempt=attempt, max_attempts=max_attempts, terminal=terminal)
    except Exception:
        pass


# lingtai#672: user-visible, sanitized notice sent exactly once when AED
# exhausts. Fixed text — never embeds ``err_desc`` (which may carry
# secrets, absolute paths, or provider internals).
_AED_EXHAUSTED_USER_MESSAGE = (
    "\u26a0\ufe0f The model service is temporarily unavailable and I couldn't "
    "finish processing your request. I've paused to recover \u2014 please try "
    "again in a little while."
)


def _parse_telegram_route(message_ref: Any) -> tuple[str, int] | None:
    """Strictly parse ``'<account>:<chat_id>:<message_id>'`` to ``(account, chat_id)``.

    Mirrors the Telegram manager's ``_parse_compound_id`` route grammar:
    exactly three colon-separated parts, a non-empty account, an integer
    ``chat_id`` that is not the reserved synthetic ``updates`` bucket, and an
    integer ``message_id``.  Anything else — malformed refs, empty account,
    extra segments, non-numeric ids, the synthetic events bucket — returns
    ``None`` (never raises).  This rejects ambiguous/malformed routes so an
    AED notice can never be mis-sent to a fabricated chat.
    """
    if not isinstance(message_ref, str) or not message_ref:
        return None
    parts = message_ref.split(":")
    if len(parts) != 3 or not parts[0]:
        return None
    if parts[1] == "updates":
        # Reserved synthetic events bucket — no real chat to send into.
        return None
    try:
        chat_id = int(parts[1])
        message_id = int(parts[2])
    except (ValueError, TypeError):
        return None
    if message_id < 0:
        return None
    return parts[0], chat_id


def _aed_origin_route(agent) -> tuple[str, int] | None:
    """Derive ``(account, chat_id)`` of the current turn's originating
    Telegram chat from the notification store, or ``None`` when unavailable.

    Mirrors ``BaseAgent._setup_telegram_task_card``: Telegram previews carry
    ``message_ref`` = ``<account>:<chat_id>:<message_id>``.  Returns the first
    *valid* Telegram route (rejecting malformed refs and the synthetic
    ``updates`` bucket so a callback-first preview cannot shadow the real
    chat).  Fail-open — any anomaly returns ``None`` and never raises.
    """
    try:
        from ..notifications import _workdir_key, is_channel_allowed
        store = getattr(agent, "_notification_store", None)
        if store is None:
            return None
        workdir = _workdir_key(agent)
        notifications = store.snapshot(
            lambda ch: is_channel_allowed(ch, workdir=workdir)
        )
        telegram_data = notifications.get("mcp.telegram")
        if not telegram_data or not isinstance(telegram_data, dict):
            return None
        data = telegram_data.get("data", {})
        previews = data.get("previews", []) if isinstance(data, dict) else []
        if not previews:
            return None
        for first in previews:
            if not isinstance(first, dict):
                continue
            route = _parse_telegram_route(first.get("message_ref", ""))
            if route is not None:
                return route
        return None
    except Exception:
        return None


def _notify_aed_exhaustion_origin(agent, route: tuple[str, int] | None) -> None:
    """Best-effort one-shot sanitized notice to the originating chat.

    lingtai#672: when AED exhausts the agent falls ASLEEP without any
    user-visible reply, leaving IM users (and Telegram's typing indicator)
    hanging. Send exactly ONE sanitized failure notice to the conversation
    that originated the current turn through the existing ``telegram`` tool
    handler (the same seam an LLM tool call would use) — never a new
    outbound channel, never ``err_desc``. Strictly fail-open: any failure is
    logged and swallowed so the AED/ASLEEP decision is never affected.

    ``route`` is the immutable origin captured when the turn was dequeued
    (see ``_run_loop``) — never a live re-read of the coalesced notification
    snapshot, so a later chat or a dismissed notification cannot lose or
    misroute the notice.
    """
    if route is None:
        return
    account, chat_id = route
    try:
        handler = getattr(agent, "_tool_handlers", {}).get("telegram")
        if handler is None:
            return
        result = handler({
            "action": "send",
            "input": {
                "account": account,
                "chat_id": chat_id,
                "text": _AED_EXHAUSTED_USER_MESSAGE,
                "rendering_mode": "plain_text",
            },
            "reasoning": "aed_exhausted_notice",
        })
        # Interpret the handler result truthfully: the Telegram family returns
        # ``{status: 'error', ...}`` mappings for send failures instead of
        # raising, so record ``failed`` in that case rather than ``sent``.
        if isinstance(result, dict) and str(result.get("status", "ok")).lower() in (
            "error", "failed", "failure",
        ):
            agent._log("aed_exhausted_notice", status="failed")
        else:
            agent._log("aed_exhausted_notice", status="sent")
    except Exception as exc:
        # Sanitized failure audit: log the exception class and a fixed
        # category only — never raw handler text, which may carry
        # secrets/paths/provider internals.
        try:
            agent._log(
                "aed_exhausted_notice_failed",
                error_type=type(exc).__name__,
            )
        except Exception:
            pass


def _recover_api_error_on_task_card(agent) -> None:
    """Observe-only companion to :func:`_report_api_error_to_task_card`."""
    recover = getattr(agent, "_recover_task_card_api_error", None)
    if recover is None:
        return
    try:
        recover()
    except Exception:
        pass


def _is_transient_provider_error(exc: Exception) -> bool:
    """Return True for provider/network blips that should not spend AED budget.

    The adapter zoo wraps HTTP failures through different SDK exception
    classes.  Prefer explicit status-code handling when present; otherwise
    fall back to stable class names and conservative message fragments.
    4xx errors (including quota/rate limit) are not treated as transient here:
    429/rate-limit errors get their own backoff branch (``_is_rate_limit_error``)
    and deterministic 4xx client errors fail fast in the AED branch
    (``_is_client_error``) instead of burning retries on an unchanged wire.
    """
    if isinstance(exc, EmptyLLMResponseError):
        return True

    status_code = _exception_status_code(exc)
    if status_code is not None:
        return 500 <= status_code < 600

    try:
        import httpx  # type: ignore
    except Exception:  # pragma: no cover - httpx is a runtime dependency today
        httpx = None
    if httpx is not None and isinstance(exc, httpx.HTTPError):
        return True

    name = type(exc).__name__
    if name in _TRANSIENT_EXC_NAMES:
        return True

    msg = safe_exception_description(exc).lower()
    return any(fragment in msg for fragment in _TRANSIENT_MSG_FRAGMENTS)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True for provider 429 / quota / rate-limit errors.

    429 is transient along the *time* axis (the same wire succeeds after the
    limit resets) but must not ride the generic transient branch: it needs
    backoff (ideally ``Retry-After`` aware) and must never burn AED attempts
    or compaction on an unchanged wire.  ``_is_transient_provider_error``
    intentionally excludes all 4xx, so rate limits get their own branch here.
    """
    status_code = _exception_status_code(exc)
    if status_code is not None:
        return status_code == 429
    msg = safe_exception_description(exc).lower()
    return any(fragment in msg for fragment in _RATE_LIMIT_MSG_FRAGMENTS)


def _is_client_error(exc: Exception) -> bool:
    """Return True for deterministic 4xx client errors (excluding 429).

    400/401/403/404-style errors mean the request itself is wrong on our side.
    Retrying the identical wire is guaranteed to fail again; the only
    wire-changing recovery is retroactive compaction, so the AED branch fails
    fast when compaction has nothing to rewrite.
    """
    status_code = _exception_status_code(exc)
    if status_code is not None:
        return 400 <= status_code < 500 and status_code != 429
    msg = safe_exception_description(exc).lower()
    return any(fragment in msg for fragment in _CLIENT_ERROR_MSG_FRAGMENTS)


def _parse_retry_after(value: object) -> float | None:
    """Parse a ``Retry-After`` value: delta-seconds or HTTP-date (RFC 7231)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            delta = (parsed - datetime.now(timezone.utc)).total_seconds()
            return max(delta, 0.0)
    except Exception:  # noqa: BLE001 — best-effort parsing, never raise
        pass
    return None


def _rate_limit_retry_after_seconds(exc: Exception) -> float | None:
    """Best-effort ``Retry-After`` / ``resets_in_seconds`` extraction.

    The adapter zoo wraps rate-limit failures differently: some SDKs put the
    header on ``exc.response.headers``, some on ``exc.headers``, OpenAI-style
    errors carry ``exc.body`` as a dict, and zhipu reports ``resets_in_seconds``
    in the error payload/string.  All shapes are probed defensively; returns
    ``None`` when nothing usable is found so the caller falls back to
    exponential backoff.
    """
    for attr in ("retry_after", "retry_after_seconds"):
        parsed = _parse_retry_after(getattr(exc, attr, None))
        if parsed is not None:
            return parsed
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "get", None)
        if getter is not None:
            for key in ("Retry-After", "retry-after", "retry_after"):
                try:
                    parsed = _parse_retry_after(getter(key))
                except Exception:  # noqa: BLE001
                    parsed = None
                if parsed is not None:
                    return parsed
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("retry_after", "retry-after", "Retry-After", "resets_in_seconds"):
            parsed = _parse_retry_after(body.get(key))
            if parsed is not None:
                return parsed
    import re

    for key in ("resets_in_seconds", "retry_after", "retry-after"):
        match = re.search(
            rf"{re.escape(key)}[\"'=:\s]+(\d+)", safe_exception_description(exc), re.IGNORECASE
        )
        if match:
            return float(match.group(1))
    return None


def _tool_call_summary(tool_calls) -> dict:
    calls = list(tool_calls or [])
    return {
        "call_count": len(calls),
        "call_ids": [getattr(call, "id", None) for call in calls],
        "tool_names": [getattr(call, "name", None) for call in calls],
    }


def _pending_tool_call_summary(iface) -> dict:
    entries = getattr(iface, "entries", None) or []
    tail = entries[-1] if entries else None
    calls = []
    if getattr(tail, "role", None) == "assistant":
        calls = [
            block
            for block in getattr(tail, "content", []) or []
            if hasattr(block, "id") and hasattr(block, "name") and hasattr(block, "args")
        ]
    return {
        "pending_tool_call_count": len(calls),
        "pending_tool_call_ids": [getattr(call, "id", None) for call in calls],
        "pending_tool_names": [getattr(call, "name", None) for call in calls],
    }


def _publish_tool_loop_guard_notification(
    agent,
    *,
    reason: str,
    detail: str,
    ledger_source: str,
    in_tool_loop: bool,
    tool_call_fields: dict,
    closed_count: int,
) -> None:
    try:
        from ..notifications import submit

        submit(
            agent,
            "tool_loop_guard",
            header="tool loop guard interrupted work",
            icon="!",
            priority="normal",
            instructions=(
                "The kernel stopped a tool-call loop before dispatch. The "
                "matching synthetic tool results are already visible in the "
                "conversation transcript and say no side effects occurred "
                "from those blocked calls. Do not re-issue the same blocked "
                "tool call(s) unchanged. Continue with a different approach, "
                "summarize the blocked/completed work, or ask the human for "
                "direction, then dismiss with notification(action='dismiss_channel', "
                "input={'channel': 'tool_loop_guard', 'force': null, "
                "'reason': 'handled'}, reasoning='...')."
            ),
            data={
                "reason": reason,
                "detail": detail,
                "ledger_source": ledger_source,
                "in_tool_loop": in_tool_loop,
                "closed_tool_result_count": closed_count,
                **tool_call_fields,
                "message": (
                    "Tool loop guard stopped before dispatch. Synthetic tool "
                    "results were committed to the transcript for the blocked "
                    "calls; no side effects occurred from those calls. Do not "
                    "retry the same blocked calls unchanged; switch strategy, "
                    "summarize blocked/completed work, or ask the human."
                ),
            },
        )
    except Exception as e:
        agent._log(
            "tool_loop_guard_notification_failed",
            reason=reason,
            detail=detail,
            error=(str(e) or repr(e))[:300],
        )
        return

    agent._log(
        "tool_loop_guard_notification_published",
        reason=reason,
        detail=detail,
        closed_tool_result_count=closed_count,
    )



# Design note: this helper deliberately only closes the provider wire
# (the ChatInterface transcript sent to the LLM) and publishes
# .notification/tool_loop_guard.json. It does not post MSG_TC_WAKE.
# After the turn unwinds to IDLE, BaseAgent._sync_notifications detects the
# notification file, injects the synthetic notification call/result pair,
# and posts MSG_TC_WAKE. _handle_tc_wake then builds a fresh ToolExecutor and
# LoopGuard, so the tool-call limit counter resets for the follow-up turn.
def _handle_guarded_non_dispatch(
    agent,
    response_tool_calls,
    *,
    reason: str,
    detail: str,
    detail_field: str,
    ledger_source: str,
    in_tool_loop: bool,
    collected_text_parts: list[str],
) -> dict:
    tool_call_fields = _tool_call_summary(response_tool_calls)
    agent._log(
        "tool_calls_not_dispatched",
        ledger_source=ledger_source,
        in_tool_loop=in_tool_loop,
        reason=reason,
        detail_field=detail_field,
        **{detail_field: detail},
        **tool_call_fields,
    )

    closed_count = 0
    chat = getattr(agent, "_chat", None)
    iface = getattr(chat, "interface", None)
    if iface is not None and iface.has_pending_tool_calls():
        pending_fields = _pending_tool_call_summary(iface)
        closed_count = pending_fields["pending_tool_call_count"]
        iface.close_pending_tool_calls(
            reason=f"{reason}: {detail}",
            tool_not_dispatched=True,
        )
        agent._save_chat_history(ledger_source=ledger_source)
        agent._log(
            "guarded_tool_calls_closed",
            ledger_source=ledger_source,
            reason=reason,
            detail=detail,
            result_count=closed_count,
            pending_tool_call_ids=pending_fields["pending_tool_call_ids"],
            pending_tool_names=pending_fields["pending_tool_names"],
        )
    else:
        agent._log(
            "guarded_tool_calls_close_skipped",
            ledger_source=ledger_source,
            reason=reason,
            detail=detail,
            skipped_reason="no_chat_or_tool_calls",
        )

    _publish_tool_loop_guard_notification(
        agent,
        reason=reason,
        detail=detail,
        ledger_source=ledger_source,
        in_tool_loop=in_tool_loop,
        tool_call_fields=tool_call_fields,
        closed_count=closed_count,
    )
    return {
        "text": "\n".join(collected_text_parts),
        "failed": False,
        "errors": [],
    }


def _prepare_aed_retry_message(agent, err_desc: str) -> Message:
    """Build the system recovery prompt reused by transient and AED retries."""
    ts = now_iso(agent)
    aed_msg = _t(
        agent._config.language,
        "system.stuck_revive",
        ts=ts,
        err_desc=err_desc,
    )
    return _make_message(MSG_REQUEST, "system", aed_msg)


# Over-window / context-pressure fragments — provider errors whose root cause
# is "the wire is too long" and whose only safe recovery is to shrink the
# transcript before retry.  Distinct from generic transient errors:
# retrying transiently on the same unchanged wire just repeats the failure.
# Matched case-insensitively against a render-safe exception description.
_OVER_WINDOW_MSG_FRAGMENTS = (
    "context window",
    "context_window",
    "context length",
    "context_length_exceeded",
    "maximum context length",
    "exceeds the maximum",
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "input token count",
    "tokens in the input",
    "request too large",
    "too many tokens",
)


def _is_over_window_error(exc: Exception) -> bool:
    """Return True for provider errors whose cause is wire length.

    Routed to the deterministic AED branch (not transient) because the
    same wire will fail the same way no matter how many retries we burn.
    Retroactive compaction MUST run before the rebuilt session replays
    the transcript — otherwise we will send the AED recovery prompt into
    an unchanged over-window wire and trip the same error.
    """
    if isinstance(exc, EmptyLLMResponseError):
        return False
    msg = safe_exception_description(exc).lower()
    return any(fragment in msg for fragment in _OVER_WINDOW_MSG_FRAGMENTS)


def _compact_history_before_retry(agent, *, source: str) -> "CompactionStats | None":
    """Retroactively spill oversized tool results before an AED retry.

    Walks ``agent._session.chat.interface._entries`` and replaces any
    ``ToolResultBlock.content`` larger than the retroactive cap (default
    5K chars — tighter than the preventive 200K cap because we want to
    actually free provider tokens before retry) with a spill manifest.
    Entries, ordering, ids, and ``tool_call``/``tool_result`` pairing are
    untouched.  Already-compacted manifests are skipped.

    When at least one block is rewritten, calls
    ``agent._save_chat_history(ledger_source="retroactive_compaction")``
    so the persisted ``history/chat_history.jsonl`` matches the compacted
    wire before the session rebuild / retry replays it.

    Logs a single bounded ``aed_history_compacted`` event on every call
    (including the noop case, so operators can correlate AED firings with
    compaction activity).  The event name is intentionally distinct from
    the per-block ``tool_result_compacted_retroactively`` emitted by
    ``compact_oversized_history`` itself.

    Safe no-op if the agent has no working_dir, no live chat, or the
    interface is in an unexpected shape — AED is the recovery path and
    must never become the cause of further failures.  Any exception
    raised by attribute access or the underlying helper is swallowed and
    logged (best-effort) instead of propagating.  Returns the
    ``CompactionStats`` for the caller's convenience, or ``None`` on
    failure.
    """
    stats: CompactionStats | None = None
    try:
        chat = agent._session.chat if agent._session is not None else None
        if chat is None:
            return None
        interface = getattr(chat, "interface", None)
        working_dir = getattr(agent, "_working_dir", None)
        stats = compact_oversized_history(
            interface,
            working_dir=working_dir,
            logger_fn=getattr(agent, "_log", None),
        )
    except Exception as exc:  # noqa: BLE001 — recovery path, never re-raise
        try:
            agent._log(
                "tool_result_compaction_failed",
                source=source,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        return None

    log_fn = getattr(agent, "_log", None)
    if log_fn is not None:
        try:
            log_fn(
                "aed_history_compacted",
                source=source,
                **stats.to_log_fields(),
            )
        except Exception:
            pass

    if stats.compacted_blocks > 0:
        # Persist the shrunk wire so the rebuilt session and any later
        # snapshot load see the same compacted history the LLM will see
        # on the retry replay.
        save_fn = getattr(agent, "_save_chat_history", None)
        if save_fn is not None:
            try:
                save_fn(ledger_source="retroactive_compaction")
            except Exception as exc:  # noqa: BLE001
                if log_fn is not None:
                    try:
                        log_fn(
                            "retroactive_compaction_save_failed",
                            source=source,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    except Exception:
                        pass
    return stats


# ---------------------------------------------------------------------------
# Puffo admission witness — settle-point wire-scan.
#
# The reliable "a receipt-bearing tool result durably reached the provider
# context" fact is not the terminal tool_call_update (that fires at tool
# COMPLETED, before commit).  Instead, at each *settle point* — immediately
# after a ``send(...)`` has fully settled (returned OR its exception handling,
# incl. any rollback/restore, has completed) and after each
# ``commit_tool_results(...)`` returns — we scan the ACTUAL canonical interface
# entries.  Reading real wire state (not a bookkeeping list of what was passed
# to ``send``) is load-bearing: the adapter rollback layer sits between the
# caller and the wire, so a rolled-back entry is naturally absent and never
# fires, while a survivor of a failed send is present and fires exactly once.
#
# Two turn-scoped pieces of state bound the scan (see
# ``begin_admission_witness_scope``): a WATERMARK (the last interface entry id
# at turn start — only strictly-newer entries are this turn's) and an EMITTED
# set (tool-call ids already witnessed this turn — each fires at most once
# across the turn's multiple settle points).
# ---------------------------------------------------------------------------


def _current_last_entry_id(agent) -> int:
    """Return the newest interface entry id, or -1 when there is none yet.

    Entry ids are monotonically increasing and never reused (``_next_id`` only
    ever advances; truncation/removal never rewinds it, and ``from_dict`` seeds
    it past the max restored id), so this is a stable pre-turn/this-turn
    boundary.  A positional index would be unreliable because mid-turn removals
    (``drop_trailing``, ``remove_pair_by_call_id``) shift positions.
    """
    chat = getattr(agent, "_chat", None)
    iface = getattr(chat, "interface", None)
    if iface is None:
        return -1
    entries = getattr(iface, "entries", None) or ()
    if not entries:
        return -1
    return getattr(entries[-1], "id", -1)


def begin_admission_witness_scope(agent) -> None:
    """Open the turn-scoped admission-witness state at the turn boundary.

    Captured once, OUTSIDE the AED retry loop, so a replayed entry cannot
    double-fire within a turn and pre-turn history is never re-witnessed.
    """
    agent._puffo_admission_watermark = _current_last_entry_id(agent)
    agent._puffo_admission_emitted = set()


def end_admission_witness_scope(agent) -> None:
    """Close the turn-scoped state so scans outside a turn are no-ops."""
    agent._puffo_admission_emitted = None
    agent._puffo_admission_watermark = -1


def scan_and_emit_committed_facts(agent) -> None:
    """Emit one committed fact per new, receipt-bearing, un-witnessed result.

    Called at each settle point.  Never touches a poisoned interface (the
    WorkerStillRunning paths re-raise before reaching any scan; this is a second
    line of defence).  Rolled-back entries are absent from the wire and so never
    fire; survivors of a failed send are present and fire exactly once.
    """
    emitted = getattr(agent, "_puffo_admission_emitted", None)
    if emitted is None:
        return
    if is_worker_interface_poisoned(agent):
        return
    chat = getattr(agent, "_chat", None)
    iface = getattr(chat, "interface", None)
    if iface is None:
        return
    watermark = getattr(agent, "_puffo_admission_watermark", -1)
    from ..llm.interface import ToolResultBlock
    from ..puffo_admission_witness import (
        admission_binding,
        extract_admission_receipt,
    )
    from ..turn_events import (
        ToolResultsCommittedEvent,
        current_turn_tool_observer,
        notify_tool_results_committed,
    )

    try:
        entries = list(iface.entries)
    except Exception:
        return
    for entry in entries:
        if getattr(entry, "id", -1) <= watermark:
            continue
        if getattr(entry, "role", None) != "user":
            continue
        for block in getattr(entry, "content", None) or ():
            if not isinstance(block, ToolResultBlock):
                continue
            block_id = block.id
            if block_id in emitted:
                continue
            raw = extract_admission_receipt(block)
            if raw is None:
                # read_inbox / synthesized / malformed / no-receipt: no fact,
                # and deliberately NOT recorded in ``emitted`` so an in-place
                # synthesized→real overwrite can still fire later.
                continue
            # The binding must be computed over the SAME id the ACP frame
            # exposes as its outer ``toolCallId`` — the receiver correlates on
            # that id and recomputes the binding over it. The kernel is
            # protocol-neutral, so it asks the turn-bound observer to map the
            # kernel-namespace id to its wire id (correlation comes DOWN to the
            # witness; the raw receipt never goes UP to the adapter). Observers
            # without the mapping (non-ACP, tests) bind over the raw id.
            observer = current_turn_tool_observer()
            namespacer = getattr(observer, "wire_tool_call_id", None)
            if callable(namespacer):
                try:
                    wire_id = namespacer(block_id)
                except Exception:
                    # A namespacer that RAISES must not silently degrade to the
                    # raw id: a raw-bound frame looks delivered (notify returns
                    # True) yet the receiver deterministically rejects it, so
                    # emitting it and retiring the fact into ``emitted`` would
                    # lose one admission with no retry and no signal on either
                    # side — the one case worse than a visible non-delivery.
                    # Leave the fact un-emitted so a later settle point retries,
                    # and do NOT send a raw-bound frame. Use a DISTINCT event
                    # from the non-delivery one: a namespacer raising is a code
                    # defect that reproduces every turn and needs a fix, not the
                    # expected session-teardown non-delivery — the two demand
                    # opposite operator responses, so they must not share a code.
                    # Bound: this logs once per settle point within the turn that
                    # created the entry (later turns skip it — its id is below the
                    # next turn's watermark), so the count is bounded by the
                    # settle-point count, not unbounded; retrying is still correct
                    # because a transiently-broken namespacer then self-heals.
                    try:
                        agent._log(
                            "puffo_admission_wire_namespacer_failed",
                            tool_call_id=block_id,
                        )
                    except Exception:
                        pass
                    continue
            else:
                # Observers without the mapping (non-ACP, unit fakes) bind over
                # the raw id. The real ACP path always has the namespacer — the
                # wire-id derivation test reddens if it is ever absent — so this
                # branch cannot silently reproduce the raw-id defect there.
                wire_id = block_id
            delivered = notify_tool_results_committed(
                ToolResultsCommittedEvent(
                    wire_id, admission_binding(wire_id, raw)
                )
            )
            if delivered:
                # Only a delivered fact is retired from further settle points.
                emitted.add(block_id)
            else:
                # Non-delivery must be visible, not silent — and NOT recorded in
                # ``emitted``, so a later settle point retries (Puffo's own
                # at-most-once idempotency is the backstop). Genuine session
                # teardown makes the ACP handler return normally (delivered), so
                # this retry path does not spin on a torn-down session.
                try:
                    agent._log(
                        "puffo_admission_fact_not_delivered",
                        tool_call_id=block_id,
                    )
                except Exception:
                    pass


def _restore_tool_results_after_continuation_failure(
    agent,
    tool_results,
    *,
    ledger_source: str,
) -> bool:
    """Persist real tool results after post-tool LLM continuation failure.

    The tools already executed locally before the continuation send. If an
    adapter rolled back the attempted user tool-result entry on provider error,
    the canonical interface tail still has pending assistant tool_calls. Restore
    the real results before AED / notification heal can synthesize a placeholder
    that lacks the actual result payload.
    """
    if (
        not tool_results
        or agent._chat is None
        or not agent._chat.interface.has_pending_tool_calls()
    ):
        return False
    agent._chat.commit_tool_results(tool_results)
    # Settle point: the restore commit returned, so any receipt-bearing result
    # it re-committed is durably on the wire.
    scan_and_emit_committed_facts(agent)
    try:
        agent._save_chat_history(ledger_source=ledger_source)
    except Exception as e:
        agent._log(
            "tool_results_restored_after_continuation_failure",
            result_count=len(tool_results),
            ledger_source=ledger_source,
            failed_at="save_chat_history",
            save_error=(str(e) or repr(e))[:300],
            side_effect="memory_state_may_be_ahead_of_disk",
        )
        raise
    agent._log(
        "tool_results_restored_after_continuation_failure",
        result_count=len(tool_results),
    )
    return True


def _close_pending_tool_calls_after_poll_backoff(
    agent,
    *,
    ledger_source: str,
) -> None:
    """Defensively close any tail tool_calls left after poll-backoff commit.

    Normal adapters append the real tool results via ``commit_tool_results``,
    so this is a no-op. If an adapter leaves the assistant tool-call tail
    pending, close it here with a distinct reason before the later IDLE
    notification path can attribute the cleanup to generic idle injection.
    """
    chat = getattr(agent, "_chat", None)
    iface = getattr(chat, "interface", None)
    if iface is None or not iface.has_pending_tool_calls():
        return

    pending_fields = _pending_tool_call_summary(iface)
    phase = "close_pending_tool_calls"
    try:
        iface.close_pending_tool_calls(
            reason="poll_backoff_exit",
            tool_completed=True,
        )
        phase = "save_chat_history"
        agent._save_chat_history(ledger_source=ledger_source)
        agent._log(
            "heal_pending_tool_calls",
            reason="poll_backoff_exit",
            **pending_fields,
        )
    except Exception as e:
        fields = {
            "reason": "poll_backoff_exit",
            "failed_at": phase,
            "error": (str(e) or repr(e))[:200],
        }
        if phase == "save_chat_history":
            fields["side_effect"] = "memory_state_may_be_ahead_of_disk"
        agent._log("heal_pending_tool_calls_failed", **fields)


def _settle_correlated_after_turn(
    agent,
    control,
    handler_result,
    terminal_error: Exception | None,
    terminal_failure: str | None,
) -> None:
    """Translate the existing turn engine's terminal facts exactly once."""

    if control is None:
        return
    from ..turns import TurnOutcome, settle_turn

    outcome = TurnOutcome.NORMAL
    text = ""
    error = None
    errors: tuple[str, ...] = ()
    if terminal_error is not None:
        outcome = TurnOutcome.FAILED
        error = safe_exception_description(terminal_error)[:300]
    elif terminal_failure is not None:
        outcome = TurnOutcome.FAILED
        error = terminal_failure[:300]
    elif isinstance(handler_result, dict):
        text = str(handler_result.get("text") or "")
        raw_errors = handler_result.get("errors") or ()
        if isinstance(raw_errors, (str, bytes)):
            raw_errors = (raw_errors,)
        else:
            try:
                raw_errors = tuple(raw_errors)
            except TypeError:
                raw_errors = (raw_errors,)
        errors = tuple(str(item)[:300] for item in raw_errors[:8])
        if bool(handler_result.get("failed")):
            outcome = TurnOutcome.FAILED
            error = errors[0] if errors else "turn processing failed"
    elif not control.cancel_requested.is_set():
        outcome = TurnOutcome.FAILED
        error = "turn ended without a result"

    settle_turn(
        agent,
        control,
        outcome=outcome,
        text=text,
        error=error,
        errors=errors,
        cooperative_cancelled=agent._cancel_event.is_set(),
    )


def _run_loop(agent) -> None:
    """Process messages and terminally settle callers on every loop escape."""

    try:
        _run_loop_body(agent)
    except Exception as exc:
        # Once begin_turn publishes current ownership, any unexpected exception
        # outside the AED path must still terminally settle that exact caller.
        # The exception is re-raised so thread/process supervision keeps seeing
        # the run-loop failure instead of mistaking settlement for recovery.
        control = getattr(agent, "_current_turn_control", None)
        if control is not None:
            try:
                error = safe_exception_description(exc)
            except Exception:
                error = type(exc).__name__
            from ..turns import TurnOutcome, settle_turn

            settle_turn(
                agent,
                control,
                outcome=TurnOutcome.FAILED,
                error=str(error or type(exc).__name__)[:300],
                cooperative_cancelled=agent._cancel_event.is_set(),
            )
        raise
    finally:
        # This also covers failures after dequeue but before begin_turn: the
        # control remains registered even though current ownership was never
        # published. Lifecycle stop may race this claim; both paths are exactly
        # once under the registry lock.
        from ..turns import cancel_all_turns
        from ..execution_workspace import clear_execution_workspace
        from ..turn_events import clear_turn_tool_observer
        from ..turn_permissions import clear_turn_permission_broker
        from ..provider_admission import clear_current_provider_admission

        cancel_all_turns(agent, reason="agent run loop stopped")
        clear_execution_workspace()
        clear_turn_tool_observer()
        clear_turn_permission_broker()
        clear_current_provider_admission()


def _run_loop_body(agent) -> None:
    """Wait for messages and process them; :func:`_run_loop` owns teardown."""
    from ..state import AgentState

    while True:
        while not agent._shutdown.is_set():
            # --- Asleep: soul off, wait for inbox message ---
            if agent._asleep.is_set():
                agent._cancel_soul_timer()
                # Heal any dangling tool_calls on the wire BEFORE going to
                # sleep. If we sleep with an unanswered tool_call, the next
                # mail's _inject_notification_pair refuses to append (would
                # violate alternation invariant) and the agent silently
                # fails to wake. The chat-saved snapshot must always be
                # appendable from a fresh wake. Common cause: cancel
                # mid-batch leaves the just-arrived assistant response
                # with tool_calls on the wire but no results yet.
                if is_worker_interface_poisoned(agent):
                    # Poisoned interface — the worker may still be mutating
                    # it. Do not heal/save; refresh will rebuild from disk.
                    agent._log(
                        "sleep_heal_skipped_poisoned_interface",
                        artifact=getattr(agent, "_llm_worker_poison_artifact", None),
                    )
                elif (
                    agent._chat is not None
                    and agent._chat.interface.has_pending_tool_calls()
                ):
                    phase = "close_pending_tool_calls"
                    try:
                        agent._chat.interface.close_pending_tool_calls(
                            reason="heal:going_asleep"
                        )
                        phase = "save_chat_history"
                        agent._save_chat_history(ledger_source="heal")
                        agent._log("heal_pending_tool_calls", reason="going_asleep")
                    except Exception as e:
                        fields = {
                            "reason": "going_asleep",
                            "failed_at": phase,
                            "error": (str(e) or repr(e))[:200],
                        }
                        if phase == "save_chat_history":
                            fields["side_effect"] = "memory_state_may_be_ahead_of_disk"
                        agent._log("heal_pending_tool_calls_failed", **fields)
                agent._log("sleep")

                # Block until a message arrives or shutdown
                msg = None
                while not agent._shutdown.is_set():
                    try:
                        msg = agent.inbox.get(timeout=1.0)
                        break
                    except queue.Empty:
                        continue

                if msg is None:
                    break  # shutdown was set — exit inner loop
                # Consume a stop wake before leaving ASLEEP or dispatching a turn.
                if agent._shutdown.is_set():
                    break

                # A successful fresh dequeue owns stale-latch reset.  Clear
                # only after the shutdown recheck and before publishing ACTIVE.
                agent._asleep.clear()
                agent._cancel_event.clear()
                agent._set_state(AgentState.ACTIVE, reason=f"woke from asleep: {msg.type}")
                agent._log("wake", trigger=msg.type)
                agent._reset_uptime()
                msg = _concat_queued_messages(agent, msg)
                # Fall through to handle the message below
            else:
                try:
                    msg = agent.inbox.get(timeout=agent._inbox_timeout)
                except queue.Empty:
                    continue
                # Consume a stop wake before merging, state change, or dispatch.
                if agent._shutdown.is_set():
                    break
                # Clear only the stale latch inherited by this fresh dequeue.
                # This precedes merging so cancellation requested during merge
                # remains visible through the rest of the logical turn.
                agent._cancel_event.clear()
                msg = _concat_queued_messages(agent, msg)
                agent._set_state(AgentState.ACTIVE, reason=f"received {msg.type}")

            # Bind correlated identity only after the existing fresh-dequeue
            # stale-latch reset and message-merge boundary. Pending cancellation
            # then becomes the current cooperative latch without affecting the
            # turn ahead of it in the same inbox.
            from ..turns import (
                TurnAdmissionError,
                TurnOutcome,
                admit_turn_origin,
                begin_turn,
                correlated_message_text,
                settle_turn,
            )

            turn_control = begin_turn(agent, msg)
            execution_workspace_token = None
            tool_observer_token = None
            permission_broker_token = None
            provider_admission_token = None
            if turn_control is not None:
                # Admission was checked synchronously before publication. Check
                # again at the final inbox-to-provider boundary so a forged or
                # stale correlated envelope cannot become provider work merely
                # by reaching the run loop.
                try:
                    admission_decision = admit_turn_origin(agent, turn_control.origin)
                except TurnAdmissionError as exc:
                    settle_turn(
                        agent,
                        turn_control,
                        outcome=TurnOutcome.FAILED,
                        error="turn origin was not admitted",
                        errors=(exc.decision.reason_code,),
                    )
                    agent._set_state(AgentState.IDLE, reason="turn origin rejected")
                    continue
                from ..execution_workspace import (
                    bind_execution_workspace,
                    clear_execution_workspace,
                )
                # Every fresh correlated turn starts from an explicitly empty
                # execution scope. This is a cheap backstop for any future
                # outer-loop control-flow path that might bypass the token reset
                # below; thread exit still clears in _run_loop's finally.
                clear_execution_workspace()
                execution_workspace_token = bind_execution_workspace(
                    turn_control.execution_workspace
                )
                from ..turn_events import (
                    bind_turn_tool_observer,
                    clear_turn_tool_observer,
                )
                clear_turn_tool_observer()
                tool_observer_token = bind_turn_tool_observer(
                    turn_control.tool_observer
                )
                # Open the turn-scoped admission-witness state here, alongside
                # the observer bind and OUTSIDE the AED retry loop below, so the
                # watermark separates pre-turn history from this turn's appends
                # and the ``emitted`` set survives retries (no double-fire).
                begin_admission_witness_scope(agent)
                from ..turn_permissions import (
                    bind_turn_permission_broker,
                    clear_turn_permission_broker,
                )
                clear_turn_permission_broker()
                permission_broker_token = bind_turn_permission_broker(
                    turn_control.permission_broker
                )
                from ..provider_admission import (
                    RootProviderAdmission,
                    bind_provider_admission,
                )

                # The typed origin is checked immediately above.  Keep its
                # Core-private parent in a ContextVar for the entire logical
                # turn so every concrete provider send is gated by the
                # service wrapper, not merely this root inbox path.
                provider_admission_token = bind_provider_admission(
                    RootProviderAdmission(
                        correlation_id=turn_control.correlation_id,
                        policy_version=admission_decision.policy_version,
                    )
                )
                msg = correlated_message_text(msg)
            elif msg.type == MSG_CORRELATED_TURN:
                # Lifecycle stop may claim a control after the post-dequeue
                # shutdown check but before this bind. The private envelope is
                # then already terminal and must never fall through as a fresh
                # provider request with a serialized implementation object.
                agent._log(
                    "correlated_turn_envelope_skipped",
                    reason="control_not_live",
                )
                if agent._shutdown.is_set():
                    break
                agent._set_state(
                    AgentState.IDLE,
                    reason="stale correlated turn envelope",
                )
                continue

            # --- Process with AED (Automatic Error Detection) ---
            sleep_state = AgentState.IDLE
            handler_result = None
            terminal_error: Exception | None = None
            terminal_failure: str | None = None
            aed_attempts = 0
            transient_attempts = 0
            rate_limit_attempts = 0
            skip_post_turn_save = False
            # lingtai#672: bind the current turn's immutable Telegram origin
            # ONCE at dequeue time, before any LLM/tool work can read or
            # replace the notification snapshot.  The AED-exhaust notice
            # consumes this captured route, never a live re-read, so a later
            # chat or a dismissed notification cannot lose/misroute it.
            turn_origin = _aed_origin_route(agent)
            while True:
                try:
                    # A cancellation requested while this envelope was pending
                    # settles without starting provider or tool work.
                    if (
                        turn_control is not None
                        and turn_control.cancel_requested.is_set()
                    ):
                        break
                    # Fail closed: if a prior turn already poisoned the
                    # interface, do not run another turn against it. Request
                    # refresh and sleep instead.
                    if is_worker_interface_poisoned(agent):
                        from .worker_recovery import request_worker_hang_refresh

                        artifact = getattr(agent, "_llm_worker_poison_artifact", None)
                        agent._log(
                            "turn_skipped_poisoned_interface",
                            message_type=getattr(msg, "type", "unknown"),
                            artifact=artifact,
                        )
                        request_worker_hang_refresh(
                            agent,
                            artifact_relpath=artifact,
                            source="run_loop_poison_guard",
                        )
                        agent._asleep.set()
                        sleep_state = AgentState.ASLEEP
                        skip_post_turn_save = True
                        terminal_failure = "agent interface is unavailable"
                        break
                    handler_result = _handle_message(agent, msg)
                    terminal_error = None
                    # If a prior provider API error was surfaced to the Task Card
                    # this turn, mark it recovered (observe-only/fail-open); a
                    # clean first-attempt turn reported nothing, so this no-ops.
                    if aed_attempts or transient_attempts or rate_limit_attempts:
                        _recover_api_error_on_task_card(agent)
                    transient_attempts = 0
                    rate_limit_attempts = 0
                    break  # success (chat saved after each session.send inside)
                except Exception as e:
                    from ..llm_utils import WorkerStillRunningError

                    # Retain the most recent provider/runtime exception until a
                    # retry succeeds; terminal branches settle it as FAILED.
                    terminal_error = e
                    # Read replay-safety markers before invoking any provider
                    # exception rendering hooks.  The rendering itself is also
                    # fail-closed so a hostile ``__str__``/``__repr__`` cannot
                    # bypass terminal rollback or escape the run loop.
                    (
                        partial_stream_terminal,
                        no_aed_retry_terminal,
                    ) = llm_replay_terminal_flags(e)
                    err_desc = safe_exception_description(e)

                    # Account selection has already exhausted the configured
                    # Codex candidates. This is deterministic for this turn,
                    # so do not rebuild/replay the session or spend AED
                    # attempts on the same selection failure.
                    from ...auth.codex_account_source import NoCandidateError

                    if isinstance(e, NoCandidateError):
                        fields = {
                            "error": err_desc[:300],
                            "exception": type(e).__name__,
                        }
                        fields.update(e.diagnostic_fields())
                        agent._log("no_candidate_terminal", **fields)
                        logger.warning(
                            f"[{agent.agent_name}] No Codex account candidate: {err_desc}"
                        )
                        _report_api_error_to_task_card(
                            agent, e, terminal=True
                        )
                        sleep_state = AgentState.ASLEEP
                        agent._asleep.set()
                        break

                    if partial_stream_terminal:
                        # Provider output has already reached the human-facing
                        # stream. Replaying this turn through transient retry or
                        # AED would duplicate/mix visible content, so terminate
                        # the turn without mutating or resaving the rolled-back
                        # provider interface.
                        agent._log(
                            "llm_partial_stream_terminal",
                            error=err_desc[:300],
                            exception=type(e).__name__,
                        )
                        skip_post_turn_save = True
                        break

                    if no_aed_retry_terminal:
                        # The provider adapter has already spent its complete,
                        # request-owned recovery budget. Rebuilding the session
                        # would replay the same request outside that budget.
                        agent._log(
                            "llm_no_aed_retry_terminal",
                            error=err_desc[:300],
                            exception=type(e).__name__,
                        )
                        logger.warning(
                            f"[{agent.agent_name}] Provider recovery exhausted: {err_desc}"
                        )
                        _report_api_error_to_task_card(agent, e, terminal=True)
                        sleep_state = AgentState.ASLEEP
                        agent._asleep.set()
                        break

                    if isinstance(e, WorkerStillRunningError):
                        # Worker future is still alive — ChatInterface is
                        # unsafe to mutate from this thread. Mark it poisoned,
                        # write recovery state from safe locals, skip chat save,
                        # and request forced refresh/relaunch.
                        from .worker_recovery import (
                            build_worker_hang_context,
                            mark_worker_interface_poisoned,
                            publish_worker_hang_notification,
                            request_worker_hang_refresh,
                            write_worker_hang_artifact,
                        )

                        context = build_worker_hang_context(agent, msg, e)
                        artifact_relpath = write_worker_hang_artifact(agent, e, context)
                        mark_worker_interface_poisoned(
                            agent,
                            e,
                            context=context,
                            artifact_relpath=artifact_relpath,
                        )
                        publish_worker_hang_notification(agent, artifact_relpath, context)
                        agent._log(
                            "llm_worker_still_running",
                            error=err_desc[:300],
                            artifact=artifact_relpath,
                            turn_entry=(context.get("turn") or {}).get("entry"),
                        )
                        agent._set_state(AgentState.STUCK, reason=err_desc)
                        request_worker_hang_refresh(
                            agent,
                            artifact_relpath=artifact_relpath,
                            source="worker_still_running",
                        )
                        agent._asleep.set()
                        agent._set_state(
                            AgentState.ASLEEP,
                            reason="LLM worker still running; refresh/relaunch requested",
                        )
                        sleep_state = AgentState.ASLEEP
                        skip_post_turn_save = True
                        break

                    # Issue #144: over-window / context-pressure errors must
                    # take the deterministic AED branch, not transient
                    # retry — the same wire will fail the same way under
                    # any number of retries.  Retroactive compaction below
                    # shrinks the transcript before _rebuild_session
                    # replays it.  This is the dedicated over-window
                    # recovery path; if we ever add a hard pre-send gate
                    # (compact *before* the first send rather than after
                    # the first failure), it would slot in at
                    # _handle_message — see TODO below.
                    over_window = _is_over_window_error(e)
                    if over_window:
                        agent._log(
                            "aed_over_window_detected",
                            error=err_desc[:300],
                            exception=type(e).__name__,
                        )

                    # Issue #593: 429 / quota / rate-limit errors are transient
                    # along the time axis — the same wire succeeds after the
                    # limit resets.  They must NOT spend AED attempts or
                    # compaction (which would permanently discard history for
                    # zero benefit): wait out Retry-After when the provider
                    # sends it, otherwise exponential backoff, then resend.
                    if not over_window and _is_rate_limit_error(e):
                        if rate_limit_attempts < _RATE_LIMIT_RETRY_LIMIT:
                            rate_limit_attempts += 1
                            retry_after_s = _rate_limit_retry_after_seconds(e)
                            if retry_after_s is not None:
                                backoff_s = min(
                                    max(retry_after_s, 1.0),
                                    _RATE_LIMIT_BACKOFF_MAX_S,
                                )
                            else:
                                backoff_s = min(
                                    _RATE_LIMIT_BACKOFF_START_S
                                    * (2.0 ** (rate_limit_attempts - 1)),
                                    _RATE_LIMIT_BACKOFF_MAX_S,
                                )
                            if agent._session.chat is not None:
                                agent._session.chat.interface.close_pending_tool_calls(
                                    reason=f"rate_limit_retry: {err_desc[:200]}",
                                    tool_completed=True,
                                )
                            agent._log(
                                "aed_rate_limit_retry",
                                attempt=rate_limit_attempts,
                                max_attempts=_RATE_LIMIT_RETRY_LIMIT,
                                backoff_s=backoff_s,
                                retry_after_s=retry_after_s,
                                error=err_desc[:300],
                            )
                            logger.warning(
                                f"[{agent.agent_name}] AED rate-limit retry "
                                f"{rate_limit_attempts}/{_RATE_LIMIT_RETRY_LIMIT}: {err_desc}",
                            )
                            _report_api_error_to_task_card(
                                agent, e,
                                attempt=rate_limit_attempts,
                                max_attempts=_RATE_LIMIT_RETRY_LIMIT,
                                terminal=False,
                            )
                            # Wait on the shutdown event instead of sleeping so a
                            # stop/refresh can break the backoff immediately;
                            # break out when the event fires (shutdown).
                            if agent._shutdown.wait(backoff_s):
                                break
                            msg = _prepare_aed_retry_message(agent, err_desc)
                            continue

                        # Budget exhausted: the limit has not reset yet.  Retrying
                        # (or compacting) now would burn quota and destroy history
                        # for zero benefit — go ASLEEP and wait for refresh/human.
                        agent._log(
                            "aed_rate_limit_exhausted",
                            attempts=rate_limit_attempts,
                            error=err_desc[:300],
                        )
                        _report_api_error_to_task_card(
                            agent,
                            e,
                            attempt=rate_limit_attempts,
                            max_attempts=_RATE_LIMIT_RETRY_LIMIT,
                            terminal=True,
                        )
                        # lingtai#672: like the generic AED exhausted exit, send
                        # ONE sanitized, user-visible failure notice to the
                        # originating conversation through the existing telegram
                        # tool handler before going ASLEEP, and publish the
                        # observable ASLEEP state explicitly. Fail-open.
                        agent._set_state(
                            AgentState.ASLEEP,
                            reason=f"rate-limit exhausted after {rate_limit_attempts} attempts",
                        )
                        _notify_aed_exhaustion_origin(agent, turn_origin)
                        sleep_state = AgentState.ASLEEP
                        agent._asleep.set()
                        break

                    if not over_window and _is_transient_provider_error(e):
                        if transient_attempts < _TRANSIENT_AED_RETRY_LIMIT:
                            transient_attempts += 1
                            backoff_s = min(2.0 ** (transient_attempts - 1), 8.0)
                            if agent._session.chat is not None:
                                agent._session.chat.interface.close_pending_tool_calls(
                                    reason=f"transient_retry: {err_desc[:200]}",
                                    tool_completed=True,
                                )
                            # Issue #144: shrink oversized historical tool
                            # results to manifests before the retry so the
                            # next send doesn't ship the same too-big wire.
                            _compact_history_before_retry(agent, source="aed_transient")
                            agent._log(
                                "aed_transient_retry",
                                attempt=transient_attempts,
                                max_attempts=_TRANSIENT_AED_RETRY_LIMIT,
                                backoff_s=backoff_s,
                                error=err_desc[:300],
                            )
                            logger.warning(
                                f"[{agent.agent_name}] AED transient retry "
                                f"{transient_attempts}/{_TRANSIENT_AED_RETRY_LIMIT}: {err_desc}",
                            )
                            # Surface the provider error to the Task Card as one
                            # stable retrying row (observe-only/fail-open; sanitized
                            # to status + allow-listed code, never err_desc).
                            _report_api_error_to_task_card(
                                agent, e,
                                attempt=transient_attempts,
                                max_attempts=_TRANSIENT_AED_RETRY_LIMIT,
                                terminal=False,
                            )
                            time.sleep(backoff_s)
                            msg = _prepare_aed_retry_message(agent, err_desc)
                            continue

                        agent._log(
                            "aed_transient_exhausted",
                            attempts=transient_attempts,
                            error=err_desc[:300],
                        )
                    # TODO(issue #144 follow-up): add a hard pre-send gate
                    # in _handle_message that runs retroactive compaction
                    # whenever ``_serialized_len(interface.entries) >
                    # context_limit_threshold``, so we don't need a failed
                    # send to discover over-window.  Out of scope for this
                    # PR — current behavior is "compact on first failure
                    # then rebuild" which is correct but reactive.

                    aed_attempts += 1

                    # Close any dangling tool_calls with synthetic error
                    # tool_results.  tool_completed=True because AED fires
                    # after the tool executor already ran — the real failure
                    # is the LLM continuation, not the tool itself.
                    if agent._session.chat is not None:
                        agent._session.chat.interface.close_pending_tool_calls(
                            reason=err_desc or "aed_recovery",
                            tool_completed=True,
                        )

                    agent._set_state(AgentState.STUCK, reason=f"AED attempt {aed_attempts}: {err_desc}")
                    agent._log("aed_attempt", attempt=aed_attempts, error=err_desc)
                    logger.warning(
                        f"[{agent.agent_name}] AED attempt {aed_attempts}/{agent._config.max_aed_attempts}: {err_desc}",
                    )

                    # Surface the provider error to the Task Card as one stable
                    # row (observe-only/fail-open; sanitized, never err_desc).
                    # Terminal truth depends on whether a *real* next recovery
                    # action exists: a viable, not-yet-attempted preset fallback
                    # means the turn is not actually done, so the row must stay
                    # nonterminal/truthful rather than falsely showing "failed"
                    # right before ``_perform_refresh``.  Report here, before the
                    # preset-fallback ``try`` below rebinds ``e``.
                    _can_still_fallback = (
                        not agent._preset_fallback_attempted
                        and agent._can_fallback_preset()
                    )
                    _aed_terminal = (
                        aed_attempts >= agent._config.max_aed_attempts
                        and not _can_still_fallback
                    )
                    _original_provider_exc = e
                    _report_api_error_to_task_card(
                        agent, e,
                        attempt=aed_attempts,
                        max_attempts=agent._config.max_aed_attempts,
                        terminal=_aed_terminal,
                    )

                    if aed_attempts >= agent._config.max_aed_attempts:
                        if _can_still_fallback:
                            agent._preset_fallback_attempted = True
                            agent._log("preset_auto_fallback",
                                      reason=err_desc,
                                      failed_attempts=aed_attempts)
                            # Automatic preset fallback would rewrite the
                            # user-owned init.json. Final v4.2 keeps recovery
                            # truthful and read-only: an agent/human must edit
                            # the config explicitly, then rerun the same reader.
                            agent._log(
                                "preset_auto_fallback_disabled",
                                reason="init_json_is_read_only_to_boot_refresh_reader",
                            )
                            _report_api_error_to_task_card(
                                agent,
                                _original_provider_exc,
                                attempt=aed_attempts,
                                max_attempts=agent._config.max_aed_attempts,
                                terminal=True,
                            )

                        agent._log("aed_exhausted", attempts=aed_attempts, error=err_desc)
                        # lingtai#672: send ONE sanitized, user-visible failure
                        # notice to the originating IM conversation through the
                        # existing telegram tool handler before going ASLEEP, so
                        # the human is not left staring at an unanswered request
                        # (and a typing indicator that never stops). Fail-open.
                        # Publish the observable ASLEEP state explicitly (like
                        # the worker-hang exit) so supervisors/humans see
                        # ASLEEP, not a terminal STUCK, even though the final
                        # guard skips `_set_state` once `_asleep` is set.
                        agent._set_state(
                            AgentState.ASLEEP,
                            reason=f"AED exhausted after {aed_attempts} attempts",
                        )
                        _notify_aed_exhaustion_origin(agent, turn_origin)
                        sleep_state = AgentState.ASLEEP
                        agent._asleep.set()
                        break

                    # Issue #144: compact oversized historical tool results
                    # before rebuilding the session so the replayed history
                    # fits.  Runs after close_pending_tool_calls (above) and
                    # before _rebuild_session so the rebuilt session sees
                    # the already-shrunk wire.  Over-window errors get a
                    # distinct source tag so AED logs make the cause
                    # auditable.
                    _compact_stats = _compact_history_before_retry(
                        agent,
                        source="aed_over_window" if over_window else "aed_deterministic",
                    )

                    # Issue #593: a deterministic 4xx client error only becomes
                    # retryable if the wire actually changes.  When retroactive
                    # compaction spilled nothing, the rebuilt session ships the
                    # same bytes and fails the same way — fail fast instead of
                    # burning the remaining AED budget on guaranteed-to-fail
                    # retries.  (429 and over-window errors are routed elsewhere.)
                    if (
                        not over_window
                        and _is_client_error(e)
                        and (
                            _compact_stats is None
                            or _compact_stats.compacted_blocks == 0
                        )
                    ):
                        agent._log(
                            "aed_client_error_noop",
                            attempts=aed_attempts,
                            error=err_desc[:300],
                        )
                        logger.warning(
                            f"[{agent.agent_name}] AED client error with no wire "
                            f"change ({type(e).__name__}); skipping further retries: "
                            f"{err_desc}",
                        )
                        _report_api_error_to_task_card(
                            agent,
                            _original_provider_exc,
                            attempt=aed_attempts,
                            max_attempts=agent._config.max_aed_attempts,
                            terminal=True,
                        )
                        sleep_state = AgentState.ASLEEP
                        agent._asleep.set()
                        break

                    # Issue #713: an over-window error means the wire itself
                    # is too long, and retroactive compaction is the only
                    # mechanism that can shrink it before the replay.  If the
                    # pass freed nothing, the rebuilt wire is the same size
                    # (the retry prompt only adds tokens), so every remaining
                    # AED attempt is mathematically guaranteed to fail with
                    # the same provider error.  Abort the wake immediately
                    # instead of burning the rest of the AED budget (which
                    # can be configured as high as 99 attempts) on a doomed
                    # retry storm.  A ``None`` result (no live chat / helper
                    # failure) is not proof of zero progress, so only a
                    # positively-observed empty pass aborts.
                    if (
                        over_window
                        and _compact_stats is not None
                        and _compact_stats.compacted_blocks == 0
                    ):
                        agent._log(
                            "aed_zero_progress_abort",
                            attempt=aed_attempts,
                            max_attempts=agent._config.max_aed_attempts,
                            error=err_desc,
                            scanned_blocks=_compact_stats.scanned_blocks,
                        )
                        _report_api_error_to_task_card(
                            agent,
                            _original_provider_exc,
                            attempt=aed_attempts,
                            max_attempts=agent._config.max_aed_attempts,
                            terminal=True,
                        )
                        agent._log(
                            "aed_exhausted", attempts=aed_attempts, error=err_desc
                        )
                        sleep_state = AgentState.ASLEEP
                        agent._asleep.set()
                        break

                    # Rebuild session with current config, preserving history
                    if agent._session.chat is not None:
                        agent._session._rebuild_session(agent._session.chat.interface)

                    # Inject recovery message
                    msg = _prepare_aed_retry_message(agent, err_desc)
                    agent._set_state(AgentState.ACTIVE, reason=f"AED recovery attempt {aed_attempts}")

            if not agent._asleep.is_set():
                agent._set_state(sleep_state)

            if (
                sleep_state == AgentState.IDLE
                and not agent._asleep.is_set()
                and not skip_post_turn_save
                and msg is not None
                and msg.type in _TEXT_MSG_TYPES
            ):
                manager = getattr(agent, "_task_card_manager", None)
                if manager is not None:
                    try:
                        manager.on_completed_work_turn()
                    except Exception as exc:
                        agent._log("task_card_reminder_error", error=str(exc))

            # Issue #83: check for pending notifications only after the
            # state is observably IDLE.  A check while still ACTIVE would
            # take the ACTIVE deferral path, leaving the fingerprint
            # uncommitted with no wake queued until a later heartbeat.
            # At the IDLE boundary, _sync_notifications uses the distinct
            # synthetic notification pair + MSG_TC_WAKE path.
            if sleep_state == AgentState.IDLE and not agent._asleep.is_set():
                try:
                    from ..notifications import (
                        _workdir_key,
                        attention_fingerprint,
                        is_channel_allowed,
                    )
                    store = agent._notification_store
                    workdir = _workdir_key(agent)
                    fp = attention_fingerprint(
                        store,
                        lambda ch: is_channel_allowed(ch, workdir=workdir),
                        workdir,
                    )
                    if fp != agent._notification_fp:
                        notifications = store.snapshot(
                            lambda ch: is_channel_allowed(ch, workdir=workdir)
                        )
                        if notifications:
                            agent._log("idle_notification_check",
                                       sources=list(notifications.keys()))
                            agent._sync_notifications()
                except Exception as notif_err:
                    agent._log("idle_notification_check_error",
                               error=str(notif_err))
            # Issue #655: the post-turn section (chat-history save and
            # auto-insight) sits outside the AED try/except above, so an
            # exception here (e.g. OSError from a full disk during save) would
            # propagate out of _run_loop and silently kill the daemon run-loop
            # thread, leaving the agent unresponsive while status still shows
            # ACTIVE/IDLE. Log and continue — the next turn retries the save.
            try:
                if skip_post_turn_save:
                    agent._log(
                        "chat_history_save_skipped",
                        reason="worker_still_running_interface_unsafe",
                    )
                else:
                    agent._save_chat_history()

                # Auto-insight: fire after N turns
                if not skip_post_turn_save and agent._config.insights_interval > 0:
                    agent._insight_turn_counter += 1
                    if agent._insight_turn_counter >= agent._config.insights_interval:
                        agent._insight_turn_counter = 0
                        from ..i18n import t as _ti
                        agent._run_inquiry(
                            _ti(agent._config.language, "insight.auto_question"),
                            source="auto",
                        )
            except Exception as e:  # noqa: BLE001 — post-turn must never kill the loop
                agent._log(
                    "post_turn_error",
                    exception=type(e).__name__,
                    error=str(e)[:300],
                )
                logger.warning(
                    f"[{agent.agent_name}] post-turn error "
                    f"({type(e).__name__}): {str(e)[:300]}",
                )

            if execution_workspace_token is not None:
                from ..execution_workspace import reset_execution_workspace
                reset_execution_workspace(execution_workspace_token)
            if tool_observer_token is not None:
                from ..turn_events import reset_turn_tool_observer
                reset_turn_tool_observer(tool_observer_token)
                # Close the admission-witness scope with the observer: a scan
                # after this point is a no-op, so a fact never fires after the
                # observer that would deliver it is gone.
                end_admission_witness_scope(agent)
            if permission_broker_token is not None:
                from ..turn_permissions import reset_turn_permission_broker
                reset_turn_permission_broker(permission_broker_token)
            if provider_admission_token is not None:
                from ..provider_admission import clear_provider_admission
                clear_provider_admission(provider_admission_token)
            _settle_correlated_after_turn(
                agent,
                turn_control,
                handler_result,
                terminal_error,
                terminal_failure,
            )

        break


_TEXT_MSG_TYPES = (MSG_REQUEST, MSG_USER_INPUT)
_UNTRUSTED_PROVIDER_ORIGIN_MESSAGE_TYPES = MESSAGE_TYPES - {MSG_CORRELATED_TURN}


def _concat_queued_messages(agent, msg: Message) -> Message:
    """Drain queued same-type text messages and concatenate into one.

    Only consumes additional messages of MSG_REQUEST or MSG_USER_INPUT
    (text-bearing types) — and only when ``msg`` itself is one of those.
    Other message types (notably MSG_TC_WAKE) are put back into the
    inbox so the run loop processes them in their own iteration with
    their own dispatch path. Without this filter, an empty-content
    MSG_TC_WAKE queued behind a MSG_REQUEST would be silently absorbed
    into the merged request, and the tc_inbox drain handler would never
    fire — mail notifications would stay queued indefinitely.

    If nothing same-type is queued, returns the original message
    unchanged. Otherwise, joins all same-type contents with blank lines
    and returns a new merged message.
    """
    if msg.type not in _TEXT_MSG_TYPES:
        return msg

    extra: list[Message] = []
    putback: list[Message] = []
    while True:
        try:
            queued = agent.inbox.get_nowait()
        except queue.Empty:
            break
        if queued.type in _TEXT_MSG_TYPES:
            extra.append(queued)
        else:
            putback.append(queued)

    for held in putback:
        agent.inbox.put_nowait(held)

    if not extra:
        return msg

    all_msgs = [msg] + extra
    parts = [m.content if isinstance(m.content, str) else str(m.content)
             for m in all_msgs]
    merged_content = "\n\n".join(parts)
    merged = _make_message(MSG_REQUEST, msg.sender, merged_content)
    agent._log("messages_concatenated", count=len(all_msgs))
    return merged


def _handle_message(agent, msg: Message) -> dict | None:
    """Route message by type. Subclasses may override for routing."""
    if msg.type in _UNTRUSTED_PROVIDER_ORIGIN_MESSAGE_TYPES:
        # These legacy/involuntary inbox shapes carry no authenticated adapter
        # admission. A constrained outer profile rejects them before any
        # downstream request, continuation, state, or notice handler runs.
        from ..turns import TurnAdmissionError, TurnOrigin, admit_turn_origin

        try:
            admit_turn_origin(agent, TurnOrigin.INTERNAL_EVENT)
        except TurnAdmissionError:
            return None
    if msg.type in (MSG_REQUEST, MSG_USER_INPUT, MSG_CORRELATED_TURN):
        return _handle_request(agent, msg)
    if msg.type == MSG_TC_WAKE:
        _handle_tc_wake(agent, msg)
        return None
    logger.warning(f"[{agent.agent_name}] Unknown message type: {msg.type}")
    return None


# Context-pressure molt reminders are emitted as `_meta.agent_meta.agent_state.context.molt`
# by meta_block.build_meta; the notification channel is kept only for
# post-molt continuation/event signals.
def _check_molt_pressure(agent) -> None:
    """Clear the legacy pressure-warning notification channel.

    Context pressure is current agent state and is now exposed under permanent
    `_meta.agent_meta.agent_state.context.molt` by ``meta_block.build_meta``. It should not
    be a dismissible notification. Post-molt continuation still uses the
    notification system and is handled separately.
    """
    if "context" not in agent._intrinsics:
        return
    from ..notifications import clear

    clear(agent, "molt")


def _turn_boundary_housekeeping(agent) -> None:
    """Run the turn-boundary housekeeping trio.

    Called from every branch at an LLM-round boundary. Timing relative to the
    ``session.send`` differs by path and is deliberately preserved from the
    pre-refactor inline code: the request path (``_handle_request``) runs the
    trio *before* the initial send of the turn, while the continuation paths
    (``_handle_tc_wake`` and the ``_process_response`` tool loop) run it *after*
    a send completes. In all cases the trio fires in this fixed order:

    1. ``_check_molt_pressure`` — clear the legacy pressure-warning channel.
    2. ``_sync_notifications`` — record same-turn notification changes (while
       ACTIVE this defers delivery to the next IDLE boundary).
    3. ``_rescan_large_tool_results`` — retained inert no-op. Large results are
       no longer re-notified at the turn boundary; they are ranked under
       ``_meta.agent_meta.agent_state.current_tool_result_chars`` instead. The call is kept
       so the trio's order and error-handling contract are unchanged.

    Steps 2 and 3 are best-effort and must never abort the turn, so each is
    guarded independently and its failures are logged as structured events
    (``turn_boundary_notification_sync_error`` /
    ``turn_boundary_rescan_error``) instead of being silently swallowed.
    This deliberately excludes the standalone IDLE-boundary notification
    check in ``_run_loop``, which has different control flow and must not be
    folded in here.
    """
    _check_molt_pressure(agent)
    try:
        agent._sync_notifications()
    except Exception as e:
        agent._log("turn_boundary_notification_sync_error", error=str(e))
    try:
        agent._rescan_large_tool_results()
    except Exception as e:
        agent._log("turn_boundary_rescan_error", error=str(e))


def _is_context_molt_call(tc) -> bool:
    """Return True when ``tc`` is ``context(action="molt", ...)``.

    A post-molt notification is published before the ``context.molt`` tool
    result returns.  If that same result batch were active-stamped with the
    notification and committed its fingerprint, the subsequent IDLE boundary
    would see no change and would not inject the synthesized notification +
    ``MSG_TC_WAKE`` continuation.  Only the molt batch needs this deferral:
    later ACTIVE batches may consume the post-molt notification normally, while
    an immediate IDLE boundary will wake from the still-uncommitted file state.

    Reads only ``args["action"]``, the exact public spelling the ``context``
    family advertises and the exact shape the kernel itself synthesizes for a
    forced molt (``tools/context/_molt.py``).  This is a read path over the
    live batch, not a second accepted call shape: nothing in ``context``
    dispatch admits the former ``psyche``/``context_molt`` spellings. A
    ``psyche`` tool still exists, but it is the unrelated read-only routing
    root over pad/lingtai/knowledge/skills — its action set has no ``molt``
    to produce one.
    """
    if getattr(tc, "name", None) != "context":
        return False
    args = getattr(tc, "args", None)
    if not isinstance(args, dict):
        return False
    return args.get("action") == "molt"


def _batch_includes_context_molt(tool_calls) -> bool:
    return any(_is_context_molt_call(tc) for tc in tool_calls or [])


def _handle_request(agent, msg: Message) -> dict:
    """Send request to LLM, process response with tool calls."""
    if is_worker_interface_poisoned(agent):
        from .worker_recovery import request_worker_hang_refresh

        artifact = getattr(agent, "_llm_worker_poison_artifact", None)
        agent._log(
            "request_skipped_poisoned_interface",
            artifact=artifact,
            message_type=getattr(msg, "type", "unknown"),
        )
        request_worker_hang_refresh(
            agent,
            artifact_relpath=artifact,
            source="handle_request_poison_guard",
        )
        return {
            "text": "",
            "failed": True,
            "errors": ["agent interface is unavailable"],
        }

    # Splice any queued involuntary tool-call pairs
    agent._drain_tc_inbox()

    max_calls, dup_free, dup_hard = _get_guard_limits(agent)
    guard = LoopGuard(
        max_total_calls=max_calls,
        dup_free_passes=dup_free,
        dup_hard_block=dup_hard,
    )
    agent._executor = _make_tool_executor(agent, guard)
    content = agent._pre_request(msg)
    # If a prior worker hang left an open recovery artifact, prepend one
    # concise recovery notice to this first safe request (once per artifact).
    try:
        from .worker_recovery import maybe_prepend_worker_hang_recovery_prompt

        content = maybe_prepend_worker_hang_recovery_prompt(agent, content)
    except Exception:
        pass
    meta = build_meta(agent)

    # Turn-boundary housekeeping: molt pressure + notification sync +
    # large-result rescan. On the request path this runs *before* the initial
    # send (preserved pre-refactor timing); see _turn_boundary_housekeeping.
    _turn_boundary_housekeeping(agent)

    prefix = render_meta(agent, meta)
    if prefix:
        content = f"{prefix}\n\n{content}"
    agent._log("text_input", text=content)
    response = agent._session.send(content)
    # Settle point: the initial send returned; any receipt-bearing result
    # spliced onto the wire during it (e.g. a request-start inbox drain) is now
    # durably present.
    scan_and_emit_committed_facts(agent)
    agent._last_usage = response.usage
    agent._save_chat_history()
    try:
        result = _process_response(agent, response)
        agent._post_request(msg, result)
    finally:
        teardown = getattr(agent, "_teardown_telegram_task_card", None)
        if teardown is not None:
            teardown()
    return result


def _handle_tc_wake(agent, msg: Message) -> None:
    """Drive one inference round off the existing wire, no append.

    Post-`.notification/`-redesign contract: the run loop receives this
    message after ``_sync_notifications`` has already spliced a
    synthesized ``(ToolCallBlock, ToolResultBlock)`` pair into the
    canonical interface (impersonating a voluntary
    ``notification(action="check")`` call from the agent's
    perspective).  This handler's job is to drive the next inference
    round off that wire — no fake user message, no meta prefix.  From
    the LLM's viewpoint it is indistinguishable from the agent having
    voluntarily called the tool itself.

    The legacy ``tc_inbox`` queue is still drained at the top for
    back-compat (in case anything outside the kernel still enqueues),
    but the empty-queue path now routes to the wire-drive path instead
    of no-op-and-return — the previous "tc_inbox_empty" silent
    no-op was the bug that left spliced notification pairs unread.
    """
    try:
        if is_worker_interface_poisoned(agent):
            from .worker_recovery import request_worker_hang_refresh

            artifact = getattr(agent, "_llm_worker_poison_artifact", None)
            agent._log("tc_wake_skipped_poisoned_interface", artifact=artifact)
            request_worker_hang_refresh(
                agent,
                artifact_relpath=artifact,
                source="tc_wake_poison_guard",
            )
            return

        if agent._chat is None:
            try:
                agent._session.ensure_session()
            except Exception as e:
                agent._log(
                    "tc_wake_noop",
                    reason="ensure_session_failed",
                    error=str(e)[:300],
                )
                return

        iface = agent._chat.interface
        items = agent._tc_inbox.drain()

        # Mid-pair tail — defer.  Re-enqueue any drained legacy items so
        # the next wake retries them.
        if iface.has_pending_tool_calls():
            for item in items:
                agent._tc_inbox.enqueue(item)
            agent._log(
                "tc_wake_noop",
                reason="pending_tool_calls",
                **_pending_tool_call_summary(iface),
            )
            return

        agent._executor = _make_tool_executor(
            agent,
            LoopGuard(
                max_total_calls=_get_guard_limits(agent)[0],
                dup_free_passes=2,
                dup_hard_block=8,
            ),
        )

        # Legacy tc_inbox path — drained items get spliced and driven the
        # old way (call appended here, result passed through send).  Empty
        # in production post-redesign; preserved for back-compat.
        for idx, item in enumerate(items):
            try:
                if getattr(item, "replace_in_history", False):
                    prior_id = agent._appendix_ids_by_source.get(item.source)
                    if prior_id is not None:
                        iface.remove_pair_by_call_id(prior_id)
                    agent._appendix_ids_by_source.pop(item.source, None)
                iface.add_assistant_message(content=[item.call])
                if getattr(item, "replace_in_history", False):
                    agent._appendix_ids_by_source[item.source] = item.call.id
                agent._save_chat_history()

                agent._log("tc_wake_dispatch", source=item.source, call_id=item.call.id)
                try:
                    response = agent._session.send([item.result])
                    # Settle point: send returned.
                    scan_and_emit_committed_facts(agent)
                except Exception as send_err:
                    from ..llm_utils import WorkerStillRunningError

                    # Worker still alive — the interface is unsafe to touch.
                    # Re-raise before the restore/heal path mutates it; the run
                    # loop's central branch poisons and requests refresh.
                    if isinstance(send_err, WorkerStillRunningError):
                        raise
                    # The spliced tool result was passed into send() and the
                    # adapter rolled the user entry back when the API call
                    # failed. Restore the real result before the catch-all
                    # below synthesizes a placeholder — without this, the
                    # original notification payload is permanently replaced by
                    # the kernel notice and the agent has no way to recover
                    # the message that was on the wire (issue #170).
                    _restore_tool_results_after_continuation_failure(
                        agent, [item.result], ledger_source="tc_wake",
                    )
                    # Settle point: send's exception handling (incl. any
                    # rollback/restore) has completed.  A survivor still on the
                    # wire (adapter did not roll back) fires here; a rolled-back
                    # entry is absent and does not.
                    scan_and_emit_committed_facts(agent)
                    raise
                agent._last_usage = response.usage
                agent._save_chat_history(ledger_source="tc_wake")
                _process_response(agent, response, ledger_source="tc_wake")
            except Exception as splice_err:
                from ..llm_utils import WorkerStillRunningError

                if isinstance(splice_err, WorkerStillRunningError):
                    # Interface poisoned — do not inspect/heal/save it. Re-queue
                    # the remaining items and re-raise to the run loop.
                    agent._log(
                        "tc_wake_send_error",
                        source=item.source,
                        call_id=item.call.id,
                        error=str(splice_err)[:300],
                        worker_still_running=True,
                    )
                    for remaining in items[idx + 1:]:
                        agent._tc_inbox.enqueue(remaining)
                    raise
                if iface.has_pending_tool_calls():
                    # tool_completed=True: the tool result was produced by the
                    # notification system and passed in as item.result — the
                    # failure is in the LLM round-trip that followed.
                    iface.close_pending_tool_calls(
                        reason=f"tc_wake splice failed: {str(splice_err)[:200]}",
                        tool_completed=True,
                    )
                    agent._save_chat_history()
                agent._log(
                    "tc_wake_send_error",
                    source=item.source,
                    call_id=item.call.id,
                    error=str(splice_err)[:300],
                )
                for remaining in items[idx + 1:]:
                    agent._tc_inbox.enqueue(remaining)
                raise

        # Wire-drive path: notification sync (or anything else that
        # appends a complete (call, result) pair before posting
        # MSG_TC_WAKE) leaves the wire ready for inference.  Drive one
        # round off the existing state — pass None as the message so the
        # adapter knows to skip the input-append step.
        #
        # Guard against stale wakes: only drive the wire when the tail is
        # a user entry carrying ToolResultBlock(s).  Anything else (empty
        # interface, tail is assistant text, etc.) means there's nothing
        # for the LLM to respond to — sending would either error or
        # produce a redundant continuation.
        from ..llm.interface import ToolResultBlock

        entries = iface.entries
        tail_is_tool_result = (
            bool(entries)
            and entries[-1].role == "user"
            and any(isinstance(b, ToolResultBlock) for b in entries[-1].content)
        )
        if not tail_is_tool_result:
            agent._log("tc_wake_noop", reason="wire_not_ready")
            return

        try:
            agent._log("tc_wake_continue")
            response = agent._session.send(None)
            # Settle point: send returned.
            scan_and_emit_committed_facts(agent)
            agent._last_usage = response.usage
            agent._save_chat_history(ledger_source="tc_wake")
            _process_response(agent, response, ledger_source="tc_wake")
            # Notification-driven turns also run turn-boundary housekeeping so molt
            # pressure / notification sync / large-result rescan fire even when the
            # agent is woken by mail/soul (see _turn_boundary_housekeeping).
            _turn_boundary_housekeeping(agent)
        except Exception as e:
            from ..llm_utils import WorkerStillRunningError

            if isinstance(e, WorkerStillRunningError):
                # Interface poisoned — the worker may still be mutating it. Do
                # not inspect/heal/save; re-raise to the run loop's central
                # WorkerStillRunning branch which poisons and requests refresh.
                agent._log(
                    "tc_wake_error",
                    error=str(e)[:300],
                    worker_still_running=True,
                )
                raise
            if iface.has_pending_tool_calls():
                # tool_completed=True: the wire-drive path only fires when the
                # tail is already user[ToolResultBlock] — the tool results
                # were committed and the adapter reverted them after the
                # LLM continuation failed.
                iface.close_pending_tool_calls(
                    reason=f"tc_wake continue heal: {str(e)[:200]}",
                    tool_completed=True,
                )
                agent._save_chat_history()
            # Settle point: send(None)'s exception handling has completed.  If
            # the API call failed with no rollback, the pre-existing tool-result
            # tail survived on the wire and fires here (once); heal only appends
            # synthesized results, which carry no receipt.
            scan_and_emit_committed_facts(agent)
            fields = {"error": str(e)[:300]}
            if isinstance(e, EmptyLLMResponseError):
                fields.update(e.diagnostic_fields())
            agent._log("tc_wake_error", **fields)
            raise
    finally:
        teardown = getattr(agent, "_teardown_telegram_task_card", None)
        if teardown is not None:
            teardown()


def _get_guard_limits(agent) -> tuple[int, int, int]:
    """Return (max_total_calls, dup_free_passes, dup_hard_block).

    The total-call ceiling is a kernel-owned ACTIVE-turn emergency fuse. It is
    intentionally not read from ``manifest.max_turns`` / ``AgentConfig.max_turns``
    so stale init.json files cannot make the runtime harsher or looser.
    """
    return (ACTIVE_TURN_TOOL_CALL_EMERGENCY_LIMIT, 3, 8)


def _make_tool_executor(agent, guard: LoopGuard) -> ToolExecutor:
    """Construct the per-turn ``ToolExecutor`` with the shared wiring.

    Every turn path wires the executor identically — dispatch fn, provider-aware
    tool-result factory, known/parallel-safe tool sets, logger, meta fn, working
    dir, and summarize threshold. Only the ``LoopGuard`` differs between paths
    (e.g. ``dup_free_passes`` 3 for fresh requests vs 2 for tc-wake
    continuations), so the caller supplies it.
    """
    return ToolExecutor(
        dispatch_fn=agent._dispatch_tool,
        make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
            name, result, provider=agent._config.provider, **kw
        ),
        guard=guard,
        known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
        parallel_safe_tools=agent._PARALLEL_SAFE_TOOLS,
        logger_fn=agent._log,
        meta_fn=lambda: build_meta(agent),
        working_dir=agent._working_dir,
        tool_call_guard=ToolCallGuard([
            build_risky_action_check(agent._working_dir),
            broker_permission_check,
        ]),
        summarize_notification_threshold=getattr(
            agent, "_summarize_notification_threshold", None
        ),
        reconstruction_event_fn=lambda: build_reconstruction_tool_meta(agent),
        summarizer_fn=_build_apriori_summarizer_fn(agent),
    )


def _record_apriori_summary_usage(agent, response, tool_name, tool_call_id) -> None:
    """Append the a-priori summarizer call's token usage to the MAIN ledger.

    The one-shot summarizer session is created with ``tracked=False`` and is
    driven outside ``BaseAgent``'s main-loop ``session.send`` hook, so its
    ``usage`` would otherwise be invisible to the agent's lifetime totals / cost
    analytics. We attribute it with ``source="summarize_apriori"`` (see
    ``APRIORI_SUMMARY_LEDGER_SOURCE``), plus ``tool_name``/``tool_call_id`` so
    the row is correlatable with the durable tool_result event. This mirrors the
    soul one-shot accounting in ``intrinsics/soul/consultation._write_soul_tokens``.

    Fail-open on *accounting*: a ledger write failure must never break the
    summary path (content-side fail-closed is handled by the orchestrator),
    so all of this is wrapped in try/except — mirroring the main-loop hook in
    ``base_agent/__init__.py``. Only the five safe codex-pool attribution
    fields are projected from ``usage.extra``; arbitrary provider metadata is
    omitted.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        working_dir = getattr(agent, "_working_dir", None)
        if working_dir is None:
            return
        service = getattr(agent, "service", None)
        ledger_path = working_dir / "logs" / "token_ledger.jsonl"
        model = getattr(service, "model", None)
        endpoint = getattr(service, "_base_url", None)
        append_token_entry(
            ledger_path,
            input=getattr(usage, "input_tokens", 0),
            output=getattr(usage, "output_tokens", 0),
            thinking=getattr(usage, "thinking_tokens", 0),
            cached=getattr(usage, "cached_tokens", 0),
            model=model,
            endpoint=endpoint,
            extra={
                "source": APRIORI_SUMMARY_LEDGER_SOURCE,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "apriori_tool_result_summary": True,
                **safe_codex_pool_usage_extra(getattr(usage, "extra", None)),
            },
        )
    except Exception as e:  # accounting must never break the summary path
        name = getattr(agent, "agent_name", "?")
        logger.warning(
            f"[{name}] Failed to append a-priori summary token ledger: {e}"
        )


def _build_apriori_summarizer_fn(agent):
    """Build the one-shot a-priori (``summary=true``) summarizer closure.

    Returns ``None`` when the agent's service has no usable one-shot session
    gateway. (The orchestrator then *fails closed* to a summary-layer error
    rather than dumping the raw into context — see ``maybe_summarize_result``.)
    The closure signature is
    ``(system_prompt, user_prompt, tool_name, tool_call_id) -> str``; it returns
    the model's text and records the call's token usage on the MAIN agent's
    ledger. Errors propagate to the caller, which fails closed to a summary-layer
    error (never leaking the raw payload).

    Why ``create_session().send()`` and not ``service.generate()``:
    ``generate`` routes through the adapter's one-shot path
    (``chat.completions.create`` for the OpenAI-family adapters). On the
    Codex/ChatGPT-OAuth provider that path targets
    ``/backend-api/codex/chat/completions``, which is not a served Codex backend
    endpoint and returns a Cloudflare challenge → ``PermissionDeniedError``
    (observed live on PR #586). The supported one-shot path on this provider is
    the same Responses session the main agent uses: ``create_session(...)`` (which
    builds a ``CodexResponsesSession``) followed by ``session.send(...)``. This is
    exactly how the kernel's other internal one-shot calls work — see
    ``intrinsics/soul/inquiry.soul_inquiry`` and
    ``intrinsics/soul/consultation``. Using it here makes the a-priori summary
    work on every provider the main agent itself works on.
    """
    service = getattr(agent, "service", None)
    if service is None or not callable(getattr(service, "create_session", None)):
        return None

    def _summarize(
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_call_id: str | None = None,
    ) -> str:
        # Untracked one-shot session, no tools, scoped to the agent's own
        # provider/model so it rides the same supported transport as the main
        # agent. No ``interface`` → a fresh, empty conversation (the untrusted
        # tool output and reason are carried entirely in ``user_prompt``).
        config = getattr(agent, "_config", None)
        provider = getattr(config, "provider", None)
        model = getattr(config, "model", None) or getattr(service, "model", None)
        session = service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=model,
            tracked=False,
            provider=provider,
        )
        response = session.send(user_prompt)
        _record_apriori_summary_usage(agent, response, tool_name, tool_call_id)
        return getattr(response, "text", "") or ""

    return _summarize


def _check_external_send(agent, tool_calls, tool_results=None) -> None:
    """Record external sends and warn on duplicates.

    Scans the just-executed batch for send/reply actions on external
    channel tools (telegram, imap, wechat, feishu). Records each send
    in the tracker for dedup. When a duplicate is detected, appends a
    warning to the corresponding tool result.
    """
    tracker = agent._sent_tracker
    for tc in tool_calls:
        if tc.name not in SEND_TOOLS:
            continue
        args = tc.args or {}
        action = args.get("action", "")
        if action in SEND_ACTIONS:
            content = args.get("message", "") or args.get("body", "") or args.get("text", "")
            recipient = args.get("to", "") or args.get("chat_id", "") or args.get("address", "")
            if content and recipient:
                if tracker.was_recently_sent(content, recipient):
                    agent._log(
                        "send_dedup_detected",
                        tool=tc.name,
                        recipient=recipient,
                    )
                    if tool_results:
                        warning = (
                            "Recently sent similar message to this recipient."
                            " This send already executed; avoid sending it again."
                        )
                        for tr in tool_results:
                            if tr.id == tc.id:
                                if isinstance(tr.content, dict):
                                    # ToolResultBlock.content is Any (str or dict);
                                    # dict + str raises TypeError. Attach as a
                                    # structured field instead — adapters render
                                    # the whole dict to the LLM as JSON.
                                    tr.content["_advisory"] = {
                                        "type": "duplicate_send",
                                        "severity": "warning",
                                        "allowed": True,
                                        "blocked": False,
                                        "advisory_only": True,
                                        "message": warning,
                                        "skill_refs": ["system-manual"],
                                    }
                                else:
                                    tr.content = (
                                        (tr.content or "")
                                        + f"\n⚠️ {warning}"
                                    )
                                break
                    continue
                tracker.record_sent(content, recipient, tc.name)
                agent._log(
                    "external_send_detected",
                    tool=tc.name,
                    action=action,
                    recipient=recipient,
                )


def _check_poll_backoff(agent, tool_calls, tool_results=None) -> bool:
    """Check if polling actions should trigger idle-after-backoff.

    Counts consecutive check/read calls on external channel tools within
    the same turn. After ``max_poll_retries`` consecutive checks on the
    same channel, returns True to signal the agent should go IDLE.

    The counter resets when a send action occurs, or when a check/read
    action actually returns messages (found_new=True).
    """
    # Build a lookup from tool-call id to result for found-new detection.
    # ToolResultBlock stores the correlated tool-call id on `.id` (the same
    # field name as ToolCallBlock), not on a separate `.tool_call_id`.
    result_by_tc_id: dict = {}
    if tool_results:
        for tr in tool_results:
            tc_id = getattr(tr, "id", None)
            if tc_id:
                result_by_tc_id[tc_id] = tr

    tracker = agent._sent_tracker
    should_idle = False
    for tc in tool_calls:
        if tc.name not in SEND_TOOLS:
            continue
        args = tc.args or {}
        action = args.get("action", "")
        if action in SEND_ACTIONS:
            # Send resets the poll counter for this channel.
            tracker.reset_poll(tc.name)
            continue
        if action not in CHECK_ACTIONS:
            continue
        # Check if this read/check actually returned items. Different
        # providers use different keys: imap→"emails", telegram/wechat→
        # "messages", feishu→"conversations" (check) or "messages" (read).
        found_new = False
        tr = result_by_tc_id.get(tc.id)
        if tr:
            content = getattr(tr, "content", None)
            payload = None
            if isinstance(content, dict):
                payload = content
            elif isinstance(content, str):
                try:
                    payload = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    payload = None
            if isinstance(payload, dict):
                for key in ("messages", "emails", "conversations"):
                    if payload.get(key):
                        found_new = True
                        break
        # Record the poll attempt with found_new status.
        tracker.record_poll(tc.name, found_new=found_new)
        if tracker.should_stop_polling(tc.name):
            agent._log(
                "poll_backoff_exhausted",
                tool=tc.name,
                action=action,
                poll_count=tracker._poll_counts.get(tc.name, 0),
            )
            should_idle = True
    return should_idle


def _process_response(agent, response, *, ledger_source: str = "main") -> dict:
    """Handle tool calls and collect text output.

    Returns a result dict: {"text": ..., "failed": ..., "errors": [...]}.

    ``ledger_source`` propagates to ``_save_chat_history`` for any
    tool-loop continuation LLM round-trips.
    """
    guard = agent._executor.guard
    collected_text_parts: list[str] = []
    collected_errors: list[str] = []
    in_tool_loop = False

    while True:
        # Empty-response guard: text + tool_calls + thoughts all empty means
        # the LLM produced nothing useful. Without this check, the loop would
        # break on `not response.tool_calls` and return success, abandoning
        # any in-progress task. Route into AED instead — a session rebuild
        # plus stuck_revive injection is the right recovery for a degenerate
        # response (often caused by heavy context or mid-loop notification
        # injection confusing the model).
        if is_all_empty_response(response):
            # Extract diagnostic metadata from provider response.
            raw = response.raw
            _diag: dict = {}
            if raw is not None:
                _diag["response_id"] = getattr(raw, "id", None)
                _diag["response_model"] = getattr(raw, "model", None)
                choices = getattr(raw, "choices", None)
                if choices:
                    _diag["finish_reason"] = getattr(
                        choices[0], "finish_reason", None
                    )
            usage = getattr(response, "usage", None)
            agent._log(
                "empty_llm_response",
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                thinking_tokens=getattr(usage, "thinking_tokens", 0) or 0,
                api_call_id=getattr(response, "api_call_id", None),
                **_diag,
            )
            raise EmptyLLMResponseError(
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                response_id=_diag.get("response_id"),
                response_model=_diag.get("response_model"),
                finish_reason=_diag.get("finish_reason"),
                api_call_id=getattr(response, "api_call_id", None),
            )

        if response.text:
            collected_text_parts.append(response.text)
            # Preserve the provider round boundary for the Telegram-owned
            # Task Card projection.  Only canonical response text is carried;
            # thoughts remain a separate hidden event and are never projected.
            agent._log(
                "diary",
                text=response.text,
                api_call_id=getattr(response, "api_call_id", None),
            )
            if response.tool_calls:
                agent._intermediate_text_streamed = False

        if response.thoughts:
            for thought in response.thoughts:
                agent._log("thinking", text=thought)

        if not response.tool_calls:
            break

        tool_call_fields = _tool_call_summary(response.tool_calls)
        if agent._cancel_event.is_set():
            agent._log(
                "tool_calls_not_dispatched",
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                reason="cancel_event",
                **tool_call_fields,
            )
            return {"text": "", "failed": False, "errors": []}

        stop_reason = guard.check_limit(len(response.tool_calls))
        if stop_reason:
            return _handle_guarded_non_dispatch(
                agent,
                response.tool_calls,
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                reason="tool_loop_limit",
                detail=stop_reason,
                detail_field="stop_reason",
                collected_text_parts=collected_text_parts,
            )

        invalid_reason = guard.check_invalid_tool_limit()
        if invalid_reason:
            return _handle_guarded_non_dispatch(
                agent,
                response.tool_calls,
                ledger_source=ledger_source,
                in_tool_loop=in_tool_loop,
                reason="invalid_tool_limit",
                detail=invalid_reason,
                detail_field="invalid_reason",
                collected_text_parts=collected_text_parts,
            )

        # Count this batch before execution so every model-visible tool result
        # can carry the post-batch ACTIVE-turn progress meter.
        guard.record_calls(len(response.tool_calls))

        # Delegate to ToolExecutor.  The progress notice is batch-scoped: it is
        # visible on this batch's tool results, then cleared before the next LLM
        # response is processed.
        try:
            tool_results, intercepted, intercept_text = agent._executor.execute(
                response.tool_calls,
                api_call_id=getattr(response, "api_call_id", None),
                on_result_hook=agent._on_tool_result_hook,
                on_pre_dispatch_hook=getattr(
                    agent, "_on_tool_pre_dispatch_hook", None
                ),
                cancel_event=agent._cancel_event,
                collected_errors=collected_errors,
            )
        finally:
            guard.clear_progress_notice()

        # Attach the current notification payload to the latest tool-result
        # dict from this batch when the material payload changes. The previous
        # LIVE holder is released from tracking first: both synthesized pairs
        # and normal tool results simply stop being tracked as live and keep
        # their recorded content unchanged (append-only — see
        # ``meta_block.skeletonize_notification_holder``). Model-facing
        # serialization keeps those historical copies instead of rewriting
        # canonical history; only the latest holder per family is current
        # state.
        if _batch_includes_context_molt(response.tool_calls):
            # ``context(action='molt')`` (``tools/context/_molt.py``) publishes
            # ``.notification/post-molt.json`` before
            # its own tool result returns.  Do not let that same result batch
            # consume the notification or commit its fingerprint; otherwise the
            # post-turn IDLE sync would see no change and skip the wake.  Leaving
            # the fingerprint untouched lets the next boundary choose naturally:
            # IDLE/ASLEEP injects synthesized notification + MSG_TC_WAKE; a later
            # ACTIVE tool-result batch stamps the notification normally.
            if agent._notification_live_holder is not None:
                from ..meta_block import skeletonize_notification_holder
                skeletonize_notification_holder(agent)
        else:
            _prior_holder = agent._notification_live_holder
            agent._notification_live_holder = attach_active_notifications(
                agent,
                tool_results,
                prior_holder=_prior_holder,
            )

        # Attach the resident Task Card (change-gated) onto the same final
        # agent_meta carrier. Runs after notifications so both axes coexist.
        try:
            agent._taskcard_live_holder = attach_active_taskcard(
                agent,
                tool_results,
                prior_holder=getattr(agent, "_taskcard_live_holder", None),
            )
        except Exception:
            agent._log(
                "taskcard_block_attach_failed",
                reason="attach_active_taskcard raised",
            )

        # Attach the complete `_meta.agent_meta` snapshot to the designated final
        # ToolResultBlock whenever private capture exists. The compatibility
        # signature may describe material changes, but never gates emission;
        # unchanged runtime state is still carried by the newest final block.
        # Older blocks retain their snapshots as historical traces.
        # Unlike notifications there is no molt-race special case: these are pure
        # per-turn snapshots, not kernel-synchronized channel state.
        #
        # MUST run before _log_notification_block_injected below: the durable
        # snapshot copies the holder's full ``_meta`` envelope, and
        # ``attach_active_runtime`` is what populates ``_meta.agent_meta`` /
        # ``_meta.agent_meta.guidance`` on that holder.  Logging before this ran would
        # persist rows missing those two blocks.
        try:
            agent._runtime_live_holder = attach_active_runtime(
                agent,
                tool_results,
                prior_holder=getattr(agent, "_runtime_live_holder", None),
            )
        except Exception:
            agent._log(
                "runtime_block_attach_failed",
                reason="attach_active_runtime raised",
            )

        # Canonicalize the completed batch exactly once.  The sidecar is the
        # only runtime transport; handler payloads remain untouched.
        finalize_two_axis_sidecars(tool_results)

        # Log the actual canonical ``_meta`` envelope that was stamped onto the
        # tool result so the TUI /notification command can show real snapshots.
        # Only log when a genuinely new notification holder was established
        # (changed and not None), i.e. when notification stamping actually
        # happened this batch.  Runs after attach_active_runtime so the persisted
        # ``_meta`` carries the full envelope (tool_meta/agent_meta/guidance/
        # notifications/notification_guidance).
        if not _batch_includes_context_molt(response.tool_calls):
            _new_holder = agent._notification_live_holder
            _new_meta = (
                getattr(_new_holder, "metadata", None)
                if _new_holder is not None else None
            )
            if (
                _new_holder is not None
                and _new_holder is not _prior_holder
                and isinstance(_new_meta, dict)
                and "notifications" in _new_meta.get("agent_meta", {})
            ):
                try:
                    _carrier_call_id = ""
                    for _result in tool_results:
                        if _result is _new_holder:
                            _carrier_call_id = str(getattr(_result, "id", "") or "")
                            break
                    agent._log_notification_block_injected(
                        _new_meta,
                        mode="active_tool_result",
                        call_id=_carrier_call_id,
                    )
                except Exception:
                    pass

        if intercepted:
            if tool_results and agent._chat:
                agent._chat.commit_tool_results(tool_results)
                # Settle point: no-API terminal commit returned.
                scan_and_emit_committed_facts(agent)
            return {
                "text": intercept_text,
                "failed": False,
                "errors": [],
            }

        # Mid-batch cancel: a tool we just ran (e.g. system(action="sleep"))
        # set _cancel_event, meaning the agent has decided to stop this
        # turn. Commit the tool_results to the wire so the assistant turn
        # we just sent has matching pairs (no dangling tool_calls), then
        # return without re-sending to the LLM. Without this, the loop
        # would call agent._session.send(tool_results) below, get back a
        # new assistant response with NEW tool_calls, save those to the
        # wire — and then the cancel check at the top of the next
        # iteration would return, leaving those new tool_calls dangling.
        # That broken wire then blocks all future notification injects.
        if agent._cancel_event.is_set():
            if tool_results and agent._chat:
                agent._chat.commit_tool_results(tool_results)
                # Settle point: no-API terminal commit returned.
                scan_and_emit_committed_facts(agent)
            agent._log("turn_cancelled_post_tool",
                       reason="cancel_event_set_after_tool_execute")
            return {"text": "", "failed": False, "errors": []}

        # Issue #63: dedup check — warn agent if it just re-sent
        # a duplicate message to an external channel.
        _check_external_send(agent, response.tool_calls, tool_results)

        # Issue #63: poll backoff — if the agent is repeatedly checking
        # for new messages without finding any, go IDLE after max retries.
        if _check_poll_backoff(agent, response.tool_calls, tool_results):
            if tool_results and agent._chat:
                agent._chat.commit_tool_results(tool_results)
                # Settle point: no-API terminal commit returned.
                scan_and_emit_committed_facts(agent)
                # Issue #126: save immediately so the in-memory and on-disk
                # interface agree that tool results are committed. Without
                # this, a notification heartbeat tick between the return
                # and the post-turn save in _run_loop can see a stale wire
                # and heal the (already-committed) tool calls.
                agent._save_chat_history(ledger_source=ledger_source)
                _close_pending_tool_calls_after_poll_backoff(
                    agent,
                    ledger_source=ledger_source,
                )
            agent._log("idle_after_poll_backoff",
                       reason="poll_retries_exhausted")
            return {
                "text": "\n".join(collected_text_parts),
                "failed": False,
                "errors": [],
            }

        in_tool_loop = True
        try:
            response = agent._session.send(tool_results)
            # Settle point: the carrying send returned; committed tool results
            # (id > watermark) are durably on the wire.
            scan_and_emit_committed_facts(agent)
        except Exception as _continuation_exc:
            # The local tools have already executed and returned results; only
            # the post-tool LLM continuation failed. Some adapters append tool
            # results as part of send(tool_results) and roll that user entry
            # back on provider error. If AED / notification heal sees the tail
            # assistant tool_calls as unanswered, it can only synthesize a
            # completion notice and the real result payload is lost. Restore the
            # real results before re-raising so recovery paths preserve truthful
            # tool completion state.
            _restore_tool_results_after_continuation_failure(
                agent, tool_results, ledger_source=ledger_source,
            )
            # Settle point: the carrying send's exception handling (incl. any
            # rollback/restore) has completed.  A survivor still on the wire
            # (e.g. a drained pair shielded from drop_trailing, or results the
            # restore re-committed) fires here exactly once; a rolled-back entry
            # is absent and does not fire.
            scan_and_emit_committed_facts(agent)
            # Surface the API error to the *still-live* Task Card here: the outer
            # AED catch runs only after ``_handle_request``'s finally has torn
            # down the card context, so its report would no-op. Observe-only and
            # fail-open — the exception is re-raised unchanged for AED/retry/
            # fallback, and the stable-row upsert keeps a later AED report (if a
            # context is somehow still live) idempotent. No attempt number is
            # known yet, so it reports as a truthful "retrying" without n/N.
            _report_api_error_to_task_card(agent, _continuation_exc, terminal=False)
            raise
        agent._last_usage = response.usage
        agent._save_chat_history(ledger_source=ledger_source)

        # Mid-loop turn-boundary housekeeping. Context pressure is now surfaced
        # on every tool result under _meta.agent_meta.agent_state.context.molt
        # (meta_block.build_meta), so _check_molt_pressure here only clears any
        # stale legacy molt.json; the rescan keeps summarize reminders in sync
        # across tool-loop LLM rounds, not only at request/notification-wake
        # boundaries (see _turn_boundary_housekeeping).
        _turn_boundary_housekeeping(agent)

    final_text = "\n".join(collected_text_parts)
    has_errors = bool(collected_errors)
    no_useful_output = not final_text.strip()
    return {
        "text": final_text,
        "failed": has_errors and no_useful_output,
        "errors": collected_errors,
    }
