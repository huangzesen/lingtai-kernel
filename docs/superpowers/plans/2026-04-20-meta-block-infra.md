# Meta-Block Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify the two ad-hoc per-turn metadata injection sites (text-input prefix and tool-result stamp) onto a single curated surface in `meta_block.py`, preserving today's observable behavior exactly while opening one place to add future fields.

**Architecture:** New module `src/lingtai_kernel/meta_block.py` owns three functions — `build_meta(agent) -> dict`, `render_meta(agent, meta) -> str`, `stamp_meta(result, meta, elapsed_ms) -> dict`. `BaseAgent._handle_request` calls `build_meta` once per turn, prepends `render_meta` output to the text content, and passes a `meta_fn` to `ToolExecutor`. `ToolExecutor` drops its `time_awareness`/`timezone_awareness` flags and calls `meta_fn` + `stamp_meta` instead of `stamp_tool_result`. Existing `time_veil` helpers stay — `build_meta` consults them internally.

**Tech Stack:** Python 3.11+, existing `lingtai_kernel` package, pytest.

---

## Pre-flight

Working directory for all tasks: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/.worktrees/meta-block-infra`

Test command prefix: `PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest`

## File Structure

| File                                                    | Purpose                                                                                                           |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| **Create:** `src/lingtai_kernel/meta_block.py`          | New home for `build_meta` / `render_meta` / `stamp_meta`. Single curation surface.                                |
| **Create:** `tests/test_meta_block.py`                  | Unit tests for the new module.                                                                                    |
| **Modify:** `src/lingtai_kernel/base_agent.py`          | Swap `now_iso` + `system.current_time` line for `build_meta` + `render_meta`. Pass `meta_fn` to `ToolExecutor`. |
| **Modify:** `src/lingtai_kernel/tool_executor.py`       | Replace `time_awareness`/`timezone_awareness` ctor args with `meta_fn`. Call `stamp_meta` instead of `stamp_tool_result`. |
| **Modify:** `src/lingtai_kernel/tool_timing.py`         | Keep `ToolTimer`. Delete `stamp_tool_result` (callers migrated).                                                |
| **Modify:** `tests/test_tool_timing.py`                 | Delete tests for `stamp_tool_result` (replaced by `test_meta_block.py`). Keep any `ToolTimer` tests if present.  |

Note: `time_veil.py` stays untouched. `build_meta` imports `now_iso` from it.

---

### Task 1: Create `meta_block.py` with `build_meta`

**Files:**
- Create: `src/lingtai_kernel/meta_block.py`
- Test: `tests/test_meta_block.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_meta_block.py`:

```python
"""Tests for meta_block — unified per-turn metadata injection."""
from __future__ import annotations

import re
from types import SimpleNamespace

from lingtai_kernel.meta_block import build_meta


def _fake_agent(*, time_awareness: bool = True, timezone_awareness: bool = True):
    """Minimal agent stand-in: build_meta only reads agent._config.*."""
    return SimpleNamespace(
        _config=SimpleNamespace(
            time_awareness=time_awareness,
            timezone_awareness=timezone_awareness,
        )
    )


def test_build_meta_time_aware_local_tz_has_offset():
    agent = _fake_agent(time_awareness=True, timezone_awareness=True)
    meta = build_meta(agent)
    assert "current_time" in meta
    ts = meta["current_time"]
    assert not ts.endswith("Z"), f"expected local offset, got {ts!r}"
    assert re.search(r"[+-]\d{2}:\d{2}$", ts), f"no ±HH:MM suffix in {ts!r}"


def test_build_meta_time_aware_utc_uses_z_suffix():
    agent = _fake_agent(time_awareness=True, timezone_awareness=False)
    meta = build_meta(agent)
    assert meta["current_time"].endswith("Z")


def test_build_meta_time_blind_returns_empty_dict():
    agent = _fake_agent(time_awareness=False)
    meta = build_meta(agent)
    assert meta == {}


def test_build_meta_time_blind_regardless_of_timezone_awareness():
    # time_awareness=False short-circuits even when timezone_awareness=True.
    agent = _fake_agent(time_awareness=False, timezone_awareness=True)
    assert build_meta(agent) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: `ModuleNotFoundError: No module named 'lingtai_kernel.meta_block'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/lingtai_kernel/meta_block.py`:

```python
"""Unified per-turn metadata injection.

Single source of truth for "what the agent sees about its own runtime state
on every turn." Both injection sites — text-input prefix (in BaseAgent) and
tool-result stamp (in ToolExecutor) — read from here.

Curate carefully: every field added to `build_meta` ships on every text input
and every tool result.
"""
from __future__ import annotations

from .time_veil import now_iso


def build_meta(agent) -> dict:
    """Return the current meta-data snapshot for the agent.

    Respects ``agent._config.time_awareness`` / ``timezone_awareness``
    internally; callers never need to special-case those flags.

    When the agent is time-blind and no other meta fields are curated in,
    returns ``{}``.
    """
    meta: dict = {}
    ts = now_iso(agent)
    if ts:
        meta["current_time"] = ts
    return meta
```

- [ ] **Step 4: Run the tests to verify they pass**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel/.worktrees/meta-block-infra
git add src/lingtai_kernel/meta_block.py tests/test_meta_block.py
git commit -m "$(cat <<'EOF'
feat(meta): introduce build_meta as the curated per-turn metadata surface

First piece of the unified meta-block infra. build_meta produces the dict
that later tasks will surface to the LLM via both the text-input prefix
and tool-result stamp paths. Currently emits only current_time (respecting
time_veil), preserving today's behaviour.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Add `render_meta` for the text-input prefix

**Files:**
- Modify: `src/lingtai_kernel/meta_block.py`
- Test: `tests/test_meta_block.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meta_block.py`:

```python
from lingtai_kernel.meta_block import render_meta


def _fake_agent_with_lang(lang: str, *, time_awareness: bool = True):
    return SimpleNamespace(
        _config=SimpleNamespace(
            time_awareness=time_awareness,
            timezone_awareness=True,
            language=lang,
        )
    )


def test_render_meta_empty_dict_returns_empty_string():
    agent = _fake_agent_with_lang("en")
    assert render_meta(agent, {}) == ""


def test_render_meta_en_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("en")
    meta = {"current_time": "2026-04-20T10:15:23-07:00"}
    assert render_meta(agent, meta) == "[Current time: 2026-04-20T10:15:23-07:00]"


def test_render_meta_zh_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("zh")
    meta = {"current_time": "2026-04-20T10:15:23-07:00"}
    assert render_meta(agent, meta) == "[当前时间：2026-04-20T10:15:23-07:00]"


def test_render_meta_wen_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("wen")
    meta = {"current_time": "2026-04-20T10:15:23-07:00"}
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00]"
```

- [ ] **Step 2: Run the tests to verify they fail**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: 4 `ImportError: cannot import name 'render_meta'`.

- [ ] **Step 3: Implement `render_meta`**

Edit `src/lingtai_kernel/meta_block.py`. Add this import at the top next to the `now_iso` import:

```python
from .i18n import t as _t
```

Then append to the bottom of the file:

```python
def render_meta(agent, meta: dict) -> str:
    """Render the meta dict as the line prepended to text input.

    Returns '' when the meta dict is empty — callers should treat '' as
    "no prefix" and skip concatenation.

    Today this only knows how to render ``current_time`` (via the existing
    ``system.current_time`` i18n key). Future fields are composed here.
    """
    if not meta:
        return ""
    if "current_time" in meta:
        return _t(agent._config.language, "system.current_time", time=meta["current_time"])
    return ""
```

- [ ] **Step 4: Run the tests to verify they pass**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/lingtai_kernel/meta_block.py tests/test_meta_block.py
git commit -m "$(cat <<'EOF'
feat(meta): add render_meta for the text-input prefix path

Produces the [Current time: …] / [当前时间：…] / [此时：…] string that will
replace the hand-rolled one in BaseAgent._handle_request. Empty meta →
empty string, so callers can opt out of the prefix cleanly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add `stamp_meta` for the tool-result path

**Files:**
- Modify: `src/lingtai_kernel/meta_block.py`
- Test: `tests/test_meta_block.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meta_block.py`:

```python
from lingtai_kernel.meta_block import stamp_meta


def test_stamp_meta_writes_meta_keys_and_elapsed_ms_in_place():
    result = {"status": "ok"}
    out = stamp_meta(result, {"current_time": "2026-04-20T10:15:23-07:00"}, 42)
    assert out is result  # in-place
    assert out["current_time"] == "2026-04-20T10:15:23-07:00"
    assert out["_elapsed_ms"] == 42
    assert out["status"] == "ok"


def test_stamp_meta_empty_meta_omits_both_keys():
    # Time-blind case: empty meta ⇒ no current_time AND no _elapsed_ms.
    # Preserves stamp_tool_result(time_awareness=False) behavior verbatim.
    result = {"status": "ok"}
    out = stamp_meta(result, {}, 42)
    assert out is result
    assert "current_time" not in out
    assert "_elapsed_ms" not in out
    assert out == {"status": "ok"}


def test_stamp_meta_future_fields_are_merged_through():
    # Forward-compatibility: every key in meta lands on the result.
    result = {"status": "ok"}
    meta = {"current_time": "2026-04-20T10:15:23-07:00", "future_field": 123}
    stamp_meta(result, meta, 7)
    assert result["future_field"] == 123
    assert result["current_time"] == "2026-04-20T10:15:23-07:00"
    assert result["_elapsed_ms"] == 7
```

- [ ] **Step 2: Run the tests to verify they fail**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: `ImportError: cannot import name 'stamp_meta'`.

- [ ] **Step 3: Implement `stamp_meta`**

Append to `src/lingtai_kernel/meta_block.py`:

```python
def stamp_meta(result: dict, meta: dict, elapsed_ms: int) -> dict:
    """Merge meta fields into a tool-result dict (in place) and return it.

    When ``meta`` is empty, neither the meta fields nor ``_elapsed_ms`` are
    written — matching the pre-existing behavior of
    ``stamp_tool_result(time_awareness=False)`` exactly.

    ``_elapsed_ms`` is stamped here (rather than inside ``build_meta``)
    because it is a per-tool-call measurement — not per-turn agent state —
    and it would be wrong for the same value to appear on the text-input
    prefix.
    """
    if not meta:
        return result
    for k, v in meta.items():
        result[k] = v
    result["_elapsed_ms"] = elapsed_ms
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/lingtai_kernel/meta_block.py tests/test_meta_block.py
git commit -m "$(cat <<'EOF'
feat(meta): add stamp_meta for the tool-result path

Merges meta fields plus _elapsed_ms into a tool-result dict in place.
Empty-meta short-circuit matches stamp_tool_result(time_awareness=False)
behaviour exactly, so the next task can swap call sites without changing
what the LLM sees.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Migrate `ToolExecutor` to `meta_fn` + `stamp_meta`

**Files:**
- Modify: `src/lingtai_kernel/tool_executor.py`
- Test: `tests/test_tool_executor.py` (existing tests must keep passing; add one new test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_executor.py`:

```python
def test_tool_executor_uses_meta_fn_for_stamping(monkeypatch):
    """ToolExecutor calls meta_fn once per tool call and merges the returned
    dict onto the result alongside _elapsed_ms."""
    from lingtai_kernel.tool_executor import ToolExecutor
    from lingtai_kernel.loop_guard import LoopGuard
    from lingtai_kernel.llm.base import ToolCall

    meta_calls = {"n": 0}

    def meta_fn():
        meta_calls["n"] += 1
        return {"current_time": "FAKE-TS", "future_field": meta_calls["n"]}

    def dispatch(tc):
        return {"status": "ok", "echo": tc.args}

    def make_result(name, result, **kw):
        return {"name": name, "result": result, **kw}

    exe = ToolExecutor(
        dispatch_fn=dispatch,
        make_tool_result_fn=make_result,
        guard=LoopGuard(max_total_calls=10, dup_free_passes=2, dup_hard_block=8),
        known_tools={"noop"},
        parallel_safe_tools=set(),
        logger_fn=None,
        meta_fn=meta_fn,
    )
    results, intercepted, _ = exe.execute([ToolCall(id="c1", name="noop", args={})])
    assert not intercepted
    assert meta_calls["n"] == 1
    payload = results[0]["result"]
    assert payload["current_time"] == "FAKE-TS"
    assert payload["future_field"] == 1
    assert "_elapsed_ms" in payload
```

