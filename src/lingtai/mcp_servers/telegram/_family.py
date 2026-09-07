"""Telegram's independent LTP-v2 tool family.

This module owns only the public Telegram envelope and action branches.  The
manager remains the legacy result/business boundary behind the validated family.
Task Card has a separate family module and registry.

Action *composition* belongs to the package's plugin descriptor (`plugin.py`):
this module declares Telegram's own actions and their strict `input` branches,
and `TELEGRAM_PLUGIN` appends the reserved `manual` action from the packaged
`SKILL.md`. `manual` therefore never routes through the manager and cannot be
omitted, re-schema'd, or rebound to other material from here.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lingtai.tools.tool_family import ChildTool, ToolFamily

from . import updates as tg_updates
from .plugin import TELEGRAM_ACTIONS, TELEGRAM_DECLARED_ACTIONS, TELEGRAM_PLUGIN
from .settings import build_telegram_settings_provider

# The package's own actions plus the plugin-appended reserved ``manual``. Kept
# local to avoid importing the manager (which consumes this schema).
_DECLARED_ACTIONS = TELEGRAM_DECLARED_ACTIONS
_ACTIONS = TELEGRAM_ACTIONS

_RENDERING_MODES = ("plain_text", "HTML", "MarkdownV2", "Markdown", "entities", "rich")


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


def _chat_id(*, updates: bool = False) -> dict[str, Any]:
    choices: list[dict[str, Any]] = [{"type": "integer"}]
    if updates:
        choices.append({
            "type": "string",
            "enum": [tg_updates.SYNTHETIC_EVENTS_CHAT_ID],
        })
    return {"anyOf": choices}


def _telegram_input_schemas() -> dict[str, dict[str, Any]]:
    media = _object(
        {
            "type": {"type": "string", "enum": ["photo", "document"]},
            "path": {"type": "string"},
        },
        required=["type", "path"],
    )
    fact = _object(
        {"label": {"type": "string"}, "value": {"type": "string"}},
        required=["label", "value"],
    )
    structured_message = _object(
        {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "facts": {"type": "array", "items": fact},
            "bullets": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": {"type": "string"}},
            "code": _object(
                {"text": {"type": "string"}, "language": {"type": "string"}},
                required=["text"],
            ),
            "next": _object(
                {"label": {"type": "string"}, "text": {"type": "string"}},
                required=["label", "text"],
            ),
            "footer": {"type": "string"},
        },
        required=["title"],
    )
    send = _object(
        {
            "account": _nullable({"type": "string"}),
            "chat_id": _chat_id(),
            "text": {"type": "string"},
            "media": _nullable(media),
            "reply_markup": _nullable({"type": "object"}),
            "placeholder": _nullable({"type": "boolean"}),
            "chat_action": _nullable({
                "type": "string",
                "enum": ["typing", "upload_photo", "upload_document", "upload_voice", ""],
            }),
            "rendering_mode": {
                "type": "string", "enum": list(_RENDERING_MODES),
                "default": "Markdown",
            },
            "entities": _nullable({"type": "array"}),
            "caption_entities": _nullable({"type": "array"}),
            "structured_message": _nullable({
                **structured_message,
                "description": (
                    "System-generated semantic content sent as native Telegram Rich "
                    "Message blocks. Agent-authored emoji are preserved wherever they "
                    "improve scanning; use rendering_mode='rich' and omit text/media."
                ),
            }),
            "link_preview_options": _nullable({"type": "object"}),
            "disable_web_page_preview": _nullable({"type": "boolean"}),
        },
        required=["chat_id"],
        any_of=[
            {"required": ["text"]},
            {"required": ["media"]},
            {"required": ["chat_action"]},
            {"required": ["structured_message"]},
        ],
    )
    send["properties"]["media"]["anyOf"][0]["description"] = (
        "Media attachment: {type: 'photo'|'document', path: '/path/to/file'}. "
        "For charts, HTML/SVG/PNG reports, CSVs, PDFs, and other generated artifacts "
        "that should arrive as an intact file, use type='document'. Use type='photo' "
        "only for native inline photo previews; Telegram photo delivery can crop, "
        "compress, thumbnail, or display text-heavy charts poorly. Do not paste local "
        "file paths in message text as a substitute for attaching the file."
    )
    send["properties"]["rendering_mode"]["description"] = (
        "Default is Markdown for the agent's messages. Choose plain_text for "
        "unformatted text/chat actions, HTML/MarkdownV2 for other parse modes, "
        "entities when supplying MessageEntity data, or rich for a native structured "
        "message; you may omit it to use Markdown; do not combine modes."
    )
    send["properties"]["entities"]["anyOf"][0]["description"] = (
        "Telegram MessageEntity[] for rendering_mode='entities' on message text."
    )
    send["properties"]["caption_entities"]["anyOf"][0]["description"] = (
        "Telegram MessageEntity[] for rendering_mode='entities' on media captions."
    )
    send["properties"]["chat_action"]["anyOf"][0]["description"] = (
        "For send only. When set and no text/media is provided, sends a chat action "
        "indicator instead of a message. Auto-expires after 5 seconds; omit or pass "
        "an empty string for no chat action."
    )
    schemas = TELEGRAM_PLUGIN.action_input_schemas({
        "send": send,
        "check": _object({"account": _nullable({"type": "string"})}),
        "read": _object(
            {
                "account": {"type": "string"},
                "chat_id": _chat_id(updates=True),
                "limit": _nullable({"type": "integer"}),
            },
            required=["chat_id"],
        ),
        "reply": _object(
            {
                "message_id": {"type": "string"},
                "text": {"type": "string"},
                "rendering_mode": {
                    "type": "string", "enum": list(_RENDERING_MODES),
                    "default": "Markdown",
                    "description": "Default is Markdown; you may omit rendering_mode.",
                },
                "entities": _nullable({"type": "array"}),
                "structured_message": _nullable(structured_message),
            },
            required=["message_id"],
            any_of=[{"required": ["text"]}, {"required": ["structured_message"]}],
        ),
        "search": _object(
            {
                "query": {"type": "string"},
                "account": _nullable({"type": "string"}),
                "chat_id": _nullable(_chat_id(updates=True)),
            },
            required=["query"],
        ),
        "delete": _object({"message_id": {"type": "string"}}, required=["message_id"]),
        "edit": _object(
            {
                "message_id": {"type": "string"},
                "text": {"type": "string"},
                "reply_markup": _nullable({"type": "object"}),
                "rendering_mode": {
                    "type": "string", "enum": list(_RENDERING_MODES),
                    "default": "Markdown",
                    "description": "Default is Markdown; you may omit rendering_mode.",
                },
                "entities": _nullable({"type": "array"}),
                "structured_message": _nullable(structured_message),
            },
            required=["message_id"],
            any_of=[{"required": ["text"]}, {"required": ["structured_message"]}],
        ),
        "contacts": _object({"account": _nullable({"type": "string"})}),
        "add_contact": _object(
            {
                "account": _nullable({"type": "string"}),
                "chat_id": {"type": "integer"},
                "alias": {"type": "string"},
            },
            required=["chat_id", "alias"],
        ),
        "remove_contact": _object(
            {
                "account": _nullable({"type": "string"}),
                "chat_id": {"type": "integer"},
                "alias": {"type": "string"},
            },
            one_of=[{"required": ["alias"]}, {"required": ["chat_id"]}],
        ),
        "accounts": _object({}),
    })

    # These annotations are intentionally attached after the closed schemas are
    # assembled. They improve first-use tool discovery without changing action
    # names, requiredness, nullability, combinators, or dispatch inputs.
    branch_descriptions = {
        "send": (
            "Send a new message to a numeric Telegram chat. Provide text, media, "
            "a rich structured_message, or a chat_action; this is an external send."
        ),
        "check": "List recent conversations and incoming unread counts; does not mark them read.",
        "read": (
            "Read recent messages from one chat; returned messages are marked read and "
            "the matching wake-notification mirror is cleared."
        ),
        "reply": (
            "Reply to one message using its compound message_id from read/search; this "
            "sends a new message, marks the target handled, and adds a replied reaction."
        ),
        "search": (
            "Regex-search stored inbound message text, sender fields, and update type; "
            "use chat_id='updates' for synthetic non-chat events."
        ),
        "delete": "Delete one bot message by compound message_id; this is an external side effect.",
        "edit": (
            "Edit one previously sent bot message by compound message_id; text/rich edits "
            "and media-caption edits have different rendering limits."
        ),
        "contacts": "List local account contact aliases; contacts do not grant inbound permission.",
        "add_contact": (
            "Save a local alias for a numeric chat_id; this does not add the user to "
            "accounts.allowed_users or authorize inbound messages."
        ),
        "remove_contact": "Remove a local contact by alias or numeric chat_id; it does not change inbound permission.",
        "accounts": "List configured account aliases and safe account details; credentials are not returned.",
    }
    for action, description in branch_descriptions.items():
        schemas[action]["description"] = description

    property_descriptions = {
        "send": {
            "account": "Optional bot account alias; omitted uses the service default.",
            "chat_id": "Real numeric Telegram chat ID for outbound delivery.",
            "text": "Message text or media caption; omit when using structured_message or chat_action.",
            "reply_markup": "Optional Telegram inline keyboard markup.",
            "placeholder": (
                "For long work, send a progress-only placeholder, edit it at meaningful "
                "phase changes, then send the final answer as a separate durable message."
            ),
            "link_preview_options": "Optional Telegram link-preview options for text messages.",
            "disable_web_page_preview": "Compatibility shortcut to disable text link previews.",
        },
        "check": {
            "account": "Optional bot account alias; omitted uses the service default.",
        },
        "read": {
            "account": "Optional bot account alias when more than one account is configured.",
            "chat_id": (
                "Numeric Telegram chat ID; the reserved 'updates' value is read-only and "
                "recovers synthetic non-chat updates."
            ),
            "limit": "Optional maximum number of recent messages; default is 10.",
        },
        "reply": {
            "message_id": "Compound target ID in account:chat_id:message_id form, from read/search results.",
            "text": "Reply text; omit when supplying structured_message.",
            "entities": "Optional MessageEntity[] when rendering_mode='entities' for text.",
        },
        "search": {
            "query": "Case-insensitive regular expression over stored message text, sender, and update type.",
            "account": "Optional bot account alias; omitted uses the service default.",
            "chat_id": "Optional numeric chat ID or exactly 'updates' for the synthetic event bucket.",
        },
        "delete": {
            "message_id": "Compound bot-message ID in account:chat_id:message_id form.",
        },
        "edit": {
            "message_id": "Compound bot-message ID in account:chat_id:message_id form.",
            "text": "Replacement text or media caption; omit when supplying structured_message.",
            "reply_markup": "Optional replacement Telegram inline keyboard markup.",
            "entities": "Optional MessageEntity[] for text edits when rendering_mode='entities'.",
        },
        "contacts": {
            "account": "Optional bot account alias; omitted uses the service default.",
        },
        "add_contact": {
            "account": "Optional bot account alias; omitted uses the service default.",
            "chat_id": "Numeric Telegram chat ID to remember locally.",
            "alias": "Local alias used to find the saved chat; not an authorization grant.",
        },
        "remove_contact": {
            "account": "Optional bot account alias; omitted uses the service default.",
            "chat_id": "Remove contacts matching this numeric Telegram chat ID.",
            "alias": "Remove the contact with this local alias.",
        },
    }
    for action, descriptions in property_descriptions.items():
        for field, description in descriptions.items():
            schemas[action]["properties"][field]["description"] = description

    rich_mode_description = (
        "Default is Markdown; use plain_text, HTML, MarkdownV2, or entities for "
        "text formatting, or rich with structured_message for native Telegram blocks."
    )
    rich_content_description = (
        "Native rich content: require rendering_mode='rich' and title; optional "
        "summary, facts, bullets, steps, code, next, and footer are rendered as "
        "Telegram blocks. Do not combine with text, media, or entity fields."
    )
    for action in ("reply", "edit"):
        schemas[action]["properties"]["rendering_mode"]["description"] = rich_mode_description
        schemas[action]["properties"]["structured_message"]["description"] = rich_content_description
        schemas[action]["properties"]["structured_message"]["anyOf"][0]["description"] = rich_content_description

    return schemas


def _schema_only_family() -> ToolFamily:
    schemas = _telegram_input_schemas()
    return TELEGRAM_PLUGIN.build_family(
        [
            ChildTool(action, schemas[action], lambda _input: {})
            for action in _DECLARED_ACTIONS
        ],
        settings_provider=build_telegram_settings_provider(None),
    )


_SCHEMA_FAMILY = _schema_only_family()


def telegram_schema() -> dict[str, Any]:
    schema = _SCHEMA_FAMILY.build_schema()
    # Telegram has intentionally overlapping optional fields (for example a
    # read with chat_id and a remove_contact with chat_id). The root allOf
    # discriminator still correlates each action to its exact closed branch;
    # use anyOf for the model-discovery list so native JSON-Schema validators do
    # not reject a valid input merely because another action's branch also fits.
    input_schema = schema["properties"]["input"]
    if "oneOf" in input_schema:
        input_schema["anyOf"] = input_schema.pop("oneOf")
    schema["properties"]["action"]["description"] = (
        "Choose one Telegram action. For inbound work, begin read-only with check, "
        "read, or search. Use send only for an authorized new outbound message to a "
        "known numeric chat_id; use reply with a compound message_id from read/search. "
        "Content-bearing send/reply/edit defaults to Markdown; use plain_text, "
        "HTML, MarkdownV2, entities, or rich only when needed. For charts, "
        "reports, generated artifacts, and other files the user should open intact, "
        "use media.type='document' (use 'photo' only for an inline preview). "
        "Read this package's detailed guidance with "
        + TELEGRAM_PLUGIN.manual_action_description()
    )
    return schema


def _basic_validate(value: Any, schema: Mapping[str, Any]) -> bool:
    """Small dependency-free validator for the dispatch safety boundary.

    JSON-Schema combinators compose with sibling constraints.  Validate them
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


