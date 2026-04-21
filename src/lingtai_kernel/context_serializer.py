"""Convert chat_history.jsonl entries into markdown — full fidelity, no truncation."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _ts(timestamp: float) -> str:
    """Format a UNIX timestamp as ISO 8601 UTC string."""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_content(content: list[dict]) -> str:
    """Render a content block list into markdown text."""
    parts: list[str] = []
    for block in content:
        btype = block.get("type", "")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            parts.append(f"<thinking>\n{block.get('text', '')}\n</thinking>")
        elif btype == "tool_call":
            name = block.get("name", "")
            args = block.get("args", {})
            parts.append(f"[tool_use: {name}({json.dumps(args, ensure_ascii=False)})]")
        elif btype == "tool_result":
            name = block.get("name", "")
            result_content = block.get("content", "")
            parts.append(f"[tool_result({name}): {result_content}]")
        else:
            # Unknown block type — include raw JSON so nothing is silently dropped
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(parts)


def serialize_context_md(entries: list[dict]) -> str:
    """Convert a list of chat_history.jsonl entries into a markdown string.

    Rules:
    - molt_boundary entries are skipped (metadata, not conversation).
    - All other entries (system, user, assistant) are rendered verbatim.
    - A '---' separator is inserted before each system entry that is NOT the
      very first rendered entry (i.e. between turns).
    - Thinking blocks are wrapped in <thinking>…</thinking>.
    - Tool calls and results are included in full with no truncation.
    - Timestamps are rendered as ISO 8601 UTC.
    """
    if not entries:
        return ""

    sections: list[str] = []

    for entry in entries:
        # Skip molt_boundary markers — they are metadata, not conversation.
        entry_type = entry.get("type")
        if entry_type == "molt_boundary":
            continue

        role = entry.get("role", "")
        timestamp = entry.get("timestamp", 0.0)
        ts_str = _ts(timestamp)
        header = f"### {role} [{ts_str}]"

        if role == "system":
            # Insert turn separator before every system entry except the first
            # rendered one.
            if sections:
                sections.append("---")
            system_text = entry.get("system", "")
            sections.append(f"{header}\n{system_text}")
        elif role in ("user", "assistant"):
            content = entry.get("content", [])
            body = _render_content(content)
            sections.append(f"{header}\n{body}")
        else:
            # Unknown role — render raw to preserve full fidelity
            sections.append(f"{header}\n{json.dumps(entry, ensure_ascii=False)}")

    return "\n\n".join(sections)
