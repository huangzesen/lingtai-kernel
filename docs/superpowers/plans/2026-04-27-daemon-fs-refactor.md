# Daemon Filesystem Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make daemon emanations filesystem-backed mini-avatars: each run gets `<parent>/daemons/em-<N>-<YYYYMMDD-HHMMSS>-<hash6>/` with `daemon.json`, `.prompt`, `.heartbeat`, `history/chat_history.jsonl`, `logs/{token_ledger,events}.jsonl`. Folders persist forever; `reclaim` only stops processes. Per-daemon token attribution; parent's ledger gets tagged copies so lifetime totals stay correct.

**Architecture:** Extract all filesystem effects into a new `DaemonRunDir` class in `core/daemon/run_dir.py`. `DaemonManager` constructs one per emanation, calls into it at every hook (start, per-turn, per-tool-dispatch, terminal). Threading model unchanged. `lingtai_kernel.token_ledger.append_token_entry` gains an optional `extra` kwarg for tagged daemon entries. Parent inbox notification, blacklist, ask-followup, and reclaim semantics preserved.

**Tech Stack:** Python 3.11+, `concurrent.futures.ThreadPoolExecutor`, `threading.Event`, `pathlib.Path`, `json`, `secrets.token_hex`, `os.replace` for atomic JSON writes, POSIX `O_APPEND` for JSONL appends. Tests use `pytest`, `tmp_path` fixture, `unittest.mock.MagicMock`.

**Spec:** [`docs/superpowers/specs/2026-04-27-daemon-fs-refactor-design.md`](../specs/2026-04-27-daemon-fs-refactor-design.md)

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/lingtai/core/daemon/run_dir.py` | NEW | `DaemonRunDir` class — owns folder creation, `daemon.json` atomic writes, JSONL appends, heartbeat, terminal markers |
| `src/lingtai/core/daemon/__init__.py` | MODIFY | `DaemonManager` orchestration only; calls `DaemonRunDir` at hook points; removes inline token-ledger aggregator |
| `src/lingtai/core/daemon/manual/SKILL.md` | MODIFY | Populated with FS layout reference and inspection patterns |
| `src/lingtai_kernel/token_ledger.py` | MODIFY | `append_token_entry` gains `extra: dict | None = None` kwarg |
| `src/lingtai/i18n/en.json` | MODIFY | Update `daemon.description` to mention FS visibility; add inspection guidance hint |
| `src/lingtai/i18n/zh.json` | MODIFY | Mirror en updates |
| `src/lingtai/i18n/wen.json` | MODIFY | Mirror en updates |
| `tests/test_daemon_run_dir.py` | NEW | Pure FS unit tests for `DaemonRunDir` |
| `tests/test_daemon.py` | MODIFY | Update existing tests for FS-backed semantics; add E2E folder-creation/preservation tests |
| `tests/test_token_ledger.py` | NEW | Unit tests for `append_token_entry`'s new `extra` kwarg (kernel-side) |

---

## Task Sequence

The plan proceeds bottom-up: first the kernel-side `token_ledger` change (foundation), then the new `DaemonRunDir` class with full unit tests, then `DaemonManager` refactor consuming it, then i18n + manual + final integration tests.

---

### Task 1: Extend `append_token_entry` to accept tagged extras

**Files:**
- Modify: `src/lingtai_kernel/token_ledger.py`
- Test: `tests/test_token_ledger.py` (NEW)

The daemon needs to write tagged entries (`{source: "daemon", em_id, run_id}`) to the parent's ledger. The current `append_token_entry` signature is closed. We add an optional `extra: dict | None = None` keyword that merges into the entry dict before serialization. Existing callers stay untouched.

- [ ] **Step 1.1: Write the failing test for `extra` kwarg merging**

Create `tests/test_token_ledger.py`:

```python
"""Tests for token_ledger.append_token_entry — including the optional extra kwarg
that lets daemon writes carry attribution tags into the parent's ledger."""
import json

from lingtai_kernel.token_ledger import append_token_entry, sum_token_ledger


def test_append_token_entry_basic(tmp_path):
    """Default behavior: writes ts + 4 numeric fields."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1)
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["input"] == 10
    assert entry["output"] == 5
    assert entry["thinking"] == 2
    assert entry["cached"] == 1
    assert "ts" in entry
    assert "source" not in entry  # no extras


def test_append_token_entry_with_extra(tmp_path):
    """`extra` dict keys merge into the entry before serialization."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(
        path,
        input=10, output=5, thinking=2, cached=1,
        extra={"source": "daemon", "em_id": "em-3",
               "run_id": "em-3-20260427-094215-a1b2c3"},
    )
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["source"] == "daemon"
    assert entry["em_id"] == "em-3"
    assert entry["run_id"] == "em-3-20260427-094215-a1b2c3"
    # numeric fields still present
    assert entry["input"] == 10


def test_extra_does_not_break_summing(tmp_path):
    """sum_token_ledger ignores unknown keys — daemon tags do not affect totals."""
    path = tmp_path / "ledger.jsonl"
    append_token_entry(path, input=10, output=5, thinking=2, cached=1)
    append_token_entry(
        path,
        input=20, output=8, thinking=3, cached=4,
        extra={"source": "daemon", "em_id": "em-1", "run_id": "x"},
    )
    totals = sum_token_ledger(path)
    assert totals["input_tokens"] == 30
    assert totals["output_tokens"] == 13
    assert totals["thinking_tokens"] == 5
    assert totals["cached_tokens"] == 5
    assert totals["api_calls"] == 2


def test_extra_cannot_override_required_fields(tmp_path):
    """Required fields (input/output/thinking/cached/ts) take precedence over `extra`.

    This protects against accidental tag conflicts. If a caller passes
    extra={"input": 999}, the explicit input=10 still wins.
    """
    path = tmp_path / "ledger.jsonl"
    append_token_entry(
        path,
        input=10, output=5, thinking=2, cached=1,
        extra={"input": 999, "ts": "fake"},
    )
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["input"] == 10
    assert entry["ts"] != "fake"
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd ~/Documents/GitHub/lingtai-kernel
.venv/bin/pytest tests/test_token_ledger.py -v
```

Expected: FAIL — `test_append_token_entry_with_extra` and `test_extra_cannot_override_required_fields` fail because `append_token_entry` does not accept `extra`. (`test_append_token_entry_basic` and `test_extra_does_not_break_summing` may pass partially; the suite as a whole must fail.)

- [ ] **Step 1.3: Implement the `extra` kwarg**

In `src/lingtai_kernel/token_ledger.py`, replace the function definition:

```python
def append_token_entry(
    path: Path | str,
    *,
    input: int,
    output: int,
    thinking: int,
    cached: int,
    extra: dict | None = None,
) -> None:
    """Append one token usage entry to the ledger.

    Creates parent directories and the file if they don't exist.

    `extra` is an optional dict of additional fields merged into the entry.
    Required fields (ts/input/output/thinking/cached) take precedence — if a
    caller passes `extra={"input": 999}`, the explicit input value still wins.
    Used by the daemon capability to tag entries with source/em_id/run_id
    so the parent's ledger preserves per-daemon attribution.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict = {}
    if extra:
        entry.update(extra)
    entry.update({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": input,
        "output": output,
        "thinking": thinking,
        "cached": cached,
    })
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

The order matters: `extra` is laid down first, then required fields overwrite. This is what `test_extra_cannot_override_required_fields` verifies.

- [ ] **Step 1.4: Run the new tests; expect pass**

```bash
.venv/bin/pytest tests/test_token_ledger.py -v
```

Expected: 4 PASSED.

- [ ] **Step 1.5: Run the full test suite to confirm no regressions**

```bash
.venv/bin/pytest -x
```

Expected: all previously-passing tests still pass (existing callers in `base_agent.py`, `intrinsics/soul.py`, `core/daemon/__init__.py` use only the keyword-only required fields and are unaffected).

- [ ] **Step 1.6: Commit**

```bash
git add src/lingtai_kernel/token_ledger.py tests/test_token_ledger.py
git commit -m "feat(token_ledger): add optional extra kwarg for tagged entries

Allows daemon writes to tag entries in the parent's ledger with
{source, em_id, run_id} for per-daemon attribution while preserving
the schema sum_token_ledger reads."
```

---

### Task 2: Create `DaemonRunDir` skeleton — construction + identity-card writes

**Files:**
- Create: `src/lingtai/core/daemon/run_dir.py`
- Test: `tests/test_daemon_run_dir.py` (NEW)

This task produces a minimal `DaemonRunDir` that handles construction (folder layout, initial `daemon.json`, `.prompt`, `.heartbeat`, `daemon_start` event). Mutating methods come in later tasks.

- [ ] **Step 2.1: Write failing tests for construction**

Create `tests/test_daemon_run_dir.py`:

```python
"""Pure FS unit tests for DaemonRunDir — no threads, no LLM mocks."""
import json
import re
from pathlib import Path

from lingtai.core.daemon.run_dir import DaemonRunDir


def _make_run_dir(tmp_path: Path, **overrides) -> DaemonRunDir:
    """Helper: construct a DaemonRunDir with sensible defaults."""
    parent_wd = tmp_path / "parent"
    parent_wd.mkdir()
    kwargs = dict(
        parent_working_dir=parent_wd,
        handle="em-3",
        task="find todos",
        tools=["file"],
        model="mock-model",
        max_turns=30,
        timeout_s=300.0,
        parent_addr="parent",
        parent_pid=12345,
        system_prompt="You are a daemon emanation.",
    )
    kwargs.update(overrides)
    return DaemonRunDir(**kwargs)


def test_construct_creates_folder_structure(tmp_path):
    rd = _make_run_dir(tmp_path)
    assert rd.path.is_dir()
    assert (rd.path / "history").is_dir()
    assert (rd.path / "logs").is_dir()
    assert rd.daemon_json_path.is_file()
    assert rd.prompt_path.is_file()
    assert rd.heartbeat_path.is_file()


def test_run_id_format(tmp_path):
    """run_id is em-<N>-<YYYYMMDD-HHMMSS>-<6 hex chars>."""
    rd = _make_run_dir(tmp_path, handle="em-7")
    assert re.fullmatch(r"em-7-\d{8}-\d{6}-[0-9a-f]{6}", rd.run_id)
    assert rd.path.name == rd.run_id


def test_folder_lives_under_parent_daemons_dir(tmp_path):
    rd = _make_run_dir(tmp_path)
    assert rd.path.parent == tmp_path / "parent" / "daemons"


def test_initial_daemon_json_fields(tmp_path):
    rd = _make_run_dir(tmp_path)
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["handle"] == "em-3"
    assert data["run_id"] == rd.run_id
    assert data["parent_addr"] == "parent"
    assert data["parent_pid"] == 12345
    assert data["task"] == "find todos"
    assert data["tools"] == ["file"]
    assert data["model"] == "mock-model"
    assert data["max_turns"] == 30
    assert data["timeout_s"] == 300.0
    assert data["state"] == "running"
    assert data["finished_at"] is None
    assert data["turn"] == 0
    assert data["current_tool"] is None
    assert data["tool_call_count"] == 0
    assert data["tokens"] == {"input": 0, "output": 0, "thinking": 0, "cached": 0}
    assert data["result_preview"] is None
    assert data["error"] is None
    # started_at is ISO 8601 UTC
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["started_at"])


def test_prompt_written_verbatim(tmp_path):
    prompt = "You are a daemon emanation.\nYour task is X.\nUse tools wisely."
    rd = _make_run_dir(tmp_path, system_prompt=prompt)
    assert rd.prompt_path.read_text() == prompt


def test_daemon_start_event_logged(tmp_path):
    rd = _make_run_dir(tmp_path)
    events_path = rd.path / "logs" / "events.jsonl"
    assert events_path.is_file()
    line = events_path.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["event"] == "daemon_start"
    assert "ts" in entry


def test_two_constructions_same_handle_no_collision(tmp_path):
    """Two run_dirs with the same handle in the same second get distinct folders."""
    rd1 = _make_run_dir(tmp_path, handle="em-1")
    rd2 = _make_run_dir(tmp_path, handle="em-1")
    assert rd1.run_id != rd2.run_id
    assert rd1.path != rd2.path
    assert rd1.path.is_dir()
    assert rd2.path.is_dir()


def test_path_properties_consistent(tmp_path):
    rd = _make_run_dir(tmp_path)
    assert rd.daemon_json_path == rd.path / "daemon.json"
    assert rd.prompt_path == rd.path / ".prompt"
    assert rd.heartbeat_path == rd.path / ".heartbeat"
    assert rd.chat_path == rd.path / "history" / "chat_history.jsonl"
    assert rd.events_path == rd.path / "logs" / "events.jsonl"
    assert rd.token_ledger_path == rd.path / "logs" / "token_ledger.jsonl"
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -v
```

Expected: FAIL — `ImportError: cannot import name 'DaemonRunDir' from 'lingtai.core.daemon.run_dir'` (module doesn't exist).

- [ ] **Step 2.3: Create `DaemonRunDir` with construction + initial writes**

Create `src/lingtai/core/daemon/run_dir.py`:

```python
"""Per-emanation filesystem run directory.

