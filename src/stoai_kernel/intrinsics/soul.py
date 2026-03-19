"""Soul intrinsic — the agent's inner voice.

Actions:
    inquiry — one-shot self-directed question, fires once on next idle
    delay   — adjust the idle delay before the soul whispers

Flow mode (continuous free reflection) is enabled at agent creation
via config.flow and cannot be toggled at runtime.
Inquiry works regardless of flow — it fires once on the next idle.
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



def _detect_lang(text: str) -> str:
    """Detect the dominant language of a text sample. Returns a lang key or 'en'."""
    from collections import Counter
    import re

    if not text.strip():
        return "en"

    counts = Counter()
    for ch in text:
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
            counts["ru"] += 1
        elif 0x0041 <= cp <= 0x024F:
            counts["latin"] += 1

    if not counts:
        return "en"

    top = counts.most_common(1)[0][0]

    if top in _PONDER:
        return top

    if top == "latin":
        lower = text.lower()
        if re.search(r"\b(el|la|los|las|es|está|pero|porque|también)\b", lower):
            return "es"
        if re.search(r"\b(le|la|les|est|mais|aussi|avec|dans|pour)\b", lower):
            return "fr"
        if re.search(r"\b(der|die|das|ist|aber|auch|und|für|mit)\b", lower):
            return "de"
        if re.search(r"\b(o|a|os|as|é|mas|também|com|para)\b", lower):
            return "pt"
        if re.search(r"\b(il|la|è|ma|anche|con|per|che|questo)\b", lower):
            return "it"
        if re.search(r"\b(bir|ve|bu|için|ile|ama|da|de)\b", lower):
            return "tr"
        if re.search(r"\b(và|của|là|không|được|này|có|cho)\b", lower):
            return "vi"
        if re.search(r"\b(dan|yang|ini|untuk|dengan|dari|ada)\b", lower):
            return "id"
        if re.search(r"\b(jest|nie|się|ale|też|dla|czy)\b", lower):
            return "pl"
        if re.search(r"\b(het|een|van|en|is|maar|ook|met)\b", lower):
            return "nl"
        if re.search(r"\b(och|är|att|men|för|med|det|som)\b", lower):
            return "sv"
        return "en"

    return "en"


def _detect_flow_message(iface) -> str:
    """Pick the ponder word matching the conversation's majority language."""
    texts = []
    for entry in iface.conversation_entries()[-10:]:
        for block in entry.content:
            if hasattr(block, "text") and block.text:
                texts.append(block.text)
    sample = " ".join(texts)[:3000]

    lang = _detect_lang(sample)
    return _PONDER.get(lang, _PONDER_FALLBACK)




SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["inquiry", "delay"],
            "description": (
                "inquiry: one-shot self-directed question — "
                "fires once on next idle. Requires 'inquiry' parameter. "
                "delay: adjust how long to wait after going idle "
                "before the soul whispers. Requires 'delay' parameter."
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
                "Min 1. Short delay = restless, long delay = patient. "
                "Required for action='delay'."
            ),
        },
    },
    "required": ["action"],
}

DESCRIPTION = (
    "Your inner voice — a second you that whispers back after you go idle. "
    "A clone of your full conversation is created: same system prompt, "
    "same history, no tools. "
    "Flow mode is determined at birth — you cannot toggle it. "
    "'inquiry' fires a one-shot self-directed question on next idle. "
    "'delay' adjusts the idle wait time. "
    "The soul keeps you going without external push."
)

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


def whisper(agent) -> str | None:
    """Clone the agent's conversation and reflect.

    Flow mode: free reflection. Inquiry mode: answer the specific question.
    Returns the inner voice text, or None if there's nothing to reflect on.

    Thread safety: called from the soul Timer thread while the agent is
    IDLE (blocked in inbox.get()), so the agent thread is not mutating
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

    # Build content — same as what the agent would see
    if agent._soul_prompt:
        content = agent._soul_prompt
    else:
        content = _detect_flow_message(iface)

    # Prepend timestamp — same pattern as _handle_request
    from datetime import datetime, timezone
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = f"[Current time: {current_time}]\n\n{content}"

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
        response = session.send(content)
    except Exception:
        return None

    return response.text or None
