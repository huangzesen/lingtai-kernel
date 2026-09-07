---
name: vision-backends-reference
tool: vision
related_files:
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/settings.py
  - src/lingtai/tools/vision/CONTRACT.md
maintenance: |
  Keep backend setup examples and troubleshooting provider-neutral where the
  implementation is provider-neutral. All installation, model, endpoint, and
  configuration changes require explicit operator consent and existing owner
  procedures.
---
# Vision backend reference

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
