---
name: wechat-setup-reference
description: |
  Focused WeChat setup and recovery reference: config/credentials resolution,
  QR or headless login, read-only settings SHOW, per-account poller locking,
  session expiry, and safe owner procedures. Read before changing setup or
  diagnosing startup.
version: 1.0.0
last_changed_at: "2026-09-07T00:00:00Z"
related_files:
- src/lingtai/mcp_servers/wechat/SKILL.md
- src/lingtai/mcp_servers/wechat/server.py
- src/lingtai/mcp_servers/wechat/login.py
- src/lingtai/mcp_servers/wechat/lockfile.py
- src/lingtai/mcp_servers/wechat/settings.py
- src/lingtai/mcp_servers/wechat/api.py
- tests/test_wechat_config_resolution.py
- tests/test_wechat_login.py
- tests/test_wechat_settings.py
- ENVIRONMENT_VARIABLES.md
maintenance: |
  Tracks WeChat configuration, login, settings projection, and poller recovery
  guidance; update when owner procedures, path precedence, redaction, or startup
  behavior changes.
---

# WeChat setup and recovery

Registration and activation are host-owned. This reference covers the provider's
files, login, startup, and read-only settings after the host has selected the
curated WeChat addon.

## CONFIGURATION FILES

`LINGTAI_WECHAT_CONFIG` points to `config.json`; the required sibling
`credentials.json` is loaded from the same directory. An absolute config path is
used as-is. A relative path prefers `LINGTAI_AGENT_DIR`, then the legacy project
root for compatibility; without an agent directory, the process working directory
is used. Put new configuration under the agent directory and use the `settings`
action for redacted active verification; treat host status diagnostics as local
and do not paste them into public reports.

The authored `config.json` fields are:

## setting-config-path

`config_path` is the sensitive resolved `config.json` path captured at manager
startup. It is not an authored JSON field; `LINGTAI_WECHAT_CONFIG` is its source.

## setting-base-url

`base_url` is the endpoint fallback: a truthy `credentials.json.base_url` wins,
then this config value, then the provider default. `cdn_base_url` is retained for
configuration compatibility and is not a current upload-destination override when
the provider supplies its upload route.

## setting-poll-interval

`poll_interval` is converted with Python `float`; default `1.0`. Use a positive
finite value even though the loader does not reject every non-positive value.

## setting-allowed-users

`allowed_users` is an optional inbound sender allow-list. A missing, null, empty,
or other falsy value means unrestricted compatibility behavior; a truthy value is
used as the configured sender set.

`credentials.json` contains the login-produced `bot_token`, account `user_id`,
and optional effective `base_url`. Treat the file as secret material: do not
print, paste, commit, or include it in diagnostics. The login writer uses an
atomic replacement and mode `0600`.

## setting-bot-token

`bot_token` is the required secret written by QR login. It has no default and is
always redacted by SHOW; replace it only through the login/bootstrap procedure.

## setting-user-id

`user_id` is the required account identity written by QR login. It has no default
and is redacted by SHOW; it is distinct from recipient IDs used by actions.

## QR LOGIN

Use the existing owner bootstrap procedure, not a messaging action:

```text
lingtai-wechat-bootstrap <config-directory>
```

For a headless host, the same login core is available through the documented
`cli_login('<config-directory>')` entry point. It creates a default config when
needed, displays a QR in the terminal (or uses the browser bootstrap flow), polls
for confirmation, and writes sibling credentials on success. The QR authorizes
the backend account; it is an admin login QR, not a contact or group QR. Never
share it. After credentials are saved, restart or refresh the WeChat MCP through
the host owner procedure, then use `settings` to verify only the redacted active
snapshot.

If the session expires, rerun the login/bootstrap flow and restart the MCP. Do
not hand-edit a token or attempt to repair credentials through `settings`.

## SETTINGS SHOW

Call the strict-empty action:

```json
{"action":"settings","input":{},"reasoning":"inspect active WeChat settings"}
```

Success is exactly `{"settings":[...]}`; each row has only `key`, `current`,
`default`, `configurable`, and `comment`. The six keys are `config_path`, `base_url`,
`poll_interval`, `allowed_users`, `bot_token`, and `user_id`, in that order.
Sensitive rows redact both `current` and `default`; SHOW never writes, resets, or
rereads owner files, and any unavailable/invalid row fails the complete inventory
rather than returning a partial one. A `configurable` row describes the owner
procedure for the next manager construction, not a mutation input to this action.

Change the selected owner file or login credentials through the procedure above,
restart/refresh at the required boundary, and call SHOW again. Do not expose the
resolved config path, endpoint, allow-list, token, or account identity in chat or
public diagnostics; the redacted settings result is the safe verification view.

## ONE POLLER PER ACCOUNT

The iLink updates stream is single-consumer. The MCP takes an exclusive per-account
POSIX lock for the lifetime of the poller and fails startup with a clear error if
another process already holds it. A normal process exit releases the lock; do not
delete lockfiles blindly. Stop or reconfigure the other owner process, then
restart the intended MCP. On a platform without `fcntl`, startup fails rather
than pretending duplicate-poller safety exists.

When startup fails, inspect the redacted status/startup error and fix the owner
configuration or lock holder. Do not start a second poller as a workaround, and
reconcile `read` history before sending after recovery.
