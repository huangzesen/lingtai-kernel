"""Schema data — canonical per-action input schemas and prose for ``notification``.

The notification tool exposes ``check``, the three atomic dismiss verbs
(``dismiss_channel``, ``dismiss_event``, ``dismiss_ref``), read-only settings
discovery, and the strictly read-only progressive-disclosure action ``manual``.
``summarize`` is *not* a notification verb — it remains a ``system`` action;
the root ``summarize`` boolean is the cross-cutting LTP v2 result-post-processing
control, not an action.

This module holds only data: each action's own canonical strict
``input_schema`` (:data:`INPUT_SCHEMAS`) and the canonical English prose.
``__init__.py`` composes these into the public model-facing schema via the
generic ``ToolFamily`` infra (``lingtai.tools.tool_family``) — see
``__init__.py::_schema_only_family``/``get_schema``. ``lang`` is accepted on
:func:`get_description` and :func:`get_schema` for source compatibility and
does not select localized aliases; schema prose is canonical English,
language-independent.

Optional dismiss fields are declared in the provider-compatible nullable
representation (``"type": ["string", "null"]`` plus membership in
``required``) per ``tools/CONTRACT.md`` "Envelope": strict OpenAI schemas
have no other way to express an optional field. ``__init__.py`` strips those
nulls back to *absent* before the pre-existing dismiss handlers run, so
``args.get("channel", "system")``-style defaulting is preserved exactly.
"""
from __future__ import annotations

from typing import Any

from ..tool_family.manual import MANUAL_INPUT_SCHEMA

LARGE_RESULT_DISMISS_ACTION_NOTE = (
    "Legacy: the kernel no longer raises large_tool_result reminders — large "
    "results are ranked under _meta.agent_meta.agent_state.current_tool_result_chars and "
    "compacted via system(action=summarize). Any large_tool_result event still "
    "present (e.g. persisted before this change or pre-molt) can be dismissed "
    "as an escape hatch. Dismissal only clears the notification surface; the "
    "original result stays in chat history and events.jsonl. See "
    "notification-manual."
)

LARGE_RESULT_FORCE_NOTE = (
    "Does not affect large_tool_result reminder dismissal; that escape hatch "
    "is always allowed and clears only the reminder surface."
)

# The canonical action order. This is the single source for the schema's
# ``action`` enum order, the ``input`` disclosure/``allOf`` branch order, and the
# child registration order in ``__init__.py`` — one list, not three.
# Read/clear actions keep the pre-existing prefix stable; hook-registry
# management (add/drop/edit/list) is administrative and follows.
NOTIFICATION_DECLARED_ACTIONS = (
    "check",
    "dismiss_channel",
    "dismiss_event",
    "dismiss_ref",
    "add",
    "drop",
    "edit",
    "list",
    "delay",
)

# The official declaration injects the kernel-reserved settings action before
# manual. Keep the full public order available to documentation/import-time
# consumers while leaving both reserved children to generic composition.
ACTION_ORDER = (*NOTIFICATION_DECLARED_ACTIONS, "settings", "manual")

_CHANNEL_DESCRIPTION = (
    "Notification channel to act on (e.g. soul, system, mcp.telegram). "
    "Required for dismiss_channel; for dismiss_event/dismiss_ref it defaults "
    "to 'system'. For producer-owned channels like email, prefer the "
    "producer's own verb (email(read/dismiss))."
)

_FORCE_DESCRIPTION = (
    "Optional for dismiss verbs. When true, bypasses a producer-registered "
    "generic-dismiss guard and the stale-channel-version refusal. Use only "
    "when knowingly clearing a stale mirror; producer-owned state is never "
    "changed. " + LARGE_RESULT_FORCE_NOTE
)

_REASON_DESCRIPTION = (
    "Optional acknowledgement reason, logged to the event log. Required when "
    "dismissing the post-molt continuation channel (use "
    "reason='<continue|defer|obsolete>: ...')."
)

_CHECK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_HOOK_NAME_DESCRIPTION = (
    "Unique hook name within this agent (e.g. 'comm_watcher'). Required for "
    "add/drop/edit; used to match manifests in .notification/hooks.json."
)

_HOOK_CHANNEL_DESCRIPTION = (
    "Channel the hook publishes into (e.g. 'mcp.comm_watcher'). Must match "
    "the .notification/<channel>.json file the hook writes. Registering it "
    "here allowlists it for this agent."
)

_HOOK_STRING_FIELD_DESCRIPTION = "Human-readable field on the hook manifest."

