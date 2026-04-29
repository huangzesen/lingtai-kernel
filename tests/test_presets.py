# tests/test_presets.py
import json
from pathlib import Path

import pytest

from lingtai.presets import (
    discover_presets,
    load_preset,
    default_presets_path,
    expand_inherit,
    preset_context_limit,
    preset_tags,
    preset_tier,
    resolve_presets_path,
    TIER_TAGS,
    TIER_VALUES,
)


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


def test_load_preset_accepts_context_limit_inside_llm_block(tmp_path):
    """Canonical layout: context_limit inside manifest.llm — accepted."""
    p = {
        "name": "okllm",
        "manifest": {
            "llm": {"provider": "x", "model": "y", "context_limit": 65536},
            "capabilities": {},
        },
    }
    (tmp_path / "okllm.json").write_text(json.dumps(p))
    loaded = load_preset(tmp_path, "okllm")
    assert preset_context_limit(loaded["manifest"]) == 65536


def test_load_preset_relocates_legacy_root_context_limit(tmp_path):
    """Legacy on-disk layout: context_limit at manifest root.

    The kernel migration system (m001) runs from inside load_preset and
    relocates the field into manifest.llm before validation. So loading a
    legacy preset is transparent to callers — they see the canonical
    layout, and the file on disk is rewritten in place.
    """
    p = {
        "name": "legacy",
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
            "context_limit": 32768,
        },
    }
    (tmp_path / "legacy.json").write_text(json.dumps(p))

    loaded = load_preset(tmp_path, "legacy")

    # Helper sees the canonical location.
    assert preset_context_limit(loaded["manifest"]) == 32768
    assert loaded["manifest"]["llm"]["context_limit"] == 32768
    # The migration rewrote the file in place.
    on_disk = json.loads((tmp_path / "legacy.json").read_text())
    assert "context_limit" not in on_disk["manifest"]
    assert on_disk["manifest"]["llm"]["context_limit"] == 32768


def test_load_preset_rejects_context_limit_at_manifest_root(tmp_path):
    """An ambiguous preset (both locations populated) is skipped by the
    migration and rejected by load_preset's validator with a clear pointer
    to the canonical location.
    """
    from lingtai_kernel.migrate.migrate import reset_process_cache
    reset_process_cache()
    p = {
        "name": "dup",
        "manifest": {
            "llm": {"provider": "x", "model": "y", "context_limit": 16384},
            "capabilities": {},
            "context_limit": 32768,
        },
    }
    (tmp_path / "dup.json").write_text(json.dumps(p))
    with pytest.raises(ValueError, match="manifest.llm"):
        load_preset(tmp_path, "dup")


def test_load_preset_rejects_non_integer_context_limit(tmp_path):
    """context_limit must be an integer (typo guard)."""
    p = {
        "name": "bad",
        "manifest": {
            "llm": {"provider": "x", "model": "y", "context_limit": "65536"},
            "capabilities": {},
        },
    }
    (tmp_path / "bad.json").write_text(json.dumps(p))
    with pytest.raises(ValueError, match="integer"):
        load_preset(tmp_path, "bad")


def test_preset_context_limit_reads_from_llm_block():
    """Helper reads from the canonical location (manifest.llm.context_limit)."""
    manifest = {
        "llm": {"provider": "x", "model": "y", "context_limit": 16384},
        "capabilities": {},
    }
    assert preset_context_limit(manifest) == 16384


def test_preset_context_limit_returns_none_when_unset():
    """No context_limit anywhere → None."""
    manifest = {"llm": {"provider": "x", "model": "y"}, "capabilities": {}}
    assert preset_context_limit(manifest) is None


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


# ---------------------------------------------------------------------------
# resolve_presets_path
# ---------------------------------------------------------------------------


def test_load_preset_rejects_dot_dot_traversal(tmp_path):
    """A preset name containing path traversal (..) is rejected."""
    plib = tmp_path / "presets"
    plib.mkdir()
    # Create a file outside the library that would be reachable via traversal
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({
        "name": "secret", "manifest": {"llm": {"provider": "x", "model": "y"}}
    }))
    with pytest.raises(KeyError, match="not found|escapes"):
        load_preset(plib, "../secret")


def test_load_preset_rejects_absolute_path_in_name(tmp_path):
    """A preset name that is an absolute path is rejected (or simply not found)."""
    plib = tmp_path / "presets"
    plib.mkdir()
    with pytest.raises(KeyError):
        load_preset(plib, "/etc/passwd")


