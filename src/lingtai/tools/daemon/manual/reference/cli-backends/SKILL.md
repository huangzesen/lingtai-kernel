---
name: daemon-cli-backends
description: >
  Nested daemon-manual reference for daemon API details and CLI backends:
  daemon(action=list, input={}), claude-p/codex/opencode behavior,
  backend_options flag passing, preset/capability inheritance, and nested
  per-backend references (Codex, OpenCode, claude-p, MiMo Code, Qwen Code,
  Kimi Code, Cursor, and Oh-My-Pi flag discovery, built-in LingTai knowledge entrypoint).
version: 1.17.1
last_changed_at: 2026-09-04T00:00:00Z
related_files:
- src/lingtai/tools/daemon/manual/SKILL.md
- src/lingtai/tools/daemon/CONTRACT.md
- src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/lingtai/SKILL.md
- src/lingtai/tools/daemon/manual/reference/dispatch-ledger/SKILL.md
maintenance: |
  Tracks the daemon CLI backends topic it documents; update when that integration changes.
---

# Daemon CLI Backend Reference

Nested daemon-manual reference. Open this when choosing a daemon backend,
inspecting `daemon(action="list", input={})`, or passing CLI flags through `backend_options`.

## Nested reference catalog

`daemon-cli-backends` owns these nested references. They are parent-owned
drill-down files, not standalone top-level skills. Backend-specific pages live
under `reference/backends/<backend>/` — despite this router's historical
`cli-backends` path, the built-in `lingtai` backend's page lives here too.
Each page is a small knowledge entrypoint that routes to the current
authority — a CLI backend's installed live help, or the built-in backend's
live manuals/preset/contract sources — never a maintained flag or rules
catalog. Only backends with proven demand get a page.

```yaml
- name: daemon-backend-codex
  location: reference/backends/codex/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the Codex backend's flag surface.
    Read this only when a daemon task needs Codex-specific CLI flags: model
    selection, reasoning effort, repeated `--config` overrides.
- name: daemon-backend-opencode
  location: reference/backends/opencode/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the OpenCode backend's flag
    surface. Read this only when a daemon task needs OpenCode-specific CLI
    flags: `--model provider/model`, `--variant`, agent choice, the reserved
    `--format` boundary.
- name: daemon-backend-claude-p
  location: reference/backends/claude-p/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the claude-p (alias claude-code)
    backend's flag surface. Read this only when a daemon task needs Claude
    Code-specific CLI flags (model, `--fallback-model`, tool restrictions), the
    reserved-flag boundary, resume, or auth-env hygiene.
- name: daemon-backend-mimocode
  location: reference/backends/mimocode/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the MiMo Code (`mimocode` /
    `mimo`) backend's flag surface. Read this only when a daemon task needs
    MiMo-specific CLI flags, the reserved session flags, or the verified JSONL
    answer/error and usage contract.
- name: daemon-backend-qwen-code
  location: reference/backends/qwen-code/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the Qwen Code (`qwen-code` /
    `qwen`) backend's flag surface. Read this only when a daemon task needs
    Qwen-specific CLI flags, the reserved `--prompt`/`--yolo`/`--approval-mode`
    boundary, or the no-`ask` planning constraint.
- name: daemon-backend-kimicode
  location: reference/backends/kimicode/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the Kimi Code backend's flag
    surface. Read this only when a daemon task needs Kimi-specific CLI flags,
    the exact reserved harness flags, the run-private `mcp.json` loader, or the
    ask/resume limitation.
- name: daemon-backend-cursor
  location: reference/backends/cursor/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the Cursor backend's flag surface.
    Read this only when a daemon task needs Cursor-specific `agent` CLI flags,
    the harness-owned surfaces, or the source-pinned stream-json usage contract.
- name: daemon-backend-deepseek
  location: reference/backends/deepseek/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the DeepSeek Harness (`deepseek`)
    backend's flag surface. Read this only when a daemon task needs deepseek
    launcher-level flags (`--patch` overlays), the run-private `$DSH_HOME`
    caveat, or the no-`ask`/resume constraint.
- name: daemon-backend-oh-my-pi
  location: reference/backends/oh-my-pi/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the Oh-My-Pi (`omp`) backend's
    flag surface. Read this only when a daemon task needs Oh-My-Pi-specific CLI
    flags, the exact harness-reserved mode/approval/session flags, or its
    session/ask/MCP status.
- name: daemon-backend-lingtai
  location: reference/backends/lingtai/SKILL.md
  description: |
    Nested daemon-cli-backends reference for the built-in `lingtai` backend
    (the in-process ChatSession default). Read this when routing a task to the
    built-in backend: it has no CLI and no `backend_options` surface, and the
    page routes to the live authorities for preset selection/inspection,
    tools/skills/MCP inheritance, and the completion contract.
```

