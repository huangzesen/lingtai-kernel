---
related_files:
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/soul/BEHAVIORS.md
  - src/lingtai/tools/soul/__init__.py
  - src/lingtai/tools/soul/config.py
  - src/lingtai/tools/soul/settings.py
  - src/lingtai/tools/soul/consultation.py
  - src/lingtai/tools/soul/flow.py
  - src/lingtai/tools/soul/inquiry.py
  - src/lingtai/tools/soul/glossary-en.md
  - src/lingtai/tools/soul/glossary-zh.md
  - src/lingtai/tools/soul/glossary-wen.md
  - src/lingtai/tools/soul/CONTRACT.md
  - src/lingtai/tools/soul/manual/SKILL.md
  - src/lingtai/tools/soul/manual/reference/flow.md
  - src/lingtai/tools/soul/manual/reference/configuration.md
  - src/lingtai/tools/soul/manual/reference/consultation.md
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/agent.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_tool_family_soul_migration.py
  - tests/test_soul_runtime_port_ab.py
  - tests/test_soul_settings.py
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/tool_family/ANATOMY.md
  - ENVIRONMENT_VARIABLES.md
maintenance: |
  Keep related_files as repo-relative paths to real files. Keep this map
  synchronized with Soul's structural composition and the paired contract.
  The root package is the only whole-Agent compatibility bridge; the five
  implementation consumers below receive SoulRuntimePort directly. Capability
  mentions require explicit related_files links to implementing code.
---
# intrinsics/soul

Soul is the official declared-host-plugin family for the agent's inner voice.
Its public root owns the LTP-v2 envelope and seven action children (`inquiry`,
`flow`, `config`, `voice`, `dismiss`, read-only `settings`, and reserved
`manual`). Soul's domain
implementation receives the least-privilege `SoulRuntimePort`; it does not
receive a whole Agent.

## Components

- `__init__.py` — declaration, family schema/dispatch, and the explicit root
  compatibility bridge. `_bind(host)` receives `host.workdir` and
  `host.soul_runtime`; `_coerce_runtime()` is the only place that adapts a
  legacy whole-Agent caller. Lifecycle and helper wrappers adapt once, then
  call the structural consumers. `handle()` and the declaration binder keep
  the public call shape and manual result compatibility.
- `config.py` — config/voice validation, prompt resolution, and atomic
  `manifest.soul` persistence. `_handle_config()` and `_handle_voice()` read
  and mutate only the granted runtime's `config`, cadence, timer, logging, and
  `working_dir` properties.
- `settings.py` — binds the generic five-field `SettingRow` projection to live
  `SoulRuntimePort` cadence/config truth and the process flow gate. It exposes
  a private-redacted prompt row and performs no mutation.
- `flow.py` — opt-in gate, IDLE timer, fire lock, consultation-fire
  orchestration, notification publication, append-only records, and appendix
  rehydration. It accesses `shutdown`, `soul_timer`, `state`, `fire_lock`, and
  notification operations through `SoulRuntimePort`.
- `inquiry.py` — one-shot mirrored conversation and human `/btw` notification
  publication. It uses runtime `chat`, `service`, `config`, and notification
  operations through the port.
- `consultation.py` — diary cue rendering, snapshot substrate loading,
  bounded consultation batches, timeout/token accounting, refusal handling, and
  synthesized flow-pair construction. Runtime access is through `chat`,
  `session`, `service`, `config`, `working_dir`, and `log` on the port.
- `manual/SKILL.md` — the local operational guide for the seven-action
  envelope, exact settings comment targets, real change procedures,
  disabled-flow/config behavior, and valid nullable input shapes.
- `tests/test_soul_runtime_port_ab.py` — focused proof that the four consumers
  accept a structural port directly and that the root bridge preserves a real
  Agent call.

## Connections

- The declaration binder creates a `SoulRuntimePort` adapter in the host
  composition layer; it never passes an Agent into `config.py`, `settings.py`,
  `flow.py`, `inquiry.py`, or `consultation.py`.
- `__init__.py` dispatches `config`/`voice` to `config.py`, `inquiry` to
  `inquiry.py`, flow lifecycle work to `flow.py`, and injects `settings.py`'s
  provider through the generic ToolFamily seam. `flow.py` imports the
  consultation batch and diary helpers; `inquiry.py` imports prompt, send,
  token, and persistence helpers from sibling modules.
- The root compatibility wrappers remain available for kernel lifecycle hooks
  and focused legacy callers. Direct implementation-module calls are
  structural-port calls and are intentionally not Agent adapters.

## Composition

- **Parent:** `src/lingtai/tools/` and its tool-family infrastructure.
- **Host boundary:** `src/lingtai/adapters/tool_plugin_host.py` implements
  `SoulRuntimePort` from narrow Agent-bound operations; `src/lingtai/agent.py`
  registers the declaration.
- **Kernel boundary:** `src/lingtai/kernel/tool_plugin/` owns the Port protocol.
  The implementation package depends inward on that vocabulary.
- **Siblings:** `notification`, `psyche`, `system`, and `email` remain separate
  capabilities; Soul only publishes/dismisses its own notification channel.

## State

- `config.py` writes `init.json` under `manifest.soul` for cadence, voice, and
  custom voice prompt.
- `settings.py` writes no state; it reads the process flow gate and the live
  cadence/voice values already owned by the runtime and config action.
- `flow.py` appends `logs/soul_flow.jsonl` and may publish/clear the `soul`
  notification through the port. `inquiry.py` persists inquiry entries through
  the same Soul persistence operation and may publish `btw` for human source.
- `history/snapshots/` is read as consultation substrate; no snapshot is
  created by Soul.
- `SoulRuntimePort.fire_lock`, `shutdown`, `idle_event`, and `soul_timer` are
  ephemeral runtime state exposed by the adapter as narrow properties.

## Notes

- `flow` is disabled unless `LINGTAI_SOUL_FLOW_ENABLED` is truthy. A disabled
  `flow` action returns `status: "disabled"` without a thread; a disabled
  `config` action still saves valid knobs and returns `status: "ok"` plus
  `soul_flow_enabled: false` and an explanatory note.
- The model-facing call is always `action` + strict action-local `input` +
  `reasoning`. Config sends both nullable knob keys and requires at least one
  non-null value; synthesized flow pairs use the same envelope.
- The root bridge is intentionally narrow and singular. Do not reintroduce
  `agent_soul_runtime`, `AgentSoulRuntimeAdapter`, or whole-Agent fallback
  logic into the four implementation consumers.
