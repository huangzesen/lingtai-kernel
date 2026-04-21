"""Convert chat_history.jsonl entries into markdown — full fidelity, no truncation."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _ts(timestamp: float) -> str:
    """Format a UNIX timestamp as ISO 8601 UTC string."""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_BANNER = (
    "# Chat History (serialized)\n\n"
    "This is your own conversation history, replayed into your system prompt. "
    "\"Input\" turns are things you received (tool results, forwarded emails, "
    "system notifications — never a human speaking directly). \"You\" turns "
    "are your own past outputs (your diary + structured tool calls you issued)."
)


def _indent(body: str, spaces: int = 4) -> str:
    """Indent every line of body by `spaces` spaces. Empty lines stay empty."""
    pad = " " * spaces
    return "\n".join(pad + line if line else "" for line in body.splitlines())


def _render_content(content: list[dict]) -> str:
    """Render a content block list into markdown text.

    Thinking blocks are dropped — they were the agent's private scratchpad
    for that turn, not durable history. Keeping them both bloats context.md
    and gives the LLM its own past thinking as apparent ground truth,
    encouraging imitation of stale reasoning threads.

    Tool calls and results are rendered as indented narrative records
    marked with ◆. Earlier formats (`[tool_use: name(...)]`, fenced JSON
    blocks) mimicked tool-call syntaxes that models are trained to emit,
    causing the LLM to produce fake tool calls inside its text output
    instead of issuing real structured tool calls. The ◆ + past-tense
    narrative shape is not a tool-call protocol in any provider's
    training data, so the model has nothing to imitate.
    """
    parts: list[str] = []
    for block in content:
        btype = block.get("type", "")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            continue
        elif btype == "tool_call":
            name = block.get("name", "")
            args = block.get("args", {})
            if not args:
                parts.append(f"◆ called tool `{name}`.")
            else:
                args_body = json.dumps(args, ensure_ascii=False, indent=2)
                parts.append(
                    f"◆ called tool `{name}` with arguments:\n\n"
                    + _indent(args_body)
                )
        elif btype == "tool_result":
            name = block.get("name", "")
            result_content = block.get("content", "")
            if not isinstance(result_content, str):
                result_content = json.dumps(result_content, ensure_ascii=False, indent=2)
            parts.append(
                f"◆ tool `{name}` returned:\n\n"
                + _indent(result_content)
            )
        else:
            # Unknown block type — include raw JSON so nothing is silently dropped
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(parts)


def serialize_context_md(entries: list[dict]) -> str:
    """Convert a list of chat_history.jsonl entries into a markdown string.

    Rules:
    - molt_boundary entries are skipped (metadata, not conversation).
    - role: system entries are skipped. They are audit records of the
      assembled system prompt sent to the LLM on each turn; including them
      would nest the system prompt recursively inside the `context` section
      (which is itself part of the system prompt), ballooning the prompt
      by ~28KB per turn.
    - role: user entries render as `### Input [ts]` — these are inbound
      notifications (tool results, forwarded emails, system messages).
      They are never a human speaking; humans only reach the agent via
      mail. The `user`/`assistant` vocabulary comes from the API schema
      and contradicts the agent's own model of its world, so we rename
      at serialization time.
    - role: assistant entries render as `### You [ts]` — these are the
      agent's own past outputs, addressed back in second person so it
      reads as autobiography rather than some generic "assistant" turn.
    - Tool calls and results are ◆-prefixed past-tense narrative (see
      _render_content).
    - Timestamps are rendered as ISO 8601 UTC.
    """
    if not entries:
        return ""

    sections: list[str] = [_BANNER]

    for entry in entries:
        entry_type = entry.get("type")
        if entry_type == "molt_boundary":
            continue

        role = entry.get("role", "")
        if role == "system":
            continue

        timestamp = entry.get("timestamp", 0.0)
        ts_str = _ts(timestamp)

        if role == "user":
            label = "Input"
        elif role == "assistant":
            label = "You"
        else:
            label = role  # unknown role — render literally
        header = f"### {label} [{ts_str}]"

        if role in ("user", "assistant"):
            content = entry.get("content", [])
            body = _render_content(content)
            sections.append(f"{header}\n{body}")
        else:
            sections.append(f"{header}\n{json.dumps(entry, ensure_ascii=False)}")

    return "\n\n".join(sections)
