---
name: refresh-watcher-behavior-tests
behavior_version: 2
labt_version: 2
contract: CONTRACT.md
anatomy: ANATOMY.md
related_files:
  - src/lingtai/kernel/refresh_watcher/CONTRACT.md
  - src/lingtai/kernel/refresh_watcher/ANATOMY.md
  - src/lingtai/kernel/refresh_watcher/__init__.py
  - src/lingtai/kernel/refresh_watcher/watcher_program.py
  - tests/test_perform_refresh_handshake.py
  - tests/test_refresh_watcher_process.py
  - tests/test_refresh_watcher_lease_probe.py
maintenance: |
  Created during the every-contract-needs-behaviors sweep. Keep this file
  reciprocal with CONTRACT.md and ANATOMY.md (tridirectional loop): when a
  refresh-watcher behavior clause changes, update the guarding LABT here in the
  same change.
---
# Refresh Watcher Behavior Tests

Self-contained agent behavior tasks guarding the observable behavior clauses of
`src/lingtai/kernel/refresh_watcher/CONTRACT.md` (one detached watcher spawn per
refresh, frozen request, handshake normalization, env-overwrite policy, and the
bounded relaunch health-check/duplicate-exit waits). Pinned pytest commands must
run from the repo root with the project's Python.

## Behavior RW001 — a successful refresh spawns the detached watcher exactly once, and failed ACK setup does not spawn

- **id**: RW001
- **title**: a successful refresh spawns the detached watcher exactly once, and failed ACK setup does not spawn
- **guards**: `refresh-watcher` § Behavior
- **runner**: any LingTai agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>`; a scratch agent working directory `<scratch>`
- **estimate**: ≈ 20 minutes

### Steps
1. From `<repo>`, run `python -m pytest tests/test_perform_refresh_handshake.py -q` and capture the outcome.
2. Run `python -m pytest tests/test_refresh_watcher_process.py -q` and capture the outcome.
3. In `<scratch>`, drive one `_perform_refresh` and count detached watcher spawns (via the injected `RefreshWatcherPort` fake); verify the frozen request carries only handshake paths, working directory, tuple command, identity fields JSON, and the env-overwrite policy bit.

### Expected evidence
- [ ] Step 1: the refresh handshake suite passes (`.refresh` → `.refresh.taken`, exactly one spawn, cancellation/shutdown path).
- [ ] Step 2: the watcher process suite passes (rendered policy, exact copied environment, detached stdio, platform detached handoff).
- [ ] Step 3: exactly one `spawn_detached` call per successful refresh; a failed ACK setup performs zero spawns; the request carries no generated source and no caller environment.

### Pass / Fail
Pass when the suites pass and the one-spawn/zero-spawn observations hold. Fail on a second spawn, on a spawn after failed ACK setup, or on a request that carries generated source or caller environment; record the evidence trail in the task report.

## Behavior RW002 — a slow-booting relaunch is not declared dead, and a terminated duplicate is gone before the next attempt

- **id**: RW002
- **title**: a slow-booting relaunch is not declared dead, and a terminated duplicate is gone before the next attempt
- **guards**: `refresh-watcher` § Contract rules, rule 2
- **runner**: any LingTai agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>`; a scratch agent working directory `<scratch>`
- **estimate**: ≈ 25 minutes
- **motivation**: production incident 2026-08-19 (`spiritual-bliss-attractor/.lingtai/codex`) exhausted all 12 relaunch attempts. The health check slept `HEALTH_CHECK_WAIT` once and sampled `.agent.heartbeat` a single time — too early for an agent still spawning MCP stdio servers — and the loop retried immediately after SIGKILLing a duplicate that had not yet released the working directory, so every attempt collided again with `another lingtai agent is already running`.

### Steps
1. From `<repo>`, run `python -m pytest tests/test_refresh_watcher_process.py tests/test_perform_refresh_handshake.py -q` and capture the outcome.
2. Read the rendered policy (`render_watcher_script`) and confirm the post-relaunch health check is a bounded poll (`_await_fresh_heartbeat`, `HEALTH_CHECK_BUDGET`, `WATCHER_POLL_INTERVAL`) rather than one `time.sleep(HEALTH_CHECK_WAIT)` followed by a single heartbeat read.
3. In `<scratch>`, run the rendered policy against a fake `PROCESS_MECHANISM` whose relaunch writes its first `.agent.heartbeat` later than `HEALTH_CHECK_WAIT` but inside `HEALTH_CHECK_BUDGET`; record the launch count and the `refresh_watcher_success` event.
4. In `<scratch>`, run the rendered policy against a fake whose duplicate only leaves the process table some time after `force_stop`; record the timestamp of each `start_agent` call and of the duplicate's disappearance.
5. In `<scratch>`, run the rendered policy against a fake whose duplicate never dies; record `logs/refresh_failed_permanent.json` and the emitted event types.

