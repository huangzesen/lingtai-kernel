---
name: psyche-manual
last_changed_at: 2026-09-06T00:00:00Z
description: >
  Short routing table for the four durable domains — pad, lingtai (灵台), knowledge,
  and skills — behind one shared file-mutation/rebuild model; routes settings
  and the separate `.rules` heartbeat-signal protocol to focused references.
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
  Keep this entry short: it is a routing table, while settings and `.rules`
  procedures live in the two focused references below. Update it together with
  src/lingtai/tools/psyche/{CONTRACT,ANATOMY}.md whenever the public action
  inventory, owned settings, a domain's durable source, or the rebuild model
  changes. The `.rules` signpost is also targeted by `avatar-manual § 9`;
  keep it in sync with `src/lingtai/kernel/base_agent/lifecycle.py`'s
  `_check_rules_file` if that consumer's semantics change.
---

# Psyche

Your **psyche** is what survives a molt: the four durable domains that are
re-read and recomposed into every fresh system prompt.

> pad + lingtai + knowledge + skills = psyche

`psyche` is the one public root that teaches them. Its five domain/routing
actions return manuals; `settings` shows a bounded, fully redacted inventory of
Psyche's Pad configuration plus its six configurable prompt-owner inputs. Every
public action is read-only. It owns no lifecycle action: `molt`, `summarize`, and
`rebuild` belong to `context`, and your name belongs to `system`.

## Routing table

| Call | Returns | Durable source it teaches |
|---|---|---|
| `psyche(action="pad", input={}, reasoning="load Pad guidance")` | `pad-manual` | `system/pad.md` + pinned references in `system/pad_append.json` |
| `psyche(action="lingtai", input={}, reasoning="load identity guidance")` | `lingtai-manual` | `system/lingtai.md` (your 灵台 / character) |
| `psyche(action="knowledge", input={}, reasoning="load knowledge guidance")` | the knowledge manual | `knowledge/<name>/KNOWLEDGE.md` entries |
| `psyche(action="skills", input={}, reasoning="load skills guidance")` | the skills manual | `.library/{intrinsic,custom}/` plus configured skills paths |
| `psyche(action="settings", input={}, reasoning="inspect Psyche prompt configuration")` | eight fully redacted five-field rows | Pad seed plus Psyche's prompt-owner document |
| `psyche(action="manual", input={}, reasoning="load the routing table")` | this routing table | — |

Every action takes a strict empty `input`; any key is rejected before its
provider or manual loader runs. For a domain you do not already know, load its
manual first. The domain manuals own their detailed procedures; this entry only
routes to them.

## The one mutation model

`psyche` has **no** mutating action. Durable content is ordinary text and is
changed by the ordinary text tools:

1. **Write** the domain's durable source with `file.write` (create or full
overwrite) or `file.edit` (exact replacement).
2. **Apply** it with one explicit
   `context(action="rebuild", input={}, reasoning="apply durable changes")`.

File mutation never hot-loads the prompt. A source written but not rebuilt is
real on disk and not yet visible in context, allowing a batch of edits to land
atomically. Full rebuild recomposes all enabled canonical sections once;
passive reconstruction (`system(action="refresh", ...)` and molt) follows the
same contract. There is no per-domain reload.

Catalog upkeep is not a Psyche action. Skills and Knowledge catalogs are
rescanned and recomposed by their own setup/refresh/reconstruction paths;
authoring a new `KNOWLEDGE.md` or `SKILL.md` and then rebuilding is the
procedure.

## Which domain am I in?

- Working notes, the current task, and the living index you tend every turn —
  **pad**.
- Who you are, your voice, and how you carry yourself — **lingtai** (灵台).
- Something learned, decided, or discovered for after a molt — **knowledge**.
- A reusable procedure that would help any agent — **skills**.

When the choice is unclear, the domain manuals own the deeper distinction.

## `summarize`

**Short-result.** Manual actions return one exact manual body, and `settings`
returns eight compact rows. Leave root `summarize` `false`; summarizing loses the
procedure or inventory you called it for.

## Settings

`psyche(action="settings", input={}, reasoning="inspect Psyche prompt configuration")`
is SHOW only. It returns exactly `pad`, `pad_file`, `base_prompt`,
`base_prompt_file`, `covenant`, `covenant_file`, `comment`, then `comment_file`.
Every row has exactly `key`, `current`, `default`, `configurable`, and `comment`.
Both `current` and `default` are always `<redacted>`, including empty or absent
values. SHOW reports the last successfully applied full-reconstruction snapshot;
an ambient edit does not change it until rebuild, refresh, or molt succeeds, and
a failed reconstruction preserves it. A provider or snapshot failure returns
only the fixed bounded `SETTINGS_UNAVAILABLE` result.

`settings/psyche.json` is the optional, closed v1 owner document for the six
prompt-owner fields. It has UTF-8 JSON object shape with integer
`schema_version: 1`; unknown/duplicate keys, invalid values, unsafe files, and
read races reject the complete reconstruction. It has no environment layer,
mutation action, migration, or writeback. The complete grammar, precedence,
apply timing, and all six setting meanings are in the
[settings reference](reference/settings/SKILL.md).

### Setting pad

The configured UTF-8 seed applies only when the durable Pad is missing or empty;
a nonempty `system/pad.md` is preserved. See the
[Pad setting procedure](reference/settings/SKILL.md#setting-pad).

### Setting pad file

A readable pointer supplies the initial Pad seed; it never overwrites a
nonempty durable Pad. See [Pad file setting](reference/settings/SKILL.md#setting-pad-file).

### Setting base prompt

The optional application prompt body is resolved during reconstruction and
renders after raw `principle`. See [base prompt](reference/settings/SKILL.md#setting-base-prompt).

### Setting base prompt file

A readable pointer wins over inline `base_prompt`; its path and body stay
redacted. See [base prompt file](reference/settings/SKILL.md#setting-base-prompt-file).

### Setting covenant

The optional protected operator contract is resolved during reconstruction and
uses the existing covenant section. See [covenant](reference/settings/SKILL.md#setting-covenant).

### Setting covenant file

A readable pointer wins over inline `covenant`; it remains redacted. See
[covenant file](reference/settings/SKILL.md#setting-covenant-file).

### Setting comment

The optional unprotected comment body has no `system/*.md` mirror; an absent
owner value removes the section. See [comment](reference/settings/SKILL.md#setting-comment).

### Setting comment file

A readable pointer wins over inline `comment`; it is redacted and has no mirror.
See [comment file](reference/settings/SKILL.md#setting-comment-file).

## Network rules protocol (`.rules`)

`.rules` is a real heartbeat signal, but it is **not** owned by `psyche` or an
action: there is no `psyche(action='rules')` and no generic instruction API.
Ordinary Psyche edits use file plus explicit rebuild; an authorized `.rules`
write is consumed by the target agent's next heartbeat instead. Read the
[network-rules reference](reference/network-rules/SKILL.md) for the exact
atomic write, consumption, replacement, persistence, and verification rules.
