---
name: pad-manual
description: |
  Read before editing the living Pad, pinning references, or preparing Pad state for rebuild/molt.
version: 2.0.0
last_changed_at: 2026-07-28T00:00:00-07:00
related_files:
- src/lingtai/tools/pad/__init__.py
- src/lingtai/tools/pad/_pad.py
- src/lingtai/tools/pad/CONTRACT.md
- src/lingtai/tools/psyche/CONTRACT.md
- src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
- src/lingtai/tools/context/manual/SKILL.md
maintenance: |
  Keep the manual-only public route through psyche, generic file ownership of both durable sources, the no-hot-load rule, delayed activation, and the context-manual route synchronized with code. Pad exposes no mutating action.
---

# Pad Manual

Pad is your living index in `system/pad.md`, plus a durable list of pinned
read-only references in `system/pad_append.json`.

## Public call

```text
psyche(action="pad", input={}, reasoning="load Pad guidance", summarize=false)
```

That is the **only** public Pad call, and it just returns this manual. Pad has no
mutating action: both of its durable sources are ordinary files you edit with
`file`. There is no append, edit, load, or reload action and no compatibility
alias.

## Mutate durable Pad content with file

Pad's durable sources are ordinary files. Rewrite with
`file(action="write", input={"file_path": "system/pad.md", ...})`; make an exact
replacement with `file(action="edit", ...)` on the same path. Neither hot-loads
the prompt — apply with one `context(action="rebuild", ...)`. The full model is
`psyche-manual` → "The one mutation model".

## Pin references

The pinned list lives in `system/pad_append.json`: a JSON array of paths, each
workdir-relative or absolute. Edit it like any other durable file —

```text
file(action="write", input={"file_path": "system/pad_append.json",
  "content": "[\"notes/design.md\", \"src/api.py\"]"}, reasoning="pin references")
```

Write `[]` to clear the list. Reconstruction reads each listed file and appends
its contents to the Pad section as read-only reference.

Reconstruction re-reads each pinned file every time, so an edit to the list —
or to a pinned file — appears only after the next rebuild.

Two things to know, because nothing validates this list for you any more: a path
that does not exist is reported as `append_not_found` at compose time rather than
rejected at write time, and pinned content counts against your context budget —
keep the list short and text-only.

Keep Pad concise: current goal, state, next action, blockers, collaborators, and
pointers to substantive knowledge/artifacts. Archive completed narrative in
knowledge, not in an ever-growing Pad. Before molt, make durable Pad state
accurate, rebuild only if needed in the current context, then follow
`context-manual` for the journal/summary/molt procedure.

Results are short; leave root `summarize` false.
