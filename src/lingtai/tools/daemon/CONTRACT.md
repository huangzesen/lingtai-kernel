---
name: daemon-contract
tool: daemon
description: >
  Unified daemon contract for the public tool surface, backend architecture
  capability invariants, selected-skills context, one-run MCP propagation,
  daemon_common completion signaling, support-status honesty, run artifacts,
  terminal notifications, and compaction boundaries.
status: active
contract_version: 15
last_changed_at: "2026-09-07"
related_files:
  - src/lingtai/tools/daemon/ANATOMY.md
  - src/lingtai/tools/daemon/BEHAVIORS.md
  - src/lingtai/tools/daemon/__init__.py
  - src/lingtai/tools/daemon/_tool_family.py
  - src/lingtai/tools/daemon/settings.py
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/adapters/posix/daemon_manager.py
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/kernel/tool_plugin/__init__.py
  - src/lingtai/tools/daemon/system_prompt.py
  - src/lingtai/tools/tool_family/CONTRACT.md
  - src/lingtai/tools/tool_family/__init__.py
  - src/lingtai/tools/tool_family/manual.py
  - src/lingtai/kernel/meta_block.py
  - src/lingtai/kernel/tool_executor.py
  - src/lingtai/kernel/tool_result_summary.py
  - src/lingtai/services/mcp.py
  - src/lingtai/kernel/llm/base.py
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/kernel/notification_store/CONTRACT.md
  - src/lingtai/adapters/posix/notification_store.py
  - src/lingtai/llm/service.py
  - src/lingtai/llm/interface_converters.py
  - src/lingtai/tools/daemon/process_port.py
  - src/lingtai/tools/daemon/interactive_terminal/__init__.py
  - src/lingtai/tools/daemon/interactive_terminal/CONTRACT.md
  - src/lingtai/tools/daemon/interactive_terminal/ANATOMY.md
  - src/lingtai/adapters/posix/interactive_terminal.py
  - src/lingtai/tools/daemon/posix_process.py
  - src/lingtai/tools/daemon/windows_process.py
  - src/lingtai/tools/daemon/run_dir.py
  - src/lingtai/tools/daemon/execution_host.py
  - src/lingtai/tools/daemon/supervisor_runtime.py
  - src/lingtai/adapters/posix/daemon_capsule.py
  - src/lingtai/adapters/posix/daemon_supervisor.py
  - src/lingtai/kernel/provider_admission.py
  - src/lingtai/tools/daemon/shell_prompt_events.py
  - src/lingtai/tools/bash/CONTRACT.md
  - tests/test_daemon_shell_prompt_events.py
  - src/lingtai/kernel/session_stats/CONTRACT.md
  - src/lingtai/tools/daemon/manual/SKILL.md
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/cli_daemon.py
  - tests/test_cli_daemon.py
  - src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md
  - src/lingtai/mcp_servers/daemon_common/server.py
  - src/lingtai/llm/openai/ANATOMY.md
  - src/lingtai/llm/mimo/ANATOMY.md
  - tests/test_task_card_proactivity.py
  - tests/test_tool_family_daemon_migration.py
  - tests/test_daemon_settings.py
  - tests/test_daemon.py
  - tests/test_daemon_central_manager.py
  - tests/test_daemon_empty_parity.py
  - tests/test_daemon_missing_finish_guidance.py
  - tests/test_apriori_summary_executor.py
  - tests/test_daemon_backend_options.py
  - tests/test_daemon_claude_p_background_guard.py
  - tests/test_daemon_opencode_backend.py
  - tests/test_daemon_cursor_backend.py
  - tests/test_daemon_claude_interactive_backend.py
  - tests/test_daemon_run_dir.py
  - tests/test_daemon_codex_usage.py
  - tests/test_codex_standalone_compaction.py
  - tests/test_daemon_windows_lock.py
  - tests/test_daemon_windows_process_port.py
  - tests/test_daemon_windows_supervisor.py
  - tests/test_daemon_detached_supervisor.py
  - tests/test_mcp_v2_adapter_metadata.py
review_triggers:
  - src/lingtai/tools/daemon/__init__.py
  - src/lingtai/tools/daemon/_tool_family.py
  - src/lingtai/tools/daemon/settings.py
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/adapters/posix/daemon_manager.py
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/kernel/tool_plugin/__init__.py
  - src/lingtai/tools/daemon/system_prompt.py
  - src/lingtai/kernel/meta_block.py
  - src/lingtai/services/mcp.py
  - src/lingtai/llm/interface_converters.py
  - src/lingtai/tools/daemon/run_dir.py
  - src/lingtai/tools/daemon/execution_host.py
  - src/lingtai/tools/daemon/supervisor_runtime.py
  - src/lingtai/adapters/posix/daemon_capsule.py
  - src/lingtai/adapters/posix/daemon_manager.py
  - src/lingtai/adapters/posix/daemon_supervisor.py
  - src/lingtai/tools/daemon/ANATOMY.md
  - src/lingtai/tools/daemon/manual/
  - src/lingtai/mcp_servers/daemon_common/
  - tests/test_daemon.py
  - tests/test_daemon_central_manager.py
  - tests/test_daemon_settings.py
  - tests/test_daemon_backend_options.py
  - tests/test_daemon_claude_p_background_guard.py
  - tests/test_daemon_missing_finish_guidance.py
  - tests/test_daemon_opencode_backend.py
  - tests/test_daemon_cursor_backend.py
  - tests/test_daemon_claude_interactive_backend.py
  - tests/test_daemon_run_dir.py
  - tests/test_daemon_codex_usage.py
  - tests/test_codex_standalone_compaction.py
  - tests/test_mcp_v2_adapter_metadata.py
maintenance: |
  Keep this unified daemon Contract in the same maintenance graph as the daemon
  ANATOMY.md and manual files listed under related_files. If behavior and this
  contract disagree, the code is the source of truth — fix the contract in the
  same change and bump contract_version on breaking contract edits.
---

# Daemon Contract

`daemon` dispatches and manages ephemeral subagents (emanations 分身之念).
This file is the single authoritative contract for both the public tool
surface and the daemon architecture capability invariant formerly documented
as the separate Daemon Architecture Capability Contract. The implementation
lives in `src/lingtai/tools/daemon/`; code remains the source of truth.

> **Maintenance trigger:** any change to a path listed in `review_triggers`
> must re-check this contract in the same change. The PR should either update
> this document or say why the daemon contract still holds.

## Routing Card

**Use this when:**

- You are editing the daemon tool schema, `handle` action dispatch, per-action
  success/error shapes, backend selection, run-dir storage, or tool-surface
  behavior an agent sees.
- You are changing backend architecture, selected-skills disclosure,
  parent-provided MCP propagation, daemon_common completion, support status,
  terminal notifications, daemon compact behavior, or run artifacts.

**Do not use this for:**

- Code navigation only: read `src/lingtai/tools/daemon/ANATOMY.md`.
- Independent peer agents that outlive the parent: use `avatar` (see
  `src/lingtai/tools/avatar/CONTRACT.md`). An emanation's lifecycle is bounded
  by the parent; an avatar's is not.

**Fast paths:** action schema -> §Tool Surface; backend names -> §Scope;
backend capability guarantees -> §Capability Invariants; support table ->
§Backend Support Matrix; run-dir layout -> §State & Storage; process and PTY
ownership -> §Process and Terminal Boundaries.

## Scope

- Canonical tool name: `daemon`.
- The parent `daemon` tool exposes seven actions: `emanate`, `list`, `ask`,
  `check`, `reclaim`, `settings`, `manual`; `action` is required, and so are
  `input` and `reasoning`
  (see §Tool Surface). Every LingTai emanation additionally receives
  the intrinsic `compact` tool, whose required `action` is explicit `run`
  (non-terminal reset) or `manual` (read-only procedures); omission is refused.
- Backends (`backend`, default `lingtai`): schema enum is `lingtai`, `claude-p`,
  `claude-code`, `codex`, `opencode`, `mimocode`, `mimo`, `qwen-code`, `qwen`,
  `oh-my-pi`, `omp`, `kimicode`, `kimi`, `cursor`, `deepseek`. Aliases collapse via
  `_normalize_backend`: `mimo→mimocode`, `qwen→qwen-code`, `omp→oh-my-pi`,
  `kimi→kimicode`; `claude-code` is a compatibility alias for `claude-p`.
  `claude` / `claude-interactive` are hidden (not schema-advertised). Active
  external CLI runs whose launch path really mounts `daemon_common`
  (`claude-p`/`claude-code`, Codex, OpenCode, Qwen, and Kimi) accept `ask` as a
  queued next-checkpoint message. This does not add a terminal resume contract:
  Qwen and Kimi still return explicit unsupported messages after terminal state,
  while hidden interactive Claude and every backend without common MCP retain the
  existing active `busy` behavior.
- The architecture capability invariant applies to every daemon backend and
  backend family LingTai exposes. It is not primarily a per-task input contract:
  the durable requirement is that any daemon architecture preserves the same
  selected-skill discovery semantics, one-run MCP registration semantics,
  completion signaling, backend support honesty, and reviewable artifact
  boundary.

Non-scope: claiming new backend MCP support before implementation, changing
third-party MCP protocols, or broad daemon scheduling/timeout behavior except
where those changes affect the invariants here.

## Tool Surface

