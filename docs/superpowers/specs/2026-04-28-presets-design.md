# Presets — Design

**Date:** 2026-04-28
**Status:** Draft for review
**Scope:** kernel (`lingtai-kernel`) primarily, plus TUI (`lingtai`) for `init.json` generation + migration. Portal: version bump only.

---

## Problem

`init.json` today encodes the agent's LLM provider and capability set as flat fields under `manifest.llm` and `manifest.capabilities`. There is no way for the agent to deliberately reach for a different LLM + tool combination at runtime — for example: "API rate-limited, switch to a backup provider," or "I'm doing heavy reasoning for the next hour, give me the smarter model." Authoring multiple such configurations means hand-editing `init.json` and triggering a refresh from the outside.

Daemons and avatars suffer the same rigidity. The agent has no way to say "spawn this avatar with a cheap LLM" — every subagent inherits the parent's main config.

## Goal

Introduce **presets** — named, atomic bundles of `{llm, capabilities}` stored as files in a per-agent library, swappable at runtime through a single agent-facing knob: `system(action="refresh", preset="<name>")`.

Design tenets:

- **Preset is a chosen configuration, not an identity.** The agent remains itself across swaps: same memory, pad, codex, chat history, name, agent_id, peer relations, running daemons. Only the **胎光 (LLM)** and **肉身 (capability set)** change — the implements the agent currently uses, not who the agent is.
- **Atomic bundle.** A preset always carries both halves. Power users who want to flip only one half edit `init.json` directly; the agent never sees a half-swap verb.
- **Two halves, one motion.** Each swap rebuilds both 胎光 and 肉身 atomically — but the agent reasons about the two halves separately when *choosing* which preset to swap to. A preset may bundle a weaker LLM with stronger faculties (e.g. Minimax with multi-modal vision); the agent can deliberately accept the LLM cost to gain the faculty, do the visual work, then swap back. The structured `comment` field exists precisely so authors can document these tradeoffs and the agent can read them. The act is one motion; the *reasoning* about it is two-dimensional.
- **Swap is folded into `refresh`.** Refresh already means "re-read `init.json` and rebuild." Swap is "edit `init.json` first, then refresh." Same engine, optional new parameter.

## Non-Goals

- **No preset composition or inheritance.** Atomic only. Users may duplicate fields between presets; that is their choice and is mitigated by structured `comment` documentation.
- **No multi-folder preset libraries.** One folder per agent (`presets_path`), no merging.
- **No agent-facing half-swap verbs.** No `swap_mind` / `swap_form` etc. Single-half swaps are a power-user concern handled via direct file edit + refresh.
- **No `preset_history` ring buffer.** Agent narrative recovers from the events log if needed.
- **No top-level provider pool / quiver redesign.** The `"inherit"` sentinel (defined below) is a small, surgical decoupling; a full provider-pool refactor is deferred to a separate spec.
- **No daemon model override.** That work landed in the prior daemon FS refactor (commit `bc3c011`). Daemons inherit the parent's currently-active preset wholesale and cannot swap.

## Reconciliation with the TUI's existing presets

**The TUI already has presets.** They live at `~/.lingtai-tui/presets/*.json` and have shipped for months. The TUI uses them as agent-creation templates (`GenerateInitJSONWithOpts(preset, ...)` bakes a preset into a fresh `init.json`). Six built-ins (`minimax`, `zhipu`, `deepseek`, `openrouter`, `codex`, `custom`) are seeded on first run; users save variants alongside (`zhipu_intl`, `deepseek_pro`, etc.).

Existing preset on-disk shape:

```jsonc
{
  "name": "deepseek",
  "description": "DeepSeek V4 — OpenAI-compatible, 1M context window, tool calls",
  "manifest": {
    "llm":          { "provider": "deepseek", "model": "...", "api_key_env": "...", "base_url": "...", "api_compat": "openai" },
    "capabilities": { /* full capability map */ },
    "admin":        { "karma": true },
    "streaming":    false
  }
}
```

