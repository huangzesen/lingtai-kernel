---
name: system-manual
description: >
  Short router for runtime, lifecycle, identity, refresh, presets, settings,
  LLM adapters, and operating procedures; route to the named reference for depth.
version: 1.21.0
last_changed_at: "2026-09-06T00:00:00Z"
tags: [lingtai, agent, runtime, procedures, substrate, system, lifecycle, alarm, memory, communication, skills, settings, molt, summarize, nudge, updates, refresh, preset, llm, adapters, codex, websocket]
related_files:
- src/lingtai/prompts/substrate/substrate.md
- src/lingtai/prompts/procedures/procedures.md
- src/lingtai/kernel/base_agent/lifecycle.py
- src/lingtai/tools/system/karma.py
- src/lingtai/tools/system/schema.py
- src/lingtai/tools/system/CONTRACT.md
- src/lingtai/tools/system/ANATOMY.md
- src/lingtai/tools/system/settings.py
- tests/test_system_declared_plugin.py
- src/lingtai/kernel/nudge/ANATOMY.md
- src/lingtai/intrinsic_skills/system-manual/reference/llm-adapters/SKILL.md
- src/lingtai/intrinsic_skills/system-manual/reference/external-attach-diagnostic/SKILL.md
- src/lingtai/llm/_register.py
- src/lingtai/llm/openai/adapter.py
- src/lingtai/intrinsic_skills/system-manual/reference/tool-plugin-settings/SKILL.md
- src/lingtai/intrinsic_skills/system-manual/reference/settings-inventory/SKILL.md
- tests/test_skills.py
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# System Manual — Progressive Disclosure Router

`system-manual` is the working router for the `system` capability. Read the
matching row below, then open that reference; do not guess a procedure from this
index. The resident `substrate` and `procedures` prompts keep only invariant
rules, while the references own operational depth.

## Nested reference catalog

These nested references are owned by this manual, not standalone skills:

```yaml
- name: substrate-manual
  location: reference/substrate-manual/SKILL.md
  description: Runtime/lifecycle model, alarms, memory, MCP, and init/preset detail (§11).
- name: procedures-manual
  location: reference/procedures-manual/SKILL.md
  description: Action discipline, authorization, collaboration, routing, and deliverables.
- name: refresh-precheck
  location: reference/refresh-precheck/SKILL.md
  description: Ordered refresh/preset checks, verification, and failure diagnosis.
- name: runtime-update-checks
  location: reference/runtime-update-checks/SKILL.md
  description: Runtime provenance, kernel-version nudges, installer ownership, and updates.
- name: environment-variables
  location: reference/environment-variables/SKILL.md
  description: Canonical environment registry, timing, and invalid-value rules.
- name: settings-inventory
  location: reference/settings-inventory/SKILL.md
  description: System SHOW sources, precedence, defaults, redaction, timing, and changes.
- name: llm-adapters
  location: reference/llm-adapters/SKILL.md
  description: Provider inventory, dispatch, and Codex REST/WebSocket behavior.
- name: sqlite-log-query
  location: reference/sqlite-log-query/SKILL.md
  description: Read-only SQLite/JSONL runtime trace recipes.
- name: trajectory-mining
  location: reference/trajectory-mining/SKILL.md
  description: Trajectory/anomaly mining, validation, and improvement digests.
- name: goal-manual
  location: reference/goal-manual/SKILL.md
  description: Active-goal setup, reminders, cancellation, and completion.
- name: how-to-change-name
  location: reference/how-to-change-name/SKILL.md
  description: Authorized physical workdir/address migration with identity checks.
- name: external-attach-diagnostic
  location: reference/external-attach-diagnostic/SKILL.md
  description: Guarded macOS attach and content-free PID/runtime diagnostics.
- name: tool-plugin-settings
  location: reference/tool-plugin-settings/SKILL.md
  description: Optional read-only settings provider guidance for ToolFamily developers.
```

## Router table

| Need / keywords | Read |
|---|---|
| Runtime model, lifecycle states, body/extensions, memory, communication, MCP/addons, alarms, or detailed init/preset composition | `reference/substrate-manual/SKILL.md` |
| Action discipline, responsiveness, authorization, collaboration, skill routing, or deliverables | `reference/procedures-manual/SKILL.md` |
| About to refresh, swap/revert a preset, or verify a refresh | `reference/refresh-precheck/SKILL.md` |
| Kernel update/version nudge, source provenance, installer ownership, or `source_drift` | `reference/runtime-update-checks/SKILL.md` |
| Environment variable purpose, accepted values, timing, or invalid fallback | `reference/environment-variables/SKILL.md` |
| `system(action="settings", input={})`, cache budget, runtime policy, effective init/manifest/LLM values, redaction, or owner procedure | `reference/settings-inventory/SKILL.md` |
| LLM provider/adapter inventory or Codex transport | `reference/llm-adapters/SKILL.md` |
| Runtime traces, `log.sqlite`, JSONL, `tool_call_id`, `lingtai-agent log doctor`, `lingtai-agent log query`, or `lingtai-agent log rebuild` | `reference/sqlite-log-query/SKILL.md` |
| trajectory/anomaly mining or improvement digest | `reference/trajectory-mining/SKILL.md` |
| Active goal and goal notifications | `reference/goal-manual/SKILL.md` |
| Physical workdir/address rename | `reference/how-to-change-name/SKILL.md` |
| Authorized external attach or PID-incarnation diagnosis | `reference/external-attach-diagnostic/SKILL.md` |
| Add or inspect a ToolFamily settings provider | `reference/tool-plugin-settings/SKILL.md` |
| Molt, summary application, provider replay, soul, notification, MCP, shell, daemon, avatar, skills, or knowledge | Read the owning `context-manual`, `soul-manual`, `notification-manual`, `mcp-manual`, `shell-manual`, `daemon-manual`, `avatar-manual`, `skills-manual`, or `knowledge-manual`. |

