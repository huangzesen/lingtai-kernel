"""``email``'s own migration evidence for the LTP v2 / ToolFamily rules.

This is one family's local evidence, chosen for *this* family's risks, not a
conformance suite (``tools/CONTRACT.md`` "Contract tests" explicitly permits —
and requires — a per-family evidence set). Email's distinguishing risks over
the already-migrated families are:

* it is a **mutating, side-effecting** family: a cross-action smuggle must be
  rejected *before* any mailbox I/O, delivery thread, or read-state change;
* it owns a **producer notification** (``.notification/email.json``) whose
  rerender is driven by read-state mutations, which the envelope must not
  disturb;
* it keeps a **reserved non-public action** (``unread``) that is deliberately
  not a child and must keep its exact pre-migration rejection;
* its 13 operational actions plus reserved ``settings``/``manual`` children
  form a wide registry, so
  ``action``-to-``input`` correlation and per-action key isolation carry more
  weight here than in a two-child family.

Every test builds its own isolated agent under ``tmp_path`` with a mocked mail
service, so nothing here delivers real mail to a real peer or human.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.kernel.base_agent import BaseAgent
from lingtai.tools import email as email_tool
from lingtai.tools.email._family_schema import ACTION_ORDER, INPUT_SCHEMAS
from lingtai.tools.tool_family.manual import MANUAL_INPUT_SCHEMA
from tests._agent_presence_helpers import make_test_presence_store
from tests._lifecycle_clock_helpers import make_test_lifecycle_clock
from tests._notification_store_helpers import notification_store_for
from tests._service_helpers import make_gemini_mock_service as make_mock_service
from tests._snapshot_helpers import (
    make_test_snapshot_port,
    make_test_source_revision_port,
)
from tests._workdir_lease_helpers import make_test_lease

# The operational/manual list stays pinned; the settings seam adds its reserved
# child immediately before manual without editing ``ACTION_ORDER``.
_PUBLIC_ACTIONS = [
    "send", "check", "read", "dismiss", "reply", "reply_all",
    "search", "archive", "delete",
    "contacts", "add_contact", "remove_contact", "edit_contact",
    "settings",
    "manual",
]

_TEST_INTRINSICS = {"email": {"module": email_tool}}


def _agent(tmp_path: Path, *, with_mail: bool = True) -> BaseAgent:
    """Build one isolated agent whose mail service is a mock (never a peer)."""
    workdir = tmp_path / "agent"
    agent = BaseAgent(
        intrinsics=_TEST_INTRINSICS,
        service=make_mock_service(),
        agent_name="email-migration-test",
        working_dir=workdir,
        workdir_lease=make_test_lease(),
        snapshot_port=make_test_snapshot_port(),
        agent_presence=make_test_presence_store(),
        lifecycle_clock=make_test_lifecycle_clock(),
        source_revision_port=make_test_source_revision_port(),
        notification_store=notification_store_for(workdir),
    )
    if with_mail:
        mail = MagicMock()
        mail.address = "email-migration-test"
        mail.send.return_value = None
        agent._mail_service = mail
    return agent


def _seed_inbox(agent: BaseAgent, msg_id: str, **overrides) -> str:
    """Write one inbox message directly to this agent's own disposable mailbox."""
    msg_dir = agent._working_dir / "mailbox" / "inbox" / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "_mailbox_id": msg_id,
        "from": "peer-agent",
        "subject": "seeded",
        "message": "seeded body",
        "received_at": "2026-07-27T10:00:00Z",
    }
    payload.update(overrides)
    (msg_dir / "message.json").write_text(json.dumps(payload), encoding="utf-8")
    return msg_id


# ---------------------------------------------------------------------------
# 1. One model-facing root, closed envelope, unchanged public identity
# ---------------------------------------------------------------------------


