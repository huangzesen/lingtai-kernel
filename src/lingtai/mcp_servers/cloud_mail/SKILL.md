---
name: cloud-mail-mcp-manual
description: |
  Progressive-disclosure usage manual for Cloud Mail REST email actions. Read
  it when action filters/ids, setup/authentication, polling/settings, or
  side-effect detail is needed. Returned by action='manual'; nested references
  are packaged but not embedded. Calls use the strict LTP-v2 envelope.
version: 1.3.0
last_changed_at: "2026-09-07T00:00:00Z"
related_files:
- src/lingtai/mcp_servers/ANATOMY.md
- src/lingtai/mcp_servers/cloud_mail/_family.py
- src/lingtai/mcp_servers/cloud_mail/client.py
- src/lingtai/mcp_servers/cloud_mail/manager.py
- src/lingtai/mcp_servers/cloud_mail/plugin.py
- src/lingtai/mcp_servers/cloud_mail/server.py
- src/lingtai/mcp_servers/cloud_mail/settings.py
- src/lingtai/mcp_servers/cloud_mail/reference/actions.md
- src/lingtai/mcp_servers/cloud_mail/reference/setup.md
- tests/test_cloud_mail_addon.py
- tests/test_cloud_mail_curated_mcp_plugin.py
- tests/test_cloud_mail_toolfamily_ltpv2.py
maintenance: |
  Tracks the Cloud Mail MCP's resident action contract and its routed action and
  setup references. Update this router and the nested references when the
  manager, settings provider, launcher, or public action surface changes.
---

# Cloud Mail MCP — usage manual (progressive disclosure)

Cloud Mail is a REST client for a self-hosted Cloud Mail deployment, not IMAP or
SMTP. Inbound mail is polled automatically and delivered to the host agent's
inbox via LICC; you normally do not poll `check` just to receive notifications.
This file is the model-facing router. Load only the packaged reference that
matches the question:

| Need | Read |
|---|---|
| Action inputs/results, compound ids, filters, content, and side effects | [`reference/actions.md`](reference/actions.md) |
| Config shape, authentication, polling/watermarks, settings, and startup diagnosis | [`reference/setup.md`](reference/setup.md) |

The references are packaged sidecars and are not embedded in the `manual`
action result. They contain operational depth without making every ordinary
call carry it.

## HOW TO CALL IT — the envelope

`cloud_mail` is one strict LTP-v2 tool family. Every call has the closed root
`{action, input, reasoning, summarize?}`: `action`, `input`, and `reasoning` are
required; `summarize` is optional, root-level, and never nested under `input`.
`input` accepts only the branch for the selected action, and validation rejects
unknown or cross-action keys before manager I/O. The actions are exactly
`check`, `search`, `read`, `send`, `accounts`, `add_user`, `settings`, and
`manual`; `settings` is immediately before `manual`. Do not use a flat/legacy
shape, `_reasoning`, aliases, or a generic dispatcher.

```python
cloud_mail(action="check", input={"limit": 10}, reasoning="check recent mail")
cloud_mail(action="read", input={"id": "cloudmail:1234"}, reasoning="read this mail")
cloud_mail(action="send", input={"address": "user@example.com", "message": "done"},
           reasoning="report completion")
cloud_mail(action="settings", input={}, reasoning="inspect owner settings")
```

## ACTIONS — first-call inventory

- **`check`** lists recent inbound mail. Optional input is `account`, `limit`,
  `to_email`, `send_email`, `subject`, `time_sort`, and `type`.
- **`search`** filters the public email list. Optional input is `account`,
  `to_email`, `send_email`, `send_name`, `subject`, `content`, `time_sort`,
  `num`, `size`, `type`, and `is_del`. Use the returned ids; filters are LIKE
  matches.
- **`read`** returns one full email. Use a returned compound `id` in the form
  `<account>:<emailId>`, or provide `account` with numeric/string `email_id`.
- **`send`** requires `address` (a recipient string or list). Supply plain text
  through `message`/`text` and/or HTML through `html`/`content_html`; optional
  fields are `account`, `subject`, `name`, and `send_account_id`. User
  credentials are required and attachments are not supported.
- **`accounts`** returns redacted per-account status; it does not return
  passwords or tokens.
- **`add_user`** requires `email` and `password`, with optional `account` and
  `role_name`; it is an admin operation that changes the Cloud Mail user set.
- **`settings`** is a strict-empty, read-only startup inventory. **`manual`**
  returns this packaged guide and its metadata on demand.

`check`, `search`, and `read` can return bulky listings or full bodies. Keep
exact ids and body text when a later action depends on them: this family does
not currently promise result summarization. Read `manual` exactly so its
procedure and constraints remain available.

## SETTINGS SHOW

`settings` accepts exactly `input={}` and returns exactly two rows, in order:
`config_path`, then `accounts`. Each successful row has only `key`, `current`,
`default`, `configurable`, and `comment`. Both rows are sensitive and render
`<redacted>` for `current` and `default`; there is no set/reset or other mutation operation.
`configurable=true` does not grant this action write authority. Follow [`reference/setup.md`](reference/setup.md) and the shared
`mcp-manual` curated-addon procedure for authorized owner changes, then perform
a full Cloud Mail relaunch and call SHOW again.

### Config path

`config_path` is the exact resolved path successfully loaded at startup from
`LINGTAI_CLOUD_MAIL_CONFIG`. It is an applied snapshot, not a fresh environment
reread, and is fully redacted because it can reveal private machine layout.

### Accounts document

`accounts` is only an opaque `configured` marker for the document selected by
`config_path`; the settings provider never traverses or projects account
records. It is fully redacted. Missing startup truth fails the whole inventory
with the fixed `SETTINGS_UNAVAILABLE` result; no partial rows or startup
exception are returned, and `manual` remains available.

## SAFETY & RESULTS

- `send` delivers real email to real recipients: confirm recipients and body
  before this external, hard-to-undo side effect.
- `add_user` mutates the Cloud Mail deployment's user set; double-check the
  account, email, and role first.
- Successful business actions return `status: "ok"`; provider or business
  failures return `status: "error"` with an error message, while envelope,
  settings, or dispatch validation failures use `status: "failed"`. Inspect the
  status and error fields; do not assume delivery.
- Setup fields, credential handling, watermark state, and retry/startup
  diagnosis are intentionally in [`reference/setup.md`](reference/setup.md),
  not repeated in this resident router.
