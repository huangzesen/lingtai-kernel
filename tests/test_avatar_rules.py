"""Tests for .rules signal consumption and system/rules.md persistence."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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
