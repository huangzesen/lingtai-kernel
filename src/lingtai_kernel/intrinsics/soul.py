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


# Backward compat — evaluated at import with English defaults
SCHEMA = get_schema("en")
DESCRIPTION = get_description("en")

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
            agent._persist_soul_entry(result)
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


def _get_principle(agent) -> str:
    """Extract the principle section from the agent's prompt manager."""
    content = agent._prompt_manager.read_section("principle")
    return content or ""


def _collect_new_diary(agent) -> str:
    """Collect diary text (assistant entries) since the last soul whisper.

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
            if isinstance(block, TextBlock) and block.text:
                parts.append(block.text)

    # Update cursor to current end
    agent._soul_cursor = len(entries)

    return "\n\n".join(parts)


def _ensure_soul_session(agent):
    """Get or create the persistent soul mirror session."""
    if getattr(agent, "_soul_session", None) is not None:
        return agent._soul_session

    principle = _get_principle(agent)

    try:
        agent._soul_session = agent.service.create_session(
            system_prompt=principle,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
        )
    except Exception as e:
        agent._log("soul_whisper_error", error=f"Failed to create soul session: {e}")
        agent._soul_session = None

    return agent._soul_session


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
        # No new diary — use static nudge
        from ..prompt import get_soul_prompt
        diary = get_soul_prompt(agent._config.language).format(
            seconds=int(agent._soul_delay)
        )

    session = _ensure_soul_session(agent)
    if session is None:
        return None

    response = _send_with_timeout(agent, session, diary)
    if not response or not response.text:
        return None

    return {
        "prompt": diary[:500],
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


def reset_soul_session(agent) -> None:
    """Reset the persistent soul session (called on molt)."""
    agent._soul_session = None
    agent._soul_cursor = 0


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

    principle = _get_principle(agent)

    try:
        session = agent.service.create_session(
            system_prompt=principle,
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

    return {
        "prompt": question,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }
