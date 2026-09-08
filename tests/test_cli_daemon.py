"""``lingtai-agent daemon`` CLI surface.

The engine itself is covered by ``test_daemon*.py``; these tests pin the CLI's
own guardrails — what it refuses, what it will not spawn, and what it must not
write — plus the fact that a dispatch reaches the unmodified engine.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_preset(tmp_path: Path, name: str, *, provider: str = "anthropic",
                  model: str = "preset-model",
                  capabilities: dict | None = None) -> str:
    """Write a loadable preset file and return its path as a string."""
    library = tmp_path / "presets"
    library.mkdir(parents=True, exist_ok=True)
    path = library / f"{name}.json"
    path.write_text(json.dumps({
        "name": name,
        "description": {"summary": f"{name} preset"},
        "manifest": {
            "llm": {
                "provider": provider,
                "model": model,
                "api_key": "preset-key",
                "base_url": None,
            },
            "capabilities": {"file": {}} if capabilities is None else capabilities,
        },
    }), encoding="utf-8")
    return str(path)


def _write_agent_dir(tmp_path: Path, *, allowed: list[str] | None = None,
                     preset_block: object | None = None,
                     capabilities: dict | None = None,
                     disable: list[str] | None = None,
                     env_file: str | None = None,
                     extra_manifest: dict | None = None) -> Path:
    """Create an agent working directory with a schema-valid init.json.

    Schema-valid matters now that the CLI reads init.json through the canonical
    reader: `manifest.preset`, when present, must carry `active`/`default`/
    non-empty `allowed`, and `active` must point at a loadable preset. Callers
    that only want an allowlist get one built around a real preset file.
    """
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "agent_name": "cli-daemon-agent",
        "language": "en",
        "llm": {
            "provider": "anthropic",
            "model": "test-model",
            "api_key": "test-key",
            "base_url": None,
        },
        "capabilities": {} if capabilities is None else capabilities,
    }
    if disable is not None:
        manifest["disable"] = disable
    if preset_block is not None:
        manifest["preset"] = preset_block
    elif allowed is not None:
        home = _write_preset(tmp_path, "home")
        manifest["preset"] = {
            "active": home,
            "default": home,
            "allowed": [home, *allowed],
        }
    if extra_manifest:
        manifest.update(extra_manifest)
    data: dict = {"manifest": manifest, "covenant": "", "pad": "", "lingtai": ""}
    if env_file is not None:
        data["env_file"] = env_file
    (agent_dir / "init.json").write_text(json.dumps(data), encoding="utf-8")
    return agent_dir


def _write_tasks(tmp_path: Path, payload: object, name: str = "tasks.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_cli(monkeypatch, argv: list[str]) -> int:
    """Invoke ``lingtai.cli.main`` and return the process exit code (0 = ok)."""
    from lingtai.cli import main

    monkeypatch.setattr(sys, "argv", ["lingtai-agent", *argv])
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture
def no_spawn(monkeypatch):
    """Record every detached spawn the engine attempts instead of performing it.

    Patching at ``_spawn_detached_lingtai_run`` keeps the whole CLI → envelope
    → ``_handle_emanate`` path (validation, preset gate, run-dir creation,
    prompt build) live, and stops exactly at the process boundary.
    """
    from lingtai.tools.daemon import DaemonManager

    spawns: list[dict] = []

    def _record(self, run_dir, **kwargs):
        spawns.append({"run_dir": run_dir, **kwargs})

    monkeypatch.setattr(DaemonManager, "_spawn_detached_lingtai_run", _record)
    return spawns


# ---------------------------------------------------------------------------
# Tasks-file validation
# ---------------------------------------------------------------------------


def test_emanate_refuses_missing_tasks_file(tmp_path, monkeypatch, capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    code = _run_cli(monkeypatch, [
        "daemon", "emanate",
        "--tasks", str(tmp_path / "absent.json"),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    assert "does not exist" in capsys.readouterr().err
    assert no_spawn == []


def test_emanate_refuses_empty_tasks_file(tmp_path, monkeypatch, capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(empty),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    assert "is empty" in capsys.readouterr().err
    assert no_spawn == []


def test_emanate_refuses_tasks_file_with_no_tasks(tmp_path, monkeypatch, capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {"tasks": []})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    assert "no tasks" in capsys.readouterr().err
    assert no_spawn == []


@pytest.mark.parametrize("payload,fragment", [
    ({"tasks": [{"task": "x", "tools": ["file"]}], "nope": 1}, "unsupported field"),
    ({"tasks": [{"task": "x", "tools": ["file"], "bogus": 1}]}, "unsupported field"),
    ({"tasks": [{"tools": ["file"]}]}, "tasks[0].task is required"),
    ({"tasks": [{"task": "x"}]}, "tasks[0].tools is required"),
    ({"tasks": ["not-an-object"]}, "tasks[0] must be object"),
])
def test_emanate_refuses_off_schema_payloads(tmp_path, monkeypatch, capsys, no_spawn,
                                             payload, fragment):
    """Structural checks read the daemon tool's own emanate schema."""
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, payload)
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    assert fragment in capsys.readouterr().err
    assert no_spawn == []


def test_emanate_accepts_bare_task_array(tmp_path, monkeypatch, capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, [{"task": "bare array form", "tools": ["file"]}])
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["count"] == 1
    assert no_spawn == []


# ---------------------------------------------------------------------------
# --owner-dir (legacy spelling --agent-dir)
# ---------------------------------------------------------------------------


def test_emanate_requires_owner_dir(tmp_path, monkeypatch, capsys, no_spawn):
    tasks = _write_tasks(tmp_path, {"tasks": [{"task": "x", "tools": ["file"]}]})
    code = _run_cli(monkeypatch, ["daemon", "emanate", "--tasks", str(tasks)])
    assert code == 2  # argparse usage error
    assert "--owner-dir" in capsys.readouterr().err
    assert no_spawn == []


def test_emanate_refuses_agent_dir_without_init_json(tmp_path, monkeypatch, capsys, no_spawn):
    bare = tmp_path / "not-an-agent"
    bare.mkdir()
    tasks = _write_tasks(tmp_path, {"tasks": [{"task": "x", "tools": ["file"]}]})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(bare), "--yes",
    ])
    assert code == 1
    assert "init.json" in capsys.readouterr().err
    assert no_spawn == []


# ---------------------------------------------------------------------------
# --yes gate
# ---------------------------------------------------------------------------


def test_emanate_without_yes_previews_and_spawns_nothing(tmp_path, monkeypatch,
                                                        capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "Summarize docs into notes.md", "tools": ["file"]},
        {"task": "Second task", "tools": ["file", "shell"]},
    ]})
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ]) == 0

    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    assert preview["status"] == "preview"
    assert preview["dispatched"] is False
    assert preview["owner_dir"] == str(agent_dir)
    assert preview["agent_dir"] == str(agent_dir)  # legacy machine-readable key
    assert preview["count"] == 2
    assert preview["backend"] == "lingtai"
    assert [t["tools"] for t in preview["tasks"]] == [["file"], ["file", "shell"]]
    assert "--yes" in captured.err

    assert no_spawn == []
    assert not (agent_dir / "daemons").exists()


