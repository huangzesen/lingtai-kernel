---
name: task_card-manual-lifecycle
description: >
  Focused Task Card lifecycle reference for renderer gates, truthful progress,
  artifact ordering, restart resume, and stop/remove distinction.
version: 0.1.0
last_changed_at: "2026-09-06T00:00:00Z"
tags: [lingtai, task-card, renderer, lifecycle, progress, restart]
related_files:
- src/lingtai/tools/task_card/manual/SKILL.md
- src/lingtai/tools/task_card/__init__.py
- src/lingtai/tools/task_card/ANATOMY.md
- src/lingtai/tools/task_card/CONTRACT.md
maintenance: |
  Tracks the Task Card renderer and lifecycle procedure; update with the owner
  manual and contract when artifact ordering, path gates, or watch recovery changes.
---

# Task Card lifecycle and truthful producer use

## When to use it

Start a watch proactively when a human is following meaningful long-running,
multi-step, or parallel work and a durable progress view materially helps:
multi-daemon fleets, multi-PR batches, and long review→merge flows are typical
cases, especially work running past roughly ten minutes. Skip quick single-step
work or ritual updates. Keep a watch only while its renderer output is truthful
and current; a stale card misleads more than no card. When `max_refreshes`
expires a watch, restart a new watch when work continues mid-task rather
than letting the card go dark.

## Producer-owned artifacts

The producer writes `taskcard/taskcard.md` as the full rendered body and
`taskcard/status` as exact `active` or `inactive`. An active watch also keeps
`taskcard/watch.json` with its renderer, cadence, watch ID, and carried refresh
budget so refresh/molt/agent-stop can resume it. `taskcard/taskcard.json` is the
agent-wide policy document; normal reads are read-only and its one-way legacy
migration is documented in [settings](settings.md). The producer does not own
Telegram/Feishu chat IDs, portal layout, transport retries, or consumer state.

## Renderer gate and publication order

`renderer_path` must name an existing regular Python file that resolves inside
the agent working directory after symlink resolution. It runs as
`sys.executable <renderer>` with that directory as cwd. It must exit `0` and
print a non-empty complete body to stdout; stderr is not the card body. The
configured body limit is a refusal, never truncation; see [settings](settings.md).

`start` runs the renderer first, atomically writes the complete body, then
atomically writes exact `active`, and only then starts the updater and persists
its descriptor. `retry` reruns the same renderer and atomically replaces only
the body; status remains `active`. A renderer or publication failure preserves
the last valid body and records the producer error rather than inventing
progress. A second `start` fails closed because one card/watch is allowed per
agent.

## Pause, terminal cleanup, and restart

- `inspect` reports the one current watch, exact artifact paths, status, and the
  last valid body; it does not create another watch.
- `stop` writes `inactive` before asking the updater to stop, joins it, clears
  the descriptor, and preserves the last body for later `retry` or inspection.
  It is a pause, not cleanup. If the updater does not quiesce, report the
  retryable failure and leave the watch/body retryable.
- `remove` takes no `watch_id`: it retires the one watch exactly as `stop` does,
  waits for quiescence, then deletes `taskcard/taskcard.md`. It leaves status
  exactly `inactive`, clears restart state, blocks rather than deleting while a
  watch may still run, and is idempotent when no body exists. Agents must not
  reach around it with Shell or File deletion.
- Agent shutdown writes `inactive`, stops the thread, and persists the carried
  descriptor unless the watch was deliberately stopped, removed, or exhausted.
  On the next setup the same watch ID, renderer, cadence, ceiling, and remaining
  refresh budget are rehydrated. Corrupt, missing, escaped, gone, or exhausted
  descriptors are cleared and left `inactive`; a transient resume failure keeps
  the last body and lets the live watch retry.

A refresh-limit exhaustion retires the watch, writes `inactive`, clears the
descriptor, and emits one typed limit event. If the underlying work continues,
start a new watch rather than letting the card go dark. Use `stop` while work is
paused and `remove` once it is completed, cancelled, or abandoned.

## Truthful progress

The renderer is the real producer of progress. Read the underlying work state,
include only evidence available to the producer, and update the body when that
state materially changes. Do not promise that a consumer transport edited,
sent, retried, or delivered a card; those consumers only read the producer's
artifact and apply their own rules.
