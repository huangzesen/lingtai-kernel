---
name: wechat-media-reference
description: |
  Focused WeChat media reference: contained outbound paths, extension-based
  message types, text-plus-media ordering and partial outcomes, inbound file
  validation, and safe recovery from cache or upload failures.
version: 1.0.0
last_changed_at: "2026-09-07T00:00:00Z"
related_files:
- src/lingtai/mcp_servers/wechat/SKILL.md
- src/lingtai/mcp_servers/wechat/manager.py
- src/lingtai/mcp_servers/wechat/media.py
- src/lingtai/mcp_servers/wechat/api.py
- src/lingtai/mcp_servers/wechat/types.py
- tests/test_wechat_media_validation.py
- tests/test_wechat_media_warning_integration.py
- tests/test_wechat_media_official_cdn.py
- tests/test_wechat_media_upload_diagnostics.py
maintenance: |
  Tracks WeChat media type detection, validation warnings, upload stages, and
  partial-delivery guidance; update when provider media fields, containment, or
  diagnostics change.
---

# WeChat media

Use this reference for attachments and inbound files. `send` remains an external
side effect; confirm `user_id` and content before starting an upload.

## OUTBOUND `media_path`

- The public schema advertises `media_path` as a file path. The outbound-file
  resolver requires a readable file within the agent's allowed working area and
  rejects paths outside that boundary before any message is sent.
- The suffix selects the WeChat item type: common image suffixes (`.jpg`,
  `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`) are images; video suffixes (`.mp4`,
  `.avi`, `.mov`, `.mkv`) are video; voice suffixes (`.wav`, `.mp3`, `.ogg`,
  `.silk`, `.amr`) are voice; other suffixes are sent as files. This is an
  extension mapping, not content inspection for outbound files.
- `text` and `media_path` may be supplied together. The manager sends them as two
  recipient-visible messages, text first and media second. It validates the path
  before sending text, preventing a missing or disallowed file from causing an
  avoidable text-only send.
- A later upload or media-reference failure can still leave the text delivered.
  The result reports a partial outcome and disables automatic replay; do not
  resend the entire text-plus-media request without reconciling what happened.

## INBOUND MEDIA AND FILES

Inbound items are saved as local artifacts and represented in message text with
bounded tags such as `[Image: path]`, `[Voice: "transcript" (audio: path)]`,
`[File: name (path)]`, and `[Video: path]`. Verify the path exists and is readable
before analysis; a path in a message is not a request to paste that path back to
the user.

Some WeChat document downloads can be encrypted or cache placeholders. Before
parsing a received file, compare its bytes with its claimed type, for example
`%PDF-` for PDF and `PK` for ZIP/DOCX. The bundled validator checks known magic
signatures and emits a warning annotation for a mismatch; an unknown or unreadable
file is not silently treated as valid. Ask for a WeChat “Save As” re-export or a
trusted download link when the bytes do not match.

Images are checked against recognized image signatures rather than trusting the
synthetic filename alone. Voice downloads may remain in Silk form when the
optional decoder is unavailable; use the preserved path and report that limitation
instead of claiming successful transcription.

## UPLOAD FAILURE HANDLING

The upload path obtains the provider's upload parameters, uploads the encrypted
payload to the official CDN route, and requires the CDN's final download
parameter before constructing the outgoing media item. Bounded immediate retries
belong to the CDN upload stage only; they do not replay the surrounding send or
its already accepted text. Read the returned stage/error fields and reconcile
provider state before any human-authorized retry.
