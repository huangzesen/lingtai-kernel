# Meta-Block Context-Window Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three context-window fields (`system_tokens`, `context_tokens`, `context_usage`) to `build_meta`, render them in the text-input prefix (en/zh/wen), and extend `system(action=show)` with the full breakdown.

**Architecture:** `SessionManager` gains one new cached value `_context_section_tokens` recomputed alongside `_system_prompt_tokens` / `_tools_tokens` via a new `read_context_section_fn` callback from `BaseAgent`. `build_meta` assembles the three new fields from already-cached session counters. `render_meta` composes a context fragment via two new i18n keys. `status()` grows a finer-grained `context` block.

**Tech Stack:** Python 3.11+, `lingtai_kernel` package, pytest.

---

## Pre-flight

Working directory for all tasks: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/.worktrees/meta-context-fields`

Branch: `feature/meta-context-fields` (stacked on `feature/meta-block-infra`; do NOT merge/rebase onto main here).

Test command prefix:

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest
```

Baseline (HEAD at `e994b85`): targeted test files `test_meta_block.py test_tool_executor.py test_session.py test_base_agent.py test_time_awareness_mail.py test_time_veil.py test_tool_timing.py test_i18n.py` must all pass.

## File Structure

| File                                                       | Purpose                                                                                                                               |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| **Modify:** `src/lingtai_kernel/session.py`                | Add `_context_section_tokens` state + `read_context_section_fn` ctor arg. Extend `_update_token_decomposition` + `get_token_usage`. |
| **Modify:** `src/lingtai_kernel/base_agent.py`             | Pass the new callback at `SessionManager` construction. Extend `status()` breakdown with finer granularity.                        |
| **Modify:** `src/lingtai_kernel/meta_block.py`             | `build_meta` adds the three new fields; `render_meta` composes the context fragment with i18n + sentinel handling.                 |
| **Modify:** `src/lingtai_kernel/i18n/{en,zh,wen}.json`     | Extend `system.current_time`; add `system.context_breakdown`, `system.context_unknown`.                                           |
| **Modify:** `tests/test_session.py`                        | Cover `_context_section_tokens` computation + cache-dirty behaviour.                                                                |
| **Modify:** `tests/test_meta_block.py`                     | Cover new `build_meta` fields + sentinel + `render_meta` variants in all three locales.                                              |
| **Modify:** `tests/test_i18n.py`                           | New keys render correctly; updated `system.current_time` renders correctly.                                                         |
| **Modify:** `tests/test_base_agent.py`                     | Extended `status()` breakdown returns the expected shape.                                                                             |

`prompt.py`, `tool_executor.py`, `tool_timing.py`, and `context_serializer.py` are NOT touched.

---

### Task 1: Add `_context_section_tokens` state + callback to `SessionManager`

**Files:**
- Modify: `src/lingtai_kernel/session.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session.py`:

```python
def test_session_update_decomposition_computes_context_section_tokens():
    """_update_token_decomposition calls read_context_section_fn and tokenizes
    its result into _context_section_tokens."""
    from unittest.mock import MagicMock
    from lingtai_kernel.session import SessionManager
    from lingtai_kernel.config import AgentConfig

    cfg = AgentConfig()
    sess = SessionManager(
        llm_service=MagicMock(),
        config=cfg,
        agent_name="t",
        streaming=False,
        build_system_prompt_fn=lambda: "system prompt body",
        build_tool_schemas_fn=lambda: [],
        read_context_section_fn=lambda: "some context section content",
        logger_fn=None,
    )
    sess._update_token_decomposition()
    assert sess._context_section_tokens > 0
    assert sess._token_decomp_dirty is False


def test_session_context_section_tokens_defaults_to_zero():
    from unittest.mock import MagicMock
    from lingtai_kernel.session import SessionManager
    from lingtai_kernel.config import AgentConfig

    sess = SessionManager(
        llm_service=MagicMock(),
        config=AgentConfig(),
        agent_name="t",
        streaming=False,
        build_system_prompt_fn=lambda: "",
        build_tool_schemas_fn=lambda: [],
        read_context_section_fn=lambda: "",
        logger_fn=None,
    )
    assert sess._context_section_tokens == 0


def test_session_get_token_usage_exposes_context_section_tokens():
    """get_token_usage adds ctx_context_section_tokens alongside existing keys."""
    from unittest.mock import MagicMock
    from lingtai_kernel.session import SessionManager
    from lingtai_kernel.config import AgentConfig

    sess = SessionManager(
        llm_service=MagicMock(),
        config=AgentConfig(),
        agent_name="t",
        streaming=False,
        build_system_prompt_fn=lambda: "x",
        build_tool_schemas_fn=lambda: [],
        read_context_section_fn=lambda: "y",
        logger_fn=None,
    )
    sess._update_token_decomposition()
    usage = sess.get_token_usage()
    assert "ctx_context_section_tokens" in usage
    assert usage["ctx_context_section_tokens"] >= 0
```

