"""Focused guards for Shell's progressive-disclosure entrypoints."""
from __future__ import annotations

from pathlib import Path

from lingtai.tools.bash._shell_dialect import ShellKind
from lingtai.tools.bash._tool_family import get_description, get_schema


ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "src/lingtai/tools/bash/manual/SKILL.md"
ASYNC_REFERENCE = ROOT / "src/lingtai/tools/bash/manual/reference/async-jobs/SKILL.md"


def _run_branch() -> dict:
    return next(
        branch
        for branch in get_schema()["properties"]["input"]["anyOf"]
        if branch["title"] == "run input"
    )


def test_shell_description_keeps_first_call_and_safety_guards():
    description = get_description(
        dialect="powershell",
        shell_kind=ShellKind.POWERSHELL,
        host_os="Windows 11",
    )

    assert "Active shell dialect: powershell" in description
    assert "Active shell: PowerShell" in description
    assert "Host OS: Windows 11" in description
    assert "shell(action='manual', input={}, reasoning='...')" in description
    assert "shell(action='run', input={'command': '...'}, reasoning='...')" in description
    assert "shell(action='poll', input={'job_id': '...'}, reasoning='...')" in description
    assert "top-level status only says the shell spawned" in description
    assert "exit_code/ok" in description
    assert "kill-on-close Job Object" in description
    assert "input.async=true" in description

    run = _run_branch()
    assert "Timeout in seconds" in run["properties"]["timeout"]["description"]
    assert "LINGTAI_TOOL_TIMEOUT_MAX_SECONDS" in run["properties"]["timeout"]["description"]
    assert "agent working directory sandbox" in run["properties"]["working_dir"]["description"]
    assert "default 1800" in run["properties"]["reminder"]["description"]


def test_shell_manual_routes_durable_async_detail_to_reference():
    manual = MANUAL.read_text(encoding="utf-8")
    reference = ASYNC_REFERENCE.read_text(encoding="utf-8")

    assert "reference/async-jobs/SKILL.md" in manual
    assert "reference/scheduled-work/SKILL.md" in manual
    assert "reference/notification-reminders/SKILL.md" in manual
    assert "reference/debugging-cleanup/SKILL.md" in manual
    assert "return_handoff" not in manual
    assert "return_handoff" in reference
    assert "bash.reminder:<job_id>" in reference
    assert "Relaunch-safe status and cancellation" in reference
    assert "daemon-manual" in reference
