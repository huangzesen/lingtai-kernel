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
                "enum": ["inquiry", "delay"],
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
    """Handle soul tool — inquiry/delay."""
    action = args.get("action", "")

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
        return {"error": f"Unknown soul action: {action}. Use inquiry or delay."}


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
        append_token_entry(
            ledger_path,
            input=u.input_tokens, output=u.output_tokens,
            thinking=u.thinking_tokens, cached=u.cached_tokens,
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
