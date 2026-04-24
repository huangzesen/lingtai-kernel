# Tool-Pairing Invariant — Design

**Date:** 2026-04-23
**Author:** Zesen Huang (with Claude)
**Scope:** `lingtai-kernel` — `ChatInterface` and `base_agent` AED recovery path

## Problem

DeepSeek V4 (and OpenAI, strictly) reject chat-completions requests where an assistant message with `tool_calls` is not immediately followed by matching tool messages. Lenient providers (Zhipu, GLM, most local runners) silently accept this shape, which has masked a latent kernel bug.

The failing wire shape looks like:

```
assistant[tool_calls=A,B,C]   ← dangling
user[text="[system] agent was interrupted, retry"]
user[tool_result=A, B, C]      ← too late, came after a user message
```

DeepSeek responds with HTTP 400: `"An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'."`

## Root cause

The `ChatInterface` canonical model does not enforce a structural invariant tying an assistant entry's `ToolCallBlocks` to the next entry's `ToolResultBlocks`. Specifically:

1. **`add_user_message()` accepts any call** regardless of tail state, so a user-text entry can be appended after an unanswered assistant `[tool_calls]`.
2. **`enforce_tool_pairing()` is set-equality only** — it checks that every `ToolCallBlock.id` has a matching `ToolResultBlock.id` somewhere in the interface, but not that they are in the correct positions.

The primary violation path is **AED (Agent Error Dispatch) recovery** in `base_agent.py:943-987`:

1. Tool-loop `send(tool_results)` raises (timeout, API error, etc.)
2. The interface tail is left as `assistant[tool_calls]` with no matching tool_result entry (the revert logic in the adapter rolls back the tool_result entry but leaves the earlier assistant turn).
3. AED handler catches the exception and builds a recovery system message `aed_msg`.
4. `send(aed_msg)` routes to `OpenAIChatSession.send(message=str)` which calls `add_user_message(aed_msg)`.
5. The interface now contains `assistant[tool_calls] | user[aed_msg]` — a positional violation.
6. On the next API send, DeepSeek rejects with 400. Zhipu / GLM silently accept and the agent carries a permanently broken history.

Secondary violation paths (lower frequency): intercepted tool-result commits (`base_agent.py:1261`) and partial revert after an adapter `send()` exception. These are covered by the same fix — the interface-level guard in change 1 rejects any future `add_user_message` call that would land on a dangling tail, forcing callers to close first.

## Non-goals

- **Run-loop queueing of external mail.** External messages arrive via `self.inbox` (a `queue.Queue`) and are drained by `_concat_queued_messages` after the outer `_handle_request` returns. Tool execution inside `_handle_request` is synchronous (via `execute_tools_batch` with `ThreadPoolExecutor.as_completed`), so inbox messages cannot interleave mid-tool-loop. No queue work is needed for external messages; the existing architecture is sound.
- **Auto-heal in `enforce_tool_pairing`.** Adding positional fix-up to `enforce_tool_pairing` was tried as a band-aid and rejected; it hides the underlying bug and leaves synthetic stripped-text entries in history. The canonical interface should be correct by construction.
- **New provider-specific wrappers.** The fix is canonical (in `ChatInterface`) so every OpenAI-compat provider — OpenAI, DeepSeek, Zhipu, GLM, Kimi, custom, etc. — gets well-formed requests.

## Design

Three changes, in order of scope:

### 1. `ChatInterface` — invariant enforcement at construction

Add a pending-tool-calls predicate, a closing operation, and a structural guard on `add_user_message` / `add_user_blocks`.

**File:** `src/lingtai_kernel/llm/interface.py`

