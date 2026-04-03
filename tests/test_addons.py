from __future__ import annotations
from unittest.mock import MagicMock, patch


def test_addon_registry():
    from lingtai.addons import _BUILTIN
    assert "imap" in _BUILTIN
    assert "telegram" in _BUILTIN
    assert "feishu" in _BUILTIN


def test_agent_addon_lifecycle():
    """Agent should accept addons parameter."""
    from lingtai.agent import Agent
    import inspect
    sig = inspect.signature(Agent.__init__)
    assert "addons" in sig.parameters


def test_setup_single_account():
    from lingtai.addons.imap import setup
    agent = MagicMock()
    agent._working_dir = "/tmp/test"
    with patch("lingtai.addons.imap.FilesystemMailService"):
        mgr = setup(agent, email_address="a@gmail.com", email_password="x",
                    bridge_port=8399)
    assert mgr is not None
    agent.add_tool.assert_called_once()
    assert agent.add_tool.call_args[0][0] == "imap"


def test_setup_multi_account():
    from lingtai.addons.imap import setup
    agent = MagicMock()
    agent._working_dir = "/tmp/test"
    with patch("lingtai.addons.imap.FilesystemMailService"):
        mgr = setup(agent, accounts=[
            {"email_address": "a@gmail.com", "email_password": "x"},
            {"email_address": "b@outlook.com", "email_password": "y"},
        ], bridge_port=8399)
    assert mgr is not None
    call_kwargs = agent.add_tool.call_args[1]
    assert "a@gmail.com" in call_kwargs["system_prompt"]
    assert "b@outlook.com" in call_kwargs["system_prompt"]


def test_setup_no_account_raises():
    from lingtai.addons.imap import setup
    agent = MagicMock()
    agent._working_dir = "/tmp/test"
    import pytest
    with pytest.raises(ValueError):
        setup(agent, bridge_port=8399)


def test_feishu_in_registry():
    from lingtai.addons import _BUILTIN
    assert "feishu" in _BUILTIN
    assert _BUILTIN["feishu"] == ".feishu"


def test_feishu_setup_single_account(tmp_path):
    from lingtai.addons.feishu import setup
    agent = MagicMock()
    agent._working_dir = str(tmp_path)
    mgr = setup(agent, app_id="cli_test", app_secret="secret")
    assert mgr is not None
    agent.add_tool.assert_called_once()
    assert agent.add_tool.call_args[0][0] == "feishu"


def test_feishu_setup_no_credentials_raises(tmp_path):
    from lingtai.addons.feishu import setup
    import pytest
    agent = MagicMock()
    agent._working_dir = str(tmp_path)
    with pytest.raises(ValueError):
        setup(agent)
