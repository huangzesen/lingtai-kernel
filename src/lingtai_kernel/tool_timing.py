import time
from datetime import datetime, timezone


def stamp_tool_result(result: dict, elapsed_ms: int, *, time_awareness: bool = True) -> dict:
    """Inject current_time and _elapsed_ms into a tool result dict (in-place).

    When ``time_awareness`` is False, neither key is injected — the result
    leaves this function without any sense of wall-clock time or duration.
    """
    if time_awareness:
        result["current_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result["_elapsed_ms"] = elapsed_ms
    return result


class ToolTimer:
    """Context manager for timing tool execution."""
    def __init__(self):
        self._start = 0.0
        self.elapsed_ms = 0
    
    def __enter__(self):
        self._start = time.monotonic()
        return self
    
    def __exit__(self, *exc):
        self.elapsed_ms = int((time.monotonic() - self._start) * 1000)
        return False
