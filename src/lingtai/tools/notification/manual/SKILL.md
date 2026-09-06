---
name: notification-manual
description: >
  Router for the notification filesystem protocol and the standalone
  `notification` tool: read when interpreting `.notification/<channel>.json`,
  or choosing producer-specific handling vs safe mirror dismissal.
  Large-result/context compaction belongs to `context-manual`, not here.
version: 0.13.0
tags: [lingtai, notifications, channels, dismiss, delay, alarm, settings, manual, force, stale, nudge, hooks, whitelist]
last_changed_at: "2026-08-29T00:00:00Z"
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

LingTai notifications are a filesystem protocol: producers publish allowlisted
`.notification/<channel>.json` surfaces, and the kernel exposes their current
model-visible state. The always-available `notification` tool is the sole
agent-callable home for reading and clearing those surfaces. `system` has no
notification or dismiss alias, and context hygiene is not a notification
operation either — that is `context(action='summarize')`.

## Quick start

The resident tool schema is the source of truth for the eleven actions, their
per-action `input` fields, and the `action` + `input` + `reasoning` envelope
(arguments live inside `input`, never at the root). What it does not say:

- `manual` returns **this router body** — it is documentation retrieval, not a
  notification-state read.
- `settings` accepts exactly `input={}` and only shows current configuration;
  it has no set, reset, or other mutation form.
- Optional fields are declared required-but-nullable, and `null` is treated
  exactly like omission. The one trap: `reason: null` does **not** satisfy the
  post-molt acknowledgement requirement.
- After handling a notification, use the narrowest correct dismiss action and end
  the turn; do not voluntarily call `check` again merely to confirm the clear.

## Consumer delay and expiry alarm

`notification(action='delay', input={'channel': '<allowed>', 'seconds': 1..LINGTAI_NOTIFICATION_DELAY_MAX_SECONDS},
reasoning='...')` hides **only consumer delivery** for one allowed target while
the timer is live. The nonzero cap is read live from
`LINGTAI_NOTIFICATION_DELAY_MAX_SECONDS` (default `600` seconds), so a current
environment setting applies to each action without restart; blank, invalid, zero,
or negative values log a fallback to `600`. It does not clear, rewrite, or pause
the producer; target messages keep accumulating in their original channel file. Every other channel
continues delivering normally. A nonzero call explicitly replaces the one prior
live delay. Use `seconds: 0` with that same channel to cancel early and
re-expose it.

At expiry (including after a refresh/restart recovery) the target becomes visible
in the same consumer sync that adds one high-priority `delay-alarm` mirror. The
alarm records target, requested/actual duration, changed/no-change, and only
conservative current measurements: producer-reported counts and retained event
entries are never asserted to be an exact total for overwritten/capped mirrors.
Handle the re-exposed target, then dismiss `delay-alarm` as a mirror when done.
Delaying `daemon` is the one exception to hiding: the daemon channel stays
readable (check, snapshot, and the bounded daemon summary keep working) and only
stops waking you until the delay ends. `delay-alarm` itself cannot be delayed. A damaged private delay record fails open
(target visible) rather than silently suppressing notification delivery.

The five-field SHOW row is `notification.delay_max_seconds`. Its meaning is the
finite nonzero delay ceiling described above; `current` is the live effective
integer and `default` is `600`. Resolution is
`LINGTAI_NOTIFICATION_DELAY_MAX_SECONDS` followed by the fixed default.
Accepted configured values are positive integer strings. Missing uses `600`;
blank, non-numeric, zero, and negative input fall back to `600`; the delay
action logs its existing bounded diagnostic, while SHOW performs no log write.
The value is non-sensitive and is read at every delay action, so an environment
value already present in the process applies to the next call. The row is
`configurable: true` because the existing launcher or configured `env_file`
procedure can change that source outside SHOW.

To change it, obtain the configuration owner's approval, then edit the exact
variable in the agent's existing `env_file` or launcher/supervisor environment.
Do not add an `init.json` field or create a Notification settings file. An
`env_file` edit or launcher change needs the normal authorized refresh/relaunch
to enter the process. Call `notification(action='settings', input={},
reasoning='verify delay ceiling')` again and confirm
`notification.delay_max_seconds.current`; SHOW never writes the environment or
performs the refresh.

## Notification settings

`notification(action='settings', input={}, reasoning='inventory')` is a
read-only progressive-disclosure action. Normal success contains exactly these
rows and exactly the five projected fields `key`, `current`, `default`,
`configurable`, and `comment`:

