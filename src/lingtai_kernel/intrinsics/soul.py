"""Soul intrinsic — the agent's inner voice.

Two modes:
    flow    — persistent mirrored chat. Agent's diary becomes soul's input,
              soul's response becomes agent's input. Shares the principle
              section of the system prompt. Automatic, continuous.
    inquiry — sync mirror session. Clones conversation (text+thinking only),
              sends question, returns answer in tool result. On-demand.

Actions:
    inquiry — ask yourself a question, get the answer in the tool result
    delay   — adjust the idle wait before flow fires
"""
from __future__ import annotations


def get_description(lang: str = "en") -> str:
    from ..i18n import t
    return t(lang, "soul.description")


def get_schema(lang: str = "en") -> dict:
    from ..i18n import t
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inquiry", "flow", "delay"],
                "description": t(lang, "soul.action_description"),
            },
            "inquiry": {
                "type": "string",
                "description": t(lang, "soul.inquiry_description"),
            },
            "delay": {
                "type": "number",
                "description": t(lang, "soul.delay_description"),
            },
        },
        "required": ["action"],
    }


_MIN_DELAY = 1.0


def handle(agent, args: dict) -> dict:
    """Handle soul tool — inquiry/flow/delay.

    flow is mechanically triggered — fires every ``_soul_delay`` seconds on
    a wall clock. Manual invocation is rejected so the action stays honest
    about its involuntary nature; agents control cadence via delay, on-demand
    reflection via inquiry.
    """
    action = args.get("action", "")

    if action == "flow":
        return {
            "error": (
                f"soul flow fires automatically every {agent._soul_delay}s. "
                "It cannot be invoked manually. Use inquiry for on-demand "
                "reflection, or adjust delay to control flow cadence."
            ),
        }

    if action == "inquiry":
        inquiry = args.get("inquiry", "")
        if not isinstance(inquiry, str) or not inquiry.strip():
            return {"error": "inquiry is required — what do you want to reflect on?"}

        agent._log("soul_inquiry", inquiry=inquiry.strip()[:200])

        result = soul_inquiry(agent, inquiry.strip())

        if result:
            agent._persist_soul_entry(result, mode="inquiry")
            agent._log("soul_inquiry_done")
            return {"status": "ok", "voice": result["voice"]}
        else:
            agent._log("soul_inquiry_done")
            return {"status": "ok", "voice": "(silence)"}

    elif action == "delay":
        delay = args.get("delay")
        try:
            delay = float(delay)
        except (TypeError, ValueError):
            return {"error": "delay must be a number."}
        if delay < _MIN_DELAY:
            return {"error": f"delay must be >= {_MIN_DELAY} seconds."}

        old = agent._soul_delay
        agent._soul_delay = delay
        agent._log("soul_delay", old=old, new=delay)
        agent._workdir.write_manifest(agent._build_manifest())
        return {"status": "ok", "delay": delay}

    else:
        return {"error": f"Unknown soul action: {action}. Use inquiry or delay (flow is mechanical)."}


