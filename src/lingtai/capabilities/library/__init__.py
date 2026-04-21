"""Library capability — per-agent skill catalog with kernel-shipped intrinsics.

Every agent has its own ``<agent>/.library/`` containing:

- ``intrinsic/`` — hard-copied from ``lingtai_kernel.intrinsic_skills`` on every
  setup. Includes the ``skill-for-skill`` meta-skill. Always rewritten.
- ``custom/`` — agent-authored skills. Never touched by the capability.

Additional paths come from ``init.json``:

``manifest.capabilities.library.paths``: list[str] — each entry is scanned
recursively and contributes to the ``<available_skills>`` XML injected into the
system prompt's ``library`` section. Paths may be absolute, relative to the
agent working dir, or tilde-prefixed.

Tool surface: a single ``info`` action that returns the ``skill-for-skill``
SKILL.md body plus a runtime health snapshot.

Usage: ``Agent(capabilities={"library": {"paths": [...]}})`` or via init.json.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import t

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
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    return {kv.group(1): kv.group(2).strip() for kv in _KV_RE.finditer(block)}


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
# Intrinsic skills hard-copy
# ---------------------------------------------------------------------------

def _intrinsic_source_dir() -> Path:
    """Locate the kernel's shipped intrinsic_skills directory."""
    import lingtai_kernel
    return Path(lingtai_kernel.__file__).parent / "intrinsic_skills"


def _hard_copy_intrinsics(target_dir: Path) -> None:
    """Copy every intrinsic skill folder from the kernel package into ``target_dir``.

    Existing contents under ``target_dir`` are removed first so a kernel upgrade
    that renames or removes an intrinsic skill propagates cleanly.
    """
    source = _intrinsic_source_dir()
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    if not source.is_dir():
        return  # kernel ships no intrinsic skills (unlikely but handle gracefully)

    for child in sorted(source.iterdir()):
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        if child.is_dir():
            shutil.copytree(child, target_dir / child.name)


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
    """Ensure dirs, hard-copy intrinsics, scan all sources, inject catalog.

    Returns a dict suitable for the ``info`` response.
    """
    working_dir = agent._working_dir
    library_dir = working_dir / ".library"
    intrinsic_dir = library_dir / "intrinsic"
    custom_dir = library_dir / "custom"

    problems: list[dict] = []
    status = "ok"
    error: str | None = None

    # Ensure structure.
    library_dir.mkdir(parents=True, exist_ok=True)
    custom_dir.mkdir(parents=True, exist_ok=True)

    # Hard-copy intrinsics (always overwrite).
    try:
        _hard_copy_intrinsics(intrinsic_dir)
    except OSError as e:
        status = "degraded"
        error = f"intrinsic hard-copy failed: {e}"

    # Scan intrinsic + custom.
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

    # Health signal: skill-for-skill must be present.
    skill_for_skill_path = intrinsic_dir / "skill-for-skill" / "SKILL.md"
    if not skill_for_skill_path.is_file():
        status = "degraded"
        error = error or "skill-for-skill SKILL.md missing — hard-copy may have failed"
        sk_body = ""
    else:
        sk_body = skill_for_skill_path.read_text(encoding="utf-8")

    result = {
        "status": status,
        "skill_for_skill": sk_body,
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
    """
    lang = agent._config.language
    path_list = list(paths) if paths else []

    # Run reconciliation once on setup so the catalog is ready before first turn.
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