def test_root_is_the_closed_ltp_v2_envelope_and_nothing_else():
    schema = email_tool.get_schema()
    assert set(schema["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert schema["required"] == ["action", "input", "reasoning"]
    assert schema["additionalProperties"] is False
    # The pre-migration flat root fields are gone from the model-facing schema.
    for legacy in ("address", "email_id", "folder", "query", "filter", "n", "mode"):
        assert legacy not in schema["properties"]


def test_settings_is_the_only_added_public_action_and_order_is_pinned():
    assert email_tool.get_schema()["properties"]["action"]["enum"] == _PUBLIC_ACTIONS
    assert list(ACTION_ORDER) == [
        action for action in _PUBLIC_ACTIONS if action != "settings"
    ]
    assert list(email_tool.DECLARATION.public_actions) == _PUBLIC_ACTIONS


def test_email_schema_keeps_first_call_safety_while_disclosing_depth():
    """The compact schema retains routing, cap, and channel guards."""
    schema = email_tool.get_schema()
    description = email_tool.get_description()
    action_description = schema["properties"]["action"]["description"]
    fields = schema["properties"]["input"]["anyOf"]
    prose = " ".join((description, action_description))
    assert "50,000" in prose
    assert "reply" in prose and "channel" in prose
    assert "manual" in prose
    assert any("email-manual" in str(branch) for branch in fields)


def test_email_manual_is_a_short_router_with_focused_references():
    """Long procedures stay available without inflating the installed router."""
    root = Path(__file__).resolve().parents[1]
    manual = root / "src/lingtai/tools/email/manual/SKILL.md"
    body = manual.read_text(encoding="utf-8")
    assert len(body) < 12_000
    assert "## Reference map" in body
    assert "Reply on the channel where the message arrived." in body
    for reference in (
        "addressing-and-replies",
        "actions-and-storage",
        "notifications-and-delivery",
        "settings-reference",
    ):
        path = manual.parent / "reference" / reference / "SKILL.md"
        assert path.is_file(), reference
        assert f"reference/{reference}/SKILL.md" in body


def test_reserved_unread_is_not_a_public_child_or_action():
    schema = email_tool.get_schema()
    assert "unread" not in schema["properties"]["action"]["enum"]
    assert "unread" not in ACTION_ORDER
    assert "unread" not in INPUT_SCHEMAS


def test_one_canonical_child_registry_drives_schema_and_dispatch(tmp_path):
    """The advertised actions and the dispatchable children are one list.

    This is the anti-drift proof the acceptance asks for: the ``action`` enum,
    the ``input.anyOf`` branch order, the ``allOf`` condition order, and the
    real per-call dispatching family's ``child_names`` all come from
    declaration plus settings provider, so an action cannot be advertised without being
    dispatchable (or vice versa).
    """
    schema = email_tool.get_schema()
    family = email_tool._build_family(_agent(tmp_path))
    assert list(family.child_names) == _PUBLIC_ACTIONS
    assert schema["properties"]["action"]["enum"] == list(family.child_names)
    assert [b["title"] for b in schema["properties"]["input"]["anyOf"]] == [
        "settings inventory input" if action == "settings" else f"{action} input"
        for action in family.child_names
    ]
    assert [
        c["if"]["properties"]["action"]["const"] for c in schema["allOf"]
    ] == list(family.child_names)


# ---------------------------------------------------------------------------
# 2. Strict per-action schemas + action/input correlation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ACTION_ORDER)
def test_every_action_input_schema_is_closed_and_self_describing(action):
    branch = INPUT_SCHEMAS[action]
    assert branch["type"] == "object"
    assert branch["additionalProperties"] is False
    # Every declared property is also required — the provider-compatible way to
    # express an optional field is nullable-and-required, per tools/CONTRACT.md.
    assert sorted(branch["required"]) == sorted(branch["properties"])
    # Envelope metadata is never action input.
    for reserved in ("action", "reasoning", "_reasoning", "summarize"):
        assert reserved not in branch["properties"]


def test_all_of_correlates_every_action_const_with_its_exact_branch_schema():
    schema = email_tool.get_schema()
    conditions = schema["allOf"]
    branches = schema["properties"]["input"]["anyOf"]
    assert len(conditions) == len(branches) == len(_PUBLIC_ACTIONS)
    for condition, branch, action in zip(conditions, branches, _PUBLIC_ACTIONS):
        assert condition["if"]["properties"]["action"]["const"] == action
        assert condition["if"]["required"] == ["action"]
        # Both surfaces derive from the same canonical child schema.
        assert condition["then"]["properties"]["input"] == {
            k: v for k, v in branch.items() if k != "title"
        }


def test_manual_child_reuses_the_canonical_strict_empty_input_literal():
    """``manual``'s advertised input is the one owned canonical spelling.

    ``tool_family/CONTRACT.md`` requires families NOT to restate it locally,
    so the schema-only family composed in ``get_schema`` and the real
    dispatching family (which registers the shared ManualTool child) advertise
    byte-identical ``manual`` input.
    """
    assert INPUT_SCHEMAS["manual"] == MANUAL_INPUT_SCHEMA
    branch = email_tool.get_schema()["properties"]["input"]["anyOf"][-1]
    assert {k: v for k, v in branch.items() if k != "title"} == MANUAL_INPUT_SCHEMA


def test_action_specific_fields_live_only_in_their_own_branch():
    """The whole point of the migration: no shared flat field bag.

    Pre-migration every one of these keys was legal on every action, because
    the root was one open object. Now each belongs to exactly the actions that
    read it.
    """
    owners = {
        "query": {"search"},
        "n": {"check"},
        "filter": {"check"},
        "attachments": {"send"},
        "delay": {"send"},
        "mode": {"send"},
        "type": {"send"},
        "note": {"add_contact", "edit_contact"},
        "name": {"add_contact", "edit_contact"},
    }
    for field, expected in owners.items():
        actual = {a for a in ACTION_ORDER if field in INPUT_SCHEMAS[a]["properties"]}
        assert actual == expected, field


# ---------------------------------------------------------------------------
# 3. Dispatch: validation, errors, and cross-action rejection before I/O
# ---------------------------------------------------------------------------


def test_cross_action_key_is_rejected_before_any_mailbox_io(tmp_path):
    """A smuggled foreign key fails with no delivery and no read-state change."""
    agent = _agent(tmp_path)
    seeded = _seed_inbox(agent, "cross-action-id")

    result = agent._intrinsics["email"](
        {"action": "check", "input": {"email_id": [seeded]}}
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "unsupported email input field"
    # No send happened and the seeded mail was NOT marked read.
    agent._mail_service.send.assert_not_called()
    read_json = agent._working_dir / "mailbox" / "read.json"
    assert not read_json.exists() or seeded not in json.loads(
        read_json.read_text(encoding="utf-8")
    )


def test_send_fields_cannot_be_smuggled_through_a_read_call(tmp_path):
    """The highest-consequence smuggle: no mail leaves via a read-shaped call."""
    agent = _agent(tmp_path)
    result = agent._intrinsics["email"](
        {
            "action": "read",
            "input": {"email_id": ["x"], "address": "peer", "message": "leak"},
        }
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"
    agent._mail_service.send.assert_not_called()


def test_unknown_root_field_is_rejected(tmp_path):
    agent = _agent(tmp_path)
    result = agent._intrinsics["email"](
        {"action": "contacts", "input": {}, "not_a_field": 1}
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["message"] == "unsupported email argument"


def test_non_boolean_summarize_is_rejected(tmp_path):
    agent = _agent(tmp_path)
    result = agent._intrinsics["email"](
        {"action": "contacts", "input": {}, "summarize": "yes"}
    )
    assert result["status"] == "failed"
    assert result["message"] == "summarize must be a boolean"


def test_unknown_and_absent_actions_keep_their_pre_migration_public_results(tmp_path):
    agent = _agent(tmp_path)
    assert agent._intrinsics["email"](
        {"action": "not-a-real-action", "input": {}}
    ) == {"error": "Unknown email action: not-a-real-action"}
    assert agent._intrinsics["email"]({"input": {}}) == {"error": "action is required"}


def test_unhashable_action_does_not_raise(tmp_path):
    """Invalid JSON can make ``action`` a list/dict — the issue #513 class.

    It must render a stable typed error rather than raising ``TypeError`` out
    of the dispatcher. An empty list/dict is additionally *falsy*, so it takes
    the same "action is required" branch that pre-migration
    ``EmailManager.handle`` used for a falsy action (``if not action``); a
    non-empty unhashable value is truthy and reports the unknown action.
    """
    agent = _agent(tmp_path)
    assert agent._intrinsics["email"]({"action": [], "input": {}}) == {
        "error": "action is required"
    }
    assert agent._intrinsics["email"]({"action": {}, "input": {}}) == {
        "error": "action is required"
    }
    assert agent._intrinsics["email"]({"action": ["x"], "input": {}}) == {
        "error": "Unknown email action: ['x']"
    }


def test_reserved_unread_keeps_its_exact_rejection_before_dispatch(tmp_path):
    """``unread`` is kernel-synthesized: not a child, and not an unknown action."""
    agent = _agent(tmp_path)
    result = agent._intrinsics["email"]({"action": "unread", "input": {}})
    assert result == {
        "status": "error",
        "message": (
            "email(action='unread', ...) is reserved for kernel-"
            "synthesized unread-mail digests and cannot be invoked "
            "directly. Use email(action='check') to view your inbox."
        ),
    }
    # It is emphatically NOT collapsed into the generic unknown-action result.
    assert "Unknown email action" not in result["message"]


def test_reserved_unread_rejection_is_not_shared_mutable_state(tmp_path):
    """Two calls must not be able to corrupt each other's pinned result."""
    agent = _agent(tmp_path)
    first = agent._intrinsics["email"]({"action": "unread", "input": {}})
    first["message"] = "mutated"
    second = agent._intrinsics["email"]({"action": "unread", "input": {}})
    assert "reserved for kernel-" in second["message"]


# ---------------------------------------------------------------------------
# 4. Envelope metadata never reaches a child; nulls become absent
# ---------------------------------------------------------------------------


def test_reasoning_and_summarize_never_reach_the_action_implementation(tmp_path, monkeypatch):
    """``reasoning``/``_reasoning``/``summarize``/``_tc_id`` are envelope-only."""
    agent = _agent(tmp_path)
    seen: list[dict] = []
    original = agent._email_manager.handle
    monkeypatch.setattr(
        agent._email_manager,
        "handle",
        lambda args: (seen.append(dict(args)), original(args))[1],
    )

    agent._intrinsics["email"](
        {
            "action": "contacts",
            "input": {},
            "reasoning": "audit trail",
            "summarize": True,
            "_tc_id": "tc-123",
        }
    )

    assert seen == [{"action": "contacts"}]
    for reserved in ("reasoning", "_reasoning", "summarize", "_tc_id"):
        assert reserved not in seen[0]


def test_explicit_nulls_are_stripped_so_defaults_still_apply(tmp_path, monkeypatch):
    """A strict provider sends ``{"folder": null}``; the manager must see absence.

    Without the strip, ``check`` would try to list a folder literally named
    ``None`` and ``edit_contact`` would blank a name the caller never sent.
    """
    agent = _agent(tmp_path)
    seen: list[dict] = []
    original = agent._email_manager.handle
    monkeypatch.setattr(
        agent._email_manager,
        "handle",
        lambda args: (seen.append(dict(args)), original(args))[1],
    )

    result = agent._intrinsics["email"](
        {"action": "check", "input": {"folder": None, "n": None, "filter": None}}
    )

    assert seen == [{"action": "check"}]
    assert result["status"] == "ok"


def test_nested_filter_nulls_are_absent_and_keep_truncate_default(tmp_path):
    """A strict all-null filter must not leak ``truncate=None`` to the manager."""
    agent = _agent(tmp_path)
    _seed_inbox(agent, "nested-null", message="x" * 700)
    result = agent._intrinsics["email"](
        {
            "action": "check",
            "input": {
                "folder": None,
                "n": None,
                "filter": {
                    "sort": None,
                    "from": None,
                    "subject": None,
                    "contains": None,
                    "after": None,
                    "before": None,
                    "unread_only": True,
                    "has_attachments": None,
                    "truncate": None,
                },
            },
        }
    )

    assert result["status"] == "ok"
    assert result["total"] == 1
    assert result["showing"] == 1
    preview = result["emails"][0]["preview"]
    assert preview.startswith("x" * 500)
    assert preview.endswith("... (200 more chars)")


def test_edit_contact_null_name_does_not_blank_the_stored_name(tmp_path):
    """The absent-vs-null distinction, proven on real stored state."""
    agent = _agent(tmp_path)
    agent._intrinsics["email"](
        {
            "action": "add_contact",
            "input": {"address": "peer", "name": "Peer Name", "note": "n"},
        }
    )
    result = agent._intrinsics["email"](
        {
            "action": "edit_contact",
            "input": {"address": "peer", "name": None, "note": "updated"},
        }
    )
    assert result["status"] == "updated"
    assert result["contact"]["name"] == "Peer Name"
    assert result["contact"]["note"] == "updated"


# ---------------------------------------------------------------------------
# 5. Manual: full body, model-visible path, no double wrap, exact public shape
# ---------------------------------------------------------------------------


def _install_manual(agent: BaseAgent, body: str) -> Path:
    path = (
        agent._working_dir
        / ".library" / "intrinsic" / "capabilities" / "email" / "SKILL.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_manual_child_returns_the_canonical_result_verbatim_no_double_wrap(tmp_path):
    """The *actually dispatched* child result carries the full body and path."""
    agent = _agent(tmp_path)
    body = "# Email manual\n\nFull body kept intact.\n"
    path = _install_manual(agent, body)

    raw = email_tool._build_family(agent).handle({"action": "manual", "input": {}})

    assert raw == {
        "status": "ok",
        "content": [{"type": "text", "text": body}],
        "structuredContent": {"manual_path": str(path)},
    }
    # No family-specific flat fields leaked into the canonical child result.
    assert "manual" not in raw


def test_manual_public_result_is_emails_exact_pre_migration_shape(tmp_path):
    """Email's public result is unchanged and gains no generic fields."""
    agent = _agent(tmp_path)
    body = "# Email manual\n\nFull body kept intact.\n"
    path = _install_manual(agent, body)

    result = agent._intrinsics["email"]({"action": "manual", "input": {}})

    assert result == {
        "status": "ok",
        "manual": body,
        "manual_path": str(path),
    }
    for generic in ("content", "structuredContent"):
        assert generic not in result


def test_manual_degraded_case_preserves_the_loader_error_verbatim(tmp_path):
    agent = _agent(tmp_path)  # no manual installed
    result = agent._intrinsics["email"]({"action": "manual", "input": {}})
    assert result["status"] == "degraded"
    assert result["manual"] == ""
    assert result["manual_path"].endswith("capabilities/email/SKILL.md")
    assert result["error"] == (
        "email manual missing — initializer may have failed or "
        "capability not installed correctly"
    )
    for generic in ("content", "structuredContent"):
        assert generic not in result


def test_manual_performs_no_mailbox_io(tmp_path):
    """``manual`` is read-only: no send, no read-state change."""
    agent = _agent(tmp_path)
    _install_manual(agent, "# Email manual\n")
    seeded = _seed_inbox(agent, "manual-no-io-id")

    agent._intrinsics["email"]({"action": "manual", "input": {}})

    agent._mail_service.send.assert_not_called()
    read_json = agent._working_dir / "mailbox" / "read.json"
    assert not read_json.exists() or seeded not in json.loads(
        read_json.read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 6. Read vs side-effect receipts, and producer notification semantics
# ---------------------------------------------------------------------------


def test_read_returns_bodies_marks_read_and_rerenders_the_digest(tmp_path):
    agent = _agent(tmp_path)
    seeded = _seed_inbox(agent, "read-receipt-id", message="body to read")

    result = agent._intrinsics["email"](
        {"action": "read", "input": {"email_id": [seeded], "folder": None}}
    )

    assert result["status"] == "ok"
    assert [e["message"] for e in result["emails"]] == ["body to read"]
    read_ids = json.loads(
        (agent._working_dir / "mailbox" / "read.json").read_text(encoding="utf-8")
    )
    assert seeded in read_ids


def test_dismiss_marks_read_without_returning_bodies(tmp_path):
    """Email's producer-specific read/dismiss split is unchanged by the envelope."""
    agent = _agent(tmp_path)
    seeded = _seed_inbox(agent, "dismiss-receipt-id", message="body not returned")

    result = agent._intrinsics["email"](
        {"action": "dismiss", "input": {"email_id": [seeded]}}
    )

    assert result == {"status": "ok", "dismissed": [seeded]}
    assert "emails" not in result
    read_ids = json.loads(
        (agent._working_dir / "mailbox" / "read.json").read_text(encoding="utf-8")
    )
    assert seeded in read_ids


def test_check_is_read_only_and_leaves_unread_state_untouched(tmp_path):
    agent = _agent(tmp_path)
    seeded = _seed_inbox(agent, "check-readonly-id")

    result = agent._intrinsics["email"]({"action": "check", "input": {}})

    assert result["status"] == "ok"
    assert result["total"] == 1
    read_json = agent._working_dir / "mailbox" / "read.json"
    assert not read_json.exists() or seeded not in json.loads(
        read_json.read_text(encoding="utf-8")
    )


def test_send_still_produces_its_exact_receipt_and_one_sent_record(tmp_path):
    agent = _agent(tmp_path)
    result = agent._intrinsics["email"](
        {
            "action": "send",
            "input": {
                "address": "peer-agent",
                "subject": "subject line",
                "message": "hello there",
                "cc": ["cc-agent"],
            },
        }
    )
    assert result == {
        "status": "sent",
        "to": ["peer-agent"],
        "cc": ["cc-agent"],
        "bcc": [],
        "delay": 0,
    }
    sent = list((agent._working_dir / "mailbox" / "sent").iterdir())
    assert len(sent) == 1


def test_body_over_the_50k_cap_is_still_refused_at_send_time(tmp_path):
    agent = _agent(tmp_path)
    result = agent._intrinsics["email"](
        {
            "action": "send",
            "input": {"address": "peer-agent", "message": "x" * 50_001},
        }
    )
    assert result["limit_chars"] == 50_000
    assert result["actual_chars"] == 50_001
    assert "50,000 character internal email" in result["error"]
    agent._mail_service.send.assert_not_called()


def test_contacts_lifecycle_results_are_unchanged(tmp_path):
    agent = _agent(tmp_path)
    added = agent._intrinsics["email"](
        {"action": "add_contact", "input": {"address": "a", "name": "A", "note": None}}
    )
    assert added["status"] == "added"
    assert added["contact"] == {"address": "a", "name": "A", "note": ""}

    listed = agent._intrinsics["email"]({"action": "contacts", "input": {}})
    assert listed == {"status": "ok", "contacts": [{"address": "a", "name": "A", "note": ""}]}

    removed = agent._intrinsics["email"](
        {"action": "remove_contact", "input": {"address": "a"}}
    )
    assert removed == {"status": "removed", "address": "a"}


def test_search_rejects_an_invalid_regex_unchanged(tmp_path):
    agent = _agent(tmp_path)
    result = agent._intrinsics["email"](
        {"action": "search", "input": {"query": "[unclosed", "folder": None}}
    )
    assert result["error"].startswith("Invalid regex:")


def test_delete_still_refuses_the_read_only_sent_folder(tmp_path):
    """The runtime guard survives even though the enum no longer offers ``sent``."""
    agent = _agent(tmp_path)
    result = agent._email_manager.handle(
        {"action": "delete", "email_id": ["x"], "folder": "sent"}
    )
    assert result == {"error": "Cannot delete from folder: sent"}


def test_manager_not_booted_returns_the_pinned_internal_error(tmp_path):
    """``handle()`` before ``boot()`` keeps its exact pre-migration message."""

    class _Bare:
        _working_dir = tmp_path

    result = email_tool.handle(_Bare(), {"action": "contacts", "input": {}})
    assert result == {
        "error": "Internal: email manager not initialized. boot() was not called."
    }


# ---------------------------------------------------------------------------
# 7. Kernel allowlist membership (or the advertised summarize is a lie)
# ---------------------------------------------------------------------------


def test_email_joined_the_ltp_v2_summarize_allowlist():
    """``build_schema`` advertises ``summarize``; the kernel must honor it."""
    from lingtai.kernel.tool_result_summary import (
        _LTP_V2_MIGRATED_FAMILIES,
        summary_requested,
    )

    assert "email" in _LTP_V2_MIGRATED_FAMILIES
    assert summary_requested({"summarize": True}, "email") is True
    assert summary_requested({"summarize": False}, "email") is False
