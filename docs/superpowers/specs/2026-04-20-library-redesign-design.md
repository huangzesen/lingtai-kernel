# Library Redesign — Internalized, Tiered, Extensible

**Date:** 2026-04-20
**Status:** Design (pre-implementation)
**Scope:** `lingtai-kernel` library capability + `lingtai` (TUI) default path injection

## 1. Motivation

Today the library capability treats `.library/` as a network-shared folder (`agent._working_dir.parent / ".library"`). There is no per-agent library, no kernel-shipped intrinsic skills, no extensibility surface. The TUI works around this by populating the single shared `.library/` with symlinks from four sources (intrinsic, active recipe, custom recipe, agora networks), conflating network-level and deployment-level concerns.

This redesign:

1. Internalizes the library per agent (`<agent>/.library/`).
2. Ships a small set of **intrinsic skills** with the kernel package, hard-copied into every agent's library on setup so that every agent has a reliable meta-skill (`skill-for-skill`) teaching the library workflow.
3. Keeps a **network-shared library** (`../.library_shared/`) as the collective knowledge base of the network.
4. Makes the library extensible via **additional paths** declared in `init.json` as Tier 1 (always-injected into the prompt catalog). Curation is intentional: users and admins choose what enters the network (by authoring into `custom/` or adding to `.library_shared/`), not by pointing at external corpora.
5. Eliminates tool-surface bloat: the library capability has **one action (`info`)** that returns the canonical teaching plus a runtime snapshot. Everything else — authoring, publishing, loading, exploration — is done via existing tools (`read`/`write`/`edit`/`bash`/`pad.append`/`system.refresh`).

## 2. Architectural Overview

Two tiers plus one authoring area:

| Tier | Source | In prompt catalog? | Mechanism |
|---|---|---|---|
| **0 — Intrinsic** | kernel package `src/lingtai_kernel/intrinsic_skills/` | Yes | Hard-copied into `<agent>/.library/intrinsic/` on every setup |
| **1 — Paths** (always-injected) | `init.json` `manifest.capabilities.library.paths` | Yes | Scanner reads declared dirs in place |
| — **Agent-authored** | `<agent>/.library/custom/` | Yes | Agent writes SKILL.md directly; auto-scanned |

### Per-agent disk layout

```
<agent>/.library/
├── intrinsic/        ← Tier 0: hard-copied from kernel; always rewritten on setup
│   └── skill-for-skill/
│       └── SKILL.md  ← canonical teaching for the library workflow
└── custom/           ← agent-authored skills (scanned automatically)
    └── <skill-name>/
        └── SKILL.md
```

`intrinsic/` is overwritten on every setup, so kernel upgrades propagate updated intrinsic skills automatically. `custom/` is the agent's own territory and is never touched by the capability.

Tier 1 paths are **not** copied or symlinked into `.library/` — the scanner reads them in place. This keeps `.library/` strictly the agent's own territory.

### Default seeds

On capability setup, `init.json` (if freshly generated) will contain defaults:

```json
"library": {
  "paths": ["../.library_shared", "~/.lingtai-tui/utilities"]
}
```

- `../.library_shared` — the network-shared library (resolves to network root from agent working dir).
- `~/.lingtai-tui/utilities` — the TUI's utility skills directory (operational skills for every agent in this deployment).

**`init.json` is the ground truth.** The capability reads `library.paths` on every setup and uses exactly that list — no runtime state, no merging, no preservation. To change paths permanently: edit `init.json`, then `system.refresh`. To restore defaults: delete `library.paths` (or re-manifest) and the two defaults are re-seeded.

### Path resolution rules

- Absolute paths used as-is.
- Relative paths resolve against the **agent working directory**, regardless of which file declares them.
- `~/` expansion honored.

Example: `../.library_shared` → `<network-root>/.library_shared`.

## 3. Library Capability Behavior

### Tool surface: one action

**`info`** — returns the canonical teaching plus a runtime health snapshot. Serves two roles: signposting (visible handle in the tool list) and **semi-health check** (end-to-end verification that the library is wired up correctly).

```json
{"action": "info"}
```

Returns on healthy library:
```json
{
  "status": "ok",
  "skill_for_skill": "<full body of .library/intrinsic/skill-for-skill/SKILL.md>",
  "library_dir": "/path/to/<agent>/.library",
  "catalog_size": 42,
  "paths": {
    "../.library_shared": {"resolved": "/abs/path", "exists": true, "skills": 12},
    "~/.lingtai-tui/utilities": {"resolved": "/abs/path", "exists": true, "skills": 8}
  },
  "problems": []
}
```

