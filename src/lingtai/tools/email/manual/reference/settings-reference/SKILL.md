---
name: email-manual-settings-reference
description: >
  Focused Email settings reference for the five SHOW rows, installed limits,
  manifest subscription source, redaction, precedence, timing, and the
  read-only boundary. Read after email-manual when interpreting settings rows.
version: 1.0.0
tags: [lingtai, email, settings, privacy, configuration]
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/email/manual/SKILL.md
- src/lingtai/tools/email/settings.py
- src/lingtai/tools/email/manager.py
- src/lingtai/tools/email/primitives.py
- src/lingtai/adapters/posix/mail.py
- src/lingtai/tools/email/CONTRACT.md
maintenance: |
  Tracks Email's SHOW-only settings rows, source truth, privacy redaction, and authorized configuration timing; update when settings ownership or constants change.
---

# Email settings reference

Call `email(action="settings", input={}, reasoning="inventory Email policy")` to
show the effective Email inventory. It is SHOW-only: no set/reset/writer exists,
non-empty input is refused, and mailbox I/O is not performed. A successful
response is `{"settings": [...]}` with exactly `key`, `current`, `default`,
`configurable`, and `comment` in every row. The complete response is bounded at
65,536 UTF-8 bytes. If applied truth or a provider row is unavailable, the whole
action fails with fixed `SETTINGS_UNAVAILABLE`; it never returns partial rows or
exception detail.

The five rows are ordered as follows:

| Key | Current | Default | Configurable | Comment anchor |
|---|---:|---:|---|---|
| `send.body_char_limit` | `50000` | `50000` | `false` | `email-manual#send-body-character-limit` |
| `send.duplicate_free_passes` | `2` | `2` | `false` | `email-manual#duplicate-send-loop-guard` |
| `check.result_token_limit` | `10000` | `10000` | `false` | `email-manual#check-result-token-limit` |
| `unread.max_entries` | `10` | `10` | `false` | `email-manual#unread-notification-entry-limit` |
| `manifest.pseudo_agent_subscriptions` | `<redacted>` | `<redacted>` | `true` | `email-manual#pseudo-agent-subscriptions` |

## Send body character limit

- **Key:** `send.body_char_limit`; installed integer `50000`.
- **Meaning:** maximum accepted internal-email body length in Unicode
  characters. Oversize `send` and `reply` bodies are refused before delivery.
- **Source/configuration:** installed code; no environment variable or owner
  settings file. `configurable` is false.
- **Timing:** only a reviewed product-code/package change plus full agent
  relaunch changes it; SHOW never writes. Verify with SHOW after relaunch.

## Duplicate send loop guard

- **Key:** `send.duplicate_free_passes`; installed integer `2`.
- **Meaning:** consecutive identical sends allowed per recipient before Email
  blocks the next duplicate as a loop.
- **Source/configuration:** installed `email/settings.py` constant; no
  environment variable or owner settings file. `configurable` is false.
- **Timing:** only reviewed code/package change plus full relaunch changes it;
  verify with a second SHOW.

## Check result token limit

- **Key:** `check.result_token_limit`; installed integer `10000`.
- **Meaning:** one `check` result's token budget; summaries are removed until
  the serialized response fits.
- **Source/configuration:** installed `email/settings.py` constant; no
  environment variable or owner settings file. `configurable` is false.
- **Timing:** only reviewed code/package change plus full relaunch changes it;
  verify with a second SHOW.

## Unread notification entry limit

- **Key:** `unread.max_entries`; installed integer `10`.
- **Meaning:** maximum newest unread entries projected into one Email
  notification mirror; total unread count remains exact.
- **Source/configuration:** installed `email/settings.py` constant; no
  environment variable or owner settings file. `configurable` is false.
- **Timing:** only reviewed code/package change plus full relaunch changes it;
  verify with a second SHOW.

## Pseudo-agent subscriptions

- **Key:** `manifest.pseudo_agent_subscriptions`; current and default always
  `<redacted>`. The raw values are path lists and never appear in SHOW output.
- **Meaning:** pseudo-agent directories whose outboxes the POSIX mail adapter
  polls in addition to this agent's inbox.
- **Accepted values:** JSON list of path strings; `[]` disables subscriptions.
  If absent, the launcher's meaningful default is `["../human"]`. The init
  schema requires a list, and an element that cannot be interpreted as a path
  fails adapter construction rather than being silently ignored.
- **Source/precedence:** exactly the `manifest.pseudo_agent_subscriptions`
  field in `init.json`; no Email environment variable or owner-file peer. The
  resolved list is sensitive local routing data and must not be inferred from
  SHOW or copied from logs.
- **Timing/change procedure:** after explicit owner/human authorization, edit
  that exact manifest field with the existing File or Shell capability, then
  perform a full agent relaunch. Ordinary refresh does not reconstruct the mail
  adapter. Call SHOW again after relaunch; it proves availability and redaction,
  never the paths.

## Privacy and ownership boundaries

Mailbox/session paths, addresses, identities, contacts, messages, attachments,
and read/archive state are runtime/domain data, not settings. They are excluded,
not merely redacted. `LINGTAI_AGENT_ALIVE_THRESHOLD_SEC` remains kernel liveness
policy and `LINGTAI_NOTIFICATION_MAX_CHARS` remains Notification presentation
policy; Email does not claim either variable. The legacy 200-character digest
renderer wording is not an effective Email setting because live unread publishing
uses full-body entries.
