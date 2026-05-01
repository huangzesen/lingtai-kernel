# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is 灵台

灵台 (Língtái) is a generic agent framework — an "agent operating system" providing the minimal kernel for AI agents: thinking (LLM), perceiving (vision, search), acting (file I/O), and communicating (inter-agent email). Domain tools, coordination, and orchestration are plugged in from outside via MCP-compatible interfaces.

Named after 灵台方寸山 — where 孙悟空 learned his 72 transformations. Each agent (器灵) can spawn avatars (分身) that venture into 三千世界 and return with experiences. The self-growing network of avatars IS the agent itself — memory becomes infinite through multiplication.

### Two-Package Architecture

This repo contains both packages, published as a single `lingtai` PyPI package:

- **`lingtai_kernel`** (`src/lingtai_kernel/`) — minimal agent runtime. Contains BaseAgent, intrinsics, LLM protocol (ABCs + service), mail/logging services, and core utilities. Zero hard dependencies. Can be used standalone.
- **`lingtai`** (`src/lingtai/`) — batteries-included layer. Depends on `lingtai_kernel`. Provides Agent (capabilities layer), 19 capabilities, LLM adapter implementations, FileIO/Vision/Search services, MCP, CLI, and addons. Re-exports kernel's public API so `from lingtai import BaseAgent` works.

The kernel must never import from `lingtai` — the dependency is strictly one-directional.

## Build & Test

```bash
# Activate the venv (required — lingtai is installed in editable mode)
source venv/bin/activate

# Install in editable mode
pip install -e .

# Run all tests
python -m pytest tests/

# Run a single test file
python -m pytest tests/test_agent.py

# Run a single test
python -m pytest tests/test_agent.py::test_agent_starts_and_stops -v

# Smoke-test after editing a module
python -c "import lingtai"
```

All dependencies (LLM provider SDKs, MCP, search) are required — no optional extras. Never add `[project.optional-dependencies]` to `pyproject.toml`.

## Architecture

### Three-Layer Agent Hierarchy

```
BaseAgent              — kernel (intrinsics, sealed tool surface)
    |
Agent(BaseAgent)   — kernel + capabilities + domain tools
    |
CustomAgent(Agent) — host's wrapper (subclass with domain logic)
```

- **BaseAgent** (`lingtai_kernel.base_agent`) — kernel coordinator (~1200 lines). Constructor takes `service: LLMService` (positional); `agent_name: str | None = None` (keyword-only, optional true name); `working_dir: str | Path` (keyword-only, required — caller provides the full path). `agent_name` is a true name (真名) — set once via `set_name()` or at construction, never changed. `set_name(name)` validates non-empty, set-once semantics, updates manifest and system prompt. 5-state lifecycle (ACTIVE/IDLE/STUCK/ASLEEP/SUSPENDED), message loop, tool dispatch routing, public API, subclass hooks. Delegates to `WorkingDir` (git/filesystem), `SessionManager` (LLM session/tokens), `ToolExecutor` (tool execution). 4 intrinsics wired from `intrinsics/*.py`. `add_tool()`/`remove_tool()` sealed after `start()`. `update_system_prompt()` stays open.
- **Agent** (`src/lingtai/agent.py`) — accepts `capabilities=` (list or dict) at construction. `get_capability(name)` for manager access. Also provides `connect_mcp()` for MCP server integration and auto-creates `LocalFileIOService` if none provided.
- **Custom agents** — subclass Agent, add domain tools via `add_tool()` or `_setup_capability()` in `__init__`.

### Four Services (all optional)

| Service | What it backs | First implementation |
|---------|--------------|---------------------|
| `LLMService` | Core agent loop (thinking) | Adapter registry (kernel) + adapters (lingtai) |
| `FileIOService` | file capabilities (read, edit, write, glob, grep) | `LocalFileIOService` (lingtai) |
| `MailService` | mail (disk-backed mailbox with inbox, send, check, read, search, delete, self-send) | `FilesystemMailService` (kernel) |
| `LoggingService` | structured JSONL event logging (auto-created in working dir) | `JSONLLoggingService` (kernel) |

`LLMService` lives in the kernel with an adapter registry; adapter implementations live in lingtai and register on import. `FileIOService` auto-creates `LocalFileIOService` in Agent (not BaseAgent). `LoggingService` auto-creates `JSONLLoggingService` at `{working_dir}/logs/events.jsonl` if not passed. `VisionService` and `SearchService` are capability-level — passed via `capabilities={"vision": {"vision_service": svc}}`.