def _send_with_timeout(agent, session, content: str):
    """Send with timeout using a daemon thread. Returns response or None.

    Uses a daemon thread so it dies with the process — no orphaned threads.
    """
    import threading
    timeout = agent._config.retry_timeout
    result_box: list = []
    error_box: list = []

    def _worker():
        try:
            result_box.append(session.send(content))
        except Exception as e:
            error_box.append(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Timed out — thread is daemon, will die with process
        agent._log("soul_whisper_error", error=f"LLM call timed out after {timeout}s")
        return None
    if error_box:
        agent._log("soul_whisper_error", error=str(error_box[0])[:200])
        return None
    return result_box[0] if result_box else None


def _collect_new_diary(agent) -> str:
    """Collect the agent's diary since the last soul whisper.

    Includes assistant text (narration) and thinking blocks. The soul
    needs both the agent's inner reasoning and its outward narration
    to reflect meaningfully.

    Uses agent._soul_cursor to track how far we've read.
    Returns squashed text of all new diary entries, or empty string.
    """
    from ..llm.interface import TextBlock, ThinkingBlock

    if agent._chat is None:
        return ""

    entries = agent._chat.interface.entries
    cursor = getattr(agent, "_soul_cursor", 0)
    parts: list[str] = []

    for i in range(cursor, len(entries)):
        entry = entries[i]
        if entry.role != "assistant":
            continue
        for block in entry.content:
            if isinstance(block, ThinkingBlock) and block.text:
                parts.append(f"[thinking] {block.text}")
            elif isinstance(block, TextBlock) and block.text:
                parts.append(block.text)

    # Update cursor to current end
    agent._soul_cursor = len(entries)

    return "\n\n".join(parts)


def _build_soul_system_prompt(agent) -> str:
    """Build the soul session's system prompt."""
    custom = getattr(agent, "_soul_flow_prompt", "")
    if custom:
        return custom
    from ..i18n import t
    return t(agent._config.language, "soul.system_prompt")


def _soul_history_path(agent):
    """Path to the soul session's persisted history."""
    return agent._working_dir / "history" / "soul_history.jsonl"


def _soul_cursor_path(agent):
    """Path to the soul cursor file."""
    return agent._working_dir / "history" / "soul_cursor.json"


def _save_soul_session(agent) -> None:
    """Persist soul session history and cursor to disk."""
    session = getattr(agent, "_soul_session", None)
    if session is None:
        return
    try:
        import json
        entries = session.interface.to_dict()
        path = _soul_history_path(agent)
        path.parent.mkdir(exist_ok=True)
        lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
        path.write_text("\n".join(lines) + "\n")
        # Cursor saved separately — changes independently (e.g. on molt)
        _soul_cursor_path(agent).write_text(
            json.dumps({"cursor": getattr(agent, "_soul_cursor", 0)})
        )
    except Exception:
        pass  # best-effort — snapshot will catch it


def _ensure_soul_session(agent):
    """Get or create the persistent soul mirror session."""
    if getattr(agent, "_soul_session", None) is not None:
        return agent._soul_session

    system_prompt = _build_soul_system_prompt(agent)

    # Try to restore from disk
    interface = None
    path = _soul_history_path(agent)
    if path.is_file():
        try:
            import json
            from ..llm.interface import ChatInterface
            messages = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if messages:
                interface = ChatInterface.from_dict(messages)
        except Exception:
            interface = None  # start fresh
    # Restore cursor from separate file
    cursor_path = _soul_cursor_path(agent)
    if cursor_path.is_file():
        try:
            import json
            agent._soul_cursor = json.loads(cursor_path.read_text()).get("cursor", 0)
        except Exception:
            pass

    try:
        agent._soul_session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=interface,
        )
    except Exception as e:
        agent._log("soul_whisper_error", error=f"Failed to create soul session: {e}")
        agent._soul_session = None

    return agent._soul_session


def _write_soul_tokens(agent, response) -> None:
    """Append soul session token usage to the agent's ledger."""
    u = response.usage
    if not (u.input_tokens or u.output_tokens or u.thinking_tokens or u.cached_tokens):
        return
    try:
        from ..token_ledger import append_token_entry
        ledger_path = agent._working_dir / "logs" / "token_ledger.jsonl"
        soul = getattr(agent, "_soul_session", None)
        model = getattr(soul, "_model", None) or getattr(agent.service, "model", None)
        endpoint = getattr(agent.service, "_base_url", None)
        append_token_entry(
            ledger_path,
            input=u.input_tokens, output=u.output_tokens,
            thinking=u.thinking_tokens, cached=u.cached_tokens,
            model=model, endpoint=endpoint,
            extra={"source": "soul"},
        )
    except Exception:
        pass


