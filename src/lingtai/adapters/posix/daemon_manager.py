"""Thin resident POSIX manager for queued detached daemon runs.

The manager is deliberately narrower than the daemon tool: it owns only
filesystem intake, a durable run journal, execution-child spawning through the
existing supervisor runtime helpers, timeout/control polling, terminal truth,
and notification publishing. The default daemon path never imports or starts
this module unless the caller explicitly enables a manager pool.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
import hashlib
from pathlib import Path
from typing import Any

from lingtai.adapters.posix.daemon_capsule import (
    ReceivedDaemonCapsule,
    close_fd,
    receive_capsule,
    send_capsule,
)
from lingtai.kernel._fsutil import atomic_write_json, read_json
from lingtai.kernel.daemon_supervisor import (
    DaemonSupervisorRequest,
    decode_request,
    encode_request,
)


MANAGER_DIR = Path("daemon") / "manager"
ENTRYPOINT_MODULE = "lingtai.adapters.posix.daemon_manager_entrypoint"
_MANAGER_RUNTIME_IDENTITY_SCHEMA = "lingtai.daemon_manager_runtime.v1"
_DAEMON_NOTIFICATION_PROTOCOL = "per-run-mini-channel.v1"
_POLL_INTERVAL_S = 0.05
_PID_STALE_AFTER_S = 2.0
_CAPSULE_SOCKET_NAME = "capsule.sock"
_CAPSULE_SEND_TIMEOUT_S = 5.0
_MAX_CAPSULE_BYTES = 4 * 1024 * 1024
_UNIX_SOCKET_PATH_LIMIT = 100


def _manager_dir(agent_working_dir: Path) -> Path:
    return Path(agent_working_dir) / MANAGER_DIR


def _capsule_socket_path(root: Path) -> Path:
    direct = root / _CAPSULE_SOCKET_NAME
    if len(str(direct)) < _UNIX_SOCKET_PATH_LIMIT:
        return direct
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:24]
    fallback = Path("/tmp") / f"lingtai-dm-{os.getuid()}-{digest}" / _CAPSULE_SOCKET_NAME
    if len(str(fallback)) >= _UNIX_SOCKET_PATH_LIMIT:
        raise RuntimeError("daemon manager capsule socket path exceeds the POSIX limit")
    return fallback


def _is_capsule_socket_fallback(path: Path) -> bool:
    prefix = f"lingtai-dm-{os.getuid()}-"
    directory = path.parent.name
    digest = directory[len(prefix):] if directory.startswith(prefix) else ""
    return (
        path.name == _CAPSULE_SOCKET_NAME
        and path.parent.parent == Path("/tmp")
        and len(digest) == 24
        and all(char in "0123456789abcdef" for char in digest)
    )


def _prepare_capsule_socket_path(socket_path: Path) -> None:
    """Prepare a direct socket path or validate the private ``/tmp`` fallback."""
    if not _is_capsule_socket_fallback(socket_path):
        _private_dir(socket_path.parent)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        return

    directory = socket_path.parent
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    directory_stat = directory.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise RuntimeError(
            "daemon manager capsule fallback directory must be a real, "
            "owner-owned mode-0700 directory"
        )

    try:
        socket_stat = socket_path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISSOCK(socket_stat.st_mode)
        or socket_stat.st_uid != os.getuid()
        or stat.S_IMODE(socket_stat.st_mode) & 0o077
    ):
        raise RuntimeError(
            "refusing to replace an unsafe daemon manager capsule fallback socket"
        )
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_manager_pid_info(root: Path) -> dict[str, Any]:
    try:
        info = read_json(root / "manager.pid", default={}, expect=dict)
    except TypeError:
        return {}
    return info if isinstance(info, dict) else {}


def _live_manager_info(root: Path) -> dict[str, Any]:
    info = _read_manager_pid_info(root)
    pid = info.get("pid")
    identity = info.get("manager_start_identity")
    if (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and _pid_alive(pid)
        and _process_identity_matches(pid, identity)
    ):
        return info
    return {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _manager_runtime_identity() -> dict[str, str]:
    """Return the compatibility identity of this process's central manager code."""
    from lingtai.adapters.posix.git_cli import PosixGitCliAdapter
    from lingtai.kernel.runtime_identity import runtime_identity

    source_root = _source_root().resolve()
    runtime = runtime_identity(PosixGitCliAdapter(source_root))
    code_head = runtime.get("git_commit") or runtime.get("stamp") or "unknown"
    return {
        "schema": _MANAGER_RUNTIME_IDENTITY_SCHEMA,
        "loaded_code_head": str(code_head),
        "source_root": str(source_root),
        "daemon_notification_protocol": _DAEMON_NOTIFICATION_PROTOCOL,
    }


