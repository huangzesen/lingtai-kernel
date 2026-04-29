"""Ephemeral capability sandbox for daemon emanations.

When an emanation runs on a different preset than the parent, the preset's
``manifest.capabilities`` block defines a *different tool surface*. Naively
calling ``setup_capability(parent_agent, ...)`` would mutate the parent's
own tool registry — bad. The sandbox is a lightweight stand-in that:

- Intercepts ``add_tool`` calls into local schemas/handlers dicts
- Forwards every other attribute read to the real parent agent
  (``_working_dir``, ``_config``, ``inbox``, ``_log``, services, etc.)

Capabilities don't know they're being instantiated in a sandbox — they
register tools as usual, and the daemon harvests the resulting
``schemas`` / ``handlers`` dicts to build the emanation's tool surface.

Lifetime is the emanation: the sandbox is discarded when the emanation
finishes. Capabilities holding state (e.g. a search service) get garbage
collected with the sandbox.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from lingtai_kernel.llm.base import FunctionSchema

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent


class _CapabilitySandbox:
    """Captures add_tool calls; forwards all other reads to the parent agent."""

    def __init__(self, parent_agent: "BaseAgent"):
        # Keep these as actual attributes so __getattr__ doesn't shadow them.
        # __getattr__ is only consulted for attributes NOT found on self, so
        # local slots take precedence — but set them explicitly to be safe.
        self.__dict__["_parent"] = parent_agent
        self.__dict__["schemas"] = {}    # name → FunctionSchema
        self.__dict__["handlers"] = {}   # name → callable

    def add_tool(
        self,
        name: str,
        *,
        schema: dict | None = None,
        handler: Callable[[dict], dict] | None = None,
        description: str = "",
        system_prompt: str = "",
    ) -> None:
        if handler is not None:
            self.handlers[name] = handler
        if schema is not None:
            self.schemas[name] = FunctionSchema(
                name=name,
                description=description,
                parameters=schema,
                system_prompt=system_prompt,
            )

    def __getattr__(self, name: str) -> Any:
        # Called only when normal attribute lookup fails.
        return getattr(self.__dict__["_parent"], name)
