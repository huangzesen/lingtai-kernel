---
product: kernel
release_version: "1.0.5"
release_tag: "v1.0.5"
migration: manual
refresh_required: true
related_files:
  - RELEASING.md
  - pyproject.toml
  - src/lingtai/kernel/nudge/kernel_version.py
  - src/lingtai/kernel/release_manifest.py
  - .github/workflows/wheels.yml
  - src/lingtai/intrinsic_skills/system-manual/reference/runtime-update-checks/SKILL.md
maintenance: |
  Replace this document for every kernel release. Keep the stable repository
  path migration/migration.md; Git commits and release tags preserve the older
  per-release versions. Never append a second release history here or invent a
  version that disagrees with package metadata.
---
# LingTai kernel 1.0.5 migration

## Applies when

The target kernel release is `1.0.5` / tag `v1.0.5` and that tag lies in the
open update interval `(current, target]`.

## Conditional migration

If an existing `init.json` has
`manifest.capabilities.daemon.max_emanations`, daemon capability setup can be
skipped after upgrade because `max_emanations` was removed before `1.0.5`.
The configuration owner must choose explicitly:

1. Remove `max_emanations` and accept the current default
   `manager_pool_size=100`.
2. Remove `max_emanations` and deliberately set an appropriate
   `manager_pool_size`.

These controls are not a 1:1 mapping; no automatic conversion exists. If this
legacy key is absent, leave existing configuration unchanged.

Before mutation, confirm that the installer selected the intended
`LINGTAI_RUNTIME_PYTHON`. If more than one LingTai runtime is present, rerun with
the explicit interpreter shown by the installer rather than guessing. The update
must use a prebuilt wheel named and hashed by the exact
`lingtai.kernel.release/v1` manifest. Do not build or install the sdist on the
user machine, and do not use PyPI metadata to choose the release version.

## Validate

- Confirm this document identifies the intended kernel release as `1.0.5` /
  `v1.0.5`.
- If the legacy key is present, verify it was removed and that the selected
  `manager_pool_size` choice is intentional.
- Verify `lingtai.__version__`, `lingtai.__file__`, and
  `lingtai.kernel.__file__` from the selected runtime interpreter after install.
- If the product, version, tag, stable path, mirror content, or artifact hash does
  not match, stop rather than borrowing a TUI migration or another release's
  document.

## Refresh

The verified wheel changes bytes on disk but a running agent still has the old
code loaded. After active work is checkpointed and refresh is authorized, call
`system(action='refresh')` and verify the new process uses the selected
interpreter and reports `1.0.5`.
