"""Unified per-turn metadata injection.

Single source of truth for "what the agent sees about its own runtime state
on every turn." Both injection sites — text-input prefix (in BaseAgent) and
tool-result stamp (in ToolExecutor) — read from here.

Curate carefully: every field added to `build_meta` ships on every text input
and every tool result.

The canonical runtime sidecar has exactly two model-visible axes:

- ``_meta.tool_meta`` — immutable per-result execution facts, written once by
  ``ToolExecutor._attach_tool_block`` and never moved.
- ``_meta.agent_meta`` — one coherent current main-agent snapshot. It is
  attached to the designated final result every batch; older snapshots remain
  historical traces.
- ``agent_meta.guidance`` — a lightweight ref/hook pointing at the resident
  ``meta_guidance`` system-prompt section (built by ``build_meta_guidance``),
  where the full kernel guidance sections, the ``_meta`` readme, and any static
  adapter runtime rules now live.  The full ordered appendix is no longer
  re-stamped on every tail result.  It rides with ``agent_meta`` on the
  designated final result.
- ``agent_meta.notifications`` / ``agent_meta.guidance.transient`` — the
  notification portion of that same current snapshot. Delivery fingerprints
  are independent and never suppress the current snapshot.

Channel encoding:
- Tool-result channel: the executor captures runtime state on a non-serialized
  ToolResultBlock field, which ``attach_active_runtime`` promotes into the final
  block's complete ``agent_meta`` sidecar whenever private capture exists.
  ``attach_active_notifications`` merges the current channel-owned notification
  payload into that same final snapshot; delivery fingerprints are diagnostic
  and compatibility state, not gates on snapshot attachment.
- Text-input channel: `render_meta` formats the same dict into a prose
  prefix line. Inbox content is NOT rendered here — it lives in the
  user-turn body, drained by ``_concat_queued_messages`` upstream.

Daemon sessions use ``attach_daemon_agent_meta`` for the same canonical envelope
and latest-snapshot rule, but supply only daemon-local runtime/token/context state;
parent notification and communication state is never projected into a daemon.

As of 2026-05-02, the meta block no longer carries inbox-drained
notifications. System-source notifications (mail arrival, bounce, future
MCP events) are now delivered as synthetic notification(action="check")
tool-call pairs spliced by ``BaseAgent._inject_notification_pair`` (the
legacy ``tc_inbox`` splice path is dormant); see
docs/plans/2026-05-02-system-notification-as-tool-call.md.
"""
from __future__ import annotations

import hashlib as _hashlib
import json as _json
import copy as _copy
import os
import time as _time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, NamedTuple

from ._fsutil import atomic_write_json
from .config import (
    CONTEXT_PRESSURE_HIGH_RATIO,
    CONTEXT_PRESSURE_FORCED_REBUILD_RATIO,
    CONTEXT_PRESSURE_RECOVERY_TARGET,
    system_prompt_pressure_ratio,
)
from .i18n import t as _t
from .reminders.context_pressure import (
    current_molt_emission_descriptor,
    render_current_molt_context,
    render_forced_rebuild_failed_warning,
    render_forced_rebuild_warning,
    render_reconstruction_molt,
)
from .time_veil import now_iso

# ---------------------------------------------------------------------------
# The single ``_meta`` envelope key and its two nested axes.  Every projected
# main-agent tool result carries only ``tool_meta`` and ``agent_meta`` beneath
# ``result["_meta"]``; notification and guidance ownership is nested below the
# latter axis.
#   * ``tool_meta``            — immutable, per-result (every tool result)
#   * ``agent_meta``           — current agent state,
#                                notifications, and guidance
# ---------------------------------------------------------------------------
META_ENVELOPE_KEY = "_meta"
TOOL_META_KEY = "tool_meta"
AGENT_META_KEY = "agent_meta"
GUIDANCE_KEY = "guidance"
NOTIFICATIONS_KEY = "notifications"
NOTIFICATION_GUIDANCE_KEY = "notification_guidance"
NOTIFICATION_PERSISTENT_KEY = "notification_persistent"
AGENT_META_INSTRUCTION = (
    "Only the latest agent_meta in conversation is current; older ones are "
    "historical traces."
)
# Telegram lives under an `mcp` namespace level to mirror the ephemeral
# `notifications.mcp.telegram` shape and match Jason #6148: the required path is
# `_meta.agent_meta.notifications.persistent.mcp.telegram` (NOT
# `...notifications.persistent.telegram`).
NOTIFICATION_PERSISTENT_MCP_KEY = "mcp"
NOTIFICATION_PERSISTENT_TELEGRAM_CHANNEL = "telegram"
# Full dotted path used in hook comments / docs so both the string and the
# structure stay in sync.
NOTIFICATION_PERSISTENT_TELEGRAM_PATH = (
    f"_meta.{AGENT_META_KEY}.{NOTIFICATIONS_KEY}.persistent."
    f"{NOTIFICATION_PERSISTENT_MCP_KEY}.{NOTIFICATION_PERSISTENT_TELEGRAM_CHANNEL}"
)
NOTIFICATION_PERSISTENT_TELEGRAM_MIN_CONTEXT = 20
NOTIFICATION_PERSISTENT_TELEGRAM_SEEN_LIMIT = 200

NOTIFICATION_PERSISTENT_EMAIL_CHANNEL = "email"
NOTIFICATION_PERSISTENT_EMAIL_PATH = (
    f"_meta.{NOTIFICATION_PERSISTENT_KEY}.{NOTIFICATION_PERSISTENT_EMAIL_CHANNEL}"
)

# WeChat mirrors the Telegram persistent lane at
# `_meta.agent_meta.notifications.persistent.mcp.wechat`. Its producer preview window is
# 10 messages (vs Telegram's 20), so the seed/delta boundary matches that
# producer window instead of Telegram's.
NOTIFICATION_PERSISTENT_WECHAT_CHANNEL = "wechat"
NOTIFICATION_PERSISTENT_WECHAT_PATH = (
    f"_meta.{AGENT_META_KEY}.{NOTIFICATIONS_KEY}.persistent."
    f"{NOTIFICATION_PERSISTENT_MCP_KEY}.{NOTIFICATION_PERSISTENT_WECHAT_CHANNEL}"
)
NOTIFICATION_PERSISTENT_WECHAT_MIN_CONTEXT = 10
NOTIFICATION_PERSISTENT_WECHAT_SEEN_LIMIT = 200

# Feishu mirrors the Telegram/WeChat persistent lane at
# `_meta.agent_meta.notifications.persistent.mcp.feishu`. The Feishu producer's structured
# preview carries the last 10 conversation messages
# (FeishuManager._build_conversation_preview_and_metadata), so the seed/delta
# boundary matches that window rather than Telegram's 20.
NOTIFICATION_PERSISTENT_FEISHU_CHANNEL = "feishu"
NOTIFICATION_PERSISTENT_FEISHU_PATH = (
    f"_meta.{AGENT_META_KEY}.{NOTIFICATIONS_KEY}.persistent."
    f"{NOTIFICATION_PERSISTENT_MCP_KEY}.{NOTIFICATION_PERSISTENT_FEISHU_CHANNEL}"
)
NOTIFICATION_PERSISTENT_FEISHU_MIN_CONTEXT = 10
NOTIFICATION_PERSISTENT_FEISHU_SEEN_LIMIT = 200

# WhatsApp lives at `_meta.agent_meta.notifications.persistent.mcp.whatsapp` but runs the
# shared IM lane in snapshot mode (email-style): every block carries the
# producer's current bounded context in full, with no delivered-id delta
# tracking and no previous_block hook, so it has no min-context/seen-limit
# tuning knobs.
NOTIFICATION_PERSISTENT_WHATSAPP_CHANNEL = "whatsapp"

# Resident Task Card axis: the agent-local ``taskcard/taskcard.md`` +
# ``taskcard/status`` artifact is projected into ``_meta.agent_meta.taskcard``
# so the human and the agent always see the same card. It is change-gated:
# identical bytes are not re-injected every turn. A body longer than
# ``TASKCARD_MAX_CHARS`` is refused rather than truncated.
TASKCARD_KEY = "taskcard"
TASKCARD_MAX_CHARS = 2000
TASKCARD_ABSENT_HINT = (
    "no taskcard present, consider maintaining one, see task_card manual"
)
TASKCARD_REFUSED_HINT = (
    "taskcard refused: body exceeds the {max}-char cap; keep the card a "
    "progressive-disclosure summary and move complex progress into files"
)
TASKCARD_UNREADABLE_HINT = (
    "taskcard present but unreadable; inspect taskcard/status and "
    "taskcard/taskcard.md before maintaining a new one"
)

NOTIFICATION_PERSISTENT_WHATSAPP_PATH = (
    f"_meta.{NOTIFICATION_PERSISTENT_KEY}."
    f"{NOTIFICATION_PERSISTENT_MCP_KEY}.{NOTIFICATION_PERSISTENT_WHATSAPP_CHANNEL}"
)

# Concise English comments attached to the Telegram persistent block so the
# agent can read the block without re-deriving structure. Kept as module-level
# constants so tests and docs can assert the exact wording.
NOTIFICATION_PERSISTENT_TELEGRAM_BURST_COMMENT = (
    "Multiple new Telegram messages arrived together; treat them as one burst "
    "and answer the combined intent."
)
NOTIFICATION_PERSISTENT_TELEGRAM_SELF_OUTGOING_COMMENT = (
    "This is the agent's own recent outgoing message, included for continuity."
)
NOTIFICATION_PERSISTENT_TELEGRAM_TRUNCATED_COMMENT = (
    "This message is truncated; call telegram.read for the exact full producer "
    "state."
)
NOTIFICATION_PERSISTENT_TELEGRAM_REFERENCED_COMMENT = (
    "This is the full Telegram message referenced by the current reply; "
    "included because it is not present in messages."
)

# WeChat mirrors the Telegram comment set with channel-appropriate wording.
# The truncation comment points at wechat.read because the WeChat producer's
# local inbox/sent records are the exact source-of-truth state.
NOTIFICATION_PERSISTENT_WECHAT_BURST_COMMENT = (
    "Multiple new WeChat messages arrived together; treat them as one burst "
    "and answer the combined intent."
)
NOTIFICATION_PERSISTENT_WECHAT_SELF_OUTGOING_COMMENT = (
    "This is the agent's own recent outgoing message, included for continuity."
)
NOTIFICATION_PERSISTENT_WECHAT_TRUNCATED_COMMENT = (
    "This message is truncated; call wechat.read for the exact full producer "
    "state."
)

# Feishu mirrors the Telegram comment set with channel-appropriate wording.
# The truncation comment points at feishu.read because the Feishu producer's
# local store is the exact source-of-truth state.
NOTIFICATION_PERSISTENT_FEISHU_BURST_COMMENT = (
    "Multiple new Feishu messages arrived together; treat them as one burst "
    "and answer the combined intent."
)
NOTIFICATION_PERSISTENT_FEISHU_SELF_OUTGOING_COMMENT = (
    "This is the agent's own recent outgoing message, included for continuity."
)
NOTIFICATION_PERSISTENT_FEISHU_TRUNCATED_COMMENT = (
    "This message is truncated; call feishu.read for the exact full producer "
    "state."
)

# Concise English comments attached to the WhatsApp persistent block. The
# WhatsApp lane runs in snapshot mode (email-style): each block carries the
# producer's current structured context in full, with no delivered-id delta
# tracking, so the comments focus on producer authority and the Cloud API
# reply rules rather than block-to-block continuity.
NOTIFICATION_PERSISTENT_WHATSAPP_CONTEXT_COMMENT = (
    "Durable WhatsApp context moved here from the legacy input path "
    "_meta.agent_meta.notifications.attention.mcp.whatsapp. The canonical path is "
    "_meta.agent_meta.notifications.persistent.mcp.whatsapp. "
    "The whatsapp tool remains the source of truth: building this block marks "
    "nothing read — use whatsapp.read/check for exact producer state. Reply on "
    "WhatsApp when the message arrived through WhatsApp (whatsapp.reply with "
    "the compound message id, or whatsapp.send); free-form business replies "
    "are allowed only inside the 24-hour customer-service window — outside it "
    "use an approved WhatsApp message template."
)
NOTIFICATION_PERSISTENT_WHATSAPP_SELF_OUTGOING_COMMENT = (
    "This is the agent's own recent outgoing message, included for continuity."
)
NOTIFICATION_PERSISTENT_WHATSAPP_TRUNCATED_COMMENT = (
    "This message is truncated; call whatsapp.read with the compound message "
    "id for the exact full producer state."
)
NOTIFICATION_PERSISTENT_WHATSAPP_MEDIA_COMMENT = (
    "Non-text WhatsApp message; only type/id metadata is stored locally — use "
    "whatsapp.read for the exact stored producer state."
)

NOTIFICATION_PERSISTENT_EMAIL_CONTEXT_COMMENT = (
    "Unread email content moved here from the legacy input path "
    "_meta.agent_meta.notifications.attention.email. The canonical path is "
    "_meta.agent_meta.notifications.persistent.email. Bodies "
    "are injected in full up to the 50,000 character send-layer limit; prefer "
    "email.dismiss after handling content, and use email.read/reply for "
    "source-of-truth actions."
)
NOTIFICATION_PERSISTENT_EMAIL_TRUNCATED_COMMENT = (
    "This legacy email body exceeded the current 50,000 character send-layer "
    "limit and was capped in the persistent notification lane. New oversize "
    "email sends are rejected."
)

# Hard cap on the model-visible persistent notification envelope
# (``{"notification_persistent": ...}`` as serialized for provider context).
# A busy hub agent (many unread emails plus several IM lanes) could otherwise
# re-serialize a 20-40k character block into every turn, so context grows fast
# and every provider call pays a large cache miss.  Over the cap the FULL block
# is spilled to a file under the agent's ``logs/`` directory and the
# model-visible copy is compacted until it fits; message ids are never dropped
# so delivery tracking still sees every message.  Payloads at or under the cap
# are returned completely unchanged (no spill file, no marker).
#
# The cap is live-readable from ``LINGTAI_NOTIFICATION_MAX_CHARS`` (a positive
# integer, clamped to the 10,000 ceiling so the context-size fix cannot be
# disabled by accident); unset or invalid values fall back to this default.
NOTIFICATION_PERSISTENT_MAX_CHARS = 10_000
NOTIFICATION_PERSISTENT_MAX_CHARS_CEILING = 10_000
# Shared floor for BOTH notification lanes: a positive configured value below
# this is clamped UP to it so the terminal marker-only recovery envelope
# (which carries an absolute spill path) can always fit inside the cap.  The
# cap remains a strict upper bound on the model-visible envelope; the floor
# only guarantees the deterministic degradation can always express the
# recovery handle.  When even this floor cannot fit an absolute spill path
# (pathologically long workdir), the final guard strips the marker path and
# the exact spill basename (including any ``-N`` suffix) still identifies the
# file.
NOTIFICATION_PERSISTENT_MAX_CHARS_MIN = 2048
NOTIFICATION_PERSISTENT_MAX_CHARS_ENV = "LINGTAI_NOTIFICATION_MAX_CHARS"
NOTIFICATION_PERSISTENT_OVERFLOW_KEY = "overflow"
NOTIFICATION_PERSISTENT_OVERFLOW_FILE_PREFIX = "notification-overflow-"
# Heavy free-text fields compacted first, per lane family.  Structural fields
# (ids, routing, subjects, dates) are never touched.
NOTIFICATION_PERSISTENT_EMAIL_HEAVY_FIELDS = (
    "message",
    "body",
    "text",
    "preview",
    "content",
)
NOTIFICATION_PERSISTENT_IM_HEAVY_FIELDS = ("text", "caption")
# Successively tighter per-field character budgets, tried in order until the
# envelope fits.
NOTIFICATION_PERSISTENT_COMPACT_BUDGETS = (200, 100, 50, 0)
# Per-record comments are repeated once per email, so they stay short: the
# cap is a context-size fix and a long comment would spend the budget it saves.
NOTIFICATION_PERSISTENT_OVERFLOW_COMMENT = (
    "Truncated by the notification block size cap; full content in {path}."
)
NOTIFICATION_PERSISTENT_OVERFLOW_NO_SPILL_COMMENT = (
    "Truncated by the notification block size cap; the overflow file could not "
    "be written — use the producer tool for the full content."
)

# Hard cap on the model-visible *attention* notification lane
# (``_meta.agent_meta.notifications.attention``) — the transient per-channel
# routing payload that is re-stamped on every eligible tool batch and on every
# IDLE/ASLEEP synthesized pair.  A busy hub agent (many unread emails plus
# several IM lanes) could otherwise re-serialize a multi-ten-thousand-character
# attention payload into every provider call, growing context fast and paying a
# large cache miss.  Over the cap the FULL attention payload is spilled to a
# file under the agent's ``logs/`` directory and the model-visible copy is
# compacted until it fits, with an ``overflow`` marker that points the agent at
# the file (or the producer tool when the spill fails).  Payloads at or under
# the cap are returned completely unchanged (no spill file, no marker).
#
# The cap shares the ``LINGTAI_NOTIFICATION_MAX_CHARS`` env bar with the
# persistent lane (a positive integer, clamped to the 10,000 ceiling so the
# context-size fix cannot be disabled by accident); unset or invalid values
# fall back to this default.
NOTIFICATION_ATTENTION_MAX_CHARS = 10_000
NOTIFICATION_ATTENTION_MAX_CHARS_CEILING = 10_000
# Documented floor for the attention lane: a positive configured value below
# this is clamped UP to it so the terminal marker-only recovery envelope
# (which carries an absolute spill path) can always fit inside the cap.  The
# cap remains a strict upper bound on the model-visible envelope; the floor
# only guarantees the deterministic degradation can always express the
# recovery handle.  When even this floor cannot fit an absolute spill path
# (pathologically long workdir), the final guard strips the marker path and
# the deterministic content-addressed spill name still identifies the file.
NOTIFICATION_ATTENTION_MAX_CHARS_MIN = 2048
NOTIFICATION_ATTENTION_MAX_CHARS_ENV = "LINGTAI_NOTIFICATION_MAX_CHARS"
NOTIFICATION_ATTENTION_OVERFLOW_KEY = "overflow"
NOTIFICATION_ATTENTION_OVERFLOW_FILE_PREFIX = "notification-attention-overflow-"
# Heavy free-text fields compacted first, regardless of lane family.  Structural
# fields (ids, routing, counts, subjects, dates) are never touched.
NOTIFICATION_ATTENTION_HEAVY_FIELDS = (
    "text",
    "message",
    "body",
    "preview",
    "content",
    "summary",
    "caption",
    "detail",
    "instruction",
)
# Successively tighter per-field character budgets, tried in order until the
# attention envelope fits.
NOTIFICATION_ATTENTION_COMPACT_BUDGETS = (200, 100, 50, 0)
NOTIFICATION_ATTENTION_OVERFLOW_COMMENT = (
    "Full notification content exceeds the attention block size cap; read "
    "{path} for the complete payload."
)
NOTIFICATION_ATTENTION_OVERFLOW_NO_SPILL_COMMENT = (
    "Full notification content exceeds the attention block size cap and the "
    "overflow file could not be written — use the producer tool for the full "
    "content."
)
# Recovery comment for the terminal guard: the spill SUCCEEDED but the
# absolute spill path is too long to fit the capped envelope, so the marker
# path is stripped (``path_omitted``) and the exact spill basename (the
# deterministic content-addressed name, including any ``-N`` suffix) still
# locates the file on disk.
NOTIFICATION_ATTENTION_OVERFLOW_PATH_OMITTED_COMMENT = (
    "Full notification content exceeds the attention block size cap; the spill "
    "path is omitted (too long for the cap) - read the content-addressed "
    "logs/{name} in the agent workdir "
    "logs/ for the complete payload, or use the producer tool."
)
# The attention spill identity is CONTENT-ADDRESSED: the file name embeds a
# short sha256 digest of the lane's canonical serialization, so an unchanged
# oversized payload (re-stamped every tool batch + IDLE pair) reuses the SAME
# file instead of re-spilling every batch.  The file is created with an
# exclusive ``os.link`` from a unique sibling temp, so a two-writer race can
# never overwrite an existing recovery handle.
NOTIFICATION_ATTENTION_OVERFLOW_DIGEST_LENGTH = 8
# Bounded head kept when pathological stub compaction bounds id lists.
NOTIFICATION_ATTENTION_ROUTING_ID_HEAD = 8
# Bounded exclusive-allocate attempts when a content-address collision blocks
# the primary name (practically impossible; mirrors the persistent lane's
# 100-attempt bound).  Never overwrites an existing spill file.
NOTIFICATION_ATTENTION_SPILL_SUFFIX_ATTEMPTS = 100

# Per-result machine-generated guidance nested under ``tool_meta``.  ``comment``
# is a small map of topic-keyed hints; today the only topic is ``overflow`` — a
# hint stamped on capped/large visible tool results pointing the agent at the
# preserved original and the cleanup action.  It is guidance, not a
# notification, not global guidance, and not a strict state machine: a quiet
# per-result note that rides on the permanent ``tool_meta`` block.
TOOL_META_COMMENT_KEY = "comment"
TOOL_META_COMMENT_OVERFLOW_KEY = "overflow"
TOOL_META_TOKEN_USAGE_KEY = "token_usage"
TOOL_META_TOKEN_USAGE_PENDING_KEY = "_tool_meta_token_usage"
# The two nested halves of ``agent_meta.agent_state.token_usage``.  ``current_call`` carries
# ONLY this result's own provider-call token/cache/output fields; ``session``
# carries the since-last-molt cumulative aggregate (surviving refresh) plus the
# current context state.  Splitting them into named sub-objects (vs the former
# single flat dict) removes the confusing flat ``input`` vs ``input_tokens``
# adjacency — see :func:`build_tool_meta_token_usage`.
TOKEN_USAGE_CURRENT_CALL_KEY = "current_call"
TOKEN_USAGE_SESSION_KEY = "session"
TOOL_META_CURRENT_TIME_KEY = "current_time"
# Current sustained-pressure molt reminder — permanent per-result metadata at
# ``agent_meta.agent_state.context.molt``. ``build_meta`` stashes the reminder
# under the transit key and carries the emission-event descriptor under the event
# transit key while
# active; ``ToolExecutor._attach_tool_block`` pops both — promoting the reminder
# into the final ``agent_meta.agent_state`` snapshot and logging with per-round dedup.
TOOL_META_CONTEXT_KEY = "context"
TOOL_META_CONTEXT_PENDING_KEY = "_tool_meta_context"
TOOL_META_CONTEXT_EVENT_PENDING_KEY = "_tool_meta_context_event"
TOOL_META_CONTEXT_REBUILD_KEY = "rebuild"

# Rendered system-prompt size pressure warning — a stable, non-colliding key
# alongside ``rebuild``/``molt`` under ``agent_meta.agent_state.context``. See
# :func:`build_system_prompt_pressure_context`.
TOOL_META_CONTEXT_SYSTEM_PROMPT_KEY = "system_prompt"

# Cache-miss budget guard — the two compact numeric fields surfaced under
# ``agent_meta.agent_state.context`` alongside the ``molt`` warning when the current-session
# cache-miss total reaches/exceeds the configured budget (see
# :func:`build_cache_miss_budget_context`).  They ride the SAME
# ``_tool_meta_context`` transit sub-object as the sustained-pressure ``molt``
# reminder, so ``ToolExecutor._attach_tool_block`` promotes them into the
# final ``agent_meta.agent_state.context`` snapshot in one step.
TOOL_META_CONTEXT_CACHE_MISS_BUDGET_KEY = "cache_miss_budget"
TOOL_META_CONTEXT_CACHE_MISS_TOKENS_KEY = "cache_miss_tokens"