def test_emanate_with_yes_dispatches_through_the_engine(tmp_path, monkeypatch,
                                                        capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "Summarize docs into notes.md", "tools": ["file"]},
    ]})
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    # The daemon tool's own emanate result shape, verbatim.
    assert result["status"] == "dispatched"
    assert result["count"] == 1
    assert len(result["ids"]) == 1
    assert result["group_id"]
    assert "handoff" in result

    assert len(no_spawn) == 1
    assert no_spawn[0]["task"] == "Summarize docs into notes.md"
    assert (agent_dir / "daemons").is_dir()


def test_emanate_env_file_budget_overrides_daemon_json(tmp_path, monkeypatch,
                                                       capsys, no_spawn):
    """A budget override in the agent's env_file reaches manager construction.

    Regression: dispatch used to build the ``DaemonManager`` before the lazy
    ``service`` read loaded the configured ``env_file``, so a valid
    ``LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS`` configured there lost to
    ``daemon/daemon.json`` on the CLI path only.
    """
    # setenv-then-delenv records the key with monkeypatch so the mid-run
    # load_env_file write is removed at teardown; the run itself starts with
    # no inherited process value masking the env_file source.
    monkeypatch.setenv("LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS", "sentinel")
    monkeypatch.delenv("LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS")

    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS=26000\n", encoding="utf-8",
    )
    agent_dir = _write_agent_dir(tmp_path, env_file=str(env_file))
    (agent_dir / "daemon").mkdir()
    (agent_dir / "daemon" / "daemon.json").write_text(
        json.dumps({"system_prompt_budget_chars": 25_000}), encoding="utf-8",
    )

    import lingtai.tools.daemon as daemon_tool

    budgets: list[int] = []
    real_setup = daemon_tool.setup

    def _recording_setup(agent, **kwargs):
        mgr = real_setup(agent, **kwargs)
        budgets.append(mgr._system_prompt_budget_chars)
        return mgr

    monkeypatch.setattr(daemon_tool, "setup", _recording_setup)

    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "Summarize docs into notes.md", "tools": ["file"]},
    ]})
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ]) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "dispatched"
    assert len(no_spawn) == 1
    assert budgets == [26_000]


def test_emanate_backend_flag_overrides_the_file(tmp_path, monkeypatch, capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {
        "tasks": [{"task": "x", "tools": ["file"]}],
        "backend": "lingtai",
    })
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--backend", "codex",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "codex"
    assert no_spawn == []


# ---------------------------------------------------------------------------
# P1-3 regression: the preview validates against the canonical emanate schema
#
# These all used to print a clean preview and exit 0, because every bound below
# lived in the family dispatcher, which only runs under --yes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload,fragment", [
    ({"tasks": [{"task": "x", "tools": ["file"]}], "backend": "not-a-backend"},
     "is not one of"),
    ({"tasks": [{"task": "x", "tools": ["file"]}], "max_turns": 0},
     "max_turns must be >= 1"),
    ({"tasks": [{"task": "x", "tools": ["file"]}], "max_turns": 999999},
     "max_turns must be <= "),
    ({"tasks": [{"task": "x", "tools": ["file"]}], "timeout": 1},
     "timeout must be >= 5"),
    ({"tasks": [{"task": "x", "tools": ["file"], "context_token_limit": 0}]},
     "context_token_limit must be >= 1"),
    ({"tasks": [{"task": "x", "tools": ["file"]}], "max_turns": "many"},
     "max_turns must be integer or null"),
    ({"tasks": [{"task": "x", "tools": [7]}]},
     "tasks[0].tools[0] must be string"),
    ({"tasks": [{"task": "x", "tools": ["file"], "preset": 7}]},
     "tasks[0].preset must be string"),
    ({"tasks": [{"task": "x", "tools": ["file"], "skills": "not-a-list"}]},
     "tasks[0].skills must be array"),
])
def test_preview_refuses_schema_violations_without_yes(tmp_path, monkeypatch, capsys,
                                                       no_spawn, payload, fragment):
    """Every bound the tool enforces at dispatch is enforced at preview too."""
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, payload)
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "does not match the daemon emanate schema" in err
    assert fragment in err
    assert no_spawn == []
    assert not (agent_dir / "daemons").exists()


def test_preview_refuses_the_reviewers_exact_payload(tmp_path, monkeypatch, capsys, no_spawn):
    """The reported reproducer: four violations at once, previously exit 0."""
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {
        "tasks": [{"task": "x", "tools": ["file"], "context_token_limit": 0}],
        "backend": "not-a-backend",
        "max_turns": 0,
        "timeout": 1,
    })
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ])
    assert code == 1
    err = capsys.readouterr().err
    # All four are reported together rather than one per run.
    assert "is not one of" in err
    assert "max_turns must be >= 1" in err
    assert "timeout must be >= 5" in err
    assert "context_token_limit must be >= 1" in err
    assert no_spawn == []


def test_backend_flag_is_held_to_the_same_enum(tmp_path, monkeypatch, capsys, no_spawn):
    """--backend overrides the file, so it needs the same enum check."""
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {"tasks": [{"task": "x", "tools": ["file"]}]})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--backend", "not-a-backend",
    ])
    assert code == 1
    assert "--backend 'not-a-backend' is not one of" in capsys.readouterr().err
    assert no_spawn == []


def test_schema_bounds_are_read_from_the_tool_not_restated():
    """The validator's numbers come from ``_tool_family``, so they cannot drift."""
    from lingtai.tools.daemon import _BACKEND_SCHEMA_ENUM
    from lingtai.tools.daemon._tool_family import _emanate_input_schema
    from lingtai.cli_daemon import CliDaemonError, _validate_emanate_input

    schema = _emanate_input_schema(list(_BACKEND_SCHEMA_ENUM))
    ceiling = schema["properties"]["max_turns"]["maximum"]
    floor = schema["properties"]["timeout"]["minimum"]

    ok = {"tasks": [{"task": "x", "tools": []}], "max_turns": ceiling, "timeout": floor}
    assert _validate_emanate_input(dict(ok))

    with pytest.raises(CliDaemonError, match=f"must be <= {ceiling}"):
        _validate_emanate_input({**ok, "max_turns": ceiling + 1})
    with pytest.raises(CliDaemonError, match=f"must be >= {floor}"):
        _validate_emanate_input({**ok, "timeout": floor - 1})


def test_validator_fails_loudly_on_an_unknown_schema_keyword():
    """A new schema keyword must break the interpreter, not be ignored."""
    from lingtai.cli_daemon import CliDaemonError, _check_schema

    with pytest.raises(CliDaemonError, match="unsupported keyword"):
        _check_schema({}, {"type": "object", "dependentRequired": {}}, "", [])


# ---------------------------------------------------------------------------
# P1-1 regression: the tool surface respects effective capability policy
# ---------------------------------------------------------------------------


