"""Daemon capability (神識) — dispatch ephemeral subagents (分神).

Gives an agent the ability to split its consciousness into focused worker
fragments that operate in parallel on the same working directory.  Each
emanation is a disposable ChatSession with a curated tool surface — not an
agent.  Results are persisted in daemon run directories; every terminal outcome
(done / failed / cancelled / timeout) is surfaced exactly once via a compact
system notification, so the parent can dispatch and go idle without polling.

Usage:
    Agent(capabilities=["daemon"])
    Agent(capabilities={"daemon": {"manager_pool_size": 100}})
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import yaml
from lingtai.services import plugin_registry as _plugin_registry
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, NamedTuple


if TYPE_CHECKING:
    from lingtai.agent import Agent

from lingtai.kernel._fsutil import atomic_write_json, read_json
from lingtai.kernel.i18n import t as _t
from lingtai.kernel.llm.base import FunctionSchema, is_all_empty_response
from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration
from lingtai.kernel.loop_guard import LoopGuard
from lingtai.kernel.message import MSG_REQUEST, _make_message
from lingtai.kernel.notifications import (
    DAEMON_CHANNEL as DAEMON_NOTIFICATION_CHANNEL,
)
from lingtai.kernel.tool_executor import ToolExecutor
from lingtai.kernel.tool_result_artifacts import compact_oversized_history
from lingtai.kernel.meta_block import (
    attach_daemon_agent_meta,
    render_system_prompt_pressure_context,
)
from lingtai.kernel.token_counter import count_tokens
from lingtai.kernel.trace_redaction import redact_text
from .._manual import load_installed_manual
from ._tool_family import (
    CHECK_LAST_MAX as _FAMILY_CHECK_LAST_MAX,
    DEFAULT_MAX_TURNS as _FAMILY_DEFAULT_MAX_TURNS,
    LIST_DEFAULT_LAST as _FAMILY_LIST_DEFAULT_LAST,
    DAEMON_DECLARED_ACTIONS,
    DaemonFamilyDispatcher,
    build_schema as _family_build_schema,
    declared_input_schemas,
)
from ..tool_family.manual import MANUAL_INPUT_SCHEMA
from lingtai.adapters.posix.process_identity import (
    process_identity,
    process_identity_matches,
)
from .run_dir import DaemonRunDir
from . import dispatch_ledger
from .system_prompt import (
    DAEMON_SYSTEM_PROMPT_BUDGET_CHARS,
    build_daemon_system_prompt,
)
from .claude_interactive import ClaudeInteractiveError, run_claude_interactive
from .runtime import (
    kill_process_group as _runtime_kill_process_group,
    iter_stdout_with_deadline as _iter_stdout_with_deadline,
    mark_cancelled_or_timeout as _mark_cancelled_or_timeout,
    spawn_stderr_drainer as _spawn_stderr_drainer,
)
from .interactive_terminal import InteractiveTerminalPort
from .posix_process import PosixDaemonProcessPort
from .process_port import (
    DaemonProcessCommand,
    DaemonProcessExit,
    DaemonProcessHandle,
    DaemonProcessObservation,
    DaemonProcessPort,
    DaemonProcessTerminationScope,
)
from .posix_process import PosixDaemonProcessPort

PROVIDERS = {"providers": [], "default": "builtin"}


def _kill_process_group(proc, *, term_timeout: float = 5.0, kill_timeout: float = 3.0) -> None:
    """Reclaim a legacy Popen using its explicit ownership scope.

    Detached hosts still have a few legacy direct-Popen backend paths. Their
    children inherit the supervisor session, so route those paths through an
    exact-PID signal while retaining the runtime process-group helper for
    ordinary manager-owned private groups. The supervisor's exact-run reclaim
    remains the only detached operation that signals the inherited PGID.
    """
    scope = getattr(proc, "_lingtai_termination_scope", None)
    if scope is not DaemonProcessTerminationScope.INHERITED_SUPERVISOR_GROUP:
        return _runtime_kill_process_group(
            proc, term_timeout=term_timeout, kill_timeout=kill_timeout,
        )
    pid = getattr(proc, "pid", None)
    pgid = getattr(proc, "_lingtai_pgid", None)
    identity = getattr(proc, "_lingtai_process_identity", None)
    if (not isinstance(pid, int) or isinstance(pid, bool)
            or not isinstance(pgid, int) or isinstance(pgid, bool)
            or not isinstance(identity, str) or not identity):
        return
    if proc.poll() is not None:
        return
    try:
        if os.getpgid(pid) != pgid or not process_identity_matches(pid, identity):
            return
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        if proc.poll() is not None:
            return
        try:
            if os.getpgid(pid) != pgid or not process_identity_matches(pid, identity):
                return
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=kill_timeout)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            pass


# Default and author ceiling for per-emanation LLM tool-loop turns.
# Agents may request a smaller per-batch value via daemon(max_turns=...), but
# larger values are capped here.
DEFAULT_MAX_TURNS = 5000
# Per-agent daemon capability config, mirroring the sibling task_card
# capability's ``<workdir>/taskcard/taskcard.json`` pattern: this capability's
# own config lives at ``<workdir>/daemon/daemon.json``. It supports
# ``max_turns``, ``manager_pool_size``, and ``system_prompt_budget_chars``.
# Configured positive ``max_turns`` and ``system_prompt_budget_chars`` become
# the corresponding defaults when ``setup()`` omits explicit capability kwargs.
# Valid ``LINGTAI_DAEMON_MAX_TURNS`` and
# ``LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS`` values are final overrides at
# daemon-manager construction. ``manager_pool_size`` caps concurrent central-manager
# execution workers. A missing file, a malformed/undecodable file, or an
# invalid field falls back independently, so agents without a config file
# behave exactly as before.
_DAEMON_CONFIG_DIR = "daemon"
_CONFIG_FILENAME = "daemon.json"


class _Config(NamedTuple):
    """Resolved agent-wide daemon defaults for new emanations."""

    max_turns: int
    manager_pool_size: int
    system_prompt_budget_chars: int


_BUILTIN_CONFIG = _Config(DEFAULT_MAX_TURNS, 100, DAEMON_SYSTEM_PROMPT_BUDGET_CHARS)


def _config_max_turns(value: Any) -> int:
    """Coerce a configured ``max_turns``; any non-positive-integer falls back."""
    return value if type(value) is int and value > 0 else DEFAULT_MAX_TURNS


def _config_nonnegative_int(value: Any, default: int) -> int:
    """Coerce a non-negative integer config field with a safe fallback."""
    return value if type(value) is int and value >= 0 else default


def _config_positive_int(value: Any, default: int) -> int:
    """Coerce a positive integer config field with a safe fallback."""
    return value if type(value) is int and value > 0 else default


def _load_config(agent_working_dir: str | os.PathLike[str]) -> _Config:
    """Load this agent's persisted daemon defaults (``<workdir>/daemon/daemon.json``).

    Mirrors ``task_card``'s config contract: a file that is missing, malformed,
    undecodable, or the wrong top-level type falls back to the built-in
    defaults (``DEFAULT_MAX_TURNS``) without raising, so a broken config file
    never breaks agent startup. Each field falls back independently.
    """
    config_path = Path(agent_working_dir) / _DAEMON_CONFIG_DIR / _CONFIG_FILENAME
    if not config_path.is_file():
        return _BUILTIN_CONFIG
    try:
        data = read_json(config_path, expect=dict)
    except (OSError, ValueError, TypeError):
        return _BUILTIN_CONFIG
    return _Config(
        _config_max_turns(data.get("max_turns")),
        _config_nonnegative_int(
            data.get("manager_pool_size"), _BUILTIN_CONFIG.manager_pool_size
        ),
        _config_positive_int(
            data.get("system_prompt_budget_chars"),
            _BUILTIN_CONFIG.system_prompt_budget_chars,
        ),
    )


DAEMON_CONTEXT_COUNTDOWN_ROUNDS = 9
DAEMON_CONTEXT_WARNING = (
    "Daemon context is at or above 90%. {remaining} proactive round(s) remain "
    "before runtime mechanical compact; call compact(action=\"run\", "
    "_reason=\"...\") now to compact with your own handoff."
)
# Kept as a named alias for the visible countdown carrier; both fields carry
# the same self-contained per-round warning sentence.
DAEMON_CONTEXT_COUNTDOWN_WARNING = DAEMON_CONTEXT_WARNING
DAEMON_MECHANICAL_COMPACT_RECOVERY = (
    "The runtime mechanically compacted your provider context after the 90% "
    "nine-round countdown expired. Recovery is required. Before continuing, "
    "recover deliberately: "
    "re-read the complete task above; inspect the preserved latest tool-call "
    "and tool-result pair and the durable run state/history/event paths; then "
    "resume from that verified state. Do not assume erased context or repeat "
    "side effects without verification."
)
_DAEMON_MISSING_COMPLETION_ERROR = "missing completion MCP finish signal"
_DAEMON_EMPTY_RESPONSE_ERROR = "daemon empty-response recovery exhausted"
_TRANSIENT_EMPTY_RESPONSE_RETRY_LIMIT = 3


def _wait_recovery_backoff(
    cancel_event: threading.Event,
    timeout_event: threading.Event | None,
    seconds: float,
) -> bool:
    """Wait interruptibly for an empty-response transient retry."""
    deadline = time.monotonic() + seconds
    while True:
        if cancel_event.is_set() or (timeout_event is not None and timeout_event.is_set()):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        cancel_event.wait(min(remaining, 0.1))


_DAEMON_SKILL_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n", re.DOTALL)


def _build_daemon_apriori_summarizer_fn(service, run_dir, *, provider, model, endpoint=None):
    """Build the daemon-local a-priori summary gateway.

    The closure deliberately owns the effective daemon service and run directory for this run:
    it creates an untracked, no-tools session on that same provider/model and
    accounts usage through the daemon dual-ledger path. It is inert unless the
    ToolExecutor receives summary=true.
    """
    if service is None or not callable(getattr(service, "create_session", None)):
        return None

    def _summarize(system_prompt, user_prompt, tool_name, tool_call_id=None):
        session = service.create_session(
            system_prompt=system_prompt,
            tools=None,
            model=model,
            tracked=False,
            provider=provider,
        )
        response = session.send(user_prompt)
        usage = getattr(response, "usage", None)
        if usage is not None:
            run_dir.append_tokens(
                input=usage.input_tokens,
                output=usage.output_tokens,
                thinking=usage.thinking_tokens,
                cached=usage.cached_tokens,
                model=model,
                endpoint=endpoint,
                usage_extra=getattr(usage, "extra", None),
            )
        return getattr(response, "text", "") or ""

    return _summarize


class _DaemonMetaState:
    """Daemon-local projector inputs for the canonical ``agent_meta`` envelope.

    This state is intentionally scoped to one emanation and never reads the
    parent agent's session, notification store, or token ledger.  The output
    uses the canonical token/context field vocabulary; ``attach_daemon_agent_meta``
    owns the envelope and latest-carrier semantics.

    ``context.system_prompt`` mirrors the main-agent rendered-system-prompt-size
    warning (``meta_block.render_system_prompt_pressure_context``) but scoped
    entirely to this daemon's own local prompt/window: never the parent's.
    """

    def __init__(
        self,
        em_id: str,
        run_id: str,
        *,
        max_turns: int,
        context_window: int = 0,
        system_prompt: str | None = None,
    ):
        self.em_id = em_id
        self.run_id = run_id
        self.max_turns = max_turns
        self.context_window = context_window if isinstance(context_window, int) and context_window > 0 else 0
        # Counted once here (never recomputed in snapshot()) via the same
        # kernel count_tokens() the main-agent path uses, over this daemon's
        # own already-built local system_prompt text.
        self.system_prompt_tokens = count_tokens(system_prompt) if system_prompt else 0
        self.rounds = 0
        self.tool_calls_this_round = 0
        self.tool_calls_total = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.last_thinking_tokens = 0
        self.last_cached_tokens = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_thinking_tokens = 0
        self.total_cached_tokens = 0
        self.warning_active = False
        self.compact_countdown: int | None = None
        self.compact_due = False

    @staticmethod
    def _count(value) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def note_response(self, response, session) -> None:
        self.rounds += 1
        usage = getattr(response, "usage", None)
        self.last_input_tokens = self._count(getattr(usage, "input_tokens", 0))
        self.last_output_tokens = self._count(getattr(usage, "output_tokens", 0))
        self.last_thinking_tokens = self._count(getattr(usage, "thinking_tokens", 0))
        self.last_cached_tokens = self._count(getattr(usage, "cached_tokens", 0))
        if usage is not None:
            self.total_input_tokens += self.last_input_tokens
            self.total_output_tokens += self.last_output_tokens
            self.total_thinking_tokens += self.last_thinking_tokens
            self.total_cached_tokens += self.last_cached_tokens
        window = self._session_context_window(session)
        if window > 0:
            self.context_window = window
        context_tokens = self._context_tokens(session)
        context_high = (
            self.context_window > 0
            and context_tokens * 10 >= self.context_window * 9
        )
        self.warning_active = context_high
        if context_high:
            if self.compact_countdown is None:
                self.compact_countdown = DAEMON_CONTEXT_COUNTDOWN_ROUNDS
            elif not self.compact_due:
                if self.compact_countdown > 1:
                    self.compact_countdown -= 1
                else:
                    # Keep the final warning visible for the response that sees
                    # it.  The following high response is the force boundary,
                    # giving that response one ordinary chance to compact.
                    self.compact_due = True
        else:
            self.compact_countdown = None
            self.compact_due = False

    def note_compact_reset(self, session) -> None:
        """Project the surviving compact result from the fresh context."""
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.last_thinking_tokens = 0
        self.last_cached_tokens = 0
        window = self._session_context_window(session)
        if window > 0:
            self.context_window = window
        self.warning_active = False
        self.compact_countdown = None
        self.compact_due = False

    def note_tool_batch(self, tool_calls) -> None:
        self.tool_calls_this_round = len(tool_calls or [])
        self.tool_calls_total += self.tool_calls_this_round

    def _session_context_window(self, session) -> int:
        try:
            value = session.context_window()
        except Exception:
            value = 0
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return 0

    def _context_tokens(self, session) -> int:
        if self.last_input_tokens > 0:
            return self.last_input_tokens
        interface = getattr(session, "interface", None)
        estimate = getattr(interface, "estimate_context_tokens", None)
        if callable(estimate):
            try:
                value = estimate()
            except Exception:
                value = 0
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0

    def snapshot(self, session) -> dict:
        context_tokens = self._context_tokens(session)
        context = {"context_tokens": context_tokens}
        if self.context_window > 0:
            context["context_window"] = self.context_window
            context["context_usage"] = round(context_tokens / self.context_window, 5)
            # This daemon's own resolved window is the ruler — never the
            # parent's. Unknown/zero prompt tokens or window omit (never
            # invented); the shared pure renderer owns the strict effective-
            # threshold decision and reads the environment for this snapshot.
            system_prompt_warning = render_system_prompt_pressure_context(
                self.system_prompt_tokens, self.context_window
            )
            if system_prompt_warning:
                context["system_prompt"] = system_prompt_warning
        if self.warning_active:
            remaining = self.compact_countdown or DAEMON_CONTEXT_COUNTDOWN_ROUNDS
            context["warning"] = DAEMON_CONTEXT_WARNING.format(remaining=remaining)
        if self.compact_countdown is not None:
            context["compact_countdown"] = self.compact_countdown
            context["compact_countdown_warning"] = DAEMON_CONTEXT_COUNTDOWN_WARNING.format(
                remaining=self.compact_countdown,
            )

        current_call = {
            "input": self.last_input_tokens,
            "cache_miss": max(self.last_input_tokens - self.last_cached_tokens, 0),
            "cache_rate": round(
                min(self.last_cached_tokens / self.last_input_tokens, 1.0), 5
            ) if self.last_input_tokens > 0 else 0.0,
            "output": self.last_output_tokens,
            "thinking": self.last_thinking_tokens,
        }
        session_usage = {
            "api_calls": self.rounds,
            "input_tokens": self.total_input_tokens,
            "cached_tokens": self.total_cached_tokens,
            "session_cache_rate": round(
                min(self.total_cached_tokens / self.total_input_tokens, 1.0), 5
            ) if self.total_input_tokens > 0 else 0.0,
            "avg_input_tokens_per_api_call": int(round(self.total_input_tokens / self.rounds))
            if self.rounds > 0 else 0,
            "cache_miss_tokens": max(self.total_input_tokens - self.total_cached_tokens, 0),
        }
        if context_tokens or self.context_window > 0:
            session_usage.update(context)
        return {
            "daemon": {
                "id": self.em_id,
                "run_id": self.run_id,
                "backend": "lingtai",
                "round": self.rounds,
                "max_turns": self.max_turns,
                "tool_calls_this_round": self.tool_calls_this_round,
                "tool_calls_total": self.tool_calls_total,
            },
            "token_usage": {
                "current_call": current_call,
                "session": session_usage,
            },
            "context": context,
        }


_DAEMON_COMPACT_MANUAL_PROCEDURES = (
    "Read-only manual: this action never compacts or changes daemon state.",
    f"When context usage reaches 90% or more, the daemon receives: {DAEMON_CONTEXT_WARNING.format(remaining=DAEMON_CONTEXT_COUNTDOWN_ROUNDS)}",
    f"A deterministic {DAEMON_CONTEXT_COUNTDOWN_ROUNDS}-round countdown is visible in _meta.agent_meta.agent_state.context.compact_countdown; each value carries the same self-contained warning sentence.",
    "Prepare a complete self-contained handoff.",
    "Call compact with action='run' as the sole tool call in its assistant batch; include only action and _reason, and make _reason the handoff and resume instruction.",
    "If the countdown expires first, the runtime mechanically compacts before the next provider call and sends an explicit recovery instruction; re-read the task, inspect the preserved latest tool-call/result pair and durable run artifacts, verify state, then continue.",
    "After the successful non-terminal reset, resume from that surviving call/result pair; the run, state, history, and event paths in the result remain available.",
)


DAEMON_ASYNC_HANDOFF = (
    "While waiting, go idle or call system(action='sleep'); the terminal result "
    "will arrive and wake you as a notification; read daemon-manual and "
    "notification-manual for details. For large concurrent batches, strongly "
    "recommend notification(action='delay') on the daemon channel to reduce wake "
    "frequency; delay masks attention only, never daemon truth. If Telegram is "
    "connected and a Task Card is available for the current turn, use it to report "
    "progress; call `telegram(action='manual')` and follow its `Programmable Task "
    "Card` section for details."
)
# A fleet (two or more daemons), or a single one whose caller explicitly asked
# for a long ceiling, is work a human wants to follow. The default 3600s
# ceiling does not count: it says nothing about how long the batch will run,
# and nudging on it would fire for every single quick daemon.
DAEMON_CARD_NUDGE_MIN_TASKS = 2
DAEMON_CARD_NUDGE_MIN_TIMEOUT_S = 900.0
DAEMON_CARD_NUDGE = (
    " You dispatched {count} daemon(s) with no active task_card watch — consider "
    "starting one (task_card action='start') so a human can follow progress."
)


_DAEMON_COMMON_MCP_NAME = "daemon_common"
_DAEMON_EMAIL_MCP_NAME = "email"
# Tool names satisfied by a LingTai-auto-mounted, task-scoped MCP server
# (``_with_daemon_email_mcp``) rather than by the parent's already-connected
# capability/MCP surface. Pre-flight tool-surface validation
# (``_build_tool_surface``) runs against a placeholder empty ``mcp_surface``
# — the owning detached supervisor connects task MCP servers for real only
# after pre-flight passes — so a name from this set must be tolerated as
# "available" there even though it is not literally present in any schema
# dict yet. It costs nothing at real dispatch time: by then the connected
# server's schema/handler are already in ``mcp_schemas``/``mcp_handlers``,
# so this only ever widens ``available``, never ``tool_names`` — a daemon
# that never requested ``email`` still never gets it.
_DAEMON_AUTO_MCP_TOOL_NAMES = frozenset({_DAEMON_EMAIL_MCP_NAME})
_DAEMON_COMPLETION_FILE = "daemon_completion.json"
_DAEMON_CLAUDE_MCP_CONFIG_FILE = "claude-mcp-config.json"
_DAEMON_COMPLETION_STATUSES = {"done", "failed", "incomplete"}
_SOURCE_ROOT = Path(__file__).resolve().parents[3]

#: Optional per-task input files (``tasks[].task_files``): the parent resolves
#: every path under the agent working directory, validates UTF-8 text and the
#: practical limits below, and snapshots the bytes content-addressed into an
#: immutable read-only input store BEFORE any run-dir creation or scheduling.
#: Workers receive only a compact manifest pointing at the snapshot paths —
#: never the file contents and never the mutable original paths — so retry and
#: relaunch never need the original file. The store lives under the daemons
#: root as ``daemons/_task_files/``; the leading underscore is the run-dir
#: scan's marker that the store is internal and never a run (see
#: ``_looks_like_daemon_run_dir``).
_TASK_FILES_STORE_DIR_NAME = "_task_files"
_TASK_FILES_MANIFEST_VERSION = 1
#: Per-task cap on task_files entries; bounds the compact manifest/prompt rows.
TASK_FILES_MAX_PER_TASK = 50
#: Per-file byte cap for one task input file; bounds the immutable blob store.
TASK_FILE_MAX_BYTES = 1_000_000
#: Cap on a task_files label/role string; keeps the prompt manifest compact.
_TASK_FILES_ANNOTATION_MAX_CHARS = 200
# Tools emanations can never use (no recursion, no spawning, no identity mutation)
EMANATION_BLACKLIST = {
    "daemon",
    "avatar",
    # The context department: it owns ``molt``, the most irreversible operation
    # an agent can perform, plus the summarize/rebuild pair that rewrites the
    # provider context. This is the same boundary the former ``psyche`` root
    # carried; it follows the capability, not the old name.
    "context",
    # The one public root for the four durable domains
    # (``pad + lingtai + knowledge + skills = psyche``). It replaced the four
    # former model-visible roots, which carried exactly the identity/prompt
    # authority the OLD ``psyche`` family was blacklisted for, so the boundary
    # follows the capability onto its successor of the same name. ``knowledge``
    # and ``skills`` are retained below as *capability* names: they register no
    # tool but must still be excluded from the borrowable host-tool floor.
    "psyche",
    "skills",
    "knowledge",
}


def _parent_host_tool_floor() -> frozenset[str]:
    """The always-on host tools a preset emanation may borrow from the parent.

    A preset selects the child LLM + provider-specific capabilities; it does
    NOT re-declare the parent's always-on ``CORE_DEFAULTS`` host floor, because
    the TUI preset wizard only writes overrides/opt-ins into
    ``manifest.capabilities``. So those floor tools must still resolve from the
    parent surface under a preset. But the floor is exactly the host
    primitives — ``shell`` and ``file`` (the one family whose actions are
    read/write/edit/glob/grep) — and nothing more: optional/provider parent
    tools (e.g. ``vision``, ``web_search``) must NOT silently fall back to the
    parent when a preset omits or fails them.

    This is an explicit contract allowlist. Growing ``CORE_DEFAULTS`` must not
    silently widen what a preset child may borrow from its parent.
    The result is exactly {shell, file}.
    """
    return frozenset({"shell", "file"})


# Env vars that override Claude Code's normal first-party OAuth credentials.
# LingTai loads ``.env`` from ``~/.lingtai-tui/`` early, so auth intended for
# another LLM adapter can leak into spawned ``claude`` subprocesses.
# ``ANTHROPIC_*`` keys force API billing (GH #107); a stale
# ``CLAUDE_CODE_OAUTH_TOKEN`` can also beat a refreshed
# ``~/.claude/.credentials.json`` and surface as a false weekly-limit error
# (GH Lingtai-AI/lingtai#189). Strip these for Claude Code subprocesses
# only: print-mode Claude (claude-p/claude-code) and interactive Claude
# (claude/claude-interactive). Other backends (codex, lingtai, opencode,
# mimocode, qwen-code, oh-my-pi, cursor, kimicode) are unaffected.
_CLAUDE_CODE_STRIP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _claude_code_env() -> dict[str, str]:
    """Return os.environ minus auth vars that override Claude Code's OAuth."""
    env = os.environ.copy()
    for key in _CLAUDE_CODE_STRIP_ENV:
        env.pop(key, None)
    return env


def _normalize_claude_usage(usage: dict | None) -> dict | None:
    """Normalize a Claude Code stream-json ``usage`` block to UI totals.

    Claude Code's final ``result`` event carries a ``usage`` block like::

        {"input_tokens": 6950, "cache_creation_input_tokens": 3068,
         "cache_read_input_tokens": 15621, "output_tokens": 4, ...}

    Returns ``{"input", "output", "cached", "thinking"}`` with::

        cached = cache_read_input_tokens + cache_creation_input_tokens

    ``thinking`` is 0 — Claude Code does not surface a separate thinking-token
    count in this block. The primary ``input_tokens`` and ``output_tokens``
    fields are required; cache-read and cache-creation counts are optional and
    default to zero. Every consumed field must be a non-negative integer
    (booleans are not token counts, matching the Codex/Cursor normalizers);
    a missing primary or malformed field invalidates the whole event. Returns
    ``None`` if ``usage`` is missing/not a dict, carries an invalid field, or
    carries no countable tokens, so callers can skip persistence cleanly.
    """
    if not isinstance(usage, dict):
        return None

    def _nonnegative_int(value) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    cache_read = _nonnegative_int(usage.get("cache_read_input_tokens", 0))
    cache_creation = _nonnegative_int(usage.get("cache_creation_input_tokens", 0))
    if None in (input_tokens, output_tokens, cache_read, cache_creation):
        return None

    cached = cache_read + cache_creation
    thinking = 0
    if not (input_tokens or output_tokens or cached or thinking):
        return None
    return {"input": input_tokens, "output": output_tokens,
            "cached": cached, "thinking": thinking}


def _normalize_codex_usage(usage: dict | None) -> dict | None:
    """Normalize a Codex ``turn.completed`` usage block for UI totals.

    The Codex CLI event contract reports ``input_tokens`` as the total input
    count, including ``cached_input_tokens``.  ``daemon.json.cli_tokens.input``
    is the disjoint (non-cached) input count, so subtract the cached portion
    and clamp at zero rather than exposing a negative number when a malformed
    source payload overstates its cache count.  Only fields present in the
    source ``TokenUsage`` contract are consumed; Codex does not provide a
    separately proven thinking/reasoning count in this event.

    Invalid, missing, negative, or all-zero usage is suppressed.  The caller
    passes the returned ``input``/``cached``/``output`` values to
    :meth:`DaemonRunDir.record_cli_tokens`, which increments ``calls`` once.
    """
    if not isinstance(usage, dict):
        return None

    def _nonnegative_int(value) -> int | None:
        # bool is an int subclass, but it is not a token count.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    total_input = _nonnegative_int(usage.get("input_tokens"))
    cached_input = _nonnegative_int(usage.get("cached_input_tokens"))
    output = _nonnegative_int(usage.get("output_tokens"))
    if total_input is None or cached_input is None or output is None:
        return None

    input_tokens = max(total_input - cached_input, 0)
    if not (input_tokens or cached_input or output):
        return None
    return {
        "input": input_tokens,
        "cached": cached_input,
        "output": output,
    }


def _normalize_cursor_usage(event: dict | None) -> dict | None:
    """Normalize Cursor 2026.05.28 ``result.usage`` to UI-only totals.

    The installed ``agent-cli@2026.05.28-a70ca7c`` bundle emits a terminal
    ``type=result`` / ``subtype=success`` event whose ``usage.inputTokens`` is
    already net of cache reads and writes.  Keep that value direct: subtracting
    either cache field again would undercount input.  Cursor emits no thinking
    or provider field in this source-pinned event contract.

    Every field is required to be a non-negative integer (booleans are not
    token counts).  Invalid and all-zero events return ``None`` so callers do
    not persist misleading UI usage or raw-event noise.
    """
    if not isinstance(event, dict):
        return None
    if (
        event.get("type") != "result"
        or event.get("subtype") != "success"
        or event.get("is_error") is not False
    ):
        return None

    raw_usage = event.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    keys = (
        "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens",
    )
    values = [raw_usage.get(key) for key in keys]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        return None

    input_tokens, output_tokens, cache_read, cache_write = values
    cached = cache_read + cache_write
    if not (input_tokens or output_tokens or cached):
        return None
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cached": cached,
        "thinking": 0,
    }


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(v) for v in values) + "]"


def _toml_inline_table(values: dict[str, str]) -> str:
    return "{ " + ", ".join(
        f"{key} = {_toml_string(value)}"
        for key, value in sorted(values.items())
    ) + " }"


def _codex_mcp_argv(registrations: list[dict]) -> list[str]:
    argv: list[str] = []
    for reg in registrations:
        if reg.get("transport", reg.get("type", "stdio")) != "stdio":
            continue
        prefix = f"mcp_servers.{reg['name']}"
        argv.extend(["-c", f"{prefix}.command={_toml_string(reg['command'])}"])
        argv.extend(["-c", f"{prefix}.args={_toml_array(list(reg.get('args') or []))}"])
        if reg.get("env"):
            argv.extend(["-c", f"{prefix}.env={_toml_inline_table(dict(reg['env']))}"])
    return argv


def _opencode_mcp_env(registrations: list[dict]) -> dict[str, str]:
    mcp: dict[str, dict] = {}
    for reg in registrations:
        if reg.get("transport", reg.get("type", "stdio")) != "stdio":
            continue
        mcp[reg["name"]] = {
            "type": "local",
            "command": [reg["command"], *list(reg.get("args") or [])],
            "enabled": True,
            "environment": dict(reg.get("env") or {}),
        }
    if not mcp:
        return {}
    return {"OPENCODE_CONFIG_CONTENT": json.dumps({"mcp": mcp}, ensure_ascii=False)}


def _write_qwen_mcp_settings(run_dir: DaemonRunDir, registrations: list[dict]) -> Path:
    servers: dict[str, dict] = {}
    for reg in registrations:
        if reg.get("transport", reg.get("type", "stdio")) != "stdio":
            continue
        servers[reg["name"]] = {
            "command": reg["command"],
            "args": list(reg.get("args") or []),
            "env": dict(reg.get("env") or {}),
        }
    path = run_dir.path / "qwen-daemon-settings.json"
    atomic_write_json(path, {"mcpServers": servers}, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _kimicode_home_dir(run_dir: DaemonRunDir) -> Path:
    return run_dir.path / "kimi-code-home"


def _write_kimicode_mcp_config(
    run_dir: DaemonRunDir,
    registrations: list[dict],
) -> Path:
    servers: dict[str, dict] = {}
    for reg in registrations:
        transport = reg.get("transport", reg.get("type", "stdio"))
        if transport == "stdio":
            server = {
                "transport": "stdio",
                "command": reg["command"],
                "args": list(reg.get("args") or []),
            }
            if reg.get("env"):
                server["env"] = dict(reg["env"])
            servers[reg["name"]] = server
        elif transport == "http":
            server = {
                "transport": "http",
                "url": reg["url"],
            }
            if reg.get("headers"):
                server["headers"] = dict(reg["headers"])
            servers[reg["name"]] = server
    kimi_home = _kimicode_home_dir(run_dir)
    kimi_home.mkdir(parents=True, exist_ok=True)
    path = kimi_home / "mcp.json"
    atomic_write_json(path, {"mcpServers": servers}, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)
        kimi_home.chmod(0o700)
    except OSError:
        pass
    return path


def _cli_backend_loads_common_mcp(backend: str) -> bool:
    return backend in {
        "claude-p",
        "claude-code",
        "codex",
        "opencode",
        "qwen-code",
        "kimicode",
    }


def _dev_pythonpath_with_source_root() -> str:
    """Return a PYTHONPATH that lets module-launched MCPs see this checkout.

    Daemon MCP subprocesses run under the parent's active interpreter. In dev
    and tests that interpreter may be an external venv whose site-packages does
    not contain the dirty worktree, so the module command needs the checkout's
    ``src`` directory in process env. This is still a direct module launch, not
    a model-visible shell helper.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_SOURCE_ROOT)]
    parts.extend(p for p in existing.split(os.pathsep) if p)
    return os.pathsep.join(dict.fromkeys(parts))


# Safe CLI option key: letters/digits with '-' or '_' separators. No leading
# '-' (the helper adds '--' itself). No spaces, no shell metachars — argv is
# passed as a list to subprocess, but we still refuse anything that doesn't
# look like a real CLI flag to keep error messages early and obvious.
_BACKEND_OPTION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# The single reserved backend_options key. It is a nested object of
# environment variables to inject into the spawned CLI subprocess (e.g.
# ``CLAUDE_CONFIG_DIR`` to pick a Claude profile) rather than an argv flag,
# so it is carved out before flag conversion and never reaches argv.
_BACKEND_OPTION_ENV_KEY = "env"
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _backend_options_env(value) -> dict[str, str]:
    """Validate the reserved ``backend_options.env`` object.

    Names must look like real environment variables
    (``[A-Za-z_][A-Za-z0-9_]*``) and values must be strings — an int/bool
    would silently stringify differently across platforms, and a nested
    object has no environment representation at all.
    """
    if not isinstance(value, dict):
        raise ValueError(
            f"backend_options[{_BACKEND_OPTION_ENV_KEY!r}] must be a JSON object "
            f"of environment variable name -> string value, got "
            f"{type(value).__name__}"
        )
    env: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not _ENV_VAR_NAME_RE.match(name):
            raise ValueError(
                f"backend_options[{_BACKEND_OPTION_ENV_KEY!r}] key {name!r} is not a "
                "valid environment variable name ([A-Za-z_][A-Za-z0-9_]*)"
            )
        if not isinstance(item, str):
            raise ValueError(
                f"backend_options[{_BACKEND_OPTION_ENV_KEY!r}][{name!r}] must be a "
                f"string (got {type(item).__name__})"
            )
        env[name] = item
    return env


def _backend_options_to_argv_and_env(
    options: dict | None,
) -> tuple[list[str], dict[str, str]]:
    """Split a free-form backend_options dict into argv tokens + env overlay.

    The reserved key ``env`` is carved out first: its value must be a JSON
    object mapping environment variable names to string values, and it is
    returned as the second element instead of being converted to a flag. It
    is never emitted as argv. Every other key follows the flag rules below.

    Conversion rules:
      - key must match ``[A-Za-z0-9][A-Za-z0-9_-]*`` (no leading '-', no
        empty). Underscores in the key are converted to dashes for the
        emitted flag. Long flags only: ``--<flag>``.
      - value ``True`` → ``["--flag"]`` (presence flag, no argument).
      - value ``False`` or ``None`` → omitted entirely.
      - value ``str`` / ``int`` / ``float`` → ``["--flag", str(value)]``.
      - value ``list``/``tuple`` of scalars → repeated
        ``["--flag", v1, "--flag", v2, ...]``.
      - Nested dicts / nested lists / objects of unsupported type → raise
        ``ValueError`` with a clear message.

    Returns argv tokens ready to be appended to a subprocess command list
    (never a shell string) plus the env overlay. Empty / falsy input returns
    ``([], {})``.
    """
    if not options:
        return [], {}
    if not isinstance(options, dict):
        raise ValueError(
            f"backend_options must be a JSON object, got {type(options).__name__}"
        )

    argv: list[str] = []
    env: dict[str, str] = {}
    for key, value in options.items():
        if key == _BACKEND_OPTION_ENV_KEY:
            env = _backend_options_env(value)
            continue
        if not isinstance(key, str) or not _BACKEND_OPTION_KEY_RE.match(key):
            raise ValueError(
                f"backend_options key {key!r} is not a safe CLI flag name "
                "(letters/digits with '-' or '_' separators, no leading '-')"
            )
        flag = "--" + key.replace("_", "-")

        if value is False or value is None:
            continue
        if value is True:
            argv.append(flag)
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            argv.extend([flag, str(value)])
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                    raise ValueError(
                        f"backend_options[{key!r}] list items must be string/int/float scalars "
                        f"(got {type(item).__name__})"
                    )
                argv.extend([flag, str(item)])
            continue
        raise ValueError(
            f"backend_options[{key!r}] has unsupported value type "
            f"{type(value).__name__}; expected bool/str/int/float/list of scalars/null"
        )
    return argv, env


def _backend_options_to_argv(options: dict | None) -> list[str]:
    """Argv-only view of :func:`_backend_options_to_argv_and_env`.

    The reserved ``env`` key is still validated here; it simply never
    contributes an argv token.
    """
    return _backend_options_to_argv_and_env(options)[0]


_CLAUDE_COMMON_RESERVED_BACKEND_FLAGS = {
    "--settings",
    "--print",
    "--output-format",
    "--mcp-config",
    "--strict-mcp-config",
}
_CLAUDE_INTERACTIVE_RESERVED_BACKEND_FLAGS = {
    "--append-system-prompt",
    "--append-system-prompt-file",
}

# OpenCode-family (opencode, mimocode) own the run output format so daemon
# event parsing keeps working; callers must not override it via backend_options.
_OPENCODE_FAMILY_RESERVED_BACKEND_FLAGS = {
    "--format",
}

# MiMo Code additionally owns its session selectors: the daemon captures the
# session id from the run's own JSONL and drives resume through
# daemon(action='ask') (``mimo run --session <id> --format json``). Letting a
# caller pass ``--session``/``--continue``/``--fork`` in backend_options would
# hijack or fork the harness-owned session and silently break resume, so they
# are reserved MiMo-specifically (generic opencode session flags are untouched).
# Short aliases ``-s``/``-c`` cannot be emitted by backend_options — which only
# creates long ``--flag`` tokens — but are listed for defense-in-depth.
_MIMOCODE_RESERVED_BACKEND_FLAGS = _OPENCODE_FAMILY_RESERVED_BACKEND_FLAGS | {
    "--session",
    "-s",
    "--continue",
    "-c",
    "--fork",
}

# Qwen Code owns the prompt/headless/approval flags that drive LingTai's
# non-interactive harness; overriding them via backend_options would break
# headless capture or re-enable interactive prompting.
_QWEN_RESERVED_BACKEND_FLAGS = {
    "--prompt",
    "-p",
    "--yolo",
    "-y",
    "--approval-mode",
}

# Kimi Code owns the prompt/output-format flags that drive LingTai's
# non-interactive text-capture harness; overriding them via backend_options
# would break output capture. ``--yolo`` is forbidden because Kimi's official
# CLI refuses ``--prompt`` combined with ``--yolo``. Session/continue flags are
# reserved because daemon(action='ask') resume is not wired for Kimi yet (no
# verified stable session-id contract), so callers must not try to hijack a
# session through backend_options. (Short ``-p``/``-y``/``-S``/``-c`` cannot be
# emitted by backend_options, which only creates long ``--flag`` tokens, but
# they are listed for clarity and defense-in-depth.)
_KIMICODE_RESERVED_BACKEND_FLAGS = {
    "--prompt",
    "-p",
    "--output-format",
    "--yolo",
    "-y",
    "--session",
    "-S",
    "--continue",
    "-c",
}

# DeepSeek Harness (`dsh`) owns the launcher-level flags that drive LingTai's
# non-interactive headless harness: ``--profile headless`` locks the one-shot
# profile (overriding it could boot a different, interactive profile, or
# change the run shape entirely), while ``--dump-default-config`` /
# ``--dump-config`` and ``--version`` / ``--help`` are inspection-only exits
# that would fake a completed run without doing the task. ``--patch`` is
# deliberately NOT reserved: it is the official, documented way to overlay a
# config tree on a one-shot run (model/provider selection), the launcher
# accepts it before the app-argument boundary, and its trust level equals the
# ``backend_options.env`` overlay (which can already set ``DSH_PERMISSION_MODE``
# etc.). The headless profile's only app argument is the task text, so any
# non-launcher flag in ``backend_options`` ends up as an app argument after the
# boundary and is rejected by the headless app as a usage error. (``-V`` cannot
# be emitted by backend_options, which only creates long ``--flag`` tokens, but
# is listed for clarity.)
_DEEPSEEK_RESERVED_BACKEND_FLAGS = {
    "--profile",
    "--dump-default-config",
    "--dump-config",
    "--version",
    "--help",
}

# Oh-My-Pi owns the mode/headless/approval/session flags that drive LingTai's
# non-interactive JSON harness; overriding them via backend_options would break
# JSON event capture, re-enable interactive prompting, or hijack the session.
# ``--print`` is reserved because it is Oh-My-Pi's alternate print-mode switch
# (short form ``-p`` cannot be emitted by backend_options, which only creates
# long ``--flag`` tokens).
_OH_MY_PI_RESERVED_BACKEND_FLAGS = {
    "--mode",
    "--print",
    "--auto-approve",
    "--yolo",
    "--approval-mode",
    "--session",
    "--resume",
    "--continue",
    "--no-session",
    "--session-dir",
}

# Backend name aliases → canonical backend id. Kept tiny on purpose: only the
# obvious short forms callers reach for.
_BACKEND_ALIASES = {
    "mimo": "mimocode",
    "qwen": "qwen-code",
    "omp": "oh-my-pi",
    "kimi": "kimicode",
}

_QWEN_CODE_ASK_UNSUPPORTED_MESSAGE = (
    "qwen-code daemon backend does not support daemon(action='ask') yet; "
    "start a new qwen-code emanation instead."
)

_KIMICODE_ASK_UNSUPPORTED_MESSAGE = (
    "kimicode daemon backend does not support daemon(action='ask') yet; "
    "start a new kimicode emanation instead."
)

_DEEPSEEK_ASK_UNSUPPORTED_MESSAGE = (
    "deepseek daemon backend does not support daemon(action='ask') yet; "
    "start a new deepseek emanation instead."
)


@dataclass(frozen=True)
class _BackendSpec:
    id: str
    is_cli: bool
    runner_attr: str | None
    ask_handler_attr: str | None
    ask_unsupported_msg: str | None
    reserved_flags: frozenset[str]


@dataclass(frozen=True)
class _CliTaskContext:
    """Passive per-task CLI context; MCP entries are registration dicts only."""

    backend_argv: list[str]
    system_prompt: str | None
    skill_catalog: str | None
    mcp_catalog: str | None
    mcp_regs: list[dict]
    plugin_catalog: str | None = None
    # Reserved ``backend_options.env`` overlay for the spawned CLI subprocess.
    backend_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _DaemonCompletion:
    """Validated daemon completion signal written by the common MCP."""

    status: str | None
    summary: str | None = None
    reason: str | None = None
    artifacts: list[str] | None = None
    error: str | None = None

    @property
    def is_done(self) -> bool:
        return self.status == "done" and self.error is None


_CLAUDE_INTERACTIVE_RESERVED_FLAGS = frozenset(
    _CLAUDE_COMMON_RESERVED_BACKEND_FLAGS
    | _CLAUDE_INTERACTIVE_RESERVED_BACKEND_FLAGS
)

_BACKEND_SPECS: dict[str, _BackendSpec] = {
    "lingtai": _BackendSpec(
        id="lingtai",
        is_cli=False,
        runner_attr=None,
        ask_handler_attr=None,
        ask_unsupported_msg=None,
        reserved_flags=frozenset(),
    ),
    "claude": _BackendSpec(
        id="claude",
        is_cli=True,
        runner_attr="_run_claude_interactive_emanation",
        ask_handler_attr="_handle_ask_claude_interactive",
        ask_unsupported_msg=None,
        reserved_flags=_CLAUDE_INTERACTIVE_RESERVED_FLAGS,
    ),
    "claude-interactive": _BackendSpec(
        id="claude-interactive",
        is_cli=True,
        runner_attr="_run_claude_interactive_emanation",
        ask_handler_attr="_handle_ask_claude_interactive",
        ask_unsupported_msg=None,
        reserved_flags=_CLAUDE_INTERACTIVE_RESERVED_FLAGS,
    ),
    "claude-p": _BackendSpec(
        id="claude-p",
        is_cli=True,
        runner_attr="_run_claude_code_emanation",
        ask_handler_attr="_handle_ask_cli",
        ask_unsupported_msg=None,
        reserved_flags=frozenset(_CLAUDE_COMMON_RESERVED_BACKEND_FLAGS),
    ),
    "claude-code": _BackendSpec(
        id="claude-code",
        is_cli=True,
        runner_attr="_run_claude_code_emanation",
        ask_handler_attr="_handle_ask_cli",
        ask_unsupported_msg=None,
        reserved_flags=frozenset(_CLAUDE_COMMON_RESERVED_BACKEND_FLAGS),
    ),
    "codex": _BackendSpec(
        id="codex",
        is_cli=True,
        runner_attr="_run_codex_emanation",
        ask_handler_attr="_handle_ask_codex",
        ask_unsupported_msg=None,
        reserved_flags=frozenset(),
    ),
    "opencode": _BackendSpec(
        id="opencode",
        is_cli=True,
        runner_attr="_run_opencode_emanation",
        ask_handler_attr="_handle_ask_opencode",
        ask_unsupported_msg=None,
        reserved_flags=frozenset(_OPENCODE_FAMILY_RESERVED_BACKEND_FLAGS),
    ),
    "mimocode": _BackendSpec(
        id="mimocode",
        is_cli=True,
        runner_attr="_run_mimocode_emanation",
        ask_handler_attr="_handle_ask_mimocode",
        ask_unsupported_msg=None,
        reserved_flags=frozenset(_MIMOCODE_RESERVED_BACKEND_FLAGS),
    ),
    "qwen-code": _BackendSpec(
        id="qwen-code",
        is_cli=True,
        runner_attr="_run_qwen_code_emanation",
        ask_handler_attr=None,
        ask_unsupported_msg=_QWEN_CODE_ASK_UNSUPPORTED_MESSAGE,
        reserved_flags=frozenset(_QWEN_RESERVED_BACKEND_FLAGS),
    ),
    "oh-my-pi": _BackendSpec(
        id="oh-my-pi",
        is_cli=True,
        runner_attr="_run_oh_my_pi_emanation",
        ask_handler_attr="_handle_ask_oh_my_pi",
        ask_unsupported_msg=None,
        reserved_flags=frozenset(_OH_MY_PI_RESERVED_BACKEND_FLAGS),
    ),
    "kimicode": _BackendSpec(
        id="kimicode",
        is_cli=True,
        runner_attr="_run_kimicode_emanation",
        ask_handler_attr=None,
        ask_unsupported_msg=_KIMICODE_ASK_UNSUPPORTED_MESSAGE,
        reserved_flags=frozenset(_KIMICODE_RESERVED_BACKEND_FLAGS),
    ),
    "deepseek": _BackendSpec(
        id="deepseek",
        is_cli=True,
        runner_attr="_run_deepseek_emanation",
        ask_handler_attr=None,
        ask_unsupported_msg=_DEEPSEEK_ASK_UNSUPPORTED_MESSAGE,
        reserved_flags=frozenset(_DEEPSEEK_RESERVED_BACKEND_FLAGS),
    ),
    "cursor": _BackendSpec(
        id="cursor",
        is_cli=True,
        runner_attr="_run_cursor_emanation",
        ask_handler_attr="_handle_ask_cursor",
        ask_unsupported_msg=None,
        reserved_flags=frozenset(),
    ),
}

_BACKEND_SCHEMA_ENUM = [
    "lingtai",
    "claude-p",
    "claude-code",
    "codex",
    "opencode",
    "mimocode",
    "mimo",
    "qwen-code",
    "qwen",
    "oh-my-pi",
    "omp",
    "kimicode",
    "kimi",
    "cursor",
    "deepseek",
]

_HIDDEN_SCHEMA_BACKENDS = frozenset({"claude", "claude-interactive"})
assert all(name == spec.id for name, spec in _BACKEND_SPECS.items())
assert set(_BACKEND_ALIASES.values()).issubset(_BACKEND_SPECS)
assert set(_BACKEND_SCHEMA_ENUM) == (
    (set(_BACKEND_SPECS) - _HIDDEN_SCHEMA_BACKENDS)
    | set(_BACKEND_ALIASES)
)
# ``_tool_family`` restates ``DEFAULT_MAX_TURNS`` as its own literal because
# importing it from this module would be circular (this module imports
# ``_tool_family``). Prove at import time that ``emanate``'s child schema still
# advertises exactly the ceiling the engine enforces. The matching
# ``CHECK_LAST_MAX`` assertion lives just after ``DaemonManager``, which owns
# ``_CHECK_LAST_MAX`` and is not defined yet here.
assert _FAMILY_DEFAULT_MAX_TURNS == DEFAULT_MAX_TURNS


def _normalize_backend(backend: str | None) -> str:
    """Map a caller-supplied backend (incl. aliases) to its canonical id."""
    if not backend:
        return "lingtai"
    return _BACKEND_ALIASES.get(backend, backend)


def _backend_spec(backend: str | None) -> _BackendSpec | None:
    """Return the runtime backend spec for a stored backend id, if known."""
    if not backend:
        return None
    return _BACKEND_SPECS.get(backend)


def _validate_claude_backend_argv(backend: str, argv: list[str]) -> None:
    """Refuse user flags that would override a daemon backend's own harness.

    ``backend_options`` is a pass-through for CLI-specific flags, but several
    daemon backends own their execution mode and must not let callers override
    it (doing so would silently break daemon progress/result extraction):

      * Claude print-mode owns ``--print`` / ``--output-format stream-json``;
        interactive mode also owns ``--settings`` hooks + managed system prompt.
      * OpenCode-family (``opencode``, ``mimocode``) own ``--format`` (JSON);
        ``mimocode`` additionally reserves its session selectors
        (``--session``/``-s``, ``--continue``/``-c``, ``--fork``) so a caller
        cannot hijack the harness-owned MiMo session/resume.
      * Qwen Code owns ``--prompt`` / ``--yolo`` / ``--approval-mode``.
      * Oh-My-Pi owns ``--mode`` / approval-yolo / session flags.

    Despite the historical name, this validator now covers all CLI backends.
    """
    spec = _backend_spec(backend)
    if spec is None or not spec.reserved_flags:
        return
    for token in argv:
        if token in spec.reserved_flags:
            raise ValueError(f"{token} is reserved by the {backend} daemon backend")



class _ToolCollector:
    """Legacy sandbox collector retained for detached and CLI host facades.

    Official Agent boot uses ``DaemonRuntimePort.setup_preset_capability``;
    detached and read-only host facades still compose established capability
    setup against this private forwarding collector without mounting tools on a
    live Agent.
    """

    def __init__(self, parent):
        self._parent = parent
        self.schemas: dict = {}
        self.handlers: dict = {}
        # The collector owns the surface it builds, so official claims and bound
        # results stay local too: preset/detached composition must never mutate
        # the parent Agent's live namespace.
        self._official_tool_plugins: dict[str, Any] = {}
        self._official_tool_declarations: dict[str, Any] = {}
        self._official_tool_bindings: dict[str, Any] = {}

    @property
    def working_dir(self) -> Path:
        return Path(self._parent._working_dir)

    def update_system_prompt(self, *_args, **_kwargs) -> None:
        raise RuntimeError("detached tool collector has no system-prompt sections")

    @property
    def official_tool_plugins(self):
        return MappingProxyType(self._official_tool_plugins)

    def _authorize_official_tool_declaration(self, declaration) -> None:
        from lingtai.kernel.base_agent import BaseAgent

        BaseAgent._authorize_official_tool_declaration(self, declaration)

    def _record_official_tool_binding(self, declaration, plugin) -> None:
        from lingtai.kernel.base_agent import BaseAgent

        BaseAgent._record_official_tool_binding(self, declaration, plugin)

    def _claim_official_tool(self, transaction) -> None:
        from lingtai.kernel.base_agent import BaseAgent

        BaseAgent._claim_official_tool(self, transaction)

    def _mount_official_tool(self, transaction) -> None:
        from lingtai.kernel.tool_plugin import (
            OFFICIAL_TOOL_PLUGIN_NAMES,
            _OfficialMountTransaction,
        )

        if not isinstance(transaction, _OfficialMountTransaction):
            raise PermissionError(
                "official tool mounting requires a registrar transaction"
            )
        declaration = transaction.declaration
        plugin = transaction.plugin
        name = declaration.name
        if (
            name not in OFFICIAL_TOOL_PLUGIN_NAMES
            or plugin.name != name
            or self._official_tool_declarations.get(name) is not declaration
            or self._official_tool_bindings.get(name) is not plugin
        ):
            raise PermissionError(
                "official mount transaction is not the canonical declaration/bind result"
            )
        live = self._official_tool_plugins.get(name)
        if live is not None and live is not declaration:
            raise PermissionError("official mount transaction is not for the live claim")
        transaction.consume()
        self.add_tool(
            name,
            schema=dict(plugin.schema),
            handler=plugin.handler,
            description=plugin.description,
            glossary_package=plugin.glossary_package,
        )
        transaction.mark_mounted(self)

    def add_tool(self, name, *, schema=None, handler=None,
                 description: str = "", system_prompt: str = "",
                 glossary_package: str | None = None):
        if handler is not None:
            self.handlers[name] = handler
        if schema is not None:
            self.schemas[name] = FunctionSchema(
                name=name, description=description,
                parameters=schema, system_prompt=system_prompt,
                glossary_package=glossary_package,
            )

    def __getattr__(self, name):
        return getattr(self._parent, name)


_DESCRIPTION = (
    "Daemon — dispatch disposable subagents for isolated parallel work. "
    "Read the daemon manual before first use. Put the complete objective, "
    "authority, safety boundary, collaboration rules, and deliverable in each "
    "task; tools grant capability only. Terminal outcomes are push-notified, "
    "so do not poll for completion. After notification, use check and the "
    "durable result/error paths for full output. LingTai runs may use the "
    "sole-call compact(action='run', _reason='...') reset; "
    "compact(action='manual') is read-only."
)


def get_description(lang: str = "en") -> str:
    return _DESCRIPTION


def _bind_daemon(host) -> BoundToolPlugin:
    """Compose Daemon against its granted ports without mounting or starting work."""
    options = host.daemon_runtime.manager_options
    manager = DaemonManager(
        host.daemon_runtime,
        max_turns=options["max_turns"],
        timeout=options["timeout"],
        notify_threshold=options["notify_threshold"],
        manager_pool_size=options["manager_pool_size"],
        system_prompt_budget_chars=options["system_prompt_budget_chars"],
        workdir=host.workdir,
        process_port=options.get("process_port"),
        interactive_terminal_port=options.get("interactive_terminal_port"),
    )
    host.daemon_runtime.attach_daemon_manager(manager)
    dispatcher = DaemonFamilyDispatcher(
        manager,
        host.workdir,
        list(_BACKEND_SCHEMA_ENUM),
        declaration=DECLARATION,
    )
    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=dispatcher.handle,
        description=DECLARATION.description,
        glossary_package=DECLARATION.glossary_package,
    )


#: Static official identity of the Daemon tool.  Its five operational actions
#: and their strict schemas are declared before an Agent exists; the kernel
#: appends the reserved installed-manual action and checks the bound schema on
#: every registration.
DECLARATION = ToolPluginDeclaration(
    name="daemon",
    actions=DAEMON_DECLARED_ACTIONS,
    input_schemas=declared_input_schemas(list(_BACKEND_SCHEMA_ENUM)),
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="daemon",
    description=_DESCRIPTION,
    binder=_bind_daemon,
    requires=("workdir", "daemon_runtime"),
    glossary_package=__package__,
    settings=True,
)


def get_schema(lang: str = "en") -> dict:
    """Compose the declaration-derived LTP v2 public Daemon schema."""
    return _family_build_schema(
        list(_BACKEND_SCHEMA_ENUM), lang, declaration=DECLARATION,
    )


# Sentinel strings a cooperatively-exited run returns through the future.
# ``[cancelled]`` is emitted on a timed-out/reclaimed run; ``[no output]`` on a
# run that produced no final text. ``[intercepted]`` is a guard-handled *normal*
# exit and must NOT be classified as a terminal abort.
def _build_emanation_prompt_standalone(
    language: str,
    task: str,
    schemas: list[FunctionSchema],
    system_prompt: str | None = None,
    system_prompt_budget_chars: int = DAEMON_SYSTEM_PROMPT_BUDGET_CHARS,
) -> str:
    """Build the bounded LingTai daemon prompt for parent and supervisor paths."""
    _ = language  # The dedicated daemon operating contract is canonical English.
    return build_daemon_system_prompt(
        task=task,
        tool_names=(schema.name for schema in schemas),
        oneshot_context=system_prompt,
        budget_chars=system_prompt_budget_chars,
    )


_CANCELLED_SENTINEL = "[cancelled]"
_NO_OUTPUT_SENTINEL = "[no output]"

# Fraction of the timeout past which a ``[no output]`` run is treated as a
# timeout by the low-priority elapsed fallback (P5).
_ELAPSED_TIMEOUT_FRACTION = 0.9


def _classify_terminal_state(
    entry: dict | None,
    future_succeeded: bool,
    text: str,
    timeout_s: float,
) -> str:
    """Classify the true terminal state of a *successfully-returned* emanation.

    Returns one of ``"timeout"``, ``"cancelled"``, ``"failed"``, ``"done"``.

    Called only when ``future.result()`` succeeded (no exception). When the
    future raises, the caller sets ``status="failed"`` directly and does not
    call this helper.

    The sole purpose is to preserve the true terminal state (timeout /
    cancelled / failed / done) before publishing the parent-facing daemon
    notification. Every terminal state, including a short successful
    ``"done"`` result, is now surfaced so the parent can safely go idle after
    dispatch and wake when the run ends.

    Priority order (first match wins):

      P1  ``run_dir.state_snapshot()["state"]`` — authoritative recorded state.
          This is the same signal current main already trusts; the marker is
          written by the run loop's ``mark_timeout`` / ``mark_cancelled`` /
          ``mark_failed`` / ``mark_done`` before the future resolves.
      P2  ``timeout_event.is_set()`` — watchdog fired. Fallback for when the
          run terminated before it could record its state.
      P3  ``cancel_event.is_set()`` — manual reclaim (only when timeout_event is
          not also set; the watchdog sets both).
      P4  ``[cancelled]`` sentinel text — last-resort backstop.
      P5  elapsed >= fraction * timeout_s with a ``[no output]`` body — a
          deliberately low-priority heuristic for the rare case where neither
          the recorded state nor the events survived.
      P6  ``"done"`` — genuine success.
    """
    run_dir = entry.get("run_dir") if entry else None

    # --- P1: recorded run_dir state (authoritative) ---
    if run_dir is not None:
        try:
            recorded = run_dir.state_snapshot().get("state")
        except Exception:
            recorded = None
        if recorded in ("timeout", "cancelled", "failed"):
            return recorded
        if recorded == "done":
            return "done"

    # --- P2: watchdog timeout event ---
    timeout_event = entry.get("timeout_event") if entry else None
    if timeout_event is not None:
        try:
            if timeout_event.is_set():
                return "timeout"
        except Exception:
            pass

    # --- P3: manual cancel event (timeout not set) ---
    cancel_event = entry.get("cancel_event") if entry else None
    if cancel_event is not None:
        try:
            if cancel_event.is_set():
                return "cancelled"
        except Exception:
            pass

    # --- P4: [cancelled] sentinel backstop ---
    if text == _CANCELLED_SENTINEL:
        return "cancelled"

    # --- P5: elapsed-near-timeout backstop (low priority) ---
    if text == _NO_OUTPUT_SENTINEL and timeout_s > 0 and entry is not None:
        start_time = entry.get("start_time")
        if start_time is not None:
            elapsed = time.time() - start_time
            if elapsed >= timeout_s * _ELAPSED_TIMEOUT_FRACTION:
                return "timeout"

    # --- P6: genuine success ---
    return "done"


class DaemonManager:
    """Manages subagent (emanation) lifecycle."""

    # Historical constructor default for ``notify_threshold``. Terminal daemon
    # completions are no longer suppressed by result length: the compact system
    # notification is the parent wake signal for every terminal state. The
    # constructor argument remains accepted for compatibility with existing
    # callers/configs.
    _NOTIFY_MIN_LEN = 20

    def __init__(self, runtime: Any,
                 max_turns: int = DEFAULT_MAX_TURNS, timeout: float = 3600.0,
                 notify_threshold: int = 20,
                 manager_pool_size: int = 100,
                 system_prompt_budget_chars: int = DAEMON_SYSTEM_PROMPT_BUDGET_CHARS,
                 *, workdir: Any | None = None,
                 process_port: DaemonProcessPort | None = None,
                 interactive_terminal_port: InteractiveTerminalPort | None = None):
        """Construct against Daemon's narrow runtime/workdir ports.

        The no-``workdir`` form remains a direct-construction compatibility
        seam for existing in-process callers and tests.  It is immediately
        adapted in the production adapter module, so the manager itself still
        consumes only the same named runtime/workdir operations used by the
        declared-plugin binder; normal Agent boot always supplies both ports.
        """
        if workdir is None:
            from lingtai.adapters.tool_plugin_host import (
                AgentWorkdirAdapter,
                daemon_runtime_for_agent,
            )

            agent = runtime
            runtime = daemon_runtime_for_agent(agent, {})
            workdir = AgentWorkdirAdapter(
                lambda: getattr(agent, "_working_dir")
                if hasattr(agent, "_working_dir") else agent.working_dir
            )
        self._runtime = runtime
        self._workdir = workdir
        self._max_turns = self._env_positive_int(
            "LINGTAI_DAEMON_MAX_TURNS", max_turns,
        )
        self._timeout = timeout
        self._manager_pool_size = self._env_nonnegative_int(
            "LINGTAI_DAEMON_MANAGER_POOL_SIZE", manager_pool_size,
        )
        self._system_prompt_budget_chars = self._env_positive_int(
            "LINGTAI_DAEMON_SYSTEM_PROMPT_BUDGET_CHARS",
            _config_positive_int(
                system_prompt_budget_chars, DAEMON_SYSTEM_PROMPT_BUDGET_CHARS
            ),
        )
        self._default_model = self._runtime.service.model
        self._notify_threshold = notify_threshold
        # Direct construction is a supported test/in-process composition path
        # as well as setup(). POSIX and Windows each have one production
        # process adapter; any other platform fails loudly.
        if process_port is None:
            if os.name == "posix":
                from .posix_process import PosixDaemonProcessPort
                process_port = PosixDaemonProcessPort()
            elif os.name == "nt":
                from .windows_process import WindowsDaemonProcessPort
                process_port = WindowsDaemonProcessPort()
            else:
                raise NotImplementedError(
                    f"daemon process supervision is unsupported on {os.name!r}"
                )
        self._process_port = process_port
        # Interactive Claude is a separate byte-stream capability. On POSIX
        # setup injects one shared adapter so group/all lifecycle sweeps own
        # children that the bridge cannot otherwise see. Windows deliberately
        # receives None until a real ConPTY implementation exists — the
        # claude-interactive bridge refuses a None terminal port loudly.
        if interactive_terminal_port is None and os.name == "posix":
            from lingtai.adapters.posix.interactive_terminal import (
                PosixInteractiveTerminalAdapter,
            )
            interactive_terminal_port = PosixInteractiveTerminalAdapter()
        self._interactive_terminal_port = interactive_terminal_port

        # Emanation registry: compact daemon id → entry dict
        self._emanations: dict[str, dict] = {}
        # New ids are compact and collision-checked in _new_emanation_id().
        # Pool tracking for reclaim
        self._pools: list[tuple[ThreadPoolExecutor, threading.Event]] = []
        # CLI process tracking for direct process-group kill on reclaim/timeout.
        # Guarded by _cli_lock — accessed from pool workers, watchdog, and reclaim.
        #
        # ``_cli_procs`` is the flat global list used by reclaim-all / agent stop.
        # ``_cli_proc_groups`` is the per-batch index keyed by daemon ``group_id``
        # so a batch's timeout watchdog kills only *its own* CLI subprocesses and
        # never a newer, unrelated batch's (GH overlapping-batch kill). Procs that
        # do not belong to a batch (e.g. CLI ``ask`` follow-ups) register with
        # ``group_id=None`` — they are tracked globally for reclaim but no batch
        # watchdog owns them.
        self._cli_procs: list[subprocess.Popen] = []
        self._cli_proc_groups: dict[str, set[subprocess.Popen]] = {}
        # LingTai-initiated termination reason per tracked proc, keyed by
        # id(proc) and guarded by ``_cli_lock``. Stamped at the out-of-loop kill
        # sites (reclaim/agent_stop/parent refresh via _drain_all_cli_procs,
        # batch timeout via _kill_cli_group) *before* SIGTERM is sent, then read
        # back when the read loop sees the resulting -15/143 returncode so the
        # exit is attributed to the local cause instead of an opaque
        # "claude CLI exited with code 143". See GH #455.
        self._cli_term_reasons: dict[int, str] = {}
        self._cli_lock = threading.Lock()
        # Dedicated pool for CLI-backend `ask` follow-ups so they run off the
        # caller's tool-dispatch thread. The agent's `daemon(action="ask")` call
        # returns immediately while progress + final reply land in the run_dir
        # (cli_output events, last_output, follow-up completion notification).
        # Workers are submitted lazily so the pool is only spun up on first use.
        self._ask_pool = ThreadPoolExecutor(
            max_workers=max(1, self._manager_pool_size),
            thread_name_prefix="daemon-cli-ask",
        )
        # New dispatches maintain only unresolved marker files.  Construction
        # inspects that bounded set and never enumerates lifetime run folders.
        self._recover_unresolved_markers()

    @staticmethod
    def _env_nonnegative_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    @staticmethod
    def _env_positive_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def _recover_unresolved_markers(self) -> None:
        """Recover only new-format unresolved marker files, never legacy history."""
        for kind, run_id, _marker in dispatch_ledger.recovery_markers(self._workdir.path):
            daemon_json_path = self._workdir.path / "daemons" / run_id / "daemon.json"
            try:
                state = json.loads(daemon_json_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            if kind == "running":
                if state.get("state") in {"done", "failed", "cancelled", "timeout"}:
                    dispatch_ledger.clear_marker(self._workdir.path, "running", run_id)
                    if state.get("terminal_notified") is False:
                        dispatch_ledger.mark_pending_terminal_notification(self._workdir.path, run_id)
                    continue
                self._reap_daemon_state_path(daemon_json_path, state)
            elif kind == "pending-terminal":
                self._reconcile_terminal_notification_path(daemon_json_path, state)

    def _reap_dead_parent_daemon_records(self) -> None:
        """Compatibility entry point: recover bounded markers only."""
        self._recover_unresolved_markers()

    def _reap_daemon_state_path(self, daemon_json_path: Path, state: dict) -> None:
        """Apply the established stale-owner classification to one marker run."""
        daemon_state = state.get("state")
        if not isinstance(daemon_state, str) or daemon_state.lower() not in {"running", "active"}:
            return
        if state.get("finished_at") not in (None, ""):
            return
        current_pid = os.getpid()
        owner = state.get("owner", "parent")
        if owner == "supervisor":
            supervisor_pid = state.get("supervisor_pid")
            if not isinstance(supervisor_pid, int) or isinstance(supervisor_pid, bool):
                return
            if self._pid_identity_matches(supervisor_pid, state.get("supervisor_start_identity")):
                return
            reap_reason = (
                "Reaped running daemon record because recorded "
                f"supervisor_pid {supervisor_pid} is no longer alive with no terminal state committed."
            )
        elif owner == "manager":
            if self._manager_owner_alive(state):
                return
            manager_pid = state.get("manager_pid")
            subject = f"manager_pid {manager_pid}" if isinstance(manager_pid, int) and not isinstance(manager_pid, bool) else "central daemon manager"
            reap_reason = f"Reaped running daemon record because recorded {subject} is no longer alive with no terminal state committed."
        else:
            parent_pid = state.get("parent_pid")
            if not isinstance(parent_pid, int) or isinstance(parent_pid, bool) or parent_pid == current_pid or self._pid_alive(parent_pid):
                return
            reap_reason = f"Reaped running daemon record because recorded parent_pid {parent_pid} is no longer alive after daemon manager startup."
        try:
            orphaned = type("DaemonOrphaned", (RuntimeError,), {})
            DaemonRunDir.attach(daemon_json_path.parent).mark_failed(orphaned(reap_reason))
        except (OSError, ValueError, json.JSONDecodeError):
            return

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            # os.kill(pid, 0) on Windows TERMINATES the target (TerminateProcess
            # with exit code 0) — it is never a liveness probe. Use the shared
            # OpenProcess-based observation instead.
            from lingtai.adapters.windows import _win32
            return _win32.process_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _manager_owner_alive(self, state: dict) -> bool:
        manager_pid = state.get("manager_pid")
        manager_identity = state.get("manager_start_identity")
        if (
            isinstance(manager_pid, int)
            and not isinstance(manager_pid, bool)
            and self._pid_identity_matches(manager_pid, manager_identity)
        ):
            return True
        try:
            from lingtai.adapters.posix.daemon_manager import MANAGER_DIR

            pid_path = self._workdir.path / MANAGER_DIR / "manager.pid"
            info = json.loads(pid_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(info, dict):
            return False
        pid = info.get("pid")
        identity = info.get("manager_start_identity")
        started_at = info.get("started_at")
        if (
            pid is None
            and info.get("state") == "starting"
            and isinstance(started_at, (int, float))
            and not isinstance(started_at, bool)
            and time.time() - started_at < self._SUPERVISOR_STARTUP_TIMEOUT_S
        ):
            return True
        return (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and self._pid_identity_matches(pid, identity)
        )

    @staticmethod
    def _pid_identity_matches(pid: int, expected: str | None) -> bool:
        """Check PID plus a stable process-incarnation identity."""
        return process_identity_matches(pid, expected)

    def _reconcile_terminal_notifications(self) -> None:
        """Compatibility entry point: retry only bounded pending markers."""
        for kind, run_id, _marker in dispatch_ledger.recovery_markers(self._workdir.path):
            if kind != "pending-terminal":
                continue
            path = self._workdir.path / "daemons" / run_id / "daemon.json"
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(state, dict):
                self._reconcile_terminal_notification_path(path, state)

    def _reconcile_terminal_notification_path(self, daemon_json_path: Path, state: dict) -> None:
        """Retry one explicitly pending new-format terminal receipt."""
        status = state.get("state")
        if status not in {"done", "failed", "cancelled", "timeout"}:
            return
        if state.get("terminal_notified") is not False:
            dispatch_ledger.clear_marker(self._workdir.path, "pending-terminal", str(state.get("run_id") or daemon_json_path.parent.name))
            return
        run_id = str(state.get("run_id") or daemon_json_path.parent.name)
        key = DaemonRunDir.terminal_notification_idempotency_key(run_id)
        text = self._terminal_notification_text_from_state(state, daemon_json_path.parent)
        if self._publish_daemon_notification(
            run_id, status=status, text=text, run_state=state,
            run_path=daemon_json_path.parent, idempotency_key=key,
        ):
            DaemonRunDir.mark_terminal_notification_published_on_disk(
                daemon_json_path, idempotency_key=key,
            )

    def _terminal_notification_text_from_state(
        self, state: dict, run_path: Path
    ) -> str:
        result_path = state.get("result_path")
        if isinstance(result_path, str) and result_path:
            try:
                with open(result_path, encoding="utf-8") as f:
                    return f.read(2000)
            except (OSError, UnicodeDecodeError):
                pass
        preview = state.get("result_preview") or state.get("last_output")
        if isinstance(preview, str) and preview:
            return preview
        error = state.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        try:
            with open(run_path / "result.txt", encoding="utf-8") as f:
                return f.read(2000)
        except (OSError, UnicodeDecodeError):
            return ""

    def handle(self, args: dict) -> dict:
        action = args.get("action")
        if action == "manual":
            return load_installed_manual(self._workdir, "daemon")
        backend = _normalize_backend(args.get("backend", "lingtai"))
        if action == "emanate":
            return self._handle_emanate(
                args.get("tasks", []),
                max_turns=args.get("max_turns"),
                timeout=args.get("timeout"),
                backend=backend,
            )
        elif action == "list":
            return self._handle_list(
                contains=args.get("contains", ""),
                status_filter=args.get("status", "all"),
                include_done=args.get("include_done", True),
                limit=args.get("last"),
            )
        elif action == "ask":
            return self._handle_ask(args.get("id", ""), args.get("message", ""))
        elif action == "check":
            return self._handle_check(
                args.get("id", ""),
                last=args.get("last", 20),
                truncate=args.get("truncate", 500),
            )
        elif action == "reclaim":
            return self._handle_reclaim()
        else:
            return {"status": "error", "message": f"Unknown action: {action}"}

    def _daemon_intrinsic_surface(self) -> tuple[dict[str, FunctionSchema], dict]:
        """Return daemon-eligible intrinsic schemas/handlers.

        Daemons do not inherit the intrinsic layer at all: identity/lifecycle
        and recursive mutation tools stay unavailable, and communication
        (``email``) is no longer an intrinsic exception here — it is provided
        as an MCP tool identical to every other daemon backend, auto-mounted
        by ``_with_daemon_email_mcp`` when a task explicitly requests
        ``tools: ["email"]`` (see ``_daemon_email_mcp_registration``). The
        only thing still synthesized here is ``compact``, a daemon-runtime
        primitive with no parent-intrinsic equivalent.
        """
        schemas: dict[str, FunctionSchema] = {}
        handlers: dict = {}
        schemas["compact"] = FunctionSchema(
            name="compact",
            description=(
                "Action is required. Use action='manual' for read-only compaction "
                "procedures; it never changes context. Use action='run' to reset "
                "this LingTai daemon into a fresh provider context in the same run. "
                "All previous conversation and tool history will be removed; only "
                "this compact call/result pair survives. The run action must be the "
                "sole tool call in its assistant batch, and _reason must be the "
                "complete, self-contained handoff document."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["run", "manual"],
                        "description": "Required: 'manual' returns read-only procedures; 'run' performs the non-terminal context reset.",
                    },
                    "_reason": {
                        "type": "string",
                        "description": "For action='run': complete self-contained handoff and resume instruction; do not provide a path or rely on prior context.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
                "anyOf": [
                    {
                        "properties": {"action": {"const": "run"}},
                        "required": ["_reason"],
                    },
                    {"properties": {"action": {"const": "manual"}}},
                ],
            },
        )
        return schemas, handlers

    @staticmethod
    def _resolve_task_skill_path(raw_path: str, working_dir: Path) -> Path:
        """Resolve one daemon task skill path to a concrete SKILL.md file."""
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = working_dir / p
        p = p.resolve(strict=False)
        if p.is_dir():
            p = p / "SKILL.md"
        if not p.is_file():
            raise ValueError(f"skill path does not resolve to a file: {raw_path}")
        if p.name != "SKILL.md":
            raise ValueError(f"skill file path must point to SKILL.md: {raw_path}")
        return p

    @staticmethod
    def _parse_task_skill_file(skill_file: Path) -> dict:
        """Parse a selected SKILL.md into the compact daemon skill catalog row."""
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(f"cannot read skill file {skill_file}: {e}") from e
        m = _DAEMON_SKILL_FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError(f"skill file missing YAML frontmatter: {skill_file}")
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"skill file has invalid YAML frontmatter: {skill_file}: {e}") from e
        if not isinstance(loaded, dict):
            raise ValueError(f"skill file frontmatter must be a mapping: {skill_file}")
        raw_name = loaded.get("name")
        raw_description = loaded.get("description")
        name = " ".join(str(raw_name).split()) if raw_name is not None else ""
        description = (
            " ".join(str(raw_description).split())
            if raw_description is not None
            else ""
        )
        if not name:
            raise ValueError(f"skill file missing required frontmatter field: name: {skill_file}")
        if not description:
            raise ValueError(f"skill file missing required frontmatter field: description: {skill_file}")
        return {"name": name, "location": str(skill_file), "description": description}

    @staticmethod
    def _plugin_path_resolution(raw_path: str, working_dir) -> Path:
        """Resolve one daemon task plugin path to a concrete plugin directory."""
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = working_dir / p
        return p.resolve(strict=False)

    @staticmethod
    def _plugin_mcp_spec_to_registration(
        plugin_name: str, server_name: str, spec: dict
    ) -> dict:
        """Translate one resolved plugin mcp.json server into a task MCP registration.

        Mirrors ``plugin_registry.to_registry_record`` transport mapping: the
        Agent Plugins v1.0.0 transports ``stdio`` / ``streamable-http`` / ``sse``
        land on the daemon registration transports ``stdio`` / ``http``. The
        resolved spec already carries absolute, containment-validated paths, so
        the registration is directly usable by the LingTai backend client.
        """
        transport_map = {
            "stdio": "stdio",
            "streamable-http": "http",
            "sse": "http",
        }
        transport = transport_map.get(spec.get("type"))
        if transport is None:
            raise ValueError(
                f"plugin {plugin_name!r} mcp server {server_name!r}: unsupported "
                f"transport {spec.get('type')!r}"
            )
        reg: dict = {"name": server_name, "transport": transport}
        if transport == "stdio":
            command = spec.get("command")
            if not isinstance(command, str) or not command:
                raise ValueError(
                    f"plugin {plugin_name!r} mcp server {server_name!r}: stdio "
                    "requires command"
                )
            reg["command"] = command
            args = spec.get("args")
            if args:
                reg["args"] = list(args)
            env = spec.get("env")
            if env:
                reg["env"] = dict(env)
            cwd = spec.get("cwd")
            if cwd:
                reg["cwd"] = cwd
        else:
            url = spec.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(
                    f"plugin {plugin_name!r} mcp server {server_name!r}: http "
                    "requires url"
                )
            reg["url"] = url
            headers = spec.get("headers")
            if headers:
                reg["headers"] = dict(headers)
        return reg

    @staticmethod
    def _render_task_plugin_catalog(plugins: list[dict]) -> str | None:
        """Render the compact plugin section injected into a daemon run prompt.

        This mirrors the main agent's resident ``plugins`` prompt field: the
        daemon sees the same whole-plugin view (name, summary, skills list, mcp
        list) instead of a flattened skills/mcp split, so it understands that
        the components belong to one distributable unit. Plugins whose mcp.json
        servers were also mounted as task MCP clients are marked ``mounted``.
        """
        if not plugins:
            return None
        lines = [
            "The parent selected these Agent Plugins for this daemon run. Read/apply their skills only when relevant to your task:",
            "plugins:",
        ]
        for pl in plugins:
            name = pl.get("name", "")
            summary = pl.get("summary", "")
            lines.append(f"  - name: {name}")
            if summary:
                lines.append(f"    summary: {summary}")
            skills = pl.get("skills") or []
            lines.append(f"    skills ({len(skills)}):")
            for sk in skills:
                lines.append(f"      - {sk}")
            servers = pl.get("mcp_servers") or []
            mounted = set(pl.get("mounted_mcp") or [])
            lines.append(f"    mcp ({len(servers)}):")
            for server in servers:
                marker = " (mounted)" if server in mounted else ""
                lines.append(f"      - {server}{marker}")
        return "\n".join(lines)

    def _task_plugin_context(
        self, spec: dict
    ) -> tuple[str | None, list[dict], list[dict]]:
        """Resolve one daemon task's ``plugin`` paths.

        Returns ``(plugin_catalog, plugin_skill_rows, plugin_mcp_regs)``.
        ``plugin_skill_rows`` are compact skill catalog rows parsed from each
        plugin's validated ``skills/``; ``plugin_mcp_regs`` are full MCP
        registration objects translated from each plugin's validated ``mcp.json``.
        The LingTai backend mounts ``plugin_mcp_regs`` as task-scoped MCP clients
        and merges ``plugin_skill_rows`` into the skill catalog; CLI backends that
        cannot mount plugins still receive the same skills and MCP registrations
        separately through the normal skill/mcp oneshot context, which is the
        "inject both separately" fallback.
        """
        raw = spec.get("plugin")
        if raw is None:
            return None, [], []
        if not isinstance(raw, list):
            raise ValueError("plugin must be an array of plugin directory paths")
        working_dir = self._workdir.path
        resolved_paths: list[Path] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"plugin[{idx}] must be a non-empty string path")
            resolved_paths.append(
                self._plugin_path_resolution(item.strip(), working_dir)
            )
        records, problems, _report = _plugin_registry.read_plugins(
            working_dir, [str(p) for p in resolved_paths]
        )
        # Component problems are reported, not fatal; a plugin that cannot be
        # read at all (invalid plugin.json) is omitted by read_plugins.
        for prob in problems:
            self._log("plugin", warning=str(prob))

        skill_rows: list[dict] = []
        mcp_regs: list[dict] = []
        mounted_mcp_by_plugin: dict[str, list[str]] = {}
        for record in records:
            name = record["name"]
            for skill_dir in record.get("skill_paths") or []:
                try:
                    skill_file = Path(skill_dir) / "SKILL.md"
                    skill_rows.append(self._parse_task_skill_file(skill_file))
                except ValueError as e:
                    self._log("plugin", warning=f"plugin {name} skill skipped: {e}")
            for server_name, spec in (record.get("mcp_specs") or {}).items():
                try:
                    mcp_regs.append(
                        self._plugin_mcp_spec_to_registration(
                            name, server_name, spec
                        )
                    )
                except ValueError as e:
                    self._log("plugin", warning=str(e))
            mounted_mcp_by_plugin[name] = list(record.get("mcp_servers") or [])

        # Attach mounted markers for the prompt view (LingTai backend only; CLI
        # backends still get the flattened skills/mcp separately below).
        for record in records:
            record["mounted_mcp"] = mounted_mcp_by_plugin.get(record["name"], [])
        catalog = self._render_task_plugin_catalog(records)
        return catalog, skill_rows, mcp_regs

    @staticmethod
    def _merge_skill_catalog_rows(
        rendered: str | None, extra_rows: list[dict]
    ) -> list[dict]:
        """Merge already-rendered task skill rows with plugin skill rows.

        ``_task_skill_catalog`` returns a rendered string, so to append plugin
        skill rows we re-render from the original list. This helper parses the
        rendered YAML back into row dicts when needed; when the base catalog is
        None (no explicit ``skills``) it simply returns the plugin rows.
        Deduplicates by canonical skill location.
        """
        if rendered is None:
            return list(extra_rows)
        rows: list[dict] = []
        current: dict | None = None
        lines = rendered.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^  - name: (.*)$", line)
            if m:
                if current is not None:
                    rows.append(current)
                current = {"name": m.group(1), "location": "", "description": ""}
                i += 1
                continue
            lm = re.match(r"^    location: (.*)$", line)
            dm = line == "    description: |"
            if current is not None and lm:
                current["location"] = lm.group(1)
            elif current is not None and dm:
                desc_lines = []
                i += 1
                while i < len(lines) and lines[i].startswith("      "):
                    desc_lines.append(lines[i][6:])
                    i += 1
                current["description"] = "\n".join(desc_lines)
                continue
            i += 1
        if current is not None:
            rows.append(current)
        seen = {row.get("location") for row in rows}
        for row in extra_rows:
            loc = row.get("location")
            if loc in seen:
                continue
            seen.add(loc)
            rows.append(row)
        return rows

    @staticmethod
    def _render_task_skill_catalog(skills: list[dict]) -> str | None:
        if not skills:
            return None
        lines = [
            "The parent selected these skills for this daemon run. Read/apply them only when relevant to your task:",
            "skills:",
        ]
        for sk in skills:
            lines.append(f"  - name: {sk['name']}")
            lines.append(f"    location: {sk['location']}")
            lines.append("    description: |")
            desc_lines = sk["description"].splitlines() or [""]
            for dl in desc_lines:
                lines.append(f"      {dl}" if dl else "      ")
        return "\n".join(lines)

    @staticmethod
    def _task_mcp_registrations(spec: dict) -> tuple[list[dict], str | None]:
        """Return normalized full MCP registrations and rendered YAML context."""
        raw = spec.get("mcp")
        if raw is None:
            return [], None
        if not isinstance(raw, list):
            raise ValueError("mcp must be an array of MCP registration objects")
        rows: list[dict] = []
        seen: set[str] = set()
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"mcp[{idx}] must be an MCP registration object")
            cfg = dict(item)
            name = cfg.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"mcp[{idx}].name must be a non-empty string")
            name = name.strip()
            if name in seen:
                raise ValueError(f"duplicate MCP registration name: {name}")
            seen.add(name)
            transport = cfg.get("transport", cfg.get("type", "stdio"))
            if transport not in ("stdio", "http"):
                raise ValueError(
                    f"mcp[{idx}].transport/type must be 'stdio' or 'http'"
                )
            normalized = dict(cfg)
            normalized["name"] = name
            normalized["transport"] = transport
            normalized.pop("type", None)
            if transport == "stdio":
                if not isinstance(normalized.get("command"), str) or not normalized["command"]:
                    raise ValueError(f"mcp[{idx}] stdio registration requires command")
                args = normalized.get("args", [])
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise ValueError(f"mcp[{idx}].args must be an array of strings")
                normalized["args"] = list(args)
                env = normalized.get("env")
                if env is not None and (
                    not isinstance(env, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
                ):
                    raise ValueError(f"mcp[{idx}].env must be an object of string values")
            else:
                if not isinstance(normalized.get("url"), str) or not normalized["url"]:
                    raise ValueError(f"mcp[{idx}] http registration requires url")
                headers = normalized.get("headers")
                if headers is not None and (
                    not isinstance(headers, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
                ):
                    raise ValueError(f"mcp[{idx}].headers must be an object of string values")
            rows.append(normalized)
        return rows, DaemonManager._render_task_mcp_catalog(rows)

    @staticmethod
    def _redact_mcp_registration_for_prompt(cfg: dict) -> dict:
        """Return a prompt-safe MCP registration copy.

        The daemon runtime uses the full object for LingTai-backend MCP startup,
        but the serialized context should not leak secret env/header values into
        model prompts. Keys remain visible so CLI backends know what must be
        supplied by their own environment/config.
        """
        out = dict(cfg)
        for field in ("env", "headers"):
            value = out.get(field)
            if isinstance(value, dict):
                out[field] = {k: "<redacted>" for k in value}
        return out

    @staticmethod
    def _render_task_mcp_catalog(registrations: list[dict]) -> str | None:
        if not registrations:
            return None
        safe = [DaemonManager._redact_mcp_registration_for_prompt(r)
                for r in registrations]
        body = yaml.safe_dump(
            {"mcp": safe},
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        return (
            "The parent provided these MCP registrations for this daemon run. "
            "They are one-run context: LingTai backend may load them directly; "
            "CLI backends should use them only if their runtime can load MCP "
            "registrations. Secret env/header values are redacted in this prompt.\n"
            f"{body}"
        )

    @staticmethod
    def _completion_file(run_dir: DaemonRunDir) -> Path:
        return run_dir.path / _DAEMON_COMPLETION_FILE

    @staticmethod
    def _daemon_common_mcp_registration(run_dir: DaemonRunDir) -> dict:
        return {
            "name": _DAEMON_COMMON_MCP_NAME,
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "lingtai.mcp_servers.daemon_common"],
            "env": {
                "LINGTAI_DAEMON_COMPLETION_FILE": str(
                    DaemonManager._completion_file(run_dir)
                ),
                "LINGTAI_DAEMON_RUN_ID": run_dir.run_id,
                "LINGTAI_DAEMON_RUN_DIR": str(run_dir.path),
                "PYTHONPATH": _dev_pythonpath_with_source_root(),
            },
        }

    @staticmethod
    def _with_daemon_common_mcp(
        registrations: list[dict],
        run_dir: DaemonRunDir,
    ) -> list[dict]:
        rows = list(registrations)
        if any(r.get("name") == _DAEMON_COMMON_MCP_NAME for r in rows):
            raise ValueError(
                f"MCP registration name {_DAEMON_COMMON_MCP_NAME!r} is reserved"
            )
        return [DaemonManager._daemon_common_mcp_registration(run_dir), *rows]

    @staticmethod
    def _daemon_email_mcp_registration(
        run_dir: DaemonRunDir,
        parent_working_dir: Path,
    ) -> dict:
        """MCP registration for the daemon-facing ``email`` tool.

        Mounted only when a task explicitly requests ``tools: ["email"]``
        (see call sites in ``_handle_emanate``/``_handle_emanate_cli``), the
        same explicit-opt-in gate the removed intrinsic exception enforced —
        a `tools=[]` result-only daemon never gets this registration, so it
        never gets email. ``LINGTAI_AGENT_DIR`` points the server at the
        *parent* agent's own working directory (its live, handshake-able
        mailbox), not this run's own nested run-dir, which the parent
        subsystem never gives an ``.agent.json``/heartbeat.
        """
        return {
            "name": _DAEMON_EMAIL_MCP_NAME,
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "lingtai.mcp_servers.daemon_email"],
            "env": {
                "LINGTAI_AGENT_DIR": str(parent_working_dir),
                "PYTHONPATH": _dev_pythonpath_with_source_root(),
            },
        }

    @staticmethod
    def _with_daemon_email_mcp(
        registrations: list[dict],
        run_dir: DaemonRunDir,
        parent_working_dir: Path,
    ) -> list[dict]:
        rows = list(registrations)
        if any(r.get("name") == _DAEMON_EMAIL_MCP_NAME for r in rows):
            raise ValueError(
                f"MCP registration name {_DAEMON_EMAIL_MCP_NAME!r} is reserved"
            )
        return [
            DaemonManager._daemon_email_mcp_registration(run_dir, parent_working_dir),
            *rows,
        ]

    @staticmethod
    def _daemon_common_context() -> str:
        return (
            "LingTai daemon checkpoint contract: at useful nonterminal task "
            "boundaries, call the reserved MCP tool `checkpoint` with a short "
            "state and bounded progress summary. The call durably wakes the "
            "parent and returns any queued parent messages exactly once; apply "
            "those messages before continuing. A checkpoint is cooperative and "
            "does not finish, pause, preempt, or turn the run into a chat. "
            "LingTai daemon completion contract: before ending this run, call "
            "the MCP tool `finish` exactly once with status `done`, `failed`, "
            "or `incomplete`. Use `done` only after the task is actually "
            "complete. If blocked, timed out, uncertain, or unable to validate "
            "the required result, call `finish(status=\"incomplete\", reason=...)` "
            "or `finish(status=\"failed\", reason=...)`. For this daemon "
            "contract, background-and-wait is invalid: do not start a background "
            "job and end your turn expecting a later notification or re-entry. "
            "Run required validation synchronously with an explicit timeout, "
            "inspect its result in this run, then call `finish`. If the run "
            "ends without calling `finish(status=...)`, the parent will "
            "report it as missing-finish — that is not proof of failure; "
            "the parent should inspect the run's trace and the full final "
            "text preserved in the run directory's physical `result.txt`."
        )

    @staticmethod
    def _append_daemon_common_context(context: str | None) -> str:
        common = DaemonManager._daemon_common_context()
        if context:
            return f"{context}\n\n## LingTai daemon completion MCP\n{common}"
        return common

    @staticmethod
    def _write_claude_mcp_config(
        run_dir: DaemonRunDir,
        registrations: list[dict],
    ) -> Path:
        servers: dict[str, dict] = {}
        for reg in registrations:
            if reg.get("transport", reg.get("type", "stdio")) != "stdio":
                continue
            server = {
                "command": reg["command"],
                "args": list(reg.get("args") or []),
            }
            if reg.get("env"):
                server["env"] = dict(reg["env"])
            servers[reg["name"]] = server
        path = run_dir.path / _DAEMON_CLAUDE_MCP_CONFIG_FILE
        atomic_write_json(path, {"mcpServers": servers}, ensure_ascii=False, indent=2)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    @staticmethod
    def _read_daemon_completion(run_dir: DaemonRunDir) -> _DaemonCompletion:
        path = DaemonManager._completion_file(run_dir)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _DaemonCompletion(None, error=_DAEMON_MISSING_COMPLETION_ERROR)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            return _DaemonCompletion(None, error=f"invalid completion signal: {e}")
        if not isinstance(data, dict):
            return _DaemonCompletion(None, error="completion signal must be a JSON object")
        status = data.get("status")
        if status not in _DAEMON_COMPLETION_STATUSES:
            return _DaemonCompletion(
                None,
                error=(
                    "completion status must be one of "
                    f"{sorted(_DAEMON_COMPLETION_STATUSES)}"
                ),
            )
        run_id = data.get("run_id")
        if run_id is not None and run_id != run_dir.run_id:
            return _DaemonCompletion(
                None,
                error=f"completion run_id mismatch: {run_id!r} != {run_dir.run_id!r}",
            )
        summary = data.get("summary")
        reason = data.get("reason")
        artifacts = data.get("artifacts")
        if summary is not None and not isinstance(summary, str):
            return _DaemonCompletion(None, error="completion summary must be a string")
        if reason is not None and not isinstance(reason, str):
            return _DaemonCompletion(None, error="completion reason must be a string")
        if artifacts is not None and (
            not isinstance(artifacts, list)
            or not all(isinstance(item, str) for item in artifacts)
        ):
            return _DaemonCompletion(
                None, error="completion artifacts must be an array of strings"
            )
        return _DaemonCompletion(status, summary, reason, artifacts)

    @staticmethod
    def _run_has_daemon_common_mcp(run_dir: DaemonRunDir) -> bool:
        call_params = run_dir.state_snapshot().get("call_parameters")
        if not isinstance(call_params, dict):
            return False
        registrations = call_params.get("mcp")
        if not isinstance(registrations, list):
            return False
        return any(
            isinstance(reg, dict) and reg.get("name") == _DAEMON_COMMON_MCP_NAME
            for reg in registrations
        )

    def _require_done_completion(self, run_dir: DaemonRunDir, final_text: str) -> None:
        if not self._run_has_daemon_common_mcp(run_dir):
            return
        completion = self._read_daemon_completion(run_dir)
        if not completion.is_done:
            raise self._fail_missing_or_bad_completion(run_dir, completion, final_text)

    def _fail_missing_or_bad_completion(
        self,
        run_dir: DaemonRunDir,
        completion: _DaemonCompletion,
        final_text: str,
    ) -> RuntimeError:
        detail = completion.error or f"finish status was {completion.status!r}"
        if completion.reason:
            detail += f"; reason: {completion.reason}"
        if completion.summary:
            detail += f"; summary: {completion.summary}"
        if completion.error == _DAEMON_MISSING_COMPLETION_ERROR:
            detail += (
                ". The run ended without a finish() signal; this does not "
                "necessarily mean the task failed — before concluding, "
                "inspect the run's trace/result and the full final text "
                "preserved in the run directory's physical result.txt"
            )
        exc = RuntimeError(
            "daemon completion MCP contract did not permit success: "
            f"{detail}. Final text: {final_text[:500]}"
        )
        run_dir.mark_failed(exc)
        try:
            run_dir.result_path.write_text(final_text, encoding="utf-8")
        except OSError:
            pass
        self._log(
            "daemon_completion_contract_failed",
            em_id=run_dir.handle,
            run_id=run_dir.run_id,
            status=completion.status,
            error=completion.error,
        )
        return exc

    def _connect_task_mcp_registrations(
        self,
        registrations: list[dict],
    ) -> tuple[dict[str, FunctionSchema], dict, list[object]]:
        """Start task-scoped MCP clients and return schemas/handlers/clients.

        Advertised MCP metadata that ``FunctionSchema`` cannot carry (title,
        output schema, annotations, icons, execution, meta) is retained on the
        owner as ``_task_mcp_tool_metadata``, keyed by tool name, for the
        lifetime of the clients returned here. The return arity is deliberately
        unchanged: both call sites and several test doubles depend on the
        3-tuple, and the sidecar is not part of the tool-surface contract.
        """
        # Reset first: this owner's previous task run owns neither these
        # clients nor their metadata, and a raise below must not leave the
        # prior run's entries visible.
        self._task_mcp_tool_metadata = {}
        if not registrations:
            return {}, {}, []
        from lingtai.services import mcp as mcp_service
        from lingtai.services.mcp import HTTPMCPClient, MCPClient

        schemas: dict[str, FunctionSchema] = {}
        handlers: dict = {}
        clients: list[object] = []
        metadata: dict[str, dict] = {}
        licc_env = {"LINGTAI_AGENT_DIR": str(self._workdir.path)}
        try:
            for cfg in registrations:
                name = cfg["name"]
                if cfg["transport"] == "http":
                    client = HTTPMCPClient(
                        url=cfg["url"],
                        headers=cfg.get("headers"),
                    )
                else:
                    merged_env = {
                        **licc_env,
                        "LINGTAI_MCP_NAME": name,
                        **(cfg.get("env") or {}),
                    }
                    client = MCPClient(
                        command=cfg["command"],
                        args=cfg.get("args"),
                        env=merged_env,
                    )
                client.start()
                clients.append(client)
                # `list_tools` returns the complete SDK v2 tool record (paged to
                # completion by the service boundary). This in-process task
                # surface consumes the same contract as the agent adapter:
                # `schema` is `input_schema`, and everything FunctionSchema
                # cannot hold goes to the metadata sidecar below.
                for tool in client.list_tools():
                    tool_name = tool["name"]
                    if tool_name in schemas:
                        raise ValueError(f"duplicate MCP tool name: {tool_name}")
                    server_schema = tool.get("schema", {}) or {}
                    schema = dict(server_schema)
                    # Existing task FunctionSchema normalization; the original
                    # server schema remains the argument-boundary authority.
                    schema.pop("additionalProperties", None)
                    # Captured pre-normalization, deep-copied by the service
                    # helper so the sidecar never aliases the client's record.
                    metadata[tool_name] = mcp_service.tool_metadata(tool)

                    def _make_handler(c, tn: str, input_schema):
                        def handler(tool_args: dict) -> dict:
                            prepared = mcp_service.prepare_mcp_tool_arguments(
                                tool_args, input_schema
                            )
                            return c.call_tool(tn, prepared)
                        return handler

                    schemas[tool_name] = FunctionSchema(
                        name=tool_name,
                        description=tool.get("description", ""),
                        parameters=schema,
                    )
                    handlers[tool_name] = _make_handler(
                        client, tool_name, server_schema
                    )
        except Exception:
            self._close_task_mcp_clients(clients)
            # Publish nothing for a surface that never came up.
            self._task_mcp_tool_metadata = {}
            raise
        self._task_mcp_tool_metadata = metadata
        return schemas, handlers, clients

    def task_mcp_tool_metadata(self, name: str) -> dict | None:
        """Return advertised MCP metadata for one task-scoped tool, or ``None``.

        The returned mapping is a copy, so a caller cannot mutate the stored
        record through it. Entries live only as long as the task MCP clients
        started by ``_connect_task_mcp_registrations``; closing them via
        ``_close_task_mcp_clients`` clears the sidecar.
        """
        stored = getattr(self, "_task_mcp_tool_metadata", {}).get(name)
        return deepcopy(stored) if stored is not None else None

    def _close_task_mcp_clients(self, clients: list[object] | None) -> None:
        """Close task-scoped MCP clients and drop their metadata sidecar.

        Was a ``@staticmethod``; it now binds so teardown can clear the
        sidecar recorded by ``_connect_task_mcp_registrations``. Every existing
        call site already went through an instance (``self.`` or ``manager.``),
        so the call shape is unchanged.
        """
        for client in clients or []:
            try:
                client.close()
            except Exception:
                pass
        # Metadata describes the clients just closed; never outlive them.
        self._task_mcp_tool_metadata = {}

    def _task_skill_catalog(self, spec: dict) -> str | None:
        """Return rendered YAML skill context selected for one daemon task."""
        raw = spec.get("skills")
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise ValueError("skills must be an array of skill directory or SKILL.md paths")
        rows: list[dict] = []
        seen: set[Path] = set()
        for idx, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"skills[{idx}] must be a non-empty string path")
            skill_file = self._resolve_task_skill_path(item.strip(), self._workdir.path)
            if skill_file in seen:
                continue
            seen.add(skill_file)
            rows.append(self._parse_task_skill_file(skill_file))
        return self._render_task_skill_catalog(rows)

    @staticmethod
    def _resolve_task_file_path(raw_path: str, working_dir: Path) -> Path:
        """Resolve one daemon task file path to a contained absolute file.

        Relative paths resolve against the agent working directory; ``~`` is
        expanded. The fully-resolved path (symlinks followed) must stay inside
        the working directory, so a symlink escaping the root is out-of-root.
        """
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = working_dir / p
        p = p.resolve(strict=False)
        if not p.is_file():
            raise ValueError(f"task file path does not resolve to a file: {raw_path}")
        root = working_dir.resolve(strict=False)
        try:
            p.relative_to(root)
        except ValueError:
            raise ValueError(
                f"task file path is outside the agent working directory: {raw_path}"
            ) from None
        return p

    @staticmethod
    def _store_task_file_blob(store_dir: Path, sha256: str, data: bytes) -> Path:
        """Write one immutable content-addressed blob; return its path.

        The blob is named by its SHA-256 so identical bytes across tasks and
        dispatches share one file. ``os.replace`` makes the publish atomic: a
        torn write never leaves a trusted corrupt blob behind.
        """
        blob = store_dir / sha256
        if blob.exists():
            return blob
        store_dir.mkdir(parents=True, exist_ok=True)
        tmp = store_dir / f".{sha256}.tmp"
        tmp.write_bytes(data)
        try:
            os.replace(tmp, blob)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return blob

    def _task_files_store_dir(self) -> Path:
        """The internal content-addressed task input store for this agent."""
        return self._workdir.path / "daemons" / _TASK_FILES_STORE_DIR_NAME

    def _snapshot_task_files(self, spec: dict) -> list[dict] | None:
        """Validate ``spec['task_files']`` and plan its snapshot rows.

        Returns one manifest row per file (``path``, ``label``, ``role``,
        ``sha256``, ``size``, ``resolved``, ``snapshot``) or ``None`` when the
        task omits ``task_files``. Any malformed entry, out-of-root/missing
        path, oversize file, or non-UTF-8 file raises ``ValueError`` so the
        caller can refuse the whole batch before any run-dir creation or
        scheduling — task input never silently falls back to the mutable
        original path. This phase writes nothing; the caller materializes the
        validated batch (``_materialize_task_files``) only after every task in
        the dispatch has passed, so a refused batch leaves no store side
        effects.
        """
        raw = spec.get("task_files")
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise ValueError(
                "task_files must be an array of {path, label?, role?} objects"
            )
        if len(raw) > TASK_FILES_MAX_PER_TASK:
            raise ValueError(
                f"task_files exceeds the {TASK_FILES_MAX_PER_TASK}-file limit"
            )
        rows: list[dict] = []
        store_dir = self._task_files_store_dir()
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"task_files[{idx}] must be an object with path")
            path = item.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ValueError(f"task_files[{idx}].path must be a non-empty string")
            label = item.get("label")
            if label is not None and (
                not isinstance(label, str)
                or len(label) > _TASK_FILES_ANNOTATION_MAX_CHARS
            ):
                raise ValueError(
                    f"task_files[{idx}].label must be a string of at most "
                    f"{_TASK_FILES_ANNOTATION_MAX_CHARS} characters"
                )
            role = item.get("role")
            if role is not None and (
                not isinstance(role, str)
                or len(role) > _TASK_FILES_ANNOTATION_MAX_CHARS
            ):
                raise ValueError(
                    f"task_files[{idx}].role must be a string of at most "
                    f"{_TASK_FILES_ANNOTATION_MAX_CHARS} characters"
                )
            resolved = self._resolve_task_file_path(
                path.strip(), self._workdir.path
            )
            try:
                data = resolved.read_bytes()
            except OSError as e:
                raise ValueError(
                    f"cannot read task file {path!r}: {e}"
                ) from e
            if len(data) > TASK_FILE_MAX_BYTES:
                raise ValueError(
                    f"task file {path!r} exceeds the {TASK_FILE_MAX_BYTES}-byte "
                    "limit"
                )
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ValueError(
                    f"task file {path!r} is not valid UTF-8 text: {e}"
                ) from e
            # A NUL makes the payload a binary blob for this text-only contract
            # even though Python's UTF-8 decoder accepts it.
            if b"\x00" in data:
                raise ValueError(f"task file {path!r} is not valid UTF-8 text: contains NUL byte")
            sha256 = hashlib.sha256(data).hexdigest()
            rows.append({
                "path": path.strip(),
                "label": label,
                "role": role,
                "sha256": sha256,
                "size": len(data),
                "resolved": str(resolved),
                "snapshot": str(store_dir / sha256),
            })
        return rows or None

    def _materialize_task_files(
        self, group_id: str, rows_per_task: list[list[dict] | None]
    ) -> str:
        """Write the validated batch's blobs and one compact per-dispatch manifest.

        Runs only after every task in the dispatch passed validation. The
        manifest is indexed by task order (``None`` for a task without
        ``task_files``). Each run's durable ``call_parameters.task_files``
        points at this manifest plus its own rows, so retry/relaunch reads the
        immutable snapshot instead of any mutable original path.
        """
        store_dir = self._task_files_store_dir()
        # Verify every original first. Only after the whole batch still matches
        # preflight do we publish any content-addressed blob, so an intervening
        # source mutation cannot leave a partial task-files store behind.
        blobs: dict[str, bytes] = {}
        for rows in rows_per_task:
            if not rows:
                continue
            for r in rows:
                resolved = Path(r["resolved"])
                data = resolved.read_bytes()
                # TOCTOU guard: the mutable original must still be byte-for-byte
                # what preflight validated; anything else fails loudly rather
                # than silently snapshotting different content than dispatched.
                if hashlib.sha256(data).hexdigest() != r["sha256"]:
                    raise ValueError(
                        f"task file changed during dispatch: {r['path']!r}"
                    )
                blobs.setdefault(r["sha256"], data)
        for sha256, data in blobs.items():
            self._store_task_file_blob(store_dir, sha256, data)
        manifest = {
            "version": _TASK_FILES_MANIFEST_VERSION,
            "group_id": group_id,
            "files": rows_per_task,
        }
        path = store_dir / f"manifest-{group_id}.json"
        atomic_write_json(path, manifest)
        return str(path)

    @staticmethod
    def _render_task_files_catalog(rows: list[dict] | None) -> str | None:
        """Render the compact read-only manifest rows for the daemon prompt.

        Only metadata and the immutable snapshot paths are rendered — never
        file contents, and never the mutable original paths the worker could
        re-read behind the snapshot's back.
        """
        if not rows:
            return None
        lines = [
            "The parent snapshotted these task input files into a read-only "
            "store; read them from the given snapshot paths when your task "
            "requires them (the original paths may change or disappear):",
            "task_files:",
        ]
        for r in rows:
            lines.append(f"  - label: {r['label'] or r['path']}")
            if r.get("role"):
                lines.append(f"    role: {r['role']}")
            lines.append(f"    sha256: {r['sha256']}")
            lines.append(f"    size: {r['size']}")
            lines.append(f"    snapshot: {r['snapshot']}")
        return "\n".join(lines)

    @staticmethod
    def _combine_oneshot_context(
        system_prompt: str | None,
        skill_catalog: str | None,
        mcp_catalog: str | None = None,
        plugin_catalog: str | None = None,
        task_files_catalog: str | None = None,
    ) -> str | None:
        parts = []
        if system_prompt:
            parts.append(system_prompt)
        if skill_catalog:
            parts.append("## Parent-selected skills\n" + skill_catalog)
        if mcp_catalog:
            parts.append("## Parent-provided MCP registrations\n" + mcp_catalog)
        if plugin_catalog:
            parts.append("## Parent-selected plugins\n" + plugin_catalog)
        if task_files_catalog:
            parts.append("## Parent-provided task files\n" + task_files_catalog)
        return "\n\n".join(parts) or None

    @staticmethod
    def _task_first_prompt(spec: dict) -> str:
        value = spec.get("prompt")
        if value is None or (isinstance(value, str) and not value.strip()):
            return "Begin the assigned daemon task."
        if not isinstance(value, str):
            raise ValueError("prompt must be a string")
        return value

    @staticmethod
    def _compose_cli_task(
        task: str, system_prompt: str | None, backend: str | None = None,
    ) -> str:
        """Embed a daemon oneshot prompt into a CLI backend task string.

        The daemon common MCP completion contract, when present, is part of
        ``system_prompt``. CLI backends receive it in-band because most CLI
        harnesses accept a single task string rather than a separate system
        prompt channel.
        """
        if not system_prompt:
            return task

        parts = []
        if system_prompt:
            parts.append(
                "Parent-provided daemon context (oneshot; bounded to this "
                "daemon run and unable to override tool/safety limits):\n"
                f"{system_prompt}"
            )
        parts.append(f"Task:\n{task}")
        return "\n\n".join(parts)

    @staticmethod
    def _daemon_codex_session_anchor(run_dir) -> str:
        """Return the per-run Codex cache-affinity anchor for a daemon."""
        return str((run_dir.path / "daemon.json").resolve())

    def _daemon_provider_defaults(
        self,
        provider: str,
        base_defaults: dict | None,
        run_dir,
        *,
        context_token_limit: int | None = None,
    ) -> dict | None:
        """Return provider defaults for a daemon-scoped LLM service.

        Daemon-scoped services preserve the parent/preset provider defaults for
        every provider, so a non-Codex daemon keeps the same adapter behavior as
        its parent. Codex is the one exception: the normal Codex agent path uses
        the agent's resolved ``init.json`` path as its cache-affinity anchor, but
        a LingTai daemon is a disposable run, so Codex daemon calls need a per-run
        anchor rather than the parent agent's anchor; otherwise parent and child
        traffic collide in one REST cache slot.

        context_token_limit: the task's optional ``context_token_limit``.
        Meaningful for a Codex-family provider, where it becomes
        ``codex_compact_token_limit`` — the standalone-compaction threshold
        consulted by ``CodexOpenAIAdapter``/``CodexResponsesSession`` — and
        for the native ``mimo`` provider, where it becomes
        ``mimo_compact_token_limit`` — the same standalone-compaction axis
        consulted by ``MimoAdapter``/``MimoResponsesSession`` (see
        ``src/lingtai/llm/mimo/ANATOMY.md``). Omitted for every other
        provider so their adapter construction is unaffected.
        """
        provider_key = str(provider).lower()
        bucket = dict(base_defaults or {})
        if provider_key in ("codex", "codex-pool", "codex_pool"):
            # Daemon traffic must use the daemon run identity so it gets its own
            # cache slot, not the parent agent's anchor. ``codex-pool`` reuses the
            # Codex adapter and also seeds its sticky auth-pool choice off this
            # anchor, so a daemon run selects independently of its parent.
            bucket["codex_session_anchor"] = self._daemon_codex_session_anchor(run_dir)
            if context_token_limit is not None:
                bucket["codex_compact_token_limit"] = context_token_limit
        elif provider_key == "mimo" and context_token_limit is not None:
            bucket["mimo_compact_token_limit"] = context_token_limit
        if not bucket:
            return None
        return {provider_key: bucket}

    @staticmethod
    def _llm_defaults_from_manifest(llm: dict) -> dict:
        """Extract adapter-consulted defaults from a preset ``manifest.llm``."""
        keys = (
            "api_compat",
            "base_url",
            "codex_auth_path",
            "codex_auth_pool_path",
            "codex_session_anchor",
            "codex_thread_salt",
            "compact_threshold",
            "default_headers",
            "max_rpm",
            "wire_api",
        )
        return {key: llm[key] for key in keys if key in llm}

    def _implicit_parent_preset_llm(self) -> dict:
        """Materialize the parent service into an implicit/effective preset.

        A LingTai daemon always runs from an effective preset. When a task does
        not name one, the parent agent's existing LLM configuration *is* the
        implicit preset: same provider/model/base_url, the credential the parent
        actually built its boot adapter with (``parent_service.api_key``), and
        the parent's own provider-defaults bucket carried through unchanged.

        This deliberately does NOT run fallback key resolution for the daemon's
        primary key — the parent already resolved that at boot, so the daemon
        inherits the same effective key directly rather than re-deriving it from
        a guessed ``{PROVIDER}_API_KEY`` env slot. The parent's ``key_resolver``
        is still carried so on-demand adapters for *other* providers behave like
        the parent's; it is never consulted for this preset's primary key.

        Extra non-manifest fields (``key_resolver``, ``context_window``, and the
        verbatim ``_provider_defaults`` bucket) ride along so the shared preset
        construction path in ``_run_emanation`` can mirror the parent service.
        The api_key is never logged or persisted.
        """
        parent_service = self._runtime.service
        provider = str(getattr(parent_service, "provider", "")).lower()
        parent_defaults = getattr(parent_service, "_provider_defaults", {}) or {}
        if not isinstance(parent_defaults, dict):
            parent_defaults = {}
        provider = provider if isinstance(provider, str) else ""
        model = getattr(parent_service, "model", "unknown")
        if not isinstance(model, str):
            model = str(model)
        base_url = getattr(parent_service, "_base_url", None)
        if base_url is not None and not isinstance(base_url, str):
            base_url = str(base_url)
        from lingtai.llm.service import CONSERVATIVE_CONTEXT_WINDOW

        context_window = getattr(parent_service, "_context_window", None)
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            context_window = CONSERVATIVE_CONTEXT_WINDOW
        bucket = parent_defaults.get(provider, {})
        if not isinstance(bucket, dict):
            bucket = {}
        return {
            "provider": provider,
            "model": model,
            "api_key": getattr(parent_service, "api_key", None),
            "base_url": base_url,
            "key_resolver": getattr(parent_service, "_key_resolver", None),
            "context_window": context_window,
            # Parent provider defaults carried verbatim (not re-derived through
            # the manifest key list) so any provider-specific field survives.
            "_provider_defaults": dict(bucket),
        }

    def _build_tool_surface(
        self,
        requested: list[str],
        preset_surface: tuple[dict, dict] | None = None,
        mcp_surface: tuple[dict[str, FunctionSchema], dict] | None = None,
    ) -> tuple[list[FunctionSchema], dict]:
        """Build filtered tool schemas and dispatch map for an emanation.

        When ``preset_surface`` is provided (preset-driven emanation), the
        preset's pre-instantiated sandbox supplies the child LLM's
        provider-specific capabilities (``preset_surface =
        (schemas_by_name, handlers_by_name)``), but it does NOT replace the
        parent's always-on host tool floor. Only that narrow floor — ``shell``
        and ``file`` (whose actions are read/write/edit/glob/grep), which the
        preset wizard omits from ``manifest.capabilities`` — stays available
        from the parent,
        so requested host tools are not rejected as unknown just because a
        preset was supplied. Optional/provider parent tools (vision,
        web_search, …) are NOT borrowable; they must come from the preset's own
        sandbox. See ``_parent_host_tool_floor``. Resolution is preset-first
        (a tool the preset re-instantiated against the child LLM wins), then
        intrinsics/task MCP, then the parent host surface fills in. Parent MCP
        tools still do not auto-inherit; task ``mcp`` provides complete one-run
        MCP registrations whose tools are added through ``mcp_surface``. When
        ``preset_surface`` is None, the parent's currently registered regular
        capability surface is used, again plus only task-scoped MCP tools.
        """
        tool_names = self._expand_requested_tools(requested)
        # LingTai daemon self-compact is an always-available daemon runtime
        # tool. It is not a parent capability, preset capability, MCP tool, or
        # provider-native compaction feature, so make it automatic at this one
        # LingTai surface owner instead of requiring public tools:["compact"].
        tool_names.add("compact")

        intrinsic_schemas, intrinsic_handlers = self._daemon_intrinsic_surface()
        mcp_schemas, mcp_handlers = mcp_surface or ({}, {})
        parent_mcp_names = self._parent_mcp_tool_names()
        reserved_names = ({s.name for s in self._runtime.tool_schemas} - parent_mcp_names) | set(intrinsic_schemas)
        mcp_collisions = set(mcp_schemas) & reserved_names
        if mcp_collisions:
            raise ValueError(
                "Task MCP tools collide with existing parent/daemon tools: "
                f"{sorted(mcp_collisions)}"
            )
        parent_mcp_requested = (tool_names & self._parent_mcp_tool_names()) - set(mcp_schemas)
        if parent_mcp_requested:
            raise ValueError(
                "Parent MCP tools must be provided as task mcp registrations, "
                f"not requested via tools: {sorted(parent_mcp_requested)}"
            )

        if preset_surface is not None:
            preset_schemas, preset_handlers = preset_surface
            # A preset selects the child LLM + provider-specific capabilities;
            # it does NOT re-declare the parent's always-on CORE_DEFAULTS host
            # floor (shell / file), because the
            # preset wizard only writes overrides/opt-ins into
            # manifest.capabilities. So those floor tools must remain available
            # — they must not become "unknown" just because a preset was
            # supplied. The borrowable floor is NARROW on purpose: only the
            # always-on host primitives, never optional/provider parent tools
            # (vision, web_search, …) — those must come from the preset's own
            # sandbox so a preset that omits/fails a provider cap does NOT
            # silently fall back to the parent. Parent MCP tools likewise do
            # NOT auto-inherit (they must come through task `mcp` registrations);
            # that exclusion is enforced by the ``parent_mcp_requested`` guard
            # above and by the floor's exclusion of `mcp`.
            parent_schema_map = {s.name: s for s in self._runtime.tool_schemas}
            parent_host_names = (set(parent_schema_map)
                                 & _parent_host_tool_floor()) - parent_mcp_names
            # Available surface = preset capabilities ∪ parent host-floor tools
            # ∪ task-scoped MCP tools ∪ names satisfied by a LingTai-auto-
            # mounted MCP server (email — see _DAEMON_AUTO_MCP_TOOL_NAMES).
            # Only task MCP tools are auto-included below.
            available = (set(preset_schemas.keys()) | parent_host_names
                         | set(mcp_schemas) | set(intrinsic_schemas)
                         | _DAEMON_AUTO_MCP_TOOL_NAMES)
            # Task MCP tools are auto-included for this one run because the
            # task supplied full one-run registrations. Auto-mounted daemon
            # MCP tools such as email remain available, but must be
            # explicitly requested via `tools` (which gates whether
            # _with_daemon_email_mcp mounts the server at all) so result-only
            # `tools=[]` daemons cannot communicate.
            tool_names |= set(mcp_schemas)

            missing = tool_names - available
            if missing:
                raise ValueError(f"Unknown tools for emanation: {missing}")

            # Build merged schemas + dispatch. Resolution order is preset-first
            # so a capability the preset re-instantiated (configured against the
            # child LLM) wins over the parent; intrinsics and task MCP next; the
            # parent host surface fills in for floor tools the preset omitted.
            schemas: list[FunctionSchema] = []
            dispatch: dict = {}
            for n in sorted(tool_names):
                if n in preset_schemas:
                    schemas.append(preset_schemas[n])
                    if n in preset_handlers:
                        dispatch[n] = preset_handlers[n]
                elif n in intrinsic_schemas:
                    schemas.append(intrinsic_schemas[n])
                    if n in intrinsic_handlers:
                        dispatch[n] = intrinsic_handlers[n]
                elif n in mcp_schemas:
                    schemas.append(mcp_schemas[n])
                    if n in mcp_handlers:
                        dispatch[n] = mcp_handlers[n]
                elif n in parent_schema_map:
                    schemas.append(parent_schema_map[n])
                    if n in self._runtime.tool_handlers:
                        dispatch[n] = self._runtime.tool_handlers[n]
            return schemas, dispatch

        # Default path: emanation runs on the parent's capability surface plus
        # task-scoped MCP tools from full registrations.
        tool_names |= set(mcp_schemas)

        # Validate requested tools exist
        available = ({s.name for s in self._runtime.tool_schemas}
                     | set(intrinsic_schemas) | set(mcp_schemas)
                     | _DAEMON_AUTO_MCP_TOOL_NAMES)
        missing = tool_names - available
        if missing:
            raise ValueError(f"Unknown tools for emanation: {missing}")

        # Build schemas and dispatch
        schema_map = {s.name: s for s in self._runtime.tool_schemas}
        schemas = []
        for n in sorted(tool_names):
            if n in intrinsic_schemas:
                schemas.append(intrinsic_schemas[n])
            elif n in mcp_schemas:
                schemas.append(mcp_schemas[n])
            elif n in schema_map:
                schemas.append(schema_map[n])
        dispatch = {n: self._runtime.tool_handlers[n]
                    for n in tool_names if n in self._runtime.tool_handlers}
        for n in tool_names:
            if n in mcp_handlers:
                dispatch[n] = mcp_handlers[n]
            if n in intrinsic_handlers:
                dispatch[n] = intrinsic_handlers[n]
        return schemas, dispatch


    def _parent_mcp_tool_names(self) -> set[str]:
        """Return parent MCP tool names through Daemon's narrow runtime port."""
        return set(self._runtime.mcp_tool_names)

    def _expand_requested_tools(self, requested: list[str]) -> set[str]:
        """Expand requested daemon tools after group aliases and blacklist."""
        from lingtai.tools.registry import canonical_capability_name

        tool_names: set[str] = set()
        for name in requested:
            name = canonical_capability_name(name)
            if name in EMANATION_BLACKLIST:
                continue
            tool_names.add(name)
        return tool_names

    def _instantiate_preset_capabilities(
        self,
        preset_caps: dict,
        preset_llm: dict,
        required_tools: set[str] | None = None,
    ) -> tuple[dict, dict]:
        """Instantiate a preset's manifest.capabilities into a sandbox.

        Returns ``(schemas_by_name, handlers_by_name)``. Capabilities run
        their ``setup()`` against a ``_CapabilitySandbox`` so the parent's
        own tool registry is not mutated. ``provider: "inherit"`` sentinels
        in the preset's capability kwargs resolve against the *preset's*
        LLM, not the parent's — capabilities follow the body that hosts
        them.

        Raises ``ValueError`` for broken capabilities that are required by
        the current task. Broken unused capabilities are logged and skipped.
        The caller (``_handle_emanate``) converts required setup failures into
        a tool-level error and refuses the whole batch.
        """
        from lingtai.tools.registry import (
            BUILTIN_TOOLS,
            canonical_capability_name,
        )
        from lingtai.presets import expand_inherit

        # Resolve provider:"inherit" sentinels against the preset's LLM
        # (not the parent's). expand_inherit mutates in place — work on a
        # deep enough copy so the original preset dict is unchanged.
        import copy
        resolved = copy.deepcopy(preset_caps)
        expand_inherit(resolved, preset_llm)

        # Capability groups no longer exist: every name here is either a real
        # capability or an unknown one that ``setup_capability`` rejects.
        expanded: dict = dict(resolved)

        collected_schemas: dict = {}
        collected_handlers: dict = {}
        required = required_tools
        for name, kwargs in expanded.items():
            name = canonical_capability_name(name)
            if name in EMANATION_BLACKLIST:
                continue
            # Tolerate non-capability names (intrinsics like 'psyche',
            # 'system', 'soul' — kernel always-on, not composable). The TUI
            # preset wizard writes these into manifest.capabilities and the
            # main Agent.__init__ tolerates them via try/except (agent.py:91-94);
            # the daemon sandbox must replicate that tolerance or "full" user
            # presets become unusable as daemon presets. See lingtai #29.
            if name not in BUILTIN_TOOLS:
                self._log(
                    "daemon_preset_capability_skipped",
                    capability=name,
                    reason="not a composable capability (intrinsic or unknown)",
                )
                continue
            if not isinstance(kwargs, dict):
                kwargs = {}
            try:
                schemas, handlers = self._runtime.setup_preset_capability(name, kwargs)
                collected_schemas.update(schemas)
                collected_handlers.update(handlers)
            except Exception as e:
                if required is not None and name not in required:
                    self._log(
                        "daemon_preset_capability_skipped",
                        capability=name,
                        reason=f"setup failed: {e}",
                    )
                    continue
                raise ValueError(
                    f"preset capability {name!r} failed to set up: {e}"
                ) from e

        return collected_schemas, collected_handlers

    def _build_emanation_prompt(
        self,
        task: str,
        schemas: list[FunctionSchema],
        system_prompt: str | None = None,
    ) -> str:
        """Build the system prompt for an emanation."""
        return _build_emanation_prompt_standalone(
            self._runtime.language,
            task,
            schemas,
            system_prompt=system_prompt,
            # Production managers resolve this in __init__.  Retain the
            # renderer's established default for lightweight test facades that
            # bind this helper directly without constructing a manager.
            system_prompt_budget_chars=getattr(
                self, "_system_prompt_budget_chars", DAEMON_SYSTEM_PROMPT_BUDGET_CHARS
            ),
        )

    _SUPERVISOR_STARTUP_TIMEOUT_S = 5.0
    _SUPERVISOR_STARTUP_POLL_S = 0.05

    def _resolve_manifest_llm(self, effective_llm: dict) -> dict:
        """Flatten an effective preset ``llm`` block into a JSON-serializable dict.

        The in-process path threads ``key_resolver`` (a live callable) and a
        ``_provider_defaults``/``provider_defaults`` bucket through untouched.
        A detached supervisor cannot receive a live callable across a process
        boundary, so the primary API key is resolved HERE (in this process,
        where ``resolve_env``/the parent's already-resolved key are still
        available) and only the resolved flat value crosses into the
        manifest — the same secret-handling boundary the manifest module's
        docstring describes (manifest lives beside daemon.json / native CLI
        config files). ``key_resolver`` itself (for on-demand *other*-provider
        adapters) is intentionally NOT carried — a detached lingtai run in
        this slice never needs an on-demand adapter for a provider other than
        its own.
        """
        # Never resolve the primary credential in the parent and never copy it
        # into durable run state.  The detached host resolves this reference in
        # its inherited environment/config at execution time.
        api_key_env = effective_llm.get("api_key_env")
        if "_provider_defaults" in effective_llm:
            base_defaults = effective_llm["_provider_defaults"]
        else:
            base_defaults = self._llm_defaults_from_manifest(effective_llm)
        context_window = effective_llm.get("context_limit")
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            context_window = effective_llm.get("context_window")
        return {
            "provider": effective_llm["provider"],
            "model": effective_llm["model"],
            "api_key_env": api_key_env,
            "base_url": effective_llm.get("base_url"),
            "context_window": context_window,
            "provider_defaults": base_defaults or None,
        }

    def _spawn_detached_lingtai_run(
        self,
        run_dir: DaemonRunDir,
        *,
        task: str,
        tools: list[str],
        max_turns: int,
        timeout_s: float,
        group_id: str | None,
        effective_llm: dict,
        context_token_limit: int | None,
        prompt: str,
        mcp: list[dict] | None = None,
        preset_name: str | None = None,
        preset_llm: dict | None = None,
        preset_capabilities: dict | None = None,
        secret_capsule: dict | None = None,
        authority_lease=None,
        use_central_manager: bool = False,
    ) -> None:
        """Write the run manifest and spawn an already-authorized detached run.

        The caller must authorize the derived launch before committing it to
        the canonical dispatch ledger. Raises on any post-admission failure
        (unwritable manifest, spawn error, or a startup handshake timeout) so
        the caller can mark the run failed cleanly — never claims a detached
        run started when it did not.
        """
        from lingtai.kernel.daemon_supervisor import DaemonSupervisorRequest
        from lingtai.kernel.daemon_supervisor.manifest import build_manifest, write_manifest

        resolved_llm = self._resolve_manifest_llm(effective_llm)
        resolved_llm["provider_defaults"] = self._daemon_provider_defaults(
            effective_llm["provider"],
            resolved_llm["provider_defaults"],
            run_dir,
            context_token_limit=context_token_limit,
        )

        manifest = build_manifest(
            run_id=run_dir.run_id,
            backend="lingtai",
            parent_working_dir=str(self._workdir.path),
            run_dir=str(run_dir.path),
            task=task,
            prompt=prompt,
            tools=list(tools),
            max_turns=max_turns,
            timeout_s=timeout_s,
            group_id=group_id,
            context_token_limit=context_token_limit,
            llm=resolved_llm,
            mcp=mcp,
            language=self._runtime.language,
            preset_name=preset_name,
            preset_llm=preset_llm,
            preset_capabilities=preset_capabilities,
        )
        write_manifest(run_dir.path, manifest)

        from lingtai.kernel.daemon_supervisor.manifest import manifest_path_for
        from .supervisor_runtime import select_daemon_supervisor_adapter

        request = DaemonSupervisorRequest(
            run_id=run_dir.run_id,
            manifest_path=str(manifest_path_for(run_dir.path)),
            python_executable=sys.executable,
        )
        capsule = dict(secret_capsule or {})
        capsule.setdefault("task", task)
        capsule.setdefault("prompt", prompt)
        capsule.setdefault("mcp", list(mcp or []))
        if authority_lease is not None:
            # This is a fail-closed delivery expectation, not a bearer. The
            # actual endpoint travels only as SCM_RIGHTS through the B8a
            # capsule wire and never enters argv, env, or durable state.
            capsule["driver_authority_required"] = True
        runtime_llm = {}
        # Resolve an explicit api_key_env reference in the parent while its
        # environment/config is available, then carry only the resulting value
        # through the ephemeral capsule.  The reference itself remains public in
        # the manifest; the value never enters argv, the child environment, or
        # durable state.  Implicit parent presets already carry the parent's
        # resolved key in ``api_key``.
        from lingtai.kernel.config_resolve import resolve_env
        resolved_key = resolve_env(
            effective_llm.get("api_key"), effective_llm.get("api_key_env")
        )
        if isinstance(resolved_key, str):
            runtime_llm["api_key"] = resolved_key
        # The durable manifest always exposes the normalized public key
        # ``provider_defaults``.  Effective in-process presets may instead carry
        # their raw provider bucket under the private alias
        # ``_provider_defaults``; copying that alias into the capsule would leave
        # the public redacted map untouched in the execution child.  Overlay the
        # exact normalized/per-run map that was used to build the manifest.
        normalized_defaults = resolved_llm.get("provider_defaults")
        if isinstance(normalized_defaults, dict):
            runtime_llm["provider_defaults"] = normalized_defaults
        if runtime_llm:
            capsule.setdefault("llm", {}).update(runtime_llm)
        adopted_fd = self._consume_driver_authority_lease_for_posix_handoff(
            authority_lease
        )
        try:
            if use_central_manager:
                enqueue_kwargs = {
                    "capsule": capsule,
                    "pool_size": self._manager_pool_size,
                    "run_dir": run_dir,
                }
                if adopted_fd is not None:
                    enqueue_kwargs["adopted_fd"] = adopted_fd
                # The public manager boundary owns the descriptor at call
                # entry, including when it closes it and then raises. Drop
                # this caller's stale integer before invoking it so the outer
                # cleanup cannot close a number another resource reuses.
                adopted_fd = None
                self._enqueue_central_daemon_manager_run(request, **enqueue_kwargs)
            else:
                supervisor = select_daemon_supervisor_adapter()
                spawn_kwargs = {"capsule": capsule}
                if adopted_fd is not None:
                    spawn_kwargs["adopted_fd"] = adopted_fd
                # The public adapter boundary owns the descriptor at call
                # entry, including its failure paths. Drop this caller's
                # stale integer before it can close and raise.
                adopted_fd = None
                supervisor.spawn_detached(request, **spawn_kwargs)
                self._await_supervisor_startup(run_dir)
        finally:
            if adopted_fd is not None:
                try:
                    os.close(adopted_fd)
                except OSError:
                    pass

    def _await_supervisor_startup(self, run_dir: DaemonRunDir) -> None:
        """Bounded poll for the supervisor to record its own PID.

        ``supervisor_runtime.run_supervisor`` writes ``daemon.json.supervisor_pid``
        immediately after attaching its ``DaemonRunDir``, before doing any
        real work. This is the caller-side half of the startup handshake the
        Port's docstring describes: the Port itself returns as soon as the
        OS process is launched, but the caller still needs truthful evidence
        the process actually reached Python and attached to this run before
        claiming ``dispatched`` success.
        """
        deadline = time.monotonic() + self._SUPERVISOR_STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            # This process's own `run_dir` object was constructed BEFORE the
            # supervisor subprocess existed; its in-memory `_state` never
            # observes that other process's writes. Only a fresh disk read
            # can see the supervisor's own daemon.json update.
            try:
                state = DaemonRunDir.read_state_from_disk(run_dir.path)
            except (OSError, json.JSONDecodeError, ValueError):
                time.sleep(self._SUPERVISOR_STARTUP_POLL_S)
                continue
            if state.get("supervisor_pid"):
                return
            if state.get("state") in ("done", "failed", "cancelled", "timeout"):
                # Supervisor started, ran, and already finished (or crashed
                # fast) before this poll observed the pid write — still a
                # real start, not a hang.
                return
            time.sleep(self._SUPERVISOR_STARTUP_POLL_S)
        raise RuntimeError(
            f"detached daemon supervisor for run {run_dir.run_id!r} did not "
            f"start within {self._SUPERVISOR_STARTUP_TIMEOUT_S}s"
        )

    def _should_use_central_daemon_manager(self, batch_count: int) -> bool:
        """Return whether this batch should use the explicit Phase 1 manager."""
        return (
            os.name == "posix"
            and self._manager_pool_size > 0
        )

    def _enqueue_central_daemon_manager_run(
        self,
        request,
        *,
        capsule: dict,
        pool_size: int,
        run_dir: DaemonRunDir,
        adopted_fd: int | None = None,
    ) -> None:
        """Submit one already-materialized run to the resident POSIX manager.

        The manager path intentionally does not wait for ``supervisor_pid`` here:
        queued runs have no manager assignment, and therefore no pid, until pool
        capacity frees up.
        """
        try:
            from lingtai.adapters.posix.daemon_manager import enqueue_manager_run

            agent_working_dir = self._workdir.path
            # The lower public boundary owns this descriptor from its call
            # entry. Keep it here through all wrapper setup, then relinquish
            # it immediately before that call.
            transferred_fd = adopted_fd
            adopted_fd = None
            enqueue_manager_run(
                agent_working_dir=agent_working_dir,
                request=request,
                capsule=capsule,
                pool_size=pool_size,
                adopted_fd=transferred_fd,
            )
        finally:
            if adopted_fd is not None:
                try:
                    os.close(adopted_fd)
                except OSError:
                    pass

    def _run_emanation(self, em_id: str, run_dir, schemas, dispatch,
                       task: str,
                       cancel_event: threading.Event,
                       timeout_event: threading.Event | None = None,
                       preset_llm: dict | None = None,
                       max_turns: int | None = None,
                       mcp_clients: list[object] | None = None,
                       context_token_limit: int | None = None,
                       prompt: str | None = None) -> str:
        """Run a single emanation's tool loop. Called in a worker thread.

        run_dir is the DaemonRunDir constructed in _handle_emanate. All
        filesystem effects flow through it.

        timeout_event distinguishes watchdog-fired cancellation (timeout) from
        manual reclaim. When set alongside cancel_event, the run loop calls
        mark_timeout instead of mark_cancelled. None is allowed for direct-call
        tests and the cancellation defaults to "cancelled" semantics.

        preset_llm: the per-task preset's ``manifest.llm`` block when the task
        explicitly named a preset (keys provider/model/api_key_env/base_url and
        optionally api_key). When the task omits ``preset``, the parent agent's
        existing LLM configuration is used as an implicit/effective preset (see
        ``_implicit_parent_preset_llm``). Either way a dedicated daemon-scoped
        LLMService is built from the effective preset — the daemon never reuses
        ``self._runtime.service`` directly and never runs provider-name env-var
        fallback resolution for its primary key.

        context_token_limit: the task's optional ``context_token_limit``
        (already validated as a positive int by ``_handle_emanate``'s
        pre-flight gate). Consulted for a Codex-family provider or the native
        ``mimo`` provider — see ``_daemon_provider_defaults``; every other
        provider ignores it, and every external CLI backend never reaches
        this method at all.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        # A LingTai daemon always runs from an effective preset: the task's
        # explicit preset if it supplied one, otherwise the parent agent's
        # current LLM configuration synthesized into an implicit preset. Both
        # flow through the same daemon-scoped service construction below.
        effective_preset_llm = preset_llm or self._implicit_parent_preset_llm()

        from lingtai.llm.service import LLMService
        from lingtai.kernel.config_resolve import resolve_env

        provider = effective_preset_llm["provider"]
        effective_model = effective_preset_llm["model"]
        # Primary key: the preset's direct api_key (resolved from its api_key_env
        # only, never a guessed provider-name slot). For the implicit preset this
        # is the parent's already-resolved effective key. Never logged/persisted.
        api_key = resolve_env(
            effective_preset_llm.get("api_key"),
            effective_preset_llm.get("api_key_env"),
        )
        # Base provider defaults: the implicit preset carries the parent bucket
        # verbatim; an explicit preset derives them from its manifest.llm fields.
        # Codex daemons additionally get a per-run cache anchor (set inside
        # _daemon_provider_defaults) so they don't collide with the parent slot.
        if "_provider_defaults" in effective_preset_llm:
            base_defaults = effective_preset_llm["_provider_defaults"]
        else:
            base_defaults = self._llm_defaults_from_manifest(effective_preset_llm)
            # A detached child receives the provider bucket under the public
            # ``provider_defaults`` key (the private ``_provider_defaults`` alias
            # never crosses the process boundary). Merge the nested bucket so
            # provider-specific fields such as ``wire_api`` / ``api_compat`` /
            # ``max_rpm`` survive the reconstruction; without this a Responses
            # provider degrades to ``auto`` and is misrouted to Chat Completions.
            public_defaults = effective_preset_llm.get("provider_defaults")
            if isinstance(public_defaults, dict):
                nested = public_defaults.get(provider)
                if not isinstance(nested, dict):
                    nested = public_defaults.get(str(provider).lower())
                if isinstance(nested, dict) and nested:
                    merged = dict(base_defaults)
                    merged.update(nested)
                    base_defaults = merged
        from lingtai.llm.service import CONSERVATIVE_CONTEXT_WINDOW

        context_window = effective_preset_llm.get("context_limit")
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            context_window = effective_preset_llm.get("context_window")
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            context_window = CONSERVATIVE_CONTEXT_WINDOW
        service_kwargs = {
            "provider": provider,
            "model": effective_model,
            "api_key": api_key,
            "base_url": effective_preset_llm.get("base_url"),
            "provider_defaults": self._daemon_provider_defaults(
                provider, base_defaults, run_dir,
                context_token_limit=context_token_limit,
            ),
        }
        # Implicit-preset-only pass-throughs that mirror the parent service:
        # the key_resolver (for on-demand adapters of *other* providers — never
        # the primary key) and the parent's context window.
        if "key_resolver" in effective_preset_llm:
            service_kwargs["key_resolver"] = effective_preset_llm["key_resolver"]
        service_kwargs["context_window"] = context_window
        service = LLMService(**service_kwargs)
        provider_call_admission_port = getattr(
            self, "_provider_call_admission_port", None
        )
        if provider_call_admission_port is not None:
            from lingtai.kernel.provider_admission import ProviderAdmittedLLMService

            service = ProviderAdmittedLLMService(
                service, provider_call_admission_port
            )

        session = service.create_session(
            system_prompt=run_dir.prompt_path.read_text(encoding="utf-8"),
            tools=schemas or None,
            model=effective_model,
            thinking="default",
            tracked=False,
        )

        system_prompt = run_dir.prompt_path.read_text(encoding="utf-8")
        effective_max_turns = max_turns if max_turns is not None else self._max_turns
        context_window = 0
        session_context_window = getattr(session, "context_window", 0)
        if callable(session_context_window):
            try:
                session_context_window = session_context_window()
            except Exception:
                session_context_window = 0
        for candidate in (
            session_context_window,
            getattr(service, "_context_window", 0),
            context_window,
        ):
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                context_window = candidate
                break
        daemon_meta_state = _DaemonMetaState(
            em_id,
            getattr(run_dir, "run_id", em_id),
            max_turns=effective_max_turns,
            context_window=context_window,
            system_prompt=system_prompt,
        )
        compact_batch_allowed = False
        compact_reset_accepted = False

        endpoint = getattr(service, "_base_url", None)
        daemon_summarizer_fn = _build_daemon_apriori_summarizer_fn(
            service, run_dir, provider=provider, model=effective_model, endpoint=endpoint,
        )

        if "compact" in {schema.name for schema in schemas}:
            dispatch = dict(dispatch)

            def _compact_daemon_context(_args: dict) -> dict:
                nonlocal compact_reset_accepted
                action = _args.get("action")
                if action == "manual":
                    return {
                        "status": "success",
                        "action": "manual",
                        "read_only": True,
                        "procedures": list(_DAEMON_COMPACT_MANUAL_PROCEDURES),
                    }
                if action != "run":
                    return {"status": "error", "message": "action is required and must be 'run' or 'manual'; context was not reset"}
                if not compact_batch_allowed:
                    return {"status": "error", "message": "compact must be the only tool call in its batch"}
                reason = _args.get("_reason")
                if not isinstance(reason, str) or not reason.strip():
                    return {"status": "error", "message": "_reason must be a non-empty string; context was not reset"}
                compact_reset_accepted = True
                return {
                    "status": "success",
                    "instruction": "The surviving compact call _reason is the complete handoff; resume execution from it after this context reset.",
                    "recovery": {
                        "run_directory": str(run_dir.path),
                        "state": str(run_dir.daemon_json_path),
                        "chat_history": str(run_dir.chat_path),
                        "event_log": str(run_dir.events_path),
                    },
                }

            dispatch["compact"] = _compact_daemon_context

        intrinsic_tool_names = set(self._daemon_intrinsic_surface()[1])

        def _dispatch_daemon_tool(tc):
            handler = dispatch.get(tc.name)
            if handler is None:
                from lingtai.kernel.types import UnknownToolError
                raise UnknownToolError(tc.name)
            args = dict(tc.args or {})
            if tc.name in intrinsic_tool_names:
                args["_tc_id"] = tc.id
            return handler(args)

        def _daemon_tool_logger(event_type: str, **fields) -> None:
            tool_name = fields.get("tool_name") or fields.get("tool")
            if event_type == "tool_call_normalized" and tool_name:
                run_dir.set_current_tool(tool_name, fields.get("tool_args") or {})
            elif event_type == "tool_result" and tool_name:
                status = "error" if fields.get("status") == "error" else "ok"
                run_dir.clear_current_tool(result_status=status)
            self._log(
                f"daemon_{event_type}",
                em_id=em_id,
                run_id=getattr(run_dir, "run_id", None),
                **fields,
            )

        executor = ToolExecutor(
            dispatch_fn=_dispatch_daemon_tool,
            make_tool_result_fn=lambda name, result, **kw: service.make_tool_result(
                name, result, **kw
            ),
            guard=LoopGuard(),
            known_tools=set(dispatch),
            parallel_safe_tools=set(),
            logger_fn=_daemon_tool_logger,
            # Daemons receive a daemon-local agent_meta projection plus universal
            # per-execution tool_meta. Parent agent/session and communication state
            # are intentionally not shared across this boundary.
            meta_fn=lambda: {"agent_state": daemon_meta_state.snapshot(session)},
            working_dir=self._workdir.path,
            tool_call_guard=self._runtime.tool_call_guard,
            summarizer_fn=daemon_summarizer_fn,
            raw_log_path=run_dir.events_path.relative_to(
                self._workdir.path
            ).as_posix(),
            raw_event_type="daemon_tool_result",
        )

        def _accum(resp):
            if resp.usage is None:
                return
            u = resp.usage
            run_dir.append_tokens(
                input=u.input_tokens,
                output=u.output_tokens,
                thinking=u.thinking_tokens,
                cached=u.cached_tokens,
                model=effective_model,
                endpoint=endpoint,
                usage_extra=getattr(u, "extra", None),
            )

        def _mechanically_compact(tool_results=None):
            """Reset expired daemon context without hiding the recovery step."""
            nonlocal session
            from lingtai.kernel.llm.interface import ChatInterface

            # Close the current assistant/tool-result pair before selecting the
            # retained tail.  This pair is the durable evidence the fresh model
            # context is allowed to rely on after the reset.
            if tool_results:
                session.interface.add_tool_results(tool_results)
                run_dir.record_user_send(
                    json.dumps([str(r) for r in tool_results], ensure_ascii=False),
                    kind="tool_results",
                )
            history = session.interface.to_dict()
            system_entries = [entry for entry in history if entry.get("role") == "system"]
            pair = None
            for index in range(len(history) - 2, -1, -1):
                assistant_entry = history[index]
                result_entry = history[index + 1] if index + 1 < len(history) else None
                if (
                    assistant_entry.get("role") == "assistant"
                    and result_entry
                    and result_entry.get("role") == "user"
                    and result_entry.get("content")
                    and all(block.get("type") == "tool_result" for block in result_entry["content"])
                ):
                    pair = [assistant_entry, result_entry]
                    break
            if pair is None or not system_entries:
                raise RuntimeError(
                    "mechanical compact requires the latest assistant/tool-result pair"
                )
            retained = ChatInterface.from_dict(
                [system_entries[-1], *pair]
            )
            session = service.create_session(
                system_prompt=system_prompt,
                tools=schemas or None,
                model=effective_model,
                thinking="default",
                tracked=False,
                interface=retained,
            )
            daemon_meta_state.note_compact_reset(session)
            recovery = (
                f"{DAEMON_MECHANICAL_COMPACT_RECOVERY} Durable artifacts: "
                f"state={run_dir.daemon_json_path}; history={run_dir.chat_path}; "
                f"events={run_dir.events_path}."
            )
            run_dir.record_user_send(recovery, kind="mechanical_compact_recovery")
            response = session.send(recovery)
            daemon_meta_state.note_response(response, session)
            _accum(response)
            return response

        def _record_recovery_response(recovery_response) -> None:
            # Keep every provider response in the daemon's durable chat history,
            # but retain the current normal turn number: recovery sends are not
            # effective-max-turns work.
            state = run_dir.state_snapshot()
            current_turn = state.get("turn", turns + 1)
            if not isinstance(current_turn, int) or isinstance(current_turn, bool):
                current_turn = turns + 1
            run_dir.bump_turn(
                turn=current_turn,
                response_text=getattr(recovery_response, "text", "") or "",
            )

        def _compact_before_empty_retry(source: str) -> None:
            interface = getattr(session, "interface", None)
            if interface is None:
                return
            try:
                stats = compact_oversized_history(
                    interface,
                    working_dir=self._workdir.path,
                    logger_fn=lambda event, **fields: run_dir.append_event(
                        event, em_id=em_id, run_id=run_dir.run_id, **fields
                    ),
                )
            except Exception:
                # Recovery must not become a new terminal failure. The main
                # loop treats this helper as best-effort for the same reason.
                run_dir.append_event(
                    "aed_history_compaction_failed",
                    em_id=em_id,
                    run_id=run_dir.run_id,
                    source=source,
                )
                return
            run_dir.append_event(
                "aed_history_compacted",
                em_id=em_id,
                run_id=run_dir.run_id,
                source=source,
                scanned_blocks=stats.scanned_blocks,
                compacted_blocks=stats.compacted_blocks,
                original_chars_total=stats.original_chars_total,
                replacement_chars_total=stats.replacement_chars_total,
                artifact_paths=list(stats.artifact_paths),
            )

        empty_transient_attempts = 0
        empty_aed_attempts = 0

        def _daemon_empty_response_error(*, in_tool_loop: bool) -> str:
            """Describe one daemon provider send without provider-controlled data."""
            where = "after tool results" if in_tool_loop else "on initial send"
            return (
                "LLM returned empty response (no text, no tool_calls, no thoughts) "
                f"{where}; ledger=daemon"
            )

        def _daemon_aed_retry_message(err_desc: str) -> str:
            """Build the daemon-local localized ``MSG_REQUEST`` recovery message."""
            language = getattr(self._runtime, "language", "en")
            return _make_message(
                MSG_REQUEST,
                "system",
                _t(
                    language,
                    "system.stuck_revive",
                    ts=self._runtime.now_iso(),
                    err_desc=err_desc,
                ),
            ).content

        def _deliver_one_shell_prompt_event():
            """Drain one run-local Shell event at an already-safe send boundary.

            This helper deliberately never calls ``shell.poll`` or reads Shell
            logs.  Its fixed guidance is the only provider-visible projection;
            exact command output remains behind the model-chosen poll tool call.
            """
            events = run_dir.drain_shell_prompt_events(limit=1)
            if not events:
                return None
            from lingtai.tools.daemon.shell_prompt_events import (
                shell_prompt_event_guidance,
            )

            event = events[0]
            guidance = shell_prompt_event_guidance(event)
            run_dir.record_user_send(guidance, kind=event["kind"])
            event_response = session.send(guidance)
            daemon_meta_state.note_response(event_response, session)
            _accum(event_response)
            return event_response

        def _recover_empty_response(response, *, in_tool_loop: bool):
            """Mirror main all-empty transient/AED recovery in this daemon."""
            nonlocal session, empty_transient_attempts, empty_aed_attempts
            if not is_all_empty_response(response):
                return response

            # The location describes this provider send, not the request that
            # originally entered this helper. Every recovery send is a fresh
            # initial-send request; a later empty response must not inherit the
            # location of the post-tool send that triggered recovery.
            error = _daemon_empty_response_error(in_tool_loop=in_tool_loop)
            max_aed_attempts = getattr(
                self._runtime, "max_aed_attempts", 3
            )
            if (
                not isinstance(max_aed_attempts, int)
                or isinstance(max_aed_attempts, bool)
                or max_aed_attempts < 1
            ):
                max_aed_attempts = 1

            while True:
                if cancel_event.is_set() or (
                    timeout_event is not None and timeout_event.is_set()
                ):
                    return None
                if empty_transient_attempts < _TRANSIENT_EMPTY_RESPONSE_RETRY_LIMIT:
                    empty_transient_attempts += 1
                    backoff_s = 2 ** (empty_transient_attempts - 1)
                    interface = getattr(session, "interface", None)
                    if interface is not None:
                        interface.close_pending_tool_calls(
                            reason=f"transient_retry: {error[:200]}",
                            tool_completed=True,
                        )
                    _compact_before_empty_retry("aed_transient")
                    run_dir.append_event(
                        "daemon_aed_transient_retry",
                        em_id=em_id,
                        run_id=run_dir.run_id,
                        attempt=empty_transient_attempts,
                        max_attempts=_TRANSIENT_EMPTY_RESPONSE_RETRY_LIMIT,
                        backoff_s=backoff_s,
                        error=error,
                    )
                    if not _wait_recovery_backoff(
                        cancel_event, timeout_event, backoff_s
                    ):
                        return None
                    retry_message = _daemon_aed_retry_message(error)
                    run_dir.record_user_send(retry_message, kind="aed_transient")
                    response = session.send(retry_message)
                    daemon_meta_state.note_response(response, session)
                    _accum(response)
                    _record_recovery_response(response)
                    if cancel_event.is_set() or (
                        timeout_event is not None and timeout_event.is_set()
                    ):
                        return None
                    if not is_all_empty_response(response):
                        return response
                    error = _daemon_empty_response_error(in_tool_loop=False)
                    continue

                empty_aed_attempts += 1
                interface = getattr(session, "interface", None)
                if interface is not None:
                    interface.close_pending_tool_calls(
                        reason=error,
                        tool_completed=True,
                    )
                if cancel_event.is_set() or (
                    timeout_event is not None and timeout_event.is_set()
                ):
                    return None
                # Record the counted attempt before checking the terminal budget.
                # A terminal attempt heals the wire but must not compact, rebuild,
                # or issue another provider call after exhaustion is known.
                run_dir.append_event(
                    "daemon_aed_attempt",
                    em_id=em_id,
                    run_id=run_dir.run_id,
                    attempt=empty_aed_attempts,
                    max_attempts=max_aed_attempts,
                    error=error,
                )
                if empty_aed_attempts >= max_aed_attempts:
                    raise RuntimeError(_DAEMON_EMPTY_RESPONSE_ERROR)
                _compact_before_empty_retry("aed_deterministic")
                preserved_interface = session.interface
                session = service.create_session(
                    system_prompt=system_prompt,
                    tools=schemas or None,
                    model=effective_model,
                    thinking="default",
                    tracked=False,
                    interface=preserved_interface,
                )
                retry_message = _daemon_aed_retry_message(error)
                run_dir.record_user_send(retry_message, kind="aed")
                response = session.send(retry_message)
                daemon_meta_state.note_response(response, session)
                _accum(response)
                _record_recovery_response(response)
                if cancel_event.is_set() or (
                    timeout_event is not None and timeout_event.is_set()
                ):
                    return None
                if not is_all_empty_response(response):
                    return response
                error = _daemon_empty_response_error(in_tool_loop=False)

        try:
            kickoff = prompt or "Begin the assigned daemon task."
            run_dir.record_user_send(kickoff, kind="kickoff")
            response = session.send(kickoff)
            daemon_meta_state.note_response(response, session)
            _accum(response)
            # A detached watchdog may fire while a provider call is in flight.
            # Re-check before accepting the response so late provider return
            # cannot overwrite truthful timeout/reclaim state with ``done``.
            if cancel_event.is_set():
                return _mark_cancelled_or_timeout(run_dir, timeout_event)
            turns = 0
            recovery_tool_batch_pending = False
            run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")
            response = _recover_empty_response(response, in_tool_loop=False)
            if response is None:
                return _mark_cancelled_or_timeout(run_dir, timeout_event)

            while response.tool_calls and (
                turns < effective_max_turns or recovery_tool_batch_pending
            ):
                if cancel_event.is_set():
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)

                # Intermediate text is already persisted in chat_history via
                # bump_turn(); do not inject daemon progress as parent requests.

                daemon_meta_state.note_tool_batch(response.tool_calls)
                compact_execution_calls = [
                    tc for tc in response.tool_calls
                    if tc.name == "compact" and (tc.args or {}).get("action") == "run"
                ]
                has_compact_call = bool(compact_execution_calls)
                compact_batch_allowed = (
                    len(compact_execution_calls) == 1
                    and len(response.tool_calls) == 1
                )
                executor.guard.record_calls(len(response.tool_calls))
                compact_reset_accepted = False
                if has_compact_call and not compact_batch_allowed:
                    result_payload = {
                        "status": "error",
                        "message": (
                            "compact must be the only tool call in its assistant "
                            "batch; no tools in this batch were executed"
                        ),
                    }
                    tool_results = [
                        service.make_tool_result(
                            tc.name,
                            dict(result_payload),
                            tool_call_id=getattr(tc, "id", None),
                        )
                        for tc in response.tool_calls
                    ]
                    intercepted = False
                    intercept_text = ""
                    executor.guard.clear_progress_notice()
                else:
                    tool_results, intercepted, intercept_text = executor.execute(
                        response.tool_calls,
                        api_call_id=getattr(response, "api_call_id", None),
                    )
                    executor.guard.clear_progress_notice()

                if not intercepted and compact_batch_allowed and compact_reset_accepted:
                    if cancel_event.is_set():
                        return _mark_cancelled_or_timeout(run_dir, timeout_event)
                    from lingtai.kernel.llm.interface import ChatInterface
                    history = session.interface.to_dict()
                    compact_assistant = history[-1] if history else None
                    if not (
                        isinstance(compact_assistant, dict)
                        and compact_assistant.get("role") == "assistant"
                    ):
                        raise RuntimeError("compact context reset requires an intact assistant compact call")
                    system_entries = [e for e in history if e.get("role") == "system"]
                    retained = ChatInterface.from_dict(
                        ([system_entries[-1]] if system_entries else [])
                        + [compact_assistant]
                    )
                    session = service.create_session(
                        system_prompt=system_prompt,
                        tools=schemas or None,
                        model=effective_model,
                        thinking="default",
                        tracked=False,
                        interface=retained,
                    )
                    # The compact result is the first carrier in the fresh context.
                    # Clear pre-reset current-call state before stamping that carrier,
                    # while preserving cumulative daemon totals and round identity.
                    daemon_meta_state.note_compact_reset(session)

                # Promote the daemon-local snapshot to the canonical final
                # ToolResultBlock sidecar. The helper deliberately omits the
                # parent-only notifications/communication axis. For an accepted
                # compact reset, this now describes the fresh retained context.
                attach_daemon_agent_meta(
                    tool_results,
                    agent_state=daemon_meta_state.snapshot(session),
                )

                if intercepted:
                    # Preserve provider pairing by recording the synthesized tool
                    # results, then terminate the daemon with the intercept text.
                    run_dir.record_user_send(
                        json.dumps([str(r) for r in tool_results], ensure_ascii=False),
                        kind="tool_results",
                    )
                    text = intercept_text or "[intercepted]"
                    self._require_done_completion(run_dir, text)
                    run_dir.mark_done(text)
                    return text

                mechanically_compacted = False
                if daemon_meta_state.compact_due:
                    if cancel_event.is_set():
                        return _mark_cancelled_or_timeout(run_dir, timeout_event)
                    response = _mechanically_compact(tool_results)
                    mechanically_compacted = True
                if not mechanically_compacted:
                    # Tool results are written to chat_history before sending.
                    run_dir.record_user_send(
                        json.dumps([str(r) for r in tool_results], ensure_ascii=False),
                        kind="tool_results",
                    )
                    response = session.send(tool_results)
                    daemon_meta_state.note_response(response, session)
                    _accum(response)
                turns += 1
                run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")
                recovery_tool_batch_pending = mechanically_compacted and bool(
                    response.tool_calls
                )
                response = _recover_empty_response(
                    response,
                    in_tool_loop=not mechanically_compacted,
                )
                if response is None:
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)
                recovery_tool_batch_pending = mechanically_compacted and bool(
                    response.tool_calls
                )

                # Inject follow-up as a separate user message — only safe when
                # the response is text-only. If it carries new tool_calls, the
                # canonical interface tail is assistant[tool_calls] and a user
                # message here would violate the pairing invariant.
                if not response.tool_calls:
                    if not mechanically_compacted and daemon_meta_state.compact_due:
                        if cancel_event.is_set():
                            return _mark_cancelled_or_timeout(run_dir, timeout_event)
                        response = _mechanically_compact()
                        turns += 1
                        run_dir.bump_turn(
                            turn=turns + 1, response_text=response.text or ""
                        )
                        response = _recover_empty_response(response, in_tool_loop=False)
                        if response is None:
                            return _mark_cancelled_or_timeout(run_dir, timeout_event)
                        recovery_tool_batch_pending = bool(response.tool_calls)
                        if response.tool_calls:
                            # Recovery tool calls must complete before the
                            # buffered follow-up is drained or sent.
                            continue
                    # Shell events are delivered only after the provider has
                    # returned a text-only response, so the canonical interface
                    # has no pending assistant tool-call pair. Drain only events
                    # already durable now; never wait, auto-poll, or keep a
                    # terminal daemon alive for a future Shell completion.
                    while not response.tool_calls:
                        shell_event_response = _deliver_one_shell_prompt_event()
                        if shell_event_response is None:
                            break
                        response = shell_event_response
                        turns += 1
                        run_dir.bump_turn(
                            turn=turns + 1, response_text=response.text or ""
                        )
                        response = _recover_empty_response(
                            response, in_tool_loop=False
                        )
                        if response is None:
                            return _mark_cancelled_or_timeout(run_dir, timeout_event)
                        recovery_tool_batch_pending = bool(response.tool_calls)
                    if response.tool_calls:
                        # A model-chosen poll (or any other tool) must complete
                        # through the ordinary loop before another event/followup
                        # can be inserted.
                        continue

                    followup = self._drain_followup(em_id)
                    if followup:
                        # A buffered follow-up is a new daemon request turn;
                        # reset only the empty-response recovery budget, not
                        # the normal tool-loop turn counter.
                        empty_transient_attempts = 0
                        empty_aed_attempts = 0
                        run_dir.record_user_send(followup, kind="followup")
                        response = session.send(followup)
                        daemon_meta_state.note_response(response, session)
                        _accum(response)
                        turns += 1
                        run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")
                        response = _recover_empty_response(response, in_tool_loop=False)
                        if response is None:
                            return _mark_cancelled_or_timeout(run_dir, timeout_event)

            if response.tool_calls and turns >= effective_max_turns:
                raise RuntimeError(
                    "max_turns exhausted before the daemon completed its tool chain"
                )
            text = response.text or "[no output]"
            # The final terminal transition is serialized with cancellation:
            # a reclaim/deadline racing the last provider response wins over a
            # late natural ``done`` commit.
            if cancel_event.is_set() or run_dir.read_state_from_disk(run_dir.path).get("state") not in {"running", "active"}:
                return _mark_cancelled_or_timeout(run_dir, timeout_event)
            self._require_done_completion(run_dir, text)
            if cancel_event.is_set():
                return _mark_cancelled_or_timeout(run_dir, timeout_event)
            run_dir.mark_done(text)
            return text
        except Exception as e:
            run_dir.mark_failed(e)
            raise
        finally:
            self._close_task_mcp_clients(mcp_clients)

    def _find_claude_session_id(self, em_id: str) -> str | None:
        """Search ~/.claude/projects/ for the session JSONL whose customTitle matches em_id.

        Claude Code stores sessions as JSONL files under
        ``~/.claude/projects/<project-hash>/``. The first line of each session
        file is a JSON object with ``type: "custom-title"`` containing the
        ``customTitle`` and ``sessionId``.
        """
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.is_dir():
            return None
        for jsonl_path in projects_dir.rglob("*.jsonl"):
            try:
                with open(jsonl_path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                obj = json.loads(first_line)
                if (obj.get("type") == "custom-title"
                        and obj.get("customTitle") == em_id):
                    return obj.get("sessionId")
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def _run_claude_code_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run a Claude Code CLI session as the emanation backend.

        Spawns Claude Code with ``--output-format stream-json --verbose`` so
        events arrive in real time (vs ``--output-format text``, which
        buffers everything until completion — see GH issues #99/#100).
        Parses each event line and writes:

        - ``claude_session_id`` to daemon.json on the first event that
          carries one (typically the system ``init`` event, but any event
          with ``session_id`` works as a fallback). This makes
          ``daemon(ask)`` usable from the moment ``emanate`` returns,
          rather than after the initial run completes.
        - Per-turn ``text``/``tool_use`` blocks via
          ``record_cli_output`` so ``daemon(check)`` shows live progress.
        - Tool calls via ``set_current_tool`` / ``clear_current_tool``.
        - stderr to its own pipe so diagnostic messages aren't lost in
          the stdout stream.

        Note: Claude Code's token ``usage`` fields are deliberately NOT
        forwarded to ``append_tokens``. Claude Code bills through its
        own provider account, and its cache_creation/cache_read
        semantics don't map cleanly onto the kernel's LLM-adapter
        accounting. Mixing them into ``sum_token_ledger`` would
        produce a misleading "lifetime totals" number for the parent.
        The final ``result`` event's ``usage`` is instead persisted to
        ``daemon.json.cli_tokens`` via ``record_cli_tokens`` — UI-only,
        never touching either token ledger — so the TUI ``/daemons``
        view can still surface what the CLI run cost.

        Falls back to the legacy JSONL scan if no ``session_id`` ever
        appears in the stream.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        # Required infrastructure flags come first; free-form
        # backend_options sit between them and the task prompt so the
        # task itself stays the trailing positional argument that the
        # Claude Code CLI expects.
        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            "--name", em_id,
        ]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(task)
        self._log("daemon_claude_code_start", em_id=em_id, cmd=" ".join(cmd))

        spawn_env = _claude_code_env()
        if len(spawn_env) != len(os.environ):
            self._log("daemon_claude_code_env_stripped", em_id=em_id,
                      stripped=[k for k in _CLAUDE_CODE_STRIP_ENV if k in os.environ])
        # The caller-supplied ``backend_options.env`` overlay is applied last so
        # a profile selector such as CLAUDE_CONFIG_DIR wins over the inherited
        # environment. It is an explicit operator choice, so it can also
        # re-introduce a name the strip list removed; the strip list defends
        # against accidental inheritance, not against a deliberate override.
        if backend_env:
            spawn_env.update(backend_env)

        command = DaemonProcessCommand(
            tuple(cmd), self._workdir.path, tuple(spawn_env.items()),
        )
        try:
            handle = self._process_port.spawn(command, group_id=run_dir.group_id)
        except FileNotFoundError:
            exc = RuntimeError("'claude' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start claude CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        # Drain stderr in a background thread so diagnostic messages reach
        # the run dir even while the main thread is parsing stdout events.
        # iLink-style daemons with a chatty stderr would otherwise block
        # the pipe and stall the process.
        stderr_thread = self._process_port.drain_stderr(
            handle,
            on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-claude-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        final_result_text: str | None = None
        final_is_error: bool = False
        session_id_captured: str | None = None
        # Buffered, not yet persisted — see the result-event handler and
        # the post-classification persistence below for why.
        usage_candidate: tuple[dict[str, int], dict] | None = None
        # Active tool_use blocks awaiting their tool_result. Keyed by
        # the tool_use id from the assistant message; value is the tool
        # name so we can call clear_current_tool with a status string.
        pending_tools: dict[str, str] = {}

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if run_dir.set_session_id("claude_session_id", sid, overwrite=True):
                session_id_captured = sid
                self._log("daemon_claude_code_session",
                          em_id=em_id, session_id=sid)

        def _handle_assistant_event(event: dict) -> None:
            message = event.get("message") or {}
            content = message.get("content") or []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text.strip():
                        run_dir.record_cli_output(text, stream="stdout")
                elif btype == "tool_use":
                    tool_id = block.get("id") or ""
                    tool_name = block.get("name") or "unknown"
                    tool_input = block.get("input") or {}
                    if tool_id:
                        pending_tools[tool_id] = tool_name
                    try:
                        run_dir.set_current_tool(tool_name, tool_input)
                    except Exception:
                        pass
            # NOTE: Claude Code spend is intentionally NOT recorded in the
            # daemon's or parent's token ledger. Claude Code runs as an
            # external process with its own billing path; counting its
            # `usage` fields here would mix unrelated currencies (cache
            # read/write semantics differ from the kernel's LLM adapters)
            # and create a misleading "lifetime totals" number. Spend
            # remains visible to the agent via daemon(check) — the
            # `last_output` field, cli_output events, and stderr — and,
            # for UI display, the final result event's usage is persisted
            # separately to daemon.json.cli_tokens (see the result-event
            # handler below). Neither path touches sum_token_ledger.

        def _handle_user_event(event: dict) -> None:
            # User events in stream-json mode carry tool_result blocks back
            # from tool executions performed by Claude Code itself.
            message = event.get("message") or {}
            content = message.get("content") or []
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id") or ""
                status = "error" if block.get("is_error") else "ok"
                if tool_id in pending_tools:
                    pending_tools.pop(tool_id, None)
                try:
                    run_dir.clear_current_tool(status)
                except Exception:
                    pass

        try:
            for raw_line in self._process_port.iter_stdout(handle):
                if cancel_event.is_set():
                    exit_receipt = self._process_port.terminate(
                        handle, reason=("timeout" if timeout_event and timeout_event.is_set()
                                        else "reclaim"),
                    )
                    if exit_receipt is not None:
                        self._attributed_process_exit(
                            exit_receipt, "claude", "", run_dir,
                        )
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Defensive: if Claude Code ever emits a non-JSON line
                    # in stream-json mode (e.g. a startup banner), don't
                    # crash the parse — surface it as raw stdout.
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                # Capture session_id from the first event that has it. The
                # very first system events (hook_started, init) already
                # carry it, so this typically fires within the first few
                # lines — well before the LLM produces any reply.
                sid = event.get("session_id")
                if sid and session_id_captured != sid:
                    _store_session_id(sid)

                etype = event.get("type")
                if etype == "assistant":
                    _handle_assistant_event(event)
                elif etype == "user":
                    _handle_user_event(event)
                elif etype == "result":
                    final_result_text = event.get("result") or ""
                    final_is_error = bool(event.get("is_error"))
                    # Buffer Claude Code's reported token usage — do not
                    # persist it yet. A watchdog/manual cancel can still
                    # fire after this final line arrives but before
                    # the process Port wait returns; persisting here would let a
                    # cancelled/timed-out run leave usage and, via
                    # mark_done below, a false "done" state. Retain only
                    # the first valid terminal usage candidate (a
                    # duplicated terminal line is not a new provider
                    # call), and persist it once terminal classification
                    # (cancel/timeout, exit code, is_error) has passed —
                    # see the persistence call after the classification
                    # gates below.
                    if usage_candidate is None:
                        usage = _normalize_claude_usage(event.get("usage"))
                        if usage is not None:
                            usage_candidate = (usage, event.get("usage"))
                    # If there are still tool_use blocks pending without
                    # a matching tool_result (shouldn't happen on success,
                    # but be defensive), clear them so daemon.json's
                    # current_tool doesn't stay stuck.
                    while pending_tools:
                        pending_tools.popitem()
                        try:
                            run_dir.clear_current_tool("ok")
                        except Exception:
                            pass

            exit_receipt = self._process_port.wait(handle)
        except Exception as e:
            exit_receipt = self._process_port.terminate(
                handle, reason=("timeout" if timeout_event and timeout_event.is_set()
                                else "reclaim"),
            )
            run_dir.mark_failed(e)
            raise
        finally:
            # Give the stderr drainer a moment to finish reading any
            # remaining bytes before the pipe closes on us.
            stderr_thread.join(timeout=2.0)
            if ('exit_receipt' in locals() and exit_receipt is not None
                    and exit_receipt.returncode is not None):
                self._process_port.release(handle)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        # Re-check cancellation after the process Port wait returns. A watchdog or
        # manual reclaim can set cancel_event while we were blocked in
        # wait() for the process to exit after stdout EOF — a zero exit
        # observed after that point must not overwrite the cancelled/
        # timeout state or persist the buffered usage candidate (mirrors
        # the Cursor backend's post-EOF cancellation fix).
        if cancel_event.is_set():
            if exit_receipt is not None:
                self._attributed_process_exit(
                    exit_receipt, "claude", stderr_tail[-500:], run_dir,
                )
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        if exit_receipt.returncode != 0:
            detail = stderr_tail or (final_result_text or "")
            attributed = self._attributed_process_exit(
                exit_receipt, "claude", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"claude CLI exited with code {exit_receipt.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # If the result event signalled an error even though the process
        # exited 0, surface that so the caller doesn't think the task
        # succeeded.
        if final_is_error:
            exc = RuntimeError(
                f"claude CLI reported is_error=true: "
                f"{(final_result_text or stderr_tail)[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # Fallback: if no event carried session_id (extremely unusual but
        # possible if Claude Code changes its stream format), fall back to
        # the legacy JSONL scan so daemon(ask) still works.
        if not session_id_captured:
            session_id = self._find_claude_session_id(em_id)
            if session_id:
                _store_session_id(session_id)

        text = (final_result_text or "").strip() or "[no output]"
        # _require_done_completion is itself a terminal acceptance gate:
        # when the run loaded daemon_common MCP, a missing/bad finish()
        # call marks the run failed and raises here, before any usage is
        # persisted or the run is marked done.
        self._require_done_completion(run_dir, text)

        # Final re-check: the classification above (including
        # _require_done_completion, which reads a completion file and can
        # take non-trivial time) could still race a watchdog/manual
        # cancel. Persist the buffered usage candidate — and mark done —
        # only for a run accepted as successful by every prior gate.
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        if usage_candidate is not None:
            usage, raw = usage_candidate
            try:
                run_dir.record_cli_tokens(
                    input=usage["input"], output=usage["output"],
                    cached=usage["cached"], thinking=usage["thinking"],
                    raw=raw,
                )
            except Exception:
                pass

        run_dir.mark_done(text)
        return text

    def _run_claude_interactive_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run an interactive Claude Code session through a PTY.

        This is the experimental ``backend="claude"`` route inspired by
        third-party ``claude -p`` replacements: run the normal interactive
        ``claude`` TUI, use SessionStart/Stop hooks as synchronization points,
        and read Claude's transcript JSONL for the daemon result.  It does not
        mutate Claude's global config and refuses credential/trust automation.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        interactive_env = _claude_code_env()
        if backend_env:
            interactive_env.update(backend_env)
        try:
            result = run_claude_interactive(
                em_id=em_id,
                run_dir=run_dir,
                working_dir=self._workdir.path,
                task=task,
                cancel_event=cancel_event,
                timeout_event=timeout_event,
                backend_argv=backend_argv,
                env=interactive_env,
                log_callback=self._log,
                terminal_port=self._interactive_terminal_port,
            )
        except ClaudeInteractiveError as e:
            run_dir.mark_failed(e)
            raise
        except Exception as e:
            run_dir.mark_failed(e)
            raise

        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        text = (result.final_text or "").strip() or "[no output]"
        run_dir.mark_done(text)
        return text

    def _run_codex_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run a Codex CLI session as the emanation backend.

        Spawns Codex with ``--json`` so events arrive as JSONL (one event
        per stdout line), and parses them so the daemon shows live
        progress and captures a resumable session id — mirroring the
        Claude Code backend. ``--ephemeral`` is intentionally **not**
        passed: it would disable session persistence and break
        ``daemon(ask, id=em-N)``.

        Event shapes (codex-cli 0.128.0):
        - ``{"type":"thread.started","thread_id":"<uuid>"}`` — first event,
          carries the session id we'll later pass to
          ``codex exec resume <id>``.
        - ``{"type":"turn.started"}`` — marks an agent turn beginning.
        - ``{"type":"item.completed","item":{"type":"agent_message","text":"..."}}``
          — visible agent reply text.
        - ``{"type":"turn.completed","usage":{...}}`` — terminal event.
          Codex reports token usage on this event, but we deliberately do
          NOT forward it to ``append_tokens``: codex runs as an external
          process with its own billing path, and counting its tokens
          into the kernel's ledger would mix unrelated currencies. Spend
          is visible to the agent via ``daemon(check)`` but not via
          ``sum_token_ledger``.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        # The normal route owns the Codex executable and flags.  A fully
        # qualified interpreter/script argv is retained as a deterministic
        # local test/backend adapter seam; it still uses this same production
        # JSONL parser and process ownership code.
        if (
            backend_argv
            and len(backend_argv) >= 2
            and Path(backend_argv[0]).is_file()
            and Path(backend_argv[1]).is_file()
        ):
            cmd = list(backend_argv)
        else:
            cmd = [
                "codex",
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
            if backend_argv:
                cmd.extend(backend_argv)
            cmd.append(task)
        self._log("daemon_codex_start", em_id=em_id, cmd=" ".join(cmd))

        # Codex normally inherits the parent environment untouched; an explicit
        # env is materialized only when the caller supplied a
        # ``backend_options.env`` overlay.
        command = DaemonProcessCommand(tuple(cmd), self._workdir.path)
        if backend_env:
            env = os.environ.copy()
            env.update(backend_env)
            command = DaemonProcessCommand(
                tuple(cmd), self._workdir.path, tuple(env.items()),
            )
        try:
            handle = self._process_port.spawn(
                command, group_id=run_dir.group_id,
            )
        except FileNotFoundError:
            exc = RuntimeError("'codex' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start codex CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-codex-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        session_id_captured: str | None = None
        agent_message_texts: list[str] = []
        turn_completed = False

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if run_dir.set_session_id("codex_session_id", sid, overwrite=True):
                session_id_captured = sid
                self._log("daemon_codex_session", em_id=em_id, session_id=sid)

        try:
            for raw_line in self._process_port.iter_stdout(handle):
                if cancel_event.is_set():
                    self._process_port.terminate(
                        handle, reason="timeout" if timeout_event and timeout_event.is_set() else "reclaim"
                    )
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Defensive: surface non-JSON lines as raw stdout
                    # instead of crashing the parser.
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                etype = event.get("type")
                if etype == "thread.started":
                    tid = event.get("thread_id")
                    if tid:
                        _store_session_id(tid)
                elif etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        text = item.get("text") or ""
                        if text.strip():
                            agent_message_texts.append(text)
                            run_dir.record_cli_output(text, stream="stdout")
                elif etype == "turn.completed":
                    # A Codex turn has one terminal usage report. Treat the
                    # first terminal event as authoritative so a duplicated
                    # line cannot double-count UI totals or forensic events.
                    if turn_completed:
                        continue
                    turn_completed = True
                    # NOTE: Codex spend is intentionally NOT recorded in
                    # the daemon's or parent's token ledger. Codex runs
                    # as an external process with its own billing path,
                    # and its `cached_input_tokens` semantics differ
                    # from the kernel's LLM adapters (codex `input_tokens`
                    # already includes the cached portion). Mixing it in
                    # would produce a misleading "lifetime totals" number.
                    # The truthful disjoint UI view is separate from the
                    # kernel ledgers and retains the raw source usage in the
                    # cli_usage forensic event.
                    usage = _normalize_codex_usage(event.get("usage"))
                    if usage is not None:
                        try:
                            run_dir.record_cli_tokens(
                                input=usage["input"],
                                output=usage["output"],
                                cached=usage["cached"],
                                raw=event.get("usage"),
                            )
                        except Exception:
                            pass

            exit_receipt = self._process_port.wait(handle)
        except Exception as e:
            self._process_port.terminate(handle)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._process_port.release(handle)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        # Cancellation is terminal truth even when the child happened to
        # return zero before the watchdog/reclaim signal was observed. Keep
        # this post-wait gate outside the non-zero branch so a successful exit
        # receipt cannot erase a late cancellation.
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        if exit_receipt.returncode != 0:
            detail = stderr_tail or "\n".join(agent_message_texts[-3:])
            if cancel_event.is_set():
                # A watchdog/reclaim may close stdout before the loop reaches
                # its next policy checkpoint. Preserve terminal truth while
                # still recording the Port's raw signal/reason attribution.
                self._attributed_process_exit(
                    exit_receipt, "codex", detail[-500:], run_dir,
                )
                return _mark_cancelled_or_timeout(run_dir, timeout_event)
            attributed = self._attributed_process_exit(exit_receipt, "codex", detail[-500:], run_dir)
            exc = RuntimeError(
                attributed
                or f"codex CLI exited with code {exit_receipt.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # Codex doesn't emit an `is_error` flag like Claude Code; the
        # signal that the turn finished cleanly is a `turn.completed`
        # event. If we never saw one AND captured no agent messages,
        # treat that as a failure even though the process exited 0.
        if not turn_completed and not agent_message_texts:
            exc = RuntimeError(
                f"codex CLI produced no turn.completed event: "
                f"{(stderr_tail or '[no output]')[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        text = "\n".join(agent_message_texts).strip() or "[no output]"
        self._require_done_completion(run_dir, text)
        run_dir.mark_done(text)
        return text

    _NOTIFICATION_PREVIEW_MAX = 500

    def _publish_daemon_notification(
        self,
        em_id: str,
        *,
        status: str,
        text: str,
        run_dir: DaemonRunDir | None = None,
        run_state: dict | None = None,
        run_path: Path | None = None,
        idempotency_key: str | None = None,
        kind: str = "daemon_terminal",
    ) -> bool:
        """Publish a compact daemon terminal event via its run mini-channel.

        Events are appended to ``.notification/daemon/<daemon-id>.json`` by the
        injected Store's existing channel mutation operation. The sibling
        ``.notification/daemon.json`` is a derived run-state report, never an
        event source; no fixed event cap is used.

        Fired on every terminal status (done / failed / cancelled / timeout) so
        the parent agent can dispatch a daemon and safely go idle: the kernel
        notification sync wakes it when the run ends, no polling required. When
        ``notification.json`` configures ``channels.daemon.alarm_threshold``,
        the kernel's attention mask keeps sub-threshold arrivals readable
        without waking; the strict ``count > N`` crossing wakes exactly once,
        and clearing the channel starts a new batch. Full
        daemon output belongs in the run directory and is inspectable via
        ``daemon(action="check", id=...)``.  The parent notification is only a
        wake signal with provenance, bounded preview, and the inspection path.
        It must not arrive as ordinary ``MSG_REQUEST`` text.

        Terminal callers pass an idempotency key and persist a durable receipt
        only after this method returns True. Follow-up (``ask``) notifications
        intentionally reuse this same compact format without terminal receipt
        state, but carry ``kind="daemon_followup"`` so a consumer never reads a
        still-running run as terminal.
        """
        preview = text or ""
        if len(preview) > self._NOTIFICATION_PREVIEW_MAX:
            preview = (
                preview[: self._NOTIFICATION_PREVIEW_MAX]
                + f"...[truncated; {len(preview)} chars total]"
            )
        parts = [
            f"Daemon {em_id} {status}.",
            f"Inspect with daemon(action=\"check\", id=\"{em_id}\").",
        ]
        recorded_error = None
        if run_dir is not None:
            snapshot = run_dir.state_snapshot()
        elif run_state is not None:
            snapshot = dict(run_state)
        else:
            snapshot = None
        if snapshot is not None:
            task = (snapshot.get("task") or "").strip()
            if task:
                if len(task) > self._NOTIFICATION_PREVIEW_MAX:
                    task = task[: self._NOTIFICATION_PREVIEW_MAX] + "..."
                parts.append(f"Task: {task}")
            if run_dir is not None:
                parts.append(f"Run directory: {run_dir.path}")
            elif run_path is not None:
                parts.append(f"Run directory: {run_path}")
            result_path = snapshot.get("result_path")
            if result_path:
                parts.append(f"Result file: {result_path}")
            recorded_error = snapshot.get("error")
        if recorded_error:
            err_type = recorded_error.get("type", "error")
            err_msg = (recorded_error.get("message") or "")[:self._NOTIFICATION_PREVIEW_MAX]
            parts.append(f"Error: {err_type}: {err_msg}".rstrip(": "))
        if preview:
            parts.append(f"Preview:\n{preview}")
        body = "\n".join(parts)
        try:
            self._runtime.enqueue_daemon_notification(
                source="daemon",
                ref_id=em_id,
                body=body,
                idempotency_key=idempotency_key,
                skip_if_idempotency_key_exists=bool(idempotency_key),
                extra={"kind": kind, "status": status},
                channel=DAEMON_NOTIFICATION_CHANNEL,
            )
        except Exception as e:
            self._log(
                "daemon_notification_error",
                em_id=em_id,
                status=status,
                error=str(e)[:200],
            )
            return False
        return True

    def _publish_followup_if_live(
        self,
        em_id: str,
        *,
        status: str,
        text: str,
        run_dir: DaemonRunDir | None = None,
    ) -> None:
        """Publish a follow-up completion notification only if the emanation
        is still tracked. A reclaim that races an in-flight CLI ask would
        otherwise produce a "follow-up failed" notification for an entry the
        agent has already torn down — surprising and unactionable. Run_dir
        writes still happen unconditionally inside the worker; this gate is
        for the parent-facing notification only.
        """
        entry = self._emanations.get(em_id)
        if entry is None or entry.get("shutdown_in_progress"):
            self._log(
                "daemon_ask_post_reclaim",
                em_id=em_id, status=status, text_length=len(text or ""),
            )
            return
        self._publish_daemon_notification(
            em_id, status=status, text=text, run_dir=run_dir,
            kind="daemon_followup",
        )

    def _on_ask_done(self, em_id: str, future) -> None:
        """Done-callback for ask workers — surface any worker-thread exception.

        Without this, an unexpected exception in the stream-parse loop
        (e.g. an unhandled stdout decode error) would land silently in the
        future and never reach the agent or the run_dir. We log the
        exception via the standard daemon log channel and best-effort
        record it into the emanation's run_dir as a cli_output line so a
        later daemon(check) shows what happened.
        """
        try:
            exc = future.exception()
        except Exception:  # noqa: BLE001 — future internals raising is itself worth logging
            exc = None
        if exc is None:
            return
        self._log(
            "daemon_ask_worker_error",
            em_id=em_id,
            exception=type(exc).__name__,
            message=str(exc)[:500],
        )
        entry = self._emanations.get(em_id)
        run_dir = entry.get("run_dir") if entry else None
        if run_dir is not None:
            try:
                run_dir.record_cli_output(
                    f"[ask worker error] {type(exc).__name__}: {str(exc)[:300]}",
                    stream="stderr",
                )
            except OSError:
                pass
        # Clear ask_in_flight if the worker raised before its finally ran
        # (very rare — finally would normally clear it). Safe to do twice.
        if entry is not None:
            try:
                with entry["followup_lock"]:
                    entry["ask_in_flight"] = False
            except Exception:  # noqa: BLE001 — entry mutation must never re-raise
                pass

    def _drain_followup(self, em_id: str) -> str | None:
        """Drain the follow-up buffer for a specific emanation.

        The production `_run_emanation` call path always binds ``self`` to a
        `DetachedDaemonExecutionHost`, whose own `_drain_followup` override
        (reading the run-local control spool) shadows this one — see
        `execution_host.py`. This method stays reachable when `_run_emanation`
        is exercised directly against a plain `DaemonManager` (as tests do) and
        as the shared in-process followup-buffer implementation.
        """
        entry = self._emanations.get(em_id)
        if not entry:
            return None
        with entry["followup_lock"]:
            text = entry["followup_buffer"]
            entry["followup_buffer"] = ""
        return text or None

    def _handle_emanate(self, tasks: list[dict],
                        max_turns: int | None = None,
                        timeout: float | None = None,
                        backend: str = "lingtai") -> dict:
        backend = _normalize_backend(backend)
        if not tasks:
            return {"status": "error", "message": "No tasks provided"}

        # Public task mapping is intentionally strict and happens before any
        # preset work, run-dir creation, or scheduling.
        for i, spec in enumerate(tasks):
            if not isinstance(spec, dict):
                return {"status": "error", "message": f"tasks[{i}] must be an object"}
            if "system_prompt" in spec:
                return {
                    "status": "error",
                    "message": (
                        f"tasks[{i}].system_prompt is obsolete; put the complete "
                        "daemon system instruction (role, constraints, tool policy, "
                        "collaboration boundaries, and safety posture) in task"
                    ),
                }
            if backend != "lingtai" and "prompt" in spec:
                return {
                    "status": "error",
                    "message": f"tasks[{i}].prompt is supported only for backend='lingtai'; external CLI tasks use task as their CLI prompt",
                }
            if "prompt" in spec:
                try:
                    self._task_first_prompt(spec)
                except ValueError as exc:
                    return {"status": "error", "message": f"tasks[{i}].prompt: {exc}"}

        # Pre-flight: optional per-task input files. Resolve every path under
        # the parent working directory, validate UTF-8 text and the practical
        # limits, and snapshot the bytes content-addressed BEFORE any run-dir
        # creation, preset work, or scheduling — a single bad entry refuses the
        # whole batch loudly and nothing is left half-started. Workers receive
        # only the compact manifest/snapshot paths, never file contents.
        task_files_rows: list[list[dict] | None] = []
        for i, spec in enumerate(tasks):
            try:
                rows = self._snapshot_task_files(spec)
            except ValueError as exc:
                return {"status": "error", "message": f"tasks[{i}].task_files: {exc}"}
            task_files_rows.append(rows)

        # Per-batch limit overrides. max_turns is capped at the manager's
        # ceiling (self._max_turns, also the schema maximum); timeout has no
        # schema maximum, so self._timeout is only the default when omitted.
        # None means "use the default/ceiling".
        if max_turns is not None:
            try:
                mt = int(max_turns)
            except (TypeError, ValueError):
                return {"status": "error",
                        "message": f"max_turns must be a positive integer (got {max_turns!r})"}
            if mt <= 0:
                return {"status": "error",
                        "message": f"max_turns must be ≥ 1 (got {mt})"}
            effective_max_turns = min(mt, self._max_turns)
        else:
            effective_max_turns = self._max_turns

        # The caller's own timeout, kept separate from the effective one: the
        # default ceiling says nothing about how long this batch will run, but
        # an explicitly requested long ceiling does.
        requested_timeout: float | None = None
        if timeout is not None:
            try:
                to = float(timeout)
            except (TypeError, ValueError):
                return {"status": "error",
                        "message": f"timeout must be a positive number (got {timeout!r})"}
            # Floor at 5s — the watchdog ticks at 1s granularity and the
            # OS scheduler may delay the watchdog thread's first run, so a
            # sub-5s timeout can fire before any emanation thread starts and
            # mark them as 'timeout' without ever running.
            if to < 5:
                return {"status": "error",
                        "message": f"timeout must be ≥ 5 seconds (got {to})"}
            # Unlike max_turns, the schema advertises no ceiling for timeout
            # (only minimum: 5) — self._timeout is the default when omitted,
            # not a cap on an explicit value.
            effective_timeout = to
            requested_timeout = effective_timeout
        else:
            effective_timeout = self._timeout

        # Clear completed emanations and stale pools.
        # Keep completed CLI emanations (backend != lingtai) so that `ask`
        # can still route to `_handle_ask_cli` / `_handle_ask_codex` /
        # `_handle_ask_opencode` / `_handle_ask_cursor`
        # and `list` can show them. Detached entries (no in-process "future")
        # are always kept in the registry here — their own supervisor process
        # owns the run's actual lifetime, and `check`/`list` fall back to
        # durable daemon.json anyway once the entry is eventually dropped by
        # a shutdown/reclaim sweep, so pruning them on a live future.done()
        # check does not apply.
        self._emanations = {
            k: v for k, v in self._emanations.items()
            if "future" not in v
            or not v["future"].done() or v.get("backend") not in (None, "lingtai")
        }
        self._pools = [(p, c) for p, c in self._pools if not c.is_set()]

        # --- External CLI backends: skip preset resolution entirely ---
        backend_spec = _backend_spec(backend)
        if backend_spec is not None and backend_spec.is_cli:
            return self._handle_emanate_cli(
                tasks, backend=backend,
                effective_max_turns=effective_max_turns,
                effective_timeout=effective_timeout,
                requested_timeout=requested_timeout,
                task_files_rows=task_files_rows,
            )

        # Pre-flight: validate per-task ``context_token_limit`` (LingTai backend
        # only — external CLI backends never reach this point, see the
        # ``backend_spec.is_cli`` return above). A single bad value refuses the
        # whole batch, consistent with the other pre-flight gates below. Bound
        # to a provider-specific standalone-compaction feature at construction
        # time (Codex's ``codex_compact_token_limit`` or the native ``mimo``
        # provider's ``mimo_compact_token_limit`` — see
        # ``_daemon_provider_defaults``), but validated generically here so the
        # schema/error shape does not leak provider identity into the daemon
        # tool surface.
        for spec in tasks:
            raw_limit = spec.get("context_token_limit")
            if raw_limit is None:
                continue
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                return {
                    "status": "error",
                    "message": (
                        f"context_token_limit must be a positive integer "
                        f"(got {raw_limit!r})"
                    ),
                }
            if raw_limit <= 0:
                return {
                    "status": "error",
                    "message": f"context_token_limit must be ≥ 1 (got {raw_limit})",
                }

        # --- Authorization gate (LingTai backend only): explicit per-task
        # presets must be in the parent's manifest.preset.allowed. This runs
        # before ANY LingTai side effect (preset load/connectivity/capability
        # setup, run-dir/thread-pool/schedule/dispatch). Omitted tasks[].preset
        # is unaffected — it is the documented parent-derived/no-preset path —
        # and the raw allowlist is only read when at least one task actually
        # requests an explicit preset, so the no-preset path never consults it.
        if any(spec.get("preset") for spec in tasks):
            from lingtai.kernel.presets import _preset_ref_in

            raw_preset_block = self._runtime.read_preset_from_init()
            allowed = (
                raw_preset_block.get("allowed")
                if isinstance(raw_preset_block, dict) else None
            )

            for spec in tasks:
                requested = spec.get("preset")
                if not requested:
                    continue
                if not _preset_ref_in(requested, allowed, working_dir=self._workdir.path):
                    self._log("daemon_preset_refused_unauthorized", requested=requested)
                    return {
                        "status": "error",
                        "message": (
                            f"preset {requested!r} is not in this agent's allowed "
                            f"list — call system(action='presets') to see what's available"
                        ),
                    }

        # Pre-flight: resolve any per-task presets BEFORE scheduling.
        # If any preset is invalid, refuse the whole batch. Presets are
        # identified by path (~/foo.json, ./foo.json, or absolute).
        from lingtai.kernel.preset_connectivity import check_connectivity

        resolved_presets: list[dict | None] = []  # one entry per task — None means inherit
        for spec in tasks:
            preset_name = spec.get("preset")
            if not preset_name:
                resolved_presets.append(None)
                continue
            # Validate preset exists and is loadable. Resolve through the agent's
            # composed preset-loader hook so the daemon never constructs a
            # migration workspace adapter itself.
            try:
                preset = self._runtime.load_preset(preset_name)
            except (KeyError, ValueError) as e:
                return {"status": "error",
                        "message": f"preset {preset_name!r} unloadable: {e}"}
            preset_llm = preset.get("manifest", {}).get("llm", {})
            # Connectivity check — refuse upfront rather than burning tokens later
            conn = check_connectivity(
                provider=preset_llm.get("provider"),
                base_url=preset_llm.get("base_url"),
                api_key_env=preset_llm.get("api_key_env"),
            )
            if conn["status"] != "ok":
                return {"status": "error",
                        "message": f"preset {preset_name!r}: {conn['status']} — "
                                   f"{conn.get('error', 'cannot reach LLM')}"}
            preset_caps = preset.get("manifest", {}).get("capabilities", {})
            # Instantiate preset capabilities into a sandbox up front so any
            # setup-time failure refuses the whole batch (consistent with
            # connectivity refusal). Empty caps dict → empty sandbox surface,
            # which means the emanation only gets task-scoped MCP tools —
            # that's a valid if unusual configuration.
            try:
                preset_schemas, preset_handlers = self._instantiate_preset_capabilities(
                    preset_caps,
                    preset_llm,
                    required_tools=self._expand_requested_tools(spec.get("tools", [])),
                )
            except ValueError as e:
                return {"status": "error",
                        "message": f"preset {preset_name!r}: {e}"}
            resolved_presets.append({
                "name": preset_name,
                "llm": preset_llm,
                "capabilities": preset_caps,
                "preset_schemas": preset_schemas,
                "preset_handlers": preset_handlers,
            })

        # Every task is one derived launch request. Admit the entire batch
        # before publishing its shared immutable task-file store: a denial in
        # any later task must not leave earlier task input blobs or manifests
        # behind. The decisions remain task-indexed for downstream Driver
        # adapters, which attach one child endpoint lease to each launch.
        from lingtai.kernel.provider_admission import DerivedLaunchAdmissionError
        try:
            launch_decisions = self._authorize_derived_launch_batch(
                "daemon", len(tasks)
            )
        except DerivedLaunchAdmissionError as error:
            return self._admission_error_result(error)

        ids = []
        group_id = DaemonRunDir.new_group_id()
        parent_addr = self._workdir.path.name
        parent_pid = os.getpid()
        use_central_manager = self._should_use_central_daemon_manager(len(tasks))

        # One compact manifest per dispatch/group next to the immutable blobs;
        # each run's durable metadata points at it (see call_parameters below).
        task_files_manifest = None
        if any(rows for rows in task_files_rows):
            try:
                task_files_manifest = self._materialize_task_files(
                    group_id, task_files_rows
                )
            except (OSError, ValueError) as exc:
                self._close_unconsumed_derived_launch_decisions(launch_decisions)
                return {"status": "error", "message": f"task_files: {exc}"}

        for i, spec in enumerate(tasks):
            em_id = self._new_emanation_id(reserved_ids=set(ids))
            ids.append(em_id)
            resolved = resolved_presets[i]
            effective_llm = (
                resolved["llm"] if resolved else self._implicit_parent_preset_llm()
            )
            effective_model = effective_llm["model"]

            # Build tool surface and system prompt up front so the run_dir
            # records the prompt verbatim before any LLM call. Validation
            # (unknown tools) raises here and aborts before scheduling.
            preset_surface = None
            if resolved is not None:
                preset_surface = (
                    resolved["preset_schemas"],
                    resolved["preset_handlers"],
                )
            task_mcp_clients: list[object] = []
            try:
                task_mcp_regs, task_mcp_catalog = self._task_mcp_registrations(spec)
                task_skill_catalog = self._task_skill_catalog(spec)
                task_plugin_catalog, plugin_skill_rows, plugin_mcp_regs = self._task_plugin_context(spec)
                task_files_catalog = self._render_task_files_catalog(task_files_rows[i])
            except Exception as e:
                self._close_task_mcp_clients(task_mcp_clients)
                self._close_unconsumed_derived_launch_decisions(launch_decisions)
                return {"status": "error", "message": str(e)}
            # Plugin skills join the skill catalog; plugin mcp.json servers join
            # the task MCP registrations (mounted as task-scoped clients below).
            if plugin_skill_rows:
                task_skill_catalog = self._render_task_skill_catalog(
                    self._merge_skill_catalog_rows(task_skill_catalog, plugin_skill_rows)
                )
            task_mcp_regs = list(task_mcp_regs) + list(plugin_mcp_regs)
            task_context = self._combine_oneshot_context(
                None, task_skill_catalog, task_mcp_catalog, task_plugin_catalog,
                task_files_catalog=task_files_catalog,
            )
            task_context = self._append_daemon_common_context(task_context)

            system_prompt = "[daemon prompt pending MCP startup]"

            # Construct run_dir — creates folder on disk, writes daemon.json,
            # .prompt, .heartbeat, daemon_start event. If FS construction fails,
            # propagate as a tool-level error and skip scheduling for this spec.
            try:
                run_dir = DaemonRunDir(
                    parent_working_dir=self._workdir.path,
                    handle=em_id,
                    run_id=em_id,
                    task=spec["task"],
                    tools=spec["tools"],
                    model=effective_model,
                    max_turns=effective_max_turns,
                    timeout_s=effective_timeout,
                    parent_addr=parent_addr,
                    parent_pid=parent_pid,
                    system_prompt=system_prompt,
                    group_id=group_id,
                    call_parameters={
                        "task": spec["task"],
                        "tools": spec.get("tools", []),
                        "skills": spec.get("skills", []),
                        "mcp": [],
                        "prompt": self._task_first_prompt(spec),
                        "context_token_limit": spec.get("context_token_limit"),
                        "task_files": {
                            "manifest": task_files_manifest,
                            "files": task_files_rows[i],
                        },
                    },
                    log_callback=self._log,
                    preset_name=resolved["name"] if resolved else None,
                    preset_provider=resolved["llm"].get("provider") if resolved else None,
                    preset_model=resolved["llm"].get("model") if resolved else None,
                )
            except OSError as e:
                self._close_task_mcp_clients(task_mcp_clients)
                self._close_unconsumed_derived_launch_decisions(launch_decisions)
                return {"status": "error",
                        "message": f"Failed to create daemon folder: {e}"}

            # Detached ownership is unconditional. The supervisor reconstructs
            # preset/MCP/skills from this run's validated, redacted durable
            # specification; no future or CLI process remains in the parent.
            try:
                task_mcp_regs = self._with_daemon_common_mcp(task_mcp_regs, run_dir)
                if "email" in (spec.get("tools") or []):
                    task_mcp_regs = self._with_daemon_email_mcp(
                        task_mcp_regs, run_dir, self._workdir.path,
                    )
                task_mcp_catalog = self._render_task_mcp_catalog(task_mcp_regs)
                task_context = self._combine_oneshot_context(
                    None, task_skill_catalog, task_mcp_catalog, task_plugin_catalog,
                    task_files_catalog=task_files_catalog,
                )
                task_context = self._append_daemon_common_context(task_context)
                # Detached ownership starts task MCP only in the supervisor.
                # The parent validates/serializes the passive catalog and builds
                # only the parent-independent portion of the prompt surface.
                # This deliberately avoids launching an MCP process or HTTP
                # client before the owning supervisor exists.
                schemas, dispatch = self._build_tool_surface(
                    spec["tools"],
                    preset_surface=preset_surface,
                    mcp_surface=({}, {}),
                )
                system_prompt = self._build_emanation_prompt(
                    spec["task"], schemas, system_prompt=task_context
                )
                run_dir.prompt_path.write_text(system_prompt, encoding="utf-8")
                call_parameters = dict(run_dir.state_snapshot()["call_parameters"])
                call_parameters["mcp"] = [
                    self._redact_mcp_registration_for_prompt(r)
                    for r in task_mcp_regs
                ]
                run_dir.update_state(call_parameters=call_parameters)
            except Exception as e:
                self._close_task_mcp_clients(task_mcp_clients)
                run_dir.mark_failed(e)
                self._close_unconsumed_derived_launch_decisions(launch_decisions)
                return {"status": "error", "message": str(e)}

            self._close_task_mcp_clients(task_mcp_clients)  # none connected in this branch
            try:
                self._commit_dispatch(run_dir)
                self._spawn_detached_lingtai_run(
                    run_dir,
                    task=spec["task"],
                    tools=spec["tools"],
                    max_turns=effective_max_turns,
                    timeout_s=effective_timeout,
                    group_id=group_id,
                    effective_llm=effective_llm,
                    context_token_limit=spec.get("context_token_limit"),
                    prompt=self._task_first_prompt(spec),
                    mcp=task_mcp_regs,
                    preset_name=resolved["name"] if resolved else None,
                    preset_llm=resolved["llm"] if resolved else None,
                    preset_capabilities=resolved["capabilities"] if resolved else None,
                    authority_lease=launch_decisions[i].child_endpoint_lease,
                    use_central_manager=use_central_manager,
                )
            except Exception as e:
                run_dir.mark_failed(e)
                self._close_unconsumed_derived_launch_decisions(launch_decisions)
                result = {"status": "error", "message": str(e)}
                from lingtai.kernel.provider_admission import DerivedLaunchAdmissionError

                if isinstance(e, DerivedLaunchAdmissionError):
                    result["reason_code"] = e.decision.reason_code
                    result["audit_id"] = e.decision.audit_id
                return result
            self._emanations[em_id] = {
                "detached": True,
                "task": spec["task"],
                "start_time": time.time(),
                "timeout_s": effective_timeout,
                "run_dir": run_dir,
                "backend": "lingtai",
            }

        self._log("daemon_emanate", ids=ids, group_id=group_id, count=len(tasks),
                  tasks=[{"task": s["task"][:80], "tools": s["tools"]} for s in tasks])

        return {"status": "dispatched", "count": len(tasks), "ids": ids,
                "group_id": group_id,
                "handoff": self._emanate_handoff(len(tasks), requested_timeout)}

    def _commit_dispatch(self, run_dir: DaemonRunDir) -> None:
        """Commit a newly accepted run before any detached execution starts."""
        state = run_dir.state_snapshot()
        created_at = state.get("started_at")
        if not isinstance(created_at, str) or not created_at:
            raise dispatch_ledger.DispatchLedgerError("initial daemon state lacks started_at")
        record = dispatch_ledger.append_dispatch(
            self._workdir.path, run_id=run_dir.run_id, created_at=created_at
        )
        # The recovery marker follows the durable ledger commit and precedes
        # launch.  Its failure is loud, so no accepted run starts untracked.
        run_dir.enable_dispatch_tracking(record.sequence)

    def _emanate_handoff(self, count: int, requested_timeout_s: float | None) -> str:
        """Async handoff line, plus a Task Card nudge for fleet-scale dispatch.

        The nudge is conditional twice over: only for a fleet or a deliberately
        long single run, and only when no watch is already running — a quick
        daemon, or one dispatched under a live card, gets the plain handoff.
        """
        if not self._should_nudge_task_card(count, requested_timeout_s):
            return DAEMON_ASYNC_HANDOFF
        return DAEMON_ASYNC_HANDOFF + DAEMON_CARD_NUDGE.format(count=count)

    def _should_nudge_task_card(
        self, count: int, requested_timeout_s: float | None
    ) -> bool:
        """True when this dispatch is card-worthy and no watch is active.

        Duck-typed on purpose: daemon still neither imports nor requires Task
        Card runtime code, so an agent whose capability is disabled simply
        never gets nudged.
        """
        if count < DAEMON_CARD_NUDGE_MIN_TASKS and (
            requested_timeout_s is None
            or requested_timeout_s < DAEMON_CARD_NUDGE_MIN_TIMEOUT_S
        ):
            return False
        watch_active = self._runtime.has_active_task_card_watch()
        return watch_active is False

    def _handle_emanate_cli(
        self,
        tasks: list[dict],
        backend: str,
        effective_max_turns: int,
        effective_timeout: float,
        requested_timeout: float | None = None,
        task_files_rows: list[list[dict] | None] | None = None,
    ) -> dict:
        """Dispatch emanations via an external CLI backend.

        Skips preset resolution — the CLI manages its own tools/model/provider.
        Creates a DaemonRunDir for tracking. CLI output is persisted in the
        run directory; only terminal completion/failure emits a compact
        system notification.
        """
        backend_spec = _backend_spec(backend)
        if (
            backend_spec is None
            or not backend_spec.is_cli
            or backend_spec.runner_attr is None
        ):
            return {"status": "error", "message": f"Unknown CLI backend: {backend}"}

        # A constrained Driver profile grants a one-use endpoint for a
        # LingTai-owned child.  An external CLI has no contract for that
        # endpoint.  Reject before asking the authority, materializing task
        # files, creating a run directory, or queuing work: requesting a grant
        # first would create a misleading Driver audit event for a backend that
        # can never consume it.
        if self._runtime.requires_derived_launch_admission:
            return {
                "status": "error",
                "message": (
                    "external CLI daemon backends are unavailable under Driver admission"
                ),
                "reason_code": "driver_external_cli_backend_unsupported",
                "audit_id": None,
            }

        # Pre-flight: validate per-task backend_options BEFORE creating any
        # run_dir or scheduling work, so a single bad spec refuses the whole
        # batch with a clear message instead of leaving half-spawned daemons.
        contexts: list[_CliTaskContext] = []
        for i, spec in enumerate(tasks):
            try:
                task_skill_catalog = self._task_skill_catalog(spec)
                task_mcp_regs, task_mcp_catalog = self._task_mcp_registrations(spec)
                task_plugin_catalog, plugin_skill_rows, plugin_mcp_regs = self._task_plugin_context(spec)
                if any(r.get("name") == _DAEMON_COMMON_MCP_NAME for r in task_mcp_regs):
                    raise ValueError(
                        f"MCP registration name {_DAEMON_COMMON_MCP_NAME!r} is reserved"
                    )
            except ValueError as e:
                return {"status": "error",
                        "message": f"tasks[{i}]: {e}"}
            # CLI backends that cannot mount plugins receive the plugin's skills
            # and mcp.json servers separately (flattened) in addition to the
            # whole-plugin prompt view, so the information is never lost.
            if plugin_skill_rows:
                task_skill_catalog = self._render_task_skill_catalog(
                    self._merge_skill_catalog_rows(task_skill_catalog, plugin_skill_rows)
                )
            task_mcp_regs = list(task_mcp_regs) + list(plugin_mcp_regs)
            if task_mcp_catalog:
                task_mcp_catalog = self._render_task_mcp_catalog(task_mcp_regs)
            # Per Jason 2026-08-09: external CLI backends do not receive the
            # whole-plugin prompt section for the next few weeks; they get the
            # plugin's skills and mcp.json servers flattened into the ordinary
            # skill/mcp oneshot context (already done above). The LingTai
            # backend alone injects the ``## Parent-selected plugins`` section.
            task_plugin_catalog = None
            raw_opts = spec.get("backend_options")
            backend_env: dict[str, str] = {}
            if raw_opts is None:
                backend_argv = []
            else:
                try:
                    backend_argv, backend_env = _backend_options_to_argv_and_env(
                        raw_opts
                    )
                    _validate_claude_backend_argv(backend, backend_argv)
                except ValueError as e:
                    return {"status": "error",
                            "message": f"tasks[{i}].backend_options: {e}"}
            contexts.append(_CliTaskContext(
                backend_argv=backend_argv,
                system_prompt=None,
                skill_catalog=task_skill_catalog,
                mcp_catalog=task_mcp_catalog,
                mcp_regs=task_mcp_regs,
                backend_env=backend_env,
            ))

        # Every task is one derived launch request. Complete all admission
        # decisions before publishing the batch's shared immutable task-file
        # store, so a later denial cannot retain an earlier task's input.
        from lingtai.kernel.provider_admission import DerivedLaunchAdmissionError
        try:
            launch_decisions = self._authorize_derived_launch_batch(
                "daemon", len(tasks)
            )
        except DerivedLaunchAdmissionError as error:
            return self._admission_error_result(error)
        if self._has_unhandoffable_driver_leases(launch_decisions):
            self._close_unconsumed_derived_launch_decisions(launch_decisions)
            return self._driver_handoff_unavailable_result()

        ids = []
        group_id = DaemonRunDir.new_group_id()
        parent_addr = self._workdir.path.name
        parent_pid = os.getpid()
        use_central_manager = self._should_use_central_daemon_manager(len(tasks))

        # The preflight validated every task input file; materialize the blobs
        # and one compact per-dispatch manifest for the group's runs.
        task_files_rows = task_files_rows or [None] * len(tasks)
        task_files_manifest = None
        if any(rows for rows in task_files_rows):
            try:
                task_files_manifest = self._materialize_task_files(
                    group_id, task_files_rows
                )
            except (OSError, ValueError) as exc:
                return {"status": "error", "message": f"task_files: {exc}"}

        for i, (spec, context) in enumerate(zip(tasks, contexts)):
            em_id = self._new_emanation_id(reserved_ids=set(ids))
            ids.append(em_id)
            task_files_catalog = self._render_task_files_catalog(task_files_rows[i])
            user_backend_argv = list(context.backend_argv)
            backend_argv = list(user_backend_argv)
            backend_harness_argv: list[str] = []
            state_updates: dict = {}
            backend_options = spec.get("backend_options") or None
            from lingtai.kernel.daemon_supervisor.manifest import redact_durable_value
            public_backend_options = (
                redact_durable_value(backend_options, field="backend_options")
                if backend_options is not None else None
            )
            system_prompt = f"[{backend} backend — task delegated to external CLI]"

            task_context = self._combine_oneshot_context(
                context.system_prompt, context.skill_catalog, context.mcp_catalog,
                task_files_catalog=task_files_catalog,
            )
            if _cli_backend_loads_common_mcp(backend):
                task_context = self._append_daemon_common_context(task_context)
            if task_context:
                system_prompt += (
                    "\n\nParent-provided daemon context (oneshot):\n"
                    + task_context
                )
            try:
                run_dir = DaemonRunDir(
                    parent_working_dir=self._workdir.path,
                    handle=em_id,
                    run_id=em_id,
                    task=spec["task"],
                    tools=spec.get("tools", []),
                    # Cursor's CLI is the source of model identity; do not
                    # mislabel the daemon backend as an upstream model.
                    model="unknown" if backend == "cursor" else backend,
                    max_turns=effective_max_turns,
                    timeout_s=effective_timeout,
                    parent_addr=parent_addr,
                    parent_pid=parent_pid,
                    system_prompt=system_prompt,
                    group_id=group_id,
                    call_parameters={
                        "task": spec["task"],
                        "tools": spec.get("tools", []),
                        "skills": spec.get("skills", []),
                        "mcp": [],
                        "backend_options": public_backend_options,
                        "task_files": {
                            "manifest": task_files_manifest,
                            "files": task_files_rows[i],
                        },
                    },
                    log_callback=self._log,
                    backend=backend,
                )
            except OSError as e:
                return {"status": "error",
                        "message": f"Failed to create daemon folder: {e}"}

            try:
                mcp_regs = (
                    self._with_daemon_common_mcp(context.mcp_regs, run_dir)
                    if _cli_backend_loads_common_mcp(backend)
                    else list(context.mcp_regs)
                )
                if _cli_backend_loads_common_mcp(backend) and "email" in (spec.get("tools") or []):
                    mcp_regs = self._with_daemon_email_mcp(
                        mcp_regs, run_dir, self._workdir.path,
                    )
                mcp_catalog = self._render_task_mcp_catalog(mcp_regs)
                task_context = self._combine_oneshot_context(
                    None, context.skill_catalog, mcp_catalog,
                    task_files_catalog=task_files_catalog,
                )
                if _cli_backend_loads_common_mcp(backend):
                    task_context = self._append_daemon_common_context(task_context)
                system_prompt = f"[{backend} backend — task delegated to external CLI]"
                if task_context:
                    system_prompt += (
                        "\n\nParent-provided daemon context (oneshot):\n"
                        + task_context
                    )
                run_dir.prompt_path.write_text(system_prompt, encoding="utf-8")
                call_parameters = dict(run_dir.state_snapshot()["call_parameters"])
                call_parameters["mcp"] = [
                    self._redact_mcp_registration_for_prompt(r)
                    for r in mcp_regs
                ]
                run_dir.update_state(call_parameters=call_parameters)
                cli_task = self._compose_cli_task(
                    spec["task"], task_context, backend=backend,
                )
                if backend_spec.runner_attr == "_run_claude_code_emanation":
                    mcp_config_path = self._write_claude_mcp_config(run_dir, mcp_regs)
                    backend_harness_argv = [
                        "--mcp-config", str(mcp_config_path),
                        "--strict-mcp-config",
                    ]
                elif backend == "codex" and _cli_backend_loads_common_mcp(backend):
                    backend_harness_argv = _codex_mcp_argv(mcp_regs)
                elif backend == "opencode" and _cli_backend_loads_common_mcp(backend):
                    opencode_env = _opencode_mcp_env(mcp_regs)
                    if opencode_env:
                        backend_harness_argv = [
                            "__lingtai_opencode_config_content",
                            opencode_env["OPENCODE_CONFIG_CONTENT"],
                        ]
                elif backend == "qwen-code" and _cli_backend_loads_common_mcp(backend):
                    qwen_settings = _write_qwen_mcp_settings(run_dir, mcp_regs)
                    backend_harness_argv = [
                        "__lingtai_qwen_system_settings_path",
                        str(qwen_settings),
                    ]
                elif backend == "kimicode" and _cli_backend_loads_common_mcp(backend):
                    kimi_mcp_config = _write_kimicode_mcp_config(run_dir, mcp_regs)
                    state_updates["backend_harness_files"] = {
                        "kimicode_mcp_config": str(kimi_mcp_config)
                    }
                backend_argv = [*user_backend_argv, *backend_harness_argv]
            except Exception as e:
                run_dir.mark_failed(e)
                return {"status": "error", "message": str(e)}

            # Persist user-supplied options separately from harness-owned argv
            # so run artifacts do not imply the model supplied MCP loader flags.
            from lingtai.kernel.daemon_supervisor.manifest import (
                redact_durable_argv, redact_durable_value,
            )
            if backend_options is not None:
                state_updates["backend_options"] = redact_durable_value(
                    backend_options, field="backend_options"
                )
                state_updates["backend_argv"] = redact_durable_argv(user_backend_argv)
            if backend_harness_argv:
                state_updates["backend_harness_argv"] = redact_durable_argv(
                    backend_harness_argv
                )
            if state_updates:
                run_dir.update_state(**state_updates)
            self._log("daemon_backend_options",
                      em_id=em_id, backend=backend,
                      argv=redact_durable_argv(user_backend_argv),
                      harness_argv=redact_durable_argv(backend_harness_argv))

            # All backend execution now crosses the same detached supervisor
            # boundary.  The parent writes a complete, redacted manifest and
            # retains only the durable run-dir facade.
            try:
                self._commit_dispatch(run_dir)
                from lingtai.kernel.daemon_supervisor import DaemonSupervisorRequest
                from lingtai.kernel.daemon_supervisor.manifest import build_manifest, manifest_path_for, write_manifest
                from .supervisor_runtime import select_daemon_supervisor_adapter
                from lingtai.adapters.posix.daemon_supervisor import selected_credential_environment
                manifest = build_manifest(
                    run_id=run_dir.run_id,
                    backend=backend,
                    parent_working_dir=str(self._workdir.path),
                    run_dir=str(run_dir.path),
                    task=cli_task,
                    tools=spec.get("tools", []),
                    max_turns=effective_max_turns,
                    timeout_s=effective_timeout,
                    group_id=group_id,
                    mcp=mcp_regs,
                    backend_argv=backend_argv,
                    language=self._runtime.language,
                )
                write_manifest(run_dir.path, manifest)
                request = DaemonSupervisorRequest(
                    run_id=run_dir.run_id,
                    manifest_path=str(manifest_path_for(run_dir.path)),
                    python_executable=sys.executable,
                )
                capsule = {
                    "task": cli_task,
                    "mcp": list(mcp_regs),
                    "backend_argv": list(backend_argv),
                    "credential_env": selected_credential_environment(backend),
                }
                # The reserved ``backend_options.env`` overlay travels in the
                # one-shot capsule only: durable state redacts every ``env``
                # container's values, so the manifest cannot carry it.
                if context.backend_env:
                    capsule["backend_env"] = dict(context.backend_env)
                if use_central_manager:
                    self._enqueue_central_daemon_manager_run(
                        request,
                        capsule=capsule,
                        pool_size=self._manager_pool_size,
                        run_dir=run_dir,
                    )
                else:
                    select_daemon_supervisor_adapter().spawn_detached(
                        request, capsule=capsule,
                    )
                    self._await_supervisor_startup(run_dir)
            except Exception as e:
                run_dir.mark_failed(e)
                return {"status": "error", "message": str(e)}
            self._emanations[em_id] = {
                "detached": True,
                "task": spec["task"],
                "start_time": time.time(),
                "timeout_s": effective_timeout,
                "run_dir": run_dir,
                "backend": backend,
                "followup_lock": threading.Lock(),
                "ask_in_flight": False,
                "ask_future": None,
            }

        # Detached supervisors enforce their own deadlines; every entry above
        # is detached, so there is no parent pool or watchdog to run here.
        self._log("daemon_emanate", ids=ids, group_id=group_id, count=len(tasks), backend=backend,
                  tasks=[{"task": s["task"][:80], "tools": s.get("tools", [])} for s in tasks])
        return {"status": "dispatched", "count": len(tasks), "ids": ids,
                "group_id": group_id, "backend": backend,
                "handoff": self._emanate_handoff(len(tasks), requested_timeout)}

    @staticmethod
    def _truncate_list_string(value: object, limit: int = 500) -> object:
        if not isinstance(value, str):
            return value
        if len(value) <= limit:
            return value
        return value[:limit] + "…[truncated]"

    @staticmethod
    def _list_search_blob(info: dict) -> str:
        try:
            return json.dumps(info, ensure_ascii=False, sort_keys=True).lower()
        except (TypeError, ValueError):
            return str(info).lower()

    def _daemon_prompt_preview(self, run_path: Path, limit: int = 500) -> tuple[str | None, int | None]:
        prompt_path = run_path / ".prompt"
        try:
            size = prompt_path.stat().st_size
            with open(prompt_path, encoding="utf-8") as f:
                text = f.read(limit + 1)
        except (OSError, UnicodeDecodeError):
            return None, None
        return self._truncate_list_string(text, limit), size

    def _daemon_list_entry_from_state(
        self,
        state: dict,
        run_path: Path,
        *,
        active_status: str | None = None,
        active_elapsed: int | None = None,
        active_error: BaseException | None = None,
    ) -> dict:
        status = active_status or state.get("state") or "unknown"
        call_params = state.get("call_parameters")
        if not isinstance(call_params, dict):
            call_params = {}
        prompt_preview, prompt_chars = self._daemon_prompt_preview(run_path)
        visible_call_params = {
            "task": self._truncate_list_string(call_params.get("task", state.get("task"))),
            "tools": call_params.get("tools", state.get("tools", [])),
            "skills": call_params.get("skills", []),
            "mcp": call_params.get("mcp", []),
            "system_prompt_preview": self._truncate_list_string(call_params.get("system_prompt")),
            "context_token_limit": call_params.get("context_token_limit"),
        }
        visible_call_params = {k: v for k, v in visible_call_params.items() if v not in (None, [], "")}
        info = {
            "id": state.get("handle"),
            "task": self._truncate_list_string(state.get("task", ""), 120),
            "status": status,
            "data_version": state.get("data_version"),
            "migration": state.get("migration"),
            "elapsed_s": active_elapsed if active_elapsed is not None else state.get("elapsed_s"),
            "run_id": state.get("run_id"),
            "group_id": state.get("group_id"),
            "backend": state.get("backend"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "path": str(run_path),
            "result_preview": state.get("result_preview"),
            "result_path": state.get("result_path"),
            "call_parameters": visible_call_params,
        }
        if prompt_preview is not None:
            info["system_prompt_preview"] = prompt_preview
            info["system_prompt_bytes"] = prompt_chars
            info["system_prompt_path"] = str(run_path / ".prompt")
        if active_error is not None:
            info["error"] = str(active_error)
        elif state.get("error"):
            info["error"] = state.get("error")
        return {k: v for k, v in info.items() if v is not None}

    @staticmethod
    def _utc_iso_from_timestamp(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _started_at_from_run_id(run_id: str) -> str | None:
        match = re.match(r"^em-\d+-(\d{8}-\d{6})-[0-9a-fA-F]+$", run_id)
        if not match:
            return None
        try:
            dt = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _handle_from_run_id(run_id: str) -> str | None:
        match = re.match(r"^(em-\d+)-", run_id)
        return match.group(1) if match else None

    @staticmethod
    def _atomic_write_daemon_json(path: Path, state: dict) -> None:
        atomic_write_json(path, state, ensure_ascii=False, indent=2)

    @staticmethod
    def _looks_like_daemon_run_dir(run_path: Path) -> bool:
        # Leading-underscore entries under the daemons root are internal-only
        # (e.g. the ``_task_files`` task input store) and never runs; this keeps
        # list/recovery/check scans from surfacing the store as an emanation.
        return (
            run_path.is_dir()
            and not run_path.name.startswith("_")
            and (
                run_path.name.startswith("em-")
                or (run_path / ".prompt").exists()
                or (run_path / "result.txt").exists()
                or (run_path / "logs" / "events.jsonl").exists()
            )
        )

    def _read_daemon_events_tail(self, run_path: Path, max_lines: int = 80) -> list[dict]:
        events_path = run_path / "logs" / "events.jsonl"
        try:
            size = events_path.stat().st_size
            with open(events_path, "rb") as f:
                f.seek(max(0, size - 65536))
                raw = f.read()
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines()[-max_lines:]
        except OSError:
            return []
        events: list[dict] = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _infer_task_from_prompt(self, run_path: Path) -> str | None:
        prompt_path = run_path / ".prompt"
        try:
            with open(prompt_path, encoding="utf-8") as f:
                text = f.read(self._system_prompt_budget_chars)
        except (OSError, UnicodeDecodeError):
            return None
        markers = ["\nYour task:\n", "\nTask:\n"]
        best = None
        best_idx = -1
        for marker in markers:
            idx = text.rfind(marker)
            if idx > best_idx:
                best = marker
                best_idx = idx
        if best is None or best_idx < 0:
            return None
        task = text[best_idx + len(best):].strip()
        if not task:
            return None
        return str(self._truncate_list_string(task, 2000))

    def _infer_terminal_state_from_events(self, events: list[dict]) -> tuple[str | None, str | None, object | None]:
        for event in reversed(events):
            name = event.get("event")
            if name == "daemon_done":
                return "done", event.get("ts"), None
            if name == "daemon_error":
                error = {
                    "type": event.get("exception") or "DaemonError",
                    "message": event.get("message") or "daemon failed",
                }
                return "failed", event.get("ts"), error
            if name == "daemon_cancelled":
                return "cancelled", event.get("ts"), None
            if name == "daemon_timeout":
                return "timeout", event.get("ts"), None
        return None, None, None

    def _result_preview_from_file(self, run_path: Path) -> tuple[str | None, str | None]:
        result_path = run_path / "result.txt"
        try:
            with open(result_path, encoding="utf-8") as f:
                text = f.read(201)
        except (OSError, UnicodeDecodeError):
            return None, None
        preview = text[:200]
        return preview, str(result_path)

    def _handle_list_from_ledger(
        self,
        *,
        query: str,
        wanted_status: str,
        include_done: bool,
        limit_int: int,
    ) -> dict:
        """Build list from append-order ledger, never historical directories."""
        # Every list/filter path reads one caller-selected bounded ledger window;
        # filters never promote the default EOF tail into full-history hydration.
        # created_at is never re-sorted.
        _ledger, rows, warnings = dispatch_ledger.read_recent_daemon_states(
            self._workdir.path,
            limit=limit_int,
            full_history=False,
        )
        entries: list[dict] = []
        known_run_ids: set[str] = set()
        for record, run_path, state in reversed(rows):
            known_run_ids.add(record.run_id)
            info = self._daemon_list_entry_from_state(state, run_path)
            entries.append(info)

        # Overlay current in-memory ownership without reconstructing old disk
        # history. This preserves active facade truth during supervisor startup.
        for em_id, entry in self._emanations.items():
            run_dir = entry.get("run_dir")
            if run_dir is None:
                # A live legacy in-memory worker has no durable run directory
                # to add to the ledger yet. It is still current registry truth,
                # not history reconstruction, and remains visible until its
                # normal run-dir path takes over.
                future = entry.get("future")
                done = bool(future is not None and future.done())
                failed = bool(done and future is not None and future.exception() is not None)
                entries.insert(0, {
                    "id": em_id,
                    "run_id": em_id,
                    "status": "failed" if failed else ("done" if done else "running"),
                    "task": entry.get("task", ""),
                    "elapsed_s": max(0.0, time.time() - float(entry.get("start_time", time.time()))),
                    "turn": 0,
                    "current_tool": None,
                })
                continue
            if run_dir.run_id in known_run_ids:
                continue
            try:
                state = self._read_run_dir_state_from_disk(run_dir) if entry.get("detached", False) else run_dir.state_snapshot()
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            state.setdefault("handle", em_id)
            entries.insert(0, self._daemon_list_entry_from_state(state, run_dir.path))

        total_before_filter = len(entries)
        if wanted_status and wanted_status != "all":
            entries = [item for item in entries if str(item.get("status", "")).lower() == wanted_status]
        if not include_done:
            entries = [item for item in entries if str(item.get("status", "")).lower() not in {"done", "failed", "cancelled", "timeout"}]
        if query:
            entries = [item for item in entries if query in self._list_search_blob(item)]
        total_matches = len(entries)
        selected = entries[:limit_int]
        return {
            "emanations": selected,
            "running": sum(1 for item in entries if item.get("status") in {"running", "active"}),
            "manager_pool_size": self._manager_pool_size,
            "history_included": include_done,
            "index": "dispatch_ledger",
            "total_before_filter": total_before_filter,
            "total_matches": total_matches,
            "showing": len(selected),
            "warnings": warnings,
        }

    def _handle_list_without_query(
        self,
        *,
        wanted_status: str,
        include_done: bool,
        limit_int: int,
    ) -> dict:
        return self._handle_list_from_ledger(
            query="", wanted_status=wanted_status, include_done=include_done, limit_int=limit_int
        )

    def _handle_list(
        self,
        contains: str | None = "",
        status_filter: str | None = "all",
        include_done: bool = True,
        limit: int | None = None,
    ) -> dict:
        try:
            limit_int = self._LIST_DEFAULT_LAST if limit is None else int(limit)
        except (TypeError, ValueError):
            return {"status": "error", "message": f"last must be a positive integer (got {limit!r})"}
        if limit_int < 1:
            return {"status": "error", "message": f"last must be >= 1 (got {limit_int})"}
        return self._handle_list_from_ledger(
            query=(contains or "").strip().lower(),
            wanted_status=(status_filter or "all").strip().lower(),
            include_done=include_done is not False,
            limit_int=limit_int,
        )

    def _durable_detached_entry(self, em_id: str) -> dict | None:
        """Hydrate a control facade from exact durable run identity.

        This never adopts execution ownership: the returned entry contains only
        a disk-attached run directory and public metadata.  The supervisor PID
        and start token are checked before a request can be submitted.
        """
        resolved = self._resolve_historical_run_dir(em_id)
        if resolved is None:
            return None
        run_path, matches = resolved
        if len(matches) != 1:
            return None
        try:
            state = DaemonRunDir.read_state_from_disk(run_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if state.get("run_id") != run_path.name:
            return None
        pid = state.get("supervisor_pid")
        terminal = state.get("state") in {"done", "failed", "cancelled", "timeout"}
        if not terminal:
            if state.get("owner") not in {"supervisor", "manager"}:
                return None
            if not isinstance(pid, int) or isinstance(pid, bool):
                return None
            if not self._pid_identity_matches(pid, state.get("supervisor_start_identity")):
                return None
        try:
            attached = DaemonRunDir.attach(run_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return {
            "detached": True,
            "task": state.get("task", ""),
            "start_time": time.time(),
            "timeout_s": state.get("timeout_s", self._timeout),
            "run_dir": attached,
            "backend": state.get("backend", "lingtai"),
            "followup_lock": threading.Lock(),
            "ask_in_flight": False,
            "ask_future": None,
            "durable_facade": True,
        }

    def _handle_ask(self, em_id: str, message: str) -> dict:
        entry = self._emanations.get(em_id)
        if not entry:
            entry = self._durable_detached_entry(em_id)
            if entry is not None:
                self._emanations[em_id] = entry
        if not entry:
            return {"status": "error", "message": f"Unknown emanation: {em_id}"}

        # CLI backends with resumable sessions:
        #   - claude / claude-interactive: interactive `claude --resume ...`
        #   - claude-p / claude-code:      `claude --resume ... --print`
        #   - codex:                       `codex exec resume <codex_session_id>`
        #   - opencode:                    `opencode run --session <opencode_session_id> ...`
        #   - mimocode:                    `mimo run --session <mimocode_session_id> ...`
        #   - oh-my-pi:                    `omp --mode json --session <oh_my_pi_session_id> ...`
        #   - cursor:                      `agent -p --resume <cursor_session_id> ...`
        # Qwen Code headless mode does not expose a stable resume contract here.
        # All stream progress into the daemon run directory so
        # `daemon(check)` shows live progress.
        # Every production entry is created with "detached": True (see
        # `_handle_emanate`/`_handle_emanate_cli`/`_durable_detached_entry`), so
        # this always takes the detached branch in production. The legacy
        # backend-specific dispatch below stays reachable only when a test
        # constructs a non-detached entry directly, exercising
        # `_handle_ask_cli`/`_handle_ask_codex`/etc. in isolation.
        if entry.get("detached"):
            return self._handle_ask_detached(em_id, entry, message)

        backend = entry.get("backend")
        backend_spec = _backend_spec(backend)
        if backend_spec is not None and backend_spec.is_cli:
            if backend_spec.ask_handler_attr is None:
                return {"status": "error", "id": em_id,
                        "message": backend_spec.ask_unsupported_msg}
            ask_handler = getattr(self, backend_spec.ask_handler_attr)
            return ask_handler(em_id, entry, message)

        if entry["future"].done():
            return {"status": "error", "message": "not running"}
        with entry["followup_lock"]:
            if entry["followup_buffer"]:
                entry["followup_buffer"] += "\n\n" + message
            else:
                entry["followup_buffer"] = message
        self._log("daemon_ask", em_id=em_id, message_length=len(message))
        return {"status": "sent", "id": em_id}

    def _handle_ask_detached(self, em_id: str, entry: dict, message: str) -> dict:
        """Follow-up for a detached lingtai run: submit via the control spool.

        The facade has no in-process ``followup_buffer``/session to write
        into — the supervisor process owns those. This writes a durable
        ``ask`` control request the supervisor's control-and-deadline watcher
        thread drains (see ``lingtai.tools.daemon.supervisor_runtime``),
        mirroring the in-process followup_buffer mechanism across the process
        boundary.
        """
        from lingtai.kernel.daemon_supervisor import control

        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}
        state = self._read_run_dir_state_from_disk(run_dir)
        backend = entry.get("backend", state.get("backend", "lingtai"))
        spec = _backend_spec(backend)
        if backend == "lingtai":
            if state.get("state") not in ("running", "active"):
                return {"status": "error", "message": f"not running (state={state.get('state')!r})"}
            if state.get("owner") not in {"supervisor", "manager"}:
                return {"status": "error", "message": "detached run owner is not confirmed"}
            pid = state.get("supervisor_pid")
            if not isinstance(pid, int) or not self._pid_identity_matches(
                pid, state.get("supervisor_start_identity")
            ):
                return {"status": "error", "message": "detached supervisor identity is not live"}
            control.submit_request(run_dir.path, "ask", {"message": message})
            self._log("daemon_ask_detached", em_id=em_id, message_length=len(message))
            return {"status": "sent", "id": em_id}
        if spec is None:
            return {"status": "error", "id": em_id,
                    "message": f"unknown backend {backend!r}"}
        state_name = state.get("state")
        if state_name in {"running", "active"}:
            if _cli_backend_loads_common_mcp(backend):
                try:
                    message_id = run_dir.enqueue_checkpoint_message(message)
                except (ValueError, RuntimeError) as exc:
                    return {"status": "error", "id": em_id, "message": str(exc)}
                if message_id:
                    self._log(
                        "daemon_ask_checkpoint_queued",
                        em_id=em_id,
                        message_id=message_id,
                        message_length=len(message),
                    )
                    return {
                        "status": "queued",
                        "id": em_id,
                        "delivery": "checkpoint",
                        "message_id": message_id,
                    }
                state = self._read_run_dir_state_from_disk(run_dir)
                state_name = state.get("state")
            if state_name in {"running", "active"}:
                if spec.ask_handler_attr is None:
                    return {"status": "error", "id": em_id,
                            "message": spec.ask_unsupported_msg}
                return {"status": "busy", "id": em_id,
                        "message": "primary detached CLI run is still active; retry ask after terminal state"}
        if spec.ask_handler_attr is None:
            return {"status": "error", "id": em_id,
                    "message": spec.ask_unsupported_msg}
        if state_name not in {"done", "failed", "cancelled", "timeout"}:
            return {"status": "busy", "id": em_id,
                    "message": "primary detached CLI run is still active; retry ask after terminal state"}
        session_key = {
            "claude": "claude_session_id", "claude-interactive": "claude_session_id",
            "claude-p": "claude_session_id", "claude-code": "claude_session_id",
            "codex": "codex_session_id", "opencode": "opencode_session_id",
            "mimocode": "mimocode_session_id", "oh-my-pi": "oh_my_pi_session_id",
            "cursor": "cursor_session_id",
        }.get(backend)
        if not session_key or not state.get(session_key):
            return {"status": "error", "id": em_id,
                    "message": f"No {backend} session ID found for {em_id}"}
        claim = run_dir.claim_resume_generation()
        if claim.get("status") == "busy":
            return {"status": "busy", "id": em_id,
                    "message": f"a previous ask on {em_id} is still running; retry after it completes"}
        from lingtai.adapters.posix.daemon_supervisor import selected_credential_environment
        from lingtai.kernel.daemon_supervisor.manifest import manifest_path_for
        from .supervisor_runtime import select_daemon_supervisor_adapter
        try:
            select_daemon_supervisor_adapter().spawn_resume_owner(
                python_executable=sys.executable,
                manifest_path=str(manifest_path_for(run_dir.path)),
                run_id=run_dir.run_id, run_dir=run_dir.path,
                generation=claim["generation"], capsule={
                    "message": message,
                    "claim_nonce": claim["launch_nonce"],
                    "credential_env": selected_credential_environment(backend),
                },
            )
        except Exception as exc:
            run_dir.release_resume_generation(
                claim["generation"], claim["launch_nonce"],
                owner_pid=os.getpid(),
                owner_identity=claim.get("owner_start_identity"),
                result_status="failed",
            )
            run_dir.record_followup(
                claim["generation"], status="failed", error=f"{type(exc).__name__}: {exc}",
            )
            return {"status": "error", "id": em_id, "message": str(exc)}
        self._log("daemon_ask_detached_resume", em_id=em_id,
                  generation=claim["generation"], message_length=len(message))
        return {"status": "sent", "id": em_id, "generation": claim["generation"],
                "async": True, "message": "detached resume owner started; inspect daemon(action='check')"}

    @staticmethod
    def _read_run_dir_state_from_disk(run_dir: DaemonRunDir) -> dict:
        """Best-effort fresh disk read of *run_dir*'s daemon.json.

        Falls back to the (possibly stale) in-memory snapshot only if the
        disk read itself fails — e.g. a transient race with an in-progress
        atomic write — so callers always get a dict shape back.
        """
        try:
            return DaemonRunDir.read_state_from_disk(run_dir.path)
        except (OSError, json.JSONDecodeError, ValueError):
            return run_dir.state_snapshot()

    def _handle_ask_claude_interactive(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch an interactive Claude ``--resume`` follow-up asynchronously."""
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("claude_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No claude session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        try:
            run_dir.record_cli_output(
                f"[interactive ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_claude_interactive_stream,
            em_id, entry, message, session_id, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future
        return {"status": "sent", "id": em_id, "async": True,
                "message": "interactive ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_claude_interactive_stream(
        self,
        em_id: str,
        entry: dict,
        message: str,
        session_id: str,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker for interactive Claude ``--resume`` follow-ups."""
        ask_cancel = threading.Event()
        ask_timeout = threading.Event()
        parent_cancel = entry.get("cancel_event")
        monitor_done = threading.Event()

        def _timeout() -> None:
            ask_timeout.set()
            ask_cancel.set()

        def _mirror_parent_cancel() -> None:
            if parent_cancel is None:
                return
            while not monitor_done.is_set():
                if parent_cancel.is_set():
                    ask_cancel.set()
                    return
                monitor_done.wait(0.05)

        timer = threading.Timer(self._timeout, _timeout)
        timer.daemon = True
        timer.start()
        monitor = threading.Thread(
            target=_mirror_parent_cancel,
            daemon=True,
            name=f"daemon-claude-interactive-ask-cancel-{em_id}",
        )
        monitor.start()
        try:
            try:
                result = run_claude_interactive(
                    em_id=em_id,
                    run_dir=run_dir,
                    working_dir=self._workdir.path,
                    task=message,
                    cancel_event=ask_cancel,
                    timeout_event=ask_timeout,
                    resume_session_id=session_id,
                    env=_claude_code_env(),
                    log_callback=self._log,
                    terminal_port=self._interactive_terminal_port,
                )
            except Exception as e:
                err = f"interactive claude ask failed: {e}"
                self._publish_followup_if_live(
                    em_id, status="follow-up failed", text=err, run_dir=run_dir,
                )
                return {"status": "error", "id": em_id, "message": err}
            if ask_timeout.is_set():
                err = f"interactive claude ask timed out after {self._timeout}s"
                self._publish_followup_if_live(
                    em_id, status="follow-up failed", text=err, run_dir=run_dir,
                )
                return {"status": "error", "id": em_id, "message": err}
            output = (result.final_text or "").strip()
            if output:
                self._publish_followup_if_live(
                    em_id, status="follow-up completed", text=output, run_dir=run_dir,
                )
            return {"status": "sent", "id": em_id, "output": output}
        finally:
            timer.cancel()
            monitor_done.set()
            monitor.join(timeout=0.2)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

    def _handle_ask_cli(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a Claude Code `--resume` follow-up off the caller's turn.

        Returns immediately after spawning the subprocess; the stream-json
        parse runs in ``self._ask_pool``. Progress + final reply still land
        in ``run_dir`` (``cli_output`` events, ``last_output``, and a
        ``follow-up completed`` notification on success), so ``daemon(check)``
        observes the ask just as it did when this method was synchronous.

        Refuses a second concurrent ask against the same emanation with
        ``status="busy"`` — ``claude --resume`` serializes per-session and
        a second spawn would either error or interleave reply text.
        """
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("claude_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No claude session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        # Concurrent-ask guard. Checked + set under followup_lock so two
        # parent tool calls racing on the same em_id can't both spawn.
        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        cmd = [
            "claude",
            "--resume", session_id,
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            message,
        ]
        self._log("daemon_claude_code_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        command = DaemonProcessCommand(
            tuple(cmd), self._workdir.path,
            tuple(_claude_code_env().items()),
        )
        try:
            handle = self._process_port.spawn(command, group_id=None)
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": "'claude' CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start claude CLI: {e}"}
        # Surface that an ask just started so `daemon(check)` shows it
        # immediately, even before any stream-json event arrives.
        # record_cli_output already routes its filesystem writes through
        # _safe (which catches OSError); the outer guard here is only for
        # the unlikely case the call site itself raises (e.g. attribute
        # access on a torn-down run_dir). Narrowed to OSError so real bugs
        # propagate.
        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_claude_code_stream, em_id, entry, handle, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_claude_code_stream(
        self,
        em_id: str,
        entry: dict,
        handle: DaemonProcessHandle,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker: stream a Claude Code `--resume` subprocess.

        Same stream-json parse as ``_run_claude_code_emanation``. Always
        clears ``ask_in_flight`` and releases the opaque Port handle.
        Return value is captured by the future for tests/debugging; the
        agent observes the result through the run_dir + notification.
        """
        stderr_thread = self._process_port.drain_stderr(
            handle,
            on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-claude-ask-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        final_result_text: str | None = None
        final_is_error = False
        timed_out = False
        # Buffered, not yet persisted — see the result-event handler and
        # the post-classification persistence below for why.
        usage_candidate: tuple[dict[str, int], dict] | None = None

        try:
            deadline = time.monotonic() + self._timeout
            for raw_line in self._process_port.iter_stdout(handle, deadline=deadline):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                etype = event.get("type")
                if etype == "assistant":
                    message_obj = event.get("message") or {}
                    for block in (message_obj.get("content") or []):
                        if block.get("type") == "text":
                            text = block.get("text") or ""
                            if text.strip():
                                run_dir.record_cli_output(text, stream="stdout")
                elif etype == "result":
                    final_result_text = event.get("result") or ""
                    final_is_error = bool(event.get("is_error"))
                    # Buffer the follow-up's token usage — do not persist
                    # yet. A result read right at the deadline can still
                    # be followed by a timeout classification below;
                    # persisting here would leave usage for a follow-up
                    # that is reported as failed. Retain only the first
                    # valid terminal usage candidate and persist once
                    # terminal classification (timeout, exit code,
                    # is_error) has passed — same UI-only, never-ledger
                    # policy as the initial emanation run.
                    if usage_candidate is None:
                        usage = _normalize_claude_usage(event.get("usage"))
                        if usage is not None:
                            usage_candidate = (usage, event.get("usage"))

            if time.monotonic() >= deadline:
                timed_out = True
                exit_receipt = self._process_port.terminate(handle, reason="timeout")
            else:
                # Reader hit EOF before the deadline. The CLI usually exits
                # within milliseconds of closing stdout, but bound the wait
                # so a misbehaving child can't strand us here.
                try:
                    exit_receipt = self._process_port.wait(
                        handle, timeout=max(1.0, deadline - time.monotonic())
                    )
                except TimeoutError:
                    timed_out = True
                    exit_receipt = self._process_port.terminate(handle, reason="timeout")
        except Exception:
            exit_receipt = self._process_port.terminate(handle, reason="error")
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            if ('exit_receipt' in locals() and exit_receipt is not None
                    and exit_receipt.returncode is not None):
                self._process_port.release(handle)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            if exit_receipt is not None:
                self._attributed_process_exit(
                    exit_receipt, "claude", stderr_tail[-500:], run_dir,
                )
            err = f"claude --resume timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if exit_receipt.returncode != 0:
            detail = stderr_tail or (final_result_text or "")
            attributed = self._attributed_process_exit(
                exit_receipt, "claude", detail[-500:], run_dir,
            )
            err = attributed or f"claude CLI exited {exit_receipt.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if final_is_error:
            err = (f"claude CLI reported is_error=true: "
                   f"{(final_result_text or stderr_tail)[-500:]}")
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if usage_candidate is not None:
            usage, raw = usage_candidate
            try:
                run_dir.record_cli_tokens(
                    input=usage["input"], output=usage["output"],
                    cached=usage["cached"], thinking=usage["thinking"],
                    raw=raw,
                )
            except Exception:
                pass

        output = (final_result_text or "").strip()
        if output:
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}

    def _handle_ask_codex(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a Codex ``exec resume`` follow-up off the caller's turn.

        Mirrors ``_handle_ask_cli``: spawn, register the proc, hand the
        JSONL stream parse to ``self._ask_pool``, return immediately.
        Concurrent-ask guard is the same — ``codex exec resume`` is
        single-writer per session.
        """
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("codex_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No codex session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        cmd = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            message,
        ]
        self._log("daemon_codex_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        try:
            handle = self._process_port.spawn(
                DaemonProcessCommand(tuple(cmd), self._workdir.path),
                group_id=None,
            )
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": "'codex' CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start codex CLI: {e}"}
        # Ask follow-ups are not part of any batch (see claude-code ask).
        # See _handle_ask_cli for the rationale on the narrowed except.
        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_codex_stream, em_id, entry, handle, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_codex_stream(
        self,
        em_id: str,
        entry: dict,
        handle: DaemonProcessHandle,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker: stream a ``codex exec resume`` subprocess.

        Same JSONL event vocabulary as ``_run_codex_emanation``:
        ``item.completed/agent_message`` for reply text, ``turn.completed``
        for terminal acknowledgement. Always clears ``ask_in_flight`` and
        releases the opaque Port handle after the stream ends.
        """
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-codex-ask-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        agent_message_texts: list[str] = []
        turn_completed = False
        timed_out = False

        try:
            deadline = time.monotonic() + self._timeout
            # The Port owns the queue-backed deadline reader so policy never
            # receives a concrete pipe or subprocess object.
            for raw_line in self._process_port.iter_stdout(handle, deadline=deadline):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                etype = event.get("type")
                if etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        text = item.get("text") or ""
                        if text.strip():
                            agent_message_texts.append(text)
                            run_dir.record_cli_output(text, stream="stdout")
                elif etype == "turn.completed":
                    # Resume streams should also account one terminal usage
                    # object at most; a repeated terminal line is not a new
                    # provider call.
                    if not turn_completed:
                        turn_completed = True
                        usage = _normalize_codex_usage(event.get("usage"))
                        if usage is not None:
                            try:
                                run_dir.record_cli_tokens(
                                    input=usage["input"],
                                    output=usage["output"],
                                    cached=usage["cached"],
                                    raw=event.get("usage"),
                                )
                            except Exception:
                                pass

            if time.monotonic() >= deadline:
                timed_out = True
                exit_receipt = self._process_port.terminate(handle, reason="timeout")
            else:
                try:
                    exit_receipt = self._process_port.wait(
                        handle, timeout=max(1.0, deadline - time.monotonic())
                    )
                except TimeoutError:
                    timed_out = True
                    exit_receipt = self._process_port.terminate(handle, reason="timeout")
        finally:
            stderr_thread.join(timeout=2.0)
            self._process_port.release(handle)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"codex exec resume timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if exit_receipt.returncode != 0:
            detail = stderr_tail or "\n".join(agent_message_texts[-3:])
            attributed = self._attributed_process_exit(
                exit_receipt, "codex", detail[-500:], run_dir,
            )
            err = attributed or f"codex CLI exited {exit_receipt.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if not turn_completed and not agent_message_texts:
            err = (f"codex exec resume produced no turn.completed event: "
                   f"{(stderr_tail or '[no output]')[-500:]}")
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        output = "\n".join(agent_message_texts).strip()
        if output:
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}

    # ------------------------------------------------------------------
    # OpenCode backend (opencode-ai CLI, `opencode run --format json`)
    # ------------------------------------------------------------------

    # OpenCode emits one JSON object per stdout line under ``--format json``.
    # The event vocabulary is less standardized than claude-code / codex —
    # field names vary by event family and version — so the parser is
    # intentionally defensive: it pulls text from any of several common
    # shapes and captures the session id from whichever event carries it
    # first. Unknown / non-JSON lines are still surfaced as cli_output so
    # nothing is lost.
    _OPENCODE_SESSION_FIELDS = (
        "session_id", "sessionID", "sessionId", "session",
        "thread_id", "threadId",
    )

    def _build_opencode_prompt(self, task: str) -> str:
        """Compose the initial prompt sent to ``opencode run``.

        OpenCode is being used as a one-shot daemon worker, not as an
        interactive session, so we wrap the user task with a short
        operating contract: write detailed work product to files in the
        parent working directory, and end with a concise final answer
        the parent agent can read at a glance.
        """
        return (
            "You are running as a LingTai daemon — a disposable subagent "
            "spawned by a parent LingTai agent to perform one task and "
            "report back.\n\n"
            "Operating contract:\n"
            "1. Do the task in the current working directory.\n"
            "2. If the answer is long, structured, or includes code, "
            "write the detailed output to a file (e.g. report.md, "
            "result.json) and reference it in your final answer.\n"
            "3. End with a concise final answer (a few short paragraphs "
            "or bullet points) summarising what you did and where to "
            "look for the full result.\n"
            "4. Do not ask the operator for clarification — make the "
            "best reasonable assumption and proceed.\n\n"
            f"Task:\n{task}"
        )

    @staticmethod
    def _opencode_extract_session_id(event: dict) -> str | None:
        """Pull a session-id-shaped string out of an opencode JSON event.

        OpenCode's event field naming is unstable across versions: a
        session-created style event may use ``session_id``, ``sessionID``,
        ``sessionId``, or a nested ``session.id``. Be defensive over all
        of them. Returns the first non-empty string found, or None.
        """
        for key in DaemonManager._OPENCODE_SESSION_FIELDS:
            val = event.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict):
                inner = val.get("id") or val.get("session_id") or val.get("sessionID")
                if isinstance(inner, str) and inner:
                    return inner
        # A bare top-level ``id`` is commonly an event/message id. Only treat
        # it as a session id when the event type is explicitly session-shaped.
        etype = event.get("type")
        if isinstance(etype, str) and "session" in etype.lower():
            val = event.get("id")
            if isinstance(val, str) and val:
                return val
        # Some opencode builds emit a ``data`` envelope on session events.
        data = event.get("data")
        if isinstance(data, dict):
            return DaemonManager._opencode_extract_session_id(data)
        return None

    @staticmethod
    def _opencode_extract_text(event: dict) -> str:
        """Best-effort text extraction from an opencode JSON event.

        Tries a handful of common shapes (top-level ``text`` / ``content``
        / ``message`` / ``delta``, content-block lists similar to
        Anthropic's, and Codex-style ``item.text``) and returns the first
        non-empty string. Returns "" when no text is present (events
        that are purely structural, e.g. tool calls, are skipped).
        """
        # Top-level scalar text fields.
        for key in ("text", "content", "message", "delta", "answer", "output", "result"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                return val
        # Content-block list (Anthropic-style).
        msg = event.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        t = block.get("text")
                        if isinstance(t, str) and t.strip():
                            parts.append(t)
                if parts:
                    return "\n".join(parts)
            elif isinstance(content, str) and content.strip():
                return content
        # Codex-style item.
        item = event.get("item")
        if isinstance(item, dict):
            t = item.get("text")
            if isinstance(t, str) and t.strip():
                return t
        return ""

    # MiMo Code JSONL contract (verified against MiMo Code 0.1.5). Unlike the
    # permissive OpenCode extractor, MiMo tags every event with ``type`` and
    # carries a nested ``part.text`` on MANY of them (reasoning, tool, step,
    # step-start, ...), not only the final answer. The generic extractor would
    # surface any of those ``part.text`` values as the daemon result, leaking
    # internal reasoning/tool chatter. The user-visible answer is ONLY the
    # ``type == "text"`` event's ``part.text`` string.
    @staticmethod
    def _mimocode_extract_answer_text(event: dict) -> str:
        """Return the MiMo answer text: ``part.text`` iff ``type == 'text'``.

        Reasoning/tool/step events also carry ``part.text``; they are ignored
        so only the model's user-visible answer becomes the daemon result.
        Returns "" for any non-answer or malformed event.
        """
        if event.get("type") != "text":
            return ""
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text
        return ""

    @staticmethod
    def _mimocode_extract_error(event: dict) -> str | None:
        """Return a bounded, secret-redacted detail for a MiMo error event.

        A structured ``type == "error"`` event must make the daemon fail
        loudly even when the process exits 0. MiMo Code 0.1.5's ``run.ts``
        emits ``emit("error", { error: props.error })`` and derives the useful
        detail as ``String(props.error.data.message)`` when present, else
        ``String(props.error.name)``. We pin that official shape first
        (``error.data.message`` → ``error.name``), then fall back to other safe
        human-readable fields for defensiveness. The chosen detail is redacted
        with the smallest suitable helper (``redact_text``) and only then
        bounded to the daemon's existing <=500-char convention, so a secret can
        never be split past the redactor; the raw nested payload is never
        surfaced. Returns None for any non-error event.
        """
        if event.get("type") != "error":
            return None
        err = event.get("error")
        # Priority chain of candidate detail sources, official 0.1.5 fields
        # first (``error.data.message`` → ``error.name``), then defensive
        # non-official fields. A truthy non-string or a whitespace-only
        # higher-priority field must NOT suppress a later valid string, so we
        # scan for the first nonblank string explicitly rather than relying on
        # ``or`` short-circuiting.
        candidates: list = []
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict):
                candidates.append(data.get("message"))  # official 0.1.5 shape
            candidates.append(err.get("name"))  # official fallback
            candidates.append(err.get("message"))  # defensive
            candidates.append(err.get("detail"))  # defensive
            candidates.append(err.get("reason"))  # defensive
        elif isinstance(err, str):
            candidates.append(err)
        candidates.append(event.get("message"))  # top-level fallback

        message: str | None = None
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                message = candidate
                break
        if message is None:
            message = "MiMo Code reported a structured error event"
        return redact_text(message)[:500]

    @staticmethod
    def _mimocode_normalize_usage(
        event: dict,
    ) -> tuple[str, dict[str, int], dict] | None:
        """Normalize one MiMo Code 0.1.5 ``step_finish`` usage event.

        MiMo's terminal JSON event is deliberately narrower than the generic
        OpenCode parser: only ``type == "step_finish"`` with a nested
        ``part.type == "step-finish"`` is accepted. Every source counter must
        be an actual non-negative ``int`` (booleans, strings, missing fields,
        and negatives are rejected), and ``part.id`` is the source-stable
        identity used for replay suppression. ``tokens.input`` already excludes
        cache usage, so it is copied directly rather than adjusted.
        """
        if not isinstance(event, dict) or event.get("type") != "step_finish":
            return None
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            return None
        part_id = part.get("id")
        if not isinstance(part_id, str) or not part_id.strip():
            return None
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            return None
        cache = tokens.get("cache")
        if not isinstance(cache, dict):
            return None
        values = {
            "input": tokens.get("input"),
            "output": tokens.get("output"),
            "reasoning": tokens.get("reasoning"),
            "cache_read": cache.get("read"),
            "cache_write": cache.get("write"),
        }
        if any(type(value) is not int or value < 0 for value in values.values()):
            return None
        normalized = {
            "input": values["input"],
            "output": values["output"],
            "cached": values["cache_read"] + values["cache_write"],
            "thinking": values["reasoning"],
        }
        if not any(normalized.values()):
            return None
        return part_id, normalized, part

    @staticmethod
    def _mimocode_usage_state(run_dir: DaemonRunDir) -> tuple[threading.Lock, set[str]]:
        """Return the per-run MiMo usage dedupe state."""
        return run_dir.__dict__.setdefault(
            "_mimocode_usage_state", (threading.Lock(), set()),
        )

    def _mimocode_record_usage(self, run_dir: DaemonRunDir, event: dict) -> None:
        """Persist one new MiMo usage part through the UI-only CLI hook."""
        normalized = self._mimocode_normalize_usage(event)
        if normalized is None:
            return
        part_id, usage, raw_part = normalized
        lock, seen_part_ids = self._mimocode_usage_state(run_dir)
        with lock:
            if part_id in seen_part_ids:
                return
            seen_part_ids.add(part_id)
            # record_cli_tokens mutates shared run state and appends its event;
            # keep both writes in the same per-run transaction as dedupe.
            run_dir.record_cli_tokens(**usage, raw=raw_part)

    def _run_opencode_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
        *,
        executable: str = "opencode",
        backend_name: str = "opencode",
        session_state_key: str = "opencode_session_id",
        cmd_prefix: list[str] | None = None,
        text_extractor: Callable[[dict], str] | None = None,
        error_detector: Callable[[dict], str | None] | None = None,
        usage_recorder: Callable[[dict], None] | None = None,
    ) -> str:
        """Run an OpenCode-family CLI session as the emanation backend.

        Spawns ``<executable> <cmd_prefix...> <backend_argv...> <prompt>`` and
        parses one JSON event per stdout line (``cmd_prefix`` defaults to
        ``["run", "--format", "json"]`` for OpenCode/MiMo; Oh-My-Pi passes
        ``["--mode", "json", "--approval-mode", "yolo"]``). Non-JSON lines are recorded
        as ``cli_output`` so nothing is silently dropped. The first event that
        carries a session-id-shaped field is stored in daemon.json under
        ``session_state_key`` (``opencode_session_id`` by default) — used later
        by ``daemon(action='ask')`` to resume the session.

        OpenCode-family event field naming is less standardized than
        claude-code or codex, so the default parser is intentionally
        permissive. ``text_extractor`` overrides how answer text is pulled from
        an event (MiMo Code passes a strict ``type == "text"``-only extractor so
        reasoning/tool/step ``part.text`` never leaks as the answer);
        ``error_detector`` lets a backend recognize a structured error event and
        fail loudly even on exit 0 (MiMo Code's ``type == "error"``). The
        optional ``usage_recorder`` receives each parsed event for a backend-
        specific UI-only usage path. See ``_opencode_extract_text`` /
        ``_opencode_extract_session_id`` for the default shapes accepted.
        """
        extract_text = text_extractor or self._opencode_extract_text
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        prompt = self._build_opencode_prompt(task)
        env_extra: dict[str, str] = {}
        raw_backend_argv = list(backend_argv or [])
        backend_argv = []
        idx = 0
        while idx < len(raw_backend_argv):
            token = raw_backend_argv[idx]
            if token == "__lingtai_opencode_config_content":
                idx += 1
                if idx < len(raw_backend_argv):
                    env_extra["OPENCODE_CONFIG_CONTENT"] = raw_backend_argv[idx]
            else:
                backend_argv.append(token)
            idx += 1

        # Required infrastructure flags come first; free-form
        # backend_options sit between them and the prompt positional so the
        # prompt stays the trailing argument the CLI expects.
        prefix = cmd_prefix if cmd_prefix is not None else ["run", "--format", "json"]
        cmd = [executable, *prefix]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(prompt)
        self._log(f"daemon_{backend_name}_start", em_id=em_id,
                  cmd_head=" ".join(cmd[:1 + len(prefix)]))

        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        # The caller's ``backend_options.env`` overlay is applied last: it wins
        # over both the inherited environment and the harness-owned env_extra.
        if backend_env:
            env.update(backend_env)
        command = DaemonProcessCommand(
            tuple(cmd), self._workdir.path, tuple(env.items()),
        )
        try:
            handle = self._process_port.spawn(command, group_id=run_dir.group_id)
        except FileNotFoundError:
            exc = RuntimeError(f"'{executable}' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start {backend_name} CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-{backend_name}-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        session_id_captured: str | None = None
        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        error_detail: str | None = None
        any_event = False

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            # The session id is established by the first session-shaped header.
            # Later OpenCode-family/Oh-My-Pi events may carry their own event ids;
            # do not let those overwrite a working resume id (overwrite=False).
            if run_dir.set_session_id(session_state_key, sid, overwrite=False):
                session_id_captured = sid
                self._log(f"daemon_{backend_name}_session", em_id=em_id, session_id=sid)

        try:
            for raw_line in self._process_port.iter_stdout(handle):
                if cancel_event.is_set():
                    self._process_port.terminate(
                        handle,
                        reason=("timeout" if timeout_event and timeout_event.is_set()
                                else "reclaim"),
                    )
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON line — record verbatim so the agent can
                    # still see banner / progress text that opencode
                    # didn't structure as an event.
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                sid = self._opencode_extract_session_id(event)
                if sid:
                    _store_session_id(sid)
                if usage_recorder is not None:
                    usage_recorder(event)

                # A structured backend error must fail the run loudly even when
                # the process later exits 0 (MiMo Code's ``type == "error"``).
                if error_detector is not None:
                    detail = error_detector(event)
                    if detail is not None:
                        error_detail = detail

                text = extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")

                # Capture a definitive final answer if the event signals
                # completion. OpenCode's "final" event names vary; we
                # accept any event whose ``type`` ends in a terminal-ish
                # token. Last-text-wins so a later result overrides
                # intermediate streaming.
                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    if low.endswith((".completed", ".done", ".finished",
                                     "result", "final")):
                        if text:
                            final_text = text

            exit_receipt = self._process_port.wait(handle)
        except Exception as e:
            self._process_port.terminate(handle)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._process_port.release(handle)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        # A structured error event is a terminal failure regardless of exit
        # code — a MiMo run that reports ``type:error`` then exits 0 must not
        # masquerade as success. The detail is already bounded + redacted by
        # the detector.
        if error_detail is not None:
            exc = RuntimeError(
                f"{backend_name} CLI reported a structured error: {error_detail}"
            )
            run_dir.mark_failed(exc)
            raise exc

        if exit_receipt.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            if cancel_event.is_set():
                self._attributed_process_exit(exit_receipt, backend_name, detail[-500:], run_dir)
                return _mark_cancelled_or_timeout(run_dir, timeout_event)
            attributed = self._attributed_process_exit(
                exit_receipt, backend_name, detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"{backend_name} CLI exited with code {exit_receipt.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # Choose the best final text: explicit terminal event > last text
        # chunk > stderr tail > no-output sentinel. ``any_event`` lets us
        # distinguish "process exited 0 but never spoke" from a real
        # silent success (which shouldn't happen, but be defensive).
        if final_text is not None:
            text = final_text.strip()
        elif text_chunks:
            text = text_chunks[-1].strip()
        elif stderr_tail:
            text = f"[no JSON events; stderr tail follows]\n{stderr_tail[-500:]}"
        else:
            text = "[no output]"
        if not any_event and not stderr_tail:
            text = "[no output]"

        self._require_done_completion(run_dir, text)
        run_dir.mark_done(text)
        return text

    def _run_mimocode_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run a MiMo Code CLI session as the emanation backend.

        MiMo Code's npm package ``@mimo-ai/cli`` exposes the ``mimo``
        executable and an OpenCode-derived ``run --format json`` command, so
        the OpenCode-family runner (session capture, argv placement, non-JSON
        tolerance) is reused with a distinct session-id field. MiMo's JSONL
        contract (0.1.5) differs from generic OpenCode in three ways the shared
        runner is told about: the user-visible answer is ONLY the
        ``type == "text"`` event's nested ``part.text`` (reasoning/tool/step
        events also carry ``part.text`` and must be ignored), a structured
        ``type == "error"`` event is a terminal failure even on exit 0, and
        source-reported ``step_finish`` usage is normalized and recorded via
        ``record_cli_tokens`` for UI totals only (duplicate ``part.id`` values
        are suppressed; neither token ledger is written).
        """
        return self._run_opencode_emanation(
            em_id, run_dir, task, cancel_event, timeout_event, backend_argv,
            backend_env,
            executable="mimo",
            backend_name="mimocode",
            session_state_key="mimocode_session_id",
            text_extractor=self._mimocode_extract_answer_text,
            error_detector=self._mimocode_extract_error,
            usage_recorder=lambda event: self._mimocode_record_usage(run_dir, event),
        )

    def _run_oh_my_pi_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run an Oh-My-Pi (``omp``) CLI session as the emanation backend.

        Oh-My-Pi's npm package ``@oh-my-pi/pi-coding-agent`` exposes the
        ``omp`` executable. ``--mode json`` makes it a non-interactive JSON
        event-stream printer (it first emits a ``type:session`` header whose
        ``id`` is the resumable session id, then one agent event per JSONL
        line); ``--approval-mode yolo`` lets the daemon proceed without interactive
        approval prompts. The OpenCode-family JSON parser is reused — its
        ``_opencode_extract_session_id`` already recognizes a ``type:session``
        header with a bare top-level ``id`` — with a distinct session-id field
        in daemon.json so ``daemon(action='ask')`` can resume via ``--session``.
        """
        return self._run_opencode_emanation(
            em_id, run_dir, task, cancel_event, timeout_event, backend_argv,
            backend_env,
            executable="omp",
            backend_name="oh-my-pi",
            session_state_key="oh_my_pi_session_id",
            cmd_prefix=["--mode", "json", "--approval-mode", "yolo"],
        )

    def _build_qwen_code_prompt(self, task: str) -> str:
        """Compose the prompt sent to Qwen Code headless mode."""
        return self._build_opencode_prompt(task)

    def _run_qwen_code_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run a Qwen Code CLI session as the emanation backend.

        Qwen Code documents headless mode as ``qwen -p <prompt>``. LingTai
        additionally owns ``--yolo`` so the daemon can proceed without
        interactive approval prompts. Qwen Code does not expose a stable
        machine-readable streaming/resume contract here, so stdout/stderr are
        recorded verbatim and ``daemon(action='ask')`` is intentionally
        unsupported for this backend.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        qwen_env: dict[str, str] = {}
        raw_backend_argv = list(backend_argv or [])
        backend_argv = []
        idx = 0
        while idx < len(raw_backend_argv):
            token = raw_backend_argv[idx]
            if token == "__lingtai_qwen_system_settings_path":
                idx += 1
                if idx < len(raw_backend_argv):
                    qwen_env["QWEN_CODE_SYSTEM_SETTINGS_PATH"] = raw_backend_argv[idx]
            else:
                backend_argv.append(token)
            idx += 1

        prompt = self._build_qwen_code_prompt(task)
        cmd = ["qwen", "--yolo"]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.extend(["-p", prompt])
        self._log("daemon_qwen_code_start", em_id=em_id, cmd_head=" ".join(cmd[:5]))

        try:
            env = os.environ.copy()
            env.update(qwen_env)
            # Caller overlay last: it wins over the harness-owned qwen env.
            if backend_env:
                env.update(backend_env)
            handle = self._process_port.spawn(
                DaemonProcessCommand(
                    tuple(cmd), self._workdir.path, tuple(env.items()),
                ),
                group_id=run_dir.group_id,
            )
        except FileNotFoundError:
            exc = RuntimeError("'qwen' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start qwen-code CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc

        stdout_lines: list[str] = []
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-qwen-code-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        try:
            for raw_line in self._process_port.iter_stdout(handle):
                if cancel_event.is_set():
                    self._process_port.terminate(
                        handle, reason=("timeout" if timeout_event and timeout_event.is_set()
                                        else "reclaim"),
                    )
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                stdout_lines.append(line)
                try:
                    run_dir.record_cli_output(line, stream="stdout")
                except Exception:
                    pass
            exit_receipt = self._process_port.wait(handle)
        except Exception as e:
            self._process_port.terminate(handle)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._process_port.release(handle)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
        output = "\n".join(stdout_lines).strip()

        if exit_receipt.returncode != 0:
            detail = stderr_tail or output
            if cancel_event.is_set():
                self._attributed_process_exit(
                    exit_receipt, "qwen-code", detail[-500:], run_dir,
                )
                return _mark_cancelled_or_timeout(run_dir, timeout_event)
            attributed = self._attributed_process_exit(
                exit_receipt, "qwen-code", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"qwen-code CLI exited with code {exit_receipt.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        text = output or (f"[no stdout; stderr tail follows]\n{stderr_tail[-500:]}" if stderr_tail else "[no output]")
        self._require_done_completion(run_dir, text)
        run_dir.mark_done(text)
        return text

    def _build_kimicode_prompt(self, task: str) -> str:
        """Compose the prompt sent to Kimi Code one-shot (``--prompt``) mode."""
        return self._build_opencode_prompt(task)

    @staticmethod
    def _kimicode_run_env(run_dir: DaemonRunDir) -> dict[str, str]:
        """Build the per-run environment overlay for a Kimi Code invocation.

        Returns only the keys to *add/override* on top of ``os.environ`` (the
        caller merges them). Contract sourced from the runyuan Kimi Code brief
        (no secrets):

        * ``KIMI_CODE_HOME`` — pinned to a run-private directory so concurrent
          daemon emanations never share Kimi's on-disk state.
        * Telemetry + auto-update disabled unconditionally for headless runs.
        * ``KIMI_MODEL_API_KEY`` — mapped from the first of
          ``KIMICODE_API_KEY`` / ``KIMI_API_KEY`` / ``MOONSHOT_API_KEY`` that is
          set, but only if ``KIMI_MODEL_API_KEY`` is not already provided. The
          value is never logged.
        * Model / provider / base URL / max-context defaults are applied only
          when absent, so an operator's explicit environment always wins.
        """
        env: dict[str, str] = {}
        kimi_home = _kimicode_home_dir(run_dir)
        try:
            kimi_home.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fall back to the run dir itself; Kimi will create subdirs it needs.
            kimi_home = run_dir.path
        env["KIMI_CODE_HOME"] = str(kimi_home)
        env["KIMI_DISABLE_TELEMETRY"] = "1"
        env["KIMI_CODE_NO_AUTO_UPDATE"] = "1"

        # API key: map only if the canonical var is not already set. Read from
        # the live process env; the value is copied verbatim and never logged.
        if not os.environ.get("KIMI_MODEL_API_KEY"):
            for src in ("KIMICODE_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY"):
                val = os.environ.get(src)
                if val:
                    env["KIMI_MODEL_API_KEY"] = val
                    break

        # Provider defaults — applied only when the operator has not set them.
        defaults = {
            "KIMI_MODEL_NAME": "kimi-for-coding",
            "KIMI_MODEL_PROVIDER_TYPE": "kimi",
            "KIMI_MODEL_BASE_URL": "https://api.kimi.com/coding/v1",
            "KIMI_MODEL_MAX_CONTEXT_SIZE": "262144",
        }
        for key, default in defaults.items():
            if not os.environ.get(key):
                env[key] = default
        return env

    def _run_kimicode_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run a Kimi Code (``kimi``) CLI session as the emanation backend.

        MoonshotAI's official ``kimi`` binary runs one-shot via
        ``kimi --prompt <prompt> --output-format text``. LingTai owns the
        ``--prompt`` and ``--output-format`` flags (and forbids ``--yolo``,
        which the CLI refuses alongside ``--prompt``), so free-form
        ``backend_options`` are inserted *before* those owned flags. Kimi Code
        does not expose a verified stable machine-readable session-id / resume
        contract here, so stdout/stderr are recorded verbatim and
        ``daemon(action='ask')`` is intentionally unsupported for this backend.

        The per-run environment (see ``_kimicode_run_env``) pins a run-private
        ``KIMI_CODE_HOME``, disables telemetry/auto-update, maps a Kimi/Moonshot
        API key onto ``KIMI_MODEL_API_KEY`` when absent, and fills in provider
        defaults only when the operator has not already set them. Secret values
        are never logged.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        kimi_env = self._kimicode_run_env(run_dir)
        backend_argv = list(backend_argv or [])

        prompt = self._build_kimicode_prompt(task)
        # Required infrastructure flags (``--prompt`` / ``--output-format``)
        # come last so the free-form backend_argv sits between the executable
        # and the owned flags; the prompt is never a trailing positional here
        # (Kimi takes it via ``--prompt``).
        cmd = ["kimi"]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.extend(["--prompt", prompt, "--output-format", "text"])
        self._log("daemon_kimicode_start", em_id=em_id, cmd_head=" ".join(cmd[:1]))

        try:
            env = os.environ.copy()
            env.update(kimi_env)
            # Caller overlay last: it wins over the run-private kimi env.
            if backend_env:
                env.update(backend_env)
            handle = self._process_port.spawn(
                DaemonProcessCommand(
                    tuple(cmd), self._workdir.path, tuple(env.items()),
                ),
                group_id=run_dir.group_id,
            )
        except FileNotFoundError:
            exc = RuntimeError("'kimi' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start kimicode CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc

        stdout_lines: list[str] = []
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-kimicode-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        try:
            for raw_line in self._process_port.iter_stdout(handle):
                if cancel_event.is_set():
                    self._process_port.terminate(
                        handle, reason=("timeout" if timeout_event and timeout_event.is_set()
                                        else "reclaim"),
                    )
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                stdout_lines.append(line)
                try:
                    run_dir.record_cli_output(line, stream="stdout")
                except Exception:
                    pass
            exit_receipt = self._process_port.wait(handle)
        except Exception as e:
            self._process_port.terminate(handle)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._process_port.release(handle)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
        output = "\n".join(stdout_lines).strip()

        if exit_receipt.returncode != 0:
            detail = stderr_tail or output
            if cancel_event.is_set():
                self._attributed_process_exit(
                    exit_receipt, "kimicode", detail[-500:], run_dir,
                )
                return _mark_cancelled_or_timeout(run_dir, timeout_event)
            attributed = self._attributed_process_exit(
                exit_receipt, "kimicode", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"kimicode CLI exited with code {exit_receipt.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        text = output or (f"[no stdout; stderr tail follows]\n{stderr_tail[-500:]}" if stderr_tail else "[no output]")
        self._require_done_completion(run_dir, text)
        run_dir.mark_done(text)
        return text

    def _build_deepseek_prompt(self, task: str) -> str:
        """Compose the prompt sent to DeepSeek Harness headless mode."""
        return self._build_opencode_prompt(task)

    @staticmethod
    def _deepseek_run_env(run_dir: DaemonRunDir) -> dict[str, str]:
        """Build the per-run environment overlay for a ``dsh`` invocation.

        Returns only the keys to *add/override* on top of ``os.environ`` (the
        caller merges them). Contract sourced from the official DeepSeek
        Harness CLI reference (see ``apps/cli/reference/README.md``, no
        secrets):

        * ``DSH_HOME`` — pinned to a run-private directory so the headless
          profile's first-use auto-initialization (shipped templates) and any
          per-profile settings stay inside the run, and concurrent daemon
          emanations never share DeepSeek Harness's on-disk state. The
          operator's machine-local ``$DSH_HOME/cordis.patch.yml`` and
          ``$DSH_HOME/.credentials.yaml`` are deliberately not honored;
          credentials must come from the inherited environment (e.g.
          ``DEEPSEEK_API_KEY``) or the invoking project's ``.env``.
        * ``DSH_TELEMETRY_DISABLED`` — any non-empty value is the official
          hard opt-out for session telemetry.
        """
        env: dict[str, str] = {}
        dsh_home = run_dir.path / "dsh-home"
        try:
            dsh_home.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fall back to the run dir itself; dsh will create subdirs it needs.
            dsh_home = run_dir.path
        env["DSH_HOME"] = str(dsh_home)
        env["DSH_TELEMETRY_DISABLED"] = "1"
        return env

    def _run_deepseek_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run a DeepSeek Harness (``dsh``) CLI session as the emanation backend.

        DeepSeek AI's official ``dsh`` CLI (npm package ``@deepseek-ai/dsh``)
        runs one-shot via ``dsh --profile headless <task>``: one fresh
        persisted session, the last non-empty assistant text on stdout, and
        exit 0 for completed / nonzero otherwise. One caveat: upstream's
        SIGTERM handler also exits 0 (``process.on('SIGTERM', () =>
        interrupt(0))``), so the runner never trusts a zero receipt alone -- a
        cancelled/timed-out run is classified from the events, not the code.
        LingTai owns the
        ``--profile headless`` launcher flags (the shipped headless app takes
        only the task text as its positional argument). ``--patch`` overlays
        remain available through free-form ``backend_options`` (the documented
        way to select models/providers for a one-shot run); every other
        launcher flag is reserved, and non-launcher flags would end up as app
        arguments and be rejected by the headless app as a usage error.
        DeepSeek Harness is a developer preview with no verified stable
        machine-readable session-id / resume contract for the headless profile
        here, so ``daemon(action='ask')`` is intentionally unsupported for
        this backend.

        The per-run environment (see ``_deepseek_run_env``) pins a run-private
        ``DSH_HOME`` so first-use profile auto-initialization never touches the
        operator's real home, and hard-disables session telemetry. Secret
        values are never logged.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        dsh_env = self._deepseek_run_env(run_dir)
        backend_argv = list(backend_argv or [])

        prompt = self._build_deepseek_prompt(task)
        # Launcher flags come first (boundary: first unrecognized token ends
        # launcher parsing and starts the app arguments); the harness-owned
        # ``--profile headless`` pair sits before the task, which is the
        # headless app's trailing positional argument.
        cmd = ["dsh"]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.extend(["--profile", "headless", prompt])
        self._log("daemon_deepseek_start", em_id=em_id, cmd_head=" ".join(cmd[:5]))

        try:
            env = os.environ.copy()
            env.update(dsh_env)
            # Caller overlay last: it wins over the run-private dsh env.
            if backend_env:
                env.update(backend_env)
            handle = self._process_port.spawn(
                DaemonProcessCommand(
                    tuple(cmd), self._workdir.path, tuple(env.items()),
                ),
                group_id=run_dir.group_id,
            )
        except FileNotFoundError:
            exc = RuntimeError("'dsh' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start deepseek CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc

        stdout_lines: list[str] = []
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-deepseek-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        try:
            for raw_line in self._process_port.iter_stdout(handle):
                if cancel_event.is_set():
                    self._process_port.terminate(
                        handle, reason=("timeout" if timeout_event and timeout_event.is_set()
                                        else "reclaim"),
                    )
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                stdout_lines.append(line)
                try:
                    run_dir.record_cli_output(line, stream="stdout")
                except Exception:
                    pass
            exit_receipt = self._process_port.wait(handle)
        except Exception as e:
            self._process_port.terminate(handle)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._process_port.release(handle)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
        output = "\n".join(stdout_lines).strip()
        detail = stderr_tail or output

        # Cancellation/timeout must win over a clean receipt: ``dsh`` exits 0
        # both when the session completed and when LingTai SIGTERMs it
        # (upstream `apps/cli/src/profile-boot.ts` installs
        # ``process.on('SIGTERM', () => interrupt(0))``), so a timed-out /
        # reclaimed run can hand back a zero return code here. The exit code
        # alone therefore cannot classify the terminal state -- only the events
        # can. (The qwen/kimi siblings need no such guard: they die with
        # -15/143 on SIGTERM, so their nonzero receipts catch cancellation.)
        if cancel_event.is_set():
            attributed = self._attributed_process_exit(
                exit_receipt, "deepseek", detail[-500:], run_dir,
            )
            if (
                attributed is None
                and exit_receipt.reason is not None
                and exit_receipt.returncode == 0
            ):
                # dsh masked the SIGTERM as exit 0, so the receipt carries no
                # raw signal for ``_attributed_process_exit`` to attribute;
                # stamp the local cause directly so the ``cli_termination``
                # forensic field survives on daemon.json.
                try:
                    run_dir.record_cli_termination(
                        reason=exit_receipt.reason,
                        signal_name="SIGTERM",
                        returncode=exit_receipt.returncode,
                    )
                except Exception:
                    pass
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        # ``dsh --profile headless`` exits 0 only for a completed session;
        # exit 1 covers usage errors, configuration/boot failures, and
        # non-completed sessions, so any nonzero receipt fails the run while
        # the printed text stays in the run dir for inspection.
        if exit_receipt.returncode != 0:
            attributed = self._attributed_process_exit(
                exit_receipt, "deepseek", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"deepseek CLI exited with code {exit_receipt.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        text = output or (f"[no stdout; stderr tail follows]\n{stderr_tail[-500:]}" if stderr_tail else "[no output]")
        self._require_done_completion(run_dir, text)
        run_dir.mark_done(text)
        return text

    def _handle_ask_opencode(
        self, em_id: str, entry: dict, message: str,
        *,
        executable: str = "opencode",
        backend_name: str = "opencode",
        session_state_key: str = "opencode_session_id",
        build_resume_cmd: Callable[[str, str, str], list[str]] | None = None,
        text_extractor: Callable[[dict], str] | None = None,
        error_detector: Callable[[dict], str | None] | None = None,
        usage_recorder: Callable[[dict], None] | None = None,
    ) -> dict:
        """Dispatch an OpenCode-family session-resume follow-up off the caller's turn.

        Mirrors ``_handle_ask_cli`` / ``_handle_ask_codex``: spawn the resume
        subprocess (``opencode run --session <id> ...`` by default), hand the
        JSON-stream parse to ``self._ask_pool``, return immediately. The
        concurrent-ask guard refuses overlapping asks per-emanation because
        resume is single-writer per session.

        ``build_resume_cmd(executable, session_id, message)`` overrides the
        argv for backends whose resume shape differs (e.g. Oh-My-Pi's
        ``omp --mode json --approval-mode yolo --session <id> <message>``).
        ``text_extractor`` / ``error_detector`` apply the same answer/error
        contract to the resume stream that the initial run used (MiMo Code
        passes its strict ``type:text`` / ``type:error`` handlers).
        ``usage_recorder`` provides the corresponding backend-specific usage
        path without changing the shared OpenCode behavior.
        """
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get(session_state_key)
        if not session_id:
            return {"status": "error",
                    "message": f"No {backend_name} session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        if build_resume_cmd is not None:
            cmd = build_resume_cmd(executable, session_id, message)
        else:
            cmd = [
                executable,
                "run",
                "--session", session_id,
                "--format", "json",
                message,
            ]
        self._log(f"daemon_{backend_name}_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        command = DaemonProcessCommand(tuple(cmd), self._workdir.path)
        try:
            handle = self._process_port.spawn(command, group_id=None)
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"'{executable}' CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start {backend_name} CLI: {e}"}
        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_opencode_stream, em_id, entry, handle, run_dir,
            backend_name, text_extractor, error_detector, usage_recorder,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _handle_ask_mimocode(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a MiMo Code ``mimo run --session`` follow-up.

        Resume argv stays the harness-owned
        ``mimo run --session <id> --format json <message>``; the MiMo
        answer/error/usage contract is applied to the resume stream too:
        source-reported ``step_finish`` usage is normalized and recorded via
        ``record_cli_tokens`` for UI totals only (duplicate ``part.id`` values
        are suppressed; neither token ledger is written), a ``type:error``
        follow-up fails loudly, and reasoning/tool ``part.text`` never surfaces
        as the reply.
        """
        return self._handle_ask_opencode(
            em_id, entry, message,
            executable="mimo",
            backend_name="mimocode",
            session_state_key="mimocode_session_id",
            text_extractor=self._mimocode_extract_answer_text,
            error_detector=self._mimocode_extract_error,
            usage_recorder=lambda event: self._mimocode_record_usage(
                entry["run_dir"], event,
            ),
        )

    @staticmethod
    def _oh_my_pi_resume_cmd(executable: str, session_id: str, message: str) -> list[str]:
        """Build the Oh-My-Pi resume argv: ``omp --mode json --approval-mode yolo
        --session <id> <message>``."""
        return [
            executable,
            "--mode", "json",
            "--approval-mode", "yolo",
            "--session", session_id,
            message,
        ]

    def _handle_ask_oh_my_pi(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch an Oh-My-Pi ``omp --mode json --session`` follow-up."""
        return self._handle_ask_opencode(
            em_id, entry, message,
            executable="omp",
            backend_name="oh-my-pi",
            session_state_key="oh_my_pi_session_id",
            build_resume_cmd=self._oh_my_pi_resume_cmd,
        )

    def _run_ask_opencode_stream(
        self,
        em_id: str,
        entry: dict,
        handle: DaemonProcessHandle,
        run_dir: DaemonRunDir,
        backend_name: str = "opencode",
        text_extractor: Callable[[dict], str] | None = None,
        error_detector: Callable[[dict], str | None] | None = None,
        usage_recorder: Callable[[dict], None] | None = None,
    ) -> dict:
        """Background worker: stream an ``opencode run --session`` subprocess.

        Same defensive JSON-line parse as ``_run_opencode_emanation``:
        non-JSON lines are recorded verbatim, text is pulled from any
        plausible field, terminal-shaped events override intermediate
        text. Always clears ``ask_in_flight`` and releases the opaque process
        handle on exit. ``text_extractor`` / ``error_detector`` mirror
        the initial-run overrides so a resumed MiMo stream applies the same
        answer/error contract (only ``type:text`` surfaces; ``type:error``
        fails the follow-up even on exit 0).
        """
        extract_text = text_extractor or self._opencode_extract_text
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-{backend_name}-ask-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        error_detail: str | None = None
        any_event = False
        timed_out = False

        try:
            deadline = time.monotonic() + self._timeout
            for raw_line in self._process_port.iter_stdout(handle, deadline=deadline):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                if usage_recorder is not None:
                    usage_recorder(event)
                if error_detector is not None:
                    detail = error_detector(event)
                    if detail is not None:
                        error_detail = detail
                text = extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")
                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    if low.endswith((".completed", ".done", ".finished",
                                     "result", "final")):
                        if text:
                            final_text = text

            if time.monotonic() >= deadline:
                timed_out = True
                exit_receipt = self._process_port.terminate(handle, reason="timeout")
            else:
                try:
                    exit_receipt = self._process_port.wait(
                        handle, timeout=max(1.0, deadline - time.monotonic())
                    )
                except TimeoutError:
                    timed_out = True
                    exit_receipt = self._process_port.terminate(handle, reason="timeout")
        except TimeoutError:
            timed_out = True
            exit_receipt = self._process_port.terminate(handle, reason="timeout")
        finally:
            stderr_thread.join(timeout=2.0)
            self._process_port.release(handle)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"{backend_name} run timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        # A structured error event fails the follow-up regardless of exit code,
        # so a resumed MiMo run that reports ``type:error`` then exits 0 does
        # not masquerade as a successful reply. Detail is bounded + redacted.
        if error_detail is not None:
            err = f"{backend_name} CLI reported a structured error: {error_detail}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if exit_receipt.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            attributed = self._attributed_process_exit(
                exit_receipt, backend_name, detail[-500:], run_dir,
            )
            err = attributed or f"{backend_name} CLI exited {exit_receipt.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if final_text is not None:
            output = final_text.strip()
        elif text_chunks:
            output = text_chunks[-1].strip()
        else:
            output = ""

        if not any_event and not output:
            output = "[no output]"

        if output and output != "[no output]":
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}


    # ------------------------------------------------------------------
    # Cursor backend (Cursor Agent CLI, `agent -p --output-format stream-json`)
    # ------------------------------------------------------------------

    # Cursor's headless CLI is exposed as the `agent` executable. In print mode
    # (`-p` / `--print`) it emits the source-pinned stream-json event shapes
    # documented by the installed 2026.05.28-a70ca7c bundle.  Keep the generic
    # text/session helpers for existing behavior, but keep usage/model parsing
    # strict to that version's terminal and init events.

    @staticmethod
    def _cursor_init_model(event: dict) -> tuple[str, str] | None:
        """Return a source-reported ``(session_id, model)`` init pair."""
        if event.get("type") != "system" or event.get("subtype") != "init":
            return None
        session_id = event.get("session_id")
        model = event.get("model")
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(model, str)
            or not model
        ):
            return None
        return session_id, model

    @staticmethod
    def _cursor_result_session_id(event: dict) -> str | None:
        """Return only the terminal event's top-level ``session_id``."""
        session_id = event.get("session_id")
        return session_id if isinstance(session_id, str) and session_id else None

    @staticmethod
    def _cursor_set_model(run_dir: DaemonRunDir, model: str) -> None:
        """Persist a model learned from a preceding matching Cursor init."""
        run_dir.update_state(model=model)

    def _cursor_process_usage_event(
        self,
        event: dict,
        run_dir: DaemonRunDir,
        init_models: dict[str, str],
        usage_candidate: tuple[dict[str, int], dict] | None,
    ) -> tuple[dict[str, int], dict] | None:
        """Join source model and retain the first valid terminal usage candidate."""
        init_model = self._cursor_init_model(event)
        if init_model is not None:
            init_models[init_model[0]] = init_model[1]
            return usage_candidate

        if event.get("type") != "result":
            return usage_candidate

        session_id = self._cursor_result_session_id(event)
        if session_id is not None:
            model = init_models.get(session_id)
            if model is not None:
                self._cursor_set_model(run_dir, model)

        if usage_candidate is not None:
            return usage_candidate
        usage = _normalize_cursor_usage(event)
        if usage is None:
            return None
        return usage, event["usage"]

    @staticmethod
    def _cursor_record_usage_candidate(
        run_dir: DaemonRunDir,
        usage_candidate: tuple[dict[str, int], dict] | None,
    ) -> None:
        """Persist a buffered usage candidate after stream success only."""
        if usage_candidate is None:
            return
        usage, raw = usage_candidate
        try:
            run_dir.record_cli_tokens(
                input=usage["input"], output=usage["output"],
                cached=usage["cached"], thinking=usage["thinking"],
                raw=raw,
            )
        except Exception:
            pass

    def _build_cursor_prompt(self, task: str) -> str:
        """Compose the initial prompt sent to Cursor Agent CLI."""
        return self._build_opencode_prompt(task)

    def _run_cursor_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        backend_env: dict[str, str] | None = None,
    ) -> str:
        """Run a Cursor Agent CLI session as the emanation backend.

        Spawns ``agent -p --force --output-format stream-json <prompt>``.
        ``-p`` puts Cursor in non-interactive print mode; ``--force`` allows
        file modifications in that mode (matching the daemon's coding-agent
        expectation); ``stream-json`` gives one JSON object per stdout line.
        The first event carrying a session-id-shaped field is stored in
        daemon.json under ``cursor_session_id`` for ``daemon(action='ask')``.
        """
        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        prompt = self._build_cursor_prompt(task)
        # Backend attribution remains ``cursor``; upstream model identity is
        # unknown until a matching source ``system/init`` + ``session_id``
        # precedes a terminal result.
        if run_dir._state.get("model") != "unknown":
            self._cursor_set_model(run_dir, "unknown")
        cmd = [
            "agent",
            "-p",
            "--force",
            "--output-format", "stream-json",
        ]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(prompt)
        self._log("daemon_cursor_start", em_id=em_id, cmd_head=" ".join(cmd[:5]))

        # Cursor normally inherits the parent environment untouched; an explicit
        # env is materialized only for a ``backend_options.env`` overlay.
        command = DaemonProcessCommand(tuple(cmd), self._workdir.path)
        if backend_env:
            env = os.environ.copy()
            env.update(backend_env)
            command = DaemonProcessCommand(
                tuple(cmd), self._workdir.path, tuple(env.items()),
            )
        try:
            handle = self._process_port.spawn(command, group_id=run_dir.group_id)
        except FileNotFoundError:
            exc = RuntimeError("'agent' Cursor CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start Cursor CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-cursor-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        session_id_captured: str | None = None
        init_models: dict[str, str] = {}
        usage_candidate: tuple[dict[str, int], dict] | None = None
        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        any_event = False

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if run_dir.set_session_id("cursor_session_id", sid, overwrite=True):
                session_id_captured = sid
                self._log("daemon_cursor_session", em_id=em_id, session_id=sid)

        try:
            for raw_line in self._process_port.iter_stdout(handle):
                if cancel_event.is_set():
                    exit_receipt = self._process_port.terminate(
                        handle, reason=("timeout" if timeout_event and timeout_event.is_set()
                                        else "reclaim"),
                    )
                    return _mark_cancelled_or_timeout(run_dir, timeout_event)

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                usage_candidate = self._cursor_process_usage_event(
                    event, run_dir, init_models, usage_candidate,
                )
                sid = self._opencode_extract_session_id(event)
                if sid:
                    _store_session_id(sid)

                text = self._opencode_extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")

                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    subtype = str(event.get("subtype") or "").lower()
                    is_error_event = bool(event.get("is_error")) or subtype == "error"
                    is_result_event = low == "result" or low.endswith(
                        (".completed", ".done", ".finished", ".result", ".final")
                    )
                    if is_result_event:
                        final_is_error = is_error_event
                        if text:
                            final_text = text

            exit_receipt = self._process_port.wait(handle)
        except Exception as e:
            reason = ("timeout" if timeout_event and timeout_event.is_set()
                      else "reclaim" if cancel_event.is_set() else "error")
            exit_receipt = self._process_port.terminate(handle, reason=reason)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            if ('exit_receipt' in locals() and exit_receipt is not None
                    and exit_receipt.returncode is not None):
                self._process_port.release(handle)

        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if exit_receipt.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            attributed = self._attributed_process_exit(
                exit_receipt, "Cursor", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"Cursor CLI exited with code {exit_receipt.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        if final_is_error:
            detail = final_text or stderr_tail or "\n".join(text_chunks[-3:])
            exc = RuntimeError(
                f"Cursor CLI reported error result: {detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        if cancel_event.is_set():
            return _mark_cancelled_or_timeout(run_dir, timeout_event)

        self._cursor_record_usage_candidate(run_dir, usage_candidate)

        if final_text is not None:
            text = final_text.strip()
        elif text_chunks:
            text = text_chunks[-1].strip()
        elif stderr_tail:
            text = f"[no JSON events; stderr tail follows]\n{stderr_tail[-500:]}"
        else:
            text = "[no output]"
        if not any_event and not stderr_tail:
            text = "[no output]"

        run_dir.mark_done(text)
        return text

    def _handle_ask_cursor(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a Cursor Agent CLI ``--resume`` follow-up off the caller's turn."""
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        # Legacy/direct run-dir callers may have initialized ``model`` to the
        # backend label; do not expose that as upstream model identity.
        if run_dir._state.get("model") == "cursor":
            self._cursor_set_model(run_dir, "unknown")

        session_id = run_dir._state.get("cursor_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No cursor session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        cmd = [
            "agent",
            "-p",
            "--force",
            "--resume", session_id,
            "--output-format", "stream-json",
            message,
        ]
        self._log("daemon_cursor_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        try:
            handle = self._process_port.spawn(
                DaemonProcessCommand(tuple(cmd), self._workdir.path),
                group_id=None,
            )
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": "'agent' Cursor CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start Cursor CLI: {e}"}
        # Ask follow-ups are not part of any batch (see claude-code ask).
        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_cursor_stream, em_id, entry, handle, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_cursor_stream(
        self,
        em_id: str,
        entry: dict,
        handle: DaemonProcessHandle,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker: stream an ``agent -p --resume`` process Port handle."""
        stderr_thread = self._process_port.drain_stderr(
            handle, on_line=lambda line: run_dir.record_cli_output(line, stream="stderr"),
            thread_name=f"daemon-cursor-ask-stderr-{em_id}",
        )
        stderr_lines = stderr_thread.lines

        init_models: dict[str, str] = {}
        usage_candidate: tuple[dict[str, int], dict] | None = None
        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        any_event = False
        timed_out = False

        try:
            deadline = time.monotonic() + self._timeout
            for raw_line in self._process_port.iter_stdout(handle, deadline=deadline):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                usage_candidate = self._cursor_process_usage_event(
                    event, run_dir, init_models, usage_candidate,
                )
                text = self._opencode_extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")
                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    subtype = str(event.get("subtype") or "").lower()
                    is_error_event = bool(event.get("is_error")) or subtype == "error"
                    is_result_event = low == "result" or low.endswith(
                        (".completed", ".done", ".finished", ".result", ".final")
                    )
                    if is_result_event:
                        final_is_error = is_error_event
                        if text:
                            final_text = text

            if time.monotonic() >= deadline:
                timed_out = True
                exit_receipt = self._process_port.terminate(handle, reason="timeout")
            else:
                try:
                    exit_receipt = self._process_port.wait(
                        handle, timeout=max(1.0, deadline - time.monotonic())
                    )
                except TimeoutError:
                    timed_out = True
                    exit_receipt = self._process_port.terminate(handle, reason="timeout")
        except Exception:
            exit_receipt = self._process_port.terminate(handle, reason="error")
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            if ('exit_receipt' in locals() and exit_receipt is not None
                    and exit_receipt.returncode is not None):
                self._process_port.release(handle)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"Cursor CLI resume timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if exit_receipt.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            attributed = self._attributed_process_exit(
                exit_receipt, "Cursor", detail[-500:], run_dir,
            )
            err = attributed or f"Cursor CLI exited {exit_receipt.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if final_is_error:
            detail = final_text or stderr_tail or "\n".join(text_chunks[-3:])
            err = f"Cursor CLI reported error result: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        self._cursor_record_usage_candidate(run_dir, usage_candidate)

        if final_text is not None:
            output = final_text.strip()
        elif text_chunks:
            output = text_chunks[-1].strip()
        else:
            output = ""

        if not any_event and not output:
            output = "[no output]"

        if output and output != "[no output]":
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}

    # ``check.last`` remains capped because its event-tail reader must protect
    # itself from an oversized JSONL tail. ``list.last`` has a bounded default
    # but accepts explicit positive overrides above that default.
    _CHECK_LAST_MAX = 1000
    _LIST_DEFAULT_LAST = _FAMILY_LIST_DEFAULT_LAST

    def _handle_check(self, em_id: str, last=20, truncate=500) -> dict:
        """Read-only progress tail for one emanation.

        Returns a snapshot of daemon.json plus the last N events from
        events.jsonl, with string fields truncated. Pure read — no
        coordination with the run thread (atomic writes + append-only JSONL
        guarantee a consistent view).
        """
        # Validate and coerce — the LLM may pass non-numeric strings;
        # reject cleanly rather than letting int() raise to the dispatcher.
        try:
            last = int(last)
        except (TypeError, ValueError):
            return {"status": "error",
                    "message": f"last must be a positive integer (got {last!r})"}
        try:
            truncate = int(truncate)
        except (TypeError, ValueError):
            return {"status": "error",
                    "message": f"truncate must be a non-negative integer (got {truncate!r})"}
        if last < 1:
            return {"status": "error", "message": f"last must be ≥ 1 (got {last})"}
        if truncate < 0:
            return {"status": "error", "message": f"truncate must be ≥ 0 (got {truncate})"}
        # Cap last to prevent self-DoS — readlines() loads the whole file
        # before slicing, so an unbounded last would read all of events.jsonl.
        last = min(last, self._CHECK_LAST_MAX)

        entry = self._emanations.get(em_id)
        if entry is not None:
            run_dir = entry.get("run_dir")
            if run_dir is None:
                return {"status": "error", "message": f"emanation {em_id} has no run_dir"}
            return self._check_snapshot_from_paths(
                em_id,
                run_path=run_dir.path,
                daemon_json_path=run_dir.daemon_json_path,
                events_path=run_dir.events_path,
                last=last,
                truncate=truncate,
            )

        # In-memory registry miss. After a refresh/molt the parent gets a fresh
        # DaemonManager whose registry is empty (__init__ does NOT reconstruct
        # entries from disk), yet the terminal notification still points at a
        # valid daemons/<run_id>/ folder. Fall back to the durable run dirs so
        # `check` by the notification's compact id resolves the historical run
        # instead of answering "Unknown emanation". Legacy short handles are
        # still accepted only when they resolve uniquely. `list` already scans
        # this history; `check` now joins it. See GH (daemon check after refresh).
        resolved = self._resolve_historical_run_dir(em_id)
        if resolved is None:
            return {"status": "error", "message": f"Unknown emanation: {em_id}"}
        run_path, matches = resolved
        snapshot = self._check_snapshot_from_paths(
            em_id,
            run_path=run_path,
            daemon_json_path=run_path / "daemon.json",
            events_path=run_path / "logs" / "events.jsonl",
            last=last,
            truncate=truncate,
        )
        if snapshot.get("status") == "error":
            return snapshot
        # Flag the disk-resolved nature so the parent can tell it apart from a
        # live registry hit. Legacy short-id ambiguity is rejected instead of
        # injecting an unbounded list of historical run directories into the
        # tool result. New compact ids exact-match their run directory names.
        if len(matches) > 1:
            matches.sort(key=lambda p: p.name)
            latest = matches[-1]
            return {
                "status": "error",
                "message": (
                    f"Ambiguous historical daemon id: {em_id} matched "
                    f"{len(matches)} run dirs; use the exact run_id instead"
                ),
                "id": em_id,
                "source": "history",
                "ambiguous": True,
                "match_count": len(matches),
                "latest_run_id": latest.name,
            }
        snapshot["source"] = "history"
        return snapshot

    def _check_snapshot_from_paths(
        self,
        em_id: str,
        *,
        run_path: Path,
        daemon_json_path: Path,
        events_path: Path,
        last: int,
        truncate: int,
    ) -> dict:
        """Build a `check` response from an on-disk run dir.

        Shared by the live-registry path and the post-refresh historical
        fallback so both surface identical daemon.json + event-tail shape.
        """
        # daemon.json — atomic-replaced, may transiently miss but never partial
        try:
            state = json.loads(daemon_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"status": "error", "message": f"daemon.json read failed: {e}"}

        # events.jsonl — append-only, missing means no events yet
        events: list[dict] = []
        events_total = 0
        if events_path.is_file():
            try:
                with open(events_path, "r", encoding="utf-8") as f:
                    raw_lines = f.readlines()
            except OSError as e:
                return {"status": "error", "message": f"events.jsonl read failed: {e}"}
            events_total = len(raw_lines)
            tail = raw_lines[-last:] if last > 0 else []
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if truncate > 0:
                    ev = {k: (v[:truncate] + "…[truncated]"
                              if isinstance(v, str) and len(v) > truncate else v)
                          for k, v in ev.items()}
                events.append(ev)

        return {
            "id": em_id,
            "run_id": state.get("run_id"),
            "state": state.get("state"),
            "backend": state.get("backend"),
            "path": str(run_path),
            "turn": state.get("turn"),
            "current_tool": state.get("current_tool"),
            "elapsed_s": state.get("elapsed_s"),
            "finished_at": state.get("finished_at"),
            "tokens": state.get("tokens", {}),
            "result_preview": state.get("result_preview"),
            "result_path": state.get("result_path"),
            "last_output": state.get("last_output"),
            "last_output_at": state.get("last_output_at"),
            "latest_checkpoint": state.get("latest_checkpoint"),
            "pending_checkpoint_messages": len(
                state.get("pending_checkpoint_messages")
                if isinstance(state.get("pending_checkpoint_messages"), list)
                else []
            ),
            "resume_generation": state.get("resume_generation"),
            "resume_state": state.get("resume_state"),
            "followup_status": state.get("followup_status"),
            "followup_result_path": state.get("followup_result_path"),
            "followup_result_preview": state.get("followup_result_preview"),
            "error": state.get("error"),
            "artifacts": self._artifacts_summary(run_path),
            "events": events,
            "events_total": events_total,
            "events_returned": len(events),
        }

    def _artifacts_summary(self, run_path: Path) -> dict:
        """Compact artifact-manifest block for a `check` response.

        Prefers the persisted ``artifacts.json`` (written at terminal time);
        for an old run that predates it — or a still-running run that has no
        manifest yet — falls back to computing one on the fly from the run dir.
        Either way only path/size/mtime/role metadata is surfaced (never file
        contents). ``source`` distinguishes the persisted manifest from a
        computed fallback so the parent knows whether it is reading a terminal
        snapshot or a live view. Never raises: a manifest is a convenience and
        must not break `check`.
        """
        manifest_path = run_path / "artifacts.json"
        source = "manifest"
        manifest = None
        try:
            if manifest_path.is_file():
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            manifest = None
        if manifest is None:
            # No persisted manifest (old run / still running). Compute a safe
            # fallback so the parent still gets a file listing.
            source = "fallback"
            try:
                manifest = DaemonRunDir.build_manifest(run_path)
            except Exception as e:  # never let a manifest break check
                return {"source": "unavailable", "error": str(e),
                        "artifacts": []}

        return {
            "source": source,
            "state": manifest.get("state"),
            "result_path": manifest.get("result_path"),
            "error_path": manifest.get("error_path"),
            "artifact_count": manifest.get("artifact_count"),
            "artifacts_total": manifest.get("artifacts_total"),
            "truncated": manifest.get("truncated", False),
            "artifacts": manifest.get("artifacts", []),
        }

    def _resolve_historical_run_dir(
        self, em_id: str
    ) -> tuple[Path, list[Path]] | None:
        """Resolve a daemon id to a durable run dir on disk.

        New daemon ids exact-match their run directory names (for example
        ``em-a1b2`` or ``em-a1b2-1``). Legacy run dirs may still carry a
        sequential handle such as ``em-5`` inside ``daemon.json``; a unique
        legacy match is accepted for post-refresh compatibility, while multiple
        legacy matches are returned so the caller can reject the ambiguous
        short id without listing every path.
        """
        em_id = (em_id or "").strip()
        if not em_id:
            return None
        daemons_dir = self._workdir.path / "daemons"
        if not daemons_dir.is_dir():
            return None

        matches: list[Path] = []
        for run_path in daemons_dir.iterdir():
            if not self._looks_like_daemon_run_dir(run_path):
                continue
            # Exact run-id (folder name) match wins unambiguously.
            if run_path.name == em_id:
                return run_path, [run_path]
            # Legacy compatibility only: otherwise match the recorded handle.
            # Prefer the persisted daemon.json handle; fall back to parsing it
            # from old long folder names so a missing/corrupt daemon.json
            # doesn't drop a real legacy match.
            handle = self._run_dir_handle(run_path)
            if handle == em_id:
                matches.append(run_path)

        if not matches:
            return None
        # Legacy folder name = handle-YYYYMMDD-HHMMSS-hash6, so lexical sort is
        # chronological for the old format. The caller rejects multi-match
        # ambiguity rather than injecting every path into the result.
        matches.sort(key=lambda p: p.name)
        return matches[-1], matches

    def _run_dir_handle(self, run_path: Path) -> str | None:
        """Best-effort handle for a run dir: daemon.json first, name second."""
        try:
            loaded = json.loads(
                (run_path / "daemon.json").read_text(encoding="utf-8")
            )
            handle = loaded.get("handle") if isinstance(loaded, dict) else None
            if isinstance(handle, str) and handle:
                return handle
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        return self._handle_from_run_id(run_path.name)

    def shutdown_for_agent_stop(
        self, reason: str = "agent_stop", wait_timeout: float = 5.0
    ) -> dict:
        """Shut down daemon-owned runtime resources during agent teardown.

        Refresh/suspend/stop must not release the parent agent heartbeat/lock
        while daemon executor workers or external CLI subprocess groups can
        still keep the old Python interpreter alive.  This lifecycle hook is
        intentionally best-effort and non-raising: callers in the agent stop
        path must continue toward teardown even if one child process is already
        gone or a pool has raced to completion.
        """
        return self._shutdown_runtime_resources(
            reason=reason, wait_timeout=wait_timeout
        )

    def _shutdown_runtime_resources(
        self, *, reason: str, wait_timeout: float = 0.0
    ) -> dict:
        futures = [
            future for e in self._emanations.values()
            if (future := e.get("future")) is not None
        ]
        ask_futures = [
            future for e in self._emanations.values()
            if (future := e.get("ask_future")) is not None
        ]
        wait_futures = futures + ask_futures
        cancelled = sum(1 for future in wait_futures if not future.done())
        errors: list[str] = []

        # Mark entries before killing child processes. CLI ask workers can wake
        # up immediately after the kill but before _emanations is cleared; the
        # parent-facing follow-up notification gate must treat that window as
        # post-reclaim too.
        for entry in self._emanations.values():
            entry["shutdown_in_progress"] = True

        # Kill all tracked CLI process groups first — this terminates child
        # shells/tools that cancel_event alone cannot reach (GH #122).
        # Snapshot under lock, kill outside to avoid holding lock during wait.
        procs_to_kill = self._drain_all_cli_procs(reason=reason)
        for proc in procs_to_kill:
            try:
                _kill_process_group(proc)
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"kill pid {getattr(proc, 'pid', '?')}: {e}")
        port_processes_killed = 0
        try:
            port_processes_killed = self._process_port.terminate_all(reason=reason)
        except Exception as e:  # pragma: no cover - defensive teardown
            errors.append(f"terminate daemon-owned processes: {e}")
        interactive_processes_killed = 0
        if self._interactive_terminal_port is not None:
            try:
                interactive_processes_killed = self._interactive_terminal_port.terminate_all(
                    reason=reason
                )
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"terminate interactive terminal children: {e}")

        pools = list(self._pools)
        self._pools.clear()
        for pool, cancel in pools:
            try:
                cancel.set()
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"cancel pool: {e}")
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"shutdown pool: {e}")

        # Tear down the dedicated CLI-ask pool too — its workers are already
        # losing their subprocesses to the kill above, but futures may still
        # be sitting in the queue. Rebuild a fresh pool so explicit reclaim
        # and stop/start reuse leave the manager in a valid state.
        try:
            self._ask_pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:  # pragma: no cover - defensive teardown
            errors.append(f"shutdown ask pool: {e}")
        self._ask_pool = ThreadPoolExecutor(
            max_workers=max(1, self._manager_pool_size),
            thread_name_prefix="daemon-cli-ask",
        )

        # During parent stop/refresh, keep heartbeat/lock alive for a bounded
        # grace period while killed CLI workers and cooperative daemon loops
        # unwind. Explicit daemon(action="reclaim") keeps the old non-blocking
        # behavior by passing wait_timeout=0.
        futures_remaining = sum(1 for future in wait_futures if not future.done())
        if wait_timeout > 0 and futures_remaining:
            try:
                wait(wait_futures, timeout=wait_timeout)
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"wait futures: {e}")
            futures_remaining = sum(
                1 for future in wait_futures if not future.done()
            )

        self._emanations.clear()

        report = {
            "status": "shutdown",
            "reason": reason,
            "cancelled": cancelled,
            "cli_processes_killed": (
                len(procs_to_kill) + port_processes_killed + interactive_processes_killed
            ),
            "interactive_terminal_processes_killed": interactive_processes_killed,
            "pools_shutdown": len(pools),
            "ask_futures_shutdown": len(ask_futures),
            "futures_remaining": futures_remaining,
            "errors": errors,
        }
        self._log("daemon_lifecycle_shutdown", **report)
        return report

    # Bounded wait for a detached run's daemon.json to reach a terminal state
    # after an explicit reclaim control request is submitted. Kept short —
    # `reclaim` must stay responsive; a run that hasn't confirmed cancellation
    # within this window is reported honestly as "requested" rather than a
    # false "cancelled" claim.
    _DETACHED_RECLAIM_CONFIRM_TIMEOUT_S = 3.0
    _DETACHED_RECLAIM_CONFIRM_POLL_S = 0.1

    def _reclaim_detached_runs(self) -> tuple[int, int]:
        """Submit a reclaim control request to every active detached run.

        Returns ``(confirmed_cancelled, requested_not_yet_confirmed)``. This
        is the explicit-reclaim-only path for detached runs — ordinary
        ``shutdown_for_agent_stop`` (agent_stop/refresh) must never call this,
        per the contract's "ordinary stop/refresh must not terminate active
        supervisors" requirement. Only genuinely detached, still-running
        entries are touched; a run whose daemon.json is already terminal is
        left alone (nothing to cancel, and re-submitting a reclaim request to
        an exited supervisor would just sit unconsumed in its control spool).
        """
        from lingtai.kernel.daemon_supervisor import control

        run_dirs = {
            entry["run_dir"].path: entry["run_dir"]
            for entry in self._emanations.values()
            if entry.get("detached") and entry.get("run_dir") is not None
        }
        # A fresh manager has no in-memory registry. Discover only exact
        # control-capable active detached records — supervisor-owned or
        # central-manager-owned, same durable owner set as the ask/entry
        # facades (`_durable_detached_entry`/`_handle_ask_detached`); this is
        # a control facade, not an execution-owner adoption path.
        skipped_dead_manager = 0
        daemons_dir = self._workdir.path / "daemons"
        if daemons_dir.is_dir():
            for run_path in daemons_dir.iterdir():
                if not self._looks_like_daemon_run_dir(run_path):
                    continue
                try:
                    state = DaemonRunDir.read_state_from_disk(run_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if state.get("state") not in {"running", "active"}:
                    continue
                owner = state.get("owner")
                if owner not in {"supervisor", "manager"}:
                    continue
                pid = state.get("supervisor_pid")
                if isinstance(pid, int) and not isinstance(pid, bool):
                    # Active detached record: both owners stamp the exact
                    # supervisor_pid/start identity at execution start, so one
                    # unchanged guard covers supervisor- and manager-owned runs.
                    if not self._pid_identity_matches(
                        pid, state.get("supervisor_start_identity")
                    ):
                        continue
                elif owner == "manager":
                    # Queued central-manager record (no execution worker yet):
                    # eligible only behind a live, identity-matched manager
                    # whose queue loop consumes the reclaim before start. A
                    # dead or mismatched manager gets no request and no
                    # signal; the startup reaper owns terminalizing its
                    # records.
                    if not self._manager_owner_alive(state):
                        skipped_dead_manager += 1
                        continue
                else:
                    continue
                try:
                    run_dirs.setdefault(run_path, DaemonRunDir.attach(run_path))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        self._last_reclaim_skipped_dead_manager = skipped_dead_manager
        if not run_dirs:
            return 0, 0

        pending: list = []
        for run_dir in run_dirs.values():
            state = self._read_run_dir_state_from_disk(run_dir)
            if state.get("state") not in ("running", "active"):
                continue
            control.submit_request(run_dir.path, "reclaim", {})
            pending.append(run_dir)

        if not pending:
            return 0, 0

        deadline = time.monotonic() + self._DETACHED_RECLAIM_CONFIRM_TIMEOUT_S
        confirmed = 0
        natural_terminal = 0
        remaining = list(pending)
        while remaining and time.monotonic() < deadline:
            still_pending = []
            for run_dir in remaining:
                terminal_state = self._read_run_dir_state_from_disk(run_dir).get("state")
                if terminal_state == "cancelled":
                    confirmed += 1
                elif terminal_state in {"timeout", "done", "failed"}:
                    # A natural terminal outcome is evidence, not a
                    # cancellation confirmation and must not inflate the count.
                    natural_terminal += 1
                else:
                    still_pending.append(run_dir)
            remaining = still_pending
            if remaining:
                time.sleep(self._DETACHED_RECLAIM_CONFIRM_POLL_S)
        self._last_reclaim_natural_terminal = natural_terminal
        return confirmed, len(remaining)

    def _handle_reclaim(self) -> dict:
        detached_confirmed, detached_pending = self._reclaim_detached_runs()
        report = self.shutdown_for_agent_stop(reason="reclaim", wait_timeout=0.0)
        cancelled = report.get("cancelled", 0) + detached_confirmed
        self._log(
            "daemon_reclaim", cancelled_count=cancelled,
            detached_confirmed=detached_confirmed, detached_pending=detached_pending,
            skipped_dead_manager=getattr(
                self, "_last_reclaim_skipped_dead_manager", 0),
        )
        result = {
            "status": "reclaimed",
            "cancelled": cancelled,
            "natural_terminal": getattr(self, "_last_reclaim_natural_terminal", 0),
        }
        if detached_pending:
            # Never overstate reclaim: a detached run that hasn't confirmed
            # cancellation within the bounded wait is reported explicitly
            # rather than folded silently into "cancelled".
            result["detached_reclaim_pending"] = detached_pending
        return result

    # ------------------------------------------------------------------
    # CLI process-group tracking helpers
    #
    # Every external-CLI backend registers its Popen here on spawn and
    # unregisters it on exit, instead of poking _cli_procs directly. Ownership
    # metadata (the batch ``group_id``) lets a batch's timeout watchdog kill
    # only its own subprocesses (_kill_cli_group), while reclaim-all still
    # drains everything (_drain_all_cli_procs).
    # ------------------------------------------------------------------

    def _cli_start_new_session(self) -> bool:
        """Keep legacy manager-owned CLI children in isolated process groups.

        Detached execution hosts override this so a CLI belongs to the already
        isolated execution-child group from birth, including before its PID is
        durably registered with the supervisor.
        """
        return True

    def _register_cli_proc(self, proc: subprocess.Popen,
                           group_id: str | None = None) -> None:
        """Track *proc* globally and (if batched) under its ``group_id``."""
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and not isinstance(pid, bool):
            try:
                pgid = os.getpgid(pid)
            except (ProcessLookupError, PermissionError, OSError):
                pgid = None
            identity = process_identity(pid)
            # The cleanup helper refuses to signal unless all three values
            # were captured.  In particular, never silently turn an unknown
            # identity into a PID-only authorization.
            proc._lingtai_pgid = pgid
            proc._lingtai_process_identity = identity
            proc._lingtai_termination_scope = (
                DaemonProcessTerminationScope.PRIVATE_PROCESS_GROUP
                if self._cli_start_new_session()
                else DaemonProcessTerminationScope.INHERITED_SUPERVISOR_GROUP
            )
        with self._cli_lock:
            self._cli_procs.append(proc)
            if group_id is not None:
                self._cli_proc_groups.setdefault(group_id, set()).add(proc)
            # CPython may recycle a previous proc's id() for this fresh object.
            # Drop any stale termination reason left under that id (e.g. a kill
            # stamped on a proc that then exited 0 before SIGTERM landed) so it
            # cannot be mis-attributed to this new subprocess. See GH #455.
            self._cli_term_reasons.pop(id(proc), None)

    def _unregister_cli_proc(self, proc: subprocess.Popen,
                             group_id: str | None = None) -> None:
        """Detach *proc* from global and group tracking. Idempotent.

        The recorded termination reason (if any) is intentionally NOT cleared
        here: ``_unregister_cli_proc`` runs in the read-loop's ``finally`` block,
        immediately before the returncode is classified, so the reason must
        survive until ``_take_cli_term_reason`` consumes it.
        """
        with self._cli_lock:
            try:
                self._cli_procs.remove(proc)
            except ValueError:
                pass  # already removed by reclaim/watchdog
            if group_id is not None:
                bucket = self._cli_proc_groups.get(group_id)
                if bucket is not None:
                    bucket.discard(proc)
                    if not bucket:
                        del self._cli_proc_groups[group_id]

    def _note_cli_term_reason(self, proc: subprocess.Popen, reason: str) -> None:
        """Record the LingTai-initiated termination *reason* for *proc*.

        Called at the out-of-loop kill sites (reclaim/agent_stop/refresh and
        batch timeout) just before SIGTERM. First reason wins so a follow-up
        teardown kill cannot overwrite the original causal reason (e.g. a
        timeout that is then swept by reclaim stays "timeout"). See GH #455.
        """
        with self._cli_lock:
            self._cli_term_reasons.setdefault(id(proc), reason)

    def _take_cli_term_reason(self, proc: subprocess.Popen) -> str | None:
        """Pop and return the recorded termination reason for *proc*, if any."""
        with self._cli_lock:
            return self._cli_term_reasons.pop(id(proc), None)

    def _kill_cli_group(self, group_id: str, reason: str = "timeout") -> None:
        """Kill only the CLI process groups owned by *group_id*.

        Snapshots the group's procs under the lock, detaches them from both
        the group index and the global list, then kills outside the lock so we
        never hold ``_cli_lock`` across a multi-second ``proc.wait``. Procs from
        other batches (and ungrouped ``ask`` procs) are left untouched.

        *reason* (default "timeout", the only current caller) is stamped on each
        proc before SIGTERM so the read loop can attribute the signal exit.
        """
        with self._cli_lock:
            bucket = self._cli_proc_groups.pop(group_id, None)
            procs_to_kill = list(bucket) if bucket else []
            for proc in procs_to_kill:
                self._cli_term_reasons.setdefault(id(proc), reason)
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass
        for proc in procs_to_kill:
            _kill_process_group(proc)
        self._process_port.terminate_group(group_id, reason=reason)
        if self._interactive_terminal_port is not None:
            self._interactive_terminal_port.terminate_group(group_id, reason=reason)

    def _drain_all_cli_procs(self, reason: str | None = None) -> list[subprocess.Popen]:
        """Clear all CLI tracking and return the procs to kill (reclaim path).

        When *reason* is given (agent_stop / parent refresh / reclaim) it is
        stamped on each drained proc before the caller sends SIGTERM, so the
        read loop attributes the resulting -15/143 exit to that local cause
        instead of reporting an opaque CLI failure (GH #455).
        """
        with self._cli_lock:
            procs_to_kill = list(self._cli_procs)
            if reason is not None:
                for proc in procs_to_kill:
                    self._cli_term_reasons.setdefault(id(proc), reason)
            self._cli_procs.clear()
            self._cli_proc_groups.clear()
        return procs_to_kill

    def _attributed_process_exit(
        self, receipt: DaemonProcessExit, backend_name: str, detail: str,
        run_dir: "DaemonRunDir | None" = None,
    ) -> str | None:
        """Apply raw-code/local-cause attribution to a Port receipt."""
        signal_name = self._signal_exit_name(receipt.returncode)
        if signal_name is None or receipt.reason is None:
            return None
        if run_dir is not None:
            try:
                run_dir.record_cli_termination(
                    reason=receipt.reason, signal_name=signal_name,
                    returncode=receipt.returncode,
                )
            except Exception:
                pass
        msg = (
            f"{backend_name} CLI terminated by LingTai ({receipt.reason}, "
            f"{signal_name}, code {receipt.returncode})"
        )
        return f"{msg}: {detail}" if detail else msg

    @staticmethod
    def _signal_exit_name(returncode: int | None) -> str | None:
        """Map a Popen returncode to a signal name, or None if not a signal.

        Covers both subprocess conventions: negative (``-15``) when Python
        reaps the child directly, and ``128 + signum`` (``143``) when the exit
        propagates through a shell.
        """
        if returncode in (-15, 143):
            return "SIGTERM"
        if returncode in (-9, 137):
            return "SIGKILL"
        return None

    def _attributed_cli_exit(
        self,
        proc: subprocess.Popen,
        backend_name: str,
        detail: str,
        run_dir: "DaemonRunDir | None" = None,
    ) -> str | None:
        """Attribute a signal-terminated CLI exit to its local cause.

        Returns a human-readable message naming the LingTai-initiated reason
        (e.g. ``agent_stop`` / ``reclaim`` / ``timeout``) when this manager
        recorded one before sending the signal, and records the reason on
        *run_dir* for forensic inspection. The raw exit code is always kept in
        the message. Returns ``None`` when the exit is not a signal we attribute
        or no local reason was recorded — the caller then keeps its existing
        opaque message so external/unknown SIGTERMs are not mislabeled as
        deliberate cancellations. See GH #455.
        """
        reason = self._take_cli_term_reason(proc)
        signal_name = self._signal_exit_name(getattr(proc, "returncode", None))
        if signal_name is None or reason is None:
            return None
        if run_dir is not None:
            try:
                run_dir.record_cli_termination(
                    reason=reason,
                    signal_name=signal_name,
                    returncode=proc.returncode,
                )
            except Exception:
                pass
        msg = (
            f"{backend_name} CLI terminated by LingTai ({reason}, "
            f"{signal_name}, code {proc.returncode})"
        )
        return f"{msg}: {detail}" if detail else msg

    def _new_emanation_id(self, *, reserved_ids: set[str] | None = None) -> str:
        """Return a compact, collision-safe daemon id for a new run.

        Older daemon handles were sequential (``em-1``) while run directories
        carried a longer timestamp/hash suffix. That split made short ids
        ambiguous after refreshes. New runs use one compact id everywhere:
        the user-facing id, daemon.json handle, and run directory name.
        """
        reserved_ids = reserved_ids or set()
        daemons_dir = self._workdir.path / "daemons"
        now_ns = time.time_ns()
        digest = ((now_ns >> 16) ^ now_ns) & 0xFFFF
        base = f"em-{digest:04x}"
        candidate = base
        suffix = 1
        while (
            candidate in reserved_ids
            or candidate in self._emanations
            or (daemons_dir / candidate).exists()
        ):
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _log(self, event_type: str, **fields) -> None:
        """Log through Daemon's narrow parent-runtime port."""
        self._runtime.log(event_type, **fields)

    @staticmethod
    def _admission_error_result(error: "DerivedLaunchAdmissionError") -> dict[str, str]:
        """Expose structured refusal evidence without creating a run artifact."""
        return {
            "status": "error",
            "message": str(error),
            "reason_code": error.decision.reason_code,
            "audit_id": error.decision.audit_id,
        }

    @staticmethod
    def _close_unconsumed_derived_launch_decisions(
        decisions: list["DerivedLaunchDecision"],
    ) -> None:
        """Release pre-authorized decisions that have not reached a child yet.

        A later denial or unavailable handoff occurs before any child can
        consume earlier Driver grants.  Close every known lease in that batch
        before exposing the refusal; generic Core decisions carry ``None``.
        """
        for decision in decisions:
            lease = decision.child_endpoint_lease
            if lease is not None:
                try:
                    lease.close()
                except OSError:
                    pass

    @staticmethod
    def _consume_driver_authority_lease_for_posix_handoff(lease) -> int | None:
        """Transfer one opaque Driver lease into B8a's POSIX FD capsule.

        Daemon Core never interprets the lease. This outer composition method
        is the sole daemon-side bridge to the POSIX transport; Windows must
        release and reject rather than silently launch without authority.
        """
        if lease is None:
            return None
        if os.name != "posix":
            try:
                lease.close()
            except OSError:
                pass
            raise RuntimeError(
                "Driver child-endpoint handoff requires POSIX SCM_RIGHTS"
            )
        from lingtai.adapters.acp.driver_authority import (
            consume_posix_child_endpoint_lease,
        )

        return consume_posix_child_endpoint_lease(lease)

    @staticmethod
    def _has_unhandoffable_driver_leases(
        decisions: list["DerivedLaunchDecision"],
    ) -> bool:
        """Detect Driver grants before their lease can reach durable state.

        B5's central manager transports capsules as JSON, so it cannot carry a
        live child endpoint.  B8 will add the SCM_RIGHTS transport and its
        restart invalidation rule.  Until then, a valid Driver grant fails
        closed rather than being queued without its authority endpoint.
        """
        return any(decision.child_endpoint_lease is not None for decision in decisions)

    @staticmethod
    def _driver_handoff_unavailable_result() -> dict[str, str | None]:
        return {
            "status": "error",
            "message": "Driver child-endpoint handoff is unavailable",
            "reason_code": "driver_child_endpoint_handoff_unavailable",
            "audit_id": None,
        }

    def _authorize_derived_launch_batch(
        self, capability_name: str, task_count: int
    ) -> list["DerivedLaunchDecision"]:
        """Authorize every child in a batch before any durable batch write."""
        decisions = []
        try:
            for _ in range(task_count):
                decisions.append(self._authorize_derived_launch(capability_name))
        except Exception:
            self._close_unconsumed_derived_launch_decisions(decisions)
            raise
        return decisions

    def _authorize_derived_launch(
        self, capability_name: str
    ) -> "DerivedLaunchDecision":
        """Reach the host decision seam before a daemon launch side effect."""
        from lingtai.kernel.provider_admission import (
            DerivedLaunchAdmissionError,
            DerivedLaunchCapability,
            DerivedLaunchDecision,
        )

        capability = DerivedLaunchCapability(capability_name)
        decision: DerivedLaunchDecision | None = None
        transferred = False
        try:
            try:
                decision = self._runtime.authorize_derived_launch(capability)
            except DerivedLaunchAdmissionError as exc:
                decision = exc.decision
                self._log(
                    "derived_launch_admission_decision",
                    capability=capability.value,
                    state=decision.state.value,
                    reason_code=decision.reason_code,
                    audit_id=decision.audit_id,
                )
                raise
            self._log(
                "derived_launch_admission_decision",
                capability=capability.value,
                state=decision.state.value,
                reason_code=decision.reason_code,
                audit_id=decision.audit_id,
            )
            if not decision.allowed:
                raise DerivedLaunchAdmissionError(decision)
            transferred = True
            return decision
        finally:
            if decision is not None and not transferred:
                self._close_unconsumed_derived_launch_decisions([decision])


# Pair of the ``DEFAULT_MAX_TURNS`` assertion above: ``_tool_family``'s
# ``check`` child schema advertises the same event-tail ceiling the engine
# enforces, while its list default matches the manager's bounded default.
assert _FAMILY_CHECK_LAST_MAX == DaemonManager._CHECK_LAST_MAX
assert _FAMILY_LIST_DEFAULT_LAST == DaemonManager._LIST_DEFAULT_LAST


def setup(agent: "Agent",
          max_turns: int | None = None, timeout: float = 3600.0,
          notify_threshold: int = 20,
          manager_pool_size: int | None = None,
          system_prompt_budget_chars: int | None = None,
          process_port: DaemonProcessPort | None = None,
          interactive_terminal_port: InteractiveTerminalPort | None = None) -> DaemonManager:
    """Set up Daemon through its official declared-host plugin route.

    The per-agent daemon configuration and explicit capability kwargs resolve
    exactly as before.  A live ``BaseAgent`` receives only the static
    :data:`DECLARATION`; the production adapter supplies Daemon's narrow runtime
    port and the kernel registrar owns activation/mounting.  The lightweight
    non-Agent fallback retains the historical direct setup seam used by isolated
    engine tests and does not participate in an official Agent tool surface.
    """
    config = _load_config(agent._working_dir)
    if max_turns is None:
        max_turns = config.max_turns
    if manager_pool_size is None:
        manager_pool_size = config.manager_pool_size
    system_prompt_budget_chars = _config_positive_int(
        system_prompt_budget_chars, config.system_prompt_budget_chars
    )
    if process_port is None:
        if os.name == "posix":
            process_port = PosixDaemonProcessPort()
        elif os.name == "nt":
            from .windows_process import WindowsDaemonProcessPort
            process_port = WindowsDaemonProcessPort()
        else:
            raise NotImplementedError(
                f"daemon process supervision is unsupported on {os.name!r}"
            )
    if interactive_terminal_port is None and os.name == "posix":
        from lingtai.adapters.posix.interactive_terminal import (
            PosixInteractiveTerminalAdapter,
        )
        interactive_terminal_port = PosixInteractiveTerminalAdapter()

    options = {
        "max_turns": max_turns,
        "timeout": timeout,
        "notify_threshold": notify_threshold,
        "manager_pool_size": manager_pool_size,
        "system_prompt_budget_chars": system_prompt_budget_chars,
        "process_port": process_port,
        "interactive_terminal_port": interactive_terminal_port,
    }

    from lingtai.kernel.base_agent import BaseAgent
    if isinstance(agent, BaseAgent):
        from lingtai.adapters.tool_plugin_host import (
            daemon_runtime_for_agent,
            register_agent_tool_plugins,
        )

        runtime = daemon_runtime_for_agent(agent, options)
        register_agent_tool_plugins(
            agent,
            [DECLARATION],
            extra_ports_for=lambda declaration: (
                {"daemon_runtime": runtime}
                if declaration is DECLARATION else {}
            ),
        )
        manager = runtime.daemon_manager
        if not isinstance(manager, DaemonManager):
            raise RuntimeError("daemon official plugin binding did not retain its manager")
        return manager

    # Compatibility for pre-existing direct test/in-process facades.  Real
    # Agent boot always takes the declared path above, where generic add_tool
    # cannot claim the kernel-reserved ``daemon`` name.
    manager = DaemonManager(
        agent,
        max_turns=max_turns,
        timeout=timeout,
        notify_threshold=notify_threshold,
        manager_pool_size=manager_pool_size,
        system_prompt_budget_chars=system_prompt_budget_chars,
        process_port=process_port,
        interactive_terminal_port=interactive_terminal_port,
    )
    dispatcher = DaemonFamilyDispatcher(
        manager, agent, list(_BACKEND_SCHEMA_ENUM), declaration=DECLARATION,
    )
    agent.add_tool(
        DECLARATION.name,
        schema=get_schema(),
        handler=dispatcher.handle,
        description=get_description(),
        glossary_package=__package__,
    )
    return manager
