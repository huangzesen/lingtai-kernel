"""Unified per-turn metadata injection.

Single source of truth for "what the agent sees about its own runtime state
on every turn." Both injection sites — text-input prefix (in BaseAgent) and
tool-result stamp (in ToolExecutor) — read from here.

Curate carefully: every field added to `build_meta` ships on every text input
and every tool result.
"""
from __future__ import annotations

from .i18n import t as _t
from .time_veil import now_iso


def build_meta(agent) -> dict:
    """Return the current meta-data snapshot for the agent.

    Respects ``agent._config.time_awareness`` / ``timezone_awareness``
    internally; callers never need to special-case those flags.

    When the agent is time-blind and no other meta fields are curated in,
    returns ``{}``.

    Context-window fields (``system_tokens``, ``context_tokens``,
    ``context_usage``) are always emitted — the time veil does not cover
    token accounting. When the session's token decomposition has not yet
    run (dirty cache and no active chat), the three fields are emitted as
    ``-1`` / ``-1.0`` sentinels so callers can render "unknown" without
    ambiguity.
    """
    meta: dict = {}
    ts = now_iso(agent)
    if ts:
        meta["current_time"] = ts

    # Context-window decomposition. Only meaningful after the session has
    # run _update_token_decomposition at least once, which it does on first
    # token-tracking call (see SessionManager._track_usage).
    session = getattr(agent, "_session", None)
    chat_obj = getattr(session, "chat", None) if session is not None else None
    decomp_ran = session is not None and not session._token_decomp_dirty

    if decomp_ran and chat_obj is not None:
        sys_prompt = session._system_prompt_tokens
        ctx_section = session._context_section_tokens
        tools = session._tools_tokens
        history = chat_obj.interface.estimate_context_tokens()

        # The "system" bucket in the meta line is everything that is NOT
        # accumulated memory: the prompt floor (minus the context section)
        # plus the tool schemas. max(0, ...) guards against tokenizer
        # underflow if the context section happens to outweigh the full
        # prompt estimate (shouldn't happen, but defensive).
        system_tokens = max(0, sys_prompt - ctx_section) + tools
        context_tokens = ctx_section + history

        limit = agent._config.context_limit or chat_obj.context_window()
        usage = (system_tokens + context_tokens) / limit if limit > 0 else -1.0

        meta["system_tokens"] = system_tokens
        meta["context_tokens"] = context_tokens
        meta["context_usage"] = usage
    else:
        meta["system_tokens"] = -1
        meta["context_tokens"] = -1
        meta["context_usage"] = -1.0

    return meta


def render_meta(agent, meta: dict) -> str:
    """Render the meta dict as the line prepended to text input.

    Returns '' when the meta dict is empty — callers should treat '' as
    "no prefix" and skip concatenation.

    Composes the existing ``system.current_time`` template (now
    extended with a context slot) plus a context fragment via
    ``system.context_breakdown`` (or ``system.context_unknown`` when the
    session has not yet computed its token decomposition).
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
        - '' if `context_usage` is not present in ``meta``
        - the locale-specific "unknown" word when the sentinel (-1) is seen
        - the composed "{pct} (sys {sys} + ctx {ctx})" fragment otherwise
    """
    if "context_usage" not in meta:
        return ""
    usage = meta["context_usage"]
    if usage < 0:
        return _t(agent._config.language, "system.context_unknown")
    return _t(
        agent._config.language,
        "system.context_breakdown",
        pct=f"{usage * 100:.1f}%",
        sys=meta.get("system_tokens", 0),
        ctx=meta.get("context_tokens", 0),
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
