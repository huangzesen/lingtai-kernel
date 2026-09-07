---
related_files:
  - src/lingtai/mcp_servers/ANATOMY.md
  - src/lingtai/tools/task_card/BEHAVIORS.md
  - src/lingtai/tools/task_card/CONTRACT.md
  - src/lingtai/tools/task_card/__init__.py
  - src/lingtai/tools/task_card/manual/SKILL.md
  - src/lingtai/tools/task_card/manual/reference/lifecycle.md
  - src/lingtai/tools/task_card/manual/reference/notifications.md
  - src/lingtai/tools/task_card/manual/reference/settings.md
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/mcp_servers/telegram/task_card/ANATOMY.md
  - src/lingtai/mcp_servers/telegram/manager.py
  - src/lingtai/mcp_servers/feishu/task_card.py
  - src/lingtai/mcp_servers/feishu/manager.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - tests/test_task_card_controller.py
  - tests/test_task_card_notifications.py
  - tests/test_tool_settings_contract.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_telegram_toolfamily_ltpv2.py
  - tests/test_telegram_task_card_programmable.py
  - tests/test_feishu_programmable_task_cards.py
  - src/lingtai/tools/task_card/glossary-en.md
  - src/lingtai/tools/task_card/glossary-wen.md
  - src/lingtai/tools/task_card/glossary-zh.md
maintenance: |
  Keep related_files repo-relative, duplicate-free, and linked to real files.
  Keep this Anatomy reciprocal with its paired CONTRACT.md and manual. Update
  this file in the same change as any ownership, file-path, lifecycle, or
  projection-boundary change.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# Intrinsic Task Card Anatomy

The intrinsic `task_card` capability owns one agent-local declarative artifact
plus its persisted agent-wide configuration, both under `<workdir>/taskcard/`,
and nothing else. It is producer-first and channel-neutral: it runs a
renderer, writes `taskcard/taskcard.md`, and writes `taskcard/status` as exact
`active` or `inactive`, reading five numeric `taskcard/taskcard.json` policies
at their existing application seams. An active watch persists a resume
descriptor at `taskcard/watch.json` so a `refresh`/molt/agent-stop can
rehydrate the same watch on the next boot; `stop` pauses a watch and preserves
the last body; `remove` is the terminal lifecycle action that also retires any
active watch and deletes the body, so a caller never needs to reach around this
capability with a filesystem delete. It does not own Telegram, Feishu, portals,
chat IDs, retry policy against a transport, or any resident message state.
It is the twelfth declared official host-plugin slice: `DECLARATION` is static
at import and `_bind` receives only `workdir`, `shutdown`,
`task_card_lifecycle`, and `task_card_notifications` ports. The granted
notification port is the kernel's closed operation-native
`TaskCardNotificationsPort` (five scalar methods, no generic publisher); the
family-local `TaskCardNotificationsAdapter` maps the producer's typed
error/recovered/limit events onto exactly those operations and refuses a port
that offers a generic enqueue. The retained manager sees that typed view
only — never a host, generic publisher, or service locator. The lifecycle port
retains the one real current-agent manager so existing stop, turn-reminder,
Daemon-probe, and restart-resume hooks keep operating across refresh (the
manager is rebound with fresh ports, not replaced); no binder receives a
whole Agent.
Normative promises live in [`CONTRACT.md`](CONTRACT.md).

## Components

- `__init__.py` — the full capability owner: static `DECLARATION` plus its
  read-only five-row settings provider,
  declaration-derived schema/description/manual family, one-watch lifecycle,
  renderer execution, atomic file writes, typed error/recovered/limit
  notifications (`TaskCardNotificationsAdapter` and its event forms), persisted
  config loading/validation (`TaskCardManager._load_config` and
  `TaskCardManager.settings_rows`), the one-way
  legacy-config migration (`TaskCardManager._migrate_legacy_config`), and the
  `setup(agent)` composition call into the official registrar.
- `manual/SKILL.md` — the progressive-disclosure manual for renderer authors
  and lifecycle use.

## Connections

