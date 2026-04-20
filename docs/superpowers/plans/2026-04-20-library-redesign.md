# Library Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the `library` capability so every agent has its own per-agent `.library/` (with kernel-shipped intrinsic skills hard-copied in), the scanner reads additional paths from `init.json` `manifest.capabilities.library.paths`, and the tool surface collapses to a single `info` action that serves as both signposting and a semi-health check.

**Architecture:** Per-agent `<agent>/.library/{intrinsic,custom}/` is scanned along with paths listed in `init.json`. Kernel intrinsics (currently just `skill-for-skill`) are hard-copied from the kernel package into `.library/intrinsic/` on every setup. TUI migration renames the old network-shared `.library/` → `.library_shared/` and removes the symlink-population logic. No git operations in the capability; no runtime JSONL state; `init.json` is ground truth.

**Tech Stack:** Python 3.11+ (lingtai-kernel), Go (lingtai TUI/portal), pytest for unit tests.

**Spec:** `docs/superpowers/specs/2026-04-20-library-redesign-design.md`

**Phases:**
- **Phase 1** — Kernel: new intrinsic skill, library capability rewrite, init.json schema, prompt section, i18n, tests.
- **Phase 2** — TUI: migration (rename network `.library/` → `.library_shared/`), init.json seeding of default paths, portal migration stub, utilities directory plumbing.
- **Phase 3** — Integration validation: end-to-end smoke test across a network.

---

## Task 0: Create feature branches in both repos

**Files:** git state only.

- [ ] **Step 1: Create branch in kernel repo**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git status
git checkout -b library-redesign
```

Expected: clean working tree, new branch created.

- [ ] **Step 2: Create branch in TUI repo**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
git status
git checkout -b library-redesign
```

Expected: clean working tree, new branch created.

- [ ] **Step 3: Commit the already-written spec**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add docs/superpowers/specs/2026-04-20-library-redesign-design.md docs/superpowers/plans/2026-04-20-library-redesign.md
git commit -m "$(cat <<'EOF'
docs(library): spec + implementation plan for library redesign

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Phase 1 — Kernel

## Task 1: Ship the intrinsic `skill-for-skill` skill

**Files:**
- Create: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai_kernel/intrinsic_skills/__init__.py`
- Create: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai_kernel/intrinsic_skills/skill-for-skill/SKILL.md`

- [ ] **Step 1: Create the package marker**

Write `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai_kernel/intrinsic_skills/__init__.py` with an empty file (needed so Python treats the directory as a package and so package-data globs pick it up).

- [ ] **Step 2: Write the meta-skill**

Write `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai_kernel/intrinsic_skills/skill-for-skill/SKILL.md`:

```markdown
---
name: skill-for-skill
description: How to use your library — find, read, load, author, and publish skills. Read this first.
version: 1.0.0
---

# Your Library

Every skill listed in `<available_skills>` in your system prompt is reachable right now. Each entry has `name`, `description`, and `location`. The library capability scans the following sources and injects the catalog:

- `<agent>/.library/intrinsic/` — kernel-shipped skills (including this one). Rewritten on every setup; do not edit.
- `<agent>/.library/custom/` — your own authored skills. This is your territory.
- Every path in `init.json` `manifest.capabilities.library.paths` — typically `../.library_shared/` (the network-shared library) and `~/.lingtai-tui/utilities/` (operational utilities shipped by the TUI).

## How the catalog works

The XML catalog in your prompt lists every skill. To read a skill's body, use `read` on the file at `<location>`. That gives you the full Markdown for that one turn.

## Loading a skill into active working memory

If you plan to use a skill across many turns or need it to survive a molt, pin its `SKILL.md` into your pad:

```
psyche({"object": "pad", "action": "append", "files": ["<location>"]})
```

The body is appended to your pad's read-only reference section, which is part of the cached system-prompt prefix. To unpin, call the same action with a new `files` list that omits the path (or `files: []` to clear everything).

Pinning is cheap per-token over a session because the pad sits in the cached prefix — repeated `read`s of the same file do NOT benefit from that cache.

## Authoring a new skill

Create a folder under `<agent>/.library/custom/<skill-name>/` with a `SKILL.md` starting with YAML frontmatter:

```
---
name: <skill-name>
description: One-line description of what this skill does
version: 1.0.0
---

