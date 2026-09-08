"""Focused regressions for issue #1579 display-boundary formatting."""
import pytest

from lingtai.kernel.nudge import _format_duration
from lingtai.mcp_servers.task_card.event_projection import TaskCardEventProjection


@pytest.mark.parametrize(
    "value, expected",
    [
        (999, "999"),
        (1_000, "1.0k"),
        (999_949, "999.9k"),
        (999_950, "1.0M"),
        (1_000_000, "1.0M"),
        (999_949_999, "999.9M"),
        (999_950_000, "1.0B"),
        (1_000_000_000, "1.0B"),
        (999_949_999_999, "999.9B"),
        (999_950_000_000, "1.0T"),
        (1_000_000_000_000, "1.0T"),
        (999_999_999_999_999, "999.9T"),
        (10**100, "999.9T"),
    ],
)
def test_count_rounding_carries_at_each_tier_and_clamps_t(value, expected):
    assert TaskCardEventProjection.format_count(value) == expected


@pytest.mark.parametrize(
    "value",
    [-1, -10**100, 1.5, 1_000.0, True, False, "1000"],
)
def test_count_formatter_rejects_negative_non_integer_inputs(value):
    assert TaskCardEventProjection.format_count(value) is None


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (0.5, "0.5s"),
        (59.9, "59.9s"),
        (60, "1m"),
        (60.5, "60.5s"),
        (3599, "3599s"),
        (3600, "1h"),
        (3600.5, "3600.5s"),
        (86399, "86399s"),
        (86400, "24h"),
        (86400.5, "86400.5s"),
        (24 * 3600, "24h"),
        (25 * 3600, "25h"),
        (36 * 3600, "36h"),
        (47 * 3600, "47h"),
        (48 * 3600, "2d"),
        (73 * 3600, "73h"),
    ],
)
def test_duration_boundaries_preserve_fractional_hours_and_exact_days(seconds, expected):
    assert _format_duration(seconds) == expected
