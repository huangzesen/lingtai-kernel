---
name: molt-manual
description: >-
  Detailed Context molt procedure: durable-store tending, session-journal
  validation, keep-set semantics, consequential handoffs, pressure recovery,
  and post-wipe continuation.
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/context/manual/SKILL.md
- src/lingtai/tools/context/manual/assets/molt-template.md
- src/lingtai/tools/context/manual/assets/session-journal-entry-template.md
- src/lingtai/tools/context/_molt.py
- src/lingtai/tools/context/_session_journal.py
- src/lingtai/tools/context/_snapshots.py
- src/lingtai/tools/system/summarize.py
- src/lingtai/agent.py
- src/lingtai/tools/context/manual/reference/summarize-manual/SKILL.md
maintenance: |
  Owns the detailed Context molt and recovery procedure routed by the canonical
  context-manual. Keep safety gates and field semantics aligned with the Context
  implementation and its focused tests; keep the top manual a short router.
---

# Context Molt Manual

Load this reference for a deliberate `context(action="molt", ...)`, especially
when a handoff is consequential. The top-level `context-manual` keeps only the
first-call guard and routes here; this page owns the full recipe. Molt is not
routine housekeeping: do it when context pressure (at least 85%), an explicit
human request, or conversation confusion makes a fresh briefing worth its cost.

## 1. Tend durable stores before molt

The four durable stores are the real persistence; the summary is only the
briefing layered over them. Tend them before every agent-initiated molt:

- **LingTai/character** — carry forward the complete identity in
  `system/lingtai.md` with `file.write` (full rewrite) or `file.edit` (exact
  replacement). Read `psyche(action="lingtai", input={}, reasoning="...")` for
  identity-mode and self-evolution guidance.
- **Pad** — maintain `system/pad.md` with `file.write`/`file.edit`, and update
  `system/pad_append.json` the same way for the pinned-reference list. Read
  `psyche(action="pad", input={}, reasoning="...")` for Pad ownership and
  archiving practice.
- **Knowledge** — write durable long-term context to
  `knowledge/<name>/KNOWLEDGE.md` with `file.write`/`file.edit`.
- **Skills** — write reusable procedures to
  `.library/custom/<name>/SKILL.md` with `name`, `description`, and `version`
  frontmatter, then rebuild when the catalog must refresh. Share source or
  artifacts so a peer installs the skill in its own custom library.

For `lingtai` and `knowledge`, hold ordinary updates until the end of the task
and commit them in one pass; checkpoint early only when a long task could lose
work in a crash. Update Pad whenever its index meaningfully changes. Generic
mutation belongs to `file`; there is no per-store append, info, or reload action.

## 2. Write the session-journal child first

A session journal records the story of this segment, which the four stores do
not capture. Write one **sub-entry** under `knowledge/session-journal/`, not a
new top-level Knowledge entry:

```
knowledge/session-journal/
├── KNOWLEDGE.md
└── <YYYY-MM-DD>-molt-<molt-count>-<slug>/KNOWLEDGE.md
```

The parent `KNOWLEDGE.md` is a short routing index: add one line containing the
date, slug, one-sentence hook, and the child's relative path. The child is the
substance and must be written before the molt summary. Use
`assets/session-journal-entry-template.md` for its required frontmatter and
sections. Read the current count from the resident identity statement (“You
have undergone N molts since birth”) and use that N in the pre-molt child name;
the result after molt reports N+1 for the next segment.

The path passed as `session_journal_path` must be the child path
`knowledge/session-journal/<entry>/KNOWLEDGE.md`, inside the workdir. It must
exist, be non-empty UTF-8, have valid YAML frontmatter with `name` and
`description`, and identify itself with `type: session-journal` or
`session_journal: true`. The parent index, a scratch file, or an absolute path
outside the workdir is not a valid substitute. The kernel validates this before
snapshot/archive/wipe or `molt_count` mutation. If the gate fails, the molt is
refused and context remains untouched.

Write a journal, not a transcript: capture what happened and why, point at
paths, branches, PRs, message IDs, or other anchors, and do not paste secrets or
large file contents. Fill the template sections with `None` rather than silently
omitting a consequential fact.

## 3. Write the summary and call molt

The summary is the only conversation-layer material the next you receives. It
should be a charge anchored in the stores, not a transcript. For a routine molt,
cover:

- current task, state, and next concrete step;
- completed work and key decisions;
- pending items, blockers, and open questions;
- collaborators and who is waiting on what;
- Knowledge, skill, Pad, or journal paths the next you should load; and
- the session-journal child path plus useful gotchas.