Each daemon emanation gets one DaemonRunDir, which owns every filesystem
effect for that run: folder layout, daemon.json atomic writes, JSONL appends,
heartbeat touches, terminal state markers. The DaemonManager calls into a
DaemonRunDir at every hook (start, per-turn, per-tool-dispatch, terminal)
without itself touching the filesystem.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from lingtai_kernel.token_ledger import append_token_entry


class DaemonRunDir:
    """Filesystem-backed mini-avatar log surface for one daemon emanation.

    Folder layout:
        <parent>/daemons/em-<N>-<YYYYMMDD-HHMMSS>-<hash6>/
            daemon.json                  # identity card + live status
            .prompt                      # system prompt verbatim
            .heartbeat                   # mtime-touched on activity
            history/chat_history.jsonl   # session transcript
            logs/token_ledger.jsonl      # per-call tokens, daemon-scoped
            logs/events.jsonl            # tool_call, tool_result, daemon_*
    """

    def __init__(
        self,
        *,
        parent_working_dir: Path,
        handle: str,
        task: str,
        tools: list[str],
        model: str,
        max_turns: int,
        timeout_s: float,
        parent_addr: str,
        parent_pid: int,
        system_prompt: str,
    ):
        self._handle = handle
        self._parent_token_ledger = parent_working_dir / "logs" / "token_ledger.jsonl"
        self._started_monotonic = time.monotonic()
        started_at_iso = self._now_iso()

        # run_id format: em-<N>-<YYYYMMDD-HHMMSS>-<6 hex>
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        hash6 = secrets.token_hex(3)
        self._run_id = f"{handle}-{timestamp}-{hash6}"

        self._path = parent_working_dir / "daemons" / self._run_id

        # Identity-card construction is strict — failures here propagate up to
        # _handle_emanate which converts them into a tool-level error response.
        self._path.mkdir(parents=True, exist_ok=False)
        (self._path / "history").mkdir()
        (self._path / "logs").mkdir()

        self._initial_state = {
            "handle": handle,
            "run_id": self._run_id,
            "parent_addr": parent_addr,
            "parent_pid": parent_pid,
            "task": task,
            "tools": list(tools),
            "model": model,
            "max_turns": max_turns,
            "timeout_s": timeout_s,
            "state": "running",
            "started_at": started_at_iso,
            "finished_at": None,
            "elapsed_s": 0.0,
            "turn": 0,
            "current_tool": None,
            "tool_call_count": 0,
            "tokens": {"input": 0, "output": 0, "thinking": 0, "cached": 0},
            "result_preview": None,
            "error": None,
        }
        self._state = dict(self._initial_state)

        self._atomic_write_json(self.daemon_json_path, self._state)
        self.prompt_path.write_text(system_prompt)
        self.heartbeat_path.touch()
        self._append_jsonl(self.events_path,
                           {"event": "daemon_start", "ts": self._now_iso()})

    # ------------------------------------------------------------------
    # Path properties
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def handle(self) -> str:
        return self._handle

    @property
    def path(self) -> Path:
        return self._path

    @property
    def daemon_json_path(self) -> Path:
        return self._path / "daemon.json"

    @property
    def prompt_path(self) -> Path:
        return self._path / ".prompt"

    @property
    def heartbeat_path(self) -> Path:
        return self._path / ".heartbeat"

    @property
    def chat_path(self) -> Path:
        return self._path / "history" / "chat_history.jsonl"

    @property
    def events_path(self) -> Path:
        return self._path / "logs" / "events.jsonl"

    @property
    def token_ledger_path(self) -> Path:
        return self._path / "logs" / "token_ledger.jsonl"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _now_secs(self) -> float:
        return round(time.monotonic() - self._started_monotonic, 3)

    def _atomic_write_json(self, path: Path, data: dict) -> None:
        """Write JSON to a tempfile then os.replace — readers never see partial state."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, path)

    def _append_jsonl(self, path: Path, entry: dict) -> None:
        """Append one JSON line. Single-writer per file — POSIX O_APPEND atomic for sub-PIPE_BUF lines."""
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _safe(self, op: str, fn) -> None:
        """Run `fn`; swallow OSError (best-effort policy for mutation writes)."""
        try:
            fn()
        except OSError:
            # Best-effort: missing a status update is far less harmful than
            # crashing the LLM loop. Not logged here — the DaemonManager
            # owns logging via _agent._log.
            pass
```

- [ ] **Step 2.4: Run construction tests; expect pass**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -v
```

Expected: 8 PASSED.

- [ ] **Step 2.5: Commit**

```bash
git add src/lingtai/core/daemon/run_dir.py tests/test_daemon_run_dir.py
git commit -m "feat(daemon): add DaemonRunDir skeleton — construction + identity card

Creates per-emanation folder under daemons/em-N-YYYYMMDD-HHMMSS-hash6/,
writes initial daemon.json (state=running, all counters zero), .prompt
verbatim, .heartbeat touch, and a daemon_start event. Mutating methods
land in subsequent commits."
```

---

### Task 3: `record_user_send` and `bump_turn` — chat-history writes

**Files:**
- Modify: `src/lingtai/core/daemon/run_dir.py`
- Modify: `tests/test_daemon_run_dir.py`

These are the per-LLM-round hooks: write a user line before `session.send`, write an assistant line after the response, update `daemon.json`'s `turn`/`elapsed_s` atomically.

- [ ] **Step 3.1: Append failing tests**

Add to `tests/test_daemon_run_dir.py`:

```python
def test_record_user_send_task_kind(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("find todos", kind="task")
    line = rd.chat_path.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["role"] == "user"
    assert entry["text"] == "find todos"
    assert entry["kind"] == "task"
    assert entry["turn"] == 0
    assert "ts" in entry


def test_record_user_send_tool_results_verbatim(tmp_path):
    """Tool result payloads written verbatim — no truncation."""
    rd = _make_run_dir(tmp_path)
    big = "x" * 50_000
    rd.record_user_send(big, kind="tool_results")
    line = rd.chat_path.read_text().splitlines()[-1]
    entry = json.loads(line)
    assert entry["text"] == big
    assert entry["kind"] == "tool_results"


def test_record_user_send_followup_kind(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("also check tests/", kind="followup")
    entry = json.loads(rd.chat_path.read_text().splitlines()[0])
    assert entry["kind"] == "followup"


def test_bump_turn_updates_daemon_json(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.bump_turn(turn=1, response_text="Scanning...")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["turn"] == 1
    assert data["current_tool"] is None
    assert data["elapsed_s"] >= 0.0
    assert data["state"] == "running"  # unchanged


def test_bump_turn_appends_assistant_chat_entry(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.bump_turn(turn=1, response_text="Scanning files...")
    line = rd.chat_path.read_text().splitlines()[-1]
    entry = json.loads(line)
    assert entry["role"] == "assistant"
    assert entry["text"] == "Scanning files..."
    assert entry["turn"] == 1


def test_bump_turn_advances_heartbeat(tmp_path):
    rd = _make_run_dir(tmp_path)
    initial_mtime = rd.heartbeat_path.stat().st_mtime
    time.sleep(0.05)
    rd.bump_turn(turn=1, response_text="ok")
    assert rd.heartbeat_path.stat().st_mtime > initial_mtime


def test_record_user_send_uses_current_turn(tmp_path):
    """user-send entries record the current turn (so tool_results land at turn=1
    after the first assistant response, not turn=0)."""
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("task", kind="task")
    rd.bump_turn(turn=1, response_text="response 1")
    rd.record_user_send("tool result", kind="tool_results")
    entries = [json.loads(line) for line in rd.chat_path.read_text().splitlines()]
    assert entries[0]["turn"] == 0  # initial task at turn 0
    assert entries[1]["turn"] == 1  # assistant response
    assert entries[2]["turn"] == 1  # tool result, fed into turn-1 send
```

Note: `import time` is already at the top of the test file via the helper — if not, add `import time` to the file imports.

- [ ] **Step 3.2: Run tests; expect failure**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py::test_record_user_send_task_kind -v
```

Expected: FAIL — `AttributeError: 'DaemonRunDir' object has no attribute 'record_user_send'`.

- [ ] **Step 3.3: Implement `record_user_send` and `bump_turn`**

Add to `src/lingtai/core/daemon/run_dir.py` inside the `DaemonRunDir` class:

```python
    # ------------------------------------------------------------------
    # Per-turn hooks
    # ------------------------------------------------------------------

    def record_user_send(self, text: str, kind: str) -> None:
        """Append a user-role entry to chat_history.jsonl before session.send.

        kind ∈ {"task", "tool_results", "followup"}. Tool result payloads are
        written verbatim — no truncation. Chat history is forensic; we want
        full fidelity. Single-writer per file (only the run thread).
        """
        def _write():
            self._append_jsonl(
                self.chat_path,
                {
                    "role": "user",
                    "text": text,
                    "kind": kind,
                    "turn": self._state["turn"],
                    "ts": self._now_iso(),
                },
            )
        self._safe("record_user_send", _write)

    def bump_turn(self, turn: int, response_text: str) -> None:
        """Mark the end of an LLM round.

        Updates daemon.json (turn, elapsed_s, current_tool=null) atomically,
        appends an assistant entry to chat_history, touches heartbeat.
        """
        def _write():
            self._state["turn"] = turn
            self._state["current_tool"] = None
            self._state["elapsed_s"] = self._now_secs()
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.chat_path,
                {
                    "role": "assistant",
                    "text": response_text,
                    "turn": turn,
                    "ts": self._now_iso(),
                },
            )
            self.heartbeat_path.touch()
        self._safe("bump_turn", _write)
```

- [ ] **Step 3.4: Run tests; expect pass**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -v
```

Expected: all tests pass (8 from Task 2 + 7 new = 15).

- [ ] **Step 3.5: Commit**

```bash
git add src/lingtai/core/daemon/run_dir.py tests/test_daemon_run_dir.py
git commit -m "feat(daemon): DaemonRunDir.record_user_send + bump_turn

Per-turn hooks: append user-role entry to chat_history before each
session.send (kind: task/tool_results/followup), bump_turn updates
daemon.json (turn, elapsed_s, current_tool cleared), appends
assistant entry, touches heartbeat. Tool result payloads written
verbatim — chat history is forensic."
```

---

### Task 4: Tool-dispatch hooks (`set_current_tool`, `clear_current_tool`)

**Files:**
- Modify: `src/lingtai/core/daemon/run_dir.py`
- Modify: `tests/test_daemon_run_dir.py`

Per-tool-dispatch state: bump `current_tool` and `tool_call_count` in `daemon.json`, log `tool_call` event with args preview, log `tool_result` event when handler returns.

- [ ] **Step 4.1: Add failing tests**

Append to `tests/test_daemon_run_dir.py`:

```python
def test_set_current_tool_updates_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "src/main.py"})
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["current_tool"] == "read"
    assert data["tool_call_count"] == 1


def test_set_current_tool_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "src/main.py"})
    # daemon_start was line 1; tool_call should be the next line
    lines = rd.events_path.read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["event"] == "tool_call"
    assert entry["name"] == "read"
    assert "args_preview" in entry
    assert "ts" in entry


def test_set_current_tool_args_preview_truncated(tmp_path):
    """args_preview is bounded — full args could be huge (e.g., write())."""
    rd = _make_run_dir(tmp_path)
    big_content = "x" * 10_000
    rd.set_current_tool("write", {"path": "out.txt", "content": big_content})
    entry = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert len(entry["args_preview"]) <= 500


def test_set_current_tool_advances_heartbeat(tmp_path):
    rd = _make_run_dir(tmp_path)
    initial = rd.heartbeat_path.stat().st_mtime
    time.sleep(0.05)
    rd.set_current_tool("read", {})
    assert rd.heartbeat_path.stat().st_mtime > initial


def test_clear_current_tool_resets_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "x"})
    rd.clear_current_tool(result_status="ok")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["current_tool"] is None
    assert data["tool_call_count"] == 1  # unchanged


def test_clear_current_tool_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "x"})
    rd.clear_current_tool(result_status="ok")
    lines = rd.events_path.read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["event"] == "tool_result"
    assert last["name"] == "read"
    assert last["status"] == "ok"


def test_multiple_tool_dispatches_increment_count(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {})
    rd.clear_current_tool(result_status="ok")
    rd.set_current_tool("write", {})
    rd.clear_current_tool(result_status="ok")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["tool_call_count"] == 2
```

- [ ] **Step 4.2: Run tests; expect failure**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -k "current_tool" -v
```

Expected: FAIL — `set_current_tool`/`clear_current_tool` don't exist.

- [ ] **Step 4.3: Implement tool hooks**

Add to `src/lingtai/core/daemon/run_dir.py` inside the `DaemonRunDir` class:

```python
    # ------------------------------------------------------------------
    # Tool dispatch hooks
    # ------------------------------------------------------------------

    _ARGS_PREVIEW_MAX = 500

    def set_current_tool(self, name: str, args: dict) -> None:
        """Mark a tool dispatch starting.

        Increments tool_call_count, sets current_tool, logs tool_call event,
        touches heartbeat. Tracked tool name (current_tool) is what the parent
        sees on a `cat daemon.json` poll.
        """
        def _write():
            self._state["current_tool"] = name
            self._state["tool_call_count"] += 1
            self._atomic_write_json(self.daemon_json_path, self._state)
            args_preview = json.dumps(args, ensure_ascii=False)
            if len(args_preview) > self._ARGS_PREVIEW_MAX:
                args_preview = args_preview[:self._ARGS_PREVIEW_MAX] + "...[truncated]"
            self._append_jsonl(
                self.events_path,
                {
                    "event": "tool_call",
                    "name": name,
                    "args_preview": args_preview,
                    "turn": self._state["turn"],
                    "ts": self._now_iso(),
                },
            )
            self.heartbeat_path.touch()
        self._safe("set_current_tool", _write)

    def clear_current_tool(self, result_status: str) -> None:
        """Mark a tool dispatch finished.

        Clears current_tool in daemon.json, logs tool_result event.
        result_status is "ok" on normal returns or "error" when the handler
        raised or returned {"status": "error", ...}.
        """
        def _write():
            tool_name = self._state["current_tool"]
            self._state["current_tool"] = None
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": "tool_result",
                    "name": tool_name,
                    "status": result_status,
                    "turn": self._state["turn"],
                    "ts": self._now_iso(),
                },
            )
        self._safe("clear_current_tool", _write)
```

- [ ] **Step 4.4: Run tests; expect pass**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -v
```

Expected: 22 passed (15 prior + 7 new).

- [ ] **Step 4.5: Commit**

```bash
git add src/lingtai/core/daemon/run_dir.py tests/test_daemon_run_dir.py
git commit -m "feat(daemon): DaemonRunDir tool-dispatch hooks

set_current_tool bumps tool_call_count, sets current_tool, logs
tool_call event with args_preview (truncated at 500 chars).
clear_current_tool resets current_tool, logs tool_result with status.
Both update daemon.json atomically — readers see consistent state."
```

---

### Task 5: `append_tokens` — dual ledger writes

**Files:**
- Modify: `src/lingtai/core/daemon/run_dir.py`
- Modify: `tests/test_daemon_run_dir.py`

Per-call token accounting: write to daemon's own `logs/token_ledger.jsonl` AND to parent's `logs/token_ledger.jsonl` with `extra={source, em_id, run_id}` tagging. Update running totals in `daemon.json`.

- [ ] **Step 5.1: Add failing tests**

Append to `tests/test_daemon_run_dir.py`:

```python
def test_append_tokens_writes_daemon_ledger(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    line = rd.token_ledger_path.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["input"] == 100
    assert entry["output"] == 20
    assert entry["thinking"] == 5
    assert entry["cached"] == 10
    assert "ts" in entry
    # daemon's own ledger has no source tag (it's already daemon-scoped by location)
    assert "source" not in entry


def test_append_tokens_writes_parent_ledger_tagged(tmp_path):
    rd = _make_run_dir(tmp_path, parent_addr="researcher")
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    parent_ledger = tmp_path / "parent" / "logs" / "token_ledger.jsonl"
    line = parent_ledger.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["input"] == 100
    assert entry["source"] == "daemon"
    assert entry["em_id"] == "em-3"
    assert entry["run_id"] == rd.run_id


def test_append_tokens_updates_running_totals(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    rd.append_tokens(input=50, output=15, thinking=3, cached=5)
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["tokens"] == {"input": 150, "output": 35, "thinking": 8, "cached": 15}


def test_append_tokens_skipped_when_all_zero(tmp_path):
    """Don't write a noise entry if the LLM call returned zero tokens."""
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=0, output=0, thinking=0, cached=0)
    assert not rd.token_ledger_path.exists() or rd.token_ledger_path.read_text() == ""
    parent_ledger = tmp_path / "parent" / "logs" / "token_ledger.jsonl"
    assert not parent_ledger.exists() or parent_ledger.read_text() == ""


