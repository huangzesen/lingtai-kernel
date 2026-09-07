---
related_files:
- src/lingtai/mcp_servers/ANATOMY.md
- src/lingtai/mcp_servers/cloud_mail/SKILL.md
- src/lingtai/mcp_servers/cloud_mail/_family.py
- src/lingtai/mcp_servers/cloud_mail/client.py
- src/lingtai/mcp_servers/cloud_mail/manager.py
- tests/test_cloud_mail_addon.py
- tests/test_cloud_mail_toolfamily_ltpv2.py
maintenance: |
  Tracks Cloud Mail action inputs, results, and side-effect guidance; update it
  with the manager, family schema, client, and focused behavior tests.
---
# Cloud Mail actions and safety

This reference expands the action inventory in
[`../SKILL.md`](../SKILL.md). It is model-facing operational guidance, not a
second schema: the strict family schema and dispatch validator remain
authoritative.

## Reading mail

- **`check`** lists recent inbound rows. It accepts an optional account alias
  (or admin email), limit, recipient `to_email`, sender `send_email`, subject,
  `time_sort` (`asc` or `desc`), and integer `type`.
- **`search`** uses the public email-list endpoint. Its optional filters are
  account, `to_email`, `send_email`, `send_name`, `subject`, `content`,
  `time_sort`, `num`, `size`, `type`, and `is_del`. Text filters are LIKE
  matches. `num` is the page number and `size` is the page size. The strict
  `check` branch has no `n` alias; use `limit` there.
- **`read`** returns the full content for one email. Prefer an id returned by
  `check` or `search`, formatted as `<account>:<emailId>`. The alternative is
  `account` plus `email_id` (string or integer). The manager searches a bounded
  window of recent pages, so an older message can be reported as not found.

Account is optional for list/read/send/add-user actions and defaults to the
first configured account. An explicit alias (or configured admin email) selects
one account; an unknown account is an error.

## Sending and administration

- **`send`** needs user credentials (`user_email` and `user_password`) in the
  selected account. `send_account_id` can override the configured sender id;
  one must be available. `address` is one recipient string or a list. Use
  `message` or `text` for plain text and `html` or `content_html` for HTML;
  `subject` and sender display `name` are optional. Attachments are not
  supported in this first pass, so omit them.
- **`accounts`** returns per-account operational status without tokens or
  passwords. Treat endpoint, identity, sender allowlist, and polling details as
  configuration data, not as credentials to repeat in reports.
- **`add_user`** requires the new user's `email` and `password`. `role_name` is
  optional. The call uses admin authorization, changes the Cloud Mail
  deployment's user set, and never echoes the password.

`send` delivers real email to real recipients and is an external, hard-to-undo
side effect. Confirm the selected account, recipient list, subject, and body
before calling it. `add_user` is also a real deployment mutation; confirm the
new account and role before calling it. Neither action is a dry run.

## Results and result size

Successful manager actions use `status: "ok"`; family or settings validation
failures use `status: "failed"`, while provider or transport failures use an
error result. Inspect status and the error text rather than assuming a send or
administrative mutation succeeded.

`check`, `search`, and `read` can return bulky listings or full bodies. Keep
exact ids, addresses, and verbatim body text whenever later actions depend on
them. Cloud Mail does not currently promise family-specific result
summarization; `send`, `accounts`, `add_user`, and `settings` results are short
and should be read exactly.

## Inbound delivery

Polling delivers new mail separately through LICC; an automatically delivered
inbox event is not proof that an outbound send succeeded. For inbound mail,
use the compound id in the event metadata or in a later `check`/`search` result
when a full body is needed. See [`setup.md`](setup.md) for polling, watermark,
and configuration details.
