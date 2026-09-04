"""Composition root for ``lingtai-agent acp``."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO


def add_acp_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "acp",
        help="Serve one existing LingTai agent over local ACP v1 stdio",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--agent-dir",
        type=Path,
        help="Existing agent working directory containing init.json",
    )
    source.add_argument(
        "--runtime-id",
        help="Opaque operator-provisioned Puffo runtime id (requires a Puffo profile)",
    )
    parser.add_argument(
        "--profile",
        choices=("puffo-v0", "puffo-v1"),
        help="Constrained locally provisioned ACP launch profile",
    )


def _force_exit_after_incomplete_stop(agent, stop_result, stop_error) -> None:
    """Terminate when bounded Agent stop cannot report completed teardown."""

    stopped = bool(stop_result is not None and stop_result.stopped)
    if stop_error is None and stopped:
        return
    # A post-quiescence cleanup exception may already have released the lease.
    # Never append to the workdir after that release; a timed-out stop still owns
    # the lease and may record the terminal diagnostic safely.
    if getattr(agent, "_workdir_lease_acquired", True):
        try:
            agent._log(
                "acp_force_exit_after_incomplete_stop",
                stop_error=type(stop_error).__name__ if stop_error is not None else None,
                run_loop_alive=getattr(stop_result, "run_loop_alive", None),
                provider_worker_alive=getattr(stop_result, "provider_worker_alive", None),
            )
        except Exception:
            pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
    # A timed-out proof still owns heartbeat/lease; a post-proof cleanup error
    # may already have released them. In either case, immediate process termination
    # is the only boundary that guarantees this host performs no later state write.
    os._exit(70)


def run_acp(
    agent_dir: Path,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    fixed_execution_workspace=None,
    forced_disable: frozenset[str] | None = None,
    turn_origin_policy=None,
    requires_turn_origin_policy: bool = False,
    provider_call_admission_port=None,
    derived_launch_admission_port=None,
    requires_derived_launch_admission_port: bool = False,
    puffo_runtime=None,
    session_mcp_validator=None,
) -> None:
    """Compose one Agent and the local ACP stdio driving adapter.

    The original stdout object is captured as the wire before any Agent/config
    construction. Application ``print`` calls are then redirected to stderr for
    the complete server lifetime, leaving only serialized JSON-RPC on the wire.
    """

    wire_in = input_stream if input_stream is not None else sys.stdin
    wire_out = output_stream if output_stream is not None else sys.stdout
    if requires_turn_origin_policy and turn_origin_policy is None:
        raise ValueError("constrained ACP composition requires a turn-origin policy")
    if requires_derived_launch_admission_port and derived_launch_admission_port is None:
        raise ValueError(
            "constrained ACP composition requires a derived-launch admission port"
        )
    if input_stream is None:
        reconfigure_in = getattr(wire_in, "reconfigure", None)
        if callable(reconfigure_in):
            reconfigure_in(encoding="utf-8", errors="strict")
    if output_stream is None:
        reconfigure_out = getattr(wire_out, "reconfigure", None)
        if callable(reconfigure_out):
            reconfigure_out(
                encoding="utf-8",
                errors="strict",
                newline="\n",
                write_through=True,
            )
    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    agent = None
    try:
        # Lazy imports keep ordinary CLI commands free of ACP startup wiring and
        # make stdout quarantine precede every potentially noisy boot operation.
        from lingtai.adapters.acp import AcpStdioServer
        from lingtai.cli import (
            _check_duplicate_process,
            _clean_signal_files,
            _force_exit_if_worker_poisoned,
            build_agent,
            load_init,
        )
        from lingtai.kernel.logging import setup_logging
        from lingtai.venv_resolve import resolve_venv

        if puffo_runtime is not None:
            from lingtai.adapters.acp.puffo_v0 import resolve_runtime
            from lingtai.kernel.execution_workspace import ExecutionWorkspace

            verified_runtime = resolve_runtime(puffo_runtime.runtime_id)
            if verified_runtime != puffo_runtime:
                raise RuntimeError("puffo-v0 runtime binding changed before ACP startup")
            agent_dir = verified_runtime.agent_dir
            fixed_execution_workspace = ExecutionWorkspace(verified_runtime.workspace)
        _check_duplicate_process(agent_dir)
        _clean_signal_files(agent_dir)
        setup_logging(
            verbose=os.environ.get("LINGTAI_VERBOSE") == "1",
            log_dir=agent_dir / "logs",
        )
        data = load_init(agent_dir)
        venv_dir = resolve_venv(data)
        os.environ["LINGTAI_RUNTIME_PYTHON"] = sys.executable
        os.environ["LINGTAI_RUNTIME_VENV"] = str(venv_dir)
        data["venv_path"] = str(venv_dir)

        # A profile policy is meaningful even when it deliberately leaves the
        # operator-managed capability surface fully enabled.  Do not couple it
        # to the historical forced-disable mechanism.  Preserve the generic
        # no-private-options call shape for ordinary ACP composition.
        build_options = {}
        if forced_disable is not None:
            build_options["_forced_disable"] = forced_disable
        if turn_origin_policy is not None:
            build_options["_turn_origin_policy"] = turn_origin_policy
        if requires_turn_origin_policy:
            build_options["_requires_turn_origin_policy"] = True
        if provider_call_admission_port is not None:
            build_options["_provider_call_admission_port"] = provider_call_admission_port
        if derived_launch_admission_port is not None:
            build_options["_derived_launch_admission_port"] = derived_launch_admission_port
        if requires_derived_launch_admission_port:
            build_options["_requires_derived_launch_admission_port"] = True
        agent = build_agent(data, agent_dir, **build_options)
        agent._venv_path = str(venv_dir)
        agent.start()
        if (
            fixed_execution_workspace is None
            and turn_origin_policy is None
            and forced_disable is None
        ):
            server = AcpStdioServer(agent, wire_in, wire_out)
        else:
            server_options = {
                "fixed_execution_workspace": fixed_execution_workspace,
                "allow_session_mcp": (
                    fixed_execution_workspace is None
                    and not (forced_disable and "mcp" in forced_disable)
                ),
            }
            if session_mcp_validator is not None:
                server_options["session_mcp_validator"] = session_mcp_validator
            server = AcpStdioServer(agent, wire_in, wire_out, **server_options)
        try:
            server.serve()
        except (BrokenPipeError, OSError, UnicodeError, KeyboardInterrupt):
            # Local client disconnects and malformed stream encodings are clean
            # transport termination, not reasons to leak a Python traceback.
            server.close()
    finally:
        if agent is not None:
            stop_result = None
            stop_error = None
            try:
                stop_result = agent.stop(timeout=10.0)
            except BaseException as exc:
                stop_error = exc
            try:
                _force_exit_after_incomplete_stop(agent, stop_result, stop_error)
            except NameError:
                # Agent construction failed before helper imports completed.
                if stop_error is not None:
                    raise stop_error
            try:
                _force_exit_if_worker_poisoned(agent)
            except NameError:
                pass
        sys.stdout = original_stdout


def handle_acp_command(args) -> None:
    if args.profile is None:
        if args.runtime_id is not None:
            print("error: --runtime-id requires a Puffo ACP profile", file=sys.stderr)
            raise SystemExit(1)
        agent_dir = args.agent_dir.resolve()
        if not agent_dir.is_dir():
            print(f"error: {agent_dir} is not a directory", file=sys.stderr)
            raise SystemExit(1)
        run_acp(agent_dir)
        return

    if args.profile in {"puffo-v0", "puffo-v1"} and args.agent_dir is not None:
        print(
            f"error: {args.profile} does not accept --agent-dir; use --runtime-id",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if args.profile not in {"puffo-v0", "puffo-v1"} or args.runtime_id is None:
        print("error: Puffo ACP profiles require --runtime-id", file=sys.stderr)
        raise SystemExit(1)
    from lingtai.adapters.acp.puffo_v0 import (
        PuffoV0RegistryError,
        RUNTIME_POLICY,
        resolve_runtime,
    )
    from lingtai.adapters.acp.driver_authority import (
        DriverAuthorityClient,
        DriverDerivedLaunchAdmissionAdapter,
        authority_adapter_from_environment,
    )
    from lingtai.kernel.execution_workspace import ExecutionWorkspace

    session_mcp_validator = None
    if args.profile == "puffo-v1":
        from lingtai.adapters.acp.puffo_v1 import validate_puffo_v1_mcp_servers

        session_mcp_validator = validate_puffo_v1_mcp_servers

    try:
        runtime = resolve_runtime(args.runtime_id)
    except PuffoV0RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    authority = authority_adapter_from_environment()
    if args.profile == "puffo-v1" and not isinstance(authority, DriverAuthorityClient):
        print(
            "error: puffo-v1 requires an authenticated Puffo Driver authority",
            file=sys.stderr,
        )
        raise SystemExit(1)
    derived_launch_port = (
        DriverDerivedLaunchAdmissionAdapter(authority)
        if isinstance(authority, DriverAuthorityClient)
        else authority
    )
    run_acp(
        runtime.agent_dir,
        fixed_execution_workspace=ExecutionWorkspace(runtime.workspace),
        puffo_runtime=runtime,
        turn_origin_policy=RUNTIME_POLICY,
        requires_turn_origin_policy=True,
        provider_call_admission_port=authority,
        derived_launch_admission_port=derived_launch_port,
        requires_derived_launch_admission_port=True,
        session_mcp_validator=session_mcp_validator,
    )


__all__ = ["add_acp_parser", "handle_acp_command", "run_acp"]