## Routing table

| Need / keywords | Read |
|---|---|
| Codex-specific flags for a daemon task: model selection, reasoning effort (`model_reasoning_effort`, ultra), `--config` overrides; discover the installed Codex CLI's flags and translate them into `backend_options` | `reference/backends/codex/SKILL.md` |
| OpenCode-specific flags for a daemon task: model selection (`--model provider/model`), reasoning variant (`--variant`), agent choice; discover the installed OpenCode CLI's flags and translate them into `backend_options` | `reference/backends/opencode/SKILL.md` |
| Claude Code-specific flags for a `claude-p` / `claude-code` daemon task: model selection, `--fallback-model`, tool restrictions; reserved/harness-owned flag boundary, resume and auth-env behavior; discover the installed Claude CLI's flags and translate them into `backend_options` | `reference/backends/claude-p/SKILL.md` |
| MiMo Code-specific flags for a daemon task (`mimocode` / `mimo`): model selection, provider switches; discover the installed `mimo` CLI's flags (`mimo run --help`) and translate them into `backend_options` | `reference/backends/mimocode/SKILL.md` |
| Qwen Code (`qwen-code` / `qwen`)-specific flags for a daemon task: model selection, provider tunables, reserved `--prompt`/`--yolo`/`--approval-mode` boundary, no-`ask` planning; discover the installed `qwen` CLI's flags and translate them into `backend_options` | `reference/backends/qwen-code/SKILL.md` |
| Kimi Code (`kimicode` / `kimi`)-specific flags for a daemon task: model selection (`--model`), skills/workspace dirs; reserved harness flags, run-private `mcp.json` MCP loader, why `ask`/resume is unsupported; discover the installed Kimi CLI's flags and translate them into `backend_options` | `reference/backends/kimicode/SKILL.md` |
| Cursor-specific flags for a daemon task: model selection, output/tooling switches; discover the installed Cursor Agent CLI's (`agent`) flags and translate them into `backend_options` | `reference/backends/cursor/SKILL.md` |
| DeepSeek Harness (`deepseek`)-specific flags for a daemon task: launcher-level options (`--patch` overlays), the reserved `--profile headless` boundary, run-private `$DSH_HOME`, and why `ask`/resume is unsupported; discover the installed `dsh` CLI's flags and translate them into `backend_options` | `reference/backends/deepseek/SKILL.md` |
| Oh-My-Pi (`omp`)-specific flags for a daemon task: model selection, tool/provider switches, the harness-reserved mode/approval/session flags; discover the installed `omp` CLI's flags and translate them into `backend_options` | `reference/backends/oh-my-pi/SKILL.md` |
| Built-in `lingtai` backend knowledge for a daemon task: confirm it has no CLI/`backend_options` flag surface; find the live authorities for preset selection/inspection, lingtai/tools/skills/MCP inheritance, and the completion contract | `reference/backends/lingtai/SKILL.md` |

Each backend page also carries a small `## Subscription & auth` section:
what the vendor subscription/account model is, how LingTai connects (login,
API-key env, or config file), and the official docs link — enough to answer
"what do I need to pay for / how does LingTai authenticate" per backend
without reading the vendor's full billing docs.

## API note: `daemon(action="list", input={})`