def test_load_preset_rejects_nested_traversal(tmp_path):
    """A preset name with multiple '..' segments is rejected."""
    plib = tmp_path / "presets"
    plib.mkdir()
    sneaky = tmp_path.parent / "sneaky.json"
    if sneaky.parent.exists():
        sneaky.write_text('{"name":"x","manifest":{"llm":{"provider":"x","model":"y"}}}')
        try:
            with pytest.raises(KeyError):
                load_preset(plib, "../../sneaky")
        finally:
            sneaky.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# resolve_presets_path
# ---------------------------------------------------------------------------


def test_resolve_presets_path_absolute(tmp_path):
    abs_path = tmp_path / "presets"
    manifest = {"preset": {"path": str(abs_path), "active": "x", "default": "x"}}
    result = resolve_presets_path(manifest, tmp_path / "wd")
    assert result == [abs_path]


def test_resolve_presets_path_relative(tmp_path):
    """Relative path resolves against working_dir, not CWD."""
    wd = tmp_path / "wd"
    wd.mkdir()
    manifest = {"preset": {"path": "./my_presets", "active": "x", "default": "x"}}
    result = resolve_presets_path(manifest, wd)
    assert result == [(wd / "my_presets").resolve()]


def test_resolve_presets_path_default_when_missing(tmp_path):
    """Missing preset block falls back to default_presets_path."""
    manifest = {}
    result = resolve_presets_path(manifest, tmp_path)
    assert result == [default_presets_path()]


def test_resolve_presets_path_default_when_empty(tmp_path):
    """Empty preset.path falls back to default."""
    manifest = {"preset": {"active": "x", "default": "x"}}  # path absent
    result = resolve_presets_path(manifest, tmp_path)
    assert result == [default_presets_path()]


# Multi-path support: manifest.preset.path can be a list of strings.

def test_resolve_presets_path_accepts_list_of_strings(tmp_path):
    """A list of paths is preserved in order, each resolved independently."""
    wd = tmp_path / "wd"
    wd.mkdir()
    abs_lib = tmp_path / "abs_lib"
    manifest = {
        "preset": {
            "path": [str(abs_lib), "./relative_lib"],
            "active": "x",
            "default": "x",
        }
    }
    result = resolve_presets_path(manifest, wd)
    assert result == [abs_lib, (wd / "relative_lib").resolve()]


def test_resolve_presets_path_empty_list_falls_back_to_default(tmp_path):
    """An explicitly empty list behaves like 'unset' — default kicks in."""
    manifest = {"preset": {"path": [], "active": "x", "default": "x"}}
    result = resolve_presets_path(manifest, tmp_path)
    assert result == [default_presets_path()]


def test_resolve_presets_path_single_string_still_returns_list(tmp_path):
    """Backward compat: a single string is normalized to a one-element list."""
    abs_path = tmp_path / "presets"
    manifest = {"preset": {"path": str(abs_path), "active": "x", "default": "x"}}
    result = resolve_presets_path(manifest, tmp_path / "wd")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == abs_path


def test_discover_presets_accepts_list_of_paths(tmp_path):
    """When given a list, discover_presets unions across all directories."""
    lib1 = tmp_path / "lib1"
    lib2 = tmp_path / "lib2"
    lib1.mkdir()
    lib2.mkdir()
    _write_preset(lib1, "alpha", _valid_preset("alpha"))
    _write_preset(lib2, "beta", _valid_preset("beta"))
    result = discover_presets([lib1, lib2])
    assert set(result.keys()) == {"alpha", "beta"}
    assert result["alpha"].parent == lib1
    assert result["beta"].parent == lib2


def test_discover_presets_first_path_wins_on_collision(tmp_path):
    """When two paths share a preset name, the earlier path wins."""
    lib1 = tmp_path / "lib1"
    lib2 = tmp_path / "lib2"
    lib1.mkdir()
    lib2.mkdir()
    _write_preset(lib1, "shared", _valid_preset("from_lib1"))
    _write_preset(lib2, "shared", _valid_preset("from_lib2"))
    result = discover_presets([lib1, lib2])
    assert result["shared"].parent == lib1


def test_discover_presets_skips_missing_dirs_in_list(tmp_path):
    """Nonexistent paths in the list are silently skipped — others still work."""
    lib1 = tmp_path / "lib1"
    lib1.mkdir()
    _write_preset(lib1, "alpha", _valid_preset("alpha"))
    missing = tmp_path / "does_not_exist"
    result = discover_presets([missing, lib1])
    assert set(result.keys()) == {"alpha"}


