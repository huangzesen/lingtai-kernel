---
name: daemon-manual
description: >
  Read before delegating work, diagnosing a slow/stuck/failed/timed-out
  emanation (including exit 143 / SIGTERM), or before reclaiming on a hunch;
  owns CLI backends/`backend_options`, settings meaning, polling cadence,
  compact, and footprint cleanup procedures.
version: 0.13.2
last_changed_at: 2026-09-04T00:00:00Z
related_files:
- src/lingtai/tools/daemon/CONTRACT.md
- src/lingtai/tools/daemon/ANATOMY.md
- src/lingtai/tools/daemon/system_prompt.py
- src/lingtai/tools/daemon/settings.py
- src/lingtai/tools/daemon/execution_host.py
- src/lingtai/tools/daemon/shell_prompt_events.py
- src/lingtai/tools/bash/manual/SKILL.md
- src/lingtai/tools/daemon/manual/reference/forensics/SKILL.md
- src/lingtai/tools/daemon/manual/reference/dispatch-ledger/SKILL.md
- tests/test_daemon_settings.py
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# Daemon Manual — Router

The `daemon` tool schema covers dispatch/follow-up/check/reclaim/settings. This manual
routes to deeper operational references: how to inspect daemon artifacts, decide
whether work is stuck, use CLI backends safely, and clean up old emanations.

Scope note: this manual does **not** restate the daemon tool argument schema, and
it does not document cross-process recovery/orphan-detection internals. For the
broader runtime turn loop that daemon emanations mirror, use `lingtai-kernel-anatomy`
and its runtime-loop reference.

Maintainer note: the unified daemon contract is
`src/lingtai/tools/daemon/CONTRACT.md`. Update or explicitly re-check that
contract when changing backend routing, selected `skills`, one-run `mcp`, native
MCP mounting, `daemon_common`, backend support status, or run artifacts.

Use the smallest reference that matches the problem. Do not kill or reclaim a
daemon on a hunch; inspect first.

## Nested reference catalog

`daemon-manual` owns these nested references. They are parent-owned drill-down
files, not standalone top-level skills.

```yaml
- name: daemon-forensics
  location: reference/forensics/SKILL.md
  description: |
    Daemon artifact forensics: persistent daemons/em-* folders, daemon.json
    status fields, chat_history.jsonl, token_ledger.jsonl, events.jsonl,
    interpreting exit code 143 / SIGTERM (terminated, not a test/code failure),
    and how to inspect progress without guessing.
- name: daemon-inspection
  location: reference/inspection/SKILL.md
  description: |
    Polling cadence, stall heuristics, anti-patterns, backend-specific polling
    notes, and reminders before resting while daemon work remains pending.
- name: daemon-cli-backends
  location: reference/cli-backends/SKILL.md
  description: |
    Daemon API details and CLI backends: daemon(action=list, input={}), claude-p/codex/opencode behavior,
    backend_options flag passing, preset/capability inheritance, and Codex
    modal capabilities.
- name: daemon-cleanup
  location: reference/cleanup/SKILL.md
  description: |
    Scope boundaries and daemon footprint cleanup: what the manual does not
    cover, reclaim persistence, and safe cleanup of old daemon artifacts.
- name: daemon-dispatch-ledger
  location: reference/dispatch-ledger/SKILL.md
  description: |
    Append-only dispatch membership/order, scoped list warnings, marker-only
    recovery, recent background session snapshots, and no-repair diagnosis.
```

## Call shape

Every `daemon` call is the same four-field envelope: a required `action`, a
required `input` object holding **only** that action's own fields, and a
required `reasoning` string. The optional root `summarize` boolean replaces the
former flat `summary` field. Passing another action's field — or any field at
the call root — is refused before the daemon engine runs.

| Action | Call |
|---|---|
| `emanate` | `daemon(action="emanate", input={"tasks": [{"task": "...", "tools": ["file"]}], "backend": "codex"}, reasoning="...")` |
| `list` | `daemon(action="list", input={"status": "running", "last": 20}, reasoning="...")` |
| `ask` | `daemon(action="ask", input={"id": "em-1", "message": "..."}, reasoning="...")` |
| `check` | `daemon(action="check", input={"id": "em-1", "last": 20, "truncate": 500}, reasoning="...")` |
| `reclaim` | `daemon(action="reclaim", input={}, reasoning="...")` |
| `settings` | `daemon(action="settings", input={}, reasoning="...")` |
| `manual` | `daemon(action="manual", input={}, reasoning="...")` |

`list`, `check`, `settings`, and `manual` are read-only. `emanate`, `ask`, and `reclaim`
are the three that change state.

## Settings inventory

