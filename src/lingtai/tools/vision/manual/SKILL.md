---
name: vision-manual
description: >
  Troubleshoot an unavailable or failed vision route without automatic
  provider/credential fallback.
last_changed_at: 2026-08-29T00:00:00Z
related_files:
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/ANATOMY.md
  - src/lingtai/tools/vision/CONTRACT.md
  - src/lingtai/tools/vision/BEHAVIORS.md
  - src/lingtai/tools/vision/settings.py
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
maintenance: |
  Keep this manual provider-neutral and the settings action read-only. Keep each
  projected comment pointer aligned with its exact Setting heading. It must not
  import, expose, or link to a TUI package, credential value, endpoint secret,
  or automatic MCP action.
---
# Vision manual

This is the provider-neutral fallback for `vision`. It contains guidance only;
it does not discover, install, start, or invoke a backend. The static official
Vision declaration owns this package as its `manual="vision"` destination, so
the registrar-bound `manual` action reads this installed `SKILL.md` and not a
host-global or another family's skill.

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

## Settings inventory

The inventory freezes the effective values used when Vision binds during boot
or `system(action="refresh", input={}, reasoning="apply approved Vision config")`.
SHOW does not re-read a file, environment variable, preset, or provider. If the
provider or required model is not truthfully available, the local owner file is
invalid, or an opaque injected service has no attributable owner route, the
whole action returns `SETTINGS_UNAVAILABLE`; it never fabricates or emits
partial rows. `configurable: true` means an existing owner procedure can change
the value outside SHOW, not that this action can mutate anything.

Each setting below names its own source and precedence because the routes do
not share one universal chain. Every real change requires owner authorization,
an edit through the named file/config/preset procedure, refresh or relaunch as
stated, and a second SHOW to verify the newly applied snapshot.

## Setting: provider

The current direct-route provider. It comes from the explicit Vision capability
or, when compatible, the active provider. There is no provider default and no
automatic provider switch. Change `manifest.capabilities.vision.provider` in
the active `init.json`/preset, then refresh. Routing changes do not grant
credentials, installation, or network authority.

## Setting: base-url

The effective endpoint override. It comes from the explicit Vision capability,
the same-provider active route, or — for `provider="local"` —
`settings/vision.json` and then `http://localhost:11434/v1`. MiMo and Codex
services also have their constructor endpoints. The value is sensitive and
always redacted. Edit the existing capability/preset or local owner file,
refresh, and SHOW again; never paste a private endpoint into logs or chat.

## Setting: model

The model identifier used by the bound route. It comes from the explicit Vision
capability, the same-provider active route, the local owner file, or the
successfully constructed service. Public identifiers are shown exactly; a
path-like model value is private, so SHOW redacts both current and default.
Local Vision has no model default. Only the explicit MLX route has a
Vision-owned model default. Change the active capability/preset or local owner
file, ensure any model pull/install has separate human approval, refresh, and
verify with SHOW.

## Setting: api-key

Whether API credential material was applied. The resolved value comes from an
explicit capability `api_key_env`/`api_key`, the same-provider credential, or
the local owner file; local Vision otherwise uses a non-secret SDK placeholder.
Codex and MLX do not consume this field. Current and default are always
redacted—SHOW retains only presence, never raw material. Change the owner secret
store or launcher configuration, then relaunch or refresh without printing it.

## Setting: api-key-env

Whether an explicit capability credential-variable pointer was applied. The
pointer is resolved once during bind before raw-key fallback. Its name and value
are both sensitive and redacted; there is no default. Change
`manifest.capabilities.vision.api_key_env` and the launcher/secret-manager
environment through their existing owner procedures, then relaunch or refresh.
SHOW never reads or changes the process environment.

## Setting: max-tokens

The direct service's positive response-token cap. An explicit capability value
wins; local Vision next reads `settings/vision.json`. OpenAI-compatible,
Anthropic-compatible, MiMo, and local services default to `1024`; MLX defaults
to `512`; routes that do not consume this field show `null`. Edit the existing
capability/preset or local owner file, refresh, and verify with SHOW.

