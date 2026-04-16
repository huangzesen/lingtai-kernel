"""Tests for stamp_tool_result with time_awareness flag."""
from lingtai_kernel.tool_timing import stamp_tool_result


def test_stamp_tool_result_time_aware_default():
    result = {"status": "ok"}
    out = stamp_tool_result(result, 42)
    assert "current_time" in out
    assert out["current_time"].endswith("Z")
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
