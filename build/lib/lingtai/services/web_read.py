"""WebReadService — abstract URL content extraction backing the web_read capability.

Implementations:
- TrafilaturaWebReadService — zero-API-key content extraction via trafilatura.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class WebReadResult:
    """Extracted content from a URL."""
    title: str
    content: str
    url: str


class WebReadService(ABC):
    """Abstract web content extraction service.

    Backs the web_read capability. Implementations fetch a URL and
    extract the main readable content.
    """

    @abstractmethod
    def read(self, url: str, output_format: str = "text") -> WebReadResult:
        """Fetch a URL and extract readable content.

        Args:
            url: The URL to read.
            output_format: Output format — 'text' or 'markdown'.

        Returns:
            Extracted content with title and URL.
        """
        ...


class ZhipuWebReadService(WebReadService):
    """Web content extraction via Z.AI's ``webReader`` HTTP MCP tool.

    Connects to the remote MCP server at
    ``https://api.z.ai/api/mcp/web_reader/mcp``.

    Args:
        api_key: Z.AI API key (ZHIPU_API_KEY).
    """

    MCP_URLS = {
        "ZAI": "https://api.z.ai/api/mcp/web_reader/mcp",
        "ZHIPU": "https://open.bigmodel.cn/api/mcp/web_reader/mcp",
    }

    def __init__(self, api_key: str, z_ai_mode: str = "ZAI") -> None:
        self._api_key = api_key
        self._z_ai_mode = z_ai_mode
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            if self._client.is_connected():
                return
            try:
                self._client.close()
            except Exception:
                pass

        from .mcp import HTTPMCPClient

        self._client = HTTPMCPClient(
            url=self.MCP_URLS.get(self._z_ai_mode, self.MCP_URLS["ZAI"]),
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        self._client.start()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def read(self, url: str, output_format: str = "markdown") -> WebReadResult:
        import json as _json

        self._ensure_client()
        result = self._client.call_tool("webReader", {"url": url})

        # Result may be a dict or a JSON string
        if isinstance(result, str):
            try:
                result = _json.loads(result)
            except (_json.JSONDecodeError, TypeError):
                # Plain text response
                if not result:
                    raise RuntimeError(f"No readable content extracted from: {url}")
                return WebReadResult(title="", content=result, url=url)

        if isinstance(result, dict):
            if result.get("status") == "error":
                raise RuntimeError(f"Zhipu web reader error: {result.get('message', 'unknown')}")
            content = result.get("content", "")
            title = result.get("title", "")
            if not content:
                raise RuntimeError(f"No readable content extracted from: {url}")
            return WebReadResult(title=title, content=content, url=url)

        raise RuntimeError(f"Unexpected response from web reader: {type(result)}")


class TrafilaturaWebReadService(WebReadService):
    """Zero-API-key web content extraction via trafilatura.

    Uses the ``trafilatura`` package to fetch and extract main content.
    Install with ``pip install lingtai[trafilatura]`` or
    ``pip install trafilatura``.
    """

    def read(self, url: str, output_format: str = "text") -> WebReadResult:
        import trafilatura  # type: ignore[import-untyped]

        downloaded = trafilatura.fetch_url(url)
        if downloaded is None:
            raise RuntimeError(f"Failed to fetch URL: {url}")

        fmt = "markdown" if output_format == "markdown" else "txt"
        content = trafilatura.extract(downloaded, output_format=fmt)
        if not content:
            raise RuntimeError(f"No readable content extracted from: {url}")

        # Extract title from metadata
        title = ""
        metadata = trafilatura.bare_extraction(downloaded)
        if metadata and isinstance(metadata, dict):
            title = metadata.get("title", "")

        return WebReadResult(title=title, content=content, url=url)
