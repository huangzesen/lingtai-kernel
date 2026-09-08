---
name: daemon-manual
description: >
  Read before delegating work, diagnosing a slow/stuck/failed/timed-out
  emanation, or reclaiming on a hunch. Routes daemon call shape, task context,
  settings, backend support, inspection, compaction, and cleanup procedures.
version: 0.16.0
last_changed_at: 2026-09-08T00:00:00Z
related_files:
- src/lingtai/tools/daemon/CONTRACT.md
- src/lingtai/tools/daemon/ANATOMY.md
- src/lingtai/services/daemon.py
- src/lingtai/cli_daemon.py
- tests/test_cli_daemon.py
- src/lingtai/tools/daemon/system_prompt.py
- src/lingtai/tools/daemon/settings.py
- src/lingtai/tools/daemon/execution_host.py
- src/lingtai/tools/daemon/shell_prompt_events.py
- src/lingtai/tools/daemon/manual/reference/forensics/SKILL.md
- src/lingtai/tools/daemon/manual/reference/inspection/SKILL.md
- src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md
- src/lingtai/tools/daemon/manual/reference/cleanup/SKILL.md
- src/lingtai/tools/daemon/manual/reference/dispatch-ledger/SKILL.md
- src/lingtai/tools/bash/manual/SKILL.md
- tests/test_daemon_settings.py
maintenance: |
  Keep this as a short router. Put backend recipes, artifact forensics,
  inspection cadence, ledger diagnosis, and cleanup detail in the nested
  references; keep links and settings anchors stable.
---

# Daemon Manual — Router

`daemon` dispatches disposable subagents (emanations) with isolated sessions and
bounded tools. Read this manual before the first call. Put the complete
objective, authority, safety boundary, collaboration rules, and deliverable in
each task's `task`; `tools` grants capability only. The daemon is not a durable
persona or hidden memory store: leave reviewable work in files and use run
artifacts for follow-up.

## Routing table

| Need / keywords | Read |
|---|---|
| First use, safe call shape, task authority, settings, completion, or compact | This router, then the relevant section below |
| `backend="lingtai"`; built-in/in-process backend; preset, skills, MCP, or `finish` behavior | `reference/cli-backends/reference/backends/lingtai/SKILL.md` directly; no generic CLI page is required |
| External CLI backend, alias, resume, `backend_options`, native MCP/config, or installed `--help` | `reference/cli-backends/SKILL.md`, then its per-backend page |
| `list`/`check` cadence, stall, reminder, or intervention | `reference/inspection/SKILL.md` |
| Run artifacts, events, transcripts, token records, or SIGTERM/143 | `reference/forensics/SKILL.md` |
| Dispatch-ledger warnings or runtime identity mismatch | `reference/dispatch-ledger/SKILL.md` |
| Reclaim, footprint, consent-gated cleanup, or deletion boundary | `reference/cleanup/SKILL.md` |
| Shell async events or shell-side supervision | `shell-manual` |
| Programmatic use, CLI/help, or scripts/CI | `lingtai-agent daemon --help` |

The built-in route is intentionally separate from the generic CLI route. The
external page remains the owner of detailed CLI support, alias/resume behavior,
MCP/config injection, reserved flags, auth hygiene, and per-backend discovery.

## Call shape and action choice

Every call uses the closed LTP-v2 envelope. `input` contains only the selected
action's fields; `reasoning` is required; root `summarize` is optional and is
not action input. The optional root `summarize` boolean replaces the former flat `summary` field. An action's other fields are rejected before the engine runs.

| Action | Example | Effect |
|---|---|---|
| `emanate` | `daemon(action="emanate", input={"tasks": [{"task": "...", "tools": ["file"]}]}, reasoning="...")` | dispatches |
| `list` | `daemon(action="list", input={"status": "running"}, reasoning="...")` | read-only |
| `ask` | `daemon(action="ask", input={"id": "run-id", "message": "..."}, reasoning="...")` | follow-up |
| `check` | `daemon(action="check", input={"id": "run-id"}, reasoning="...")` | read-only |
| `settings` | `daemon(action="settings", input={}, reasoning="inspect daemon settings")` | read-only |
| `reclaim` | `daemon(action="reclaim", input={}, reasoning="stop confirmed work")` | cancels |
| `manual` | `daemon(action="manual", input={}, reasoning="read procedures")` | read-only |

