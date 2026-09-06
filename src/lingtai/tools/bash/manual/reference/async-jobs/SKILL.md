---
name: bash-async-jobs
description: >
  Nested shell-manual reference for durable asynchronous shell jobs: detached
  daemon routing, reminder/completion wakeups, relaunch-safe polling,
  cancellation, and the shell-side coding-CLI harness rules.
version: 1.0.0
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/bash/manual/SKILL.md
- src/lingtai/tools/bash/_async_supervisor.py
- src/lingtai/tools/bash/_async_process.py
- src/lingtai/tools/bash/_state_lock.py
maintenance: |
  Tracks the durable async shell-job and shell-side coding-CLI topic it
  documents; update this reference and its parent router when that lifecycle or
  routing changes.
---

# Async Shell Jobs Reference

Nested shell-manual reference. Open this after the top-level manual's first-call
rules when a background command needs durable polling, reminder/completion
wakeup, cancellation, relaunch recovery, or coding-CLI supervision. The shell
engine and its process adapters own execution; this page only discloses the
operational discipline and recovery details.

## Core async rules

A long-running agent/coding CLI session — `claude -p`, `codex exec`, `opencode
run`, the Cursor agent CLI, or any sub-agent that may think and run tools for
minutes — must **never** be a synchronous `shell` call. Run it with
`input.async=true` and poll the returned `job_id`. A synchronous call blocks the
whole turn until the child exits: you stay `ACTIVE` and stop seeing channel
notifications (mail, refresh, interrupts). Async + poll keeps the turn
responsive and prevents ACTIVE blockage while the child CLI works.

The public envelope is used for every follow-up. Run-only fields
(`command`, `async`, `reminder`, and `timeout`) stay in `run`'s `input`; `poll`
and `cancel` take only `job_id`:

```text
shell(action="run",
      input={"command": "<long command>", "async": true, "reminder": 1800},
      reasoning="start the background command")
shell(action="poll",
      input={"job_id": "job-<id>"},
      reasoning="check the background command")
shell(action="cancel",
      input={"job_id": "job-<id>"},
      reasoning="stop the background command")
```

Use a Task Card for progress only when Telegram is connected and a Task Card is
available for the current turn. In that conditional case, read
`telegram(action='manual')` and follow its `Programmable Task Card` section.
Shell does not create or require a watcher; this guidance does not change the
job lifecycle.

If repeated-call `_advisory` appears on `shell(action="poll")`, the poll
already executed. Stop tight polling: handle messages, do other work, or set a
future reminder, then poll again only when a completion notification, reminder,
or concrete state change warrants it.

## Detached daemon Shell

When a **selected detached LingTai daemon** uses `shell`, it has no live Agent
notification store, mailbox, heartbeat, or `.notification/system.json` /
`.notification/bash.json` route. Its async job state is in that run's private
`<run>/shell-jobs` namespace, never the task workdir's shared `system/jobs`,
even though command cwd remains the granted parent task workdir. Its async
reminder/completion becomes a bounded same-run prompt event only while that
daemon is still live. A temporarily full event queue is retried by the live
selected manager with capped backoff after capacity can drain. The event
contains a job id but **not** command stdout/stderr; at its next safe model-send
boundary the daemon is told to call `shell.poll` for exact output. It is not a
parent wake or `daemon_common` checkpoint, does not auto-poll or consume the
result, and does not keep/restart a terminal daemon. Read `daemon-manual` for
the daemon lifecycle rule. Ordinary Agent Shell keeps the `.notification`
behavior described below.

## Reminders, completion wakes, and idle care

Set an async `input.reminder` that matches the expected duration. Every async
run has a last-resort reminder; `null` (or omission through the compatibility
path) uses 1800 seconds. The value is run-only: `poll` and `cancel` never carry
it. Pick the delay from the task's *expected* duration, not a fixed number — a
30-second scan and a 40-minute build warrant different windows.

The initial durable deadline is a crash fallback while the supervisor starts. A
bounded durable `return_handoff` guard prevents a relaunched/second manager from
publishing that fallback while the first manager is before supervisor `Popen`,
or after the command is durably `running` but before async `run` has completed
its return transition. Successful `run` atomically resets the deadline to
`returned_at + reminder` and arms the guard, so startup latency does not consume
the requested interval. Shell reports `status: ok` only when this still-valid
transition wins (or exact completed/failed truth already won under the valid
guard). If the owner resumes after expiry, it returns `status: error` with the
durable `job_id`/`pid` and an explicit pollable-recovery message rather than
claiming false success.

