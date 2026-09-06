---
name: notification-manual-channel-model
description: >
  Nested notification-manual reference for LingTai's notification filesystem
  channel protocol, allowlist, payload envelopes and instructions, nudge routing,
  kernel sync, voluntary check behavior, and canonical producer state versus
  notification mirrors. Read after notification-manual when interpreting,
  producing, or debugging notification payloads; skip for dismissal policy.
version: 0.7.0
tags: [lingtai, notifications, channels, protocol, sync, delay, alarm, nudge, hooks, whitelist]
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/notification/manual/SKILL.md
- src/lingtai/tools/notification/schema.py
- src/lingtai/kernel/notification_store/__init__.py
maintenance: |
  Tracks the notification channel-model/protocol topic it documents; update when that integration changes.
---

# Notification Channel Model

## Files and allowlist

A channel is the filename stem in `.notification/<channel>.json`:

- `.notification/email.json` becomes `_meta.agent_meta.notifications.attention.email`;
- `.notification/system.json` becomes `_meta.agent_meta.notifications.attention.system`;
- `.notification/mcp.telegram.json` becomes
  `_meta.agent_meta.notifications.attention["mcp.telegram"]`;
- `.notification/goal.json` becomes `_meta.agent_meta.notifications.attention.goal`.

The kernel accepts built-in channels including `email`, `system`, `soul`,
`nudge`, `post-molt`, `tool_loop_guard`, `bash`, `btw`, `cron`, `molt`, `goal`,
`daemon`, and `delay-alarm`; MCP bridge channels use the `mcp.` prefix. The
**effective allowlist** is `static ∪ mcp.* ∪ the agent's own registered hook
channels`, and is **per-agent, not process-global**: a hook channel is allowed
only for the agent whose workdir registered it. External hooks register
manifests via `notification(action='add', ...)`, which appends to
`.notification/hooks.json` and allowlists the manifest's `channel`.

Unknown JSON filenames are ignored by collection, and kernel publish/dismiss
helpers reject names outside the effective allowlist, so arbitrary workdir files
cannot enter the model-visible lane. A blocked present channel is observable:
the kernel emits one deduplicated `notification_hook` system warn-and-flag event
(`ref_id: blocked_channel:<channel>`) per workdir and channel until registration.
The scan skips kernel-private dotfiles (for example `.nudge_state.json`),
non-`.json` entries, and syntactically invalid stems because they cannot become
channels.

`nudge` is the formal channel for throttled checks: runtime update checks publish
`data.nudges[]` entries with `kind: kernel_version`, and source-freshness checks
may publish `kind: source_drift`; the latter stays local and never enters
release-migration routing. Runtime update, configuration, and refresh policy is
owned by `../../../system-manual/reference/runtime-update-checks/SKILL.md`.

## Envelope and producer instructions

Producer helpers write the current channel surface as a standard envelope:

```json
{
  "header": "1 system notification",
  "icon": "🔔",
  "priority": "normal",
  "published_at": "2026-06-10T00:00:00Z",
  "instructions": "Optional agent-facing handling guidance.",
  "data": {"events": []}
}
```

`instructions` is an optional field inside one channel payload, not a channel
name. It tells the agent how that producer expects the event to be handled or
cleared. Producers own that directive because only they know whether the file is
a disposable output, a mirror over canonical state, a coalesced event summary,
or protected source of truth.

External producers that can write the workdir may publish the same envelope to an
allowlisted `mcp.<server>.json` path. They must use atomic sibling-temp
replacement so readers never observe a partial JSON file.

## Voluntary check and model-visible delivery

`check` returns a dict placeholder that the turn-loop post-hook stamps with the
canonical live payload; the handler assembles no second channel representation
and writes no notification state.

When notifications arrive while an agent is IDLE or ASLEEP, the kernel can
synthesize the same `notification(action='check')` tool-call/result shape—same
`action`/`input`/`reasoning` envelope, indistinguishable from a read you issued
yourself—and wake the agent. During ACTIVE work the post-hook moves the single
live payload onto a suitable dict-shaped tool result only on first appearance,
material change, or a deliberate check. Delivery fingerprints and the live
holder belong to kernel synchronization, not to the `manual` action.

## Consumer delay filtering and expiry recovery

The `daemon` target is masked, not filtered: its payload, byte version, and
bounded summary stay visible while its attention entry collapses to a constant
token, so daemon arrivals stay readable but do not wake until delay expires.
Independent channels continue to wake.

