# Meta-Block Context-Window Fields

**Status:** Design approved
**Date:** 2026-04-20
**Depends on:** `2026-04-20-meta-block-infra-design.md` (already merged on branch `feature/meta-block-infra`)

## Problem

The meta-block infra shipped one field: `current_time`. That surfaces *when* the
agent is, but not *where* it stands in its context window. The agent cannot
reason about memory pressure from the text-input prefix or from any tool
result; it only sees a context-usage number when the molt-pressure threshold
fires, and even then only as an opaque percentage.

We want the agent to always see a token-level breakdown of what is consuming
its context window, so it can reason about molt timing, prune its own notes,
shed capabilities, or defer work — before the automatic pressure system has to
escalate.

## Goals

- Surface three numbers on every text input and every tool result:
  - **`system_tokens`** — the fixed floor: `system.md` *minus* the context
    section, plus tool-schema tokens. Together this is the constitutional
    cost the agent cannot shrink mid-turn without `system.refresh` or
    capability removal.
  - **`context_tokens`** — the growing memory: the `context` section
    (≈ `context.md` on disk) plus in-memory chat history since the last
    `_flush_context_to_prompt`.
  - **`context_usage`** — `(system_tokens + context_tokens) / limit`,
    where `limit` is `config.context_limit or chat.context_window()`.
- Render these in the text-input prefix as a single compact line per locale.
- Include them as structured JSON keys on every tool result (via the
  existing `stamp_meta` path — no extra work; they flow automatically once
  `build_meta` emits them).
- Keep the full detailed decomposition (system.md broken out from tools,
  context.md broken out from session, provider-reported wire tokens,
  token-ledger totals) on the existing `system(action=show)` intrinsic —
  NOT on every turn.

## Non-Goals

- Changing the molt-pressure threshold system (`base_agent.py:1095-1123`).
  It continues to use `session.get_context_pressure()`. The new fields are
  surfaced to the LLM; the existing automatic ladder stays.
- Changing how `context.md` is built, flushed, or persisted.
- Surfacing the full breakdown (tools separate from system prompt, etc.)
  on the meta line. That lives in `system.show` only.
- Adding more i18n keys. We extend the existing `system.current_time`
  template and add one new key `system.context_breakdown` for the
  composed sub-fragment.

## Design

### Meta dict shape

`build_meta(agent)` returns:

```python
{
    "current_time": "2026-04-20T10:15:23-07:00",  # as today, when time-aware
    "system_tokens": 4720,      # int, >=0 — fixed-cost floor
    "context_tokens": 9450,     # int, >=0 — growing memory
    "context_usage": 0.071,     # float in [0, 1]; -1.0 sentinel if unavailable
}
```

Behaviour under failure / unavailability:

- **Before the first API response and before `_update_token_decomposition`
  has run once** — token fields are unknown. `build_meta` emits `-1` for
  each integer and `-1.0` for `context_usage`. `render_meta` detects the
  sentinel and renders the locale-specific "unknown" word.
- **Time-blind agents** — `current_time` is omitted (existing behaviour).
  Context fields are still emitted and still rendered (time-veil only
  covers wall-clock, not token accounting). This is a design choice —
  if you later want context fields also gated by `time_awareness`, the
  gate goes into `build_meta` centrally.

### Computation

`build_meta` pulls already-cached values off the agent's `SessionManager`:

| Field | Source | Cost |
|---|---|---|
| `system_tokens` | `session._system_prompt_tokens - session._context_section_tokens + session._tools_tokens` | O(1) read after one-time recompute |
| `context_tokens` | `session._context_section_tokens + chat_interface.estimate_context_tokens()` | O(1) cached read + O(n_history) scan per turn |
| `context_usage` | `(system_tokens + context_tokens) / (config.context_limit or chat.context_window())` | O(1) |

One new cached value is needed on `SessionManager`:

- `self._context_section_tokens: int = 0`

Recomputed in `_update_token_decomposition()` alongside the existing two:

```python
def _update_token_decomposition(self) -> None:
    self._system_prompt_tokens = count_tokens(self._build_system_prompt_fn())
    self._tools_tokens = count_tool_tokens(self._build_tool_schemas_fn())
    self._context_section_tokens = count_tokens(self._read_context_section_fn())
    self._token_decomp_dirty = False
```

`_read_context_section_fn` is a new callback passed into `SessionManager`'s
constructor by `BaseAgent`, returning the current content of the `"context"`
section (or `""` when absent). This keeps `SessionManager` free of any direct
`prompt_manager` coupling.

Existing dirtiness flag `_token_decomp_dirty` already fires whenever the
prompt changes (`BaseAgent.update_system_prompt`), so the new cached value
gets the same invalidation for free.

`chat_interface.estimate_context_tokens()` is already used by
`get_context_pressure()`'s fallback path and by `eigen`/`soul` intrinsics.
We call it once per turn at `build_meta` time — same cost as today's fallback.

### Render — text-input prefix

The existing `system.current_time` i18n template is **extended** (not
replaced, not deprecated) to include the context slot:

```json
// en.json
"system.current_time": "[Current time: {time} | context: {ctx}]",

// zh.json
"system.current_time": "[此时：{time} | 上下文：{ctx}]",

// wen.json
"system.current_time": "[此时：{time} | 上下文：{ctx}]",
```

Note `zh.json` changes `当前时间` → `此时` to match wen. This is a small
user-visible change in the zh locale, deliberate for consistency across the
two Chinese registers.

`render_meta` composes `{ctx}` itself using a new i18n key
`system.context_breakdown`:

```json
// en.json
"system.context_breakdown": "{pct} (sys {sys} + ctx {ctx})",

// zh.json & wen.json
"system.context_breakdown": "{pct} (系统 {sys} + 对话 {ctx})",
```

With a locale-specific "unknown" sentinel:

```json
// en.json
"system.context_unknown": "unavailable",

// zh.json & wen.json
"system.context_unknown": "未知",
```

`render_meta` logic:

```python
def render_meta(agent, meta: dict) -> str:
    if not meta:
        return ""
    time_val = meta.get("current_time", "")
    ctx_val = _render_context_fragment(agent, meta)
    # Both present: extended form.
    # Only time present: current_time template with ctx_val == ""
    # (handled by i18n template choice — see below).
    if time_val or ctx_val:
        return _t(agent._config.language, "system.current_time",
                  time=time_val, ctx=ctx_val)
    return ""


def _render_context_fragment(agent, meta: dict) -> str:
    usage = meta.get("context_usage")
    if usage is None:
        return ""  # no context data at all — caller handles
    if usage < 0:
        return _t(agent._config.language, "system.context_unknown")
    return _t(
        agent._config.language,
        "system.context_breakdown",
        pct=f"{usage:.1%}",
        sys=meta.get("system_tokens", 0),
        ctx=meta.get("context_tokens", 0),
    )
```

### The "time-blind but context-aware" edge case

When `time_awareness=False` but context fields are present, `meta` contains
`system_tokens`/`context_tokens`/`context_usage` but no `current_time`.
Rendering with the template `"[Current time: {time} | context: {ctx}]"`
would produce `"[Current time:  | context: 7.1% …]"` — awkward empty
slot.

**Decision**: we accept this today and defer. The realistic user of
`time_awareness=False` is already opting into a compromised rendering, and
the existing meta-block-infra already documented a similar case
(time-blind agents lose the `[Current time: ]` prefix entirely because
the whole meta dict was empty). Now the dict isn't empty, so we *do*
render — with an empty time slot. A follow-up can add a
time-blind-specific template if anyone complains.

Alternative considered and rejected: switching to a two-template scheme
(`system.meta_full` vs `system.meta_context_only`) — that's Option C from
the design discussion, now deferred.

### Example renders