def test_disabled_tool_refuses_the_batch(tmp_path, monkeypatch, capsys, no_spawn):
    """``manifest.disable`` is policy; a task cannot route around it."""
    agent_dir = _write_agent_dir(tmp_path, disable=["shell"])
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "run something", "tools": ["shell"]},
    ]})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "this agent does not grant" in err
    assert "whole batch is refused" in err
    assert no_spawn == []
    assert not (agent_dir / "daemons").exists()


def test_disabled_tool_refuses_the_whole_batch_including_allowed_siblings(
    tmp_path, monkeypatch, capsys, no_spawn,
):
    agent_dir = _write_agent_dir(tmp_path, disable=["shell"])
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "fine", "tools": ["file"]},
        {"task": "not fine", "tools": ["shell"]},
    ]})
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ]) == 1
    assert "tasks[1] requests tool 'shell'" in capsys.readouterr().err
    assert no_spawn == []


def test_disabled_tool_is_refused_before_yes(tmp_path, monkeypatch, capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path, disable=["shell"])
    tasks = _write_tasks(tmp_path, {"tasks": [{"task": "x", "tools": ["shell"]}]})
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ]) == 1
    assert "this agent does not grant" in capsys.readouterr().err
    assert no_spawn == []


def test_engine_also_refuses_a_disabled_tool_when_the_cli_gate_is_bypassed(tmp_path):
    """The CLI gate is defense in depth; the surface itself must fail closed.

    Even calling the dispatcher directly — no CLI gate — a disabled tool is
    never registered on the facade, so ``_build_tool_surface`` refuses.
    """
    from lingtai.cli_daemon import _CliDaemonAgent, _dispatch_through_tool_family

    agent_dir = _write_agent_dir(tmp_path, disable=["shell"])
    agent = _CliDaemonAgent.for_dispatch(agent_dir)
    agent.install_tool_surface({"shell", "file"})

    assert "shell" not in {s.name for s in agent._tool_schemas}
    result = _dispatch_through_tool_family(agent, "emanate", {
        "tasks": [{"task": "x", "tools": ["shell"]}],
        "backend": "lingtai", "max_turns": None, "timeout": None,
    })
    assert result["status"] == "error"
    assert "Unknown tools for emanation" in result["message"]


def test_effective_capabilities_apply_core_defaults_and_disable(tmp_path):
    """The effective set is ``apply_core_defaults``, not the raw manifest."""
    from lingtai.cli_daemon import _CliDaemonAgent

    agent_dir = _write_agent_dir(
        tmp_path, capabilities={"shell": {"yolo": False}}, disable=["vision"],
    )
    granted = _CliDaemonAgent.for_dispatch(agent_dir).effective_capabilities()

    assert "vision" not in granted           # dropped by manifest.disable
    assert "file" in granted                 # core floor, never named in init
    assert granted["shell"] == {"yolo": False}  # authored kwargs win over the floor


def test_authored_capability_kwargs_reach_setup(tmp_path, monkeypatch):
    """A capability is instantiated with the agent's configuration, not defaults."""
    from lingtai.cli_daemon import _CliDaemonAgent
    import lingtai.tools.registry as registry

    agent_dir = _write_agent_dir(tmp_path, capabilities={"shell": {"yolo": False}})
    seen: dict = {}
    real = registry.setup_capability

    def _spy(collector, name, **kwargs):
        seen[name] = kwargs
        return real(collector, name, **kwargs)

    monkeypatch.setattr(registry, "setup_capability", _spy)
    _CliDaemonAgent.for_dispatch(agent_dir).install_tool_surface({"shell"})

    assert seen["shell"] == {"yolo": False}


@pytest.mark.parametrize("tool", ["email", "compact"])
def test_engine_provided_tools_are_not_refused_by_the_capability_gate(
    tmp_path, monkeypatch, capsys, no_spawn, tool,
):
    """The gate must not over-refuse names the engine supplies itself.

    ``email`` is auto-mounted as MCP and ``compact`` comes from the daemon
    intrinsic surface; neither is a capability in ``BUILTIN_TOOLS``, so the
    capability set is not their authority.
    """
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {"tasks": [{"task": "x", "tools": [tool]}]})
    _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ])
    assert "this agent does not grant" not in capsys.readouterr().err


def test_a_preset_task_is_not_gated_on_the_parent_capability_set(tmp_path, monkeypatch,
                                                                 capsys, no_spawn):
    """A preset brings its own sandbox, so the parent set is not its authority."""
    preset = _write_preset(tmp_path, "cheap", capabilities={"file": {}})
    agent_dir = _write_agent_dir(tmp_path, allowed=[preset], disable=["shell"])
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "x", "tools": ["shell"], "preset": preset},
    ]})
    # Reaches the engine (which owns preset-surface resolution) rather than
    # being refused by the parent-capability gate.
    _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ])
    assert "this agent does not grant" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Preset allowlist — fail closed
# ---------------------------------------------------------------------------


def test_disallowed_preset_refuses_the_whole_batch(tmp_path, monkeypatch, capsys, no_spawn):
    """One unauthorized preset refuses every task in the file, not just its own."""
    agent_dir = _write_agent_dir(tmp_path, allowed=[str(tmp_path / "ok.json")])
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "allowed sibling", "tools": ["file"]},
        {"task": "unauthorized", "tools": ["file"], "preset": str(tmp_path / "evil.json")},
    ]})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "not in this agent's allowed list" in err
    assert "whole batch is refused" in err
    assert no_spawn == []
    assert not (agent_dir / "daemons").exists()


def test_absent_allowlist_fails_closed(tmp_path, monkeypatch, capsys, no_spawn):
    """An agent with no preset block grants no preset."""
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "x", "tools": ["file"], "preset": str(tmp_path / "any.json")},
    ]})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    assert "not in this agent's allowed list" in capsys.readouterr().err
    assert no_spawn == []


@pytest.mark.parametrize("preset_block", [{}, {"allowed": []}, {"allowed": "oops"}])
def test_malformed_preset_block_fails_closed(tmp_path, monkeypatch, capsys,
                                             no_spawn, preset_block):
    """A malformed preset block is refused by the canonical init reader.

    This used to reach the CLI's own allowlist gate because init.json was read
    with a bare ``json.loads``. Reading through ``read_init`` means the schema
    rejects the block first — an earlier and stricter fail-closed point, but
    still a refusal with no dispatch, which is what matters.
    """
    agent_dir = _write_agent_dir(tmp_path, preset_block=preset_block)
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "x", "tools": ["file"], "preset": str(tmp_path / "any.json")},
    ]})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ])
    assert code == 1
    assert "is not usable" in capsys.readouterr().err
    assert no_spawn == []
    assert not (agent_dir / "daemons").exists()


