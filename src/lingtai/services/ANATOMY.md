---
related_files:
  - docs/references/licc-notification-wake-runbook.md
  - setup.py
  - src/lingtai/ANATOMY.md
  - src/lingtai/services/__init__.py
  - src/lingtai/services/file_io.py
  - src/lingtai/services/daemon.py
  - src/lingtai/services/file_io_sidecar.py
  - src/lingtai/tools/file/ANATOMY.md
  - src/lingtai/services/mail.py
  - src/lingtai/services/mcp.py
  - src/lingtai/services/session_mcp.py
  - src/lingtai/agent.py
  - src/lingtai/tools/daemon/__init__.py
  - src/lingtai/tools/daemon/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - tests/test_cli_daemon.py
  - src/lingtai/services/mcp_registry.py
  - src/lingtai/services/mcp_inbox.py
  - src/lingtai/services/mcp_licc.py
  - tests/test_session_mcp.py
  - src/lingtai/services/plugin_registry.py
  - src/lingtai/tools/plugin/ANATOMY.md
  - src/lingtai/tools/plugin/settings.py
  - src/lingtai/services/LICC_NOTIFICATION_CONTRACT.md
  - src/lingtai/services/vision/ANATOMY.md
  - src/lingtai/services/websearch/ANATOMY.md
  - src/lingtai/adapters/posix/ANATOMY.md
  - src/lingtai/kernel/mail_transport/ANATOMY.md
  - src/lingtai/kernel/services/ANATOMY.md
  - tests/test_mcp_closed_resource_restart.py
  - ENVIRONMENT_VARIABLES.md
  - tests/test_mcp_structured_result.py
  - tests/test_mcp_sdk_v2_contract.py
  - tests/test_mcp_capability.py
  - tests/test_mcp_v2_adapter_metadata.py
  - src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/reference/mcp-protocol.md
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# src/lingtai/services/

Root services package — pluggable backends for intrinsic tools and MCP clients.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|---|---|---|
| `__init__.py` | 1 | Docstring-only package marker |
| `daemon.py` | — | `DaemonService(state_root)`: standalone, non-Agent composition of the existing `DaemonManager` and `DaemonFamilyDispatcher`; native calls require direct preset paths and readback uses ledger-driven mutation-free views |
| `file_io.py` | 533 | `FileIOService` facade contract + `FileIOBackend`/`LocalFileIOBackend` — backs read/edit/write/glob/grep. `grep` accepts an optional basename `glob_filter` that prunes the candidate set before stat/read |
| `file_io_sidecar.py` | 771 | Rust-backed grep/glob: `RustFileIOBackend`, `SidecarAdapter`, `SidecarError`, plus the `resolve_sidecar_binary` resolver and the `default_file_io_service` factory used by `Agent.__init__`. The factory retains an immutable File construction snapshot: normalized backend selection plus the applied canonical/legacy override source and resolved value; the sensitive value is excluded from repr and fully redacted by owner SHOW. `grep`'s `glob_filter` is applied as a Python-side basename post-filter (the sidecar wire protocol carries no glob field yet) |
| `mail.py` | 19 | High-level compatibility surface: re-exports the Core `MailTransportPort` as `MailService` and the POSIX adapter as both `PosixFilesystemMailAdapter` and the legacy public name `FilesystemMailService` |
| `mcp.py` | 1130 | `MCPClient` (stdio) + `HTTPMCPClient` (streamable HTTP) — async-to-sync bridges over the official MCP Python SDK v2 `mcp.Client`, with shared schema-aware host-private argument preparation (`mcp.py:32-88`), tool-record adaptation, rich-result preservation, and the legacy structured-result projection |
| `session_mcp.py` | — | Process-local stdio MCP overlay ownership for driving sessions: starts/lists every client before one collision-checked publication, rebuilds an existing managed provider session against its preserved canonical interface when the live tool surface changes, returns an idempotent lease, restores the prior tool surface on failure, and removes only handler/route/schema identities still owned by that lease before closing children in reverse order |
| `mcp_registry.py` | — | MCP registry infrastructure (the non-tool half of the `lingtai/tools/mcp` capability): record schema (`validate_record`), JSONL registry I/O (`read_registry`, `_append_record`), catalog loader (`_load_catalog`, path constant recomputed for this location, publicly exposed as `load_catalog` — a deep-copied read), secret-safe identity projection (`read_identities`, `IDENTITY_SAFE_ACCOUNT_KEYS`), boot-time addon decompression (`decompress_addons`), and the system-prompt XML renderer (`_build_registry_xml`). Consumed by the `lingtai/tools/mcp` tool slice (lazy import) and `agent.py` — `Agent._load_mcp_from_workdir`'s `_resolve_curated_launch` calls `load_catalog()` directly (never the registry's decompressed-at-addon-time record) to build a curated init.json entry's complete stdio launcher |
| `mcp_inbox.py` | — | LICC v1 filesystem inbox poller plus Core projection; in-process publication receives the agent and uses its injected Notification Store while the external inbox path/envelope stays unchanged (`src/lingtai/services/mcp_inbox.py:373-395`). |
| `mcp_licc.py` | — | LICC v1 client producer (`push_inbox_event`); imports contract constants from `mcp_inbox.py` |
| `plugin_registry.py` | — | Agent Plugins v1.0.0 discovery **and registration** infrastructure (the non-tool half of the `lingtai/tools/plugin` capability, `src/lingtai/tools/plugin/ANATOMY.md`): §4.1 path containment (`resolve_contained`), manifest validation against the pinned `PLUGIN_SCHEMA_URL` and the v1.0.0 `name` grammar (`validate_manifest`), component discovery through the one server gate (`_scan_skills`, `resolve_server_spec`, `_scan_mcp_servers`), per-plugin and per-path scanning (`read_plugin`, `scan_plugin_root`, `read_plugins`), boot-time registration of declared plugins (`declared_plugin_paths`, `to_registry_record`, `prune_plugin_records`, `register_plugins`), and the `<registered_plugin>` system-prompt XML renderer (`_build_registry_xml`). The registration snapshot preserves folded authored roots as `configured_declared` separately from operational `declared`, which may include the automatic workdir root; Plugin's redacted SHOW provider consumes only the former. Stdlib only, spawns nothing, and makes no network call — `$schema` values are compared as opaque version identifiers, never fetched. It writes in exactly one place, at boot only: `mcp_registry.jsonl` lines stamped `source="plugin:<name>"`, appended through `mcp_registry.validate_record` — the same gate curated addons pass. Consumed by the `lingtai/tools/plugin` tool slice (lazy import) and by `Agent._register_declared_plugins` |
| `LICC_NOTIFICATION_CONTRACT.md` | — | The LICC notification two-lane projection contract governing curated IM producers; live diagnosis and recovery are documented in `docs/references/licc-notification-wake-runbook.md` |

