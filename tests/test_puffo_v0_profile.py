"""Conformance tests for the registry-backed full-tool Puffo ACP profile."""
from __future__ import annotations

import argparse
import io
import json
import multiprocessing
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lingtai.adapters.acp.puffo_v0 import (
    PuffoV0RegistryError,
    PuffoV0RuntimeState,
    RUNTIME_POLICY,
    _digest,
    discover_runtimes,
    provision_runtime,
    resolve_runtime,
    revoke_runtime,
)
from lingtai.adapters.acp.server import AcpStdioServer, INVALID_PARAMS
from lingtai.adapters.acp.puffo_v1 import PUFFO_MCP_ARGS, validate_puffo_v1_mcp_servers
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


def test_discover_lists_only_initialized_agents_and_preserves_registry_bytes(tmp_path):
    root = tmp_path / "selected-root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    ordinary_directory = root / "ordinary"
    ordinary_directory.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Discovery is read-only: it must not opportunistically harden a legacy
    # registry mode while scanning candidates.
    registry.chmod(0o644)
    registry_before = registry.read_bytes()
    registry_mode_before = stat.S_IMODE(registry.stat().st_mode)
    tombstone = registry.with_name(f".{registry.name}.revocations.jsonl")
    tombstone.chmod(0o644)
    tombstone_mode_before = stat.S_IMODE(tombstone.stat().st_mode)

    candidates = discover_runtimes(root, registry_path=registry)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.agent_dir == agent_dir.resolve()
    assert candidate.workspace == workspace.resolve()
    assert candidate.display_name == "identity"
    assert candidate.runtime_id == "runtime-a"
    assert registry.read_bytes() == registry_before
    assert stat.S_IMODE(registry.stat().st_mode) == registry_mode_before
    assert stat.S_IMODE(tombstone.stat().st_mode) == tombstone_mode_before


def test_discover_skips_symlink_escape_and_reports_revoked_binding(tmp_path):
    # Behavior change (was ..._revoked_bindings_are_available): a revoked binding
    # is no longer collapsed into an unbound "available" candidate. It surfaces
    # state=REVOKED and its runtime_id, because the caller's correct action --
    # re-provision under a NEW id -- differs from "you may provision here".
    root = tmp_path / "selected-root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "init.json").write_text("{}", encoding="utf-8")
    (root / "outside-link").symlink_to(outside, target_is_directory=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    revoke_runtime("runtime-a", registry_path=registry)

    candidates = discover_runtimes(root, registry_path=registry)

    assert [candidate.agent_dir for candidate in candidates] == [agent_dir.resolve()]
    assert candidates[0].state is PuffoV0RuntimeState.REVOKED
    assert candidates[0].runtime_id == "runtime-a"
    assert candidates[0].workspace is None


def test_discover_skips_unreadable_descendant_errors(tmp_path, monkeypatch):
    import lingtai.adapters.acp.puffo_v0 as puffo_v0

    root = tmp_path / "selected-root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")

    def unreadable_walk(path, *, topdown, followlinks, onerror):
        assert path == root.resolve()
        assert topdown is True
        assert followlinks is False
        yield str(path), ["blocked", "identity"], []
        onerror(PermissionError("blocked"))
        yield str(agent_dir), [], ["init.json"]

    monkeypatch.setattr(puffo_v0.os, "walk", unreadable_walk)

    candidates = discover_runtimes(root, registry_path=tmp_path / "missing-registry.json")

    assert [candidate.agent_dir for candidate in candidates] == [agent_dir.resolve()]


def test_discover_cli_emits_stable_json(tmp_path, monkeypatch, capsys):
    import lingtai.adapters.acp.puffo_v0 as puffo_v0
    import lingtai.cli_puffo_v0 as cli_puffo_v0

    root = tmp_path / "selected-root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(puffo_v0, "default_registry_path", lambda: registry)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    cli_puffo_v0.add_puffo_v0_parser(commands)
    args = parser.parse_args(["puffo-v0", "discover", "--root", str(root), "--json"])

    cli_puffo_v0.handle_puffo_v0_command(args)

    assert json.loads(capsys.readouterr().out) == {
        "runtimes": [{
            "agent_dir": str(agent_dir.resolve()),
            "display_name": "identity",
            "formerly_bound_runtime_id": None,
            "runtime_id": None,
            "status": "available",
            "workspace": None,
        }]
    }


def test_discover_cli_human_output_hangs_the_reuse_sign(tmp_path, monkeypatch, capsys):
    # Option C's sign must reach the human-readable (non-JSON) output too, not only
    # --json: a same-path replacement prints `available` AND the inline note naming
    # the runtime whose path was reused.
    import lingtai.adapters.acp.puffo_v0 as puffo_v0
    import lingtai.cli_puffo_v0 as cli_puffo_v0

    root = tmp_path / "selected-root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(puffo_v0, "default_registry_path", lambda: registry)
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Same-path replacement: move the recorded identity out of the root, recreate
    # a fresh directory at the same path -> `available` with the reuse sign.
    agent_dir.rename(tmp_path / "identity-moved")
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    cli_puffo_v0.add_puffo_v0_parser(commands)
    args = parser.parse_args(["puffo-v0", "discover", "--root", str(root)])  # no --json
    cli_puffo_v0.handle_puffo_v0_command(args)

    out = capsys.readouterr().out
    assert "(available)" in out
    assert "path previously bound runtime-a" in out


def test_discover_fails_closed_when_posix_registry_security_is_unavailable(tmp_path, monkeypatch):
    import lingtai.adapters.acp.puffo_v0 as puffo_v0

    root = tmp_path / "selected-root"
    root.mkdir()
    monkeypatch.setattr(puffo_v0.os, "name", "nt")

    with pytest.raises(PuffoV0RegistryError, match="requires POSIX"):
        discover_runtimes(root, registry_path=tmp_path / "missing-registry.json")


def _mutate_registry_entry(registry, runtime_id, *, resign, **overrides):
    """Overwrite fields of one registry entry, optionally re-signing its digest.

    ``resign=True`` recomputes ``entry_digest`` so the entry stays authentic
    (used to build a *legitimately* drifted/foreign entry); ``resign=False``
    leaves the digest untouched so the override alone decides integrity.
    """
    data = json.loads(registry.read_text(encoding="utf-8"))
    entry = data["runtimes"][runtime_id]
    entry.update(overrides)
    if resign:
        canonical = {k: v for k, v in entry.items() if k != "entry_digest"}
        entry["entry_digest"] = _digest(canonical)
    registry.write_text(json.dumps(data), encoding="utf-8")


def _discover_one(root, registry):
    candidates = discover_runtimes(root, registry_path=registry)
    assert len(candidates) == 1
    return candidates[0]


def _make_agent_dir_binding_owner_stale(registry, runtime_id="runtime-a"):
    """Make the live agent_dir identity mismatch WITHOUT moving the directory.

    Re-sign the entry with a wrong ``owner`` in ``agent_dir_binding``, keeping the
    same device/inode.  The classifier still rules it ACTIVE (the entry is
    authentic), and it is still keyed by the unchanged device/inode, so it attaches
    to the same walked directory -- but ``_bound_directory`` compares the full
    identity, so the mismatched owner makes ``_binding_matches`` false and discovery
    downgrades it to STALE_BINDING.  Leaves exactly one directory under the root,
    which the ``--json`` CLI pairwise test needs (it asserts a single row).
    """
    data = json.loads(registry.read_text(encoding="utf-8"))
    binding = dict(data["runtimes"][runtime_id]["agent_dir_binding"])
    binding["owner"] = binding["owner"] + 1
    _mutate_registry_entry(
        registry, runtime_id, resign=True, agent_dir_binding=binding
    )


def test_same_path_agent_dir_replacement_reports_available_and_moved_original_stale(
    tmp_path,
):
    # Tianzhe's report (agent_dir half), resolved by identity keying. A same-path
    # replacement no longer reports the recreated directory as `bound`: its
    # device/inode are genuinely new and unbound, so discover reports it
    # `available` -- and provisioning it under a new id really succeeds, so
    # `available = may provision` holds. The moved original keeps the provisioned
    # identity, so while it stays under the root it is reported `stale_binding`
    # with the runtime_id (the revoke path), and resolve_runtime still rejects the
    # entry on the binding. Assert BOTH rows and both follow-on operations.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Move the real directory (device/inode preserved) to a new path UNDER the
    # root, then recreate a fresh directory (new device/inode) at the old path.
    moved = root / "identity-moved"
    agent_dir.rename(moved)
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")

    candidates = {c.agent_dir: c for c in discover_runtimes(root, registry_path=registry)}
    recreated = candidates[agent_dir]
    assert recreated.state is PuffoV0RuntimeState.PROVISIONABLE   # fresh identity, unbound
    assert recreated.runtime_id is None
    assert recreated.workspace is None
    # Option C: the recreated path carries the advisory reuse sign -- the caller
    # sees this path formerly bound runtime-a even though it is now provisionable.
    assert recreated.formerly_bound_runtime_id == "runtime-a"
    moved_candidate = candidates[moved]
    assert moved_candidate.state is PuffoV0RuntimeState.STALE_BINDING
    assert moved_candidate.state is not PuffoV0RuntimeState.ACTIVE
    assert moved_candidate.runtime_id == "runtime-a"   # surfaced so it can be revoked
    assert moved_candidate.workspace is None
    # resolve still rejects the entry (its stored path now holds a new identity):
    with pytest.raises(PuffoV0RegistryError, match="binding no longer matches"):
        resolve_runtime("runtime-a", registry_path=registry)
    # And `available` is truthful: provisioning the recreated directory succeeds.
    workspace2 = tmp_path / "workspace2"
    workspace2.mkdir()
    provision_runtime("runtime-c", agent_dir, workspace2, registry_path=registry)


