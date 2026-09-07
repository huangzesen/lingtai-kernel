from __future__ import annotations

import json
import os
import stat
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lingtai.adapters.posix import daemon_manager
from lingtai.adapters.posix.daemon_capsule import ReceivedDaemonCapsule
from lingtai.adapters.posix.daemon_manager import MANAGER_DIR
from lingtai.adapters.posix.daemon_manager import _DaemonManagerProcess
from lingtai.adapters.posix.process_identity import process_identity
from lingtai.kernel.daemon_supervisor import DaemonSupervisorRequest, encode_request
from lingtai.kernel.daemon_supervisor.manifest import build_manifest, manifest_path_for, write_manifest
from lingtai.tools.daemon import DaemonManager
from lingtai.tools.daemon.run_dir import DaemonRunDir
from tests._daemon_helpers import install_fake_detached_owner, make_daemon_agent


def test_capsule_socket_fallback_ignores_overlong_ambient_temp_roots() -> None:
    root = Path("/private/tmp") / ("deep-agent-root-" * 12)

    socket_path = daemon_manager._capsule_socket_path(root)

    assert socket_path.parent.parent == Path("/tmp")
    assert socket_path.parent.name.startswith(f"lingtai-dm-{os.getuid()}-")
    assert socket_path.name == "capsule.sock"
    assert len(str(socket_path)) < daemon_manager._UNIX_SOCKET_PATH_LIMIT


def test_capsule_socket_fallback_reuses_only_a_safe_stale_socket(monkeypatch) -> None:
    socket_path = (
        Path("/tmp")
        / f"lingtai-dm-{os.getuid()}-{'a' * 24}"
        / "capsule.sock"
    )
    unlinked: list[Path] = []

    def fake_mkdir(path: Path, **_kwargs) -> None:
        assert path == socket_path.parent
        raise FileExistsError

    def fake_lstat(path: Path):
        if path == socket_path.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid())
        assert path == socket_path
        return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid())

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "unlink", lambda path: unlinked.append(path))

    daemon_manager._prepare_capsule_socket_path(socket_path)

    assert unlinked == [socket_path]


@pytest.mark.parametrize(
    ("mode", "uid"),
    [
        (stat.S_IFLNK | 0o700, os.getuid()),
        (stat.S_IFDIR | 0o755, os.getuid()),
        (stat.S_IFDIR | 0o700, os.getuid() + 1),
    ],
)
def test_capsule_socket_fallback_refuses_unsafe_directory_before_unlink(
    monkeypatch, mode: int, uid: int
) -> None:
    socket_path = (
        Path("/tmp")
        / f"lingtai-dm-{os.getuid()}-{'b' * 24}"
        / "capsule.sock"
    )
    monkeypatch.setattr(
        Path,
        "mkdir",
        lambda _path, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(st_mode=mode, st_uid=uid)
        if path == socket_path.parent
        else pytest.fail("unsafe directory must fail before inspecting the socket"),
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda _path: pytest.fail("unsafe fallback must not unlink any path"),
    )

    with pytest.raises(RuntimeError, match="owner-owned mode-0700"):
        daemon_manager._prepare_capsule_socket_path(socket_path)


@pytest.mark.parametrize(
    ("mode", "uid"),
    [
        (stat.S_IFLNK | 0o600, os.getuid()),
        (stat.S_IFREG | 0o600, os.getuid()),
        (stat.S_IFSOCK | 0o660, os.getuid()),
        (stat.S_IFSOCK | 0o600, os.getuid() + 1),
    ],
)
def test_capsule_socket_fallback_refuses_unsafe_stale_entry(
    monkeypatch, mode: int, uid: int
) -> None:
    socket_path = (
        Path("/tmp")
        / f"lingtai-dm-{os.getuid()}-{'c' * 24}"
        / "capsule.sock"
    )
    monkeypatch.setattr(
        Path,
        "mkdir",
        lambda _path, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid()
        )
        if path == socket_path.parent
        else SimpleNamespace(st_mode=mode, st_uid=uid),
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda _path: pytest.fail("unsafe fallback must not unlink any path"),
    )

    with pytest.raises(RuntimeError, match="unsafe.*fallback socket"):
        daemon_manager._prepare_capsule_socket_path(socket_path)


def _make_run(tmp_path: Path, run_id: str, *, timeout_s: float = 30.0) -> tuple[DaemonRunDir, DaemonSupervisorRequest]:
    parent = tmp_path / "agent"
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = DaemonRunDir(
        parent_working_dir=parent,
        handle=run_id,
        run_id=run_id,
        task=f"task {run_id}",
        tools=[],
        model="fake",
        max_turns=1,
        timeout_s=timeout_s,
        parent_addr=parent.name,
        parent_pid=12345,
        system_prompt=f"daemon\n\nYour task:\ntask {run_id}",
        call_parameters={"task": f"task {run_id}", "tools": []},
    )
    manifest = build_manifest(
        run_id=run_id,
        backend="lingtai",
        parent_working_dir=str(parent),
        run_dir=str(run_dir.path),
        task=f"task {run_id}",
        tools=[],
        max_turns=1,
        timeout_s=timeout_s,
        group_id=None,
        llm={"provider": "fake", "model": "fake"},
    )
    write_manifest(run_dir.path, manifest)
    return run_dir, DaemonSupervisorRequest(
        run_id=run_id,
        manifest_path=str(manifest_path_for(run_dir.path)),
        python_executable="python",
    )


def _write_job(
    queue_dir: Path,
    request: DaemonSupervisorRequest,
    capsule: dict | None = None,
    *,
    enqueued_at: float | None = None,
) -> None:
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / f"{request.run_id}.json").write_text(
        json.dumps(
            {
                "schema": "lingtai.daemon_manager_job.v1",
                "run_id": request.run_id,
                "request": encode_request(request),
                "capsule_in_memory": True,
                "enqueued_at": enqueued_at if enqueued_at is not None else time.time(),
            }
        ),
        encoding="utf-8",
    )


def _manager_with_capsules(
    queue_dir: Path,
    journal_dir: Path,
    *,
    pool_size: int,
    capsules: dict[str, dict] | None = None,
) -> _DaemonManagerProcess:
    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=pool_size)
    manager.capsules.update({
        run_id: ReceivedDaemonCapsule(value=capsule)
        for run_id, capsule in (capsules or {}).items()
    })
    return manager


def test_manager_submission_closes_adopted_fd_when_capsule_send_fails(
    tmp_path, monkeypatch
):
    run_dir, request = _make_run(tmp_path, "em-send-failure")
    child_endpoint, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    adopted_fd = child_endpoint.detach()
    monkeypatch.setattr(daemon_manager, "_ensure_manager", lambda *_a, **_k: None)
    monkeypatch.setattr(
        daemon_manager,
        "_send_capsule",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("send failed")),
    )

    try:
        with pytest.raises(RuntimeError, match="send failed"):
            daemon_manager.enqueue_manager_run(
                agent_working_dir=run_dir.path.parent.parent,
                request=request,
                capsule={"task": "test"},
                pool_size=1,
                adopted_fd=adopted_fd,
            )
        peer.settimeout(1)
        assert peer.recv(1) == b""
    finally:
        peer.close()


def test_manager_pre_execution_failure_closes_pending_adopted_fd(tmp_path):
    run_dir, request = _make_run(tmp_path, "em-owned-failure")
    child_endpoint, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    pending = ReceivedDaemonCapsule(
        value={"task": "test"},
        adopted_fd=child_endpoint.detach(),
    )
    mismatched = DaemonSupervisorRequest(
        run_id="em-wrong-run",
        manifest_path=request.manifest_path,
        python_executable=request.python_executable,
    )
    manager = _DaemonManagerProcess(
        tmp_path / "manager" / "queue",
        tmp_path / "manager" / "journal",
        pool_size=1,
    )

    try:
        manager._run_job(mismatched, pending)
        peer.settimeout(1)
        assert peer.recv(1) == b""
        assert DaemonRunDir.read_state_from_disk(run_dir.path)["state"] == "failed"
    finally:
        peer.close()


def test_manager_socket_transfers_fd_into_pending_capsule(tmp_path):
    root = tmp_path / "manager"
    manager = _DaemonManagerProcess(
        root / "queue",
        root / "journal",
        pool_size=1,
    )
    root.mkdir(parents=True)
    manager.start_capsule_server(daemon_manager._capsule_socket_path(root))
    child_endpoint, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    source_fd: int | None = child_endpoint.detach()
    pending = None

    try:
        daemon_manager._send_capsule(
            root,
            "em-manager-wire",
            {"task": "test"},
            adopted_fd=source_fd,
        )
        os.close(source_fd)
        source_fd = None
        pending = manager.capsules.pop("em-manager-wire")
        received_fd = pending.take_fd()
        assert received_fd is not None
        assert not os.get_inheritable(received_fd)
        os.write(received_fd, b"manager-owned")
        os.close(received_fd)
        peer.settimeout(1)
        assert peer.recv(len(b"manager-owned")) == b"manager-owned"
        assert peer.recv(1) == b""
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if pending is not None:
            pending.close()
        if manager._capsule_socket is not None:
            manager._capsule_socket.close()
        peer.close()


def test_manager_sender_accepts_fragmented_ack(tmp_path, monkeypatch):
    class FragmentedAckSocket:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ack_chunks = iter((b"O", b"K"))

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, _path: str) -> None:
            return None

        def sendmsg(self, buffers, _ancillary) -> int:
            return len(buffers[0])

        def sendall(self, _payload: bytes) -> None:
            return None

        def shutdown(self, _how: int) -> None:
            return None

        def recv(self, _size: int) -> bytes:
            return next(self.ack_chunks)

    monkeypatch.setattr(daemon_manager.socket, "socket", FragmentedAckSocket)

    daemon_manager._send_capsule(tmp_path, "em-fragmented-ack", {"task": "test"})


def test_manager_transfer_does_not_close_reused_descriptor(tmp_path, monkeypatch):
    run_dir, request = _make_run(tmp_path, "em-transfer-once")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, request)
    child_endpoint, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    adopted_fd = child_endpoint.detach()
    replacement_source = os.open(os.devnull, os.O_RDONLY)

    def fake_run(rd, manifest, capsule, *, adopted_fd):
        os.close(adopted_fd)
        os.dup2(replacement_source, adopted_fd)
        rd.mark_done("transferred")

    monkeypatch.setattr(
        "lingtai.adapters.posix.daemon_manager._run_one_emanation",
        fake_run,
    )
    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=1)
    manager.capsules[request.run_id] = ReceivedDaemonCapsule(
        value={"task": "test"},
        adopted_fd=adopted_fd,
    )

    try:
        manager.run()
        os.fstat(adopted_fd)
        assert _wait_state(run_dir, "done")["state"] == "done"
    finally:
        os.close(adopted_fd)
        os.close(replacement_source)
        peer.close()


class _CapsuleConnection:
    def __init__(self, *, fail_reply: bool = False) -> None:
        self.fail_reply = fail_reply

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def sendall(self, _payload: bytes) -> None:
        if self.fail_reply:
            raise OSError("reply failed")


class _CapsuleListener:
    def __init__(self, *connections: _CapsuleConnection) -> None:
        self.connections = list(connections)

    def accept(self):
        if not self.connections:
            raise OSError("listener stopped")
        return self.connections.pop(0), None


def _received_fd_capsule(run_id: str):
    child_endpoint, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return ReceivedDaemonCapsule(
        value={"run_id": run_id, "capsule": {"task": "test"}},
        adopted_fd=child_endpoint.detach(),
    ), peer


def test_manager_failed_ack_discards_received_fd(tmp_path, monkeypatch):
    manager = _DaemonManagerProcess(
        tmp_path / "manager" / "queue",
        tmp_path / "manager" / "journal",
        pool_size=1,
    )
    wire, peer = _received_fd_capsule("em-ack-failure")
    connection = _CapsuleConnection(fail_reply=True)
    manager._capsule_socket = _CapsuleListener(connection)
    monkeypatch.setattr(manager, "_read_capsule_message", lambda _conn: wire)

    try:
        manager._serve_capsules()
        assert manager.capsules == {}
        peer.settimeout(1)
        assert peer.recv(1) == b""
    finally:
        peer.close()


def test_manager_replacement_discards_previous_fd(tmp_path, monkeypatch):
    manager = _DaemonManagerProcess(
        tmp_path / "manager" / "queue",
        tmp_path / "manager" / "journal",
        pool_size=1,
    )
    first_wire, first_peer = _received_fd_capsule("em-replaced")
    second_wire, second_peer = _received_fd_capsule("em-replaced")
    manager._capsule_socket = _CapsuleListener(
        _CapsuleConnection(),
        _CapsuleConnection(),
    )
    wires = iter((first_wire, second_wire))
    monkeypatch.setattr(manager, "_read_capsule_message", lambda _conn: next(wires))

    try:
        manager._serve_capsules()
        first_peer.settimeout(1)
        assert first_peer.recv(1) == b""
        second_peer.settimeout(0.05)
        with pytest.raises(TimeoutError):
            second_peer.recv(1)
        manager.capsules.pop("em-replaced").close()
        second_peer.settimeout(1)
        assert second_peer.recv(1) == b""
    finally:
        first_peer.close()
        second_peer.close()


def test_manager_unlink_rollback_preserves_concurrent_replacement(
    tmp_path, monkeypatch
):
    run_dir, request = _make_run(tmp_path, "em-unlink-replacement")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, request)
    job_path = queue_dir / f"{request.run_id}.json"
    old_wire, old_peer = _received_fd_capsule(request.run_id)
    replacement_wire, replacement_peer = _received_fd_capsule(request.run_id)
    old_pending = ReceivedDaemonCapsule(
        value=old_wire.value["capsule"],
        adopted_fd=old_wire.take_fd(),
    )
    replacement = ReceivedDaemonCapsule(
        value=replacement_wire.value["capsule"],
        adopted_fd=replacement_wire.take_fd(),
    )
    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=1)
    manager.capsules[request.run_id] = old_pending

    def replace_then_fail_unlink(path: Path) -> None:
        assert path == job_path
        with manager.lock:
            manager.capsules[request.run_id] = replacement
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(Path, "unlink", replace_then_fail_unlink)

    try:
        manager._start_queued_jobs()
        assert manager.capsules[request.run_id] is replacement
        old_peer.settimeout(1)
        assert old_peer.recv(1) == b""
        replacement_peer.settimeout(0.05)
        with pytest.raises(TimeoutError):
            replacement_peer.recv(1)
        manager.capsules.pop(request.run_id).close()
        replacement_peer.settimeout(1)
        assert replacement_peer.recv(1) == b""
        assert DaemonRunDir.read_state_from_disk(run_dir.path)["state"] == "running"
    finally:
        old_wire.close()
        replacement_wire.close()
        old_pending.close()
        replacement.close()
        old_peer.close()
        replacement_peer.close()


def test_manager_malformed_job_discards_pending_fd(tmp_path):
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    queue_dir.mkdir(parents=True)
    (queue_dir / "em-malformed-fd.json").write_text("[1, 2, 3]", encoding="utf-8")
    wire, peer = _received_fd_capsule("em-malformed-fd")
    pending = ReceivedDaemonCapsule(
        value=wire.value["capsule"],
        adopted_fd=wire.take_fd(),
    )
    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=1)
    manager.capsules["em-malformed-fd"] = pending

    try:
        manager._start_queued_jobs()
        assert manager.capsules == {}
        peer.settimeout(1)
        assert peer.recv(1) == b""
    finally:
        wire.close()
        peer.close()


def test_manager_queued_cancel_discards_pending_fd(tmp_path):
    from lingtai.kernel.daemon_supervisor import control

    run_dir, request = _make_run(tmp_path, "em-cancelled-fd")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, request)
    control.submit_request(run_dir.path, "reclaim", {})
    wire, peer = _received_fd_capsule(request.run_id)
    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=1)
    manager.capsules[request.run_id] = ReceivedDaemonCapsule(
        value=wire.value["capsule"],
        adopted_fd=wire.take_fd(),
    )

    try:
        manager._consume_queue_cancel_requests()
        assert manager.capsules == {}
        peer.settimeout(1)
        assert peer.recv(1) == b""
        assert DaemonRunDir.read_state_from_disk(run_dir.path)["state"] == "cancelled"
    finally:
        wire.close()
        peer.close()


def test_manager_exit_discards_unclaimed_fd(tmp_path):
    manager = _DaemonManagerProcess(
        tmp_path / "manager" / "queue",
        tmp_path / "manager" / "journal",
        pool_size=1,
    )
    wire, peer = _received_fd_capsule("em-manager-exit")
    manager.capsules["em-manager-exit"] = ReceivedDaemonCapsule(
        value=wire.value["capsule"],
        adopted_fd=wire.take_fd(),
    )

    try:
        manager.run(idle_exit_s=0)
        assert manager.capsules == {}
        peer.settimeout(1)
        assert peer.recv(1) == b""
    finally:
        wire.close()
        peer.close()


def _wait_state(run_dir: DaemonRunDir, state: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = DaemonRunDir.read_state_from_disk(run_dir.path)
        if data.get("state") == state:
            return data
        time.sleep(0.02)
    raise AssertionError(f"{run_dir.run_id} did not reach {state}")


def _notification_events(parent: Path) -> list[dict]:
    events: list[dict] = []
    for path in sorted((parent / ".notification" / "daemon").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        events.extend(payload.get("data", {}).get("events", []))
    return events


def _enable_detached_fake_llm(monkeypatch, agent, *, sleep_s: float = 0.0) -> None:
    agent.service.provider = "lingtai-supervisor-test-fake"
    agent.service.model = "fake-model"
    agent.service.api_key = "detached-test-key"
    agent.service._base_url = None
    agent.service._provider_defaults = {}
    monkeypatch.setenv("LINGTAI_DAEMON_SUPERVISOR_TEST_FAKE_LLM", "1")
    monkeypatch.setenv("LINGTAI_DAEMON_SUPERVISOR_TEST_FAKE_LLM_FINISH", "1")
    if sleep_s:
        monkeypatch.setenv("LINGTAI_DAEMON_SUPERVISOR_TEST_FAKE_LLM_SLEEP", str(sleep_s))
    tests_dir = str(Path(__file__).parent)
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    parts = [tests_dir, src_dir]
    parts.extend(p for p in existing.split(os.pathsep) if p)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(dict.fromkeys(parts)))


def _install_fake_opencode(tmp_path: Path, monkeypatch, *, sleep_s: float = 1.0) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(
        "\n".join([
            "#!/usr/bin/env python3",
            "import json, os, pathlib, time",
            "cfg = json.loads(os.environ.get('OPENCODE_CONFIG_CONTENT') or '{}')",
            "env = cfg.get('mcp', {}).get('daemon_common', {}).get('environment', {})",
            "completion = env.get('LINGTAI_DAEMON_COMPLETION_FILE')",
            "if completion:",
            "    pathlib.Path(completion).write_text(json.dumps({",
            "        'schema': 'lingtai.daemon_completion.v1',",
            "        'status': 'done',",
            "        'run_id': env.get('LINGTAI_DAEMON_RUN_ID'),",
            "        'summary': 'fake opencode done',",
            "    }), encoding='utf-8')",
            f"time.sleep({sleep_s!r})",
            "print(json.dumps({'type': 'result', 'session_id': 'fake-session', 'result': 'done'}), flush=True)",
        ]),
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


def _wait_for(predicate, *, timeout: float = 5.0, message: str = "condition") -> object:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}; last={last!r}")


def _terminate_resident_manager(agent) -> None:
    pid_path = agent._working_dir / MANAGER_DIR / "manager.pid"
    if not pid_path.exists():
        return
    try:
        info = json.loads(pid_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    pid = info.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _manager_runtime_identity(code_head: str) -> dict[str, str]:
    return {
        "schema": "lingtai.daemon_manager_runtime.v1",
        "loaded_code_head": code_head,
        "source_root": "/test/source",
        "daemon_notification_protocol": "per-run-mini-channel.v1",
    }


def _write_live_manager_pid(
    agent,
    *,
    pid: int | None = None,
    runtime_identity: dict[str, str] | None = None,
) -> None:
    pid = os.getpid() if pid is None else pid
    root = agent._working_dir / MANAGER_DIR
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "state": "running",
        "started_at": time.time(),
        "manager_start_identity": process_identity(pid),
    }
    if runtime_identity is not None:
        payload["manager_runtime_identity"] = runtime_identity
    (root / "manager.pid").write_text(json.dumps(payload), encoding="utf-8")


def test_central_manager_persists_loaded_runtime_identity(tmp_path, monkeypatch):
    expected = _manager_runtime_identity("current-head")
    monkeypatch.setattr(daemon_manager, "_manager_runtime_identity", lambda: expected)

    class FakeManager:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start_capsule_server(self, _socket_path) -> None:
            pass

        def recover_interrupted_active_runs(self) -> None:
            pass

        def run(self, *, idle_exit_s=None) -> None:
            assert idle_exit_s is None

    monkeypatch.setattr(daemon_manager, "_DaemonManagerProcess", FakeManager)
    agent_working_dir = tmp_path / "agent"

    daemon_manager.run_manager(agent_working_dir, pool_size=1)

    record = json.loads((agent_working_dir / MANAGER_DIR / "manager.pid").read_text(encoding="utf-8"))
    assert record["manager_runtime_identity"] == expected


def test_central_manager_reuses_only_matching_runtime_identity(tmp_path, monkeypatch):
    agent = SimpleNamespace(_working_dir=tmp_path / "agent")
    expected = _manager_runtime_identity("current-head")
    _write_live_manager_pid(agent, runtime_identity=expected)
    monkeypatch.setattr(daemon_manager, "_manager_runtime_identity", lambda: expected)

    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("matching central manager must be reused")

    monkeypatch.setattr(daemon_manager.subprocess, "Popen", unexpected_popen)

    daemon_manager._ensure_manager(agent._working_dir, pool_size=1)


def test_central_manager_refuses_mismatched_runtime_before_queue_write(tmp_path, monkeypatch):
    run_dir, request = _make_run(tmp_path, "em-stale-manager")
    agent = SimpleNamespace(_working_dir=tmp_path / "agent")
    _write_live_manager_pid(agent, runtime_identity=_manager_runtime_identity("old-head"))
    monkeypatch.setattr(
        daemon_manager,
        "_manager_runtime_identity",
        lambda: _manager_runtime_identity("current-head"),
    )
    monkeypatch.setattr(
        daemon_manager,
        "_send_capsule",
        lambda *_args, **_kwargs: pytest.fail("mismatched manager must not receive a capsule"),
    )

    with pytest.raises(RuntimeError, match="runtime identity.*daemon-manual"):
        daemon_manager.enqueue_manager_run(
            agent_working_dir=agent._working_dir,
            request=request,
            capsule={"capability": "fresh"},
            pool_size=1,
        )

    assert not (agent._working_dir / MANAGER_DIR / "queue").exists()
    assert DaemonRunDir.read_state_from_disk(run_dir.path).get("owner") != "manager"


def test_central_manager_refuses_mismatched_starting_identity(tmp_path, monkeypatch):
    agent_working_dir = tmp_path / "agent"
    root = agent_working_dir / MANAGER_DIR
    root.mkdir(parents=True)
    (root / "manager.pid").write_text(
        json.dumps(
            {
                "pid": None,
                "started_at": time.time(),
                "state": "starting",
                "manager_runtime_identity": _manager_runtime_identity("old-head"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        daemon_manager,
        "_manager_runtime_identity",
        lambda: _manager_runtime_identity("current-head"),
    )

    with pytest.raises(RuntimeError, match="runtime identity.*daemon-manual"):
        daemon_manager._ensure_manager(agent_working_dir, pool_size=1)


def test_concurrent_ensure_manager_callers_reserve_and_spawn_one_manager(tmp_path, monkeypatch):
    """Two callers that both see no live manager must not both spawn one.

    The barrier inside the ``manager.pid`` read forces the classic TOCTOU
    interleaving: neither caller may reserve until both have observed the
    absent record.  When the read/reserve/spawn sequence is exclusive the
    second reader cannot arrive, the barrier breaks after its bounded wait,
    and the first caller proceeds alone; the second then sees the fresh
    ``starting`` reservation and reuses it.  The test therefore never depends
    on a lucky schedule to make either regime observable.
    """
    agent_working_dir = tmp_path / "agent"
    expected = _manager_runtime_identity("current-head")
    monkeypatch.setattr(daemon_manager, "_manager_runtime_identity", lambda: expected)

    spawn_lock = threading.Lock()
    spawned_tokens: list[str] = []

    def counting_popen(_argv, **kwargs):
        with spawn_lock:
            spawned_tokens.append(kwargs["env"]["LINGTAI_DAEMON_MANAGER_TOKEN"])
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(daemon_manager.subprocess, "Popen", counting_popen)

    both_observed_absent = threading.Barrier(2, timeout=1.0)
    real_read_json = daemon_manager.read_json

    def barrier_read_json(path, *args, **kwargs):
        info = real_read_json(path, *args, **kwargs)
        if Path(path).name == "manager.pid" and not info:
            try:
                both_observed_absent.wait()
            except threading.BrokenBarrierError:
                pass
        return info

    monkeypatch.setattr(daemon_manager, "read_json", barrier_read_json)

    errors: list[BaseException] = []

    def call_ensure_manager() -> None:
        try:
            daemon_manager._ensure_manager(agent_working_dir, pool_size=1)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    callers = [threading.Thread(target=call_ensure_manager) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=10.0)

    assert not any(caller.is_alive() for caller in callers)
    assert errors == []
    assert len(spawned_tokens) == 1
    record = json.loads((agent_working_dir / MANAGER_DIR / "manager.pid").read_text(encoding="utf-8"))
    assert record["state"] == "starting"
    assert record["manager_token"] == spawned_tokens[0]
    assert record["manager_runtime_identity"] == expected


def test_central_manager_completes_run_and_notifies(tmp_path, monkeypatch):
    run_dir, request = _make_run(tmp_path, "em-manager")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, request)

    def fake_run(rd, manifest, capsule):
        rd.mark_done("manager completed")

    monkeypatch.setattr("lingtai.adapters.posix.daemon_manager._run_one_emanation", fake_run)

    manager = _manager_with_capsules(
        queue_dir, journal_dir, pool_size=1, capsules={request.run_id: {}},
    )
    manager.run()

    state = _wait_state(run_dir, "done")
    assert state["owner"] == "manager"
    assert state["terminal_notified"] is True
    events = _notification_events(run_dir.path.parent.parent)
    assert [ev["ref_id"] for ev in events].count("em-manager") == 1


def test_central_manager_recovery_marks_active_failed_without_duplicate_notify(tmp_path):
    run_dir, request = _make_run(tmp_path, "em-recover")
    journal_dir = tmp_path / "manager" / "journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "em-recover.json").write_text(
        json.dumps(
            {
                "schema": "lingtai.daemon_manager_journal.v1",
                "run_id": "em-recover",
                "request": encode_request(request),
                "capsule_in_memory": True,
                "capsule_scrubbed": True,
                "state": "active",
            }
        ),
        encoding="utf-8",
    )

    manager = _DaemonManagerProcess(tmp_path / "manager" / "queue", journal_dir, pool_size=1)
    manager.recover_interrupted_active_runs()
    manager.recover_interrupted_active_runs()

    state = _wait_state(run_dir, "failed")
    assert "central daemon manager recovered" in state["error"]["message"]
    events = _notification_events(run_dir.path.parent.parent)
    assert [ev["ref_id"] for ev in events].count("em-recover") == 1


def test_central_manager_restart_fails_queued_job_without_capsule(tmp_path):
    run_dir, request = _make_run(tmp_path, "em-missing-capsule")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, request, enqueued_at=0.0)

    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=1)
    manager.run()

    state = _wait_state(run_dir, "failed")
    assert "manager_restart_capsule_unavailable" in state["error"]["message"]
    assert state["terminal_notified"] is True
    assert not (queue_dir / f"{request.run_id}.json").exists()
    events = _notification_events(run_dir.path.parent.parent)
    assert [ev["ref_id"] for ev in events].count("em-missing-capsule") == 1
    record = json.loads((journal_dir / f"{request.run_id}.json").read_text(encoding="utf-8"))
    assert record["state"] == "failed_missing_capsule"
    assert record["capsule_in_memory"] is False
    assert record["capsule_scrubbed"] is True
    assert record["evidence"]["reason"] == "manager_restart_capsule_unavailable"
    assert record["evidence"]["source"].endswith("em-missing-capsule.json")
    assert "capsule" not in record


def test_central_manager_timeout_run_notifies(tmp_path, monkeypatch):
    run_dir, request = _make_run(tmp_path, "em-timeout", timeout_s=5.0)
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, request)

    def fake_timeout(rd, manifest, capsule):
        rd.mark_timeout()

    monkeypatch.setattr("lingtai.adapters.posix.daemon_manager._run_one_emanation", fake_timeout)

    manager = _manager_with_capsules(
        queue_dir, journal_dir, pool_size=1, capsules={request.run_id: {}},
    )
    manager.run()

    state = _wait_state(run_dir, "timeout")
    assert state["terminal_notified"] is True
    events = _notification_events(run_dir.path.parent.parent)
    assert [ev["ref_id"] for ev in events].count("em-timeout") == 1


def test_central_manager_queues_until_worker_frees(tmp_path, monkeypatch):
    first, req1 = _make_run(tmp_path, "em-first")
    second, req2 = _make_run(tmp_path, "em-second")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, req1)
    _write_job(queue_dir, req2)
    starts: list[str] = []

    def fake_slow(rd, manifest, capsule):
        starts.append(rd.run_id)
        time.sleep(0.15)
        rd.mark_done(rd.run_id)

    monkeypatch.setattr("lingtai.adapters.posix.daemon_manager._run_one_emanation", fake_slow)

    manager = _manager_with_capsules(
        queue_dir,
        journal_dir,
        pool_size=1,
        capsules={req1.run_id: {}, req2.run_id: {}},
    )
    manager.run()

    assert starts == ["em-first", "em-second"]
    assert _wait_state(first, "done")["state"] == "done"
    assert _wait_state(second, "done")["state"] == "done"