`list` tails the agent-local append-order dispatch ledger (see
`reference/dispatch-ledger/SKILL.md`), not a folder scan. It returns
completed, failed, cancelled, timed-out, and running entries with `run_id`,
`group_id`, `status`, `backend`, task preview, visible call parameters
(`task`, `tools`, `prompt`, `skills`, redacted `mcp`, system-prompt preview
when recorded), result preview, and filesystem paths, each read from the
ledger-selected run's own `daemon.json`. It does not enumerate historical run
folders and does not lazily create or repair a missing or invalid
`daemon.json`; inspect a known legacy run (one predating the ledger, or with
a damaged file) directly with `daemon(action="check", input={"id": "em-N"})` or
manual filesystem inspection instead — an omitted `list` entry does not mean the run was
deleted or failed. Use `contains` for
case-insensitive substring search over that visible index, `status` for status
filtering, and `last` as a positive result limit: omitted or null returns the
newest 1000 rows, while an explicit value (including one above 1000) is honored.
The deferred materialization benefit applies when `contains` is absent or empty;
a non-empty `contains` searches prompt-preview text and therefore preserves full
candidate materialization before filtering and paging. Use `include_done=false` when
you only want currently tracked in-memory runs. This is the first layer of progressive disclosure; read the returned
`.prompt`/`result.txt` paths for detail, and grep `events.jsonl` /
`chat_history.jsonl` / `token_ledger.jsonl` only for forensic depth.


## Bash harness subskills

Ownership note: per-backend operational guides for the coding CLIs have moved
here from the bash manual. `shell` remains a fully supported way to run these
CLIs — the async + poll supervision discipline lives in `shell-manual` — but
each CLI's command shape, flags, env contracts, and caveats are owned by this
page's per-backend references below (`reference/backends/<backend>/SKILL.md`),
which **supersede the old `reference/bash-*/SKILL.md` guides** in the bash
manual.

All 10 shipped daemon backends have drill-down pages under
`reference/backends/`. CLIs that are not daemon backends are not documented
here. `shell-manual` owns only the shell-side supervision discipline
(async + poll, reminders, scheduling, debugging, cleanup) that applies no
matter which CLI you run.

## Backend promotion gate

Several pages document a CLI that is *not* a daemon backend yet. A backend
needs all of:

- a deterministic non-interactive start command;
- a stable session id plus a tested resume command, for `ask`/resume;
- JSON/JSONL output, or a transcript parser whose output contract is pinned by
  tests.

If any is missing, keep the CLI as a documented shell harness rather than
adding a speculative daemon backend.

## Generic validation checklist

For any coding CLI before relying on it in automation:

1. `command -v <binary>` succeeds, or the documented installation path exists.
2. `--help` confirms the non-interactive command and approval flags.
3. A dry-run in a disposable worktree exits non-interactively.
4. If promoted to a daemon backend, tests mock subprocess launch, output
   parsing, session capture, resume/ask, and reserved `backend_options`
   handling.

## CLI backends

The `backend` parameter selects the execution engine for emanations. Default is
`lingtai` (the built-in ChatSession loop). External CLI backends are also
available:

**Claude backend naming.** For Claude Code daemon work select `claude-p`, the
print-mode backend that wraps Claude Code's official `--print`/stream-json mode;
`claude-code` is a compatibility alias with the same runner and reserved-flag
set. The legacy interactive PTY/TUI names (`claude` / `claude-interactive`) are
**no longer user-selectable** — hidden from the daemon schema enum and
description. Do not select them for new work; that path proved unreliable under
the daemon watchdog (mid-exploration SIGTERM / exit code 143). The code path
remains in the tree only so older callers and stored entries that recorded
`backend="claude"` keep resolving.

> **MCP completion contract.** MCP-capable daemon backends load LingTai's
> built-in `daemon_common` MCP for every run. The model must call the MCP
> `finish(status, summary?, reason?, artifacts?)` tool before ending:
> `status="done"` is the only signal that permits `mark_done`; `failed`,
> `incomplete`, a missing finish call, or an invalid completion file makes the
> run **failed** while preserving the final text/error for inspection.
> Background-and-wait is invalid: run required validation synchronously with an
> adequate explicit timeout and inspect the result in the same run.

