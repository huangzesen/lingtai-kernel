---
name: shell-manual
description: >
  **Read before running a long-lived agent/coding CLI as a shell subprocess**,
  or before setting up cron/launchd/systemd timers or scheduled reminders.
  Routes shell-side async+poll supervision, host-scheduler setup, LingTai
  wake-by-mailbox-drop, one-shot reminders, and safe cleanup. Per-backend CLI
  operational detail (command shapes, flags, env contracts) for daemon-backed
  CLIs now lives in `daemon-manual` → `reference/cli-backends/SKILL.md`, which
  supersedes the old bash reference guides — this manual owns only the
  shell-side supervision discipline.
version: 1.12.1
last_changed_at: 2026-09-05T00:00:00Z
related_files:
- src/lingtai/tools/bash/__init__.py
- src/lingtai/tools/bash/_tool_family.py
- src/lingtai/tools/bash/CONTRACT.md
- src/lingtai/tools/bash/ANATOMY.md
- tests/test_shell_settings.py
- src/lingtai/tools/bash/manual/reference/scheduled-work/SKILL.md
- src/lingtai/tools/daemon/manual/SKILL.md
- src/lingtai/tools/daemon/shell_prompt_events.py
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# Shell Manual — Router

The `shell` tool schema covers one-off command execution. This manual routes to
operational depth that is too long for the schema: host scheduling, mailbox-drop
wakeups, async last-resort reminders, reminder files, debugging, and cleanup.

For ordinary short, deterministic one-off shell commands, use the tool schema
synchronously. For anything involving time, recurring work, external schedulers,
a silent scheduled job, or a **long-running agent/coding CLI** (see the resident
rule below), start here.

## Nested reference catalog

`shell-manual` owns these nested references. They are parent-owned drill-down
files, not standalone top-level skills.

```yaml
- name: bash-scheduled-work
  location: reference/scheduled-work/SKILL.md
  description: |
    Cron-driven scheduled work: when to use host schedulers, the LingTai
    wake-by-mailbox-drop contract, prompt boundaries, script hygiene, macOS
    launchd, Linux systemd timers, crontab fallback, and the launchd
    process-tree reaping gotcha.
- name: bash-notification-reminders
  location: reference/notification-reminders/SKILL.md
  description: |
    One-shot wakeup reminders via `.notification/cron.json`: payload shape,
    atomic writer, shell example, and the rest checklist for agents leaving work
    pending.
- name: bash-debugging-cleanup
  location: reference/debugging-cleanup/SKILL.md
  description: |
    Debugging and cleanup for scheduled jobs: scheduler fired, script ran, work
    landed, agent saw mail, worked launchd diagnosis, retiring cron jobs, and
    bash work footprint hygiene.
```

Coding-CLI reference pages have moved to `daemon-manual` (ownership change,
not a prohibition — `shell` remains a supported way to run them).

The nine coding CLIs with daemon backends (named in the frontmatter above) are
supported both as **daemon backends** (see `daemon-manual`) and via `shell`.
The per-backend operational details (flags, env, caveats) are owned by
`daemon-manual`'s corresponding submanuals — `reference/cli-backends/SKILL.md`
and its per-backend pages under `reference/cli-backends/reference/backends/`
— which **supersede the old bash reference guides**. This manual keeps only
the shell-side discipline that applies no matter which CLI you run: the async
+ poll supervision rules in `## Core rules to keep resident` and the
CLI-vs-daemon choice in `## Coding-CLI harness baseline` below.

## Router table