**Sub-packages (not covered here):** `vision/` (7 provider files), `websearch/` (6 provider files).
**Sibling crates:** `crates/lingtai-search-sidecar/` (Rust) — opt-in binary that backs `RustFileIOBackend`. Not required for install/tests.

## Connections

- **→ `lingtai.kernel.logging.get_logger`** (mcp.py:16) — structured logging.
- **→ `lingtai.kernel.mail_transport`** (`mail.py:9`) — re-exports the Core-owned Port as `MailService`.
- **→ `lingtai.adapters.posix.mail`** (`mail.py:10-13`) — re-exports the production adapter under its canonical name and the legacy public `FilesystemMailService` alias.
- **→ `mcp.Client`**, **`mcp.client.stdio`**, **`mcp.client.streamable_http`**, **`httpx2`** — official MCP Python SDK v2 (`mcp>=2,<3`, protocol `2026-07-28`) plus the `httpx2` client the HTTP transport requires. Imported lazily inside the async connect methods.
- **← `lingtai.tools.vision`** — uses `services.vision.VisionService`.
- **← `lingtai.tools.web_search`** — uses `services.websearch.SearchService`.
- **← `lingtai.tools.file`** — the unified File family uses `FileIOService`
  (injected as `agent._file_io`) for its five operations and receives the
  factory-applied bounded construction snapshot for SHOW through a separate
  immutable host configuration port; the sensitive value is projected only
  through full redaction.
- **← `lingtai.cli_daemon`** — thin command driver over `DaemonService`; it owns no daemon policy or Agent facade.
- **→ `lingtai.tools.daemon`** — `DaemonService` composes the existing manager/family dispatcher and artifacts rather than implementing a second engine.
- **← `lingtai.agent` / `lingtai.tools.daemon`** — stdio, HTTP, and task-scoped MCP handlers call `prepare_mcp_tool_arguments` immediately before provider dispatch, using the server's original input schema as authority.

## Composition

