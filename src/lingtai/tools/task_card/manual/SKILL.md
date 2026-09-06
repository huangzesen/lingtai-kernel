---
name: task_card-manual
description: >
  Read before managing the declarative Task Card / progress-watch artifact;
  routes renderer lifecycle, settings, and projection detail while preserving
  the one-card-per-agent contract and stop/remove distinction.
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/task_card/__init__.py
- src/lingtai/tools/task_card/ANATOMY.md
- src/lingtai/tools/task_card/CONTRACT.md
- src/lingtai/tools/task_card/manual/reference/lifecycle.md
- src/lingtai/tools/task_card/manual/reference/settings.md
- src/lingtai/tools/task_card/manual/reference/notifications.md
- src/lingtai/kernel/tool_plugin/CONTRACT.md
maintenance: |
  Keep this router aligned with the intrinsic task_card action/settings surface,
  exact taskcard/status and taskcard/taskcard.md paths, one-card lifecycle, and
  the focused references it routes to. Update it with the paired Anatomy/Contract
  whenever those contracts or the renderer boundary change.
---

# task_card manual

Use `task_card` to maintain one agent-local, channel-neutral Task Card artifact.
The capability is producer-first: it runs your renderer and writes the body and
status; Telegram, Feishu, and other consumers decide how to read or project that
producer state.

## First call

`task_card` is a strict LTP-v2 family. Pass `action`, that action's strict
`input` object, and root `reasoning`; optional `summarize` is root-only. The
public actions are `start`, `inspect`, `retry`, `stop`, `remove`, `settings`, and
`manual`; `manual` takes `{}` and is directly callable.

For `start`, provide an existing Python `renderer_path` inside the working
directory. The renderer must exit successfully and print a non-empty full body to
stdout. The producer writes that body atomically to `taskcard/taskcard.md`, then
writes exact `active` to `taskcard/status`. It permits one active watch per
agent. Keep its output truthful and current: start a watch for meaningful
long-running, multi-step, or parallel work, not ritual noise.

Read [lifecycle and truthful producer use](reference/lifecycle.md) for renderer
path gates, action ordering, restart resume, and the complete stop/remove
lifecycle. Read [settings and cadence](reference/settings.md) for owner policy,
ceilings, and body limits. Read [notifications and projection](reference/notifications.md)
for typed producer events, reminders, and consumer boundaries.

## When to use it

Start proactively when a durable progress view materially helps a human follow
ongoing work. Skip quick single-step work or any watch whose renderer cannot
remain truthful. If a watch expires while work continues, restart a new watch;
use `stop` to pause while preserving the last body, and `remove` after work is
completed, cancelled, or abandoned. Never delete the body with Shell or File.

## Settings

Call `settings` with exact `input={}` to SHOW the five numeric policies owned by
`taskcard/taskcard.json`. SHOW is read-only: it never writes or migrates that
file, and it has no set/reset form. Every row contains only `key`, `current`,
`default`, `configurable`, and `comment`; after an authorized owner edit, call
SHOW again. Effective truth is whole-action or failure, never partial.

The stable row comments retain these anchors; their detail is in the focused
settings reference:

### interval-s
See [interval-s](reference/settings.md#interval-s).

### timeout-s
See [timeout-s](reference/settings.md#timeout-s).

### max-refreshes
See [max-refreshes](reference/settings.md#max-refreshes).

### reminder-turns
See [reminder-turns](reference/settings.md#reminder-turns).

### max-body-chars
See [max-body-chars](reference/settings.md#max-body-chars).

## Boundary reminder

The producer owns its artifact and typed Task Card notifications only. It does
not own transport-specific IDs, retries, layout, or a promise that an external
channel will display every state. Consumers read `taskcard/status` and
`taskcard/taskcard.md` independently. Keep the card a concise progressive-
disclosure summary and put complex evidence in referenced files; see the
[projection reference](reference/notifications.md).
