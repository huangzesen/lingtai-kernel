---
name: stream-progress
contract_version: 1
root_contract: CONTRACT.md
related_files:
  - src/lingtai/kernel/stream_progress/ANATOMY.md
  - src/lingtai/kernel/stream_progress/BEHAVIORS.md
  - src/lingtai/kernel/stream_progress/__init__.py
  - src/lingtai/adapters/stream_progress.py
  - src/lingtai/kernel/session.py
  - src/lingtai/kernel/llm_utils.py
  - src/lingtai/kernel/llm/base.py
  - src/lingtai/kernel/llm/streaming.py
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/kernel/base_agent/CONTRACT.md
  - src/lingtai/cli.py
  - src/lingtai/tools/system/settings.py
  - docs/references/stream-progress.md
  - tests/test_stream_progress.py
  - tests/test_streaming.py
  - tests/test_architecture_documents.py
maintenance: |
  <!-- CANONICAL-MAINTENANCE v2 BEGIN -->
  This component contract is governed by the root CONTRACT.md. Keep
  related_files complete and repo-relative: the paired ANATOMY.md, Port, every
  production Adapter, contract tests, and directly relevant component contracts
  belong here. Re-read this contract whenever a linked boundary changes. Update
  the Port, affected Adapters, contract tests, and this contract in the same
  change; update the paired Anatomy when structure or composition also changes;
  bump contract_version for a breaking Port-contract change. If code and contract
  disagree, treat the disagreement as a defect—do not silently rewrite the
  normative contract to match the implementation.
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
  <!-- CANONICAL-MAINTENANCE END -->
---
# Stream Progress

