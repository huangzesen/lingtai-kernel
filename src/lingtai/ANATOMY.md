---
related_files:
  - ANATOMY.md
  - src/lingtai/BEHAVIORS.md
  - src/lingtai/__init__.py
  - src/lingtai/__main__.py
  - src/lingtai/agent.py
  - src/lingtai/adapters/__init__.py
  - src/lingtai/adapters/acp/ANATOMY.md
  - src/lingtai/adapters/acp/puffo_v0.py
  - src/lingtai/adapters/acp/puffo_v1.py
  - src/lingtai/adapters/posix/ANATOMY.md
  - src/lingtai/adapters/windows/ANATOMY.md
  - src/lingtai/adapters/shell.py
  - src/lingtai/adapters/shell_process.py
  - src/lingtai/adapters/shell_state_lock.py
  - src/lingtai/adapters/refresh_watcher.py
  - src/lingtai/adapters/process_scan.py
  - src/lingtai/adapters/lifecycle_clock.py
  - src/lingtai/adapters/project_workspace.py
  - src/lingtai/adapters/browser_transport.py
  - src/lingtai/adapters/avatar_launcher.py
  - src/lingtai/adapters/workdir_lease.py
  - src/lingtai/auth/ANATOMY.md
  - src/lingtai/cli.py
  - src/lingtai/cli_project.py
  - src/lingtai/cli_acp.py
  - src/lingtai/cli_puffo_v0.py
  - src/lingtai/cli_daemon.py
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/avatar/ANATOMY.md
  - src/lingtai/tools/bash/ANATOMY.md
  - src/lingtai/tools/daemon/ANATOMY.md
  - src/lingtai/tools/knowledge/ANATOMY.md
  - src/lingtai/tools/mcp/ANATOMY.md
  - src/lingtai/tools/skills/ANATOMY.md
  - src/lingtai/tools/system/preset.py
  - src/lingtai/tools/system/ANATOMY.md
  - src/lingtai/tools/system/CONTRACT.md
  - src/lingtai/tools/system/settings.py
  - src/lingtai/kernel/meta_block.py
  - src/lingtai/tools/web_search/ANATOMY.md
  - src/lingtai/tools/registry.py
  - src/lingtai/CONTRACT.md
  - src/lingtai/init.jsonc
  - src/lingtai/tools/psyche/ANATOMY.md
  - src/lingtai/tools/psyche/prompt.py
  - src/lingtai/tools/psyche/settings.py
  - src/lingtai/init_reader.py
  - src/lingtai/init_schema.py
  - src/lingtai/intrinsic_skills/ANATOMY.md
  - src/lingtai/intrinsic_skills/__init__.py
  - src/lingtai/intrinsic_skills/system-manual/SKILL.md
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/intrinsic_skills/system-manual/reference/substrate-manual/SKILL.md
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/service.py
  - src/lingtai/mcp_catalog.json
  - src/lingtai/mcp_servers/ANATOMY.md
  - src/lingtai/mcp_servers/wechat/manager.py
  - src/lingtai/network.py
  - src/lingtai/presets.py
  - src/lingtai/prompts/ANATOMY.md
  - src/lingtai/services/ANATOMY.md
  - src/lingtai/services/mcp.py
  - src/lingtai/services/session_mcp.py
  - src/lingtai/services/mcp_inbox.py
  - src/lingtai/services/mcp_registry.py
  - src/lingtai/venv_resolve.py
  - src/lingtai/kernel/ANATOMY.md
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/kernel/nudge/init_config.py
  - src/lingtai/kernel/presets.py
  - src/lingtai/kernel/snapshot/ANATOMY.md
  - src/lingtai/kernel/workdir.py
  - src/lingtai/kernel/session_stats/ANATOMY.md
  - src/lingtai/kernel/session_stats/CONTRACT.md
  - tests/test_agent_preset_manifest.py
  - tests/test_agent_config_hydration.py
  - tests/test_cli.py
  - tests/test_cli_daemon.py
  - tests/test_deep_refresh.py
  - tests/test_kernel_migrate.py
  - tests/test_init_schema.py
  - tests/test_lingtai_facade.py
  - tests/test_preset_materialization.py
  - tests/test_presets.py
  - tests/test_venv_resolve.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# lingtai