def test_central_manager_dispatches_high_concurrency_without_waiting_for_queued_pid(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path, ["file", "daemon"])
    _enable_detached_fake_llm(monkeypatch, agent, sleep_s=1.0)
    manager = DaemonManager(agent, manager_pool_size=1)

    try:
        start = time.monotonic()
        result = manager._handle_emanate([
            {"task": "slow task A", "tools": ["file"]},
            {"task": "slow task B", "tools": ["file"]},
        ])
        elapsed = time.monotonic() - start

        assert result["status"] == "dispatched", result
        assert result["count"] == 2
        assert elapsed < 0.75

        run_dirs = [manager._emanations[run_id]["run_dir"] for run_id in result["ids"]]
        queue_dir = agent._working_dir / MANAGER_DIR / "queue"

        _wait_for(
            lambda: DaemonRunDir.read_state_from_disk(run_dirs[0].path).get("supervisor_pid"),
            timeout=5.0,
            message="first managed run to start",
        )
        second_state = DaemonRunDir.read_state_from_disk(run_dirs[1].path)
        assert not second_state.get("supervisor_pid")
        queued_job = queue_dir / f"{result['ids'][1]}.json"
        assert queued_job.exists()
        assert "detached-test-key" not in queued_job.read_text(encoding="utf-8")
        active_journal = agent._working_dir / MANAGER_DIR / "journal" / f"{result['ids'][0]}.json"
        assert "detached-test-key" not in active_journal.read_text(encoding="utf-8")

        for run_dir in run_dirs:
            _wait_state(run_dir, "done", timeout=10.0)
    finally:
        _terminate_resident_manager(agent)


def test_parent_restart_does_not_reap_queued_manager_owned_run(tmp_path):
    agent = make_daemon_agent(tmp_path, ["file", "daemon"], working_dir_name="agent")
    run_dir, request = _make_run(tmp_path, "em-queued")
    run_dir.update_state(owner="manager")
    _write_live_manager_pid(agent)
    queue_dir = agent._working_dir / MANAGER_DIR / "queue"
    _write_job(queue_dir, request)

    DaemonManager(agent)

    state = DaemonRunDir.read_state_from_disk(run_dir.path)
    assert state["state"] == "running"
    assert state["owner"] == "manager"
    assert state.get("finished_at") is None


def test_parent_restart_does_not_reap_active_manager_owned_run(tmp_path):
    agent = make_daemon_agent(tmp_path, ["file", "daemon"], working_dir_name="agent")
    run_dir, request = _make_run(tmp_path, "em-active")
    manager_pid = os.getpid()
    run_dir.update_state(
        owner="manager",
        manager_pid=manager_pid,
        manager_start_identity=process_identity(manager_pid),
    )
    _write_live_manager_pid(agent, pid=manager_pid)
    journal_dir = agent._working_dir / MANAGER_DIR / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / f"{request.run_id}.json").write_text(
        json.dumps({
            "schema": "lingtai.daemon_manager_journal.v1",
            "run_id": request.run_id,
            "request": encode_request(request),
            "state": "active",
            "manager_pid": manager_pid,
            "capsule_in_memory": True,
            "capsule_scrubbed": True,
        }),
        encoding="utf-8",
    )

    DaemonManager(agent)

    state = DaemonRunDir.read_state_from_disk(run_dir.path)
    assert state["state"] == "running"
    assert state["owner"] == "manager"
    assert state.get("finished_at") is None


def test_central_manager_cli_route_dispatches_without_waiting_for_queued_pid(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path, ["file", "daemon"])
    _install_fake_opencode(tmp_path, monkeypatch, sleep_s=1.0)
    manager = DaemonManager(agent, manager_pool_size=1)

    try:
        start = time.monotonic()
        result = manager._handle_emanate_cli(
            [
                {"task": "slow cli task A", "tools": ["file"]},
                {"task": "slow cli task B", "tools": ["file"]},
            ],
            backend="opencode",
            effective_max_turns=1,
            effective_timeout=10.0,
        )
        elapsed = time.monotonic() - start

        assert result["status"] == "dispatched", result
        assert result["count"] == 2
        assert elapsed < 0.75

        run_dirs = [manager._emanations[run_id]["run_dir"] for run_id in result["ids"]]
        queue_dir = agent._working_dir / MANAGER_DIR / "queue"

        _wait_for(
            lambda: DaemonRunDir.read_state_from_disk(run_dirs[0].path).get("supervisor_pid"),
            timeout=5.0,
            message="first managed CLI run to start",
        )
        second_state = DaemonRunDir.read_state_from_disk(run_dirs[1].path)
        assert not second_state.get("supervisor_pid")
        assert (queue_dir / f"{result['ids'][1]}.json").exists()

        for run_dir in run_dirs:
            _wait_state(run_dir, "done", timeout=10.0)
    finally:
        _terminate_resident_manager(agent)


def test_default_disabled_routing_uses_legacy_spawn_adapter(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path, ["file", "daemon"])
    records = install_fake_detached_owner(monkeypatch)
    manager = DaemonManager(agent, manager_pool_size=0)

    result = manager._handle_emanate([
        {"task": "legacy detached", "tools": ["file"]},
    ])

    assert result["status"] == "dispatched"
    assert len(records) == 1
    assert not (agent._working_dir / MANAGER_DIR / "queue").exists()


def test_central_manager_defaults_pool_100_all_batch_sizes(tmp_path):
    agent = SimpleNamespace(
        service=SimpleNamespace(model="mock"),
        _working_dir=tmp_path / "agent",
        _log=lambda *a, **k: None,
    )
    manager = DaemonManager(agent)

    assert manager._manager_pool_size == 100
    assert manager._should_use_central_daemon_manager(1) is (os.name == "posix")
    assert manager._should_use_central_daemon_manager(50) is (os.name == "posix")
    assert manager._should_use_central_daemon_manager(51) is (os.name == "posix")


