"""Tests for BaseAgent — agent_name optional, set_name()."""
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
    agent = BaseAgent(service=make_mock_service(), base_dir=tmp_path)
    assert agent.agent_name is None
    assert agent.agent_id
    assert agent.working_dir == tmp_path / agent.agent_id


def test_set_name_once(tmp_path):
    agent = BaseAgent(service=make_mock_service(), base_dir=tmp_path)
    agent.set_name("悟空")
    assert agent.agent_name == "悟空"


def test_set_name_twice_fails(tmp_path):
    agent = BaseAgent(service=make_mock_service(), base_dir=tmp_path)
    agent.set_name("悟空")
    with pytest.raises(RuntimeError, match="already named"):
        agent.set_name("八戒")


def test_set_name_empty_fails(tmp_path):
    agent = BaseAgent(service=make_mock_service(), base_dir=tmp_path)
    with pytest.raises(ValueError, match="cannot be empty"):
        agent.set_name("")


def test_agent_with_name_at_construction(tmp_path):
    agent = BaseAgent(service=make_mock_service(), base_dir=tmp_path, agent_name="alice")
    assert agent.agent_name == "alice"
    with pytest.raises(RuntimeError, match="already named"):
        agent.set_name("bob")


def test_set_name_updates_manifest(tmp_path):
    import json

    agent = BaseAgent(service=make_mock_service(), base_dir=tmp_path)
    agent.set_name("悟空")
    manifest = json.loads((agent.working_dir / ".agent.json").read_text())
    assert manifest["agent_name"] == "悟空"
