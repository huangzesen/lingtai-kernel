# Presets — Design

**Date:** 2026-04-28
**Status:** Draft for review
**Scope:** kernel (`lingtai-kernel`) only — TUI/portal unaffected

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

## Architecture

### Preset library on disk

A preset is a single JSONC file in a folder pointed to by `manifest.presets_path` (relative to the agent's working dir, or absolute). Auto-discovery rules:

- File extension `.json` or `.jsonc` → treated as a preset.
- Filename stem (without extension) is the preset's name. `cheap.json` → preset `"cheap"`.
- Subdirectories ignored. Only top-level files in `presets_path` are scanned.
- Non-`.json[c]` files (e.g. `README.md`) are ignored — useful for documenting the library.

Each preset file's structure:

```jsonc
{
  // Forward-compatible structured comment. The kernel surfaces this verbatim
  // to the agent via system(action="presets") and does not validate keys.
  "comment": {
    "summary": "Cheap reasoning, no vision",
    "tradeoffs": ["faster", "cheaper", "no multi-modal"],
    "recommended_for": ["bulk file scans", "boilerplate writes"]
  },

  "llm": {
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "api_key_env": "DEEPSEEK_API_KEY",
    "base_url": "https://api.deepseek.com",
    "api_compat": "openai"
  },

  "capabilities": {
    "read": {}, "write": {}, "edit": {},
    "grep": {}, "glob": {}, "bash": { "yolo": true },
    "email": {}, "codex": {}, "library": { "paths": ["../.library_shared"] },
    "psyche": {}, "avatar": {}, "daemon": {},

    "web_search": { "provider": "inherit" },
    "web_read":   { "provider": "inherit" },
    "vision":     { "provider": "inherit" },
    "listen":     { "provider": "whisper" }
  }
}
```

`comment` is optional. Its keys are not validated; new keys are forward-compatible. Suggested keys: `summary`, `tradeoffs`, `recommended_for`, `not_recommended_for`, `cost_tier`, `notes`.

### `init.json` changes

Two new optional fields under `manifest`:

```jsonc
{
  "manifest": {
    "presets_path": "./presets",       // path to preset library folder
    "active_preset": "default",        // name of preset currently materialized below

    // The kernel still reads these directly. They are the materialization
    // of <presets_path>/<active_preset>.json after `inherit` resolution.
    "llm":          { /* materialized */ },
    "capabilities": { /* materialized */ },

    /* ...all other existing fields unchanged... */
  }
}
```

Backward compatibility:

- An `init.json` with no `presets_path` and no `active_preset` works exactly as today. The kernel treats `manifest.llm` and `manifest.capabilities` as authoritative and never touches the (nonexistent) preset library.
- An `init.json` with `presets_path` set must also have `active_preset` set, and the named preset must exist on disk. Validation error otherwise.
- An `init.json` with `active_preset` set but no `presets_path` is a validation error (orphan pointer).

When both are set, `manifest.llm` and `manifest.capabilities` reflect the materialization of the active preset. The agent's runtime always reads these fields, never the preset file directly. This means **what you see in `init.json` is what the agent is running** — there is no hidden indirection. The preset library is the source; `init.json` is the currently-loaded incarnation.

### The `"inherit"` sentinel

Capabilities that today take a `provider` kwarg (`web_search`, `web_read`, `vision`, `listen`, `compose`, etc.) gain support for the literal value `"inherit"`. When the kernel resolves a capability config and sees `"provider": "inherit"`, it expands the config in-place using the main LLM's settings:

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

1. Read the current `init.json`.
2. Read `<presets_path>/<name>.json[c]` (raise `KeyError` if missing).
3. Validate the preset shape (presence of `llm`, type of `capabilities`).
4. Substitute: `manifest.llm = preset.llm`; `manifest.capabilities = preset.capabilities`; `manifest.active_preset = name`.
5. Write atomically via `tmp + os.replace`.
6. Return.

If step 5 fails (disk write error), no in-memory state has been touched yet. The agent simply hasn't swapped. If step 6 returns successfully but `_perform_refresh()` then fails, the on-disk state reflects the new preset but the running agent may be in a partial-rebuild state — same failure mode as a plain refresh that fails. This is acceptable because the next refresh will reconcile.

### `presets` action

A new top-level action under `system`:

```jsonc
{ "action": "presets" }
```

Returns:

```jsonc
{
  "status": "ok",
  "active": "default",
  "available": [
    {
      "name": "default",
      "comment": { "summary": "main daily-driver", ... },
      "llm": { "provider": "gemini", "model": "gemini-2.5-pro" },
      "capabilities": ["read", "write", "edit", "web_search", "vision", ...]
    },
    {
      "name": "cheap",
      "comment": { "summary": "Cheap reasoning, no vision", ... },
      "llm": { "provider": "deepseek", "model": "deepseek-v4-pro" },
      "capabilities": [...]
    }
  ]
}
```

`comment` is surfaced verbatim. `llm` is summarized to `{provider, model}` only (no credentials). `capabilities` is the list of enabled capability names from the preset (does not pre-resolve `inherit` or graceful-fallback — the agent sees what's *configured*, not what would actually load).

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

- `src/lingtai/init_schema.py` — accept `manifest.presets_path` (str) and `manifest.active_preset` (str). Validation: both-or-neither; if both, file must exist.
- `src/lingtai/agent.py` — new method `_activate_preset(name)` (writes init.json with materialized preset). `_perform_refresh()` already does the rest.
- `src/lingtai_kernel/intrinsics/system.py` — `_refresh` accepts new `preset` arg. New `_presets` handler. Schema enum gains `"presets"`. Schema gains `preset` property.
- `src/lingtai/capabilities/web_search/__init__.py` (and `web_read`, `vision`, `listen`, `compose`) — add `fallback_on_inherit` to the module-level provider registry; teach `setup()` to honor the inherit-failure-skip-silently contract.
- `src/lingtai/core/avatar/__init__.py` — when writing the avatar's `init.json`, propagate `presets_path` (made absolute) and `active_preset`.
- `src/lingtai/i18n/{en,zh,wen}.json` — descriptions for the new action and new arg.

### Unchanged

- `src/lingtai/core/daemon/__init__.py` — daemons already inherit parent's preset by virtue of reading `manifest.llm`/`manifest.capabilities`. Nothing to change.
- TUI / portal — no awareness needed. They read `manifest.llm.model` for display, and after a swap that reads the new model. Filesystem-only contract preserved.

## Data Flow

### Agent calls `refresh(preset="cheap")`

```
1. agent's tool dispatcher routes to system._refresh(args)
2. _refresh sees preset="cheap"
3. _refresh calls agent._activate_preset("cheap")
   3a. Read current init.json from disk
   3b. Read presets/cheap.json from disk
   3c. Validate preset shape
   3d. Substitute manifest.llm, manifest.capabilities, manifest.active_preset
   3e. Atomically write init.json (tmp + os.replace)
4. _refresh calls agent._perform_refresh()
   4a. _perform_refresh re-reads init.json (now reflecting cheap)
   4b. expand_inherit() on manifest.capabilities using new manifest.llm
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
4. For each, load_preset() -> read comment, llm summary, capabilities list
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
| `presets_path` not set, `active_preset` set | init.json validation error at load time |
| `presets_path` points to nonexistent dir | init.json validation error at load time |
| `active_preset` names a preset file that doesn't exist | init.json validation error at load time |
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
- `system(action="presets")` → returns active + available with comments.

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

No migration needed. Existing `init.json` files without `presets_path` continue to work unchanged. Users opt in by:

1. Creating a `presets/` folder under their working dir.
2. Moving their current `manifest.llm` + `manifest.capabilities` into `presets/default.json`.
3. Adding `presets_path: "./presets"` and `active_preset: "default"` to `manifest`.
4. (Optional) Authoring additional presets.

A future TUI migration can offer to do steps 1–3 automatically, but is out of scope for the kernel spec.

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