Full instructions in Markdown below...
```

Required frontmatter: `name`, `description`. Optional: `version`, `author`, `tags`.

After writing, call `system({"action": "refresh"})` so the library capability rescans and re-injects the catalog.

## Publishing to the network-shared library

If a custom skill is worth sharing with every agent in the network:

```
bash({"command": "cp -r .library/custom/<name> ../.library_shared/<name>"})
```

Then `system.refresh`. Do **not** overwrite an existing entry in `.library_shared/` — if the name collides, rename your skill or consult the admin agent.

## Admin curation of `.library_shared/`

If you are the admin agent, you may edit, consolidate, rename, or remove entries in `.library_shared/` using `edit`/`write`/`rm` as needed.

If you are not the admin agent, **do not modify** `.library_shared/` beyond adding new entries with `cp`. Editing or removing existing entries is admin's stewardship. This is a norm, not a mechanical lock.

## Adding a new library path

To expand your library with another source directory:

1. `edit` `init.json` under `manifest.capabilities.library.paths`. Append your new path (absolute or relative to your working dir; `~/` expansion honored).
2. `system.refresh`.

`init.json` is the ground truth. There is no runtime state — whatever is in `paths` at setup time is the exact set scanned.

## Name collision discipline

Two skills with the same `name` in the catalog would collide. Before authoring a new skill in `custom/` or publishing to shared, grep the existing catalog:

```
bash({"command": "grep -rh '^name:' .library/ ../.library_shared/ ~/.lingtai-tui/utilities/"})
```

If you hit a collision: rename, or adapt the existing skill instead of forking a second one.

## Health check

Call `library({"action": "info"})` to verify your library is wired correctly. It returns this SKILL.md body plus a runtime snapshot: `catalog_size`, resolved paths with exist/skill-count info, and any `problems` (invalid frontmatter, unreadable folders). If `status` is `"degraded"`, the error message tells you what needs fixing.
```

- [ ] **Step 3: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai_kernel/intrinsic_skills/
git commit -m "$(cat <<'EOF'
feat(kernel): ship skill-for-skill as first intrinsic skill

First entry in the new intrinsic_skills package-data directory.
Teaches agents the full library workflow.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Include intrinsic_skills in package data

**Files:**
- Modify: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/pyproject.toml` (line 35-37 area — `[tool.setuptools.package-data]`)

- [ ] **Step 1: Update package-data glob**

Edit `pyproject.toml`:

```toml
[tool.setuptools.package-data]
lingtai_kernel = ["i18n/*.json", "intrinsic_skills/**/*"]
lingtai = ["i18n/*.json", "capabilities/*.json", "addons/*/*.json"]
```

(Add `"intrinsic_skills/**/*"` to the `lingtai_kernel` tuple; leave the `lingtai` line alone.)

- [ ] **Step 2: Verify the installed package picks them up**

Run:
```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
pip install -e . --quiet
python -c "from pathlib import Path; import lingtai_kernel; p = Path(lingtai_kernel.__file__).parent / 'intrinsic_skills' / 'skill-for-skill' / 'SKILL.md'; print('exists:', p.is_file()); assert p.is_file()"
```

Expected: `exists: True`.

- [ ] **Step 3: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add pyproject.toml
git commit -m "$(cat <<'EOF'
build(kernel): include intrinsic_skills in package-data

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Write failing tests for the new library capability

**Files:**
- Create: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/tests/test_library.py`

- [ ] **Step 1: Write the full test file**

Write `/Users/huangzesen/Documents/GitHub/lingtai-kernel/tests/test_library.py`:

```python
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
        assert (workdir / ".library" / "custom").is_dir()
    finally:
        agent.stop(timeout=1.0)


def test_library_setup_hard_copies_intrinsics(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        skill_md = workdir / ".library" / "intrinsic" / "skill-for-skill" / "SKILL.md"
        assert skill_md.is_file()
        assert "name: skill-for-skill" in skill_md.read_text()
    finally:
        agent.stop(timeout=1.0)


def test_library_setup_overwrites_stale_intrinsic(tmp_path):
    # Simulate a stale intrinsic skill from a previous kernel version.
    workdir = tmp_path / "agent"
    stale = workdir / ".library" / "intrinsic" / "skill-for-skill" / "SKILL.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("---\nname: skill-for-skill\ndescription: STALE\n---\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {}},
    )
    try:
        body = stale.read_text()
        assert "STALE" not in body
        assert "How to use your library" in body or "Your Library" in body
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
        names = [
            line for line in result["skill_for_skill"].splitlines()
            if False  # just exercise return shape below
        ]
        assert result["status"] == "ok"
        assert result["paths"][str(extra)]["skills"] == 1
        assert result["catalog_size"] >= 2  # skill-for-skill + shared-skill
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


def test_info_returns_skill_for_skill_body(tmp_path):
    agent, _ = _mk_agent(tmp_path)
    try:
        result = agent._tool_handlers["library"]({"action": "info"})
        assert "skill_for_skill" in result
        assert "name: skill-for-skill" in result["skill_for_skill"]
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
    agent, workdir = _mk_agent(tmp_path)
    try:
        # Simulate the intrinsic getting deleted *after* setup.
        skill = workdir / ".library" / "intrinsic" / "skill-for-skill" / "SKILL.md"
        skill.unlink()
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
        assert "skill-for-skill" in prompt
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
```

