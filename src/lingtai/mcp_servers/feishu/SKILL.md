---
name: feishu-mcp-manual
description: |
  Concise progressive-disclosure entry point for the Feishu (Lark) MCP tool.
  It covers the strict action envelope, action inventory, recipient/reply and
  card safety boundaries, read-only settings, and the relative route to the
  deep message-semantics reference. Pull the full usage guidance with
  action='manual'; you do not need to call it before every send.
version: 1.17.0
last_changed_at: 2026-09-07T00:00:00Z
related_files:
- ENVIRONMENT_VARIABLES.md
- src/lingtai/mcp_servers/ANATOMY.md
- src/lingtai/mcp_servers/feishu/account.py
- src/lingtai/mcp_servers/feishu/manager.py
- src/lingtai/mcp_servers/feishu/server.py
- src/lingtai/mcp_servers/feishu/service.py
- src/lingtai/mcp_servers/feishu/settings.py
- src/lingtai/mcp_servers/feishu/control_cards.py
- src/lingtai/mcp_servers/local_commands/core.py
- src/lingtai/mcp_servers/feishu/task_card.py
- src/lingtai/mcp_servers/task_card/event_projection.py
- src/lingtai/mcp_servers/task_card/resident.py
- src/lingtai/mcp_servers/feishu/_family.py
- src/lingtai/mcp_servers/feishu/_errors.py
- src/lingtai/mcp_servers/feishu/reference/setup.md
- src/lingtai/mcp_servers/feishu/reference/diagnostics.md
- src/lingtai/mcp_servers/feishu/reference/capability-matrix.md
- src/lingtai/mcp_servers/feishu/reference/message-semantics.md
- tests/test_feishu_settings.py
maintenance: |
  Tracks the Feishu MCP's concise model-facing router, strict action surface,
  and relative manual references; keep deep message semantics in the linked
  sidecar and update both when the server or settings contract changes.
---

# Feishu (Lark) MCP — usage manual (progressive disclosure)

This is the concise model-facing entry point returned by `action='manual'`.
Use it for the first-call contract and follow the relative references below
only when the question needs operational depth. The deep companion is
[`reference/message-semantics.md`](reference/message-semantics.md); setup,
rollout, and symptom-led diagnosis remain separate operator references.

## OPERATOR REFERENCES

| Need | Read |
|---|---|
| App permissions, event/card callback setup, complete config fields, multi-account behavior, canary, acceptance, rollback | [`reference/setup.md`](reference/setup.md) |
| Safe status interpretation and symptom-based startup, WebSocket, admission, media, card, reaction, Task Card, refresh, and error diagnosis | [`reference/diagnostics.md`](reference/diagnostics.md) |
| Feishu v1 vs Telegram coverage, action/content inventory, and explicit non-goals | [`reference/capability-matrix.md`](reference/capability-matrix.md) |
| Detailed Agent-facing message, card, notification, Task Card, settings, and failure semantics | [`reference/message-semantics.md`](reference/message-semantics.md) |

The sidecars are packaged with LingTai but are not embedded into the
`action='manual'` result. Follow only the reference that answers the current
question; do not load all of them for an ordinary message send.

## FIRST-CALL CONTRACT

Every call uses the strict root shape
`{action, input, reasoning, summarize?}`. `action`, `input`, and `reasoning`
are required; `input` is a closed object owned by the selected action;
`summarize`, when present, is boolean. Unknown root fields, cross-action input
fields, and the retired flat/`_reasoning` shape are rejected before provider
I/O. The complete action inventory is:

| Action | First-call shape and purpose |
|---|---|
| `send` | Fresh message: `receive_id` and exactly one of `text`/`content`; `receive_id_type` (default `open_id`), `account`, and `placeholder` are optional. |
| `check` | Recent conversations and unread counts; optional `account`. |
| `read` | Messages from one chat: `chat_id`; optional `account` and `limit`. |
| `reply` | Reply to an exact compound `message_id` with exactly one of `text`/`content`; optional `reply_in_thread`. |
| `react` | Add with `operation='add'` + `emoji_type`, or remove with `operation='remove'` + the exact `reaction_id`. |
| `search` | Regex search: `query`; optional `account` and `chat_id`. |
| `delete` | Delete the logical provider message identified by a returned compound `message_id`; use only an ID for a bot-authored sent message. |
| `edit` | Edit that logical provider message with exactly one of `text`/`content`; use only a bot-authored sent ID, and do not edit media messages. |
| `contacts` | List saved contacts; optional `account`. |
| `add_contact` | Save `open_id` + `alias`; optional `account`, `name`, and `chat_id`. |
| `remove_contact` | Remove saved contacts by `alias` or `open_id`; duplicate aliases can remove multiple entries. Optional `account`. |
| `accounts` | List configured app accounts and non-secret identity details. |
| `settings` | Read-only Feishu settings inventory; `input` must be `{}` and no write form exists. |
| `manual` | Strict-empty route to this packaged usage manual; follow its relative references for depth. |