- [ ] **Step 2: Run the test to verify it fails**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_tool_executor.py::test_tool_executor_uses_meta_fn_for_stamping -v
```

Expected: `TypeError: ToolExecutor.__init__() got an unexpected keyword argument 'meta_fn'`.

- [ ] **Step 3: Update `ToolExecutor` imports and `__init__`**

In `src/lingtai_kernel/tool_executor.py`, replace the import line:

```python
from .tool_timing import ToolTimer, stamp_tool_result
```

with:

```python
from .meta_block import stamp_meta
from .tool_timing import ToolTimer
```

Then replace the `__init__` method signature and body (currently lines ~54-74) with:

```python
    def __init__(
        self,
        dispatch_fn: Callable[[ToolCall], Any],
        make_tool_result_fn: Callable,
        guard: LoopGuard,
        known_tools: set[str] | None = None,
        parallel_safe_tools: set[str] | None = None,
        logger_fn: Callable | None = None,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
        meta_fn: Callable[[], dict] | None = None,
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._make_tool_result_fn = make_tool_result_fn
        self._guard = guard
        self._known_tools = known_tools or set()
        self._parallel_safe_tools = parallel_safe_tools or set()
        self._logger_fn = logger_fn
        self._max_result_bytes = max_result_bytes
        self._meta_fn = meta_fn or (lambda: {})
```

- [ ] **Step 4: Replace the three `stamp_tool_result` call sites**

Find each of these three calls in `src/lingtai_kernel/tool_executor.py` (currently near lines 172, 208, 309):

```python
stamp_tool_result(result, timer.elapsed_ms, time_awareness=self._time_awareness, timezone_awareness=self._timezone_awareness)
```

and

```python
stamp_tool_result(err_result, timer.elapsed_ms, time_awareness=self._time_awareness, timezone_awareness=self._timezone_awareness)
```

Replace each with, respectively:

```python
stamp_meta(result, self._meta_fn(), timer.elapsed_ms)
```

and

```python
stamp_meta(err_result, self._meta_fn(), timer.elapsed_ms)
```

(There are three such calls total: two `result` sites and one `err_result` site. Use `grep -n stamp_tool_result src/lingtai_kernel/tool_executor.py` to confirm you updated them all.)

- [ ] **Step 5: Verify no leftover references**

```bash
grep -n "stamp_tool_result\|_time_awareness\|_timezone_awareness" src/lingtai_kernel/tool_executor.py
```

Expected: no output (all references removed).

- [ ] **Step 6: Run the tool_executor tests**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_tool_executor.py -v
```

Expected: all tests pass, including the new `test_tool_executor_uses_meta_fn_for_stamping`.

If existing tool_executor tests constructed `ToolExecutor` with `time_awareness=` / `timezone_awareness=` kwargs, they will now fail with `TypeError`. In each such failing test, simply delete those kwargs — the default `meta_fn=None` gives the empty-meta behaviour that matches the old `time_awareness=False` path (which is what those tests presumably relied on). If a test genuinely needs a stamped `current_time`, pass `meta_fn=lambda: {"current_time": "T"}`.

- [ ] **Step 7: Commit**

```bash
git add src/lingtai_kernel/tool_executor.py tests/test_tool_executor.py
git commit -m "$(cat <<'EOF'
refactor(tool_executor): inject meta via meta_fn, drop time-awareness flags

Replaces the time_awareness/timezone_awareness constructor args and the
three stamp_tool_result call sites with a single meta_fn callback + stamp_meta.
ToolExecutor is now agnostic of the time-veil policy; BaseAgent (next task)
owns it by supplying a meta_fn that calls build_meta(self).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Migrate `BaseAgent._handle_request`

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py`
- Test: `tests/test_base_agent.py`, `tests/test_time_awareness_mail.py`, `tests/test_session.py` (regression check only)

- [ ] **Step 1: Verify baseline is still green before editing**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py tests/test_time_awareness_mail.py tests/test_session.py tests/test_time_veil.py -q
```

Expected: all pass. (If not, stop and investigate — don't proceed.)

- [ ] **Step 2: Update the `ToolExecutor` construction in `_handle_request`**

In `src/lingtai_kernel/base_agent.py`, find `_handle_request` (currently starting at line ~1068). The `ToolExecutor(...)` call currently ends with:

```python
            logger_fn=self._log,
            time_awareness=self._config.time_awareness,
            timezone_awareness=self._config.timezone_awareness,
        )
