---
name: telegram-mcp-manual
description: |
  Progressive-disclosure usage manual for the Telegram MCP tool. The resident
  schema carries safe first-use guidance; call `manual` for the action map,
  channel/reply/media/rendering rules, placeholder and chat-action boundaries,
  inbound envelopes, settings, Task Card projection, and error handling.
version: 1.8.0
last_changed_at: 2026-09-07T00:00:00Z
related_files:
- src/lingtai/mcp_servers/ANATOMY.md
- src/lingtai/mcp_servers/task_card/event_projection.py
- src/lingtai/mcp_servers/task_card/resident.py
- src/lingtai/mcp_servers/local_commands/ANATOMY.md
- src/lingtai/mcp_servers/local_commands/core.py
- src/lingtai/mcp_servers/telegram/manager.py
- src/lingtai/mcp_servers/telegram/account.py
- src/lingtai/mcp_servers/telegram/render.py
- src/lingtai/mcp_servers/telegram/server.py
- src/lingtai/mcp_servers/telegram/_family.py
- src/lingtai/mcp_servers/telegram/settings.py
- src/lingtai/mcp_servers/telegram/service.py
- src/lingtai/mcp_servers/telegram/task_card/_family.py
- src/lingtai/mcp_servers/telegram/task_card/ANATOMY.md
- src/lingtai/mcp_servers/telegram/task_card/SKILL.md
- src/lingtai/mcp_servers/telegram/reference/rate-limits/SKILL.md
- src/lingtai/tools/task_card/manual/SKILL.md
- tests/test_telegram_structured_rendering.py
- tests/test_telegram_settings.py
- ENVIRONMENT_VARIABLES.md
maintenance: |
  Tracks the MCP server's manager/config/settings behavior; update when setup,
  settings precedence/redaction, or the public API surface changes. Keep this
  manual a concise router: detailed provider facts belong in the linked
  rate-limit and Task Card references, while this page retains first-use safety
  distinctions.
---

# Telegram MCP — usage manual

This page is the on-demand manual for the `telegram` MCP family. The resident
schema is intentionally short; call `telegram` with `action='manual'` and an
empty `input` when you need this page. Registration, `init.json` activation,
private config placement/permissions, and setup readiness belong to
`mcp-manual` → `reference/curated-addons.md`, not this package manual.

## Read first: strict envelope and safe first use

Every call uses the closed root `{action, input, reasoning, summarize?}`:
`action`, `input`, and `reasoning` are required; `summarize` is optional and is
not action input. Put only the selected action's fields inside `input`. The
`manual` and `settings` actions take `{}`. `reasoning` is audit metadata, not a
Telegram message or a substitute for `input`.

A safe first-use route is:

1. Use `read` or `check` to establish the conversation. Prefer `reply` with the
   compound `message_id` from `read`/`search` when answering a specific message.
2. Use `send` for a standalone message and a real numeric `chat_id`. A call that
   changes Telegram messages (`send`, `reply`, `edit`, or `delete`) is an
   external side effect; inspect the target and check the result.
3. Inspect the returned object for `error` (and for 429 results, the provider
   cooldown) instead of assuming delivery from a successful tool invocation.

Content-bearing `send`, `reply`, and `edit` default to `rendering_mode='Markdown'`.
Use `plain_text`, `HTML`, `MarkdownV2`, `entities`, or `rich` deliberately; do
not mix rendering modes. Detailed field guidance is in the sections below.

## Action map

| Action | Use and side effect |
|---|---|
| `send` | New message to a numeric `chat_id`; provide `text`, `media`, `structured_message`, or an ephemeral `chat_action`. |
| `check` | Recent conversation summaries and incoming unread counts; does not mark messages read. |
| `read` | Recent messages for one `chat_id`; marks returned records read and clears the matching wake-notification mirror. |
| `reply` | New message threaded to a compound target ID from `read`/`search`; marks the target handled and adds the replied reaction. |
| `search` | Case-insensitive regex over stored inbound text, sender fields, and update type; no send side effect. |
| `edit` | Edit a bot message by compound ID; text/rich messages and media captions have different limits. |
| `delete` | Delete a bot message by compound ID; external side effect, so verify the exact ID. |
| `contacts` / `add_contact` / `remove_contact` | Read or change local aliases only. A contact never grants inbound permission. |
| `accounts` | List configured account aliases and safe details; credentials are not returned. |
| `settings` | Read-only five-field settings inventory; it has no set/reset form. |
| `manual` | Return this packaged page and metadata; it performs no Telegram operation. |

