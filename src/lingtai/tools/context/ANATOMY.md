---
related_files:
  - src/lingtai/tools/context/manual/SKILL.md
  - src/lingtai/tools/context/manual/assets/molt-template.md
  - src/lingtai/tools/context/manual/assets/session-journal-entry-template.md
  - src/lingtai/tools/context/manual/reference/summarize-manual/SKILL.md
  - src/lingtai/tools/context/manual/reference/molt-manual/SKILL.md
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/context/BEHAVIORS.md
  - src/lingtai/tools/context/CONTRACT.md
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/tools/pad/ANATOMY.md
  - src/lingtai/tools/lingtai/ANATOMY.md
  - src/lingtai/tools/system/ANATOMY.md
  - src/lingtai/tools/system/summarize.py
  - src/lingtai/tools/context/__init__.py
  - tests/test_context_declared_tool_plugin.py
  - src/lingtai/tools/context/_molt.py
  - src/lingtai/tools/context/_session_journal.py
  - src/lingtai/tools/context/_snapshots.py
  - src/lingtai/agent.py
  - src/lingtai/kernel/base_agent/prompt.py
  - src/lingtai/tools/context/glossary-en.md
  - src/lingtai/tools/context/glossary-wen.md
  - src/lingtai/tools/context/glossary-zh.md
maintenance: |
  Keep paths real, repo-relative, duplicate-free, and reciprocal with the paired
  Contract and connected anatomies. Update this graph with schema, lifecycle,
  composition-path, summary-engine, or state-ownership changes.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# tools/context

Context lifecycle family with exact public actions
`molt | summarize | rebuild | manual`. `rebuild` is the sole active full
reconstruction operation; refresh and molt invoke the same internal contract as
passive lifecycle scenarios.

## Components

- `__init__.py`
  - module-level `DECLARATION` owns Context identity, ordered operational
    actions, strict schemas, and the packaged `context-manual`;
  - `_bind(host)` composes the public family from only `host.workdir` and
    `host.context_runtime`; `setup`/`boot` supply the registrar wiring while
    `handle` is direct-caller compatibility only;
  - strict per-action schemas, including genuinely optional `rebuild.items` so
    bare `{}` is schema-valid;
  - `_summarize_action` pins record-only engine mode;
  - `_rebuild_action` calls `agent._reconstruct_context` before invoking the
    private summary engine, handles reconstruction failures as result dicts, and
    marks successful engine results `prompt_reconstructed: true`;
  - `_CHILD_SPECS`, `_build_children`, `_FAMILY`, `get_schema`, `handle` provide
    single-registry schema/dispatch and isolate `_tc_id` to molt;
  - manual adaptation resolves `context-manual` once after dispatch.
- `../system/summarize.py` — private history-summary engine. It records pending
  marker replacements, marks the applied set done, persists history, and only
  then calls `chat.request_history_rebuild`. It is not a public `system` action.
- `_molt.py` — agent and system molt implementations; shared
  `_select_keep_last_entries` atomically selects suffixes around complete
  single/parallel assistant tool-result batches; replay selection,
  archive/wipe, post-molt hook invocation before fresh-session creation, and
  post-molt notification publishing.
- `_session_journal.py` — fail-closed journal-path/frontmatter gate.
- `_snapshots.py` — atomic pre-molt snapshots and retrospective persistence.
- `agent.py`
  - `_reload_prompt_sections` is the authoritative all-source composer and
    reuses private `_lingtai_load`/`_pad_load`, consuming either its caller's
    immutable Psyche candidate or one owner read;
  - `_reconstruct_context` wraps that composer and the final full prompt flush
    in one applied-prompt-generation rollback boundary;
  - `_setup_from_init` validates one Psyche candidate before live teardown,
    routes refresh through this method, and registers exactly this method as the
    one post-molt hook.
- `kernel/base_agent/prompt.py::_flush_system_prompt` calls the virtual
  `agent._build_system_prompt`, preserving Agent-owned `base_prompt` and tool
  composition in the published/provider-visible prompt.

## Ordering and connections

Active rebuild flow:

```text
context official mount handler
  -> ContextRuntimePort.rebuild
  -> _rebuild_action
  -> Agent._reconstruct_context
     -> Agent._reload_prompt_sections
        -> private LingTai/Pad composers + every other canonical source
     -> virtual full prompt build/flush to disk and live interface
  -> private summary engine (new and/or pending summaries)
  -> chat.request_history_rebuild (provider replay)
```

Bare rebuild follows the same flow even when there are no pending markers.
Refresh supplies already-resolved init data and later rebuilds its session with
preserved history; its Psyche candidate is resolved before teardown and reused
by reconstruction. Molt invokes the one registered `_reconstruct_context` hook
before `ensure_session`. A failed reconstruction restores the prior in-memory
prompt generation and derived base/covenant/system mirrors. Pad/LingTai boot
functions only perform initial composition; they do not register hooks.

## State and invariants

Context-owned persistent paths are `system/summaries/`, `history/snapshots/`,
`history/chat_history.jsonl`, `history/chat_history_archive.jsonl`, and the
post-molt notification. Pad and LingTai files are durable sources owned by their
families/file mutation, but the context reconstruction path composes them along
with base prompt, covenant, packaged layers, rules, comment, guidance,
and current tool/meta sections.

`summarize` never reconstructs. `rebuild` always composes before history
mutation and provider request. `molt` retains refusal-before-shed and its distinct
archive/count/replay effects. No retired root or action is an alias.
