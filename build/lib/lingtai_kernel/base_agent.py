"""
BaseAgent — generic agent kernel with intrinsic tools and capability dispatch.

Key concepts:
    - **5-state lifecycle**: ACTIVE, IDLE, STUCK, ASLEEP, SUSPENDED.
    - **Persistent LLM session**: each agent keeps its chat session across messages.
    - **2-layer tool dispatch**: intrinsics (built-in) + capability handlers.
    - **Opaque context**: the host app can pass any context object — the agent
      stores it but never introspects it.
    - **4 optional services**: LLM, FileIO, Mail, Logging —
      missing service auto-disables the intrinsics it backs.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import AgentConfig
from .state import AgentState
from .workdir import WorkingDir
from .message import Message, _make_message, MSG_REQUEST, MSG_USER_INPUT
from .intrinsics import ALL_INTRINSICS
from .prompt import SystemPromptManager
from .llm import (
    FunctionSchema,
    LLMService,
    ToolCall,
)
from .i18n import t as _t
from .logging import get_logger
from .loop_guard import LoopGuard
from .prompt import build_system_prompt
from .session import SessionManager
from .tool_executor import ToolExecutor
from .token_ledger import append_token_entry, sum_token_ledger
from .types import UnknownToolError

logger = get_logger()


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class BaseAgent:
    """Generic research agent with intrinsic tools and MCP tool dispatch.

    Services (all optional):
        - ``service`` (LLMService): The brain — thinking, generating text.
        - ``file_io`` (FileIOService): File access — backs read/edit/write/glob/grep.
        - ``mail_service`` (MailService): Message transport — backs mail intrinsic.

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

    def __init__(
        self,
        service: LLMService,
        *,
        agent_name: str | None = None,
        working_dir: str | Path,
        file_io: Any | None = None,
        mail_service: Any | None = None,
        config: AgentConfig | None = None,
        context: Any = None,
        admin: dict | None = None,
        streaming: bool = False,
        covenant: str = "",
        principle: str = "",
        procedures: str = "",
        brief: str = "",
        memory: str = "",
        comment: str = "",
    ):
        self.agent_name = agent_name  # true name (真名) — immutable once set
        self.nickname: str | None = None  # mutable alias (别名)
        self.service = service
        self._config = config or AgentConfig()
        self._context = context
        self._admin = admin or {}
        self._cancel_event = threading.Event()
        self._state = AgentState.IDLE
        self._started_at: str = ""
        self._last_usage = None  # UsageMetadata from last LLM call, for ledger
        self._created_at: str = ""
        self._uptime_anchor: float | None = None  # set in start(), None means not started

        # Working directory (caller-owned path)
        self._workdir = WorkingDir(working_dir)
        self._working_dir = self._workdir.path

        # LoggingService: always JSONL in working dir
        from .services.logging import JSONLLoggingService
        log_dir = self._working_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        self._log_service = JSONLLoggingService(
            log_dir / "events.jsonl",
            ensure_ascii=self._config.ensure_ascii,
        )

        # Acquire working directory lock (10s grace for prior process cleanup)
        self._workdir.acquire_lock(timeout=10)

        # --- Wire services ---
        # FileIOService: optional, provided by Agent or host
        self._file_io = file_io

        # MailService: None means mail intrinsic disabled
        self._mail_service = mail_service

        # Set by psyche capability to prevent stop() from overwriting memory.md
        self._eigen_owns_memory = False

        # Covenant, principle, procedures, brief, and memory file paths
        system_dir = self._working_dir / "system"
        memory_file = system_dir / "memory.md"
        covenant_file = system_dir / "covenant.md"
        principle_file = system_dir / "principle.md"
        procedures_file = system_dir / "procedures.md"
        brief_file = system_dir / "brief.md"

        system_dir.mkdir(exist_ok=True)

        # Covenant: constructor value wins, then fall back to file on disk
        if covenant:
            covenant_file.write_text(covenant)
        elif covenant_file.is_file():
            covenant = covenant_file.read_text()

        # Principle: constructor value wins, then fall back to file on disk
        # (mirrors covenant — ensures principle survives molts even when
        # init.json omits it, and seeds the mirror on first construction).
        if principle:
            principle_file.write_text(principle)
        elif principle_file.is_file():
            principle = principle_file.read_text()

        # Procedures: same pattern as covenant/principle
        if procedures:
            procedures_file.write_text(procedures)
        elif procedures_file.is_file():
            procedures = procedures_file.read_text()

        # Brief: externally-maintained context (written by secretary agent).
        # init.json value only seeds the file if it doesn't exist yet;
        # once the secretary writes brief.md, the disk version always wins.
        if brief and not brief_file.is_file():
            brief_file.write_text(brief)
        elif brief_file.is_file():
            brief = brief_file.read_text()

        # Memory: constructor value seeds the file if it doesn't exist
        if memory and not memory_file.is_file():
            memory_file.write_text(memory)

        # Auto-load memory from file into prompt manager
        loaded_memory = ""
        if memory_file.is_file():
            loaded_memory = memory_file.read_text()

        # System prompt manager
        self._prompt_manager = SystemPromptManager()
        if principle:
            self._prompt_manager.write_section("principle", principle, protected=True)
        if covenant:
            self._prompt_manager.write_section("covenant", covenant, protected=True)
        if procedures:
            self._prompt_manager.write_section("procedures", procedures, protected=True)
        if brief:
            self._prompt_manager.write_section("brief", brief, protected=True)
        # Load existing rules from system/rules.md (survives molts, refreshes, and resumes)
        rules_md = system_dir / "rules.md"
        if rules_md.is_file():
            try:
                rules_content = rules_md.read_text().strip()
                if rules_content:
                    self._prompt_manager.write_section("rules", rules_content, protected=True)
            except OSError:
                pass
        if loaded_memory.strip():
            self._prompt_manager.write_section("memory", loaded_memory)
        if comment:
            self._prompt_manager.write_section("comment", comment)

        # Soul delay — needed before manifest build
        self._soul_delay = max(1.0, self._config.soul_delay)

        # Agent ID, created_at, and molt_count — persistent state restored
        # from the existing manifest on disk if present (resume path), or
        # freshly initialized otherwise. All three must be set BEFORE the
        # manifest write below, because _build_manifest() reads them and
        # write_manifest() will overwrite the old file.
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

        # Write manifest — identity + construction recipe (no runtime state)
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest_data = self._build_manifest()
        self._workdir.write_manifest(manifest_data)

        # Auto-inject identity into system prompt from manifest
        import json as _json
        self._prompt_manager.write_section(
            "identity", _json.dumps(manifest_data, indent=2, ensure_ascii=False), protected=True
        )

        # Post to billboard — ephemeral discovery index at ~/.lingtai/billboard/
        self._billboard_path: Path | None = None
        try:
            billboard_dir = Path.home() / ".lingtai" / "billboard"
            billboard_dir.mkdir(parents=True, exist_ok=True)
            self._billboard_path = billboard_dir / f"{self._working_dir.name}.json"
            import json as _json, os as _os
            tmp = self._billboard_path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(manifest_data, indent=2, ensure_ascii=False))
            _os.replace(str(tmp), str(self._billboard_path))
        except OSError:
            self._billboard_path = None

        self._nap_wake = threading.Event()  # signalled to wake nap early
        self._nap_wake_reason = ""  # why the nap was woken

        # Mailbox identity — capabilities override these to change notification text.
        # _mailbox_name: human label ("mail box", "email box", "gmail box")
        # _mailbox_tool: tool name for check/read instructions ("mail", "email", "gmail")
        self._mailbox_name = "mail box"
        self._mailbox_tool = "mail"

        # Non-intrinsic tool handlers (capabilities, MCP, add_tool)
        self._tool_handlers: dict[str, Callable[[dict], dict]] = {}
        self._tool_schemas: list[FunctionSchema] = []


        # --- Wire intrinsic tools ---
        self._intrinsics: dict[str, Callable[[dict], dict]] = {}
        self._wire_intrinsics()

        # Inbox
        self.inbox: queue.Queue[Message] = queue.Queue()

        # Lifecycle
        self._shutdown = threading.Event()
        self._asleep = threading.Event()   # set when entering ASLEEP; cleared on wake
        self._thread: threading.Thread | None = None
        self._idle = threading.Event()
        self._idle.set()
        self._state = AgentState.IDLE
        self._sealed = False

        # Soul — inner voice
        # soul_delay initialized earlier (before manifest build).
        # Inquiry: on-demand one-shot, independent of flow.
        self._soul_prompt = ""       # non-empty during inquiry
        self._soul_oneshot = False    # True during pending inquiry
        self._soul_timer: threading.Timer | None = None
        self._insight_turn_counter: int = 0

        # Heartbeat — always-on health monitor
        self._heartbeat: float = 0.0
        self._heartbeat_thread: threading.Thread | None = None
        self._aed_start: float | None = None

        # Snapshot — periodic git commits (Time Machine)
        self._last_snapshot: float = 0.0
        self._last_gc: float = 0.0

        # Session manager — LLM session, token tracking, compaction
        self._session = SessionManager(
            llm_service=service,
            config=self._config,
            agent_name=agent_name,
            streaming=streaming,
            build_system_prompt_fn=self._build_system_prompt,
            build_tool_schemas_fn=self._build_tool_schemas,
            logger_fn=self._log,
        )

    # ------------------------------------------------------------------
    # Intrinsic wiring
    # ------------------------------------------------------------------

    def _wire_intrinsics(self) -> None:
        """Wire kernel intrinsic tool handlers."""
        for name, info in ALL_INTRINSICS.items():
            handle_fn = info["module"].handle
            self._intrinsics[name] = lambda args, fn=handle_fn: fn(self, args)

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
    def _chat(self) -> Any:
        """Proxy to SessionManager's chat session.

        Many parts of the codebase (intrinsics, capabilities, psyche)
        read ``self._chat`` directly — this property keeps them working.
        """
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
    # Naming
    # ------------------------------------------------------------------

    def set_name(self, name: str) -> None:
        """Set the agent's true name (真名). Immutable once set."""
        if not name:
            raise ValueError("Agent name cannot be empty.")
        if self.agent_name is not None:
            raise RuntimeError(
                f"True name already set ({self.agent_name!r}). "
                f"True names are immutable. Use set_nickname() instead."
            )
        self.agent_name = name
        self._update_identity()

    def set_nickname(self, nickname: str) -> None:
        """Set or change the agent's nickname (别名). Mutable."""
        self.nickname = nickname or None
        self._update_identity()

    def _update_identity(self) -> None:
        """Write manifest and update identity section in system prompt."""
        self._workdir.write_manifest(self._build_manifest())
        import json as _json
        self._prompt_manager.write_section(
            "identity", _json.dumps(self._build_manifest(), indent=2, ensure_ascii=False), protected=True
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the agent's main loop thread."""
        self._sealed = True
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()

        # Initialize git repo in working directory (first start only)
        self._workdir.init_git()

        # Capture startup time for uptime tracking
        from datetime import datetime, timezone
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._uptime_anchor = time.monotonic()

        # Export assembled system prompt to system/system.md
        self._flush_system_prompt()

        # Restore chat session and token state from filesystem if available
        chat_history_file = self._working_dir / "history" / "chat_history.jsonl"
        if chat_history_file.is_file():
            try:
                messages = [json.loads(line) for line in chat_history_file.read_text().splitlines() if line.strip()]
                self.restore_chat({"messages": messages})
                self._log("session_restored")
            except Exception as e:
                logger.warning(f"[{self.agent_name}] Failed to restore chat history: {e}")
        # Restore token state from ledger (lifetime accumulator)
        try:
            ledger_path = self._working_dir / "logs" / "token_ledger.jsonl"
            totals = sum_token_ledger(ledger_path)
            self.restore_token_state(totals)
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to restore token state from ledger: {e}")

        # Start MailService listener if configured
        if self._mail_service is not None:
            try:
                self._mail_service.listen(on_message=lambda payload: self._on_mail_received(payload))
            except RuntimeError:
                pass  # Already listening — that's fine

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"agent-{self.agent_name or self._working_dir.name}",
        )
        self._thread.start()
        self._start_heartbeat()

    def _reset_uptime(self) -> None:
        """Reset the uptime anchor for stamina tracking (used on wake from asleep)."""
        self._uptime_anchor = time.monotonic()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown and wait for the agent thread to exit."""
        self._log("agent_stop")
        self._stop_heartbeat()
        self._cancel_soul_timer()
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._session.close()

        # Stop MailService if configured
        if self._mail_service is not None:
            try:
                self._mail_service.stop()
            except Exception:
                pass

        # Close LoggingService if configured
        if self._log_service is not None:
            try:
                self._log_service.close()
            except Exception:
                pass

        # Persist memory from prompt manager to file
        if not self._eigen_owns_memory:
            memory_content = self._prompt_manager.read_section("memory") or ""
            memory_file = self._working_dir / "system" / "memory.md"
            if memory_file.is_file() or memory_content:
                memory_file.parent.mkdir(exist_ok=True)
                memory_file.write_text(memory_content)

        # Remove billboard entry
        if self._billboard_path and self._billboard_path.is_file():
            try:
                self._billboard_path.unlink()
            except OSError:
                pass

        # Persist final state and release lock
        self._workdir.write_manifest(self._build_manifest())
        self._workdir.release_lock()

    def _on_mail_received(self, payload: dict) -> None:
        """Callback for MailService — route incoming mail to inbox.

        This method is never replaced — it is the stable entry point for all
        incoming mail. Lifecycle control (interrupt, sleep, lull, cpr, nirvana)
        is handled by the system intrinsic via signal files, not mail.
        """
        self._on_normal_mail(payload)

    def _on_normal_mail(self, payload: dict) -> None:
        """Handle a normal mail — notify agent via inbox.

        The message is already persisted to mailbox/inbox/ by MailService.
        This method signals arrival and sends a uniform push notification.
        Capabilities configure ``_mailbox_name`` and ``_mailbox_tool``
        to change the notification text (e.g. "email box" / "email").
        """
        from uuid import uuid4

        email_id = payload.get("_mailbox_id") or str(uuid4())
        address = payload.get("from", "unknown")
        identity = payload.get("identity")
        name = address
        if identity and identity.get("agent_name"):
            name = identity["agent_name"]
        subject = payload.get("subject", "(no subject)")
        message = payload.get("message", "")
        sent_at = payload.get("sent_at") or payload.get("time") or ""

        self._wake_nap("mail_arrived")

        if len(message) > 500:
            preview = message[:500].replace("\n", " ") + f"... ({len(message) - 500} more chars)"
        else:
            preview = message.replace("\n", " ")
        notification = _t(
            self._config.language, "system.new_mail",
            box=self._mailbox_name, address=address, name=name, subject=subject,
            sent_at=sent_at, preview=preview, tool=self._mailbox_tool,
        )

        self._log("mail_received", address=address, name=name, subject=subject, message=message)
        msg = _make_message(MSG_REQUEST, "system", notification)
        self.inbox.put(msg)

    # ------------------------------------------------------------------
    # Public addon API
    # ------------------------------------------------------------------

    @property
    def working_dir(self) -> Path:
        """The agent's working directory (read-only)."""
        return self._working_dir

    def wake(self, reason: str) -> None:
        """Wake the agent from nap. Call when external input arrives."""
        self._wake_nap(reason)

    def log(self, event_type: str, **fields) -> None:
        """Write a structured event to the agent's event log."""
        self._log(event_type, **fields)

    def notify(self, sender: str, text: str) -> None:
        """Put a system notification into the agent's inbox.

        This is the primary way addons inform the agent about external events.
        The message appears in the agent's conversation as a system message.
        """
        msg = _make_message(MSG_REQUEST, sender, text)
        self.inbox.put(msg)

    def _set_state(self, new_state: AgentState, reason: str = "") -> None:
        """Transition to a new state."""
        old = self._state
        if old == new_state:
            return
        self._state = new_state
        if new_state == AgentState.ACTIVE:
            self._idle.clear()
            self._cancel_soul_timer()
        else:
            self._idle.set()
            if new_state == AgentState.IDLE:
                self._start_soul_timer()
        self._log("agent_state", old=old.value, new=new_state.value, reason=reason)
        self._workdir.write_manifest(self._build_manifest())

    def _start_soul_timer(self) -> None:
        """Start the soul delay timer for flow or pending inquiry."""
        if not self._soul_oneshot and self._soul_delay > self._config.stamina:
            return  # delay exceeds stamina — effectively disabled
        if self._shutdown.is_set():
            return
        self._cancel_soul_timer()
        self._soul_timer = threading.Timer(self._soul_delay, self._soul_whisper)
        self._soul_timer.daemon = True
        self._soul_timer.name = f"soul-{self.agent_name or self._working_dir.name}"
        self._soul_timer.start()

    def _cancel_soul_timer(self) -> None:
        """Cancel any pending soul timer."""
        if self._soul_timer is not None:
            self._soul_timer.cancel()
            self._soul_timer = None

    def _wake_nap(self, reason: str) -> None:
        """Signal the nap to wake up with a given reason."""
        self._nap_wake_reason = reason
        self._nap_wake.set()

    def _soul_whisper(self) -> None:
        """Called by soul timer — flow mode only. Inquiry is sync via tool handler."""
        self._soul_timer = None
        try:
            from .intrinsics.soul import soul_flow, _save_soul_session
            result = soul_flow(self)
            if result:
                voice = result["voice"]
                self._log("soul_whisper", length=len(voice))
                self._persist_soul_entry(result)
                _save_soul_session(self)
                prefix = _t(self._config.language, "soul.flow_prefix")
                msg = _make_message(MSG_REQUEST, "soul", f"{prefix} {voice}")
                self.inbox.put(msg)
        except Exception as e:
            self._log("soul_whisper_error", error=str(e))

    def _persist_soul_entry(self, result: dict, mode: str = "flow", source: str = "agent") -> None:
        """Append a soul entry to the appropriate log file.

        Flow entries go to logs/soul_flow.jsonl.
        Inquiry entries go to logs/soul_inquiry.jsonl.
        """
        from datetime import datetime, timezone
        filename = f"soul_{mode}.jsonl"
        soul_file = self._working_dir / "logs" / filename
        soul_file.parent.mkdir(exist_ok=True)
        entry = json.dumps({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": mode,
            "source": source,
            "prompt": result["prompt"],
            "thinking": result["thinking"],
            "voice": result["voice"],
        }, ensure_ascii=False)
        with open(soul_file, "a") as f:
            f.write(entry + "\n")

    def _run_inquiry(self, question: str, source: str = "agent") -> None:
        """Run soul.inquiry and log result as insight event."""
        try:
            from .intrinsics.soul import soul_inquiry
            result = soul_inquiry(self, question)
            if result:
                self._log("insight", text=result["voice"], question=question, source=source)
                self._persist_soul_entry(result, mode="inquiry", source=source)
            else:
                self._log("insight", text="(silence)", question=question, source=source)
        except Exception as e:
            self._log("insight_error", error=str(e)[:200], question=question)

    # ------------------------------------------------------------------
    # Heartbeat — always-on health monitor (involuntary)
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start the heartbeat daemon thread."""
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name=f"heartbeat-{self.agent_name or self._working_dir.name}",
        )
        self._heartbeat_thread.start()
        self._log("heartbeat_start")

    def _stop_heartbeat(self) -> None:
        """Stop the heartbeat (called only by stop/shutdown)."""
        thread = self._heartbeat_thread
        self._heartbeat_thread = None  # signals the loop to exit
        if thread is not None:
            thread.join(timeout=5.0)
        hb_file = self._working_dir / ".agent.heartbeat"
        try:
            hb_file.unlink(missing_ok=True)
        except OSError:
            pass
        self._log("heartbeat_stop", heartbeat=self._heartbeat)

    def _heartbeat_loop(self) -> None:
        """Beat every 1 second. AED if agent is STUCK."""
        while self._heartbeat_thread is not None and not self._shutdown.is_set():
            self._heartbeat = time.time()

            # Write heartbeat file in ALL living states (everything except SUSPENDED)
            try:
                hb_file = self._working_dir / ".agent.heartbeat"
                hb_file.write_text(str(self._heartbeat))
            except OSError:
                pass

            # --- signal file detection ---
            interrupt_file = self._working_dir / ".interrupt"
            if interrupt_file.is_file():
                try:
                    interrupt_file.unlink()
                except OSError:
                    pass
                self._cancel_event.set()
                self._log("interrupt_received", source="signal_file")

            # .suspend = SUSPENDED (full process death, external only)
            suspend_file = self._working_dir / ".suspend"
            if suspend_file.is_file():
                try:
                    suspend_file.unlink()
                except OSError:
                    pass
                self._cancel_event.set()
                self._set_state(AgentState.SUSPENDED, reason="suspend signal")
                self._shutdown.set()
                self._log("suspend_received", source="signal_file")

            # .sleep = ASLEEP (sleep, listeners stay alive)
            sleep_file = self._working_dir / ".sleep"
            if sleep_file.is_file():
                try:
                    sleep_file.unlink()
                except OSError:
                    pass
                self._cancel_event.set()
                self._set_state(AgentState.ASLEEP, reason="sleep signal")
                self._asleep.set()
                self._log("sleep_received", source="signal_file")

            # .prompt = inject text input as [system] message
            prompt_file = self._working_dir / ".prompt"
            if prompt_file.is_file():
                try:
                    content = prompt_file.read_text().strip()
                except OSError:
                    content = ""
                try:
                    prompt_file.unlink()
                except OSError:
                    pass
                if content:
                    self.send(content, sender="system")
                    self._log("prompt_received", source="signal_file")

            # .inquiry = soul inquiry (from TUI /btw or auto-insight)
            # Format: first line = source, rest = question.
            # If no newline, entire content is question with source "human".
            # Flow: .inquiry → .inquiry.taken (processing) → deleted (done)
            inquiry_file = self._working_dir / ".inquiry"
            taken_file = self._working_dir / ".inquiry.taken"
            if inquiry_file.is_file() and not taken_file.is_file():
                try:
                    inquiry_file.rename(taken_file)
                except OSError:
                    pass
                else:
                    try:
                        content = taken_file.read_text().strip()
                    except OSError:
                        content = ""
                    if content:
                        lines = content.split("\n", 1)
                        if len(lines) == 2 and lines[0] in ("human", "insight", "agent"):
                            source, question = lines[0], lines[1].strip()
                        else:
                            source, question = "human", content.strip()
                        if question:
                            def _inquiry_done(q: str, s: str, tf: Path) -> None:
                                self._run_inquiry(q, source=s)
                                try:
                                    tf.unlink()
                                except OSError:
                                    pass
                            threading.Thread(
                                target=_inquiry_done,
                                args=(question, source, taken_file),
                                daemon=True,
                            ).start()
                        else:
                            try:
                                taken_file.unlink()
                            except OSError:
                                pass
                    else:
                        try:
                            taken_file.unlink()
                        except OSError:
                            pass

            # .rules = network rules signal (consumed → persisted to system/rules.md)
            self._check_rules_file()

            # Stamina enforcement — asleep when stamina expires
            if self._uptime_anchor is not None and self._state not in (AgentState.ASLEEP, AgentState.SUSPENDED):
                elapsed = time.monotonic() - self._uptime_anchor
                if elapsed >= self._config.stamina:
                    self._log("stamina_expired", elapsed=round(elapsed, 1), stamina=self._config.stamina)
                    self._cancel_event.set()
                    self._set_state(AgentState.ASLEEP, reason="stamina expired")
                    self._asleep.set()

            if self._state == AgentState.STUCK:
                now = time.monotonic()
                if self._aed_start is None:
                    self._aed_start = now
                if now - self._aed_start > self._config.aed_timeout:
                    self._log("aed_timeout", seconds=now - self._aed_start)
                    self._set_state(AgentState.ASLEEP, reason="AED timeout")
                    self._save_chat_history()
                    self._asleep.set()
            else:
                self._aed_start = None

            # Periodic snapshot — every 5 minutes
            now_mono = time.monotonic()
            if now_mono - self._last_snapshot >= 300:  # 5 minutes
                self._workdir.snapshot()
                self._last_snapshot = now_mono

            # Periodic GC — every 24 hours
            if now_mono - self._last_gc >= 86400:  # 24 hours
                self._workdir.gc()
                self._last_gc = now_mono

            time.sleep(1.0)

    def _log(self, event_type: str, **fields) -> None:
        """Write a structured event to the logging service, if configured."""
        if self._log_service:
            self._log_service.log({
                "type": event_type,
                "address": self._working_dir.name,
                "agent_name": self.agent_name,
                "ts": time.time(),
                **fields,
            })

    # ------------------------------------------------------------------
    # Main loop (final — do not override)
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Wait for messages, process them. Agent persists between messages."""
        while True:
            while not self._shutdown.is_set():
                # --- Asleep: soul off, wait for inbox message ---
                if self._asleep.is_set():
                    self._cancel_soul_timer()
                    self._log("sleep")

                    # Block until a message arrives or shutdown
                    msg = None
                    while not self._shutdown.is_set():
                        try:
                            msg = self.inbox.get(timeout=1.0)
                            break
                        except queue.Empty:
                            continue

                    if msg is None:
                        break  # shutdown was set — exit inner loop

                    # Wake up
                    self._asleep.clear()
                    self._cancel_event.clear()  # clear stale sleep/stamina signal
                    self._set_state(AgentState.ACTIVE, reason=f"woke from asleep: {msg.type}")
                    self._log("wake", trigger=msg.type)
                    self._reset_uptime()
                    msg = self._concat_queued_messages(msg)
                    # Fall through to handle the message below
                else:
                    try:
                        msg = self.inbox.get(timeout=self._inbox_timeout)
                    except queue.Empty:
                        continue
                    msg = self._concat_queued_messages(msg)
                    self._set_state(AgentState.ACTIVE, reason=f"received {msg.type}")

                sleep_state = AgentState.IDLE
                aed_attempts = 0
                while True:
                    try:
                        self._handle_message(msg)
                        break  # success (chat saved after each session.send inside)
                    except Exception as e:
                        err_desc = str(e) or repr(e)
                        aed_attempts += 1

                        # Pop orphan tool call from interface (idempotent)
                        if self._session.chat is not None:
                            self._session.chat.interface.pop_orphan_tool_call()

                        if aed_attempts > self._config.max_aed_attempts:
                            logger.error(
                                f"[{self.agent_name}] AED exhausted after {aed_attempts - 1} attempts: {err_desc}",
                            )
                            self._log("aed_exhausted", attempts=aed_attempts - 1, error=err_desc)
                            sleep_state = AgentState.ASLEEP
                            self._asleep.set()
                            break

                        self._set_state(AgentState.STUCK, reason=f"AED attempt {aed_attempts}: {err_desc}")
                        self._log("aed_attempt", attempt=aed_attempts, error=err_desc)
                        logger.warning(
                            f"[{self.agent_name}] AED attempt {aed_attempts}/{self._config.max_aed_attempts}: {err_desc}",
                        )

                        # Rebuild session with current config, preserving history
                        if self._session.chat is not None:
                            self._session._rebuild_session(self._session.chat.interface)

                        # Inject recovery message
                        from datetime import datetime, timezone
                        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        aed_msg = _t(self._config.language, "system.stuck_revive", ts=ts, tool_calls=err_desc)
                        msg = _make_message(MSG_REQUEST, "system", aed_msg)

                if not self._asleep.is_set():
                    self._set_state(sleep_state)
                self._save_chat_history()

                # Auto-insight: fire after N turns
                if self._config.insights_interval > 0:
                    self._insight_turn_counter += 1
                    if self._insight_turn_counter >= self._config.insights_interval:
                        self._insight_turn_counter = 0
                        from .i18n import t as _ti
                        self._run_inquiry(
                            _ti(self._config.language, "insight.auto_question"),
                            source="auto",
                        )

            break

    def _perform_refresh(self) -> None:
        """Refresh = self-suspend + deferred relaunch.

        1. Launch a detached process that sleeps 3 seconds then runs
           ``lingtai run <dir>``
        2. Touch .suspend — the heartbeat loop picks it up and cleanly
           kills this process via the normal suspend path

        The 3-second delay is enough for the old process to die.
        No two agent processes overlap on the same working directory.
        """
        import subprocess, sys
        self._log("refresh_start")
        self._save_chat_history()
        cmd = self._build_launch_cmd()
        if cmd is None:
            self._log("refresh_no_launch_cmd")
            return

        working_dir = self._working_dir
        (working_dir / ".refresh").touch()

        # Deferred relaunch: wait for lock file to disappear (old process
        # died), then start new process. Polls every 0.5s, gives up after
        # 60s to avoid orphaned relaunch scripts.
        lock_path = str(working_dir / ".agent.lock")
        relaunch_script = (
            "import time, subprocess, os, sys\n"
            f"lock = {lock_path!r}\n"
            "deadline = time.time() + 60\n"
            "time.sleep(1)\n"  # brief grace period
            "while os.path.exists(lock) and time.time() < deadline:\n"
            "    time.sleep(0.5)\n"
            "if os.path.exists(lock):\n"
            "    sys.exit(1)\n"  # timed out — do not relaunch
            "time.sleep(0.5)\n"  # extra settle time after lock gone
            f"subprocess.Popen({cmd!r},\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL, start_new_session=True)\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", relaunch_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._log("refresh_deferred_relaunch", cmd=cmd[0])

        # Self-suspend — heartbeat loop picks this up cleanly
        (working_dir / ".suspend").touch()

    def _build_launch_cmd(self) -> list[str] | None:
        """Return the command to relaunch this agent. Override in subclasses."""
        return None

    def _concat_queued_messages(self, msg: Message) -> Message:
        """Drain any additional queued messages and concatenate into one.

        If nothing else is queued, returns the original message unchanged.
        Otherwise, joins all message contents with blank lines and returns
        a new merged message. Non-string content is converted via str().
        """
        extra: list[Message] = []
        while True:
            try:
                queued = self.inbox.get_nowait()
            except queue.Empty:
                break
            extra.append(queued)

        if not extra:
            return msg

        all_msgs = [msg] + extra
        parts = [m.content if isinstance(m.content, str) else str(m.content)
                 for m in all_msgs]
        merged_content = "\n\n".join(parts)
        merged = _make_message(MSG_REQUEST, msg.sender, merged_content)
        self._log("messages_concatenated", count=len(all_msgs))
        return merged

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_message(self, msg: Message) -> None:
        """Route message by type. Subclasses may override for routing."""
        if msg.type in (MSG_REQUEST, MSG_USER_INPUT):
            self._handle_request(msg)
        else:
            logger.warning(f"[{self.agent_name}] Unknown message type: {msg.type}")

    def _handle_request(self, msg: Message) -> None:
        """Send request to LLM, process response with tool calls."""
        from datetime import datetime, timezone

        max_calls, dup_free, dup_hard = self._get_guard_limits()
        guard = LoopGuard(
            max_total_calls=max_calls,
            dup_free_passes=dup_free,
            dup_hard_block=dup_hard,
        )
        self._executor = ToolExecutor(
            dispatch_fn=self._dispatch_tool,
            make_tool_result_fn=lambda name, result, **kw: self.service.make_tool_result(
                name, result, provider=self._config.provider, **kw
            ),
            guard=guard,
            known_tools=set(self._intrinsics) | set(self._tool_handlers),
            parallel_safe_tools=self._PARALLEL_SAFE_TOOLS,
            logger_fn=self._log,
        )
        content = self._pre_request(msg)
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Molt pressure — warn agent when context is getting full
        # Needs eigen intrinsic (always present) or psyche capability to self-molt
        has_molt = "eigen" in self._intrinsics or self.has_capability("psyche")
        pressure = self._session.get_context_pressure()
        if pressure >= self._config.molt_pressure and has_molt:
            max_warnings = self._config.molt_warnings
            self._session._compaction_warnings += 1
            warnings = self._session._compaction_warnings
            remaining = max(0, max_warnings - warnings)
            lang = self._config.language
            if warnings > max_warnings:
                # Auto-forget — agent ignored all warnings
                self._log("auto_forget", reason=f"ignored {max_warnings} molt warnings", pressure=pressure)
                from .intrinsics import eigen as _eigen
                _eigen.context_forget(self)
                self._session._compaction_warnings = 0
                content = f"{_t(lang, 'system.molt_wiped')}\n\n{content}"
            else:
                # Warning ladder: level 1, 2, 3 based on accumulated warnings.
                # Clamp to level 3 in case molt_warnings is configured >3.
                # The molt procedure is in the system prompt (## procedures),
                # so warnings only need urgency framing.
                level = min(warnings, 3)
                level_prompt = _t(
                    lang,
                    f"system.molt_warning_level{level}",
                    pressure=f"{pressure:.0%}",
                    remaining=remaining,
                )
                # User's custom molt_prompt (if set) wins over the default ladder
                molt_prompt = self._config.molt_prompt or level_prompt
                status = f"[context: {pressure:.0%} | {remaining}/{max_warnings}]"
                content = f"{molt_prompt}\n{status}\n\n{content}"

        content = f"{_t(self._config.language, 'system.current_time', time=current_time)}\n\n{content}"
        self._log("text_input", text=content)
        response = self._session.send(content)
        self._last_usage = response.usage
        self._save_chat_history()
        result = self._process_response(response)
        self._post_request(msg, result)

    def _get_guard_limits(self) -> tuple[int, int, int]:
        """Return (max_total_calls, dup_free_passes, dup_hard_block).

        Uses config.max_turns as the basis.
        """
        max_turns = self._config.max_turns
        return (max_turns, 2, 8)

    # ------------------------------------------------------------------
    # Response processing
    # ------------------------------------------------------------------

    def _process_response(self, response: LLMResponse) -> dict:
        """Handle tool calls and collect text output.

        Returns a result dict: {"text": ..., "failed": ..., "errors": [...]}.
        """
        # Clear any stale cancel event from a previous silence.
        self._cancel_event.clear()
        guard = self._executor.guard
        collected_text_parts: list[str] = []
        collected_errors: list[str] = []

        while True:
            if response.text:
                collected_text_parts.append(response.text)
                self._log("diary", text=response.text)
                if response.tool_calls:
                    self._intermediate_text_streamed = False

            if response.thoughts:
                for thought in response.thoughts:
                    self._log("thinking", text=thought)

            if not response.tool_calls:
                break

            if self._cancel_event.is_set():
                self._cancel_event.clear()
                return {"text": "", "failed": False, "errors": []}

            stop_reason = guard.check_limit(len(response.tool_calls))
            if stop_reason:
                break

            invalid_reason = guard.check_invalid_tool_limit()
            if invalid_reason:
                break

            # Delegate to ToolExecutor
            tool_results, intercepted, intercept_text = self._executor.execute(
                response.tool_calls,
                on_result_hook=self._on_tool_result_hook,
                cancel_event=self._cancel_event,
                collected_errors=collected_errors,
            )

            if intercepted:
                if tool_results and self._chat:
                    self._chat.commit_tool_results(tool_results)
                return {
                    "text": intercept_text,
                    "failed": False,
                    "errors": [],
                }

            guard.record_calls(len(response.tool_calls))

            # Break on repeated identical errors
            if (
                len(collected_errors) >= 2
                and collected_errors[-1] == collected_errors[-2]
            ):
                logger.warning(
                    "[%s] Same error repeated, breaking early: %s",
                    self.agent_name,
                    collected_errors[-1],
                )
                break

            response = self._session.send(tool_results)
            self._last_usage = response.usage
            self._save_chat_history()

        final_text = "\n".join(collected_text_parts)
        has_errors = bool(collected_errors)
        no_useful_output = not final_text.strip()
        return {
            "text": final_text,
            "failed": has_errors and no_useful_output,
            "errors": collected_errors,
        }

    # ------------------------------------------------------------------
    # Tool dispatch — 2-layer
    # ------------------------------------------------------------------

    def _dispatch_tool(self, tc: ToolCall) -> dict:
        """Dispatch a tool call to the appropriate handler.

        Layer 1: intrinsics (built-in tools)
        Layer 2: MCP handlers (domain tools)

        Raises UnknownToolError if the tool name is not found.
        """
        if tc.name in self._intrinsics:
            return self._intrinsics[tc.name](tc.args or {})
        elif tc.name in self._tool_handlers:
            return self._tool_handlers[tc.name](tc.args or {})
        else:
            raise UnknownToolError(tc.name)

    # ------------------------------------------------------------------
    # LLM communication
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build the system prompt from base + sections + tool inventory."""
        # Build tool inventory from full tool descriptions
        lang = self._config.language
        lines = []
        for name in self._intrinsics:
            info = ALL_INTRINSICS.get(name)
            if info:
                lines.append(f"### {name}\n{info['module'].get_description(lang)}")
        for s in self._tool_schemas:
            if s.description:
                lines.append(f"### {s.name}\n{s.description}")
        if lines:
            self._prompt_manager.write_section(
                "tools", "\n\n".join(lines), protected=True
            )
        return build_system_prompt(prompt_manager=self._prompt_manager, language=self._config.language)

    def _build_tool_schemas(self) -> list[FunctionSchema]:
        """Build the complete tool schema list for the LLM.

        Every tool gets a 'reasoning' parameter injected — the agent must
        explain why it's calling this tool. Reasoning is logged as part of
        the agent's diary and stripped before the handler runs.
        """
        reasoning_prop = {
            "reasoning": {
                "type": "string",
                "description": _t(self._config.language, "tool.reasoning_description"),
            },
        }

        schemas = []

        # Intrinsic schemas
        lang = self._config.language
        for name in self._intrinsics:
            info = ALL_INTRINSICS.get(name)
            if info:
                params = dict(info["module"].get_schema(lang))
                props = dict(params.get("properties", {}))
                props.update(reasoning_prop)
                params["properties"] = props
                schemas.append(
                    FunctionSchema(
                        name=name,
                        description=info["module"].get_description(lang),
                        parameters=params,
                    )
                )

        # Capability + MCP schemas — inject reasoning into each
        for s in self._tool_schemas:
            params = dict(s.parameters)
            props = dict(params.get("properties", {}))
            props.update(reasoning_prop)
            params["properties"] = props
            schemas.append(
                FunctionSchema(
                    name=s.name,
                    description=s.description,
                    parameters=params,
                )
            )

        return schemas

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _build_manifest(self) -> dict:
        """Build the manifest dict for .agent.json.

        Subclasses override to add fields (e.g. capabilities).
        Contains everything the agent knows about itself.
        address is always the current working_dir (hot-refreshed on every write).
        Must not depend on _session or _chat — called during __init__.
        """
        data = {
            "agent_id": self._agent_id,
            "agent_name": self.agent_name,
            "nickname": self.nickname,
            "address": self._working_dir.name,
            "created_at": self._created_at,
            "started_at": self._started_at,
            "admin": self._admin,
            "language": self._config.language,
            "stamina": self._config.stamina,
            "state": self._state.value,
            "soul_delay": self._soul_delay,
            "molt_count": self._molt_count,
        }
        if self._mail_service is not None and self._mail_service.address:
            data["address"] = self._mail_service.address
        return data

    def mail(self, address: str, message: str, subject: str = "") -> dict:
        """Send a message to another agent (public API). Requires MailService."""
        return self._intrinsics["mail"]({"action": "send", "address": address, "message": message, "subject": subject})

    def has_capability(self, name: str) -> bool:
        """Check if a capability is registered. Subclasses override."""
        return False

    def add_tool(
        self,
        name: str,
        *,
        schema: dict | None = None,
        handler: Callable[[dict], dict] | None = None,
        description: str = "",
        system_prompt: str = "",
    ) -> None:
        """Register a dynamic tool."""
        if self._sealed:
            raise RuntimeError("Cannot modify tools after start()")
        if handler is not None:
            self._tool_handlers[name] = handler
        if schema is not None:
            # Remove any existing schema with same name
            self._tool_schemas = [s for s in self._tool_schemas if s.name != name]
            self._tool_schemas.append(
                FunctionSchema(
                    name=name,
                    description=description,
                    parameters=schema,
                    system_prompt=system_prompt,
                )
            )
        # Update the live session's tools if one exists
        if self._chat is not None:
            self._chat.update_tools(self._build_tool_schemas())
        self._token_decomp_dirty = True

    def remove_tool(self, name: str) -> None:
        """Unregister a dynamic tool."""
        if self._sealed:
            raise RuntimeError("Cannot modify tools after start()")
        self._tool_handlers.pop(name, None)
        self._tool_schemas = [s for s in self._tool_schemas if s.name != name]
        if self._chat is not None:
            self._chat.update_tools(self._build_tool_schemas())
        self._token_decomp_dirty = True

    def override_intrinsic(self, name: str) -> Callable[[dict], dict]:
        """Remove an intrinsic and return its handler for delegation.

        Called by capabilities that upgrade an intrinsic (email → mail,
        psyche → eigen). Must be called before start() (tool surface sealed).

        Returns the original handler so the capability can delegate to it.
        """
        if self._sealed:
            raise RuntimeError("Cannot modify tools after start()")
        handler = self._intrinsics.pop(name)  # raises KeyError if missing
        self._token_decomp_dirty = True
        return handler

    def _check_rules_file(self) -> None:
        """Consume .rules signal file, diff against system/rules.md, update if changed."""
        rules_file = self._working_dir / ".rules"
        if not rules_file.is_file():
            return
        try:
            content = rules_file.read_text().strip()
        except OSError:
            return
        # Always consume the signal file
        try:
            rules_file.unlink()
        except OSError:
            return
        if not content:
            return
        # Diff against canonical system/rules.md
        canonical = self._working_dir / "system" / "rules.md"
        existing = ""
        if canonical.is_file():
            try:
                existing = canonical.read_text().strip()
            except OSError:
                pass
        if content == existing:
            return
        # Content changed — persist and refresh
        try:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text(content)
        except OSError:
            self._log("rules_write_error", source="signal")
            return
        self._prompt_manager.write_section("rules", content, protected=True)
        self._flush_system_prompt()
        self._log("rules_loaded", source="signal")

    def _flush_system_prompt(self) -> None:
        """Rebuild system prompt, persist to system/system.md, update live session."""
        prompt = self._build_system_prompt()
        system_md = self._working_dir / "system" / "system.md"
        system_md.parent.mkdir(exist_ok=True)
        system_md.write_text(prompt)
        if self._chat is not None:
            self._chat.update_system_prompt(prompt)

    def update_system_prompt(
        self, section: str, content: str, *, protected: bool = False
    ) -> None:
        """Update a named section of the system prompt.

        Args:
            section: Section name.
            content: Section content.
            protected: If True, the LLM cannot overwrite this section.
        """
        self._prompt_manager.write_section(section, content, protected=protected)
        self._token_decomp_dirty = True
        self._flush_system_prompt()

    def _cpr_agent(self, address: str) -> "BaseAgent | None":
        """Resuscitate a suspended agent at *address*.

        Returns the resuscitated agent, or None if not supported.
        Override in subclasses (e.g. lingtai's Agent) to provide
        full reconstruction from persisted working dir state.
        """
        return None

    def send(
        self,
        content: str | dict,
        sender: str = "user",
    ) -> None:
        """Send a message to the agent (fire-and-forget).

        Args:
            content: Message content.
            sender: Message sender.
        """
        msg = _make_message(MSG_REQUEST, sender, content)
        self.inbox.put(msg)
        self._wake_nap("message_received")

    # ------------------------------------------------------------------
    # Session persistence (delegates to SessionManager)
    # ------------------------------------------------------------------

    def get_chat_state(self) -> dict:
        """Serialize current chat session for persistence."""
        return self._session.get_chat_state()

    def restore_chat(self, state: dict) -> None:
        """Restore or create a chat session from saved state."""
        self._session.restore_chat(state)

    def restore_token_state(self, state: dict) -> None:
        """Restore cumulative token counters from a saved session."""
        self._session.restore_token_state(state)

    def _save_chat_history(self) -> None:
        """Write chat history and token usage to disk (no git commit).

        Called after every completed interaction for crash resilience.
        Git commits are handled by the periodic snapshot system.
        """
        history_dir = self._working_dir / "history"
        history_dir.mkdir(exist_ok=True)
        try:
            state = self.get_chat_state()
            if state and state.get("messages"):
                lines = [json.dumps(entry, ensure_ascii=False) for entry in state["messages"]]
                (history_dir / "chat_history.jsonl").write_text("\n".join(lines) + "\n")
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to save chat history: {e}")
        # Update .agent.json with current state
        try:
            self._workdir.write_manifest(self._build_manifest())
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to update manifest: {e}")
        # Write .status.json — live runtime snapshot (same as system("show"))
        try:
            (self._working_dir / ".status.json").write_text(
                json.dumps(self.status(), ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.warning(f"[{self.agent_name}] Failed to write .status.json: {e}")
        # Append per-call token usage to ledger (capture-and-null to avoid race
        # with heartbeat thread calling _save_chat_history on AED timeout)
        usage, self._last_usage = self._last_usage, None
        if usage is not None:
            try:
                ledger_path = self._working_dir / "logs" / "token_ledger.jsonl"
                append_token_entry(
                    ledger_path,
                    input=usage.input_tokens,
                    output=usage.output_tokens,
                    thinking=usage.thinking_tokens,
                    cached=usage.cached_tokens,
                )
            except Exception as e:
                logger.warning(f"[{self.agent_name}] Failed to append token ledger: {e}")


    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return live runtime status — written to .status.json, returned by system("show").

        Contains identity, runtime metrics, and token/context usage.
        Must only be called after _session exists (not during __init__).
        """
        from datetime import datetime, timezone

        mail_addr = None
        if self._mail_service is not None and self._mail_service.address:
            mail_addr = self._mail_service.address

        uptime = time.monotonic() - self._uptime_anchor if self._uptime_anchor is not None else 0.0
        stamina_left = max(0.0, self._config.stamina - uptime) if self._uptime_anchor is not None else None

        usage = self.get_token_usage()

        window_size = None
        usage_pct = None
        if self._chat is not None:
            try:
                # Use configured context_limit if set, otherwise model default
                window_size = self._config.context_limit or self._chat.context_window()
                ctx_total = usage["ctx_total_tokens"]
                usage_pct = round(ctx_total / window_size * 100, 1) if window_size else 0.0
            except Exception:
                pass

        return {
            "identity": {
                "address": str(self._working_dir),
                "agent_name": self.agent_name,
                "mail_address": mail_addr,
            },
            "runtime": {
                "current_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "started_at": self._started_at,
                "uptime_seconds": round(uptime, 1),
                "stamina": self._config.stamina,
                "stamina_left": round(stamina_left, 1) if stamina_left is not None else None,
            },
            "tokens": {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "thinking_tokens": usage["thinking_tokens"],
                "cached_tokens": usage["cached_tokens"],
                "total_tokens": usage["total_tokens"],
                "api_calls": usage["api_calls"],
                "estimated": self._session._token_fallback_warned,
                "context": {
                    "system_tokens": usage["ctx_system_tokens"],
                    "tools_tokens": usage["ctx_tools_tokens"],
                    "history_tokens": usage["ctx_history_tokens"],
                    "total_tokens": usage["ctx_total_tokens"],
                    "window_size": window_size,
                    "usage_pct": usage_pct,
                },
            },
        }

    # ------------------------------------------------------------------
    # Hooks (overridable by subclasses)
    # ------------------------------------------------------------------

    def _pre_request(self, msg: Message) -> str:
        """Transform message content before sending to LLM.

        Returns the content string to send.
        """
        return msg.content if isinstance(msg.content, str) else json.dumps(msg.content)

    def _post_request(self, msg: Message, result: dict) -> None:
        """Called after _process_response.

        Override in subclasses for post-processing.
        """

    def _on_tool_result_hook(
        self, tool_name: str, tool_args: dict, result: dict
    ) -> str | None:
        """Hook called after each tool execution.

        If this returns a non-None string, the current request processing
        returns immediately with that string as the result text.
        """
        return None

