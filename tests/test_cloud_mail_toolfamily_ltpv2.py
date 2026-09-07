"""Focused Cloud Mail LTP-v2 family tests: strict envelope, dispatch boundary."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from lingtai.mcp_servers.cloud_mail import manager as cloud_mail_mgr
from lingtai.mcp_servers.cloud_mail._family import (
    CLOUD_MAIL_ACTIONS,
    CLOUD_MAIL_SCHEMA,
    _basic_validate,
    _cloud_mail_input_schemas,
    handle_cloud_mail,
)
from lingtai.mcp_servers.cloud_mail.settings import (
    ACCOUNTS_COMMENT,
    CONFIG_ENV,
    CONFIG_PATH_COMMENT,
    CloudMailSettingsProvider,
)


class _CountingManager:
    def __init__(self, *, config_path: str | None = None) -> None:
        self.calls: list[dict] = []
        self._config_path = Path(config_path) if config_path is not None else None

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    def handle(self, args: dict) -> dict:
        self.calls.append(dict(args))
        return {"status": "ok", "action": args.get("action")}


def _branches(schema: dict) -> dict[str, dict]:
    inputs = schema["properties"]["input"]
    branches = inputs.get("oneOf") or inputs.get("anyOf")
    return {branch["title"].removesuffix(" input"): branch for branch in branches}


def test_schema_declares_strict_root_envelope():
    assert set(CLOUD_MAIL_SCHEMA["properties"]) == {"action", "input", "reasoning", "summarize"}
    assert CLOUD_MAIL_SCHEMA["required"] == ["action", "input", "reasoning"]
    assert CLOUD_MAIL_SCHEMA["additionalProperties"] is False
    assert CLOUD_MAIL_SCHEMA["properties"]["action"]["enum"] == list(CLOUD_MAIL_ACTIONS)
    assert set(_branches(CLOUD_MAIL_SCHEMA)) == set(CLOUD_MAIL_ACTIONS)


def test_family_dispatch_rejects_root_and_cross_branch_before_manager_io():
    manager = _CountingManager()
    valid = {"action": "accounts", "input": {}, "reasoning": "schema probe"}
    invalid = [
        # legacy compatibility field must not be forwarded/accepted
        {"action": "accounts", "input": {}, "reasoning": "x", "_reasoning": "legacy"},
        {"action": "accounts", "input": {}, "reasoning": "x", "unknown": 1},
        {"action": "accounts", "input": {}, "reasoning": 7},
        # cross-branch: 'address' belongs to send, not accounts
        {"action": "accounts", "input": {"address": "x@y.com"}, "reasoning": "x"},
        # send requires 'address'
        {"action": "send", "input": {"message": "missing address"}, "reasoning": "x"},
        # unknown action
        {"action": "bogus", "input": {}, "reasoning": "x"},
        # missing input
        {"action": "check", "reasoning": "x"},
        # missing reasoning
        {"action": "check", "input": {}},
        # summarize wrong type
        {"action": "check", "input": {}, "reasoning": "x", "summarize": "yes"},
    ]
    for args in invalid:
        result = handle_cloud_mail(manager, args)
        assert result["status"] == "failed", args
        assert manager.calls == []
    ok = handle_cloud_mail(manager, valid)
    assert ok["status"] == "ok"
    assert len(manager.calls) == 1
    assert manager.calls[0] == {"action": "accounts"}


def test_send_requires_address_and_accepts_valid_input():
    manager = _CountingManager()
    result = handle_cloud_mail(
        manager,
        {
            "action": "send",
            "input": {"address": ["a@x.com", "b@x.com"], "message": "hi"},
            "reasoning": "send test",
        },
    )
    assert result["status"] == "ok"
    assert manager.calls[-1] == {
        "action": "send", "address": ["a@x.com", "b@x.com"], "message": "hi",
    }
    assert not _basic_validate({}, _branches(CLOUD_MAIL_SCHEMA)["send"])
    assert _basic_validate({"address": "x@y.com"}, _branches(CLOUD_MAIL_SCHEMA)["send"])


def test_reasoning_and_summarize_never_reach_manager_input():
    manager = _CountingManager()
    handle_cloud_mail(
        manager,
        {
            "action": "check",
            "input": {"limit": 5},
            "reasoning": "why",
            "summarize": True,
        },
    )
    assert manager.calls == [{"action": "check", "limit": 5}]


def test_manual_action_answers_from_plugin_without_entering_manager():
    manager = _CountingManager()
    result = handle_cloud_mail(manager, {"action": "manual", "input": {}, "reasoning": "x"})
    assert result["status"] == "ok"
    assert result["action"] == "manual"
    assert result["skill"] == "cloud-mail-mcp-manual"
    # ``manual`` is plugin-owned: it never dispatches into the manager, even
    # when a live manager is present.
    assert manager.calls == []


def test_manual_action_without_manager_returns_bundled_skill():
    result = handle_cloud_mail(None, {"action": "manual", "input": {}, "reasoning": "x"})
    assert result["status"] == "ok"
    assert result["action"] == "manual"
    assert result["skill"] == "cloud-mail-mcp-manual"
    assert isinstance(result["manual"], str) and result["manual"].strip()


def test_settings_provider_uses_only_the_applied_startup_snapshot(monkeypatch):
    manager = _CountingManager(config_path="resolved-config.json")
    monkeypatch.setenv(CONFIG_ENV, "unrelated-later-value.json")
    provider = CloudMailSettingsProvider(manager)

    rows = provider()
    assert [row.key for row in rows] == ["config_path", "accounts"]
    assert [row.current for row in rows] == ["resolved-config.json", "configured"]
    assert [row.default for row in rows] == [None, None]
    assert [row.configurable for row in rows] == [True, True]
    assert [row.comment for row in rows] == [
        CONFIG_PATH_COMMENT,
        ACCOUNTS_COMMENT,
    ]
    assert [row._sensitive for row in rows] == [True, True]
    assert CONFIG_PATH_COMMENT == "cloud-mail-mcp-manual#config-path"
    assert ACCOUNTS_COMMENT == "cloud-mail-mcp-manual#accounts-document"
    assert "### Config path" in cloud_mail_mgr._SKILL_BODY
    assert "### Accounts document" in cloud_mail_mgr._SKILL_BODY
    assert manager.calls == []


def test_settings_success_is_exact_five_field_redacted_projection():
    manager = _CountingManager(config_path="resolved-config.json")
    result = handle_cloud_mail(
        manager,
        {"action": "settings", "input": {}, "reasoning": "inventory"},
    )

    assert result == {
        "settings": [
            {
                "key": "config_path",
                "current": "<redacted>",
                "default": "<redacted>",
                "configurable": True,
                "comment": "cloud-mail-mcp-manual#config-path",
            },
            {
                "key": "accounts",
                "current": "<redacted>",
                "default": "<redacted>",
                "configurable": True,
                "comment": "cloud-mail-mcp-manual#accounts-document",
            },
        ]
    }
    assert manager.calls == []
    serialized = json.dumps(result)
    assert "resolved-config" not in serialized
    assert CONFIG_ENV not in serialized
    assert "configured" not in serialized


def test_settings_requires_empty_input_and_fails_whole_without_startup_truth():
    expected = {
        "status": "failed",
        "error_code": "SETTINGS_UNAVAILABLE",
        "message": "settings inventory is unavailable",
    }
    for manager in (None, _CountingManager()):
        result = handle_cloud_mail(
            manager,
            {"action": "settings", "input": {}, "reasoning": "diagnose startup"},
        )
        assert result == expected
        assert "settings" not in result

    rejected = handle_cloud_mail(
        _CountingManager(config_path="resolved-config.json"),
        {
            "action": "settings",
            "input": {"set": "config_path"},
            "reasoning": "probe",
        },
    )
    assert rejected == {
        "status": "failed",
        "error_code": "INVALID_ARGUMENT",
        "message": "invalid cloud_mail input",
    }


def test_family_validation_and_no_manager_operational_stub_stay_unchanged():
    invalid_calls = (
        {"action": "bogus", "input": {}, "reasoning": "x"},
        {"action": "accounts", "input": {"address": "x@y.com"}, "reasoning": "x"},
        {"action": "send", "input": {}, "reasoning": "x"},
    )
    for call in invalid_calls:
        rejected = handle_cloud_mail(None, call)
        assert rejected["status"] == "failed"
        assert rejected["error_code"] in {"ACTION_REQUIRED", "INVALID_ARGUMENT"}

    unavailable_stub = handle_cloud_mail(
        None,
        {"action": "accounts", "input": {}, "reasoning": "inspect accounts"},
    )
    assert unavailable_stub == {}


def test_openai_responses_scrub_preserves_family_root_and_action_branches():
    from lingtai.llm.openai.adapter import _scrub_responses_schema

    wire = _scrub_responses_schema(copy.deepcopy(CLOUD_MAIL_SCHEMA), is_root=True)
    assert wire["required"] == CLOUD_MAIL_SCHEMA["required"]
    assert wire["properties"]["action"]["enum"] == list(CLOUD_MAIL_ACTIONS)
    assert wire["properties"]["input"]["anyOf"]
    assert wire["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Manual (SKILL.md) truthfulness — guards against re-drifting to a flat/legacy
# example or an undocumented root ``summarize`` control (CONTRACT.md
# "Dispatch and actions" MUST).
# ---------------------------------------------------------------------------

def test_manual_never_teaches_the_retired_check_n_alias():
    # 'n' was a flat-shape alias for 'limit' on the pre-migration manager
    # dispatch; the LTP-v2 'check' input branch does not accept it
    # (additionalProperties: false), so the manual must not teach it.
    assert "check" in _cloud_mail_input_schemas()
    assert "n" not in _cloud_mail_input_schemas()["check"]["properties"]
    body = cloud_mail_mgr._SKILL_BODY
    assert not re.search(r"`limit`/`n`", body)
    assert not re.search(r"optional[^.\n]*\bn\b[^.\n]*filters", body, re.IGNORECASE)
