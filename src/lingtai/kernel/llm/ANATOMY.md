---
related_files:
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/service.py
  - src/lingtai/kernel/ANATOMY.md
  - src/lingtai/kernel/stream_progress/ANATOMY.md
  - src/lingtai/kernel/llm/__init__.py
  - src/lingtai/kernel/llm/base.py
  - src/lingtai/kernel/llm/reasoning_effort.py
  - src/lingtai/kernel/llm/interface.py
  - src/lingtai/kernel/llm/service.py
  - src/lingtai/kernel/llm/streaming.py
  - tests/test_llm_service.py
  - tests/test_wire_tool_description.py
  - tests/test_tool_prose_section_gate.py
  - tests/test_codex_live_effort.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# llm

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues/mail/PR proposals; do not silently fix.

Provider-agnostic LLM protocol layer. This folder defines the canonical chat log, normalized response/tool schema types, streaming accumulation, and ABCs the kernel uses; concrete provider adapters live in the wrapper package under `src/lingtai/llm/`.

## Components

- `llm/__init__.py` — public re-export surface for `ChatSession`, `LLMResponse`, `ToolCall`, `FunctionSchema`, and `LLMService` (`llm/__init__.py:2-10`).
- `llm/base.py` — normalized dataclasses, `ChatSession` ABC, and the shared replay-terminal exception boundary.
  - `safe_exception_description`, exact `LLMReplayTerminalError`, `llm_replay_terminal_flags`, and `mark_llm_replay_terminal` (`llm/base.py:24-91`) form the provider-neutral no-replay boundary. Every provider-owned exception is wrapped, and only the exact kernel wrapper carries trusted `True` flags; producer and consumer never read or mutate provider marker storage. Requested flags merge, so visible partial output retains priority over exhausted provider recovery, while nested `str`/`repr`/class-label fallback keeps both adapter and BaseAgent error paths render-safe. Regressions: `tests/test_aed_recovery.py`, `tests/test_codex_native_multiaccount.py`.
  - `ToolCall`, `UsageMetadata`, `LLMResponse`, and `FunctionSchema` define tool calls, token usage, provider responses, and tool schemas (`llm/base.py:112-238`). `FunctionSchema.glossary_package` is an optional non-wire metadata field naming the importable resource package that owns the tool's `glossary-{lang}.md` files; the `## tools` renderer uses it to append a localized terminology body via `tool_glossary.append_tool_glossary`. It is never serialized into provider payloads (`to_dict` excludes it alongside `system_prompt`). `wire_tool_description` (`llm/base.py:190`) resolves the top-level wire description for registered `FunctionSchema` tools, so exactly one surface carries a tool's prose per turn. Default (`LINGTAI_TOOL_PROSE_SECTION_ENABLED` unset/falsey): the resident `## tools` section is not rendered at all (`base_agent/tools.py:_refresh_tool_inventory_section`) and the wire carries the full `FunctionSchema.description`. Opted in: the section carries the prose and the wire carries the `WIRE_TOOL_DESCRIPTION` pointer constant (`llm/base.py:187`) instead. An empty description always falls back to the pointer. Canonical `ChatInterface` tool snapshots (`to_dict`/`list_to_dicts`) keep the full prose in both states. Nested parameter/property descriptions are untouched. Separate structured-output pseudo-tools keep their task-specific descriptions. Tests: `tests/test_wire_tool_description.py`, `tests/test_tool_prose_section_gate.py`.
  - `ChatSession` requires an `interface` property and `send()` accepting text, tool results, or `None` (`llm/base.py:240-418`), then supplies default helpers for history/state, usage totals, streaming fallback, tool-result commits, tool/system updates, reset, interaction id, context window, and context-overflow recovery (`llm/base.py:240-654`).
  - **`send_stream(message, on_chunk=None, on_output_chars=None)`** — two independent optional callbacks: `on_chunk` is the pre-existing visible-text delta callback, kept with its legacy semantics for backward compatibility; a session that overrides `send_stream` without the new keyword is still called by `send_with_timeout_stream` without it (fail-open, no progress); `on_output_chars: Callable[[int], None]` is the count-only output-progress callback (the length of every provider output fragment, never content) that the kernel `stream_progress` boundary consumes. The default non-streaming fallback calls `send()`, passes `response.text` to `on_chunk`, and reports one `response_output_chars` count. See `src/lingtai/kernel/stream_progress/ANATOMY.md`.
  - `reasoning_effort.py` is the provider-neutral live-control port: `ReasoningEffortCapability` describes one exact active route, `ReasoningEffortController` owns process-local self-facing set/clear state, and immutable snapshots plus route fingerprints keep each dispatch stable and fail closed on route drift. Provider adapters bind through `ChatSession` without importing wrapper-specific vocabulary.
  - **`send()` signature contract** — adapters accept three message shapes: `str` (new user text → `add_user_message`), `list[ToolResultBlock]` (tool returns → `add_tool_results`), and `None` (the "continue from wire" signal — caller has already pre-staged the canonical interface, e.g. via `_inject_notification_pair`; the adapter must skip the input-append step and send the wire as-is). On API error the error-path `drop_trailing` must be guarded so a `None` send does not corrupt the pre-staged wire. See `lingtai/llm/openai/ANATOMY.md` and `lingtai/llm/anthropic/ANATOMY.md` for adapter-side details, and `base_agent/turn.py:_handle_tc_wake` for the call site that drives a turn off the existing wire.
  - **`pre_request_hook`** (`llm/base.py:273`) — optional callable adapters fire after committing the message to the canonical `ChatInterface` but before the API call. Historically the kernel installed `BaseAgent._drain_tc_inbox_for_hook` here to drain the involuntary tool-call inbox mid-turn. Post-`.notification/`-redesign (`fadbabf` / `d2da97e`) the hook is still installed but the queue is always empty in production — ACTIVE notifications now defer to the post-turn IDLE synthetic-pair path instead of a send-time prefix hook. Default `None` — adapters that don't install treat the call as a no-op. Phase 3 will remove the hook. See root `ANATOMY.md` "Notifications" for the full picture, including the canonical-vs-server-state regime distinction.
  - **Runtime reasoning-effort port** (`llm/base.py`, added beside
    `pre_request_hook`) — `reasoning_effort_capability()`,
    `set_reasoning_effort_policy(provider)`, and
    `last_reasoning_effort_dispatch()`. A DEDICATED seam, deliberately not the
    single-slot `pre_request_hook` (owned by the Task Card inbox drain): a
    session may carry both and installing one never disturbs the other.
    Defaults are fail-closed — unavailable capability, `False` binding, no
    dispatch evidence — so every adapter without such a route is unchanged and
    Core never branches on a concrete provider. Today only
    `CodexResponsesSession` implements it (`src/lingtai/llm/openai/ANATOMY.md`).
