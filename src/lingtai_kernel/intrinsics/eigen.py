"""Eigen intrinsic — bare essentials of agent self.

Objects:
    pad — edit/load system/pad.md (agent's working notes)
    name — set true name (once), set/clear nickname

Internal:
    _context_molt — the shed-and-reload machinery (archive chat_history,
        wipe the wire session, reload pad/lingtai, inject a short post-molt
        notice as the opening message of the fresh session).
    context_forget — public entry point for system-initiated molt, called
        by base_agent after the warning ladder is exhausted. The agent has
        no tool surface to molt voluntarily anymore — molt happens to the
        agent, not something it performs. The persistent state (pad,
        lingtai, codex) is maintained every turn by procedure, so there
        is nothing for the agent to "prepare" at molt time.
"""
from __future__ import annotations

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
                "enum": ["pad", "name"],
                "description": t(lang, "eigen.object_description"),
            },
            "action": {
                "type": "string",
                "enum": ["edit", "load", "set", "nickname"],
                "description": t(lang, "eigen.action_description"),
            },
            "content": {
                "type": "string",
                "description": t(lang, "eigen.content_description"),
            },
        },
        "required": ["object", "action"],
    }


def handle(agent, args: dict) -> dict:
    """Handle eigen tool — pad and name."""
    obj = args.get("object", "")
    action = args.get("action", "")

    if obj == "pad":
        if action == "edit":
            return _pad_edit(agent, args)
        elif action == "load":
            return _pad_load(agent, args)
        else:
            return {"error": f"Unknown pad action: {action}. Use edit or load."}
    elif obj == "name":
        if action == "set":
            return _name_set(agent, args)
        elif action == "nickname":
            return _name_nickname(agent, args)
        else:
            return {"error": f"Unknown name action: {action}. Use set (true name) or nickname."}
    else:
        return {"error": f"Unknown object: {obj}. Use pad or name."}


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
    """Shed working memory: archive chat_history, wipe the wire session,
    reload pad/lingtai, inject a short system notice as the opening
    message of the fresh session.

    Not exposed as a tool action. Called only by ``context_forget``
    (below), which is itself invoked by base_agent after the molt-
    warning ladder is exhausted. The ``summary`` in ``args`` is the
    short post-molt notice (not a user-authored briefing); if missing,
    we fall back to the system default from i18n.
    """
    from ..i18n import t
    lang = agent._config.language
    summary = args.get("summary") or t(lang, "eigen.context_forget_summary")

    if agent._chat is None:
        return {"error": "No active chat session to molt."}

    before_tokens = agent._chat.interface.estimate_context_tokens()

    # Flush any unappended interface entries to chat_history.jsonl before
    # wiping the session — otherwise they'd vanish without reaching the
    # archive below.
    agent._append_chat_audit()

    # Wipe context
    agent._session._chat = None
    agent._session._interaction_id = None

    # Clear context section (context.md)
    agent._prompt_manager.delete_section("context")
    context_file = agent._working_dir / "system" / "context.md"
    if context_file.exists():
        context_file.unlink()

    # Reset molt warnings
    if hasattr(agent._session, "_compaction_warnings"):
        agent._session._compaction_warnings = 0

    # Track molt count and persist to manifest
    agent._molt_count += 1
    agent._workdir.write_manifest(agent._build_manifest())

    # Archive the pre-molt chat history:
    #   chat_history.jsonl (current molt) → chat_history_archive.jsonl (all past molts)
    # A molt_boundary entry is written to the archive as a separator between molts.
    import time as _time
    import json as _json
    boundary = {
        "type": "molt_boundary",
        "molt_count": agent._molt_count,
        "timestamp": _time.time(),
        "summary": summary,
    }
    history_dir = agent._working_dir / "history"
    history_dir.mkdir(exist_ok=True)
    current_path = history_dir / "chat_history.jsonl"
    archive_path = history_dir / "chat_history_archive.jsonl"
    try:
        with open(archive_path, "a") as archive:
            if current_path.is_file():
                archive.write(current_path.read_text())
            archive.write(_json.dumps(boundary, ensure_ascii=False) + "\n")
        if current_path.is_file():
            current_path.unlink()
    except OSError:
        pass
    # Fresh molt starts with an empty jsonl; the next append_chat_audit call
    # must treat the new interface's entries as new.
    agent._chat_audit_watermark = 0
    agent._idles_since_context_rebuild = 0

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

    # Inject a short post-molt notice as the opening message of the fresh
    # session. Not framed as a user-authored briefing — it's a state
    # announcement. The agent's durable state (pad, lingtai, codex) is
    # already loaded into the new system prompt via the post-molt hooks.
    iface = agent._session._chat.interface
    iface.add_user_message(summary)

    after_tokens = iface.estimate_context_tokens()

    agent._log(
        "eigen_molt",
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        molt_count=agent._molt_count,
    )

    return {
        "status": "ok",
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
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


def context_forget(agent) -> dict:
    """System-initiated molt. The only entry point — there is no tool
    surface for the agent to molt voluntarily. base_agent calls this
    when the molt-warning ladder is exhausted, or when it otherwise
    decides working memory should be shed. _context_molt falls back to
    the localized default summary when none is provided, so this is a
    one-liner.
    """
    return _context_molt(agent, {})
