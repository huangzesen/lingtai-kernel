"""Codex OAuth token manager.

Reads tokens written by the TUI (``~/.lingtai-tui/codex-auth.json``),
checks expiry, and auto-refreshes via the OpenAI OAuth endpoint.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from filelock import FileLock

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_BUFFER_SECONDS = 300  # refresh if within 5 minutes of expiry


class CodexTokenManager:
    """Manages Codex OAuth tokens stored on disk by the TUI."""

    def __init__(self, token_path: str | None = None) -> None:
        if token_path is None:
            tui_dir = os.environ.get("LINGTAI_TUI_DIR", "~/.lingtai-tui")
            token_path = str(Path(tui_dir).expanduser() / "codex-auth.json")
        self._path = Path(token_path)
        self._lock_path = self._path.with_suffix(".json.lock")
        self._cache: dict | None = None
        self._cache_mtime: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_authenticated(self) -> bool:
        """Return *True* if the token file exists and contains a refresh token."""
        try:
            data = self._read()
            return bool(data.get("refresh_token"))
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return False

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing automatically if needed.

        Raises ``FileNotFoundError`` when the token file does not exist.
        """
        data = self._read()

        expires_at = data.get("expires_at", 0)
        if time.time() + REFRESH_BUFFER_SECONDS >= expires_at:
            self._refresh(data)
            data = self._read()

        return data["access_token"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read(self) -> dict:
        """Read the token file, using an mtime-based cache to avoid re-parsing."""
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Codex token file not found: {self._path}. "
                "Please authenticate via the TUI first."
            )

        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache

        with open(self._path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self._cache = data
        self._cache_mtime = mtime
        return data

    def _refresh(self, data: dict) -> None:
        """Refresh the access token using the stored refresh token.

        Uses a file lock so concurrent processes don't race.  After
        acquiring the lock the file is re-read — another process may have
        already completed the refresh.
        """
        lock = FileLock(self._lock_path, timeout=30)
        with lock:
            # Re-read inside the lock; someone else may have refreshed.
            fresh = self._read()
            if fresh.get("expires_at", 0) > time.time() + REFRESH_BUFFER_SECONDS:
                return  # already refreshed by another process

            refresh_token = fresh.get("refresh_token") or data.get("refresh_token")
            if not refresh_token:
                raise RuntimeError("No refresh_token available in token file.")

            response = httpx.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()

            # Merge new tokens into existing data, preserving email etc.
            fresh["access_token"] = result["access_token"]
            if "refresh_token" in result:
                fresh["refresh_token"] = result["refresh_token"]
            fresh["expires_at"] = result.get(
                "expires_at", int(time.time()) + result.get("expires_in", 3600)
            )

            tmp_path = self._path.with_suffix(".json.tmp")
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(fresh, f, indent=2)
            tmp_path.replace(self._path)

            # Invalidate cache so next _read() picks up the new file.
            self._cache = None
            self._cache_mtime = 0.0