| Need / keywords | Read |
|---|---|
| Running a long-running agent/coding CLI as a sub-process (see frontmatter for the named CLIs); "run an agent in the background"; avoid blocking the turn | Supported via `shell` (run with `input.async=true` and poll — keep `## Core rules to keep resident` and `## Coding-CLI harness baseline` resident). The nine with daemon backends can also be dispatched as daemon backends; per-CLI operational detail (flags, env, caveats): `daemon-manual` → `reference/cli-backends/SKILL.md`. Gemini CLI, Aider, Goose, OpenHands, Crush are shell-only harnesses — no LingTai backend id |
| Human asks for time-driven recurring work: "every hour", "daily", "weekdays at 9", "write/check/send on a schedule"; choose cron vs event watcher; create launchd/systemd/crontab wiring; understand wake-by-mailbox-drop; write scheduler prompt/script hygiene | `reference/scheduled-work/SKILL.md` |
| Need a one-shot reminder or wakeup nudge while work is pending; `.notification/cron.json`; atomic reminder writer; rest checklist | `reference/notification-reminders/SKILL.md` |
| Scheduled job is silent, fires twice, exits immediately, gets killed by launchd, fails to deliver mail, or must be retired/cleaned up | `reference/debugging-cleanup/SKILL.md` |

## Quick decision tree

1. **Short deterministic host work** (finishes in seconds: `ls`, `git status`,
   `grep`, a quick build)? Use `shell` synchronously; this manual is not needed
   unless the command is risky, scheduled, or failing mysteriously.
2. **Long-running agent/coding CLI** (any named in the frontmatter, or any
   sub-agent that may think/run tools for minutes)? **Never run it
   synchronously.** Use `input.async=true` and poll — see the resident rule
   below. Running these CLIs via `shell` is supported; the nine with daemon
   backends can also be dispatched as daemon backends — Gemini CLI, Aider,
   Goose, OpenHands, and Crush have no LingTai backend id. Per-CLI detail
   (flags, env, ask/resume status): `daemon-manual` →
   `reference/cli-backends/SKILL.md`.
3. **Time itself is the trigger?** Read `reference/scheduled-work/SKILL.md`.
4. **You only need a single future nudge?** Read
   `reference/notification-reminders/SKILL.md`.
5. **A scheduled job already exists and is misbehaving?** Read
   `reference/debugging-cleanup/SKILL.md` before editing blindly.

## Settings inventory

`shell(action="settings", input={}, reasoning="inspect applied Shell settings")`
is read-only progressive disclosure. It returns only `key`, `current`,
`default`, `configurable`, and the exact section pointer in `comment`.
The input is strict empty: there is no set, reset, or other mutation form, and
Shell owns no settings file. Read the matching section below before changing a
value through its existing owner procedure, then call `settings` again to
inspect the newly applied truth. If any current value cannot be read, the whole
action returns `SETTINGS_UNAVAILABLE` without partial rows.

### Shell kind

`shell_kind` is the active command-language adapter: `posix`, `powershell`,
`cmd`, `gitbash`, or `wsl`. It is public and configurable by an authorized
Shell owner. Precedence is a valid capability `shell_kind` value, then a valid
case-insensitive `LINGTAI_SHELL`, then platform discovery; invalid values fall
through. Because platform discovery has no single universal fallback, the
projected `default` is `null`. Change the capability/launcher configuration or
call `setup(..., shell_kind="<kind>")`, then rebuild Shell (or relaunch the
owning Agent) and verify with another SHOW. Dialect selection never bypasses
command policy or the working-directory boundary.

### Sync timeout default

`sync_timeout_default_seconds` is the built-in `30`-second default used when a
synchronous `run` supplies `input.timeout=null` or omits it through the internal
compatibility path. It is public, built-in-only, has no config or environment
key, and is therefore not family-configurable. To vary one call, pass a finite
non-negative `input.timeout` no greater than the live ceiling; that per-run
value applies immediately but does not change this default, so a later SHOW
still reports `30`.

### Sync timeout ceiling

`sync_timeout_max_seconds` is the public hard ceiling for synchronous
`input.timeout`. Its canonical owner environment key is
`LINGTAI_TOOL_TIMEOUT_MAX_SECONDS`: a positive finite value wins over the
built-in `120`; missing, blank, non-numeric, non-positive, or non-finite values
use `120`, and values below `30` are floored to `30`. The value is read for
each settings SHOW and each sync run. Change it only through the authorized
process/launcher environment (relaunch if that environment is snapshotted at
process start); the next operation in a process that sees the new value applies
it. Work needing longer must use `input.async=true`.