def test_disallowed_preset_is_refused_before_yes(tmp_path, monkeypatch, capsys, no_spawn):
    """The gate runs at preview time too — a bad batch never looks dispatchable."""
    agent_dir = _write_agent_dir(tmp_path, allowed=[str(tmp_path / "ok.json")])
    tasks = _write_tasks(tmp_path, {"tasks": [
        {"task": "x", "tools": ["file"], "preset": str(tmp_path / "evil.json")},
    ]})
    code = _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ])
    assert code == 1
    assert "not in this agent's allowed list" in capsys.readouterr().err
    assert no_spawn == []


def test_engine_preset_gate_still_runs_when_the_cli_gate_is_bypassed(tmp_path, monkeypatch):
    """The CLI's own check is defense in depth, never the only gate."""
    from lingtai.cli_daemon import _CliDaemonAgent, _dispatch_through_tool_family

    agent_dir = _write_agent_dir(tmp_path, allowed=[str(tmp_path / "ok.json")])
    agent = _CliDaemonAgent.for_dispatch(agent_dir)
    agent.install_tool_surface({"file"})
    result = _dispatch_through_tool_family(agent, "emanate", {
        "tasks": [{"task": "x", "tools": ["file"], "preset": str(tmp_path / "evil.json")}],
        "backend": "lingtai",
        "max_turns": None,
        "timeout": None,
    })
    assert result["status"] == "error"
    assert "not in this agent's allowed list" in result["message"]
    assert not (agent_dir / "daemons").exists()


# ---------------------------------------------------------------------------
# P1-2 regression: dispatch uses the agent's effective config, not raw JSON
# ---------------------------------------------------------------------------


def test_active_preset_model_is_used_not_the_raw_init_llm(tmp_path, monkeypatch,
                                                          capsys, no_spawn):
    """A materialized preset decides the daemon's provider/model.

    Reading init.json with a bare ``json.loads`` skipped active-preset
    materialization, so the daemon launched on the *stale raw* llm block that
    the preset was supposed to replace.
    """
    from lingtai.cli_daemon import _CliDaemonAgent

    active = _write_preset(
        tmp_path, "minimax", provider="minimax", model="effective-model",
    )
    agent_dir = _write_agent_dir(tmp_path, preset_block={
        "active": active, "default": active, "allowed": [active],
    }, extra_manifest={
        "llm": {
            "provider": "anthropic",
            "model": "stale-raw-model",
            "api_key": "raw-key",
            "base_url": None,
        },
    })

    agent = _CliDaemonAgent.for_dispatch(agent_dir)
    assert agent._init_data["manifest"]["llm"]["model"] == "effective-model"
    assert agent._init_data["manifest"]["llm"]["provider"] == "minimax"
    assert agent.service.model == "effective-model"
    assert agent.service.provider == "minimax"

    # And it is the effective model that reaches the run directory.
    tasks = _write_tasks(tmp_path, {"tasks": [{"task": "x", "tools": ["file"]}]})
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ]) == 0
    capsys.readouterr()
    state = json.loads(
        (no_spawn[0]["run_dir"].path / "daemon.json").read_text(encoding="utf-8")
    )
    assert state["model"] == "effective-model"


def test_relative_env_file_resolves_under_agent_dir_not_cwd(tmp_path, monkeypatch):
    """``--agent-dir`` is the base for every relative path in init.json.

    ``resolve_paths`` is part of the canonical reader; skipping it meant a
    relative ``env_file`` was loaded from wherever the CLI happened to be
    invoked, so a daemon could boot with another directory's credentials — or
    none at all.
    """
    from lingtai.cli_daemon import _CliDaemonAgent

    agent_dir = _write_agent_dir(tmp_path, env_file="secrets.env", extra_manifest={
        "llm": {
            "provider": "anthropic",
            "model": "m",
            "api_key_env": "CLI_DAEMON_ENV_FILE_PROBE",
            "base_url": None,
        },
    })
    (agent_dir / "secrets.env").write_text(
        "CLI_DAEMON_ENV_FILE_PROBE=from-agent-dir\n", encoding="utf-8",
    )
    # A decoy with the same relative name next to the caller's CWD.
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "secrets.env").write_text(
        "CLI_DAEMON_ENV_FILE_PROBE=from-cwd\n", encoding="utf-8",
    )
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("CLI_DAEMON_ENV_FILE_PROBE", raising=False)

    agent = _CliDaemonAgent.for_dispatch(agent_dir)
    assert agent._init_data["env_file"] == str(agent_dir / "secrets.env")
    assert agent.service.api_key == "from-agent-dir"


def test_jsonc_init_is_parsed(tmp_path):
    """The canonical reader accepts JSONC; ``json.loads`` did not."""
    from lingtai.cli_daemon import _CliDaemonAgent

    agent_dir = _write_agent_dir(tmp_path)
    raw = (agent_dir / "init.json").read_text(encoding="utf-8")
    (agent_dir / "init.json").write_text(
        "// the canonical reader tolerates comments\n" + raw, encoding="utf-8",
    )
    assert _CliDaemonAgent.for_dispatch(agent_dir)._config.language == "en"


def test_invalid_init_refuses_dispatch(tmp_path, monkeypatch, capsys, no_spawn):
    """Schema validation now runs; an unusable init.json refuses the batch."""
    agent_dir = _write_agent_dir(tmp_path)
    (agent_dir / "init.json").write_text(
        json.dumps({"manifest": {"agent_name": "x"}}), encoding="utf-8",
    )
    tasks = _write_tasks(tmp_path, {"tasks": [{"task": "x", "tools": ["file"]}]})
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks),
        "--agent-dir", str(agent_dir), "--yes",
    ]) == 1
    assert "is not usable" in capsys.readouterr().err
    assert no_spawn == []


def test_reading_effective_config_writes_nothing(tmp_path):
    """Unlike ``cli.load_init``, the CLI never publishes a resolved manifest."""
    from lingtai.cli_daemon import _CliDaemonAgent

    active = _write_preset(tmp_path, "p")
    agent_dir = _write_agent_dir(tmp_path, preset_block={
        "active": active, "default": active, "allowed": [active],
    })
    before = (agent_dir / "init.json").read_bytes()

    _CliDaemonAgent.for_dispatch(agent_dir)

    assert (agent_dir / "init.json").read_bytes() == before
    assert not (agent_dir / "system" / "manifest.resolved.json").exists()


def test_inspection_does_not_require_a_usable_init(tmp_path, monkeypatch, capsys):
    """A broken agent must still be inspectable — refusing to list is backwards."""
    agent_dir = _write_agent_dir(tmp_path)
    _seed_run_dir(agent_dir)
    (agent_dir / "init.json").write_text(
        json.dumps({"manifest": {"agent_name": "broken"}}), encoding="utf-8",
    )
    assert _run_cli(monkeypatch, ["daemon", "list", "--agent-dir", str(agent_dir)]) == 0
    assert "em-1" in capsys.readouterr().out


