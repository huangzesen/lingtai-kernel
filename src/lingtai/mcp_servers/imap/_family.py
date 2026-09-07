"""IMAP's independent LTP-v2 tool family.

This module owns only the public IMAP envelope and action branches. The
manager remains the legacy result/business boundary behind the validated
family — mirrors ``telegram/_family.py``.

Action *composition* belongs to the package's plugin descriptor (`plugin.py`):
this module declares IMAP's own actions and their strict `input` branches, and
`IMAP_PLUGIN` inserts the reserved read-only `settings` action immediately
before the packaged `manual`. Neither reserved action routes through the
business manager.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lingtai.tools.tool_family import ChildTool, ToolFamily

from .plugin import IMAP_ACTIONS, IMAP_DECLARED_ACTIONS, IMAP_PLUGIN
from .settings import imap_setting_rows

# The package's own actions plus plugin-composed ``settings`` and ``manual``.
# Kept local to avoid importing the manager (which consumes this schema).
_DECLARED_ACTIONS = IMAP_DECLARED_ACTIONS
_ACTIONS = IMAP_ACTIONS


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def _address_list() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ],
    }


def _email_id_list() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ],
        "description": (
            "Email ID(s) returned by check/search/read; compound key "
            "account:folder:uid. Reply uses the first ID."
        ),
    }


def _account_field() -> dict[str, Any]:
    return _nullable({
        "type": "string",
        "description": (
            "Which account to use (email address). Optional — an empty or "
            "whitespace-only string is treated as omitted and defaults to "
            "the primary account."
        ),
    })


def _imap_input_schemas() -> dict[str, dict[str, Any]]:
    send = _object(
        {
            "account": _account_field(),
            "address": _address_list(),
            "subject": {"type": "string", "description": "Email subject line"},
            "message": {"type": "string", "description": "Email body"},
            "cc": _address_list(),
            "bcc": _address_list(),
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Attachment paths for send/reply; relative paths use the working "
                    "dir and absolute paths must stay inside it."
                ),
            },
        },
        required=["address"],
    )
    send["properties"]["address"]["description"] = (
        "Target recipient address(es) for a real outbound send; verify them "
        "before delivery."
    )
    send["properties"]["subject"]["description"] = "Subject to review before real delivery"
    send["properties"]["message"]["description"] = (
        "Body to review before real delivery; the schema accepts an omitted body."
    )
    send["properties"]["cc"]["description"] = (
        "Visible CC recipient(s); verify before real delivery"
    )
    send["properties"]["bcc"]["description"] = (
        "Hidden BCC recipient(s); verify before real delivery"
    )

    check = _object({
        "account": _account_field(),
        "folder": _nullable({
            "type": "string",
            "description": (
                "IMAP folder name (e.g. INBOX, [Gmail]/Sent Mail). An empty "
                "or whitespace-only string is treated as omitted and "
                "defaults to INBOX."
            ),
        }),
        "n": _nullable({
            "type": "integer",
            "description": "Max recent emails to show (default 10)",
        }),
    })

    read = _object(
        {
            "account": _account_field(),
            "email_id": _email_id_list(),
        },
        required=["email_id"],
    )

    reply = _object(
        {
            "account": _account_field(),
            "email_id": _email_id_list(),
            "subject": {"type": "string", "description": "Email subject line"},
            "message": {"type": "string", "description": "Email body"},
            "cc": _address_list(),
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Attachment paths for send/reply; relative paths use the working "
                    "dir and absolute paths must stay inside it."
                ),
            },
        },
        required=["email_id", "message"],
    )
    reply["properties"]["email_id"]["description"] = (
        "Target email ID from check/search/read; compound account:folder:uid. "
        "Reply uses the first ID and requires reading it before delivery."
    )
    reply["properties"]["subject"]["description"] = (
        "Optional subject override; otherwise reply threading derives it from the target"
    )
    reply["properties"]["message"]["description"] = (
        "Reply body to review before real delivery"
    )
    reply["properties"]["cc"]["description"] = (
        "Visible CC recipient(s); verify before real delivery"
    )

    search = _object(
        {
            "account": _account_field(),
            "query": {
                "type": "string",
                "description": (
                    "Server-side IMAP search DSL (for example from:addr "
                    "subject:text unseen since:YYYY-MM-DD); see manual for detail."
                ),
            },
            "folder": _nullable({
                "type": "string",
                "description": (
                    "IMAP folder name. An empty or whitespace-only string is "
                    "treated as omitted and defaults to INBOX."
                ),
            }),
        },
        required=["query"],
    )

    delete = _object(
        {
            "account": _account_field(),
            "email_id": _email_id_list(),
        },
        required=["email_id"],
    )
    delete["properties"]["email_id"]["description"] = (
        "Email ID(s) to delete; verify compound IDs because this changes "
        "server-side mailbox state."
    )

    move = _object(
        {
            "account": _account_field(),
            "email_id": _email_id_list(),
            "folder": {
                "type": "string",
                "description": (
                    "Non-empty destination folder; this changes mailbox state and "
                    "is never defaulted to INBOX."
                ),
            },
        },
        required=["email_id", "folder"],
    )
    move["properties"]["email_id"]["description"] = (
        "Email ID(s) to move; verify source IDs and destination before changing "
        "server-side mailbox state."
    )

    flag = _object(
        {
            "account": _account_field(),
            "email_id": _email_id_list(),
            "flags": {
                "type": "object",
                "description": (
                    "Non-empty flag-name-to-bool map; e.g. {\"seen\": true, "
                    "\"flagged\": false}. This changes mailbox state."
                ),
            },
        },
        required=["email_id", "flags"],
    )
    flag["properties"]["email_id"]["description"] = (
        "Email ID(s) to flag; verify them before changing server-side mailbox state."
    )

    folders = _object({"account": _account_field()})
    contacts = _object({"account": _account_field()})

    add_contact = _object(
        {
            "account": _account_field(),
            "address": {"type": "string", "description": "Contact's email address"},
            "name": {
                "type": "string",
                "description": "Contact's human-readable name",
            },
            "note": {"type": "string", "description": "Free-text note about the contact"},
        },
        required=["address", "name"],
    )

    remove_contact = _object(
        {
            "account": _account_field(),
            "address": {"type": "string", "description": "Contact's email address"},
        },
        required=["address"],
    )

    edit_contact = _object(
        {
            "account": _account_field(),
            "address": {"type": "string", "description": "Contact's email address"},
            "name": {
                "type": "string",
                "description": "Contact's human-readable name",
            },
            "note": {"type": "string", "description": "Free-text note about the contact"},
        },
        required=["address"],
    )

    accounts = _object({})
    return IMAP_PLUGIN.action_input_schemas({
        "send": send,
        "check": check,
        "read": read,
        "reply": reply,
        "search": search,
        "delete": delete,
        "move": move,
        "flag": flag,
        "folders": folders,
        "contacts": contacts,
        "add_contact": add_contact,
        "remove_contact": remove_contact,
        "edit_contact": edit_contact,
        "accounts": accounts,
    })


def _schema_only_family() -> ToolFamily:
    schemas = _imap_input_schemas()
    return IMAP_PLUGIN.build_family(
        [
            ChildTool(action, schemas[action], lambda _input: {})
            for action in _DECLARED_ACTIONS
        ],
        settings_provider=lambda: imap_setting_rows(None),
    )


_SCHEMA_FAMILY = _schema_only_family()


def imap_schema() -> dict[str, Any]:
    schema = _SCHEMA_FAMILY.build_schema()
    # IMAP has intentionally overlapping optional fields (for example every
    # action accepts optional account). The root allOf discriminator still
    # correlates each action to its exact closed branch; use anyOf for the
    # model-discovery list so native JSON-Schema validators do not reject a
    # valid input merely because another action's branch also fits.
    input_schema = schema["properties"]["input"]
    if "oneOf" in input_schema:
        input_schema["anyOf"] = input_schema.pop("oneOf")
    schema["properties"]["action"]["description"] = (
        "Strict action-owned input branches for real IMAP/SMTP email. "
        "Safe first route: use check/search, then read the returned compound "
        "email_id before deciding whether to reply. send and reply deliver real "
        "external mail; verify to/cc/bcc recipients and the body first. For an "
        "external reply, follow the standing policy or confirm the sender is the "
        "same human who contacted you internally. account defaults when omitted; "
        "blank check/search folders mean INBOX; move requires a non-empty "
        "destination. Use returned IDs in account:folder:uid form. delete, move, "
        "and flag mutate mailbox state; inspect errors and delivery status. "
        "Call the manual for attachment, search, contacts, accounts, settings, "
        "configuration, and deeper safety detail. "
        + IMAP_PLUGIN.manual_action_description()
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


def build_imap_family(manager: Any | None) -> ToolFamily:
    """Compose manager actions plus read-only settings and the plugin manual.

    Only IMAP's operational actions are built here. The plugin inserts
    ``settings`` and ``manual``. The manual works without a manager; settings
    returns the generic fixed failure when applied runtime truth is unavailable.
    """
    schemas = _imap_input_schemas()
    children = [
        ChildTool(
            action,
            schemas[action],
            (lambda input_, action=action: manager.handle({"action": action, **dict(input_)}))
            if manager is not None else (lambda _input: {}),
        )
        for action in _DECLARED_ACTIONS
    ]
    return IMAP_PLUGIN.build_family(
        children,
        settings_provider=lambda: imap_setting_rows(manager),
    )


def handle_imap(manager: Any | None, args: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(args or {})
    if set(raw) - {"action", "input", "reasoning", "summarize"}:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "unsupported imap argument"}
    action = raw.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return {"status": "failed", "error_code": "ACTION_REQUIRED", "message": "invalid imap action"}
    if "input" not in raw or not isinstance(raw.get("input"), Mapping):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "input must be an object"}
    if type(raw.get("reasoning")) is not str:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "reasoning is required"}
    if "summarize" in raw and type(raw["summarize"]) is not bool:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "summarize must be a boolean"}
    schema = _imap_input_schemas()[action]
    if not _basic_validate(raw["input"], schema):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "invalid imap input"}
    return build_imap_family(manager).handle(raw)


IMAP_SCHEMA = imap_schema()
# ``IMAP_ACTIONS`` is re-exported from ``plugin.py`` (imported above) so the
# public action list has exactly one definition.
