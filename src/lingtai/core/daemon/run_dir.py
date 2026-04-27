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

        self._state = {
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

        self._atomic_write_json(self.daemon_json_path, self._state)
        self.prompt_path.write_text(system_prompt, encoding="utf-8")
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
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
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
