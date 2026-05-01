"""Tests for meta_block — unified per-turn metadata injection."""
from __future__ import annotations

import re
from types import SimpleNamespace

from lingtai_kernel.meta_block import build_meta, render_meta, stamp_meta


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


def test_build_meta_time_blind_emits_context_sentinel():
    agent = _fake_agent(time_awareness=False)
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert meta["context"]["system_tokens"] == -1


def test_build_meta_time_blind_regardless_of_timezone_awareness():
    # time_awareness=False short-circuits even when timezone_awareness=True.
    agent = _fake_agent(time_awareness=False, timezone_awareness=True)
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert meta["context"]["system_tokens"] == -1


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
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[Current time: 2026-04-20T10:15:23-07:00 | context: 7.1% (sys 4720 + ctx 9450)]"


def test_render_meta_zh_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("zh")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：7.1% (系统 4720 + 对话 9450)]"


def test_render_meta_wen_uses_existing_current_time_template():
    agent = _fake_agent_with_lang("wen")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：7.1% (系统 4720 + 对话 9450)]"


def test_render_meta_non_empty_without_current_time_returns_empty():
    # Verifies render_meta ignores keys it doesn't know how to render
    # (neither current_time nor any context field). Produces '' so the
    # caller can omit the prefix entirely.
    agent = _fake_agent_with_lang("en")
    assert render_meta(agent, {"future_field": 123}) == ""


def test_render_meta_context_unknown_sentinel_en():
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": -1,
            "history_tokens": -1,
            "usage": -1.0,
        },
    }
    assert render_meta(agent, meta) == "[Current time: 2026-04-20T10:15:23-07:00 | context: unavailable]"


def test_render_meta_context_unknown_sentinel_zh():
    agent = _fake_agent_with_lang("zh")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {
            "system_tokens": -1,
            "history_tokens": -1,
            "usage": -1.0,
        },
    }
    assert render_meta(agent, meta) == "[此时：2026-04-20T10:15:23-07:00 | 上下文：未知]"


def test_render_meta_rounds_usage_to_one_decimal():
    """Usage ratios round to one decimal place, not raw float."""
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "T",
        "context": {
            "system_tokens": 1000,
            "history_tokens": 500,
            "usage": 0.0723456,
        },
    }
    result = render_meta(agent, meta)
    assert "7.2%" in result


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


def test_stamp_meta_elapsed_ms_overrides_meta_key():
    # Guard: if meta ever carries _elapsed_ms, the measured value wins.
    result = {}
    stamp_meta(result, {"_elapsed_ms": 9999}, 7)
    assert result["_elapsed_ms"] == 7


def _fake_agent_with_session(
    *,
    time_awareness=True,
    timezone_awareness=True,
    language="en",
    system_prompt_tokens=0,
    tools_tokens=0,
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
                # Real interface.estimate_context_tokens() returns
                # system + tools + conversation — match that contract.
                return system_prompt_tokens + tools_tokens + history_tokens

        interface = _iface()

    chat_obj = _Chat() if decomp_ran else None
    # Server-authoritative wire-count: system + tools + history.
    # This is the invariant our production code relies on
    # (history = latest_input - system - tools).
    latest_input = system_prompt_tokens + tools_tokens + history_tokens

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
            _latest_input_tokens=latest_input,
            _token_decomp_dirty=not decomp_ran,
            _chat=chat_obj,
            chat=chat_obj,
        ),
    )


def test_build_meta_emits_context_fields_when_decomp_ran():
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
        context_limit=100000,
    )
    meta = build_meta(agent)
    # system = system_prompt + tools = 5000 + 500 = 5500
    assert meta["context"]["system_tokens"] == 5500
    # history = 200
    assert meta["context"]["history_tokens"] == 200
    # usage = (5500 + 200) / 100000 = 0.057
    assert abs(meta["context"]["usage"] - 0.057) < 1e-6


def test_build_meta_emits_sentinels_before_decomp_runs():
    # When decomposition has never run (dirty flag True) and no chat yet,
    # we cannot compute any of the three fields honestly.
    agent = _fake_agent_with_session(decomp_ran=False)
    meta = build_meta(agent)
    assert meta["context"]["system_tokens"] == -1
    assert meta["context"]["history_tokens"] == -1
    assert meta["context"]["usage"] == -1.0


