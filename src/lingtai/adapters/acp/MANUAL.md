---
related_files:
  - src/lingtai/adapters/acp/CONTRACT.md
  - src/lingtai/adapters/acp/ANATOMY.md
  - src/lingtai/adapters/acp/BEHAVIORS.md
  - src/lingtai/adapters/acp/driver_authority.py
  - src/lingtai/adapters/acp/puffo_v0.py
  - src/lingtai/adapters/acp/server.py
  - src/lingtai/cli_acp.py
  - src/lingtai/cli_puffo_v0.py
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/kernel/turns.py
  - src/lingtai/kernel/execution_workspace.py
  - src/lingtai/kernel/turn_events.py
  - src/lingtai/kernel/turn_permissions.py
  - src/lingtai/kernel/tool_executor.py
  - src/lingtai/services/session_mcp.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - tests/test_acp_stdio.py
  - tests/test_puffo_v0_profile.py
  - tests/test_correlated_turns.py
  - tests/test_execution_workspace.py
  - tests/test_turn_events.py
  - tests/test_turn_permissions.py
  - tests/test_tool_executor.py
  - tests/test_session_mcp.py
  - tests/test_lifecycle_daemon_shutdown.py
maintenance: |
  Keep this manual's launch, wire, cancellation, diagnostics, scope, and
  current-spec limitation aligned with the ACP adapter, Core turn Port, CLI
  composition, governed twins, and tests. This manual must remain reachable from
  both ACP CONTRACT.md and ANATOMY.md; update all three when behavior changes.
---
# LingTai local ACP v1 stdio manual

## What this capability is

`lingtai-agent acp` lets one local ACP client drive one existing LingTai agent
over standard input/output. It is intended for an editor, terminal UI, or other
local process that can launch an ACP subprocess. The implementation speaks ACP
protocol version 1 directly with the standard library; no ACP SDK or optional
package is required.

This slice supports exactly:

- `initialize` negotiation that returns this Agent's supported `protocolVersion: 1`;
- one `session/new` per process;
- one canonical execution workspace from `session/new.cwd`;
- zero or more session-scoped stdio MCP servers mounted all-or-nothing;
- one active `session/prompt` at a time;
- baseline Text and ResourceLink prompt blocks;
- one-shot fail-closed tool permission and minimal lifecycle projection;
- one completed response projected as `agent_message_chunk`;
- terminal `end_turn`, cooperative `cancelled`, or a fixed JSON-RPC failure;
- `session/cancel` for the active turn.

It deliberately does **not** provide session load/persistence, multiple sessions,
remote MCP servers, additional workspace roots, persistent permission choices,
capability-gated image/audio/embedded-resource content, message/usage streaming,
tool arguments/results/content, remote transport,
authentication, or ACP v2.

Stable ACP v1 requires stdio session MCP and applying `cwd`. This slice implements
both: cwd is canonicalized once and scopes execution-facing File, Shell, guard,
and parallel tool work; stdio servers use stable v1's `name`, absolute `command`,
string `args`, and `{name,value}` env-array shape. It remains a narrow local flow,
not complete general-purpose ACP v1 conformance.

## Launch

Use an already initialized agent directory containing a valid `init.json`:

```bash
lingtai-agent acp --agent-dir /absolute/path/to/existing-agent
```

Configure the ACP client to launch that exact command and communicate over its
stdin/stdout. Do not run the command interactively and type prose into stdin:
each input line must be one complete JSON-RPC object. The agent directory keeps
its ordinary workdir lease, so another live LingTai process cannot safely share
it.

### Constrained Puffo profile

For Puffo Phase A, an operator first provisions an already-initialized,
persistent LingTai identity and its canonical execution workspace locally:

```bash
lingtai-agent puffo-v0 provision \
  --runtime-id puffo-agent-7 \
  --agent-dir /operator/managed/agent \
  --workspace /operator/managed/workspace
```

The controlled Puffo driver then launches only:

```bash
lingtai-agent acp --profile puffo-v0 --runtime-id puffo-agent-7
```

The ACP command accepts no profile `agent_dir` path. The runtime id is resolved
through the local operator registry (`~/.lingtai/puffo-v0/runtime-registry.json`)
to its bound persistent identity and workspace. The profile rejects an unknown,
tampered, or revoked id before constructing the Agent. Revoke a future launch
with `lingtai-agent puffo-v0 revoke --runtime-id puffo-agent-7`.
Revocation does not terminate an already-running ACP host or invalidate its
in-progress turn; stop that host separately when incident response must stop
existing work.

