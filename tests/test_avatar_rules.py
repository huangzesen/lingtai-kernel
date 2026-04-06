"""Tests for .rules signal consumption and system/rules.md persistence."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from lingtai.capabilities.avatar import AvatarManager

import pytest


class TestRulesHeartbeatWatch:
    """Test that the heartbeat loop consumes .rules signal and persists to system/rules.md."""

    def _make_agent(self, tmp_path):
        from lingtai.agent import Agent

        svc = MagicMock()
        svc.get_adapter.return_value = MagicMock()
        svc.provider = "gemini"
        svc.model = "gemini-test"
        wd = tmp_path / "agent"
        agent = Agent(service=svc, agent_name="test", working_dir=wd)
        return agent

    def test_rules_signal_consumed_and_persisted(self, tmp_path):
        """Writing .rules should: inject section, persist to system/rules.md, delete .rules."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # No rules section initially
        assert agent._prompt_manager.read_section("rules") is None

        # Write .rules signal file
        (wd / ".rules").write_text("No deleting files.\nAlways log actions.")

        # Simulate one heartbeat tick
        agent._check_rules_file()

        # Section injected
        assert agent._prompt_manager.read_section("rules") == "No deleting files.\nAlways log actions."
        # Persisted to system/rules.md
        assert (wd / "system" / "rules.md").read_text() == "No deleting files.\nAlways log actions."
        # Signal file consumed (deleted)
        assert not (wd / ".rules").is_file()

    def test_rules_diff_skips_identical(self, tmp_path):
        """If .rules content matches system/rules.md, no prompt refresh."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # Pre-load rules into section and canonical file
        agent._prompt_manager.write_section("rules", "No deleting files.", protected=True)
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("No deleting files.")

        # Write identical .rules signal
        (wd / ".rules").write_text("No deleting files.")

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_not_called()

        # Signal still consumed even if content is identical
        assert not (wd / ".rules").is_file()

    def test_rules_diff_refreshes_on_change(self, tmp_path):
        """If .rules content differs from system/rules.md, prompt is refreshed."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # Pre-load old rules
        agent._prompt_manager.write_section("rules", "Old rules.", protected=True)
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("Old rules.")

        # Write new .rules signal
        (wd / ".rules").write_text("New rules.")

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_called_once()

        assert agent._prompt_manager.read_section("rules") == "New rules."
        assert (system_dir / "rules.md").read_text() == "New rules."
        assert not (wd / ".rules").is_file()

    def test_rules_loaded_from_system_on_init(self, tmp_path):
        """If system/rules.md exists at agent start, rules section should be pre-loaded."""
        wd = tmp_path / "agent"
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("Pre-existing rules.")

        from lingtai.agent import Agent
        svc = MagicMock()
        svc.get_adapter.return_value = MagicMock()
        svc.provider = "gemini"
        svc.model = "gemini-test"
        agent = Agent(service=svc, agent_name="test", working_dir=wd)

        # Rules should be loaded from system/rules.md during init
        assert agent._prompt_manager.read_section("rules") == "Pre-existing rules."

    def test_rules_unlink_failure_skips_processing(self, tmp_path, monkeypatch):
        """If .rules cannot be unlinked, the function should return WITHOUT calling flush."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir
        (wd / ".rules").write_text("Some rules.")

        # Make Path.unlink raise OSError
        original_unlink = Path.unlink
        def failing_unlink(self, *args, **kwargs):
            if self.name == ".rules":
                raise PermissionError("simulated unlink failure")
            return original_unlink(self, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", failing_unlink)

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_not_called()
        # File should still exist (we couldn't unlink it)
        assert (wd / ".rules").is_file()


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


class TestAvatarRulesAction:
    """Test avatar(action=rules) distribution."""

    def test_rules_requires_admin(self, tmp_path):
        """Non-admin agent cannot set rules."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="worker",
            working_dir=tmp_path / "worker",
            capabilities=["avatar"],
            admin={},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "No deleting.",
        })
        assert "error" in result

    def test_rules_persists_to_system_rules_md(self, tmp_path):
        """Admin agent should persist rules to system/rules.md (canonical copy)."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="admin",
            working_dir=tmp_path / "admin",
            capabilities=["avatar"],
            admin={"karma": True},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Always log actions.",
        })
        assert result["status"] == "ok"
        # Canonical copy in system/rules.md
        assert (agent._working_dir / "system" / "rules.md").read_text() == "Always log actions."
        # Prompt section updated
        assert agent._prompt_manager.read_section("rules") == "Always log actions."

    def test_rules_distributes_signals_to_descendants(self, tmp_path):
        """Rules should write .rules signal files to all descendant directories.

        IMPORTANT: As of v0.5.13, the ledger stores relative directory names
        (e.g. 'child_a'), not absolute paths. Descendants live as siblings of
        the parent agent in the same `.lingtai/` directory.
        """
        from lingtai.agent import Agent

        # All agents are siblings under tmp_path (mimicking .lingtai/ layout)
        parent_dir = tmp_path / "parent"
        child_a_dir = tmp_path / "child_a"
        child_b_dir = tmp_path / "child_b"
        child_a_dir.mkdir(parents=True)
        child_b_dir.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # Write ledger entries with RELATIVE names (current convention)
        ledger_dir = parent_dir / "delegates"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = ledger_dir / "ledger.jsonl"
        with open(ledger_path, "w") as f:
            f.write(json.dumps({"event": "avatar", "name": "a", "working_dir": "child_a"}) + "\n")
            f.write(json.dumps({"event": "avatar", "name": "b", "working_dir": "child_b"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "No external API calls.",
        })
        assert result["status"] == "ok"
        # Descendants get .rules signal files (their heartbeats will consume them)
        assert (child_a_dir / ".rules").read_text() == "No external API calls."
        assert (child_b_dir / ".rules").read_text() == "No external API calls."
        # Parent gets canonical system/rules.md (not a signal)
        assert (parent_dir / "system" / "rules.md").read_text() == "No external API calls."
        # distributed_to reports relative names
        assert set(result["distributed_to"]) == {"child_a", "child_b"}

    def test_rules_distributes_recursively(self, tmp_path):
        """Rules should propagate to grandchildren (avatars of avatars).

        All three agents are siblings under tmp_path. The ledger records use
        relative names; resolution happens against the parent's parent dir.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        child_dir = tmp_path / "child"
        grandchild_dir = tmp_path / "grandchild"
        for d in (parent_dir, child_dir, grandchild_dir):
            d.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # Parent → child ledger (relative name "child")
        p_ledger = parent_dir / "delegates" / "ledger.jsonl"
        p_ledger.parent.mkdir(parents=True, exist_ok=True)
        p_ledger.write_text(json.dumps({"event": "avatar", "name": "child", "working_dir": "child"}) + "\n")

        # Child → grandchild ledger (relative name "grandchild")
        c_ledger = child_dir / "delegates" / "ledger.jsonl"
        c_ledger.parent.mkdir(parents=True, exist_ok=True)
        c_ledger.write_text(json.dumps({"event": "avatar", "name": "gc", "working_dir": "grandchild"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Be concise.",
        })
        assert result["status"] == "ok"
        # All descendants get .rules signal files
        assert (child_dir / ".rules").read_text() == "Be concise."
        assert (grandchild_dir / ".rules").read_text() == "Be concise."

    def test_rules_requires_content(self, tmp_path):
        """action=rules without rules_content should error."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="admin",
            working_dir=tmp_path / "admin",
            capabilities=["avatar"],
            admin={"karma": True},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({"action": "rules"})
        assert "error" in result

    def test_spawn_default_action(self, tmp_path):
        """Omitting action should default to spawn (backward compatible).

        NOTE: Real spawning launches a subprocess. We patch _launch to avoid
        that, and pre-create init.json so _spawn reaches the launch path.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        agent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
        )

        # _spawn requires parent to have init.json
        (parent_dir / "init.json").write_text(
            json.dumps({"manifest": {"agent_name": "parent", "admin": {}}})
        )

        mgr = agent.get_capability("avatar")
        with patch.object(AvatarManager, "_launch", return_value=12345):
            result = mgr.handle({"name": "child", "dir": "child"})
        assert result["status"] == "ok"
        assert result["agent_name"] == "child"
        assert result["address"] == "child"  # relative name (current convention)

    def test_rules_root_not_self_distributed_via_cycle(self, tmp_path):
        """If a descendant's ledger references the root, root should NOT receive .rules.

        Verifies that _walk_avatar_tree's visited set is seeded with root, so cycles
        through root cannot cause the admin's own dir to appear in the distribution set.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        child_dir = tmp_path / "child"
        child_dir.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # parent → child
        p_ledger = parent_dir / "delegates" / "ledger.jsonl"
        p_ledger.parent.mkdir(parents=True, exist_ok=True)
        p_ledger.write_text(json.dumps({"event": "avatar", "name": "child", "working_dir": "child"}) + "\n")

        # child → parent (malicious cycle pointing back to root)
        c_ledger = child_dir / "delegates" / "ledger.jsonl"
        c_ledger.parent.mkdir(parents=True, exist_ok=True)
        c_ledger.write_text(json.dumps({"event": "avatar", "name": "parent", "working_dir": "parent"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Cycle test.",
        })
        assert result["status"] == "ok"
        # Child receives the signal
        assert (child_dir / ".rules").read_text() == "Cycle test."
        # Root should NOT receive a .rules signal — it gets system/rules.md directly
        assert not (parent_dir / ".rules").is_file()
        # distributed_to should contain only child, not parent
        assert "parent" not in result["distributed_to"]
        assert "child" in result["distributed_to"]


class TestAutoDistributeAfterSpawn:
    """After avatar(action=spawn), parent's rules should be distributed to newborn.

    These tests mock _launch to avoid actually spawning subprocesses, and
    pre-create the parent's init.json so the spawn code path can proceed
    to ledger append and rules distribution.
    """

    def _setup_spawnable_parent(self, tmp_path, with_rules: bool):
        """Build a parent agent with init.json, optionally with system/rules.md."""
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )
        (parent_dir / "init.json").write_text(
            json.dumps({"manifest": {"agent_name": "parent", "admin": {"karma": True}}})
        )
        if with_rules:
            system_dir = parent_dir / "system"
            system_dir.mkdir(parents=True, exist_ok=True)
            (system_dir / "rules.md").write_text("Always be concise.")
        return parent, parent_dir

    def test_spawn_distributes_existing_rules(self, tmp_path):
        """If parent has system/rules.md, spawning should write .rules to new avatar."""
        parent, parent_dir = self._setup_spawnable_parent(tmp_path, with_rules=True)

        mgr = parent.get_capability("avatar")
        with patch.object(AvatarManager, "_launch", return_value=12345):
            result = mgr.handle({"name": "child", "dir": "child"})
        assert result["status"] == "ok"

        # Child dir is a sibling of parent_dir (avatar_working_dir = parent.parent / dir_name)
        child_dir = parent_dir.parent / "child"
        # Child gets .rules signal file (heartbeat will consume and persist it)
        assert (child_dir / ".rules").read_text() == "Always be concise."

    def test_spawn_without_rules_no_distribution(self, tmp_path):
        """If parent has no system/rules.md, spawn should not create .rules in child."""
        parent, parent_dir = self._setup_spawnable_parent(tmp_path, with_rules=False)

        mgr = parent.get_capability("avatar")
        with patch.object(AvatarManager, "_launch", return_value=12345):
            result = mgr.handle({"name": "child", "dir": "child"})
        assert result["status"] == "ok"

        child_dir = parent_dir.parent / "child"
        assert not (child_dir / ".rules").is_file()
