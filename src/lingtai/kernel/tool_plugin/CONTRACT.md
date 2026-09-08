---
name: declared-host-tool-plugin
contract_version: 5
root_contract: CONTRACT.md
related_files:
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/BEHAVIORS.md
  - src/lingtai/kernel/tool_plugin/__init__.py
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/kernel/base_agent/tools.py
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/kernel/base_agent/CONTRACT.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/mcp/__init__.py
  - src/lingtai/tools/mcp/skills/mcp-manual/SKILL.md
  - src/lingtai/tools/avatar/__init__.py
  - src/lingtai/tools/avatar/manual/SKILL.md
  - src/lingtai/tools/context/__init__.py
  - src/lingtai/tools/context/manual/SKILL.md
  - src/lingtai/tools/daemon/__init__.py
  - src/lingtai/services/ANATOMY.md
  - src/lingtai/services/daemon.py
  - tests/test_cli_daemon.py
  - src/lingtai/tools/daemon/execution_host.py
  - src/lingtai/tools/daemon/shell_prompt_events.py
  - tests/test_daemon_shell_prompt_events.py
  - src/lingtai/tools/daemon/manual/SKILL.md
  - src/lingtai/tools/email/__init__.py
  - src/lingtai/tools/email/manual/SKILL.md
  - src/lingtai/tools/file/__init__.py
  - src/lingtai/tools/file/manual/SKILL.md
  - src/lingtai/tools/plugin/__init__.py
  - src/lingtai/tools/plugin/manual/SKILL.md
  - src/lingtai/tools/notification/ANATOMY.md
  - src/lingtai/tools/notification/CONTRACT.md
  - src/lingtai/tools/notification/__init__.py
  - src/lingtai/tools/notification/manual/SKILL.md
  - src/lingtai/kernel/notifications.py
  - src/lingtai/tools/bash/__init__.py
  - src/lingtai/tools/bash/_tool_family.py
  - src/lingtai/tools/bash/ANATOMY.md
  - src/lingtai/tools/bash/CONTRACT.md
  - src/lingtai/tools/bash/manual/SKILL.md
  - src/lingtai/tools/soul/__init__.py
  - src/lingtai/tools/soul/CONTRACT.md
  - src/lingtai/tools/soul/manual/SKILL.md
  - src/lingtai/tools/system/__init__.py
  - src/lingtai/tools/system/CONTRACT.md
  - src/lingtai/tools/system/plugin.py
  - src/lingtai/tools/system/karma.py
  - src/lingtai/intrinsic_skills/system-manual/SKILL.md
  - src/lingtai/tools/task_card/__init__.py
  - src/lingtai/tools/task_card/CONTRACT.md
  - src/lingtai/tools/task_card/manual/SKILL.md
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/CONTRACT.md
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/web_search/__init__.py
  - src/lingtai/tools/web_search/CONTRACT.md
  - src/lingtai/tools/web_search/manual/SKILL.md
  - tests/test_web_settings_action.py
  - src/lingtai/agent.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_tool_settings_contract.py
  - tests/test_tool_family_avatar_migration.py
  - tests/test_context_declared_tool_plugin.py
  - tests/test_daemon.py
  - tests/test_email_official_tool_plugin.py
  - tests/test_file_tool_plugin_package.py
  - tests/test_notification_settings.py
  - tests/test_notification_delay_alarm.py
  - tests/test_notification_store.py
  - tests/test_shell_tool_plugin_declaration.py
  - tests/test_system_declared_plugin.py
  - tests/test_task_card_controller.py
  - tests/test_task_card_notifications.py
  - tests/test_tool_family_vision_migration.py
  - tests/test_web_official_plugin.py
  - tests/test_web_composition_port.py
  - tests/test_intrinsic_manual_actions.py
maintenance: |
  This component contract is governed by the root CONTRACT.md and owns the
  declared host-plugin contract every official model-facing tool family follows.
  Keep related_files complete and repo-relative: the paired ANATOMY.md and
  BEHAVIORS.md, the Port module, the production Adapter
  (src/lingtai/adapters/tool_plugin_host.py), the host mount seam
  (src/lingtai/kernel/base_agent/tools.py), the owning LTP contract
  (src/lingtai/tools/CONTRACT.md), declared slices and their manuals, the
  Composition Root, and the contract tests. OFFICIAL_TOOL_PLUGIN_NAMES is
  normative: adding, removing, or renaming a reserved official name is a change
  to this contract and must move the list, this file, BEHAVIORS.md, and
  tests/test_tool_plugin_declaration.py together. Ports are earned by real
  slices (root CONTRACT.md rules 10-11) — add a host port only with the family
  that consumes it, and never widen GRANTABLE_HOST_PORTS to a whole-Agent
  argument or to tool_mount. Update the Port, affected Adapters, contract tests,
  and this contract in the same change; update the paired Anatomy when structure
  changes; bump contract_version for a breaking Port-contract change. Follow the
  root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or
  auto-fix the rule here.
---
# Declared Host Tool Plugin Contract

