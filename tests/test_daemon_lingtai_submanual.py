"""Docs/routing contract for the built-in LingTai daemon-backend submanual.

The LingTai child under the daemon backend router is the built-in sibling of
the Codex flag-discovery page: a small progressive-disclosure knowledge
entrypoint. Unlike CLI backend pages it discloses *knowledge routing*, not
flags — `backend="lingtai"` is the in-process ChatSession default with no
external CLI and no `backend_options` surface, so the page must route agents
to the live authorities (daemon-manual router, `system(action="presets")`
preset inspection, tools/skills/MCP inheritance rules, CONTRACT.md)
and must never grow into a duplicated rules catalog or invent a flag
surface. Runtime behavior itself is covered by ``tests/test_daemon.py`` and
``tests/test_daemon_backend_options.py`` and is not re-tested here.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md"
TOP_MANUAL = ROOT / "src/lingtai/tools/daemon/manual/SKILL.md"
CHILD = (
    ROOT
    / "src/lingtai/tools/daemon/manual/reference/cli-backends"
    / "reference/backends/lingtai/SKILL.md"
)
CHILD_LOCATION = "reference/backends/lingtai/SKILL.md"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} must start with YAML frontmatter"
    _blank, frontmatter, _body = text.split("---", 2)
    return yaml.safe_load(frontmatter) or {}


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.split("---", 2)[2]


def test_router_has_yaml_catalog_entry_for_lingtai_child():
    text = ROUTER.read_text(encoding="utf-8")
    assert "## Nested reference catalog" in text
    catalog_section = text.split("## Nested reference catalog", 1)[1]
    match = re.search(r"```yaml\n(.*?)```", catalog_section, re.DOTALL)
    assert match, "nested reference catalog must be a fenced YAML block"
    entries = yaml.safe_load(match.group(1))
    lingtai_entries = [e for e in entries if e.get("location") == CHILD_LOCATION]
    assert len(lingtai_entries) == 1
    assert lingtai_entries[0]["name"] == "daemon-backend-lingtai"
    assert lingtai_entries[0]["description"].strip()


def test_top_manual_has_one_owner_routing_table():
    text = TOP_MANUAL.read_text(encoding="utf-8")
    assert "## Nested reference catalog" not in text
    assert text.count("## Routing table") == 1
    table = text.split("## Routing table", 1)[1]
    rows = "\n".join(line for line in table.splitlines() if line.startswith("|"))
    assert rows.count(CHILD_LOCATION) == 1
    assert rows.count("reference/cli-backends/SKILL.md") == 1
    for location in (
        "reference/inspection/SKILL.md",
        "reference/forensics/SKILL.md",
        "reference/dispatch-ledger/SKILL.md",
        "reference/cleanup/SKILL.md",
        "shell-manual",
        "lingtai-agent daemon --help",
    ):
        assert location in table, location
    for anchor in (
        "daemon-manual#max-turns",
        "daemon-manual#manager-pool-size",
        "daemon-manual#system-prompt-budget-chars",
        "daemon-manual#timeout",
    ):
        assert text.count(anchor) == 1, anchor


def test_router_admits_builtin_sibling_without_moving():
    # The router keeps its historical cli-backends path but must say the
    # built-in backend's page lives under it too.
    text = ROUTER.read_text(encoding="utf-8")
    assert "historical" in text
    assert ROUTER.parent.name == "cli-backends", "router must not be renamed/moved"


def test_router_routing_table_points_to_lingtai_child():
    text = ROUTER.read_text(encoding="utf-8")
    assert "## Routing table" in text
    table_rows = [
        line for line in text.splitlines()
        if line.startswith("|") and CHILD_LOCATION in line
    ]
    assert table_rows, "routing table must map built-in backend needs to the child"


def test_lingtai_child_frontmatter_and_location():
    assert CHILD.is_file()
    meta = _frontmatter(CHILD)
    assert meta["name"] == "daemon-backend-lingtai"
    assert meta["description"].strip()


def test_lingtai_child_routes_to_live_authorities():
    body = _body(CHILD)
    # The page routes to current authorities instead of restating rules.
    for phrase in (
        "daemon-manual",
        'system(action="presets")',
        "reference/substrate-manual/SKILL.md",
        "CONTRACT.md",
        "auto-inherited",
        "one-run `mcp` registrations",
        "daemon_common",
        'finish(status="done")',
        "context_token_limit",
        "manifest.llm.context_limit",
        "native LingTai LLM providers",
        "mimocode",
        "non-fatal",
        "hard failure",
    ):
        assert phrase in body, phrase


def test_lingtai_child_declares_no_cli_flag_surface():
    body = _body(CHILD)
    # Built-in backend: no wrapped CLI, and backend_options is ignored.
    assert "in-process" in body
    assert "`backend_options` is ignored" in body
    assert "no flag surface" in body
    # No invented per-run argv artifacts for this backend.
    assert "no reserved flags" in body


def test_lingtai_child_has_exactly_one_example_with_explicit_surface():
    body = _body(CHILD)
    fences = re.findall(r"```jsonc?\n(.*?)```", body, re.DOTALL)
    assert len(fences) == 1, "the child carries exactly one compact example"
    example = fences[0]
    assert '"backend": "lingtai"' in example
    for field in ('"preset"', '"tools"', '"skills"', '"mcp"'):
        assert field in example, field
    assert '"backend_options"' not in example