def test_summing_parent_ledger_includes_daemon_spend(tmp_path):
    """sum_token_ledger on parent's ledger sums daemon and parent calls together."""
    from lingtai_kernel.token_ledger import append_token_entry, sum_token_ledger
    rd = _make_run_dir(tmp_path)
    parent_ledger = tmp_path / "parent" / "logs" / "token_ledger.jsonl"
    # Parent's own call
    append_token_entry(parent_ledger, input=200, output=40, thinking=10, cached=20)
    # Daemon call
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    totals = sum_token_ledger(parent_ledger)
    assert totals["input_tokens"] == 300
    assert totals["output_tokens"] == 60
    assert totals["api_calls"] == 2
```

- [ ] **Step 5.2: Run tests; expect failure**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py::test_append_tokens_writes_daemon_ledger -v
```

Expected: FAIL — `append_tokens` not defined.

- [ ] **Step 5.3: Implement `append_tokens`**

Add to `src/lingtai/core/daemon/run_dir.py` inside the `DaemonRunDir` class:

```python
    # ------------------------------------------------------------------
    # Token accounting — dual ledger writes
    # ------------------------------------------------------------------

    def append_tokens(self, *, input: int, output: int,
                     thinking: int, cached: int) -> None:
        """Record per-call token usage to both ledgers.

        Daemon's own logs/token_ledger.jsonl gets an untagged entry (the
        location is already attribution enough). Parent's logs/token_ledger.jsonl
        gets a tagged entry with source/em_id/run_id so future analytics can
        decompose, while existing sum_token_ledger callers continue to count
        daemon spend in the parent's lifetime totals (they only read the
        numeric fields).

        Skips both writes if all four values are zero — avoids ledger noise
        from LLM calls that returned no usage.

        Each write is independently fault-tolerant — if the parent's ledger
        write fails, the daemon's local ledger is still authoritative.
        """
        if not (input or output or thinking or cached):
            return

        # Update running totals in daemon.json
        def _update_state():
            self._state["tokens"]["input"] += input
            self._state["tokens"]["output"] += output
            self._state["tokens"]["thinking"] += thinking
            self._state["tokens"]["cached"] += cached
            self._atomic_write_json(self.daemon_json_path, self._state)
        self._safe("append_tokens.state", _update_state)

        # Daemon's own ledger — no tag needed (location attributes it)
        self._safe(
            "append_tokens.daemon_ledger",
            lambda: append_token_entry(
                self.token_ledger_path,
                input=input, output=output,
                thinking=thinking, cached=cached,
            ),
        )

        # Parent's ledger — tagged for attribution
        self._safe(
            "append_tokens.parent_ledger",
            lambda: append_token_entry(
                self._parent_token_ledger,
                input=input, output=output,
                thinking=thinking, cached=cached,
                extra={"source": "daemon", "em_id": self._handle,
                       "run_id": self._run_id},
            ),
        )
```

