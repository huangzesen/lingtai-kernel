"""Preset library — atomic {llm, capabilities} bundles for agent runtime swap.

A preset lives as a single JSON or JSONC file in `manifest.preset.path` (or
the default `~/.lingtai-tui/presets/` when path is omitted). The filename
stem is the preset's discovery name (e.g. `cheap.json` → `"cheap"`). The
kernel reads the file's `manifest.llm` and `manifest.capabilities` and
substitutes them into the running agent's manifest at boot time or on swap.

`manifest.preset.path` may be a single string or a list of strings. With a
list, libraries layer in order — the first path that contains a given preset
name wins, and `discover_presets` unions across all paths (with collisions
resolved first-path-wins). When `path` is omitted or empty, the kernel falls
back to the per-machine default library at ~/.lingtai-tui/presets/.

This module owns:
- `discover_presets`: list available presets across one or more folders
- `load_preset`: read + validate one preset (searches paths in order)
- `expand_inherit`: resolve `"provider": "inherit"` sentinels against main LLM
- `default_presets_path`: the TUI's per-machine library at ~/.lingtai-tui/presets/
- `resolve_presets_path`: resolve manifest.preset.path against working_dir
  (always returns a list[Path], even for the singleton case)

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


def resolve_presets_path(manifest: dict, working_dir: Path) -> list[Path]:
    """Resolve manifest.preset.path against working_dir.

    Returns a list[Path] in declared order. The schema accepts either a
    single string or a list of strings; both are normalized to a list here.
    When the umbrella is absent or path is missing/empty (None, "", []),
    falls back to ``[default_presets_path()]``.

    Relative paths are resolved against working_dir (not the process CWD)
    so an agent's library reference remains valid regardless of where the
    process was launched.
    """
    preset_block = manifest.get("preset") or {}
    raw = preset_block.get("path") if isinstance(preset_block, dict) else None

    if isinstance(raw, str):
        entries: list[str] = [raw] if raw else []
    elif isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, str) and e]
    else:
        entries = []

    if not entries:
        return [default_presets_path()]

    resolved: list[Path] = []
    for entry in entries:
        p = Path(entry).expanduser()
        resolved.append(p if p.is_absolute() else (working_dir / p).resolve())
    return resolved


def _normalize_paths(presets_path: Path | str | list[Path | str]) -> list[Path]:
    """Coerce the discover/load argument to a list[Path].

    Accepts a single Path/str or a list of Path/str. Empty list returns [].
    """
    if isinstance(presets_path, (str, Path)):
        return [Path(presets_path)]
    return [Path(p) for p in presets_path]


def discover_presets(
    presets_path: Path | str | list[Path | str],
) -> dict[str, Path]:
    """Return a mapping of preset name → file path for top-level *.json[c] files.

    Accepts a single directory or a list of directories. With a list, paths
    are scanned in order and unioned; on name collision the earlier path
    wins (and a warning is logged). Nonexistent directories in the list are
    silently skipped — they're not an error.

    The preset name is the filename stem (e.g. `cheap.json` → `"cheap"`).
    Subdirectories and non-JSON files are ignored.

    Triggers any pending kernel-side preset migrations against each path
    before listing — see lingtai_kernel.migrate. Migrations are idempotent
    and process-cached, so repeated calls share the work.
    """
    from lingtai_kernel.migrate import run_migrations
    from lingtai_kernel.migrate.migrate import meta_filename

    paths = _normalize_paths(presets_path)
    skip = meta_filename()
    out: dict[str, Path] = {}

    for p in paths:
        if not p.is_dir():
            continue
        run_migrations(p)
        for entry in p.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix not in (".json", ".jsonc"):
                continue
            if entry.name == skip:
                continue
            if entry.stem in out:
                existing = out[entry.stem]
                if existing.parent != entry.parent:
                    log.warning(
                        "preset name %r found in multiple libraries: %r (used) and %r (shadowed)",
                        entry.stem, str(existing), str(entry),
                    )
                    # Earlier path wins; skip this entry.
                    continue
                log.warning(
                    "preset stem collision: %r and %r — load_preset will use .json first",
                    str(existing), str(entry),
                )
                # If we already have a .json entry, keep it. Otherwise the new one wins.
                if existing.suffix == ".json":
                    continue
            out[entry.stem] = entry
    return out


def load_preset(
    presets_path: Path | str | list[Path | str],
    name: str,
) -> dict:
    """Load and validate a single preset by name.

    Args:
        presets_path: directory or list of directories to search. With a
            list, paths are tried in order — the first path containing the
            preset wins. Missing directories in the list are skipped.
        name: the preset's filename stem (without extension)

    Returns:
        The parsed preset dict with shape {name, description, manifest: {...}}.

    Raises:
        KeyError: the preset file does not exist in any path
        ValueError: the file is malformed or missing required fields
    """
    from .config_resolve import load_jsonc
    from lingtai_kernel.migrate import run_migrations

    paths = _normalize_paths(presets_path)

    # Search in order. Run kernel migrations on each existing path so
    # legacy on-disk shapes are normalized before validation. This is
    # idempotent and process-cached (shared with discover_presets).
    file_path: Path | None = None
    matched_root: Path | None = None
    for p in paths:
        if p.is_dir():
            run_migrations(p)
        for ext in ("json", "jsonc"):
            candidate = p / f"{name}.{ext}"
            if candidate.is_file():
                file_path = candidate
                matched_root = p
                break
        if file_path is not None:
            break

    if file_path is None or matched_root is None:
        raise KeyError(f"preset not found: {name!r} in {[str(p) for p in paths]}")

    # Boundary check: refuse names that escape the matched library.
    # An agent passing "../../../etc/passwd" would otherwise let load_preset
    # follow that path out of the library.
    try:
        resolved = file_path.resolve()
        root = matched_root.resolve()
    except OSError as e:
        raise KeyError(f"preset path resolution failed for {name!r}: {e}") from e
    if not str(resolved).startswith(str(root) + "/") and resolved.parent != root:
        raise KeyError(
            f"preset name {name!r} escapes presets directory {matched_root}"
        )

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

    # context_limit is a property of the model and lives inside manifest.llm.
    # The kernel migration system relocated any old root-level placements
    # before we got here (see lingtai_kernel.migrate.m001), so the only
    # presets that still have it at the root are ambiguous (both locations
    # set, which the migration explicitly skips) or hand-edited regressions.
    # Reject either case with a pointed error.
    if "context_limit" in manifest:
        raise ValueError(
            f"preset {name!r} ({file_path}): context_limit must live inside "
            f"manifest.llm, not at manifest root — move it under llm and retry"
        )
    ctx_limit = llm.get("context_limit")
    if ctx_limit is not None and not isinstance(ctx_limit, int):
        raise ValueError(
            f"preset {name!r} ({file_path}): context_limit must be an integer (got {type(ctx_limit).__name__})"
        )

    caps = manifest.get("capabilities", {})
    if not isinstance(caps, dict):
        raise ValueError(f"preset {name!r} ({file_path}): manifest.capabilities must be an object")

    # Optional top-level `tags` field — list of namespaced strings like
    # "tier:opus" or "specialty:code". Used by agents (and the TUI) to
    # pick presets by category. The first namespace shipped is "tier:",
    # whose vocabulary is mythos|opus|sonnet|haiku|freebie — see
    # `valid_tier_tags()` and the procedures.md daemon-selection guidance.
    tags = data.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError(
            f"preset {name!r} ({file_path}): 'tags' must be a list of strings"
        )
    for i, tag in enumerate(tags):
        if not isinstance(tag, str):
            raise ValueError(
                f"preset {name!r} ({file_path}): tags[{i}] must be a string (got {type(tag).__name__})"
            )

    internal_name = data.get("name")
    if internal_name and internal_name != name:
        log.warning("preset name mismatch: file %s has internal name %r", file_path, internal_name)

    return data


# ---------------------------------------------------------------------------
# Tag taxonomy
# ---------------------------------------------------------------------------
#
# Tags are namespaced strings stored as a top-level list on each preset:
#
#     "tags": ["tier:4", "specialty:code"]
#
# The first namespace introduced is `tier:`, a five-rung cost/quality ladder
# stored as plain numeric strings 1..5 — higher is better. The TUI renders
# these as star icons (★ through ★★★★★). Agents read tags via
# `system(action='presets')` and pick presets accordingly; the default
# procedures.md gives guidance for daemon-spawn decisions.
#
# Future namespaces (specialty, modality, context-class) follow the same
# `<namespace>:<value>` pattern so callers can filter by `t.startswith("X:")`.
TIER_NAMESPACE = "tier"
TIER_VALUES = ("1", "2", "3", "4", "5")
TIER_TAGS = tuple(f"{TIER_NAMESPACE}:{v}" for v in TIER_VALUES)


def preset_tags(preset: dict) -> list[str]:
    """Return the preset's tags list (or [] when unset)."""
    if not isinstance(preset, dict):
        return []
    tags = preset.get("tags")
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, str)]


def preset_tier(preset: dict) -> str | None:
    """Return the preset's tier value (e.g. 'opus') or None.

    Reads the first `tier:*` tag — multiple tier tags on one preset is
    nonsensical, so the helper trusts the first.
    """
    for t in preset_tags(preset):
        if t.startswith(f"{TIER_NAMESPACE}:"):
            return t.split(":", 1)[1]
    return None


def preset_context_limit(preset_manifest: dict) -> int | None:
    """Return the preset's context_limit (lives inside manifest.llm).

    context_limit is a property of the model, so it's stored next to
    provider/model. Returns None when unset.

    For presets read via `load_preset`, the kernel migration system has
    already relocated any legacy root-level placements before validation —
    so by the time this helper is called, only the canonical location is
    populated.
    """
    if not isinstance(preset_manifest, dict):
        return None
    llm = preset_manifest.get("llm")
    if isinstance(llm, dict):
        return llm.get("context_limit")
    return None


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
