"""Soul intrinsic — the agent's inner voice.

Actions:
    inquiry — one-shot self-directed question, fires once on next idle
    delay   — adjust the idle delay before the soul whispers

Flow mode (continuous free reflection) is enabled at agent creation
via config.flow and cannot be toggled at runtime.
Inquiry works regardless of flow — it fires once on the next idle.
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

        agent._soul_prompt = inquiry.strip()
        agent._soul_oneshot = True
        agent._log("soul_inquiry", delay=agent._soul_delay, inquiry=agent._soul_prompt[:200])
        return {"status": "ok", "mode": "inquiry", "delay": agent._soul_delay}

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
        return {"status": "ok", "delay": delay}

    else:
        return {"error": f"Unknown soul action: {action}. Use inquiry or delay."}


def whisper(agent) -> dict | None:
    """Clone the agent's conversation and reflect.

    Flow mode: free reflection. Inquiry mode: answer the specific question.
    Returns {"prompt": str, "voice": str, "thinking": list[str]} or None.

    Thread safety: called from the soul Timer thread while the agent is
    IDLE (blocked in inbox.get()), so the agent thread is not mutating
    the interface.  The cloned interface is a deep copy via serialization,
    so the subsequent create_session/send touches no shared state.
    """
    from ..llm.interface import ChatInterface
    from ..i18n import t

    if agent._chat is None:
        return None

    iface = agent._chat.interface
    if not iface.conversation_entries():
        return None

    # Deep-copy the interface (safe: agent thread is blocked in inbox.get())
    cloned = ChatInterface.from_dict(iface.to_dict())

    # Strip tool calls and tool results — the soul has no tools and must
    # produce only text.  Leaving tool examples in the history causes some
    # LLMs (e.g. MiniMax) to mimic the tool-call XML syntax in plain text.
    from ..llm.interface import ToolCallBlock, TextBlock
    for entry in cloned._entries:
        if entry.role == "system":
            continue
        stripped = [b for b in entry.content if not isinstance(b, ToolCallBlock)]
        entry.content = stripped or [TextBlock(text="(action taken)")]

    # Build content — no timestamp here; _handle_request adds it when the
    # agent processes the [inner voice] message from the inbox.
    if agent._soul_prompt:
        content = agent._soul_prompt
    else:
        delay = int(agent._soul_delay)
        content = t(agent._config.language, "soul.time_lapse", seconds=delay)

    # Build system prompt WITHOUT tool descriptions — the soul has no tools
    # and must not see tool inventories that tempt it to emit tool-call syntax.
    saved_tools = agent._prompt_manager.read_section("tools")
    agent._prompt_manager.delete_section("tools")
    system_prompt = agent._build_system_prompt()
    # Restore tools section for the agent's own prompt
    if saved_tools is not None:
        agent._prompt_manager.write_section("tools", saved_tools, protected=True)
    try:
        session = agent.service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
            provider=agent._config.provider,
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
