"""Tests for the redesigned library capability."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _mk_agent(tmp_path: Path, library_cfg: dict | None = None):
    """Create an agent with the library capability, optionally passing kwargs."""
    caps = {"library": library_cfg or {}}
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=caps,
    )
    return agent, workdir


def _write_skill(folder: Path, name: str, desc: str = "test skill"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBody of {name}.\n"
    )


# ---------------------------------------------------------------------------
# Structure & setup
# ---------------------------------------------------------------------------


def test_library_setup_creates_per_agent_directories(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        assert (workdir / ".library").is_dir()
        assert (workdir / ".library" / "intrinsic").is_dir()
        assert (workdir / ".library" / "intrinsic" / "capabilities").is_dir()
        assert (workdir / ".library" / "intrinsic" / "addons").is_dir()
        assert (workdir / ".library" / "custom").is_dir()
    finally:
        agent.stop(timeout=1.0)


def test_library_setup_hard_copies_intrinsics(tmp_path):
    # The Agent initializer installs each loaded capability's manual/ bundle
    # into intrinsic/capabilities/<cap>/. The library capability documents
    # itself like every other capability.
    agent, workdir = _mk_agent(tmp_path)
    try:
        skill_md = (
            workdir / ".library" / "intrinsic" / "capabilities" / "library" / "SKILL.md"
        )
        assert skill_md.is_file()
        assert "name: library-manual" in skill_md.read_text()
    finally:
        agent.stop(timeout=1.0)


def test_library_setup_overwrites_stale_intrinsic(tmp_path):
    # The Agent initializer wipes-and-rewrites intrinsic/ on construction.
    # A stale entry from a previous kernel version must be replaced.
    workdir = tmp_path / "agent"
    stale = (
        workdir / ".library" / "intrinsic" / "capabilities" / "library" / "SKILL.md"
    )
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("---\nname: library-manual\ndescription: STALE\n---\n")

    # Also leave a stale top-level dir to confirm wipe-and-rewrite scrubs old layouts.
    old_layout = workdir / ".library" / "intrinsic" / "skill-for-skill" / "SKILL.md"
    old_layout.parent.mkdir(parents=True, exist_ok=True)
    old_layout.write_text("---\nname: skill-for-skill\ndescription: ANCIENT\n---\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {}},
    )
    try:
        body = stale.read_text()
        assert "STALE" not in body
        assert "The Library Capability" in body or "library-manual" in body
        # Old layout scrubbed.
        assert not old_layout.exists()
    finally:
        agent.stop(timeout=1.0)


def test_library_setup_leaves_custom_untouched(tmp_path):
    workdir = tmp_path / "agent"
    user_skill = workdir / ".library" / "custom" / "my-tool" / "SKILL.md"
    user_skill.parent.mkdir(parents=True, exist_ok=True)
    user_skill.write_text("---\nname: my-tool\ndescription: Mine\n---\nUser content.\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {}},
    )
    try:
        assert user_skill.read_text() == "---\nname: my-tool\ndescription: Mine\n---\nUser content.\n"
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_library_scans_absolute_path(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "shared-skill", "shared-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        assert result["status"] == "ok"
        assert result["paths"][str(extra)]["skills"] == 1
        assert result["catalog_size"] >= 2  # library-manual + shared-skill
    finally:
        agent.stop(timeout=1.0)


def test_library_resolves_relative_path_from_working_dir(tmp_path):
    # Build a network-root layout: tmp_path is the network root.
    # The agent lives at tmp_path/agent, and .library_shared sits at tmp_path/.library_shared.
    shared = tmp_path / ".library_shared"
    _write_skill(shared / "net-skill", "net-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": ["../.library_shared"]})
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        assert result["status"] == "ok"
        assert result["paths"]["../.library_shared"]["exists"] is True
        assert result["paths"]["../.library_shared"]["skills"] == 1
    finally:
        agent.stop(timeout=1.0)


def test_library_expands_tilde(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    utils = fake_home / "my-utils"
    _write_skill(utils / "util-skill", "util-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": ["~/my-utils"]})
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        assert result["paths"]["~/my-utils"]["exists"] is True
    finally:
        agent.stop(timeout=1.0)


def test_library_reports_missing_path_as_not_existing(tmp_path):
    agent, _ = _mk_agent(tmp_path, {"paths": ["/does/not/exist"]})
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        assert result["paths"]["/does/not/exist"]["exists"] is False
        assert result["paths"]["/does/not/exist"]["skills"] == 0
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# info action
# ---------------------------------------------------------------------------


def test_info_returns_library_manual_body(tmp_path):
    agent, _ = _mk_agent(tmp_path)
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        assert "library_manual" in result
        assert "name: library-manual" in result["library_manual"]
    finally:
        agent.stop(timeout=1.0)


def test_info_reports_ok_when_healthy(tmp_path):
    agent, _ = _mk_agent(tmp_path)
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        assert result["status"] == "ok"
        assert "error" not in result
    finally:
        agent.stop(timeout=1.0)


def test_info_reports_degraded_when_intrinsic_missing(tmp_path):
    # The library capability is pure presentation — it does NOT reinstall
    # manuals when info is called. So if the initializer-installed manual is
    # deleted out-of-band after setup, info must report degraded.
    agent, workdir = _mk_agent(tmp_path)
    try:
        manual_path = (
            workdir / ".library" / "intrinsic" / "capabilities" / "library" / "SKILL.md"
        )
        assert manual_path.is_file(), "precondition: initializer installed manual"
        manual_path.unlink()

        result = agent._tool_handlers["library"]({"action": "info"})
        assert result["status"] == "degraded"
        assert "error" in result
    finally:
        agent.stop(timeout=1.0)


def test_info_surfaces_problems(tmp_path):
    workdir = tmp_path / "agent"
    # Pre-create a broken custom skill (missing description frontmatter).
    bad = workdir / ".library" / "custom" / "broken" / "SKILL.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nname: broken\n---\nno description!\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {}},
    )
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        problem_folders = [p["folder"] for p in result["problems"]]
        assert any("broken" in f for f in problem_folders)
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_catalog_injected_into_library_section(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "shared-thing", "shared-thing")

    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        prompt = agent._prompt_manager.read_section("library") or ""
        assert "<available_skills>" in prompt
        assert "library-manual" in prompt
        assert "shared-thing" in prompt
    finally:
        agent.stop(timeout=1.0)


def test_custom_skills_appear_in_catalog(tmp_path):
    workdir = tmp_path / "agent"
    _write_skill(workdir / ".library" / "custom" / "my-tool", "my-tool", "my desc")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {}},
    )
    try:
        prompt = agent._prompt_manager.read_section("library") or ""
        assert "my-tool" in prompt
        assert "my desc" in prompt
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# No git operations
# ---------------------------------------------------------------------------


def test_library_does_not_create_git_repo(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        assert not (workdir / ".library" / ".git").exists()
    finally:
        agent.stop(timeout=1.0)
