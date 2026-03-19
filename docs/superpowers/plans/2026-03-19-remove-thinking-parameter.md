# Remove `thinking` Parameter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `thinking` parameter from the kernel's session creation path. Thinking mode is a model variant — the adapter should decide, not the kernel. The kernel's job is to say *which* model, not *how* it should think.

**Architecture:** Remove `thinking` from `LLMAdapter.create_chat()`, `LLMService.create_session()`, `LLMService.resume_session()`, `SessionManager.ensure_session()`, and all callers. Also remove `AgentConfig.thinking_budget`. Keep all `thinking_tokens`/`ThinkingBlock`/`thoughts` — those are observational (what the model did), not instructional.

**Tech Stack:** Python 3.11+, stoai-kernel internals only

**What stays:** `UsageMetadata.thinking_tokens`, `LLMResponse.thoughts`, `ThinkingBlock`, all token counting for thinking — these observe model behavior, they don't control it.

**What goes:** The `thinking: str` parameter threaded through session creation, and `AgentConfig.thinking_budget`.

---

## File Structure

| Action | Path | What changes |
|--------|------|-------------|
| Modify | `src/stoai_kernel/llm/base.py:270` | Remove `thinking` param from `LLMAdapter.create_chat()` ABC |
| Modify | `src/stoai_kernel/llm/service.py:297,335,503` | Remove `thinking` param from `create_session()`, `resume_session()`, `check_and_compact()` |
| Modify | `src/stoai_kernel/session.py:146,186,264` | Remove `thinking="high"` from all `create_session()` calls in `SessionManager` |
| Modify | `src/stoai_kernel/intrinsics/soul.py` | Already removed (no `thinking` param in `whisper()`) |
| Modify | `src/stoai_kernel/config.py:20` | Remove `thinking_budget` field from `AgentConfig` |
| Modify | `tests/test_session.py` | Update any tests that assert on `thinking` param |
| Modify | `tests/test_llm_service.py` | Update any tests that pass `thinking` |

---

### Task 1: Remove `thinking` from `LLMAdapter.create_chat()` ABC

**Files:**
- Modify: `src/stoai_kernel/llm/base.py`

- [ ] **Step 1: Read the file and identify the parameter**

The `thinking` parameter is at line 270 with docstring at line 286-287.

- [ ] **Step 2: Remove `thinking` parameter and its docstring**

Remove `thinking: str = "default",` from the signature and the corresponding docstring lines.

- [ ] **Step 3: Smoke test**

Run: `python -c "from stoai_kernel.llm.base import LLMAdapter; print('ok')"`

- [ ] **Step 4: Commit**

```bash
git add src/stoai_kernel/llm/base.py
git commit -m "refactor: remove thinking param from LLMAdapter.create_chat() ABC"
```

---

### Task 2: Remove `thinking` from `LLMService`

**Files:**
- Modify: `src/stoai_kernel/llm/service.py`

- [ ] **Step 1: Remove `thinking` from `create_session()` signature and its passthrough to `adapter.create_chat()`**

Lines 297 and 318.

- [ ] **Step 2: Remove `thinking` from `resume_session()` signature and its passthrough**

Lines 335 and 352.

- [ ] **Step 3: Remove `thinking` from `check_and_compact()` call to `create_session()`**

Line 503.

- [ ] **Step 4: Smoke test**

Run: `python -c "from stoai_kernel.llm.service import LLMService; print('ok')"`

- [ ] **Step 5: Commit**

```bash
git add src/stoai_kernel/llm/service.py
git commit -m "refactor: remove thinking param from LLMService"
```

---

### Task 3: Remove `thinking` from `SessionManager`

**Files:**
- Modify: `src/stoai_kernel/session.py`

- [ ] **Step 1: Remove `thinking="high"` from all three `create_session()` calls**

Lines 146, 186, and 264.

- [ ] **Step 2: Smoke test**

Run: `python -c "from stoai_kernel.session import SessionManager; print('ok')"`

- [ ] **Step 3: Commit**

```bash
git add src/stoai_kernel/session.py
git commit -m "refactor: remove thinking param from SessionManager"
```

---

### Task 4: Remove `thinking_budget` from `AgentConfig`

**Files:**
- Modify: `src/stoai_kernel/config.py`

- [ ] **Step 1: Read config.py and remove `thinking_budget` field**

- [ ] **Step 2: Grep for any references to `thinking_budget` in the kernel**

Run: `grep -r "thinking_budget" src/stoai_kernel/`

If found, remove those references too.

- [ ] **Step 3: Smoke test**

Run: `python -c "from stoai_kernel.config import AgentConfig; print('ok')"`

- [ ] **Step 4: Commit**

```bash
git add src/stoai_kernel/config.py
git commit -m "refactor: remove thinking_budget from AgentConfig"
```

---

### Task 5: Update tests

**Files:**
- Modify: `tests/test_session.py`
- Modify: `tests/test_llm_service.py`

- [ ] **Step 1: Find all test references to `thinking` parameter**

Run: `grep -n "thinking" tests/`

- [ ] **Step 2: Update tests — remove `thinking` from mock assertions and call signatures**

Any test that asserts `thinking="high"` was passed, or passes `thinking` to `create_session()`, needs updating.

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: update tests for thinking param removal"
```

---

### Task 6: Verify soul.py and final smoke test

**Files:**
- Verify: `src/stoai_kernel/intrinsics/soul.py`

- [ ] **Step 1: Confirm soul.py does not pass `thinking`**

Run: `grep thinking src/stoai_kernel/intrinsics/soul.py`

Expected: no matches.

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`

- [ ] **Step 3: Verify all imports**

Run: `python -c "import stoai_kernel; print('ok')"`

- [ ] **Step 4: Final commit if needed**

---

## Breaking change note

This is a **breaking change for adapter implementors**. Any adapter that implements `create_chat()` with a `thinking` parameter will need to:
1. Remove `thinking` from their `create_chat()` signature
2. Decide their own thinking behavior based on the model name or adapter configuration

Consumers of `LLMService.create_session()` that pass `thinking=` will get a `TypeError`. This is intentional — they should stop telling the adapter how to think.
