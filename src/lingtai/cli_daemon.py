"""Thin ``lingtai-agent daemon`` driver for :class:`DaemonService`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from lingtai.kernel.daemon_supervisor.manifest import redact_durable_value
from lingtai.services.daemon import DaemonService, DaemonServiceError

_MAX_TASKS_FILE_BYTES = 4 * 1024 * 1024
_EMANATE_KEYS = frozenset({"tasks", "backend", "max_turns", "timeout"})


class CliDaemonError(Exception):
    """A user-facing CLI refusal."""


def _strict_positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _state_root(raw: Path) -> Path:
    root = raw.expanduser().resolve()
    if not root.is_dir():
        raise CliDaemonError(f"state root is not a directory: {root}")
    return root


def _load_tasks_file(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CliDaemonError(f"cannot read tasks file {path}: {exc}") from exc
    if size > _MAX_TASKS_FILE_BYTES:
        raise CliDaemonError(
            f"tasks file exceeds {_MAX_TASKS_FILE_BYTES} bytes: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliDaemonError(f"invalid tasks file {path}: {exc}") from exc
    if isinstance(payload, list):
        return {"tasks": payload}
    if not isinstance(payload, dict):
        raise CliDaemonError("tasks file must contain an object or a bare task array")
    unknown = sorted(set(payload) - _EMANATE_KEYS)
    if unknown:
        raise CliDaemonError(f"unknown tasks payload fields: {', '.join(unknown)}")
    return payload


def _emit_json(value: Any) -> None:
    print(json.dumps(redact_durable_value(value), ensure_ascii=False, indent=2))


def _print_list_table(result: dict) -> None:
    rows = [
        (
            str(entry.get("id") or ""),
            str(entry.get("status") or ""),
            str(entry.get("backend") or "lingtai"),
            str(entry.get("started_at") or ""),
            str(entry.get("task") or "").replace("\n", " ")[:120],
        )
        for entry in result.get("emanations", [])
        if isinstance(entry, dict)
    ]
    headers = ("ID", "STATUS", "BACKEND", "STARTED", "TASK")
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip())
    print(f"\n{len(rows)} shown, {result.get('running', 0)} running")


def _handle_emanate(args: argparse.Namespace) -> int:
    payload = _load_tasks_file(args.tasks.expanduser().resolve())
    backend = args.backend if args.backend is not None else payload.get("backend", "lingtai")
    service = DaemonService(_state_root(args.state_root))
    result = service.emanate(
        payload.get("tasks", []),
        backend=backend,
        max_turns=payload.get("max_turns"),
        timeout=payload.get("timeout"),
    )
    _emit_json(result)
    return 0 if result.get("status") == "dispatched" else 1


def _handle_list(args: argparse.Namespace) -> int:
    result = DaemonService(_state_root(args.state_root)).list(
        status=args.status,
        last=args.last,
    )
    if result.get("status") == "error":
        _emit_json(result)
        return 1
    _print_list_table(result)
    return 0


def _handle_check(args: argparse.Namespace) -> int:
    result = DaemonService(_state_root(args.state_root)).check(args.id)
    _emit_json(result)
    return 0 if result.get("status") != "error" else 1


def _handle_reclaim(args: argparse.Namespace) -> int:
    result = DaemonService(_state_root(args.state_root)).reclaim()
    _emit_json(result)
    return 0 if result.get("status") == "reclaimed" else 1


_HANDLERS = {
    "emanate": _handle_emanate,
    "list": _handle_list,
    "check": _handle_check,
    "reclaim": _handle_reclaim,
}


def _add_state_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help="Standalone daemon state directory",
    )


def add_daemon_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the standalone daemon command tree on the root parser."""
    parser = sub.add_parser(
        "daemon",
        help="Dispatch and inspect standalone daemon runs",
    )
    commands = parser.add_subparsers(dest="daemon_command", required=True)

    emanate = commands.add_parser("emanate", help="Dispatch a daemon task batch")
    _add_state_root(emanate)
    emanate.add_argument(
        "--tasks",
        type=Path,
        required=True,
        help="Daemon emanate input JSON, or a bare array of task objects",
    )
    emanate.add_argument(
        "--backend",
        default=None,
        help="Override execution backend (default: payload, else lingtai)",
    )

    listing = commands.add_parser("list", help="Show daemon run status (read-only)")
    _add_state_root(listing)
    listing.add_argument("--status", default="all", help="Status filter (default: all)")
    listing.add_argument(
        "--last", type=_strict_positive_int, default=None, metavar="N",
        help="Show the newest N rows",
    )

    check = commands.add_parser("check", help="Inspect one daemon run (read-only)")
    _add_state_root(check)
    check.add_argument("id", help="Daemon id or exact run id")

    reclaim = commands.add_parser("reclaim", help="Cancel active runs in this state root")
    _add_state_root(reclaim)


def handle_daemon_command(args: argparse.Namespace) -> None:
    """Run one daemon subcommand and exit nonzero on refusal."""
    handler = _HANDLERS.get(getattr(args, "daemon_command", None))
    if handler is None:
        print("error: missing daemon subcommand", file=sys.stderr)
        raise SystemExit(1)
    try:
        code = handler(args)
    except (CliDaemonError, DaemonServiceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if code:
        raise SystemExit(code)


__all__ = ["add_daemon_parser", "handle_daemon_command", "CliDaemonError"]