- [ ] **Step 5.4: Run tests; expect pass**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -v
```

Expected: 27 passed (22 prior + 5 new).

- [ ] **Step 5.5: Commit**

```bash
git add src/lingtai/core/daemon/run_dir.py tests/test_daemon_run_dir.py
git commit -m "feat(daemon): DaemonRunDir.append_tokens — dual ledger writes

Per-call: write to daemon's own logs/token_ledger.jsonl untagged,
write to parent's logs/token_ledger.jsonl with extra={source,em_id,run_id},
update running totals in daemon.json. Each write independently
fault-tolerant. Skips if all four values are zero."
```

---

### Task 6: Terminal markers (`mark_done`, `mark_failed`, `mark_cancelled`, `mark_timeout`)

**Files:**
- Modify: `src/lingtai/core/daemon/run_dir.py`
- Modify: `tests/test_daemon_run_dir.py`

End-of-life writes: set terminal `state`, `finished_at`, `result_preview` or `error`, log a terminal event.

- [ ] **Step 6.1: Add failing tests**

Append to `tests/test_daemon_run_dir.py`:

```python
def test_mark_done_writes_terminal_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_done("Task done. Found 3 TODOs.")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "done"
    assert data["finished_at"] is not None
    assert data["result_preview"] == "Task done. Found 3 TODOs."
    assert data["error"] is None


def test_mark_done_truncates_result_preview(tmp_path):
    rd = _make_run_dir(tmp_path)
    long_text = "a" * 500
    rd.mark_done(long_text)
    data = json.loads(rd.daemon_json_path.read_text())
    assert len(data["result_preview"]) <= 200


def test_mark_done_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_done("ok")
    lines = rd.events_path.read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["event"] == "daemon_done"
    assert "elapsed_s" in last


def test_mark_failed_records_error(tmp_path):
    rd = _make_run_dir(tmp_path)
    exc = RuntimeError("boom")
    rd.mark_failed(exc)
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "failed"
    assert data["finished_at"] is not None
    assert data["error"]["type"] == "RuntimeError"
    assert data["error"]["message"] == "boom"
    assert data["result_preview"] is None


def test_mark_failed_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_failed(ValueError("bad"))
    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "daemon_error"
    assert last["exception"] == "ValueError"


def test_mark_cancelled_writes_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_cancelled()
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "cancelled"
    assert data["finished_at"] is not None
    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "daemon_cancelled"


def test_mark_timeout_writes_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_timeout()
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "timeout"
    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "daemon_timeout"


def test_terminal_markers_idempotent_safe(tmp_path):
    """Calling a terminal marker twice does not crash (defensive)."""
    rd = _make_run_dir(tmp_path)
    rd.mark_done("first")
    rd.mark_done("second")  # should not raise
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["result_preview"] == "second"  # last write wins
```

- [ ] **Step 6.2: Run tests; expect failure**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -k "mark_" -v
```

Expected: FAIL — `mark_done` not defined.

- [ ] **Step 6.3: Implement terminal markers**

Add to `src/lingtai/core/daemon/run_dir.py` inside the `DaemonRunDir` class:

```python
    # ------------------------------------------------------------------
    # Terminal markers
    # ------------------------------------------------------------------

    _RESULT_PREVIEW_MAX = 200

    def mark_done(self, text: str) -> None:
        """Normal completion. Sets state=done, finished_at, result_preview."""
        def _write():
            self._state["state"] = "done"
            self._state["finished_at"] = self._now_iso()
            self._state["elapsed_s"] = self._now_secs()
            self._state["current_tool"] = None
            preview = text or ""
            if len(preview) > self._RESULT_PREVIEW_MAX:
                preview = preview[:self._RESULT_PREVIEW_MAX]
            self._state["result_preview"] = preview
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": "daemon_done",
                    "elapsed_s": self._state["elapsed_s"],
                    "ts": self._now_iso(),
                },
            )
        self._safe("mark_done", _write)

    def mark_failed(self, exc: BaseException) -> None:
        """Exception in run loop. Sets state=failed, error.{type, message}."""
        def _write():
            self._state["state"] = "failed"
            self._state["finished_at"] = self._now_iso()
            self._state["elapsed_s"] = self._now_secs()
            self._state["current_tool"] = None
            self._state["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": "daemon_error",
                    "exception": type(exc).__name__,
                    "message": str(exc),
                    "ts": self._now_iso(),
                },
            )
        self._safe("mark_failed", _write)

    def mark_cancelled(self) -> None:
        """Cancel event observed. Sets state=cancelled."""
        self._mark_terminal("cancelled", "daemon_cancelled")

    def mark_timeout(self) -> None:
        """Watchdog timeout. Sets state=timeout."""
        self._mark_terminal("timeout", "daemon_timeout")

    def _mark_terminal(self, state: str, event: str) -> None:
        def _write():
            self._state["state"] = state
            self._state["finished_at"] = self._now_iso()
            self._state["elapsed_s"] = self._now_secs()
            self._state["current_tool"] = None
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": event,
                    "elapsed_s": self._state["elapsed_s"],
                    "ts": self._now_iso(),
                },
            )
        self._safe(f"mark_{state}", _write)
```

- [ ] **Step 6.4: Run tests; expect pass**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -v
```

Expected: 35 passed (27 prior + 8 new).

- [ ] **Step 6.5: Commit**

```bash
git add src/lingtai/core/daemon/run_dir.py tests/test_daemon_run_dir.py
git commit -m "feat(daemon): DaemonRunDir terminal markers

mark_done/mark_failed/mark_cancelled/mark_timeout — each updates
daemon.json (state, finished_at, result_preview/error) atomically
and logs a terminal event. result_preview truncated at 200 chars."
```

---

### Task 7: Robustness tests (atomicity + best-effort failures)

**Files:**
- Modify: `tests/test_daemon_run_dir.py`

Verify atomic-replace doesn't leave partial state and that OSError in mutation methods doesn't propagate.

- [ ] **Step 7.1: Add atomicity tests**

Append to `tests/test_daemon_run_dir.py`:

```python
def test_atomic_write_no_partial_state_on_replace_failure(tmp_path, monkeypatch):
    """If os.replace raises mid-flight, the prior daemon.json remains valid."""
    rd = _make_run_dir(tmp_path)
    initial_data = json.loads(rd.daemon_json_path.read_text())

    # Simulate replace failure on next bump_turn
    real_replace = os.replace
    call_count = [0]

    def failing_replace(src, dst):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("simulated")
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", failing_replace)
    rd.bump_turn(turn=99, response_text="should not land")

    # daemon.json must still be valid JSON with prior contents
    data = json.loads(rd.daemon_json_path.read_text())
    assert data == initial_data
    assert data["turn"] == 0  # prior value preserved


def test_oserror_in_mutation_does_not_raise(tmp_path):
    """Best-effort policy: OSError swallowed, run continues."""
    rd = _make_run_dir(tmp_path)
    # Make logs/ unwritable
    logs_dir = rd.path / "logs"
    logs_dir.chmod(0o500)
    try:
        # Should not raise
        rd.set_current_tool("read", {})
        rd.clear_current_tool(result_status="ok")
        rd.append_tokens(input=10, output=5, thinking=2, cached=1)
    finally:
        logs_dir.chmod(0o700)


def test_chat_history_jsonl_lines_parseable(tmp_path):
    """All lines in chat_history.jsonl are valid JSON."""
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("task", kind="task")
    rd.bump_turn(turn=1, response_text="response")
    rd.record_user_send("more", kind="followup")
    rd.bump_turn(turn=2, response_text="another")
    for line in rd.chat_path.read_text().splitlines():
        assert json.loads(line)  # parses without error


def test_events_jsonl_lines_parseable(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"a": 1})
    rd.clear_current_tool(result_status="ok")
    rd.mark_done("ok")
    for line in rd.events_path.read_text().splitlines():
        assert json.loads(line)


def test_token_ledger_lines_parseable(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=10, output=5, thinking=2, cached=1)
    rd.append_tokens(input=20, output=8, thinking=3, cached=4)
    for line in rd.token_ledger_path.read_text().splitlines():
        assert json.loads(line)
```

Add `import os` to the file's imports if not already present.

- [ ] **Step 7.2: Run robustness tests; expect pass**

```bash
.venv/bin/pytest tests/test_daemon_run_dir.py -v
```

Expected: 40 passed (35 prior + 5 new). The `_safe()` wrapper catches OSError; `_atomic_write_json` uses `os.replace` so a failed replace leaves the prior file intact.

- [ ] **Step 7.3: Commit**

```bash
git add tests/test_daemon_run_dir.py
git commit -m "test(daemon): DaemonRunDir robustness — atomicity + best-effort

