"""Release-candidate and route exclusivity regressions for kernel updates."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_MIGRATION = ROOT / "migration/migration.md"
_SUBSTRATE = ROOT / "src/lingtai/prompts/substrate/substrate.md"
_CHANNEL_MODEL = ROOT / "src/lingtai/tools/notification/manual/reference/channel-model/SKILL.md"
_RUNTIME_UPDATE = ROOT / "src/lingtai/intrinsic_skills/system-manual/reference/runtime-update-checks/SKILL.md"


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{path} has no YAML frontmatter"
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        parsed = re.match(
            r"^(release_version|release_tag|migration):\s*[\"']?([^\"']+?)[\"']?\s*$",
            line,
        )
        if parsed:
            values[parsed.group(1)] = parsed.group(2)
    return values


def test_migration_frontmatter_matches_authoritative_package_version_and_tag():
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = package["project"]["version"]
    metadata = _frontmatter(_MIGRATION)
    assert metadata["release_version"] == version
    assert metadata["release_tag"] == f"v{version}"


def test_migration_is_a_manual_release_migration_for_legacy_daemon_config():
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = package["project"]["version"]
    tag = f"v{version}"
    document = _MIGRATION.read_text(encoding="utf-8")
    body = " ".join(document.split())
    metadata = _frontmatter(_MIGRATION)

    assert metadata["migration"] == "manual"
    assert metadata["migration"] != "no-op"
    assert "**No configuration rewrite.**" not in body
    assert f"# LingTai kernel {version} migration" in body
    assert f"The target kernel release is `{version}` / tag `{tag}`" in body
    assert "`1.0.0`" not in body
    assert "`v1.0.0`" not in body
    assert "`v0.19.5`" not in body
    assert "`0.19.5`" not in body
    assert "post-tag" not in body
    assert "already cut" not in body
    assert "This document does not establish a corrected publication." not in body
    assert "`manifest.capabilities.daemon.max_emanations`" in body
    assert f"`max_emanations` was removed before `{version}`." in body
    assert "daemon capability setup can be skipped after upgrade" in body
    assert "`manager_pool_size=100`" in body
    assert "not a 1:1 mapping; no automatic conversion exists." in body
    assert "If this legacy key is absent, leave existing configuration unchanged." in body
    assert f"interpreter and reports `{version}`." in body


def test_kernel_update_guidance_uses_only_the_installer_route():
    for path in (_CHANNEL_MODEL, _RUNTIME_UPDATE):
        text = path.read_text(encoding="utf-8")
        assert "https://lingtai.ai/install.sh" in text
        assert "--help" in text
        assert "update --help" not in text
        assert "explicit human/config-owner" in text
        assert "https://lingtai.ai/skill.md" not in text
    # Resident substrate no longer restates the installer route; it routes to
    # `system-manual`, whose router sends runtime/update questions to
    # `runtime-update-checks` — the actual owner asserted above.
    substrate = _SUBSTRATE.read_text(encoding="utf-8")
    assert "https://lingtai.ai/install.sh" not in substrate
    assert "`system-manual`" in substrate


def test_repository_kernel_version_guidance_cannot_restore_obsolete_routes():
    """Scan every source Markdown guidance surface; historical exemptions must
    be added here with a path and a written reason rather than silently widening
    the banned-route pattern.
    """
    historical_exemptions: dict[str, str] = {}
    banned = (
        "https://lingtai.ai/skill.md",
        "normal install/update commands remain TUI-managed",
        "normal installation/update commands remain TUI-managed",
        "update --help",
    )
    for path in ROOT.rglob("*.md"):
        if ".git" in path.parts or "scratch" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if relative in historical_exemptions:
            assert historical_exemptions[relative], f"{relative} needs an exemption rationale"
            continue
        if re.search(r"kernel[_ -]?version|kernel version", text, re.IGNORECASE):
            for phrase in banned:
                assert phrase not in text, f"obsolete kernel-version route in {relative}: {phrase}"
            assert not re.search(r"separate\s+TUI\s+(?:update|updater)", text, re.IGNORECASE), relative


def test_update_guidance_keeps_source_drift_local_only():
    channel = _CHANNEL_MODEL.read_text(encoding="utf-8")
    runtime = _RUNTIME_UPDATE.read_text(encoding="utf-8")
    for text in (channel, runtime):
        assert "source_drift" in text
        assert "local" in text
        assert "release-migration" in text
    # Resident substrate no longer restates source_drift mechanics; it routes
    # to `system-manual`, whose router sends this to `runtime-update-checks` —
    # the actual owner asserted above.
    substrate = _SUBSTRATE.read_text(encoding="utf-8")
    assert "source_drift" not in substrate
    assert "`system-manual`" in substrate
