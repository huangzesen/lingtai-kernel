---
name: notification-manual
description: >
  Router for the notification filesystem protocol and the standalone
  `notification` tool: read when interpreting `.notification/<channel>.json`,
  choosing producer-specific handling, or safely dismissing a mirror.
  Large-result/context compaction belongs to `context-manual`, not here.
version: 0.15.0
tags: [lingtai, notifications, channels, dismiss, delay, alarm, settings, manual, force, stale, nudge, hooks, whitelist]
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/prompts/meta_guidance/catalog/notification_handling.md
- src/lingtai/tools/notification/ANATOMY.md
- src/lingtai/tools/notification/CONTRACT.md
- src/lingtai/tools/notification/__init__.py
- src/lingtai/kernel/tool_plugin/CONTRACT.md
- src/lingtai/tools/notification/schema.py
- src/lingtai/tools/notification/settings.py
- src/lingtai/tools/notification/manual/reference/channel-model/SKILL.md
- src/lingtai/tools/notification/manual/reference/dismissal-safety/SKILL.md
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# Notification Manual — Router

`notification` is the sole agent-callable surface for reading and clearing the
current `.notification/<channel>.json` mirrors. `system` has no notification or
dismiss alias; compaction is `context(action='summarize')`.

## Quick start

The resident schema is the source of truth for eleven strict actions in the
`action` + `input` + `reasoning` envelope. Begin with:

```text
notification(action='check', input={}, reasoning='inspect current notifications')
```

`check` is read-only and returns a placeholder whose live payload is attached by
the kernel. After handling a notification, use the narrowest matching dismiss
action and do not call `check` merely to confirm the clear. Follow a producer's
own read/dismiss verb when `instructions` names one: generic dismissal clears
the mirror only, never producer state. Reread current state after a stale
refusal; use `force=true` only for a confirmed stale mirror, never for producer
or protected state. Read the dismissal-safety reference before forcing and the
channel-model reference when interpreting payloads, hooks, delay, or delivery.

Optional fields are required-but-nullable in the provider schema; `null` means
omitted. `dismiss_channel` requires `channel`; `dismiss_event` and
`dismiss_ref` default `channel` to `system`. A post-molt dismissal requires a
non-empty `continue|defer|obsolete: ...` reason. `manual` and `settings` accept
only `input={}` and are read-only.

## Consumer delay and expiry alarm

`notification(action='delay', input={'channel': '<allowed>', 'seconds': 0 or a positive configured cap}, reasoning='...')`
hides consumer delivery for one allowed target only. `seconds: 0` cancels the
matching delay; a nonzero call replaces the previous live delay. Producer files
keep receiving updates. The live cap is
`LINGTAI_NOTIFICATION_DELAY_MAX_SECONDS` (default `600`); missing, blank,
non-numeric, non-positive, or non-finite values use that default. `delay-alarm`
cannot be targeted. At expiry or recovery, delivery resumes and one
high-priority `delay-alarm` mirror records bounded evidence; persistence and
recovery details belong to the channel-model reference.

The SHOW row is `notification.delay_max_seconds`: `current` is the effective
positive integer from the live environment or the default `600`, and invalid
input falls back to `600`. SHOW does not write or refresh anything. Change the
existing launcher or `env_file` only with configuration-owner approval, then
perform the authorized refresh/relaunch and verify with `settings`.

## Notification settings

`notification(action='settings', input={}, reasoning='inventory')` is read-only.
It returns exactly two rows, in order, each with only `key`, `current`, `default`,
`configurable`, and `comment`:

- `notification.max_chars` →
  `notification-manual#block-size-cap-persistent-and-attention-lanes`
- `notification.delay_max_seconds` →
  `notification-manual#consumer-delay-and-expiry-alarm`

The comment targets are the source of truth for meaning, precedence, accepted
values, and authorized change/verification procedures. If either current value
is unavailable, the whole action fails; it never returns partial rows. Channel
payloads, accounts, hook manifests, delay state, and session state are not
settings rows.

## Root `summarize`

Notification is a short-result family. Leave the root `summarize` boolean false
when retrieving `manual`, because exact procedures and constraints matter. Use
`context(action='summarize')` for tool-result compaction and recovery; the
legacy `large_tool_result` reminder is only an escape hatch and never changes
producer state.

## Installed manual retrieval

`notification(action='manual', input={})` reads only:

```text
<agent>/.library/intrinsic/capabilities/notification/SKILL.md
```

Success returns exactly `status`, `notification_manual`, and `manual_path`. A
missing installed file is degraded with an empty body and an actionable `error`;
there is no source-checkout fallback. Manual retrieval neither reads nor writes
`.notification/`, producer state, delay state, or logs.

## Hooks & whitelist

External hooks must be registered with `add` before their channel is accepted.
The effective allowlist is the built-in set, `mcp.*`, and channels registered by
this agent's workdir; it is not process-global. `drop` revokes registration but
never stops the hook process. Use the manifest's `how_to_cancel` to stop it.
The channel-model reference owns the setup flow, manifest fields, registry
behavior, and blocked-channel warn-and-flag details.

## Block size cap (persistent and attention lanes)

`notification.max_chars` is one shared character cap (default `10000`, bounded
`2048..10000`) for the persistent and attention lanes. The effective value reads
live environment `LINGTAI_NOTIFICATION_MAX_CHARS`, then valid System-v2
`notification_max_chars`, then the default; malformed values fall through.
Oversized payloads are atomically spilled and compacted while preserving routing
ids, with a marker-only fallback when necessary. This is a context-size control,
not delivery or access control. Spill names, compaction order, and recovery are
owned by the channel-model reference.

The SHOW row `notification.max_chars` reports that same effective clamped value.
Change only through the existing authorized environment or closed System-v2 owner
procedure; an environment/launcher change needs the authorized refresh or
relaunch, while the file layer is hot-read. SHOW itself never writes, refreshes,
adds an `init.json` field, or creates a Notification settings file.

## Routing table

| Need / keywords | Read |
|---|---|
| First read; channel names; `.notification/*.json`; envelopes; `check`; delivery; allowlist; hooks; delay; block cap; settings sources | `reference/channel-model/SKILL.md` |
| Which dismiss action; producer-specific handling; stale mirror; `force`; protected `goal`; post-molt reason; legacy `large_tool_result` | `reference/dismissal-safety/SKILL.md` |
| Tool-result ranking, digest quality, `context(action='summarize')`, recovery by `tool_call_id` | `../context-manual/reference/summarize-manual/SKILL.md` |
| Active goal cancellation/completion; runtime/kernel update nudges | `../system-manual/reference/goal-manual/SKILL.md` and `../system-manual/reference/runtime-update-checks/SKILL.md` |
