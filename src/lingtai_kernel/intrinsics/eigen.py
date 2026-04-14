"""Eigen intrinsic — bare essentials of agent self.

Objects:
    pad — edit/load system/pad.md (agent's working notes)
    context — molt (shed context, keep a briefing)

Internal:
    context_forget — forced molt with system message (after ignored warnings)
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
        },
        "required": ["object", "action"],
    }


def handle(agent, args: dict) -> dict:
    """Handle eigen tool — pad and context management."""
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
    """Agent molt: summary IS the briefing, wipe + re-inject."""
    summary = args.get("summary")
    if summary is None:
        return {"error": "summary is required — write a briefing to your future self."}
    if not summary.strip():
        return {"error": "summary cannot be empty — write what you need to remember."}

    if agent._chat is None:
        return {"error": "No active chat session to molt."}

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

    # Inject the agent's summary as the opening context
    from ..i18n import t
    lang = agent._config.language
    iface = agent._session._chat.interface
    iface.add_user_message(f"{t(lang, 'eigen.molt_summary_prefix')}\n{summary}")

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
    """Forced molt with system message. Internal only — not exposed in SCHEMA.

    Called by base_agent auto-forget after ignored molt warnings.
    Same mechanism as molt, just with a system-authored summary.
    """
    from ..i18n import t
    return _context_molt(agent, {
        "summary": t(agent._config.language, "eigen.context_forget_summary"),
    })
