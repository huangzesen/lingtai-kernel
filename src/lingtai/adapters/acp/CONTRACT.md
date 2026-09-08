---
name: acp-local-stdio
contract_version: 4
root_contract: CONTRACT.md
related_files:
  - src/lingtai/adapters/acp/ANATOMY.md
  - src/lingtai/adapters/acp/BEHAVIORS.md
  - src/lingtai/adapters/acp/MANUAL.md
  - src/lingtai/adapters/acp/__init__.py
  - src/lingtai/adapters/acp/driver_authority.py
  - src/lingtai/adapters/acp/puffo_v0.py
  - src/lingtai/adapters/acp/server.py
  - src/lingtai/cli_acp.py
  - src/lingtai/cli_puffo_v0.py
  - src/lingtai/cli.py
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/kernel/turns.py
  - src/lingtai/kernel/execution_workspace.py
  - src/lingtai/kernel/turn_events.py
  - src/lingtai/kernel/turn_permissions.py
  - src/lingtai/kernel/provider_admission.py
  - src/lingtai/kernel/puffo_admission_witness.py
  - src/lingtai/kernel/tool_executor.py
  - src/lingtai/services/session_mcp.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - src/lingtai/kernel/process_match.py
  - src/lingtai/kernel/base_agent/CONTRACT.md
  - pyproject.toml
  - tests/test_acp_stdio.py
  - tests/test_puffo_v0_profile.py
  - tests/test_puffo_admission_witness.py
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
  Keep this contract reciprocal with its Anatomy and root CONTRACT.md. Update
  ACP translation, the Core turn boundary, composition, manual, and settlement/
  wire tests together. This is the v1 local-stdio slice only; widening sessions,
  content, MCP, workspace, permissions, or transport requires an explicit
  contract change rather than an undocumented fallback.
---
# ACP local stdio

## Purpose

Expose one existing LingTai agent to a local Agent Client Protocol v1 client
through newline-delimited JSON-RPC on stdio. ACP is a driving Adapter: it
translates protocol messages into the protocol-neutral correlated inbound-turn
API owned by Core and never reaches into provider/session/tool internals.

This slice implements stable ACP v1's baseline session `cwd` and stdio MCP
requirements alongside Text and ResourceLink prompts. It remains deliberately
narrow: remote MCP, additional directories, rich content, persistent permissions, and
multi-session persistence are not advertised.

## Behavior