`daemon(action="settings", input={}, reasoning="inspect daemon settings")`
is SHOW-only. Success contains only `{"settings": [...]}`; every row has exactly
`key`, `current`, `default`, `configurable`, and the exact manual-section
pointer `comment`. The action accepts no set/reset or other input and never
writes a config file, process environment, launcher state, or daemon run.

`configurable: true` means an authorized owner can use the existing procedure
in the named section and then verify the new active value with a second SHOW.
It does not authorize the caller or this action to perform the change.

### Max turns

`max_turns` is the active manager's default and ceiling for a new run's
tool-loop turns; its meaningful default is `5000`. Accepted owner values are
positive integers. Source precedence is a valid positive-integer
`LINGTAI_DAEMON_MAX_TURNS` environment value, an explicit daemon capability or
setup value, a positive `<workdir>/daemon/daemon.json` key `max_turns`, then
the default. Invalid environment input retains the valid explicit, file, or
default result. The per-call `emanate.max_turns` input may select a smaller
value for one batch but does not change this owner setting.

An authorized owner changes the launcher/environment,
`manifest.capabilities.daemon.max_turns` in `init.json`, or the existing
`daemon/daemon.json` key with the normal File/Shell procedure, then refreshes
or relaunches the agent. The value applies when a new `DaemonManager` is built;
SHOW never writes it.

### Manager pool size

`manager_pool_size` bounds concurrent POSIX central-manager execution
children; `0` selects the classic per-run supervisor path. Its default is
`100`, and accepted values are non-negative integers. Source precedence is a
valid `LINGTAI_DAEMON_MANAGER_POOL_SIZE` environment value, an explicit daemon
capability or setup value, a valid `daemon/daemon.json` key
`manager_pool_size`, then the default. Invalid environment or file input falls
through without repair.

An authorized owner updates the launcher/environment,
`manifest.capabilities.daemon.manager_pool_size`, or the existing owner file
with the normal Shell/File/config procedure, then refreshes or relaunches. The
value applies when the manager is rebuilt; SHOW only reports the active
snapshot.

### System prompt budget chars

`system_prompt_budget_chars` is the character limit for a LingTai daemon's
rendered system prompt. Over-budget prompts fail rather than truncate
constraints. The default is `20000`; accepted values are positive integers.
Source precedence is a valid
`LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS` environment value, an explicit
daemon capability or setup value, a valid `daemon/daemon.json` key
`system_prompt_budget_chars`, then the default. Invalid input falls through.

An authorized owner uses the existing launcher/environment,
`manifest.capabilities.daemon.system_prompt_budget_chars`, or owner-file
procedure and then refreshes or relaunches. The value applies to a newly built
manager; SHOW performs no write.

### Timeout

`timeout` is the active manager's default wall-clock seconds for a run and for
supported follow-up workers; its default is `3600.0`. The operationally valid
owner value is a finite JSON number (integer or float, but not a Boolean) of at
least `5` seconds; there is no upper bound. Its only source is the
launcher/capability setup layer: the owner value has no daemon-file or
environment peer. An explicit
`manifest.capabilities.daemon.timeout` value is passed by capability setup,
otherwise the default applies. A one-run `emanate.timeout` input does not
change the owner setting.

The existing setup seam does not enforce that type, range, or finiteness: the
`float` annotation is informational, and `setup()` / `DaemonManager` store an
explicit value unchanged. A JSON-finite wrong-type or out-of-range value is
therefore neither coerced nor replaced by the default; SHOW reports that exact
active value. If a non-finite float reaches setup, it is likewise stored, but
the generic settings serializer refuses non-finite JSON, so SHOW fails the
whole inventory with `SETTINGS_UNAVAILABLE` and emits no partial rows. Invalid
stored values can fail later when timeout arithmetic or persistence consumes
them; there is no automatic repair or fallback.

An authorized owner changes the daemon capability value in `init.json` using
the normal config/File procedure and refreshes or relaunches. The new default
applies when the manager is rebuilt; SHOW never mutates it.

## Programmatic use / CLI

Callers outside a live agent turn — a shell script, a Python subprocess, a CI
job — should use the `lingtai-agent daemon` subcommand instead of scripting this
tool. It runs the same engine (`DaemonManager`) through the same action
envelope, so dispatch, run directories, presets, and result shapes are
identical.

```bash
lingtai-agent daemon emanate --tasks batch.json --agent-dir ~/agents/foo [--backend lingtai] [--yes]
lingtai-agent daemon list  [--status running] [--last N] [--agent-dir ~/agents/foo]
lingtai-agent daemon check em-1 [--agent-dir ~/agents/foo]
```