def test_bound_requires_live_workspace_binding_not_just_agent_dir(tmp_path):
    # Tianzhe's report (workspace half): the agent_dir is untouched but the
    # WORKSPACE is replaced. The workspace lives outside the discovery root and is
    # never walked, so discovery must lstat it directly. A test that only renamed
    # the agent_dir would leave this half unpinned.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Replace the workspace at the same path with a fresh directory (new inode).
    workspace.rename(tmp_path / "workspace-moved")
    workspace.mkdir()

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.STALE_BINDING
    assert candidate.runtime_id == "runtime-a"
    assert candidate.workspace is None
    with pytest.raises(PuffoV0RegistryError, match="binding no longer matches"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_orphaned_revocation_log_is_not_reported_available(tmp_path):
    # Tianzhe's report (orphaned tombstone): a revocation log with no registry is a
    # broken control-plane state. discover previously reported the directory as
    # `available` (may provision), but provision refuses to re-initialize over the
    # orphaned log. discover must fail closed instead. Assert the PAIR -- discover
    # raises AND provision raises.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    # Provision then revoke to create both the registry and its revocation log,
    # then delete only the registry to orphan the tombstone.
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    revoke_runtime("runtime-a", registry_path=registry)
    tombstone = registry.with_name(f".{registry.name}.revocations.jsonl")
    assert tombstone.exists()
    registry.unlink()

    with pytest.raises(PuffoV0RegistryError, match="revocation log exists without"):
        discover_runtimes(root, registry_path=registry)
    # And the operation discover is supposed to predict really does fail:
    other = tmp_path / "identity-2"
    other.mkdir()
    (other / "init.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PuffoV0RegistryError, match="unexpected revocation log"):
        provision_runtime("runtime-b", other, workspace, registry_path=registry)


def test_provision_rejects_double_binding_through_a_renamed_directory_symlink(tmp_path):
    # Peter's bug 3 at the EXECUTION point. Provision a runtime, then move the real
    # agent_dir to a new path and leave a symlink at the old path. The physical
    # directory keeps its device/inode, so a second provision at the NEW path binds
    # the SAME physical directory twice unless the guard compares identity: a
    # path-string comparison sees two different strings and waves it through.
    # Assert the PAIR -- discover reports the new path stale_binding (not
    # available), and the second provision is refused as a conflict on the same
    # identity. The provision match string is specific ("already bound to an active
    # runtime") so reverting the guard to a path-string comparison reddens here.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Move the real directory (device/inode preserved) and leave a symlink behind.
    new_path = root / "identity-new"
    agent_dir.rename(new_path)
    agent_dir.symlink_to(new_path)

    candidates = {c.agent_dir: c for c in discover_runtimes(root, registry_path=registry)}
    assert new_path in candidates                      # the symlink itself is not followed
    assert candidates[new_path].state is PuffoV0RuntimeState.STALE_BINDING
    assert candidates[new_path].state is not PuffoV0RuntimeState.PROVISIONABLE
    assert candidates[new_path].runtime_id == "runtime-a"
    # provision at the new path must be refused: same physical identity.
    workspace2 = tmp_path / "workspace2"
    workspace2.mkdir()
    with pytest.raises(PuffoV0RegistryError, match="already bound to active runtime") as exc_info:
        provision_runtime("runtime-b", new_path, workspace2, registry_path=registry)
    # Boris condition 二: the rejection must name (a) the offending runtime_id and
    # (b) the operation that clears it, plus the path the conflicting entry recorded.
    message = str(exc_info.value)
    assert "runtime-a" in message                         # (a) who holds it
    assert "revoke it before re-provisioning" in message  # (b) how to release it
    assert str(agent_dir) in message                      # the conflicting recorded path


def test_provision_conflict_criterion_excludes_owner_group_so_chown_cannot_reopen_it(
    tmp_path,
):
    # Boris's chown trap. "Same physical directory" is (device, inode); "still the
    # binding I provisioned" is the full (device, inode, owner, group) + canonical
    # path. The double-bind conflict criterion must use ONLY device/inode -- if it
    # ever widened to the full tuple, a chown between provisions (dev/inode
    # unchanged, owner changed) would make the two compare unequal and the second
    # provision of the SAME physical directory would be waved through, reviving
    # bug 3 while every test stayed green. A real chown needs root, so simulate the
    # post-provision owner change by re-signing the stored binding with a different
    # owner (authentic, so still ACTIVE): the live directory now has a different
    # owner than the entry records, exactly the change the widened criterion would
    # mishandle. It must still be refused, with the directory-occupied reason
    # (provision never emits stale_binding). Widening the criterion to the full
    # tuple reddens here.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Simulate `chown agent_dir` after provision: stored owner now differs from the
    # live owner, but device/inode are unchanged. Re-signed, so still authentic.
    binding = dict(json.loads(registry.read_text())["runtimes"]["runtime-a"]["agent_dir_binding"])
    binding["owner"] = binding["owner"] + 1
    _mutate_registry_entry(registry, "runtime-a", resign=True, agent_dir_binding=binding)
    workspace2 = tmp_path / "workspace2"
    workspace2.mkdir()
    with pytest.raises(PuffoV0RegistryError, match="already bound to active runtime"):
        provision_runtime("runtime-b", agent_dir, workspace2, registry_path=registry)


def test_provision_conflict_survives_a_corrupted_status_field(tmp_path):
    # Boris's second execution-point hole: the guard must read status THROUGH the
    # classifier (after the integrity check), never the raw signed field before it.
    # Corrupt an active entry's status WITHOUT re-signing: the digest now fails, so
    # the classifier rules it INTEGRITY_FAILED. It still occupies its directory, so
    # a second provision on the SAME identity must be refused -- with the integrity
    # message, distinct from the plain directory-occupied one -- NOT waved through
    # because the raw status no longer reads "active". Moving the status check back
    # above the classifier reddens here (the corrupt entry would vanish from the
    # guard and the second provision would succeed).
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Corrupt status without re-signing -> digest mismatch -> INTEGRITY_FAILED, but
    # the agent_dir_binding stays intact and still names the live identity.
    _mutate_registry_entry(registry, "runtime-a", resign=False, status="disabled")
    workspace2 = tmp_path / "workspace2"
    workspace2.mkdir()
    with pytest.raises(PuffoV0RegistryError, match="failed its integrity check") as exc_info:
        provision_runtime("runtime-b", agent_dir, workspace2, registry_path=registry)
    # The message names the escalation, not a dead-end recovery, and its truth
    # reduces to one behavior: revoke is REFUSED for a tampered entry.
    msg = str(exc_info.value)
    assert "escalate for review" in msg
    assert "revoke is refused" in msg
    assert "resolve it before provisioning" not in msg
    # Executable proof the message is honest: revoke actually refuses the tampered
    # entry (so it cannot re-sign it and erase the integrity signal), and the block
    # therefore persists rather than being cleared by a "successful" revoke.
    with pytest.raises(PuffoV0RegistryError, match="revoke is refused"):
        revoke_runtime("runtime-a", registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="failed its integrity check"):
        provision_runtime("runtime-b", agent_dir, workspace2, registry_path=registry)


def test_shape_mismatch_conflict_names_escalation_and_revoke_refuses_it(tmp_path):
    # A shape-broken entry occupying a directory blocks provisioning, and the
    # rejection must name the action that actually helps -- escalate + repair out of
    # band -- not revoke. Its truth reduces to one behavior: revoke_runtime REFUSES a
    # malformed entry (rather than "succeeding" while leaving the block in place, the
    # earlier dead end). This uses an extra-key subtype; the per-subtype matrix
    # (test_revoke_refuses_and_preserves_a_blocking_entry) proves the SAME refusal
    # holds for every shape/integrity subtype, so the message's universal claim is
    # backed by the admission rule rather than a single sample.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(registry, "runtime-a", resign=False, unexpected=True)
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    with pytest.raises(
        PuffoV0RegistryError, match="does not match the puffo-v0 profile"
    ) as exc_info:
        provision_runtime("runtime-b", agent_dir, ws2, registry_path=registry)
    msg = str(exc_info.value)
    assert "escalate for review" in msg
    assert "revoke is refused" in msg
    assert "resolve it before provisioning" not in msg
    # revoke really refuses (not "succeeds yet leaves the block"), so the block
    # persists and no self-service clear exists.
    with pytest.raises(PuffoV0RegistryError, match="revoke is refused"):
        revoke_runtime("runtime-a", registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="does not match the puffo-v0 profile"):
        provision_runtime("runtime-c", agent_dir, ws2, registry_path=registry)


def test_provision_fails_closed_on_an_unreadable_conflict_binding(tmp_path):
    # The no-silent-skip invariant AT the guard. An existing entry whose stored
    # binding cannot even be parsed cannot be proven NOT to occupy the target, so
    # it must not pass as "no conflict" (the old `continue`'s allow direction). It
    # fails closed with a distinct "unreadable binding" message -- even when the
    # target is an unrelated directory -- so a corrupt entry blocks provisioning
    # until it is resolved rather than being silently skipped.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Corrupt the binding itself (unparseable) without re-signing -> INTEGRITY_FAILED
    # and _parse_binding raises when the guard tries to read its identity.
    _mutate_registry_entry(registry, "runtime-a", resign=False, agent_dir_binding={"device": 1})
    other = tmp_path / "other"
    other.mkdir()
    (other / "init.json").write_text("{}", encoding="utf-8")
    workspace2 = tmp_path / "workspace2"
    workspace2.mkdir()
    with pytest.raises(PuffoV0RegistryError, match="unreadable binding"):
        provision_runtime("runtime-b", other, workspace2, registry_path=registry)


def test_discover_fails_closed_on_an_unreadable_entry_binding(tmp_path):
    # The no-silent-drop invariant AT discover. An entry whose stored binding cannot
    # be parsed has no device/inode to key on, so it cannot be attributed to any
    # walked directory. Dropping it would let a directory it actually holds be
    # reported `available` (the dangerous direction), so discover fails closed with
    # a distinct message rather than omit the entry.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(registry, "runtime-a", resign=False, agent_dir_binding={"device": 1})
    with pytest.raises(PuffoV0RegistryError, match="unreadable directory binding"):
        discover_runtimes(root, registry_path=registry)


def test_reuse_hint_does_not_change_the_decision_only_the_advisory_field(tmp_path):
    # Boris condition 一: the sign is JUST a sign, enforced not asserted. Two
    # `available` directories -- one whose path was reused (carries the hint), one
    # that never had an entry -- must be byte-identical in (status, runtime_id,
    # workspace) and BOTH must actually provision. The stored path reaches only the
    # advisory field, never a decision branch; if it ever leaked into
    # status/accept-reject, the two would diverge and this reds.
    root = tmp_path / "root"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    # Hinted directory: provision, then same-path replace so its path was reused
    # (move the recorded identity OUT of the root so only the recreated dir walks).
    hinted = root / "hinted"
    hinted.mkdir()
    (hinted / "init.json").write_text("{}", encoding="utf-8")
    provision_runtime("runtime-a", hinted, workspace, registry_path=registry)
    hinted.rename(tmp_path / "hinted-moved")
    hinted.mkdir()
    (hinted / "init.json").write_text("{}", encoding="utf-8")
    # Plain directory: never bound.
    plain = root / "plain"
    plain.mkdir()
    (plain / "init.json").write_text("{}", encoding="utf-8")

    cands = {c.agent_dir: c for c in discover_runtimes(root, registry_path=registry)}
    h, p = cands[hinted], cands[plain]
    # The decision is identical; ONLY the advisory field differs.
    assert (h.state, h.runtime_id, h.workspace) == (p.state, p.runtime_id, p.workspace)
    assert h.state is PuffoV0RuntimeState.PROVISIONABLE
    assert h.formerly_bound_runtime_id == "runtime-a"
    assert p.formerly_bound_runtime_id is None
    # accept/reject is identical too: both directories really provision.
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    ws3 = tmp_path / "ws3"
    ws3.mkdir()
    provision_runtime("runtime-h", hinted, ws2, registry_path=registry)
    provision_runtime("runtime-p", plain, ws3, registry_path=registry)


def test_released_directory_roster_is_revoked_only_and_shared():
    # Boris condition 三: "discover fail-closed set == guard block set" is a
    # set-equality claim, not a single member. Both occupancy checks route through
    # the one predicate _state_releases_directory, so the sets cannot diverge by
    # construction; this pins the roster itself -- only REVOKED releases -- over the
    # whole enum, so a state added later without deciding its membership reds here
    # rather than silently landing in `available`/no-conflict on one side only.
    from lingtai.adapters.acp.puffo_v0 import _state_releases_directory

    released = {state for state in PuffoV0RuntimeState if _state_releases_directory(state)}
    assert released == {PuffoV0RuntimeState.REVOKED}
    # The two sides that must agree (guard vs discover) both call the predicate;
    # the behavioral case for an unreadable binding is pinned by
    # test_provision_fails_closed_on_an_unreadable_conflict_binding /
    # test_discover_fails_closed_on_an_unreadable_entry_binding (an unsigned corrupt
    # binding is INTEGRITY -> both fail closed). A REVOKED entry can no longer have an
    # unreadable binding at all: a malformed binding payload classifies SHAPE_MISMATCH
    # ahead of the revoked gate, pinned by
    # test_a_malformed_binding_never_classifies_revoked_even_with_a_signed_revoked_status,
    # so the earlier "revoked + unreadable binding" asymmetry is unreachable, not
    # merely handled.


def test_reuse_hint_is_sourced_from_path_so_policy_drifted_replacement_is_flagged(
    tmp_path,
):
    # Option C's discriminating case, and why the hint must be sourced from the
    # stored PATH not from the STALE downgrade. The STALE downgrade only runs on an
    # ACTIVE entry; a POLICY_VERSION_MISMATCH entry whose directory is replaced at
    # the same path never becomes STALE, so a hint sourced from STALE entries would
    # leave the recreated directory bare `available` with no sign. Sourcing from
    # the path (any non-revoked entry) fires the sign here, while the identity
    # attribution still (correctly) reports the fresh directory PROVISIONABLE.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Drift the policy version (re-signed, so authentic -> POLICY_VERSION_MISMATCH,
    # NOT stale), then replace the directory at the same path with a fresh inode.
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, runtime_policy_version="puffo-v0.OLD"
    )
    agent_dir.rename(tmp_path / "identity-moved")   # move the drifted identity out of root
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.PROVISIONABLE   # fresh identity, provisionable
    assert candidate.formerly_bound_runtime_id == "runtime-a"     # but the sign is hung
    # And the sign is truthful: provisioning the recreated directory succeeds.
    workspace2 = tmp_path / "workspace2"
    workspace2.mkdir()
    provision_runtime("runtime-b", agent_dir, workspace2, registry_path=registry)


def test_a_malformed_binding_never_classifies_revoked_even_with_a_signed_revoked_status(
    tmp_path,
):
    # A REVOKED entry always has a well-formed binding payload: the binding-payload
    # shape check precedes the revoked gate, so a malformed binding classifies as a
    # blocking SHAPE_MISMATCH even when its ``status`` is a validly-signed "revoked".
    # This closes the old asymmetry (a REVOKED entry whose binding was unreadable)
    # at the source -- it can no longer arise -- and keeps discover's fail-closed set
    # equal to the guard's block set: both treat it as blocking, neither releases it.
    from lingtai.adapters.acp.puffo_v0 import _classify_registry_entry

    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # A signed entry with status="revoked" AND a malformed binding: the malformed
    # binding must win (SHAPE_MISMATCH), never REVOKED.
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, status="revoked", agent_dir_binding={"device": 1}
    )
    data = json.loads(registry.read_text(encoding="utf-8"))
    state = _classify_registry_entry("runtime-a", data["runtimes"]["runtime-a"], revoked_runtime_ids=frozenset())
    assert state is PuffoV0RuntimeState.SHAPE_MISMATCH
    assert state is not PuffoV0RuntimeState.REVOKED
    # Both sides treat it as blocking: discover fails closed (cannot key the entry),
    # and the guard fails closed (cannot read the binding) rather than releasing it.
    with pytest.raises(PuffoV0RegistryError, match="unreadable directory binding"):
        discover_runtimes(root, registry_path=registry)
    other = tmp_path / "other"
    other.mkdir()
    (other / "init.json").write_text("{}", encoding="utf-8")
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    with pytest.raises(PuffoV0RegistryError, match="unreadable binding"):
        provision_runtime("runtime-b", other, ws2, registry_path=registry)