Guarded by: [ACP001](BEHAVIORS.md#behavior-acp001) and
[ACP002](BEHAVIORS.md#behavior-acp002).

A successful process negotiates ACP protocol version `1`, creates exactly one
opaque session, accepts baseline Text and ResourceLink prompt blocks, emits the
completed LingTai response as one `agent_message_chunk` session update, and settles the original prompt with
`end_turn`. `session/cancel` targets only the active handle and the original
prompt eventually settles `cancelled`; cancellation is cooperative and does not
claim hard provider abort or running-tool preemption. Each Adapter-authored wire
line is one compact UTF-8 JSON object. The composition root redirects Python
`sys.stdout`/`print` diagnostics to stderr; native fd 1 writes, pre-captured stdout
objects, and child-process stdout are not quarantined in this slice and are
therefore prohibited while ACP owns the transport.

## Port

The Adapter consumes `BaseAgent.submit_turn(content, sender, correlation_id,
execution_workspace, tool_observer, permission_broker) -> TurnHandle` and
`TurnHandle.cancel()/result()` from
`src/lingtai/kernel/turns.py`. The terminal `TurnResult` distinguishes
`normal`, `cancelled`, and `failed` and carries the complete response text for
normal settlement. This Port contains no ACP method, JSON-RPC object, session
identifier, ACP method, MCP configuration, or transport vocabulary. Its optional
permission broker is protocol-neutral and carries only tool call id/name.
It may carry the generic immutable `ExecutionWorkspace` value attached to a
correlated turn.

## Adapters

`AcpStdioServer` is the production local stdio Adapter. `cli_acp.run_acp` is the
outer composition root behind `lingtai-agent acp --agent-dir <existing-dir>`: it
captures the original Python stdout wire, redirects `sys.stdout` to stderr before
Agent construction, loads and starts the existing agent, serves ACP, and requests
a typed bounded stop on EOF or interrupt. A timed-out/non-quiescent stop retains
services, heartbeat, and lease until the process owner hard-exits; no later Python
state write can occur after OS release. It adds no dependency and uses standard
library JSON/threading streams directly.

`puffo_v0` and `cli_puffo_v0` provide a narrower composition profile. An
operator locally provisions an existing persistent identity and canonical
execution workspace under an opaque runtime id. The profile data plane receives
only that id; it never accepts an agent directory, workspace path, executable,
argv, environment, or MCP command from the remote caller.

## Contract rules

1. `acp-local-stdio.protocol.v1` — initialization requires an integer
   `protocolVersion` and negotiates by returning this Agent's latest supported
   `protocolVersion: 1`, plus empty
   `agentCapabilities`, LingTai `agentInfo`, and `authMethods: []`. Unsupported
   methods/params use JSON-RPC errors; notifications receive no response. An
   included request id may be a string, non-boolean signed 64-bit integer, or explicit null
   and is echoed exactly; a missing id alone denotes a notification. Invalid
   ids produce the JSON-RPC diagnostic id null. Local session busy and
   not-initialized errors use unreserved server-error codes `-32010` and
   `-32011`, respectively, rather than colliding with ACP's predefined
   authentication `-32000` / resource-not-found `-32002` meanings or this
   Adapter's established session-not-found `-32001` meaning.
2. `acp-local-stdio.session.v1` — one process owns at most one initialized ACP
   session and one active prompt. A second `session/new` or concurrent prompt
   fails explicitly. `cwd` must be absolute, exist, and be a directory; it is
   canonicalized once and attached to each correlated turn without changing the
   Agent identity/config/history workdir or process cwd. `mcpServers` uses the
   stable-v1 stdio `{name, command, args, env}` shape. Names are unique, command
   is absolute, args are strings, and env is an array of unique string
   `{name,value}` records. Malformed/unknown fields and HTTP/SSE are rejected.
   Every server starts and lists tools before one atomic publication; duplicate,
   existing, or reserved tool names reject the session and close all clients.
   Non-empty `additionalDirectories` fails explicitly because extra roots are not
   advertised in this slice.
3. `acp-local-stdio.turn.v1` — prompt input is a non-empty list of baseline
   Text/ResourceLink blocks. Text is concatenated in order; each validated
   ResourceLink is projected into the Core text boundary as compact JSON metadata
   without fetching it. Images/audio, embedded resources,
   message/usage streaming, and rich tool content are unsupported. During the
   correlated turn, the Adapter projects only minimal tool lifecycle metadata as
   ACP v1 `tool_call`/`tool_call_update`: one session-unique id, the tool name as
   title, and `in_progress`/`completed`/`failed` status. Arguments, results,
   content, locations, `rawInput`, `rawOutput`, and internal errors never cross
   the wire. Normal output is at most one completed-response
   `agent_message_chunk` followed by `{stopReason: "end_turn"}`; no hidden
   thoughts or tool internals are projected. Additionally, the Adapter projects
   a **Puffo admission committed-fact** as a `tool_call_update` carrying only
   `_meta.puffo.admission/1 = {toolCallId, binding}`. The nested `toolCallId`
   MUST be byte-identical to the update's own outer `toolCallId` — the
   session-correlated wire id (`<correlation>:<raw Core id>`). This equality is
   load-bearing, not cosmetic: the Puffo receiver derives its internal fact id
   from the outer wire id and rejects the frame unless the nested id matches, so
   emitting the raw Core id there is a deterministic non-admission. It is
   still metadata-only (no arguments/results/content), and is the *reliable*
   counterpart to the fail-open lifecycle projection above: Core emits it at a
   caller settle point once a receipt-bearing tool result is durably present on
   the canonical wire (see `src/lingtai/kernel/base_agent/`), and the Adapter
   handler refuses the cosmetic fail-open suppression (per-call publication
   lock, `_terminal`/`_announced`) used for lifecycle updates. It is idempotent
   (at most one per `toolCallId` per generation) and the ONLY legitimate
   non-delivery is genuine session teardown (closing / superseded generation /
   claimed-or-replaced active prompt — a torn-down session has no wire to
   publish onto). Three properties bound its meaning: (a) `binding =
   sha256(toolCallId ‖ 0x00 ‖ raw_receipt)` where `toolCallId` is that same
   correlated outer wire id — the receiver recomputes the binding over the outer
   id and strictly compares, so the witness maps its kernel-namespace id through
   the turn-bound observer's wire namespacer BEFORE hashing (correlation comes
   down to the witness; the raw receipt never rises to the Adapter, preserving
   the metadata-only event boundary). The wire id has ONE construction site in
   this tree (`_PromptToolObserver.wire_tool_call_id`); the lifecycle,
   permission, and admission frames all route through it. That single source is
   itself load-bearing across the repo boundary: the receiver fills its
   `_completed_tool_calls` from the lifecycle frame's id and admits the fact only
   if the admission frame's id is absent from that set, so the two ids must be
   byte-identical — which holds only because both are produced there. It proves the emitter *possessed the
   receipt paired with this exact correlated toolCallId* — it does NOT itself prove commit;
   "committed" rests entirely on the emission-point discipline (a settle point
   is after a carrying `send(...)` has fully settled, incl. any
   rollback/restore, or after a `commit_tool_results(...)` returns, scanning
   real interface state so a rolled-back entry is naturally never witnessed).
   (b) `read_inbox` results carry no receipt marker, so they are an explicit
   no-receipt / no-fact exclusion. (c) Residual — if the process crashes after a
   receipt-bearing result is committed to the wire but before its settle-point
   scan runs, or AED retry compaction / large-result spill rewrites that block's
   content (dropping the marker) before it is witnessed, the fact is never
   emitted; Puffo then fail-closed denies an otherwise-legitimate result. This
   benign-prohibited outcome is acceptable but must not be silent: Core logs a
   bounded `puffo_admission_fact_not_delivered` event when a scanned fact has no
   observer to receive it. The turn-start watermark is a monotonic
   `ChatInterface` entry id; `from_dict` reseeds `_next_id = max(id)+1`, so a
   narrow theoretical window exists where deleting a tail entry then serializing
   and reloading could reuse a retired id and let the watermark admit a stale
   entry — a known benign-prohibited residual in the same fail-closed direction
   (at worst one extra at-most-once fact, which Puffo idempotency absorbs).
   Settle-point test mapping: every production-reachable settle point — the
   restore commit, the initial-send drain carrier, the three no-API terminal
   commits (intercept / cancel / poll-backoff), and the main tool-loop send
   success and exception points — is pinned by a real-path discriminating test
   in `tests/test_puffo_admission_witness.py` (deleting the scan at any of
   them turns a test red). The four tc_wake settle points (dispatch
   `send([item.result])` success/exception and continue `send(None)`
   success/exception) are EXEMPT from red-coverage by design: non-correlated
   turns bind no witness scope, so those scans are lazy no-ops in production —
   uniform defense-in-depth instrumentation, not reachable behavior. Any
   change that binds a witness scope over tc_wake turns must add the
   corresponding red tests in the same change. The scope itself — the
   production `begin_admission_witness_scope` bind and its `end_..._scope`
   teardown on the correlated-turn loop — is pinned separately by
   `tests/test_correlated_turns.py::test_correlated_turn_binds_admission_witness_scope_on_production_path`,
   which drives the real `_run_loop` across two turns with NO hand-assembled
   scope: deleting the bind makes the in-turn scope absent (fail-silent: every
   settle-point scan then early-returns and a real ACP turn emits zero facts),
   deleting the teardown leaves the scope open after the loop, and hoisting the
   bind out of the loop ("bind once per session") hands turn two turn one's
   scope object — a per-turn re-bind probe reddens that last case. That probe
   pins that the bind RUNS afresh each turn, not that the watermark's numeric
   boundary is recomputed correctly; the value-level non-refire property is
   pinned separately by `test_puffo_admission_witness.py::test_iv_two_turn_watermark_no_refire`
   (real interface, hand-assembled scope). This closes the gap that the settle-point suite in
   `test_puffo_admission_witness.py` cannot: those tests assemble the scope by
   hand, so they stay green if the production bind regresses.
4. `acp-local-stdio.cancel.v1` — `session/cancel` calls only the active
   correlated handle. Cancellation requested before exact terminal settlement
   wins and the original prompt returns `{stopReason: "cancelled"}`. The reader
   stays live while the prompt worker waits, but cancellation remains the Core
   cooperative latch boundary, not a provider-abort guarantee.
5. `acp-local-stdio.failure.v1` — a failed Core turn settles the original request
   with a bounded fixed JSON-RPC Internal error; provider/internal detail is not
   copied to the wire. EOF/close cancels active work and invalidates every prompt
   frame that has not crossed the writer's start check. A frame already inside an
   OS write may finish, so close between update/final can leave the update without
   the final response. Agent shutdown is checked before terminal claim, enqueue,
   and each physical prompt frame. Typed stop proves run-loop plus retained
   poisoned-provider Future quiescence before service/heartbeat/lease teardown.
   If poison recovery reaches `STOPPED` and releases the workdir lease before the
   process-owner poison exit, no later Python workdir log is permitted. The shared
   poison helper logs only while ownership remains, then best-effort flushes and
   exits 0; ACP's earlier incomplete-stop branch still exits 70 while retaining
   ownership and never reaches this successful-stop poison path.
6. `acp-local-stdio.framing.v1` — input and output are UTF-8 newline-delimited
   JSON-RPC 2.0, one object per physical line. Batch messages and non-standard
   JSON constants are invalid. Producers only serialize and `put_nowait` one
   atomic batch into a bounded FIFO; a single disposable daemon writer owns the
   stream. Queue saturation, serialization failure, short write, write failure,
   or flush failure aborts the whole transport with no retry/fallback frame. The
   writer is never joined or made teardown authority, so blocked stdout cannot
   hold the coordinator, Agent stop, or lease teardown. Python `sys.stdout` and
   `print` diagnostics are stderr-only; native/pre-captured/child fd 1 output is
   prohibited rather than quarantined. The bounded reader queue prevents an
   unbounded line backlog, and Agent shutdown returns the coordinator even with
   stdin open. The duplicate-host guard recognizes module/console/legacy ACP
   forms including quoted Windows `lingtai-agent.exe` before stale signal cleanup;
   the workdir lease remains authoritative.
7. `acp-local-stdio.scope.v1` — local stdio and ACP v1 only. Multi-session
   persistence/load, additional workspace roots, persistent permission choices,
   message/usage streaming, tool content/results, remote MCP/transports,
   authentication, and ACP v2 are non-goals.
8. `acp-local-stdio.workspace-mcp-lifecycle.v1` — relative/default File paths,
   Shell cwd validation/defaults, risky-action canonicalization, and parallel
   dispatch observe the canonical turn workspace. Parent and symlink escapes
   fail. Context is reset between turns and copied to worker threads. The ACP
   server owns one idempotent session-MCP lease and closes it on close, EOF,
   fatal abort, startup rollback, or Agent stop.

9. `acp-local-stdio.tool-lifecycle.v1` — each first lifecycle event emits one
   `tool_call`; later events for that id emit `tool_call_update`. Per-tool state is
   monotonic and duplicate terminal events are dropped. For parallel dispatched
   tools, workers emit only STARTED; the collector exclusively claims terminal
   after incorporating a result or deciding exception, timeout, or cancellation,
   so the returned outcome and projected terminal cannot diverge. The server state lock
   linearizes lifecycle enqueue against terminal claim, the bounded FIFO preserves
   accepted updates before the terminal response, and close/generation/terminal
   invalidation drops later events. Observer or projection failure cannot fail the
   underlying tool, but queue/framing failure still aborts the transport under rule
   6. Tool-call ids are session-unique and no argument/result/raw payload is sent.
10. `acp-local-stdio.permission.v1` — every otherwise-allowed known ACP tool is
   announced `pending`; the permission request carries a plain minimal
   `ToolCallUpdate` with the same id/title/status but no `sessionUpdate`
   discriminator, plus exactly `allow_once` and `reject_once`. Only the first
   valid matching nested result
   `{outcome: {outcome: "selected", optionId: "allow_once"}}` that arrives
   after the request frame is physically
   written and flushed dispatches; response arrival and the post-flush published
   bit linearize under the state lock, so a guessed/pre-flush response remains
   denied even if descheduled until after publication. A per-request publication
   lock protects the wire boundary without holding the global state lock across
   stdout. Nested reject/cancel, flattened legacy, malformed/error/unknown-option
   responses, timeout after 60 seconds, close/EOF, queue/framing/write failure,
   and broker failure deny and wake the waiter. Unknown, duplicate, and late
   responses are ignored. Tagged batches suppress an unwritten resolved request;
   lifecycle is marked announced only after its initial pending frame flushes. If
   cancel/timeout drains permission during an already-started pending write, the
   writer emits an adjacent failed update after that frame and suppresses request.
   Arguments, commands, paths, environment, results, content, locations, raw
   input/output, internal errors, and private paths never enter permission wire.
   The existing risky-action gate remains first and may deny without a request.
11. `acp-local-stdio.puffo-v0.v1` — `lingtai-agent acp --profile puffo-v0
    --runtime-id <id>` resolves `<id>` only through the local
    operator-managed registry. The entry must be active, structurally exact,
    entry-digest-valid, and bind one initialized agent directory and canonical
    workspace to their provision-time canonical path plus POSIX device/inode/
    owner/group identity. Resolve rejects a symlink retarget, canonical-path
    drift, or replacement at the same path; active runtime bindings are unique
    for both agent directory and workspace. The composition root resolves the
    runtime again immediately before Agent construction. This narrows ordinary
    resolve-to-start drift; a same-OS principal that rewrites the filesystem
    after that check remains within the explicit host trust boundary below.
    `entry_digest` protects the exact registry entry only; it is not a digest
    of `init.json`, effective tool/action policy, executable/argv/environment,
    or addon/plugin policy. The driver-owned full launch-plan digest remains a
    separate cross-process contract. A profile session accepts
    only that workspace and
    `mcpServers: []`; it never starts a client-supplied process. This is an
    identity/workspace-bound **full-tool** profile: operator-managed LingTai
    capabilities remain available, including after refresh. Its capability
    boundary is provider-turn initiation: the profile consumes one
    launcher-injected `LINGTAI_DRIVER_AUTHORITY_FD` root endpoint before Agent
    construction and uses its Driver-backed Port for each root provider request
    and derived-launch decision. The locator is removed immediately and is not
    forwarded to a child process. Missing, malformed, unusable, or derived-role
    endpoints install a typed fail-closed Port pair, so no generic profile
    policy can accidentally authorize provider I/O or a launch. Inbox,
    task-card, alarm, mail/MCP wake, and other independent root events remain
    denied before provider dispatch. This slice configures only the root ACP
    process; daemon/avatar child endpoint adoption and supervisor lifecycle are
    separate layers. This controls who may start a root
    turn, not what state a later admitted turn may read: non-ACP sources may
    still write state. The profile adds no `external_send` approval
    boundary and does not promise workspace-only writes, process containment,
    no background descendants, or network containment. “Authenticated Adapter”
    is a typed handoff from the Puffo driver that owns the local ACP process;
    LingTai's stdio server does not independently authenticate a remote Puffo
    user. Registry provision/revoke mutations are process-serialized.
    `lingtai-agent puffo-v0 discover --root <directory> --json` is a separate
    read-only control-plane query: it lists only initialized directories below
    the caller-selected canonical root, never follows directory symlinks, skips
    unreadable descendants, and creates or rewrites no registry, lock, or
    tombstone artifact. Each item returns canonical `agent_dir`, a directory-name
    display label, a `runtime_id` when the directory is attributed to a registry
    entry, a `workspace` only when a live binding exists (`null` otherwise, never
    a fabricated path), a `status` classifying the directory by the action it
    demands of the caller, and an advisory `formerly_bound_runtime_id` (`null`
    except on an `available` directory whose path a prior entry recorded — see
    below). `status` is emitted directly from that classification,
    never re-derived from whether a `runtime_id` is present, so the states below
    are distinguishable: `available` (no entry — may provision), `bound` (usable
    now — resolvable), `revoked` (re-provision under a new id),
    `policy_version_mismatch` (authentic but provisioned under an older policy
    version — revoke and re-provision the same directory), `stale_binding`
    (authentic and active, but the on-disk directory it names no longer presents
    the provisioned device/inode/owner/group identity — revoke and re-provision
    after verifying the directory), `integrity_failed` (digest
    missing or mismatched — stop and escalate; never auto-revoked), and
    `shape_mismatch` (authentic but not this profile — escalate). Discovery
    attributes each entry to a walked directory by that provisioned device/inode
    identity, never by the stored path string, so an entry whose recorded path has
    since become non-canonical (a parent turned into a symlink, or the directory
    was renamed with a symlink left at the old path) is still attributed to the
    physical directory it actually holds. `bound` is a
    live promise, not a registry-content claim: discovery verifies the recorded
    agent_dir and workspace still resolve to their provisioned identity — the
    exact check `resolve_runtime` enforces — before reporting
    `bound`, and reports `stale_binding` (with the `runtime_id` and no workspace)
    otherwise, so `bound = usable now` cannot be a lie a subsequent resolve
    exposes. The classifier also validates runtime-id syntax: an entry whose id is
    syntactically invalid — which `resolve_runtime` rejects outright and `revoke`
    cannot reach — classifies as `shape_mismatch`, never `bound`, closing the gap
    where a valid digest over an illegal id was reported `bound` yet not resolvable. Because attribution is by identity, a same-path replacement — the
    recorded directory renamed away and a fresh directory recreated at the same
    path — reports the fresh directory `available` (its identity is genuinely
    unbound and provisioning it succeeds), while the moved original, if still
    under the root, reports `stale_binding` under the runtime_id that owns that
    identity. So the caller is not left to mistake a reused path for a never-bound
    one, that `available` directory also carries `formerly_bound_runtime_id`: the
    id of the entry that recorded this exact path but whose identity is no longer
    here. It is sourced from the stored path of any non-revoked entry (a revoked
    entry released its path on purpose), so a policy-drifted or shape-mismatched
    entry whose directory was replaced is flagged too, not only the active-then-
    moved case; when several entries name one path the most-constraining supplies
    the id. It is advisory: it never changes `status` (the directory stays
    provisionable) and is deliberately NOT part of the distinctness invariant —
    two `available` directories, one signed and one not, demand the same action.
    The governing invariant: two states that require a different caller
    response never share a `(status, runtime_id, workspace)` representation.
    Classifier ordering is load-bearing. Integrity is decided before policy version
    and independently, so a tampered entry is never read as a benign version drift
    and auto-revoked; digest PRESENCE is part of integrity, so a missing or
    non-string `entry_digest` is `integrity_failed`, not `shape_mismatch`. Every
    value-shape check — the profile fields, a syntactically valid runtime id, a
    `status` that is exactly `active` or `revoked`, and well-formed binding payloads
    — is decided BEFORE the revoked gate, and only an explicit revocation (`status ==
    "revoked"` or a revocation-log tombstone) releases a directory. An unknown but
    validly signed `status` is therefore blocking (`shape_mismatch`), never silently
    released, and no value defect can be laundered into a released state by editing
    `status`. When one directory is named by several entries, discovery reports the
    most constraining state (`integrity_failed` > `shape_mismatch` >
    `policy_version_mismatch` > `stale_binding` > `bound` > `revoked`); the provision
    guard selects among conflicting entries by this SAME precedence, so which
    occupant it names is independent of registry insertion order and never
    contradicts what discovery reports for that directory. Two active entries on one
    directory remain hard corruption. The same classification
    governs `resolve_runtime`, which rejects every non-active state with a
    distinct message, and `provision_runtime`, whose one-to-one binding guard
    compares that same provisioned device/inode identity — never the path string,
    so a renamed directory reached through a symlink cannot be bound twice — and
    reads each entry's status through the classifier, so a corrupted status field
    cannot make an occupant vanish from the guard. The guard fails closed on an
    entry whose binding it cannot read rather than skipping it (a skip is the
    allow direction). Every rejection it raises names the runtime_id that holds the
    directory, the path that entry recorded, and the operation that actually clears
    it — never a generic "another active runtime" the caller cannot act on, and
    never an operation that does not work. For an active or policy-drifted occupant
    that is revoke. For a tampered (`integrity_failed`) or malformed
    (`shape_mismatch`) entry there is no safe self-service clear, and that is a
    BEHAVIOR, not merely a message: `revoke_runtime` admits only an entry the
    classifier rules ACTIVE or `policy_version_mismatch` (a stale binding is included
    — the classifier rules it ACTIVE and discovery downgrades it only for display)
    and refuses every other entry — tampered, malformed, unknown-status, or already
    revoked — before it writes anything. The admission check runs BEFORE the first
    persistence deliberately: revocation appends a tombstone before it touches the
    entry, and a tombstone alone releases the directory (the classifier honours the
    revocation log), so a check placed after the append would release identity even
    on a refused call — irreversibly, because the log is append-only. Refusing up
    front also means revoke never re-signs a tampered entry (which would erase the
    integrity signal) and never re-adds a dropped `entry_digest`/`status` to launder
    a broken entry into a released state. Each rejection message names this admission
    rule rather than a per-subtype capability claim (so no message can be falsified
    by a shape subtype it did not enumerate), and `resolve_runtime` reports the same
    escalation for the same states. The guard parses both stored bindings —
    `agent_dir` and `workspace` — before any identity comparison, so an entry with
    either binding unreadable makes the guard refuse every provision; discovery reads
    the same two fields and fails closed for the whole listing to match. A revocation
    log present without its registry, and any non-revoked entry whose stored
    `agent_dir` or `workspace` binding cannot be read, are broken control-plane states
    — provisioning refuses to re-initialize over an orphaned log and cannot rule out a
    conflict on a binding it cannot read — so discovery fails closed rather than
    reporting any directory `available`. A legitimately revoked entry, by contrast,
    has released its directory, which is reported `available` — matching provision,
    which lets a new-id binding through; such an entry always has well-formed
    bindings, because a malformed binding payload classifies `shape_mismatch` ahead of
    the revoked gate, so a "revoked entry with an unreadable binding" cannot arise. Revocation
    additionally writes an
    append-only tombstone before the mutable registry, so a stale full-registry
    snapshot cannot reactivate an id. The versioned registry declares this log
    mandatory: a missing, unreadable, malformed, or mismatched log rejects
    resolve/provision rather than being treated as an empty history. Its POSIX
    directory is owner-only (`0700`) and its lock, temporary, registry, and
    tombstone files are owner-only (`0600`), independent of umask; existing
    registry artifacts are tightened before use. This Phase A registry fails closed on Windows until an
    equivalent owner-only ACL adapter exists. The local control plane is its
    only supported writer: manual or third-party mutation is unsupported and
    malformed/rollback state is rejected rather than treated as authority. A
    revoked entry denies only future profile spawns; it does not terminate an
    already-running ACP host or invalidate a turn already in progress. Incident
    response that must stop existing work must separately stop that host. This
    is a controlled-entrypoint guard,
    not host isolation: an OS principal able to edit the registry or launch the
    generic `--agent-dir` command remains outside this profile's threat model.
    The profile flag is matched by its exact `--profile` spelling only:
    abbreviated long-option forms (`--prof`, `--p`, `--pro=`) are rejected with
    argparse's usage error (`exit 2`), so a constrained `puffo-v0`/`puffo-v1`
    launch cannot be smuggled past a trusted caller that classifies the launch on
    the literal `--profile` token.
    The required Puffo integration seam is outside this repository: before ACP
    spawn, Puffo's trusted ACP driver must derive its `ValidatedLaunchPlan`
    (executable, complete argv, environment, workspace, and empty MCP session
    plan) from operator-managed binding/configuration, then emit only this
    profile command and opaque id. `puffo-v0` identity binding is valid only
    through that ACP entrypoint; Puffo's OpenCode driver is explicitly excluded.
    `puffo-v1` consumes that same immutable identity/workspace binding but has
    one intentionally different session ingress: `mcpServers` must contain
    exactly one local stdio descriptor whose name is `puffo` and whose arguments
    are exactly `-m puffo_agent.mcp.puffo_core_server`. Its interpreter path is
    deployment-specific and is validated only as the ordinary non-empty absolute
    stdio command. Its ordinary unique string name/value environment must
    include a non-empty `PUFFO_LOCAL_SERVICE_TOKEN`; all other names and values pass through
    unchanged and are never logged. Environment is not service identity or a
    hostile-peer boundary: an executable path and Python environment can change
    what code runs. Service identity is the unique service, exact name/module,
    and trusted Puffo-to-LingTai startup boundary; a future hostile-peer defense
    needs a launch nonce/capability, not an environment-name list. No second
    service, alternate name/module, arbitrary MCP descriptor, missing local
    service token, an empty local service token, or missing authenticated Driver authority is accepted. This
    means *all* tools exported by that one Puffo service are available; it does
    not mean arbitrary MCP ingress. `puffo-v0` remains strictly `mcpServers: []`.

## Contract tests

`tests/test_acp_stdio.py` pins request-id and error-taxonomy conformance,
initialize/session/prompt/update/end-turn framing,
permission response arbitration and fail-closed teardown,
cancel settlement, ResourceLink projection, fixed failure redaction, single-
session/busy/unsupported errors, strict JSON line framing, invalid UTF-8, EOF,
blocked coordinator/prompt output, FIFO/generation/queue-full/write-failure paths,
Agent-stop-with-open-stdin, Windows duplicate-before-cleanup, typed quiescence,
and CLI Python-stdout quarantine/hard-exit ownership.
`tests/test_puffo_v0_profile.py` pins opaque-id provisioning/resolution,
tamper/revocation rejection, full-tool composition, fixed-workspace and
empty-session-MCP rejection, authenticated-adapter admission, profile CLI
composition, and rejection of abbreviated `--profile` flag spellings. `tests/test_driver_authority_adapter.py` pins the inherited root
endpoint configuration and its fail-closed missing/malformed/derived-role
outcomes. `tests/test_correlated_turns.py` independently proves an
untrusted inbox event cannot reach provider dispatch under this profile policy.
`tests/test_provider_admission.py` independently proves a missing, denied, or
indeterminate provider admission cannot reach the underlying provider service;
that each provider request needs a new decision rather than reusing a previous
grant; that the typed call class is not inferred from request text; and that
the real non-streaming, streaming, Soul consultation, rate-gated, and reused
worker dispatch boundaries preserve admission rather than treating a direct
proxy test as production-path proof.
`tests/test_execution_workspace.py`, `tests/test_turn_events.py`, `tests/test_turn_permissions.py`, `tests/test_tool_executor.py`, `tests/test_session_mcp.py`, and the ACP
wire tests pin workspace rooting/escape/isolation, stdio validation, atomic
publication/rollback, collisions, and close/EOF ownership.
`tests/test_correlated_turns.py` pins the consumed Core Port's normal, matching
active cancel, pending-cancel isolation, failure, and shutdown settlement.
`tests/test_puffo_admission_witness.py` pins the Puffo admission committed-fact:
receipt extraction rules (structured field vs. plain-text last-marker,
malformed/absent, synthesized, `read_inbox` no-receipt), the
`sha256(toolCallId ‖ 0x00 ‖ receipt)` binding, and the settle-point wire-scan
(commit-interrupted → no fact, success → fact, parallel same-name → two correct
facts, rolled-back-vs-survivor, per-turn idempotency, and the turn-start
watermark that prevents cross-turn re-fire); `tests/test_acp_stdio.py`
additionally pins the reliable ACP emit — no cosmetic suppression, idempotency,
and teardown non-delivery.

## Maintenance

Follow the frontmatter maintenance block and the
[`MANUAL.md`](MANUAL.md) operator procedure. Check the current stable ACP v1
specification before changing any method or wire shape; record deliberate scope
limits rather than advertising omitted capabilities. Do not introduce an ACP SDK
or optional-dependencies section for this standard-library slice.

## Driver authority client

`driver_authority.py` is an isolated AF_UNIX client for the Puffo Driver
admission protocol. `puffo-v0` composition may consume exactly one
`LINGTAI_DRIVER_AUTHORITY_FD` locator and must install a fail-closed Port pair
when it is absent, invalid, or not a root endpoint; this does not give the
protocol client ownership of profile composition. Every hello and decision
request carries a fresh `call_id`;
a missing or mismatched response id closes the transport and fails closed.
The typed `source` field is the sole authority for where a non-grant
originated; consumers MUST NOT infer that origin from `reason_code`. A reason
code is a stable policy label, not an origin namespace: in particular,
`nested_derived_launch_denied` is valid with either `DRIVER` (the remote
authority refused a request) or `LOCAL_POLICY` (the client rejected an
impossible nested request before exchanging). Consumers that need different
handling for those cases MUST branch on `source` as well as any policy reason
they display or record.
Granted derived-launch endpoints are opaque, one-use leases and remain within
the typed in-memory launch decision only. `DriverDerivedLaunchAdmissionAdapter`
performs that narrow projection; it does not serialize, consume, or otherwise
hand off the lease. This module does **not** compose ACP profiles or launch
daemons, avatars, supervisors, or managers; those consumers belong to separate
layers.