## Purpose
Guarded by: [SP001](BEHAVIORS.md#behavior-sp001)

The stream-progress boundary is the kernel's consumer-neutral, documented,
read-only exposure of *how much* of the current LLM response has streamed —
never *what* streamed. The kernel owns the RAM-resident state and its
documented local API; the TUI is only the first consumer. The capability is
taught by [`docs/references/stream-progress.md`](../../../../docs/references/stream-progress.md).
It does not own output text, transcripts, token accounting, the streaming
adapters, or any on-disk status schema.

## Behavior

Runtime and coding agents MUST treat stream progress as memory-only state: no
status file, JSONL record, shared `.lingtai/` schema, output transcript,
partial output text, or credential may be written or served for it. Progress
has one rule: **provider output arrived, so its length is added.** Whatever
form the model's output takes, and whether or not the kernel retains,
normalizes, or can read it, each fragment adds the Python `len` of the
representation the provider delivered (Unicode characters for text — the
`chars / 4` approximation; the delivered representation's length for
bytes/base64/opaque strings; canonical JSON length for structured payloads).
The boundary neither distinguishes nor documents kinds of output. Request
input, prompts, tool schemas, tool results, usage metadata, and
transport/lifecycle framing are not model output and add zero. The same output
is never counted twice: a terminal echo of output already delivered as deltas
adds nothing; output delivered only as a terminal payload counts once. Content
is never retained. Consumers estimate tokens as integer `streamed_chars / 4`.
The snapshot lifecycle is per provider response: `begin` runs before the
session starts waiting on the provider (`generation += 1`, `active = true`,
`streamed_chars = 0`); every output fragment increments; success and failure
both clear (`active = false`, `streamed_chars = 0`) in a `finally`. A failure
inside progress publication
MUST never fail the LLM call, and a bind or read failure of the local endpoint
MUST leave the agent (and any consumer) running without the badge. LLM output
streaming is System-owned for production agents: valid `LINGTAI_STREAMING` >
valid v2 `settings/system.json` `streaming` > fixed `false`, never `init.json`.
A legacy `manifest.streaming` cannot affect boot or refresh. Agents MUST NOT add a text
field, a write or authenticated operation, a non-loopback bind, a filesystem
side channel, or a generic plugin/tool surface to this boundary.

## Port

`StreamProgressPort` (`src/lingtai/kernel/stream_progress/__init__.py`) exposes
exactly three operations, bound together by a generation token:

1. `begin() -> int` — a new provider response starts; returns the new
   generation the caller binds the next two operations to.
2. `add_chars(generation: int, count: int)` — provider output of length
   `count` arrived for `generation` (called from the LLM worker thread);
   counted only while that generation is the active one.
3. `end(generation: int)` — the response for `generation` finished, success or
   failure; clears only while that generation is the active one.

Core also owns the memory-only implementation `StreamProgressState`
(thread-safe; `snapshot() -> StreamProgressSnapshot`), the typed
`StreamProgressSnapshot` with exactly the documented fields, and the shared
discovery arithmetic `candidate_ports(agent_id)`. The Port names no socket,
HTTP, server, thread-server, filesystem, or platform vocabulary.

### Documented local API (v1)

- Bound only to `127.0.0.1`; path `GET /v1/stream-progress`; JSON body with
  `Cache-Control: no-store`. Other paths answer 404; non-GET methods 405.
- `schema` is the string `lingtai.stream-progress/v1`. Fields: `schema`,
  `agent_id` (string), `generation` (integer), `active` (bool),
  `streamed_chars` (integer), `updated_unix_ms` (integer), `pid` (integer).
  There is no text field.
- Discovery, shared byte-for-byte by producer and every client:
  `seed = uint16_be(SHA256("lingtai.stream-progress/v1\0" + UTF8(agent_id))[0:2])`;
  candidate `i` for `i = 0..7` is `41000 + ((seed + i * 7919) mod 20000)`.
  The publisher binds the first available candidate; a reader probes the
  candidates in order, accepts only a valid v1 response whose `agent_id`
  exactly matches, caches that port in RAM, and rescans after a failure.
- Endpoint lifetime is the agent process, so a restarted consumer reattaches
  by rerunning discovery while the agent lives.

## Adapters

`LoopbackStreamProgressPublisher` (`src/lingtai/adapters/stream_progress.py`)
is the one production adapter: it delegates the three generation-bound Port
operations to a `StreamProgressState` and serves that state's snapshot through Python's
`http.server` from a daemon thread on the first free candidate port, with
`allow_reuse_address` off so two live publishers never share a port.
`start()` returns `False` and logs once when every candidate is taken — the
publisher keeps counting with no endpoint. `loopback_stream_progress_factory`
is the composition-root factory. The adapter is portable (no filesystem,
`fcntl`, or platform selection), so it lives at the top of `lingtai.adapters`
and is not registered through a selector. Core never imports it.

## Contract rules

1. `SessionManager.__init__` accepts an optional `stream_progress` Port. With
   `None`, `_send_streaming` issues the pre-existing
   `send_with_timeout_stream` call with no callback argument (byte-for-byte
   unchanged `send_stream(message)` call shape). With a Port it calls
   `begin()` before the provider wait and captures the returned generation,
   passes a worker-thread count-only `on_output_chars` closure built for that
   call that publishes `add_chars(generation, count)` for each positive `int`
   it receives (anything else publishes nothing), and calls
   `end(generation)` for that same generation in a `finally`. The session
   passes no other callback. Every Port call is wrapped fail-open with one
   warning per session; a `begin()` that raises or returns a non-`int` issues
   no generation, so that call passes no `on_output_chars` and calls no
   `end`. Response, tool-call, `_text_already_streamed`, and
   `_intermediate_text_streamed` semantics are unchanged.
2. The neutral `ChatSession.send_stream(message, on_chunk=None,
   on_output_chars=None)` boundary (`src/lingtai/kernel/llm/base.py`) adds
   the optional, independent, backward-compatible
   `on_output_chars: Callable[[int], None]` count-only callback: positive
   `int` lengths, never content. The pre-existing `on_chunk` keeps its legacy
   semantics for compatibility only — a detail of the LLM interface docs, not
   of this boundary, which knows no output kinds. `send_with_timeout_stream`
   passes each callback only when not `None`, and passes `on_output_chars`
   only when the session's `send_stream` accepts that keyword (named or via
   `**kwargs`); a session that cannot accept it, or whose signature cannot
   be inspected, is called without it exactly once and simply publishes no
   progress — fail-open, never a `TypeError`, never a retry. `OutputProgress`
   (`src/lingtai/kernel/llm/streaming.py`) is the one provider-neutral count
   seam: every streaming adapter (Anthropic, OpenAI Chat Completions,
   OpenAI/custom Responses, Codex, Gemini Interactions, the gate proxy) hands
   it every output fragment it receives — including ones
   `StreamingAccumulator` never retains — and it publishes only lengths,
   counting a terminal payload once and never after its output was already
   delivered; no provider identity enters Core. A session
   inheriting the non-streaming fallback reports the whole response's length
   once after `send()` returns and never claims temporal streaming.
