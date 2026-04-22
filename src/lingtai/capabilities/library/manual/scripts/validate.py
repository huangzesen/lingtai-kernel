#!/usr/bin/env python3
"""
Skill structure validation script — validates SKILL.md format, directory structure, scripts.
English error messages (usable by non-Chinese agents).
"""

import os
import sys
import re
import argparse
from pathlib import Path


PLACEHOLDER_RE = re.compile(r'\[[A-Z][A-Z_]*\]')


def validate_frontmatter(skill_path: Path) -> tuple[bool, list[str]]:
    """Check frontmatter completeness and quality."""
    errors = []
    warnings = []
    skill_md = skill_path / "SKILL.md"

    if not skill_md.exists():
        return False, ["SKILL.md does not exist"]

    content = skill_md.read_text(encoding="utf-8")

    # Required: name field
    name_match = re.search(r'^name:\s*(\S+)', content, re.MULTILINE)
    if not name_match:
        errors.append("Missing 'name' field in frontmatter")
    else:
        # Check for unfilled placeholder
        name_val = name_match.group(1).strip().strip('"\'')
        if PLACEHOLDER_RE.match(name_val):
            errors.append(f"Unfilled placeholder: name is still '{name_val}'")

    # Required: description field
    desc_match = re.search(r'^description:\s*(.+)', content, re.MULTILINE)
    if not desc_match:
        errors.append("Missing 'description' field in frontmatter")
    else:
        desc_text = desc_match.group(1).strip().strip('"\'')
        # Check for unfilled placeholder
        if PLACEHOLDER_RE.search(desc_text):
            errors.append(f"Unfilled placeholder in description: {desc_text}")

    # Required: YAML opening delimiter
    if not content.strip().startswith('---'):
        errors.append("Frontmatter must start with '---'")

    # Quality: description length (warn if > 500 chars — catalog is cluttered)
    if desc_match:
        desc_text = desc_match.group(1).strip().strip('"\'')
        if len(desc_text) > 500:
            warnings.append(f"WARNING: description is {len(desc_text)} chars (> 500). Catalog display will be cluttered.")

    # Quality: description is too short (should explain what it does)
    if desc_match:
        desc_text = desc_match.group(1).strip().strip('"\'')
        word_count = len(desc_text.split())
        if word_count < 3:
            warnings.append(f"WARNING: description is very short ({word_count} words). Should explain what this skill does.")

    return len(errors) == 0, errors + warnings


def validate_skill_md_length(skill_path: Path) -> tuple[bool, list[str]]:
    """Warn if SKILL.md is too long — content should be externalized."""
    warnings = []
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return True, []

    lines = skill_md.read_text(encoding="utf-8").splitlines()
    # Filter out empty lines and frontmatter
    code_lines = [l for l in lines if l.strip() and not l.strip().startswith('#')]

    if len(code_lines) > 500:
        warnings.append(f"WARNING: SKILL.md has {len(code_lines)} non-comment lines (> 500). Consider moving content to references/.")
    elif len(lines) > 300:
        warnings.append(f"NOTE: SKILL.md is {len(lines)} lines. Aim for < 300 lines; move detail to references/.")

    return True, warnings  # warnings don't fail validation


def validate_directory_structure(skill_path: Path) -> tuple[bool, list[str]]:
    """Check directory structure."""
    errors = []

    if not (skill_path / "SKILL.md").exists():
        errors.append("Missing SKILL.md")

    # Check referenced files exist in SKILL.md body
    # Capture full relative paths (e.g., "references/examples.md" not just "examples.md")
    content = (skill_path / "SKILL.md").read_text(encoding="utf-8") if (skill_path / "SKILL.md").exists() else ""
    ref_matches = re.findall(r'(scripts/[\w./_-]+|assets/[\w./_-]+|references/[\w./_-]+)', content)
    for ref in ref_matches:
        clean = ref.rstrip('/')
        if clean and not (skill_path / clean).exists():
            errors.append(f"Broken reference: {clean} (mentioned in SKILL.md but does not exist)")

    return len(errors) == 0, errors


def validate_scripts(skill_path: Path) -> tuple[bool, list[str]]:
    """Check scripts are executable."""
    warnings = []
    scripts_dir = skill_path / "scripts"

    if not scripts_dir.exists():
        return True, []

    for script in scripts_dir.glob("*.py"):
        if not os.access(script, os.X_OK):
            warnings.append(f"WARNING: {script.name} is not executable (run: chmod +x {script.name})")

    return True, warnings  # non-executable is a warning, not an error


def validate_skill(skill_path: str) -> bool:
    """Validate a single skill directory."""
    skill_path = Path(skill_path)

    if not skill_path.exists():
        print(f"FAIL: directory does not exist: {skill_path}")
        return False

    print(f"\n{'='*50}")
    print(f"  Validating: {skill_path.name}")
    print(f"{'='*50}")

    all_passed = True

    # 1. Frontmatter
    passed, msgs = validate_frontmatter(skill_path)
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] Frontmatter")
    for m in msgs:
        prefix = "  " if "WARNING" in m or "NOTE" in m else "    - "
        print(f"  {prefix}{m}")
    if not passed:
        all_passed = False

    # 2. SKILL.md length
    _, msgs = validate_skill_md_length(skill_path)
    if msgs:
        for m in msgs:
            print(f"  NOTE: {m}")

    # 3. Directory structure
    passed, errors = validate_directory_structure(skill_path)
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] Directory structure")
    for e in errors:
        print(f"    - {e}")
    if not passed:
        all_passed = False

    # 4. Scripts
    passed, msgs = validate_scripts(skill_path)
    if msgs:
        for m in msgs:
            print(f"  NOTE: {m}")

    # Summary
    print(f"{'='*50}")
    print(f"  {'ALL CHECKS PASSED' if all_passed else 'SOME CHECKS FAILED'}")
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Validate LingTai skill structure")
    parser.add_argument("skill", nargs="?", help="Skill directory path")
    args = parser.parse_args()

    if args.skill:
        success = validate_skill(args.skill)
        sys.exit(0 if success else 1)

    # Validate current directory
    current = Path.cwd()
    if (current / "SKILL.md").exists():
        success = validate_skill(current)
        sys.exit(0 if success else 1)
    else:
        print("Usage: python validate.py <skill-directory>")
        sys.exit(1)


if __name__ == "__main__":
    main()