# Always-on since-last-molt cache-miss/budget telemetry surfaced inside the
# ``session`` (since-last-molt cumulative) half of ``agent_meta.agent_state.token_usage`` (see
# :func:`_build_session_token_economy`).  Unlike the ``agent_state.context`` guard
# above — which appears ONLY once the session cache-miss total reaches/exceeds
# the budget — these three fields ride on EVERY result whenever the session
# aggregate token usage is available, so an agent can always read its current
# cumulative cache miss and how much budget remains without recomputing
# ``input_tokens - cached_tokens`` or remembering the default budget:
#   * ``cache_miss_tokens``            = max(input_tokens - cached_tokens, 0)
#   * ``cache_miss_budget``            = one effective positive budget resolved
#                                        by the outer Agent hook, or the fixed
#                                        compatibility fallback
#   * ``cache_miss_remaining_tokens``  = max(cache_miss_budget - cache_miss_tokens, 0)
# ``cache_miss_tokens`` — derivable from session data alone — is always emitted
# with the session half.
TOKEN_USAGE_CACHE_MISS_TOKENS_KEY = "cache_miss_tokens"
TOKEN_USAGE_CACHE_MISS_BUDGET_KEY = "cache_miss_budget"
TOKEN_USAGE_CACHE_MISS_REMAINING_KEY = "cache_miss_remaining_tokens"

# Fixed compatibility fallback for a bare kernel Agent, a missing outer hook, or
# any hook failure/invalid return. Concrete source and settings ownership remains
# outside Core.
CACHE_MISS_BUDGET_DEFAULT = 2_000_000

# Current context state carried under the ``session`` half of
# ``agent_meta.agent_state.token_usage`` (moved off ``current_call``, since context usage is
# current session/context state, not this provider call's own facts).  Emitted
# only when resolvable: ``context_tokens`` from the cumulative
# ``get_token_usage().ctx_total_tokens``; ``context_window`` from the provider
# snapshot's ``context_window`` or the configured/live window; ``context_usage``
# = ``context_tokens / context_window`` when both are positive.
TOKEN_USAGE_CONTEXT_TOKENS_KEY = "context_tokens"
TOKEN_USAGE_CONTEXT_WINDOW_KEY = "context_window"
TOKEN_USAGE_CONTEXT_USAGE_KEY = "context_usage"


def build_tool_meta_overflow_comment(tool_call_id: str | None) -> dict:
    """Return the ``tool_meta.comment.overflow`` hint for a capped/large result.

    Stamped only when the model-visible payload is capped or large (the caller
    decides; see :meth:`ToolExecutor._attach_tool_block`).  LingTai preserves the
    full, un-capped original in the durable runtime log, so the hint points there
    by ``tool_call_id`` rather than at any external sidecar/saved-path file.

    There is deliberately exactly one comment topic for this feature —
    ``overflow``.  All guidance (what happened, where the original is, how to
    retrieve it, what to do after consuming it) lives under this single key, not
    split across parallel ``comment.retrieval`` / ``comment.summarize`` headings.
    """
    call_id = tool_call_id or "<unknown>"
    return {
        "summary": (
            "The model-visible context for this tool result is capped or large; "
            "what you see here may be a preview or compacted form, not the full payload."
        ),
        "full_original": (
            f"The full original is preserved in logs/events.jsonl under "
            f"tool_call_id={call_id}."
        ),
        "how_to_retrieve": (
            f"Retrieve it from the durable log by tool_call_id: "
            f"grep '{call_id}' <workdir>/logs/events.jsonl, or use "
            f"`lingtai-agent log query` (see the sqlite-log-query manual). For a "
            f"broad extraction, delegate to a daemon/subagent with the "
            f"tool_call_id and the exact question instead of pulling the whole "
            f"original back into your own context."
        ),
        "after_consuming": (
            "After you have consumed what you need, call "
            "system(action=\"summarize\") for this tool_call_id to replace the "
            "visible payload with your own agent-authored summary."
        ),
    }

# Keys that are kernel/runtime scaffolding, not the formal tool-result payload.
# Summarize and the current_tool_result_chars char-ranking must ignore these so
# notification or guidance text is not treated as result content to be summarized
# or counted toward a result's size.
FORMAL_TOOL_RESULT_EXCLUDED_KEYS = frozenset({
    META_ENVELOPE_KEY,
    "_advisory",
    "active_turn_tool_calls",
    "active_turn_tool_call_notice",
})


def formal_tool_result_content(content):
    """Return the formal tool-result payload, excluding kernel metadata.

    The ``_meta`` envelope can contain notifications and guidance that are
    channel/runtime state, not the payload returned by the tool.  Context
    summarization and the ``current_tool_result_chars`` char-ranking operate on
    this formal body only, so notification contents are neither size-counted nor
    summarized as if they were the result.
    """
    if not isinstance(content, dict):
        return content
    return {
        key: value
        for key, value in content.items()
        if key not in FORMAL_TOOL_RESULT_EXCLUDED_KEYS
    }


def _visible_content_text(content) -> str:
    if isinstance(content, str):
        return content
    try:
        return _json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def formal_tool_result_visible_len(content) -> int:
    """Visible character length of the formal tool-result payload only."""
    return len(_visible_content_text(formal_tool_result_content(content)))


def formal_tool_result_preview(content, limit: int = 200) -> str:
    """Preview string for the formal tool-result payload only."""
    if limit <= 0:
        return ""
    return _visible_content_text(formal_tool_result_content(content))[:limit]



def _is_tool_result_block(block) -> bool:
    """Best-effort duck-typing for ToolResultBlock without a hard import cycle."""
    return block.__class__.__name__ == "ToolResultBlock" and hasattr(block, "content")


def _iter_history_tool_result_blocks(agent):
    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    interface = getattr(chat, "interface", None)
    entries = getattr(interface, "_entries", None)
    if not entries:
        return
    for entry in entries:
        for block in getattr(entry, "content", ()) or ():
            if _is_tool_result_block(block):
                yield block


def adapter_comment(agent):
    """Return an optional adapter-authored, agent-facing runtime note."""

    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    comment_fn = getattr(chat, "adapter_comment", None)
    if not callable(comment_fn):
        return None
    try:
        return comment_fn()
    except Exception:
        # `_meta.agent_meta` must never be made unavailable by an adapter note.
        return None


def static_adapter_comment(agent):
    """Return the adapter's static/rule-like runtime note (no dynamic state).

    The static comment is the durable explanation of how the active adapter's
    continuation/caching/summarize machinery behaves; it does not change turn to
    turn.  It is rendered once into the resident ``meta_guidance`` system-prompt
    section rather than re-stamped onto every tail ``_meta``.  Adapters expose it
    via a ``static_adapter_comment`` method; adapters without one simply
    contribute nothing to ``meta_guidance``.  Prefer the service/adapter-level
    hook because the first prompt build happens before a ChatSession exists; the
    chat-level hook remains as a compatibility fallback.
    """
    service = getattr(agent, "service", None)
    comment_fn = getattr(service, "static_adapter_comment", None)
    if callable(comment_fn):
        try:
            comment = comment_fn()
        except Exception:
            comment = None
        if comment:
            return comment

    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    comment_fn = getattr(chat, "static_adapter_comment", None)
    if not callable(comment_fn):
        return None
    try:
        return comment_fn()
    except Exception:
        return None


def dynamic_adapter_comment(agent: object) -> Mapping[str, Any] | None:
    """Return adapter-owned dynamic tail state for ``_meta.agent_meta``.

    Adapters that can separate static guidance from dynamic runtime state should
    implement ``dynamic_adapter_comment``.  For legacy adapters, fall back to the
    combined ``adapter_comment`` payload; the generic tail slimmer will only
    trim oversized structures, not guess adapter-specific static keys.
    """
    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    comment_fn = getattr(chat, "dynamic_adapter_comment", None)
    if callable(comment_fn):
        try:
            comment = comment_fn()
        except Exception:
            comment = None
        if comment:
            if not isinstance(comment, Mapping):
                return {"note": str(comment)}
            return dict(comment)
    return adapter_comment(agent)