- [ ] **Step 2: Run tests and confirm they fail**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
pytest tests/test_library.py -v 2>&1 | head -60
```

Expected: All tests **FAIL** (either `KeyError`, `AssertionError`, or import errors because the new library capability hasn't been written yet). This is the TDD "red" state.

- [ ] **Step 3: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add tests/test_library.py
git commit -m "$(cat <<'EOF'
test(library): add failing tests for redesigned capability

Covers: per-agent directories, intrinsic hard-copy, path resolution
(absolute/relative/tilde), info action (ok/degraded), catalog injection,
no-git guarantee.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Rewrite `library.py` capability

**Files:**
- Modify: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai/capabilities/library.py` (full rewrite)

- [ ] **Step 1: Overwrite with the new implementation**

Write `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai/capabilities/library.py`:

```python
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
# Skill scanner (adapted from previous implementation)
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
```

- [ ] **Step 2: Smoke-test the module**

Run:
```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
python -c "from lingtai.capabilities import library; print(dir(library))"
```

Expected: no import errors; the `setup`, `get_description`, `get_schema` symbols are listed.

- [ ] **Step 3: Run the test suite**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
pytest tests/test_library.py -v 2>&1 | tail -40
```

Expected: some tests still fail because the i18n keys don't exist yet (`library.preamble`, `library.action_info`, `library.description`). Most structural tests (directory creation, intrinsic hard-copy, path resolution, info return shape) should pass or fail cleanly on the missing i18n key.

- [ ] **Step 4: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai/capabilities/library.py
git commit -m "$(cat <<'EOF'
feat(library): rewrite capability for per-agent library redesign

- Per-agent .library/{intrinsic,custom}/ structure.
- Hard-copy intrinsic skills from kernel package on every setup.
- Read Tier 1 paths from init.json manifest.capabilities.library.paths.
- Single tool action: info (returns skill-for-skill body + health snapshot).
- No git operations, no runtime state files.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Update i18n keys for the new capability

**Files:**
- Modify: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai/i18n/en.json`
- Modify: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai/i18n/zh.json`
- Modify: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai/i18n/wen.json`

Per user preference (memory `feedback_i18n_three_locales.md`): always update all three locale files when adding/changing library keys.

- [ ] **Step 1: Update `en.json`**

Find the two existing library keys in `src/lingtai/i18n/en.json` and replace them:

Old:
```json
  "library.action": "register: validate all skill folders, git add + commit changes, refresh catalog. Use after downloading or modifying skills via bash.\nrefresh: rescan the skill store and re-inject the catalog into your system prompt. Use to pick up skills registered by other agents.",
  "library.preamble": "The following skills provide specialized instructions for specific tasks. When a task matches a skill's description, use the read tool to load the SKILL.md file at the path shown in that skill's location field, then follow its instructions. Each skill folder may contain supporting files (scripts, templates, data) — the SKILL.md will reference them. Only load one skill at a time. If no skill clearly applies, do not load any."
```

New:
```json
  "library.description": "Your per-agent skill library. The <available_skills> catalog in your system prompt lists every skill reachable right now. Call info to read the full library workflow (authoring, publishing, loading via pad.append) and verify your library is healthy. See also the skill-for-skill skill in .library/intrinsic/.",
  "library.action_info": "info: return the skill-for-skill meta-skill body plus a runtime health snapshot (catalog size, resolved paths, problems).",
  "library.preamble": "The following skills provide specialized instructions for specific tasks. When a task matches a skill's description, use the read tool to load the SKILL.md file at the path shown in that skill's location field, then follow its instructions. Each skill folder may contain supporting files (scripts, templates, data) — the SKILL.md will reference them. Skills you use often can be pinned into your pad via psyche({object:'pad', action:'append', files:[location]}) for cross-turn persistence. If no skill clearly applies, do not load any."
```

Also confirm there is an existing `"library.description"` entry — if so, replace it with the new one above. If `library.description` did NOT exist before (check with grep), just add it.

- [ ] **Step 2: Update `zh.json`**

Same three keys in Chinese:

```json
  "library.description": "你的器灵专属技能库。系统提示词中的 <available_skills> 目录列出了当前所有可用的技能。调用 info 可阅读完整的技能库工作流（编写、发布、通过 pad.append 加载），并验证技能库的健康状态。也可直接查阅 .library/intrinsic/ 中的 skill-for-skill 技能。",
  "library.action_info": "info：返回 skill-for-skill 元技能正文，以及运行时健康快照（目录规模、解析后的路径、问题列表）。",
  "library.preamble": "以下技能为特定任务提供专门指导。当任务匹配某个技能的描述时，使用 read 工具读取 location 字段所示路径下的 SKILL.md 文件，然后遵循其中指示。每个技能文件夹可能包含支持文件（脚本、模板、数据）——SKILL.md 会引用它们。经常使用的技能可通过 psyche({object:'pad', action:'append', files:[location]}) 固定到 pad 中，使其跨轮次持久存在。若没有明显匹配的技能，则不加载任何技能。"
```

- [ ] **Step 3: Update `wen.json`**

Same three keys in classical Chinese (文言):

