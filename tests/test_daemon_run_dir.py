"""Pure FS unit tests for DaemonRunDir — no threads, no LLM mocks."""
import json
import re
import time
from pathlib import Path

from lingtai.core.daemon.run_dir import DaemonRunDir


def _make_run_dir(tmp_path: Path, **overrides) -> DaemonRunDir:
    """Helper: construct a DaemonRunDir with sensible defaults."""
    parent_wd = tmp_path / "parent"
    parent_wd.mkdir(exist_ok=True)
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
