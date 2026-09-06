---
name: task_card-manual-notifications
description: >
  Focused Task Card reference for typed producer notifications, reminders,
  change-gated resident projection, and limits on consumer guarantees.
version: 0.1.0
last_changed_at: "2026-09-06T00:00:00Z"
tags: [lingtai, task-card, notifications, projection, reminders]
related_files:
- src/lingtai/tools/task_card/manual/SKILL.md
- src/lingtai/tools/task_card/__init__.py
- src/lingtai/tools/task_card/CONTRACT.md
- src/lingtai/adapters/tool_plugin_host.py
maintenance: |
  Tracks Task Card's typed notification and consumer-projection boundary;
  update with the owner manual and contract when event forms, reminder seams,
  or projection guarantees change.
---

# Task Card notifications and projection

## Typed producer boundary

The producer emits only `TaskCardErrorNotification`,
`TaskCardRecoveredNotification`, and `TaskCardLimitNotification` through its
family adapter. The granted native port exposes only
`publish_error`, `publish_recovered`, `publish_limit`, `submit_reminder(turns)`,
and `clear_reminder()`. A generic publisher, arbitrary keyword fields, foreign
source/channel, or caller-supplied priority/extra metadata is refused.

The host adapter pins the established wire policy: error and recovered states
use `task_card.error`, refresh exhaustion uses `task_card.limit`, and events go
to the system channel with bounded extras, idempotency, and priority. The
producer owns event content and deduplication identity; it does not select a
transport or another channel.

Renderer failures after a valid watch preserve the last body and emit a deduped
error state; a later successful publication emits recovery. Refresh exhaustion
emits one limit event whose guidance says to start a new watch if work remains.
Notification failure must not turn a truthful producer state into a false one.

## Absent/stale reminders

After the configured `reminder_turns` completed text turns (default `10`), the
producer asks the agent to check whether the card is absent or stale and update
or retire it only if useful. The counter resets after a successful publication
(`start`, refresh, or resume), so a watch that keeps publishing does not reach
the absent/stale reminder threshold. The reminder resurfaces the decision; it
does not re-inject an unchanged body.

## Resident projection

The card is resident in `_meta.agent_meta.taskcard` for the agent's own view and
may be read by human-facing consumers. Projection is change-gated: unchanged
body/status bytes are not repeatedly re-injected, while a first appearance or
material change can attach a fresh payload. If no card is present, the resident
hint is generic and routes the agent to the `task_card` manual.

The body cap is a producer guard: oversized renderer output is refused rather
than truncated. Keep the card focused on the current goal, status, and next
step; put complex progress in reports, logs, or checklists referenced by the
card. A consumer may compare bytes and apply its own edit/send rate limits, but
those are not promises made by this capability. A real changed body remains a
real producer update even when a consumer chooses when or how to display it.

## No hidden external projection promise

Task Card owns the producer artifact and the typed events, not Telegram, Feishu,
portal, chat IDs, message edits, retries, or delivery guarantees. Consumers
independently read `taskcard/status` and `taskcard/taskcard.md` and interpret
missing, invalid, or inactive state. Do not report a consumer-side projection
as producer progress; the renderer must report only evidence from the underlying
work.
