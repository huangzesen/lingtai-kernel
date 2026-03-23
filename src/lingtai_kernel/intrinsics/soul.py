"""Soul intrinsic — the agent's inner voice.

Actions:
    inquiry — one-shot self-directed question, fires once on next idle
    delay   — adjust the idle delay before the soul whispers

soul_delay controls flow timing. Very large delay (> vigil) = effectively off.
Inquiry works regardless of delay — it fires once on the next idle.
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

        # Sync: do the whisper right now, return the answer
        agent._soul_prompt = inquiry.strip()
        agent._soul_oneshot = True
        result = whisper(agent)
        agent._soul_oneshot = False
        agent._soul_prompt = ""

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
        # Persist to .agent.json
        agent._workdir.write_manifest(agent._build_manifest())
        return {"status": "ok", "delay": delay}

    else:
        return {"error": f"Unknown soul action: {action}. Use inquiry or delay."}


def whisper(agent) -> dict | None:
    """Clone the agent's conversation and reflect.

    Continuous mode: free reflection. Inquiry mode: answer the specific question.
    Returns {"prompt": str, "voice": str, "thinking": list[str]} or None.

    Thread safety: called from the soul Timer thread while the agent is
    IDLE (blocked in inbox.get()), so the agent thread is not mutating
    the interface.  The cloned interface is a deep copy via serialization,
    so the subsequent create_session/send touches no shared state.
    """
    from ..llm.interface import ChatInterface

    # Build a stripped-down interface for reflection:
    # - No system entries (no system prompt, no tool schemas)
    # - No ToolCallBlocks or ToolResultBlocks
    # - Keep only TextBlocks and ThinkingBlocks
    # - Strip the last assistant turn — the soul prompt IS that last thought
    from ..llm.interface import TextBlock, ToolCallBlock, ToolResultBlock, ThinkingBlock
    lang = agent._config.language

    # Collect stripped entries
    stripped_entries: list[tuple[str, list]] = []  # (role, blocks)

    if agent._chat is not None:
        iface = agent._chat.interface
        for entry in iface.entries:
            if entry.role == "system":
                continue
            stripped: list = []
            for block in entry.content:
                if isinstance(block, (TextBlock, ThinkingBlock)):
                    stripped.append(block)
            if stripped:
                stripped_entries.append((entry.role, stripped))

    # Extract the last assistant text as the soul prompt (the agent's last thought).
    # Clone gets everything except that last turn.
    last_diary = ""
    if stripped_entries and stripped_entries[-1][0] == "assistant":
        last_role, last_blocks = stripped_entries.pop()
        for block in last_blocks:
            if isinstance(block, TextBlock) and block.text:
                last_diary = block.text
                break

    cloned = ChatInterface()
    for role, blocks in stripped_entries:
        if role == "assistant":
            cloned.add_assistant_message(blocks)
        else:
            cloned.add_user_blocks(blocks)

    # Build soul prompt: use last diary if available, else static prompt
    if agent._soul_prompt:
        raw = agent._soul_prompt
    elif last_diary:
        raw = last_diary
    else:
        from ..prompt import get_soul_prompt
        template = get_soul_prompt(lang)
        raw = template.format(seconds=int(agent._soul_delay))
    content = raw

    # Create a temporary session: same system prompt, no tools, cloned history
    system_prompt = agent._build_system_prompt()
    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            interface=cloned,
        )
        response = session.send(content)
    except Exception:
        return None

    if not response.text:
        return None

    return {
        "prompt": content,
        "voice": response.text,
        "thinking": response.thoughts or [],
    }
