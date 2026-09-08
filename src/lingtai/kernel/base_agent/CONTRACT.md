---
name: agent-runtime
contract_version: 1
root_contract: CONTRACT.md
related_files:
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/kernel/base_agent/BEHAVIORS.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/kernel/config.py
  - src/lingtai/kernel/meta_block.py
  - ENVIRONMENT_VARIABLES.md
  - tests/test_meta_block.py
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - src/lingtai/kernel/base_agent/turn.py
  - src/lingtai/kernel/base_agent/worker_recovery.py
  - src/lingtai/kernel/turns.py
  - src/lingtai/kernel/execution_workspace.py
  - src/lingtai/kernel/turn_events.py
  - src/lingtai/kernel/turn_permissions.py
  - src/lingtai/kernel/provider_admission.py
  - docs/references/provider-admission.md
  - src/lingtai/adapters/acp/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/tools/system/karma.py
  - tests/test_aed_recovery.py
  - tests/test_notification_sync.py
  - tests/test_silence_kill.py
  - tests/test_system.py
  - tests/test_system_declared_plugin.py
  - tests/test_karma.py
  - tests/test_perform_refresh_handshake.py
  - tests/test_tool_result_restore_after_continuation_failure.py
  - tests/test_correlated_turns.py
  - tests/test_execution_workspace.py
  - tests/test_turn_events.py
  - tests/test_turn_permissions.py
  - tests/test_provider_admission.py
  - tests/test_acp_stdio.py
  - src/lingtai/kernel/process_match.py
  - src/lingtai/kernel/process_scan.py
  - src/lingtai/adapters/posix/process_scan.py
  - src/lingtai/adapters/windows/process_scan.py
  - src/lingtai/adapters/process_scan.py
  - src/lingtai/kernel/workdir_lease/CONTRACT.md
  - src/lingtai/kernel/agent_presence/CONTRACT.md
  - src/lingtai/kernel/refresh_watcher/CONTRACT.md
  - src/lingtai/kernel/lifecycle_clock/CONTRACT.md
  - src/lingtai/kernel/stream_progress/CONTRACT.md
  - src/lingtai/kernel/notification_store/CONTRACT.md
  - src/lingtai/kernel/event_journal/CONTRACT.md
  - src/lingtai/kernel/session_stats/CONTRACT.md
  - src/lingtai/agent.py
  - src/lingtai/cli.py
  - docs/references/windows-support.md
  - tests/test_agent.py
  - tests/test_lifecycle_daemon_shutdown.py
  - tests/test_lingtai_facade.py
  - tests/test_system_sleep_alarm.py
  - tests/test_process_scan.py
  - tests/test_process_match.py
  - tests/test_windows_import_graph.py
  - tests/test_cli.py
maintenance: |
  <!-- CANONICAL-MAINTENANCE v2 BEGIN -->
  This component contract is governed by the root CONTRACT.md. Keep
  related_files complete and repo-relative: the paired ANATOMY.md, Core Ports,
  every production Adapter, selector, contract tests, and directly relevant
  component contracts belong here. Re-read this contract whenever a linked
  boundary changes. Update the Ports, affected Adapters, selector, contract
  tests, and this contract in the same change; update the paired Anatomy when
  structure or composition also changes; bump contract_version for a breaking
  Port-contract change. If code and contract disagree, treat the disagreement
  as a defect—do not silently rewrite the normative contract to match the
  implementation.
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
  <!-- CANONICAL-MAINTENANCE END -->
---
# Agent Runtime

Stable entry: `lingtai.kernel.agent-runtime.v1`.

