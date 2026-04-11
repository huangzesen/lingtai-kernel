"""Skills capability — shared skill store with registration and catalog injection.

Skills are Markdown files (SKILL.md) with YAML frontmatter that teach the
agent specialized behaviors.  All skills live in a shared git-tracked store
at ``.lingtai/.skills/<skill-name>/SKILL.md`` — a sibling directory to the
agent working dirs, accessible to every agent in the same network.

Individual skill folders may themselves be git repos (cloned from a remote).
The outer ``.skills`` repo tracks the files and ignores inner ``.git`` dirs.

Usage: Agent(capabilities=["skills"])

Tool actions:
    register — validate all skill folders, git add + commit changes.
    refresh  — rescan the store and re-inject the XML catalog into the
               system prompt so newly added skills become available.

SKILL.md format::

    ---
    name: my-skill
    description: One-line description of what this skill does
    version: 1.0.0
    ---

    Full instructions in Markdown…

Required frontmatter: name, description.
Optional frontmatter: version, author, tags (list[str]).
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

log = logging.getLogger(__name__)

PROVIDERS = {"providers": [], "default": "builtin"}

# ---------------------------------------------------------------------------
# Frontmatter parser (minimal, no PyYAML dependency)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^(\w[\w-]*)\s*:\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse YAML-like frontmatter from a SKILL.md file.

    Only handles simple ``key: value`` lines (no nesting, no lists).
    Returns a dict of string key→value pairs.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    return {kv.group(1): kv.group(2).strip() for kv in _KV_RE.finditer(block)}


def _resolve_skills_dir(agent: "BaseAgent") -> Path:
    """Resolve the shared skills directory for this agent's network.

    Skills live at ``.lingtai/.skills/`` — a sibling to agent working dirs.
    Agent working dirs are ``<network>/<agent-name>/``, so the skills dir
    is ``<network>/.skills/``.
    """
    return agent._working_dir.parent / ".skills"


# ---------------------------------------------------------------------------
# XML catalog builder
# ---------------------------------------------------------------------------

def _escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_catalog_xml(skills: list[dict], lang: str) -> str:
    """Build the XML skill catalog for system prompt injection.

    Each *skill* dict has keys: name, description, path.
    """
    if not skills:
        return ""

    lines = [
        t(lang, "skills.preamble"),
        "",
        "<available_skills>",
    ]
    for sk in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_escape_xml(sk['name'])}</name>")
        lines.append(f"    <description>{_escape_xml(sk['description'])}</description>")
        lines.append(f"    <location>{_escape_xml(sk['path'])}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skill scanner
# ---------------------------------------------------------------------------

def _scan_skills(skills_dir: Path) -> tuple[list[dict], list[dict]]:
    """Scan ``skills_dir`` for skill folders.

    Returns (valid_skills, problems) where each valid skill is
    ``{name, description, path, version}`` and each problem is
    ``{folder, reason}``.
    """
    if not skills_dir.is_dir():
        return [], []

    valid: list[dict] = []
    problems: list[dict] = []

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        # Skip hidden dirs (.git, etc.)
        if skill_dir.name.startswith("."):
            continue

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            problems.append({
                "folder": skill_dir.name,
                "reason": "missing SKILL.md",
            })
            continue

        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as e:
            problems.append({
                "folder": skill_dir.name,
                "reason": f"cannot read SKILL.md: {e}",
            })
            continue

        fm = _parse_frontmatter(text)
        name = fm.get("name", "")
        description = fm.get("description", "")
        if not name:
            problems.append({
                "folder": skill_dir.name,
                "reason": "SKILL.md missing required frontmatter field: name",
            })
            continue
        if not description:
            problems.append({
                "folder": skill_dir.name,
                "reason": "SKILL.md missing required frontmatter field: description",
            })
            continue

        valid.append({
            "name": name,
            "description": description,
            "version": fm.get("version", ""),
            "path": str(skill_file),
        })

    return valid, problems


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(skills_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in the skills directory."""
    return subprocess.run(
        ["git", *args],
        cwd=str(skills_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _ensure_git_repo(skills_dir: Path) -> None:
    """Ensure the skills directory exists and is a git repo."""
    skills_dir.mkdir(parents=True, exist_ok=True)

    gitdir = skills_dir / ".git"
    if not gitdir.exists():
        _git(skills_dir, "init")
        # Ignore inner .git dirs (skills that are themselves git repos)
        gitignore = skills_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("**/.git\n")
        _git(skills_dir, "add", ".gitignore")
        _git(skills_dir, "commit", "-m", "init skills store")


# ---------------------------------------------------------------------------
# Tool actions
# ---------------------------------------------------------------------------

def _action_register(agent: "BaseAgent", skills_dir: Path) -> dict:
    """Validate skill folders, git add + commit changes."""
    _ensure_git_repo(skills_dir)
    valid, problems = _scan_skills(skills_dir)

    # Stage all changes (new files, modifications, deletions)
    _git(skills_dir, "add", "-A")

    # Check if there's anything to commit
    status = _git(skills_dir, "status", "--porcelain")
    staged = status.stdout.strip()

    commit_msg = ""
    if staged:
        # Build commit message from skill names
        skill_names = [s["name"] for s in valid]
        msg = f"register: {', '.join(skill_names)}" if skill_names else "register: update skills"
        result = _git(skills_dir, "commit", "-m", msg)
        if result.returncode == 0:
            commit_msg = msg
        else:
            commit_msg = f"commit failed: {result.stderr.strip()}"

    # Re-inject catalog (or clear if no valid skills remain)
    lang = agent._config.language
    catalog_xml = _build_catalog_xml(valid, lang)
    if catalog_xml:
        agent.update_system_prompt("skills", catalog_xml, protected=True)
    else:
        agent.update_system_prompt("skills", "", protected=True)

    return {
        "status": "ok",
        "skills_dir": str(skills_dir),
        "registered": [
            {"name": s["name"], "description": s["description"], "version": s["version"]}
            for s in valid
        ],
        "problems": problems,
        "committed": commit_msg,
    }


def _action_refresh(agent: "BaseAgent", skills_dir: Path) -> dict:
    """Rescan skills and re-inject the XML catalog into system prompt."""
    valid, problems = _scan_skills(skills_dir)

    lang = agent._config.language
    catalog_xml = _build_catalog_xml(valid, lang)
    if catalog_xml:
        agent.update_system_prompt("skills", catalog_xml, protected=True)
    else:
        agent.update_system_prompt("skills", "", protected=True)

    return {
        "status": "ok",
        "skills_dir": str(skills_dir),
        "loaded": [
            {"name": s["name"], "description": s["description"], "version": s["version"]}
            for s in valid
        ],
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# Capability setup
# ---------------------------------------------------------------------------

def get_description(lang: str = "en") -> str:
    return t(lang, "skills.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["register", "refresh"],
                "description": t(lang, "skills.action"),
            },
        },
        "required": ["action"],
    }


def setup(agent: "BaseAgent") -> None:
    """Set up the skills capability — tool + initial catalog injection."""
    lang = agent._config.language
    skills_dir = _resolve_skills_dir(agent)

    def handle_skills(args: dict) -> dict:
        action = args.get("action", "")
        if action == "register":
            return _action_register(agent, skills_dir)
        elif action == "refresh":
            return _action_refresh(agent, skills_dir)
        else:
            return {"status": "error", "message": f"unknown action: {action!r}, use 'register' or 'refresh'"}

    agent.add_tool(
        "skills",
        schema=get_schema(lang),
        handler=handle_skills,
        description=get_description(lang),
    )

    # Initial catalog injection — scan and inject on startup
    valid, _ = _scan_skills(skills_dir)
    if valid:
        catalog_xml = _build_catalog_xml(valid, lang)
        agent.update_system_prompt("skills", catalog_xml, protected=True)
        log.info("skills: injected %d skill(s) from %s", len(valid), skills_dir)
    else:
        log.debug("skills: no skills found in %s", skills_dir)
