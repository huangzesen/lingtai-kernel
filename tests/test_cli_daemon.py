"""Standalone daemon service and its thin CLI driver."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def _write_preset(tmp_path: Path) -> Path:
    path = tmp_path / "direct-preset.json"
    path.write_text(
        json.dumps(
            {
                "name": "direct-preset",
                "description": {"summary": "standalone acceptance preset"},
                "manifest": {
                    "llm": {
                        "provider": "anthropic",
                        "model": "preset-model",
                        "api_key": "preset-key",
                        "base_url": None,
                    },
                    "capabilities": {"file": {}},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _invoke_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    from lingtai import cli

    monkeypatch.setattr(sys, "argv", ["lingtai-agent", *argv])
    cli.main()


@pytest.mark.parametrize("driver", ["service", "cli"])
def test_standalone_direct_preset_dispatch_and_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    driver: str,
) -> None:
    """No init/Agent: direct preset dispatch and readback stay under state root."""
    from lingtai.agent import Agent
    from lingtai.services.daemon import DaemonService
    from lingtai.tools.daemon import DaemonManager

    state_root = tmp_path / "standalone-state"
    state_root.mkdir()
    preset = _write_preset(tmp_path)
    tasks_path = tmp_path / "tasks.json"

    assert not (state_root / "init.json").exists()
    monkeypatch.setattr(
        "lingtai.kernel.preset_connectivity.check_connectivity",
        lambda **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(
        Agent,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("standalone daemon service must not construct Agent")
        ),
    )
    spawned: list[Path] = []

    def capture_spawn(_manager: DaemonManager, run_dir, **_kwargs) -> None:
        spawned.append(run_dir.path)

    monkeypatch.setattr(DaemonManager, "_spawn_detached_lingtai_run", capture_spawn)

    missing = [{"task": "missing preset must fail", "tools": ["file"]}]
    if driver == "service":
        service = DaemonService(state_root)
        refusal = service.emanate(missing)
    else:
        tasks_path.write_text(json.dumps(missing), encoding="utf-8")
        with pytest.raises(SystemExit) as exit_info:
            _invoke_cli(
                monkeypatch,
                [
                    "daemon", "emanate", "--state-root", str(state_root),
                    "--tasks", str(tasks_path),
                ],
            )
        assert exit_info.value.code == 1
        refusal = json.loads(capsys.readouterr().out)
    assert refusal["status"] == "error"
    assert "preset is required" in refusal["message"]
    assert spawned == []
    assert not (state_root / "daemons").exists()

    task = {
        "task": "prove standalone direct-preset dispatch",
        "tools": ["file"],
        "preset": str(preset),
    }
    if driver == "service":
        result = service.emanate([task])
    else:
        tasks_path.write_text(json.dumps([task]), encoding="utf-8")
        _invoke_cli(
            monkeypatch,
            [
                "daemon", "emanate", "--state-root", str(state_root),
                "--tasks", str(tasks_path),
            ],
        )
        result = json.loads(capsys.readouterr().out)

    assert result["status"] == "dispatched"
    daemon_id = result["ids"][0]
    run_dir = state_root / "daemons" / daemon_id
    state = json.loads((run_dir / "daemon.json").read_text(encoding="utf-8"))
    assert spawned == [run_dir]
    assert state["preset_name"] == str(preset)
    assert state["preset_model"] == "preset-model"
    assert state["model"] == "preset-model"

    if driver == "service":
        snapshot = service.check(daemon_id)
    else:
        _invoke_cli(
            monkeypatch,
            ["daemon", "check", daemon_id, "--state-root", str(state_root)],
        )
        snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["state"] == "running"
    assert snapshot["id"] == daemon_id
    assert Path(snapshot["path"]) == run_dir

    assert not (state_root / "init.json").exists()
    assert not (state_root / ".agent.lock").exists()
    assert not (state_root / ".agent.heartbeat").exists()
    assert not (state_root / ".agent.json").exists()