- `notification.max_chars` →
  `notification-manual#block-size-cap-persistent-and-attention-lanes`
- `notification.delay_max_seconds` →
  `notification-manual#consumer-delay-and-expiry-alarm`

The `comment` pointer is where meaning, accepted values, precedence, canonical
source names, apply timing, sensitivity, authorization notes, and the real
change procedure live. Neither row is sensitive; channel payloads, events,
accounts, file paths, delay state, hook manifests, and session state are not
settings rows. If either effective value cannot be resolved or serialized, the
whole call fails with the fixed `SETTINGS_UNAVAILABLE` result; there are no
partial rows or per-row unavailable placeholders.

## Root `summarize`

Notification is a **short-result** family, so leave the root `summarize` boolean
false — especially for `manual`, where summarizing would drop the exact
procedures and constraints you called it for.

## Installed manual retrieval

`notification(action='manual', input={})` reads only:

```text
<agent>/.library/intrinsic/capabilities/notification/SKILL.md
```

Success returns exactly `status`, `notification_manual`, and `manual_path`. A
missing installed file returns `status: degraded`, an empty
`notification_manual`, the same fixed `manual_path`, and an actionable `error`
naming an initializer or capability-install problem. It never falls back to a
source checkout, and it touches neither notification nor producer state.

## Hooks & whitelist

External hooks deliver notifications through channels that are **not** on the
static allowlist (which covers kernel intrinsics and `mcp.` bridge servers).
Registering a hook is the whitelist gate: only registered hook channels pass
through; everything else is ignored (and, when the kernel observes a blocked
attempt, surfaced as a warn-and-flag system event so the agent can investigate).

Hook channels are **per-agent**: registering a hook allowlists its channel for
this agent's working directory only — a hook channel is not visible to other
agents' workdirs. The registry (`.notification/hooks.json`) is re-read whenever
its `(mtime, size)` stat changes, so an out-of-band write by another process (a
sibling CLI, the Telegram server, or the hook installer itself) is picked up on
the next sync without a restart.

### Setup flow

1. **Write the hook script** that polls a source (a file, a service, a remote
   node) and, on an event, publishes `.notification/<channel>.json` with the
   standard envelope (`header`, `icon`, `priority`, `published_at`, `data`,
   optional `instructions`).
2. **Register its manifest** with the notification tool:
   `notification(action='add', input={...})`. `add` validates the manifest,
   appends it to the disk registry (`.notification/hooks.json`), and
   **allowlists the manifest's `channel`** — from then on the channel passes
   the kernel's allow predicate.
3. **Publish** `.notification/<channel>.json` from the hook process. The
   notification now appears in `check` / the meta-block payload like any other
   channel.
4. **Read and dismiss** per the producer's `instructions` / the manifest's
   `description`, using the narrowest correct dismiss action. Dismissing the
   mirror does not touch the hook process; `drop` only revokes the
   registration.

### Manifest fields

- `name` — unique hook identifier (required).
- `channel` — the `.notification/<channel>.json` stem this hook owns
  (required; must be unique across hooks). It must not be a built-in static
  channel (`system`/`email`/`soul`/`goal`/`molt`/`nudge`/`post-molt`/`bash`/`btw`/`cron`/`daemon`/`delay-alarm`/`tool_loop_guard`)
  nor a Store-reserved non-channel stem (`hooks`/`large_result_acks`); `add`
  refuses those with a clear error.
