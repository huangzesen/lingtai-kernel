"""Feishu's independent LTP-v2 tool family.

This module owns only the public Feishu envelope and action branches. The
manager remains the legacy result/business boundary behind the validated
family, matching the Telegram MCP's split (``../telegram/_family.py``).

Action *composition* belongs to the package's plugin descriptor (`plugin.py`):
this module declares Feishu's own actions and their strict `input` branches,
and `FEISHU_PLUGIN` inserts read-only `settings` before the reserved `manual`
action from the packaged `SKILL.md`. Neither reserved action routes through
the manager.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lingtai.tools.tool_family import ChildTool, ToolFamily

from ._errors import failure_result
from .plugin import FEISHU_ACTIONS, FEISHU_DECLARED_ACTIONS, FEISHU_PLUGIN
from .settings import build_feishu_settings

# The package's own actions plus plugin-composed reserved settings/manual. Kept
# local to avoid importing the manager (which consumes this schema).
_DECLARED_ACTIONS = FEISHU_DECLARED_ACTIONS
_ACTIONS = FEISHU_ACTIONS


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


def _media_source_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            _object(
                {
                    "type": {"type": "string", "enum": ["path"]},
                    "path": {"type": "string", "minLength": 1},
                },
                required=["type", "path"],
            ),
            _object(
                {
                    "type": {"type": "string", "enum": ["key"]},
                    "key": {"type": "string", "minLength": 1},
                },
                required=["type", "key"],
            ),
        ]
    }


def _outbound_content_schema(*, include_media: bool = True) -> dict[str, Any]:
    """Strict outbound union; media may be excluded for edit."""
    post_value = {"type": "object", "minProperties": 1}
    card_value = {
        "type": "object",
        "properties": {
            "schema": {"type": "string", "enum": ["2.0"]},
        },
        "required": ["schema"],
        "minProperties": 2,
    }
    branches = [
        _object(
            {
                "type": {"type": "string", "enum": ["text"]},
                "text": {"type": "string", "minLength": 1},
            },
            required=["type", "text"],
        ),
        _object(
            {
                "type": {"type": "string", "enum": ["markdown"]},
                "markdown": {"type": "string", "minLength": 1},
            },
            required=["type", "markdown"],
        ),
        _object(
            {
                "type": {"type": "string", "enum": ["post"]},
                "post": post_value,
            },
            required=["type", "post"],
        ),
        _object(
            {
                "type": {"type": "string", "enum": ["card"]},
                "card": card_value,
            },
            required=["type", "card"],
        ),
    ]
    if include_media:
        source = _media_source_schema()
        branches.extend([
            _object(
                {
                    "type": {"type": "string", "enum": ["image"]},
                    "source": source,
                    "caption": {"type": "string"},
                },
                required=["type", "source"],
            ),
            _object(
                {
                    "type": {"type": "string", "enum": ["file"]},
                    "source": source,
                    "file_name": {"type": "string", "minLength": 1},
                },
                required=["type", "source"],
            ),
            _object(
                {
                    "type": {"type": "string", "enum": ["audio"]},
                    "source": source,
                },
                required=["type", "source"],
            ),
            _object(
                {
                    "type": {"type": "string", "enum": ["video"]},
                    "source": source,
                    "caption": {"type": "string"},
                },
                required=["type", "source"],
            ),
            _object(
                {
                    "type": {"type": "string", "enum": ["share_chat"]},
                    "chat_id": {"type": "string", "minLength": 1},
                },
                required=["type", "chat_id"],
            ),
            _object(
                {
                    "type": {"type": "string", "enum": ["share_user"]},
                    "user_id": {"type": "string", "minLength": 1},
                },
                required=["type", "user_id"],
            ),
            _object(
                {
                    "type": {"type": "string", "enum": ["sticker"]},
                    "file_key": {"type": "string", "minLength": 1},
                },
                required=["type", "file_key"],
            ),
        ])
    return {"oneOf": branches}


def _feishu_input_schemas() -> dict[str, dict[str, Any]]:
    content = _outbound_content_schema()
    editable_content = _outbound_content_schema(include_media=False)
    send = _object(
        {
            "account": _nullable({"type": "string"}),
            "receive_id": {"type": "string"},
            "receive_id_type": _nullable({
                "type": "string",
                "enum": ["open_id", "user_id", "email", "chat_id", "union_id"],
            }),
            "text": {"type": "string"},
            "content": content,
            "placeholder": _nullable({"type": "boolean"}),
        },
        required=["receive_id"],
        one_of=[{"required": ["text"]}, {"required": ["content"]}],
    )
    send["properties"]["receive_id_type"]["anyOf"][0]["description"] = (
        "Type of receive_id. Use 'open_id' for individual users "
        "(format: ou_xxx), 'chat_id' for group chats (format: oc_xxx). "
        "Defaults to 'open_id'."
    )
    send["properties"]["placeholder"]["anyOf"][0]["description"] = (
        "send only — wrap text, markdown, or post content in a native "
        "schema-2.0 progress card and return its compound message_id. "
        "Edit that card only at meaningful phase changes; send the final "
        "answer separately with send or reply."
    )
    empty = _object({})
    return FEISHU_PLUGIN.action_input_schemas({
        "send": send,
        "check": _object({"account": _nullable({"type": "string"})}),
        "read": _object(
            {
                "account": _nullable({"type": "string"}),
                "chat_id": {"type": "string"},
                "limit": _nullable({"type": "integer"}),
            },
            required=["chat_id"],
        ),
        "reply": _object(
            {
                "message_id": {"type": "string"},
                "text": {"type": "string"},
                "content": content,
                "reply_in_thread": {"type": "boolean"},
            },
            required=["message_id"],
            one_of=[{"required": ["text"]}, {"required": ["content"]}],
        ),
        "react": _object(
            {
                "message_id": {"type": "string", "minLength": 1},
                "operation": {
                    "type": "string", "enum": ["add", "remove"],
                },
                "emoji_type": {"type": "string", "minLength": 1},
                "reaction_id": {"type": "string", "minLength": 1},
            },
            required=["message_id", "operation"],
            one_of=[
                {
                    "properties": {
                        "operation": {"type": "string", "enum": ["add"]},
                    },
                    "required": ["emoji_type"],
                    "not": {"required": ["reaction_id"]},
                },
                {
                    "properties": {
                        "operation": {"type": "string", "enum": ["remove"]},
                    },
                    "required": ["reaction_id"],
                    "not": {"required": ["emoji_type"]},
                },
            ],
        ),
        "search": _object(
            {
                "query": {"type": "string"},
                "account": _nullable({"type": "string"}),
                "chat_id": _nullable({"type": "string"}),
            },
            required=["query"],
        ),
        "delete": _object({
            "message_id": {
                "type": "string",
                "description": (
                    "Any compound ID returned for a logical bot message; "
                    "all persisted physical chunks are deleted."
                ),
            },
        }, required=["message_id"]),
        "edit": _object(
            {
                "message_id": {
                    "type": "string",
                    "description": (
                        "Any compound ID returned for a logical bot message; "
                        "all persisted physical chunks are edited."
                    ),
                },
                "text": {"type": "string"},
                "content": editable_content,
            },
            required=["message_id"],
            one_of=[{"required": ["text"]}, {"required": ["content"]}],
        ),
        "contacts": _object({"account": _nullable({"type": "string"})}),
        "add_contact": _object(
            {
                "account": _nullable({"type": "string"}),
                "open_id": {"type": "string"},
                "alias": {"type": "string"},
                "name": _nullable({"type": "string"}),
                "chat_id": _nullable({"type": "string"}),
            },
            required=["open_id", "alias"],
        ),
        "remove_contact": _object(
            {
                "account": _nullable({"type": "string"}),
                "open_id": {"type": "string"},
                "alias": {"type": "string"},
            },
            one_of=[{"required": ["alias"]}, {"required": ["open_id"]}],
        ),
        "accounts": empty,
    })


def _schema_only_family() -> ToolFamily:
    schemas = _feishu_input_schemas()
    return FEISHU_PLUGIN.build_family(
        [
            ChildTool(action, schemas[action], lambda _input: {})
            for action in _DECLARED_ACTIONS
        ],
        settings_provider=lambda: (),
    )


_SCHEMA_FAMILY = _schema_only_family()


def feishu_schema() -> dict[str, Any]:
    schema = _SCHEMA_FAMILY.build_schema()
    # check/contacts/accounts/settings/manual all share the empty-object branch,
    # so a strict oneOf here is ambiguous ({} matches more than one branch). The
    # root allOf discriminator still correlates each action to its exact
    # closed branch; use anyOf for the model-discovery list so native
    # JSON-Schema validators do not reject a valid input merely because
    # another action's branch also fits.
    input_schema = schema["properties"]["input"]
    if "oneOf" in input_schema:
        input_schema["anyOf"] = input_schema.pop("oneOf")
    schema["properties"]["action"]["description"] = (
        "Feishu/Lark messaging: send, receive, reply, edit, and inspect messages "
        "plus reactions, contacts, and account status. Strict call shape is "
        "{action, input, reasoning, summarize?}; action, input, and reasoning "
        "are required, input is a closed action-local object, and summarize is "
        "optional boolean. Actions: send (receive_id, receive_id_type, exactly "
        "one of text/content; optional account, placeholder); check (recent "
        "conversations); read (chat_id); reply (exact message_id, exactly one "
        "of text/content, optional reply_in_thread); react (add + emoji_type or "
        "remove + reaction_id); search (query); delete (logical message_id); "
        "edit (logical message_id, text/content; no media); contacts; "
        "add_contact; remove_contact; accounts; settings (read-only empty "
        "input); manual (full guidance). Send/reply content includes text, "
        "markdown, post, schema-2.0 card, media, share, or sticker; edit "
        "supports text/markdown/post/card. Compound reply targets never fall back to a "
        "fresh send, and card callbacks and resident Task Cards have separate "
        "semantics. send, reply, edit, delete, and react are external side "
        "effects; confirm targets/content. Failures expose classified retry "
        "guidance and the client never hides an automatic retry. "
        + FEISHU_PLUGIN.manual_action_description()
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
        if len(value) < schema.get("minProperties", 0):
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
        return (
            isinstance(value, str)
            and value in schema.get("enum", [value])
            and len(value) >= schema.get("minLength", 0)
        )
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


def build_feishu_family(manager: Any | None) -> ToolFamily:
    """Compose manager actions with package-owned settings and manual children.

    Only Feishu's own actions are built here. The settings provider reads the
    bound service, while ``manual`` answers directly from the packaged
    ``SKILL.md`` even when no manager is available.
    """
    schemas = _feishu_input_schemas()
    children = [
        ChildTool(
            action,
            schemas[action],
            (lambda input_, action=action: manager.handle({"action": action, **dict(input_)}))
            if manager is not None else (lambda _input: {})
        )
        for action in _DECLARED_ACTIONS
    ]
    return FEISHU_PLUGIN.build_family(
        children,
        settings_provider=lambda: build_feishu_settings(manager),
    )


def handle_feishu(manager: Any | None, args: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(args or {})
    if set(raw) - {"action", "input", "reasoning", "summarize"}:
        return failure_result(
            "unsupported feishu argument", error_code="INVALID_ARGUMENT",
        )
    action = raw.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return failure_result(
            "invalid feishu action", error_code="ACTION_REQUIRED",
        )
    if "input" not in raw or not isinstance(raw.get("input"), Mapping):
        return failure_result(
            "input must be an object", error_code="INVALID_ARGUMENT",
        )
    if type(raw.get("reasoning")) is not str:
        return failure_result(
            "reasoning is required", error_code="INVALID_ARGUMENT",
        )
    if "summarize" in raw and type(raw["summarize"]) is not bool:
        return failure_result(
            "summarize must be a boolean", error_code="INVALID_ARGUMENT",
        )
    schema = _feishu_input_schemas()[action]
    if not _basic_validate(raw["input"], schema):
        return failure_result(
            "invalid feishu input", error_code="INVALID_ARGUMENT",
        )
    if action == "react":
        input_ = raw["input"]
        operation = input_.get("operation")
        expected = "emoji_type" if operation == "add" else "reaction_id"
        unexpected = "reaction_id" if operation == "add" else "emoji_type"
        if operation not in {"add", "remove"} or not input_.get(expected) or input_.get(
            unexpected
        ):
            return failure_result(
                "invalid feishu input", error_code="INVALID_ARGUMENT",
            )
    if manager is None and action not in {"settings", "manual"}:
        return failure_result(
            "Feishu manager is not initialized",
            error_code="NOT_CONNECTED",
        )
    return build_feishu_family(manager).handle(raw)


FEISHU_SCHEMA = feishu_schema()
# ``FEISHU_ACTIONS`` is re-exported from ``plugin.py`` (imported above) so the
# public action list has exactly one definition.
