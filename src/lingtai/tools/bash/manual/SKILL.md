---
name: shell-manual
description: >
  **Read before running a long-lived agent/coding CLI as a shell subprocess**,
  or before setting up cron/launchd/systemd timers or scheduled reminders.
  Routes shell-side async+poll supervision, host-scheduler setup, LingTai
  wake-by-mailbox-drop, one-shot reminders, and safe cleanup. Per-backend CLI
  operational detail (command shapes, flags, env contracts) for daemon-backed
  CLIs lives in `daemon-manual` → `reference/cli-backends/SKILL.md`.
version: 1.13.0
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/bash/__init__.py
- src/lingtai/tools/bash/_tool_family.py
- src/lingtai/tools/bash/CONTRACT.md
- src/lingtai/tools/bash/ANATOMY.md
- tests/test_shell_settings.py
- src/lingtai/tools/bash/manual/reference/async-jobs/SKILL.md
- src/lingtai/tools/bash/manual/reference/scheduled-work/SKILL.md
- src/lingtai/tools/bash/manual/reference/notification-reminders/SKILL.md
- src/lingtai/tools/bash/manual/reference/debugging-cleanup/SKILL.md
- src/lingtai/tools/daemon/manual/SKILL.md
- src/lingtai/tools/daemon/shell_prompt_events.py
maintenance: |
  Tracks the shell capability's short operational router and its nested
  references; update the route and the owning reference when shell guidance
  changes.
---

# Shell Manual — Router

The `shell` tool executes one host command at a time. This manual is the
progressive-disclosure entrypoint: keep first-call and safety rules here, then
open the focused reference for durable async jobs, host scheduling, one-shot
reminders, or debugging/cleanup. For ordinary short, deterministic commands,
use the schema synchronously; for anything long-running, scheduled, or
failure-prone, read the matching route first.

## Routing table

| Need / keywords | Read |
|---|---|
| Run a long-lived command or coding CLI without blocking the turn; `job_id`, `poll`, `cancel`, reminder, completion wake, or relaunch | [async jobs](reference/async-jobs/SKILL.md) |
| "Every hour", "daily", "weekdays at 9", or any other time-driven recurring work | [scheduled work](reference/scheduled-work/SKILL.md) |
| A single future nudge while work is pending | [notification reminders](reference/notification-reminders/SKILL.md) |
| A scheduler is silent, fires twice, exits immediately, or must be retired | [debugging and cleanup](reference/debugging-cleanup/SKILL.md) |
| Backend-specific coding-CLI flags, environment, and parser caveats | `daemon-manual` → `reference/cli-backends/SKILL.md` |

The async reference supersedes the former long coding-CLI and durable-lifecycle
block in this router. The three host/scheduler references remain the owners of
their recipes; do not duplicate their examples here.

## First-call decision tree

1. **Short deterministic host work** (for example `ls`, `git status`, `grep`, or
a quick bounded build): call `shell(action="run", input={"command": "..."},
reasoning="...")` synchronously.
2. **Anything that may run for minutes**: never block the turn. Call `run` with
`input.async=true`, then follow the async reference and poll with the returned
ID. A coding CLI is not a LingTai daemon; read `daemon-manual` when choosing
between a direct CLI and a daemon backend.
3. **Time is the trigger**: read `reference/scheduled-work/SKILL.md`.
4. **One future self-wakeup**: read `reference/notification-reminders/SKILL.md`.
5. **Existing scheduler trouble or retirement**: read
`reference/debugging-cleanup/SKILL.md` before editing it.

## Settings inventory

`shell(action="settings", input={}, reasoning="inspect applied Shell settings")`
is read-only progressive disclosure. It returns only rows with `key`, `current`,
`default`, `configurable`, and the exact section pointer in `comment`. The input
is strict empty: there is no set/reset mutation form and Shell owns no settings
file. Read the matching section before changing a value through its existing
owner procedure, then call `settings` again. If any current value is
unavailable, the whole action returns `SETTINGS_UNAVAILABLE` without partial
rows.

### Shell kind

`shell_kind` is the active command-language adapter: `posix`, `powershell`,
`cmd`, `gitbash`, or `wsl`. An authorized owner may configure it. Precedence is
a valid capability value, then valid case-insensitive `LINGTAI_SHELL`, then
platform discovery; invalid values fall through. The projected default is
`null` because discovery has no universal fallback. Change the capability or
launcher configuration (or call `setup(..., shell_kind="<kind>")`), rebuild or
relaunch the owning Agent, and verify with SHOW. Dialect selection never bypasses
command policy or the working-directory boundary.

### Sync timeout default

`sync_timeout_default_seconds` is the built-in 30-second default when sync
`run` receives `input.timeout=null` or omits it through compatibility handling.
It is built-in-only and not family-configurable. A finite, non-negative timeout
up to the live ceiling applies to one call only; a later SHOW still reports 30.

### Sync timeout ceiling