Verifies os.replace failure preserves prior daemon.json, OSError in
mutation methods does not propagate (best-effort policy), all JSONL
files contain only valid JSON lines."
```

---

### Task 8: Wire `DaemonRunDir` into `DaemonManager._handle_emanate`

**Files:**
- Modify: `src/lingtai/core/daemon/__init__.py`
- Modify: `tests/test_daemon.py`

`_handle_emanate` constructs the `DaemonRunDir` (folder created on disk) before scheduling the future. Registry entry gains a `"run_dir"` field. The system_prompt — currently built inside `_run_emanation` — is now built in `_handle_emanate` and passed to the run_dir constructor.

- [ ] **Step 8.1: Add an integration test for folder creation at emanate time**

Add to `tests/test_daemon.py` (near the bottom):

```python
def test_emanate_creates_folder_on_disk(tmp_path):
    """_handle_emanate creates daemons/<run_id>/ before the future starts."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "find todos", "tools": ["file"]},
    ]})
    assert result["status"] == "dispatched"

    daemons_dir = agent._working_dir / "daemons"
    assert daemons_dir.is_dir()
    children = list(daemons_dir.iterdir())
    assert len(children) == 1
    folder = children[0]
    # Folder name matches em-1-<YYYYMMDD-HHMMSS>-<6 hex>
    assert re.fullmatch(r"em-1-\d{8}-\d{6}-[0-9a-f]{6}", folder.name)
    # daemon.json exists with state=running and identity fields
    data = json.loads((folder / "daemon.json").read_text())
    assert data["handle"] == "em-1"
    assert data["task"] == "find todos"
    assert data["tools"] == ["file"]
    assert data["state"] == "running"
```

Add `import json` and `import re` to the test file imports if not present.

- [ ] **Step 8.2: Run; expect failure**

```bash
.venv/bin/pytest tests/test_daemon.py::test_emanate_creates_folder_on_disk -v
```

Expected: FAIL — no `daemons/` folder created (manager doesn't construct `DaemonRunDir` yet).

- [ ] **Step 8.3: Refactor `_handle_emanate` to construct `DaemonRunDir`**

In `src/lingtai/core/daemon/__init__.py`:

(a) Add the import at the top of the file (after existing imports):

```python
import os

from .run_dir import DaemonRunDir
```

(b) Replace the body of `_handle_emanate` (lines 276-332) with this version. The change: build the system_prompt and `DaemonRunDir` before submitting the future, and pass `run_dir` (not raw spec fields) into `_run_emanation`:

```python
    def _handle_emanate(self, tasks: list[dict]) -> dict:
        if not tasks:
            return {"status": "error", "message": "No tasks provided"}

        # Clear completed emanations and stale pools
        self._emanations = {k: v for k, v in self._emanations.items()
                            if not v["future"].done()}
        self._pools = [(p, c) for p, c in self._pools if not c.is_set()]

        # Capacity check against pruned registry
        running = len(self._emanations)
        if running + len(tasks) > self._max_emanations:
            lang = self._agent._config.language
            return {"status": "error",
                    "message": t(lang, "daemon.limit_reached",
                                 running=running, requested=len(tasks),
                                 max=self._max_emanations)}

        cancel_event = threading.Event()
        pool = ThreadPoolExecutor(max_workers=len(tasks))
        self._pools.append((pool, cancel_event))

        ids = []
        parent_addr = self._agent._working_dir.name
        parent_pid = os.getpid()

        for spec in tasks:
            em_id = f"em-{self._next_id}"
            self._next_id += 1
            ids.append(em_id)

            # Build tool surface and system prompt up front so the run_dir
            # records the prompt verbatim before any LLM call. Validation
            # (unknown tools) raises here and aborts before scheduling.
            try:
                schemas, dispatch = self._build_tool_surface(spec["tools"])
            except ValueError as e:
                return {"status": "error", "message": str(e)}
            system_prompt = self._build_emanation_prompt(spec["task"], schemas)

            # Construct run_dir — creates folder on disk, writes daemon.json,
            # .prompt, .heartbeat, daemon_start event. If FS construction fails,
            # propagate as a tool-level error and skip scheduling for this spec.
            try:
                run_dir = DaemonRunDir(
                    parent_working_dir=self._agent._working_dir,
                    handle=em_id,
                    task=spec["task"],
                    tools=spec["tools"],
                    model=spec.get("model") or self._default_model,
                    max_turns=self._max_turns,
                    timeout_s=self._timeout,
                    parent_addr=parent_addr,
                    parent_pid=parent_pid,
                    system_prompt=system_prompt,
                )
            except OSError as e:
                return {"status": "error",
                        "message": f"Failed to create daemon folder: {e}"}

            future = pool.submit(
                self._run_emanation,
                em_id, run_dir, schemas, dispatch,
                spec["task"], spec.get("model"), cancel_event,
            )
            future.add_done_callback(
                lambda f, eid=em_id, task=spec["task"]:
                    self._on_emanation_done(eid, task, f)
            )
            self._emanations[em_id] = {
                "future": future,
                "task": spec["task"],
                "start_time": time.time(),
                "cancel_event": cancel_event,
                "followup_buffer": "",
                "followup_lock": threading.Lock(),
                "run_dir": run_dir,
            }

        # Start watchdog
        watchdog = threading.Thread(
            target=self._watchdog, args=(cancel_event, self._timeout),
            daemon=True,
        )
        watchdog.start()

        self._log("daemon_emanate", ids=ids, count=len(tasks),
                  tasks=[{"task": s["task"][:80], "tools": s["tools"]} for s in tasks])

        return {"status": "dispatched", "count": len(tasks), "ids": ids}
```

The `_run_emanation` signature changed (now takes `run_dir, schemas, dispatch` instead of `tools, model`). The next task wires the new signature. For now this commit will leave `_run_emanation` referring to the old signature — that's intentional: tests will fail temporarily and Task 9 fixes them. To keep the suite passing across this commit, **also update `_run_emanation`'s signature** to accept the new args but keep the old body working:

Replace the existing `_run_emanation` signature line:

```python
    def _run_emanation(self, em_id: str, task: str, tool_names: list[str],
                       model: str | None, cancel_event: threading.Event) -> str:
```

with:

```python
    def _run_emanation(self, em_id: str, run_dir, schemas, dispatch,
                       task: str, model: str | None,
                       cancel_event: threading.Event) -> str:
```

And replace the first lines of the body (currently `schemas, dispatch = self._build_tool_surface(tool_names)` and `system_prompt = self._build_emanation_prompt(task, schemas)`) — remove both lines, since callers now pass the values in. So the function starts directly at:

```python
        if cancel_event.is_set():
            return "[cancelled]"

        session = self._agent.service.create_session(
            ...
        )
```

The old token-aggregator logic stays for now — it'll be ripped out in Task 9.

- [ ] **Step 8.4: Run the new test plus existing tests**

```bash
.venv/bin/pytest tests/test_daemon.py -v
```

Expected: many existing tests will now FAIL because they call `_run_emanation` directly with the old signature. **This is expected and Task 9 will fix them.** The new `test_emanate_creates_folder_on_disk` should PASS, plus tests that go through `_handle_emanate` (e.g., `test_handle_emanate_dispatches_and_returns_ids`) should still pass.

The signature mismatch means individual `_run_emanation` direct-call tests fail with `TypeError: _run_emanation() missing required argument` — that's the signal that Task 9 needs to update those tests.

If `test_emanate_creates_folder_on_disk` passes and at least `test_handle_emanate_dispatches_and_returns_ids` still passes, the commit is good.

- [ ] **Step 8.5: Commit**

```bash
git add src/lingtai/core/daemon/__init__.py tests/test_daemon.py
git commit -m "feat(daemon): wire DaemonRunDir into _handle_emanate

Construct DaemonRunDir before scheduling the future — creates
daemons/<run_id>/ on disk with daemon.json, .prompt, .heartbeat,
daemon_start event. Registry entry gains run_dir field. _run_emanation
signature updated to accept (run_dir, schemas, dispatch) so callers
pass the constructed run_dir in. Tool-surface validation now raises
before scheduling.

Note: direct-call _run_emanation tests temporarily fail; fixed in
the next commit which threads run_dir hooks through the loop."
```

---

### Task 9: Thread `DaemonRunDir` hooks through `_run_emanation`

**Files:**
- Modify: `src/lingtai/core/daemon/__init__.py`
- Modify: `tests/test_daemon.py`

Replace the inline token-aggregator logic with `run_dir.append_tokens` per call. Add `record_user_send`/`bump_turn`/`set_current_tool`/`clear_current_tool` calls at the right hook points. Add `mark_done`/`mark_failed`/`mark_cancelled` in the `finally` block.

- [ ] **Step 9.1: Update direct-call tests to pass `run_dir`**

In `tests/test_daemon.py`, each test that calls `mgr._run_emanation(...)` directly needs to construct a `DaemonRunDir` first. Add a helper near `_make_agent`:

```python
def _make_run_dir(agent, em_id="em-test"):
    """Helper: build a DaemonRunDir matching the new _run_emanation signature."""
    from lingtai.core.daemon.run_dir import DaemonRunDir
    return DaemonRunDir(
        parent_working_dir=agent._working_dir,
        handle=em_id,
        task="test task",
        tools=["file"],
        model="mock-model",
        max_turns=30,
        timeout_s=300.0,
        parent_addr=agent._working_dir.name,
        parent_pid=12345,
        system_prompt="You are a daemon.",
    )
```

Update each direct-call test. Pattern: where the old call was

```python
result = mgr._run_emanation(em_id, "task", ["file"], None, cancel)
```

it becomes:

```python
mgr._build_tool_surface  # noqa — referenced below
schemas, dispatch = mgr._build_tool_surface(["file"])
run_dir = _make_run_dir(agent, em_id=em_id)
mgr._emanations[em_id] = {
    "followup_buffer": "",
    "followup_lock": threading.Lock(),
    "run_dir": run_dir,
}
result = mgr._run_emanation(em_id, run_dir, schemas, dispatch, "task", None, cancel)
```

Apply this transformation to:
- `test_run_emanation_returns_text`
- `test_run_emanation_dispatches_tools`
- `test_run_emanation_respects_cancel_before_first_send`
- `test_run_emanation_respects_cancel_mid_loop`

Also: where existing tests construct `_emanations` entries by hand (e.g., `test_handle_ask_*`, `test_handle_list_shows_status`, `test_handle_reclaim_cancels_all`, `test_handle_emanate_rejects_over_limit`), add `"run_dir": None` to the dict so the registry shape is consistent (None is acceptable here — `_handle_list`/`_handle_ask`/`_handle_reclaim` don't dereference run_dir).

For `mock_resp` MagicMocks: ensure `.usage` is set with zero tokens so `append_tokens` skips:

```python
mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                            thinking_tokens=0, cached_tokens=0)
```

Apply this fix wherever a test creates a `mock_resp` for use with `session.send`.

- [ ] **Step 9.2: Refactor `_run_emanation` body**

In `src/lingtai/core/daemon/__init__.py`, replace the `_run_emanation` body (lines roughly 175-258, the entire current implementation) with:

```python
    def _run_emanation(self, em_id: str, run_dir, schemas, dispatch,
                       task: str, model: str | None,
                       cancel_event: threading.Event) -> str:
        """Run a single emanation's tool loop. Called in a worker thread.

        run_dir is the DaemonRunDir constructed in _handle_emanate. All
        filesystem effects flow through it.
        """
        if cancel_event.is_set():
            run_dir.mark_cancelled()
            return "[cancelled]"

        session = self._agent.service.create_session(
            system_prompt=run_dir.prompt_path.read_text(),
            tools=schemas or None,
            model=model or self._default_model,
            thinking="default",
            tracked=False,
        )

        def _accum(resp):
            u = resp.usage
            run_dir.append_tokens(
                input=u.input_tokens,
                output=u.output_tokens,
                thinking=u.thinking_tokens,
                cached=u.cached_tokens,
            )

        try:
            run_dir.record_user_send(task, kind="task")
            response = session.send(task)
            _accum(response)
            turns = 0
            run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")

            while response.tool_calls and turns < self._max_turns:
                if cancel_event.is_set():
                    run_dir.mark_cancelled()
                    return "[cancelled]"

                # Intermediate text → notify parent
                if response.text:
                    self._notify_parent(em_id, response.text)

                tool_results = []
                for tc in response.tool_calls:
                    handler = dispatch.get(tc.name)
                    if handler is None:
                        run_dir.set_current_tool(tc.name, tc.args or {})
                        result = {"status": "error", "message": f"Unknown tool: {tc.name}"}
                        run_dir.clear_current_tool(result_status="error")
                    else:
                        run_dir.set_current_tool(tc.name, tc.args or {})
                        try:
                            result = handler(tc.args or {})
                            status = "error" if isinstance(result, dict) and result.get("status") == "error" else "ok"
                            run_dir.clear_current_tool(result_status=status)
                        except Exception as e:
                            result = {"status": "error", "message": str(e)}
                            run_dir.clear_current_tool(result_status="error")
                    tool_results.append(
                        self._agent.service.make_tool_result(
                            tc.name, result, tool_call_id=tc.id,
                        )
                    )

                # Tool results are written to chat_history before sending
                run_dir.record_user_send(
                    json.dumps([str(r) for r in tool_results], ensure_ascii=False),
                    kind="tool_results",
                )
                response = session.send(tool_results)
                _accum(response)
                turns += 1
                run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")

                # Inject follow-up as a separate user message — only safe when
                # the response is text-only. If it carries new tool_calls, the
                # canonical interface tail is assistant[tool_calls] and a user
                # message here would violate the pairing invariant.
                if not response.tool_calls:
                    followup = self._drain_followup(em_id)
                    if followup:
                        run_dir.record_user_send(followup, kind="followup")
                        response = session.send(followup)
                        _accum(response)
                        turns += 1
                        run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")

            text = response.text or "[no output]"
            run_dir.mark_done(text)
            return text
        except Exception as e:
            run_dir.mark_failed(e)
            raise
```

(a) Add `import json` to the top of the file if not present (it isn't — current imports show `from concurrent.futures import ThreadPoolExecutor`, etc., but no `json`).

(b) Remove the inline token aggregator at the end of the old body (the `tok_in = tok_out = ...` block and the `finally` that wrote to the parent's ledger). This logic now lives in `DaemonRunDir.append_tokens`.

(c) Remove the now-unused import: `from lingtai_kernel.token_ledger import append_token_entry` — no longer needed in this file.

- [ ] **Step 9.3: Run the full daemon test suite**

```bash
.venv/bin/pytest tests/test_daemon.py tests/test_daemon_run_dir.py -v
```

Expected: all pass. Existing daemon tests now exercise the FS-backed path; folders are created in `tmp_path` for each test.

- [ ] **Step 9.4: Run the entire test suite to confirm no kernel regressions**

```bash
.venv/bin/pytest -x
```

Expected: all pass. Other tests don't exercise the daemon FS layer.

- [ ] **Step 9.5: Smoke-test imports**

```bash
.venv/bin/python -c "from lingtai.core.daemon import setup, DaemonManager; from lingtai.core.daemon.run_dir import DaemonRunDir; print('ok')"
```

Expected: `ok`. Catches any leftover import errors invisible to diff review (per user CLAUDE.md preference).

- [ ] **Step 9.6: Commit**

```bash
git add src/lingtai/core/daemon/__init__.py tests/test_daemon.py
git commit -m "feat(daemon): thread DaemonRunDir hooks through _run_emanation

record_user_send before each session.send; bump_turn after each response;
set_current_tool/clear_current_tool around each handler dispatch;
append_tokens per response (replaces old end-of-run aggregator);
mark_done/mark_failed/mark_cancelled in terminal paths.

Removes the inline append_token_entry block — token writes now happen
per-call inside DaemonRunDir.append_tokens, which dual-writes to the
daemon's own ledger and the parent's tagged ledger."
```

---

### Task 10: Reset `_next_id` on reclaim; expose `run_id`/`path` in `_handle_list`

**Files:**
- Modify: `src/lingtai/core/daemon/__init__.py`
- Modify: `tests/test_daemon.py`

Two small spec-conformance fixes:

1. **`_next_id` reset on reclaim.** The spec says handles reset to 1 on reclaim. Today `_next_id` is monotonic — `_handle_reclaim` clears `_emanations` but not the counter. Add the reset.
2. **`_handle_list` returns `run_id` and `path`.** So an inspecting agent knows where to look on disk.

- [ ] **Step 10.1: Add failing tests**

Append to `tests/test_daemon.py`:

```python
def test_reclaim_resets_next_id_to_1(tmp_path):
    """After reclaim, the next emanate gets em-1 again. Folder timestamps disambiguate."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    r1 = mgr.handle({"action": "emanate", "tasks": [{"task": "a", "tools": ["file"]}]})
    assert r1["ids"] == ["em-1"]
    time.sleep(0.5)
    mgr.handle({"action": "reclaim"})
    r2 = mgr.handle({"action": "emanate", "tasks": [{"task": "b", "tools": ["file"]}]})
    assert r2["ids"] == ["em-1"]  # handle reused after reclaim