Provision stores each directory's canonical path and POSIX device/inode/owner/
group identity. An active agent directory or workspace may be bound to only one
runtime. At launch the profile rejects symlink retargeting, a changed canonical
path, or a replacement directory at the same path, then rechecks the binding
immediately before Agent construction. This detects normal configuration drift;
it is not host isolation against a same-OS principal that changes the filesystem
after that final check.

`entry_digest` protects the exact registry record, not the complete effective
launch/security configuration. In particular, it does not freeze or hash
`init.json`, presets, executable/argv/environment policy, or addon/plugin
policy. The Puffo driver's versioned launch-plan security projection is a
separate cross-process contract; do not treat this registry hash as its proxy.

The Phase A registry is POSIX-only: it serializes provision/revoke updates,
records terminal revocations in an append-only local tombstone log, and creates
its registry directory as `0700` and registry, tombstone, temporary, and lock
files as `0600`, independent of umask. Loading an older registry tightens its
directory and file modes before use. On Windows the command fails closed until
an equivalent owner-only ACL implementation is available. The `puffo-v0`
control-plane commands are the only supported writers; do not hand-edit the
registry or use a third-party writer. The current versioned registry requires
its tombstone log to exist: missing, unreadable, malformed, or mismatched
history fails closed instead of being interpreted as no prior revocations.

In this profile, `session/new.cwd` must be exactly the provisioned workspace and
`mcpServers` must be `[]`. It is an **identity/workspace-bound full-tool ACP
profile**: local, operator-managed capabilities remain available across initial
composition and refresh. It does not add an `external_send` human-confirmation
rule and does not promise shell confinement, workspace-only writes, network
egress control, no background descendants, or OS process containment.

### Puffo v1 full-service session ingress

`puffo-v1` uses the same already-provisioned runtime id and the same Driver
authority handoff, but selects the one intended change in the ACP session
policy:

```bash
lingtai-agent acp --profile puffo-v1 --runtime-id puffo-agent-7
```

Unlike `puffo-v0`, this process refuses to start without a successfully
authenticated root Driver-authority endpoint. That startup requirement makes
the driver handoff the sole path that can reach the fixed MCP ingress.

The Puffo driver supplies exactly one `mcpServers` descriptor to `session/new`.
LingTai requires its name to be `puffo`, its arguments to be exactly
`-m puffo_agent.mcp.puffo_core_server`, and `PUFFO_LOCAL_SERVICE_TOKEN` in its
ordinary unique name/value environment. Other environment entries are passed
through unchanged, and their values are never logged; no env-name allowlist is
used because Puffo can add deployment-specific variables independently. Env is
not an identity or hostile-peer check—an interpreter and Python environment can
change the executed code. The identity anchor is the unique service, exact
name/module, and trusted Driver startup boundary; a future hostile-peer defense
would need a launch nonce/capability. The command path is the local Python
interpreter chosen by the Puffo installation, so LingTai validates its generic
absolute stdio shape rather than a machine-specific path. The driver must pass
the whole descriptor through unchanged except for the ACP field conversion:
every tool the one Puffo service exports is available. A missing/extra service,
alternate module/name, missing local-service token, or any other MCP descriptor
fails the `session/new` request. This is not a general MCP-import feature, and
it does not weaken `puffo-v0`, which remains empty-only.

Its capability-side boundary is ACP-only turn initiation. A direct ACP prompt
is tagged as an authenticated driving-adapter turn; a profile policy denies any
legacy, inbox, task-card, alarm, daemon, mail/MCP wake, or other independent
event before it can dispatch a provider/model turn. This applies to **every**
root provider/model turn, not merely the inbox dispatch step. The Core
provider-call Port is crossed immediately before each such request, so a
missing or denied parent cannot fall through to the provider service. The
currently available daemon and avatar tools still use their historical
independent execution routes; they are not covered by this root-only Core
slice. The future derived adapter has an explicit unconnected outcome:
`derived_admission_port_unconnected` rejects before provider I/O rather than
acting as a permissive placeholder. That outcome does not yet cover the
historical daemon/avatar routes themselves. Before treating `puffo-v0` as a
complete all-turn profile, the driver must provide host-mediated derived
admission for each daemon/avatar provider call. Future child turns must carry
verified ancestry from an admitted prompt, and the driver must decide at every
actual provider call against then-current authority rather than reusing a
turn-start grant.
This is an initiation boundary, not content provenance: non-ACP systems can
still write state which a later authenticated prompt may cause the model to
read. Existing minimal ACP permission projection does not alter launch inputs,
and no interrupted side effect becomes retry-safe.

