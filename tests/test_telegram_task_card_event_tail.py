"""Automatic Task Card as a broadcast projection of the agent's ``events.jsonl``.

The automatic slot mechanically consumes the agent's authoritative
``logs/events.jsonl`` in file order, keeps the most recent N provider-call groups
of canonical ``diary`` text and safe tool fields, and projects
current session telemetry only from the latest final-carrier ``notification_block_injected``,
and broadcasts the same projection to every resident Task Card for the agent
(no per-route correlation — this is an agent-behavior broadcast, not per-chat
visibility).

These tests exercise ``TelegramManager``'s tailer directly: no durable
cursor/checkpoint file, no BaseAgent pre-dispatch/result callback dependency,
no full-file scan on every poll, and restart rehydration from the same
durable file only.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from lingtai.mcp_servers.task_card.event_projection import TaskCardEventProjection
from lingtai.mcp_servers.telegram.manager import TelegramManager
from tests._notification_store_helpers import FakeNotificationStore


class FakeAccount:
    """Mimics the real TelegramAccount API needed for automatic broadcast."""

    def __init__(self, alias="mybot", *, fail_send=False, fail_edit=False):
        self.alias = alias
        self.calls: list = []
        self._task_cards: dict[str, str] = {}
        self._next_id = 100
        self._fail_send = fail_send
        self._fail_edit = fail_edit

    def send_message(self, chat_id, text, reply_to_message_id=None, **kwargs):
        if self._fail_send:
            raise RuntimeError("send failed")
        msg_id = self._next_id
        self._next_id += 1
        self.calls.append(("send_message", chat_id, msg_id, text))
        return {"message_id": msg_id}

    def edit_message(self, chat_id, message_id, text, **kwargs):
        self.calls.append(("edit_message", chat_id, message_id, text))
        if self._fail_edit:
            raise RuntimeError(
                "Telegram API error: Bad Request: message to edit not found"
            )
        return {"ok": True}

    def delete_message(self, chat_id, message_id):
        self.calls.append(("delete_message", chat_id, message_id))
        return {"ok": True}

    def get_task_card(self, chat_id):
        return self._task_cards.get(str(chat_id))

    def set_task_card(self, chat_id, compound_id):
        self._task_cards[str(chat_id)] = compound_id

    def clear_task_card(self, chat_id):
        self._task_cards.pop(str(chat_id), None)

    def list_task_card_chats(self):
        out = []
        for key in self._task_cards:
            try:
                out.append(int(key))
            except (TypeError, ValueError):
                continue
        return out

    def get_last_message_id(self, chat_id):
        return None

    def set_message_reaction(self, *_args, **_kwargs):
        return None


class FakeService:
    def __init__(self, accounts):
        self._accounts = {a.alias: a for a in accounts}
        self._order = [a.alias for a in accounts]
        self.default_account = accounts[0]
        self.normal_rows = 1

    def get_account(self, alias):
        return self._accounts[alias]

    def list_accounts(self):
        return list(self._order)

    def taskcard_enabled(self):
        return True

    def taskcard_normal_rows(self):
        return self.normal_rows


def _manager(tmp_path, *accounts):
    if not accounts:
        accounts = (FakeAccount(),)
    service = FakeService(list(accounts))
    manager = TelegramManager(
        service,
        working_dir=Path(tmp_path),
        on_inbound=lambda _: None,
        notification_store=FakeNotificationStore(),
    )
    # Most projection tests intentionally perform several semantic transitions
    # synchronously; disable wall-clock throttling unless a test explicitly opts
    # into the production interval with a deterministic fake clock.
    manager._TASK_CARD_EVENT_POLL_INTERVAL = 0.0
    return manager, service


def _events_path(tmp_path: Path) -> Path:
    path = Path(tmp_path) / "logs" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_lines(path: Path, lines: list[str]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _tool_call_line(
    tool_name="bash", action="run", reasoning="doing a thing", ts=1.0,
    call_id="c1",
) -> str:
    return json.dumps({
        "type": "tool_call",
        "address": "agent-1",
        "agent_name": "agent-1",
        "ts": ts,
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "tool_trace_id": "t1",
        "tool_args": {"action": action, "_reasoning": reasoning, "secret": "sh"},
    })


def _pre_resident(account: FakeAccount, chat_id: int, manager: TelegramManager) -> None:
    """Seed a resident Task Card target the way an existing account would have one."""
    account.set_task_card(chat_id, f"{account.alias}:{chat_id}:1")


# ---------------------------------------------------------------------------
# Broadcast with no Telegram notification file present at all
# ---------------------------------------------------------------------------


def test_first_real_inbound_establishes_one_resident(tmp_path):
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    manager.on_incoming("mybot", {"message": {
        "message_id": 8, "date": 1781600000,
        "from": {"username": "alice"},
        "chat": {"id": 123, "type": "private"}, "text": "hello",
    }})
    assert acct.get_task_card(123) == "mybot:123:100"
    assert [call[0] for call in acct.calls] == ["send_message"]


def test_broadcast_requires_no_notification_file(tmp_path):
    """The tailer must project from events.jsonl alone, no notification store read."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line()])

    manager._poll_event_tail()

    edits = [c for c in acct.calls if c[0] == "edit_message"]
    assert len(edits) == 1
    assert "bash" in edits[0][3]
    assert "doing a thing" in edits[0][3]


# ---------------------------------------------------------------------------
# Restart rehydration: reverse-tail to find latest N without a durable cursor
# ---------------------------------------------------------------------------


