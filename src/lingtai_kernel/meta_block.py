"""Unified per-turn metadata injection.

Single source of truth for "what the agent sees about its own runtime state
on every turn." Both injection sites — text-input prefix (in BaseAgent) and
tool-result stamp (in ToolExecutor) — read from here.

Curate carefully: every field added to `build_meta` ships on every text input
and every tool result.

Channel encoding:
- Tool-result channel: `stamp_meta` flattens the dict into the result dict
  as-is. The LLM sees structured JSON (e.g. ``result["context"]["usage"]``,
  ``result["notifications"]``).
- Text-input channel: `render_meta` formats the same dict into a prose
  prefix line. Inbox content is NOT rendered here — it lives in the
  user-turn body, drained by ``_concat_queued_messages`` upstream.

Inbox drain policy:
- ``build_meta(agent, drain_inbox=True)`` consumes ``agent.inbox`` and
  stores the full message contents in ``meta["notifications"]`` (a list of
  strings, in FIFO order). Used by the tool-result ``meta_fn``.
- ``build_meta(agent)`` (drain_inbox=False, default) leaves inbox alone.
  Used by the text-input prefix path; inbox is drained by the outer loop's
  ``_concat_queued_messages`` into the user-turn message body.
"""
from __future__ import annotations

import queue

from .i18n import t as _t
from .time_veil import now_iso


def build_meta(agent, *, drain_inbox: bool = False) -> dict:
    """Return the current meta-data snapshot for the agent.

    Respects ``agent._config.time_awareness`` / ``timezone_awareness``
    internally; callers never need to special-case those flags.

    Shape::

        {
            "current_time": "<iso>",     # absent when time-blind
            "context": {
                "system_tokens": int,    # sys prompt + tools schema
                "history_tokens": int,   # conversation history
                "usage": float,          # fraction of context window used
            },
            "notifications": [...],      # absent when inbox empty / not drained
        }

    Sentinel handling: when token decomposition has not yet run, the
    ``context`` sub-object is still emitted but with ``-1`` / ``-1.0``
    values so callers can render "unknown" without ambiguity.

    When ``drain_inbox=True`` and ``agent.inbox`` is non-empty, every
    queued message is consumed and its content joins ``meta["notifications"]``
    (full content, no truncation, no newline flattening — JSON channel can
    carry it). When ``drain_inbox=False`` (default), inbox is left alone.
    """
    meta: dict = {}
    ts = now_iso(agent)
    if ts:
        meta["current_time"] = ts

    # Context-window decomposition. The decomposition needs the agent's
    # system prompt, tool schemas, and context section — all of which
    # are available via the builder callbacks without needing any LLM
    # call to have happened. If the cached values are dirty, refresh them
    # eagerly so the text-input prefix reports real numbers on the very
    # first call of the turn instead of "unknown".
    session = getattr(agent, "_session", None)
    chat_obj = getattr(session, "chat", None) if session is not None else None

    if session is not None and session._token_decomp_dirty:
        try:
            session._update_token_decomposition()
        except Exception:
            pass  # leave dirty; sentinels below

    decomp_ran = session is not None and not session._token_decomp_dirty

    if decomp_ran:
        sys_prompt = session._system_prompt_tokens
        tools = session._tools_tokens
        # "history" = in-memory turns (wire chat).
        # Derived from the server-reported wire count when available
        # (_latest_input_tokens - sys_prompt - tools). Before the first
        # LLM call of a session (e.g. right after start() rehydrates the
        # ChatInterface from chat_history.jsonl on cold start or refresh),
        # _latest_input_tokens is still 0, which would report "对话 0"
        # even though the wire chat has been restored. Fall back to the
        # interface's local estimate so the meta-line reflects the
        # restored history from turn 1.
        if session._latest_input_tokens > 0:
            history = max(
                0,
                session._latest_input_tokens - sys_prompt - tools,
            )
        elif chat_obj is not None:
            # interface.estimate_context_tokens() returns system + tools +
            # conversation. Subtract system + tools to isolate the history
            # portion — otherwise history_tokens would double-count them
            # when system_tokens is added back in the usage calculation,
            # diverging from session.get_context_pressure().
            try:
                history = max(
                    0,
                    chat_obj.interface.estimate_context_tokens() - sys_prompt - tools,
                )
            except Exception:
                history = 0
        else:
            history = 0

        system_tokens = sys_prompt + tools
        history_tokens = history

        # context_window comes from the live chat if available; otherwise
        # fall back to the agent's configured limit. On the very first
        # call of a turn (before ensure_session runs) chat_obj is None;
        # we still want real system/context tokens, just usage% may be
        # a sentinel if no limit is configured.
        if chat_obj is not None:
            limit = agent._config.context_limit or chat_obj.context_window()
        else:
            limit = agent._config.context_limit or 0
        usage = (system_tokens + history_tokens) / limit if limit > 0 else -1.0

        meta["context"] = {
            "system_tokens": system_tokens,
            "history_tokens": history_tokens,
            "usage": usage,
        }
    else:
        meta["context"] = {
            "system_tokens": -1,
            "history_tokens": -1,
            "usage": -1.0,
        }

    if drain_inbox:
        drained = _drain_inbox(agent)
        if drained:
            meta["notifications"] = drained

    return meta