_ADD_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": _HOOK_NAME_DESCRIPTION},
        "channel": {"type": "string", "description": _HOOK_CHANNEL_DESCRIPTION},
        "source": {
            "type": "string",
            "description": "Who/what produces the notifications (e.g. 'comm_watcher.py').",
        },
        "description": {
            "type": "string",
            "description": "What this hook watches and why (one or two sentences).",
        },
        "how_to_modify": {
            "type": "string",
            "description": "How to change the hook (config file, env var, command).",
        },
        "how_to_cancel": {
            "type": "string",
            "description": "How to stop the hook (pid/kill, unregister, config toggle).",
        },
        "version": {
            "type": ["string", "null"],
            "description": "Optional manifest version (default '1.0.0').",
        },
        "instructions": {
            "type": ["string", "null"],
            "description": "Optional agent-facing handling guidance for this hook's notifications.",
        },
    },
    "required": [
        "name",
        "channel",
        "source",
        "description",
        "how_to_modify",
        "how_to_cancel",
        "version",
        "instructions",
    ],
    "additionalProperties": False,
}

_DROP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": _HOOK_NAME_DESCRIPTION}
    },
    "required": ["name"],
    "additionalProperties": False,
}

_EDIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": _HOOK_NAME_DESCRIPTION},
        "version": {"type": ["string", "null"], "description": _HOOK_STRING_FIELD_DESCRIPTION},
        "source": {"type": ["string", "null"], "description": _HOOK_STRING_FIELD_DESCRIPTION},
        "description": {"type": ["string", "null"], "description": _HOOK_STRING_FIELD_DESCRIPTION},
        "channel": {"type": ["string", "null"], "description": _HOOK_CHANNEL_DESCRIPTION},
        "how_to_modify": {"type": ["string", "null"], "description": _HOOK_STRING_FIELD_DESCRIPTION},
        "how_to_cancel": {"type": ["string", "null"], "description": _HOOK_STRING_FIELD_DESCRIPTION},
        "instructions": {"type": ["string", "null"], "description": _HOOK_STRING_FIELD_DESCRIPTION},
    },
    "required": ["name", "version", "source", "description", "channel", "how_to_modify", "how_to_cancel", "instructions"],
    "additionalProperties": False,
}

_LIST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_DELAY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "channel": {
            "type": "string",
            "description": (
                "Allowed notification channel whose consumer delivery is temporarily "
                "hidden. delay-alarm is an alarm mirror and cannot be delayed."
            ),
        },
        "seconds": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Delay duration. 0 cancels the currently delayed channel; a nonzero "
                "value is bounded by live LINGTAI_NOTIFICATION_DELAY_MAX_SECONDS "
                "(default 600 seconds)."
            ),
        },
    },
    "required": ["channel", "seconds"],
    "additionalProperties": False,
}

_DISMISS_CHANNEL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "channel": {"type": "string", "description": _CHANNEL_DESCRIPTION},
        "force": {"type": ["boolean", "null"], "description": _FORCE_DESCRIPTION},
        "reason": {"type": ["string", "null"], "description": _REASON_DESCRIPTION},
    },
    "required": ["channel", "force", "reason"],
    "additionalProperties": False,
}

_DISMISS_EVENT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "event_id": {
            "type": "string",
            "description": (
                "Remove only the matching system notification event_id from "
                ".notification/system.json instead of the whole channel."
            ),
        },
        "channel": {"type": ["string", "null"], "description": _CHANNEL_DESCRIPTION},
        "force": {"type": ["boolean", "null"], "description": _FORCE_DESCRIPTION},
        "reason": {"type": ["string", "null"], "description": _REASON_DESCRIPTION},
    },
    "required": ["event_id", "channel", "force", "reason"],
    "additionalProperties": False,
}

_DISMISS_REF_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref_id": {
            "type": "string",
            "description": (
                "Remove system notification event(s) carrying this producer "
                "ref_id from .notification/system.json instead of the whole "
                "channel."
            ),
        },
        "channel": {"type": ["string", "null"], "description": _CHANNEL_DESCRIPTION},
        "force": {"type": ["boolean", "null"], "description": _FORCE_DESCRIPTION},
        "reason": {"type": ["string", "null"], "description": _REASON_DESCRIPTION},
    },
    "required": ["ref_id", "channel", "force", "reason"],
    "additionalProperties": False,
}

# Per-action strict schemas for the actions this package itself declares.
# ``settings`` and ``manual`` are deliberately absent: the kernel declaration
# injects their canonical shared schemas and the dispatching family composes the
# matching children. Keeping them absent here prevents package-owned actions
# from drifting into either reserved slot.
DECLARED_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "add": _ADD_INPUT_SCHEMA,
    "drop": _DROP_INPUT_SCHEMA,
    "edit": _EDIT_INPUT_SCHEMA,
    "list": _LIST_INPUT_SCHEMA,
    "delay": _DELAY_INPUT_SCHEMA,
    "check": _CHECK_INPUT_SCHEMA,
    "dismiss_channel": _DISMISS_CHANNEL_INPUT_SCHEMA,
    "dismiss_event": _DISMISS_EVENT_INPUT_SCHEMA,
    "dismiss_ref": _DISMISS_REF_INPUT_SCHEMA,
}