PyPI wrapper package — `Agent(BaseAgent)` with composable capabilities, preset materialization, CLI, and public re-exports.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | Role |
|---|---|
| `__init__.py` | Lazy public API facade — re-exports every name in ``__all__`` on first access via PEP-562 ``__getattr__`` from its canonical source module (``_LAZY_EXPORTS``). Public Core values include `StopResult`/`StopStatus`, the typed bounded-stop proof. ``import lingtai`` loads only the stdlib and package version, using `0+unknown` when a source checkout has no distribution metadata; implementation modules resolve lazily. |
| `__main__.py` | `python -m lingtai` → `cli.main()` |
| `agent.py` | **THE key file.** `Agent(BaseAgent)` — layer-2 agent with capability composition, preset swap, persistent MCP plus explicit process-local session-MCP leases, init.json refresh, and default POSIX event-journal + notification-store + agent-presence + workdir-lease + snapshot/source-revision injection for outer callers (it selects the platform lease via `lingtai.adapters.workdir_lease.select_workdir_lease` when `working_dir` is present and no `workdir_lease` was passed, and constructs `PosixAgentPresenceStoreAdapter(working_dir)` when no `agent_presence` was passed — see `kernel/agent_presence/CONTRACT.md`). It selects the refresh-watcher capability through `lingtai.adapters.refresh_watcher.select_refresh_watcher` when no watcher is injected. It also constructs the portable `SystemLifecycleClockAdapter()` when no `lifecycle_clock` was passed (no `working_dir` needed — see `kernel/lifecycle_clock/CONTRACT.md`). Its narrow `resolve_cache_miss_budget()`, `resolve_notification_max_chars()`, and `resolve_runtime_policy()` methods are lazy outer composition hooks from kernel metadata and boot/refresh setup to the System-owned `tools/system/settings.py` resolvers; Core never imports that tool module. Selection and `BaseAgent` construction share one guard that closes the wrapper-owned journal best-effort while preserving the original failure. `mount_session_mcp_stdio` delegates atomic overlay publication to `services/session_mcp.py`, and Agent stop closes those leases after execution quiescence. Mounted MCP tools preserve the server's advertised schema; exact closed LTP-v2 families receive `_reasoning` → public `reasoning` restoration, while stdio/HTTP handlers copy arguments and remove only server-undeclared host-private fields immediately before the provider call. Explicitly declared private fields and ordinary unknown business fields remain untouched. Tool schemas registered here carry package ownership into the inherited BaseAgent inventory renderer; `Agent` no longer duplicates tool-inventory rendering, so package glossaries are appended once in the kernel path. |
| `adapters/acp/` | Governed ACP v1 local-stdio Adapter: strict framing, one session/active prompt, canonical execution workspace, atomic session stdio MCP lease, baseline content translation, cancellation, and teardown. It consumes the protocol-neutral Core turn boundary; see its Anatomy/Contract. |
| `adapters/posix/` | Narrow production POSIX adapter package for the JSONL + SQLite event journal, filesystem mail transport, notification store, POSIX workdir lease, refresh-watcher trio, duplicate-launch process scan, fixed-command Git snapshot/source-revision adapter, and POSIX avatar launcher. See `adapters/posix/ANATOMY.md`. |
| `adapters/windows/` | Narrow production native-Windows adapter package: `msvcrt` byte-range workdir lease, refresh-watcher trio (detached handoff, entrypoint, CIM/`.suspend` process mechanism), CIM duplicate-launch process scan, avatar launcher, detached daemon supervisor with inherited-handle capsule wire and entrypoint mirrors, process-incarnation identity, PowerShell shell dialect, Job Object async shell process adapter, `msvcrt` shell state lock, and the shared `_win32` ctypes surface. See `adapters/windows/ANATOMY.md`. |
| `adapters/__init__.py` | The adapter package marker: "Production adapters for Core-owned ports." No wiring lives here — each Port's outer selector is its own module. |
| `adapters/workdir_lease.py` | Outer platform selector for the `WorkdirLeasePort` adapter: composition-root wiring that reads the running platform, selects the concrete adapter, and constructs it. Deliberately the only place that branches on the OS for leasing — Core never imports it; `lingtai.agent` and `lingtai.cli` call `select_workdir_lease` and inject the returned Port into `BaseAgent` and the SQLite rebuild. An unsupported platform fails loudly rather than silently degrading. |
| `adapters/browser_transport.py` | Production static HTTP(S) Adapter for the internal browse Core-owned `BrowserPort`; bounds DNS wait with one in-flight resolver job, pins vetted IPs, and preserves Host/SNI, selected lazily by unified web setup. |
| `adapters/lifecycle_clock.py` | The one portable production `SystemLifecycleClockAdapter` for the Core-owned `LifecycleClockPort` — direct `wall_seconds()`→`time.time()` / `monotonic_seconds()`→`time.monotonic()`, no caching or policy. Not POSIX (no filesystem/`fcntl`/platform selection), so it sits at the top of `adapters/` rather than under `adapters/posix/`; its promise/navigation are owned by the kernel `lifecycle_clock/` governed pair (`src/lingtai/kernel/lifecycle_clock/CONTRACT.md` + `ANATOMY.md`). |
| `adapters/project_workspace.py` | Filesystem implementation of the Project Core Port: exclusively creates one fresh `.lingtai` tree, writes its seed, and applies an injected init-reader validation. |
| `cli.py` | `lingtai-agent run <dir>` / `lingtai-agent acp --agent-dir <dir>` / `lingtai-agent acp --profile puffo-v0\|puffo-v1 --runtime-id <id>` / `lingtai-agent puffo-v0 provision\|revoke` / `lingtai-agent project create ...` / `lingtai-agent check-caps` / `lingtai-agent log ...` / `lingtai-agent maintenance cleanup <target>` entry points; the `run` composition root performs a post-stop hard exit only when existing worker-poison state would otherwise keep the old process alive and block the refresh watcher |
| `cli_project.py` | Inbound composition for one fresh `project create` seed: caller inputs, wrapper preset loading, current-reader validation, Project adapter, and output; it does not start an Agent. |
| `cli_acp.py` / `adapters/acp/puffo_v0.py` / `adapters/acp/puffo_v1.py` / `cli_puffo_v0.py` | ACP outer composition and Puffo full-tool profiles: generic ACP accepts a local existing agent directory; both Puffo profiles resolve only an operator-provisioned opaque runtime id to canonical identity/workspace and admit only authenticated ACP-origin provider turns. `puffo-v0` forces empty session MCP; `puffo-v1` accepts only the single fixed Puffo Core stdio service. The local control plane still provisions/revokes the shared binding. This is a controlled-entrypoint gate, not same-OS host isolation or runtime containment. |
| `cli_daemon.py` | `lingtai-agent daemon emanate|list|check` — the programmatic (shell/Python/CI) skin over the daemon engine. `_CliDaemonAgent` is the minimal parent-agent facade `DaemonManager` reads (no lease, heartbeat, or agent identity), built from the agent's effective config through the canonical `init_reader.read_init`; `emanate` validates the tasks file against the tool's own emanate schema and enforces the preset allowlist and effective capability policy before previewing, then dispatches through the `DaemonFamilyDispatcher` envelope only under `--yes`; `_ReadOnlyDaemonView` binds the manager's unmodified `_handle_list`/`_handle_check` with both of its write paths (startup reconciliation, lazy daemon.json repair) removed |
| `network.py` | Read-only network topology crawler — avatar/contact/mail edge discovery |
| `presets.py` | Compatibility shim re-exporting the kernel preset library (`lingtai.kernel.presets`) |
| `init.jsonc` / `init_reader.py` / `init_schema.py` | Kernel canonical shape plus the one real parse → materialize → validate → resolve reader. `InitReadOutcome` reports fully-effective, ignored-field, or failed reads with typed PASS/NUDGE/BLOCKED/UNKNOWN shape evidence without rewriting user-owned init.json; `validate_init()` remains the schema validator. See `CONTRACT.md`. |
| `venv_resolve.py` | Python venv resolution — explicit `init.json` venv → global runtime → auto-create with platform-aware interpreter selection, plus kernel-owned `.lingtai-env.json` marker check/stamp semantics for TUI and kernel callers |
| `intrinsic_skills/` | Standalone skill bundles (manuals plus sidecar scripts/assets, e.g. the `lingtai-kernel-anatomy` checker and benchmark) copied verbatim into `.library/intrinsic/capabilities/`; see `intrinsic_skills/ANATOMY.md` for the full bundle inventory and the packaging rule its `reference/`/`assets/` levels depend on |
| `mcp_servers/` | Curated MCP server implementations shipped in the `lingtai` distribution and launched by `mcp_catalog.json` via `python -m lingtai.mcp_servers.<name>`; see `mcp_servers/ANATOMY.md` for the bundled `SKILL.md` manual-action and sidecar packaging contract. Stateful curated servers may own per-agent sidecars: the WeChat manager checkpoints `wechat/state.json` cursor progress and `wechat/inbox_seen.json` replay guards in `mcp_servers/wechat/manager.py:266` and `mcp_servers/wechat/manager.py:912`. |

