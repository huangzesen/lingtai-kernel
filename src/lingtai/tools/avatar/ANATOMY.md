---
related_files:
  - src/lingtai/ANATOMY.md
  - src/lingtai/kernel/ANATOMY.md
  - src/lingtai/adapters/windows/ANATOMY.md
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/avatar/BEHAVIORS.md
  - src/lingtai/tools/avatar/__init__.py
  - src/lingtai/tools/avatar/_launcher.py
  - src/lingtai/tools/avatar/settings.py
  - src/lingtai/kernel/_fsutil.py
  - src/lingtai/kernel/prompt.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
  - src/lingtai/tools/avatar/CONTRACT.md
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/adapters/avatar_launcher.py
  - src/lingtai/adapters/posix/ANATOMY.md
  - src/lingtai/adapters/posix/avatar_launcher.py
  - src/lingtai/cli.py
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/tools/avatar/manual/SKILL.md
  - src/lingtai/tools/avatar/manual/reference/spawn.md
  - src/lingtai/tools/avatar/manual/reference/lifecycle.md
  - src/lingtai/tools/tool_family/ANATOMY.md
  - tests/test_avatar_rules.py
  - tests/test_tool_family_avatar_migration.py
  - tests/test_tool_plugin_declaration.py
  - src/lingtai/tools/avatar/glossary-en.md
  - src/lingtai/tools/avatar/glossary-zh.md
  - src/lingtai/tools/avatar/glossary-wen.md
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# core/avatar

Avatar capability — spawn independent peer agents (分身) as fully detached
processes. Two modes:

- **Shallow (初生):** Copy `init.json`, create a narrow Psyche prompt-owner
  document in a new working dir, strip identity, and launch. A Driver lease
  additionally writes restrictive `.lingtai-derived-child.json`. The avatar
  gets the same LLM config + capabilities but no history.
- **Deep (二重身):** Copy identity and durable knowledge (`system/`, `knowledge/`, `exports/`)
  plus `init.json` and the narrow Psyche prompt-owner document, strip name + history. The avatar is a doppelgänger — same
  character, pad, knowledge — but starts a fresh conversation.

Both modes launch `lingtai-agent run <dir>` as a detached process. The avatar is an
independent life — its existence does not depend on yours.

## Components

- `avatar/__init__.py` — static official `DECLARATION`, settings binding, local
  manual child, validation, preparation, boot policy, ledger, schemas, and
  registrar setup. The core class is `AvatarManager`.
- `avatar/_launcher.py` — immutable launch request/receipt, the avatar-local
  opaque-handle Port, current restrictive child-boot marker name, read-only
  legacy-marker compatibility path, and their shared present/absent/unknown
  marker probe.
- `cli.py:run()` — consumes that marker when an avatar process boots and makes
  a missing nested-derived authority fail closed; it does not receive authority
  through the environment.
- `avatar/settings.py` — the no-I/O `AvatarSettingsProvider` and the constants
  shared with Avatar's runtime validation/default/lifecycle consumers. It owns
  no store, environment reader, runtime object, or writer.

## Public API

The capability exposes one public tool, `avatar`, an LTP v2 action-separated
family (`src/lingtai/tools/CONTRACT.md`) whose actions are canonical children:

| Action | Own strict `input` | Description |
|------|------|-------------|
| `spawn` | `name`, `type`, `comment`, `dry_run`, `confirm` | Spawn a new avatar agent (shallow or deep). `dry_run` previews only; `confirm` acknowledges the mission-quality gate. |
| `settings` | `{}` | Return 16 immutable Avatar defaults, validation constraints, and lifecycle policies as exact five-field rows. |
| `manual` | *(empty)* | Read-only: returns the exact `manual/SKILL.md` body plus its host-local `manual_path`. No spawn I/O. |

Avatar no longer owns a `rules` action or an automatic post-spawn rules
fan-out (removed, not relocated — see `src/lingtai/tools/avatar/CONTRACT.md`
contract_version 9). The `.rules` heartbeat signal and `system/rules.md`
persistence remain real, unchanged kernel state owned by
`src/lingtai/kernel/base_agent/lifecycle.py`; see `psyche-manual` for that
protocol.

The model-facing root is exactly `action` + `input` + required `reasoning` +
optional `summarize`, `additionalProperties: false`. Each action owns exactly
its own fields, so a key from another action's branch is rejected *before* any
handler I/O. The child canonical name equals the public action value equals the
dispatch key — there is no mapping layer.

