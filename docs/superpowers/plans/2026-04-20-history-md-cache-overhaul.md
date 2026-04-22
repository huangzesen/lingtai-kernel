# context.md System Prompt Cache Overhaul

## ABANDONED 2026-04-22

This plan was implemented in full (commits `ac324d6`, `f2920d5`, `dd8bd96`, `15b0b7b`, `b77b920`, and the follow-up series through `7044deb`, `fcd873e`, `909e7da`) but has been reverted.

**Why:** The caching optimization was correct on paper — it successfully moved chat history into a cacheable system-prompt region, turning per-turn token cost from O(N²) on the wire into near-linear. In practice it introduced two serious behavioral regressions:

1. **Phenomenology of resumption.** With history serialized into the system prompt and the wire chat nuked at each idle, the LLM no longer "continues a conversation" — it "resumes from a transcript." Tool-call IDs, assistant thinking blocks, and tool-result structure all get flattened to markdown. The agent visibly loses the feeling of continuity; it narrates its own past tense as if reading a log. For the LingTai use case — persistent embodied agents with felt continuity — this is not a price worth paying.
2. **Refresh breaks context entirely.** The cold-start path (via `_setup_from_init`) clears the prompt manager and reloads sections from init.json — but the reload list in `Agent._reload_prompt_sections` did not include context. So every CLI start or `system.refresh` silently wiped the serialized history from the prompt. The agent had neither wire-chat continuity (removed by this plan) nor system-prompt continuity (the serialization gap). Users observed "context 0" after every refresh.

**What replaced it:** the pre-plan behavior. `chat_history.jsonl` is restored directly into the wire `ChatInterface` on `start()`, exactly as it worked before `b77b920`. Refresh preserves the live interface via `_setup_from_init`'s save/restore path (agent.py:581-583, 725-727) which was already in place for in-memory refresh and now matches the post-relaunch path thanks to the restored `start()` rehydration. `system/context.md`, `context_serializer.py`, `_flush_context_to_prompt`, the `context` prompt section, and the two config fields (`context_serialization_enabled`, `context_rebuild_every_n_idles`) are all removed.

**What we give up:** the O(N²)→O(N) caching win on the history portion. For the active caching targets (system prompt sections, tools), the multi-batch `cache_control` machinery in the Anthropic adapter stays in place — just without the Batch 3 "context" tier.

**Lessons:** (a) When moving a long-lived invariant ("past conversation is visible to the LLM") to a new storage location, the migration must cover every entry path (cold start, refresh, molt, avatar spawn). This plan's task list covered the kernel `__init__` but missed the wrapper's `_setup_from_init`. (b) Caching optimizations that change semantic shape (native turns → transcript) are not free — they should be evaluated against the felt behavior of the agent, not only the token economics.

The text below is preserved as historical record. Do not follow it.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move conversation history into the system prompt as a `context` section to maximize prompt cache hit rates, reducing token cost from O(N²) to near-linear.

**Architecture:** `chat_history.jsonl` is the append-only source of truth. On each idle transition, rebuild `context.md` by serializing all JSONL entries since the last molt boundary into markdown. This markdown is injected as the last section of the system prompt (cacheable). The ChatInterface is wiped after each idle, so the next turn starts with empty conversation + cached system prompt containing context.md.

**Tech Stack:** Python (lingtai-kernel), Go (lingtai TUI)

---

## Core Design

1. **chat_history.jsonl** — append-only, persistent, source of truth. Contains molt_boundary markers.
2. **context.md** — derived view, rebuilt from JSONL on every idle. Injected as `## context` section (last in system prompt). Deleted on molt.
3. **Serialization** — full fidelity, no truncation, no filtering. Every JSONL entry since last `molt_boundary` is rendered as `### role [ISO timestamp]\n<content>`. The only entries skipped are `molt_boundary` markers themselves (metadata, not conversation).

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/lingtai_kernel/prompt.py` | Modify | Add `context` to section order |
| `src/lingtai_kernel/context_serializer.py` | Create | `serialize_context_md(entries)` — JSONL→markdown |
| `src/lingtai_kernel/base_agent.py` | Modify | Idle rebuild, startup, audit refactor |
| `src/lingtai_kernel/intrinsics/eigen.py` | Modify | Write molt_boundary, delete context.md |
| `tui/internal/tui/app.go` | Modify | Delete context.md on refresh |
| `tests/test_context_md.py` | Create | Tests for the new context system |

---

### Task 1: Add `context` Section to SystemPromptManager

**Files:**
- Modify: `src/lingtai_kernel/prompt.py:27`
- Test: `tests/test_context_md.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_context_md.py
"""Tests for context.md system prompt cache overhaul."""
import pytest
from lingtai_kernel.prompt import SystemPromptManager