`notification(action='delay', input={'channel': ..., 'seconds': 0 or a live configured cap})`
is not a producer operation. Its private `.notification/.delay_state.json`
record causes the coherent consumer snapshot, delivery fingerprint, synthetic
wake, and voluntary `check` projection to omit exactly one allowed target while
it is live; the target file continues receiving and retaining producer updates.
A nonzero delay replaces the prior one, and `seconds: 0` cancels only the
matching target and makes it visible again. The target file is never rewritten.

The finite nonzero cap is read live from
`LINGTAI_NOTIFICATION_DELAY_MAX_SECONDS`; a missing, blank, non-numeric,
non-positive, or non-finite value falls back to `600` with the existing bounded
diagnostic. The process timer is only a prompt path. Every coherent sync also
recovers a persisted overdue delay: recovery stops filtering and publishes one
high-priority `delay-alarm` mirror in the same read/wake cycle. Its state uses
the established native notification mutation lock plus atomic sibling
replacement, and a stable request id makes a stale callback or restart retry
overwrite the same latest-only alarm rather than append a duplicate.

The alarm contains only byte-level change comparison plus producer-reported or
retained-event measurements; these values are not claimed to be exact totals for
overwrite or capped payloads. `delay-alarm` is a built-in mirror consumers may
dismiss, but it cannot itself be delayed. Malformed or unreadable delay state
fails open to visible target delivery. A `daemon` delay masks only its attention
token; payload, delivered version, and bounded summary remain readable.

## Canonical producer state versus mirror

A generic channel clear changes only the `.notification/<channel>.json` surface.
It does not mark an email read, change a goal, consume an MCP source queue, or
mutate any other producer-owned state. A producer whose notification is a mirror
over canonical state must register a generic-dismiss guard and teach the
producer-specific verb in `instructions`.

This separation is deliberate: the filesystem protocol gives the kernel one
current high-attention surface, while canonical state remains under the
producer's own schema and lifecycle.

## Hook registration workflow

An external hook follows this sequence:

1. Write a watcher that publishes the standard envelope atomically to an
   allowlisted `.notification/<channel>.json` path.
2. Call `notification(action='add', input={...})` with `name`, `channel`,
   `source`, `description`, `how_to_modify`, and `how_to_cancel`; optional
   `version` defaults to `1.0.0`, and `instructions` teaches handling.
3. Read the resulting channel with `check`, then use the producer instruction or
   narrowest dismiss action. `drop` revokes registration but does not kill the
   process.

`list` returns manifests in registry order. `edit` changes named fields and
revalidates channel uniqueness; a null-only edit is a no-op. Corrupt or
unreadable `hooks.json` makes `list`, `add`, `drop`, and `edit` return a bounded
load-failed result. Built-in channels and Store-reserved stems (`hooks` and
`large_result_acks`) cannot be registered.

A blocked present JSON channel emits one deduplicated
`notification_hook` event with `ref_id: blocked_channel:<channel>` per workdir and
channel until registration. Kernel-private dotfiles, non-JSON entries, and
invalid stems are skipped by this scan.

## Block-cap and settings detail

The persistent block (`notification_persistent`) and transient attention lane
share `LINGTAI_NOTIFICATION_MAX_CHARS` (default `10000`, clamped to `2048..10000`).
Resolution is live environment, then valid closed System-v2
`notification_max_chars`, then default. Invalid or malformed values contribute
no layer value. At or under the cap, serialization is byte-identical. Above it,
the full persistent payload is atomically spilled to
`logs/notification-overflow-<ts>.json`; the attention lane uses a
content-addressed `notification-attention-overflow-<digest8>.json` (with a
collision suffix), and both model copies compact while preserving routing ids.

Heavy free text is reduced in stages; if an id-only copy cannot fit, a marker-only
envelope retains the exact spill basename, and a failed spill points back to the
producer tool. The cap is a context-size control, not delivery accounting.

The SHOW row `notification.max_chars` reports that same effective clamped value.
To change it, obtain owner approval and use the existing environment launcher or
closed System-v2 procedure; an environment/launcher change needs the authorized
refresh or relaunch, while the file layer is hot-read. SHOW itself never writes,
refreshes, adds an `init.json` field, or creates a Notification settings file.

## Footprint

The protocol footprint is `.notification/<channel>.json` plus kernel-owned
notification metadata such as legacy acknowledgement state
(`.notification/large_result_acks.json`), the hook-manifest registry
(`.notification/hooks.json`, a single non-channel file invisible to collection),
and consumer-delay state (`.notification/.delay_state.json`, a private dotfile
invisible to collection). Inspect it read-only before diagnosing a producer.
Never delete the directory or bulk-remove files—doing so bypasses the guards and
stale checks that only the producer verb or an atomic notification action honor.