```

Replace those last two kwargs so the block reads:

```python
            logger_fn=self._log,
            meta_fn=lambda: build_meta(self),
        )
```

- [ ] **Step 3: Replace the text-prefix line**

Still inside `_handle_request`, find the block (currently around lines 1088-1129):

```python
        content = self._pre_request(msg)
        current_time = now_iso(self)

        # Molt pressure — warn agent when context is getting full
        ...
        content = f"{_t(self._config.language, 'system.current_time', time=current_time)}\n\n{content}"
```

Change the two affected lines:

**(a)** Replace `current_time = now_iso(self)` with `meta = build_meta(self)`.

**(b)** Replace the final `content = f"{_t(...'system.current_time'..., time=current_time)}\n\n{content}"` line with:

```python
        prefix = render_meta(self, meta)
        if prefix:
            content = f"{prefix}\n\n{content}"
```

Leave the molt-pressure block between them exactly as-is.

- [ ] **Step 4: Add the new imports**

Near the top of `src/lingtai_kernel/base_agent.py`, find the line:

```python
from .time_veil import now_iso, scrub_time_fields
```

Change it to (keep `now_iso` for the two remaining call sites at lines ~934 and ~1697 and `scrub_time_fields` for its existing use):

```python
from .meta_block import build_meta, render_meta
from .time_veil import now_iso, scrub_time_fields
```

- [ ] **Step 5: Sanity-grep leftover refs**

```bash
grep -n "system\.current_time\|now_iso" src/lingtai_kernel/base_agent.py
```

Expected: exactly two `now_iso` call sites remain (the stuck-revive `ts = now_iso(self)` around line 934 and the runtime snapshot `"current_time": now_iso(self)` around line 1697). Zero `system.current_time` references.

- [ ] **Step 6: Smoke-test the module imports cleanly**

```bash
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -c "import lingtai_kernel.base_agent"
```

Expected: no output (clean import).

- [ ] **Step 7: Run the relevant test suites**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py tests/test_time_awareness_mail.py tests/test_session.py tests/test_time_veil.py tests/test_tool_executor.py tests/test_meta_block.py -v
```

Expected: all pass.

If any test asserts against the literal string `"[Current time: "` / `"[当前时间："` / `"[此时："`, it will continue to pass — `render_meta` produces byte-identical output for those. If any test inspects a time-blind agent's text_input log and previously saw `"[Current time: ]\n\n..."`, it will now see just `"..."` without the empty prefix. That is the **intentional behaviour change** of this refactor (flagged in the spec). If such a test exists, update its assertion to match the new cleaner output; record the change in the commit message.

- [ ] **Step 8: Commit**

```bash
git add src/lingtai_kernel/base_agent.py tests/
git commit -m "$(cat <<'EOF'
refactor(base_agent): route per-turn metadata through build_meta/render_meta

_handle_request now calls build_meta(self) once and hands the result to
both the text-input prefix path (via render_meta) and the ToolExecutor
(via meta_fn). The stuck-revive and runtime snapshot call sites still use
now_iso directly — those are scoped, non-per-turn uses that don't belong
in the meta block.

Side effect: time-blind agents no longer get a "[Current time: ]" empty
prefix on text input; render_meta suppresses the line entirely. Documented
in the spec and intentional.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Remove the obsolete `stamp_tool_result` helper

**Files:**
- Modify: `src/lingtai_kernel/tool_timing.py`
- Modify: `tests/test_tool_timing.py`

- [ ] **Step 1: Confirm no remaining callers**

```bash
grep -rn "stamp_tool_result" src/ tests/
```

Expected: no output. If anything matches, stop — Task 4 missed a site.

- [ ] **Step 2: Trim `tool_timing.py` to just `ToolTimer`**

Replace the entire contents of `src/lingtai_kernel/tool_timing.py` with:

```python
"""Tool execution timing helpers."""
import time