def slim_adapter_comment_for_tail(
    comment: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Trim dynamic adapter tail payload without guessing static keys.

    Static-vs-dynamic partitioning is owned by the adapter via
    ``static_adapter_comment`` / ``dynamic_adapter_comment``.  The kernel only
    removes verbose dynamic structures that are too heavy for every-turn tail
    metadata and adds a hook back to the resident ``meta_guidance`` section.
    """
    if not comment:
        return None
    if not isinstance(comment, Mapping):
        return {"note": str(comment)}

    slim: dict[str, Any] = dict(comment)
    ledger = slim.pop("cache_ledger", None)
    if isinstance(ledger, Mapping):
        summary = ledger.get("summary")
        if isinstance(summary, Mapping) and "cache_ledger_summary" not in slim:
            slim["cache_ledger_summary"] = dict(summary)
        last_full = ledger.get("last_full")
        if isinstance(last_full, Mapping):
            slim.setdefault("last_full_api_calls_ago", last_full.get("api_calls_ago"))
            slim.setdefault("last_full_reason", last_full.get("reason"))
        last_ws_full = ledger.get("last_ws_full")
        if isinstance(last_ws_full, Mapping):
            slim.setdefault(
                "last_ws_full_api_calls_ago",
                last_ws_full.get("api_calls_ago"),
            )
            slim.setdefault("last_ws_full_reason", last_ws_full.get("reason"))

    hint = slim.get("maintenance_hint")
    if isinstance(hint, Mapping):
        compact_hint = dict(hint)
        compact_hint.pop("reason", None)
        if compact_hint:
            slim["maintenance_hint"] = compact_hint
        else:
            slim.pop("maintenance_hint", None)

    return slim or None


TOOL_RESULT_CHARS_TOP_N = 5
TOOL_RESULT_CHARS_MIN_TOP_CHARS = 1000
# Fallback large-result hint threshold (chars) used by current_tool_result_chars
# when the agent has no ``_summarize_notification_threshold`` set.  Mirrors
# BaseAgent's default and messaging.DEFAULT_SUMMARIZE_NOTIFICATION_THRESHOLD;
# kept local to avoid a base_agent import cycle.
DEFAULT_LARGE_RESULT_THRESHOLD = 3000
TOOL_RESULT_CHARS_README = (
    "listing top 5 tool results over 1000 chars by char count "
    "(id, tool_name, chars; no preview); no need to summarize this helper "
    "(it rides on agent_meta; read the current final-carrier snapshot for the "
    "current list); these are summarize candidates, "
    "not a directive to summarize "
    "every entry: prefer summarizing prior results that are already "
    "consumed/digested and useless, irrelevant, obsolete, or no longer needed "
    "in full, weighing context pressure, recoverability from logs, and future "
    "reuse/token savings, and batch them by the listed ids/tool names; if an "
    "adapter comment is present, follow its adapter-specific summarize rules too"
)


def _tool_result_id(block) -> str:
    return str(getattr(block, "id", None) or getattr(block, "tool_call_id", None) or "")


def _tool_result_name(block) -> str:
    return str(getattr(block, "name", None) or getattr(block, "tool_name", None) or "")


def current_tool_result_chars(agent, extra_results=()) -> dict:
    """Return current context-visible formal tool-result char summary.

    The count is intentionally based on formal result payloads rather than
    runtime metadata.  ``_meta`` notifications/guidance, transient scaffolding,
    and other non-payload fields are excluded by
    ``formal_tool_result_visible_len``.  ``extra_results`` lets latest-result
    stamping include the just-created tool-result batch before those blocks are
    appended to chat history.

    The returned dict also carries ``threshold`` (the agent's configured
    large-result hint threshold in chars) and ``over_threshold_count`` (how many
    in-context formal results exceed it).  Together with ``top_results`` these
    let the agent see what counts as "large" and how many candidates exist —
    the context the removed ``large_tool_result`` notification used to carry —
    so it can decide what to ``context(action="summarize")``.
    """
    threshold = getattr(
        agent, "_summarize_notification_threshold", DEFAULT_LARGE_RESULT_THRESHOLD
    )
    total = 0
    over_threshold_count = 0
    top: list[dict] = []
    seen: set[int] = set()

    def visit(block) -> None:
        nonlocal total, over_threshold_count
        seen.add(id(block))
        content = getattr(block, "content", "")
        chars = formal_tool_result_visible_len(content)
        total += chars
        if isinstance(threshold, int) and threshold > 0 and chars > threshold:
            over_threshold_count += 1
        if chars > TOOL_RESULT_CHARS_MIN_TOP_CHARS:
            top.append(
                {
                    "id": _tool_result_id(block),
                    "tool_name": _tool_result_name(block),
                    "chars": chars,
                }
            )

    for block in _iter_history_tool_result_blocks(agent) or ():
        visit(block)
    for block in extra_results or ():
        if not _is_tool_result_block(block) or id(block) in seen:
            continue
        visit(block)

    top.sort(key=lambda item: item["chars"], reverse=True)
    return {
        "total_chars": total,
        "threshold": threshold,
        "over_threshold_count": over_threshold_count,
        "top_results": top[:TOOL_RESULT_CHARS_TOP_N],
    }


def _meta_block(result: dict) -> dict:
    """Return ``result["_meta"]``, creating an empty dict if absent.

    Centralizes the envelope so the per-result ``tool_meta`` writer and the
    current ``agent_meta``/``guidance`` updater and notification merger all share
    one container.
    """
    meta = result.get(META_ENVELOPE_KEY)
    if not isinstance(meta, dict):
        meta = {}
        result[META_ENVELOPE_KEY] = meta
    return meta


def build_meta_readme() -> dict:
    """Self-describing readme for the two canonical ``_meta`` axes.

    This readme is rendered once into the resident ``meta_guidance``
    system-prompt section (via :func:`build_meta_guidance`), not stamped onto
    every tool result; the tail ``_meta.agent_meta.guidance`` carries only a lightweight
    ref back to that section.  Each entry states what the block is for and
    whether it is per-result or current-state — no policy,
    just structural orientation.
    """
    return {
        TOOL_META_KEY: (
            "Immutable facts for one tool execution only: correlation id, tool "
            "name when needed, completion time, elapsed time, status/error phase, "
            "character counts, spill, and a-priori-summary effects. It has no "
            "agent/session/current-state semantics and remains valid historically. "
            "Token diagnostics live in the nested "
            "`agent_meta.agent_state.token_usage` block: "
            "`token_usage.current_call` contains this result's own "
            "token/cache/output facts; `token_usage.session` contains the "
            "since-last-molt `session_cache_rate`, `api_calls`, cumulative token "
            "fields, context fields, and the ALWAYS-ON "
            "`cache_miss_tokens`; `cache_miss_budget` and "
            "`cache_miss_remaining_tokens` appear when a positive budget is "
            "configured. When the budget is reached, "
            "`context.molt` says `molt now`; molt proactively rather than "
            "summarize/reconstruct."
        ),
        AGENT_META_KEY: {
            "instruction": AGENT_META_INSTRUCTION,
            "agent_state": (
                "Timely current main-agent/runtime state and diagnostics, including "
                "`agent_meta.agent_state.token_usage` with nested "
                "`current_call` and `session` halves. only the NEWEST emission "
                "is current; older payloads remain historical traces. Replay "
                "preserves those historical holders and does not strip them. "
                "`current_tool_result_chars` reports total/threshold/count and "
                "`top_results` entries with id, tool_name, and chars; entries "
                "have no preview and are proactive summarization candidates. "
                "`adapter_comment` carries dynamic adapter state."
            ),
            "notifications": (
                "Timely current notifications and persistent communication "
                "context. only the NEWEST emission is current; older payloads "
                "remain historical traces. Replay preserves those historical "
                "holders and does not strip them. Older payloads are not current "
                "instructions; the producer channel is the source of truth."
            ),
            "guidance": {
                "persistent": "Stable references and rules.",
                "transient": "Current warnings and hooks.",
            },
        },
    }


def now_iso_plain() -> str:
    """Return the UTC completion timestamp used by universal tool metadata."""
    try:
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


_GUIDANCE_CACHE: dict | None = None
_GUIDANCE_REQUIRED_TOP_KEYS = ("schema_version", "guidance_version", "priority", "render_mode", "sections")


class GuidanceSchemaError(ValueError):
    """Raised when the runtime guidance payload does not match the expected shape.

    A structural problem in the *packaged* resource is a build/authoring error,
    not a runtime condition, so this is surfaced loudly to ``validate_runtime_guidance``
    callers (and the test suite). The live loader (``build_runtime_guidance``)
    degrades to ``{}`` rather than crashing an agent on a bad ship.
    """



META_README_SECTION_ID = "meta_readme"


def build_meta_readme_section() -> Dict[str, str]:
    """Return the guidance section that explains the `_meta` envelope.

    This readme is one ordered section among the kernel guidance sections; both
    are rendered into the resident ``meta_guidance`` system-prompt section (see
    :func:`build_meta_guidance`).  The tail ``_meta.agent_meta.guidance`` on tool results is
    only a lightweight ref back to that section, never the full body.
    """
    readme = build_meta_readme()
    body_lines = [
        "This section explains the `_meta` envelope carried on tool results.",
        "These explanations are resident here in the `meta_guidance` system-prompt section; the tail `_meta.agent_meta.guidance` on each tool result carries only a lightweight ref back to this section, not the full body.",
        "",
    ]
    body_lines.extend(f"- `{key}`: {value}" for key, value in readme.items())
    return {
        "id": META_README_SECTION_ID,
        "title": "_meta envelope readme",
        "body": "\n".join(body_lines),
    }


def build_guidance_with_meta_readme(base_guidance: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return runtime guidance with the `_meta` readme appended as a section."""
    source = build_runtime_guidance() if base_guidance is None else base_guidance
    guidance = dict(source or {})
    # Preserve packaged guidance keys when available, but keep the fallback shape
    # valid too: even if the guidance catalog cannot be loaded, guidance remains the
    # same system-prompt-like structure with a single meta_readme section.
    guidance.setdefault("schema_version", 1)
    guidance.setdefault("guidance_version", "runtime-meta-readme")
    guidance.setdefault("priority", "high")
    guidance.setdefault("render_mode", "latest_tool_result_only")
    sections = []
    for section in guidance.get("sections") or []:
        if not isinstance(section, dict):
            continue
        if section.get("id") == META_README_SECTION_ID:
            continue
        sections.append(dict(section))
    sections.append(build_meta_readme_section())
    guidance["sections"] = sections
    return guidance

# ---------------------------------------------------------------------------
# meta_guidance — resident system-prompt section.
#
# The static, rule-like content that used to ride in every tail
# ``_meta.agent_meta.guidance`` (the runtime guidance sections + the ``_meta`` readme) and
# in the adapter's ``adapter_comment`` (the long full-epoch/summarize prose) is
# rendered once here and appended as the final, always-resident system-prompt
# section named ``meta_guidance``.  The tail ``_meta`` then carries only a
# lightweight ref pointing back at this section.
# ---------------------------------------------------------------------------

META_GUIDANCE_SECTION_ID = "meta_guidance"

# Short hook the unified ``agent_meta.agent_state.token_usage`` block carries back to the
# resident guidance subsection that explains how to read/act on it. A short
# sentence (not a bare path) pointing at the ``token_efficiency`` subsection of
# the ``meta_guidance`` system-prompt section.
TOKEN_USAGE_GUIDANCE_REF = (
    f"See {META_GUIDANCE_SECTION_ID}.token_efficiency for details."
)


def build_meta_guidance_ref() -> dict:
    """Return the lightweight guidance hook for the current runtime block."""
    return {"ref": META_GUIDANCE_SECTION_ID}

def _render_guidance_sections_markdown(guidance: dict) -> list[str]:
    """Render guidance.sections (incl. meta_readme) as Markdown subsections."""
    lines: list[str] = []
    for section in (guidance or {}).get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = section.get("title") or section.get("id") or ""
        body = section.get("body") or ""
        if title:
            lines.append(f"### {title}")
        if body:
            lines.append(body)
        lines.append("")
    return lines


def _render_adapter_comment_markdown(comment: dict) -> list[str]:
    """Render a static adapter_comment dict as a Markdown subsection."""
    if not isinstance(comment, dict) or not comment:
        return []
    adapter = comment.get("adapter") or "adapter"
    lines = [f"### {adapter} runtime rules"]
    for key, value in comment.items():
        if key == "adapter":
            continue
        if isinstance(value, str) and value:
            lines.append(f"- `{key}`: {value}")
    lines.append("")
    return lines


def build_meta_guidance(agent) -> str:
    """Render the resident ``meta_guidance`` system-prompt section body.

    Combines the static, rule-like material that previously rode on every tail
    ``_meta``:

      * the runtime guidance sections from the Markdown guidance catalog (e.g.
        summarize/molt best practice);
      * the ``_meta`` envelope readme (which blocks exist and whether each is
        per-result or current-state);
      * the active adapter's *static* runtime rules (from
        :func:`static_adapter_comment`), if any.

    Dynamic per-result state (tool_meta, current context/molt hints,
    notifications, current_tool_result_chars, adapter epoch counters, cache
    ledger summary, …) is deliberately NOT rendered here — it stays in the tail
    ``_meta`` so this section can remain a stable, cache-friendly prefix.

    Returns the Markdown body (no ``## meta_guidance`` header — the prompt
    manager adds the section header).  Returns ``""`` only if nothing renders.
    """
    guidance = build_guidance_with_meta_readme()
    lines: list[str] = [
        "Resident kernel guidance for reading runtime metadata. This is the "
        "static, rule-like material; dynamic per-turn state stays in the tail "
        "`_meta` block on tool results (which points back here via "
        "`_meta.agent_meta.guidance.ref`).",
        "",
    ]
    lines.extend(_render_guidance_sections_markdown(guidance))
    static_comment = static_adapter_comment(agent)
    lines.extend(_render_adapter_comment_markdown(static_comment))
    body = "\n".join(lines).strip()
    return body


def validate_runtime_guidance(data) -> dict:
    """Validate the guidance payload shape, returning it unchanged on success.

    Raises :class:`GuidanceSchemaError` on any structural violation:
      * top-level must be a dict with ``schema_version`` (int), ``guidance_version``
        (str), ``priority`` (str), ``render_mode`` (str), and ``sections`` (list);
      * each section must be a dict with non-empty string ``id``, ``title``, ``body``;
      * section ``id`` and ``title`` must each be unique across the list.

    This is intentionally strict and independently testable so a malformed
    packaged resource is caught by the test suite rather than silently shipping
    empty guidance to production agents.
    """
    if not isinstance(data, dict):
        raise GuidanceSchemaError(f"guidance must be a JSON object, got {type(data).__name__}")
    for key in _GUIDANCE_REQUIRED_TOP_KEYS:
        if key not in data:
            raise GuidanceSchemaError(f"guidance missing required key: {key!r}")
    if not isinstance(data["schema_version"], int) or isinstance(data["schema_version"], bool):
        raise GuidanceSchemaError("guidance.schema_version must be an int")
    for str_key in ("guidance_version", "priority", "render_mode"):
        if not isinstance(data[str_key], str) or not data[str_key]:
            raise GuidanceSchemaError(f"guidance.{str_key} must be a non-empty string")
    sections = data["sections"]
    if not isinstance(sections, list) or not sections:
        raise GuidanceSchemaError("guidance.sections must be a non-empty list")

    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            raise GuidanceSchemaError(f"guidance.sections[{idx}] must be an object")
        for field in ("id", "title", "body"):
            value = section.get(field)
            if not isinstance(value, str) or not value:
                raise GuidanceSchemaError(
                    f"guidance.sections[{idx}].{field} must be a non-empty string"
                )
        sid = section["id"]
        stitle = section["title"]
        if sid in seen_ids:
            raise GuidanceSchemaError(f"duplicate guidance section id: {sid!r}")
        if stitle in seen_titles:
            raise GuidanceSchemaError(f"duplicate guidance section title: {stitle!r}")
        seen_ids.add(sid)
        seen_titles.add(stitle)
    return data


def build_runtime_guidance() -> dict:
    """Load, validate, and return the runtime guidance payload.

    Sourced from the skill-style Markdown catalog under
    ``lingtai/prompts/meta_guidance/catalog/`` (``INDEX.md`` + one ``<id>.md`` per section),
    assembled by :func:`lingtai.kernel.prompt_catalog.load_guidance_catalog` into
    the same dict shape the kernel has always consumed (``schema_version`` int,
    ordered ``sections`` with stable ``id``/``title``/``body``). The return type
    stays a ``dict`` so it can both feed ``build_meta_guidance`` and back the
    derived ``system/guidance.json`` mirror the TUI/Portal read.

    Cached after first successful load. The assembled payload is schema-checked
    via :func:`validate_runtime_guidance`; on a missing/unreadable catalog, a
    malformed file, or a schema violation the loader returns an empty dict so a
    live agent degrades (no guidance) rather than crashing. Tests should call
    :func:`validate_runtime_guidance` directly to assert the *packaged* catalog
    is well-formed — that path raises, this one does not.
    """
    global _GUIDANCE_CACHE
    if _GUIDANCE_CACHE is not None:
        return _GUIDANCE_CACHE
    try:
        from .prompt_catalog import load_guidance_catalog

        parsed = load_guidance_catalog()
        validate_runtime_guidance(parsed)
        _GUIDANCE_CACHE = parsed
        return parsed
    except Exception:
        return {}



def build_molt_context(agent, usage: float) -> str | None:
    # NOTE: the lighter 85% manual-rebuild hint is built by
    # ``build_context_rebuild_hint`` below.  This function remains the stronger
    # sustained-pressure molt reminder.
    """Return the sustained-pressure molt reminder string, or ``None``.

    The returned text is attached to current ``_meta.agent_meta.agent_state.context.molt``
    (``build_meta`` routes it there via a transit key so it persists on every
    result while the warning is active — it is part of the complete current
    ``agent_meta.agent_state`` snapshot).
    The contract (channel B)
    replaces the old immediate trip-wire with a
    *sustained-pressure* signal: the reminder appears only once context has been
    high (>= the 0.85 reconstruction ratio) for
    ``CONTEXT_PRESSURE_WARN_AFTER_ROUNDS`` consecutive *fresh provider rounds*,
    tracked by ``SessionManager.note_context_pressure_round``. The first two
    high rounds are the window in which the automatic delayed-summarize
    reconstruction (and any agent summarize) is expected to relieve pressure; a
    drop below the threshold resets the streak and clears the reminder.

    Keep this agent-facing value sentence-like. The agent needs a clear reminder
    about why it appeared and what to do, not a tag soup of ``stage`` /
    ``threshold`` / ``action`` fields.
    """
    if "context" not in getattr(agent, "_intrinsics", set()):
        return None

    session = getattr(agent, "_session", None)
    if session is None:
        return None
    # The warning decision + prose live in ``ContextPressureReminder``; the
    # context-intrinsic gate and session lookup stay here (they are agent/session
    # concerns, not reminder concerns). Prefer the real reminder object; fall
    # back to the session's compat streak/active surface so lightweight test
    # stand-ins (a SimpleNamespace with only context_pressure_* attributes) still
    # render identical prose.
    reminder = getattr(session, "context_pressure_reminder", None)
    if reminder is not None:
        return reminder.current_molt_context(usage)

    if not getattr(session, "context_pressure_warning_active", False):
        return None
    streak = int(getattr(session, "context_pressure_streak", 0))
    return render_current_molt_context(streak=streak, usage=usage)


def build_context_rebuild_hint(agent, usage: float) -> str | None:
    """Return the lightweight 85% manual provider-context rebuild hint.

    This is not a molt warning and not an event route.  It is a current-state line
    stamped under ``_meta.agent_meta.agent_state.context.rebuild`` whenever context is at/above
    ``CONTEXT_PRESSURE_HIGH_RATIO`` and the system intrinsic is available, so the
    agent may explicitly request a rebuild via
    ``context(action='rebuild')`` instead of letting the
    1.0 hard boundary force one.
    """
    if "system" not in getattr(agent, "_intrinsics", set()):
        return None
    try:
        pressure = float(usage)
    except (TypeError, ValueError):
        return None
    if pressure < CONTEXT_PRESSURE_HIGH_RATIO:
        return None
    return (
        "context now above 85%: recording summaries does NOT itself rebuild the "
        "active provider context. If recorded summaries are worth making active "
        "sooner, you MAY pay for a provider-context rebuild via "
        "context(action='rebuild') (with or without new items). This "
        "is a permitted option, not a requirement; if you do nothing, the runtime "
        "forces a rebuild at the 1.0 hard boundary (full context) regardless. "
        "Preferring a proactive rebuild here avoids the emergency forced path. Keep "
        "summarizing digested results to shrink recorded history either way. See "
        "meta_guidance for details."
    )


def _rendered_system_prompt_tokens(agent) -> int | None:
    """Return the exact rendered-system-prompt-only token count, or ``None``.

    Reuses the same lazily-refreshed decomposition :func:`_current_context_usage`
    relies on (``SessionManager._system_prompt_tokens``, populated by
    ``count_tokens(self._build_system_prompt_fn())`` — the kernel's actual
    provider-aware token counter over exactly the joined rendered system-prompt
    text, batched and non-batched builders included since
    ``build_system_prompt`` joins ``build_system_prompt_batches``'s segments).
    Deliberately excludes ``_tools_tokens`` (tool schemas) and history — this is
    the system-prompt body alone. Returns ``None`` when no session exists or the
    decomposition cannot be refreshed, so callers omit rather than guess.
    """
    session = getattr(agent, "_session", None)
    if session is None:
        return None
    if getattr(session, "_token_decomp_dirty", True):
        try:
            session._update_token_decomposition()
        except Exception:
            pass
    if getattr(session, "_token_decomp_dirty", True):
        return None
    tokens = getattr(session, "_system_prompt_tokens", None)
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        return None
    return tokens


def render_system_prompt_pressure_context(tokens: int | None, window: int | None) -> str | None:
    """Pure renderer for the rendered-system-prompt-size warning, or ``None``.

    Owns the whole decision + bounded message: strictly above the effective
    environment threshold (default 40%) of ``window``, the rendered
    system prompt ALONE is warned as oversized; at or below — or when either
    input is missing/non-positive (invalid/zero omission, never guessed or
    divided by zero) — returns ``None``. Never embeds or repeats the prompt
    body itself; only bounded numeric/percentage state and a fixed
    progressive-disclosure instruction. Shared verbatim by the main-agent
    wrapper (:func:`build_system_prompt_pressure_context`, which resolves its
    own prompt tokens/window and delegates here) and any daemon-local caller
    that resolves its own prompt tokens/window against its own resolved window.
    """
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        return None
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return None
    threshold = system_prompt_pressure_ratio()
    ratio = tokens / window
    if ratio <= threshold:
        return None
    percentage = round(ratio * 100, 1)
    threshold_percentage = threshold * 100
    return (
        f"system prompt is {tokens} tokens ({percentage}% of the {window}-token "
        "effective context window), above the "
        f"{threshold_percentage:g}% threshold. Apply progressive "
        "disclosure: reorganize pad and lingtai so the system prompt keeps only "
        "active essentials — route completed/duplicate task state to knowledge, "
        "reusable procedures to skills, and identity-shaping lessons to lingtai. "
        "Do not paste or repeat the system prompt body here."
    )


def build_system_prompt_pressure_context(agent) -> str | None:
    """Return the rendered-system-prompt-size warning, or ``None``.

    Deterministic current-state projection (not an event, not a Nudge): strictly
    above the effective environment threshold (default 40%) of the effective
    context window, the rendered system prompt ALONE (never conversation history or tool
    results) is warned as oversized; at or below, ``None`` (no warning). Routed
    to ``agent_meta.agent_state.context.system_prompt`` alongside ``rebuild``/
    ``molt`` — a distinct, non-colliding key.

    Resolves this agent's own prompt-token count
    (:func:`_rendered_system_prompt_tokens`) and effective context window
    (:func:`_session_context_window` — the SAME provider-snapshot-first,
    configured/live-fallback precedence already used by session token
    telemetry: latest provider-round snapshot's ``context_window``, then
    configured ``context_limit``, then the live ``chat.context_window()``),
    then delegates the strict-threshold/invalid/zero decision and bounded message to
    the pure :func:`render_system_prompt_pressure_context` renderer.

    The warning instructs progressive disclosure — reorganizing ``pad`` and
    ``lingtai`` so only active essentials stay resident in the prompt — and
    never embeds or repeats the prompt body itself.
    """
    tokens = _rendered_system_prompt_tokens(agent)
    window = _session_context_window(agent)
    return render_system_prompt_pressure_context(tokens, window if window > 0 else None)


def build_context_overflow_warning(agent) -> str | None:
    """Return the persistent post-forced-rebuild overflow warning, or ``None``.

    Distinct from the sustained-pressure reminder (:func:`build_molt_context`,
    the 3-round streak) and the one-shot reconstruction event
    (:func:`build_reconstruction_tool_meta`, a historical A→B rebuild record):
    this is the human-authored hard-boundary warning that stays on EVERY
    ``_meta.agent_meta.agent_state.context.molt`` result while the automatic one-shot forced
    provider-context rebuild has already fired for the current ``>= 1.0`` episode,
    its first post-rebuild provider response has been observed, and current
    provider-reported usage is still STRICTLY above 1.0 (the forced rebuild failed
    to clear the overflow).

    The adapter owns the one-shot latch + verification state and exposes the
    numeric status via ``session.chat.context_overflow_status()`` (forwarded
    through the gate proxy); this function only renders the fixed sentence with the
    measured percentage. Returns ``None`` whenever the status is absent — it never
    invents a warning.
    """
    session = getattr(agent, "_session", None)
    if session is None:
        return None
    chat = getattr(session, "chat", None)
    status_fn = getattr(chat, "context_overflow_status", None)
    if not callable(status_fn):
        return None
    try:
        status = status_fn()
    except Exception:
        return None
    if not isinstance(status, dict):
        return None
    try:
        usage = float(status.get("usage"))
    except (TypeError, ValueError):
        return None
    return render_forced_rebuild_failed_warning(usage)


def _resolve_cache_miss_budget(agent) -> int:
    """Return a positive outer-hook budget or the fixed compatibility fallback."""
    try:
        resolve = getattr(agent, "resolve_cache_miss_budget", None)
    except Exception:
        return CACHE_MISS_BUDGET_DEFAULT
    if not callable(resolve):
        return CACHE_MISS_BUDGET_DEFAULT
    try:
        budget = resolve()
    except Exception:
        return CACHE_MISS_BUDGET_DEFAULT
    if type(budget) is not int or budget <= 0:
        return CACHE_MISS_BUDGET_DEFAULT
    return budget


def build_cache_miss_budget_context(
    agent, *, resolved_budget: int | None = None
) -> dict | None:
    """Return the cache-miss budget guard sub-object, or ``None``.

    ``build_meta`` passes its one immutable per-snapshot ``resolved_budget`` so
    the guard and session telemetry cannot observe different live file values.
    Standalone/test calls that omit it preserve the helper's live resolution.

    A soft since-last-molt cap on total cache-miss (uncached input) tokens.  The
    cache-miss total is derived from ``agent.get_token_usage()`` — the
    CUMULATIVE / restored totals, which SURVIVE ``restore_token_state`` — so a
    refresh does NOT reset the guard (Jason FINAL; matches the always-on
    ``session`` telemetry in :func:`_build_session_token_economy`, both on the
    same cumulative basis) as::

        cache_miss = max(input_tokens - cached_tokens, 0)

    When ``cache_miss >=`` the effective budget (inclusive) — resolved through
    the outer Agent hook with a fixed ``2_000_000`` compatibility fallback —
    return a
    dict destined for the SAME ``_tool_meta_context`` transit sub-object as the
    sustained-pressure ``molt`` reminder::

        {
            "molt": "cache miss budget {budget} reached, molt now",
            "cache_miss_budget": <budget>,
            "cache_miss_tokens": <cache_miss>,
        }

    ``ToolExecutor._attach_tool_block`` promotes the whole sub-object into the
    current ``agent_meta.agent_state.context`` snapshot, so the warning is
    available on the newest final carrier and the budget value is surfaced at
    ``agent_meta.agent_state.context.cache_miss_budget`` while the guard is tripped.

    Returns ``None`` (no guard) when: the ``context`` intrinsic is absent (matching
    :func:`build_molt_context`, since ``molt`` presupposes the molt action), the
    budget is not a positive int, the cumulative-usage getter is missing/raising,
    or the cache-miss total is below the budget.  It is a soft signal only —
    nothing is blocked — and NOT a new event route (no emission-event payload).
    """
    if "context" not in getattr(agent, "_intrinsics", set()):
        return None

    # Defensive: only a positive int arms the guard. ``build_meta`` supplies its
    # single resolved snapshot value; standalone helper calls still resolve live.
    budget = (
        _resolve_cache_miss_budget(agent)
        if resolved_budget is None
        else resolved_budget
    )
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        return None

    # Since-last-molt basis: read the cumulative/restored totals so a refresh
    # does not reset the guard (identical source to the always-on session-half
    # cache-miss telemetry).
    usage_fn = getattr(agent, "get_token_usage", None)
    if not callable(usage_fn):
        return None
    try:
        usage = usage_fn()
    except Exception:
        return None
    if not isinstance(usage, Mapping):
        return None

    input_tokens = _non_negative_int(usage.get("input_tokens"))
    cached_tokens = _non_negative_int(usage.get("cached_tokens"))
    cache_miss = max(input_tokens - cached_tokens, 0)
    if cache_miss < budget:
        return None

    return {
        "molt": f"cache miss budget {budget} reached, molt now",
        TOOL_META_CONTEXT_CACHE_MISS_BUDGET_KEY: budget,
        TOOL_META_CONTEXT_CACHE_MISS_TOKENS_KEY: cache_miss,
    }


def _current_molt_emission_event(agent, *, usage, message) -> dict | None:
    """Return the current-molt emission-event descriptor, or ``None``.

    Pure / side-effect-free: it only builds the ``{event_name, payload}``
    descriptor from the session's reminder state (the values that produced
    ``message``).  It does NOT decide whether to log — the DEDUP happens at the
    real emission site (``ToolExecutor._attach_tool_block``), keyed by the
    payload's ``last_round_id``, so this render-path call never mutates agent
    state (``build_meta`` runs both for the text-input prefix and per tool-result
    stamp; a side effect here would desync the dedup).

    Returns ``None`` only when no real reminder object is available (compat
    session stand-ins that expose just ``context_pressure_*`` attributes carry no
    round id / debug state to build a meaningful event from).
    """
    session = getattr(agent, "_session", None)
    reminder = getattr(session, "context_pressure_reminder", None)
    if reminder is None:
        return None
    try:
        return current_molt_emission_descriptor(reminder, usage=usage, message=message)
    except Exception:
        return None


def build_reconstruction_tool_meta(agent) -> dict | None:
    """Build the one-shot delayed-summarize reconstruction event (channel A).

    One-shot evidence, destined for ``_meta.agent_meta.agent_state.events.reconstruction``.
    Distinct from :func:`build_molt_context` (channel B, current-state reminder
    routed to ``agent_meta.agent_state.events.reconstruction``): this records a
    *historical event* — the runtime actually rebuilt the
    provider context around the compacted history after a manual rebuild request or
    at the 1.0 hard forced-rebuild boundary.

    The adapter supplies the before-context (A) and fixed trigger/recovery
    metadata via ``session.chat.take_pending_reconstruction_event()`` (one-shot:
    the adapter clears it on read). This function fills the after-context (B).

    Call order makes B honest: ``SessionManager.send`` runs ``_track_usage``
    (which sets ``_latest_input_tokens`` from the post-reconstruction provider
    request's reported input) BEFORE the resulting tool calls reach the
    ToolExecutor that stamps this event. So at attach time ``_latest_input_tokens``
    already holds the provider-reported size of the rebuilt context. B therefore
    **prefers** ``_latest_input_tokens / context_window`` (``source:
    provider_input_tokens``) and only falls back to the local compacted-history
    estimate (``source: local_estimate``) when the provider input is unavailable
    (0, e.g. a provider that returned no usage). The delayed-reconstruction
    threshold is itself provider-input based, so this keeps B on the same ruler.

    If B is still at/above the 0.75 recovery target, a natural-language molt
    reminder is attached saying summarize/reconstruction was attempted and
    pressure remains above the recovery target, so consider molt. If B < 0.75,
    the A->B event is returned without a reminder.

    Returns ``None`` when no reconstruction is pending (the common case).
    """
    session = getattr(agent, "_session", None)
    if session is None:
        return None
    chat = getattr(session, "chat", None)
    take = getattr(chat, "take_pending_reconstruction_event", None)
    if not callable(take):
        # Fall back to a session-level hook if the adapter exposes it there.
        take = getattr(session, "take_pending_reconstruction_event", None)
    if not callable(take):
        return None
    raw = take()
    if not raw:
        return None

    # Context window: prefer the value the adapter captured at reconstruction
    # time; fall back to the configured/live window so B can be computed even if
    # the event omitted it.
    ctx_window = 0
    try:
        ctx_window = int(raw.get("context_window") or 0)
    except Exception:
        ctx_window = 0
    if ctx_window <= 0:
        ctx_window = _fallback_context_window(agent)
        if ctx_window <= 0:
            ctx_window = 0

    # After-context (B): prefer the provider-reported input from the
    # post-reconstruction request; fall back to the local compacted-history
    # estimate only when that is unavailable.
    after_tokens = None
    after_usage = -1.0
    after_source = None
    try:
        provider_input = int(getattr(session, "_latest_input_tokens", 0) or 0)
    except Exception:
        provider_input = 0
    if provider_input > 0 and ctx_window > 0:
        after_tokens = provider_input
        after_usage = provider_input / ctx_window
        after_source = "provider_input_tokens"
    else:
        # Local fallback: reuse the same local (system + history) / window math
        # used for current-state context-pressure warnings.  The value is no
        # longer serialized into agent_meta, but reconstruction events still need
        # it when provider input tokens are unavailable.
        try:
            local_usage = float(_current_context_usage(agent))
        except Exception:
            local_usage = -1.0
        if local_usage >= 0:
            after_usage = local_usage
            after_source = "local_estimate"
            if ctx_window > 0:
                after_tokens = int(round(local_usage * ctx_window))

    event = {
        "type": raw.get("type", "delayed_summarize_reconstruction"),
        "reason": raw.get("reason", "delayed_summarize_reconstruction"),
        "trigger_threshold": raw.get(
            "trigger_threshold", CONTEXT_PRESSURE_FORCED_REBUILD_RATIO
        ),
        "threshold_high": raw.get("threshold_high", CONTEXT_PRESSURE_HIGH_RATIO),
        "recovery_target": raw.get("recovery_target", CONTEXT_PRESSURE_RECOVERY_TARGET),
        "context_window": raw.get("context_window"),
        "before": raw.get("before", {}),
        "after": {
            "context_tokens": after_tokens,
            "usage": round(after_usage, 5) if after_usage >= 0 else after_usage,
            "source": after_source,
        },
    }

    recovery_target = event["recovery_target"]

    if event["type"] == "delayed_summarize_reconstruction":
        # 1.0 HARD forced rebuild: ALWAYS attach the one unified warning,
        # regardless of whether the rebuilt context dropped low or stayed high. It
        # folds the before→after change, the proactive-rebuild advice, and the
        # conditional "if still above 0.75, molt" instruction into a single string —
        # no after-high/low branching.
        before = event.get("before") if isinstance(event.get("before"), dict) else {}
        event["warning"] = render_forced_rebuild_warning(
            before_tokens=before.get("context_tokens"),
            before_usage=before.get("usage"),
            after_tokens=after_tokens,
            after_usage=after_usage,
            trigger_threshold=event.get(
                "trigger_threshold", CONTEXT_PRESSURE_FORCED_REBUILD_RATIO
            ),
            high_threshold=event.get("threshold_high", CONTEXT_PRESSURE_HIGH_RATIO),
            recovery_target=recovery_target,
        )
        return event

    # Manual rebuild=true reconstruction (summarize_rebuild_only_reconstruction):
    # the agent already acted proactively, so no forced-rebuild warning. Keep the
    # recovery molt reminder (channel A) when the rebuilt context is still above the
    # recovery target. Delegate to the session's reminder when present, falling back
    # to the pure renderer for session stand-ins without one.
    reminder = getattr(session, "context_pressure_reminder", None)
    if reminder is not None:
        molt = reminder.annotate_reconstruction(
            after_usage, recovery_target=recovery_target
        )
    else:
        molt = render_reconstruction_molt(
            after_usage=after_usage,
            recovery_target=recovery_target,
            reconstruction_ratio=event.get("threshold_high", CONTEXT_PRESSURE_HIGH_RATIO),
        )
    if molt:
        event["molt"] = molt
    return event


def _build_provider_round_token_usage(agent) -> dict:
    """Return the ``current_call`` (provider-round) half of the token_usage block.

    ``current_call`` is ONLY this provider call's own token/cache/output facts.
    Reads ``SessionManager.latest_token_usage_snapshot()`` — the full
    provider-round record kept for internal logging (scope, api-call index/id,
    cached/context tokens, estimated flag, ...) — and projects only the per-result
    evidence agents need: ``input``/``cache_miss``/``cache_rate``/``output``/
    ``thinking``, mapped from the snapshot's long field names.

    Current CONTEXT state (``context_usage``/``window``/context tokens) is NOT
    part of this call's own facts — it is current session/context state and now
    lives in the ``session`` half (see :func:`_build_session_token_economy`), so
    it is deliberately dropped here along with the other noisy/invalid/duplicate
    fields (scope, api_call_id, context_tokens, estimated, the provider-round
    cached_tokens). Missing fields are omitted rather than invented; existing
    numeric zero/sentinel values are preserved. Returns ``{}`` when no snapshot
    exists.
    """
    session = getattr(agent, "_session", None)
    snapshot_fn = getattr(session, "latest_token_usage_snapshot", None)
    if callable(snapshot_fn):
        try:
            snapshot = snapshot_fn()
        except Exception:
            snapshot = None
    else:
        snapshot = getattr(session, "_latest_token_usage_snapshot", None)
    if not isinstance(snapshot, Mapping):
        return {}
    # Map full snapshot field names -> compact injected keys. Only emit a key
    # when the source field is present, so the injected object stays robust to
    # partial snapshots without inventing values. NOTE: context_usage/window are
    # intentionally absent — they moved to the session half.
    field_map = (
        ("input", "input_tokens"),
        ("cache_miss", "cache_miss_tokens"),
        ("cache_rate", "cache_rate"),
        ("output", "output_tokens"),
        ("thinking", "thinking_tokens"),
    )
    return {
        out_key: snapshot[src_key]
        for out_key, src_key in field_map
        if src_key in snapshot
    }


def _session_context_window(agent) -> int:
    """Return the context window for the ``session`` context state, or ``0``.

    Prefers the latest provider-round snapshot's ``context_window`` (the value the
    provider actually served the current context against); falls back to the
    configured/live window via :func:`_fallback_context_window` (config
    ``context_limit`` then ``chat.context_window()``).  Returns ``0`` when no
    positive window is resolvable, so callers omit the context-state fields
    rather than dividing by an unknown window.
    """
    session = getattr(agent, "_session", None)
    snapshot_fn = getattr(session, "latest_token_usage_snapshot", None)
    snapshot = None
    if callable(snapshot_fn):
        try:
            snapshot = snapshot_fn()
        except Exception:
            snapshot = None
    else:
        snapshot = getattr(session, "_latest_token_usage_snapshot", None)
    if isinstance(snapshot, Mapping):
        window = _non_negative_int(snapshot.get("context_window"))
        if window > 0:
            return window
    fallback = _fallback_context_window(agent)
    return fallback if isinstance(fallback, int) and fallback > 0 else 0


def _build_session_token_economy(
    agent, *, resolved_budget: int | None = None
) -> dict:
    """Return the ``session`` (since-last-molt) half of the token_usage block.

    ``build_meta`` passes its one immutable per-snapshot ``resolved_budget``;
    standalone/test calls that omit it continue to resolve the live setting.

    Sources the aggregate from the AGENT-SESSION object when available
    (``agent.agent_session_token_usage()``), falling back to
    ``agent.get_token_usage()`` for agents/stubs that expose no agent-session
    accessor.  Both read the CUMULATIVE / restored ``_total_*``/``_api_calls``
    counters, which SURVIVE ``restore_token_state`` (refresh/restart) — and, since
    the startup restore is now seeded from the rebuilt agent session's since-molt
    totals (see ``lifecycle._start``), those counters are genuinely
    since-current-molt across a refresh rather than lifetime.  This is the
    "since last molt" contract: the injected ``token_usage.session`` must NOT
    reset on refresh, so it deliberately reads these totals rather than
    ``get_runtime_session_token_usage()`` (the since-refresh deltas, which zero
    out on every restart — that was the #679 defect).  The since-refresh runtime
    getter is never consulted here.

    Routing the numbers through the agent-session view keeps a single owner for
    the since-molt aggregate (the ``AgentSession``), per Jason's same-PR wiring:
    the numbers are identical to ``get_token_usage`` (same counters), but the
    agent-session object is now the named source.  The current CONTEXT state
    (``ctx_total_tokens``) still comes from ``get_token_usage`` since it is live
    context, not part of the since-molt token aggregate.

    Projects the aggregate counters agents act on now: ``session_cache_rate``
    (cached/input clamped to a 0-1 fraction), ``api_calls``,
    ``input_tokens``/``cached_tokens``, and ``avg_input_tokens_per_api_call``,
    deriving the rates from the raw counters.

    It also carries the current CONTEXT state (moved off ``current_call``, since
    context usage is current session/context state, not this call's own facts),
    when resolvable:

    * ``context_tokens`` from ``get_token_usage().ctx_total_tokens``;
    * ``context_window`` from the provider snapshot or configured window (see
      :func:`_session_context_window`);
    * ``context_usage`` = ``context_tokens / context_window`` when both positive.

    And the ALWAYS-ON since-last-molt cache-miss/budget telemetry so an agent
    never has to recompute ``input_tokens - cached_tokens`` or remember the
    default budget (contrast the ``agent_state.context`` guard, which appears only
    at/above budget):

    * ``cache_miss_tokens`` = ``max(input_tokens - cached_tokens, 0)`` — the
      since-last-molt cumulative cache miss, on the same cumulative basis as
      :func:`build_cache_miss_budget_context`, so a refresh does not reset it.
      Always emitted here, since it needs only the aggregate counters.
    * ``cache_miss_budget`` = the effective positive budget from the outer Agent
      hook (or the fixed ``2_000_000`` compatibility fallback), and
      ``cache_miss_remaining_tokens`` = ``max(cache_miss_budget - cache_miss_tokens, 0)``.

    Returns ``{}`` when no aggregate usage is available; numeric zeros are preserved.
    """
    usage_fn = getattr(agent, "get_token_usage", None)
    if not callable(usage_fn):
        return {}
    try:
        usage = usage_fn()
    except Exception:
        return {}
    if not isinstance(usage, Mapping):
        return {}

    # Prefer the AGENT-SESSION view for the since-molt token aggregate (single
    # owner), falling back to the raw cumulative counters for stubs/agents that
    # expose no agent-session accessor. The numbers are the same counters; this
    # only routes them through the named object per the same-PR wiring.
    agg = usage
    agent_session_usage_fn = getattr(agent, "agent_session_token_usage", None)
    if callable(agent_session_usage_fn):
        try:
            candidate = agent_session_usage_fn()
        except Exception:
            candidate = None
        if isinstance(candidate, Mapping):
            agg = candidate

    api_calls = _non_negative_int(agg.get("api_calls"))
    input_tokens = _non_negative_int(agg.get("input_tokens"))
    cached_tokens = _non_negative_int(agg.get("cached_tokens"))
    avg_input = int(round(input_tokens / api_calls)) if api_calls > 0 else 0
    session_cache_rate = (
        round(min(cached_tokens / input_tokens, 1.0), 5)
        if input_tokens > 0
        else 0.0
    )
    cache_miss = max(input_tokens - cached_tokens, 0)
    economy = {
        "session_cache_rate": session_cache_rate,
        "api_calls": api_calls,
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "avg_input_tokens_per_api_call": avg_input,
        # Always-on: derivable from the cumulative counters alone.
        TOKEN_USAGE_CACHE_MISS_TOKENS_KEY: cache_miss,
    }

    # Current context state — only when resolvable (never invented).
    if "ctx_total_tokens" in usage:
        context_tokens = _non_negative_int(usage.get("ctx_total_tokens"))
        economy[TOKEN_USAGE_CONTEXT_TOKENS_KEY] = context_tokens
        window = _session_context_window(agent)
        if window > 0:
            economy[TOKEN_USAGE_CONTEXT_WINDOW_KEY] = window
            economy[TOKEN_USAGE_CONTEXT_USAGE_KEY] = round(
                context_tokens / window, 5
            )

    budget = (
        _resolve_cache_miss_budget(agent)
        if resolved_budget is None
        else resolved_budget
    )
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        economy[TOKEN_USAGE_CACHE_MISS_BUDGET_KEY] = budget
        economy[TOKEN_USAGE_CACHE_MISS_REMAINING_KEY] = max(budget - cache_miss, 0)
    return economy


def build_tool_meta_token_usage(
    agent, *, resolved_budget: int | None = None
) -> dict | None:
    """Return the token diagnostics block for ``agent_meta.agent_state``.

    ``build_meta`` supplies the same immutable ``resolved_budget`` used by the
    Context guard; callers that omit it retain standalone live resolution.

    ALL token-related diagnostics live in ONE ``_meta.agent_meta.agent_state.token_usage``
    block — there is no separate ``agent_meta.agent_state.token_efficiency`` nor
    ``agent_meta.token_efficiency``.  The block is NESTED into two explicitly
    named halves so the confusing flat ``input`` vs ``input_tokens`` adjacency is
    gone; each half keeps its own local key convention:

    * ``current_call`` — ONLY this tool result's own provider-call token/cache/
      output facts: ``input``, ``cache_miss``, ``cache_rate``, ``output``,
      ``thinking`` (see :func:`_build_provider_round_token_usage`).  Current
      context state is NOT here — it moved to the ``session`` half.
    * ``session`` — the SINCE-LAST-MOLT cumulative aggregate: ``session_cache_rate``,
      ``api_calls``, ``input_tokens``, ``cached_tokens``,
      ``avg_input_tokens_per_api_call``, the current context state
      ``context_tokens`` / ``context_window`` / ``context_usage`` (when
      resolvable), plus the always-on cache-miss/budget telemetry
      ``cache_miss_tokens``, ``cache_miss_budget``, and
      ``cache_miss_remaining_tokens``.  These are
      cumulative/restored totals that SURVIVE refresh (NOT the since-refresh
      runtime-session deltas); see :func:`_build_session_token_economy`.

    Each half is emitted only when its source data is available (an empty half is
    omitted entirely, not left as an empty sub-object); missing inner values are
    omitted, not invented; numeric zero/sentinel values are preserved.  When the
    block exists it always carries a single top-level ``ref`` hook
    (:data:`TOKEN_USAGE_GUIDANCE_REF`) — shared across both halves, never
    duplicated inside them — back to the resident guidance subsection.  Returns
    ``None`` when neither half has any data (never an empty block).
    """
    current_call = _build_provider_round_token_usage(agent)
    session = _build_session_token_economy(
        agent, resolved_budget=resolved_budget
    )
    if not current_call and not session:
        return None
    block: dict = {}
    if current_call:
        block[TOKEN_USAGE_CURRENT_CALL_KEY] = current_call
    if session:
        block[TOKEN_USAGE_SESSION_KEY] = session
    block["ref"] = TOKEN_USAGE_GUIDANCE_REF
    return block

def _current_context_usage(agent) -> float:
    """Return the current context-window usage ratio for warnings/events.

    This helper owns the local (system + history) / window estimate that used to
    be serialized under ``agent_meta.agent_state.context``.  The number is still needed for
    current-state decisions such as ``context.molt`` and reconstruction event
    fallbacks, but it is no longer exposed in ``agent_meta`` because
    ``agent_meta.agent_state.token_usage`` is the current token-diagnostics carrier.
    """
    session = getattr(agent, "_session", None)
    chat_obj = getattr(session, "chat", None) if session is not None else None

    if session is not None and getattr(session, "_token_decomp_dirty", True):
        try:
            session._update_token_decomposition()
        except Exception:
            pass  # leave dirty; sentinel below

    decomp_ran = session is not None and not getattr(session, "_token_decomp_dirty", True)
    if not decomp_ran:
        return -1.0

    sys_prompt = getattr(session, "_system_prompt_tokens", 0)
    tools = getattr(session, "_tools_tokens", 0)

    # "history" = in-memory turns (wire chat).  Prefer the provider-reported
    # wire count after a call; before the first post-restore call, fall back to
    # the interface's local estimate so current-state warnings use restored
    # history rather than reporting zero.
    latest_input = getattr(session, "_latest_input_tokens", 0) or 0
    if latest_input > 0:
        history = max(0, latest_input - sys_prompt - tools)
    elif chat_obj is not None:
        try:
            history = max(0, chat_obj.interface.estimate_context_tokens() - sys_prompt - tools)
        except Exception:
            history = 0
    else:
        history = 0

    system_tokens = sys_prompt + tools
    history_tokens = history

    if chat_obj is not None:
        limit = getattr(agent._config, "context_limit", 0) or chat_obj.context_window()
    else:
        limit = getattr(agent._config, "context_limit", 0) or 0
    return (system_tokens + history_tokens) / limit if limit > 0 else -1.0

def build_meta(agent) -> dict:
    """Return the current meta-data snapshot for the agent.

    Respects ``agent._config.time_awareness`` / ``timezone_awareness``
    internally; callers never need to special-case those flags.

    Shape::

        {
            "current_time": "<iso>",          # transient; promoted into agent_state
            "_tool_meta_context": {           # transient; promoted into agent_state.context
                "rebuild": str,               # 85%+ manual rebuild permission hint
                "molt": str,                  # sustained-pressure and/or cache-miss-budget reminder
                "cache_miss_budget": int,     # present only when the budget guard is tripped
                "cache_miss_tokens": int,     # present only when the budget guard is tripped
                "system_prompt": str,         # present only when rendered system prompt > effective env threshold
            },
            "_tool_meta_context_event": {...},# transient; deduped current-molt emission event
            "current_tool_result_chars": dict,# total + top formal tool results >1000 chars
        }

    ``current_time`` and the two ``_tool_meta_context*`` keys are transient
    transit keys: ``ToolExecutor._attach_tool_block`` promotes them into the
    current ``agent_state`` (including ``context`` and its rebuild/molt state),
    and logs ``context_pressure_current_molt_reminder_emitted`` from the
    ``_tool_meta_context_event`` payload — deduped there to once per provider
    round (this function is side-effect-free and carries the payload on every
    build while the warning is active, since it also runs for the text-input
    prefix).  Final tool-result batches carry the resulting state under
    ``agent_meta.agent_state``; ``tool_meta`` remains limited to immutable
    result-local execution facts.

    The ``_tool_meta_context`` sub-object is emitted when the lightweight 85%+
    manual-rebuild hint is active, OR the sustained-pressure warning is active,
    OR the cache-miss budget guard is tripped
    (:func:`build_cache_miss_budget_context`).  When warning paths fire together,
    both warnings are
    preserved in ``molt`` (the budget line is appended on its own line, never
    replacing the context-pressure prose) and the budget fields
    (``cache_miss_budget`` / ``cache_miss_tokens``) ride alongside.  The budget
    guard is a soft signal only and NOT a new event route — it never attaches a
    ``_tool_meta_context_event``, and the context-pressure event still hashes only
    its own pure message.

    The effective cache-miss budget is resolved exactly once per snapshot and
    passed as one immutable integer to both the Context guard and session-token
    telemetry projections.
    """
    resolved_budget = _resolve_cache_miss_budget(agent)
    meta: dict = {}
    ts = now_iso(agent)
    if ts:
        meta["current_time"] = ts

    usage = _current_context_usage(agent)

    rebuild_hint = build_context_rebuild_hint(agent, usage)
    if rebuild_hint:
        meta[TOOL_META_CONTEXT_PENDING_KEY] = {
            TOOL_META_CONTEXT_REBUILD_KEY: rebuild_hint
        }

    # Sustained-pressure molt reminder — current agent state at
    # ``agent_meta.agent_state.context.molt``.  It rides via a transit key that
    # ``ToolExecutor._attach_tool_block`` promotes into the private final-batch
    # capture; finalization carries it on the whole agent snapshot.
    molt = build_molt_context(agent, usage)
    if molt:
        existing_context = meta.get(TOOL_META_CONTEXT_PENDING_KEY)
        if isinstance(existing_context, dict):
            existing_context["molt"] = molt
        else:
            meta[TOOL_META_CONTEXT_PENDING_KEY] = {"molt": molt}
        # The channel-B emission event is built from the PURE sustained-pressure
        # message (before the budget line is appended below), so its
        # ``message_hash`` and per-round dedup semantics stay unchanged even when
        # both warnings are active.
        event = _current_molt_emission_event(agent, usage=usage, message=molt)
        if event is not None:
            meta[TOOL_META_CONTEXT_EVENT_PENDING_KEY] = event

    # Persistent post-forced-rebuild overflow warning — the human-authored hard
    # boundary sentence, routed to the same ``agent_meta.agent_state.context.molt``
    # channel.  It is a current-state warning (the adapter owns the one-shot latch
    # + verification), NOT a new event route, so it never attaches a
    # ``_tool_meta_context_event``.  When the sustained-pressure reminder is also
    # active, PRESERVE both: append the overflow line on its own newline rather
    # than replacing the sustained-pressure prose (the cache-miss budget line, if
    # any, is appended after this below).
    overflow_warning = build_context_overflow_warning(agent)
    if overflow_warning:
        existing_context = meta.get(TOOL_META_CONTEXT_PENDING_KEY)
        if isinstance(existing_context, dict):
            prior_molt = existing_context.get("molt")
            existing_context["molt"] = (
                f"{prior_molt}\n{overflow_warning}" if prior_molt else overflow_warning
            )
        else:
            meta[TOOL_META_CONTEXT_PENDING_KEY] = {"molt": overflow_warning}

    # Cache-miss budget guard — rides the SAME ``_tool_meta_context`` transit
    # sub-object as the sustained-pressure reminder.  When both are active we
    # PRESERVE both warnings: the budget line is appended to ``molt`` on a new
    # line (never replacing the context-pressure prose), and the budget fields
    # are merged in alongside.  This is a soft signal, not a new event route, so
    # no ``_tool_meta_context_event`` is emitted for it.
    budget_ctx = build_cache_miss_budget_context(
        agent, resolved_budget=resolved_budget
    )
    if budget_ctx:
        existing = meta.get(TOOL_META_CONTEXT_PENDING_KEY)
        if isinstance(existing, dict):
            prior_molt = existing.get("molt")
            budget_molt = budget_ctx["molt"]
            existing["molt"] = (
                f"{prior_molt}\n{budget_molt}" if prior_molt else budget_molt
            )
            existing[TOOL_META_CONTEXT_CACHE_MISS_BUDGET_KEY] = budget_ctx[
                TOOL_META_CONTEXT_CACHE_MISS_BUDGET_KEY
            ]
            existing[TOOL_META_CONTEXT_CACHE_MISS_TOKENS_KEY] = budget_ctx[
                TOOL_META_CONTEXT_CACHE_MISS_TOKENS_KEY
            ]
        else:
            meta[TOOL_META_CONTEXT_PENDING_KEY] = budget_ctx

    # Rendered system-prompt size pressure — its own non-colliding key
    # (``system_prompt``), never overwriting ``rebuild``/``molt``. Deterministic
    # current-state projection: present only while strictly above the effective
    # environment threshold (default 40%); absent otherwise, with no merging
    # into the other context lines.
    system_prompt_warning = build_system_prompt_pressure_context(agent)
    if system_prompt_warning:
        existing = meta.get(TOOL_META_CONTEXT_PENDING_KEY)
        if isinstance(existing, dict):
            existing[TOOL_META_CONTEXT_SYSTEM_PROMPT_KEY] = system_prompt_warning
        else:
            meta[TOOL_META_CONTEXT_PENDING_KEY] = {
                TOOL_META_CONTEXT_SYSTEM_PROMPT_KEY: system_prompt_warning
            }

    tool_meta_token_usage = build_tool_meta_token_usage(
        agent, resolved_budget=resolved_budget
    )
    if tool_meta_token_usage:
        meta[TOOL_META_TOKEN_USAGE_PENDING_KEY] = tool_meta_token_usage

    meta["current_tool_result_chars"] = current_tool_result_chars(agent)

    daemon_summary = getattr(agent, "_notification_daemon_summary", None)
    if isinstance(daemon_summary, dict):
        # Bounded current daemon state, derived from the same coherent
        # mini-channel snapshot that drives notification delivery. It stays
        # available even if the attention lane spills its raw event payload.
        meta["daemon"] = daemon_summary

    wake_provenance = getattr(agent, "_notification_wake_provenance", None)
    if isinstance(wake_provenance, dict):
        # Set only while an ASLEEP notification pair is being synthesized; the
        # caller clears it immediately afterwards, so ordinary later results do
        # not repeat or blur the causal wake record.
        meta["notification_wake"] = wake_provenance

    comment = dynamic_adapter_comment(agent)
    if comment:
        # Only the slim dynamic view rides on the tail; the static adapter rules
        # are resident in the ``meta_guidance`` system-prompt section.
        meta["adapter_comment"] = slim_adapter_comment_for_tail(comment)

    # Notifications are attached at the batch boundary to the same final
    # agent_meta snapshot. Producer fingerprints handle delivery dedup; they do
    # not suppress current state on later results.

    return meta


# ---------------------------------------------------------------------------
# Active-state notification stamping — canonical current payload.
# ---------------------------------------------------------------------------


def build_notification_payload(notifications: dict) -> dict:
    """Return active notification payload plus a compact guidance hook.

    Producers own the per-channel envelope under ``notifications``.  Static
    safety/provenance framing lives in resident
    ``meta_guidance.notification_handling``, so the per-result ``_meta`` block
    carries only active sources and channel-owned dynamic payloads.
    """
    sources = [str(source) for source in notifications.keys()]
    payloads: dict = {}
    for source, payload in notifications.items():
        if isinstance(payload, dict):
            payload_for_wire = dict(payload)
        else:
            payload_for_wire = {"data": payload}
        payload_for_wire.pop(NOTIFICATION_GUIDANCE_KEY, None)
        payloads[str(source)] = payload_for_wire

    return {
        NOTIFICATION_GUIDANCE_KEY: {
            "ref": "meta_guidance.notification_handling",
            "sources": sources,
        },
        NOTIFICATIONS_KEY: payloads,
    }




class _ImPersistentLane(NamedTuple):
    """Per-channel parameters for the shared IM persistent-notification lane.

    The preview/fallback/annotate/sanitize machinery is identical across
    curated IM producers; only the channel identity, the producer preview
    window, the agent-side delivery-tracking attributes, the English comment
    wording, and the delivery ``mode`` differ.  Telegram is the reference
    instance; WeChat and Feishu mirror it.

    ``mode`` selects one of two delivery shapes:

    - ``"delta"`` — seed/delta blocks with in-memory delivered-id tracking and
      a ``previous_block`` hook to the prior block (Telegram, WeChat, Feishu).
    - ``"snapshot"`` — email-style: every block carries the producer's current
      bounded context in full under a standing ``snapshot_context_comment``;
      no delivered-id state, no ``previous_block``, no burst/seed comments
      (WhatsApp, whose producer re-sends the last-10 window per event and
      whose replies are gated by the Cloud API 24-hour window).

    ``referenced_comment`` is ``None`` for producers that never attach
    ``referenced_messages`` (reply targets outside the preview window).
    ``media_comment`` is set for producers whose local store keeps only
    type/id metadata for non-text messages.
    """

    channel: str            # e.g. "telegram" — key under notifications.persistent.mcp
    source_key: str         # e.g. "mcp.telegram" — key under agent_meta.notifications.attention
    path: str               # full dotted persistent path, for hooks/comments
    display_name: str       # e.g. "Telegram" — English comment wording
    mode: str               # "delta" or "snapshot" (see class docstring)
    self_outgoing_comment: str
    truncated_comment: str
    # Delta-mode fields (unused for snapshot lanes).
    min_context: int = 0     # seed/delta boundary == producer preview window
    seen_limit: int = 0      # delivered-id cache cap
    delivered_ids_attr: str | None = None  # agent attr: delivered message-id list
    last_tool_id_attr: str | None = None   # agent attr: prior block's tool id
    burst_comment: str | None = None
    referenced_comment: str | None = None
    # Snapshot-mode field: standing context comment on every block.
    snapshot_context_comment: str | None = None
    # Optional per-message hint for non-text messages (any mode).
    media_comment: str | None = None


_TELEGRAM_PERSISTENT_LANE = _ImPersistentLane(
    channel=NOTIFICATION_PERSISTENT_TELEGRAM_CHANNEL,
    source_key="mcp.telegram",
    path=NOTIFICATION_PERSISTENT_TELEGRAM_PATH,
    display_name="Telegram",
    mode="delta",
    min_context=NOTIFICATION_PERSISTENT_TELEGRAM_MIN_CONTEXT,
    seen_limit=NOTIFICATION_PERSISTENT_TELEGRAM_SEEN_LIMIT,
    delivered_ids_attr="_notification_persistent_telegram_message_ids",
    last_tool_id_attr="_notification_persistent_telegram_last_tool_id",
    burst_comment=NOTIFICATION_PERSISTENT_TELEGRAM_BURST_COMMENT,
    self_outgoing_comment=NOTIFICATION_PERSISTENT_TELEGRAM_SELF_OUTGOING_COMMENT,
    truncated_comment=NOTIFICATION_PERSISTENT_TELEGRAM_TRUNCATED_COMMENT,
    referenced_comment=NOTIFICATION_PERSISTENT_TELEGRAM_REFERENCED_COMMENT,
)

_WECHAT_PERSISTENT_LANE = _ImPersistentLane(
    channel=NOTIFICATION_PERSISTENT_WECHAT_CHANNEL,
    source_key="mcp.wechat",
    path=NOTIFICATION_PERSISTENT_WECHAT_PATH,
    display_name="WeChat",
    mode="delta",
    min_context=NOTIFICATION_PERSISTENT_WECHAT_MIN_CONTEXT,
    seen_limit=NOTIFICATION_PERSISTENT_WECHAT_SEEN_LIMIT,
    delivered_ids_attr="_notification_persistent_wechat_message_ids",
    last_tool_id_attr="_notification_persistent_wechat_last_tool_id",
    burst_comment=NOTIFICATION_PERSISTENT_WECHAT_BURST_COMMENT,
    self_outgoing_comment=NOTIFICATION_PERSISTENT_WECHAT_SELF_OUTGOING_COMMENT,
    truncated_comment=NOTIFICATION_PERSISTENT_WECHAT_TRUNCATED_COMMENT,
    # The WeChat producer has no reply-target threading, so it never attaches
    # referenced_messages; the referenced-message pass is skipped for this lane.
    referenced_comment=None,
)

_FEISHU_PERSISTENT_LANE = _ImPersistentLane(
    channel=NOTIFICATION_PERSISTENT_FEISHU_CHANNEL,
    source_key="mcp.feishu",
    path=NOTIFICATION_PERSISTENT_FEISHU_PATH,
    display_name="Feishu",
    mode="delta",
    min_context=NOTIFICATION_PERSISTENT_FEISHU_MIN_CONTEXT,
    seen_limit=NOTIFICATION_PERSISTENT_FEISHU_SEEN_LIMIT,
    delivered_ids_attr="_notification_persistent_feishu_message_ids",
    last_tool_id_attr="_notification_persistent_feishu_last_tool_id",
    burst_comment=NOTIFICATION_PERSISTENT_FEISHU_BURST_COMMENT,
    self_outgoing_comment=NOTIFICATION_PERSISTENT_FEISHU_SELF_OUTGOING_COMMENT,
    truncated_comment=NOTIFICATION_PERSISTENT_FEISHU_TRUNCATED_COMMENT,
    # The Feishu producer threads replies via per-message `reply_to` refs and
    # never attaches out-of-window `referenced_messages`; the referenced pass
    # is skipped for this lane.
    referenced_comment=None,
)

_WHATSAPP_PERSISTENT_LANE = _ImPersistentLane(
    channel=NOTIFICATION_PERSISTENT_WHATSAPP_CHANNEL,
    source_key="mcp.whatsapp",
    path=NOTIFICATION_PERSISTENT_WHATSAPP_PATH,
    display_name="WhatsApp",
    # Snapshot lane (email-style): full bounded context per block, no
    # delivered-id delta state, no previous_block hook — see the class
    # docstring for why WhatsApp deliberately differs from the delta lanes.
    mode="snapshot",
    self_outgoing_comment=NOTIFICATION_PERSISTENT_WHATSAPP_SELF_OUTGOING_COMMENT,
    truncated_comment=NOTIFICATION_PERSISTENT_WHATSAPP_TRUNCATED_COMMENT,
    snapshot_context_comment=NOTIFICATION_PERSISTENT_WHATSAPP_CONTEXT_COMMENT,
    # The WhatsApp local store keeps only type/id metadata for media messages.
    media_comment=NOTIFICATION_PERSISTENT_WHATSAPP_MEDIA_COMMENT,
)

# Ordered registry of IM channels sharing the persistent lane machinery.
_IM_PERSISTENT_LANES = (
    _TELEGRAM_PERSISTENT_LANE,
    _WECHAT_PERSISTENT_LANE,
    _FEISHU_PERSISTENT_LANE,
    _WHATSAPP_PERSISTENT_LANE,
)


def _im_preview_list(notification_payload: dict, source_key: str) -> list[dict]:
    """Return IM notification preview entries from the canonical payload."""
    notifications = notification_payload.get(NOTIFICATIONS_KEY)
    if not isinstance(notifications, dict):
        return []
    channel = notifications.get(source_key)
    if not isinstance(channel, dict):
        return []
    data = channel.get("data")
    if not isinstance(data, dict):
        return []
    previews = data.get("previews")
    if not isinstance(previews, list):
        return []
    return [preview for preview in previews if isinstance(preview, dict)]


def _im_fallback_message_from_preview(preview: dict) -> dict | None:
    """Build a persistent message from a legacy IM preview-only event."""
    preview_text = preview.get("preview")
    if not isinstance(preview_text, str) or not preview_text:
        return None

    msg_id = preview.get("message_ref")
    if not isinstance(msg_id, str) or not msg_id:
        digest_src = "|".join(
            str(preview.get(key, ""))
            for key in ("conversation_ref", "from", "subject", "preview")
        )
        msg_id = "notification-preview:" + _hashlib.sha1(
            digest_src.encode("utf-8", errors="replace")
        ).hexdigest()[:16]

    sender = preview.get("from")
    item: dict = {
        "id": msg_id,
        "direction": "incoming",
        "sender": sender if isinstance(sender, str) and sender else "unknown",
        "text": preview_text,
        "text_truncated": bool(preview.get("preview_truncated")),
        "source": "notification_preview",
    }
    for key in ("subject", "conversation_ref", "platform"):
        value = preview.get(key)
        if isinstance(value, str) and value:
            item[key] = value
    return item


def _im_message_identity(message: dict) -> str | None:
    """Return the dedup/delivery identity for a persistent IM message.

    Prefers the additive per-update ``event_id`` (unique per inbound update —
    repeated callback presses on one keyboard share a compound message ``id``
    but never an ``event_id``); falls back to the legacy compound ``id`` for
    producers/records that do not carry one.
    """
    event_id = message.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg_id = message.get("id")
    if isinstance(msg_id, str) and msg_id:
        return msg_id
    return None


def _im_structured_omission_marker(value: object) -> dict | None:
    """Return *value* when it is an explicit LICC structured-omission marker."""
    if isinstance(value, dict) and value.get("licc_structured_omitted"):
        return value
    return None


def _im_structured_omission_markers_from_notifications(
    notification_payload: dict, source_key: str
) -> list[dict]:
    """Collect explicit ``licc_structured_omitted`` markers from IM previews.

    The LICC consumer replaces an oversize/unserializable curated structured
    family with a marker carrying the reason and the producer's recovery
    handle. Markers are not message candidates — they are carried into the
    persistent lane as explicit metadata so the agent still learns what was
    omitted and how to recover it (never silently dropped).
    """
    markers: list[dict] = []
    seen: set[str] = set()
    for preview in _im_preview_list(notification_payload, source_key):
        for key in ("latest_incoming", "recent_messages", "referenced_messages"):
            marker = _im_structured_omission_marker(preview.get(key))
            if marker is None:
                continue
            dedup_key = _json.dumps(marker, sort_keys=True, default=str)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            markers.append(dict(marker))
    return markers


def _im_persistent_event_from_preview(preview: dict) -> dict | None:
    """Move IM event/routing hook metadata into the persistent lane."""
    event: dict = {}
    for key in (
        "from", "subject", "conversation_ref", "message_ref", "platform",
        "event_id",
    ):
        value = preview.get(key)
        if isinstance(value, str) and value:
            event[key] = value
    if not event:
        return None
    return event


def _im_persistent_events_from_notifications(
    notification_payload: dict, source_key: str
) -> list[dict]:
    """Extract IM event/routing hooks from notification preview metadata."""
    events: list[dict] = []
    for preview in _im_preview_list(notification_payload, source_key):
        event = _im_persistent_event_from_preview(preview)
        if event is not None:
            events.append(event)
    return events


def _im_notification_event_count(notification_payload: dict, source_key: str) -> int:
    """Return the IM notification event count when the producer reports it."""
    notifications = notification_payload.get(NOTIFICATIONS_KEY)
    if not isinstance(notifications, dict):
        return 0
    channel = notifications.get(source_key)
    if not isinstance(channel, dict):
        return 0
    data = channel.get("data")
    if not isinstance(data, dict):
        return 0
    count = data.get("count")
    return count if isinstance(count, int) and count > 0 else 0


def _im_persistent_messages_from_notifications(
    notification_payload: dict, source_key: str
) -> list[dict]:
    """Extract ordered IM message objects from notification preview metadata.

    Prefer the curated structured ``recent_messages`` / ``latest_incoming``
    fields.  If an older or degraded IM notification has only the bounded
    body preview, move that preview into the persistent lane as a fallback
    message so the transient notification never carries IM content.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for preview in _im_preview_list(notification_payload, source_key):
        candidates: list[object] = []
        has_structured = False
        recent = preview.get("recent_messages")
        if isinstance(recent, list):
            candidates.extend(recent)
            has_structured = True
        latest = preview.get("latest_incoming")
        # An explicit LICC omission marker is not a message and must not
        # suppress the bounded preview fallback; it is carried separately
        # (see _im_structured_omission_markers_from_notifications).
        if isinstance(latest, dict) and not _im_structured_omission_marker(latest):
            candidates.append(latest)
            has_structured = True
        if not has_structured:
            fallback = _im_fallback_message_from_preview(preview)
            if fallback is not None:
                candidates.append(fallback)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            msg_id = candidate.get("id")
            if not isinstance(msg_id, str) or not msg_id:
                continue
            # De-duplicate by per-update event identity when present so
            # distinct events sharing one compound message id (repeated
            # callbacks on one keyboard) never collapse into one entry.
            identity = _im_message_identity(candidate) or msg_id
            if identity not in by_id:
                order.append(identity)
            by_id[identity] = dict(candidate)
    return [by_id[identity] for identity in order if identity in by_id]


def _im_referenced_messages_from_notifications(
    notification_payload: dict, source_key: str
) -> list[dict]:
    """Extract full referenced IM messages (reply targets) from previews.

    Curated producers (currently only Telegram) attach the full referenced
    message under ``referenced_messages`` when the current reply targets a
    message outside the preview window. De-duplicate by message ID, preserving
    first-seen order.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for preview in _im_preview_list(notification_payload, source_key):
        referenced = preview.get("referenced_messages")
        if not isinstance(referenced, list):
            continue
        for candidate in referenced:
            if not isinstance(candidate, dict):
                continue
            msg_id = candidate.get("id")
            if not isinstance(msg_id, str) or not msg_id:
                continue
            if msg_id not in by_id:
                order.append(msg_id)
            by_id[msg_id] = dict(candidate)
    return [by_id[msg_id] for msg_id in order if msg_id in by_id]


def _im_display_message_number(compound_id: object) -> str:
    """Return a robust human-facing message number from a producer message ID.

    Telegram compound IDs are ``account:chat:message``; the trailing segment is
    the Telegram message id.  Other producers (WeChat local UUIDs) and
    degraded/fallback ids fall back to the raw value so the range comment never
    crashes on an unexpected shape.
    """
    if not isinstance(compound_id, str) or not compound_id:
        return "?"
    parts = compound_id.split(":")
    if len(parts) == 3 and parts[2]:
        return parts[2]
    return compound_id


def _im_range_context_comment(messages: list[dict], display_name: str) -> str | None:
    """Build the English historical-range comment for a seeded context block.

    Identifies the current/new message (``is_current`` when present, else the
    last incoming message) and describes the remaining messages as historical
    context using robust ids drawn from the producer ids.  Returns ``None`` when
    there is no historical range to describe (e.g. a single-message block).
    """
    if len(messages) < 2:
        return None
    current = next((m for m in messages if m.get("is_current")), None)
    if current is None:
        current = next(
            (m for m in reversed(messages) if m.get("direction") == "incoming"),
            None,
        )
    if current is None:
        current = messages[-1]
    current_id = current.get("id")
    historical = [m for m in messages if m.get("id") != current_id]
    if not historical:
        return None
    first_num = _im_display_message_number(historical[0].get("id"))
    last_num = _im_display_message_number(historical[-1].get("id"))
    current_num = _im_display_message_number(current_id)
    if first_num == last_num:
        span = f"Message {first_num} is historical context"
    else:
        span = f"Messages {first_num}–{last_num} are historical context"
    return (
        f"{span} from the recent {display_name} conversation. "
        f"The current/new message is {current_num}."
    )


def _annotate_im_message(message: dict, lane: _ImPersistentLane) -> dict:
    """Return a copy of *message* with per-message continuity/truncation hints.

    Adds the self-outgoing continuity comment to the agent's own outgoing
    messages, the truncation comment to truncated messages, and (for lanes whose
    local store keeps only type/id metadata) the media comment to non-text
    messages. When several apply, the comments are joined so no signal is
    dropped. Media metadata already on the message is preserved untouched.
    """
    annotated = dict(message)
    hints: list[str] = []
    if annotated.get("direction") == "outgoing":
        hints.append(lane.self_outgoing_comment)
    if annotated.get("text_truncated"):
        hints.append(lane.truncated_comment)
    if lane.media_comment is not None:
        message_type = annotated.get("type")
        if (
            isinstance(message_type, str)
            and message_type not in ("", "text")
            and not annotated.get("text")
        ):
            hints.append(lane.media_comment)
    if hints:
        existing = annotated.get("comment")
        if isinstance(existing, str) and existing:
            hints = [existing, *hints]
        annotated["comment"] = " ".join(hints)
    return annotated


def _email_notification_data(notification_payload: dict) -> dict:
    notifications = notification_payload.get(NOTIFICATIONS_KEY)
    if not isinstance(notifications, dict):
        return {}
    email = notifications.get("email")
    if not isinstance(email, dict):
        return {}
    data = email.get("data")
    return data if isinstance(data, dict) else {}


def _email_notification_email_ids(notification_payload: dict) -> list[str]:
    data = _email_notification_data(notification_payload)
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            ids.append(value)

    raw_ids = data.get("email_ids")
    if isinstance(raw_ids, list):
        for value in raw_ids:
            add(value)
    raw_emails = data.get("emails")
    if isinstance(raw_emails, list):
        for item in raw_emails:
            if isinstance(item, dict):
                add(item.get("id"))
    return ids


def _email_persistent_emails(notification_payload: dict) -> list[dict]:
    data = _email_notification_data(notification_payload)
    raw_emails = data.get("emails")
    if not isinstance(raw_emails, list):
        return []
    emails: list[dict] = []
    for item in raw_emails:
        if not isinstance(item, dict):
            continue
        email = dict(item)
        if (
            email.get("message_truncated") or email.get("preview_truncated")
        ) and not email.get("comment"):
            email["comment"] = NOTIFICATION_PERSISTENT_EMAIL_TRUNCATED_COMMENT
        emails.append(email)
    return emails


def _build_email_notification_persistent_payload(agent, notification_payload: dict) -> dict | None:
    data = _email_notification_data(notification_payload)
    if not data:
        return None

    email_ids = _email_notification_email_ids(notification_payload)
    emails = _email_persistent_emails(notification_payload)
    count = data.get("count")
    newest_received_at = data.get("newest_received_at")
    if not (email_ids or emails):
        return None

    payload: dict = {
        "context_comment": NOTIFICATION_PERSISTENT_EMAIL_CONTEXT_COMMENT,
    }
    if email_ids:
        payload["email_ids"] = email_ids
    if isinstance(count, int):
        payload["count"] = count
    if isinstance(newest_received_at, str) and newest_received_at:
        payload["newest_received_at"] = newest_received_at
    if emails:
        payload["emails"] = emails

    return payload


def _build_snapshot_im_persistent_payload(
    notification_payload: dict,
    lane: _ImPersistentLane,
    candidates: list[dict],
    events: list[dict],
    *,
    omission_markers: list[dict] | None = None,
) -> dict:
    """Build a snapshot (email-style) persistent payload for one IM lane.

    Every block carries the producer's current bounded conversation context in
    full under a standing ``context_comment``.  There is no delivered-id delta
    tracking, no ``previous_block`` hook, and no burst/seed comments: the
    snapshot lane re-emits the producer's current window each material update
    and the producer tool remains the source of truth (building this block marks
    nothing read).  Per-message continuity/truncation/media comments are applied
    via the shared ``_annotate_im_message`` helper.
    """
    annotated = [_annotate_im_message(message, lane) for message in candidates]
    payload: dict = {
        "context_comment": lane.snapshot_context_comment,
        "messages": annotated,
    }
    count = _im_notification_event_count(notification_payload, lane.source_key)
    if count:
        payload["count"] = count
    if events:
        payload["events"] = events
    if omission_markers:
        payload["structured_omitted"] = omission_markers
    return payload


def _build_im_notification_persistent_payload(
    agent, notification_payload: dict, lane: _ImPersistentLane
) -> dict | None:
    """Build the `_meta.agent_meta.notifications.persistent` payload for one IM lane.

    ``mode == "delta"`` lanes (Telegram, WeChat, Feishu) use the seed/delta
    shape: the first delivery after startup/molt (or when fewer than the minimum
    number of messages has been delivered into the current provider context)
    carries the recent context snapshot, and later material notification updates
    only carry messages whose producer message IDs have not been delivered yet,
    plus a ``previous_block`` hook pointing at the prior block for this lane.

    ``mode == "snapshot"`` lanes (WhatsApp, email-style) re-emit the producer's
    current bounded context in full on every material update, with no
    delivered-id state and no ``previous_block`` hook; see
    ``_build_snapshot_im_persistent_payload``.
    """
    candidates = _im_persistent_messages_from_notifications(
        notification_payload, lane.source_key
    )
    events = _im_persistent_events_from_notifications(
        notification_payload, lane.source_key
    )
    omission_markers = _im_structured_omission_markers_from_notifications(
        notification_payload, lane.source_key
    )
    if not candidates and not events and not omission_markers:
        return None

    if lane.mode == "snapshot":
        return _build_snapshot_im_persistent_payload(
            notification_payload, lane, candidates, events,
            omission_markers=omission_markers,
        )

    delivered = getattr(agent, lane.delivered_ids_attr, [])
    if not isinstance(delivered, (list, tuple, set)):
        delivered = []
    delivered_ids = {msg_id for msg_id in delivered if isinstance(msg_id, str)}
    previous_tool_id = getattr(agent, lane.last_tool_id_attr, None)
    has_previous_block = isinstance(previous_tool_id, str)
    # Provider context can be fresh after molt/restart even when an in-memory
    # delivered-id cache survived.  Only treat delivered_ids as enough recent
    # context when the current provider context also has a previous persistent
    # block for this lane to link to.
    has_recent_context = (
        has_previous_block
        and len(delivered_ids) >= lane.min_context
    )

    is_seed_block = False
    if candidates and has_recent_context:
        messages = [
            message
            for message in candidates
            if isinstance(message.get("id"), str)
            and _im_message_identity(message) not in delivered_ids
        ]
    elif candidates:
        messages = candidates[-lane.min_context:]
        is_seed_block = True
    else:
        messages = []

    if not messages and not events and not omission_markers:
        return None

    # Newly-arrived (not previously delivered) incoming messages drive the burst
    # hint; annotate per-message continuity/truncation comments before the count.
    new_incoming = 0
    annotated_messages: list[dict] = []
    for message in messages:
        annotated_messages.append(_annotate_im_message(message, lane))
        identity = _im_message_identity(message)
        if (
            message.get("direction") == "incoming"
            and isinstance(identity, str)
            and identity not in delivered_ids
        ):
            new_incoming += 1
    messages = annotated_messages

    lane_payload: dict = {"messages": messages}

    # Seed blocks describe the historical range and the current/new message so
    # the agent does not have to re-derive which id is new from the raw list.
    if is_seed_block:
        range_comment = _im_range_context_comment(messages, lane.display_name)
        if range_comment:
            lane_payload["context_comment"] = range_comment

    # Burst: multiple genuinely new incoming messages arrived together.  Seed
    # blocks carry historical preview-window context, so do not count those
    # historical messages as a burst unless the producer's notification count
    # says multiple new events triggered this block.
    event_count = _im_notification_event_count(notification_payload, lane.source_key)
    if (not is_seed_block and new_incoming >= 2) or event_count >= 2:
        lane_payload["burst_comment"] = lane.burst_comment

    # Full referenced reply target(s) missing from the messages list — only for
    # lanes whose producer attaches referenced_messages (currently Telegram).
    if lane.referenced_comment is not None:
        referenced = _im_referenced_messages_from_notifications(
            notification_payload, lane.source_key
        )
        present_ids = {
            identity
            for identity in (_im_message_identity(m) for m in messages)
            if isinstance(identity, str)
        }
        referenced_out: list[dict] = []
        for ref in referenced:
            if _im_message_identity(ref) in present_ids:
                continue
            annotated = _annotate_im_message(ref, lane)
            existing = annotated.get("comment")
            if isinstance(existing, str) and existing:
                annotated["comment"] = f"{lane.referenced_comment} {existing}"
            else:
                annotated["comment"] = lane.referenced_comment
            referenced_out.append(annotated)
        if referenced_out:
            lane_payload["referenced_messages"] = referenced_out

    if events:
        lane_payload["events"] = events

    # Explicit LICC structured-omission markers (oversize/unserializable
    # curated family) ride into the persistent lane so the agent still gets
    # the omission reason and the producer's recovery handle.
    if omission_markers:
        lane_payload["structured_omitted"] = omission_markers

    # Every persistent block carries an explicit hook to the previous block for
    # its lane (Jason #6148). The first block after startup/molt has no
    # predecessor: it is marked `is_first_block: true` with `tool_result_id: null`.
    # Later blocks point `tool_result_id` at the prior tool result id.
    is_first_block = not has_previous_block
    previous_block: dict = {
        "path": lane.path,
        "tool_result_id": previous_tool_id if has_previous_block else None,
    }
    if is_first_block:
        previous_block["is_first_block"] = True
    else:
        previous_block["comment"] = (
            f"For earlier {lane.display_name} context, see tool result "
            f"{previous_tool_id} at {lane.path}."
        )
    lane_payload["previous_block"] = previous_block

    return lane_payload


def _notification_persistent_envelope_chars(persistent: dict) -> int:
    """Return the serialized size of the model-visible persistent envelope.

    Measures exactly what the provider sees (the ``notification_persistent``
    wrapper key included).  The canonical provider converters re-serialize
    projected dictionaries with default ASCII escaping
    (``json.dumps(..., default=str)``), so the ruler uses the same escaping
    (``ensure_ascii=True``): multilingual content is counted exactly as the
    provider will serialize it.  An unserializable payload is reported as
    ``0`` so the cap never turns a serialization problem into a
    spill/compaction.
    """
    try:
        return len(
            _json.dumps(
                {NOTIFICATION_PERSISTENT_KEY: persistent},
                ensure_ascii=True,
                sort_keys=True,
                default=str,
            )
        )
    except (TypeError, ValueError):
        return 0


def _spill_notification_persistent(agent, envelope: dict) -> str | None:
    """Write the full persistent envelope to the agent's ``logs/`` dir.

    Returns the absolute spill path, or ``None`` when the agent has no working
    directory or the write failed — the block is still compacted either way, the
    agent just gets the producer tool instead of a file as the recovery handle.
    """
    workdir = getattr(agent, "_working_dir", None)
    if not workdir:
        return None
    try:
        logs_dir = Path(workdir) / "logs"
        stamp = int(_time.time())
        path = logs_dir / f"{NOTIFICATION_PERSISTENT_OVERFLOW_FILE_PREFIX}{stamp}.json"
        # A second overflow inside the same second must not clobber the file the
        # previous block already handed to the model as its recovery handle.
        suffix = 1
        while path.exists() and suffix <= 100:
            path = logs_dir / (
                f"{NOTIFICATION_PERSISTENT_OVERFLOW_FILE_PREFIX}{stamp}-{suffix}.json"
            )
            suffix += 1
        # Atomic sibling-temp + os.replace, ensure_ascii=False, indent=2.
        written = atomic_write_json(path, envelope, default=str)
        if not written.is_absolute():
            written = written.resolve()
        return str(written)
    except (OSError, TypeError, ValueError):
        return None


def _truncate_persistent_value(value: object, budget: int) -> tuple[object, bool]:
    """Return ``(value, changed)`` with a string *value* capped at *budget*."""
    if not isinstance(value, str) or len(value) <= budget:
        return value, False
    if budget <= 0:
        return "", True
    return value[:budget] + "...", True


def _compact_persistent_record(record: object, fields: tuple[str, ...], budget: int) -> object:
    """Return a copy of *record* with its heavy string *fields* truncated."""
    if not isinstance(record, dict):
        return record
    compacted = dict(record)
    for field in fields:
        value, changed = _truncate_persistent_value(compacted.get(field), budget)
        if changed:
            compacted[field] = value
    return compacted


def _compact_email_persistent_lane(
    lane_payload: dict, budget: int, comment: str | None
) -> dict:
    """Compact the email lane: keep every email and its routing/id fields.

    *comment* is the per-email spill note; it is ``None`` at the tighter budgets,
    where repeating it once per email would cost more context than the
    truncation saves (the spill path stays discoverable on the block's
    top-level ``overflow`` marker).
    """
    compacted = dict(lane_payload)
    emails = compacted.get("emails")
    if not isinstance(emails, list):
        return compacted
    out: list = []
    for item in emails:
        if not isinstance(item, dict):
            out.append(item)
            continue
        email = _compact_persistent_record(
            item, NOTIFICATION_PERSISTENT_EMAIL_HEAVY_FIELDS, budget
        )
        if comment is not None:
            existing = item.get("comment")
            email["comment"] = (
                f"{existing} {comment}"
                if isinstance(existing, str) and existing
                else comment
            )
        out.append(email)
    compacted["emails"] = out
    return compacted


def _compact_im_persistent_lane(lane_payload: dict, budget: int) -> dict:
    """Compact one IM lane: keep every message and its identity fields."""
    compacted = dict(lane_payload)
    for key in ("messages", "referenced_messages"):
        records = compacted.get(key)
        if isinstance(records, list):
            compacted[key] = [
                _compact_persistent_record(
                    record, NOTIFICATION_PERSISTENT_IM_HEAVY_FIELDS, budget
                )
                for record in records
            ]
    return compacted


def _compact_notification_persistent(
    persistent: dict, budget: int, overflow_marker: dict, comment: str | None
) -> dict:
    """Return a fresh compacted copy of *persistent* at one per-field budget."""
    compacted: dict = {}
    for key, value in persistent.items():
        if key == NOTIFICATION_PERSISTENT_EMAIL_CHANNEL and isinstance(value, dict):
            compacted[key] = _compact_email_persistent_lane(value, budget, comment)
        elif key == NOTIFICATION_PERSISTENT_MCP_KEY and isinstance(value, dict):
            compacted[key] = {
                channel: (
                    _compact_im_persistent_lane(lane, budget)
                    if isinstance(lane, dict)
                    else lane
                )
                for channel, lane in value.items()
            }
        else:
            compacted[key] = value
    # Kernel-owned marker: overwrite any producer key of the same name.
    compacted[NOTIFICATION_PERSISTENT_OVERFLOW_KEY] = dict(overflow_marker)
    return compacted


def _stub_persistent_record(record: dict) -> dict:
    """Return the id-only stub that stands in for a dropped message.

    ``event_id`` rides along because it is the delivery identity preferred by
    ``_im_message_identity``; without it a dropped message would be recorded
    under the wrong identity and re-delivered forever.
    """
    stub: dict = {}
    for key in ("id", "event_id"):
        value = record.get(key)
        if value is not None:
            stub[key] = value
    return stub


def _drop_notification_persistent_records(
    persistent: dict, max_chars: int | None = None
) -> dict:
    """Replace the oldest messages with id-only stubs until the envelope fits.

    Pathological last resort, reached only when every heavy field is already
    empty.  Messages are never removed from the list: each dropped message
    leaves an ``{"id": ..., "event_id": ...}`` stub (so
    ``record_notification_persistent_delivery`` still records it and the agent
    never re-receives it forever) plus its id in the lane's ``dropped_ids``.
    """
    lanes: list[tuple[dict, str]] = []
    email_lane = persistent.get(NOTIFICATION_PERSISTENT_EMAIL_CHANNEL)
    if isinstance(email_lane, dict) and isinstance(email_lane.get("emails"), list):
        lanes.append((email_lane, "emails"))
    mcp = persistent.get(NOTIFICATION_PERSISTENT_MCP_KEY)
    if isinstance(mcp, dict):
        for lane_payload in mcp.values():
            if isinstance(lane_payload, dict) and isinstance(
                lane_payload.get("messages"), list
            ):
                lanes.append((lane_payload, "messages"))
    if not lanes:
        return persistent

    cursors = [0] * len(lanes)
    progressed = True
    if max_chars is None:
        max_chars = _notification_persistent_max_chars()
    while progressed:
        if _notification_persistent_envelope_chars(persistent) <= max_chars:
            return persistent
        progressed = False
        for slot, (lane_payload, key) in enumerate(lanes):
            records = lane_payload[key]
            index = cursors[slot]
            while index < len(records) and not isinstance(records[index], dict):
                index += 1
            if index >= len(records):
                cursors[slot] = index
                continue
            record = records[index]
            records[index] = _stub_persistent_record(record)
            dropped = lane_payload.setdefault("dropped_ids", [])
            record_id = record.get("id")
            if isinstance(record_id, str) and record_id and record_id not in dropped:
                dropped.append(record_id)
            cursors[slot] = index + 1
            progressed = True
    return persistent


def _drop_notification_persistent_terminal(
    persistent: dict, marker: dict, max_chars: int
) -> dict:
    """Return a marker-only persistent envelope that strictly fits *max_chars*.

    Pathological last resort, reached only when even the id-only stub envelope
    still exceeds the cap.  The kernel-owned overflow marker is the recovery
    handle; when the absolute spill path is pathologically long, the marker
    path is stripped (``path=None``, ``path_omitted=True``) and the exact spill
    basename (including any ``-N`` suffix) is retained in ``spill_file`` so the
    file stays findable.  With the 2048 floor this compact envelope ALWAYS
    satisfies ``len(json.dumps(..., default=str)) <= max_chars`` for the
    bounded production spill basename; the production allocator never produces
    an unbounded basename (it is a timestamped ``notification-overflow-<ts>.json``
    sibling created in the agent's own ``logs/`` directory).
    """
    envelope = {NOTIFICATION_PERSISTENT_OVERFLOW_KEY: dict(marker)}
    if _notification_persistent_envelope_chars(envelope) <= max_chars:
        return envelope
    compact_marker = dict(marker)
    spill_path = marker.get("path")
    spill_basename = (
        Path(spill_path).name if isinstance(spill_path, str) and spill_path else None
    )
    compact_marker["path"] = None
    compact_marker["path_omitted"] = True
    if spill_basename:
        compact_marker["spill_file"] = spill_basename
    return {NOTIFICATION_PERSISTENT_OVERFLOW_KEY: compact_marker}


def _outer_notification_max_chars(agent) -> int | None:
    """Return a positive int from the outer ``resolve_notification_max_chars`` hook.

    The outer Agent lazily projects the System-owned ``settings/system.json``
    value through this narrow hook; Core knows no System file syntax, env
    vocabulary, or path. A bare kernel stub, a missing or failing hook, or a
    non-positive/non-int return yields ``None`` so the caller keeps its fixed
    default.
    """
    if agent is None:
        return None
    try:
        resolve = getattr(agent, "resolve_notification_max_chars", None)
    except Exception:
        return None
    if not callable(resolve):
        return None
    try:
        value = resolve()
    except Exception:
        return None
    if type(value) is not int or value <= 0:
        return None
    return value


def _notification_max_chars(agent, *, default: int, floor: int, ceiling: int) -> int:
    """Shared resolver for both lanes: live env > outer System hook > default.

    A positive configured value from either source is clamped to
    [*floor*, *ceiling*]; missing, blank, non-numeric, zero, or negative env
    values fall through to the outer hook, and an absent/invalid hook value
    falls back to *default*.
    """
    raw = os.environ.get(NOTIFICATION_PERSISTENT_MAX_CHARS_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        # Env values are always str, so ``int(raw)`` never yields a ``bool``;
        # the guard is kept for symmetry with the other env int parsers.
        if not isinstance(value, bool) and value > 0:
            return min(max(value, floor), ceiling)
    configured = _outer_notification_max_chars(agent)
    if configured is not None:
        return min(max(configured, floor), ceiling)
    return default


def _notification_persistent_max_chars(agent=None) -> int:
    """Return the effective model-visible persistent notification cap.

    Live-read ``LINGTAI_NOTIFICATION_MAX_CHARS`` at every payload build (no
    restart needed, like the nudge env vars), then the outer Agent's
    ``resolve_notification_max_chars()`` hook (the System-owned
    ``settings/system.json`` v2 ``notification_max_chars`` field; ``None`` for
    a bare kernel stub), then the default 10,000.  A positive value from
    either source is clamped to [``NOTIFICATION_PERSISTENT_MAX_CHARS_MIN``
    (2048), ``NOTIFICATION_PERSISTENT_MAX_CHARS_CEILING`` (10,000)] so the
    context-size fix cannot be silently disabled and the terminal record-stub
    recovery envelope ALWAYS fits (the 2048 floor mirrors the attention lane's
    floor: ONE shared bar means ONE shared floor).  Missing, blank,
    non-numeric, zero, or negative env values fall through.  Kept as a
    function rather than a module constant so an operator can tighten or relax
    the cap per-process without a code change.
    """
    return _notification_max_chars(
        agent,
        default=NOTIFICATION_PERSISTENT_MAX_CHARS,
        floor=NOTIFICATION_PERSISTENT_MAX_CHARS_MIN,
        ceiling=NOTIFICATION_PERSISTENT_MAX_CHARS_CEILING,
    )


def _cap_notification_persistent(agent, persistent: dict) -> dict:
    """Return *persistent* unchanged, or a compacted copy plus a spill file.

    At or under ``NOTIFICATION_PERSISTENT_MAX_CHARS`` this is a no-op: no spill
    file, no marker, byte-identical block.  Over the cap the full block is
    spilled to disk and the returned copy carries an ``overflow`` marker with
    the spill path, the original size, and truncated content.
    """
    full_chars = _notification_persistent_envelope_chars(persistent)
    max_chars = _notification_persistent_max_chars(agent)
    if full_chars <= max_chars:
        return persistent

    spill_path = _spill_notification_persistent(
        agent, {NOTIFICATION_PERSISTENT_KEY: persistent}
    )
    marker: dict = {
        "path": spill_path,
        "full_chars": full_chars,
        "truncated": True,
    }
    if spill_path:
        comment = NOTIFICATION_PERSISTENT_OVERFLOW_COMMENT.format(path=spill_path)
    else:
        marker["spill_failed"] = True
        comment = NOTIFICATION_PERSISTENT_OVERFLOW_NO_SPILL_COMMENT

    compacted = persistent
    widest = NOTIFICATION_PERSISTENT_COMPACT_BUDGETS[0]
    for budget in NOTIFICATION_PERSISTENT_COMPACT_BUDGETS:
        compacted = _compact_notification_persistent(
            persistent, budget, marker, comment if budget >= widest else None
        )
        if (
            _notification_persistent_envelope_chars(compacted)
            <= max_chars
        ):
            return compacted
    dropped = _drop_notification_persistent_records(compacted, max_chars)
    if _notification_persistent_envelope_chars(dropped) <= max_chars:
        return dropped
    return _drop_notification_persistent_terminal(dropped, marker, max_chars)


def _notification_attention_envelope_chars(attention: dict) -> int:
    """Return the serialized size of the model-visible attention lane.

    Measures exactly what the provider sees under
    ``_meta.agent_meta.notifications.attention``.  The canonical Anthropic,
    OpenAI Chat/Responses, and Gemini ToolResultBlock converters re-serialize
    projected dictionaries with default ASCII escaping
    (``json.dumps(..., default=str)``, i.e. ``ensure_ascii=True``), so the
    ruler uses the same escaping: multilingual content is counted exactly as
    the provider will serialize it and cannot silently cross the cap on the
    wire after the ruler reports it under.  An unserializable payload is
    reported as ``0`` so the cap never turns a serialization problem into a
    spill/compaction.
    """
    try:
        return len(
            _json.dumps(attention, ensure_ascii=True, sort_keys=True, default=str)
        )
    except (TypeError, ValueError):
        return 0


def _notification_attention_max_chars(agent=None) -> int:
    """Return the effective model-visible attention notification cap.

    Shares the ``LINGTAI_NOTIFICATION_MAX_CHARS`` env bar — and the outer
    Agent's ``resolve_notification_max_chars()`` System-file hook behind it —
    with the persistent lane so one operator control enforces the upper limit
    across all notification channels (Jason 2026-08-13).  A positive value
    from either source is clamped to [``NOTIFICATION_ATTENTION_MAX_CHARS_MIN``
    (2048), ``NOTIFICATION_ATTENTION_MAX_CHARS_CEILING`` (10,000)]: configured
    values below 2048 are clamped UP to 2048 so the terminal marker-only
    recovery envelope (which carries an absolute spill path) ALWAYS fits, and
    values above 10,000 clamp down so the context-size fix cannot be silently
    disabled.  The cap is still a strict upper bound on the model-visible
    envelope.  Missing, blank, non-numeric, zero, or negative env values fall
    through; with no valid hook value the default 10,000 applies.
    """
    return _notification_max_chars(
        agent,
        default=NOTIFICATION_ATTENTION_MAX_CHARS,
        floor=NOTIFICATION_ATTENTION_MAX_CHARS_MIN,
        ceiling=NOTIFICATION_ATTENTION_MAX_CHARS_CEILING,
    )


def _attention_spill_canonical_text(attention: dict) -> str:
    """Return the canonical serialization that defines the spill identity."""
    return _json.dumps(attention, ensure_ascii=False, sort_keys=True, default=str)


def _attention_spill_digest8(attention: dict) -> str:
    """Return the short sha256 content digest used in the spill file name."""
    return _hashlib.sha256(
        _attention_spill_canonical_text(attention).encode("utf-8")
    ).hexdigest()[:NOTIFICATION_ATTENTION_OVERFLOW_DIGEST_LENGTH]


def _attention_spill_matches(path: Path, attention: dict) -> bool:
    """True when *path* already holds the same attention payload.

    Two writers race on the same content-addressed name; the loser must verify
    that the winner's file is genuinely the same payload before reusing it (an
    existing recovery handle is never overwritten).
    """
    try:
        existing = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        return _attention_spill_canonical_text(existing) == _attention_spill_canonical_text(
            attention
        )
    except (TypeError, ValueError):
        return False


def _attention_spill_create_exclusive(path: Path, attention: dict) -> Path | None:
    """Write *attention* to a unique sibling temp and link it exclusively.

    ``os.link`` is atomic and fails with ``FileExistsError`` when *path* already
    exists (``O_EXCL`` semantics), so a two-writer race can never overwrite an
    existing recovery handle.  Returns *path* when this writer created it, or
    ``None`` when the name was already taken (the winner owns it; callers verify
    content next).
    """
    tmp = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
    try:
        atomic_write_json(tmp, attention, default=str)
        os.link(str(tmp), str(path))
        return path
    except FileExistsError:
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _attention_spill_allocate(
    logs_dir: Path, digest: str, attention: dict
) -> Path | None:
    """Exclusive-create the content-addressed spill name, with a bounded fallback.

    Primary name ``notification-attention-overflow-<digest>.json``; on a content
    collision (existing file with a DIFFERENT payload) fall back to a bounded
    suffix loop with exclusive create, never overwriting.  Returns the allocated
    path, or ``None`` when no name could be allocated.
    """
    base = logs_dir / f"{NOTIFICATION_ATTENTION_OVERFLOW_FILE_PREFIX}{digest}.json"
    for suffix in range(NOTIFICATION_ATTENTION_SPILL_SUFFIX_ATTEMPTS + 1):
        candidate = (
            base
            if suffix == 0
            else logs_dir / f"{NOTIFICATION_ATTENTION_OVERFLOW_FILE_PREFIX}{digest}-{suffix}.json"
        )
        created = _attention_spill_create_exclusive(candidate, attention)
        if created is not None:
            return created
        if _attention_spill_matches(candidate, attention):
            return candidate
    return None


def _spill_notification_attention(agent, attention: dict) -> str | None:
    """Write the full attention lane once, content-addressed and exclusive.

    The file name embeds a short sha256 digest of the lane's canonical
    serialization (``logs/notification-attention-overflow-<digest8>.json``), so
    an unchanged oversized payload — re-stamped on every tool batch and every
    IDLE/ASLEEP synthesized pair — reuses the SAME file instead of re-spilling
    every batch (no disk amplification, stable marker path).  The file is
    created with an exclusive ``os.link`` from a unique sibling temp: a
    two-writer race can never overwrite an existing recovery handle; the loser
    verifies the existing file holds the same payload and reuses it.  A hash
    collision with different content (practically impossible) falls back to a
    bounded exclusive suffix loop; if no name can be allocated the spill fails.

    Returns the absolute spill path, or ``None`` when the agent has no working
    directory or no exclusive name could be allocated — the lane is still
    compacted either way, the agent just gets the producer tool instead of a
    file as the recovery handle.  The spill file always holds the FULL original
    attention lane.
    """
    workdir = getattr(agent, "_working_dir", None)
    if not workdir:
        return None
    try:
        logs_dir = Path(workdir) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        digest = _attention_spill_digest8(attention)
        path = _attention_spill_allocate(logs_dir, digest, attention)
        if path is None:
            return None
        return str(path.resolve())
    except (OSError, TypeError, ValueError):
        return None


def _compact_attention_node(node, budget: int) -> tuple[object, bool]:
    """Return ``(compacted, changed)`` with heavy attention strings truncated.

    Walks the attention lane's per-source payloads (dicts and lists) and caps
    every heavy free-text field at *budget*; structural fields (ids, routing,
    counts, subjects, dates) are left untouched.
    """
    if isinstance(node, dict):
        out: dict = {}
        changed = False
        for key, value in node.items():
            if key in NOTIFICATION_ATTENTION_HEAVY_FIELDS and isinstance(value, str):
                new_value, this_changed = _truncate_persistent_value(value, budget)
                if this_changed:
                    changed = True
                out[key] = new_value
            elif isinstance(value, (dict, list)):
                new_value, this_changed = _compact_attention_node(value, budget)
                if this_changed:
                    changed = True
                out[key] = new_value
            else:
                out[key] = value
        return out, changed
    if isinstance(node, list):
        out_list = []
        changed = False
        for item in node:
            if isinstance(item, (dict, list)):
                new_item, this_changed = _compact_attention_node(item, budget)
                if this_changed:
                    changed = True
                out_list.append(new_item)
            else:
                out_list.append(item)
        return out_list, changed
    return node, False


# Routing keys preserved in the terminal attention stub, in order of
# increasing criticality.  ``message_ids`` is the stable IM routing identifier
# produced by ``_sanitize_im_notification_after_persistent`` and is kept for as
# long as any routing remains (the bounded degradation drops or bounds the
# other keys first).
NOTIFICATION_ATTENTION_ROUTING_KEYS = (
    "count",
    "email_ids",
    "message_ref",
    "event_id",
    "event_ids",
    "ref",
    "ref_id",
    "message_ids",
)


def _attention_stub_drop_keys(stub: dict, keys: tuple[str, ...]) -> None:
    """Drop *keys* from every per-source routing payload in *stub*."""
    for payload in stub.values():
        if not isinstance(payload, dict):
            continue
        for key in keys:
            payload.pop(key, None)
        data = payload.get("data")
        if isinstance(data, dict):
            for key in keys:
                data.pop(key, None)


def _attention_stub_bound_id_lists(stub: dict) -> None:
    """Bound ``email_ids``/``message_ids`` lists to a deterministic head."""
    for payload in stub.values():
        if not isinstance(payload, dict):
            continue
        holders = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            holders.append(data)
        for holder in holders:
            for key in ("email_ids", "message_ids"):
                value = holder.get(key)
                if isinstance(value, list) and len(value) > NOTIFICATION_ATTENTION_ROUTING_ID_HEAD:
                    holder[key] = value[:NOTIFICATION_ATTENTION_ROUTING_ID_HEAD]


def _drop_notification_attention_records(
    attention: dict, marker: dict, comment: str, max_chars: int
) -> dict:
    """Return a routing-only attention stub that strictly fits *max_chars*.

    Pathological last resort, reached only when every heavy field is already
    empty and the structural metadata alone still exceeds the cap.  The stub
    keeps per-source routing (``count`` and id-bearing fields: ``email_ids``,
    ``message_ref``, ``event_id`` lists, and — critically — ``message_ids``,
    the stable IM routing identifier) so the agent still knows which channels
    have events and which producer records to read, while dropping heavy
    preview bodies.  The kernel-owned overflow marker and the recovery comment
    ride on the stub.

    The configured cap is enforced STRICTLY on the serialized stub (the old
    code returned a stub that was still over cap with ``message_ids`` missing).
    When the routing stub is over cap, a deterministic bounded degradation
    drops the least-critical routing keys in a fixed order — first
    ``ref``/``ref_id``, then ``event_id``/``event_ids``, then ``message_ref``,
    then bounds the ``email_ids``/``message_ids`` lists to a head of 8, then
    drops ``count`` — re-measuring after each phase until it fits.
    ``message_ids`` is kept as long as possible (it is the IM routing
    identifier).  Under a pathologically small cap the recovery comment (long
    relative to a tiny cap) is dropped before any routing id, then the per-
    source routing itself, ending at the minimal marker-only envelope.  A final
    guard makes the terminal envelope capped BY CONSTRUCTION: when even the
    marker-only envelope exceeds the cap (pathologically long absolute spill
    path), the marker path is stripped (``path=None``, ``path_omitted=True``)
    and the compact envelope is returned with the exact spill basename
    (including any ``-N`` suffix) in a short recovery comment.  With the 2048
    floor, that compact envelope ALWAYS satisfies
    ``len(json.dumps(..., default=str)) <= max_chars`` for the bounded
    production basename; the runtime allocator bounds the spill basename to
    ``notification-attention-overflow-<digest8>[-<N>].json`` (digest8 plus at
    most a two-digit suffix under the 100-attempt bound), so an arbitrary
    white-box 3,000-character basename is not a production input.  The spill
    file remains on disk, findable by its deterministic content-addressed name.
    """
    stub: dict = {}
    for source, payload in attention.items():
        if source in (NOTIFICATION_ATTENTION_OVERFLOW_KEY, "comment"):
            continue
        if not isinstance(payload, dict):
            stub[source] = payload
            continue
        routed: dict = {}
        for key, value in payload.items():
            if key == "data" and isinstance(value, dict):
                routed[key] = {
                    k: v for k, v in value.items() if k in NOTIFICATION_ATTENTION_ROUTING_KEYS
                }
            elif key in NOTIFICATION_ATTENTION_ROUTING_KEYS:
                routed[key] = value
        stub[source] = routed
    stub[NOTIFICATION_ATTENTION_OVERFLOW_KEY] = dict(marker)
    stub["comment"] = comment

    def fits(candidate: dict) -> bool:
        return _notification_attention_envelope_chars(candidate) <= max_chars

    if fits(stub):
        return stub

    for phase_keys in (
        ("ref", "ref_id"),
        ("event_id", "event_ids"),
        ("message_ref",),
    ):
        _attention_stub_drop_keys(stub, phase_keys)
        if fits(stub):
            return stub

    _attention_stub_bound_id_lists(stub)
    if fits(stub):
        return stub

    _attention_stub_drop_keys(stub, ("count",))
    if fits(stub):
        return stub

    # Pathologically small cap: drop the long recovery comment before any
    # routing id, then the per-source routing, ending at the minimal
    # marker-only envelope (the guard: if nothing fits, the marker still
    # survives as the recovery handle).
    stub.pop("comment", None)
    if fits(stub):
        return stub
    marker_only = {
        NOTIFICATION_ATTENTION_OVERFLOW_KEY: stub.get(
            NOTIFICATION_ATTENTION_OVERFLOW_KEY, dict(marker)
        )
    }
    if fits(marker_only):
        return marker_only

    # FINAL GUARD: even the marker-only envelope exceeds the cap because the
    # absolute spill path is pathologically long.  Strip the path, record the
    # omission and the actual spill basename (the deterministic
    # content-addressed name, INCLUDING any ``-N`` suffix allocated when the
    # unsuffixed base name was already occupied by a different payload) so the
    # recovery comment points at the exact file on disk, and re-attach the
    # short recovery comment.  With the 2048 floor this compact envelope
    # ALWAYS satisfies ``len(json.dumps(..., default=str)) <= max_chars``.
    compact_marker = dict(marker)
    spill_path = marker.get("path")
    spill_basename = (
        Path(spill_path).name if isinstance(spill_path, str) and spill_path else None
    )
    compact_marker["path"] = None
    compact_marker["path_omitted"] = True
    if spill_basename:
        compact_marker["spill_file"] = spill_basename
        comment = NOTIFICATION_ATTENTION_OVERFLOW_PATH_OMITTED_COMMENT.format(
            name=spill_basename
        )
    else:
        # No path at all (spill failed earlier): keep the digest-only recovery
        # hint (the deterministic content-addressed name still locates the file
        # if any later spill succeeded under the same digest).
        compact_marker["digest"] = _attention_spill_digest8(attention)
        comment = NOTIFICATION_ATTENTION_OVERFLOW_PATH_OMITTED_COMMENT.format(
            name=f"notification-attention-overflow-{compact_marker['digest']}.json"
        )
    return {
        NOTIFICATION_ATTENTION_OVERFLOW_KEY: compact_marker,
        "comment": comment,
    }


def _cap_notification_attention(agent, attention: dict) -> dict:
    """Return *attention* unchanged, or a compacted copy plus a spill file.

    At or under ``NOTIFICATION_ATTENTION_MAX_CHARS`` this is a no-op: no spill
    file, no marker, byte-identical block.  Over the cap the full attention
    lane is spilled to disk and the returned copy carries an ``overflow``
    marker with the spill path, the original size, and a comment that guides
    the agent to read the file-based notification (or use the producer tool
    when the spill failed).
    """
    full_chars = _notification_attention_envelope_chars(attention)
    max_chars = _notification_attention_max_chars(agent)
    if full_chars <= max_chars:
        return attention

    spill_path = _spill_notification_attention(agent, attention)
    marker: dict = {
        "path": spill_path,
        "full_chars": full_chars,
        "truncated": True,
    }
    if spill_path:
        comment = NOTIFICATION_ATTENTION_OVERFLOW_COMMENT.format(path=spill_path)
    else:
        marker["spill_failed"] = True
        comment = NOTIFICATION_ATTENTION_OVERFLOW_NO_SPILL_COMMENT

    compacted: dict = attention
    for budget in NOTIFICATION_ATTENTION_COMPACT_BUDGETS:
        node, _changed = _compact_attention_node(attention, budget)
        if not isinstance(node, dict):
            compacted = {"data": node}
        else:
            compacted = node
        # Kernel-owned marker: overwrite any producer key of the same name.
        # The top-level comment is a short recovery hint, so it is stamped on
        # every compacted budget (the payload is rebuilt from the original at
        # each budget, and a budget that eventually fits may not be the widest).
        compacted[NOTIFICATION_ATTENTION_OVERFLOW_KEY] = dict(marker)
        compacted["comment"] = comment
        if _notification_attention_envelope_chars(compacted) <= max_chars:
            return compacted
    return _drop_notification_attention_records(attention, marker, comment, max_chars)


def build_notification_persistent_payload(agent, notification_payload: dict) -> dict | None:
    persistent: dict = {}

    email_payload = _build_email_notification_persistent_payload(
        agent, notification_payload
    )
    if email_payload is not None:
        persistent[NOTIFICATION_PERSISTENT_EMAIL_CHANNEL] = email_payload

    for lane in _IM_PERSISTENT_LANES:
        lane_payload = _build_im_notification_persistent_payload(
            agent, notification_payload, lane
        )
        if lane_payload is not None:
            persistent.setdefault(NOTIFICATION_PERSISTENT_MCP_KEY, {})[
                lane.channel
            ] = lane_payload

    if not persistent:
        return None
    # Single chokepoint for the model-visible size cap: both the ACTIVE
    # (attach_active_notifications) and IDLE (_inject_notification_pair) paths
    # build their block here.
    return {NOTIFICATION_PERSISTENT_KEY: _cap_notification_persistent(agent, persistent)}


def _record_im_persistent_delivery(
    agent,
    lane_payload: dict,
    lane: _ImPersistentLane,
    *,
    tool_call_id: str | None,
) -> None:
    """Record one IM lane's delivered message ids and previous-block hook.

    Snapshot lanes (``delivered_ids_attr`` / ``last_tool_id_attr`` is ``None``)
    keep no in-memory delivery state and are skipped.
    """
    if lane.delivered_ids_attr is None or lane.last_tool_id_attr is None:
        return
    messages = lane_payload.get("messages")
    if not isinstance(messages, list):
        return

    existing = getattr(agent, lane.delivered_ids_attr, [])
    if not isinstance(existing, list):
        existing = list(existing) if isinstance(existing, (tuple, set)) else []
    seen = set(existing)
    for message in messages:
        if not isinstance(message, dict):
            continue
        # Track delivery by per-update event identity when present (falls
        # back to the compound id) so a later event that shares a compound id
        # (repeated callback) is still delivered as new.
        msg_id = _im_message_identity(message)
        if isinstance(msg_id, str) and msg_id and msg_id not in seen:
            existing.append(msg_id)
            seen.add(msg_id)
    if len(existing) > lane.seen_limit:
        existing = existing[-lane.seen_limit:]
    try:
        setattr(agent, lane.delivered_ids_attr, existing)
        if tool_call_id:
            setattr(agent, lane.last_tool_id_attr, tool_call_id)
    except Exception:
        pass


def record_notification_persistent_delivery(
    agent,
    notification_persistent_payload: dict | None,
    *,
    tool_call_id: str | None,
) -> None:
    """Record persistent notification context delivered to provider context."""
    if not notification_persistent_payload:
        return
    persistent = notification_persistent_payload.get(NOTIFICATION_PERSISTENT_KEY)
    if not isinstance(persistent, dict):
        return

    mcp = persistent.get(NOTIFICATION_PERSISTENT_MCP_KEY)
    if not isinstance(mcp, dict):
        return
    for lane in _IM_PERSISTENT_LANES:
        lane_payload = mcp.get(lane.channel)
        if isinstance(lane_payload, dict):
            _record_im_persistent_delivery(
                agent, lane_payload, lane, tool_call_id=tool_call_id
            )


def _im_notification_message_ids(
    notification_payload: dict, source_key: str
) -> list[str]:
    """Return stable IM event IDs for the transient high-attention hook."""
    message_ids: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            message_ids.append(value)

    for event in _im_persistent_events_from_notifications(
        notification_payload, source_key
    ):
        if isinstance(event, dict):
            add(event.get("message_ref"))

    notifications = notification_payload.get(NOTIFICATIONS_KEY)
    channel = notifications.get(source_key) if isinstance(notifications, dict) else None
    data = channel.get("data") if isinstance(channel, dict) else None
    if not isinstance(data, dict):
        return message_ids

    # Fallback for older/partial payloads that have structured messages but no
    # event hook.  Keep only IDs; content and routing details stay persistent.
    for preview in _im_preview_list(notification_payload, source_key):
        if isinstance(preview, dict):
            add(preview.get("message_ref"))
            latest = preview.get("latest_incoming")
            if isinstance(latest, dict):
                add(latest.get("id"))

    return message_ids


def _sanitize_im_notification_after_persistent(
    notification_payload: dict, lane: _ImPersistentLane
) -> None:
    """Reduce one IM lane's ephemeral block to a minimal event identity hook.

    Message text, structured context, routing hooks, sender/subject, platform,
    counts, and summaries live under the lane's
    ``_meta.agent_meta.notifications.persistent.mcp.<channel>`` path.  The transient
    ``_meta.agent_meta.notifications.attention.mcp.<channel>`` block remains only a short
    high-attention/progressive-disclosure hook that names the producer event IDs
    requiring explicit handling through the producer tool.

    No-op when there is no notification for this lane. Safe to call
    unconditionally.
    """
    notifications = notification_payload.get(NOTIFICATIONS_KEY)
    if not isinstance(notifications, dict):
        return
    channel = notifications.get(lane.source_key)
    if not isinstance(channel, dict):
        return

    minimal_data: dict = {
        "message_ids": _im_notification_message_ids(
            notification_payload, lane.source_key
        )
    }

    # Preserve generic notification scaffolding (icon / priority / published_at)
    # but replace all channel content/summary fields with the standard LICC
    # transient hook: event identity in data, content/context in persistent.
    channel["header"] = f"{lane.display_name} event"
    channel["data"] = minimal_data
    channel["instructions"] = (
        f"High-attention {lane.display_name} hook: use notification_persistent "
        "for content/context; when handled, dismiss this notification."
    )


def sanitize_telegram_notification_after_persistent(notification_payload: dict) -> None:
    """Reduce Telegram's ephemeral lane to a minimal event identity hook."""
    _sanitize_im_notification_after_persistent(
        notification_payload, _TELEGRAM_PERSISTENT_LANE
    )


def sanitize_wechat_notification_after_persistent(notification_payload: dict) -> None:
    """Reduce WeChat's ephemeral lane to a minimal event identity hook."""
    _sanitize_im_notification_after_persistent(
        notification_payload, _WECHAT_PERSISTENT_LANE
    )


def sanitize_feishu_notification_after_persistent(notification_payload: dict) -> None:
    """Reduce Feishu's ephemeral lane to a minimal event identity hook."""
    _sanitize_im_notification_after_persistent(
        notification_payload, _FEISHU_PERSISTENT_LANE
    )


def sanitize_whatsapp_notification_after_persistent(notification_payload: dict) -> None:
    """Reduce WhatsApp's ephemeral lane to a minimal event identity hook."""
    _sanitize_im_notification_after_persistent(
        notification_payload, _WHATSAPP_PERSISTENT_LANE
    )


def _result_tool_call_id(result) -> str | None:
    meta = getattr(result, "metadata", None)
    if not isinstance(meta, dict):
        meta = result.get("_meta") if isinstance(result, dict) else None
    if not isinstance(meta, dict):
        return None
    tool_meta = meta.get(TOOL_META_KEY)
    if not isinstance(tool_meta, dict):
        return None
    call_id = tool_meta.get("id")
    return call_id if isinstance(call_id, str) and call_id else None


def build_synthetic_tool_meta(
    call_id: str,
    *,
    char_count: int = 0,
    elapsed_ms: int = 0,
) -> dict:
    """Return a minimal synthetic ``tool_meta`` block for the IDLE/ASLEEP pair.

    The synthesized ``notification(action="check")`` pair has no real tool
    execution, so :class:`ToolExecutor._attach_tool_block` never stamps a
    ``_meta.tool_meta`` block on it.  The ``/notification`` history view still
    wants a ``tool_meta`` block to render, so this builds a parallel one carrying
    the same identity fields a real ``tool_meta`` has (id/timestamp/char_count/
    elapsed_ms) plus a ``synthetic: True`` marker that distinguishes it from a
    real tool result's permanent block.
    """
    return {
        "id": call_id or "<unknown>",
        "timestamp": now_iso_plain(),
        "char_count": int(char_count),
        "elapsed_ms": int(elapsed_ms),
        "synthetic": True,
    }


def build_synthetic_meta_envelope(
    agent,
    notification_payload: dict,
    *,
    call_id: str,
) -> dict:
    """Assemble the canonical two-axis sidecar for a synthesized notification pair.

    Produces the same ``_meta`` envelope an ACTIVE tool result persists:

      * ``tool_meta``            — synthetic identity (see
        :func:`build_synthetic_tool_meta`)
      * ``agent_meta``           — current ``build_meta`` snapshot
      * ``guidance``             — lightweight ref to the resident
        ``meta_guidance`` system-prompt section (see
        :func:`build_meta_guidance_ref`)
      * ``notifications`` +
        ``notification_guidance``— from ``notification_payload`` (the dict
        returned by :func:`build_notification_payload`)

    Used for both the live synthetic result and its durable notification log.
    """
    context = None
    try:
        agent_meta = build_meta(agent)
        token_usage = agent_meta.pop(TOOL_META_TOKEN_USAGE_PENDING_KEY, None)
        context = agent_meta.pop(TOOL_META_CONTEXT_PENDING_KEY, None)
        agent_meta.pop(TOOL_META_CONTEXT_EVENT_PENDING_KEY, None)
    except (AttributeError, TypeError):
        agent_meta = {}
        token_usage = None

    tool_meta = build_synthetic_tool_meta(call_id)
    raw_state = agent_meta.get("agent_state") if isinstance(agent_meta, dict) else None
    state = dict(raw_state) if isinstance(raw_state, dict) else dict(agent_meta)
    if isinstance(agent_meta, dict):
        state.update({k: v for k, v in agent_meta.items() if k != "agent_state"})
    if isinstance(token_usage, dict) and token_usage:
        state[TOOL_META_TOKEN_USAGE_KEY] = token_usage
    if isinstance(context, dict) and context:
        state[TOOL_META_CONTEXT_KEY] = context
    envelope: dict = {
        TOOL_META_KEY: tool_meta,
        AGENT_META_KEY: {
            "instruction": AGENT_META_INSTRUCTION,
            "agent_state": state,
            "notifications": {
                "attention": _cap_notification_attention(
                    agent, notification_payload.get(NOTIFICATIONS_KEY, {})
                ),
                "persistent": notification_payload.get(NOTIFICATION_PERSISTENT_KEY, {}),
            },
            "guidance": {
                "persistent": build_meta_guidance_ref(),
                "transient": notification_payload.get(NOTIFICATION_GUIDANCE_KEY, {}),
            },
        },
    }
    persistent = notification_payload.get(NOTIFICATION_PERSISTENT_KEY, {})
    if isinstance(persistent, dict) and persistent:
        envelope[AGENT_META_KEY]["notifications"]["persistent"] = persistent
    return envelope


def _collect_active_notifications(agent):
    """Return ``(payload, versions)`` for the current active notifications.

    ``versions`` is the ``CoherentAttentionRead`` the payload was built from, so
    the caller can commit the fingerprint **of the bytes it is about to
    deliver** instead of re-reading the store afterwards. That distinction is
    load-bearing: ``dismiss_channel`` uses the committed raw fingerprint as its
    compare-and-swap token, so a version taken from a later independent read
    could describe a payload published *after* delivery — letting a non-forced
    dismiss clear bytes the model never saw.

    ``payload`` is ``None`` when there are no active channels (or anything goes
    wrong); callers treat ``None`` as "do not stamp". ``versions`` is ``None``
    only when the read itself failed, which leaves the fingerprint uncommitted
    for a later retry rather than committing an unverified version.
    """
    try:
        from .notifications import (
            coherent_attention_read,
            is_channel_allowed,
            sync_hook_registry,
            _workdir_key,
        )

        # Seed the module-level hook-channel mirror so registered external-hook
        # channels are collected here exactly as in the main sync path.
        sync_hook_registry(agent)
        workdir = _workdir_key(agent)

        observed = coherent_attention_read(
            agent._notification_store,
            lambda ch: is_channel_allowed(ch, workdir=workdir),
            workdir,
        )
        # A moving directory observation has no authoritative payload/version
        # pair. Leave any live holder and fingerprints intact for a later retry.
        if not observed.stable:
            return None, None
        if not observed.payloads:
            return None, observed
        return build_notification_payload(observed.payloads), observed
    except Exception:
        return None, None


def _final_tool_result_block(tool_results: list):
    """Return the designated final ToolResultBlock, independent of content type."""
    for block in reversed(tool_results):
        if _is_tool_result_block(block):
            return block
    return None


def skeletonize_notification_holder(agent) -> None:
    """Release the live notification holder without mutating its history.

    The live holder (``agent._notification_live_holder``) is a dict that is
    shared by reference with a historical ``ToolResultBlock.content`` already
    appended to canonical ``ChatInterface`` entries — possibly already sent to
    a provider. Both normal tool-result holders and synthesized pair holders
    are simply RELEASED from live tracking here: this function never mutates
    the dict's keys. Notification payloads are timely transient state (Jason
    #4307): canonical history is never retroactively stripped or rewritten
    when the payload moves or disappears; only the newest emitted holder is
    current. Model-facing full-history serialization preserves every holder's
    content unchanged, synthesized or not (see
    ``lingtai.llm.interface_converters``).

    After this call ``agent._notification_live_holder`` is ``None``.
    Called by:
    * The IDLE/ASLEEP inject path before stamping the new synthesized pair.
    * The ACTIVE path in ``attach_active_notifications`` when moving payload
      to a newer normal tool result (via ``prior_holder`` arg).
    * The notifications-cleared path so no holder reference lingers.
    """
    agent._notification_live_holder = None


# Keep the old name as an alias so external callers (if any) don't break.
# Internal code should prefer skeletonize_notification_holder.
def clear_active_notification_holder(agent) -> None:
    """Legacy alias for :func:`skeletonize_notification_holder`.

    Maintained for backward compatibility.  New code should call
    ``skeletonize_notification_holder`` directly.
    """
    skeletonize_notification_holder(agent)


def sanitize_email_notification_after_persistent(notification_payload: dict) -> None:
    notifications = notification_payload.get(NOTIFICATIONS_KEY)
    if not isinstance(notifications, dict):
        return
    email = notifications.get("email")
    if not isinstance(email, dict):
        return
    email_ids = _email_notification_email_ids(notification_payload)
    sanitized = {
        key: value
        for key, value in email.items()
        if key not in {"data", "header", "instructions"}
    }
    sanitized["header"] = "Email event"
    sanitized["data"] = {"email_ids": email_ids}
    sanitized["instructions"] = (
        "High-attention email hook: full unread content lives in "
        "notification_persistent.email. Prefer email.dismiss after handling; "
        "use email.read/reply for source-of-truth mailbox actions. When "
        "handled through the email tool, the producer mirror updates or "
        "clears this notification."
    )
    notifications["email"] = sanitized


def notification_payload_signature(payload: Mapping[str, Any] | None) -> str:
    """Return a stable signature of the *material* notification payload.

    The signature describes the material notification payload for delivery
    diagnostics and persistent-message bookkeeping. It does not gate copying
    the current payload onto the newest final agent_meta carrier.

    The whole ``build_notification_payload`` output is signed — the per-channel
    ``notifications`` payloads *and* the ``notification_guidance`` (whose
    ``sources`` list changes when a channel appears or disappears).  A channel
    coming or going is a material change worth re-surfacing, so signing the full
    payload is the least-surprising definition.  Unlike ``agent_meta`` there is
    no volatile per-batch bookkeeping to exclude: the payload is channel-owned
    current state, so every field is material.
    """
    try:
        return _json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(sorted((payload or {}).items()))


def _is_notification_check_placeholder(content) -> bool:
    """Return True when ``content`` is a voluntary ``notification(action=check)``
    placeholder result.

    The ``notification`` intrinsic's ``check`` action returns a dict carrying
    ``_notification_placeholder: True`` (see
    ``tools/notification/__init__._check``).  A deliberate check is a read
    request: its result must receive the current notification payload even when
    the payload is materially unchanged, so the ordinary final-carrier path and
    deliberate read both receive the current payload. The IDLE/ASLEEP synthesized pair
    also carries this key but is built by ``_inject_notification_pair`` on its
    own fingerprint-gated path and never reaches here.
    """
    return isinstance(content, dict) and content.get("_notification_placeholder") is True


def _commit_notification_fp(agent, delivered=None) -> None:
    """Commit the fingerprint of the delivered notification state.

    ``delivered`` is the ``CoherentAttentionRead`` the payload just stamped onto
    the wire was built from. It MUST be supplied by any caller that actually
    delivered bytes to the model: committing a version read *after* delivery is
    the B1 TOCTOU. Between the delivery snapshot and a later fingerprint call a
    producer can publish ``P2``; the model received ``P1``, but the committed
    raw version would describe ``P2``, and ``dismiss_channel`` — which uses that
    value as its non-forced compare-and-swap token — would happily clear ``P2``
    without it ever having been seen.

    Best-effort: a fingerprint failure must never break the caller. Committing
    ``_notification_fp`` is the bridge that stops the IDLE-path synthesized pair
    from re-delivering state already represented by a tool-result holder — so
    even an unchanged / equivalently-rewritten payload commits it, preventing a
    forever-retry against the IDLE sync path.

    ``_notification_fp`` is the *masked* attention fingerprint (the sync path
    compares against it, so committing a raw hash for a below-threshold daemon
    channel would read as a change on the next tick and wake the agent the mask
    exists to keep quiet). ``_notification_raw_fp`` is its byte-exact companion,
    the value a non-forced dismiss compares against — never the masked token,
    which ``compare_update_channel`` could never match.
    """
    try:
        if delivered is None:
            from .notifications import (
                coherent_attention_read,
                is_channel_allowed,
                _workdir_key,
            )

            workdir = _workdir_key(agent)
            delivered = coherent_attention_read(
                agent._notification_store,
                lambda ch: is_channel_allowed(ch, workdir=workdir),
                workdir,
            )
        if not delivered.stable:
            return
        agent._notification_fp = delivered.masked_fp
        agent._notification_raw_fp = delivered.raw_fp
    except Exception:
        pass


def attach_active_notifications(
    agent,
    tool_results: list,
    *,
    prior_holder: dict | None = None,
) -> dict | None:
    """Attach the current notification payload to the final agent_meta carrier.

    The current channel payload is merged into the newest final
    ``ToolResultBlock`` on every eligible batch, including when its material
    content is unchanged. ``agent._notification_payload_signature`` remains for
    delivery diagnostics and persistent-message bookkeeping; it is not an
    attachment gate. Older holders remain historical traces.

    Contract:
        * When there are no active notifications, no stamping happens,
          ``_notification_fp`` is left untouched, ``prior_holder`` (if any) is
          released (a synthesized pair is skeletonized; a normal tool result
          RETAINS its payload as a historical trace),
          ``_notification_payload_signature`` is reset to ``None``
          (so a later reappearance of the same payload attaches afresh as the
          first active payload), and ``None`` is returned.
        * When active notifications exist but this batch has no final
          ``ToolResultBlock`` to receive them, the prior holder is kept intact,
          ``_notification_fp`` is left uncommitted, and ``prior_holder`` is
          returned — the state can still be delivered later.
        * The current payload is copied for both unchanged and changed
          signatures, including a deliberate ``notification(action="check")``
          read. The prior holder is released (a
          synthesized pair is skeletonized; a normal tool result RETAINS its old
          payload as a historical trace — timely transient semantics, Jason
          #4307), the same ``notifications`` + ``notification_guidance`` payload
          shape used by the synthesized notification pair is stamped under
          ``_meta`` on the latest final result, the fingerprint is
          committed, the new signature is recorded, and that dict is returned as
          the new holder.  Only the newest emitted payload is current;
          model-facing full-history serialization preserves every
          normal-result holder's content and does not strip ``notifications``
          or ``notification_guidance`` keys (see
          ``lingtai.llm.interface_converters``).

    ``post-molt`` is intentionally not special-cased here.  The dangerous race
    is narrower: the ``context.molt`` tool call writes ``post-molt.json`` before
    returning, so only that same molt-result batch must skip active stamping.
    Later ACTIVE batches may consume the post-molt notification normally; if no
    later ACTIVE batch happens, the IDLE/ASLEEP sync path wakes the agent.

    ``tool_results`` is the list of ToolResultBlock objects returned from
    ToolExecutor; their ``.content`` is shared by reference with the canonical
    ChatInterface entries that the adapters append, so mutating the dict here
    propagates to history without a separate write.

    Active-state delivery only: the IDLE-path synthesized notification pair is
    built by ``_inject_notification_pair`` directly, but both paths call
    ``build_notification_payload`` so the live notification payload shape stays
    identical. Committing ``_notification_fp`` here is the bridge that prevents
    the same notification state from being delivered twice (once via tool-result
    meta, again via the synthesized pair).
    """
    payload, delivered_versions = _collect_active_notifications(agent)
    target = _final_tool_result_block(tool_results)
    # A failed or unstable store read is not an authoritative empty state.
    # Preserve the existing live holder and leave fingerprints uncommitted.
    if delivered_versions is None:
        return prior_holder
    if not payload:
        # Explicitly clear the current axis on the newest final carrier. Older
        # blocks remain untouched historical traces.
        if target is not None:
            current = target.metadata.get(AGENT_META_KEY)
            if not isinstance(current, dict):
                current = {"instruction": AGENT_META_INSTRUCTION, "agent_state": {}, "guidance": {}}
            current["instruction"] = AGENT_META_INSTRUCTION
            current["notifications"] = {}
            current.setdefault("guidance", {})["transient"] = {}
            target.metadata[AGENT_META_KEY] = current
        if prior_holder is not None:
            agent._notification_live_holder = prior_holder
            skeletonize_notification_holder(agent)
        try:
            agent._notification_payload_signature = None
        except Exception:
            pass
        return None

    if target is None:
        # Active notifications exist, but this batch has no final
        # result to receive the moving payload. Keep the prior live holder
        # (if any) intact and leave _notification_fp uncommitted so the
        # state can still be delivered later via another tool result or
        # the IDLE synthesized-pair path.
        return prior_holder

    # Signature and placeholder status remain delivery/accounting inputs only;
    # the current payload is always copied onto the final carrier.
    signature = notification_payload_signature(payload)
    is_check_read = _is_notification_check_placeholder(getattr(target, "content", None))
    unchanged = signature == getattr(agent, "_notification_payload_signature", None)

    # ``signature`` and ``is_check_read`` remain delivery/accounting inputs only;
    # an unchanged active payload is still copied onto this final carrier.

    # Material change (or deliberate check read). Release the previous holder:
    # a synthesized pair is skeletonized; a normal tool result keeps its old
    # payload as a historical trace (only the newest emission is current).
    if prior_holder is not None:
        agent._notification_live_holder = prior_holder
        skeletonize_notification_holder(agent)

    # Nest the canonical notification payload under the result's agent_meta
    # sidecar. Handler content is never used as a transport holder.
    persistent_payload = build_notification_persistent_payload(agent, payload)
    # Move (not duplicate): curated durable IM fields are always stripped
    # from the model-visible ephemeral lane, even when every message id was
    # already delivered and no new persistent block is emitted this round.
    # `payload` is freshly materialized for this delivery cycle, so in-place
    # preview trimming cannot mutate producer-owned on-disk notification state.
    sanitize_telegram_notification_after_persistent(payload)
    sanitize_wechat_notification_after_persistent(payload)
    sanitize_feishu_notification_after_persistent(payload)
    sanitize_whatsapp_notification_after_persistent(payload)
    sanitize_email_notification_after_persistent(payload)
    agent_meta = target.metadata.setdefault(AGENT_META_KEY, {
        "instruction": AGENT_META_INSTRUCTION,
        "agent_state": {},
        "notifications": {},
        "guidance": {},
    })
    # Compose the two current axes. Runtime owns agent_state and persistent
    # guidance; notification attachment owns notifications and transient
    # guidance. Neither phase may replace the other phase's current subtree.
    agent_meta["instruction"] = AGENT_META_INSTRUCTION
    agent_meta.setdefault("notifications", {})["attention"] = _cap_notification_attention(
        agent, payload.get(NOTIFICATIONS_KEY, {})
    )
    agent_meta.setdefault("guidance", {})["transient"] = payload.get(NOTIFICATION_GUIDANCE_KEY, {})
    if persistent_payload:
        agent_meta.setdefault("notifications", {})["persistent"] = persistent_payload.get(
            NOTIFICATION_PERSISTENT_KEY, {}
        )
        if not unchanged or is_check_read:
            record_notification_persistent_delivery(
                agent,
                persistent_payload,
                tool_call_id=_result_tool_call_id(target),
            )
    # Register this dict as the new live holder.
    agent._notification_live_holder = target

    # Record the new signature so a subsequent unchanged batch is recognized.
    try:
        agent._notification_payload_signature = signature
    except Exception:
        pass

    # Commit the fingerprint so the IDLE-path `_sync_notifications` will
    # see fp == agent._notification_fp and skip the synthesized pair for
    # this same unchanged state. The version committed is the one the payload
    # above was read from, so the raw fingerprint a later non-forced dismiss
    # compares against describes exactly the bytes just delivered.
    _commit_notification_fp(agent, delivered_versions)

    return target



def render_meta(agent, meta: dict) -> str:
    """Render the meta dict as the line prepended to text input.

    Returns '' when the meta dict is empty — callers should treat '' as
    "no prefix" and skip concatenation.

    Composes the existing ``system.current_time`` template plus a context
    fragment via ``system.context_breakdown`` (or ``system.context_unknown``
    when the session has not yet computed its token decomposition).
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
    if "usage" not in ctx:
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
    """Deprecated compatibility no-op; runtime capture now belongs to ToolResultBlock."""
    # Runtime callers now capture metadata in ``ToolExecutor`` keyed by exact
    # tool-call id and place it on ``ToolResultBlock.metadata``. Keeping this
    # function as a no-op avoids reintroducing a model-visible transport when
    # an older extension still imports the symbol.
    return result


# ---------------------------------------------------------------------------
# agent_meta / guidance blocks — coherent current snapshot under _meta. A
# delivery signature may remain for diagnostics, but never gates the final
# whole snapshot carried by the latest ToolResultBlock.
# ---------------------------------------------------------------------------


def _strip_agent_pending(tool_results: list) -> None:
    """Clear runtime-only captures without touching handler content."""
    for block in tool_results:
        if _is_tool_result_block(block):
            block._agent_pending = None


# Legacy volatile-field set retained for diagnostic/compatibility signatures.
# It is not a carrier gate: the complete current snapshot is emitted on the
# final block whenever private capture exists.
_AGENT_META_VOLATILE_KEYS = frozenset({
    "elapsed_ms",
    "active_turn_tool_calls",
    TOOL_META_CURRENT_TIME_KEY,
})

# Within ``current_tool_result_chars`` the running ``total_chars`` grows by a
# little every batch as results accumulate, so it is volatile.  The material
# signals — which large results exist (``top_results``), how many exceed the
# hint threshold (``over_threshold_count``), and the ``threshold`` itself — are
# kept in the signature so a genuinely new large result re-surfaces agent_meta.
_TOOL_RESULT_CHARS_VOLATILE_KEYS = frozenset({"total_chars"})


def agent_meta_signature(agent_meta: Mapping[str, Any]) -> str:
    """Return a stable signature of the *material* agent_meta content.

    The signature is retained for diagnostics and compatibility only.
    ``_meta.agent_meta`` is attached to the designated final result whenever
    private capture exists; this signature never suppresses that snapshot.

    Volatile bookkeeping is excluded from this diagnostic signature so callers
    can compare material state without churn.  Material signals still change
    the signature, but that comparison does not control current-state emission.
    Runtime/context/token/reconstruction state remains under
    ``agent_meta.agent_state``.
    """
    material: dict = {}
    for key, value in (agent_meta or {}).items():
        if key in _AGENT_META_VOLATILE_KEYS:
            continue
        if key == "current_tool_result_chars" and isinstance(value, Mapping):
            material[key] = {
                sub_key: sub_value
                for sub_key, sub_value in value.items()
                if sub_key not in _TOOL_RESULT_CHARS_VOLATILE_KEYS
            }
            continue
        material[key] = value
    try:
        return _json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(sorted(material.items()))


def _collect_taskcard_payload(agent) -> dict | None:
    """Read the agent-local resident Task Card artifact.

    Returns ``None`` when the card is absent or not ``active``. Returns a dict
    with ``status`` and ``body`` when an active card exists and its body fits
    within ``TASKCARD_MAX_CHARS``. Returns a dict with ``status``/``refused``
    (never the body) when the body exceeds the cap — the oversize card is
    refused, not truncated or injected.
    """
    try:
        workdir = getattr(agent, "_working_dir", None)
        if workdir is None:
            return None
        taskcard_dir = Path(workdir) / "taskcard"
        status_path = taskcard_dir / "status"
        body_path = taskcard_dir / "taskcard.md"
        if not status_path.is_file() or not body_path.is_file():
            return None
        status = status_path.read_text(encoding="utf-8").strip()
        if status != "active":
            return None
        body = body_path.read_text(encoding="utf-8")
        if not body.strip():
            return None
        if len(body) > TASKCARD_MAX_CHARS:
            return {
                "status": "refused",
                "hint": TASKCARD_REFUSED_HINT.format(max=TASKCARD_MAX_CHARS),
            }
        return {"status": "active", "body": body}
    except (OSError, UnicodeDecodeError) as exc:
        # A card that exists but cannot be read is NOT "no card": reporting
        # absence would instruct the agent to create a duplicate.
        agent_log = getattr(agent, "_log", None)
        if callable(agent_log):
            try:
                agent_log("taskcard.unreadable", error=type(exc).__name__)
            except Exception:
                pass
        return {"status": "unreadable", "hint": TASKCARD_UNREADABLE_HINT}
    except Exception:
        return None


def taskcard_signature(payload: Mapping[str, Any] | None) -> str:
    """Return a stable signature of the *material* resident Task Card."""
    try:
        return _json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(sorted((payload or {}).items()))


def attach_active_taskcard(
    agent,
    tool_results: list,
    *,
    prior_holder: dict | None = None,
) -> dict | None:
    """Attach the current resident Task Card to the final ``agent_meta`` carrier.

    Change-gated projection of ``<workdir>/taskcard/taskcard.md`` into
    ``_meta.agent_meta.taskcard`` so the human and the agent always see the same
    card without re-injecting identical bytes every turn.

    Contract:
        * When there is no active card, a generic hint is attached ONCE when
          the state transitions from a prior card (or from nothing):
          ``{taskcard: {present: False, hint: TASKCARD_ABSENT_HINT}}``.
        * When an active card exists and its body is unchanged since the last
          emission, nothing is attached and ``prior_holder`` is returned
          unchanged — the prior card stays the current holder.
        * When the card body changes, the new payload is attached under
          ``_meta.agent_meta.taskcard`` on the designated final result, the
          fingerprint is committed, and the new holder is returned. Older
          holders remain historical traces.
        * A body longer than ``TASKCARD_MAX_CHARS`` is refused: the payload
          carries ``status: refused`` and a hint, never the oversize body.

    ``tool_results`` is the list of ToolResultBlock objects returned from
    ToolExecutor; their ``.content`` is shared by reference with the canonical
    ChatInterface entries, so mutating the dict here propagates to history
    without a separate write.
    """
    target = _final_tool_result_block(tool_results)
    if target is None:
        return prior_holder
    payload = _collect_taskcard_payload(agent)

    def _carrier() -> dict:
        return target.metadata.setdefault(AGENT_META_KEY, {
            "instruction": AGENT_META_INSTRUCTION,
            "agent_state": {},
            "guidance": {},
        })

    if payload is None:
        signature = taskcard_signature({"present": False})
        if signature == getattr(agent, "_taskcard_signature", None):
            return prior_holder
        _carrier()[TASKCARD_KEY] = {"present": False, "hint": TASKCARD_ABSENT_HINT}
        try:
            agent._taskcard_signature = signature
        except Exception:
            pass
        return target

    signature = taskcard_signature(payload)
    if signature == getattr(agent, "_taskcard_signature", None):
        return prior_holder
    _carrier()[TASKCARD_KEY] = {"present": True, **payload}
    try:
        agent._taskcard_signature = signature
    except Exception:
        pass
    return target


def attach_active_runtime(
    agent,
    tool_results: list,
    *,
    prior_holder: dict | None = None,
) -> dict | None:
    """Attach the complete current ``agent_meta`` snapshot to the final block.

    The complete current ``agent_meta`` is attached to the designated final
    ``ToolResultBlock`` whenever private capture exists, regardless of material
    change.  ``agent._agent_meta_signature`` may be updated for diagnostics or
    compatibility, but it never gates emission.  Older blocks retain their
    snapshots as historical traces.

    Mirrors :func:`attach_active_notifications`, but with the change gate:

      * Build the candidate ``agent_meta`` from the final block's private runtime
        capture:
        kernel runtime state, including token/context/reconstruction state, plus
        ``elapsed_ms`` + ``active_turn_tool_calls``
        + ``current_tool_result_chars`` + a slimmed dynamic ``adapter_comment``.
      * Compute the diagnostic signature and record it.  Always promote the
        complete ``agent_meta`` + the ``_meta.agent_meta.guidance`` ref onto
        the new target and return the new holder.  The prior holder RETAINS its
        snapshot as a historical trace: canonical history is not retroactively
        stripped and only the newest snapshot is current. Model-facing full-history
        serialization preserves every holder's content and does not strip
        ``agent_meta`` or ``guidance`` keys (see
        ``lingtai.llm.interface_converters``).
      * When the signature is **unchanged**, nothing is attached or moved and
        ``prior_holder`` is returned unchanged — its ``agent_meta`` stays put.
      * Private runtime captures are cleared from *all* results regardless of
        the change outcome.

    Volatile bookkeeping (``elapsed_ms``, ``active_turn_tool_calls``,
    ``current_time``, ``current_tool_result_chars.total_chars``) is excluded from
    the signature so it cannot force ``agent_meta`` onto every result; see
    :func:`agent_meta_signature`.

    ``active_turn_tool_calls`` is read from the agent's executor guard.
    ``elapsed_ms`` is part of the final block's private capture when supplied.

    No live runtime is produced (and the prior holder is returned unchanged) only
    when the batch has no final ``ToolResultBlock`` or the final block carries no
    private pending snapshot (e.g. a time-blind agent whose ``meta`` is empty).
    """
    target_block = _final_tool_result_block(tool_results)
    pending = None
    if target_block is not None:
        pending = getattr(target_block, "_agent_pending", None)
        target_block._agent_pending = None

    # Reconstruction is a one-shot capture, so it can be consumed while an
    # earlier result is being stamped in a multi-tool batch.  It is current
    # batch state, not an earlier result's snapshot: promote only this event to
    # the designated final carrier and do not copy any other earlier state.
    reconstruction_event = None
    for block in tool_results:
        if block is target_block or not _is_tool_result_block(block):
            continue
        candidate = getattr(block, "_agent_pending", None)
        if not isinstance(candidate, dict):
            continue
        state = candidate.get("agent_state")
        events = state.get("events") if isinstance(state, dict) else None
        event = events.get("reconstruction") if isinstance(events, dict) else None
        if isinstance(event, dict):
            reconstruction_event = _copy.deepcopy(event)
            break
    if reconstruction_event is None and isinstance(pending, dict):
        state = pending.get("agent_state")
        events = state.get("events") if isinstance(state, dict) else None
        event = events.get("reconstruction") if isinstance(events, dict) else None
        if isinstance(event, dict):
            reconstruction_event = _copy.deepcopy(event)
    if reconstruction_event is not None and isinstance(pending, dict):
        state = pending.setdefault("agent_state", {})
        if isinstance(state, dict):
            state.setdefault("events", {})["reconstruction"] = reconstruction_event

    # Clear scaffolding from every other result regardless of outcome.
    _strip_agent_pending(tool_results)

    if target_block is None or not isinstance(pending, dict) or not pending:
        # No live runtime this batch: leave any prior holder (and its historical
        # agent_meta) untouched.
        return prior_holder

    agent_state = pending.get("agent_state", {}) if isinstance(pending, dict) else {}
    agent_meta: dict = {"agent_state": dict(agent_state) if isinstance(agent_state, dict) else {}}
    agent_meta.pop(TOOL_META_TOKEN_USAGE_PENDING_KEY, None)
    # Defensive backstop: current_time belongs in agent_state. Hand-built tests
    # or future producers must not create a second top-level state axis.
    agent_meta.pop(TOOL_META_CURRENT_TIME_KEY, None)
    # Context/rebuild/molt transit keys belong in agent_state; keep them out of
    # the compatibility signature's top-level carrier.
    agent_meta.pop(TOOL_META_CONTEXT_PENDING_KEY, None)
    agent_meta.pop(TOOL_META_CONTEXT_EVENT_PENDING_KEY, None)
    calls = _active_turn_tool_calls(agent)
    if calls is not None:
        agent_meta["active_turn_tool_calls"] = calls
    agent_meta["current_tool_result_chars"] = current_tool_result_chars(
        agent, extra_results=tool_results
    )
    # The adapter_comment carries both dynamic per-turn scalars and static
    # rule-like prose plus a long cache ledger.  The static content is resident
    # in the ``meta_guidance`` system-prompt section, so the tail keeps only the
    # slim dynamic view plus a ref back to that section.
    comment = dynamic_adapter_comment(agent)
    if comment:
        agent_meta["adapter_comment"] = slim_adapter_comment_for_tail(comment)

    # The signature is diagnostic/compatibility state only. The final block
    # always receives the newest whole snapshot.
    signature = agent_meta_signature(agent_meta)
    # The signature remains useful for diagnostics/dedup compatibility, but it
    # is not a current-state gate: the newest whole snapshot must be present.
    existing_agent_meta = target_block.metadata.get(AGENT_META_KEY)
    existing_agent_meta = existing_agent_meta if isinstance(existing_agent_meta, dict) else {}
    target_block.metadata[AGENT_META_KEY] = {
        "instruction": AGENT_META_INSTRUCTION,
        "agent_state": agent_meta["agent_state"],
        "notifications": existing_agent_meta.get("notifications", {}),
        **({TASKCARD_KEY: existing_agent_meta[TASKCARD_KEY]}
           if TASKCARD_KEY in existing_agent_meta else {}),
        "guidance": {
            "persistent": build_meta_guidance_ref(),
            **({"transient": existing_agent_meta["guidance"]["transient"]}
               if isinstance(existing_agent_meta.get("guidance"), dict)
               and "transient" in existing_agent_meta["guidance"] else {}),
        },
    }
    # Keep runtime diagnostics alongside the state, never as a second wrapper.
    target_block.metadata[AGENT_META_KEY]["agent_state"].update(
        {k: v for k, v in agent_meta.items() if k != "agent_state"}
    )
    try:
        agent._agent_meta_signature = signature
    except Exception:
        pass
    return target_block


def attach_daemon_agent_meta(
    tool_results: list,
    *,
    agent_state: Mapping[str, Any] | None = None,
) -> object | None:
    """Attach a daemon's current ``agent_meta`` snapshot to its final result.

    Daemon sessions deliberately do not have the parent ``BaseAgent``'s
    notification/communication state, so they cannot use
    :func:`attach_active_runtime` directly.  This small projector nevertheless
    keeps the canonical envelope, instruction, and final-carrier/latest-snapshot
    semantics in one place.  It does not manufacture a guidance reference because
    daemon prompts do not contain the main agent's resident ``meta_guidance``
    section.  The supplied ``agent_state`` is daemon-local runtime/token/context
    state; if omitted, the
    private ``_agent_pending`` capture staged by ``ToolExecutor`` is promoted.
    Older result blocks remain historical and the newest final result is the
    only current ``agent_meta`` carrier.
    """
    target = _final_tool_result_block(tool_results)
    pending_state = None
    for block in tool_results:
        if not _is_tool_result_block(block):
            continue
        candidate = getattr(block, "_agent_pending", None)
        if block is target and isinstance(candidate, dict):
            raw_state = candidate.get("agent_state")
            if isinstance(raw_state, dict):
                pending_state = raw_state
        block._agent_pending = None
    if target is None:
        return None
    state = agent_state if isinstance(agent_state, Mapping) else pending_state
    if not isinstance(state, Mapping):
        return target
    target.metadata[AGENT_META_KEY] = {
        "instruction": AGENT_META_INSTRUCTION,
        "agent_state": _copy.deepcopy(dict(state)),
    }
    return target


def finalize_two_axis_sidecars(tool_results: list) -> None:
    """Move boundary metadata into the canonical two-axis ToolResultBlock sidecar.

    Handler content is deliberately left as the handler returned it.  This is
    called once after notification/runtime attachment and before results return
    to the model; old holders in history are never rewritten.
    """
    for block in tool_results:
        metadata = getattr(block, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        # The batch runtime hook consumes `_agent_pending` only from the
        # designated final carrier. Every other result must lose the private
        # capture without becoming an agent-meta carrier.
        metadata.pop("_agent_pending", None)
        agent = metadata.get(AGENT_META_KEY)
        if isinstance(agent, dict):
            agent["instruction"] = AGENT_META_INSTRUCTION
            agent_state = agent.get("agent_state", {})
            normalized_agent = {
                "instruction": agent["instruction"],
                "agent_state": agent_state,
            }
            daemon_local_state = (
                isinstance(agent_state, dict)
                and isinstance(agent_state.get("daemon"), dict)
            )
            if not daemon_local_state or "notifications" in agent:
                normalized_agent["notifications"] = agent.get("notifications", {})
            if not daemon_local_state or "guidance" in agent:
                normalized_agent["guidance"] = agent.get("guidance", {})
            if TASKCARD_KEY in agent:
                normalized_agent[TASKCARD_KEY] = agent.get(TASKCARD_KEY, {})
            metadata[AGENT_META_KEY] = normalized_agent
        # No private transport or obsolete root siblings can reach adapters.
        for key in list(metadata):
            if key not in {TOOL_META_KEY, AGENT_META_KEY}:
                metadata.pop(key, None)


def _active_turn_tool_calls(agent) -> int | None:
    """Best-effort read of the ACTIVE-turn tool-call counter from the guard.

    Returns ``None`` (counter omitted) if the agent has no executor/guard or
    the attribute is unavailable, so a missing counter never breaks stamping.
    """
    try:
        guard = getattr(getattr(agent, "_executor", None), "guard", None)
        total = getattr(guard, "total_calls", None)
        return int(total) if total is not None else None
    except Exception:
        return None


def _non_negative_int(value, *, default: int = 0) -> int:
    """Best-effort conversion for agent-facing token counters."""
    try:
        if isinstance(value, bool):
            raise TypeError
        ivalue = int(value)
    except Exception:
        return default
    return ivalue if ivalue >= 0 else default


def _fallback_context_window(agent) -> int:
    """Return a best-effort context window for the reconstruction event."""
    try:
        config_limit = int(getattr(getattr(agent, "_config", None), "context_limit", 0) or 0)
    except Exception:
        config_limit = 0
    if config_limit > 0:
        return config_limit
    try:
        session = getattr(agent, "_session", None)
        chat = getattr(session, "chat", None)
        if chat is None:
            chat = getattr(session, "_chat", None)
        window_fn = getattr(chat, "context_window", None)
        if callable(window_fn):
            window = _non_negative_int(window_fn(), default=-1)
            return window if window > 0 else -1
    except Exception:
        pass
    return -1
