---
name: daemon-manual
description: >
  Read before delegating work, diagnosing a slow/stuck/failed/timed-out
  emanation, or reclaiming on a hunch. Routes daemon call shape, task context,
  settings, backend support, inspection, compaction, and cleanup procedures.
version: 0.14.0
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/daemon/CONTRACT.md
- src/lingtai/tools/daemon/ANATOMY.md
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
  Keep this short router aligned with the daemon Contract and Anatomy. Put
  backend recipes, artifact forensics, inspection cadence, ledger diagnosis, and
  cleanup detail in the nested references listed above; update links when those
  owners move.
---

# Daemon Manual — Router

`daemon` dispatches disposable subagents (emanations) that share the parent's
working directory but have isolated sessions and a bounded tool surface. Read
this manual before the first call. Keep the complete objective, authority,
safety boundary, collaboration rules, and deliverable in each task's `task`
field; `tools` only grants capabilities. The daemon is not a durable persona or
hidden memory store: leave reviewable work in files and use the run artifacts for
follow-up.

## Nested reference catalog

These are parent-owned drill-down references, not additional top-level skills.
Load only the page needed for the current operation.

```yaml
- name: daemon-forensics
  location: reference/forensics/SKILL.md
  description: Run folders, daemon.json, artifacts, transcripts, event/token logs, and SIGTERM/143 diagnosis.
- name: daemon-inspection
  location: reference/inspection/SKILL.md
  description: List/check cadence, stall heuristic, reminders, and safe intervention.
- name: daemon-cli-backends
  location: reference/cli-backends/SKILL.md
  description: Backend selection, list semantics, backend_options, presets, MCP status, and per-backend routes.
- name: daemon-cleanup
  location: reference/cleanup/SKILL.md
  description: Reclaim scope, persistent forensic footprint, consent-gated cleanup, and boundaries.
- name: daemon-dispatch-ledger
  location: reference/dispatch-ledger/SKILL.md
  description: Append-order dispatch membership, list warnings, marker-only recovery, and runtime mismatch diagnosis.
```

The authoritative behavioral promises are in
[`daemon/CONTRACT.md`](../CONTRACT.md); this router teaches procedures without
copying that contract. For exact backend command/flag surfaces, use
`reference/cli-backends/SKILL.md` and its per-backend pages.

## Call shape and action choice

Every call is the closed LTP-v2 envelope below. `input` contains only the
selected action's fields; `reasoning` is required; root `summarize` is optional
and is not action input. Passing another action's field is rejected before the
engine runs.

| Action | Example | State |
|---|---|---|
| `emanate` | `daemon(action="emanate", input={"tasks": [{"task": "...", "tools": ["file"]}]}, reasoning="...")` | dispatches |
| `list` | `daemon(action="list", input={"status": "running"}, reasoning="...")` | read-only |
| `ask` | `daemon(action="ask", input={"id": "em-a1b2", "message": "..."}, reasoning="...")` | follow-up |
| `check` | `daemon(action="check", input={"id": "em-a1b2"}, reasoning="...")` | read-only |
| `settings` | `daemon(action="settings", input={}, reasoning="inspect daemon settings")` | read-only |
| `reclaim` | `daemon(action="reclaim", input={}, reasoning="stop confirmed work")` | cancels |
| `manual` | `daemon(action="manual", input={}, reasoning="read procedures")` | read-only |

`emanate`, `ask`, and `reclaim` are the side-effectful actions. `manual` is
always directly callable and does not reach the daemon engine. An emanate
returns immediately with ids and a batch `group_id`; use each returned id/run id
for inspection and audit.

## First dispatch: task context and boundaries

Each `tasks[]` item requires `task` and `tools`.

- **`task`** is the complete parent-controlled instruction: objective, role,
  constraints, safety posture, collaboration boundaries, tool-use policy, and
  deliverable. The removed `system_prompt` field has no alias; put its complete
  instruction here. Tell the daemon what it may read/write, whether shell or
  network access is allowed, and where detailed output belongs.
- **`tools`** is the technical capability list for this run, not authorization
  to exceed `task`. Parent MCP tools are not automatically inherited. Guarded
  tool calls still pass through the normal `ToolExecutor`/`ToolCallGuard` path.
- **`skills`** is an optional list of skill directories or direct `SKILL.md`
  paths. Relative paths resolve against the parent working directory. The
  runtime parses frontmatter and injects only a compact `name`/`location`/
  `description` catalog; the worker reads a selected skill path when relevant,
  rather than receiving its whole body.
- **`mcp`** is an optional list of complete one-run `stdio`/`http` registrations.
  Registration names and shape remain visible in prompt context while secret
  `env`/`headers` values are redacted. LingTai starts task-scoped clients;
  external CLIs mount only the transports documented in the backend reference.
  Tool names must be unique across task and plugin registrations.