def test_restart_rehydrates_latest_n_from_tail_without_checkpoint_file(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    lines = [
        _tool_call_line(tool_name=f"tool{i}", reasoning=f"reason{i}", call_id=f"c{i}")
        for i in range(5)
    ]
    _write_lines(events_path, lines)

    # No file anywhere except events.jsonl itself carries this state.
    working = Path(tmp_path)
    before = {p for p in working.rglob("*") if p.is_file()}

    manager2, service2 = _manager(tmp_path, acct)
    manager2._init_event_tail()

    after = {p for p in working.rglob("*") if p.is_file()}
    assert after == before, "restart must not create a new durable checkpoint file"

    window = manager2._task_card_event_window()
    # Latest-N (default window) ends with the most recent event.
    assert window[-1]["tool"] == "tool4"
    assert window[-1]["reasoning"] == "reason4"
    acct.calls.clear()
    manager2._broadcast_task_card_event_window()
    assert acct.get_task_card(555) == "mybot:555:1"
    assert [call[0:3] for call in acct.calls] == [("edit_message", 555, 1)]


# ---------------------------------------------------------------------------
# Non-whitelisted / malformed / partial-line rows are skipped safely
# ---------------------------------------------------------------------------


def test_provider_groups_count_calls_and_exclude_unprojected_fields(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)
    service.normal_rows = 2
    visible = "text two " + ("x" * 2000)
    events = [
        {"type": "diary", "api_call_id": "api-1", "text": "text one"},
        {"type": "tool_call", "api_call_id": "api-1", "tool_name": "bash",
         "tool_args": {"action": "run", "_reasoning": "safe one"}},
        {"type": "assistant_text", "api_call_id": "api-1", "text": "ALIAS_SECRET"},
        {"type": "diary", "api_call_id": "api-2", "text": visible},
        {"type": "tool_call", "api_call_id": "api-2", "tool_name": "read",
         "tool_args": {"action": "read", "command": "ARG_SECRET",
                       "_reasoning": "safe two"}},
        {"type": "thinking", "api_call_id": "api-2", "text": "THINKING_SECRET"},
        {"type": "tool_result", "api_call_id": "api-2", "result": "RESULT_SECRET"},
    ]
    _write_lines(_events_path(tmp_path), [json.dumps(event) for event in events])
    manager._poll_event_tail()
    rendered = [call for call in acct.calls if call[0] == "edit_message"][-1][3]
    divider = manager._TASK_CARD_API_CALL_DIVIDER
    api_dividers = [ln for ln in rendered.splitlines()
                    if ln == divider or (ln.startswith(divider + " ") and ln.endswith(" " + divider))]
    footer_idx = next(i for i, ln in enumerate(rendered.splitlines()) if "Don't reply to this Task Card." in ln)
    assert len(api_dividers) == 2
    assert footer_idx == 0
    assert rendered.splitlines()[footer_idx + 1] == TaskCardEventProjection.header("en")
    assert all(value in rendered for value in ("text one", "• bash.run:", "text two", "• read.read:"))
    assert len(rendered) <= manager._TASK_CARD_TEXT_LIMIT
    assert all(secret not in rendered for secret in (
        "ARG_SECRET", "ALIAS_SECRET", "THINKING_SECRET", "RESULT_SECRET",
    ))

    service.normal_rows = 1
    manager._broadcast_task_card_event_window()
    latest = acct.calls[-1][3]
    latest_lines = latest.splitlines()
    latest_api_dividers = [ln for ln in latest_lines
                           if ln == divider or (ln.startswith(divider + " ") and ln.endswith(" " + divider))]
    latest_footer_idx = next(i for i, ln in enumerate(latest_lines) if "Don't reply to this Task Card." in ln)
    assert len(latest_api_dividers) == 1
    assert latest_footer_idx == 0
    assert latest_lines[latest_footer_idx + 1] == TaskCardEventProjection.header("en")
    assert "text two" in latest and "• read.read:" in latest
    assert "text one" not in latest and "• bash.run:" not in latest


def test_non_whitelisted_event_types_are_skipped(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [
        json.dumps({"type": "tool_result", "tool_name": "bash", "ts": 1.0}),
        json.dumps({"type": "llm_call", "ts": 1.0}),
        _tool_call_line(tool_name="only-this-one"),
    ])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert [row["tool"] for row in window] == ["only-this-one"]


def test_malformed_json_line_is_skipped(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [
        "{not valid json",
        _tool_call_line(tool_name="after-garbage"),
    ])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert [row["tool"] for row in window] == ["after-garbage"]


def test_partial_trailing_line_is_not_consumed_until_complete(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="first")])

    manager._poll_event_tail()
    assert [row["tool"] for row in manager._task_card_event_window()] == ["first"]

    # Append a partial line with no trailing newline (simulates a writer mid-write).
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "tool_call", "tool_name": "second",
            "tool_args": {"action": "x", "_reasoning": "y"}, "ts": 2.0,
        })[:20])  # deliberately truncated, no newline

    manager._poll_event_tail()
    # The partial line must not be consumed as a complete row yet.
    assert [row["tool"] for row in manager._task_card_event_window()] == ["first"]

    # Complete the line.
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "tool_call", "tool_name": "second",
            "tool_args": {"action": "x", "_reasoning": "y"}, "ts": 2.0,
        })[20:] + "\n")

    manager._poll_event_tail()
    assert [row["tool"] for row in manager._task_card_event_window()] == ["first", "second"]


def test_startup_rehydrate_does_not_consume_unterminated_final_line(tmp_path):
    """A restart/refresh must not treat the file's in-progress final line as
    already consumed: the forward offset must land at the start of that
    incomplete tail, not at the current EOF, so completion is read as one
    whole row later instead of being silently dropped forever."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="first")])
    complete_size = events_path.stat().st_size

    # Append a partial line with no trailing newline, simulating a writer
    # mid-append at the moment of a manager restart/refresh.
    partial = json.dumps({
        "type": "tool_call", "tool_name": "second",
        "tool_args": {"action": "x", "_reasoning": "y"}, "ts": 2.0,
    })[:20]
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(partial)

    manager._init_event_tail()

    # Only the complete "first" row is rehydrated; "second" is not consumed.
    assert [row["tool"] for row in manager._task_card_event_window()] == ["first"]
    # The offset must be at the start of the unterminated tail, not at the
    # current (larger) EOF, or the completed line would never be read.
    assert manager._event_tail_offset() == complete_size

    # Complete the line and poll: it must be read as one whole new row.
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "tool_call", "tool_name": "second",
            "tool_args": {"action": "x", "_reasoning": "y"}, "ts": 2.0,
        })[20:] + "\n")

    manager._poll_event_tail()
    assert [row["tool"] for row in manager._task_card_event_window()] == ["first", "second"]


# ---------------------------------------------------------------------------
# Latest-N order and bounded window
# ---------------------------------------------------------------------------


def test_window_keeps_only_latest_n_in_order(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)
    manager._TASK_CARD_EVENT_WINDOW = 3

    events_path = _events_path(tmp_path)
    lines = [_tool_call_line(tool_name=f"t{i}", call_id=f"c{i}") for i in range(7)]
    _write_lines(events_path, lines)

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert [row["tool"] for row in window] == ["t4", "t5", "t6"]


def test_reasoning_cap_includes_the_ellipsis_not_just_the_prefix(tmp_path):
    """The final displayed reasoning (prefix + ellipsis) must not exceed
    ``_TASK_CARD_EVENT_REASONING_CAP`` characters."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)
    cap = manager._TASK_CARD_EVENT_REASONING_CAP

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(reasoning="x" * (cap + 50))])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert len(window) == 1
    reasoning = window[0]["reasoning"]
    assert reasoning.endswith("…")
    assert len(reasoning) == cap


