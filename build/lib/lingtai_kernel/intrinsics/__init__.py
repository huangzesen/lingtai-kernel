"""Intrinsic tools available to all agents.

Each intrinsic module exposes:
- get_schema(lang) -> dict: JSON Schema for tool parameters
- get_description(lang) -> str: human-readable description
- handle(agent, args) -> dict: handler function
"""
from . import mail, system, eigen, soul

ALL_INTRINSICS = {
    "mail": {"module": mail},
    "system": {"module": system},
    "eigen": {"module": eigen},
    "soul": {"module": soul},
}