- `source` — what the hook polls (required, e.g. `G:`).
- `description` — one-line purpose (required).
- `how_to_modify` / `how_to_cancel` — how the agent updates or stops the hook
  (required; cancellation is the owner's job — `drop` never kills a process).
- `version` — manifest version (optional, defaults to `1.0.0`).
- `instructions` — agent-facing handling guidance (optional).

### drop / edit / list semantics

- `list` — read-only; returns the registered manifests in registry order, or
  `hook_registry_load_failed` when the registry is corrupt or unreadable.
- `edit` — update a manifest's fields by `name`; changing `channel` moves the
  allowlist entry (and is refused with `channel_in_use` if another hook owns
  that channel). Moving `channel` onto a built-in static channel or a
  Store-reserved stem (`hooks`/`large_result_acks`) is refused with
  `invalid_manifest`. An `edit` providing no non-null fields is a `no_change`
  no-op.
- `drop` — remove the manifest **and revoke its channel** from the allowlist;
  unknown names return `not_found`. `drop` is registration evidence only —
  stopping the hook process follows the manifest's `how_to_cancel`.

### Warn-and-flag

When a channel that is neither statically allowlisted nor registered attempts
notification, the kernel emits one `notification_hook` system event
(`ref_id: blocked_channel:<channel>`) per workdir+channel — deduped until the
channel registers (then a later re-block can warn again). The scan only flags
stems that can become channels: kernel-private dotfiles (`.nudge_state.json`),
non-`.json` entries, and syntactically invalid stems are skipped. If you see
such an event, run `list` to inspect hooks and
`add` to register the hook if the producer is legitimate.

### Worked example: `comm_watcher`

```text
1. A watcher script polls a G: node (source) for changes.
2. On a change it writes .notification/comm_watcher.json with the standard
   envelope and instructions (e.g. "read the relayed message, then dismiss").
3. The agent (or operator) registers it once:
   notification(action='add', input={
     'name': 'comm_watcher', 'channel': 'comm_watcher', 'source': 'G:',
     'description': 'poll G: node and relay',
     'how_to_modify': 'notification(action=edit, ...)',
     'how_to_cancel': 'stop the watcher process',
     'instructions': 'read the relayed message and dismiss the channel'})
4. The channel is now allowlisted: notifications pass through to check, and
   the agent reads/dismisses per the manifest's instructions.
5. To decommission: notification(action='drop', input={'name': 'comm_watcher'})
   revokes the channel, then stop the watcher process per how_to_cancel.
```

## Block size cap (persistent and attention lanes)

Two model-visible notification envelopes are re-serialized into provider
context: the persistent block
(`_meta.agent_meta.notifications.persistent`, the `notification_persistent`
block, rebuilt per payload build) and the attention lane
(`_meta.agent_meta.notifications.attention`, the transient per-channel routing
payload re-stamped on every eligible tool batch and every IDLE/ASLEEP pair).
A busy hub agent with many unread emails plus several IM lanes could otherwise
grow context fast and pay a large per-call cache miss. The kernel caps BOTH
lanes with ONE shared bar (`LINGTAI_NOTIFICATION_MAX_CHARS`):

- **At or under the cap** (default `10000` characters): the block is delivered
  byte-identical, no spill file, no marker.
- **Over the cap - persistent lane**: the FULL block is written atomically to
  `<workdir>/logs/notification-overflow-<ts>.json`, and the model-visible copy
  is compacted (heavy free-text fields 200 → 100 → 50 → 0, then id-only
  message stubs) until it fits; a terminal marker-only envelope with the exact
  spill basename is returned BY CONSTRUCTION when even id-only stubs exceed
  the cap. The compacted block carries an `overflow` marker
  `{path, full_chars, truncated}` (and `path_omitted` + `spill_file` when the
  absolute path is stripped). Message ids are never dropped, so delivery
  tracking still sees every message and never re-delivers a truncated one.
- **Over the cap - attention lane**: the FULL lane is written once to
  `<workdir>/logs/notification-attention-overflow-<digest8>.json` — the name
  is content-addressed (short sha256 of the lane's canonical serialization),
  so an unchanged oversized lane reuses the SAME file instead of re-spilling
  every batch, and exclusive creation never overwrites an existing recovery
  handle (a different-content collision allocates `<digest8>-<N>.json`; the
  exact allocated basename, including any `-N` suffix, is returned as
  `overflow.spill_file`). The model-visible copy is compacted (heavy fields
  truncated; routing ids including `message_ids` preserved) and carries the
  same `overflow` marker. If even the routing stub cannot fit, the lane
  degrades deterministically to a marker-only envelope that is capped BY
  CONSTRUCTION: a pathologically long absolute spill path is stripped from the
  marker (`path = None`, `path_omitted`, exact `spill_file` basename retained)
  so the envelope always satisfies the cap; the full payload remains on disk
  under the deterministic content-addressed name. If the spill file itself
  cannot be written, the marker carries `spill_failed` and the block points
  the agent at the producer tool for the full content.

The five-field SHOW row is `notification.max_chars`. `current` is the same
effective clamped value the live Agent consumes and `default` is `10000`.
Resolution is a valid live `LINGTAI_NOTIFICATION_MAX_CHARS` value, then the
existing `notification_max_chars` field in closed-v2
`<agent>/settings/system.json` through `Agent.resolve_notification_max_chars()`,
then `10000`. Positive values above `10000` clamp back to `10000`; values below
`2048` clamp up to `2048` on both lanes. A missing, blank, non-numeric, zero, or
negative environment value falls through to the valid System-v2 field, then
the default. A malformed, unknown-field, wrong-version, or otherwise invalid
System document is rejected whole and contributes no file-layer value. The
value is non-sensitive, both sources are consulted at every payload build, and
the row is `configurable: true` through these existing owner procedures.

To change the environment source, obtain the configuration owner's approval
and edit `LINGTAI_NOTIFICATION_MAX_CHARS` in the existing `env_file` or
launcher/supervisor environment; refresh/relaunch is required for an `env_file`
or launcher change to enter the process. To change the file layer instead, use
the existing authorized File/Shell procedure to edit
`<agent>/settings/system.json` as a closed `schema_version: 2` document and set
only its `notification_max_chars` field while preserving any other valid v2
fields. Do not add a Notification JSON document or an `init.json` field, and do
not widen the closed System grammar. The file layer is hot-read. Call
`notification(action='settings', input={}, reasoning='verify notification
cap')` again and confirm `notification.max_chars.current`; SHOW itself never
writes configuration, refreshes, or launches anything. This is a context-size
steering knob only: it never grants access and never changes which messages are
considered delivered.

## Nested reference catalog

```yaml
- name: notification-manual-channel-model
  location: reference/channel-model/SKILL.md
  description: |
    Nested notification-manual reference for the filesystem channel protocol,
    allowlist, envelopes and instructions, nudge routing, kernel sync, voluntary
    check behavior, and producer canonical-state versus mirror boundaries. Read
    this when interpreting or producing notification payloads.
- name: notification-manual-dismissal-safety
  location: reference/dismissal-safety/SKILL.md
  description: |
    Nested notification-manual reference for atomic dismissal, producer-specific
    verbs, stale-version and force rules, protected channels, post-molt
    acknowledgement, and legacy large_tool_result reminder escape hatches. Read
    this before clearing notification state or diagnosing a refusal.
```

## Routing table

| Need / keywords | Read |
|---|---|
| Channel names; `.notification/*.json`; allowlist; `mcp.` channels; envelope fields; `instructions`; nudge/update checks; `_meta.agent_meta.notifications.attention`; voluntary `check`; producer state versus mirror | `reference/channel-model/SKILL.md` |
| `notification_persistent` and `notification.attention` block size cap; `LINGTAI_NOTIFICATION_MAX_CHARS` (floor `2048` / ceiling `10000`); `notification-overflow-<ts>.json` and `notification-attention-overflow-<digest8>.json` spill files; compacted copy; message-id preservation; marker-only degradation | this section (`Block size cap (persistent and attention lanes)`) |
| External-hook registration; `.notification/hooks.json`; `add`/`drop`/`edit`/`list`; whitelist gate; warn-and-flag on blocked channels | this section (`Hooks & whitelist`) + `reference/channel-model/SKILL.md` (effective allowlist) |
| Temporarily hide one channel; `delay`; 0 or live configured seconds (default cap 600); replacement/cancellation; expiry, restart recovery, or `delay-alarm` | this section (`Consumer delay and expiry alarm`) + `reference/channel-model/SKILL.md` |
| Show Notification settings; exact five fields; `notification.max_chars`; `notification.delay_max_seconds`; authorized change and verification procedures | this section (`Notification settings`) and each row's exact `comment` target |
| Which dismiss action; producer-specific handling; guarded/stale mirror; `force`; protected `goal`; post-molt reason; legacy `large_tool_result` event | `reference/dismissal-safety/SKILL.md` |
| Tool-result ranking, digest quality, `context(action='summarize')`, recovery by `tool_call_id`, summarize versus molt | `../context-manual/reference/summarize-manual/SKILL.md` |
| Active goal source-of-truth and cancellation/completion | `../system-manual/reference/goal-manual/SKILL.md` |
| Runtime/kernel update nudges | `../system-manual/reference/runtime-update-checks/SKILL.md` |

## Safety boundaries to keep resident

The producer-verb preference and `force` semantics are resident (meta_guidance
`notification_handling` and the schema's `_FORCE_DESCRIPTION`). The two facts
neither of them states:

- Neither `check`, `settings`, nor `manual` writes notification state or runtime
  configuration.
- `force=true` does **not** override protected source-of-truth channels.

Producer guards exist so that clearing a mirror is never mistaken for handling
the producer's canonical state.