def test_build_meta_history_falls_back_to_interface_estimate_after_restore():
    """After start() rehydrates the wire ChatInterface from chat_history.jsonl,
    _latest_input_tokens is still 0 until the first LLM call completes. The
    meta-line must fall back to interface.estimate_context_tokens() so the
    first post-refresh text_input shows the restored history, not '对话 0'."""
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=50000,  # restored from JSONL
    )
    # Simulate pre-first-LLM-call state: interface has history but server
    # has not reported an input count yet.
    agent._session._latest_input_tokens = 0
    meta = build_meta(agent)
    # history should come from interface.estimate_context_tokens(), not 0
    assert meta["context"]["history_tokens"] == 50000
    assert meta["context"]["system_tokens"] == 5500


def test_build_meta_time_blind_still_emits_context_fields():
    agent = _fake_agent_with_session(
        time_awareness=False,
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
    )
    meta = build_meta(agent)
    assert "current_time" not in meta
    assert meta["context"]["system_tokens"] == 5500
    assert meta["context"]["history_tokens"] == 200


def test_render_meta_time_blind_with_context_present_emits_empty_time_slot():
    """Known edge case (documented in spec): a time-blind agent whose session
    has context data produces '[Current time:  | context: ...]' with an empty
    time slot. This is intentional — the spec accepts this and defers a
    time-blind-specific template to a follow-up. If future work changes the
    behavior, this test must be updated together with the spec."""
    agent = _fake_agent_with_lang("en")
    meta = {
        "context": {
            "system_tokens": 4720,
            "history_tokens": 9450,
            "usage": 0.071,
        },
    }
    assert render_meta(agent, meta) == "[Current time:  | context: 7.1% (sys 4720 + ctx 9450)]"


def test_build_meta_history_tokens_does_not_double_count_system_and_tools():
    """Regression: history_tokens must NOT include the system prompt or tool
    schema tokens (they belong to system_tokens). Computed from the server's
    authoritative input count minus system + tools, mirroring
    SessionManager.get_token_usage's ctx_history_tokens."""
    agent = _fake_agent_with_session(
        system_prompt_tokens=5000,
        tools_tokens=500,
        history_tokens=200,
    )
    meta = build_meta(agent)
    # system_tokens = 5000 + 500 = 5500
    assert meta["context"]["system_tokens"] == 5500
    # history_tokens = history only = 200
    assert meta["context"]["history_tokens"] == 200
    # usage = (5500 + 200) / 100000 = 0.057
    assert abs(meta["context"]["usage"] - 0.057) < 1e-6


def test_build_meta_usage_matches_get_context_pressure_after_restore():
    """Regression: on the very first turn after a restore (before the first
    LLM call returns), the meta-prefix usage% must match what
    SessionManager.get_context_pressure() would report for the same state.
    Otherwise the molt warning and the injected '[... | context: X%]'
    prefix show different numbers on the same turn, confusing the agent.

    Pre-fix bug: build_meta treated estimate_context_tokens() as
    history-only, but the real method returns system + tools + conversation.
    That made history_tokens = full estimate, which then double-counted
    system + tools when added to system_tokens in the usage calculation.
    """
    sys_prompt = 5000
    tools = 500
    history = 50000
    limit = 100000
    agent = _fake_agent_with_session(
        system_prompt_tokens=sys_prompt,
        tools_tokens=tools,
        history_tokens=history,
        context_limit=limit,
    )
    # Simulate post-restore state: wire chat rehydrated from JSONL,
    # but no LLM response has landed yet for this run.
    agent._session._latest_input_tokens = 0
    meta = build_meta(agent)

    # history_tokens must be history-only, not the full estimate
    assert meta["context"]["history_tokens"] == history
    assert meta["context"]["system_tokens"] == sys_prompt + tools

    # meta usage% must equal what get_context_pressure() would return:
    # pressure = estimate_context_tokens() / limit = (sys+tools+history) / limit
    expected_pressure = (sys_prompt + tools + history) / limit
    assert abs(meta["context"]["usage"] - expected_pressure) < 1e-9


# ---------------------------------------------------------------------------
# notifications — drain agent.inbox into meta when drain_inbox=True
# ---------------------------------------------------------------------------

def _fake_agent_with_inbox(messages: list, *, language: str = "en"):
    """Agent stand-in with an inbox queue. messages is a list of pre-built Message objects."""
    import queue as _q
    inbox = _q.Queue()
    for m in messages:
        inbox.put(m)
    return SimpleNamespace(
        _config=SimpleNamespace(
            time_awareness=False,
            timezone_awareness=False,
            language=language,
        ),
        inbox=inbox,
    )