- [ ] **Step 2: Run the tests to verify they fail**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_session.py -v -k "context_section or get_token_usage_exposes"
```

Expected: `TypeError: SessionManager.__init__() got an unexpected keyword argument 'read_context_section_fn'` on the first two; `KeyError`/`assert "ctx_context_section_tokens" in usage` on the third.

- [ ] **Step 3: Add the ctor arg, state, and decomposition logic**

In `src/lingtai_kernel/session.py`:

**(a)** Update `SessionManager.__init__` signature. Find:

```python
    def __init__(
        self,
        *,
        llm_service: LLMService,
        config: AgentConfig,
        agent_name: str | None = None,
        streaming: bool,
        build_system_prompt_fn: Callable[[], str],
        build_tool_schemas_fn: Callable[[], list[FunctionSchema]],
        logger_fn: Callable[..., None] | None,
    ):
```

Change to:

```python
    def __init__(
        self,
        *,
        llm_service: LLMService,
        config: AgentConfig,
        agent_name: str | None = None,
        streaming: bool,
        build_system_prompt_fn: Callable[[], str],
        build_tool_schemas_fn: Callable[[], list[FunctionSchema]],
        read_context_section_fn: Callable[[], str],
        logger_fn: Callable[..., None] | None,
    ):
```

**(b)** In the body of `__init__`, find:

```python
        self._build_system_prompt_fn = build_system_prompt_fn
        self._build_tool_schemas_fn = build_tool_schemas_fn
        self._logger_fn = logger_fn
```

Insert the callback assignment immediately after `build_tool_schemas_fn`:

```python
        self._build_system_prompt_fn = build_system_prompt_fn
        self._build_tool_schemas_fn = build_tool_schemas_fn
        self._read_context_section_fn = read_context_section_fn
        self._logger_fn = logger_fn
```

**(c)** In `__init__` "Token tracking" block, find:

```python
        self._system_prompt_tokens = 0
        self._tools_tokens = 0
        self._token_decomp_dirty = True
```

Insert `_context_section_tokens`:

```python
        self._system_prompt_tokens = 0
        self._tools_tokens = 0
        self._context_section_tokens = 0
        self._token_decomp_dirty = True
```

**(d)** In `_update_token_decomposition`, find:

```python
    def _update_token_decomposition(self) -> None:
        """Recompute cached system prompt and tools token counts."""
        self._system_prompt_tokens = count_tokens(self._build_system_prompt_fn())
        self._tools_tokens = count_tool_tokens(self._build_tool_schemas_fn())
        self._token_decomp_dirty = False
```

Replace body with:

```python
    def _update_token_decomposition(self) -> None:
        """Recompute cached system prompt, tools, and context-section token counts."""
        self._system_prompt_tokens = count_tokens(self._build_system_prompt_fn())
        self._tools_tokens = count_tool_tokens(self._build_tool_schemas_fn())
        self._context_section_tokens = count_tokens(self._read_context_section_fn())
        self._token_decomp_dirty = False
```

**(e)** In `get_token_usage`, find the return block:

```python
        return {
            "input_tokens": self._total_input_tokens,
            ...
            "ctx_total_tokens": self._latest_input_tokens,
        }
```

Add `"ctx_context_section_tokens"` alongside the other `ctx_*` fields:

```python
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "thinking_tokens": self._total_thinking_tokens,
            "cached_tokens": self._total_cached_tokens,
            "total_tokens": (
                self._total_input_tokens
                + self._total_output_tokens
                + self._total_thinking_tokens
            ),
            "api_calls": self._api_calls,
            "ctx_system_tokens": self._system_prompt_tokens,
            "ctx_tools_tokens": self._tools_tokens,
            "ctx_context_section_tokens": self._context_section_tokens,
            "ctx_history_tokens": max(
                0,
                self._latest_input_tokens
                - self._system_prompt_tokens
                - self._tools_tokens,
            ),
            "ctx_total_tokens": self._latest_input_tokens,
        }
