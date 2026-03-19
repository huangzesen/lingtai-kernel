# CLAUDE.md — stoai-kernel

Minimal agent kernel — think, communicate, remember, host tools.

## What is stoai-kernel

The kernel of the stoai agent framework, extracted as a standalone package. Provides the minimal runtime for AI agents: LLM thinking, inter-agent mail, memory (eigen), tool dispatch, and working directory management. No capabilities, no file I/O tools, no MCP, no multimodal — just the core.

## Build & Test

```bash
# Create and activate venv
python -m venv venv && source venv/bin/activate

# Install in editable mode
pip install -e .

# Run tests
python -m pytest tests/ -v
```

Zero hard dependencies. Optional LLM provider SDKs are lazy-imported.

## Architecture

### BaseAgent (kernel coordinator)

`base_agent.py` — ~900 lines. 2-state lifecycle (SLEEPING/ACTIVE), message loop, tool dispatch, mail notification pipeline. Delegates to `WorkingDir`, `SessionManager`, `ToolExecutor`.

### Three Intrinsics

| Intrinsic | What it does |
|-----------|-------------|
| `mail` | Disk-backed mailbox: send, check, read, search, delete |
| `system` | Runtime inspection, synchronization, lifecycle (show, shutdown, wait) |
| `eigen` | Memory (edit/load `system/memory.md`), context management (molt) |

### LLM Protocol

`llm/base.py` defines `LLMAdapter` (ABC) and `ChatSession` (ABC). `llm/service.py` provides `LLMService` with adapter registry — adapters register via `LLMService.register_adapter()`. No built-in adapters; consumers (like `stoai`) register their own.

### Services

| Service | ABC | Default impl |
|---------|-----|-------------|
| Mail | `MailService` | `TCPMailService` |
| Logging | `LoggingService` | `JSONLLoggingService` |

### Key Modules

- `base_agent.py` — Kernel coordinator
- `session.py` — LLM session lifecycle
- `tool_executor.py` — Sequential/parallel tool execution
- `workdir.py` — Git-backed agent filesystem
- `prompt.py` — System prompt builder
- `llm/interface.py` — `ChatInterface`, canonical provider-agnostic conversation history

## Conventions

- Python 3.11+, `from __future__ import annotations` throughout
- Zero hard dependencies — provider SDKs lazy-imported
- All services optional — missing service disables backed intrinsics
- Kernel must never import from `stoai` — dependency is one-directional

## Extension

```python
from stoai_kernel import BaseAgent
from stoai_kernel.llm import LLMService, LLMAdapter

# Register a custom adapter
LLMService.register_adapter("my_provider", lambda **kw: MyAdapter(**kw))

# Build on the kernel directly
class MinimalBot(BaseAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.add_tool("greet", schema={...}, handler=greet_handler)
```