- `setup(agent)` hands `DECLARATION` to
  `lingtai.adapters.tool_plugin_host.register_agent_tool_plugins`; the kernel
  reserves `task_card`, grants only the four declared ports, binds, resumes a
  persisted active watch (`TaskCardManager.resume_persisted_watch`), then mounts
  it, so the card survives `refresh`/molt/agent-stop restarts without a whole
  Agent entering the manager. At bind time the family wraps the granted native
  notification port in its typed event view before dispatch; the host-side
  `AgentTaskCardNotificationsAdapter` in `lingtai.adapters.tool_plugin_host`
  pins source/channel/priority/idempotency/extras behind those operations.
- The declaration/provider opt-in injects `settings` exactly once immediately
  before `manual`; the retained manager resolves fresh owner facts without
  creating or changing the owner document.
- `lifecycle._stop` calls `shutdown_for_agent_stop()` so a stopping agent
  writes `inactive`, joins the watch thread best-effort, and re-persists the
  watch descriptor with its carried refresh budget for the next boot.
- Telegram and Feishu are only consumers: each manager reads
  `<workdir>/taskcard/status` and `<workdir>/taskcard/taskcard.md` and projects
  them separately. The intrinsic capability never calls back into either
  messaging adapter.
- One-way only, the reverse direction: if `<workdir>/taskcard/taskcard.json`
  has never been created, `start` reads `<workdir>/telegram/taskcard.json`
  (the retired Telegram-owned controller's persisted refresh ceiling) once,
  and migrates it only when that legacy value differs from its own untouched
  default (which ordinary `/taskcard` commands persist regardless of any real
  customization). Either way — migrated or built-in — this first resolution
  writes the new intrinsic config file immediately, so the legacy path is
  never read again for this agent, even if that Telegram file changes later.

## Composition

- Parent: [`src/lingtai/tools/ANATOMY.md`](../ANATOMY.md)
- Paired contract: [`CONTRACT.md`](CONTRACT.md)
- Consumer-specific projection rules: `src/lingtai/mcp_servers/telegram/` and
  `src/lingtai/mcp_servers/feishu/task_card.py`

## State

- `<workdir>/taskcard/status` — exact `active` or `inactive`
- `<workdir>/taskcard/taskcard.md` — the full rendered body
- `<workdir>/taskcard/taskcard.json` — persisted agent-wide config
  (`interval_s`/`timeout_s`/`max_refreshes`/`reminder_turns`/
  `max_body_chars`); read at each field's existing runtime application seam,
  written by the capability only during the one-way legacy migration (never by
  a model-facing action). Settings SHOW reads or previews those same effective
  values without invoking the writer.
- `<workdir>/taskcard/watch.json` — persisted active-watch descriptor
  (`watch_id`/`renderer_path`/`interval_s`/`timeout_s`/`max_refreshes`/
  `refreshes_used`/`started_at`); written on `start` and re-written on
  agent-stop with the carried refresh budget, read by `setup` to resume the
  watch, and cleared on `stop`/`remove`/refresh exhaustion
- In-memory only: one active watch, its thread, last valid body/timestamp, and
  deduped error/limit bookkeeping (the descriptor is the only cross-process
  watch state)
- Notification operation view: producer-owned typed error/recovered/limit
  events plus `submit_reminder(turns)`/`clear_reminder()`. Source and channel
  are adapter-pinned; foreign publisher fields never enter this view.

## Notes

- Atomic ordering is the structural point of this unit: write the body fully
  before activation, update the body by atomic replace, write `inactive`
  before stopping, and — for `remove` — confirm the watch has quiesced before
  deleting the body, so the updater can never recreate a file `remove` just
  removed.
- Missing, invalid, or inactive producer state is a consumer concern. This
  intrinsic capability only writes the artifact truthfully.
- Notification policy stays in the producer; the family adapter forwards typed
  events to the five closed native operations, and the host adapter behind them
  pins the established `task_card.error`/`task_card.limit` wire forms to the
  system channel. Foreign source/channel/extra-field injection is rejected at
  both the typed event forms and the native operations.
- The legacy-config migration is a one-time bootstrap, not an integration:
  it is gated on `taskcard/taskcard.json` not yet existing (never on its
  content), so this capability never carries an ongoing runtime dependence on
  Telegram or any other consumer for its own policy.
- Settings inventory projects only those five public numeric policies.
  Renderer/workdir paths, body/status/watch contents, notification state, and
  unknown owner-document fields remain operational or sensitive state outside
  the projection.
