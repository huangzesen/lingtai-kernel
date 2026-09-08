---
name: base-agent-behavior-tests
behavior_version: 1
labt_version: 2
contract: CONTRACT.md
anatomy: ANATOMY.md
related_files:
  - src/lingtai/kernel/base_agent/CONTRACT.md
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/kernel/base_agent/lifecycle.py
  - src/lingtai/kernel/base_agent/turn.py
  - src/lingtai/kernel/base_agent/worker_recovery.py
  - src/lingtai/kernel/turns.py
  - src/lingtai/kernel/execution_workspace.py
  - src/lingtai/kernel/turn_events.py
  - src/lingtai/kernel/turn_permissions.py
  - src/lingtai/kernel/provider_admission.py
  - docs/references/provider-admission.md
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/tools/system/karma.py
  - tests/test_aed_recovery.py
  - tests/test_notification_sync.py
  - tests/test_cli_worker_poison_recovery.py
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
  - src/lingtai/adapters/acp/BEHAVIORS.md
maintenance: |
  Created during the every-contract-needs-behaviors sweep. Keep this file
  reciprocal with CONTRACT.md and ANATOMY.md (tridirectional loop): when a
  composed-lifecycle behavior clause of the agent-runtime contract changes,
  update the guarding LABT here in the same change.
---
# Agent Runtime Behavior Tests

Self-contained agent behavior tasks guarding the observable behavior clauses of
`src/lingtai/kernel/base_agent/CONTRACT.md` (the composed lifecycle promise:
construction, liveness, ordered stop, refresh handshake). Pinned pytest commands
must run from the repo root with the project's Python.

## Behavior BA001 — stop retains ownership until execution quiescence, then orders manifest-persist → heartbeat-withdraw → lease-release

- **id**: BA001
- **title**: stop retains ownership until execution quiescence, then orders manifest-persist → heartbeat-withdraw → lease-release
- **guards**: `agent-runtime` § Behavior
- **runner**: any LingTai agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>`; no other agent process sharing the scratch working directory
- **estimate**: ≈ 20 minutes

### Steps
1. From `<repo>`, run `python -m pytest tests/test_lifecycle_daemon_shutdown.py -q` and capture the outcome.
2. Run `python -m pytest tests/test_agent.py -q` and capture the outcome.
3. Inspect `src/lingtai/kernel/base_agent/lifecycle.py`: one bounded deadline covers run loop plus retained poisoned-provider Future; `TIMED_OUT` returns before every service/liveness/lease teardown; only the quiescent branch enters the issue-661 release `finally`.
4. Inspect public imports/type hints in `tests/test_lingtai_facade.py` and confirm callers can consume `StopResult`/`StopStatus` without importing an implementation-private module.

### Expected evidence
- [ ] Step 1: the lifecycle shutdown suite passes, pinning typed timeout, provider-write-before-service-teardown, heartbeat-before-release ordering, and release on post-proof cleanup failure.
- [ ] Step 2: the agent construction/start/stop suite passes, pinning construction, liveness, and stop behavior.
- [ ] Step 3: timed stop retains Task Card/services/heartbeat/lease; the successful retry orders provider quiescence → services → manifest → heartbeat → lease.
- [ ] Step 4: `lingtai`, `lingtai.kernel`, and `lingtai.kernel.base_agent` expose one identical typed stop result/status and stop annotations resolve to it.

### Pass / Fail
Pass when both suites pass, non-quiescent stop releases nothing, the typed public proof is usable, and post-proof teardown matches the contract. Fail if settled callers are mistaken for quiescence, any live provider can write after ownership release, heartbeat/lease is withdrawn on `TIMED_OUT`, or ordered release regresses; record the evidence trail in the task report.

## Behavior BA002 — a refresh that fails before the watcher handoff can be retried in the same process, and only a completed handoff is terminal

- **id**: BA002
- **title**: a refresh that fails before the watcher handoff can be retried in the same process, and only a completed handoff is terminal
- **guards**: `agent-runtime` § Contract rules, rule 5 (`agent-runtime.refresh.v1`) — see [CONTRACT.md](CONTRACT.md#contract-rules)
- **supersedes**: `tests/test_perform_refresh_handshake.py::test_launch_cmd_exception_releases_single_flight_slot_for_retry`, `tests/test_perform_refresh_handshake.py::test_raising_spawn_releases_slot_without_shutdown_and_retry_is_not_coalesced`, `tests/test_perform_refresh_handshake.py::test_successful_handoff_keeps_slot_claimed_even_if_post_handoff_logging_raises` (kept as bottom asserts)
- **runner**: any LingTai agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>`; a scratch agent working directory `<scratch>` containing an empty `logs/` subdirectory; no other agent process sharing `<scratch>`
- **estimate**: ≈ 20 minutes
- **motivation**: production defect 2026-08-24 — `_perform_refresh` claimed the process-lifetime single-flight slot before calling the bound `_build_launch_cmd()`; when the wrapper's configured-`venv_path` precheck raised, the slot stayed claimed and every corrected later refresh was silently skipped with `refresh_skipped(refresh_already_in_progress)` until the process was restarted by hand.

