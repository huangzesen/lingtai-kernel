"""Schema data — canonical per-action input schemas and prose for ``system``.

The system tool is an LTP v2 family (``../CONTRACT.md``): the model-facing root
is the closed ``action`` + ``input`` + ``reasoning`` + ``summarize`` envelope,
and each action's arguments live in its own strict ``input`` object.

This module holds only data: each action's own canonical strict
``input_schema`` (:data:`INPUT_SCHEMAS`) and the canonical English prose.
``__init__.py`` composes these into the public model-facing schema via the
generic ``ToolFamily`` infra (``lingtai.tools.tool_family``) — see
``__init__.py::_build_children``/``get_schema``. ``lang`` is accepted on
:func:`get_description` for source compatibility and does not select localized
aliases; schema prose is canonical English, language-independent.

The migration moved the six pre-migration flat sibling fields (``reason``,
``address``, ``preset``, ``revert_preset``, ``rebuild``, ``items``) into the
``input`` object of exactly the actions that read them. Their descriptions,
types, and defaults are carried over verbatim; no action was added, removed,
renamed, or reordered, and no field changed meaning.

Two fields deserve a note because they are not a straight carry-over of the
pre-migration *schema*:

* ``sleep.force`` was always read by ``karma._sleep`` (the kernel#112 escape
  hatch) but never advertised in the flat schema. A strict child ``input``
  must declare every key its handler accepts, or dispatch would reject a call
  that succeeds today, so it is declared here. This surfaces an existing
  behavior; it does not add one.
* ``notification_threshold_chars`` is deliberately still absent. It is
  config-only (``manifest.summarize_notification_threshold`` + refresh), and
  ``summarize._summarize``'s loud runtime-mutation refusal is retained as the
  inner layer for direct in-process callers that bypass the envelope.

Optional fields are declared in the provider-compatible nullable
representation (``"type": ["string", "null"]`` plus membership in
``required``) per ``tools/CONTRACT.md`` "Envelope": strict OpenAI schemas have
no other way to express an optional field. ``__init__.py`` strips those nulls
back to *absent* before the pre-existing handlers run, so
``args.get("reason", "")``-style defaulting is preserved exactly.
"""
from __future__ import annotations

from typing import Any

from ..tool_family.manual import MANUAL_INPUT_SCHEMA
from .plugin import SYSTEM_DECLARED_ACTIONS

# Canonical compatibility order for the eleven operational actions plus
# ``manual``. The ToolPlugin declaration owns operational child registration;
# ToolFamily's reserved settings-provider opt-in mechanically inserts
# ``settings`` immediately before ``manual`` in the final model-facing family.
ACTION_ORDER = (*SYSTEM_DECLARED_ACTIONS, "manual")

# --- Shared field descriptions, carried over verbatim from the flat schema ---

_REASON_DESCRIPTION = (
    "Reason for sleep, refresh, or clear (logged to the event log; for clear, "
    "becomes the source tag in the recovery summary)."
)

_ADDRESS_DESCRIPTION = (
    "Target agent's address (working directory path). Required for interrupt, "
    "lull, suspend, cpr, clear, nirvana."
)

_PRESET_DESCRIPTION = (
    "Optional preset to swap to before refreshing. A preset is a {LLM, "
    "capabilities} bundle from your library. Use action='presets' to list. "
    "Swap is light and reversible. If current context exceeds target preset's "
    "context_limit, swap is refused — molt first."
)

_REVERT_PRESET_DESCRIPTION = (
    "Optional. Pass true with action='refresh' to swap back to your default "
    "preset (manifest.preset.default — typically the one your agent was "
    "created with). Cannot be used together with the 'preset' argument. "
    "Useful as a 'home button' after experimenting with another preset, "
    "without needing to remember your default's name. Errors if no default is "
    "configured."
)

_FORCE_DESCRIPTION = (
    "Optional for action='sleep'. When true, go to sleep even though unread "
    "notifications are already waiting on disk. The default (false) refuses "
    "the transition instead, so mail that arrived during this same turn is "
    "not silently slept through. Use only when you knowingly want to sleep "
    "anyway."
)

def _address_input_schema() -> dict[str, Any]:
    """Build the shared input shape for the six address-taking verbs.

    ``lull``/``interrupt``/``suspend``/``cpr``/``clear``/``nirvana`` take
    exactly the same two fields, so they are generated rather than restated
    six times. Each call returns a fresh dict so no two children share a
    mutable schema container.
    """
    return {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": _ADDRESS_DESCRIPTION},
            "reason": {
                "type": ["string", "null"],
                "description": _REASON_DESCRIPTION,
            },
        },
        "required": ["address", "reason"],
        "additionalProperties": False,
    }


_REFRESH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": ["string", "null"], "description": _REASON_DESCRIPTION},
        "preset": {"type": ["string", "null"], "description": _PRESET_DESCRIPTION},
        "revert_preset": {
            "type": ["boolean", "null"],
            "description": _REVERT_PRESET_DESCRIPTION,
        },
    },
    "required": ["reason", "preset", "revert_preset"],
    "additionalProperties": False,
}