- `llm/reasoning_effort.py` — Core-owned neutral value objects and controller for
  the port above: `ReasoningEffortCapability` (available/route/values/
  provider_default/baseline/settable/fingerprint/evidence_revision, plus the
  shared `UNAVAILABLE_CAPABILITY` singleton), `ReasoningEffortSnapshot` (the one
  immutable per-dispatch decision: effective/source/revision/fingerprint/
  baseline/requested), `ReasoningEffortResult`, and
  `ReasoningEffortController`. The controller holds only
  `{capability (fingerprint + baseline), optional override, monotonic revision}`
  and owns `status()` / `set()` / `clear()` / `snapshot()`. `route` and
  `fingerprint` are opaque adapter-owned tokens that Core stores and compares
  but never parses. `bind_capability()` keeps the override across a rebind to
  the SAME fingerprint (what makes an in-process session rebuild transparent)
  and drops it on route drift. An unavailable route is get-only, an invalid
  `set` never mutates state, and `clear` restores the capability's `baseline`
  rather than any provider value. **`baseline` means the adapter session's
  ACTUAL construction value**, so binding a controller reproduces the unchanged
  wire and can never move a request on its own; a provider that also has a fixed
  omitted/default policy reports that separately (see
  `src/lingtai/llm/openai/ANATOMY.md`). `SessionManager` owns the instance
  (`kernel/ANATOMY.md`); tests: `tests/test_codex_live_effort.py`.
- `llm/interface.py` — canonical conversation representation.
  - Content blocks: `TextBlock`, `ToolCallBlock`, `ToolResultBlock`, `ThinkingBlock`; `ContentBlock` union and `content_block_from_dict()` (`llm/interface.py:35-228`).
  - `InterfaceEntry` is one role+content row with id, role, timestamp, provider metadata, model/provider, usage, and optional tool snapshot (`llm/interface.py:230-294`).
  - `ChatInterface` is the append-only source of truth for history (`llm/interface.py:296`). It appends system/user/assistant/tool-result entries (`llm/interface.py:522-660`), enforces/repairs tool-call pairing (`llm/interface.py:345-521`), removes strict synthetic pairs (`llm/interface.py:662-767`), prunes history (`llm/interface.py:857-930`), estimates tokens (`llm/interface.py:932-980`), and supports compaction summaries (`llm/interface.py:981-1057`).