`--tasks` takes the tool's own `emanate` input object (`tasks`, and optionally
`backend` / `max_turns` / `timeout`), or a bare array of task objects:

```json
{"tasks": [{"task": "Summarize docs/ into notes.md", "tools": ["file"]}]}
```

Behavior worth knowing before wiring it into a job:

- **`emanate` previews by default.** Without `--yes` it prints the batch it
  *would* dispatch (count, backend, presets, per-task tools) and exits 0
  without spawning anything. With `--yes` it prints the tool's own emanate
  result — `status`, `count`, `ids`, `group_id`, `handoff`.
- **The preview is fully validated.** The tasks file is checked against this
  tool's own `emanate` schema before `--yes` is even considered: backend enum,
  field types, `max_turns` 1..5000, `timeout` ≥ 5, `context_token_limit` ≥ 1.
  An invalid file exits non-zero with every violation listed, rather than
  printing a preview that dispatch would reject.
- **`--agent-dir` is required for `emanate`** and must contain `init.json`;
  `list` / `check` default to the current directory.
- **`emanate` reads the agent's *effective* configuration**, through the same
  canonical reader boot uses: JSONC parsed, active preset materialized,
  `provider: "inherit"` expanded, schema validated, and every relative path
  (notably `env_file`) resolved against `--agent-dir` rather than the caller's
  working directory. A daemon therefore launches on the preset's effective
  provider/model, not the raw `manifest.llm` the preset replaces. An
  unusable `init.json` refuses the batch.
- **Capability policy is enforced.** A task may only request tools this agent
  actually grants — `manifest.capabilities` overlaid on the core floor, minus
  `manifest.disable` — and each is instantiated with the agent's authored
  kwargs. Requesting a disabled tool refuses the *whole* batch. Tasks naming an
  explicit `preset` are governed by that preset's own sandbox instead.
- **Preset allowlist is fail-closed.** A task naming a preset outside
  `manifest.preset.allowed` refuses the *whole* batch, at preview time as well
  as at dispatch. An agent with no allowlist grants no preset.
- **`list` and `check` are categorically read-only.** Default list tails the
  newest 1000 append-order dispatch records and reads only their referenced
  `daemon.json` files; it never scans, sorts, backfills, or repairs historical
  folders. Its scoped `warnings` are advisory. A known legacy run remains
  inspectable by exact id/filesystem forensics, not by an automatic migration.
  Pass a positive `last: N` for an explicit page; non-empty `contains` searches prompt-preview text
  among explicitly ledger-selected candidates and may stream more ledger records as an ad-hoc query.
- **Terminal notifications stay with the owning agent.** Runs dispatched from
  the CLI are detached and still notify the agent that owns the working
  directory; the CLI process itself publishes nothing.
- **Secrets are redacted from CLI output** (`backend_options.env`, MCP
  `env`/`headers`, credential-shaped keys), using the same policy as the
  durable daemon manifest.
- `ask` and `reclaim` are deliberately not exposed: this surface causes no side
  effect beyond spawning daemons.

## Router table

| Need / keywords | Read |
|---|---|
| Find an emanation's folder; inspect `daemon.json`, transcript, token ledger, event log; understand result paths or token attribution | `reference/forensics/SKILL.md` |
| Interpret a CLI-backend **exit code 143 / SIGTERM** (terminated from outside — watchdog/timeout/reclaim — not a test or code failure); decide rerun vs hand-off; report it to a human | `reference/forensics/SKILL.md` |
| Decide whether a daemon is stuck; choose when to list/check/tail; avoid polling too often; set a reminder before resting | `reference/inspection/SKILL.md` |
| Use `daemon(action="list", input={})`; choose `lingtai` vs `claude-p`/`codex`/`opencode`; pass `backend_options`; understand CLI backend limitations | `reference/cli-backends/SKILL.md` |
| Retire or audit old daemon artifacts; understand what `reclaim` does and does not delete; scope boundaries | `reference/cleanup/SKILL.md` |
| Interpret `daemon(list)` ledger warnings, missing legacy history, malformed tails, recovery markers, or recent snapshot lag | `reference/dispatch-ledger/SKILL.md` |

## Quick decision tree

1. **Need only the daemon tool argument schema?** Use the tool description.
2. **Daemon seems slow?** Read `reference/forensics/SKILL.md`, then
   `reference/inspection/SKILL.md` if you might intervene.
3. **Daemon failed/timed out?** Read recent events/transcript via the forensics
   reference before retrying.
4. **Choosing an execution backend or flags?** Read
   `reference/cli-backends/SKILL.md`.
5. **Cleaning old folders?** Read `reference/cleanup/SKILL.md` and avoid deleting
   useful forensic evidence without a reason.

