---
name: refresh-precheck
description: |
  Nested system-manual reference: the ordered pre-flight to run BEFORE
  `system(action="refresh")` (including preset swap/revert), the refresh
  sequence itself, and the post-refresh verification pass. Covers
  authorization boundary, allowed-preset catalog, MCP registry/init.json
  consistency, newly introduced env vars, context-vs-target-context_limit,
  durable-store and working-tree state, source_drift, and what to check when
  a refresh fails or comes back with a broken surface.
version: 1.1.2
last_changed_at: "2026-09-05T00:00:00Z"
tags: [lingtai, system, refresh, preset, precheck, checklist, mcp, env, pth, editable-install, verification, lifecycle]
related_files:
- src/lingtai/intrinsic_skills/system-manual/SKILL.md
- src/lingtai/intrinsic_skills/system-manual/reference/substrate-manual/SKILL.md
- src/lingtai/intrinsic_skills/system-manual/reference/runtime-update-checks/SKILL.md
- src/lingtai/intrinsic_skills/system-manual/reference/environment-variables/SKILL.md
- src/lingtai/intrinsic_skills/system-manual/reference/settings-inventory/SKILL.md
- src/lingtai/tools/context/manual/SKILL.md
- src/lingtai/prompts/substrate/substrate.md
- src/lingtai/prompts/procedures/procedures.md
- src/lingtai/tools/system/schema.py
- src/lingtai/tools/system/settings.py
- src/lingtai/kernel/presets.py
maintenance: |
  Sequencing-only node: every check here cites the owner that holds the fact
  (preset runtime model → substrate-manual §11; installer/update authority and
  runtime/version probe → runtime-update-checks; env var catalogue →
  environment-variables → root ENVIRONMENT_VARIABLES.md; MCP registry health →
  mcp-manual; molt/rebuild → context-manual). Do not restate an owned fact
  here — add or reorder a step and keep the citation. Update when
  `system(action="refresh")` / `system(action="presets")` semantics, the
  preset `allowed` gate, or the nudge kinds change.
---

# Refresh Pre-check — the ordered pre-flight for `system(action="refresh")`

`system(action="refresh")` is the highest-blast-radius routine action an agent
takes on itself: it rebuilds LLM/config, capabilities, MCP clients, addons,
prompt sections, and identity projection from `init.json`, optionally swapping
the active preset.

Every fact needed to run one safely is already owned somewhere — `substrate-manual`
§3/§11 (refresh and preset semantics), `runtime-update-checks` (authorization,
runtime/version probe, update/nudge lifecycle, and installer ownership),
`environment-variables` (env var catalogue), `mcp-manual` (registry
health), `context-manual` (molt/rebuild). What is *not* owned anywhere else is
the **ordering**: which check runs at the moment before you press the button,
and what you verify after. That is this node's only job. It adds sequencing,
not content; each step cites its owner rather than restating it.

## The pre-refresh checklist

Ordered. Steps 0–3 are unconditional; 4–8 are conditional and each names its
trigger. Every step states **check → command → refuse/proceed**.

### Step 0 — Authorization boundary (unconditional, blocking)

**Check:** does the refresh implement a config/prompt/MCP/preset change that a
human/config-owner authorized, or is it a self-initiated reload?

- Any refresh that **applies** an update, migration, or configuration write requires
  explicit human/config-owner authority *before* the write and *before* the refresh
  (`runtime-update-checks` steps 8–10). A nudge is a fact, not a command,
  and never grants authority.
- A refresh that could **interrupt active work** (running daemons, an in-flight
  collaboration, a peer waiting on a reply) is gated by work safety and the human's
  intent even when nothing is being written.
- A pure self-reload with nothing pending and no active work is the only case that does
  not need a fresh ask.

**If the trigger is a `kernel_version` nudge:** stop. That is the installer's route —
let Shell run `https://lingtai.ai/install.sh --help` and follow *its* current output.
Do not read or paste the script source. Refresh is the last step after authorized
writes are validated, never the first.

### Step 1 — Know what you are actually running (unconditional)

```bash
# POSIX. Uses only the exported runtime interpreter; never guesses a path.
PYTHON="$LINGTAI_RUNTIME_PYTHON"
[ -n "$PYTHON" ] || { echo "LINGTAI_RUNTIME_PYTHON is unset — stop; locate the actual launcher/runtime venv via runtime-update-checks instead of guessing one." >&2; return 1 2>/dev/null || exit 1; }
"$PYTHON" -c 'import sys, lingtai, lingtai.kernel; print(sys.executable); print(lingtai.__file__); print(lingtai.kernel.__file__); print(getattr(lingtai,"__version__","unknown"))'
```

Refresh reloads the **current on-disk/runtime surface**; it does not fetch code, pull a
commit, switch an editable checkout, or install a package. If you cannot say which
interpreter and which module files are authoritative, **stop and ask** — do not refresh
hoping it resolves the ambiguity.

### Step 2 — `.pth` / editable-install occupancy (unconditional, blocking on mismatch)

Step 1 says *which* files import. This step says *why* — and it is the check that catches a
refresh-time landmine before it fires.

```bash
SP=$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
ls -1 "$SP" | grep -Ei '^(__editable__|__editable___)?[._]*lingtai.*\.(pth|dist-info|egg-link)$|^lingtai.*'
for f in "$SP"/*lingtai*.pth; do echo "== $f"; cat "$f"; done
cat "$SP"/lingtai-*.dist-info/direct_url.json 2>/dev/null
```

**Check, in order:**

1. **Exactly one lingtai path marker.** `__editable__.lingtai-*.pth` /
   `__editable___lingtai*.pth` / `lingtai*.pth` / `lingtai.egg-link`. Two markers, or a
   `.pth` alongside a real `site-packages/lingtai/` package directory, means pip left both
   an editable and a non-editable install — imports then resolve by `sys.path` order, not
   by intent.
2. **Every path inside each `.pth` exists.** A residual marker from a removed release
   points at a deleted checkout; the entry is silently skipped, and the *next* candidate on
   `sys.path` wins. Same for an orphaned `lingtai-*.dist-info` with no matching `.pth`.
3. **The marker matches the intended install kind.** `~/.lingtai-tui/install.json`
   declares it (`install_kind`, `kernel_source`, `kernel_source_path`); `direct_url.json`
   in the dist-info records what pip actually did (`{"dir_info": {"editable": true}}` for
   an editable). Editable → the `.pth` must point at the source checkout's `src/`.
   Non-editable → there must be **no** `.pth` and a real package dir instead.
4. **Cross-check against step 1's `lingtai.__file__`.** If the `.pth` names one source and
   the import resolves elsewhere, do not refresh — the refresh will load whichever one
   `sys.path` happens to favour, and you will have no evidence of which. Resolve the
   provenance first.

Resolve the interpreter from the exported `LINGTAI_RUNTIME_PYTHON` only and read
module `__file__`, never a PATH `python` and never a guessed venv path — if it
is unset, stop and locate the actual launcher via `runtime-update-checks`
rather than assuming a default venv location; that rule is owned by
`runtime-update-checks`, this step only sequences it.

**Refuse to refresh if:** more than one lingtai path marker is live; any `.pth` entry does
not exist on disk; or the marker and `lingtai.__file__` disagree. Repairing an install is a
pip/installer action, not a refresh — it needs the same authority as step 0.

### Step 3 — Config parses and is internally consistent (unconditional if anything was edited)

```bash
"$PYTHON" -c 'import json,sys; d=json.load(open("init.json")); print("init.json parses ok"); print("addons:", d.get("addons")); print("mcp:", list((d.get("mcp") or {}).keys()))'
```

Then cross-check against the registry:

```text
mcp(action="info", input={}, reasoning="pre-refresh registry health and problems")
```