- `llm/service.py` — `LLMService` ABC: `model`, `provider`, `create_session()`, `generate()`, and `make_tool_result()` (`llm/service.py:16-70`).
- `llm/streaming.py` — `StreamingAccumulator`, which gathers streaming text/thought/tool-call deltas and finalizes to `LLMResponse`. Sequential tool-call assembly accepts either deltas or a first non-empty complete terminal argument string via `set_tool_args_if_empty()` (which now reports whether the terminal value was adopted); index-keyed deltas remain separate, as do atomic tool calls and `_finalize_tool()`. The same module owns the separate count-only **`OutputProgress`** seam — `add(*fragments)` publishes the summed `output_length` (delivered representation length) of whatever an adapter hands it, `add_stream`/`add_final` dedupe terminal echoes, `output_values` reads an event shape whole, and `response_output_chars` is the whole-response count the `ChatSession` fallback reports. Consumed by the kernel `stream_progress` boundary (`src/lingtai/kernel/stream_progress/ANATOMY.md`); tests: `tests/test_streaming.py`, `tests/test_stream_progress.py`.

## Connections

- `base_agent/` imports kernel LLM types for service injection, tool execution, and synthetic history repair (`base_agent/__init__.py:29-36`, `base_agent/__init__.py:773`, `base_agent/__init__.py:1034`, `base_agent/__init__.py:1406`).
- `session.py` imports `ChatSession`, `FunctionSchema`, `LLMResponse`, and `LLMService` to own session lifecycle and token/context bookkeeping (`session.py:12-17`).
- `tool_executor.py` consumes `ToolCall` (`tool_executor.py:8`); `tc_inbox.py` (legacy, dormant — preserved for back-compat until Phase 3) consumes `ToolCallBlock`/`ToolResultBlock` for synthetic pairs (`tc_inbox.py:33`). The same canonical block types are now used by `BaseAgent._inject_notification_pair` to splice synthesized `notification(action="check")` `(call, result)` pairs into the wire, replacing the legacy queue path.
- `lingtai/tools/context/` and `lingtai/tools/soul/` use canonical blocks/interfaces for molt replay and soul-flow consultation (`src/lingtai/tools/context/_molt.py:14`, `src/lingtai/tools/soul/inquiry.py:16`, `src/lingtai/tools/soul/consultation.py:223`, `src/lingtai/tools/soul/consultation.py:454`, `src/lingtai/tools/soul/consultation.py:594`).
- Outbound from this folder is minimal: `ChatInterface.estimate_context_tokens()` lazy-imports `token_counter.count_tokens` (`llm/interface.py:943`).
- Wrapper boundary: `src/lingtai/llm/service.py` provides the concrete `LLMService` subclass (`src/lingtai/llm/service.py:25`); wrapper adapters import kernel types, but the kernel does not import the wrapper.

## Composition

- **Parent:** `src/lingtai/kernel/` (see `ANATOMY.md`).
- **Subfolders:** none.
- **Siblings:** `session.py` persists and compacts `ChatInterface`; `token_ledger.py` persists usage; `intrinsics/` manufactures synthetic LLM blocks for psyche/soul/email flows.

## State

- **Ephemeral:** `ChatInterface._entries`, `_next_id`, current system/tools, and `_pending_system` live in memory for one session (`llm/interface.py:305-318`).
- **Ephemeral:** `StreamingAccumulator` stores partial text, tool args, and thoughts until `finalize()`; `OutputProgress` keeps only the keys of streamed terminal-deduped items, never content (`llm/streaming.py`).
- **Persistent writes:** none in this folder. `session.py` writes `history/chat_history.jsonl`; token/state persistence happens in sibling modules that consume these types.

## Notes

- `add_system()` defers system/tool updates while the tail has unanswered tool calls so strict providers do not see a system entry between assistant tool calls and user tool results (`llm/interface.py:522-572`).
- `close_pending_tool_calls()` closes unanswered tail tool calls by first accepting optional real recovered `ToolResultBlock`s from a recovery lookup and then synthesizing abort placeholders for any remaining misses (`llm/interface.py:445-521`, `llm/interface.py:99-186`).
- `StreamingAccumulator` intentionally supports three provider styles in one place: sequential, index-keyed, and atomic tool calls (`llm/streaming.py:72-126`).