| Backend | CLI command | Session resume | Notes |
|---------|-------------|----------------|-------|
| `lingtai` | (built-in) | N/A — in-process `ask` | Default. Uses preset resolution, tool surface curation, model routing. |
| `claude-p` | `claude --print --dangerously-skip-permissions --output-format stream-json --verbose --name <em_id> --mcp-config <run>/claude-mcp-config.json --strict-mcp-config <task>` | `claude --resume <claude_session_id> --print ...` via `ask` (async — returns immediately, reply arrives via notification / `check`) | Print-mode Claude Code backend (the recommended Claude backend). Wraps Claude Code's official `--print`/stream-json mode and loads the per-run `daemon_common` MCP. Session ID is captured from stream-json output, so `ask` is usable as soon as `emanate` returns. |
| `claude-code` | same as `claude-p` | same as `claude-p` | Backward-compatible alias retained for existing callers and stored daemon entries. |
| `codex` | `codex exec --json --dangerously-bypass-approvals-and-sandbox -c mcp_servers.daemon_common.command=... -c mcp_servers.daemon_common.args=... -c mcp_servers.daemon_common.env=... <task>` | `codex exec resume <codex_session_id>` via `ask` (async — returns immediately, reply arrives via notification / `check`) | Mirrors the print-mode Claude backend. `thread.started` event carries the session id (codex internally calls it `thread_id`), captured immediately. The per-run `daemon_common` MCP is injected through Codex config overrides. |
| `opencode` | `OPENCODE_CONFIG_CONTENT=<per-run daemon_common config> opencode run --format json <prompt>` | `opencode run --session <opencode_session_id> ...` via `ask` (async) | Uses opencode's session id/event vocabulary. The daemon injects the built-in completion MCP through OpenCode's per-process config content environment. |
| `mimocode` / `mimo` | `mimo run --format json <prompt>` | `mimo run --session <mimocode_session_id> --format json ...` via `ask` (async) | MiMo Code CLI backend (npm package `@mimo-ai/cli`, binary `mimo`). `mimo` canonicalizes to `mimocode`. Per-run MCP injection is not wired here yet because no local binary/docs path in this workspace confirmed a MiMo-specific config override compatible with LingTai's wrapper. |
| `qwen-code` / `qwen` | `QWEN_CODE_SYSTEM_SETTINGS_PATH=<run>/qwen-daemon-settings.json qwen --yolo -p <prompt>` | Not supported yet; `ask` returns an explicit unsupported-backend error | Qwen Code CLI backend (npm package `@qwen-code/qwen-code`, binary `qwen`). `qwen` canonicalizes to `qwen-code`. The per-run settings file contains `mcpServers.daemon_common`. |
| `oh-my-pi` / `omp` | `omp --mode json --approval-mode yolo <prompt>` | `omp --mode json --approval-mode yolo --session <oh_my_pi_session_id> ...` via `ask` (async) | Oh-My-Pi pi-coding-agent CLI backend (npm package `@oh-my-pi/pi-coding-agent`, binary `omp`). `--mode json` is non-interactive JSON event-stream print mode; the first `type:session` header line carries the resumable session id. `omp` canonicalizes to `oh-my-pi`. Per-run MCP injection is not wired yet pending evidence of its accepted config/env path. |
| `kimicode` / `kimi` | `KIMI_CODE_HOME=<run>/kimi-code-home kimi --prompt <prompt> --output-format text` (same per-run env: telemetry/auto-update off, `KIMI_MODEL_API_KEY` mapped from `KIMICODE_API_KEY`/`KIMI_API_KEY`/`MOONSHOT_API_KEY` when unset, provider/base-url/model/context defaults only when absent) | Not supported yet; `ask` returns an explicit unsupported-backend error | MoonshotAI Kimi Code CLI backend (official `MoonshotAI/kimi-code`, binary `kimi`, source-verified v0.22.3 for MCP config). `kimi` canonicalizes to `kimicode`. LingTai owns `--prompt`/`--output-format` and forbids `--yolo` (the CLI refuses `--prompt` + `--yolo`); session/`--continue` flags are reserved because resume is not wired. Stable session-id output was not verified, so `ask`/resume is intentionally unsupported. The daemon writes `<run>/kimi-code-home/mcp.json` with `daemon_common` plus parent stdio and HTTP MCP registrations; secret env/header values stay out of prompts/logs and live only in the native per-run config. |
| `deepseek` | `DSH_HOME=<run>/dsh-home DSH_TELEMETRY_DISABLED=1 dsh --profile headless <prompt>` | Not supported yet; `ask` returns an explicit unsupported-backend error | Official DeepSeek Harness CLI backend (npm package `@deepseek-ai/dsh`, binary `dsh`; **developer preview** — upstream may break compatibility). Runs the shipped headless one-shot profile: stdout gets the final assistant text and exit 0 means the session completed. LingTai owns the `--profile headless` launcher flags and pins a run-private `$DSH_HOME` so first-use profile auto-initialization never touches the operator's real home; telemetry is hard-disabled. `daemon_common` MCP injection is not wired yet (no evidence-backed dsh config path in this slice). Only launcher-level `backend_options` flags (e.g. `--patch`) are meaningful; see the child page. |
| `cursor` | `agent -p <prompt>` | `agent -p --resume <cursor_session_id> ...` via `ask` (async) | Cursor Agent CLI backend. Per-run MCP injection is not wired yet; local `agent --help` could not be inspected in this environment because the CLI attempted macOS keychain access and failed before printing help. |

