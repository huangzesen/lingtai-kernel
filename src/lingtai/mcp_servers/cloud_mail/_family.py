"""Cloud Mail's independent LTP-v2 tool family.

This module owns only the public Cloud Mail envelope and action branches.
``CloudMailManager`` remains the legacy result/business boundary behind the
validated family, mirroring ``telegram/_family.py``.

Action *composition* belongs to the package's plugin descriptor (`plugin.py`):
this module declares Cloud Mail's own actions and their strict `input`
branches, and `CLOUD_MAIL_PLUGIN` inserts the reserved `settings` action before
the packaged `manual` action. Neither reserved action routes through the
business manager or can be re-schema'd from here.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lingtai.tools.tool_family import SettingsProvider
from lingtai.tools.tool_family.settings import build_settings_child

from .plugin import CLOUD_MAIL_ACTIONS, CLOUD_MAIL_DECLARED_ACTIONS, CLOUD_MAIL_PLUGIN
from .settings import CloudMailSettingsProvider

# The package's own actions plus plugin-inserted ``settings`` and ``manual``.
# Kept local to avoid importing the manager (which consumes this schema).
_DECLARED_ACTIONS = CLOUD_MAIL_DECLARED_ACTIONS
_ACTIONS = CLOUD_MAIL_ACTIONS

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


def _cloud_mail_input_schemas() -> dict[str, dict[str, Any]]:
    account = {"type": "string", "description": "Account alias (or admin email). Defaults to the first configured account."}
    time_sort = _nullable({"type": "string", "enum": ["asc", "desc"]})
    check = _object(
        {
            "account": _nullable(account),
            "limit": _nullable({"type": "integer"}),
            "to_email": _nullable({"type": "string"}),
            "send_email": _nullable({"type": "string"}),
            "subject": _nullable({"type": "string"}),
            "time_sort": time_sort,
            "type": _nullable({"type": "integer"}),
        },
    )
    search = _object(
        {
            "account": _nullable(account),
            "to_email": _nullable({"type": "string"}),
            "send_email": _nullable({"type": "string"}),
            "send_name": _nullable({"type": "string"}),
            "subject": _nullable({"type": "string"}),
            "content": _nullable({"type": "string"}),
            "time_sort": time_sort,
            "num": _nullable({"type": "integer"}),
            "size": _nullable({"type": "integer"}),
            "type": _nullable({"type": "integer"}),
            "is_del": _nullable({"type": "integer"}),
        },
    )
    read = _object(
        {
            "account": _nullable(account),
            "id": _nullable({"type": "string"}),
            "email_id": _nullable({"anyOf": [{"type": "string"}, {"type": "integer"}]}),
        },
    )
    send = _object(
        {
            "account": _nullable(account),
            "address": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Recipient address or list of addresses.",
            },
            "subject": _nullable({"type": "string"}),
            "message": _nullable({"type": "string"}),
            "text": _nullable({"type": "string"}),
            "html": _nullable({"type": "string"}),
            "content_html": _nullable({"type": "string"}),
            "name": _nullable({"type": "string"}),
            "send_account_id": _nullable({"type": "integer"}),
            "attachments": _nullable({"type": "array"}),
        },
        required=["address"],
    )
    accounts = _object({})
    add_user = _object(
        {
            "account": _nullable(account),
            "email": {"type": "string"},
            "password": {"type": "string"},
            "role_name": _nullable({"type": "string"}),
        },
        required=["email", "password"],
    )
    return CLOUD_MAIL_PLUGIN.action_input_schemas({
        "check": check,
        "search": search,
        "read": read,
        "send": send,
        "accounts": accounts,
        "add_user": add_user,
    })


def cloud_mail_schema() -> dict[str, Any]:
    schemas = _cloud_mail_input_schemas()
    branches = []
    for action in _ACTIONS:
        branch = {"title": f"{action} input"}
        branch.update(schemas[action])
        branches.append(branch)
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": (
                    "Cloud Mail action; each action owns a strict input branch. "
                    "check lists inbound mail; search filters; read returns full "
                    "content by '<account>:<emailId>'; send delivers real email "
                    "with user credentials; accounts returns redacted status; "
                    "add_user is an admin user mutation; settings is a read-only "
                    "redacted startup inventory. Inbound mail is polled. Call "
                    "manual "
                    + CLOUD_MAIL_PLUGIN.manual_action_description()
                ),
            },
            "input": {
                "description": (
                    "Strict action-specific input; the selected action is "
                    "validated again at dispatch."
                ),
                "anyOf": branches,
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of why you are calling this tool (recorded in your diary).",
            },
            "summarize": {
                "type": "boolean",
                "description": (
                    "Root, cross-cutting result post-processing control; "
                    "absent or false by default. Not action input."
                ),
            },
        },
        "required": ["action", "input", "reasoning"],
        "additionalProperties": False,
    }


def _basic_validate(value: Any, schema: Mapping[str, Any]) -> bool:
    """Small dependency-free validator for the dispatch safety boundary."""
    if "anyOf" in schema and not any(
        _basic_validate(value, branch) for branch in schema["anyOf"]
    ):
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
        return type(value) is int and value in schema.get("enum", [value])
    if expected == "null":
        return value is None
    return True


def build_cloud_mail_family(
    manager: Any | None,
    *,
    settings_provider: SettingsProvider | None = None,
):
    """Bind action handlers to ``manager`` (or produce no-op stubs).

    Only Cloud Mail's own actions are handler-bound to ``manager`` here.
    ``settings`` uses the generic bounded SHOW child with its owner provider,
    and ``manual`` answers directly from ``CLOUD_MAIL_PLUGIN``'s packaged
    ``SKILL.md``. Neither enters the business manager.
    """
    schemas = _cloud_mail_input_schemas()

    def _handler(action: str):
        if manager is not None:
            return lambda input_, action=action: manager.handle({"action": action, **dict(input_)})
        return lambda _input: {}

    handlers = {action: _handler(action) for action in _DECLARED_ACTIONS}
    if CLOUD_MAIL_PLUGIN.settings:
        provider = (
            settings_provider
            if settings_provider is not None
            else CloudMailSettingsProvider(manager)
        )
        handlers["settings"] = build_settings_child(provider).handler
    handlers["manual"] = lambda _input: CLOUD_MAIL_PLUGIN.manual_payload()
    return handlers, schemas


def handle_cloud_mail(
    manager: Any | None,
    args: Mapping[str, Any] | None,
    *,
    settings_provider: SettingsProvider | None = None,
) -> dict[str, Any]:
    raw = dict(args or {})
    if set(raw) - {"action", "input", "reasoning", "summarize"}:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "unsupported cloud_mail argument"}
    action = raw.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return {"status": "failed", "error_code": "ACTION_REQUIRED", "message": "invalid cloud_mail action"}
    if "input" not in raw or not isinstance(raw.get("input"), Mapping):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "input must be an object"}
    if type(raw.get("reasoning")) is not str:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "reasoning is required"}
    if "summarize" in raw and type(raw["summarize"]) is not bool:
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "summarize must be a boolean"}
    schema = _cloud_mail_input_schemas()[action]
    if not _basic_validate(raw["input"], schema):
        return {"status": "failed", "error_code": "INVALID_ARGUMENT", "message": "invalid cloud_mail input"}
    handlers, _schemas = build_cloud_mail_family(
        manager,
        settings_provider=settings_provider,
    )
    return handlers[action](raw["input"])


CLOUD_MAIL_SCHEMA = cloud_mail_schema()
# ``CLOUD_MAIL_ACTIONS`` is re-exported from ``plugin.py`` (imported above) so
# the public action list has exactly one definition.