**Refuse to refresh if:** `init.json` does not parse (a refresh on unparseable config is
how you get a broken surface with no easy way back); the `mcp` block names an entry with
no corresponding registry record (stale/duplicate surface); or `mcp(action="info")`
reports `problems` you have not explained. Fix the config *first*, then refresh once.

Remember the direction of causation: **a config/prompt/MCP/capability edit needs
`refresh` to take effect. `context(action="summarize")` never applies config and is not
a refresh substitute.**

### Step 4 — Preset target validity (trigger: `preset` or `revert_preset` in the call)

```text
system(action="presets", input={}, reasoning="confirm exact allowed preset paths before swap")
```

Use only an exact path from this call's result. See `substrate-manual` §11
for the allowed-only catalog mechanics, the `preset`/`revert_preset` conflict
rule, and `revert_preset`'s default-lookup behavior — this step only
sequences the check before activation, it does not restate those rules.

### Step 5 — Context fits the target's `context_limit` (trigger: preset swap)

**Check:** current context usage (`agent_meta.agent_state.context`) against the target
preset's `context_limit` as reported by `system(action="presets")`.

If current context exceeds the target's limit, **the swap is refused before activation
— molt first.** Do not discover this by attempting the swap; a refused swap after you
have already told a human "switching now" is a self-inflicted incident. Order is:
tend durable stores → `context(action="molt", …)` → re-check → swap.

The cache-miss total accumulates **since last molt and survives a refresh** —
refreshing does not reset it, so do not expect this precheck's refresh to clear
it. Inspect the live value through `system(action="settings", input={})`; see
`reference/settings-inventory/SKILL.md` → "Cache-miss budget" for its exact
source precedence and document shape. The owner is `settings/system.json`, the
fixed fallback is the `2,000,000` default, and Legacy `manifest.cache_miss_budget` is ignored.

### Step 6 — Newly introduced environment variables (trigger: the change adds an env read)

**Check three things, in this order:**

1. **Is the variable documented?** New reads belong in
   `system-manual → reference/environment-variables/SKILL.md` (purpose, default,
   accepted values, scope, read point, reload behavior, invalid-value handling).
2. **Will the running agent actually see it?** `env_file`/`venv_path` are resolved at
   **boot**; refresh/restart *reuse* that resolution. A variable added to a file the
   process never loaded will not appear just because you refreshed. Confirm the read
   point, not the file's existence.
3. **Is the test suite hermetic against it?** If the ambient environment
   sets the variable, tests that assume the default will pass or fail depending on the
   shell they run in. The suite needs an autouse fixture clearing it. This check belongs
   in the *change*, before the refresh, not after a confusing CI result.

### Step 7 — Durable-store and working-tree state (unconditional, cheap)

Refresh preserves identity and conversation. It is **not** a save operation for anything
else. Before refreshing:

- Deposit anything that should outlive this runtime surface into pad / knowledge /
  skills / character — durable stores are written with `file.write` / `file.edit`, and a
  refresh is not a substitute for having written them.
- Check for uncommitted work in any checkout you are mid-edit on
  (`git -C <checkout> status --short --branch`). Refresh does not commit, stash, or
  protect a dirty tree.
- Check for in-flight daemon work. Ordinary stop/refresh must not terminate active
  daemon runs, but a refresh mid-batch changes the parent surface those runs report
  back into — prefer to let a batch finish, or know exactly why you are not.
- If context is near the limit, tend durable stores **now**, not after (step 5).

### Step 8 — Known breaking-change nudge (unconditional, read-only)

```bash
ls -l .notification/nudge.json 2>/dev/null && sed -n '1,240p' .notification/nudge.json
```

- **`source_drift`** (channel `source_integrity`): the running process differs from code
  on disk. This is exactly the case where refresh *is* the right action — and also the
  case where you should know what changed before you load it. It stays local to refresh
  mechanics and never enters release-migration routing.
- **`init_config_shape`** (channel `configuration_staleness`): configuration-staleness
  guidance. Read it before refreshing, not after.
- **`kernel_version`** (channel `release_version`): **not** a refresh trigger by itself —
  see step 0.

## The refresh sequence

### Before

1. Complete steps 0–8 above. Anything that says *refuse* is a stop, not a warning.
2. If near the context limit or swapping to a smaller preset: tend durable stores, then
   molt, then re-check current context against the target's `context_limit`.
3. Tell the human what you are about to reload and why, if the refresh was their ask or
   could interrupt their work.

### During

**Exactly one call.** Do not stack refreshes; do not "refresh again to be sure."

```text
system(action="refresh", input={"reason": "<what config change this applies>"},
       reasoning="apply the authorized <X> edit")
```

or, for a swap:

```text
system(action="refresh", input={"preset": "<exact path from system(action='presets')>"},
       reasoning="swap to <preset> for <task>")
```

or, to go home:

```text
system(action="refresh", input={"revert_preset": true},
       reasoning="return to default preset after experimenting")
```

The refresh path checks `allowed` membership → checks target context limit → activates
atomically (writing raw `init.json`) → persists the new default for a named swap →
best-effort retries failed MCPs → rebuilds LLM/config/capabilities/MCP/prompts,
preserving conversation history where a live session exists.

Note: refresh requests a **deferred relaunch** and only when the runtime can build a
valid launch command and has a configured refresh watcher. Without a launch command it
returns *without relaunching*; without a refresh watcher it raises. **Neither of those
is success** — diagnose before believing the refresh happened.

### After (verification — this is not optional)

1. **Confirm you are the new process, from the intended source.** Re-run the step-1
   interpreter/import probe *and* the step-2 `.pth` listing. Same commands, new process;
   compare. `lingtai.__file__` must resolve to the intended source, and no stale or
   duplicate lingtai path marker may have appeared.
2. **Confirm the MCP surface.** `mcp(action="info", input={}, reasoning="post-refresh
   verification")` — count, `problems` empty, and the specific tools you expected
   actually present. A config that looked right is exactly the case where the surface
   still does not match.
3. **Confirm the preset.** `system(action="presets", input={}, …)` and/or read the
   derived `system/manifest.resolved.json` — the fully materialized, validated,
   path-resolved running configuration. It is derived and read-only; never write it.
4. **Confirm the tool surface.** The tool list and LLM may have changed. Do not assume
   the tool you were about to call still exists at the same shape.
5. **Re-check daemon allowed presets before dispatching.** If you are about
   to dispatch daemon work with an explicit `tasks[].preset`, re-read
   `system(action="presets")` *after* the refresh and pass an exact path from that list.
   An unauthorized path refuses the **whole batch** before load, connectivity probing,
   capability construction, run-dir creation, scheduling, or dispatch.
6. **Then, and only then**, dismiss the nudge that motivated the refresh —
   `notification(action="dismiss_channel", input={"channel": "nudge", …})`. Dismissing
   before verifying loses the evidence.

## Failure handling

| Symptom | First check | Then |
|---|---|---|
| Refresh returned but nothing changed | Re-run the interpreter/import probe in the new process | A refresh only loads code already on disk. It cannot pull a commit or repair an incomplete checkout. Check for a still-held old process or a different environment. |
| `lingtai` imports from the wrong source, or an edit to the checkout has no effect | Step 2 — list `*lingtai*.pth` + `lingtai-*.dist-info` in the runtime venv's site-packages and compare each `.pth` entry against `lingtai.__file__` | Two markers (editable + non-editable) → `sys.path` order decides, not intent. A `.pth` naming a path that no longer exists → the entry is skipped and the next candidate wins. Orphaned `dist-info` with no `.pth` → stale metadata reporting a version nothing imports. Repair the install via pip/installer with step-0 authority; do **not** refresh against a mismatched marker. |
| Refresh returned without relaunching | Can the runtime build a valid launch command? Is a refresh watcher configured? | Missing launch command → returns silently; missing watcher → raises. Diagnose the launcher, do not retry blindly. |
| Refresh raised | The raised error text first | Then `init.json` parse, then `allowed` membership, then target `context_limit` — these are the three gates that reject *before* any runtime change (so the old surface is intact). |
| Preset swap refused | Was the path in `system(action="presets")` output? Does current context fit the target `context_limit`? | Unauthorized path → ask the config owner to add it to `manifest.preset.allowed`, then refresh, then re-verify with `presets`. Context too large → molt first. |
| Broken/missing MCP surface after refresh | `mcp(action="info")` `problems` list; `init.json` `mcp` block vs. `mcp_registry.jsonl` records | Fix the registry/config, then refresh **once** more. Do not loop refreshes against an unfixed config. |
| Active preset file missing or malformed | Which one? | Missing → materialization may fall back to a different loadable default (so the running preset may not be the one you think). Malformed **existing active** preset → materialization fails rather than silently substituting. Read `system/manifest.resolved.json` to see what actually loaded. |
| Wrong preset is live and you want out | — | `system(action="refresh", input={"revert_preset": true})` — the home button to `manifest.preset.default`. Cannot be combined with `preset`. |
| Terminal relaunch failure | `logs/refresh_failed_permanent.json` and the high-priority `.notification/system.json` event the kernel writes on permanent relaunch failure | This is a human-escalation condition, not a retry condition. |
| Mixed/contradictory evidence (offline? stale paths? heartbeat vs. status disagree?) | `lingtai-doctor` — read-only, redacts secrets, never edits | Diagnose before repair; repairs belong to the owning manual. |

**When to ask the human — bright lines.** Ask, do not improvise, when: (a) the
interpreter/import path is ambiguous; (b) `manifest.preset.allowed` needs a new entry
(the agent cannot authorize itself, and a daemon call cannot mutate `allowed`); (c) any
migration or `init.json` write beyond an explicit preset activation is implied; (d) a
`kernel_version` nudge suggests an install/update; (e) two refreshes have not produced
the expected surface — the third attempt is not the answer.

## Scope guard — what this node must NOT do

1. **It is not the installer or update route.** It never instructs a download, install,
   `pip install --upgrade`, package switch, or version migration. The single official
   route is `https://lingtai.ai/install.sh`, and `runtime-update-checks` owns that
   routing. This node's `source_drift` handling stays local and never enters
   release-migration routing.
2. **It performs no automatic migrations and no config writes.** It is a checklist the
   agent *reads*; it does not ship a script that edits `init.json`,
   `mcp_registry.jsonl`, `manifest.preset.allowed`, or any durable store.
3. **It grants no authorization.** Completing every check does not substitute for the
   human/config-owner authority required by `runtime-update-checks`. A checklist is not
   consent. Explicitly: "the pre-flight passed" is never a reason to skip asking.
4. **It does not restate owned facts.** Preset runtime model → `substrate-manual` §11.
   Runtime/version provenance probe (`LINGTAI_RUNTIME_PYTHON` / module `__file__`, never a
   PATH `python`) → `runtime-update-checks`; step 2 sequences that rule, it does not restate it.
   Env var catalogue → `environment-variables`. Update/nudge lifecycle →
   `runtime-update-checks`. MCP registry mechanics → `mcp-manual`. Molt/rebuild →
   `context-manual`. Notification dismissal safety → `notification-manual`. Each check
   *cites*; duplication here means four places to update and three that will go stale.
5. **It does not replace `context(action="rebuild")`.** Rebuild is the active
   full-context reconstruction operation. Refresh is a lifecycle operation with broader
   effects. Do not reach for refresh merely to apply a summary — that boundary is
   already stated in the resident prompts and must be reinforced, not re-litigated, here.
