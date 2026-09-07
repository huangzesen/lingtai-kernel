---
name: email-manual-notifications-and-delivery
description: >
  Focused Email reference for daemon delivery, heartbeat/liveness, bounce
  recovery, unread notification payloads, persistent bodies, overflow, and
  read-state mirror refresh. Read after email-manual when delivery or notices
  need diagnosis.
version: 1.0.0
tags: [lingtai, email, notifications, delivery, recovery]
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/email/manual/SKILL.md
- src/lingtai/tools/email/manual/reference/actions-and-storage/SKILL.md
- src/lingtai/tools/email/manager.py
- src/lingtai/tools/email/primitives.py
- src/lingtai/adapters/posix/mail.py
- src/lingtai/tools/email/CONTRACT.md
maintenance: |
  Tracks Email delivery, unread notification projection, bounce recovery, and read-state mirror semantics; update when producer or mail-adapter behavior changes.
---

# Email notifications and delivery

## Delivery and liveness

`send` writes the sender's outbox/sent record before creating one daemon
`_mailman` thread per recipient. `delay=0` still permits a sent receipt before
the delivery attempt finishes. A normal agent target must have valid
`.agent.json` metadata and a fresh `.agent.heartbeat` (normally under two
seconds); a human target (`admin: null`) does not need a heartbeat. A refreshing
or relaunching target can therefore bounce as `not running`; Email does not
queue the attempt for later. The sender receives the eventual failure as an
`email.bounce` event in `.notification/system.json`.

A process can appear in `ps` before publishing a fresh heartbeat. If a CPR
(child-process restart) attempt also exits because the duplicate-process guard
finds the existing same-workdir process, these observations are compatible:
do not stack CPR attempts. Wait for a fresh heartbeat and retry Email once. Use
CPR only if the existing startup exits or never becomes live.

For an explicitly authorized `abs` target, the same metadata/heartbeat handshake
applies. An absolute address is not a liveness or authorization bypass.

## Unread producer mirror

Mail arrival and every read-state mutation render the current unread set to
`.notification/email.json`. The kernel's next heartbeat sync exposes that data
through `notification(action="check")`. A typical producer envelope is:

```json
{
  "header": "3 unread emails",
  "icon": "📧",
  "priority": "normal",
  "published_at": "<timestamp>",
  "instructions": "Handle the mail, then prefer email.dismiss or use email.read/reply.",
  "data": {
    "count": 3,
    "newest_received_at": "<timestamp>",
    "email_ids": ["<local-id>"],
    "emails": [{
      "id": "<local-id>", "from": "peer", "subject": "...",
      "message": "full body", "message_chars": 10,
      "message_truncated": false
    }]
  }
}
```

`instructions` is producer-owned guidance: Email knows whether a message is a
full-body context entry and which producer verb clears its source state. The
attention lane is a high-attention hook carrying IDs; full structured entries
live in `_meta.agent_meta.notifications.persistent.email`. There is no separate
per-mail notification pair.

Ordinary new messages are rendered without truncating their bodies. The send
layer rejects bodies over 50,000 characters; only legacy over-limit records may
carry `message_truncated=true` defensively. The unread projection limits the
number of newest entries but keeps total unread count exact.

## Handling persistent mail and overflow

The persistent lane can make `read` unnecessary merely to see ordinary text.
After handling a visible entry, call `email(action="dismiss", input={"email_id":
[...]}, ...)` to mark it read without returning another body. Use `read` when
source-of-truth metadata, attachments, or a deliberate refresh is required.
`reply` and `reply_all` both mark the source inbox ID read as part of handling.
`archive` and `delete` also update read state. All four mutation paths rerender
the Email mirror, which disappears when no unread mail remains.

If the full persistent notification exceeds its model-visible block cap, the
kernel spills it to a local `logs/notification-overflow-<timestamp>.json` and
adds an `overflow` marker. Follow that marker or call the producer action for
full content; do not assume a truncated body is complete. The exact block cap
and environment ownership belong to `notification-manual`.

A generic
`notification(action="dismiss_channel", input={"channel": "email", ...}, ...)`
only clears the mirror. It does not mark source messages read and is not a
substitute for Email's `dismiss`, `read`, `reply`, `archive`, or `delete`.

## Bounce and retry interpretation

A bounce means no recipient inbox record was queued for that attempt. Inspect the
bounce event and target metadata/heartbeat. For a target in a refresh window,
wait for its heartbeat and retry once; do not assume that a successful `sent`
receipt proves delivery. For an unknown or invalid address, correct the address
from local metadata rather than guessing.