Account aliases are optional on actions that accept them; omission uses the
service default. When multiple accounts exist, use the alias explicitly for
stateful operations. Compound message IDs have the form
`account_alias:chat_id:message_id` and should be copied from a returned record,
not reconstructed from a guess.

## Send and reply: channel, media, and side effects

- `send` needs a real numeric `chat_id` and one content alternative: `text`,
  `media`, `structured_message`, or `chat_action`. `reply` needs a compound
  `message_id` plus `text` or `structured_message`; it is the preferred response
  to one incoming message. `reply` itself sends a new durable message; it does
  not edit the target.
- `media` is `{type: 'photo'|'document', path: '...'}`. Use
  `type='document'` for charts, plots, reports, HTML/SVG/PNG/PDF exports, CSVs,
  and other generated artifacts the user should open intact. Use `photo` only
  for a native inline preview: Telegram may crop, compress, or thumbnail a
  photo, which can make text-heavy graphics unreadable. Attach the file; do not
  paste a local path into message text. Outbound paths must resolve inside the
  agent working directory and point to a readable, non-empty file.
- `reply_markup` is accepted only by `send` and `edit`. `entities` applies to
  message text on `send`, `reply`, and `edit`; `caption_entities` is accepted
  only by media-bearing `send` when `rendering_mode='entities'`. Do not combine
  entity data with a parse-mode choice. `link_preview_options` and
  `disable_web_page_preview` are `send`-only text options.
- An identical send can return `status='blocked'`; treat that as already sent,
  not as a transient failure to replay.

## Rendering and native rich messages

The supported modes are exactly `plain_text`, `HTML`, `Markdown`, `MarkdownV2`,
`entities`, and `rich`. `plain_text` omits Telegram `parse_mode`; the named
parse modes pass through to Telegram; `entities` supplies explicit
`MessageEntity[]` data.

For `rich`, omit `text` and `media`, set `rendering_mode='rich'`, and provide
`structured_message`. Its allowed semantic fields are:

- required `title`;
- optional `summary`, `facts` (each `{label, value}`), `bullets`, and ordered
  `steps`;
- optional `code` (`text` plus `language`), `next` (`label` plus `text`), and
  `footer`.

The addon renders these as native heading, paragraph, list, preformatted,
divider, and footer blocks while preserving authored wording and meaningful
emoji. Rich content can be sent or used in a reply and can edit a text/rich
message; it cannot edit a media caption. Ordinary conversation need not be
forced into a rich card.

## Placeholder and chat actions

For work likely to take more than about five seconds, `send` may use
`placeholder=true` with interim text. The result supplies a compound
`message_id`; edit that same message at meaningful phase changes. The
placeholder is progress-only: send the final answer as a separate durable
`send` or `reply` message, rather than editing the placeholder into the final
answer. This surface is separate from the Task Card.

A `chat_action` (`typing`, `upload_photo`, `upload_document`, or `upload_voice`)
with no text/media sends only Telegram's ephemeral indicator. It expires after
about five seconds, so repeat it deliberately during long work. Pass `''` or
omit it for no indicator. A chat action is not a message and is not persisted
as a sent record.

## Read, check, and search

- `check` groups recent inbox and sent records by chat and counts **incoming**
  unread messages only; outgoing replies never inflate `unread`.
- `read` takes a required `chat_id` and optional `limit` (default 10). It
  combines incoming and outgoing records, marks the returned records read, and
  clears the handled notification mirror. The reserved string `chat_id='updates'`
  is read-only and recovers synthetic non-chat updates.
- `search` takes a regex `query`, with optional `account` and `chat_id`. It
  searches stored inbound text, sender names/usernames, and update type. Invalid
  regex syntax is an error; search does not mark records read.
- Message records expose concise fields plus an additive `telegram` envelope
  containing the complete raw Bot API Update, branch, actor policy result, and
  unknown nested fields. Edited messages retain an append-only raw `edits`
  history; `event_id` remains the root identity and `current_event_id` tracks the
  latest applied edit.
- Non-message updates (reactions, polls, member/boost/business events,
  inline-only callbacks, and unknown branches) are stored in the synthetic
  `updates` conversation with `synthetic=true`. They are never valid outbound
  targets. Use the raw envelope when a concise preview omits a field.

## Inbound media

Inbound photos/documents/voice/audio include downloaded metadata and, when
available, an absolute local `path` under the agent's Telegram inbox. Voice
messages may also carry a local Whisper `voice_transcript`; treat that as
additive to the original attachment. Use the `vision` capability to inspect
image-like attachments; do not infer contents from a filename. If
`download_error` is present, metadata is retained without a path: read the text
and ask the user to resend or use another transfer method.
The hosted Telegram Bot API limits `getFile` downloads to 20 MB; this addon does
not configure a local Bot API server.

## Slash-command menu

Telegram's `/` picker and runtime command handling are separate. The optional
per-account `commands` config list registers menu entries via `setMyCommands`;
command names omit the leading slash and registration does not create a local
handler. Built-in local handlers include `/help`, `/status`, `/kanban`,
`/system`, `/refresh`, `/sleep`, `/clear`, and `/taskcard`; other slash commands
pass through as ordinary inbound messages for the host agent.

`/taskcard`, `/taskcard on|off`, `/taskcard N`, and `/taskcard lang en|zh` are
local preference operations. `commands: []` clears the menu; omitted or `null`
uses the built-in menu. Configuration edits and refresh/restart are setup
operations, not MCP tool calls. Never print or place a bot token in this manual,
chat, logs, or generated examples.

## SETTINGS SHOW

Call `telegram(action='settings', input={}, reasoning='inspect Telegram settings')`
for read-only progressive disclosure. Success is exactly `{"settings": [...]}`;
each row has `key`, `current`, `default`, `configurable`, and a `comment`.
There is no set/reset form. Make an authorized change through the existing
launcher, private config, File/Shell, or `/taskcard` procedure named below, then
call `settings` again. If any current fact is unavailable, the complete action
returns one bounded `SETTINGS_UNAVAILABLE` failure with no partial rows. Account
and config authority values are redacted; contacts, read markers, message
records, update offsets, and resident routes are operational state, not settings.

### Telegram config path

`config.path` is the successfully resolved `LINGTAI_TELEGRAM_CONFIG` path captured
at startup. Relative values resolve against `LINGTAI_AGENT_DIR` (or process cwd
when absent). SHOW redacts the path. Change it only through the authorized
launcher/config procedure and restart or refresh the curated MCP.

### Account aliases

`accounts.aliases` is the live service-order snapshot of `accounts[].alias`
values. It is redacted because aliases bind account state and compound IDs.
Change aliases only in the existing private account JSON, preserving credentials
and policy, then restart/refresh and verify with SHOW plus `accounts`.

### Bot tokens

`accounts.bot_tokens` is the aggregate of `accounts[].bot_token` credentials.
Both values are redacted in SHOW. Rotate a token through BotFather and the
private JSON, preserve file permissions, restart/refresh, and verify through the
established account/status path. Never put a token in chat, logs, tests,
examples, or a settings response.

### Allowed users

`accounts.allowed_users` is the aggregate allow-list. Omitted, `null`, and `[]`
all mean unrestricted admission; current and default remain redacted because the
IDs identify authorized humans. A saved contact does not alter this list.

### Account poll intervals

`accounts.poll_intervals` snapshots each account's `poll_interval`, defaulting to
`1.0` when omitted. The service preserves the configured value as-is; it adds no
validation that runtime does not enforce. The value is account authority and is
redacted in SHOW.

### Slash-command menu

`accounts.commands` snapshots each account's optional command menu. Omitted or
`null` means the built-in menu and `[]` means clear it. Other values contain
Telegram-compatible `{command, description}` objects and are applied
best-effort at startup. The aggregate is redacted; verify with SHOW, the `/`
picker, or the safe status resource.

