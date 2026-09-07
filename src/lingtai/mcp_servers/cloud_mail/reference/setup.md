---
related_files:
- ENVIRONMENT_VARIABLES.md
- src/lingtai/mcp_servers/ANATOMY.md
- src/lingtai/mcp_servers/cloud_mail/SKILL.md
- src/lingtai/mcp_servers/cloud_mail/_watermark.py
- src/lingtai/mcp_servers/cloud_mail/client.py
- src/lingtai/mcp_servers/cloud_mail/manager.py
- src/lingtai/mcp_servers/cloud_mail/server.py
- src/lingtai/mcp_servers/cloud_mail/settings.py
- tests/test_cloud_mail_addon.py
- tests/test_cloud_mail_toolfamily_ltpv2.py
maintenance: |
  Tracks Cloud Mail configuration, authentication, polling/watermark state, and
  startup/settings diagnosis; update it with the server, manager, client,
  watermark, settings provider, and focused tests.
---
# Cloud Mail setup, authentication, and startup diagnosis

This reference expands the setup route in [`../SKILL.md`](../SKILL.md). For the
kernel-level curated-addon activation sequence, consult the `mcp-manual`
curated-addons reference; do not invent a launcher or configuration flow here.

## Provider and activation boundary

Cloud Mail is a REST client for a self-hosted Cloud Mail deployment (Cloudflare
Workers). It is not IMAP/SMTP. The curated addon is activated by the registry
name `cloud_mail`; the package entry point is `lingtai.mcp_servers.cloud_mail`.
The addon's sole configuration reference, with no fallback source, is the
non-empty `LINGTAI_CLOUD_MAIL_CONFIG` environment value. A relative value
resolves against `LINGTAI_AGENT_DIR` or the process working directory, and `~`
expands. The configuration is read at startup; a changed value is not live until
a full Cloud Mail relaunch.

Use the existing curated-addon owner procedure to place this environment value
in the activation's `env` and to create a private JSON document. Do not add
launcher-shaped `command`, `args`, or `type` fields for a curated addon. Keep
credentials out of examples, reports, and logs.

## Configuration shape

The canonical outer shape is a non-empty `accounts` list. A retained flat
single-account object with `base_url` is also accepted by the loader. Each
account requires `base_url`; a missing or unreadable file, invalid JSON, empty
accounts list, or missing base URL prevents manager startup. The account alias
falls back to `admin_email`, then `base_url`, and an alias or admin email can be
used for action-level account selection.

Relevant account fields are:

| Field | Use |
|---|---|
| `alias` | Stable account selector and watermark namespace. |
| `base_url` | Cloud Mail REST deployment URL; required. |
| `admin_email`, `admin_password` | Public API token acquisition for list/read/search, polling, and `add_user`. |
| `user_email`, `user_password` | Optional user login used by `send`. |
| `send_account_id` | Configured sender id used by `send` when not supplied in the action. |
| `allowed_senders` | Optional case-insensitive inbound sender allowlist. |
| `poll_interval` | Poll cadence in seconds; use a positive finite number. |
| `notify_existing` | When true, deliver existing rows on the first poll instead of silently seeding. |

`notify_existing` is interpreted by the current parser as a truthy value, so use
a JSON boolean. The parser converts `poll_interval` to a float but does not add a
strong rejection for non-positive or non-finite values; use positive finite input
as the safe operator contract. A supplied `allowed_senders` value should be a
JSON list of strings; a string is currently iterated character-by-character, and
an absent or falsey value permits all senders. These are operator constraints
from the current implementation, not additional schema aliases.

## Authentication and request behavior

List/search/read/poll and `add_user` use an admin-minted public token from
`POST /public/genToken`; the token is sent as the raw `Authorization` value,
not as `Bearer ...`. `send` logs in with the configured user credentials via
`POST /login` and sends with the returned raw user JWT. For authenticated
requests, the client checks HTTP status before parsing the response body and
refreshes the cached token once after HTTP 401/403. After that retry opportunity,
a non-JSON response or a decoded envelope whose `code` is not 200 becomes a
`CloudMailError`; a non-200 envelope code alone does not trigger token refresh.
Transport exceptions are surfaced as bounded manager error results with their
native `error_type`; neither path should log credentials or full auth headers.

User credentials are optional: public read/search/check/poll actions can work
without them, while `send` returns a clear error. Admin credentials remain
needed for public endpoints and `add_user`.

## Polling, LICC, and watermark state

Each configured account has a polling thread; the first tick runs immediately.
The poll reads a bounded recent page and compares integer `emailId` values with
the per-account high-water mark. On a fresh run, the current high-water mark is
recorded silently unless `notify_existing` is true. There is no historical-mail
flood by default.

New rows are delivered oldest-first through one LICC inbox event per row. An
`allowed_senders` rejection advances the watermark without delivery. If an
allowed row cannot be delivered, advancement stops at that row so the next poll
can retry it before later rows. The event includes a safe compound id of the
form `<alias>:<emailId>` for a later `read` call.

Watermark state is stored per account under the agent working directory in
`cloud_mail/<alias>/watermark.json` with `last_email_id` and `seeded`. Writes use
an atomic replacement. A missing or corrupt state document is treated as empty,
so the next poll reseeds according to the rules above. Polling failures stay in
server diagnostics; they do not turn `manual` into an unavailable action.

## Settings and startup diagnosis

The `settings` action is SHOW-only and reads the manager's successfully loaded
startup snapshot. It never rereads the environment or traverses account
credentials. Both `config_path` and the opaque `accounts` marker are sensitive
and fully redacted. A missing manager or config path returns the fixed
`SETTINGS_UNAVAILABLE` no-row result; it does not expose an exception, path, or
partial inventory. After an authorized config change, perform a full Cloud Mail
relaunch, then call `cloud_mail(action="settings", input={}, reasoning="verify")`.

`LINGTAI_AGENT_DIR` and `LINGTAI_MCP_NAME` identify the launcher and callback
context; they are not Cloud Mail preference rows. `manual` is still available
when eager manager startup fails. Use the existing owner lifecycle and read-only
SHOW result to distinguish a configuration/startup problem from provider
connectivity or action-level errors.