def soul_flow(agent) -> dict | None:
    """Flow mode — persistent mirrored chat.

    Agent's diary → squashed into one message → sent to the soul session.
    Soul responds → injected into agent's inbox as plain text.

    The soul session persists across cycles. It shares the principle
    section of the system prompt but has no tools, no covenant, no identity.
    """
    # Collect new diary entries since last whisper
    diary = _collect_new_diary(agent)

    if not diary:
        return None  # nothing to reflect on

    session = _ensure_soul_session(agent)
    if session is None:
        return None

    response = _send_with_timeout(agent, session, diary)
    if not response or not response.text:
        return None

    _write_soul_tokens(agent, response)

    # Gradual forgetting — drop oldest entries when over budget
    _trim_soul_session(agent)

    return {
        "prompt": diary,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


def enqueue_flow_voice(agent, voice: str, thinking: list) -> None:
    """Build a synthetic (call, result) pair for a soul flow voice and
    enqueue it on the agent's involuntary tool-call inbox.

    Coalesces with prior unspliced flow pairs (source="soul.flow"): if the
    agent has been busy and multiple flows fired without draining, only the
    most recent voice survives.

    The call args carry only ``action="flow"`` — that alone tells the agent
    "this was the involuntary flow action, not the agent-initiated inquiry
    action." No ``trigger`` field needed; the action enum is the type.
    """
    import secrets
    import time
    from ..llm.interface import ToolCallBlock, ToolResultBlock
    from ..tc_inbox import InvoluntaryToolCall

    tc_id = f"tc_{int(time.time())}_{secrets.token_hex(2)}"
    call = ToolCallBlock(id=tc_id, name="soul", args={"action": "flow"})
    payload: dict = {"status": "ok", "voice": voice}
    if thinking:
        payload["thinking"] = thinking
    result_block = ToolResultBlock(id=tc_id, name="soul", content=payload)
    agent._tc_inbox.enqueue(InvoluntaryToolCall(
        call=call,
        result=result_block,
        source="soul.flow",
        enqueued_at=time.time(),
        coalesce=True,
    ))


def _trim_soul_session(agent) -> None:
    """Drop oldest conversation entries if soul session exceeds token budget.

    The soul gradually forgets — old exchanges fade while recent ones remain.
    Entries are dropped one at a time from the front (oldest first),
    preserving the system prompt entry.
    """
    session = getattr(agent, "_soul_session", None)
    if session is None:
        return

    limit = agent._config.soul_context_limit
    if limit <= 0:
        return

    iface = session.interface
    tokens = iface.estimate_context_tokens()

    while tokens > limit:
        # Find first non-system entry to drop
        dropped = False
        for i, entry in enumerate(iface.entries):
            if entry.role != "system":
                iface._entries.pop(i)
                dropped = True
                break
        if not dropped:
            break  # only system entries left
        tokens = iface.estimate_context_tokens()


def reset_soul_session(agent) -> None:
    """Reset the soul's diary cursor after molt.

    The soul session itself is NOT reset — the soul's inner conversation
    persists across molts. Only the cursor resets so the soul re-reads
    the agent's post-molt diary from the beginning.
    """
    agent._soul_cursor = 0
    # Persist the reset cursor immediately — prevents desync if crash
    # occurs between molt and next soul whisper
    _save_soul_session(agent)


def soul_inquiry(agent, question: str) -> dict | None:
    """Inquiry mode — one-shot mirror session with cloned conversation.

    Clones the agent's conversation (thinking + diary only, no tool
    calls/results), sends the question. Fresh session each time.
    """
    from ..llm.interface import ChatInterface, TextBlock, ThinkingBlock

    cloned = ChatInterface()

    if agent._chat is not None:
        for entry in agent._chat.interface.entries:
            if entry.role == "system":
                continue
            stripped: list = []
            for block in entry.content:
                if isinstance(block, (TextBlock, ThinkingBlock)):
                    stripped.append(block)
            if stripped:
                if entry.role == "assistant":
                    cloned.add_assistant_message(stripped)
                else:
                    cloned.add_user_blocks(stripped)

    system_prompt = _build_soul_system_prompt(agent)
    system_prompt += "\n\nYou have no tools. Respond with plain text only. Never output tool calls or XML tags."

    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=cloned,
        )
    except Exception as e:
        agent._log("soul_whisper_error", error=str(e)[:200])
        return None

    response = _send_with_timeout(agent, session, question)
    if not response or not response.text:
        return None

    _write_soul_tokens(agent, response)

    return {
        "prompt": question,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


# ---------------------------------------------------------------------------
# Past-self consultation — the new soul flow
#
# Replaces timer-driven flow with per-turn-cadence past-self consultation.
# Snapshots written by psyche._write_molt_snapshot are the substrate; this
# layer reads them, runs M = 1+K consultations in parallel (1 insights
# against the current self, K random past-snapshot consultations), bundles
# the voices into a single synthetic (assistant{tool_call}, user{tool_result})
# pair, and hands it off to base_agent's tc_inbox with replace_in_history=True.
#
# The single-slot invariant in chat history is enforced at drain time: any
# prior soul.flow pair already in ChatInterface.entries is removed before
# the new pair is appended. This keeps voices "drifting" naturally from
# tail toward prefix as subsequent turns extend history past them, which
# preserves provider prompt cache through the voice's lifetime.
# ---------------------------------------------------------------------------


def _load_snapshot_interface(path):
    """Load a snapshot file written by psyche._write_molt_snapshot and
    return its frozen ChatInterface, or None on any failure.

    Schema check: payload must carry an integer ``schema_version`` and
    a list ``interface`` of entry dicts compatible with
    ``ChatInterface.from_dict``. Any failure (missing file, bad JSON,
    schema mismatch, malformed entries) returns None — the caller skips
    that snapshot and proceeds with whatever else loaded.
    """
    import json
    from pathlib import Path
    from ..llm.interface import ChatInterface

    try:
        p = Path(path)
        if not p.is_file():
            return None
        raw = p.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("schema_version"), int):
            return None
        entries = payload.get("interface")
        if not isinstance(entries, list):
            return None
        return ChatInterface.from_dict(entries)
    except Exception:
        return None


