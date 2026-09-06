---
name: soul-contract
tool: soul
contract_version: 3
root_contract: CONTRACT.md
related_files:
  - src/lingtai/tools/soul/BEHAVIORS.md
  - src/lingtai/tools/soul/__init__.py
  - src/lingtai/tools/soul/config.py
  - src/lingtai/tools/soul/settings.py
  - src/lingtai/tools/soul/manual/SKILL.md
  - src/lingtai/tools/soul/manual/reference/flow.md
  - src/lingtai/tools/soul/manual/reference/configuration.md
  - src/lingtai/tools/soul/manual/reference/consultation.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/agent.py
  - tests/test_tool_plugin_declaration.py
  - src/lingtai/tools/soul/ANATOMY.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/tool_family/CONTRACT.md
  - src/lingtai/kernel/tool_result_summary.py
  - tests/test_tool_family_soul_migration.py
  - tests/test_soul_settings.py
maintenance: |
  Keep related_files as repo-relative paths to real files. If behavior and this
  contract disagree, the code is the source of truth — fix the contract in the
  same change and bump contract_version on breaking contract edits. Soul is an
  official declared host plugin: keep its declaration, `SoulRuntimePort`,
  adapter, packaged manual, and compact live proof together with this contract.
---

# Soul capability contract

`soul` is the agent's inner voice: on-demand past-self `inquiry`, mechanical
periodic `flow` consultation, cadence/voice `config`, a `dismiss` for the
soul-flow notification, and read-only `settings` discovery. The implementation
lives in `src/lingtai/tools/soul/`; the code is the source of truth.

`soul` is migrated to the LingTai Tool Protocol v2 shape defined in
`src/lingtai/tools/CONTRACT.md` and builds its schema composition and envelope
dispatch on the generic `src/lingtai/tools/tool_family/` infrastructure. The
generic settings contract adds one reserved SHOW action without changing the
existing six action values, their success/error payloads, log events, or
persistence paths. The settings action owns no writer.

