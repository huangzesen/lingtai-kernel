---
related_files:
  - src/lingtai/ANATOMY.md
  - src/lingtai/tools/daemon/BEHAVIORS.md
  - src/lingtai/tools/daemon/CONTRACT.md
  - src/lingtai/tools/daemon/__init__.py
  - src/lingtai/tools/daemon/_tool_family.py
  - src/lingtai/tools/daemon/settings.py
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/tools/daemon/system_prompt.py
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/tools/tool_family/__init__.py
  - src/lingtai/prompts/ANATOMY.md
  - src/lingtai/kernel/meta_block.py
  - src/lingtai/kernel/llm/base.py
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/kernel/notification_store/CONTRACT.md
  - src/lingtai/adapters/posix/notification_store.py
  - src/lingtai/kernel/tool_executor.py
  - src/lingtai/kernel/tool_result_summary.py
  - src/lingtai/services/mcp.py
  - src/lingtai/llm/service.py
  - src/lingtai/llm/interface_converters.py
  - src/lingtai/llm/openai/ANATOMY.md
  - src/lingtai/tools/daemon/process_port.py
  - src/lingtai/tools/daemon/interactive_terminal/__init__.py
  - src/lingtai/tools/daemon/interactive_terminal/CONTRACT.md
  - src/lingtai/tools/daemon/interactive_terminal/ANATOMY.md
  - src/lingtai/adapters/posix/interactive_terminal.py
  - src/lingtai/tools/daemon/posix_process.py
  - src/lingtai/tools/daemon/windows_process.py
  - src/lingtai/tools/daemon/claude_interactive.py
  - src/lingtai/tools/daemon/manual/SKILL.md
  - src/lingtai/cli_daemon.py
  - tests/test_cli_daemon.py
  - src/lingtai/tools/daemon/run_dir.py
  - src/lingtai/tools/daemon/shell_prompt_events.py
  - src/lingtai/tools/bash/ANATOMY.md
  - tests/test_daemon_shell_prompt_events.py
  - src/lingtai/tools/daemon/dispatch_ledger.py
  - src/lingtai/kernel/daemon_dispatch.py
  - src/lingtai/kernel/session_stats/ANATOMY.md
  - src/lingtai/kernel/session_stats/CONTRACT.md
  - src/lingtai/mcp_servers/daemon_common/server.py
  - tests/test_daemon.py
  - tests/test_daemon_dispatch_ledger.py
  - tests/test_daemon_attention_delay.py
  - tests/test_daemon_central_manager.py
  - tests/test_tool_family_daemon_migration.py
  - tests/test_daemon_settings.py
  - tests/test_daemon_empty_parity.py
  - tests/test_apriori_summary_executor.py
  - tests/test_daemon_run_dir.py
  - tests/test_daemon_codex_usage.py
  - tests/test_mcp_v2_adapter_metadata.py
  - tests/test_codex_standalone_compaction.py
  - tests/test_daemon_detached_supervisor.py
  - src/lingtai/kernel/daemon_supervisor/ANATOMY.md
  - src/lingtai/kernel/daemon_supervisor/CONTRACT.md
  - src/lingtai/kernel/daemon_supervisor/__init__.py
  - src/lingtai/kernel/daemon_supervisor/manifest.py
  - src/lingtai/kernel/daemon_supervisor/control.py
  - src/lingtai/kernel/daemon_supervisor/agent_stub.py
  - src/lingtai/adapters/posix/daemon_supervisor.py
  - src/lingtai/adapters/posix/daemon_capsule.py
  - src/lingtai/adapters/posix/daemon_manager.py
  - src/lingtai/adapters/posix/daemon_supervisor_entrypoint.py
  - src/lingtai/adapters/posix/daemon_execution_child_entrypoint.py
  - src/lingtai/adapters/posix/daemon_resume_owner_entrypoint.py
  - src/lingtai/adapters/posix/process_identity.py
  - src/lingtai/adapters/windows/daemon_supervisor.py
  - src/lingtai/tools/daemon/runtime.py
  - src/lingtai/tools/daemon/glossary-en.md
  - src/lingtai/tools/daemon/glossary-zh.md
  - src/lingtai/tools/daemon/glossary-wen.md
  - src/lingtai/tools/daemon/manual/reference/cleanup/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/forensics/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/inspection/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/dispatch-ledger/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/claude-p/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/codex/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/cursor/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/deepseek/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/kimicode/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/lingtai/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/mimocode/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/oh-my-pi/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/opencode/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/qwen-code/SKILL.md
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/tools/ANATOMY.md
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# core/daemon

Daemon capability (分神) — dispatch ephemeral subagents (分神) that operate
in parallel on the agent's working directory. Each LingTai-backend emanation
is a disposable `ChatSession` with a curated tool surface, not an agent; the
`task` is the complete parent-controlled system instruction; LingTai may add an
optional first-user `prompt` (with an exact default) while external CLI backends
keep `task` as their CLI prompt. The parent may select
role, constraints, and tool-use policy; per-task `skills` paths render into a
compact progressive-disclosure skill catalog; and per-task `mcp` supplies full one-run MCP
registrations serialized as YAML for all backends, loaded as task-scoped MCP
tools by the LingTai backend, mounted through native stdio MCP config for
the Claude print/Codex/OpenCode/Qwen CLI backends, and mounted through native
stdio/HTTP MCP config for Kimi Code via a run-private `$KIMI_CODE_HOME/mcp.json`.
The maintained unified daemon
contract lives in `CONTRACT.md`. MCP-capable backends also get the built-in
`daemon_common` MCP (`src/lingtai/mcp_servers/daemon_common/server.py`): its
live-only `checkpoint` tool atomically records sequence/latest state, drains
ID-bound parent messages exactly once, appends an event, refreshes heartbeat,
and publishes a nonterminal wake; its unchanged `finish` tool writes
`daemon_completion.json`, and only a validated `finish(status="done")` allows
terminal `done`. Parent MCP tools are not
auto-inherited. The daemon-eligible `email` intrinsic is available only when explicitly requested in a task's `tools` list, and daemon tool
calls still pass through the kernel `ToolExecutor`/`ToolCallGuard` path before
any handler runs. Each `daemon.emanate` batch gets a stable `group_id` shared by
every run in that batch, while each daemon still keeps its own `run_id`. Each
LingTai-backend daemon with no preset builds a fresh daemon-scoped `LLMService`
mirroring the parent (provider/model/base_url/key_resolver/context_window) and
preserving the parent's provider defaults, rather than reusing the parent
service. Codex additionally derives a daemon-scoped cache anchor from each run's
`daemon.json`, so the child's `session_id`, `thread_id`, and `prompt_cache_key`
do not collide with the parent agent's cache slot. A preset/manifest `codex_auth_path` (the per-agent Codex
OAuth token file) is among the preserved provider defaults — and is on the
`_llm_defaults_from_manifest` preset allowlist — so preset-driven daemon work
authenticates against the same Codex account as the parent. A per-task
`context_token_limit` (positive int; validated pre-flight in
`_handle_emanate`) is threaded through `_run_emanation` into
`_daemon_provider_defaults` as `codex_compact_token_limit` (Codex) or
`mimo_compact_token_limit` (the native `mimo` provider) — effective only
when the task's resolved provider is one of those two; every other provider
and every external CLI backend ignores it. Omitted, the session falls back
to its own resolved `context_window()` as the threshold. See
`src/lingtai/llm/openai/ANATOMY.md` for the shared `_StandaloneCompactionMixin`
mechanics this threshold drives, and `src/lingtai/llm/mimo/ANATOMY.md` for
MiMo's hard-failure-on-compact-error divergence from Codex's non-fatal
policy. Results are persisted in per-run daemon folders; **every** terminal outcome
(done / failed / cancelled / timeout) is surfaced as a compact event in
`.notification/daemon/<daemon-id>.json` — never as ordinary parent request text.
The typed Store's daemon-only owner mutation is the append hot path: it takes the
run/control scopes and does not scan the aggregate or rebuild a report. Durable
`.notification/daemon/.tombstone` state serializes aggregate clear/dismiss/CAS
and commits logical removal before later best-effort compaction. The sibling
`.notification/daemon.json` is a non-authoritative derived report of mini-file
run/state statistics, excluded from delivery, fingerprinting, and dismissal; any
retained legacy-root facts live only under report migration metadata. Same-run
checkpoint, terminal, and follow-up events append without a fixed retention cap;
`daemon.json.terminal_notified` is written only after publication succeeds (or an
idempotent retry observes the same published event), so a parent that dispatched
a daemon can safely go idle and be woken when the run ends, without polling.

