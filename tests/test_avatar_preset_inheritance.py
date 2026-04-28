"""Tests that avatar spawn correctly inherits presets_path + active_preset."""
import json
from pathlib import Path

import pytest


def _baseline_parent_init(presets_path: str | None = None,
                          active_preset: str | None = None) -> dict:
    """Build a minimal but valid parent init.json dict."""
    manifest = {
        "agent_name": "parent",
        "language": "en",
        "llm": {"provider": "x", "model": "x", "api_key": None,
                "api_key_env": "X"},
        "capabilities": {},
        "soul": {"delay": 120}, "stamina": 3600,
        "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 50,
        "admin": {}, "streaming": False,
    }
    if presets_path is not None:
        manifest["presets_path"] = presets_path
    if active_preset is not None:
        manifest["active_preset"] = active_preset
    return {
        "manifest": manifest,
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
    }


def test_avatar_inherits_active_preset_and_absolute_path(tmp_path):
    """Avatar's init.json carries parent's active_preset and absolute presets_path verbatim."""
    parent_init = _baseline_parent_init(
        presets_path="/abs/path/to/presets", active_preset="minimax")

    from lingtai.core.avatar import AvatarManager
    avatar_init = AvatarManager._make_avatar_init(parent_init, "child")

    assert avatar_init["manifest"]["active_preset"] == "minimax"
    assert avatar_init["manifest"]["presets_path"] == "/abs/path/to/presets"


def test_avatar_resolves_relative_presets_path(tmp_path):
    """If parent's presets_path is relative, avatar gets it resolved to absolute."""
    parent_wd = tmp_path / "parent"
    parent_wd.mkdir()
    (parent_wd / "presets").mkdir()
    parent_init = _baseline_parent_init(
        presets_path="./presets", active_preset="x")

    from lingtai.core.avatar import AvatarManager
    avatar_init = AvatarManager._make_avatar_init(
        parent_init, "child", parent_working_dir=parent_wd)

    presets_path = avatar_init["manifest"]["presets_path"]
    # Must be absolute and point to the parent's presets folder
    assert Path(presets_path).is_absolute()
    assert Path(presets_path) == (parent_wd / "presets").resolve()


def test_avatar_no_preset_unchanged(tmp_path):
    """Avatar with parent that has no presets carries no presets_path/active_preset."""
    parent_init = _baseline_parent_init()

    from lingtai.core.avatar import AvatarManager
    avatar_init = AvatarManager._make_avatar_init(parent_init, "child")

    assert "active_preset" not in avatar_init["manifest"]
    assert "presets_path" not in avatar_init["manifest"]


def test_avatar_no_parent_working_dir_relative_path_unchanged(tmp_path):
    """If parent_working_dir is None, relative presets_path is left as-is.

    This preserves backward compatibility with callers that don't pass the new
    keyword. Production callers (avatar._spawn) always pass it; tests may not.
    """
    parent_init = _baseline_parent_init(
        presets_path="./presets", active_preset="x")

    from lingtai.core.avatar import AvatarManager
    avatar_init = AvatarManager._make_avatar_init(parent_init, "child")

    # Without parent_working_dir, the path is preserved verbatim
    assert avatar_init["manifest"]["presets_path"] == "./presets"
