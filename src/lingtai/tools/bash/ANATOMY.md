---
related_files:
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/ANATOMY.md
  - src/lingtai/adapters/windows/ANATOMY.md
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/adapters/posix/ANATOMY.md
  - src/lingtai/tools/bash/BEHAVIORS.md
  - src/lingtai/tools/bash/__init__.py
  - src/lingtai/kernel/execution_workspace.py
  - src/lingtai/tools/bash/_tool_family.py
  - src/lingtai/tools/tool_family/settings.py
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/tools/tool_family/CONTRACT.md
  - src/lingtai/tools/bash/_async_supervisor.py
  - src/lingtai/tools/bash/_async_process.py
  - src/lingtai/tools/bash/_state_lock.py
  - src/lingtai/tools/bash/_shell_dialect.py
  - src/lingtai/adapters/bash.py
  - src/lingtai/adapters/bash_process.py
  - src/lingtai/adapters/bash_state_lock.py
  - src/lingtai/adapters/shell.py
  - src/lingtai/adapters/shell_process.py
  - src/lingtai/adapters/shell_state_lock.py
  - src/lingtai/adapters/windows/powershell.py
  - src/lingtai/adapters/windows/powershell_process.py
  - src/lingtai/adapters/windows/powershell_state_lock.py
  - src/lingtai/adapters/posix/bash.py
  - src/lingtai/adapters/posix/bash_process.py
  - src/lingtai/adapters/posix/bash_state_lock.py
  - src/lingtai/tools/bash/bash_policy.json
  - src/lingtai/tools/bash/CONTRACT.md
  - src/lingtai/tools/bash/manual/SKILL.md
  - tests/test_bash_async.py
  - tests/test_execution_workspace.py
  - tests/test_shell_tool_plugin_declaration.py
  - tests/test_shell_progressive_disclosure.py
  - tests/test_shell_settings.py
  - tests/test_layers_bash.py
  - src/lingtai/tools/bash/glossary-en.md
  - src/lingtai/tools/bash/glossary-zh.md
  - src/lingtai/tools/bash/glossary-wen.md
  - src/lingtai/tools/bash/_output_hygiene.py
  - src/lingtai/tools/bash/manual/reference/async-jobs/SKILL.md
  - src/lingtai/tools/bash/manual/reference/debugging-cleanup/SKILL.md
  - src/lingtai/tools/bash/manual/reference/notification-reminders/SKILL.md
  - src/lingtai/tools/bash/manual/reference/scheduled-work/SKILL.md
  - src/lingtai/tools/bash/powershell_policy.json
  - src/lingtai/tools/daemon/ANATOMY.md
  - src/lingtai/tools/daemon/execution_host.py
  - src/lingtai/tools/daemon/shell_prompt_events.py
  - tests/test_daemon_shell_prompt_events.py
  - tests/test_daemon_detached_supervisor.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# core/shell (retained implementation: bash)

Canonical `shell` capability — shell command execution with file-based policy; PR1 retains the internal Bash package and durable namespace. Adds the
ability to run shell commands. This is a capability (not intrinsic) because
not every agent should have shell access — it's a powerful ability that should
be explicitly opted into.

## Components

