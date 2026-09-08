"""lingtai-agent run <working_dir> — boot agent into ASLEEP, wake on external messages."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from lingtai.adapters.posix.event_journal import PosixJsonlEventJournalAdapter
from lingtai.adapters.posix.git_cli import PosixGitCliAdapter
from lingtai.adapters.posix.mail import PosixFilesystemMailAdapter
from lingtai.adapters.posix.agent_presence import PosixAgentPresenceStoreAdapter
from lingtai.adapters.posix.notification_store import PosixNotificationStoreAdapter
from lingtai.adapters.lifecycle_clock import SystemLifecycleClockAdapter
from lingtai.adapters.stream_progress import loopback_stream_progress_factory
from lingtai.adapters.refresh_watcher import select_refresh_watcher
from lingtai.adapters.workdir_lease import select_workdir_lease
from lingtai.kernel.config_resolve import (
    load_env_file,
    resolve_env_checked,
)
from lingtai.init_reader import InitReadStatus, read_init, reader_callbacks
from lingtai.llm.service import (
    CONSERVATIVE_CONTEXT_WINDOW,
    LLMService,
    build_provider_defaults_from_manifest_llm,
)
from lingtai.agent import Agent
from lingtai.kernel.process_match import match_agent_acp, match_agent_run
from lingtai.tools.system.settings import resolve_runtime_policy


def load_init(working_dir: Path) -> dict:
    """Read ``init.json`` through the shared real-reader path.

    Boot reports the same structured outcome as live refresh.  This function is
    intentionally read-only with respect to the user's file: compatibility and
    deprecated fields are diagnosed, never stripped or written back.
    """
    from lingtai.agent import load_preset

    materialize, prepare = reader_callbacks(working_dir, load_preset=load_preset)
    outcome = read_init(
        working_dir,
        materialize=materialize,
        prepare=prepare,
        failure_behavior="STOP",
    )
    if outcome.status is InitReadStatus.READ_FAILED:
        print(f"error: {json.dumps(outcome.to_payload(), ensure_ascii=False, default=str)}", file=sys.stderr)
        sys.exit(1)

    from lingtai.kernel.workdir import write_resolved_manifest
    effective_path = write_resolved_manifest(working_dir, outcome.data or {})
    if effective_path is not None:
        outcome.effective_config_source = str(effective_path)
    print(
        f"init.json reader: {json.dumps(outcome.to_payload(), ensure_ascii=False, default=str)}",
        file=sys.stderr,
    )
    assert outcome.data is not None
    return outcome.data


def _raise_env_miss(message: str, env_file: str | None) -> None:
    """Boot-time hard failure when ``manifest.llm.api_key_env`` cannot resolve.

    A fresh agent with no API key can never make an LLM call, so failing fast
    at boot with a message naming the variable is strictly better than an
    opaque provider auth error on the first turn.
    """
    raise ValueError(
        f"{message}; the agent cannot boot without it "
        f"(env_file: {env_file!r})"
    )


def build_llm_service(
    data: dict, working_dir: Path, runtime_policy: Any | None = None
) -> LLMService:
    """Construct the manifest's ``LLMService`` — no Agent, lease, or heartbeat.

    Split out of :func:`build_agent` so short-lived, non-booting entry points
    (``lingtai-agent daemon``) resolve the very same provider/model/credential/
    provider-defaults configuration the boot path resolves, instead of
    re-deriving credentials themselves. Loading ``env_file`` stays with the
    caller: boot has refresh-marker semantics that a one-shot CLI must not
    inherit.

    ``max_rpm`` and ``context_limit`` come from the System-resolved runtime
    policy (env > ``settings/system.json`` v2 > fixed default),
    the same policy ``Agent._setup_from_init`` applies, so the service built
    here never disagrees with the later configured setup. Pass
    *runtime_policy* to reuse an already-resolved policy.
    """
    m = data["manifest"]
    llm = m["llm"]
    env_file = data.get("env_file")
    if runtime_policy is None:
        runtime_policy = resolve_runtime_policy(working_dir)

    api_key = resolve_env_checked(
        llm.get("api_key"),
        llm.get("api_key_env"),
        context="manifest.llm.api_key_env",
        warn=lambda msg: _raise_env_miss(msg, env_file),
    )

    # Default 60 matches AgentConfig.max_rpm — agents whose init.json
    # predates this field cooperatively share the network-wide 60 RPM cap
    # by default. Set the System v2 field or environment variable to disable gating.
    max_rpm = runtime_policy.max_rpm
    # Pass working_dir so Codex agents get their per-agent session/thread
    # identity (agent path + last molt time) wired in by default.
    provider_defaults = build_provider_defaults_from_manifest_llm(
        llm, max_rpm=max_rpm, working_dir=working_dir
    )
    context_window = runtime_policy.context_limit
    if (
        not isinstance(context_window, int)
        or isinstance(context_window, bool)
        or context_window <= 0
    ):
        context_window = CONSERVATIVE_CONTEXT_WINDOW
    return LLMService(
        provider=llm["provider"],
        model=llm["model"],
        api_key=api_key,
        base_url=llm.get("base_url"),
        context_window=context_window,
        provider_defaults=provider_defaults,
    )


def build_agent(
    data: dict,
    working_dir: Path,
    *,
    _forced_disable: frozenset[str] | None = None,
    _turn_origin_policy=None,
    _requires_turn_origin_policy: bool = False,
    _provider_call_admission_port=None,
    _derived_launch_admission_port=None,
    _requires_derived_launch_admission_port: bool = False,
) -> Agent:
    """Construct Agent from validated init data.

    Creates a minimal Agent (LLMService + working_dir + mail_service),
    then delegates all setup to _perform_refresh() which reads init.json.
    This ensures boot and live refresh share one code path.
    """
    # Load env file if specified (needed for LLM API key resolution).
    # Refresh relaunches inherit the old process env; a watcher marker lets
    # the freshly edited env_file replace those stale values once, then is
    # consumed so later child processes keep normal boot semantics.
    env_file = data.get("env_file")
    overwrite_env_file = os.environ.get("LINGTAI_REFRESH_ENV_OVERWRITE") == "1"
    if env_file:
        load_env_file(env_file, overwrite=overwrite_env_file)
    if overwrite_env_file:
        os.environ.pop("LINGTAI_REFRESH_ENV_OVERWRITE", None)

    m = data["manifest"]
    # Resolve the ordinary runtime policy once, after env_file loading, and
    # feed both the early service and the constructor-time streaming flag
    # from it; _setup_from_init re-resolves the same inputs for the
    # configured pass, so boot cannot be internally inconsistent.
    runtime_policy = resolve_runtime_policy(working_dir)
    service = build_llm_service(data, working_dir, runtime_policy)

    mail_service = PosixFilesystemMailAdapter(
        working_dir=working_dir,
        pseudo_agent_subscriptions=m.get("pseudo_agent_subscriptions", ["../human"]),
    )

    # Minimal construction — _perform_refresh reads init.json for everything else
    agent = Agent(
        service,
        agent_name=m.get("agent_name"),
        admin=m.get("admin", {}),
        working_dir=working_dir,
        workdir_lease=select_workdir_lease(working_dir),
        notification_store=PosixNotificationStoreAdapter(working_dir),
        agent_presence=PosixAgentPresenceStoreAdapter(working_dir),
        lifecycle_clock=SystemLifecycleClockAdapter(),
        refresh_watcher=select_refresh_watcher(),
        snapshot_port=PosixGitCliAdapter(working_dir),
        source_revision_port=PosixGitCliAdapter(Path(__file__).resolve().parent),
        mail_service=mail_service,
        # Config hydration follows construction on this boot path, so preserve
        # the existing constructor-time JSON serialization default (False).
        event_journal=PosixJsonlEventJournalAdapter(
            working_dir,
            ensure_ascii=False,
        ),
        streaming=runtime_policy.streaming,
        # Stream-progress is a composition-only loopback publisher. The effective
        # System runtime policy above controls streaming; BaseAgent does not invoke
        # this factory when that policy is false.
        stream_progress_factory=loopback_stream_progress_factory,
        _forced_disable=_forced_disable,
        _turn_origin_policy=_turn_origin_policy,
        _requires_turn_origin_policy=_requires_turn_origin_policy,
        _requires_derived_launch_admission_port=(
            _requires_derived_launch_admission_port
        ),
        provider_call_admission_port=_provider_call_admission_port,
        derived_launch_admission_port=_derived_launch_admission_port,
        _from_init_boot=True,
    )

    # The private sentinel applies only to construction. Clear it before the
    # established configured pass so its mandatory declared intrinsics boot
    # normally, and an early return cannot leave suppression armed.
    agent._from_init_boot = False

    # Full setup from init plus Psyche prompt-owner settings (capabilities,
    # addons, config, covenant, etc.)
    agent._setup_from_init()
    outcome = getattr(agent, "_last_init_read_outcome", None)
    if outcome is not None and outcome.status is InitReadStatus.READ_FAILED:
        print(
            f"error: {json.dumps(outcome.to_payload(), ensure_ascii=False, default=str)}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Restore molt count from previous run (if resuming)
    prev_manifest = working_dir / ".agent.json"
    if prev_manifest.is_file():
        try:
            prev = json.loads(prev_manifest.read_text(encoding="utf-8"))
            agent._molt_count = prev.get("molt_count", 0)
        except (json.JSONDecodeError, OSError):
            pass

    return agent


def _clean_signal_files(working_dir: Path) -> None:
    """Remove stale .suspend / .sleep files left over from a previous run."""
    for name in (".suspend", ".sleep", ".refresh"):
        f = working_dir / name
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass


def _stop_signal_numbers() -> list[int]:
    """Stop signals the CLI host hooks on this platform.

    POSIX delivers SIGTERM/SIGINT. Windows never delivers SIGTERM to a
    process; console-control events surface as SIGINT (Ctrl+C) and SIGBREAK
    (Ctrl+Break / console close), so those are the honest hooks there. The
    cooperative ``.suspend`` file channel remains the primary stop path on
    both platforms.
    """
    if os.name == "nt":
        numbers = [signal.SIGINT]
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            numbers.append(sigbreak)
        return numbers
    return [signal.SIGTERM, signal.SIGINT]


def _install_signal_handlers(working_dir: Path, agent: Agent) -> None:
    """Platform stop signals → touch .suspend and unblock main thread."""
    suspend_file = working_dir / ".suspend"

    def _handler(signum, frame):
        suspend_file.touch()
        agent._shutdown.set()

    for signum in _stop_signal_numbers():
        signal.signal(signum, _handler)


def _check_duplicate_process(working_dir: Path) -> None:
    """Abort if another LingTai run/ACP host for ``working_dir`` is alive.

    Defense-in-depth alongside the kernel's workdir lease — the lease prevents
    data corruption, but a duplicate process still shows up in the process
    table and can mislead users.  This check catches the case where the old
    process is mid-teardown (heartbeat file gone, lease about to be released)
    but still visible. The concrete process-table mechanism lives behind the
    platform-selected ``AgentProcessScanPort``; an unavailable scan falls
    through to the lease, which is the exclusion authority.

    Both this process and its launcher parent are excluded. On Windows a venv's
    ``Scripts\\python.exe`` is a launcher stub that runs the base interpreter as
    a CHILD process carrying a byte-identical command line, so the agent code
    runs in the child while the stub stays visible in the process table:

        PID 45676  ppid=23408  "…\\venv\\Scripts\\python.exe" -m lingtai run <dir>
        PID 36624  ppid=45676  "…\\Python311\\python.exe"     -m lingtai run <dir>

    Skipping only ``os.getpid()`` left the stub matching, so the guard reported
    its own launcher and refused to boot — deterministically, for every agent
    launched as ``<venv python> -m lingtai run <dir>``. Excluding the parent
    cannot mask a genuine duplicate: a real second agent is not this process's
    own parent, and the workdir lease remains the exclusion authority either
    way. Only the immediate parent is excluded because the Port observes
    ``(pid, command)`` and deliberately exposes no parent links to walk.
    """
    from lingtai.adapters.process_scan import select_agent_process_scan

    scan = select_agent_process_scan()
    if scan is None:
        return  # no scan mechanism on this platform — the lease is the guard
    abs_dir = str(working_dir.resolve())
    own_pids = {os.getpid(), os.getppid()}
    for pid, command in scan.iter_process_commands():
        if pid in own_pids:
            continue
        if (
            match_agent_run(command, abs_dir) is None
            and match_agent_acp(command, abs_dir) is None
        ):
            continue
        print(
            f"error: another lingtai agent is already running in {abs_dir}\n"
            f"  PID {pid}: {command}\n"
            f"  If this is a stale process, kill it first.",
            file=sys.stderr,
        )
        sys.exit(1)


def _derived_avatar_requires_admission(working_dir: Path) -> bool:
    """Whether durable avatar state restricts nested derived launches.

    The state is deliberately presence-based.  The only safe way a child can
    become less restricted is for the marker to be absent; a malformed file,
    directory, or symlink at the marker location therefore remains restrictive.
    It is not an authority credential and is only a defense against accidental
    loss of the requirement in a trusted-host deployment.
    """
    from lingtai.tools.avatar._launcher import (
        DerivedAvatarState,
        probe_derived_avatar_state,
    )

    return probe_derived_avatar_state(working_dir) is not DerivedAvatarState.ABSENT


def run(working_dir: Path) -> None:
    """Boot agent into ASLEEP — wakes on external messages (mail/imap/telegram)."""
    _check_duplicate_process(working_dir)
    _clean_signal_files(working_dir)
    # Durable file logging for daemonized agents: stderr alone is DEVNULL for
    # avatars and truncated per-spawn in logs/spawn.stderr, so logger warnings
    # (mail claim failures, adapter retries, restore diagnostics) would
    # otherwise be lost. Do this before load_init() so boot/migration warnings
    # are captured too. Short-lived inspector subcommands (log, check-caps,
    # maintenance) deliberately never reach this path.
    from lingtai.kernel.logging import setup_logging

    setup_logging(
        verbose=os.environ.get("LINGTAI_VERBOSE") == "1",
        log_dir=working_dir / "logs",
    )
    data = load_init(working_dir)

    # Resolve venv and store on agent for CPR/avatar to use
    from lingtai.venv_resolve import resolve_venv
    venv_dir = resolve_venv(data)
    # Expose the live runtime interpreter to bash/tools in a platform-neutral way.
    os.environ["LINGTAI_RUNTIME_PYTHON"] = sys.executable
    os.environ["LINGTAI_RUNTIME_VENV"] = str(venv_dir)
    # Keep the resolved venv in the in-memory effective mapping only.  The
    # reader contract never rewrites user-owned init.json; an agent/human may
    # explicitly edit it if they want to persist this choice.
    data["venv_path"] = str(venv_dir)

    # Avatar child processes persist a restrictive state in their own working
    # directory. The environment marker is only redundant transport for the
    # immediate launch. Neither carries a parent, grant, or authority bearer.
    from lingtai.tools.avatar._launcher import DERIVED_AVATAR_EXECUTION_ENV

    build_options = {}
    if (
        _derived_avatar_requires_admission(working_dir)
        or os.environ.get(DERIVED_AVATAR_EXECUTION_ENV) == "1"
    ):
        build_options["_requires_derived_launch_admission_port"] = True
    agent = build_agent(data, working_dir, **build_options)
    agent._venv_path = str(venv_dir)
    _install_signal_handlers(working_dir, agent)

    from lingtai.kernel.state import AgentState
    agent._asleep.set()
    agent._state = AgentState.ASLEEP

    # Detect refresh boot — old process renamed .refresh → .refresh.taken
    taken_file = working_dir / ".refresh.taken"
    is_refresh = taken_file.is_file()
    if is_refresh:
        taken_file.unlink()

    try:
        agent.start()

        # Kick-start after refresh — wake agent with a system message
        if is_refresh:
            from lingtai.kernel.i18n import t
            lang = agent._config.language
            agent.send(t(lang, "system.refresh_successful"), sender="system")

        agent._shutdown.wait()
    finally:
        try:
            agent.stop(timeout=10.0)
        except Exception:
            pass
        _force_exit_if_worker_poisoned(agent)


def _force_exit_if_worker_poisoned(agent) -> None:
    """Hard-exit the process when a wedged LLM worker poisoned the interface.

    A ``WorkerStillRunningError`` poison means an LLM worker thread is still
    alive inside the session's ``_timeout_pool`` ThreadPoolExecutor. The AED
    loop already skipped the unsafe chat save, put the agent ASLEEP, and
    requested a refresh whose watcher will relaunch a fresh process. A bounded
    ``stop()`` may either retain heartbeat/lease because quiescence was not
    proved, or prove quiescence and release them before this helper runs. The
    wedged worker is still a non-daemon thread that ``session.close()`` cannot
    reclaim (``shutdown(wait=False)`` cannot cancel a thread stuck in the HTTP
    call), so ``concurrent.futures``' atexit join can block interpreter exit
    indefinitely and strand the watcher-owned relaunch.

    ``_stop`` already reclaims daemon workers / CLI process groups for exactly
    this reason; a wedged LLM worker is the one resource it cannot reclaim, so
    the process owner force-exits after bounded teardown. The terminal event is
    logged only while this process still owns the workdir lease; after release,
    no Python path may append to workdir state. Guarded on the poison flag, so
    ordinary stop/refresh shutdowns are unchanged.
    """
    if not getattr(agent, "_llm_worker_interface_poisoned", False):
        return
    if getattr(agent, "_workdir_lease_acquired", True):
        try:
            agent._log(
                "process_force_exit_after_worker_poison",
                artifact=getattr(agent, "_llm_worker_poison_artifact", None),
            )
        except Exception:
            pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    # Exit 0 because the refresh watcher owns relaunch; this is handled recovery, not a crash.
    os._exit(0)


def _emit_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _handle_log_command(args) -> None:
    from lingtai.kernel.services.logging import (
        doctor_sqlite_event_index,
        query_sqlite_event_index,
        rebuild_sqlite_event_index,
    )

    agent_dir = args.agent_dir.resolve()
    if not agent_dir.is_dir():
        print(f"error: {agent_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if args.log_command == "rebuild":
        try:
            _emit_json(
                rebuild_sqlite_event_index(
                    agent_dir,
                    workdir_lease=select_workdir_lease(agent_dir),
                )
            )
        except Exception as e:
            print(f"error: failed to rebuild sqlite log index: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.log_command == "doctor":
        try:
            _emit_json(doctor_sqlite_event_index(agent_dir))
        except Exception as e:
            print(f"error: failed to inspect sqlite log index: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.log_command == "query":
        try:
            _emit_json(query_sqlite_event_index(agent_dir, args.sql))
        except Exception as e:
            print(f"error: failed to query sqlite log index: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("error: missing log subcommand", file=sys.stderr)
        sys.exit(1)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _handle_maintenance_command(args) -> None:
    if args.maintenance_command != "cleanup":
        print("error: missing maintenance subcommand", file=sys.stderr)
        sys.exit(1)

    from lingtai.kernel.maintenance import (
        RetentionOptions,
        TargetError,
        report_to_dict,
        scan_retention,
    )

    options = RetentionOptions(
        older_than_days=args.older_than_days,
        include_archive=args.include_archive,
    )
    try:
        report = scan_retention(args.target, options)
    except (TargetError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    data = report_to_dict(report)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        return

    totals = data["totals"]
    print("Retention cleanup dry-run")
    print(f"target: {data['target']}")
    print(f"cutoff: older than {data['cutoff']['older_than_days']} days "
          f"(before {data['cutoff']['before']})")
    print(f"candidates: {totals['candidates']} "
          f"({totals['candidate_bytes']} bytes)")
    print(f"protected: {totals['protected']}; skipped: {totals['skipped']}")
    print(f"footprints: {totals['footprints']} "
          f"({totals['footprint_bytes']} bytes)")
    for name, info in data["classes"].items():
        if info["candidates"] or info["protected"] or info["skipped"]:
            print(
                f"- {name}: {info['candidates']} candidates, "
                f"{info['protected']} protected, {info['skipped']} skipped, "
                f"{info['bytes']} bytes"
            )
    for name, info in data["footprint_classes"].items():
        if info["items"]:
            sample = info["samples"][0]
            print(
                f"- footprint {name}: {info['items']} items, "
                f"{info['count']} counted paths, {info['bytes']} bytes, "
                f"risk={sample['risk']}, "
                f"recommendation={sample['recommendation']}"
            )
    print("No files were changed. Use --json for full candidate paths and footprint details.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lingtai-agent",
        description="lingtai agent runtime",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Boot agent into sleep — wakes on external messages")
    run_parser.add_argument("working_dir", type=Path, help="Agent working directory containing init.json")
    run_parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level console logging (equivalent to LINGTAI_VERBOSE=1)",
    )

    sub.add_parser("check-caps", help="Output capability provider metadata as JSON")

    liveness_parser = sub.add_parser(
        "liveness",
        help="Emit the kernel-owned published-Agent liveness result as JSON",
    )
    liveness_parser.add_argument(
        "--agent-dir",
        type=Path,
        required=True,
        help="Agent working directory to inspect without booting it",
    )

    log_parser = sub.add_parser("log", help="Inspect the additive SQLite log index")
    log_sub = log_parser.add_subparsers(dest="log_command", required=True)

    log_rebuild = log_sub.add_parser("rebuild", help="Rebuild logs/log.sqlite from agent events, chat history, and daemon JSONL")
    log_rebuild.add_argument("agent_dir", type=Path, help="Agent working directory")

    log_doctor = log_sub.add_parser("doctor", help="Check logs/log.sqlite integrity and counts")
    log_doctor.add_argument("agent_dir", type=Path, help="Agent working directory")

    log_query = log_sub.add_parser("query", help="Run a read-only SQL query against logs/log.sqlite")
    log_query.add_argument("agent_dir", type=Path, help="Agent working directory")
    log_query.add_argument("sql", help="SQL query to execute")

    from lingtai.cli_daemon import add_daemon_parser
    add_daemon_parser(sub)
    from lingtai.cli_acp import add_acp_parser
    add_acp_parser(sub)
    from lingtai.cli_puffo_v0 import add_puffo_v0_parser
    add_puffo_v0_parser(sub)
    from lingtai.cli_project import add_project_parser
    add_project_parser(sub)

    maintenance_parser = sub.add_parser(
        "maintenance",
        help="Inspect kernel-owned maintenance surfaces",
    )
    maintenance_sub = maintenance_parser.add_subparsers(
        dest="maintenance_command",
        required=True,
    )
    cleanup = maintenance_sub.add_parser(
        "cleanup",
        help="Report stale retention candidates without deleting anything",
    )
    cleanup.add_argument(
        "target",
        type=Path,
        help="Agent working directory or direct .lingtai root",
    )
    cleanup.add_argument(
        "--older-than-days",
        type=_positive_int,
        default=30,
        help="Report candidates older than this many days (default: 30)",
    )
    cleanup.add_argument(
        "--include-archive",
        action="store_true",
        help="Include mailbox/archive entries as report candidates",
    )
    cleanup.add_argument(
        "--json",
        action="store_true",
        help="Emit stable JSON instead of a human summary",
    )

    args = parser.parse_args()

    if args.command == "run":
        working_dir = args.working_dir.resolve()
        if not working_dir.is_dir():
            print(f"error: {working_dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        if getattr(args, "verbose", False):
            os.environ["LINGTAI_VERBOSE"] = "1"
        run(working_dir)
    elif args.command == "check-caps":
        from lingtai.tools.registry import get_all_providers
        print(json.dumps(get_all_providers()))
    elif args.command == "liveness":
        agent_dir = args.agent_dir.resolve()
        if not agent_dir.is_dir():
            print(f"error: {agent_dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        from lingtai.kernel.session_stats import (
            query_published_agent_liveness,
            read_agent_record,
        )
        _emit_json(query_published_agent_liveness(
            read_agent_record(agent_dir),
            wall_now=time.time(),
        ))
    elif args.command == "log":
        _handle_log_command(args)
    elif args.command == "daemon":
        from lingtai.cli_daemon import handle_daemon_command

        handle_daemon_command(args)
    elif args.command == "acp":
        from lingtai.cli_acp import handle_acp_command

        handle_acp_command(args)
    elif args.command == "puffo-v0":
        from lingtai.cli_puffo_v0 import handle_puffo_v0_command

        handle_puffo_v0_command(args)
    elif args.command == "project":
        from lingtai.cli_project import handle_project_command

        handle_project_command(args)
    elif args.command == "maintenance":
        _handle_maintenance_command(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