## High-concurrency central manager (Phase 1)

For high-concurrency daemon batches, the kernel can route work through a
**central daemon manager** instead of spawning one detached supervisor per run.
The manager is a single thin POSIX process per agent that owns queue intake,
assignment, deadline/control, terminal truth, idempotent notification, and a
durable journal with restart recovery. Its purpose is to cap RAM growth: at most
`manager_pool_size` execution children run concurrently, and runs beyond that
queue as pure state (≈0 MB RAM) until a worker frees.

Configuration remains per-agent and is not written by SHOW. See
[Manager pool size](#manager-pool-size) for its accepted values, precedence,
apply timing, and existing change procedure.

Behavior notes:
- The manager path is POSIX-only and enabled by default (`manager_pool_size`
  default `100`); Windows and an explicit `manager_pool_size: 0` keep the
  classic per-run supervisor behavior.
- When enabled, every batch size runs through the manager and at most
  `manager_pool_size` execution children run at a time; the rest queue and
  execute as workers free.
- Queued runs keep only secret-free durable state under `<agent>/daemon/manager`.
  Their runtime capsules are memory-only. If the manager restarts before assigning
  a queued run, that run is failed with `manager_restart_capsule_unavailable`
  evidence and terminal notification instead of being replayed.
- Phase 1 still spawns one execution child per active run (no reusable LLM
  worker yet); the reusable worker pool is a later phase.
- **For large concurrent batches, strongly recommend delaying the `daemon`
  notification channel** with `notification(action="delay", input={"channel":
  "daemon", "seconds": <duration>})` so terminal arrivals do not repeatedly
  wake the parent. Delay masks attention only: per-run mini-files, durable
  receipts, `daemon.json`, and aggregate truth remain readable; choose a bounded
  duration and let the delay-alarm mirror re-expose the channel. Read
  `notification-manual` before selecting the duration or handling expiry.

### Runtime-identity mismatch after refresh

If `emanate` refuses before a new worker starts with either **“resident central
 daemon manager runtime identity does not match”** or **“starting central daemon
 manager runtime identity does not match”**, stop retrying and use this manual.
This is a fail-closed safety fence: it prevents a persistent POSIX manager loaded
from different code, source root, or daemon-notification protocol from accepting
new work. Existing queued or active work is deliberately not taken over.

A common cause is an agent that has refreshed or changed runtime source while an
older resident manager process remains alive (including a legacy record without
the runtime-identity stamp). It is not by itself evidence that the requested
model, preset, task, or repository is broken.

1. **Inspect; do not reclaim or kill on a hunch.** Give the runtime owner the
   error, manager PID/incarnation, runtime-identity record, queue count, and
   journal states. A mismatch can coexist with real queued or active work.
2. **Preserve fail-closed safety.** If the PID/incarnation cannot be confirmed,
   any queue entry exists, or any journal is nonterminal, do not terminate or
   take over the manager. Escalate to the runtime owner.
3. **Only an authorized owner may recover an actually idle manager.** After
   confirming the exact manager process, an empty queue, and terminal-only
   journal records, perform a controlled manager restart, then run one small
   no-write daemon check. Refresh/relaunch the agent only through its normal
   lifecycle procedure; do not edit private manager records to bypass the fence.
4. **Report the outcome.** State whether a fresh manager wrote a current runtime
   identity and whether the bounded check dispatched and finished. If it still
   refuses, preserve the exact error and escalate rather than repeatedly retrying.

## Core rules to keep resident

- Keep daemon lightweight. If the task needs long-lived persona, molt/pad,
  durable knowledge, or ongoing ownership, spawn an avatar/agent instead of
  stretching daemon.
- **Do not set a small `max_turns` merely because a task looks simple.** Omit it
  to use the default, or set it only when a concrete operational boundary still
  leaves enough turns for unexpected investigation, validation, and a truthful finish.
- Every LingTai daemon receives a short package-owned operating prompt before
  the parent task. It tells the worker to read a relevant manual before first
  using a tool/workflow that has one, use a visible result tool's `summary=true`
  only for predictable bulky output whose exact raw text is unnecessary, use
  daemon `compact` rather than the unavailable parent `system.summarize`, and
  finish truthfully. The complete rendered system prompt is capped at 20,000
  characters by default and fails instead of truncating task/skill/MCP
  context — see "System prompt budget chars" above for the exact
  override precedence. Keep the run-specific objective,
  authority, safety boundary, and deliverable in `task`.
- On the LingTai backend, explicit `summary=true` uses a daemon-local,
  no-tools session on the same effective service, provider, and model. It keeps
  raw output in the run-local `logs/events.jsonl` and returns the generated
  summary (or fail-closed error) with the exact recovery locator.
- Think of each task item as **task objective + behavior guidance + tool
  surface**:
  - `task` is the complete parent-controlled daemon system instruction:
    objective, role, constraints, safety posture, collaboration boundaries, and
    tool-use policy. The removed `system_prompt` field has no alias; migrate its
    complete contents into `task`.
  - `prompt` is optional and LingTai-only. It is the first ordinary user message,
    sent exactly when nonblank, or exactly `Begin the assigned daemon task.` when omitted/blank.
    External CLI backends reject it and keep `task` as their CLI prompt.
  - `tools` answers **what the daemon can technically use** for this run. The
    parent puts the complete operating contract in `task`: when and how those
    tools should be used, for example read-only file access, no network, write
    only to one report path, or ask a named peer before guessing. `email` is
    daemon-eligible communication, but it is not granted by default; include
    `tools: ["email"]` only when the daemon should be able to use internal mail.
    Other tool names still matter for file/shell/web/etc. access.
  - `skills` answers **which workflows the daemon should know about**. It is an
    optional list of strings. Each string may be either a skill directory
    containing `SKILL.md` or a direct `SKILL.md` path; relative paths resolve
    against the parent agent working directory. The runtime parses each skill's
    frontmatter and injects a compact YAML skill list into the daemon prompt.
    Use `task` to say when/how those selected skills should be applied.
  - `mcp` answers **which one-run MCP registrations belong to this daemon**. It
    is optional and is an array of full MCP registration objects: `name` plus
    `transport`/`type` (`stdio` or `http`), then `command`/`args`/`env` for
    stdio or `url`/`headers` for HTTP. The runtime serializes these registrations
    as YAML into every backend's oneshot context. The built-in LingTai backend
    also starts them as task-scoped MCP clients and exposes their tools for this
    run. Claude, Codex, OpenCode, Qwen, and Kimi CLI backends additionally receive
    daemon-generated native MCP configuration for the built-in `daemon_common`;
    Claude, Codex, OpenCode, and Qwen also receive native config for
    parent-provided stdio MCP registrations. Kimi receives native config for
    parent-provided stdio and HTTP MCP registrations through its run-private
    `mcp.json`. HTTP MCP registrations remain prompt catalog context for other
    CLI backends until a backend-specific HTTP MCP config path is implemented.
    LingTai automatically adds the built-in `daemon_common` MCP to MCP-capable
    daemon backends. Its strict live-only
    `checkpoint(state, summary, artifacts?, blocker?, request?)` tool records a
    bounded nonterminal snapshot, wakes the parent, and returns any ID-bound
    parent messages exactly once. Use it at useful boundaries, not as chat,
    polling, stdin injection, or preemption. Its
    `finish(status, summary?, reason?, artifacts?)` tool remains the hard
    terminal-success contract: only `finish(status="done")` permits `done`;
    `failed`/`incomplete`, missing finish, or invalid completion prevents
    silent success. A daemon that ends without calling `finish()` is reported
    as a missing-finish failure; that is not necessarily proof the underlying
    task failed — before treating the work as lost, inspect the run's trace/result
    and the full final text preserved in the run directory's
    physical `result.txt` (the run directory is the `path` that `check`
    reports; `result_path` stays null on this failure path). Secret
    `env`/`headers` values are redacted in prompts.
  - `plugin` answers **which Agent Plugins belong to this daemon run**. It is
    optional and is an array of paths; each item may be a plugin directory
    containing `plugin.json`, or a plugin search root whose immediate children
    are plugin directories. Relative paths resolve against the parent agent
    working directory. The runtime reads each plugin manifest and injects a
    compact plugin section (name, summary, skills list, mcp list) into the
    daemon system prompt. The built-in LingTai backend also mounts the plugin's
    `skills/` as skill context and its `mcp.json` servers as task-scoped MCP
    clients, exactly like the main agent mounts plugins. CLI backends that
    cannot mount plugins yet receive the plugin's skills and mcp.json servers
    separately as normal skill/mcp oneshot context until the whole-plugin
    injection matures.
  - `task_files` answers **which local text files this daemon should read**. It
    is optional and is an array of `{path, label?, role?}` objects; `path` is a
    UTF-8 text file under the parent agent working directory (absolute or
    relative; relative paths resolve against the parent working directory). At
    dispatch the parent resolves every path, validates UTF-8 and size limits,
    and snapshots the bytes content-addressed into an immutable read-only input
    store (`daemons/_task_files/` under the parent working directory); the
    daemon receives only a compact `## Parent-provided task files` manifest
    listing label/role/sha256/size and the snapshot path for each file — never
    the file contents and never the mutable original paths, so the run keeps
    working (and can be re-checked or relaunched) even if the original file
    changes or is deleted. Malformed, out-of-root, missing, non-UTF-8, or
    oversize entries refuse the whole batch before any run starts. Up to
    `TASK_FILES_MAX_PER_TASK` files per task, `TASK_FILE_MAX_BYTES` per file.
  - `preset`: optional body/model/tool-shape override for this daemon — an
    explicit `.json`/`.jsonc` path. On the LingTai backend it must already be
    a member of the parent agent's resolved `manifest.preset.allowed` set
    (the same fail-closed normalized path check `system(action="refresh")`
    uses); an unauthorized path is refused before load/connectivity/capability
    checks, run-dir creation, scheduling, or dispatch. Being present in the
    saved/library directory is not by itself authorization — call
    `system(action="presets")` first and pass one of the exact paths it
    returns. Omitting `preset` inherits the parent's regular (non-MCP)
    effective surface instead of a fresh independent default, and does not
    perform this allowlist check at all. External CLI backends skip LingTai
    preset resolution entirely and use their own model/tools/permissions. The
    full preset runtime model is owned by `system-manual` →
    `reference/substrate-manual/SKILL.md` §11.
  - `backend_options`: raw CLI flags for CLI backends only.
- `context_token_limit`: optional context-token compaction threshold (rendered/provider-context tokens, not cumulative spend). Effective only for `backend="lingtai"` tasks whose resolved provider is Codex (`codex`/`codex-pool`) or the native `mimo` LLM provider (`manifest.llm.provider="mimo"` — NOT the `backend` enum's `mimo`/`mimocode` alias, which drives the external `mimo` CLI as a subprocess and never consults this field at all); every other provider and every external CLI backend ignores it. This threshold does not set the daemon context window. When the session's provider-visible input-token count reaches the limit, the runtime compacts provider context via that provider's standalone compaction (`POST /responses/compact`) and continues the same tool loop — the daemon keeps running; nothing restarts or drops history. Native `mimo` defaults to the stateless OpenAI Responses wire (full-history/raw-output-item replay; never `store`/`previous_response_id`/`conversation`/generic `context_management` — MiMo's Responses API marks those incompatible); an explicit `wire_api="chat_completions"` on the preset still selects the Chat Completions escape hatch instead. **Failure policy differs by provider:** a standalone-compaction failure is non-fatal for Codex (that turn's compaction is skipped; the loop continues on full history) but a HARD failure for native `mimo` (it propagates to the caller — never silently continuing on full history and never falling back to a different wire), because MiMo has no generic `context_management` fallback and no server-side state to lean on. Omit to use the daemon session's own resolved context window as the threshold: explicit preset canonical `manifest.llm.context_limit` when supplied, otherwise the inherited valid parent effective window, otherwise 272,000. Must be a positive integer; a boolean is rejected.

- `compact`: automatic for every LingTai daemon and absent from external CLI
  backends. `action` is required; omission or any value other than explicit
  `"run"`/`"manual"` is refused without changing context. For execution, call
  `compact(action="run", _reason="...")` as the sole tool call in a batch with
  a non-empty complete self-contained handoff. All previous provider-visible
  history is removed; only the compact assistant call/result pair and rebuilt
  system prompt survive. The surviving result is stamped from the fresh
  retained context, so a pre-reset >=90% warning/countdown clears. The result
  includes exact run/state/history/event paths. It is repeatable and non-terminal.
- `compact(action="manual")`: read-only procedures for compaction. It never
  resets context, requires no `_reason`, and may be called to inspect the
  procedure before deciding whether to compact. The daemon runtime starts a
  deterministic nine-round countdown on the first provider round at or above
  90%, starting at visible value `9` and decrementing through `1`. The `warning`
  and `compact_countdown_warning` fields carry one self-contained sentence for
  the current value: `Daemon context is at or above 90%. {N} proactive round(s)
  remain before runtime mechanical compact; call compact(action="run",
  _reason="...") now to compact with your own handoff.` Values decrement only
  while above `1`; value `1` remains visible through one ordinary provider
  response. On the next high response, the runtime latches expiry while
  preserving visible `1`; if that response does not issue a valid sole-call
  proactive compact, mechanical compaction occurs before the next provider
  continuation. On any drop below 90%, the countdown and warning reset/disappear.
  Mechanical compaction retains the system prompt and latest assistant/tool-result
  pair, then sends an explicit recovery message. Before continuing, re-read the
  task, inspect that preserved pair and durable run state/history/event paths,
  verify state, and only then resume; never silently continue after context loss.
  Mechanical compaction failures propagate as daemon failures.
- Every LingTai daemon tool batch ends with a canonical `_meta.agent_meta`
  snapshot on its newest final `ToolResultBlock`. Read `agent_state.daemon` for
  run/round identity, `agent_state.token_usage` for current-call and cumulative
  counters, and `agent_state.context` for provider context tokens/window/ratio.
  This is daemon-local state: parent notifications and communication context are
  intentionally absent, and no main-agent resident-guidance reference is invented.
  Only the latest `agent_meta` is current; older snapshots are historical traces.
  While current context usage is at or above 90%, every daemon round carries the
  self-contained warning sentence plus the visible countdown fields; after a
  successful compact or any other drop below 90%, the warning and countdown
  disappear. Provider-specific automatic standalone
  compaction remains independent of this daemon-owned countdown.

- Treat `task` as the parent's behavioral contract for **all** tools
  and selected skills/MCP context, not only for communication. If a daemon receives `shell`,
  say whether it may run mutating commands; if it receives file access, say what
  it may read/write; if it receives web/MCP tools, say what external calls are
  allowed; if `skills` or `mcp` are selected, say when to read/apply/call them.
  For `email` specifically — daemon-eligible but opt-in, granted only with
  `tools: ["email"]` — availability is not authorization to broadcast: state the
  allowed recipients, purpose, thread/reply discipline, information boundaries,
  whether the daemon may ask questions or only report, and how to report back to
  the parent.
- LingTai-backend daemon tool calls go through the kernel `ToolExecutor` /
  `ToolCallGuard` path before dispatch, so guarded side effects are not allowed
  to bypass normal proposal/execution policy just because they run in a daemon.
- Every `daemon.emanate` call returns a batch `group_id` shared by all daemon
  runs launched in that same call. Use `group_id` for logical batch context and
  audit. It is not a hard security boundary; use each daemon's `run_id` for
  per-run filesystem/audit identity.
- Track daemon work in the parent agent's pad, not in daemon itself. When you
  fan out multiple tasks, immediately write a small pad table after `emanate`:
  label/purpose, returned `id`, `group_id`, brief/context file path, expected
  artifact, and current status. Use `daemon(action="list", input={})` and
  `daemon(action="check", input={"id": ...})` as the mechanical truth, then update the pad
  as the parent-facing map. Daemon should stay thin; if you need durable memory
  or identity, use an avatar instead.
- Do not copy large background into every task. Put reusable context in a
  brief/report/notes file and pass that file path explicitly in the `task`
  (with file access if the daemon should read it). A follow-up daemon should
  consume visible artifacts such as the previous task prompt, result file,
  report, event summary, or context files; do not treat a daemon as a resumable
  mind or hidden-context container. Prefer making daemon history searchable and
  easy to point at over copying or reviving a daemon session.
- Each emanation is disposable memory but durable evidence: its folder persists
  after completion or reclaim until cleanup.
- `daemon(action="list", input={})` is the first layer of progressive disclosure over
  active and historical runs — compact metadata, previews, and paths, not a full
  transcript. Use the returned paths for detail; see
  `reference/cli-backends/SKILL.md` for its filters and lazy-rebuild behavior.
### Selected detached Shell async jobs

A selected `shell` in a detached LingTai daemon is **not** normal Agent Shell
notification delivery. The daemon has no parent Agent notification store,
mailbox, heartbeat, or `.notification/system.json` / `.notification/bash.json`
write. Its Shell state lives under that run's private `<run>/shell-jobs`, while
commands still use the granted task workdir; it never rehydrates or polls parent
or sibling daemon jobs. Shell reminder/completion publications become bounded,
durable events in that daemon run only. A full queue is retried by the live
selected Shell manager with capped backoff after a normal safe-boundary drain.
At a later safe provider-send boundary while the daemon is still live, fixed
guidance names the job id and tells the daemon to call `shell.poll` for exact
output. Output is not placed in the prompt.

Do not wait for an event, auto-poll, assume a parent wake per Shell job, or end
a run expecting re-entry: an event cannot hold open, restart, or resurrect a
terminal daemon. The supervisor's ordinary terminal receipt is still the only
final parent wake. This path is distinct from a `daemon_common` checkpoint and
from normal Agent Shell's `.notification` behavior.

- **Every terminal outcome is push-notified exactly once** — done, failed,
  cancelled, or timed out. After you dispatch, you can safely go IDLE and wait
  for the notification; do not poll only to ask "is it done yet". The
  notification arrives on the system channel carrying the daemon id, terminal
  status, task summary, and the result/error path. React to it with
  `daemon(action="check", input={"id": ...})` (and read `result.txt` for the full output).
- **A cooperative checkpoint is a separate nonterminal wake.** On one, call
  `daemon(action="check", input={"id": ...})` to inspect `latest_checkpoint`
  and the `pending_checkpoint_messages` count. If a correction is needed, use
  `daemon(action="ask", input={"id": ..., "message": ...})`. A
  `{status: "queued", delivery: "checkpoint", message_id: ...}` receipt means
  the message waits durably for the model's next checkpoint; it is not live
  chat or immediate CLI input. This active-run route exists only for
  `claude-p`/`claude-code`, Codex, OpenCode, Qwen, and Kimi because those exact
  launch paths mount `daemon_common`; hidden interactive Claude, MiMo,
  Oh-My-Pi, and Cursor remain `busy` while active. Qwen/Kimi still have no
  terminal resume route. Do not poll waiting for checkpoints — the model chooses
  useful boundaries, and terminal notification discipline remains unchanged.
- **Use a Task Card for progress when one is available for this turn.**
  The dispatch success `handoff` is conditional: if Telegram is connected and a
  Task Card is available for the current turn, use it to report progress — call
  `telegram(action='manual')` and follow its `Programmable Task Card` section.
  The daemon tool does not create a Task Card automatically or require a
  watcher; daemon lifecycle and terminal-notification behavior are unchanged.
  A card-worthy dispatch — two or more tasks, or an explicitly requested
  `timeout` of 900s or more — appends one extra nudge sentence to `handoff`
  when you have no active watch: start one with `task_card(action='start')` so
  a human can follow the fleet instead of watching it run dark. The nudge
  disappears once a watch is running.
- **`check` still resolves a daemon after refresh/molt.** A refresh/molt gives
  you a fresh daemon registry with no in-memory entries, but the run folders
  and their notifications survive on disk. New daemon ids are compact run ids
  such as `em-a1b2` (or `em-a1b2-1` after a collision), and
  `daemon(action="check", input={"id": ...})` exact-matches that `daemons/<run_id>/` folder
  on a registry miss. Legacy short handles such as `em-5` are accepted only when
  they resolve to one historical run; if several old runs share the handle,
  `check` returns an ambiguity error with `match_count`/`latest_run_id` instead
  of an unbounded path list. Use the exact `run_id` from the notification or
  `daemon(action="list", input={})` when a legacy handle is ambiguous.
- **Defense-in-depth, not primary signal: a self-wake guards against a daemon
  that never reaches a terminal state at all.** The terminal notification covers
  every state a run can *finish* in, but a run that hangs without the watchdog
  firing, or a degraded notification-wake path, could leave you waiting forever.
  When daemon work is pending and unverified-healthy, arm one self-wake sized to
  the task's expected duration as a backstop, then health-check on wake and
  reclaim/downgrade/switch path rather than waiting indefinitely. Do not turn
  this backstop into frequent polling. Procedure:
  `reference/inspection/SKILL.md`.
- If repeated-call `_advisory` appears on `daemon(list/check)`, the call still
  ran; treat it as a signal to stop the loop, centralize status checking in the
  parent, and read `reference/inspection/SKILL.md` before polling again.
- If an emanation might be stuck, inspect state changes, recent transcript, and
  event activity before reclaiming.
- CLI backend flags are passthroughs. Verify the current CLI's `--help` before
  relying on a flag.

### Example: separate task from behavior guidance

Put the deliverable and the daemon's operating contract together in `task`.
Use `prompt` only when LingTai needs a custom first ordinary user message:

```json
{
  "task": "Act as a documentation reviewer. Stay read-only except for the requested report file. Use the selected daemon-manual skills only when you need exact daemon semantics. Use the local-docs MCP only for daemon documentation lookup, not for unrelated search. You may use email only to ask dev-2 for missing daemon context; do not contact the human. If you email dev-2, state the exact question, include only the relevant snippet, and summarize the exchange in your final report. Do not use web tools unless the local docs are insufficient. Deliverable: audit the daemon manual changes and write a concise review to reports/daemon-manual-review.md.",
  "prompt": "Begin the documentation review.",
  "tools": ["file", "shell"],
  "mcp": [
    {"name": "local-docs", "transport": "stdio", "command": "python", "args": ["-m", "local_docs_mcp"]}
  ],
  "skills": [
    "src/lingtai/tools/daemon/manual",
    "src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md"
  ]
}
```

`tools` grants a capability surface, `skills` a selected workflow catalog, `mcp`
one-run registrations; `task` tells the daemon how to exercise all of them in
this one run.

## Maintenance

Keep this router short. Put new backend recipes, inspection examples, and cleanup
procedures in nested references so agents load only the needed detail.