### Task Card poll interval

`automatic.poll_interval_seconds` is the manager's import-time
`LINGTAI_TASKCARD_POLL_INTERVAL` snapshot, default `5.0` seconds. It governs
automatic journal tailing, programmable artifact polling, and resident edit
throttling. The loader uses plain `float()`; non-finite values make the
all-or-nothing settings response unavailable. Change the launcher environment
and fully restart the MCP.

### Task Card delivery

`automatic.enabled` is the agent-wide `taskcard` boolean in
`<workdir>/telegram/taskcard.json`, defaulting to `true`. It gates presentation
of both automatic and programmable slots without stopping their mechanics. Use
`/taskcard on|off`, then verify with `/taskcard` and SHOW.

### Task Card normal rows

`automatic.normal_rows` is the rolling API-call-group window, default `1`,
accepted range `1..10`; it is not a count of tool rows. Use `/taskcard N` and
verify with SHOW. The compatibility `max_refreshes` field is not an active
Telegram runtime ceiling.

### Task Card locale

`automatic.locale` is the projection locale, `en` by default, with `en` and `zh`
as the accepted values. Use `/taskcard lang en|zh` and verify with SHOW.

### Task Card display expression

`automatic.display_expression` is an allowlisted ordered list in
`<workdir>/telegram/taskcard.json`. The default is
`["footer","header","rows","blank","divider","metadata","time","ask_agent"]`;
a custom nonempty list has at most 32 entries drawn from
`header`, `rows`, `blank`, `footer`, `divider`, `metadata`, `time`, and
`ask_agent`. Invalid values fall back wholesale to the default. There is no
slash-command editor; use an authorized atomic File/Shell edit preserving
sibling fields, then verify the effective list with SHOW.

## Task Card: two distinct surfaces

Telegram's automatic Task Card is a bounded, mechanical projection of safe
public `diary` and `tool_call` events from the agent's durable event history. It
is not a turn-local heartbeat or completion lifecycle. It omits hidden thinking,
raw arguments/results, prompts, credentials, paths, and other private
diagnostics. The rolling `normal_rows` window counts API-call groups. Delivery
of both automatic and programmable slots is governed by `taskcard: True|False`;
turning it off suppresses presentation while mechanics continue.

The public programmable `task_card` tool is intrinsic and channel-neutral. Read
[`../../tools/task_card/manual/SKILL.md`](../../tools/task_card/manual/SKILL.md)
before authoring or operating a watcher. Telegram does not own that tool, does
not run its renderer, and does not accept Task Card JSON/controller instructions.
Telegram only reads `taskcard/status` and `taskcard/taskcard.md`: exact `active`
with a nonempty body projects a programmable frame; exact `inactive`
idempotently excludes only that frame. Missing/unreadable status, active with a
missing/blank body, other status text, or unchanged bytes is a no-op. Telegram
never rewrites producer files. Projection details for this retained adapter are
in [`task_card/SKILL.md`](task_card/SKILL.md) and
[`task_card/CONTRACT.md`](task_card/CONTRACT.md).

A changed programmable body still causes a real Telegram edit/send and therefore
consumes provider quota. Diff-only skipping protects unchanged bytes, not a
churning renderer. Read [`reference/rate-limits/SKILL.md`](reference/rate-limits/SKILL.md)
before changing cadence or recovery; it owns published quotas and
`retry_after` semantics.

## Error and rate-limit handling

Treat every result as data: inspect `error`, `status`, and action-specific fields.
The addon does not schedule hidden retries. HTTP 429 returns `status='error'`,
`error_code=429`, and `auto_retry=false`; when Telegram supplies a valid
nonnegative `retry_after`, the result adds `retryable=true` and the seconds to
wait before a new action. Missing or malformed cooldown metadata is omitted,
not guessed. Do not send a second Telegram notice through the rate-limited route.

Read [`reference/rate-limits/SKILL.md`](reference/rate-limits/SKILL.md) for the
official quota facts, undocumented scope, and safe client policy. A duplicate
send is `status='blocked'`, not a reason to replay. A media download failure is
reported on the inbound record and does not trigger an automatic reply.