## Purpose
Guarded by: [TP001](BEHAVIORS.md#behavior-tp001), [TP002](BEHAVIORS.md#behavior-tp002)

This component is the kernel's boundary for **one declared official**
model-facing tool plugin. Every official tool family in this distribution
follows one declared plugin contract: a static declaration of its identity and
public actions, a bind step against a least-privilege host facade, and a
kernel-owned registrar that reserves official names and refuses a conflict
before anything is bound or mounted.

It owns exactly four things:

1. `ToolPluginDeclaration` — the static declaration shape and its
   construction-time validation.
2. The kernel host Ports (`WorkdirPort`, `PromptSectionPort`, `FileIOPort`,
   `AvatarParentPort`, `ContextRuntimePort`, `DaemonRuntimePort`,
   read-only `PluginCatalogPort`, Shell's `NotificationPort` and
   `ConfigurationPort`, Task Card's `ShutdownPort`, `TaskCardLifecyclePort`,
   and closed operation-native `TaskCardNotificationsPort`, Vision's
   read-through `ActiveProviderPort`, Web's narrow read-only
   `ProviderIdentityPort`, `ToolMountPort`),
   File's structural match/traversal result Protocols, and the two
   family-owned grant names Email's `EmailRuntimePort` (`email_runtime`) and
   Web's `WebCompositionPort` (`web_runtime`), through which a plugin receives
   only its capability-native view of the live Agent body, plus the
   `ToolPluginHost` facade that grants a declaration exactly the ports it named.
   `NotificationStatePort`, `ToolMountPort`) and Email's family-owned
   `EmailRuntimePort`, through which a plugin controls the live Agent body, and
   the `ToolPluginHost` facade
   that grants a declaration exactly the ports it named.
3. `OFFICIAL_TOOL_PLUGIN_NAMES` — the auditable, static, kernel-owned reserved
   list of official plugin names.
4. `register_official_tool_plugins` — the fail-fast registrar and its ordering
   promise.

It owns none of the following, and adding any of them here is a defect:
declaration *content* for any family; a module path, import, or behavioral
knowledge of a concrete family; filesystem, entry-point, or manifest discovery;
a wrapper runtime, universal MCP server compiler, or plugin-admission engine;
LTP envelope or JSON-schema composition (owned by
[`src/lingtai/tools/CONTRACT.md`](../../tools/CONTRACT.md) and
`lingtai.tools.tool_family`); transport, process, or connection lifecycle; and
third-party MCP or Agent Plugins v1.0.0 semantics, which are untouched.

**Host-local versus external implementation is not a product category.** A
declaration says what an official family *is*; whether its implementation runs
in this process, behind a stdio MCP server, in a spawned peer process (Avatar),
or over a channel (Telegram) is a transport/launcher choice made by an adapter
at the boundary where that technology actually varies (root `CONTRACT.md`
rule 8). Curated MCP server packages
(`src/lingtai/mcp_servers/_plugin.py`) and `src/lingtai/mcp_catalog.json`
therefore remain valid, unchanged, external-transport concerns — one adapter
form over a declaration, never the required form of every official tool.

## Behavior

Coding agents and LingTai agents MUST observe the following.

- **Declare statically.** A family's declaration is a module-level
  `ToolPluginDeclaration` constructed at import, before any `Agent` exists. It
  MUST NOT be built from a scanned directory, an entry point, a manifest file,
  or any runtime lookup. `registry.py` remains a hand-edited static table and
  gains no plugin packaging.
- **Never take the Agent.** A declaration's `binder` receives a
  `ToolPluginHost` and nothing else. Reaching around a granted port into an
  adapter's private attributes, or accepting an unguarded whole-`Agent`
  argument, is a contract violation.
- **Require only what you consume.** `requires` names ports the family actually
  uses in its own slice. Do not add a port speculatively, and do not add one
  without the real family that consumes it (root `CONTRACT.md` rules 10-11).
- **Do not self-register.** Binding composes and validates. Activation and
  mounting are the registrar's steps, in that order, and `tool_mount` is never
  grantable to a declaration.
- **Do not claim blanket conformance.** A family conforms only once its own
  vertical slice lands with its own evidence. Today `mcp`, `avatar`, `context`,
  `daemon`, `email`, `file`, `plugin`, `notification`, `shell`, `soul`,
  `system`, `task_card`, `vision`, and `web` are declared, in that official
  order; the former later-family target register is now empty. Task Card is a
  channel-neutral intrinsic dynamic capability: its one `TaskCardManager` is
  retained on the current Agent through `TaskCardLifecyclePort` and rebound on
  every refresh, and its persisted watch resumes only after a successful bind.
  Vision is a channel-neutral dynamic capability whose public surface is
  exactly `analyze | check | list | manual`: it binds `workdir`, the live
  read-through `active_provider`, and one `configuration` snapshot; default
  routing uses only the active provider, an explicitly allowed preset resolves
  only that preset's own route/credential for the one requested `check`/
  `analyze` call, and no provider/credential/MCP fallback is ever automatic
  (see `src/lingtai/tools/vision/CONTRACT.md`).
  Web is the unified search/browse capability whose public surface is exactly
  `search | browse | settings | manual`: its settings child is the generic
  read-only SHOW seam bound to Web-owned rows, and it binds `workdir`, its
  Web-owned typed
  `web_runtime` composition value (browser transport, immutable engine specs,
  and default provenance, composed by its own `setup` and granted to the `web`
  declaration alone through `extra_ports_for`), and the narrow read-only
  `provider_identity` label that gates its explicit Anthropic/Gemini opt-in;
  its bind fails closed on a missing or mistyped `web_runtime`, and no
  provider/browser fallback is ever automatic beyond the family's one
  documented OpenAI→DuckDuckGo runtime fallback (see
  `src/lingtai/tools/web_search/CONTRACT.md`).
  Notification is a mandatory injected official
  family: its declaration remains mounted once through the existing official
  boot route on construction and refresh even when its capability is null or
  listed in `disable`.
