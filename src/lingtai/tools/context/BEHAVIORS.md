---
name: context-behavior-tests
behavior_version: 1
labt_version: 2
contract: CONTRACT.md
anatomy: ANATOMY.md
related_files:
  - src/lingtai/tools/context/__init__.py
  - src/lingtai/tools/context/manual/SKILL.md
  - src/lingtai/tools/context/_molt.py
  - src/lingtai/tools/context/_session_journal.py
  - src/lingtai/tools/context/_snapshots.py
  - src/lingtai/tools/system/karma.py
  - src/lingtai/tools/system/name.py
  - src/lingtai/tools/system/preset.py
  - src/lingtai/kernel/state.py
  - src/lingtai/kernel/base_agent/lifecycle.py
  - src/lingtai/kernel/nudge/goal.py
  - src/lingtai/intrinsic_skills/system-manual/reference/how-to-change-name/SKILL.md
  - src/lingtai/intrinsic_skills/system-manual/reference/how-to-change-name/scripts/change_name.py
  - tests/test_cli_integration.py
  - tests/test_molt_notification_persistence.py
  - tests/test_post_molt_notification.py
  - tests/test_preset_context_guard.py
  - tests/test_how_to_change_name_e2e.py
  - tests/test_goal_notification.py
  - tests/test_context_declared_tool_plugin.py
maintenance: |
  Written by the lifecycle/state behavior audit. Keep in sync with the clauses
  these LABTs guard and with the paired ANATOMY.md files: when CONTRACT.md
  (context or system) or ANATOMY.md changes agent-observable lifecycle behavior
  (signal-file transitions, molt persistence, post-molt channel, preset guard,
  naming semantics, goal reminders), update the matching LABT here in the same
  change and re-verify the tridirectional loop.
---
# Context Behavior Tests — lifecycle and state

LABT v1. These are self-contained agent-executable behavioral tests for the
lifecycle/state family: the 5-state machine driven by signal files
(`src/lingtai/kernel/state.py`, `src/lingtai/kernel/base_agent/lifecycle.py`),
molt persistence and the post-molt continuation channel
(`src/lingtai/tools/context/_molt.py`), the preset context-limit guard
(`src/lingtai/tools/system/preset.py`), identity naming
(`src/lingtai/tools/system/name.py`), and the protected goal reminder
(`src/lingtai/kernel/nudge/goal.py`). The family spans two contracts: the
`context` contract ([CONTRACT.md](CONTRACT.md)) owns molt and its side effects;
the `system` contract ([CONTRACT.md](../system/CONTRACT.md)) owns refresh,
sleep/suspend signal files, and the name actions. Every LABT below is
self-contained and executable verbatim by an agent with the tools listed in its
`runner` field; low-level mechanics stay in pytest.

## Behavior L001 — .sleep signal transitions the agent to ASLEEP (process stays alive)