def _fit_interface_to_window(iface, target_tokens: int):
    """Tail-trim a ChatInterface to fit within ``target_tokens`` while
    preserving tool-call/tool-result pairing invariants.

    Strategy: walk entries from the end backward, accumulating until the
    next addition would exceed ``target_tokens``. The resulting "kept
    suffix" must start on a clean boundary — never with a user{tool_result}
    whose matching assistant{tool_call} has been dropped. If the natural
    cutoff falls mid-pair, walk one more step backward (or forward, if
    that would yield zero entries) until a clean start is reached.

    System entries (typically index 0) are *always* preserved at the head
    of the kept set when present, since they carry the frozen system
    prompt that the snapshot represents. They count toward the budget.

    Returns a fresh ChatInterface containing only the kept entries.
    """
    from ..llm.interface import ChatInterface, ToolCallBlock, ToolResultBlock

    if target_tokens <= 0:
        return ChatInterface.from_dict([])

    entries = list(iface.entries)
    if not entries:
        return ChatInterface.from_dict([])

    # Already fits — return as-is (clone via to_dict round-trip so the
    # caller can mutate the trimmed copy without affecting the source).
    current = iface.estimate_context_tokens()
    if current <= target_tokens:
        return ChatInterface.from_dict(iface.to_dict())

    # Identify a leading system entry (preserve it).
    head_system = []
    body_start = 0
    if entries[0].role == "system":
        head_system = [entries[0]]
        body_start = 1

    # Walk body from tail backward to find the largest suffix that fits.
    # Build a dict-list of (head_system + suffix) and ask the live
    # ChatInterface for an accurate token count each time.
    head_dicts = [e.to_dict() for e in head_system]
    body = entries[body_start:]
    body_dicts = [e.to_dict() for e in body]

    kept_suffix_start = len(body)  # start with empty suffix
    for i in range(len(body) - 1, -1, -1):
        candidate_dicts = head_dicts + body_dicts[i:]
        probe = ChatInterface.from_dict(candidate_dicts)
        if probe.estimate_context_tokens() > target_tokens:
            break
        kept_suffix_start = i

    # Adjust kept_suffix_start to land on a clean boundary: if the entry
    # at kept_suffix_start is a user-role with only ToolResultBlocks,
    # find the matching tool_call earlier in the body and either include
    # the call (drop kept_suffix_start by 1) or drop the result entry
    # (raise kept_suffix_start by 1). The simpler safe move is the
    # latter — drop the orphaned tool_result entry from the head of the
    # suffix.
    while kept_suffix_start < len(body):
        entry = body[kept_suffix_start]
        if entry.role != "user":
            break
        # If every block in this user entry is a ToolResultBlock, it's a
        # candidate orphan. Check whether its tool_call ids appear in the
        # already-kept suffix (they can only appear earlier than us — by
        # construction the matching call would be just before, so it's
        # been excluded by the cutoff).
        if all(isinstance(b, ToolResultBlock) for b in entry.content):
            # Orphan tool_result with no preceding tool_call in the kept
            # suffix. Drop it and keep walking forward in case the next
            # entry is also orphan.
            kept_suffix_start += 1
            continue
        break

    final_body = body[kept_suffix_start:]
    if not final_body and not head_system:
        # Nothing fits at all — return an empty interface rather than
        # something malformed. Caller treats empty interface as "skip
        # this consultation."
        return ChatInterface.from_dict([])

    final_dicts = head_dicts + [e.to_dict() for e in final_body]
    return ChatInterface.from_dict(final_dicts)