```json
  "library.description": "尔之器灵藏典。系统之中 <available_skills> 所列，皆此时可取之技。呼 info 以得 skill-for-skill 全文（藏用、出新、以 pad.append 载入），兼验藏典康否。详参 .library/intrinsic/ 中 skill-for-skill 一技。",
  "library.action_info": "info：还 skill-for-skill 元技之文，并录当时之康状（技数、解径、患记）。",
  "library.preamble": "下列诸技，各司其事。凡所遇事与技之描合者，以 read 取其 location 所指之 SKILL.md，循而行之。每技之匣，或含副件（script、template、data），SKILL.md 自有所引。常用之技，可以 psyche({object:'pad', action:'append', files:[location]}) 固于 pad，跨轮常存。若无可取者，勿妄载。"
```

- [ ] **Step 4: Run tests again**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
pytest tests/test_library.py -v 2>&1 | tail -30
```

Expected: all tests PASS. If any still fail, fix the capability code or test expectations until green.

- [ ] **Step 5: Smoke-test the module import**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
python -c "from lingtai.capabilities import library; print('ok')"
```

Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai/i18n/
git commit -m "$(cat <<'EOF'
i18n(library): add description + action_info keys; update preamble

Replaces obsolete library.action (register/refresh). Preamble updated to
mention pad.append as the pinning mechanism. All three locales (en/zh/wen)
updated in lockstep.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Add `library` subfield validation to init_schema

**Files:**
- Modify: `/Users/huangzesen/Documents/GitHub/lingtai-kernel/src/lingtai/init_schema.py`

- [ ] **Step 1: Find where capabilities are validated**

Read `src/lingtai/init_schema.py` to locate where `manifest.capabilities` is handled. Based on prior reading, the current code accepts `capabilities` as `dict` without validating individual capability shapes.

- [ ] **Step 2: Add the library subfield check**

Open `src/lingtai/init_schema.py` and, after the existing `_optional_keys(manifest, ...)` block and before any closing return, add:

```python
    # Validate manifest.capabilities.library shape if present.
    caps = manifest.get("capabilities") or {}
    library_cfg = caps.get("library") if isinstance(caps, dict) else None
    if library_cfg is not None:
        if not isinstance(library_cfg, dict):
            raise ValueError(
                f"manifest.capabilities.library: expected object, got {type(library_cfg).__name__}"
            )
        paths = library_cfg.get("paths")
        if paths is not None:
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ValueError(
                    "manifest.capabilities.library.paths: expected list[str]"
                )
        for key in library_cfg:
            if key != "paths":
                warnings.append(f"unknown field in manifest.capabilities.library: {key}")
```

Place it before the `return warnings` statement. If `validate_init` doesn't end with `return warnings`, add it at the appropriate point before the function's return.

- [ ] **Step 3: Smoke-test validation**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
python -c "
from lingtai.init_schema import validate_init
# Valid
data = {
    'manifest': {'llm': {}, 'capabilities': {'library': {'paths': ['../.library_shared']}}},
    'principle': 'p', 'covenant': 'c', 'pad': 'p', 'prompt': 'x', 'soul': 's'
}
warnings = validate_init(data)
print('valid ok; warnings:', warnings)
# Invalid
try:
    data2 = dict(data)
    data2['manifest'] = dict(data['manifest'])
    data2['manifest']['capabilities'] = {'library': {'paths': 'not-a-list'}}
    validate_init(data2)
    print('ERROR: should have raised')
except ValueError as e:
    print('correctly raised:', e)
"
```

Expected output includes `valid ok; warnings: []` and `correctly raised: manifest.capabilities.library.paths: expected list[str]`.

- [ ] **Step 4: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai/init_schema.py
git commit -m "$(cat <<'EOF'
feat(init): validate manifest.capabilities.library schema

library must be an object; library.paths must be list[str] if present.
Unknown keys under library emit a warning.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: End-to-end kernel sanity check

**Files:** none; just verification.

- [ ] **Step 1: Run the full kernel test suite**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
pytest -x 2>&1 | tail -30
```

Expected: all tests PASS. If a pre-existing test breaks because of the capability rewrite (for example, something that referenced the old `register`/`refresh` actions), investigate and fix — this is expected since the old actions were intentionally removed.

- [ ] **Step 2: Smoke test booting an agent with library**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
python -c "
from pathlib import Path
import tempfile, shutil
from unittest.mock import MagicMock
from lingtai.agent import Agent

svc = MagicMock()
svc.get_adapter.return_value = MagicMock()
svc.provider = 'gemini'
svc.model = 'test'