3. `BaseAgent.__init__` accepts an optional defaulted-`None`
   `stream_progress_factory: Callable[[str], StreamProgressPort | None]`. When
   `streaming` is true it is called exactly once, after the stable `agent_id`
   is resolved and before `SessionManager` is built, and a raising factory
   leaves the Port `None` without failing construction. With an explicit
   `streaming=False` the factory is never called — no publisher is composed
   and no endpoint is bound for a session that publishes nothing. Core never
   imports or constructs the concrete publisher. `lingtai.cli.build_agent`
   injects `loopback_stream_progress_factory`; the `run` host closes the
   publisher best-effort after `agent.stop`, outside the kernel stop order.
4. One production policy is truthful end to end: `cli.build_agent` passes
   `streaming=runtime_policy.streaming`, where
   `resolve_runtime_policy(working_dir)` resolves valid `LINGTAI_STREAMING` >
   valid v2 `settings/system.json` boolean `streaming` > fixed `False`. It
   never reads a manifest key — the canonical `init.jsonc` ships no `streaming`
   field, while a legacy `manifest.streaming` remains compatibility-readable but
   cannot affect boot or refresh. `BaseAgent.__init__` (therefore
   `lingtai.Agent`) also defaults `streaming=False`. A resolved or explicit
   `False` takes the non-stream `send` path.
5. `StreamProgressState` is generation-bound: `add_chars`/`end` mutate only
   while active *and* the given generation equals the current one. A delta or
   `end` from any other generation — an abandoned timed-out worker that keeps
   emitting after a newer response has begun, or one that arrives after its
   own response was cleared — is ignored, so a cleared snapshot stays cleared
   and a newer active snapshot is never altered by an older response.
   `generation` only ever increases within a process and resets with it.
6. The v1 snapshot exposes exactly the seven documented fields; adding a text
   field or a write/auth operation is a breaking change that requires a new
   schema string and `contract_version`. Consumers are expected to be equally
   strict: a body carrying any other field (including `text`) is not a valid
   v1 snapshot.

## Contract tests

`tests/test_stream_progress.py` proves the Port exposes exactly the three
abstract operations with no concrete-technology vocabulary in Core; the
discovery arithmetic reproduces pinned known vectors (shared with the Go
client); the state transitions (begin/delta/end, inactive and wrong-generation
deltas/ends ignored, including the old-generation-after-new-begin regression)
and the exact seven-field snapshot; `SessionManager` begins before the
provider wait, passes only the count-only callback, publishes every output
fragment's length bound to the returned generation, ignores non-positive or
non-`int` counts, clears that generation in `finally` on success and failure,
is immune to an abandoned worker's late callbacks/end during a newer response,
is fail-open when the Port raises (begin, add, or end) or returns a non-`int`
generation, uses `send` untouched with explicit `streaming=False`, and keeps
the no-Port call shape. `send_with_timeout_stream` passes only the callbacks it
was given; the `ChatSession` fallback keeps legacy `on_chunk` behavior and
reports the whole response once; every streaming adapter (Responses/Codex
parametrized, Anthropic, Chat Completions, Gemini) counts identity, arguments,
reasoning, signatures, opaque payloads, and other delta forms, terminal echoes
once. `tests/test_streaming.py` pins `OutputProgress` itself. The System-owned
production policy is valid `LINGTAI_STREAMING`, then v2 System settings, then
fixed false; a legacy manifest key is not an owner. The explicit-false path
(factory never called, non-stream send), `BaseAgent` factory injection
with the stable `agent_id` and factory fail-open; and the loopback endpoint
(`127.0.0.1` bind on a discovery candidate, schema/identity/`no-store`, 404/405,
live transition readback, next-free-candidate binding with reader-side
identity rejection, and bind-failure fail-open).
`tests/test_architecture_documents.py` enforces the governed twin and
reciprocal links.

## Maintenance

Read the paired Anatomy for locations and composition. Port, adapter, Core
callers, the Go client that mirrors the discovery arithmetic, shared contract
tests, and this contract change together. Breaking Port, schema, or discovery
changes bump `contract_version` and the schema string; implementation drift is
a defect, not permission to weaken this contract.
