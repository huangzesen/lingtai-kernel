---
related_files:
  - src/lingtai/kernel/stream_progress/BEHAVIORS.md
  - src/lingtai/kernel/stream_progress/CONTRACT.md
  - src/lingtai/kernel/stream_progress/__init__.py
  - src/lingtai/kernel/ANATOMY.md
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/kernel/llm/ANATOMY.md
  - src/lingtai/adapters/stream_progress.py
  - src/lingtai/kernel/session.py
  - src/lingtai/kernel/llm_utils.py
  - src/lingtai/kernel/llm/base.py
  - src/lingtai/kernel/llm/streaming.py
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/cli.py
  - src/lingtai/tools/system/settings.py
  - docs/references/stream-progress.md
maintenance: |
  Keep related_files repo-relative, duplicate-free, and linked to real files.
  Keep this component's ANATOMY.md and CONTRACT.md reciprocal and keep
  parent/child anatomy links bidirectional. Code is the structural source of
  truth: update this anatomy in the same change that moves files, symbols,
  connections, composition, or state. Verify every changed citation and run the
  architecture-document validation before merge.
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# Stream Progress Port Anatomy

This folder is the Core-owned RAM-resident stream-progress boundary: the
technology-neutral Port the kernel session brackets every streaming provider
call with, the memory-only state and typed snapshot a publisher serves, and the
deterministic loopback discovery arithmetic shared with every consumer. The
production loopback HTTP publisher lives outside Core; its promises are defined
in the paired [`CONTRACT.md`](CONTRACT.md) and the capability is taught by the
manual [`docs/references/stream-progress.md`](../../../../docs/references/stream-progress.md).

## Components

- `StreamProgressPort` — abstract outbound Port with exactly
  `begin() -> generation`, `add_chars(generation, count)`, `end(generation)`
  (`src/lingtai/kernel/stream_progress/__init__.py`).
- `StreamProgressState` — thread-safe memory-only implementation of the Port;
  `add_chars`/`end` act only for the currently active generation;
  `snapshot()` returns a `StreamProgressSnapshot` (same file).
- `StreamProgressSnapshot` — frozen value with exactly `schema`, `agent_id`,
  `generation`, `active`, `streamed_chars`, `updated_unix_ms`, `pid`;
  `to_dict()` is the wire body (same file).
- `STREAM_PROGRESS_SCHEMA` / `STREAM_PROGRESS_PATH` / `candidate_ports` /
  `discovery_seed` — the `lingtai.stream-progress/v1` schema string, the
  `/v1/stream-progress` path, and the seed + 8-candidate port arithmetic
  (same file).

## Connections

- `SessionManager(stream_progress=...)` stores the Port; `_send_streaming`
  calls `_progress_begin` (fail-open `begin()` → generation or `None`) before
  `send_with_timeout_stream`, passes the per-call count-only closure from
  `_make_output_chars_callback(progress, generation)` as `on_output_chars`
  (worker thread → `add_chars(generation, count)` for positive `int` counts),
  and calls `end(generation)` in a `finally`;
  `_progress_call`/`_warn_progress_failure` are the one fail-open wrapper and
  its once-per-session warning (`src/lingtai/kernel/session.py`).
- The count reaches the session through the neutral LLM boundary:
  `send_with_timeout_stream(..., on_chunk=None, on_output_chars=None)`
  forwards each callback only when given, and `on_output_chars` only when
  `_accepts_keyword` finds the session's `send_stream` takes it
  (`src/lingtai/kernel/llm_utils.py`);
  `ChatSession.send_stream(message, on_chunk=None, on_output_chars=None)`
  declares the two independent callbacks and its non-streaming fallback
  reports one `response_output_chars` count (`src/lingtai/kernel/llm/base.py`);
  `OutputProgress` is the one count seam — `add(*fragments)` publishes the
  summed `output_length` of whatever the adapter hands it,
  `add_stream`/`add_final` keep terminal echoes from counting twice, and
  `output_values` lets an adapter count an event shape whole
  (`src/lingtai/kernel/llm/streaming.py`; see
  [`src/lingtai/kernel/llm/ANATOMY.md`](../llm/ANATOMY.md)). Every streaming
  provider adapter under `src/lingtai/llm/` feeds it each output fragment it
  receives, alongside its unchanged `StreamingAccumulator`; none is named in
  Core.
- `BaseAgent.__init__(stream_progress_factory=...)` calls the factory once with
  the stable `agent_id` right after identity resolution — only when
  `streaming` is true; an explicit `streaming=False` never calls it — stores
  the Port as `self._stream_progress`, and passes it to `SessionManager`
  (`src/lingtai/kernel/base_agent/__init__.py`). `streaming` defaults to `False`.
- The only production adapter is `LoopbackStreamProgressPublisher` plus its
  `loopback_stream_progress_factory` (`src/lingtai/adapters/stream_progress.py`),
  mapped structurally by the source root [`src/lingtai/ANATOMY.md`](../../ANATOMY.md).
  `lingtai.cli.build_agent` injects the factory and passes
  `streaming=runtime_policy.streaming` from the System v2 runtime-policy owner
  (`src/lingtai/tools/system/settings.py`: valid `LINGTAI_STREAMING` > valid v2
  `settings/system.json` `streaming` > fixed `false`; never `init.json`), and
  `run` closes the publisher best-effort after stop (`src/lingtai/cli.py`).
  `lingtai.Agent` passes the kwarg through untouched; programmatic callers get
  no endpoint unless they explicitly enable streaming and inject a factory.
- Consumers (first: the `lingtai-tui` Go client) reimplement `candidate_ports`
  byte-for-byte and read `GET /v1/stream-progress` on `127.0.0.1`.

## Composition

- **Parent:** `src/lingtai/kernel/` (see [`ANATOMY.md`](../ANATOMY.md)).
- **Paired contract:** [`CONTRACT.md`](CONTRACT.md) owns the Port's behavioral
  promises, the documented v1 API, and the contract tests.
- **Sibling relationship:** `base_agent/` composes the Port through its factory
  kwarg; see [`src/lingtai/kernel/base_agent/ANATOMY.md`](../base_agent/ANATOMY.md).
  `llm/` owns the neutral `ChatSession.send_stream` boundary and the
  `OutputProgress` seam the Port's counts come from; see
  [`src/lingtai/kernel/llm/ANATOMY.md`](../llm/ANATOMY.md).

## State

`StreamProgressState` owns the only state: `generation` (monotonic per process;
also the token `begin` hands back and `add_chars`/`end` must match), `active`,
`streamed_chars`, `updated_unix_ms`, plus the fixed `agent_id`/`pid`, all behind
one lock and all in RAM. Nothing is persisted; nothing outlives the agent
process. The Port itself and the discovery arithmetic own no state.

## Notes

This is a navigation-only Port anatomy; the lifecycle, fail-open, configuration,
schema, and discovery rules are normative in the paired
[`CONTRACT.md`](CONTRACT.md). There is no dedicated anatomy for the one-file
portable loopback adapter — its structure is owned by this governed pair and
the source-root composition map. Provider adapters only hand each output
fragment they receive to `OutputProgress`; the rule — output arrived, add its
length — lives once in Core, never per provider.