`manual` is directly callable and reaches no daemon engine. An `emanate` returns
ids and a batch `group_id`; use each id/run id for inspection and audit. `list`, `check`, `settings`, and `manual` are read-only; `emanate`, `ask`, and `reclaim`
are side-effectful. The terminal distinctions are fixed and cannot be inferred
from a field borrowed from another action.

## First dispatch: task context and boundaries

Each `tasks[]` item requires `task` and `tools`.

- **`task`** is the complete parent-controlled objective, authority, constraints,
  safety posture, collaboration rules, tool-use policy, and deliverable. The
  removed `system_prompt` field has no alias; put its instruction here.
- **`tools`** grants technical capability, not permission beyond `task`. Parent
  MCP tools are not inherited. Guarded calls still pass the normal executor and
  call-guard path.
- **`skills`** is an optional list of skill directories or `SKILL.md` paths.
  Relative paths use the parent working directory; runtime injects a compact
  frontmatter catalog and the worker reads selected skills when relevant.
- **`mcp`** is an optional list of complete one-run stdio/http registrations.
  Names and shape remain visible while secret `env`/`headers` are redacted.
  LingTai mounts task-scoped clients; exposed tool names must be unique. External
  CLIs mount only transports documented in the backend reference.
- **`plugin`** is an optional plugin directory or search-root list. A manifest
  becomes compact prompt context and merges its skills/MCP; missing or unreadable
  paths resolve to nothing.
- **`task_files`** is an optional `{path, label?, role?}` UTF-8 text list under
  the parent working directory. Dispatch validates containment, encoding, and
  size, then snapshots bytes into the immutable store; the worker gets metadata
  and snapshot paths, never mutable originals or contents. A bad entry rejects
  the whole batch.
- **`preset`** is an optional authorized `.json`/`.jsonc` path from
  `system(action="presets")`; use the full returned path. Omission inherits the
  parent's regular tools; MCP still needs explicit task registrations. An
  unauthorized preset is refused before loading or scheduling.
- **`prompt`** is LingTai-only and is the first ordinary user message. Blank or
  omitted means exactly `Begin the assigned daemon task.`; external CLIs reject
  it and use `task` as their prompt.
- **`backend_options`** is CLI-only passthrough: booleans emit flags, scalars
  values, lists repeat flags, false/null omit them, and reserved `env` supplies
  string environment overrides. Unsafe or harness-owned keys fail preflight;
  options apply at `emanate`, not `ask`. Run the installed CLI's `--help` first.

### Per-task `context_token_limit`

This is a provider-compaction threshold, not a daemon context or window setting.
For native-provider support, resolved-window ownership, and failure behavior, use
the built-in LingTai child route in the table above. External CLI backends ignore
this field.

## Backend choice and support

`lingtai` is the default in-process backend. `claude-p` (alias `claude-code`),
Codex, OpenCode, Qwen Code, and Kimi Code have source-backed native
`daemon_common` checkpoint/completion paths as documented in the CLI reference.
MiMo Code, Oh-My-Pi, Cursor, and DeepSeek retain prompt-catalog-only
MCP/completion status; prompt text does not grant native tools. Hidden
interactive Claude is for legacy stored runs, not new selection. Do not invent
a fallback for an unsupported native path.

Before relying on an external CLI, inspect its installed `--help`. `tools` in
the task is ignored by external CLIs; `prompt` is LingTai-only. CLI backend
support, aliases, resume limits, reserved options, auth, and native MCP
boundaries belong to `reference/cli-backends/SKILL.md` and its child pages.

## Settings inventory (SHOW only)

Call `daemon(action="settings", input={}, reasoning="inspect daemon settings")`.
Success is exactly `{"settings": [...]}` with row fields `key`, `current`,
`default`, `configurable`, and `comment`. This action has no set/reset input and
writes no files, environment, launcher state, or run. `configurable: true` means
an owner procedure exists; it grants no mutation authority. A serialization/read
failure returns `SETTINGS_UNAVAILABLE`, never partial rows.

### Max turns

