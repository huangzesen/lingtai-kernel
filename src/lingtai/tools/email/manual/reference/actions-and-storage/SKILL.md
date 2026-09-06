---
name: email-manual-actions-and-storage
description: >
  Focused Email reference for action inputs, folders, check filters, delivery
  side effects, self-send/time capsules, contacts, and mailbox persistence.
  Read after email-manual when an operation needs more than the first-call map.
version: 1.0.0
tags: [lingtai, email, actions, mailbox, storage]
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/email/manual/SKILL.md
- src/lingtai/tools/email/_family_schema.py
- src/lingtai/tools/email/manager.py
- src/lingtai/tools/email/primitives.py
- src/lingtai/tools/email/settings.py
- src/lingtai/tools/email/CONTRACT.md
maintenance: |
  Tracks Email action behavior, input details, mailbox layout, and persistence guidance; update when action contracts or storage ownership changes.
---

# Email actions and storage

The root manual is the first-call router. This page supplies action-level detail;
input keys remain closed to the action that owns them. Omitted optional values
use the action defaults. An explicit `null` is treated as omitted at the family
boundary.

## Sending

```python
email(action="send", input={
    "address": "peer", "subject": "status", "message": "ready",
    "cc": ["human"], "bcc": [], "attachments": [], "delay": 0,
}, reasoning="report status")
```

`address` is a bare peer name (or an authorized absolute path in `abs` mode).
`cc` is visible to recipients; `bcc` is stored in the sender's copy but hidden
from recipients. `attachments` contains file paths. `message` is capped at
50,000 Unicode characters at send time; oversize bodies are rejected with the
limit and actual size rather than truncated.

A send writes sender-side outbox/sent state synchronously, then starts one daemon
mailman thread per recipient. A successful `{status: "sent"}` receipt means the
attempt was scheduled, not necessarily delivered. `delay` is a non-negative
number of seconds before the one delivery attempt; the outbox record exists while
the thread waits. See [Notifications and delivery](../notifications-and-delivery/SKILL.md)
for liveness and bounce behavior. Identical consecutive sends are guarded as a
loop; the installed pass count is in [Settings reference](../settings-reference/SKILL.md).

## Listing and filtering

`check` lists newest-first by default from `inbox`; `folder` can be `inbox`,
`sent`, or `archive`, and `n` defaults to 10. Its optional `filter` is owned by
`check` alone:

```python
email(action="check", input={
    "folder": "inbox", "n": 20,
    "filter": {
        "sort": "newest", "from": "peer", "subject": "status",
        "contains": "blocker", "after": "2026-04-01T00:00:00Z",
        "before": "2026-05-01T00:00:00Z", "unread_only": True,
        "has_attachments": False, "truncate": 500,
    },
}, reasoning="find unread status mail")
```

`sort` is `newest` (default) or `oldest`; `from`, `subject`, and `contains`
are case-insensitive substring filters; `after`/`before` accept ISO 8601
timestamps; the two boolean filters select unread or attachment-bearing mail.
`truncate` controls preview characters (default 500); `0` requests the full
body. A check result can be trimmed by Email's token budget and reports that
fact. Prefer filtering in the call rather than retrieving a large mailbox and
post-filtering mentally.

`search` takes a regular-expression `query` and optional `folder`; it searches
sender, subject, and body and rejects invalid regexes. It does not accept
`filter` or `n`.

## Reading, mutating, and contacts

- `read` takes a list of mailbox IDs, returns source records (including
  attachments), and marks inbox IDs read.
- `dismiss` takes a list of IDs and marks handled inbox mail read without
  returning bodies. Prefer it after handling a body already in persistent
  notification context.
- `reply` takes one ID and a message, preserves thread linkage, and addresses
  the sender. `reply_all` keeps the original recipients except self. Both obey
  the root manual's same-channel rule.
- `archive` moves inbox IDs to `mailbox/archive`; it removes them from the read
  set as part of the move. `delete` permanently removes IDs from inbox or
  archive and refuses `sent`.
- `contacts` lists the private address book. `add_contact` upserts by address;
  `remove_contact` deletes by address; `edit_contact` changes supplied name or
  note fields. Contact writes use a temporary file and atomic replacement.

All IDs must come from the current agent's own notification or mailbox result.
Stale IDs produce a not-found hint; an ID from another working directory has no
meaning here.

## Self-send and time capsules

Sending to your own bare address creates an ordinary unread inbox message that
survives molt, can be searched later, and remains in the unread lane until
`dismiss`, `read`, `archive`, or `delete`. Add `delay` to make a one-shot time
capsule: the outbox is written immediately, and the daemon mailman thread waits
until the deadline. A delayed self-send is a future nudge, not delayed tool
execution. Recurring sends are not supported; use a host scheduler through
`shell-manual` for recurring work.

## Mailbox layout

Paths are relative to the agent working directory:

```text
mailbox/inbox/<id>/message.json       received mail
mailbox/sent/<id>/message.json        sent copy (one record per send call)
mailbox/archive/<id>/message.json     archived inbox mail
mailbox/outbox/<id>/message.json      pending/delayed send
mailbox/read.json                     read-ID set
mailbox/contacts.json                 private contact book
.notification/email.json              producer-owned unread mirror
```

Message JSON uses UTF-8. Mailbox message files are direct writes; contact writes
are the atomic exception. BCC data is not exposed to recipients. The unread
mirror is refreshed by every read-state mutation; its payload and delivery
semantics are in the notifications reference.