with tempfile.TemporaryDirectory() as t:
    workdir = Path(t) / 'agent'
    shared = Path(t) / '.library_shared'
    shared.mkdir()
    (shared / 'shared-one' / 'SKILL.md').parent.mkdir(parents=True)
    (shared / 'shared-one' / 'SKILL.md').write_text('---\nname: shared-one\ndescription: shared test skill\n---\nbody')
    agent = Agent(
        service=svc,
        agent_name='test',
        working_dir=workdir,
        capabilities={'library': {'paths': ['../.library_shared']}},
    )
    try:
        result = agent._tool_handlers['library']({'action': 'info'})
        print('status:', result['status'])
        print('catalog_size:', result['catalog_size'])
        print('paths:', result['paths'])
        assert result['status'] == 'ok'
        assert result['catalog_size'] >= 2  # skill-for-skill + shared-one
        print('smoke test OK')
    finally:
        agent.stop(timeout=1.0)
"
```

Expected: `status: ok`, `catalog_size: 2` (or more), `paths` dict shows `../.library_shared` with `exists: True`, `skills: 1`. Ends with `smoke test OK`.

- [ ] **Step 3: No commit** (verification only).

---

# Phase 2 — TUI (separate repo: `lingtai`)

## Task 8: TUI migration — rename `.library/` → `.library_shared/` at network level

**Files:**
- Read first: `/Users/huangzesen/Documents/GitHub/lingtai/tui/internal/migrate/migrate.go`
- Create: `/Users/huangzesen/Documents/GitHub/lingtai/tui/internal/migrate/m<NNN>_library_split.go` (where NNN is current CurrentVersion + 1, padded to 3 digits).

- [ ] **Step 1: Read the current migration state**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
cat tui/internal/migrate/migrate.go | head -60
ls tui/internal/migrate/
```

Note the current `CurrentVersion` value and the highest `mNNN_...go` number. The new migration will be `m<NNN+1>_library_split.go`.

- [ ] **Step 2: Write the migration**

Create `tui/internal/migrate/m<NNN>_library_split.go` with content (substitute `NNN` with the actual number, substitute `Nnn` in function name to match):

```go
package migrate

import (
	"fmt"
	"os"
	"path/filepath"
)

// migrateLibrarySplit renames the pre-existing network-level .library/ (which
// previously mixed agent-shared skills with TUI-managed symlinks) to
// .library_shared/ and strips leftover symlinks. The new per-agent library
// capability maintains <agent>/.library/ independently from the network-shared
// directory.
//
// Invariant: destructive only on the old .library/ directory. Symlinks inside
// that directory are removed (they were TUI-managed and are no longer needed);
// real skill folders are preserved via the rename.
func migrateLibrarySplit(lingtaiDir string) error {
	networkRoot := filepath.Dir(lingtaiDir)
	oldPath := filepath.Join(networkRoot, ".library")
	newPath := filepath.Join(networkRoot, ".library_shared")

	oldInfo, err := os.Lstat(oldPath)
	if os.IsNotExist(err) {
		// Fresh network — just ensure the new path exists.
		return os.MkdirAll(newPath, 0o755)
	}
	if err != nil {
		return fmt.Errorf("stat %s: %w", oldPath, err)
	}
	if !oldInfo.IsDir() {
		fmt.Printf("warning: %s exists but is not a directory; skipping\n", oldPath)
		return nil
	}

	// If .library_shared already exists, don't clobber it — warn and proceed
	// to strip symlinks only.
	if _, err := os.Stat(newPath); err == nil {
		fmt.Printf("warning: %s already exists; stripping symlinks from old .library but not renaming\n", oldPath)
		return stripSymlinks(oldPath)
	}

	if err := stripSymlinks(oldPath); err != nil {
		return fmt.Errorf("strip symlinks: %w", err)
	}

	if err := os.Rename(oldPath, newPath); err != nil {
		return fmt.Errorf("rename %s → %s: %w", oldPath, newPath, err)
	}

	fmt.Printf("migrated: %s → %s\n", oldPath, newPath)
	return nil
}

// stripSymlinks removes every symlink under dir recursively, leaving real
// files and directories intact.
func stripSymlinks(dir string) error {
	return filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return nil // best-effort; skip errors
		}
		if info.Mode()&os.ModeSymlink != 0 {
			os.Remove(p)
		}
		return nil
	})
}
```

- [ ] **Step 3: Register the migration**

Edit `tui/internal/migrate/migrate.go`:

1. Bump `CurrentVersion` to `NNN`.
2. Append to the `migrations` slice: `migrateLibrarySplit`.

The exact change depends on the file's current shape. Open it, find the `migrations` slice literal, and add the new entry in order:

```go
var migrations = []func(string) error{
	// ... existing entries ...
	migrateLibrarySplit,
}
```

And at the top:
```go
const CurrentVersion = NNN  // bumped from NNN-1
```

- [ ] **Step 4: Bump portal version too**

Per `tui/CLAUDE.md`: the TUI and portal share meta.json version space with separate registries. Edit `portal/internal/migrate/migrate.go` to bump its `CurrentVersion` to the same `NNN`, and register a no-op stub for `migrateLibrarySplit`:

```go
func migrateLibrarySplit(lingtaiDir string) error {
	// TUI-owned migration; portal doesn't need to do anything here
	// (the TUI will have run the actual rename before the portal opens).
	return nil
}

var migrations = []func(string) error{
	// ... existing entries ...
	migrateLibrarySplit,
}
```