### Result size limit

`result_max_chars` is the public per-stream character limit applied to captured
stdout and stderr. The built-in default is `50000`; an authorized embedding
owner may supply a positive integer through the existing
`ShellManager(..., max_output=N)` construction procedure, which applies when
that manager is built. The normal `setup` capability configuration exposes no
`max_output` key and there is no environment key. Rebuild the embedding's
manager/dispatcher and call SHOW again to verify the applied limit. This limit
changes result disclosure only; it grants no command or filesystem authority.

### Async default

`async_default` is the public built-in `false` used when `run` does not select
a mode. It has no config or environment key and is not family-configurable. Use
`input.async=true` or `false` to select the mode for one run; that choice applies
immediately and does not change the default reported by a second SHOW.

### Async reminder default

`async_reminder_default_seconds` is the public built-in `1800`-second
last-resort wake delay for an async run with no explicit reminder. It has no
config or environment key and is not family-configurable. For one async run,
pass a finite non-negative `input.reminder` no greater than the platform timer
bound; the value applies to that job only and does not change the default
reported by SHOW.

### Command policy

`command_policy` is the security-sensitive allowlist, denylist, or allow-all
policy bound to this Shell owner. Both `current` and `default` are always
`<redacted>`; policy paths and rules never enter the settings response.
Precedence is capability `yolo=true`, then `policy_file`, then the
platform-packaged policy. These are the canonical capability/setup keys; there
is no environment key. An authorized owner changes `yolo` or the reviewed
policy file through existing capability configuration or
`setup(..., policy_file=..., yolo=...)`, then rebuilds Shell. SHOW never grants
mutation authority and a second SHOW remains redacted by design.

## Reading command results — never trust top-level `status` alone

The top-level `status` of a `shell` result (`ok`/`done`) means only that the
**shell spawned** the command — **not** that the command succeeded. A failed
build, a missing file, a Python traceback, or a missing import all come back
under `status: "ok"`. Proceeding on that false success is the single most
common way agents corrupt their own downstream work.

- **Check `exit_code` / `ok` on every command whose success matters.** The
  result carries `ok` (bool) and `command_status` (`"success"`/`"failed"`)
  keyed off the exit code. `exit_code != 0` means the command failed even
  though `status` says `ok`.
- **Read the `warning` field when present.** On failure **or** a suspicious
  zero-exit (a traceback/missing-module signature in the output despite exit
  code 0) the result includes a one-line `warning` naming the nonzero exit, any
  detected Python traceback or missing module, and a stderr tail. The tail is
  run through the kernel's secret redactor, so secret-shaped lines are masked in
  `warning`; the raw `stderr` field is unchanged. If `warning` is present, stop
  and read it before acting on the output.
- **Use the venv interpreter for project code.** Bare `python3` lacks
  third-party packages and LingTai's own modules (`lingtai`, `lingtai.kernel`).
  A `No module named …` / `missing_module` warning usually means you ran the
  wrong interpreter — invoke the project's virtualenv `python`, not the system
  one.

## Avoid broad recursive scans

Unbounded recursive walks over large roots (`work/projects/.lingtai`) are the
top cause of `shell` timeouts. On a timeout the tool appends an `rg` recipe when
it detects this shape, but prefer it from the start:

- Replace `find <root> -name …`, `Path(...).rglob(...)`, `os.walk(...)`, and
  `glob('**/…')` over big trees with:
  `rg --files --hidden -g '!**/{.git,node_modules,daemons,.worktrees}/**' <root>`
  then filter the file list — `rg` honors `.gitignore` and skips the expensive
  directories by default. If `rg` is not installed, fall back to
  `find <root> -type f -not -path '*/.git/*' -not -path '*/node_modules/*' ...`
  (slower, but never silently fails).
