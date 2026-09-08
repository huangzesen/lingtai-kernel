---
name: acp-local-stdio-behavior-tests
behavior_version: 3
labt_version: 2
contract: CONTRACT.md
anatomy: ANATOMY.md
related_files:
  - src/lingtai/adapters/acp/CONTRACT.md
  - src/lingtai/adapters/acp/ANATOMY.md
  - src/lingtai/adapters/acp/MANUAL.md
  - src/lingtai/adapters/acp/driver_authority.py
  - src/lingtai/adapters/acp/server.py
  - src/lingtai/cli_acp.py
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/kernel/turns.py
  - src/lingtai/kernel/execution_workspace.py
  - src/lingtai/kernel/turn_events.py
  - src/lingtai/kernel/turn_permissions.py
  - src/lingtai/kernel/tool_executor.py
  - src/lingtai/services/session_mcp.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - tests/test_acp_stdio.py
  - tests/test_driver_authority_adapter.py
  - tests/test_correlated_turns.py
  - tests/test_execution_workspace.py
  - tests/test_turn_events.py
  - tests/test_turn_permissions.py
  - tests/test_tool_executor.py
  - tests/test_session_mcp.py
  - tests/test_lifecycle_daemon_shutdown.py
maintenance: |
  Keep this LABT reciprocal with the ACP Contract and Anatomy. Update exact wire
  evidence, supported scope, commands, and pass criteria whenever the v1 stdio
  behavior changes; do not turn an omitted capability into an implied promise.
---
# ACP Local Stdio Behavior Tests

## Behavior ACP001 — one local ACP v1 baseline turn settles normally or cooperatively cancelled without corrupting stdout