Returns on degraded library (e.g., intrinsic missing, path unresolvable):
```json
{
  "status": "degraded",
  "error": "skill-for-skill SKILL.md missing — hard-copy may have failed",
  "library_dir": "/path/to/<agent>/.library",
  "catalog_size": 0,
  "paths": {"../.library_shared": {"resolved": "...", "exists": false, "skills": 0}},
  "problems": [{"folder": "custom/broken", "reason": "invalid frontmatter"}]
}
```

The action is the **only** thing that reads the meta-skill's SKILL.md from disk at call time — so a successful call proves (a) `.library/intrinsic/skill-for-skill/SKILL.md` exists and is readable, (b) the capability's handler is wired, (c) path resolution works, (d) the scanner runs without crashing. That's meaningful end-to-end health coverage in one cheap call.

**Tool description** (short; teaching lives in the meta-skill):
> "Your library — a folder of skills (SKILL.md files). Your skill catalog is always visible in your system prompt. Call `info` for the full workflow (finding, reading, loading, authoring, publishing) and to verify your library is healthy."

The tool's purpose is twofold: **signposting** (first-class handle in the tool list pointing at the meta-skill) and **health verification** (reading the meta-skill file + runtime snapshot exercises the library's core invariants). The `info` action is deliberately redundant with directly `read`ing the meta-skill's SKILL.md — that's convergent signposting — and also serves as a canary: if `info` succeeds, the library is correctly set up.

### Silent setup work

On `setup()` and on every `system.refresh` (which re-runs setup):

1. **Ensure directories exist**: `<agent>/.library/{intrinsic,custom}/`.
2. **Hard-copy intrinsics**: copy every skill folder under `src/lingtai_kernel/intrinsic_skills/` into `<agent>/.library/intrinsic/`, overwriting existing contents. This guarantees kernel upgrades propagate.
3. **Resolve Tier 1 paths**: read `init.json` `manifest.capabilities.library.paths`; expand `~/` and relative forms; log a warning for any path that doesn't resolve to a directory.
4. **Scan for catalog**: recursively scan `<agent>/.library/intrinsic/` + `<agent>/.library/custom/` + each Tier 1 path. Collect valid skills (folder with `SKILL.md` containing valid frontmatter `name` + `description`) and problems (missing frontmatter, etc.).
5. **Inject catalog**: build the `<available_skills>` XML and write it to the system prompt's `library` section via `agent.update_system_prompt("library", xml, protected=True)`.

No network calls, no git operations, no writes to init.json.

### Removal of git operations

The current capability `git init`s and commits to `.library/`. Time-veil already provides per-agent history at a richer granularity, so git-in-capability is redundant. **The new capability does no git work.** `.library/` is a plain directory.

## 4. Prompt Sections

### `library` section (existing, reshaped)

XML catalog of scanned skills (Tier 0 + Tier 1 + `.library/custom/`):

```xml
<available_skills>
  <skill>
    <name>skill-for-skill</name>
    <description>Canonical teaching for the library workflow. Read this first.</description>
    <location>/path/to/.library/intrinsic/skill-for-skill/SKILL.md</location>
  </skill>
  <skill>
    <name>data-export</name>
    <description>Export agent state to structured archives</description>
    <location>/path/to/.library_shared/data-export/SKILL.md</location>
  </skill>
  ...
</available_skills>
```

Skills with problems (invalid frontmatter, missing name, etc.) are logged but **not** injected into the catalog. They don't pollute the prompt.


## 5. Kernel Package Additions

### `src/lingtai_kernel/intrinsic_skills/`

New directory, shipped as package data. Initial contents:

- `skill-for-skill/SKILL.md` — the meta-skill.

Other intrinsics may be added later. Each one is a folder containing at minimum a valid `SKILL.md` (may also contain scripts/references/assets subfolders per the standard skill layout).

### `pyproject.toml`

Add `intrinsic_skills/**/*` to package data so `pip install lingtai` bundles the intrinsic skill folders.

## 6. The `skill-for-skill` Meta-Skill

Hard-shipped at `src/lingtai_kernel/intrinsic_skills/skill-for-skill/SKILL.md`. Teaches:

**How the catalog works:**
- The `<available_skills>` XML in your system prompt lists every skill reachable in your catalog. Each entry has `name`, `description`, `location`.
- To read a skill's body: `read` the file at `<location>`.

**Loading a skill into active working memory:**
- Use `pad.append(files=["<location>"])` to pin the SKILL.md into your pad. This places the body in the cached system prompt prefix, making it active across turns and molts without repeated reads.
- Use `pad.append(files=[])` with an empty list (or omit an entry) to remove a pinned skill.

**Authoring a new skill:**
- Write `<agent>/.library/custom/<skill-name>/SKILL.md` with YAML frontmatter at the top:
  ```
  ---
  name: <skill-name>
  description: One-line description
  version: 1.0.0
  ---
  ```
- Required: `name`, `description`. Optional: `version`, `author`, `tags`.
- After authoring, call `system.refresh` to pick up the new skill into the catalog.