`sync_timeout_max_seconds` is the hard synchronous `input.timeout` ceiling. Its
owner environment key is `LINGTAI_TOOL_TIMEOUT_MAX_SECONDS`: a positive finite
value wins over 120; missing, blank, non-numeric, non-positive, or non-finite
values use 120, and values below 30 are floored to 30. The value is read for
each SHOW and sync run. Change it only in the authorized process/launcher
environment (relaunch when that environment is snapshotted). Work needing more
than the ceiling must use `input.async=true`.

### Result size limit

`result_max_chars` is the per-stream stdout/stderr capture limit. The built-in
is 50000; an authorized embedding owner may pass a positive `max_output` to
`ShellManager(...)` when constructing it. Normal setup exposes no `max_output`
key or environment setting. Rebuild the manager and SHOW again to verify. This
limit changes result disclosure only; it grants no command or filesystem
authority.

### Async default

`async_default` is the built-in `false` when `run` does not select a mode. It has
no config or environment key. `input.async=true` or `false` selects one call
only and does not change a later SHOW.

### Async reminder default

`async_reminder_default_seconds` is the built-in 1800-second last-resort wake
delay for async `run` without an explicit reminder. It has no config or
environment key. For one async run, pass a finite non-negative `input.reminder`
within the platform timer bound; that value does not change the default.

### Command policy

`command_policy` is the security-sensitive allowlist, denylist, or allow-all
policy bound to this Shell owner. Both `current` and `default` are always
`<redacted>`; policy paths and rules never enter SHOW. Precedence is capability
`yolo=true`, then `policy_file`, then the platform-packaged policy. An
authorized owner changes these through existing capability/setup procedures.
SHOW has no mutation authority and remains redacted on every call.

## Reading command results — never trust top-level `status` alone

A completed result's top-level `status` (`ok`/`done`) says only that the shell
spawned the command, not that the command succeeded. Always check `exit_code`
and `ok`; `command_status` is `success` or `failed`. Read `warning` whenever it
is present: it can identify a nonzero exit, Python traceback, missing module,
or a bounded redacted stderr tail. Raw `stdout` and `stderr` remain available.
A still-running poll has no `exit_code` and therefore no fidelity fields.

For project code, use the repository/approved virtualenv interpreter rather
than bare `python3`; a `No module named …` warning usually means the wrong
interpreter was selected. The top-level `status: "error"` is reserved for the
shell failing to run the command (validation, policy, timeout, or spawn error),
so do not rewrite inner command failure as a transport error.

## Avoid broad recursive scans and malformed log parsing

Unbounded `find … -name/-path/-type`, `Path(...).rglob(...)`, `os.walk(...)`, or
`glob('**/...')` scans over large roots commonly time out. Prefer a bounded root
and `rg --files --hidden -g '!**/{.git,node_modules,daemons,.worktrees}/**'`
then filter the list; narrow the root or add `-maxdepth` when appropriate.
The timeout result may include an advisory `rg --files` recipe, but it never
rewrites the command.

`events.jsonl` and daemon logs are JSON Lines, not one JSON document. Iterate
non-empty lines with `json.loads`, or filter with `jq -c`/`rg`; use `tail -n`
when only recent evidence is needed.

## Core async rule and first follow-up

A long-running command, coding CLI, or sub-agent must **never** run
synchronously. Use `input.async=true`; the immediate result contains a
`job_id`, and later calls use the registered envelope, not a flat legacy shape:

```text
shell(action="run", input={"command": "<long command>", "async": true,
                             "reminder": 1800}, reasoning="start it")
shell(action="poll", input={"job_id": "job-<id>"}, reasoning="check it")
```

The run-only fields (`command`, `timeout`, `working_dir`, `async`, `reminder`)
live only in `run`'s `input`; `poll` and `cancel` take only `job_id`. `null`
means absent for nullable run options: timeout defaults to 30 for sync, async
defaults to false, and reminder defaults to 1800. The `working_dir` must remain
inside the active sandbox; for an external checkout keep the granted agent
working directory and use an explicit `cd /absolute/path && ...` in `command`.
Before ordinary shell work, read this manual; no exception. Open
`reference/async-jobs/SKILL.md` for durable leases, reminder/completion wakes,
relaunch-safe status, cancellation, detached-daemon behavior, and coding-CLI
harness detail.

## Host scheduling and cleanup routes

LingTai has no built-in recurring scheduler. Host schedulers wake agents by
producing channel input, usually a mailbox drop. Prefer event watchers when an
external event is the real trigger; use cron/launchd/systemd only when time is
the trigger or polling is the right tradeoff. Scheduler scripts must be
idempotent, audited, logged, absolute-path based, and explicit about how they
wake the agent. On macOS, read the scheduled-work reference's launchd
process-tree warning. Do not leave silent janitors or hidden recurring jobs;
read the debugging reference before retirement. A shell-created artifact is
not automatically safe to delete: retain work unless the human explicitly
approves a dry-run and cleanup plan.

## Maintenance

Keep this top-level document a short router. Put detailed async lifecycle,
backend flags, scheduler recipes, notification payloads, and troubleshooting
walkthroughs in the focused references above so agents load only the depth they
need. Preserve the exact settings headings because SHOW comments link to them.