### Three-Tier Tool Model

| Tier | What | How added |
|------|------|-----------|
| **Intrinsics** | Kernel services (mail, system, eigen, soul). Mail provides a disk-backed mailbox: send, check, read, search, delete. Self-send (to own address) creates persistent notes that survive context compaction. System provides runtime inspection (`show`), synchronization (`nap` — timed pause), lifecycle (`refresh` — reload MCP, reset session), self-sleep (`sleep`), and karma/nirvana-gated actions (`lull`, `interrupt`, `suspend`, `cpr`, `nirvana`). Eigen provides pad (`edit`/`load` on `system/pad.md`), context management (`molt` for self-compaction with briefing), and naming (`name`/`set` — set true name once, `nickname` — mutable). `context_forget` is internal only (auto-wipe). Covenant is a protected prompt section (no tool access). Capabilities can upgrade intrinsics via `override_intrinsic()`. | Built-in, always present |
| **Capabilities** | Composable capabilities (file [read/write/edit/glob/grep], psyche, library, bash, avatar, email, vision, web_search, web_read, talk, compose, draw, listen, daemon) | Declared at construction via `capabilities=` on Agent |
| **MCP tools** | Domain tools from external MCP servers | Connected via `Agent.connect_mcp()` using `MCPClient` from `services/mcp.py`, or `add_tool()` in subclass constructors |

### Key Modules

