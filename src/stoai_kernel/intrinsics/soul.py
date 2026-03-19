"""Soul intrinsic — the agent's inner voice.

Actions:
    on    — activate continuous free reflection (flow mode)
    off   — deactivate the soul
    inquiry — one-shot self-directed question, fires once on next idle

When active (flow), the soul whispers after the agent goes idle:
it clones the agent's full conversation into a temporary session
and injects the response as [inner voice] into the agent's inbox.
Inquiry mode does the same but with a specific question, once.
"""
from __future__ import annotations

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["on", "off", "inquiry"],
            "description": (
                "on: activate your inner voice in flow mode — "
                "continuous free reflection after each idle. "
                "off: silence your inner voice. "
                "inquiry: one-shot self-directed question — "
                "fires once on next idle, then deactivates. "
                "Requires 'inquiry' parameter."
            ),
        },
        "inquiry": {
            "type": "string",
            "description": (
                "Your self-inquiry — a question to yourself. "
                "Required for action='inquiry'. "
                "This is you asking yourself a question, not prompting someone else."
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
    "same history, no tools. "
    "'on' activates continuous free reflection (flow mode). "
    "'inquiry' fires a one-shot self-directed question, then deactivates. "
    "'off' silences it. "
    "The soul keeps you going without external push."
)

_MAX_DELAY = 3600.0
_MIN_DELAY = 1.0
_DEFAULT_DELAY = 120.0


def handle(agent, args: dict) -> dict:
    """Handle soul tool — on/off/inquiry."""
    action = args.get("action", "")

    if action == "on":
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
        agent._soul_prompt = ""  # flow mode — no fixed inquiry
        agent._soul_oneshot = False
        agent._log("soul_on", delay=delay, mode="flow")
        return {"status": "ok", "active": True, "mode": "flow", "delay": delay}

    elif action == "inquiry":
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
        agent._soul_oneshot = True
        agent._log("soul_inquiry", delay=delay, inquiry=agent._soul_prompt[:200])
        return {"status": "ok", "active": True, "mode": "inquiry", "delay": delay}

    elif action == "off":
        agent._soul_active = False
        agent._log("soul_off")
        return {"status": "ok", "active": False}

    else:
        return {"error": f"Unknown soul action: {action}. Use on, off, or inquiry."}


def whisper(agent) -> str | None:
    """Clone the agent's conversation and reflect.

    Flow mode: free reflection. Inquiry mode: answer the specific question.
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

    # Build the whisper message
    if agent._soul_prompt:
        # Inquiry mode
        message = (
            f"This is your own question to yourself: {agent._soul_prompt}\n\n"
            f"Be brief, you are addressing yourself. Answer in the same language as the inquiry."
        )
    else:
        # Flow mode
        message = "Briefly reflect yourself in same language."

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
        response = session.send(message)
    except Exception:
        return None

    return response.text or None
