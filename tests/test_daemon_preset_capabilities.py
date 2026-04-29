"""Tests for preset-driven capability surface on daemon emanations.

When a per-task preset is given, the emanation's tool surface comes from
the preset's manifest.capabilities (instantiated in a sandbox with
expand_inherit against the preset's LLM), unioned with the parent's MCP
tools, minus the EMANATION_BLACKLIST. When omitted, the parent's
currently registered surface is used (existing behavior).
"""
import json
import queue
from unittest.mock import MagicMock, patch

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.llm.base import FunctionSchema


def _make_agent(tmp_path, capabilities=None, presets_dir=None):
    from lingtai.agent import Agent
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=capabilities or ["daemon"],
        config=AgentConfig(),
    )
    if presets_dir is not None:
        agent._read_init = lambda: {
            "manifest": {
                "preset": {"active": "mock", "default": "mock",
                           "path": str(presets_dir)},
                "llm": {"provider": "mock", "model": "mock-model"},
            }
        }
    return agent


def _write_preset(presets_dir, name, capabilities, provider="deepseek",
                  model="deepseek-v3", api_key_env="DEEPSEEK_API_KEY"):
    preset = {
        "name": name,
        "description": f"{name} preset",
        "manifest": {
            "llm": {
                "provider": provider,
                "model": model,
                "api_key": None,
                "api_key_env": api_key_env,
            },
            "capabilities": capabilities,
        },
    }
    (presets_dir / f"{name}.json").write_text(json.dumps(preset))


# ---------------------------------------------------------------------------
# Sandbox unit tests
# ---------------------------------------------------------------------------

def test_sandbox_captures_add_tool_calls():
    from lingtai.core.daemon._capability_sandbox import _CapabilitySandbox

    parent = MagicMock()
    sandbox = _CapabilitySandbox(parent)

    sandbox.add_tool("foo", schema={"type": "object"},
                     handler=lambda args: {"ok": True}, description="foo desc")
    sandbox.add_tool("bar", schema={"type": "object"},
                     handler=lambda args: {"ok": True})

    assert "foo" in sandbox.schemas
    assert "bar" in sandbox.schemas
    assert isinstance(sandbox.schemas["foo"], FunctionSchema)
    assert sandbox.schemas["foo"].description == "foo desc"
    assert callable(sandbox.handlers["foo"])


def test_sandbox_forwards_unknown_attrs_to_parent():
    from lingtai.core.daemon._capability_sandbox import _CapabilitySandbox

    parent = MagicMock()
    parent._working_dir = "/tmp/x"
    parent._log = MagicMock()

    sandbox = _CapabilitySandbox(parent)
    # Read-through to parent
    assert sandbox._working_dir == "/tmp/x"
    sandbox._log("event", x=1)
    parent._log.assert_called_once_with("event", x=1)


def test_sandbox_does_not_pollute_parent_tool_registry():
    """Most important property: the parent's _tool_handlers / _tool_schemas
    must remain unchanged after sandbox add_tool calls."""
    from lingtai.core.daemon._capability_sandbox import _CapabilitySandbox

    parent = MagicMock()
    parent._tool_handlers = {}
    parent._tool_schemas = []
    sandbox = _CapabilitySandbox(parent)

    sandbox.add_tool("foo", schema={}, handler=lambda a: {})
    assert parent._tool_handlers == {}
    assert parent._tool_schemas == []


# ---------------------------------------------------------------------------
# _instantiate_preset_capabilities tests
# ---------------------------------------------------------------------------

def test_instantiate_preset_capabilities_returns_schemas_and_handlers(tmp_path):
    """Preset's capabilities (e.g. 'file' group) instantiate into the sandbox."""
    agent = _make_agent(tmp_path, ["daemon"])  # NOTE: parent has only daemon
    mgr = agent.get_capability("daemon")
    # File group expands to read/write/edit/glob/grep
    schemas, handlers = mgr._instantiate_preset_capabilities(
        {"file": {}},
        {"provider": "mock", "model": "mock"},
    )
    # Each file sub-capability should register its tool
    for name in ("read", "write", "edit", "glob", "grep"):
        assert name in schemas, f"{name} not registered"
        assert name in handlers, f"{name} handler missing"