LingTai's local stdio process does not authenticate a Puffo user or route by
itself. “Authenticated adapter” means the controlled Puffo ACP driver has
authenticated the remote request and exclusively owns this profile process's
stdin; that first gate is required outside this repository. A same-OS principal
able to bypass the driver can invoke generic local APIs and is outside this
profile's host trust boundary.

`puffo-v0` is a second gate for this controlled entrypoint, not complete host
isolation. A principal with the same OS authority can edit the local registry or
invoke the generic `lingtai-agent acp --agent-dir ...` command directly; that is
the host trust boundary, not a guarantee provided by this profile.

### Puffo driver integration boundary

The Puffo ACP driver owns the first gate. Before it starts this process, its
single `open()` seam must derive a `ValidatedLaunchPlan` from the authorized
Puffo route, local binding, and operator-managed configuration. That plan covers
the executable, complete argv, environment, workspace, and the fixed empty MCP
session plan; `_spawn` must accept only that validated type. The only
identity-bound command the seam may emit is:

```bash
lingtai-agent acp --profile puffo-v0 --runtime-id <operator-bound-id>
```

Lingtai is deliberately not a substitute for that driver proof: it has no way
to attest to another process's executable, argv, or environment. Its second
gate accepts only the opaque id and re-resolves the canonical local identity and
workspace. A message sender cannot submit either a LingTai identity id or a
filesystem path.

The Puffo OpenCode driver is explicitly outside this trust path. It must not
launch a `puffo-v0` bound identity or be presented as an alternative identity
binding entrypoint; only the ACP driver participates in this profile.

For each `puffo-v0` process, that ACP driver also passes exactly one already
open root Driver-authority AF_UNIX stream descriptor through
`LINGTAI_DRIVER_AUTHORITY_FD`. LingTai consumes and removes this descriptor
locator before constructing the Agent. A usable root endpoint becomes the
profile's provider-call Port and is projected to its derived-launch Port.
Missing, malformed, unavailable, or derived-role endpoints instead install the
typed `driver_authority_unavailable` fail-closed pair; the profile never falls
back to generic runtime policy for either boundary. This B7 configuration step
does not supervise a process or transfer a derived child endpoint: B8 owns
supervisor and child-FD lifetime.

## Wire sequence

A minimal client sequence is:

1. Send `initialize` with `protocolVersion: 1`.
2. Send `session/new` with an absolute existing-directory `cwd` and either
   `mcpServers: []` or strict stdio entries. Startup is all-or-nothing.
3. Retain the returned opaque `sessionId`.
4. Send `session/prompt` with that id and a non-empty Text/ResourceLink block
   list. ResourceLink metadata is forwarded to Core as compact text; this slice
   does not fetch the URI.
5. For each tool, answer `session/request_permission` with
   `{"result":{"outcome":{"outcome":"selected","optionId":"allow_once"}}}`
   to permit it, or a nested reject/cancel outcome to deny it. Then read the
   minimal lifecycle `session/update` frames, followed by zero or one
   completed Text `agent_message_chunk`, then the response carrying
   `stopReason: "end_turn"`.
6. While step 4 is unresolved, a client may send the `session/cancel`
   notification for the same session. Keep reading: the original prompt request,
   not the cancel notification, eventually receives `stopReason: "cancelled"`.