```

- [ ] **Step 4: Run the new tests**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_session.py -v -k "context_section or get_token_usage_exposes"
```

Expected: 3 passed.

- [ ] **Step 5: Run the full `test_session.py` — existing tests must still pass**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_session.py -v
```

Expected: all pass. If pre-existing tests now fail with `TypeError: ... got an unexpected keyword argument 'read_context_section_fn'`, they were constructing `SessionManager` directly without the new kwarg. Update each such test construction to pass `read_context_section_fn=lambda: ""` (default-empty). Do NOT change test logic.

- [ ] **Step 6: Run `test_base_agent.py` — BaseAgent still constructs SessionManager without the new kwarg, so this MUST fail now**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py -q
```

Expected: failures. That is intentional — Task 2 wires the callback. Do not fix it here.

- [ ] **Step 7: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel/.worktrees/meta-context-fields
git add src/lingtai_kernel/session.py tests/test_session.py
git commit -m "$(cat <<'EOF'
feat(session): cache context-section token count via new callback

SessionManager gains read_context_section_fn ctor arg and
_context_section_tokens cached counter, updated alongside
_system_prompt_tokens and _tools_tokens in _update_token_decomposition.
Exposed via get_token_usage() as ctx_context_section_tokens.

BaseAgent still constructs SessionManager without this kwarg — that is
fixed in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Wire the callback in `BaseAgent`

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py`
- Test: `tests/test_base_agent.py` (regression only)

- [ ] **Step 1: Verify target test is currently red**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py -q 2>&1 | tail -5
```

Expected: failures with `TypeError: SessionManager.__init__() missing 1 required keyword-only argument: 'read_context_section_fn'`.

- [ ] **Step 2: Pass the callback at construction**

In `src/lingtai_kernel/base_agent.py`, find:

```python
        self._session = SessionManager(
            llm_service=service,
            config=self._config,
            agent_name=agent_name,
            streaming=streaming,
            build_system_prompt_fn=self._build_system_prompt,
            build_tool_schemas_fn=self._build_tool_schemas,
            logger_fn=self._log,
        )
```

Insert `read_context_section_fn=self._read_context_section` above `logger_fn`:

```python
        self._session = SessionManager(
            llm_service=service,
            config=self._config,
            agent_name=agent_name,
            streaming=streaming,
            build_system_prompt_fn=self._build_system_prompt,
            build_tool_schemas_fn=self._build_tool_schemas,
            read_context_section_fn=self._read_context_section,
            logger_fn=self._log,
        )
```

- [ ] **Step 3: Add the `_read_context_section` method**

In `src/lingtai_kernel/base_agent.py`, find `_build_system_prompt`. Add `_read_context_section` immediately after it. Grep for the marker comment "# Intrinsic wiring" as a nearby landmark.

Pattern to find:

```python
    def _build_system_prompt(self) -> str:
```

Read the file to see the method. After its `return` line (the method is small — a few lines), add:

```python
    def _read_context_section(self) -> str:
        """Return the current 'context' prompt-manager section, or '' if absent.

        Passed as read_context_section_fn to SessionManager so it can
        tokenize the section independently of the full system prompt.
        """
        return self._prompt_manager.read_section("context") or ""
```