Guarded by: [D001](BEHAVIORS.md#behavior-d001), [D002](BEHAVIORS.md#behavior-d002), [D003](BEHAVIORS.md#behavior-d003), [D004](BEHAVIORS.md#behavior-d004), [D005](BEHAVIORS.md#behavior-d005), [D010](BEHAVIORS.md#behavior-d010)

`daemon` is migrated to the LingTai Tool Protocol v2 action-separated envelope
(`../CONTRACT.md`, `../tool_family/CONTRACT.md`) using the generic
`tool_family` infrastructure. The public root is exactly `action`, `input`,
required `reasoning`, and optional `summarize`, with
`required: ["action", "input", "reasoning"]` and
`additionalProperties: false`. Exactly one model-facing tool named `daemon`
remains registered: the seven actions are internal `ChildTool`s and MUST NOT
consume additional model tool slots, and no compatibility alias or second
public root exists.

The seven canonical children are `emanate | list | ask | check | reclaim |
settings | manual`, each owning one strict, closed `input` schema. Every field
the pre-migration flat root advertised to all operational actions at once lives in
exactly the branch that consumes it:

| Action | `input` fields |
|---|---|
| `emanate` | `tasks[]` (each requires `task` + `tools`; optional per-task fields are unchanged and specified below), `backend`, `max_turns`, `timeout` |
| `list` | `contains`, `status`, `include_done`, `last` |
| `ask` | `id`, `message` |
| `check` | `id`, `last`, `truncate` |
| `reclaim` | — (canonical strict-empty `input`) |
| `settings` | — (canonical strict-empty `input`) |
| `manual` | — (canonical strict-empty `input`) |

Optional fields are spelled as required-nullable properties, as strict
validators demand of a closed object; `null` means *absent* and the engine
applies its own unchanged default, so a falsy-but-present value (`truncate: 0`,
`include_done: false`, `contains: ""`) is preserved verbatim. The nested
per-task object inside `emanate.tasks` is deliberately left open
(`additionalProperties` unset), because `_handle_emanate`'s own strict per-task
validation owns that boundary and returns domain-specific errors (notably the
obsolete `system_prompt` migration message) a schema rejection would replace
with a generic one.

Dispatch is the second, always-authoritative enforcement layer: an unknown or
unhashable `action`, an unknown root field, a non-object `input`, a non-boolean
`summarize`, and any `input` key belonging to another action's branch are all
refused before the daemon engine runs, fail-closed. The unknown-action envelope
failure carries daemon's own seven-action message.

The former flat root `summary` boolean is replaced by the canonical root
`summarize`; `daemon` is on `kernel/tool_result_summary.py`'s
`_LTP_V2_MIGRATED_FAMILIES` allowlist so that spelling is actually honored
rather than silently ignored, and the legacy `summary` spelling remains
accepted there for historical/pending calls.

`DECLARATION` in `daemon/__init__.py` is Daemon's static official identity:
its five operational actions use `_tool_family.py`'s strict schemas, the generic
family inserts opted-in `settings`, and the kernel appends the reserved
installed `manual` child. Its binder receives only
`workdir` plus the capability-native `daemon_runtime` port; it constructs the
unchanged `DaemonManager` and `DaemonFamilyDispatcher` without retaining an
Agent. The runtime port preserves the real current-agent service/model,
regular tool schemas and handlers, MCP-name exclusion, preset sandbox/load,
notification, time, Task Card, logging, and resolved manager options through
named operations. `DaemonManager` remains the engine for batch emanation,
backend routing, run directories, supervision, completion signaling,
cancellation, timeouts, terminal notifications, and result/error persistence.
Its legacy flat `action="manual"` branch remains for internal callers only; the
registered `manual` is `build_manual_child(workdir, DECLARATION.manual)`,
returning canonical `content[0].text` / `structuredContent.manual_path`
verbatim with no manager operation or double wrap.

### External owner CLI

Guarded by: [D011](BEHAVIORS.md#behavior-d011)

`lingtai-agent daemon emanate | list | check | ask | wait | reclaim` is the
same engine driven by an **external owner**: a same-machine principal (a coding
agent acting for a human, CI, a shell operator) that owns the runs it dispatches
instead of borrowing a live Agent's identity. Every command takes `--owner-dir`
(`--agent-dir` is the legacy spelling of the same argument), which MUST be a
directory holding a valid `init.json` and MAY be a standalone directory the
caller prepared itself; no Agent needs to be running there and the CLI never
takes the directory's `.agent.lock` lease, heartbeat, or agent identity. A
standalone owner supplies its own minimal `init.json`; the CLI copies no other
agent's credentials or preset allowlist and creates no secrets or configuration.
Run directories, the dispatch ledger, and resident manager pools are anchored
to the owner directory exactly as for a live agent, and the detached
supervisor publishes terminal and follow-up notifications to
`<owner>/.notification/daemon/<daemon-id>.json` from the manifest's
`parent_working_dir` alone (§ 6). `emanate`, `ask`, and `reclaim` dispatch
through `setup()` and the `DaemonFamilyDispatcher` envelope, so `ask` follows
the engine's own delivery rules unchanged (`sent`/`queued` exit 0; `busy`/`error`
exit 1 with the engine result printed). `list` (`--json` for the engine payload),
`check`, and `wait` are read-only: they construct no manager and run no
reconciliation or repair. `wait <id>` resolves the id/path through one durable
`check`, polls only the run's atomic `daemon.json` every `--interval` seconds
(never rescanning a growing events log), and performs one final full `check`. It
reports each progress change (state, turn, current tool, latest checkpoint
sequence, last output, pending checkpoint messages, resume/follow-up state)
exactly once and ends with one final record: exit 0
for `done`, 1 for `failed`/`cancelled`/`timeout`, 124 when `--timeout` elapses,
130 on interrupt; non-finite bounds are refused and `--json` emits one object
per line. `emanate` preview publishes primary `owner_dir` plus the retained
machine-readable `agent_dir` compatibility key. `wait` never adopts
execution ownership — the run's detached supervisor stays its only owner.

### Daemon settings ownership

Daemon's declaration opts its family into the generic read-only
`settings` action. Successful output is `{"settings": [...]}` with exactly
`key`, `current`, `default`, `configurable`, and `comment` on every row. The
provider returns exactly these owner keys in this order:

| Key | Current truth | Default | Configurable | Manual section |
|---|---|---:|---|---|
| `max_turns` | manager's effective `_max_turns` | `5000` | yes | `daemon-manual#max-turns` |
| `manager_pool_size` | manager's effective `_manager_pool_size` | `100` | yes | `daemon-manual#manager-pool-size` |
| `system_prompt_budget_chars` | manager's effective `_system_prompt_budget_chars` | `20000` | yes | `daemon-manual#system-prompt-budget-chars` |
| `timeout` | manager's effective `_timeout` | `3600.0` | yes | `daemon-manual#timeout` |

`configurable` means an authorized owner procedure exists outside SHOW;
`settings` has no set/reset/mutation operation. Missing manager truth, a
provider exception, a malformed row, a non-finite or unserializable value, or
an oversized complete response fails the whole action with the generic fixed
bounded failure and returns no partial row. Sensitive settings in any family
redact both `current` and `default`; Daemon's four owner rows are nonsensitive.

`max_turns` is Daemon-owned. Its precedence is a valid
`LINGTAI_DAEMON_MAX_TURNS` value, explicit capability/setup override, valid
`daemon/daemon.json` `max_turns`, then `5000`. The other source and change
procedures are defined at the stable manual anchors named above. In particular,
`timeout` has only the launcher/capability setup layer and the `3600.0` default;
per-run `emanate.timeout` is not this owner setting.

`list`, `check`, `settings`, and `manual` are read-only. `emanate`, `ask`, and `reclaim`
are the three side-effectful actions.

`tasks[].task` is required and is the complete parent-controlled daemon system
instruction for `backend="lingtai"`: objective, role, constraints, tool policy,
collaboration boundaries, and safety posture all go there. Optional
`tasks[].prompt` is LingTai's first ordinary user message. Missing, empty, or
whitespace-only `prompt` defaults exactly to `Begin the assigned daemon task.`;
any nonblank string is sent and stored byte-for-byte, including leading and
trailing whitespace. `prompt` is not appended to the system task, and the task
is not duplicated into user[0]. `system_prompt` is removed with no alias:
callers must put the complete system instruction in `task`, and preflight
rejects the obsolete field before run-dir creation or scheduling. External CLI
backend tasks reject `prompt` before run-dir creation; CLI behavior remains
task-as-CLI-prompt.

Optional `tasks[].plugin` is an array of Agent Plugin path strings for this
one daemon run. Each path resolves against the parent agent's working
`plugin/` directory; the LingTai backend renders a `## Parent-selected plugins`
oneshot-context section into the durable `.prompt` the detached child reads,
merges each plugin's validated `skills/` rows into the task skill catalog, and
mounts each plugin's validated `mcp.json` servers as task-scoped MCP clients.
A missing or unreadable plugin path resolves to nothing without failing the
task; a non-list value fails preflight before run-dir creation. External CLI
backends receive the plugin's skills and MCP registrations through the normal
skill/mcp oneshot context and mount no plugin-native surface.

Optional `tasks[].task_files` is an array of `{path, label?, role?}` objects
naming UTF-8 text input files under the parent agent working directory.
Preflight (before any run-dir creation, preset work, or scheduling) resolves
each path relative to that working directory, enforces containment inside it
(fully-resolved, symlinks followed), validates UTF-8 text and the practical
limits (`TASK_FILES_MAX_PER_TASK` files per task, `TASK_FILE_MAX_BYTES` per
file), and snapshots the bytes content-addressed into the immutable read-only
input store `daemons/_task_files/` (one blob per SHA-256 across the whole
dispatch/group, published atomically via `os.replace`). Any malformed,
out-of-root, missing, oversize, or non-UTF-8 entry refuses the whole batch
with a `tasks[i].task_files[j]`-context error — task input never silently
falls back to the mutable original path. Each run's durable
`daemon.json.call_parameters.task_files` records `{manifest, files}` pointing
at the compact per-dispatch manifest (`daemons/_task_files/manifest-<group>.json`)
and that run's snapshot rows (`path`, `label`, `role`, `sha256`, `size`,
`snapshot`), so `check`, retry, and relaunch never need the original file.
Every backend receives only a compact `## Parent-provided task files`
oneshot-context section with the snapshot paths — never file contents —
keeping the daemon system prompt within its resolved character budget
regardless of input size. The `_task_files` store is internal-only: its
leading underscore excludes it from every run-dir scan
(`_looks_like_daemon_run_dir`), so `list`/recovery/`check` never surface it
as a run.