- Narrow the root, add `-maxdepth`, or raise `timeout` only when the tree is
  genuinely large and you have a reason to walk all of it.

## Parse JSONL line-by-line

Event/log files (`events.jsonl`, daemon logs) are **JSON Lines** — one JSON
object per line, **not** a single JSON document. `json.loads(whole_file)` fails
on them. Iterate lines and `json.loads` each non-empty line, or pipe through
`jq -c .` / `rg` to filter before parsing. Tail with `tail -n` instead of
reading the whole file when you only need recent events.

## Core rules to keep resident

- **Synchronous `shell` is only for short, deterministic commands.** A long-running
  agent/coding CLI session — `claude -p`, `codex exec`, `opencode run`, the Cursor
  agent CLI, or any sub-agent that may think and run tools for minutes — must
  **never** be a synchronous `shell` call. Run it with `input.async=true` and poll
  the returned `job_id`. A synchronous call blocks the whole turn until the child
  exits: you stay `ACTIVE` and stop seeing channel notifications (mail, refresh,
  interrupts) for the entire duration. Async + poll keeps you responsive and
  prevents ACTIVE blockage while the child CLI works.

  ```text
  # Start the child agent in the background — returns immediately with a job_id.
  # Run-only fields (command, async, reminder, timeout, working_dir) live in input:
  shell(action="run",
        input={"command": "claude -p 'refactor the auth module' --output-format json",
               "async": true, "reminder": 1800},
        reasoning="start the refactor sub-agent in the background")
  # → {"status": "ok", "job_id": "job-a1b2c3d4e5f678901234567890abcdef", "pid": 4321}

  # Later turns: poll until done (handle mail/other work between polls).
  # poll takes job_id and nothing else — no reminder, no command:
  shell(action="poll",
        input={"job_id": "job-a1b2c3d4e5f678901234567890abcdef"},
        reasoning="check whether the refactor sub-agent finished")
  # → {"status": "running", …}   then eventually
  # → {"status": "done", "exit_code": 0, "ok": true, "command_status": "success", "stdout": "…", "stderr": "…"}
  #   On failure: {"status": "done", "exit_code": 1, "ok": false,
  #                "command_status": "failed", "warning": "command exited with code 1; …"}

  # Abandon it if needed — cancel also takes job_id and nothing else:
  shell(action="cancel",
        input={"job_id": "job-a1b2c3d4e5f678901234567890abcdef"},
        reasoning="the refactor is no longer needed")
  ```

- **Use a Task Card for progress when one is available for this turn.**
  The async success `handoff` is conditional: if Telegram is connected and a
  Task Card is available for the current turn, use it to report progress; call
  `telegram(action='manual')` and follow its `Programmable Task Card` section
  for details. The shell tool does not create a Task Card automatically or
  require a watcher; the calling agent follows the Task Card manual. This keeps
  background command lifecycle and notification behavior unchanged while giving
  Telegram-originated turns a better progress surface.

- **If repeated-call `_advisory` appears on `shell(action="poll")`, stop
  tight polling.** The poll already executed; the advisory is not a block. If
  the job is still running and nothing meaningful changed, handle any human
  messages, do other work, or set one future reminder (`bash` notification
  reminder or internal delayed self-email) and yield/idle. Poll again only when
  a completion notification arrives, the reminder fires, or you have a concrete
  reason to expect new state.

### Detached daemon Shell is deliberately different

When a **selected detached LingTai daemon** uses `shell`, it has no live Agent
notification store, mailbox, heartbeat, or `.notification/system.json` /
`.notification/bash.json` route. Its async job state is in that run's private
`<run>/shell-jobs` namespace, never the task workdir's shared `system/jobs`, even
though command cwd remains the granted task workdir. Its async
reminder/completion becomes a bounded same-run prompt event only while that
daemon is still live. A temporarily full event queue is retried by the live
selected manager with capped backoff after capacity can drain. The event contains
a job id but **not** command stdout/stderr; at its next safe model-send boundary
the daemon is told to call `shell.poll` for exact output. It is not a parent wake
or `daemon_common` checkpoint, does not auto-poll or consume the result, and
does not keep/restart a terminal daemon. Read `daemon-manual` for the daemon
lifecycle rule. Ordinary Agent Shell keeps the `.notification` behavior described
below.

