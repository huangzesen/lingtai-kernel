---
name: vision-settings-reference
tool: vision
related_files:
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/settings.py
  - src/lingtai/tools/vision/CONTRACT.md
maintenance: |
  Keep the thirteen setting anchors in the router stable. Document only the
  applied, read-only snapshot and existing owner procedures; never add a writer
  or imply provider/credential fallback.
---
# Vision settings reference

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