## Working contract

Call the model-facing family with the closed LTP v2 envelope:

```json
{"action": "<one action from the installed schema>", "input": {"<fields for that action only>": "..."}, "reasoning": "<short purpose>", "summarize": false}
```

`action`, `input`, and `reasoning` are required; `summarize` is optional. The
root and the selected `input` are closed, so keep fields in their own branch.
`presets` can return a large allowed-only catalog; use `summarize=true` only
when exact entries are unnecessary. Use `system(action="manual", input={})` to
fetch this installed manual. The
`manual` action itself must always use `summarize=false`; do not summarize away
the procedure you are about to follow. `system(action="summarize")` is not an
action: context hygiene belongs to `context(action="summarize"|'rebuild'|'molt')`.

### Lifecycle and authorization

Self actions are `sleep`, `refresh`, `presets`, `name_set`, `name_nickname`,
`settings`, and `manual`. Peer controls `lull`, `interrupt`, `suspend`, `cpr`,
and `clear` require `admin.karma=True`; `nirvana` additionally requires
`admin.nirvana=True` and permanently destroys the target. Availability never
proves authority. For any peer action, use the exact target working-directory
address and prefer communication/diagnosis before a forceful lifecycle change.

Normal waiting is IDLE. A positive `sleep` `delay` is only a last-resort,
one-shot alarm for async work lacking reliable completion notification; null or
omission means ordinary sleep. Pending notifications refuse ordinary self-sleep
unless the explicit `force` escape hatch is used. Read the substrate reference
for alarm state, wake, and failure details.

### Refresh, presets, and installation are different

`refresh` reloads the existing on-disk `init.json`/runtime surface and rebuilds
configuration, prompts, capabilities, MCP, and LLM state while preserving
identity and live conversation where supported. It does not download, install,
upgrade, switch an editable checkout, or repair a mismatched environment. Before
refreshing, read `reference/refresh-precheck/SKILL.md`; for a kernel-version or
installation/update question, route to `reference/runtime-update-checks/SKILL.md`
instead.

For a preset swap, first call `system(action="presets", input={})` and use an
exact path from its allowed-only result. `refresh` refuses a path outside
`manifest.preset.allowed`, a `preset` plus `revert_preset` conflict, or a target
whose context limit cannot hold the current conversation. Use
`revert_preset=true` to return to `manifest.preset.default`. The substrate
reference §11 owns the runtime model; the refresh reference owns ordering and
post-refresh checks. A config/prompt/MCP/capability edit needs refresh to take
effect; refresh is not a substitute for `context(action="rebuild")`, and neither
is an installer.

### Identity and settings

`name_set` sets the true name once and immutably; `name_nickname` changes or
clears the mutable nickname. Both update live identity, `.agent.json`, and the
protected prompt identity section. Neither edits raw init configuration nor
renames the physical address/workdir; use `reference/how-to-change-name/SKILL.md`
for that separately authorized migration.

`settings` is a complete, read-only SHOW. It accepts only `input={}` and returns
five-field rows (`key`, `current`, `default`, `configurable`, `comment`). It
never writes, resets, refreshes, or authorizes a change. Follow each row's
comment to `reference/settings-inventory/SKILL.md` for canonical source,
precedence, redaction, application timing, and the external owner procedure;
any unavailable current owner fails the complete inventory. Runtime-policy
sources include `LINGTAI_CONTEXT_LIMIT`, `LINGTAI_MAX_RPM`,
`LINGTAI_STREAMING`, `LINGTAI_AED_TIMEOUT`, `LINGTAI_MAX_AED_ATTEMPTS`,
`LINGTAI_SNAPSHOT_INTERVAL`, and `LINGTAI_ACTIVENESS`; the reference owns their
accepted values and timing.

### Anchored settings routes

### Cache-miss budget

This stable `system-manual#cache-miss-budget` anchor routes to the exact
cache-budget source, validation, precedence, and authorized procedure in
`reference/settings-inventory/SKILL.md`. SHOW is read-only. The owner file is
`<agent-workdir>/settings/system.json`; its minimal v1 shape is:

```json
{"schema_version": 1, "cache_miss_budget": 2000000}
```

The live `LINGTAI_CACHE_MISS_BUDGET` source wins over that file, then the fixed
`2,000,000` default. After explicit owner authorization, use the existing File or
Shell capability to change the source, then call `system(action="settings", input={})` again to verify it. This setting is unrelated to `.notification/system.json`, and
legacy `manifest.cache_miss_budget` is ignored. The reference owns validation,
shadowing, timing, and the full procedure.

## Runtime policy (v2)

This stable `system-manual#runtime-policy-v2` anchor routes to the closed v2
document grammar and its application/refresh procedure in
`reference/settings-inventory/SKILL.md`. Do not copy policy values into this
router.

## Choosing and maintaining this manual

If resident guidance answers the question, act. Otherwise use this router, then
read the named node before improvising; descend to cited code/tests for ground
truth. Keep this file a short router. Move new workflows, catalogs, examples,
field dictionaries, troubleshooting, and protocol detail into the appropriate
reference instead of expanding the always-sent entry.