**Per-task mapping.** The task-shape contract (`task` / `prompt` / `tools` /
`skills` / `mcp`) is owned by the `daemon-manual` router. The backend-dependent
part: for the built-in `lingtai` backend the complete `task` is compiled into the
daemon system prompt beside selected skills/MCP/tool guidance, and it cannot
override lifecycle limits, tool schemas, or the ToolExecutor/ToolCallGuard
execution gate. For external CLI backends `task` is the exact CLI prompt,
persisted in the daemon `.prompt` file for forensics, and a task containing
`prompt` is rejected before run-dir creation (`prompt` is LingTai-only).

**LingTai backend tool surface.** The built-in `lingtai` backend uses preset
resolution plus daemon tool curation. Parent MCP tools are not auto-inherited:
provide full one-run MCP registrations per task with `mcp: [{name, transport,
...}]`. The runtime serializes those registrations into the oneshot prompt as
YAML for every backend; for the built-in LingTai backend it also starts the
registered MCP clients for this run and exposes their tools in the daemon tool
surface, closing them when the run finishes. Secret `env`/`headers` values are
redacted in prompts. Beyond the daemon-eligible, opt-in `email` intrinsic, other
intrinsics remain unavailable to keep daemon lightweight and non-recursive.

> **MCP tool-name uniqueness.** Tool names exposed by MCP registrations must be
> unique within a run. If a plugin `mcp.json` server and a task-level `mcp`
> registration (or two registrations of either kind) expose the same tool name,
> dispatch fails immediately with `ValueError: duplicate MCP tool name` before any
> work starts — rename or dedupe one of the registrations. This is by design: both
> injection paths are live at the same time, so same-server duplicates collide.

Where each backend receives its native `daemon_common` MCP config:

| Backend | Injection path |
|---|---|
| `claude-p` / `claude-code` | per-run `--mcp-config` file |
| `codex` | `-c mcp_servers.daemon_common.*` overrides |
| `opencode` | `OPENCODE_CONFIG_CONTENT` environment variable |
| `qwen-code` | per-run settings file path |
| `kimicode` | run-private `mcp.json` loader (stdio **and** HTTP registrations) |
| `mimocode`, `oh-my-pi`, `cursor`, `deepseek` | **not wired yet** — documented or plausible MCP entrypoints still needing daemon-native config and tests; not declared unsupported |

