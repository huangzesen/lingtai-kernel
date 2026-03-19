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

# "Ponder." in ~20 languages — the flow mode trigger.
# Detect the majority language of the conversation and use the matching word.
# Fallback is English.
_PONDER = {
    "zh": "沉思。",
    "ja": "熟考せよ。",
    "ko": "숙고하라.",
    "en": "Ponder.",
    "es": "Reflexiona.",
    "fr": "Médite.",
    "de": "Besinne dich.",
    "pt": "Pondera.",
    "it": "Rifletti.",
    "ru": "Обдумай.",
    "ar": "تأمّل.",
    "hi": "विचार करो।",
    "th": "ครุ่นคิด",
    "vi": "Suy ngẫm.",
    "tr": "Düşün.",
    "pl": "Rozważ.",
    "nl": "Overdenk.",
    "sv": "Begrunda.",
    "uk": "Поміркуй.",
    "id": "Renungkan.",
}
_PONDER_FALLBACK = "Ponder."


def _detect_flow_message(iface) -> str:
    """Pick the ponder word matching the conversation's majority language."""
    from collections import Counter
    import re

    # Sample text from recent conversation entries
    texts = []
    for entry in iface.conversation_entries()[-10:]:
        for block in entry.content:
            if hasattr(block, "text") and block.text:
                texts.append(block.text)
    sample = " ".join(texts)[:3000]

    if not sample.strip():
        return _PONDER_FALLBACK

    # Simple heuristic: count Unicode script ranges
    counts = Counter()
    for ch in sample:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            counts["zh"] += 1
        elif 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            counts["ja"] += 1
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            counts["ko"] += 1
        elif 0x0600 <= cp <= 0x06FF:
            counts["ar"] += 1
        elif 0x0900 <= cp <= 0x097F:
            counts["hi"] += 1
        elif 0x0E00 <= cp <= 0x0E7F:
            counts["th"] += 1
        elif 0x0400 <= cp <= 0x04FF:
            # Could be Russian or Ukrainian — default to ru
            counts["ru"] += 1
        elif 0x0041 <= cp <= 0x024F:
            counts["latin"] += 1

    if not counts:
        return _PONDER_FALLBACK

    top = counts.most_common(1)[0][0]

    # For CJK scripts, we have direct mappings
    if top in _PONDER:
        return _PONDER[top]

    # Latin script — can't distinguish languages by script alone, use English
    if top == "latin":
        # Quick keyword detection for common Latin-script languages
        lower = sample.lower()
        if re.search(r"\b(el|la|los|las|es|está|pero|porque|también)\b", lower):
            return _PONDER["es"]
        if re.search(r"\b(le|la|les|est|mais|aussi|avec|dans|pour)\b", lower):
            return _PONDER["fr"]
        if re.search(r"\b(der|die|das|ist|aber|auch|und|für|mit)\b", lower):
            return _PONDER["de"]
        if re.search(r"\b(o|a|os|as|é|mas|também|com|para)\b", lower):
            return _PONDER["pt"]
        if re.search(r"\b(il|la|è|ma|anche|con|per|che|questo)\b", lower):
            return _PONDER["it"]
        if re.search(r"\b(bir|ve|bu|için|ile|ama|da|de)\b", lower):
            return _PONDER["tr"]
        if re.search(r"\b(và|của|là|không|được|này|có|cho)\b", lower):
            return _PONDER["vi"]
        if re.search(r"\b(dan|yang|ini|untuk|dengan|dari|ada)\b", lower):
            return _PONDER["id"]
        if re.search(r"\b(jest|nie|się|ale|też|dla|czy)\b", lower):
            return _PONDER["pl"]
        if re.search(r"\b(het|een|van|en|is|maar|ook|met)\b", lower):
            return _PONDER["nl"]
        if re.search(r"\b(och|är|att|men|för|med|det|som)\b", lower):
            return _PONDER["sv"]
        return _PONDER["en"]

    return _PONDER_FALLBACK


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
        # Flow mode — one word in the conversation's language
        message = _detect_flow_message(iface)

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
