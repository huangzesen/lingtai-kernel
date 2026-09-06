---
name: email-manual
description: >
  Internal LingTai mail: send/read/dismiss/reply, bare-path addressing,
  delayed self-send time capsules, and the full-body persistent notification
  contract. Not internet email (see `mcp-manual`) or recurring schedules
  (see `shell-manual`).
version: 1.3.0
tags: [capabilities, email, communication]
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/email/__init__.py
- src/lingtai/tools/email/_family_schema.py
- src/lingtai/tools/email/manager.py
- src/lingtai/tools/email/primitives.py
- src/lingtai/tools/email/settings.py
- src/lingtai/adapters/posix/mail.py
- src/lingtai/tools/email/ANATOMY.md
- src/lingtai/tools/email/CONTRACT.md
- src/lingtai/tools/email/manual/reference/addressing-and-replies/SKILL.md
- src/lingtai/tools/email/manual/reference/actions-and-storage/SKILL.md
- src/lingtai/tools/email/manual/reference/notifications-and-delivery/SKILL.md
- src/lingtai/tools/email/manual/reference/settings-reference/SKILL.md
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# Email Manual — the internal `email` tool

> Internal LingTai mail only. It moves JSON files inside a shared `.lingtai/`
> network; it is not Gmail, Outlook, IMAP, SMTP, DNS, or any other internet mail.

## 0. Call envelope

Every call has `action`, `input`, and `reasoning`; `input` contains only the
fields for the selected action. `reasoning` is required and `summarize` is an
optional root result control, never an input field.

```python
email(action="check", input={}, reasoning="check for new mail")
email(action="read", input={"email_id": ["<id>"]}, reasoning="read the request")
email(action="send", input={"address": "peer", "message": "done"},
      reasoning="report completion")
```

The family rejects unknown root fields and cross-action input keys before
mailbox I/O, delivery, or read-state mutation. Call
`email(action="manual", input={}, reasoning="learn Email")` to return this
installed router and its host-local path.

`check`, `read`, and `search` can return bulky listings or bodies: use
`summarize=true` only when exact IDs, addresses, or body text are not needed.
Leave it false for receipts, contacts, settings, and `manual`.

## 1. Choose an action

| Action | Use | Required input / critical note |
|---|---|---|
| `send` | Start a new internal thread | `address`, `message`; body max 50,000 characters |
| `check` | List mail | Optional `folder`, `n`, and structured `filter` |
| `read` | Fetch source-of-truth mail | `email_id` list; marks inbox IDs read |
| `dismiss` | Clear handled mail without fetching bodies | `email_id` list; marks inbox IDs read |
| `reply` / `reply_all` | Continue a thread | `email_id` list (one ID) and `message`; use the arrival channel |
| `search` | Regex search | `query`; optional `folder` |
| `archive` | Move inbox mail out of the inbox | `email_id` list |
| `delete` | Permanently remove mail | `email_id` list; inbox/archive only, never `sent` |
| `contacts` | List the private address book | no input |
| `add_contact` | Add or update a contact | `address`, `name`; optional `note` |
| `remove_contact` | Remove a contact | `address` |
| `edit_contact` | Update contact fields | `address`; optional `name`/`note` |
| `settings` | Show Email policy/source truth | input must be `{}`; read-only |
| `manual` | Load this procedure | input must be `{}`; no mailbox I/O |

For action-specific fields, defaults, filters, persistence, and examples, read
[Actions and storage](reference/actions-and-storage/SKILL.md). For address modes,
reply routing, sender names, and local-ID privacy, read
[Addressing and replies](reference/addressing-and-replies/SKILL.md).

## 2. Non-negotiable routing and privacy

- **Reply on the channel where the message arrived.** For Email, use `reply` or
  `reply_all`, not a new `send`; never answer through text output (that is a
  private diary). If a dead sender forces a channel change, explain the pivot in
  the message first.
- Use the sender's `sender_nickname` when non-empty, otherwise `sender_name`.
- Addresses are bare names/paths inside `.lingtai/`, not `@` addresses. For
  internet mail, use the separately owned `imap` MCP addon. `mode="peer"` is
  normally enough; `mode="abs"` is restricted to explicitly authorized
  cross-network paths and does not bypass delivery checks.
