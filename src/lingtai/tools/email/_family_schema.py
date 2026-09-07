"""Schema data — canonical per-action ``input`` schemas for the ``email`` family.

This module holds only data: one strict, closed ``input_schema`` per
operational/manual ``email`` action (:data:`INPUT_SCHEMAS`), the canonical
pre-settings action order
(:data:`ACTION_ORDER`), and the canonical English action prose
(:data:`ACTION_ENUM_DESCRIPTION`).  ``__init__.py`` composes these into the
public model-facing schema via the generic ``ToolFamily`` infra
(``lingtai.tools.tool_family``) — see ``__init__.py::get_schema``.

Why a new module rather than reshaping ``schema.py``: ``schema.py``'s flat
``get_schema()`` is still the *internal* ``EmailManager.handle`` argument
shape (the same seam ``shell`` kept when its ``ShellManager`` stayed flat —
``tools/CONTRACT.md`` "Relationship to current runtime"), and
``tests/test_layers_email.py`` pins several of its facts.  Keeping the
per-action data here means the model-facing composition and the legacy flat
shape have one owner each, and the ledger in the migration report can name
exactly what each file is for.

Field descriptions retain the first-call semantics of ``schema.py``'s flat
properties while moving rationale, catalogs, and examples to ``email-manual``
references. ``ACTION_ORDER`` owns the operational/manual order and child
registration order in ``__init__.py``. The generic declaration opt-in inserts
``settings`` immediately before ``manual`` and consequently composes the public
``input.anyOf``/``allOf`` order without adding a hand-authored schema here.

Optional fields are declared in the provider-compatible nullable
representation (``"type": [..., "null"]`` plus membership in ``required``) per
``tools/CONTRACT.md`` "Envelope": a strict OpenAI schema has no other way to
express an optional field.  ``__init__.py`` strips those nulls back to
*absent* before the pre-existing ``EmailManager`` handlers run, so their
``args.get("folder", "inbox")``-style defaulting — and the difference between
"folder omitted" and "folder null" that ``_read``/``_search`` genuinely
depend on — is preserved exactly.
"""
from __future__ import annotations

from typing import Any

from .primitives import mode_field
from ..tool_family.manual import MANUAL_INPUT_SCHEMA

# The canonical operational/manual action order. Identical to the pre-settings
# flat ``schema.py`` ``action`` enum, including family-owned ``manual`` last;
# the generic declaration seam inserts ``settings`` immediately before it.
ACTION_ORDER: tuple[str, ...] = (
    "send", "check", "read", "dismiss", "reply", "reply_all",
    "search", "archive", "delete",
    "contacts", "add_contact", "remove_contact", "edit_contact",
    "manual",
)

# --- Shared field descriptions, verbatim from the pre-migration flat schema ---

_ADDRESS_DESCRIPTION = "Bare name/path for send; string or list."
_CC_DESCRIPTION = "Visible CC addresses."
_BCC_DESCRIPTION = "Hidden BCC addresses."
_ATTACHMENTS_DESCRIPTION = "Attachment paths for send."
_SUBJECT_DESCRIPTION = "Subject."
_MESSAGE_DESCRIPTION = "Body; max 50,000 characters."
_EMAIL_ID_DESCRIPTION = "Own mailbox ID list; replies use one ID."
_N_DESCRIPTION = "Max messages for check; default 10."
_QUERY_DESCRIPTION = "Regex query over sender, subject, and body."
_FOLDER_DESCRIPTION = "Folder; check inbox/search both; sent is read-only."
_DELAY_DESCRIPTION = "Delivery delay in seconds; default 0."
_TYPE_DESCRIPTION = "Send type; default normal."
_NAME_DESCRIPTION = "Contact name."
_NOTE_DESCRIPTION = "Contact note."

# The ``filter`` object for ``check`` keeps the flat schema's property set,
# defaults, and first-call matching semantics; its long catalog and examples
# live in the Email manual. ``additionalProperties: False`` is added because a
# migrated family's ``input`` branches are closed all the way down
# (``tools/CONTRACT.md`` "Envelope": "Action branches are closed").
_FILTER_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "Optional check filters; see email-manual for fields and defaults.",
    "properties": {
        "sort": {
            "type": ["string", "null"],
            "enum": ["newest", "oldest", None],
            "description": "Sort newest (default) or oldest.",
        },
        "from": {
            "type": ["string", "null"],
            "description": "Case-insensitive sender substring.",
        },
        "subject": {
            "type": ["string", "null"],
            "description": "Case-insensitive subject substring.",
        },
        "contains": {
            "type": ["string", "null"],
            "description": "Case-insensitive body substring.",
        },
        "after": {
            "type": ["string", "null"],
            "description": "Only messages after an ISO 8601 timestamp.",
        },
        "before": {
            "type": ["string", "null"],
            "description": "Only messages before an ISO 8601 timestamp.",
        },
        "unread_only": {
            "type": ["boolean", "null"],
            "description": "Only unread messages.",
        },
        "has_attachments": {
            "type": ["boolean", "null"],
            "description": "Only messages with attachments.",
        },
        "truncate": {
            "type": ["integer", "null"],
            "description": "Preview characters; default 500, 0 means full body.",
        },
    },
    "required": [
        "sort", "from", "subject", "contains", "after", "before",
        "unread_only", "has_attachments", "truncate",
    ],
    "additionalProperties": False,
}


def _mode_property() -> dict[str, Any]:
    """``send``'s optional address-mode field, nullable-wrapped.

    Reuses ``primitives.mode_field`` — the one owned definition of this
    field's enum and its long routing description — rather than restating it,
    so the peer/abs guidance cannot drift between the legacy flat schema and
    this one. Only the nullable representation is added on top.
    """
    field = dict(mode_field())
    field["type"] = ["string", "null"]
    field["enum"] = [*field["enum"], None]
    return field


_SEND_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "address": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
            "description": _ADDRESS_DESCRIPTION,
        },
        "subject": {"type": ["string", "null"], "description": _SUBJECT_DESCRIPTION},
        "message": {"type": ["string", "null"], "description": _MESSAGE_DESCRIPTION},
        "cc": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": _CC_DESCRIPTION,
        },
        "bcc": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": _BCC_DESCRIPTION,
        },
        "attachments": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": _ATTACHMENTS_DESCRIPTION,
        },
        "delay": {"type": ["integer", "null"], "description": _DELAY_DESCRIPTION},
        "mode": _mode_property(),
        "type": {
            "type": ["string", "null"],
            "enum": ["normal", None],
            "description": _TYPE_DESCRIPTION,
        },
    },
    "required": [
        "address", "subject", "message", "cc", "bcc", "attachments",
        "delay", "mode", "type",
    ],
    "additionalProperties": False,
}

_CHECK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "folder": {
            "type": ["string", "null"],
            "enum": ["inbox", "sent", "archive", None],
            "description": _FOLDER_DESCRIPTION,
        },
        "n": {"type": ["integer", "null"], "description": _N_DESCRIPTION},
        "filter": _FILTER_SCHEMA,
    },
    "required": ["folder", "n", "filter"],
    "additionalProperties": False,
}

_READ_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email_id": {
            "type": "array",
            "items": {"type": "string"},
            "description": _EMAIL_ID_DESCRIPTION,
        },
        "folder": {
            "type": ["string", "null"],
            "enum": ["inbox", "sent", "archive", None],
            "description": _FOLDER_DESCRIPTION,
        },
    },
    "required": ["email_id", "folder"],
    "additionalProperties": False,
}

# ``dismiss`` takes no ``folder``: it is inbox-only by construction
# (``manager._dismiss`` resolves each id and treats anything outside the inbox
# as ``already_handled``), so admitting the key would advertise an argument the
# action never reads.
_DISMISS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email_id": {
            "type": "array",
            "items": {"type": "string"},
            "description": _EMAIL_ID_DESCRIPTION,
        },
    },
    "required": ["email_id"],
    "additionalProperties": False,
}

# ``reply``/``reply_all`` share a shape: a single-element ``email_id`` list,
# the required body, and the optional subject override plus cc/bcc fan-out.
def _reply_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "email_id": {
                "type": "array",
                "items": {"type": "string"},
                "description": _EMAIL_ID_DESCRIPTION,
            },
            "message": {"type": "string", "description": _MESSAGE_DESCRIPTION},
            "subject": {
                "type": ["string", "null"],
                "description": _SUBJECT_DESCRIPTION,
            },
            "cc": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": _CC_DESCRIPTION,
            },
            "bcc": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": _BCC_DESCRIPTION,
            },
        },
        "required": ["email_id", "message", "subject", "cc", "bcc"],
        "additionalProperties": False,
    }


_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": _QUERY_DESCRIPTION},
        "folder": {
            "type": ["string", "null"],
            "enum": ["inbox", "sent", "archive", None],
            "description": _FOLDER_DESCRIPTION,
        },
    },
    "required": ["query", "folder"],
    "additionalProperties": False,
}

# ``archive`` moves inbox mail only — no ``folder`` argument exists in
# ``manager._archive``.
_ARCHIVE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email_id": {
            "type": "array",
            "items": {"type": "string"},
            "description": _EMAIL_ID_DESCRIPTION,
        },
    },
    "required": ["email_id"],
    "additionalProperties": False,
}

# ``delete`` accepts only the two writable folders; ``sent`` is read-only and
# ``manager._delete`` rejects it with "Cannot delete from folder: sent". The
# enum here narrows the advertised choice to what the action actually allows,
# while that runtime rejection stays in place for direct/internal callers.
_DELETE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email_id": {
            "type": "array",
            "items": {"type": "string"},
            "description": _EMAIL_ID_DESCRIPTION,
        },
        "folder": {
            "type": ["string", "null"],
            "enum": ["inbox", "archive", None],
            "description": _FOLDER_DESCRIPTION,
        },
    },
    "required": ["email_id", "folder"],
    "additionalProperties": False,
}

_CONTACTS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_ADD_CONTACT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "address": {"type": "string", "description": _ADDRESS_DESCRIPTION},
        "name": {"type": "string", "description": _NAME_DESCRIPTION},
        "note": {"type": ["string", "null"], "description": _NOTE_DESCRIPTION},
    },
    "required": ["address", "name", "note"],
    "additionalProperties": False,
}

_REMOVE_CONTACT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "address": {"type": "string", "description": _ADDRESS_DESCRIPTION},
    },
    "required": ["address"],
    "additionalProperties": False,
}

# ``edit_contact`` distinguishes absent from present for ``name``/``note``
# (``manager._edit_contact`` uses ``if "name" in args``), which the null-strip
# in ``__init__.py`` preserves: a null becomes absent and leaves the stored
# field untouched, exactly as omitting it did pre-migration.
_EDIT_CONTACT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "address": {"type": "string", "description": _ADDRESS_DESCRIPTION},
        "name": {"type": ["string", "null"], "description": _NAME_DESCRIPTION},
        "note": {"type": ["string", "null"], "description": _NOTE_DESCRIPTION},
    },
    "required": ["address", "name", "note"],
    "additionalProperties": False,
}

#: One strict ``input_schema`` per operational/manual action. The reserved ``manual``
#: child references the exported canonical ``MANUAL_INPUT_SCHEMA`` literal
#: rather than restating it (``tool_family/CONTRACT.md``: families MUST NOT
#: restate it locally), so the schema-only family composed here and the real
#: dispatching family in ``__init__.py`` — which registers the shared
#: ManualTool child — advertise byte-identical ``manual`` input.
INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "send": _SEND_INPUT_SCHEMA,
    "check": _CHECK_INPUT_SCHEMA,
    "read": _READ_INPUT_SCHEMA,
    "dismiss": _DISMISS_INPUT_SCHEMA,
    "reply": _reply_input_schema(),
    "reply_all": _reply_input_schema(),
    "search": _SEARCH_INPUT_SCHEMA,
    "archive": _ARCHIVE_INPUT_SCHEMA,
    "delete": _DELETE_INPUT_SCHEMA,
    "contacts": _CONTACTS_INPUT_SCHEMA,
    "add_contact": _ADD_CONTACT_INPUT_SCHEMA,
    "remove_contact": _REMOVE_CONTACT_INPUT_SCHEMA,
    "edit_contact": _EDIT_CONTACT_INPUT_SCHEMA,
    "manual": MANUAL_INPUT_SCHEMA,
}

# The canonical concise ``action`` enum router. It retains first-call action
# choice and critical safety guidance; action procedures and examples live in
# the Email manual references (``tools/CONTRACT.md`` "Dispatch and actions").
ACTION_ENUM_DESCRIPTION = (
    "Choose one action; put only that action's fields in input. send: new internal "
    "message (address and message required; body max 50,000 characters). check: "
    "list/filter mail. read: fetch mailbox IDs and mark them read; dismiss: mark "
    "handled IDs read without returning bodies. Unread bodies are injected in full "
    "into persistent Email notifications: prefer dismiss after handling; use read "
    "for source records or attachments. reply/reply_all: answer existing mail on "
    "the arrival channel. search: regex lookup. archive/delete: move or remove "
    "inbox/archive mail. contacts actions manage the address book. settings is "
    "read-only. manual returns this manual without mailbox I/O."
)
