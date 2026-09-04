"""Conformance tests for the registry-backed full-tool Puffo ACP profile."""
from __future__ import annotations

import argparse
import io
import json
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lingtai.adapters.acp.puffo_v0 import (
    PuffoV0RegistryError,
    RUNTIME_POLICY,
    provision_runtime,
    resolve_runtime,
    revoke_runtime,
)
from lingtai.adapters.acp.server import AcpStdioServer, INVALID_PARAMS
from lingtai.adapters.acp.puffo_v1 import validate_puffo_v1_mcp_servers
from lingtai.agent import Agent
from lingtai.kernel.execution_workspace import ExecutionWorkspace
from lingtai.kernel.config import AgentConfig
from lingtai.kernel.provider_admission import ProviderAdmittedLLMService
from lingtai.kernel.turns import TurnAdmissionError, TurnOrigin, submit_turn
from tests._service_helpers import make_gemini_mock_service as make_mock_service
from tests.test_deep_refresh import _make_init


class _Agent:
    def __init__(self):
        self._shutdown = None
        self.session_mcp_configs = None

    def mount_session_mcp_stdio(self, configs):
        self.session_mcp_configs = configs
        return _SessionMCPLease()


class _SessionMCPLease:
    def close(self):
        return None


def _frames(output: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _wait_for_frames(output: io.StringIO, count: int) -> list[dict]:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        frames = _frames(output)
        if len(frames) >= count:
            return frames
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {count} frames")


def _new_profile_server(workspace: Path) -> tuple[AcpStdioServer, io.StringIO]:
    output = io.StringIO()
    return (
        AcpStdioServer(
            _Agent(),
            io.StringIO(),
            output,
            fixed_execution_workspace=ExecutionWorkspace(workspace),
            allow_session_mcp=False,
        ),
        output,
    )


def _puffo_server_config(**overrides):
    config = {
        "name": "puffo",
        "command": "/usr/local/bin/python3",
        "args": ["-m", "puffo_agent.mcp.puffo_core_server"],
        "env": [
            {"name": "PUFFO_LOCAL_SERVICE_TOKEN", "value": "test-token"},
            {"name": "PYTHONPATH", "value": "/tmp/editable"},
        ],
    }
    config.update(overrides)
    return config


def _new_v1_profile_server(workspace: Path) -> tuple[AcpStdioServer, _Agent, io.StringIO]:
    output = io.StringIO()
    agent = _Agent()
    return (
        AcpStdioServer(
            agent,
            io.StringIO(),
            output,
            fixed_execution_workspace=ExecutionWorkspace(workspace),
            session_mcp_validator=validate_puffo_v1_mcp_servers,
        ),
        agent,
        output,
    )


def _request(server: AcpStdioServer, request_id: int, method: str, params: dict) -> None:
    server._dispatch({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    })


def test_provisioned_runtime_resolves_only_the_canonical_local_paths(tmp_path):
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"

    provisioned = provision_runtime(
        "puffo-agent-7", agent_dir, workspace, registry_path=registry
    )
    resolved = resolve_runtime("puffo-agent-7", registry_path=registry)

    assert resolved == provisioned
    stored = json.loads(registry.read_text(encoding="utf-8"))
    entry = stored["runtimes"]["puffo-agent-7"]
    assert entry["mcp_servers"] == []
    assert entry["tool_surface"] == "operator_managed_full"
    assert entry["turn_origins"] == ["authenticated_adapter"]
    assert entry["runtime_policy_version"] == RUNTIME_POLICY.policy_version


def test_registry_rejects_tampering_and_revoked_runtime(tmp_path):
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("opaque-id", agent_dir, workspace, registry_path=registry)

    data = json.loads(registry.read_text(encoding="utf-8"))
    data["runtimes"]["opaque-id"]["workspace"] = str(tmp_path)
    registry.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PuffoV0RegistryError, match="does not match"):
        resolve_runtime("opaque-id", registry_path=registry)

    other_agent_dir = tmp_path / "other-identity"
    other_agent_dir.mkdir()
    (other_agent_dir / "init.json").write_text("{}", encoding="utf-8")
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    provision_runtime("active-id", other_agent_dir, other_workspace, registry_path=registry)
    revoke_runtime("active-id", registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="inactive"):
        resolve_runtime("active-id", registry_path=registry)