LingTai-backend daemon LLM construction uses one effective context window:
an explicit daemon preset's canonical `manifest.llm.context_limit` wins,
otherwise an implicit/no-preset daemon inherits the parent service's valid
resolved `_context_window`, and missing/invalid values fall back to the shared
`lingtai.llm.service.CONSERVATIVE_CONTEXT_WINDOW` value (272,000). That same
window is passed into the daemon `LLMService` and its context telemetry.

Per-task `context_token_limit` (positive integer; bool rejected) is a
context-token compaction threshold — rendered/provider-context tokens, never
cumulative spend — effective only for `backend="lingtai"` tasks whose resolved
provider is Codex (`codex`/`codex-pool`) or the native `mimo` LLM provider
(`manifest.llm.provider="mimo"` — distinct from the `backend` enum's
`mimo`/`mimocode` alias above, which drives the external `mimo` CLI as a
subprocess and never consults this field); every other provider and every
external CLI backend ignores it. This threshold does not set the daemon context
window. Omitted, it uses the daemon session's own resolved context window as the
threshold: explicit preset canonical `manifest.llm.context_limit` when supplied,
otherwise the inherited valid parent effective window, otherwise 272,000. An
explicit `context_token_limit` value wins only for this separate provider
compaction threshold. Native
`mimo` defaults to the stateless OpenAI Responses wire (full-history replay;
never `store`/`previous_response_id`/`conversation`/generic
`context_management`) — an explicit `wire_api="chat_completions"` on the
preset selects the Chat Completions escape hatch instead. **Failure policy
differs by provider:** a standalone-compaction failure is non-fatal for Codex
(that turn's compaction is skipped; the loop continues on full history) but a
HARD failure for native `mimo` (propagates to the caller; never silently
continues on full history and never falls back to a different wire).

| Action | Required inputs | Optional inputs | Success output | Error shapes |
|---|---|---|---|---|
| `emanate` | `tasks[]` (each `task`+`tools`) | `backend`, `max_turns`, `timeout`, per-task `prompt` (LingTai only), `skills`/`mcp`/`preset`/`backend_options`/`context_token_limit`/`plugin`/`task_files` | `{status: "dispatched", count, ids: [...], group_id, handoff}`; `handoff` tells the model it may go idle or call `system(action='sleep')` while waiting for the terminal notification, and conditionally says that if Telegram is connected and a Task Card is available for the current turn, the model should use it to report progress via `telegram(action='manual')` and that manual's `Programmable Task Card` section; for large concurrent batches it strongly recommends `notification(action='delay')` on `daemon` to reduce wake frequency while preserving daemon truth; read `daemon-manual` and `notification-manual` for details | `{status: "error", message}` — obsolete `system_prompt` migration, CLI `prompt`, bad limits, or tool-surface/preset failure |
| `list` | — | `contains`, `status`, `include_done` (default true), `last` (default newest 1000; explicit positive values, including values above 1000, are accepted) | `{...}` list blob of matching emanations (running + persisted history) | `{status: "error", message}` |
| `ask` | `id`, `message` | — | `{status: "sent", id, output}` (resume-capable CLI ask returns immediately as `{status: "sent", id, async: true, ...}`); an active common-MCP CLI returns `{status: "queued", id, delivery: "checkpoint", message_id}` | `{status: "error", id, message}` — unknown/absent id or terminal backend resume unsupported; an active backend without common MCP remains `{status: "busy", ...}` |
| `check` | `id` | `last` (default 20), `truncate` (default 500) | `{id, run_id, state, backend, path, turn, current_tool, elapsed_s, finished_at, tokens, result_preview, result_path, last_output, error, latest_checkpoint, pending_checkpoint_messages, events: [...]}`; pending is a count, never message content | `{status: "error", message}` — unknown id, no run_dir, invalid `last`/`truncate`, or read failure |
| `reclaim` | — | — | `{status: "reclaimed", cancelled: <n>}` (or `{status: "shutdown", ...}` on lifecycle shutdown) | — |

For `list`, omitted or null `last` is resolved by `DaemonManager` to a newest-
1000 default before list-entry materialization. A caller that needs more entries
must pass a positive `last: N` explicitly; values above 1000 are accepted. The
current run-directory persistence layout still requires reading candidate
`daemon.json` records to preserve timestamp sorting and filtering. For current
versioned records, and when `contains` is absent or empty, the bounded path
materializes prompt/result entry details only for rows eligible for the returned
page. A non-empty `contains` preserves full candidate materialization because it
searches prompt-preview text before filtering and paging; the deferred
no-`contains` benefit does not apply to that search path. Missing, corrupt, or
stale records still use the candidate-wide migration/rebuild path required to
repair the index. This is an execution default, not a `daemon.json` or
environment configuration, and there is no unbounded default route.

`emanate` returns immediately after dispatch; terminal state (`done` /
`timeout` / `cancelled` / `failed`) reaches the parent via a `source="daemon"`
system notification per emanation. `check` classifies terminal state from the
recorded run-dir snapshot first (see `_classify_terminal_state`).

Agents following an `emanate` success `handoff` MUST treat Task Card guidance as
conditional: use the Task Card only when Telegram is connected and a Task Card
is available for the current turn, and read `telegram(action='manual')` for the
`Programmable Task Card` details. Daemon does not create or require a watcher
and does not import or call Telegram/Task Card runtime code.

A card-worthy dispatch — two or more tasks in the batch, or an explicitly
requested `timeout` at or above 900s — additionally appends one nudge sentence
to `handoff` naming the dispatched count and suggesting
`task_card(action='start')`. An omitted `timeout` never qualifies on its own:
the default ceiling describes no actual run length, so a single daemon
dispatched without an explicit long `timeout` is not card-worthy. The nudge
also appears only when the agent has the Task Card capability enabled and no
watch is currently active, so a quick single daemon, an agent with the
capability disabled, and a fleet dispatched under a live card all receive the
plain `handoff` unchanged. The active-watch check is a duck-typed read-only
probe; daemon still imports no Task Card runtime code and never starts a watch
itself.

## Capability Invariants

### 1. Selected skills are progressive-disclosure catalog entries

Every daemon backend must preserve selected `skills` as discoverable workflow
context, not as copied skill bodies. The runtime resolves each supplied skill
directory or direct `SKILL.md` path, parses frontmatter, and renders only
`name`, `location`, and `description` into the daemon context. The model reads
the referenced `SKILL.md` only when relevant. A backend must not paste full
SKILL bodies into the prompt or hide the path needed for progressive
disclosure. The selected skills catalog/path contract is part of the final
prompt/context for every supported daemon architecture.

### 2. Parent-provided MCP registrations have two lanes

The prompt lane is universal: parent-provided MCP registrations are normalized
as one-run registration objects, rendered as a prompt-visible catalog, and
redacted for `env` and `headers` values while preserving names, transports,
keys, and non-secret shape.

The native lane is backend-specific: a backend may mount those MCP
registrations as actual tools only when its daemon runner has a verified
run-scoped native MCP config or client path. The LingTai backend starts
task-scoped MCP clients directly. CLI backends must not claim native MCP
availability from the prompt catalog alone.

The LingTai task-scoped client copies provider-bound arguments and applies the
server's original advertised input schema only to kernel-owned private fields.
It removes `_reasoning` when the server does not declare it, preserves it when
explicitly declared, and restores public `reasoning` for the exact strict
LTP-v2 family schema. Other unknown business fields pass through so the server
remains authoritative for closed-schema validation.

### 3. daemon_common provides cooperative checkpoints and terminal completion

MCP-capable daemon backends receive the built-in `daemon_common` MCP before any
parent registrations. Its strict LTP-v2 `checkpoint` tool accepts
`action="checkpoint"`, an `input` object with required bounded `state` and
`summary` plus optional bounded `artifacts`, `blocker`, and `request`, and the
required `reasoning` field. The server attaches only to the exact run named by
`LINGTAI_DAEMON_RUN_DIR` + `LINGTAI_DAEMON_RUN_ID` and rejects mismatches,
invalid payloads, and terminal runs before mutation.

A valid live checkpoint performs one RunDir state transaction: increment
`checkpoint_sequence`, store `latest_checkpoint`, and drain bounded/redacted
ID-bearing `pending_checkpoint_messages` exactly once in the durable state write;
then append a `daemon_checkpoint` event and refresh heartbeat. It next publishes
a stable-key `source="daemon"`, `kind="daemon_checkpoint"`, `terminal=false`
event on the singular built-in `daemon` channel so the parent wakes without
consuming the exactly-once terminal notification. Checkpoints use the same
append/idempotency format and durable `data.daemon` batch count/alarm latch as
terminal daemon notices; no daemon-originated parent wake lands on `system`. The tool response returns the drained IDs/messages. Once the
durable state write succeeds, an event append, heartbeat touch, or notification
publication failure is an honest error carrying `checkpoint_recorded=true`, the
sequence, and those same drained messages; a retry cannot hide or redeliver them.
Checkpoint is cooperative and nonterminal:
it is not chat, stdin injection, preemption, cancellation, or a completion
receipt.

The oneshot context still tells the model to call `finish` exactly once with
`done`, `failed`, or `incomplete`. That unchanged tool writes
`daemon_completion.json` with `status`, optional `summary`, optional `reason`
(required by validation when `status` is `failed` or `incomplete`), and optional
`artifacts`. A checkpoint never satisfies this gate. When `daemon_common` is
loaded, a conversational final answer is not enough. Success requires a
validated `finish(status="done")`; missing completion, invalid JSON, invalid
status, run-id mismatch, `failed`, or `incomplete` must prevent terminal `done`.
A missing-finish failure is a contract failure, not by itself proof the
underlying task failed: the failure message tells the parent that before
concluding it should inspect the run's trace and the full final text preserved
in the run directory's physical `result.txt` (on this failure path `result_path`
stays null; the physical file is the discoverable route), and the daemon
context/manual carry the same guidance.