```
en:     [Current time: 2026-04-20T10:15:23-07:00 | context: 7.1% (sys 4720 + ctx 9450)]
zh/wen: [此时：2026-04-20T10:15:23-07:00 | 上下文：7.1% (系统 4720 + 对话 9450)]

Before tokens available:
en:     [Current time: 2026-04-20T10:15:23-07:00 | context: unavailable]
zh/wen: [此时：2026-04-20T10:15:23-07:00 | 上下文：未知]

Time-blind with tokens available:
en:     [Current time:  | context: 7.1% (sys 4720 + ctx 9450)]
zh/wen: [此时： | 上下文：7.1% (系统 4720 + 对话 9450)]
```

### Render — tool result

No render work. `stamp_meta` already merges the full meta dict onto tool
results. Once `build_meta` emits the three new keys, every tool result
automatically gets them as structured JSON fields:

```json
{
  "status": "ok",
  "...": "...",
  "current_time": "2026-04-20T10:15:23-07:00",
  "system_tokens": 4720,
  "context_tokens": 9450,
  "context_usage": 0.071,
  "_elapsed_ms": 42
}
```

This is deliberate — the JSON surface is the machine-readable contract; the
prose prefix is the human-readable one.

### `system.show` — the full breakdown

The existing `system(action=show)` intrinsic gets extended to also render
the full context decomposition:

```
context:
  total:         14,170 / 200,000 (7.1%)
  fixed:
    system.md:    4,200  (system prompt minus context section)
    tools:          520  (tool schemas)
  growing:
    context.md:   8,100  (persisted conversation since last molt)
    session:      1,350  (in-memory turns since last flush)
  provider:
    wire tokens: 14,180  (most recent API call, server-reported)
```

All numbers are already available on `SessionManager`; `system.show` just
renders them. This is where the agent goes for detail when the compact
meta line prompts investigation.

(`system.show` implementation is scoped to this spec but intentionally
simple — formatting-only.)

## Testing

### Unit

`tests/test_meta_block.py`:

- `build_meta` with context fields present emits all four keys with the
  right types.
- `build_meta` with `_system_prompt_tokens == 0` (decomp never ran) emits
  `-1` sentinels.
- `render_meta` with the sentinels renders the "unavailable" / "未知" word.
- `render_meta` with real values renders the extended prefix across all
  three locales.
- `stamp_meta` merges all context fields onto a tool result (this already
  works — covered by forward-extensibility test; add a concrete
  assertion for the new keys).

`tests/test_session.py`:

- `_update_token_decomposition` computes `_context_section_tokens` from
  the callback.
- Changing the context section marks `_token_decomp_dirty` and triggers
  recompute next turn.

### Integration

`tests/test_base_agent.py`:

- A full `_handle_request` turn produces a text-input log line containing
  both `current_time` and the context breakdown.

### i18n

`tests/test_i18n.py`:

- `system.current_time` with both `time` and `ctx` kwargs renders
  correctly in all three locales.
- Existing single-arg-only callers (there are none after this refactor)
  would render `{ctx}` as literal — not a regression because all call
  sites pass both args after this change. A test documents that.
- `system.context_breakdown` and `system.context_unknown` exist and
  render correctly in all three locales.

## Migration / Rollout

- `system.current_time` template changes in all three locales. Any tests
  asserting the old `"[当前时间：{time}]"` literal get updated to the new
  extended form. Sweep: `tests/test_meta_block.py`, `tests/test_i18n.py`
  (and `grep` for `当前时间` / `Current time:` to catch stragglers).
- Existing agents running on disk don't notice — there is no persisted
  state tied to the old template.
- No config surface change; no user-facing opt-in.

## Open questions (deliberately left for follow-up specs, not this one)

- Should `context_usage` also be the molt-pressure source of truth?
  Currently `get_context_pressure()` uses server-reported
  `_latest_input_tokens / context_window`. Our computed
  `context_usage = (system + context) / limit` differs slightly.
  Unifying them is a separate discussion.
- Token-cost budget hints on tool results (e.g. "this tool result is
  2,100 tokens") — not part of the meta block; would go on
  `_elapsed_ms`'s sibling slot.
- Per-field compression hints ("tools are 520 tokens — consider shedding
  capability X") — an intrinsic skill, not a meta-block concern.