**Publishing to the shared library:**
- `cp -r .library/custom/<name> ../.library_shared/<name>` via bash.
- Do **not** overwrite an existing entry in `.library_shared/`. If the name collides, rename or consult the admin agent.

**Admin curation of `.library_shared/` (norm-enforced):**
- If you are the admin agent: edit, consolidate, rename, or remove entries in `.library_shared/` using `edit`/`write`/`rm` as needed.
- If you are not the admin agent: **do not modify** `.library_shared/` beyond adding new entries. Editing or removing is admin's stewardship.

**Adding a new library path:**
- Edit `init.json` under `manifest.capabilities.library.paths`. Every folder listed there is scanned and its skills appear in your catalog.
- Call `system.refresh` to apply.
- Adding paths is intentional curation — only add sources you trust. Libraries are part of who you are.

**Name collision discipline:**
- Skills across all tiers must have unique `name` values to appear correctly in the catalog. Duplicates are surfaced as problems (not injected).
- Before authoring a new skill in `custom/` or publishing to shared, check the existing catalog with `grep` to avoid collisions.

## 7. `init.json` Schema Additions

`manifest.capabilities.library` is validated (type: object) with one optional key:

```jsonc
"capabilities": {
  "library": {
    "paths": ["../.library_shared", "~/.lingtai-tui/utilities"]  // list[str], Tier 1
  }
}
```

`paths`:
- Type: list of strings.
- Paths may be absolute or relative (relative to agent working dir); `~/` expanded.
- **`init.json` is the ground truth.** The capability reads `library.paths` on every setup (both agent boot and `system.refresh`, since both call `_perform_refresh → _setup_from_init`) and uses exactly that list. There is no runtime state file, no merging, no preservation of prior runs. To change paths permanently: edit `init.json`, then call `system.refresh`.
- Re-manifestation (init-json regeneration, e.g., via TUI) writes the default `paths` set from scratch. If the user wants custom paths, they edit the written `init.json` and don't re-manifest — or they re-manifest and re-apply their edits.

`init_schema.py` `validate_init` gets a new check for the `library` subfield. Unknown keys under `library` emit warnings.

## 8. TUI Integration

### Default utilities path

The TUI ships a directory of utility skills at `~/.lingtai-tui/utilities/` (the path is written at TUI install time; the exact location is TBD but consistent across deployments). The TUI's init-json generator seeds `library.paths` with `~/.lingtai-tui/utilities` so every agent picks them up as Tier 1 automatically.

### Migration from current system

The current TUI code (`tui/internal/preset/recipe_library.go`) populates a single network-level `.library/` with symlinks from four sources. That code is **removed** in favor of:

1. TUI writes the utilities path into `init.json` on agent initialization.
2. TUI ensures `<network>/.library_shared/` exists on network init (creates it fresh for new networks; the migration below handles existing ones).
3. Recipe skills move into `~/.lingtai-tui/utilities/` (if they should be always-available) or remain network-authored in `.library_shared/` (if they're collective knowledge).

This is a breaking change for existing networks. A TUI migration (new migration file following the `tui/internal/migrate/` pattern) handles:
- Renaming existing `.library/` to `.library_shared/` at the network level.
- Removing symlinks inside (TUI no longer manages those).
- Bumping `.lingtai/meta.json` version.

**Important** (per CLAUDE.md in `tui/`): the TUI and portal share the same meta.json version space but separate migration registries. When adding this migration to the TUI, also bump `CurrentVersion` in `portal/internal/migrate/migrate.go` and register a no-op stub.

## 9. Files to Modify

### `lingtai-kernel`

- **NEW** `src/lingtai_kernel/intrinsic_skills/skill-for-skill/SKILL.md` — meta-skill body.
- **NEW** `src/lingtai_kernel/intrinsic_skills/__init__.py` (empty, for package-data inclusion).
- **`pyproject.toml`** — add `intrinsic_skills/**/*` to package data / `package-data`.
- **`src/lingtai/capabilities/library.py`** — substantial rewrite:
  - Remove git operations (`_ensure_git_repo`, `_git`, `_action_register` git pieces).
  - Remove `register`/`refresh` actions; replace with single `info` action.
  - Change `_resolve_library_dir` to return `agent._working_dir / ".library"` (not `parent / ".library"`).
  - Add `_hard_copy_intrinsics` that copies from the kernel package's intrinsic_skills dir into `<agent>/.library/intrinsic/`.
  - Add `_resolve_paths_from_init` that reads `manifest.capabilities.library.paths` via the existing config surface.
  - `setup()` does: ensure dirs → hard-copy intrinsics → resolve Tier 1 paths from init.json → scan catalog → inject catalog XML.
- **`src/lingtai/init_schema.py`** — validate new `manifest.capabilities.library` schema.
- **`src/lingtai/i18n/{en,zh,wen}.json`** — new keys: `library.description` (updated), `library.action_info`, `library.preamble` (updated). Remove obsolete `library.action` (it was the register/refresh enum description).

### `lingtai` (TUI)

- **REMOVE** `tui/internal/preset/recipe_library.go` symlink-population logic.
- **NEW** `tui/internal/migrate/m<NNN>_library_split.go` — renames `.library/` → `.library_shared/`, removes old symlinks. Bump `CurrentVersion` and register.
- **NEW** `portal/internal/migrate/` — no-op stub for the same migration version, bump `CurrentVersion` there too.
- **`tui/internal/tui/library.go`** — update scanner to reflect the new per-agent `.library/` structure (the viewer already recurses; main change is which path is passed in).
- **`tui/internal/preset/skills/`** — skills here fall into two buckets:
  - TUI-shipped utilities → install to `~/.lingtai-tui/utilities/`, reachable via init.json seed.
  - Network-level (if any) → move to `.library_shared/` at network init.
  - Recipe-specific skills stay with their recipes and are picked up via additional Tier 1 paths if a recipe declares them (out of scope for this design).
- **Init generation** — write the `library` section into new init.json files with the two default paths.

### Tests (`lingtai-kernel/tests/`)

- `test_library.py` (new or rewrite): cover hard-copy of intrinsics, scan of all tiers, injection into correct prompt sections, no-git behavior, additive re-manifest, path resolution rules, `info` action return shape.

## 10. Non-Goals (Deliberately Excluded)

- **Semantic search / `query` tool** — the agent uses `read`/`grep`/`ls` on Tier 2 reference corpora. No embedding store, no FTS index.
- **`preview`, `manage`, `register`, `unregister`, `promote` actions** — all covered by existing tools or norm-enforced file operations.
- **Auto-loading into pad** — loading is an explicit choice the agent makes via `pad.append`. The library capability never writes to the pad.
- **Reference corpora / Tier 2** — external skill corpora surfaced for the agent to browse but not auto-catalogued. Intentionally excluded: adding a large external arsenal does more harm than good because curation should be a deliberate human act. Users who find valuable skills add them to `.library_shared/` or author them into `.library/custom/`. Future extension can reintroduce a reference surface if a concrete need emerges.
- **Remote skill repos / clone-on-demand** — Tier 1 paths are local. Future extension can add git-clone or HTTP-fetch handling without touching this design.
- **Tier-level permissions / ACLs on `.library_shared/`** — norm-enforced by meta-skill description. Non-admin agents are told "don't edit shared." This is sufficient at current network scale.

## 11. Open Questions / Deferred Decisions

- **Exact location of TUI utilities dir** (`~/.lingtai-tui/utilities/` vs. a Homebrew/pipx-aware path). Decided during TUI migration PR.
- **List of intrinsic skills** beyond `skill-for-skill`. Starts with one; candidates for later (`library-debugging`, `pad-workflow`) evaluated when concrete need arises.
- **TUI utility skills content** — which of the current `tui/internal/preset/skills/` entries move to `~/.lingtai-tui/utilities/` vs. stay recipe-bound. Covered in TUI migration PR.

## 12. Migration / Rollout

1. Ship kernel changes with a new minor version.
2. Ship TUI changes with a new minor version that bumps meta.json version and includes the migration.
3. Existing agents with a `<network>/.library/` directory: TUI migration renames to `.library_shared/`, strips symlinks. The new library capability then creates each agent's `.library/intrinsic/` and `.library/custom/` on first boot.
4. No backwards compatibility for the old capability actions (`register`, `refresh`) — they're removed. If an agent's system prompt references them in pad content, the agent will get a tool-not-found error if it tries to call them, which the meta-skill will teach it to handle.

## 13. Validation

After implementation, the design is correct if:

- A fresh agent boots with `<agent>/.library/intrinsic/skill-for-skill/SKILL.md` present.
- `skill-for-skill` appears in the `<available_skills>` XML catalog.
- `library({"action": "info"})` returns `status: "ok"` with the SKILL.md body and runtime snapshot. Breaking an invariant (delete `.library/intrinsic/skill-for-skill/SKILL.md`, or put an invalid path in `init.json` `library.paths`) flips the return to `status: "degraded"` with an actionable error.
- Editing `init.json` `library.paths` and calling `system.refresh` picks up the new path.
- Writing a new SKILL.md into `.library/custom/foo/` and calling `system.refresh` makes `foo` appear in the catalog.
- `cp -r .library/custom/foo ../.library_shared/foo` + `system.refresh` makes `foo` appear in the catalog with its shared location.
- Re-running manifestation re-reads `init.json` as ground truth and rewrites `library.paths` accordingly (see §7 for exact semantics).
- The TUI migration renames `.library/` → `.library_shared/` on an existing network without data loss.