- `bash/__init__.py` — the execution engine plus policy, sync execution, and durable async manager orchestration. It declares no schema or description of its own: the package's single public pair is re-exported from `_tool_family.py` at `__init__.py:43` under the canonical `get_schema`/`get_description` names, so there is exactly one model-facing surface and no second flat one to drift against. `setup` is only composition wiring: it passes policy/dialect overrides as immutable configuration to the static `DECLARATION` in `_tool_family.py`, then asks `register_agent_tool_plugins` to reserve, bind, activate, and mount the public `shell` tool through the official host path. `ShellManager` (`__init__.py:330`, aliased `BashManager` at `__init__.py:1467`) owns validation, manager semantics, durable-state rehydration, notification publication, and poll/cancel consumption; its `handle` dispatches `poll`/`cancel`/`run` only — `settings` and `manual` are family children and never reach it. Its async start receipt and durable reminder body both teach the registered envelope call `shell(action="poll", input={"job_id": ...})`. `_augment_command_result` adds `ok`/`command_status`/`warning` fidelity fields (`__init__.py:191`). Sync `_run_sync` feeds a dialect's `stdin_script` through `subprocess.run(input=...)` so the PowerShell bootstrap receives the real command over UTF-8 stdin.
- `bash/_tool_family.py` — the public/model-facing `shell` LTP v2 envelope. The static declaration opts Shell into the generic read-only `settings` child immediately before `manual`, so its five branches use `input.anyOf`; `_shell_setting_rows` reads seven existing manager/runtime facts into exact five-field rows and marks command policy for full private redaction without rendering its rules. `ShellFamilyDispatcher` otherwise keeps the existing run/poll/cancel flattening and raw-result delegation unchanged. Neither `settings` nor `manual` reaches `ShellManager`, and the exact owner procedures and setting semantics route to `bash/manual/SKILL.md`.
- `bash/_shell_dialect.py` — the Bash-local `ShellDialect` port and serializable `ShellInvocation`; the POSIX extraction helper preserves the existing policy grammar. `ShellInvocation` gains an optional `stdin_script`: when set, the script is not placed on the child command line; spawners instead write it to the child's stdin as UTF-8 (PowerShell's ASCII-bootstrap transport). `posix_shell_display_name()` derives the model-facing POSIX label with the same Darwin login-shell resolver (`_darwin_default_shell`) used by execution, so the setup-time description reports Zsh when the current host environment resolves zsh.
- `adapters/posix/bash.py` — `PosixBashDialect`, the first production adapter; it provides POSIX policy extraction and script-form shell invocation.
- `adapters/shell.py` — `select_shell_dialect`, the outer selector for POSIX and PowerShell dialects; `adapters/bash.py` remains a private compatibility selector.
- `bash/_async_process.py` and `bash/_state_lock.py` — retained implementation Ports (also exported as `ShellAsyncProcessPort`/`ShellStateLockPort`) for neutral process refs/observations/owned lifecycle handles and cross-process state serialization.
- `adapters/shell_process.py` and `adapters/shell_state_lock.py` — canonical outer selectors; the old Bash-named selectors remain compatibility-only.
- `adapters/posix/bash_process.py` and `adapters/posix/bash_state_lock.py` — production POSIX implementations owning `Popen`, identity/liveness, process groups/signals/quiescence, exact waits, and `flock`.
- `adapters/windows/powershell.py`, `powershell_process.py`, and `powershell_state_lock.py` — PowerShell 7 dialect (ASCII-only cmdline + UTF-8 stdin bootstrap), Job Object process-tree ownership, and native cross-process byte-range locking.
- `adapters/windows/gitbash.py` — Git Bash (Git for Windows) fallback dialect with an explicit `bash -lc` spawn form and a WSL-launcher-rejecting `discover_git_bash`; see the Git Bash / MSYS pitfall list (path mangling, `/c/...` translation, `usr\bin` PATH prepend, login-shell coreutils gap, ASLR spawn-failure retry class, WSL-bash ambiguity) in the Notes of `adapters/windows/ANATOMY.md`.
- `bash/_async_supervisor.py` — private detached policy runner that selects the same Ports, claims leases, delegates spawn/wait, and atomically persists terminal truth.
- `tools/daemon/execution_host.py` + `tools/daemon/shell_prompt_events.py` — the sole detached-daemon composition branch. When selected `shell` is reconstructed under `DetachedDaemonExecutionHost`, its private `_setup_detached_daemon_shell()` composer supplies a run-local `DaemonShellPromptEventAdapter` as the existing `notifications` grant and `<run>/shell-jobs` as its async state namespace; command cwd stays the granted parent task workdir. Activation/rehydration therefore cannot inspect or claim `<parent>/system/jobs`. Its two Shell publications enqueue bounded, ref-deduplicated RunDir prompt events instead of using the supervisor stub, Agent store, or `.notification`; a queue-full `False` is reconciled in the live detached manager with capped backoff. The retained Shell manager/supervisor still owns async job state and poll/cancel.
- `bash/_output_hygiene.py` — output hygiene at the shell-tool boundary. Small dependency-free helpers applied before output is returned, stripping ANSI/CSI color and cursor sequences, C0/C1 control bytes (bell, backspace, OSC title sequences, DEL), and shell-startup noise lines (`bash: no job control in this shell`, `tcsetattr: ...`) that would otherwise pollute tool results, waste context, and confuse plain-text parsers.
- `bash/powershell_policy.json` — the PowerShell-dialect counterpart to `bash_policy.json`, denying the cmdlet/alias spellings of the same classes (`Remove-Item`/`ri`/`rm`/`del`, `Format-Volume`/`Clear-Disk`, `Stop-Computer`/`Stop-Process`, `Invoke-Expression`/`iex`, `Set-Acl`/`icacls`/`takeown`, `Invoke-WebRequest`/`iwr`, `Set-ExecutionPolicy`, `reg`/`sc`).
- `bash/bash_policy.json` — default denylist policy shipped with the kernel. Denies destructive (`rm`, `rmdir`, `shred`, `dd`), privilege escalation (`sudo`, `su`, `doas`), permission changes (`chmod`, `chown`, `chgrp`), disk management (`mount`, `umount`, `mkfs`, `fdisk`), package managers (`apt`, `apt-get`, `yum`, `dnf`, `brew`), process control (`kill`, `killall`, `pkill`, `shutdown`, `reboot`, `systemctl`), network (`nc`, `ncat`), and code execution (`eval`, `exec`).

