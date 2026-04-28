# tests/test_presets.py
import json
from pathlib import Path

import pytest

from lingtai.presets import discover_presets, load_preset, default_presets_path, expand_inherit


def test_discover_presets_empty_dir(tmp_path):
    """Empty directory returns empty dict."""
    assert discover_presets(tmp_path) == {}


def test_discover_presets_lists_json_files(tmp_path):
    """Top-level *.json files are discovered, indexed by stem."""
    (tmp_path / "alpha.json").write_text('{"name": "alpha", "manifest": {"llm": {"provider": "x", "model": "y"}}}')
    (tmp_path / "beta.json").write_text('{"name": "beta", "manifest": {"llm": {"provider": "x", "model": "y"}}}')
    result = discover_presets(tmp_path)
    assert set(result.keys()) == {"alpha", "beta"}
    assert result["alpha"].name == "alpha.json"
    assert result["beta"].name == "beta.json"


def test_discover_presets_ignores_non_json(tmp_path):
    """Non-.json files (README.md, etc.) are ignored."""
    (tmp_path / "preset.json").write_text('{"name": "p", "manifest": {"llm": {"provider": "x", "model": "y"}}}')
    (tmp_path / "README.md").write_text("# Library docs")
    (tmp_path / "notes.txt").write_text("scratch")
    assert set(discover_presets(tmp_path).keys()) == {"preset"}


def test_discover_presets_ignores_subdirs(tmp_path):
    """Subdirectories are not recursed into."""
    (tmp_path / "top.json").write_text('{"name": "top", "manifest": {"llm": {"provider": "x", "model": "y"}}}')
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "nested.json").write_text('{"name": "nested", "manifest": {"llm": {"provider": "x", "model": "y"}}}')
    assert set(discover_presets(tmp_path).keys()) == {"top"}


def test_discover_presets_accepts_jsonc(tmp_path):
    """*.jsonc files are also discovered."""
    (tmp_path / "with_comments.jsonc").write_text('{"name": "with_comments", "manifest": {"llm": {"provider": "x", "model": "y"}}}')
    assert "with_comments" in discover_presets(tmp_path)


def test_discover_presets_missing_dir(tmp_path):
    """Nonexistent directory returns empty dict (no error)."""
    missing = tmp_path / "does_not_exist"
    assert discover_presets(missing) == {}


def test_default_presets_path_returns_correct_location():
    """default_presets_path returns ~/.lingtai-tui/presets/ as a Path."""
    p = default_presets_path()
    assert isinstance(p, Path)
    assert p.parts[-2:] == (".lingtai-tui", "presets")


def _write_preset(dir: Path, name: str, content: dict) -> Path:
    p = dir / f"{name}.json"
    p.write_text(json.dumps(content))
    return p


def _valid_preset(name: str = "test") -> dict:
    return {
        "name": name,
        "description": "test preset",
        "manifest": {
            "llm": {"provider": "deepseek", "model": "deepseek-v4-flash",
                    "api_key": None, "api_key_env": "DEEPSEEK_API_KEY"},
            "capabilities": {"file": {}, "email": {}},
        },
    }


def test_load_preset_returns_parsed_dict(tmp_path):
    _write_preset(tmp_path, "alpha", _valid_preset("alpha"))
    p = load_preset(tmp_path, "alpha")
    assert p["name"] == "alpha"
    assert p["manifest"]["llm"]["provider"] == "deepseek"


def test_load_preset_missing_raises_key_error(tmp_path):
    with pytest.raises(KeyError, match="nonexistent"):
        load_preset(tmp_path, "nonexistent")


def test_load_preset_jsonc_strips_comments(tmp_path):
    """JSONC with // comments and trailing commas parses correctly."""
    body = '''{
      "name": "withcomments",   // inline comment
      "description": "tests JSONC",
      "manifest": {
        "llm": {"provider": "x", "model": "y"},
        "capabilities": {"file": {}},   // trailing comma here
      },
    }'''
    (tmp_path / "withcomments.jsonc").write_text(body)
    p = load_preset(tmp_path, "withcomments")
    assert p["name"] == "withcomments"


def test_load_preset_missing_manifest_raises(tmp_path):
    """Preset without manifest.llm is rejected."""
    bad = {"name": "bad", "description": "x"}
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="manifest"):
        load_preset(tmp_path, "bad")


def test_load_preset_missing_llm_raises(tmp_path):
    """Preset with manifest but no llm is rejected."""
    bad = {"name": "bad", "manifest": {"capabilities": {}}}
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="manifest.llm"):
        load_preset(tmp_path, "bad")


def test_load_preset_empty_provider_raises(tmp_path):
    """Preset with empty provider string is rejected (not just missing key)."""
    bad = {"name": "bad", "manifest": {"llm": {"provider": "", "model": "y"}, "capabilities": {}}}
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="provider"):
        load_preset(tmp_path, "bad")


def test_load_preset_empty_model_raises(tmp_path):
    """Preset with empty model string is rejected."""
    bad = {"name": "bad", "manifest": {"llm": {"provider": "x", "model": ""}, "capabilities": {}}}
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="model"):
        load_preset(tmp_path, "bad")


def test_load_preset_malformed_json_raises(tmp_path):
    """Unparseable JSON is rejected with a clear error."""
    (tmp_path / "broken.json").write_text("{ not valid json }")
    with pytest.raises(ValueError, match="parse"):
        load_preset(tmp_path, "broken")


def test_load_preset_warns_on_name_mismatch(tmp_path, caplog):
    """Filename stem != internal `name` field logs a warning but still loads."""
    import logging
    p = _valid_preset("internal_name")
    (tmp_path / "filename_stem.json").write_text(json.dumps(p))
    caplog.set_level(logging.WARNING, logger="lingtai.presets")
    result = load_preset(tmp_path, "filename_stem")
    assert result["name"] == "internal_name"
    assert any("name mismatch" in r.message.lower() for r in caplog.records)


def test_expand_inherit_resolves_to_main_llm():
    """`provider: "inherit"` is replaced with main LLM's provider + creds."""
    main_llm = {
        "provider": "gemini", "model": "gemini-2.5-pro",
        "api_key": None, "api_key_env": "GEMINI_API_KEY",
        "base_url": None,
    }
    caps = {
        "web_search": {"provider": "inherit"},
        "vision":     {"provider": "inherit"},
        "file":       {},
    }
    expand_inherit(caps, main_llm)
    assert caps["web_search"]["provider"] == "gemini"
    assert caps["web_search"]["api_key_env"] == "GEMINI_API_KEY"
    assert caps["vision"]["provider"] == "gemini"
    assert caps["file"] == {}  # untouched


def test_expand_inherit_does_not_inherit_model():
    """`model` field is NOT inherited — capability picks its own."""
    main_llm = {
        "provider": "openai", "model": "gpt-5",
        "api_key": None, "api_key_env": "OPENAI_API_KEY",
    }
    caps = {"vision": {"provider": "inherit"}}
    expand_inherit(caps, main_llm)
    assert "model" not in caps["vision"]


def test_expand_inherit_no_op_for_explicit_provider():
    """Explicit providers are not touched."""
    main_llm = {"provider": "gemini", "model": "x", "api_key_env": "GEMINI_API_KEY"}
    caps = {"web_search": {"provider": "duckduckgo"}}
    expand_inherit(caps, main_llm)
    assert caps["web_search"] == {"provider": "duckduckgo"}


def test_expand_inherit_handles_missing_main_llm_creds():
    """Inherit from main LLM with no api_key_env still expands provider, with None creds."""
    main_llm = {"provider": "local", "model": "x"}
    caps = {"web_search": {"provider": "inherit"}}
    expand_inherit(caps, main_llm)
    assert caps["web_search"]["provider"] == "local"
    assert caps["web_search"].get("api_key_env") is None