```python
class PendingToolCallsError(Exception):
    """Raised when a user entry would be appended while tool_calls are unanswered."""


class ChatInterface:

    def has_pending_tool_calls(self) -> bool:
        """True iff the tail entry is an assistant with unanswered ToolCallBlocks."""
        if not self._entries:
            return False
        last = self._entries[-1]
        if last.role != "assistant":
            return False
        return any(isinstance(b, ToolCallBlock) for b in last.content)

    def close_pending_tool_calls(self, reason: str) -> None:
        """Synthesize placeholder ToolResultBlocks for unanswered tool_calls on
        the tail assistant entry. No-op if the tail has no pending calls.

        Used by recovery paths (AED, restore-from-disk) to bring the interface
        into a valid state before appending a new user entry.
        """
        if not self.has_pending_tool_calls():
            return
        last = self._entries[-1]
        pending = [b for b in last.content if isinstance(b, ToolCallBlock)]
        placeholder = [
            ToolResultBlock(id=b.id, name=b.name, content=f"[aborted: {reason}]")
            for b in pending
        ]
        self._append("user", placeholder)

    def add_user_message(self, text: str) -> InterfaceEntry:
        if self.has_pending_tool_calls():
            raise PendingToolCallsError(
                "Cannot append user message while the tail assistant turn has "
                "unanswered tool_calls. Call close_pending_tool_calls(reason) or "
                "add_tool_results(...) first."
            )
        return self._append("user", [TextBlock(text=text)])

    def add_user_blocks(self, blocks: list[ContentBlock]) -> InterfaceEntry:
        # Tool results are the legitimate closing op — allow them.
        is_tool_result_only = blocks and all(isinstance(b, ToolResultBlock) for b in blocks)
        if self.has_pending_tool_calls() and not is_tool_result_only:
            raise PendingToolCallsError(
                "Cannot append non-tool-result user blocks while the tail "
                "assistant turn has unanswered tool_calls."
            )
        return self._append("user", blocks)
```

`add_tool_results` is unchanged — it is the legitimate closing operation and requires no guard (it appends `ToolResultBlocks`, not text).

`enforce_tool_pairing` stays as-is — with the invariant enforced at construction, set-equality is sufficient for defense-in-depth cleanup.

### 2. AED recovery — close pending calls before revive

**File:** `src/lingtai_kernel/base_agent.py` (AED handler, around line 943-987)

Before the line that sends the recovery system message (`self._session.send(aed_msg)`), close any dangling tool_calls on the interface tail:

```python
# After a failed tool-loop send, the interface tail may be
# assistant[tool_calls] with no matching tool_results. Close it before
# injecting the recovery message so we don't violate the chat-completions
# invariant (strict providers like DeepSeek / OpenAI reject otherwise).
if self._chat is not None:
    iface = self._chat.interface
    if iface.has_pending_tool_calls():
        iface.close_pending_tool_calls(reason=err_desc or "aed_recovery")
```

`err_desc` is the truncated error message already captured by AED at the catch site — a useful breadcrumb for the model on the next turn. The placeholder tool_results take the form `[aborted: <err_desc>]`.

This same pattern applies to any future recovery path that needs to inject a system message after a partial tool-loop (e.g., molt triggered mid-loop, if that ever becomes a thing).

### 3. Session restore — close dangling calls on rehydrate

**File:** `src/lingtai_kernel/session.py` (`restore_chat`, around line 385)

After `ChatInterface.from_dict(messages)` produces the restored interface, proactively close any dangling tool_calls before the session starts taking new input. Handles the case where a prior process crashed between tool_call and tool_result persistence.

```python
def restore_chat(self, state: dict) -> None:
    from .llm.interface import ChatInterface
    messages = state.get("messages")
    if messages:
        try:
            interface = ChatInterface.from_dict(messages)
            interface.enforce_tool_pairing()
            if interface.has_pending_tool_calls():
                interface.close_pending_tool_calls(
                    reason="restored from disk — prior session ended mid-tool-loop"
                )
            self._rebuild_session(interface)
            return
        except Exception as e:
            logger.warning(
                f"[{self._display_name}] Failed to restore chat: {e}. Starting fresh.",
                exc_info=True,
            )
    self.ensure_session()
```

## Data flow (after changes)

### Normal tool-loop (unchanged)
```
LLM → assistant[tool_calls=A,B]
      ↓ (add_assistant_message — no invariant check, allowed)
tool executor → execute both, collect results
      ↓ (add_tool_results — legitimate close, allowed)
LLM → assistant[text reply]
```

