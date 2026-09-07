---
name: wechat-mcp-manual
description: |
  Progressive-disclosure usage manual for the WeChat MCP tool. Read this when you
  need detail beyond the resident action catalog: safe send/reply operations,
  inbound and outbound media, QR login, settings, account ownership, or poller
  recovery. Pulled on demand via action='manual'; it is not required before every
  send.
version: 2.0.0
last_changed_at: "2026-09-07T00:00:00Z"
related_files:
- src/lingtai/mcp_servers/ANATOMY.md
- src/lingtai/mcp_servers/wechat/manager.py
- src/lingtai/mcp_servers/wechat/server.py
- src/lingtai/mcp_servers/wechat/_family.py
- src/lingtai/mcp_servers/wechat/plugin.py
- src/lingtai/mcp_servers/wechat/settings.py
- src/lingtai/mcp_servers/wechat/api.py
- src/lingtai/mcp_servers/wechat/media.py
- src/lingtai/mcp_servers/wechat/login.py
- src/lingtai/mcp_servers/wechat/lockfile.py
- src/lingtai/mcp_servers/wechat/reference/operations.md
- src/lingtai/mcp_servers/wechat/reference/media.md
- src/lingtai/mcp_servers/wechat/reference/setup.md
- tests/test_wechat_toolfamily_ltpv2.py
- tests/test_wechat_media_validation.py
- tests/test_wechat_login.py
- tests/test_wechat_settings.py
- tests/test_wechat_config_resolution.py
- ENVIRONMENT_VARIABLES.md
maintenance: |
  Tracks the WeChat MCP action catalog and its progressive-disclosure safety,
  media, and setup routes; update this router and the owning references when the
  provider behavior, setup boundary, settings projection, or public actions change.
---

# WeChat MCP — usage manual (progressive disclosure)

The `wechat` MCP exposes one strict LTP-v2 tool. The `manual` action returns this
file on demand; the resident description and action catalog are intentionally
short so ordinary tool calls do not load operational detail.

## Public family and first-call boundaries

The root envelope is closed `{action, input, reasoning, summarize?}`. `action`,
`input`, and `reasoning` are required. `input` is a closed object owned by the
selected action; host-only fields, aliases, and legacy flat arguments are not
accepted. The exact public actions are listed below; `settings` is immediately
before `manual`.

- `send` and `reply` cause real external side effects. Verify the recipient and
  content first. A provider-accepted request is not proof of delivery and must
  not be automatically replayed.
- Use a `user_id` returned by `check`, `read`, or `contacts`; do not invent a
  recipient. `reply` needs the `message_id` of an inbound message from `read`.
- `media_path` is an outbound file operation. Keep the file in the agent's
  allowed working area; text and media can produce a partial outcome.
- Login and configuration are owner/setup procedures, not messaging actions.
  Never print, paste, or share the admin login QR, bot token, or credentials.
- iLink `getUpdates` has one consumer per bot account. The MCP holds a per-account
  poller lock and refuses a second poller; after a refresh or recovery, reconcile
  with `read` before replying so a replay cannot cause a duplicate response.
- `settings` is SHOW-only and `manual` is the route to this guide. Read the
  focused reference only when its topic is needed.

## Settings anchors

The settings provider's `comment` values point to these stable anchors. The
setup reference owns the detail; SHOW remains read-only.

## setting-config-path

See [`reference/setup.md#setting-config-path`](reference/setup.md#setting-config-path).

## setting-base-url

See [`reference/setup.md#setting-base-url`](reference/setup.md#setting-base-url).

## setting-poll-interval

See [`reference/setup.md#setting-poll-interval`](reference/setup.md#setting-poll-interval).

## setting-allowed-users

See [`reference/setup.md#setting-allowed-users`](reference/setup.md#setting-allowed-users).

## setting-bot-token

See [`reference/setup.md#setting-bot-token`](reference/setup.md#setting-bot-token).

## setting-user-id

See [`reference/setup.md#setting-user-id`](reference/setup.md#setting-user-id).

## Resident action catalog

| Action | Input and purpose |
|---|---|
| `send` | `user_id` plus `text` and/or `media_path`; deliver a new message |
| `check` | `{}`; list conversations and unread counts |
| `read` | `user_id`, optional `limit`; read merged inbox/sent history |
| `reply` | `message_id` plus `text`; respond to a specific inbound message |
| `search` | `query`, optional `user_id`; regex-search inbox messages |
| `contacts` | `{}`; list saved contacts |
| `add_contact` | `user_id` plus `alias`; save a local contact alias |
| `remove_contact` | `alias` or `user_id`; remove a saved contact |
| `accounts` | `{}`; list configured account details |
| `settings` | `{}`; show the read-only startup configuration inventory |
| `manual` | `{}`; return this progressive-disclosure guide |

All calls still require the root `reasoning` string. Exact schemas, required
fields, nullability, and validation remain owned by the advertised tool schema.

## Focused references

| Need | Read |
|---|---|
| Send/reply, reading, contacts, account results, errors, and replay-safe operations | [`reference/operations.md`](reference/operations.md) |
| Outbound attachments, inbound files, magic-byte checks, and partial media delivery | [`reference/media.md`](reference/media.md) |
| QR login, config/credentials, poller startup, settings SHOW, and recovery | [`reference/setup.md`](reference/setup.md) |

These sidecars ship with the package but are not embedded in the `manual` result.
Use the relative links above; do not infer setup fields or provider behavior from
memory. Human-facing addon registration and activation remain the host's
`mcp-manual` procedure, not this provider manual.
