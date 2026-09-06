---
name: lingtai-manual
description: |
  Read before changing character/identity in `system/lingtai.md`, choosing forced versus self-evolve mode, or applying durable identity edits.
version: 2.0.0
last_changed_at: 2026-07-28T00:00:00-07:00
related_files:
- src/lingtai/tools/lingtai/__init__.py
- src/lingtai/tools/lingtai/_lingtai.py
- src/lingtai/tools/lingtai/CONTRACT.md
- src/lingtai/tools/psyche/CONTRACT.md
- src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
- src/lingtai/tools/context/manual/SKILL.md
maintenance: |
  Keep the manual-only public route through psyche, generic file ownership, the no-hot-load rule, identity modes, and the context reconstruction route synchronized with code.
---

# LingTai Manual

Your 灵台 is the character that distinguishes you from every other agent. Its
durable source is `system/lingtai.md`; canonical reconstruction renders it into
the protected `character` system-prompt section.

## Public call

```text
psyche(action="lingtai", input={}, reasoning="load identity guidance", summarize=false)
```

That is the **only** public LingTai call, and it just returns this manual. It
performs no disk or prompt mutation. There is no update, load, or reload action
and no compatibility alias.

## Change durable identity with file

Your durable identity is an ordinary file. Rewrite it with
`file(action="write", input={"file_path": "system/lingtai.md", ...})` — carrying
forward everything you intend to keep — or make a bounded, unambiguous change
with `file(action="edit", ...)` on the same path. Neither hot-loads the prompt;
apply with one `context(action="rebuild", ...)`. The full model is
`psyche-manual` → "The one mutation model".

## Identity modes

- **Self-evolve:** configured `lingtai`/`lingtai_file` is absent or empty.
  Reconstruction preserves and composes your self-authored
  `system/lingtai.md`.
- **Forced:** configuration resolves to a nonempty identity. Every
  reconstruction materializes that configured value into `system/lingtai.md`
  before composing it, so a file change is replaced at the next rebuild,
  refresh, or molt.

Keep character separate from operator `covenant`, third-party `base_prompt`,
and mechanical name/manifest `identity`. Names remain
`system(action="name_set"|"name_nickname")`.

Before molt, tend identity once when the task's lessons genuinely changed who
you are; use `context-manual` for the journal/summary/molt procedure.