def test_load_preset_accepts_list_of_paths_first_wins(tmp_path):
    """load_preset with a list searches paths in order; first match wins."""
    lib1 = tmp_path / "lib1"
    lib2 = tmp_path / "lib2"
    lib1.mkdir()
    lib2.mkdir()
    _write_preset(lib1, "shared", _valid_preset("from_lib1"))
    _write_preset(lib2, "shared", _valid_preset("from_lib2"))
    loaded = load_preset([lib1, lib2], "shared")
    assert loaded["name"] == "from_lib1"


def test_load_preset_accepts_list_falls_through_to_later_path(tmp_path):
    """If first path lacks the preset, fall through to the next."""
    lib1 = tmp_path / "lib1"
    lib2 = tmp_path / "lib2"
    lib1.mkdir()
    lib2.mkdir()
    _write_preset(lib2, "only_in_lib2", _valid_preset("only_in_lib2"))
    loaded = load_preset([lib1, lib2], "only_in_lib2")
    assert loaded["name"] == "only_in_lib2"


def test_load_preset_list_raises_when_not_in_any_path(tmp_path):
    """Preset missing from every listed path → KeyError mentioning the name."""
    lib1 = tmp_path / "lib1"
    lib2 = tmp_path / "lib2"
    lib1.mkdir()
    lib2.mkdir()
    with pytest.raises(KeyError, match="missing"):
        load_preset([lib1, lib2], "missing")


# ---------------------------------------------------------------------------
# Tags schema + tier vocabulary
# ---------------------------------------------------------------------------

def test_load_preset_accepts_tags_list(tmp_path):
    """Top-level tags: list[str] is accepted and round-trips."""
    p = {
        "name": "tagged",
        "tags": ["tier:4", "specialty:code"],
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    (tmp_path / "tagged.json").write_text(json.dumps(p))
    loaded = load_preset(tmp_path, "tagged")
    assert loaded["tags"] == ["tier:4", "specialty:code"]


def test_load_preset_treats_missing_tags_as_empty(tmp_path):
    """Presets without a tags field are valid; preset_tags returns []."""
    p = _valid_preset("notags")
    (tmp_path / "notags.json").write_text(json.dumps(p))
    loaded = load_preset(tmp_path, "notags")
    assert preset_tags(loaded) == []


def test_load_preset_rejects_non_list_tags(tmp_path):
    """tags must be a list, not a string or dict."""
    p = {
        "name": "bad",
        "tags": "tier:4",  # should be a list
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    (tmp_path / "bad.json").write_text(json.dumps(p))
    with pytest.raises(ValueError, match="tags.*list"):
        load_preset(tmp_path, "bad")


def test_load_preset_rejects_non_string_tag_entry(tmp_path):
    """Each tag must be a string."""
    p = {
        "name": "bad",
        "tags": ["tier:4", 42],  # int is not a string
        "manifest": {
            "llm": {"provider": "x", "model": "y"},
            "capabilities": {},
        },
    }
    (tmp_path / "bad.json").write_text(json.dumps(p))
    with pytest.raises(ValueError, match="tags\\[1\\]"):
        load_preset(tmp_path, "bad")


def test_preset_tags_returns_empty_for_missing_field():
    """No tags field → []."""
    assert preset_tags({"name": "x"}) == []


def test_preset_tags_filters_non_strings_defensively():
    """If a hand-written preset slips a non-string past the validator,
    preset_tags filters it out rather than returning a mixed list."""
    assert preset_tags({"tags": ["tier:4", 42, "specialty:code"]}) == [
        "tier:4", "specialty:code"
    ]


def test_preset_tier_returns_canonical_value():
    """preset_tier extracts the value of the first tier:* tag."""
    assert preset_tier({"tags": ["tier:4"]}) == "4"
    assert preset_tier({"tags": ["specialty:code", "tier:1"]}) == "1"


def test_preset_tier_returns_none_when_unset():
    """No tier:* tag → None."""
    assert preset_tier({"tags": []}) is None
    assert preset_tier({"tags": ["specialty:code"]}) is None
    assert preset_tier({}) is None


def test_tier_vocabulary_is_numeric_one_through_five():
    """The shipped tier ladder is a five-rung star scale stored as plain
    numeric strings; higher is better. The TUI renders these as star icons."""
    assert TIER_VALUES == ("1", "2", "3", "4", "5")
    assert TIER_TAGS == ("tier:1", "tier:2", "tier:3", "tier:4", "tier:5")