`daemon.py` is the reusable standalone composition root: its only durable owner is the caller-selected state root, native configuration comes from each task’s direct preset path, and it never constructs or leases an Agent. `file_io.py` is a pure stdlib abstraction layer. `LocalFileIOService` is the tool-facing facade while `LocalFileIOBackend` owns the default Python local filesystem implementation. `file_io_sidecar.py` provides `RustFileIOBackend`, an opt-in alternative backend that delegates `read`/`write`/`edit` to a private `LocalFileIOBackend` but routes `grep`/`glob` to the Rust binary under `crates/lingtai-search-sidecar/` via short-lived JSON subprocess calls. `mail.py` is a high-level compatibility re-export across the Core Port and POSIX Adapter; it owns no implementation. `mcp.py` keeps two transport-specific client classes and composes them with one protocol-generic result decoder and one schema-aware host-private argument adapter shared by Agent and task-daemon handlers. `session_mcp.py` composes only the existing stdio client into an ephemeral, Agent-surface lease; it writes no registry or persistent configuration.

## State

- **`SessionMCPLease`**: owns an immutable mapping of tool names to the exact client/handler/schema identities it published plus the started client list; close is lock-protected and idempotent, unpublishes only still-owned identities, and closes children in reverse order.
- **`MCPClient` / `HTTPMCPClient`**: each instance manages a background daemon thread, an asyncio event loop (`_loop`), a first-class SDK v2 `Client` (`_client`) whose entered value is held as `_session`, the last preserved typed result (`_last_result`), and a 50-entry activity log. `HTTPMCPClient` additionally owns the `httpx2.AsyncClient` (`_http_client`) it constructs, enters before the `Client`, and exits after it. Thread-safe via `threading.Lock` and `threading.Event`.
- **`LocalFileIOService`**: facade over a `_backend`; exposes `last_traversal` from the backend for tool metadata.
- **`LocalFileIOBackend`**: default Python local filesystem backend; state is optional `_root` plus `last_traversal`.
- **`RustFileIOBackend`**: holds an embedded `LocalFileIOBackend` (for read/write/edit), a `SidecarAdapter` (subprocess client), and a `last_traversal` rebuilt from each sidecar envelope.
- **`SidecarAdapter`**: either pins a strict explicit/environment-selected binary path or re-resolves automatic packaged/dev-tree sources per call; one subprocess per `call()`.
- **`FileIOService` / `FileIOBackend` ABCs**: pure interfaces, no state.

## Notes

