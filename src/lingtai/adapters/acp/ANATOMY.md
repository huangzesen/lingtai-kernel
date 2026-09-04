---
related_files:
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/ANATOMY.md
  - src/lingtai/adapters/acp/CONTRACT.md
  - src/lingtai/adapters/acp/BEHAVIORS.md
  - src/lingtai/adapters/acp/MANUAL.md
  - src/lingtai/adapters/acp/__init__.py
  - src/lingtai/adapters/acp/driver_authority.py
  - src/lingtai/adapters/acp/puffo_v0.py
  - src/lingtai/adapters/acp/puffo_v1.py
  - src/lingtai/adapters/acp/server.py
  - src/lingtai/cli_acp.py
  - src/lingtai/cli_puffo_v0.py
  - src/lingtai/cli.py
  - src/lingtai/kernel/turns.py
  - src/lingtai/kernel/execution_workspace.py
  - src/lingtai/kernel/turn_events.py
  - src/lingtai/kernel/turn_permissions.py
  - src/lingtai/kernel/provider_admission.py
  - src/lingtai/kernel/tool_executor.py
  - src/lingtai/services/session_mcp.py
  - src/lingtai/kernel/process_match.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/kernel/base_agent/CONTRACT.md
  - tests/test_acp_stdio.py
  - tests/test_puffo_v0_profile.py
  - tests/test_driver_authority_adapter.py
  - tests/test_correlated_turns.py
  - tests/test_execution_workspace.py
  - tests/test_turn_events.py
  - tests/test_turn_permissions.py
  - tests/test_provider_admission.py
  - tests/test_tool_executor.py
  - tests/test_session_mcp.py
  - tests/test_process_match.py
  - tests/test_lifecycle_daemon_shutdown.py
  - tests/test_lingtai_facade.py
  - tests/test_tools_package_data.py
maintenance: |
  Keep related_files as repo-relative paths to real files and keep the parent
  src/lingtai Anatomy edge reciprocal. Update this Anatomy with the ACP Contract,
  behavior task, manual, adapter, composition root, Core turn boundary, and tests
  whenever structure or ownership changes. See lingtai-dev-guide for details.
---
# ACP local stdio adapter

Local Agent Client Protocol v1 driving Adapter. This is a governed component
because it owns a real ecosystem wire promise; its normative owner is the
co-located [`CONTRACT.md`](CONTRACT.md), and its operator/developer procedure is
[`MANUAL.md`](MANUAL.md).

## Components