`action` has no default — it is required both by the schema and at runtime,
matching the established action-tool convention already used by `knowledge`,
`mcp`, `skills`, `notification`, `system`, `soul`, and `daemon`. Omitting
`action` fails deterministically with avatar's own pinned unknown-action
envelope; it never falls through to `spawn`.

**Mission brief.** The spawn mission is root `reasoning` (normalized to
`_reasoning` by ToolExecutor), never an `input` property — nested `input` must
never carry `reasoning`/`_reasoning`/`summarize`. `handle()` captures it from
the envelope and hands it to `_spawn` out-of-band, clearing it in `finally` so
no later call can inherit a previous call's mission.

Schema composition and envelope dispatch are delegated to the generic, optional
`tool_family` infrastructure (`../tool_family/ANATOMY.md`); `handle()` is
retained as avatar's own outer layer solely to normalize the generic
`ACTION_REQUIRED` envelope failure back to avatar's exact pinned
unknown-action error string.

## Internal Module Layout

```
avatar/__init__.py
  ├── _SPAWN_INPUT_SCHEMA           — canonical strict operational input
  ├── DECLARATION                   — static official identity, actions,
  │                                    settings/manual reservations, and exact
  │                                    `(workdir, avatar_parent)` grant
  ├── _CHILD_SPECS / _build_family  — declaration-derived public listing plus
  │                                    generic settings and local manual children
  ├── _bind()                       — pure host composition → BoundToolPlugin
  ├── AvatarManager.__init__        — narrow host + per-instance ToolFamily
  ├── handle()                      — envelope entry: captures root _reasoning,
  │                                    delegates to ToolFamily.handle(), then
  │                                    normalizes ACTION_REQUIRED back to
  │                                    avatar's pinned unknown-action error
  ├── _dispatch_spawn               — operational child handler; strips nulls,
  │                                    threads the mission brief to _spawn
  ├── _strip_nulls()                — nullable-optional → absent
  ├── _manual_payload()             — plugin-owned local `manual/SKILL.md`
  │                                    result; no manager/host mutation
  ├── settings.AvatarSettingsProvider — fresh immutable SettingRow values;
  │                                    no parent state or configuration I/O
  │
  │  Spawn pipeline:
  ├── _spawn()                      — validates name, checks liveness, prepares working dir, launches process
  ├── _make_avatar_init()           — builds avatar's init.json from parent's (strips identity, reroots paths)
  ├── _make_avatar_psyche_settings() — delegates to Psyche's v1 serializer with inherited base/covenant inputs and the replacement spawn comment
  ├── _prepare_deep()               — copies system/ + knowledge/ + exports/ + combo.json for deep mode
  ├── _launch()                     — resolves argv and delegates to the launcher Port
  ├── _wait_for_boot()              — polls .agent.heartbeat or Port exit truth
  │
  │  Ledger:
  ├── _append_ledger()              — appends spawn event to delegates/ledger.jsonl
  └── _read_ledger()                — reads all ledger records
```

`_rules()`, `_walk_avatar_tree()`, and `_distribute_rules_to_descendants()`
(the admin-gated rules update and its BFS descendant fan-out) plus the
post-spawn canonical-rules read they fed are removed entirely — not renamed
or relocated (contract_version 9). Nothing in this package writes `.rules`
anymore.

## Key Invariants

- **Declared least privilege:** `AvatarManager` receives a `ToolPluginHost`, not
  an Agent. `workdir` supplies every local path; `avatar_parent` supplies only
  the current parent identity and optional venv inheritance. The binder cannot
  mount itself.
- **Local manual:** `manual` is the declaration-appended reserved child but
  remains package-local: it returns `manual/SKILL.md` and performs no host or
  manager I/O.
- **Settings are SHOW-only:** declaration opt-in injects `settings` immediately
  before `manual`. The provider returns only
  `key/current/default/configurable/comment`, with every Avatar row fixed and
  non-configurable. Avatar owns no settings file, environment peer, provider
  cache, writer, or set/reset operation; parent identity, runtime/venv/auth,
  handoff, and invocation/session state are omitted rather than sampled.
