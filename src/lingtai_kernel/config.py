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
    retry_timeout: float = 120.0
    cpr_timeout: float = 1200.0  # 20 minutes — max CPR before pronouncing dead
    thinking_budget: int | None = None
    data_dir: str | None = None  # for cache files (e.g., model context windows)
    soul_delay: float = 120.0  # seconds idle before soul whispers; large value (> stamina) = effectively off
    language: str = "en"  # agent language ("en", "zh"); controls all kernel-injected strings
    stamina: float = 3600.0  # agent stamina in seconds; set at birth, not changeable by the agent
    context_limit: int | None = None  # max context tokens; None = use model default
    molt_pressure: float = 0.8  # context usage fraction that triggers molt warnings (0.0–1.0)
    molt_warnings: int = 5  # number of warnings before auto-wipe
    molt_prompt: str = ""  # user-provided instruction for how to prepare for molt
    ensure_ascii: bool = False  # JSON output: False = readable unicode, True = \uXXXX escapes
