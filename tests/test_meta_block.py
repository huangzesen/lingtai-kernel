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
    assert meta == {}


def test_build_meta_time_blind_regardless_of_timezone_awareness():
    # time_awareness=False short-circuits even when timezone_awareness=True.
    agent = _fake_agent(time_awareness=False, timezone_awareness=True)
    assert build_meta(agent) == {}


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


def test_render_meta_non_empty_without_current_time_returns_empty():
    # Documents current limitation: render_meta only renders current_time.
    # When future fields (e.g. context-window breakdown) are added to build_meta,
    # render_meta must be extended or this test will catch the silent drop.
    agent = _fake_agent_with_lang("en")
    assert render_meta(agent, {"future_field": 123}) == ""


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
