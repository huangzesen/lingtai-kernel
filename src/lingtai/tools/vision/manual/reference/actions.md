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
  row's exact `comment` section below for meaning and the owner procedure.
- `vision(action="manual", input={}, reasoning="...")` — this guidance. Its
  input is strictly empty; it reads the installed manual body/path and performs
  no config, credential, provider, image, or analyze operation.

`reasoning` is required on every action and is invocation metadata; it never
becomes part of child input. Optional `summarize` is a root presentation
control. An unknown action, root field, or cross-action input field is rejected
before provider, credential, image, or manual-child work.