- **Name validation:** Avatar names must match `^[\w-]+$` (Unicode-aware), max 64 chars, no dots or path separators. The name doubles as the working directory basename.
- **Path scope:** The avatar's working directory must be a direct sibling of the parent's (same parent directory). Resolved path is checked against the network root to prevent escape.
- **No identity inheritance:** Avatars get no inherited name (`agent_name` is set to the avatar name), admin privileges, parent comment, brief, or addons (IMAP/Telegram). The inherited `lingtai` seed is blanked; the spawn comment is newly authored and the first turn still arrives via a separate `.prompt` signal file.
- **Preset stability:** Avatars always spawn on the parent's DEFAULT preset, not its currently-active one. Materialized `llm` + `capabilities` are stripped so the avatar re-materializes from the preset on first boot.
- **Relative path re-rooting:** Preset paths (`default`, `active`, `allowed`) that are relative are re-rooted against the parent's working dir so they remain valid from the avatar's different directory.
- **Liveness check:** Before spawning, existing ledger entries are observed through a target-bound `PosixAgentPresenceStoreAdapter` and Core `observe_alive()` policy. If a live avatar with the same name exists, the spawn is refused with `already_active`.
- **Boot verification:** After launching, `_wait_for_boot()` polls for `.agent.heartbeat` or Port exit truth within 5 seconds. If the process exits before handshaking, stderr is captured and the failure is reported. Port release after observation never kills a live slow avatar.
- **Derived child requirement:** only a Driver-granted child-endpoint lease
  makes `_spawn()` atomically write `.lingtai-derived-child.json` before
  launch, outside the child-managed `system/` namespace. The shared probe also treats a legacy
  `system/derived_child.json` as restrictive so existing children retain their
  requirement after upgrade; only both locations being missing is absence.
  `cli.run()` reads that durable state on every boot and turns it into the
  restrictive requirement that any nested daemon/avatar launch has authority.
  The launcher also adds
  `LINGTAI_DERIVED_AVATAR_EXECUTION=1` as redundant immediate-launch defense.
  Neither marker nor environment form carries a parent, grant, or authority
  bearer; the opaque lease is handed only to the POSIX launcher, or closed if
  launch does not reach that Port.
- **Deep copy scope guard:** `_prepare_deep()` asserts `dst.parent == src.parent` to prevent rmtree from reaching outside the network root.
- **Mission-quality gate (issue #33):** Before any filesystem mutation, `_spawn` runs `_mission_looks_unsafe(reasoning)` — empty / sub-20-char / debug-placeholder missions return `{"status": "confirmation_needed", ...}` unless `confirm=true`. The dry-run path is exempt (its purpose is preview without commitment).
- **Dry-run (issue #33):** `dry_run=true` short-circuits after parent `init.json` is loaded and before any working dir is created or process launched, returning `{"status": "dry_run", "preview": {...}}`. The preview includes whether the mission would have tripped the quality gate.

## Dependencies

- `lingtai.kernel.i18n` — `t()` for localized strings
- `lingtai.kernel.agent_presence` + `lingtai.adapters.posix.agent_presence` — ordered Core liveness policy and the target-bound production presence adapter
- `lingtai.kernel.handshake` — `resolve_address()` for ledger-based tree walking
- `lingtai.venv_resolve` — `resolve_venv()`, `venv_python()` for resolving the Python executable to launch the avatar
- `lingtai.agent.Agent` — parent agent type (TYPE_CHECKING only)

## Composition

- **Parent:** `src/lingtai/tools/` (tool package).
- **Siblings:** `daemon/`, `mcp/`, `knowledge/` (private durable memory), `skills/` (skill catalog), `bash/`.
- **Kernel hooks:** `setup()` is called during capability initialization and
  routes `DECLARATION` through `register_agent_tool_plugins`; the kernel checks
  the reserved `avatar` name, grants only `workdir`/`avatar_parent`, binds the
  manager, and mounts the issued transaction. `AvatarManager` internally
  dispatches `spawn`, the generic declaration-bound `settings` child,
  and the declaration-owned local `manual` child through `ToolFamily`. `avatar`
  is on the kernel's `_LTP_V2_MIGRATED_FAMILIES` allowlist
  (`src/lingtai/kernel/tool_result_summary.py`), so the root `summarize` boolean
  it advertises is actually honored by the single central summarizer. The daemon
  capability blacklists `avatar` to prevent avatar-in-daemon recursion.

Platform process mechanics are in `adapters/avatar_launcher.py` plus the POSIX
and Windows adapters. A Driver child-endpoint handoff is POSIX-only: the POSIX
adapter inherits exactly the one-shot endpoint, while the Windows adapter
closes and rejects that lease rather than dropping it.
