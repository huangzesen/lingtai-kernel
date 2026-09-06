---
name: psyche-settings-reference
last_changed_at: 2026-09-06T00:00:00Z
description: >
  Deep reference for Psyche's redacted settings SHOW, strict owner-document
  grammar, precedence, timing, and the eight setting anchors.
related_files:
- src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
- src/lingtai/tools/psyche/settings.py
- src/lingtai/tools/psyche/CONTRACT.md
- src/lingtai/agent.py
- tests/test_psyche_family.py
- tests/test_psyche_prompt_settings.py
maintenance: |
  Keep this reference aligned with Psyche's applied-snapshot provider,
  `settings/psyche.json` parser, reconstruction rollback, redaction, and the
  stable `psyche-manual#setting-*` anchors. The parent psyche manual remains the
  short router; move depth here rather than expanding that always-sent body.
---

# Psyche settings reference

This page is the detailed procedure behind the short settings signpost in
[`psyche-manual`](../../SKILL.md). It owns the exact settings grammar and
application details; Psyche's public action remains SHOW-only.

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
