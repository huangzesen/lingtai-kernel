---
related_files:
- src/lingtai/kernel/stream_progress/CONTRACT.md
- src/lingtai/kernel/stream_progress/ANATOMY.md
- src/lingtai/kernel/stream_progress/__init__.py
- src/lingtai/adapters/stream_progress.py
- src/lingtai/tools/system/settings.py
maintenance: |
  Capability manual for the stream-progress Port and its loopback read API; reciprocally linked from stream_progress/CONTRACT.md and ANATOMY.md related_files (enforced by tests/test_architecture_documents.py) — keep the schema, discovery arithmetic, and lifecycle guidance synced with the Port's actual contract, the adapter, and the Go client that mirrors it.
---

# Stream Progress — capability manual

**Stream progress** is the kernel's consumer-neutral, read-only view of *how
much* of the current LLM response has streamed so far. It answers one question
for any local viewer — "is this agent receiving output right now, and roughly
how many tokens have arrived?" — without exposing a single character of that
output and without writing anything to disk.

This manual teaches *what to do* with the capability. The normative promises
live in the paired
[`CONTRACT.md`](../../src/lingtai/kernel/stream_progress/CONTRACT.md); the code
map lives in the paired
[`ANATOMY.md`](../../src/lingtai/kernel/stream_progress/ANATOMY.md).

## Why it exists

While an agent is `ACTIVE`, a viewer such as `lingtai-tui` shows `Active N s`
and nothing else; a long provider response looks identical to a stalled one.
Claude Code shows `N tok downloaded` next to its spinner for the same reason.
The kernel is the only process that knows the answer, but the answer must not
become a status file (there is no filesystem progress state), must not leak
output text, and must not depend on which viewer is asking. So the kernel owns
a RAM-resident counter behind a Core Port and serves it through one documented
loopback endpoint that any consumer can find deterministically.

## What the kernel publishes

The state has five moving parts and is memory-only:

| field | meaning |
|---|---|
| `schema` | always `lingtai.stream-progress/v1` |
| `agent_id` | the agent's stable manifest identity |
| `generation` | increments on every provider response start (process-local) |
| `active` | `true` between response start and its success/failure |
| `streamed_chars` | length of all provider output received so far in this response; `0` when inactive |
| `updated_unix_ms` | last transition time (wall clock, integer ms) |
| `pid` | the publishing agent process |

Consumers estimate tokens as the integer `streamed_chars / 4`. There is **no
text field** and there never will be in v1.

## What counts

One rule, the same for every provider: **provider output arrived, so its
length is added.** Text, reasoning, tool identity and arguments, signatures,
opaque or encrypted payloads, and whatever a provider streams next all count
the same way — the Python length of the representation actually delivered
(Unicode characters for text, which is the `chars / 4` approximation Claude
Code uses; the delivered representation's length for bytes/base64/opaque
strings; canonical JSON length for structured payloads). A generation that
spends twelve seconds composing a tool call therefore shows progress while it
happens. Request input, prompts, tool results, usage metadata, and transport
framing are not model output and add nothing; a terminal event that repeats
output already delivered as deltas adds nothing, and output delivered only as
a terminal payload is counted once. Content is never retained — the count-only
`on_output_chars: Callable[[int], None]` callback on
`ChatSession.send_stream(message, on_chunk=None, on_output_chars=None)`
receives integers, nothing else. A session that only inherits the
non-streaming fallback reports the whole response once after the call returns.

## Lifecycle inside the kernel

`SessionManager._send_streaming` brackets the
`ChatSession.send_stream(message, on_output_chars=...)` call:

1. `generation = begin()` — before the session starts waiting on the provider.
2. `add_chars(generation, count)` — from the worker thread, for every
   provider output fragment, through a count-only callback closure built for
   this one call; anything that is not a positive integer publishes nothing.
3. `end(generation)` — in a `finally`, so a raised timeout, a provider error,
   and a normal completion all clear the snapshot (`active=false`,
   `streamed_chars=0`).

The generation token is what keeps a timed-out response harmless: the kernel
abandons the worker thread on timeout but cannot stop it, and if it keeps
emitting after the next response has begun, its deltas and its `end` carry the
old generation and are ignored. Only the response that owns the current
generation can change the snapshot.

Every Port call is fail-open: a raising publisher is logged once per session
and the LLM call proceeds normally.

## Streaming is System-owned

For a production `lingtai-agent run` agent, the one configuration owner is
`lingtai.tools.system.settings.resolve_runtime_policy` (the System manual's
runtime-policy v2 section): a valid `LINGTAI_STREAMING` environment value, then
a valid boolean `streaming` in a v2 `<agent>/settings/system.json` document,
then the fixed default `false`. `init.json` does not participate: its canonical
template ships no `streaming` key, while a legacy `manifest.streaming` remains
compatibility-readable but cannot affect boot or refresh. When the resolved
policy is off, the agent uses the non-streaming send and composes no publisher,
so no loopback endpoint is bound. Programmatic `BaseAgent`/`lingtai.Agent`
callers likewise default to `streaming=False`; callers that need streaming pass
`True` explicitly.

## Reading it (writing a consumer)

1. Learn the agent's `agent_id` (its `.agent.json`).
2. Compute the candidate ports exactly like the kernel:
   `seed = uint16_be(SHA256("lingtai.stream-progress/v1\0" + UTF8(agent_id))[0:2])`,
   then for `i = 0..7`: `41000 + ((seed + i * 7919) mod 20000)`.
3. Probe `http://127.0.0.1:<candidate>/v1/stream-progress` in order with a short
   timeout and redirects disabled (a foreign service on a candidate must never
   send you off loopback). Accept a response only if it is `200`, a single
   JSON object with exactly the seven v1 fields — no extras, no `text` —
   `schema == "lingtai.stream-progress/v1"`, and `agent_id` equals yours
   exactly. Anything else (connection refused, a redirect, a foreign service,
   another agent) means "try the next candidate".
4. Cache the accepted port in memory. On the next read, try the cached port
   first; on any failure or identity mismatch, rescan from step 3.
5. Show progress only when `active` is `true`; clear immediately otherwise.
   Never persist a snapshot.

Because discovery is deterministic and the endpoint lives as long as the agent
process, a viewer that restarts reattaches without any shared file. Two agents
never collide: a publisher binds the *first free* candidate for its own id, and
a reader rejects any body whose `agent_id` differs.

```bash
# Quick manual check while an agent is running
python - <<'EOF'
from lingtai.kernel.stream_progress import candidate_ports
print(candidate_ports("<agent_id from .agent.json>"))
EOF
curl -s http://127.0.0.1:<candidate>/v1/stream-progress
```

## Composition

`lingtai.cli.build_agent` resolves the System runtime policy once and passes
`streaming=runtime_policy.streaming` while injecting
`loopback_stream_progress_factory`; `BaseAgent` calls the factory once with the
stable `agent_id` only when streaming is on, then passes the returned Port to
`SessionManager`. Bare `BaseAgent`/`lingtai.Agent` callers get no endpoint
unless they explicitly enable streaming and inject their own factory — a fake
Port in tests, for example. A bind failure on all eight candidates is logged
once and the agent keeps running without a badge.

## Non-goals

No filesystem progress state; no output preview; no bind beyond `127.0.0.1`;
no authentication or write API; no generic plugin/tool; no change to mail,
status, or Portal schemas; no sampling thread, history, rate, or chart in the
kernel — every temporal derivation (tokens per second, diffs, retention) is
downstream of the single current snapshot. Provider adapters only hand each
output fragment they receive to the one Core count seam; no provider identity
enters Core.
