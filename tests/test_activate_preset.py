"""Tests for Agent._activate_preset — substitute a new preset's llm +
capabilities into init.json and write atomically."""
import json
from pathlib import Path

import pytest


def _make_workdir_and_lib(tmp_path: Path) -> tuple[Path, Path]:
    """Create a workdir with init.json and a presets library with two presets."""
    plib = tmp_path / "presets"
    plib.mkdir()
    (plib / "deepseek.json").write_text(json.dumps({
        "name": "deepseek",
        "description": "DeepSeek V4",
        "manifest": {
            "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                    "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
            "capabilities": {"file": {}, "web_search": {"provider": "duckduckgo"}},
        },
    }))
    (plib / "minimax.json").write_text(json.dumps({
        "name": "minimax",
        "description": "MiniMax M2.7",
        "manifest": {
            "llm": {"provider": "minimax", "model": "MiniMax-M2.7-highspeed",
                    "api_key": None, "api_key_env": "MINIMAX_API_KEY"},
            "capabilities": {"file": {}, "vision": {"provider": "minimax",
                                                    "api_key_env": "MINIMAX_API_KEY"}},
        },
    }))

    wd = tmp_path / "agent"
    wd.mkdir()
    init = {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "presets_path": str(plib),
            "active_preset": "deepseek",
            "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                    "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
            "capabilities": {"file": {}, "web_search": {"provider": "duckduckgo"}},
            "soul": {"delay": 120},
            "stamina": 3600,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 50,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
    }
    (wd / "init.json").write_text(json.dumps(init))
    return wd, plib


def _make_probe_agent(wd: Path):
    from lingtai.agent import Agent

    class _Probe(Agent):
        def __init__(self, working_dir):
            self._working_dir = Path(working_dir)
            self._log_events = []
        def _log(self, event, **kw):
            self._log_events.append((event, kw))
    return _Probe(wd)


def test_activate_preset_substitutes_llm_and_capabilities(tmp_path):
    wd, plib = _make_workdir_and_lib(tmp_path)
    a = _make_probe_agent(wd)
    a._activate_preset("minimax")

    # init.json on disk now reflects minimax
    data = json.loads((wd / "init.json").read_text())
    assert data["manifest"]["llm"]["provider"] == "minimax"
    assert data["manifest"]["llm"]["model"] == "MiniMax-M2.7-highspeed"
    assert "vision" in data["manifest"]["capabilities"]
    assert data["manifest"]["active_preset"] == "minimax"


def test_activate_preset_preserves_other_manifest_fields(tmp_path):
    """admin, soul, stamina, agent_name, etc. survive the swap."""
    wd, plib = _make_workdir_and_lib(tmp_path)
    a = _make_probe_agent(wd)
    a._activate_preset("minimax")

    data = json.loads((wd / "init.json").read_text())
    m = data["manifest"]
    assert m["agent_name"] == "alice"
    assert m["admin"]["karma"] is True
    assert m["soul"]["delay"] == 120
    assert m["stamina"] == 3600


def test_activate_preset_unknown_raises_key_error(tmp_path):
    """Unknown preset name → KeyError; init.json untouched."""
    wd, plib = _make_workdir_and_lib(tmp_path)
    original = (wd / "init.json").read_text()
    a = _make_probe_agent(wd)

    with pytest.raises(KeyError, match="ghost"):
        a._activate_preset("ghost")

    # Disk is unchanged
    assert (wd / "init.json").read_text() == original


def test_activate_preset_atomic_write(tmp_path, monkeypatch):
    """If the disk write fails midway, init.json keeps its original content."""
    wd, plib = _make_workdir_and_lib(tmp_path)
    original = (wd / "init.json").read_text()
    a = _make_probe_agent(wd)

    # Force os.replace to fail
    import os
    real_replace = os.replace
    def failing_replace(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("os.replace", failing_replace)

    with pytest.raises(OSError, match="disk full"):
        a._activate_preset("minimax")

    # init.json on disk is the original
    assert (wd / "init.json").read_text() == original


def test_activate_preset_uses_default_path_when_unset(tmp_path, monkeypatch):
    """If init.json has active_preset but no presets_path, default applies."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    plib = fake_home / ".lingtai-tui" / "presets"
    plib.mkdir(parents=True)
    (plib / "minimax.json").write_text(json.dumps({
        "name": "minimax",
        "description": "MiniMax",
        "manifest": {
            "llm": {"provider": "minimax", "model": "x",
                    "api_key": None, "api_key_env": "MINIMAX_API_KEY"},
            "capabilities": {"file": {}},
        },
    }))

    # Build workdir without presets_path
    wd = tmp_path / "agent"
    wd.mkdir()
    init = {
        "manifest": {
            "agent_name": "alice", "language": "en",
            "active_preset": "deepseek",
            "llm": {"provider": "x", "model": "x", "api_key": None,
                    "api_key_env": "X"},
            "capabilities": {},
            "soul": {"delay": 120}, "stamina": 3600, "molt_pressure": 0.8,
            "molt_prompt": "", "max_turns": 50, "admin": {}, "streaming": False,
        },
        "principle": "p", "covenant": "c", "pad": "", "prompt": "", "soul": "",
    }
    (wd / "init.json").write_text(json.dumps(init))
    a = _make_probe_agent(wd)
    a._activate_preset("minimax")

    data = json.loads((wd / "init.json").read_text())
    assert data["manifest"]["llm"]["provider"] == "minimax"
