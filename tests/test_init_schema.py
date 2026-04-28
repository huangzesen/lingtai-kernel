import json
import pytest
from lingtai.init_schema import validate_init


def _valid_init() -> dict:
    """Return a minimal valid init.json dict."""
    return {
        "manifest": {
            "agent_name": "alice",
            "language": "en",
            "llm": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "api_key": None,
                "base_url": None,
            },
            "capabilities": {},
            "soul": {"delay": 120},
            "stamina": 3600,
            "context_limit": None,
            "molt_pressure": 0.8,
            "molt_prompt": "",
            "max_turns": 50,
            "admin": {"karma": True},
            "streaming": False,
        },
        "principle": "",
        "covenant": "",
        "pad": "",
        "prompt": "",
        "soul": "",
    }


def test_valid_init_passes():
    validate_init(_valid_init())  # should not raise


def test_missing_top_level_key():
    data = _valid_init()
    del data["covenant"]
    with pytest.raises(ValueError, match="covenant"):
        validate_init(data)


def test_missing_manifest_field():
    """Only manifest.llm is truly required — other fields are optional."""
    data = _valid_init()
    del data["manifest"]["llm"]
    with pytest.raises(ValueError, match="manifest.llm"):
        validate_init(data)


def test_minimal_init_passes():
    """Bare-minimum init.json: only manifest.llm with provider+model."""
    data = {
        "manifest": {
            "llm": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
            },
        },
        "principle": "",
        "covenant": "",
        "pad": "",
        "prompt": "",
        "soul": "",
    }
    validate_init(data)  # should not raise


def test_missing_llm_field():
    data = _valid_init()
    del data["manifest"]["llm"]["provider"]
    with pytest.raises(ValueError, match="manifest.llm.provider"):
        validate_init(data)


def test_wrong_type_top_level():
    data = _valid_init()
    data["covenant"] = 123
    with pytest.raises(ValueError, match="covenant.*str"):
        validate_init(data)


def test_wrong_type_manifest_field():
    data = _valid_init()
    data["manifest"]["stamina"] = "one hour"
    with pytest.raises(ValueError, match="manifest.stamina.*(int|float|number)"):
        validate_init(data)


def test_wrong_type_capabilities():
    data = _valid_init()
    data["manifest"]["capabilities"] = ["file", "bash"]
    with pytest.raises(ValueError, match="manifest.capabilities.*object"):
        validate_init(data)


def test_wrong_type_streaming():
    data = _valid_init()
    data["manifest"]["streaming"] = "yes"
    with pytest.raises(ValueError, match="manifest.streaming.*bool"):
        validate_init(data)


def test_bool_rejected_for_numeric_field():
    """bool is a subclass of int in Python — must be rejected for numeric fields."""
    data = _valid_init()
    data["manifest"]["stamina"] = True
    with pytest.raises(ValueError, match="manifest.stamina.*number.*bool"):
        validate_init(data)


# --- optional fields ---


def test_env_file_optional():
    data = _valid_init()
    validate_init(data)  # no env_file — should pass
    data["env_file"] = "~/.lingtai/.env"
    validate_init(data)  # with env_file — should pass


def test_env_file_wrong_type():
    data = _valid_init()
    data["env_file"] = 123
    with pytest.raises(ValueError, match="env_file.*str"):
        validate_init(data)


def test_api_key_env_optional():
    data = _valid_init()
    data["manifest"]["llm"]["api_key_env"] = "MY_KEY"
    data["env_file"] = ".env"  # required when api_key_env is used without api_key
    validate_init(data)


def test_api_key_env_wrong_type():
    data = _valid_init()
    data["manifest"]["llm"]["api_key_env"] = 123
    with pytest.raises(ValueError, match="api_key_env.*str"):
        validate_init(data)


# --- addons ---


def test_addons_optional():
    data = _valid_init()
    validate_init(data)  # no addons — should pass


def test_addons_imap_valid():
    data = _valid_init()
    data["addons"] = {
        "imap": {
            "email_address": "test@gmail.com",
            "email_password": "secret",
        },
    }
    validate_init(data)


