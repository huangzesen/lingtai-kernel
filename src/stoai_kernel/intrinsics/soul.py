"""Soul intrinsic — the agent's inner voice.

Actions:
    on  — activate the soul (requires prompt, optional delay)
    off — deactivate the soul

When active, the soul whispers after the agent goes idle:
it clones the agent's full conversation into a temporary session,
sends the agent-authored prompt, and injects the response as
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
                "After you go idle, the clone receives your prompt "
                "and its response is delivered to you as [inner voice]. "
                "You must provide 'prompt' — the instruction you want "
                "your clone to respond to. "
                "off: silence your inner voice."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "The message sent to your clone. Required for 'on'. "
                "This is fully under your control — write what you want "
                "your inner voice to reflect on, question, or remind you of. "
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
    "Your inner voice — a clone of your conversation that whispers back "
    "after you go idle. The clone sees your full history and system prompt "
    "but has no tools. You control what it reflects on via 'prompt'. "
    "Use 'on' with a prompt to activate, 'off' to silence. "
    "The soul keeps you going without external push."
)

_MAX_DELAY = 3600.0
_MIN_DELAY = 1.0
_DEFAULT_DELAY = 120.0


def handle(agent, args: dict) -> dict:
    """Handle soul tool — on/off toggle with agent-authored prompt."""
    action = args.get("action", "")

    if action == "on":
        prompt = args.get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"error": "prompt is required — tell your soul what to reflect on."}

        delay = args.get("delay", _DEFAULT_DELAY)
        delay = float(delay)
        if delay < _MIN_DELAY:
            return {"error": f"delay must be >= {_MIN_DELAY} seconds."}
        delay = min(delay, _MAX_DELAY)

        agent._soul_active = True
        agent._soul_delay = delay
        agent._soul_prompt = prompt.strip()
        agent._log("soul_on", delay=delay, prompt=agent._soul_prompt[:200])
        return {"status": "ok", "active": True, "delay": delay}

    elif action == "off":
        agent._soul_active = False
        agent._log("soul_off")
        return {"status": "ok", "active": False}

    else:
        return {"error": f"Unknown soul action: {action}. Use on or off."}
