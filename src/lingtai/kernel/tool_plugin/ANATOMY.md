---
related_files:
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/kernel/tool_plugin/BEHAVIORS.md
  - src/lingtai/kernel/tool_plugin/__init__.py
  - src/lingtai/kernel/ANATOMY.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/kernel/base_agent/tools.py
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/mcp/ANATOMY.md
  - src/lingtai/tools/mcp/__init__.py
  - src/lingtai/tools/mcp/skills/mcp-manual/SKILL.md
  - src/lingtai/tools/avatar/ANATOMY.md
  - src/lingtai/tools/avatar/__init__.py
  - src/lingtai/tools/avatar/manual/SKILL.md
  - src/lingtai/tools/context/ANATOMY.md
  - src/lingtai/tools/context/__init__.py
  - src/lingtai/tools/context/manual/SKILL.md
  - src/lingtai/tools/daemon/ANATOMY.md
  - src/lingtai/tools/daemon/__init__.py
  - src/lingtai/services/ANATOMY.md
  - src/lingtai/services/daemon.py
  - tests/test_cli_daemon.py
  - src/lingtai/tools/daemon/execution_host.py
  - src/lingtai/tools/daemon/shell_prompt_events.py
  - tests/test_daemon_shell_prompt_events.py
  - src/lingtai/tools/daemon/manual/SKILL.md
  - src/lingtai/tools/email/ANATOMY.md
  - src/lingtai/tools/email/__init__.py
  - src/lingtai/tools/email/manual/SKILL.md
  - src/lingtai/tools/file/ANATOMY.md
  - src/lingtai/tools/file/__init__.py
  - src/lingtai/tools/file/manual/SKILL.md
  - src/lingtai/tools/plugin/ANATOMY.md
  - src/lingtai/tools/plugin/__init__.py
  - src/lingtai/tools/plugin/manual/SKILL.md
  - src/lingtai/tools/psyche/ANATOMY.md
  - src/lingtai/tools/psyche/CONTRACT.md
  - src/lingtai/tools/psyche/__init__.py
  - src/lingtai/tools/psyche/settings.py
  - src/lingtai/tools/notification/ANATOMY.md
  - src/lingtai/tools/notification/CONTRACT.md
  - src/lingtai/tools/notification/__init__.py
  - src/lingtai/tools/notification/manual/SKILL.md
  - src/lingtai/tools/bash/__init__.py
  - src/lingtai/tools/bash/_tool_family.py
  - src/lingtai/tools/bash/ANATOMY.md
  - src/lingtai/tools/bash/CONTRACT.md
  - src/lingtai/tools/bash/manual/SKILL.md
  - src/lingtai/tools/soul/ANATOMY.md
  - src/lingtai/tools/soul/__init__.py
  - src/lingtai/tools/soul/manual/SKILL.md
  - src/lingtai/tools/system/ANATOMY.md
  - src/lingtai/tools/system/__init__.py
  - src/lingtai/tools/system/karma.py
  - src/lingtai/intrinsic_skills/system-manual/SKILL.md
  - src/lingtai/tools/task_card/ANATOMY.md
  - src/lingtai/tools/task_card/__init__.py
  - src/lingtai/tools/task_card/manual/SKILL.md
  - src/lingtai/tools/vision/ANATOMY.md
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/web_search/ANATOMY.md
  - src/lingtai/tools/web_search/__init__.py
  - src/lingtai/tools/web_search/manual/SKILL.md
  - src/lingtai/kernel/notifications.py
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/tools/_manual.py
  - src/lingtai/agent.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_tool_settings_contract.py
  - tests/test_tool_family_avatar_migration.py
  - tests/test_context_declared_tool_plugin.py
  - tests/test_daemon.py
  - tests/test_email_official_tool_plugin.py
  - tests/test_file_tool_plugin_package.py
  - tests/test_plugin_tool.py
  - tests/test_notification_settings.py
  - tests/test_notification_delay_alarm.py
  - tests/test_notification_store.py
  - tests/test_shell_tool_plugin_declaration.py
  - tests/test_soul_runtime_port_ab.py
  - tests/test_system_declared_plugin.py
  - tests/test_task_card_controller.py
  - tests/test_task_card_notifications.py
  - tests/test_tool_family_vision_migration.py
  - tests/test_web_official_plugin.py
  - tests/test_web_composition_port.py
  - tests/test_intrinsic_manual_actions.py
