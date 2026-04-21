# Unified Meta-Block Infrastructure

**Status:** Design approved (infra only — field curation deferred to a follow-up spec)
**Date:** 2026-04-20

## Problem

Today the kernel injects runtime metadata to the LLM at two separate sites, each with its own helper:

1. **Text input path** — `base_agent.py:1125` prepends `[Current time: …]` to every
   outgoing user/system message by calling `now_iso(agent)` and the
   `system.current_time` i18n string.
2. **Tool result path** — `tool_executor.py` calls `stamp_tool_result()`
   (`tool_timing.py:5`), which writes `current_time` and `_elapsed_ms` into
   every tool result dict.

Both helpers are ad-hoc and only carry time. We want to surface more runtime
metadata (e.g. context window breakdown: system prompt tokens, tools tokens,
session history tokens) on every turn, and we want **one curated surface** so
the agent sees a consistent meta-vocabulary everywhere, and so new fields are
added in one place rather than two.

## Non-Goals

- **Deciding what fields go into the block.** That is a separate, follow-up
  discussion. This spec lands the infrastructure only. It preserves the
  existing `current_time` / `_elapsed_ms` behaviour verbatim, behind the new
  abstraction.
- **Changing the molt-warning line** (`base_agent.py:1122`). It continues to
  work as today. A later spec can decide whether the meta block subsumes or
  merely supplements it.
- **Changing `time_veil.py` semantics.** The time-awareness switch remains
  authoritative for whether any time field is rendered.

## Design

### New module: `src/lingtai_kernel/meta_block.py`

Two functions, one source of truth.

```python
def build_meta(agent) -> dict:
    """Return the current meta-data snapshot for the agent.

    Single source of truth for 'what the agent sees about its own runtime
    state on every turn.' Curate carefully — this ships on every text input
    and every tool result.

    Respects agent._config.time_awareness / timezone_awareness internally;
    callers never need to special-case those flags.
    """

def render_meta(agent, meta: dict) -> str:
    """Render the meta dict as the human-readable line prepended to text input.

    Returns '' when the meta block would be empty (e.g. time-blind agent with
    no other fields curated in) — callers should treat '' as 'no prefix'.
    """

def stamp_meta(result: dict, meta: dict, elapsed_ms: int) -> dict:
    """Merge meta fields into a tool-result dict in place. `_elapsed_ms` is
    kept on the tool result — it is tool-specific telemetry, not meta — but
    it is suppressed alongside the meta fields when `meta` is empty, to
    preserve the pre-existing `stamp_tool_result(time_awareness=False)`
    behaviour verbatim."""
```

Internal composition: `stamp_meta` writes each `meta` key onto `result` (shallow),
and writes `_elapsed_ms` after them — but only when `meta` is non-empty. Empty
`meta` short-circuits and nothing is written, matching the old time-blind path.
`build_meta` is the one place that reads agent state and consults the time veil.

### Integration points

| Site                                            | Before                                                               | After                                                                                                       |
| ----------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `base_agent.py:1088-1125` (`_handle_request`)   | `current_time = now_iso(self)` then `_t(..., 'system.current_time')` | `meta = build_meta(self)` then `content = f"{render_meta(self, meta)}\n\n{content}"` (skip prefix when `''`) |
| `tool_executor.py:172, 208, 309`                | `stamp_tool_result(result, elapsed_ms, time_awareness=…, timezone_awareness=…)` | `stamp_meta(result, meta, elapsed_ms)` where `meta` is built once per tool-batch via `build_meta(agent)`    |

The `ToolExecutor` currently owns `time_awareness` / `timezone_awareness` flags
so it can call `stamp_tool_result` itself. After this change, `ToolExecutor`
instead receives a `meta_fn: Callable[[], dict]` (or equivalent) from
`BaseAgent` at construction time (`base_agent.py:1076`), and calls it to get
fresh `meta` before each stamp. This keeps `ToolExecutor` agnostic of the
`time_awareness` config and centralises the policy in `meta_block.py`.

### Preserving current behaviour

For this infra-only change, `build_meta` returns exactly the fields that ship
today:

```python
{"current_time": "<ISO-8601 or '' if time-blind>"}
```

And `render_meta` produces exactly today's string:

```
[Current time: 2026-04-20T10:15:23-07:00]   # en
[当前时间：2026-04-20T10:15:23-07:00]        # zh
[此时：2026-04-20T10:15:23-07:00]            # wen
```

Achieved by having `render_meta` continue to use the `system.current_time`
i18n key when only `current_time` is present. When new fields are added later,
either the i18n key evolves or `render_meta` composes multiple pieces — that
is a decision for the field-curation spec, not this one.

When `time_awareness` is false today, the existing code calls
`now_iso(agent)` which returns `''`, and the rendered string becomes
`[Current time: ]` — a useless empty prefix on every turn. **This is the
one intentional observable change in this refactor:** `render_meta` returns
`""` for empty meta, and the caller in `_handle_request` suppresses the
prefix entirely (`if prefix: content = f"{prefix}\n\n{content}"`). Time-blind
agents no longer see the empty-brackets line. All other behaviour (time-aware
rendering in all three locales, tool-result stamps, `_elapsed_ms` omission
for time-blind tool results) is byte-identical to the pre-refactor code.

### What `build_meta` does NOT do

- It does not compute anything expensive. `current_time` is a syscall;
  future fields should either be cheap reads of already-maintained counters
  (e.g. `session._latest_input_tokens`, `session._system_prompt_tokens`) or
  be memoised. Avoid any per-turn tokenizer pass.
- It does not mutate agent state.
- It does not emit logs. Callers do their own logging.

## Testing

- Unit tests for `meta_block.py`:
  - `build_meta` with `time_awareness=True` / `False` returns expected keys
  - `render_meta` produces the identical strings that the previous
    `system.current_time` path produced (snapshot test against all three
    locales)
  - `stamp_meta` merges keys in place and always writes `_elapsed_ms`
- Regression tests:
  - Existing `test_time_awareness_mail.py` and any tool-result snapshot tests
    continue to pass unchanged.

## Migration / Rollout

This is a pure refactor with no config surface change. One commit swaps the
two call sites and removes the now-unused `now_iso` / `stamp_tool_result`
helpers (or keeps them as thin shims for one release if other code imports
them — grep first).

## Follow-up: field curation (separate spec)

Once the infra is in, a second spec will decide what fields actually live in
the block. User has already flagged they want context-window breakdown
(system prompt tokens, tools tokens, session-history tokens). Additional
candidates to discuss there: turn count, stamina, molt pressure, etc. The
point of landing the infra first is that each of those becomes a one-line
addition in `build_meta` + a formatting choice in `render_meta`, rather than
a fresh injection-site question.