def test_reclaim_dispatches_through_the_daemon_family(tmp_path, monkeypatch, capsys):
    """CLI reclaim is the tool-family reclaim surface, not a second control path."""
    agent_dir = _write_agent_dir(tmp_path)
    calls: list[tuple[Path, str, dict]] = []

    def fake_dispatch(agent, action, action_input):
        calls.append((agent._working_dir, action, action_input))
        return {"status": "reclaimed", "cancelled": 2}

    monkeypatch.setattr("lingtai.cli_daemon._dispatch_through_tool_family", fake_dispatch)

    assert _run_cli(monkeypatch, [
        "daemon", "reclaim", "--agent-dir", str(agent_dir),
    ]) == 0
    assert calls == [(agent_dir, "reclaim", {})]
    assert json.loads(capsys.readouterr().out) == {"status": "reclaimed", "cancelled": 2}


# ---------------------------------------------------------------------------
# Read-only list / check
# ---------------------------------------------------------------------------


#: A PID that is not this process and is overwhelmingly unlikely to be alive —
#: what a run directory left behind by a since-exited agent looks like.
_DEAD_PARENT_PID = 999_999


def _seed_run_dir(agent_dir: Path, *, handle: str = "em-1",
                  task: str = "seeded task", state: str = "done") -> Path:
    """Create a well-formed daemon run directory through ``DaemonRunDir``.

    Written by the real writer and then explicitly appended to the dispatch
    ledger, so read-only list/check exercise only accepted new-format
    membership. The record is deliberately left with a dead owner: marker-only
    startup recovery must not scan it without a recovery marker.
    """
    from lingtai.tools.daemon.run_dir import DaemonRunDir

    run_dir = DaemonRunDir(
        parent_working_dir=agent_dir,
        handle=handle,
        run_id=f"{handle}-20260813-101010-abcdef",
        task=task,
        tools=["file"],
        model="test-model",
        max_turns=5,
        timeout_s=60.0,
        parent_addr=agent_dir.name,
        parent_pid=_DEAD_PARENT_PID,
        system_prompt="prompt",
        backend="lingtai",
    )
    if state != "running":
        run_dir.update_state(state=state, finished_at="2026-08-13T10:11:10Z")
    from lingtai.kernel.daemon_dispatch import append_dispatch
    append_dispatch(agent_dir, run_id=run_dir.run_id, created_at=run_dir.state_snapshot()["started_at"])
    return run_dir.path


def test_list_prints_a_status_table(tmp_path, monkeypatch, capsys):
    agent_dir = _write_agent_dir(tmp_path)
    _seed_run_dir(agent_dir)
    assert _run_cli(monkeypatch, ["daemon", "list", "--agent-dir", str(agent_dir)]) == 0
    out = capsys.readouterr().out
    assert "STATUS" in out and "TASK" in out
    assert "em-1" in out
    assert "seeded task" in out


def test_list_status_filter(tmp_path, monkeypatch, capsys):
    agent_dir = _write_agent_dir(tmp_path)
    _seed_run_dir(agent_dir)
    assert _run_cli(monkeypatch, [
        "daemon", "list", "--status", "running", "--agent-dir", str(agent_dir),
    ]) == 0
    assert "no daemon runs" in capsys.readouterr().out


def test_list_cli_keeps_default_1000_and_forwards_explicit_1005(
    tmp_path, monkeypatch, capsys,
):
    """The public CLI keeps the engine default but exposes a larger page."""
    agent_dir = _write_agent_dir(tmp_path)
    for index in range(1005):
        run_path = _seed_run_dir(agent_dir, handle=f"em-{index}")
        state_path = run_path / "daemon.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        minute, second = divmod(index, 60)
        state["started_at"] = f"2026-08-13T10:{minute:02d}:{second:02d}Z"
        state_path.write_text(json.dumps(state), encoding="utf-8")

    assert _run_cli(monkeypatch, ["daemon", "list", "--agent-dir", str(agent_dir)]) == 0
    default_out = capsys.readouterr().out
    assert "1000 shown, 0 running" in default_out
    assert "em-1004" in default_out
    assert "em-0" not in default_out  # newest 1000, not the oldest five

    assert _run_cli(monkeypatch, [
        "daemon", "list", "--last", "1005", "--agent-dir", str(agent_dir),
    ]) == 0
    explicit_out = capsys.readouterr().out
    assert "1005 shown, 0 running" in explicit_out
    assert "em-0" in explicit_out
    assert "em-1004" in explicit_out


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_list_cli_last_is_strictly_positive(tmp_path, monkeypatch, capsys, value):
    agent_dir = _write_agent_dir(tmp_path)
    code = _run_cli(monkeypatch, [
        "daemon", "list", "--last", value, "--agent-dir", str(agent_dir),
    ])
    assert code == 2
    assert "--last" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["daemon", "list"],
    ["daemon", "check", "em-1"],
])
def test_list_and_check_write_nothing(tmp_path, monkeypatch, capsys, argv):
    """A stale, unnotified record must survive inspection byte-for-byte."""
    agent_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(agent_dir, state="running")
    before = (run_path / "daemon.json").read_bytes()

    assert _run_cli(monkeypatch, [*argv, "--agent-dir", str(agent_dir)]) == 0
    capsys.readouterr()

    assert (run_path / "daemon.json").read_bytes() == before
    assert not (agent_dir / ".notification").exists()


def test_full_manager_does_not_scan_unmarked_legacy_record(tmp_path):
    """Marker-only recovery preserves an unmarked run byte-for-byte at startup."""
    from lingtai.tools.daemon import DaemonManager
    from lingtai.cli_daemon import _CliDaemonAgent

    agent_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(agent_dir, state="running")
    before = (run_path / "daemon.json").read_bytes()

    DaemonManager(_CliDaemonAgent.for_dispatch(agent_dir))

    assert (run_path / "daemon.json").read_bytes() == before


def test_check_prints_a_snapshot(tmp_path, monkeypatch, capsys):
    agent_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(agent_dir)
    assert _run_cli(monkeypatch, [
        "daemon", "check", "em-1", "--agent-dir", str(agent_dir),
    ]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["id"] == "em-1"
    assert snapshot["state"] == "done"
    assert snapshot["path"] == str(run_path)


def test_check_unknown_id_exits_nonzero(tmp_path, monkeypatch, capsys):
    agent_dir = _write_agent_dir(tmp_path)
    code = _run_cli(monkeypatch, [
        "daemon", "check", "em-404", "--agent-dir", str(agent_dir),
    ])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


# ---------------------------------------------------------------------------
# P1-4 regression: inspection never rewrites durable state, whatever its shape
#
# The engine's ``_load_or_rebuild_daemon_state`` self-heals a daemon.json that
# is missing, unparseable, or stamped with an older data_version. That repair
# belongs to the owning agent; a CLI must observe without mutating.
# ---------------------------------------------------------------------------


def _damage(run_path: Path, kind: str) -> None:
    """Put a run directory into one of the three would-be-rebuild shapes."""
    daemon_json = run_path / "daemon.json"
    if kind == "missing":
        daemon_json.unlink()
    elif kind == "unparseable":
        daemon_json.write_text("{not json", encoding="utf-8")
    elif kind == "stale_version":
        state = json.loads(daemon_json.read_text(encoding="utf-8"))
        state["data_version"] = 0
        daemon_json.write_text(json.dumps(state), encoding="utf-8")
    else:  # pragma: no cover - guards the parametrization
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["missing", "unparseable", "stale_version"])
@pytest.mark.parametrize("argv", [["daemon", "list"], ["daemon", "check", "em-1"]])
def test_inspection_never_repairs_damaged_durable_state(tmp_path, monkeypatch, capsys,
                                                        kind, argv):
    agent_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(agent_dir)
    _damage(run_path, kind)
    daemon_json = run_path / "daemon.json"
    existed = daemon_json.exists()
    before = daemon_json.read_bytes() if existed else None

    _run_cli(monkeypatch, [*argv, "--agent-dir", str(agent_dir)])
    capsys.readouterr()

    assert daemon_json.exists() is existed, "inspection created daemon.json"
    if existed:
        assert daemon_json.read_bytes() == before, "inspection rewrote daemon.json"