maintenance: |
  Keep related_files repo-relative, duplicate-free, and linked to real files.
  Keep this component's ANATOMY.md, CONTRACT.md, and BEHAVIORS.md reciprocal and
  keep parent/child anatomy links bidirectional (src/lingtai/kernel/ANATOMY.md
  upward; src/lingtai/tools/ANATOMY.md and the MCP, Avatar, Context, Daemon,
  Email, File, Plugin, Task Card, Vision, and Web owner Anatomies across to the declaring side). Code is the
  structural source of truth: update
  upward; src/lingtai/tools/ANATOMY.md, src/lingtai/tools/mcp/ANATOMY.md, and
  src/lingtai/tools/daemon/ANATOMY.md, src/lingtai/tools/email/ANATOMY.md, and
  src/lingtai/tools/notification/ANATOMY.md across to the declaring side). Code is the structural source of truth: update
  this anatomy in the same change that moves files, symbols, connections,
  composition, or state — in particular when a host port is added, when a family
  recuts onto the declared contract, or when OFFICIAL_TOOL_PLUGIN_NAMES changes.
  Name the guarding LABT ids (TP001, TP002) beside the implementing code. Verify
  every changed citation and run the architecture-document validation before
  merge. Follow the root Anatomy/Contract pairing rule, report mismatches, and do
  not duplicate or auto-fix the rule here.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# Declared Host Tool Plugin Anatomy

Where the kernel-owned declared host-plugin primitive lives, and how each official
landed family reaches the live Agent body through a runtime-bound host bridge. Promises and normative
rules are in the paired [`CONTRACT.md`](CONTRACT.md); the agent-executable proof
is in [`BEHAVIORS.md`](BEHAVIORS.md).

## Components

- Detached selected Shell reuses the same declared `notifications` key only through `DetachedDaemonExecutionHost`'s private `bash._setup_detached_daemon_shell()` composer; the registrar's per-declaration extra-port precedence substitutes a RunDir event sink without widening `NotificationPort` or mounting a live Agent capability. Public Shell setup/manifest values cannot reach this override.
- `src/lingtai/kernel/tool_plugin/__init__.py` — the whole Core. One module,
  because the component is a shape rather than a machine:
  - constants `MANUAL_ACTION`, `GRANTABLE_HOST_PORTS`, and
    `OFFICIAL_TOOL_PLUGIN_NAMES` (the auditable reserved official namespace,
    guarded by TP002);
  - errors `ToolPluginError` and its four subclasses
    (`ToolPluginDeclarationError`, `UnreservedToolPluginNameError`,
    `DuplicateToolPluginNameError`, `HostPortError`);
  - the twenty-one kernel host/value Port Protocols `WorkdirPort`,
    `PromptSectionPort`, `FileIOPort`, `AvatarParentPort`, `ContextRuntimePort`,
    `DaemonRuntimePort` (including host-selected explicit-preset requirement and authorization operations), read-only `PluginCatalogPort` (with detached
    `PluginCatalogState`), read-only `PsycheSettingsPort` (returning the
    structural `PsycheSettingsSnapshotPort`),
    `NotificationStatePort`, Shell's narrow durable `NotificationPort` and
    setup-only `ConfigurationPort`, Soul's explicit live-self `SoulRuntimePort`,
    System's bounded lifecycle `SystemRuntimePort` and durable naming
    `IdentityPort`, Task Card's one-predicate `ShutdownPort`, current-Agent
    `TaskCardLifecyclePort`, and closed operation-native
    `TaskCardNotificationsPort` (five scalar-signature methods — `publish_error`,
    `publish_recovered`, `publish_limit`, `submit_reminder`, `clear_reminder` —
    with no generic enqueue, `**kwargs`, source, channel, or extra argument),
    Vision's read-through `ActiveProviderPort` (one `service` read of the
    current active provider; Vision also consumes Shell's `ConfigurationPort`
    for its setup snapshot), Web's narrower read-only `ProviderIdentityPort`
    (one `provider` string-or-`None` read of the current canonical provider
    label, never the service),
    and host-only `ToolMountPort`, plus File's
    structural `FileGrepMatch`/`FileTraversalStats` result Protocols;
    `email_runtime` and `web_runtime` are also grantable (twenty-one grant names
    in `GRANTABLE_HOST_PORTS`) but their Protocols remain family-owned —
    Email's `EmailRuntimePort` and Web's `WebCompositionPort`;
  - `ToolPluginHost`, the `__slots__`-based least-privilege facade, and its
    `grant()` classmethod;
  - `BoundToolPlugin`, the frozen mountable result carrying `schema`,
    `handler`, `description`, `glossary_package`, and the optional separate
    `activate` step;
  - private `_OfficialMountTransaction`, a registrar-issued one-use object
    binding one anchored declaration to the exact canonical bound plugin for the
    internal mount seam; its constructor rejects caller-supplied declaration /
    plugin pairs;
  - `ToolPluginDeclaration`, the frozen static declaration, validated in
    `__post_init__` (guarded by TP001), with `public_actions`,
    `public_input_schemas()`, and `bind()` — which checks the bound plugin's
    name *and* its advertised action inventory against the declaration, via the
    module-private `_advertised_actions` reader; its `settings` boolean only
    adds the reserved read-only action immediately before `manual`;
  - `register_official_tool_plugins`, the fail-fast registrar whose two-phase
    body — check every name, then bind/activate/mount — is the ordering promise
    (guarded by TP002).