Strict all-empty recovery parity with the main agent shares only the pure
`LLMResponse` predicate `is_all_empty_response`
(`src/lingtai/kernel/llm/base.py:80-91`): text, tool calls, and thoughts must
all be empty; thoughts-only output is not empty. The daemon-owned loop mirrors
main's initial response plus three same-session transient retries with 1/2/4
second backoff, pending-call healing/compaction, localized
`system.stuck_revive`, then counted AED with preserved-interface rebuilds. The
daemon builds the fixed safe error description and localized `MSG_REQUEST`
locally for each provider send: the first post-tool failure says `after tool
results`, while every later recovery send is a fresh `on initial send` request.
Retry/AED events carry only that fixed description plus bounded phase/attempt
fields; provider payloads and BaseAgent private helpers do not cross this
boundary. Main owns ACTIVE/STUCK/ASLEEP; daemon owns cancel/timeout/max-turn
and maps terminal exhaustion to detached run FAILED. The daemon does not adopt
the BaseAgent lifecycle, service, session, or executor.

## Components

- `daemon/__init__.py` — public capability surface. `get_description`, `get_schema`, and `setup`; the core class is `DaemonManager`, which manages the full emanation lifecycle and parent-stop cleanup. Key internals: `DECLARATION` and `_bind_daemon()` make this the second official
host-plugin slice; `DaemonManager` receives only `workdir` and the named
`DaemonRuntimePort`, while the production `AgentDaemonRuntimeAdapter` owns the
host-private preset/detached capability collector that captures ordinary
`add_tool` calls and accepts registrar-issued official mount transactions into
its own local claim/binding maps without mutating the parent's registry; this is
what lets the declared File family remain declared inside a daemon sandbox. `EMANATION_BLACKLIST` (`daemon/__init__.py:558`) prevents recursion by blocking `daemon`, `avatar`, `psyche`, `skills`, and `knowledge`; `_parent_host_tool_floor()` (`daemon/__init__.py:579`) is the explicit NARROW allowlist of always-on host tools a preset emanation may borrow from the parent — exactly `shell` and `file` (read/write/edit/glob/grep). It is intentionally independent of `CORE_DEFAULTS`, so new core defaults cannot silently widen the child surface; provider-bound parent tools (`vision`, `web_search`) remain excluded so a preset that omits/fails a provider cap does not silently fall back to the parent; `_recover_unresolved_markers()` reads only small running/pending-terminal recovery markers at construction; it never scans lifetime run folders; `_write_kimicode_mcp_config()` (`daemon/__init__.py:309-337`) writes the run-private Kimi `mcp.json` for stdio and HTTP registrations; `_daemon_intrinsic_surface()` synthesizes only the automatic LingTai `compact` schema — daemons no longer inherit any intrinsic, `email` included: it is provided as an MCP tool (`mcp_servers/daemon_email/`, wired via `_daemon_email_mcp_registration()`/`_with_daemon_email_mcp()`), auto-mounted only when a task's `tools` explicitly requests `email`, mirroring exactly how `daemon_common` is auto-mounted for `checkpoint` and `finish`; `_task_mcp_registrations()` (`daemon/__init__.py:1296`) validates full per-task MCP registrations and renders prompt-safe YAML; `_daemon_common_mcp_registration()` / `_read_daemon_completion()` add the built-in checkpoint/completion MCP and validate its terminal receipt; `_connect_task_mcp_registrations()` (`daemon/__init__.py:1998-2085`) starts task-scoped MCP clients for the LingTai backend and uses each server's pre-normalization schema to remove undeclared host-private arguments or restore the strict LTP-v2 public `reasoning` field immediately before dispatch; `_daemon_provider_defaults()` (`daemon/__init__.py:1695`) preserves the parent/preset provider defaults for every provider and adds the per-run `daemon.json` cache anchor only for Codex; `_build_tool_surface()` (`daemon/__init__.py:1786`) automatically includes `compact`, includes only task-scoped MCP tools rather than auto-inheriting parent MCP tools, and — for preset-driven emanations — preserves the parent's narrow host tool floor (`_parent_host_tool_floor()`: shell + file primitives) that saved presets omit from `manifest.capabilities`, so requested host tools valid in the parent set are not rejected as unknown (resolution is preset-first, then intrinsics/MCP, then parent host fill-in); `_DaemonMetaState` (`daemon/__init__.py:177-333`) owns the per-emanation runtime/token/context snapshot, deterministic nine-round >=90% countdown, visible self-contained warning guidance, and expiry latch after the final warning's ordinary response opportunity; it also counts this daemon's own local rendered system prompt once (via the kernel `count_tokens()`) at construction and surfaces the shared `meta_block.render_system_prompt_pressure_context` progressive-disclosure warning under `context.system_prompt` whenever that local prompt/window ratio is strictly above the effective `LINGTAI_SYSTEM_PROMPT_PRESSURE_RATIO` threshold (default 40%) of this daemon's own currently-resolved window — never the parent's. `_run_emanation()` (`daemon/__init__.py:2740`) is the LingTai-backend worker loop that builds a fresh daemon-scoped service for every no-preset run instead of reusing the parent service, sends LingTai `prompt` (or its exact default) as the first ordinary user message, projects daemon-local runtime/token/context state through `meta_block.attach_daemon_agent_meta` without parent notifications, performs sole-call provider-independent `compact` resets, mechanically compacts at expiry while retaining the latest assistant/tool-result pair and sending explicit recovery instructions, and enforces `finish(done)` before `mark_done`. At a text-only safe send boundary it drains already-queued Shell prompt events, records their `shell_completion`/`shell_reminder` history kind, and sends only fixed poll guidance; it never auto-polls, waits for events, or revives a terminal run. All-empty responses use the shared pure predicate and the daemon-owned recovery loop: initial send plus three same-session transient retries (1/2/4 seconds), close pending calls with `tool_completed=True`, compact bounded history before retry, append localized `system.stuck_revive`, then counted AED with preserved-interface rebuilds; recovery sends do not consume `effective_max_turns`, and exhausted AED maps to detached run FAILED. No prior tool batch or successful side effect is replayed.

