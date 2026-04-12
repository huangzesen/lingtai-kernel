"""Tests for lingtai.auth.codex.CodexTokenManager."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from lingtai.auth.codex import REFRESH_BUFFER_SECONDS, CodexTokenManager


def _write_token_file(path, *, access_token="tok_valid", refresh_token="rt_abc",
                      expires_at=None, email="user@example.com"):
    """Helper to write a well-formed token file."""
    if expires_at is None:
        expires_at = int(time.time()) + 3600  # 1 hour from now
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "email": email,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


# ------------------------------------------------------------------
# get_access_token
# ------------------------------------------------------------------

class TestGetAccessToken:
    def test_valid_token(self, tmp_path):
        """Token with future expiry is returned as-is."""
        token_file = tmp_path / "codex-auth.json"
        _write_token_file(token_file, access_token="tok_fresh")

        mgr = CodexTokenManager(token_path=str(token_file))
        assert mgr.get_access_token() == "tok_fresh"

    def test_missing_file_raises(self, tmp_path):
        """FileNotFoundError is raised when the token file does not exist."""
        token_file = tmp_path / "nonexistent" / "codex-auth.json"
        mgr = CodexTokenManager(token_path=str(token_file))

        with pytest.raises(FileNotFoundError):
            mgr.get_access_token()


# ------------------------------------------------------------------
# is_authenticated
# ------------------------------------------------------------------

class TestIsAuthenticated:
    def test_true_when_file_has_refresh_token(self, tmp_path):
        token_file = tmp_path / "codex-auth.json"
        _write_token_file(token_file)

        mgr = CodexTokenManager(token_path=str(token_file))
        assert mgr.is_authenticated() is True

    def test_false_when_no_file(self, tmp_path):
        token_file = tmp_path / "does-not-exist.json"
        mgr = CodexTokenManager(token_path=str(token_file))
        assert mgr.is_authenticated() is False


# ------------------------------------------------------------------
# Refresh behaviour
# ------------------------------------------------------------------

class TestRefresh:
    @patch("lingtai.auth.codex.httpx.post")
    def test_refresh_on_expired_token(self, mock_post, tmp_path):
        """An expired token triggers a refresh; new tokens are written."""
        token_file = tmp_path / "codex-auth.json"
        _write_token_file(
            token_file,
            access_token="tok_old",
            expires_at=int(time.time()) - 60,  # already expired
        )

        new_expires_at = int(time.time()) + 7200
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "tok_new",
            "refresh_token": "rt_new",
            "expires_at": new_expires_at,
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        mgr = CodexTokenManager(token_path=str(token_file))
        token = mgr.get_access_token()

        assert token == "tok_new"
        mock_post.assert_called_once()

        # Verify file was updated
        written = json.loads(token_file.read_text(encoding="utf-8"))
        assert written["access_token"] == "tok_new"
        assert written["refresh_token"] == "rt_new"
        assert written["expires_at"] == new_expires_at
        assert written["email"] == "user@example.com"  # preserved

    @patch("lingtai.auth.codex.httpx.post")
    def test_refresh_near_expiry(self, mock_post, tmp_path):
        """Token expiring within the buffer window triggers a refresh."""
        token_file = tmp_path / "codex-auth.json"
        # Expires in 2 minutes — inside the 5-minute buffer
        near_expiry = int(time.time()) + 120
        _write_token_file(
            token_file,
            access_token="tok_soon_expired",
            expires_at=near_expiry,
        )

        new_expires_at = int(time.time()) + 7200
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "tok_refreshed",
            "refresh_token": "rt_refreshed",
            "expires_at": new_expires_at,
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        mgr = CodexTokenManager(token_path=str(token_file))
        token = mgr.get_access_token()

        assert token == "tok_refreshed"
        mock_post.assert_called_once()