## Setting: api-compat

The compatibility family selected from explicit Vision configuration or the
same active provider's defaults. Accepted values are `openai` or `anthropic`
where the relay supports that protocol. There is no default and an irrelevant
route shows `null`. Change the owning capability/provider configuration,
refresh, run `check`, and verify with SHOW. It does not grant provider access.

## Setting: wire-api

The effective OpenAI-compatible wire. Accepted configuration values are
`auto`, `chat_completions`, or `responses`; route support still decides whether
construction succeeds. Local/OpenAI-compatible services normally resolve to
`chat_completions`, while Codex is `responses`; non-applicable routes show
`null`. Change the owning capability or same-provider configuration, refresh,
run `check`, and verify with SHOW.

## Setting: default-headers

Whether provider-owned HTTP headers were applied to a compatible service.
Explicit Vision headers precede same-provider defaults. The complete mapping,
including header names, is sensitive and always redacted; no headers is the
`null` default. Edit the owning capability/provider configuration, refresh, and
SHOW again without logging the mapping.

## Setting: token-path

Whether a Codex OAuth identity path was applied. It comes from an explicit
Vision value, the same provider's `codex_auth_path`, or authorized pool
selection. Non-Codex routes show `null`. The path and token material are
sensitive and always redacted. Use the existing Codex login/account-pool or
preset procedure, then refresh and SHOW; never copy the file or token to output.

## Setting: instructions

Whether Codex Responses instructions were applied. Explicit Vision
instructions win; the Codex service otherwise owns its concise-assistant
default. Non-Codex routes show `null`. The text is sensitive and always
redacted. Edit the active capability/preset, refresh, and SHOW again; never put
credentials or authorization in instructions.

## Setting: max-output-tokens

The optional Codex Responses output cap. Accepted values are positive integers
supported by the backend, or `null` to omit it; the default is `null`. Change
the active capability/preset only after validating backend support, refresh,
run `check`, and verify with SHOW. This is distinct from `max_tokens`.

## Setting: timeout

The Codex request timeout in positive finite seconds. An explicit capability
value wins; Codex defaults to `120.0`, and non-Codex routes show `null`. Change
the active capability/preset, refresh, and verify with SHOW. A larger timeout
changes wait tolerance only; it grants no network, provider, or retry authority.

## Route behavior and failures

`vision` is always registered. With no explicit provider or `preset`, the default
route follows the active provider's own compatible identity (model, endpoint,
wire, and credential) or an explicitly configured Vision service. Missing or
unsupported identity fails closed to manual guidance. There is no hidden model,
legacy credential, provider switch, or automatic MCP/provider fallback.

An explicit `preset` request is different from fallback. The reference must be
listed in `manifest.preset.allowed`; Vision then loads that preset read-only and
uses the allowed preset's own `manifest.llm` and `manifest.capabilities.vision`
identity. That can include resolving the allowed preset's own `api_key` or
`api_key_env`, or selecting its own Codex OAuth-pool identity, in order to build
the requested borrowed service. Borrowing is therefore authorized credential
routing for one call: it does not switch the active preset, lend the active
preset's model/credential to the borrowed route, or silently choose another
preset after a failure. An unlisted, unreadable, or incomplete preset fails
closed with sanitized guidance.

A direct setup or request failure reports the failure type and points here for
explicit alternatives; it never exposes exception contents. A mention of MCP,
a local server, another preset, or the Claude CLI is an instruction for a later
explicit operator/agent action, not an automatic fallback or invocation.

## Borrow flow

To use another already-authorized preset's vision service for one image request:

1. Run `vision(action="list", input={}, reasoning="...")` to see which allowed
   preset declarations advertise vision and their endpoint classification.
