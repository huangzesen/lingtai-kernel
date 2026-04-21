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


def test_build_meta_time_blind_returns_empty_dict():
    agent = _fake_agent(time_awareness=False)
    meta = build_meta(agent)
    assert "current_time" not in meta and meta["system_tokens"] == -1


def test_build_meta_time_blind_regardless_of_timezone_awareness():
    # time_awareness=False short-circuits even when timezone_awareness=True.
    agent = _fake_agent(time_awareness=False, timezone_awareness=True)
    meta = build_meta(agent)
    assert "current_time" not in meta and meta["system_tokens"] == -1


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


def test_render_meta_time_blind_with_context_present_emits_empty_time_slot():
    """Known edge case (documented in spec): a time-blind agent whose session
    has context data produces '[Current time:  | context: ...]' with an empty
    time slot. This is intentional — the spec accepts this and defers a
    time-blind-specific template to a follow-up. If future work changes the
    behavior, this test must be updated together with the spec."""
    agent = _fake_agent_with_lang("en")
    meta = {
        "system_tokens": 4720,
        "context_tokens": 9450,
        "context_usage": 0.071,
    }
    assert render_meta(agent, meta) == "[Current time:  | context: 7.1% (sys 4720 + ctx 9450)]"