## Public API

The canonical `shell` tool is a migrated LTP v2 family (`src/lingtai/tools/CONTRACT.md`). Its model-facing root is exactly four fields, and the action's own inputs live under `input`:

| Root field   | Type    | Description |
|--------------|---------|-------------|
| `action`     | string  | Required. One of `run`, `poll`, `cancel`, `settings`, `manual`. No default. |
| `input`      | object  | Required. Strict, action-specific input; cross-action keys are rejected at dispatch. |
| `reasoning`  | string  | Required. Host InvocationContext/audit metadata; never forwarded into `input`. |
| `summarize`  | boolean | Optional root result post-processing control (default false); never forwarded into `input`. |

Per-action `input` (run-only fields exist only in `run`; `job_id` only in `poll`/`cancel`):

| Action   | `input` field  | Type    | Description |
|----------|----------------|---------|-------------|
| `run`    | `command`      | string  | Shell command to execute (the one genuinely required, non-nullable field) |
| `run`    | `timeout`      | number\|null | Timeout in seconds; `null` → default 30, sync only. Hard ceiling `LINGTAI_TOOL_TIMEOUT_MAX_SECONDS` (default 120): values above it are refused with an `async=true` steering message |
| `run`    | `working_dir`  | string\|null | Working directory; `null`/`""` → the agent working dir, and it must stay inside that sandbox |
| `run`    | `async`        | boolean\|null | `true` runs in background and returns `job_id` immediately; `null` → false |
| `run`    | `reminder`     | number\|null | Last-resort async wake delay; `null` → default 1800. Consumed/validated only for async `run` |
| `poll`   | `job_id`       | string  | Job ID returned by an async run |
| `cancel` | `job_id`       | string  | Job ID returned by an async run |
| `settings` | —             | —       | Strict empty `{}`; returns only read-only five-field setting rows |
| `manual` | —              | —       | Strict empty `{}`; performs no shell operation |

**Settings** (`action="settings"`): Reads fresh Shell-owned facts and returns
only `key`, `current`, `default`, `configurable`, and an exact `shell-manual`
section pointer. Provider failure is whole-action and command-policy values are
fully redacted; there is no mutation API.

**Manual** (`action="manual"`): Returns the shared ManualTool contract result — full `shell-manual` body at `content[0].text`, host-local path at `structuredContent.manual_path` — verbatim, with no double wrapping. It reaches no execution engine, spawns no process, and touches no job state, and it serves the installed manual body/path unchanged (`.library/intrinsic/capabilities/shell/SKILL.md`).

**Sync mode** (`input.async=false`, default): Returns `{status, exit_code, stdout, stderr, ok, command_status[, warning]}` once the command completes, or `{status: "error", message}` only when the shell itself could not run it (empty command, policy denial, invalid/non-finite timeout, timeout above the hard ceiling `LINGTAI_TOOL_TIMEOUT_MAX_SECONDS` — steered to `async=true`, timeout, spawn failure). On Windows the sync run is contained in a kill-on-close Job Object (`_run_sync_contained` → `win32_job.spawn_into_job`): a timeout terminates the whole tree and drains pipes for a bounded window; on success, closing the job handle also terminates any surviving descendant, so a background process started by a sync command does not outlive it — use async mode for work that must outlive the command.

