---
name: notification-store
contract_version: 5
root_contract: CONTRACT.md
related_files:
  - src/lingtai/kernel/notification_store/ANATOMY.md
  - src/lingtai/kernel/base_agent/CONTRACT.md
  - src/lingtai/kernel/refresh_watcher/CONTRACT.md
  - src/lingtai/kernel/notification_store/__init__.py
  - src/lingtai/kernel/notification_store/_mutation_lock.py
  - src/lingtai/adapters/notification_store_lock.py
  - src/lingtai/adapters/posix/notification_store.py
  - src/lingtai/adapters/posix/notification_store_lock.py
  - src/lingtai/adapters/windows/notification_store_lock.py
  - src/lingtai/kernel/notifications.py
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/agent.py
  - src/lingtai/cli.py
  - src/lingtai/mcp_servers/telegram/manager.py
  - src/lingtai/mcp_servers/telegram/server.py
  - src/lingtai/tools/daemon/supervisor_runtime.py
  - src/lingtai/kernel/refresh_watcher/watcher_program.py
  - tests/_notification_store_helpers.py
  - tests/test_notification_delay_alarm.py
  - tests/test_notification_store.py
maintenance: |
  <!-- CANONICAL-MAINTENANCE v2 BEGIN -->
  This component contract is governed by the root CONTRACT.md. Keep
  related_files complete and repo-relative: the paired ANATOMY.md, Port, every
  production Adapter, contract tests, and directly relevant component contracts
  belong here. Re-read this contract whenever a linked boundary changes. Update
  the Port, affected Adapters, contract tests, and this contract in the same
  change; update the paired Anatomy when structure or composition also changes;
  bump contract_version for a breaking Port-contract change. If code and contract
  disagree, treat the disagreement as a defect—do not silently rewrite the
  normative contract to match the implementation.
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
  <!-- CANONICAL-MAINTENANCE END -->
---
# Notification Store Contract