- **Idle care: set an async `input.reminder` that matches the expected duration.**
  Every async run has a last-resort `reminder` delay. It lives only in `run`'s
  `input`, alongside the other run-only fields; `poll` and `cancel` take
  `job_id` and nothing else, so they never carry it. Pass `null` (or omit it) to
  get the runtime default of 1800 seconds.
  The initial durable deadline is a crash fallback while the supervisor starts.
  A bounded durable return-handoff guard prevents a relaunched/second manager from
  publishing that fallback while the first manager is still before supervisor
  `Popen`, or after the command is durably `running` but before async `run` has
  completed its return transition. Successful `run` atomically resets the
  deadline to `returned_at + reminder` and arms the guard, so startup latency does
  not consume the interval you requested. Shell reports `status: ok` only when this
  still-valid transition wins (or an exact completed/failed result already won
  under the valid guard). If the owner resumes after expiry, it returns
  `status: error` with the durable `job_id`/`pid` and an explicit "remains
  pollable" recovery message rather than claiming false success. A live job keeps
  the expired fallback; an expired launch or definitively dead supervisor becomes
  explicit unrecoverable state instead.
  If the job is still non-terminal when its final deadline expires, Shell publishes
  a `bash.reminder` event into `.notification/system.json`.
  The deadline and stable `bash.reminder:<job_id>` claim survive agent
  stop/relaunch: Shell re-arms a future deadline or retries an overdue/stale claim.
  Cancellation temporarily uses a bounded durable `suppressing` state; if the
  manager crashes or the supervisor does not commit before it expires, reminder
  publication becomes recoverable again. Exact supervisor terminal commit
  suppresses a pending/publishing/suppressing `may still be running` watchdog and
  publishes the authoritative completion wake through `.notification/bash.json`;
  terminal poll and confirmed cancellation also suppress future reminder retries.
  Final reminder publication and suppression are serialized, so a stale claim
  that loses the job-state lock cannot publish. If the reminder sink write won
  before terminal commit, that already-published event may remain as historical
  evidence.
  Do not treat either stable ref as a global exactly-once guarantee: a crash after
  sink write before durable acknowledgement can retry after the bounded system
  list evicts the reminder ref, while the latest-only Bash slot can be overwritten
  by another completion. Pick the delay from the task's *expected* duration — not
  a fixed number; a 30 s scan and a 40 min build warrant different windows. When
  a reminder fires, health-check rather than assume progress: poll the job,
  confirm the log is **growing**, the PID/child is **alive** if still running,
  the output file/worktree shows **progress**, and the job is not stuck on an
  interactive prompt or a provider/model error. If there is no progress, do not
  keep waiting — cancel, downgrade, or switch path, and report to the human.
  Use a separate `.notification/cron.json` reminder or delayed self-email only
  for a broader workflow wake that is not tied to one Shell async job. Do not
  conflate them: a Bash reminder belongs to one persisted `job_id`, while
  `.notification/cron.json` is a separately scheduled workflow wake.