# ---------------------------------------------------------------------------
# Safe field whitelist: never forward raw tool_args
# ---------------------------------------------------------------------------


def test_only_safe_bounded_fields_are_projected(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [json.dumps({
        "type": "tool_call",
        "tool_name": "bash",
        "tool_call_id": "c1",
        "tool_trace_id": "t1",
        "ts": 1.0,
        "tool_args": {
            "action": "run",
            "_reasoning": "safe text",
            "command": "rm -rf /very/secret/path --token=abc123",
            "env": {"API_KEY": "sk-should-never-appear"},
        },
    })])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert len(window) == 1
    row = window[0]
    assert set(row.keys()) <= {"tool", "tool_action", "reasoning", "started_at", "status", "api_delay_s"}
    assert row["tool"] == "bash"
    assert row["tool_action"] == "run"
    assert row["reasoning"] == "safe text"

    manager._poll_event_tail()
    edits = [c for c in acct.calls if c[0] == "edit_message"]
    rendered = edits[-1][3]
    assert "sk-should-never-appear" not in rendered
    assert "/very/secret/path" not in rendered
    assert "--token=abc123" not in rendered


def test_tool_result_updates_status_and_elapsed(tmp_path):
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)
    path = _events_path(tmp_path)

    _write_lines(path, [_tool_call_line(tool_name="read", action="read", call_id="c1")])
    manager._poll_event_tail()
    assert "(0ms, running)" in [c for c in acct.calls if c[0] == "edit_message"][-1][3]

    _write_lines(path, [json.dumps({
        "type": "tool_result", "tool_call_id": "c1", "status": "ok",
        "elapsed_ms": 2300, "result": "RESULT_SECRET",
    })])
    manager._poll_event_tail()
    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    assert "(2.3s, success)" in rendered
    assert "RESULT_SECRET" not in rendered

    _write_lines(path, [
        _tool_call_line(tool_name="write", action="write", call_id="c2"),
        json.dumps({
            "type": "tool_result", "tool_call_id": "c2", "status": "error",
            "elapsed_ms": 400,
        }),
    ])
    manager._poll_event_tail()
    assert "(0.4s, error)" in [c for c in acct.calls if c[0] == "edit_message"][-1][3]


def test_second_tool_call_api_delay_is_previous_tool_ts_delta(tmp_path):
    """CHANGE A: the second tool call row carries ``api_delay_s`` equal to the
    ts gap since the previous tool call, and the render shows it distinctly
    from the tool-result elapsed (``3.4s api``)."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [
        _tool_call_line(tool_name="first", call_id="c1", ts=100.0),
        _tool_call_line(tool_name="second", call_id="c2", ts=103.4),
    ])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert [row["tool"] for row in window] == ["first", "second"]
    # First tool call of the stream has no prior progress: 0.0 baseline.
    assert window[0]["api_delay_s"] == 0.0
    # Second tool call: exact ts delta.
    assert window[1]["api_delay_s"] == 3.4
    # The raw ts stays private and never leaks into the public window.
    assert all("_ts" not in row for row in window)

    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    # The api delay is rendered on the group divider line, not in tool rows.
    assert "↻ 3.4s" in rendered
    # Tool rows no longer carry the api suffix.
    assert "3.4s api" not in rendered


def test_divider_renders_compact_per_call_usage_arrows(tmp_path):
    """The group divider carries per-call LLM usage: down arrow = output tokens,
    parenthesized count = thinking tokens, up arrow = cache miss, and the
    trailing percentage = cache rate. The usage arrives
    on the notification_block_injected carrier (real kernel shape), keyed by
    call_id, and is attached to the matching tool row."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    def carrier(call_id, out, miss, rate, context=None, thinking=None):
        token_usage = {
            "current_call": {
                "output": out,
                "cache_miss": miss,
                "cache_rate": rate,
            }
        }
        if thinking is not None:
            token_usage["current_call"]["thinking"] = thinking
        if context is not None:
            token_usage["session"] = {"context_tokens": context}
        return json.dumps({
            "type": "notification_block_injected",
            "call_id": call_id,
            "_meta": {"agent_meta": {"agent_state": {"token_usage": token_usage}}},
        })

    _write_lines(_events_path(tmp_path), [
        _tool_call_line(call_id="c1", ts=100.0),
        carrier("c1", 412, 31_000, 0.97716, "junk", 0),
        _tool_call_line(call_id="c2", ts=103.4),
        carrier("c2", 1_234, 512_345, 0.55, 259_800, 56_789),
    ])

    manager._poll_event_tail()

    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    # The visible tail (last group) carries its own API delay + arrows + rate.
    assert "↻ 3.4s" in rendered
    assert "\u21931.2k" in rendered    # ↓1.2k output tokens
    assert "(56.8k)" in rendered  # 56.8k thinking/reasoning tokens
    assert "\u2191512.3k" in rendered  # ↑512.3k cache miss
    assert "↻ 3.4s ↓1.2k (56.8k) ↑512.3k ◌ 259.8k | 55.0%" in rendered
    # Usage is private per-row state: projected for rendering but never
    # leaked into the public window rows.
    assert all("_usage" not in row for row in manager._task_card_event_window())
    assert all("_ts" not in row for row in manager._task_card_event_window())


def test_divider_usage_degrades_when_event_lacks_usage(tmp_path):
    """Old tool_call events without a usage carrier still render delay alone."""
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)
    _write_lines(_events_path(tmp_path), [
        _tool_call_line(tool_name="first", call_id="c1", ts=100.0),
        _tool_call_line(tool_name="second", call_id="c2", ts=103.4),
    ])
    manager._poll_event_tail()
    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    assert "↻ 3.4s" in rendered
    assert "\u2191" not in rendered
    assert "\u2193" not in rendered