@pytest.mark.parametrize("kind", ["missing", "unparseable", "stale_version"])
def test_list_reports_selected_damaged_state_without_repair(tmp_path, monkeypatch, capsys, kind):
    """Ledger-selected unreadable state is a warning, never reconstruction."""
    agent_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(agent_dir)
    _damage(run_path, kind)
    before = (run_path / "daemon.json").read_bytes() if (run_path / "daemon.json").exists() else None

    assert _run_cli(monkeypatch, ["daemon", "list", "--agent-dir", str(agent_dir)]) == 0
    captured = capsys.readouterr()
    if kind in {"missing", "unparseable"}:
        assert "no daemon runs" in captured.out
        assert "dispatch_ledger_daemon_state_unreadable" in captured.err
    else:
        assert "em-1" in captured.out
    daemon_json = run_path / "daemon.json"
    assert daemon_json.exists() is (before is not None)
    if before is not None:
        assert daemon_json.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "unparseable", "stale_version"])
def test_full_manager_does_not_repair_damaged_unmarked_or_ledger_state(tmp_path, kind):
    """The cutover intentionally removes automatic list/startup repair."""
    from lingtai.tools.daemon import DaemonManager
    from lingtai.cli_daemon import _CliDaemonAgent

    agent_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(agent_dir)
    _damage(run_path, kind)
    daemon_json = run_path / "daemon.json"
    existed = daemon_json.exists()
    before = daemon_json.read_bytes() if existed else None

    manager = DaemonManager(_CliDaemonAgent.for_dispatch(agent_dir))
    manager._handle_list()

    assert daemon_json.exists() is existed
    if existed:
        assert daemon_json.read_bytes() == before

def test_read_only_view_reads_only_ledger_selected_states(tmp_path):
    from lingtai.cli_daemon import _CliDaemonAgent, _ReadOnlyDaemonView

    agent_dir = _write_agent_dir(tmp_path)
    healthy = _seed_run_dir(agent_dir, handle="em-1")
    stale = _seed_run_dir(agent_dir, handle="em-2")
    _damage(stale, "stale_version")

    view = _ReadOnlyDaemonView(_CliDaemonAgent.for_inspection(agent_dir))
    result = view._handle_list()
    assert {e["run_id"] for e in result["emanations"]} == {healthy.name, stale.name}


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_backend_options_env_values_are_redacted_from_output(tmp_path, monkeypatch,
                                                             capsys, no_spawn):
    agent_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, {
        "tasks": [{
            "task": "x",
            "tools": ["file"],
            "backend_options": {"env": {"CLAUDE_CONFIG_DIR": "s3cr3t-value"}},
        }],
        "backend": "claude-p",
    })
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--agent-dir", str(agent_dir),
    ]) == 0
    out = capsys.readouterr().out
    assert "s3cr3t-value" not in out


def test_redaction_keeps_explicit_nulls_in_the_printed_shape():
    """A script reading `check` output must see null, not a missing key."""
    from lingtai.cli_daemon import _redact_preserving_nulls

    assert _redact_preserving_nulls({
        "result_path": None,
        "current_tool": None,
        "state": "done",
        "env": {"TOKEN": "s3cr3t"},
        "events": [{"detail": None, "event": "daemon_done"}],
    }) == {
        "result_path": None,
        "current_tool": None,
        "state": "done",
        "env": {"TOKEN": "<redacted>"},
        "events": [{"detail": None, "event": "daemon_done"}],
    }


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


def test_facade_takes_no_lease_and_writes_no_agent_identity(tmp_path):
    """The facade must never look like a second agent in the working dir."""
    from lingtai.cli_daemon import _CliDaemonAgent

    agent_dir = _write_agent_dir(tmp_path)
    agent = _CliDaemonAgent.for_dispatch(agent_dir)
    agent.install_tool_surface({"file"})

    assert {s.name for s in agent._tool_schemas} == {"file"}
    assert agent._config.language == "en"
    assert not (agent_dir / ".agent.heartbeat").exists()
    assert not (agent_dir / ".agent.lock").exists()
    assert not (agent_dir / ".agent.json").exists()


def test_facade_reads_the_sanitized_preset_allowlist(tmp_path):
    """``_read_preset_from_init`` is the live Agent's implementation, not a copy."""
    from lingtai.agent import Agent
    from lingtai.cli_daemon import _CliDaemonAgent

    home = _write_preset(tmp_path, "home")
    agent_dir = _write_agent_dir(tmp_path, preset_block={
        "allowed": [home, 7],
        "active": home,
        "default": home,
        "secret": "must-not-survive",
    })
    agent = _CliDaemonAgent.for_inspection(agent_dir)
    assert _CliDaemonAgent._read_preset_from_init is Agent._read_preset_from_init
    assert agent._read_preset_from_init() == {
        "allowed": [home], "active": home, "default": home,
    }


def test_facade_refuses_to_publish_terminal_notifications(tmp_path):
    """A CLI publish must fail so the pending receipt survives for the agent."""
    from lingtai.cli_daemon import _CliDaemonAgent, _ReadOnlyDaemonView

    agent_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(agent_dir)
    agent = _CliDaemonAgent.for_dispatch(agent_dir)
    view = _ReadOnlyDaemonView(agent)

    published = view._publish_daemon_notification(
        "em-1", status="done", text="body", run_path=run_path,
        run_state=json.loads((run_path / "daemon.json").read_text(encoding="utf-8")),
        idempotency_key="k",
    )
    assert published is False


def test_facade_service_is_lazy(tmp_path):
    """`list`/`check` must work with no resolvable credential."""
    from lingtai.cli_daemon import _CliDaemonAgent

    env_file = tmp_path / "agent.env"
    env_file.write_text("OTHER=1\n", encoding="utf-8")
    agent_dir = _write_agent_dir(tmp_path, env_file=str(env_file), extra_manifest={
        "llm": {
            "provider": "anthropic",
            "model": "m",
            "api_key_env": "NOPE_MISSING_CLI_DAEMON_KEY",
            "base_url": None,
        },
    })

    agent = _CliDaemonAgent.for_dispatch(agent_dir)
    assert agent._working_dir == agent_dir  # construction touched no credential
    with pytest.raises(ValueError):
        _ = agent.service