The LingTai loop shares only the pure `LLMResponse` all-empty predicate
(`text`, `tool_calls`, and `thoughts` all empty) with the main agent. An all-empty
response at any daemon-owned provider-send site (kickoff, post-tool
continuation, follow-up, or compact-reset continuation) enters one recovery
state machine: the initial response receives three same-session transient
retries with 1/2/4 second backoff; each retry closes pending tool calls with
`tool_completed=True`, applies the bounded AED history-compaction semantics, and
appends the localized generic `system.stuck_revive` request. The daemon builds
both the fixed safe error description and each localized `MSG_REQUEST` locally;
the first failed post-tool send is described as `after tool results`, while each
later recovery send is a fresh `on initial send` request. Retry/AED events expose
only that fixed description and bounded attempt/phase data, never provider text,
ids, model, or finish reason. Only after that budget does counted AED begin,
using `max_aed_attempts` and preserved-interface session rebuilds; with the
default three counted attempts, two rebuild sends are allowed and the third
exhaustion is terminal. Recovery sends respect daemon cancel/timeout and do not
consume `effective_max_turns`.

Main owns ACTIVE/STUCK/ASLEEP lifecycle states; the daemon owns its cancellation,
timeout, and max-turn boundaries and maps empty-response AED exhaustion to a
clear detached-run `FAILED` receipt. Thoughts-only responses, non-empty text,
other provider exceptions, and partial streams do not enter this path. Canonical
empty assistant/history entries and real tool results remain intact; prior tool
calls are never redispatched. A validated `finish(status="done")` remains the
only completion gate when `daemon_common` is loaded.

### 4. Artifacts separate review evidence from secret-bearing config

Run artifacts must make the daemon contract reviewable without leaking secrets.
`DaemonRunDir` owns the run folder and persistent artifact set: `daemon.json`,
`.prompt`, `.heartbeat`, `history/chat_history.jsonl`, `logs/events.jsonl`,
`logs/token_ledger.jsonl`, `result.txt`, and `artifacts.json`.
`daemon.json.call_parameters` and `.prompt` may contain task surface,
selected-skill catalog/path context, and redacted MCP registrations. Secret MCP
values belong only in native run-scoped launch plumbing where a backend needs
them to mount tools.

External CLI usage is a separate, UI-only lane. A source-reported Codex
`turn.completed` usage object is accepted only when its contract fields are
non-negative integer counts (`input_tokens`, `cached_input_tokens`, and
`output_tokens`); the persisted `cli_tokens.input` is the disjoint
`max(input_tokens - cached_input_tokens, 0)`, while `cached` and `output` are
preserved and `calls` increments once for the terminal event. The raw usage
object is retained in the append-only `cli_usage` event. Missing, malformed,
and all-zero usage is silent, duplicate terminal events do not add another
call, and neither ledger receives a row. No Codex thinking/reasoning field is
projected because this event contract does not prove one.

### 5. Unsupported support status is an explicit capability state

An unsupported backend or transport must stay honest: prompt-catalog-only is not
native tool availability, and unsupported native MCP paths must be omitted or
reported explicitly rather than malformed into a fake-success launch. HTTP MCP
registrations are accepted for the prompt catalog today, and native HTTP
mounting is claimed only for backends whose source-proven config schema
supports it. Other CLI backends keep HTTP prompt-only until a backend-specific
path is implemented and tested.

### 6. Terminal notifications use published receipts, not attempted claims

