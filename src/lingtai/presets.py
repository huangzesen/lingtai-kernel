"""Preset library — atomic {llm, capabilities} bundles for agent runtime swap.

A preset lives as a single JSON or JSONC file in `manifest.presets_path`. The
filename stem is the preset's discovery name (e.g. `cheap.json` → `"cheap"`).
The kernel reads the file's `manifest.llm` and `manifest.capabilities` and
substitutes them into the running agent's manifest at boot time or on swap.

This module owns:
- `discover_presets`: list available presets in a folder
- `load_preset`: read + validate one preset
- `expand_inherit`: resolve `"provider": "inherit"` sentinels against main LLM
- `default_presets_path`: the TUI's per-machine library at ~/.lingtai-tui/presets/
- `resolve_presets_path`: resolve manifest.presets_path against working_dir

The TUI's existing schema is `{name, description, manifest: {...}}`. The kernel
adopts it unchanged. The `description` field may be a plain string or a
structured object — both are surfaced verbatim to the agent.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def default_presets_path() -> Path:
    """The TUI's per-machine preset library."""
    return Path.home() / ".lingtai-tui" / "presets"


def resolve_presets_path(manifest: dict, working_dir: Path) -> Path:
    """Resolve manifest.presets_path against working_dir, defaulting to
    ~/.lingtai-tui/presets/ when unset.

    Relative paths are resolved against working_dir (not the process CWD)
    so an agent's library reference remains valid regardless of where the
    process was launched.

    Empty-string and missing both fall back to the default; the difference
    (silent fallback for missing, warning-eligible for empty string) is the
    caller's concern — _read_init emits the warning before calling this.
    """
    presets_path_str = manifest.get("presets_path")
    if presets_path_str:
        p = Path(presets_path_str).expanduser()
        return p if p.is_absolute() else (working_dir / p).resolve()
    return default_presets_path()


def discover_presets(presets_path: Path | str) -> dict[str, Path]:
    """Return a mapping of preset name → file path for top-level *.json[c] files.

    The preset name is the filename stem (e.g. `cheap.json` → `"cheap"`).
    Subdirectories and non-JSON files are ignored. A nonexistent directory
    returns an empty dict (not an error).
    """
    p = Path(presets_path)
    if not p.is_dir():
        return {}
    out: dict[str, Path] = {}
    for entry in p.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix not in (".json", ".jsonc"):
            continue
        if entry.stem in out:
            log.warning(
                "preset stem collision: %r and %r — load_preset will use .json first",
                str(out[entry.stem]), str(entry),
            )
            # If we already have a .json entry, keep it. Otherwise the new one wins.
            if out[entry.stem].suffix == ".json":
                continue
        out[entry.stem] = entry
    return out


def load_preset(presets_path: Path | str, name: str) -> dict:
    """Load and validate a single preset by name.

    Args:
        presets_path: directory containing the preset files
        name: the preset's filename stem (without extension)

    Returns:
        The parsed preset dict with shape {name, description, manifest: {...}}.

    Raises:
        KeyError: the preset file does not exist
        ValueError: the file is malformed or missing required fields
    """
    from .config_resolve import load_jsonc

    presets_path = Path(presets_path)
    candidates = [presets_path / f"{name}.json", presets_path / f"{name}.jsonc"]
    file_path = next((c for c in candidates if c.is_file()), None)
    if file_path is None:
        raise KeyError(f"preset not found: {name!r} in {presets_path}")

    try:
        data = load_jsonc(file_path)
    except Exception as e:
        raise ValueError(f"failed to parse preset {name!r} ({file_path}): {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"preset {name!r} ({file_path}): expected object, got {type(data).__name__}")

    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError(f"preset {name!r} ({file_path}): missing or invalid 'manifest' object")

    llm = manifest.get("llm")
    if not isinstance(llm, dict):
        raise ValueError(f"preset {name!r} ({file_path}): missing or invalid 'manifest.llm' object")

    if not llm.get("provider") or not llm.get("model"):
        raise ValueError(f"preset {name!r} ({file_path}): manifest.llm requires non-empty 'provider' and 'model'")

    caps = manifest.get("capabilities", {})
    if not isinstance(caps, dict):
        raise ValueError(f"preset {name!r} ({file_path}): manifest.capabilities must be an object")

    internal_name = data.get("name")
    if internal_name and internal_name != name:
        log.warning("preset name mismatch: file %s has internal name %r", file_path, internal_name)

    return data


def expand_inherit(capabilities: dict, main_llm: dict) -> dict:
    """Resolve `"provider": "inherit"` sentinels in capability configs.

    For each capability whose kwargs has `provider == "inherit"`, replace it
    with the main LLM's provider plus its credentials (api_key, api_key_env,
    base_url). The `model` field is NOT inherited — capabilities pick their
    own model independently.

    Mutates `capabilities` in place. Returns the same dict for convenience.
    """
    for cap_name, kwargs in capabilities.items():
        if not isinstance(kwargs, dict):
            continue
        if kwargs.get("provider") != "inherit":
            continue
        kwargs["provider"]    = main_llm.get("provider")
        kwargs["api_key"]     = main_llm.get("api_key")
        kwargs["api_key_env"] = main_llm.get("api_key_env")
        kwargs["base_url"]    = main_llm.get("base_url")
    return capabilities