**This design unifies the kernel-side runtime swap with the TUI's existing creation-template concept** — same files, same library, same schema, two consumers:
- **TUI:** uses presets as agent-creation templates (existing behavior, unchanged).
- **Kernel:** uses presets as runtime swap targets (new behavior added by this spec).

Concretely: when the agent does `refresh(preset="cheap")`, the kernel reads the *same* `~/.lingtai-tui/presets/cheap.json` the TUI's wizard would have used, and substitutes its `manifest.llm` + `manifest.capabilities` into the agent's currently-active manifest. No second concept, no schema duplication.

## Architecture

### Preset library on disk

A preset is a single JSON file in `manifest.presets_path`. Auto-discovery rules:

- File extension `.json` (or `.jsonc`) → treated as a preset.
- The preset's `name` field inside the JSON is authoritative for identity. By convention, the filename stem matches `name` (the TUI enforces this on save), but the kernel reads `name` from the file content. Mismatch is allowed but warned.
- Subdirectories ignored. Only top-level files are scanned.
- Non-`.json[c]` files (e.g. `README.md`) are ignored.

Default value of `presets_path`: `~/.lingtai-tui/presets/` (the existing TUI library). Agents written by the TUI today will pick up this default automatically once the migration runs (see Migration). Users wanting a project-local library override by writing an explicit `presets_path` in `init.json`.

Preset file shape (existing TUI schema, extended for forward compatibility):

```jsonc
{
  "name": "deepseek",
  "description": "DeepSeek V4 — OpenAI-compatible, 1M context window, tool calls",
  // OR — forward-compatible structured form:
  // "description": {
  //   "summary": "DeepSeek V4 — text-only but cheap and large-context",
  //   "gains": ["1M context", "low cost"],
  //   "loses": ["vision", "multi-modal"]
  // }
  "manifest": {
    "llm": {
      "provider": "deepseek",
      "model": "deepseek-v4-pro",
      "api_key_env": "DEEPSEEK_API_KEY",
      "base_url": "https://api.deepseek.com",
      "api_compat": "openai"
    },
    "capabilities": {
      "file": {}, "email": {}, "bash": { "yolo": true },
      "psyche": {}, "codex": {}, "avatar": {}, "daemon": {},
      "library": { "paths": ["../.library_shared", "~/.lingtai-tui/utilities"] },
      "web_search": { "provider": "duckduckgo" },   // hand-picked fallback
      "listen":     { "provider": "whisper" },       // hand-picked fallback
      "web_read":   {},
      "vision":     { "provider": "inherit" }        // optional new sentinel
    },
    "admin":     { "karma": true },
    "streaming": false
  }
}
```

**`description` field — string-or-object union.** Existing presets carry a plain string description. Forward-thinking presets *may* use a structured object with author-chosen keys (e.g. `summary`, `gains`, `loses`, `recommended_for`, `cost_tier`, `notes`). The kernel surfaces `description` verbatim; it does not validate the inside of structured forms. Both shapes work; the structured form is purely additive for authors who want richer tradeoff documentation. The TUI's existing presets continue to use strings until migrated.

**Preset's `manifest` block is a *partial* manifest.** The TUI today writes presets with `{llm, capabilities, admin, streaming, ...}` — only the fields that vary by provider. When the agent does a runtime swap, the kernel substitutes only `manifest.llm` and `manifest.capabilities` from the preset; other manifest fields (admin, soul, stamina, max_rpm, molt_pressure, agent_name, etc.) on the running agent are *not* touched. This preserves agent identity and personal config across swaps. The TUI's *creation-time* use of presets still consumes the full `manifest` block — that path is unchanged.

### `init.json` changes

Two new optional fields under `manifest`:

```jsonc
{
  "manifest": {
    "presets_path": "~/.lingtai-tui/presets",   // defaults to TUI's global presets dir
    "active_preset": "deepseek",                // name of preset currently materialized below

    // The kernel still reads these directly. They are the materialization
    // of <presets_path>/<active_preset>.json (manifest.llm + manifest.capabilities only).
    "llm":          { /* materialized from preset */ },
    "capabilities": { /* materialized from preset */ },

    /* ...all other existing fields unchanged: admin, soul, stamina, max_rpm, etc. */
  }
}
```