Every terminal daemon outcome (`done`, `failed`, `cancelled`, `timeout`) must
surface through the per-run mini-channel
`.notification/daemon/<daemon-id>.json`, rather than ordinary parent request
text. `daemon` is a built-in notification channel; both the in-process manager
and detached supervisor use the typed `NotificationStorePort.compare_update_channel`
operation, and the production adapter routes each run id through the narrow
Daemon-only `owner` hot path to its own file. That append is unconditional and
idempotent under its run/control resources; it does not scan the aggregate or
rebuild the sibling report. The Store's durable
`.notification/daemon/.tombstone` control record is the aggregate-clear,
dismiss, and CAS linearization authority: it commits a visibility cut before
best-effort mini-file compaction, so a crash cannot resurrect a cleared event.
A corrupt control record fails loudly into Store/doctor repair visibility rather
than blacking out daemon notices. The sibling `.notification/daemon.json` is a
derived report containing only mini-file run/state statistics; it is excluded
from aggregate snapshot, fingerprint, and dismissal. Existing root event facts may be retained only as
report migration metadata and are never re-delivered; new event writes always
route to the run's mini-file and never fall back to the root. The run directory
may write a temporary `daemon.json.terminal_notification_claim` before publication to
suppress concurrent callbacks, but `daemon.json.terminal_notified=true` is a
receipt and may be written only after `_publish_daemon_notification` succeeds or
an idempotent retry observes an already-published daemon-channel event. Every
new terminal event is typed with `kind="daemon_terminal"` and its terminal
`status`, rather than requiring consumers to parse its human-readable body. A
follow-up (`ask`) result is not a terminal outcome: both publishers type it
`kind="daemon_followup"`, and a consumer counts an event as terminal only when
it is typed terminal or carries a `daemon-terminal:` idempotency key — never
when it is typed follow-up, carries a `daemon-followup:` key, or (the legacy
in-process shape already on disk) reports a `follow-up ...` status.
Guarded by: [D009](BEHAVIORS.md#behavior-d009)

The parent metadata projection carries a bounded current `agent_state.daemon`
summary derived from the same coherent mini-channel aggregate (run/event/active/
terminal counts, terminal-status counts, and at most three latest terminals). It
is refreshed on every stable coherent read — including quiet, masked, and
delayed reads that deliver nothing — so current daemon truth never lags
attention.
When an ASLEEP parent is actually woken by a synthesized notification pair, that
pair additionally carries one-shot `agent_state.notification_wake` provenance:
changed channels plus bounded daemon deltas/latest terminals and, when relevant,
Telegram message ids. It is cleared before later ordinary tool results; a mixed
wake is labeled `source_kind="mixed"` rather than attributed by guesswork. Daemon
deltas are counts of what is new and are never negative: when a dismissal,
clear, or batch reset leaves the aggregate below the delivered baseline, the
baseline is dropped, the remaining counts are reported as new, and
`baseline_reset: true` names the reason.

Failed enqueue must clear the pending claim and leave the terminal run
retryable. Startup reconciliation retries only new-schema terminal run dirs that
explicitly carry `terminal_notified=false`, including stale pending claims left
by a crash. Legacy records with `terminal_notified=true` or with the key absent
are treated conservatively as already handled, not retroactively replayed. The
event idempotency key is stable per terminal run, so a crash after publication
but before receipt persistence does not create a duplicate event on restart. A
mini-channel retains all same-run checkpoint/terminal/follow-up events; there is
no fixed 20-event retention cap. If a run's event is dismissed, its exact
mini-file is removed only after the delivered aggregate version still matches;
a late/newer mini-file therefore survives a stale dismissal.

Delivery is separate from attention. The aggregate daemon projection carries
durable batch state under `data.daemon` (`count` since the last clear, plus a
latched `alarm_fired`). When `<agent>/notification.json` sets
`channels.daemon.alarm_threshold`, arrivals at or below the threshold remain
readable through notification snapshot/check but do not move the attention
fingerprint, so they neither wake nor inject; the strict first `count > N`
crossing produces exactly one alarm edge, and clearing the channel resets the
batch so a later crossing can alarm again. Absent a valid threshold, every
terminal notice wakes the parent as before. Per-run `daemons/<id>/daemon.json`
and result files remain the terminal source of truth regardless of attention
state; the top-level `.notification/daemon.json` is only the derived report.

A consumer `notification(action="delay")` whose target is `daemon` reuses that
same one attention seam for its bounded window. It suppresses daemon *attention*
only, never daemon truth or storage: mini-files, receipts, `daemon.json`, and the
aggregate payload are untouched and stay readable, while the daemon attention
entry collapses to one constant token so no daemon arrival wakes or injects.
Independent channels — including registered hook channels — keep byte-exact
change detection and wake the parent normally throughout, and delay expiry lifts
the mask and publishes the `delay-alarm` mirror in the same sync cycle, so a
delayed daemon channel cannot strand an ASLEEP parent. The delay action itself —
targets, cap, replacement/cancellation, expiry mirror, and fail-open state — is
owned by [`../notification/CONTRACT.md`](../notification/CONTRACT.md) (`delay`).
Guarded by: [N004](../notification/BEHAVIORS.md#behavior-n004)

### 7. LingTai task mapping and self-compact are separate from provider compaction

`task` is the complete parent-controlled daemon system instruction. Optional
`prompt` is only LingTai's first ordinary user message, defaulting exactly to
`Begin the assigned daemon task.` when omitted, empty, or whitespace-only; any
nonblank prompt is preserved byte-for-byte. It is never appended to the system
task or sent to external CLI backends. `system_prompt` is removed with no alias:
callers must migrate the complete instruction into `task`, and preflight
rejects the obsolete field before a run directory is created.

LingTai's final daemon system prompt is composed by the package-owned
`system_prompt.py` from one concise operating contract, the available host-tool
names, parent-provided one-run context, and the complete `task`. The provider's
tool schemas remain authoritative and full tool descriptions are not duplicated
inside the prompt. The operating contract requires progressive disclosure: read
the relevant manual before first using a tool or workflow that has one; use a
visible result tool's `summary=true` only for predictably bulky output whose
exact raw text is unnecessary; and use daemon `compact`, never the unavailable
parent `system.summarize`, for same-run context reset. The complete rendered
string MUST be no more than the resolved `system_prompt_budget_chars` Python
string-character limit. The base value is the per-agent daemon setting in
`<workdir>/daemon/daemon.json`, defaults to 20,000, and accepts only a positive
integer; a missing, malformed, non-integer, boolean, zero, or negative file
value falls back safely to 20,000. A valid positive explicit per-agent daemon
capability kwarg replaces that file value; an invalid explicit value retains it.
Finally, a valid positive-integer
`LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS` environment value overrides both at
`DaemonManager` construction; a missing, blank, malformed, zero, or negative
environment value instead retains the explicit-capability/file resolution.
`lingtai-agent daemon emanate` dispatches through the same `setup()` and manager
resolution, not a CLI-specific parser, and loads the agent's configured
`env_file` (non-overwriting, exactly as boot does) before constructing the
manager, so an override configured there is visible at construction. On POSIX,
a fresh parent reuses a resident central manager only when its live process-start
identity **and** its persisted loaded-code head, source root, and per-run daemon
notification-protocol identity match. A missing, malformed, or mismatched stamp
refuses the new submission before it is queue-owned or receives a capsule; it
does not terminate or take over the old manager's active/capsule-backed runs.
Concurrent submitters for one agent directory serialize the whole
observe/identity-check/`starting`-reservation/spawn sequence under one exclusive
`fcntl.flock` on `daemon/manager/manager.lock`, so later callers re-read
`manager.pid` instead of acting on the same absent or stale observation. A fresh
`starting` reservation is reused under the existing grace; stale-start recovery
remains unchanged.
When the manager's direct Unix-socket path is too long, its capsule transport
uses `/tmp/lingtai-dm-<uid>-<digest>/capsule.sock`, independent of ambient temp
variables. Before stale-socket unlink or bind, the fallback parent MUST be a
real owner-owned mode-0700 directory. An existing entry is reusable only when
it is an owner-owned socket with no group/other permission bits; symlink, type,
owner, or mode mismatches fail closed without unlinking the entry. A normally
created private stale socket remains reusable.
If task or selected skill/MCP context would exceed the resolved budget, prompt
construction fails before the LLM is scheduled and MUST NOT silently truncate
any parent constraint.

Every LingTai daemon receives `compact` automatically, independent of provider.
Its `action` is required and accepts only explicit `run` or `manual`. Execution
uses `compact(action="run", _reason="...")` as the sole assistant-batch tool call;
its canonical `_reason` must be a non-empty, complete self-contained handoff. The
sole compact call/result pair survives a same-run provider-context reset beside
the rebuilt system prompt; the result contains status, resume instruction, and
exact run/state/history/event paths. It is repeatable and non-terminal. The
explicit `manual` action returns read-only procedures, does not compact, and may
be used without `_reason`. External CLI backends never receive `compact`.

### 8. Daemon agent metadata and context countdown

The LingTai daemon's final model-visible `ToolResultBlock` in each tool batch
carries the canonical `_meta.agent_meta` sidecar. Its `agent_state` contains only
that daemon's runtime identity/round counters, current-call and session token
counters, and provider-context token/window/ratio state. It deliberately omits
the parent agent's notification and communication state; only the latest
`agent_meta` snapshot is current and older snapshots are historical traces.

The daemon-owned runtime state uses provider-reported input tokens when available
(and the session estimate only when provider usage is unavailable) against the
resolved context window. On the first round at or above 90%, it starts a fixed
nine-round countdown and emits visible value `9`. Every subsequent high round
decrements the visible `agent_state.context.compact_countdown` while its value is
greater than `1`, exposing values `9` through `1` on the per-round
`_meta.agent_meta` carrier. The `warning` and `compact_countdown_warning` fields
carry one self-contained sentence for the current value:
`Daemon context is at or above 90%. {N} proactive round(s) remain before runtime
mechanical compact; call compact(action="run", _reason="...") now to compact
with your own handoff.`

A provider-context drop below 90% clears the countdown and warning sentence; a
successful proactive compact also clears both from the fresh retained context.
Value `1` remains visible for one ordinary provider response. When a further high
response arrives while the visible value is already `1`, the runtime latches
mechanical compaction due but preserves that value while processing the response.
If that response did not issue a valid sole-call proactive compact, the runtime
mechanically compacts before the next provider continuation if the current
response has tool results to continue. Mechanical compaction retains the rebuilt
system prompt plus the latest assistant `ToolCallBlock`/tool-result pair, then
sends a fresh user recovery instruction that says context was compacted and
requires the daemon to re-read its task, inspect the preserved pair and durable
run artifacts, verify state, and only then continue. It is not silent
continuation. Compaction construction/send failures propagate through the
existing daemon failure path; they are not swallowed.

Provider-specific standalone compaction (including Codex/native-MiMo's
`context_token_limit` path) remains independent: this countdown is daemon-owned,
provider-independent safety state and adds no user configuration or flag.

Separately, `agent_state.context` carries a `system_prompt` key: the same
bounded, body-free progressive-disclosure warning
(`kernel/meta_block.render_system_prompt_pressure_context`) the main agent
carries under its own `agent_state.context.system_prompt`, but derived
entirely from this daemon's own local state. `_DaemonMetaState` counts this
daemon's already-built local rendered system prompt exactly once, at
construction, via the kernel `count_tokens()`. The key is present only while
that local prompt-token count is strictly above the effective `LINGTAI_SYSTEM_PROMPT_PRESSURE_RATIO` threshold (default 40%) of THIS daemon's own
currently-resolved context window (never the parent's window, never a
comparison against parent-scale state), and is omitted whenever the local
prompt tokens or window are unknown/zero. It never repeats or embeds the
prompt body, adds no Nudge/notification/file/timer/dismissal/config surface,
and does not affect or share state with the 90% countdown above.

### 9. Per-task `context_token_limit` is Codex/native-mimo-only and lingtai-backend-only

The daemon task object also carries an optional per-task `context_token_limit`
(positive integer; bool rejected) — a context-token compaction threshold, never
cumulative spend. This capability is narrowly scoped and does not join the
general skills/MCP/completion/backend-support invariants above:

- Effective ONLY for `backend="lingtai"` tasks whose resolved provider is Codex
  (`codex`/`codex-pool`) or the native `mimo` LLM provider, threaded through
  `_daemon_provider_defaults` as `codex_compact_token_limit` /
  `mimo_compact_token_limit` respectively. Every other provider and every
  external CLI backend never receives it.
- Omitted, the threshold uses the daemon session's own resolved context window:
  explicit preset canonical `manifest.llm.context_limit` when supplied,
  otherwise the inherited valid parent effective window, otherwise 272,000.
  This value is only the provider-compaction threshold and does not set the
  daemon context window; an explicit task value always wins for the threshold.
- When the threshold is reached, the Codex or native-MiMo Responses session
  compacts prior context via that provider's standalone `POST /responses/compact`
  endpoint and continues the same tool loop; neither uses the generic OpenAI
  Responses `context_management` axis.
- **Failure policy differs by provider.** A standalone compact call/parse
  failure is non-fatal for Codex; for the native `mimo` provider the same class
  of failure is a hard failure.
- Trigger/boundary/invalidation mechanics are shared Responses adapter/session
  internals (`_StandaloneCompactionMixin`), not daemon-owned. This contract
  states only the daemon-task-object capability boundary.

## A-priori summary composition

The LingTai backend builds one daemon-local summary closure and passes it to the
kernel `ToolExecutor`. The closure:

- uses the effective daemon `LLMService`, provider, and model;
- creates a `tracked=False` session with no tools;
- forwards the existing kernel summary prompt contract; and
- accounts usage through `DaemonRunDir.append_tokens`, keeping daemon and parent
  ledgers consistent.

`ToolExecutor` remains responsible for the summary gate (the parent-facing
`daemon` tool now opts in via the canonical root `summarize`; the daemon
worker's own visible result tools keep their unmigrated `summary=true`
spelling), raw logging,
fail-closed replacement, and the 500,000-character cap. Daemons expose the
run-local `logs/events.jsonl` / `daemon_tool_result` recovery locator. The closure
is inert unless a tool explicitly requests `summary=true`.

## Backend Support Matrix

Guarded by: [D006](BEHAVIORS.md#behavior-d006), [D007](BEHAVIORS.md#behavior-d007)

Current source-backed status:

| Backend / architecture | Selected skills catalog/path | Parent MCP native mounting | `daemon_common` native checkpoint + completion |
|---|---|---|---|
| `lingtai` | Yes, in the daemon prompt/context. | Yes, task-scoped stdio and HTTP MCP clients. | Yes; live checkpoint is available and `finish(done)` is enforced. |
| `claude-p` / `claude-code` | Yes. | Yes for stdio via per-run `--mcp-config`; HTTP omitted. | Yes; live checkpoint and finish use the same per-run config. |
| `codex` | Yes. | Yes for stdio via `-c mcp_servers.*`; HTTP omitted. | Yes; live checkpoint and finish use the same config override path. |
| `opencode` | Yes. | Yes for stdio via `OPENCODE_CONFIG_CONTENT`; HTTP omitted. | Yes; live checkpoint and finish use the same per-process config content. |
| `qwen-code` / `qwen` | Yes. | Yes for stdio via per-run Qwen settings; HTTP omitted. | Yes; live checkpoint and finish use the same settings file. |
| `mimocode` / `mimo` | Yes. | Not wired in this slice; prompt catalog only. | Not wired; do not claim checkpoint or completion. |
| `oh-my-pi` / `omp` | Yes. | Not verified; prompt catalog only. | Not wired; do not claim checkpoint or completion. |
| `kimicode` / `kimi` | Yes. | Yes for stdio and HTTP via run-private `$KIMI_CODE_HOME/mcp.json`. | Yes; live checkpoint and finish use the same run-private config. |
| `cursor` | Yes. | Not verified; prompt catalog only. | Not wired; do not claim checkpoint or completion. |
| `deepseek` | Yes. | Not wired in this slice; prompt catalog only. | Not wired; do not claim checkpoint or completion. |

The native stdio/helper set is source-owned by `_codex_mcp_argv`,
`_opencode_mcp_env`, `_write_qwen_mcp_settings`, `_write_kimicode_mcp_config`,
`_write_claude_mcp_config`, and `_cli_backend_loads_common_mcp`. If a backend is
not in that loaded set, this contract treats it as prompt-catalog-only until
code and tests prove otherwise.

## State & Storage

All paths are relative to the parent agent working directory (`<parent>/`):

```text
<parent>/daemons/<handle>-<YYYYMMDD-HHMMSS>-<hash6>/   # one dir per run (run_id)
  daemon.json                  # identity/live status + checkpoint sequence/latest/pending count
  .prompt                      # system prompt verbatim
  .heartbeat                   # mtime-touched on activity
  history/chat_history.jsonl   # session transcript
  logs/token_ledger.jsonl      # per-call tokens, daemon-scoped (source="daemon")
  logs/events.jsonl            # tool_call / tool_result / cli_output / cli_usage / daemon_checkpoint / daemon_*
  result.txt                   # full terminal result when available

<parent>/daemons/.dispatch-ledger.jsonl    # append-only accepted dispatch membership/order
<parent>/daemons/.dispatch-recovery/       # only unresolved running / pending-terminal markers
<parent>/logs/token_ledger.jsonl   # ALSO receives each daemon token row, tagged
                                    # source="daemon" + em_id + run_id (dual-ledger)
```

Token accounting is dual-ledger: every daemon call appends to the daemon's own
`logs/token_ledger.jsonl` and to the parent's `logs/token_ledger.jsonl`, both
rows tagged `source="daemon"` so `sum_token_ledger(scope="main_agent")`
excludes daemon spend while `scope="all"` includes it.

After preflight and initial `daemon.json` durability, an accepted run MUST append
exactly one `{schema, sequence, run_id, created_at}` dispatch record under the
agent-scoped lock before launch. Append order is canonical; `created_at` is never
an ordering key. A malformed tail refuses a new launch without repair/truncation.
`daemon.json` remains status truth. Default `daemon(list)` reads only the newest
1000 ledger records and their referenced run files, returning scoped advisory
warnings for empty/invalid/gapped/duplicate ledger records and unreadable state.
Explicit search/filter/history may stream more ledger records; neither path
backfills or scans legacy directories. Startup recovery reads only unresolved
marker files, not lifetime history; legacy directories without markers are a
cutover limit. The Agent Record's bounded daemon summary is an asynchronous
ledger-selected snapshot governed by `kernel/session_stats/CONTRACT.md`; no
per-run `session_stats.json` is written.

## Process and Terminal Boundaries

### 10. Task input files are snapshot-only, never live-path reads

Optional `tasks[].task_files` input is copied once, content-addressed, into the
immutable `daemons/_task_files/` store before any run starts; the daemon prompt
and every backend receive only the compact manifest rows (`label`, `role`,
`sha256`, `size`, `snapshot` path). A worker must read the snapshot paths, and
no backend may embed file contents into the prompt/JSONL or point the worker at
the mutable original path — the original may change or disappear after dispatch
without affecting the run, its `check` inspection, or a relaunch. Preflight
fails the whole batch loudly for any malformed, out-of-root, missing, oversize,
or non-UTF-8 entry; there is no silent fallback to the original path.

### External CLI process boundary

Codex, Cursor, the shared OpenCode/MiMo/Oh-My-Pi family, and the Qwen/Kimi raw
one-shot initial `emanate` runners route through the daemon-local process Port.
Qwen and Kimi remain Manager-owned text-capture backends and do not gain a
terminal resume contract from this boundary. While their initial run is active,
`ask` may queue one bounded message only because their launch paths independently
mount `daemon_common`; delivery waits for the model's next checkpoint. `DaemonProcessCommand` is an immutable
argv/cwd/environment value; policy receives only an opaque handle and a
`DaemonProcessExit` containing the raw return code and optional local
termination reason. `PosixDaemonProcessPort` owns POSIX session creation,
stdout iteration, stderr draining, bounded TERM-then-KILL escalation, group/all
ownership, and idempotent release.

Release is non-blocking: it unregisters only a terminal/reaped child. A live
child remains owned after failed quiescence so later group/all sweeps can retry;
release never performs an unbounded wait. A concurrently blocked waiter reads
the final first-writer-wins local termination cause, and group/all sweeps return
the number of targeted children for truthful lifecycle reporting.

`WindowsDaemonProcessPort` is the `os.name == "nt"` production sibling behind
the same Port with the same handle/receipt/release semantics. Composition
selects it wherever POSIX composition selects `PosixDaemonProcessPort`; any
other platform still fails loudly at construction.

### Interactive Claude transport status

The hidden interactive Claude compatibility route has a bounded POSIX-first
transport slice: `InteractiveTerminalPort` and `PosixInteractiveTerminalAdapter`
own only PTY allocation, 120x40 sizing, raw master byte I/O, child
session/process-group termination, reaping, and terminal resource release.
`DaemonManager` injects one adapter and sweeps its group/all ownership during
watchdog timeout, reclaim, and parent stop. The bridge retains all terminal and
result policy. This does not add ConPTY, a pipe-only Windows substitute, or a
public backend name; native Windows interactive support remains deferred until
a genuine ConPTY adapter and native acceptance lane exist. On Windows,
composition injects `interactive_terminal_port=None` and the bridge fails
loudly (`ClaudeInteractiveError`, "requires an injected
InteractiveTerminalPort") — interactive Claude is unsupported-on-Windows, never
a silent no-op.

### POSIX invariants

DOCUMENT ONLY — POSIX behavior is a hard invariant; do not change these
assumptions. Windows has its own invariants section below and never rewrites
POSIX mechanics for symmetry.

- On POSIX, ordinary in-process `DaemonManager` composition keeps
  `start_new_session=True`: its Port owns the private child process group,
  tracked per batch by `group_id`, and stamps the first local reason before
  TERM/KILL so signal return codes are attributed. Detached execution is
  different: the execution child already owns the session/group, so its headless
  and interactive Ports use `start_new_session=False` and carry the explicit
  `INHERITED_SUPERVISOR_GROUP` termination scope. A detached Port
  `terminate`, `terminate_group`, or `terminate_all` signals/reaps only each
  exact `Popen` child; it never sends a group signal to the execution host or
  caller. Only supervisor exact-run reclaim may signal the inherited run PGID.
- The hidden interactive Claude backend uses a POSIX PTY. Native interactive
  support remains explicitly deferred until a ConPTY adapter exists and is
  accepted.
- The LingTai backend spawns no CLI process; its run loop is an in-thread
  `_run_emanation` inside the run's own detached supervisor, whose
  `_control_and_deadline_watcher` flips `cancel_event`/`timeout_event` for
  that loop to observe.

### Windows invariants

Windows (`os.name == "nt"`) maps the POSIX ownership vocabulary onto native
mechanisms; every mechanism import is lazy/guarded so all modules import on
every platform.

- **Daemon-state lock.** The cross-process `daemon.json` transaction lock
  (`.daemon-state.lock`, used by both `DaemonRunDir._state_transaction` and
  `DaemonRunDir.state_file_lock`) is an `msvcrt.locking` byte-range lock on
  byte 0, length 1, acquired with `LK_NBLCK` in an explicit retry loop —
  never `LK_LOCK`, whose CRT blocking mode hides a bounded ~10-attempt
  timeout. POSIX keeps blocking `fcntl.flock(LOCK_EX)` unchanged.
- **Process-group mapping.** `WindowsDaemonProcessPort` with
  `PRIVATE_PROCESS_GROUP` scope gives each spawn its own Job Object, assigned
  while the child is `CREATE_SUSPENDED` and resumed after assignment, WITHOUT
  `KILL_ON_JOB_CLOSE` (POSIX private-session children survive manager death;
  so do Job members here). Group termination is `TerminateJobObject` plus a
  bounded active-process wait. With `INHERITED_SUPERVISOR_GROUP` scope no Job
  is created and lifecycle operations terminate only the exact `Popen` child
  through its retained handle. Windows termination is forceful-only: the
  SIGTERM→SIGKILL ladder collapses to force-then-reap with the same bounded
  waits and first-writer-wins reason receipts.
- **Identity and signalling.** Process identity is
  `windows:<creation_filetime>` from the shared `_win32` observation surface;
  `pgid` fields are recorded as `None` and every signal path stays fail-closed
  on missing/mismatched identity. `os.kill(pid, 0)` is NEVER used as a
  liveness probe on Windows (it terminates); liveness uses
  `OpenProcess`/`GetExitCodeProcess`.
- **Narrower supervisor reclaim (residual).** Supervisor exact-run reclaim on
  Windows performs identity-guarded `TerminateProcess` of only the exact
  recorded nested-CLI and execution-child PIDs. Grandchildren spawned outside
  those exact PIDs are not swept by the supervisor (there is no inherited
  process group to signal); the execution Port's own Job/exact-child
  ownership is the containment boundary.
- **Capsule transport.** The one-shot secret capsule crosses to detached
  processes as an inherited pipe HANDLE allowed through
  `STARTUPINFO.lpAttributeList["handle_list"]`; the child environment carries
  only the numeric handle (`LINGTAI_DAEMON_CAPSULE_HANDLE`). Capsule bytes
  never touch disk, argv, or an environment value, keep the 4 MiB bound, and
  are consumed exactly once (see `kernel/daemon_supervisor/CONTRACT.md`).
- **Interactive backend.** No interactive terminal port exists on Windows;
  the claude-interactive backend fails loudly (see the transport status
  section above). ConPTY remains out of scope.

## Execution Ownership: One Detached Supervisor per Run

Every backend is created under one detached supervisor process at emanation
birth. The parent `DaemonManager` validates the request, writes a secret-free
manifest, launches the platform's supervisor entrypoint (POSIX or Windows
adapter, selected by `select_daemon_supervisor_adapter`; other platforms fail
loudly), and retains only a durable submit / inspect / control facade. `execution_host.py` composes the existing
`DaemonManager` and `_BackendSpec` execution units inside the supervisor, so
all backend parsers, option/session behavior, native MCP setup, skills/preset
setup, and completion gates remain single-source production code. The
supervisor owns the exact child process group, deadline, run-owned diagnostics,
terminal state, result/artifact files, and one idempotent terminal notification.

Agent stop and `system.refresh` shut down only parent-local resources; they do
not inspect or terminate a detached supervisor or its backend child. Explicit
`daemon(action="reclaim")` is the only parent control that requests run
cancellation. `daemon(action="ask")` uses the run-local control spool and is
accepted only while durable state is running. The ownership transition is
unconditional; detached supervision is not gated behind a production flag.

The detached execution child is not currently given derived-launch authority.
Its production composition root sets only the restrictive
`_requires_derived_launch_admission_port` requirement. Therefore, if its full
tool surface reaches a nested daemon/avatar launch, absence of a real authority
is a structured `required_derived_launch_admission_port_missing` refusal before
launch side effects; it must not fall back to generic `legacy_default` allow.
This requirement flag is not a grant, parent identity, or bearer. A future
Driver authority bridge must supply those separately before any legitimate
derived launch can be allowed.

The POSIX manager/supervisor child-endpoint lifecycle can now transfer one
already-open descriptor through the private manager socket and both detached
process boundaries without persisting it. Each accepting API adopts ownership
immediately; successful transfer closes the sender copy, while failed ACK,
replacement, queued cancellation, malformed work, pre-execution failure, and
process exit close the current owner. Manager restart cannot reconstruct the
descriptor and follows the existing missing-capsule terminal failure.

That lifecycle is not yet production Driver wiring. A root Driver batch that
receives one or more child endpoint leases remains refused before task-file
materialization, run-directory creation, durable enqueue, or spawn, and every
already-issued lease is closed. An external CLI backend is refused even
earlier, before it asks Driver for a lease it cannot consume. The execution
child holds no composed derived authority and closes any unconsumed descriptor.
These remain deliberately fail-closed transition rules until authority adoption
and dispatch wiring are added separately.

## Acceptance Gate

Any new daemon backend, backend-family reuse, or contract-impacting daemon
change must prove all applicable items:

1. Selected skills catalog/path context is visible in the final prompt/context
   without pasting SKILL.md bodies.
2. LingTai ToolResultBlocks carry daemon-local `_meta.agent_meta` runtime,
   token, and context state; parent notification/communication state is absent,
   the latest snapshot is current, and the exact warning is present on every
   round whose current context usage is >=90% and absent below that threshold.
   `agent_state.context.system_prompt` is present only while this daemon's own
   local rendered-prompt tokens are strictly above the effective `LINGTAI_SYSTEM_PROMPT_PRESSURE_RATIO` threshold (default 40%) of this daemon's own
   resolved window, and is derived from local state only — never the parent's.
3. `compact(action="manual")` is read-only; `action` is required, omission is
   refused without state change, and explicit `compact(action="run", _reason="...")`
   remains a repeatable non-terminal sole-call reset whose surviving result
   reflects the fresh retained context.
4. Parent MCP registrations appear in prompt context and durable call
   parameters with `env` and `headers` values redacted.
5. Native MCP config includes parent registrations only for transports and
   backends with a verified run-scoped loader; unsupported transports are
   omitted or reported honestly.
   LingTai task-scoped calls additionally prove that undeclared host-private
   arguments do not cross the provider boundary, without filtering unknown
   business arguments or weakening strict LTP-v2 restoration.
6. `daemon_common` is available only on source-proven loaders. Its live
   checkpoint durably stores sequence/latest state and drains an ID-bound bounded
   inbox exactly once before appending an event, refreshing heartbeat, and
   publishing a unique nonterminal wake. Any failure after that durable write
   returns `checkpoint_recorded=true` and the drained messages without redelivery.
   Terminal success remains separately gated by valid `finish(status="done")`.
7. Unsupported backends remain documented as prompt-catalog-only or fail
   explicitly; they must not imply tool availability from prompt text alone.
8. `.prompt`, `daemon.json`, native config files/env/argv/settings,
   `result.txt`, `events.jsonl`, heartbeat, and artifact manifests remain
   inspectable within the daemon run boundary while secret-bearing native config
   is not copied into review artifacts.
9. Terminal notification tests prove failure retry, restart reconciliation,
   concurrent done-callback idempotency, crash-window idempotency, legacy
   `terminal_notified=true` and missing-key compatibility, and absence of a
   caller-facing notification toggle.

## Review Triggers

Re-check this contract when touching:

- `src/lingtai/tools/daemon/__init__.py` backend routing, selected-skill catalog
  assembly, MCP registration handling, native config writers, compact handling,
  or completion enforcement.
- `src/lingtai/tools/daemon/settings.py` row ownership, defaults, or manual
  pointers.
- `src/lingtai/cli_daemon.py` owner-directory resolution, command set, `wait`
  progress/exit semantics, or JSON output shapes.
- `src/lingtai/tools/daemon/run_dir.py` artifact paths, `daemon.json`
  `call_parameters`, checkpoint sequence/latest/inbox fields, redaction-sensitive
  fields, terminal markers, terminal-notification receipt fields, or manifests.
- `src/lingtai/tools/daemon/manual/` daemon argument semantics, backend status,
  MCP capability guidance, compact guidance, or completion guidance.
- `src/lingtai/mcp_servers/daemon_common/` checkpoint/finish schemas,
  RunDir identity/wake behavior, payload file, or server behavior.
- `tests/test_daemon*.py` coverage that proves backend options, CLI native MCP,
  daemon_common completion, OpenCode-family routing, Qwen settings, Claude print
  MCP config, run-dir artifacts, prompt redaction, selected-skill catalog
  preservation, prompt mapping, or compact context reset behavior.
- `tests/test_codex_standalone_compaction.py` for per-task
  `context_token_limit` wiring/pre-flight validation.

## Anchored Claims

| Claim | Source | Test |
|---|---|---|
| the parent dispatches seven actions; unknown actions error | `src/lingtai/tools/daemon/__init__.py`, `src/lingtai/tools/daemon/_tool_family.py` | `tests/test_tool_family_daemon_migration.py`, `tests/test_daemon_check.py::test_check_unknown_id_returns_error` |
| Default `manager_pool_size` is 100 and the config reaches the manager/list output | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon.py::test_daemon_default_manager_pool_size_is_100`, `::test_daemon_manager_pool_size_config_reaches_manager` |
| Concurrent submitters for one agent directory serialize manager observe/reserve/spawn under `manager.lock`; later callers re-read the persisted reservation instead of acting on the same absent state | `src/lingtai/adapters/posix/daemon_manager.py` | `tests/test_daemon_central_manager.py::test_concurrent_ensure_manager_callers_reserve_and_spawn_one_manager` |
| `max_turns` precedence is valid `LINGTAI_DAEMON_MAX_TURNS`, explicit capability/setup, valid owner file, then 5000; invalid environment input retains the lower valid result | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon.py::test_daemon_max_turns_env_beats_explicit_and_config`, `::test_daemon_invalid_max_turns_env_keeps_explicit_value` |
| Per-agent `system_prompt_budget_chars` defaults to 20,000, accepts a positive `daemon/daemon.json` override, and safely falls back for malformed/non-positive values while retaining fail-loud/no-truncation rendering | `src/lingtai/tools/daemon/__init__.py`, `src/lingtai/tools/daemon/system_prompt.py` | `tests/test_daemon.py::test_daemon_default_system_prompt_budget_is_20000_without_config`, `::test_daemon_config_system_prompt_budget_allows_larger_complete_prompt`, `::test_daemon_invalid_system_prompt_budget_falls_back_to_default` |
| `settings` returns exactly the four owner rows and five public fields, has no mutation route, and fails as one fixed response when current manager truth is unavailable | `src/lingtai/tools/daemon/settings.py`, `src/lingtai/tools/daemon/_tool_family.py` | `tests/test_daemon_settings.py` |
| Budget precedence: a valid `LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS` wins at manager construction (including `lingtai-agent daemon emanate` via the agent's `env_file`); invalid env or explicit capability values retain the file/default resolution and never drop the capability | `src/lingtai/tools/daemon/__init__.py`, `src/lingtai/cli_daemon.py` | `tests/test_daemon.py::test_daemon_system_prompt_budget_env_overrides_config`, `::test_daemon_invalid_system_prompt_budget_env_keeps_config_value`, `::test_daemon_invalid_explicit_system_prompt_budget_keeps_file_or_env`, `tests/test_cli_daemon.py::test_emanate_env_file_budget_overrides_daemon_json` |
| Every daemon CLI command takes `--owner-dir` (legacy `--agent-dir`, one destination), speaks "owner" in help/errors, needs no live Agent, and leaves no lease/heartbeat/identity marker; run directories stay under `<owner>/daemons/` | `src/lingtai/cli_daemon.py` | `tests/test_cli_daemon.py::test_owner_dir_and_legacy_agent_dir_share_one_destination`, `::test_every_daemon_command_documents_owner_dir`, `::test_owner_dir_errors_speak_owner`, `::test_standalone_owner_dispatch_keeps_state_owner_local_without_a_lease` |
| The detached supervisor's terminal notification lands under the manifest's owner directory | `src/lingtai/tools/daemon/supervisor_runtime.py` | `tests/test_cli_daemon.py::test_supervisor_terminal_notification_anchors_to_the_owner_dir` |
| CLI `ask` routes id/message through the tool-family `ask` child only; `sent`/`queued` exit 0, `busy`/`error` exit 1, blank messages never reach the engine | `src/lingtai/cli_daemon.py` | `tests/test_cli_daemon.py::test_ask_dispatches_through_the_daemon_family`, `::test_ask_exit_status_tracks_the_engine_result`, `::test_ask_unknown_id_is_refused_by_the_real_engine`, `::test_ask_refuses_a_blank_message_before_dispatch` |
| CLI `wait` reports each progress change once, maps terminal state to exit 0/1, exits 124 on `--timeout` and 130 on interrupt, emits JSONL under `--json`, and constructs no manager and writes nothing | `src/lingtai/cli_daemon.py` | `tests/test_cli_daemon.py::test_wait_reports_each_progress_change_once_then_exits_zero_on_done`, `::test_wait_json_emits_one_record_per_change_and_a_terminal_record`, `::test_wait_exit_status_reflects_the_terminal_state`, `::test_wait_timeout_exits_124_and_writes_nothing`, `::test_wait_interrupt_exits_130_with_a_final_record` |
| Backend schema enum matches the ordered alias contract | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon_backend_options.py::test_backend_schema_enum_matches_ordered_contract`, `::test_backend_metadata_consistency_keeps_hidden_legacy_claude` |
| `check` returns state + events, honors `last`/`truncate`, validates inputs | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon_check.py` |
| CLI-backend terminal `ask` returns immediately and enforces its own timeout | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon.py::test_ask_codex_returns_immediately_when_subprocess_hangs`, `::test_ask_codex_silent_subprocess_enforces_timeout` |
| Active common-MCP CLI `ask` queues an ID-bound next-checkpoint message; checkpoint records/drains/wakes without terminal mutation and old live RunDirs backfill fields | `src/lingtai/tools/daemon/__init__.py`, `src/lingtai/tools/daemon/run_dir.py`, `src/lingtai/mcp_servers/daemon_common/server.py` | `tests/test_daemon_checkpoint.py`, `tests/test_daemon_run_dir.py::test_checkpoint_inbox_backfills_pre_checkpoint_live_state` |
| Token rows are written to both the daemon and parent ledgers, tagged | `src/lingtai/tools/daemon/run_dir.py` | `tests/test_daemon_run_dir.py::test_append_tokens_writes_daemon_ledger`, `::test_append_tokens_writes_parent_ledger_tagged` |
| `context_token_limit` is validated, reaches Codex and native `mimo`, and is inert for every other provider and every external CLI backend | `src/lingtai/tools/daemon/__init__.py` | `tests/test_codex_standalone_compaction.py`, `tests/test_mimo_responses_compaction.py` |
| `tasks[].plugin` renders the `## Parent-selected plugins` section into the durable `.prompt` the detached child reads; plugin skills and mcp.json servers are merged/mounted; missing plugin paths resolve to nothing; non-list fails preflight | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon.py::test_task_plugin_context_renders_catalog_and_flattens_skills_mcp`, `::test_task_plugin_context_rejects_bad_plugin_path`, `::test_task_plugin_context_rejects_non_list`, `::test_handle_emanate_writes_plugin_section_to_prompt_before_detach` |
| LingTai daemon tool results carry daemon-local `_meta.agent_meta`, omit parent notifications/guidance, and carry the exact warning only while current usage is >=90% | `src/lingtai/tools/daemon/__init__.py`, `src/lingtai/kernel/meta_block.py` | `tests/test_daemon.py::test_daemon_agent_meta_is_local_and_warning_tracks_current_usage` |
| LingTai task-scoped MCP calls remove server-undeclared `_reasoning`, retain ordinary unknown business fields, and preserve strict LTP-v2 restoration | `src/lingtai/services/mcp.py`, `src/lingtai/tools/daemon/__init__.py` | `tests/test_mcp_v2_adapter_metadata.py::test_task_daemon_adapts_host_private_arguments_at_mcp_boundary` |
| `_DaemonMetaState.snapshot` carries `agent_state.context.system_prompt` only while this daemon's own local rendered prompt is strictly above the effective `LINGTAI_SYSTEM_PROMPT_PRESSURE_RATIO` threshold (default 40%) of its own resolved window, never the parent's | `src/lingtai/tools/daemon/__init__.py`, `src/lingtai/kernel/meta_block.py` | `tests/test_daemon.py::test_daemon_meta_state_system_prompt_warning_is_local_not_parent` |
| `compact.action` is required; `manual` is read-only, omission is refused, and explicit `run` resets with fresh post-compact metadata | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon.py::test_compact_schema_requires_explicit_run_or_manual_action`, `::test_compact_missing_action_is_refused_without_reset`, `::test_compact_success_prunes_to_system_call_and_result` |
| `reclaim` cancels running emanations; agent stop shuts the daemon down first | `src/lingtai/tools/daemon/__init__.py` | `tests/test_lifecycle_daemon_shutdown.py::test_agent_stop_shuts_down_daemon_before_heartbeat_and_lock` |
| `emanate`'s explicit `timeout` (schema: `minimum: 5`, no `maximum`) is an uncapped override reaching `daemon.json` and `supervisor_manifest.json` unchanged; omitting it (`null`) falls back to the manager's default. Unlike `timeout`, `max_turns` has a schema `maximum` and IS capped at the manager's ceiling. | `src/lingtai/tools/daemon/__init__.py` | `tests/test_daemon_per_batch_limits.py::test_emanate_honors_explicit_timeout_above_default_ceiling`, `::test_emanate_caps_max_turns_at_ceiling` |

## Verification Matrix

| Invariant | Automated test | Manual check | Risk if broken |
|---|---|---|---|
| Action dispatch + per-action shapes are stable | `tests/test_daemon.py`, `tests/test_daemon_check.py` | `emanate` a trivial task, then `check` its id | Agents cannot dispatch or inspect subagents |
| Settings projection stays exact, owner-documented, and read-only | `tests/test_daemon_settings.py`, `tests/test_tool_settings_contract.py` | Call `settings`, inspect the five fields, use each `comment` to route to the manual, and confirm `list` is unchanged | Effective daemon settings drift or become falsely mutable |
| Backend enum/alias contract stays consistent | `tests/test_daemon_backend_options.py::test_backend_schema_enum_matches_ordered_contract` | Pass an alias (`mimo`) and confirm it normalizes | Backend selection drifts from advertised names |
| Terminal state is classified from the recorded snapshot | `tests/test_daemon_check.py::test_check_includes_terminal_event_for_done_emanation` | Run to completion, confirm `state=done` in `check` | Parent mis-reads timeout/cancel as success |
| CLI `ask` never blocks the caller's tool thread | `tests/test_daemon.py::test_ask_codex_returns_immediately_when_subprocess_hangs` | `ask` a hung CLI daemon, confirm immediate return | Parent loop stalls on a hung subprocess |
| Reclaim kills every tracked CLI proc; each run's own detached supervisor kills only its own exact child on timeout | `tests/test_daemon_cli_watchdog_scope.py`, `tests/test_lifecycle_daemon_shutdown.py` | Emanate two runs, reclaim, confirm both are killed; let one run time out and confirm only its own child dies | Reclaim misses a tracked proc, or one run's timeout kills an unrelated run's child |
| Dual-ledger token accounting stays correct | `tests/test_daemon_run_dir.py::test_append_tokens_writes_parent_ledger_tagged` | Inspect both token_ledger.jsonl files after a run | Daemon spend double-counted or lost in totals |
| `context_token_limit` stays Codex/native-mimo-only and inert everywhere else; native `mimo` compaction failure is a HARD failure | `tests/test_codex_standalone_compaction.py`, `tests/test_mimo_responses_compaction.py` | Emanate a `backend='lingtai'` Codex task with an explicit `context_token_limit`, then repeat with native `mimo` | A bad value silently breaks unrelated providers/backends or swallows a hard MiMo failure |
| Task-scoped MCP host-private argument isolation preserves server schema authority | `tests/test_mcp_v2_adapter_metadata.py::test_task_daemon_adapts_host_private_arguments_at_mcp_boundary` | Mount a closed-schema MCP tool, invoke it with model reasoning, and inspect provider-bound arguments | Kernel rationale leaks to providers or business schema errors are silently masked |

Run before merging daemon tool-surface changes:

```bash
python -m pytest tests/test_daemon_settings.py tests/test_tool_family_daemon_migration.py tests/test_tool_settings_contract.py tests/test_daemon.py tests/test_daemon_check.py tests/test_daemon_backend_options.py tests/test_daemon_run_dir.py tests/test_lifecycle_daemon_shutdown.py tests/test_codex_standalone_compaction.py tests/test_mimo_responses_compaction.py -q
```

## Schema and Glossary Ownership

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


## Detached Shell async prompt events

For a selected `shell` in `DetachedDaemonExecutionHost`, the execution host
invokes Shell's private detached composer. It constructs
`DaemonShellPromptEventAdapter(run_dir)`, passes it as Shell's existing
`notifications` grant, and supplies only `<run>/shell-jobs` as Shell's async
state namespace. Command cwd remains the granted parent task workdir, but Shell
creation, activation, rehydration, publication, poll, and cancel cannot observe
or claim the parent `system/jobs` namespace or another daemon's jobs. Public
Shell setup and manifest capability configuration cannot select this destination
or private namespace. This does not create a second Agent or grant a new plugin
runtime. The adapter accepts only Shell's stable reminder and completion
publications and calls `DaemonRunDir.enqueue_shell_prompt_event`.

`daemon.json` holds a bounded `pending_shell_prompt_events` queue and bounded
delivered-ref history. Each event is ref-deduplicated and contains only `kind`,
`ref_id`, `job_id`, queue timestamp, and completion exit metadata; no command,
stdout, stderr, preview, or arbitrary publication body is provider-visible. A
full queue, filesystem failure, or non-running run returns `False` to Shell, so
its durable publication state stays retryable. In the selected live manager,
failed reminder publication is re-armed and failed completion publication stays
watched with capped exponential backoff; draining capacity therefore delivers one
stable ref exactly once without auto-polling. A successful enqueue (or stable
duplicate) is its publication acknowledgement. Queued/delivered audit records
land in the run event log.

`_run_emanation` drains only already-persisted events at a legal text-only
provider-send boundary after the preceding assistant tool-call pair is complete.
It records a `shell_completion` or `shell_reminder` history entry and sends fixed
trusted guidance containing the job id and `call shell.poll for exact output`.
The loop never auto-polls, reads Shell logs, consumes a terminal Shell result,
creates a parent wake per job, or waits for a future event. Once this daemon is
terminal, enqueue fails and no event revives or holds it; the supervisor's
existing terminal daemon receipt remains the only final parent wake. These events
are neither `daemon_common` checkpoint traffic nor `.notification` artifacts.
