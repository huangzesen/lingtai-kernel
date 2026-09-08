"""Pure projection of canonical agent events into bounded Task Card text."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from lingtai.kernel.state import AgentState
from lingtai.kernel.trace_redaction import redact_text


_ASYNC_STATUS_KEYS = ("running", "done", "failed", "cancelled", "timeout", "unknown")


class TaskCardEventProjection:
    """Shared, transport-free event grouping, redaction, and rendering core."""

    REASONING_CAP = 500
    TEXT_LIMIT = 3500
    # English is the canonical default surface; ``zh`` is an opt-in per-agent
    # locale (set via ``/taskcard lang zh|en``), never a hard-coded channel
    # behavior. The class attributes stay the English surface for backward
    # compatibility; render paths resolve text through the locale helpers below
    # (Jason 2026-08-10: revert #1209 hard-coded Chinese, make it configurable).
    HEADER = "📋 ACTIVITIES"
    FOOTER = (
        "Don't reply to this Task Card. Use /taskcard on|off to toggle; "
        "/taskcard N sets normal rows (1-10"
    )
    DEFAULT_NORMAL_ROWS = 1
    METADATA_MAX_CHARS = 500
    TIME_PREFIX = "Last Updated: "
    AGENT_STATES = frozenset(state.value for state in AgentState)

    SUPPORTED_LOCALES = frozenset({"en", "zh"})
    _LOCALE_TEXTS: dict[str, dict[str, str]] = {
        "en": {
            "header": "📋 ACTIVITIES",
            "time_prefix": "Last Updated: ",
            "ask_agent": "Ask agent for \"Task Card\"",
            "footer_prefix": (
                "Don't reply to this Task Card. Use /taskcard on|off to toggle; "
                "/taskcard N sets normal rows (1-10"
            ),
            "footer_current": "current",
            "try_refresh": "try /refresh",
            "api_error": "API error",
            "recovered": "recovered",
            "failed": "failed",
            "retrying": "retrying",
            "retrying_attempt": "retrying (attempt {n})",
            "session": "Session",
            "identity": "Identity",
            "async_work": "Async Work",
            "daemons": "Daemons",
            "backends": "Backends",
            "shell": "Shell",
            "daemon_stats": "Daemon stats",
        },
        "zh": {
            "header": "📋 活动",
            "time_prefix": "最后更新: ",
            "ask_agent": "向 agent 询问 \"Task Card\"",
            "footer_prefix": (
                "请勿回复此任务卡片。使用 /taskcard on|off 切换；"
                "/taskcard N 设置显示组数 (1-10"
            ),
            "footer_current": "当前",
            "try_refresh": "尝试 /refresh",
            "api_error": "API 错误",
            "recovered": "已恢复",
            "failed": "失败",
            "retrying": "重试中",
            "retrying_attempt": "重试中 (第 {n} 次)",
            "session": "会话",
            "identity": "身份",
            "async_work": "异步工作",
            "daemons": "守护进程",
            "backends": "后端",
            "shell": "Shell",
            "daemon_stats": "守护进程统计",
        },
    }

    @classmethod
    def normalize_locale(cls, locale: object) -> str:
        """Coerce a locale to the supported set; unknown values fall back to en."""
        if isinstance(locale, str) and locale in cls.SUPPORTED_LOCALES:
            return locale
        return "en"

    @classmethod
    def header(cls, locale: str = "en") -> str:
        return cls._locale_text("header", locale)

    @classmethod
    def time_prefix(cls, locale: str = "en") -> str:
        return cls._locale_text("time_prefix", locale)

    @classmethod
    def _locale_text(cls, key: str, locale: str = "en") -> str:
        return cls._LOCALE_TEXTS[cls.normalize_locale(locale)][key]

    EVENT_WINDOW = 10
    EVENT_REASONING_CAP = 300
    EVENT_TEXT_CAP = 500
    MAX_EVENTS_PER_CALL = 24
    MAX_ELAPSED_MS = 9_999_999
    # One symmetric dash run on each side of the per-API-call wall-clock stamp:
    # the divider becomes a log-style section header `──── 00:22:02 U-7 ────`
    # (Jason 2026-08-09).
    API_CALL_DIVIDER = "────"
    # Section divider on the resident card — deliberately shorter than the
    # old full-width run so section breaks read as compact separators
    # (Jason 2026-08-13).
    METADATA_DIVIDER = "────────"
    _ASYNC_STATUS_KEYS = _ASYNC_STATUS_KEYS

    # ------------------------------------------------------------------
    # Display expression: a small, safe, declarative composition grammar.
    #
    # A "display expression" is an ordered tuple of tokens drawn only from
    # ``DISPLAY_SLOTS``. Each token names one preformatted presentation
    # fragment this projection already renders (the header line, the
    # composed activity rows, the footer, ...); composing an expression is
    # pure concatenation of those fragments in the chosen order. It never
    # evaluates code, reads workdir/config/event/prompt data, or scrapes a
    # regex match -- it only rearranges output this class already produced.
    # ``DEFAULT_DISPLAY_EXPRESSION`` encodes Jason's approved footer-first
    # presentation (Telegram 14343) while retaining the same safe, preformatted
    # fragments and hot-swappable declarative grammar.
    DISPLAY_SLOTS: tuple[str, ...] = (
        "header",
        "rows",
        "blank",
        "footer",
        "divider",
        "metadata",
        "time",
        "ask_agent",
    )
    DEFAULT_DISPLAY_EXPRESSION: tuple[str, ...] = (
        "footer",
        "header",
        "rows",
        "blank",
        "divider",
        "metadata",
        "time",
        "ask_agent",
    )
    MAX_DISPLAY_EXPRESSION_LENGTH = 32

    @classmethod
    def validate_display_expression(cls, value: object) -> tuple[str, ...] | None:
        """Validate a raw (e.g. JSON-decoded) display expression.

        Returns the normalized token tuple, or ``None`` when ``value`` is
        ``None`` (meaning "use ``DEFAULT_DISPLAY_EXPRESSION``") or invalid.
        Anything other than a non-empty, bounded-length list of strings each
        drawn from ``DISPLAY_SLOTS`` is rejected wholesale -- there is no
        partial/best-effort acceptance -- so a malformed or unknown-slot
        expression fails closed to the caller's documented default instead of
        composing a degraded layout.
        """
        if value is None:
            return None
        if (
            not isinstance(value, list)
            or not value
            or len(value) > cls.MAX_DISPLAY_EXPRESSION_LENGTH
        ):
            return None
        tokens: list[str] = []
        for item in value:
            if not isinstance(item, str) or item not in cls.DISPLAY_SLOTS:
                return None
            tokens.append(item)
        return tuple(tokens)

    @classmethod
    def compose_display(
        cls,
        slots: dict[str, list[str]],
        expression: tuple[str, ...] | None = None,
    ) -> str:
        """Join preformatted fragments per the declarative display expression.

        ``slots`` maps each ``DISPLAY_SLOTS`` token to the zero-or-more lines
        already rendered for it. ``expression`` selects and orders which
        slots appear; an absent/empty expression uses
        ``DEFAULT_DISPLAY_EXPRESSION``. Composition is pure concatenation --
        no slot's content is interpreted, evaluated, or re-derived here.
        """
        tokens = expression if expression else cls.DEFAULT_DISPLAY_EXPRESSION
        lines: list[str] = []
        for token in tokens:
            lines.extend(slots.get(token, ()))
        return "\n".join(lines)

    @classmethod
    def footer(cls, normal_rows: int, locale: str = "en") -> str:
        if cls.normalize_locale(locale) == "zh":
            return f"{cls._locale_text('footer_prefix', locale)}，{cls._locale_text('footer_current', locale)}: {normal_rows})。"
        return f"{cls.FOOTER}, current: {normal_rows})."

    @staticmethod
    def format_current_time(now: datetime) -> str:
        """Render ``HH:MM:SS U±H`` (e.g. ``00:22:02 U-7``) or empty text.

        The offset is deliberately compact (Jason 2026-08-09): ``UTC-07``
        became ``U-7`` so the symmetric divider header stays short.
        """
        offset = now.utcoffset()
        if offset is None:
            return ""
        total = offset.total_seconds()
        sign = "-" if total < 0 else "+"
        hours = int(abs(total) // 3600)
        return f"{now.strftime('%H:%M:%S')} U{sign}{hours}"

    @classmethod
    def format_row_timestamp(cls, ts: object) -> str:
        """Convert a canonical epoch into the Task Card row timestamp."""
        if type(ts) not in (int, float):
            return ""
        if isinstance(ts, float) and not math.isfinite(ts):
            return ""
        try:
            local = datetime.fromtimestamp(ts).astimezone()
        except (OverflowError, OSError, ValueError):
            return ""
        return cls.format_current_time(local)

    @classmethod
    def project_agent_text_event(
        cls,
        event: dict[str, Any],
        *,
        text_cap: int | None = None,
    ) -> dict[str, Any] | None:
        """Project only canonical public ``diary`` text."""
        if event.get("type") != "diary":
            return None
        if event.get("hidden") is True or event.get("visibility") not in (
            None,
            "public",
        ):
            return None
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        text = redact_text(text).strip()
        cap = cls.EVENT_TEXT_CAP if text_cap is None else text_cap
        if len(text) > cap:
            text = text[: cap - 1] + "…"
        row: dict[str, Any] = {"kind": "text", "text": text}
        raw_ts = event.get("ts")
        if type(raw_ts) in (int, float) and not isinstance(raw_ts, bool):
            try:
                ts = float(raw_ts)
            except (OverflowError, ValueError):
                ts = None
            if ts is not None and math.isfinite(ts):
                row["_ts"] = ts
        api_call_id = event.get("api_call_id")
        if isinstance(api_call_id, str) and api_call_id:
            row["_api_call_id"] = api_call_id
        return row

    @classmethod
    def project_tool_call_row(
        cls,
        event: dict[str, Any],
        *,
        reasoning_cap: int | None = None,
    ) -> dict[str, Any] | None:
        """Extract the fixed safe-field allowlist from one tool call."""
        if event.get("type") != "tool_call":
            return None
        tool_name = event.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return None
        tool_args = event.get("tool_args")
        if not isinstance(tool_args, dict):
            return None
        reasoning = tool_args.get("_reasoning", "")
        if not isinstance(reasoning, str):
            reasoning = ""
        reasoning = redact_text(reasoning)
        cap = cls.EVENT_REASONING_CAP if reasoning_cap is None else reasoning_cap
        if len(reasoning) > cap:
            reasoning = reasoning[: cap - 1] + "…"
        row: dict[str, Any] = {"tool": tool_name, "reasoning": reasoning}
        call_id = event.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            row.update({"_tool_call_id": call_id, "status": "???"})
        # Preserve the provider round's api_call_id so a pure-tool turn (no
        # notification_block_injected carrier) can still match the llm_response
        # per-call usage through the _api_call_id lookup. Jason 2026-08-08.
        api_call_id = event.get("api_call_id")
        if isinstance(api_call_id, str) and api_call_id:
            row["_api_call_id"] = api_call_id
        action = tool_args.get("action")
        if isinstance(action, str) and action:
            row["tool_action"] = action
        # No per-row wall-clock stamp: each API-call group renders exactly one
        # timestamp above its API metadata line (Jason 2026-08-09), so tool rows
        # stay compact and the API-call timeline is uncluttered.
        raw_ts = event.get("ts")
        if type(raw_ts) in (int, float) and not isinstance(raw_ts, bool):
            try:
                ts = float(raw_ts)
            except (OverflowError, ValueError):
                ts = None
            if ts is not None and math.isfinite(ts):
                row["_ts"] = ts
        return row

    @classmethod
    def project_event(
        cls,
        event: dict[str, Any],
        *,
        text_cap: int | None = None,
        reasoning_cap: int | None = None,
    ) -> dict[str, Any] | None:
        text = cls.project_agent_text_event(event, text_cap=text_cap)
        if text is not None:
            return text
        row = cls.project_tool_call_row(event, reasoning_cap=reasoning_cap)
        if row is not None:
            row["kind"] = "tool"
            return row
        return None

    @staticmethod
    def event_group_id(event: dict[str, Any], fallback: int) -> str:
        value = event.get("api_call_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return f"legacy:{fallback}"

    @classmethod
    def group_events(
        cls,
        projected: list[tuple[dict[str, Any], dict[str, Any]]],
        *,
        window: int | None = None,
        max_events_per_call: int | None = None,
    ) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        limit = (
            cls.MAX_EVENTS_PER_CALL
            if max_events_per_call is None
            else max_events_per_call
        )
        last_tool_ts: float | None = None
        for index, (event, row) in enumerate(projected):
            group_id = cls.event_group_id(event, index)
            group = by_id.get(group_id)
            if group is None:
                group = {"api_call_id": group_id, "events": []}
                by_id[group_id] = group
                groups.append(group)
            events = group["events"]
            # The LLM API round trip Jason watches as ``active (N sec)`` is the
            # gap between consecutive progress events: the previous tool_call's
            # own ``ts`` (stream order, so a group's first row still sees the
            # previous group's last tool call; only the very first tool row of
            # the stream has no prior progress and reads 0.0).
            raw_ts = row.get("_ts")
            if type(raw_ts) in (int, float) and not isinstance(raw_ts, bool):
                ts = float(raw_ts)
                if last_tool_ts is None:
                    row["api_delay_s"] = 0.0
                else:
                    row["api_delay_s"] = max(0.0, round(ts - last_tool_ts, 2))
                last_tool_ts = ts
            if len(events) < limit:
                events.append(row)
        count = cls.EVENT_WINDOW if window is None else window
        return groups[-count:]

    @staticmethod
    def flatten_groups(
        groups: list[dict[str, Any]],
        *,
        include_group_id: bool = False,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for group in groups:
            group_id = group.get("api_call_id")
            for event in group.get("events", []):
                row = dict(event)
                if include_group_id:
                    row["group_id"] = group_id
                else:
                    row.pop("group_id", None)
                    row.pop("_tool_call_id", None)
                    row.pop("_ts", None)
                    row.pop("_usage", None)
                    row.pop("_api_call_id", None)
                    row.pop("_result_ts", None)
                    row.pop("_apriori_summary", None)
                if row.get("kind") == "tool":
                    row.pop("kind", None)
                rows.append(row)
        return rows

    @staticmethod
    def project_final_carrier_metadata(
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Project safe session telemetry from a final-carrier event."""
        if event.get("type") != "notification_block_injected":
            return None
        envelope = event.get("_meta")
        if not isinstance(envelope, dict):
            return {}
        agent_meta = envelope.get("agent_meta")
        if not isinstance(agent_meta, dict):
            return {}
        state = agent_meta.get("agent_state")
        if not isinstance(state, dict):
            return {}
        token_usage = state.get("token_usage")
        if not isinstance(token_usage, dict):
            return {}
        session = token_usage.get("session")
        if not isinstance(session, dict):
            return {}
        supported = (
            "input_tokens",
            "session_cache_rate",
            "cache_miss_tokens",
            "cache_miss_budget",
            "api_calls",
            "context_tokens",
            "context_window",
            "context_usage",
        )
        return {key: session[key] for key in supported if key in session}

    @staticmethod
    def decode_event_line(raw: bytes) -> dict[str, Any] | None:
        line = raw.strip()
        if not line:
            return None
        try:
            event = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return event if isinstance(event, dict) else None

    @staticmethod
    def project_current_call_usage(
        event: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """Extract per-call LLM usage from a ``notification_block_injected``
        carrier. Returns ``(call_id, {output, thinking, cache_miss, cache_rate,
        context})`` so the tailer can attach it to the matching tool row.
        ``context`` is the session's actual ``context_tokens`` count riding the
        same carrier (never derived from a percentage); absent/invalid values
        simply omit the key."""
        if event.get("type") != "notification_block_injected":
            return None
        call_id = event.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        envelope = event.get("_meta")
        if not isinstance(envelope, dict):
            return None
        agent_meta = envelope.get("agent_meta")
        if not isinstance(agent_meta, dict):
            return None
        state = agent_meta.get("agent_state")
        if not isinstance(state, dict):
            return None
        token_usage = state.get("token_usage")
        if not isinstance(token_usage, dict):
            return None
        current = token_usage.get("current_call")
        if not isinstance(current, dict):
            return None
        supported = ("output", "thinking", "cache_miss", "cache_rate")
        usage = {key: current[key] for key in supported if key in current}
        session = token_usage.get("session")
        if usage and isinstance(session, dict):
            context = session.get("context_tokens")
            if type(context) is int and context >= 0:
                usage["context"] = context
        return (call_id, usage) if usage else None

    @staticmethod
    def project_llm_response_usage(
        event: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """Extract per-call LLM usage from a ``llm_response`` event.

        Pure-text turns (an assistant reply with no tool call) never carry a
        ``notification_block_injected`` carrier, so their per-call token usage
        has to come from the ``llm_response`` event itself. Returns
        ``(api_call_id, {output, thinking, cache_miss, cache_rate, context})``.
        Estimated token counts are still shown (they are the only signal a
        pure-text turn has) but missing/zero input degrades to ``None``.
        ``context`` is the
        event's ``input_tokens`` — the exact value the kernel records as that
        round's session ``context_tokens`` (``ctx_total_tokens``), an actual
        count, never reconstructed from the cache percentage.
        """
        if event.get("type") != "llm_response":
            return None
        call_id = event.get("api_call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        output = event.get("output_tokens")
        thinking = event.get("thinking_tokens")
        cached = event.get("cached_tokens")
        total = event.get("input_tokens")
        if (
            type(output) is not int
            or type(cached) is not int
            or type(total) is not int
            or total <= 0
        ):
            return None
        usage: dict[str, Any] = {
            "output": output,
            "cache_miss": max(total - cached, 0),
            "context": total,
        }
        if type(thinking) is int and thinking >= 0:
            usage["thinking"] = thinking
        if cached > 0:
            usage["cache_rate"] = min(cached / total, 1.0)
        return (call_id, usage)

    @staticmethod
    def apply_tool_usages(
        groups: list[dict[str, Any]],
        usages: dict[str, dict[str, Any]],
    ) -> bool:
        """Attach per-call usage to rows by their ``_tool_call_id``.

        Pure-text rows carry ``_api_call_id`` instead, which the tailer keys
        from ``llm_response`` events; fall back to it so a text-only turn still
        gets its divider usage arrows. Tool rows now preserve the event's own
        ``api_call_id`` as ``_api_call_id``, so a pure-tool turn (e.g.
        ``context.molt``) with no carrier also matches the same ``llm_response``
        usage through that key. Jason 2026-08-08.
        """
        changed = False
        for group in groups:
            for row in group.get("events", []):
                usage = usages.get(row.get("_tool_call_id"))
                if usage is None:
                    usage = usages.get(row.get("_api_call_id"))
                if usage is None:
                    continue
                if row.get("_usage") == usage:
                    continue
                row["_usage"] = usage
                changed = True
        return changed

    @staticmethod
    def apply_tool_results(
        groups: list[dict[str, Any]],
        tool_results: dict[str, dict[str, Any]],
    ) -> bool:
        changed = False
        for group in groups:
            for row in group.get("events", []):
                result = tool_results.get(row.get("_tool_call_id"))
                if result is None:
                    continue
                status = result.get("status")
                row["status"] = (
                    "error"
                    if status == "error"
                    else "success"
                    if isinstance(status, str) and status
                    else "???"
                )
                elapsed_ms = result.get("elapsed_ms")
                if type(elapsed_ms) in (int, float) and elapsed_ms >= 0:
                    row["elapsed_s"] = elapsed_ms / 1000
                    # Keep the raw ms so sub-second durations render exactly
                    # (e.g. ``412ms``) instead of rounding to ``0s``.
                    row["elapsed_ms"] = elapsed_ms
                raw_ts = result.get("ts")
                if type(raw_ts) in (int, float) and not isinstance(raw_ts, bool):
                    result_ts = float(raw_ts)
                    if math.isfinite(result_ts):
                        row["_result_ts"] = result_ts
                changed = True
        return changed

    @staticmethod
    def project_apriori_summary_event(
        event: dict[str, Any],
    ) -> tuple[str, float] | None:
        """Project only correlation and completion time from a generated summary.

        Generated summary text, provider fields, and arbitrary event payload stay
        outside the Task Card projection.
        """
        if event.get("type") != "apriori_summary_generated":
            return None
        call_id = event.get("tool_call_id")
        raw_ts = event.get("ts")
        if (
            not isinstance(call_id, str)
            or not call_id
            or type(raw_ts) not in (int, float)
            or isinstance(raw_ts, bool)
        ):
            return None
        summary_ts = float(raw_ts)
        if not math.isfinite(summary_ts):
            return None
        return call_id, summary_ts

    @classmethod
    def apply_apriori_summary_metrics(
        cls,
        groups: list[dict[str, Any]],
        summary_times: dict[str, float],
        summary_usages: dict[str, dict[str, int]],
    ) -> bool:
        """Attach safe existing summary timing/token facts to successful tools."""
        changed = False
        for group in groups:
            for row in group.get("events", []):
                if row.get("status") != "success":
                    continue
                call_id = row.get("_tool_call_id")
                summary_ts = summary_times.get(call_id)
                usage = summary_usages.get(call_id)
                result_ts = row.get("_result_ts")
                if (
                    type(summary_ts) not in (int, float)
                    or isinstance(summary_ts, bool)
                    or type(result_ts) not in (int, float)
                    or isinstance(result_ts, bool)
                    or not isinstance(usage, dict)
                ):
                    continue
                delta_ms = (float(summary_ts) - float(result_ts)) * 1000
                if (
                    not math.isfinite(delta_ms)
                    or delta_ms < 0
                    or delta_ms > cls.MAX_ELAPSED_MS
                ):
                    continue
                input_tokens = usage.get("input")
                output_tokens = usage.get("output")
                if (
                    type(input_tokens) is not int
                    or input_tokens < 0
                    or type(output_tokens) is not int
                    or output_tokens < 0
                ):
                    continue
                metrics = {
                    "elapsed_ms": int(round(delta_ms)),
                    "input": input_tokens,
                    "output": output_tokens,
                }
                if row.get("_apriori_summary") == metrics:
                    continue
                row["_apriori_summary"] = metrics
                changed = True
        return changed

    @classmethod
    def format_apriori_summary_metrics(cls, value: object) -> str | None:
        """Format the requested summary time/input/output line."""
        if not isinstance(value, dict):
            return None
        elapsed_ms = value.get("elapsed_ms")
        input_text = cls.format_count(value.get("input"))
        output_text = cls.format_count(value.get("output"))
        if (
            type(elapsed_ms) is not int
            or elapsed_ms < 0
            or elapsed_ms > cls.MAX_ELAPSED_MS
            or input_text is None
            or output_text is None
        ):
            return None
        elapsed = cls.format_elapsed_ms(elapsed_ms)
        return f"(summary, {elapsed}, {input_text} in, {output_text} out)"

    @classmethod
    def render_event_groups(
        cls,
        groups: list[dict[str, Any]],
        *,
        normal_rows: int,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
        locale: str = "en",
        display_expression: tuple[str, ...] | None = None,
    ) -> str:
        rows: list[dict[str, Any]] = []
        for group in groups[-normal_rows:]:
            # One wall-clock stamp per API call sits centered in the divider
            # line (Jason 2026-08-09): the first progress row of the group
            # marks the round trip's start, so the symmetric divider carries it
            # `──── 00:22:02 U-7 ────` — no separate timestamp line, no marker.
            stamp = ""
            group_ts: float | None = None
            for row in group.get("events", []):
                if group_ts is None:
                    v = row.get("_ts")
                    if type(v) in (int, float) and not isinstance(v, bool):
                        group_ts = float(v)
                if group_ts is not None:
                    stamp = cls.format_row_timestamp(group_ts)
                    break
            if stamp:
                rows.append(
                    {"kind": "divider", "text": f"{cls.API_CALL_DIVIDER} {stamp} {cls.API_CALL_DIVIDER}"}
                )
            else:
                rows.append({"kind": "divider", "text": cls.API_CALL_DIVIDER})
            # The group's API delay is the LLM round-trip gap since the previous
            # progress; the per-call usage rides the same divider line, both kept
            # out of tool rows.
            api_delay_s: float | None = None
            usage: dict[str, Any] | None = None
            for row in group.get("events", []):
                v = row.get("api_delay_s")
                if api_delay_s is None and type(v) in (int, float) and not isinstance(v, bool) and v >= 0:
                    api_delay_s = float(v)
                u = row.get("_usage")
                if usage is None and isinstance(u, dict) and u:
                    usage = u
                if api_delay_s is not None and usage is not None:
                    break
            info = cls.format_divider_info(api_delay_s, usage)
            if info:
                rows.append({"kind": "api_info", "text": info})
            rows.extend(group.get("events", []))
        text = cls.format_task_card_text(
            "",
            "",
            "",
            rows=rows,
            metadata=metadata,
            normal_rows=normal_rows,
            now=now,
            locale=locale,
            display_expression=display_expression,
        )
        return text[: cls.TEXT_LIMIT] if len(text) > cls.TEXT_LIMIT else text

    @classmethod
    def format_divider_info(
        cls,
        api_delay_s: float | None,
        usage: dict[str, Any] | None,
    ) -> str:
        """Compact divider: `↻ x s ↓out (think) ↑miss ◌ ctx | cache%`.

        The down arrow denotes output tokens; the immediately following
        parenthesized count denotes thinking/reasoning tokens; and the up arrow
        denotes cache miss. ``◌`` is the current context length (an actual token
        count) and the trailing percentage is that call's cache rate, joined as
        ``◌ <context> | <rate>``. Any piece missing from the event degrades
        silently — no dangling marker or separator — so old events without
        usage still render the delay alone.
        """
        parts: list[str] = []
        if api_delay_s is not None and api_delay_s > 0:
            parts.append(f"↻ {api_delay_s:.1f}s")
        if isinstance(usage, dict) and usage:
            out = usage.get("output")
            thinking = usage.get("thinking")
            miss = usage.get("cache_miss")
            rate = usage.get("cache_rate")
            out_text = cls.format_count(out)
            if out_text is not None:
                parts.append(f"\u2193{out_text}")
                thinking_text = cls.format_count(thinking)
                if thinking_text is not None:
                    parts.append(f"({thinking_text})")
            if type(miss) is int and miss >= 0:
                parts.append(f"\u2191{cls.format_count(miss)}")
            context = cls.format_count(usage.get("context"))
            rate_text = (
                f"{float(rate):.1%}"
                if type(rate) in {int, float}
                and not isinstance(rate, bool)
                and 0 <= rate <= 1
                else None
            )
            if context is not None and rate_text is not None:
                parts.append(f"\u25cc {context} | {rate_text}")
            elif context is not None:
                parts.append(f"\u25cc {context}")
            elif rate_text is not None:
                parts.append(rate_text)
        return " ".join(parts)

    @classmethod
    def format_task_card_text(
        cls,
        tool: str,
        action: str,
        reasoning: str,
        *,
        rows: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        normal_rows: int = DEFAULT_NORMAL_ROWS,
        now: datetime | None = None,
        locale: str = "en",
        display_expression: tuple[str, ...] | None = None,
    ) -> str:
        if rows is None:
            return cls.format_scalar_task_card_text(tool, action, reasoning, locale=locale)
        return cls.format_rows_task_card_text(
            rows,
            metadata=metadata,
            normal_rows=normal_rows,
            now=now,
            locale=locale,
            display_expression=display_expression,
        )

    @classmethod
    def format_scalar_task_card_text(
        cls,
        tool: str,
        action: str,
        reasoning: str,
        *,
        locale: str = "en",
    ) -> str:
        redacted = redact_text(reasoning)
        if len(redacted) > cls.REASONING_CAP:
            excerpt = redacted[: cls.REASONING_CAP] + "…"
        else:
            excerpt = redacted
        label = f"{tool}.{action}" if action else tool
        header = cls.header(locale)
        if label:
            return f"{header}\n{label}: {excerpt}"
        return f"{header}\n{excerpt}" if excerpt else header

    @staticmethod
    def format_count(value: object) -> str | None:
        if type(value) is not int or value < 0:
            return None
        for threshold, suffix in (
            (1_000_000_000_000, "T"),
            (1_000_000_000, "B"),
            (1_000_000, "M"),
            (1_000, "k"),
        ):
            if value >= threshold:
                tenths = (value * 10 + threshold // 2) // threshold
                if suffix == "T":
                    tenths = min(tenths, 9_999)
                return f"{tenths // 10}.{tenths % 10}{suffix}"
        return str(value)

    @classmethod
    def format_metadata(cls, metadata: object, locale: str = "en") -> list[str]:
        """Render bounded resident-card metadata as explicit semantic sections.

        The manager supplies ``async_work`` as a render-time, read-only snapshot.
        This method deliberately knows nothing about the filesystem or providers;
        it only sanitizes and arranges the already validated payload.
        """
        if not isinstance(metadata, dict):
            return []

        def label(key: str) -> str:
            return cls._locale_text(key, locale)

        session_parts: list[str] = []
        model = metadata.get("model")
        if isinstance(model, str) and model.strip() and len(model.strip()) <= 128:
            session_parts.append(model.strip())
        thinking = metadata.get("thinking")
        if isinstance(thinking, str) and thinking.strip() and len(thinking.strip()) <= 48:
            session_parts.append(thinking.strip())
        service_tier = metadata.get("service_tier")
        if (
            isinstance(service_tier, str)
            and service_tier.strip()
            and len(service_tier.strip()) <= 48
        ):
            session_parts.append(f"tier {service_tier.strip()}")
        endpoint = metadata.get("endpoint")
        if isinstance(endpoint, str) and endpoint.strip() and len(endpoint.strip()) <= 96:
            session_parts.append(f"@{endpoint.strip()}")
        context = cls.format_count(metadata.get("context_tokens"))
        window = cls.format_count(metadata.get("context_window"))
        usage = metadata.get("context_usage")
        if (
            type(usage) in {int, float}
            and not isinstance(usage, bool)
            and math.isfinite(float(usage))
            and 0 <= usage <= 1
        ):
            if context is not None:
                session_parts.append(
                    f"ctx {float(usage):.0%} · {context}/{window}"
                    if window is not None
                    else f"ctx {float(usage):.0%}"
                )
            else:
                session_parts.append(f"ctx {float(usage):.0%}")
        elif context is not None:
            session_parts.append(
                f"ctx {context}/{window}" if window is not None else f"ctx {context}"
            )
        tokens = cls.format_count(metadata.get("input_tokens"))
        if tokens is not None:
            session_parts.append(f"tokens {tokens}")
        cache_rate = metadata.get("session_cache_rate")
        if (
            type(cache_rate) in {int, float}
            and not isinstance(cache_rate, bool)
            and math.isfinite(float(cache_rate))
            and 0 <= cache_rate <= 1
        ):
            session_parts.append(f"cache {float(cache_rate):.1%}")
        miss = cls.format_count(metadata.get("cache_miss_tokens"))
        budget = cls.format_count(metadata.get("cache_miss_budget"))
        if miss is not None:
            session_parts.append(
                f"miss {miss}/{budget}" if budget is not None else f"miss {miss}"
            )
        calls = cls.format_count(metadata.get("api_calls"))
        if calls is not None:
            session_parts.append(f"calls {calls}")

        agent_part: str | None = None
        lifecycle = metadata.get("agent_lifecycle")
        if lifecycle in (AgentState.STUCK.value, "offline"):
            agent_part = f"agent · {lifecycle} · {cls._locale_text('try_refresh', locale)}"
        elif lifecycle == AgentState.ACTIVE.value:
            active_seconds = metadata.get("agent_active_seconds")
            if (
                type(active_seconds) in {int, float}
                and not isinstance(active_seconds, bool)
                and math.isfinite(float(active_seconds))
                and active_seconds >= 0
            ):
                agent_part = f"active ({float(active_seconds):.0f}s)"
            else:
                agent_part = "active"
        elif lifecycle in cls.AGENT_STATES:
            agent_part = f"agent · {lifecycle}"

        line1_parts: list[str] = []
        if agent_part:
            line1_parts.append(agent_part)
        line1_parts.extend(session_parts)
        session_line = (
            f"{label('session')} · {' · '.join(line1_parts)}"
            if line1_parts
            else None
        )

        # Keep all identity values behind the strict machine_identifier allowlist.
        # In particular, working_dir is never rendered from an arbitrary string.
        device = cls.machine_identifier(metadata.get("device_short_name"), limit=64)
        shell_name = cls.machine_identifier(metadata.get("shell_name"), limit=48)
        identity_parts: list[str] = []
        if device is not None or shell_name is not None:
            parts: list[str] = []
            if device is not None:
                parts.append(device)
            if shell_name is not None:
                parts.append(f"shell {shell_name}")
            identity_parts.append("device · " + " · ".join(parts))
        working_dir = cls.machine_identifier(metadata.get("working_dir"), limit=220)
        if working_dir is not None:
            identity_parts.append(f"path · {working_dir}")
        identity_payload = " | ".join(identity_parts) if identity_parts else None
        identity_line = (
            f"{label('identity')} · {identity_payload}"
            if identity_payload
            else None
        )

        def count(value: object) -> int | None:
            if type(value) is int and value >= 0:
                return value
            return None

        def status_parts(source: object) -> list[str]:
            if not isinstance(source, dict):
                return []
            out: list[str] = []
            for key in cls._ASYNC_STATUS_KEYS:
                value = count(source.get(key))
                if value is not None and value > 0:
                    out.append(f"{key} {value}")
            return out

        async_work = metadata.get("async_work")
        async_sections: list[tuple[str, str, int]] = []
        if isinstance(async_work, dict):
            daemon = async_work.get("daemon")
            shell = async_work.get("shell")
            daemon_parts = status_parts(daemon)
            shell_parts = status_parts(shell)
            # The adapter normally supplies validated combined counts. When a
            # carrier is hand-built or stale, prefer the two lane truths so the
            # visible unified kanban cannot contradict its detail rows.
            if isinstance(daemon, dict) or isinstance(shell, dict):
                totals = []
                for key in cls._ASYNC_STATUS_KEYS:
                    value = 0
                    for lane in (daemon, shell):
                        if isinstance(lane, dict):
                            lane_value = count(lane.get(key))
                            if lane_value is not None:
                                value += lane_value
                    if value > 0:
                        totals.append(f"{key} {value}")
            else:
                totals = status_parts(async_work)
            backend_parts: list[str] = []
            if isinstance(daemon, dict) and isinstance(daemon.get("backend_counts"), dict):
                safe_backends: list[tuple[str, int]] = []
                for raw_backend, raw_count in daemon["backend_counts"].items():
                    backend = cls.machine_identifier(raw_backend, limit=48)
                    value = count(raw_count)
                    if backend is None:
                        backend = "unknown"
                    if value is not None and value > 0:
                        safe_backends.append((backend, value))
                for backend, value in sorted(safe_backends):
                    backend_parts.append(f"{backend} {value}")

            if isinstance(daemon, dict) and isinstance(daemon.get("model_counts"), dict):
                safe_models: list[tuple[str, int]] = []
                for raw_model, raw_count in daemon["model_counts"].items():
                    model = cls.machine_identifier(raw_model, limit=128)
                    value = count(raw_count)
                    if model is not None and value is not None and value > 0:
                        safe_models.append((model, value))
                safe_models.sort()
                model_total = sum(value for _, value in safe_models)
                if model_total == 1:
                    daemon_parts.append(safe_models[0][0])
                elif model_total > 1:
                    daemon_parts.extend(
                        f"{model} × {value}" for model, value in safe_models
                    )

            stats_parts: list[str] = []
            if isinstance(daemon, dict):
                input_tokens = count(daemon.get("input_tokens"))
                output_tokens = count(daemon.get("output_tokens"))
                cached_tokens = count(daemon.get("cached_tokens"))
                cli_calls = count(daemon.get("cli_calls"))
                if input_tokens is not None and input_tokens > 0:
                    stats_parts.append(f"in {cls.format_count(input_tokens)}")
                if output_tokens is not None and output_tokens > 0:
                    stats_parts.append(f"out {cls.format_count(output_tokens)}")
                if (
                    cached_tokens is not None
                    and input_tokens is not None
                    and input_tokens > 0
                    and cached_tokens > 0
                ):
                    stats_parts.append(
                        f"cache {min(cached_tokens / input_tokens, 1.0):.1%}"
                    )
                if cli_calls is not None and cli_calls > 0:
                    stats_parts.append(f"api {cli_calls}")

            # All rows belong to one Async Work section. The priority value is
            # used only by the whole-line 500-character budget below.
            if totals:
                async_sections.append(("totals", f"{label('async_work')} · {' · '.join(totals)}", 2))
            if daemon_parts:
                async_sections.append(("daemon", f"{label('daemons')} · {' · '.join(daemon_parts)}", 3))
            if backend_parts:
                async_sections.append(("backend", f"{label('backends')} · {' · '.join(backend_parts)}", 4))
            if shell_parts:
                async_sections.append(("shell", f"{label('shell')} · {' · '.join(shell_parts)}", 3))
            if stats_parts:
                async_sections.append(("stats", f"{label('daemon_stats')} · {' · '.join(stats_parts)}", 4))

        # ``sections`` is intentionally list[list[str]]: a section is either
        # present or absent, and dividers are inserted only between present
        # adjacent sections (never after Identity or inside Async Work).
        sections: list[list[str]] = []
        priorities: list[list[int]] = []
        if session_line is not None:
            sections.append([session_line])
            priorities.append([0])
        if identity_line is not None:
            sections.append([identity_line])
            priorities.append([1])
        if async_sections:
            sections.append([line for _, line, _ in async_sections])
            priorities.append([priority for _, _, priority in async_sections])

        def render_selected(selected: list[list[tuple[str, int]]]) -> list[str]:
            result: list[str] = []
            previous = False
            for section in selected:
                present = [line for line, _ in section if line]
                if not present:
                    continue
                if previous:
                    result.append(cls.METADATA_DIVIDER)
                result.extend(present)
                previous = True
            return result

        def total_length(lines: list[str]) -> int:
            return len("\n".join(lines))

        selected: list[list[tuple[str, int]]] = [
            [(line, priority) for line, priority in zip(lines, section_priorities)]
            for lines, section_priorities in zip(sections, priorities)
        ]

        # Remove lower-priority complete rows before touching Identity. Session
        # and Identity are section rows, while Async totals outrank lane detail.
        for priority_to_drop in (4, 3, 2):
            if total_length(render_selected(selected)) <= cls.METADATA_MAX_CHARS:
                break
            for section in selected:
                section[:] = [item for item in section if item[1] != priority_to_drop]

        # If necessary, shorten only the Identity payload. The label and its
        # separator remain atomic, and the ellipsis is added only to a shortened
        # payload (never to a divider or a partial label).
        if total_length(render_selected(selected)) > cls.METADATA_MAX_CHARS:
            identity_location: tuple[int, int] | None = None
            for section_index, section in enumerate(selected):
                for item_index, (_, priority) in enumerate(section):
                    if priority == 1:
                        identity_location = (section_index, item_index)
                        break
                if identity_location is not None:
                    break
            if identity_location is not None:
                section_index, item_index = identity_location
                original, priority = selected[section_index][item_index]
                marker = " · "
                marker_index = original.find(marker)
                if marker_index >= 0:
                    prefix = original[: marker_index + len(marker)]
                    payload = original[marker_index + len(marker) :]
                    low, high = 0, len(payload)
                    best: str | None = None
                    while low <= high:
                        mid = (low + high) // 2
                        candidate_payload = payload if mid == len(payload) else (
                            payload[: max(0, mid - 1)] + "…"
                        )
                        candidate = prefix + candidate_payload
                        trial = [list(section) for section in selected]
                        trial[section_index][item_index] = (candidate, priority)
                        if total_length(render_selected(trial)) <= cls.METADATA_MAX_CHARS:
                            best = candidate
                            low = mid + 1
                        else:
                            high = mid - 1
                    if best is not None:
                        selected[section_index][item_index] = (best, priority)
                    else:
                        selected[section_index].pop(item_index)

        # A pathological session payload may itself exceed the budget. There is
        # no safe partial Session representation; retain the higher-priority
        # whole line and discard lower-priority whole lines rather than splitting
        # a label/separator. Normal session fields are bounded well below this.
        while total_length(render_selected(selected)) > cls.METADATA_MAX_CHARS:
            removable = [
                (priority, section_index, item_index)
                for section_index, section in enumerate(selected)
                for item_index, (_, priority) in enumerate(section)
                if priority > 0
            ]
            if not removable:
                break
            _, section_index, item_index = max(removable)
            selected[section_index].pop(item_index)

        return render_selected(selected)

    @classmethod
    def _short_working_dir(cls, working_dir: str) -> str:
        """Trim an absolute agent path to ``<project>/.lingtai/<agent>``."""
        idx = working_dir.find("/.lingtai/")
        if idx > 0:
            prev = working_dir.rfind("/", 0, idx)
            if prev >= 0:
                return working_dir[prev + 1 :]
            return working_dir
        return working_dir

    @classmethod
    def format_rows_task_card_text(
        cls,
        rows: list[Any],
        *,
        metadata: dict[str, Any] | None = None,
        normal_rows: int = DEFAULT_NORMAL_ROWS,
        now: datetime | None = None,
        locale: str = "en",
        display_expression: tuple[str, ...] | None = None,
    ) -> str:
        footer = cls.footer(normal_rows, locale)
        tool_prepared: list[tuple[int, str, str, str, bool, str | None, str | None]] = []
        text_prepared: list[tuple[int, str]] = []
        api_prepared: list[tuple[int, str]] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            kind = row.get("kind")
            if kind == "divider":
                divider = redact_text(str(row.get("text", cls.API_CALL_DIVIDER))).strip()
                api_prepared.append((idx, divider[: cls.EVENT_TEXT_CAP]))
                continue
            if kind == "api_info":
                info = redact_text(str(row.get("text", ""))).strip()
                if info:
                    api_prepared.append((idx, info[: cls.EVENT_TEXT_CAP]))
                continue
            if kind == "api_ts":
                stamp = redact_text(str(row.get("text", ""))).strip()
                if stamp:
                    api_prepared.append((idx, stamp[: cls.EVENT_TEXT_CAP]))
                continue
            if kind == "text":
                text = redact_text(str(row.get("text", ""))).strip()
                if text:
                    text_prepared.append((idx, text[: cls.EVENT_TEXT_CAP]))
                continue
            if kind == "api_error":
                api_prepared.append((idx, cls.format_api_error_line(row, locale)))
                continue
            tool = str(row.get("tool", ""))
            action = str(row.get("tool_action", ""))
            label = f"{tool}.{action}" if action else tool
            redacted = redact_text(str(row.get("reasoning", "")))
            done = bool(row.get("done", False))
            status = row.get("status")
            status = status if status in {"success", "error", "???"} else None
            # Duration in whole milliseconds (sub-second tool results were
            # useless as ``0s``). The LLM API round-trip gap since the previous
            # progress is rendered on the group divider, not here.
            elapsed = cls.format_elapsed_ms(cls.row_elapsed_ms(row))
            if status == "???":
                # A tool call with no result yet is genuinely running; showing
                # ``???`` wasted the slot without saying anything real.
                status_suffix = ""
                suffix = f" ({elapsed}, running)" if elapsed else " (running)"
            else:
                status_suffix = f", {status}" if status else ""
                suffix = f" ({elapsed}{status_suffix})"
            summary_metrics = (
                cls.format_apriori_summary_metrics(row.get("_apriori_summary"))
                if status == "success"
                else None
            )
            tool_prepared.append(
                (idx, label, redacted, suffix, done, status, summary_metrics)
            )

        metadata_lines = cls.format_metadata(metadata, locale)
        time_line = f"{cls.time_prefix(locale)}{cls.render_time(now)}"
        ask_agent_line = cls._locale_text("ask_agent", locale)
        if not tool_prepared and not text_prepared and not api_prepared:
            slots = {
                "header": [cls.header(locale)],
                "rows": [],
                "blank": [""],
                "footer": [footer],
                "divider": [cls.METADATA_DIVIDER],
                "metadata": metadata_lines,
                "time": [time_line],
                "ask_agent": [ask_agent_line],
            }
            return cls.compose_display(slots, display_expression)

        api_scaffold = sum(len(line) + 1 for _, line in api_prepared)
        text_scaffold = sum(len(text) + 4 for _, text in text_prepared)
        tool_scaffold = 0
        for (
            _,
            label,
            _redacted,
            suffix,
            done,
            status,
            summary_metrics,
        ) in tool_prepared:
            marker = "✓ " if done or status == "success" else "• "
            prefix = f"{marker}{label}: " if label else marker
            tool_scaffold += len(prefix) + len(suffix) + 2
            if summary_metrics:
                tool_scaffold += len(summary_metrics) + 2
        fixed = (
            len(cls.header(locale))
            + 1
            + 1
            + len(footer)
            + len(cls.METADATA_DIVIDER)
            + 1
            + sum(len(line) + 1 for line in metadata_lines)
            + len(time_line)
            + 1
            + len(ask_agent_line)
            + 1
            + api_scaffold
            + text_scaffold
            + tool_scaffold
        )
        budget = cls.TEXT_LIMIT - fixed
        divisor = max(1, len(tool_prepared) + len(text_prepared))
        per_row_cap = max(0, min(cls.REASONING_CAP, budget // divisor))

        by_idx: dict[int, str] = {}
        for idx, label, redacted, suffix, done, status, summary_metrics in tool_prepared:
            excerpt = (
                redacted[:per_row_cap] + "…"
                if len(redacted) > per_row_cap
                else redacted
            )
            marker = "✓ " if done or status == "success" else "• "
            prefix = f"{marker}{label}: " if label else marker
            line = f"{prefix}{excerpt}{suffix}"
            if summary_metrics:
                line += f"\n {summary_metrics}"
            by_idx[idx] = line
        for idx, text in text_prepared:
            excerpt = text[:per_row_cap] + "…" if len(text) > per_row_cap else text
            by_idx[idx] = f"• {excerpt}"
        for idx, line in api_prepared:
            by_idx[idx] = line

        slots = {
            "header": [cls.header(locale)],
            "rows": [by_idx[index] for index in sorted(by_idx)],
            "blank": [""],
            "footer": [footer],
            "divider": [cls.METADATA_DIVIDER],
            "metadata": metadata_lines,
            "time": [time_line],
            "ask_agent": [ask_agent_line],
        }
        return cls.compose_display(slots, display_expression)

    @classmethod
    def render_time(cls, now: datetime | None) -> str:
        if now is None:
            now = datetime.now().astimezone()
        return cls.format_current_time(now)

    @staticmethod
    def machine_identifier(value: object, *, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or len(value) > limit:
            return None
        safe_punctuation = frozenset("._:/\\-")
        if not all(
            ch.isascii() and (ch.isalnum() or ch in safe_punctuation) for ch in value
        ):
            return None
        return value

    @classmethod
    def format_api_error_line(cls, row: dict[str, Any], locale: str = "en") -> str:
        state = row.get("state")
        parts = [cls._locale_text("api_error", locale)]
        error_type = cls.machine_identifier(row.get("error_type"), limit=48)
        if error_type is not None:
            parts.append(error_type)
        provider = cls.machine_identifier(row.get("provider"), limit=48)
        model = cls.machine_identifier(row.get("model"), limit=80)
        if provider is not None and model is not None:
            parts.append(f"{provider}/{model}")
        elif provider is not None or model is not None:
            parts.append(provider or model or "")
        status = row.get("status")
        if type(status) is int and 100 <= status <= 599:
            parts.append(f"HTTP {status}")
        code = row.get("code")
        if isinstance(code, str) and code:
            parts.append(code)
        summary = " · ".join(parts)

        if state == "recovered":
            return f"✓ {summary} · {cls._locale_text('recovered', locale)}"
        if state == "error":
            return f"⚠️ {summary} · {cls._locale_text('failed', locale)}"
        attempt = row.get("attempt")
        max_attempts = row.get("max_attempts")
        if (
            type(attempt) is int
            and type(max_attempts) is int
            and attempt > 0
            and max_attempts > 0
        ):
            return f"⚠️ {summary} · {cls._locale_text('retrying', locale)} {attempt}/{max_attempts}"
        if type(attempt) is int and attempt > 0:
            return f"⚠️ {summary} · {cls._locale_text('retrying_attempt', locale).format(n=attempt)}"
        return f"⚠️ {summary} · {cls._locale_text('retrying', locale)}"

    @staticmethod
    def format_elapsed(value: object) -> str:
        try:
            return str(max(0, int(float(value))))
        except (TypeError, ValueError):
            return "0"

    @staticmethod
    def row_elapsed_ms(row: dict[str, Any]) -> float:
        """Resolve a tool row's elapsed duration in whole milliseconds.

        Prefers the raw ``elapsed_ms`` captured from the tool result so
        sub-second durations are preserved exactly; falls back to converting
        the legacy ``elapsed_s`` field. Missing/malformed values degrade to 0.
        """
        raw_ms = row.get("elapsed_ms")
        if (
            type(raw_ms) in (int, float)
            and not isinstance(raw_ms, bool)
            and raw_ms >= 0
        ):
            return float(raw_ms)
        raw_s = row.get("elapsed_s")
        if (
            type(raw_s) in (int, float)
            and not isinstance(raw_s, bool)
            and raw_s >= 0
        ):
            return float(raw_s) * 1000
        return 0.0

    @classmethod
    def format_elapsed_ms(cls, value: object) -> str:
        """Render a tool row's elapsed duration adaptively.

        Under 0.1s (Jason 2026-08-08): whole milliseconds (``31ms``); at or above
        0.1s: one decimal second (``2.3s``). Milliseconds for sub-0.1s tools stay
        useful instead of rounding to ``0s``, and seconds read naturally for
        longer tools instead of a wide millisecond wall. Coerces defensively
        (floored, junk/non-finite/negative degrade to ``0ms``) and caps the
        display at ``MAX_ELAPSED_MS`` so a runaway timer cannot widen the row
        unboundedly.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "0ms"
        if not math.isfinite(number) or number < 0:
            return "0ms"
        if number < 100:
            return f"{min(int(number), cls.MAX_ELAPSED_MS)}ms"
        seconds = min(number, cls.MAX_ELAPSED_MS) / 1000
        return f"{seconds:.1f}s"
