"""ToolExecutor — sequential and parallel tool call execution."""
from __future__ import annotations

import json as _json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .llm.base import ToolCall
from .loop_guard import LoopGuard
from .tool_timing import ToolTimer, stamp_tool_result
from .types import UnknownToolError

# Default max tool result size: 50 KB.
_DEFAULT_MAX_RESULT_BYTES = 50_000


def _truncate_result(result: Any, max_bytes: int) -> Any:
    """Truncate a tool result if its serialized size exceeds max_bytes."""
    if max_bytes <= 0:
        return result
    if isinstance(result, str):
        if len(result) > max_bytes:
            return result[:max_bytes] + f"\n\n[truncated — showing first {max_bytes} of {len(result)} bytes]"
        return result
    if isinstance(result, dict):
        serialized = _json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) <= max_bytes:
            return result
        # Truncate the largest string/list values
        truncated = dict(result)
        patches = {}
        for key, val in truncated.items():
            if isinstance(val, str) and len(val) > max_bytes // 2:
                patches[key] = val[:max_bytes // 2] + f"\n[truncated — {len(val)} bytes total]"
            elif isinstance(val, list) and len(_json.dumps(val, ensure_ascii=False, default=str)) > max_bytes // 2:
                kept = []
                size = 0
                for item in val:
                    item_size = len(_json.dumps(item, ensure_ascii=False, default=str))
                    if size + item_size > max_bytes // 2:
                        break
                    kept.append(item)
                    size += item_size
                patches[key] = kept
                patches[f"_{key}_truncated"] = f"showing {len(kept)} of {len(val)} items"
        truncated.update(patches)
        return truncated
    return result


class ToolExecutor:
    """Executes tool calls sequentially or in parallel."""

    def __init__(
        self,
        dispatch_fn: Callable[[ToolCall], Any],
        make_tool_result_fn: Callable,
        guard: LoopGuard,
        known_tools: set[str] | None = None,
        parallel_safe_tools: set[str] | None = None,
        logger_fn: Callable | None = None,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
        time_awareness: bool = True,
        timezone_awareness: bool = True,
    ) -> None:
        self._dispatch_fn = dispatch_fn
        self._make_tool_result_fn = make_tool_result_fn
        self._guard = guard
        self._known_tools = known_tools or set()
        self._parallel_safe_tools = parallel_safe_tools or set()
        self._logger_fn = logger_fn
        self._max_result_bytes = max_result_bytes
        self._time_awareness = time_awareness
        self._timezone_awareness = timezone_awareness

    @property
    def guard(self) -> LoopGuard:
        return self._guard

    @guard.setter
    def guard(self, value: LoopGuard) -> None:
        self._guard = value

    def _log(self, event_type: str, **fields) -> None:
        if self._logger_fn:
            self._logger_fn(event_type, **fields)

    def execute(
        self,
        tool_calls: list[ToolCall],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
        collected_errors: list[str] | None = None,
    ) -> tuple[list, bool, str]:
        """Execute tool calls. Returns (results, intercepted, intercept_text)."""
        if collected_errors is None:
            collected_errors = []

        all_parallel_safe = (
            len(tool_calls) > 1
            and self._parallel_safe_tools
            and all(tc.name in self._parallel_safe_tools for tc in tool_calls)
        )

        if all_parallel_safe:
            return self._execute_parallel(
                tool_calls, collected_errors,
                on_result_hook=on_result_hook,
                cancel_event=cancel_event,
            )
        else:
            return self._execute_sequential(
                tool_calls, collected_errors,
                on_result_hook=on_result_hook,
                cancel_event=cancel_event,
            )

    def _execute_single(
        self,
        tc: ToolCall,
        collected_errors: list[str],
        *,
        on_result_hook: Callable | None = None,
    ) -> tuple[Any, bool, str]:
        tc_id = getattr(tc, "id", None)
        args = dict(tc.args) if tc.args else {}
        reasoning = args.pop("reasoning", None)
        args.pop("commentary", None)
        args.pop("_sync", None)

        if reasoning:
            self._log("tool_reasoning", tool=tc.name, reasoning=reasoning)
            args["_reasoning"] = reasoning

        verdict = self._guard.record_tool_call(tc.name, args)
        if verdict.blocked:
            result = {
                "status": "blocked",
                "_duplicate_warning": verdict.warning,
                "message": f"Execution skipped — duplicate call #{verdict.count}",
            }
            msg = self._make_tool_result_fn(tc.name, result, tool_call_id=tc_id)
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status="blocked",
                elapsed_ms=0,
                result=result,
                duplicate_count=verdict.count,
            )
            return msg, False, ""

        self._log("tool_call", tool_name=tc.name, tool_call_id=tc_id, tool_args=args)
        timer = ToolTimer()
        try:
            # Pre-check for unknown tool (records in guard for limit tracking)
            if self._known_tools and tc.name not in self._known_tools:
                self._guard.record_invalid_tool(tc.name)
                raise UnknownToolError(tc.name)

            with timer:
                result = self._dispatch_fn(
                    ToolCall(name=tc.name, args=args, id=tc_id)
                )

            result = _truncate_result(result, self._max_result_bytes)

            if isinstance(result, dict):
                stamp_tool_result(result, timer.elapsed_ms, time_awareness=self._time_awareness, timezone_awareness=self._timezone_awareness)

            status = result.get("status", "success") if isinstance(result, dict) else "success"
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status=status,
                elapsed_ms=timer.elapsed_ms,
                result=result,
            )

            if verdict.warning and isinstance(result, dict):
                result["_duplicate_warning"] = verdict.warning

            if isinstance(result, dict) and result.get("intercept"):
                intercept_text = result.get("text", "")
                result_msg = self._make_tool_result_fn(tc.name, result, tool_call_id=tc_id)
                return result_msg, True, intercept_text

            result_msg = self._make_tool_result_fn(tc.name, result, tool_call_id=tc_id)

            if isinstance(result, dict) and result.get("status") == "error":
                err_msg = result.get("message", "unknown error")
                collected_errors.append(f"{tc.name}: {err_msg}")

            if on_result_hook is not None:
                intercept = on_result_hook(tc.name, args, result)
                if intercept is not None:
                    return result_msg, True, intercept

            return result_msg, False, ""

        except Exception as e:
            err_result = {"status": "error", "message": str(e)}
            stamp_tool_result(err_result, timer.elapsed_ms, time_awareness=self._time_awareness, timezone_awareness=self._timezone_awareness)
            result_msg = self._make_tool_result_fn(tc.name, err_result, tool_call_id=tc_id)
            collected_errors.append(f"{tc.name}: {e}")
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status="error",
                elapsed_ms=timer.elapsed_ms,
                result=err_result,
                exception=type(e).__name__,
                exception_message=str(e),
            )
            return result_msg, False, ""

    def _execute_sequential(
        self,
        tool_calls: list[ToolCall],
        collected_errors: list[str],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
    ) -> tuple[list, bool, str]:
        tool_results = []
        for tc in tool_calls:
            if cancel_event is not None and cancel_event.is_set():
                return [], False, ""
            result_msg, intercepted, intercept_text = self._execute_single(
                tc, collected_errors, on_result_hook=on_result_hook,
            )
            if result_msg is not None:
                tool_results.append(result_msg)
            if intercepted:
                return tool_results, True, intercept_text
        return tool_results, False, ""

    def _execute_parallel(
        self,
        tool_calls: list[ToolCall],
        collected_errors: list[str],
        *,
        on_result_hook: Callable | None = None,
        cancel_event: Any | None = None,
    ) -> tuple[list, bool, str]:
        # Phase 1: Pre-check duplicates (sequential — guard not thread-safe)
        to_execute: list[tuple[int, ToolCall, dict]] = []
        tool_results: list[tuple[int, Any]] = []

        for i, tc in enumerate(tool_calls):
            tc_id = getattr(tc, "id", None)
            args = dict(tc.args) if tc.args else {}
            reasoning = args.pop("reasoning", None)
            args.pop("commentary", None)
            args.pop("_sync", None)

            if reasoning:
                self._log("tool_reasoning", tool=tc.name, reasoning=reasoning)
                args["_reasoning"] = reasoning

            verdict = self._guard.record_tool_call(tc.name, args)
            if verdict.blocked:
                result = {
                    "status": "blocked",
                    "_duplicate_warning": verdict.warning,
                    "message": f"Execution skipped — duplicate call #{verdict.count}",
                }
                tool_results.append((i, self._make_tool_result_fn(
                    tc.name, result, tool_call_id=tc_id,
                )))
                self._log(
                    "tool_result",
                    tool_name=tc.name,
                    tool_call_id=tc_id,
                    tool_args=args,
                    status="blocked",
                    elapsed_ms=0,
                    result=result,
                    duplicate_count=verdict.count,
                )
            else:
                to_execute.append((i, tc, args))

        if not to_execute:
            tool_results.sort(key=lambda x: x[0])
            return [r for _, r in tool_results], False, ""

        # Phase 2: Execute in parallel
        results_map: dict[int, Any] = {}
        errors_map: dict[int, str] = {}

        def _run_one(index: int, tc: ToolCall, args: dict):
            tc_id = getattr(tc, "id", None)
            self._log("tool_call", tool_name=tc.name, tool_call_id=tc_id, tool_args=args)
            timer = ToolTimer()
            with timer:
                result = self._dispatch_fn(
                    ToolCall(name=tc.name, args=args, id=tc.id)
                )
            result = _truncate_result(result, self._max_result_bytes)
            if isinstance(result, dict):
                stamp_tool_result(result, timer.elapsed_ms, time_awareness=self._time_awareness, timezone_awareness=self._timezone_awareness)
            status = result.get("status", "success") if isinstance(result, dict) else "success"
            self._log(
                "tool_result",
                tool_name=tc.name,
                tool_call_id=tc_id,
                tool_args=args,
                status=status,
                elapsed_ms=timer.elapsed_ms,
                result=result,
            )
            return index, result

        pool = ThreadPoolExecutor(max_workers=len(to_execute))
        try:
            futures = {
                pool.submit(_run_one, i, tc, args): i
                for i, tc, args in to_execute
            }
            for future in as_completed(futures, timeout=300.0):
                if cancel_event is not None and cancel_event.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return [], False, ""
                try:
                    idx, result = future.result()
                    results_map[idx] = result
                except Exception as e:
                    idx = futures[future]
                    errors_map[idx] = str(e)
                    tc_entry = next(((tc, args) for i, tc, args in to_execute if i == idx), None)
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args = tc_entry[1] if tc_entry else {}
                    self._log(
                        "tool_result",
                        tool_name=tc_name,
                        tool_call_id=tc_id,
                        tool_args=tc_args,
                        status="error",
                        elapsed_ms=0,
                        result={"status": "error", "message": str(e)},
                        exception=type(e).__name__,
                        exception_message=str(e),
                    )
        except TimeoutError:
            for future, idx in futures.items():
                if idx not in results_map and idx not in errors_map:
                    errors_map[idx] = "Timed out"
                    tc_entry = next(((tc, args) for i, tc, args in to_execute if i == idx), None)
                    tc_name = tc_entry[0].name if tc_entry else "unknown"
                    tc_id_t = getattr(tc_entry[0], "id", None) if tc_entry else None
                    tc_args_t = tc_entry[1] if tc_entry else {}
                    self._log(
                        "tool_result",
                        tool_name=tc_name,
                        tool_call_id=tc_id_t,
                        tool_args=tc_args_t,
                        status="error",
                        elapsed_ms=0,
                        result={"status": "error", "message": "Timed out"},
                    )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Phase 3: Build result messages (sequential)
        for i, tc, args in to_execute:
            tc_id = getattr(tc, "id", None)
            if i in results_map:
                result = results_map[i]
                tool_results.append((i, self._make_tool_result_fn(
                    tc.name, result, tool_call_id=tc_id,
                )))
                if isinstance(result, dict) and result.get("status") == "error":
                    err_msg = result.get("message", "unknown error")
                    collected_errors.append(f"{tc.name}: {err_msg}")
                if isinstance(result, dict) and result.get("intercept"):
                    tool_results.sort(key=lambda x: x[0])
                    return (
                        [r for _, r in tool_results],
                        True,
                        result.get("text", ""),
                    )
            elif i in errors_map:
                err_msg = errors_map[i]
                err_result = {"status": "error", "message": err_msg}
                tool_results.append((i, self._make_tool_result_fn(
                    tc.name, err_result, tool_call_id=tc_id,
                )))
                collected_errors.append(f"{tc.name}: {err_msg}")

        tool_results.sort(key=lambda x: x[0])
        return [r for _, r in tool_results], False, ""