- **`lingtai_kernel.base_agent`** — `BaseAgent` class (kernel coordinator, ~1200 lines). Constructor: `BaseAgent(service: LLMService, *, agent_name: str | None = None, working_dir: str | Path, ...)`. Caller provides the full `working_dir` path; `WorkingDir` creates it on disk. `agent_name` is an optional true name (真名) — set once via `set_name(name)` or at construction, never changed. Eigen intrinsic provides `name`/`set` action for self-naming. 5-state lifecycle (ACTIVE/IDLE/STUCK/ASLEEP/SUSPENDED), message loop, 2-layer tool dispatch routing (intrinsics + tools), mail notification pipeline via MailService (messages persisted to disk by MailService, agent notified via `[system]` notification with configurable `_mailbox_name`/`_mailbox_tool`). Messages queued during active work are concatenated into one LLM turn via `_concat_queued_messages`. Public API (`add_tool`, `remove_tool`, `override_intrinsic`, `set_name`, `send`, `mail`). `send()` is fire-and-forget only — all agents are async peers. Subclass hooks (`_pre_request`, `_post_request`, `_on_tool_result_hook`). Delegates to `WorkingDir`, `SessionManager`, `ToolExecutor`. Tool surface sealed after `start()`.
- **`lingtai_kernel.workdir`** — `WorkingDir` class. Agent working directory management: receives full path from caller, creates it via `mkdir(parents=True)`. Exclusive file locking, git init with opt-in tracking, manifest read/write, `diff()` (read-only) and `diff_and_commit()`. No reference to BaseAgent — pure filesystem/subprocess operations.
- **`lingtai_kernel.session`** — `SessionManager` class. LLM session lifecycle: `ensure_session()`, `send()` (single attempt, raises on failure — AED retry is in the message loop), `_rebuild_session()` (create new session with current config, preserving conversation history), context compaction, token tracking, session persistence. No reference to BaseAgent — receives callbacks (`build_system_prompt_fn`, `build_tool_schemas_fn`) at construction.
- **`lingtai_kernel.tool_executor`** — `ToolExecutor` class. Sequential and parallel tool call execution with timing, error handling, guard checks, and intercept hooks. No reference to BaseAgent — receives `dispatch_fn` and `make_tool_result_fn` callbacks.
- **`src/lingtai/agent.py`** — `Agent(BaseAgent)`. Accepts `capabilities=` at construction. Tracks `_capabilities` for avatar replay. `get_capability(name)` returns manager instances.
- **`src/lingtai/state.py`** — `AgentState` enum (ACTIVE, IDLE, STUCK, ASLEEP, SUSPENDED).
- **`src/lingtai/message.py`** — `Message` dataclass (type, sender, content, id, reply_to, timestamp), `_make_message` (auto-prepends UTC timestamp to string content), `MSG_REQUEST`, `MSG_USER_INPUT`. No synchronous reply mechanism — all communication is async.
- **`src/lingtai/services/`** — lingtai services: `file_io.py` (ABC + LocalFileIOService), `vision.py`, `search.py`, `mcp.py`. Kernel services (`mail.py`, `logging.py`) live in `lingtai_kernel.services`.
- **`lingtai_kernel.llm.interface`** — `ChatInterface`, the canonical provider-agnostic conversation history. Single source of truth — adapters rebuild provider formats from this. Content blocks: `TextBlock`, `ToolCallBlock`, `ToolResultBlock`, `ThinkingBlock`, `ImageBlock`.
- **`lingtai_kernel.llm.base`** — `LLMAdapter` (ABC), `ChatSession` (ABC), `LLMResponse`, `ToolCall`, `FunctionSchema`. All agent code depends on these, never on provider SDKs directly.
- **`lingtai_kernel.llm.service`** — `LLMService`. Adapter registry + factory, session registry, one-shot generation gateway, context compaction orchestration. Adapters register via `LLMService.register_adapter()`. Decoupled from config files — uses injected `key_resolver` and `provider_defaults`.
- **`src/lingtai/llm/interface_converters.py`** — Bidirectional converters between `ChatInterface` and provider-specific formats (Anthropic, OpenAI, Gemini).
- **`lingtai_kernel.intrinsics`** — Each file exports `get_schema(lang)`, `get_description(lang)`, and `handle(agent, args)`. All 4 kernel intrinsics (mail, system, eigen, soul) have self-contained handler logic — they receive the agent as an explicit parameter. Mail intrinsic provides a disk-backed mailbox with 5 actions: `send` (fire-and-forget with optional `delay` in seconds — all sends go through outbox → mailman thread → sent pipeline), `check` (list inbox with unread flags), `read` (by ID, non-destructive), `search` (regex), `delete`. Every send writes to `mailbox/outbox/`, spawns a daemon `_mailman` thread that sleeps for the delay, dispatches (filesystem write or self-send), then moves to `mailbox/sent/` with `sent_at` and `status`. Returns `{"status": "sent", "to": addr, "delay": N}` — the agent doesn't know dispatch outcome. `outbox/` (transient) and `sent/` (audit trail) are not exposed to the agent. Messages persist in `mailbox/inbox/{uuid}/message.json` — delivery is a filesystem write to the recipient's inbox directory. System intrinsic provides runtime inspection (`show`), synchronization (`nap` — timed pause, wakes on mail), lifecycle (`refresh` — reload MCP, reset session), self-sleep (`sleep`), and karma/nirvana-gated actions: `lull` (put other to sleep), `interrupt` (cancel other's turn), `suspend` (force other to SUSPENDED), `cpr` (resuscitate SUSPENDED agent), `nirvana` (permanently destroy). Eigen intrinsic provides pad (`edit`/`load` on `system/pad.md`), context management (`molt` for self-compaction with briefing), and naming (`name`/`set` — set true name once, `nickname` — mutable). `context_forget` is internal only (called by auto-wipe). Covenant is injected at construction as a protected prompt section (no tool access).
- **`src/lingtai/capabilities/`** — Each capability module exports `setup(agent, **kwargs)`. 20 built-in: read, write, edit, glob, grep (file I/O — also available as `"file"` group), psyche, codex, library, bash, avatar, email, vision, web_search, web_read, talk, compose, draw, video, listen, daemon. The email capability upgrades the mail intrinsic with reply/reply_all, CC/BCC, contacts, sent/archive folders, archive action (one-way inbox→archive), delete action (inbox or archive), delayed send (`delay` param), private mode, and scheduled recurring sends (`schedule` sub-object with create/cancel/list). Routes per-recipient dispatch through the mail intrinsic's outbox → `_mailman(skip_sent=True)` pipeline, writes one sent record per logical email. Delegates inbox ops to mail intrinsic helpers. The psyche capability upgrades the eigen intrinsic with evolving identity (character), knowledge codex, and `pad.edit` with `files` param for importing codex exports into working notes. `molt` is inherited from eigen. Avatar (分身) spawns `Agent` with `name` + optional `mirror` (deep copy identity). Reasoning sent as first message.
- **`src/lingtai/capabilities/daemon.py`** — Daemon (神識) subagent capability. `DaemonManager` dispatches ephemeral `ChatSession` tool loops in threads via `ThreadPoolExecutor`. Each emanation gets a curated tool surface (parent's capability handlers + MCP tools, minus blacklist). Results delivered as `[daemon:em-N]` messages to parent's inbox. Four actions: emanate (分), list (观), ask (问), reclaim (收). Configurable: `max_emanations`, `max_turns`, `timeout`.
- **`src/lingtai/cli.py`** — CLI entrypoint (`lingtai run <working_dir>`). Reads `init.json` manifest, creates LLMService + Agent, starts the agent loop. Also `lingtai cpr <dir>` for resuscitating suspended agents.
- **`src/lingtai/network.py`** — `AgentNetwork` class. Three-layer topology discovery: avatar edges (from `delegates/ledger.jsonl`), contact edges (from `mailbox/contacts.json`), mail edges (from `mailbox/inbox/` + `mailbox/sent/`). Returns `AgentNode` and edge objects.
- **`src/lingtai/init_schema.py`** — `validate_init()` for init.json manifest validation.
- **`src/lingtai/addons/`** — Optional integrations registered at Agent construction via `addons=` kwarg. `imap/` (IMAP email polling — `IMAPManager`, `IMAPService`, `IMAPAccount`), `telegram/` (Telegram bot — `TelegramManager`, `TelegramService`, `TelegramAccount`).
- **`lingtai_kernel.config`** — `AgentConfig` dataclass. Key fields: `max_turns`, `provider`, `model`, `api_key`, `base_url`, `retry_timeout`, `aed_timeout` (max seconds in STUCK before ASLEEP, default 360), `max_aed_attempts` (default 3), `thinking_budget`, `data_dir`, `soul_delay`, `language`, `stamina` (max uptime before ASLEEP), `context_limit`, `molt_pressure`, `molt_warnings`, `molt_prompt`. Host app injects resolved values; no file-based config inside lingtai.
- **`lingtai_kernel.prompt`** — Builds system prompt from base template + `SystemPromptManager` sections + MCP tool descriptions.
- **`lingtai_kernel.loop_guard`** — `LoopGuard` class. Duplicate tool call tracking, total call limit enforcement, invalid tool detection, escalating warnings (dup_free_passes → warnings → hard block).
- **`lingtai_kernel.handshake`** — `is_agent(path)`, `is_alive(path)`, `is_human(path)`, `manifest(path)` — agent discovery and health checks via filesystem.

### LLM Provider Adapters

5 adapter directories under `src/lingtai/llm/`, each lazy-imported and registered with `LLMService.register_adapter()` on `import lingtai.llm`: Gemini (`google-genai`), OpenAI, Anthropic, MiniMax, Custom. The Custom adapter handles additional providers (DeepSeek, Grok, Qwen, GLM, Kimi) via `api_compat` routing. Each adapter subdirectory has `adapter.py` (implementation) and `defaults.py` (model defaults). LLM protocol ABCs live in `lingtai_kernel.llm`; adapter implementations live in `lingtai.llm`.

### Built-in Capabilities (19)

| Capability | Usage | What it adds |
|-----------|-------|-------------|
| `file` | `capabilities=["file"]` | Group sugar — expands to read, write, edit, glob, grep |
| `read` | `capabilities=["read"]` | Read text file contents via FileIOService |
| `write` | `capabilities=["write"]` | Create or overwrite files via FileIOService |
| `edit` | `capabilities=["edit"]` | Exact string replacement in files via FileIOService |
| `glob` | `capabilities=["glob"]` | Find files by glob pattern via FileIOService |
| `grep` | `capabilities=["grep"]` | Search file contents by regex via FileIOService |
| `psyche` | `capabilities=["psyche"]` | Upgrades eigen intrinsic — evolving identity (character), knowledge library, `memory.edit` with `files` param imports library exports into working notes. Molt inherited from eigen. |
| `library` | `capabilities=["library"]` | Knowledge library management for psyche |
| `bash` | `capabilities={"bash": {"policy_file": "p.json"}}` or `{"bash": {"yolo": True}}` | Shell command execution with policy |
| `avatar` | `capabilities=["avatar"]` | Spawn avatar (分身) as fully independent detached process. Two params: `name` (required, true name) and `type` ('shallow' default — copies init.json only, 投胎; 'deep' — copies character/memory/library too, 二重身). Each avatar gets its own working dir + `lingtai run` process. Survives parent death. Reasoning = starting prompt in init.json. |
| `email` | `capabilities=["email"]` | Upgrades mail intrinsic with reply/reply_all, CC/BCC, contacts, sent/archive folders, archive (inbox→archive), delete (inbox/archive), delayed send (`delay`), private mode, and scheduled recurring sends (`schedule` sub-object with create/cancel/list). Routes dispatch through outbox → `_mailman(skip_sent=True)`. Sets `_mailbox_name="email box"`, `_mailbox_tool="email"`. Delegates inbox ops to mail intrinsic helpers. |
| `vision` | `capabilities=["vision"]` or `{"vision": {"vision_service": svc}}` | Image understanding (LLM multimodal or dedicated VisionService) |
| `web_search` | `capabilities=["web_search"]` or `{"web_search": {"search_service": svc}}` | Web search (LLM grounding or dedicated SearchService) |
| `web_read` | `capabilities=["web_read"]` | Read and extract content from web pages |
| `talk` | `capabilities=["talk"]` | Text-to-speech via MiniMax MCP |
| `compose` | `capabilities=["compose"]` | Music generation via MiniMax MCP |
| `draw` | `capabilities=["draw"]` | Text-to-image via MiniMax MCP |
| `video` | `capabilities=["video"]` or `{"video": {"provider": "minimax"}}` | Video generation via MiniMax MCP. Text-to-video and image-to-video (via `first_frame_image`). Director models support camera movement instructions in prompts. Output: MP4 saved to media/videos/. |
| `listen` | `capabilities=["listen"]` | Speech transcription + music analysis |
| `daemon` | `capabilities=["daemon"]` or `{"daemon": {"max_emanations": 10}}` | Subagent system (分神). Dispatch ephemeral LLM sessions as parallel workers in the same working dir. Actions: emanate (分, dispatch batch), list (观, status), ask (问, follow-up), reclaim (收, kill all). Results return as `[daemon:em-N]` notifications. MCP tools auto-inherited. Blacklist: daemon, avatar, psyche, library. |

### Extension Pattern

```python
# Layer 2: Agent with capabilities
agent = Agent(
    service=svc, agent_name="alice", working_dir="/agents/alice",
    capabilities=["file", "vision", "web_search", "bash"],  # "file" expands to read/write/edit/glob/grep
)
agent = Agent(
    service=svc, agent_name="bob", working_dir="/agents/bob",
    capabilities={"bash": {"policy_file": "p.json"}},   # dict form (with kwargs)
)

# Layer 3: Custom agent subclass
class ResearchAgent(Agent):
    def __init__(self, **kwargs):
        super().__init__(capabilities=["file", "vision", "web_search"], **kwargs)
        self._setup_capability("bash", policy_file="research.json")
        self.add_tool("query_db", schema={...}, handler=db_handler)

# Low-level API (on BaseAgent, sealed after start)
agent.add_tool(name, schema=schema, handler=handler)     # register tool
agent.remove_tool(name)                                   # unregister tool
agent.override_intrinsic(name)                            # remove intrinsic, return handler
agent.update_system_prompt(section, content)              # inject prompt section (open at any time)
```

Note: `capabilities=` accepts `list[str]` (no kwargs) or `dict[str, dict]` (with kwargs per capability). Group names like `"file"` expand to individual capabilities. `add_tool()`, `remove_tool()`, and `override_intrinsic()` raise `RuntimeError` after `start()`.

### System Prompt Structure

Base prompt (minimal — identity and general guidance only) → Sections (injected by host/capabilities via `update_system_prompt`) → MCP tool descriptions (auto-generated). Protected sections cannot be modified by the LLM's `eigen` intrinsic.

**Do not put tool pipelines or tool-specific instructions in the system prompt.** Pipelines (e.g., "mail admin first, then sleep") belong in tool schema descriptions where the LLM sees them in context. The system prompt should stay minimal.

## Conventions

- Python 3.11+, `from __future__ import annotations` used throughout.
- Dataclasses preferred over dicts for structured data.
- No file-based config inside lingtai — all config injected via constructor args.
- All services optional — missing service auto-disables backed intrinsics.
- Provider SDKs lazy-imported — only active provider needs installation.
- Tests use `unittest.mock.MagicMock` for LLM service mocking. Test functions follow `test_<what_is_tested>` naming.
- Migrations should be complete and clean — remove old code entirely. No backward-compatibility shims, no deprecated wrappers, no legacy aliases unless the user explicitly asks for them.
- Kernel must never import from `lingtai` — dependency is strictly one-directional.
- All imports in lingtai use `from lingtai_kernel.xxx import ...` for kernel types.
