"""Soul intrinsic — the agent's inner voice.

Two modes:
    flow    — text completion from serialized thinking+diary. Stateless, automatic.
    inquiry — mirror session with cloned conversation. Sync, on-demand.

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
        # Persist to .agent.json
        agent._workdir.write_manifest(agent._build_manifest())
        return {"status": "ok", "delay": delay}

    else:
        return {"error": f"Unknown soul action: {action}. Use inquiry or delay."}


def _send_with_timeout(agent, session, content: str):
    """Send with timeout. Returns response or None."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    timeout = agent._config.retry_timeout

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(session.send, content)
            return future.result(timeout=timeout)
    except FuturesTimeout:
        agent._log("soul_whisper_error", error=f"LLM call timed out after {timeout}s")
        return None
    except Exception as e:
        agent._log("soul_whisper_error", error=str(e)[:200])
        return None


def soul_flow(agent) -> dict | None:
    """Flow mode — text completion from serialized stream of consciousness.

    Serializes all thinking + diary text into one prompt, sends to a fresh
    session with no history. Stateless — the soul starts blank every time.
    """
    from ..llm.interface import TextBlock, ThinkingBlock

    # Serialize all thinking and diary text
    parts: list[str] = []
    if agent._chat is not None:
        for entry in agent._chat.interface.entries:
            if entry.role == "system":
                continue
            for block in entry.content:
                if isinstance(block, (ThinkingBlock, TextBlock)) and block.text:
                    parts.append(block.text)

    if parts:
        content = "\n\n---\n\n".join(parts)
    else:
        # No conversation yet: use static prompt
        from ..prompt import get_soul_prompt
        template = get_soul_prompt(agent._config.language)
        content = template.format(seconds=int(agent._soul_delay))

    # Fresh session, no history, no system prompt, no tools
    try:
        session = agent.service.create_session(
            system_prompt="",
            tools=None,
            model=agent._config.model or agent.service.model,
            thinking="high",
            tracked=False,
        )
    except Exception as e:
        agent._log("soul_whisper_error", error=str(e)[:200])
        return None

    response = _send_with_timeout(agent, session, content)
    if not response or not response.text:
        return None

    return {
        "prompt": content[:500],
        "voice": response.text,
        "thinking": response.thoughts or [],
    }


def soul_inquiry(agent, question: str) -> dict | None:
    """Inquiry mode — mirror session with cloned conversation.

    Clones the agent's conversation (thinking + diary only, no tool
    calls/results), sends the question. The soul has full context to
    give a meaningful answer.
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

    # Mirror session: no system prompt, no tools, cloned history
    try:
        session = agent.service.create_session(
            system_prompt="",
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
