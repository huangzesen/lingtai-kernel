---
name: email-manual-addressing-and-replies
description: >
  Focused Email reference for bare-path addressing, peer/abs resolution, safe
  same-channel replies, sender identity display, and local mailbox-ID privacy.
  Read after email-manual when routing or answering internal mail.
version: 1.0.0
tags: [lingtai, email, routing, replies, privacy]
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/email/manual/SKILL.md
- src/lingtai/tools/email/primitives.py
- src/lingtai/tools/email/manager.py
- src/lingtai/tools/email/CONTRACT.md
maintenance: |
  Tracks Email addressing, reply routing, identity display, and local-ID privacy; update when the manager or routing contract changes.
---

# Email addressing and replies

## Bare-path addresses

Internal Email addresses are names of directories inside `.lingtai/`, with no
`@`, domain, or slash in ordinary peer mode:

- `human` addresses the operator mailbox;
- `mimo-1` addresses an agent directory;
- your own agent name addresses a durable self-inbox.

`address` accepts one string or a list. Discover real agents by globbing `.lingtai/*/.agent.json` and use each file's
`agent_name`; do not invent an address. A refused target creates no recipient
inbox entry and is reported as an `email.bounce` system event.

## `peer` and `abs`

`mode` belongs to `send` and is normally omitted:

- `peer` (default) resolves a bare agent name against the parent of your own
  working directory. It is the normal mode for the human, fellow agents, and
  self-send.
- `abs` treats `address` as a literal absolute working-directory path in a
  different, explicitly authorized `.lingtai/` network on the same machine.
  It is for cross-network/project messaging only.

Both modes require the target's valid `.agent.json` and fresh heartbeat. `abs`
does not bypass the handshake or liveness check. Absolute sends embed a
`_return_route` containing the sender workdir and agent identity; this protects
replies when separate `.lingtai/` networks contain agents with the same short
name. Older messages without that field still use the absolute-`from` fallback.

## Same-channel reply discipline

**Reply on the channel where the message arrived.** An Email message must be
answered with `reply` or `reply_all`, not a new `send`, Telegram, IM, or text
output. Text output is a private diary, not a communication channel. If the
original sender is unavailable and a channel pivot is unavoidable, explain that
pivot in the message before sending elsewhere.

`reply` addresses the resolved sender. `reply_all` also carries the original
`to` and `cc` recipients except self and the primary reply target, so CC
participants are not silently dropped. Unless explicitly overridden, both derive
a `Re: ` subject from the original subject (or its first body line when absent).
They do not persist an `in_reply_to` field or thread ID; continuity comes from
recipient selection and the derived subject. The manager resolves a reply target
in this order: embedded `_return_route`, an absolute `from`, then a bare peer
`from`. It refuses an ambiguous peer route that would resolve to the responder's
own workdir while the original identity names a different agent.

## Sender display and local IDs

Inbound messages carry an `identity` block. In prose, call the sender by its
non-empty `sender_nickname`; otherwise use `sender_name`. The `from` value is a
routing address, not a display name.

A mailbox UUID (`email_id`) is local to one working directory. Pass IDs copied
from your own persistent notification or `check` result to `read`, `dismiss`,
`reply`, or `reply_all`; never paste a raw ID into mail or public prose. When referring to mail to another agent, use subject, sender, and approximate
time.

## Adjacent surfaces

This manual owns only the kernel-intrinsic `email` tool. Real internet mail
(Gmail, Outlook, IMAP/SMTP) belongs to the `imap` or `cloud_mail` MCP addon;
Telegram, Feishu, WeChat, and WhatsApp belong to their respective MCP addons.
Recurring agent-side work belongs to a host scheduler through `shell-manual`.
Do not use Email for an external address: an unknown target is refused without a
recipient inbox entry and is reported as a bounce.
