"""Tests for stamp_tool_result with time_awareness flag."""
from lingtai_kernel.tool_timing import stamp_tool_result


def test_stamp_tool_result_time_aware_default():
    """Default behaviour: timezone_awareness=True, so local-tz offset (not Z)."""
    import re
    result = {"status": "ok"}
    out = stamp_tool_result(result, 42)
    assert "current_time" in out
    # Default is timezone_awareness=True → local time with ±HH:MM offset
    assert not out["current_time"].endswith("Z"), (
        f"expected local-tz offset by default, got {out['current_time']!r}"
    )
    assert re.search(r"[+-]\d{2}:\d{2}$", out["current_time"]), (
        f"no ±HH:MM suffix in {out['current_time']!r}"
    )
    assert out["_elapsed_ms"] == 42


def test_stamp_tool_result_time_aware_explicit():
    result = {"status": "ok"}
    out = stamp_tool_result(result, 42, time_awareness=True)
    assert "current_time" in out
    assert out["_elapsed_ms"] == 42


def test_stamp_tool_result_time_blind_drops_both_keys():
    result = {"status": "ok"}
    out = stamp_tool_result(result, 42, time_awareness=False)
    assert "current_time" not in out
    assert "_elapsed_ms" not in out
    assert out["status"] == "ok"


def test_stamp_tool_result_time_blind_mutates_in_place_still_returns_dict():
    """Existing in-place contract preserved: returns the same dict object."""
    result = {"status": "ok"}
    out = stamp_tool_result(result, 42, time_awareness=False)
    assert out is result


def test_stamp_tool_result_timezone_awareness_true_uses_local():
    """When timezone_awareness=True, current_time has ±HH:MM offset, not Z."""
    import re
    from lingtai_kernel.tool_timing import stamp_tool_result
    result = {}
    stamp_tool_result(result, 100, time_awareness=True, timezone_awareness=True)
    ts = result["current_time"]
    assert not ts.endswith("Z"), f"expected local-tz offset, got {ts!r}"
    assert re.search(r"[+-]\d{2}:\d{2}$", ts), f"no ±HH:MM suffix in {ts!r}"


def test_stamp_tool_result_timezone_awareness_false_uses_utc():
    """When timezone_awareness=False, current_time ends in Z."""
    from lingtai_kernel.tool_timing import stamp_tool_result
    result = {}
    stamp_tool_result(result, 100, time_awareness=True, timezone_awareness=False)
    assert result["current_time"].endswith("Z")


def test_stamp_tool_result_time_awareness_false_omits_regardless():
    """time_awareness=False short-circuits regardless of timezone_awareness."""
    from lingtai_kernel.tool_timing import stamp_tool_result
    r1 = {}
    stamp_tool_result(r1, 100, time_awareness=False, timezone_awareness=True)
    assert "current_time" not in r1
    r2 = {}
    stamp_tool_result(r2, 100, time_awareness=False, timezone_awareness=False)
    assert "current_time" not in r2
