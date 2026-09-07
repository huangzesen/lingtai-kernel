---
name: wechat-operations-reference
description: |
  Focused WeChat operation reference: recipient selection, send versus reply,
  bounded reads and search, contacts/accounts, result errors, and replay-safe
  handling of provider acceptance. Read from wechat-mcp-manual when operating the
  tool beyond its first-call safety rules.
version: 1.0.0
last_changed_at: "2026-09-07T00:00:00Z"
related_files:
- src/lingtai/mcp_servers/wechat/SKILL.md
- src/lingtai/mcp_servers/wechat/manager.py
- src/lingtai/mcp_servers/wechat/_family.py
- src/lingtai/mcp_servers/wechat/api.py
- tests/test_wechat_toolfamily_ltpv2.py
- tests/test_wechat_history_index.py
- tests/test_wechat_reply_read_state.py
- tests/test_wechat_inbound_replay.py
maintenance: |
  Tracks the WeChat manager's action/result semantics and replay-safe operating
  guidance; update when action dispatch, history views, acknowledgement handling,
  or local side effects change.
---

# WeChat operations

This is a focused reference for the `wechat` action family. The public envelope
and safety boundaries remain in the parent [`SKILL.md`](../SKILL.md); this file
only adds operational detail.

## SEND versus REPLY

- `send` starts a new message for the supplied `user_id`. It requires `text`,
  `media_path`, or both; the recipient ID is routing data, not a contact alias.
- `reply` takes an inbound `message_id` from `read`, resolves its original
  sender, and sends the supplied `text`. It fails when the message cannot be
  found or its sender cannot be determined; it does not silently become a new
  recipient-less send.
- A successful reply marks its target inbound message read. A failed send does
  not mark it read.
- Both actions deliver to real WeChat users. A successful result records
  provider acceptance, but `delivery_confirmed` remains false because the
  provider does not prove recipient delivery.

## CHECK, READ, AND SEARCH

- `check` returns recent conversation aggregates with `user_id`, optional saved
  alias, total count, unread count, latest preview metadata, and date. Unread
  counts concern inbound messages; outgoing records are included for context but
  are not unread messages.
- `read` requires `user_id` and accepts an optional `limit` (default 10). It
  returns the newest bounded view merged from inbox and sent records, labels
  outgoing records, and marks returned inbound records read.
- `search` requires a regular-expression `query`, optionally filtered by
  `user_id`. It searches inbox message bodies and returns at most 20 matches; an
  invalid regular expression is an action error.
- After a worker refresh, molt, or recovery, use `read` to reconcile the merged
  view before replying. Do not infer that an unread preview is a new message or
  that the absence of a search match proves no outgoing reply exists.

## CONTACTS AND ACCOUNTS

- `contacts` lists locally saved aliases. `add_contact` persists an alias for a
  `user_id`; `remove_contact` accepts either an existing alias or a user ID.
  These are local state changes, not proof that WeChat accepted a contact
  relationship remotely.
- `accounts` reports the configured account view. Treat account IDs and paths as
  potentially sensitive metadata; it never authorizes changing credentials.
  Login and credential replacement belong to the owner procedure in
  [`setup.md`](setup.md).

## RESULTS AND ERROR SURFACING

- Successful business actions return a result object. Failures use an `error`
  field (for example, missing identifiers, an unknown message, invalid regex, or
  an unreadable outbound file); surface that error instead of assuming success.
- Send acknowledgement accepts the provider's missing/null `ret` or integer
  zero only when `errcode` is absent or integer zero. Nonzero or invalid values
  remain failures. This is provider acceptance, not delivery confirmation.
- A text-plus-media send can return `status: partial` when text was accepted but
  the media stage failed. Its result carries `partial_delivery` for compatibility,
  precise provider-acceptance fields, and `automatic_retry_allowed: false`.
  Reconcile state before deciding what to do next; never replay the whole request
  automatically.

## INBOUND REPLAY AND POLLER STATE

The manager persists cursor progress and bounded stable inbound signatures so a
stale cursor after a worker failure does not normally create a second local
message. This is a replay guard, not permission to send twice. Reply at most once
per inbound `message_id`, and reread the merged history after a refresh before
performing another external side effect.
