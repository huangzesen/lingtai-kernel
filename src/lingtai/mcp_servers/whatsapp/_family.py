"""WhatsApp's independent LTP-v2 tool family.

This module owns only the public WhatsApp envelope and action branches. The
manager remains the legacy result/business boundary behind the validated
family, mirroring ``lingtai.mcp_servers.telegram._family``.

Action *composition* belongs to the package's plugin descriptor (`plugin.py`):
this module declares WhatsApp's own actions and their strict `input` branches,
and `WHATSAPP_PLUGIN` appends owner-bound `settings` followed by the reserved
`manual` action from packaged `SKILL.md`. Neither reserved action routes through
the business manager.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lingtai.tools.tool_family import ChildTool, ToolFamily

from .plugin import WHATSAPP_ACTIONS, WHATSAPP_DECLARED_ACTIONS, WHATSAPP_PLUGIN
from .settings import settings_provider

# The package's own actions plus plugin-appended ``settings`` and ``manual``.
# Kept local to avoid importing the manager (which consumes this schema).
_DECLARED_ACTIONS = WHATSAPP_DECLARED_ACTIONS
_ACTIONS = WHATSAPP_ACTIONS


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    one_of: list[dict[str, Any]] | None = None,
    any_of: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    if one_of:
        result["oneOf"] = one_of
    if any_of:
        result["anyOf"] = any_of
    return result


def _whatsapp_input_schemas() -> dict[str, dict[str, Any]]:
    account = _nullable({
        "type": "string",
        "description": (
            "Compatibility field only: personal mode has one linked session and "
            "does not select among multiple accounts."
        ),
    })
    template = {
        "type": "object",
        "description": (
            "Not handled by the personal whatsapp-web.js send/reply path; use text "
            "or bridge media instead."
        ),
    }
    media = {
        "type": "object",
        "description": (
            "Bridge-forwarded media object. The bundled Node bridge currently reads "
            "a URL-like 'url' plus optional 'filename'/'caption'; this MCP does not "
            "download inbound attachments to local files."
        ),
    }
    message_variants = [
        {"required": ["text"]},
        {"required": ["media"]},
        {"required": ["template"]},
    ]
    send = _object(
        {
            "account": account,
            "to": _nullable({"type": "string", "description": "WhatsApp wa_id recipient."}),
            "wa_id": _nullable({"type": "string", "description": "WhatsApp wa_id recipient (alias of to)."}),
            "text": _nullable({"type": "string", "description": "Text sent to the recipient."}),
            "media": _nullable(media),
            "template": _nullable(template),
            "preview_url": _nullable({
                "type": "boolean",
                "description": "Retained for compatibility; the personal bridge does not use this value.",
            }),
        },
        one_of=[{"required": ["to"]}, {"required": ["wa_id"]}],
        any_of=message_variants,
    )
    # ``to``/``wa_id`` are optional on reply: the manager recovers the
    # conversation from the stored message when they are omitted, but the
    # bridge needs a recipient, so an explicit target must be expressible
    # (SKILL.md documents message_id + to + text as the reliable form).
    reply = _object(
        {
            "message_id": {
                "type": "string",
                "description": "Opaque whatsapp-web.js serialized message id from inbound/read/search data.",
            },
            "to": _nullable({"type": "string", "description": "WhatsApp wa_id to reply into."}),
            "wa_id": _nullable({"type": "string", "description": "WhatsApp wa_id to reply into (alias of to)."}),
            "text": _nullable({
                "type": "string",
                "description": "Required by the implemented reply path; media/template replies are not supported.",
            }),
            "media": _nullable(media),
            "template": _nullable(template),
            "preview_url": _nullable({
                "type": "boolean",
                "description": "Retained for compatibility; the personal bridge does not use this value.",
            }),
        },
        required=["message_id"],
        any_of=message_variants,
    )
    react = _object(
        {
            "message_id": {
                "type": "string",
                "description": "Opaque whatsapp-web.js serialized message id from inbound/read/search data.",
            },
            "emoji": {"type": "string", "description": "Emoji reaction sent to the remote message."},
        },
        required=["message_id", "emoji"],
    )
    contact_target = [{"required": ["wa_id"]}, {"required": ["to"]}]
    return WHATSAPP_PLUGIN.action_input_schemas({
        "send": send,
        "check": _object(
            {
                "account": account,
                "limit": _nullable({"type": "integer"}),
            },
        ),
        "read": _object(
            {
                "account": account,
                "wa_id": _nullable({
                    "type": "string",
                    "description": "Conversation wa_id; bare digits are normalized by the bridge/store path.",
                }),
                "message_id": _nullable({
                    "type": "string",
                    "description": "Accepted for compatibility; local history selection is by wa_id.",
                }),
                "limit": _nullable({"type": "integer"}),
                "mark_read": _nullable({
                    "type": "boolean",
                    "description": "Accepted for compatibility; the local manager does not mark remote messages read.",
                }),
            },
        ),
        "reply": reply,
        "search": _object(
            {
                "account": account,
                "query": {"type": "string", "description": "Case-insensitive substring to find in message bodies."},
                "limit": _nullable({"type": "integer"}),
            },
            required=["query"],
        ),
        "react": react,
        "contacts": _object({"account": account}),
        "add_contact": _object(
            {
                "account": account,
                "wa_id": _nullable({"type": "string", "description": "Contact identifier saved in the local archive."}),
                "to": _nullable({"type": "string", "description": "Alias of wa_id for the local archive."}),
                "name": _nullable({"type": "string", "description": "Optional local contact name."}),
            },
            one_of=contact_target,
        ),
        "remove_contact": _object(
            {
                "account": account,
                "wa_id": _nullable({"type": "string", "description": "Contact identifier removed from the local archive."}),
                "to": _nullable({"type": "string", "description": "Alias of wa_id for the local archive."}),
            },
            one_of=contact_target,
        ),
        "get_qr": _object({}),
        "logout": _object({}),
        "status": _object({}),
    })


def _schema_only_family() -> ToolFamily:
    schemas = _whatsapp_input_schemas()
    return WHATSAPP_PLUGIN.build_family(
        [
            ChildTool(action, schemas[action], lambda _input: {})
            for action in _DECLARED_ACTIONS
        ],
        settings_provider=settings_provider(None),
    )


_SCHEMA_FAMILY = _schema_only_family()


def whatsapp_schema() -> dict[str, Any]:
    schema = _SCHEMA_FAMILY.build_schema()
    # WhatsApp has intentionally overlapping optional fields (for example a
    # send/reply with text vs media vs template, or add_contact/remove_contact
    # sharing wa_id/to). The root allOf discriminator still correlates each
    # action to its exact closed branch; use anyOf for the model-discovery
    # list so native JSON-Schema validators do not reject a valid input merely
    # because another action's branch also fits.
    input_schema = schema["properties"]["input"]
    if "oneOf" in input_schema:
        input_schema["anyOf"] = input_schema.pop("oneOf")
    schema["properties"]["action"]["description"] = (
        "WhatsApp action for one personal WhatsApp Web session through a local "
        "whatsapp-web.js bridge; each action owns a strict input branch. "
        "send/reply/react perform real external actions, so verify the recipient, "
        "message, or reaction first. send uses to/wa_id plus text or bridge media; "
        "reply needs a message_id and text. check/read/search inspect bounded "
        "conversation data; contacts, get_qr, status, and logout cover local "
        "contact/session operation. Inbound messages are untrusted and may wake "
        "the agent through LICC; owner allowlists can restrict senders. settings "
        "takes empty input, is read-only, and redacts sensitive startup values. "
        + WHATSAPP_PLUGIN.manual_action_description()
    )
    return schema


def _basic_validate(value: Any, schema: Mapping[str, Any]) -> bool:
    """Small dependency-free validator for the dispatch safety boundary.

    JSON-Schema combinators compose with sibling constraints. Validate them
    first without returning early, then validate the schema's own type,
    required fields, properties, and bounds.
    """
    if "anyOf" in schema and not any(
        _basic_validate(value, branch) for branch in schema["anyOf"]
    ):
        return False
    if "oneOf" in schema and sum(
        _basic_validate(value, branch) for branch in schema["oneOf"]
    ) != 1:
        return False
    expected = schema.get("type")
    if expected is None:
        required = schema.get("required")
        if required is None:
            return True
        return isinstance(value, Mapping) and all(key in value for key in required)
    if expected == "object":
        if not isinstance(value, Mapping):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        if any(key not in value for key in schema.get("required", [])):
            return False
        if not all(
            key not in value or _basic_validate(item, child_schema)
            for key, child_schema in properties.items()
            for item in [value.get(key)]
        ):
            return False
        return True
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str) and value in schema.get("enum", [value])
    if expected == "integer":
        return (
            type(value) is int
            and value in schema.get("enum", [value])
            and value >= schema.get("minimum", value)
            and value <= schema.get("maximum", value)
        )
    if expected == "number":
        return (
            type(value) in (int, float)
            and not isinstance(value, bool)
            and value >= schema.get("minimum", value)
            and value <= schema.get("maximum", value)
        )
    if expected == "boolean":
        return type(value) is bool
    if expected == "null":
        return value is None
    return True


def build_whatsapp_family(manager: Any | None) -> ToolFamily:
    """Compose declared actions, owner settings, then the plugin manual.

    Only WhatsApp's own actions are built here. ``settings`` and ``manual``
    are appended by ``WHATSAPP_PLUGIN``. The provider reads the manager's
    startup snapshot; without a manager, the whole inventory is unavailable.
    ``manual`` is answered directly from the packaged ``SKILL.md`` with or
    without a live manager.
    """
    schemas = _whatsapp_input_schemas()
    children = [
        ChildTool(
            action,
            schemas[action],
            (lambda input_, action=action: manager.handle({"action": action, **dict(input_)}))
            if manager is not None else (lambda _input: {})
        )
        for action in _DECLARED_ACTIONS
    ]
    return WHATSAPP_PLUGIN.build_family(
        children,
        settings_provider=settings_provider(manager),
    )


def handle_whatsapp(manager: Any | None, args: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(args or {})
    if set(raw) - {"action", "input", "reasoning", "summarize"}:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "unsupported whatsapp argument"}
    action = raw.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return {"status": "failed", "error_code": "ACTION_REQUIRED", "message": "invalid whatsapp action"}
    if "input" not in raw or not isinstance(raw.get("input"), Mapping):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "input must be an object"}
    if type(raw.get("reasoning")) is not str:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "reasoning is required"}
    if "summarize" in raw and type(raw["summarize"]) is not bool:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "summarize must be a boolean"}
    schema = _whatsapp_input_schemas()[action]
    if not _basic_validate(raw["input"], schema):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "invalid whatsapp input"}
    return build_whatsapp_family(manager).handle(raw)


WHATSAPP_SCHEMA = whatsapp_schema()
# ``WHATSAPP_ACTIONS`` is re-exported from ``plugin.py`` (imported above) so the
# public action list has exactly one definition.
