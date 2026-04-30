"""Library capability — per-agent skill catalog (pure presentation).

Every agent has its own ``<agent>/.library/``:

- ``intrinsic/capabilities/<cap>/`` and ``intrinsic/addons/<addon>/`` — manual
  bundles installed by the Agent initializer (wipe-and-rewrite on every
  ``_setup_from_init``). The library capability does NOT create or populate
  this directory.
- ``custom/`` — agent-authored skills. Never touched by any kernel code.

Additional paths come from ``init.json``:

``manifest.capabilities.library.paths``: list[str] — each entry is scanned
recursively and contributes to the ``<available_skills>`` XML injected into the
system prompt's ``library`` section. Paths may be absolute, relative to the
agent working dir, or tilde-prefixed.

This capability is pure presentation: it scans whatever is on disk and builds
the catalog. It never writes to ``.library/``. File installation is the
initializer's job.

Tool surface: a single ``info`` action that returns the library manual body
plus a runtime health snapshot.

Usage: ``Agent(capabilities={"library": {"paths": [...]}})`` or via init.json.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ...i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

log = logging.getLogger(__name__)

PROVIDERS = {"providers": [], "default": "builtin"}


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        loaded = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    # Coerce to str|str — YAML may produce ints/lists/None for unrelated keys
    # (e.g. version: 2.0). Multi-line scalars (>, |) collapse to clean strings.
    return {str(k): (" ".join(str(v).split()) if v is not None else "") for k, v in loaded.items()}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_path(p: str, working_dir: Path) -> Path:
    """Resolve a user-declared library path.

    - Tilde expansion (``~/foo`` → user home).
    - Absolute paths used as-is.
    - Relative paths resolved against the agent working dir.
    """
    expanded = Path(p).expanduser()
    if expanded.is_absolute():
        return expanded
    return (working_dir / expanded).resolve(strict=False)


# ---------------------------------------------------------------------------
# Skill scanner
# ---------------------------------------------------------------------------

def _parse_skill_file(skill_file: Path, label: str) -> tuple[dict | None, dict | None]:
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as e:
        return None, {"folder": label, "reason": f"cannot read SKILL.md: {e}"}

    fm = _parse_frontmatter(text)
    name = fm.get("name", "")
    description = fm.get("description", "")
    if not name:
        return None, {"folder": label, "reason": "SKILL.md missing required frontmatter field: name"}
    if not description:
        return None, {"folder": label, "reason": "SKILL.md missing required frontmatter field: description"}

    return {
        "name": name,
        "description": description,
        "version": fm.get("version", ""),
        "path": str(skill_file),
    }, None


def _scan_recursive(
    directory: Path,
    valid: list[dict],
    problems: list[dict],
    prefix: str = "",
) -> None:
    if not directory.is_dir():
        return

    try:
        children = sorted(directory.iterdir())
    except OSError:
        return

    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue

        label = f"{prefix}{child.name}" if prefix else child.name
        skill_file = child / "SKILL.md"

        if skill_file.is_file():
            sk, prob = _parse_skill_file(skill_file, label)
            if sk:
                valid.append(sk)
            if prob:
                problems.append(prob)
            continue

        # No SKILL.md — classify.
        try:
            grandchildren = list(child.iterdir())
        except OSError:
            continue
        has_loose_files = any(
            not c.is_dir() and not c.name.startswith(".")
            for c in grandchildren
        )
        if has_loose_files:
            problems.append({
                "folder": label,
                "reason": "not a skill (no SKILL.md) and has loose files — corrupted",
            })
            continue

        _scan_recursive(child, valid, problems, prefix=f"{label}/")


def _scan(directory: Path) -> tuple[list[dict], list[dict]]:
    valid: list[dict] = []
    problems: list[dict] = []
    _scan_recursive(directory, valid, problems)
    return valid, problems


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
    if not skills:
        return ""

    lines = [
        t(lang, "library.preamble"),
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
# Core reconciliation (shared by setup and `info` health check)
# ---------------------------------------------------------------------------

def _reconcile(
    agent: "BaseAgent",
    paths: list[str],
) -> dict:
    """Scan ``.library/`` + Tier-1 paths, inject catalog, report status.

    The library capability is pure presentation: it reads whatever the Agent
    initializer wrote to ``.library/intrinsic/`` and the agent wrote to
    ``.library/custom/``. It does NOT create directories or copy files.

    Returns a dict suitable for the ``info`` response.
    """
    working_dir = agent._working_dir
    library_dir = working_dir / ".library"
    intrinsic_dir = library_dir / "intrinsic"
    custom_dir = library_dir / "custom"

    problems: list[dict] = []
    status = "ok"
    error: str | None = None

    # Scan intrinsic + custom. If they don't exist, _scan silently returns empty.
    all_skills: list[dict] = []
    int_valid, int_problems = _scan(intrinsic_dir)
    all_skills.extend(int_valid)
    problems.extend(int_problems)

    cus_valid, cus_problems = _scan(custom_dir)
    all_skills.extend(cus_valid)
    problems.extend(cus_problems)

    # Scan each Tier 1 path.
    paths_report: dict[str, dict] = {}
    for raw in paths:
        resolved = _resolve_path(raw, working_dir)
        exists = resolved.is_dir()
        p_valid: list[dict] = []
        p_problems: list[dict] = []
        if exists:
            p_valid, p_problems = _scan(resolved)
            all_skills.extend(p_valid)
            problems.extend(p_problems)
        else:
            log.warning("library: path does not exist: %s (resolved=%s)", raw, resolved)
        paths_report[raw] = {
            "resolved": str(resolved),
            "exists": exists,
            "skills": len(p_valid),
        }

    # Build and inject catalog.
    lang = agent._config.language
    catalog_xml = _build_catalog_xml(all_skills, lang)
    if catalog_xml:
        agent.update_system_prompt("library", catalog_xml, protected=True)
    else:
        agent.update_system_prompt("library", "", protected=True)

    # Health signal: the library capability's own manual must be present.
    library_manual_path = intrinsic_dir / "capabilities" / "library" / "SKILL.md"
    if not library_manual_path.is_file():
        status = "degraded"
        error = error or (
            "library manual missing — initializer may have failed or "
            "capability not installed correctly"
        )
        manual_body = ""
    else:
        manual_body = library_manual_path.read_text(encoding="utf-8")

    result = {
        "status": status,
        "library_manual": manual_body,
        "library_dir": str(library_dir),
        "catalog_size": len(all_skills),
        "paths": paths_report,
        "problems": problems,
    }
    if error:
        result["error"] = error
    return result


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def get_description(lang: str = "en") -> str:
    return t(lang, "library.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["info"],
                "description": t(lang, "library.action_info"),
            },
        },
        "required": ["action"],
    }


def setup(agent: "BaseAgent", paths: list[str] | None = None, **_ignored) -> None:
    """Set up the library capability.

    ``paths`` is the Tier 1 list from ``init.json`` ``manifest.capabilities.library.paths``.
    When omitted (e.g., direct ``Agent(capabilities=["library"])`` use without kwargs),
    no additional paths are scanned — only the per-agent ``.library/``.

    The capability itself does not create or populate ``.library/``; the Agent
    initializer's ``_install_intrinsic_manuals`` step handles that. Setup just
    scans whatever is on disk and injects the XML catalog so the first turn
    sees a ready catalog.
    """
    lang = agent._config.language
    path_list = list(paths) if paths else []

    # Run reconciliation once on setup so the catalog is ready before first turn.
    # This only READS from .library/ — the initializer has already written it.
    _reconcile(agent, path_list)

    # Register the `info` action. `info` re-runs _reconcile to get a fresh snapshot.
    def handle_library(args: dict) -> dict:
        action = args.get("action", "")
        if action == "info":
            return _reconcile(agent, path_list)
        return {
            "status": "error",
            "message": f"unknown action: {action!r}, only 'info' is supported",
        }

    agent.add_tool(
        "library",
        schema=get_schema(lang),
        handler=handle_library,
        description=get_description(lang),
    )
