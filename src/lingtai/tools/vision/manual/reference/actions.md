---
name: vision-actions-reference
tool: vision
related_files:
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/CONTRACT.md
maintenance: |
  Keep examples aligned with the declaration-owned action schemas and the
  installed manual router. Preserve strict input, first-call, and no-fallback
  semantics when updating examples.
---
# Vision actions reference

## Call shape

`vision` is one action-separated tool with five strict actions:

- `vision(action="analyze", input={"image_path": "...", "question": null},
  reasoning="...")` — the direct image request. `image_path` and nullable
  `question` are required fields; `null` selects the default prompt
  `Describe what you see in this image.`. The optional nullable `preset` field
  explicitly borrows one allowed preset's vision service for this call.
- `vision(action="check", input={"preset": null}, reasoning="...")` — resolve
  the default route without an image. The `preset` field is required and must
  be `null` or an allowed preset reference. A non-null value resolves the
  borrowed provider/model and constructs its service, but never calls a
  provider or sends image data.
- `vision(action="list", input={}, reasoning="...")` — mechanically enumerate
  the active route and vision-capable presets in `manifest.preset.allowed`.
  It reads route declarations only: it constructs no provider service and
  reads no credential.
- `vision(action="settings", input={}, reasoning="...")` — show the applied
  bind-time configuration as rows containing exactly `key`, `current`,
  `default`, `configurable`, and `comment`. Input is strictly `{}`. This action
  never sets, resets, validates, re-reads, or writes configuration. Follow each
  row's exact `comment` into the top manual's stable setting anchor, then use
  the settings reference for meaning and the owner procedure.
- `vision(action="manual", input={}, reasoning="...")` — load the installed
  top-level Vision manual. Its input is strictly empty; it reads that manual's
  body/path and performs no config, credential, provider, image, or analyze
  operation.

`reasoning` is required on every action and is invocation metadata; it never
becomes part of child input. Optional `summarize` is a root presentation
control. An unknown action, root field, or cross-action input field is rejected
before provider, credential, image, or manual-child work.

## Result shapes

- `analyze` success is exactly `{"status": "ok", "analysis": text}`.
- `check` success is exactly `{"status": "ok", "route": route,
  "provider": provider, "model": model}`.
- `list` success is `{"status": "ok", "default": default,
  "presets": presets, "count": count}`; entries contain route identity, not
  credentials.
- `settings` success is `{"settings": [...]}` with only the five row fields
  above; unavailable truth fails as a fixed no-row result.
- `manual` success is exactly `{"status": "ok", "action": "manual",
  "manual": body, "manual_path": path}`; a missing installed manual returns a
  truthful degraded result rather than another family's guidance.

Setup, authorization, image, provider, or empty-response failures are
structured errors with sanitized guidance; raw exception contents,
credentials, and unsanitized endpoints never enter a result.