def test_policy_version_drift_is_a_recoverable_state_not_integrity_failure(tmp_path):
    # The one auto-recoverable class: an authentic entry (digest re-signed) whose
    # only divergence is runtime_policy_version. It must NOT be read as tampering,
    # or the caller's revoke+re-provision recovery would run against a forged entry.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, runtime_policy_version="puffo-v0.OLD"
    )

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.POLICY_VERSION_MISMATCH
    assert candidate.state is not PuffoV0RuntimeState.INTEGRITY_FAILED
    # The runtime_id is surfaced: it is the escape from the dead-end (you must
    # know it to revoke), and the workspace is still an authentic binding.
    assert candidate.runtime_id == "runtime-a"
    assert candidate.workspace == workspace.resolve()
    with pytest.raises(PuffoV0RegistryError, match="different policy version"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_policy_drift_dead_end_is_sealed_at_both_discover_and_provision(tmp_path):
    # Boris's reproducible dead-end, now escapable. Before the fix: discover said
    # the dir was unbound while provision said "already bound to an active
    # runtime", and you could not revoke because discover hid the id.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, runtime_policy_version="puffo-v0.OLD"
    )

    # Discover half: the dir is surfaced as drifted, carrying the id to revoke.
    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.POLICY_VERSION_MISMATCH
    assert candidate.runtime_id == "runtime-a"

    # Provision half: still rejected (accept/reject set unchanged), but the reason
    # points at the revoke recovery instead of the misleading "another runtime".
    with pytest.raises(PuffoV0RegistryError) as exc_info:
        provision_runtime("runtime-b", agent_dir, workspace, registry_path=registry)
    message = str(exc_info.value)
    assert "revoke it before re-provisioning" in message
    assert "already bound to active runtime" not in message

    # And the escape actually works end to end: revoke the surfaced id, re-provision.
    revoke_runtime(candidate.runtime_id, registry_path=registry)
    runtime = provision_runtime("runtime-b", agent_dir, workspace, registry_path=registry)
    assert runtime.runtime_id == "runtime-b"