- **id**: ACP001
- **title**: one local ACP v1 baseline turn settles normally or cooperatively cancelled without corrupting stdout
- **guards**: `acp-local-stdio` § Behavior and Contract rules 1–9 — see [CONTRACT.md](CONTRACT.md#contract-rules)
- **supersedes**: `tests/test_acp_stdio.py`, `tests/test_correlated_turns.py` (retained as bottom asserts)
- **runner**: any LingTai coding agent with shell access to this repository
- **prerequisites**: a clean checkout at `<repo>`; a project Python with installed runtime/test dependencies; no live agent sharing a pytest scratch directory
- **estimate**: ≈ 5 minutes

### Steps
1. From `<repo>`, run `python -m pytest -q -x tests/test_turn_events.py tests/test_turn_permissions.py tests/test_tool_executor.py tests/test_correlated_turns.py tests/test_execution_workspace.py tests/test_session_mcp.py tests/test_acp_stdio.py` with the project Python.
2. Inspect the captured normal-turn frames in the passing wire test: initialize result, session/new result, one `session/update` with `sessionUpdate=agent_message_chunk` and Text content, then the original prompt result with `stopReason=end_turn`; inspect the ResourceLink case and confirm validated link metadata reaches the Core text boundary.
3. Inspect lifecycle tests: serial, parallel, denied, and failed tools emit minimal ordered `tool_call`/`tool_call_update` status frames before terminal response; collector-owned future exceptions, timeout boundaries after worker return, and cancellation each produce one FAILED terminal with no late completion; arguments/results/raw payloads are absent; observer exceptions, late terminal events, and close do not leak or alter Core execution.
4. Inspect permission tests: every otherwise-allowed known tool emits pending and
   the exact two-option request with a plain `ToolCallUpdate` carrying no
   `sessionUpdate`; only the exact nested result
   `{outcome: {outcome: "selected", optionId: "allow_once"}}` received after
   request write+flush reaches STARTED. A response captured before flush
   remains denied after publication; cancel-before-registration emits no orphan,
   and cancel during a blocked pending write yields pending→failed without request;
   nested reject/cancel, flattened legacy, malformed/extra-field/error/late
   responses, timeout, close, queue/write/
   flush failure deny and wake without exposing private tool data.
5. Inspect the cancellation test: while the original prompt is unresolved, send a no-id `session/cancel` for the same session and confirm only the original prompt id receives `stopReason=cancelled`.
6. Inspect request-id/error-taxonomy tests: explicit null/string/signed-int64
   boundary ids round-trip, missing id remains notification-only, and bool,
   fractional, structured, or out-of-range integer ids fail with diagnostic id
   null. Confirm methodless permission responses keep their
   existing routing. Confirm local session busy/not-initialized errors are
   `-32010`/`-32011`, not ACP's predefined authentication/resource errors
   `-32000`/`-32002` or the Adapter's established session-not-found `-32001`.
7. Inspect every Adapter-authored stdout line with `json.loads`; confirm there is one complete JSON-RPC object per physical line and Python boot/runtime/stop `print` output is captured only on stderr. Confirm docs prohibit native fd 1, pre-captured stdout, and child stdout rather than claiming to quarantine them.
8. Inspect explicit-scope/lifecycle tests: malformed cwd/MCP, a second session, concurrent prompt, invalid ResourceLink, and failed Core turn each produce the named error path. Confirm canonical outside-agent workspace rooting, parent/symlink refusal, parallel propagation without later leakage, atomic stdio MCP publication/rollback/collision refusal, and lease teardown on close/EOF. Confirm the existing blocked-write and typed-stop lifecycle evidence remains intact.

### Expected evidence
- [ ] Step 1 reports all focused tests passing with no network/provider call.
- [ ] Normal wire order is zero or more permission pending/request plus lifecycle cycles, then an optional agent-message update and final response; terminal reason is exactly `end_turn`.
- [ ] Tool lifecycle frames use session-unique ids and only title/status metadata; no arguments, results, content, locations, rawInput/rawOutput, or internal error detail is projected.
- [ ] Cancel has no response of its own and the original prompt settles exactly once as `cancelled`; no later agent update is emitted for it.
- [ ] Every Adapter-authored stdout line parses independently as JSON-RPC 2.0; Python diagnostic prints are stderr-only and unsupported native/child fd 1 paths are documented.
- [ ] Included null/string/signed-int64 request ids echo exactly, invalid ids use
  diagnostic id null, missing ids remain notifications, and local session-state
  errors avoid ACP's predefined `-32000` authentication / `-32002` resource
  meanings plus the Adapter's established `-32001` session-not-found meaning.
- [ ] Baseline ResourceLink reaches Core as validated compact metadata; strict stdio MCP publishes atomically while malformed/remote variants fail explicitly, and Core failure uses the fixed `LingTai turn failed` wire message.
- [ ] Agent shutdown cannot wait on EOF or any client write; not-yet-started prompt frames are invalidated, fatal queue/write failures abort, typed timeout retains liveness/lease until ACP process termination, a successful poisoned stop emits no post-release workdir log before exit 0, and invalid UTF-8 has a fixed Parse error only.

### Pass / Fail
Pass when all evidence is observed, every handle reaches one terminal result, and
no provider/network/config mutation is needed. Fail on a hanging handle,
duplicate terminal response, uncorrelated cancellation, stdout contamination,
implicit second session, ignored non-empty session MCP input, leaked internal
failure/tool payload, out-of-order or post-terminal lifecycle updates, or any
claimed remote/v2/persistent-permission/workspace capability; record
the evidence trail in the task report.

## Behavior ACP002 — puffo-v0 accepts only an operator-provisioned local runtime

- **id**: ACP002
- **title**: puffo-v0 accepts only an operator-provisioned local runtime
- **guards**: `acp-local-stdio` § Contract rules 11 — see [CONTRACT.md](CONTRACT.md#contract-rules)
- **runner**: any LingTai coding agent with shell access to this repository
- **prerequisites**: a project Python with test dependencies
- **estimate**: ≈ 1 minute

### Steps

1. Run `python -m pytest -q -x tests/test_puffo_v0_profile.py`.
2. Inspect the registry cases: only an active, entry-digest-valid opaque id
  with its original directory identity resolves; tampered, retargeted, or
  revoked entries, including a missing required revocation log, fail before
  composition. An active identity and workspace cannot be bound twice.
3. Inspect the profile session and turn-origin cases: `puffo-v0` rejects every
   non-empty `mcpServers` input. `puffo-v1` accepts exactly one `puffo` service
   with `-m puffo_agent.mcp.puffo_core_server`, a deployment-local absolute
   Python path, ordinary unique string environment values, and
   a non-empty `PUFFO_LOCAL_SERVICE_TOKEN`; unknown environment names pass through and do
   not identify the service. Missing/extra/alternate descriptors fail. The
   operator-managed tool surface is retained, but
   legacy/inbox/internal events cannot queue or dispatch any provider/model
   turn. A direct ACP prompt carries the authenticated-adapter origin.
4. Inspect Driver authority composition: the launcher supplies exactly one
   inherited root AF_UNIX-stream descriptor in `LINGTAI_DRIVER_AUTHORITY_FD`.
   The profile consumes and removes the locator before Agent construction,
   passes the resulting Driver client to the root provider-call Port, and
   projects that same client to the derived-launch Port. Missing, malformed,
   unusable, or derived-role endpoints install the typed fail-closed pair;
   they never select the generic runtime policy as a substitute.

### Expected evidence

- [ ] A remote ACP payload cannot provide a filesystem path or arbitrary MCP
  process launch through either profile; `puffo-v1` can only mount the one
  fixed Puffo Core service projected by its driver.
- [ ] The profile denies unavailable identities and every non-ACP origin before
  provider dispatch, while preserving the configured local tool surface.
- [ ] Root provider calls and derived-launch requests use the consumed Driver
  endpoint, or both return `driver_authority_unavailable`; the environment
  locator is absent before Agent construction and cannot reach descendants.
- [ ] The host-boundary non-guarantee remains documented.

### Pass / Fail

Pass when the focused tests prove every profile startup field comes from the
local registry and constrained session policy. Fail on a remote-controlled
workspace/MCP input, a non-ACP event that reaches provider dispatch, accepted
revoked id, a missing/invalid/non-root Driver endpoint that falls back to a
generic admission policy, or any claim that this controlled entrypoint isolates
a same-OS principal or contains full-tool runtime behavior.
This behavior is a registry/session-policy foundation: its entry digest does
not by itself prove a complete effective tool/action or launch-security policy.
