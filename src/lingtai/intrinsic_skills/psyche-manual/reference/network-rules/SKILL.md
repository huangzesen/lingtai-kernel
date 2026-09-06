---
name: psyche-network-rules-reference
last_changed_at: 2026-09-06T00:00:00Z
description: >
  Deep reference for the separate `.rules` heartbeat signal: authorized atomic
  writes, consumption, replacement, persistence, verification, and boundaries.
related_files:
- src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
- src/lingtai/kernel/base_agent/lifecycle.py
- src/lingtai/kernel/base_agent/__init__.py
- src/lingtai/tools/avatar/manual/SKILL.md
- tests/test_avatar_rules.py
maintenance: |
  Keep this reference synchronized with `_check_rules_file` and the Avatar
  manual's signpost. `.rules` is not a Psyche action; preserve that boundary and
  do not turn this reference into a generic instruction or mutation API.
---

# Psyche network-rules reference

This page is the detailed procedure behind the short `.rules` signpost in
[`psyche-manual`](../../SKILL.md). It documents a separate heartbeat signal, not
an action exposed by Psyche.

## Network rules protocol (`.rules`)

`.rules` is a real mechanism, but it is **not** owned by `psyche`, `avatar`, or
any other action tool — there is no `psyche(action='rules')` and no generic
instruction API for it. It is a plain signal file consumed by an agent's own
heartbeat loop (`_check_rules_file` in
`src/lingtai/kernel/base_agent/lifecycle.py`), documented here because it is
easy to confuse with the ordinary Psyche edit-then-`context.rebuild` model
above — the two are deliberately different mechanisms:

- **Ordinary Psyche edit:** write a durable source file (`system/pad.md`,
  `system/lingtai.md`, a `KNOWLEDGE.md`/`SKILL.md`, or the Psyche owner
  document), then call `context(action="rebuild", ...)` (or wait for
  refresh/molt) to recompose **all** enabled sections at once.
- **`.rules`:** use Shell to write a `.rules` file to the explicitly authorized
  target agent's working-directory root. Its next runnable heartbeat reads and
  unlinks the signal before deciding whether to apply it; no explicit rebuild
  or refresh is needed. A read or unlink failure leaves the signal unconsumed
  and stops processing, so file disappearance alone is not proof of success.

### Write through Shell

First prepare the complete approved UTF-8 rule body and confirm the exact target
path. For example, in a POSIX shell (use the active shell's equivalent elsewhere):

```sh
target='/absolute/path/to/authorized-agent'
body='/absolute/path/to/approved-rules.txt'
tmp=$(mktemp "$target/.rules.XXXXXX") &&
  cat "$body" > "$tmp" &&
  mv "$tmp" "$target/.rules"
```

The temporary file is in the target directory so the final rename exposes the
complete signal, not a partly written body. If a command fails, stop and inspect
that exact temporary file; do not announce success or blindly replay the batch.
For multiple agents, confirm an explicit target list and perform/report the write
for each target. There is no automatic descendant broadcast or post-spawn fan-out.
Do not write `system/rules.md` as a substitute for this live signal workflow.

Consumption semantics, exactly as implemented:

- **Complete replacement, not a merge.** A non-empty `.rules` body entirely
  replaces the canonical `system/rules.md` content and the protected `rules`
  prompt section — it is never appended to or merged with prior rules.
- **Empty is a no-op.** Whitespace-only or empty `.rules` content is consumed
  (the signal file is deleted either way) but writes nothing and triggers no
  prompt refresh.
- **Identical content is a no-flush no-op.** If the `.rules` body (stripped)
  equals the existing `system/rules.md` (stripped), the signal is consumed
  but `system/rules.md` is not rewritten and the system prompt is not
  reflushed.
- **Changed content persists and flushes.** A genuinely different body
  overwrites `system/rules.md`, rewrites the protected `rules` prompt
  section, and flushes the live system prompt — logged as `rules_loaded`.
  A write failure while persisting the canonical file is logged as
  `rules_write_error` and aborts before any prompt mutation.
- **Boot/rebuild injection.** `system/rules.md` is also re-read directly into
  the protected `rules` prompt section on ordinary agent construction and on
  every full reconstruction (`context.rebuild`, refresh, molt) — independent
  of any pending `.rules` signal. This is what makes existing rules survive a
  molt, refresh, or resume even without a fresh `.rules` write; an empty or
  missing `system/rules.md` at reconstruction time removes the section.

**Verification.** The canonical value is `system/rules.md` on disk — read it
directly. The effective (currently composed) value is what the protected
`rules` prompt section holds; a `.rules` write is only reflected there after
that *same agent's own* next heartbeat tick actually processed it (or after
any subsequent boot/rebuild/refresh/molt, which re-reads the same canonical
file). A `.rules` file that has not yet ticked is real on disk as a pending
signal, not yet visible in the prompt — the same "written but not applied"
distinction as an unbuilt Psyche source edit, but on the heartbeat's cadence
instead of an explicit `context.rebuild` call.

**Cross-agent writes are scoped by what you can reach, not by a privilege
check.** `avatar` used to own an admin/karma-gated `rules` action that wrote
`.rules` to a caller's own directory and to every descendant in its avatar
tree; that action and its authorization check were both **removed**, not
replaced by a new guard anywhere else. Today, writing a `.rules` file to
another agent's directory (e.g. a sibling avatar) is an ordinary filesystem
write — typically via `shell` naming that agent's explicit path — subject
only to whatever access the same-OS-user trust model already gives you, not
to any dedicated authorization mechanism. This paragraph is documentation,
not enforcement: do not treat it, or any other prose, as proof that a write
outside your own directory is authorized — only the human's actual scope and
the target's real accessibility decide that.