For a long-running task, multiple collaborators, pending human commitments,
open artifacts/worktrees, active jobs, or any hard-to-reconstruct handoff, read
`assets/molt-template.md`. It has the complete nine-section scaffold and
pre-molt checklist; fill every section and write `None` when a section does not
apply.

The call shape is:

```text
context(
    action="molt",
    input={
        "summary": <briefing for the next you>,
        "session_journal_path": "knowledge/session-journal/<entry>/KNOWLEDGE.md",
        "keep_tool_calls": null,
        "keep_last": null,
    },
    reasoning="why you are molting now",
)
```

The envelope always has one public `action`, that action's strict `input`, and a
root `reasoning`. The root `summarize` **boolean** is an unrelated result-
presentation control; it is never action input. Leave it false for Context's
small results, including `manual`, so the exact procedure is not summarized
away. `context(action="summarize")` is a separate record-only action; read
`reference/summarize-manual/SKILL.md` for its cadence and rebuild boundary.

### Input field dictionary

| Field | Semantics |
|---|---|
| `summary` | Required non-empty retrospective for the next session. The four stores and journal are tended before this call. |
| `session_journal_path` | Required validated per-segment child path. Missing or invalid input refuses before any context is shed. |
| `keep_tool_calls` | Nullable optional ordered list of prior tool-call IDs to replay. An unknown ID refuses before shedding; `null` keeps none. Keep this list short because durable stores are primary. |
| `keep_last` | Nullable optional minimum count of recent conversation entries. `null` means 20; `0` archives everything. The retained suffix can expand to keep an adjacent assistant tool-call/result batch whole; overlap with `keep_tool_calls` is deduplicated. |

The private `_tc_id` metadata injected by the kernel is not a public input field.
Molt uses it to locate and replay its precise ToolCallBlock. The true
system-forced `context_forget` path is distinct and synthesizes its own
model-visible call/result pair; do not imitate it as an agent-initiated molt.

## 4. What survives and what changes

Validation and keep-list checks happen before snapshot, archive, wipe, or count
mutation. A successful agent molt preserves the four stores, the session-journal
child, history under `history/`, summaries under `system/summaries/`, and
notification files under `.notification/`. It archives and replays according to
the accepted keep set, updates `molt_count`, writes the molt summary when it
can, runs the one canonical reconstruction hook before the fresh session, and
publishes the post-molt continuation notification. The durable molt event key is
unchanged; only the public action/root names are current.

`context(action="molt")` does not update the stores for you. Neither summarize
mode nor molt is a substitute for tending Pad, character, Knowledge, Skills, or
the session journal. If a required write fails, stop and recover before trying
to shed context.

## 5. Pressure, rebuild, and when to molt

Context pressure is a decision aid, not an automatic molt order. The reminder is
stamped only after sustained high usage; read
`reference/summarize-manual/SKILL.md` for the urgent/idle cadence, a-priori versus
a-posteriori summarization, delayed provider reconstruction, and the 0.85/1.0
boundaries. If a summarize/rebuild pass still leaves context above its recovery
target, tend the stores and molt deliberately rather than looping summarize or
rebuild.

The cache-miss budget is System-owned guidance. `LINGTAI_CACHE_MISS_BUDGET` may
override the positive integer at runtime; an invalid value falls back to the
System file and then the fixed default. The cumulative total survives refresh or
restart. Read `system-manual` for the file format; keep both a cache-budget and
context-pressure warning when both are active.

## 6. After a system-performed molt

A system-forced molt (karma, `.clear`, or operator; not a context-pressure
reminder) publishes a post-molt notification pointing at a system-authored
summary under `system/summaries/`. Canonical prompt sources, including character
and Pad, were reconstructed; recent conversation may be gone except for entries
the system explicitly retained. Recover in this order:

1. Read `summary_path` from the post-molt notification.
2. Call `email(check)` to see what arrived while you were down.
3. Check `knowledge/session-journal/KNOWLEDGE.md` for the session-history index.
4. Read the `skills` section of the system prompt for installed procedures.
5. Inspect the relevant tail or filtered lines of `logs/events.jsonl` only when
   needed; do not load the whole log.

Reconstruct the situation from those durable sources. The full activity log is
evidence, not a durable-memory substitute; use a surgical lookup by
`tool_call_id` or another known anchor.

## Pre-molt checklist

Before the call, verify:

- Pad, LingTai/character, Knowledge, Skills, and the journal child are updated
  where needed.
- The parent session-journal index has a new relative-path hook, and the child
  was written before the summary.
- The summary names every outstanding task, matching action, collaborator,
  recipient/channel, and useful path.
- Active background work, pending replies, authorization limits, and
  secret/privacy constraints are explicit.
- The first five minutes after wake are obvious.