(If you can't find `_build_system_prompt`, it is around line 1080 of `base_agent.py` — search by exact name.)

- [ ] **Step 4: Smoke-test the module imports cleanly**

```bash
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -c "import lingtai_kernel.base_agent"
```

Expected: no output.

- [ ] **Step 5: Run `test_base_agent.py` + `test_session.py`**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py tests/test_session.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/base_agent.py
git commit -m "$(cat <<'EOF'
feat(base_agent): feed context-section content to SessionManager

New _read_context_section method returns the current 'context'
prompt-manager section content (or ''). Wired into SessionManager via
read_context_section_fn, restoring all existing tests to green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `build_meta` emits context-window fields

**Files:**
- Modify: `src/lingtai_kernel/meta_block.py`
- Test: `tests/test_meta_block.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meta_block.py`:

```python
def _fake_agent_with_session(
    *,
    time_awareness=True,
    timezone_awareness=True,
    language="en",
    system_prompt_tokens=0,
    tools_tokens=0,
    context_section_tokens=0,
    history_tokens=0,
    context_limit=100000,
    decomp_ran=True,
):
    """Agent stand-in that exposes the session state build_meta reads."""
    class _Chat:
        def context_window(self_):
            return 200000  # model default

        class _iface:
            @staticmethod
            def estimate_context_tokens():
                return history_tokens

        interface = _iface()

    chat_obj = _Chat() if decomp_ran else None

    return SimpleNamespace(
        _config=SimpleNamespace(
            time_awareness=time_awareness,
            timezone_awareness=timezone_awareness,
            language=language,
            context_limit=context_limit,
        ),
        _session=SimpleNamespace(
            _system_prompt_tokens=system_prompt_tokens,
            _tools_tokens=tools_tokens,
            _context_section_tokens=context_section_tokens,
            _token_decomp_dirty=not decomp_ran,
            _chat=chat_obj,
            chat=chat_obj,
        ),
    )


def test_build_meta_emits_context_fields_when_decomp_ran():
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,   # includes 1000 of context section
        context_section_tokens=1000,
        tools_tokens=500,
        history_tokens=200,
        context_limit=100000,
    )
    meta = build_meta(agent)
    # system = (system_prompt - context_section) + tools = 4000 + 500 = 4500
    assert meta["system_tokens"] == 4500
    # context = context_section + history = 1000 + 200 = 1200
    assert meta["context_tokens"] == 1200
    # usage = (4500 + 1200) / 100000 = 0.057
    assert abs(meta["context_usage"] - 0.057) < 1e-6


def test_build_meta_emits_sentinels_before_decomp_runs():
    # When decomposition has never run (dirty flag True) and no chat yet,
    # we cannot compute any of the three fields honestly.
    agent = _fake_agent_with_session(decomp_ran=False)
    meta = build_meta(agent)
    assert meta["system_tokens"] == -1
    assert meta["context_tokens"] == -1
    assert meta["context_usage"] == -1.0


def test_build_meta_time_blind_still_emits_context_fields():
    agent = _fake_agent_with_session(
        time_awareness=False,
        system_prompt_tokens=5000,
        context_section_tokens=1000,
        tools_tokens=500,
        history_tokens=200,
    )
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert meta["system_tokens"] == 4500
    assert meta["context_tokens"] == 1200
```

- [ ] **Step 2: Run to verify failure**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v -k "emits_context or sentinels or time_blind_still"
```

Expected: `KeyError` on `meta["system_tokens"]`.

- [ ] **Step 3: Extend `build_meta`**

In `src/lingtai_kernel/meta_block.py`, replace the entire `build_meta` function with:

```python
def build_meta(agent) -> dict:
    """Return the current meta-data snapshot for the agent.

    Respects ``agent._config.time_awareness`` / ``timezone_awareness``
    internally; callers never need to special-case those flags.

    When the agent is time-blind and no other meta fields are curated in,
    returns ``{}``.

    Context-window fields (``system_tokens``, ``context_tokens``,
    ``context_usage``) are always emitted — the time veil does not cover
    token accounting. When the session's token decomposition has not yet
    run (dirty cache and no active chat), the three fields are emitted as
    ``-1`` / ``-1.0`` sentinels so callers can render "unknown" without
    ambiguity.
    """
    meta: dict = {}
    ts = now_iso(agent)
    if ts:
        meta["current_time"] = ts

    # Context-window decomposition. Only meaningful after the session has
    # run _update_token_decomposition at least once, which it does on first
    # token-tracking call (see SessionManager._track_usage).
    session = getattr(agent, "_session", None)
    chat_obj = getattr(session, "chat", None) if session is not None else None
    decomp_ran = session is not None and not session._token_decomp_dirty

    if decomp_ran and chat_obj is not None:
        sys_prompt = session._system_prompt_tokens
        ctx_section = session._context_section_tokens
        tools = session._tools_tokens
        history = chat_obj.interface.estimate_context_tokens()

        # The "system" bucket in the meta line is everything that is NOT
        # accumulated memory: the prompt floor (minus the context section)
        # plus the tool schemas. max(0, ...) guards against tokenizer
        # underflow if the context section happens to outweigh the full
        # prompt estimate (shouldn't happen, but defensive).
        system_tokens = max(0, sys_prompt - ctx_section) + tools
        context_tokens = ctx_section + history

        limit = agent._config.context_limit or chat_obj.context_window()
        usage = (system_tokens + context_tokens) / limit if limit > 0 else -1.0

        meta["system_tokens"] = system_tokens
        meta["context_tokens"] = context_tokens
        meta["context_usage"] = usage
    else:
        meta["system_tokens"] = -1
        meta["context_tokens"] = -1
        meta["context_usage"] = -1.0

    return meta
```

- [ ] **Step 4: Run the new tests**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v -k "emits_context or sentinels or time_blind_still"
```

Expected: 3 passed.

- [ ] **Step 5: Run the full `test_meta_block.py`**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: all prior tests still pass. The pre-existing `test_build_meta_time_aware_local_tz_has_offset` etc. use `_fake_agent` (not `_fake_agent_with_session`) — those agents lack `_session`, so `build_meta` takes the `decomp_ran=False` branch and adds sentinel fields. Update these assertions only if they explicitly check for dict-equality without the new keys. Concretely:

- `test_build_meta_time_blind_returns_empty_dict` — previously `assert meta == {}`. Now `meta` also has `system_tokens: -1`, `context_tokens: -1`, `context_usage: -1.0`. Update assertion to `assert "current_time" not in meta and meta["system_tokens"] == -1`.
- `test_build_meta_time_blind_regardless_of_timezone_awareness` — same fix.

Keep the test docstrings/intent; update only the dict-equality checks.

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/meta_block.py tests/test_meta_block.py
git commit -m "$(cat <<'EOF'
feat(meta): build_meta emits system_tokens, context_tokens, context_usage

Reads already-cached SessionManager counters to decompose context-window
usage into a fixed floor (system.md minus context section + tools) and
a growing memory (context section + in-memory history), plus the ratio
against config.context_limit or the model default.

Emits -1 / -1.0 sentinels when the session's decomposition has not yet
run, so renderers can show "unknown" unambiguously.

Pre-existing time-blind tests updated: meta dict is no longer empty
(context fields are unaffected by time-veil). Adjusted assertions to
check current_time absence explicitly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: i18n — extend `system.current_time` + add two new keys

**Files:**
- Modify: `src/lingtai_kernel/i18n/en.json`
- Modify: `src/lingtai_kernel/i18n/zh.json`
- Modify: `src/lingtai_kernel/i18n/wen.json`
- Test: `tests/test_i18n.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_i18n.py`:

```python
class TestContextBreakdownKeys:
    def test_context_breakdown_en(self):
        result = t("en", "system.context_breakdown", pct="7.1%", sys=4720, ctx=9450)
        assert result == "7.1% (sys 4720 + ctx 9450)"

    def test_context_breakdown_zh(self):
        result = t("zh", "system.context_breakdown", pct="7.1%", sys=4720, ctx=9450)
        assert result == "7.1% (系统 4720 + 对话 9450)"

    def test_context_breakdown_wen(self):
        result = t("wen", "system.context_breakdown", pct="7.1%", sys=4720, ctx=9450)
        assert result == "7.1% (系统 4720 + 对话 9450)"

    def test_context_unknown_en(self):
        assert t("en", "system.context_unknown") == "unavailable"

    def test_context_unknown_zh(self):
        assert t("zh", "system.context_unknown") == "未知"

    def test_context_unknown_wen(self):
        assert t("wen", "system.context_unknown") == "未知"

    def test_current_time_en_extended(self):
        result = t("en", "system.current_time", time="T", ctx="CTX")
        assert result == "[Current time: T | context: CTX]"

    def test_current_time_zh_extended(self):
        result = t("zh", "system.current_time", time="T", ctx="CTX")
        assert result == "[此时：T | 上下文：CTX]"

    def test_current_time_wen_extended(self):
        result = t("wen", "system.current_time", time="T", ctx="CTX")
        assert result == "[此时：T | 上下文：CTX]"
```

- [ ] **Step 2: Run to verify failure**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_i18n.py::TestContextBreakdownKeys -v
```

Expected: multiple failures — the 3 extended `system.current_time` tests fail because the existing template takes only `{time}`; the `context_breakdown` and `context_unknown` tests fail with `KeyError` on missing key.

- [ ] **Step 3: Edit `src/lingtai_kernel/i18n/en.json`**

Find:

```json
  "system.current_time": "[Current time: {time}]",
```

Replace with:

```json
  "system.current_time": "[Current time: {time} | context: {ctx}]",
  "system.context_breakdown": "{pct} (sys {sys} + ctx {ctx})",
  "system.context_unknown": "unavailable",
```

(Order: keep `system.current_time` in its existing position. Add the two new keys immediately below it.)

- [ ] **Step 4: Edit `src/lingtai_kernel/i18n/zh.json`**

Find:

```json
  "system.current_time": "[当前时间：{time}]",
```

Replace with:

```json
  "system.current_time": "[此时：{time} | 上下文：{ctx}]",
  "system.context_breakdown": "{pct} (系统 {sys} + 对话 {ctx})",
  "system.context_unknown": "未知",
```

Note the zh-specific change: `当前时间` → `此时` to match wen.

- [ ] **Step 5: Edit `src/lingtai_kernel/i18n/wen.json`**

Find:

```json
  "system.current_time": "[此时：{time}]",
```

Replace with:

```json
  "system.current_time": "[此时：{time} | 上下文：{ctx}]",
  "system.context_breakdown": "{pct} (系统 {sys} + 对话 {ctx})",
  "system.context_unknown": "未知",
```

- [ ] **Step 6: Run the new tests**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_i18n.py -v
```

Expected: all pass, including the pre-existing `TestCurrentTimeKeys` tests. Those existing tests call `t("...", "system.current_time", time="...")` without a `ctx` kwarg — the `.format()` call in the `t()` helper will raise `KeyError: 'ctx'`. That is a REAL failure.

**Update the pre-existing `TestCurrentTimeKeys` tests in `tests/test_i18n.py`** to pass both `time` and `ctx` kwargs. Replace each `t("xx", "system.current_time", time="2026-03-19T00:00:00Z")` with `t("xx", "system.current_time", time="2026-03-19T00:00:00Z", ctx="CTX")`, and update the expected-string assertions accordingly:

- en: expect `"[Current time: 2026-03-19T00:00:00Z | context: CTX]"`
- zh: expect `"[此时：2026-03-19T00:00:00Z | 上下文：CTX]"`
- wen: expect `"[此时：2026-03-19T00:00:00Z | 上下文：CTX]"`
- The fallback test (`t("xx", ...)`) falls back to en — update expected to the extended en form.

- [ ] **Step 7: Run both i18n tests AND the meta_block tests to check that the existing `render_meta` tests still match the new template**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_i18n.py tests/test_meta_block.py -v
```

Expected: `test_render_meta_en_uses_existing_current_time_template` etc. now fail because they assert the OLD template output (e.g. `"[Current time: 2026-04-20T10:15:23-07:00]"`). Leave those tests failing for now — Task 5 updates `render_meta` and those tests together.

Running just i18n tests should now be all green:

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_i18n.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/lingtai_kernel/i18n/en.json src/lingtai_kernel/i18n/zh.json src/lingtai_kernel/i18n/wen.json tests/test_i18n.py
git commit -m "$(cat <<'EOF'
feat(i18n): extend system.current_time; add context_breakdown / context_unknown

system.current_time gains a {ctx} slot; zh unifies 当前时间 → 此时 with wen.
Two new keys carry the composed context fragment and the "unknown"
sentinel label for all three locales.

tests/test_meta_block.py render_meta tests are temporarily red and fixed
in the next commit alongside render_meta itself.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `render_meta` composes the context fragment

**Files:**
- Modify: `src/lingtai_kernel/meta_block.py`
- Test: `tests/test_meta_block.py`

- [ ] **Step 1: Update the existing `render_meta` tests + add new coverage**

In `tests/test_meta_block.py`, find the three existing `test_render_meta_{en,zh,wen}_uses_existing_current_time_template` tests. Each currently asserts the old output. Update:

```python
def test_render_meta_en_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "system_tokens": 4720,
        "context_tokens": 9450,
        "context_usage": 0.071,
    }
    assert render_meta(agent, meta) == "[Current time: 2026-04-20T10:15:23-07:00 | context: 7.1% (sys 4720 + ctx 9450)]"