## Routing Card
Guarded by: [SU001](BEHAVIORS.md#behavior-su001),
[SU002](BEHAVIORS.md#behavior-su002)


**Use this when:**
- You are editing the sync `inquiry` mirror session, the periodic `flow`
  consultation fire, or the soul cadence/voice config knobs.
- You are reviewing how soul voices reach the agent (via `.notification/
  soul.json`) and how flow is opt-in gated.

**Do not use this for:**
- General notification reads: use the `notification` tool
  (`src/lingtai/tools/notification/CONTRACT.md`). `soul(action='dismiss')` is a thin
  wrapper that clears only the `soul` channel via the shared helper.
- Context molt / summarize: those are `psyche` and `system`
  (`src/lingtai/tools/context/CONTRACT.md`, `src/lingtai/tools/system/CONTRACT.md`). Soul reads
  molt snapshots as consultation substrate but does not create them.
- Code navigation only: read `src/lingtai/tools/soul/ANATOMY.md`.

**Fast paths:** action list -> §Tool surface; flow opt-in gate -> §Anchored
claims; log/notification paths -> §State & storage.

## Scope

- Canonical tool name: `soul`.
- The root property set is exactly `action`, `input`, `reasoning`, and
  `summarize`, with `additionalProperties: false`. `action`, `input`, and
  `reasoning` are required; `summarize` is optional Host presentation and is
  never action input. The action enum is `inquiry`, `flow`, `config`, `voice`,
  `dismiss`, `settings`, `manual` — one canonical child each, where the child's
  name is simultaneously the public action value and the dispatch key.
- Each action owns one strict, closed `input` object. Declared optional fields
  use the provider-compatible nullable representation (`["number", "null"]`
  etc.); null means "absent" at dispatch.
- `summarize` guidance profile: **short-result** for every action — soul's
  payloads are small, so leave it false. Call `manual` with `summarize=false`
  so the exact enable/disable procedure is not summarized away.
- `flow` is opt-in via an environment variable and disabled by default; the
  agent-invoked call only *triggers* a fire — voices arrive asynchronously.
- Non-goals: general notification verbs, molt/summarize, mailbox actions.

## Declared host plugin

`DECLARATION` is static at import and owns Soul's operational actions
`inquiry | flow | config | voice | dismiss`; the reserved read-only `settings`
and `manual` children are appended by the generic contract, with the latter
using package-owned `manual/SKILL.md` as `soul-manual`. `_bind(host)` derives
its name, input schemas, and manual destination from that declaration and gets
only `workdir` plus `soul_runtime`. `AgentSoulRuntimeAdapter` implements the
latter as the explicit self-state/flow vocabulary Soul consumes — current
chat/session/config/service, cadence/lock state, logging, and Soul notification
operations — never a whole Agent or generic mount capability.

The injected Soul module remains available only for kernel lifecycle hooks.
`Agent` removes its temporary intrinsic dispatcher entry and mounts the public
root through `register_agent_tool_plugins`; refresh repeats the controlled mount.
The public name, existing six ordered actions, strict inputs, flow gate,
persistence, results/errors, and historical
`.library/intrinsic/capabilities/soul-manual/` manual path are unchanged. The
reserved `settings` child is additive. Any second operational owner for that
destination must fail loudly rather than be resolved by scan order.

## Tool surface

Schema and dispatch both live in `src/lingtai/tools/soul/__init__.py`
(`get_schema`, `handle`).

Inputs below are fields of that action's own `input` object, never of the root.

| Action | Required `input` fields | Optional `input` fields | Success output | Error shapes |
|---|---|---|---|---|
| `inquiry` | `inquiry` (non-empty str) | — | `{status: "ok", voice}` (or `voice: "(silence)"`) | `{error: "inquiry is required ..."}` |
| `flow` | — (empty `input`) | — | `{status: "ok", message}` when triggered; `{status: "disabled", enabled: False, env_var, message}` when opt-out | `{error: "soul flow ongoing, request rejected"}` when a fire is already in flight |
| `config` | at least one non-null of `delay_seconds`, `consultation_past_count` | the other knob (nullable) | `{status: "ok", old, new}` (+ `soul_flow_enabled`/`note` when flow disabled) | `{error: "config requires at least one of ..."}`; range/type `{error}` for each field |
| `voice` | — (read: both nullable fields null) | `set` (`inner`/`observer`/`custom`), `prompt` (required for `custom`) | `{status: "ok", current, available, prompt, ...}` | `{error: "set must be a string ..."}`; `{error: "Unknown voice profile: ..."}`; `{error: "set='custom' requires a non-empty 'prompt' ..."}`; `{error: "prompt is too long ..."}` |
| `dismiss` | — (empty `input`) | — | `{status: "ok", message}` (delegates to `dismiss_channel(soul)`) | dismissal `{status: "error", ...}` from the shared helper |
| `settings` | — (strict empty `input`) | — | `{settings: [{key, current, default, configurable, comment}, ...]}` with exactly five Soul rows | one fixed no-row failure for unavailable/malformed/unserializable provider truth; fixed oversize failure |
| `manual` | — (strict empty `input`) | — | flat `{status, manual, manual_path}` (+ `error` when the manual is missing) | `{status: "degraded", ..., error: "soul-manual manual missing ..."}` |

An unknown/absent `action` returns `{error: "Unknown soul action: ..."}`.
`flow`'s disabled/ongoing paths return **before** spawning any fire thread.
`manual` performs **no** soul operation: it reads the installed manual and
touches no timer, lock, consultation, config, voice, or notification state.

The `settings` action is also read-only and takes exactly `input={}`. Its five
rows are `flow_enabled`, `delay_seconds`, `consultation_past_count`, `voice`,
and `voice_prompt`, in that order. Every successful row projects exactly
`key`, `current`, `default`, `configurable`, and an exact `soul-manual#...`
section pointer in `comment`; the custom prompt's current/default values are
fully redacted. Current truth comes fresh from the live runtime and the same
process flow gate the owner consumes. If any current value is unavailable or
not JSON-safe, the provider raises and the generic action returns one bounded
failure with no partial rows. `configurable` means the launcher, existing
`config`, or existing atomic `voice` procedure can change the value; SHOW
itself has no set/reset operation.

### Envelope enforcement

- The root `allOf` correlates each `action` const with that action's exact
  `input` schema, so a provider that enforces `allOf`/`if`/`then` can reject a
  mismatched pairing before invocation; `input.anyOf` discloses every action's
  exact shape in one place.
- Dispatch remains the always-authoritative, fail-closed boundary. An `input`
  key belonging to another action's branch (e.g. `action='inquiry'` with
  `input={'delay_seconds': 60}`) is rejected with
  `{status: "failed", error_code: "INVALID_ARGUMENT", message: "unsupported soul input field"}`
  **before** any handler I/O — no LLM call, no config write, no log event.
- A non-boolean `summarize`, an unknown root field, and a non-object `input`
  each fail with a stable typed `INVALID_ARGUMENT` envelope error.
- `reasoning`, `_reasoning`, and `summarize` never reach a child handler.
  Neither does `_tc_id`, the transport metadata `base_agent._dispatch_tool`
  injects into every intrinsic's args; soul drops it before the closed-root
  check and does not consume it.
- `soul` is listed in `_LTP_V2_MIGRATED_FAMILIES`
  (`src/lingtai/kernel/tool_result_summary.py`), so the canonical root
  `summarize` spelling is recognized as the a-priori summary control for this
  family, and the generic dispatcher's `status: "failed"` envelope errors are
  never summarized away.
- No action and no input field anywhere in this family can enable soul flow.
  The opt-in gate is the operator's `LINGTAI_SOUL_FLOW_ENABLED` env var alone;
  `config` tunes cadence only and says so in its own result.

### Synthesized involuntary flow pair

The soul-flow fire appends a synthesized `(ToolCallBlock, ToolResultBlock)`
pair to chat history (`consultation.build_consultation_pair`). That call block
is replayed to the provider as an assistant `tool_use` block, so it is a
**model-visible example of how to call `soul`** and MUST carry the same
envelope the schema advertises: `action: "flow"`, strict empty `input`, and a
Host-authored `reasoning` (`INVOLUNTARY_FLOW_REASONING`) stating plainly that
the agent did not initiate the call. An involuntary fire has no agent rationale
to record, so the constant states that fact rather than inventing one.

This is not cosmetic: a model imitating its own history and sending the
pre-migration flat `{"action": "flow"}` succeeded before the migration and now
fails with `INVALID_ARGUMENT`. Any future producer of synthesized `soul` calls
carries the same obligation.

Appendix-pair detection (`flow._rehydrate_appendix_tracking`) reads only
`args.get("action")`, so it recognizes both pre- and post-migration history
without a shape branch. That is a read path over existing history, **not** a
second accepted call shape: nothing in dispatch admits the flat form.

## State & storage

Paths are relative to the agent working directory (`agent._working_dir`).

```text
.notification/soul.json     — where flow/inquiry voices are published for the kernel to surface
logs/soul_flow.jsonl        — append-only record of every soul entry; the `mode` field
                              distinguishes "flow" from "inquiry" (there is no separate
                              soul_inquiry.jsonl)
history/snapshots/          — past-self snapshots sampled as flow consultation substrate (read-only here)
init.json                   — manifest.soul persistence for config (delay_seconds,
                              consultation_past_count) and voice (soul_voice, soul_voice_prompt)
```

- `flow` fires `M = 1 + K` parallel LLM calls, writes voices to
  `.notification/soul.json` via `publish_notification`; the kernel's notification
  sync surfaces them inside the synthesized `notification(action='check')` pair.
- `inquiry` runs a synchronous mirror session and persists the result via
  `_persist_soul_entry(..., mode="inquiry")` into `logs/soul_flow.jsonl`.
- `config`/`voice` update live agent state and persist to `init.json`
  (`manifest.soul`); `config` also restarts the wall-clock timer when
  `delay_seconds` changes.
- `settings` only reads those live values plus the process flow gate; it never
  writes `init.json`, process environment, timers, or notifications.
- `dismiss` clears `.notification/soul.json` through
  `lingtai.kernel.notifications.dismiss_channel(agent, "soul", invoked_by="soul")`.

## Cross-platform invariants

- All persistence is JSON/JSONL via `pathlib.Path` and the shared
  `notifications`/`config` helpers (init.json writes are atomic via a `.tmp` +
  replace). DOCUMENT.
- `flow`/`inquiry` fan-out runs on `threading.Thread` daemons and gates on the
  agent's `_idle` event; no subprocess/PTY. DOCUMENT (do not change).
- No platform-specific path handling beyond `agent._working_dir` joins.
  DOCUMENT — all file access via pathlib.

## Anchored claims

| Claim | Source | Test |
|---|---|---|
| Schema exposes the existing operational children plus reserved `settings` and `manual` behind one closed LTP v2 root | `src/lingtai/tools/soul/__init__.py:get_schema` | `tests/test_soul.py`, `tests/test_tool_family_soul_migration.py` |
| Each action's parameters live only in that action's own strict `input` branch | `src/lingtai/tools/soul/__init__.py` (child `input_schema` objects) | `tests/test_tool_family_soul_migration.py::test_action_parameters_are_no_longer_advertised_to_every_action` |
| Cross-action `input` is rejected before any handler I/O | `src/lingtai/tools/soul/__init__.py:handle` via `tool_family.ToolFamily.handle` | `tests/test_tool_family_soul_migration.py::test_cross_action_input_is_rejected_before_any_handler_io` |
| `manual` returns the full body plus host-local `manual_path`, no double wrap, and performs no soul operation | `src/lingtai/tools/soul/__init__.py:_adapt_manual_result`, `tool_family/manual.py:build_manual_child` | `tests/test_tool_family_soul_migration.py` (manual section), `tests/test_intrinsic_manual_actions.py` |
| One public `soul` root on both the Chat and Responses wires, `reasoning` required | `src/lingtai/tools/soul/__init__.py:get_schema`, `kernel/base_agent/tools.py:_build_tool_schemas` | `tests/test_tool_family_soul_migration.py::test_agent_composition_keeps_reasoning_required_on_both_wires` |
| The synthesized involuntary flow pair carries the current envelope, not the flat pre-migration shape | `src/lingtai/tools/soul/consultation.py:build_consultation_pair` | `tests/test_tool_family_soul_migration.py::test_synthesized_involuntary_flow_pair_uses_the_current_envelope`, `tests/test_soul_consultation.py::TestBuildConsultationPair::test_pair_uses_soul_flow_action` |
| Schema and dispatch are generated from one child registry and cannot drift | `src/lingtai/tools/soul/__init__.py:_build_declared_children`/`_build_family` | `tests/test_tool_family_soul_migration.py::test_schema_and_dispatch_come_from_one_registry` |
| `flow` is opt-in and returns a stable `disabled` status when the env var is unset | `src/lingtai/tools/soul/__init__.py:handle` (`_soul_flow_enabled`) | `tests/test_soul.py` |
| A late consultation result is discarded after a state change | `src/lingtai/tools/soul/flow.py` | `tests/test_soul.py::test_consultation_fire_discards_late_result_after_state_change` |
| `inquiry` returns a voice (or "(silence)") and persists the entry | `src/lingtai/tools/soul/__init__.py:handle`, `src/lingtai/tools/soul/inquiry.py:soul_inquiry` | `tests/test_soul_consultation.py` |
| `config` validates `delay_seconds`/`consultation_past_count` bounds and persists to init.json | `src/lingtai/tools/soul/config.py:_handle_config`/`_persist_soul_config` | `tests/test_soul.py` |
| `voice` reads/switches built-in profiles and stores custom prompts within the cap | `src/lingtai/tools/soul/config.py:_handle_voice`/`_persist_soul_voice` | `tests/test_soul.py` |
| `dismiss` delegates to the shared `dismiss_channel` helper for the `soul` channel | `src/lingtai/tools/soul/__init__.py:handle` | `tests/test_system_dismiss.py::test_soul_dismiss_alias_uses_shared_helper` |
| `settings` returns the exact five-field owner projection, redacts the custom prompt, and fails as one unit when current truth is unavailable | `src/lingtai/tools/soul/settings.py:soul_settings_provider` | `tests/test_soul_settings.py` |
| Soul entries append to `logs/soul_flow.jsonl` keyed by `mode` | `src/lingtai/tools/soul/flow.py:_persist_soul_entry` | `tests/test_soul_consultation.py` |

## Verification matrix

| Invariant | Automated test | Manual check | Risk if broken |
|---|---|---|---|
| Flow stays opt-in and burns no thread when disabled | `tests/test_soul.py` (disabled-path assertions) | Call `soul(action='flow')` without the env var | Unexpected LLM cost / fan-out |
| Concurrent flow fires are rejected, not silently dropped | `tests/test_soul.py` (`flow ongoing` path) | Trigger `flow` twice quickly | Surprising silent no-op |
| Config bounds are enforced and persisted | `tests/test_soul.py` (config validation) | Set `delay_seconds` below the min | Runaway cadence / lost settings |
| `dismiss` only clears the `soul` channel via the shared guard | `tests/test_system_dismiss.py::test_soul_dismiss_alias_uses_shared_helper` | Dismiss soul, inspect other channels | Cross-channel notification wipe |
| Late/stale consultation results are discarded | `tests/test_soul.py::test_consultation_fire_discards_late_result_after_state_change` | Change state mid-fire | Stale voices injected into a new context |
| A cross-action `input` never reaches a handler | `tests/test_tool_family_soul_migration.py::test_cross_action_input_is_rejected_before_any_handler_io` | Send `action='inquiry'` with `input={'delay_seconds': 60}` | A mis-paired call silently mutating cadence or burning an LLM call |
| `manual` performs no soul operation | `tests/test_tool_family_soul_migration.py::test_manual_performs_no_soul_operation` | Call `soul(action='manual', input={})` and inspect init.json + logs | Reading the manual perturbing live soul state |
| Synthesized flow calls stay envelope-shaped | `tests/test_tool_family_soul_migration.py::test_synthesized_involuntary_flow_pair_uses_the_current_envelope` | Trigger a fire, read the appended pair's args in chat history | History teaching a shape the schema rejects; a model imitating it gets `INVALID_ARGUMENT` |
| Settings stays five-field SHOW-only | `tests/test_soul_settings.py` | Call `settings` twice around an authorized owner change | Secret disclosure, mutation through SHOW, or stale/unavailable truth presented as success |

Run before merging soul changes:

```bash
python -m pytest tests/test_soul.py tests/test_soul_consultation.py \
  tests/test_system_dismiss.py tests/test_tool_family_soul_migration.py \
  tests/test_intrinsic_manual_actions.py tests/test_soul_settings.py \
  tests/test_tool_settings_contract.py -q
```

## Schema and glossary ownership

- **Canonical identifiers:** function names, JSON property names, action/enum
  values, required fields, defaults, and bounds are canonical English literals.
  The schema (`get_schema()`) and description (`get_description()`) are
  language-independent; the optional `lang` argument is accepted for source
  compatibility but ignored.
- **Provider wire:** provider adapters resolve the top-level tool description
  through `wire_tool_description`: the global `WIRE_TOOL_DESCRIPTION` pointer
  while the resident `## tools` section is opted in via
  `LINGTAI_TOOL_PROSE_SECTION_ENABLED`, otherwise the full
  `FunctionSchema.description` prose (that section is off by default, so the
  wire is where the canonical prose lands). Nested parameter descriptions are
  unchanged either way.
- **Glossary resources:** this package owns `glossary-en.md`, `glossary-zh.md`,
  and `glossary-wen.md`. Each has strict YAML frontmatter
  (`kind: tool-glossary`, `schema_version: 1`, `tool_package: tools.<pkg>`,
  `language: <lang>`). English body is empty; zh/wen bodies contain concise
  terminology mappings that quote immutable English identifiers and never offer
  localized aliases.
- **Fallback:** exact normalized language lookup, then English, then no
  appendix. Fail-closed for localized text; fail-open for tool availability.
- **Update triggers:** changing a function name, action/enum value, property
  name, or user-visible concept requires reviewing all three glossary files in
  the same PR.
- **Validation:** `python -m lingtai.tools.glossary_validator --check`.