Example shapes (one compact object per real line in an actual transport):

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/absolute/client/cwd","mcpServers":[]}}
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"<returned-id>","prompt":[{"type":"text","text":"Hello"}]}}
```

ACP v1 request ids may be strings, signed 64-bit integers, or explicit `null`; the response
echoes an included id exactly. Omitting `id` instead makes the message a
notification, so these two wire shapes are not interchangeable.

## Tool lifecycle projection

For each tool call, the server first emits a pending `tool_call` and a
`session/request_permission` carrying a plain `ToolCallUpdate` with the same
safe id/title/status, no `sessionUpdate` discriminator, and only Allow once /
Reject options. Only the exact nested selected Allow once outcome received after the request frame's
physical write+flush boundary dispatches. Response arrival and the post-flush
published bit linearize under the state lock, so a pre-flush response stays denied
even if it resumes after publication. The per-request publication lock does not
hold the global state lock over client stdout. Approval then emits
`tool_call_update` with `in_progress`; later
states use `tool_call_update` with that id and status. Status is only
`in_progress`, `completed`, or `failed`; local guard denial is `failed` without
executing the tool. For parallel dispatch, workers announce only start and the
collector assigns the one terminal state from the outcome it actually accepts;
future exceptions, timeout, or cancellation therefore cannot leave `in_progress`
or be overwritten by a late completion. Accepted updates remain FIFO-before the prompt terminal
response, while events after close, generation change, or terminal claim are
dropped.

This is deliberately metadata-only. The Adapter does not send tool arguments,
results, content, locations, `rawInput`, `rawOutput`, or internal error text.
Observer/projection exceptions cannot change Core tool execution. The initial
pending record becomes announced only after its frame flushes; a pre-emission
denial uses a valid initial failed record. If denial races an already-started
pending write, the writer closes the physically emitted record with an adjacent
failed update and suppresses the request; Core lifecycle observation still never
blocks behind stuck client stdout. Permission
broker errors, timeout, cancellation, or transport failure deny; fatal bounded
queue/framing failure still aborts the transport.

## Cancellation semantics

Cancellation is cooperative. LingTai correlates it to the one active Core turn
and prevents that request from being mistaken for a later turn, but it cannot
promise that a provider HTTP call or already running tool stops immediately.
The reader remains available for cancel while a worker waits for settlement. A
cancel request that linearizes before terminal settlement wins; a cancel after
settlement is a no-op.

EOF or Ctrl-C closes the adapter and requests cancellation. Prompt frames that
have not crossed the writer's start check are suppressed; an OS write already in
progress may finish, so an update can exist without its final response if close
wins between them. The process then requests a bounded typed Agent stop. Services,
heartbeat, and workdir lease are released only after both run loop and any retained
poisoned-provider Future are quiescent; otherwise the ACP owner hard-exits while
ownership is still held. Agent-initiated stop/refresh returns the coordinator even
if stdin remains open; the ACP connection is not preserved across refresh.

## Diagnostics and recovery

The Adapter and Python `sys.stdout`/`print` path are protocol-only. Configure the
client to capture stderr for boot reader outcomes, logs, and diagnostics. This
slice does not redirect native fd 1, previously captured stdout objects, or child
stdout: code launched in this host must not use those paths. Common explicit errors:

- non-integer protocol version: invalid params (a different integer negotiates to
  this Agent's supported version `1`, which the client must accept or close);
- relative, missing, or non-directory `cwd`: invalid params;
- malformed, duplicate, HTTP, or SSE `mcpServers`: invalid params; startup or
  tool-name collision closes earlier clients and publishes nothing;
- non-empty `additionalDirectories`: unsupported (additional roots are not advertised);
- second `session/new`: unsupported;
- second prompt while one is active: session busy (`-32010`);
- session methods before initialization: server not initialized (`-32011`);
- ResourceLink without non-empty `uri`/`name` or with invalid metadata: invalid params;
- image/audio/embedded-resource prompt block: unsupported;
- failed Core turn: fixed `LingTai turn failed` Internal error, with details kept
  out of the ACP wire.

The two local session-state codes are distinct from ACP v1's predefined
authentication (`-32000`) and resource-not-found (`-32002`) errors, and from
this Adapter's existing session-not-found code (`-32001`); clients must not
interpret them as any of those errors.

After correcting client input, launch a fresh process if session creation already
succeeded; the one-session state is intentionally process-local. For agent boot
or provider problems, inspect stderr and the existing agent `logs/` artifacts.
Do not work around an error by placing logs on stdout or by sending unsupported
fields and assuming they were honored.

## Why the boundary is narrow

ACP is an external driving protocol, while LingTai Core owns turn execution.
The Adapter therefore translates into `BaseAgent.submit_turn` and waits on a
protocol-neutral handle/result instead of reading chat history or provider
objects. This keeps wire/session policy outside Core and makes cancellation and
terminal settlement reusable by later driving adapters. Broader ACP capabilities
should be added as separately accepted vertical slices, not guessed inside this
one.

## Driver authority protocol client

The `puffo-v0` ACP composition consumes one launcher-injected
`LINGTAI_DRIVER_AUTHORITY_FD` descriptor before Agent construction. It removes
that locator from the environment immediately; the descriptor is never a
serializable credential. A missing, malformed, unusable, or derived-role
endpoint installs a fail-closed admission Port instead of falling back to the
generic profile policy. Other composition layers do not read this variable.
The peer must reply to hello and every request with the exact request `call_id`.
Any timeout, malformed frame, unexpected descriptor, or correlation mismatch
invalidates the stream; recreate the client rather than retrying it. A derived
grant's endpoint is a one-use opaque lease, not an identifier that may be
serialized or reopened.
