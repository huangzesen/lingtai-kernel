"""AgentState — lifecycle state enum for StoAI agents."""

from __future__ import annotations

import enum


class AgentState(enum.Enum):
    """Lifecycle state of an agent.

    ACTIVE --(completed)--------> IDLE
    ACTIVE --(timeout/exception)-> ERROR
    IDLE   --(inbox message)----> ACTIVE
    ERROR  --(AED)--------------> ACTIVE  (session reset, fresh run loop)
    ERROR  --(AED timeout)------> DEAD    (shutdown)
    """

    ACTIVE = "active"
    IDLE = "idle"
    ERROR = "error"
    DEAD = "dead"