6. **It is not a gate in code.** Nothing in the kernel enforces it; this is strong
   guidance an agent is expected to follow, not a runtime refusal.

## Recipes (copy-paste)

### Recipe A — Pre-refresh health check (run before any config-motivated refresh)

```bash
# POSIX. Uses only the exported runtime interpreter; never guesses a path.
PYTHON="$LINGTAI_RUNTIME_PYTHON"
[ -n "$PYTHON" ] || { echo "LINGTAI_RUNTIME_PYTHON is unset — stop; locate the actual launcher/runtime venv via runtime-update-checks instead of guessing one." >&2; return 1 2>/dev/null || exit 1; }
"$PYTHON" -c 'import sys, lingtai, lingtai.kernel; print("py=",sys.executable); print("lingtai=",lingtai.__file__); print("kernel=",lingtai.kernel.__file__)'
SP=$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
ls -1d "$SP"/*lingtai* 2>/dev/null; for f in "$SP"/*lingtai*.pth; do echo "== $f"; cat "$f"; done
"$PYTHON" -c 'import json; d=json.load(open("init.json")); print("init.json OK"); print("addons=",d.get("addons")); print("mcp=",list((d.get("mcp") or {}).keys()))'
ls -l .notification/nudge.json 2>/dev/null && sed -n '1,120p' .notification/nudge.json
git -C "$(pwd)" status --short --branch 2>/dev/null || true
```

```text
mcp(action="info", input={}, reasoning="pre-refresh registry health and problems")
```

Proceed only if: interpreter unambiguous, exactly one lingtai path marker whose entries
exist and agree with `lingtai.__file__`, `init.json` parses, `mcp` block matches the
registry, `problems` empty or explained, and no `kernel_version` nudge is the real
trigger.

### Recipe B — Daemon-preset preflight (before dispatching daemon work with an explicit preset)

```text
1. system(action="presets", input={}, reasoning="read the allowed-only catalog")
2. # If the path you want is absent, it is NOT authorized (allowed-catalog
   #   mechanics: substrate-manual §11). Ask the config owner to add it, then:
   system(action="refresh", input={"reason": "pick up new allowed preset entry"},
          reasoning="apply the config owner's allowed-list edit")
3. system(action="presets", input={}, reasoning="confirm the new path is now listed")
4. # Pass the EXACT path from step 3 as tasks[].preset — not the pre-authorization
   #   string, not a library-screen path, not a shorthand.
```

An unauthorized explicit `tasks[].preset` refuses the **entire batch** before dispatch.
Omitting `preset` entirely inherits the parent's regular surface and skips this check.
External CLI backends (`claude-p`, `codex`, `opencode`, …) skip LingTai preset
resolution entirely — this recipe does not apply to them.

### Recipe C — Post-refresh verification (always, immediately after refresh returns)

```bash
# POSIX. Uses only the exported runtime interpreter; never guesses a path.
PYTHON="$LINGTAI_RUNTIME_PYTHON"
[ -n "$PYTHON" ] || { echo "LINGTAI_RUNTIME_PYTHON is unset — stop; locate the actual launcher/runtime venv via runtime-update-checks instead of guessing one." >&2; return 1 2>/dev/null || exit 1; }
"$PYTHON" -c 'import sys, lingtai, lingtai.kernel; print(sys.executable, lingtai.__file__, lingtai.kernel.__file__)'
SP=$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'); ls -1d "$SP"/*lingtai*
sed -n '1,200p' system/manifest.resolved.json 2>/dev/null
```

```text
mcp(action="info", input={}, reasoning="post-refresh: confirm MCP tools actually loaded")
system(action="presets", input={}, reasoning="post-refresh: confirm active preset and allowed set")
```

Confirm, in order: new process importing from the intended source with no stale/duplicate
lingtai `.pth` → MCP count/`problems`/expected tools present → active
preset is what you asked for → the specific tool you are about to use still exists.
Only then dismiss the motivating nudge.