**Async mode** (`input.async=true`): Returns `{status: "ok", job_id, pid, message}` immediately. Initial durable state carries both a tokenized finite supervisor-start lease and a bounded `return_handoff`. The supervisor must claim/recheck the start lease before adapter spawn; the parent then atomically replaces the crash-fallback reminder deadline with `returned_at + reminder` and arms the return handoff before returning. `status: ok` requires winning that valid pending-to-armed transition (or exact completed/failed truth under the still-valid guard); a late owner after expiry returns a pollable error with `job_id`/`pid` instead of false success. Any rehydrated old timer defers while that handoff is pending. The detached supervisor owns durable lease/cancellation/terminal policy and selects the Bash-local process Port; the POSIX adapter owns the command handle, spawn, exact wait, and process-tree cancellation. The supervisor atomically records `{cwd, started_at, finished_at, exit_status_known, exit_code}` from that exact owned wait. A fresh manager never consumes unknown merely because a command process ref vanished: it waits/reloads while the recorded supervisor can still commit, marks unknown only after an expired start lease or a definitively gone supervisor, and otherwise returns recoverable `running`. Terminal poll is a conditional one-shot claim. `cancel` persists a request only after the selected adapter verifies the neutral supervisor ref as the same incarnation; unknown identity is never cancellation authority. The POSIX adapter retains the direct child unreaped through TERM grace, KILLs the original group, proves live-member quiescence, and returns exact terminal truth for the supervisor to commit before cancellation can atomically consume/suppress the job. Async run validates `reminder` as a finite non-negative number bounded by `threading.TIMEOUT_MAX` and defaults omitted direct calls to 1800 seconds.

**Result fidelity — top-level `status` vs. inner command success.** The top-level `status` (`ok`/`done`) reflects only that the shell *spawned* the command; it stays `ok`/`done` even when the inner command exits nonzero. To make inner failures impossible to skim past *without* changing the `status` contract that downstream recovery/telemetry branch on (`tool_executor.py` enriches/logs/collects on `status == "error"`), `_augment_command_result` (`__init__.py`) adds three additive, model-visible fields keyed off `exit_code`:

- `ok` (bool) — `True` only when `exit_code == 0`.
- `command_status` (str) — `"success"` or `"failed"`.
- `warning` (str, present on failure or a suspicious zero-exit) — one-line summary: the nonzero exit code, any detected `python_traceback`/`missing_module` signature (`_detect_failure_signature`), and a bounded stderr tail. The hoisted tail is routed through `lingtai.kernel.trace_redaction.redact_text` (`_redact_warning_tail`, fail-open) so a secret-shaped error line is not made more prominent in the top-level `warning` than it already is in the raw `stderr` field; the raw `stderr`/`stdout` fields are never altered.

On a still-running poll there is no `exit_code`, so no fidelity fields are added.

**Timeout hint.** On a sync timeout whose command resembles a broad recursive scan (`find … -name/-path/-type`, `rglob(`, `os.walk(`, `glob('**…')` — `_broad_scan_hint`), the timeout `message` appends an `rg --files`-based recipe. The hint is advisory text only; it never blocks or rewrites the command.

Job files are stored under `system/jobs/{job_id}/`, where new IDs use the full UUID4 hex form `job-<32 hex>` and only old `job-<8 hex>` names remain accepted for legacy reads. `state.json` is the atomically replaced source of truth; it carries command/cwd, supervisor-start and successful-return handoff leases, neutral supervisor/command refs, retained v3 PID/identity compatibility fields, lifecycle timestamps, terminal result, cancellation request, and completion/reminder publication state. `stdout.log` and `stderr.log` remain supervisor-owned. Directories intentionally remain as durable records; retention/compaction is future policy, not implicit deletion. A conditional terminal claim changes `terminal_polled` and reminder suppression in one write, so concurrent managers have one terminal consumer. Reminder state is a tokenized `pending → publishing → published` (or bounded `suppressing` / terminal `suppressed`) claim; a due timer defers while `return_handoff` remains pending, and the final sink write shares the state lock with suppression. Stable reminder/completion refs correlate and can dedupe only while their bounded/current sinks retain them; they do not make bounded system events or latest-only Bash completion delivery globally exactly-once. Both are separate from `.notification/cron.json` workflow reminders.

## Internal Module Layout

