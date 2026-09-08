---
name: context-manual
description: |
  Read before summarize/rebuild/molt, session journaling, consequential handoff or context-loss recovery.
version: 2.1.2
last_changed_at: "2026-09-04T00:00:00Z"
related_files:
- src/lingtai/tools/context/__init__.py
- src/lingtai/tools/context/_molt.py
- src/lingtai/tools/context/_session_journal.py
- src/lingtai/tools/system/summarize.py
- src/lingtai/agent.py
- src/lingtai/tools/psyche/CONTRACT.md
- src/lingtai/tools/context/manual/reference/molt-manual/SKILL.md
- src/lingtai/tools/context/manual/reference/summarize-manual/SKILL.md
- src/lingtai/tools/context/manual/assets/molt-template.md
- src/lingtai/tools/context/manual/assets/session-journal-entry-template.md
maintenance: |
  This package is the canonical Context manual source and runtime-installed owner.
  Keep this entry a short router: put complete procedures in the focused
  references and update those references when the tool/capability behavior changes.
---

# Context Manual

This is the short router for `context`: `molt`, `summarize`, `rebuild`, and
`manual`. Use the action that matches the job; load the focused reference before
an involved operation. The manual action is directly callable as
`context(action="manual", input={}, reasoning="load context guidance")` and has
no pre-read dependency.

## First-call essentials

Every call uses the closed envelope: `action`, that action's strict `input`
object, and root `reasoning`. The optional root `summarize` is a boolean presentation
control; it is **not** child input and should normally be `false` here. It is
unrelated to the `summarize` action below.

| Action | Use this first | Load for depth |
|---|---|---|
| `molt` | Shed conversation while retaining durable stores. `input.summary` and a valid `input.session_journal_path` are required before anything is shed. | `reference/molt-manual/SKILL.md` and `assets/session-journal-entry-template.md`; for a consequential handoff also `assets/molt-template.md`. |
| `summarize` | Record one or more non-empty `{tool_call_id, summary}` replacements. The raw event remains retrievable and this action does **not** rebuild the provider context. | `reference/summarize-manual/SKILL.md`. |
| `rebuild` | The ordinary call is `input={}` (or `{"items": null}`): it is schema-valid even with zero pending summaries. It recomposes all canonical prompt sources, then applies summaries, then requests provider replay. Make one tactical call; do not loop. | `reference/summarize-manual/SKILL.md` for delayed reconstruction and recovery. |
| `manual` | Use strict `input={}`; it returns this installed manual without a lifecycle operation. | The reference named by the need. |

## Molt gate

Before `molt`, tend the four durable stores (Pad, LingTai/character,
Knowledge, and Skills) and write the session-journal **sub-entry** first. The
path must be inside the workdir and look like
`knowledge/session-journal/<entry>/KNOWLEDGE.md`; it must be non-empty UTF-8,
have `name`/`description` frontmatter, and carry `type: session-journal` (or
`session_journal: true`). The parent index is not a valid argument. A missing
or invalid journal is refused before snapshot, archive, wipe, or count mutation.
The full store rhythm, journal format, keep-field semantics, summary scaffold,
and post-molt checklist are in `reference/molt-manual/SKILL.md`.

`keep_tool_calls` is an optional ordered list of prior IDs (`null` keeps none);
an unknown ID refuses the molt before shedding. `keep_last` is an optional
minimum recent-entry count (`null` defaults to 20, `0` archives everything);
whole assistant tool-call/result batches may expand the retained suffix and
overlaps are deduplicated. Do not improvise a consequential summary: use
`assets/molt-template.md`.

## Mutation and recovery rules

Generic durable mutations do not hot-load. Use `file.write` or `file.edit`, then
make one explicit `context(action="rebuild", input={}, ...)` call when the
change must apply now; there is no per-store append/info/reload action. Passive
refresh and molt use the same canonical reconstruction contract while retaining
their distinct lifecycle effects. If reconstruction fails, it must not apply
summaries or request provider replay; follow the recovery guidance in the
focused references.

Summarize is not durable memory and neither summarize mode updates Pad,
character, Knowledge, Skills, or the session journal. At a whole-session
boundary, tend those stores and molt deliberately rather than using summarize
as a mini memory layer. For sustained context pressure, delayed rebuild,
cache-miss budget, or post-wipe recovery, load
`reference/summarize-manual/SKILL.md` and `reference/molt-manual/SKILL.md` as
appropriate.

## Reference and asset catalog

Focused references keep detailed recipes, field semantics, thresholds, recovery
steps, and troubleshooting out of this first response:

- `reference/molt-manual/SKILL.md` — durable-store tending, session-journal
  format/gate, summary and keep-field recipes, pressure reminder, and post-wipe
  recovery.
- `reference/summarize-manual/SKILL.md` — a-priori versus a-posteriori
  summarization, delayed provider reconstruction, thresholds, raw-result
  recovery, and the summarize-versus-molt decision.
- `assets/session-journal-entry-template.md` — write the required journal
  sub-entry before the molt.
- `assets/molt-template.md` — nine-section scaffold and pre-molt verification
  checklist for consequential handoffs.

Read only the reference that answers the current need. The installed root
manual remains the canonical entry point and its relative links are packaged
with it.