`send` and `reply` content supports tagged text, Markdown, raw post, complete
schema-2.0 cards, media, shares, and stickers. `edit` supports text, Markdown,
post, or card replacement but not media. The adapter does not independently
prove edit/delete authorship, so the caller must supply a bot-authored sent ID. See the deep companion for exact tagged shapes,
chunking, callbacks, and inbound projections.

## RECIPIENTS AND SAFETY BOUNDARIES

- `receive_id_type` defaults to `open_id`; use `chat_id` for a group. The
  omitted `account` uses the first configured account. Compound IDs are
  `{account_alias}:{chat_id}:{feishu_message_id}` and must be passed back
  verbatim.
- `reply` follows the target's topic/thread by default and a gone target fails;
  it never silently becomes a fresh `send`. Group/topic inbound messages need
  an explicit Bot mention; `allowed_users`, when configured, still gates the
  sender. Saving a contact does not grant admission.
- Complete schema-2.0 business cards may generate authorized `card_action`
  records. Local control-card callbacks are separate: they update the control
  card, do not become business inbox records, and do not wake the Agent.
- Automatic and programmable resident Task Cards are mechanical channel
  projections, not messages to manage with the public `feishu` actions.
  `placeholder=true` progress cards are separate from those residents and from
  the final durable answer.
- `send`, `reply`, `edit`, `delete`, and `react` are real external side effects;
  confirm the recipient, target, content, and operation before acting. Feishu
  makes one outbound attempt per chunk and never hides an automatic retry.
  Failures retain classified `error_code`, `retryable`, and
  `retry_after_seconds` guidance; do not replay delivered chunks blindly.

## SETTINGS SHOW

Call `action='settings', input={}, reasoning='inspect Feishu settings'` for the
read-only inventory. Each row contains only `key`, `current`, `default`,
`configurable`, and a manual pointer in `comment`. SHOW never writes and fails
as one bounded no-row result when applied truth is unavailable. The detailed
owner semantics remain in the deep companion:

### Setting config path

See [`reference/message-semantics.md#setting-config-path`](reference/message-semantics.md#setting-config-path).

### Setting account aliases

See [`reference/message-semantics.md#setting-account-aliases`](reference/message-semantics.md#setting-account-aliases).

### Setting account app ids

See [`reference/message-semantics.md#setting-account-app-ids`](reference/message-semantics.md#setting-account-app-ids).

### Setting account app secrets

See [`reference/message-semantics.md#setting-account-app-secrets`](reference/message-semantics.md#setting-account-app-secrets).

### Setting account allowed users

See [`reference/message-semantics.md#setting-account-allowed-users`](reference/message-semantics.md#setting-account-allowed-users).

### Setting task card enabled

See [`reference/message-semantics.md#setting-task-card-enabled`](reference/message-semantics.md#setting-task-card-enabled).

### Setting task card normal rows

See [`reference/message-semantics.md#setting-task-card-normal-rows`](reference/message-semantics.md#setting-task-card-normal-rows).

## WHERE TO GO NEXT

- For exact send/reply/edit content unions, media source ownership, message
  reads, notifications, reactions, cards, and Task Cards, load
  [`reference/message-semantics.md`](reference/message-semantics.md).
- For app permissions, account fields, event delivery, canary, or rollback,
  load [`reference/setup.md`](reference/setup.md).
- For a failure symptom, load [`reference/diagnostics.md`](reference/diagnostics.md)
  before retrying a provider side effect.
- For Feishu-vs-Telegram scope or deliberate non-goals, load
  [`reference/capability-matrix.md`](reference/capability-matrix.md).
