---
name: psyche-manual
last_changed_at: 2026-09-04T00:00:00Z
description: >
  Routing table for the four durable domains — pad, lingtai (灵台), knowledge,
  skills — behind one shared file-mutation/rebuild model; also documents the
  separate `.rules` heartbeat-signal protocol (not a psyche action).
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
- tests/test_psyche_family.py
- tests/test_avatar_rules.py
maintenance: |
  This is the psyche family's own manual, loaded by
  `psyche(action='manual', input={}, reasoning='...')`.
  It is a routing table by design: keep it short and keep the depth in the four
  domain manuals it points to. Update it together with
  src/lingtai/tools/psyche/{CONTRACT,ANATOMY}.md whenever the public action
  inventory, owned settings, a domain's durable source, or the rebuild model
  changes. The `.rules` network-rules protocol section is a signpost target
  from `avatar-manual` §9 (Avatar owns no rules action or mechanism); keep it
  in sync with `src/lingtai/kernel/base_agent/lifecycle.py`'s
  `_check_rules_file` if that consumer's semantics change.
---

# Psyche

Your **psyche** is what survives a molt: the four durable domains that are
re-read and recomposed into every fresh system prompt.

> pad + lingtai + knowledge + skills = psyche

`psyche` is the one public root that teaches them. Its five domain/routing
actions return manuals; `settings` shows a bounded, fully redacted inventory of
Psyche's Pad configuration plus its six configurable prompt-owner inputs. Every public action is read-only. It owns no
lifecycle action: molt, summarize, and rebuild belong to `context`, and your
name belongs to `system`.

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
provider or manual loader runs.

## The one mutation model

`psyche` has **no** mutating action. That is deliberate, not an omission:
durable content is ordinary text, so it is changed by the ordinary text tools.

1. **Write** the durable source with `file.write` (create or full overwrite) or
   `file.edit` (exact replacement).
2. **Apply** it with one explicit
   `context(action="rebuild", input={}, reasoning="apply durable changes")`.

File mutation never hot-loads the prompt: a durable change written but not
rebuilt is real on disk and simply not yet visible in your context — which is what
makes a batch of edits land atomically instead of one half-composed section at a
time. A full rebuild recomposes **all** enabled canonical sections once, applies
pending summaries, then requests provider replay; passive reconstruction
(`system(action="refresh", ...)` and molt) runs the same contract. There is no
per-domain reload to call.

Catalog upkeep is not yours to trigger either. Skills and Knowledge catalogs are
rescanned and recomposed by that same reconstruction path (and at setup/refresh);
authoring a new `KNOWLEDGE.md` or `SKILL.md` and then rebuilding is the whole
procedure.

## Which domain am I in?

- Working notes, the current task, the living index you tend every turn → **pad**.
- Who you are, your voice, how you carry yourself → **lingtai** (灵台).
- Something you learned, decided, or discovered and want back after a molt,
  possibly referencing local paths, mail ids, or logs → **knowledge**.
- A reusable procedure that would help any agent, not just you → **skills**.

When the choice is genuinely unclear, the domain manuals own that distinction in
depth.

## `summarize`

**Short-result.** Manual actions return one manual body, and `settings` returns
eight compact rows. Leave root `summarize` `false`; summarizing either result loses
the exact procedure or inventory you called it for.

## Settings

`psyche(action="settings", input={}, reasoning="inspect Psyche prompt configuration")`
is SHOW only. It returns exactly `pad`, `pad_file`, `base_prompt`,
`base_prompt_file`, `covenant`, `covenant_file`, `comment`, then `comment_file`.
Every row has exactly `key`, `current`, `default`, `configurable`, and `comment`
in that order. Both `current` and `default` are always `<redacted>` for every
row, including empty or absent values. The action reports the applied snapshot
from the last successful full reconstruction. Ambient source edits do not change
SHOW until rebuild, refresh, or molt applies them; a failed reconstruction keeps
the last successful snapshot. A provider/snapshot failure returns only the fixed
bounded `SETTINGS_UNAVAILABLE` failure, never partial rows or parser details.

