"""Versioned migration runner for the kernel preset library.

Mirrors `tui/internal/globalmigrate` from the TUI repo. The TUI runs its
analogue once at process start against `~/.lingtai-tui/`; the kernel runs
this once per `presets_path` per process, triggered lazily from
`lingtai.presets.discover_presets`.

Append-only registry. Each migration claims a strictly-increasing version
number. The on-disk version counter (in `<presets_dir>/_kernel_meta.json`)
records the highest migration that has run successfully against this
directory. A migration runs at most once per directory; when its version
number ≤ the on-disk counter, it is skipped.

Best-practice invariants:
- Versions form a contiguous strictly-increasing sequence (1, 2, 3, ...).
  The runner asserts this at import time so a typo in the registry fails
  fast rather than silently mis-ordering migrations.
- `CURRENT_VERSION` is derived from the registry — there is no
  hand-maintained constant that can drift out of sync with the migrations
  that are actually registered.
- Forward-only: a meta file with a future version (e.g. from a newer
  kernel that was later downgraded) is honored as-is and never rolled
  back. A warning is logged so the operator knows.
- Tmp-file writes use a PID suffix so concurrent processes (parent +
  avatar) sharing the same presets directory do not clobber each other's
  in-flight write.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from .m001_context_limit_relocation import migrate_context_limit_relocation
from .m002_description_object import migrate_description_object

log = logging.getLogger(__name__)

# Filename used to track migration state. Lives inside the presets directory
# itself so each preset library carries its own migration state. The leading
# underscore signals "internal" to humans browsing the directory;
# discover_presets_in_dirs explicitly skips this filename.
_META_FILENAME = "_kernel_meta.json"

# Per-process guard so we run at most once per (presets_path) per process.
# Keyed by resolved absolute path string.
_migrated: set[str] = set()


# Append-only registry. Each entry: (version, name, function).
# Versions MUST form a strictly-increasing contiguous sequence starting at 1.
# The validator below catches violations at import time.
_MIGRATIONS: tuple[tuple[int, str, Callable[[Path], None]], ...] = (
    (1, "context_limit_relocation", migrate_context_limit_relocation),
    (2, "description_object", migrate_description_object),
)


def _validate_registry() -> int:
    """Sanity-check the registry shape at import time.

    Returns the highest registered version, which becomes CURRENT_VERSION.
    Raises RuntimeError if the registry violates contiguity, ordering, or
    uniqueness — these are programmer errors that should fail loudly long
    before a migration ever runs against user data.
    """
    if not _MIGRATIONS:
        return 0
    seen: set[int] = set()
    expected = 1
    for entry in _MIGRATIONS:
        if not (isinstance(entry, tuple) and len(entry) == 3):
            raise RuntimeError(
                f"kernel migrate registry: malformed entry {entry!r} — "
                f"expected (version, name, function)"
            )
        version, name, fn = entry
        if not isinstance(version, int) or version <= 0:
            raise RuntimeError(
                f"kernel migrate registry: version must be a positive int, got {version!r} "
                f"(in {name!r})"
            )
        if version in seen:
            raise RuntimeError(
                f"kernel migrate registry: duplicate version {version} (in {name!r})"
            )
        if version != expected:
            raise RuntimeError(
                f"kernel migrate registry: expected version {expected}, got {version} "
                f"(in {name!r}) — versions must be strictly increasing and contiguous"
            )
        if not callable(fn):
            raise RuntimeError(
                f"kernel migrate registry: function for version {version} ({name!r}) is not callable"
            )
        seen.add(version)
        expected += 1
    return _MIGRATIONS[-1][0]


CURRENT_VERSION: int = _validate_registry()


def meta_filename() -> str:
    """The filename `discover_presets` must skip when listing presets."""
    return _META_FILENAME


def _load_version(presets_path: Path) -> int:
    """Read the on-disk version counter. Returns 0 when missing or unreadable."""
    meta_path = presets_path / _META_FILENAME
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except OSError as e:
        log.warning("kernel migrate: failed to read %s: %s", meta_path, e)
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("kernel migrate: malformed %s: %s — treating as version 0",
                    meta_path, e)
        return 0
    v = data.get("version", 0)
    return v if isinstance(v, int) else 0


def _save_version(presets_path: Path, version: int) -> None:
    """Atomically persist the version counter.

    The tmp file uses a PID suffix so concurrent processes sharing this
    directory cannot clobber each other's in-flight write. os.replace is
    atomic on POSIX and Windows for same-filesystem renames.
    """
    meta_path = presets_path / _META_FILENAME
    tmp = meta_path.with_name(f"{_META_FILENAME}.{os.getpid()}.tmp")
    payload = json.dumps({"version": version}, ensure_ascii=False)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(meta_path))
    except OSError as e:
        log.warning("kernel migrate: failed to write %s: %s", meta_path, e)
        # Best-effort cleanup so we don't leave orphan tmp files behind.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def run_migrations(presets_path: Path | str) -> None:
    """Run pending kernel migrations against the given presets directory.

    Idempotent and process-cached: subsequent calls in the same process for
    the same path are no-ops. Reads the current version from
    `<presets_path>/_kernel_meta.json` (defaulting to 0), runs all
    registered migrations whose version is greater than the current value,
    and persists the new version after each successful step.

    Failures in individual migrations log a warning and abort the run for
    this path (no partial version advancement past the failed step).
    Subsequent process starts will retry from the last-successful version.

    A nonexistent presets directory is a no-op — there's nothing to
    migrate, and we don't want to create the directory implicitly.

    Forward-only: a meta file with a version greater than CURRENT_VERSION
    (e.g. written by a newer kernel that was later downgraded) is honored
    as-is. A warning is logged so the operator notices.
    """
    p = Path(presets_path)
    try:
        resolved_key = str(p.resolve())
    except OSError:
        return  # path doesn't resolve — nothing to do
    if resolved_key in _migrated:
        return
    if not p.is_dir():
        _migrated.add(resolved_key)
        return

    current = _load_version(p)

    if current > CURRENT_VERSION:
        log.warning(
            "kernel migrate: %s reports version %d but this kernel only knows up to %d "
            "— honoring on-disk version, running no migrations (likely a downgrade)",
            p, current, CURRENT_VERSION,
        )
        _migrated.add(resolved_key)
        return

    if current == CURRENT_VERSION:
        _migrated.add(resolved_key)
        return

    for version, name, fn in _MIGRATIONS:
        if version <= current:
            continue
        try:
            fn(p)
        except Exception as e:
            log.warning(
                "kernel migrate %d (%s) failed for %s: %s — aborting run, will retry next launch",
                version, name, p, e,
            )
            return
        current = version
        _save_version(p, current)

    _migrated.add(resolved_key)


def reset_process_cache() -> None:
    """Clear the per-process migration guard.

    Test-only — not part of the public API. Useful when a test needs to
    re-run migrations against a freshly-built fixture inside the same
    process.
    """
    _migrated.clear()
