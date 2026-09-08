"""Agent configuration, runtime constants, and environment-backed policy helpers."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


# Accepted manifest.llm.thinking values, mirroring the upstream Responses
# ``reasoning.effort`` payload values in ascending effort order. Explicit
# ``"none"`` is a real payload value (effort none), distinct from an *omitted*
# field — omitted stays the internal ``"default"`` sentinel and adapters that
# own a default map it to ``"xhigh"``.
THINKING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Codex-family providers that accept manifest.llm.thinking. ``codex-pool``
# reuses the Codex adapter (both dash/underscore spellings). This list stays
# Codex-only; the complete acceptance rule (Anthropic and every
# OpenAI-compatible block too) lives in ``llm_supports_thinking`` so validators
# share it.
THINKING_PROVIDERS = ("codex", "codex-pool", "codex_pool")

# Non-Codex providers whose adapter is thinking-capable on its own, even when
# the manifest omits ``api_compat``: the Anthropic adapter maps thinking to an
# extended-thinking budget, and the openai/deepseek factories always pin an
# OpenAI-compatible adapter, so their blocks carry an implicit
# ``api_compat="openai"``.
THINKING_NATIVE_PROVIDERS = ("anthropic", "openai", "deepseek", "claude-code", "claude_code")

# Providers that own their reasoning-effort contract in their own module: the
# accepted vocabulary is per model and per wire, and so is what an OMITTED
# value means. This is a coarse SCOPE name only — the kernel deliberately holds
# no level vocabulary, model list, alias table, or default for these routes,
# and cannot import them (see tests/test_kernel_isolation.py). The exact
# validation is applied by the lingtai-layer ingress (``lingtai/init_schema.py``
# and ``lingtai/agent.py``) against the provider's own module.
THINKING_OWNED_PROVIDERS = ("deepseek",)

# Usage adapters attach this explicit, provider-neutral semantic to
# ``UsageMetadata.extra``. The kernel defaults to ``subset`` when a legacy or
# custom adapter omits it, avoiding any provider-name/alias guessing.
THINKING_TOKENS_SEMANTICS_KEY = "thinking_tokens_semantics"
THINKING_TOKENS_SUBSET = "subset"
THINKING_TOKENS_SEPARATE = "separate"


def llm_supports_thinking(llm: dict) -> bool:
    """Return whether a manifest LLM block accepts explicit thinking effort.

    Every thinking-capable wire is accepted:

    * the Codex family (``THINKING_PROVIDERS``) — it owns its own wire/backend;
    * ``THINKING_NATIVE_PROVIDERS`` — ``anthropic`` (thinking budget),
      Claude Code's provider-local CLI effort route, plus the OpenAI-wire
      natives whose ``api_compat`` may be left implicit;
    * any OpenAI-compatible block (``api_compat == "openai"``) regardless of
      ``wire_api`` — Responses sends ``reasoning.effort`` and Chat Completions
      sends ``reasoning_effort``, so both wires carry the effort.

    Everything else (Gemini, MiniMax, a custom Gemini/Anthropic-compat block)
    is rejected so a knob the wire would silently drop fails loudly.
    """
    provider = str(llm.get("provider") or "").lower()
    if provider in THINKING_PROVIDERS or provider in THINKING_NATIVE_PROVIDERS:
        return True
    return str(llm.get("api_compat") or "").lower() == "openai"

# Molt context-pressure thresholds are kernel-fixed runtime constants — NOT
# agent-configurable. An agent must not be able to raise its own molt
# thresholds (or defeat them entirely) to avoid molting under pressure, so the
# stage boundaries are owned by the kernel. Legacy ``init.json`` /
# resolved-manifest ``molt_notice`` / ``molt_pressure`` / ``molt_urgency``
# fields are tolerated for backward compatibility (old agents still validate)
# but are ignored — they no longer override these values. See
# ``lingtai/agent.py`` (config reload) and ``lingtai/init_schema.py``
# (MANIFEST_LEGACY_IGNORED).
MOLT_NOTICE_THRESHOLD = 0.75  # legacy name; now the molt RECOVERY TARGET (see below)

# Sustained context-pressure / manual-rebuild / molt-warning constants
# (kernel-fixed).
#
# The warning surfaced in ``_meta.agent_meta.agent_state.context.molt`` is no
# longer an immediate trip-wire.  It is a *sustained-pressure*
# signal, while provider-context reconstruction is a separate, rarer event:
#
#   * CONTEXT_PRESSURE_HIGH_RATIO (0.85) — a fresh provider round whose context
#     usage is at/above this fraction counts as a "high" round.  The same
#     inclusive threshold (``usage >= 0.85``) also continuously stamps
#     ``_meta.agent_meta.agent_state.context.rebuild`` with permission to manually rebuild via
#     ``context(action='rebuild')``.  It does NOT force an
#     automatic provider-context rebuild — it is the proactive hint boundary.
#   * CONTEXT_PRESSURE_FORCED_REBUILD_RATIO (1.0) — the HARD boundary. Once
#     context usage reaches this inclusive threshold, the runtime forces a
#     provider-context rebuild / fresh replay on the next model request
#     REGARDLESS of whether pending summaries exist: if pending markers exist, they
#     are applied and marked done; ``summarize`` is the only historical
#     tool-result body replacement a rebuild applies — the fresh replay
#     otherwise preserves each historical timely-transient holder and does not
#     strip its agent_meta/guidance or notifications/notification_guidance keys
#     in shared model-facing serialization (only the LATEST holder per family
#     is current state; older holders are historical traces). A one-shot
#     unified warning is ALWAYS emitted after this forced rebuild.
#     (``CONTEXT_PRESSURE_RECONSTRUCTION_RATIO`` is a back-compat alias.)
#   * CONTEXT_PRESSURE_WARN_AFTER_ROUNDS (3) — the resident ``context.molt``
#     warning begins on the THIRD consecutive high round; earlier high rounds get
#     the manual-rebuild hint but not the stronger molt reminder.
#   * CONTEXT_PRESSURE_RECOVERY_TARGET (0.75) — if summarize/rebuild cannot bring
#     context below this fraction of the window, molt becomes the recommended
#     action.  This is the new meaning of the legacy 0.75 constant: a recovery
#     target, not an immediate trip-wire.
CONTEXT_PRESSURE_HIGH_RATIO = 0.85
CONTEXT_PRESSURE_FORCED_REBUILD_RATIO = 1.0
# Back-compat alias for the pre-1.0 name (was 0.95, "delayed reconstruction");
# the boundary is now the hard 1.0 forced rebuild.
CONTEXT_PRESSURE_RECONSTRUCTION_RATIO = CONTEXT_PRESSURE_FORCED_REBUILD_RATIO
CONTEXT_PRESSURE_WARN_AFTER_ROUNDS = 3
CONTEXT_PRESSURE_RECOVERY_TARGET = MOLT_NOTICE_THRESHOLD  # 0.75

MOLT_PRESSURE_THRESHOLD = MOLT_NOTICE_THRESHOLD  # legacy alias; not a separate stage
MOLT_URGENCY_THRESHOLD = MOLT_NOTICE_THRESHOLD  # legacy alias; not a separate stage
DEFAULT_SOUL_DELAY_SECONDS = 999999999.0

# Rendered system-prompt size pressure — distinct from the CONTEXT_PRESSURE_*
# family above (which measures system + tools + history against the window).
# This ratio gates a warning on the rendered system prompt ALONE against the
# effective context window. It is deliberately read at snapshot-render time so
# the main agent and daemon share live process-environment behavior.
DEFAULT_SYSTEM_PROMPT_PRESSURE_RATIO = 0.4
SYSTEM_PROMPT_PRESSURE_RATIO_ENV = "LINGTAI_SYSTEM_PROMPT_PRESSURE_RATIO"


def system_prompt_pressure_ratio() -> float:
    """Return the valid current environment ratio, or the default."""
    raw = os.environ.get(SYSTEM_PROMPT_PRESSURE_RATIO_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SYSTEM_PROMPT_PRESSURE_RATIO
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SYSTEM_PROMPT_PRESSURE_RATIO
    if not math.isfinite(value) or not 0 < value < 1:
        return DEFAULT_SYSTEM_PROMPT_PRESSURE_RATIO
    return value


# ---------------------------------------------------------------------------
# Resident ``## tools`` prose walkthrough — opt-in, DEFAULT OFF
# ---------------------------------------------------------------------------
#
# Every tool's full canonical-English prose used to be rendered TWICE into the
# model-facing context of one turn: once as the resident ``## tools`` section of
# the composed system prompt (``base_agent/tools.py``
# ``_refresh_tool_inventory_section``) and once as the tool-calling schema's
# top-level ``description``. For API providers the wire copy is the generic
# ``WIRE_TOOL_DESCRIPTION`` pointer, so only the section carried the prose; for
# the CLI-backed adapters (``claude_code``, ``kimi_code``) the full prose is
# serialised verbatim into the ``# AVAILABLE TOOLS`` block *next to* the very
# same text inside ``# AGENT SYSTEM INSTRUCTIONS`` — literal byte-identical
# duplication of ~1.1 KB per registered tool, every turn.
#
# The section is now OPT-IN and DEFAULT OFF. With it off, each tool's prose
# lives in exactly one place — the tool-calling schema description, which every
# adapter already sends (``wire_tool_description`` returns the full prose
# instead of the pointer sentence, so no provider loses guidance). Set
# ``LINGTAI_TOOL_PROSE_SECTION_ENABLED`` to a truthy value to restore the old
# two-copy behavior byte-for-byte, including the ``WIRE_TOOL_DESCRIPTION``
# pointer on API wires.
#
# Nested parameter/property descriptions are never affected either way.
TOOL_PROSE_SECTION_ENABLED_ENV = "LINGTAI_TOOL_PROSE_SECTION_ENABLED"
# Case-insensitive truthy set, matching the other kernel opt-in gates
# (``LINGTAI_RISKY_ACTION_GATE``, ``LINGTAI_SOUL_FLOW_ENABLED``). Anything else
# — including unset and "" — is off.
_TOOL_PROSE_SECTION_TRUTHY = frozenset({"1", "true", "yes", "on"})


def tool_prose_section_enabled() -> bool:
    """Return whether the resident ``## tools`` prose section is opted in.

    Read fresh from ``os.environ`` at every prompt rebuild and every provider
    payload build so a value flipped in an agent's ``env_file`` applies at the
    next refresh without a restart.
    """
    raw = os.environ.get(TOOL_PROSE_SECTION_ENABLED_ENV, "")
    return raw.strip().lower() in _TOOL_PROSE_SECTION_TRUTHY


# Hidden runtime housekeeping: an agent that remains IDLE for this long is moved
# to ASLEEP. This is deliberately kernel-fixed and not surfaced in init.json,
# prompts, status, or tool metadata.
IDLE_SLEEP_TIMEOUT_SECONDS = 86400.0

# Heartbeat cadence and shared cross-process liveness. _heartbeat_loop
# stamps .agent.heartbeat every HEARTBEAT_TICK_SECONDS; a reader considers the
# agent alive if the stamp is younger than HEARTBEAT_LIVENESS_SECONDS. The
# resolver runs when this module is imported, so every participating process
# must be started (or its config module reloaded) after an environment change.
# Ten ticks of default headroom tolerate delayed writer scheduling without
# flapping to "dead"; explicit positive finite overrides remain operator policy.
HEARTBEAT_TICK_SECONDS = 1.0
HEARTBEAT_LIVENESS_ENV_VAR = "LINGTAI_AGENT_ALIVE_THRESHOLD_SEC"
DEFAULT_HEARTBEAT_LIVENESS_SECONDS = 10.0


def _resolve_heartbeat_liveness_seconds() -> float:
    """Read the shared heartbeat threshold with a safe 10-second fallback."""
    raw = os.environ.get(HEARTBEAT_LIVENESS_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_HEARTBEAT_LIVENESS_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_LIVENESS_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_HEARTBEAT_LIVENESS_SECONDS
    return value


HEARTBEAT_LIVENESS_SECONDS = _resolve_heartbeat_liveness_seconds()
# Retention is report-only and must over-estimate liveness so it never
# classifies a live agent's files as stale; keep 2x extra margin. The default
# is therefore 20.0 seconds when heartbeat liveness uses its 10-second default.
RETENTION_LIVE_HEARTBEAT_SECONDS = 2 * HEARTBEAT_LIVENESS_SECONDS


@dataclass
class AgentConfig:
    """Configuration for a BaseAgent instance.

    The host app reads its own config files and passes resolved values here.
    No file-based config reading inside lingtai.
    """
    max_turns: int = 50
    provider: str | None = None  # None = use LLMService's provider
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    retry_timeout: float = 300.0  # LLM call watchdog (seconds). Bumped from 120s — modern thinking models (GLM-5.1, DeepSeek V4 thinking, Anthropic extended-thinking) routinely take 60–180s for high-context turns; 120s spuriously fired on slow-but-successful calls and triggered AED cascades. 300s catches truly-hung connections without false positives on normal responses.
    aed_timeout: float = 360.0   # max seconds in STUCK before ASLEEP
    max_aed_attempts: int = 3   # max AED retry attempts per inbox message turn
    max_rpm: int = 60  # API requests-per-minute cap for this agent's provider; 0 = no gating. Shared across all agents in the same process that use the same (provider, base_url) pair (adapter cache key).
    thinking_budget: int | None = None
    thinking: str = "high"  # reasoning/thinking tier passed to the main persistent LLM session
    data_dir: str | None = None  # for cache files (e.g., model context windows)
    soul_delay: float = DEFAULT_SOUL_DELAY_SECONDS  # seconds idle before soul whispers; large value = effectively off
    language: str = "en"  # legacy language field retained for compatibility; prompt.py no longer injects prose from it
    activeness: str | None = "balanced"  # legacy responsiveness posture field; prompt.py no longer injects text from it
    stamina: float = IDLE_SLEEP_TIMEOUT_SECONDS  # legacy ignored constructor field; hidden idle timeout uses the kernel constant above
    time_awareness: bool = True  # experimental: False strips LLM-visible timestamps (perception nerf)
    timezone_awareness: bool = True  # when True, now_iso emits OS local time; when False, UTC
    context_limit: int | None = None  # max context tokens; None = use model default
    # Legacy molt-threshold fields, retained ONLY for backward compatibility
    # (old AgentConfig constructions / serialized state still set them). They are
    # NOT the active warning threshold and are no longer read by the warning
    # path: the sustained-pressure warning (meta_block.build_molt_context) is
    # driven by the SessionManager streak and the kernel constants
    # CONTEXT_PRESSURE_* (see top of this module), not by these fields. Legacy
    # init.json molt_notice/molt_pressure/molt_urgency values remain ignored.
    # The 0.75 default here now corresponds to the molt RECOVERY TARGET
    # (CONTEXT_PRESSURE_RECOVERY_TARGET), not an immediate trip-wire.
    molt_notice: float = MOLT_NOTICE_THRESHOLD  # legacy/compat only; == recovery target (0.75), not a trip-wire
    molt_pressure: float = MOLT_PRESSURE_THRESHOLD  # legacy alias; unused by the warning path
    molt_urgency: float = MOLT_URGENCY_THRESHOLD  # legacy alias; unused by the warning path
    ensure_ascii: bool = False  # JSON output: False = readable unicode, True = \uXXXX escapes
    insights_interval: int = 0  # turns between auto-insights; 0 = off
    consultation_past_count: int = 0  # K random past-snapshot consultations per fire; default 0 = current-context soul flow only
    soul_voice: str = "inner"  # consultation prompt profile — "inner" (terse, "you are the soul, speak as inner voice"), "observer" (structured stepped-back hook framing), or "custom" (use soul_voice_prompt). One unified prompt per profile; the per-fire cue text differentiates insights (current diary) vs past (future-self diary).
    soul_voice_prompt: str = ""  # custom voice prompt — only used when soul_voice == "custom". Set/cleared by the agent via soul(action="voice", set="custom", prompt="..."). Length-capped at SOUL_VOICE_PROMPT_MAX in soul.py.
    snapshot_interval: float | None = None  # seconds between git snapshots; None = off

    def __post_init__(self):
        # Clamp max_aed_attempts to at least 1.  A value of 0 or negative
        # causes the AED retry loop in turn.py to spin forever: aed_attempts
        # starts at 1 (incremented before the equality check) and never equals
        # 0 or a negative max.  See issue #654.
        if self.max_aed_attempts < 1:
            self.max_aed_attempts = 1
