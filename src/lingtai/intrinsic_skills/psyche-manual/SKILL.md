---
name: psyche-manual
last_changed_at: 2026-09-06T00:00:00Z
description: >
  Short routing table for the four durable domains — pad, lingtai (灵台), knowledge,
  and skills — with one shared file-mutation/rebuild model; routes settings and the
  separate `.rules` heartbeat signal to focused references.
related_files:
- src/lingtai/tools/psyche/CONTRACT.md
- src/lingtai/tools/psyche/ANATOMY.md
- src/lingtai/tools/psyche/settings.py
- src/lingtai/agent.py
- src/lingtai/intrinsic_skills/pad-manual/SKILL.md
- src/lingtai/intrinsic_skills/lingtai-manual/SKILL.md
- src/lingtai/tools/knowledge/manual/SKILL.md
- src/lingtai/tools/skills/manual/SKILL.md
- src/lingtai/tools/context/manual/SKILL.md
- src/lingtai/kernel/base_agent/lifecycle.py
- src/lingtai/kernel/base_agent/__init__.py
- src/lingtai/tools/avatar/manual/SKILL.md
- src/lingtai/intrinsic_skills/psyche-manual/reference/settings/SKILL.md
- src/lingtai/intrinsic_skills/psyche-manual/reference/network-rules/SKILL.md
- tests/test_psyche_family.py
- tests/test_avatar_rules.py
maintenance: |
  This is the psyche family's own manual, loaded by
  `psyche(action='manual', input={}, reasoning='...')`.
  Keep it a short router: settings and `.rules` procedures belong to the two
  focused references below. Update it with src/lingtai/tools/psyche/{CONTRACT,ANATOMY}.md
  when the public action inventory, durable-source ownership, or rebuild model
  changes; keep the `.rules` signpost aligned with `avatar-manual § 9` and
  `_check_rules_file`.
---

# Psyche

`psyche` is the one public root for the four durable domains that survive a molt:

> pad + lingtai + knowledge + skills = psyche

## Routing table

| Call | Returns | Durable source it teaches |
|---|---|---|
| `psyche(action="pad", input={}, reasoning="load Pad guidance")` | `pad-manual` | `system/pad.md` and pinned Pad references |
| `psyche(action="lingtai", input={}, reasoning="load identity guidance")` | `lingtai-manual` | `system/lingtai.md` (灵台 / character) |
| `psyche(action="knowledge", input={}, reasoning="load knowledge guidance")` | the knowledge manual | `knowledge/<name>/KNOWLEDGE.md` entries |
| `psyche(action="skills", input={}, reasoning="load skills guidance")` | the skills manual | `.library/{intrinsic,custom}/` and configured skill paths |
| `psyche(action="settings", input={}, reasoning="inspect Psyche settings")` | a fully redacted settings view | Psyche's owner inputs and applied snapshot |
| `psyche(action="manual", input={}, reasoning="load the routing table")` | this router | — |

All six calls use strict empty `input` (`input={}`); any key is rejected before dispatch.
Every action is read-only. For an unfamiliar domain, call manual first; the
returned domain manuals own the detailed procedure.

## One mutation model

`psyche` has no mutating action. To change durable content, use `file.write` for
a full rewrite or `file.edit` for an exact replacement on the owning source, then
apply it once with `context(action="rebuild", input={}, reasoning="apply durable changes")`.
A file mutation never hot-loads the prompt; passive refresh or molt may apply the
same candidate. There is no per-domain reload, and catalog setup/refresh remains
owned by Skills or Knowledge.

## Lifecycle ownership

`psyche` owns no lifecycle action: `context` owns `molt`, `summarize`, and
`rebuild`, while `system` owns identity/name. Full reconstruction composes the
enabled durable sections together; domain manuals own their own catalog and
source procedures.

## Settings

`psyche(action="settings", input={}, reasoning="inspect Psyche settings")` is a
SHOW-only action with fully redacted values. Read the [settings reference](reference/settings/SKILL.md)
for the complete owner-document contract and application procedure.

### Setting pad
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-pad).

### Setting pad file
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-pad-file).

### Setting base prompt
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-base-prompt).

### Setting base prompt file
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-base-prompt-file).

### Setting covenant
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-covenant).

### Setting covenant file
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-covenant-file).

### Setting comment
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-comment).

### Setting comment file
Owner/default/apply contract: [settings reference](reference/settings/SKILL.md#setting-comment-file).

## Network rules protocol (`.rules`)

`.rules` is a separate heartbeat signal, not a Psyche action or a rebuild API.
Read the [network-rules reference](reference/network-rules/SKILL.md) before using
that boundary.

## `summarize`

Use the short-result profile and leave root `summarize` `false`: manual actions
return exact guidance, while settings returns its redacted view.