## Purpose
Guarded by: [NS001](BEHAVIORS.md#behavior-ns001)


The Notification Store persists and observes current notification-channel
mirrors without owning notification policy. Core owns channel validation,
dismiss authority, stale decisions, envelopes, acknowledgement union/purge,
wake ordering, live-holder behavior, and model-visible lanes.

## Behavior

Runtime and coding agents MUST use the injected Port rather than construct
storage paths in Core. They MUST preserve the established external
`.notification/<channel>.json` protocol for every non-daemon channel. The daemon
channel is the sole exception: producers persist independent mini-channels at
`.notification/daemon/<daemon-id>.json`, while Store snapshot/fingerprint expose
one aggregate `daemon` channel. The sibling `.notification/daemon.json` is a
report containing only mini-file-derived run/state statistics; it is not a
channel event source. Non-force dismiss conflicts remain stale refusals, and
event/ref daemon dismissal removes only matching event keys from a mini-file,
deleting that mini-file only when no sibling events remain. A daemon channel
dismissal must compare the delivered aggregate version before unlinking any
mini-file, so a mini-file arriving after delivery is preserved. Existing root
event facts may be retained only under report migration metadata and MUST NOT
be delivered. Daemon append routing uses the existing typed channel mutator
payload and does not add a Store operation. The optional `owner` argument on
`compare_update_channel` is daemon-only: it routes one unconditional append to
one mini-file and is not a general resource key. The
`.notification/daemon/.tombstone` control record linearizes aggregate
clear/dismiss/CAS and records process-crash-safe append pending state; committed
clear/dismiss visibility cuts are fsync-durable. It is Store state, never an
agent-visible channel. A malformed or unreadable control record MUST remain fail-closed on daemon
writes/mutations. Snapshot and fingerprint instead expose one bounded,
content-free high-priority daemon control-error payload directing the agent to
`lingtai-doctor`, while all unrelated channels remain deliverable.
They MUST NOT add a nullable/no-op Store, Path-or-Port overload, locator, hidden
Core construction, ninth operation family, generic event database, or
caller-held transaction lock.

## Port

`NotificationStorePort` has exactly eight operation families:

1. `snapshot(allow_channel)`;
2. `fingerprint(allow_channel)`;
3. `publish(channel, payload)`;
4. `clear(channel) -> bool`;
5. `compare_update_channel(channel, expected_version, pure_core_mutator)`;
6. read-only `load_ack_refs() -> set[str]`;
7. `update_ack_refs(pure_core_set_mutator) -> UpdateAckRefsResult`;
8. read-only `load_hook_manifests() -> list[dict]`,
   `update_hook_manifests(pure_core_manifest_mutator) -> UpdateHookManifestsResult`,
   and read-only `stat_hook_registry() -> tuple[int, int] | None`.

The Port also exposes the read-only `mutation_lock` Port composed with that
Store. This is not a ninth persistence family: notification Core uses it only
for the private delay-state + delay-alarm transaction that sits beside Store
state, and MUST NOT wrap any of the eight Store families in a caller-held lock.
The Core-owned coordinator acquires deterministic resource scopes plus the
process-wide canonical-path guard; native lock selection remains in the outer
adapter.

`UNCONDITIONAL` is distinct from `None`: `None` means expected absence. A
fingerprint tuple means the exact delivered version. Channel mutators return
`(payload_or_none, changed, value)`; acknowledgement mutators return
`(set, changed, value)`; hook-manifest mutators return
`(list[dict], changed, value)`. `CompareUpdateResult` exposes `applied`,
`conflict`, `changed`, `cleared`, `value`, `current_version`, and
`previous_version`.

## Adapters

`PosixNotificationStoreAdapter` is the production filesystem adapter. The
Store-private lock vocabulary maps canonical channel, acknowledgement, hook,
delay, daemon-run, and daemon-control resources to bounded sanitized-plus-hash
filenames under `.notification/.locks/`. The Core-owned lock coordinator's
process-wide RLock keyed by that canonical path closes `flock`'s
same-process/open-description gap; native locks then serialize only that
resource across independently composed processes.
The POSIX selector takes an exclusive scoped `flock` plus a **shared** legacy
`.store.lock` bridge for one compatibility release. Windows uses scoped
byte-range locks but cannot express that shared bridge: old global writers and
new scoped writers require a documented quiesced cutover, not false parity.
Lock files are never deleted as part of normal Store operation. Agent, CLI,
daemon supervisor, and Telegram server composition roots construct the Store
adapter. External LICC/direct `mcp.*` producers keep the same filesystem path and
envelope. The POSIX adapter stores daemon events in per-id mini-files under
`.notification/daemon/`; ordinary owner append does not scan the aggregate or
rebuild a report. Snapshot/fingerprint derive an aggregate only on read; the
sibling `.notification/daemon.json` is a non-authoritative compatibility report,
excluded from snapshot, fingerprint, dismissal, and the heartbeat hot path.

## Contract rules

- Snapshot and fingerprint are read-only: they take no native mutation lock,
  write no daemon report, and skip missing, malformed, or unreadable ordinary
  entries while applying the live Core allow-predicate. Fingerprints are sorted
  SHA-256 entries of filename, byte size, and bytes, not mtime. For daemon, the
  single aggregate fingerprint is derived from every logical nested mini-file's
  name and bytes, so a mini-file addition, removal, or same-file append changes
  the aggregate version; `.notification/daemon.json` never changes it. An
  unreadable/corrupt daemon control/tombstone is exceptional Store authority:
  daemon mutation paths raise `DaemonControlError` with no payload-body echo,
  while snapshot/fingerprint substitute the same bounded content-free,
  high-priority daemon control-error projection and continue unrelated channels.
  It is never treated as an empty aggregate. New per-run appends carry their daemon id in
  the pure payload marker consumed by the adapter; the marker is not included in
  the aggregate projection.
- Publish is atomic sibling-temp replacement. Publish and clear hold the Store's
  in-process and cross-process mutation locks. Clear returns `False` only for
  absence; other clear and write errors propagate unless a Core best-effort
  wrapper explicitly preserves legacy suppression.
- Compare-update reads payload and version under the same complete-transaction
  **resource** serialization. Ordinary channel/ack/hook/delay resources do not
  block unrelated resources. The daemon owner hot path holds only its run scope
  plus daemon-control scope; it appends one mini-file with a durable pending
  receipt, then commits batch state without scanning the aggregate or rebuilding
  the report. Aggregate clear/dismiss/CAS is linearized under daemon-control;
  it commits a durable tombstone visibility cut before best-effort physical
  compaction, so a crash after the cut cannot resurrect cleared events. Only
  `FileNotFoundError` is absence; every other ordinary read error propagates.
  Readable malformed/non-dict JSON retains its version and presents `{}` to
  Core, so it cannot satisfy expected absence.
- A compare conflict does not call the mutator and carries no policy value. A
  matched guard runs the mutator once. `changed=False` performs no write;
  `payload=None` clears; a dict publishes atomically. Operational result fields
  report the resulting version and actual clear outcome, while `value` carries
  all Core response/log policy evidence.
- Ack load preserves legacy best effort: absent, malformed, or unreadable state
  yields an empty set. Atomic ack update holds the same in-process and
  cross-process Store locks across that read, one pure Core set mutation, and
  store-or-clear. `changed=False` performs
  no write. Non-empty write failures propagate. Empty-set clear preserves legacy
  best effort by swallowing every unlink `OSError`; typed `changed/value`
  evidence still returns, with `changed=False` when no unlink succeeds.
- Hook-manifest load distinguishes an absent registry from a corrupt or
  unreadable one: an absent `hooks.json` yields an empty list; an invalid-JSON
  or unreadable registry raises, which the tool layer surfaces as a structured
  `hook_registry_load_failed` error so "registry broken" is never reported as
  "nothing registered". Atomic hook-manifest update
  holds the same in-process and cross-process Store locks across that read, one
  pure Core list mutation, and store-or-clear. `changed=False` performs no
  write. Non-empty write failures propagate. Empty-list clear preserves legacy
  best effort by swallowing every unlink `OSError`; typed `changed/value`
  evidence still returns, with `changed=False` when no unlink succeeds.
- `stat_hook_registry()` returns the cheap `(st_mtime_ns, st_size)` staleness
  fingerprint of the hook-registry file, or `None` when absent. Core uses it to
  re-seed its in-memory hook mirror when another process (sibling CLI, Telegram
  server, hook installer) wrote `hooks.json` out-of-band, without re-reading the
  file on every sync tick.
- `STORE_RESERVED_NON_CHANNEL_STEMS` is `{"hooks", "large_result_acks"}`: the
  Store-owned registry and acknowledgement files are never channels. Core
  validation rejects these stems as hook channels, so a registered hook can
  never publish over or clear Store-owned files. Adapters MUST keep their
  snapshot/fingerprint skip lists in sync with this set.
- Core hook add/edit/drop MUST use family 8, never split family 8's read from a
  later write. The registry file `.notification/hooks.json` is a single
  non-channel registry, invisible to snapshot/fingerprint and to the allow
  predicate except through Core's registered-hook mirror.
- Core acknowledgement union and purge MUST use family 7, never split family 6
  read from a later write. System, nudge, Telegram, and daemon-terminal mutations
  decide from the current payload inside compare-update; daemon event/ref removal
  tombstones only matching event keys, preserves same-run siblings, and never
  rewrites another daemon's file.
  A channel owner may also consult owner-private read-only state inside that
  mutator when the read acquires no additional lock, performs no write, and is
  required to serialize the owner's decision with the channel commit; mutator
  writes and Store re-entry remain forbidden. Force uses `UNCONDITIONAL`, while
  non-force dismiss uses the delivered fingerprint entry including explicit
  absence.
- `.notification/.store.lock` is compatibility coordination metadata, not
  notification state or authority. POSIX scoped writers acquire it shared for one
  release while legacy writers retain their old exclusive whole-store lock; this
  prevents mixed-version lost updates without reintroducing new-to-new global
  serialization. Windows cannot provide shared byte-range parity and therefore
  requires a quiesced legacy-writer cutover. Scoped `.locks/*.lock` files and
  legacy `.store.lock` files are never deleted by live Store code. Native lock
  ownership, not file existence, defines exclusion and release follows process
  death. Snapshot and fingerprint continue to expose only allowed JSON channel
  files.

## Contract tests

Shared conformance covers the eight-family surface and composed mutation-lock
Port, expected absence versus unconditional updates,
malformed/unreadable/error behavior, typed policy values,
atomic same-process and spawned-process channel updates, atomic acknowledgement
union/purge, atomic hook-manifest append/clear, required injection, outer
composition, stale dismiss refusal, unrelated-event survival, daemon mini-channel
isolation/append, aggregate fingerprint changes, targeted mini-file deletion,
late-file stale-CAS preservation, nudge updates, and Telegram current-mirror
clearing. Production evidence additionally uses real native cross-process lock
paths: an unrelated `email` reader/writer proceeds while a `system` or daemon
scope is held; forty same-run publishers preserve idempotency/no loss;
clear/dismiss overlap, crash-after-tombstone-before-compaction, corrupt
control-record failure, no-delay zero-native-lock heartbeat, snapshot no-write,
and POSIX legacy bridge behavior are covered. Windows certification either proves
its scoped native locks or explicitly asserts the quiesced legacy-cutover gate.
Production adapter tests must use only an explicitly authorized persistent
scratch path when deletion is separately authorized.

## Migration and rollback

This release keeps the old POSIX `.store.lock` file as a one-release shared-lock
bridge: an old exclusive writer and a new scoped writer exclude each other, while
two new unrelated resources do not serialize. The scoped `.locks/` files are
coordination metadata and remain harmless across a normal code rollback; no live
code deletes either kind of lock file. Windows upgrades must quiesce all legacy
writers before selecting the scoped adapter.

Daemon aggregate authority adds `.notification/daemon/.tombstone`. New readers
validate it: a corrupt control record becomes a bounded daemon-only error
projection pointing to `lingtai-doctor`, while daemon mutations still refuse.
A rollback after aggregate clear/dismiss
has committed a tombstone visibility cut is **not** an automatic compatibility
operation: quiesce writers, inspect/repair or explicitly migrate that control
state, then select the legacy reader. A legacy reader cannot be allowed to
reinterpret a tombstoned-but-not-yet-compacted mini-file as a resurrected event.

## Maintenance

Read the paired Anatomy for locations and composition. Port, adapter, Core
callers, shared conformance tests, and this contract change together. Breaking
Port or semantic changes bump `contract_version`; implementation drift is a
defect, not permission to weaken this contract.
