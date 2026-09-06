"""Soul —official host plugin ——the agent's inner voice.

Soul remains the agent's real self-state and soul-flow capability: its five
operational actions keep their LTP-v2 schemas, result shapes, persistence,
consultation, timer, and notification behavior. The only recut is the host
boundary: a static :data:`DECLARATION` binds the family through the kernel's
least-privilege ``SoulRuntimePort`` and ``WorkdirPort`` instead of accepting a
whole Agent. The legacy intrinsic hook exports below remain compatibility
bridges for kernel lifecycle calls; they adapt at that boundary and never form
the model-facing tool surface.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from lingtai.kernel.config import DEFAULT_SOUL_DELAY_SECONDS
from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration

from ..tool_family import ChildTool, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child
from .config import (
    CONSULTATION_PAST_COUNT_MAX,
    CONSULTATION_PAST_COUNT_MIN,
    SOUL_DELAY_MIN_SECONDS,
    SOUL_VOICE_BUILTINS,
    SOUL_VOICE_PROMPT_MAX,
    _atomic_write_init,
    _build_soul_system_prompt as _build_soul_system_prompt_impl,
    _handle_config,
    _handle_voice,
    _persist_soul_config as _persist_soul_config_impl,
    _persist_soul_voice as _persist_soul_voice_impl,
)
from .consultation import (
    _CONSULTATION_MAX_ROUNDS,
    _DIARY_CUE_TOKEN_CAP,
    _build_consultation_cue as _build_consultation_cue_impl,
    _build_consultation_tool_refusal,
    _fit_interface_to_window,
    _kind_for_source,
    _list_snapshot_paths as _list_snapshot_paths_impl,
    _load_snapshot_interface,
    _render_current_diary as _render_current_diary_impl,
    _run_consultation as _run_consultation_impl,
    _run_consultation_batch as _run_consultation_batch_impl,
    _send_with_timeout as _send_with_timeout_impl,
    _write_soul_tokens as _write_soul_tokens_impl,
    build_consultation_pair as _build_consultation_pair_impl,
)
from .flow import (
    _append_soul_flow_record as _append_soul_flow_record_impl,
    _cancel_soul_timer as _cancel_soul_timer_impl,
    _flatten_v3_for_pair as _flatten_v3_for_pair_impl,
    _persist_soul_entry as _persist_soul_entry_impl,
    _rehydrate_appendix_tracking as _rehydrate_appendix_tracking_impl,
    _run_consultation_fire as _run_consultation_fire_impl,
    _soul_fire_allowed as _soul_fire_allowed_impl,
    _soul_whisper as _soul_whisper_impl,
    _start_soul_timer as _start_soul_timer_impl,
)
from .inquiry import _run_inquiry as _run_inquiry_impl
from .inquiry import soul_inquiry as _soul_inquiry_impl
from .settings import soul_settings_provider

if TYPE_CHECKING:
    from lingtai.kernel.tool_plugin import SoulRuntimePort, ToolPluginHost, WorkdirPort


_INQUIRY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "inquiry": {
            "type": "string",
            "description": "Your self-inquiry — a question to yourself. Required for action='inquiry'. This is you asking yourself a question, not prompting someone else.",
        },
    },
    "required": ["inquiry"],
    "additionalProperties": False,
}

# Flow takes no input. The env opt-in gate is the operator's, not the model's:
# there is deliberately no field here (and none anywhere in this family) that
# could enable flow from a tool call.
_FLOW_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# Both knobs are optional-but-at-least-one; strict provider schemas express
# optional fields as required nullable properties, and ``_strip_nulls`` turns a
# null back into "absent" before the existing validator sees it — so
# ``{delay_seconds: null, consultation_past_count: null}`` still produces the
# exact pre-migration "config requires at least one of ..." error.
_CONFIG_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "delay_seconds": {
            "type": ["number", "null"],
            "minimum": SOUL_DELAY_MIN_SECONDS,
            "description": "Wall-clock delay between soul flow fires, in seconds. This is ONLY the cadence AFTER soul flow is enabled via env LINGTAI_SOUL_FLOW_ENABLED=1 — it is NOT an off switch, and cannot itself enable/disable flow. If the env var is unset, soul flow is disabled entirely and NO fires occur regardless of this value. Soul flow is your periodic inner reflection — when enabled and the timer fires, past versions of yourself (from molt snapshots) and a stepped-back read of your current work speak to you as voices, surfacing patterns, blind spots, and perspective you might miss while busy. Pass null to leave it unchanged. Minimum 30s; lower for more frequent reflection (e.g. 300 = every 5 minutes; 7200 = every 2 hours). Setting it while flow is enabled cancels the currently-pending fire and restarts the timer on the new schedule. See soul-manual.",
        },
        "consultation_past_count": {
            "type": ["integer", "null"],
            "minimum": CONSULTATION_PAST_COUNT_MIN,
            "maximum": CONSULTATION_PAST_COUNT_MAX,
            "description": "K — number of past-self voices sampled per fire. Pass null when not setting it. Each fire runs M=1+K parallel LLM calls (1 stepped-back diary reader + K random past-snapshot voices). Range [0, 5]. 0 = insights-only fires (cheapest, no past-self voices). Higher K is costlier per fire and fills more chat-history with voice content; lower K is faster and quieter. At least one of delay_seconds/consultation_past_count must be non-null.",
        },
    },
    "required": ["delay_seconds", "consultation_past_count"],
    "additionalProperties": False,
}

_VOICE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "set": {
            "type": ["string", "null"],
            "description": "Which voice profile to switch to. Built-ins: 'inner' (terse — 'you are the soul, speak as inner voice') or 'observer' (structured stepped-back hook framing). Or 'custom', which requires a 'prompt' field with your own system-prompt text. Pass null to read the current voice and resolved prompt without changing anything.",
        },
        "prompt": {
            "type": ["string", "null"],
            "maxLength": SOUL_VOICE_PROMPT_MAX,
            "description": "Custom system prompt for soul-flow voice. Required when set='custom'; ignored otherwise. Length capped at 4000 characters. Speak to yourself as the soul — describe how you want to be framed when reading your own diary. The same prompt is used for both insights (current self) and past (frozen earlier self) consultations; the per-fire cue text differentiates whose diary you're reading.",
        },
    },
    "required": ["set", "prompt"],
    "additionalProperties": False,
}

_DISMISS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

def _strip_nulls(action_input: Mapping[str, Any]) -> dict[str, Any]:
    """Drop explicit nulls so "absent" and "null" mean the same downstream.

    Strict provider schemas express an optional field as a REQUIRED nullable
    property, so the model must send ``{"set": null, "prompt": null}`` for a
    bare voice read. The pre-migration handlers keyed off ``"x" in args`` /
    ``args.get("set") is None``; stripping nulls here reproduces that exact
    behavior — including ``config``'s "at least one knob" error when both
    knobs are null — without touching the validators themselves.
    """
    return {key: value for key, value in action_input.items() if value is not None}


def _adapt_manual_result(mcp_result: dict) -> dict:
    """Flatten the reserved ``manual`` child's canonical result to soul's shape.

    The reserved child is registered unwrapped, so ``ToolFamily.handle()``
    returns its canonical ``content``/``structuredContent`` result verbatim.
    Soul's public manual result predates that generic contract and must stay
    the flat ``status``/``manual``/``manual_path`` shape, so this Host-owned
    adapter runs strictly *after* dispatch — never inside the child, and never
    as a second envelope around it.
    """
    flat: dict = {
        "status": mcp_result.get("status", "ok"),
        "manual": mcp_result["content"][0]["text"],
        "manual_path": mcp_result["structuredContent"]["manual_path"],
    }
    if "error" in mcp_result:
        flat["error"] = mcp_result["error"]
    return flat


def _handle_flow(runtime) -> dict:
    """``action='flow'`` — trigger one voluntary consultation fire.

    Relocated verbatim from the pre-migration ``handle`` if-chain; the gate,
    lock, thread, and payloads below are unchanged.
    """
    # Opt-in gate: soul flow is disabled by default. When disabled,
    # return an explicit, stable "disabled" status BEFORE touching the
    # lock or spawning a fire thread — so a disabled agent burns no
    # thread and does not wait for IDLE. This is expected config state,
    # not an error to retry (see soul-manual).
    from .flow import _soul_flow_enabled, SOUL_FLOW_ENABLED_ENV
    if not _soul_flow_enabled():
        runtime.log("soul_flow_voluntary_disabled")
        return {
            "status": "disabled",
            "enabled": False,
            "env_var": SOUL_FLOW_ENABLED_ENV,
            "message": (
                "Soul flow is disabled by default on this agent. It is "
                "opt-in: set the environment variable "
                f"{SOUL_FLOW_ENABLED_ENV}=1 (also true/yes/on), then "
                "refresh/restart, to enable periodic and voluntary "
                "past-self consultation. delay_seconds is only the "
                "cadence AFTER this opt-in — it is not an off switch, "
                "and soul(action='config') does not enable flow. "
                "inquiry, config, voice, and dismiss remain available "
                "while flow is disabled. Do not retry flow blindly; the "
                "operator must set the env var first. See soul-manual "
                "skill for how to enable/disable, troubleshoot, and the "
                "privacy/cost rationale."
            ),
        }

    # Voluntary trigger: try-acquire the fire lock non-blocking. If
    # held, another fire is already in flight (timer-fired or a prior
    # voluntary call) — refuse so the agent isn't surprised by a
    # silent no-op. If free, release immediately and kick off the
    # real fire on a daemon thread; _run_consultation_fire will
    # re-acquire under the same gate.
    lock = runtime.fire_lock
    if lock is not None:
        if not lock.acquire(blocking=False):
            runtime.log("soul_flow_voluntary_rejected", reason="ongoing")
            return {"error": "soul flow ongoing, request rejected"}
        lock.release()

    import threading
    from . import flow as _flow

    def _fire():
        try:
            # Wait for IDLE before firing — voluntary flow is triggered
            # while ACTIVE (inside a tool call), but _run_consultation_fire
            # gates on IDLE.  _idle is a threading.Event set on every
            # non-ACTIVE transition (see base_agent._set_state).
            idle_event = runtime.idle_event
            if idle_event is not None:
                runtime.log("soul_flow_voluntary_waiting_idle")
                # Wait up to soul_delay seconds; if the agent never goes
                # IDLE (stuck in ACTIVE), give up rather than hang.
                timeout = runtime.soul_delay
                if not idle_event.wait(timeout=timeout):
                    runtime.log("soul_flow_voluntary_timeout",
                               timeout=timeout)
                    return
            _flow._run_consultation_fire(runtime)
        except Exception as e:
            try:
                runtime.log("soul_flow_voluntary_error", error=str(e)[:200])
            except Exception:
                pass

    t = threading.Thread(target=_fire, daemon=True, name="soul-flow-voluntary")
    t.start()
    runtime.log("soul_flow_voluntary_triggered")
    return {
        "status": "ok",
        "message": (
            "Soul flow triggered. Voices will arrive shortly as a "
            "separate soul(action='flow') tool-call pair appended to "
            "your chat history (replacing any prior soul-flow pair)."
        ),
    }


def _handle_inquiry(runtime, action_input: Mapping[str, Any]) -> dict:
    """``action='inquiry'`` — sync mirror session; requires inquiry text."""
    inquiry = action_input.get("inquiry", "")
    if not isinstance(inquiry, str) or not inquiry.strip():
        return {"error": "inquiry is required — what do you want to reflect on?"}

    runtime.log("soul_inquiry", inquiry=inquiry.strip()[:200])

    result = soul_inquiry(runtime, inquiry.strip())

    if result:
        runtime.persist_soul_entry(result, mode="inquiry")
        runtime.log("soul_inquiry_done")
        return {"status": "ok", "voice": result["voice"]}
    else:
        runtime.log("soul_inquiry_done")
        return {"status": "ok", "voice": "(silence)"}


def _handle_dismiss(runtime) -> dict:
    """``action='dismiss'`` — clear the current soul flow notification."""
    result = runtime.dismiss_notification("soul", invoked_by="soul")
    if result.get("status") == "ok":
        result.setdefault("message", "Soul flow notification dismissed.")
    return result


# Soul's operational action names and their strict inputs are declared once.
# ``manual`` remains reserved to the declaration and is appended last by the
# family through the shared ManualTool child.
SOUL_DECLARED_ACTIONS: tuple[str, ...] = (
    "inquiry", "flow", "config", "voice", "dismiss",
)

_DECLARED_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "inquiry": _INQUIRY_INPUT_SCHEMA,
    "flow": _FLOW_INPUT_SCHEMA,
    "config": _CONFIG_INPUT_SCHEMA,
    "voice": _VOICE_INPUT_SCHEMA,
    "dismiss": _DISMISS_INPUT_SCHEMA,
}

_DESCRIPTION = (
    "Your inner voice. One tool, seven actions, each with its own strict input "
    "object: soul(action=..., input={...}, reasoning='why'). flow is OPT-IN "
    "and DISABLED by default: it runs only when the operator sets env "
    "LINGTAI_SOUL_FLOW_ENABLED=1 (then refreshes). While disabled, "
    "soul(action='flow', input={}) returns status='disabled' (not an error — do not retry); inquiry/config/voice/dismiss still work. When enabled, "
    "flow fires periodic past-self consultation every soul_delay seconds while "
    "IDLE — M=1+K parallel LLM calls (1 stepped-back read of current chat + K "
    "past-snapshot voices) arrive as an involuntary soul(action='flow') pair. "
    "delay_seconds is only the cadence after opt-in, NOT an off switch, and no "
    "action in this family can enable flow. inquiry: ask a deep copy of "
    "yourself a question; answer returns in the tool result. config: tune flow "
    "knobs at runtime (delay_seconds, consultation_past_count) — does not enable "
    "flow. voice: read or choose how your own soul-flow voice sounds. dismiss: "
    "clear the current flow notification. settings: show Soul's five current "
    "settings without changing them. manual: return the installed "
    "soul-manual skill without performing any soul operation. Results are "
    "small, so leave root summarize false (short-result profile); call manual "
    "with summarize=false so the exact procedure is not summarized away. See "
    "soul-manual for details."
)


def _coerce_runtime(agent: Any) -> "SoulRuntimePort":
    """Adapt a kernel-hook/legacy caller at the one compatibility boundary."""
    from lingtai.adapters.tool_plugin_host import AgentSoulRuntimeAdapter, agent_soul_runtime

    if isinstance(agent, AgentSoulRuntimeAdapter):
        return agent
    return agent_soul_runtime(agent)


def _build_declared_children(runtime: "SoulRuntimePort | None") -> list[ChildTool]:
    if runtime is None:
        def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
            raise AssertionError("the module-level schema-only ToolFamily never dispatches")

        return [
            ChildTool(action, _DECLARED_INPUT_SCHEMAS[action], _unused, title=f"{action} input")
            for action in SOUL_DECLARED_ACTIONS
        ]
    return [
        ChildTool("inquiry", _INQUIRY_INPUT_SCHEMA, lambda i: _handle_inquiry(runtime, _strip_nulls(i)), title="inquiry input"),
        ChildTool("flow", _FLOW_INPUT_SCHEMA, lambda _i: _handle_flow(runtime), title="flow input"),
        ChildTool("config", _CONFIG_INPUT_SCHEMA, lambda i: _handle_config(runtime, _strip_nulls(i)), title="config input"),
        ChildTool("voice", _VOICE_INPUT_SCHEMA, lambda i: _handle_voice(runtime, _strip_nulls(i)), title="voice input"),
        ChildTool("dismiss", _DISMISS_INPUT_SCHEMA, lambda _i: _handle_dismiss(runtime), title="dismiss input"),
    ]


def _build_family(
    runtime: "SoulRuntimePort | None",
    manual_source: Any | None = None,
) -> ToolFamily:
    children = _build_declared_children(runtime)
    if runtime is None:
        children.append(ChildTool("manual", MANUAL_INPUT_SCHEMA, lambda _i: {}, title="manual input"))
        settings_provider = tuple
    else:
        children.append(build_manual_child(manual_source, DECLARATION.manual))
        settings_provider = soul_settings_provider(runtime)
    return ToolFamily(
        DECLARATION.name,
        children,
        settings_provider=settings_provider,
    )


def _build_children(agent: Any) -> list[ChildTool]:
    """Compatibility test/hook view of the same declaration-owned children."""
    if agent is None:
        return list(_build_family(None)._children.values())
    runtime = _coerce_runtime(agent)
    return list(_build_family(runtime, runtime)._children.values())


def get_description(lang: str = "en") -> str:
    return _DESCRIPTION


def get_schema(lang: str = "en") -> dict:
    return _FAMILY.build_schema()


def _handle_bound(runtime: "SoulRuntimePort", manual_source: Any, args: Mapping[str, Any] | None) -> dict:
    raw = dict(args or {})
    raw.pop("_tc_id", None)
    action = raw.get("action")
    result = _build_family(runtime, manual_source).handle(raw)
    if action == "manual" and "content" in result:
        return _adapt_manual_result(result)
    if result.get("error_code") == "ACTION_REQUIRED":
        return {
            "error": (
                f"Unknown soul action: {action if action is not None else ''}. "
                "Use inquiry, config, voice, dismiss, settings, manual, or wait "
                "for flow (mechanical)."
            )
        }
    return result


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    runtime = host.soul_runtime
    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=lambda args: _handle_bound(runtime, host.workdir, args),
        description=get_description(),
        glossary_package=__package__,
    )


DECLARATION = ToolPluginDeclaration(
    name="soul",
    actions=SOUL_DECLARED_ACTIONS,
    input_schemas=_DECLARED_INPUT_SCHEMAS,
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="soul-manual",
    description=_DESCRIPTION,
    binder=_bind,
    requires=("workdir", "soul_runtime"),
    glossary_package=__package__,
    settings=True,
)

SOUL_ACTIONS: tuple[str, ...] = DECLARATION.public_actions
ACTION_INPUT_SCHEMAS = DECLARATION.public_input_schemas()
_FAMILY = _build_family(None)


def setup(agent: Any, **_ignored: Any) -> None:
    """Mount Soul through the controlled official host-plugin registrar."""
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    register_agent_tool_plugins(agent, [DECLARATION])


def handle(agent: Any, args: dict) -> dict:
    """Compatibility entry for kernel hooks and focused legacy tests.

    Production Agent composition mounts :data:`DECLARATION`; this bridge keeps
    direct callers on the same family and runtime port without giving the
    declaration binder an Agent.
    """
    runtime = _coerce_runtime(agent)
    return _handle_bound(runtime, runtime, args)


# Kernel-facing intrinsic hook compatibility. BaseAgent resolves these exports
# through its injected intrinsic registry; each adapts the live Agent once and
# the real implementation thereafter sees only SoulRuntimePort.
def _start_soul_timer(agent: Any) -> None:
    _start_soul_timer_impl(_coerce_runtime(agent))


def _cancel_soul_timer(agent: Any) -> None:
    _cancel_soul_timer_impl(_coerce_runtime(agent))


def _soul_whisper(agent: Any) -> None:
    _soul_whisper_impl(_coerce_runtime(agent))


def _persist_soul_entry(agent: Any, result: dict, mode: str = "flow", source: str = "agent") -> None:
    _persist_soul_entry_impl(_coerce_runtime(agent), result, mode=mode, source=source)


def _append_soul_flow_record(agent: Any, record: dict) -> None:
    _append_soul_flow_record_impl(_coerce_runtime(agent), record)


def _flatten_v3_for_pair(agent: Any, voice: dict) -> dict:
    return _flatten_v3_for_pair_impl(_coerce_runtime(agent), voice)


def _run_consultation_fire(agent: Any) -> None:
    _run_consultation_fire_impl(_coerce_runtime(agent))


def _soul_fire_allowed(agent: Any) -> bool:
    return _soul_fire_allowed_impl(_coerce_runtime(agent))


def _rehydrate_appendix_tracking(agent: Any) -> None:
    _rehydrate_appendix_tracking_impl(_coerce_runtime(agent))


def soul_inquiry(agent: Any, question: str) -> dict | None:
    return _soul_inquiry_impl(_coerce_runtime(agent), question)


def _run_inquiry(agent: Any, question: str, source: str = "agent") -> None:
    _run_inquiry_impl(_coerce_runtime(agent), question, source=source)


def _render_current_diary(agent: Any) -> str:
    return _render_current_diary_impl(_coerce_runtime(agent))


def _write_soul_tokens(agent: Any, response: Any) -> None:
    _write_soul_tokens_impl(_coerce_runtime(agent), response)


def _list_snapshot_paths(agent: Any):
    return _list_snapshot_paths_impl(_coerce_runtime(agent))


def _run_consultation(agent: Any, iface: Any, source: str) -> dict | None:
    return _run_consultation_impl(_coerce_runtime(agent), iface, source)


def _run_consultation_batch(agent: Any) -> list[dict]:
    return _run_consultation_batch_impl(_coerce_runtime(agent))


def _send_with_timeout(agent: Any, session: Any, content: "str | list"):
    return _send_with_timeout_impl(_coerce_runtime(agent), session, content)


def _build_consultation_cue(agent: Any, kind: str, diary: str) -> str:
    return _build_consultation_cue_impl(_coerce_runtime(agent), kind, diary)


def build_consultation_pair(agent: Any, voices: list[dict], tc_id: str | None = None):
    return _build_consultation_pair_impl(_coerce_runtime(agent), voices, tc_id=tc_id)


def _persist_soul_config(agent: Any, new_values: dict) -> str | None:
    return _persist_soul_config_impl(_coerce_runtime(agent), new_values)


def _persist_soul_voice(agent: Any, *, voice: str, voice_prompt: str) -> str | None:
    return _persist_soul_voice_impl(_coerce_runtime(agent), voice=voice, voice_prompt=voice_prompt)


def _build_soul_system_prompt(agent: Any, kind: str = "inquiry") -> str:
    return _build_soul_system_prompt_impl(_coerce_runtime(agent), kind=kind)