def test_render_meta_zh_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("zh")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "system_tokens": 4720,
        "context_tokens": 9450,
        "context_usage": 0.071,
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：7.1% (系统 4720 + 对话 9450)]"


def test_render_meta_wen_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("wen")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "system_tokens": 4720,
        "context_tokens": 9450,
        "context_usage": 0.071,
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：7.1% (系统 4720 + 对话 9450)]"
```

Then append new tests:

```python
def test_render_meta_context_unknown_sentinel_en():
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "system_tokens": -1,
        "context_tokens": -1,
        "context_usage": -1.0,
    }
    assert render_meta(agent, meta) == "[Current time: 2026-04-20T10:15:23-07:00 | context: unavailable]"


def test_render_meta_context_unknown_sentinel_zh():
    agent = _fake_agent_with_lang("zh")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "system_tokens": -1,
        "context_tokens": -1,
        "context_usage": -1.0,
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：未知]"


def test_render_meta_rounds_usage_to_one_decimal():
    """Usage ratios round to one decimal place, not raw float."""
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "T",
        "system_tokens": 1000,
        "context_tokens": 500,
        "context_usage": 0.0723456,
    }
    result = render_meta(agent, meta)
    assert "7.2%" in result
```

- [ ] **Step 2: Run tests to verify they fail**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v -k "render_meta"
```