If the job remains non-terminal when its final deadline expires, Shell publishes
a `bash.reminder` event into `.notification/system.json`. The deadline and
stable `bash.reminder:<job_id>` claim survive agent stop/relaunch: Shell re-arms
a future deadline or retries an overdue/stale claim. Cancellation temporarily
uses a bounded durable `suppressing` state; if the manager crashes or the
supervisor does not commit before it expires, reminder publication becomes
recoverable again. Exact supervisor terminal commit suppresses a
pending/publishing/suppressing `may still be running` watchdog and publishes
the authoritative completion wake through `.notification/bash.json`; terminal
poll and confirmed cancellation also suppress future reminder retries.

Final reminder publication and suppression are serialized, so a stale claim
that loses the job-state lock cannot publish. If the reminder sink write won
before terminal commit, that already-published event may remain as historical
evidence. Stable refs are bounded/current-sink deduplication aids, not a global
exactly-once guarantee: a crash after sink write before durable acknowledgement
can retry after the bounded system list evicts the reminder ref, while the
latest-only Bash slot can be overwritten by another completion.

When a reminder fires, health-check rather than assume progress: poll the job,
confirm the log is **growing**, the PID/child is **alive** if still running, the
output file/worktree shows **progress**, and the job is not stuck on an
interactive prompt or provider/model error. If there is no progress, do not
keep waiting — cancel, downgrade, or switch path, and report to the human. Use
a separate `.notification/cron.json` reminder or delayed self-email only for a
broader workflow wake that is not tied to one Shell async job. A Bash reminder
belongs to one persisted `job_id`; `.notification/cron.json` is a separate
scheduled workflow wake.

## Relaunch-safe status and cancellation

The launching manager records the observed supervisor identity immediately;
the detached supervisor confirms its incarnation and persists exact wait truth.
A missing command PID is not a terminal result while that supervisor may still
commit: poll retries briefly, then remains recoverably `running` unless the
supervisor is definitively gone. A retained legacy job with a still-live
recorded PID remains conservatively `running` and uncancellable because Bash
cannot prove that PID's incarnation; after the PID dies, one poll may return
`exit_status_known: false` and `exit_code: null`. Unrecoverable durable state
uses the same explicit unknown shape. Bash never invents `-1` or calls that a
command failure.

Cancellation is a durable request to the supervisor that holds the unreaped
child: it sends TERM, bounded KILL escalation if needed, and reports
`cancelled` only after exact terminal commit. A timeout/error leaves poll and
the reminder available for recovery. Terminal poll and successful cancel are
atomic one-shot consumer actions; the durable record remains for evidence, but
any later poll/cancel returns `Job already finished` instead of exposing a
second result. New job IDs carry full UUID4 hex; legacy eight-hex IDs are
accepted only to read retained old records.

## Coding-CLI harness baseline

These two rules apply to every coding-CLI run, whether via `shell` (async +
poll) or as a daemon backend: run `--help` before relying on any flag, and pick
CLI-vs-daemon by the shape of the work. Per-CLI specifics (flags, env, caveats)
are owned by `daemon-manual` → `reference/cli-backends/SKILL.md`, which
supersedes the old bash reference guides.

**Before relying on any coding CLI in automation:** run the installed CLI's own
`--help`. Flag surfaces change between releases, so a documented flag is
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
(`shell` with `input.async=true`, a supervised background wrapper, or a daemon
backend), and that wrapper owns logs, timeout, cancellation, and recovery notes.
Keep synchronous inline calls short and explicitly timed; do not solve a long
task by raising the synchronous timeout to 15+ minutes while the main agent
waits. Checkpoint the worktree, branch, goal, and recovery instructions to pad
or a journal before dispatching anything that might outlive the turn, and prefer
several smaller bounded calls over one monolithic prompt. Backend names,
`backend_options`, `ask`/resume, and per-backend parser caveats belong to
daemon-manual → `reference/cli-backends/SKILL.md`, not here.

Backend-promotion criteria for a CLI that is not yet a daemon backend live in
daemon-manual → `reference/cli-backends/SKILL.md` (`## Backend promotion gate`).