```
bash/__init__.py
  ├── _detect_failure_signature()    — labels python_traceback / missing_module in output
  ├── _broad_scan_hint()             — rg recipe hint for broad-recursive-walk timeouts
  ├── _augment_command_result()      — adds ok / command_status / warning to completed results
  │
  ├── BashPolicy                     — command execution policy
  │   ├── __init__(allow, deny)      — two modes: allowlist (if allow present) or denylist
  │   ├── from_file(path)            — loads policy from JSON file
  │   ├── yolo()                     — creates a policy that allows everything
  │   ├── describe()                 — human-readable summary of policy rules
  │   ├── is_allowed(command)        — checks command against policy
  │   ├── _check_single(cmd)         — checks a single command name
  │
  ├── ShellDialect / ShellInvocation — selected shell-language boundary and serializable invocation
  │
  ├── BashManager                    — execution manager
  │   ├── __init__(policy, working_dir, max_output, agent) — stores policy, config, notification locks
  │   ├── handle(args)               — dispatches to _handle_run / _handle_poll / _handle_cancel
  │   ├── _handle_run(args)          — validates + runs sync or async
  │   ├── _run_sync(command, cwd, timeout) — contained paths per platform: Job Object on Windows (`_run_sync_contained`, win32_job), own-process-group graceful-then-KILL on POSIX (`_run_sync_posix_grouped`), plain subprocess.run fallback; augments result + timeout hint
  │   ├── _run_async(command, cwd, reminder) — writes initial state, starts/reaps private supervisor, returns command PID
  │   ├── _rehydrate_async_jobs()    — resumes reminder/completion state at manager construction
  │   ├── _run_reminder_timer(...)   — deadline worker; claim is durable rather than timer-local
  │   ├── _publish_async_reminder(...) / _publish_completion_if_due(...) — stable-ref notifications
  │   ├── _handle_poll(args)         — reads exact durable outcome or explicit unknown exit
  │   └── _handle_cancel(args)       — verifies durable supervisor ownership, requests cancel, waits exact commit
  │
  ├── _async_supervisor.py           — private detached policy runner; claims leases and persists terminal truth
  │   ├── write_initial_state/update_state — fsync + replace under the selected Bash state-lock Port
  │   └── supervise(job_dir)          — selects the process Port, delegates spawn/wait, persists exact exit
  │
  └── setup(agent, policy_file, yolo) — resolves policy, registers bash tool
```

## Key Invariants