def test_central_manager_orders_queue_by_enqueued_at(tmp_path, monkeypatch):
    older, older_req = _make_run(tmp_path, "em-z-older")
    newer, newer_req = _make_run(tmp_path, "em-a-newer")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, newer_req, enqueued_at=200.0)
    _write_job(queue_dir, older_req, enqueued_at=100.0)
    starts: list[str] = []

    def fake_run(rd, manifest, capsule):
        starts.append(rd.run_id)
        rd.mark_done(rd.run_id)

    monkeypatch.setattr("lingtai.adapters.posix.daemon_manager._run_one_emanation", fake_run)

    manager = _manager_with_capsules(
        queue_dir,
        journal_dir,
        pool_size=1,
        capsules={older_req.run_id: {}, newer_req.run_id: {}},
    )
    manager.run()

    assert starts == ["em-z-older", "em-a-newer"]
    assert _wait_state(older, "done")["state"] == "done"
    assert _wait_state(newer, "done")["state"] == "done"


def test_central_manager_terminal_journal_scrubs_capsule(tmp_path, monkeypatch):
    run_dir, request = _make_run(tmp_path, "em-scrub")
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    _write_job(queue_dir, request, capsule={"api_key": "secret", "backend_env": {"X": "Y"}})

    def fake_run(rd, manifest, capsule):
        assert capsule["api_key"] == "secret"
        rd.mark_done("manager completed")

    monkeypatch.setattr("lingtai.adapters.posix.daemon_manager._run_one_emanation", fake_run)

    manager = _manager_with_capsules(
        queue_dir,
        journal_dir,
        pool_size=1,
        capsules={request.run_id: {"api_key": "secret", "backend_env": {"X": "Y"}}},
    )
    manager.run()

    _wait_state(run_dir, "done")
    record = json.loads((journal_dir / "em-scrub.json").read_text(encoding="utf-8"))
    assert "capsule" not in record
    assert record["capsule_scrubbed"] is True


def test_central_manager_recovery_error_keeps_evidence(tmp_path):
    journal_dir = tmp_path / "manager" / "journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "em-bad.json").write_text(
        json.dumps({
            "schema": "lingtai.daemon_manager_journal.v1",
            "run_id": "em-bad",
            "request": "not-a-valid-request",
            "state": "active",
        }),
        encoding="utf-8",
    )

    manager = _DaemonManagerProcess(tmp_path / "manager" / "queue", journal_dir, pool_size=1)
    manager.recover_interrupted_active_runs()

    record = json.loads((journal_dir / "em-bad.json").read_text(encoding="utf-8"))
    assert record["state"] == "recovery_error"
    assert record["source"].endswith("em-bad.json")
    assert record["error"]["type"]


def test_central_manager_malformed_top_level_queue_job_is_terminal(tmp_path):
    queue_dir = tmp_path / "manager" / "queue"
    journal_dir = tmp_path / "manager" / "journal"
    queue_dir.mkdir(parents=True)
    (queue_dir / "em-bad.json").write_text("[1, 2, 3]", encoding="utf-8")

    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=1)
    manager.run()

    assert not (queue_dir / "em-bad.json").exists()
    record = json.loads((journal_dir / "em-bad.json").read_text(encoding="utf-8"))
    assert record["state"] == "failed_malformed_queue_job"
    assert record["error"]["type"] == "ValueError"


def test_reclaim_cancels_active_and_queued_central_manager_runs(tmp_path, monkeypatch):
    """One daemon-family reclaim cancels central-manager active and queued runs."""
    from lingtai.cli_daemon import (
        _CliDaemonAgent,
        _ReadOnlyDaemonView,
        _dispatch_through_tool_family,
    )

    agent = make_daemon_agent(tmp_path, ["file", "daemon"])
    _enable_detached_fake_llm(monkeypatch, agent, sleep_s=8.0)
    manager = DaemonManager(agent, manager_pool_size=1)
    try:
        result = manager._handle_emanate([
            {"task": "slow task A", "tools": ["file"]},
            {"task": "slow task B", "tools": ["file"]},
        ])
        active_dir = manager._emanations[result["ids"][0]]["run_dir"]
        queued_dir = manager._emanations[result["ids"][1]]["run_dir"]
        queued_job = agent._working_dir / MANAGER_DIR / "queue" / f"{result['ids'][1]}.json"
        _wait_for(
            lambda: DaemonRunDir.read_state_from_disk(active_dir.path).get("supervisor_pid"),
            timeout=10.0,
            message="first managed run to start",
        )
        assert queued_job.exists()

        outcome = _dispatch_through_tool_family(agent, "reclaim", {})
        assert outcome == {"status": "reclaimed", "cancelled": 2, "natural_terminal": 0}
        _wait_state(active_dir, "cancelled")
        _wait_state(queued_dir, "cancelled")
        assert not queued_job.exists()

        view = _ReadOnlyDaemonView(_CliDaemonAgent.for_inspection(agent._working_dir))
        listed = view._handle_list(
            contains="", status_filter="cancelled", include_done=True, limit=None,
        )
        assert set(result["ids"]) <= {entry["id"] for entry in listed["emanations"]}
    finally:
        _terminate_resident_manager(agent)
