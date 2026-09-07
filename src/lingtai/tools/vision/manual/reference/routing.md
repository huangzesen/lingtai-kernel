---
name: vision-routing-reference
tool: vision
related_files:
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/CONTRACT.md
  - src/lingtai/tools/vision/BEHAVIORS.md
maintenance: |
  Keep routing and authorization language aligned with the Vision resolver.
  Alternatives remain explicit instructions: do not introduce automatic
  provider, model, credential, preset, MCP, or CLI fallback.
---
# Vision routing reference

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