def test_integrity_failure_is_decided_by_digest_alone(tmp_path):
    # Boris's finding (1): break ONLY the digest, nothing that also trips the path
    # binding check, so the INTEGRITY_FAILED verdict cannot come from another gate.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(registry, "runtime-a", resign=False, entry_digest="0" * 64)

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.INTEGRITY_FAILED
    assert candidate.workspace is None
    with pytest.raises(PuffoV0RegistryError, match="integrity check"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_tampered_entry_that_also_drifts_version_is_integrity_failure(tmp_path):
    # The security-critical ordering: integrity is decided before version. Changing
    # runtime_policy_version WITHOUT re-signing both drifts the version AND breaks
    # the digest. If version were judged first this would look like the one
    # auto-recoverable state and the caller's automatic revoke would run against a
    # forged entry. It must be INTEGRITY_FAILED, never POLICY_VERSION_MISMATCH.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=False, runtime_policy_version="puffo-v0.OLD"
    )

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.INTEGRITY_FAILED
    assert candidate.state is not PuffoV0RuntimeState.POLICY_VERSION_MISMATCH
    with pytest.raises(PuffoV0RegistryError, match="integrity check"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_authentic_but_foreign_profile_is_shape_mismatch(tmp_path):
    # Digest re-signed over a changed tool_surface: authentic yet not our profile.
    # Distinct from integrity failure (the entry is validly signed) and from drift.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, tool_surface="operator_managed_partial"
    )

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.SHAPE_MISMATCH
    with pytest.raises(PuffoV0RegistryError, match="does not match the puffo-v0 profile"):
        resolve_runtime("runtime-a", registry_path=registry)


def test_discover_cli_representations_are_pairwise_distinct_across_states(
    tmp_path, monkeypatch, capsys
):
    # The invariant, checked at the boundary Puffo actually parses: run every
    # state through the real --json CLI path and assert no two share a
    # (status, runtime_id, workspace) triple. REVOKED / INTEGRITY_FAILED /
    # SHAPE_MISMATCH all carry (runtime_id="runtime-a", workspace=None), so if
    # cli_puffo_v0 ever re-derives status from `runtime_id is not None` (finding
    # (2)) they collapse to one triple and this fails -- which is the point.
    import lingtai.adapters.acp.puffo_v0 as puffo_v0
    import lingtai.cli_puffo_v0 as cli_puffo_v0

    def _triple_for(name, mutate):
        base = tmp_path / name
        base.mkdir()
        agent_dir = base / "identity"
        agent_dir.mkdir()
        (agent_dir / "init.json").write_text("{}", encoding="utf-8")
        workspace = base / "workspace"
        workspace.mkdir()
        registry = base / "registry.json"
        if mutate is not None:
            provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
            mutate(registry)
        monkeypatch.setattr(
            puffo_v0, "default_registry_path", lambda registry=registry: registry
        )
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        cli_puffo_v0.add_puffo_v0_parser(commands)
        args = parser.parse_args(["puffo-v0", "discover", "--root", str(base), "--json"])
        cli_puffo_v0.handle_puffo_v0_command(args)
        rows = json.loads(capsys.readouterr().out)["runtimes"]
        assert len(rows) == 1
        row = rows[0]
        return (row["status"], row["runtime_id"], row["workspace"])

    triples = [
        _triple_for("provisionable", None),
        _triple_for("active", lambda registry: None),
        _triple_for(
            "revoked",
            lambda registry: revoke_runtime("runtime-a", registry_path=registry),
        ),
        _triple_for(
            "drift",
            lambda registry: _mutate_registry_entry(
                registry, "runtime-a", resign=True, runtime_policy_version="puffo-v0.OLD"
            ),
        ),
        _triple_for(
            "integrity",
            lambda registry: _mutate_registry_entry(
                registry, "runtime-a", resign=False, entry_digest="0" * 64
            ),
        ),
        _triple_for(
            "shape",
            lambda registry: _mutate_registry_entry(
                registry, "runtime-a", resign=True, tool_surface="operator_managed_partial"
            ),
        ),
        _triple_for("stale", _make_agent_dir_binding_owner_stale),
    ]
    assert len(set(triples)) == len(triples), triples
    # Coverage, not just distinctness: the cases must exercise every enum member,
    # so a state added later without a case is caught here rather than silently
    # escaping the invariant. Adding a member without a case reddens this.
    observed_statuses = {status for status, _runtime_id, _workspace in triples}
    assert observed_statuses == {state.value for state in PuffoV0RuntimeState}


def test_resolve_state_messages_cover_every_rejectable_state():
    # resolve_runtime looks a non-active state up in _RESOLVE_STATE_MESSAGES; a
    # member the classifier can return but the map omits would raise a bare
    # KeyError on the execution path instead of PuffoV0RegistryError. PROVISIONABLE
    # is discover-only (never returned for an existing entry); ACTIVE resolves.
    from lingtai.adapters.acp.puffo_v0 import _RESOLVE_STATE_MESSAGES

    rejectable = set(PuffoV0RuntimeState) - {
        PuffoV0RuntimeState.ACTIVE,
        PuffoV0RuntimeState.PROVISIONABLE,
    }
    assert set(_RESOLVE_STATE_MESSAGES) == rejectable


def test_discovery_state_precedence_ranks_every_rankable_state_exactly_once():
    # Completeness of the multi-entry precedence: every state that can be
    # attributed to a directory must be rankable, or _state_rank raises a bare
    # ValueError from inside discovery for the unranked one. PROVISIONABLE is the
    # no-entry state and never competes for a directory, so it is excluded.
    # Adding a member to the enum without placing it in _DISCOVERY_STATE_PRECEDENCE
    # reddens this.
    from lingtai.adapters.acp.puffo_v0 import _DISCOVERY_STATE_PRECEDENCE

    rankable = set(PuffoV0RuntimeState) - {PuffoV0RuntimeState.PROVISIONABLE}
    assert set(_DISCOVERY_STATE_PRECEDENCE) == rankable
    # No duplicates: a repeated member would make _state_rank order-dependent on
    # which index .index() happens to return.
    assert len(_DISCOVERY_STATE_PRECEDENCE) == len(rankable)


def test_discovery_state_precedence_orders_integrity_above_every_recoverable_state():
    # The safety order is pinned here, not left to the enum's declaration order:
    # a directory holding both a tampered (INTEGRITY_FAILED) entry and a revoked
    # or drifted one must be reported as the integrity failure ("stop, escalate,
    # never auto-revoke"), never as the recoverable state ("re-provision"). A
    # cosmetic reorder of _DISCOVERY_STATE_PRECEDENCE -- exactly the edit that
    # would silently reopen the auto-revoke-a-tampered-entry hole -- reddens this.
    from lingtai.adapters.acp.puffo_v0 import _DISCOVERY_STATE_PRECEDENCE, _state_rank

    # Full sequence, most-constraining first.
    assert _DISCOVERY_STATE_PRECEDENCE == (
        PuffoV0RuntimeState.INTEGRITY_FAILED,
        PuffoV0RuntimeState.SHAPE_MISMATCH,
        PuffoV0RuntimeState.POLICY_VERSION_MISMATCH,
        PuffoV0RuntimeState.STALE_BINDING,
        PuffoV0RuntimeState.ACTIVE,
        PuffoV0RuntimeState.REVOKED,
    )
    # The load-bearing inequalities, stated independently of the tuple literal so
    # the security intent survives even if the sequence above is ever revised:
    # integrity outranks (is more constraining than) every state whose recovery
    # would mutate the registry.
    for recoverable in (
        PuffoV0RuntimeState.SHAPE_MISMATCH,
        PuffoV0RuntimeState.POLICY_VERSION_MISMATCH,
        PuffoV0RuntimeState.STALE_BINDING,
        PuffoV0RuntimeState.ACTIVE,
        PuffoV0RuntimeState.REVOKED,
    ):
        assert _state_rank(PuffoV0RuntimeState.INTEGRITY_FAILED) < _state_rank(recoverable)
    # A stale binding is authentic-but-unusable: it must outrank ACTIVE so a
    # directory that also has a usable entry is never reported usable while the
    # stale one stands, and stay below the tamper/shape/drift states.
    assert _state_rank(PuffoV0RuntimeState.STALE_BINDING) < _state_rank(
        PuffoV0RuntimeState.ACTIVE
    )
    assert _state_rank(PuffoV0RuntimeState.POLICY_VERSION_MISMATCH) < _state_rank(
        PuffoV0RuntimeState.STALE_BINDING
    )
    # And a drifted (auto-revoke-recoverable) occupant must never outrank a
    # revoked one in a way that hides the drift's recovery id.
    assert _state_rank(PuffoV0RuntimeState.POLICY_VERSION_MISMATCH) < _state_rank(
        PuffoV0RuntimeState.REVOKED
    )


def test_discovery_reports_most_constraining_state_when_one_dir_has_several_entries(
    tmp_path,
):
    # Pins the CONSUMER of the safety order, not just the table: when one agent_dir
    # is named by both a REVOKED and an INTEGRITY_FAILED entry, discovery must
    # report the integrity failure ("stop, escalate, never auto-revoke"), never the
    # revoked one ("re-provision under a new id"). This is the concrete harm the
    # precedence exists to prevent; it stays reachable if _discovery_records reads
    # the order the wrong way. Reddens under both `_state_rank(...) < ...` -> `>`
    # and deletion of the most-constraining-wins block.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace_a = tmp_path / "workspace-a"
    workspace_a.mkdir()
    workspace_b = tmp_path / "workspace-b"
    workspace_b.mkdir()
    registry = tmp_path / "registry.json"
    # First occupant: provisioned then revoked -> a REVOKED entry for agent_dir.
    provision_runtime("runtime-a", agent_dir, workspace_a, registry_path=registry)
    revoke_runtime("runtime-a", registry_path=registry)
    # Second occupant on the SAME agent_dir: the revoked one no longer blocks it.
    provision_runtime("runtime-b", agent_dir, workspace_b, registry_path=registry)
    # Tamper the live one so agent_dir now carries REVOKED + INTEGRITY_FAILED.
    _mutate_registry_entry(registry, "runtime-b", resign=False, entry_digest="0" * 64)

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.INTEGRITY_FAILED
    assert candidate.state is not PuffoV0RuntimeState.REVOKED
    assert candidate.runtime_id == "runtime-b"
    assert candidate.workspace is None