### AED recovery (changed)
```
LLM → assistant[tool_calls=A,B]                          ← entry N
      ↓ (add_assistant_message, OK)
send(tool_results=...) appends user[tool_results=A,B]    ← entry N+1 (transient)
      ↓ API call raises (timeout / 400 / etc.)
adapter except-block runs drop_trailing(role=user)       ← entry N+1 removed
      ↓ interface tail is now entry N: assistant[tool_calls=A,B], unanswered
AED catches the propagated error
      ↓ interface.close_pending_tool_calls(reason=err_desc)
      ↓ interface appends entry N+1': user[tool_result=A ("[aborted: ...]"), tool_result=B ...]
send(aed_msg)
      ↓ add_user_message("[recovery] please continue") — invariant holds
LLM → next turn
```

### Session restore (changed)
```
chat_history.jsonl on disk ends in assistant[tool_calls=A,B]  (crash before tool_results persisted)
      ↓ ChatInterface.from_dict()
      ↓ interface.enforce_tool_pairing() — sets equal (no orphans), no-op
      ↓ interface.has_pending_tool_calls() == True
      ↓ interface.close_pending_tool_calls(reason="restored from disk — ...")
      ↓ interface tail is now user[tool_result=A ("[aborted: restored...]"), ...]
session ready to accept next input
```

## Error handling

- **`PendingToolCallsError`** is raised only when a caller violates the invariant. It is a programmer-error exception — not a runtime failure to catch in production. Tests exercise every code path that calls `add_user_message` to ensure the invariant is always held.
- **Existing adapter `except Exception: drop_trailing(...)` revert logic** remains in place. If a send fails mid-tool-loop, the trailing user entry (holding the tool_results that didn't get successfully transmitted) is dropped, leaving the interface in the pre-send state. AED then runs `close_pending_tool_calls` before its recovery message.
- **No silent healing.** If `add_user_message` raises, the stack trace points at the exact line that violated the invariant. This is a feature, not a bug.

## Test plan

### New unit tests (`tests/test_chat_interface_invariant.py`)

1. `test_add_user_message_rejects_pending_tool_calls` — build interface ending in unanswered `assistant[tool_calls]`, assert `add_user_message("x")` raises `PendingToolCallsError`.
2. `test_add_user_blocks_rejects_pending_tool_calls_unless_results` — `add_user_blocks([TextBlock(...)])` raises, `add_user_blocks([ToolResultBlock(...)])` succeeds.
3. `test_close_pending_tool_calls_synthesizes_results` — for an interface with unanswered `[A, B]`, after `close_pending_tool_calls("test")`, the tail is a user entry with two `ToolResultBlock`s matching A and B, each containing `[aborted: test]`.
4. `test_close_pending_tool_calls_noop_on_clean_tail` — idempotent when tail is clean.
5. `test_has_pending_tool_calls` — returns True only when last entry is assistant with ToolCallBlocks.
6. `test_close_then_add_user_message_succeeds` — full sequence: close pending, then append user message.

### Integration tests

7. **AED recovery replay** — construct interface matching the failing archive scenario (`assistant[tool_calls×3]` at tail, no results), simulate AED path, send to mock client, verify the request messages now contain `role=tool` entries before the revive user message.
8. **Session restore rehydrate** — feed a `chat_history.jsonl` fragment with trailing unanswered tool_calls through `restore_chat`, verify the rehydrated interface has no pending tool_calls.

### Regression

Existing `test_llm_service.py`, `test_adapter_registry.py`, `test_deepseek_adapter.py`, `test_llm_utils.py` must all continue to pass.

## Migration / deployment

- **No schema change to `chat_history.jsonl`.** The new synthetic tool_result blocks serialize through the existing `ToolResultBlock.to_dict()` path. Old jsonl files are readable by the new code; new jsonl files are readable by old code (pre-fix), though the old code won't have the invariant check.
- **Stranded in-flight agents** running on old kernel code today may have broken interface state on disk. On next process start with the new kernel, `restore_chat` will auto-close any dangling tool_calls with a "restored from disk" placeholder.
- **No config changes required.**

## Out of scope (future work)

- Extending the invariant to `to_anthropic` / `to_gemini` converters (which have their own pairing models). The canonical-interface invariant already prevents malformed interfaces; provider-specific converters inherit correctness for free.
- Surfacing `PendingToolCallsError` to the LLM as a tool-control signal (e.g., "wait, you have pending calls"). Out of scope — the invariant is a kernel guarantee, not an LLM-visible concept.