### Expected evidence
- [ ] Step 1: both suites pass.
- [ ] Step 2: the rendered policy contains `_await_fresh_heartbeat` bounded by `HEALTH_CHECK_BUDGET`, `_await_duplicate_exit` bounded by `DUPLICATE_EXIT_WAIT`, and no single-sample `time.time() - hb_ts < HEALTH_CHECK_WAIT + 10` health check.
- [ ] Step 3: exactly one `start_agent` call, exit code 0, and one `refresh_watcher_success` event whose `heartbeat_wait` exceeds `HEALTH_CHECK_WAIT`.
- [ ] Step 4: the second `start_agent` call happens no earlier than the duplicate's disappearance, and no `refresh_watcher_stale_duplicate_still_alive` event is emitted.
- [ ] Step 5: the loop terminates after `MAX_ATTEMPTS`; the artifact records `last_cleanup_action = 'terminate_stale_duplicate'` and `last_cleanup_result = 'still_alive'`; one `refresh_watcher_stale_duplicate_still_alive` event per attempt; the final event is `refresh_failed_permanent`.

### Pass / Fail
Pass when a heartbeat that arrives after `HEALTH_CHECK_WAIT` but inside `HEALTH_CHECK_BUDGET` is accepted on the first attempt, the retry waits for a terminated duplicate, and an undying duplicate is reported honestly instead of hanging. Fail on a slow boot costing a second attempt, on a retry that starts while the duplicate still matches the same-agent guard, on an unbounded wait, or on a terminal artifact that hides the `still_alive` outcome; record the evidence trail in the task report.

## Behavior RW003 — a dead owner's lock pathname or young heartbeat never strands the relaunch, and `.refresh.taken` is settled with a truthful exit at every terminal outcome

- **id**: RW003
- **title**: a dead owner's lock pathname or young heartbeat never strands the relaunch, and `.refresh.taken` is settled with a truthful exit at every terminal outcome
- **guards**: `refresh-watcher` § Purpose (lease/heartbeat paragraph), § Adapters and composition (entrypoint fail-safe), § Contract rules, rule 12
- **pinned by**: `tests/test_refresh_watcher_lease_probe.py` (all tests); `tests/test_perform_refresh_handshake.py::test_refresh_watcher_entrypoint_invoked_via_dash_m_runs_watcher_program` (already-alive requires an advancing heartbeat) and `::test_refresh_watcher_permanent_failure_writes_operator_alert` (exit 1)
- **runner**: any LingTai coding agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>` and the project Python with pytest
- **estimate**: ≈ 10 minutes
- **motivation**: production incident 2026-09-08 (Runyuan). A `WorkerStillRunningError` poisoned the interface, the CLI hard-exited via `os._exit` seconds after its last heartbeat and before `WorkdirLeasePort.release()`; the OS lease was gone but the `.agent.lock` pathname and a young heartbeat remained. The watcher trusted the pathname, logged `refresh_watcher_timeout phase=lock`, and left `.refresh.taken`, so the TUI showed `Refreshing` for hours; trusting the young heartbeat instead would have exited "already alive" without launching a child.

### Steps
1. From `<repo>`, run `python -m pytest tests/test_refresh_watcher_lease_probe.py tests/test_refresh_watcher_process.py tests/test_perform_refresh_handshake.py -q`.
2. Read `render_watcher_script` and confirm: the lock phase ends on a cleared pathname or a successful `WORKDIR_LEASE.acquire(0)`/`release` probe; `hb_baseline` is captured at lock release and `advanced_heartbeat_age` gates already-alive, success, and duplicate protection; `_settle_taken` runs on ACK/lock timeout, already-alive, success, permanent failure, and in the `try` handlers; `sys.exit` appears only inside `_exit`.

### Expected evidence
- [ ] Step 1: all three suites pass; the suite shows: stale pathname + free lease relaunches (fake and real POSIX adapter); a 5 s-old dead heartbeat does not suppress the relaunch and is never accepted as the child's success; an advancing heartbeat is already-alive; a young dead heartbeat does not starve duplicate cleanup; lock/ack timeouts and permanent failure exit 1 with the marker cleared (permanent: alert → marker → terminal event order); an unexpected exception or `SystemExit(17|0|None)` from a mechanism yields one `refresh_watcher_exception`, a cleared marker, and exit 17/1/1; both entrypoints settle only untagged failures once, preserve 17 and turn 0/None into 1; a decode failure touches nothing.
- [ ] Step 2: the named helpers exist and the policy names no adapter or platform lock vocabulary.

### Pass / Fail
Pass when the suite is green and the inspection holds. Fail on a `phase=lock` timeout caused solely by a pathname, on already-alive without an observed advance, on any terminal outcome that leaves the marker, on a zero exit for a failure, or on a failure reported twice; record the evidence trail in the task report.
