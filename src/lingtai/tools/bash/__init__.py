"""Canonical shell capability — shell command execution with file-based policy.

Adds the ability to run shell commands. This is a capability (not intrinsic)
because not every agent should have shell access — it's a powerful
capability that should be explicitly opted into.

Usage:
    agent.add_capability("shell", policy_file="path/to/policy.json")
    agent.add_capability("shell", yolo=True)  # no restrictions
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ._shell_dialect import (
    ShellDialect,
    ShellInvocation,
    ShellKind,
    _POSIX_UNSUPPORTED,
    extract_posix_commands,
)

from ._async_supervisor import (
    load_state,
    publish_reminder_if_claimed,
    update_state,
    write_initial_state,
)
from ._async_process import (
    BashAsyncProcessPort,
    ProcessRef,
    process_ref_from_state,
)
# The package's one and only public schema/description pair is the migrated
# action-separated family surface, re-exported here under the canonical
# duck-typed names (the same single-surface shape ``web`` has). There is no
# second, flat, pre-migration pair to drift against.
from ._tool_family import (
    DECLARATION,
    TIMEOUT_MAX_ENV,
    ShellFamilyDispatcher,
    _DEFAULT_TIMEOUT_SECONDS,
    get_description,
    get_schema,
    resolve_timeout_max_seconds,
)

# Output hygiene (ANSI/CSI stripping, C0/C1 control escaping, startup-noise
# stripping, explicit truncation) applied at the tool boundary before results
# are returned to the model.
from ._output_hygiene import sanitize_output


if TYPE_CHECKING:
    from lingtai.kernel.base_agent import BaseAgent

PROVIDERS = {"providers": [], "default": "builtin"}

_DEFAULT_POLICY_FILE = Path(__file__).parent / "bash_policy.json"
_POWERSHELL_POLICY_FILE = Path(__file__).parent / "powershell_policy.json"
_DEFAULT_ASYNC_REMINDER_SECONDS = 1800.0
_AGENT_ASYNC_HANDOFF = (
    "While waiting, go idle or call system(action='sleep'); the terminal result "
    "will arrive and wake you as a notification; read shell-manual and "
    "notification-manual for details. If Telegram is connected and a Task Card "
    "is available for the current turn, use it to report progress; call "
    "`telegram(action='manual')` and follow its `Programmable Task Card` "
    "section for details."
)
_DETACHED_DAEMON_ASYNC_HANDOFF = (
    "While this daemon is still running, Shell reminder and completion events "
    "are delivered only into this same daemon at a safe provider send boundary. "
    "They are not parent notifications and may not arrive after daemon completion; "
    "call shell.poll for exact output."
)
# Detached daemon queue admission can temporarily fail at its bounded RunDir
# capacity. Retry only that private composition path; normal Agent publications
# retain their established one-shot notification behavior.
_DETACHED_PUBLICATION_RETRY_INITIAL_SECONDS = 0.05
_DETACHED_PUBLICATION_RETRY_MAX_SECONDS = 1.0


class _DetachedDaemonShellBinding:
    """Private setup token for the sole detached-daemon Shell composition path."""

    __slots__ = ("jobs_dir",)

    def __init__(self, jobs_dir: Path) -> None:
        self.jobs_dir = jobs_dir
_SUPERVISOR_START_LEASE_SECONDS = 3.0
# The parent may spend one start lease launching the supervisor and another
# waiting for its durable PID before it can atomically arm the user-visible
# reminder from the successful-return boundary.  During that bounded handoff,
# another manager must not publish the earlier crash-fallback deadline.
_RETURN_HANDOFF_LEASE_SECONDS = _SUPERVISOR_START_LEASE_SECONDS * 2
_RETURN_HANDOFF_RECHECK_SECONDS = 0.05
_SUPERVISOR_COMMIT_GRACE_SECONDS = 0.25
# The supervisor's bounded cancel-commit work must fit inside this window: the
# Job-Object active-process confirmation can take its full 5s, the
# identity-gated taskkill fallback sweep can take its full 10s
# (``_TASKKILL_TIMEOUT_SECONDS``), and the bounded root reap adds up to 2s --
# ~17s worst case on the escaped-child branch (the job-kill-failure branch
# skips the 5s wait).  A tighter 3s window flaked the native Windows cancel
# contract under runner load (manager gave up with "awaiting supervisor
# terminal commit" while the supervisor was still committing); 20s keeps the
# full worst case inside the window with margin.
_CANCEL_COMMIT_TIMEOUT_SECONDS = 20.0
# Windows sync runs bound the post-kill pipe drain (Codex ``io_drain_timeout``,
# Goose PR #7689): a grandchild that inherited the stdout/stderr pipe write
# ends and survived the kill must not block the caller on EOF forever.
_IO_DRAIN_TIMEOUT_SECONDS = 0.5
_JOB_ID_RE = re.compile(r"job-(?:[0-9a-f]{32}|[0-9a-f]{8})\Z")


def _working_dir_contained(resolved: str, sandbox: str) -> bool:
    """Return whether resolved-path *resolved* is the sandbox or nested under it.

    Both arguments are already-resolved absolute-path strings. The boundary
    separator is the live platform ``os.sep``, read at call time: POSIX
    ``resolve()`` yields ``/``-joined paths, Windows ``resolve()`` yields
    ``\\``-joined paths, so a hardcoded ``"/"`` would reject every legitimate
    nested Windows working_dir. Appending the separator before the prefix test
    keeps sibling-prefix directories (``/a/bb`` under sandbox ``/a/b``) out.
    """
    return resolved == sandbox or resolved.startswith(sandbox + os.sep)


def _select_shell_dialect(kind: ShellKind | None = None) -> ShellDialect:
    """Load the canonical outer selector lazily to keep imports acyclic."""
    from lingtai.adapters.shell import select_shell_dialect

    return select_shell_dialect(shell_kind=kind)


def _resolve_shell_kind(kind: ShellKind | None = None) -> ShellKind:
    """Resolve the classifier result, honoring an explicit kind override."""
    if kind is not None:
        return kind
    from lingtai.adapters.shell import resolve_shell_kind

    return resolve_shell_kind()


def _describe_host_os() -> str:
    """Load setup-time host metadata from the outer composition layer."""
    from lingtai.adapters.shell import describe_host_os

    return describe_host_os()


def _select_shell_async_process() -> BashAsyncProcessPort:
    """Load the canonical process selector lazily."""
    from lingtai.adapters.shell_process import select_shell_async_process
    return select_shell_async_process()


def _sync_run_contained() -> bool:
    """Return whether the sync run path uses Windows Job Object containment."""
    return os.name == "nt"


# Retained private names keep old implementation-only callers readable during
# the PR1 rollout; they do not create another registered tool.
_select_bash_shell_dialect = _select_shell_dialect
_select_bash_async_process = _select_shell_async_process

# Length of the stderr tail surfaced in the failure warning. Short on purpose:
# the full stderr is already present in the result; the tail just makes the
# failure impossible to miss when an agent skims the top-level fields.
_WARNING_STDERR_TAIL = 600


def _redact_warning_tail(text: str) -> str:
    """Best-effort secret redaction for the stderr tail copied into ``warning``.

    The raw ``stderr``/``stdout`` fields already mirror the command output
    verbatim; this only touches the bounded tail that gets hoisted into the
    top-level ``warning`` string, where a secret-shaped error line would be made
    *more* prominent. Routes through the kernel's mechanical
    ``trace_redaction.redact_text`` so the warning surface gets the same
    high-confidence token/key redaction as durable trajectory writes.

    Fail-open: if the redactor cannot be imported or raises (it must never break
    a bash result), the original tail is returned unchanged — the raw stderr is
    already present in the result, so this introduces no new exposure beyond it.
    """
    try:
        from lingtai.kernel.trace_redaction import redact_text

        return redact_text(text)
    except Exception:
        return text

# Substrings that signal a "successful shell, failed program" — the failure the
# fidelity warning exists to surface. A Python traceback or a missing-module
# error commonly exits nonzero, but agents have been observed proceeding on the
# false success because the top-level status said "ok".
_FAILURE_SIGNATURES = (
    "Traceback (most recent call last)",
    "ModuleNotFoundError",
    "No module named",
)


def _detect_failure_signature(stdout: str, stderr: str) -> str | None:
    """Return a short label if stdout/stderr carries a known failure signature.

    Detection is best-effort and advisory only — it never changes ``exit_code``
    or whether the command is considered failed; that is driven solely by the
    exit code. It only enriches the human-/model-visible ``warning`` text so a
    Python traceback or missing-import under a zero/nonzero exit is named
    explicitly instead of being buried in the output.
    """
    haystack = f"{stderr}\n{stdout}"
    # Prefer the most specific, most actionable label. A missing-module error
    # also emits a full traceback, so check for it before the generic one.
    if _FAILURE_SIGNATURES[1] in haystack or _FAILURE_SIGNATURES[2] in haystack:
        return "missing_module"
    if _FAILURE_SIGNATURES[0] in haystack:
        return "python_traceback"
    return None


# Command shapes that frequently time out via unbounded recursive directory
# walks over large roots (work/projects/.lingtai). Matched only to *append a
# hint* on timeout — never to block or alter the command.
_BROAD_SCAN_RE = re.compile(
    r"""
    \bfind\s+[^|]*\s-(?:name|path|type|iname)\b   # find ... -name/-path/-type
    | \brglob\s*\(                                  # Path.rglob(
    | \bos\.walk\s*\(                               # os.walk(
    | \bglob\s*\(\s*['"][^'"]*\*\*                  # glob('**/...')
    """,
    re.VERBOSE,
)

_BROAD_SCAN_HINT = (
    "This looks like a broad recursive scan, the most common cause of bash "
    "timeouts. Prefer `rg --files --hidden -g '!**/{.git,node_modules,daemons,"
    ".worktrees}/**' <root>` (then filter), narrow the root, or raise `timeout` "
    "for a genuinely large tree."
)

# Steering guidance appended to a timeout error when the command produced no
# output before it was killed (OpenClaw exec-runner.ts ``no-output-timeout`` /
# overall-timeout copy, adapted to our ``async`` parameter name; Hermes
# foreground-hint guidance).  The point is to steer the model away from both
# failure modes that this class of timeout represents: a foreground command that
# should have been launched with ``async=true``, and shell backgrounding with a
# trailing ``&``, which detaches the child from any supervision/collectable
# output stream.
_BACKGROUND_GUIDANCE = (
    "Long-running or no-output work should be launched with async=true "
    "(or as a daemon) rather than as a foreground command; do not rely on "
    "shell backgrounding with a trailing &."
)


def _broad_scan_hint(command: str) -> str | None:
    """Return a broad-scan recipe hint if the command resembles a recursive walk.

    Best-effort heuristic used only to enrich a timeout message. False positives
    are harmless (an extra sentence); it never blocks or rewrites the command.
    """
    return _BROAD_SCAN_HINT if _BROAD_SCAN_RE.search(command) else None


def _timeout_error(command: str, timeout: float, no_output: bool = False) -> dict:
    """Build the timeout result shape; shared by every sync path.

    ``no_output`` is retained for signature compatibility only (OpenClaw
    ``no-output-timeout`` heritage); the message is identical for both values.
    Jason 2026-08-10 tool-timeout redesign: the async steering guidance is
    appended on *every* timeout result, so the model always sees the async
    boundary instead of repeatedly raising a sync timeout.
    """
    msg = f"Command timed out after {timeout}s"
    hint = _broad_scan_hint(command)
    if hint:
        msg = f"{msg}. {hint}"
    # ``hint`` (when present) already ends with a period, so a plain space
    # keeps a clean sentence boundary in both shapes.
    msg = f"{msg}{' ' if hint else '. '}{_BACKGROUND_GUIDANCE}"
    return {"status": "error", "message": msg}


# =============================================================================
# Exit-code interpretation (benign non-zero codes)
# =============================================================================
# Many Unix commands use non-zero exit codes for informational purposes, not
# failure.  The model sees a raw exit_code=1 from `grep` and wastes a turn
# investigating something that just means "no matches".  Port of Hermes'
# ``_interpret_exit_code``: when the *last* pipeline/chain segment is one of
# these commands and its exit code is a known benign code, a human-readable
# note is appended to the result so the agent can move on.  The exit_code
# field itself is never rewritten — this is a presentation/guidance layer
# only (the native-exit wrapper keeps $LASTEXITCODE fidelity).
#
# ``last segment`` means the last *top-level* segment: a ``|``/``;`` inside a
# ``$(...)`` or backtick command substitution is not a chain operator — only
# the substitution's result participates in the outer pipeline.  The splitter
# therefore treats substitution bodies as opaque, so a pipe feeding grep
# inside a substitution can never be mistaken for the command whose exit
# code we are annotating (e.g. ``pytest $(git diff --name-only | grep test_)``
# must never get a "no matches" note on a real pytest failure).
_BENIGN_EXIT_NOTES: dict[str, dict[int, str]] = {
    # grep/rg/ag/ack: 1 = no matches found (normal), 2+ = real error
    "grep":   {1: "No matches found (not an error)"},
    "egrep":  {1: "No matches found (not an error)"},
    "fgrep":  {1: "No matches found (not an error)"},
    "rg":     {1: "No matches found (not an error)"},
    "ag":     {1: "No matches found (not an error)"},
    "ack":    {1: "No matches found (not an error)"},
}


def _split_on_chain_operators(command: str) -> list[str]:
    """Split a shell command on chain/pipeline operators outside quotes.

    Splits on ``;``, ``&&``, ``||`` and ``|`` while respecting single quotes,
    double quotes and backslash escapes, so ``echo 'a | b'`` stays one
    segment. Command substitutions (``$(...)`` and backticks) are opaque:
    operators inside them are *not* top-level chain operators — only the
    substitution's result participates in the outer pipeline, so
    ``pytest $(git diff --name-only | grep test_)`` stays a single segment
    whose command is ``pytest`` (a ``|`` inside ``$(...)`` must never make
    ``grep`` look like the last command). ``|&`` (stdout+stderr pipe) is a
    two-character operator like ``||``. A bare ``&`` (backgrounding) is not a
    chain operator and stays inside its segment. Deliberately simple: used
    only to pick the last segment whose exit status the shell reports —
    never to execute anything.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    paren_subst = 0  # nesting depth of $( ... ) command substitutions
    backtick_subst = False  # inside ` ... ` command substitution (no nesting)
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if escaped:
            current.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            current.append(ch)
            escaped = True
            i += 1
            continue
        if quote is not None:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "`":
            backtick_subst = not backtick_subst
            current.append(ch)
            i += 1
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            paren_subst += 1
            current.append(ch)
            i += 1
            continue
        if paren_subst > 0 or backtick_subst:
            current.append(ch)
            if ch == ")" and paren_subst > 0:
                paren_subst -= 1
            i += 1
            continue
        if ch in (";", "|", "&"):
            if ch == "&" and (i + 1 >= n or command[i + 1] != "&"):
                # Bare `&` backgrounds the command; keep it in the segment so
                # the segment's first word still names the command.
                current.append(ch)
                i += 1
                continue
            if ch == "&":
                operator = "&&"
            elif ch == "|" and i + 1 < n and command[i + 1] in ("|", "&"):
                operator = command[i : i + 2]  # ``||`` or ``|&``
            else:
                operator = ch
            segments.append("".join(current))
            current = []
            i += len(operator)
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def interpret_exit_code(command: str, exit_code: int) -> tuple[int, str | None]:
    """Return ``(exit_code, note)`` when a non-zero exit is benign.

    Inspects only the *last* pipeline/compound segment (split on
    ``;``/``&&``/``||``/``|`` respecting quotes; ``$(...)``/backtick
    substitution bodies are opaque and never split) — that is the command
    whose exit status the shell reports. Maps known benign non-zero codes
    (``grep``/``egrep``/``fgrep``/``rg``/``ag``/``ack`` exit 1 = "No matches")
    to a guidance note; returns the original code with ``None`` otherwise.
    Never changes ``exit_code`` — presentation/guidance only.  Caveat: under
    ``set -o pipefail`` the reported exit may come from an earlier pipeline
    stage, in which case a "no matches" note can misattribute the failure;
    the note is guidance text, not a rewrite, so the raw exit code stays
    visible.
    """
    if exit_code == 0:
        return exit_code, None
    segments = _split_on_chain_operators(command)
    last_segment = (segments[-1] if segments else command).strip()
    if not last_segment:
        return exit_code, None
    # Base command name = first word of the last segment, skipping env-var
    # assignments (``VAR=val cmd``) and stripping path prefixes
    # (``/usr/bin/grep`` -> ``grep``).
    base_cmd = ""
    for word in last_segment.split():
        if "=" in word and not word.startswith("-"):
            continue
        base_cmd = word.split("/")[-1]
        break
    return exit_code, _BENIGN_EXIT_NOTES.get(base_cmd, {}).get(exit_code)


def _augment_command_result(result: dict, command: str | None = None) -> dict:
    """Add explicit pass/fail fidelity fields to a completed-command result.

    The top-level ``status`` of a bash result reflects only that the shell
    *spawned* — it stays ``ok``/``done`` even when the inner command failed.
    Agents have repeatedly missed inner failures because of this. To make a
    failure impossible to skim past *without* changing the ``status`` contract
    (which downstream recovery/telemetry branches on), this adds three additive,
    model-visible fields keyed off ``exit_code``:

    - ``ok`` (bool): ``True`` only when ``exit_code == 0``.
    - ``command_status`` (str): ``"success"`` or ``"failed"``.
    - ``warning`` (str, on failure *or* a suspicious zero-exit): one-line summary
      naming the nonzero exit, any detected traceback/missing-module signature,
      and a stderr tail. The tail is routed through the kernel redactor so a
      secret-shaped error line is not made more prominent than it already is in
      the raw ``stderr`` field.

    ``status`` itself is intentionally left untouched so existing callers and
    tests that branch on it keep working. The raw ``stderr``/``stdout`` fields
    are mirrored verbatim and never altered here.
    """
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int):
        return result
    failed = exit_code != 0
    result["ok"] = not failed
    result["command_status"] = "failed" if failed else "success"

    signature = _detect_failure_signature(
        result.get("stdout", "") or "", result.get("stderr", "") or ""
    )
    # Benign non-zero exit (e.g. grep/rg no-match) → guidance note, only when
    # the originating command is known. Presentation-only: exit_code is kept.
    exit_note: str | None = None
    if command:
        _, exit_note = interpret_exit_code(command, exit_code)
    if not failed and signature is None and exit_note is None:
        return result

    parts: list[str] = []
    if failed:
        parts.append(f"command exited with code {exit_code}")
    else:
        # Zero exit but a traceback/missing-module signature is present — the
        # command may have swallowed the error. Flag it without claiming failure.
        parts.append(f"command exited 0 but output contains a {signature}")
    if failed and signature is not None:
        parts.append(f"detected {signature}")
    if exit_note is not None:
        parts.append(f"note: {exit_note}")
    stderr = (result.get("stderr") or "").strip()
    if stderr:
        tail = stderr[-_WARNING_STDERR_TAIL:]
        if len(stderr) > _WARNING_STDERR_TAIL:
            tail = "…" + tail
        # Redact the hoisted tail only — the raw stderr field is left verbatim.
        parts.append(f"stderr tail: {_redact_warning_tail(tail)}")
    result["warning"] = "; ".join(parts)
    return result



class ShellPolicy:
    """Command execution policy — allow/deny lists with pipe awareness.

    Two modes, determined by the policy file content:
    - **Denylist mode** (only ``deny`` key): everything allowed except denied commands.
    - **Allowlist mode** (``allow`` key present): only listed commands allowed,
      everything else blocked. ``deny`` key is ignored in this mode.

    The mode is implicit — if ``allow`` is present, it's allowlist mode.
    """

    def __init__(self, allow: list[str] | None = None, deny: list[str] | None = None):
        self._allow = set(allow) if allow else None
        # deny is only used in denylist mode (when allow is absent)
        self._deny = set(deny) if deny and not allow else None

    @classmethod
    def from_file(cls, path: str) -> "ShellPolicy":
        """Load policy from a JSON file with allow/deny lists."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Policy file not found: {path}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(allow=data.get("allow"), deny=data.get("deny"))

    @classmethod
    def yolo(cls) -> "ShellPolicy":
        """Create a policy that allows everything."""
        return cls()

    def describe(self) -> str:
        """Return a human-readable summary of the policy rules."""
        if self._allow is None and self._deny is None:
            return ""
        if self._allow is not None:
            return (
                f"ALLOWLIST MODE: Only these commands are permitted (all others blocked): "
                f"{', '.join(sorted(self._allow))}"
            )
        return (
            f"DENYLIST MODE: All commands are allowed except: "
            f"{', '.join(sorted(self._deny))}"
        )

    def is_allowed(self, command: str) -> bool:
        """Check if a command string is allowed by this policy.

        Parses pipes, chains, and subshells to check every command.
        """
        if self._allow is None and self._deny is None:
            return True
        commands = self._extract_commands(command)
        # The extractor emits this sentinel when static policy cannot prove
        # command safety. A configured allow/deny policy must reject it rather
        # than treating the sentinel as an ordinary, non-denied command.
        if _POSIX_UNSUPPORTED in commands:
            return False
        return all(self._check_single(cmd) for cmd in commands)

    def _check_single(self, cmd: str, *, case_insensitive: bool = False) -> bool:
        """Check one command name against policy.

        PowerShell command names are case-insensitive; POSIX retains its
        historical case-sensitive matching.  The manager supplies the dialect
        fact rather than making this policy object inspect the host.
        """
        if case_insensitive:
            cmd = cmd.casefold()
            allow = {item.casefold() for item in self._allow} if self._allow is not None else None
            deny = {item.casefold() for item in self._deny} if self._deny is not None else None
        else:
            allow, deny = self._allow, self._deny
        if allow is not None:
            return cmd in allow
        if deny is not None:
            return cmd not in deny
        return True

    @staticmethod
    def _extract_commands(command: str) -> list[str]:
        """Extract all command names from a potentially chained command string.

        Handles: |, &&, ||, ;, newlines, $(), backticks, env-var prefixes.
        Returns the first actual command word of each sub-command.
        """
        return list(extract_posix_commands(command))


class ShellManager:
    """Manages shell commands; async terminal truth belongs to a durable child."""

    def __init__(
        self,
        policy: ShellPolicy,
        working_dir: str,
        agent: "BaseAgent | object | None" = None,
        max_output: int = 50_000,
        dialect: ShellDialect | None = None,
        async_process: BashAsyncProcessPort | None = None,
        shell_kind: "ShellKind | str | None" = None,
        notification_port: object | None = None,
        async_handoff: str | None = None,
        async_jobs_dir: Path | None = None,
        retry_failed_publications: bool = False,
        rehydrate: bool = True,
    ):
        self._policy = policy
        self._working_dir = working_dir
        self._max_output = max_output
        # Direct-manager callers retain the historical Agent-shaped injection
        # for compatibility.  The official plugin path leaves this unset and
        # receives only ``notification_port`` from its granted host facade.
        self._agent = agent
        self._notifications = notification_port
        # Ordinary Agent composition keeps the established notification handoff.
        # Detached daemon composition selects its distinct, run-local wording
        # through the same immutable Shell configuration snapshot.
        self._async_handoff = (
            async_handoff if isinstance(async_handoff, str) and async_handoff.strip()
            else _AGENT_ASYNC_HANDOFF
        )
        self._dialect = dialect or _select_shell_dialect()
        # Runtime shell-family metadata: classifier override when provided,
        # otherwise derived from the dialect.  Unknown dialects (test mocks)
        # fall back to the POSIX kind for metadata purposes only.
        self._shell_kind = ShellKind.coerce(shell_kind) or self._dialect.kind() or ShellKind.POSIX
        self._async_process = async_process or _select_shell_async_process()
        # Detached daemon composition supplies its own run-private job namespace;
        # command cwd intentionally remains ``_working_dir`` (the granted task
        # workdir). Ordinary Shell preserves its historical <workdir>/system/jobs.
        self._jobs_dir: Path | None = async_jobs_dir
        self._retry_failed_publications = retry_failed_publications
        self._reminder_lock = threading.Lock()
        self._reminder_cancel_events: dict[str, threading.Event] = {}
        self._reminder_retry_delays: dict[str, float] = {}
        self._completion_lock = threading.Lock()
        self._completion_watchers: set[str] = set()
        if rehydrate:
            self._rehydrate_async_jobs()

    def activate(self) -> None:
        """Resume durable async reminders/completion watches after plugin bind.

        Direct manager construction remains eager for compatibility; the
        declared host-plugin path calls this only as the registrar's separate
        post-name-check activation step, so binding itself neither starts a
        watcher nor reaches a live Agent.
        """
        self._rehydrate_async_jobs()

    @property
    def shell_kind(self) -> ShellKind:
        """Runtime shell-family metadata (drives model-facing description)."""
        return self._shell_kind

    def _jobs_path(self) -> Path:
        return self._jobs_dir or Path(self._working_dir) / "system" / "jobs"

    def _ensure_jobs_dir(self) -> Path:
        """Create and return the jobs directory (only for an async run)."""
        if self._jobs_dir is None:
            self._jobs_dir = Path(self._working_dir) / "system" / "jobs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        return self._jobs_dir

    def _validate_working_dir(self, cwd: str) -> dict | None:
        """Validate cwd is under the agent sandbox. Returns error dict or None."""
        try:
            from lingtai.kernel.execution_workspace import (
                current_execution_workspace,
                resolve_execution_path,
            )
            workspace = current_execution_workspace()
            if workspace is None:
                resolved = str(Path(cwd).resolve())
                sandbox = str(Path(self._working_dir).resolve())
            else:
                resolved = str(resolve_execution_path(cwd, fallback_root=workspace.root))
                sandbox = str(workspace.root)
            if not _working_dir_contained(resolved, sandbox):
                if workspace is not None:
                    return {
                        "status": "error",
                        "message": f"working_dir must stay within execution workspace: {sandbox}",
                    }
                return {
                    "status": "error",
                    "message": (
                        f"working_dir must be under agent working directory: "
                        f"{self._working_dir}. To operate on an external path, "
                        f"use an allowed working_dir and put `cd {resolved} && ...` "
                        f"inside the command."
                    ),
                }
        except (ValueError, OSError):
            return {"status": "error", "message": "Invalid working_dir path"}
        return None

    def _validate_command(self, command: str) -> dict | None:
        """Validate command is non-empty and allowed by policy. Returns error dict or None."""
        if not command.strip():
            return {"status": "error", "message": "command is required"}
        try:
            commands = self._dialect.extract_commands(command)
        except (NotImplementedError, ValueError) as exc:
            return {"status": "error", "message": f"Shell dialect cannot validate command safely: {exc}"}
        state_key = self._dialect.state_key()
        # PowerShell and cmd.exe command names are case-insensitive; POSIX
        # retains its historical case-sensitive matching. The manager supplies
        # the dialect fact rather than making this policy object inspect the host.
        case_insensitive = state_key in {"powershell", "cmd"}
        powershell = state_key == "powershell"
        cmd = state_key == "cmd"
        posix = state_key == "posix"
        # PowerShell and cmd.exe both fail closed on syntax the static
        # extractor cannot prove (dynamic invocation, ``%`` expansion): the
        # refusal marker is only enforced when a policy is actually
        # configured -- yolo mode has nothing to protect.
        unsupported = (
            (powershell and "__powershell_unsupported__" in commands)
            or (cmd and "__cmd_unsupported__" in commands)
            or (posix and "__posix_unsupported__" in commands)
        )
        if unsupported and (
            self._policy._allow is not None or self._policy._deny is not None
        ):
            return {
                "status": "error",
                "message": (
                    "cmd.exe policy validation does not support this syntax; "
                    "refusing to run it"
                    if cmd
                    else (
                        "POSIX policy validation does not support this syntax; refusing "
                        "to run it. The parser could not statically extract all commands; "
                        "simplify the command or use yolo=true only when bypassing policy "
                        "is intentional."
                        if posix
                        else (
                            "PowerShell policy validation does not support this syntax; refusing "
                            "to run it. The parser could not statically extract all commands "
                            "(likely variable-based invocation, here-strings, or complex "
                            "expressions). Options: (1) simplify the script to use only literal "
                            "command names, (2) run with yolo=true to bypass policy, or "
                            "(3) split into multiple simpler commands."
                        )
                    )
                ),
            }
        if not all(self._policy._check_single(cmd, case_insensitive=case_insensitive) for cmd in commands):
            denied = commands
            return {
                "status": "error",
                "message": f"Command not allowed by policy. "
                f"Denied command(s): {', '.join(denied)}",
            }
        return None

    @staticmethod
    def _validate_job_id(job_id: str) -> dict | None:
        """Accept only retained full UUID IDs and the old eight-hex legacy form."""
        if not isinstance(job_id, str) or not job_id:
            return {"status": "error", "message": "job_id is required"}
        if _JOB_ID_RE.fullmatch(job_id) is None:
            return {"status": "error", "message": f"Invalid job_id: {job_id}"}
        return None

    @staticmethod
    def _validate_reminder(value) -> tuple[float | None, dict | None]:
        """Validate async reminder delay, defaulting omitted values for runtime compatibility."""
        if value is None:
            return _DEFAULT_ASYNC_REMINDER_SECONDS, None
        if isinstance(value, bool):
            return None, {"status": "error", "message": "reminder must be a finite non-negative number of seconds"}
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return None, {"status": "error", "message": "reminder must be a finite non-negative number of seconds"}
        if delay < 0 or not math.isfinite(delay) or delay > threading.TIMEOUT_MAX:
            return None, {
                "status": "error",
                "message": (
                    "reminder must be a finite non-negative number of seconds "
                    f"not greater than {threading.TIMEOUT_MAX}"
                ),
            }
        return delay, None

    def handle(self, args: dict) -> dict:
        """Execute one already-validated action in the internal flat shape.

        ``manual`` is not handled here: it is a no-I/O documentation action
        owned by the family's reserved ``manual`` child
        (``_tool_family.build_manual_child``), which never reaches this engine.
        """
        action = args.get("action", "run")
        if action == "poll":
            return self._handle_poll(args)
        if action == "cancel":
            return self._handle_cancel(args)
        return self._handle_run(args)

    def _handle_run(self, args: dict) -> dict:
        command = args.get("command", "")
        err = self._validate_command(command)
        if err:
            return err
        from lingtai.kernel.execution_workspace import current_execution_workspace
        workspace = current_execution_workspace()
        cwd = args.get("working_dir") or (
            str(workspace.root) if workspace is not None else self._working_dir
        )
        if isinstance(cwd, str) and not cwd.strip():
            cwd = str(workspace.root) if workspace is not None else self._working_dir
        if workspace is not None:
            from lingtai.kernel.execution_workspace import resolve_execution_path
            try:
                cwd = str(resolve_execution_path(cwd, fallback_root=workspace.root))
            except (ValueError, OSError):
                return {"status": "error", "message": "Invalid working_dir path"}
        err = self._validate_working_dir(cwd)
        if err:
            return err
        # The powershell dialect may reject cmd.exe-shim commands whose
        # metacharacters would be unsafe under cmd.exe; surface that as a
        # regular error instead of an unhandled exception.
        try:
            invocation = self._dialect.make_invocation(command)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if args.get("async", False):
            reminder, err = self._validate_reminder(args.get("reminder"))
            if err:
                return err
            return self._run_async(command, cwd, reminder, invocation)
        # Jason 2026-08-10 tool-timeout redesign: the default stays 30s; a
        # sync call may set ``timeout`` at most to the hard ceiling
        # (``LINGTAI_TOOL_TIMEOUT_MAX_SECONDS``, default 120).  Above the
        # ceiling the call is refused and steered to ``async=true`` rather
        # than silently clamped, so the model learns the async boundary
        # instead of receiving a shorter timeout than it asked for.
        #
        # Defensive validation keeps the "never raise to the tool caller"
        # invariant even for legacy-flat callers that bypass the family's
        # schema stripping: ``None`` means *absent* (the default applies),
        # non-numeric and non-finite values become a regular error result,
        # and a negative value is rejected (``timeout: 0`` still flows
        # through unchanged, preserving the CONTRACT falsy-passthrough).
        timeout = args.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
        if timeout is None:
            timeout = _DEFAULT_TIMEOUT_SECONDS
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return {"status": "error", "message": "timeout must be a number of seconds"}
        if not math.isfinite(timeout) or timeout < 0:
            return {
                "status": "error",
                "message": "timeout must be a finite non-negative number of seconds",
            }
        # The ceiling is floored at the default timeout so an operator who
        # sets the environment variable below 30 does not silently disable
        # every default sync run (BLOCKING-1 in fable r1).
        cap = max(resolve_timeout_max_seconds(), float(_DEFAULT_TIMEOUT_SECONDS))
        if timeout > cap:
            return {
                "status": "error",
                "message": (
                    f"timeout {timeout:g}s exceeds the hard ceiling "
                    f"{cap:g}s ({TIMEOUT_MAX_ENV}); for work that may need "
                    "longer, launch it with async=true and poll the job "
                    "instead of raising the sync timeout."
                ),
            }
        return self._run_sync(command, cwd, timeout, invocation)

    def _run_sync(self, command: str, cwd: str, timeout: float, invocation: ShellInvocation) -> dict:
        """Run the selected invocation; timeout/capture/result policy stays here."""
        try:
            process_args, process_kwargs = invocation.process_args()
        except Exception as e:
            # Historical safety net: a dialect-construction failure (e.g. the
            # #1191 "stdin_script requires the argv form" ValueError) surfaces
            # as a result on every platform, never as an exception to the
            # tool caller.
            return {"status": "error", "message": f"Command failed: {e}"}
        if invocation.encoding is not None:
            process_kwargs["encoding"] = invocation.encoding
        if invocation.errors is not None:
            process_kwargs["errors"] = invocation.errors
        # #1191 stdin bootstrap: a dialect that delivers the real script via
        # stdin (``invocation.stdin_script``) carries it as the ``input``
        # process kwarg, consumed by ``subprocess.run`` here and forwarded to
        # ``communicate(input=...)`` by the contained path.  The getattr guard
        # keeps this inert until #1191 lands on this branch.
        stdin_script = getattr(invocation, "stdin_script", None)
        if stdin_script is not None:
            process_kwargs["input"] = stdin_script
        if _sync_run_contained():
            try:
                return self._run_sync_contained(
                    command, cwd, timeout, invocation, process_args, process_kwargs,
                )
            except Exception as e:
                # Keep the historical safety net: an unexpected contained-path
                # failure is reported, never raised to the tool caller.
                return {"status": "error", "message": f"Command failed: {e}"}
        if os.name == "posix":
            # POSIX (macOS/Linux): own process group with graceful-then-KILL
            # tree kill on timeout.  macOS has no cgroups/Job Objects and no
            # ``/usr/bin/timeout``; the process group is the only reliable
            # tree primitive there, and this supervisor enforces ``timeout``
            # in-process instead of shelling out.
            try:
                return self._run_sync_posix_grouped(
                    command, cwd, timeout, invocation, process_args, process_kwargs,
                )
            except Exception as e:
                # Same safety net as the contained path: an unexpected
                # grouped-path failure is reported, never raised to the tool
                # caller.
                return {"status": "error", "message": f"Command failed: {e}"}
        try:
            process_args, process_kwargs = invocation.process_args()
            if invocation.encoding is not None:
                process_kwargs["encoding"] = invocation.encoding
            if invocation.errors is not None:
                process_kwargs["errors"] = invocation.errors
            if invocation.stdin_script is not None:
                # The dialect transports the real command through stdin (the
                # command line carries only an ASCII bootstrap).  ``input`` in
                # text mode lets subprocess encode it with the dialect encoding
                # (UTF-8) and feed it while concurrently draining the pipes.
                process_kwargs["input"] = invocation.stdin_script
            result = subprocess.run(
                process_args, capture_output=True, text=True,
                timeout=timeout, cwd=cwd, **process_kwargs,
            )
            stdout, stderr = result.stdout, result.stderr
            # Output hygiene at the tool boundary: strip ANSI/CSI, escape
            # C0/C1 controls, drop startup noise, then cap with the explicit
            # truncation marker (all in one pass per stream).
            stdout = sanitize_output(stdout, self._max_output)
            stderr = sanitize_output(stderr, self._max_output)
            return _augment_command_result({
                "status": "ok", "exit_code": result.returncode,
                "stdout": stdout, "stderr": stderr,
            }, command=command)
        except subprocess.TimeoutExpired as exc:
            # ``no-output`` timeout (OpenClaw exec-runner.ts semantics): the
            # command was killed before it produced any output, so steer the
            # model toward async=true / a daemon instead of a silent foreground
            # run (or a trailing-& shell background).
            return _timeout_error(
                command, timeout, no_output=not (exc.stdout or exc.stderr)
            )
        except Exception as e:
            return {"status": "error", "message": f"Command failed: {e}"}

    def _sync_result_from(self, stdout: str, stderr: str, returncode: int, command: str | None = None) -> dict:
        """Sanitize captured output once and apply shared fidelity fields."""
        stdout = sanitize_output(stdout, self._max_output)
        stderr = sanitize_output(stderr, self._max_output)
        return _augment_command_result({
            "status": "ok", "exit_code": returncode,
            "stdout": stdout, "stderr": stderr,
        }, command=command)

    def _run_sync_contained(
        self, command: str, cwd: str, timeout: float,
        invocation: ShellInvocation, process_args: object, process_kwargs: dict,
    ) -> dict:
        """Windows sync run: Job Object containment plus bounded pipe drain.

        ``subprocess.run``'s Windows timeout path kills only the direct child
        and then blocks in a second ``communicate()`` until EOF; a grandchild
        that inherited the stdout/stderr pipe write ends hangs the caller
        forever (Goose PR #7689).  Contain the command in a kill-on-close Job
        Object (Codex ``process_group`` ``win/job.rs``) so a timeout kills the
        whole tree race-free, then drain the pipes only for the bounded
        ``io_drain_timeout`` window (Codex ``exec.rs``).  If Job containment is
        unavailable on the host, fall back to the historical plain
        ``subprocess.run`` behavior.

        Containment is deliberate: the Job Object is kill-on-close, so closing
        the job handle on the success path also terminates any surviving
        descendant (e.g. a daemon the script started with ``Start-Process``
        and detached stdio from).  Background work that must outlive the
        command belongs to the async path (``async: true``).
        """
        from lingtai.adapters.windows import win32_job

        spawn_kwargs = {
            **process_kwargs,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "cwd": cwd,
        }
        # #1191 stdin bootstrap integration: the dialect delivers the real
        # script through stdin (``invocation.stdin_script``, surfaced as the
        # ``input`` kwarg by ``_run_sync``).  ``Popen`` has no ``input``
        # parameter, so pop it and feed ``communicate(input=...)``; without
        # this, the bootstrap's ``ReadToEnd()`` never sees EOF and every sync
        # command would time out and be tree-killed.  The invocation-attribute
        # fallback keeps the contained path correct even if a later merge
        # drops the ``_run_sync`` wiring line.
        input_data = spawn_kwargs.pop("input", None)
        if input_data is None:
            input_data = getattr(invocation, "stdin_script", None)
        if input_data is not None:
            spawn_kwargs["stdin"] = subprocess.PIPE
        else:
            # Never inherit the parent console stdin: a sync run must not read
            # from or pin the supervisor's input handle, and a background
            # child cannot keep our stdin open after the command completes.
            spawn_kwargs["stdin"] = subprocess.DEVNULL
        try:
            process, job = win32_job.spawn_into_job(process_args, spawn_kwargs)
        except FileNotFoundError:
            # A genuine spawn failure (missing executable) must be reported,
            # not retried through the plain path as if the Job machinery had
            # failed.
            raise
        except OSError:
            # Job containment unavailable (host job policy / sandboxed
            # environment): keep the pre-existing plain subprocess.run path.
            try:
                result = subprocess.run(
                    process_args, capture_output=True, text=True,
                    timeout=timeout, cwd=cwd, **process_kwargs,
                )
            except subprocess.TimeoutExpired as exc:
                return _timeout_error(
                    command, timeout, no_output=not (exc.stdout or exc.stderr)
                )
            return self._sync_result_from(result.stdout, result.stderr, result.returncode, command)
        try:
            try:
                stdout, stderr = process.communicate(input=input_data, timeout=timeout)
                returncode = process.returncode
            except subprocess.TimeoutExpired as exc:
                win32_job.terminate_owned_tree(job, process.pid)
                # Wait briefly for the killed tree to actually exit before the
                # pipe drain: in the common case the real EOF then arrives
                # within the drain bound and the drain returns the full output
                # instead of the partial from the timeout exception.  The
                # bound is structural (wait_job_empty never waits longer than
                # its own timeout), and the drain itself never blocks on EOF.
                win32_job.wait_job_empty(job, win32_job.IO_DRAIN_TIMEOUT_SECONDS)
                partial_out, partial_err = win32_job.drain_pipes(
                    process, win32_job.IO_DRAIN_TIMEOUT_SECONDS
                )
                # Windows ``communicate`` raises the bare TimeoutExpired (no
                # ``output=``/``stderr=`` args), so ``exc.stdout``/``exc.stderr``
                # are always None here and the no-output signal must come from
                # the partial the bounded drain actually collected.
                return _timeout_error(
                    command, timeout, no_output=not (partial_out or partial_err)
                )
        finally:
            # Closing the last job handle fires KILL_ON_JOB_CLOSE, terminating
            # any surviving descendant — this is the containment contract of
            # the sync path (see module/CONTRACT docs).
            win32_job.close_handle(job)
        return self._sync_result_from(stdout, stderr, returncode, command)

    def _run_sync_posix_grouped(
        self, command: str, cwd: str, timeout: float,
        invocation: ShellInvocation, process_args: object, process_kwargs: dict,
    ) -> dict:
        """POSIX sync run: own process group plus graceful-then-KILL tree kill.

        macOS has no cgroups/Job Objects and no ``/usr/bin/timeout``; the only
        reliable tree primitive is the process group (Hermes killpg pattern).
        Every POSIX sync command is therefore spawned with
        ``start_new_session=True`` (the child becomes its own PGID leader),
        ``timeout`` is enforced by this supervisor, and on expiry the whole
        group gets SIGTERM and then SIGKILL after a short grace period.
        ``subprocess.run``'s timeout path kills only the direct child and
        leaks grandchildren; this path never relies on an external ``timeout``
        binary.  This mirrors the Windows Job Object containment
        (``_run_sync_contained``) on the POSIX side.
        """
        spawn_kwargs = {
            **process_kwargs,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "cwd": cwd,
            "start_new_session": True,
        }
        # stdin bootstrap integration (same contract as the contained path):
        # ``input`` is a ``subprocess.run``-only kwarg, so pop it and feed
        # ``communicate(input=...)``; the ASCII bootstrap otherwise never sees
        # EOF and every command would time out and be group-killed.
        input_data = spawn_kwargs.pop("input", None)
        if input_data is not None:
            spawn_kwargs["stdin"] = subprocess.PIPE
        else:
            # Never inherit the parent stdin: a sync run must not read from or
            # pin the supervisor's input handle.
            spawn_kwargs["stdin"] = subprocess.DEVNULL
        process = subprocess.Popen(process_args, **spawn_kwargs)
        try:
            try:
                stdout, stderr = process.communicate(input=input_data, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                pgid = process.pid
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
                # Grace period for the direct child and its descendants to
                # exit on SIGTERM before the forced kill (0.5s, matching the
                # async POSIX adapter's graceful-then-KILL window).
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)
                    except (ProcessLookupError, OSError):
                        break
                    time.sleep(0.05)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    process.wait(timeout=_IO_DRAIN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                # Same no-output steering as the contained paths: a kill-before-
                # output timeout appends the background-discipline guidance
                # (#1201), so the grouped POSIX path stays message-compatible.
                return _timeout_error(
                    command, timeout, no_output=not (exc.stdout or exc.stderr)
                )
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        return self._sync_result_from(stdout, stderr, process.returncode, command)

    @staticmethod
    def _terminal(status: object) -> bool:
        return status in {"completed", "unrecoverable"}

    def _rehydrate_async_jobs(self) -> None:
        """Restore deadline/completion publication work from durable job state."""
        jobs_dir = self._jobs_path()
        if not jobs_dir.is_dir():
            return
        for job_dir in jobs_dir.iterdir():
            if not job_dir.is_dir() or _JOB_ID_RE.fullmatch(job_dir.name) is None:
                continue
            state = load_state(job_dir)
            if state is None:
                continue  # Legacy jobs remain readable by _handle_poll.
            if not self._terminal(state.get("status")):
                state = self._mark_unrecoverable_if_supervisor_gone(job_dir) or load_state(job_dir) or state
            job_id = job_dir.name
            reminder = state.get("reminder")
            if self._terminal(state.get("status")):
                # Completion owns the wake-up once terminal truth exists.  A
                # watchdog saying the job "may still be running" is stale and
                # must not be re-armed by a fresh manager.
                def suppress_terminal_reminder(current: dict) -> dict:
                    durable_reminder = current.get("reminder")
                    if isinstance(durable_reminder, dict) and durable_reminder.get("state") in {
                        "pending", "publishing", "suppressing"
                    }:
                        durable_reminder.update({
                            "state": "suppressed",
                            "suppressed_at": time.time(),
                        })
                        durable_reminder.pop("claim_token", None)
                        durable_reminder.pop("suppressing_at", None)
                        durable_reminder.pop("suppressing_until", None)
                    return current

                update_state(job_dir, suppress_terminal_reminder)
                if self._retry_failed_publications:
                    self._start_completion_watcher(job_id, job_dir)
                else:
                    self._publish_completion_if_due(job_id, job_dir)
            else:
                if isinstance(reminder, dict) and reminder.get("state") in {"pending", "publishing", "suppressing"}:
                    self._start_reminder_timer(job_id, job_dir)
                self._start_completion_watcher(job_id, job_dir)

    def _initial_async_state(
        self, job_id: str, command: str, cwd: str, reminder: float,
        invocation: ShellInvocation | None = None,
    ) -> dict:
        now = time.time()
        invocation = invocation or self._dialect.make_invocation(command)
        return {
            "version": 3,
            "job_id": job_id,
            "command": command,
            "shell_dialect": self._dialect.state_key(),
            "shell_kind": self._shell_kind.value,
            "invocation": invocation.to_dict(),
            "cwd": cwd,
            "status": "launching",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "pid": None,
            "pid_identity": None,
            "pid_start_time": None,
            "process_group": None,
            "supervisor_process": None,
            "command_process": None,
            "supervisor_start_lease": {
                "token": secrets.token_hex(16),
                "deadline_at": now + _SUPERVISOR_START_LEASE_SECONDS,
                "state": "pending",
            },
            "return_handoff": {
                "state": "pending",
                "deadline_at": now + _RETURN_HANDOFF_LEASE_SECONDS,
            },
            "exit_status_known": False,
            "exit_code": None,
            "terminal_polled": False,
            "reminder": {
                "deadline_at": now + reminder,
                "state": "pending",
                "ref_id": f"bash.reminder:{job_id}",
            },
            "completion": {
                "state": "pending",
                "ref_id": f"bash.completion:{job_id}",
            },
        }

    def _run_async(
        self, command: str, cwd: str, reminder: float,
        invocation: ShellInvocation | None = None,
    ) -> dict:
        """Start a detached durable supervisor and return its command PID."""
        jobs_dir = self._ensure_jobs_dir()
        job_dir: Path | None = None
        job_id = ""
        # Retained records make collision handling a correctness requirement, not
        # cleanup hygiene.  A full UUID has ample entropy; mkdir remains the
        # collision-safe authority if a hostile or extraordinarily unlikely name
        # is already present.
        for _ in range(8):
            candidate = f"job-{uuid.uuid4().hex}"
            try:
                candidate_dir = jobs_dir / candidate
                candidate_dir.mkdir()
            except FileExistsError:
                continue
            except OSError as exc:
                return {"status": "error", "message": f"Failed to create async job: {exc}"}
            job_id, job_dir = candidate, candidate_dir
            break
        if job_dir is None:
            return {"status": "error", "message": "Failed to allocate a unique async job ID"}
        initial_state = self._initial_async_state(job_id, command, cwd, reminder, invocation)
        start_lease = initial_state["supervisor_start_lease"]
        start_token = start_lease["token"]
        try:
            write_initial_state(job_dir, initial_state)
        except Exception as exc:
            return {"status": "error", "message": f"Failed to initialize async job: {exc}"}
        try:
            supervisor_ref, supervisor = self._async_process.launch_supervisor(job_dir, start_token)
        except Exception as exc:
            supervisor_error = f"cannot start supervisor: {exc}"

            def mark_failed(state: dict) -> dict:
                state.update({
                    "status": "unrecoverable", "finished_at": time.time(),
                    "supervisor_error": supervisor_error,
                })
                return state

            update_state(job_dir, mark_failed)
            return {"status": "error", "message": supervisor_error}

        # Record the launched supervisor PID from the owning parent even when an
        # OS incarnation identity cannot be observed.  The child must still claim
        # the matching durable lease before it can spawn the command.
        supervisor_identity = supervisor_ref.incarnation

        def record_supervisor(state: dict) -> dict:
            lease = state.get("supervisor_start_lease")
            if (
                self._terminal(state.get("status"))
                or not isinstance(lease, dict)
                or lease.get("token") != start_token
            ):
                return state
            state["supervisor_pid"] = supervisor_ref.public_id
            if supervisor_identity is not None:
                state["supervisor_identity"] = supervisor_identity
            state["supervisor_process"] = supervisor_ref.to_dict()
            return state

        update_state(job_dir, record_supervisor)

        # If this owned supervisor exits before a terminal commit, its parent has
        # stronger evidence than any PID heuristic and closes the state itself.
        threading.Thread(
            target=self._reap_supervisor,
            args=(supervisor, job_dir),
            daemon=True,
        ).start()

        deadline = time.monotonic() + _SUPERVISOR_START_LEASE_SECONDS
        state = load_state(job_dir)
        while time.monotonic() < deadline:
            state = load_state(job_dir)
            pid = state.get("pid") if state else None
            if isinstance(pid, int):
                # Preserve the historical/user-facing meaning of `reminder=N`:
                # the caller gets N seconds after a successful async-start return,
                # rather than losing supervisor-startup time from that interval.
                # The initial deadline remains a crash-safe fallback if this
                # manager disappears before reaching the return path.
                return_armed = False

                def arm_from_return(current: dict) -> dict:
                    # This lock-owned mutation is the successful-return boundary.
                    # Success is conditional on winning the still-valid handoff:
                    # after expiry another manager is entitled to publish the
                    # crash fallback, and that already-published event cannot be
                    # recalled by a late owner.
                    nonlocal return_armed
                    returned_at = time.time()
                    return_handoff = current.get("return_handoff")
                    handoff_pending = (
                        isinstance(return_handoff, dict)
                        and return_handoff.get("state") == "pending"
                    )
                    handoff_deadline = (
                        return_handoff.get("deadline_at")
                        if isinstance(return_handoff, dict)
                        else None
                    )
                    handoff_valid = (
                        handoff_pending
                        and isinstance(handoff_deadline, (int, float))
                        and not isinstance(handoff_deadline, bool)
                        and returned_at < float(handoff_deadline)
                    )
                    if self._terminal(current.get("status")):
                        # A very short command may finish exactly before the start
                        # call returns.  That is still a successful start only when
                        # exact terminal truth won while the handoff was valid; its
                        # terminal commit already suppressed the fallback reminder.
                        if (
                            handoff_valid
                            and current.get("status") in {"completed", "failed"}
                            and current.get("exit_status_known") is True
                        ):
                            return_handoff.update({
                                "state": "completed_before_return",
                                "returned_at": returned_at,
                            })
                            return_armed = True
                        elif handoff_pending:
                            return_handoff.update({
                                "state": "aborted",
                                "resolved_at": returned_at,
                            })
                        return current
                    if not handoff_pending:
                        return current
                    if not handoff_valid:
                        return_handoff.update({
                            "state": "expired",
                            "expired_at": returned_at,
                        })
                        return current
                    durable_reminder = current.get("reminder")
                    if not (
                        isinstance(durable_reminder, dict)
                        and durable_reminder.get("state") in {
                            "pending", "publishing", "suppressing"
                        }
                    ):
                        return current
                    durable_reminder["deadline_at"] = returned_at + reminder
                    # A pre-return publisher should have been deferred by the
                    # handoff guard.  Recover conservatively if a stale claim from
                    # an older implementation nevertheless exists.
                    if durable_reminder.get("state") == "publishing":
                        durable_reminder["state"] = "pending"
                        durable_reminder.pop("claim_token", None)
                        durable_reminder.pop("claimed_at", None)
                    return_handoff.update({
                        "state": "armed",
                        "returned_at": returned_at,
                    })
                    return_armed = True
                    return current

                update_state(job_dir, arm_from_return)
                self._start_reminder_timer(job_id, job_dir)
                self._start_completion_watcher(job_id, job_dir)
                if not return_armed:
                    return {
                        "status": "error",
                        "job_id": job_id,
                        "pid": pid,
                        "message": (
                            "Async job started, but its successful-return handoff "
                            "expired or was superseded. The job remains pollable "
                            "by job_id and its crash-fallback reminder remains authoritative."
                        ),
                    }
                return {
                    "status": "ok", "job_id": job_id, "pid": pid,
                    # Teaches the registered public envelope, not the internal
                    # flat shape: an agent that copies this receipt verbatim
                    # must land a call the public ``shell`` schema accepts.
                    "message": (
                        'Job started. Use shell(action="poll", '
                        f'input={{"job_id": "{job_id}"}}) to check.'
                    ),
                    "handoff": self._async_handoff,
                }
            if state and self._terminal(state.get("status")):
                break
            time.sleep(0.01)
        self._mark_unrecoverable_if_supervisor_gone(job_dir)
        return {"status": "error", "message": "Failed to start async job supervisor"}

    def _reap_supervisor(self, supervisor, job_dir: Path) -> None:
        try:
            returncode = self._async_process.wait_supervisor(supervisor)
        except Exception:
            return

        def close_abandoned_start(state: dict) -> dict:
            if self._terminal(state.get("status")):
                return state
            state.update({
                "status": "unrecoverable",
                "exit_status_known": False,
                "exit_code": None,
                "finished_at": time.time(),
                "supervisor_error": (
                    f"owned supervisor exited with code {returncode} before terminal commit"
                ),
            })
            return state

        update_state(job_dir, close_abandoned_start)

    def _start_reminder_timer(self, job_id: str, job_dir: Path, delay: float | None = None) -> None:
        """Arm/re-arm the persisted deadline; a new manager can resume it."""
        if delay is None:
            state = load_state(job_dir)
            reminder = state.get("reminder") if state else None
            if not isinstance(reminder, dict) or not isinstance(reminder.get("deadline_at"), (int, float)):
                return
            delay = max(0.0, float(reminder["deadline_at"]) - time.time())
        with self._reminder_lock:
            if job_id in self._reminder_cancel_events:
                return
            cancel_event = threading.Event()
            self._reminder_cancel_events[job_id] = cancel_event
        threading.Thread(
            target=self._run_reminder_timer,
            args=(job_id, job_dir, delay, cancel_event), daemon=True,
        ).start()

    def _run_reminder_timer(
        self, job_id: str, job_dir: Path, delay: float, cancel_event: threading.Event,
    ) -> None:
        if cancel_event.wait(delay):
            return
        claim_token = self._claim_reminder_timer(job_id, job_dir, cancel_event)
        if claim_token is None:
            return
        # The helper retains the cross-manager state lock through the final
        # pre-publish suppression check, sink write, and acknowledgement.  A
        # terminal claim which wins that lock makes this stale token a no-op.
        published = self._publish_claimed_reminder(job_id, job_dir, claim_token)
        if published:
            self._clear_reminder_retry_delay(job_id)
        elif self._retry_failed_publications and job_dir.is_dir():
            # A full daemon RunDir queue returns False without acknowledging this
            # durable claim. Re-arm a bounded-backoff timer so freeing capacity in
            # this still-live manager reconciles the same stable reminder ref.
            self._start_reminder_timer(
                job_id, job_dir, self._next_reminder_retry_delay(job_id)
            )

    def _next_reminder_retry_delay(self, job_id: str) -> float:
        """Return the next bounded detached-publication retry delay."""
        with self._reminder_lock:
            previous = self._reminder_retry_delays.get(job_id)
            delay = (
                _DETACHED_PUBLICATION_RETRY_INITIAL_SECONDS
                if previous is None
                else min(previous * 2, _DETACHED_PUBLICATION_RETRY_MAX_SECONDS)
            )
            self._reminder_retry_delays[job_id] = delay
            return delay

    def _clear_reminder_retry_delay(self, job_id: str) -> None:
        with self._reminder_lock:
            self._reminder_retry_delays.pop(job_id, None)

    def _claim_reminder_timer(
        self, job_id: str, job_dir: Path, cancel_event: threading.Event,
    ) -> str | None:
        """Claim only a currently due reminder; stale timers defer to durable truth."""
        with self._reminder_lock:
            current = self._reminder_cancel_events.get(job_id)
            if current is not cancel_event or cancel_event.is_set() or not job_dir.is_dir():
                return None
            self._reminder_cancel_events.pop(job_id, None)
            cancel_event.set()
        state = load_state(job_dir)
        if state is None:  # Compatibility for the original private race tests.
            return "legacy-private-race"
        claim_token = uuid.uuid4().hex
        claimed = False
        defer_seconds: float | None = None

        def claim(current_state: dict) -> dict:
            nonlocal claimed, defer_seconds
            reminder = current_state.get("reminder")
            if not isinstance(reminder, dict) or reminder.get("state") in {
                "suppressed", "published"
            }:
                return current_state
            now = time.time()
            if reminder.get("state") == "suppressing":
                suppressing_until = reminder.get("suppressing_until")
                if (
                    isinstance(suppressing_until, (int, float))
                    and not isinstance(suppressing_until, bool)
                    and float(suppressing_until) > now
                ):
                    defer_seconds = float(suppressing_until) - now
                    return current_state
                reminder["state"] = "pending"
                reminder.pop("suppressing_at", None)
                reminder.pop("suppressing_until", None)

            return_handoff = current_state.get("return_handoff")
            if (
                isinstance(return_handoff, dict)
                and return_handoff.get("state") == "pending"
            ):
                handoff_deadline = return_handoff.get("deadline_at")
                if (
                    isinstance(handoff_deadline, (int, float))
                    and not isinstance(handoff_deadline, bool)
                    and float(handoff_deadline) > now
                ):
                    # The manager which owns the synchronous start response has not
                    # yet durably moved the reminder to returned_at + delay.  Check
                    # the cross-process state again soon rather than sleeping until
                    # the whole lease expires, so a crash immediately after arming
                    # still recovers close to the requested deadline.
                    defer_seconds = min(
                        float(handoff_deadline) - now,
                        _RETURN_HANDOFF_RECHECK_SECONDS,
                    )
                    return current_state
                return_handoff.update({"state": "expired", "expired_at": now})

                # Once the bounded handoff fails, do not emit a misleading
                # may-still-be-running reminder for a start which is already known
                # to be unrecoverable.  The completion channel owns that wake-up.
                lease_expired = self._supervisor_start_lease_expired(current_state)
                supervisor_gone = self._supervisor_definitively_gone(current_state)
                if lease_expired or supervisor_gone:
                    reason = (
                        "supervisor start lease expired before command spawn"
                        if lease_expired
                        else "recorded supervisor is definitively gone before terminal commit"
                    )
                    current_state.update({
                        "status": "unrecoverable",
                        "exit_status_known": False,
                        "exit_code": None,
                        "finished_at": now,
                        "supervisor_error": reason,
                    })
                    reminder.update({"state": "suppressed", "suppressed_at": now})
                    reminder.pop("claim_token", None)
                    reminder.pop("claimed_at", None)
                    return current_state

            deadline_at = reminder.get("deadline_at")
            if (
                isinstance(deadline_at, (int, float))
                and not isinstance(deadline_at, bool)
                and float(deadline_at) > now
            ):
                # Another manager may have moved the crash-fallback deadline to
                # the successful-return boundary after this timer was armed.
                # Revert any stale publishing claim and let a fresh timer own the
                # later durable deadline.
                reminder["state"] = "pending"
                reminder.pop("claim_token", None)
                reminder.pop("claimed_at", None)
                defer_seconds = float(deadline_at) - now
                return current_state
            # A stale ``publishing`` claim is recoverable after a crash.  Replacing
            # its token makes concurrent rehydrators mutually exclusive at the
            # final publication gate below.
            reminder.update({
                "state": "publishing",
                "claimed_at": now,
                "claim_token": claim_token,
            })
            claimed = True
            return current_state

        update_state(job_dir, claim)
        if defer_seconds is not None:
            self._start_reminder_timer(job_id, job_dir, defer_seconds)
        return claim_token if claimed else None

    def _publish_claimed_reminder(self, job_id: str, job_dir: Path, claim_token: str) -> bool:
        if claim_token == "legacy-private-race":
            return self._publish_async_reminder(job_id) is not False
        return publish_reminder_if_claimed(
            job_dir, claim_token, lambda: self._publish_async_reminder(job_id),
        )

    def _cancel_reminder_timer(self, job_id: str) -> None:
        """Stop this manager's local deadline worker; durable suppression is a terminal claim."""
        with self._reminder_lock:
            cancel_event = self._reminder_cancel_events.pop(job_id, None)
            self._reminder_retry_delays.pop(job_id, None)
        if cancel_event is not None:
            cancel_event.set()

    def _publish_async_reminder(self, job_id: str) -> bool:
        # Same envelope-teaching rule as the async start receipt: this durable
        # watchdog may wake an agent hours later (possibly post-molt), so the
        # call it hands over must be the one the public schema accepts.
        body = (
            f"Shell async job {job_id} may still be running. "
            f'Poll it with shell(action="poll", input={{"job_id": "{job_id}"}}).'
        )
        notifications = self._notifications
        if notifications is not None:
            try:
                return bool(notifications.publish_system(
                    source="bash.reminder",
                    ref_id=f"bash.reminder:{job_id}",
                    body=body,
                    skip_if_ref_id_exists=True,
                ))
            except Exception:
                return False
        # Compatibility-only direct-manager route. Official bindings never
        # receive this Agent-shaped object; their port adapter preserves the
        # same canonical enqueue/fallback behavior above.
        agent = self._agent
        if hasattr(agent, "_enqueue_system_notification"):
            try:
                agent._enqueue_system_notification(
                    source="bash.reminder", ref_id=f"bash.reminder:{job_id}",
                    body=body, skip_if_ref_id_exists=True,
                )
                return True
            except Exception:
                pass
        try:
            self._append_system_notification_fallback(
                source="bash.reminder", ref_id=f"bash.reminder:{job_id}", body=body,
            )
            return True
        except Exception:
            return False

    def _append_system_notification_fallback(
        self, *, source: str, ref_id: str, body: str,
    ) -> str:
        """Append a system event using the agent's serialized store."""
        import secrets
        from datetime import datetime, timezone
        from lingtai.kernel.notification_store import UNCONDITIONAL

        store = self._agent._notification_store
        event_id = f"evt_{int(time.time()*1000):x}_{secrets.token_hex(8)}"
        received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        def mutate(current_payload: dict) -> tuple[dict | None, bool, str]:
            current = current_payload if isinstance(current_payload, dict) else {}
            events = list(current.get("data", {}).get("events", []))
            if any(isinstance(event, dict) and event.get("ref_id") == ref_id for event in events):
                return current_payload, False, ""
            events.append({
                "event_id": event_id, "source": source, "ref_id": ref_id,
                "body": body, "at": received_at,
            })
            events = events[-20:]
            return ({
                "header": f"{len(events)} system notification{'s' if len(events) != 1 else ''}",
                "icon": "🔔", "priority": "normal", "published_at": received_at,
                "data": {"events": events},
            }, True, event_id)
        result = store.compare_update_channel("system", UNCONDITIONAL, mutate)
        return result.value if isinstance(result.value, str) else ""

    def _start_completion_watcher(self, job_id: str, job_dir: Path) -> None:
        with self._completion_lock:
            if job_id in self._completion_watchers:
                return
            self._completion_watchers.add(job_id)
        threading.Thread(
            target=self._watch_durable_job, args=(job_id, job_dir), daemon=True,
        ).start()

    def _watch_durable_job(self, job_id: str, job_dir: Path) -> None:
        retry_delay = _DETACHED_PUBLICATION_RETRY_INITIAL_SECONDS
        try:
            while True:
                state = load_state(job_dir)
                if state is None:
                    return
                if self._terminal(state.get("status")):
                    self._publish_completion_if_due(job_id, job_dir)
                    if not self._retry_failed_publications:
                        return
                    latest = load_state(job_dir)
                    completion = latest.get("completion") if latest else None
                    if not isinstance(completion, dict) or completion.get("state") == "published":
                        return
                    # The daemon's bounded queue may drain without any Shell
                    # action. Keep reconciling its unacknowledged stable ref with
                    # capped exponential backoff; this thread is daemonized and
                    # cannot hold a terminal daemon process open.
                    time.sleep(retry_delay)
                    retry_delay = min(
                        retry_delay * 2, _DETACHED_PUBLICATION_RETRY_MAX_SECONDS
                    )
                    continue
                time.sleep(0.05)
        finally:
            with self._completion_lock:
                self._completion_watchers.discard(job_id)

    def _publish_completion_if_due(self, job_id: str, job_dir: Path) -> None:
        claimed = False
        def claim(state: dict) -> dict:
            nonlocal claimed
            completion = state.get("completion")
            if not self._terminal(state.get("status")) or not isinstance(completion, dict):
                return state
            if completion.get("state") == "published":
                return state
            completion["state"] = "publishing"
            claimed = True
            return state
        state = update_state(job_dir, claim)
        if not claimed or state is None:
            return
        if self._publish_async_completion(job_id, job_dir, state):
            def published(current: dict) -> dict:
                completion = current.get("completion")
                if isinstance(completion, dict) and completion.get("state") == "publishing":
                    completion["state"] = "published"
                    completion["published_at"] = time.time()
                return current
            update_state(job_dir, published)

    def _publish_async_completion(self, job_id: str, job_dir: Path, state: dict) -> bool:
        try:
            from datetime import datetime, timezone
            stdout, _ = self._read_logs(job_dir)
            exit_code = state.get("exit_code") if state.get("exit_status_known") else None
            payload = {
                "header": f"Job {job_id} completed (exit {exit_code})",
                "icon": "⚡", "priority": "normal",
                "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "data": {
                    "job_id": job_id,
                    "command": str(state.get("command", ""))[:200],
                    "exit_code": exit_code,
                    "exit_status_known": bool(state.get("exit_status_known")),
                    "stdout_preview": stdout[:200],
                    "ref_id": f"bash.completion:{job_id}",
                },
            }
            ref_id = f"bash.completion:{job_id}"
            notifications = self._notifications
            if notifications is not None:
                return bool(notifications.publish_channel("bash", payload, ref_id=ref_id))
            # Compatibility-only direct-manager route; official bindings carry
            # only the port above and never reach an Agent or its store.
            store = self._agent._notification_store
            if hasattr(store, "compare_update_channel"):
                from lingtai.kernel.notification_store import UNCONDITIONAL

                def mutate(current_payload: dict) -> tuple[dict | None, bool, bool]:
                    current = current_payload if isinstance(current_payload, dict) else {}
                    data = current.get("data")
                    if isinstance(data, dict) and data.get("ref_id") == ref_id:
                        return current_payload, False, True
                    return payload, True, True

                result = store.compare_update_channel("bash", UNCONDITIONAL, mutate)
                return bool(result.value)
            store.publish("bash", payload)
            return True
        except Exception:
            return False

    def _read_logs(self, job_dir: Path) -> tuple[str, str]:
        def read_log(name: str) -> str:
            raw = (job_dir / name).read_bytes()
            if os.name != "nt":
                # POSIX async logs keep the historical replacement-character
                # decode; the Windows OEM fallback below is console-specific.
                text = raw.decode("utf-8", errors="replace")
            else:
                # The PowerShell wrapper forces the child's console encoding
                # to UTF-8, but a native tool can still emit OEM-codepage
                # bytes; re-decode those instead of corrupting them with
                # errors="replace".  The fallback is decided per line, so a
                # mostly-UTF-8 log with one invalid byte run is not re-decoded
                # wholesale as OEM (which would garble the whole log).
                from lingtai.adapters.windows.powershell import decode_windows_output

                text = decode_windows_output(raw)
            # Restore the universal-newlines translation the previous
            # read_text() provided: pwsh emits CRLF-terminated lines, and
            # without this every async stdout/stderr line would carry a
            # stray "\r" into results, truncation accounting, and
            # newline-based splitting.
            return text.replace("\r\n", "\n").replace("\r", "\n")

        try:
            stdout = read_log("stdout.log")
        except OSError:
            stdout = ""
        try:
            stderr = read_log("stderr.log")
        except OSError:
            stderr = ""
        # Same hygiene as the sync path: async logs are surfaced to the model
        # through poll results and completion previews, so they get the same
        # ANSI/control/noise stripping and capped truncation.
        stdout = sanitize_output(stdout, self._max_output)
        stderr = sanitize_output(stderr, self._max_output)
        return stdout, stderr

    def _already_finished(self, state: dict) -> dict:
        label = "cancelled" if state.get("terminal_consumed_by") == "cancel" else state.get("status")
        return {"status": "error", "message": f"Job already finished ({label})"}

    def _claim_terminal(self, job_dir: Path, consumer: str) -> dict | None:
        """Atomically consume a terminal result and suppress its reminder.

        The conditional state transition is the one-shot linearization point for
        both poll and cancel.  Suppression belongs in this same durable write so
        no successful terminal consumer can leave a later deadline publication.
        """
        claimed = False

        def claim(current: dict) -> dict:
            nonlocal claimed
            if not self._terminal(current.get("status")) or current.get("terminal_polled"):
                return current
            now = time.time()
            current.update({
                "terminal_polled": True,
                "terminal_polled_at": now,
                "terminal_consumed_by": consumer,
            })
            reminder = current.get("reminder")
            if isinstance(reminder, dict):
                # Even a previously published reminder is terminally suppressed
                # for future retries; its published_at remains historical evidence.
                reminder.update({"state": "suppressed", "suppressed_at": now})
                reminder.pop("claim_token", None)
                reminder.pop("suppressing_at", None)
                reminder.pop("suppressing_until", None)
            claimed = True
            return current

        state = update_state(job_dir, claim)
        return state if claimed else None

    def _terminal_result(self, job_id: str, job_dir: Path) -> dict | None:
        state = self._claim_terminal(job_dir, "poll")
        if state is None:
            return None
        self._cancel_reminder_timer(job_id)
        stdout, stderr = self._read_logs(job_dir)
        if state.get("exit_status_known") and isinstance(state.get("exit_code"), int):
            return _augment_command_result({
                "status": "done", "exit_status_known": True,
                "exit_code": state["exit_code"], "stdout": stdout, "stderr": stderr,
            }, command=str(state.get("command") or ""))
        return {
            "status": "done", "job_id": job_id, "exit_status_known": False,
            "exit_code": None, "stdout": stdout, "stderr": stderr,
            "message": "Async job terminated but its exit status is unavailable",
        }

    def _claim_legacy_terminal(self, job_dir: Path) -> bool:
        """Preserve old unknown-exit one-shot behavior without creating an exit code."""
        try:
            marker = job_dir / ".legacy-terminal-polled"
            with marker.open("x", encoding="utf-8") as handle:
                handle.write(f"{time.time()}\n")
            return True
        except FileExistsError:
            return False
        except OSError:
            return False

    def _legacy_unknown(self, job_id: str, job_dir: Path, message: str | None = None) -> dict:
        if not self._claim_legacy_terminal(job_dir):
            return {"status": "error", "message": "Job already finished (legacy unknown)"}
        stdout, stderr = self._read_logs(job_dir)
        return {
            "status": "done", "job_id": job_id, "exit_status_known": False,
            "exit_code": None, "stdout": stdout, "stderr": stderr,
            "message": message or "Legacy async job has no recoverable exit status",
        }

    def _handle_legacy_poll(self, job_id: str, job_dir: Path) -> dict:
        if (job_dir / ".legacy-terminal-polled").exists():
            return {"status": "error", "message": "Job already finished (legacy unknown)"}
        try:
            status = (job_dir / "status").read_text(encoding="utf-8").strip()
            pid = int((job_dir / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return self._legacy_unknown(job_id, job_dir)
        if status != "running" or self._async_process.observe(ProcessRef(pid, "legacy")).kind == "gone":
            return self._legacy_unknown(job_id, job_dir)
        # A legacy record cannot prove the PID incarnation, so it is never safe
        # to signal.  But a still-live PID is not evidence that the old command
        # has terminated either: keep the job pollable instead of consuming a
        # fabricated unknown terminal result while it may still be running.
        return {
            "status": "running",
            "job_id": job_id,
            "pid": pid,
            "message": "Legacy async job may still be running; cancellation is unavailable without durable supervisor ownership",
        }

    @staticmethod
    def _supervisor_start_lease_expired(state: dict) -> bool:
        """Whether a version-3 job missed its bounded pre-command start lease."""
        if state.get("status") != "launching":
            return False
        lease = state.get("supervisor_start_lease")
        if not isinstance(lease, dict) or lease.get("state") not in {"pending", "claimed"}:
            return False
        deadline_at = lease.get("deadline_at")
        return (
            isinstance(deadline_at, (int, float))
            and not isinstance(deadline_at, bool)
            and time.time() >= float(deadline_at)
        )

    @staticmethod
    def _durable_process_ref(state: dict, prefix: str) -> ProcessRef | None:
        """Prefer the neutral state contract, retaining v3 PID fields as fallback."""
        process = process_ref_from_state(state, prefix)
        if process is not None:
            return process
        if prefix == "command":
            public_id = state.get("pid")
            incarnation = state.get("pid_identity")
        elif prefix == "supervisor":
            public_id = state.get("supervisor_pid")
            incarnation = state.get("supervisor_identity")
        else:
            return None
        if (
            not isinstance(public_id, int)
            or isinstance(public_id, bool)
            or public_id <= 0
            or not isinstance(incarnation, str)
            or not incarnation
        ):
            return None
        return ProcessRef(public_id, incarnation)

    def _supervisor_definitively_gone(self, state: dict) -> bool:
        """True when an owned supervisor is absent or its incarnation changed."""
        process = self._durable_process_ref(state, "supervisor")
        if process is not None:
            return self._async_process.observe(process).kind in {"gone", "changed"}
        public_id = state.get("supervisor_pid")
        if not isinstance(public_id, int) or isinstance(public_id, bool) or public_id <= 0:
            return False
        # Absence remains proof for retained states that could not capture an
        # incarnation; a still-live diagnostic ID alone is never ownership proof.
        return self._async_process.observe(ProcessRef(public_id, "legacy")).kind == "gone"

    def _mark_unrecoverable_if_supervisor_gone(self, job_dir: Path) -> dict | None:
        """Resolve a lost supervisor or expired start lease under the state lock."""
        marked = False

        def mark(current: dict) -> dict:
            nonlocal marked
            if self._terminal(current.get("status")):
                return current
            lease_expired = self._supervisor_start_lease_expired(current)
            supervisor_gone = self._supervisor_definitively_gone(current)
            if not lease_expired and not supervisor_gone:
                return current
            reason = (
                "supervisor start lease expired before command spawn"
                if lease_expired
                else "recorded supervisor is definitively gone before terminal commit"
            )
            current.update({
                "status": "unrecoverable",
                "exit_status_known": False,
                "exit_code": None,
                "finished_at": time.time(),
                "supervisor_error": reason,
            })
            marked = True
            return current

        state = update_state(job_dir, mark)
        return state if marked else None

    def _await_supervisor_commit(self, job_dir: Path, timeout: float) -> dict | None:
        """Reload terminal truth while a supervisor or valid start lease can commit."""
        deadline = time.monotonic() + timeout
        state = load_state(job_dir)
        while state is not None:
            if self._terminal(state.get("status")):
                return state
            resolved = self._mark_unrecoverable_if_supervisor_gone(job_dir)
            if resolved is not None:
                return resolved
            if time.monotonic() >= deadline:
                return state
            time.sleep(0.01)
            state = load_state(job_dir)
        return None

    def _running_result(self, job_id: str, state: dict) -> dict:
        process = self._durable_process_ref(state, "command")
        if process is not None and self._async_process.observe(process).kind == "same":
            return {"status": "running", "job_id": job_id, "pid": process.public_id}
        return {
            "status": "running",
            "job_id": job_id,
            "message": "Awaiting the durable supervisor terminal commit",
        }

    def _handle_poll(self, args: dict) -> dict:
        job_id = args.get("job_id", "")
        err = self._validate_job_id(job_id)
        if err:
            return err
        job_dir = self._jobs_path() / job_id
        if not job_dir.is_dir():
            return {"status": "error", "message": f"Job not found: {job_id}"}
        state = load_state(job_dir)
        if state is None:
            return self._handle_legacy_poll(job_id, job_dir)
        if state.get("terminal_polled"):
            return self._already_finished(state)
        if self._terminal(state.get("status")):
            result = self._terminal_result(job_id, job_dir)
            return result if result is not None else self._already_finished(load_state(job_dir) or state)

        process = self._durable_process_ref(state, "command")
        if process is not None and self._async_process.observe(process).kind == "same":
            return {"status": "running", "job_id": job_id, "pid": process.public_id}

        # A dead/mismatched command PID is not terminal evidence: its detached
        # supervisor may have already obtained the exact wait result but not yet
        # committed it.  Give that verified supervisor a bounded commit window.
        state = self._await_supervisor_commit(job_dir, _SUPERVISOR_COMMIT_GRACE_SECONDS) or state
        if state.get("terminal_polled"):
            return self._already_finished(state)
        if self._terminal(state.get("status")):
            result = self._terminal_result(job_id, job_dir)
            return result if result is not None else self._already_finished(load_state(job_dir) or state)
        return self._running_result(job_id, state)

    def _handle_cancel(self, args: dict) -> dict:
        job_id = args.get("job_id", "")
        err = self._validate_job_id(job_id)
        if err:
            return err
        job_dir = self._jobs_path() / job_id
        if not job_dir.is_dir():
            return {"status": "error", "message": f"Job not found: {job_id}"}
        state = load_state(job_dir)
        if state is None:
            return {"status": "error", "message": "Cannot cancel legacy async job without durable supervisor ownership"}
        if self._terminal(state.get("status")) or state.get("terminal_polled"):
            return self._already_finished(state)
        supervisor = self._durable_process_ref(state, "supervisor")
        if supervisor is None:
            return {
                "status": "error",
                "message": "Cannot cancel async job: durable supervisor identity is unavailable",
            }
        supervisor_observation = self._async_process.observe(supervisor).kind
        if supervisor_observation in {"gone", "changed"}:
            self._mark_unrecoverable_if_supervisor_gone(job_dir)
            return {
                "status": "error",
                "message": "Cannot cancel async job: recorded supervisor identity is no longer live; poll for the durable terminal result",
            }
        if supervisor_observation != "same":
            return {
                "status": "error",
                "message": "Cannot cancel async job: durable supervisor identity cannot be verified",
            }

        requested = False

        def request_cancel(current: dict) -> dict:
            nonlocal requested
            if self._terminal(current.get("status")) or current.get("terminal_polled"):
                return current
            now = time.time()
            if not current.get("cancel_requested_at"):
                current["cancel_requested_at"] = now
            reminder = current.get("reminder")
            if isinstance(reminder, dict) and reminder.get("state") in {
                "pending", "publishing"
            }:
                reminder.update({
                    "state": "suppressing",
                    "suppressing_at": now,
                    "suppressing_until": now + _CANCEL_COMMIT_TIMEOUT_SECONDS,
                })
                reminder.pop("claim_token", None)
                reminder.pop("claimed_at", None)
            requested = True
            return current

        state = update_state(job_dir, request_cancel)
        if not requested or state is None:
            return self._already_finished(state or {})
        self._cancel_reminder_timer(job_id)

        # The detached supervisor owns the unreaped Popen and performs TERM/KILL.
        # A manager only requests that protocol, then waits for its exact commit.
        state = self._await_supervisor_commit(job_dir, _CANCEL_COMMIT_TIMEOUT_SECONDS)
        if state is not None and state.get("status") == "completed":
            if state.get("cancellation_outcome") != "group_cancelled":
                return {
                    "status": "error",
                    "message": (
                        "Cancellation did not confirm process-group termination; "
                        "poll for the exact durable terminal result"
                    ),
                }
            claimed = self._claim_terminal(job_dir, "cancel")
            if claimed is not None:
                self._cancel_reminder_timer(job_id)
                return {"status": "cancelled", "job_id": job_id}
            return self._already_finished(load_state(job_dir) or state)
        if state is not None and state.get("terminal_polled"):
            return self._already_finished(state)
        reminder_restored = False

        def restore_reminder(current: dict) -> dict:
            nonlocal reminder_restored
            if self._terminal(current.get("status")):
                return current
            reminder = current.get("reminder")
            if isinstance(reminder, dict) and reminder.get("state") == "suppressing":
                reminder["state"] = "pending"
                reminder.pop("suppressing_at", None)
                reminder.pop("suppressing_until", None)
                reminder_restored = True
            return current

        update_state(job_dir, restore_reminder)
        if reminder_restored:
            self._start_reminder_timer(job_id, job_dir)
        return {
            "status": "error",
            "message": (
                "Cancellation requested; awaiting supervisor terminal commit. "
                "The job remains pollable and its reminder remains recoverable."
            ),
        }

    def _close_handles(self, job_id: str) -> None:
        """Compatibility no-op: durable supervisors own and close their logs."""
        return None

# Compatibility names for direct imports from the retained implementation
# package.  The canonical public symbols are ShellPolicy and ShellManager.
BashPolicy = ShellPolicy
BashManager = ShellManager


def _mount_declared_shell(
    agent: "BaseAgent", *, configuration, notification_port: object | None = None,
) -> ShellManager:
    """Mount one Shell declaration with composition-owned, already-typed ports."""
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    (bound,) = register_agent_tool_plugins(
        agent,
        [DECLARATION],
        extra_ports_for=lambda declaration: (
            {
                "configuration": configuration,
                **({"notifications": notification_port} if notification_port is not None else {}),
            }
            if declaration is DECLARATION else {}
        ),
    )
    dispatcher = getattr(bound.handler, "__self__", None)
    if not isinstance(dispatcher, ShellFamilyDispatcher):  # pragma: no cover - wiring invariant
        raise RuntimeError("Shell declaration did not bind a ShellFamilyDispatcher")
    return dispatcher.manager


def setup(
    agent: "BaseAgent",
    policy_file: str | None = None,
    yolo: bool = False,
    shell_kind: "ShellKind | str | None" = None,
    **unsupported: object,
) -> ShellManager:
    """Mount ordinary Shell with only its public capability configuration.

    The normal setup/manifest surface intentionally cannot choose a notification
    destination, async handoff, or job-state namespace. Detached daemon Shell
    composition is a separate private helper below.
    """
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise RuntimeError(
            "Shell capability configuration does not accept "
            f"{names}; detached Shell composition is private to "
            "DetachedDaemonExecutionHost"
        )
    from lingtai.adapters.tool_plugin_host import StaticConfigurationAdapter

    return _mount_declared_shell(
        agent,
        configuration=StaticConfigurationAdapter({
            "policy_file": policy_file,
            "yolo": yolo,
            "shell_kind": shell_kind,
        }),
    )


def _setup_detached_daemon_shell(agent: "BaseAgent", *, run_dir) -> tuple[ShellManager, object]:
    """Private detached-daemon composition; never reached by capability config."""
    from lingtai.adapters.tool_plugin_host import StaticConfigurationAdapter
    from lingtai.tools.daemon.run_dir import DaemonRunDir
    from lingtai.tools.daemon.shell_prompt_events import DaemonShellPromptEventAdapter

    if not isinstance(run_dir, DaemonRunDir):
        raise TypeError("detached Shell composition requires a DaemonRunDir")
    adapter = DaemonShellPromptEventAdapter(run_dir)
    binding = _DetachedDaemonShellBinding(run_dir.path / "shell-jobs")
    manager = _mount_declared_shell(
        agent,
        configuration=StaticConfigurationAdapter({
            "_detached_daemon_shell": binding,
        }),
        notification_port=adapter,
    )
    return manager, adapter