def _manager_env() -> dict[str, str]:
    env = dict(os.environ)
    parts = [str(_source_root())]
    parts.extend(p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return env


def _ensure_manager(agent_working_dir: Path, *, pool_size: int) -> None:
    """Reuse the agent's resident manager, or reserve and spawn exactly one.

    The complete observe/identity/reserve/spawn sequence runs under one
    exclusive ``fcntl.flock`` on ``manager.lock`` so concurrent callers for the
    same agent directory cannot both observe an absent manager and both spawn
    one; the later caller instead sees the ``starting`` reservation.
    """
    import fcntl

    root = _manager_dir(agent_working_dir)
    _private_dir(root)
    with open(root / "manager.lock", "a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            _ensure_manager_locked(agent_working_dir, root, pool_size=pool_size)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _ensure_manager_locked(agent_working_dir: Path, root: Path, *, pool_size: int) -> None:
    pid_path = root / "manager.pid"
    expected_runtime_identity = _manager_runtime_identity()
    try:
        info = read_json(pid_path, default={}, expect=dict)
    except TypeError:
        info = {}
    pid = info.get("pid") if isinstance(info, dict) else None
    started_at = info.get("started_at") if isinstance(info, dict) else None
    start_identity = info.get("manager_start_identity") if isinstance(info, dict) else None
    is_live = (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and _pid_alive(pid)
        and _process_identity_matches(pid, start_identity)
    )
    if is_live:
        if info.get("manager_runtime_identity") == expected_runtime_identity:
            return
        raise RuntimeError(
            "resident central daemon manager runtime identity does not match the "
            "current agent (loaded code head, source root, or daemon notification "
            "protocol); refusing to submit new work; see daemon-manual for safe "
            "diagnosis and recovery"
        )
    if isinstance(started_at, (int, float)) and time.time() - started_at < _PID_STALE_AFTER_S:
        if info.get("manager_runtime_identity") == expected_runtime_identity:
            return
        raise RuntimeError(
            "starting central daemon manager runtime identity does not match the "
            "current agent; refusing to submit new work; see daemon-manual for safe "
            "diagnosis and recovery"
        )
    token = uuid.uuid4().hex
    _write_private_json(
        pid_path,
        {
            "pid": None,
            "started_at": time.time(),
            "pool_size": pool_size,
            "state": "starting",
            "manager_token": token,
            "manager_runtime_identity": expected_runtime_identity,
        },
    )
    env = _manager_env()
    env["LINGTAI_DAEMON_MANAGER_TOKEN"] = token
    subprocess.Popen(
        [sys.executable, "-m", ENTRYPOINT_MODULE, str(agent_working_dir), str(pool_size)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(agent_working_dir),
        start_new_session=True,
        close_fds=True,
    )


def enqueue_manager_run(
    *,
    agent_working_dir: Path,
    request: DaemonSupervisorRequest,
    capsule: dict | None,
    pool_size: int,
    adopted_fd: int | None = None,
) -> None:
    """Durably enqueue one run and adopt its optional child descriptor."""
    try:
        _enqueue_manager_run_owned(
            agent_working_dir=agent_working_dir,
            request=request,
            capsule=capsule,
            pool_size=pool_size,
            adopted_fd=adopted_fd,
        )
    finally:
        close_fd(adopted_fd)


def _enqueue_manager_run_owned(
    *,
    agent_working_dir: Path,
    request: DaemonSupervisorRequest,
    capsule: dict | None,
    pool_size: int,
    adopted_fd: int | None,
) -> None:
    if os.name != "posix":
        raise NotImplementedError("central daemon manager is POSIX-only")
    if pool_size <= 0:
        raise ValueError("central daemon manager requires a positive pool size")
    root = _manager_dir(Path(agent_working_dir))
    _ensure_manager(Path(agent_working_dir), pool_size=pool_size)
    queue_dir = root / "queue"
    _private_dir(queue_dir)
    _private_dir(root / "journal")
    _mark_run_manager_owned(request, root)
    payload = {
        "schema": "lingtai.daemon_manager_job.v1",
        "run_id": request.run_id,
        "request": encode_request(request),
        "capsule_in_memory": True,
        "enqueued_at": time.time(),
    }
    job_path = queue_dir / f"{request.run_id}.json"
    _write_private_json(job_path, payload)
    try:
        _send_capsule(
            root,
            request.run_id,
            capsule or {},
            adopted_fd=adopted_fd,
        )
    except Exception:
        try:
            job_path.unlink()
        except OSError:
            pass
        raise
    _mark_run_manager_owned(request, root)


def run_manager(agent_working_dir: Path, *, pool_size: int) -> None:
    """Run the resident queue manager until idle for a short grace period."""
    if pool_size <= 0:
        raise ValueError("pool_size must be positive")
    root = _manager_dir(Path(agent_working_dir))
    queue_dir = root / "queue"
    journal_dir = root / "journal"
    for path in (root, queue_dir, journal_dir):
        _private_dir(path)
    _write_private_json(
        root / "manager.pid",
        {
            "pid": os.getpid(),
            "started_at": time.time(),
            "pool_size": pool_size,
            "state": "running",
            "manager_start_identity": _process_identity(os.getpid()),
            "manager_token": os.environ.get("LINGTAI_DAEMON_MANAGER_TOKEN"),
            "manager_runtime_identity": _manager_runtime_identity(),
        },
    )
    manager = _DaemonManagerProcess(queue_dir, journal_dir, pool_size=pool_size)
    manager.start_capsule_server(_capsule_socket_path(root))
    manager.recover_interrupted_active_runs()
    manager.run(idle_exit_s=None)


class _DaemonManagerProcess:
    def __init__(self, queue_dir: Path, journal_dir: Path, *, pool_size: int) -> None:
        self.queue_dir = queue_dir
        self.journal_dir = journal_dir
        self.pool_size = pool_size
        self.active: dict[str, threading.Thread] = {}
        self.capsules: dict[str, ReceivedDaemonCapsule] = {}
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.started_at = time.time()
        self._capsule_socket: socket.socket | None = None
        self._capsule_server_thread: threading.Thread | None = None

    def start_capsule_server(self, socket_path: Path) -> None:
        """Accept one-shot runtime capsules over a private local socket."""
        _prepare_capsule_socket_path(socket_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(socket_path))
        try:
            socket_path.chmod(0o600)
        except OSError:
            pass
        sock.listen()
        self._capsule_socket = sock
        thread = threading.Thread(
            target=self._serve_capsules,
            name="daemon-manager-capsules",
            daemon=True,
        )
        self._capsule_server_thread = thread
        thread.start()

    def _serve_capsules(self) -> None:
        sock = self._capsule_socket
        if sock is None:
            return
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            with conn:
                wire = None
                accepted: tuple[str, ReceivedDaemonCapsule] | None = None
                try:
                    wire = self._read_capsule_message(conn)
                    data = wire.value
                    run_id = data.get("run_id")
                    capsule = data.get("capsule")
                    if isinstance(run_id, str) and isinstance(capsule, dict):
                        pending = ReceivedDaemonCapsule(
                            value=capsule,
                            adopted_fd=wire.take_fd(),
                        )
                        with self.lock:
                            previous = self.capsules.get(run_id)
                            self.capsules[run_id] = pending
                        accepted = (run_id, pending)
                        if previous is not None:
                            previous.close()
                        conn.sendall(b"OK")
                        accepted = None
                    else:
                        conn.sendall(b"ERR")
                except Exception:
                    try:
                        conn.sendall(b"ERR")
                    except OSError:
                        pass
                finally:
                    if accepted is not None:
                        run_id, pending = accepted
                        with self.lock:
                            if self.capsules.get(run_id) is pending:
                                self.capsules.pop(run_id, None)
                        pending.close()
                    if wire is not None:
                        wire.close()

    def _read_capsule_message(
        self, conn: socket.socket
    ) -> ReceivedDaemonCapsule:
        return receive_capsule(conn)

    def recover_interrupted_active_runs(self) -> None:
        """Mark manager-owned active runs from a prior crash failed."""
        for path in sorted(self.journal_dir.glob("*.json")):
            record = read_json(path, default={}, expect=dict)
            if not isinstance(record, dict) or record.get("state") != "active":
                continue
            run_id = str(record.get("run_id") or path.stem)
            try:
                request = decode_request(str(record["request"]))
                manifest = _read_manifest_for_request(request)
                run_dir = _attach_run_dir(manifest)
                state = run_dir.read_state_from_disk(run_dir.path)
                if state.get("state") not in {"done", "failed", "cancelled", "timeout"}:
                    run_dir.update_state(
                        owner="manager",
                        manager_recovered_by=os.getpid(),
                    )
                    _terminate_exact_children(run_dir)
                    run_dir.mark_failed(RuntimeError(
                        "central daemon manager recovered an active journal "
                        "entry after manager restart; prior manager died before "
                        "committing terminal truth"
                    ))
                _publish_terminal(run_dir, manifest)
                self._journal(run_id, "failed_recovered", request, {})
            except Exception as exc:
                self._journal_raw(path.stem, {
                    "state": "recovery_error",
                    "run_id": run_id,
                    "source": str(path),
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "updated_at": time.time(),
                })

    def run(self, *, idle_exit_s: float | None = 2.0) -> None:
        idle_since: float | None = None
        try:
            while True:
                self._reap_finished_threads()
                self._consume_queue_cancel_requests()
                self._start_queued_jobs()
                if self.active or list(self.queue_dir.glob("*.json")):
                    idle_since = None
                    self.last_activity = time.monotonic()
                else:
                    if idle_exit_s is None:
                        time.sleep(_POLL_INTERVAL_S)
                        continue
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since > idle_exit_s:
                        return
                time.sleep(_POLL_INTERVAL_S)
        finally:
            with self.lock:
                pending_capsules = list(self.capsules.values())
                self.capsules.clear()
            for pending in pending_capsules:
                pending.close()

    def _reap_finished_threads(self) -> None:
        with self.lock:
            finished = [run_id for run_id, thread in self.active.items() if not thread.is_alive()]
            for run_id in finished:
                self.active.pop(run_id, None)

    def _consume_queue_cancel_requests(self) -> None:
        """Honor durable reclaim requests for still-queued jobs before start.

        A queued job has no execution worker and therefore no control watcher
        yet; this pass is the queue-phase consumer of the same run-local
        ``control/`` spool. The queue job file's ``unlink()`` is the single
        arbitration token, shared with ``_start_queued_jobs``: whoever unlinks
        the job owns the run's fate, so a pending reclaim is honored on
        exactly one branch — here (terminal ``cancelled`` before start) or by
        the started worker's own active watcher. Queued ``ask`` requests are
        deliberately left untouched for that future watcher.
        """
        from lingtai.kernel.daemon_supervisor import control

        for job_path in sorted(self.queue_dir.glob("*.json")):
            job = read_json(job_path, default={}, expect=dict)
            if not isinstance(job, dict) or "request" not in job:
                # Malformed jobs stay owned by _start_queued_jobs' quarantine.
                continue
            try:
                request = decode_request(str(job["request"]))
            except Exception:
                continue
            # Canonical layout puts the manifest inside the run directory, so
            # the spool can be probed cheaply before any manifest read.
            run_path = Path(request.manifest_path).parent
            pending = control.pending_requests(run_path)
            if not pending:
                continue
            try:
                manifest = _read_manifest_for_request(request)
                run_dir = _attach_run_dir(manifest)
            except Exception:
                continue
            reclaim_requests: list[Path] = []
            for req_path in pending:
                try:
                    req = control.read_request(req_path)
                except Exception:
                    control.mark_request_done(
                        req_path, {"status": "error", "error": "unreadable request"}
                    )
                    continue
                if req.get("run_id") != request.run_id:
                    control.mark_request_done(
                        req_path, {"status": "rejected", "error": "run_id mismatch"}
                    )
                    continue
                if req.get("kind") != "reclaim":
                    continue
                reclaim_requests.append(req_path)
            if not reclaim_requests:
                continue
            state = run_dir.read_state_from_disk(run_dir.path)
            if state.get("state") not in {"running", "active"}:
                for req_path in reclaim_requests:
                    control.mark_request_done(req_path, {
                        "status": "rejected",
                        "error": f"run is already {state.get('state')!r}",
                    })
                continue
            try:
                job_path.unlink()
            except OSError:
                continue  # the worker won the handoff; its watcher consumes this
            with self.lock:
                pending = self.capsules.pop(request.run_id, None)
            if pending is not None:
                pending.close()
            run_dir.update_state(owner="manager", manager_pid=os.getpid())
            run_dir.mark_cancelled()
            _publish_terminal(run_dir, manifest)
            self._journal(request.run_id, "cancelled_queued", request, {})
            for req_path in reclaim_requests:
                control.mark_request_done(
                    req_path, {"status": "accepted", "phase": "queued"}
                )

    def _start_queued_jobs(self) -> None:
        with self.lock:
            capacity = self.pool_size - len(self.active)
        if capacity <= 0:
            return
        jobs = sorted(
            self.queue_dir.glob("*.json"),
            key=lambda path: (*self._queue_sort_key(path), path.name),
        )
        for job_path in jobs[:capacity]:
            job = read_json(job_path, default={}, expect=dict)
            if not isinstance(job, dict):
                self._quarantine_malformed_job(
                    job_path,
                    ValueError("manager queue job JSON must be an object"),
                )
                continue
            try:
                if "request" not in job:
                    raise ValueError("manager queue job missing request")
                request = decode_request(str(job["request"]))
            except Exception as exc:
                self._quarantine_malformed_job(job_path, exc)
                continue
            with self.lock:
                pending = self.capsules.pop(request.run_id, None)
            if pending is None:
                if self._missing_capsule_is_terminal(job):
                    self._fail_unavailable_capsule_job(job_path, request)
                continue
            try:
                job_path.unlink()
            except OSError:
                restored = False
                with self.lock:
                    if request.run_id not in self.capsules:
                        self.capsules[request.run_id] = pending
                        restored = True
                if not restored:
                    pending.close()
                continue
            self._journal(request.run_id, "active", request, pending.value)
            thread = threading.Thread(
                target=self._run_job,
                args=(request, pending),
                name=f"daemon-manager-{request.run_id}",
                daemon=True,
            )
            with self.lock:
                self.active[request.run_id] = thread
            thread.start()

    def _queue_sort_key(self, job_path: Path) -> tuple[float, str]:
        try:
            job = read_json(job_path, default={}, expect=dict)
        except Exception:
            return (float("inf"), job_path.name)
        enqueued_at = job.get("enqueued_at") if isinstance(job, dict) else None
        if isinstance(enqueued_at, (int, float)) and not isinstance(enqueued_at, bool):
            return (float(enqueued_at), job_path.name)
        return (float("inf"), job_path.name)

    def _missing_capsule_is_terminal(self, job: dict[str, Any]) -> bool:
        enqueued_at = job.get("enqueued_at")
        if isinstance(enqueued_at, bool) or not isinstance(enqueued_at, (int, float)):
            return True
        deadline = max(float(enqueued_at), self.started_at) + _CAPSULE_SEND_TIMEOUT_S
        return time.time() >= deadline

    def _fail_unavailable_capsule_job(
        self,
        job_path: Path,
        request: DaemonSupervisorRequest,
    ) -> None:
        evidence_timestamp = time.time()
        evidence = {
            "reason": "manager_restart_capsule_unavailable",
            "source": str(job_path),
            "timestamp": evidence_timestamp,
            "manager_pid": os.getpid(),
        }
        try:
            manifest = _read_manifest_for_request(request)
            run_dir = _attach_run_dir(manifest)
            state = run_dir.read_state_from_disk(run_dir.path)
            if state.get("state") not in {"done", "failed", "cancelled", "timeout"}:
                run_dir.update_state(
                    owner="manager",
                    manager_recovered_by=os.getpid(),
                )
                run_dir.mark_failed(RuntimeError(
                    "manager_restart_capsule_unavailable: central daemon manager "
                    "found a queued job after restart but its in-memory runtime "
                    f"capsule was unavailable; source={job_path}"
                ))
            _publish_terminal(run_dir, manifest)
        except Exception as exc:
            evidence["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        self._journal_raw(request.run_id, {
            "schema": "lingtai.daemon_manager_journal.v1",
            "run_id": request.run_id,
            "request": encode_request(request),
            "state": "failed_missing_capsule",
            "capsule_in_memory": False,
            "capsule_scrubbed": True,
            "evidence": evidence,
            "manager_pid": os.getpid(),
            "updated_at": evidence_timestamp,
        })
        try:
            job_path.unlink()
        except OSError:
            pass

    def _quarantine_malformed_job(self, job_path: Path, exc: Exception) -> None:
        with self.lock:
            pending = self.capsules.pop(job_path.stem, None)
        if pending is not None:
            pending.close()
        self._journal_raw(job_path.stem, {
            "schema": "lingtai.daemon_manager_journal.v1",
            "run_id": job_path.stem,
            "state": "failed_malformed_queue_job",
            "source": str(job_path),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "manager_pid": os.getpid(),
            "updated_at": time.time(),
        })
        try:
            job_path.unlink()
        except OSError:
            pass

    def _run_job(
        self,
        request: DaemonSupervisorRequest,
        pending: ReceivedDaemonCapsule,
    ) -> None:
        try:
            manifest = _read_manifest_for_request(request)
            run_dir = _attach_run_dir(manifest)
            if request.run_id != manifest.get("run_id"):
                run_dir.update_state(owner="manager", supervisor_pid=os.getpid())
                run_dir.mark_failed(ValueError("manager request/manifest run_id mismatch"))
                _publish_terminal(run_dir, manifest)
                return
            run_dir.update_state(
                owner="manager",
                supervisor_pid=os.getpid(),
                supervisor_start_identity=_process_identity(os.getpid()),
                supervisor_manifest_path=str(Path(request.manifest_path).resolve()),
                manager_pid=os.getpid(),
            )
            adopted_fd = pending.take_fd()
            if adopted_fd is None:
                _run_one_emanation(run_dir, manifest, pending.value)
            else:
                _run_one_emanation(
                    run_dir,
                    manifest,
                    pending.value,
                    adopted_fd=adopted_fd,
                )
        except Exception as exc:
            try:
                manifest = _read_manifest_for_request(request)
                run_dir = _attach_run_dir(manifest)
                run_dir.mark_failed(exc)
                _publish_terminal(run_dir, manifest)
            except Exception:
                pass
        finally:
            try:
                manifest = _read_manifest_for_request(request)
                run_dir = _attach_run_dir(manifest)
                _publish_terminal(run_dir, manifest)
            except Exception:
                pass
            pending.close()
            self._journal(request.run_id, "terminal", request, pending.value)

    def _journal(
        self,
        run_id: str,
        state: str,
        request: DaemonSupervisorRequest,
        capsule: dict,
    ) -> None:
        payload = {
            "schema": "lingtai.daemon_manager_journal.v1",
            "run_id": run_id,
            "request": encode_request(request),
            "state": state,
            "manager_pid": os.getpid(),
            "updated_at": time.time(),
        }
        payload["capsule_in_memory"] = state == "active"
        payload["capsule_scrubbed"] = True
        self._journal_raw(run_id, payload)

    def _journal_raw(self, run_id: str, payload: dict[str, Any]) -> None:
        _write_private_json(self.journal_dir / f"{run_id}.json", payload)


def _read_manifest_for_request(request: DaemonSupervisorRequest) -> dict:
    from lingtai.kernel.daemon_supervisor.manifest import read_manifest

    return read_manifest(Path(request.manifest_path))


def _attach_run_dir(manifest: dict):
    from lingtai.tools.daemon.run_dir import DaemonRunDir

    return DaemonRunDir.attach(Path(manifest["run_dir"]))


def _mark_run_manager_owned(request: DaemonSupervisorRequest, root: Path) -> None:
    try:
        manifest = _read_manifest_for_request(request)
        run_dir = _attach_run_dir(manifest)
    except Exception:
        return
    updates: dict[str, Any] = {"owner": "manager"}
    info = _live_manager_info(root)
    pid = info.get("pid")
    identity = info.get("manager_start_identity")
    if isinstance(pid, int) and not isinstance(pid, bool):
        updates["manager_pid"] = pid
        updates["manager_start_identity"] = identity
    run_dir.update_state(**updates)


def _send_capsule(
    root: Path,
    run_id: str,
    capsule: dict,
    *,
    adopted_fd: int | None = None,
) -> None:
    socket_path = _capsule_socket_path(root)
    payload = json.dumps(
        {"run_id": run_id, "capsule": capsule},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_CAPSULE_BYTES:
        raise ValueError("daemon manager capsule exceeds size limit")
    deadline = time.monotonic() + _CAPSULE_SEND_TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect(str(socket_path))
                send_capsule(sock, payload, adopted_fd=adopted_fd)
                ack = bytearray()
                while len(ack) < 2:
                    chunk = sock.recv(2 - len(ack))
                    if not chunk:
                        break
                    ack.extend(chunk)
                if ack == b"OK":
                    return
                raise RuntimeError("daemon manager rejected runtime capsule")
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            last_error = exc
            time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        f"central daemon manager did not accept runtime capsule for {run_id!r}"
    ) from last_error


def _process_identity(pid: int) -> str | None:
    from lingtai.adapters.posix.process_identity import process_identity

    return process_identity(pid)


def _process_identity_matches(pid: int, saved_identity: object) -> bool:
    from lingtai.adapters.posix.process_identity import process_identity_matches

    return process_identity_matches(pid, saved_identity)


def _run_one_emanation(
    run_dir,
    manifest: dict,
    capsule: dict,
    *,
    adopted_fd: int | None = None,
) -> None:
    try:
        from lingtai.tools.daemon.supervisor_runtime import _run_one_emanation
    except Exception:
        close_fd(adopted_fd)
        raise

    _run_one_emanation(
        run_dir,
        manifest,
        capsule,
        adopted_fd=adopted_fd,
    )


def _publish_terminal(run_dir, manifest: dict) -> None:
    from lingtai.tools.daemon.supervisor_runtime import _publish_terminal_notification_if_needed

    _publish_terminal_notification_if_needed(run_dir, manifest)


def _terminate_exact_children(run_dir) -> None:
    from lingtai.tools.daemon.supervisor_runtime import _terminate_exact_run_children

    _terminate_exact_run_children(run_dir, owned_procs=())
