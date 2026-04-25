"""Confirm avatars inherit manifest.timezone_awareness from their parent."""
from lingtai.core.avatar import AvatarManager


def test_avatar_inherits_timezone_awareness_true():
    parent_init = {
        "manifest": {
            "agent_name": "mom",
            "timezone_awareness": True,
        },
        "covenant": "x",
        "prompt": "y",
    }
    child = AvatarManager._make_avatar_init(parent_init, "kid")
    assert child["manifest"]["timezone_awareness"] is True


def test_avatar_inherits_timezone_awareness_false():
    parent_init = {
        "manifest": {
            "agent_name": "mom",
            "timezone_awareness": False,
        },
        "covenant": "x",
        "prompt": "y",
    }
    child = AvatarManager._make_avatar_init(parent_init, "kid")
    assert child["manifest"]["timezone_awareness"] is False


def test_avatar_inherits_missing_field_as_missing():
    """If parent never set the field, child also lacks it (will default True at runtime)."""
    parent_init = {
        "manifest": {
            "agent_name": "mom",
        },
        "covenant": "x",
        "prompt": "y",
    }
    child = AvatarManager._make_avatar_init(parent_init, "kid")
    assert "timezone_awareness" not in child["manifest"]
