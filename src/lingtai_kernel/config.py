"""Agent configuration — injected at construction, not read from files."""
from __future__ import annotations

from dataclasses import dataclass, field


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
    max_aed_attempts: int = 3    # max AED retry attempts in message loop
    max_rpm: int = 60  # API requests-per-minute cap for this agent's provider; 0 = no gating. Shared across all agents in the same process that use the same (provider, base_url) pair (adapter cache key).
    thinking_budget: int | None = None
    data_dir: str | None = None  # for cache files (e.g., model context windows)
    soul_delay: float = 300.0  # seconds idle before soul whispers; large value (> stamina) = effectively off
    language: str = "en"  # agent language ("en", "zh"); controls all kernel-injected strings
    stamina: float = 3600.0  # agent stamina in seconds; set at birth, not changeable by the agent
    time_awareness: bool = True  # experimental: False strips LLM-visible timestamps (perception nerf)
    timezone_awareness: bool = True  # when True, now_iso emits OS local time; when False, UTC
    context_limit: int | None = None  # max context tokens; None = use model default
    molt_pressure: float = 0.7  # context usage fraction that triggers molt warnings (0.0–1.0)
    molt_warnings: int = 5  # number of warnings before auto-wipe
    molt_hard_ceiling: float = 0.95  # unconditional auto-wipe threshold — if pressure ≥ this, force-wipe next turn regardless of warning count
    molt_prompt: str = ""  # user-provided instruction for how to prepare for molt
    ensure_ascii: bool = False  # JSON output: False = readable unicode, True = \uXXXX escapes
    insights_interval: int = 0  # turns between auto-insights; 0 = off
    consultation_interval: int = 40  # main-chat LLM calls between past-self consultation fires; 0 = off (wall-clock timer still runs independently)
    consultation_past_count: int = 2  # K random past-snapshot consultations per fire (M = 1 insights + K)
    snapshot_interval: float | None = None  # seconds between git snapshots; None = off
