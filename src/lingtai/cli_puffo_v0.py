"""Local operator control plane for the constrained Puffo ACP profile."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lingtai.adapters.acp.puffo_v0 import (
    PuffoV0RegistryError,
    discover_runtimes,
    provision_runtime,
    revoke_runtime,
)


def add_puffo_v0_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        "puffo-v0",
        help="Provision or revoke locally managed Puffo ACP runtimes",
    )
    commands = parser.add_subparsers(dest="puffo_v0_command", required=True)
    provision = commands.add_parser(
        "provision",
        help="Bind one existing persistent agent identity to an opaque runtime id",
    )
    provision.add_argument("--runtime-id", required=True)
    provision.add_argument("--agent-dir", type=Path, required=True)
    provision.add_argument("--workspace", type=Path, required=True)
    provision.add_argument("--json", action="store_true", dest="as_json")
    revoke = commands.add_parser(
        "revoke",
        help="Prevent future puffo-v0 ACP spawns for one runtime id",
    )
    revoke.add_argument("--runtime-id", required=True)
    revoke.add_argument("--json", action="store_true", dest="as_json")
    discover = commands.add_parser(
        "discover",
        help="List initialized agents below one user-selected directory",
    )
    discover.add_argument("--root", type=Path, required=True)
    discover.add_argument("--json", action="store_true", dest="as_json")


def handle_puffo_v0_command(args: argparse.Namespace) -> None:
    try:
        if args.puffo_v0_command == "provision":
            runtime = provision_runtime(args.runtime_id, args.agent_dir, args.workspace)
            payload = {
                "status": "provisioned",
                "runtime_id": runtime.runtime_id,
                "agent_dir": str(runtime.agent_dir),
                "workspace": str(runtime.workspace),
                "entry_digest": runtime.entry_digest,
            }
        elif args.puffo_v0_command == "revoke":
            revoke_runtime(args.runtime_id)
            payload = {"status": "revoked", "runtime_id": args.runtime_id}
        elif args.puffo_v0_command == "discover":
            candidates = discover_runtimes(args.root)
            payload = {
                "runtimes": [
                    {
                        "agent_dir": str(candidate.agent_dir),
                        "display_name": candidate.display_name,
                        "runtime_id": candidate.runtime_id,
                        # Status comes straight from the classifier so a third
                        # state (drifted / integrity-failed / ...) can never be
                        # flattened into the bound/available pair a caller acts on.
                        "status": candidate.state.value,
                        "workspace": (
                            str(candidate.workspace)
                            if candidate.workspace is not None
                            else None
                        ),
                        # Advisory reuse sign (option C): present on an `available`
                        # directory whose exact path a prior runtime recorded but no
                        # longer holds. null otherwise. Does not change `status`.
                        "formerly_bound_runtime_id": candidate.formerly_bound_runtime_id,
                    }
                    for candidate in candidates
                ]
            }
        else:
            raise SystemExit(2)
    except PuffoV0RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif args.puffo_v0_command == "discover":
        for candidate in payload["runtimes"]:
            runtime_id = candidate["runtime_id"]
            suffix = f" {runtime_id}" if runtime_id else ""
            formerly = candidate["formerly_bound_runtime_id"]
            note = (
                f" [path previously bound {formerly}; its identity has moved or is gone]"
                if formerly
                else ""
            )
            print(f"{candidate['agent_dir']} ({candidate['status']}{suffix}){note}")
    else:
        print(f"puffo-v0 runtime {payload['runtime_id']} {payload['status']}.")


__all__ = ["add_puffo_v0_parser", "handle_puffo_v0_command"]