def _run_consultation(agent, iface, source: str) -> dict | None:
    """Run one consultation against a seeded ChatInterface.

    Creates a one-shot tools-less session over the given interface,
    sends a minimal placeholder cue, and returns
    ``{"voice": str, "thinking": list, "source": source}`` or None on
    any failure (load issue, timeout, empty response).

    The cue prompt is intentionally minimal at this stage — the real cue
    that surfaces the agent's recent context to past selves is a
    separate prompt-engineering ticket. For the mechanical scaffold,
    "What do you notice? Speak freely." is enough to get a substantive
    response.

    Window-fit happens here: tail-trim to 0.8 × the consulting model's
    context window. The consulting model is the same model that runs the
    main agent chat (``agent.service``), so the budget is read from the
    main chat's ``context_window()`` (or ``config.context_limit`` if the
    user pinned a smaller cap). Same pattern as meta_block.py and
    session.py use everywhere else in the kernel.
    """
    if iface is None or not iface.entries:
        return None

    # Read the consulting model's context window the same way the rest of
    # the kernel does. If the agent hasn't built its main chat yet (early
    # boot), conservatively assume 200k — every modern provider supports
    # at least that and it leaves room for the system prompt + cue.
    window = None
    if getattr(agent, "_chat", None) is not None:
        try:
            window = agent._chat.context_window()
        except Exception:
            window = None
    if window is None:
        window = int(getattr(agent._config, "context_limit", None) or 200_000)
    target = max(1, int(window * 0.8))
    fitted = _fit_interface_to_window(iface, target)
    if not fitted.entries:
        return None

    system_prompt = _build_soul_system_prompt(agent)
    system_prompt += "\n\nYou have no tools. Respond with plain text only. Never output tool calls or XML tags."

    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=fitted,
        )
    except Exception as e:
        try:
            agent._log("consultation_session_failed", source=source, error=str(e)[:200])
        except Exception:
            pass
        return None

    cue = "What do you notice? Speak freely."
    response = _send_with_timeout(agent, session, cue)
    if not response or not response.text:
        return None

    try:
        _write_soul_tokens(agent, response)
    except Exception:
        pass

    return {
        "source": source,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


def _list_snapshot_paths(agent):
    """Return a list of pathlib.Path entries for all snapshot files in
    <workdir>/history/snapshots/, or [] if the directory does not exist."""
    from pathlib import Path
    snapshots_dir = agent._working_dir / "history" / "snapshots"
    if not snapshots_dir.is_dir():
        return []
    try:
        return sorted(snapshots_dir.glob("snapshot_*.json"))
    except Exception:
        return []


def _clone_current_chat_for_insights(agent):
    """Build a tools-stripped ChatInterface clone of the agent's current
    chat. Same pattern as soul_inquiry: keep TextBlock + ThinkingBlock
    only, skip ToolCallBlock + ToolResultBlock entirely (the consultation
    session is tools-less).
    """
    from ..llm.interface import ChatInterface, TextBlock, ThinkingBlock

    cloned = ChatInterface()
    if agent._chat is None:
        return cloned

    for entry in agent._chat.interface.entries:
        if entry.role == "system":
            continue
        stripped = []
        for block in entry.content:
            if isinstance(block, (TextBlock, ThinkingBlock)):
                stripped.append(block)
        if not stripped:
            continue
        if entry.role == "assistant":
            cloned.add_assistant_message(stripped)
        else:
            try:
                cloned.add_user_blocks(stripped)
            except Exception:
                # Pending-tool-call guard tripped on the clone — skip
                # this entry rather than crash. Should be very rare since
                # we already stripped tool blocks.
                continue

    return cloned


def _run_consultation_batch(agent) -> list[dict]:
    """Run one full consultation fire: 1 insights + K past-snapshot
    consultations in parallel. Returns the list of surviving voices
    (failed/timed-out consultations are filtered out).
    """
    import random
    import threading

    K = max(0, int(getattr(agent._config, "consultation_past_count", 2)))

    # Build work items.
    work: list[tuple[str, "ChatInterface"]] = []
    insights_iface = _clone_current_chat_for_insights(agent)
    if insights_iface.entries:
        work.append(("insights", insights_iface))

    # Sample K snapshot paths; load each.
    paths = _list_snapshot_paths(agent)
    if paths and K > 0:
        sampled = random.sample(paths, min(K, len(paths)))
        for path in sampled:
            iface = _load_snapshot_interface(path)
            if iface is None or not iface.entries:
                try:
                    agent._log("consultation_load_failed", path=str(path))
                except Exception:
                    pass
                continue
            # source label encodes molt_count + ts when parseable from filename
            source = f"snapshot:{path.stem}"
            work.append((source, iface))

    if not work:
        return []

    # Run all consultations in parallel daemon threads with a barrier.
    results: list[dict | None] = [None] * len(work)

    def worker(idx: int, source: str, iface) -> None:
        try:
            results[idx] = _run_consultation(agent, iface, source)
        except Exception as e:
            try:
                agent._log("consultation_thread_error",
                           source=source, error=str(e)[:200])
            except Exception:
                pass
            results[idx] = None

    threads: list[threading.Thread] = []
    for idx, (source, iface) in enumerate(work):
        t = threading.Thread(
            target=worker, args=(idx, source, iface),
            daemon=True,
            name=f"consult-w-{idx}-{source[:20]}",
        )
        threads.append(t)
        t.start()

    timeout = float(getattr(agent._config, "retry_timeout", 300.0)) * 2.0
    for t in threads:
        t.join(timeout=timeout)

    voices = [r for r in results if r is not None and r.get("voice")]
    return voices


def build_consultation_pair(agent, voices: list[dict]):
    """Build a synthetic (ToolCallBlock, ToolResultBlock) pair carrying
    the bundled consultation voices. The result content includes an
    appendix_note framing the voices as advisory and ephemeral.
    """
    import secrets
    import time
    from ..llm.interface import ToolCallBlock, ToolResultBlock
    from ..i18n import t as _t

    tc_id = f"tc_{int(time.time())}_{secrets.token_hex(2)}"
    call = ToolCallBlock(id=tc_id, name="soul", args={"action": "flow"})

    # Strip the thinking block from the wire payload — it inflates tokens
    # without adding readable signal at the consumption site (the agent
    # main turn). Keep the source label and the voice text only.
    rendered_voices = [
        {"source": v["source"], "voice": v["voice"]}
        for v in voices
        if v.get("voice")
    ]
    payload = {
        "appendix_note": _t(agent._config.language, "soul.appendix_note"),
        "voices": rendered_voices,
    }
    result = ToolResultBlock(id=tc_id, name="soul", content=payload)
    return call, result