- **`plugin`** is an optional list of plugin directories or search roots,
  resolved relative to the parent working directory. A manifest becomes a
  compact prompt section and its skills/MCP registrations are merged for the
  run. Missing or unreadable plugin paths resolve to nothing; malformed input
  is rejected before dispatch.
- **`task_files`** is an optional list of `{path, label?, role?}` UTF-8 text
  inputs under the parent working directory. Dispatch validates containment,
  encoding, and size, then snapshots bytes content-addressed into the immutable
  `daemons/_task_files/` store. The worker receives metadata and snapshot paths,
  never contents or mutable originals; a bad entry refuses the whole batch.
- **`preset`** is an optional authorized `.json`/`.jsonc` path from
  `system(action="presets")`, using the full returned path rather than a
  shorthand. Omission inherits the parent's regular tool surface; MCP still
  needs explicit task registrations. An unauthorized preset is refused before
  loading, connectivity checks, run-directory creation, or scheduling.
- **`prompt`** is LingTai-only and is the first ordinary user message, not part
  of `task`. A nonblank value is preserved byte-for-byte; omitted, empty, or
  whitespace-only means exactly `Begin the assigned daemon task.`. External CLI
  tasks reject it and use `task` as their CLI prompt.
- **`context_token_limit`** is a positive provider-context compaction threshold,
  not cumulative spend or daemon window size. It applies only to LingTai tasks
  whose resolved provider is Codex or native `mimo`; other providers and all
  external CLI backends ignore it. Omission uses this daemon session's resolved
  window. Codex compaction failure is non-fatal; native MiMo failure is hard.
  See the Contract for the provider boundary and the live backend reference.

Before dispatching a CLI task, inspect the installed command's `--help`; do not
assume a flag from an old example. `backend_options` is a CLI-only passthrough:
booleans emit flags, scalars emit values, lists repeat flags, and false/null
omit them. Its reserved `env` object supplies string environment overrides and
emits no argv flag. Unsafe keys and harness-owned flags fail preflight;
options apply at `emanate`, not `ask`. See the CLI reference for reserved flags,
auth hygiene, and command-specific details.

## Backend choice and support

The default `lingtai` backend is an in-process session. `claude-p` (with
compatibility alias `claude-code`), Codex, OpenCode, Qwen Code, and Kimi Code
have source-backed native `daemon_common` checkpoint/completion paths as
specified in the CLI reference. MiMo Code, Oh-My-Pi, Cursor, and DeepSeek keep
prompt-catalog-only MCP/completion status; they must not be treated as if prompt
text granted native tools. Hidden interactive Claude is for legacy stored runs,
not new selection. The backend enum and aliases are schema contracts; do not
invent a fallback for an unsupported native path.

## Settings inventory (SHOW only)

Call `daemon(action="settings", input={}, reasoning="inspect daemon settings")`.
Success is exactly `{"settings": [...]}` with five fields per row:
`key`, `current`, `default`, `configurable`, and `comment`. This action has no
set/reset input and writes no files, environment, launcher state, or run.
`configurable: true` means an authorized owner procedure exists; it does not
grant this caller mutation authority. Verify an owner change with a fresh SHOW.

### Max turns

Anchor: `daemon-manual#max-turns`. `max_turns` is the manager's positive
integer default/ceiling, normally `5000`; valid environment, setup, and owner
file precedence is defined by the Contract. A per-call `emanate.max_turns` may
choose a smaller positive value up to the schema maximum for one batch only.
Do not set a low cap merely because a task looks simple: exploration, action,
verification, and truthful completion all consume turns.

### Manager pool size

Anchor: `daemon-manual#manager-pool-size`. `manager_pool_size` is a
non-negative integer, normally `100`; `0` selects the classic per-run path.
It bounds concurrent POSIX central-manager execution children and applies when a
manager is rebuilt. The owner procedure is configuration/setup, not SHOW.

### System prompt budget chars

Anchor: `daemon-manual#system-prompt-budget-chars`.
`system_prompt_budget_chars` is a positive character limit, normally `20000`,
for a LingTai daemon's complete rendered prompt. Over-budget task/skill/MCP
context fails rather than silently truncating constraints. Environment, setup,
and owner-file precedence plus apply timing are in the Contract; SHOW never
changes it.

### Timeout

Anchor: `daemon-manual#timeout`. `timeout` is the manager's finite wall-clock
seconds default, normally `3600.0`, with an operational minimum of 5 and no
upper bound. Its owner value comes from capability/launcher setup, not the
owner file or environment. A per-call `emanate.timeout` is a one-batch override,
not a settings mutation. Invalid stored values are not silently repaired and
may make SHOW or later arithmetic fail closed.