- `../cli_daemon.py` — external-owner CLI skin (`lingtai-agent daemon emanate|list|check|ask|wait|reclaim`, owned by `src/lingtai/ANATOMY.md`). It reuses this package unchanged: `emanate`/`ask`/`reclaim` go through `setup()` and the `DaemonFamilyDispatcher` envelope on a lease-free `_CliDaemonAgent` facade, while `list`/`check`/`wait` bind `_handle_list`/`_handle_check` read-only. `--owner-dir` (legacy `--agent-dir`) is any directory with its own `init.json`; its `daemons/` and `.notification/daemon/` are the run-state and supervisor-notification anchors, so an external same-machine caller owns its runs without a live Agent. Guarded by [D011](BEHAVIORS.md#behavior-d011).
- `daemon/system_prompt.py` — package-owned LingTai worker-prompt variant. It keeps the daemon operating contract short, names rather than re-describes host tools, teaches manual-before-first-use and precise `summary=true` use, maps parent `system.summarize` to daemon `compact`, preserves oneshot context plus the complete task, and rejects any final rendered prompt above its resolved character budget instead of truncating constraints. `DaemonManager` resolves that budget from the per-agent `<workdir>/daemon/daemon.json` `system_prompt_budget_chars` positive integer (20,000 fallback for missing, malformed, or non-positive values); a valid explicit capability value replaces the file value, an invalid explicit value retains it, and a valid `LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS` is the final manager-construction override while an invalid environment value retains that file/explicit-capability resolution. Both ordinary agent setup and `lingtai-agent daemon emanate` dispatch use that shared `setup()`/manager path. The resolved value reaches the renderer and bounded durable prompt inspection. When `shell` is selected, it adds only concise daemon-local async-event guidance (no stdout/stderr and poll for exact output). Its sibling architecture is the main-agent system-prompt graph in `src/lingtai/prompts/ANATOMY.md`; the daemon deliberately does not render that full resident section stack.

- `kernel/meta_block.py` — canonical `_meta` envelope/projector. `attach_daemon_agent_meta` carries only the instruction plus daemon-local `agent_state` onto the newest final `ToolResultBlock`; parent notification/communication state is not projected, and no main-agent resident-guidance reference is invented.
- `daemon/claude_interactive.py` — interactive Claude Code daemon backend. **Hidden / not user-selectable:** `claude` / `claude-interactive` were removed from the `get_schema()` `backend` enum and description (`daemon/__init__.py:889-904`); the code path stays only so older callers and stored daemon entries with `backend="claude"` still resolve through `_normalize_backend`/dispatch. New work uses print-mode `claude-p`. This retained PTY bridge is POSIX-only;
Windows headless composition never selects it, and native interactive support
remains deferred until ConPTY has its own accepted adapter. `ClaudeInteractiveBridge` (`daemon/claude_interactive.py:103`) accepts only the persistent Port injected by runtime composition; it never constructs a private adapter and rejects missing injection before managed-workspace, harness, or spawn work. It runs normal interactive `claude` under a PTY from a LingTai-managed workspace, writes the managed system prompt (`daemon/claude_interactive.py:80-96`), prepares empty or explicit-source detached worktrees (`daemon/claude_interactive.py:250-309`), answers terminal probes, injects `SessionStart`/`Stop` hooks via inline `--settings`, relays hook payloads through a FIFO, auto-selects workspace trust only inside the verified managed root (`daemon/claude_interactive.py:535-559`), and parses Claude transcript JSONL into daemon progress/result state.
- `daemon/shell_prompt_events.py` — narrow detached-Shell `NotificationPort` adapter. It accepts only `bash.reminder` and `bash` completion publications, discards process output/presentation fields, and asks the run directory to atomically persist bounded stable-ref prompt events; it knows no Agent, store, provider session, or parent wake.
- `daemon/run_dir.py` — per-emanation filesystem run directory. `DaemonRunDir` owns every filesystem effect for one run: folder layout, `daemon.json` atomic writes, batch `group_id` metadata (`DaemonRunDir.new_group_id()`), JSONL appends, CLI progress events, live checkpoint sequence/latest state, the bounded pending-message inbox, heartbeat touches, `result.txt`, versioned `daemon.json` data (`data_version`), visible `call_parameters`, terminal state markers, backend resume-id persistence, and the authoritative running/pending-terminal marker transitions. After initial `daemon.json` durability, `DaemonManager` appends one acceptance record through `dispatch_ledger` before it launches execution. `_persist_daemon_state()` writes only authoritative `daemon.json` plus recovery state; it deliberately creates no duplicate per-turn session record. The owning Agent Record reads the newest ledger-selected daemon.json states in a background snapshot (see `kernel/session_stats/ANATOMY.md`).
- `daemon/dispatch_ledger.py` re-exports `kernel/daemon_dispatch.py` for this owner. The kernel primitive owns the agent-scoped lock, append-only canonical order, tail/full checked diagnostics, and compact marker files. It neither writes daemon status nor backfills/repairs legacy history.
- `adapters/posix/daemon_execution_child_entrypoint.py` — fresh-interpreter execution boundary launched by the supervisor before its watcher. It registers exact execution PID/PGID/start identity and composes the existing production host; it does not duplicate backend parsers. `daemon_resume_owner_entrypoint.py` provides the bounded detached owner for one terminal supported-CLI resume generation. Durable `resume-claims/` records enforce one writer, and `followups/` plus `daemon.json` expose follow-up truth to `daemon(check)`.
- `adapters/posix/process_identity.py` — shared POSIX process-incarnation helper. Linux identities combine boot ID and `/proc/<pid>/stat` start ticks; Darwin/BSD identities use bounded `ps` start time plus PPID. It returns `None` when observation is unavailable, and all ownership-sensitive signal paths refuse unknown or mismatched identities.
- `daemon/runtime.py` — stateless daemon backend runtime primitives shared by the LingTai in-process loop and transitional CLI runners. Port-owned Codex, Cursor, OpenCode-family, Qwen, and Kimi runners use the daemon-local process Port for stderr draining and stdout iteration; only ask workers use a local deadline, while initial streams remain watchdog-owned. The historical private names remain for unmigrated paths and compatibility tests.
- `daemon/CONTRACT.md` — maintained daemon contract for the public tool surface, selected skills catalog/path semantics, MCP registration redaction/native mounting, `daemon_common` checkpoint/inbox delivery and completion enforcement, backend implementation status, run artifacts, review triggers, and the acceptance gate for new backend or contract-impacting changes.
- `daemon/settings.py` — read-only owner provider for the daemon family's
  progressive-disclosure `settings` action. It projects the active manager's
  four effective settings as exact five-field `SettingRow` values and exposes
  no mutation path.

- `daemon/interactive_terminal/` — capability-local immutable command/exit values and the
  raw byte-stream `InteractiveTerminalPort`; its separate Contract/Anatomy
  governs interactive children without widening the headless process Port.
- `adapters/posix/interactive_terminal.py` — the persistent POSIX PTY adapter
  injected by `setup()`; manager group/all shutdown and watchdog sweeps own its
  opaque handles, including any handle still live after bridge final release.
  Detached execution composition selects its inherited-group mode and binds the
  same immutable child observation/scope callback before interactive Claude
  injection. Interactive per-handle Stop/timeout/error/cancel never signals the
  inherited group; callback failure reaps the child and closes its PTY master
  before re-raising.

- `daemon/process_port.py` — daemon-local technology-neutral process Port and
  immutable command, opaque handle, and raw exit-receipt value objects. It owns
  no backend parsing, run-directory state, notification, or timeout policy.
- `daemon/posix_process.py` — the POSIX adapter selected by `setup()`. It owns
  `Popen(..., start_new_session=True)` for ordinary manager runs, with a
  detached-composition `start_new_session=False` mode that inherits the
  execution child's group. It owns pipe readers, process-group TERM/KILL
  escalation, bounded wait/reap, Port ownership registration, and the
  immutable post-spawn identity/scope observation callback used to publish
  durable detached child state before stream I/O. Ordinary Ports own private
  process groups; inherited detached Ports signal only their exact child while
  supervisor exact-run reclaim owns the inherited group. Observation callback
  failure is transactional: exact-child reap, registry removal, and callback
  descriptor cleanup happen before the original exception is re-raised.
- `daemon/windows_process.py` — the Windows adapter selected by `setup()` on
  `nt`. It maps `PRIVATE_PROCESS_GROUP` to one Job Object per spawn
  (suspended spawn → assignment → `NtResumeProcess`, no `KILL_ON_JOB_CLOSE`)
  and `INHERITED_SUPERVISOR_GROUP` to exact-child-only termination through the
  retained handle; termination is forceful-only with the same bounded
  wait/reap, first-writer-wins reason receipts, and transactional
  observation-callback failure semantics as the POSIX sibling. Identity comes
  from the shared `adapters/windows/_win32` creation-time surface.

## Public API

`daemon` is one model-facing tool carrying the LTP v2 action-separated envelope
(`action`, `input`, required `reasoning`, optional `summarize`) composed by
`_tool_family.py` from seven internal `ChildTool`s. The children consume no extra
model tool slot. Each action's own strict `input` fields are listed in
`CONTRACT.md` §Tool Surface; `list`/`check`/`settings`/`manual` are read-only and
`emanate`/`ask`/`reclaim` are the side-effectful three.

The `daemon` tool exposes seven actions:

| Action     | Description |
|------------|-------------|
| `emanate`  | Spawn one or more subagents with specified task + tools + optional preset |
| `list`     | List running/completed/failed emanations with status and elapsed time |
| `ask`      | Send a follow-up message to a running emanation |
| `check`    | Read-only progress tail: `daemon.json` state + last N events from `events.jsonl` + a compact `artifacts` block (the run's artifact manifest — relative path/size/mtime/role per important file, plus run-level state/result_path/error_path). On in-memory registry miss (e.g. after refresh/molt) falls back to the durable `daemons/*/` run dirs, resolving by full `run_id` (exact) or short `handle` (most-recent, with ambiguity flagged) |
| `settings` | Return the read-only five-field daemon settings inventory. The provider must resolve every current manager value or the complete action fails; exact meanings and owner change procedures are in the manual sections named by each row's `comment` |
| `reclaim`  | Cancel all running emanations, shut down CLI process groups/thread pools through the same runtime-shutdown helper used by agent stop, reset ID counter |
| `manual`   | Return the installed `daemon-manual` skill via the shared reserved `tool_family.manual.build_manual_child(agent, "daemon")` child: canonical `content[0].text` body + `structuredContent.manual_path`, returned verbatim with no double wrap. Reaches no `DaemonManager` method, so it performs no daemon operation |

Every LingTai worker also receives the intrinsic `compact` tool. Its `action`
is required: explicit `action="manual"` is read-only, explicit `action="run"`
with `_reason` is a repeatable non-terminal sole-call reset, and omission is
refused. The surviving successful reset result is stamped from the fresh retained
context. The final ToolResultBlock of each daemon tool batch carries daemon-local
`_meta.agent_meta`; the parent notification/communication
axis is not inherited.

## Internal Module Layout

```
daemon/__init__.py
  ├── DaemonManager.__init__        — stores agent ref, config ceilings, emanation registry
  ├── handle()                      — internal legacy flat dispatcher (emanate/list/ask/check/reclaim/manual); NOT the registered model-facing handler since the ToolFamily migration
  ├── _daemon_intrinsic_surface()   — synthesizes only the automatic LingTai `compact` schema; no intrinsic (including `email`) is wired onto a daemon
  ├── _build_tool_surface()         — auto-includes `compact`, filters other requested tools against blacklist, expands groups, and merges preset/MCP surfaces; tolerates `email` as "available" pre-connection via `_DAEMON_AUTO_MCP_TOOL_NAMES` since `_with_daemon_email_mcp` mounts it as task-scoped MCP before the real connection happens; preset emanations also keep the NARROW parent host floor (`_parent_host_tool_floor()`: shell + file primitives only) so saved presets that omit core caps don't make requested host tools unknown — optional/provider parent tools (vision/web_search) are NOT borrowed and stay unknown unless the preset supplies them
  ├── _instantiate_preset_capabilities() — sets up preset tool surface in a sandbox
  ├── _build_emanation_prompt()     — delegates to `system_prompt.py` for the bounded operating contract, compact host-tool names, complete task, and selected oneshot context
  ├── _run_emanation()              — LingTai worker loop: sends `prompt`/default first, runs ToolExecutor/ToolCallGuard, and performs same-run sole-call compact resets while retaining the exact call/result pair
  ├── _run_claude_interactive_emanation() — `claude` / `claude-interactive` backend; delegates to `run_claude_interactive()` (`daemon/claude_interactive.py:771`) to create the managed workspace, drive the interactive Claude TUI through PTY + hooks + transcript parsing, and persist managed-workspace state.
  ├── _run_claude_code_emanation()  — `claude-p` / compatibility `claude-code` backend; parses `--output-format stream-json --verbose` print-mode events in real time so `claude_session_id`, per-turn text, and tool_use/tool_result land in DaemonRunDir during the run (vs. post-hoc). Claude Code's own `usage` fields are deliberately NOT forwarded to append_tokens (external billing path; semantics don't match the kernel's adapter accounting); `_normalize_claude_usage` requires the primary `input_tokens` and `output_tokens` fields, defaults the optional cache-read/cache-creation fields to zero, and rejects bool/negative/malformed consumed values (matching Codex/Cursor). The first valid terminal `result` event's usage is buffered — not persisted — until after cancellation/timeout, exit code, `is_error`, and `_require_done_completion` (the daemon_common `finish()` MCP contract — itself a terminal acceptance gate: when the run loaded `daemon_common`, a missing/bad `finish()` call fails the run here, before any usage is persisted or `mark_done` runs) are all classified/passed, then persisted exactly once UI-only to `daemon.json.cli_tokens` via `run_dir.record_cli_tokens` (`cached = cache_read + cache_creation` input tokens). Cancellation/timeout observed before the final acceptance check, a later failure classification, or a missing/bad completion sentinel arriving after the terminal line therefore cannot leave false accepted usage; cancellation published after that acceptance point retains the existing best-effort terminal semantics rather than introducing cross-backend atomic arbitration in this path. `_run_ask_claude_code_stream` (the `daemon(ask)` resume follow-up) applies the same buffer-then-classify ordering against its deadline/exit-code/`is_error` gates — resume has no `_require_done_completion` gate — so the TUI `/daemons` view can show accepted usage without touching either token ledger.
  ├── _run_codex_emanation()        — codex backend; parses `codex exec --json` JSONL events (thread.started → codex_session_id, item.completed → agent_message text, turn.completed → terminal). Symmetric with the claude-code backend. A valid terminal `usage` object is normalized to disjoint input (`input_tokens - cached_input_tokens`, clamped at zero), cached input, and output in UI-only `daemon.json.cli_tokens`; the raw object is retained in a `cli_usage` event, with no token-ledger row and at most one terminal account per stream. Codex receives native config overrides for `daemon_common` plus parent-provided stdio MCP registrations, and `finish(done)` is required before success when daemon_common is loaded.
  ├── _run_opencode_emanation()     — Port-owned OpenCode-family runner; builds an immutable grouped command `<executable> <cmd_prefix...> <prompt>` (default prefix `run --format json`) and parses one JSON event per stdout line via defensive helpers (`_opencode_extract_session_id`, `_opencode_extract_text`) because OpenCode-family event field naming is less standardized than claude-code or codex. Session id is stored as `opencode_session_id` (or a caller-supplied family key) in daemon.json on the first event that carries one; terminal-shaped events (`*.completed`, `*.done`, `*.finished`, `result`, `final`) override intermediate streaming text. Non-JSON lines are still recorded as cli_output. `_build_opencode_prompt` wraps the user task with the daemon operating contract (write detailed work product to files; end with a concise final answer). The `executable` / `backend_name` / `session_state_key` / `cmd_prefix` keyword args let MiMo Code and Oh-My-Pi reuse this runner without duplicating the parse loop; the optional `text_extractor` / `error_detector` / `usage_recorder` hooks let a family member override answer extraction, recognize a structured error event that must fail the run even on exit 0, or persist source-specific UI usage (MiMo Code passes all three). The OpenCode backend receives per-run `OPENCODE_CONFIG_CONTENT` for `daemon_common` and parent-provided stdio MCP registrations; MiMo and Oh-My-Pi reuse the parser but do not yet have evidence-backed MCP config injection.
  ├── _run_mimocode_emanation()     — MiMo Code backend; reuses the OpenCode-family runner around `mimo run --format json <prompt>` (session id stored as `mimocode_session_id`) but injects MiMo's verified 0.1.5 JSONL contract via the runner's `text_extractor` / `error_detector` / `usage_recorder` hooks: `_mimocode_extract_answer_text` surfaces ONLY a `type:text` event's nested `part.text` (reasoning/tool/step `part.text` is ignored), `_mimocode_extract_error` turns a structured `type:error` event into a terminal failure even on exit 0 with a bounded (≤500) `redact_text`-scrubbed detail, and `_mimocode_normalize_usage` accepts only source-shaped `step_finish` parts and records UI-only totals with duplicate `part.id` suppression. Follow-up asks use `_handle_ask_mimocode()` → `mimo run --session <mimocode_session_id> --format json <message>`, applying the same answer/error/usage contract to the resume stream.
  ├── _run_qwen_code_emanation()    — Qwen Code backend; submits an immutable grouped `DaemonProcessCommand` (`qwen --yolo <backend_argv> -p <prompt>`) to the daemon-local Port with no local stdout deadline. `QWEN_CODE_SYSTEM_SETTINGS_PATH` points at a per-run settings file containing `mcpServers.daemon_common` plus parent-provided stdio MCP registrations; Manager records stdout/stderr as cli_output/result text, and intentionally rejects `daemon(action="ask")` until the Qwen CLI exposes a stable resume contract.
  ├── _run_oh_my_pi_emanation()     — Oh-My-Pi (`omp`) backend; OpenCode-family wrapper with `cmd_prefix=["--mode", "json", "--approval-mode", "yolo"]` → spawns `omp --mode json --approval-mode yolo <prompt>`. The OpenCode JSON parser already recognizes Oh-My-Pi's `type:session` header (bare top-level `id`), stored as `oh_my_pi_session_id`. Follow-up asks use `_handle_ask_oh_my_pi()` → `_handle_ask_opencode(..., build_resume_cmd=_oh_my_pi_resume_cmd)` → `omp --mode json --approval-mode yolo --session <oh_my_pi_session_id> <message>`.
  ├── _run_kimicode_emanation()     — Kimi Code (`kimi`) backend; submits `kimi <backend_argv> --prompt <prompt> --output-format text` (owned `--prompt`/`--output-format` follow the free-form backend_argv; `--yolo` forbidden because the CLI rejects it alongside `--prompt`) to the grouped daemon-local Port with no local stdout deadline. Sets a per-run env overlay via `_kimicode_run_env` (`daemon/__init__.py:5205-5252`: run-private `KIMI_CODE_HOME`, telemetry/auto-update off, `KIMI_MODEL_API_KEY` mapped from `KIMICODE_API_KEY`/`KIMI_API_KEY`/`MOONSHOT_API_KEY` only when unset, provider/base-url/model/context defaults only when absent; secrets never logged). For MCP-capable runs, `_handle_emanate_cli` writes `<run>/kimi-code-home/mcp.json` and records only its path in `backend_harness_files` (`daemon/__init__.py:3479-3483`). Records stdout/stderr as cli_output/result text and intentionally rejects `daemon(action="ask")` because no stable session-id/resume contract was verified.
  ├── _run_deepseek_emanation()     — DeepSeek Harness (`deepseek`) backend; submits `dsh <backend_argv> --profile headless <prompt>` (harness-owned `--profile headless` launcher flags lock the shipped one-shot headless profile; `--patch` overlays stay available through free-form backend_argv) to the grouped daemon-local Port with no local stdout deadline. Sets a per-run env overlay via `_deepseek_run_env` (run-private `DSH_HOME` at `<run>/dsh-home` so first-use profile auto-initialization never touches the operator's real home, `DSH_TELEMETRY_DISABLED=1` hard opt-out; secrets never logged). Records stdout/stderr as cli_output/result text, treats exit 0 as completed and any nonzero exit as failed, and intentionally rejects `daemon(action="ask")` because no stable session-id/resume contract was verified for the headless profile. `deepseek` is not in `_cli_backend_loads_common_mcp`: `daemon_common` injection is not wired (no evidence-backed dsh config path in this slice).
  ├── _run_cursor_emanation()       — Cursor Agent CLI backend; spawns `agent -p --force --output-format stream-json <prompt>` (Cursor's headless print mode with file edits enabled) and parses the version-pinned 2026.05.28-a70ca7c `result.usage` contract into UI-only `cli_tokens`, preserving raw usage and joining model only from a preceding matching `system/init` by `session_id`; it also captures `cursor_session_id` and final `result` text. Ask follow-ups resume with `agent -p --force --resume <cursor_session_id> --output-format stream-json <message>` and use the same usage path.
  ├── _find_claude_session_id()     — legacy `~/.claude/projects/` JSONL scan; now only a fallback when the stream-json `session_id` capture fails
  ├── _handle_emanate()             — validates presets, creates DaemonRunDir, launches one detached supervisor per run
  ├── _handle_list/check/reclaim    — individual action handlers; list scans active entries plus historical run-dir JSON records and performs best-effort lazy daemon.json rebuilds. `_handle_check` tries the in-memory registry first, then falls back to `_resolve_historical_run_dir` so the compact id from a terminal notification still resolves after refresh/molt
  ├── _check_snapshot_from_paths()  — builds the `check` response (daemon.json fields + truncated event tail + `artifacts` block) from a run dir's paths; shared by the live-registry hit and the historical fallback so both surface identical shape
  ├── _artifacts_summary()          — compact artifact-manifest block for `check`: prefers the persisted `artifacts.json` (written at terminal time; `source="manifest"`), else computes a fallback via `DaemonRunDir.build_manifest` for a still-running or pre-manifest run (`source="fallback"`); on any failure returns `source="unavailable"` rather than breaking `check`. Surfaces path/size/mtime/role metadata only — never file contents; artifact entries are run-dir-relative while run-level `result_path`/`error_path` keep the existing absolute-path `check` convention
  ├── _resolve_historical_run_dir() — resolves a compact `run_id` to a durable `daemons/<run_id>/` folder; exact folder-name match wins, while legacy short `handle` lookup is accepted only for a unique match and multi-match ambiguity is rejected without returning every path
  ├── _run_dir_handle()             — best-effort handle for a run dir (daemon.json `handle`, falling back to parsing the folder name) used by historical resolution
  ├── _handle_ask()                 — every entry is created with `"detached": True` (see `_handle_emanate`/`_handle_emanate_cli`/`_durable_detached_entry`), so `_handle_ask` always routes to `_handle_ask_detached`, which submits the follow-up through the run-local control spool owned by the run's detached supervisor
  ├── _handle_ask_cli()             — claude-code follow-up via `claude --resume <claude_session_id>`. Spawns the subprocess, hands the stream-json parse to `_ask_pool`, returns `{"status":"sent","async":true}` immediately so the parent's tool turn isn't held for the duration of the follow-up
  ├── _run_ask_claude_code_stream() — background worker for the claude-code ask. Same stream-json parse as `_run_claude_code_emanation`; clears `ask_in_flight` on exit
  ├── _handle_ask_codex()           — codex follow-up via `codex exec resume <codex_session_id> --json`. Symmetric with `_handle_ask_cli`: spawns + dispatches to `_ask_pool`, returns immediately
  ├── _run_ask_codex_stream()       — background worker for the codex ask. Same JSONL parse as `_run_codex_emanation`; `daemon(check)` therefore sees progress on follow-ups too
  ├── _handle_ask_opencode()        — OpenCode-family follow-up via `opencode run --session <opencode_session_id> --format json <message>` by default; callers such as oh-my-pi can pass `build_resume_cmd` to customize the resume argv (`omp --mode json --approval-mode yolo --session <oh_my_pi_session_id> <message>`). Symmetric with claude-code / codex ask: spawns, dispatches to `_ask_pool`, returns immediately. Returns a clear error if the backend-specific session id has not been captured yet.
  ├── _run_ask_opencode_stream()    — background worker for the opencode ask. Same defensive JSON-line parse as `_run_opencode_emanation`; clears `ask_in_flight` on exit; terminal-shaped events override intermediate text
  ├── _register_cli_proc()/_unregister_cli_proc()/_kill_cli_group()/_drain_all_cli_procs() — legacy direct-Popen CLI tracking predating the detached supervisor. `_register_cli_proc` (the only writer into `_cli_procs`/`_cli_proc_groups`) has no production caller left — every CLI backend now runs under its own detached supervisor, which owns exact-child ownership and deadline enforcement directly (`kernel/daemon_supervisor/CONTRACT.md`). `_drain_all_cli_procs` remains reachable from the live `_shutdown_runtime_resources` funnel (`_handle_reclaim`, agent stop, refresh) and always drains an empty list in production; it is kept as defensive teardown for any future non-detached direct-Popen caller, not removed as dead code.
  └── _publish_daemon_notification() — publishes one compact event to the typed Store's `.notification/daemon/<daemon-id>.json` mini-channel (id, terminal status, task summary, run dir, result/error path, bounded preview) and stamps the channel's `data.daemon` batch state; terminal callers pass a stable idempotency key. Every event is typed by `kind` (`daemon/__init__.py:4845`), which defaults to `daemon_terminal`; a follow-up (`ask`) result reuses this same publisher with `kind="daemon_followup"` (`daemon/__init__.py:4954`), and the detached supervisor's equivalent takes the same argument (`daemon/supervisor_runtime.py:496`), so the parent's summary never reads a still-running run as terminal (`_is_terminal_daemon_event`, `kernel/base_agent/__init__.py:99`). The Store routes the mutation with the daemon-only owner/run hot path, records batch state through durable tombstone control, and does not rebuild the root report on append. The root `.notification/daemon.json` is only a derived run/state report and never enters the aggregate projection. Called directly by parent-process code paths (e.g. `_publish_followup_if_live`) that still run in this process; the detached supervisor's own terminal path uses its own equivalent in `supervisor_runtime.py`, not this method (`daemon/__init__.py:3762`)

daemon/run_dir.py
  ├── DaemonRunDir.__init__         — creates folder on disk, writes versioned daemon.json (data_version + call_parameters) + .prompt
  ├── Path properties               — run_id, handle, group_id, path, daemon_json_path, prompt_path, heartbeat_path, chat_path, events_path, token_ledger_path, result_path, manifest_path (`artifacts.json`)
  ├── build_manifest() / write_manifest() — `build_manifest(run_path)` is a classmethod pure read over a run dir: lists path/size/mtime/inferred-role for well-known artifacts (priority order) + extra work-product files (sorted, `.tmp` skipped), capped at `_MANIFEST_MAX_ENTRIES` (records `artifacts_total`/`truncated`), plus run-level `state`/`result_path`/`error_path` (the last set only for failed/timeout/cancelled runs with a result.txt). Used both by `write_manifest()` (instance, called from every terminal marker after daemon.json/result.txt are final, best-effort via `_safe`, persists `artifacts.json`) and by `_artifacts_summary`'s fallback for old/running runs that have no `artifacts.json`. `MANIFEST_VERSION` mirrors `data_version`
  ├── record_user_send()            — appends user-role entry to chat_history.jsonl
  ├── bump_turn()                   — marks end of LLM round (daemon.json + chat_history + heartbeat)
  ├── set_current_tool()            — marks tool dispatch starting (daemon.json + events + heartbeat)
  ├── clear_current_tool()          — marks tool dispatch finished
  ├── record_cli_output()           — records CLI backend stdout/stderr as cli_output events
  ├── append_tokens()               — dual-ledger token accounting (daemon's + parent's); `_accum` passes `UsageMetadata.extra`, then `RunDir` sanitizes it once through `safe_codex_pool_usage_extra` and mirrors the same five-field safe codex-pool attribution subset into both rows while dropping arbitrary extras. Both rows remain tagged `source="daemon"` + `em_id` + `run_id` so every row self-describes regardless of which ledger it lives in (`sum_token_ledger(scope="main_agent")` therefore excludes all rows of a daemon-local ledger; use `scope="all"` to total one). Standard ledger appends may also best-effort mirror to each ledger's sibling `log.sqlite.token_entries` sidecar; JSONL remains authoritative.
  ├── record_cli_tokens()           — accumulates external CLI usage into `daemon.json.cli_tokens` (`input/output/cached/thinking/calls`) for UI display only; never writes either token ledger; appends a `cli_usage` event (with raw usage) to `events.jsonl`
  ├── enqueue_checkpoint_message()   — appends one bounded/redacted ID-bearing parent correction only while the run is live; old run dirs lazily acquire the queue
  ├── record_checkpoint()            — under the RunDir state transaction increments sequence, stores latest, drains queued messages exactly once, appends `daemon_checkpoint`, and touches heartbeat without changing terminal fields
  ├── mark_done/failed/cancelled/timeout — terminal state markers (result.txt + preview on done); each sets `daemon.json.state` to the authoritative terminal status read back by the detached supervisor's own terminal-notification path (`supervisor_runtime.py`), then calls `write_manifest()` to persist `artifacts.json` capturing the final artifact set
  ├── claim_terminal_notification() / clear_terminal_notification_claim() / mark_terminal_notification_published() — terminal notification state machine: pending claim blocks concurrent callbacks, failed enqueue clears the claim for retry, and only the post-publication receipt sets `daemon.json.terminal_notified=true` (`daemon/run_dir.py:887-937`); `mark_terminal_notification_published_on_disk()` persists the same receipt for restart reconciliation (`daemon/run_dir.py:940-962`)
  ├── set_session_id()              — persists a CLI backend resume id (`claude_session_id`/`codex_session_id`/`opencode_session_id`/`cursor_session_id`) to daemon.json; no-op (returns False) on empty or unchanged value, `overwrite=False` keeps the first id (OpenCode family); write failures propagate so the run fails
  └── _atomic_write_json()          — tempfile + os.replace for crash-safe writes

daemon/runtime.py
  ├── kill_process_group()          — SIGTERM→(wait 5s)→SIGKILL of the subprocess's own process group (`start_new_session=True` ⇒ pgid==pid); swallows ProcessLookupError/OSError
  ├── iter_stdout_with_deadline()   — queue-backed stdout generator: blocking `for line in proc.stdout` runs on a daemon reader thread while the caller pulls with a deadline, so a silent CLI can't outlast the watchdog
  ├── mark_cancelled_or_timeout()   — marks `timeout` when the timeout_event is set else `cancelled`, returns the `"[cancelled]"` sentinel (timeout_event may be None ⇒ cancelled)
  └── spawn_stderr_drainer()/StderrDrain — background stderr drain mirroring non-blank lines to `record_cli_output(stream="stderr")`; `StderrDrain.join()`/`.tail(n=20)` for failure-message tails. Re-exported into `daemon/__init__.py` as `_kill_process_group`/`_iter_stdout_with_deadline`/`_mark_cancelled_or_timeout`/`_spawn_stderr_drainer` so existing monkeypatches keep working
```

## Key Invariants

- **No recursion:** `EMANATION_BLACKLIST` prevents emanations from spawning sub-emanations, avatars, psyche, the skill catalog, or codex-style recursive agent execution.
- **Tool surface isolation:** `_ToolCollector` ensures preset/detached capability setup does not mutate the parent agent's tool registry; ordinary and registrar-issued official mounts, claims, and canonical bound-result checks all remain collector-local.
- **Filesystem isolation:** Each manager-created emanation gets one compact-id run directory such as `daemons/em-a1b2/` or `daemons/em-a1b2-1/` on collision; legacy direct `DaemonRunDir` callers may still create the older timestamp/hash folder form. `DaemonRunDir` uses atomic `os.replace` for `daemon.json` and single-writer append-only JSONL for events/chat history.
- **Startup recovery is marker-only:** `DaemonManager.__init__` reads only new-format running and pending-terminal marker files, then applies existing stale-parent/receipt logic to those named runs. It never scans legacy run folders, and absent legacy markers are an explicit cutover limit. Markers are removed only after their authoritative daemon.json or receipt transition is durable. Terminal retry still uses `daemon-terminal:<run_id>` as its idempotency key.
- **Timeout vs. cancel distinction:** Separate `timeout_event` and `cancel_event` allow the run loop to call `mark_timeout()` vs. `mark_cancelled()` based on which signal fired first.
- **Timeout kills are per-run, reclaim is global:** every backend runs under its own detached supervisor process, whose `_control_and_deadline_watcher` (`daemon/supervisor_runtime.py`) enforces that run's own deadline and terminates only its own exact execution/CLI child group — there is no shared parent-process batch watchdog left to scope, since each run already has its own isolated process. Explicit `daemon(action="reclaim")` and agent-stop still sweep everything the parent process itself owns via `_shutdown_runtime_resources`: the legacy `_cli_procs`/`_cli_proc_groups` index (populated only by unmigrated direct-Popen callers, currently none in production), the process Port's own tracked children, and the interactive-terminal Port.
- **Capacity control:** `manager_pool_size` caps concurrent central-manager execution workers; every POSIX batch routes through the manager by default.
- **Central-manager refresh boundary:** `adapters/posix/daemon_manager.py` persists its loaded-code head, resolved source root, and per-run notification-protocol identity with the PID/start token. A new parent may reuse only an exact match; a missing or mismatched stamp fails before queue/capsule handoff, rather than letting newly dispatched work execute in a stale imported process. The manager can pair a capsule with one in-memory descriptor received through `daemon_capsule.py`; replacement, cancellation, quarantine, failed ACK, pre-execution failure, and manager exit close the current owner. This is intentionally limited to the reusable POSIX central manager: it neither restarts nor takes over detached one-run supervisors or their active/capsule-backed work, and manager restart cannot recover the ephemeral descriptor.
- **Preset authorization and validation are pre-flight:** For LingTai-backend batches, an unauthorized explicit `tasks[].preset` refuses the whole batch before preset loading, connectivity/capability checks, run-directory creation, scheduling, or dispatch. Authorized preset connectivity and capability instantiation are then checked before any emanation is scheduled. A single failure refuses the whole batch; omitted-preset and external-CLI paths do not use this authorization gate.
- **Dual token ledger (lingtai backend only):** For lingtai-backend emanations, token usage is written to both the daemon's own ledger and the parent's ledger with `source=daemon` attribution; safe codex-pool source/size/weight/model-scope telemetry from `UsageMetadata.extra` is projected once and copied to both rows, while all other extras are omitted. The token-ledger SQLite sidecar mirrors these JSONL rows but does not replace them. **CLI backends (claude, claude-p/claude-code, codex, opencode, cursor) deliberately do NOT write to either ledger** — they run as external processes with their own billing paths, and their cache-creation/cache-read semantics do not map cleanly onto the kernel's adapter accounting. Mixing them in would produce a misleading "lifetime totals" number. CLI-backend spend is visible to the agent through `daemon(check)` output (`last_output`, `cli_output` events, stderr), not through `sum_token_ledger`. **As UI-only exceptions, claude-p/claude-code persists the Claude Code stream-json `result` usage; Codex persists one `turn.completed` usage object per stream; MiMo Code records accepted source-reported `step_finish` usage into `daemon.json.cli_tokens` via `record_cli_tokens` (initial runs and `ask` follow-ups); and Cursor persists its source-reported stream-json `result` usage into `daemon.json.cli_tokens` via `record_cli_tokens` (initial runs and `ask` follow-ups)** so the TUI `/daemons` view can display them. Claude and Codex initial/follow-up usage is likewise recorded through `record_cli_tokens`; Codex disjoins `input_tokens` by subtracting `cached_input_tokens` and retains the raw source object in `cli_usage`; MiMo accepts only pinned 0.1.5 `step_finish` parts with duplicate `part.id` suppression and retains raw source counters; Cursor keeps its direct net `inputTokens`, sums `cacheReadTokens` + `cacheWriteTokens` into `cached`, retains the raw source object in `cli_usage`, and joins model only from a matching source `system/init` event while provider remains unknown. These fields are separate from `tokens`, are never written to either `token_ledger.jsonl`, and never feed `sum_token_ledger`.
- **CLI progress stays inspectable, not conversational:** Claude/Codex/OpenCode/MiMo Code/Qwen Code/Oh-My-Pi/Kimi Code/Cursor stdout or parsed transcript output is persisted as `cli_output` events plus `daemon.json.last_output`; completion/failure publishes a bounded `daemon`-channel notification pointing the parent to `daemon(action="check", id=...)`.
- **Cooperative checkpoints are nonterminal and bounded:** LingTai `ask` retains its control spool. While a detached external CLI run is active, only the exact backends whose launch path mounts `daemon_common` (`claude-p`/`claude-code`, Codex, OpenCode, Qwen, and Kimi) queue one ID-bound correction for the next model-chosen checkpoint; unsupported active backends retain `busy`, and every terminal resume path remains unchanged. Each checkpoint publishes a stable-key nonterminal event on the singular built-in `daemon` channel (sharing terminal batch state) and appears in `daemon(check)` as `latest_checkpoint` plus a pending-message count; it is not streaming chat, stdin injection, preemption, or a terminal receipt.
- **Full results live on disk:** `mark_done()` writes complete terminal output to `result.txt`; `daemon.json.result_preview` and notification bodies stay bounded. For MCP-capable backends, `daemon_common.checkpoint` updates live RunDir truth while the internal `daemon_completion.json` sentinel is still written only by `daemon_common.finish` and validated before success.
- **List is a ledger projection, not a second truth:** `daemon(action="list")` overlays active in-memory runs on the agent-local append-only dispatch ledger and reads the referenced authoritative `daemon.json` files. Omitted/null `last` uses the newest 1000 ledger records; explicit filters/search/history may stream more only because the user requested that ad hoc work. The reader validates exactly the checked range and returns bounded advisory `warnings` for empty/invalid/non-monotonic/duplicate ledger rows or missing/corrupt run state. It never sorts or rewrites ledger order, backfills legacy runs, repairs daemon.json, or falls back to a lifetime directory scan. Exact-id `check` remains the on-demand legacy inspection path.
- **Check survives refresh/molt via durable run dirs:** After a refresh/molt the parent gets a fresh `DaemonManager` whose `_emanations` registry is empty (`__init__` does not reconstruct registry entries from disk), but daemon terminal notifications still point at valid `daemons/<run_id>/result.txt` paths. `_handle_check` therefore falls back, on a registry miss, to `_resolve_historical_run_dir`: a new compact id exact-matches its folder name and resolves unambiguously with `source="history"`; a legacy short `handle` resolves only when it has a unique historical match. If several legacy run dirs share the same handle, check returns an ambiguity error with `match_count`/`latest_run_id` rather than injecting an unbounded `other_run_dirs` list. Live registry hits keep using the in-memory `DaemonRunDir` and are never flagged `history`. An id that matches neither memory nor disk still returns `Unknown emanation`.
- **Claude Code spawns get a sanitized env.** All Claude backend entry points (`_run_claude_interactive_emanation`, `_handle_ask_claude_interactive`, `_run_claude_code_emanation`, and `_handle_ask_cli`) build the subprocess env via `_claude_code_env()`, which copies `os.environ` and pops `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `CLAUDE_CODE_OAUTH_TOKEN`. LingTai loads `.env` from `~/.lingtai-tui/` early in startup, so an API key intended for the lingtai LLM adapter would otherwise leak into spawned `claude` processes and force them off the user's Claude Code subscription onto API billing — surfacing as `Credit balance is too low` even when the subscription is healthy. Print-mode stripped vars are logged once per spawn via `daemon_claude_code_env_stripped`; interactive Claude uses the same sanitized env while avoiding global `~/.claude*` writes. Codex spawns are unaffected (they use OpenAI creds). See GH #107.
- **`backend_options` is a CLI-backend-only argv passthrough.** Per-task `backend_options` (JSON object) is converted to argv tokens by `_backend_options_to_argv` (`daemon/__init__.py:373-428`) and appended to the CLI command before the task prompt by `_run_claude_interactive_emanation`, `_run_claude_code_emanation`, `_run_codex_emanation`, `_run_opencode_emanation`, `_run_mimocode_emanation`, `_run_qwen_code_emanation`, `_run_oh_my_pi_emanation`, `_run_kimicode_emanation`, and `_run_cursor_emanation`. Validation happens pre-flight in `_handle_emanate_cli` — a single bad spec refuses the whole batch with a clear `ValueError`. The resolved object + argv are persisted to `daemon.json` (`backend_options`, `backend_argv`) and logged as `daemon_backend_options`. The lingtai backend silently ignores the field. `daemon(action="ask")` does not re-pass options — `--resume` / `exec resume` / `--session` reuses the session as-is (Qwen Code currently rejects ask because no stable resume contract is wired). Harness-owned flags such as Claude's `--settings` / `--print` / `--output-format`, OpenCode-family `--format`, MiMo Code's additional session selectors (`--session`/`-s`, `--continue`/`-c`, `--fork`), Qwen Code prompt/approval flags, Oh-My-Pi `--mode` / `--approval-mode yolo` / session flags, and Kimi Code `--prompt` / `--output-format` / `--yolo` / session-`--continue` flags are rejected in `backend_options` before spawn by `_validate_claude_backend_argv` (`daemon/__init__.py:716-739`). Interactive Claude additionally consumes the LingTai-owned `--managed-worktree-from` option (`backend_options.managed_worktree_from`) before invoking Claude; it is not forwarded to the CLI.
- **Ask workers cannot block on silent stdout.** Port-owned Claude print-mode, Codex, Cursor, and OpenCode-family ask workers pass an explicit monotonic deadline to `iter_stdout`; the Port enforces it and the worker terminates with `reason="timeout"`, clears `ask_in_flight`, and releases the handle. Interactive Claude PTY remains outside this headless boundary. Initial Port-owned streams have no local deadline because their batch watchdog remains authoritative.
- **CLI-backend `ask` is non-blocking; lingtai-backend `ask` is in-process.** `_handle_ask_claude_interactive` / `_handle_ask_cli` / `_handle_ask_codex` spawn the resumed CLI subprocess on the calling thread (so subprocess-launch errors like missing CLI surface synchronously) but hand the stream-json/JSONL parse loop to a dedicated `ThreadPoolExecutor` (`_ask_pool`, sized to `manager_pool_size`). The agent's `daemon(action="ask")` call returns `{"status":"sent","async":true}` within milliseconds; progress lands as `cli_output` events + `last_output` in the run_dir, and the final reply (or failure) is announced via `_publish_daemon_notification("follow-up completed"/"follow-up failed", kind="daemon_followup")`, so the run stays active in the parent's bounded daemon summary instead of being retired as terminal. The lingtai-backend path is unchanged — it buffers into the emanation's `followup_buffer` and is drained by the in-process run loop. A per-entry `ask_in_flight` flag (guarded by `followup_lock`) refuses a second concurrent ask with `{"status":"busy", ...}` because interactive `claude --resume`, print-mode `claude --resume`, and `codex exec resume` serialize per session and a second spawn would either error or interleave reply text. `_handle_reclaim` shuts down `_ask_pool` alongside the regular emanation pools and rebuilds a fresh one. This fixes the regression where a single `daemon(ask)` could hold the parent agent's tool turn for up to `self._timeout` seconds (default 3600). Parent agent stop/refresh uses the same cleanup path via `shutdown_for_agent_stop` / `_shutdown_runtime_resources` (`daemon/__init__.py:6391-6475`) before heartbeat/lock release, waiting on both primary daemon futures and CLI `ask_future` follow-up workers, so daemon executor workers and CLI child process groups cannot keep the old agent process alive after liveness is withdrawn.
- **CLI backends stream structured events where the CLI supports them, not buffered text.** `_run_claude_interactive_emanation` / `_handle_ask_claude_interactive` use a managed workspace + PTY + `SessionStart`/`Stop` hooks + transcript JSONL; `_run_claude_code_emanation` / `_handle_ask_cli` use `claude --output-format stream-json --verbose`; `_run_codex_emanation` / `_handle_ask_codex` use `codex exec --json`; `_run_opencode_emanation` / `_handle_ask_opencode`, `_run_mimocode_emanation` / `_handle_ask_mimocode`, and `_run_oh_my_pi_emanation` / `_handle_ask_oh_my_pi` parse OpenCode-family JSON events (Oh-My-Pi via `omp --mode json` whose first `type:session` header carries the resumable id); `_run_qwen_code_emanation` captures Qwen's headless stdout/stderr until a stable JSON/resume contract exists. The first event that carries a session id writes it to `daemon.json` (`claude_session_id`, `codex_session_id`, `opencode_session_id`, `mimocode_session_id`, `oh_my_pi_session_id`, or `cursor_session_id`) immediately — typically within ms of process start, well before any LLM work — so `daemon(action="ask")` is usable from the moment `emanate` returns for backends with resume support. stderr drains in a background thread to its own pipe (no longer merged into stdout), so API/auth/rate-limit errors surface as `cli_output` events with `stream="stderr"`. For claude-code: a final `result` event with `is_error=true` is surfaced as `mark_failed`, so an error inside the LLM stream doesn't masquerade as success even when the underlying process exits 0. For codex: absence of a `turn.completed` event (combined with no captured `agent_message`s) is treated as failure similarly. Codex's `--ephemeral` flag is intentionally NOT passed: it would disable session persistence and break `daemon(ask)`. See GH issues #99 / #100 / #101 for the prior buffered-text failure mode that motivated this design.
- **Detached daemon supervisor (unconditional all-backend route).** `_handle_emanate` validates and writes a secret-free manifest, then the POSIX adapter launches one detached supervisor per run. `execution_host.py` composes the existing `DaemonManager`/`_BackendSpec` setup and runners in that process, so LingTai, Claude, Codex, OpenCode, MiMo, Qwen, Oh-My-Pi, Kimi, and Cursor retain one production parser each. The supervisor owns deadline/control, exact child process groups, run-owned diagnostics, terminal `DaemonRunDir` truth, and notification publication; the parent manager retains only submit/inspect/control state. Presets, selected skills, task MCP registrations, and the built-in `daemon_common.checkpoint` / `daemon_common.finish` surface are reconstructed from the validated manifest and run directory. POSIX lifecycle infrastructure can transfer one already-open child descriptor manager → supervisor → execution child with `SCM_RIGHTS`, closing every sender/failed/discarded owner; the host closes it if no runtime consumer claims it. Production Driver daemon dispatch still fails closed because this slice does not connect that descriptor to authority composition or remove the existing lease gate. Resolved credentials and descriptors are never written to the manifest; request/control files and supervisor logs use restrictive permissions where supported. `system.refresh`/agent stop only shuts down parent-local resources and cannot see detached futures or child handles.

## Two-axis `_meta` boundary

Daemon handlers are unchanged and their results use the canonical
`ToolResultBlock.metadata` sidecar. The daemon loop projects a daemon-local
`agent_meta.agent_state` (runtime identity, token counters, context usage, and
post-90% countdown/warning state) onto the newest final result through
`kernel/meta_block.attach_daemon_agent_meta`; older snapshots remain historical.
The worker's countdown is deterministic `_DaemonMetaState` state rather than
meta-prompt theater; value `1` remains visible through an ordinary provider
response, then expiry mechanically retains the latest tool pair and sends an
explicit recovery message before another provider continuation unless that
response performed a valid sole-call proactive compact.
`agent_state.context` also carries `system_prompt` — the same bounded
progressive-disclosure warning `kernel/meta_block.render_system_prompt_pressure_context`
renders for the main agent — whenever this daemon's own local rendered system
prompt (counted once via the kernel `count_tokens()` when `_DaemonMetaState` is
constructed) is strictly above the effective `LINGTAI_SYSTEM_PROMPT_PRESSURE_RATIO` threshold (default 40%) of THIS daemon's own currently-resolved
context window; it is never derived from or compared against the parent's
window, and is omitted whenever either metric is unknown/zero.
For LingTai-backend daemon LLM runs, that resolved window comes from the explicit
preset's canonical `manifest.llm.context_limit`, else from the parent service's
valid `_context_window`, else from the shared `CONSERVATIVE_CONTEXT_WINDOW`
fallback (272,000); it is passed into the daemon `LLMService` before telemetry
reads it back from the session/service.
Parent agent/session and notification/communication state is not shared or
injected. `llm/interface_converters.py` projects the sidecar for both dictionary
and string results without requiring handler wrappers and preserves the daemon's
intentional omission of the parent notification axis.

## Dependencies

- `lingtai.kernel.llm.base.FunctionSchema` — tool schema type
- `BaseAgent._enqueue_system_notification` — compact daemon completion/failure events
- `lingtai.kernel.token_ledger` — `append_token_entry` for token accounting
- `lingtai.kernel.i18n` — `t()` for localized strings
- `lingtai.tools.registry` — `setup_capability`, `canonical_capability_name` for preset sandbox instantiation
- `lingtai.presets` — `load_preset`, `expand_inherit` for per-emanation preset resolution
- `lingtai.kernel.preset_connectivity` — `check_connectivity` for LLM reachability pre-flight
- `lingtai.kernel.config_resolve` — `resolve_env` for API key resolution
- `lingtai.llm.service` — `LLMService` for dedicated preset LLM services
- `lingtai.agent.Agent` — parent agent type (TYPE_CHECKING only)

## Composition

- **Parent:** `src/lingtai/tools/` (tool package).
- **Siblings:** `avatar/`, `mcp/`, `knowledge/` (private durable memory), `skills/` (skill catalog), canonical `shell` (retained `bash/` implementation).
- **Summary composition edge:** `_build_daemon_apriori_summarizer_fn` in
  `daemon/__init__.py` closes over the effective daemon `LLMService` and
  `DaemonRunDir`; `_run_emanation` injects it into the kernel `ToolExecutor`.
  The executor retains raw-log and replacement ownership; the daemon logger
  exposes its run-local raw-result locator to the worker.
- **Manual:** `daemon/manual/SKILL.md` — skill documentation for the LLM. It
  progressively discloses into `manual/reference/`: the operational bundles
  `cleanup/`, `forensics/`, and `inspection/`, plus `cli-backends/`, whose own
  `reference/backends/<backend>/SKILL.md` leaves document one CLI backend each
  (`claude-p`, `codex`, `cursor`, `kimicode`, `lingtai`, `mimocode`,
  `oh-my-pi`, `opencode`, `qwen-code`) and pair with the `_BackendSpec` runners
  in `daemon/runtime.py`.
- **Contract:** `daemon/CONTRACT.md` — unified daemon contract for tool-surface behavior, selected skills, one-run MCP registrations, completion, artifacts, backend support status, review triggers, and acceptance gates.
- **Kernel hooks:** `setup()` is called during capability initialization; `DaemonFamilyDispatcher.handle()` (`daemon/_tool_family.py`) is registered as the `daemon` tool handler, and translates one envelope call into `DaemonManager.handle()`'s unchanged flat shape. `daemon` is on `kernel/tool_result_summary.py`'s `_LTP_V2_MIGRATED_FAMILIES` allowlist, so the canonical root `summarize` control it advertises is actually honored.