class ToolTimer:
    """Context manager for timing tool execution."""
    def __init__(self):
        self._start = 0.0
        self.elapsed_ms = 0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.elapsed_ms = int((time.monotonic() - self._start) * 1000)
        return False
```

- [ ] **Step 3: Delete the stale `stamp_tool_result` tests**

Replace the entire contents of `tests/test_tool_timing.py` with:

```python
"""Tests for ToolTimer."""
import time

from lingtai_kernel.tool_timing import ToolTimer


def test_tool_timer_measures_elapsed_ms():
    with ToolTimer() as timer:
        time.sleep(0.01)
    assert timer.elapsed_ms >= 10
    assert timer.elapsed_ms < 500  # generous upper bound to avoid flakes
```

(The seven `stamp_tool_result` tests are replaced in full by `tests/test_meta_block.py` — behavior coverage has migrated with the implementation.)

- [ ] **Step 4: Smoke-test imports**

```bash
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -c "import lingtai_kernel.tool_timing; import lingtai_kernel.tool_executor; import lingtai_kernel.base_agent; import lingtai_kernel.meta_block"
```

Expected: no output.

- [ ] **Step 5: Run the full relevant test subset**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_tool_timing.py tests/test_tool_executor.py tests/test_meta_block.py tests/test_base_agent.py tests/test_time_awareness_mail.py tests/test_session.py tests/test_time_veil.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/tool_timing.py tests/test_tool_timing.py
git commit -m "$(cat <<'EOF'
chore(meta): drop stamp_tool_result, the last pre-meta-block helper

All callers migrated to stamp_meta via the meta_fn wiring in ToolExecutor.
Behaviour coverage now lives in tests/test_meta_block.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Final regression sweep

**Files:** none modified — verification only.

- [ ] **Step 1: Run the targeted test set**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py tests/test_tool_timing.py tests/test_tool_executor.py tests/test_base_agent.py tests/test_time_awareness_mail.py tests/test_session.py tests/test_time_veil.py -v
```

Expected: all pass.

- [ ] **Step 2: Run the wider kernel suite, skipping pre-existing addon collection errors**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest -q \
    --ignore=tests/test_addons.py --ignore=tests/test_account.py \
    --ignore=tests/test_manager.py --ignore=tests/test_service.py
```

Expected failure count: **≤ 60** (matches the pre-existing baseline captured when the worktree was created). If higher, our refactor broke something — investigate before closing the branch.

- [ ] **Step 3: Confirm the diff is tight**

```bash
git log --oneline main..HEAD
git diff --stat main..HEAD
```

Expected: 6 commits (one per task 1-6), diff limited to the files named in the File Structure table plus the spec committed earlier.

- [ ] **Step 4: No commit — verification task.**

---

## Self-Review Notes

- **Spec coverage:** infra section → Tasks 1-3 (`build_meta`/`render_meta`/`stamp_meta`); integration points → Tasks 4-5; obsolete-helper removal → Task 6; testing section → every task has a test step. Migration section (remove `stamp_tool_result`) → Task 6. Field-curation follow-up is explicitly out of scope per spec.
- **Behavior preservation:** The one intentional observable change is that time-blind agents no longer see the empty `[Current time: ]` prefix. Called out in Task 5 Step 7 and in the commit body.
- **`_elapsed_ms` invariant:** empty meta → `_elapsed_ms` also omitted (matches `stamp_tool_result(time_awareness=False)` verbatim). Spec originally said `_elapsed_ms` always writes; the plan corrects that.
- **Type consistency:** `build_meta(agent) -> dict`, `render_meta(agent, meta) -> str`, `stamp_meta(result, meta, elapsed_ms) -> dict`, `meta_fn: Callable[[], dict]` — all consistent across tasks.
