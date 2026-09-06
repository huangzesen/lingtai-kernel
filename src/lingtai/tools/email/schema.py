"""Description and the legacy flat schema for the email intrinsic tool.

``get_description`` remains the registered intrinsic description.

``get_schema`` below is the **legacy flat** schema. Since the ToolFamily
migration it is no longer the model-facing schema: the composed LTP v2 family
schema lives in ``__init__.py::get_schema``, and this one now describes the
*internal* ``EmailManager.handle`` argument shape (the same seam ``shell``
kept for ``ShellManager``). ``__init__.py`` re-exports it as
``get_flat_schema``. Retained rather than deleted because it is the honest
description of that still-live internal interface — see the keep/remove ledger
in the migration report.
"""
from __future__ import annotations

from .primitives import mode_field


def get_description(lang: str = "en") -> str:
    return ("Internal .lingtai mailbox only — not internet email (use the imap tool for Gmail/Outlook). "
            "Use bare agent/path addresses; the closed envelope is action + input + reasoning, "
            "with only the selected action's fields in input. Cross-action fields are rejected "
            "before mailbox I/O. Reply on the channel the message arrived on (prefer reply or "
            "reply_all); never reply in text output. Address senders by sender_nickname, else "
            "sender_name. Call email(action='manual', input={}) for the email-manual router.")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "send", "check", "read", "dismiss", "reply", "reply_all",
                    "search", "archive", "delete",
                    "contacts", "add_contact", "remove_contact", "edit_contact",
                    "manual",
                ],
                "description": ("Choose one action; put only its fields in input. send: new internal "
                                "message (address/message required; body max 50,000 characters). "
                                "check: list/filter mail. read: fetch IDs and mark read; dismiss: "
                                "mark handled IDs read without returning bodies. reply/reply_all: "
                                "reply in-thread. search: regex lookup. archive/delete: move or "
                                "remove inbox/archive mail. contacts actions manage the address book. "
                                "settings is read-only. manual returns this manual without mailbox I/O."),
            },
            "address": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": 'Bare name/path for send; string or list.',
            },
            "cc": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Visible CC addresses.',
            },
            "bcc": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Hidden BCC addresses.',
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Attachment paths for send.',
            },
            "subject": {"type": "string", "description": 'Subject.'},
            "message": {"type": "string", "description": 'Body; max 50,000 characters.'},
            "email_id": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Own mailbox ID list; replies use one ID.',
            },
            "n": {
                "type": "integer",
                "description": 'Max messages for check; default 10.',
                "default": 10,
            },
            "query": {
                "type": "string",
                "description": 'Regex query over sender, subject, and body.',
            },
            "folder": {
                "type": "string",
                "enum": ["inbox", "sent", "archive"],
                "description": "Folder; check inbox/search both; sent is read-only.",
            },
            "delay": {
                "type": "integer",
                "description": 'Delivery delay in seconds; default 0.',
            },
            "mode": mode_field(lang),
            "type": {
                "type": "string",
                "enum": ["normal"],
                "description": 'Send type; default normal.',
            },
            "name": {
                "type": "string",
                "description": "Contact name.",
            },
            "note": {
                "type": "string",
                "description": "Contact note.",
            },
            "filter": {
                "type": "object",
                "description": "Optional check filters; see email-manual for fields and defaults.",
                "properties": {
                    "sort": {
                        "type": "string",
                        "enum": ["newest", "oldest"],
                        "description": "Sort newest (default) or oldest.",
                    },
                    "from": {
                        "type": "string",
                        "description": "Case-insensitive sender substring.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Case-insensitive subject substring.",
                    },
                    "contains": {
                        "type": "string",
                        "description": "Case-insensitive body substring.",
                    },
                    "after": {
                        "type": "string",
                        "description": "Only messages after an ISO 8601 timestamp.",
                    },
                    "before": {
                        "type": "string",
                        "description": "Only messages before an ISO 8601 timestamp.",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only unread messages.",
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": "Only messages with attachments.",
                    },
                    "truncate": {
                        "type": "integer",
                        "description": "Preview characters; default 500, 0 means full body.",
                        "default": 500,
                    },
                },
            },
        },
        "required": [],
    }
