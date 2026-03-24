"""Tests for BaseAgent — true name (immutable) and nickname (mutable)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent


def make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


def test_agent_no_name(tmp_path):
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    assert agent.agent_name is None
    assert agent.nickname is None
    assert agent.working_dir == tmp_path / "test"


def test_set_name_once(tmp_path):
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_name("悟空")
    assert agent.agent_name == "悟空"


def test_set_name_twice_fails(tmp_path):
    """True name is immutable — cannot be set twice."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_name("悟空")
    with pytest.raises(RuntimeError, match="True name already set"):
        agent.set_name("八戒")
    assert agent.agent_name == "悟空"


def test_set_name_empty_fails(tmp_path):
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    with pytest.raises(ValueError, match="cannot be empty"):
        agent.set_name("")


def test_agent_with_name_at_construction_is_immutable(tmp_path):
    """Name given at construction is a true name — immutable."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test", agent_name="alice")
    assert agent.agent_name == "alice"
    with pytest.raises(RuntimeError, match="True name already set"):
        agent.set_name("bob")


def test_nickname_mutable(tmp_path):
    """Nickname can be set and changed freely."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test", agent_name="悟空")
    assert agent.nickname is None
    agent.set_nickname("代码探索者")
    assert agent.nickname == "代码探索者"
    agent.set_nickname("bug猎手")
    assert agent.nickname == "bug猎手"


def test_nickname_clear(tmp_path):
    """Empty nickname clears it to None."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_nickname("explorer")
    assert agent.nickname == "explorer"
    agent.set_nickname("")
    assert agent.nickname is None


def test_set_name_updates_manifest(tmp_path):
    import json

    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    agent.set_name("悟空")
    manifest = json.loads((agent.working_dir / ".agent.json").read_text())
    assert manifest["agent_name"] == "悟空"


def test_nickname_in_manifest(tmp_path):
    import json

    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test", agent_name="悟空")
    agent.set_nickname("代码探索者")
    manifest = json.loads((agent.working_dir / ".agent.json").read_text())
    assert manifest["agent_name"] == "悟空"
    assert manifest["nickname"] == "代码探索者"
