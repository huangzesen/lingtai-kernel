---
name: vision-manual
description: >
  Choose a Vision action, route, setting, or backend reference without
  automatic provider/credential fallback.
last_changed_at: 2026-09-06T00:00:00Z
related_files:
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/ANATOMY.md
  - src/lingtai/tools/vision/CONTRACT.md
  - src/lingtai/tools/vision/BEHAVIORS.md
  - src/lingtai/tools/vision/settings.py
  - src/lingtai/tools/vision/manual/reference/actions.md
  - src/lingtai/tools/vision/manual/reference/routing.md
  - src/lingtai/tools/vision/manual/reference/settings.md
  - src/lingtai/tools/vision/manual/reference/backends.md
maintenance: |
  Keep this page a short provider-neutral router and the settings action
  read-only. Keep every setting anchor stable and route depth to the focused
  references. Do not add provider, credential, endpoint, CLI, or MCP fallback;
  never import, expose, or link a secret.
---
# Vision manual

This installed, provider-neutral manual is guidance only. The reserved
`manual` action reads this package body and path; it does not discover, install,
start, or invoke a backend.

## Choose an action

`vision` has one strict action-separated root. Every call requires
`action`, `input`, and `reasoning`; `input` must match the selected child. Use
these first-call forms:

- **Analyze an image:**
  `vision(action="analyze", input={"image_path": "...", "question": null}, reasoning="...")`.
  `image_path` is required; a relative path is resolved from the workdir.
  `question: null` means `Describe what you see in this image.`. Add
  `preset: "<allowed reference>"` only to explicitly borrow a route listed in
  `manifest.preset.allowed`.
- **Check a route:**
  `vision(action="check", input={"preset": null}, reasoning="...")`.
  `null` checks the default route; a reference checks that explicitly borrowed
  route without sending an image.
- **List routes:** `vision(action="list", input={}, reasoning="...")`.
  This mechanically lists the active route and vision-capable allowed presets;
  it constructs no provider service or credential.
- **Show applied settings:**
  `vision(action="settings", input={}, reasoning="...")`. This is SHOW-only:
  it does not read, validate, set, reset, or write configuration.
- **Load guidance:** `vision(action="manual", input={}, reasoning="...")`.

Unknown actions, root fields, or cross-action input fields fail before provider,
credential, image, or manual-child work. `preset` is an explicit authorization
boundary, not fallback: the allowed preset's own provider/model/credential
identity is used for that request. A default failure remains a sanitized error;
Vision never automatically switches provider/model/credential, preset, MCP, or
CLI, and Vision never auto-invokes MCP. Alternatives are instructions for a
later explicit operator action.

For action details and result shapes, read
[actions](reference/actions.md). For route identity, borrowing, Claude CLI,
active-preset guidance, and safety, read [routing](reference/routing.md). For
local servers, MLX, setup, and troubleshooting, read
[backends](reference/backends.md).

## Settings anchors

The settings action returns exactly `key`, `current`, `default`,
`configurable`, and `comment` for the applied bind snapshot. Sensitive values
and path-like models are redacted. SHOW never mutates or re-reads state. Each
anchor below is stable; read the [settings reference](reference/settings.md)
for source, precedence, redaction, and the existing owner procedure.

## Setting: provider

The current direct-route provider; no provider default or automatic switch.

## Setting: base-url

The bound endpoint override or route-owned endpoint; sensitive values are redacted.

## Setting: model

The bound route model; path-like values are redacted and no hidden local model is assumed.

## Setting: api-key

Whether the bound route applied credential material; raw values are never shown.

## Setting: api-key-env

Whether an explicit credential-variable pointer was applied; SHOW never reads the environment.

## Setting: max-tokens

The route's response-token cap where supported; route defaults remain owner-defined.

## Setting: api-compat

The route compatibility family where applicable; it grants no provider access.

## Setting: wire-api

The configured compatible wire where applicable; route support still controls construction.

## Setting: default-headers

Whether provider-owned headers were applied; the mapping is always redacted.

## Setting: token-path

Whether a Codex OAuth identity path was applied; path and token material are redacted.

## Setting: instructions

Whether Codex Responses instructions were applied; instruction text is redacted.

## Setting: max-output-tokens

The optional Codex Responses output cap, distinct from `max_tokens`.

## Setting: timeout

The Codex request timeout where applicable; changing it grants no retry or network authority.

If a route, local settings document, model, or credential is unavailable, the
whole settings inventory fails closed rather than returning partial or guessed
rows. To change a value, use its existing owner procedure, refresh or relaunch,
and then SHOW again; this manual never performs that change.