Expected: the 3 updated tests fail with the old template output; the 3 new tests fail similarly.

- [ ] **Step 3: Extend `render_meta`**

In `src/lingtai_kernel/meta_block.py`, replace the `render_meta` function body with:

```python
def render_meta(agent, meta: dict) -> str:
    """Render the meta dict as the line prepended to text input.

    Returns '' when the meta dict is empty — callers should treat '' as
    "no prefix" and skip concatenation.

    Composes the existing ``system.current_time`` template (now
    extended with a context slot) plus a context fragment via
    ``system.context_breakdown`` (or ``system.context_unknown`` when the
    session has not yet computed its token decomposition).
    """
    if not meta:
        return ""

    time_val = meta.get("current_time", "")
    ctx_val = _render_context_fragment(agent, meta)

    if time_val == "" and ctx_val == "":
        return ""

    return _t(
        agent._config.language,
        "system.current_time",
        time=time_val,
        ctx=ctx_val,
    )


def _render_context_fragment(agent, meta: dict) -> str:
    """Render the context sub-fragment for the text-input prefix.

    Returns:
        - '' if none of the three context fields are present in ``meta``
        - the locale-specific "unknown" word when the sentinel (-1) is seen
        - the composed "{pct} (sys {sys} + ctx {ctx})" fragment otherwise
    """
    if "context_usage" not in meta:
        return ""
    usage = meta["context_usage"]
    if usage < 0:
        return _t(agent._config.language, "system.context_unknown")
    return _t(
        agent._config.language,
        "system.context_breakdown",
        pct=f"{usage * 100:.1f}%",
        sys=meta.get("system_tokens", 0),
        ctx=meta.get("context_tokens", 0),
    )
```