- **Fail the boot, do not skip the capability.** Every error in this component
  descends from `ToolPluginError`, which is deliberately **not** a `ValueError`
  subclass. The Composition Root's capability loop
  (`src/lingtai/agent.py`, `__init__` and `_setup_from_init`) catches
  `(ValueError, ImportError, TypeError)` around `_setup_capability` and
  downgrades it to a `capability_skipped` log line, so a `ValueError`-based
  hierarchy would turn a violated official-name reservation into an agent that
  boots successfully with the official tool silently missing. Skipping a
  capability is signalled by returning `CAPABILITY_UNAVAILABLE`
  (`src/lingtai/tools/registry.py`), never by raising from here. Re-basing
  these errors on `ValueError`, or catching them in the boot loop, is a defect.
- **Report, do not normalize.** If an implementation and this contract
  disagree, treat the disagreement as a defect and report it rather than
  weakening the promise.

## Port

Ports are capability-native and narrow (root `CONTRACT.md`
`### Capability-native interfaces`). There is deliberately no single host
interface: each port carries the smallest vocabulary that expresses one
capability.

| Port | Operation | Promise |
|---|---|---|
| `WorkdirPort` | `path -> Path` | The agent working directory, read through on every access so a holder never renders a stale directory after a refresh. Grants no read, write, listing, or lease operation. |
| `PromptSectionPort` | `write_protected_section(body) -> None` | Replace **this plugin's own** protected system-prompt section. There is no section argument and no `protected` flag: the granted port is bound to the declaring plugin's name, so a plugin can neither address another's section nor write an unprotected one. |
| `FileIOPort` | `read`, `write`, `glob`, `grep`, `last_traversal`, `max_result_chars` | File-only bounded UTF-8 text operations and concrete match/traversal facts. It exposes neither the backing generic service nor the Agent; path rooting remains the separate `WorkdirPort`. |
| `AvatarParentPort` | `parent_name`, `venv_path` | Avatar-only parent context: the identity placed in a newborn prompt and optional runtime location inherited into its init. It grants no mutable admin/configuration surface or Agent reference. Avatar owns no rules-distribution action, so this port no longer carries an authorization-bit method for one (**contract_version 4**, breaking: `has_rule_privilege()` removed). |
| `ContextRuntimePort` | `molt(args)`, `summarize(args)`, `rebuild(args)` | Context-only lifecycle-operation boundary. It preserves the live molt, record-only summary, and reconstruction/replay engines without granting Context the Agent or unrelated private state. |
| `DaemonRuntimePort` | named model/tool/preset-policy/notification/log operations | Daemon-only host-runtime boundary: optional inherited service and regular tool snapshots, explicit-preset requirement + authorization, preset sandbox/load, notification route, time, Task Card, logging, and resolved manager options. Agent composition authorizes through its allowlist; standalone composition requires and directly loads caller-supplied preset paths. It never grants an Agent or a mount operation. |
| `NotificationStatePort` | `dismiss(channel, *, force, reason, event_id=None, ref_id=None)`, `delay(channel, seconds)`, hook operations, `read_settings() -> tuple[int, int]`, bounded `log` | Notification-only Core delegation. `read_settings` returns the fresh effective payload cap and delay ceiling through canonical resolvers; it grants no configuration object or writer. `AgentNotificationStateAdapter` owns only callbacks bound to the live Agent; it hands the family no Agent, Store, fingerprint, producer state, generic dispatch, or mount seam. Notification Core retains dismissal authorization, stale-delivery comparison, producer guards, acknowledgement, delay/timer, hook-manifest, and logging policy. |
| `EmailRuntimePort` (Email-owned) | `handle_email(EmailRuntimeRequest) -> EmailResult` | Email-only manager boundary. The host `AgentEmailRuntimeAdapter` rejects foreign declared actions, reads the current `agent._email_manager` at call time, and invokes it once with already-normalized `{'action': request.action, **dict(request.input)}`; it neither captures `_intrinsics` nor recurses through an official handler. |
| `PluginCatalogPort` | `read_state() -> PluginCatalogState` | Return a detached read-only projection of Agent Plugins registration/discovery facts: boot snapshot, configured plugin paths, inherited skill paths, and skills availability. It cannot validate, register, prune, launch, write, or mount. |
| `PsycheSettingsPort` | `read_snapshot() -> PsycheSettingsSnapshotPort` | Return only Psyche's last completely applied immutable structural snapshot: `pad` / `pad_file` plus `base_prompt`, `covenant`, and `comment` with their file pointers. It grants no Agent, owner-source read, prompt mutation, reconstruction, or settings write. |
| `NotificationPort` | `publish_system(...) -> bool`; `publish_channel(channel, payload, ref_id=...) -> bool` | Publish an idempotent durable system event or a latest-channel payload without reaching an Agent/store. Shell uses exactly these two operations for its existing async watchdog and completion wake semantics. It is distinct from `NotificationStatePort`, which grants Notification Core's mirror/hook administration. |
| `ConfigurationPort` | `values -> Mapping[str, Any]` | Immutable copied values explicitly selected by capability setup for this one bind (Shell policy and dialect override, and Vision's `VisionConfiguration` snapshot fields, today); no Agent configuration lookup or write operation. |
| `SoulRuntimePort` | bounded self-state, consultation, cadence, and Soul-notification operations | Soul's explicit live-self vocabulary; no Agent, generic attribute escape hatch, tool mount, or unrelated capability API. |
| `SystemRuntimePort` | Read/query `admin`, `language`, `token_usage()`, `load_preset()`; act through `log()`, preset activation, `retry_failed_mcps()`, `perform_refresh()`, `resuscitate()`; sleep evidence/effects via `sleep_attention_fingerprints()`, `transition_to_asleep()`, `sleep_alarm_lock()`, `arm_sleep_alarm()` | System's bounded runtime/lifecycle vocabulary. The four sleep members are translation-only evidence/effects: the one sleep policy (fingerprint comparison, refusal/force, receipts, audit) lives in `lingtai.tools.system.karma.sleep_use_case`, never in this port or its adapter. Identity is deliberately absent. |
| `IdentityPort` | Read `name`; durably write `set_name()` and `set_nickname()` | System's separate naming vocabulary. The current name is read-only through the port; its two explicit writes may update durable identity, but cannot mutate address, workdir, or general runtime state. |
| `ShutdownPort` | `is_set() -> bool` | Observe only whether the current Agent is stopping, so a Task Card watch thread ends promptly. It grants no lifecycle transition, join, or event mutation. |
| `TaskCardLifecyclePort` | `current_manager()`, `retain_manager(manager)`, `report_resume_failure(error)` | The one current-Agent Task Card manager slot and its bounded resume diagnostic. It preserves the existing agent-stop, completed-work reminder, and Daemon `has_active_task_card_watch` hooks over the same retained manager without becoming a generic state bag. |
| `TaskCardNotificationsPort` | `publish_error(watch_id, body, code, retryable, idempotency_key, last_valid_body_at=None)`, `publish_recovered(watch_id, body, idempotency_key)`, `publish_limit(watch_id, body, idempotency_key, used, max_refreshes, last_valid_body_at=None)`, `submit_reminder(turns)`, `clear_reminder()` | Exactly the Task Card producer's established error/recovered/limit and absent-or-stale reminder operations, as five closed scalar-signature methods. There is no generic enqueue, `**kwargs`, `source`, `channel`, `priority`, or `extra` argument: the production adapter pins the `task_card.error`/`task_card.limit` sources, the `system` channel, priority, idempotency skip, and the bounded `extra` projection internally, and holds the Agent's generic publisher privately. A holder cannot publish a foreign source or address another channel (guarded by Task Card's [TK002](../../tools/task_card/BEHAVIORS.md#behavior-tk002)). |
| `ActiveProviderPort` | `service -> Any` | Read only the current active provider service, read through on every access so a refresh never leaves a stale provider identity. The consuming family (Vision today) may inspect that one service's provider/model/credential route but receives neither the Agent, its capability map, nor a generic provider/capability lookup (guarded by Vision's [VN006](../../tools/vision/BEHAVIORS.md#behavior-vn006)). |
| `ProviderIdentityPort` | `provider -> str \| None` | Read only the current canonical provider *label*, read through on every access. Narrower than `ActiveProviderPort` by design: Web consumes this one string for its explicit Anthropic/Gemini eligibility gate and receives neither the provider service, credentials, model configuration, the Agent, nor any provider registry; a non-string read is reported as `None`, never coerced. |
| `WebCompositionPort` (Web-owned) | `browser_port`, `specs`, `default_engine`, `default_source`, `legacy_fallback_from`, `publish_manager(manager)` | Web-only setup boundary behind the grant name `web_runtime`. The typed `WebComposition` value is composed by `web.setup` from the `BrowserPort` plus immutable engine specs and default provenance, granted to the `web` declaration alone through `extra_ports_for`, and never built in the standard table; the bind publishes its `WebManager` back through it exactly once. It exposes no Agent, LLM service, or credential. |
| `ToolMountPort` | `mount_tool(transaction) -> None` | Publish the registrar-created one-use transaction carrying one declaration and its exact `BoundToolPlugin` on the live model-facing tool surface. **Host-only** — it is absent from `GRANTABLE_HOST_PORTS` and is held solely by the registrar. |

`GRANTABLE_HOST_PORTS` is the closed set a declaration may name. It contains
exactly twenty-one grantable names: `workdir`, `prompt_section`, `avatar_parent`,
`context_runtime`,
`daemon_runtime`, `email_runtime`, `file_io`, `plugin_catalog`,
`psyche_settings`, `notification_state`, `notifications`, `configuration`, `soul_runtime`,
`system_runtime`, `identity`, `shutdown`, `task_card_lifecycle`,
`task_card_notifications`, `active_provider`, `web_runtime`, and
`provider_identity`: `mcp`
consumes the first two as its base reference; Avatar, Context, and Daemon
consume their respective narrow runtime ports; Email consumes `workdir` plus its
Email-owned `email_runtime`; File consumes exactly `workdir`, kernel-owned
`file_io`, and the setup-selected immutable `configuration` port carrying its
bounded factory snapshot (the sensitive sidecar value is private and redacted
before projection); and Plugin consumes `workdir`, its own
`prompt_section`, and the
read-only `plugin_catalog` projection; Psyche consumes `workdir` plus only the
read-only `psyche_settings` snapshot; Shell consumes `workdir` plus
`notifications` and `configuration` for its existing durable async execution
semantics; Soul consumes `workdir` plus its explicit `soul_runtime`
live-self operations vocabulary; System consumes `workdir` plus its
`system_runtime` lifecycle vocabulary and the durable naming `identity`
port; Task Card consumes `workdir` plus `shutdown`,
`task_card_lifecycle`, and `task_card_notifications`, built in the standard
table only for the `task_card` declaration; and Vision consumes `workdir` plus
its read-through `active_provider` (built in the standard table only for the
`vision` declaration) and the same setup-selected `configuration` port Shell
earned, carrying one `VisionConfiguration` snapshot through `extra_ports_for`
— never the Agent, and never a generic provider lookup; and Web consumes
`workdir` plus its Web-owned typed `web_runtime` composition value (granted
only by `web.setup` through `extra_ports_for`, never built in the standard
table) and the narrow read-only `provider_identity` label (built in the
standard table only for the `web` declaration). Family-specific runtime ports are
composed only for their declaration through `extra_ports` or `extra_ports_for`,
so they do not expand another declaration's grant; a port built in the standard
table, such as `avatar_parent` or `plugin_catalog`, is likewise reachable only
by a declaration that named it, because `ToolPluginHost.grant` copies exactly
`requires`. `tool_mount` remains absent. Later families must earn a named
capability-native port with implementation, adapter, declaration, and vertical
evidence rather than pre-enumerating a dispatch escape hatch.
`daemon_runtime`, `email_runtime`, and `notification_state`: `mcp` consumes the
first two as the shared-C base reference; Avatar, Context, and Daemon consume
their respective narrow runtime ports; Email consumes `workdir` plus its
Email-owned `email_runtime` boundary; and Notification consumes exactly
`workdir` plus `notification_state`. `email_runtime` is a grant name, not a
universal kernel Protocol. Its one production adapter is family-specific; the
Notification base-table adapter is still granted only to a declaration that
requires it. Neither expands a family into a generic Agent or dispatch seam.
Later families must earn a named capability-native port with implementation,
adapter, declaration, and vertical evidence rather than pre-enumerating a
dispatch escape hatch.

`ToolPluginHost` is the facade. A granted port is an attribute; anything else
raises `AttributeError` naming the missing port. The facade holds no reference
to the `Agent`. Python cannot make a live object deeply unreachable and this
contract does not pretend otherwise: the promise is about the **declared
argument surface** handed to a plugin, not about deep object-graph isolation.

## Adapters

`src/lingtai/adapters/tool_plugin_host.py` is the one production adapter set,
placed outside the kernel package so the dependency points inward
(`Adapter -> Port <- Core`). `AgentWorkdirAdapter` and
`AgentPromptSectionAdapter` translate the live
`BaseAgent` into the grantable ports, each constructed from a bound method or
one narrow read closure rather than from the agent object.
`AgentAvatarParentAdapter` supplies Avatar's identity/runtime/authorization
facts without passing the Agent through. `AgentDaemonRuntimeAdapter` supplies
Daemon's named runtime operations; its notification operation looks up the
current host route when publishing, so a replaced failing route keeps terminal
state retryable rather than reporting a stale callback as published.
`AgentEmailRuntimeAdapter` holds only a manager reader, performs the
Email-owned action check before a single flattened manager call, and reads a
replacement manager live; it never uses `_intrinsics` or a tool-handler route.
`AgentFileIOAdapter` holds only typed read/write/glob/grep callbacks plus
traversal and result-cap readers. It has no `Any`-typed File surface, generic
forwarding/dispatch, whole-Agent reference, or mount operation. File's `setup`
captures the service and executor separately before supplying the adapter only
through `extra_ports_for`.
`AgentPluginCatalogAdapter` is a read-only value projection: it holds one
registration reader and one capability reader, deep-copies the registration
snapshot on every `read_state()`, and returns a frozen `PluginCatalogState`. A
tool result mutated by a caller therefore cannot reach the Agent's snapshot or
capability configuration, and the adapter exposes no registration, prune,
launch, config-write, or mount operation.
`AgentNotificationStateAdapter` holds only Notification Core callbacks: a
`dismiss_channel(..., invoked_by="notification")` partial, delay, hook, fresh
effective-settings read, and bounded logging operations. The payload cap uses
the live Agent hook so System-v2 file precedence is preserved; the delay ceiling
uses the same live environment resolver without a logging callback, keeping SHOW
side-effect free. It does not pass the Notification declaration an Agent, Store,
producer state, fingerprint, configuration object, writer, generic handler, or
`ToolMountPort`.
`AgentNotificationAdapter` translates only the canonical system-event method
and a store reader into Shell's two durable publication operations, preserving
the pre-plugin compare-and-update semantics, while `StaticConfigurationAdapter`
carries only copied setup values and is granted through `extra_ports_for` to
the one declaration whose `setup` selected them — Shell's policy/dialect
values, or Vision's `VisionConfiguration.port_values()` snapshot.
`AgentActiveProviderAdapter` is built in the standard table only for the
`vision` declaration: it holds one read closure over `Agent.service` and
exposes nothing else — no capability map, tool surface, provider registry, or
mount authority.
`AgentProviderIdentityAdapter` is built in the standard table only for the
`web` declaration: it holds one read closure over `Agent.service.provider`
and exposes a single `provider` string-or-`None` property — no service,
credential, model, Agent, or registry. Web's `web_runtime` needs no host
adapter class: the family-owned `WebComposition` value composed by `web.setup`
*is* the port, granted to that declaration alone through `extra_ports_for`.
Daemon's host runtime continues to omit the parent `email` official surface, so
its separately accepted explicit task-scoped daemon-email MCP route is not
silently widened by Email's parent declaration.
`AgentShutdownAdapter`, `AgentTaskCardLifecycleAdapter`, and
`AgentTaskCardNotificationsAdapter` (built by `agent_task_card_ports`) serve
Task Card alone: the lifecycle adapter's closures read and replace the Agent's
retained `_task_card_manager` slot, and the notifications adapter implements
exactly the five closed port operations, holding `_enqueue_system_notification`
privately and pinning source, `channel="system"`, priority, idempotency skip,
and bounded extras itself — no generic enqueue method exists on the granted
port object. `agent_host_ports` builds one
declaration's grantable table;
`register_agent_tool_plugins` is the
composition/registrar wiring helper.

The registrar-local mount seam reaches `BaseAgent._mount_official_tool`, then
`_add_tool` at the common model-facing boundary
(`src/lingtai/kernel/base_agent/tools.py`), whose existing semantics — including
the tool-surface seal after `start()` and same-name replacement for
nonreserved tools — are unchanged by this contract. This component adds a
common-boundary rejection for reserved official names. Direct generic `add_tool`,
external stdio/HTTP catalogs, and foreign registrar declarations cannot overwrite
an existing official claim; same-name replacement for nonreserved tools remains.

The Composition Root stays `src/lingtai/agent.py`: dynamic capability `setup()`
hooks and injected official-family `boot()` hooks select when a declaration is
registered. Email is the latter: its boot creates/replaces its real manager,
then uses `extra_ports_for` to grant `email_runtime`. File remains a dynamic
capability and uses the same per-declaration seam for `file_io`. The Agent manual
installer maps File's package-owned body to the established `file-manual`
destination. This component never selects.
then uses `extra_ports_for` to grant `email_runtime`. Notification is also a
mandatory injected official family, registered through that existing route with
its static `DECLARATION` and canonical package-owned manual; capability null and
`disable` declarations do not suppress its first-construction or refresh mount.
This component never selects.

## Contract rules

1. **Declaration validity is checked at construction.** A declaration MUST have
   a non-empty `name`, `manual`, and `description`; at least one operational
   action; no duplicate action; no attempt to declare the reserved `manual`
   action; exactly one `input_schemas` entry per operational action; a callable
   `binder`; and a duplicate-free `requires` drawn only from
   `GRANTABLE_HOST_PORTS`. A violation raises `ToolPluginDeclarationError` at
   import.
2. **Reserved actions are appended, never declared.** The boolean `settings`
   opt-in adds its strict-empty schema and action immediately before `manual`;
   the existing bound-schema inventory check proves that the family exposes the
   same order, while false preserves `actions + ("manual",)`. The family still
   owns both handlers and their implementation; this component only guarantees
   the reserved slots. The
   reserved-action rule itself remains normative in
   [`src/lingtai/tools/CONTRACT.md`](../../tools/CONTRACT.md).
3. **`OFFICIAL_TOOL_PLUGIN_NAMES` is the reserved official namespace.** It is a
   static literal in this package holding names only — never a module path, an
   import, or family behavior. A declaration whose name is absent is refused
   with `UnreservedToolPluginNameError`. Adding a name is a reviewed change to
   this contract.
4. **Name conflicts fail before bind.** `register_official_tool_plugins`
   validates every name in the batch — reserved, unique within the batch, and
   not already claimed by a *different* declaration — **before** the first
   `bind()`, the first `activate()`, and the first `mount_tool()`. A conflict
   raises `DuplicateToolPluginNameError` and leaves the live tool surface and
   the claim map exactly as they were. There is no last-registration-wins path
   here and a name conflict never leaves a partially mounted batch. The mount
   callback receives only a registrar-issued, one-use transaction created after
   this declaration's successful bind. Its issuer, persistent declaration anchor,
   and exact canonical bound-result identity are checked before handler/schema
   publication; a caller-supplied ``BoundToolPlugin`` or public adapter cannot
   manufacture an official mount. The Agent claim view is read-only and claims
   are accepted only for that transaction after a successful mount, not from a
   caller-supplied name/declaration. This is a trusted-in-process Python
   provenance boundary, not an absolute defense against code that deliberately
   mutates private module or Agent state. That all-or-nothing promise is scoped to the name checks, exactly:
   registrar mounts and claims each member as it goes, so a failure raised afterwards by
   `ports_for`, `grant`, `bind`, `activate`, or `mount_tool` on member *N*
   leaves members 1..*N*-1 mounted and claimed and propagates. This component
   owns no unmount port and MUST NOT be described as transactional beyond
   names.
5. **Re-registration of the same declaration is idempotent.**
   `_setup_from_init` re-runs the whole boot on every refresh, so re-registering
   the identical declaration object for an already-claimed name re-binds and
   re-mounts without raising. A *different* declaration claiming a live name is
   the collision rule 4 refuses.
6. **`bind()` is pure composition, and it checks what it composed.** It MUST
   NOT mount, activate, start a process or server, open a connection, or write
   a prompt section. It returns a `BoundToolPlugin`, whose name is checked
   against the declaration so a family cannot bind onto a name the kernel did
   not reserve for it. A host granted to a different plugin is refused with
   `HostPortError`.

   The bound plugin's **advertised action inventory** is checked too: the
   schema it ships must advertise exactly `public_actions`, or `bind()` raises
   `ToolPluginDeclarationError`. Declared-versus-shipped agreement is therefore
   enforced on every boot, in the registrar's own path, rather than asserted
   once in a test. This is the *only* structural fact this component reads out
   of a composed schema — it composes none and validates no other part of the
   LTP envelope, which stays owned by
   [`src/lingtai/tools/CONTRACT.md`](../../tools/CONTRACT.md). The remaining
   agreement is upheld at the source: a declaring family MUST derive its
   composed tool name, its per-action `input` schemas, and its installed manual
   destination *from its own declaration* rather than restating them, so there
   is no second literal to drift.
7. **Registration is not activation.** `BoundToolPlugin.activate` is the
   plugin's explicit, separate boot-presentation step. The registrar runs it
   only after every name check passes, and immediately before mounting. It MUST
   NOT start a server, spawn a process, or open a transport.
8. **Least privilege is enforced at grant.** `ToolPluginHost.grant` raises
   `HostPortError` when a required port is missing, and grants nothing beyond
   `requires`. `tool_mount` is never grantable.
9. **Public surface preservation.** Recutting a family onto this contract MUST
   NOT change its public tool name, action inventory or spelling, per-action
   strict `input` schemas, the closed LTP root, result shapes, error
   vocabulary, authorization gates, or side effects. It is an internal
   least-privilege recut, not a new public capability.

## Contract tests

`tests/test_tool_plugin_declaration.py` is the shared primitive/slice suite;
`tests/test_deep_refresh.py` owns init reconstruction, including the opt-in Web
claim/schema/handler removal and re-add regression; and
`tests/test_tool_family_avatar_migration.py` supplies Avatar's focused declared
vertical proof; `tests/test_context_declared_tool_plugin.py` supplies Context's
focused static-declaration, restricted-runtime-port, canonical-manual, and
installer-collision proof; `tests/test_daemon.py` preserves Daemon manager
lifecycle coverage, including terminal-notification retry behavior;
`tests/test_email_official_tool_plugin.py` supplies Email's manager/port,
no-row/one-mount, and refresh-replacement proof; and
`tests/test_file_tool_plugin_package.py` supplies File's typed port/adapter,
two-port grant, one-body manual, one-mount, and packaging proof; and
`tests/test_plugin_tool.py` supplies Plugin's read-only action boundary,
protected-field projection, closed vanilla-skills namespace, and detached
catalog-state proof:

- declaration staticness and the
  `mcp`/`avatar`/`context`/`daemon`/`email`/`file`/`plugin`
  declared-versus-composed surfaces, including the official Daemon binding
  manager's live notification-route retry regression, and the standard-table
  proof that `plugin_catalog`/`avatar_parent` stay unreachable for a
  declaration that did not name them;
`tests/test_notification_delay_alarm.py` plus `tests/test_notification_store.py`
preserve Notification Core delay/timer and Store behavior:

- declaration staticness and the `mcp`/`avatar`/`context`/`daemon`/`email`/
  `notification` declared-versus-composed surfaces, including the official
  Daemon binding manager's live notification-route retry regression;
- construction-time validation, including the reserved `manual` action,
  duplicate/empty actions, schema/action agreement, and the non-grantable
  `tool_mount` port;
- least privilege — granted-port scoping, `AttributeError` on an ungranted
  port, missing-port failure, foreign-host refusal, and that neither the facade
  nor the bound plugin exposes the `Agent` on its public surface;
- fail-fast names — unreserved name, duplicate within one batch, and a second
  different declaration against a live claim, each asserting zero binds,
  activations, and mounts with an unchanged claim map;
- boot-path observability — an official-name conflict raised out of
  `Agent._setup_capability`, an unreserved name and a missing host port each
  failing a real `Agent(...)` boot instead of being absorbed as
  `capability_skipped`;
- declared-versus-shipped agreement — a plugin advertising undeclared actions
  or no action enum is refused at `bind()` with nothing mounted or claimed, and
  `mcp`'s manual destination and per-action `input` schemas follow its
  declaration;
- registrar ordering and issuance — `bind()` alone activates and mounts nothing;
  each successful member runs bind, activate, mount, then claim; re-registering
  the same declaration object repeats that controlled sequence; and only the
  registrar can issue the transaction carrying the exact bound result;
- name-check-only atomicity — a later host-port failure propagates after leaving
  an earlier member mounted and claimed, because no unmount capability exists;
- claim lifecycle across refresh — the deep-refresh owner proves that removing
  and re-adding opt-in Web keeps its public claim, schema, and handler surface
  synchronized.
- the live slices — boot claims and mounts exactly one `mcp`, one `vision`,
  and one `web` tool; a post-seal mount raises, a foreign declaration cannot take a live
  name, and
  neither a foreign `BoundToolPlugin` nor a directly constructed transaction
  can replace the official handler/schema/claim; the prompt-section port writes
  only this plugin's protected section;
- kernel isolation — no file under `src/lingtai/kernel/` imports
  `lingtai.tools`, with relative imports resolved so the kernel's own
  `base_agent.tools` module is not mistaken for it;
- Avatar's static declaration, local packaged-manual result, restricted port
  grant, preserved spawn/rules facts, and one live registrar mount;
- Email's static declaration, canonical package manual, one mounted schema, no
  capability/manifest manager row, null/disable parity, and a production adapter
  that observes a replaced manager at call time without intrinsic dispatch;
- File's exact `workdir`/`file_io`/`configuration` grant, typed adapters without
  Agent/generic dispatch/mount authority, unchanged five operations plus
  generic SHOW-only settings immediately before reserved manual,
  established `file-manual` runtime destination with no second `file` install,
  and one live registrar mount.
- Notification's static `DECLARATION`, exact `workdir`/`notification_state`
  grant, no-Agent/no-Store/no-writer boundary, package-owned canonical manual,
  exact two-row fresh settings projection, unchanged `check` placeholder, one
  claimed/mounted schema and handler under both capability opt-out forms on
  construction and refresh, and real Core-backed `dismiss_channel` behavior.
- Task Card's static `DECLARATION`, exact
  `workdir`/`shutdown`/`task_card_lifecycle`/`task_card_notifications` grant,
  one retained `TaskCardManager` that survives refresh and is rebound, one
  mount, the package-owned `capabilities/task_card/SKILL.md` manual, and the
  native notification boundary: the granted port object carries exactly the
  five closed operations and no generic publisher, foreign
  `source`/`channel`/`extra` arguments are refused, the manager retains only the
  family's typed view, and `tests/test_task_card_notifications.py` pins exact
  error/recovered/limit wire parity plus reminder submit/clear through the
  production adapter (`tests/test_tool_plugin_declaration.py`,
  `tests/test_task_card_controller.py`).
- Vision's static `DECLARATION`, exact
  `workdir`/`active_provider`/`configuration` grant, one mount on a real
  Agent with the `active_provider` port reading the live `Agent.service` and
  the `configuration` snapshot absent from the standard table (a bare standard
  grant fails with `HostPortError`), the package-owned
  `capabilities/vision/SKILL.md` manual, the strict controlled-host manual
  proof in `tests/test_intrinsic_manual_actions.py`, and the family-local
  provider/preset/credential boundaries (VN001–VN006:
  `tests/test_tool_family_vision_migration.py`,
  `tests/test_vision_capability.py`, `tests/test_inherit_fallback.py`).
- Web's static `DECLARATION`, exact
  `workdir`/`web_runtime`/`provider_identity` grant and exact
  `search | browse | settings | manual` surface, one mount on a real Agent
  with the
  `provider_identity` port reading the live `Agent.service.provider` label
  (and exposing only that), the typed `web_runtime` composition absent from
  the standard table (a bare standard grant fails with `HostPortError`, and a
  bind with a missing, legacy-carrier, or mistyped `web_runtime` fails closed
  with `HostPortError`), one manager published back through the composition
  exactly once, idempotent refresh re-claim, the package-owned
  `capabilities/web/SKILL.md` manual with strict zero-input manual/settings
  behavior, the
  strict controlled-host manual proof in
  `tests/test_intrinsic_manual_actions.py`, and the family-local provider
  gate/spill/isolation/settings suites (`tests/test_web_official_plugin.py`,
  `tests/test_web_composition_port.py`,
  `tests/test_web_canonical_provider_routing.py`,
  `tests/test_web_output_spill.py`, `tests/test_web_search_capability.py`,
  `tests/test_unified_web_capability.py`, `tests/test_web_settings_action.py`).

Also decisive for a change here:
`tests/test_mcp_capability.py`, `tests/test_tool_family_mcp_migration_parity.py`,
`tests/test_mcp_identity_discovery.py` (the slice's unchanged public behavior),
`tests/test_curated_mcp_plugin_package.py` (the external transport route is
undisturbed), and `tests/test_architecture_documents.py`.

## Maintenance

See the `maintenance` frontmatter above. The paired
[`ANATOMY.md`](ANATOMY.md) owns where the code lives and how it composes;
[`BEHAVIORS.md`](BEHAVIORS.md) owns the agent-executable proof of the clauses
above. Change one, re-check the other two.


## Shell NotificationPort destination selection

`NotificationPort` remains Shell's exact two-operation grant; it is not a
requirement that every adapter be a live Agent notification store. Normal Agent
composition supplies the established Agent adapter. Only the private
`bash._setup_detached_daemon_shell()` composition invoked by
`DetachedDaemonExecutionHost` can override the already-declared `notifications`
value with a run-local adapter; normal public `bash.setup()` and manifest
capability configuration have no such arguments. No new `requires` entry,
generic callback/runtime, parent-store capability, or notification-state grant
is implied. The selected adapter must return `False` when its durable enqueue
fails so Shell's private live retry can retain its publication transition.
