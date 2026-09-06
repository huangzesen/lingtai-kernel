---
name: email-manual
description: >
  Internal LingTai mail: send/read/dismiss/reply, bare-path addressing,
  delayed self-send time capsules, and the full-body persistent notification
  contract. Not internet email (see `mcp-manual`) or recurring schedules
  (see `shell-manual`).
version: 1.2.2
tags: [capabilities, email, communication]
last_changed_at: "2026-09-04T00:00:00Z"
related_files:
- src/lingtai/tools/skills/manual/reference/cleanup-footprint-contract.md
- src/lingtai/tools/email/__init__.py
- src/lingtai/tools/email/_family_schema.py
- src/lingtai/tools/email/manager.py
- src/lingtai/tools/email/primitives.py
- src/lingtai/tools/email/settings.py
- src/lingtai/adapters/posix/mail.py
- src/lingtai/tools/email/ANATOMY.md
- src/lingtai/tools/email/CONTRACT.md
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# Email Manual — the internal `email` tool

> LingTai email protocol between agents in your `.lingtai/` network. Not the internet. No IMAP, no SMTP, no DNS. Messages are JSON files written under `mailbox/inbox/` of the recipient agent and `mailbox/sent/` of the sender.

## 0. How to call it — the envelope

Every call takes `action` + `input` + `reasoning`, where `input` is the strict
argument object **for the selected action only**:

```python
email(action="check", input={}, reasoning="check for new mail")
email(action="read", input={"email_id": ["<id>"]}, reasoning="read the request")
email(action="send", input={"address": "human", "message": "done"},
      reasoning="report completion")
```

Two rules follow from that, and they are enforced at dispatch, not just in the
schema:

- **Each argument belongs to one action.** `query` only exists on `search`,
  `filter`/`n` only on `check`, `attachments`/`delay`/`mode` only on `send`. A
  key from another action's branch is refused *before* anything is read, sent,
  or marked read — you get `unsupported email input field`, not a silent
  ignore.
- **`reasoning` and `summarize` are root fields, never inside `input`.**
  `reasoning` is required and is recorded in your diary; `summarize` is the
  optional result post-processing control.

**`summarize` guidance for this family.** `check`, `read`, and `search` are
**bulky-result** actions — mailbox listings and full bodies can be long, so
`summarize=true` is reasonable when you only need the gist. Leave it false
when you need exact IDs, addresses, or verbatim body text, because you will
act on those literally. Every other action (`send`, `dismiss`, `reply`,
`reply_all`, `archive`, `delete`, the four contact verbs, `settings`) is
**short-result**: its receipt is small and meant to be read exactly, so leave
`summarize` false. Call `manual` itself with `summarize=false` so procedure
and constraints are not summarized away.