def test_reclaim_preserves_folders(tmp_path):
    """reclaim stops processes but leaves daemon folders on disk."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    mgr.handle({"action": "emanate", "tasks": [{"task": "a", "tools": ["file"]}]})
    time.sleep(0.5)
    daemons_dir = agent._working_dir / "daemons"
    folders_before = list(daemons_dir.iterdir())
    assert len(folders_before) == 1

    mgr.handle({"action": "reclaim"})
    folders_after = list(daemons_dir.iterdir())
    assert folders_after == folders_before  # same folder still there


def test_handle_list_includes_run_id_and_path(tmp_path):
    """list output exposes run_id and path so inspectors know where to read."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "running"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    mgr.handle({"action": "emanate", "tasks": [{"task": "x", "tools": ["file"]}]})
    time.sleep(0.5)
    listing = mgr._handle_list()
    assert len(listing["emanations"]) >= 1
    em = listing["emanations"][0]
    assert "run_id" in em
    assert "path" in em
    assert em["run_id"].startswith("em-1-")
    assert em["path"].endswith(em["run_id"])
```

Note `test_handle_list_shows_status` (existing) needs to be updated similarly — its in-memory entries lack `run_dir`, and the new `_handle_list` will reference it. Where the test does:

```python
mgr._emanations = {
    "em-1": {"future": done_future, "task": "task A", "start_time": ...},
    ...
}
```

change to:

```python
mgr._emanations = {
    "em-1": {"future": done_future, "task": "task A",
             "start_time": time.time() - 10,
             "cancel_event": threading.Event(), "run_dir": None},
    ...
}
```

And update its asserts to check that when `run_dir is None`, `run_id` and `path` are absent or set to `None`.

- [ ] **Step 10.2: Run; expect failure**

```bash
.venv/bin/pytest tests/test_daemon.py::test_reclaim_resets_next_id_to_1 tests/test_daemon.py::test_handle_list_includes_run_id_and_path -v
```

Expected: FAIL — `_next_id` not reset, `run_id`/`path` not in list output.

- [ ] **Step 10.3: Implement the two fixes**

In `src/lingtai/core/daemon/__init__.py`:

(a) `_handle_reclaim` — add `_next_id` reset. Replace the existing method with:

```python
    def _handle_reclaim(self) -> dict:
        cancelled = sum(1 for e in self._emanations.values()
                        if not e["future"].done())
        for pool, cancel in self._pools:
            cancel.set()
            pool.shutdown(wait=False, cancel_futures=True)
        self._pools.clear()
        self._emanations.clear()
        self._next_id = 1  # handles can be re-used; folder names disambiguate
        self._log("daemon_reclaim", cancelled_count=cancelled)
        return {"status": "reclaimed", "cancelled": cancelled}
```

(b) `_handle_list` — include `run_id` and `path` when run_dir is present. Replace with:

```python
    def _handle_list(self) -> dict:
        emanations = []
        running = 0
        for em_id, entry in self._emanations.items():
            elapsed = time.time() - entry["start_time"]
            future = entry["future"]
            if future.done():
                exc = future.exception()
                if exc:
                    status = "failed"
                else:
                    status = "done"
            else:
                status = "running"
                running += 1
                exc = None
            info = {"id": em_id, "task": entry["task"][:80],
                    "status": status, "elapsed_s": round(elapsed)}
            if status == "failed" and exc:
                info["error"] = str(exc)
            run_dir = entry.get("run_dir")
            if run_dir is not None:
                info["run_id"] = run_dir.run_id
                info["path"] = str(run_dir.path)
            emanations.append(info)
        return {
            "emanations": emanations,
            "running": running,
            "max_emanations": self._max_emanations,
        }
```

- [ ] **Step 10.4: Run; expect pass**

```bash
.venv/bin/pytest tests/test_daemon.py -v
```

Expected: all pass. (The original `test_handle_list_shows_status` should pass with the run_dir=None update.)

- [ ] **Step 10.5: Commit**

```bash
git add src/lingtai/core/daemon/__init__.py tests/test_daemon.py
git commit -m "feat(daemon): reclaim resets _next_id; list returns run_id+path