2. Run `vision(action="check", input={"preset": "<allowed preset>"},
   reasoning="...")` to resolve that preset's provider/model without sending
   an image. Route construction may resolve that preset's own credential.
3. Run `vision(action="analyze",
   input={"image_path": "...", "question": null,
   "preset": "<allowed preset>"}, reasoning="...")` to send one image request
   through the explicitly selected service.

The allowed list is the authorization boundary. Borrowing never silently
switches the active preset and never auto-invokes MCP or another provider. If
the selected route fails, inspect the returned manual guidance and ask the
operator before changing configuration, preset authorization, or installing a
backend.

## Claude backend: use the Claude CLI for vision

When the active provider is a Claude-family backend (`claude-code`, `claude_code`,
or the `claude-p` vision alias), the vision capability does not proxy Claude's
own CLI authentication. The analyze call fails closed with explicit guidance
instead of constructing a service:

> You are using claude as backend, therefore to use vision run `claude -p`;
> see the vision manual for more details.

### How Claude CLI vision works

Claude Code attaches images by file path: when the prompt references an image
path, the CLI reads the file and sends it to the model as an image input block
alongside the text. `-p` / `--print` is the non-interactive print mode, so the
analysis is returned as plain text on stdout — ideal for scripting.

- Run in print mode with the image path referenced in the prompt:
  `claude -p "Analyze this image: /path/to/image.png"`.
- Supported image formats include JPEG, PNG, and GIF (GIF uses the first
  frame). The CLI uses its own authentication (claude.ai subscription, API
  key, or a configured provider) and its own cost model.

### Progressive disclosure to the official docs

For authoritative details, progressively read the Claude Code CLI documentation:

- CLI reference: <https://code.claude.com/docs/en/cli-reference>
- Image workflows: <https://code.claude.com/docs/en/common-workflows>

This manual never auto-invokes the CLI; running `claude -p` is an explicit
operator/agent action with the CLI's own auth and cost model.

## Stay on the active preset

Inspect the identity already shown in the prompt: the current provider, model,
and sanitized endpoint. The default route follows that active LLM; do not
substitute another provider, model, credential, endpoint, or wire protocol, and
never silently switch or auto-invoke an MCP. If the active route cannot see
images, the call fails explicitly. Use a borrowed route only by naming an
already-authorized preset in the `preset` field; its own credential may be
resolved for that explicit request.

## Find the current preset's method

Use the `skills` capability's catalog to search installed skills for a manual
matching that provider/model or preset. Read the matching manual before trying
its documented method or official-page pointer. If no matching manual is
present, report that no discoverable vision method is available.

An optional MCP or other skill may be described by that preset manual, but it is
always an explicit operator/agent action. This manual never auto-loads or
auto-invokes MCP.

## Safety

Never request or print API keys, OAuth tokens, environment values, headers, or
full unsanitized URLs. Missing provider, model, or endpoint fields are simply
unknown; do not fill them with guesses.

## Local vision (generic OpenAI-compatible provider)

`provider="local"` points the `vision` capability at any local
OpenAI-compatible vision server (Ollama, LM Studio, vLLM, llama.cpp server, ...)
by URL. It needs no API key (a placeholder is synthesized; local servers ignore
it), defaults `base_url` to `http://localhost:11434/v1`, and requires an
explicit `model` - there is no hidden default model, because a silently assumed
model masks misconfiguration.

The endpoint is operator-owned. Configure it in `settings/vision.json` (the
family-owned file, like `settings/web.json`), in the capability manifest, or
both (capability kwargs override the file).

### 1. Pick and install a server + pull a vision model

Any server that speaks the OpenAI Chat Completions API with image support
works. Examples:

- **Ollama** (easiest): install from <https://ollama.com>, then pull a
  vision-capable model. `moondream` is a good small default (~1.7 GB, runs on
  CPU or a small GPU, fine for OCR and basic description):

      ollama pull moondream

  Other vision-capable Ollama models exist (`llava`, `qwen2.5vl`, ...). The
  model must be a vision model - a text-only model fails at request time with a
  "does not support images" style error.