## Progress, inspection, and follow-up

Completion is push-notified on the daemon channel for every terminal outcome:
`done`, `failed`, `cancelled`, and `timeout`. After a successful dispatch, do
not run a completion-poll loop. Wait for the notification, then call
`check` and open the durable `result.txt`/error path for full output; previews
and notification text are bounded. Use `list` for a bounded status sweep or
`check` for deliberate mid-flight evidence. Read
`reference/inspection/SKILL.md` before declaring a stall, reclaiming, or resting
with unverified-healthy work; it sets cadence and the one-shot self-wake rule.

`list` reads the append-only dispatch ledger and referenced `daemon.json` state,
not a lifetime folder scan. Omitted/null `last` means the newest 1000 records;
an explicit positive value is honored. `check` accepts a live id or exact
historical run id and returns state, events, paths, and an artifact manifest.
After refresh/molt, exact run ids still resolve from disk. For malformed tails,
missing records, recovery markers, or a runtime-identity mismatch, read
`reference/dispatch-ledger/SKILL.md` and do not repair, repeatedly retry, or
reclaim on a hunch.

`ask` is backend-specific. LingTai buffers the message in its in-process
session. A resumable CLI returns quickly with an asynchronous follow-up; a
second follow-up while one is in flight is busy. An active CLI with proven
`daemon_common` may instead return a bounded checkpoint-queued receipt, which
waits for the model's next checkpoint and is not stdin injection or live chat.
Terminal Qwen/Kimi resume is unsupported, and active backends without common
MCP retain their busy/unsupported behavior. Inspect the CLI reference before
relying on resume.

For large batches, use the returned `group_id` for logical audit but use each
run id for filesystem identity. A Task Card is conditional on the dispatch
handoff: use it only when Telegram is connected and a card is available; read
that capability's manual first. Daemon itself creates no watcher.

## Compact and completion boundaries

Every LingTai daemon receives the intrinsic `compact`; external CLI backends do
not. Its `action` is required: `manual` is read-only, while `run` is a repeatable
non-terminal reset. Invoke `compact(action="run", _reason="...")` as the sole
assistant-batch tool call with a complete self-contained handoff. The reset
retains only the compact call/result pair beside a rebuilt system prompt and
returns exact run/state/history/event paths. Never call the unavailable parent
`system.summarize`. At high context use, heed the visible countdown warning;
mechanical compaction retains the latest tool-call/result pair and sends an
explicit recovery instruction rather than silently continuing. Detailed
metadata/countdown semantics are in `daemon/CONTRACT.md`.

MCP-capable backends also receive built-in `daemon_common`. Use its
`checkpoint` at useful nonterminal boundaries for bounded state/summary and
ID-bound parent-message delivery; it is not chat, cancellation, preemption, or
completion. Before ending, call `finish` exactly once with `done`, `failed`, or
`incomplete` as appropriate. Only a validated `finish(status="done")` permits
terminal success. Missing/invalid completion is a missing-finish failure, not
proof the underlying work is lost: inspect the run trace and physical
`result.txt` first.

## Safety, Shell events, and footprint

Keep the parent task's authorization and privacy boundaries authoritative. Do
not give a daemon a broader purpose merely because a tool is available; do not
use daemon for durable identity, automatic recursion, or hidden context. Stop
and inspect before `reclaim`: it terminates work in flight, although run folders
remain as evidence. `reclaim` does not delete artifacts, and there is no
automatic folder cleanup. Read `reference/cleanup/SKILL.md` before any
consent-gated deletion or footprint audit; never delete active or needed
forensic records.

A selected `shell` inside a detached LingTai daemon has a private `<run>/shell-jobs`
namespace and no parent notification store. Its reminder/completion event carries
only bounded job metadata; the daemon receives fixed guidance to call
`shell.poll` for exact output at a safe text-only boundary. It never auto-polls,
waits for a future event, or revives a terminal daemon. Read `shell-manual` for
shell-side async supervision and reminders.

## Programmatic use

Shell scripts, Python, and CI should use `lingtai-agent daemon` rather than
scripting this tool directly. It uses the same envelope and run artifacts;
`emanate` previews without `--yes`, validates the complete tasks file, and
requires `--agent-dir` for dispatch. `list` and `check` are read-only. See the
CLI reference for the command examples and configuration behavior.

## Maintenance

Keep this file a router. Put backend command recipes and reserved flags in the
per-backend references, inspection/forensics in their references, and cleanup
procedures in cleanup. Keep the action anchors above stable because settings
rows link to them.