# ---------------------------------------------------------------------------
# Docs / description hint
# ---------------------------------------------------------------------------


def test_daemon_tool_description_routes_detail_to_the_manual():
    from lingtai.tools.daemon import get_description

    description = get_description()
    assert "Read the daemon manual before first use" in description
    assert "lingtai-agent daemon" not in description


def test_daemon_manual_routes_programmatic_use_to_current_help():
    manual = (
        Path(__file__).resolve().parents[1]
        / "src/lingtai/tools/daemon/manual/SKILL.md"
    ).read_text(encoding="utf-8")
    assert "## Programmatic use / CLI" in manual
    assert "lingtai-agent daemon --help" in manual
    assert "lingtai-agent daemon emanate" not in manual
    assert "lingtai-agent daemon list" not in manual
    assert "non-empty `contains` searches prompt-preview text" not in manual


# ---------------------------------------------------------------------------
# External owner (#1659): --owner-dir naming, no live Agent, owner-scoped state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--owner-dir", "--agent-dir"])
def test_owner_dir_and_legacy_agent_dir_share_one_destination(tmp_path, monkeypatch,
                                                             capsys, flag):
    """``--agent-dir`` survives only as a spelling of the same owner directory."""
    owner_dir = _write_agent_dir(tmp_path)
    _seed_run_dir(owner_dir)
    assert _run_cli(monkeypatch, ["daemon", "list", flag, str(owner_dir)]) == 0
    assert "em-1" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["emanate", "list", "check", "ask", "wait", "reclaim"])
def test_every_daemon_command_documents_owner_dir(monkeypatch, capsys, command):
    assert _run_cli(monkeypatch, ["daemon", command, "--help"]) == 0
    out = capsys.readouterr().out
    assert "--owner-dir" in out
    assert "owner" in out.lower()
    assert "Agent working directory" not in out


