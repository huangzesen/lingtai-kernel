"""Tests for preset materialization at boot — _read_init substitutes the
active preset's llm + capabilities into manifest before validation."""
import json
from pathlib import Path

import pytest


def _make_workdir(tmp_path: Path, active_preset: str | None = None,
                  presets_path: str | None = None,
                  manifest_extra: dict | None = None) -> Path:
    """Create a working dir with init.json. Optionally points at a preset."""
    wd = tmp_path / "agent"
    wd.mkdir()
    manifest = {
        "agent_name": "alice",
        "language": "en",
        "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
        "capabilities": {"file": {}},
        "soul": {"delay": 120},
        "stamina": 3600,
        "molt_pressure": 0.8,
        "molt_prompt": "",
        "max_turns": 50,
        "admin": {"karma": True},
        "streaming": False,
    }
    if active_preset is not None:
        manifest["active_preset"] = active_preset
    if presets_path is not None:
        manifest["presets_path"] = presets_path
    if manifest_extra:
        manifest.update(manifest_extra)
    # Create a dummy env_file so validate_init doesn't reject api_key_env
    env_file = wd / ".env"
    env_file.write_text("")
    init = {
        "manifest": manifest,
        "principle": "p", "covenant": "c", "pad": "", "prompt": "",
        "soul": "",
        "env_file": str(env_file),
    }
    (wd / "init.json").write_text(json.dumps(init))
    return wd


def _make_preset_lib(tmp_path: Path, presets: dict[str, dict]) -> Path:
    """Create a presets dir with the given name → preset-content mapping."""
    pdir = tmp_path / "presets"
    pdir.mkdir()
    for name, content in presets.items():
        (pdir / f"{name}.json").write_text(json.dumps(content))
    return pdir


def _make_probe_agent(wd: Path):
    """Build a minimal Agent subclass that exposes _read_init for direct testing.

    We don't fully construct an Agent because that triggers full setup. Instead
    we shim the bare attributes _read_init needs: _working_dir and _log.
    """
    from lingtai.agent import Agent

    class _Probe(Agent):
        def __init__(self, working_dir):
            self._working_dir = Path(working_dir)
            self._log_events = []
        def _log(self, event, **kw):
            self._log_events.append((event, kw))
    return _Probe(wd)


def test_materialize_substitutes_llm_and_capabilities(tmp_path, monkeypatch):
    """When init.json has active_preset, _read_init substitutes the preset's
    llm + capabilities into the manifest."""
    plib = _make_preset_lib(tmp_path, {
        "minimax": {
            "name": "minimax",
            "description": "MiniMax M2.7",
            "manifest": {
                "llm": {"provider": "minimax", "model": "MiniMax-M2.7-highspeed",
                        "api_key": None, "api_key_env": "MINIMAX_API_KEY"},
                "capabilities": {"file": {}, "vision": {"provider": "minimax",
                                                        "api_key_env": "MINIMAX_API_KEY"}},
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset="minimax",
                       presets_path=str(plib))
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "minimax"
    assert data["manifest"]["llm"]["model"] == "MiniMax-M2.7-highspeed"
    assert "vision" in data["manifest"]["capabilities"]


def test_materialize_no_preset_field_unchanged(tmp_path):
    """init.json without active_preset behaves exactly as before."""
    wd = _make_workdir(tmp_path)
    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data["manifest"]["llm"]["provider"] == "deepseek"  # original


def test_materialize_unknown_preset_returns_none_and_logs(tmp_path):
    """Active preset that doesn't exist → _read_init returns None and logs."""
    plib = _make_preset_lib(tmp_path, {})
    wd = _make_workdir(tmp_path, active_preset="ghost", presets_path=str(plib))
    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is None
    events = [e for e, _ in a._log_events]
    assert "refresh_init_error" in events


def test_materialize_default_presets_path(tmp_path, monkeypatch):
    """active_preset without presets_path uses ~/.lingtai-tui/presets/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    plib = fake_home / ".lingtai-tui" / "presets"
    plib.mkdir(parents=True)
    (plib / "deepseek.json").write_text(json.dumps({
        "name": "deepseek",
        "description": "DeepSeek default",
        "manifest": {
            "llm": {"provider": "deepseek-NEW", "model": "v4-NEW",
                    "api_key": None, "api_key_env": "X"},
            "capabilities": {"file": {}},
        },
    }))
    wd = _make_workdir(tmp_path, active_preset="deepseek")  # no presets_path
    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "deepseek-NEW"


def test_materialize_inherit_expansion_runs(tmp_path, monkeypatch):
    """Capabilities with provider:inherit get the main LLM's provider after materialization."""
    plib = _make_preset_lib(tmp_path, {
        "smart": {
            "name": "smart",
            "description": "smart preset",
            "manifest": {
                "llm": {"provider": "gemini", "model": "gemini-2.5-pro",
                        "api_key": None, "api_key_env": "GEMINI_API_KEY"},
                "capabilities": {
                    "file": {},
                    "web_search": {"provider": "inherit"},
                },
            },
        },
    })
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    wd = _make_workdir(tmp_path, active_preset="smart", presets_path=str(plib))
    a = _make_probe_agent(wd)
    data = a._read_init()
    caps = data["manifest"]["capabilities"]
    assert caps["web_search"]["provider"] == "gemini"
    assert caps["web_search"]["api_key_env"] == "GEMINI_API_KEY"
