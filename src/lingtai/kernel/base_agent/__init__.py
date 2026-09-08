"""
BaseAgent — generic agent kernel with intrinsic tools and capability dispatch.

Key concepts:
    - **5-state lifecycle**: ACTIVE, IDLE, STUCK, ASLEEP, SUSPENDED.
    - **Persistent LLM session**: each agent keeps its chat session across messages.
    - **2-layer tool dispatch**: intrinsics (built-in) + capability handlers.
    - **Opaque context**: the host app can pass any context object — the agent
      stores it but never introspects it.
    - **4 optional services**: LLM, FileIO, Mail, Event Journal —
      missing service auto-disables the intrinsics it backs.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import json
import queue
import threading
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..config import AgentConfig
from ..event_journal import EventJournalPort
from ..state import AgentState
from ..workdir import WorkingDir
from ..workdir_lease import WorkdirLeasePort
from ..notification_store import NotificationStorePort
from ..agent_presence import AgentPresenceStorePort
from ..lifecycle_clock import LifecycleClockPort
from ..refresh_watcher import RefreshWatcherPort
from ..snapshot import SnapshotPort, SourceRevisionPort
from ..stream_progress import StreamProgressPort
from ..message import Message
from ..prompt import SystemPromptManager
from ..llm import (
    FunctionSchema,
    LLMService,
    ToolCall,
)
from ..logging import get_logger
from ..meta_block import (
    TOOL_META_CONTEXT_EVENT_PENDING_KEY,
    TOOL_META_CONTEXT_PENDING_KEY,
    build_meta,
    build_synthetic_meta_envelope,
    build_notification_payload,
    build_notification_persistent_payload,
    record_notification_persistent_delivery,
    sanitize_email_notification_after_persistent,
    sanitize_feishu_notification_after_persistent,
    sanitize_telegram_notification_after_persistent,
    sanitize_whatsapp_notification_after_persistent,
    sanitize_wechat_notification_after_persistent,
)
from ..session import SessionManager
from ..tc_inbox import TCInbox
from ..token_ledger import append_token_entry
from .._fsutil import atomic_write_json, atomic_write_text
from ..trace_redaction import redact_for_trajectory
from ..runtime_identity import runtime_identity_event_fields
from ..execution_workspace import ExecutionWorkspace
from ..turn_events import TurnToolObserver
from ..turn_permissions import TurnPermissionBroker
from ..turns import TurnHandle, TurnOrigin
from .lifecycle import StopResult, StopStatus

logger = get_logger()

# Retained legacy literal for the retired kernel-driven Telegram Task Card
# reverse channel. The current public ``task_card`` capability is intrinsic in
# ``lingtai.tools.task_card``; Telegram only projects its artifact read-only.
# Keep this only while legacy cleanup paths still reference the historical name.
_TASK_CARD_TOOL = "_lingtai_telegram_task_card"


def _notification_source_signatures(payloads: Mapping[str, object]) -> dict[str, str]:
    """Return bounded deterministic signatures for an observed channel snapshot."""
    signatures: dict[str, str] = {}
    for source, payload in payloads.items():
        try:
            material = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            material = repr(payload).encode("utf-8", "replace")
        signatures[str(source)] = hashlib.sha256(material).hexdigest()
    return signatures


# Typed daemon event kinds and their durable idempotency-key prefixes. A run's
# mini-channel multiplexes checkpoints, follow-up (`ask`) results, and the one
# terminal outcome; only the terminal outcome retires the run.
_DAEMON_TERMINAL_KIND = "daemon_terminal"
_DAEMON_FOLLOWUP_KIND = "daemon_followup"
_DAEMON_TERMINAL_KEY_PREFIX = "daemon-terminal:"
_DAEMON_FOLLOWUP_KEY_PREFIX = "daemon-followup:"
_DAEMON_FOLLOWUP_STATUS_PREFIX = "follow-up"


def _is_terminal_daemon_event(event: Mapping) -> bool:
    """Return True iff *event* is a run's terminal outcome.

    Follow-up (`ask`) results are explicitly not terminal: the run stays active
    and can be asked again. They are recognised by their typed kind first, then
    by the two legacy shapes that already exist on disk — a detached follow-up's
    ``daemon-followup:`` idempotency key, and an in-process follow-up which
    carried no key at all and only its ``follow-up ...`` status.
    """
    kind = event.get("kind")
    key = event.get("idempotency_key")
    key = key if isinstance(key, str) else ""
    status = event.get("status")
    status = status if isinstance(status, str) else ""
    if (
        kind == _DAEMON_FOLLOWUP_KIND
        or key.startswith(_DAEMON_FOLLOWUP_KEY_PREFIX)
        or status.startswith(_DAEMON_FOLLOWUP_STATUS_PREFIX)
    ):
        return False
    return kind == _DAEMON_TERMINAL_KIND or key.startswith(_DAEMON_TERMINAL_KEY_PREFIX)


def _daemon_notification_summary(payload: object) -> dict | None:
    """Summarize the aggregate daemon mini-channel payload without raw events."""
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    events = data.get("events") if isinstance(data, Mapping) else None
    if not isinstance(events, list):
        return None

    runs: set[str] = set()
    terminal_runs: set[str] = set()
    terminal_by_status: dict[str, int] = {}
    latest_terminal: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        run_id = event.get("ref_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        runs.add(run_id)
        if not _is_terminal_daemon_event(event):
            continue
        terminal_runs.add(run_id)
        status = event.get("status")
        if not isinstance(status, str) or not status:
            status = "unknown"
        terminal_by_status[status] = terminal_by_status.get(status, 0) + 1
        at = event.get("at")
        latest_terminal.append(
            {"run_id": run_id, "status": status, "at": at if isinstance(at, str) else ""}
        )

    latest_terminal.sort(key=lambda item: (item["at"], item["run_id"]))
    return {
        "run_count": len(runs),
        "event_count": len(events),
        "active_run_count": len(runs - terminal_runs),
        "terminal_run_count": len(terminal_runs),
        "terminal_by_status": dict(sorted(terminal_by_status.items())),
        "latest_terminal": latest_terminal[-3:],
    }


_DAEMON_DELTA_FIELDS = ("event_count", "run_count", "terminal_run_count")


def _daemon_summary_count(source: object, field: str) -> int:
    """Return one non-negative count from a daemon summary, defaulting to 0."""
    value = source.get(field) if isinstance(source, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _daemon_summary_delta(previous: object, current: Mapping) -> dict:
    """Return the daemon wake deltas between two bounded summaries.

    A delta answers "how much of this is new since the model last saw the
    channel". Dismissal, a whole-channel clear, and a batch reset all shrink the
    aggregate below the delivered baseline, and subtracting a larger baseline
    reported a negative delta — a count that cannot exist. A shrunk aggregate is
    not a smaller change, it is a *different* batch: the delivered baseline is
    dropped, every remaining count is reported as new, and ``baseline_reset``
    names the reason. No extra durable state is needed — the shrink itself is
    the evidence.
    """
    reset = any(
        _daemon_summary_count(previous, field) > _daemon_summary_count(current, field)
        for field in _DAEMON_DELTA_FIELDS
    )
    delta: dict[str, object] = {}
    for field in _DAEMON_DELTA_FIELDS:
        baseline = 0 if reset else _daemon_summary_count(previous, field)
        delta[f"{field}_delta"] = _daemon_summary_count(current, field) - baseline
    delta["latest_terminal"] = current.get("latest_terminal")
    if reset:
        delta["baseline_reset"] = True
    return delta


def _block_type_name(block: object) -> str:
    """Return a compact, safe block type label for diagnostics."""
    try:
        data = block.to_dict()  # type: ignore[attr-defined]
        btype = data.get("type") if isinstance(data, dict) else None
        if isinstance(btype, str) and btype:
            return btype
    except Exception:
        pass
    name = type(block).__name__
    if name.endswith("Block"):
        name = name[:-5]
    return name[:80]


def _pending_tool_call_diagnostics(iface, *, tail_limit: int = 3) -> dict:
    """Bounded, argument-free diagnostics for a pending tool-call tail."""
    entries = list(getattr(iface, "entries", None) or [])
    tail_entries = entries[-tail_limit:]
    tail = entries[-1] if entries else None
    pending_calls = []
    if getattr(tail, "role", None) == "assistant":
        pending_calls = [
            block
            for block in getattr(tail, "content", []) or []
            if hasattr(block, "id") and hasattr(block, "name") and hasattr(block, "args")
        ]

    return {
        "pending_tool_call_count": len(pending_calls),
        "pending_tool_call_ids": [getattr(call, "id", None) for call in pending_calls],
        "pending_tool_names": [getattr(call, "name", None) for call in pending_calls],
        "pending_tail_roles": [getattr(entry, "role", None) for entry in tail_entries],
        "pending_tail_block_types": [
            [_block_type_name(block) for block in (getattr(entry, "content", []) or [])]
            for entry in tail_entries
        ],
    }


# Issue #164 — event types that count as "the agent made forward
# progress." Bumping ``_last_progress_at`` on these gives the ACTIVE-
# without-progress watchdog a single, robust signal that survives
# refactors of individual call sites: every progress event already calls
# ``_log()``. Each entry's value is the active-turn ``kind`` to record
# (``None`` means "leave kind alone").
_PROGRESS_EVENTS: dict[str, str | None] = {
    "wake": "wake",
    "tc_wake_continue": "wake",
    "llm_call": "llm_call",
    "llm_response": None,  # progress, but turn kind stays "llm_call"
    "tool_call": "tool_call",
    "tool_result": None,
    "notification_pair_injected": "notification_injection",
    "turn_cancelled_post_tool": None,
}


# ---------------------------------------------------------------------------
# Identity prompt section (curated prose)
# ---------------------------------------------------------------------------



def _build_identity_section(manifest_data: dict, mailbox_name: str | None = None) -> str:
    """Render the agent's identity as curated prose for the system prompt.

    Stable across turns (no transient runtime state) so it sits in the
    cacheable prefix without invalidating cache. The `state` field is
    explicitly omitted upstream — it changes every turn.

    Returns a markdown paragraph. Empty/missing fields are silently
    omitted so the prose stays clean for minimal manifests.
    """
    name = manifest_data.get("agent_name") or "(unnamed)"
    nickname = manifest_data.get("nickname") or ""
    agent_id = manifest_data.get("agent_id") or ""
    address = manifest_data.get("address") or ""
    created = manifest_data.get("created_at") or ""
    admin = manifest_data.get("admin") or {}
    soul_delay = manifest_data.get("soul_delay")
    molt_count = manifest_data.get("molt_count", 0)

    lines: list[str] = []

    # Lead — name, nickname, id, address.
    lead = f"You are **{name}**"
    if nickname:
        lead += f" — \"{nickname}\""
    if agent_id:
        lead += f" (id `{agent_id}`)"
    lead += "."
    lines.append(lead)
    if address:
        lines.append(f"Your address is `{address}`.")

    # Origin — birth only. `started_at` (session start) is deliberately
    # excluded: it changes on every process restart, including a plain
    # refresh with no source/config change, and would otherwise invalidate
    # the prompt-cache prefix this section is designed to stay stable in.
    if created:
        lines.append(f"You were born {created}.")
    if molt_count > 0:
        lines.append(
            f"You have undergone {molt_count} molt"
            f"{'s' if molt_count != 1 else ''} since birth."
        )

    # Admin role.
    if admin:
        flags = [k for k, v in admin.items() if v]
        if flags:
            if "nirvana" in flags:
                lines.append(
                    "You hold both **karma** and **nirvana** privileges — "
                    "you can manage and destroy other agents in this network."
                )
            elif "karma" in flags:
                lines.append(
                    "You hold **karma** privilege — "
                    "you can lull / suspend / cpr / clear other agents."
                )
            else:
                lines.append(f"You hold admin flags: {', '.join(flags)}.")

    # Resources.
    if soul_delay is not None:
        lines.append(f"Your soul flow fires {soul_delay}s after you go idle.")
    if mailbox_name:
        lines.append(f"You receive messages via {mailbox_name}.")

    # Runtime LLM identity — provider/model/endpoint as the agent runs.
    # Sourced from `manifest_data["llm"]` (sanitized at build time —
    # see identity.py `_safe_llm_from_service` and wrapper `Agent._build_manifest`).
    # Rendered as a single line so it sits in the cacheable prefix without
    # adding much weight; missing fields are silently skipped.
    llm = manifest_data.get("llm") or {}
    if isinstance(llm, dict):
        model = _identity_scalar(llm.get("model"))
        provider = _identity_scalar(llm.get("provider"))
        base_url = _identity_scalar(llm.get("base_url"))
        if provider or model:
            bits = []
            if model:
                bits.append(f"model `{model}`")
            if provider:
                bits.append(f"provider `{provider}`")
            if base_url:
                bits.append(f"endpoint `{base_url}`")
            if bits:
                lines.append("You are running on " + ", ".join(bits) + ".")

    # Active preset — only the wrapper agent has a preset surface, so this
    # block is silent for bare BaseAgent instances. Reports the active path
    # plus the default if the two differ (lets the agent see when it's on a
    # non-default preset). Allowed list is intentionally omitted from the
    # prompt — it's structural metadata, not identity prose.
    preset = manifest_data.get("preset") or {}
    if isinstance(preset, dict):
        active = _identity_scalar(preset.get("active"))
        default = _identity_scalar(preset.get("default"))
        if active:
            if default and default != active:
                lines.append(
                    f"Your active preset is `{active}` "
                    f"(default `{default}`)."
                )
            else:
                lines.append(f"Your active preset is `{active}`.")

    return "\n".join(lines)


def _identity_scalar(value) -> str:
    """Return prompt-safe scalar text for identity metadata, else empty string."""
    if isinstance(value, str):
        return value if value else ""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


def _release_acquired_workdir_lease_on_init_failure(initializer: Callable) -> Callable:
    """Roll back a successfully acquired lease without hiding the boot error."""

    @functools.wraps(initializer)
    def guarded(self, *args, **kwargs):
        try:
            return initializer(self, *args, **kwargs)
        except BaseException:
            if getattr(self, "_workdir_lease_acquired", False):
                with contextlib.suppress(Exception):
                    self._workdir_lease.release()
                self._workdir_lease_acquired = False
            raise

    return guarded


class BaseAgent:
    """Generic research agent with intrinsic tools and MCP tool dispatch.

    Required dependencies:
        - ``workdir_lease`` (WorkdirLeasePort): Exclusive claim on the working
          directory, acquired at construction and released at teardown. It has no
          unlocked/no-op form — omitting it fails loudly at construction.
        - ``notification_store`` (NotificationStorePort): Persistence for
          ``.notification/`` channel mirrors. Required on every supported
          agent; there is no nullable/no-op path.
        - ``agent_presence`` (AgentPresenceStorePort): Own-heartbeat publish/
          withdraw and foreign-address presence observation, bound to this
          agent's working directory. Required and explicit; there is no
          nullable/no-op path and Core never constructs the concrete adapter.
        - ``lifecycle_clock`` (LifecycleClockPort): The two lifecycle time
          sources — wall-clock seconds for persisted/cross-process timestamps
          and ages, monotonic seconds for process-local elapsed intervals.
          Required and explicit; there is no default/no-op/optional path and
          Core never constructs the concrete adapter.
        - ``snapshot_port`` (SnapshotPort): Best-effort workdir initialization,
          capture, and maintenance used by lifecycle policy.
        - ``source_revision_port`` (SourceRevisionPort): Bounded running-source
          revision and tracked-dirty queries used by identity and drift policy.

    Conditionally required:
        - ``refresh_watcher`` (RefreshWatcherPort | None): Detached-process
          handoff for the generated relaunch watcher script, used by
          ``_perform_refresh`` after the ``.refresh``/``.refresh.taken``
          handshake completes. Composition roots (``lingtai.Agent``,
          ``lingtai.cli``) always inject the production adapter; there is no
          no-op watcher and Core never constructs the concrete adapter. A raw
          ``BaseAgent`` built without one (e.g. most non-refresh tests)
          constructs successfully — omitting it only fails loudly inside
          ``_perform_refresh``, and only once a real launch command exists,
          before any handshake or shutdown mutation. The no-launch-cmd path
          (``_build_launch_cmd()`` returns ``None``) works without it.

    Services (all optional):
        - ``service`` (LLMService): The brain — thinking, generating text.
        - ``file_io`` (FileIOService): File access — backs read/edit/write/glob/grep.
        - ``mail_service`` (MailTransportPort): Message transport — backs mail intrinsic.
        - ``event_journal`` (EventJournalPort): Durable structured event append.

    Missing service = intrinsics backed by it are auto-disabled.

    Subclasses customize behavior via:
        - ``_pre_request(msg)`` — transform message before LLM send
        - ``_post_request(msg, result)`` — side effects after LLM responds
        - ``_handle_message(msg)`` — message routing (must call super for processing)
        - ``_get_guard_limits()`` — per-agent loop guard limits
        - ``_PARALLEL_SAFE_TOOLS`` — set of tool names safe for concurrent execution
    """

    agent_type: str = ""

    # Tools safe for concurrent execution
    _PARALLEL_SAFE_TOOLS: set[str] = set()

    # Inbox polling interval (seconds)
    _inbox_timeout: float = 1.0

    @_release_acquired_workdir_lease_on_init_failure
    def __init__(
        self,
        service: LLMService,
        *,
        agent_name: str | None = None,
        working_dir: str | Path,
        workdir_lease: WorkdirLeasePort,
        notification_store: "NotificationStorePort",
        agent_presence: AgentPresenceStorePort,
        lifecycle_clock: LifecycleClockPort,
        snapshot_port: SnapshotPort,
        source_revision_port: SourceRevisionPort,
        refresh_watcher: RefreshWatcherPort | None = None,
        provider_call_admission_port=None,
        derived_launch_admission_port=None,
        intrinsics: "Mapping[str, Mapping[str, Any]] | None" = None,
        file_io: Any | None = None,
        mail_service: Any | None = None,
        event_journal: EventJournalPort | None = None,
        config: AgentConfig | None = None,
        context: Any = None,
        admin: dict | None = None,
        streaming: bool = False,
        stream_progress_factory: Callable[[str], StreamProgressPort | None] | None = None,
        covenant: str = "",
        principle: str = "",
        substrate: str = "",
        procedures: str = "",
        pad: str = "",
        comment: str = "",
    ):
        self.agent_name = agent_name  # true name (真名) — immutable once set
        self.nickname: str | None = None  # mutable alias (别名)
        # A constrained composition injects one Core-owned provider-admission
        # Port. Wrap the service rather than only the main run loop: the root
        # session, summaries, soul, and future calls through this Agent service
        # cross the same boundary. Detached daemon/avatar execution constructs
        # independent provider services and therefore requires the separate
        # host-mediated derived-admission adapter; it must not inherit this
        # ContextVar or be treated as covered here. Generic agents retain the
        # unwrapped historical path.
        self._provider_call_admission_port = provider_call_admission_port
        # A separate host-mediated port decides daemon/avatar *process* launch.
        # It is intentionally not inferred from the provider-call port: generic
        # Agents preserve legacy behavior while a constrained composition must
        # supply its own fail-closed Driver authority.
        self._derived_launch_admission_port = derived_launch_admission_port
        if provider_call_admission_port is None:
            self.service = service
        else:
            from ..provider_admission import ProviderAdmittedLLMService

            self.service = ProviderAdmittedLLMService(
                service, provider_call_admission_port
            )
        self._config = config or AgentConfig()
        # Preset-loader hook: Agent wrapper composes it; None on a bare BaseAgent so `load_preset` fails loud.
        self._preset_loader: Callable[..., dict] | None = None
        self._context = context
        self._admin = admin or {}
        # Core receives the lifecycle clock as a required Port and binds it
        # before the first monotonic/wall sample below. Core never imports or
        # constructs the concrete adapter; the wall/monotonic domains stay
        # distinct (see kernel/lifecycle_clock/CONTRACT.md).
        self._lifecycle_clock = lifecycle_clock
        self._cancel_event = threading.Event()
        # Correlated inbound-turn state is process-local and protected separately
        # from the legacy process-global cooperative latch.
        self._turn_controls_lock = threading.Lock()
        self._turn_controls: dict[str, Any] = {}
        self._current_turn_control: Any | None = None
        self._state = AgentState.IDLE
        self._idle_since_monotonic: float | None = self._lifecycle_clock.monotonic_seconds()
        self._started_at: str = ""
        self._last_usage = None  # UsageMetadata from last LLM call, for ledger
        self._created_at: str = ""
        self._uptime_anchor: float | None = None  # set in start(), None means not started
        # Core receives both snapshot/revision capabilities as required Ports.
        self._snapshot_port = snapshot_port
        self._source_revision_port = source_revision_port
        # Core receives the refresh-watcher Port; the concrete detached-process
        # mechanism (a POSIX subprocess adapter today) is composed outside.
        # There is no no-op fallback, but construction itself does not require
        # it: composition roots always inject the production adapter, while a
        # raw BaseAgent (most non-refresh tests) may omit it and construct
        # successfully. `_perform_refresh` fails loudly if it is absent, but
        # only once a real launch command exists and before any handshake or
        # shutdown mutation (see kernel/refresh_watcher/CONTRACT.md).
        self._refresh_watcher = refresh_watcher
        self._runtime_identity_event_fields = runtime_identity_event_fields(
            self._source_revision_port
        )

        # Working directory (caller-owned path)
        self._workdir = WorkingDir(working_dir)
        self._working_dir = self._workdir.path

        # Core receives the journal Port; concrete storage is composed outside.
        self._event_journal = event_journal

        # Core receives the workdir-lease Port; the concrete exclusion mechanism
        # (a POSIX flock today) is composed outside. This is a required, explicit
        # dependency: there is no unlocked or no-op fallback.
        self._workdir_lease = workdir_lease

        # Acquire the working-directory lease (10s grace for prior process
        # cleanup) through the injected Port.
        self._workdir_lease_acquired = False
        self._workdir_lease.acquire(10)
        self._workdir_lease_acquired = True

        # Core receives the notification-store Port; the concrete persistence
        # mechanism (a POSIX filesystem adapter today) is composed outside.
        # This is a required, explicit dependency: there is no no-op fallback.
        self._notification_store = notification_store

        # Core receives the agent-presence Port bound to this working directory;
        # the concrete filesystem mechanism (a POSIX .agent.json/.agent.heartbeat
        # adapter today) is composed outside. The heartbeat loop publishes and
        # teardown withdraws liveness through it. Required and explicit: there is
        # no no-op fallback and Core never constructs the concrete adapter.
        self._agent_presence = agent_presence

        # --- Wire services ---
        # FileIOService: optional, provided by Agent or host
        self._file_io = file_io

        # MailService: None means mail intrinsic disabled
        self._mail_service = mail_service

        # Covenant, principle, substrate, procedures, and pad file paths
        system_dir = self._working_dir / "system"
        pad_file = system_dir / "pad.md"
        covenant_file = system_dir / "covenant.md"
        principle_file = system_dir / "principle.md"
        substrate_file = system_dir / "substrate.md"
        procedures_file = system_dir / "procedures.md"

        system_dir.mkdir(exist_ok=True)

        # The kernel-owned section mirrors (principle/substrate/procedures) may
        # carry skill-style YAML frontmatter on disk — developer-facing metadata
        # that must never reach the LLM prompt. Strip it on read so the section
        # the prompt manager renders is body-only. Covenant mirrors are operator
        # content with no frontmatter, but stripping is a no-op there too.
        from .._frontmatter import strip_frontmatter as _strip_frontmatter

        # Covenant: constructor value wins, then fall back to file on disk
        if covenant:
            covenant_file.write_text(covenant, encoding="utf-8")
        elif covenant_file.is_file():
            covenant = _strip_frontmatter(covenant_file.read_text(encoding="utf-8"))

        # Principle: constructor value wins, then fall back to file on disk
        if principle:
            principle_file.write_text(principle, encoding="utf-8")
        elif principle_file.is_file():
            principle = _strip_frontmatter(principle_file.read_text(encoding="utf-8"))

        # Substrate: lower-level BaseAgent seed/fallback. The init.json
        # contract is enforced by lingtai.agent.Agent, where substrate is
        # kernel-owned and not an external override.
        if substrate:
            substrate_file.write_text(substrate, encoding="utf-8")
        elif substrate_file.is_file():
            substrate = _strip_frontmatter(substrate_file.read_text(encoding="utf-8"))

        # Procedures: same pattern as covenant/principle
        if procedures:
            procedures_file.write_text(procedures, encoding="utf-8")
        elif procedures_file.is_file():
            procedures = _strip_frontmatter(procedures_file.read_text(encoding="utf-8"))

        # Pad: constructor value seeds the file if it doesn't exist
        if pad and not pad_file.is_file():
            pad_file.write_text(pad, encoding="utf-8")

        # Auto-load pad from file into prompt manager
        loaded_pad = ""
        if pad_file.is_file():
            loaded_pad = pad_file.read_text(encoding="utf-8")

        # System prompt manager
        self._prompt_manager = SystemPromptManager()
        if principle:
            self._prompt_manager.write_section("principle", principle, protected=True)
        if covenant:
            self._prompt_manager.write_section("covenant", covenant, protected=True)
        if substrate:
            self._prompt_manager.write_section("substrate", substrate, protected=True)
        if procedures:
            self._prompt_manager.write_section("procedures", procedures, protected=True)
        # Load existing rules from system/rules.md (survives molts, refreshes, and resumes)
        rules_md = system_dir / "rules.md"
        if rules_md.is_file():
            try:
                rules_content = rules_md.read_text(encoding="utf-8").strip()
                if rules_content:
                    self._prompt_manager.write_section("rules", rules_content, protected=True)
            except OSError:
                pass
        if loaded_pad.strip():
            self._prompt_manager.write_section("pad", loaded_pad)
        if comment:
            self._prompt_manager.write_section("comment", comment)

        # Soul delay — needed before manifest build
        self._soul_delay = max(1.0, self._config.soul_delay)

        # Agent ID, created_at, and molt_count — persistent state restored
        from datetime import datetime, timezone
        import secrets
        existing = self._workdir.read_full_manifest()
        self._agent_id: str = existing.get("agent_id", "")
        self._created_at: str = existing.get("created_at", "")
        self._molt_count: int = existing.get("molt_count", 0)
        if not self._agent_id or not self._created_at:
            now = datetime.now(timezone.utc)
            if not self._agent_id:
                self._agent_id = now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
            if not self._created_at:
                self._created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Stream-progress Port (kernel/stream_progress/). Core never constructs
        # the concrete loopback publisher: a composition root injects a factory
        # that receives the stable ``agent_id`` resolved just above and returns
        # the Port handed to ``SessionManager``. Bare/unit agents omit it and
        # a raising factory is fail-open — the agent boots without a badge.
        # An explicit ``streaming=False`` opt-out never calls the factory: a
        # non-streaming session has no deltas to publish, so composing a
        # publisher (and binding a loopback endpoint) for it would be waste.
        self._stream_progress: StreamProgressPort | None = None
        if stream_progress_factory is not None and streaming:
            try:
                self._stream_progress = stream_progress_factory(self._agent_id)
            except Exception:
                logger.warning("stream_progress_factory_failed", exc_info=True)

        # Write manifest — identity + construction recipe (no runtime state)
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        from .identity import _build_manifest
        manifest_data = _build_manifest(self)
        self._workdir.write_manifest(manifest_data)

        # Construction is README.md's third mechanical regeneration point
        # (template-version check only) — the one avatars reach, since they
        # never pass through refresh or molt before first use. Fail-soft: a
        # README problem must never break construction.
        try:
            from ..agent_readme import ensure_agent_readme

            ensure_agent_readme(self._working_dir, _log=self._log)
        except Exception:
            try:
                self._log("agent_readme_ensure_failed", stage="construction")
            except Exception:
                pass


        # Auto-inject identity into system prompt from manifest
        self._prompt_manager.write_section(
            "identity",
            _build_identity_section(
                manifest_data,
                mailbox_name=getattr(self, "_mailbox_name", None),
            ),
            protected=True,
        )

        self._nap_wake = threading.Event()  # signalled to wake nap early
        self._nap_wake_reason = ""  # why the nap was woken

        # Mailbox identity — capabilities override these to change notification text.
        self._mailbox_name = "email box"
        self._mailbox_tool = "email"

        # Non-intrinsic tool handlers (capabilities, MCP, add_tool)
        self._tool_handlers: dict[str, Callable[[dict], dict]] = {}
        self._tool_schemas: list[FunctionSchema] = []

        # The live official model-facing namespace: reserved plugin name → the
        # one declaration that claimed it. Owned here because the kernel owns
        # the live tool surface; written only by
        # ``lingtai.kernel.tool_plugin.register_official_tool_plugins``, which
        # refuses a second, different declaration for a claimed name *before*
        # it binds or mounts anything. Re-registering the same declaration on
        # refresh is idempotent. Cleared by ``_setup_from_init`` together with
        # the tool surface it describes, so the claim map never outlives the
        # tools it claims — a capability dropped from ``manifest.disable`` on
        # refresh leaves no stale claim. Read publicly through
        # ``official_tool_plugins``.
        self._official_tool_plugins: dict[str, Any] = {}
        # Persistent provenance for the official surface. Unlike the live claim
        # view, these anchors are not cleared by refresh: clearing/replacing the
        # public backing map must not admit a foreign declaration on the next
        # registration. The bound-result map is rebuilt with the live tool
        # surface and is checked by the mount and claim seams.
        self._official_tool_declarations: dict[str, Any] = {}
        self._official_tool_bindings: dict[str, Any] = {}

        # --- Wire intrinsic tools ---
        # Intrinsics are injected by the composing layer (``lingtai.Agent``
        # passes ``lingtai.tools.registry.INTRINSICS``). The kernel owns the tool
        # machinery, not the concrete tools: a bare ``BaseAgent`` with no
        # intrinsics is legal and intentional — it is pure machinery with an
        # empty tool surface. ``_intrinsic_modules`` maps name → the intrinsic
        # module (used by schema build / dispatch / boot / kernel hook lookup);
        # ``_intrinsics`` maps name → the bound handler closure.
        self._intrinsic_registry: Mapping[str, Mapping[str, Any]] = intrinsics or {}
        self._intrinsic_modules: dict[str, Any] = {}
        self._intrinsics: dict[str, Callable[[dict], dict]] = {}
        self._wire_intrinsics()

        # Inbox — text-channel notifications (mail, daemon, user input)
        self.inbox: queue.Queue[Message] = queue.Queue()

        # Involuntary tool-call inbox
        self._tc_inbox: TCInbox = TCInbox()

        # Tracks the most recent in-history call_id for each "single-slot" source.
        self._appendix_ids_by_source: dict[str, str] = {}

        # _pending_mail_notifications removed — email arrivals now use
        # single-slot unread-digest (email.unread) instead of per-arrival
        # notification pairs. Bounce/MCP/soul events publish their own
        # `.notification/*.json` files and don't need per-ref tracking.

        # LLM worker poison state. Set when WorkerStillRunningError means the
        # current in-memory ChatInterface may still be mutated by a worker
        # thread. Process-local only; refresh/relaunch restores from disk.
        self._llm_worker_interface_poisoned: bool = False
        self._llm_worker_poison_reason: str | None = None
        self._llm_worker_poison_artifact: str | None = None
        self._llm_worker_poisoned_at: str | None = None
        self._llm_worker_poison_turn_entry: str | None = None
        self._llm_worker_refresh_requested: bool = False
        self._llm_worker_refresh_source: str | None = None

        # system.sleep's persisted `.alarm` is shared by the tool handler and
        # the heartbeat. This narrow lock makes arm/expiry last-writer-wins
        # without widening notification or lifecycle state ownership.
        self._sleep_alarm_lock: threading.RLock = threading.RLock()
        self._sleep_alarm_problem_signature: str | None = None

        # Notification sync state (filesystem-as-protocol redesign).
        # _notification_fp: last-seen `.notification/` fingerprint for
        #   change-detection between heartbeat ticks.
        # _notification_block_id: call_id of the most recently injected
        #   synthesized pair — kept for informational/molt-reset purposes;
        #   no longer used for remove_pair_by_call_id (pairs are now
        #   skeletonized in-place, not deleted).
        # See notifications.py and notification-filesystem-redesign.md.
        self._notification_fp: tuple = ()
        # _notification_raw_fp: the same fingerprint BEFORE daemon-attention
        # masking is applied. `_notification_fp` is the wake-deciding value
        # (masked, so a sub-threshold daemon channel never moves it); the raw
        # value is the true byte-exact directory fingerprint and is what a
        # non-forced dismiss's optimistic-concurrency check must compare
        # against (the Store's compare_update_channel always reads raw bytes
        # off disk, never the masked token). See notifications.dismiss_channel.
        self._notification_raw_fp: tuple = ()
        # Serializes ``_sync_notifications()`` check-then-act between the
        # run-loop IDLE boundary and the heartbeat thread (issue #659).
        # The store's flock guards on-disk mutations only; it does NOT
        # serialize this in-memory fingerprint check + wire append, so
        # without this lock both callers can pass the fp check and
        # double-inject notification pairs.  RLock so hook/synthesize
        # paths that transitively re-enter sync cannot self-deadlock.
        self._notification_sync_lock: threading.RLock = threading.RLock()
        # System-channel RMW serialization is owned by the injected
        # NotificationStorePort through compare_update_channel.
        # Last ACTIVE-state notification fingerprint that has already emitted
        # ``notification_deferred_active``.  This is intentionally separate
        # from ``_notification_fp``: ACTIVE must keep the delivery fingerprint
        # uncommitted so the next IDLE boundary retries, but the log should not
        # repeat the same status echo on every heartbeat.
        self._notification_deferred_log_fp: tuple = ()
        self._notification_block_id: str | None = None
        # Monotonic counter ensuring every synthesized notification pair
        # carries unique tokens (timestamp + seq) even when the underlying
        # payload repeats — defeats DeepSeek's cache fast-path empty-completion
        # failure mode on byte-identical synthetic pairs.
        self._notification_inject_seq: int = 0
        # Unified live notification holder — points to whichever dict
        # currently carries the live notification payload.  May be:
        #   * a normal tool-result content dict (ACTIVE path), or
        #   * a synthesized pair's result content dict (IDLE path).
        # Only ONE holder exists at a time.  When a new holder is
        # registered, the old one is skeletonized in-place so history
        # never accumulates stale notification data across results.
        # See `meta_block.skeletonize_notification_holder` and
        # `meta_block.attach_active_notifications`.
        #
        # The current notification payload is merged into the newest final
        # agent_meta snapshot on every eligible batch. The fingerprint and live
        # holder remain for delivery bookkeeping and historical ownership, but
        # they do not suppress the newest whole snapshot.
        self._notification_live_holder: dict | None = None
        # Material signature of the last emitted notification payload; retained
        # for delivery diagnostics and persistent-message bookkeeping. It is
        # not an attachment gate; reset to ``None`` whenever notifications go
        # empty so a later reappearance records a fresh diagnostic baseline.
        self._notification_payload_signature: str | None = None
        # Per-IM-channel persistent communication-context lane.  These IDs
        # track which messages have already been emitted in
        # `_meta.agent_meta.notifications.persistent.mcp.<channel>.messages` for the
        # current provider-visible context, so later deliveries can be deltas
        # with a `previous_block` hook pointing back to the previous block.
        # Reset on context molt. Snapshot-only IM lanes (currently WhatsApp) do
        # not keep agent-side delivery state.
        self._notification_persistent_telegram_message_ids: list[str] = []
        self._notification_persistent_telegram_last_tool_id: str | None = None
        self._notification_persistent_wechat_message_ids: list[str] = []
        self._notification_persistent_wechat_last_tool_id: str | None = None
        self._notification_persistent_feishu_message_ids: list[str] = []
        self._notification_persistent_feishu_last_tool_id: str | None = None

        # Retained legacy Telegram Task Card turn-local route bookkeeping.
        # It may still be captured/cleared for compatibility, but the current
        # intrinsic ``task_card`` producer does not consume it; channel consumers
        # discover and project the agent-local file artifact independently.
        self._telegram_task_card_context: dict | None = None

        # Provider-visible tool result currently carrying the latest whole
        # `_meta.agent_meta` snapshot (kernel runtime state, notifications, and
        # guidance). The designated final result becomes current each eligible
        # batch; older holders remain historical traces. See
        # `meta_block.attach_active_runtime` / `agent_meta_signature`.
        self._runtime_live_holder: dict | None = None
        # Material signature of the last emitted `_meta.agent_meta`; retained
        # for diagnostics/compatibility only. The complete newest snapshot is
        # emitted whenever private capture exists.
        self._agent_meta_signature: str | None = None
        # Resident Task Card meta axis (change-gated): signature of the last
        # attached ``_meta.agent_meta.taskcard`` payload and the live holder
        # carrying it. ``attach_active_taskcard`` compares the current
        # signature and only re-attaches on material change; identical bytes
        # are not re-injected every turn.
        self._taskcard_signature: str | None = None
        self._taskcard_live_holder: dict | None = None

        # Large-result hint threshold (chars).  When a main-agent tool result's
        # serialized length exceeds this value it is treated as "large": the
        # ToolExecutor stamps a tool_meta.comment.overflow hint, and the result
        # is surfaced for summarization through
        # _meta.agent_meta.agent_state.current_tool_result_chars.top_results.  Large results
        # no longer raise a `large_tool_result` system notification — see
        # meta_block.current_tool_result_chars and _maybe_notify_large_tool_result.
        # Default: 3000 chars.  Configurable only via manifest.summarize_notification_threshold
        # in init.json + refresh — runtime mutation is not supported.
        self._summarize_notification_threshold: int = 3000

        # Lifecycle
        self._shutdown = threading.Event()
        self._asleep = threading.Event()   # set when entering ASLEEP; cleared on wake
        self._thread: threading.Thread | None = None
        self._idle = threading.Event()
        self._idle.set()
        self._state = AgentState.IDLE
        self._sealed = False

        # Soul — inner voice
        self._soul_prompt = ""       # non-empty during inquiry
        self._soul_oneshot = False    # True during pending inquiry
        self._soul_timer: threading.Timer | None = None
        # Held while a soul flow consultation fire is running. Voluntary
        # soul(action='flow') calls try-acquire non-blocking — if held,
        # the call is rejected with "soul flow ongoing".
        self._soul_fire_lock: threading.Lock = threading.Lock()
        self._insight_turn_counter: int = 0

        # Agent record — throttled by LINGTAI_SESSION_STATS_REFRESH_SECONDS;
        # see _write_session_stats_record. Sequence is process-local only
        # (resets on restart); atomic replace already makes torn reads
        # impossible, sequence is a bonus ordering signal for one process.
        self._session_stats_last_written_at: float | None = None
        self._session_stats_sequence: int = 0
        # Created lazily by _write_session_stats_record so the explicit
        # background owner is only present for agents that publish this record.
        self._daemon_stats_snapshot = None

        # Heartbeat — always-on health monitor
        self._heartbeat: float = 0.0
        self._heartbeat_thread: threading.Thread | None = None
        # Final-stop signal for the heartbeat cadence. It stays distinct from
        # _shutdown because heartbeat remains live throughout teardown.
        self._heartbeat_stop = threading.Event()
        self._aed_start: float | None = None

        # Issue #164 — ACTIVE-without-progress watchdog.
        #
        # ``_state_changed_at`` records when the agent last transitioned
        # state (wall-clock seconds, ``self._lifecycle_clock.wall_seconds()``).
        # ``_last_progress_at``
        # is bumped by any of the kernel's progress events — ``wake``,
        # ``tc_wake_continue``, ``llm_call``, ``llm_response``, ``tool_call``,
        # ``tool_result``, ``notification_pair_injected``, and state
        # transitions themselves. The heartbeat tick reads both: when
        # ``state == ACTIVE`` and no progress event has fired for longer
        # than ``LINGTAI_ACTIVE_STUCK_THRESHOLD_S`` (default 600s, ~10min),
        # we log ``active_without_progress`` once per condition so the
        # symptom Jason reported (ACTIVE wedged + notification_deferred
        # storm with no turn ever starting) is diagnosable from the event
        # log instead of requiring forensic cross-referencing.
        #
        # The watchdog deliberately does NOT auto-restart the agent — the
        # safest action across the failure modes we've seen is "make it
        # visible and let admin or .clear handle recovery." Auto-restart
        # without understanding the underlying race could mask real bugs
        # behind retries.
        now_wall = self._lifecycle_clock.wall_seconds()
        self._state_changed_at: float = now_wall
        self._last_progress_at: float = now_wall
        #: Wall time of the most recent ``llm_call`` (API call start), used
        #: to surface "how long has this agent been active since its last
        #: API call" (Jason 2026-08-16). Seeded on state transitions like
        #: ``_last_progress_at`` so a fresh ACTIVE turn starts at zero;
        #: only ``llm_call`` bumps it afterwards.
        self._last_api_call_at: float | None = now_wall
        self._active_turn_kind: str | None = None
        self._active_turn_started_at: float | None = None
        self._active_turn_id: str | None = None
        #: Counts repeated ``notification_deferred_active`` events since
        #: the last successful injection. Reset on
        #: ``notification_pair_injected``. Surfaced in ``.status.json`` so
        #: the deferral storm in #164 shows up before the user notices.
        self._deferred_notifications_count: int = 0
        self._deferred_notifications_oldest_at: float | None = None
        #: One-shot latch so the watchdog logs exactly once per stuck
        #: episode. Cleared on any state transition out of ACTIVE.
        self._active_stuck_logged: bool = False

        # Snapshot — periodic git commits (Time Machine)
        self._last_snapshot: float = 0.0
        self._last_gc: float = 0.0

        # Auto-fallback state
        self._preset_fallback_attempted = False

        # Sent message tracker — dedup + idle-after-send for external channels
        from ..sent_message_tracker import SentMessageTracker
        self._sent_tracker = SentMessageTracker()

        # Session manager — LLM session, token tracking, compaction
        self._session = SessionManager(
            llm_service=self.service,
            config=self._config,
            agent_name=agent_name,
            streaming=streaming,
            build_system_prompt_fn=self._build_system_prompt,
            build_tool_schemas_fn=self._build_tool_schemas,
            logger_fn=self._log,
            build_system_batches_fn=self._build_system_prompt_batches,
            tool_result_recovery_lookup_fn=self._recover_pending_tool_result,
            stream_progress=self._stream_progress,
        )

        # Boot ordinary intrinsics first. Official-intrinsic shims retain the
        # injected kernel-hook and tool-call-id path, but their public surface is
        # mounted only by the declared host-plugin registrar below.
        for name in self._intrinsics:
            if self._intrinsic_registry.get(name, {}).get("official_plugin"):
                continue
            module = self._intrinsic_modules.get(name)
            boot_fn = getattr(module, "boot", None) if module is not None else None
            if boot_fn is not None:
                boot_fn(self)
        self._boot_official_intrinsics()

    # ------------------------------------------------------------------
    # Intrinsic wiring
    # ------------------------------------------------------------------

    def _wire_intrinsics(self) -> None:
        """Wire injected intrinsic tool handlers onto the tool surface.

        Iterates the registry injected at construction (``intrinsics=`` — the
        composing layer passes ``lingtai.tools.registry.INTRINSICS``). Each value has
        the shape ``{"module": <module>}``. ``_intrinsic_modules`` keeps the
        module for schema/description/boot/kernel-hook lookup; ``_intrinsics``
        holds the bound handler closure the dispatcher calls.
        """
        for name, info in self._intrinsic_registry.items():
            module = info["module"]
            self._intrinsic_modules[name] = module
            if info.get("official_plugin"):
                # Context needs the intrinsic dispatcher's private ``_tc_id``
                # injection for its exact live ToolCallBlock replay, but its
                # model-facing schema/handler are published exclusively through
                # the official host mount. This shim is transport routing, not a
                # second public registration.
                def _official_dispatch(args, _name=name):
                    handler = self._tool_handlers.get(_name)
                    if handler is None:
                        raise RuntimeError(
                            f"official intrinsic {_name!r} was dispatched before it mounted"
                        )
                    return handler(args)

                self._intrinsics[name] = _official_dispatch
            else:
                handle_fn = module.handle
                self._intrinsics[name] = lambda args, fn=handle_fn: fn(self, args)

    def _boot_official_intrinsics(self) -> None:
        """Run declared mandatory-plugin wiring after construction or refresh.

        Registry injection still keeps the kernel free of concrete family imports;
        the flag only distinguishes an internal transport/hook shim from a
        model-facing intrinsic registration. Each module's own ``boot`` owns the
        registrar call and therefore the declaration it publishes.
        """
        for name, info in self._intrinsic_registry.items():
            if not info.get("official_plugin"):
                continue
            module = self._intrinsic_modules.get(name)
            boot_fn = getattr(module, "boot", None) if module is not None else None
            if boot_fn is not None:
                boot_fn(self)

    def _intrinsic_hook(self, intrinsic: str, name: str):
        """Resolve a kernel-facing hook function from an injected intrinsic.

        The kernel used to reach into intrinsic modules by import (e.g.
        ``from ..intrinsics.soul.flow import _start_soul_timer``). After the
        tools consolidation the kernel cannot import ``tools``, so every such
        touchpoint resolves through the injected registry instead: the
        intrinsic package re-exports its kernel-facing functions from its
        package ``__init__`` as its documented hook surface.

        Returns the bound function, or ``None`` when the intrinsic is absent
        (bare ``BaseAgent``) or does not export the hook — callers no-op.
        """
        module = self._intrinsic_modules.get(intrinsic)
        if module is None:
            return None
        return getattr(module, name, None)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_idle(self) -> bool:
        return self._idle.is_set()

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def agent_id(self) -> str:
        """Permanent birth certificate — never changes across restarts or moves."""
        return self._agent_id

    @property
    def working_dir(self) -> Path:
        """The agent's working directory."""
        return self._workdir.path

    @property
    def official_tool_plugins(self) -> Mapping[str, Any]:
        """The live official model-facing namespace: plugin name -> declaration.

        The documented seam ``register_official_tool_plugins`` claims reserved
        official names through (``kernel/tool_plugin/CONTRACT.md``). It is the
        live mapping, not a copy — the registrar records claims in it and
        refuses a different declaration of a name already there — and it tracks
        the live tool surface: ``_setup_from_init`` clears it beside
        ``_tool_handlers`` / ``_tool_schemas``. Treat it as read-only elsewhere.
        """
        return MappingProxyType(self._official_tool_plugins)

    def _authorize_official_tool_declaration(self, declaration: Any) -> None:
        """Anchor one official declaration before its first bind.

        The anchor survives refresh and is deliberately separate from the
        read-only live claim view. This is not a public security boundary (a
        trusted in-process caller can inspect private state), but ordinary
        extension/public registration cannot clear claims and then substitute a
        different declaration for a live official name.
        """
        from ..tool_plugin import OFFICIAL_TOOL_PLUGIN_NAMES, ToolPluginDeclaration

        if not isinstance(declaration, ToolPluginDeclaration):
            raise PermissionError("official registration requires a declared plugin")
        name = declaration.name
        if name not in OFFICIAL_TOOL_PLUGIN_NAMES:
            raise PermissionError("cannot anchor an unreserved official name")
        current = self._official_tool_declarations.get(name)
        if current is not None and current is not declaration:
            from ..tool_plugin import DuplicateToolPluginNameError

            raise DuplicateToolPluginNameError(
                f"official tool plugin name {name!r} is anchored to a different "
                "declaration; official names are not overwritable"
            )
        self._official_tool_declarations[name] = declaration

    def _record_official_tool_binding(self, declaration: Any, plugin: Any) -> None:
        """Record the exact bound result issued by the kernel registrar."""
        self._authorize_official_tool_declaration(declaration)
        self._official_tool_bindings[declaration.name] = plugin

    def _claim_official_tool(self, transaction: Any) -> None:
        """Record a claim only after this Agent mounted an issued transaction."""
        from ..tool_plugin import _OfficialMountTransaction

        if not isinstance(transaction, _OfficialMountTransaction):
            raise PermissionError("official claims require a registrar transaction")
        declaration = transaction.declaration
        name = declaration.name
        if transaction.mounted_agent is not self:
            raise PermissionError("official claim requires a completed official mount")
        if self._official_tool_declarations.get(name) is not declaration:
            raise PermissionError("official claim is not for the anchored declaration")
        if self._official_tool_bindings.get(name) is not transaction.plugin:
            raise PermissionError("official claim is not for the canonical bound result")
        self._official_tool_plugins[name] = declaration

    @property
    def _chat(self) -> Any:
        """Proxy to SessionManager's chat session."""
        return self._session.chat

    @_chat.setter
    def _chat(self, value: Any) -> None:
        self._session.chat = value

    @property
    def _streaming(self) -> bool:
        """Proxy to SessionManager's streaming flag."""
        return self._session.streaming

    @property
    def _token_decomp_dirty(self) -> bool:
        """Proxy to SessionManager's token decomp dirty flag."""
        return self._session.token_decomp_dirty

    @_token_decomp_dirty.setter
    def _token_decomp_dirty(self, value: bool) -> None:
        self._session.token_decomp_dirty = value

    @property
    def _interaction_id(self) -> str | None:
        """Proxy to SessionManager's interaction ID."""
        return self._session.interaction_id

    @_interaction_id.setter
    def _interaction_id(self, value: str | None) -> None:
        self._session.interaction_id = value

    @property
    def _intermediate_text_streamed(self) -> bool:
        """Proxy to SessionManager's intermediate text streamed flag."""
        return self._session.intermediate_text_streamed

    @_intermediate_text_streamed.setter
    def _intermediate_text_streamed(self, value: bool) -> None:
        self._session.intermediate_text_streamed = value

    # ------------------------------------------------------------------
    # Naming (pass-throughs to identity.py)
    # ------------------------------------------------------------------

    def set_name(self, name: str) -> None:
        from .identity import _set_name
        _set_name(self, name)

    def set_nickname(self, nickname: str) -> None:
        from .identity import _set_nickname
        _set_nickname(self, nickname)

    def _update_identity(self) -> None:
        from .identity import _update_identity
        _update_identity(self)

    # ------------------------------------------------------------------
    # Lifecycle (pass-throughs to lifecycle.py + direct methods)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the agent's main loop thread."""
        from .lifecycle import _start
        _start(self)

    def _reset_uptime(self) -> None:
        """Reset the uptime anchor for runtime uptime tracking."""
        from .lifecycle import _reset_uptime
        _reset_uptime(self)

    def stop(self, timeout: float = 5.0) -> StopResult:
        """Request shutdown and return proof of execution quiescence/timeout."""
        from .lifecycle import _stop
        return _stop(self, timeout)

    def _close_agent_owned_services_after_quiescence(self) -> None:
        """Subclass hook run only after run-loop/provider quiescence is proven."""

    def _request_turn_cancel(self) -> None:
        """Latch cooperative cancellation for the current logical turn."""
        self._cancel_event.set()

    def _set_state(self, new_state: AgentState, reason: str = "") -> None:
        """Transition to a new state.

        Drives the soul cadence timer: the timer runs only while the
        agent is IDLE.  Entering IDLE starts a fresh ``soul_delay``-second
        timer; leaving IDLE (to ACTIVE, STUCK, ASLEEP, or SUSPENDED)
        cancels it.  The timer does NOT reschedule itself after firing —
        the next IDLE transition starts a fresh countdown.
        """
        _start_soul_timer = self._intrinsic_hook("soul", "_start_soul_timer")
        _cancel_soul_timer = self._intrinsic_hook("soul", "_cancel_soul_timer")

        old = self._state
        if old == new_state:
            return
        self._state = new_state
        if new_state == AgentState.ACTIVE:
            self._idle.clear()
        else:
            self._idle.set()

        # Soul timer + hidden idle-timeout bookkeeping: IDLE-only.  Start on
        # entering IDLE, cancel/clear on leaving. No-op when soul is absent.
        if new_state == AgentState.IDLE:
            self._idle_since_monotonic = self._lifecycle_clock.monotonic_seconds()
            if _start_soul_timer is not None:
                _start_soul_timer(self)
        elif old == AgentState.IDLE:
            self._idle_since_monotonic = None
            if _cancel_soul_timer is not None:
                _cancel_soul_timer(self)

        # Issue #164 — watchdog bookkeeping. A state transition is itself
        # forward progress, so reset the no-progress clock. The
        # one-shot stuck-logged latch is cleared whenever we leave ACTIVE
        # so the next stuck episode can be reported.
        now_wall = self._lifecycle_clock.wall_seconds()
        self._state_changed_at = now_wall
        self._last_progress_at = now_wall
        self._last_api_call_at = now_wall
        if new_state == AgentState.ACTIVE:
            # The kernel doesn't know yet what kind of turn this will be —
            # the next progress event (``wake``, ``tc_wake_continue``,
            # ``llm_call``, ``tool_call``) refines this. We seed with a
            # "pending" marker so .status.json never claims a turn is
            # already in flight when only the state flipped.
            self._active_turn_kind = "pending"
            self._active_turn_started_at = now_wall
            self._active_turn_id = None
        else:
            self._active_turn_kind = None
            self._active_turn_started_at = None
            self._active_turn_id = None
            self._active_stuck_logged = False

        self._log("agent_state", old=old.value, new=new_state.value, reason=reason)
        self._workdir.write_manifest(self._build_manifest())

    def _wake_nap(self, reason: str) -> None:
        """Signal the nap to wake up with a given reason."""
        self._nap_wake_reason = reason
        self._nap_wake.set()

    def _note_notification_deferred_active(self, fp: tuple, *, sources: list[str]) -> None:
        """Record ACTIVE notification deferral without per-heartbeat log spam.

        ACTIVE deliberately leaves ``_notification_fp`` uncommitted so delivery
        retries at the next IDLE boundary.  Heartbeat ticks therefore rediscover
        the same filesystem fingerprint.  Keep watchdog counters accurate for
        every tick, but emit ``notification_deferred_active`` only once per
        distinct notification fingerprint.
        """
        self._deferred_notifications_count += 1
        if self._deferred_notifications_oldest_at is None:
            self._deferred_notifications_oldest_at = self._lifecycle_clock.wall_seconds()

        if fp == getattr(self, "_notification_deferred_log_fp", ()):
            return

        self._log(
            "notification_deferred_active",
            sources=sources,
            _deferred_counter_already_updated=True,
        )
        self._notification_deferred_log_fp = fp

    def _log(self, event_type: str, **fields) -> None:
        """Write a structured event to the logging service, if configured.

        Also updates issue #164 watchdog bookkeeping: known progress
        events bump ``_last_progress_at`` and may refine the active-turn
        kind/id, and ``notification_deferred_active`` events update the
        deferred-notification counters.
        """
        deferred_counter_already_updated = bool(
            fields.pop("_deferred_counter_already_updated", False)
        )

        # Watchdog bookkeeping — done before the actual log write so the
        # bookkeeping is in place even if the log service raises.
        if event_type in _PROGRESS_EVENTS:
            self._last_progress_at = self._lifecycle_clock.wall_seconds()
            if event_type == "llm_call":
                # "Active since last API call" — the wall time of the most
                # recent LLM API call start, so external observers (taskcard
                # footer, TUI Email To) can show how long the agent has been
                # grinding since it last talked to the model (Jason
                # 2026-08-16). Only ``llm_call`` bumps it, not responses,
                # tools, or notifications.
                self._last_api_call_at = self._lifecycle_clock.wall_seconds()
            kind = _PROGRESS_EVENTS[event_type]
            if kind is not None:
                self._active_turn_kind = kind
                self._active_turn_started_at = self._last_progress_at
            # ToolExecutor emits provider IDs as tool_call_id; older/manual
            # event producers may still use call_id. Surface either one so
            # status snapshots can tie back to events.jsonl.
            call_id = fields.get("tool_call_id") or fields.get("call_id")
            if isinstance(call_id, str):
                self._active_turn_id = call_id
        elif event_type == "notification_deferred_active":
            if not deferred_counter_already_updated:
                self._deferred_notifications_count += 1
                if self._deferred_notifications_oldest_at is None:
                    self._deferred_notifications_oldest_at = self._lifecycle_clock.wall_seconds()
        elif event_type == "agent_state":
            # Successful injection / state transitions reset the deferral
            # storm counter — the very next state change after a deferral
            # storm is exactly the recovery signal we want to note.
            if self._deferred_notifications_count:
                self._deferred_notifications_count = 0
                self._deferred_notifications_oldest_at = None

        if self._event_journal is not None:
            self._event_journal.append({
                "type": event_type,
                "address": self._working_dir.name,
                "agent_name": self.agent_name,
                "ts": self._lifecycle_clock.wall_seconds(),
                **self._runtime_identity_event_fields,
                **fields,
            })

    def wake(self, reason: str) -> None:
        """Wake the agent from nap. Call when external input arrives."""
        self._wake_nap(reason)

    def log(self, event_type: str, **fields) -> None:
        """Write a structured event to the agent's event log."""
        self._log(event_type, **fields)

    # ------------------------------------------------------------------
    # Public addon API (pass-throughs)
    # ------------------------------------------------------------------

    def _on_mail_received(self, payload: dict) -> None:
        from .messaging import _on_mail_received
        _on_mail_received(self, payload)

    def _on_normal_mail(self, payload: dict) -> None:
        from .messaging import _on_normal_mail
        _on_normal_mail(self, payload)

    def _enqueue_system_notification(
        self,
        *,
        source: str,
        ref_id: str,
        body: str,
        skip_if_ref_id_exists: bool = False,
        idempotency_key: str | None = None,
        skip_if_idempotency_key_exists: bool = False,
        priority: str = "normal",
        extra: dict | None = None,
        channel: str = "system",
    ) -> str:
        from .messaging import _enqueue_system_notification
        return _enqueue_system_notification(
            self,
            source=source,
            ref_id=ref_id,
            body=body,
            skip_if_ref_id_exists=skip_if_ref_id_exists,
            idempotency_key=idempotency_key,
            skip_if_idempotency_key_exists=skip_if_idempotency_key_exists,
            priority=priority,
            extra=extra,
            channel=channel,
        )

    def notify(self, sender: str, text: str) -> None:
        from .messaging import _notify
        _notify(self, sender, text)

    def _rescan_large_tool_results(self) -> int:
        from .messaging import _rescan_large_tool_results
        return _rescan_large_tool_results(self)

    # ------------------------------------------------------------------
    # Soul (pass-throughs to soul_flow.py)
    # ------------------------------------------------------------------

    def _start_soul_timer(self) -> None:
        fn = self._intrinsic_hook("soul", "_start_soul_timer")
        if fn is not None:
            fn(self)

    def _cancel_soul_timer(self) -> None:
        fn = self._intrinsic_hook("soul", "_cancel_soul_timer")
        if fn is not None:
            fn(self)

    def _soul_whisper(self) -> None:
        fn = self._intrinsic_hook("soul", "_soul_whisper")
        if fn is not None:
            fn(self)

    def _drain_tc_inbox(self) -> None:
        """Splice queued involuntary tool-call pairs at a safe boundary.

        Also (re)installs the pre-request drain hook on the active chat
        session — see :meth:`_install_drain_hook` for the rationale.
        Called from two paths today: the entry drain at request start
        (``base_agent/turn.py:_handle_request``) and the dedicated TC
        wake handler (``_handle_tc_wake``). The pre-request hook itself
        adds a third path: drain fires once per LLM round-trip inside
        the tool-call loop, so mail notifications and soul.flow voices
        splice into the wire mid-task instead of waiting for the outer
        turn to end.
        """
        from .worker_recovery import is_worker_interface_poisoned
        if is_worker_interface_poisoned(self):
            self._log(
                "tc_inbox_drain_skipped_poisoned_interface",
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
            )
            return
        if self._chat is None:
            try:
                self._session.ensure_session()
            except Exception:
                return
        # Idempotent — re-installing the same hook on the same session
        # is a no-op. Cheap to call on every drain so a session created
        # via _rebuild_session (AED recovery) gets the hook automatically
        # without the AED path needing to know about it.
        self._install_drain_hook()
        result = self._tc_inbox.drain_into(
            self._chat.interface,
            self._appendix_ids_by_source,
        )
        if result.count > 0:
            self._log("tc_inbox_drain", count=result.count, sources=result.sources)
            self._save_chat_history()

    def _install_drain_hook(self) -> None:
        """Install the mid-turn tc_inbox drain hook on the active chat session.

        The hook fires inside each adapter's ``send()`` after the message
        has been committed to the canonical ChatInterface but before the
        API call — at that moment the wire tail is ``user[tool_results]``
        or ``user[text]``, so ``has_pending_tool_calls()`` returns False
        and the splicer can safely append a new ``(call, result)`` pair.

        Wire-state semantic, in two regimes:

        * **Canonical-interface adapters** (anthropic, openai-CC,
          codex-Responses, deepseek): the hook splices into the same
          interface the adapter is about to serialize for the wire, so
          the spliced pair appears in the *current* API request.
          Mail notifications enqueued during a long bash chain reach
          the LLM within one tool round.

        * **Server-state adapters** (OpenAIResponsesSession, both
          GeminiChatSession and InteractionsChatSession): the hook
          splices into the canonical interface, but the wire payload
          for the current request is built from server-side state
          (``previous_response_id`` / ``previous_interaction_id``) or
          the genai SDK's own chat history. The spliced pair is only
          visible to the LLM on the *next* turn after the agent
          re-syncs. The agent-side persistence and inspection paths
          (chat_history.jsonl, .status.json, /codex view) update
          immediately either way.

        Subtle semantic for ``replace_in_history=True`` (soul.flow):
        when the hook fires mid-turn, splicing in a replacement pair
        removes the prior pair of the same source from the interface.
        This is *almost* identical to the turn-boundary behavior that
        already exists today, with one nuance: the LLM's reasoning in
        the *current* turn was conditioned on a wire that contained
        the prior pair, but its next API call (or its in-flight
        reasoning continuation) may serialize a wire that doesn't.
        For soul.flow's reflective voices this is harmless — they
        don't drive tool calls and the model isn't building a chain
        of reasoning that depends on the prior voice's exact text.
        For any future producer that uses ``replace_in_history=True``
        with content the agent might cite mid-turn, this is a
        consideration; flagged here rather than buried in commit
        history.

        Idempotent: re-assigning the same callable to the same session
        attribute is a no-op. Called from :meth:`_drain_tc_inbox` so
        sessions created via ``_rebuild_session`` (AED recovery) pick
        up the hook on the next drain without a separate code path.
        """
        if self._chat is None:
            return
        if not hasattr(self._chat, "pre_request_hook"):
            return
        # Bind via lambda so the hook captures self, not the chat session.
        # The drain method itself rebinds to self._chat.interface, so the
        # hook ignores the interface argument the adapter passes in.
        self._chat.pre_request_hook = lambda _iface: self._drain_tc_inbox_for_hook()

    def _drain_tc_inbox_for_hook(self) -> None:
        """Hook-callable variant of _drain_tc_inbox without re-installing.

        The pre-request hook is called from inside an adapter's send(),
        which means we're already inside a session.send() call. Calling
        the full _drain_tc_inbox would try to re-install the hook (cheap
        but pointless) and could in pathological cases recurse if a
        future producer enqueues during drain. This variant just splices
        and returns.
        """
        from .worker_recovery import is_worker_interface_poisoned
        if is_worker_interface_poisoned(self):
            self._log(
                "tc_inbox_drain_skipped_poisoned_interface",
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
                from_hook=True,
            )
            return
        if self._chat is None:
            return
        result = self._tc_inbox.drain_into(
            self._chat.interface,
            self._appendix_ids_by_source,
        )
        if result.count > 0:
            self._log(
                "tc_inbox_drain",
                count=result.count,
                sources=result.sources,
                from_hook=True,
            )
            self._save_chat_history()

    # ------------------------------------------------------------------
    # Notification sync — filesystem-as-protocol replacement for tc_inbox.
    # See notifications.py for the notification filesystem design rationale.
    # ------------------------------------------------------------------

    def _sync_notifications(self) -> None:
        """Sync `.notification/` state into the wire.

        Serialized under ``_notification_sync_lock``: the run-loop IDLE
        boundary (turn.py) and the heartbeat thread (lifecycle.py) call
        this concurrently, and without the lock their check-then-act on
        ``_notification_fp`` can both observe the same stale fingerprint
        and inject duplicate notification pairs (issue #659).
        """
        lock = getattr(self, "_notification_sync_lock", None)
        if lock is None:
            # Partial test doubles bypass __init__; give them a real lock.
            lock = self._notification_sync_lock = threading.RLock()
        with lock:
            self._sync_notifications_locked()

    def _sync_notifications_locked(self) -> None:
        """Sync `.notification/` state into the wire.

        Takes one coherent observation of `.notification/` (fingerprint,
        daemon-attention mask, and payloads describing a single instant) and
        compares its masked fingerprint against ``_notification_fp``; if
        unchanged, no wake-worthy delta exists. But the mask keeps a virtual
        quiet daemon entry present whether or not `daemon.json` exists, so a
        masked-unchanged tick can still hide a real on-disk change (a
        sub-threshold daemon clear). That case is detected via the raw
        (unmasked) fingerprint: if it moved, a live holder still advertising
        the removed state is skeletonized — without injecting or waking, since
        the wake-deciding fingerprint itself did not change.

        On an actual (masked) fingerprint change:
        1. Skeletonize the current live holder (if any) in-place — does NOT
           remove synthesized pairs from history.  Synthesized pairs are kept
           as placeholder skeletons; only normal tool-result dicts have their
           notification keys stripped.
        2. If the new collection is empty, commit the empty fingerprint and
           return.
        3. Otherwise, inject a new block appropriate for current state:

           * IDLE → splice ``(call, result)`` pair (impersonates a
             voluntary ``notification(action="check")`` call from the
             agent's perspective), post ``MSG_TC_WAKE`` so the run loop
             unblocks and ``_handle_tc_wake`` drives the next inference
             round off the existing wire — no fake user input, no meta
             prefix.
           * ACTIVE → defer without touching the wire or committing the
             fingerprint; the next IDLE boundary retries delivery via
             the ordinary synthetic pair path.
           * ASLEEP → wake to IDLE, splice the pair, post
             ``MSG_TC_WAKE``.

        Invariant: at most one result block is tracked as the current LIVE
        notification holder at any time. Old synthesized pairs become skeleton
        placeholders but are never deleted; normal tool results keep old
        payload copies as historical timely state. The conversation structure is
        preserved, and model-facing serialization does not strip timely-transient
        keys from older holders; only the latest holder per family is current
        state.

        The fingerprint is committed only when injection succeeds (or
        when in a state that cannot inject — STUCK/SUSPENDED/empty).
        If injection is blocked (e.g. ``has_pending_tool_calls()``),
        the fingerprint stays at its prior value and the next heartbeat
        tick retries.
        """
        from ..notifications import (
            DAEMON_CHANNEL,
            _workdir_key,
            arm_notification_delay_timer,
            coherent_attention_read,
            flag_unregistered_channel,
            is_channel_allowed,
            is_present_channel_flagable,
            masked_empty_attention_fp,
            sync_hook_registry,
        )
        from ..meta_block import skeletonize_notification_holder
        from .worker_recovery import (
            is_worker_interface_poisoned,
            request_worker_hang_refresh,
        )

        def _skip_poisoned_sync(*, phase: str) -> bool:
            """Fail closed: never touch a poisoned interface; request refresh."""
            if not is_worker_interface_poisoned(self):
                return False
            artifact = getattr(self, "_llm_worker_poison_artifact", None)
            self._log(
                "notification_sync_skipped_poisoned_interface",
                phase=phase,
                artifact=artifact,
                action="refresh_requested",
            )
            request_worker_hang_refresh(
                self,
                artifact_relpath=artifact,
                source="notification_sync",
            )
            return True

        store = self._notification_store

        # Seed the module-level hook-channel mirror from hooks.json so the
        # allow predicate below sees registered external-hook channels.
        sync_hook_registry(self)

        def _allow(channel: str) -> bool:
            return is_channel_allowed(channel, workdir=_workdir_key(self))

        # Re-arm the durable consumer-delay timer after refresh/restart.  The
        # helper also recovers an overdue delay before the coherent read, so its
        # target becomes visible together with the delay-alarm mirror.
        arm_notification_delay_timer(self)

        # One coherent observation: the fingerprint, the daemon-attention mask
        # derived from it, and the payloads all describe the same instant
        # (verify-and-retry inside `coherent_attention_read`). Reading them
        # independently tears — an alarmed daemon write seen by the fingerprint
        # pass plus a clear seen by the payload pass would rewrite the alarmed
        # entry to the quiet token and lose the alarm edge with no file left to
        # replay it. Below the configured threshold the daemon entry collapses
        # to a constant token, so ordinary terminal notices stay readable
        # through snapshot/check without moving `fp` and waking the agent; the
        # strict count > N crossing flips the token once. No configured
        # threshold leaves the raw hash in place (usual per-terminal wake).
        observed = coherent_attention_read(store, _allow, _workdir_key(self))
        # An unstable read pairs a snapshot with a later fingerprint while a
        # producer is still writing. It is not authoritative notification state:
        # do not inject, skeletonize, or commit either version; retry next tick.
        if not observed.stable:
            return
        raw_fp = observed.raw_fp
        fp = observed.masked_fp

        # Stable current state, refreshed on every coherent read — including the
        # quiet paths below (masked sub-threshold arrivals, a delayed daemon
        # channel, an unchanged fingerprint). Attention decides whether to wake;
        # it never decides what is true, so the newest meta envelope reports
        # current bounded daemon progress even when nothing is delivered.
        daemon_summary = _daemon_notification_summary(
            observed.payloads.get(DAEMON_CHANNEL)
        )
        self._notification_daemon_summary = daemon_summary

        # Resolve the "never synced yet" baseline through the same mask. An
        # agent starts at `()`, but with a threshold configured the masked
        # fingerprint of an empty directory is not `()` — it is the virtual
        # quiet daemon token. Without this, the first sub-threshold daemon.json
        # an agent ever sees reads as a change and injects/wakes, which is
        # exactly what the threshold exists to prevent. An alarmed first write
        # (threshold 0, or a payload already past count > N) still differs from
        # this baseline, because its token is `daemon:alarm=1`, so it wakes.
        baseline = self._notification_fp
        if not baseline:
            baseline = masked_empty_attention_fp(_workdir_key(self))

        # The unregistered-channel scan needs the allow-*all* view, which the
        # coherent read (allow-filtered, and the value every wake comparison
        # uses) does not carry. It is best-effort diagnostics only, so a plain
        # extra pass is correct here — it never feeds a wake decision.
        present_fp = store.fingerprint(lambda ch: True)

        # Warn-and-flag (D2): detect present-but-unregistered channel files.
        # Iterates only when the allow-all view changed (cache), and skips
        # kernel-private dotfiles (e.g. .nudge_state.json) and syntactically
        # invalid stems so no unresolvable "register this hook" event is
        # emitted. Best-effort; never blocks sync.
        if present_fp != getattr(self, "_notification_present_fp", ()):
            self._notification_present_fp = present_fp
            for name, _, _ in present_fp:
                if not is_present_channel_flagable(name):
                    continue
                stem = name[: -len(".json")]
                if not is_channel_allowed(
                    stem,
                    workdir=_workdir_key(self),
                ):
                    flag_unregistered_channel(self, stem)

        if fp == baseline:
            # The wake-deciding (masked) fingerprint is unchanged, so no
            # injection and no wake are warranted. But the daemon attention
            # mask keeps a virtual quiet entry present in `fp` whether or not
            # `daemon.json` actually exists (see apply_daemon_attention_mask),
            # so a quiet clear — a sub-threshold daemon.json being removed —
            # is invisible in this comparison even though real on-disk state
            # changed. The raw (unmasked) fingerprint does see it.
            if raw_fp == getattr(self, "_notification_raw_fp", ()):
                return
            # An unstable observation is not evidence of anything: a producer
            # was writing throughout the read, so `payloads` may not pair with
            # `raw_fp`. Leave the raw fingerprint uncommitted and retry on the
            # next tick rather than retiring a holder on a torn read.
            if not observed.stable:
                return
            if _skip_poisoned_sync(phase="before_quiet_clear_check"):
                return
            # A hidden raw change is not necessarily a clear: a below-threshold
            # daemon arrival or count update must not retire an unrelated email
            # holder. Release only when the live holder actually advertises
            # daemon data and the stable current payload no longer has daemon.
            holder = getattr(self, "_notification_live_holder", None)
            holder_metadata = getattr(holder, "metadata", None)
            holder_agent_meta = (
                holder_metadata.get("agent_meta", {})
                if isinstance(holder_metadata, dict)
                else {}
            )
            holder_notifications = (
                holder_agent_meta.get("notifications", {}).get("attention", {})
                if isinstance(holder_agent_meta, dict)
                else {}
            )
            if (
                isinstance(holder_notifications, dict)
                and DAEMON_CHANNEL in holder_notifications
                and DAEMON_CHANNEL not in observed.payloads
            ):
                skeletonize_notification_holder(self)
            # Commit raw only after the quiet-clear action actually completed.
            # Committing before the poison check or the holder release would
            # let the next healthy tick see matching raw state and return
            # early, permanently skipping the release.
            self._notification_raw_fp = raw_fp
            return

        if _skip_poisoned_sync(phase="before_collect"):
            return

        # Reuse the payloads from the coherent observation `fp`/`raw_fp` were
        # derived from. A fresh snapshot here would reintroduce the tear the
        # coherent read exists to close: it could return state that does not
        # match the fingerprint about to be committed, so the committed version
        # would describe bytes the agent never delivered.
        notifications = observed.payloads
        source_signatures = _notification_source_signatures(notifications)

        def _delivery_provenance(*, waking: bool) -> dict:
            previous_signatures = getattr(
                self, "_notification_delivered_source_signatures", {}
            )
            if not isinstance(previous_signatures, Mapping):
                previous_signatures = {}
            changed_channels = sorted(
                source
                for source in set(previous_signatures) | set(source_signatures)
                if previous_signatures.get(source) != source_signatures.get(source)
            )
            provenance: dict[str, object] = {
                "kind": "wake" if waking else "delivery",
                "changed_channels": changed_channels,
            }
            if DAEMON_CHANNEL in changed_channels and isinstance(daemon_summary, dict):
                previous_daemon = getattr(
                    self, "_notification_delivered_daemon_summary", None
                )
                if not isinstance(previous_daemon, Mapping):
                    previous_daemon = {}
                provenance["daemon"] = _daemon_summary_delta(
                    previous_daemon, daemon_summary
                )
            telegram = notifications.get("mcp.telegram")
            telegram_data = telegram.get("data") if isinstance(telegram, Mapping) else None
            message_ids = telegram_data.get("message_ids") if isinstance(telegram_data, Mapping) else None
            if "mcp.telegram" in changed_channels and isinstance(message_ids, list):
                provenance["telegram"] = {
                    "message_ids": [str(item) for item in message_ids[-5:]],
                }
            if len(changed_channels) > 1:
                provenance["source_kind"] = "mixed"
            elif changed_channels:
                provenance["source_kind"] = changed_channels[0]
            else:
                provenance["source_kind"] = "unknown"
            return provenance

        def _inject_with_wake_provenance() -> bool:
            self._notification_wake_provenance = _delivery_provenance(waking=True)
            try:
                return self._inject_notification_pair(notifications)
            finally:
                # One synthesized result consumes this cause. Future ordinary
                # tool results keep only the stable daemon summary.
                self._notification_wake_provenance = None

        if not notifications:
            if _skip_poisoned_sync(phase="before_empty_skeletonize"):
                return
            # All channels cleared.  Skeletonize the current live holder
            # (whether it is a normal tool-result dict or a synthesized
            # pair content dict) so no history block keeps advertising
            # stale notification state.  Synthesized pairs remain in
            # history as placeholders; they are never deleted.
            skeletonize_notification_holder(self)
            self._notification_fp = fp
            self._notification_raw_fp = raw_fp
            self._notification_deferred_log_fp = ()
            return

        # --- Inject new block based on current state ---
        from ..state import AgentState

        inject_ok = False

        if self._state == AgentState.ASLEEP:
            if _skip_poisoned_sync(phase="asleep_before_wake"):
                return
            # Notification arrival wakes the agent, then inject as IDLE.
            # The synthesized (call, result) pair impersonates a
            # voluntary notification(action="check") call; MSG_TC_WAKE
            # unblocks the run loop so _handle_tc_wake drives one
            # inference round off the existing wire (no fake user
            # input, no meta prefix).
            #
            # If the wire has pending tool_calls left over from an
            # earlier turn that exited mid-sequence (e.g. AED-exhausted
            # ASLEEP after a stuck LLM call), `_inject_notification_pair`
            # would refuse the append to preserve alternation. Heal the
            # wire first by closing those pending calls with synthetic
            # error results, then retry. If injection STILL fails after
            # healing, fall through to the degraded path below: stay
            # IDLE, deliver a degraded `MSG_REQUEST` that points the
            # agent at the recovery handles, and commit the fingerprint
            # so the same failure does not replay until on-disk state
            # changes.
            self._asleep.clear()
            self._set_state(AgentState.IDLE, reason="notification_arrival")
            self._reset_uptime()
            # Old synthesized pairs are kept in history as placeholder
            # skeletons, not deleted.  Do not skeletonize the current holder
            # until this new injection succeeds; otherwise a blocked append
            # would discard the only live payload even though _notification_fp
            # remains uncommitted for retry.
            if _skip_poisoned_sync(phase="asleep_before_inject"):
                return
            inject_ok = _inject_with_wake_provenance()
            if not inject_ok:
                if _skip_poisoned_sync(phase="asleep_before_heal"):
                    return
                self._heal_pending_tool_calls(reason="wake_inject_blocked")
                if _skip_poisoned_sync(phase="asleep_before_reinject"):
                    return
                inject_ok = _inject_with_wake_provenance()
            if inject_ok:
                if _skip_poisoned_sync(phase="asleep_before_wake_enqueue"):
                    return
                from ..message import _make_message, MSG_TC_WAKE
                try:
                    wake_msg = _make_message(MSG_TC_WAKE, "system", "")
                    self.inbox.put(wake_msg)
                    self._wake_nap("notification_arrival")
                except Exception:
                    pass
            else:
                # Could not inject even after healing. Reverting to ASLEEP
                # without committing the fingerprint produced a livelock:
                # the next heartbeat tick saw the same .notification/
                # state, woke us again, failed inject again, reverted
                # again — forever (Jason's MCP/WeChat wake report).
                # Instead, stay IDLE and deliver a degraded MSG_REQUEST
                # that explains the situation and tells the agent how to
                # read the notification state directly. Commit the
                # fingerprint so the same failure does not replay.
                sources = sorted(notifications.keys())
                from ..message import _make_message, MSG_REQUEST
                degraded_text = (
                    "[system] Notification delivery could not be injected onto "
                    f"the wire after a heal attempt. Affected source(s): "
                    f"{', '.join(sources)}. Please query the current state by "
                    "calling notification(action=\"check\") or read the "
                    "producer files under .notification/ directly, then decide "
                    "whether to act. The kernel will not retry this delivery "
                    "until the on-disk state changes."
                )
                try:
                    self.inbox.put(_make_message(MSG_REQUEST, "system", degraded_text))
                    self._wake_nap("notification_arrival_degraded")
                except Exception:
                    pass
                self._log(
                    "notification_wake_degraded",
                    reason="inject_failed_after_heal",
                    sources=sources,
                )
                self._notification_fp = fp
                self._notification_raw_fp = raw_fp

        elif self._state == AgentState.IDLE:
            if _skip_poisoned_sync(phase="idle_before_inject"):
                return
            # Skeletonize + reinject AND post MSG_TC_WAKE.  IDLE is
            # "between turns, run loop blocked on inbox.get()" — without
            # a wake message the loop sits forever, the wire pair never
            # goes to the LLM, and the agent appears unresponsive even
            # though the notification arrived.
            #
            # _handle_tc_wake (post-rewrite) drives the wire forward
            # without appending anything: the (call, result) pair we
            # just spliced IS the new turn from the agent's perspective.
            # No fake user input, no meta prefix.
            #
            # Same heal-and-retry as the ASLEEP branch: if the wire has
            # dangling tool_calls, close them synthetically and retry,
            # otherwise the IDLE inbox stays dead.
            # Old synthesized pairs are kept in history as placeholder
            # skeletons, not deleted.  Do not skeletonize the current holder
            # until this new injection succeeds; otherwise a blocked append
            # would discard the only live payload even though _notification_fp
            # remains uncommitted for retry.
            inject_ok = self._inject_notification_pair(notifications)
            if not inject_ok:
                if _skip_poisoned_sync(phase="idle_before_heal"):
                    return
                self._heal_pending_tool_calls(reason="idle_inject_blocked")
                if _skip_poisoned_sync(phase="idle_before_reinject"):
                    return
                inject_ok = self._inject_notification_pair(notifications)
            if inject_ok:
                if _skip_poisoned_sync(phase="idle_before_wake_enqueue"):
                    return
                from ..message import _make_message, MSG_TC_WAKE
                try:
                    wake_msg = _make_message(MSG_TC_WAKE, "system", "")
                    self.inbox.put(wake_msg)
                    self._wake_nap("notification_sync")
                except Exception:
                    pass

        elif self._state == AgentState.ACTIVE:
            # Do not mutate unrelated tool results while a turn is active.
            # Leave the fingerprint uncommitted so the same on-disk
            # notification state is retried once the run loop transitions
            # to IDLE at the post-turn boundary.
            self._note_notification_deferred_active(
                fp,
                sources=list(notifications.keys()),
            )

        # STUCK / SUSPENDED — no injection.  The on-disk state is
        # observed; we just can't act on it until state recovers.

        # --- Commit fingerprint only if injection succeeded ---
        # ACTIVE deliberately defers without committing; only
        # STUCK/SUSPENDED commit here (they can't inject at all).
        if _skip_poisoned_sync(phase="before_fingerprint_commit"):
            return
        if inject_ok:
            self._notification_fp = fp
            self._notification_raw_fp = raw_fp
            self._notification_deferred_log_fp = ()
            # Provenance compares against the last notification snapshot the
            # model actually received, never against a deferred ACTIVE read.
            self._notification_delivered_source_signatures = source_signatures
            self._notification_delivered_daemon_summary = daemon_summary
        elif self._state in (AgentState.STUCK, AgentState.SUSPENDED):
            self._notification_fp = fp
            self._notification_raw_fp = raw_fp
            self._notification_deferred_log_fp = ()

    def _heal_pending_tool_calls(self, *, reason: str) -> bool:
        """Close unanswered tool_calls so subsequent appends respect pairing.

        The close path first replays any matching durable real tool results
        from ``logs/events.jsonl``; calls without recorded results still get the
        existing synthetic error results.

        Used by the notification-sync wake path: if a previous turn
        exited mid-tool-sequence (AED-exhausted, kernel exception, etc.)
        and left dangling tool_calls, ``_inject_notification_pair``
        refuses to append. Without healing, the agent is stuck —
        notifications keep arriving, the inject keeps failing, and the
        run loop never gets a MSG_TC_WAKE. Heal once on wake so the
        retry can succeed.

        Returns True if anything was closed, False if the wire was
        already clean (or the session isn't ready, in which case there's
        nothing we can do here).
        """
        from .worker_recovery import is_worker_interface_poisoned
        if is_worker_interface_poisoned(self):
            self._log(
                "heal_pending_tool_calls_skipped_poisoned_interface",
                reason=reason,
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
            )
            return False
        if self._chat is None:
            return False
        iface = self._chat.interface
        try:
            iface.tool_result_recovery_lookup = self._recover_pending_tool_result
        except Exception:
            pass
        if not iface.has_pending_tool_calls():
            return False
        diagnostics = _pending_tool_call_diagnostics(iface)
        try:
            iface.close_pending_tool_calls(reason=f"heal:{reason}")
        except Exception as e:
            self._log(
                "heal_pending_tool_calls_failed",
                reason=reason,
                error=str(e)[:200],
                **diagnostics,
            )
            return False
        self._log("heal_pending_tool_calls", reason=reason, **diagnostics)
        try:
            self._save_chat_history(ledger_source="heal")
        except Exception:
            pass
        return True

    def _recover_pending_tool_result(self, tool_call):
        from ..tool_result_recovery import recover_tool_result_block_from_events

        block = recover_tool_result_block_from_events(
            self._working_dir,
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            logger_fn=self._log,
        )
        if (
            block is not None
            and block.synthesized
            and isinstance(block.metadata, dict)
            and block.metadata.get("redacted") is True
        ):
            # Redacted replay is lossy: reset the committed fingerprint so the
            # next sync re-injects producer state (LICC "Redacted replay and
            # producer reconciliation"). Best-effort — must never break heal.
            try:
                self._notification_fp = ()
                self._notification_raw_fp = ()
                self._notification_deferred_log_fp = ()
                self._log(
                    "notification_redacted_replay_resync",
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                )
            except Exception:
                pass
        return block

    def _inject_notification_pair(self, notifications: dict) -> bool:
        """Inject a synthetic (call, result) pair for IDLE / ASLEEP states.

        Builds ``notification(action="check")`` / ``<JSON dict>`` and
        appends to the wire interface.  Records the call_id for later
        stripping.

        The synthesized pair is byte-shape-identical to a voluntary
        ``notification(action="check")`` read so the LLM cannot distinguish
        a kernel-injected delivery from one it issued itself; the
        ``_synthesized: true`` body flag remains the only marker.

        The assistant turn carries only the synthetic ``ToolCallBlock``.
        The model-visible notification details and guidance live in the
        matching ``ToolResultBlock`` body, so notification wakes do not
        surface as transcript text / diary-like synthesized summaries.

        The ``ToolResultBlock`` is created with ``synthesized=True``
        (the existing flag the kernel already uses for heal-path
        placeholders) and its ``content`` is a dict (not a JSON string) so
        every adapter can serialize it directly via ``json.dumps``.  When the
        live payload later moves to a newer holder, this dict is simply
        released from live tracking (``skeletonize_notification_holder``) —
        never mutated — so the pair stays in history exactly as recorded.
        The ``_synthesized: true`` field in the body lets the agent
        distinguish kernel-injected reads from voluntary calls when reading
        conversation history.

        The result content carries a monotonic injection_seq; its metadata
        carries the current build_meta-derived runtime sidecar. Internal tool-meta
        transit keys are stripped below before the synthesized pair reaches the wire.
        call.args deliberately carries none of this: it must stay byte-identical
        to what a voluntary ``notification(action="check")`` call could produce,
        since a provider/model can copy assistant-turn call args into a new real
        call, and the freshness fields would fail the family's root-field
        allowlist. The freshness fields still make every synthesized pair's
        result tokenize uniquely even when the underlying notification payload
        repeats — a protection layer against the DeepSeek cache fast-path
        empty-response failure without needing a visible assistant text prefix.

        Returns True if injection succeeded, False if it had to abort
        (e.g. pending tool_calls block append).  When False is returned,
        the caller MUST NOT update ``_notification_fp`` — otherwise the
        change would be silently dropped instead of retried.
        """
        import secrets
        from ..llm.interface import ToolCallBlock, ToolResultBlock
        from .worker_recovery import is_worker_interface_poisoned

        if is_worker_interface_poisoned(self):
            self._log(
                "notification_inject_skipped_poisoned_interface",
                sources=list(notifications.keys()),
                artifact=getattr(self, "_llm_worker_poison_artifact", None),
            )
            return False

        if self._chat is None:
            try:
                self._session.ensure_session()
            except Exception as e:
                self._log("notification_inject_aborted",
                          reason="ensure_session_failed", error=str(e)[:200])
                return False
            if self._chat is None:
                self._log("notification_inject_aborted",
                          reason="chat_still_none_after_ensure")
                return False

        iface = self._chat.interface
        # If the wire has unanswered tool_calls, appending a user-role
        # result entry would violate the alternation invariant.  Defer.
        if iface.has_pending_tool_calls():
            # Issue #126 diagnostic: log the tail shape so we can trace
            # why tool results were not detected as committed.
            tail_info = ""
            if iface._entries:
                last = iface._entries[-1]
                tail_info = f" tail_role={last.role} tail_blocks={len(last.content)}"
                if last.role == "assistant":
                    tc_ids = [b.id[:20] for b in last.content
                              if hasattr(b, 'id') and hasattr(b, 'name')]
                    tail_info += f" tc_ids={tc_ids}"
            self._log("notification_inject_aborted",
                      reason="pending_tool_calls",
                      sources=list(notifications.keys()),
                      _tail=tail_info)
            return False

        call_id = f"notif_{int(time.time()*1000):x}_{secrets.token_hex(2)}"

        # Capture the same build_meta current-state hints real tool results use
        # for runtime diagnostics. This copy is recorded in the injection event;
        # the model-visible freshness marker is added to result.content below.
        # Keep both result-side representations out of call.args: the monotonic
        # injection_seq guarantees novelty within the same second (heal+retry
        # tight loops, time-blind agents).
        # Defensive getattr covers test doubles that bypass __init__ and
        # don't carry the full agent attribute surface.
        self._notification_inject_seq = getattr(self, "_notification_inject_seq", 0) + 1
        try:
            meta = build_meta(self)
        except (AttributeError, TypeError):
            meta = {}
        # ``current_time`` and the ``_tool_meta_*`` transit keys are
        # permanent per-tool-result fields consumed by ToolExecutor.
        # Notification injections are synthesized pairs, not real tool results,
        # and already have injection_seq for freshness/novelty; never flatten
        # internal tool-meta transit payloads onto the model-visible wire.
        meta.pop("current_time", None)
        meta.pop(TOOL_META_CONTEXT_PENDING_KEY, None)
        meta.pop(TOOL_META_CONTEXT_EVENT_PENDING_KEY, None)
        meta["injection_seq"] = self._notification_inject_seq

        notifications_with_guidance = build_notification_payload(notifications)
        # Keep log-only source counts from the raw canonical payload before the
        # transient lanes are sanitized for model visibility.  For example,
        # email's model-visible hook drops count and keeps only email_ids, but
        # the operational injection log should still say "1 email".
        notification_summary_counts: dict[str, object] = {}
        raw_notifications = notifications_with_guidance.get("notifications")
        if isinstance(raw_notifications, dict):
            for raw_source, raw_payload in raw_notifications.items():
                raw_count = None
                if isinstance(raw_payload, dict):
                    raw_data = raw_payload.get("data") or {}
                    if isinstance(raw_data, dict):
                        raw_count = raw_data.get("count")
                        if raw_count is None and isinstance(
                            raw_data.get("events"), list
                        ):
                            raw_count = len(raw_data["events"])
                        if raw_count is None and isinstance(
                            raw_data.get("voices"), list
                        ):
                            raw_count = len(raw_data["voices"])
                notification_summary_counts[raw_source] = raw_count

        # Build the canonical two-axis sidecar. The handler-shaped body remains
        # independent; adapters project this sidecar into model-visible _meta.
        notification_persistent_payload = build_notification_persistent_payload(
            self, notifications_with_guidance
        )
        # Delivery accounting and the model-visible envelope must describe the
        # same persistent lane.  Merge the separately built durable snapshot
        # before constructing the ToolResultBlock sidecar.
        if isinstance(notification_persistent_payload, dict):
            notifications_with_guidance["notification_persistent"] = (
                notification_persistent_payload.get("notification_persistent", {})
            )
        # Move (not duplicate): curated durable IM context now lives in
        # persistent lanes, so strip it from the model-visible ephemeral lane
        # before it is nested into the synthesized pair's _meta (and the
        # summary/logging envelope built from the same payload below).  This runs
        # even when no new persistent block is emitted, because the transient lane
        # must still remain routing-only on deliberate notification checks.
        # `notifications_with_guidance` is freshly built for this delivery cycle,
        # so in-place trimming cannot mutate producer-owned state.
        sanitize_telegram_notification_after_persistent(notifications_with_guidance)
        sanitize_wechat_notification_after_persistent(notifications_with_guidance)
        sanitize_feishu_notification_after_persistent(notifications_with_guidance)
        sanitize_whatsapp_notification_after_persistent(notifications_with_guidance)
        sanitize_email_notification_after_persistent(notifications_with_guidance)
        body = {
            "_synthesized": True,
        }
        # Only the freshness marker belongs in the handler-shaped synthetic
        # body; current agent state remains in the sidecar.
        body["injection_seq"] = self._notification_inject_seq
        # Store body as a dict (not a JSON string) so it can be mutated
        # in-place when this pair is skeletonized later.  All adapters
        # already handle dict content via isinstance checks — see
        # interface_converters.py and anthropic/adapter.py.
        content_dict = body

        # Build a per-source summary: "3 email, 1 soul, 0 system".
        # Counts come from data.count / len(data.events) / len(data.voices)
        # depending on the producer; fall back to "?" if unparseable.
        summary_parts = []
        for source, payload in notifications_with_guidance["notifications"].items():
            count = None
            if isinstance(payload, dict):
                data = payload.get("data") or {}
                if isinstance(data, dict):
                    count = data.get("count")
                    if count is None and isinstance(data.get("events"), list):
                        count = len(data["events"])
                    if count is None and isinstance(data.get("voices"), list):
                        count = len(data["voices"])
            if count is None:
                raw_count = notification_summary_counts.get(source)
                if isinstance(raw_count, int):
                    count = raw_count
            if count is None and source == "email" and isinstance(
                notification_persistent_payload, dict
            ):
                persistent = notification_persistent_payload.get(
                    "notification_persistent"
                )
                if isinstance(persistent, dict):
                    email_context = persistent.get("email")
                    if isinstance(email_context, dict):
                        persistent_count = email_context.get("count")
                        if isinstance(persistent_count, int):
                            count = persistent_count
            summary_parts.append(f"{count if count is not None else '?'} {source}")
        guidance_text = (
            "Notice: this is kernel-synchronized state from notification channels, "
            "not necessarily a human instruction. Identify the source, interpret "
            "the relevant channel payload, and verify intent before deciding "
            "whether to act. If it contains an identifiable human message whose "
            "preview is truncated, ambiguous, includes media, or needs exact "
            "anchoring, first use the producer channel's normal read action; if "
            "a human is waiting, acknowledge directly before long work."
        )
        summary_text = (
            f"[synthesized — kernel notification sync] "
            f"Notification received: {', '.join(summary_parts)}. {guidance_text}"
            if summary_parts
            else f"[synthesized — kernel notification sync] Notification received. {guidance_text}"
        )

        # ``summary_text`` is log-only.  Do not place it in a TextBlock on the
        # wire: successful notification sync should be a structured
        # notification(action="check") call/result pair, not a visible
        # synthesized diary/text-input row.
        # ``notification`` is an LTP v2 family (``tools/CONTRACT.md``): the
        # default voluntary read is ``{action, input, reasoning}`` with
        # ``check``'s own strict-empty ``input``; the optional public
        # ``summarize`` control is valid but absent here. This synthesized call
        # must carry that same minimal envelope, because the pair is deliberately
        # byte-shape-identical to a
        # voluntary read (see this method's docstring) — emitting the old flat
        # ``{action}`` shape would make the kernel's own injection the one
        # notification call the model could never have produced itself.
        # ``reasoning`` is required by the family schema, so a truthful
        # synthetic rationale is supplied rather than omitted.
        # ``injection_seq`` stays out of ``call_block.args``: a provider/model
        # can and does copy assistant-turn tool-call args verbatim into a new
        # real call, and ``notification.handle`` rejects any root field
        # outside the public ``{action, input, reasoning, summarize}`` allowlist with
        # ``INVALID_ARGUMENT: unsupported notification argument`` — so this
        # pair's args must be exactly what a voluntary call could produce.
        # Freshness/novelty against byte-equality is carried on the result
        # side instead (``result_block.content["injection_seq"]`` /
        # ``result_block.metadata``), which is never fed back as call args.
        call_block = ToolCallBlock(
            id=call_id,
            name="notification",
            args={
                "action": "check",
                "input": {},
                "reasoning": "kernel notification sync",
            },
        )
        result_block = ToolResultBlock(
            id=call_id,
            name="notification",
            content=content_dict,
            metadata=build_synthetic_meta_envelope(
                self, notifications_with_guidance, call_id=call_id
            ),
            synthesized=True,
        )

        iface.add_assistant_message(content=[call_block])
        iface.add_tool_results([result_block])

        # Durable kernel-internal recovery record so heal replay can rebuild
        # the complete synthesized result (content + metadata sidecar +
        # provenance) instead of a tool_result_replay_miss. Deliberately no
        # tool_trace_id/lifecycle events; origin gates the extension fields.
        # `redacted` deterministically records whether the mandatory
        # redact_for_trajectory pass changed the durable payload vs the wire.
        # Best-effort: failure never aborts injection; surfaced as
        # recovery_record_error on notification_pair_injected.
        recovery_record_error: str | None = None
        try:
            from ..trace_redaction import redact_for_trajectory

            recovery_payload = {
                "tool_args": copy.deepcopy(call_block.args),
                "result": content_dict,
                "result_metadata": copy.deepcopy(result_block.metadata),
            }
            recovery_redacted = (
                redact_for_trajectory(recovery_payload) != recovery_payload
            )
            self._log(
                "tool_result",
                tool_call_id=call_id,
                tool_name="notification",
                tool_args=recovery_payload["tool_args"],
                status="success",
                elapsed_ms=0,
                result=content_dict,
                result_metadata=recovery_payload["result_metadata"],
                synthesized=result_block.synthesized,
                origin="kernel_notification_sync",
                redacted=recovery_redacted,
            )
        except Exception as exc:
            recovery_record_error = type(exc).__name__

        # The append succeeded.  Now release the previous live holder (if
        # any) from tracking before registering this synthesized pair as the
        # new live holder.  Doing it after append preserves the old live
        # payload if injection had to abort because of pending tool calls.
        # Release only stops future code from treating the prior holder as
        # authoritative — its recorded content is never mutated.
        prior_holder = getattr(self, "_notification_live_holder", None)
        if prior_holder is not None and prior_holder is not content_dict:
            try:
                from ..meta_block import skeletonize_notification_holder
                self._notification_live_holder = prior_holder
                skeletonize_notification_holder(self)
            except Exception:
                pass

        # Register content_dict as the live holder so future
        # skeletonize_notification_holder / attach_active_notifications calls
        # can release tracking of it without touching conversation history.
        # _notification_block_id is retained for informational / molt-reset
        # purposes; it is no longer used for remove_pair_by_call_id.
        self._notification_live_holder = result_block
        self._notification_block_id = call_id
        if notification_persistent_payload:
            record_notification_persistent_delivery(
                self,
                notification_persistent_payload,
                tool_call_id=call_id,
            )

        self._save_chat_history(ledger_source="notification_sync")
        pair_event_extra: dict = {}
        if recovery_record_error is not None:
            pair_event_extra["recovery_record_error"] = recovery_record_error
        self._log(
            "notification_pair_injected",
            call_id=call_id,
            sources=list(notifications.keys()),
            summary=summary_text,
            meta=meta,
            **pair_event_extra,
        )
        # Log the exact canonical sidecar that was attached to the live block.
        synthetic_envelope = result_block.metadata
        self._log_notification_block_injected(
            synthetic_envelope,
            mode="synthetic_notification_pair",
            call_id=call_id,
        )
        return True

    def _log_notification_block_injected(
        self,
        meta_envelope: dict,
        *,
        mode: str,
        call_id: str | None = None,
    ) -> None:
        """Persist a durable notification_block_injected event capturing the
        full ``_meta`` envelope the model saw.

        Best-effort: any exception is swallowed so callers are never broken by a
        logging failure.  ``meta_envelope`` is the complete four-block envelope
        — ``tool_meta``, ``agent_meta``, ``guidance``, plus ``notifications`` and
        ``notification_guidance`` — exactly as it appears under the tool result's
        ``_meta`` key (ACTIVE) or as reconstructed for the synthesized pair
        (IDLE/ASLEEP, via ``build_synthetic_meta_envelope``).

        The envelope is persisted under a top-level ``_meta`` field on the event
        so the TUI ``/notification`` view renders ``_meta.tool_meta`` /
        ``_meta.agent_meta`` / ``_meta.agent_meta.guidance`` /
        ``_meta.agent_meta.guidance.transient`` / ``_meta.agent_meta.notifications``
        directly.  A deep copy is stored so later
        in-place skeletonization or nested mutation of the live holder does not
        corrupt the logged snapshot.
        """
        try:
            agent_meta = meta_envelope.get("agent_meta", {})
            notifications = agent_meta.get("notifications", {}).get("attention", {}) if isinstance(agent_meta, dict) else {}
            sources = sorted(notifications.keys()) if isinstance(notifications, dict) else []
            self._log(
                "notification_block_injected",
                mode=mode,
                call_id=call_id or "",
                sources=sources,
                _meta=copy.deepcopy(meta_envelope),
            )
        except Exception:
            pass

    def _persist_soul_entry(self, result: dict, mode: str = "flow", source: str = "agent") -> None:
        fn = self._intrinsic_hook("soul", "_persist_soul_entry")
        if fn is not None:
            fn(self, result, mode=mode, source=source)

    def _append_soul_flow_record(self, record: dict) -> None:
        fn = self._intrinsic_hook("soul", "_append_soul_flow_record")
        if fn is not None:
            fn(self, record)

    def _run_inquiry(self, question: str, source: str = "agent") -> None:
        fn = self._intrinsic_hook("soul", "_run_inquiry")
        if fn is not None:
            fn(self, question, source=source)

    def _flatten_v3_for_pair(self, voice: dict) -> dict:
        fn = self._intrinsic_hook("soul", "_flatten_v3_for_pair")
        if fn is None:
            return voice
        return fn(self, voice)

    def _run_consultation_fire(self) -> None:
        fn = self._intrinsic_hook("soul", "_run_consultation_fire")
        if fn is not None:
            fn(self)

    def _rehydrate_appendix_tracking(self) -> None:
        fn = self._intrinsic_hook("soul", "_rehydrate_appendix_tracking")
        if fn is not None:
            fn(self)

    # ------------------------------------------------------------------
    # Heartbeat (pass-throughs to lifecycle.py)
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        from .lifecycle import _start_heartbeat
        _start_heartbeat(self)

    def _stop_heartbeat(self) -> None:
        from .lifecycle import _stop_heartbeat
        _stop_heartbeat(self)

    def _heartbeat_loop(self) -> None:
        from .lifecycle import _heartbeat_loop
        _heartbeat_loop(self)

    # ------------------------------------------------------------------
    # Main loop (pass-throughs to turn.py)
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        from .turn import _run_loop
        _run_loop(self)

    def _concat_queued_messages(self, msg: Message) -> Message:
        from .turn import _concat_queued_messages
        return _concat_queued_messages(self, msg)

    def _handle_message(self, msg: Message) -> None:
        from .turn import _handle_message
        _handle_message(self, msg)

    def _handle_request(self, msg: Message) -> None:
        from .turn import _handle_request
        _handle_request(self, msg)

    def _handle_tc_wake(self, msg: Message) -> None:
        from .turn import _handle_tc_wake
        _handle_tc_wake(self, msg)

    def _get_guard_limits(self) -> tuple[int, int, int]:
        from .turn import _get_guard_limits
        return _get_guard_limits(self)

    def _process_response(self, response, *, ledger_source: str = "main") -> dict:
        from .turn import _process_response
        return _process_response(self, response, ledger_source=ledger_source)

    # ------------------------------------------------------------------
    # Refresh / preset (pass-throughs to lifecycle.py)
    # ------------------------------------------------------------------

    def _perform_refresh(
        self,
        *,
        skip_chat_history_save: bool = False,
        skip_save_reason: str | None = None,
    ) -> None:
        from .lifecycle import _perform_refresh
        _perform_refresh(
            self,
            skip_chat_history_save=skip_chat_history_save,
            skip_save_reason=skip_save_reason,
        )

    def load_preset(self, name: str, working_dir: "str | Path | None" = None) -> dict:
        """Load a preset through the composed preset-loader hook.

        The surface daemon/system tools call so they never import Core
        ``load_preset`` or construct an adapter — the wrapper sets ``_preset_loader``.
        Fails loud on a bare BaseAgent. ``working_dir`` defaults to this agent's workdir.
        """
        if self._preset_loader is None:
            raise RuntimeError(
                f"preset loader not composed on {type(self).__name__}; the Agent "
                "wrapper must set _preset_loader"
            )
        wd = working_dir if working_dir is not None else self._working_dir
        return self._preset_loader(name, wd)

    def _activate_preset(self, name: str) -> None:
        """Swap to a named preset — override in subclasses that support presets.

        BaseAgent raises NotImplementedError; Agent (lingtai.agent) overrides
        this with the real implementation.
        """
        raise NotImplementedError(
            f"_activate_preset not supported on {type(self).__name__}"
        )

    def _can_fallback_preset(self) -> bool:
        from .lifecycle import _can_fallback_preset
        return _can_fallback_preset(self)

    def _activate_default_preset(self) -> None:
        """Override hook — Agent subclass implements via _activate_preset(default).
        BaseAgent stub raises NotImplementedError."""
        raise NotImplementedError(
            "_activate_default_preset must be implemented by Agent subclass"
        )

    def _build_launch_cmd(self) -> list[str] | None:
        """Return the command to relaunch this agent. Override in subclasses."""
        return None

    # ------------------------------------------------------------------
    # Tool dispatch (pass-throughs to tools.py)
    # ------------------------------------------------------------------

    def _dispatch_tool(self, tc: ToolCall) -> dict:
        from .tools import _dispatch_tool
        return _dispatch_tool(self, tc)

    def _refresh_tool_inventory_section(self) -> None:
        from .tools import _refresh_tool_inventory_section
        _refresh_tool_inventory_section(self)

    def _build_tool_schemas(self) -> list[FunctionSchema]:
        from .tools import _build_tool_schemas
        return _build_tool_schemas(self)

    def has_capability(self, name: str) -> bool:
        from .tools import _has_capability
        return _has_capability(self, name)

    def _mount_official_tool(self, transaction) -> None:
        """Publish only a registrar-issued canonical official transaction.

        The public ``add_tool`` path cannot publish a statically reserved
        official name. The registrar issues the transaction after binding and
        the Agent adapter records the exact declaration/bound result first;
        this route verifies both identities before trusting any handler/schema.
        A caller-supplied ``BoundToolPlugin`` or forged transaction therefore
        cannot replace the live official surface through this seam.
        """
        from ..tool_plugin import (
            OFFICIAL_TOOL_PLUGIN_NAMES,
            _OFFICIAL_MOUNT_TOKEN,
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
        from .tools import _add_tool
        _add_tool(
            self,
            name,
            schema=dict(plugin.schema),
            handler=plugin.handler,
            description=plugin.description,
            glossary_package=plugin.glossary_package,
            _official_mount_token=_OFFICIAL_MOUNT_TOKEN,
        )
        transaction.mark_mounted(self)

    def add_tool(
        self,
        name: str,
        *,
        schema: dict | None = None,
        handler: Callable[[dict], dict] | None = None,
        description: str = "",
        system_prompt: str = "",
        glossary_package: str | None = None,
    ) -> None:
        from .tools import _add_tool
        _add_tool(self, name, schema=schema, handler=handler, description=description, system_prompt=system_prompt, glossary_package=glossary_package)

    def remove_tool(self, name: str) -> None:
        from .tools import _remove_tool
        _remove_tool(self, name)

    def override_intrinsic(self, name: str) -> Callable[[dict], dict]:
        from .tools import _override_intrinsic
        return _override_intrinsic(self, name)

    # ------------------------------------------------------------------
    # Prompt (pass-throughs to prompt.py)
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        from .prompt import _build_system_prompt
        return _build_system_prompt(self)

    def _build_system_prompt_batches(self) -> list[str]:
        from .prompt import _build_system_prompt_batches
        return _build_system_prompt_batches(self)

    def _flush_system_prompt(self) -> None:
        from .prompt import _flush_system_prompt
        _flush_system_prompt(self)

    def update_system_prompt(
        self, section: str, content: str, *, protected: bool = False
    ) -> None:
        from .prompt import _update_system_prompt
        _update_system_prompt(self, section, content, protected=protected)

    def _check_rules_file(self) -> None:
        from .lifecycle import _check_rules_file
        _check_rules_file(self)

    # ------------------------------------------------------------------
    # Identity / status (pass-throughs to identity.py)
    # ------------------------------------------------------------------

    def _build_manifest(self) -> dict:
        from .identity import _build_manifest
        return _build_manifest(self)

    def status(self) -> dict:
        from .identity import _status
        return _status(self)

    def _write_status_snapshot(self) -> None:
        """Write .status.json — live runtime snapshot consumed by TUI/portal."""
        try:
            atomic_write_json(
                self._working_dir / ".status.json",
                self.status(),
                preserve_existing_mode=True,
            )
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to write .status.json: {e}")

    def _build_agent_record_extra(self) -> dict:
        """Curated ``handles``/``integrations`` blocks for the Agent record.

        Core owns no MCP/integration knowledge, so the base implementation
        contributes nothing; ``lingtai.Agent`` overrides this to safelist
        verified consumer-facing handles (e.g. a Telegram bot username) and
        visible MCP integration labels from ``services.mcp_registry``.
        """
        return {}

    def _write_session_stats_record(self) -> None:
        """Publish the redacted Agent record, throttled by
        LINGTAI_SESSION_STATS_REFRESH_SECONDS (default 5s).

        This is the one atomic/versioned/redacted live personal record every
        LingTai Agent (including avatars) owns — see
        ``lingtai.kernel.session_stats``. Best-effort: a write failure is
        logged and never interrupts the turn.
        """
        from ..session_stats import (
            RecentDaemonSnapshot,
            build_agent_record,
            session_stats_refresh_seconds,
            should_refresh_agent_record,
            write_agent_record,
        )

        try:
            wall_now = self._lifecycle_clock.wall_seconds()
            if not should_refresh_agent_record(
                self._session_stats_last_written_at,
                wall_now,
                session_stats_refresh_seconds(),
            ):
                return
            snapshot_owner = getattr(self, "_daemon_stats_snapshot", None)
            if snapshot_owner is None:
                snapshot_owner = RecentDaemonSnapshot(self._working_dir)
                self._daemon_stats_snapshot = snapshot_owner
            # Never wait for the newest-1000 daemon reads: a blocked storage
            # read must not delay the heartbeat's liveness publication.
            snapshot_owner.schedule()
            self._session_stats_sequence += 1
            record = build_agent_record(
                self,
                sequence=self._session_stats_sequence,
                daemon_summary=snapshot_owner.snapshot(),
            )
            write_agent_record(self._working_dir, record)
            self._session_stats_last_written_at = wall_now
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to write agent record: {e}")

    # ------------------------------------------------------------------
    # Messaging (pass-throughs)
    # ------------------------------------------------------------------

    def mail(self, address: str, message: str, subject: str = "") -> dict:
        from .messaging import _mail
        return _mail(self, address, message, subject)

    def send(self, content: str | dict, sender: str = "user") -> None:
        from .messaging import _send
        _send(self, content, sender)

    def submit_turn(
        self,
        content: str,
        *,
        sender: str = "user",
        correlation_id: str | None = None,
        execution_workspace: str | Path | ExecutionWorkspace | None = None,
        tool_observer: TurnToolObserver | None = None,
        permission_broker: TurnPermissionBroker | None = None,
        origin: TurnOrigin = TurnOrigin.LEGACY,
    ) -> TurnHandle:
        """Queue one text turn and return its protocol-neutral terminal handle."""
        from ..turns import submit_turn
        return submit_turn(
            self,
            content,
            sender=sender,
            correlation_id=correlation_id,
            execution_workspace=execution_workspace,
            tool_observer=tool_observer,
            permission_broker=permission_broker,
            origin=origin,
        )

    def cancel_turn(self, correlation_id: str) -> bool:
        """Request cooperative cancellation for one matching live turn."""
        from ..turns import cancel_turn
        return cancel_turn(self, correlation_id)

    # ------------------------------------------------------------------
    # Session persistence (delegates to SessionManager)
    # ------------------------------------------------------------------

    def get_token_usage(self) -> dict:
        """Return token usage summary (delegates to SessionManager)."""
        if not hasattr(self, "_session"):
            return {
                "input_tokens": 0, "output_tokens": 0,
                "thinking_tokens": 0, "cached_tokens": 0,
                "total_tokens": 0, "api_calls": 0,
                "ctx_system_tokens": 0, "ctx_tools_tokens": 0,
                "ctx_history_tokens": 0, "ctx_total_tokens": 0,
            }
        return self._session.get_token_usage()

    def get_runtime_session_token_usage(self) -> dict:
        """Return RUNTIME-SESSION token usage DELTAS — since last refresh/process start.

        Delegates to :meth:`SessionManager.get_runtime_session_token_usage`.
        "Runtime session" = the live process, counted since it last started or
        refreshed. This is NOT the source of the injected
        ``_meta.agent_meta.agent_state.token_usage.session`` half: that half is "since last
        molt" and reads cumulative :meth:`get_token_usage` totals (which survive
        refresh), so it is not zeroed on refresh. This runtime getter's baseline
        resets on every refresh, so it is used only for since-refresh diagnostics.
        """
        if not hasattr(self, "_session"):
            return {
                "api_calls": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "session_cache_rate": 0.0,
                "avg_input_tokens_per_api_call": 0,
            }
        return self._session.get_runtime_session_token_usage()

    def get_current_session_token_usage(self) -> dict:
        """DEPRECATED compat alias for :meth:`get_runtime_session_token_usage`.

        The ``current_session`` name was ambiguous (it read like "since last
        molt" but always meant "since last refresh"). Retained only for external
        callers; new code must use :meth:`get_runtime_session_token_usage`.
        """
        return self.get_runtime_session_token_usage()

    # ------------------------------------------------------------------
    # Runtime reasoning effort — in-process, self-facing (issue #1197 K1a)
    # ------------------------------------------------------------------
    #
    # These are the agent's own get/set/clear entry points. There is
    # deliberately NO system tool action, file protocol, CLI command, TUI
    # command, or daemon control behind them: this slice is the in-process
    # vertical only. A set/clear affects the next not-yet-captured dispatch —
    # never one already in flight — and never propagates to any daemon.

    def reasoning_effort_status(self) -> dict:
        """Return the current effort route/capability and controller state."""
        return self._session.reasoning_effort_status()

    def set_reasoning_effort(self, value: str):
        """Request a process-local effort override for the next dispatch."""
        return self._session.set_reasoning_effort(value)

    def clear_reasoning_effort(self):
        """Drop the override and restore the route's construction baseline."""
        return self._session.clear_reasoning_effort()

    def runtime_session(self):
        """Return the current RUNTIME-SESSION object (live lifecycle segment).

        No id; a fresh empty object per process start / refresh / restart / molt.
        See :meth:`SessionManager.runtime_session` and
        docs/references/runtime-vs-agent-session-objects.md.
        """
        return self._session.runtime_session()

    def agent_session(self):
        """Return the current AGENT-SESSION object (molt generation), or ``None``.

        Keyed by ``molt_count`` (no new id). Rebuilt from the durable trajectory
        at start/refresh by :meth:`rebuild_agent_session`. ``None`` before the
        first rebuild is installed.
        """
        return self._session.agent_session()

    def rebuild_agent_session(self):
        """(Re)build the AGENT-SESSION for the current ``molt_count`` and install it.

        Uses the optimized rebuild path (indexed ``log.sqlite`` → bounded reverse
        JSONL scan → full scan last resort; see
        :func:`agent_session.rebuild_agent_session_from_events`), so the normal
        case does NOT full-scan a large ``events.jsonl``. The rebuilt since-molt
        aggregate is installed on the session manager so the injected
        ``token_usage.session`` half and other since-molt consumers can read a
        single owner. Returns the rebuilt :class:`AgentSession`.

        Never raises for a missing/empty trajectory — a brand-new agent yields a
        zeroed boot session at the current ``molt_count``.
        """
        from ..agent_session import rebuild_agent_session_from_events

        session = rebuild_agent_session_from_events(
            self._working_dir,
            molt_count=int(getattr(self, "_molt_count", 0) or 0),
            logger_fn=self._log,
        )
        self._session.install_agent_session(session)
        return session

    def get_chat_state(self) -> dict:
        """Serialize current chat session for persistence."""
        return self._session.get_chat_state()

    def restore_chat(self, state: dict) -> None:
        """Restore or create a chat session from saved state."""
        self._session.restore_chat(state)

    def restore_token_state(self, state: dict) -> None:
        """Restore cumulative token counters from a saved session."""
        self._session.restore_token_state(state)

    def _save_chat_history(self, *, ledger_source: str = "main") -> None:
        """Write chat history and token usage to disk (no git commit).

        Called after every completed interaction for crash resilience.
        Git commits are handled by the periodic snapshot system. The persisted
        chat history is intentionally redacted; after process restart, restored
        history likewise contains redacted placeholders rather than raw secrets.

        ``ledger_source`` tags any token-ledger entry written for the
        most recent LLM round-trip. Default ``"main"`` covers the bulk
        of callers. Set to ``"tc_wake"`` from involuntary splice paths
        so consultation cadence does not double-count splices as main turns.
        """
        history_dir = self._working_dir / "history"
        history_dir.mkdir(exist_ok=True)
        try:
            state = self.get_chat_state()
            if state and state.get("messages"):
                redacted_messages = redact_for_trajectory(state["messages"])
                lines = [json.dumps(entry, ensure_ascii=False) for entry in redacted_messages]
                atomic_write_text(history_dir / "chat_history.jsonl", "\n".join(lines) + "\n")
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to save chat history: {e}")
        # Update .agent.json with current state
        try:
            self._workdir.write_manifest(self._build_manifest())
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to update manifest: {e}")
        self._write_status_snapshot()
        self._write_session_stats_record()
        # Append per-call token usage to ledger
        usage, self._last_usage = self._last_usage, None
        if usage is not None:
            try:
                ledger_path = self._working_dir / "logs" / "token_ledger.jsonl"
                model = getattr(self._session, "_model", None) or getattr(self.service, "model", None)
                endpoint = getattr(self.service, "_base_url", None)
                ledger_extra = {"source": ledger_source}
                usage_extra = getattr(usage, "extra", None)
                if isinstance(usage_extra, dict):
                    ledger_extra.update(
                        {k: v for k, v in usage_extra.items() if v is not None}
                    )
                append_token_entry(
                    ledger_path,
                    input=usage.input_tokens,
                    output=usage.output_tokens,
                    thinking=usage.thinking_tokens,
                    cached=usage.cached_tokens,
                    model=model,
                    endpoint=endpoint,
                    extra=ledger_extra,
                )
            except Exception as e:
                logger.warning(f"[{self.agent_name}] Failed to append token ledger: {e}")

    # ------------------------------------------------------------------
    # Hooks (overridable by subclasses)
    # ------------------------------------------------------------------

    def _cpr_agent(self, address: str) -> "BaseAgent | None":
        """Resuscitate a suspended agent at *address*.

        Returns the resuscitated agent, or None if not supported.
        Override in subclasses (e.g. lingtai's Agent) to provide
        full reconstruction from persisted working dir state.
        """
        return None

    def _pre_request(self, msg: Message) -> str:
        """Transform message content before sending to LLM.

        Returns the content string to send.
        """
        return msg.content if isinstance(msg.content, str) else json.dumps(msg.content)

    def _post_request(self, msg: Message, result: dict) -> None:
        """Called after _process_response.

        Override in subclasses for post-processing.
        """
        # Clean up turn-local Telegram Task Card context.
        self._teardown_telegram_task_card()

    def _on_tool_result_hook(
        self,
        tool_name: str,
        tool_args: dict,
        result: dict,
        *,
        tool_call_id: str | None = None,
    ) -> str | None:
        """Hook called after each tool execution.

        If this returns a non-None string, the current request processing
        returns immediately with that string as the result text.

        The automatic Telegram Task Card no longer observes tool completion
        through this hook — it is a mechanical projection of
        ``logs/events.jsonl`` owned by ``TelegramManager`` (see
        ``mcp_servers/telegram/manager.py``), not a turn-local callback model.
        This hook is retained as a subclass override point (see
        ``base_agent/ANATOMY.md``) and currently does nothing.

        Large tool results no longer raise a ``large_tool_result`` system
        notification here.  They are ranked instead through
        ``_meta.agent_meta.agent_state.current_tool_result_chars.top_results`` and digested
        via ``context(action="summarize")`` (see meta_block.current_tool_result_chars).
        """
        return None

    def _setup_telegram_task_card(self) -> None:
        """Maintain retained legacy Telegram route-capture bookkeeping.

        This compatibility hook derives ``(account, chat_id)`` from the first
        Telegram preview's ``message_ref`` and keeps the historical turn-local
        field stable across unchanged notification fingerprints. The retired
        Telegram-owned controller consumed that field; the current intrinsic
        ``task_card`` producer does not. It writes an agent-local file artifact,
        and Telegram independently projects that artifact read-only while its
        automatic slot remains manager-owned.
        """
        from ..notifications import _workdir_key, is_channel_allowed

        store = self._notification_store
        workdir = _workdir_key(self)
        fp = store.fingerprint(
            lambda ch: is_channel_allowed(ch, workdir=workdir)
        )
        last_fp = getattr(self, "_last_telegram_card_fingerprint", None)
        if fp == last_fp:
            return

        notifications = store.snapshot(
            lambda ch: is_channel_allowed(ch, workdir=workdir)
        )
        telegram_data = notifications.get("mcp.telegram")
        if not telegram_data or not isinstance(telegram_data, dict):
            return
        data = telegram_data.get("data", {})
        previews = data.get("previews", []) if isinstance(data, dict) else []
        if not previews:
            return
        first = previews[0] if isinstance(previews, list) and previews else {}
        if not isinstance(first, dict):
            return
        message_ref = first.get("message_ref", "")
        if not message_ref or not isinstance(message_ref, str):
            return
        parts = message_ref.split(":", 2)
        if len(parts) < 2:
            return
        account, chat_id_str = parts[0], parts[1]
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return

        self._telegram_task_card_context = {
            "account": account,
            "chat_id": chat_id,
        }
        self._last_telegram_card_fingerprint = fp

    def _teardown_telegram_task_card(self) -> None:
        """Clear the turn-local Telegram route context.

        Idempotent: safe to call multiple times, including when no route was
        ever captured (direct-answer turn, non-Telegram turn). The automatic
        Task Card itself needs no finalize call here — it is continuously
        re-derived by ``TelegramManager`` from ``logs/events.jsonl`` and keeps
        broadcasting independent of this turn's lifecycle.
        """
        self._telegram_task_card_context = None


    def _maybe_notify_large_tool_result(
        self,
        tool_name: str,
        result: object,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Retained no-op: large tool results no longer raise a notification.

        Large results used to publish a ``source="large_tool_result"`` system
        notification (gated by a total-length threshold) so the agent would be
        reminded to summarize them.  That reminder has been removed: large
        results are surfaced as a ranked list under
        ``_meta.agent_meta.agent_state.current_tool_result_chars.top_results`` (see
        :func:`meta_block.current_tool_result_chars`) and digested via
        ``context(action="summarize")``.  The result still flows into normal
        tool-result history and the char-ranking; it simply creates no
        ``.notification/system.json`` event.

        This method is kept as a stable, overridable seam (subclasses/tests
        may still reference it) but is intentionally inert.
        """
        return None