def test_owner_dir_errors_speak_owner(tmp_path, monkeypatch, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _run_cli(monkeypatch, ["daemon", "list", "--owner-dir", str(bare)]) == 1
    err = capsys.readouterr().err
    assert "--owner-dir" in err and "init.json" in err
    assert "--agent-dir" not in err


def test_standalone_owner_dispatch_keeps_state_owner_local_without_a_lease(
    tmp_path, monkeypatch, capsys, no_spawn,
):
    """An owner directory with its own init.json needs no running Agent."""
    owner_dir = _write_agent_dir(tmp_path)
    tasks = _write_tasks(tmp_path, [{"task": "owned task", "tools": ["file"]}])
    assert _run_cli(monkeypatch, [
        "daemon", "emanate", "--tasks", str(tasks), "--owner-dir", str(owner_dir), "--yes",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dispatched"
    assert len(no_spawn) == 1
    assert Path(no_spawn[0]["run_dir"].path).parent == owner_dir / "daemons"
    for marker in (".agent.lock", ".agent.heartbeat", ".agent.json"):
        assert not (owner_dir / marker).exists()


def test_supervisor_terminal_notification_anchors_to_the_owner_dir(tmp_path):
    """The detached publisher routes by the manifest's owner directory alone."""
    from lingtai.tools.daemon.run_dir import DaemonRunDir
    from lingtai.tools.daemon.supervisor_runtime import _publish_daemon_notification

    owner_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(owner_dir)
    run_dir = DaemonRunDir.attach(run_path)
    state = DaemonRunDir.read_state_from_disk(run_path)

    published = _publish_daemon_notification(
        run_dir, {"parent_working_dir": str(owner_dir)},
        status="done", state=state, idempotency_key="k-owner",
    )
    assert published is True
    assert list((owner_dir / ".notification" / "daemon").glob("*.json"))
    assert not (tmp_path / ".notification").exists()


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def test_ask_dispatches_through_the_daemon_family(tmp_path, monkeypatch, capsys):
    """CLI ask is the tool-family ask surface, not a second follow-up path."""
    owner_dir = _write_agent_dir(tmp_path)
    calls: list[tuple[Path, str, dict]] = []

    def fake_dispatch(agent, action, action_input):
        calls.append((agent._working_dir, action, action_input))
        return {"status": "sent", "id": "em-1"}

    monkeypatch.setattr("lingtai.cli_daemon._dispatch_through_tool_family", fake_dispatch)

    assert _run_cli(monkeypatch, [
        "daemon", "ask", "em-1", "stop at the next checkpoint", "--owner-dir", str(owner_dir),
    ]) == 0
    assert calls == [(owner_dir, "ask", {"id": "em-1", "message": "stop at the next checkpoint"})]
    assert json.loads(capsys.readouterr().out) == {"status": "sent", "id": "em-1"}


@pytest.mark.parametrize("status, code", [
    ("sent", 0), ("queued", 0), ("busy", 1), ("error", 1),
])
def test_ask_exit_status_tracks_the_engine_result(tmp_path, monkeypatch, capsys,
                                                  status, code):
    owner_dir = _write_agent_dir(tmp_path)
    monkeypatch.setattr(
        "lingtai.cli_daemon._dispatch_through_tool_family",
        lambda agent, action, action_input: {"status": status, "id": "em-1"},
    )
    assert _run_cli(monkeypatch, [
        "daemon", "ask", "em-1", "hello", "--owner-dir", str(owner_dir),
    ]) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_ask_unknown_id_is_refused_by_the_real_engine(tmp_path, monkeypatch, capsys):
    owner_dir = _write_agent_dir(tmp_path)
    assert _run_cli(monkeypatch, [
        "daemon", "ask", "em-404", "hello", "--owner-dir", str(owner_dir),
    ]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert "em-404" in result["message"]


def test_ask_refuses_a_blank_message_before_dispatch(tmp_path, monkeypatch, capsys):
    owner_dir = _write_agent_dir(tmp_path)
    monkeypatch.setattr(
        "lingtai.cli_daemon._dispatch_through_tool_family",
        lambda *a: pytest.fail("a blank message must never reach the engine"),
    )
    assert _run_cli(monkeypatch, [
        "daemon", "ask", "em-1", "   ", "--owner-dir", str(owner_dir),
    ]) == 1
    assert "message" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


def _script_sleeps(monkeypatch, steps) -> list:
    """Drive ``wait`` deterministically: each poll interval runs the next step."""
    queue = list(steps)

    def _sleep(_seconds: float) -> None:
        if not queue:
            raise AssertionError("wait polled after the scripted run ended")
        queue.pop(0)()

    monkeypatch.setattr("lingtai.cli_daemon._sleep", _sleep)
    return queue


def _forbid_manager_construction(monkeypatch) -> None:
    from lingtai.tools.daemon import DaemonManager

    def _refuse(self, *args, **kwargs):
        pytest.fail("wait must observe without constructing a DaemonManager")

    monkeypatch.setattr(DaemonManager, "__init__", _refuse)


def test_wait_reports_each_progress_change_once_then_exits_zero_on_done(
    tmp_path, monkeypatch, capsys,
):
    from lingtai.tools.daemon.run_dir import DaemonRunDir

    owner_dir = _write_agent_dir(tmp_path)
    run_dir = DaemonRunDir.attach(_seed_run_dir(owner_dir, state="running"))
    remaining = _script_sleeps(monkeypatch, [
        lambda: run_dir.update_state(turn=1, current_tool="file"),
        lambda: None,  # nothing changed: must not be reported again
        lambda: run_dir.record_checkpoint({"state": "implementing", "summary": "half way"}),
        lambda: run_dir.mark_done("finished"),
    ])

    assert _run_cli(monkeypatch, ["daemon", "wait", "em-1", "--owner-dir", str(owner_dir)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert remaining == []
    assert len(lines) == 4  # first observation, turn/tool, checkpoint, terminal
    assert "running" in lines[0]
    assert "turn=1" in lines[1] and "file" in lines[1]
    assert "checkpoint" in lines[2] and "half way" in lines[2]
    assert "done" in lines[3]


def test_wait_json_emits_one_record_per_change_and_a_terminal_record(
    tmp_path, monkeypatch, capsys,
):
    from lingtai.tools.daemon.run_dir import DaemonRunDir

    owner_dir = _write_agent_dir(tmp_path)
    run_dir = DaemonRunDir.attach(_seed_run_dir(owner_dir, state="running"))
    _forbid_manager_construction(monkeypatch)
    _script_sleeps(monkeypatch, [
        lambda: run_dir.update_state(turn=1, current_tool="file"),
        lambda: run_dir.record_checkpoint({"state": "implementing", "summary": "half way"}),
        lambda: run_dir.mark_done("finished"),
    ])

    assert _run_cli(monkeypatch, [
        "daemon", "wait", "em-1", "--json", "--owner-dir", str(owner_dir),
    ]) == 0

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["event"] for r in records] == ["progress", "progress", "progress", "terminal"]
    assert all(r["id"] == "em-1" for r in records)
    assert records[0]["state"] == "running" and records[0]["checkpoint"] is None
    assert records[1]["turn"] == 1 and records[1]["current_tool"] == "file"
    assert records[2]["checkpoint"]["sequence"] == 1
    assert records[2]["checkpoint"]["summary"] == "half way"
    assert records[-1]["state"] == "done" and records[-1]["exit_code"] == 0
    assert records[-1]["check"]["result_path"].endswith("result.txt")


def test_wait_does_not_rescan_the_events_log_on_each_poll(
    tmp_path, monkeypatch, capsys,
):
    """Only initial resolution and terminal detail use the full check path."""
    from lingtai.tools.daemon import DaemonManager
    from lingtai.tools.daemon.run_dir import DaemonRunDir

    owner_dir = _write_agent_dir(tmp_path)
    run_dir = DaemonRunDir.attach(_seed_run_dir(owner_dir, state="running"))
    calls = []
    original = DaemonManager._handle_check

    def counted(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DaemonManager, "_handle_check", counted)
    _script_sleeps(monkeypatch, [
        lambda: run_dir.update_state(turn=1),
        lambda: run_dir.update_state(turn=2),
        lambda: run_dir.mark_done("finished"),
    ])

    assert _run_cli(monkeypatch, [
        "daemon", "wait", "em-1", "--json", "--owner-dir", str(owner_dir),
    ]) == 0
    assert len(calls) == 2  # initial id/path resolution, then one terminal full check
    assert calls[0][1] == {"last": 1}
    assert calls[1][1] == {}
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["event"] == "terminal"


@pytest.mark.parametrize("state, code", [
    ("done", 0), ("failed", 1), ("cancelled", 1), ("timeout", 1),
])
def test_wait_exit_status_reflects_the_terminal_state(tmp_path, monkeypatch, capsys,
                                                      state, code):
    owner_dir = _write_agent_dir(tmp_path)
    _seed_run_dir(owner_dir, state=state)
    monkeypatch.setattr(
        "lingtai.cli_daemon._sleep",
        lambda _s: pytest.fail("an already-terminal run must not be polled again"),
    )
    assert _run_cli(monkeypatch, [
        "daemon", "wait", "em-1", "--json", "--owner-dir", str(owner_dir),
    ]) == code
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["event"] for r in records] == ["terminal"]
    assert records[0]["state"] == state and records[0]["exit_code"] == code


def test_wait_timeout_exits_124_and_writes_nothing(tmp_path, monkeypatch, capsys):
    owner_dir = _write_agent_dir(tmp_path)
    run_path = _seed_run_dir(owner_dir, state="running")
    before = (run_path / "daemon.json").read_bytes()
    clock = [0.0]
    monkeypatch.setattr("lingtai.cli_daemon._sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr("lingtai.cli_daemon._monotonic", lambda: clock[0])

    assert _run_cli(monkeypatch, [
        "daemon", "wait", "em-1", "--timeout", "3", "--interval", "1", "--json",
        "--owner-dir", str(owner_dir),
    ]) == 124

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["event"] == "progress"
    assert records[-1]["event"] == "timeout"
    assert records[-1]["state"] == "running" and records[-1]["exit_code"] == 124
    assert (run_path / "daemon.json").read_bytes() == before
    assert not (owner_dir / ".notification").exists()


def test_wait_interrupt_exits_130_with_a_final_record(tmp_path, monkeypatch, capsys):
    owner_dir = _write_agent_dir(tmp_path)
    _seed_run_dir(owner_dir, state="running")

    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("lingtai.cli_daemon._sleep", _interrupt)
    assert _run_cli(monkeypatch, [
        "daemon", "wait", "em-1", "--json", "--owner-dir", str(owner_dir),
    ]) == 130
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]["event"] == "interrupted" and records[-1]["exit_code"] == 130


def test_wait_unknown_id_exits_nonzero(tmp_path, monkeypatch, capsys):
    owner_dir = _write_agent_dir(tmp_path)
    assert _run_cli(monkeypatch, [
        "daemon", "wait", "em-404", "--owner-dir", str(owner_dir),
    ]) == 1
    assert "em-404" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--timeout", "0"], ["--interval", "0"], ["--interval", "-1"],
    ["--timeout", "inf"], ["--interval", "nan"],
])
def test_wait_bounds_must_be_positive(tmp_path, monkeypatch, capsys, argv):
    owner_dir = _write_agent_dir(tmp_path)
    assert _run_cli(monkeypatch, [
        "daemon", "wait", "em-1", *argv, "--owner-dir", str(owner_dir),
    ]) == 2


# ---------------------------------------------------------------------------
# list --json
# ---------------------------------------------------------------------------


def test_list_json_prints_the_engine_payload(tmp_path, monkeypatch, capsys):
    owner_dir = _write_agent_dir(tmp_path)
    _seed_run_dir(owner_dir)
    assert _run_cli(monkeypatch, [
        "daemon", "list", "--json", "--owner-dir", str(owner_dir),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in payload["emanations"]] == ["em-1"]
    assert payload["emanations"][0]["status"] == "done"
    assert payload["running"] == 0
