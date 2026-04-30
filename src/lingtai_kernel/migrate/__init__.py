"""Kernel-side preset library migrations.

Per-machine analogue of `tui/internal/globalmigrate` in the TUI repo:
versioned, append-only, forward-only migrations applied to a preset
library directory (typically `~/.lingtai-tui/presets/`, or any directory
that holds preset *.json files referenced by agents'
`manifest.preset.allowed` lists).

The version number is tracked in `<presets_dir>/_kernel_meta.json`.
Migrations run lazily — `lingtai.presets.discover_presets_in_dirs`
invokes `run_migrations(presets_path)` before listing files, and
`load_preset` runs them on the file's parent directory before reading.
Both paths are guarded by a process-level cache so each directory is
migrated at most once per run.

Conventions:
- Append-only ordered slice in `migrate.py`.
- Each migration lives in `m<NNN>_<name>.py` and exports a
  `migrate_<name>(presets_path: Path) -> None` function.
- Failures are reported via `logging.warning` and abort the run for
  that path (no partial advancement of the version counter).
"""
from __future__ import annotations

from .migrate import CURRENT_VERSION, run_migrations

__all__ = ["CURRENT_VERSION", "run_migrations"]