def build_telegram_family(manager: Any | None) -> ToolFamily:
    """Compose the public family: manager-backed declared actions + plugin manual.

    Only Telegram's own actions are built here. ``manual`` is appended by
    ``TELEGRAM_PLUGIN`` and answered directly from the packaged ``SKILL.md``,
    with or without a live manager — the same payload the manager's own
    ``_manual()`` returns, minus the possibility of the business boundary
    replacing it.
    """
    schemas = _telegram_input_schemas()
    children = [
        ChildTool(
            action,
            schemas[action],
            (lambda input_, action=action: manager.handle({"action": action, **dict(input_)}))
            if manager is not None else (lambda _input: {}),
        )
        for action in _DECLARED_ACTIONS
    ]
    return TELEGRAM_PLUGIN.build_family(
        children,
        settings_provider=build_telegram_settings_provider(manager),
    )


def handle_telegram(manager: Any | None, args: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(args or {})
    if set(raw) - {"action", "input", "reasoning", "summarize"}:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "unsupported telegram argument"}
    action = raw.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return {"status": "failed", "error_code": "ACTION_REQUIRED", "message": "invalid telegram action"}
    if "input" not in raw or not isinstance(raw.get("input"), Mapping):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "input must be an object"}
    if type(raw.get("reasoning")) is not str:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "reasoning is required"}
    if "summarize" in raw and type(raw["summarize"]) is not bool:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "summarize must be a boolean"}
    schema = _telegram_input_schemas()[action]
    if not _basic_validate(raw["input"], schema):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "invalid telegram input"}
    return build_telegram_family(manager).handle(raw)


TELEGRAM_SCHEMA = telegram_schema()
# ``TELEGRAM_ACTIONS`` is re-exported from ``plugin.py`` (imported above) so the
# public action list has exactly one definition.