## Purpose
Guarded by: [BA001](BEHAVIORS.md#behavior-ba001), [BA002](BEHAVIORS.md#behavior-ba002), [BA005](BEHAVIORS.md#behavior-ba005)


Agent runtime is the composed lifecycle promise of one LingTai agent process:
what it means for an agent to be constructed, launched, observably alive,
refreshed, and stopped in a working directory — on every supported platform.
This contract owns the *composition* of the capability contracts it links
(workdir lease, agent presence, refresh watcher, lifecycle clock, notification
store, event journal) plus the runtime surfaces that previously had no
normative owner: the CLI lifecycle host's duplicate-launch guard and stop
signals, the canonical agent-run process matcher, the CPR relaunch mechanism,
and the per-platform capability profile. It states each promise once and links
onward; the linked capability contracts remain the normative source for their
own boundaries.

This is also the kernel counterpart of the cross-repository runtime
coordination with the LingTai TUI. Clause acceptance state is explicit: no
clause is marked mutually `accepted` until both repositories' contract
revisions reference each other (see `## Contract rules`).

## Behavior

Agents and coding agents MUST treat the composed lifecycle as one ordered
truth. Construction acquires the workdir lease exactly once (10 s grace) and
rolls back on any construction failure. A live agent's observability is
manifest-first presence plus heartbeat freshness (`.agent.json`,
`.agent.heartbeat`) — never process-table visibility or lock-file existence.
Stop first requests execution quiescence and returns a typed `StopResult`. If
the run loop or retained poisoned-provider Future survives the bounded deadline,
`TIMED_OUT` retains Task Card, Agent services, daemon/session/mail/journal, final
manifest, heartbeat, and lease ownership. Only a proved `STOPPED` path tears down
Task Card → Agent services → daemon → session/mail/journal → final manifest →
heartbeat withdrawal → lease release, with heartbeat fresh through teardown.
Refresh is the `.refresh` → `.refresh.taken` handshake with
exactly one detached watcher spawn; a refresh attempt that fails or raises
before that handoff leaves the live process able to attempt refresh again,
and only a completed handoff is terminal for the process
([BA002](BEHAVIORS.md#behavior-ba002)). Cooperative stop requests are the signal
files (`.suspend`, `.sleep`, `.interrupt`), consumed by the agent's own
heartbeat loop; OS signals and console events are translated into that channel
by the CLI host, never handled ad hoc elsewhere. Capability support is a
per-platform matrix (see the
[`windows-support`](../../../../docs/references/windows-support.md) manual);
an unsupported capability fails loudly at its own selector or composition
gate — never silently degrades, never no-ops.

A delayed self-sleep persists exactly one atomic `<workdir>/.alarm` absolute
wall-clock deadline before it transitions ASLEEP. Heartbeat reads that file
cheaply under the same capability-local lock as arming; before deadline it is a
no-op, and at/after deadline it uses the existing ordinary system-notification
producer and sync wake path. A stable stored-deadline-derived ref/idempotency
identity makes retry after a publish/consume crash window safe. A successful
publish consumes the file; a failed publish leaves it retryable. Early wake does
not cancel the file, and a new delayed sleep last-writer-wins overwrites it.
Malformed/unreadable state remains visible through a bounded once-per-unchanged
problem `sleep_alarm_malformed` event; no scheduler, cross-process lock, or
new Store protocol exists.

## Port

This contract owns one new Core Port and composes the linked ones:

- `AgentProcessScanPort` (`src/lingtai/kernel/process_scan.py`) — best-effort
  observation of visible processes as `(pid, command_line)` pairs for the
  duplicate-launch guard. One operation, `iter_process_commands()`; yields
  nothing when the process table is unavailable. It is defense-in-depth beside
  the workdir lease, which remains the exclusion authority.
- `match_agent_run(cmdline, working_dir)` and
  `match_agent_acp(cmdline, working_dir)`
  (`src/lingtai/kernel/process_match.py`) — separate canonical pure matchers for
  ordinary agent-run and local ACP-host command lines. Duplicate-launch guards
  use both before stale signal cleanup; refresh-watcher stale-process recovery
  deliberately uses only `match_agent_run`. The doctor skill carries a stdlib-
  only run-matcher copy pinned to the run test matrix.
- Composed Ports: `WorkdirLeasePort`, `AgentPresenceStorePort`,
  `RefreshWatcherPort`/`RefreshWatcherProcessPort`, `LifecycleClockPort`,
  `NotificationStorePort`, `EventJournalPort` — each normatively owned by its
  linked contract.
- `ProviderCallAdmissionPort` (`src/lingtai/kernel/provider_admission.py`) —
  optional, technology-neutral outbound decision Port for a constrained
  composition. Core installs an opaque, task-local root or derived parent and
  crosses the Port immediately before every concrete provider `send`,
  `send_stream`, or direct `generate`. A missing parent, malformed decision, or
  denial fails before provider I/O. Correlation is audit-only, never authority;
  transport/authentication remains adapter-owned.
- `StreamProgressPort` (`src/lingtai/kernel/stream_progress/CONTRACT.md`) —
  optional, technology-neutral RAM-only progress publisher composed through the
  defaulted-`None` `stream_progress_factory` kwarg. `BaseAgent` calls it once
  with its stable `agent_id` only when construction receives `streaming=True`,
  then hands the Port to `SessionManager`; an explicit false never creates a
  publisher or endpoint. The outer composition root supplies the effective
  streaming policy.

## Adapters

Platform profiles are selector-composed at the composition roots
(`lingtai.cli.build_agent`, `lingtai.Agent`); Core never branches on platform:

- **POSIX profile** — `PosixWorkdirLeaseAdapter` (flock),
  `PosixRefreshWatcherAdapter` (+ POSIX entrypoint/process mechanism),
  `PosixAgentProcessScanAdapter` (`ps`), CLI stop signals SIGTERM/SIGINT,
  CPR relaunch via `start_new_session` detachment.
- **Windows profile** — `WindowsWorkdirLeaseAdapter` (msvcrt byte-0/length-1),
  `WindowsRefreshWatcherAdapter` (+ Windows entrypoint/process mechanism),
  `WindowsAgentProcessScanAdapter` (CIM query), CLI stop signals
  SIGINT/SIGBREAK, CPR relaunch via the shared detached creation flags
  (`lingtai.adapters.windows._win32`).
- **Portable transports** — the presence, notification, event-journal, mail,
  migration-workspace, and Git adapters under `adapters/posix/` are portable
  filesystem/subprocess implementations selected on both platforms; their
  `Posix*` names are historical. Certification is per-platform contract-test
  execution, not duplicate classes.
- Any other platform fails loudly at the first selector
  (`NotImplementedError`), and the process-scan selector returns `None`
  (guard honestly absent; the lease remains the authority).

## Contract rules

Clause IDs are stable; each rule composes the linked normative source.

1. `agent-runtime.paths.v1` — One working directory owns one agent. The
   runtime artifacts are `.agent.json` (manifest), `.agent.heartbeat`
   (liveness), `.agent.lock` (lease), the signal files
   (`.suspend`/`.sleep`/`.interrupt`/`.refresh`/`.refresh.taken`/`.prompt`/
   `.clear`/`.inquiry`/`.rules`), `.alarm` (the one self-sleep absolute
   deadline), `.notification/`, `logs/`, and `history/`. Artifact names and
   meanings are frozen; observers may read,
   only the owning agent/watcher mutates.
2. `agent-runtime.presence.v1` — Liveness truth is manifest-first presence
   plus heartbeat freshness, per
   [`agent_presence/CONTRACT.md`](../agent_presence/CONTRACT.md). Process
   visibility, PID files, and lock-file existence are never liveness.
3. `agent-runtime.launch.v1` — Construction requires the six injected Ports
   with no defaults, acquires the lease once with 10 s grace per
   [`workdir_lease/CONTRACT.md`](../workdir_lease/CONTRACT.md), and rolls
   back the lease on construction failure. The CLI host refuses to boot when
   the duplicate-launch scan observes a live same-workdir agent run
   (canonical matcher over `AgentProcessScanPort` observations, own PID
   excluded); an unavailable scan falls through to the lease.
4. `agent-runtime.stop.v1` — `BaseAgent.stop(timeout)` returns public immutable
   `StopResult(status, run_loop_alive, provider_worker_alive)` with `StopStatus`
   `STOPPED` or `TIMED_OUT`. One deadline covers the run-loop join and any retained
   poisoned-provider Future. `TIMED_OUT` performs no Task Card, Agent-service,
   daemon/session/mail/journal, manifest, heartbeat, or lease teardown. After
   quiescence, stop order is Task Card → Agent services → daemon → session/mail/
   journal → final manifest → heartbeat withdrawal → lease release; all post-proof
   cleanup is inside the issue-661 release `finally`. Cooperative stop arrives via
   signal files; the CLI host hooks each platform's real stop signals (POSIX
   SIGTERM/SIGINT; Windows SIGINT/SIGBREAK) and translates them into `.suspend` +
   shutdown, nothing else. Process hosts must retry after quiescence or terminate
   while ownership remains held; they must never equate settled callers with
   execution quiescence.
5. `agent-runtime.refresh.v1` — Refresh is the `.refresh` → `.refresh.taken`
   handshake with exactly one detached watcher spawn per
   [`refresh_watcher/CONTRACT.md`](../refresh_watcher/CONTRACT.md), on every
   supported platform. Permanent failure publishes the bounded, redacted
   artifact + high-priority notification; it never deletes `.agent.lock`.
   Per-process refresh is single-flight: near-concurrent requests coalesce
   into one watcher, and a request arriving after a completed handoff is
   skipped (`refresh_skipped`, `refresh_already_in_progress`). That
   single-flight claim is released by every ordinary failure or exception
   before `spawn_detached` returns — including no launch command, a raising
   launch-command build (the wrapper's configured-`venv_path` precheck), a
   missing watcher Port, failed ACK setup, request construction, or a
   raising spawn — so a corrected later refresh in the same process is not
   skipped. Failures before handshake normalization leave no handshake or
   cancel/shutdown mutation; failed ACK setup does not spawn or signal
   shutdown. Once `.refresh.taken` has been established, a later pre-handoff
   exception may leave that handshake artifact, but still releases the
   claim and does not signal cancellation/shutdown. A completed handoff
   (the Port's normal return) keeps the claim for the rest of the process
   lifetime even if logging or shutdown signaling after it fails.
   Guarded by: [BA002](BEHAVIORS.md#behavior-ba002)
6. `agent-runtime.process-identity.v1` — An agent-run process is identified
   by its command line through the canonical matcher; runtime relaunches
   (watcher, CPR, avatar) always use the module form
   (`<python> -m lingtai run <dir>`). PID alone is never authority. CPR
   success is asserted only through fresh target presence/heartbeat, never
   through the child PID. Detachment is `start_new_session` on POSIX and the
   shared detached creation flags on Windows.
7. `agent-runtime.failure.v1` — Unsupported capability = loud failure at the
   owning selector/composition gate with an exact reason. There is no silent
   fallthrough to another platform's mechanism, no no-op adapter, and no
   capability flag. Known degradations are named in the capability matrix.
8. `agent-runtime.compat.v1` — Platform support is published only as the
   per-capability matrix in
   [`docs/references/windows-support.md`](../../../../docs/references/windows-support.md),
   backed by named test suites/CI lanes; there is no single boolean
   "Windows supported" claim. Cross-repository counterpart state: the LingTai
   TUI's `.agent.lock` byte-0/length-1 duplicate-launch probe (TUI PR #687)
   is the accepted *interop target* consumed by
   [`workdir_lease/CONTRACT.md`](../workdir_lease/CONTRACT.md); every
   `agent-runtime.*` clause above is `proposed` toward the TUI's future
   full-lifecycle counterpart and none is mutually `accepted` until both
   repositories' contract revisions cross-reference each other. This clause
   MUST NOT be marked accepted unilaterally.
9. `agent-runtime.system-prompt-pressure.v1` — Main-agent runtime metadata
   and daemon-local metadata use the shared `kernel/meta_block.py` renderer,
   which reads `LINGTAI_SYSTEM_PROMPT_PRESSURE_RATIO` on each snapshot with a
   default of `0.4`; invalid values fall back to that default. The warning is
   emitted only for strictly greater rendered-prompt/window ratios with valid
   metrics, remains bounded and prompt-body-free, and preserves each caller's
   existing effective-window precedence and local isolation.
10. `agent-runtime.provider-recovery-terminal.v1` — A provider adapter may mark
   its final exception `_lingtai_no_aed_retry` only after consuming a complete,
   request-owned bounded recovery budget. Producer and main-turn consumer MUST
   use the shared kernel replay-terminal representation: every provider-owned
   exception is wrapped in an exact kernel-owned `LLMReplayTerminalError`, and
   only that exact type may carry trusted `True` flags. Neither side may read,
   write, dynamically dispatch, or coerce provider-owned marker storage. A
   secondary account callback may inspect only the wrapper's original exception;
   callback failure MUST NOT replace or remove the replay-safe wrapper.
   After that budget is consumed, every ordinary downstream failure—from
   credential recovery through retry wire creation/iteration, response-event
   processing, response finalization, rollback-snapshot construction, and the
   complete post-finalization success bookkeeping tail—MUST fail closed with
   this representation. Any adapter path that
   describes the exception before terminal dispatch, and the main turn loop
   itself, MUST use the shared render-safe description rather than letting
   provider `str`/`repr` hooks escape. The main turn reports the failure as
   terminal, sets the agent's ASLEEP gate, and MUST NOT transient-retry,
   increment AED, rebuild the session, or replay the request. Visible partial
   output MUST acquire the stricter `_lingtai_partial_stream` flag before
   secondary account telemetry, retains priority when flags merge, and ends the
   turn without replay.
11. `agent-runtime.turn-cancel-latch.v1` — Guarded by
   [BA003](BEHAVIORS.md#behavior-ba003). `_cancel_event` is one private,
   process-global, cooperative latch for the current logical turn. Producers
   (`.interrupt`, `.suspend`, `.sleep`, successful refresh, and both official
   and retained direct System self-sleep transitions) MUST request cancellation
   through idempotent `BaseAgent._request_turn_cancel()` while preserving each
   producer's existing file/state/watcher/shutdown ordering. Only a successful
   fresh inbox dequeue may consume a stale latch: after its shutdown re-check
   and before the dequeued message is published ACTIVE or merged. In the awake
   branch, reset MUST precede queued-message concatenation so a cancellation
   requested during merge remains latched. From ACTIVE until the logical turn
   exits, response/tool consumers may observe the latch but MUST NOT clear it;
   an ASLEEP notification synchronization wake likewise MUST NOT clear it. The
   latch suppresses undispatched tool calls and post-tool continuation while
   preserving existing tool-result/history commits. It does not hard-abort a
   provider, preempt a running tool, or by itself identify a request or create a
   terminal result. The correlated inbound-turn boundary in rule 12 composes on
   this unchanged cooperative mechanism rather than strengthening it. See the
   paired [`ANATOMY.md`](ANATOMY.md) for ownership and code routes.
12. `agent-runtime.correlated-turn.v1` — Guarded by
   [BA004](BEHAVIORS.md#behavior-ba004). `BaseAgent.submit_turn` accepts one text
   turn plus optional caller correlation, generic immutable execution workspace
   metadata, a protocol-neutral turn-scoped tool observer, and a separate
   protocol-neutral permission broker, and returns a
   `TurnHandle`; the handle
   settles exactly once as `normal`, `cancelled`, or `failed`, with completed
   response text only on normal settlement. Correlated envelopes are distinct
   from mergeable fire-and-forget text messages and retain inbox serialization.
   Cancelling a pending handle marks only that control; it MUST NOT set the
   process-global latch for the turn ahead. When the matching handle becomes
   current, or is cancelled while current, it composes onto
   `_request_turn_cancel`. Cancellation linearized before settlement wins over a
   concurrently completing normal/failure candidate; after settlement it returns
   false and cannot affect a later turn. Run-loop exit and Agent stop settle all
   live handles so waiters cannot hang across teardown. An unexpected run-loop
   exception after current ownership is published settles that exact control
   `failed` with bounded rendered error detail and is re-raised for supervision;
   the loop's terminal `finally` cancels every other live control, including a
   registered envelope dequeued before `begin_turn`. Racing teardown claims remain
   exactly once under the same registry lock. While the correlated turn is
   current, Core binds its workspace, optional observer, and optional permission
   broker through task-local context and resets all before settlement/loop teardown; parallel tool workers
   receive explicit context copies. The observer receives only bounded tool-call
   id/name/lifecycle state, and observer failure MUST NOT change tool execution or
   settlement. Parallel workers announce start, while the collector exclusively
   claims terminal from the result/exception/timeout/cancellation it accepts, so
   terminal observation cannot disagree with batch outcome. This inbound Port is protocol-neutral:
   adapters may translate ACP or another driver into it, but
   Core types and methods contain no ACP session, JSON-RPC, MCP configuration,
   protocol-specific permission or transport vocabulary. The broker receives only
   tool id/name; absent brokerage passes through, while broker exceptions or
   invalid decisions deny. It still promises no hard provider abort or
   running-tool preemption.
13. `agent-runtime.provider-admission.v1` — Guarded by
   [BA005](BEHAVIORS.md#behavior-ba005). A composition that injects a
   `ProviderCallAdmissionPort` turns the service boundary into the single
   structural provider-call gate. Core binds a `RootProviderAdmission` only
   after the final correlated-turn origin check, and service/session proxies
   call the Port before each provider request. A live refresh that rebuilds
   the concrete provider adapter MUST reapply the same admitted-service
   wrapper before publishing it to the session. A Port returns exactly one of
   `GRANTED`, `DENIED`, or `INDETERMINATE`; only `GRANTED` may reach the
   provider. No bound parent, a malformed Port response, a Port exception, an
   explicit denial, or indeterminate authority MUST prevent the underlying
   provider request. The parent is a Core-private in-memory object;
   correlation ids, paths, registry digests, prompt content, and tool output
   are not credentials. A derived daemon/avatar call uses a typed parent with
   an internal non-serializable handle and crosses the Port again for each
   actual provider call; it cannot reuse a root grant. v0 is explicitly
   one-hop: only a `RootProviderAdmission` can mint a derived parent, and a
   daemon/avatar child cannot mint another model-executing daemon/avatar.
   Driver launch admission MUST reject the same nested request; adding recursive
   derivation requires a new contract with per-hop authority and a recursive
   production-boundary inventory. This is an authorization rule, not a tool
   surface reduction: a full-tool child must be able to make its real
   daemon/avatar request, which Core/Driver then reject before any provider
   I/O. In particular, the historical daemon `EMANATION_BLACKLIST` currently
   filters both capabilities before authorization. The transition is ordered:
   (1) every production derived-launch constructor first routes through an
   observable domain decision; (2a) while the filter remains, a production
   launch-path test reaches that decision and records the principal, requested
   capability, and reason code; (3) only after 2a passes may puffo-v0 remove
   or bypass the `daemon` and `avatar` filters together; (2b) the change that
   removes those filters cannot merge until a production child uses each real
   tool path, receives the denial, and a recording transport proves zero
   provider I/O. Thus 2a proves that the decision already applies on every
   production launch path covered by the inventory before the old safeguard is
   removed; it does not prove the inventory's explicit static blind spots.
   Those require focused review and production-path E2E. 2b proves that the
   opened tool surface reaches the intended refusal rather than empty-passing
   at the old filter. The launch
   inventory is a regression tripwire, not a one-time review: a new constructor
   must fail until classified. Its static matcher must cover direct-name and
   attribute calls, imported aliases, package re-exports, and direct simple
   assignment aliases. It is not a
   whole-program proof over dynamic dispatch (`getattr`, registry lookup, or
   factory indirection) or subclasses/wrappers that override a launch entry;
   those are explicit blind spots that require focused review and production
   path E2E, not an inference from a green inventory. The Core `TypeError` is
   merely a structural backstop and must not be the only rejection signal. The
   derived-launch inventory uses `(file, enclosing qualified function,
   constructor)` keys, so unrelated line movement does not turn that specific
   tripwire into mechanical maintenance.
   This adapter integration is not delivered by this Core type boundary alone.
   The following are **constrained host-adapter requirements**, not current
   Core-seam delivery claims:

   1. `provision_ref` is a Driver-registry opaque lookup handle bound to the
      requested `launch_id` and `parent_launch_id`; it is non-transferable and
      is never an artifact bearer. Only the Driver resolves it. A child,
      parent, manifest, capsule, or other artifact cannot provide or override
      identity, workdir, or agent-dir.
   2. The Driver holds and actually uses the workdir and agent-dir directory
      handles for execution (`fd` inheritance, `SCM_RIGHTS`, or a platform
      equivalent). A `dev+ino` value is only an additional check on that
      handle, never permission to resolve a path and reopen it later. A backend
      that cannot establish this non-replaceable binding is unsupported and
      fails closed.
   3. On a same-OS-user host without sandbox isolation, this mechanism protects
      against accidental confusion: wrong ref, ref reuse, cross-child use, and
      lineage mismatch. It does not claim to resist a malicious child that can
      directly modify the Driver registry.
   4. `admit_provider_call` accepts only `launch_id`, a single-use `call_id`,
      and an exact capability (`provider` plus `daemon` or `avatar`). The
      Driver derives principal, root-session, depth, binding revision, and
      lineage from current registry state keyed by `launch_id`. A compatibility
      interface that carries any of those derived facts MUST compare each one
      against that state and deny a mismatch with a distinct attack-signal
      reason code.
   5. The Driver-side CAS request carries the provider and exact capability
      that transport is about to execute. In the same atomic operation, the
      Driver compares both values with the grant issued for `admission_id` and
      performs `unused -> consumed_before_io`; a mismatch fails as the distinct
      attack-signal denial from requirement 4. A local transport mark is not
      consumption; an unsuccessful CAS prevents provider I/O.
   6. A timeout or crash after that CAS and before a known provider result is
      `consumed_outcome_unknown`: the grant remains consumed and is never
      restored or reused, and this outcome is recorded in the auditable
      admission record. A retry uses a new `call_id` and obtains a new grant.
   7. Production E2E must assert the authorization seam's exact `reason_code`
      and an audit record linkable by `audit_id` or `admission_id`; merely
      observing a rejection is insufficient evidence that the seam ran.
   8. In one run, one recording transport must observe a non-empty provider
      call list for a legal root-to-one-hop path and `provider_calls == []` for
      each refused nested daemon/avatar path. This proves the recorder is live
      and rules out an empty assertion caused by a disconnected recorder.
   9. The typed decision `source` is the exclusive indicator of where a
      non-grant originated. Consumers MUST NOT infer origin from `reason_code`:
      a policy label may legitimately be emitted by both Driver and local
      policy. In particular, `nested_derived_launch_denied` is valid with
      `DRIVER` and with `LOCAL_POLICY`; code that distinguishes those cases
      MUST branch on `source`.

   Dynamic revoke freshness and propagation remain explicitly undelivered;
   these requirements reserve no claim that they are already implemented. A host that
   binds a derived parent before its transport is connected receives explicit
   `derived_admission_port_unconnected` indeterminacy and rejects before
   provider I/O. Historical daemon/avatar routes do not yet bind that parent,
   so they remain outside this gate until the separate driver-mediated adapter
   is wired. The raw `LLMService` direct-construction inventory (including
   imported aliases and attribute calls) is pinned by
   `tests/test_provider_admission.py`; adding a constructor requires an
   explicit classification rather than silently creating another route. The
   future host Port MUST decide against authority current at that
   call, not a turn-start snapshot or an implicit cache. This rule prevents
   accidental or structurally separate non-admitted paths. It does not claim
   that a full-tool Agent sharing the same OS trust domain as Core is sandboxed
   from its own host process.
   The current dispatch-propagation inventory is closed as follows. The main
   `SessionManager.send()` path calls `send_with_timeout()` and the streaming
   path calls `send_with_timeout_stream()`; both submit the already-wrapped
   session into the session timeout worker under a copy of the submitting
   context. Main-turn retries, recovery, tool-result continuation, and stream
   continuation all return through one of those two `SessionManager` paths.
   Soul consultation/inquiry creates an independently timed daemon thread and
   likewise copies the submitting context before its wrapped `session.send()`.
   If provider RPM gating is configured, `APICallGate` runs *after* the outer
   admitted-session proxy has made its Port decision; it never performs an
   additional admission lookup. `ProviderAdmittedLLMService.generate()` makes
   its Port decision synchronously before delegating to the adapter; no Core
   root-turn production caller currently invokes that one-shot API. Historical
   daemon/avatar services remain outside this inventory until their separate
   driver-mediated adapter is wired. Adding another dispatch mechanism between
   an admitted parent and a wrapped provider call requires a propagation test
   at that concrete boundary; no inferred coverage is sufficient. The source
   creation-point inventory in `tests/test_provider_admission.py` independently
   enumerates direct `Thread`, executor, `to_thread`, and `run_in_executor`
   calls under `src/lingtai/**`. It classifies the session timeout pool and
   Soul worker as context-propagation boundaries, `APICallGate` as
   post-admission dispatch, and every other current point as outside root
   provider dispatch. A new direct creation point therefore fails until it is
   classified; this structural tripwire is not a whole-program proof over
   dynamic concurrency factories. It presently inventories creation points,
   not dispatches to an already-created pool (`submit`, `map`, or
   `apply_async`); a future hardening must inventory those dispatch operations
   as a separate axis. Its `(file, line, constructor)` keys are likewise an
   intentionally narrow implementation: a later revision should use the
   enclosing qualified function instead, so unrelated line movement cannot
   turn the tripwire into mechanical maintenance. Neither limitation reduces
   the concrete propagation tests required above.
   A constrained composition MUST carry its origin policy at startup; a missing
   required policy rejects rather than falling back to the generic default. Its
   closed inbox surface is `MESSAGE_TYPES`: every canonical message other than
   the private correlated-turn envelope is rejected before downstream request,
   continuation, state, or notice handlers execute. The envelope still needs
   its independent typed-origin check at the final inbox-to-provider boundary.

## Contract tests

Composed behavior is pinned by the linked capability suites plus:
`tests/test_agent.py` (construction/start/stop),
`tests/test_lifecycle_daemon_shutdown.py` (typed timeout retains services/
heartbeat/lease until run-loop/provider quiescence, then pins post-proof teardown
order and issue-661 release), `tests/test_lingtai_facade.py` (public typed stop
exports and resolvable annotations), `tests/test_workdir_lease.py` (lease composition
at the CLI roots), `tests/test_perform_refresh_handshake.py` +
`tests/test_refresh_watcher_windows.py` (refresh on both platforms),
`tests/test_process_match.py` (canonical run/ACP matcher matrices incl. Windows-shaped
command lines and doctor parity), `tests/test_process_scan.py` (scan Port,
platform selector, CLI stop signals, CPR spawn kwargs, and the Windows
scan→guard wiring), `tests/test_windows_import_graph.py` (the boot-path import
graph survives missing POSIX mechanisms — the construction-gate proof), and
`tests/test_cli.py` (duplicate-guard policy), and
`tests/test_aed_recovery.py` (partial-output/exhausted provider-recovery
terminal guards plus fresh-dequeue latch reset and merge-race survival),
`tests/test_tool_result_restore_after_continuation_failure.py` (preset-latch
non-dispatch/no-continuation with latch retention),
`tests/test_notification_sync.py` (ASLEEP notification wake retention),
`tests/test_silence_kill.py` (idempotent private producer),
`tests/test_system.py` + `tests/test_system_declared_plugin.py` (direct and
official self-sleep ordering), and `tests/test_karma.py` +
`tests/test_perform_refresh_handshake.py` (signal-file and watcher-spawn
producer ordering). The
Windows release CI lane (`.github/workflows/kernel-windows-pr.yml`) executes the
platform-marked tiers natively on `release.published`; routine pull requests do
not spend a Windows runner. The capability matrix cites which rows carry native
receipts.

## Maintenance

Follow the canonical maintenance block in frontmatter. This contract composes
other contracts: when a linked capability contract changes its own promises,
re-read the composing clause here and update only the composition statement —
never fork or duplicate the capability's normative text into this file.
Cross-repository acceptance state changes (proposed → accepted) require both
repositories' revisions to reference each other and explicit maintainer
authorization.
