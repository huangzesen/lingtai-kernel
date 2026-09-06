---
name: avatar-spawn-reference
description: |
  Avatar-manual reference for spawn calls: canonical identity/path gates,
  shallow/deep payloads, mission quality, dry-run/confirmation, and prompt
  inheritance.
version: 1.0.0
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/avatar/manual/SKILL.md
- src/lingtai/tools/avatar/__init__.py
- src/lingtai/tools/avatar/settings.py
- src/lingtai/tools/avatar/CONTRACT.md
- src/lingtai/tools/psyche/settings.py
- src/lingtai/tools/psyche/CONTRACT.md
- src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
maintenance: |
  Tracks Avatar's spawn and identity procedure. Keep it aligned with the
  Avatar implementation, contract, settings anchors, and parent router; move
  detailed procedure here rather than expanding the top manual.
---

# Avatar spawn and identity

Nested reference for the Avatar Manual. Open this after the first-call manual
read when preparing a real spawn.

## Call contract

The canonical call is:

```text
avatar(action="spawn",
       input={"name": "researcher", "type": "shallow", "comment": null,
              "dry_run": false, "confirm": false},
       reasoning="A concrete mission of at least 20 characters")
```

The root `reasoning` is the mission brief and becomes the child's first prompt.
It is not a member of `input`; putting `reasoning`, `_reasoning`, or `summarize`
in nested input is invalid. `input` is strict and action-specific. Optional
`type`, `comment`, `dry_run`, and `confirm` may be null, which means absent and
uses the fixed default. `action` is explicit and has no default.

## Canonical name and path gate

`input.name` is both the avatar's public agent name and the basename of its
working directory. It must be 1–64 characters and match the Unicode-aware
`^[\\w-]+$` rule: letters, digits, underscore, and hyphen only. No dot, slash,
space, control character, or leading dot is accepted. The target is a direct
sibling of the parent directory and must remain inside the network root. The
manager checks the resolved path before mutating anything; an existing target
or an active ledger peer is refused.

Do not supply an alternate directory. A name-shaped public call is the only
supported identity/path input, which keeps ledger identity and filesystem
identity canonical.

## Choose a payload

- **Shallow (default, 初生):** copies the parent's `init.json` and only the
  narrow Psyche base/covenant owner inputs. It is a blank slate: no parent
  identity, pad, knowledge, history, brief, addons, or admin privileges.
- **Deep (二重身):** also copies `system/`, `knowledge/`, `exports/`, and
  `combo.json`, while retaining a fresh conversation. It is a character/knowledge
  doppelgänger, not a second in-process handle. Normal deep copy includes an
  existing `system/rules.md`; Avatar does not create or distribute rules.

Both modes set the child `agent_name`, blank the inherited `lingtai` seed,
strip identity/history and kernel/secretary overrides, and pin the parent's
default preset. Relative preset paths are re-rooted against the parent's
working directory. The child receives no inherited parent comment or brief.

## Mission, preview, and confirmation

Write `reasoning` as an actionable brief: objective, relevant paths/resources,
reporting route, done condition, and constraints. The parent identity/address
is supplied by the system prompt; do not rely on a vague one-line rationale.

Before any filesystem mutation, the mission-quality gate flags an empty mission,
a mission shorter than 20 characters, or a mission equal to/starting with the
placeholder tokens `bar`, `check`, `debug`, `foo`, `temp`, `test`, and `tmp`.
The result is `status="confirmation_needed"` with a preview unless
`confirm=true`. `confirm` is an acknowledgement, not a grant of authority;
it does not bypass name/path or host lifecycle checks.

Use `dry_run=true` to preview after loading the parent's `init.json`. Dry-run
short-circuits before creating a directory, writing a ledger, marker, or
`.prompt`, and before launching a process. It is exempt from the mission-quality
gate so an inspection can show whether confirmation would be needed.

## Child prompt and persistent comment

Avatar builds a child `init.json` from the parent while clearing newborn admin,
materialized `llm`/`capabilities`, inert legacy prompt fields, addon state, and
other parent-only overrides. The child re-materializes from the parent's
**default** preset on first boot.

The separate `settings/psyche.json` retains only the Psyche base/covenant owner
inputs, anchors relative pointers to the parent workdir, replaces `comment`
with the new spawn comment, and omits `comment_file`. The comment is rendered
after `meta_guidance` and before `rules`; that is its position, not precedence
over later sections. It is not inherited and survives refresh, molt, and wake.
Leave it empty unless it is a fact the child must always remember.

The parent identity and mission are delivered through a separate `.prompt` signal
file, consumed once. The `lingtai` character seed is blanked rather than being
used as a hidden mission channel.

## No automatic rules side effect

Avatar has no `rules` action and a successful shallow or deep spawn does not
write a `.rules` signal or broadcast rules. The `.rules` heartbeat and
`system/rules.md` persistence remain kernel state; use the Psyche manual for
that protocol.