- [ ] **Step 4: Run the render_meta tests**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v -k "render_meta"
```

Expected: all pass, including the rounding test and both sentinel tests.

- [ ] **Step 5: Run the whole `test_meta_block.py` file**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py -v
```

Expected: all pass.

- [ ] **Step 6: Run the wider relevant suite**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py tests/test_tool_executor.py tests/test_tool_timing.py tests/test_session.py tests/test_base_agent.py tests/test_i18n.py tests/test_time_veil.py tests/test_time_awareness_mail.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/lingtai_kernel/meta_block.py tests/test_meta_block.py
git commit -m "$(cat <<'EOF'
feat(meta): render_meta composes context fragment in text-input prefix

render_meta now assembles the extended line via system.current_time +
system.context_breakdown (or system.context_unknown when the sentinel
-1 is present). Usage rounds to one decimal place.

New tests cover the full extended format in all three locales, the
sentinel fallback, and the rounding rule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Extend `status()` breakdown

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py`
- Test: `tests/test_base_agent.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_base_agent.py` (if a similar fixture already exists for instantiating a `BaseAgent`, reuse it; if not, use an existing test as a template):

```python
def test_status_exposes_finer_context_decomposition(tmp_path):
    """status() adds a 'context' sub-block with the fixed/growing split."""
    # Reuse whatever helper the file already uses to construct a BaseAgent
    # against tmp_path. See existing tests in this file for the fixture.
    agent = _make_agent_for_status_test(tmp_path)  # adapt to existing helper
    st = agent.status()
    ctx = st["tokens"]["context"]
    # Old keys still present
    assert "system_tokens" in ctx
    assert "tools_tokens" in ctx
    assert "history_tokens" in ctx
    assert "total_tokens" in ctx
    assert "window_size" in ctx
    assert "usage_pct" in ctx
    # New keys
    assert "context_section_tokens" in ctx
    assert "fixed_tokens" in ctx    # = system_tokens - context_section_tokens + tools_tokens
    assert "growing_tokens" in ctx  # = context_section_tokens + history_tokens
```

If no existing helper works, write a minimal fixture inline or mark this step as blocked and report — creating a full `BaseAgent` from scratch for a test is too large for this task. **Strongly prefer to extend an existing test pattern in the same file.**

- [ ] **Step 2: Run to verify failure**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py -v -k "exposes_finer_context_decomposition"
```