class TestContextSection:
    def test_context_is_last_section(self):
        pm = SystemPromptManager()
        pm.write_section("pad", "my notes")
        pm.write_section("context", "### user [2026-04-20T10:00:00Z]\nhello")
        rendered = pm.render()
        pad_pos = rendered.index("## pad")
        context_pos = rendered.index("## context")
        assert context_pos > pad_pos

    def test_context_empty_not_rendered(self):
        pm = SystemPromptManager()
        pm.write_section("pad", "my notes")
        rendered = pm.render()
        assert "## context" not in rendered

    def test_context_deleted_disappears(self):
        pm = SystemPromptManager()
        pm.write_section("context", "some content")
        pm.delete_section("context")
        rendered = pm.render()
        assert "## context" not in rendered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestContextSection::test_context_is_last_section -v`
Expected: FAIL

- [ ] **Step 3: Modify `_DEFAULT_ORDER` in prompt.py**

In `src/lingtai_kernel/prompt.py` line 27, change:

```python
_DEFAULT_ORDER = ["principle", "covenant", "rules", "tools", "procedures", "comment", "codex", "library", "identity", "brief", "pad"]
```

to:

```python
_DEFAULT_ORDER = ["principle", "covenant", "rules", "tools", "procedures", "comment", "codex", "library", "identity", "brief", "pad", "context"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestContextSection -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Smoke-test module**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "from lingtai_kernel.prompt import SystemPromptManager; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/prompt.py tests/test_context_md.py
git commit -m "feat: add context as tail section in SystemPromptManager"
```

---

### Task 2: Create `context_serializer.py` — JSONL to Markdown

**Files:**
- Create: `src/lingtai_kernel/context_serializer.py`
- Test: `tests/test_context_md.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_context_md.py`:

```python
import json
from lingtai_kernel.context_serializer import serialize_context_md


class TestSerializeContextMd:
    def test_basic_user_assistant(self):
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 1713600000.0},
            {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hi there"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "### user [" in md
        assert "### assistant [" in md
        assert "hello" in md
        assert "hi there" in md

    def test_thinking_blocks_included(self):
        entries = [
            {"id": 0, "role": "assistant", "content": [
                {"type": "thinking", "text": "let me think about this"},
                {"type": "text", "text": "here's my answer"},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert "let me think about this" in md
        assert "here's my answer" in md

    def test_tool_call_full_args(self):
        long_args = {"content": "x" * 5000}
        entries = [
            {"id": 0, "role": "assistant", "content": [
                {"type": "tool_call", "id": "tc1", "name": "write", "args": long_args},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        # Full args, no truncation
        assert "x" * 5000 in md

    def test_tool_result_full_content(self):
        long_result = "line\n" * 2000
        entries = [
            {"id": 0, "role": "user", "content": [
                {"type": "tool_result", "id": "tc1", "name": "read", "content": long_result},
            ], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        # Full content, no truncation
        assert long_result in md

    def test_system_entries_included(self):
        entries = [
            {"id": 0, "role": "system", "system": "You are a helpful agent.", "timestamp": 1713600000.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "You are a helpful agent." in md

    def test_molt_boundary_skipped(self):
        entries = [
            {"type": "molt_boundary", "molt_count": 1, "timestamp": 1713600000.0, "summary": "old stuff"},
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "fresh start"}], "timestamp": 1713600001.0},
        ]
        md = serialize_context_md(entries)
        assert "molt_boundary" not in md
        assert "old stuff" not in md
        assert "fresh start" in md

    def test_empty_entries(self):
        md = serialize_context_md([])
        assert md == ""

    def test_timestamp_format(self):
        # timestamp 1713600000.0 = 2024-04-20T08:00:00Z
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1713600000.0},
        ]
        md = serialize_context_md(entries)
        assert "2024-04-20T" in md

    def test_turn_separator(self):
        """System entries mark turn boundaries — render --- between turns."""
        entries = [
            {"id": 0, "role": "system", "system": "prompt v1", "timestamp": 1.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "turn 1"}], "timestamp": 2.0},
            {"id": 2, "role": "assistant", "content": [{"type": "text", "text": "reply 1"}], "timestamp": 3.0},
            {"id": 0, "role": "system", "system": "prompt v2", "timestamp": 4.0},
            {"id": 1, "role": "user", "content": [{"type": "text", "text": "turn 2"}], "timestamp": 5.0},
            {"id": 2, "role": "assistant", "content": [{"type": "text", "text": "reply 2"}], "timestamp": 6.0},
        ]
        md = serialize_context_md(entries)
        assert "turn 1" in md
        assert "turn 2" in md
        assert "\n---\n" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestSerializeContextMd::test_basic_user_assistant -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lingtai_kernel.context_serializer'`

- [ ] **Step 3: Implement `context_serializer.py`**

Create `src/lingtai_kernel/context_serializer.py`:

```python
"""Serialize chat_history.jsonl entries into context.md markdown.

Full fidelity — no truncation, no filtering. Every entry since the last
molt_boundary is rendered verbatim. The only entries skipped are
molt_boundary markers themselves.

System entries mark turn boundaries and are rendered with --- separators.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _ts_to_iso(ts: float) -> str:
    """Convert Unix timestamp to ISO 8601 string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_content_blocks(blocks: list[dict]) -> str:
    """Render content blocks to text."""
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        if btype == "text":
            parts.append(block["text"])
        elif btype == "thinking":
            parts.append(f"<thinking>\n{block['text']}\n</thinking>")
        elif btype == "tool_call":
            args_str = json.dumps(block["args"], ensure_ascii=False, default=str)
            parts.append(f"[tool_use: {block['name']}({args_str})]")
        elif btype == "tool_result":
            content = block["content"]
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            parts.append(f"[tool_result({block['name']}): {content}]")
    return "\n".join(parts)


def serialize_context_md(entries: list[dict]) -> str:
    """Serialize JSONL entries into markdown for the context section.

    Args:
        entries: List of dicts from chat_history.jsonl (post-molt-boundary).

    Returns:
        Markdown string. Empty string if no entries.
    """
    if not entries:
        return ""

    parts: list[str] = []
    seen_first_content = False

    for entry in entries:
        # Skip molt_boundary markers
        if entry.get("type") == "molt_boundary":
            continue

        ts = _ts_to_iso(entry.get("timestamp", 0))
        role = entry.get("role", "")

        if role == "system":
            # System entries mark turn boundaries
            if seen_first_content:
                parts.append("---")
            system_text = entry.get("system", "")
            parts.append(f"### system [{ts}]\n{system_text}")
            seen_first_content = True
        else:
            content_blocks = entry.get("content", [])
            rendered = _render_content_blocks(content_blocks)
            parts.append(f"### {role} [{ts}]\n{rendered}")
            seen_first_content = True

    return "\n\n".join(parts)
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestSerializeContextMd -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Smoke-test module**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "from lingtai_kernel.context_serializer import serialize_context_md; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/context_serializer.py tests/test_context_md.py
git commit -m "feat: create context_serializer — JSONL to markdown, full fidelity"
```

---

### Task 3: Make chat_history.jsonl Append-Only + Rebuild context.md on Idle

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py`
- Test: `tests/test_context_md.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_context_md.py`:

```python
from pathlib import Path
from unittest.mock import MagicMock
from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.llm.interface import TextBlock


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


class TestFlushContextToPrompt:
    def _make_agent(self, tmp_path):
        return BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )

    def test_flush_rebuilds_context_from_jsonl(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()

        # Simulate a completed turn — append to JSONL manually
        history_dir = tmp_path / "test" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 1713600000.0},
            {"id": 1, "role": "assistant", "content": [{"type": "text", "text": "hi"}], "timestamp": 1713600001.0},
        ]
        with open(history_dir / "chat_history.jsonl", "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        agent._rebuild_context_md()

        ctx = agent._prompt_manager.read_section("context")
        assert ctx is not None
        assert "hello" in ctx
        assert "hi" in ctx
        agent.stop(timeout=2.0)

    def test_flush_wipes_chat_interface(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("hello")
        iface.add_assistant_message([TextBlock(text="hi")])

        # Append to JSONL (simulating mid-turn audit)
        agent._append_chat_audit()
        # Now flush
        agent._flush_context_to_prompt()

        assert agent._session.chat is None
        agent.stop(timeout=2.0)

    def test_flush_persists_context_md_to_disk(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()

        history_dir = tmp_path / "test" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "persist test"}], "timestamp": 1713600000.0},
        ]
        with open(history_dir / "chat_history.jsonl", "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        agent._rebuild_context_md()

        context_file = tmp_path / "test" / "system" / "context.md"
        assert context_file.exists()
        assert "persist test" in context_file.read_text()
        agent.stop(timeout=2.0)

    def test_flush_only_reads_since_last_molt(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()

        history_dir = tmp_path / "test" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "before molt"}], "timestamp": 1.0},
            {"type": "molt_boundary", "molt_count": 1, "timestamp": 2.0, "summary": "x"},
            {"id": 0, "role": "user", "content": [{"type": "text", "text": "after molt"}], "timestamp": 3.0},
        ]
        with open(history_dir / "chat_history.jsonl", "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        agent._rebuild_context_md()

        ctx = agent._prompt_manager.read_section("context")
        assert "before molt" not in ctx
        assert "after molt" in ctx
        agent.stop(timeout=2.0)

    def test_flush_noop_when_no_jsonl(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.start()
        agent._rebuild_context_md()
        ctx = agent._prompt_manager.read_section("context")
        assert ctx is None
        agent.stop(timeout=2.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestFlushContextToPrompt::test_flush_rebuilds_context_from_jsonl -v`
Expected: FAIL — `AttributeError: 'BaseAgent' object has no attribute '_rebuild_context_md'`

- [ ] **Step 3: Implement methods in base_agent.py**

Add after `_save_chat_history` in `src/lingtai_kernel/base_agent.py`:

```python
    def _append_chat_audit(self) -> None:
        """Append current interface entries to chat_history.jsonl (audit log).

        Append-only — never rewrites. Each entry is one JSON line.
        """
        if self._session.chat is None:
            return
        state = self._session.get_chat_state()
        messages = state.get("messages")
        if not messages:
            return
        history_dir = self._working_dir / "history"
        history_dir.mkdir(exist_ok=True)
        try:
            with open(history_dir / "chat_history.jsonl", "a") as f:
                for entry in messages:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to append chat audit: {e}")

    def _rebuild_context_md(self) -> None:
        """Rebuild context.md from chat_history.jsonl since last molt boundary.

        Reads the JSONL, finds the last molt_boundary, serializes everything
        after it into markdown, and writes to the prompt manager + disk.
        """
        from .context_serializer import serialize_context_md

        jsonl_path = self._working_dir / "history" / "chat_history.jsonl"
        if not jsonl_path.is_file():
            return

        try:
            lines = jsonl_path.read_text().splitlines()
        except OSError:
            return

        # Find last molt boundary
        last_boundary_idx = -1
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "molt_boundary":
                    last_boundary_idx = i
            except json.JSONDecodeError:
                continue

        # Take entries after last molt boundary
        start = last_boundary_idx + 1
        entries = []
        for line in lines[start:]:
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not entries:
            return

        md = serialize_context_md(entries)
        if not md:
            return

        self._prompt_manager.write_section("context", md, protected=False)

        # Persist to disk
        context_file = self._working_dir / "system" / "context.md"
        context_file.parent.mkdir(exist_ok=True)
        context_file.write_text(md)

    def _flush_context_to_prompt(self) -> None:
        """Append current turn to audit log, rebuild context.md, wipe ChatInterface.

        Called on every idle transition.
        """
        # Append current turn to JSONL
        self._append_chat_audit()

        # Rebuild context.md from JSONL
        self._rebuild_context_md()

        # Wipe the ChatInterface
        if self._session.chat is not None:
            self._session._chat = None
            self._session._interaction_id = None

        # Update system prompt (includes the new context section)
        self._flush_system_prompt()
        self._session._token_decomp_dirty = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestFlushContextToPrompt -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Smoke-test module**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "from lingtai_kernel.base_agent import BaseAgent; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/base_agent.py tests/test_context_md.py
git commit -m "feat: implement _flush_context_to_prompt — rebuild context.md from JSONL"
```

---

### Task 4: Wire Idle Transition + Refactor `_save_chat_history`

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py` (lines 935-937, 1520-1561, 1129, 1215)

- [ ] **Step 1: Modify `_save_chat_history` to remove JSONL rewrite**

Replace the `_save_chat_history` method (lines 1520-1561) with:

```python
    def _save_chat_history(self) -> None:
        """Write manifest, status, and token ledger to disk.

        Chat history persistence is now handled by:
        - _append_chat_audit() — appends to chat_history.jsonl (mid-turn)
        - _flush_context_to_prompt() — appends + rebuilds context.md (on idle)

        This method only persists manifest, status, and token ledger.
        """
        # Update .agent.json with current state
        try:
            self._workdir.write_manifest(self._build_manifest())
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to update manifest: {e}")
        # Write .status.json — live runtime snapshot
        try:
            (self._working_dir / ".status.json").write_text(
                json.dumps(self.status(), ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to write .status.json: {e}")
        # Append per-call token usage to ledger
        usage, self._last_usage = self._last_usage, None
        if usage is not None:
            try:
                ledger_path = self._working_dir / "logs" / "token_ledger.jsonl"
                append_token_entry(
                    ledger_path,
                    input=usage.input_tokens,
                    output=usage.output_tokens,
                    thinking=usage.thinking_tokens,
                    cached=usage.cached_tokens,
                )
            except Exception as e:
                logger.warning(f"[{self.agent_name}] Failed to append token ledger: {e}")
```

- [ ] **Step 2: Wire `_flush_context_to_prompt` into idle transition**

Find lines 935-937:
```python
            if not self._asleep.is_set():
                self._set_state(sleep_state)
            self._save_chat_history()
```

Replace with:
```python
            if not self._asleep.is_set():
                self._set_state(sleep_state)
            self._flush_context_to_prompt()
            self._save_chat_history()
```

- [ ] **Step 3: Add mid-turn audit calls**

At line 1129 (after initial LLM response):
```python
        response = self._session.send(content)
        self._last_usage = response.usage
        self._append_chat_audit()
        self._save_chat_history()
```

At line 1215 (after tool-use round):
```python
            response = self._session.send(tool_results)
            self._last_usage = response.usage
            self._append_chat_audit()
            self._save_chat_history()
```

- [ ] **Step 4: Run existing tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_agent.py tests/test_context_md.py -v`
Expected: PASS

- [ ] **Step 5: Smoke-test**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "from lingtai_kernel.base_agent import BaseAgent; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/base_agent.py
git commit -m "feat: wire idle flush + refactor _save_chat_history to audit-only"
```

---

### Task 5: Modify Startup — Load context.md, Remove ChatInterface Restore

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py` (lines 193-205, 427-434)
- Test: `tests/test_context_md.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_context_md.py`:

```python
class TestStartupContextLoad:
    def test_startup_loads_context_md_into_prompt(self, tmp_path):
        work_dir = tmp_path / "test"
        work_dir.mkdir(parents=True)
        system_dir = work_dir / "system"
        system_dir.mkdir()
        (system_dir / "context.md").write_text("### user [2026-04-20T10:00:00Z]\nprevious conversation")

        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=work_dir,
        )
        ctx = agent._prompt_manager.read_section("context")
        assert ctx is not None
        assert "previous conversation" in ctx

    def test_startup_does_not_restore_chat_interface(self, tmp_path):
        work_dir = tmp_path / "test"
        work_dir.mkdir(parents=True)
        history_dir = work_dir / "history"
        history_dir.mkdir()
        entry = {"id": 0, "role": "user", "content": [{"type": "text", "text": "old msg"}], "timestamp": 1.0}
        (history_dir / "chat_history.jsonl").write_text(json.dumps(entry) + "\n")

        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=work_dir,
        )
        agent.start()
        assert agent._session.chat is None
        agent.stop(timeout=2.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestStartupContextLoad::test_startup_does_not_restore_chat_interface -v`
Expected: FAIL — currently restores from JSONL

- [ ] **Step 3: Modify `__init__` — load context.md**

Near line 202 (after `loaded_pad` section load), add:

```python
        # Load existing context from system/context.md (survives restarts within a molt)
        context_md = system_dir / "context.md"
        if context_md.is_file():
            try:
                context_content = context_md.read_text().strip()
                if context_content:
                    self._prompt_manager.write_section("context", context_content, protected=False)
            except OSError:
                pass
```

- [ ] **Step 4: Modify `start()` — remove chat_history.jsonl restore**

Replace lines 427-434:
```python
        chat_history_file = self._working_dir / "history" / "chat_history.jsonl"
        if chat_history_file.is_file():
            try:
                messages = [json.loads(line) for line in chat_history_file.read_text().splitlines() if line.strip()]
                self.restore_chat({"messages": messages})
                self._log("session_restored")
            except Exception as e:
                logger.warning(f"[{self.agent_name}] Failed to restore chat history: {e}")
```

With:
```python
        # chat_history.jsonl is now an append-only audit log.
        # Conversation context is served from the "context" section
        # (loaded from system/context.md in __init__).
```

- [ ] **Step 5: Run tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestStartupContextLoad tests/test_agent.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/base_agent.py tests/test_context_md.py
git commit -m "feat: load context.md on startup, remove chat_history.jsonl restore"
```

---

### Task 6: Clear Context on Molt + Write Boundary Marker

**Files:**
- Modify: `src/lingtai_kernel/intrinsics/eigen.py`
- Test: `tests/test_context_md.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_context_md.py`:

```python
class TestMoltClearsContext:
    def test_molt_deletes_context_section(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )
        agent.start()
        agent._prompt_manager.write_section("context", "old context content")
        context_file = tmp_path / "test" / "system" / "context.md"
        context_file.parent.mkdir(exist_ok=True)
        context_file.write_text("old context content")

        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("trigger molt")

        from lingtai_kernel.intrinsics.eigen import _context_molt
        result = _context_molt(agent, {"summary": "test summary"})

        assert result["status"] == "ok"
        assert agent._prompt_manager.read_section("context") is None
        assert not context_file.exists()
        agent.stop(timeout=2.0)

    def test_molt_writes_boundary_marker(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test",
        )
        agent.start()
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("pre-molt msg")

        from lingtai_kernel.intrinsics.eigen import _context_molt
        _context_molt(agent, {"summary": "molt summary"})

        audit_file = tmp_path / "test" / "history" / "chat_history.jsonl"
        assert audit_file.exists()
        lines = [json.loads(l) for l in audit_file.read_text().splitlines() if l.strip()]
        boundary = lines[-1]
        assert boundary["type"] == "molt_boundary"
        assert boundary["molt_count"] == 1
        assert boundary["summary"] == "molt summary"
        agent.stop(timeout=2.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestMoltClearsContext::test_molt_deletes_context_section -v`
Expected: FAIL

- [ ] **Step 3: Modify `_context_molt` in eigen.py**

After line 133 (`agent._session._interaction_id = None`), add:

```python
    # Clear context section (context.md)
    agent._prompt_manager.delete_section("context")
    context_file = agent._working_dir / "system" / "context.md"
    if context_file.exists():
        context_file.unlink()
```

After line 140 (`agent._molt_count += 1`), add:

```python
    # Write molt boundary marker to audit log
    import time as _time
    import json as _json
    boundary = {
        "type": "molt_boundary",
        "molt_count": agent._molt_count,
        "timestamp": _time.time(),
        "summary": summary,
    }
    history_dir = agent._working_dir / "history"
    history_dir.mkdir(exist_ok=True)
    try:
        with open(history_dir / "chat_history.jsonl", "a") as f:
            f.write(_json.dumps(boundary, ensure_ascii=False) + "\n")
    except OSError:
        pass
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestMoltClearsContext -v`
Expected: PASS

- [ ] **Step 5: Smoke-test**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "from lingtai_kernel.intrinsics.eigen import _context_molt; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add src/lingtai_kernel/intrinsics/eigen.py tests/test_context_md.py
git commit -m "feat: clear context.md on molt + write boundary marker to audit log"
```

---

### Task 7: TUI — Delete context.md on Refresh

**Files:**
- Modify: `tui/internal/tui/app.go:652`

- [ ] **Step 1: Add the deletion line**

After line 652 (`os.Remove(filepath.Join(dir, "history", "chat_history.jsonl"))`), add:

```go
os.Remove(filepath.Join(dir, "system", "context.md"))
```

- [ ] **Step 2: Build to verify compilation**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai/tui && make build`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add tui/internal/tui/app.go
git commit -m "feat: delete system/context.md on refresh"
```

---

### Task 8: End-to-End Integration Test

**Files:**
- Test: `tests/test_context_md.py`

- [ ] **Step 1: Write end-to-end test**

Append to `tests/test_context_md.py`:

```python
class TestEndToEnd:
    def test_full_lifecycle(self, tmp_path):
        """start -> message -> idle -> message -> idle -> molt -> restart"""
        work_dir = tmp_path / "agent"
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="e2e",
            working_dir=work_dir,
        )
        agent.start()

        # --- Turn 1 ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("first message")
        iface.add_assistant_message([TextBlock(text="first reply")])
        agent._flush_context_to_prompt()
        agent._save_chat_history()

        assert agent._session.chat is None
        ctx = agent._prompt_manager.read_section("context")
        assert "first message" in ctx
        assert "first reply" in ctx
        assert (work_dir / "system" / "context.md").exists()

        # --- Turn 2 ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("second message")
        iface.add_assistant_message([TextBlock(text="second reply")])
        agent._flush_context_to_prompt()
        agent._save_chat_history()

        ctx = agent._prompt_manager.read_section("context")
        assert "first message" in ctx
        assert "second message" in ctx

        # --- Molt ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("about to molt")
        from lingtai_kernel.intrinsics.eigen import _context_molt
        _context_molt(agent, {"summary": "I learned things"})

        assert agent._prompt_manager.read_section("context") is None
        assert not (work_dir / "system" / "context.md").exists()

        # Audit log has everything
        audit_lines = (work_dir / "history" / "chat_history.jsonl").read_text().splitlines()
        entries = [json.loads(l) for l in audit_lines if l.strip()]
        boundaries = [e for e in entries if e.get("type") == "molt_boundary"]
        assert len(boundaries) == 1
        assert boundaries[0]["molt_count"] == 1

        # --- Post-molt turn ---
        agent._session.ensure_session()
        iface = agent._session.chat.interface
        iface.add_user_message("new life")
        iface.add_assistant_message([TextBlock(text="fresh start")])
        agent._flush_context_to_prompt()

        ctx = agent._prompt_manager.read_section("context")
        assert "new life" in ctx
        assert "first message" not in ctx  # pre-molt content gone

        # --- Restart ---
        agent.stop(timeout=2.0)
        agent2 = BaseAgent(
            service=make_mock_service(),
            agent_name="e2e",
            working_dir=work_dir,
        )
        ctx2 = agent2._prompt_manager.read_section("context")
        assert "new life" in ctx2
        assert "first message" not in ctx2
```

- [ ] **Step 2: Run integration test**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_context_md.py::TestEndToEnd -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_context_md.py
git commit -m "test: add end-to-end integration test for context.md lifecycle"
```

---

## Verification Checklist

1. `python -m pytest tests/test_context_md.py -v` — all new tests pass
2. `python -m pytest tests/ -v` — no regressions
3. `python -c "from lingtai_kernel.base_agent import BaseAgent"` — imports clean
4. `cd tui && make build` — TUI compiles
5. Manual: run agent, send message, verify `system/context.md` appears after idle
6. Manual: trigger molt, verify context.md deleted + boundary in JSONL
7. Manual: restart agent, verify context.md loaded from disk
