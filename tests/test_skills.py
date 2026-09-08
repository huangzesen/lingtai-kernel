"""Tests for the renamed skills capability."""
from __future__ import annotations

import importlib.util
from datetime import date, datetime
import json
import re
import sqlite3
import time
from pathlib import Path
from lingtai.agent import Agent
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def _parse_skill_frontmatter(skill_md: Path) -> dict:
    content = skill_md.read_text(encoding="utf-8")
    assert content.startswith("---\n"), f"missing frontmatter: {skill_md}"
    end = content.find("\n---\n", 4)
    assert end != -1, f"missing closing frontmatter delimiter: {skill_md}"
    import yaml

    data = yaml.safe_load(content[4:end])
    assert isinstance(data, dict), f"frontmatter is not a mapping: {skill_md}"
    return data


def _timestamp_text(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value).strip().strip('"\'')


def _assert_iso_timestamp(value, path: Path):
    text = _timestamp_text(value)
    assert text, f"missing last_changed_at: {path}"
    datetime.fromisoformat(text.replace("Z", "+00:00"))
    assert "T" in text, f"last_changed_at must include time: {path}"
    assert text.endswith("Z") or "+" in text[10:] or "-" in text[10:], (
        f"last_changed_at must include timezone: {path}"
    )


def _mk_agent(tmp_path: Path, skills_cfg: dict | None = None):
    """Create an agent with the skills capability, optionally passing kwargs."""
    caps = {"skills": skills_cfg or {}}
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=caps,
    )
    return agent, workdir


def _paths_of(agent):
    """The configured Tier-1 skills paths this agent was set up with."""
    for name, kwargs in agent._capabilities:
        if name == "skills":
            return list(kwargs.get("paths", []) or [])
    return []


def _reconcile_now(agent, paths):
    """Run the private catalog reconciliation the lifecycle owns.

    The retired public ``skills.info`` action used to expose this; the catalog
    itself did not move, so the health snapshot is now observed by calling the
    owner directly.
    """
    from lingtai.tools import skills as skills_tool

    return skills_tool._reconcile(agent, paths)


def _write_skill(folder: Path, name: str, desc: str = "test skill"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBody of {name}.\n"
    )


def test_skills_registers_no_public_tool(tmp_path):
    """The ``skills`` root and its ``info`` action were retired without alias."""
    agent, _ = _mk_agent(tmp_path)
    try:
        assert "skills" not in agent._tool_handlers
        assert "skills" not in agent._intrinsics
        assert "skills" not in [s.name for s in agent._build_tool_schemas()]
    finally:
        agent.stop(timeout=1.0)


def test_skills_package_exposes_no_schema_or_dispatch():
    from lingtai.tools import skills as skills_tool

    for attribute in ("get_schema", "get_description", "handle", "ACTION_ORDER"):
        assert not hasattr(skills_tool, attribute)
    # The retired info handler is gone too, not merely unregistered.
    assert not hasattr(skills_tool, "_skills_info")


def test_skills_capability_remains_registered_and_configurable(tmp_path):
    """Retiring the tool did not retire the capability or its ``paths``."""
    from lingtai.tools.registry import BUILTIN_TOOLS, CORE_DEFAULTS

    assert BUILTIN_TOOLS["skills"] == "lingtai.tools.skills"
    assert "skills" in CORE_DEFAULTS

    extra = tmp_path / "extra"
    _write_skill(extra / "configured-skill", "configured-skill")
    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        assert _paths_of(agent) == [str(extra)]
        assert "configured-skill" in agent._prompt_manager.read_section("skills")
    finally:
        agent.stop(timeout=1.0)




def test_lingtai_owned_skill_frontmatter_has_last_changed_at():
    root = Path(__file__).resolve().parents[1]
    skill_files = sorted((root / "src" / "lingtai").rglob("SKILL.md"))
    assert skill_files, "expected LingTai-owned source skills"
    for skill_md in skill_files:
        fm = _parse_skill_frontmatter(skill_md)
        _assert_iso_timestamp(fm.get("last_changed_at"), skill_md)