def test_discovery_prefers_stale_binding_over_active_on_the_same_directory(tmp_path):
    # Pins the NEW rank edge STALE_BINDING < ACTIVE at the consumer, not just the
    # table: one agent_dir named by both a stale-binding entry and a genuinely
    # usable one must report `stale_binding` -- reporting `bound` would send the
    # caller to a runtime whose sibling entry is broken. Under identity keying both
    # entries must share the SAME stored device/inode to compete for the directory,
    # so the stale one is forged with the same device/inode but a wrong owner:
    # authentic (re-signed, not INTEGRITY_FAILED), keyed to the same directory, yet
    # its full identity no longer matches the live lstat -> STALE_BINDING. runtime-b
    # is provisioned normally (ACTIVE, full binding matches live).
    # Reddens under `_state_rank(...) < ...` -> `>`.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-b", agent_dir, workspace, registry_path=registry)

    data = json.loads(registry.read_text(encoding="utf-8"))
    clone = dict(data["runtimes"]["runtime-b"])
    clone["runtime_id"] = "runtime-a"
    wrong = dict(clone["agent_dir_binding"])
    wrong["owner"] = wrong["owner"] + 1          # same device/inode, wrong owner -> stale
    clone["agent_dir_binding"] = wrong
    canonical = {k: v for k, v in clone.items() if k != "entry_digest"}
    clone["entry_digest"] = _digest(canonical)   # authentic, so not INTEGRITY_FAILED
    # runtime-b first so discovery sees ACTIVE, then the stale entry must win.
    data["runtimes"] = {"runtime-b": data["runtimes"]["runtime-b"], "runtime-a": clone}
    registry.write_text(json.dumps(data), encoding="utf-8")

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.STALE_BINDING
    assert candidate.state is not PuffoV0RuntimeState.ACTIVE
    # Both entries share the directory's device/inode; the stale forgery wins the
    # rank, and its own id is surfaced so the broken binding can be revoked.
    assert candidate.runtime_id == "runtime-a"
    assert candidate.workspace is None


def test_discovery_rejects_two_active_runtimes_on_one_directory(tmp_path):
    # The historical hard-corruption guard: two *active* entries cannot bind one
    # agent_dir. provision refuses to create the second, so this is only reachable
    # by a directly forged registry -- built here by re-signing a copy under a new
    # id -- and discovery must raise rather than silently pick one.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)

    data = json.loads(registry.read_text(encoding="utf-8"))
    clone = dict(data["runtimes"]["runtime-a"])
    clone["runtime_id"] = "runtime-b"
    canonical = {k: v for k, v in clone.items() if k != "entry_digest"}
    clone["entry_digest"] = _digest(canonical)
    data["runtimes"]["runtime-b"] = clone
    registry.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PuffoV0RegistryError, match="multiple active runtimes"):
        discover_runtimes(root, registry_path=registry)