**When to use CLI backends:** Use them when the task benefits from a different
agent runtime's tool surface (for example Claude Code's built-in file editing or
Codex's sandboxed execution) rather than the LingTai emanation's curated tool
set. `mimocode`/`mimo`, `qwen-code`/`qwen`, `oh-my-pi`/`omp`, and `kimicode`/`kimi` are accepted as canonical backend names plus short aliases; persisted daemon entries use the canonical name.

**Claude Code auth environment hygiene.** The print-mode Claude backend strips
auth override variables from the subprocess environment, so daemon runs are
already protected; a *manual* `claude` shell invocation is not. For the exact
variables, the weekly-limit smoke test, and how to find a stale token's source,
read `reference/backends/claude-p/SKILL.md`. Never print token values while
diagnosing.

**CLI backends skip preset resolution** — the external CLI manages its own model,
tools, and permissions. The `tools` field in the task spec is ignored for CLI
backends.

## Passing free-form CLI flags via `backend_options`

For CLI backends, each task may carry an optional `backend_options` JSON object
that is converted to argv tokens and appended to the CLI command before the task
prompt. This lets you reach the underlying CLI's flag surface (model selection,
search/web access, effort levels, sandbox/policy switches, etc.) without the
daemon needing to hard-code every flag. This is the default generic path for
backend flags: one object may carry any number of keys, each converted
independently in insertion order. Per-backend discovery and translation examples
live on each backend's page — see the routing table above.

This is intentionally a passthrough, not a fixed table. Every wrapped CLI revs
its flag list between releases, so before adding new options run the installed
CLI's `--help` in `shell` (`claude --help`, `codex exec --help`,
`opencode run --help`, `mimo run --help`, `qwen --help`, `omp --help`,
`kimi --help`, or `agent --help`). Anything here is illustrative, not
authoritative.

```jsonc
// Print-mode Claude backend (the recommended Claude backend)
{
  "action": "emanate",
  "backend": "claude-p",
  "tasks": [{
    "task": "Review this PR and summarize risks.",
    "tools": [],
    "backend_options": {
      "model": "claude-opus-4-7"
    }
  }]
}

// Codex with model + web search
{
  "action": "emanate",
  "backend": "codex",
  "tasks": [{
    "task": "Find the breaking change in the last release.",
    "tools": [],
    "backend_options": {
      "model": "gpt-5",
      "search": true
    }
  }]
}
```

**Conversion rules** (validated before any process is spawned — a single bad
spec refuses the whole batch with a clear `ValueError`):

| Value type | Result |
|---|---|
| `true` | flag only, e.g. `{"search": true}` → `--search` |
| `false` / `null` | flag omitted entirely |
| string / int / float | `--flag <value>` |
| list of scalars | repeated: `--flag v1 --flag v2 ...` |
| nested object / array of objects | **rejected** with `ValueError` (except the reserved `env` key) |

**Reserved `env` overlay.** `backend_options.env` is the one key that is not a
flag: a JSON object of string → string injected into the spawned CLI
subprocess's environment (names must match `[A-Za-z_][A-Za-z0-9_]*`, values must
be strings). It emits no argv token and applies to every CLI backend; the
overlay is merged last, so it wins over both the inherited environment and the
harness-owned per-backend env. For `claude-p` / `claude-code` this is how you
select a Claude profile — `{"env": {"CLAUDE_CONFIG_DIR": "..."}}`; see
`reference/backends/claude-p/SKILL.md`. Like the rest of `backend_options`, it
applies only to `emanate`, not to `ask`.

**Key safety:** keys must look like CLI flag names (letters/digits with `-` or
`_` separators, no leading `-`, no spaces). Underscores in keys are converted to
dashes in the emitted flag, so JSON-friendly `{"approval_policy":"never"}`
becomes `--approval-policy never`. Unsafe keys are rejected before any subprocess
call.