- **Relaunch-safe status is still evidence, not PID guessing.** The launching
  manager records the observed supervisor identity immediately; Bash's detached
  supervisor confirms its incarnation and persists exact wait truth. A missing command PID
  is not a terminal result while that supervisor may still commit: poll retries
  briefly, then remains recoverably `running` unless the recorded supervisor is
  definitively gone. A retained legacy job with a still-live recorded PID remains
  conservatively `running` and uncancellable because Bash cannot prove that PID's
  incarnation; after the PID dies, one poll may return `exit_status_known: false`
  and `exit_code: null`. Unrecoverable durable state uses the same explicit unknown
  shape. Bash never invents `-1` or calls that a command failure. Cancellation is
  a durable request to the
  supervisor that holds the unreaped child: it sends TERM, bounded KILL escalation
  if needed, and the manager reports `cancelled` only after exact terminal commit.
  A timeout/error leaves poll and the reminder available for recovery. Terminal
  poll and successful cancel are atomic one-shot consumer actions; the durable
  record remains for evidence, but any later poll/cancel returns `Job already
  finished` instead of exposing a second result. New job IDs carry full UUID4 hex;
  legacy eight-hex IDs are accepted only to read retained old records.

## Coding-CLI harness baseline

This section keeps the two rules every coding-CLI run shares — whether via
`shell` (async + poll) or as a daemon backend: run `--help` before relying on
any flag, and pick CLI-vs-daemon by the shape of the work. Per-CLI specifics
(flags, env, caveats) are owned by `daemon-manual` →
`reference/cli-backends/SKILL.md`, which supersedes the old bash reference
guides.

**Before relying on any coding CLI in automation:** run the installed CLI's own
`--help`. Flag surfaces rev between releases, so a documented flag is
illustrative, never authoritative.

**CLI vs daemon — pick by the shape of the work.** A CLI subprocess is one
synchronous run whose transcript you read yourself; a LingTai daemon is a
dispatched worker with its own worktree, branch, and context window.

| Signal | Pick |
|---|---|
| "I want the answer in this conversation, now" | **CLI** |
| "The output is a small string/snippet I'll paste somewhere" | **CLI** |
| "I need this exact CLI model/agent/flag, or a warm attached server" | **CLI** |
| "I want to do three of these at once" | **Daemon** (one per task) |
| "I'll review a diff afterward, not the transcript" | **Daemon** |
| "This will take 15+ minutes and produce a branch" | **Daemon** |
| "This might block my main turn while a human waits" | **Daemon** or supervised background wrapper |
| "I'm the orchestrator; the daemon is the worker" | **Daemon** |

When in doubt for non-trivial work: daemon. A CLI has **no LingTai job protocol
of its own** — "async" always means a LingTai or OS wrapper around it
(`shell` with `input.async=true`, a supervised background job, or a daemon
backend), and
that wrapper owns logs, timeout, cancellation, and recovery notes. Keep
synchronous inline calls short and explicitly timed (for example a 300 s bash
timeout); do not solve a long task by raising the synchronous timeout to 15+
minutes while the main agent waits. Checkpoint the worktree, branch, goal, and
recovery instructions to pad or a journal before dispatching anything that might
outlive the turn, and prefer several smaller bounded calls over one monolithic
prompt. Backend names, `backend_options`, `ask`/resume, and per-backend parser
caveats belong to `daemon-manual` → `reference/cli-backends/SKILL.md`, not here.

Backend-promotion criteria for a CLI that is not yet a daemon backend live in
`daemon-manual` → `reference/cli-backends/SKILL.md` (`## Backend promotion gate`).

## Scheduling rules to keep resident

- LingTai has no built-in recurring scheduler. Host schedulers wake agents by
  producing channel input, usually a mailbox-drop or notification file.
- Prefer event watchers/webhooks when an external event is the real trigger;
  prefer cron/launchd/systemd only when time is the trigger or polling is truly
  the right tradeoff.
- Scheduler scripts must be idempotent, audited, logged, absolute-path based,
  and explicit about how they wake the agent.
- On macOS, remember launchd process-tree reaping; use the documented
  double-fork pattern when a child process must outlive the launchd job.
- Do not leave silent janitors or hidden recurring jobs behind. Document and
  clean them up when the human no longer needs them.

## Maintenance

Keep this top-level router short. Add detailed examples, platform recipes, and
troubleshooting trees to nested references so agents can load only the section
needed for the current task.