### Key functions / classes

**`agent.py`** — `Agent(BaseAgent)`: `__init__` (accept `capabilities=` + `disable=`, expand groups, `apply_core_defaults`, decompress addons, setup caps, install manuals, load MCP) · `resolve_cache_miss_budget` (lazy System settings delegation) · `_boot_official_intrinsics` (defers only the private CLI shell's initial declared-tool boot) · `_setup_capability` · `_persist_llm_config` · `_install_intrinsic_manuals` · `_build_system_prompt` / `_build_system_prompt_batches` (pass Psyche's resolved third-party `base_prompt` (`self._base_prompt`) to the kernel builder, which renders it after raw `principle` and before the rest of Batch 1) · `_load_mcp_from_workdir` (also tracks specs in `_mcp_init_specs`) · `_retry_failed_mcps` (re-spawn dead MCPs on `system(refresh)` — issue #34) · `_read_init` delegates the shared `lingtai.init_reader.read_init` path, then publishes the real reader's effective secret-redacted manifest to `system/manifest.resolved.json` via `lingtai.kernel.workdir.write_resolved_manifest` — issue #259; it never rewrites user-owned init.json. · `_setup_from_init` (**full reconstruct** — shared by boot and live refresh; reads `manifest.disable` and re-applies `apply_core_defaults`) · `_activate_preset` (runtime swap, atomic write) · `_reconstruct_context` / `_reload_prompt_sections` (one shared active/passive full reconstruction contract plus authoritative all-source composer; resolves Psyche's immutable prompt plan exactly once per reconstruction and, only after the final prompt flush, commits the resolved `pad`/`pad_file` pair plus all six prompt owner values/pointers to `_psyche_settings_snapshot`; provider/session replay follows) (Psyche prompt-owner contract: the externally changeable prompt surface is exactly `base_prompt`/`covenant`/`comment`; the six former init spellings are compatibility-known but inert. Resolves `base_prompt` into `self._base_prompt` (mirrored to `system/base_prompt.md`), writes `covenant`/`rules` plus the Psyche plan's `substrate`/`principle`/`procedures` contributions (init overrides ignored) and `guidance.json`, then Psyche-sourced `comment`; delegates `character` to `_lingtai_load` and `pad` to `_pad_load` — the canonical composers — so boot/refresh/molt are consistent and hook-order-independent) · `_dispatch_tool` (strict mounted LTP-v2 MCP family reasoning restoration) · `connect_mcp` / `connect_mcp_http` (schema-aware MCP argument boundary) · `start` / `stop`

*(Function-name anchors, not line numbers — line citations in this table drift with every edit; grep the name in `agent.py` for the current location.)*

**Current Psyche prompt ownership:** Active rebuild/molt reconstruction reads
the independent `<workdir>/settings/psyche.json` v1 document exactly once;
refresh resolves the same immutable candidate immediately after its successful
init read and before live teardown, then passes it through reconstruction.
Only the `base_prompt`, `covenant`, and `comment` pairs enter local composition.
Pad plus all six owner values/pointers commit to Psyche SHOW only after the full
reconstruction succeeds; a failed candidate restores prompt-manager sections,
the wrapper base prompt, and derived base/covenant/system mirrors as the prior
generation. The six former init keys are compatibility-known but inert. See
`tools/psyche/{ANATOMY,CONTRACT}.md` for the closed reader and v1 serializer.

**`cli.py`**: `load_init` · `build_llm_service` (shared with `cli_daemon.py`) · `build_agent` · `run` · `_force_exit_if_worker_poisoned` · `_handle_log_command` · `_handle_maintenance_command` · `main`

**`cli_acp.py`**: `add_acp_parser` · `run_acp` · `handle_acp_command`

**`cli_project.py`**: `add_project_parser` · `_request` · `_validate_agent` · `handle_project_command`

**`cli_daemon.py`**: `_CliDaemonAgent` (`for_dispatch` reads effective config via `init_reader.read_init`; `for_inspection` reads none) · `_CliDaemonAgent.effective_capabilities` / `install_tool_surface` (`registry.apply_core_defaults` — honors `manifest.disable` and authored kwargs) · `_ReadOnlyDaemonView` (overrides `_load_or_rebuild_daemon_state` so inspection reconstructs in memory instead of repairing on disk) · `_read_effective_init` · `_load_tasks_file` · `_check_schema` / `_validate_emanate_input` (interprets the tool's own `_emanate_input_schema` at preview time) · `_enforce_preset_allowlist` · `_enforce_capability_policy` · `_dispatch_through_tool_family` · `add_daemon_parser` · `handle_daemon_command`

**`presets.py`**: compatibility re-export shim (`presets.py:1-21`); implementation lives in `lingtai.kernel.presets` (`discover_presets_in_dirs` :177 · `load_preset` :232 · `materialize_active_preset` :360 · `expand_inherit` :580).

**`init_reader.py` / `init_schema.py`**: `read_init` is the shared parse → compatibility classify → materialize → prepare → validate → resolve path; `InitReadOutcome` reports `FULLY_EFFECTIVE`, `READ_OK_WITH_IGNORED_FIELDS`, or `READ_FAILED` plus typed `PASS`/`NUDGE`/`BLOCKED`/`UNKNOWN` shape evidence without rewriting user-owned init.json. `manifest.capabilities.bash` is mapped in memory to canonical `shell` and differing dual values fail closed. Ordinary runtime keys (`context_limit`, `max_rpm`, `streaming`, `aed_timeout`, `max_aed_attempts`, `snapshot_interval`, `activeness`) are root-level recognized-and-ignored compatibility data: they are neither type-checked nor passed to runtime policy. Preset `manifest.llm.context_limit` remains preset-local and materialization discards it. A `READ_FAILED` outcome takes precedence in `finding_decision` over any earlier compatibility NUDGE (UNKNOWN, or BLOCKED when the shape itself was blocked). `validate_init` remains the strict schema validator; legacy/deprecated fields are diagnosed as ignored paths rather than stripped. `manifest.llm.compact_threshold` and the Codex/custom-Responses scope of `manifest.llm.thinking` remain validated in `init_schema.py`; `manifest.cache_miss_budget` is now schema-unknown ignored data, reported without rewrite and never hydrated/effective.

**`network.py`**: `build_network` :310 · `_discover_agents` :147 · `_build_avatar_edges` :172

**`venv_resolve.py`**: `resolve_venv` :75 · `venv_python` :102 · `_is_default_runtime_dir` :109 · `_test_venv` :119 · `_test_venv_detail` :125 · `_create_venv` :168 · `_PythonSelectionPolicy` :206 · `_normalize_selector_architecture` :222 · `_parse_macos_major` :232 · `_python_selection_policy` :246 · `_probe_python_candidate` :323 · `_selection_remedy` :416 · `_find_python` :531 · `_env_marker_status` :565 · `_env_marker_status_detail` :570 · `_remove_mismatched_managed_venv` :643 · `_env_marker_main` :658

> Config-resolution helpers (`load_jsonc`/`resolve_env`/`resolve_paths`/`_resolve_capabilities`) and preset-connectivity probing (`check_connectivity`/`check_many`) live in the kernel — import directly from `lingtai.kernel.config_resolve` / `lingtai.kernel.preset_connectivity`. The former wrapper-side compatibility shims were removed (no back-compat shims per repo policy).

## Connections

**Inbound:** `lingtai-tui` calls `cli.run()` to boot agents; imports `load_preset`, `discover_presets_in_dirs` for UI. Kernel's `BaseAgent` is the parent class.

**Project creation:** `cli_project` composes caller inputs, wrapper `load_preset`, the current init reader, and `FilesystemProjectWorkspaceAdapter` for `kernel.project`; it writes a seed only and never starts an Agent.

**Outbound — kernel:** `lingtai.kernel.base_agent.{BaseAgent,StopResult,StopStatus}`, `.config.AgentConfig`, `.event_journal.EventJournalPort`, `.mail_transport.MailTransportPort`, `.workdir_lease.WorkdirLeasePort`, `.notification_store.NotificationStorePort` (S4: capability-native persistence for `.notification/` channel mirrors; see `kernel/notification_store/CONTRACT.md`), `.agent_presence.AgentPresenceStorePort` (own-heartbeat + foreign liveness; see `kernel/agent_presence/CONTRACT.md`), `.lifecycle_clock.LifecycleClockPort` (S7b: wall/monotonic lifecycle time; see `kernel/lifecycle_clock/CONTRACT.md`), `.snapshot.{SnapshotPort,SourceRevisionPort}` (S5: workdir capture and bounded source identity; see `kernel/snapshot/CONTRACT.md`), `.prompt.build_system_prompt`, `.handshake.resolve_address`, and the shared `lingtai.init_reader.read_init` / `lingtai.kernel.workdir.write_resolved_manifest` path. Legacy migration modules remain outside this production reader path; see `../lingtai/kernel/migrate/CONTRACT.md` for their retained historical/test surface.

**Outbound — adapters:** the CLI composition root injects `lingtai.adapters.posix.mail.PosixFilesystemMailAdapter` (the production `MailTransportPort` implementation, back-compat public name `FilesystemMailService`), `lingtai.adapters.posix.event_journal.PosixJsonlEventJournalAdapter`, `lingtai.adapters.posix.notification_store.PosixNotificationStoreAdapter` (S4: the production `NotificationStorePort` implementation), `lingtai.adapters.posix.git_cli.PosixGitCliAdapter` (S5: distinct workdir and running-source instances), the portable `lingtai.adapters.lifecycle_clock.SystemLifecycleClockAdapter` (S7b: the production `LifecycleClockPort` implementation, constructed by `Agent` fallback and explicitly by `cli.build_agent`), and — via `lingtai.adapters.workdir_lease.select_workdir_lease` — the production `WorkdirLeasePort` (`lingtai.adapters.posix.workdir_lease.PosixWorkdirLeaseAdapter`) for both agent construction and the `log rebuild` command; and — via `lingtai.adapters.refresh_watcher.select_refresh_watcher` — the production `RefreshWatcherPort` for `Agent` and CLI construction. Both selectors fail loud on unsupported platforms. The unified `web` capability composes `lingtai.adapters.browser_transport.VettedHttpTransport` only inside its retained web owner; the browse Core policy and `BrowserPort` remain in the internal browser child pair. The avatar capability
selects its POSIX production launcher lazily through
`lingtai.adapters.avatar_launcher.select_avatar_launcher`. A production
Windows launcher (`WindowsAvatarLauncherAdapter`) is wired for
`sys.platform == "win32"` alongside the POSIX one; only genuinely
unsupported platforms fail loudly.

**Cross-module:** `agent.py` → `lingtai.tools.registry.{setup_capability,INTRINSICS,CORE_DEFAULTS}`; unified web setup → `lingtai.adapters.browser_transport.VettedHttpTransport` (lazy composition only), `services.mcp_registry.{decompress_addons,read_registry}`, `services.mcp_inbox.MCPInboxPoller`, `services.mcp.{MCPClient,HTTPMCPClient}`, `llm.service.LLMService`, `presets`, `lingtai.kernel.config_resolve`, `init_schema`. `cli.py` → `agent.Agent`, `lingtai.tools.registry.{CORE_DEFAULTS,get_all_providers}`, `lingtai.kernel.config_resolve`, `presets`.

**Agent → BaseAgent:** Three-layer hierarchy: `BaseAgent` (kernel) → `Agent` (capabilities) → `CustomAgent` (domain). Agent adds capability registration, MCP auto-loading, preset swap, full init.json reconstruct, and composes `PosixJsonlEventJournalAdapter`, `PosixNotificationStoreAdapter`, and distinct `PosixGitCliAdapter` snapshot/source-revision instances for callers that did not supply those dependencies (`agent.py:115-151`).

**Capability registration:** `setup_capability()` in `lingtai/tools/registry.py`; the registry is `BUILTIN_TOOLS` (per-tool module paths under `lingtai.tools.<pkg>`) plus `CORE_DEFAULTS` (which boot automatically). Agent calls `apply_core_defaults` + `_setup_capability` (agent.py) during `__init__` and `_setup_from_init`. Hosts disable defaults via the `disable=[...]` kwarg or `manifest.disable` in init.json. The six mandatory intrinsics are injected separately as `BaseAgent(intrinsics=lingtai.tools.registry.INTRINSICS)`.

**Agent init reader + preset materialization:** `cli.load_init` (boot) and `Agent._read_init` (refresh) are composition roots that both delegate the shared `lingtai.init_reader.read_init` parse/materialize/validate/resolve path; neither constructs a migration workspace or rewrites user-owned init.json. Then `materialize_active_preset` (`lingtai/kernel/presets.py`) reads `manifest.preset.active`, loads preset via the **required injected preset-loader callback** (the wrapper module-level `agent.load_preset`, whose production read callback is migration-free), and substitutes `llm`+`capabilities` into manifest before validation. Daemon/system tools resolve presets through the fail-loud `BaseAgent.load_preset` hook (`Agent` sets `_preset_loader = agent.load_preset`); preset materialization mutates only the in-memory effective mapping and the existing redacted manifest artifact is derived separately. The preset owns explicit opt-in capabilities, but per-agent init.json kwargs survive in two ways: (1) for capabilities the preset *also* enables, init.json wins key-by-key; (2) for always-on `CORE_DEFAULTS` capabilities the preset *omits* (daemon, bash, knowledge, …), init.json kwargs are carried forward so `apply_core_defaults` doesn't re-add an empty entry and lose e.g. `daemon.manager_pool_size`. Non-core optional caps the preset omits are dropped (the swap). `CORE_DEFAULTS` lives in `lingtai.tools.registry` and is injected via the `core_defaults=` arg by both callers (`agent._read_init` :1224, `cli.load_init` :62) — the kernel does not import the `lingtai.tools` package. `skills.paths` additionally append-merges (preset defaults first). For the LingTai-agent-facing preset runtime model (raw vs. resolved `init.json`, preset identity, the two catalogs, main-agent swap/refresh, and the daemon task/CLI distinction), read `src/lingtai/intrinsic_skills/system-manual/SKILL.md` → `reference/substrate-manual/SKILL.md` §11 — the canonical detailed reference this Anatomy routes coding agents toward.

## Composition

Parent: `src/lingtai/` under `lingtai-kernel/src/` alongside `lingtai/kernel/` (kernel package) and the `lingtai/tools/` package (concrete built-in tools; see `tools/ANATOMY.md`). Siblings: `llm/`, `services/`, `auth/`. See `../../ANATOMY.md`.

## State

| Path | When | What |
|---|---|---|
| `<workdir>/init.json` | `_activate_preset` :1254 (explicit preset action only), `init_reader.py` read path | User-owned input. Boot/refresh parse, materialize, validate, and resolve in memory; the reader never strips, canonicalizes, persists venv paths, or otherwise rewrites this file. |
| `<workdir>/settings/psyche.json` | `tools/psyche/settings.py` via `tools/psyche/prompt.py` and `Agent._setup_from_init` / `_reconstruct_context` | Optional strict Psyche-owned v1 prompt document. Missing defaults all six owner values; any present invalid/non-regular/raced document aborts plan composition. Refresh validates it before live teardown. Agent never writes or migrates it. |
| `<workdir>/logs/{events.jsonl,log.sqlite}` | `Agent.__init__` :115-154 and `cli.build_agent` :125-141 | Authoritative structured event JSONL plus derived SQLite query sidecar, owned by the injected POSIX adapter. |
| `<workdir>/system/llm.json` | `_persist_llm_config` :136 | LLM provider/model/base_url for revive |
| `<workdir>/system/manifest.resolved.json` | `_read_init` :1169 via `lingtai.kernel.workdir.write_resolved_manifest` | Derived runtime artifact (issue #259): fully-resolved manifest (preset materialized, validated, paths resolved) with secret-bearing keys removed, plus `schema`/`generated_at`/`source`/`preset` metadata. Atomic write, regenerated on every boot/refresh/molt-reload; init.json is never written back. |
| `<workdir>/system/{base_prompt,covenant,principle,substrate,procedures,rules,pad,lingtai}.md` + `system/guidance.json` + `system/system.md` + `pad_append.json` | `Agent._reload_prompt_sections` / `_reconstruct_context` | Prompt sections from closed Psyche ownership plus disk/package sources. **Psyche prompt-owner contract:** `<workdir>/settings/psyche.json` is the sole owner of the six `base_prompt`/`base_prompt_file`, `covenant`/`covenant_file`, and `comment`/`comment_file` inputs; the six former init spellings are compatibility-known but inert. Reconstruction consumes one immutable prompt plan (prevalidated before live-refresh teardown), preserves the existing file-over-inline/path behavior, mirrors only resolved base/covenant content (comment remains non-mirrored), and publishes the applied Pad pair plus all six owner values/pointers to the eight-field Psyche SHOW snapshot only after the successful final prompt flush. A failed candidate restores the prior prompt-manager sections, `self._base_prompt`, base/covenant mirrors, `system/system.md`, and SHOW as one applied generation. Resolved `base_prompt` remains the third-party (application / recipe / preset) injection point in `self._base_prompt`, mirrored to `system/base_prompt.md`, and rendered by the kernel builder after raw `principle` and before the rest of Batch 1 (it is NOT a prompt-manager section). `covenant.md`→`covenant` (operator contract). `lingtai.md`→`character` (via `_lingtai_load`); `pad.md`+`pad_append.json`→`pad` (via `_pad_load`). `character` is the agent's identity (灵台) with two supported modes: a nonempty resolved `lingtai` value (inline or `lingtai_file` content) is forced and materialized into `system/lingtai.md` on boot, refresh, and post-molt prompt reconstruction; an absent or empty resolved value selects self-evolve mode and leaves `system/lingtai.md` untouched so file-authored changes persist until canonical reconstruction. It is distinct from `covenant`, from the third-party `base_prompt` injection point, and from the mechanical `identity` section. `lingtai`/`lingtai_file` was renamed from `prompt`/`prompt_file` with **no legacy alias** — a stale `prompt` field is an unknown-field warning. Static plan layers are NOT external overrides: `principle.md` mirrors packaged `lingtai/prompts/principle/principle.md` (init `principle`/`principle_file` ignored — legacy-migrated); `substrate` mirrors packaged `lingtai/prompts/substrate/substrate.md` on every boot (init `substrate`/`substrate_file` remain compatibility-known and ignored by the shared read-only reader — kept compact and routed to the packaged `system-manual` skill); `procedures` likewise follows the static plan. The three packaged bodies now live under `lingtai/prompts/<section>/` (one directory per section) alongside their `<section>.yaml` semantic definitions; the runtime-guidance catalog nests under the section it generates at `lingtai/prompts/meta_guidance/catalog/`; see `src/lingtai/prompts/ANATOMY.md` for the definition-vs-injection map. `brief` remains only a generic retired init field and historical migration name; no runtime path reads `system/brief.md`, renders a brief section, or exposes a brief command. `system/guidance.json` is a TUI-readable **derived** mirror serialized from the skill-style Markdown runtime-guidance catalog (`lingtai/prompts/meta_guidance/catalog/` — `INDEX.md` + per-section `<id>.md`, assembled by `lingtai.kernel.prompt_catalog.load_guidance_catalog`) and refreshed by `_reload_prompt_sections`; it is not itself a prompt section. The static-plan `principle`/`substrate`/`procedures` mirrors keep their skill-style frontmatter on disk, but the rendered prompt section is body-only (frontmatter stripped on read). In those prompt/guidance frontmatter blocks, `related_files` is not ANATOMY and not a dependency map: it is a maintained inner-link graph for crawling related prompt sources (principle ↔ prompt/guidance sources, guidance INDEX ↔ guidance sections), and it should not list tests or indirect package/runtime dependencies merely because they validate or load the files. `lingtai.kernel.prompt` composes no runtime principle prose: language/activeness remain legacy compatibility fields. |
| `<workdir>/.library/intrinsic/` | `_install_intrinsic_manuals` :174 | Wipe-and-rewrite every boot |
| `<workdir>/.agent.json` | `_build_manifest` :262 via `_workdir.write_manifest` | Runtime manifest snapshot. Includes sanitized `llm` (provider/model/base_url) from the live LLMService and `preset` (active/default/allowed) read from `init.json` by `_read_preset_from_init` :300 — see issue #78. |
| `<workdir>/.mcp_inbox/` | MCPInboxPoller (started at :701) | LICC events from out-of-process MCPs |
| `<workdir>/system/agent_record.json` | `BaseAgent._write_session_stats_record` (kernel) via `kernel.session_stats.build_agent_record`, extended by `Agent._build_agent_record_extra` | The Agent Record — one atomic/versioned/redacted live personal record every Agent (including avatars) publishes, throttled by `LINGTAI_SESSION_STATS_REFRESH_SECONDS`. `Agent._build_agent_record_extra` adds `handles`/`integrations` from `services.mcp_registry` (Core itself never imports MCP modules). See `kernel/session_stats/ANATOMY.md` and `CONTRACT.md`. |

## Notes

- **Identity modes:** `lingtai` is optional. A nonempty resolved value, whether
  inline or loaded from `lingtai_file`, is forced and materialized into
  `system/lingtai.md` during boot, refresh, and post-molt prompt reconstruction;
  an absent or empty resolved value selects self-evolve mode and leaves that
  file untouched. This identity is distinct from `covenant`, `base_prompt`,
  and the mechanical `identity` section; `prompt` remains unsupported with no
  legacy alias.

- **CLI, direct boot, and refresh composition:** direct `Agent.__init__` composes normally. `cli.build_agent` explicitly injects the POSIX event journal and passes private `_from_init_boot=True` to create an internal shell; `Agent._boot_official_intrinsics` defers only that shell's initial declared-tool boot. The constructor self-disarms the shell and CLI clears the sentinel again before one configured `_setup_from_init()` pass, so constructor-time event JSON encoding retains the existing `False` default before config hydration. Live refresh re-enters that configured method.
- **Current init-reader discipline:** `lingtai.init_reader.read_init` is the single boot/refresh parse → materialize → validate → resolve path. It diagnoses legacy/deprecated paths and leaves user-owned `init.json` unchanged; `system/manifest.resolved.json` is the only derived effective-config artifact. The retained `lingtai.kernel.migrate` registry is not invoked by production boot/refresh; its historical tests remain in `tests/test_kernel_migrate.py`.
- **Init/preset documentation cross-check obligation:** a change to `init.json` composition, preset materialization, or the daemon-task preset path must re-check all four surfaces together in the same PR — this Anatomy's citations (above), the canonical `reference/substrate-manual/SKILL.md` §11 model, the resident `substrate`/`procedures` routing cues, and `tests/test_preset_runtime_model_docs.py` — rather than updating only the code or only one doc layer.
- **`materialize_active_preset` is pure dict mutation** — disk write only in `_activate_preset` :1261 (atomic `.tmp` → replace).
- **Preset implementation moved to kernel** — wrapper `presets.py` re-exports `lingtai.kernel.presets`; production preset reads validate authored data without invoking the retained migration registry.
- **Sensitive key stripping (capabilities):** `_build_manifest` :262 strips `api_key`, `api_key_env`, `api_secret`, `token`, `password` (`_SENSITIVE_KEYS`) from capability kwargs before writing `.agent.json`.
- **LLM / preset safelists (issue #78):** `_build_manifest` :262 also re-applies `_LLM_PUBLIC_KEYS = ("provider", "model", "base_url", "api_compat", "context_limit")` to the kernel-supplied `llm` block as defense-in-depth, and reads `manifest.preset` from init.json via `_read_preset_from_init` :300 filtered to `_PRESET_PUBLIC_KEYS = ("active", "default", "allowed")`. Anything outside the safelists never reaches `.agent.json` or the identity prompt section. This is the central safety claim of #78 — see `tests/test_agent_preset_manifest.py::test_manifest_never_contains_api_key`.
- **AgentConfig hydration:** `_setup_from_init` rebuilds runtime config through `build_agent_config`. Ordinary runtime fields (`context_limit`, `max_rpm`, `streaming`, `aed_timeout`, `max_aed_attempts`, `snapshot_interval`, `activeness`) come only from System runtime policy (valid env > valid v2 settings > fixed defaults); legacy init values are ignored. Cache-miss budget is no longer an AgentConfig field or manifest overlay: `tools/system/settings.py` owns live env/file/default resolution and `Agent.resolve_cache_miss_budget()` lazily projects its positive scalar to kernel metadata. Legacy `max_turns` (in `MANIFEST_LEGACY_IGNORED`), `manifest.cache_miss_budget`, and `molt_*` manifest values remain deliberately ignored. `manifest.llm.thinking` hydrates verbatim when present (schema values `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`) for every thinking-capable provider — the Codex family, `anthropic`, `openai`, `deepseek`, and any `api_compat="openai"` block on either wire; `llm_supports_thinking` is the shared schema/preset scope rule. When omitted, Codex-family providers (`THINKING_PROVIDERS`) keep the `"default"` sentinel so the Codex adapter applies its own default (`reasoning.effort = "xhigh"`), while custom Responses and all other providers keep the legacy cross-provider `"high"` main-session default. DeepSeek is provider-owned (`THINKING_OWNED_PROVIDERS`): its accepted values are per model and wire and are validated by `src/lingtai/llm/deepseek/policy.py`, and an omitted (or explicit `null`) value sends no reasoning field at all so DeepSeek's own default applies — the kernel never promotes it to the cross-provider `"high"`.
- **Addon decompression** runs BEFORE capability setup so `mcp` capability sees populated `mcp_registry.jsonl` on first reconcile (`Agent.__init__` :33, `_setup_from_init` :1338).
- **MCP retry contract (issue #34):** `_load_mcp_from_workdir` :376 records every registered init.json mcp entry into `self._mcp_init_specs` (name → `{cfg, source, client}`). `_retry_failed_mcps` :524 walks this dict, closes any dead client (`is_connected()` False), respawns with the original config, and reports `{retried, recovered, still_failed, healthy}`. `system(action="refresh")` calls it via `lingtai/tools/system/preset.py:_refresh` before `_perform_refresh` so the documented "fix config → refresh" recovery path works without full process restart.
- **Curated MCP launcher ownership:** `_load_mcp_from_workdir` :795 looks up each init.json mcp entry's `source` in `mcp_registry.jsonl`; when it is exactly `"lingtai-curated"` (the imap/telegram/feishu/wechat/whatsapp/cloud_mail addons from `mcp_catalog.json`), the entry's `init.json` config is activation-only — its presence activates the addon and its non-launch `env` (account/config keys) passes through — while `_resolve_curated_launch` builds the *complete* launcher (transport/`type`, interpreter, module `args`) fresh from `services.mcp_registry.load_catalog()` (the running kernel's own packaged `mcp_catalog.json`, never the registry's decompressed-at-addon-time copy) plus this Agent's own `sys.executable` and currently-imported `lingtai` source root, set as the child's entire `PYTHONPATH`. Interpreter alone is not enough because the MCP stdio transport's default env allowlist excludes `PYTHONPATH`, so a curated child on the right interpreter could still resolve `lingtai` from that interpreter's own site-packages instead of this Agent's actual source. A curated entry's `command`/`args`/`type`/`env.PYTHONPATH` fields are read-compatible legacy inputs: present, they are ignored with one bounded warning naming them, never read for the launch, and never rewritten or migrated on disk. If the catalog cannot supply a safe stdio launcher for a name registered as curated (missing entry, non-stdio transport, malformed `args`), the entry is skipped with a warning — fail loud, never fall back to a stale launcher field. `_mcp_init_specs[name]["cfg"]` holds the resolved effective cfg, so `_retry_failed_mcps` respawns with the same catalog-derived launcher. Non-curated (third-party/plugin/legacy `mcp/servers.json`) entries are unaffected — their `command`/`args`/`type`/`env` still come entirely from their own activation spec — and daemon task/plugin MCPs, which spawn from their own task/plugin configs (never from `mcp_registry.jsonl`), are unaffected by this resolution or by any registry reconcile.
- **Managed-runtime Python selection:** before auto-creating a venv, `venv_resolve.py:_find_python` :531 derives policy from the current process (`sys.platform`, normalized `platform.machine()`, and the macOS product major read directly from `platform.mac_ver()`). On macOS the managed cap is Python 3.11–3.13 across all supported cells (the release workflow builds only cp311/cp312/cp313 wheels, so a managed 3.14 choice would fall onto a source build that can omit the native Rust sidecar); Apple Silicon on macOS 14+ and Apple Silicon on macOS 13 plus x86_64/translated Rosetta on macOS 13+ therefore accept 3.11–3.13. Non-macOS targets retain the open-ended Python >=3.11 baseline. Versioned names are tried newest-first on macOS while generic aliases stay first elsewhere; resolved aliases are deduplicated, and each candidate receives one five-second JSON probe of version/platform/architecture/macOS class. Bad probes fall through, while invalid macOS targets and exhausted searches fail actionably before venv or pip subprocesses start.
- **Runtime venv markers:** `venv_resolve.py` accepts legacy managed venvs without `.lingtai-env.json` if `import lingtai` succeeds, then stamps the marker best-effort. Marker read/parse/probe failures are `error`, not `mismatch`, and never delete the managed runtime. A valid marker that proves a different OS/arch/Python environment is a confirmed mismatch: explicit `init.venv_path` candidates are rejected but left on disk, while only the managed global runtime venv (`~/.lingtai-tui/runtime/venv/`) may be removed before auto-create. The TUI calls this same logic through `python -m lingtai.venv_resolve env-marker {check,stamp} --venv <path>`.
- **Lazy top-level facade:** `src/lingtai/__init__.py` uses PEP-562 ``__getattr__`` to resolve every public name lazily from its canonical source module (`_LAZY_EXPORTS`, ``__init__.py:21-79``). A bare ``import lingtai`` performs only stdlib/importlib.metadata work and uses the established `0+unknown` sentinel when distribution metadata is unavailable; it must not load `lingtai.agent`, `lingtai.kernel`, `tools`, `lingtai.llm`, services, MCP servers, or concrete providers. ``__dir__`` returns standard module globals unioned with ``__all__`` (``__init__.py:136-137``). Verified by `tests/test_lingtai_facade.py` and `tests/test_kernel_isolation.py`.