def test_runtime_binding_rejects_symlink_retargeting_of_identity_and_workspace(tmp_path):
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)

    replacement_agent = tmp_path / "replacement-agent"
    replacement_agent.mkdir()
    (replacement_agent / "init.json").write_text("{}", encoding="utf-8")
    replacement_workspace = tmp_path / "replacement-workspace"
    replacement_workspace.mkdir()
    agent_dir.rename(tmp_path / "original-agent")
    workspace.rename(tmp_path / "original-workspace")
    agent_dir.symlink_to(replacement_agent, target_is_directory=True)
    workspace.symlink_to(replacement_workspace, target_is_directory=True)

    with pytest.raises(PuffoV0RegistryError, match="binding no longer matches"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_runtime_binding_rejects_replaced_directory_at_the_same_canonical_path(tmp_path):
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)

    agent_dir.rename(tmp_path / "original-agent")
    workspace.rename(tmp_path / "original-workspace")
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace.mkdir()

    with pytest.raises(PuffoV0RegistryError, match="binding no longer matches"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_active_runtime_bindings_require_dedicated_identity_and_workspace(tmp_path):
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_agent_dir = tmp_path / "other-identity"
    other_agent_dir.mkdir()
    (other_agent_dir / "init.json").write_text("{}", encoding="utf-8")
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)

    with pytest.raises(PuffoV0RegistryError, match="agent_dir is already bound"):
        provision_runtime("runtime-b", agent_dir, other_workspace, registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="workspace is already bound"):
        provision_runtime("runtime-c", other_agent_dir, workspace, registry_path=registry)


def test_registry_mutations_are_linearized_so_revoke_cannot_be_resurrected(
    monkeypatch, tmp_path
):
    """A stale provision snapshot must never overwrite a completed revoke."""
    import lingtai.adapters.acp.puffo_v0 as puffo_v0

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_dir_b = tmp_path / "identity-b"
    agent_dir_b.mkdir()
    (agent_dir_b / "init.json").write_text("{}", encoding="utf-8")
    workspace_b = tmp_path / "workspace-b"
    workspace_b.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)

    original_write = puffo_v0._write_registry
    provision_waiting = threading.Event()
    release_provision = threading.Event()
    revoke_write_seen = threading.Event()
    failures: list[BaseException] = []

    def controlled_write(path, data):
        entry_a = data["runtimes"].get("runtime-a", {})
        if "runtime-b" in data["runtimes"] and entry_a.get("status") == "active":
            provision_waiting.set()
            assert release_provision.wait(timeout=5)
        if entry_a.get("status") == "revoked":
            revoke_write_seen.set()
        original_write(path, data)

    monkeypatch.setattr(puffo_v0, "_write_registry", controlled_write)

    def provision() -> None:
        try:
            provision_runtime("runtime-b", agent_dir_b, workspace_b, registry_path=registry)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    def revoke() -> None:
        try:
            revoke_runtime("runtime-a", registry_path=registry)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    provision_thread = threading.Thread(target=provision)
    provision_thread.start()
    assert provision_waiting.wait(timeout=5)
    revoke_thread = threading.Thread(target=revoke)
    revoke_thread.start()

    # Without a mutation lock, revoke writes while provision is paused and the
    # stale provision snapshot subsequently resurrects runtime-a.  With the
    # lock, revoke cannot reach its write until provision releases the lock.
    revoke_write_seen.wait(timeout=0.2)
    release_provision.set()
    provision_thread.join(timeout=5)
    revoke_thread.join(timeout=5)
    assert not provision_thread.is_alive()
    assert not revoke_thread.is_alive()
    assert not failures

    data = json.loads(registry.read_text(encoding="utf-8"))
    assert data["runtimes"]["runtime-a"]["status"] == "revoked"
    assert data["runtimes"]["runtime-b"]["status"] == "active"