def test_instantiate_unknown_capability_raises_value_error(tmp_path):
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    try:
        mgr._instantiate_preset_capabilities(
            {"nonsense_capability": {}},
            {"provider": "mock", "model": "mock"},
        )
    except ValueError as e:
        assert "nonsense_capability" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_instantiate_skips_blacklisted_capabilities(tmp_path):
    """daemon/avatar/psyche/library in a preset's capabilities are skipped
    (not instantiated, no error)."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    schemas, handlers = mgr._instantiate_preset_capabilities(
        {"daemon": {}, "avatar": {}, "read": {}},
        {"provider": "mock", "model": "mock"},
    )
    assert "daemon" not in schemas
    assert "avatar" not in schemas
    assert "read" in schemas


def test_instantiate_resolves_inherit_against_preset_llm(tmp_path):
    """provider:'inherit' in a capability kwarg gets the preset's LLM, not
    the parent's. We check this by giving the parent a 'mock' provider but
    the preset a 'gemini' provider — the resolved capability must see gemini.
    """
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    # Capture what setup_capability is called with
    captured = {}

    def fake_setup(target, name, **kwargs):
        captured[name] = kwargs

    with patch("lingtai.capabilities.setup_capability", side_effect=fake_setup):
        mgr._instantiate_preset_capabilities(
            {"web_search": {"provider": "inherit"}},
            {"provider": "gemini", "model": "gemini-pro",
             "api_key_env": "GEMINI_API_KEY"},
        )

    assert captured.get("web_search", {}).get("provider") == "gemini"
    # api credentials inherited too
    assert captured["web_search"].get("api_key_env") == "GEMINI_API_KEY"


# ---------------------------------------------------------------------------
# _build_tool_surface integration with preset_surface
# ---------------------------------------------------------------------------

def test_build_tool_surface_with_preset_uses_preset_capabilities(tmp_path):
    """Parent has only daemon; preset has 'file' — the emanation can request
    'file' tools and they resolve from the preset's surface."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    preset_schemas, preset_handlers = mgr._instantiate_preset_capabilities(
        {"file": {}},
        {"provider": "mock", "model": "mock"},
    )
    schemas, dispatch = mgr._build_tool_surface(
        ["file"],
        preset_surface=(preset_schemas, preset_handlers),
    )
    names = {s.name for s in schemas}
    # Parent didn't have these — they came from the preset
    assert "read" in names
    assert "write" in names
    assert "grep" in names
    # Handlers wired up
    assert "read" in dispatch


def test_build_tool_surface_with_preset_unknown_tool_raises(tmp_path):
    """Even with a preset, a tool name not in (preset ∪ parent MCP) raises."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    preset_schemas, preset_handlers = mgr._instantiate_preset_capabilities(
        {"file": {}},  # provides read/write/edit/glob/grep
        {"provider": "mock", "model": "mock"},
    )
    try:
        mgr._build_tool_surface(
            ["bogus_tool"],
            preset_surface=(preset_schemas, preset_handlers),
        )
    except ValueError as e:
        assert "bogus_tool" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_build_tool_surface_omitted_preset_uses_parent(tmp_path):
    """Regression: when preset_surface is None, parent's surface is used
    exactly like before."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    schemas, dispatch = mgr._build_tool_surface(["file"])
    names = {s.name for s in schemas}
    assert "read" in names
    assert "grep" in names
    # And the dispatch is the parent's actual handler
    assert dispatch["read"] is agent._tool_handlers["read"]


# ---------------------------------------------------------------------------
# End-to-end through _handle_emanate
# ---------------------------------------------------------------------------

def test_emanate_with_preset_instantiates_caps_for_emanation(tmp_path,
                                                              monkeypatch):
    """Parent has only ['daemon']; preset declares 'file'. Emanation can
    request 'file' tools and the daemon spawns successfully."""
    import lingtai.preset_connectivity as preset_connectivity
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(presets_dir, "thinker", capabilities={"file": {}})

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "scan files", "tools": ["file"], "preset": "thinker"},
        ]})

    assert result["status"] == "dispatched", result.get("message")
    # Folder was created — preset surface was satisfied
    daemons_dir = agent._working_dir / "daemons"
    assert daemons_dir.is_dir()
    assert len(list(daemons_dir.iterdir())) == 1


def test_emanate_preset_unknown_capability_refuses_batch(tmp_path, monkeypatch):
    import lingtai.preset_connectivity as preset_connectivity
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(presets_dir, "broken",
                  capabilities={"nonsense_capability_xyz": {}})

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "x", "tools": ["read"], "preset": "broken"},
        ]})

    assert result["status"] == "error"
    assert "broken" in result["message"]
    assert "nonsense_capability_xyz" in result["message"]


def test_emanate_preset_does_not_pollute_parent_tool_registry(tmp_path,
                                                                monkeypatch):
    """After a preset-driven emanation is scheduled, the parent's tool
    registry is unchanged — no preset tools leaked into the parent."""
    import lingtai.preset_connectivity as preset_connectivity
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset(presets_dir, "thinker", capabilities={"file": {}})

    agent = _make_agent(tmp_path, ["daemon"], presets_dir=presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    pre_handlers = set(agent._tool_handlers.keys())
    pre_schemas = {s.name for s in agent._tool_schemas}

    with patch.object(preset_connectivity, "_probe_host", return_value=12.5):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "x", "tools": ["file"], "preset": "thinker"},
        ]})
    assert result["status"] == "dispatched"

    # Parent unchanged
    assert set(agent._tool_handlers.keys()) == pre_handlers
    assert {s.name for s in agent._tool_schemas} == pre_schemas
