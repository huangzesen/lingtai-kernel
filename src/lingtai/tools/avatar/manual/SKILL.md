---
name: avatar-manual
description: |
  Read before spawning a persistent avatar, choosing shallow or deep mode, or
  deciding whether a disposable daemon is the right substrate.
version: 1.4.0
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/avatar/__init__.py
- src/lingtai/tools/avatar/settings.py
- src/lingtai/tools/avatar/ANATOMY.md
- src/lingtai/tools/avatar/CONTRACT.md
- src/lingtai/tools/CONTRACT.md
- src/lingtai/tools/avatar/manual/reference/spawn.md
- src/lingtai/tools/avatar/manual/reference/lifecycle.md
- src/lingtai/tools/skills/manual/reference/cleanup-footprint-contract.md
- src/lingtai/kernel/prompt.py
- src/lingtai/kernel/base_agent/lifecycle.py
- src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
maintenance: |
  Tracks the avatar guidance router and its nested references. Keep the router
  short, preserve the settings anchors and first-call guard, and update the
  focused reference when spawn, authority, lifecycle, or cleanup semantics change.
---

# Avatar Manual — Router

Avatar creates a **persistent, independent agent process** (他我). Read this
router before choosing Avatar. It is not an in-process worker: use `daemon` for
a disposable session whose conclusion is enough, and `shell` for one-off host
commands.

## First-call prerequisite

Before the first spawn, call the read-only manual action and keep its exact
body:

```text
avatar(action="manual", input={}, reasoning="read avatar guidance before spawning")
```

`action`, `input`, and root `reasoning` are required. `input` is closed: spawn
owns `name`, `type`, `comment`, `dry_run`, and `confirm`; `settings` and `manual`
own `{}` only. The mission is root `reasoning`, never an input field. There is
no default action, and Avatar has no `rules` action. The manual call performs no
spawn or ledger I/O. Use the routes below for depth rather than guessing.

## Quick routes

| Need | Read |
|---|---|
| Select shallow/deep, validate the canonical name, write a mission, preview, or confirm a spawn | [Spawn and identity](reference/spawn.md) |
| Understand detached life, ledger/state, authority boundaries, boot observation, escalation, or platform launch | [Lifecycle and authority](reference/lifecycle.md) |
| `.rules` heartbeat and prompt protocol | [Psyche manual](../../../intrinsic_skills/psyche-manual/SKILL.md) |
| Inspect Avatar's immutable settings | `avatar(action="settings", input={}, reasoning="inspect Avatar policy")`; anchors are below |
| Inspect footprint or consider retirement | [Shared footprint recipe](../../skills/manual/reference/cleanup-footprint-contract.md#shared-footprint-check-recipe) |

## Tool essentials

```text
avatar(action="spawn",
       input={"name": "researcher", "type": null, "comment": null,
              "dry_run": false, "confirm": false},
       reasoning="<mission>")
avatar(action="spawn",
       input={"name": "clone", "type": "deep", "comment": null,
              "dry_run": false, "confirm": false},
       reasoning="<mission>")
avatar(action="settings", input={}, reasoning="inspect Avatar policy")
```

The strict model-facing `spawn` branch requires all five input keys; use `null`
for a defaulted value. `name` is the canonical sibling-directory basename.
`type` is `shallow` or `deep` and defaults to `shallow`; `dry_run` previews
without files or a process; `confirm` acknowledges review when the mission is
empty, short, or placeholder-like. Null values mean absent. A spawn is an
external side effect and an independent life; do not batch it with unrelated
calls.

## Settings

`avatar(action="settings", input={}, reasoning="inspect Avatar policy")` is
SHOW-only. Its 16 rows each contain exactly `key`, `current`, `default`,
`configurable`, and `comment`. Every row is immutable (`configurable:false`):
the fixed values in `avatar/settings.py` are both the fresh effective `current`
and truthful `default`. Avatar has no settings file, `LINGTAI_AVATAR_*`
environment source, alternate precedence, or set/reset action. A per-call spawn
input affects only that invocation.

The 16 exposed policy values are non-sensitive and unredacted. SHOW writes
nothing and omits parent identity, runtime/venv/auth, inherited environment
contents, handoff values, and invocation/session state. If the inventory cannot
be read or serialized safely, it fails as one result capped at 65,536 UTF-8
bytes, without partial rows or raw exception detail. There is no runtime setting
change procedure: an authorized permanent policy change requires source/manual
review, `tests/test_tool_family_avatar_migration.py` plus the shared
`tests/test_tool_settings_contract.py`, and a relaunch; call SHOW again after
relaunch to verify the effective values. The anchors below define each row group.

### Spawn call defaults

Absent or `null` values use `type="shallow"`, `comment=""`,
`dry_run=false`, and `confirm=false`. These are fixed source defaults, not
settings-file or environment values. Per-call inputs never change later SHOW
results. Permanent policy changes require source review, focused tests, and a
relaunch. The persistent `comment` is rendered after `meta_guidance` and before `rules`; its position does not override later sections.

### Spawn validation policy

Allowed types are `shallow` and `deep`. Names are 1–64 characters and must be a
single Unicode word segment using letters, digits, `_`, or `-`; dots, slashes,
spaces, and a leading dot are forbidden. Missions under 20 characters or equal
to/starting with `bar`, `check`, `debug`, `foo`, `temp`, `test`, or `tmp` require
`confirm=true`. These are fixed package policies.

### Spawn lifecycle policy

Boot observation waits 5.0 seconds and polls every 0.1 seconds; early-exit
stderr is capped at its final 2,000 bytes. Avatar uses the parent's default
preset, inherits the launcher process environment without exposing it, launches
a detached independent life, and clears newborn admin. A slow observation
releases the parent-side handle without terminating the child.

## Safety boundaries

- Avatar receives only its granted workdir and avatar-parent ports; it never
  receives the parent `Agent` or a parent in-process handle.
- The child directory is a direct sibling under the network root. The canonical
  `name` is both the public identity and path basename; path escape is refused.
- Shallow spawn is a blank slate with `init.json` plus narrow Psyche owner
  inputs. Deep spawn additionally copies durable identity/knowledge state, but
  still starts a fresh conversation. Neither mode inherits parent admin,
  identity, brief, or addons.
- Avatar no longer distributes rules. `.rules` remains kernel/Psyche state;
  read the Psyche route above instead of treating it as an Avatar action.

For procedures, storage layout, authority markers, boot receipts, caring for a
quiet avatar, and cleanup boundaries, open the two nested references. They are
part of this manual's source of truth, not optional examples.