### Steps
1. From `<repo>`, run `python -m pytest tests/test_perform_refresh_handshake.py -q` and capture the outcome.
2. In `<scratch>`, construct a bare `BaseAgent` with an injected fake `RefreshWatcherPort` (record every `spawn_detached` call) and bind `agent._build_launch_cmd` to a callable that raises `RuntimeError` on its first call and returns `["python", "-c", "pass"]` afterwards. Call `agent._perform_refresh()` once and let the `RuntimeError` propagate.
3. Record, immediately after step 2: the number of `spawn_detached` calls; whether `<scratch>/.refresh` or `<scratch>/.refresh.taken` exists; `agent._shutdown.is_set()`; `agent._cancel_event.is_set()`; and `agent._refresh_started`.
4. Call `agent._perform_refresh()` a second time with the same agent and record the same observations plus every `refresh_skipped` / `refresh_deferred_relaunch` event the agent logged.
5. With a fresh agent in a fresh `<scratch>`, inject a fake `RefreshWatcherPort` whose `spawn_detached` records the call and then raises `OSError` on its first call only. Call `agent._perform_refresh()` once (let the `OSError` propagate) and record: `spawn_detached` call count, `agent._refresh_started`, `_shutdown.is_set()`, `_cancel_event.is_set()`, and whether `<scratch>/.refresh.taken` exists. Call `agent._perform_refresh()` a second time and record the call count, the `handshake` field of the `refresh_deferred_relaunch` event, and any `refresh_skipped` event.
6. With a fresh agent in a fresh `<scratch>`, bind `agent._log` so it raises `OSError` exactly when the event name is `refresh_deferred_relaunch`, call `agent._perform_refresh()` once (let the `OSError` propagate), then call it a second time; record `spawn_detached` call counts after each call and the second call's logged events.

### Expected evidence
- [ ] Step 1: the refresh handshake suite passes, including the single-flight coalescing and concurrent exact-one-watcher tests.
- [ ] Step 3 (failure before handshake normalization): zero `spawn_detached` calls; neither `.refresh` nor `.refresh.taken` exists; `_shutdown` and `_cancel_event` are both clear; `agent._refresh_started` is `False`.
- [ ] Step 4: the second call is not skipped — exactly one `spawn_detached` call in total, `.refresh.taken` exists, `_shutdown` and `_cancel_event` are set, one `refresh_deferred_relaunch` event, and no `refresh_skipped` event with reason `refresh_already_in_progress`.
- [ ] Step 5 (exception after ACK establishment): after the first call exactly one recorded `spawn_detached` call (the one that raised), `agent._refresh_started` is `False`, `_shutdown` and `_cancel_event` are both clear, and `.refresh.taken` is present (the established ACK is not rolled back — record its presence, do not require its absence). After the second call: two recorded calls, `refresh_deferred_relaunch` with `handshake=preexisting_taken`, and no `refresh_skipped` event with reason `refresh_already_in_progress`.
- [ ] Step 6: exactly one `spawn_detached` call after the first call and still exactly one after the second; the second call logs `refresh_skipped` with reason `refresh_already_in_progress`.