def _drain_inbox(agent) -> list[str]:
    """Consume ``agent.inbox`` and return each message's content as a string.

    Returns an empty list when the agent has no inbox or the inbox is empty.
    Empty-content messages are preserved as empty strings — the count and
    ordering of inbox messages is information the agent may want.

    Full content per message — no truncation, no newline flattening. The
    JSON channel can carry it.

    Robust against agents without ``.inbox`` (legacy/test stand-ins): returns
    [] in that case.
    """
    inbox = getattr(agent, "inbox", None)
    if inbox is None:
        return []

    drained: list[str] = []
    while True:
        try:
            m = inbox.get_nowait()
        except queue.Empty:
            break
        content = getattr(m, "content", "")
        text = content if isinstance(content, str) else str(content)
        drained.append(text)
    return drained


def render_meta(agent, meta: dict) -> str:
    """Render the meta dict as the line prepended to text input.

    Returns '' when the meta dict is empty — callers should treat '' as
    "no prefix" and skip concatenation.

    Composes the existing ``system.current_time`` template plus a context
    fragment via ``system.context_breakdown`` (or ``system.context_unknown``
    when the session has not yet computed its token decomposition).

    Inbox notifications are intentionally not rendered here — they live in
    the user-turn body (drained by ``_concat_queued_messages`` upstream)
    or in the tool-result JSON (drained by the tool-result ``meta_fn``).
    """
    if not meta:
        return ""

    time_val = meta.get("current_time", "")
    ctx_val = _render_context_fragment(agent, meta)

    if time_val == "" and ctx_val == "":
        return ""

    return _t(
        agent._config.language,
        "system.current_time",
        time=time_val,
        ctx=ctx_val,
    )


def _render_context_fragment(agent, meta: dict) -> str:
    """Render the context sub-fragment for the text-input prefix.

    Returns:
        - '' if `context` is not present in ``meta``
        - the locale-specific "unknown" word when the sentinel (-1) is seen
        - the composed "{pct} (sys {sys} + ctx {ctx})" fragment otherwise
    """
    ctx = meta.get("context")
    if not ctx:
        return ""
    usage = ctx.get("usage", -1.0)
    if usage < 0:
        return _t(agent._config.language, "system.context_unknown")
    return _t(
        agent._config.language,
        "system.context_breakdown",
        pct=f"{usage * 100:.1f}%",
        sys=ctx.get("system_tokens", 0),
        ctx=ctx.get("history_tokens", 0),
    )


def stamp_meta(result: dict, meta: dict, elapsed_ms: int) -> dict:
    """Merge meta fields into a tool-result dict (in place) and return it.

    When ``meta`` is empty, neither the meta fields nor ``_elapsed_ms`` are
    written — matching the pre-existing behaviour of
    ``stamp_tool_result(time_awareness=False)`` exactly. This is deliberate:
    the spec originally claimed ``_elapsed_ms`` always writes, but preserving
    the old time-blind path means a time-blind agent's tool results stay
    free of any timing signal, not just wall-clock. Callers that want a
    timing-only stamp should pass a non-empty meta dict.

    ``_elapsed_ms`` lives here (rather than inside ``build_meta``) because
    it is a per-tool-call measurement — not per-turn agent state — and it
    would be wrong for the same value to appear on the text-input prefix.
    It is written unconditionally after the meta-key loop, so it always
    overrides any identically-named key in ``meta``.
    """
    if not meta:
        return result
    for k, v in meta.items():
        result[k] = v
    result["_elapsed_ms"] = elapsed_ms
    return result
