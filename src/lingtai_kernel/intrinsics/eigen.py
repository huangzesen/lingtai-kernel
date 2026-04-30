"""Eigen intrinsic — bare essentials of agent self.

Objects:
    pad — edit/load system/pad.md (agent's working notes)
    context — molt (shed context, keep a briefing)
    name — set true name (once), set/clear nickname

Internal:
    _context_molt — the shed-and-reload machinery (archive chat_history,
        wipe the wire session, reload pad/lingtai, inject the agent's
        summary as the opening message of the fresh session).
    context_forget — system-initiated molt, called by base_agent after the
        warning ladder is exhausted. Same mechanism as agent-called molt,
        but with a system-authored summary.
"""
from __future__ import annotations

from ..llm.interface import ToolCallBlock, ToolResultBlock


def get_description(lang: str = "en") -> str:
    from ..i18n import t
    return t(lang, "eigen.description")


def get_schema(lang: str = "en") -> dict:
    from ..i18n import t
    return {
        "type": "object",
        "properties": {
            "object": {
                "type": "string",
                "enum": ["pad", "context", "name"],
                "description": t(lang, "eigen.object_description"),
            },
            "action": {
                "type": "string",
                "enum": ["edit", "load", "molt", "set", "nickname"],
                "description": t(lang, "eigen.action_description"),
            },
            "content": {
                "type": "string",
                "description": t(lang, "eigen.content_description"),
            },
            "summary": {
                "type": "string",
                "description": t(lang, "eigen.summary_description"),
            },
            "keep_tool_calls": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "eigen.keep_tool_calls_description"),
            },
        },
        "required": ["object", "action"],
    }


def handle(agent, args: dict) -> dict:
    """Handle eigen tool — pad, context, and name."""
    obj = args.get("object", "")
    action = args.get("action", "")

    if obj == "pad":
        if action == "edit":
            return _pad_edit(agent, args)
        elif action == "load":
            return _pad_load(agent, args)
        else:
            return {"error": f"Unknown pad action: {action}. Use edit or load."}
    elif obj == "context":
        if action == "molt":
            return _context_molt(agent, args)
        else:
            return {"error": f"Unknown context action: {action}. Use molt."}
    elif obj == "name":
        if action == "set":
            return _name_set(agent, args)
        elif action == "nickname":
            return _name_nickname(agent, args)
        else:
            return {"error": f"Unknown name action: {action}. Use set (true name) or nickname."}
    else:
        return {"error": f"Unknown object: {obj}. Use pad, context, or name."}


def _pad_edit(agent, args: dict) -> dict:
    """Write content to system/pad.md and auto-load into system prompt."""
    content = args.get("content", "")

    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    pad_path = system_dir / "pad.md"
    pad_path.write_text(content)

    agent._log("eigen_pad_edit", length=len(content))

    # Auto-load into system prompt
    _pad_load(agent, {})

    return {"status": "ok", "path": str(pad_path), "size_bytes": len(content.encode("utf-8"))}


def _pad_load(agent, args: dict) -> dict:
    """Load system/pad.md into the system prompt."""
    system_dir = agent._working_dir / "system"
    system_dir.mkdir(exist_ok=True)
    pad_path = system_dir / "pad.md"
    if not pad_path.is_file():
        pad_path.write_text("")

    content = pad_path.read_text()
    size_bytes = len(content.encode("utf-8"))

    if content.strip():
        agent._prompt_manager.write_section("pad", content)
    else:
        agent._prompt_manager.delete_section("pad")
    agent._token_decomp_dirty = True
    agent._flush_system_prompt()

    agent._log("eigen_pad_load", size_bytes=size_bytes)

    return {
        "status": "ok",
        "path": str(pad_path),
        "size_bytes": size_bytes,
        "content_preview": content[:200],
    }


