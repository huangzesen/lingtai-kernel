"""Unified per-turn metadata injection.

Single source of truth for "what the agent sees about its own runtime state
on every turn." Both injection sites — text-input prefix (in BaseAgent) and
tool-result stamp (in ToolExecutor) — read from here.

Curate carefully: every field added to `build_meta` ships on every text input
and every tool result.
"""
from __future__ import annotations

from .time_veil import now_iso


def build_meta(agent) -> dict:
    """Return the current meta-data snapshot for the agent.

    Respects ``agent._config.time_awareness`` / ``timezone_awareness``
    internally; callers never need to special-case those flags.

    When the agent is time-blind and no other meta fields are curated in,
    returns ``{}``.
    """
    meta: dict = {}
    ts = now_iso(agent)
    if ts:
        meta["current_time"] = ts
    return meta