# Compatibility/readability view of the complete public shape.  The actual
# declaration is built in ``notification.__init__`` from
# ``DECLARED_INPUT_SCHEMAS``; it reuses this imported canonical manual schema,
# rather than treating this map as a second source of truth.
INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    **DECLARED_INPUT_SCHEMAS,
    "settings": dict(_CHECK_INPUT_SCHEMA),
    "manual": MANUAL_INPUT_SCHEMA,
}

ACTION_ENUM_DESCRIPTION = (
    "check: read all notification channels (input={}). Returns a placeholder; " +
    "the live payload is stamped onto this same result under " +
    "`_meta.agent_meta.notifications.attention` and " +
    "`_meta.agent_meta.guidance.transient`. Replace-only — do not call " +
    "voluntarily after handling; dismiss instead, preferably coalesced with " +
    "other tool work you already need this turn." +

    "dismiss_channel: clear one notification channel whole (input={'channel': " +
    "'<name>', ...}). Prefer a producer-specific verb first (e.g. " +
    "email(read/dismiss)); guarded channels require force=true only for stale " +
    "mirrors. Does not accept event_id/ref_id — use dismiss_event/dismiss_ref " +
    "for those." +

    "dismiss_event: remove a single system event by event_id from " +
    ".notification/system.json (channel defaults to 'system' when null)." +

    "dismiss_ref: remove system event(s) by ref_id from " +
    ".notification/system.json (channel defaults to 'system' when null)." +

    "add: register an external hook (input={'name', 'channel', 'source', " +
    "'description', 'how_to_modify', 'how_to_cancel', ...}), writing the " +
    "manifest to .notification/hooks.json and allowlisting its channel. " +
    "Agent self-service: add your own hooks." +

    "drop: unregister a hook by name (input={'name': ...}); removes its " +
    "manifest and revokes the channel from the effective allowlist. Never " +
    "kills the hook process itself — use the manifest's how_to_cancel." +

    "edit: update one hook's fields by name (input={'name': ..., ...fields}); " +
    "channel edits re-validate uniqueness." +

    "list: return the registered hook manifests (input={}), showing what is " +
    "whitelisted and how each hook is modified/cancelled." +

    "delay: temporarily hide one allowed target channel from consumer delivery " +
    "(input={'channel': '<name>', 'seconds': 0 or a live-configured positive " +
    "cap}); 0 cancels the current matching delay. Target producer state is " +
    "never changed; expiry re-exposes it and raises one high-priority " +
    "delay-alarm mirror. delay-alarm itself cannot be targeted." +

    "settings: show the Notification-owned effective settings as exact " +
    "key/current/default/configurable/comment rows (input={}); read-only. " +
    "Read the comment-targeted manual sections for meaning/change procedures." +

    "manual: call notification(action='manual', input={}) to return the " +
    "installed notification-manual skill body. Strictly read-only."
) + "\n\n" + LARGE_RESULT_DISMISS_ACTION_NOTE


def get_description(lang: str = "en") -> str:
    return "Notification surface — inspect and acknowledge the agent's notification channels, and manage external-hook registrations. Self-actions, no permissions needed. This is the only tool that exposes notification verbs; the system tool no longer offers notification or dismiss aliases. Every call takes action + input + reasoning; input is the strict argument object for the selected action. Prefer a producer's own read/dismiss (e.g. email) over the generic dismiss verbs here when one exists; check is inspection only, not a clearing operation. Guarded channels require force=true only for known-stale mirrors, and dismissal after a molt needs a continuation reason. add/edit/drop/list manage the hook manifest only — drop never stops the hook's own process. delay suppresses consumer delivery only for one allowed channel (0 cancels) and cannot target delay-alarm. Use notification(action='settings', input={}, reasoning='...') and notification(action='manual', input={}, reasoning='...') for the read-only settings rows and the installed manual — neither changes notification state, and call manual with summarize=false. Context compaction is not here — use context(action='summarize')."


# NOTE: ``get_schema`` is deliberately NOT defined here. The model-facing
# schema is *composed* from the data above by the generic ``ToolFamily``
# infra, and that composition lives next to the child registry it is
# generated from — ``__init__.py::get_schema``. Defining a second one here
# would either duplicate the composition or import the package back into its
# own data module. ``__init__.py`` re-exports the composed ``get_schema``, so
# ``lingtai.tools.notification.get_schema`` (the intrinsic protocol entry
# point ``_build_tool_schemas`` actually calls) is unchanged.