def test_pure_text_turn_renders_api_usage_from_llm_response(tmp_path):
    """A pure-text turn (diary reply with no tool call) has no
    notification_block_injected carrier, so the divider usage arrows come from
    the llm_response event keyed by api_call_id (Jason 2026-08-08: pure text
    output turns only showed the API line with no usage)."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    def diary(text, ts, api_call_id):
        return json.dumps({
            "type": "diary", "ts": ts, "api_call_id": api_call_id,
            "text": text, "visibility": "public",
        })

    def llm_response(api_call_id, total, cached, out, thinking, ts):
        return json.dumps({
            "type": "llm_response", "ts": ts, "api_call_id": api_call_id,
            "input_tokens": total, "cached_tokens": cached,
            "output_tokens": out, "thinking_tokens": thinking,
            "estimated": False,
        })

    _write_lines(_events_path(tmp_path), [
        diary("first pure text", 100.0, "api_pure_1"),
        llm_response("api_pure_1", 1_000, 900, 123, 45, 105.0),
    ])

    manager._poll_event_tail()

    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    assert "first pure text" in rendered
    # llm_response usage rides the divider: output, thinking, cache miss, cache rate.
    assert "\u2193123 (45)" in rendered
    assert "\u2191100" in rendered   # 1000 - 900 cache miss
    assert "◌ 1.0k | 90.0%" in rendered  # context=input_tokens; rate=900/1000



def test_divider_context_fallbacks():
    cases = (
        (
            {"output": 200, "thinking": 0, "cache_miss": 2_400, "cache_rate": 0.99, "context": "junk"},
            "↻ 8.5s ↓200 (0) ↑2.4k 99.0%",
        ),
        (
            {"output": 200, "cache_miss": 2_400, "context": 259_800},
            "↻ 8.5s ↓200 ↑2.4k ◌ 259.8k",
        ),
    )
    for usage, expected in cases:
        assert TaskCardEventProjection.format_divider_info(8.5, usage) == expected

def test_pure_text_turn_api_line_has_no_bullet(tmp_path):
    """The API divider info line must not carry the bullet prefix (it would
    look like a tool row). Jason 2026-08-08."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    def diary(text, ts, api_call_id):
        return json.dumps({
            "type": "diary", "ts": ts, "api_call_id": api_call_id,
            "text": text, "visibility": "public",
        })

    _write_lines(_events_path(tmp_path), [
        diary("only text", 100.0, "api_no_usage"),
    ])
    manager._poll_event_tail()
    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    lines = rendered.split("\n")
    # No API info line at all when the turn has no usage (delay 0.0 for a lone
    # first row is not rendered), and no stray bullet-only line is invented.
    assert "only text" in rendered
    assert all("\u2022 API" not in line for line in lines)


def test_sub_second_tool_elapsed_renders_adaptive_seconds_not_zero_seconds(tmp_path):
    """CHANGE B: a 412ms tool result renders ``0.4s``, never the useless ``0s``."""
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)
    path = _events_path(tmp_path)

    _write_lines(path, [_tool_call_line(tool_name="fast", call_id="c1")])
    manager._poll_event_tail()
    _write_lines(path, [json.dumps({
        "type": "tool_result", "tool_call_id": "c1", "status": "ok",
        "elapsed_ms": 412, "result": "RESULT_SECRET",
    })])
    manager._poll_event_tail()

    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    assert "(0.4s, success)" in rendered
    assert "RESULT_SECRET" not in rendered


def test_tool_call_action_is_rendered_as_tool_action():
    event = json.loads(_tool_call_line(tool_name="knowledge", action="info"))
    row = TelegramManager._project_tool_call_row(event)

    assert row["tool_action"] == "info"
    rendered = TelegramManager._format_task_card_text("", "", "", rows=[row])
    assert "• knowledge.info: doing a thing" in rendered


def test_tool_call_without_action_preserves_tool_label():
    event = json.loads(_tool_call_line(tool_name="edit"))
    del event["tool_args"]["action"]
    row = TelegramManager._project_tool_call_row(event)

    assert "tool_action" not in row
    rendered = TelegramManager._format_task_card_text("", "", "", rows=[row])
    assert "• edit: doing a thing" in rendered


def test_empty_none_and_non_string_actions_do_not_add_suffix():
    for action in ("", None, 0, False, []):
        event = json.loads(_tool_call_line(tool_name="system"))
        event["tool_args"]["action"] = action
        row = TelegramManager._project_tool_call_row(event)

        assert "tool_action" not in row
        rendered = TelegramManager._format_task_card_text("", "", "", rows=[row])
        assert "• system: doing a thing" in rendered


# ---------------------------------------------------------------------------
# Row timestamp: derived from the event's own canonical ``ts``, never raw
# tool args, never the current render instant, safely omitted when malformed
# ---------------------------------------------------------------------------