- **LM Studio**: start a local server with an image-capable model, note the
  port (default `http://localhost:1234/v1`).
- **vLLM / llama.cpp server**: serve a multimodal model and point `base_url` at
  its `/v1` endpoint.

### 2. Configure the endpoint

Two equivalent owner procedures are available; capability input wins over the
file. The public settings action only shows the bound result.

**`settings/vision.json`** (agent working dir, applies on next refresh):

    {
      "schema_version": 1,
      "base_url": "http://localhost:11434/v1",
      "model": "moondream",
      "max_tokens": 1024
    }

`api_key` is optional and omitted here. Only `schema_version` plus the
documented fields are allowed; an invalid file is a hard setup error surfaced
as manual guidance.

**Capability manifest** (`init.json` or the active preset's
`manifest.capabilities`):

    "vision": {
      "provider": "local",
      "model": "moondream"
    }

    "vision": {
      "provider": "local",
      "model": "moondream",
      "base_url": "http://localhost:11434/v1",
      "max_tokens": 1024
    }

`model` is required and must name a model the server actually serves. `base_url`
defaults to `http://localhost:11434/v1`; change it when the server runs on a
non-default port (the `/v1` OpenAI-compatible suffix is required). `api_key` is
optional - local servers ignore it, so a placeholder is synthesized.

> **Preset note.** `vision` is always registered; an explicit `capabilities.vision`
> entry is **not** required to make the tool appear. The default route inherits
> the active LLM's own Responses API. A capability-manifest entry (in
> `init.json` or the active preset) is only needed to override that default,
> e.g. to point at `provider="local"`. To borrow another preset's vision
> service for a single call, list that preset in `manifest.preset.allowed` and
> pass `preset` on the analyze call; no `capabilities.vision` edit is needed.

### 3. Use it

After configuring and refreshing, the `vision` tool is available:

    vision(action="analyze", input={"image_path": "/path/to/image.png", "question": null}, reasoning="...")

A successful call returns `{"status": "ok", "analysis": "..."}`. If you get a
sanitized setup failure instead, check the troubleshooting table below.

### 4. Troubleshooting local vision

| Symptom | Likely cause / fix |
|---|---|
| "No direct vision provider was configured" | No explicit provider and no usable active-LLM route. The tool is always registered; either borrow an allowed preset's vision service via the `preset` option, or configure a local route (see below), then refresh. |
| "Local vision needs an explicit model" | No `model` is set in `settings/vision.json` or the capability manifest. Set `model` to a pulled/served vision model name, then refresh. |
| "Local vision settings are invalid" | `settings/vision.json` has an unknown field, bad type, or a schema_version other than 1. Fix the file and refresh. |
| Connection refused on the endpoint | The local server is not running. Start it (`ollama serve` or the desktop app) and retry. |
| "model '<name>' not found" | The model was never pulled or has a different name. Run `ollama list` (or your server's model list) and set `model` to the exact name. |
| "does not support images" / vision request rejected | The configured model is text-only. Pull/serve a vision model (e.g. `moondream`) and point `model` at it. |
| "...missing the '/v1' suffix..." (from the service) | `base_url` is missing the OpenAI-compatible suffix. Use e.g. `http://localhost:11434/v1`. |
| HTML/JSON parse failure on the response | The server returned a non-ChatCompletion body - usually the route is wrong (see previous row) or the server is too old. Upgrade and use `/v1`. |
| GPU not used / slow | The server offloads to the GPU only when the model fits VRAM. `moondream` fits most GPUs; larger models fall back to CPU. |

### 5. Apple MLX (macOS only)

The native on-device MLX pseudo-provider (`provider="mlx"`) is available as an
explicit opt-in for Apple Silicon. It is not advertised in check-caps; pass
`model` (an `mlx-community/...` vision model) and `max_tokens`. It requires no
API key.