- Mailbox IDs are local to this working directory. Pass IDs read from your own
  notification or listing to Email actions, but never put raw IDs in mail or
  public prose. See [Addressing and replies](reference/addressing-and-replies/SKILL.md).

Sending writes sender-side state before starting one daemon delivery thread per
recipient. Even with no delay, `status="sent"` can precede delivery; a target
must have valid agent metadata and a fresh heartbeat. Delivery failures are
reported as `email.bounce` system events, not queued for later. Full delivery,
refresh-window, and recovery semantics are in
[Notifications and delivery](reference/notifications-and-delivery/SKILL.md).

## 3. Read state and notifications

Unread bodies are injected in full into
`_meta.agent_meta.notifications.persistent.email`. After handling content already
shown there, prefer `dismiss`; use `read` for a source-of-truth refresh,
attachments, or deliberate audit. `read`, `dismiss`, `archive`, and `delete`
refresh the producer-owned `.notification/email.json` mirror. A handled message
stays visible until one of those producer verbs changes its read state.

The mirror's attention hook carries IDs while the persistent lane carries full
entries. If the model-visible block overflows, follow its `overflow` marker to the
local spill file or use the producer action; do not infer missing content.
Detailed payload shape, caps, refresh behavior, and the distinction from generic
notification dismissal are in
[Notifications and delivery](reference/notifications-and-delivery/SKILL.md).

## 4. Settings anchors

`settings` is SHOW-only: every row has exactly `key`, `current`, `default`,
`configurable`, and `comment`. It performs no mailbox I/O and never exposes
paths, identities, addresses, contacts, content, attachments, or read state.
Comments below are stable anchors used by the settings provider; each short stub
routes to the full source/precedence/procedure reference.

### Send body character limit

`send.body_char_limit` is the installed 50,000-character send/reply cap; see
[the settings reference](reference/settings-reference/SKILL.md#send-body-character-limit).

### Duplicate send loop guard

`send.duplicate_free_passes` is the installed consecutive-duplicate guard; see
[the settings reference](reference/settings-reference/SKILL.md#duplicate-send-loop-guard).

### Check result token limit

`check.result_token_limit` is the installed `check` result budget; see
[the settings reference](reference/settings-reference/SKILL.md#check-result-token-limit).

### Unread notification entry limit

`unread.max_entries` limits projected unread entries while preserving total count;
see [the settings reference](reference/settings-reference/SKILL.md#unread-notification-entry-limit).

### Pseudo-agent subscriptions

`manifest.pseudo_agent_subscriptions` is the configurable, fully redacted
construction snapshot; see [the settings reference](reference/settings-reference/SKILL.md#pseudo-agent-subscriptions).

## 5. Self-send and delay

Mail to your own address is a durable inbox note that survives molt and remains
in the unread lane until handled. `delay` is seconds before one delivery attempt:
the outbox record is written immediately and a daemon thread waits. Delayed
self-send is a one-shot future nudge, not delayed tool execution or recurring
scheduling. For recurring work, use the host scheduler routed by `shell-manual`.
See [Actions and storage](reference/actions-and-storage/SKILL.md#self-send-and-time-capsules).

## 6. Cleanup and footprint

Email persists inbox/archive/sent messages, attachments, contacts, and read state.
Do not blindly delete mail that is the only copy of a decision, handoff, or
attachment. Prefer the Email `archive`/`delete` actions over filesystem removal.
For a dry-run footprint inspection and explicit-consent cleanup procedure, load
the shared [cleanup-footprint contract](../../skills/manual/reference/cleanup-footprint-contract.md#shared-footprint-check-recipe).

## Reference map

- [Addressing and replies](reference/addressing-and-replies/SKILL.md) — bare-path
  addresses, `peer`/`abs`, same-channel replies, identity, and ID privacy.
- [Actions and storage](reference/actions-and-storage/SKILL.md) — action details,
  filters, folders, self-send, time capsules, and durable layout.
- [Notifications and delivery](reference/notifications-and-delivery/SKILL.md) —
  liveness, bounce/recovery, unread payloads, overflow, and read-state refresh.
- [Settings reference](reference/settings-reference/SKILL.md) — five rows,
  effective sources, redaction, timing, and SHOW-only behavior.

> Found a bug? Load the `lingtai-issue-report` skill and follow its procedure.
