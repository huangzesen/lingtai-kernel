"""Web read capability — fetch and extract readable content from URLs.

Uses WebReadService if provided, otherwise auto-creates TrafilaturaWebReadService.

Usage:
    agent.add_capability("web_read")  # uses trafilatura (default)
    agent.add_capability("web_read", web_read_service=my_svc)  # uses custom service
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {"providers": ["zhipu"], "default": "builtin"}


def get_description(lang: str = "en") -> str:
    return t(lang, "web_read.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": t(lang, "web_read.url")},
            "output_format": {
                "type": "string",
                "enum": ["text", "markdown"],
                "description": t(lang, "web_read.output_format"),
            },
        },
        "required": ["url"],
    }



class WebReadManager:
    """Handles web_read tool calls."""

    def __init__(
        self,
        web_read_service: Any | None = None,
    ) -> None:
        self._service = web_read_service

    def handle(self, args: dict) -> dict:
        url = args.get("url")
        if not url:
            return {"status": "error", "message": "Missing required parameter: url"}

        output_format = args.get("output_format", "markdown")

        # Auto-create default service if none provided
        if self._service is None:
            from ..services.web_read import TrafilaturaWebReadService
            self._service = TrafilaturaWebReadService()

        try:
            result = self._service.read(url, output_format=output_format)
            return {"status": "ok", "url": url, "content": result.content, "title": result.title}
        except Exception as e:
            return {"status": "error", "message": str(e)}


def setup(agent: "BaseAgent", web_read_service: Any | None = None,
          provider: str | None = None, api_key: str | None = None,
          **kwargs: Any) -> WebReadManager:
    """Set up the web_read capability on an agent.

    If ``provider="zhipu"`` and ``api_key`` is given, uses Z.AI's reader API
    instead of the default trafilatura fallback.
    """
    if web_read_service is None and provider == "zhipu" and api_key:
        from ..services.web_read import ZhipuWebReadService
        from ._zhipu_mode import resolve_z_ai_mode
        web_read_service = ZhipuWebReadService(api_key=api_key, z_ai_mode=resolve_z_ai_mode(agent))
    lang = agent._config.language
    mgr = WebReadManager(web_read_service=web_read_service)
    agent.add_tool("web_read", schema=get_schema(lang), handler=mgr.handle, description=get_description(lang))
    return mgr
