from __future__ import annotations

from lingtai.tools.mcp import get_description as mcp_description
from lingtai.tools.mcp import get_schema as mcp_schema
from lingtai.tools.psyche import get_description as psyche_description


def test_psyche_description_is_an_explicit_signpost() -> None:
    desc = psyche_description()
    assert desc.startswith("SIGNPOST ONLY:")
    # The four durable domains it routes to, named in the model-facing text.
    for domain in ("pad", "lingtai", "knowledge", "skills"):
        assert domain in desc
    # Every action returns a manual and nothing else.
    assert "never authors, edits, pins, installs, rescans, or loads anything" in desc
    assert "file.write" in desc and "file.edit" in desc
    assert "context.rebuild" in desc


def test_retired_domain_roots_expose_no_tool_description() -> None:
    """The four former public roots are gone, with no compatibility surface."""
    import importlib

    for pkg in ("pad", "lingtai", "knowledge", "skills"):
        module = importlib.import_module(f"lingtai.tools.{pkg}")
        assert not hasattr(module, "get_description"), pkg
        assert not hasattr(module, "get_schema"), pkg
        assert not hasattr(module, "handle"), pkg


def test_mcp_description_and_actions_are_explicit_signposts() -> None:
    assert mcp_description().startswith("SIGNPOST ONLY:")
    assert "does not register, activate, configure, or troubleshoot MCP servers" in mcp_description()
    assert "`info` only re-reads the registry" in mcp_description()
    assert "`settings` shows MCP-owned configuration" in mcp_description()
    assert "`manual` returns the mcp-manual body" in mcp_description()
    schema = mcp_schema()
    prop = schema["properties"]["action"]
    # The bounded SHOW child is inserted before the reserved manual child.
    assert prop["enum"] == ["info", "settings", "manual"]
    assert schema["required"] == ["action", "input", "reasoning"]
    assert [b["title"] for b in schema["properties"]["input"]["anyOf"]] == [
        "info input",
        "settings inventory input",
        "manual input",
    ]
    action = prop["description"]
    assert "info: signpost-only action" in action
    assert "without the manual body" in action
    assert "settings: show the MCP-owned init.addons" in action
    assert "manual: return only the mcp-manual skill body" in action
    assert "No action mutates MCP configuration" in action