- `MCPClient` wraps the official `stdio_client` transport (subprocess) in a first-class SDK v2 `mcp.Client`; `HTTPMCPClient` wraps `streamable_http_client` (the v1 `streamablehttp_client` alias is gone) over an `httpx2.AsyncClient` this module owns and closes, because v2 moved headers/timeouts onto that client. Both expose identical `call_tool()` / `list_tools()` / `close()` API.
- **Negotiation is the SDK's, not LingTai's.** Both clients use the default `mode="auto"`: the SDK probes `server/discover` and falls back to the pre-2026 `initialize` handshake, so one client speaks to 2026 and legacy servers alike. The negotiated facts are re-exposed read-only as `protocol_version`, `server_info`, `server_capabilities`, and `instructions` rather than being discarded at connect. LingTai owns no version constant, comparison, or gate.
- Lazy start: both clients auto-connect on first `call_tool()`.
- **Complete tool catalog:** `_list_all_tools` pages `tools/list` with `cursor=` until `next_cursor is None`, so a paging server's full surface is returned. `_tool_record` keeps the v2 `input_schema` under the historical `schema` key and carries the remaining advertised metadata (`title`, `output_schema`, `annotations`, `icons`, `_meta`) on the record instead of dropping it.
- **Host-private arguments:** `prepare_mcp_tool_arguments` always copies caller arguments. It restores `_reasoning` to public `reasoning` only for the exact strict LTP-v2 family schema, preserves `_reasoning` only when the server explicitly declares it, and otherwise removes that kernel-owned field. Ordinary unknown business fields are not filtered and remain the server validator's responsibility. Tests: `tests/test_mcp_capability.py`, `tests/test_mcp_v2_adapter_metadata.py`.
- **Rich results vs. the compatibility projection:** `preserve_tool_result` keeps the full ordered content union (text/image/audio/resource-link/embedded-resource), `structured_content` at any JSON type, `is_error`, `result_type`, and `_meta`; it is reachable per call through `last_tool_result`. `_decode_tool_result` remains the explicit **compatibility projection** to the single legacy value every kernel tool handler expects: dictionary `structured_content` first, then JSON-object text, preserving structured error fields at top level while forcing protocol-authoritative `status="error"`; plain-text errors retain the legacy `status`/`message` envelope. The projection is a deliberate reduction, not the only copy. Tests: `tests/test_mcp_structured_result.py`, `tests/test_mcp_sdk_v2_contract.py`.
- **Stale-resource recovery (issue #104):** `MCPClient` detects a dead stdio transport in `call_tool` and recovers. `_format_exception` renders `ClassName: message` (class-only when `str(e)` is empty) so an empty `ClosedResourceError` never surfaces as a blank `{"status":"error","message":""}`. `_is_stale_resource_error` flags closed/broken transports by class name + message substrings. On a stale error `call_tool` calls `restart()` (which `close()`s, clears `_ready`/`_error`, resets `_closed`/`_session`/`_loop`/`_thread`/`*_cm` so `start()` cannot lie) to make a future independent call possible. The default `retry_policy="never"` then returns an ambiguous, non-retryable error **without replaying** the submitted tool call because its remote commit point is unknowable. Exactly one replay occurs only when the caller explicitly attests `retry_policy="safe"`; a failed safe replay returns a helpful error naming the original class and retry failure. Non-stale errors surface the class name without churning the subprocess. `HTTPMCPClient` reuses `MCPClient._format_exception` / `_is_stale_resource_error` / `_ambiguous_call_error` and, since issue #740, has its own `restart()` and the same call_tool recovery flow — except it never replays (HTTP non-retry is contractual, below). Tests: `tests/test_mcp_closed_resource_restart.py`, `tests/test_mcp_sdk_v2_contract.py`.
- **HTTP non-retry is contractual.** `HTTPMCPClient.call_tool` deliberately takes no `retry_policy`: an HTTP tool call has the same unknowable remote commit point as stdio, so it never replays. The client still `restart()`s a stale HTTP/SSE transport on a stale error (issue #740) so a future independent call can succeed — recovery of the connection, not replay of the call. This asymmetry is a stated policy, not an unfinished feature, and `tests/test_mcp_closed_resource_restart.py` asserts the parameter's absence.
- The transport lifecycle, `list_tools()`, `_run_loop()`, and `_async_cleanup()` patterns remain duplicated between the two clients; result normalization is deliberately shared.
- `mail.py` is a compatibility-only alias surface. The normative boundary is `lingtai.kernel.mail_transport.MailTransportPort`; the production implementation is `lingtai.adapters.posix.mail.PosixFilesystemMailAdapter`. The legacy public names remain aliases, not a second implementation or a Core shim.
- `file_io_sidecar.py` is the **default native backend** for `Agent`-created file-I/O services. `default_file_io_service` is the factory that `Agent.__init__` calls; it consults `LINGTAI_FILE_IO_BACKEND` (`auto` / `rust` / `python`, default `auto`) and `resolve_sidecar_binary` to pick between Rust and the pure-Python `LocalFileIOBackend`. Resolver priority: explicit `binary_path=` > `LINGTAI_FILE_IO_SIDECAR` env > `LINGTAI_SEARCH_SIDECAR` (legacy) env > packaged `lingtai/bin/` binary (shipped in platform-specific wheels by `setup.py`) > dev-tree `crates/lingtai-search-sidecar/target/{release,debug}/`. The factory attaches a bounded construction snapshot: normalized selected mode, which environment alias supplied an applied override, and that resolved override value. The sensitive value is private, absent from snapshot repr, and projected only through the generic full-redaction flag. The strict `SidecarAdapter()` constructor still ignores packaged / dev-tree sources — opt-in callers see `not_configured` rather than picking up a stale binary. A valid environment selection is a service-construction input and stays pinned/strict until the service restarts; when neither environment value resolves at construction, the factory's `SidecarAdapter.autodiscover()` adapter re-resolves only automatic packaged/dev-tree sources on every call. Thus a staged packaged copy that a later `setup.py` build clears (it is a gitignored artifact) cannot leave a live agent's glob/grep bound to a missing path while the dev-tree binary still resolves, and a later environment mutation cannot take over that service. Defaults (`DEFAULT_*` constants) are imported from `file_io.py` so both backends stay in lock-step. Cargo is **not** required for install or the normal test suite — tests use a Python-script "sidecar"; only `test_rust_sidecar_integration_grep_and_glob` is cargo-gated.