Reclaim now resets _next_id to 1 (handles re-usable; folder timestamps
disambiguate runs). _handle_list output includes run_id and path so
inspecting agents know where to read forensic state on disk."
```

---

### Task 11: End-to-end integration test — full lifecycle on disk

**Files:**
- Modify: `tests/test_daemon.py`

One holistic test that exercises the entire pipeline: emanate → daemon runs → tokens written to both ledgers → daemon.json reaches state=done → folder preserved after reclaim.

- [ ] **Step 11.1: Add the integration test**

Append to `tests/test_daemon.py`:

```python
def test_e2e_emanate_writes_full_fs_artifact(tmp_path):
    """Full lifecycle: emanate → tool dispatch → completion → forensic folder."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    # Two LLM rounds: first emits a tool call, second completes.
    tc = ToolCall(name="read", args={"file_path": "/tmp/x"}, id="tc-1")
    resp1 = MagicMock()
    resp1.text = "Checking..."
    resp1.tool_calls = [tc]
    resp1.usage = MagicMock(input_tokens=100, output_tokens=20,
                             thinking_tokens=5, cached_tokens=10)
    resp2 = MagicMock()
    resp2.text = "Task done. Found 3 TODOs."
    resp2.tool_calls = []
    resp2.usage = MagicMock(input_tokens=80, output_tokens=15,
                             thinking_tokens=3, cached_tokens=5)

    mock_session = MagicMock()
    mock_session.send = MagicMock(side_effect=[resp1, resp2])
    agent.service.create_session = MagicMock(return_value=mock_session)
    agent.service.make_tool_result = MagicMock(return_value="mock_result")
    agent._tool_handlers["read"] = MagicMock(return_value={"content": "file text"})

    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "find TODOs", "tools": ["file"]},
    ]})
    assert result["status"] == "dispatched"
    em_id = result["ids"][0]

    # Wait for completion
    time.sleep(2.0)

    # Find the folder
    daemons_dir = agent._working_dir / "daemons"
    folders = list(daemons_dir.iterdir())
    assert len(folders) == 1
    folder = folders[0]

    # daemon.json shows terminal state with full info
    data = json.loads((folder / "daemon.json").read_text())
    assert data["state"] == "done"
    assert data["finished_at"] is not None
    assert data["task"] == "find TODOs"
    assert data["tool_call_count"] == 1
    assert data["result_preview"] == "Task done. Found 3 TODOs."
    assert data["tokens"]["input"] == 180
    assert data["tokens"]["output"] == 35

    # chat_history.jsonl has user+assistant entries across both rounds
    chat_lines = (folder / "history" / "chat_history.jsonl").read_text().splitlines()
    assert len(chat_lines) >= 4  # task + assistant1 + tool_results + assistant2
    chat_entries = [json.loads(line) for line in chat_lines]
    assert any(e["role"] == "user" and e["kind"] == "task" for e in chat_entries)
    assert any(e["role"] == "assistant" and "Found 3 TODOs" in e["text"] for e in chat_entries)

    # events.jsonl has daemon_start, tool_call, tool_result, daemon_done
    events = [json.loads(line) for line in (folder / "logs" / "events.jsonl").read_text().splitlines()]
    event_types = [e["event"] for e in events]
    assert "daemon_start" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "daemon_done" in event_types

    # Daemon's own token ledger has 2 entries
    daemon_ledger = (folder / "logs" / "token_ledger.jsonl").read_text().splitlines()
    assert len(daemon_ledger) == 2

    # Parent's ledger has the same 2 entries, tagged
    parent_ledger_path = agent._working_dir / "logs" / "token_ledger.jsonl"
    parent_lines = parent_ledger_path.read_text().splitlines()
    daemon_tagged = [json.loads(line) for line in parent_lines
                     if json.loads(line).get("source") == "daemon"]
    assert len(daemon_tagged) == 2
    assert all(e["em_id"] == em_id for e in daemon_tagged)

    # Reclaim does not touch folder
    mgr.handle({"action": "reclaim"})
    assert folder.is_dir()
    # daemon.json still readable, still state=done (reclaim doesn't rewrite completed daemons)
    data_after = json.loads((folder / "daemon.json").read_text())
    assert data_after["state"] == "done"
```

- [ ] **Step 11.2: Run; expect pass**

```bash
.venv/bin/pytest tests/test_daemon.py::test_e2e_emanate_writes_full_fs_artifact -v
```

Expected: PASS.

- [ ] **Step 11.3: Run the full daemon suite**

```bash
.venv/bin/pytest tests/test_daemon.py tests/test_daemon_run_dir.py tests/test_token_ledger.py -v
```

Expected: all pass.

- [ ] **Step 11.4: Commit**

```bash
git add tests/test_daemon.py
git commit -m "test(daemon): end-to-end FS artifact assertion

Single test that verifies the full lifecycle: emanate → 2-round
tool loop → folder created with daemon.json (state=done, tokens
accumulated, result_preview), chat_history (user+assistant lines),
events.jsonl (start/tool/done), daemon ledger (2 entries), parent
ledger (2 tagged entries), folder preserved after reclaim."
```

---

### Task 12: Populate the daemon manual SKILL.md

**Files:**
- Modify: `src/lingtai/core/daemon/manual/SKILL.md`

The placeholder gets real content: folder layout reference, inspection patterns, worked examples.

- [ ] **Step 12.1: Replace the placeholder content**

Replace the entire content of `src/lingtai/core/daemon/manual/SKILL.md` with:

```markdown
---
name: daemon-manual
description: Reference manual for the `daemon` tool — debugging, inspection patterns, and worked examples for the filesystem-backed emanation log surface.
version: 0.2.0
---

# daemon manual

The `daemon` tool's schema description covers the happy path. This manual is the deeper reference: how to inspect a slow or failed emanation, the on-disk artifact layout, and worked examples.

## Each emanation is a forensic mini-avatar

Every time you call `daemon(action="emanate", tasks=[...])`, each task gets a working folder under `daemons/` in your own directory. The folder is named:

    daemons/em-<N>-<YYYYMMDD-HHMMSS>-<6 hex>/

where `em-<N>` is the in-context handle (e.g. `em-3`). The handle resets to `em-1` after `reclaim`, but the timestamp+hash means historical folders never collide. **Folders persist forever** — `reclaim` only stops processes, not files. They're cleaned up incidentally when you molt (which wipes the working directory).

This means: when an emanation looks stuck, you can read its actual state instead of guessing. Don't kill it on a hunch — inspect first.

## Folder layout

```
daemons/em-3-20260427-094215-a1b2c3/
├── daemon.json                  ← identity card + live status snapshot
├── .prompt                      ← system prompt as built (forensic)
├── .heartbeat                   ← mtime touched on every write
├── history/
│   └── chat_history.jsonl       ← full LLM transcript
└── logs/
    ├── token_ledger.jsonl       ← per-call token usage
    └── events.jsonl             ← daemon_start, tool_call, tool_result, daemon_done/...
```

## Inspection patterns

### "Is this emanation actually doing anything?"

Read `daemon.json` once. The fields you want:

- `state` — `running` / `done` / `failed` / `cancelled` / `timeout`
- `current_tool` — `"read"` / `"bash"` / null. If null while `state=running`, the emanation is waiting on the LLM. If non-null, it's executing that tool.
- `turn` — which LLM round the emanation is on
- `tool_call_count` — how many tool dispatches it has done
- `tokens` — running totals
- `elapsed_s` — wall clock since start

If `current_tool` is null AND `tool_call_count` hasn't changed for a while, the LLM is thinking — wait. If `current_tool` is set and stays set, that tool is slow (e.g., a big file read or a long bash command).

### "What has it figured out so far?"

Tail `history/chat_history.jsonl`. Each line is one role/turn entry:

- `{role: "user", kind: "task"}` — the original task
- `{role: "assistant", text: "..."}` — what the emanation said
- `{role: "user", kind: "tool_results"}` — what the tools returned
- `{role: "user", kind: "followup"}` — your `daemon(action="ask", ...)` messages

Read the most recent assistant text to see the latest progress narrative.

### "What did it spend?"

Either of:
- `daemon.json` field `tokens` — running totals across the whole run
- `logs/token_ledger.jsonl` — per-call entries, sortable by line

The same per-call entries are also in your own `logs/token_ledger.jsonl` (the parent's), tagged with `source: "daemon"` and `em_id`. Your lifetime token totals (what `sum_token_ledger` reports) include all daemon spend.

### "Why did it fail?"

Read `daemon.json`'s `error` field — `{type, message}`. For more depth, tail `logs/events.jsonl` for the `daemon_error` event and look at the preceding `tool_call`/`tool_result` entries to see what was happening just before the failure.

## Worked example: a daemon that's been running 5 minutes

You called `daemon(action="emanate", ...)` for `em-3`, asked it to "scan src/ for security issues", and it's been running 5 minutes. You're nervous.

```bash
# What's the live state?
read("daemons/em-3-20260427-094215-abc123/daemon.json")
# → state=running, turn=8, current_tool=null, tool_call_count=15, tokens.input=22000

# Last few lines of the transcript
bash("tail -n 20 daemons/em-3-20260427-094215-abc123/history/chat_history.jsonl")
# → assistant: "Found a potential SQL injection in db.py:42. Continuing..."

# Recent tool activity
bash("tail -n 10 daemons/em-3-20260427-094215-abc123/logs/events.jsonl")
# → series of read/grep events on src/db/, src/auth/
```

That's a healthy pattern: the LLM is between tool calls, has good progress narrative, and is steadily working through files. **Don't reclaim.** Let it cook.

## API note: `daemon(action="list")`

`list` reports only currently-active emanations (in-memory registry). It includes `run_id` and `path` so you know where to read on disk. Historical (completed/failed/cancelled) emanations don't appear in `list` — find them with `bash("ls daemons/")` instead.

## What the manual does NOT cover

- Provider routing / LLM presets — deferred to a separate spec.
- Cross-process recovery — if your kernel restarted mid-daemon, the folder may show `state=running` indefinitely. Compare `now()` vs `.heartbeat` mtime to detect orphans.
- Folder cleanup — there is none. Molts wipe the working dir. For non-molting agents, you may eventually want to `rm -rf daemons/em-*-2026-04-*` manually.
```

- [ ] **Step 12.2: Verify the file parses as valid markdown frontmatter**

```bash
.venv/bin/python -c "
import re
content = open('src/lingtai/core/daemon/manual/SKILL.md').read()
m = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
assert m, 'frontmatter missing'
print('frontmatter ok')
print(m.group(1))
"
```

Expected: prints frontmatter block.

- [ ] **Step 12.3: Commit**

```bash
git add src/lingtai/core/daemon/manual/SKILL.md
git commit -m "docs(daemon): populate manual with FS layout + inspection patterns

Replaces placeholder with full reference: folder layout, fields-by-purpose
in daemon.json, inspection patterns by question, worked example of a
slow-running emanation. Covers what the schema description can't carry."
```

---

### Task 13: i18n updates — surface FS visibility in the daemon tool description

**Files:**
- Modify: `src/lingtai/i18n/en.json`
- Modify: `src/lingtai/i18n/zh.json`
- Modify: `src/lingtai/i18n/wen.json`

The current `daemon.description` says results are mailed back via inbox notifications. Add a sentence about the FS folder so agents know they can inspect when worried.

- [ ] **Step 13.1: Update `en.json`**

In `src/lingtai/i18n/en.json`, find the existing `"daemon.description"` key and replace its value with:

```
"daemon.description": "Daemon (神識) — delegate work to ephemeral subagents (emanations) for context isolation. Each emanation is a disposable LLM session with its own context window; it shares your working directory but retains NO memory after completion. Use daemon to keep noisy, context-heavy work out of your own context: large file scans, exploratory searches, multi-step research, batch transformations — anything where you only need the conclusion. Max 4 concurrent by default (configurable per agent in init.json; daemon(action='list') reports the actual cap). For tasks that need persistent memory or learning across sessions, use avatar (分身) instead — daemon is fire-and-forget. IMPORTANT: emanation results are truncated to ~2000 chars. If you need detailed output, instruct the emanation to write a report to a file, then read the file yourself. Actions: emanate (分, dispatch batch), list (观, check status), ask (问, follow-up), reclaim (收, kill all).\nFILESYSTEM: Each emanation creates a forensic folder at daemons/<run_id>/ with daemon.json (live status), history/chat_history.jsonl (transcript), logs/{token_ledger,events}.jsonl. Read these instead of killing a slow emanation prematurely — see the daemon-manual skill for inspection patterns. Folders persist forever (cleaned up by molts).\nPRIVACY: Emanation IDs (em-N) are private to your daemon context — other agents cannot use them. Never share emanation IDs with peers. If an emanation produces useful results, share the actual content via email or write it to a file."
```

- [ ] **Step 13.2: Update `zh.json` — Chinese mirror**

In `src/lingtai/i18n/zh.json`, find `"daemon.description"` and append the FS paragraph in Chinese. Locate the existing description, then add right before the `\nPRIVACY:` (or `\n隐私:` etc.) section a translated FILESYSTEM paragraph. The exact existing zh string varies by current state of the file — read it first, then edit. The conceptual content to add:

```
\n文件系统：每个分身在 daemons/<run_id>/ 下创建文件夹，包含 daemon.json（实时状态）、history/chat_history.jsonl（对话记录）、logs/{token_ledger,events}.jsonl（token 与事件日志）。当分身运行缓慢时，读取这些文件而非贸然中止 — 详见 daemon-manual 技能。文件夹永久保留（蜕变时清理）。
```

Run `.venv/bin/python -c "import json; json.load(open('src/lingtai/i18n/zh.json'))"` after the edit to validate JSON.

- [ ] **Step 13.3: Update `wen.json` — Classical Chinese mirror**

Same shape as zh, in classical Chinese register. Locate existing `"daemon.description"` and add an analogous paragraph in 文言. Preserve existing aesthetic register. Validate JSON afterward.

- [ ] **Step 13.4: Verify all three locales parse**

```bash
.venv/bin/python -c "
import json
for loc in ['en', 'zh', 'wen']:
    data = json.load(open(f'src/lingtai/i18n/{loc}.json'))
    assert 'daemon.description' in data
    assert 'daemons/' in data['daemon.description'] or '文件夹' in data['daemon.description'] or 'daemons' in data['daemon.description']
    print(f'{loc}: ok ({len(data[\"daemon.description\"])} chars)')
"
```

Expected: all three print `ok` with their character counts.

- [ ] **Step 13.5: Run the daemon test suite to confirm i18n keys still resolve**

```bash
.venv/bin/pytest tests/test_daemon.py -v
```

Expected: all pass.

- [ ] **Step 13.6: Commit**

```bash
git add src/lingtai/i18n/en.json src/lingtai/i18n/zh.json src/lingtai/i18n/wen.json
git commit -m "i18n(daemon): surface FS visibility in tool description

Adds a FILESYSTEM paragraph to daemon.description across en/zh/wen.
Tells agents that each emanation creates a forensic folder under
daemons/<run_id>/ and to read it before killing a slow emanation.
Points at daemon-manual skill for deeper reference."
```

---

### Task 14: Final integration smoke test + sanity run

**Files:** none touched; verification only.

A final pass to make sure nothing's broken outside the daemon system.

- [ ] **Step 14.1: Run the full kernel test suite**

```bash
.venv/bin/pytest -x
```

Expected: every test passes. The token_ledger change in Task 1 is backward-compatible; no other caller is affected.

- [ ] **Step 14.2: Smoke-test critical imports**

```bash
.venv/bin/python -c "
from lingtai.core.daemon import setup, DaemonManager, get_description, get_schema
from lingtai.core.daemon.run_dir import DaemonRunDir
from lingtai_kernel.token_ledger import append_token_entry, sum_token_ledger
print('imports ok')
"
```

Expected: `imports ok`.

- [ ] **Step 14.3: Smoke-test agent boot with daemon capability**

```bash
.venv/bin/python -c "
from unittest.mock import MagicMock
from pathlib import Path
import tempfile

from lingtai.agent import Agent
from lingtai_kernel.config import AgentConfig

with tempfile.TemporaryDirectory() as tmp:
    svc = MagicMock(provider='mock', model='mock-model')
    svc.create_session = MagicMock()
    agent = Agent(
        svc,
        working_dir=Path(tmp) / 'agent',
        capabilities=['daemon'],
        config=AgentConfig(),
    )
    mgr = agent.get_capability('daemon')
    assert mgr is not None
    print('agent with daemon capability boots ok')
"
```

Expected: `agent with daemon capability boots ok`.

- [ ] **Step 14.4: Verify all spec deliverables are touched**

```bash
git log --oneline c752970..HEAD
```

Expected commits (in order):
1. `feat(token_ledger): add optional extra kwarg for tagged entries`
2. `feat(daemon): add DaemonRunDir skeleton — construction + identity card`
3. `feat(daemon): DaemonRunDir.record_user_send + bump_turn`
4. `feat(daemon): DaemonRunDir tool-dispatch hooks`
5. `feat(daemon): DaemonRunDir.append_tokens — dual ledger writes`
6. `feat(daemon): DaemonRunDir terminal markers`
7. `test(daemon): DaemonRunDir robustness — atomicity + best-effort`
8. `feat(daemon): wire DaemonRunDir into _handle_emanate`
9. `feat(daemon): thread DaemonRunDir hooks through _run_emanation`
10. `feat(daemon): reclaim resets _next_id; list returns run_id+path`
11. `test(daemon): end-to-end FS artifact assertion`
12. `docs(daemon): populate manual with FS layout + inspection patterns`
13. `i18n(daemon): surface FS visibility in tool description`

(13 commits, all of `core/daemon/` plus token_ledger + i18n + manual covered.)

- [ ] **Step 14.5: Final commit (changelog stub, optional)**

If the kernel has a CHANGELOG or release-notes pattern, add an entry summarizing the user-facing change. Otherwise skip.

```bash
# Check for changelog convention
ls CHANGELOG.md docs/CHANGELOG.md 2>/dev/null
```

If a changelog exists, add an entry under the current unreleased section:

```markdown
- **daemon**: emanations are now filesystem-backed mini-avatars. Each
  emanation creates `daemons/em-N-<timestamp>-<hash6>/` with daemon.json
  (live status), chat_history.jsonl, per-daemon token_ledger.jsonl, and
  events.jsonl. Folders persist forever; reclaim only stops processes.
  Parent's token_ledger gets tagged daemon entries for attribution.
  See daemon-manual skill for inspection patterns.
```

If no changelog exists, this step is a no-op.

---

## Self-Review

**Spec coverage check:**

| Spec section | Implemented in |
|---|---|
| Folder layout `daemons/em-N-<ts>-<hash6>/` | Task 2 |
| `daemon.json` schema (16 fields, 5 states) | Tasks 2 (initial), 3, 4, 5, 6 (mutations) |
| Token ledger duplication with tagging | Tasks 1, 5 |
| `record_user_send` / `bump_turn` hooks | Task 3 |
| `set_current_tool` / `clear_current_tool` hooks | Task 4 |
| `append_tokens` dual-ledger | Task 5 |
| Terminal markers (done/failed/cancelled/timeout) | Task 6 |
| Atomic write + best-effort policy | Tasks 2 (helpers), 7 (tests) |
| `_handle_emanate` constructs run_dir | Task 8 |
| `_run_emanation` threads hooks | Task 9 |
| `_handle_reclaim` resets `_next_id`, preserves folders | Task 10 |
| `_handle_list` exposes `run_id` + `path` | Task 10 |
| `tracked=False` invariant preserved | Task 9 (preserved in refactor) |
| End-to-end folder verification | Task 11 |
| Manual SKILL.md populated | Task 12 |
| i18n description surfaces FS visibility | Task 13 |
| Test suite green | Task 14 |

All spec sections covered.

**Placeholder scan:** No "TBD"/"TODO"/"implement later" found. Every step contains either exact code or an exact command with expected output.

**Type consistency check:**
- `DaemonRunDir.__init__` signature: keyword-only after `*`, all 11 fields named consistently across Tasks 2 and 9.
- `DaemonRunDir` properties (`run_id`, `handle`, `path`, `daemon_json_path`, `prompt_path`, `heartbeat_path`, `chat_path`, `events_path`, `token_ledger_path`) used identically in Tasks 2-6 and consumers.
- `append_tokens` signature `(*, input, output, thinking, cached)` matches `append_token_entry` upstream and the kernel's `Usage.{input,output,thinking,cached}_tokens` shape.
- `_run_emanation` signature change `(em_id, run_dir, schemas, dispatch, task, model, cancel_event)` consistent between Task 8 (introduction) and Task 9 (full body).
- `_handle_list` info dict keys (`id`, `task`, `status`, `elapsed_s`, `error`, `run_id`, `path`) consistent in Task 10.

No mismatches.

**Scope check:** This plan implements the FS refactor only. The deferred LLM preset/quiver work is explicitly out of scope and not touched. The `_next_id` reset on reclaim (Task 10) is a small spec-conformance fix, not an unrelated cleanup.
