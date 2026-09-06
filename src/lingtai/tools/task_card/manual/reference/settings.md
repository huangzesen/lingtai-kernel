---
name: task_card-manual-settings
description: >
  Focused Task Card settings reference for the read-only SHOW inventory,
  owner-document validation, cadence, safety ceilings, migration, and body cap.
version: 0.1.0
last_changed_at: "2026-09-06T00:00:00Z"
tags: [lingtai, task-card, settings, cadence, ceilings, migration]
related_files:
- src/lingtai/tools/task_card/manual/SKILL.md
- src/lingtai/tools/task_card/__init__.py
- src/lingtai/tools/task_card/CONTRACT.md
maintenance: |
  Tracks the five Task Card owner policies and their SHOW/change procedure;
  update with the owner manual and contract when defaults, validation, or seams change.
---

# Task Card settings and cadence

## SHOW and owner document

Call `task_card(action="settings", input={})` with exact empty input. The
provider returns exactly five rows, in owner order: `interval_s`, `timeout_s`,
`max_refreshes`, `reminder_turns`, and `max_body_chars`. Each row has only
`key`, `current`, `default`, `configurable`, and `comment`; comments point to the
stable anchors in the parent `task_card-manual`.

SHOW reads fresh effective values without creating or changing
`taskcard/taskcard.json` and without running its one-time legacy migration. A
`configurable: true` row means an authorized owner may create or edit that JSON
object outside SHOW, preserving sibling fields; it does not make SHOW a writer.
After an authorized edit, call SHOW again. Invalid or missing fields fall back
independently to their own built-in defaults. If effective truth is unavailable,
the whole bounded inventory fails with no partial rows or raw exception detail.
Renderer paths, body/status/watch contents, notification state, and unknown
owner fields are never projected.

## interval-s

`interval_s` is the polling cadence in seconds for a newly started or resumed
watch. The valid owner value is finite and at least `1`; otherwise the default
is `5`. An explicit `start.interval_s` also obeys only that one-second floor and
has no ceiling: a slower cadence is honored. The value is read at each `start`
or persisted-watch resume; a running watch keeps its captured cadence.

## timeout-s

`timeout_s` is the ceiling, in seconds, for one renderer execution, not the
watch's lifetime. The valid owner value is finite and at least `0.1`; otherwise
the default is `10`. An omitted `start.timeout_s` uses the configured ceiling;
an explicit value may lower it but is silently capped at that ceiling if larger.
A running watch keeps its captured ceiling.

## max-refreshes

`max_refreshes` is the refresh ceiling for a newly started or resumed watch. A
valid owner value is a positive integer; otherwise the default is `2000`. An
explicit `start.max_refreshes` may lower but never exceed the configured ceiling.
Before `taskcard/taskcard.json` exists, SHOW may preview a genuinely customized
positive legacy Telegram ceiling; the legacy untouched default `1000` is
ignored. Preview does not write the new document.

## reminder-turns

`reminder_turns` is the number of completed text turns between absent-or-stale
Task Card reminders. A valid owner value is a positive integer; otherwise the
default is `10`. It is read on each completed text turn, so changing the owner
document applies at that seam rather than changing an already-running watch's
captured cadence.

## max-body-chars

`max_body_chars` is the maximum rendered body accepted by the producer. A valid
owner value is an integer at least `100`; otherwise the default is `2000`. It is
read on each publication. An oversized body is refused, never truncated, and
the last valid body remains visible.

## Resolution, floors, and the one-way migration

The five fields resolve independently from `taskcard/taskcard.json`; malformed
siblings do not discard valid values. `start` resolves the first three fresh;
`reminder_turns` and `max_body_chars` are read at their application seams. The
first resolution made when the intrinsic JSON document does not exist may read
`telegram/taskcard.json` only to carry forward a valid customized positive
`max_refreshes` different from that retired design's untouched `1000` default.
It then writes the intrinsic document whether the result was migrated or built
in, and later changes to the legacy file are never consulted. If the intrinsic
document already exists but is malformed, it remains the sole owner and fields
fall back to built-ins.

Thus the safety contract is monotonic: `interval_s` cannot go below `1`,
`timeout_s` cannot go below `0.1`, refresh/reminder counts stay positive, and
explicit timeout/refresh requests cannot exceed their configured ceilings.