And:
```go
const CurrentVersion = NNN
```

- [ ] **Step 5: Build both binaries**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai/tui && make build 2>&1 | tail -10
cd /Users/huangzesen/Documents/GitHub/lingtai/portal && make build 2>&1 | tail -10
```

Expected: both builds succeed. Binaries at `tui/bin/lingtai-tui` and `portal/bin/lingtai-portal`.

- [ ] **Step 6: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
git add tui/internal/migrate/ portal/internal/migrate/
git commit -m "$(cat <<'EOF'
feat(migrate): rename network .library/ → .library_shared/

New migration renames the pre-existing network-level shared library
directory and strips TUI-managed symlinks. Per-agent .library/ is now
owned by the kernel library capability.

Both TUI and portal migration versions bumped; portal registers a no-op
stub for this TUI-owned migration.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Remove `recipe_library.go` symlink-population logic

**Files:**
- Modify (or delete): `/Users/huangzesen/Documents/GitHub/lingtai/tui/internal/preset/recipe_library.go`
- Find callers via: `grep -rn "LinkRecipeLibrary\|PopulateBundledLibrary\|PruneStaleLibrarySymlinks" /Users/huangzesen/Documents/GitHub/lingtai/tui`

- [ ] **Step 1: Find all callers**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
grep -rn "LinkRecipeLibrary\|PopulateBundledLibrary\|PruneStaleLibrarySymlinks" tui/
```

Note each caller — they need to be removed or stubbed.

- [ ] **Step 2: Remove or neutralize each caller**

For every caller, remove the call site (the function no longer has a job — the kernel library capability handles `.library/` setup per-agent, and `.library_shared/` is a plain directory that the kernel scans). If removing the call leaves an unused import or parameter, clean it up.

- [ ] **Step 3: Delete or empty `recipe_library.go`**

If nothing in the file is referenced after step 2, delete it:

```bash
rm /Users/huangzesen/Documents/GitHub/lingtai/tui/internal/preset/recipe_library.go
```

Also delete its test file:
```bash
rm /Users/huangzesen/Documents/GitHub/lingtai/tui/internal/preset/recipe_library_test.go
```

If a helper (e.g., `isHidden`) was defined in that file and is still used elsewhere, move it to a new small helper file or inline it at the one remaining caller.

- [ ] **Step 4: Build the TUI**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai/tui && make build 2>&1 | tail -20
```

Expected: clean build. If the build fails due to leftover references, fix them.

- [ ] **Step 5: Run TUI tests**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai/tui && go test ./... 2>&1 | tail -20
```

Expected: PASS. Broken tests should be updated or deleted alongside the removed code.

- [ ] **Step 6: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
git add -A tui/internal/preset/
git commit -m "$(cat <<'EOF'
refactor(tui): remove symlink-population of .library/

The kernel library capability now owns per-agent <agent>/.library/.
The network-shared library lives at ../.library_shared/ as plain
directories, scanned directly by the kernel. No TUI-managed symlinks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Seed the library defaults into generated init.json

**Files:**
- Find and modify the init.json generator in the TUI. Expected location: `tui/internal/preset/` or wherever the TUI composes init.json for new agents.

- [ ] **Step 1: Locate the init.json generator**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
grep -rn '"manifest"\|"capabilities"' tui/internal/ | head -20
```

Identify the file(s) that write init.json for new agents.

- [ ] **Step 2: Determine the TUI utilities install path**

The default path `~/.lingtai-tui/utilities/` needs to be an absolute path in init.json. Choose the install strategy:

- Homebrew: something like `$(brew --prefix)/share/lingtai-tui/utilities`
- Direct binary: fallback to `~/.lingtai-tui/utilities/`

Add a helper function (or extend an existing one) that resolves the utilities path at init-generation time. For this plan, assume `~/.lingtai-tui/utilities` as the default (deferred decision in spec §11).

- [ ] **Step 3: Inject the library section into generated init.json**

In the init-generator code, add the library section to the manifest's capabilities:

```go
// Inside the init.json manifest generation
manifest["capabilities"] = map[string]interface{}{
    // ... existing capabilities ...
    "library": map[string]interface{}{
        "paths": []string{
            "../.library_shared",
            "~/.lingtai-tui/utilities",
        },
    },
}
```

Adapt to whatever shape the existing code uses (struct vs map, templating, etc.). The key invariant: **every freshly-generated init.json contains `manifest.capabilities.library.paths` with the two defaults**.

- [ ] **Step 4: Build and test**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai/tui && make build 2>&1 | tail -10
cd /Users/huangzesen/Documents/GitHub/lingtai/tui && go test ./... 2>&1 | tail -10
```

Expected: both green.

- [ ] **Step 5: Manual smoke test**

Use the built TUI to create a fresh network + agent, then inspect the generated init.json:

```bash
# Exact invocation depends on TUI CLI; for example:
# cd /tmp && mkdir fresh-test && cd fresh-test
# /Users/huangzesen/Documents/GitHub/lingtai/tui/bin/lingtai-tui init   # or similar
# cat <created-agent>/init.json | grep -A 10 '"library"'
```

Confirm the generated init.json contains:
```json
"library": {
  "paths": ["../.library_shared", "~/.lingtai-tui/utilities"]
}
```

- [ ] **Step 6: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
git add -A tui/
git commit -m "$(cat <<'EOF'
feat(tui): seed library.paths into generated init.json

Every newly-generated agent now ships with the two default library
paths: ../.library_shared (network shared) and ~/.lingtai-tui/utilities
(TUI utilities). Agents can edit init.json to add more.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Move TUI preset skills into the utilities directory

**Files:**
- Source: `/Users/huangzesen/Documents/GitHub/lingtai/tui/internal/preset/skills/`
- Target: install-time copy into `~/.lingtai-tui/utilities/`

- [ ] **Step 1: Audit which preset skills become utilities**

Per spec §11 (deferred decision), the full triage is a TUI-PR-scope call. For this task, adopt the simple rule: **all current entries under `tui/internal/preset/skills/` become TUI utilities**, installed into `~/.lingtai-tui/utilities/`. Recipe-specific skills stay bundled with recipes and can be added to `library.paths` by those recipes in a follow-up.

- [ ] **Step 2: Implement install-time population of `~/.lingtai-tui/utilities/`**

On TUI startup (or in a dedicated `install-utilities` step), the TUI needs to ensure `~/.lingtai-tui/utilities/` exists and contains the current set of skills. Two options:

**(a)** Embed the skills into the TUI binary (via `embed.FS`) and extract them on startup.
**(b)** Ship a separate directory and use a sync step.

Simplest: **(a)** with `embed.FS`. Create `tui/internal/preset/embed_utilities.go`:

```go
package preset

import (
	"embed"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
)

//go:embed skills
var utilitiesFS embed.FS

// PopulateUtilitiesDir ensures ~/.lingtai-tui/utilities/ exists and mirrors
// the embedded skills directory. Overwrites existing files so TUI upgrades
// propagate.
func PopulateUtilitiesDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	target := filepath.Join(home, ".lingtai-tui", "utilities")
	_ = runtime.GOOS // silence unused import if cross-compile diverges later

	// Wipe and repopulate.
	os.RemoveAll(target)
	if err := os.MkdirAll(target, 0o755); err != nil {
		return "", err
	}

	return target, fs.WalkDir(utilitiesFS, "skills", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel := p
		if rel == "skills" {
			return nil
		}
		// Strip leading "skills/" from the embed path to get the utility layout.
		rel = rel[len("skills/"):]
		dest := filepath.Join(target, rel)
		if d.IsDir() {
			return os.MkdirAll(dest, 0o755)
		}
		data, err := utilitiesFS.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(dest, data, 0o644)
	})
}
```

- [ ] **Step 3: Call the population from TUI startup**

Find the TUI's startup sequence (`tui/cmd/` or wherever `main()` lives). Add an early call:

```go
if _, err := preset.PopulateUtilitiesDir(); err != nil {
	fmt.Fprintf(os.Stderr, "warning: failed to populate utilities: %v\n", err)
}
```

- [ ] **Step 4: Build and test**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai/tui && make build 2>&1 | tail -10
/Users/huangzesen/Documents/GitHub/lingtai/tui/bin/lingtai-tui --help  # or a non-destructive invocation that runs PopulateUtilitiesDir
ls ~/.lingtai-tui/utilities/ | head
```

Expected: `~/.lingtai-tui/utilities/` now contains the skill subdirectories (`lingtai-recipe`, `lingtai-mcp`, etc.).

- [ ] **Step 5: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
git add -A
git commit -m "$(cat <<'EOF'
feat(tui): embed preset skills as utilities; populate ~/.lingtai-tui/utilities/

TUI now extracts its bundled skills to a stable per-user location on
startup. Agents pick them up via the default library.paths entry in
init.json.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Phase 3 — Integration Validation

## Task 12: End-to-end smoke test with a real network

**Files:** none; manual validation.

- [ ] **Step 1: Build both binaries fresh**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai/tui && make build 2>&1 | tail -5
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && pip install -e . --quiet
```

Expected: clean build; clean pip install.

- [ ] **Step 2: Create a fresh network and agent**

In a clean scratch directory:

```bash
mkdir -p /tmp/lingtai-library-test && cd /tmp/lingtai-library-test
/Users/huangzesen/Documents/GitHub/lingtai/tui/bin/lingtai-tui init  # adapt to actual CLI
```

Follow the TUI prompts to create one agent. Look at its generated init.json:

```bash
cat <agent-dir>/init.json | python -m json.tool | grep -A 4 '"library"'
```

Expected: the library section with the two default paths.

- [ ] **Step 3: Verify `.library_shared/` exists at network level**