- `src/lingtai/adapters/tool_plugin_host.py` — the production Adapter set,
  outside the kernel package. It supplies callback-only adapters for File's
  concrete operations/facts, Plugin's detached read-only catalog projection,
  and Notification Core's dismissal, delay, hook, and logging operations,
  alongside the existing MCP, Avatar, Context, Daemon, and Email adapters,
  plus `AgentPsycheSettingsAdapter` for the one read-through applied Psyche
  owner-input snapshot, and `AgentSoulRuntimeAdapter`/`agent_soul_runtime` for
  Soul's explicit
  live-self runtime operations and
  `AgentSystemRuntimeAdapter`/`AgentIdentityAdapter`/`agent_system_runtime`
  for System's lifecycle and naming vocabularies (whose sleep members are
  translation-only evidence/effects for `karma.sleep_use_case`), and
  `AgentShutdownAdapter`/`AgentTaskCardLifecycleAdapter`/
  `AgentTaskCardNotificationsAdapter` built by `agent_task_card_ports` for Task
  Card's shutdown predicate, its retained current-Agent manager slot
  (`Agent._task_card_manager`, the same object `base_agent/lifecycle.py` stops
  and the Daemon runtime's `has_active_task_card_watch` probes), and its five
  closed notification operations — the Agent's generic system-event publisher
  is held privately behind them and pinned to the established
  `task_card.error`/`task_card.limit` sources, `system` channel, priority,
  idempotency skip, and bounded `extra` projection (guarded by TK002), and
  `AgentActiveProviderAdapter` for Vision's one read-through `service` view of
  `Agent.service`, built in the standard table only for the `vision`
  declaration (Vision's `configuration` snapshot reuses
  `StaticConfigurationAdapter` through `extra_ports_for`), and
  `AgentProviderIdentityAdapter` for Web's one read-through `provider` label
  over `Agent.service.provider`, built in the standard table only for the
  `web` declaration (Web's typed `web_runtime` composition needs no host
  adapter class: `web.setup` grants its own `WebComposition` value through
  `extra_ports_for`).
  No adapter exposes an Agent or generic dispatcher; `agent_host_ports` and
  `register_agent_tool_plugins` construct the private registrar mount seam.
- `src/lingtai/tools/mcp/__init__.py` — the current base reference slice.
  Its static declaration opts into the reserved SHOW-only settings child and
  binds the per-host family and protected prompt
  section; Avatar, Context, Daemon, Email, File, Plugin, Psyche, Notification, Shell,
  Soul, System, Task Card, Vision, and Web are separately accepted vertical
  slices. The later-family target register is now empty; the reserved list is
  not an admission path.
- `src/lingtai/tools/avatar/__init__.py` — separately landed vertical evidence,
  not a C candidate claim. Its static `DECLARATION` binds `AvatarManager` to
  `workdir` plus the earned, narrow `avatar_parent` port; the local packaged
  manual stays a reserved child owned by the family, and `setup(agent)` only
  routes it through the registrar.
- `src/lingtai/tools/context/__init__.py` — current in-process vertical evidence.
  Its static `DECLARATION` preserves `molt | summarize | rebuild | manual` and
  binds only `workdir` plus the earned `context_runtime` operation port; the
  port delegates the established lifecycle engines without handing Context the
  Agent. Its package manual is the canonical runtime-installed `context-manual`.
- `src/lingtai/tools/daemon/__init__.py` is the fourth actual vertical slice.
  Its static `DECLARATION` preserves Daemon's action-separated public family and
  binds the established `DaemonManager`/dispatcher only to `workdir` plus the
  earned `daemon_runtime` port; no manager holds an Agent reference.
- `src/lingtai/tools/email/__init__.py` is the fifth accepted vertical slice.
  It owns `EmailRuntimeRequest` and `EmailRuntimePort`; its declaration consumes
  `workdir` plus `email_runtime`. `boot(agent)` first replaces the real
  `EmailManager`, then registers the declaration through `extra_ports_for` with
  `AgentEmailRuntimeAdapter(lambda: getattr(agent, "_email_manager", None))`.
  The adapter flattens an already-normalized request into one manager call and
  reads the manager at call time; Email has no dynamic `setup()` bridge.
- `src/lingtai/tools/file/__init__.py` is the sixth accepted vertical slice. Its
  static `DECLARATION` preserves `read | write | edit | glob | grep`, opts into
  generic `settings` immediately before `manual`, and binds only `workdir`, the
  earned `file_io` port, and a setup-selected `configuration` snapshot.
  `setup(agent)` captures
  the live File service and executor as separate narrow objects, builds
  `AgentFileIOAdapter`, captures the canonical File factory's bounded backend
  construction snapshot in `StaticConfigurationAdapter`, and grants both only
  through `extra_ports_for`; the adapters
  own no Agent, `Any` surface, generic dispatch, or mount authority. The package
  manual is the sole body and installs at the established `file-manual` path;
  no File settings file or writer is introduced.
- `src/lingtai/tools/plugin/__init__.py` is the seventh accepted vertical slice.
  Its static `DECLARATION` preserves the read-only `info | manual` family and
  binds only `workdir`, its own `prompt_section`, and the earned
  `plugin_catalog` port. `_reconcile(host)` reads the detached catalog state and
  scans discovery, then writes only the Plugin-owned protected prompt section:
  validated plugin skills are named there and never composed into the vanilla
  skills catalog. The port carries no registration, prune, launch, config-write,
  or mount authority, so registration stays the boot-only service path and
  `setup(agent)` is composition wiring through the standard port table.
- `src/lingtai/tools/notification/__init__.py` is the eighth accepted vertical
  slice. Its static `DECLARATION` preserves the LTP envelope, adds the reserved
  read-only `settings` action immediately before `manual`, and binds only
  `workdir` plus `notification_state`. The callback-only adapter delegates every
  dismissal, stale-delivery comparison, producer guard, delay, timer,
  hook-manifest, logging decision, and fresh two-scalar effective-settings read
  to the canonical owners; the family receives no Agent, Store, fingerprint,
  configuration object, writer, or local parallel state machine. Its
  package-owned `manual/SKILL.md` is the one canonical installed
  `capabilities/notification/SKILL.md` source.
- `src/lingtai/tools/bash/_tool_family.py` is the ninth accepted vertical slice,
  the Shell declaring family over the retained implementation package. Its static
  `DECLARATION` derives the existing three action schemas and packaged manual
  destination, binds the retained `ShellManager` through only
  `workdir`/`notifications`/`configuration`, and returns a `BoundToolPlugin`
  whose activation resumes the unchanged durable async state.
  `AgentNotificationAdapter` holds only the canonical system-event method and a
  store reader and preserves the pre-plugin compare-and-update semantics, while
  `StaticConfigurationAdapter` carries only copied setup values.
  `bash/__init__.py` `setup(agent, ...)` only supplies that configuration through
  `extra_ports_for` and calls the registrar.
- `src/lingtai/tools/soul/__init__.py` is the tenth accepted vertical slice.
  Its static `DECLARATION` preserves the public
  `inquiry | flow | config | voice | dismiss | manual` family and binds the
  five operational children only to the earned, explicit `soul_runtime` port;
  the reserved manual child receives only the granted `workdir`.
  `AgentSoulRuntimeAdapter` stores individual read closures and bound
  operations — never the Agent — for Soul's real conversation, cadence,
  consultation-lock, and notification semantics. Soul stays an injected
  intrinsic for kernel lifecycle hooks while its model-facing root mounts only
  through the registrar; the package manual is the sole operational body,
  installed at the historical `soul-manual` destination.
- `src/lingtai/tools/system/__init__.py` is the eleventh accepted vertical
  slice. Its static `DECLARATION` preserves the public
  `refresh | sleep | lull | interrupt | suspend | cpr | clear | nirvana |
  presets | name_set | name_nickname | manual` family (no public `summarize`)
  and binds the retained handlers through the private `_SystemHandlerHost`
  bridge to exactly `workdir`, `system_runtime`, and `identity`. The one
  self-sleep policy — fingerprint comparison, refusal/force, receipts, audit
  events, the one-shot `delay` alarm ordering, and the ASLEEP transition —
  lives in `karma.sleep_use_case` over the `SystemSleepPort` vocabulary; the
  mounted route reaches it through the granted `SystemRuntimePort` and the
  direct `handle(agent, ...)` route through the translation-only
  `_DirectSleepPort`. `system` boots as an `official_plugin` intrinsic:
  `BaseAgent._boot_official_intrinsics` calls the module `boot`, which
  registers the declaration through the controlled registrar on construction
  and on every refresh. The canonical operational manual remains the installed
  `system-manual` router bundle.
- `src/lingtai/tools/task_card/__init__.py` is the twelfth accepted vertical
  slice, the channel-neutral intrinsic Task Card producer. Its static
  `DECLARATION` preserves the public
  `start | inspect | retry | stop | remove | manual` family and binds
  `TaskCardManager` to exactly `workdir`, `shutdown`, `task_card_lifecycle`,
  and `task_card_notifications`. `_bind` reuses and rebinds the one manager the
  lifecycle port already retains for this current Agent (or retains a new one),
  so one `TaskCardManager` survives refresh; `activate` resumes the persisted
  `taskcard/watch.json` watch only after that successful bind. The manager
  keeps a manager-only `_TaskCardRuntime` (workdir, shutdown predicate, and the
  family-local `TaskCardNotificationsAdapter`), never a host, Agent, generic
  publisher, or service locator; that adapter consumes only the five native
  notification operations and refuses a port that offers a generic publisher.
  Task Card knows nothing of Telegram/Feishu/transport: it writes only the
  `taskcard/status` + `taskcard/taskcard.md` artifact that transports project.
  `setup(agent)` is composition wiring through the registrar; the package
  manual installs at `capabilities/task_card/SKILL.md`.
- `src/lingtai/tools/vision/__init__.py` is the thirteenth accepted vertical
  slice, the channel-neutral image-understanding family. Its static
  `DECLARATION` owns the public `analyze | check | list | manual` family (one
  strict input schema per action) and binds `VisionManager` to exactly
  `workdir`, the live read-through `active_provider`, and one `configuration`
  snapshot. `_bind` rebuilds the immutable `VisionConfiguration` from the
  granted port's copied mapping (`VisionConfiguration.from_port_values`),
  resolves the default route from the active provider only, and returns the
  mountable manager; `setup(agent, ...)` hands
  `StaticConfigurationAdapter(VisionConfiguration(...).port_values())` to the
  `vision` declaration alone through `extra_ports_for` and calls the
  registrar. `check` constructs/resolves the selected route without any image
  or provider request, `list` enumerates only authorized preset declarations,
  `manual` reads only the installed package manual, and an explicitly allowed
  `preset` resolves only that preset's own credential for the one requested
  call — never switching the active preset, borrowing active secrets, or
  falling back to another provider/MCP. The package manual installs at
  `capabilities/vision/SKILL.md`.
- `src/lingtai/tools/web_search/__init__.py` is the fourteenth accepted
  vertical slice, the unified `web` search/browse family. Its static
  `DECLARATION` owns the public `search | browse | settings | manual` family
  (two strict operational schemas derived from the family's one
  `_CHILD_SPECS` source, plus generic settings opt-in) and binds `WebManager`
  to exactly `workdir`, the Web-owned typed
  `web_runtime` composition value, and the narrow read-only
  `provider_identity` label. `setup(agent, ...)` keeps the existing lazy
  engine/browser composition, folds the `BrowserPort` plus immutable
  `_EngineSpec` set and default provenance into one `WebComposition`, grants
  it to the `web` declaration alone through `extra_ports_for`, and returns the
  manager the bind published back through `WebComposition.publish_manager`
  (exactly once). `_bind` fails closed with `HostPortError` unless
  `host.web_runtime` is granted and is a typed `WebComposition` — there is no
  fallback carrier, default transport, or default engine set at bind. The
  manager retains only the granted workdir and provider-identity ports;
  `_same_provider_identity` compares the port's label exactly for the explicit
  Anthropic/Gemini opt-in. The package manual installs at
  `capabilities/web/SKILL.md`.

## Connections

- `lingtai.tools.mcp`, `lingtai.tools.avatar`, `lingtai.tools.context`,
  `lingtai.tools.daemon`, `lingtai.tools.email`, `lingtai.tools.file`,
  `lingtai.tools.plugin`, and `lingtai.tools.notification` import
  `lingtai.kernel.tool_plugin` (declarations depend on the shape). The kernel
  `lingtai.tools.daemon`, `lingtai.tools.email`, and
  `lingtai.tools.notification` import `lingtai.kernel.tool_plugin`
  (declarations depend on the shape). The kernel
  imports nothing from `lingtai.tools`; that edge is
  swept by `tests/test_tool_plugin_declaration.py`.
- `lingtai.tools.bash._tool_family` imports `lingtai.kernel.tool_plugin`; its
  lazy `_bind` sees a `ToolPluginHost`, never an Agent. The retained Shell
  manager receives only the granted workdir/notification ports, while copied
  setup values arrive through `ConfigurationPort`.
- `lingtai.tools.soul` imports `lingtai.kernel.tool_plugin`; its `_bind` sees a
  `ToolPluginHost`, never an Agent, and its five operational children receive
  only the granted `SoulRuntimePort`. `Agent` re-mounts the declaration through
  `override_intrinsic("soul")` plus `soul.setup(agent)` on construction and on
  every refresh.
- `lingtai.tools.system` imports `lingtai.kernel.tool_plugin`; its `_bind` sees
  a `ToolPluginHost`, never an Agent, and its handlers reach the body only
  through the granted `system_runtime`/`identity` ports (plus `workdir` for the
  manual child). The mount is the official-intrinsic boot route: the registry
  marks `system` with `official_plugin`, so `_boot_official_intrinsics` runs
  `system.boot(agent)` on construction and on every refresh.
- `lingtai.tools.task_card` imports `lingtai.kernel.tool_plugin`; its `_bind`
  sees a `ToolPluginHost`, never an Agent. The lifecycle port's closures read
  and replace `Agent._task_card_manager`, which is why the existing
  `base_agent/lifecycle.py` agent-stop hook, `turn.py` completed-work reminder
  hook, and Daemon's `has_active_task_card_watch` probe keep operating on the
  same retained manager across refresh; the notification port's adapter holds
  `Agent._enqueue_system_notification` privately and never exposes it.
- `lingtai.tools.vision` imports `lingtai.kernel.tool_plugin`; its `_bind`
  sees a `ToolPluginHost`, never an Agent. The `active_provider` adapter's one
  closure reads `Agent.service` on every access, so a refreshed provider is
  seen without a stale identity; the `configuration` port is the same
  `StaticConfigurationAdapter` seam Shell uses, granted only to `vision`.
- `lingtai.tools.web_search` imports `lingtai.kernel.tool_plugin`; its `_bind`
  sees a `ToolPluginHost`, never an Agent. The `provider_identity` adapter's
  one closure reads `Agent.service.provider` on every access; the
  `web_runtime` port is the family's own `WebComposition`, granted only to
  `web` through `extra_ports_for` from `web.setup`, and the bound manager is
  published back through that same value so `setup` still returns it.
- `lingtai.adapters.tool_plugin_host` imports `lingtai.kernel.tool_plugin`
  (`Adapter -> Port <- Core`) and reaches the Agent only through the public
  `working_dir`, `update_system_prompt`, and the read-only
  `official_tool_plugins` surface. The last is the live claim view on
  `BaseAgent`; a persistent declaration anchor and canonical bound-result map
  remain separate from that view. Composition changes claims only through the
  registrar-issued mounted transaction callback; clearing the live backing map
  cannot authorize a different declaration.
- The registrar-local transaction mount calls `BaseAgent._mount_official_tool`
  (`src/lingtai/kernel/base_agent/__init__.py`), which verifies the issuer,
  anchored declaration, and exact bound-result identity before delegating to
  `_add_tool` in `src/lingtai/kernel/base_agent/tools.py`. That common boundary
  performs the reserved-name guard; its seal check and same-name replacement
  for nonreserved tools remain unchanged. This is trusted-in-process Python
  provenance, not an absolute defense against deliberate private-state mutation.
- `lingtai.tools.mcp.setup()`, `lingtai.tools.avatar.setup()`,
  `lingtai.tools.context.setup()`, `lingtai.tools.daemon.setup()`,
  `lingtai.tools.file.setup()`, `lingtai.tools.plugin.setup()`,
  `lingtai.tools.task_card.setup()`, `lingtai.tools.vision.setup()`, and
  `lingtai.tools.web_search.setup()` call
  `lingtai.adapters.tool_plugin_host.register_agent_tool_plugins` through the
  ordinary capability boot loop. Daemon and File add their capability-native
  ports through `extra_ports_for`, Vision adds its setup-selected
  `configuration` snapshot the same way, and Web adds its typed
  `web_runtime` composition the same way; Plugin needs no factory because its
  read-only `plugin_catalog` projection is built in the standard table and is
  still granted only to a declaration that names it; Task Card's three ports
  are likewise built in the standard table (`agent_task_card_ports`) only for
  the `task_card` declaration. Email instead is an injected
  `official_plugin`: `BaseAgent._boot_official_intrinsics()` calls `email.boot`,
  which creates its manager before registering the declaration with its sole
  family-specific `email_runtime` grant.
  ordinary capability boot loop. Email and Notification instead are injected
  `official_plugin` families: `BaseAgent._boot_official_intrinsics()` calls
  Email's `boot` hook and Notification's `setup(agent)` wiring. Email creates
  its manager before registering with its sole family-specific `email_runtime`
  grant; Notification registers its static declaration with only `workdir` and
  `notification_state`. That port's settings read keeps the live Agent/System-v2
  hook available without exposing the Agent or System's file grammar.
  Notification remains always-on even when a capability manifest names it null
  or lists it in `disable`.
- `_build_family(host)` passes only `host.workdir` to
  `lingtai.tools.tool_family.manual.build_manual_child`, which reads the
  installed manual through `src/lingtai/tools/_manual.py`. That loader accepts
  the live Agent (private `_working_dir`) or a `WorkdirPort` (`path`), so
  migrated and unmigrated families share one loader. File keeps a local manual
  child that reads the same installed body at `file-manual`; the installer maps
  the package body to that destination, so the runtime has one body and one
  destination.

## Composition

`import lingtai.tools.mcp`, `import lingtai.tools.avatar`,
`import lingtai.tools.context`, `import lingtai.tools.daemon`,
`import lingtai.tools.email`, `import lingtai.tools.file`,
`import lingtai.tools.plugin`, `import lingtai.tools.task_card`,
`import lingtai.tools.vision`, or `import lingtai.tools.web_search` →
`import lingtai.tools.email`, or `import lingtai.tools.notification` →
`ToolPluginDeclaration.__post_init__` validates
its declared shape, with no Agent in existence.

Direct `Agent.__init__` composes constructor-time official boot normally. The
wrapper-only `cli.build_agent` passes private `_from_init_boot=True` to make an
internal shell, so `Agent._boot_official_intrinsics()` defers that initial boot;
the constructor self-disarms the shell and CLI clears the sentinel again before
the one configured `Agent._setup_from_init()` pass performs final boot.

Dynamic-family boot remains `Agent.__init__` / `Agent._setup_from_init` →
`_setup_capability(name)` → that family's `setup(agent)` →
`register_agent_tool_plugins(agent, [DECLARATION])`. Email's mandatory official
boot is `BaseAgent._boot_official_intrinsics()` → `email.boot(agent)` → create or
replace `EmailManager` → `register_agent_tool_plugins(..., extra_ports_for=...)`.
Notification is likewise a mandatory injected official family: the same existing
official-intrinsic route runs its `setup(agent)` on first construction and every
refresh, registering exactly its static declaration even when `notification` is
null or disabled in the capability manifest. Both routes reach
`register_official_tool_plugins`, which then runs, in order:

1. check every declared name against `OFFICIAL_TOOL_PLUGIN_NAMES`, the batch,
   and the live claim map;
2. build `agent_host_ports(agent, name, family_specific_ports)`, then
   `ToolPluginHost.grant` only the declaration's ordered `requires` subset;
3. `declaration.bind(host)` → the family's `_bind` composes its real surface,
   deriving the tool name, the per-action `input` schemas, and the installed
   manual's destination from `DECLARATION` itself; `bind()` then refuses a
   bound plugin whose advertised action enum is not `public_actions`;
4. `bound.activate()` → the family's optional `_reconcile(host)` writes only
   that family's own protected prompt section, because
   `AgentPromptSectionAdapter` is bound to the declaring plugin's name at grant
   time (`mcp` for MCP, `plugin` for Plugin); a family that declares no
   `activate` writes nothing;
5. record the exact bound result, issue a registrar-only one-use transaction,
   and call `agent._mount_official_tool(transaction)`; the Agent verifies the
   persistent declaration anchor and canonical bound identity before `_add_tool`
   performs the final common-boundary reserved-name guard;
6. record the claim through `agent._claim_official_tool(transaction)` only after
   the mount marks that issued transaction successful.

Step 1 completes for the whole batch before step 2 begins for any member, so a
*name conflict* never leaves a partially mounted surface. Steps 2-6 run per
member, in order, and are not transactional: a failure raised by `grant`,
`bind`, `activate`, or `mount_tool` on member N leaves members 1..N-1 mounted
and claimed and propagates. Rolling those back would need an unmount port this
component does not own.

## State

- `OFFICIAL_TOOL_PLUGIN_NAMES` — module-level immutable tuple; the reserved
  official namespace.
- `BaseAgent._official_tool_plugins` — per-agent `dict[str,
  ToolPluginDeclaration]`, initialized in `BaseAgent.__init__`
  (`src/lingtai/kernel/base_agent/__init__.py`) beside `_tool_handlers` /
  `_tool_schemas` and read publicly through the `official_tool_plugins`
  property. The live official namespace is written only by the registrar's
  mounted-transaction claim seam. Persistent
  `_official_tool_declarations` anchors and live
  `_official_tool_bindings` are separate: refresh clears the latter with the
  tool surface but not the former, so clearing the claim map cannot admit a
  foreign declaration. A dynamic capability dropped on refresh leaves no live
  claim; surviving dynamic families re-register their declaration idempotently.
  Email and Notification are mandatory injected official families. Email's
  refresh boot replaces its manager; Notification's reclaims one live static
  binding with only `workdir`/`notification_state`; every settings call resolves
  fresh through that port and retains no snapshot. Neither family is suppressed
  by a null capability declaration or `disable` entry.
- `ToolPluginHost._ports` — the granted subset, fixed at grant time.
- No other state. The component keeps no cache, no registry file, and no
  process handle.

## Boundaries

- Declaration *content* belongs to each family under `src/lingtai/tools/`; the
  LTP envelope and schema composition belong to
  `src/lingtai/tools/CONTRACT.md` and `src/lingtai/tools/tool_family/`.
- Which declarations are registered and when is Composition-Root work in
  `src/lingtai/agent.py`, dynamic-family `setup()`, and injected official-family
  `boot()` hooks; the core never selects.
- External transport and launcher concerns — curated MCP server packages,
  `src/lingtai/mcp_catalog.json`, `mcp_registry.jsonl`, Agent Plugins v1.0.0 —
  live outside this component and are unchanged by it.

## Extension points

- A new host port: extend `GRANTABLE_HOST_PORTS`, keep the Protocol in the
  owning family when it is family-specific (as Email does), add the production
  adapter, and land all of it with the one real family that consumes it.
- A new official family: add its name to `OFFICIAL_TOOL_PLUGIN_NAMES` (a
  reviewed contract change), build its module-level `DECLARATION`, and route
  its approved composition hook through `register_agent_tool_plugins`; do not
  infer that every official family must be a dynamic `setup()` capability.
  The fifteen actual slices are `mcp`, `avatar`, `context`, `daemon`, `email`,
  `file`, `plugin`, `psyche`, `notification`, `shell`, `soul`, `system`, `task_card`,
  `vision`, and `web`;
  Notification demonstrates an always-on injected family rather than a
  later-family target or normal opt-in capability, Task Card demonstrates a
  manager-owning dynamic family whose one manager is retained on the Agent
  through a family-specific lifecycle port and rebound on every refresh,
  Vision demonstrates a dynamic family that reads the live active provider
  through one read-through port and receives its setup input as a
  configuration snapshot rather than an Agent, and Web demonstrates a dynamic
  family whose setup composes a typed family-owned value (transport plus
  immutable engine specs) that is granted to its own declaration alone and
  publishes the bound manager back, beside one narrow read-only label port.