def test_discover_fails_closed_on_an_unreadable_workspace_binding_like_the_guard(tmp_path):
    # Peter/Boris bug 1: the guard parses BOTH stored bindings (agent_dir AND
    # workspace) before any identity comparison and fails closed on either, so a
    # single unreadable workspace_binding makes it reject EVERY provision -- the
    # whole registry is unprovisionable while that entry stands. discover only read
    # agent_dir, so it kept reporting OTHER directories `available` that could not
    # in fact be provisioned. discover must fail closed on the same both-field set.
    # Assert the PAIR the way Boris framed it: not "b provisions" but "no `available`
    # holds while the bad entry stands" -- a brand-new directory + workspace is
    # refused too. Reddens if the workspace_binding parse is dropped from discovery.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    # A second, genuinely unbound initialized directory: pre-fix discovery reported
    # THIS one `available` even though provisioning it was impossible.
    other = root / "other"
    other.mkdir()
    (other / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    # Corrupt ONLY the workspace binding (agent_dir stays keyable) without resigning
    # -> the entry is INTEGRITY_FAILED and its workspace_binding is unparseable.
    _mutate_registry_entry(
        registry, "runtime-a", resign=False, workspace_binding={"device": 1}
    )
    with pytest.raises(PuffoV0RegistryError, match="unreadable directory binding"):
        discover_runtimes(root, registry_path=registry)
    # The operation discover is supposed to predict: a completely fresh directory +
    # workspace is still refused, so there was no truthful `available` to report.
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    (fresh_dir / "init.json").write_text("{}", encoding="utf-8")
    fresh_ws = tmp_path / "fresh-ws"
    fresh_ws.mkdir()
    with pytest.raises(PuffoV0RegistryError, match="unreadable binding"):
        provision_runtime("runtime-b", fresh_dir, fresh_ws, registry_path=registry)


def test_invalid_runtime_id_is_shape_mismatch_not_bound_because_resolve_rejects_it(
    tmp_path,
):
    # Peter/Boris bug 2: a valid digest over a syntactically illegal runtime id (key
    # and entry.runtime_id agree) classified as ACTIVE, so discover reported `bound`
    # -- but resolve_runtime rejects the id outright (`_valid_runtime_id`), so `bound`
    # promised a resolve that always fails. The classifier now validates runtime-id
    # syntax, so discover reports SHAPE_MISMATCH instead. Assert the PAIR (discover
    # state AND resolve), then prove the SHAPE_MISMATCH guard message is HONEST on
    # this new code path by EXECUTION -- the 190618 trap was string-asserting a cell
    # that was never run.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("valid-id", agent_dir, workspace, registry_path=registry)
    # Rekey the (otherwise authentic) entry to an illegal id and RE-SIGN it, so the
    # digest is valid and only the id syntax is wrong -- exactly Peter's fixture.
    data = json.loads(registry.read_text(encoding="utf-8"))
    entry = data["runtimes"].pop("valid-id")
    entry["runtime_id"] = "invalid/runtime"
    canonical = {k: v for k, v in entry.items() if k != "entry_digest"}
    entry["entry_digest"] = _digest(canonical)
    data["runtimes"]["invalid/runtime"] = entry
    registry.write_text(json.dumps(data), encoding="utf-8")

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.SHAPE_MISMATCH
    assert candidate.state is not PuffoV0RuntimeState.ACTIVE     # never `bound`
    with pytest.raises(PuffoV0RegistryError, match="opaque local identifier"):
        resolve_runtime("invalid/runtime", registry_path=registry)

    # The SHAPE_MISMATCH conflict message names escalation and states that revoke is
    # refused. Prove that is TRUE here, not merely asserted: provisioning the SAME
    # directory hits the shape conflict, revoke cannot even reach the entry (the id
    # is rejected first), and a repeat provision still blocks. The invalid-id term
    # now sits BEFORE the revoked gate (with the rest of value-shape), so a signed
    # ``status="revoked"`` can no longer launder it into a released state.
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    with pytest.raises(
        PuffoV0RegistryError, match="does not match the puffo-v0 profile"
    ) as exc_info:
        provision_runtime("runtime-b", agent_dir, ws2, registry_path=registry)
    assert "revoke is refused" in str(exc_info.value)
    with pytest.raises(PuffoV0RegistryError, match="opaque local identifier"):
        revoke_runtime("invalid/runtime", registry_path=registry)     # revoke is a dead end
    with pytest.raises(PuffoV0RegistryError, match="does not match the puffo-v0 profile"):
        provision_runtime("runtime-c", agent_dir, ws2, registry_path=registry)


def _matrix_healthy(base):
    """One bound runtime + one initialized-but-unbound directory (the control)."""
    root = base / "root"
    root.mkdir()
    bound_dir = root / "bound"
    bound_dir.mkdir()
    (bound_dir / "init.json").write_text("{}", encoding="utf-8")
    unbound_dir = root / "unbound"
    unbound_dir.mkdir()
    (unbound_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    registry = base / "registry.json"
    provision_runtime("runtime-a", bound_dir, workspace, registry_path=registry)
    return root, registry


def _matrix_corrupt_workspace_binding(base):
    """Bug 1: unreadable workspace_binding + a second unbound directory."""
    root = base / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    other = root / "other"
    other.mkdir()
    (other / "init.json").write_text("{}", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    registry = base / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=False, workspace_binding={"device": 1}
    )
    return root, registry


def _matrix_corrupt_agent_dir_binding(base):
    """Unreadable agent_dir_binding (discover already fails closed here)."""
    root = base / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    registry = base / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=False, agent_dir_binding={"device": 1}
    )
    return root, registry


def _matrix_invalid_runtime_id(base):
    """Bug 2: valid digest, illegal runtime-id syntax (key == entry.runtime_id)."""
    root = base / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    registry = base / "registry.json"
    provision_runtime("valid-id", agent_dir, workspace, registry_path=registry)
    data = json.loads(registry.read_text(encoding="utf-8"))
    entry = data["runtimes"].pop("valid-id")
    entry["runtime_id"] = "invalid/runtime"
    canonical = {k: v for k, v in entry.items() if k != "entry_digest"}
    entry["entry_digest"] = _digest(canonical)
    data["runtimes"]["invalid/runtime"] = entry
    registry.write_text(json.dumps(data), encoding="utf-8")
    return root, registry


def _matrix_same_path_replacement(base):
    """A reused agent_dir path: recreated dir is `available`, moved one is stale."""
    root = base / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    registry = base / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    agent_dir.rename(root / "identity-moved")
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    return root, registry


def _matrix_stale_workspace(base):
    """A moved workspace: discover reports stale_binding (neither bound nor available)."""
    root = base / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    registry = base / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    workspace.rename(base / "workspace-moved")
    workspace.mkdir()
    return root, registry


def test_discover_promises_hold_across_a_damaged_entry_matrix(tmp_path):
    # Boris's crown recommendation, mechanized: for EVERY discover output, `bound`
    # must resolve and `available` must provision -- the #1622 invariant itself.
    # The two bugs Peter found lived on axes a per-state scenario table cannot reach
    # (which binding FIELD, which validity PREDICATE each side reads), so instead of
    # enumerating that table this pins the end-to-end promise over a matrix of
    # damaged entries. On the PRE-FIX code the damaged fixtures made discover emit
    # `available`/`bound` that then failed the follow-on op (RED); post-fix each
    # either fails closed (promised nothing) or reports a non-usable state.
    #
    # Fail-closed is an acceptable outcome, so a broken fixture passing silently is a
    # risk -- the `healthy` and `same_path_replacement` controls defeat it by
    # exercising BOTH promise branches positively, asserted at the end, so the matrix
    # cannot pass by discover raising on everything.
    fixtures = [
        ("healthy", _matrix_healthy, True),
        ("corrupt_workspace_binding", _matrix_corrupt_workspace_binding, False),
        ("corrupt_agent_dir_binding", _matrix_corrupt_agent_dir_binding, False),
        ("invalid_runtime_id", _matrix_invalid_runtime_id, False),
        ("same_path_replacement", _matrix_same_path_replacement, True),
        ("stale_workspace", _matrix_stale_workspace, False),
    ]
    saw_bound = 0
    saw_available = 0
    for name, build, must_report in fixtures:
        base = tmp_path / name
        base.mkdir()
        root, registry = build(base)
        try:
            candidates = discover_runtimes(root, registry_path=registry)
        except PuffoV0RegistryError:
            # Fail-closed: discover promised nothing, so no promise can be broken.
            assert not must_report, f"{name}: a healthy fixture must not fail closed"
            continue
        tombstone = registry.with_name(f".{registry.name}.revocations.jsonl")
        for candidate in candidates:
            if candidate.state is PuffoV0RuntimeState.ACTIVE:
                saw_bound += 1
                resolve_runtime(candidate.runtime_id, registry_path=registry)  # `bound` => resolvable
            elif candidate.state is PuffoV0RuntimeState.PROVISIONABLE:
                saw_available += 1
                # `available` => provisionable. Provision mutates the registry, so
                # snapshot + restore around each attempt, with a fresh id and a fresh
                # workspace outside every recorded one (advisor: else a legitimate
                # workspace conflict would red the test for the wrong reason).
                registry_snapshot = registry.read_bytes()
                tombstone_snapshot = (
                    tombstone.read_bytes() if tombstone.exists() else None
                )
                probe_ws = base / f"probe-ws-{saw_available}"
                probe_ws.mkdir()
                provision_runtime(
                    f"probe-{saw_available}",
                    candidate.agent_dir,
                    probe_ws,
                    registry_path=registry,
                )
                registry.write_bytes(registry_snapshot)
                if tombstone_snapshot is None:
                    tombstone.unlink(missing_ok=True)
                else:
                    tombstone.write_bytes(tombstone_snapshot)
    # Non-degeneracy: both promise branches were actually exercised by the controls.
    assert saw_bound >= 1, "the matrix never checked a `bound => resolve` promise"
    assert saw_available >= 1, "the matrix never checked an `available => provision` promise"


# Boris's damaged-entry roster (msg_63a298f5), adapted to parametrized fixtures. The
# list itself is the "roster": adding a classifier/shape check means adding a row
# here, so a future check can never be tested against only the one subtype someone
# reproduced by hand -- the failure that cost round 190632. resign=True reflects the
# threat model in force everywhere in this suite: _digest is keyless, so anyone with
# write access can produce a "signed" edit; no secret is needed.
def _matrix_rekey(entry, data, new_id):
    entry["runtime_id"] = new_id
    data["runtimes"].pop("runtime-a")
    data["runtimes"][new_id] = entry


_DAMAGED_ENTRY_CASES = [
    ("status=disabled",             lambda e, d: e.update(status="disabled"),                 True),
    ("status=empty",                lambda e, d: e.update(status=""),                         True),
    ("status missing",              lambda e, d: e.pop("status", None),                       True),
    ("status non-str",              lambda e, d: e.update(status=123),                        True),
    ("runtime_id invalid syntax",   lambda e, d: _matrix_rekey(e, d, "invalid/runtime"),      True),
    ("runtime_id key mismatch",     lambda e, d: e.update(runtime_id="other"),                True),
    ("profile foreign",             lambda e, d: e.update(profile="not-puffo-v0"),            True),
    ("mcp_servers non-empty",       lambda e, d: e.update(mcp_servers=["x"]),                 True),
    ("tool_surface wrong",          lambda e, d: e.update(tool_surface=["nope"]),             True),
    ("turn_origins wrong",          lambda e, d: e.update(turn_origins=["nope"]),             True),
    ("agent_dir non-str",           lambda e, d: e.update(agent_dir=5),                       True),
    ("workspace non-str",           lambda e, d: e.update(workspace=5),                       True),
    ("agent_dir_binding broken",    lambda e, d: e.update(agent_dir_binding={"device": 1}),   True),
    ("workspace_binding broken",    lambda e, d: e.update(workspace_binding={"device": 1}),   True),
    ("agent_dir_binding wrong ino", lambda e, d: e["agent_dir_binding"].update(inode=999999), True),
    ("workspace_binding wrong ino", lambda e, d: e["workspace_binding"].update(inode=999999), True),
    ("entry_digest missing",        lambda e, d: e.pop("entry_digest", None),                 False),
    ("entry_digest wrong",          lambda e, d: e.update(entry_digest="0" * 64),             False),
    ("entry_digest non-str",        lambda e, d: e.update(entry_digest=None),                 False),
    ("extra key",                   lambda e, d: e.update(unexpected=True),                   True),
    ("policy drift",                lambda e, d: e.update(runtime_policy_version="OLD"),      True),
]
_MATRIX_IDS = [case[0] for case in _DAMAGED_ENTRY_CASES]

# The only cases that leave a legitimately revocable classification (ACTIVE or
# POLICY_VERSION_MISMATCH). A wrong-inode binding keeps a WELL-FORMED payload -- a
# liveness mismatch, not a shape defect -- so the pure classifier rules it ACTIVE.
# Every other case is a blocking state whose revoke must be refused with zero writes.
_MATRIX_REVOCABLE = {
    "agent_dir_binding wrong ino",
    "workspace_binding wrong ino",
    "policy drift",
}


def _seed_matrix_registry(base):
    base.mkdir()
    registry = base / "registry.json"
    root = base / "root"
    root.mkdir()
    bound_dir = root / "bound"
    bound_dir.mkdir()
    (bound_dir / "init.json").write_text("{}", encoding="utf-8")
    free_dir = root / "free"
    free_dir.mkdir()
    (free_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = base / "ws"
    workspace.mkdir()
    provision_runtime("runtime-a", bound_dir, workspace, registry_path=registry)
    return registry, root, bound_dir


def _apply_matrix_case(registry, mutate, resign):
    data = json.loads(registry.read_text(encoding="utf-8"))
    entry = data["runtimes"]["runtime-a"]
    mutate(entry, data)
    if resign:  # keyless _digest: a "signed" edit is available to anyone with write access
        canonical = {k: v for k, v in entry.items() if k != "entry_digest"}
        entry["entry_digest"] = _digest(canonical)
    registry.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("label,mutate,resign", _DAMAGED_ENTRY_CASES, ids=_MATRIX_IDS)
def test_damaged_entry_never_breaks_a_discover_promise(tmp_path, label, mutate, resign):
    # Boris's crown invariant, swept over the full roster: for EVERY discover output,
    # `bound` must resolve and `available` must provision, and a non-bound state must
    # never expose a workspace path that no longer exists (#7). Discover raising is an
    # acceptable outcome -- it promised nothing, so neither implication can break.
    registry, root, _bound_dir = _seed_matrix_registry(tmp_path / "case")
    _apply_matrix_case(registry, mutate, resign)
    try:
        candidates = discover_runtimes(root, registry_path=registry)
    except PuffoV0RegistryError:
        return
    for probe_n, candidate in enumerate(candidates):
        if candidate.state is PuffoV0RuntimeState.ACTIVE:
            resolve_runtime(candidate.runtime_id, registry_path=registry)  # bound => resolvable
        elif candidate.state is PuffoV0RuntimeState.PROVISIONABLE:
            snapshot = registry.read_bytes()
            probe_ws = tmp_path / f"probe-ws-{probe_n}"
            probe_ws.mkdir()
            provision_runtime(  # available => provisionable
                f"probe-{probe_n}", candidate.agent_dir, probe_ws, registry_path=registry
            )
            registry.write_bytes(snapshot)
        if candidate.state is not PuffoV0RuntimeState.ACTIVE and candidate.workspace is not None:
            assert candidate.workspace.exists(), (
                f"{label}: {candidate.state.value} exposed a workspace that does not "
                f"exist: {candidate.workspace}"
            )


@pytest.mark.parametrize("label,mutate,resign", _DAMAGED_ENTRY_CASES, ids=_MATRIX_IDS)
def test_revoke_refuses_and_preserves_a_blocking_entry(tmp_path, label, mutate, resign):
    # Peter's full-row invariant over the roster (190664) plus Boris's tombstone-
    # ordering trap (190662): revoke either legitimately succeeds (only for a
    # classification that is ACTIVE or policy-drift) or is REFUSED with ZERO
    # persistence. The refusal path is the dangerous one: a tombstone is appended
    # before the entry is touched, and a tombstone ALONE releases the directory, so a
    # check placed after it would report an error while irreversibly releasing
    # identity. Registry bytes AND the tombstone's bytes/line-count are therefore
    # asserted SEPARATELY, and the classification + occupancy must be unchanged.
    from lingtai.adapters.acp.puffo_v0 import _classify_registry_entry

    registry, _root, bound_dir = _seed_matrix_registry(tmp_path / "case")
    _apply_matrix_case(registry, mutate, resign)
    tombstone = registry.with_name(f".{registry.name}.revocations.jsonl")

    def _classify():
        data = json.loads(registry.read_text(encoding="utf-8"))
        rid = next(iter(data["runtimes"]))
        return rid, _classify_registry_entry(
            rid, data["runtimes"][rid], revoked_runtime_ids=frozenset()
        )

    rid, before = _classify()
    if label in _MATRIX_REVOCABLE:
        assert before in (
            PuffoV0RuntimeState.ACTIVE,
            PuffoV0RuntimeState.POLICY_VERSION_MISMATCH,
        )
        revoke_runtime(rid, registry_path=registry)  # a live/drifted runtime is revocable
        _rid, after = _classify()
        assert after is PuffoV0RuntimeState.REVOKED
        return
    # A blocking entry: revoke must refuse and change nothing.
    assert before in (
        PuffoV0RuntimeState.INTEGRITY_FAILED,
        PuffoV0RuntimeState.SHAPE_MISMATCH,
    )
    registry_before = registry.read_bytes()
    tombstone_before = tombstone.read_bytes()
    with pytest.raises(PuffoV0RegistryError):
        revoke_runtime(rid, registry_path=registry)
    assert registry.read_bytes() == registry_before                    # registry byte-identical
    assert tombstone.read_bytes() == tombstone_before                  # tombstone byte-identical
    assert tombstone_before.count(b"\n") == tombstone.read_bytes().count(b"\n")  # no appended line
    _rid, after = _classify()
    assert after is before                                             # still the same blocking state
    ws2 = tmp_path / "still-blocked-ws"
    ws2.mkdir()
    with pytest.raises(PuffoV0RegistryError):  # identity still occupied, not released
        provision_runtime("probe-after", bound_dir, ws2, registry_path=registry)


def test_provision_conflict_selection_is_independent_of_registry_order(tmp_path):
    # #5: a directory named by both an active and an integrity-failed entry must
    # report the integrity failure (recovery: escalate), never the active one
    # (recovery: revoke), regardless of registry insertion order -- discover already
    # picks by precedence, and the guard must too, or the two hand the caller
    # contradictory recoveries for the same directory. Build the two-entry registry in
    # BOTH orders and assert the guard raises the identical message.
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def _guard_message(registry, *, integrity_first):
        provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
        data = json.loads(registry.read_text(encoding="utf-8"))
        active = data["runtimes"]["runtime-a"]
        integrity = dict(active)
        integrity["runtime_id"] = "runtime-b"
        integrity["entry_digest"] = "0" * 64  # break digest -> INTEGRITY_FAILED, same agent_dir_binding
        ordered = (
            {"runtime-b": integrity, "runtime-a": active}
            if integrity_first
            else {"runtime-a": active, "runtime-b": integrity}
        )
        data["runtimes"] = ordered
        registry.write_text(json.dumps(data), encoding="utf-8")
        ws2 = registry.with_name("ws2")
        ws2.mkdir()
        with pytest.raises(PuffoV0RegistryError) as exc_info:
            provision_runtime("runtime-c", agent_dir, ws2, registry_path=registry)
        return str(exc_info.value)

    # provision_runtime creates the registry's parent directory, so the two runs live
    # in separate subdirectories and cannot see each other's registry.
    integrity_first = _guard_message(tmp_path / "a" / "registry.json", integrity_first=True)
    active_first = _guard_message(tmp_path / "b" / "registry.json", integrity_first=False)
    assert integrity_first == active_first                  # order-independent selection
    assert "failed its integrity check" in integrity_first  # the integrity entry, not the active one
    assert "runtime-b" in integrity_first


def test_policy_drift_with_a_deleted_workspace_reports_no_workspace(tmp_path):
    # #7: a policy-drifted entry whose workspace was deleted must not report the dead
    # workspace path -- a reported workspace names a LIVE binding. The agent_dir is
    # intact so discover still classifies policy_version_mismatch, but workspace=None.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, runtime_policy_version="puffo-v0.OLD"
    )
    shutil.rmtree(workspace)

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.POLICY_VERSION_MISMATCH
    assert candidate.runtime_id == "runtime-a"
    assert candidate.workspace is None


def test_unknown_signed_status_is_blocking_not_released(tmp_path):
    # #3: a validly-signed but unknown `status` (e.g. "disabled") must NOT be read as
    # REVOKED / released -- that is the allow direction. It classifies as a blocking
    # shape_mismatch, discover never reports it revoked, the directory stays occupied,
    # and revoke refuses it.
    from lingtai.adapters.acp.puffo_v0 import _classify_registry_entry

    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(registry, "runtime-a", resign=True, status="disabled")

    data = json.loads(registry.read_text(encoding="utf-8"))
    state = _classify_registry_entry(
        "runtime-a", data["runtimes"]["runtime-a"], revoked_runtime_ids=frozenset()
    )
    assert state is PuffoV0RuntimeState.SHAPE_MISMATCH
    assert state is not PuffoV0RuntimeState.REVOKED
    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.SHAPE_MISMATCH
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    with pytest.raises(PuffoV0RegistryError, match="does not match the puffo-v0 profile"):
        provision_runtime("runtime-b", agent_dir, ws2, registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="revoke is refused"):
        revoke_runtime("runtime-a", registry_path=registry)


def test_missing_entry_digest_is_integrity_not_shape(tmp_path):
    # #4 (contract alignment): a missing entry_digest is an integrity failure, not a
    # shape mismatch. Decided before the structural key-set check on purpose -- an
    # unauthenticated record must read as integrity, and with the write-side admission
    # revoke cannot re-add the digest to launder it into a released state.
    from lingtai.adapters.acp.puffo_v0 import _classify_registry_entry

    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["runtimes"]["runtime-a"].pop("entry_digest")
    registry.write_text(json.dumps(data), encoding="utf-8")

    reloaded = json.loads(registry.read_text(encoding="utf-8"))["runtimes"]["runtime-a"]
    state = _classify_registry_entry("runtime-a", reloaded, revoked_runtime_ids=frozenset())
    assert state is PuffoV0RuntimeState.INTEGRITY_FAILED
    assert state is not PuffoV0RuntimeState.SHAPE_MISMATCH
    with pytest.raises(PuffoV0RegistryError, match="failed its integrity check"):
        resolve_runtime("runtime-a", registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="revoke is refused"):
        revoke_runtime("runtime-a", registry_path=registry)


def test_digest_valid_nul_path_surfaces_a_registry_error_not_a_raw_valueerror(tmp_path):
    # Hardening: a digest-valid entry whose stored path contains an embedded NUL must
    # surface a bounded PuffoV0RegistryError, not a raw ValueError from resolve()/lstat
    # (which is neither OSError nor RuntimeError, so it used to propagate). pytest.raises
    # (PuffoV0RegistryError) does NOT match a plain ValueError, so this reddens if the
    # wrap is removed.
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, agent_dir=str(agent_dir) + "\x00evil"
    )
    with pytest.raises(PuffoV0RegistryError):
        resolve_runtime("runtime-a", registry_path=registry)


