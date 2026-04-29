"""Integration tests for standalone services — hits real APIs.

Run with: python -m pytest tests/test_services_integration.py -v -s
Requires .env with GEMINI_API_KEY, MINIMAX_API_KEY.
Saves outputs to ~/Downloads/lingtai-service-tests/
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Load .env
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.is_file():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

OUT_DIR = Path.home() / "Downloads" / "lingtai-service-tests"
OUT_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
MINIMAX_KEY = os.getenv("MINIMAX_API_KEY", "")


# ─── Web Search ──────────────────────────────────────────────────────────

class TestWebSearch:
    def test_duckduckgo(self):
        from lingtai.services.websearch import create_search_service
        svc = create_search_service("duckduckgo")
        results = svc.search("Python programming language", max_results=3)
        assert len(results) > 0
        print(f"\n  DuckDuckGo: {len(results)} results")
        for r in results:
            print(f"    {r.title}: {r.url}")

    @pytest.mark.skipif(not GEMINI_KEY, reason="GEMINI_API_KEY not set")
    def test_gemini(self):
        from lingtai.services.websearch import create_search_service
        svc = create_search_service("gemini", api_key=GEMINI_KEY)
        results = svc.search("what is lingtai AI agent framework")
        assert len(results) > 0
        print(f"\n  Gemini search: {results[0].snippet[:100]}...")

    @pytest.mark.skipif(not MINIMAX_KEY, reason="MINIMAX_API_KEY not set")
    def test_minimax(self):
        from lingtai.services.websearch import create_search_service
        svc = create_search_service("minimax", api_key=MINIMAX_KEY)
        try:
            results = svc.search("latest news today")
            print(f"\n  MiniMax search: {len(results)} results")
            for r in results[:2]:
                print(f"    {r.title}: {r.snippet[:80]}")
        finally:
            if hasattr(svc, "close"):
                svc.close()


# ─── Vision ──────────────────────────────────────────────────────────────

class TestVision:
    @pytest.fixture
    def test_image(self):
        """Create a simple test PNG image."""
        import struct
        import zlib

        width, height = 100, 100
        # Red 100x100 PNG
        raw = b""
        for _ in range(height):
            raw += b"\x00"  # filter byte
            for _ in range(width):
                raw += b"\xff\x00\x00"  # RGB red

        def _chunk(chunk_type, data):
            c = chunk_type + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n"
        png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        png += _chunk(b"IDAT", zlib.compress(raw))
        png += _chunk(b"IEND", b"")

        img_path = OUT_DIR / "test_image.png"
        img_path.write_bytes(png)
        return str(img_path)

    @pytest.mark.skipif(not GEMINI_KEY, reason="GEMINI_API_KEY not set")
    def test_gemini(self, test_image):
        from lingtai.services.vision import create_vision_service
        svc = create_vision_service("gemini", api_key=GEMINI_KEY)
        result = svc.analyze_image(test_image, prompt="What color is this image?")
        assert result
        assert len(result) > 5
        print(f"\n  Gemini vision: {result[:100]}")