- `server.py` — `AcpStdioServer`: strict newline-delimited JSON-RPC reader,
  ACP initialize/session state machine, canonical workspace, strict stdio MCP
  validation/lease ownership, Text/ResourceLink translation, minimal turn-scoped
  tool lifecycle projection, lock-owned permission registry/response routing,
  one prompt waiter thread so the reader remains available for cancel, and one bounded FIFO
  of atomic batches consumed only by a disposable daemon writer. Generation/start
  checks suppress not-yet-started prompt frames; framing/write failures abort the
  transport and active prompt without making stdout teardown authority. Implements
  behavior [ACP001](BEHAVIORS.md#behavior-acp001).
- `__init__.py` — small public package export for the protocol version and server.
- `puffo_v0.py` — local operator registry and typed ACP-only turn-origin policy
  for the identity/workspace-bound full-tool `puffo-v0`
  profile. It resolves an opaque runtime id to one canonical persistent identity
  and workspace, verifies an entry digest plus provision-time filesystem
  identity, serializes provision/revoke read-modify-write operations with a
  POSIX lock, records terminal revocations in an append-only tombstone log, and
  refuses missing, malformed, tampered, retargeted, or revoked entries before
  Agent construction. Its `entry_digest` authenticates registry data rather
  than claiming to authenticate the effective manifest, tool/action, or full
  launch policy. Its policy admits only authenticated driving-adapter provider
  turns; it is not a tool/runtime containment policy. Its Phase A
  owner-only-filesystem implementation deliberately rejects Windows until an
  equivalent ACL-backed adapter exists.
- `puffo_v1.py` — the one fixed session-MCP ingress for the next profile. It
  accepts exactly one Puffo-owned stdio service named `puffo`, with fixed
  `-m puffo_agent.mcp.puffo_core_server` arguments. The interpreter and
  environment are deployment-owned: ordinary ACP string values are preserved,
  while the required local-service token is never logged. They do not identify
  the service or form a security boundary.
- `../../cli_acp.py` — outer composition root. Captures the original stdout wire,
  quarantines Python application stdout to stderr before Agent construction,
  composes the existing Agent, consumes the typed bounded stop proof, and hard-
  exits on incomplete quiescence so no later Python state write can race teardown.
  For both Puffo profiles, it consumes the one launcher-injected Driver authority
  descriptor and composes either its root Port pair or a fail-closed pair.
  Shared poisoned-worker exit logging is lease-aware: retained ownership may log,
  while a successful `STOPPED` release skips every later workdir append and still
  reaches the unconditional process exit.
- `../../cli_puffo_v0.py` — local-only control plane that provisions an existing
  persistent identity or revokes it for future `puffo-v0` launches; it is not an
  ACP data-plane surface.
- `../../kernel/process_match.py` — exact duplicate-host grammar for module,
  console, legacy, and quoted Windows `.exe` ACP launch forms.
- `../../kernel/turns.py`, `../../kernel/execution_workspace.py`, `../../kernel/turn_events.py`, `../../kernel/turn_permissions.py`, `../../kernel/provider_admission.py`, and `../../kernel/tool_executor.py` — inward Core boundary consumed by the Adapter:
  `TurnHandle`, `TurnResult`, terminal outcome, exact correlation, and matching
  cooperative cancellation, immutable workspace metadata, task-local scope, and
  failure-isolated lifecycle observation and fail-closed one-shot permission.
  They contain no ACP vocabulary.
- `../../services/session_mcp.py` — atomic outer stdio overlay: start/list all,
  collision preflight, one publication, explicit lease, and rollback/close.
- `tests/test_acp_stdio.py` / `tests/test_puffo_v0_profile.py` / `tests/test_correlated_turns.py` /
  `tests/test_turn_events.py` / `tests/test_turn_permissions.py` / `tests/test_tool_executor.py` /
  `tests/test_process_match.py` — wire, Core settlement, and duplicate-host
  conformance evidence. `tests/test_tools_package_data.py` pins wheel/sdist
  inclusion of this component's governed docs.

## Connections

Inbound: a local ACP client launches `lingtai-agent acp --agent-dir <dir>` and
exchanges one JSON-RPC object per stdio line. The constrained Puffo profiles
instead launch `lingtai-agent acp --profile puffo-v0|puffo-v1 --runtime-id
<opaque-id>` and share the same registry-bound identity/workspace. `puffo-v0`
denies every session MCP; `puffo-v1` accepts only its one fixed Puffo Core stdio
service. Core admits only the typed authenticated adapter origin to provider
dispatch. This controls turn initiation, not what state the eventual turn can
read. The composition root re-resolves a profile runtime immediately before
Agent composition so a normal resolve-to-start drift fails closed; host-principal
filesystem replacement after that check remains outside this profile's trust
boundary. The same composition root consumes and removes
`LINGTAI_DRIVER_AUTHORITY_FD`; only a valid root endpoint becomes the profile's
provider and derived-launch Port pair, while every other outcome is typed
fail-closed. Outbound: the Adapter calls only the
protocol-neutral `BaseAgent.submit_turn`/`TurnHandle` boundary with an optional
turn-scoped tool observer. The CLI root
reuses `cli.load_init`, `cli.build_agent`, venv resolution, logging, lifecycle,
and the workdir lease; the Adapter does not construct Core or provider objects.

## Composition

Parent: [`src/lingtai/`](../../ANATOMY.md). Neighbor adapters remain under
`src/lingtai/adapters/`; this child is technology-specific at the ACP boundary,
while Core correlation lives under `kernel/`. There is no ACP SDK dependency,
selector, remote adapter, session store, workspace service, persistent permission store,
or remote MCP bridge in this slice.

## State

Process-local state only: initialized flag, one opaque session id, one canonical
workspace, one session-MCP lease, one active prompt/handle/observer-broker with terminal tool-id ownership,
one lock-owned pending-permission registry,
closing/aborted generation, a bounded 64-batch FIFO, one disposable
daemon writer, and short-lived waiter thread records. Active/busy ownership lasts
through physical terminal-batch completion, close invalidation, or fatal abort.
ACP session/correlation identifiers are not persisted. Both Puffo profiles
read the same operator-managed local registry at spawn time; neither creates a
durable ACP session nor changes the Agent's own durable identity state. Durable
agent state remains owned by the existing Agent/workdir lifecycle. The one-time
Driver authority environment locator is removed at composition and is not
retained as process state. Closing requests active cancellation
and suppresses prompt frames that have not crossed the writer start check; typed
Agent stop retains services/heartbeat/lease until execution quiescence is proven.

## Notes

The stable ACP v1 schema supports broader content and session integrations than
this deliberately narrow slice. Empty or strict stable-v1 stdio `mcpServers`
plus baseline Text and ResourceLink prompts are accepted; remote MCP and
capability-gated rich content fail explicitly. Capability objects stay empty so omitted optional
features are never advertised. Follow the manual and Contract before widening
scope.

Both Puffo profiles are a second gate on their controlled entrypoint, not host isolation:
the same OS identity can still alter the registry or bypass it by launching the
generic `--agent-dir` ACP command. That is an explicit host trust boundary.

`driver_authority.py` is process-local protocol state: one authenticated
AF_UNIX stream, a bounded receive buffer, one request lock, endpoint identity,
and any one-use lease not yet consumed by a future launch consumer. It contains
no daemon queue, supervisor, profile, or persistent state.
