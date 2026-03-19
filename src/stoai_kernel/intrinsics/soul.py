"""Soul intrinsic — the agent's inner voice.

Actions:
    on  — activate the soul (requires inquiry, optional delay)
    off — deactivate the soul

When active, the soul whispers after the agent goes idle:
it clones the agent's full conversation into a temporary session,
sends the agent's self-inquiry, and injects the response as
[inner voice] into the agent's inbox. The clone sees everything
the agent has seen — same system prompt, same history — but has
no tools. One message in, one message out, then discarded.
"""
from __future__ import annotations

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["on", "off"],
            "description": (
                "on: activate your inner voice. "
                "A clone of your full conversation is created — "
                "same system prompt, same history, no tools. "
                "After you go idle, the clone receives your inquiry "
                "and its response is delivered to you as [inner voice]. "
                "You must provide 'inquiry' — your self-directed question, "
                "the introspection you want your inner voice to reflect on. "
                "off: silence your inner voice."
            ),
        },
        "inquiry": {
            "type": "string",
            "description": (
                "Your self-inquiry — introspection directed at yourself. "
                "Required for 'on'. Not a prompt to someone else: "
                "this is you asking yourself a question. "
                "What do you want to reflect on, question, or reconsider? "
                "The clone sees your entire conversation and system prompt."
            ),
        },
        "delay": {
            "type": "number",
            "description": (
                "Seconds to wait after going idle before the soul whispers. "
                "Default 120. Min 1, max 3600. "
                "Short delay = restless, long delay = patient."
            ),
        },
    },
    "required": ["action"],
}

DESCRIPTION = (
    "Your inner voice — a second you that whispers back after you go idle. "
    "A clone of your full conversation is created: same system prompt, "
    "same history, no tools. You control what it reflects on via 'inquiry' — "
    "a self-directed question, introspection, not a prompt to someone else. "
    "Use 'on' with an inquiry to activate, 'off' to silence. "
    "The soul keeps you going without external push."
)

_MAX_DELAY = 3600.0
_MIN_DELAY = 1.0
_DEFAULT_DELAY = 120.0


def handle(agent, args: dict) -> dict:
    """Handle soul tool — on/off toggle with agent self-inquiry."""
    action = args.get("action", "")

    if action == "on":
        inquiry = args.get("inquiry", "")
        if not isinstance(inquiry, str) or not inquiry.strip():
            return {"error": "inquiry is required — what do you want to reflect on?"}

        delay = args.get("delay", _DEFAULT_DELAY)
        try:
            delay = float(delay)
        except (TypeError, ValueError):
            return {"error": "delay must be a number."}
        if delay < _MIN_DELAY:
            return {"error": f"delay must be >= {_MIN_DELAY} seconds."}
        delay = min(delay, _MAX_DELAY)

        agent._soul_active = True
        agent._soul_delay = delay
        agent._soul_prompt = inquiry.strip()
        agent._log("soul_on", delay=delay, inquiry=agent._soul_prompt)
        return {"status": "ok", "active": True, "delay": delay}

    elif action == "off":
        agent._soul_active = False
        agent._log("soul_off")
        return {"status": "ok", "active": False}

    else:
        return {"error": f"Unknown soul action: {action}. Use on or off."}


def whisper(agent) -> str | None:
    """Clone the agent's conversation and send the agent's self-inquiry.

    Returns the inner voice text, or None if there's nothing to reflect on.

    Thread safety: called from the soul Timer thread while the agent is
    SLEEPING (blocked in inbox.get()), so the agent thread is not mutating
    the interface.  The cloned interface is a deep copy via serialization,
    so the subsequent create_session/send touches no shared state.
    """
    from ..llm.interface import ChatInterface

    if agent._chat is None:
        return None

    iface = agent._chat.interface
    if not iface.conversation_entries():
        return None

    # Deep-copy the interface (safe: agent thread is blocked in inbox.get())
    cloned = ChatInterface.from_dict(iface.to_dict())

    # Create a temporary session: same system prompt, no tools, cloned history
    system_prompt = agent._build_system_prompt()
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
        response = session.send(
            f"[Agent inquiry] {agent._soul_prompt}\n\n[Be brief, you are the agent, agent is you, answer the inquiry as if answering to yourself. Don't address the agent as peer, you are addressing yourself. Answer in the same language as the inquiry. You don't have tools, do not attempt tool calls.]"
        )
    except Exception:
        return None

    return response.text or None