def test_addons_imap_with_env():
    data = _valid_init()
    data["addons"] = {
        "imap": {
            "email_address": "test@gmail.com",
            "email_password_env": "IMAP_PASS",
        },
    }
    validate_init(data)


def test_addons_imap_missing_password():
    data = _valid_init()
    data["addons"] = {
        "imap": {
            "email_address": "test@gmail.com",
        },
    }
    with pytest.raises(ValueError, match="email_password"):
        validate_init(data)


def test_addons_imap_missing_email():
    data = _valid_init()
    data["addons"] = {
        "imap": {
            "email_password": "secret",
        },
    }
    with pytest.raises(ValueError, match="email_address"):
        validate_init(data)


def test_addons_telegram_valid():
    data = _valid_init()
    data["addons"] = {
        "telegram": {
            "bot_token": "123:ABC",
        },
    }
    validate_init(data)


def test_addons_telegram_with_env():
    data = _valid_init()
    data["addons"] = {
        "telegram": {
            "bot_token_env": "TG_TOKEN",
        },
    }
    validate_init(data)


def test_addons_telegram_missing_token():
    data = _valid_init()
    data["addons"] = {
        "telegram": {},
    }
    with pytest.raises(ValueError, match="bot_token"):
        validate_init(data)


def test_addons_feishu_valid():
    data = _valid_init()
    data["addons"] = {
        "feishu": {
            "config": "feishu.json",
        },
    }
    validate_init(data)


def test_addons_feishu_with_env():
    data = _valid_init()
    data["addons"] = {
        "feishu": {
            "app_id_env": "FEISHU_APP_ID",
            "app_secret_env": "FEISHU_APP_SECRET",
        },
    }
    warnings = validate_init(data)
    # inline credentials without config file should produce a warning
    assert any("feishu" in w and "config" in w for w in warnings)


def test_addons_feishu_missing_config_warns():
    data = _valid_init()
    data["addons"] = {"feishu": {}}
    warnings = validate_init(data)
    assert any("feishu" in w for w in warnings)


def test_addons_feishu_config_wrong_type():
    data = _valid_init()
    data["addons"] = {"feishu": {"config": 42}}
    with pytest.raises(ValueError, match="feishu.config"):
        validate_init(data)


def test_addons_feishu_not_object():
    data = _valid_init()
    data["addons"] = {"feishu": "cli_xxx"}
    with pytest.raises(ValueError, match="feishu"):
        validate_init(data)


def test_time_awareness_field_valid_bool():
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "time_awareness": False,
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    warnings = validate_init(data)
    assert all("time_awareness" not in w for w in warnings)


def test_time_awareness_field_wrong_type_raises():
    import pytest
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "time_awareness": "yes",
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    with pytest.raises(ValueError):
        validate_init(data)


def test_timezone_awareness_field_valid_bool():
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "timezone_awareness": False,
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    warnings = validate_init(data)
    assert all("timezone_awareness" not in w for w in warnings)


def test_timezone_awareness_field_wrong_type_raises():
    import pytest
    from lingtai.init_schema import validate_init

    data = {
        "manifest": {
            "llm": {"provider": "minimax", "model": "x"},
            "timezone_awareness": "yes",
        },
        "covenant": "hi",
        "prompt": "hello",
        "pad": "",
        "soul": "",
        "principle": "",
    }
    with pytest.raises(ValueError):
        validate_init(data)


# --- schema self-consistency (drift prevention) ---
#
# The schema maintains two parallel structures per scope: an OPTIONAL dict
# (field name -> expected type, used for type validation) and a KNOWN set
# (used to suppress "unknown field" warnings). When a new field is added,
# both must be updated. These tests catch the common drift where one is
# updated and the other is forgotten.