`settings/psyche.json` is Psyche's deliberately small owner document. It is
optional: a missing file means schema v1 with all six owner values absent. When
present it must be UTF-8 JSON object `{"schema_version": 1, ...}` with no keys
other than `base_prompt`, `base_prompt_file`, `covenant`, `covenant_file`,
`comment`, and `comment_file`; each present value is a string (including `""`).
Duplicate/unknown keys, Boolean/non-1 versions, non-regular or symlink files,
files over 64 KiB, unstable reads, invalid UTF-8/JSON, and read failures reject
the complete reconstruction before prompt publication. There is no environment
layer, `set`, `reset`, patch action, migration, or writeback.

The legacy top-level init spellings for these six fields are compatibility-known
but inert. They neither configure the prompt nor populate SHOW. For each owner
pair, a readable `*_file` wins; a missing file falls back to inline. `~` expands
and relative pointers resolve against the agent workdir. Edit the owner document
with `file.write`/`file.edit`, then use `context.rebuild` (or refresh/molt) to
apply it atomically.

**Upgrade / external-writer note:** before reconstructing an agent that still
stores any of these six values in `init.json`, copy them into
`settings/psyche.json`; the runtime never migrates or writes them back. An
existing resolved `base_prompt` or `covenant` may continue through its
`system/base_prompt.md` or `system/covenant.md` mirror, but `comment` has no
mirror and is removed when the owner document does not supply it. Fresh project,
recipe, TUI, Avatar, or other external seed writers must emit the Psyche owner
document instead of relying on the inert init spellings.

### Setting pad

- **Meaning and default:** the configured UTF-8 initial Pad seed. Its meaningful
  default is the empty string.
- **Source and precedence:** top-level `pad_file` wins when it names a readable
  file; otherwise top-level inline `pad` is the fallback. Reconstruction
  materializes, validates, and path-resolves that shape once; SHOW reports the
  resulting applied snapshot without rereading either source.
- **Configurable:** `true`, because the operator may edit the authorized root
  init source or the file it names. SHOW still fully redacts the effective body.
- **Apply timing and procedure:** active or passive full reconstruction seeds
  `system/pad.md` only when that durable body is missing or empty. A nonempty
  durable Pad is preserved. To replace one, edit `system/pad.md` through the Pad
  procedure, then call `context(action="rebuild", input={}, reasoning="apply Pad change")`;
  changing only the configured seed does not overwrite a nonempty durable Pad.

### Setting pad file

- **Meaning and default:** the configured file pointer supplying the initial Pad
  seed. It has no meaningful default, so its underlying default is `null`.
- **Source and precedence:** top-level `pad_file` only. `~` expands and a
  relative path resolves against the agent working directory. A readable file
  supplies `pad`; a missing or blank pointer falls back to inline `pad`. SHOW
  fully redacts the resolved pointer as well as the Pad body.
- **Configurable:** `true`, because the operator may edit the authorized root
  init source. No environment or settings-file layer exists.
- **Apply timing and procedure:** the pointer is re-read on full reconstruction,
  but its content remains only an initial seed for a missing/empty
  `system/pad.md`. To change a nonempty durable Pad, use the Pad file procedure
  and rebuild instead of expecting `pad_file` to overwrite it.

A second SHOW before reconstruction deliberately reports the same applied
snapshot. After an authorized rebuild/refresh/molt succeeds, another SHOW can
verify that discovery remains available, but because both values are always
redacted it cannot reveal or compare the underlying content.

### Setting base prompt

The optional third-party/application prompt body; default `""`, configurable
`true`. Psyche resolves it once per reconstruction, writes nonempty content to
`system/base_prompt.md`, and otherwise falls back to that mirror. The kernel
renders it after raw `principle` and before the remaining Batch 1 sections.

### Setting base prompt file

Optional pointer; default `null`, configurable `true`. A readable file wins
over `base_prompt`; its path is fully redacted and follows the owner-document
relative/`~` rules above.

### Setting covenant

Optional protected operator-contract body; default `""`, configurable `true`.
Nonempty resolved content mirrors to `system/covenant.md` and uses the existing
protected covenant section; absent owner content falls back to that mirror.

### Setting covenant file

Optional pointer; default `null`, configurable `true`. A readable file wins
over `covenant`; the pointer and resolved body remain fully redacted.

### Setting comment

Optional unprotected comment-section body; default `""`, configurable `true`.
Unlike base prompt and covenant, comment has no `system/*.md` mirror: an absent
owner value removes the section on the applied reconstruction.

### Setting comment file

Optional pointer; default `null`, configurable `true`. A readable file wins
over `comment`; it is fully redacted and has no mirror behavior.

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
