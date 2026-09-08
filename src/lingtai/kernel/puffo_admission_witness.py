"""Puffo admission-receipt extraction and binding (pure unit).

Puffo exposes MCP tools to the LingTai agent.  When the agent calls a Puffo
tool, the result carries a one-time *admission receipt* marker so Puffo can
later recognise that the result durably reached the LingTai provider context.
This module is the pure, side-effect-free half of the "admission witness"
feature: it recognises the marker on a canonical :class:`ToolResultBlock` and
derives the stable ``(tool_call_id, raw_receipt)`` binding Puffo verifies.

The marker format is ``[puffo:model-visible-read:<raw>]`` where ``<raw>`` is a
non-empty token containing no whitespace and no ``]``.

Extraction is deterministic and fail-closed — any format mismatch, missing
marker, or ambiguity yields ``None`` (no fact).  In particular ``read_inbox``
results carry **no** marker, so this returns ``None`` for them: that is the
intended, explicit exclusion of the inbox read from the witness signal.

This module deliberately has no dependency on the turn machinery, the observer
bus, or the ACP adapter; the settle-point scan that consumes it lives in
``base_agent/turn.py``.
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm.interface import ToolResultBlock

# ``<raw>`` = one or more chars that are neither whitespace nor ``]``.
_MARKER_RE = re.compile(r"\[puffo:model-visible-read:([^\s\]]+)\]")

# The dedicated structured fields, in precedence order.  When ``content`` is a
# dict, the marker is read from the FIRST present of these fields ONLY; a marker
# appearing anywhere else in the dict is ignored (rule ①).
_RECEIPT_FIELDS = ("admission_receipt", "tool_result_admission")


def _last_marker_raw(text: str) -> str | None:
    """Return the raw of the LAST marker occurrence in ``text``, or ``None``.

    The genuine receipt is appended at the end of a plain-text body, so a
    quoted marker-like string earlier in the body never wins.
    """
    last = None
    for last in _MARKER_RE.finditer(text):
        pass
    return last.group(1) if last is not None else None


def extract_admission_receipt(block: "ToolResultBlock") -> str | None:
    """Return the raw admission receipt carried by ``block``, or ``None``.

    Deterministic, fail-closed rules (aligned with the MCP decode projection in
    ``services/mcp.py::_decode_tool_result``, which is what actually shapes a
    tool result before it reaches the kernel/interface):

    ① If ``block.content`` is a ``dict`` AND carries one of the dedicated
       receipt fields (``admission_receipt`` preferred, then
       ``tool_result_admission``): parse the marker from THAT field's value
       ONLY, ignoring any marker elsewhere in the dict.  The field value must
       be a string that is *exactly* a well-formed marker (a ``fullmatch`` on
       the stripped value); otherwise → ``None``.  Structured MCP results
       (``structured_content`` dicts, JSON-object text) land here.
    ①b Else if ``block.content`` is the legacy plain-text **success envelope**
       ``{"status": "success", "text": <str>}`` — the shape a non-JSON
       plain-text tail (read_history / get_post) decodes to — apply rule ②'s
       last-marker search to ``content["text"]``.  Every OTHER dict shape
       (error envelope, JSON arrays/lists, scalars, arbitrary JSON dicts)
       stays fail-closed; markers buried in arbitrary dict fields are never
       honoured.
    ② Else if ``block.content`` is a ``str``: take the LAST occurrence of the
       marker in the text (the bare-string carrier; rare in production because
       most results are decoded to a dict, but kept for completeness).
    ③ Any extraction failure, format mismatch, or absent marker → ``None``.

    A heal-synthesized block (``block.synthesized`` True) is never a witness and
    always returns ``None``.  ``read_inbox`` results carry no marker anywhere,
    so they naturally return ``None`` (the intended explicit exclusion).
    """
    if getattr(block, "synthesized", False):
        return None

    content = getattr(block, "content", None)

    if isinstance(content, dict):
        # Rule ① — structured dedicated field wins, and is the ONLY source
        # consulted when present.
        for field in _RECEIPT_FIELDS:
            if field in content:
                value = content[field]
                if not isinstance(value, str):
                    return None
                match = _MARKER_RE.fullmatch(value.strip())
                return match.group(1) if match is not None else None
        # Rule ①b — the plain-text success envelope is the production carrier
        # for read_history / get_post text tails; scan ITS ``text`` only.
        if content.get("status") == "success" and isinstance(
            content.get("text"), str
        ):
            return _last_marker_raw(content["text"])
        # Any other dict shape (error envelope, arbitrary JSON object): no fact.
        return None

    # Rule ② — bare plain text: the real receipt is appended last.
    if isinstance(content, str):
        return _last_marker_raw(content)

    # Rule ③ — anything else (None, list/array, scalar, ...) → no fact.
    return None


def admission_binding(tool_call_id: str, raw_receipt: str) -> str:
    """Return the stable receipt-possession binding for ``(id, receipt)``.

    ``sha256(tool_call_id ‖ 0x00 ‖ raw_receipt)`` as lowercase hex.  The NUL
    separator makes the pairing unambiguous so no ``(id, receipt)`` split can
    collide with a different one.  The binding proves the emitter possessed the
    receipt paired with this exact tool-call id; it does not by itself prove
    commit (see the admission-witness contract).
    """
    digest = hashlib.sha256(
        tool_call_id.encode("utf-8") + b"\x00" + raw_receipt.encode("utf-8")
    )
    return digest.hexdigest()


__all__ = ["extract_admission_receipt", "admission_binding"]
