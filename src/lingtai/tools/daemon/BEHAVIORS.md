---
name: daemon-behavior-tests
behavior_version: 1
labt_version: 2
contract: CONTRACT.md
anatomy: ANATOMY.md
related_files:
  - src/lingtai/tools/daemon/__init__.py
  - src/lingtai/tools/daemon/run_dir.py
  - src/lingtai/tools/daemon/_tool_family.py
  - src/lingtai/tools/daemon/settings.py
  - src/lingtai/tools/daemon/manual/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/claude-p/SKILL.md
  - src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/qwen-code/SKILL.md
  - tests/test_daemon_check.py
  - tests/test_daemon_check_historical.py
  - tests/test_daemon_claude_p_submanual.py
  - tests/test_daemon_qwen_code_submanual.py
  - tests/test_daemon_per_batch_limits.py
  - tests/test_daemon_attention_delay.py
  - tests/test_daemon_settings.py
  - src/lingtai/kernel/base_agent/__init__.py
  - src/lingtai/tools/daemon/supervisor_runtime.py
maintenance: |
  Written by the daemon CONVERT_BEHAVIOR migration (2026-08). Keep in sync
  with CONTRACT.md clauses this file guards and ANATOMY.md entries for the
  daemon tool family; when CONTRACT.md or the daemon manual changes in a way
  that affects agent-observable behavior (check JSON contract, per-batch
  emanate limits, CLI-backend submanual routing), update the matching LABT
  here in the same change. D009 guards the typed terminal/follow-up event
  contract and the non-negative daemon wake deltas in CONTRACT.md § 6; update it
  whenever event typing, terminal classification, or the bounded
  `agent_state.daemon` projection changes.
  D010 guards the daemon owner's exact read-only five-field settings inventory;
  update it whenever row ownership, defaults, configurability, or manual
  section pointers change.
---
# Daemon Behavior Tests

LABT v1. These are self-contained agent-executable behavioral tests for the
`daemon` tool family. They prove the *observable* promises of
`src/lingtai/tools/daemon/CONTRACT.md`: the `check` JSON contract, historical
run-dir fallback, per-batch `emanate` limits, and the CLI-backend submanual
routing contracts. Low-level mechanics stay in pytest; each LABT below is
self-contained and executable verbatim by an agent with the `daemon` and
`file` tools.

## Behavior D001 — daemon.check returns the canonical JSON contract

- **id**: D001
- **title**: `check` returns state, events, result-file fields, and honors
  `last` / `truncate`