**Settings:** `email(action="settings", input={}, reasoning="inventory Email policy")`
is SHOW-only and returns exactly five fields per row: `key`, `current`,
`default`, `configurable`, and an exact section pointer in `comment`. It has no
set/reset or other mutation input. Read the [settings reference](#settings-reference)
below for source, precedence, accepted values, timing, sensitivity, and the
actual owner procedure. A second SHOW verifies the effective snapshot after an
authorized external change and full relaunch.

## Settings reference

Email supports no owner settings file or environment peer: there is no
`settings/email.json`, no `settings/email.<action>.json`, and no
`LINGTAI_EMAIL_*`. Four installed policy limits are public and
non-configurable. The pseudo-agent subscription list is the one configurable
row, owned by the existing launcher manifest path. Mailbox/session paths,
addresses, identities, contacts, messages, attachments, and read/archive state
are private runtime or domain data, not settings, and never appear.

`LINGTAI_AGENT_ALIVE_THRESHOLD_SEC` remains kernel liveness policy and
`LINGTAI_NOTIFICATION_MAX_CHARS` remains Notification presentation policy;
Email does not claim either environment variable. Per-call `send`/`check`
options are action input rather than persisted settings. The legacy
200-character digest-renderer prose is discarded by the live full-body
notification publisher, so it is not an effective Email setting.

### Send body character limit

- Key: `send.body_char_limit`; current and meaningful default: integer `50000`.
- Meaning: maximum accepted internal-email body length, in Unicode characters;
  an oversize `send` is refused before delivery.
- Accepted configuration values: none. The installed integer constant in
  `email/settings.py` is consumed directly by send and unread-notification code.
- Source and precedence: installed code only. Canonical environment variable
  and config key: none. It is public, not sensitive.
- Application timing and authorized change procedure: `configurable` is false.
  Only a reviewed product-code/package change followed by a full agent relaunch
  can change it; SHOW never writes. Re-run SHOW after relaunch.

### Duplicate send loop guard

- Key: `send.duplicate_free_passes`; current and meaningful default: integer
  `2`.
- Meaning: number of consecutive identical sends allowed per recipient before
  Email blocks the next duplicate as a loop.
- Accepted configuration values: none. The installed constant in
  `email/settings.py` initializes each `EmailManager`.
- Source and precedence: installed code only. Canonical environment variable
  and config key: none. It is public, not sensitive.
- Application timing and authorized change procedure: `configurable` is false.
  Only a reviewed code/package change plus full relaunch changes it; verify with
  a second SHOW.

### Check result token limit

- Key: `check.result_token_limit`; current and meaningful default: integer
  `10000`.
- Meaning: token budget for one `check` result; Email removes summaries until
  the serialized response fits.
- Accepted configuration values: none. The installed constant in
  `email/settings.py` is consumed directly by `EmailManager._check`.
- Source and precedence: installed code only. Canonical environment variable
  and config key: none. It is public, not sensitive.
- Application timing and authorized change procedure: `configurable` is false.
  Only a reviewed code/package change plus full relaunch changes it; verify with
  a second SHOW.

### Unread notification entry limit

- Key: `unread.max_entries`; current and meaningful default: integer `10`.
- Meaning: maximum number of newest unread message entries projected into one
  Email notification mirror; the total unread count remains exact.
- Accepted configuration values: none. The installed constant in
  `email/settings.py` supplies the unread-renderer defaults.
- Source and precedence: installed code only. Canonical environment variable
  and config key: none. It is public, not sensitive.
- Application timing and authorized change procedure: `configurable` is false.
  Only a reviewed code/package change plus full relaunch changes it; verify with
  a second SHOW.

### Pseudo-agent subscriptions

- Key: `manifest.pseudo_agent_subscriptions`; current and default are always
  `<redacted>`. Both are path lists and are fully redacted by construction.
- Meaning: pseudo-agent directories whose outboxes the POSIX mail adapter polls
  in addition to its own inbox. SHOW reads the adapter's effective list after
  those paths were resolved once against the agent workdir at construction.
- Accepted values: a JSON list of path strings. An empty list disables these
  subscriptions. The launcher's meaningful default when the field is absent is
  `["../human"]`. The init schema checks that the outer value is a list; an
  element that cannot be interpreted as a path fails mail-adapter construction
  instead of being silently ignored.
- Source and precedence: only `init.json` key
  `manifest.pseudo_agent_subscriptions`; there is no environment or owner-file
  peer. The raw configured/resolved lists are sensitive local routing data and
  must not be copied from logs or inferred from SHOW.
- Application timing and authorized change procedure: `configurable` is true.
  After explicit owner/human authorization, edit that exact `init.json` field
  with the existing File or Shell capability, then perform a full agent
  relaunch. Ordinary refresh does not reconstruct the mail adapter and cannot
  apply this field. Call SHOW again after relaunch; it confirms availability
  and redaction, never the raw paths.

## 1. What is internal email

The `email` tool moves messages as files between agents that share a `.lingtai/` directory tree:

- `email(action="send")` records sender-side outbox/sent state, then starts one `_mailman` daemon thread per recipient. Even when `delay=0`, the tool may return `status: "sent"` before that background delivery attempt finishes.
- Delivery to a normal agent accepts only a target with `.agent.json` and a fresh `.agent.heartbeat` (normally younger than two seconds); human recipients (`admin: null`) skip the heartbeat check. If the target is refreshing/relaunching and its heartbeat is not fresh yet, delivery is refused as `not running`; no recipient inbox entry is queued for later. The eventual failure is surfaced to the sender as an `email.bounce` event in `.notification/system.json`.
- Read state lives in the recipient's `mailbox/read.json` (a set of message IDs).
- The kernel mirrors current unread mail into `.notification/email.json`, which surfaces as a notification block read via `notification(action="check")` — that's how you find out new mail arrived.

> **Refresh/relaunch window.** A target `lingtai run` process can already be visible in `ps` before it publishes a fresh heartbeat. In that interval, internal email may bounce for liveness while a CPR attempt's child launch exits because the CLI duplicate-process guard finds the existing same-workdir PID. Those results are compatible: do not stack CPR attempts. Wait for the target heartbeat to become fresh, then retry the email once. Use CPR only if the existing startup exits or fails to become live.

**If a request involves `@gmail.com`, `@outlook.com`, IMAP folders, or anything that needs to leave the machine, the right tool is the `imap` MCP addon — see the `mcp-manual` skill, not this one.**

| Feature         | Internal Email (this skill)                            | IMAP (see `mcp-manual`)                          |
|-----------------|--------------------------------------------------------|--------------------------------------------------|
| What            | LingTai email protocol within `.lingtai/` network      | Real email via IMAP/SMTP (Gmail, Outlook, etc.)  |
| Address format  | Bare path (e.g. `human`, `mimo-1`)                     | `@` address (e.g. `alice@gmail.com`)             |
| Tool            | `email` (intrinsic)                                    | `imap` (MCP server, `imap` addon)                |
| Reply policy    | Always reply on the same channel                       | Requires confirmation for unknown senders        |
| Persistence     | Survives molt, lives in working directory              | External mailbox, managed by IMAP server         |
| Use case        | Agent-to-agent communication, self-send, time capsules | Real-world email integration                     |

## 2. Addressing

Addresses are **bare directory names** inside `.lingtai/`. No `@`, no domains, no slashes.

| Address              | Meaning                                                  |
|----------------------|----------------------------------------------------------|
| `human`              | The human's mailbox at `.lingtai/human/`                 |
| `mimo-1`             | An agent whose working directory is `.lingtai/mimo-1/`   |
| `<your-own-name>`    | Self — creates an inbox entry that survives molt (§6)    |

Multiple recipients: pass `address` as a string or a list, plus optional `cc` / `bcc`.

```python
email(action="send",
      input={"address": ["mimo-1", "scribe"], "cc": ["human"],
             "subject": "status", "message": "ready"},
      reasoning="report status to the team")
```

To discover who exists: glob `.lingtai/*/.agent.json` from a shell. Use the `agent_name` field of each as the address. Do not invent addresses — a refused dispatch produces an `email.bounce` event and queues no recipient inbox entry.

### `mode` — peer vs abs address resolution

`mode` is the **address mode for `send`**, not an output-verbosity knob. Almost always leave it unset:

- `peer` (default) — `address` is a bare agent name resolved against your own network folder. Correct for the human, fellow agents, and your own avatars.
- `abs` — `address` is a literal absolute path to another agent's working directory, e.g. `/Users/alice/projectB/.lingtai/外援`. Use it only to reach an agent in a *different* `.lingtai/` network on the same machine. It embeds a `_return_route` so replies resolve unambiguously, and it does **not** bypass the handshake: the recipient still needs a valid `.agent.json` and a fresh heartbeat.

Any other value is rejected with `invalid mode`.

## 3. Reply discipline — the one rule you cannot break

> **Reply on the channel the message arrived on.**

If a message arrived via `email`, reply with `email(action="reply", ...)`. Do not pivot to `pigeon`, IM, or a fresh `send`. If you must change channels (e.g. the original sender is dead), explain that pivot in the reply body before sending it elsewhere.

**Prefer `reply` and `reply_all` over `send`** even when you know the addresses:

- `reply` preserves the thread linkage (the original `id` lands in the new message's `in_reply_to`), so a future `search` or `check` shows the conversation as related.
- `reply_all` mirrors the original recipient set automatically, so you don't drop someone who was `cc`'d.
- `send` is for **new** conversations.

Doing it the other way scatters conversations across orphaned threads and is the single most common confusion source in human-facing audits.

## 4. Sender display name resolution

Inbound mail carries an `identity` block:

```json
"identity": {
  "sender_name":     "mimo-1",
  "sender_nickname": "MiMo",
  "via":             "lingtai" | "claude-code" | ...
}
```

When you mention the sender in a reply body or in a summary you give the human, use `sender_nickname` if it is set and non-empty; otherwise fall back to `sender_name`. The address itself (`from`) is for routing, not for prose.

## 5. Actions — full surface

| Action            | Purpose                                                            | Required args                                      |
|-------------------|--------------------------------------------------------------------|----------------------------------------------------|
| `send`            | Start a new thread; body hard-capped at 50,000 characters          | `address`, `subject`, `message`                    |
| `check`           | List inbox (newest-first), with optional `filter={...}` and `n=N`  | —                                                  |
| `read`            | Fetch source-of-truth record/attachments and mark read             | `email_id` (list of IDs)                           |
| `dismiss`         | Mark read **without** re-fetching body — preferred after persistent content is handled | `email_id` (list of IDs)                           |
| `reply`           | Reply to sender only; preserves thread linkage                     | `email_id`, `message`                              |
| `reply_all`       | Reply to sender + all original recipients minus self               | `email_id`, `message`                              |
| `search`          | Search across inbox/sent/archive by `query` + `filter`             | `query` (and/or `filter`)                          |
| `archive`         | Move from inbox to archive folder (keeps thread, removes from view)| `email_id`                                         |
| `delete`          | Permanently delete (inbox/archive only; `sent` is read-only)       | `email_id`                                         |
| `contacts`        | List your address book                                             | —                                                  |
| `add_contact`     | Add or upsert by `address`                                         | `address`, `name`, optional `note`                 |
| `remove_contact`  | Remove by `address`                                                | `address`                                          |
| `edit_contact`    | Update fields                                                      | `address`, plus the fields to change               |
| `settings`        | SHOW Email policy/source truth; no mutation input exists           | —                                                  |
| `manual`          | Return this installed Email manual                                 | —                                                  |

### `read` vs `dismiss` — when to use which

Unread email bodies are injected in full into `_meta.agent_meta.notifications.persistent.email` (up to the 50,000-character send-layer cap below). You do **not** need `read` merely to see ordinary message text. When the whole persistent notification envelope exceeds its model-visible cap (see `notification-manual` → "Block size cap" for the exact default/ceiling/floor and `LINGTAI_NOTIFICATION_MAX_CHARS` precedence), the full block is spilled to `<workdir>/logs/notification-overflow-<ts>.json` and the block carries an `overflow` marker with the path; read that file (or the producer tool) for the full bodies. After you have handled the visible content, prefer `dismiss`: same read-state effect, no body returned, and the unread notification clears once count reaches zero.

Use `read` when you need to refresh the source-of-truth mailbox record, inspect attachment/metadata details, or deliberately fetch the producer state before a reply/audit. Use `reply`/`reply_all` when answering. Failing to `dismiss`, `read`, `archive`, or `delete` a handled mail keeps the notification reminding you on every heartbeat.

These are the producer-owned verbs for the `email` notification channel; a generic `notification(action='dismiss_channel', input={'channel': 'email', 'force': null, 'reason': null}, reasoning='...')` would clear only the mirror. See `notification-manual` → `reference/dismissal-safety/SKILL.md`.

### 50,000-character send cap

Internal email bodies are capped at 50,000 characters at **send time**. The reason is architectural: unread bodies are injected in full into the persistent notification stream, so the reading/notification layer should not guess, summarize, or truncate ordinary mail. If a message is too large for that guarantee, `send` refuses it with `limit_chars` and `actual_chars`; shorten the body, attach a file, or put bulky material somewhere else and mail a pointer.

### `check` filter

`check` accepts a structured `filter` for narrowing the inbox without round-tripping:

```python
email(action="check",
      input={
          "n": 20,
          "filter": {
              "unread_only":     True,
              "from":            "mimo-1",
              "subject":         "status",
              "contains":        "blocker",
              "after":           "2026-05-18T00:00:00Z",
              "has_attachments": False,
              "sort":            "newest",
              "truncate":        500,    # body preview length per entry
          },
      },
      reasoning="find unread blockers from mimo-1")
```

`filter` and `n` belong to `check` alone. Any field you do not need may simply
be omitted (or sent as `null`, which is treated the same as omitting it).

Use this aggressively. Pulling 100 messages with `check` and then post-filtering in your head is wasteful.

## 6. Self-send — persistent notes that survive molt

Mail sent to **your own address** lands in your own inbox. It is marked self-sent, but otherwise behaves like any other unread message — meaning:

- It survives a molt (because it lives in `mailbox/inbox/`, not in chat history).
- It surfaces in the persistent unread notification lane until you `dismiss`, `read`, `archive`, or `delete` it.
- It can be `search`ed by the future you.

Use this for: TODOs you want to remember after a memory rotation, breadcrumbs about decisions, "hand-off to self" notes during a long task. See the recipes in §10.

## 7. Time capsule — delayed self-send

Add `"delay": <seconds>` to `send`'s `input` to defer delivery. The outbox entry is written immediately; the `_mailman` daemon thread sleeps until the deadline, then dispatches. Combined with self-send this gives you cheap one-shot alarms without standing up a cron; the notification is delivered exactly once.

Use delayed self-send as a **future nudge**, not delayed tool execution. The message should tell the future you what to inspect and why, then let that future turn decide with current context whether to run `shell(action="poll")`, `daemon(check)`, a channel read, or nothing at all. It is one of the escape hatches when a repeated-call `_advisory` says you may be polling the same thing: write one concrete reminder, then yield/idle.

**Recurring work is not an email feature.** The internal `email` tool has no recurring scheduling API. For repeating reminders or agent-side scheduled work, use a host scheduler (cron, launchd, systemd, or an event watcher) via `shell-manual` → `reference/scheduled-work/SKILL.md`; for a single lightweight wakeup that does not need mailbox state, `shell-manual` → `reference/notification-reminders/SKILL.md` owns the `.notification/cron.json` pattern.

## 8. Privacy — internal IDs

The mailbox UUID (`email_id`) is **local to your working directory**. Never paste a raw mailbox ID into a message to another agent or to the human — it has no meaning outside your tree and reveals nothing useful. Refer to messages by `subject` + `from` + approximate time.

The exception: when you call `email(action="read"/"dismiss"/"reply", input={"email_id": [...]})`, you pass IDs you read out of *your own* persistent notification or mailbox listing. That's internal plumbing, fine.

## 9. Addon ownership — what this skill does NOT cover

This skill is the manual for the **kernel-intrinsic `email` tool** only. Adjacent surfaces live elsewhere:

| Want to …                                          | Use                                          |
|----------------------------------------------------|----------------------------------------------|
| Send/receive real internet email (Gmail, etc.)     | `mcp-manual` → `imap` or `cloud_mail` addon  |
| Send Telegram / Feishu / WeChat / WhatsApp messages | `mcp-manual` → respective MCP addon          |
| Send a notification-style ping to another agent    | This skill — it IS the notification channel  |
| Schedule a one-off wake-up of your own loop        | This skill, `delay` + self-send (§7)         |
| Run recurring agent-side work                       | Host scheduler / event watcher via `shell-manual` |

Those MCP addons are separate processes with separate auth surfaces and failure modes; each ships its own manual. Do not try to use the `email` tool for an external address: an unknown target is refused without creating a recipient inbox entry and is reported through `email.bounce`.

## 10. Quick reference — common recipes

```python
# Handle content already injected into notification_persistent.email
email(action="dismiss", input={"email_id": ["<id-from-persistent-email>"]},
      reasoning="clear mail already handled from the notification")

# Need source-of-truth refresh / attachments / metadata
email(action="read", input={"email_id": ["<id-from-persistent-email>"]},
      reasoning="read the full body and attachments")

# Optional mailbox listing / filters
email(action="check", input={"n": 20, "filter": {"unread_only": True}},
      reasoning="list pending unread mail")

# Thread-preserving reply
email(action="reply", input={"email_id": ["<id>"], "message": "ack, looking now"},
      reasoning="acknowledge on the channel it arrived on")

# Self-note that survives molt
email(action="send",
      input={"address": "<self>", "subject": "resume",
             "message": "Picked the Helmholtz approach; see paper/drafts/2026-05-18.md"},
      reasoning="leave a breadcrumb for after the next molt")

# 5-minute timer
email(action="send",
      input={"address": "<self>", "delay": 300,
             "subject": "ding", "message": "check the deploy"},
      reasoning="set a one-shot nudge to re-check the deploy")

# Reach an agent in another .lingtai/ network on this machine
email(action="send",
      input={"mode": "abs", "address": "/Users/alice/projectB/.lingtai/外援",
             "subject": "cross-network", "message": "ping"},
      reasoning="contact an agent in another network by absolute path")

# Find related mail
email(action="search", input={"query": "helmholtz"},
      reasoning="find prior discussion of this approach")

# Address book
email(action="add_contact",
      input={"address": "mimo-1", "name": "MiMo (vision)",
             "note": "reachable for image-analysis requests"},
      reasoning="record a durable contact")
```

Note that `search` takes `query` (and optionally `folder`) — the `filter`
object belongs to `check`, and passing it to `search` is refused at dispatch.

---
> **Found a bug or issue?** If you encounter any problems with this skill, load the `lingtai-issue-report` skill and follow its instructions to report it.

## Cleanup / Footprint

Internal email persists under the agent mailbox: inbox/archive/sent message
files, attachments, contacts, and read/archive state. Mail is also memory: do not
blindly delete it. Prefer `email(archive)` or `email(delete)` verbs over `rm`,
and never delete mail that is the only copy of a decision, handoff, or
attachment the human may expect you to retain.

Footprint check: load the [shared inspection recipe](../../skills/manual/reference/cleanup-footprint-contract.md#shared-footprint-check-recipe)
through `skills-manual` → `reference/cleanup-footprint-contract.md`. Combine
its definitions with this tool-specific selection in one task-owned script;
the selection is not a standalone executable. Inspection writes nothing.
Appending `logs/cleanup.jsonl` is the separate, explicitly selected audit step
in that recipe; retain this manual's cleanup/approval rules below.

```python
agent = Path.cwd()  # the relevant agent directory, not a repository root
roots = [p for p in (agent / "mailbox", agent / "mail", agent / "email") if p.exists()]
items = [p for root in roots for p in ([root] if root.is_file() else root.iterdir())]
rows, total = footprint_check(items, tool="email", top_n=20)
```

Recommended cadence: when large attachments are exchanged, before exporting or
archiving a project, and quarterly for long-lived agents. Cleanup requires a
dry-run report plus explicit user consent; after deletion/archive, append an
`apply` record to `logs/cleanup.jsonl`.