def test_row_started_at_is_derived_from_event_ts_and_rendered(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    event_ts = 1752600000.0
    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(ts=event_ts)])

    manager._poll_event_tail()

    local = datetime.fromtimestamp(event_ts).astimezone()
    off = local.utcoffset()
    sign = "-" if off.total_seconds() < 0 else "+"
    hours = int(abs(off.total_seconds()) // 3600)
    expected = f"{local:%H:%M:%S} U{sign}{hours}"
    window = manager._task_card_event_window()
    assert len(window) == 1
    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert edits
    # The row itself carries no inline stamp; the single per-API-call stamp is
    # embedded centered in the symmetric divider line (Jason 2026-08-09).
    row_line = next(ln for ln in edits[-1][3].splitlines() if "bash.run" in ln)
    assert "UTC" not in row_line
    assert (
        f"\n{manager._TASK_CARD_API_CALL_DIVIDER} {expected} {manager._TASK_CARD_API_CALL_DIVIDER}\n"
        in f"\n{edits[-1][3]}"
    )


def test_event_log_final_carrier_projects_session_telemetry_into_final_render(tmp_path):
    """Rows and the footer telemetry come from their authoritative events.

    The final-carrier ``notification_block_injected`` owns the current whole ``agent_meta``
    snapshot; a retired ``tool_meta`` snapshot and row arguments are present as
    decoys and must not affect the automatic card.
    """
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    event_ts = 1752600000.0
    session = {
        "input_tokens": 2_345_678,
        "session_cache_rate": 0.87803,
        "cache_miss_tokens": 170631,
        "cache_miss_budget": 1_000_000,
        "api_calls": 13,
        "context_tokens": 171246,
        "context_window": 272000,
        "context_usage": 0.62958,
    }
    events_path = _events_path(tmp_path)
    row_event = json.loads(_tool_call_line(ts=event_ts, reasoning="event-log row"))
    row_event["tool_args"]["metadata"] = {"api_calls": 999}
    _write_lines(events_path, [
        json.dumps(row_event),
        # A plain tool_result is the live decoy immediately before the real
        # notification carrier; even if it fabricates agent_meta, it must not
        # own the current snapshot.
        json.dumps({
            "type": "tool_result",
            "tool_name": "bash",
            "tool_call_id": "c1",
            "_meta": {"agent_meta": {"agent_state": {"token_usage": {
                "session": {"api_calls": 888},
            }}}},
        }),
        json.dumps({
            "type": "notification_block_injected",
            "tool_name": "bash",
            "tool_call_id": "c1",
            "ts": event_ts + 1,
            "_meta": {
                "tool_meta": {"token_usage": {"session": {
                    "session_cache_rate": 0.01,
                    "api_calls": 1,
                }}},
                "agent_meta": {
                    "agent_state": {"token_usage": {
                        "current_call": {"api_calls": 999},
                        "session": session,
                    }},
                    "notifications": {"persistent": {"api_calls": 777}},
                },
            },
        }),
    ])

    manager._poll_event_tail()

    local = datetime.fromtimestamp(event_ts).astimezone()
    off = local.utcoffset()
    sign = "-" if off.total_seconds() < 0 else "+"
    hours = int(abs(off.total_seconds()) // 3600)
    expected_stamp = f"{local:%H:%M:%S} U{sign}{hours}"
    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert edits
    rendered = edits[-1][3]
    assert "• bash.run: event-log row" in rendered
    tool_row = next(ln for ln in rendered.splitlines() if "bash.run" in ln)
    assert expected_stamp not in tool_row
    # The single per-API-call stamp renders centered in the symmetric divider
    # line (Jason 2026-08-09), not inline on the tool row.
    assert (
        f"\n{manager._TASK_CARD_API_CALL_DIVIDER} {expected_stamp} {manager._TASK_CARD_API_CALL_DIVIDER}\n"
        in f"\n{rendered}"
    )
    assert "tokens 2.3M" in rendered
    assert "cache 87.8% · miss 170.6k/1.0M · calls 13" in rendered
    assert "ctx 63% · 171.2k/272.0k" in rendered
    assert "calls 888" not in rendered
    assert "calls 999" not in rendered
    assert "calls 777" not in rendered


def test_malformed_current_telemetry_carrier_clears_previous_snapshot(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [
        _tool_call_line(),
        json.dumps({
            "type": "notification_block_injected",
            "_meta": {"agent_meta": {"agent_state": {"token_usage": {
                "session": {"api_calls": 7},
            }}}},
        }),
    ])
    manager._poll_event_tail()
    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert "calls 7" in edits[-1][3]

    _write_lines(events_path, [json.dumps({
        "type": "notification_block_injected",
        "_meta": {"agent_meta": {"agent_state": {"token_usage": {
            "session": {"api_calls": "malformed"},
        }}}},
    })])
    manager._poll_event_tail()

    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert "calls 7" not in edits[-1][3]
    assert "session ·" not in edits[-1][3]


def _programmable_update(manager, account, chat_id, lines):
    return manager._handle_task_card_update({
        "sub_action": "update",
        "channel": "programmable",
        "account": account,
        "chat_id": chat_id,
        "card": {"lines": lines},
    })


def test_automatic_footer_label_is_last_updated(tmp_path):
    """The automatic channel's footer timestamp is labeled ``Last Updated``,
    reporting when its event-tail snapshot was rendered — not a wall clock
    that must change on unrelated Telegram edits."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line()])
    manager._poll_event_tail()

    edits = [call for call in acct.calls if call[0] == "edit_message"]
    rendered = edits[-1][3]
    assert "Last Updated: " in rendered
    assert "Current Time: " not in rendered


def test_programmable_frame_includes_its_own_last_updated_line(tmp_path):
    """A non-empty programmable frame carries its own ``Last Updated`` line,
    independent of the automatic channel's timestamp."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    result = _programmable_update(manager, acct.alias, 555, ["watch line"])
    assert result["status"] == "ok"

    edits = [call for call in acct.calls if call[0] == "edit_message"]
    rendered = edits[-1][3]
    assert "watch line" in rendered
    assert "Last Updated: " in rendered


def test_programmable_update_leaves_automatic_frame_unchanged(tmp_path):
    """A programmable edit must not read, advance, or override the automatic
    channel's committed frame/session footer."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [
        _tool_call_line(),
        json.dumps({
            "type": "notification_block_injected",
            "_meta": {"agent_meta": {"agent_state": {"token_usage": {
                "session": {"api_calls": 1},
            }}}},
        }),
    ])
    manager._poll_event_tail()
    key = manager._channel_key(acct.alias, 555)
    committed_automatic_before = manager._resident.frames[key]["automatic"]

    # New telemetry lands, but only a programmable edit runs — no automatic
    # poll re-reads events.jsonl in between.
    _write_lines(events_path, [json.dumps({
        "type": "notification_block_injected",
        "_meta": {"agent_meta": {"agent_state": {"token_usage": {
            "session": {"api_calls": 42},
        }}}},
    })])

    result = _programmable_update(manager, acct.alias, 555, ["watch line"])
    assert result["status"] == "ok"

    assert manager._resident.frames[key]["automatic"] == committed_automatic_before
    assert "calls 42" not in manager._resident.frames[key]["automatic"]


def test_automatic_update_leaves_programmable_frame_unchanged(tmp_path):
    """An automatic event-tail broadcast must preserve the committed
    programmable frame byte-for-byte."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    programmable_before = "— TASK CARD —\nprogrammable content"
    manager._set_channel_frame(acct.alias, 555, "programmable", programmable_before)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line()])
    manager._poll_event_tail()

    key = manager._channel_key(acct.alias, 555)
    assert manager._resident.frames[key]["programmable"] == programmable_before