**Do not set a CLI backend's `max_turns` too low.** A turn budget that fits a
quick scripted task will kill a Claude Code / Codex backend mid-exploration,
surfacing as **exit code 143** (SIGTERM) with little or no useful output. The
exploration-then-act shape of these agents means early turns are spent on reads
and greps, not the deliverable; budget for that. Size any cap to the *full* task
(explore + act + verify), not to a single edit, and prefer leaving `max_turns`
unset over guessing low. For how to read and report a 143, see
`../forensics/SKILL.md`.

**Reserved flags are per backend.** Each backend refuses its own harness-owned
flags in `backend_options` before spawn — one bad key refuses the whole batch.
The exact list lives on that backend's page under "Harness boundary"; consult it
before adding options.

**When it applies:** `backend_options` is honored only at `emanate` time (when
the CLI session is first spawned). `daemon(action="ask", input={"id": ..., "message": ...})` reuses the
existing session via `claude --resume` / `codex exec resume` / backend-specific
resume and does not re-pass `backend_options` — the runtime flags chosen at
emanate time persist for the life of the session.

**Where it shows up on disk:** user-supplied options are written into the
emanation's `daemon.json` as `backend_options` (the raw object) and
`backend_argv` (the converted user argv tokens). Harness-owned MCP/config flags
are written separately as `backend_harness_argv`, and the runner receives
`backend_argv + backend_harness_argv`. This separation prevents run artifacts
from implying the model supplied the completion-MCP loader flags. The parent
also logs `daemon_backend_options` with separate `argv` and `harness_argv`
fields in `logs/events.jsonl`.

`lingtai` backend ignores `backend_options`: there is no CLI process to forward
it to.

## Progress, resume, and `ask`

**Working directory:** CLI backends run in the parent agent's working
directory (`_working_dir`), not in the emanation's `daemons/em-N-*/` folder. The
`daemons/` folder still tracks daemon state (`daemon.json`, logs) and terminal
output (`result.txt`).

**Progress delivery:** CLI stdout/stderr and parsed transcript events are
persisted to the run directory as `cli_output` events and
`daemon.json.last_output`; they are not injected into the parent as ordinary
`[daemon:em-N]` request text. Completion/failure publishes one compact system
notification.

**`ask` on CLI backends is asynchronous.** For resumable CLI emanations,
`daemon(action="ask", input={"id": "em-N", "message": "..."})` spawns/resumes the backend and
returns immediately with `{"status":"sent","async":true}`. Progress streams
into the same run directory (`cli_output` events, `last_output`); the final reply
text arrives as a `follow-up completed` system notification and is also visible
via `daemon(action="check", input={"id": "em-N"})`. Poll `check` (or wait for the
notification) instead of expecting the reply in the `ask` return value.

While one CLI `ask` is in flight against a given emanation, a second `ask` to the
same id returns `{"status":"busy", ...}`. CLI sessions serialize per session, so
wait for the first follow-up to complete before sending another. The `lingtai`
backend's `ask` is unchanged: it buffers into a per-emanation followup buffer and
is drained by the in-process run loop.

## Backend-specific observability

**Legacy interactive Claude (`claude` — hidden).** Older stored runs may still
carry `claude_interactive_*` fields in `daemon.json` (managed-workspace path,
transcript path, raw PTY log, trust-answer flag, etc.); they are kept only for
forensics on historical runs.

**Print-mode Claude (`claude-p` / `claude-code`).** `claude_session_id` is set on
the first stream-json event that carries a session id (typically the system
`init` event, within milliseconds of process start). Earlier versions wrote the
session id only post-hoc by scanning `~/.claude/projects/`; that scan remains a
fallback if the stream never carries a session id.

**Codex.** `codex_session_id` (stored as `daemon.json.codex_session_id`) is set
on the first event — `{"type":"thread.started","thread_id":"<uuid>"}` — within
milliseconds of process start. `ask` runs `codex exec resume <codex_session_id>
--json "<message>"` asynchronously.

**Token accounting:** external CLI token/spend fields are deliberately not mixed
into the parent/kernel token ledger. They use separate billing paths and cache
semantics. Spend/progress remains visible through daemon run artifacts.