def test_revocation_tombstone_denies_a_stale_active_registry_snapshot(tmp_path):
    """The main registry cannot reactivate an id once its tombstone is written."""
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    stale_active_snapshot = registry.read_text(encoding="utf-8")

    revoke_runtime("runtime-a", registry_path=registry)
    registry.write_text(stale_active_snapshot, encoding="utf-8")

    with pytest.raises(PuffoV0RegistryError, match="inactive"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_missing_required_tombstone_log_fails_closed(tmp_path):
    import lingtai.adapters.acp.puffo_v0 as puffo_v0

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    stale_active_snapshot = registry.read_text(encoding="utf-8")
    revoke_runtime("runtime-a", registry_path=registry)
    puffo_v0._revocation_log_path(registry).unlink()
    registry.write_text(stale_active_snapshot, encoding="utf-8")

    with pytest.raises(PuffoV0RegistryError, match="revocation log is unavailable"):
        resolve_runtime("runtime-a", registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="revocation log is unavailable"):
        provision_runtime("runtime-b", agent_dir, workspace, registry_path=registry)


@pytest.mark.skipif(os.name != "posix", reason="puffo-v0 registry is POSIX-only")
def test_registry_mutation_lock_blocks_a_second_process(tmp_path):
    import lingtai.adapters.acp.puffo_v0 as puffo_v0

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_dir_b = tmp_path / "identity-b"
    agent_dir_b.mkdir()
    (agent_dir_b / "init.json").write_text("{}", encoding="utf-8")
    workspace_b = tmp_path / "workspace-b"
    workspace_b.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)

    context = multiprocessing.get_context("fork")
    provisioned = context.Event()

    def provision_in_child() -> None:
        provision_runtime("runtime-b", agent_dir_b, workspace_b, registry_path=registry)
        provisioned.set()

    with puffo_v0._registry_mutation_lock(registry):
        child = context.Process(target=provision_in_child)
        child.start()
        assert not provisioned.wait(timeout=0.2)
    child.join(timeout=5)
    assert child.exitcode == 0
    assert provisioned.is_set()
    assert resolve_runtime("runtime-b", registry_path=registry).runtime_id == "runtime-b"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not meaningful on Windows")