def test_row_started_at_omitted_when_ts_missing(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    line = json.dumps({
        "type": "tool_call", "tool_name": "bash",
        "tool_args": {"action": "run", "_reasoning": "no ts here"},
    })
    _write_lines(events_path, [line])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert len(window) == 1
    assert "started_at" not in window[0]


def test_row_started_at_omitted_when_ts_malformed(tmp_path):
    """Bool, non-numeric, non-finite, and out-of-range ``ts`` all safely omit
    the row's stamp rather than crashing or fabricating one."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    bad_values = [True, "not-a-number", float("nan"), float("inf"), 1e20, 10**400]
    lines = [
        json.dumps({
            "type": "tool_call", "tool_name": f"tool{i}",
            "tool_args": {"action": "run", "_reasoning": "x"},
            "ts": v,
        })
        for i, v in enumerate(bad_values)
    ]
    _write_lines(events_path, lines)

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert len(window) == len(bad_values)
    for row in window:
        assert "started_at" not in row


def test_events_missing_expected_fields_are_skipped_fail_closed(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [
        json.dumps({"type": "tool_call"}),  # no tool_name/tool_args at all
        json.dumps({"type": "tool_call", "tool_name": 123, "tool_args": {}}),  # wrong type
        _tool_call_line(tool_name="valid-one"),
    ])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert [row["tool"] for row in window] == ["valid-one"]


# ---------------------------------------------------------------------------
# Broadcast to multiple resident Task Card targets, no route correlation
# ---------------------------------------------------------------------------


def test_broadcasts_same_projection_to_every_resident_target_across_accounts(tmp_path):
    acct1 = FakeAccount(alias="bot1")
    acct2 = FakeAccount(alias="bot2")
    manager, service = _manager(tmp_path, acct1, acct2)
    _pre_resident(acct1, 111, manager)
    _pre_resident(acct1, 222, manager)
    _pre_resident(acct2, 333, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="broadcast-me")])

    manager._poll_event_tail()

    for acct, chat_id in ((acct1, 111), (acct1, 222), (acct2, 333)):
        edits = [c for c in acct.calls if c[0] == "edit_message" and c[1] == chat_id]
        assert len(edits) == 1, f"expected exactly one edit for {acct.alias}:{chat_id}"
        assert "broadcast-me" in edits[0][3]


def test_normal_rows_limits_rendered_event_tail_without_shrinking_buffer(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [
        _tool_call_line(tool_name="older"),
        _tool_call_line(tool_name="newest", call_id="c2"),
    ])

    manager._poll_event_tail()

    # /taskcard 1 limits the render, while the fixed rehydration buffer retains
    # both recent events for a later increase to the normal-row setting.
    assert manager._taskcard_normal_rows() == 1
    assert [row["tool"] for row in manager._task_card_event_window()] == [
        "older", "newest",
    ]
    edits = [c for c in acct.calls if c[0] == "edit_message"]
    assert len(edits) == 1
    rendered = edits[0][3]
    assert "newest" in rendered
    # The older event must not be rendered as a tool row (checked via the
    # exact tool-row prefix, not a bare substring, because the metadata path
    # line may legitimately contain "older" inside words like "folders").
    assert "\u2022 older." not in rendered
    assert "current: 1" in rendered


def test_no_resident_targets_means_no_transport_calls(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    # No resident card anywhere.

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line()])

    manager._poll_event_tail()

    assert acct.calls == []


# ---------------------------------------------------------------------------
# No full-file scan: seek from the in-memory offset, not JSONLLoggingService
# ---------------------------------------------------------------------------


def test_poll_only_reads_appended_bytes_not_whole_file(tmp_path, monkeypatch):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="old")])
    manager._poll_event_tail()
    assert [row["tool"] for row in manager._task_card_event_window()] == ["old"]

    offset_before = manager._event_tail_offset()
    assert offset_before == events_path.stat().st_size

    _write_lines(events_path, [_tool_call_line(tool_name="new", call_id="c2")])
    manager._poll_event_tail()

    assert [row["tool"] for row in manager._task_card_event_window()] == ["old", "new"]
    assert manager._event_tail_offset() == events_path.stat().st_size


def test_truncated_or_replaced_file_reinitializes_from_new_eof(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="before-truncate")])
    manager._poll_event_tail()
    assert manager._event_tail_offset() == events_path.stat().st_size

    # Simulate log rotation: file replaced with a smaller one.
    events_path.write_text("", encoding="utf-8")
    _write_lines(events_path, [_tool_call_line(tool_name="after-truncate")])

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert [row["tool"] for row in window] == ["after-truncate"]


def test_truncation_to_empty_still_broadcasts_the_now_empty_window(tmp_path):
    """A non-empty -> empty truncation must be reflected honestly: the stale
    (larger) window must not keep being displayed just because the freshly
    rehydrated window happens to be empty."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="before-truncate")])
    manager._poll_event_tail()
    edits_before = [c for c in acct.calls if c[0] == "edit_message"]
    assert len(edits_before) == 1
    assert "before-truncate" in edits_before[0][3]

    # Replace with an empty file — same primitive as log rotation, but the
    # new tail has zero matching rows.
    events_path.write_text("", encoding="utf-8")

    manager._poll_event_tail()

    window = manager._task_card_event_window()
    assert window == []
    edits_after = [c for c in acct.calls if c[0] == "edit_message"]
    assert len(edits_after) == 2, "the now-empty window must still be broadcast"
    assert "before-truncate" not in edits_after[-1][3]


# ---------------------------------------------------------------------------
# Reverse-scan read failure must fail closed, not silently jump to EOF
# ---------------------------------------------------------------------------


def test_reverse_tail_read_failure_does_not_advance_offset_to_eof(tmp_path, monkeypatch):
    """If the reverse-tail scan itself fails (I/O error mid-read), the offset
    must NOT be advanced to EOF as though the file had actually been read —
    that would silently drop real history and make the failure unretryable."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="unreachable")])

    monkeypatch.setattr(
        manager, "_reverse_tail_latest_rows", lambda path, size: None,
    )

    manager._init_event_tail()

    assert manager._task_card_event_window() == []
    # Offset must NOT be the file's real size (which would silently mark
    # "unreachable" as consumed history it never actually read).
    assert manager._event_tail_offset() == 0


# ---------------------------------------------------------------------------
# Manager start/stop exactly once — one worker thread joined with the
# Telegram MCP manager lifecycle
# ---------------------------------------------------------------------------


class _ThreadCountingService:
    def __init__(self, accounts):
        self._accounts = {a.alias: a for a in accounts}
        self._order = [a.alias for a in accounts]
        self.default_account = accounts[0]
        self.start_calls = 0
        self.stop_calls = 0

    def get_account(self, alias):
        return self._accounts[alias]

    def list_accounts(self):
        return list(self._order)

    def taskcard_enabled(self):
        return True

    def taskcard_normal_rows(self):
        return 1

    def start(self):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1


def test_start_stop_run_exactly_one_tail_worker(tmp_path):
    acct = FakeAccount()
    service = _ThreadCountingService([acct])
    manager = TelegramManager(
        service,
        working_dir=Path(tmp_path),
        on_inbound=lambda _: None,
        notification_store=FakeNotificationStore(),
    )

    before_threads = set(threading.enumerate())
    manager.start()
    try:
        assert service.start_calls == 1
        new_threads = set(threading.enumerate()) - before_threads
        tail_threads = [
            t for t in new_threads if "task_card" in t.name.lower()
            or "event_tail" in t.name.lower() or "tail" in t.name.lower()
        ]
        assert len(tail_threads) == 1, f"expected exactly one tail worker, got {new_threads}"
    finally:
        manager.stop()
        assert service.stop_calls == 1

    # Give the thread a moment to actually exit.
    for t in list(threading.enumerate()):
        if t in new_threads:
            t.join(timeout=2.0)
            assert not t.is_alive()


def test_start_called_twice_does_not_start_a_second_worker(tmp_path):
    acct = FakeAccount()
    service = _ThreadCountingService([acct])
    manager = TelegramManager(
        service,
        working_dir=Path(tmp_path),
        on_inbound=lambda _: None,
        notification_store=FakeNotificationStore(),
    )

    before_threads = set(threading.enumerate())
    manager.start()
    manager.start()
    try:
        new_threads = set(threading.enumerate()) - before_threads
        tail_threads = [t for t in new_threads if "tail" in t.name.lower()]
        assert len(tail_threads) == 1
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# Programmable slot unchanged: automatic broadcast never touches the
# programmable channel's committed frame.
# ---------------------------------------------------------------------------


def test_programmable_slot_untouched_by_automatic_broadcast(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    # Commit a programmable frame first, the way the controller would.
    manager._set_channel_frame(acct.alias, 555, "programmable", "— TASK CARD —\nprogrammable content")

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line(tool_name="automatic-row")])

    manager._poll_event_tail()

    edits = [c for c in acct.calls if c[0] == "edit_message"]
    assert len(edits) == 1
    rendered = edits[-1][3]
    assert "automatic-row" in rendered
    assert "programmable content" in rendered


# ---------------------------------------------------------------------------
# Blanket 1s rebuild dedupe: same frame must not re-fire a Telegram edit;
# content change must; resident loss must force a re-send even on same frame.
# ---------------------------------------------------------------------------


def test_blanket_rebuild_skips_unchanged_frame(tmp_path):
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line()])
    manager._poll_event_tail()  # first broadcast creates/edits the resident

    assert len([c for c in acct.calls if c[0] == "edit_message"]) == 1
    acct.calls.clear()

    # Same frame on a later blanket tick: no Telegram edit.
    manager._broadcast_task_card_event_window()
    assert acct.calls == []

    # New event changes the frame: edit fires.
    _write_lines(events_path, [_tool_call_line(tool_name="second", reasoning="more")])
    manager._poll_event_tail()
    assert len([c for c in acct.calls if c[0] == "edit_message"]) == 1


def test_blanket_force_bypasses_fingerprint_but_not_edit_gate(tmp_path):
    """Truncation/replacement remains a legal forced re-render after the gate.

    ``force=True`` bypasses only unchanged-fingerprint dedupe; it cannot violate
    the per-target hard edit interval.
    """
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    manager._TASK_CARD_EVENT_POLL_INTERVAL = 5.0
    now = [100.0]
    manager._task_card_edit_clock = lambda: now[0]
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line()])
    manager._poll_event_tail()
    assert len([c for c in acct.calls if c[0] == "edit_message"]) == 1
    acct.calls.clear()

    manager._broadcast_task_card_event_window(force=True)
    assert acct.calls == []

    now[0] += manager._TASK_CARD_EVENT_POLL_INTERVAL
    manager._broadcast_task_card_event_window(force=True)
    assert len([c for c in acct.calls if c[0] == "edit_message"]) == 1


def test_replacement_force_throttled_retries_on_next_ordinary_blanket(tmp_path):
    """A one-shot replacement force survives the gate until real delivery.

    The replacement has byte-identical semantic content, so its normalized
    fingerprint equals the previously delivered frame.  The subsequent call is
    deliberately an ordinary blanket (no caller repeats ``force=True``).
    """
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    manager._TASK_CARD_EVENT_POLL_INTERVAL = 5.0
    now = [100.0]
    manager._task_card_edit_clock = lambda: now[0]
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    line = _tool_call_line()
    _write_lines(events_path, [line])
    manager._poll_event_tail()
    assert len([c for c in acct.calls if c[0] == "edit_message"]) == 1
    delivered_fingerprint = manager._task_card_automatic_fingerprints[("mybot", 555)]
    acct.calls.clear()

    # Atomic same-content replacement changes file identity and takes the sole
    # production force=True branch while the target gate remains closed.
    replacement = events_path.with_suffix(".replacement")
    _write_lines(replacement, [line])
    replacement.replace(events_path)
    manager._poll_event_tail()

    assert acct.calls == []
    assert manager._task_card_automatic_fingerprints[("mybot", 555)] == (
        delivered_fingerprint
    )
    assert ("mybot", 555) in manager._task_card_pending_force

    # Once eligible, a plain blanket drains the retained force before unchanged
    # fingerprint dedupe and performs the required re-render exactly once.
    now[0] += manager._TASK_CARD_EVENT_POLL_INTERVAL
    manager._broadcast_task_card_event_window()

    edits = [c for c in acct.calls if c[0] == "edit_message"]
    assert len(edits) == 1
    assert "bash" in edits[0][3]
    assert ("mybot", 555) not in manager._task_card_pending_force
    assert not manager._task_card_edit_is_pending("mybot", 555)


def test_blanket_resends_when_resident_lost_on_same_frame(tmp_path, monkeypatch):
    """Fingerprint match plus a missing tracked resident must still re-send.

    The resident-target enumeration and the tracked-resident lookup are
    independent: a peer process can rotate the durable state (target survives,
    resident id gone), so the blanket tick must not suppress the re-send just
    because the frame fingerprint is unchanged.
    """
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    events_path = _events_path(tmp_path)
    _write_lines(events_path, [_tool_call_line()])
    manager._poll_event_tail()
    assert len([c for c in acct.calls if c[0] == "edit_message"]) == 1
    acct.calls.clear()

    # Simulate durable-state rotation: the chat stays a target but the tracked
    # resident lookup now reports no resident.
    monkeypatch.setattr(manager, "_get_resident_task_card", lambda account, chat_id: None)
    manager._broadcast_task_card_event_window()
    assert [c[0] for c in acct.calls] == ["send_message"]


def test_pure_tool_turn_renders_api_usage_from_llm_response_group(tmp_path):
    """A pure-tool turn (tool call with no diary row, no
    notification_block_injected carrier — e.g. context.molt) still gets divider
    usage arrows. The tool_call event carries api_call_id; the projection now
    preserves it as _api_call_id so apply_tool_usages matches the llm_response
    per-call usage. Jason 2026-08-08: the molt row showed only ``↻ 8.0s``
    with no ↓↑ because the tool row dropped api_call_id."""
    acct = FakeAccount()
    manager, service = _manager(tmp_path, acct)
    _pre_resident(acct, 555, manager)

    def tool_call(api_call_id, call_id, ts):
        return json.dumps({
            "type": "tool_call", "ts": ts, "api_call_id": api_call_id,
            "tool_name": "context", "tool_call_id": call_id,
            "tool_trace_id": "t1", "tool_args": {"action": "molt", "_reasoning": "molt now"},
        })

    def llm_response(api_call_id, total, cached, out, ts):
        return json.dumps({
            "type": "llm_response", "ts": ts, "api_call_id": api_call_id,
            "input_tokens": total, "cached_tokens": cached,
            "output_tokens": out, "estimated": False,
        })

    _write_lines(_events_path(tmp_path), [
        tool_call("api_molt_1", "call_molt_1", 100.0),
        llm_response("api_molt_1", 2_000, 1_700, 456, 105.0),
    ])

    manager._poll_event_tail()

    rendered = [c for c in acct.calls if c[0] == "edit_message"][-1][3]
    assert "context" in rendered
    # llm_response usage rides the divider via the group api_call_id fallback:
    # output 456, cache miss 300, cache rate 85.0%.
    assert "\u2193456" in rendered
    assert "\u2191300" in rendered
    assert "85.0%" in rendered


def test_changed_poll_and_blanket_share_per_target_edit_gate(
    tmp_path, monkeypatch,
):
    """A changed poll plus its same-tick blanket may issue one edit per target.

    The metadata mutation between renders models async state changing in the
    narrow gap that used to make both broadcasts perform real Telegram edits.
    """
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    manager._TASK_CARD_EVENT_POLL_INTERVAL = 5.0
    now = [100.0]
    manager._task_card_edit_clock = lambda: now[0]
    _pre_resident(acct, 111, manager)
    _pre_resident(acct, 222, manager)
    live_metadata = {"api_calls": 1}
    monkeypatch.setattr(
        manager,
        "_task_card_event_metadata_snapshot",
        lambda: dict(live_metadata),
    )

    _write_lines(_events_path(tmp_path), [_tool_call_line()])
    manager._poll_event_tail()
    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert [call[1] for call in edits] == [111, 222]

    # The blanket sees genuinely different content, not a fingerprint no-op,
    # but both target-local gates are still closed in this tick.
    live_metadata["api_calls"] = 2
    manager._broadcast_task_card_event_window()
    assert len([call for call in acct.calls if call[0] == "edit_message"]) == 2

    now[0] += 4.999
    manager._broadcast_task_card_event_window()
    assert len([call for call in acct.calls if call[0] == "edit_message"]) == 2

    now[0] += 0.001
    manager._broadcast_task_card_event_window()
    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert [call[1] for call in edits] == [111, 222, 111, 222]
    assert all("calls 2" in call[3] for call in edits[-2:])


def test_inbound_ensure_and_enable_callback_use_same_resident_gate(
    tmp_path, monkeypatch,
):
    """Both loop-external automatic update paths share the edit timestamp."""
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    manager._TASK_CARD_EVENT_POLL_INTERVAL = 5.0
    now = [200.0]
    manager._task_card_edit_clock = lambda: now[0]
    _pre_resident(acct, 555, manager)
    live_metadata = {"api_calls": 1}
    monkeypatch.setattr(
        manager,
        "_task_card_event_metadata_snapshot",
        lambda: dict(live_metadata),
    )

    manager._broadcast_task_card_event_window()
    assert len([call for call in acct.calls if call[0] == "edit_message"]) == 1

    # Real inbound handling calls _ensure_task_card_resident outside the tail
    # loop. It must not edit the same resident again inside the interval.
    manager.on_incoming("mybot", {"message": {
        "message_id": 8,
        "date": 1781600000,
        "from": {"username": "alice"},
        "chat": {"id": 555, "type": "private"},
        "text": "hello",
    }})
    assert len([call for call in acct.calls if call[0] == "edit_message"]) == 1

    # Re-enable sees changed automatic content and therefore reaches delivery;
    # the same resident gate, rather than fingerprint dedupe, suppresses it.
    live_metadata["api_calls"] = 2
    manager._on_taskcard_changed(False)
    manager._on_taskcard_changed(True)
    assert len([call for call in acct.calls if call[0] == "edit_message"]) == 1

    now[0] += manager._TASK_CARD_EVENT_POLL_INTERVAL
    manager._on_taskcard_changed(False)
    manager._on_taskcard_changed(True)
    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert len(edits) == 2
    assert "calls 2" in edits[-1][3]


def test_active_seconds_tick_does_not_edit_after_interval(
    tmp_path, monkeypatch,
):
    """An eligible blanket tick still dedupes a pure active-seconds change."""
    acct = FakeAccount()
    manager, _ = _manager(tmp_path, acct)
    manager._TASK_CARD_EVENT_POLL_INTERVAL = 5.0
    now = [300.0]
    manager._task_card_edit_clock = lambda: now[0]
    _pre_resident(acct, 555, manager)
    live_metadata = {
        "agent_lifecycle": "active",
        "agent_active_seconds": 1.0,
    }
    monkeypatch.setattr(
        manager,
        "_task_card_event_metadata_snapshot",
        lambda: dict(live_metadata),
    )

    manager._broadcast_task_card_event_window()
    assert len([call for call in acct.calls if call[0] == "edit_message"]) == 1

    now[0] += manager._TASK_CARD_EVENT_POLL_INTERVAL
    live_metadata["agent_active_seconds"] = 6.0
    manager._broadcast_task_card_event_window()
    assert len([call for call in acct.calls if call[0] == "edit_message"]) == 1

    live_metadata.clear()
    live_metadata["agent_lifecycle"] = "idle"
    manager._broadcast_task_card_event_window()
    edits = [call for call in acct.calls if call[0] == "edit_message"]
    assert len(edits) == 2
    assert "agent · idle" in edits[-1][3]


def test_fingerprint_ignores_only_wall_clock_ticks(tmp_path):
    """Last Updated and active seconds are volatile; lifecycle/content are not."""
    manager, _ = _manager(tmp_path, FakeAccount())
    timestamp_a = "\u2699 WORKING\n\u2022 row\n\nfooter\nLast Updated: 10:00:00 U+8"
    timestamp_b = "\u2699 WORKING\n\u2022 row\n\nfooter\nLast Updated: 10:00:01 U+8"
    other_row = "\u2699 WORKING\n\u2022 other\n\nfooter\nLast Updated: 10:00:00 U+8"
    assert manager._task_card_automatic_fingerprint(timestamp_a) == \
        manager._task_card_automatic_fingerprint(timestamp_b)
    assert manager._task_card_automatic_fingerprint(timestamp_a) != \
        manager._task_card_automatic_fingerprint(other_row)

    def render(metadata):
        return TaskCardEventProjection.render_event_groups(
            [], metadata=metadata, normal_rows=1,
        )

    active_12 = render({
        "agent_lifecycle": "active", "agent_active_seconds": 12.0,
    })
    active_13 = render({
        "agent_lifecycle": "active", "agent_active_seconds": 13.0,
    })
    idle = render({"agent_lifecycle": "idle"})
    active_with_usage = render({
        "agent_lifecycle": "active",
        "agent_active_seconds": 13.0,
        "api_calls": 1,
    })
    fingerprint = manager._task_card_automatic_fingerprint
    assert fingerprint(active_12) == fingerprint(active_13)
    assert fingerprint(active_12) != fingerprint(idle)
    assert fingerprint(active_13) != fingerprint(active_with_usage)
