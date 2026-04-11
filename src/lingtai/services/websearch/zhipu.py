"""Zhipu web search — uses Z.AI's web_search REST API."""
from __future__ import annotations

import os

from lingtai_kernel.logging import get_logger

from . import SearchResult, SearchService

logger = get_logger()


class ZhipuSearchService(SearchService):
    """Web search via Z.AI's ``/web_search`` API.

    Uses the ``search-prime`` engine. No MCP subprocess needed — direct
    HTTP calls to ``https://api.z.ai/api/paas/v4/web_search``.

    Args:
        api_key: Z.AI API key (ZHIPU_API_KEY).
    """

    API_URL = "https://api.z.ai/api/paas/v4/web_search"

    def __init__(self, api_key: str, **_kwargs) -> None:
        self._api_key = api_key

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        import requests

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "search_engine": "search-prime",
            "search_query": query,
            "count": min(max_results, 50),
        }

        try:
            resp = requests.post(
                self.API_URL, json=payload, headers=headers, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Zhipu web search failed: %s", e)
            return []

        results: list[SearchResult] = []
        for item in data.get("search_result", [])[:max_results]:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("content", ""),
                )
            )
        return results