def test_registry_directory_and_files_are_owner_only_even_with_a_permissive_umask(
    monkeypatch, tmp_path
):
    import lingtai.adapters.acp.puffo_v0 as puffo_v0

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "private" / "registry.json"
    observed: dict[str, int] = {}
    original_replace = os.replace

    def inspect_temporary(source, target):
        observed["temporary"] = stat.S_IMODE(Path(source).stat().st_mode)
        original_replace(source, target)

    monkeypatch.setattr(puffo_v0.os, "replace", inspect_temporary)
    previous_umask = os.umask(0)
    try:
        provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(registry.parent.stat().st_mode) == 0o700
    assert observed["temporary"] == 0o600
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    assert stat.S_IMODE(registry.with_name(".registry.json.lock").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not meaningful on Windows")
def test_resolve_tightens_legacy_registry_file_and_directory_modes(tmp_path):
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "legacy" / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    registry.parent.chmod(0o755)
    registry.chmod(0o644)

    assert resolve_runtime("runtime-a", registry_path=registry).runtime_id == "runtime-a"
    assert stat.S_IMODE(registry.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600


def test_profile_session_rejects_remote_workspace_and_mcp_inputs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    server, output = _new_profile_server(workspace)
    _request(server, 1, "initialize", {"protocolVersion": 1})

    _request(server, 2, "session/new", {"cwd": str(other), "mcpServers": []})
    _request(server, 3, "session/new", {
        "cwd": str(workspace),
        "mcpServers": [{"name": "unsafe", "command": "/bin/echo", "args": [], "env": []}],
    })

    frames = _wait_for_frames(output, 3)
    assert frames[1]["error"]["code"] == INVALID_PARAMS
    assert frames[1]["error"]["message"] == "cwd must match the profile's fixed execution workspace"
    assert frames[2]["error"]["code"] == INVALID_PARAMS
    assert frames[2]["error"]["message"] == "mcpServers must be an empty array for this profile"
    server.close()


def test_puffo_v1_session_mounts_only_the_fixed_puffo_core_service(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server, agent, output = _new_v1_profile_server(workspace)
    _request(server, 1, "initialize", {"protocolVersion": 1})
    _request(
        server,
        2,
        "session/new",
        {"cwd": str(workspace), "mcpServers": [_puffo_server_config()]},
    )

    frames = _wait_for_frames(output, 2)
    assert "result" in frames[1]
    assert agent.session_mcp_configs is not None
    assert len(agent.session_mcp_configs) == 1
    assert agent.session_mcp_configs[0].name == "puffo"
    assert agent.session_mcp_configs[0].args == (
        "-m", "puffo_agent.mcp.puffo_core_server"
    )
    server.close()


@pytest.mark.parametrize(
    ("servers", "message"),
    [
        ([], "puffo-v1 requires exactly one Puffo MCP server"),
        (
            [_puffo_server_config(), _puffo_server_config(name="puffo-two")],
            "puffo-v1 requires exactly one Puffo MCP server",
        ),
        ([_puffo_server_config(name="not-puffo")], "puffo-v1 MCP server must be named puffo"),
        (
            [_puffo_server_config(args=["-m", "another.server"])],
            "puffo-v1 MCP server must run puffo_agent.mcp.puffo_core_server",
        ),
        (
            [_puffo_server_config(env=[])],
            "puffo-v1 MCP server requires a non-empty Puffo local service token",
        ),
        (
            [_puffo_server_config(env=[{"name": "HOME", "value": "/tmp"}])],
            "puffo-v1 MCP server requires a non-empty Puffo local service token",
        ),
        (
            [_puffo_server_config(env=[{"name": "PUFFO_LOCAL_SERVICE_TOKEN", "value": ""}])],
            "puffo-v1 MCP server requires a non-empty Puffo local service token",
        ),
    ],
)
def test_puffo_v1_session_rejects_any_other_mcp_shape(tmp_path, servers, message):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server, _agent, output = _new_v1_profile_server(workspace)
    _request(server, 1, "initialize", {"protocolVersion": 1})
    _request(server, 2, "session/new", {"cwd": str(workspace), "mcpServers": servers})

    frames = _wait_for_frames(output, 2)
    assert frames[1]["error"] == {"code": INVALID_PARAMS, "message": message}
    server.close()


def test_puffo_v1_session_preserves_unknown_environment_names(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server, agent, output = _new_v1_profile_server(workspace)
    _request(server, 1, "initialize", {"protocolVersion": 1})
    config = _puffo_server_config()
    config["env"].append({"name": "HOME", "value": "/tmp"})
    _request(server, 2, "session/new", {"cwd": str(workspace), "mcpServers": [config]})

    frames = _wait_for_frames(output, 2)
    assert "result" in frames[1]
    assert ("HOME", "/tmp") in agent.session_mcp_configs[0].env
    server.close()


def test_profile_cli_resolves_an_opaque_id_before_composing_acp(monkeypatch, tmp_path):
    import lingtai.cli_acp as cli_acp
    from lingtai.adapters.acp.driver_authority import UnavailableDriverAuthorityAdapter
    from lingtai.adapters.acp.puffo_v0 import DirectoryBinding, PuffoV0Runtime

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = DirectoryBinding(device=1, inode=2, owner=3, group=4)
    runtime = PuffoV0Runtime(
        "runtime-1", agent_dir, workspace, "digest", binding, binding,
        RUNTIME_POLICY.policy_version,
    )
    observed = {}
    monkeypatch.setattr(cli_acp, "resolve_runtime", lambda _id: runtime, raising=False)
    # The handler imports from the profile module after parser validation.
    monkeypatch.setattr("lingtai.adapters.acp.puffo_v0.resolve_runtime", lambda _id: runtime)
    monkeypatch.setattr(
        "lingtai.adapters.acp.driver_authority.authority_adapter_from_environment",
        UnavailableDriverAuthorityAdapter,
    )
    monkeypatch.setattr(cli_acp, "run_acp", lambda directory, **kwargs: observed.update(directory=directory, **kwargs))

    cli_acp.handle_acp_command(SimpleNamespace(profile="puffo-v0", runtime_id="runtime-1", agent_dir=None))

    assert observed["directory"] == agent_dir
    assert observed["fixed_execution_workspace"].root == workspace
    assert observed.get("forced_disable") is None
    assert observed["turn_origin_policy"] is RUNTIME_POLICY
    assert observed["requires_turn_origin_policy"] is True
    assert isinstance(observed["provider_call_admission_port"], UnavailableDriverAuthorityAdapter)
    assert observed["derived_launch_admission_port"] is observed["provider_call_admission_port"]
    assert observed["requires_derived_launch_admission_port"] is True
    assert observed["puffo_runtime"] == runtime


def test_profile_cli_composes_a_root_driver_for_both_admission_boundaries(monkeypatch, tmp_path):
    import lingtai.cli_acp as cli_acp
    from lingtai.adapters.acp.driver_authority import (
        DriverAuthorityClient,
        DriverDerivedLaunchAdmissionAdapter,
    )
    from lingtai.adapters.acp.puffo_v0 import DirectoryBinding, PuffoV0Runtime

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = DirectoryBinding(device=1, inode=2, owner=3, group=4)
    runtime = PuffoV0Runtime(
        "runtime-1", agent_dir, workspace, "digest", binding, binding,
        RUNTIME_POLICY.policy_version,
    )
    authority = object.__new__(DriverAuthorityClient)
    observed = {}
    monkeypatch.setattr("lingtai.adapters.acp.puffo_v0.resolve_runtime", lambda _id: runtime)
    monkeypatch.setattr(
        "lingtai.adapters.acp.driver_authority.authority_adapter_from_environment",
        lambda: authority,
    )
    monkeypatch.setattr(cli_acp, "run_acp", lambda directory, **kwargs: observed.update(directory=directory, **kwargs))

    cli_acp.handle_acp_command(SimpleNamespace(profile="puffo-v0", runtime_id="runtime-1", agent_dir=None))

    assert observed["provider_call_admission_port"] is authority
    assert isinstance(observed["derived_launch_admission_port"], DriverDerivedLaunchAdmissionAdapter)
    assert observed["derived_launch_admission_port"]._authority is authority


def test_puffo_v1_cli_reuses_the_bound_runtime_with_fixed_mcp_ingress(monkeypatch, tmp_path):
    import lingtai.cli_acp as cli_acp
    from lingtai.adapters.acp.driver_authority import DriverAuthorityClient
    from lingtai.adapters.acp.puffo_v1 import validate_puffo_v1_mcp_servers
    from lingtai.adapters.acp.puffo_v0 import DirectoryBinding, PuffoV0Runtime

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = DirectoryBinding(device=1, inode=2, owner=3, group=4)
    runtime = PuffoV0Runtime(
        "runtime-1", agent_dir, workspace, "digest", binding, binding,
        RUNTIME_POLICY.policy_version,
    )
    observed = {}
    monkeypatch.setattr("lingtai.adapters.acp.puffo_v0.resolve_runtime", lambda _id: runtime)
    authority = object.__new__(DriverAuthorityClient)
    monkeypatch.setattr(
        "lingtai.adapters.acp.driver_authority.authority_adapter_from_environment",
        lambda: authority,
    )
    monkeypatch.setattr(cli_acp, "run_acp", lambda directory, **kwargs: observed.update(directory=directory, **kwargs))

    cli_acp.handle_acp_command(SimpleNamespace(profile="puffo-v1", runtime_id="runtime-1", agent_dir=None))

    assert observed["directory"] == agent_dir
    assert observed["fixed_execution_workspace"].root == workspace
    assert observed["session_mcp_validator"] is validate_puffo_v1_mcp_servers
    assert observed["turn_origin_policy"] is RUNTIME_POLICY


def test_puffo_v1_cli_requires_an_authenticated_driver_authority(monkeypatch, tmp_path, capsys):
    import lingtai.cli_acp as cli_acp
    from lingtai.adapters.acp.driver_authority import UnavailableDriverAuthorityAdapter
    from lingtai.adapters.acp.puffo_v0 import DirectoryBinding, PuffoV0Runtime

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = DirectoryBinding(device=1, inode=2, owner=3, group=4)
    runtime = PuffoV0Runtime(
        "runtime-1", agent_dir, workspace, "digest", binding, binding,
        RUNTIME_POLICY.policy_version,
    )
    monkeypatch.setattr("lingtai.adapters.acp.puffo_v0.resolve_runtime", lambda _id: runtime)
    monkeypatch.setattr(
        "lingtai.adapters.acp.driver_authority.authority_adapter_from_environment",
        UnavailableDriverAuthorityAdapter,
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_acp.handle_acp_command(
            SimpleNamespace(profile="puffo-v1", runtime_id="runtime-1", agent_dir=None)
        )

    assert exc_info.value.code == 1
    assert "puffo-v1 requires an authenticated Puffo Driver authority" in capsys.readouterr().err


def test_constrained_acp_refuses_to_start_without_its_derived_launch_port(tmp_path):
    import lingtai.cli_acp as cli_acp

    with pytest.raises(
        ValueError, match="constrained ACP composition requires a derived-launch admission port"
    ):
        cli_acp.run_acp(
            tmp_path,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
            requires_derived_launch_admission_port=True,
        )


def test_profile_cli_rejects_agent_dir_instead_of_ignoring_it(capsys):
    import lingtai.cli_acp as cli_acp

    with pytest.raises(SystemExit) as exc_info:
        cli_acp.handle_acp_command(
            SimpleNamespace(profile="puffo-v0", runtime_id=None, agent_dir=Path("/tmp"))
        )
    assert exc_info.value.code == 1
    assert "puffo-v0 does not accept --agent-dir; use --runtime-id" in capsys.readouterr().err

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    cli_acp.add_acp_parser(subparsers)
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([
            "acp", "--profile", "puffo-v0", "--agent-dir", "/tmp",
            "--runtime-id", "opaque-id",
        ])
    assert exc_info.value.code == 2


def test_full_tool_profile_keeps_operator_managed_capabilities_available(tmp_path):
    agent = Agent(
        service=make_mock_service(),
        agent_name="profile-test",
        working_dir=tmp_path / "identity",
        _turn_origin_policy=RUNTIME_POLICY,
    )
    try:
        registered = {name for name, _ in agent._capabilities}
        # This is an external product oracle, not the implementation policy's
        # own constant: the full-tool profile intentionally preserves these
        # installed, operator-managed capability families.
        assert {"avatar", "daemon", "file", "mcp", "plugin", "shell", "task_card"} <= registered
    finally:
        agent.stop(timeout=1.0)


def test_full_tool_profile_refresh_does_not_erase_operator_managed_capabilities(tmp_path):
    (tmp_path / "init.json").write_text(
        json.dumps(_make_init(capabilities={"avatar": {}, "daemon": {}, "mcp": {}})),
        encoding="utf-8",
    )
    service = MagicMock()
    service.provider = "openai"
    service.model = "gpt-4o"
    service._base_url = None
    agent = Agent(
        service,
        agent_name="profile-refresh",
        working_dir=tmp_path,
        config=AgentConfig(),
        _turn_origin_policy=RUNTIME_POLICY,
        _from_init_boot=True,
    )
    agent._from_init_boot = False
    try:
        agent._setup_from_init()
        agent._setup_from_init()
        registered = {name for name, _ in agent._capabilities}
        assert {"avatar", "daemon", "mcp"} <= registered
    finally:
        agent.stop(timeout=1.0)


def test_profile_refresh_preserves_the_provider_admission_service_boundary(tmp_path):
    """Refresh cannot replace a profile's admitted service with raw LLM I/O."""
    (tmp_path / "init.json").write_text(
        json.dumps(_make_init(capabilities={"avatar": {}, "daemon": {}, "mcp": {}})),
        encoding="utf-8",
    )
    service = MagicMock()
    service.provider = "different-provider"  # Force the refresh rebuild path.
    service.model = "different-model"
    service._base_url = None
    service._context_window = None
    service._provider_defaults = {}
    agent = Agent(
        service,
        agent_name="profile-refresh-admission",
        working_dir=tmp_path,
        config=AgentConfig(),
        _turn_origin_policy=RUNTIME_POLICY,
        provider_call_admission_port=RUNTIME_POLICY,
        _from_init_boot=True,
    )
    agent._from_init_boot = False
    try:
        agent._setup_from_init()

        assert isinstance(agent.service, ProviderAdmittedLLMService)
        assert agent._session._llm_service is agent.service
        assert agent.service._port is RUNTIME_POLICY
    finally:
        agent.stop(timeout=1.0)


def test_profile_admits_only_authenticated_adapter_turns(tmp_path):
    """Independent origin oracle: non-ACP callers cannot queue provider work."""

    class OriginBoundAgent:
        def __init__(self):
            import queue

            self.inbox = queue.Queue()
            self._shutdown = None
            self._turn_origin_policy = RUNTIME_POLICY

    agent = OriginBoundAgent()

    with pytest.raises(TurnAdmissionError) as denied:
        submit_turn(agent, "must not run", origin=TurnOrigin.LEGACY)
    assert denied.value.decision.reason_code == "origin_not_authenticated_adapter"
    assert agent.inbox.empty()

    handle = submit_turn(
        agent,
        "authenticated prompt",
        origin=TurnOrigin.AUTHENTICATED_ADAPTER,
    )
    assert not handle.done()
    assert not agent.inbox.empty()


def test_constrained_profile_with_a_missing_origin_policy_fails_closed():
    class MissingPolicyAgent:
        def __init__(self):
            import queue

            self.inbox = queue.Queue()
            self._shutdown = None
            self._requires_turn_origin_policy = True

    with pytest.raises(TurnAdmissionError) as denied:
        submit_turn(MissingPolicyAgent(), "must not run")
    assert denied.value.decision.reason_code == "required_policy_missing"