Expected: `KeyError` or similar on the new keys.

- [ ] **Step 3: Extend `status()`**

In `src/lingtai_kernel/base_agent.py`, find the `status` method (around line 1662). Find the `"context"` block inside the return dict:

```python
                "context": {
                    "system_tokens": usage["ctx_system_tokens"],
                    "tools_tokens": usage["ctx_tools_tokens"],
                    "history_tokens": usage["ctx_history_tokens"],
                    "total_tokens": usage["ctx_total_tokens"],
                    "window_size": window_size,
                    "usage_pct": usage_pct,
                },
```

Replace with:

```python
                "context": {
                    "system_tokens": usage["ctx_system_tokens"],
                    "tools_tokens": usage["ctx_tools_tokens"],
                    "context_section_tokens": usage["ctx_context_section_tokens"],
                    "history_tokens": usage["ctx_history_tokens"],
                    "total_tokens": usage["ctx_total_tokens"],
                    "window_size": window_size,
                    "usage_pct": usage_pct,
                    # Meta-line decomposition (matches build_meta's buckets)
                    "fixed_tokens": max(
                        0,
                        usage["ctx_system_tokens"] - usage["ctx_context_section_tokens"],
                    ) + usage["ctx_tools_tokens"],
                    "growing_tokens": usage["ctx_context_section_tokens"] + usage["ctx_history_tokens"],
                },
```

- [ ] **Step 4: Run the test**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py -v -k "exposes_finer_context_decomposition"
```

Expected: passes.

- [ ] **Step 5: Run the full test_base_agent.py**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_base_agent.py -v
```

Expected: all pass. If an existing test asserts the exact shape of the `"context"` dict (e.g., `assert ctx == {old_shape}`), update it to either (a) use `>=` key-presence checks or (b) add the new keys to the expected dict.

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/base_agent.py tests/test_base_agent.py
git commit -m "$(cat <<'EOF'
feat(status): expose context_section_tokens + meta-line decomposition

status() now includes context_section_tokens (server-reported floor
minus the context section) and the fixed/growing split that mirrors
build_meta's two buckets. This is the on-demand breakdown surface;
the per-turn prefix stays compact.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: End-to-end integration smoke

**Files:** verification-only.

- [ ] **Step 1: Run the targeted test set**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest tests/test_meta_block.py tests/test_tool_executor.py tests/test_tool_timing.py tests/test_session.py tests/test_base_agent.py tests/test_i18n.py tests/test_time_veil.py tests/test_time_awareness_mail.py -v
```

Expected: all pass.

- [ ] **Step 2: Wider kernel sweep**

```
PYTHONPATH=src /Users/huangzesen/Documents/GitHub/lingtai/venv/bin/python -m pytest -q \
    --ignore=tests/test_addons.py --ignore=tests/test_account.py \
    --ignore=tests/test_manager.py --ignore=tests/test_service.py 2>&1 | tail -5
```

Baseline from feature/meta-block-infra: 59 failed / 1095 passed. This task should add ~20 passing tests (session + meta_block + i18n + base_agent additions) and NOT add any new failures. Acceptable: failures ≤ 59. If greater, investigate before declaring done.

- [ ] **Step 3: Confirm the diff is tight**

```bash
git log --oneline feature/meta-block-infra..HEAD
git diff --stat feature/meta-block-infra..HEAD
```

Expected: 6 code commits on top of the spec commit.

- [ ] **Step 4: No commit — verification only.**

---

## Self-Review Notes

- **Spec coverage:** Meta dict shape → Tasks 1+3 (session caching, build_meta emits); render → Tasks 4+5 (i18n, render_meta); full breakdown in system.show → Task 6; tool-result automatic merge → no task needed (stamp_meta already forwards all keys).
- **Behavior preservation:** Tool-result shape gains 3 keys automatically (additive, non-breaking). The text-input prefix changes for all locales, including the `当前时间 → 此时` unification in zh.
- **Sentinel handling:** `-1` / `-1.0` clearly distinguishes "unknown yet" from "zero tokens" (which is meaningful during early setup). Renderer branches on `< 0`.
- **Time-blind edge case:** documented in the spec as accepted; no plan work needed. An agent with `time_awareness=False` after this change will render `"[Current time:  | context: …]"` — awkward empty slot, acceptable.
- **Type consistency:** `system_tokens`: int; `context_tokens`: int; `context_usage`: float; sentinels: -1 / -1.0. `read_context_section_fn: Callable[[], str]`. All consistent across tasks.