_SLEEP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": ["string", "null"], "description": _REASON_DESCRIPTION},
        "force": {"type": ["boolean", "null"], "description": _FORCE_DESCRIPTION},
        "delay": {
            "type": ["number", "null"],
            "exclusiveMinimum": 0,
            "description": (
                "Optional finite positive seconds until one ordinary system "
                "notification alarm. Use only as a last-resort for async work "
                "without a reliable completion notification, not as normal "
                "waiting (use IDLE). An early wake does not cancel it; a later "
                "sleep delay replaces it. Null omits it; no maximum is imposed."
            ),
        },
    },
    "required": ["reason", "force", "delay"],
    "additionalProperties": False,
}

_PRESETS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# The two name actions, moved here from the dissolved ``psyche`` family. Both
# take exactly one field, ``content``, matching the pre-move shape and
# semantics. They are identity *runtime state* (live in-memory identity, the
# persisted ``.agent.json``, and the protected prompt ``identity`` section) —
# NOT raw init/config editing, and NOT the physical agent address/workdir
# rename, which remains the operator migration workflow in ``system-manual``.
_NAME_SET_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "Your chosen true name (真名). Set ONCE and immutable "
                "thereafter — a second name_set is refused. Use name_nickname "
                "for a changeable display name."
            ),
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

_NAME_NICKNAME_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": (
                "Your chosen nickname (别名) — mutable, unlike the true name. "
                "Pass an empty string to clear it."
            ),
        },
    },
    "required": ["content"],
    "additionalProperties": False,
}

INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "refresh": _REFRESH_INPUT_SCHEMA,
    "sleep": _SLEEP_INPUT_SCHEMA,
    "lull": _address_input_schema(),
    "interrupt": _address_input_schema(),
    "suspend": _address_input_schema(),
    "cpr": _address_input_schema(),
    "clear": _address_input_schema(),
    "nirvana": _address_input_schema(),
    "presets": _PRESETS_INPUT_SCHEMA,
    "name_set": _NAME_SET_INPUT_SCHEMA,
    "name_nickname": _NAME_NICKNAME_INPUT_SCHEMA,
    # Referenced, not restated: ``build_manual_child`` owns this literal, so
    # the schema-only family and the dispatching family cannot drift
    # (``tool_family/CONTRACT.md`` Contract rules).
    "manual": MANUAL_INPUT_SCHEMA,
}

# The per-action prose the model reads to choose an action. Carried over
# verbatim from the pre-migration flat ``action`` enum description, with the
# argument references restated in the ``input`` shape they now take.
ACTION_ENUM_DESCRIPTION = (
    "refresh: rebuild from init.json — same identity, preserved conversation, "
    "reloading MCP, capabilities, addons, LLM, and prompt sections. See "
    "system-manual.\n\n"
    "presets: list available presets with tags, connectivity, capabilities. "
    "See system-manual.\n\n"
    "sleep: go to sleep until mail wakes you (self only). Normal waiting uses "
    "reliable completion notifications and IDLE; delay is only a last-resort "
    "one-shot alarm for async work lacking one — an early wake does not "
    "cancel it, and a later delay replaces it.\n\n"
    "lull: put another agent to sleep (karma).\n\n"
    "suspend: freeze another agent (karma).\n\n"
    "cpr: resuscitate a suspended agent (karma).\n\n"
    "interrupt: cancel another agent's turn (karma).\n\n"
    "clear: force molt on another agent (karma). See system-manual.\n\n"
    "nirvana: permanently destroy an agent (karma + nirvana privilege). See "
    "system-manual.\n\n"
    "name_set: set your true name (真名) — input={'content': '<name>'}. "
    "Once-only and immutable; a second name_set is refused.\n\n"
    "name_nickname: set or change your display name (别名) — "
    "input={'content': '<nickname>'}, mutable; an empty string clears it. "
    "Neither name action renames your address or working directory — that "
    "is an operator migration, see system-manual.\n\n"
    "settings: read the effective System-owned settings (input={}). See "
    "system-manual for meaning and the authorized change procedure.\n\n"
    "Context hygiene is NOT here — use context(action='summarize') or "
    "context(action='rebuild').\n\n"
    "manual: return the installed system-manual skill (input={}) without "
    "changing runtime state."
)


def get_description(lang: str = "en") -> str:
    return (
        "Runtime inspection, lifecycle control, synchronization, and "
        "inter-agent management.\n\n"
        "Self-actions (no permissions needed): sleep, refresh, presets, "
        "name_set, name_nickname, settings, manual.\n"
        "Karma actions (require admin.karma=True): lull, interrupt, suspend, "
        "cpr, clear.\n"
        "Nirvana (requires admin.karma=True AND admin.nirvana=True): "
        "permanently destroys an agent and is irreversible. Never infer any "
        "of this authority from availability alone.\n\n"
        "Notification verbs (check/dismiss) live on the standalone "
        "notification tool, and context hygiene lives on "
        "context(action='summarize'|'rebuild'|'molt') — neither is here. "
        "Call system(action='manual', input={}) for the installed "
        "system-manual skill.\n\n"
        "presets and the complete settings inventory can be bulky, so root "
        "summarize=true may help when exact entries are unneeded; every "
        "other action returns a short receipt, so leave summarize false — "
        "including for manual, so the exact procedure is not summarized away."
    )