- **Two policy modes:** Allowlist mode (when `allow` key is present in policy) — only listed commands permitted, everything else blocked. Denylist mode (only `deny` key) — everything allowed except denied commands. The mode is implicit.
- **Pipe-aware POSIX command extraction:** `PosixBashDialect` parses `|`, `&&`, `||`, `;`, newlines, `$()`, backticks, and env-var prefixes to find every command name in a compound expression. Future dialects provide their own extractor.
- **Working directory sandbox:** `working_dir` is validated beneath the active task-local execution workspace when bound, otherwise beneath the agent workdir. Relative/default cwd uses that same canonical root; parent and symlink escapes fail. Persistent async job records remain under the agent workdir.
- **Result fidelity is additive, never status-changing:** A completed command always returns top-level `status: "ok"`/`"done"` regardless of `exit_code`. The pass/fail signal lives in additive `ok`/`command_status`/`warning` fields. `status: "error"` is reserved for the shell failing to run the command at all (empty/denied command, timeout, spawn failure) — this preserves the `tool_executor` contract that branches on `status == "error"` for error enrichment, lifecycle logging, and `collected_errors`.
- **Output truncation:** `max_output = 50_000` chars. Both stdout and stderr are truncated with a note showing total length.
- **Subprocess isolation:** Commands run in the agent's working directory with captured output and an in-process supervisor timeout. On Windows the child is contained in a kill-on-close Job Object (`_run_sync_contained`, win32_job); on POSIX every command is spawned with `start_new_session=True` so it owns a process group, and timeout/cancellation signals the whole group SIGTERM then SIGKILL after a short grace (`_run_sync_posix_grouped` / `PosixBashAsyncProcessAdapter.wait`). This never relies on an external `/usr/bin/timeout` binary (which macOS lacks).
- **Async supervisor:** A private detached policy runner must claim a tokenized finite start lease and recheck it under the selected state-lock Port before asking the selected process Port to spawn. It records neutral supervisor/command refs, retains v3 PID fields for compatibility, delegates owned handles and exact waits to the adapter, and atomically persists terminal truth. If it exits before terminal commit, its owning parent reaper or a later lease/definitive-ref proof makes the job explicitly unrecoverable.
- **Terminal truth, cancellation, and consumption:** A missing command ref is pending evidence while the recorded supervisor can still commit. Poll writes unrecoverable only after bounded start-lease or supervisor-loss proof. Unknown identity is never treated as same-process ownership and cannot authorize cancellation. After a durable cancel request, the POSIX adapter retains the direct shell unreaped through TERM grace, KILLs the original group, and reports success only after live non-zombie group quiescence. `terminal_polled` is a conditional atomic claim coupled to reminder suppression, so poll/cancel races have one consumer.
- **Async reminder lifecycle:** Deadline, return handoff, and publication state live in `state.json`, not only a timer. Initial state provides a crash fallback plus a bounded pending-return guard; due claims defer until the successful-return mutation atomically writes `returned_at + reminder` and arms that guard. The returning manager reports success only if this still-valid lock transition wins (or exact completed/failed terminal truth already won under the valid guard); after expiry it returns a pollable recovery error. On guard expiry, a live job may use the fallback while an expired launch/dead supervisor becomes unrecoverable. Cancellation's durable `suppressing` state is bounded and returns to `pending` after crash/timeout. Final reminder publication is serialized with terminal suppression; exact completion suppresses stale pending/publishing/suppressing watchdogs and Bash completion owns the wake. Stable refs offer bounded/current-sink deduplication only, so crashes after sink write can still duplicate after retention/eviction.
- **Job lifecycle:** Jobs remain durable records after terminal poll/confirmed cancellation; retention/compaction is future policy. New IDs are full UUID4 hex and collision-safe; old eight-hex IDs are legacy read compatibility. A live legacy PID remains conservatively running and uncancellable because its incarnation cannot be proved; after it dies, one poll returns explicit unknown exit status rather than fabricated `-1` failure.
- **Policy file location:** Default policy is `bash/bash_policy.json` (shipped with the kernel). Can be overridden via `policy_file` arg or bypassed with `yolo=True`.
- **macOS login-shell detection and PATH guarantee:** On Darwin the POSIX invocation is an explicit `[shell, "-lc", script]` argv (never `shell=True` string concatenation) through the user's login shell, resolved as `$SHELL` → `dscl . -read /Users/<user> UserShell` → `/bin/zsh` → `/bin/bash`. GUI-launched apps inherit launchd's minimal PATH, so the child env (`ShellInvocation.env`, serialized into durable async state) prepends `/opt/homebrew/bin` and `/usr/local/bin` and strips credential-shaped variables (`*_API_KEY`, auth/OAuth tokens, `*_SECRET*`, `*_PASSWORD`), keeping general tokens like `GITHUB_TOKEN` intact. Linux keeps the historical `shell=True` form byte-for-byte.

## Dependencies

- `lingtai.kernel.i18n` — `t()` for localized strings
- `lingtai.kernel.base_agent.BaseAgent` — agent type (TYPE_CHECKING only)
- `lingtai.kernel.base_agent.messaging._enqueue_system_notification` — canonical `.notification/system.json` multi-event append path when Bash is installed on an agent (`src/lingtai/kernel/base_agent/messaging.py:66-180`).
- `lingtai.kernel.notification_store.NotificationStore` — serialized compare/update ownership for direct-manager reminder appends and sink-idempotent Bash completion writes; injected through `agent._notification_store` (`__init__.py:937`, `:1012`).
- `lingtai.kernel.trace_redaction.redact_text` — mechanical secret redaction for the stderr tail hoisted into `warning` (imported lazily inside `_redact_warning_tail`, fail-open).

## Composition

- **Parent:** `src/lingtai/tools/` (tool package).
- **Siblings:** `daemon/`, `avatar/`, `mcp/`, `knowledge/` (private durable memory), `skills/` (skill catalog).
- **Manual:** `bash/manual/SKILL.md` — short operational router for agents covering first-call/result-fidelity rules, async/poll/reminder durability, scheduled / cron-driven work, wake-by-mailbox-drop, hygiene, and cleanup. It progressively discloses into four nested sub-skills under `manual/reference/`: `bash-async-jobs`, `bash-debugging-cleanup`, `bash-notification-reminders`, and `bash-scheduled-work`.
- **Kernel hooks:** `setup()` is called during capability initialization; `ShellManager.handle()` is registered as the canonical `shell` tool handler.