def test_skills_validator_can_require_last_changed_at(tmp_path):
    root = Path(__file__).resolve().parents[1]
    validator_path = root / "src" / "lingtai" / "tools" / "skills" / "manual" / "scripts" / "validate.py"
    spec = importlib.util.spec_from_file_location("skill_validate", validator_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    skill = tmp_path / "skill"
    _write_skill(skill, "demo-skill", "demo skill for validator")
    passed, messages = module.validate_frontmatter(skill, require_last_changed_at=True)
    assert not passed
    assert any("last_changed_at" in msg for msg in messages)

    (skill / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: demo skill for validator\n"
        "last_changed_at: \"2026-06-29T08:00:00Z\"\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    passed, messages = module.validate_frontmatter(skill, require_last_changed_at=True)
    assert passed, messages


# ---------------------------------------------------------------------------
# Structure & setup
# ---------------------------------------------------------------------------


def test_skills_setup_creates_per_agent_directories(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        assert (workdir / ".library").is_dir()
        assert (workdir / ".library" / "intrinsic").is_dir()
        assert (workdir / ".library" / "intrinsic" / "capabilities").is_dir()
        # Note: intrinsic/addons/ is not a skills-library concern; curated
        # addon implementations ship as MCP servers and are decompressed into
        # mcp_registry.jsonl by the `mcp` capability.
        assert (workdir / ".library" / "custom").is_dir()
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_hard_copies_intrinsics(tmp_path):
    # The Agent initializer installs each loaded capability's manual/ bundle
    # into intrinsic/capabilities/<cap>/. The skills capability documents
    # itself like every other capability.
    agent, workdir = _mk_agent(tmp_path)
    try:
        skill_md = (
            workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
        )
        assert skill_md.is_file()
        body = skill_md.read_text(encoding="utf-8")
        assert "name: skills-manual" in body
        assert "Nested skill/reference pattern for umbrella manuals" in body
        assert "Nested reference catalog" in body
        assert "## Routing table" in body
        assert "children's routing metadata explicitly" in body
        assert "machine-readable routing table" in body
        assert "Do not leave the parent as only a prose list of links" in body
        assert "reference/substrate-manual/SKILL.md" in body
        assert "The catalog scanner treats a directory that already" in body
        assert "validate.py reference/topic-a/" in body

        bash_md = (
            workdir / ".library" / "intrinsic" / "capabilities" / "shell" / "SKILL.md"
        )
        assert bash_md.is_file()
        bash_body = bash_md.read_text(encoding="utf-8")
        assert "name: shell-manual" in bash_body
        assert "Nested reference catalog" in bash_body
        assert "reference/scheduled-work/SKILL.md" in bash_body
        assert "reference/notification-reminders/SKILL.md" in bash_body
        assert "reference/debugging-cleanup/SKILL.md" in bash_body

        web_root = (
            workdir / ".library" / "intrinsic" / "capabilities" / "web"
        )
        assert "name: web-manual" in (web_root / "SKILL.md").read_text(encoding="utf-8")
        assert (web_root / "scripts" / "extract_page.py").is_file()
        assert (web_root / "reference" / "tier-quick-refs" / "SKILL.md").is_file()
        # The bash manual routes coding-CLI detail to daemon-manual, but must
        # never resurrect a retired bash-* page or re-claim a non-backend CLI
        # as a daemon backend (only the ten real backends have backend ids).
        assert "daemon-manual" in bash_body
        assert "reference/cli-backends/SKILL.md" in bash_body
        for gone in (
            "bash-claude-code",
            "bash-gemini-cli",
            "bash-aider",
            "bash-goose",
            "bash-openhands",
            "bash-crush",
            "bash-zed-acp",
        ):
            assert gone not in bash_body

        bash_reference_dir = bash_md.parent / "reference"
        for reference_name in (
            "scheduled-work",
            "notification-reminders",
            "debugging-cleanup",
        ):
            bash_reference = bash_reference_dir / reference_name / "SKILL.md"
            assert bash_reference.is_file()
        assert "Nested shell-manual reference" in (
            bash_reference_dir / "scheduled-work" / "SKILL.md"
        ).read_text(encoding="utf-8")

        daemon_md = (
            workdir / ".library" / "intrinsic" / "capabilities" / "daemon" / "SKILL.md"
        )
        assert daemon_md.is_file()
        daemon_body = daemon_md.read_text(encoding="utf-8")
        assert "name: daemon-manual" in daemon_body
        assert "Nested reference catalog" in daemon_body
        assert "reference/forensics/SKILL.md" in daemon_body
        assert "reference/inspection/SKILL.md" in daemon_body
        assert "reference/cli-backends/SKILL.md" in daemon_body
        assert "reference/cleanup/SKILL.md" in daemon_body

        daemon_reference_dir = daemon_md.parent / "reference"
        for reference_name in ("forensics", "inspection", "cli-backends", "cleanup"):
            daemon_reference = daemon_reference_dir / reference_name / "SKILL.md"
            assert daemon_reference.is_file()
        for backend_name in (
            "codex",
            "opencode",
            "claude-p",
            "mimocode",
            "qwen-code",
            "kimicode",
            "cursor",
            "deepseek",
            "oh-my-pi",
            "lingtai",
        ):
            backend_reference = (
                daemon_reference_dir
                / "cli-backends"
                / "reference"
                / "backends"
                / backend_name
                / "SKILL.md"
            )
            assert backend_reference.is_file()
        assert "Nested daemon-manual reference" in (
            daemon_reference_dir / "forensics" / "SKILL.md"
        ).read_text(encoding="utf-8")
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_hard_copies_standalone_intrinsic_skills(tmp_path):
    # Standalone always-included skills live in lingtai.intrinsic_skills and are
    # copied next to capability manuals under .library/intrinsic/capabilities/.
    agent, workdir = _mk_agent(tmp_path)
    try:
        skill_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "file-manual"
            / "SKILL.md"
        )
        assert skill_md.is_file()
        body = skill_md.read_text(encoding="utf-8")
        assert "name: file-manual" in body
        assert "encoding='gbk'" in body
        assert "iconv -f gbk -t utf-8" in body

        system_manual_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "system-manual"
            / "SKILL.md"
        )
        assert system_manual_md.is_file()
        system_manual_body = system_manual_md.read_text(encoding="utf-8")
        assert "name: system-manual" in system_manual_body
        assert "Progressive Disclosure Router" in system_manual_body
        assert "reference/substrate-manual/SKILL.md" in system_manual_body
        assert "reference/procedures-manual/SKILL.md" in system_manual_body
        assert "reference/sqlite-log-query/SKILL.md" in system_manual_body
        assert "reference/runtime-update-checks/SKILL.md" in system_manual_body
        assert "reference/refresh-precheck/SKILL.md" in system_manual_body
        assert "reference/trajectory-mining/SKILL.md" in system_manual_body
        assert "lingtai-agent log doctor" in system_manual_body
        assert "lingtai-agent log query" in system_manual_body
        assert "lingtai-agent log rebuild" in system_manual_body
        assert "name: substrate-manual" in system_manual_body
        assert "name: procedures-manual" in system_manual_body
        assert "name: sqlite-log-query" in system_manual_body
        assert "name: runtime-update-checks" in system_manual_body
        assert "name: refresh-precheck" in system_manual_body
        assert "name: trajectory-mining" in system_manual_body
        assert "name: external-attach-diagnostic" in system_manual_body
        assert "reference/external-attach-diagnostic/SKILL.md" in system_manual_body
        assert "Nested reference catalog" in system_manual_body
        assert "location: reference/notification-manual/SKILL.md" not in system_manual_body
        external_attach = workdir / ".library" / "intrinsic" / "capabilities" / "system-manual"
        external_attach = external_attach / "reference" / "external-attach-diagnostic"
        assert (external_attach / "SKILL.md").is_file()
        assert (external_attach / "scripts" / "external_attach_diagnostic.py").is_file()

        notification_manual_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "notification"
            / "SKILL.md"
        )
        assert notification_manual_md.is_file()
        notification_manual_body = notification_manual_md.read_text(encoding="utf-8")
        assert "name: notification-manual" in notification_manual_body
        assert "# Notification Manual" in notification_manual_body
        assert "<agent>/.library/intrinsic/capabilities/notification/SKILL.md" in notification_manual_body
        assert "location: reference/channel-model/SKILL.md" in notification_manual_body
        assert "location: reference/dismissal-safety/SKILL.md" in notification_manual_body
        assert (
            notification_manual_md.parent / "reference" / "channel-model" / "SKILL.md"
        ).is_file()
        assert (
            notification_manual_md.parent / "reference" / "dismissal-safety" / "SKILL.md"
        ).is_file()
        assert not (
            system_manual_md.parent / "reference" / "notification-manual"
        ).exists()

        substrate_ref = system_manual_md.parent / "reference" / "substrate-manual" / "SKILL.md"
        assert substrate_ref.is_file()
        substrate_body = substrate_ref.read_text(encoding="utf-8")
        assert "name: substrate-manual" in substrate_body
        assert "Nested system-manual reference" in substrate_body
        assert "# Substrate Manual" in substrate_body
        assert "**ACTIVE**" in substrate_body
        assert "**ASLEEP**" in substrate_body
        assert "**SUSPENDED**" in substrate_body
        assert "MCP and addon ownership" in substrate_body
        assert "notification" in substrate_body
        assert "dismiss" in substrate_body

        procedures_ref = system_manual_md.parent / "reference" / "procedures-manual" / "SKILL.md"
        assert procedures_ref.is_file()
        procedures_body = procedures_ref.read_text(encoding="utf-8")
        assert "name: procedures-manual" in procedures_body
        assert "Nested system-manual reference" in procedures_body
        assert "# Procedures Manual" in procedures_body
        assert "Human-facing deliverables" in procedures_body
        assert "external side effects" in procedures_body
        assert "Resident procedures maintenance" in procedures_body

        runtime_update_ref = (
            system_manual_md.parent / "reference" / "runtime-update-checks" / "SKILL.md"
        )
        assert runtime_update_ref.is_file()
        runtime_update_body = runtime_update_ref.read_text(encoding="utf-8")
        assert "name: runtime-update-checks" in runtime_update_body
        assert "Nested system-manual reference" in runtime_update_body
        assert "# Runtime Update Checks" in runtime_update_body
        assert "kind: kernel_version" in runtime_update_body
        assert ".notification/nudge.json" in runtime_update_body
        assert "roughly 60-second in-memory probe gate" in runtime_update_body
        assert "editable/source/dev" in runtime_update_body
        assert "receiving explicit confirmation" in runtime_update_body

        context_manual_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "context-manual"
            / "SKILL.md"
        )
        assert context_manual_md.is_file()
        context_manual_body = context_manual_md.read_text(encoding="utf-8")
        assert "name: context-manual" in context_manual_body
        assert "## Reference and asset catalog" in context_manual_body
        assert "reference/molt-manual/SKILL.md" in context_manual_body
        assert "reference/summarize-manual/SKILL.md" in context_manual_body
        assert "assets/molt-template.md" in context_manual_body
        assert "9. **Context Status**" not in context_manual_body
        molt_reference = context_manual_md.parent / "reference" / "molt-manual" / "SKILL.md"
        assert molt_reference.is_file()
        molt_reference_body = molt_reference.read_text(encoding="utf-8")
        assert "name: molt-manual" in molt_reference_body
        assert "nine-section scaffold" in molt_reference_body
        assert "## Input field dictionary" in molt_reference_body
        assert "type: session-journal" in molt_reference_body

        file_manual_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "file-manual"
            / "SKILL.md"
        )
        assert file_manual_md.is_file()
        file_manual_body = file_manual_md.read_text(encoding="utf-8")
        package_file_manual = Path("src/lingtai/tools/file/manual/SKILL.md").read_text(
            encoding="utf-8"
        )
        assert file_manual_body == package_file_manual
        assert "# File Manual" in file_manual_body
        assert not (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "file"
        ).exists()

        molt_template_asset = context_manual_md.parent / "assets" / "molt-template.md"
        assert molt_template_asset.is_file()
        molt_template_body = molt_template_asset.read_text(encoding="utf-8")
        assert "# Consequential Molt Summary Template" in molt_template_body
        assert "## Summary scaffold" in molt_template_body
        for section in (
            "1. **Who I Am**",
            "2. **Accomplishments**",
            "3. **Outstanding Tasks**",
            "4. **Action Checklist**",
            "5. **Collaborators**",
            "6. **Durable Memory and Execution Notes**",
            "7. **Key Paths and Artifacts**",
            "8. **Lessons and Gotchas**",
            "9. **Context Status**",
        ):
            assert section in molt_template_body
        assert "## Pre-molt verification checklist" in molt_template_body

        sqlite_log_query_ref = system_manual_md.parent / "reference" / "sqlite-log-query" / "SKILL.md"
        assert sqlite_log_query_ref.is_file()
        sqlite_log_query_body = sqlite_log_query_ref.read_text(encoding="utf-8")
        assert "name: sqlite-log-query" in sqlite_log_query_body
        assert "Nested system-manual reference" in sqlite_log_query_body
        assert "# SQLite Log Query" in sqlite_log_query_body
        assert "lingtai-agent log query" in sqlite_log_query_body
        assert "scripts/event_summary.py" in sqlite_log_query_body
        # Trajectory mining is its own sibling reference; sqlite-log-query keeps
        # the data layer (schema, SQL recipes, redaction) and routes to it.
        assert "trajectory mining" in sqlite_log_query_body.lower()
        assert "../trajectory-mining/SKILL.md" in sqlite_log_query_body

        trajectory_ref = system_manual_md.parent / "reference" / "trajectory-mining" / "SKILL.md"
        assert trajectory_ref.is_file()
        trajectory_body = trajectory_ref.read_text(encoding="utf-8")
        assert "name: trajectory-mining" in trajectory_body
        assert "# Trajectory Mining" in trajectory_body
        assert "finding schema" in trajectory_body.lower()
        assert "cheap model" in trajectory_body.lower() or "cheap-model" in trajectory_body.lower()
        assert "reference/sqlite-log-query/SKILL.md" in trajectory_body

        refresh_precheck_ref = (
            system_manual_md.parent / "reference" / "refresh-precheck" / "SKILL.md"
        )
        assert refresh_precheck_ref.is_file()
        refresh_precheck_body = refresh_precheck_ref.read_text(encoding="utf-8")
        assert "name: refresh-precheck" in refresh_precheck_body
        assert "Nested system-manual reference" in refresh_precheck_body
        assert 'system(action="refresh")' in refresh_precheck_body
        assert 'system(action="presets")' in refresh_precheck_body
        assert ".pth" in refresh_precheck_body

        # event_summary.py script exists, is referenced, and can summarize
        # a minimal SQLite sidecar using the actual events schema columns.
        sqlite_scripts = sqlite_log_query_ref.parent / "scripts" / "event_summary.py"
        assert sqlite_scripts.is_file(), "event_summary.py script must exist"
        spec = importlib.util.spec_from_file_location("event_summary_for_test", sqlite_scripts)
        assert spec and spec.loader
        event_summary = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(event_summary)

        db_path = tmp_path / "log.sqlite"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              type TEXT NOT NULL,
              agent_address TEXT,
              agent_name_snapshot TEXT,
              fields_json TEXT NOT NULL,
              source_file TEXT,
              source_offset INTEGER,
              source_line INTEGER,
              source_kind TEXT,
              scope TEXT,
              run_id TEXT,
              inserted_at TEXT
            );
            """
        )
        now = time.time()
        conn.executemany(
            "INSERT INTO events(ts, type, fields_json, source_kind, scope, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (now - 60, "tool_call", json.dumps({"name": "bash"}), "agent_events", "agent", None),
                (now, "tool_result", json.dumps({"error": "token abcdefghijklmnop"}), "agent_events", "agent", None),
            ],
        )
        conn.commit()
        conn.close()
        summary = event_summary.summarize(str(db_path), source_kind="agent_events", hours=1)
        assert summary["total_events"] == 2
        assert summary["event_type_counts"]
        assert summary["schema_keys"]
        assert summary["error_clusters"][0]["error"] == "token=[REDACTED]"

        # No standalone top-level trajectory-mining skill: the capability is
        # intentionally exposed only through system-manual's SQLite reference.
        trajectory_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "lingtai-trajectory-mining"
            / "SKILL.md"
        )
        assert not trajectory_md.exists()
        assert "trajectory/anomaly mining" in system_manual_body
        assert "sqlite-log-query" in system_manual_body

        doctor_md = (
            workdir
            / ".library"
            / "intrinsic"
            / "capabilities"
            / "lingtai-doctor"
            / "SKILL.md"
        )
        doctor_script = doctor_md.parent / "scripts" / "doctor.py"
        assert doctor_md.is_file()
        assert doctor_script.is_file()
        assert "name: lingtai-doctor" in doctor_md.read_text(encoding="utf-8")
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_overwrites_stale_intrinsic(tmp_path):
    # The Agent initializer wipes-and-rewrites intrinsic/ on construction.
    # A stale entry from a previous kernel version must be replaced.
    workdir = tmp_path / "agent"
    stale = (
        workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
    )
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("---\nname: skills-manual\ndescription: STALE\n---\n")

    # Also leave a stale top-level dir to confirm wipe-and-rewrite scrubs old layouts.
    old_layout = workdir / ".library" / "intrinsic" / "skill-for-skill" / "SKILL.md"
    old_layout.parent.mkdir(parents=True, exist_ok=True)
    old_layout.write_text("---\nname: skill-for-skill\ndescription: ANCIENT\n---\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        body = stale.read_text()
        assert "STALE" not in body
        assert "The Skills Capability" in body or "skills-manual" in body
        # Old layout scrubbed.
        assert not old_layout.exists()
    finally:
        agent.stop(timeout=1.0)


def test_skills_setup_leaves_custom_untouched(tmp_path):
    workdir = tmp_path / "agent"
    user_skill = workdir / ".library" / "custom" / "my-tool" / "SKILL.md"
    user_skill.parent.mkdir(parents=True, exist_ok=True)
    user_skill.write_text("---\nname: my-tool\ndescription: Mine\n---\nUser content.\n")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        assert user_skill.read_text() == "---\nname: my-tool\ndescription: Mine\n---\nUser content.\n"
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_skills_scans_absolute_path(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "shared-skill", "shared-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        result = _reconcile_now(agent, _paths_of(agent))
        assert result["status"] == "ok"
        assert result["paths"][str(extra)]["skills"] == 1
        assert result["catalog_size"] >= 2  # skills-manual + shared-skill
    finally:
        agent.stop(timeout=1.0)


def test_skills_resolves_relative_path_from_working_dir(tmp_path):
    # Build a network-root layout: tmp_path is the network root.
    # The agent lives at tmp_path/agent, and .library_shared sits at tmp_path/.library_shared.
    shared = tmp_path / ".library_shared"
    _write_skill(shared / "net-skill", "net-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": ["../.library_shared"]})
    try:
        result = _reconcile_now(agent, _paths_of(agent))
        assert result["status"] == "ok"
        assert result["paths"]["../.library_shared"]["exists"] is True
        assert result["paths"]["../.library_shared"]["skills"] == 1
    finally:
        agent.stop(timeout=1.0)


def test_skills_expands_tilde(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows expanduser uses USERPROFILE
    utils = fake_home / "my-utils"
    _write_skill(utils / "util-skill", "util-skill")

    agent, _ = _mk_agent(tmp_path, {"paths": ["~/my-utils"]})
    try:
        result = _reconcile_now(agent, _paths_of(agent))
        assert result["paths"]["~/my-utils"]["exists"] is True
    finally:
        agent.stop(timeout=1.0)


def test_skills_reports_missing_path_as_not_existing(tmp_path):
    agent, _ = _mk_agent(tmp_path, {"paths": ["/does/not/exist"]})
    try:
        result = _reconcile_now(agent, _paths_of(agent))
        assert result["paths"]["/does/not/exist"]["exists"] is False
        assert result["paths"]["/does/not/exist"]["skills"] == 0
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# info action
# ---------------------------------------------------------------------------


def test_reconcile_result_omits_the_manual_body(tmp_path):
    """The health snapshot never carries manual bodies."""
    agent, _ = _mk_agent(tmp_path)
    try:
        result = _reconcile_now(agent, _paths_of(agent))
        assert "skills_manual" in result  # the composer still reads it for health
        assert result["status"] == "ok"
        assert "error" not in result
    finally:
        agent.stop(timeout=1.0)


def test_manual_body_is_returned_by_psyche(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        expected = (
            workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
        ).read_text(encoding="utf-8")
        result = agent._intrinsics["psyche"](
            {"action": "skills", "input": {}, "reasoning": "test"}
        )
        assert result["status"] == "ok"
        assert result["manual"] == expected
        assert result["manual_path"] and Path(result["manual_path"]).as_posix().endswith(
            ".library/intrinsic/capabilities/skills/SKILL.md"
        )
    finally:
        agent.stop(timeout=1.0)




def test_info_reports_degraded_when_intrinsic_missing(tmp_path):
    # The skills capability is pure presentation — it does NOT reinstall
    # manuals when info is called. So if the initializer-installed manual is
    # deleted out-of-band after setup, info must report degraded.
    agent, workdir = _mk_agent(tmp_path)
    try:
        manual_path = (
            workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
        )
        assert manual_path.is_file(), "precondition: initializer installed manual"
        manual_path.unlink()

        result = _reconcile_now(agent, _paths_of(agent))
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
        capabilities={"skills": {}},
    )
    try:
        result = _reconcile_now(agent, _paths_of(agent))
        problem_folders = [p["folder"] for p in result["problems"]]
        assert any("broken" in f for f in problem_folders)
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# LTP v2 migration: exact semantic preservation of both children
# ---------------------------------------------------------------------------


def test_reconcile_result_keys_and_health_are_exactly_preserved(tmp_path):
    """The catalog health snapshot shape is unchanged by the tool retirement."""
    extra = tmp_path / "extra"
    _write_skill(extra / "good", "good")
    (extra / "broken").mkdir(parents=True, exist_ok=True)
    (extra / "broken" / "SKILL.md").write_text("---\nname: broken\n---\n")

    agent, workdir = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        result = _reconcile_now(agent, [str(extra)])
        assert result["status"] == "ok"
        assert result["skills_dir"] == str(workdir / ".library")
        assert result["library_dir"] == result["skills_dir"]
        assert result["paths"] == {
            str(extra): {"resolved": str(extra), "exists": True, "skills": 1}
        }
        assert result["problems"] == [
            {
                "folder": "broken",
                "reason": "SKILL.md missing required frontmatter field: description",
            }
        ]
        assert result["catalog_size"] >= 2
    finally:
        agent.stop(timeout=1.0)


def test_psyche_skills_manual_has_no_catalog_side_effect(tmp_path, monkeypatch):
    """Loading the manual must not rescan or reinject the catalog."""
    from lingtai.tools import skills as skills_tool

    extra = tmp_path / "extra"
    _write_skill(extra / "before-skill", "before-skill")
    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        before = agent._prompt_manager.read_section("skills") or ""
        _write_skill(extra / "after-skill", "after-skill")

        def _boom(*_a, **_k):
            raise AssertionError("manual rescanned the catalog")

        monkeypatch.setattr(skills_tool, "_reconcile", _boom)
        manual_result = agent._intrinsics["psyche"](
            {"action": "skills", "input": {}, "reasoning": "read manual"}
        )
        assert manual_result["status"] == "ok"
        unchanged = agent._prompt_manager.read_section("skills") or ""
        assert unchanged == before
        assert "- name: after-skill" not in unchanged
        for health_only in ("catalog_size", "paths", "problems", "skills_dir"):
            assert health_only not in manual_result

        # The private lifecycle is what reconciles — the new skill appears now.
        monkeypatch.undo()
        info_result = _reconcile_now(agent, [str(extra)])
        refreshed = agent._prompt_manager.read_section("skills") or ""
        assert "- name: after-skill" in refreshed
        assert info_result["paths"][str(extra)]["skills"] == 2
    finally:
        agent.stop(timeout=1.0)


def test_manual_degrades_with_exact_loader_message(tmp_path):
    """A missing manual degrades through the shared loader message."""
    agent, workdir = _mk_agent(tmp_path)
    try:
        manual_path = (
            workdir / ".library" / "intrinsic" / "capabilities" / "skills" / "SKILL.md"
        )
        assert manual_path.is_file(), "precondition: initializer installed manual"
        manual_path.unlink()

        assert agent._intrinsics["psyche"](
            {"action": "skills", "input": {}, "reasoning": "read manual"}
        ) == {
            "status": "degraded",
            "manual": "",
            "manual_path": str(manual_path),
            "error": (
                "skills manual missing — initializer may have failed or "
                "capability not installed correctly"
            ),
        }
    finally:
        agent.stop(timeout=1.0)


def test_no_skills_root_reaches_either_provider_wire(tmp_path):
    """The retired root must not appear on Chat Completions or Responses."""
    from lingtai.kernel.base_agent.tools import _build_tool_schemas

    agent, _ = _mk_agent(tmp_path)
    try:
        names = [s.name for s in _build_tool_schemas(agent)]
        assert "skills" not in names
        assert names.count("psyche") == 1
    finally:
        agent.stop(timeout=1.0)


def test_skills_is_no_longer_an_ltp_v2_summarize_family():
    """The root `summarize` control follows the surviving public root."""
    from lingtai.kernel.tool_result_summary import summary_requested

    assert summary_requested({"summarize": True}, tool_name="psyche") is True
    assert summary_requested({"summarize": False}, tool_name="psyche") is False
    # The retired root is not recognized, exactly like any unmigrated name.
    assert summary_requested({"summarize": True}, tool_name="skills") is False
    assert summary_requested({"summarize": True}, tool_name="bash") is False


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_catalog_injected_into_skills_section(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "shared-thing", "shared-thing")

    agent, _ = _mk_agent(tmp_path, {"paths": [str(extra)]})
    try:
        prompt = agent._prompt_manager.read_section("skills") or ""
        assert "- name: skills-manual" in prompt
        assert "- name: file-manual" in prompt
        assert "- name: shared-thing" in prompt
    finally:
        agent.stop(timeout=1.0)


def test_web_manual_is_one_top_level_catalog_entry_matching_the_tool(tmp_path):
    # Ownership collapse contract: the generic skill catalogue must expose
    # exactly one top-level entry for the web tool's manual, named
    # `web-manual`, with no separate top-level `web-browsing` identity — and
    # `web(action="manual")` must return the same installed SKILL.md bytes
    # that catalogue entry was built from. Nested reference/<topic>/SKILL.md
    # files stay parent-owned drill-down references, never separate
    # top-level catalogue entries.
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}, "web": {"provider": "duckduckgo"}},
    )
    try:
        prompt = agent._prompt_manager.read_section("skills") or ""
        top_level_names = re.findall(r"^- name: (\S+)$", prompt, flags=re.MULTILINE)

        web_entries = [n for n in top_level_names if n == "web-manual" or n.startswith("web-browsing")]
        assert web_entries == ["web-manual"], (
            f"expected exactly one top-level 'web-manual' entry and no 'web-browsing*' "
            f"entry, got: {web_entries}"
        )
        assert not any(n.startswith("web-browsing") for n in top_level_names)

        installed_manual_path = (
            workdir / ".library" / "intrinsic" / "capabilities" / "web" / "SKILL.md"
        )
        installed_text = installed_manual_path.read_text(encoding="utf-8")
        assert "name: web-manual" in installed_text

        tool_result = agent._tool_handlers["web"]({"action": "manual", "input": {}})
        assert tool_result["status"] == "ok"
        # Compare decoded text (the tool returns universal-newline text; the
        # checked-out file may carry CRLF on Windows via git autocrlf).
        assert tool_result["manual"] == installed_text
        assert tool_result["manual_path"] == str(installed_manual_path)

        # Nested reference SKILL.md files exist and are parent-owned — they
        # must not surface as additional top-level catalogue names.
        nested = [
            "web-manual-tier-quick-refs",
            "web-manual-routing-and-sites",
            "web-manual-maintenance-bundles",
        ]
        for name in nested:
            assert name not in top_level_names
    finally:
        agent.stop(timeout=1.0)


def test_catalog_rendering_is_readable_without_xml_quote_noise(tmp_path):
    # The catalog goes straight into the system prompt; humans (and the model)
    # complained that the prior XML shape was escape soup. Pin the YAML shape:
    # per-skill block with a `description:` block scalar carrying raw quotes
    # and apostrophes, no `&quot;` / `&apos;` over-escaping noise.
    workdir = tmp_path / "agent"
    _write_skill(
        workdir / ".library" / "custom" / "fancy-tool",
        "fancy-tool",
        'Handles "quoted" args and \'apostrophes\' — keep them raw.',
    )

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        prompt = agent._prompt_manager.read_section("skills") or ""
        # No spurious escape entities for `"` and `'` in element text.
        assert "&quot;" not in prompt
        assert "&apos;" not in prompt
        # YAML shape: `- name:` entry with a `description: |` block scalar.
        assert "- name: fancy-tool" in prompt
        assert "  description: |" in prompt
        # Body sits one level deeper than the `description:` field.
        assert "    Handles \"quoted\" args" in prompt
    finally:
        agent.stop(timeout=1.0)


def test_custom_skills_appear_in_catalog(tmp_path):
    workdir = tmp_path / "agent"
    _write_skill(workdir / ".library" / "custom" / "my-tool", "my-tool", "my desc")

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"skills": {}},
    )
    try:
        prompt = agent._prompt_manager.read_section("skills") or ""
        assert "my-tool" in prompt
        assert "my desc" in prompt
    finally:
        agent.stop(timeout=1.0)



# NOTE: `knowledge` and `skills` are now default-on (the always-on tool floor
# boots on every Agent). The tests below preserve the breaking-rename guarantee
# at its remaining surface: legacy `library` / `codex` capability NAMES must not
# themselves produce tool handlers. Whether `knowledge`/`skills` are present is
# governed by core defaults, not by alias normalization.


def test_former_library_config_does_not_register_library_tool(tmp_path):
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {}},
    )
    try:
        assert "library" not in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_former_library_list_config_does_not_register_library_tool(tmp_path):
    workdir = tmp_path / "agent"
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities=["library"],
    )
    try:
        assert "library" not in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_former_library_paths_do_not_leak_into_skills_catalog(tmp_path):
    """Skills extra paths must come from the `skills` cap, not `library` alias."""
    extra = tmp_path / "extra"
    _write_skill(extra / "old-shared", "old-shared")
    workdir = tmp_path / "agent"

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"library": {"paths": [str(extra)]}},
    )
    try:
        # `skills` is default-on, but the legacy `library.paths` must not be
        # picked up as an extra skill path by alias normalization.
        assert "old-shared" not in (agent._prompt_manager.read_section("skills") or "")
    finally:
        agent.stop(timeout=1.0)


def test_former_codex_library_pair_does_not_register_legacy_tools(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "paired-shared", "paired-shared")
    workdir = tmp_path / "agent"

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"codex": {}, "library": {"paths": [str(extra)]}},
    )
    try:
        assert "codex" not in agent._tool_handlers
        assert "library" not in agent._tool_handlers
    finally:
        agent.stop(timeout=1.0)


def test_new_knowledge_and_skills_config_registers_both(tmp_path):
    extra = tmp_path / "extra"
    _write_skill(extra / "new-shared", "new-shared")
    workdir = tmp_path / "agent"

    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"knowledge": {}, "skills": {"paths": [str(extra)]}},
    )
    try:
        # Both capabilities are set up; neither registers a public tool any more.
        assert {"knowledge", "skills"}.isdisjoint(agent._tool_handlers)
        assert {"knowledge", "skills"} <= {n for n, _ in agent._capabilities}
        assert "library" not in agent._tool_handlers
        assert "codex" not in agent._tool_handlers
        assert "new-shared" in (agent._prompt_manager.read_section("skills") or "")
        # Knowledge is filesystem-backed and isomorphic to skills: author by
        # writing knowledge/<name>/KNOWLEDGE.md, then apply with one rebuild.
        entry_dir = workdir / "knowledge" / "new-entry"
        entry_dir.mkdir(parents=True)
        (entry_dir / "KNOWLEDGE.md").write_text(
            "---\nname: new-entry\ndescription: A freshly authored knowledge entry.\n---\nBody.\n"
        )
        # Writing alone does not hot-load the catalog section.
        assert "new-entry" not in (agent._prompt_manager.read_section("knowledge") or "")
        agent._reconstruct_context()
        assert "new-entry" in (agent._prompt_manager.read_section("knowledge") or "")
        # And the skills catalog was recomposed by the same one rebuild.
        assert "new-shared" in (agent._prompt_manager.read_section("skills") or "")
    finally:
        agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# No git operations
# ---------------------------------------------------------------------------


def test_skills_does_not_create_git_repo(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    try:
        assert not (workdir / ".library" / ".git").exists()
    finally:
        agent.stop(timeout=1.0)


def test_resident_prompts_route_to_system_manual_nested_references():
    root = Path(__file__).resolve().parents[1]

    substrate = (root / "src" / "lingtai" / "prompts" / "substrate" / "substrate.md").read_text(
        encoding="utf-8"
    )
    assert "read\n`system-manual` → `reference/substrate-manual/SKILL.md`" in substrate

    procedures = (root / "src" / "lingtai" / "prompts" / "procedures" / "procedures.md").read_text(
        encoding="utf-8"
    )
    assert "`system-manual` → `reference/procedures-manual/SKILL.md`" in procedures


def test_tool_plugin_settings_reference_is_catalogued_and_routable():
    root = Path(__file__).resolve().parents[1]
    router_path = root / "src/lingtai/intrinsic_skills/system-manual/SKILL.md"
    router_body = router_path.read_text(encoding="utf-8")
    location = "reference/tool-plugin-settings/SKILL.md"
    assert f"location: {location}" in router_body and f"`{location}`" in router_body
    assert _parse_skill_frontmatter(router_path.parent / location)["name"] == (
        "tool-plugin-settings-reference"
    )


def test_skills_manual_documents_external_skill_intake_default():
    manual = (
        Path(__file__).resolve().parents[1]
        / "src/lingtai/tools/skills/manual/SKILL.md"
    ).read_text(encoding="utf-8")
    required = [
        "## External skill intake (default flow)",
        "<agent>/.library/custom/<skill-name>/",
        "run the bundled validator",
        'context(action="rebuild", input={}, reasoning="rescan skills catalog")',
        "the skill is\n   only a file on disk",
        "Each receiving agent clones/copies it into",
        "Do not assume `.library_shared` is loaded by default",
        "add `../.library_shared` to each participating",
    ]
    for phrase in required:
        assert phrase in manual


def test_context_manual_routes_skill_sharing_through_custom_by_default():
    root = Path(__file__).resolve().parents[1]
    manual_path = root / "src/lingtai/tools/context/manual/SKILL.md"
    manual = manual_path.read_text(encoding="utf-8")
    assert "reference/molt-manual/SKILL.md" in manual
    detail = (manual_path.parent / "reference" / "molt-manual" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "peers install it into their own `.library/custom/<name>/`" in detail
    assert "explicit opt-in" in detail
    assert "local-network shared root" in detail


def test_resident_layers_query_settings_instead_of_copying_adjustable_defaults():
    root = Path(__file__).resolve().parents[1]
    for name in ("substrate", "procedures"):
        source = root / "src" / "lingtai" / "prompts" / name / f"{name}.md"
        body = " ".join(source.read_text(encoding="utf-8").split("---", 2)[2].split())
        assert "settings" in body
        assert 'system(action="settings", input={})' in body
        assert "without another" in body
        assert "1,000,000" not in body
        assert "2,000,000" not in body