```bash
ls -la /tmp/lingtai-library-test/.library_shared/
```

Expected: directory exists (possibly empty).

- [ ] **Step 4: Boot the agent and check its prompt**

Start the agent via the TUI. Once running, verify its system prompt includes the `<available_skills>` catalog with at minimum `skill-for-skill` listed. Exact verification depends on TUI UX — either the TUI shows the prompt panel, or check the agent's log file.

- [ ] **Step 5: Test the `info` action from a live prompt**

Send the agent a message asking it to call `library({"action": "info"})` and report back. Expected response includes:
- `status: "ok"`
- `skill_for_skill: "<meta-skill body>"`
- `catalog_size: >= 1`
- `paths: {...}` with both defaults, `~/.lingtai-tui/utilities` showing skills >= 1

- [ ] **Step 6: Test authoring + refresh cycle**

Have the agent write a new skill into `<agent>/.library/custom/test-skill/SKILL.md` and then call `system({"action": "refresh"})`. After refresh, call `library.info` again — `test-skill` should appear in `catalog_size`.

- [ ] **Step 7: Test publishing to shared**

Have the agent run `bash({"command": "cp -r .library/custom/test-skill ../.library_shared/test-skill"})`, then `system.refresh`. Verify: the catalog now shows `test-skill` with a `location` under `.library_shared/`, not `custom/` (since scanner orders by... — but you may see both; that's a legitimate "name collision" surfaced as a problem, which matches the spec's name-collision discipline).

- [ ] **Step 8: Test migration on a pre-existing network**

If you have (or can simulate) a network with a pre-existing `<network>/.library/` directory containing symlinks plus real folders, run the TUI against it. Expected:
- `.library/` is renamed to `.library_shared/`.
- Symlinks inside are stripped.
- Real folders survive the rename.
- Meta.json version has been bumped.

Simulate with:
```bash
mkdir -p /tmp/lingtai-migrate-test/.lingtai
mkdir -p /tmp/lingtai-migrate-test/.library/some-skill
echo '---\nname: some-skill\ndescription: testing\n---\nbody' > /tmp/lingtai-migrate-test/.library/some-skill/SKILL.md
ln -s /tmp/lingtai-migrate-test/.library/some-skill /tmp/lingtai-migrate-test/.library/linked
echo '{"version": 0}' > /tmp/lingtai-migrate-test/.lingtai/meta.json
cd /tmp/lingtai-migrate-test
/Users/huangzesen/Documents/GitHub/lingtai/tui/bin/lingtai-tui  # triggers migration
ls /tmp/lingtai-migrate-test/.library_shared/
```

Expected: `some-skill/` exists under `.library_shared/`; no `linked` symlink remains.

- [ ] **Step 9: Clean up**

```bash
rm -rf /tmp/lingtai-library-test /tmp/lingtai-migrate-test
```

- [ ] **Step 10: No commit** (validation only).

---

## Task 13: PR preparation

**Files:** commit history only.

- [ ] **Step 1: Review kernel branch history**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git log main..library-redesign --oneline
```

Expected: ~7 commits corresponding to Tasks 1–7.

- [ ] **Step 2: Review TUI branch history**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai
git log main..library-redesign --oneline
```

Expected: ~4 commits corresponding to Tasks 8–11.

- [ ] **Step 3: Decide on PR strategy**

Both repos have coupled changes. Options:
- **(a)** Open both PRs simultaneously, reference each other in the descriptions. Merge both together.
- **(b)** Merge kernel first (its changes are additive and backwards-compatible for any consumer not using the old `register`/`refresh` actions — except lingtai TUI, which is about to change anyway). Then merge TUI.

Coordinate with user.

- [ ] **Step 4: Stop here.** The user will open PRs when ready.

---

## Self-Review Summary

- **Spec coverage** — every section has a task:
  - §2 Architecture → Tasks 1, 4 (directory layout, intrinsic ship, scan).
  - §3 Capability behavior → Tasks 4, 5 (tool surface, silent setup).
  - §4 Prompt sections → Task 4 (XML catalog injection).
  - §5 Kernel package additions → Tasks 1, 2 (intrinsic_skills + package-data).
  - §6 Meta-skill teaching → Task 1 (SKILL.md body).
  - §7 init.json schema → Task 6.
  - §8 TUI integration → Tasks 8, 10, 11 (migration, init.json seed, utilities dir).
  - §9 Files to modify → all tasks.
  - §12 Migration/rollout → Tasks 8, 12.
  - §13 Validation → Task 12.

- **No Placeholders** — every step has concrete code, exact commands, expected output. The only TBDs are surfaced as deferred decisions in the spec (utility install path in Task 11 step 2; skill-triage in Task 11 step 1) with a clear provisional rule.

- **Type consistency** — `info` return shape is consistent across Task 3 (test assertions), Task 4 (implementation), Task 7 (smoke test): `status`, `skill_for_skill`, `library_dir`, `catalog_size`, `paths`, `problems`, optional `error`.
