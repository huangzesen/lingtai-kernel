"""AgentState — lifecycle state enum for 灵台 agents."""

from __future__ import annotations

import enum


class AgentState(enum.Enum):
    """Lifecycle state of an agent.

    ACTIVE --(completed)--------> IDLE
    ACTIVE --(timeout/exception)-> STUCK
    IDLE   --(inbox message)----> ACTIVE
    STUCK  --(AED)--------------> ACTIVE  (session reset, fresh run loop)
    STUCK  --(AED timeout)------> DORMANT (shutdown)
    ACTIVE/IDLE --(quell/shutdown)-> DORMANT
    DORMANT --(revive)-----------> IDLE    (reconstructed from working dir)
    """

    ACTIVE = "active"
    IDLE = "idle"
    STUCK = "stuck"
    DORMANT = "dormant"