Backward compatibility:

- An `init.json` with no `presets_path` and no `active_preset` works exactly as today. The kernel treats `manifest.llm` and `manifest.capabilities` as authoritative. `system(action="refresh", preset=...)` will return an error explaining that the agent has no presets library configured.
- An `init.json` with `presets_path` set must also have `active_preset` set. Validation error otherwise.
- An `init.json` with `active_preset` set but no `presets_path` defaults `presets_path` to `~/.lingtai-tui/presets/` (the TUI's global library). This is the common case for TUI-created agents.
- The named preset must exist in `presets_path`. Validation error if not.

When both are set, `manifest.llm` and `manifest.capabilities` reflect the materialization of the active preset's `manifest.llm` and `manifest.capabilities`. The agent's runtime always reads these fields, never the preset file directly. **What you see in `init.json` is what the agent is running** — there is no hidden indirection. The preset library is the source; `init.json` is the currently-loaded incarnation.

### The `"inherit"` sentinel (optional, coexists with hand-picked fallbacks)

Today's TUI presets hand-pick fallback providers per multi-modal capability. For example, `deepseek.json` writes `web_search: { "provider": "duckduckgo" }`, `listen: { "provider": "whisper" }`, and leaves `web_read: {}` (kernel-default trafilatura). This works fine and existing presets continue to work unchanged.

The `"inherit"` sentinel is an *additional* tool authors can use. Instead of hand-picking a provider per capability, an author can write `"provider": "inherit"` and the kernel resolves it at preset-load time using the main LLM's settings. This is purely opt-in; no existing preset is forced to adopt it.

When the kernel resolves a capability config and sees `"provider": "inherit"`, it expands the config in-place using the main LLM's settings:

```python
# pseudo-code, run at preset-load time (during refresh)
def _expand_inherit(caps: dict, main_llm: dict) -> dict:
    for cap_name, kwargs in caps.items():
        if isinstance(kwargs, dict) and kwargs.get("provider") == "inherit":
            kwargs["provider"]    = main_llm["provider"]
            kwargs["api_key"]     = main_llm.get("api_key")
            kwargs["api_key_env"] = main_llm.get("api_key_env")
            kwargs["base_url"]    = main_llm.get("base_url")
            # NB: model is NOT inherited — capability picks its own model
    return caps
```

The capability's existing `setup()` then attempts to honor this expanded provider:

1. **Provider supported.** The capability's service registry includes the requested provider → instantiate normally with the inherited credentials.
2. **Provider unsupported, fallback exists.** The capability declares a `fallback_on_inherit` provider in its module-level registry (e.g. `web_search` → `duckduckgo`, `listen` → local whisper, `web_read` → trafilatura). Use the fallback. No credentials required if the fallback is a local/free service.
3. **Provider unsupported, no fallback.** Silently skip registration — no schema entry, no handler. Log a `capability_skipped` event with `{ name, reason: "no provider for <main_provider>" }` so users can debug from the events log.

This is the **graceful-degradation contract**. The agent writes `"provider": "inherit"` once and the system handles the matrix of which faculties survive which LLM swap.

### `refresh` extension

`system(action="refresh")` already triggers `Agent._perform_refresh()`, which re-reads `init.json` and rebuilds the LLM service + capabilities + prompt sections in place.

The schema gains one new property:

```jsonc
{
  "action": "refresh",
  "preset": "<name>",   // optional; if present, swap to this preset before refreshing
  "reason": "<text>"    // existing
}
```

Handler logic:

```python
def _refresh(agent, args):
    preset_name = args.get("preset")
    if preset_name is not None:
        try:
            agent._activate_preset(preset_name)
        except KeyError:
            agent._log("preset_swap_failed",
                       requested=preset_name,
                       available=agent._list_presets(),
                       reason="not_found")
            return {"status": "error",
                    "message": f"preset '{preset_name}' not found",
                    "available": agent._list_presets()}
        except Exception as e:
            agent._log("preset_swap_failed",
                       requested=preset_name,
                       reason=f"validation_error: {e}")
            return {"status": "error", "message": str(e)}

    agent._perform_refresh()
    return {"status": "ok", "message": ...}
```

`agent._activate_preset(name)` does the on-disk substitution:

1. Resolve `presets_path` from current `manifest` (defaulting to `~/.lingtai-tui/presets/`).
2. Read the current `init.json`.
3. Read `<presets_path>/<name>.json[c]` (raise `KeyError` if missing).
4. Validate the preset shape (presence of `manifest.llm`, type of `manifest.capabilities`). Warn if the preset's internal `name` field doesn't match the filename.
5. Substitute into the running agent's init.json: `manifest.llm = preset.manifest.llm`; `manifest.capabilities = preset.manifest.capabilities`; `manifest.active_preset = name`. Other manifest fields (admin, soul, stamina, max_rpm, agent_name, etc.) are NOT touched — those are part of the running agent's identity, not the preset.
6. Write atomically via `tmp + os.replace`.
7. Return.

If step 5 fails (disk write error), no in-memory state has been touched yet. The agent simply hasn't swapped. If step 6 returns successfully but `_perform_refresh()` then fails, the on-disk state reflects the new preset but the running agent may be in a partial-rebuild state — same failure mode as a plain refresh that fails. This is acceptable because the next refresh will reconcile.

### `presets` action

A new top-level action under `system`:

```jsonc
{ "action": "presets" }
```

Returns the full library in one shot — every preset's name, `description`, LLM summary, and capability map. No pagination, no separate "info" action. The agent gets enough information to reason about tradeoffs across the whole library in one tool call.

Why one call: a typical library is 5–20 presets (the TUI's global library has 11 today). Each preset summary is ~200–500 tokens (mostly the `description` + capability map). Total fits in a few thousand tokens, well under the cost of two round-trips, and lets the agent compare presets directly — which is exactly what the two-dimensional (LLM × faculties) reasoning needs.

Returns:

```jsonc
{
  "status": "ok",
  "active": "deepseek",
  "available": [
    {
      "name": "minimax",
      "description": "MiniMax M2.7 — full multimodal capabilities",
      "llm": { "provider": "minimax", "model": "MiniMax-M2.7-highspeed" },
      "capabilities": {
        "file": {}, "email": {}, "bash": { "yolo": true },
        "vision":     { "provider": "minimax", "api_key_env": "MINIMAX_API_KEY" },
        "talk":       { "provider": "minimax", "api_key_env": "MINIMAX_API_KEY" },
        "draw":       { "provider": "minimax", "api_key_env": "MINIMAX_API_KEY" },
        "video":      { "provider": "minimax", "api_key_env": "MINIMAX_API_KEY" },
        "compose":    { "provider": "minimax", "api_key_env": "MINIMAX_API_KEY" },
        "web_search": { "provider": "minimax", "api_key_env": "MINIMAX_API_KEY" },
        /* ... etc ... */
      }
    },
    {
      "name": "deepseek",
      "description": "DeepSeek V4 — OpenAI-compatible, 1M context window, tool calls",
      "llm": { "provider": "deepseek", "model": "deepseek-v4-flash" },
      "capabilities": {
        "file": {}, "email": {},
        "web_search": { "provider": "duckduckgo" },
        "listen":     { "provider": "whisper" },
        "web_read":   {},
        /* ... no vision, no compose, no draw, no video, no talk ... */
      }
    }
    /* ... etc for all 11 presets ... */
  ]
}
```

Field semantics:

- **`description`** — surfaced verbatim. May be a plain string (existing TUI presets) or a structured object (forward-thinking authors). Either way, the agent reads it as the preset's "what you gain, what you lose" card.
- **`llm`** — summarized to `{provider, model}` only. Credentials (`api_key`, `api_key_env`), `base_url`, `api_compat` are stripped — never expose secrets to the agent's context, even on inspection.
- **`capabilities`** — the full per-preset capabilities object, exactly as authored on disk. Includes any `provider: "inherit"` sentinels and capability-specific kwargs. Does NOT pre-resolve `inherit` or graceful-fallback: the agent sees what's *configured* in the preset file, not what the resolver would produce when the swap happens. At swap time, fallback decisions depend on the *target* main LLM, which the agent is choosing right now — pre-resolving would obscure that decision.

The active preset's entry in `available[]` is structurally identical to the others. The only signal of "this is the current one" is the top-level `active` field. The agent compares the active preset against alternatives naturally (e.g. "I'm on `deepseek` now and considering `minimax` — what would I gain? compose, draw, talk, vision, video. What would I lose? a 1M context window.").

### Daemon and avatar interaction

**Daemons** snapshot the parent's currently-active preset at emanate time. Concretely: at `daemon(emanate)` time, the kernel reads `manifest.llm` and `manifest.capabilities` (already materialized) and uses them to build the daemon's LLM session and tool surface. The `model` field of `DaemonRunDir` (which we kept for forensics) stays. Daemons cannot call `refresh(preset=...)` because daemons don't have the `system` capability in their tool surface — same exclusion that prevents recursive emanation. No new code needed for the exclusion.

**Avatars** inherit the parent's `presets_path` field at spawn time. The avatar's freshly-written `init.json` carries the same `presets_path` (resolved to absolute path so it survives directory moves) and an `active_preset` chosen by the parent (or defaulting to the parent's current `active_preset`). After spawn, the avatar runs as an independent agent; it has full `system` capability and can `refresh(preset=...)` to swap among the same library. Parent and avatars share the library; if the user edits a preset file, all running agents see the change on their next refresh.

This means **the soul-library is per-project, not per-agent**. A project with five avatars sharing one preset library is the natural shape.

### Power-user escape hatch

Users who want to swap only the LLM half (易胎) without touching capabilities, or vice versa, edit `init.json` directly:

- Mutate `manifest.llm` (or `manifest.capabilities`) however desired.
- Run `lingtai refresh <agent_dir>` from outside, or call `system(action="refresh")` from inside.

The agent never gets a half-swap verb. This keeps the agent's mental model simple: "one verb, one bundle." Power users have file-system access; they don't need a special CLI command for this.

## Components

### New module

- `src/lingtai/presets.py` — preset loader and resolver.
  - `discover_presets(presets_path: Path) -> dict[str, Path]` — list `*.json[c]` files.
  - `load_preset(presets_path: Path, name: str) -> dict` — read + parse + validate one preset.
  - `expand_inherit(caps: dict, main_llm: dict) -> dict` — resolve the sentinel.
  - `materialize(preset: dict) -> tuple[dict, dict]` — return `(llm, capabilities)` ready to drop into manifest.

### Modified modules

- `src/lingtai/init_schema.py` — accept `manifest.presets_path` (str, optional) and `manifest.active_preset` (str, optional). When `active_preset` is set, `presets_path` defaults to `~/.lingtai-tui/presets/`. The named preset file must exist; the file must contain `manifest.llm` and `manifest.capabilities`.
- `src/lingtai/agent.py` — new method `_activate_preset(name)` (writes init.json with materialized preset). `_perform_refresh()` already does the rest.
- `src/lingtai_kernel/intrinsics/system.py` — `_refresh` accepts new `preset` arg. New `_presets` handler. Schema enum gains `"presets"`. Schema gains `preset` property.
- `src/lingtai/capabilities/web_search/__init__.py` (and `web_read`, `vision`, `listen`, `compose`) — add `fallback_on_inherit` to the module-level provider registry; teach `setup()` to honor the inherit-failure-skip-silently contract.
- `src/lingtai/core/avatar/__init__.py` — when writing the avatar's `init.json`, propagate `presets_path` (made absolute) and `active_preset`.
- `src/lingtai/i18n/{en,zh,wen}.json` — descriptions for the new action and new arg.

### TUI changes (separate repo: `lingtai-tui`)

- `tui/internal/preset/preset.go` — `GenerateInitJSONWithOpts` writes `manifest.active_preset = preset.Name` into the generated init.json. `presets_path` is omitted (defaults to global library).
- `tui/internal/migrate/m02X_add_active_preset.go` — new versioned migration. For each agent in the project, infer `active_preset` by matching the current `manifest.llm.{provider, model}` against entries in `~/.lingtai-tui/presets/`. Write the field if a unique match is found; otherwise leave the agent in no-preset mode.
- `tui/internal/migrate/migrate.go` — register the new migration, bump `CurrentVersion`.
- `portal/internal/migrate/migrate.go` — bump `CurrentVersion` to match (the portal shares meta.json version space) and register a no-op stub for the new migration.

### Unchanged

- `src/lingtai/core/daemon/__init__.py` — daemons already inherit parent's preset by virtue of reading `manifest.llm`/`manifest.capabilities`. Nothing to change.
- TUI presets folder layout — `~/.lingtai-tui/presets/*.json` already shipping. Used unchanged.
- Existing preset on-disk schema (`{name, description, manifest: {...}}`) — used unchanged. The kernel reads from the same files the TUI already creates.

## Data Flow

### Agent calls `refresh(preset="minimax")`

```
1. agent's tool dispatcher routes to system._refresh(args)
2. _refresh sees preset="minimax"
3. _refresh calls agent._activate_preset("minimax")
   3a. Resolve presets_path (default ~/.lingtai-tui/presets/)
   3b. Read current init.json
   3c. Read <presets_path>/minimax.json
   3d. Validate preset.manifest.llm exists, .capabilities is a dict
   3e. Substitute init.json's manifest.llm, manifest.capabilities,
       manifest.active_preset = "minimax". Other manifest fields untouched.
   3f. Atomically write init.json (tmp + os.replace)
4. _refresh calls agent._perform_refresh()
   4a. _perform_refresh re-reads init.json (now reflecting minimax)
   4b. expand_inherit() on manifest.capabilities using new manifest.llm
       (no-op for hand-picked-fallback presets like the existing TUI ones)
   4c. Tear down old LLMService and capabilities
   4d. Build new LLMService from materialized manifest.llm
   4e. Register surviving capabilities (those with valid providers or fallbacks)
   4f. Skipped capabilities log capability_skipped events
   4g. Reset prompt sections, mailbox name, etc.
5. _refresh returns {status: "ok"}
```

### Agent calls `refresh(preset="nonexistent")`

```
1. _refresh sees preset="nonexistent"
2. _activate_preset raises KeyError
3. _refresh logs preset_swap_failed event with available list
4. _refresh returns {status: "error", message: "...", available: [...]}
5. init.json untouched, agent state untouched
```

### Agent calls `presets`

```
1. system._presets(args)
2. Read presets_path from current manifest
3. discover_presets() -> dict of name -> file path
4. For each, load_preset() -> read name, description, llm summary, capabilities map
5. Return {active, available[]}
```

### Avatar spawn with presets

```
1. Parent calls avatar(spawn, name="alice")
2. AvatarManager builds avatar's working dir
3. When writing avatar's init.json:
   - presets_path: parent's presets_path resolved to absolute
   - active_preset: parent's active_preset
   - llm/capabilities: materialized from that preset (same as today's avatar inheritance pattern)
4. Avatar starts as independent process, reads its own init.json
5. Avatar can refresh(preset=...) freely
```

## Error Handling

| Situation | Behavior |
|---|---|
| `presets_path` set, `active_preset` not set | init.json validation error at load time |
| `presets_path` not set, `active_preset` set | `presets_path` defaults to `~/.lingtai-tui/presets/` |
| `presets_path` points to nonexistent dir | init.json validation error at load time |
| `active_preset` names a preset file that doesn't exist | init.json validation error at load time |
| Neither set | No-preset mode. `refresh(preset=...)` returns error explaining no library configured. |
| Preset file is malformed JSON | Validation error with file path and parse message |
| Preset file missing required `llm` field | Validation error |
| `refresh(preset=X)` where X doesn't exist | Return error, log `preset_swap_failed`, init.json untouched |
| `refresh(preset=X)` where X is malformed | Return error, log `preset_swap_failed`, init.json untouched |
| `refresh(preset=X)` succeeds disk write but rebuild fails | init.json reflects new preset; agent may be in degraded state. Next refresh reconciles. Same failure shape as today's `refresh` failure. |
| `inherit` resolves to a provider the capability supports | Use it with shared credentials |
| `inherit` resolves to a provider the capability doesn't support, fallback exists | Use fallback, log `capability_skipped` is NOT emitted (the cap loaded fine, just on a different provider) |
| `inherit` resolves to a provider the capability doesn't support, no fallback | Skip registration, log `capability_skipped` event |
| Capability's fallback service itself fails to instantiate | Skip registration, log `capability_skipped` with the fallback's error |

## Testing Strategy

### Unit tests — `tests/test_presets.py` (new)

- Discovery: empty folder, single preset, multiple presets, mixed `.json` and `.jsonc`, ignored subfolders, ignored non-JSON files.
- Loading: valid preset, malformed JSON, missing `llm`, missing `capabilities` (allowed — empty), structured `comment`.
- `expand_inherit`: provider field `"inherit"` → expanded; `"inherit"` not present → unchanged; `inherit` with no main LLM credentials → expanded but `api_key` is None.
- `materialize`: returns `(llm, capabilities)` with inherit pre-expanded.

### Unit tests — `tests/test_init_schema.py` (extend)

- `presets_path` + `active_preset` both present and valid → no error.
- Only `presets_path`, no `active_preset` → error.
- Only `active_preset`, no `presets_path` → error.
- `active_preset` names nonexistent file → error with file path.

### Unit tests — `tests/test_system.py` (extend)

- `refresh(preset="known")` → init.json updated, refresh runs, success.
- `refresh(preset="unknown")` → error returned, `preset_swap_failed` logged, init.json untouched.
- `refresh(preset="malformed")` → error, log entry, init.json untouched.
- `refresh()` with no preset arg → behaves as today.
- `system(action="presets")` → returns active + available list. Each entry includes name, full structured `comment`, LLM summary `{provider, model}` (no credentials), and the full per-preset `capabilities` object as authored (no inherit/fallback pre-resolution).

### Integration tests — `tests/test_preset_swap_e2e.py` (new)

- Build agent with `presets_path` + 2 presets. Verify initial materialization. Call `refresh(preset="other")`. Verify init.json on disk changed. Verify LLMService.provider changed. Verify capability set changed.
- Preset with `"provider": "inherit"` on web_search; main LLM is gemini → web_search gets gemini provider. Swap to deepseek (no web_search support) → web_search falls back to duckduckgo. Swap to a hypothetical no-fallback case → web_search disappears from tool surface.
- Avatar inheritance: spawn avatar with no preset arg → avatar's init.json carries parent's `presets_path` (absolute) and `active_preset`. Verify avatar can independently `refresh(preset=...)`.

### Existing tests

All existing daemon, avatar, refresh, and capability tests must continue passing. The preset feature is additive on the resolver path; agents without `presets_path` are unaffected.

## Open Questions / Risks

1. **What if a preset references a provider whose API key isn't in the environment?** The resolver expands `inherit` blindly using `api_key_env`; capability `setup()` discovers the missing env var when it tries to instantiate. Same failure mode as today (capability fails to setup, agent runs without it). Spec consistent.

2. **Atomic disk write on Windows.** `os.replace` is atomic on POSIX and best-effort-atomic on Windows. The kernel runs primarily on macOS and Linux; Windows users may see a torn write under crash. Acceptable, same as the rest of the kernel's filesystem contract.

3. **Concurrent swap attempts.** The agent is single-threaded for tool dispatch; two `refresh(preset=...)` calls can't interleave on the same agent. If the TUI also writes init.json simultaneously (it doesn't today), a race could occur. Out of scope.

4. **`active_preset` not mentioned in spec for what happens during validation when both fields are present at first load.** Clarified: load_preset validates the preset file; if it's malformed, init.json validation fails *before* the agent boots. The user must fix the preset or remove the active_preset field.

## Migration

The TUI's `~/.lingtai-tui/presets/` folder already exists and is populated with built-in and saved presets. Two migration paths interact:

**Kernel migration — none needed.** Existing `init.json` files without `presets_path` and `active_preset` continue to work unchanged. The kernel feature is purely additive at the runtime level: `system(action="refresh", preset=...)` returns an error when these fields are absent ("agent has no presets library configured — run `lingtai migrate` to enable preset swapping"). Agents that never opt in keep behaving exactly as before.

**TUI migration — adds the pointer fields.** A new TUI migration (`m02X_add_active_preset.go`) runs once per project and does:
1. For each agent in the project, read `init.json`.
2. Compare `manifest.llm.provider`/`manifest.llm.model` against each preset in `~/.lingtai-tui/presets/`.
3. If a unique match is found → write `manifest.active_preset = <name>`. `manifest.presets_path` is not written; it defaults to the TUI's global library.
4. If no match (custom config) → write nothing. The agent stays in "no preset" mode and can opt in later by editing `init.json` manually or re-running the wizard.
5. If multiple matches (rare) → pick the alphabetically first; warn in stderr.

This means **TUI-created agents automatically gain preset-swap capability after the next launch**, with their current LLM as the active preset. Users with custom configs are not forced into the system but can opt in.

**TUI's `GenerateInitJSONWithOpts` extension.** When the TUI creates a fresh agent from a preset, it now also writes `manifest.active_preset = preset.name` into the generated `init.json`. `presets_path` is omitted (defaults to global library). A few lines added to the existing function; no schema break.

**A future enhancement** — not in this spec — is a TUI command that lets users move presets from the global library to a project-local `<project>/.lingtai/presets/` folder and update `presets_path` accordingly. Useful for team-shared project libraries. Out of scope for now.

## Future Directions (deferred)

- **Provider pool / quiver.** Top-level `providers: { main: {...}, stealth: {...} }` with capabilities referencing pools by name. This is a deeper refactor; presets ship first and can later layer on top.
- **TUI preset library editor.** Browse and edit presets visually. The kernel design supports this fully (preset files are plain JSONC).
- **Cross-agent preset libraries.** A project-wide library at `<project>/.lingtai/presets/` that all agents in the project pull from. Today users can already achieve this by setting `presets_path: "../.lingtai/presets"` on each agent; standardizing it is a TUI/convention concern, not a kernel one.
- **Spawn-time preset for avatar.** `avatar(spawn, name="alice", preset="cheap")` — parent assigns avatar's initial preset. Trivial extension once presets ship.

## Naming reference

Across the spec, the following framing terms are used in agent-facing i18n descriptions:

- **胎光** (tāi guāng) — the LLM service. The mind-light of the agent.
- **肉身** (ròu shēn) — the capability set. The hands the agent uses.
- **行囊** (xíng náng) — the preset library. The agent's bag of implements.
- **易形换胎** — the swap act. Light, deliberate, identity-preserving.

The agent reads `refresh` schema description as: *"Re-read your config and rebuild yourself. Optional `preset` argument: pick a different bundle of mind-light (LLM) and hands (capabilities) from your library — like a practitioner reaching into their bag for a different set of implements. Each preset is a deliberate tradeoff between the two halves: a smarter LLM with fewer faculties, a weaker LLM with multi-modal vision, a fast model with stealth web tools. Read each preset's `comment` field to know what you'd gain and lose. The swap is light, takes one call, and reversible. You remain yourself; only your current implements change."*
