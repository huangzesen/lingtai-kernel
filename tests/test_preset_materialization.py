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
        preset_block: dict = {"active": active_preset, "default": active_preset}
        if presets_path is not None:
            preset_block["path"] = presets_path
        manifest["preset"] = preset_block
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


def test_materialize_relative_presets_path_resolves_against_workdir(tmp_path, monkeypatch):
    """A relative presets_path is resolved against the agent's working dir, not CWD."""
    wd = tmp_path / "agent"
    wd.mkdir()
    plib = wd / "my_presets"
    plib.mkdir()
    (plib / "local.json").write_text(json.dumps({
        "name": "local",
        "description": "local preset",
        "manifest": {
            "llm": {"provider": "p1", "model": "m1",
                    "api_key": None, "api_key_env": "P1KEY"},
            "capabilities": {"file": {}},
        },
    }))
    # Set up env_file (validate_init requires it when api_key_env is set)
    env = wd / ".env"
    env.write_text("P1KEY=sk-test\n")

    init = {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "preset": {
                "path": "./my_presets",  # RELATIVE to agent workdir
                "active": "local",
                "default": "local",
            },
            "llm": {"provider": "PLACEHOLDER", "model": "PLACEHOLDER",
                    "api_key": None, "api_key_env": "P1KEY"},
            "capabilities": {},
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
        "env_file": str(env),
    }
    (wd / "init.json").write_text(json.dumps(init))

    # Change CWD to a different location entirely so a CWD-relative resolution would fail
    monkeypatch.chdir(tmp_path)

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "p1"


def test_materialize_omitted_path_falls_back_to_default(tmp_path, monkeypatch):
    """preset block without path falls back to ~/.lingtai-tui/presets."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    plib = fake_home / ".lingtai-tui" / "presets"
    plib.mkdir(parents=True)
    (plib / "fallback.json").write_text(json.dumps({
        "name": "fallback",
        "description": "fallback preset",
        "manifest": {
            "llm": {"provider": "p2", "model": "m2",
                    "api_key": None, "api_key_env": "P2KEY"},
            "capabilities": {"file": {}},
        },
    }))

    wd = tmp_path / "agent"
    wd.mkdir()
    env = wd / ".env"
    env.write_text("P2KEY=sk-test\n")

    init = {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "preset": {
                "active": "fallback",  # path omitted → falls back to default
                "default": "fallback",
            },
            "llm": {"provider": "PLACEHOLDER", "model": "PLACEHOLDER",
                    "api_key": None, "api_key_env": "P2KEY"},
            "capabilities": {},
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
        "env_file": str(env),
    }
    (wd / "init.json").write_text(json.dumps(init))

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["llm"]["provider"] == "p2"


def test_materialize_picks_up_context_limit_from_legacy_layout(tmp_path, monkeypatch):
    """A legacy preset (context_limit at manifest root) still works end-to-end.

    The kernel migration system (m001) runs from inside load_preset and
    relocates the field to the canonical location before the materializer
    reads the preset. The materializer then writes it into init.json's
    manifest root (init.json's schema is unchanged).
    """
    from lingtai_kernel.migrate.migrate import reset_process_cache
    reset_process_cache()
    plib = _make_preset_lib(tmp_path, {
        "narrow": {
            "name": "narrow",
            "description": "narrow context",
            "manifest": {
                "llm": {"provider": "p", "model": "m",
                        "api_key": None, "api_key_env": "PKEY"},
                "capabilities": {"file": {}},
                "context_limit": 16384,
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset="narrow",
                       presets_path=str(plib))
    monkeypatch.setenv("PKEY", "sk-test")

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["context_limit"] == 16384
    # The migration rewrote the on-disk preset to the canonical layout.
    on_disk = json.loads((plib / "narrow.json").read_text())
    assert "context_limit" not in on_disk["manifest"]
    assert on_disk["manifest"]["llm"]["context_limit"] == 16384


def test_materialize_picks_up_context_limit_from_llm_block(tmp_path, monkeypatch):
    """Canonical layout: context_limit lives inside manifest.llm in the preset.

    The materializer must lift it out of llm and write it to manifest root
    in init.json (the runtime contract — init.json schema is unchanged).
    The materialized llm block must NOT carry context_limit forward, since
    init.json's llm block doesn't have that field.
    """
    plib = _make_preset_lib(tmp_path, {
        "narrow": {
            "name": "narrow",
            "description": "narrow context",
            "manifest": {
                "llm": {"provider": "p", "model": "m",
                        "api_key": None, "api_key_env": "PKEY",
                        "context_limit": 16384},
                "capabilities": {"file": {}},
            },
        },
    })
    wd = _make_workdir(tmp_path, active_preset="narrow",
                       presets_path=str(plib))
    monkeypatch.setenv("PKEY", "sk-test")

    a = _make_probe_agent(wd)
    data = a._read_init()
    assert data is not None
    assert data["manifest"]["context_limit"] == 16384
    # llm block in init.json schema does not carry context_limit
    assert "context_limit" not in data["manifest"]["llm"]


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