- **id**: L001
- **title**: touching `.sleep` in a running agent's workdir puts it to sleep without killing the process
- **guards**: `system-contract` § State & storage
  ([CONTRACT.md](../system/CONTRACT.md#state--storage)) — `.sleep` = agent goes
  ASLEEP, process keeps running; state machine
  `ACTIVE/IDLE --(sleep)--> ASLEEP` (`src/lingtai/kernel/state.py`)
- **supersedes**: `tests/test_cli_integration.py::test_sleep_signal_triggers_asleep`
- **runner**: any LingTai agent with a `shell` and `file` tool on POSIX
  (Linux/macOS), with `lingtai` importable (installed, or
  `PYTHONPATH=<repo>/src`)
- **prerequisites**: a scratch agent workdir `<WD>` (empty directory);
  `init.json` inside it (exact content in step 1); the lifecycle loop needs no
  reachable LLM — the agent boots and idles without any turn
- **estimate**: 2 min

### Steps
1. Write `<WD>/init.json` with exactly:
   ```json
   {
     "manifest": {
       "agent_name": "integration-test", "language": "en",
       "llm": {"provider": "gemini", "model": "test-model", "api_key": "fake-key", "base_url": null},
       "capabilities": {}, "soul": {"delay": 5}, "stamina": 10,
       "context_limit": null, "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 5,
       "admin": {}
     },
     "principle": "", "covenant": "You are a test agent.", "pad": "", "lingtai": ""
   }
   ```
2. Start the agent in the background (capture output to `<WD>/boot.log`):
   `python -m lingtai run <WD>` (from a checkout: `PYTHONPATH=<repo>/src python -m lingtai run <WD>`).
   Record the process pid.
3. Wait (poll up to 30 s) until `<WD>/.agent.heartbeat`, `<WD>/.agent.lock`,
   and `<WD>/.agent.json` all exist.
4. Create the sleep signal: `touch <WD>/.sleep`.
5. Poll `<WD>/logs/events.jsonl` (up to 10 s) for a JSON line whose `type` is
   `sleep_received`.
6. Check that `<WD>/.sleep` no longer exists (the agent consumed it).
7. Check the agent process is still alive: `kill -0 <pid>` returns 0.
8. Cleanup: `touch <WD>/.suspend`, wait up to 20 s for the process to exit,
   then `kill -TERM <pid>` if it still runs.

### Expected evidence
- [ ] `<WD>/logs/events.jsonl` contains a `sleep_received` event.
- [ ] `<WD>/.sleep` was consumed (deleted by the agent).
- [ ] The agent process is still running after the transition (`kill -0` succeeds).

### Pass / Fail
Pass when all three evidence items hold: the event fired, the signal file was
consumed, and the process stayed alive — ASLEEP is a sleep, not a shutdown. Fail
if the process exits on `.sleep` (that would be suspend semantics) or no
`sleep_received` event is logged. Forbidden side effects: `.suspend` must not be
touched by this LABT, and `<WD>` must not be deleted.

## Behavior L002 — .suspend signal shuts the agent down to SUSPENDED

- **id**: L002
- **title**: touching `.suspend` in a running agent's workdir terminates the process (SUSPENDED)
- **guards**: `system-contract` § State & storage
  ([CONTRACT.md](../system/CONTRACT.md#state--storage)) — `.suspend` = process
  shuts down; state machine `ASLEEP --(.suspend/SIGINT)--> SUSPENDED`
  (`src/lingtai/kernel/state.py`)
- **supersedes**: `tests/test_cli_integration.py::test_suspend_triggers_shutdown`
- **runner**: any LingTai agent with a `shell` and `file` tool on POSIX, with
  `lingtai` importable (same environment as L001)
- **prerequisites**: same scratch workdir `<WD>` and `init.json` as L001 (step 1)
- **estimate**: 2 min

### Steps
1. Boot the agent exactly as in L001 steps 1–3 (background `python -m lingtai run <WD>`,
   wait for `.agent.heartbeat`, `.agent.lock`, `.agent.json`). Record the pid.
2. Create the suspend signal: `touch <WD>/.suspend`.
3. Wait (up to 15 s) for the process to exit (`kill -0 <pid>` fails).
4. Check that `<WD>/.suspend` no longer exists (consumed).
5. Check `<WD>/logs/events.jsonl` for a `suspend_received` event (best effort —
   the process may exit before the flush; the file-deletion + exit evidence below
   is sufficient on its own).
6. Verify `<WD>` still exists and `.agent.json` is intact (SUSPENDED preserves
   the workdir — this is not nirvana).

### Expected evidence
- [ ] The agent process exited after `.suspend` was touched.
- [ ] `<WD>/.suspend` was consumed (deleted).
- [ ] `<WD>` still exists with `.agent.json` present.

### Pass / Fail
Pass when the process exits and the signal file is consumed. Fail if the process
stays alive (that would be sleep semantics) or the workdir is destroyed (that
would be nirvana). A missing `suspend_received` log line does NOT fail the LABT
if process exit + file consumption are observed.

## Behavior L003 — agent-initiated molt preserves durable stores and notification files

- **id**: L003
- **title**: a successful `context(action="molt")` preserves `.notification/` files, the durable stores, and the session journal, and persists the summary
- **guards**: `context-contract` § Molt safety invariants
  ([CONTRACT.md](CONTRACT.md#molt-safety-invariants)) — durable history and
  summary paths remain under `history/` and `system/summaries/`; notification
  files survive the shed
- **supersedes**: `tests/test_molt_notification_persistence.py::test_notification_files_survive_agent_molt`
- **runner**: any LingTai agent with `context`, `file`, and `shell` tools
- **prerequisites**: a **disposable executor agent** — this LABT performs a real
  molt on the executing agent's own context. The session-journal sub-entry must
  exist BEFORE the molt call (the kernel refuses a molt without it)
- **estimate**: 3 min

### Steps
1. Create the session-journal sub-entry
   `<WD>/knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md` with exactly:
   ```markdown
   ---
   name: 2026-06-19-molt-1-test
   description: A test session journal entry for the molt gate.
   date: 2026-06-19
   molt_count: 1
   type: session-journal
   ---

   ## What this segment was about
   Testing.

   ## Accomplishments
   Wrote a valid session journal.
   ```
2. Create `<WD>/system/pad.md` containing exactly the line `pad sentinel`.
3. Create `<WD>/.notification/email.json` with exactly:
   `{"header": "test notification", "icon": "📧", "priority": "normal", "data": {"test": true}}`.
4. Call:
   ```
   context(action="molt", input={"summary": "LABT L003: verify durable persistence", "session_journal_path": "knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md", "keep_tool_calls": null, "keep_last": null}, reasoning="LABT: prove durable state survives molt")
   ```
5. Read the result; record `molt_count` and `summary_path` (a relative path under
   `system/summaries/`).
6. Verify `<WD>/.notification/` still exists and `email.json` still exists with
   `header == "test notification"` unchanged.
7. Verify `<WD>/system/pad.md` still exists and its content is unchanged.
8. Verify the journal entry
   `<WD>/knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md` still exists.
9. Resolve `<summary_path>` from step 5 under `<WD>`; verify the file exists and
   its content mentions `session_journal_path`.

### Expected evidence
- [ ] Molt result `status == "ok"` and `molt_count` is an integer ≥ 1.
- [ ] `.notification/email.json` survived with unchanged content.
- [ ] `system/pad.md` survived with unchanged content.
- [ ] The session-journal sub-entry survived.
- [ ] The summary file exists at `<WD>/system/summaries/molt_<count>_<ts>.md` and
      records `session_journal_path`.

### Pass / Fail
Pass when all evidence holds. Fail if the molt was refused, if any notification
file, durable store, or journal entry disappeared, or if `summary_path` does not
resolve. Forbidden side effects: the molt must not delete `.notification/`,
`system/pad.md`, or the journal entry.

## Behavior L004 — post-molt continuation channel

- **id**: L004
- **title**: every molt publishes `.notification/post-molt.json` with continuation identity and a reason-required ack
- **guards**: `context-contract` § Passive lifecycle scenarios
  ([CONTRACT.md](CONTRACT.md#passive-lifecycle-scenarios)) — "updates
  `molt_count`, writes its summary, and publishes the post-molt reminder"
- **supersedes**: `tests/test_post_molt_notification.py`
  (`test_agent_molt_publishes_post_molt_with_reasoning`,
  `test_agent_molt_carries_continuation_fields`,
  `test_instructions_spell_out_reconstruct_then_ack`)
- **runner**: any LingTai agent with `context`, `notification`, and `file` tools
- **prerequisites**: a disposable executor agent; stage the same journal entry
  and pad file as L003 steps 1–2 before molting
- **estimate**: 3 min

### Steps
1. Stage the journal entry and `<WD>/system/pad.md` exactly as in L003 steps 1–2.
2. Call:
   ```
   context(action="molt", input={"summary": "LABT L004: continuation channel", "session_journal_path": "knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md", "keep_tool_calls": null, "keep_last": null}, reasoning="LABT: verify post-molt continuation")
   ```
   Record `molt_count` from the result.
3. Read `<WD>/.notification/post-molt.json` (UTF-8 JSON) and verify:
   - `header` contains `post-molt`; `priority == "high"`;
   - `data.initiator == "agent"`; `data.molt_count` equals the result's `molt_count`;
   - `data.molt_id` starts with `molt-<molt_count>-`;
   - `data.molt_at` is a non-empty ISO-8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`);
   - `data.source_agent` equals the agent's true name;
   - `data.ack_options == ["continue", "defer", "obsolete"]`;
   - `data.reminder` is non-empty; `data.summary_path` is present;
     `data.session_journal_path` is present; `data.reasoning` equals
     `"LABT: verify post-molt continuation"`.
4. Verify `data` does NOT contain a `next_action` key (no heuristic extraction).
5. Verify `instructions` is non-empty and mentions `pad`, `summary`,
   `human-channel`, the labels `continue`/`defer`/`obsolete`, and the taught
   dismiss call `notification(action='dismiss_channel', input={'channel': 'post-molt', 'force': null, 'reason': 'continue: ...'}, reasoning='...')`.
6. Attempt dismissal WITHOUT a reason:
   ```
   notification(action="dismiss_channel", input={"channel": "post-molt"}, reasoning="LABT")
   ```
   Expect an error result with `reason == "missing_ack_reason"`.
7. Dismiss WITH a reason:
   ```
   notification(action="dismiss_channel", input={"channel": "post-molt", "force": null, "reason": "continue: LABT L004 complete"}, reasoning="LABT")
   ```
   Expect `status == "ok"`; verify `<WD>/.notification/post-molt.json` is gone.

### Expected evidence
- [ ] `post-molt.json` exists with every field and value from step 3.
- [ ] No `next_action` key in `data`.
- [ ] Dismiss without a reason is refused with `missing_ack_reason`.
- [ ] Dismiss with `reason: 'continue: ...'` succeeds and clears the channel.

### Pass / Fail
Pass when all evidence holds. Fail if fields are missing/mismatched, a
`next_action` heuristic field appears, or the ack gate is bypassed (dismiss
succeeds without a reason). Forbidden side effects: legacy `molt` channel
cleanup must never remove `post-molt.json`.

## Behavior L005 — preset context guard: refresh refused when context exceeds the target preset

- **id**: L005
- **title**: `system(action="refresh", input={"preset": ...})` is refused with a molt-first error when the target preset's `context_limit` is below current usage
- **guards**: `system-contract` § Tool surface
  ([CONTRACT.md](../system/CONTRACT.md#tool-surface)) — `refresh` returns
  `{status: "error", message}` on oversize context; event
  `preset_swap_refused_oversize`
- **supersedes**: `tests/test_preset_context_guard.py`
  (`test_swap_refused_when_current_context_exceeds_target_limit`,
  `test_guard_reads_context_limit_from_llm_block`,
  `test_revert_refused_when_current_context_exceeds_default_limit`)
- **runner**: any LingTai agent with `system` and `file` tools
- **prerequisites**: a **disposable agent** whose `<WD>/init.json` you may edit
  (this LABT mutates the `manifest.preset` block and may persist
  `manifest.preset.default`); the preset library and `.env` file created in the
  steps; your current context usage must exceed 1000 tokens (see step 4)
- **estimate**: 3 min

### Steps
1. Create a preset library `<PLIB>/` with four files. Use your own working
   provider values (api key/base_url from your environment) but keep the
   `context_limit` values exactly:
   - `big.json`: `{"name": "big", "manifest": {"llm": {"provider": "<yours>", "model": "<yours>", "api_key": "<yours>", "context_limit": 200000}, "capabilities": {"file": {}}}}`
   - `small.json`: same shape with `"name": "small"` and `"context_limit": 1000`
     at the **manifest root** (sibling of `llm`)
   - `tight.json`: same shape with `"name": "tight"` and `"context_limit": 1000`
     **inside** `manifest.llm` (nested layout — the guard must find it there too)
   - `no_limit.json`: `{"name": "no_limit", "manifest": {"llm": {"provider": "<yours>", "model": "<yours>", "api_key": "<yours>"}, "capabilities": {"file": {}}}}` — no `context_limit` field at all
2. Write `<WD>/.env` containing `P1KEY=sk-test` (or any value your provider
   reads) and set `manifest.env_file` in `<WD>/init.json` to `"<WD>/.env"`.
3. Edit `<WD>/init.json` so `manifest.preset` is:
   ```json
   {"path": "<PLIB>", "active": "<PLIB>/big.json", "default": "<PLIB>/big.json",
    "allowed": ["<PLIB>/big.json", "<PLIB>/small.json", "<PLIB>/tight.json", "<PLIB>/no_limit.json"]}
   ```
4. Read your current context usage (`ctx_total_tokens`) from your
   `_meta.agent_meta.agent_state`; if it is ≤ 1000, perform a few tool calls
   (any tool results add tokens) and re-read until it exceeds 1000.
5. Call `system(action="refresh", input={"preset": "<PLIB>/small.json"}, reasoning="LABT L005")`.
   Expect `status == "error"`; the message must contain `molt` and the target
   limit `1000` (and may quote the current usage).
6. Verify `<WD>/init.json` → `manifest.preset.active` is unchanged
   (`"<PLIB>/big.json"`): the swap was NOT applied.
7. Repeat step 5 with `<PLIB>/tight.json`; expect the same refusal (limit read
   from the nested `manifest.llm.context_limit`).
8. Verify `<WD>/logs/events.jsonl` contains a `preset_swap_refused_oversize` event.
9. Positive control: call
   `system(action="refresh", input={"preset": "<PLIB>/no_limit.json"}, reasoning="LABT L005")`
   — a preset without `context_limit` skips the guard; expect `status == "ok"`
   (or an activation error that does NOT mention molt/context limit).
10. Revert control: edit `manifest.preset.default` to `"<PLIB>/small.json"`, then
    call `system(action="refresh", input={"revert_preset": true}, reasoning="LABT L005")`.
    Expect `status == "error"` mentioning `molt`; `manifest.preset.active` stays
    `"<PLIB>/big.json"`.

### Expected evidence
- [ ] Swap to `small.json` refused; error mentions `molt` and `1000`.
- [ ] `manifest.preset.active` unchanged after the refusal.
- [ ] Swap to `tight.json` refused (nested-limit layout respected).
- [ ] `preset_swap_refused_oversize` event logged.
- [ ] `no_limit.json` swap is NOT refused by the guard.
- [ ] `revert_preset` to a too-narrow default is refused and `active` unchanged.

### Pass / Fail
Pass when all evidence holds. Fail if a too-narrow swap (or revert) succeeds,
if the swap is applied despite refusal, or if a preset without `context_limit`
is refused by the guard. Forbidden side effects: an unauthorized preset must
still fail with the not-found/unauthorized error (the guard must not mask it).

## Behavior L006 — physical rename of a live POSIX agent (address/workdir)

- **id**: L006
- **title**: `change_name.py` renames a live agent's workdir/address via suspend → no-replace rename → resume, without changing `agent_name` or `agent_id`
- **guards**: `system-contract` § Routing Card
  ([CONTRACT.md](../system/CONTRACT.md#routing-card)) — name actions "mutate
  neither address nor working directory — that is the operator migration
  workflow in `system-manual`"; V1 contract of
  `reference/how-to-change-name/SKILL.md`
- **supersedes**: `tests/test_how_to_change_name_e2e.py::test_real_agent_suspend_rename_rebase_and_resume`
- **runner**: an agent with `shell` and `file` tools on POSIX (Linux/macOS);
  Windows and network filesystems are out of scope
- **prerequisites**: POSIX host; Python ≥ 3.10 that can import `lingtai`;
  `<REPO>` = the lingtai-kernel checkout; a scratch root `<ROOT>` (empty); the
  helper source at
  `<REPO>/src/lingtai/intrinsic_skills/system-manual/reference/how-to-change-name/scripts/change_name.py`
- **estimate**: 5 min

### Steps
1. Create scratch dirs `<ROOT>/old` and `<ROOT>/new` (new must NOT exist at
   rename time). Create a venv inside the old workdir:
   `python -m venv --copies --without-pip --system-site-packages <ROOT>/old/runtime/venv`.
2. Write `<ROOT>/old/init.json` with exactly:
   ```json
   {
     "manifest": {
       "agent_name": "temporary-true-name", "language": "en",
       "llm": {"provider": "gemini", "model": "test", "api_key": "fake", "base_url": null},
       "capabilities": {}, "soul": {"delay": 60}, "stamina": 10,
       "context_limit": null, "molt_pressure": 0.8, "molt_prompt": "", "max_turns": 5,
       "admin": {}
     },
     "principle": "", "covenant": "No network.", "pad": "", "lingtai": "",
     "venv_path": "<ROOT>/old/runtime/venv"
   }
   ```
3. Copy the helper and make it executable:
   `cp <REPO>/src/lingtai/intrinsic_skills/system-manual/reference/how-to-change-name/scripts/change_name.py <ROOT>/old/change_name.py && chmod 755 <ROOT>/old/change_name.py`.
4. Boot the agent in the background:
   `cd <ROOT>/old && PYTHONPATH=<REPO>/src <ROOT>/old/runtime/venv/bin/python -m lingtai run <ROOT>/old`.
   Wait (up to 25 s) for `<ROOT>/old/.agent.heartbeat`, `.agent.lock`, and
   `.agent.json`.
5. Read `<ROOT>/old/.agent.json`; record `agent_id`, `agent_name`
   (must be `temporary-true-name`), and `address` (must be `old`).
6. Run the helper in the foreground (must return exit code 0):
   `PYTHONPATH=<REPO>/src <ROOT>/old/runtime/venv/bin/python <ROOT>/old/change_name.py <ROOT>/old new --timeout 20`.
7. Wait (up to 25 s) for `<ROOT>/new/.agent.heartbeat` to reappear.
8. Verify: `<ROOT>/old` no longer exists; `<ROOT>/new` exists; read
   `<ROOT>/new/.agent.json`: `agent_id` unchanged from step 5, `agent_name` still
   `temporary-true-name`, `address == "new"`; read `<ROOT>/new/init.json`:
   `venv_path == "<ROOT>/new/runtime/venv"` (rebased).
9. Cleanup: `touch <ROOT>/new/.suspend`; wait up to 20 s for the process to
   exit; `kill -TERM` any surviving `python -m lingtai run <ROOT>/new` pid.

### Expected evidence
- [ ] The helper exits 0.
- [ ] `<ROOT>/old` is gone; `<ROOT>/new` exists with a fresh `.agent.heartbeat`.
- [ ] `agent_id` and `agent_name` are identical before/after; `address` changed to `new`.
- [ ] `venv_path` in the new `init.json` points into `<ROOT>/new/runtime/venv`.

### Pass / Fail
Pass when all evidence holds. Fail if `agent_id`/`agent_name` changed, if the
old directory was replaced instead of renamed (RENAME_NOREPLACE violation), or
if the resumed agent does not come back up under the new path.

## Behavior L007 — name_set immutable / name_nickname mutable

- **id**: L007
- **title**: `system(action="name_set")` sets the true name once and refuses thereafter; `system(action="name_nickname")` stays mutable and clears on empty; neither renames the workdir
- **guards**: `system-contract` § Tool surface
  ([CONTRACT.md](../system/CONTRACT.md#tool-surface)) — `name_set` errors when
  a true name is already set (immutable); § Anchored claims
  ([CONTRACT.md](../system/CONTRACT.md#anchored-claims)) — "A true name stays
  immutable and neither name action renames the workdir"
- **supersedes**: `tests/test_tool_family_system_migration.py::test_name_actions_preserve_identity_semantics`
- **runner**: any LingTai agent with a `shell` tool; the subject is a fresh
  in-process agent created by an embedded script (a newborn has no true name,
  which is the only state in which `name_set` succeeds)
- **prerequisites**: `lingtai` importable from `<REPO>/src`; `python` available
- **estimate**: 2 min

### Steps
1. Write the script `<TMP>/labt_name.py` with exactly:
   ```python
   import json, sys, tempfile
   from pathlib import Path
   from unittest.mock import MagicMock
   sys.path.insert(0, "<REPO>/src")
   from lingtai.agent import Agent
   from lingtai.tools.system import handle as system_handle

   svc = MagicMock()
   svc.get_adapter.return_value = MagicMock()
   svc.provider = "gemini"
   svc.model = "gemini-test"
   workdir = Path(tempfile.mkdtemp()) / "named"
   agent = Agent(service=svc, working_dir=workdir)
   before_dir = agent._working_dir

   r1 = system_handle(agent, {"action": "name_set", "input": {"content": "wukong"}})
   assert r1.get("name") == "wukong", r1
   r2 = system_handle(agent, {"action": "name_set", "input": {"content": "bajie"}})
   assert "error" in r2 and "name_nickname" in r2["error"], r2
   r3 = system_handle(agent, {"action": "name_set", "input": {"content": ""}})
   assert "error" in r3, r3
   for value, expected in (("monkey", "monkey"), ("king", "king"), ("", None)):
       got = system_handle(agent, {"action": "name_nickname", "input": {"content": value}})
       assert got["nickname"] == expected, (value, got)
   manifest = json.loads((workdir / ".agent.json").read_text())
   assert manifest["agent_name"] == "wukong", manifest
   assert manifest["nickname"] is None, manifest
   assert "wukong" in agent._prompt_manager.read_section("identity")
   assert agent._working_dir == before_dir
   assert manifest["address"] == before_dir.name
   print("LABT L007 PASS")
   ```
2. Run it: `python <TMP>/labt_name.py`. Exit code must be 0.

### Expected evidence
- [ ] First `name_set` returns `{status: "ok", name: "wukong"}`.
- [ ] Second `name_set` returns an error whose text mentions `name_nickname`.
- [ ] Empty `name_set` is refused.
- [ ] `name_nickname` accepts successive values and returns `nickname: null` on empty.
- [ ] `.agent.json` persists `agent_name == "wukong"`, `nickname == null`; the
      protected prompt `identity` section contains `wukong`.
- [ ] The workdir and `address` are unchanged by any name action.
- [ ] Script prints `LABT L007 PASS` and exits 0.

### Pass / Fail
Pass when the script exits 0 and prints `LABT L007 PASS`. Fail if a second
`name_set` overwrites the true name, an empty `name_set` clears it, or any name
action renames the workdir/address.

## Behavior L008 — goal notification: active goal reminder after idle delay

- **id**: L008
- **title**: an active `.notification/goal.json` with a due idle delay publishes one `goal.reminder` event into `.notification/system.json`
- **guards**: `src/lingtai/CONTRACT.md` § Contract rules rule 6
  ([CONTRACT.md](../../CONTRACT.md#contract-rules)) — "The goal reminder is
  explicitly a separate protected-goal system notification"
- **supersedes**: `tests/test_goal_notification.py`
  (`test_goal_reminder_publishes_short_system_event_after_idle_delay`,
  `test_goal_reminder_does_not_duplicate_existing_event`,
  `test_goal_reminder_clears_when_goal_becomes_done`)
- **runner**: any LingTai agent with `shell` and `file` tools on POSIX, with
  `lingtai` importable (the target agent must be IDLE — the goal check runs in
  the target's own idle loop, never while it is ACTIVE)
- **prerequisites**: a scratch agent workdir `<WD>` booted as in L001 steps 1–3
  (a freshly booted agent with no messages is IDLE); the goal reminder delay is
  set per-goal via `data.reminder_delay_seconds`
- **estimate**: 4 min

### Steps
1. Boot the target agent exactly as in L001 steps 1–3 (background
   `python -m lingtai run <WD>`, wait for heartbeat files).
2. Write `<WD>/.notification/goal.json` with exactly:
   ```json
   {
     "header": "Active goal", "icon": "🎯", "priority": "normal",
     "instructions": "Current active goal. Details live here; see the goal manual under system-manual.",
     "data": {"id": "demo", "status": "active", "reminder_delay_seconds": 1}
   }
   ```
3. Poll `<WD>/.notification/system.json` (up to 90 s — the check cadence is
   10 s) until `data.events` contains an event with
   `source == "goal.reminder"` and `ref_id == "goal:demo"`.
4. Verify the event `body` equals exactly:
   `Goal reminder: read .notification/goal.json and follow its instructions; see the goal manual under system-manual.`
5. Verify there is exactly ONE `goal.reminder` event for `goal:demo` (dedup by
   ref_id) — poll two more check cycles and confirm the count stays 1.
6. Rewrite `<WD>/.notification/goal.json` with `"status": "done"` (same id),
   wait up to 30 s, and verify `goal:demo` is no longer in `data.events`.
7. Delete `<WD>/.notification/goal.json`, wait up to 30 s, and verify the
   `system` channel is clear of `goal.reminder` events.
8. Write a new goal with `"id": "replacement"` (active, delay 1), wait up to
   60 s, and verify a fresh event with `ref_id == "goal:replacement"` appears.

### Expected evidence
- [ ] `goal.reminder` event with `ref_id == "goal:demo"` and the exact body text.
- [ ] Exactly one event per goal id (no duplicates across check cycles).
- [ ] Event cleared when the goal becomes `done` and when `goal.json` is deleted.
- [ ] A new goal id produces a new `goal:replacement` event.

### Pass / Fail
Pass when all evidence holds. Fail if no reminder publishes while IDLE, if
duplicates accumulate, if a `done`/deleted goal keeps its reminder, or if a
reminder publishes while the agent is not IDLE. Forbidden side effects: the
reminder must never modify `goal.json` itself.