- **guards**: `daemon-contract` § Tool Surface (check success output)
  ([CONTRACT.md](CONTRACT.md#tool-surface))
- **supersedes**: `tests/test_daemon_check.py` (JSON-shape and truncation tests)
- **runner**: any LingTai agent with the `daemon` and `file` tools
- **prerequisites**: a working dir; the ability to emanate one trivial task
  and wait for its `source="daemon"` terminal notification
- **estimate**: 3 min

### Steps
1. From your working dir, call
   `daemon(action="emanate", input={"tasks": [{"task": "Create the file
   scratch/check-evidence.txt containing the word evidence, then read it back
   with the file tool. Reply with exactly: DONE", "tools": ["file"]}]},
   reasoning="...")`. Record the returned `ids[0]` as `<id>`.
2. Wait for the terminal notification for `<id>` (status `done`). Then call
   `daemon(action="check", input={"id": "<id>"}, reasoning="...")` with no
   other fields.
3. Call `daemon(action="check", input={"id": "<id>", "last": 3,
   "truncate": 0}, reasoning="...")`.
4. Call `daemon(action="check", input={"id": "<id>", "truncate": 1},
   reasoning="...")`.
5. Read the file at the `result_path` returned in step 2 and compare it with
   the `result_preview` field.
6. Call `daemon(action="check", input={"id": "<id>"}, reasoning="...")` a
   second time and compare `events_total` with the first call.

### Expected evidence
- [ ] Step 2 returns a success object (no `status` key) containing `id`,
      `run_id`, `state: "done"`, `backend`, `path`, `turn`,
      `finished_at` (non-null), `result_preview` (non-empty), `result_path`
      (a real path ending in `result.txt`), `error: null`, `events` (a list),
      `events_returned`, and `events_total`.
- [ ] The `events` list contains a `daemon_done` event plus `tool_call` /
      `tool_result` events (from the file calls) whose `tool_call` entries
      carry an `args_preview` string.
- [ ] `events_returned == min(events_total, 20)` (default `last` is 20).
- [ ] Step 3: `events_returned == 3` and `events_total >= 3`.
- [ ] Step 3 (`truncate: 0`): no event string field carries the
      `[truncated]` marker.
- [ ] Step 4 (`truncate: 1`): the last `tool_call` event's `args_preview`
      contains `[truncated]` and its length is `<= 1 + len("…[truncated]")`.
- [ ] Step 5: the file at `result_path` contains the same text as
      `result_preview`.
- [ ] Step 6: `events_total` did not grow (checking is read-only for a
      finished run).

### Pass / Fail
Pass when every evidence item holds. Fail on any missing field, a wrong
`state`, a `{status: "error"}` response for a known id, a non-null `error` on
a `done` run, or evidence that a check call mutated the run dir.

## Behavior D002 — daemon.check validates inputs and bounds last

- **id**: D002
- **title**: `check` refuses unknown ids and invalid `last` / `truncate`, and
  bounds `last` at the engine maximum
- **guards**: `daemon-contract` § Tool Surface (check error shape)
  ([CONTRACT.md](CONTRACT.md#tool-surface))
- **supersedes**: `tests/test_daemon_check.py` (input-validation tests)
- **runner**: any LingTai agent with the `daemon` tool
- **prerequisites**: none beyond your working dir
- **estimate**: 2 min

### Steps
1. Call `daemon(action="check", input={"id": "em-999"}, reasoning="...")`
   with an id that has never existed in your session.
2. Call `daemon(action="check", input={"id": "em-x", "last": "twenty"},
   reasoning="...")`.
3. Call `daemon(action="check", input={"id": "em-x", "truncate": "tons"},
   reasoning="...")`.
4. Call `daemon(action="check", input={"id": "em-x", "last": 0},
   reasoning="...")`, then the same with `last: -1`.
5. Call `daemon(action="check", input={"id": "em-x", "truncate": -10},
   reasoning="...")`.
6. Call `daemon(action="check", input={"id": "em-x", "last": 1000000000},
   reasoning="...")`.

### Expected evidence
- [ ] Step 1: `{status: "error", message}` and the message contains the
      requested id `em-999`; no events blob is returned.
- [ ] Step 2: refused with an error that names `last`.
- [ ] Step 3: refused with an error that names `truncate`.
- [ ] Step 4: both calls refused with errors that name `last`.
- [ ] Step 5: refused with an error that names `truncate`.
- [ ] Step 6: the call is refused cleanly (the `check.last` schema bound is
      `maximum: 1000`) or, if your host permits it through, returns a success
      shape with `events_returned <= 1000` (the engine's `_CHECK_LAST_MAX`)
      — never an unbounded event list or an exception.

### Pass / Fail
Pass when every invalid input returns a clean error naming the offending
field and the oversized `last` is refused or capped without error. Fail if any
invalid call raises, succeeds, or returns an unbounded list.

## Behavior D003 — daemon.check falls back to historical run dirs

- **id**: D003
- **title**: `check` resolves completed run dirs on disk when the in-memory
  registry misses, and rejects ambiguous legacy handles
- **guards**: `daemon-contract` § Tool Surface (check accepts historical run
  ids) ([CONTRACT.md](CONTRACT.md#tool-surface)); see also `daemon-manual`
  "check still resolves a daemon after refresh/molt"
  ([manual/SKILL.md](manual/SKILL.md))
- **supersedes**: `tests/test_daemon_check_historical.py`
- **runner**: any LingTai agent with the `daemon` and `file` tools
- **prerequisites**: a `<parent>/daemons/` directory (created by any
  emanation); file write access to it; the synthetic dirs created in the
  steps are removed at the end
- **estimate**: 3 min

### Steps
1. If you have no completed run dir on disk, emanate a trivial task and wait
   for its `done` notification; record its exact `run_id` (from the
   notification or `daemon(action="list", input={})`).
2. Create two synthetic completed run dirs sharing the legacy short handle
   `em-7`, under `<parent>/daemons/`:
   - `<parent>/daemons/em-7-20260623-100000-aaaaaa/` with
     `daemon.json` = `{"handle": "em-7", "run_id": "em-7-20260623-100000-aaaaaa", "state": "done", "backend": "lingtai", "data_version": 1, "result_path": "<dir>/result.txt", "result_preview": "older run", "finished_at": "2026-06-23T00:00:00Z"}`, `result.txt` containing `older run`, and `logs/events.jsonl` containing one line `{"event": "daemon_done", "run_id": "em-7-20260623-100000-aaaaaa"}`.
   - `<parent>/daemons/em-7-20260623-110000-bbbbbb/` with the same shape,
     `run_id` / `result_preview` = `em-7-20260623-110000-bbbbbb` / `newer run`.
   (`data_version` is `DaemonRunDir.DATA_VERSION`, currently 1.)
3. Create one compact-id run dir `<parent>/daemons/em-abcd/` with
   `daemon.json` = `{"handle": "em-abcd", "run_id": "em-abcd", "state": "done", "backend": "lingtai", "data_version": 1, "result_path": "<dir>/result.txt", "result_preview": "compact daemon work", "finished_at": "2026-06-23T00:00:00Z"}`, `result.txt` containing `compact daemon work`, and `logs/events.jsonl` containing one line `{"event": "daemon_done", "run_id": "em-abcd"}`.
4. Call `daemon(action="check", input={"id": "em-7-20260623-110000-bbbbbb"},
   reasoning="...")`.
5. Call `daemon(action="check", input={"id": "em-7"}, reasoning="...")`.
6. Call `daemon(action="check", input={"id": "em-abcd"}, reasoning="...")`.
7. Call `daemon(action="check", input={"id": "em-999"}, reasoning="...")`.
8. Delete the three synthetic dirs you created in steps 2–3.

### Expected evidence
- [ ] Step 4: success (no `status: "error"`), `run_id == "em-7-20260623-110000-bbbbbb"`, `state == "done"`, `path` is that dir, `result_preview == "newer run"`, `source == "history"`, and `events` (tailed from the on-disk `logs/events.jsonl`) contains `daemon_done`.
- [ ] Step 5: `{status: "error", ambiguous: true, match_count: 2,
      latest_run_id: "em-7-20260623-110000-bbbbbb"}`; the message contains
      `use the exact run_id`; no `other_run_dirs` key is present.
- [ ] Step 6: success with `run_id == "em-abcd"` and `source == "history"`;
      no `other_run_dirs` key.
- [ ] Step 7: `{status: "error", message}` containing `em-999`.
- [ ] Step 8: the synthetic dirs are gone and no other files were touched.

### Pass / Fail
Pass when a registry-miss `check` resolves on-disk run dirs with
`source: "history"`, rejects an ambiguous legacy handle with
`match_count` / `latest_run_id` instead of an unbounded path list, and still
errors on a truly unknown id. Fail if a completed on-disk run is reported
unknown, an ambiguous handle returns a path list, or a synthetic dir is left
behind.

## Behavior D004 — emanate per-batch max_turns ceiling is 1000

- **id**: D004
- **title**: `emanate` defaults `max_turns` to the 1000 ceiling, honors a
  smaller per-batch value, caps larger values, and rejects non-positive ones
- **guards**: `daemon-contract` § Tool Surface (emanate row: optional
  `max_turns`) ([CONTRACT.md](CONTRACT.md#tool-surface))
- **supersedes**: `tests/test_daemon_per_batch_limits.py` (max_turns tests)
- **runner**: any LingTai agent with the `daemon` and `file` tools
- **prerequisites**: a working dir; each dispatched run is allowed to reach a
  terminal state (or is reclaimed at the end)
- **estimate**: 4 min

### Steps
1. Call `daemon(action="emanate", input={"tasks": [{"task": "Reply with
   exactly: done", "tools": []}]}, reasoning="...")` with no `max_turns`.
   Record `<id>`; immediately call
   `daemon(action="check", input={"id": "<id>"}, reasoning="...")` to get
   `<path>`, then read `<path>/daemon.json` and note its `max_turns` value.
2. Repeat step 1 with `"max_turns": 50` and note the recorded `max_turns`.
3. Repeat step 1 with `"max_turns": 9999` and note the recorded `max_turns`.
4. Repeat step 1 with `"max_turns": 1000` and note the recorded `max_turns`.
5. Call `daemon(action="emanate", input={"tasks": [{"task": "x",
   "tools": []}], "max_turns": 0}, reasoning="...")` and then the same with
   `"max_turns": -5`; read both results.
6. Wait for each run's terminal notification (or reclaim at the end) so no
   run is left running.

### Expected evidence
- [ ] Step 1: dispatched; `<path>/daemon.json` records `max_turns == 1000`
      (the default is the ceiling).
- [ ] Step 2: `max_turns == 50` (per-batch override respected).
- [ ] Step 3: `max_turns == 1000` (9999 capped at the ceiling).
- [ ] Step 4: `max_turns == 1000` (the ceiling itself is allowed).
- [ ] Step 5: both calls return `{status: "error", message}` and the message
      mentions `max_turns`.
- [ ] The emanate branch of the public `daemon` schema advertises `max_turns`
      with `minimum: 1`, `maximum: 1000`, and a description containing `1000`
      (pinned by the CONVERT_BEHAVIOR contract).
- [ ] No emanation is left running at the end of step 6.

### Pass / Fail
Pass when the recorded values match exactly and invalid values are refused
before dispatch. Fail if the default exceeds 1000, an override above 1000 is
recorded uncapped, zero/negative values are accepted, or a run is left
running.

## Behavior D005 — emanate per-batch timeout contract

- **id**: D005
- **title**: `emanate` honors a per-batch `timeout`, caps it at the manager
  ceiling, and rejects zero, negative, and sub-5s values
- **guards**: `daemon-contract` § Tool Surface (emanate row: optional
  `timeout`) ([CONTRACT.md](CONTRACT.md#tool-surface))
- **supersedes**: `tests/test_daemon_per_batch_limits.py` (timeout tests)
- **runner**: any LingTai agent with the `daemon` and `file` tools
- **prerequisites**: a working dir; each dispatched run is allowed to reach a
  terminal state (or is reclaimed at the end)
- **estimate**: 4 min

### Steps
1. Call `daemon(action="emanate", input={"tasks": [{"task": "Reply with
   exactly: done", "tools": []}], "timeout": 600}, reasoning="...")`.
   Record `<id>`; call
   `daemon(action="check", input={"id": "<id>"}, reasoning="...")` to get
   `<path>`, then read `<path>/daemon.json` and note its `timeout_s` value.
2. Repeat with `"timeout": 99999` and note the recorded `timeout_s`.
3. Call with `"timeout": 0`, then `"timeout": -1`, then `"timeout": 2`;
   read all three results.
4. Wait for each run's terminal notification (or reclaim at the end) so no
   run is left running.

### Expected evidence
- [ ] Step 1: dispatched; `<path>/daemon.json` records `timeout_s == 600.0`.
- [ ] Step 2: dispatched; `timeout_s` equals the manager's ceiling — `3600.0`
      seconds in default configuration (the same value the schema describes
      as "Default: parent max (3600s)").
- [ ] Step 3: `timeout: 0` and `timeout: -1` each return
      `{status: "error", message}` mentioning `timeout`; `timeout: 2` returns
      `{status: "error", message}` mentioning both `timeout` and `5` (sub-5s
      timeouts are refused because the watchdog could fire before the run
      starts).
- [ ] The emanate branch of the public `daemon` schema advertises `timeout`
      with `minimum: 5`.
- [ ] No emanation is left running at the end of step 4.

### Pass / Fail
Pass when the recorded values match exactly and invalid values are refused.
Fail if a per-batch timeout is not recorded, an oversized timeout is recorded
uncapped, or a zero/negative/sub-5s timeout is accepted.

## Behavior D006 — claude-p backend submanual routing contract

- **id**: D006
- **title**: the daemon CLI-backends router catalogs and routes to the
  claude-p submanual, which stays a tiny live-help entrypoint
- **guards**: `daemon-contract` § Backend Support Matrix (claude-p row)
  ([CONTRACT.md](CONTRACT.md#backend-support-matrix)); daemon-manual nested
  reference catalog ([manual/SKILL.md](manual/SKILL.md))
- **supersedes**: `tests/test_daemon_claude_p_submanual.py`
- **runner**: any LingTai agent with file read/grep access to the repo root
- **prerequisites**: the repo root; the three files listed below exist
- **estimate**: 2 min

### Steps
1. Read `src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md`.
   Locate the `## Nested reference catalog` section and its fenced YAML block;
   confirm exactly one entry has `location: reference/backends/claude-p/SKILL.md`,
   `name: daemon-backend-claude-p`, and a non-empty `description`.
2. In the same file, locate the `## Routing table` section; confirm at least
   one table row contains `reference/backends/claude-p/SKILL.md`.
3. Read `src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/claude-p/SKILL.md`.
   Check its YAML frontmatter: `name: daemon-backend-claude-p`, non-empty
   `description`, and a present `last_changed_at`.
4. Confirm the child body contains each exact string: `claude --version`,
   `claude --help`, `backend_options`, `"fallback_model"`, `--fallback-model`,
   `not validate, enumerate, or simulate`, `claude-code`, `compatibility alias`,
   `--settings`, `--print`, `--output-format`, `--mcp-config`,
   `--strict-mcp-config`, `claude_session_id`, `ANTHROPIC_API_KEY`,
   `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`.
5. Count the lines of the child file (shell `wc -l` or your file read tool's
   line count); record the number.

### Expected evidence
- [ ] Exactly one catalog entry resolves to the claude-p child, named
      `daemon-backend-claude-p`, with a non-empty description.
- [ ] The routing table contains a row pointing at
      `reference/backends/claude-p/SKILL.md`.
- [ ] Child frontmatter has the pinned `name`, non-empty `description`, and
      `last_changed_at`.
- [ ] Every phrase in step 4 is present in the child body.
- [ ] The child file has `<= 230` lines.

### Pass / Fail
Pass when all evidence holds. Fail on any missing phrase, a missing or
duplicate catalog entry, a missing routing-table row, wrong frontmatter, or a
child grown past 230 lines — the submanual must route agents to the installed
CLI's live help (`claude --version`, `claude --help`), never become a
maintained flag catalog.

## Behavior D007 — qwen-code backend submanual static-content contract

- **id**: D007
- **title**: the daemon CLI-backends router catalogs and routes to the
  qwen-code submanual, which stays a tiny live-help entrypoint
- **guards**: `daemon-contract` § Backend Support Matrix (qwen-code row)
  ([CONTRACT.md](CONTRACT.md#backend-support-matrix)); daemon-manual nested
  reference catalog ([manual/SKILL.md](manual/SKILL.md))
- **supersedes**: `tests/test_daemon_qwen_code_submanual.py`
- **runner**: any LingTai agent with file read/grep access to the repo root
- **prerequisites**: the repo root; the three files listed below exist
- **estimate**: 2 min

### Steps
1. Read `src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md`.
   Locate the `## Nested reference catalog` section and its fenced YAML block;
   confirm exactly one entry has `location: reference/backends/qwen-code/SKILL.md`,
   `name: daemon-backend-qwen-code`, and a non-empty `description`.
2. In the same file, locate the `## Routing table` section; confirm at least
   one table row contains `reference/backends/qwen-code/SKILL.md`.
3. Read `src/lingtai/tools/daemon/manual/reference/cli-backends/reference/backends/qwen-code/SKILL.md`.
   Check its YAML frontmatter: `name: daemon-backend-qwen-code`, non-empty
   `description`, and a present `last_changed_at`.
4. Confirm the child body contains each exact string: `qwen --version`,
   `qwen --help`, `no subcommand`, `backend_options`, `qwen3-coder-plus`,
   `qwen --yolo --model qwen3-coder-plus -p <prompt>`,
   `not validate, enumerate, or simulate`, `--prompt`, `--yolo`,
   `--approval-mode`, `QWEN_CODE_SYSTEM_SETTINGS_PATH`,
   `qwen-daemon-settings.json`, and
   `daemon(action='ask', input={'id': ..., 'message': ...})`.
5. Count the lines of the child file (shell `wc -l` or your file read tool's
   line count); record the number.

### Expected evidence
- [ ] Exactly one catalog entry resolves to the qwen-code child, named
      `daemon-backend-qwen-code`, with a non-empty description.
- [ ] The routing table contains a row pointing at
      `reference/backends/qwen-code/SKILL.md`.
- [ ] Child frontmatter has the pinned `name`, non-empty `description`, and
      `last_changed_at`.
- [ ] Every phrase in step 4 is present in the child body.
- [ ] The child file has `<= 90` lines.

### Pass / Fail
Pass when all evidence holds. Fail on any missing phrase, a missing or
duplicate catalog entry, a missing routing-table row, wrong frontmatter, or a
child grown past 90 lines — the submanual must route agents to the installed
CLI's live help (`qwen --version`, `qwen --help`, no subcommand), never become
a maintained flag catalog.

## Behavior D008 — active common-MCP CLI checkpoint and parent correction

- **id**: D008
- **title**: a live common-MCP CLI run records a cooperative checkpoint and
  drains one parent correction without changing terminal truth
- **guards**: `daemon-contract` § daemon_common provides cooperative
  checkpoints and terminal completion
  ([CONTRACT.md](CONTRACT.md#3-daemon_common-provides-cooperative-checkpoints-and-terminal-completion))
- **supersedes**: `tests/test_daemon_checkpoint.py` and
  `tests/test_daemon_run_dir.py::test_checkpoint_inbox_backfills_pre_checkpoint_live_state`
- **runner**: any LingTai agent with the `daemon` tool and a common-MCP CLI backend
- **prerequisites**: a live detached CLI run whose launch path mounts
  `daemon_common` (`claude-p`/`claude-code`, Codex, OpenCode, Qwen, or Kimi)
- **estimate**: 5 min

### Steps
1. Emanate a long-running task on a backend that mounts `daemon_common` and
   record its daemon id.
2. While it is live, call `daemon.ask` with one correction and retain the
   returned delivery fields.
3. Have the daemon call the strict `checkpoint` tool at a useful boundary.
4. Inspect the checkpoint response, `daemon.check`, the daemon notification
   mini-channel, and the run's terminal fields.

### Expected evidence
- [ ] The parent call returns `status="queued"`, `delivery="checkpoint"`, and
      an opaque `message_id`.
- [ ] One RunDir transaction increments and stores the checkpoint, drains that
      ID-bearing correction once, appends an event, and touches heartbeat.
- [ ] The checkpoint response returns the message, while `daemon.check`
      projects the latest checkpoint plus only a pending count.
- [ ] One unique nonterminal event on the built-in `daemon` channel wakes the
      parent and advances durable batch state.
- [ ] Terminal state, result, receipt, and `finish` requirements are unchanged;
      unsupported backends remain `busy` while active.
- [ ] A wake-publication failure reports that the checkpoint was recorded and
      still returns the drained message.

### Pass / Fail
Pass when the supported/unsupported matrix, drain-once acknowledgement,
nonterminal wake, trust/bounds/live gates, old-state compatibility, local
LingTai surface, and unchanged terminal fields all hold. Fail on chat-style or
preemptive delivery, message redelivery/loss, a terminal checkpoint, a false
backend capability claim, or any checkpoint that satisfies or mutates the
terminal completion receipt.

## Behavior D009 — a follow-up result never retires its run

- **id**: D009
- **title**: an `ask` follow-up notice is typed `daemon_followup` and leaves its
  run active in the parent's bounded daemon summary, and daemon wake deltas are
  never negative after a dismissal
- **guards**: `daemon-contract` § 6. Terminal notifications use published
  receipts, not attempted claims
  ([CONTRACT.md](CONTRACT.md#6-terminal-notifications-use-published-receipts-not-attempted-claims))
- **supersedes**: `tests/test_daemon_attention_delay.py` (classification and
  delta tables)
- **runner**: any LingTai agent with the `daemon`, `notification`, and `file`
  tools
- **prerequisites**: an agent working dir; a backend whose runs accept
  `daemon(action="ask")` (claude-code, codex, opencode/oh-my-pi, or cursor) with
  its CLI installed and authenticated
- **estimate**: 10 min

### Steps
1. Emanate one run on an ask-capable backend, e.g.
   `daemon(action="emanate", input={"tasks": [{"task": "Reply with exactly:
   READY", "backend": "codex"}]}, reasoning="probe")`. Record `ids[0]` as
   `<id>` and wait for its terminal notice.
2. Call `daemon(action="ask", input={"id": "<id>", "message": "Reply with
   exactly: FOLLOWUP"}, reasoning="probe")` and wait for the follow-up notice.
3. Read `.notification/daemon/<id>.json` with the `file` tool and inspect the
   appended events.
4. Read the newest tool result's `_meta.agent_meta.agent_state.daemon` summary
   (or `notification(action="check", input={}, reasoning="probe")` plus the same
   metadata on the next result).
5. Call `notification(action="dismiss", input={"channel": "daemon"},
   reasoning="probe")`, emanate one more trivial run, let the parent go ASLEEP
   and be woken by it, then read
   `_meta.agent_meta.agent_state.notification_wake.daemon` on the injected
   result.

### Expected evidence
- [ ] Step 3: the terminal event carries `"kind": "daemon_terminal"`; the
      follow-up event carries `"kind": "daemon_followup"` with a
      `follow-up ...` status, and both live in the same `<id>` mini-file.
- [ ] Step 4: the run is counted once — `run_count` includes `<id>` once,
      `terminal_run_count` counts it once, and `latest_terminal` still names the
      run's original terminal status rather than `follow-up completed`.
- [ ] Step 5: every `*_delta` in the wake provenance is `>= 0`, and the shrunk
      baseline is reported as `"baseline_reset": true` rather than a negative
      count.
- [ ] `daemons/<id>/daemon.json` keeps its terminal state and receipt unchanged
      throughout.

### Pass / Fail
Pass when all evidence is observed and no forbidden side effect occurs. Fail if
a follow-up is typed or counted as terminal, if it adds a second terminal for
the same run, if `latest_terminal` reports a follow-up status, or if any wake
delta is negative; record the evidence trail in the task report.

## Behavior D010 — daemon settings are exact and read-only

- **id**: D010
- **title**: the daemon owner projects its effective inventory as exactly five
  public fields and routes all change guidance to exact manual sections
- **guards**: `daemon-contract` § Daemon settings ownership
  ([CONTRACT.md](CONTRACT.md#daemon-settings-ownership))
- **supersedes**: `tests/test_daemon_settings.py` (projection and refusal paths)
- **runner**: any LingTai agent with the `daemon` tool
- **prerequisites**: a configured daemon capability
- **estimate**: 2 min

### Steps
1. Call `daemon(action="settings", input={}, reasoning="inspect")` and retain
   the complete response.
2. Confirm the row keys are exactly `max_turns`, `manager_pool_size`,
   `system_prompt_budget_chars`, and `timeout`, in that order.
3. Confirm every row has exactly `key`, `current`, `default`, `configurable`,
   and `comment`.
4. Open `daemon(action="manual", input={}, reasoning="inspect")`; follow every
   `comment` fragment to its exact heading and verify the omitted meaning,
   accepted values, source/precedence, canonical key, apply timing,
   authorization, and real owner change procedure are present there.
5. Call `daemon(action="list", input={}, reasoning="unchanged")` and confirm it
   still returns the ordinary daemon index. Do not alter configuration during
   this behavior check.

### Expected evidence
- [ ] SHOW contains exactly four rows in the owner-defined order.
- [ ] Every successful row exposes exactly the five contracted public fields.
- [ ] Every comment resolves to the exact owner-manual heading containing the
      operational details omitted from SHOW.
- [ ] No set, reset, writer, state, receipt, or other mutation operation is
      exposed, and the unchanged `list` action still succeeds.
- [ ] The automated unavailable-current case returns only the generic fixed
      whole-action failure; it never emits a partial or placeholder row.

### Pass / Fail
Pass when all evidence holds. Fail on a missing or extra key, a sixth public
field, a dangling manual pointer, any settings mutation route, partial output
on failure, or a regression in the unchanged `list` action.
