"""System intrinsic — runtime, lifecycle, and synchronization.

Actions:
    show      — display agent identity, runtime, and resource usage
    nap       — pause execution; wakes on incoming message or timeout
    refresh   — stop, reload MCP servers and config from working dir, restart
    quell     — self-quell (no address) or quell another agent (with address)
    revive    — revive a dormant agent
    interrupt — interrupt a running agent's current turn
    nirvana   — permanently destroy an agent's working directory
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

def get_description(lang: str = "en") -> str:
    from ..i18n import t
    return t(lang, "system_tool.description")


def get_schema(lang: str = "en") -> dict:
    from ..i18n import t
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["show", "nap", "refresh", "quell", "revive", "interrupt", "nirvana"],
                "description": t(lang, "system_tool.action_description"),
            },
            "seconds": {
                "type": "number",
                "description": t(lang, "system_tool.seconds_description"),
            },
            "reason": {
                "type": "string",
                "description": t(lang, "system_tool.reason_description"),
            },
            "address": {
                "type": "string",
                "description": t(lang, "system_tool.address_description"),
            },
        },
        "required": ["action"],
    }


# Backward compat
SCHEMA = get_schema("en")
DESCRIPTION = get_description("en")


def handle(agent, args: dict) -> dict:
    """Handle system tool — runtime, lifecycle, synchronization."""
    action = args.get("action", "show")
    handler = {
        "show": _show,
        "nap": _nap,
        "refresh": _refresh,
        "quell": _quell,
        "revive": _revive,
        "interrupt": _interrupt,
        "nirvana": _nirvana,
    }.get(action)
    if handler is None:
        return {"status": "error", "message": f"Unknown system action: {action}"}
    return handler(agent, args)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def _show(agent, args: dict) -> dict:
    mail_addr = None
    if agent._mail_service is not None and agent._mail_service.address:
        mail_addr = agent._mail_service.address

    uptime = time.monotonic() - agent._uptime_anchor if agent._uptime_anchor is not None else 0.0
    stamina_left = max(0.0, agent._config.stamina - uptime) if agent._uptime_anchor is not None else None

    usage = agent.get_token_usage()

    if agent._chat is not None:
        try:
            window_size = agent._chat.context_window()
            ctx_total = usage["ctx_total_tokens"]
            usage_pct = round(ctx_total / window_size * 100, 1) if window_size else 0.0
        except Exception:
            window_size = None
            usage_pct = None
    else:
        window_size = None
        usage_pct = None

    return {
        "status": "ok",
        "identity": {
            "address": str(agent._working_dir),
            "agent_name": agent.agent_name,
            "mail_address": mail_addr,
        },
        "runtime": {
            "current_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "started_at": agent._started_at,
            "uptime_seconds": round(uptime, 1),
            "stamina": agent._config.stamina,
            "stamina_left": round(stamina_left, 1) if stamina_left is not None else None,
        },
        "tokens": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "thinking_tokens": usage["thinking_tokens"],
            "cached_tokens": usage["cached_tokens"],
            "total_tokens": usage["total_tokens"],
            "api_calls": usage["api_calls"],
            "context": {
                "system_tokens": usage["ctx_system_tokens"],
                "tools_tokens": usage["ctx_tools_tokens"],
                "history_tokens": usage["ctx_history_tokens"],
                "total_tokens": usage["ctx_total_tokens"],
                "window_size": window_size,
                "usage_pct": usage_pct,
            },
        },
    }


# ---------------------------------------------------------------------------
# nap (ported from clock.wait)
# ---------------------------------------------------------------------------

def _nap(agent, args: dict) -> dict:
    max_wait = 300
    seconds = args.get("seconds")
    if seconds is None:
        return {"status": "error", "message": "seconds is required for nap"}

    seconds = float(seconds)
    if seconds < 0:
        return {"status": "error", "message": "seconds must be non-negative"}
    seconds = min(seconds, max_wait)

    agent._log("system_nap_start", seconds=seconds)

    # Nap = idle: arm the soul timer so the inner voice can whisper during nap
    agent._start_soul_timer()

    def _check_wake(waited: float) -> dict | None:
        if agent._cancel_event.is_set():
            agent._log("system_nap_end", reason="interrupted", waited=waited)
            return {"status": "ok", "reason": "interrupted", "waited": waited}
        if agent._mail_arrived.is_set():
            agent._log("system_nap_end", reason="mail_arrived", waited=waited)
            return {"status": "ok", "reason": "mail_arrived", "waited": waited}
        return None

    result = _check_wake(0.0)
    if result:
        return result

    agent._mail_arrived.clear()

    poll_interval = 0.5
    t0 = time.monotonic()

    while True:
        waited = time.monotonic() - t0

        result = _check_wake(waited)
        if result:
            return result

        if waited >= seconds:
            agent._log("system_nap_end", reason="timeout", waited=waited)
            return {"status": "ok", "reason": "timeout", "waited": waited}

        remaining = seconds - waited
        sleep_time = min(poll_interval, remaining)

        agent._mail_arrived.wait(timeout=sleep_time)


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def _refresh(agent, args: dict) -> dict:
    from ..i18n import t
    reason = args.get("reason", "")
    agent._log("refresh_requested", reason=reason)
    agent._refresh_requested = True
    agent._shutdown.set()
    return {
        "status": "ok",
        "message": t(agent._config.language, "system_tool.refresh_message"),
    }


# ---------------------------------------------------------------------------
# Karma gate mapping
# ---------------------------------------------------------------------------

_KARMA_ACTIONS = {"interrupt", "quell", "revive"}
_NIRVANA_ACTIONS = {"nirvana"}


def _check_karma_gate(agent, action: str, args: dict) -> dict | None:
    from ..handshake import is_agent
    if action in _KARMA_ACTIONS and not agent._admin.get("karma"):
        return {"error": True, "message": f"Not authorized for {action} (requires admin.karma=True)"}
    if action in _NIRVANA_ACTIONS and not (agent._admin.get("karma") and agent._admin.get("nirvana")):
        return {"error": True, "message": f"Not authorized for {action} (requires admin.nirvana=True)"}
    address = args.get("address")
    if not address:
        return {"error": True, "message": f"{action} requires an address"}
    if str(agent._working_dir) == str(address):
        return {"error": True, "message": f"Cannot {action} self"}
    if not is_agent(address):
        return {"error": True, "message": f"No agent at {address}"}
    return None


def _quell(agent, args: dict) -> dict:
    from ..i18n import t
    address = args.get("address")
    if not address:
        # Self-quell — any agent can put itself to sleep
        from ..state import AgentState
        reason = args.get("reason", "")
        agent._log("self_quell", reason=reason)
        agent._set_state(AgentState.DORMANT, reason="self-quell")
        agent._dormant.set()
        agent._cancel_event.set()
        return {
            "status": "ok",
            "message": t(agent._config.language, "system_tool.quell_message"),
        }
    # Quell other — karma-gated
    from pathlib import Path
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "quell", args)
    if err:
        return err
    if not is_alive(address):
        return {"error": True, "message": f"Agent at {address} is not running — already dormant?"}
    (Path(address) / ".quell").write_text("")
    agent._log("karma_quell", target=address)
    return {"status": "quelled", "address": address}


def _revive(agent, args: dict) -> dict:
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "revive", args)
    if err:
        return err
    address = args["address"]
    if is_alive(address):
        return {"error": True, "message": f"Agent at {address} is already running"}
    revived = agent._revive_agent(address)
    if revived is None:
        return {"error": True, "message": "Revive not supported — no _revive_agent handler"}
    agent._log("karma_revive", target=address)
    return {"status": "revived", "address": address}


def _interrupt(agent, args: dict) -> dict:
    from pathlib import Path
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "interrupt", args)
    if err:
        return err
    address = args["address"]
    if not is_alive(address):
        return {"error": True, "message": f"Agent at {address} is not running"}
    (Path(address) / ".interrupt").write_text("")
    agent._log("karma_interrupt", target=address)
    return {"status": "interrupted", "address": address}


def _nirvana(agent, args: dict) -> dict:
    import shutil
    from pathlib import Path
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "nirvana", args)
    if err:
        return err
    address = args["address"]
    if is_alive(address):
        (Path(address) / ".quell").write_text("")
        import time as _time
        deadline = _time.time() + 10.0
        while _time.time() < deadline:
            if not is_alive(address):
                break
            _time.sleep(0.5)
        else:
            if is_alive(address):
                return {"error": True, "message": f"Agent at {address} did not quell within timeout"}
    shutil.rmtree(address)
    agent._log("karma_nirvana", target=address)
    return {"status": "nirvana", "address": address}
