"""System intrinsic — runtime, lifecycle, and synchronization.

Actions:
    show      — display agent identity, runtime, and resource usage
    nap       — pause execution; wakes on incoming message or timeout
    refresh   — stop, reload MCP servers and config from working dir, restart
    sleep     — self only, go to sleep (no karma needed)
    lull      — put another agent to sleep (requires karma)
    suspend   — suspend another agent (requires karma)
    cpr       — resuscitate a suspended agent (requires karma)
    interrupt — interrupt a running agent's current turn (requires karma)
    clear     — force a full molt on another agent (requires karma)
    nirvana   — permanently destroy an agent's working directory (requires nirvana)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..handshake import resolve_address

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
                "enum": ["show", "nap", "refresh", "sleep", "lull", "interrupt", "suspend", "cpr", "clear", "nirvana"],
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


def handle(agent, args: dict) -> dict:
    """Handle system tool — runtime, lifecycle, synchronization."""
    action = args.get("action", "show")
    handler = {
        "show": _show,
        "nap": _nap,
        "refresh": _refresh,
        "sleep": _sleep,
        "lull": _lull,
        "suspend": _suspend,
        "cpr": _cpr,
        "interrupt": _interrupt,
        "clear": _clear,
        "nirvana": _nirvana,
    }.get(action)
    if handler is None:
        return {"status": "error", "message": f"Unknown system action: {action}"}
    return handler(agent, args)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def _show(agent, args: dict) -> dict:
    result = agent.status()
    result["status"] = "ok"
    return result


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

    # Clear stale wake signals — only events arriving DURING the nap should wake it.
    agent._nap_wake.clear()
    agent._nap_wake_reason = ""

    def _check_wake(waited: float) -> dict | None:
        if agent._cancel_event.is_set():
            agent._log("system_nap_end", reason="interrupted", waited=waited)
            return {"status": "ok", "reason": "interrupted", "waited": waited}
        if agent._nap_wake.is_set():
            reason = agent._nap_wake_reason or "unknown"
            agent._log("system_nap_end", reason=reason, waited=waited)
            return {"status": "ok", "reason": reason, "waited": waited}
        return None

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

        # Clear right before wait to avoid TOCTOU: if a wake signal arrives
        # between clear and wait, the event is re-set and wait returns immediately.
        agent._nap_wake.clear()
        agent._nap_wake.wait(timeout=sleep_time)


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def _refresh(agent, args: dict) -> dict:
    from ..i18n import t
    reason = args.get("reason", "")
    agent._log("refresh_requested", reason=reason)
    agent._perform_refresh()
    return {
        "status": "ok",
        "message": t(agent._config.language, "system_tool.refresh_message"),
    }


# ---------------------------------------------------------------------------
# Karma / Nirvana gate mapping
# ---------------------------------------------------------------------------

_KARMA_ACTIONS = {"interrupt", "lull", "suspend", "cpr", "clear"}
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
    # Resolve relative address to absolute path
    base_dir = agent._working_dir.parent
    resolved = resolve_address(address, base_dir)
    if str(resolved) == str(agent._working_dir):
        return {"error": True, "message": f"Cannot {action} self"}
    if not is_agent(resolved):
        return {"error": True, "message": f"No agent at {address}"}
    # Store resolved path for downstream use
    args["_resolved_address"] = resolved
    return None


def _sleep(agent, args: dict) -> dict:
    """Self-sleep — any agent can put itself to sleep, no karma needed."""
    from ..i18n import t
    from ..state import AgentState
    reason = args.get("reason", "")
    agent._log("self_sleep", reason=reason)
    agent._set_state(AgentState.ASLEEP, reason="self-sleep")
    agent._asleep.set()
    agent._cancel_event.set()
    return {
        "status": "ok",
        "message": t(agent._config.language, "system_tool.sleep_message"),
    }


def _lull(agent, args: dict) -> dict:
    """Lull another agent to sleep — karma-gated."""
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "lull", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running — already asleep?"}
    (resolved / ".sleep").write_text("")
    agent._log("karma_lull", target=address)
    return {"status": "asleep", "address": address}


def _suspend(agent, args: dict) -> dict:
    """Suspend another agent — karma-gated."""
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "suspend", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running — already suspended?"}
    (resolved / ".suspend").write_text("")
    agent._log("karma_suspend", target=address)
    return {"status": "suspended", "address": address}


def _cpr(agent, args: dict) -> dict:
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "cpr", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is already running"}
    resuscitated = agent._cpr_agent(str(resolved))
    if resuscitated is None:
        return {"error": True, "message": "CPR not supported — no _cpr_agent handler"}
    agent._log("karma_cpr", target=address)
    return {"status": "resuscitated", "address": address}


def _interrupt(agent, args: dict) -> dict:
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "interrupt", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running"}
    (resolved / ".interrupt").write_text("")
    agent._log("karma_interrupt", target=address)
    return {"status": "interrupted", "address": address}


def _clear(agent, args: dict) -> dict:
    """Force a full molt on another agent — karma-gated.

    Writes a .clear signal; the target's heartbeat loop picks it up and
    invokes eigen.context_forget, which archives chat history and injects
    a system-authored recovery summary pointing at pad/codex/inbox.
    """
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "clear", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running"}
    # Content of .clear becomes the `source` tag in the recovery summary.
    # Default to the calling agent's name so targets can see who forced it.
    source = (args.get("reason") or "").strip() or agent.agent_name or "admin"
    (resolved / ".clear").write_text(source)
    agent._log("karma_clear", target=address, source=source)
    return {"status": "cleared", "address": address, "source": source}


def _nirvana(agent, args: dict) -> dict:
    import shutil
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "nirvana", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if is_alive(resolved):
        (resolved / ".sleep").write_text("")
        import time as _time
        deadline = _time.time() + 10.0
        while _time.time() < deadline:
            if not is_alive(resolved):
                break
            _time.sleep(0.5)
        else:
            if is_alive(resolved):
                return {"error": True, "message": f"Agent at {address} did not sleep within timeout"}
    shutil.rmtree(resolved)
    agent._log("karma_nirvana", target=address)
    return {"status": "nirvana", "address": address}