def test_matrix_revocable_set_matches_classification(tmp_path):
    # Guards _MATRIX_REVOCABLE against silent drift. The revocable set the
    # parametrized revoke test branches on must EQUAL the roster labels that actually
    # classify ACTIVE or POLICY_VERSION_MISMATCH -- otherwise a future edit that made,
    # say, `policy drift` classify SHAPE would leave that test green (it would just
    # take the other branch) and hide a real behavior change.
    from lingtai.adapters.acp.puffo_v0 import _classify_registry_entry

    revocable = set()
    for index, (label, mutate, resign) in enumerate(_DAMAGED_ENTRY_CASES):
        registry, _root, _bound = _seed_matrix_registry(tmp_path / f"case-{index}")
        _apply_matrix_case(registry, mutate, resign)
        data = json.loads(registry.read_text(encoding="utf-8"))
        rid = next(iter(data["runtimes"]))
        state = _classify_registry_entry(
            rid, data["runtimes"][rid], revoked_runtime_ids=frozenset()
        )
        if state in (
            PuffoV0RuntimeState.ACTIVE,
            PuffoV0RuntimeState.POLICY_VERSION_MISMATCH,
        ):
            revocable.add(label)
    assert revocable == _MATRIX_REVOCABLE


def test_policy_drift_with_a_replaced_workspace_reports_no_workspace(tmp_path):
    # #7, tightened past mere existence: a reported workspace names a LIVE binding.
    # Replace the drifted entry's workspace with a FRESH directory at the same path
    # (new inode) -- the path exists but no longer holds the provisioned identity, so
    # discover must report workspace=None. Proves the check is a binding match, not an
    # os.path.exists.
    root = tmp_path / "root"
    root.mkdir()
    agent_dir = root / "identity"
    agent_dir.mkdir()
    (agent_dir / "init.json").write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = tmp_path / "registry.json"
    provision_runtime("runtime-a", agent_dir, workspace, registry_path=registry)
    _mutate_registry_entry(
        registry, "runtime-a", resign=True, runtime_policy_version="puffo-v0.OLD"
    )
    workspace.rename(tmp_path / "workspace-moved")
    workspace.mkdir()  # fresh directory, same path, new inode -> exists but wrong identity

    candidate = _discover_one(root, registry)
    assert candidate.state is PuffoV0RuntimeState.POLICY_VERSION_MISMATCH
    assert candidate.workspace is None


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
    # Editing a signed field without re-signing breaks the digest; integrity is
    # decided before any path-binding check, so the rejection names the digest.
    with pytest.raises(PuffoV0RegistryError, match="integrity check"):
        resolve_runtime("opaque-id", registry_path=registry)

    data = json.loads(registry.read_text(encoding="utf-8"))
    data["runtimes"]["opaque-id"]["unexpected"] = True
    registry.write_text(json.dumps(data), encoding="utf-8")
    # An extra key changes the entry shape, decided before the digest check.
    with pytest.raises(PuffoV0RegistryError, match="does not match the puffo-v0 profile"):
        resolve_runtime("opaque-id", registry_path=registry)

    other_agent_dir = tmp_path / "other-identity"
    other_agent_dir.mkdir()
    (other_agent_dir / "init.json").write_text("{}", encoding="utf-8")
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    provision_runtime("active-id", other_agent_dir, other_workspace, registry_path=registry)
    revoke_runtime("active-id", registry_path=registry)
    with pytest.raises(PuffoV0RegistryError, match="has been revoked"):
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

    # The stale snapshot is self-consistent (its digest still matches), so
    # integrity passes and the tombstone -- which the snapshot restore cannot
    # erase -- is what denies it: a revoked id, never a reusable one.
    with pytest.raises(PuffoV0RegistryError, match="has been revoked"):
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