def _context_molt(agent, args: dict) -> dict:
    """Agent molt: summary IS the briefing, wipe + re-inject.

    Optional ``keep_tool_calls`` is a list of LingTai-issued tool-call ids
    (the ``_tool_call_id`` field stamped into every tool-result content by
    LLMService.make_tool_result). Each named pair (tool_use + matching
    tool_result) survives the wipe, replayed into the fresh session right
    after the summary. Validation runs BEFORE any mutation: if any id is
    not found in the current chat history, the molt is refused, the count
    is not incremented, and the agent can retry with a corrected list.
    """
    summary = args.get("summary")
    if summary is None:
        return {"error": "summary is required — write a briefing to your future self."}
    if not summary.strip():
        return {"error": "summary cannot be empty — write what you need to remember."}

    if agent._chat is None:
        return {"error": "No active chat session to molt."}

    keep_tool_calls = args.get("keep_tool_calls") or []
    if keep_tool_calls and not isinstance(keep_tool_calls, list):
        return {"error": "keep_tool_calls must be a list of LingTai tool-call ids (strings)."}

    # Validate keep-list BEFORE any state mutation so a typo doesn't
    # consume a molt. Walk the live interface, harvest LingTai-issued ids
    # from tool_result content, and confirm every requested id is present.
    iface_pre = agent._chat.interface
    keep_pairs: list[tuple] = []  # list of (tool_call_block, tool_result_block)
    if keep_tool_calls:
        requested = set(keep_tool_calls)
        # First pass: find tool_results whose content carries a matching
        # _tool_call_id, capture the wire id (provider's tool_use_id).
        provider_id_for_lingtai: dict[str, str] = {}
        result_for_provider_id: dict[str, object] = {}
        for entry in iface_pre.entries:
            for block in entry.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                content = block.content
                if not isinstance(content, dict):
                    continue
                lt_id = content.get("_tool_call_id")
                if lt_id in requested:
                    provider_id_for_lingtai[lt_id] = block.id
                    result_for_provider_id[block.id] = block
        unmatched = [tid for tid in keep_tool_calls if tid not in provider_id_for_lingtai]
        if unmatched:
            return {
                "error": (
                    "Some keep_tool_calls ids were not found in the current "
                    "chat history. Molt refused; molt count unchanged. "
                    "Retry with a corrected list."
                ),
                "unmatched_ids": unmatched,
                "matched_count": len(provider_id_for_lingtai),
            }
        # Second pass: for each matched provider id, find the corresponding
        # tool_call (assistant) block. tool_use and tool_result share the
        # same id on the wire — that's the pairing key.
        call_for_provider_id: dict[str, object] = {}
        for entry in iface_pre.entries:
            for block in entry.content:
                if isinstance(block, ToolCallBlock) and block.id in result_for_provider_id:
                    call_for_provider_id[block.id] = block
        # Build the pair list in the order the agent requested.
        for lt_id in keep_tool_calls:
            pid = provider_id_for_lingtai[lt_id]
            call_block = call_for_provider_id.get(pid)
            result_block = result_for_provider_id.get(pid)
            if call_block is not None and result_block is not None:
                keep_pairs.append((call_block, result_block))

    before_tokens = agent._chat.interface.estimate_context_tokens()

    # Wipe context
    agent._session._chat = None
    agent._session._interaction_id = None

    # Reset molt warnings
    if hasattr(agent._session, "_compaction_warnings"):
        agent._session._compaction_warnings = 0

    # Track molt count and persist to manifest
    agent._molt_count += 1
    agent._workdir.write_manifest(agent._build_manifest())

    # Archive the pre-molt chat history:
    #   chat_history.jsonl (current molt) → chat_history_archive.jsonl (all past molts)
    history_dir = agent._working_dir / "history"
    history_dir.mkdir(exist_ok=True)
    current_path = history_dir / "chat_history.jsonl"
    archive_path = history_dir / "chat_history_archive.jsonl"
    try:
        if current_path.is_file():
            with open(archive_path, "a") as archive:
                archive.write(current_path.read_text())
            current_path.unlink()
    except OSError:
        pass

    # Reset soul mirror session
    from .soul import reset_soul_session
    reset_soul_session(agent)

    # Post-molt hooks — reload character/pad into prompt manager BEFORE new session
    for cb in getattr(agent, "_post_molt_hooks", []):
        try:
            cb()
        except Exception:
            pass

    # Now create fresh session with updated prompt manager
    agent._session.ensure_session()

    # Inject the agent's summary as the opening context of the fresh session.
    from ..i18n import t
    lang = agent._config.language
    iface = agent._session._chat.interface
    iface.add_user_message(f"{t(lang, 'eigen.molt_summary_prefix')}\n{summary}")

    # Replay kept tool-call pairs into the fresh session as
    # assistant[tool_call] + user[tool_result] entries. The wire ids carry
    # over so any provider that pairs by id stays consistent with its own
    # transcript invariants.
    for call_block, result_block in keep_pairs:
        iface.add_assistant_message(content=[call_block])
        iface.add_tool_results([result_block])

    after_tokens = iface.estimate_context_tokens()

    agent._log(
        "eigen_molt",
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        molt_count=agent._molt_count,
        kept_tool_calls=len(keep_pairs),
    )

    return {
        "status": "ok",
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "kept_tool_calls": len(keep_pairs),
    }


def _name_set(agent, args: dict) -> dict:
    """Set the agent's true name."""
    name = args.get("content", "").strip()
    if not name:
        return {"error": "Name cannot be empty. Provide your chosen name in 'content'."}
    try:
        agent.set_name(name)
    except RuntimeError as e:
        return {"error": str(e)}
    return {"status": "ok", "name": name}


def _name_nickname(agent, args: dict) -> dict:
    """Set or change the agent's nickname (别名). Mutable."""
    nickname = args.get("content", "").strip()
    agent.set_nickname(nickname)
    return {"status": "ok", "nickname": nickname or None}


def context_forget(agent, *, source: str = "warning_ladder", attempts: int = 0) -> dict:
    """Forced molt with a system-authored summary.

    Called by base_agent from three paths:
      - source="warning_ladder" (default): post-molt-warning exhaustion
      - source="aed": after max AED retries, before declaring ASLEEP
      - source=<name>: a .forget signal file dropped externally (karma-gated)

    Same mechanism as agent-called molt, just with a system-authored summary
    whose wording reflects the trigger.
    """
    from ..i18n import t
    lang = agent._config.language
    if source == "warning_ladder":
        summary = t(lang, "eigen.context_forget_summary")
    elif source == "aed":
        summary = t(lang, "eigen.context_forget_summary_aed").replace("{attempts}", str(attempts))
    else:
        summary = t(lang, "eigen.context_forget_summary_signal").replace("{source}", source)
    return _context_molt(agent, {"summary": summary})