Anchor: `daemon-manual#max-turns`. Current value and application belong to
`daemon.settings`; precedence and setup details belong to the Contract. Per-call
`emanate.max_turns` may choose a smaller positive value for one batch.

### Manager pool size

Anchor: `daemon-manual#manager-pool-size`. Current value and application belong
to `daemon.settings`; precedence and rebuild details belong to the Contract.

### System prompt budget chars

Anchor: `daemon-manual#system-prompt-budget-chars`. Current value and
application belong to `daemon.settings`; prompt-budget behavior belongs to the
Contract. SHOW does not mutate it.

### Timeout

Anchor: `daemon-manual#timeout`. Current value and application belong to
`daemon.settings`; precedence and failure behavior belong to the Contract.
Per-call `emanate.timeout` is a one-batch override, not a settings mutation.

## Progress, inspection, and follow-up

Every terminal outcome (`done`, `failed`, `cancelled`, `timeout`) is
push-notified exactly once. Do not poll for "is it done". After notification,
call `check` and open the durable `result.txt`/error path for full output;
previews and notification text are bounded. `list` is a bounded ledger sweep,
not a lifetime folder scan. `check` accepts a live id or exact historical run
id and returns state, events, paths, and an artifact manifest. Read inspection
before declaring a stall or reclaiming on a hunch.

For large batches, use `group_id` for logical audit but each run id for filesystem
identity. A Task Card is conditional on the dispatch handoff: use it only when
Telegram is connected and a card exists; daemon itself creates no watcher.

## Compact and completion boundaries

Every LingTai daemon receives intrinsic `compact`; external CLIs do not. Its
`action` is required: `manual` is read-only, while `run` is a repeatable
non-terminal reset. Call `compact(action="run", _reason="...")` as the sole
assistant-batch tool call with a complete self-contained handoff. Never call the
unavailable parent `system.summarize`.

MCP-capable backends also receive built-in `daemon_common`. Call its
`checkpoint` at useful nonterminal boundaries; it is not chat, cancellation,
preemption, or completion. Before ending, call `finish` exactly once. Only
`finish(status="done")` permits terminal success. Background-and-wait is
invalid: run validation synchronously with an adequate explicit timeout and
inspect the result in the same run. Missing/invalid completion is a failure;
inspect durable artifacts before assuming anything was lost.

## Safety, Shell events, and footprint

Keep the parent task's authorization and privacy boundary authoritative. Do not
use daemon for durable identity, automatic recursion, or hidden context. A
selected daemon `shell` has a private `<run>/shell-jobs` namespace and no parent
notification store; read `shell-manual` for async + poll supervision. Do not
reclaim on a hunch: it terminates work in flight but keeps run folders as
evidence. There is no automatic cleanup.

Inspection, forensics, dispatch-ledger, cleanup, shell-event, programmatic-use,
and no-deletion routes remain available through the references above. Never
permit deletion, global configuration/install changes, account/provider changes,
or cleanup outside an authorized scope.

## Programmatic use / CLI

For scripts and CI, the reusable boundary is
`lingtai.services.daemon.DaemonService(state_root)`. It owns daemon artifacts
under that existing caller-chosen directory and never constructs an Agent. A
native LingTai task must carry its preset path directly; this mode never reads
or requires `init.json`:

```python
from lingtai.services.daemon import DaemonService

service = DaemonService("/absolute/path/to/daemon-state")
result = service.emanate([{
    "task": "Do the bounded task and leave reviewable artifacts.",
    "tools": ["file"],
    "preset": "/absolute/path/to/preset.json",
}])
snapshot = service.check(result["ids"][0])
```

The CLI is only a driver over that object. Its state root and existing task
payload are sufficient; there is no preview confirmation or Agent-directory
compatibility path:

```text
lingtai-agent daemon emanate --state-root /absolute/state --tasks tasks.json
lingtai-agent daemon check em-1234 --state-root /absolute/state
```

Use `lingtai-agent daemon --help` for exact options. External CLI backends keep
the engine's existing no-preset mechanics; the direct-preset requirement is for
native `lingtai` work.

## Maintenance

Keep this file a router. Put backend command recipes and reserved flags in the
per-backend references; keep inspection, forensics, ledger, shell, and cleanup
procedures there. Keep the action and settings anchors stable as deeper owners
move.