def _msg(content: str):
    """Minimal Message-like object — only .content is read by the drain helper."""
    from lingtai_kernel.message import _make_message, MSG_REQUEST
    return _make_message(MSG_REQUEST, "system", content)


def test_notifications_absent_when_inbox_empty_with_drain():
    agent = _fake_agent_with_inbox([])
    meta = build_meta(agent, drain_inbox=True)
    assert "notifications" not in meta


def test_notifications_absent_when_inbox_empty_without_drain():
    agent = _fake_agent_with_inbox([])
    meta = build_meta(agent)
    assert "notifications" not in meta


def test_notifications_absent_when_agent_has_no_inbox():
    """build_meta must tolerate agents without an inbox attribute (legacy callers)."""
    agent = _fake_agent(time_awareness=False)  # no inbox attribute
    meta = build_meta(agent, drain_inbox=True)
    assert "notifications" not in meta


def test_notifications_full_content_when_drained():
    msgs = [_msg("[system] mail from alice"), _msg("[soul flow] short whisper"), _msg("[system] mail from bob")]
    agent = _fake_agent_with_inbox(msgs)
    meta = build_meta(agent, drain_inbox=True)
    notifs = meta["notifications"]
    assert isinstance(notifs, list)
    assert len(notifs) == 3
    assert notifs[0] == "[system] mail from alice"
    assert notifs[1] == "[soul flow] short whisper"
    assert notifs[2] == "[system] mail from bob"


def test_drain_consumes_inbox():
    msgs = [_msg("a"), _msg("b")]
    agent = _fake_agent_with_inbox(msgs)
    build_meta(agent, drain_inbox=True)
    # Drain MUST consume — messages are now in meta["notifications"], not inbox.
    assert agent.inbox.qsize() == 0


def test_no_drain_preserves_inbox():
    msgs = [_msg("a"), _msg("b")]
    agent = _fake_agent_with_inbox(msgs)
    meta = build_meta(agent)  # default drain_inbox=False
    # Inbox untouched — text-input prefix path doesn't drain.
    assert agent.inbox.qsize() == 2
    assert "notifications" not in meta


def test_notifications_preserve_full_content_no_truncation():
    """JSON channel can carry the whole message — no 200-char ceiling."""
    long_content = "x" * 500
    agent = _fake_agent_with_inbox([_msg(long_content)])
    meta = build_meta(agent, drain_inbox=True)
    assert meta["notifications"][0] == long_content
    assert len(meta["notifications"][0]) == 500


def test_notifications_preserve_newlines():
    """JSON channel preserves message structure — no newline flattening."""
    agent = _fake_agent_with_inbox([_msg("line1\nline2\nline3")])
    meta = build_meta(agent, drain_inbox=True)
    assert meta["notifications"][0] == "line1\nline2\nline3"


def test_notifications_list_all_queued_messages_in_fifo_order():
    msgs = [_msg(f"msg-{i}") for i in range(15)]
    agent = _fake_agent_with_inbox(msgs)
    meta = build_meta(agent, drain_inbox=True)
    notifs = meta["notifications"]
    assert len(notifs) == 15
    assert notifs[0] == "msg-0"
    assert notifs[14] == "msg-14"


def test_render_meta_does_not_render_notifications():
    """Notifications never appear in the text-input prefix — they live in
    the user-turn body (drained by _concat_queued_messages) or in tool-result
    JSON (drained by tool-result meta_fn). Renderer ignores them entirely."""
    agent = _fake_agent_with_lang("en")
    meta = {
        "current_time": "2026-04-20T10:15:23-07:00",
        "context": {"system_tokens": 100, "history_tokens": 200, "usage": 0.003},
        "notifications": ["[system] mail from alice", "[soul flow] short whisper"],
    }
    rendered = render_meta(agent, meta)
    # Only time + context appear; notifications are not rendered.
    assert "alice" not in rendered
    assert "soul flow" not in rendered
    assert "Pending" not in rendered
    assert "2026-04-20T10:15:23-07:00" in rendered
    assert "0.3%" in rendered


def test_stamp_meta_propagates_notifications_to_tool_result():
    msgs = [_msg("[system] new mail")]
    agent = _fake_agent_with_inbox(msgs)
    meta = build_meta(agent, drain_inbox=True)
    result = {"status": "ok"}
    stamp_meta(result, meta, 50)
    # Tool result carries notifications as a top-level list of strings.
    assert "notifications" in result
    assert result["notifications"] == ["[system] new mail"]