class _CompositionAgent:
    """Minimal Agent stand-in for driving the real ``run_acp`` composition.

    Records the MCP configs handed to ``mount_session_mcp_stdio`` so a test can
    prove the session validator's *return value* reached the mount, and reports
    a clean bounded stop so ``run_acp``'s teardown returns instead of calling
    ``os._exit``.
    """

    def __init__(self):
        self._shutdown = None
        self._venv_path = None
        self.session_mcp_configs = None

    def start(self):
        return None

    def stop(self, timeout=None):
        return SimpleNamespace(
            stopped=True, run_loop_alive=False, provider_worker_alive=False
        )

    def mount_session_mcp_stdio(self, configs):
        self.session_mcp_configs = configs
        return _SessionMCPLease()


class _ScriptedWire:
    """An ACP input stream that yields scripted lines then holds the wire open.

    ``run_acp`` writes its responses from a background writer thread, so a
    ``StringIO`` that hits EOF immediately can be closed before the last frame
    is drained. This stream yields the scripted request lines and then blocks
    the server's reader until the test has observed the expected frames and
    calls ``release()`` — mirroring a real client that keeps stdin open.
    """

    def __init__(self, lines):
        self._lines = list(lines)
        self._index = 0
        self._release = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line
        # Block until the test has observed its frames and releases the wire.
        # Unbounded on purpose: a bounded wait here could expire while the test
        # is still polling and EOF the server mid-write, reintroducing the
        # close-before-drain race. ``_run_v1_composition``'s finally always calls
        # release(), and this reader is a daemon thread, so it cannot wedge.
        self._release.wait()
        raise StopIteration

    def release(self):
        self._release.set()


def _run_v1_composition(monkeypatch, tmp_path, mcp_servers):
    """Drive the *real* ``run_acp`` puffo-v1 composition over a scripted ACP wire.

    Only agent construction (orthogonal to validator forwarding) is stubbed;
    every link of the forwarding seam stays real — ``run_acp`` builds the
    ``AcpStdioServer`` from its own ``session_mcp_validator`` kwarg and a real
    ``session/new`` applies it. Uses the minimal composition arg set that reaches
    the forwarding branch (``fixed_execution_workspace`` set → the else-branch of
    ``run_acp``); the turn-origin / derived-launch ports are deliberately omitted
    because they feed the stubbed ``build_agent`` and would add failure modes
    unrelated to the seam under test. Returns the two output frames (initialize
    result + session/new result-or-error) and the mount-recording agent.
    """
    import lingtai.cli_acp as cli_acp

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_dir = tmp_path / "identity"
    agent_dir.mkdir()
    agent = _CompositionAgent()

    monkeypatch.setattr("lingtai.cli._check_duplicate_process", lambda *a, **k: None)
    monkeypatch.setattr("lingtai.cli._clean_signal_files", lambda *a, **k: None)
    monkeypatch.setattr("lingtai.cli.load_init", lambda _agent_dir: {})
    monkeypatch.setattr(
        "lingtai.cli.build_agent", lambda data, directory, **opts: agent
    )
    monkeypatch.setattr("lingtai.kernel.logging.setup_logging", lambda **k: None)
    monkeypatch.setattr("lingtai.venv_resolve.resolve_venv", lambda data: tmp_path / "venv")

    wire_in = _ScriptedWire(
        [
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": 1}}
            ) + "\n",
            json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "session/new",
                 "params": {"cwd": str(workspace), "mcpServers": mcp_servers}}
            ) + "\n",
        ]
    )
    wire_out = io.StringIO()

    def _serve():
        cli_acp.run_acp(
            agent_dir,
            input_stream=wire_in,
            output_stream=wire_out,
            fixed_execution_workspace=ExecutionWorkspace(workspace),
            session_mcp_validator=validate_puffo_v1_mcp_servers,
        )

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()
    try:
        frames = _wait_for_frames(wire_out, 2)
    finally:
        wire_in.release()
        server_thread.join(timeout=5.0)
    return frames, agent


def test_puffo_v1_composition_forwards_validator_into_a_live_session(monkeypatch, tmp_path):
    """Real ``run_acp`` must thread ``session_mcp_validator`` into the server it builds.

    Pins ``cli_acp.run_acp``'s forwarding line (``session_mcp_validator`` →
    ``server_options`` → ``AcpStdioServer``) end to end: the existing
    ``test_puffo_v1_session_*`` tests construct the server directly and
    ``test_puffo_v1_cli_reuses...`` mocks ``run_acp``, so neither covers this
    seam. Scope: asserts the validator's own return value reached
    ``mount_session_mcp_stdio``; it does NOT assert ``allow_session_mcp``
    semantics — the server consults the validator first, so that flag is
    short-circuited when the validator is present, and the rejection test below
    carries the validator-identity discriminator.
    """
    frames, agent = _run_v1_composition(monkeypatch, tmp_path, [_puffo_server_config()])

    session_frame = next(f for f in frames if f.get("id") == 2)
    assert "result" in session_frame, session_frame
    assert agent.session_mcp_configs is not None
    assert len(agent.session_mcp_configs) == 1
    assert agent.session_mcp_configs[0].name == "puffo"
    assert agent.session_mcp_configs[0].args == PUFFO_MCP_ARGS


@pytest.mark.parametrize(
    ("servers", "message"),
    [
        ([], "puffo-v1 requires exactly one Puffo MCP server"),
        (
            [_puffo_server_config(name="not-puffo")],
            "puffo-v1 MCP server must be named puffo",
        ),
        (
            [_puffo_server_config(env=[])],
            "puffo-v1 MCP server requires a non-empty Puffo local service token",
        ),
    ],
)
def test_puffo_v1_composition_applies_validator_rejection_through_real_run_acp(
    monkeypatch, tmp_path, servers, message
):
    """The forwarded validator's puffo-v1-specific rejection must reach the wire.

    Distinguishes ``validate_puffo_v1_mcp_servers`` from the generic MCP parser
    independent of ``allow_session_mcp``: the generic path would accept these
    shapes (empty → no mount; wrong name / missing token → mount), so only a
    validator actually forwarded through real ``run_acp`` yields these exact
    messages and mounts nothing.
    """
    frames, agent = _run_v1_composition(monkeypatch, tmp_path, servers)

    session_frame = next(f for f in frames if f.get("id") == 2)
    assert session_frame.get("error") == {"code": INVALID_PARAMS, "message": message}
    assert agent.session_mcp_configs is None


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


def test_acp_parser_rejects_profile_flag_abbreviations():
    """The ACP profile flag must be matched exactly, never abbreviated.

    A constrained caller classifies the launch on the literal ``--profile``
    token.  If argparse also accepted ``--prof``/``--p``/``--pro=``, a real
    ``puffo-v0``/``puffo-v1`` profile could be admitted while bypassing that
    classifier and its authority/controlled-spawn path, so every abbreviated
    long-option form must exit 2 while the exact spellings still parse.
    """
    import lingtai.cli_acp as cli_acp

    def _parse(argv):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command", required=True)
        cli_acp.add_acp_parser(subparsers)
        return parser.parse_args(argv)

    for exact in (
        ["acp", "--profile", "puffo-v0", "--runtime-id", "opaque-id"],
        ["acp", "--profile=puffo-v1", "--runtime-id", "opaque-id"],
    ):
        assert _parse(exact).profile in {"puffo-v0", "puffo-v1"}

    for abbreviated in (
        ["acp", "--prof", "puffo-v0", "--runtime-id", "opaque-id"],
        ["acp", "--pro=puffo-v1", "--runtime-id", "opaque-id"],
        ["acp", "--p", "puffo-v0", "--runtime-id", "opaque-id"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            _parse(abbreviated)
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