### Pass / Fail
Pass when every failure before `spawn_detached` returns leaves the slot released with no cancel/shutdown signal and the corrected retry completes exactly one handoff; a failure before handshake normalization additionally leaves no watcher call and no `.refresh`/`.refresh.taken` mutation, while an exception after the ACK was established may leave `.refresh.taken` in place; and a completed handoff (the Port's normal return) keeps the slot claimed. Fail if a retry after a raising launch-command build or a raising spawn is skipped as already-in-progress, if a pre-handshake failure spawns a watcher or mutates `.refresh`/`.refresh.taken`, if any pre-handoff failure sets `_shutdown`/`_cancel_event`, or if a post-handoff logging failure lets a later request spawn a second watcher; record the evidence trail in the task report.

## Behavior BA003 — one cooperative turn-cancel latch survives ACTIVE work and is consumed only by a fresh dequeue

- **id**: BA003
- **title**: one cooperative turn-cancel latch survives ACTIVE work and is consumed only by a fresh dequeue
- **guards**: `agent-runtime.turn-cancel-latch.v1`
- **runner**: any LingTai agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>`; no live agent process sharing any pytest scratch working directory
- **estimate**: ≈ 10 minutes

### Steps
1. From `<repo>`, run `python -m pytest -q -x tests/test_tool_result_restore_after_continuation_failure.py tests/test_aed_recovery.py tests/test_notification_sync.py tests/test_silence_kill.py`.
2. Run `python -m pytest -q -x tests/test_system.py tests/test_system_declared_plugin.py tests/test_karma.py tests/test_perform_refresh_handshake.py`.
3. Inspect `BaseAgent._request_turn_cancel`, both `_run_loop` dequeue branches, `_sync_notifications`, and `_process_response`; confirm producer writes route through the helper, only the two post-shutdown fresh-dequeue sites clear, the awake clear precedes concatenation, and inner consumers never clear.

### Expected evidence
- [ ] Step 1: a normal preset `threading.Event` prevents tool dispatch and continuation while remaining set; awake and ASLEEP fresh dequeues clear stale state; an event-barrier cancellation during concatenation survives; an ASLEEP notification wake preserves the latch; repeated helper calls are harmless.
- [ ] Step 2: official and direct System self-sleep publish ASLEEP state/event before latching; heartbeat interrupt/sleep consume their signal file before latching; successful refresh spawns its watcher before latching and sets shutdown afterward; failed setup remains unsignaled.
- [ ] Step 3: source inspection finds one direct `.set()` inside the helper and exactly two `.clear()` calls in fresh-dequeue ownership. No provider abort, running-tool preemption, request identity, stop-drain guarantee, or terminal cancellation result has been introduced.

### Pass / Fail
Pass when both focused groups pass and source ownership matches the contract. Fail if an async notification wake or inner response consumer clears cancellation, if merge-time cancellation is lost, if a producer bypasses the helper, or if the cooperative latch is represented as hard/per-request cancellation; record the evidence trail in the task report.

## Behavior BA004 — correlated inbound turns settle exactly once and pending cancellation cannot affect the turn ahead

- **id**: BA004
- **title**: correlated inbound turns settle exactly once and pending cancellation cannot affect the turn ahead
- **guards**: `agent-runtime` § Contract rules, rule 12 (`agent-runtime.correlated-turn.v1`) — see [CONTRACT.md](CONTRACT.md#contract-rules)
- **supersedes**: `tests/test_correlated_turns.py` (retained as bottom asserts)
- **runner**: any LingTai coding agent with shell access to this repository
- **prerequisites**: a clean checkout of `<repo>` and a project Python with pytest; no live agent sharing pytest scratch state
- **estimate**: ≈ 3 minutes

### Steps
1. From `<repo>`, run `python -m pytest -q -x tests/test_turn_events.py tests/test_turn_permissions.py tests/test_correlated_turns.py tests/test_execution_workspace.py tests/test_tool_executor.py` with the project Python.
2. Inspect the normal test and confirm the submitted correlation id returns one `normal` result carrying the complete collected text.
3. Inspect the active-cancel and two-queued-turn tests: cancel the matching active handle before releasing its fake provider, then cancel the pending second handle while the first is blocked.
4. Inspect failure and shutdown tests and confirm AED terminal failure becomes `failed` while run-loop shutdown settles an unprocessed queued handle `cancelled`; a control already claimed during the dequeue/bind race makes its private envelope skip provider dispatch.
5. Inspect the two unexpected-run-loop tests: a failure after dequeue but before `begin_turn` cancels the still-registered handle, while a failure after provider completion but before normal settlement fails the current handle with bounded detail; both exceptions remain visible to supervision rather than being swallowed.
6. Inspect the cancel-vs-settle race and worker-hang context tests: exactly one settlement wins, a later cancel is false, and correlated request text uses the existing bounded/redacted request artifact shape.
7. Inspect execution-workspace and turn-observer tests: task-local scope roots
   execution paths, lifecycle observers reach serial/parallel tool workers, a
   consecutive unobserved turn sees no stale observer, and observer failure does
   not alter tool execution or settlement.
8. Inspect permission tests: absent brokerage passes through; a bound broker sees
   only safe identity; exceptions/invalid decisions deny; consecutive turns reset scope.

### Expected evidence
- [ ] All focused tests pass without a provider or network call.
- [ ] Active cancellation wins before settlement, emits no late text, and a later cancel returns false.
- [ ] Pending cancellation leaves the process-global latch clear while the first turn is current; the first settles normal and only the second settles cancelled without provider dispatch.
- [ ] Failure and shutdown each settle rather than leaving a waiter blocked; a terminal stale envelope never reaches provider work.
- [ ] Pre-bind and post-provider unexpected exceptions both re-raise, leave no live control, and settle the affected waiter cancelled/failed respectively without requiring `Agent.stop()`.
- [ ] Cancel/settle races leave exactly one terminal result, and WorkerStillRunning attribution records the correlated turn as a bounded/redacted request.
- [ ] Tool lifecycle observation is turn-scoped, ordered per tool-call id, inherited by parallel workers, reset before later turns, and unable to fail Core execution.

### Pass / Fail
Pass when every handle settles exactly once with the expected correlation and the
pending-cancel isolation assertion proves the turn ahead was untouched. Fail on
a hanging/duplicate result, merged correlation, cancellation leaking to a later
or earlier turn, failure represented as normal, or any hard provider-abort claim;
record the evidence trail in the task report.

## Behavior BA005 — every provider request is freshly admitted and a derived child cannot mint another child

- **id**: BA005
- **title**: every provider request is freshly admitted and a derived child cannot mint another child
- **guards**: `agent-runtime.provider-admission.v1` in [CONTRACT.md](CONTRACT.md#contract-rules)
- **supersedes**: `tests/test_provider_admission.py` (kept as bottom asserts)
- **runner**: any LingTai coding agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>` and the project Python with pytest; no live provider credentials are required
- **estimate**: ≈ 5 minutes

### Steps

1. From `<repo>`, run `python -m pytest -q tests/test_provider_admission.py`.
2. Inspect the derived-admission tests: a `RootProviderAdmission` may create one typed daemon or avatar parent; attempting to create a child from that parent raises before provider I/O.
3. Inspect the denied and indeterminate provider-call tests: they assert the recording provider has no calls; invoke two admitted calls and confirm the recording Port receives a fresh decision for each.
4. Inspect the constructor-inventory sensitivity test. Confirm direct names, import aliases, package attributes, and module/function assignment aliases are recognized; review every inventory addition rather than treating the scan as a whole-program proof.

### Expected evidence

- [ ] The focused suite passes without a network provider call.
- [ ] A nested daemon/avatar request cannot mint authority and no denied/indeterminate call reaches the recording provider.
- [ ] Each actual provider request crosses the Port separately; a root grant is never reused as a derived-call grant.
- [ ] The static inventory sees the documented direct forms, while dynamic factories, registry dispatch, and subclass/wrapper overrides remain explicit review blind spots.

### Pass / Fail

Pass when the focused suite proves fresh fail-closed admission and the one-hop limit, and the inventory sensitivity cases match the Contract. Fail if a child mints another child, an unavailable/denied call reaches provider I/O, a second call reuses an earlier decision, or an advertised static constructor form is invisible; record the evidence trail in the task report.

## Behavior BA006 — a poison-recovery relaunch stays ASLEEP until a genuinely later wake

- **id**: BA006
- **title**: a poison-recovery relaunch stays ASLEEP until a genuinely later wake
- **guards**: `agent-runtime` § Contract rules, rule 5 (`agent-runtime.refresh.v1`), poison-recovery paragraph — see [CONTRACT.md](CONTRACT.md#contract-rules)
- **pinned by**: `tests/test_cli_worker_poison_recovery.py::test_refresh_boot_with_pending_worker_recovery_stays_asleep_without_kickstart`, `tests/test_notification_sync.py::test_start_with_pending_worker_recovery_does_not_self_wake_on_first_sync`, `tests/test_notification_sync.py::test_baseline_refuses_when_external_notification_already_pending` (bottom asserts added with this behavior; no legacy pytest was converted)
- **runner**: any LingTai coding agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>` and the project Python with pytest; no live agent sharing pytest scratch state
- **estimate**: ≈ 5 minutes
- **motivation**: production defect 2026-09-08 — after a 300 s provider timeout plus grace (`WorkerStillRunningError`) the agent went ACTIVE → STUCK → ASLEEP and was force-relaunched to discard the poisoned interface, but the fresh process treated the relaunch as a user refresh: the CLI sent `system.refresh_successful`, the agent woke (`woke from asleep: request`) and started a new LLM call 250 ms after boot with no external request; the rehydrated worker-hang notification would likewise have woken it on the first heartbeat because a fresh `_notification_fp` starts empty.

### Steps
1. From `<repo>`, run `python -m pytest -q -x tests/test_cli_worker_poison_recovery.py tests/test_notification_sync.py -k "refresh_boot or non_refresh_boot or pending_worker_recovery or baseline or foreign"` and capture the outcome.
2. Inspect `src/lingtai/cli.py::run`: on a `.refresh.taken` boot the refresh-success `send` is skipped exactly when `has_pending_worker_hang_recovery_prompt(agent)` is true, logging `refresh_kickstart_deferred`; every other refresh boot still sends it.
3. Inspect `src/lingtai/kernel/base_agent/lifecycle.py::_start`: `baseline_notifications_for_pending_worker_recovery` runs after `rehydrate_worker_hang_recovery` and before `_heartbeat_runtime_ready = True`; confirm it seeds only `_notification_fp`/`_notification_raw_fp`, only from a stable coherent read whose sole content is the worker-hang recovery event (only the `system` channel, every event a WorkerStillRunning ref, nothing beyond the virtual quiet daemon baseline), never arms the consumer-delay timer, and never dismisses, clears, or hides `.notification/` bytes or mutates the artifact.
4. Inspect the wake tests: after a seeded baseline, a later genuine channel publish moves the agent ASLEEP → IDLE with one `MSG_TC_WAKE` and delivers the full current payload including the worker-hang event; with an external `email`/`mcp.telegram` payload or a non-worker-hang system event already pending at boot, no baseline is seeded (`foreign_notifications_pending`) and the FIRST sync wakes and delivers every channel. The artifact remains open and un-prompted for `maybe_prepend_worker_hang_recovery_prompt` in every case.

### Expected evidence
- [ ] Step 1: the focused group passes without a provider or network call.
- [ ] Step 2: pending recovery ⇒ no `send`, state ASLEEP, `refresh_kickstart_deferred(reason=pending_worker_hang_recovery)`; ordinary refresh, already-prompted artifact, and resolved artifact ⇒ exactly one localized refresh-success request from `system`.
- [ ] Step 3: with only the worker-hang event present, the first sync after boot leaves a pending-recovery relaunch ASLEEP with an empty inbox; an unstable or failed read logs `worker_hang_notification_baseline_skipped` and seeds nothing.
- [ ] Step 4: a later change wakes normally; an already-pending external notification wakes on the first tick with all channels delivered; nothing was hidden or resolved.

### Pass / Fail
Pass when a pending poison recovery whose boot-time notification state is solely the recovery event yields no synthesized inference round in the relaunched process, while any already-pending external notification is delivered on the first tick, and the next genuine change still wakes with the full payload. Fail if the relaunch sends the refresh-success request, if the rehydrated notification alone wakes the agent, if a pre-existing external message is deferred past the first tick, if the baseline dismisses/hides any payload or touches the artifact, if a prompted/resolved artifact suppresses an ordinary refresh kick-start, or if an unstable read is treated as quiet; record the evidence trail in the task report.