def test_manifest_optional_fields_all_in_known():
    """Every optional manifest field must also be in MANIFEST_KNOWN,
    otherwise a valid use of that field would produce a spurious
    'unknown field' warning."""
    from lingtai.init_schema import MANIFEST_OPTIONAL, MANIFEST_KNOWN
    missing = set(MANIFEST_OPTIONAL) - MANIFEST_KNOWN
    assert not missing, (
        f"Fields in MANIFEST_OPTIONAL but not in MANIFEST_KNOWN "
        f"(would trigger unknown-field warning): {sorted(missing)}"
    )


def test_manifest_required_fields_all_in_known():
    """Every required manifest field must also be in MANIFEST_KNOWN."""
    from lingtai.init_schema import MANIFEST_REQUIRED, MANIFEST_KNOWN
    missing = set(MANIFEST_REQUIRED) - MANIFEST_KNOWN
    assert not missing, (
        f"Fields in MANIFEST_REQUIRED but not in MANIFEST_KNOWN: {sorted(missing)}"
    )


def test_manifest_known_fields_all_typed():
    """Every field in MANIFEST_KNOWN must appear in either MANIFEST_REQUIRED
    or MANIFEST_OPTIONAL, otherwise a user-supplied value passes without
    any type check."""
    from lingtai.init_schema import MANIFEST_OPTIONAL, MANIFEST_REQUIRED, MANIFEST_KNOWN
    typed = set(MANIFEST_OPTIONAL) | set(MANIFEST_REQUIRED)
    untyped = MANIFEST_KNOWN - typed
    assert not untyped, (
        f"Fields in MANIFEST_KNOWN but not type-checked (missing from "
        f"MANIFEST_OPTIONAL or MANIFEST_REQUIRED): {sorted(untyped)}"
    )


def test_top_optional_fields_all_in_known():
    """Every optional top-level field must also be in TOP_KNOWN."""
    from lingtai.init_schema import TOP_OPTIONAL, TOP_KNOWN
    missing = set(TOP_OPTIONAL) - TOP_KNOWN
    assert not missing, (
        f"Fields in TOP_OPTIONAL but not in TOP_KNOWN "
        f"(would trigger unknown-field warning): {sorted(missing)}"
    )


def test_manifest_accepts_pseudo_agent_subscriptions():
    data = _valid_init()
    data["manifest"]["pseudo_agent_subscriptions"] = ["../human", "../announcements"]
    warnings = validate_init(data)
    # No warnings related to this field.
    for w in warnings:
        assert "pseudo_agent_subscriptions" not in w, f"unexpected warning: {w}"


def test_manifest_rejects_non_list_pseudo_agent_subscriptions():
    import pytest
    data = _valid_init()
    data["manifest"]["pseudo_agent_subscriptions"] = "../human"  # string, not list
    with pytest.raises(ValueError, match="pseudo_agent_subscriptions"):
        validate_init(data)


def test_active_preset_field_accepted():
    """`manifest.active_preset` is a known optional field."""
    data = _valid_init()
    data["manifest"]["active_preset"] = "default"
    validate_init(data)  # should not raise


def test_presets_path_field_accepted():
    """`manifest.presets_path` is a known optional field."""
    data = _valid_init()
    data["manifest"]["presets_path"] = "/some/path"
    data["manifest"]["active_preset"] = "default"
    validate_init(data)  # should not raise


def test_presets_path_set_without_active_preset_raises():
    """`presets_path` without `active_preset` is invalid."""
    data = _valid_init()
    data["manifest"]["presets_path"] = "/some/path"
    with pytest.raises(ValueError, match="active_preset"):
        validate_init(data)


def test_active_preset_wrong_type_raises():
    """`active_preset` must be a string."""
    data = _valid_init()
    data["manifest"]["active_preset"] = 42
    with pytest.raises(ValueError, match="active_preset"):
        validate_init(data)


def test_presets_path_wrong_type_raises():
    """`presets_path` must be a string."""
    data = _valid_init()
    data["manifest"]["presets_path"] = ["a", "b"]
    data["manifest"]["active_preset"] = "default"
    with pytest.raises(ValueError, match="presets_path"):
        validate_init(data)
