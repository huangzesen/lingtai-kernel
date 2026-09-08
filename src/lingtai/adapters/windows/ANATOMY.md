---
related_files:
  - src/lingtai/ANATOMY.md
  - src/lingtai/kernel/workdir_lease/ANATOMY.md
  - src/lingtai/kernel/workdir_lease/CONTRACT.md
  - src/lingtai/kernel/refresh_watcher/ANATOMY.md
  - src/lingtai/adapters/windows/__init__.py
  - src/lingtai/adapters/windows/_win32.py
  - src/lingtai/adapters/windows/workdir_lease.py
  - src/lingtai/adapters/windows/refresh_watcher.py
  - src/lingtai/adapters/windows/refresh_watcher_process.py
  - src/lingtai/adapters/windows/refresh_watcher_entrypoint.py
  - src/lingtai/adapters/windows/process_scan.py
  - src/lingtai/adapters/windows/avatar_launcher.py
  - src/lingtai/adapters/windows/daemon_supervisor.py
  - src/lingtai/adapters/windows/daemon_supervisor_entrypoint.py
  - src/lingtai/adapters/windows/daemon_execution_child_entrypoint.py
  - src/lingtai/adapters/windows/daemon_resume_owner_entrypoint.py
  - src/lingtai/adapters/windows/process_identity.py
  - src/lingtai/kernel/daemon_supervisor/CONTRACT.md
  - src/lingtai/adapters/windows/powershell.py
  - src/lingtai/adapters/windows/powershell_process.py
  - src/lingtai/adapters/windows/win32_job.py
  - src/lingtai/adapters/windows/powershell_state_lock.py
  - src/lingtai/adapters/windows/gitbash.py
  - src/lingtai/adapters/windows/wsl.py
  - src/lingtai/tools/bash/ANATOMY.md
  - src/lingtai/tools/bash/CONTRACT.md
  - src/lingtai/tools/avatar/ANATOMY.md
  - src/lingtai/tools/avatar/CONTRACT.md
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/adapters/windows/cmd.py
  - src/lingtai/adapters/windows/windows_cmd_shim.py
maintenance: |
  Keep related_files repo-relative, duplicate-free, and linked to real files.
  Keep parent/child anatomy links bidirectional. Code is the structural source of
  truth: update this anatomy in the same change that moves files, symbols,
  connections, composition, or state. Verify every changed citation and run the
  architecture-document validation before merge.
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# Windows Adapter Anatomy

This narrow package contains the production native-Windows adapters for
Core-owned and capability-owned Ports: the workdir lease, the refresh-watcher
outer/process/entrypoint trio, the duplicate-launch process scan, the avatar
launcher, the detached daemon supervisor (with its three entrypoint mirrors
and the process-incarnation identity), and the shell capability's PowerShell
dialect, Job Object process adapter, and state lock. It is an implementation-only Anatomy with no
independent local Contract; for the Anatomy/Contract pairing rule its unique
owning Core component Contract is
`src/lingtai/kernel/workdir_lease/CONTRACT.md` (this Anatomy is listed only in
that Contract's `related_files`). Each adapter implements its owning Port
rather than defining a separate behavioral promise: shell promises are owned
by `src/lingtai/tools/bash/CONTRACT.md`, avatar promises by
`src/lingtai/tools/avatar/CONTRACT.md`, refresh-watcher promises by
`src/lingtai/kernel/refresh_watcher/CONTRACT.md`, and lease/scan promises by
`src/lingtai/kernel/workdir_lease/CONTRACT.md` /
`src/lingtai/kernel/base_agent/CONTRACT.md`. Every module here imports its
Windows mechanism (`msvcrt`, Win32 `ctypes` surfaces) lazily or guards it
behind an `os.name` check so the package stays importable on every platform;
only method execution requires Windows.

## Components

- `_win32` — shared low-level ctypes surface: `process_alive`
  (OpenProcess/GetExitCodeProcess — never `os.kill`, which terminates on
  Windows), `process_creation_identity` (`windows:<creation_filetime>`),
  `terminate_pid` (exact-PID `TerminateProcess`), `taskkill_tree`
  (identity-gated `taskkill /PID <pid> /T /F` tree-kill fallback), and the
  `DETACHED_CREATIONFLAGS` spawn constant
  (`src/lingtai/adapters/windows/_win32.py`).
- `WindowsWorkdirLeaseAdapter` — exclusive workdir lease via `msvcrt.locking`
  byte 0/length 1 on `<workdir>/.agent.lock`, the frozen TUI-probe interop
  range (`src/lingtai/adapters/windows/workdir_lease.py`).
- `WindowsRefreshWatcherAdapter` — detached `-m` watcher handoff with the
  shared creation flags (`src/lingtai/adapters/windows/refresh_watcher.py`);
  `refresh_watcher_entrypoint.main` decodes/renders the Core policy and
  injects the workdir-bound process mechanism and the workdir-bound
  `WindowsWorkdirLeaseAdapter` (`WORKDIR_LEASE`) for the lock-phase lease probe
  (`src/lingtai/adapters/windows/refresh_watcher_entrypoint.py`).
- `WindowsRefreshWatcherProcessAdapter` — watcher-local process mechanism:
  CIM `Win32_Process` command-line observation, handle-based liveness,
  detached replacement launch, `.suspend` graceful-stop channel, exact-PID
  forced stop (`src/lingtai/adapters/windows/refresh_watcher_process.py`).
- `WindowsAgentProcessScanAdapter` — one bounded CIM query yielding
  `(pid, command_line)` for the CLI duplicate-launch guard
  (`src/lingtai/adapters/windows/process_scan.py`).
- `WindowsAvatarLauncherAdapter` — avatar spawn with the shared creation
  flags; `terminate`/`force_terminate` are both documented-forceful
  `TerminateProcess`. The Driver avatar child-endpoint handoff is POSIX-only;
  Windows closes and rejects such a lease rather than launching a child
  without it (`src/lingtai/adapters/windows/avatar_launcher.py`).
- `WindowsDaemonSupervisorAdapter` — detached daemon supervisor spawn with the
  inherited-handle one-shot capsule wire (`handle_list` +
  `LINGTAI_DAEMON_CAPSULE_HANDLE`), plus execution-child and resume-owner
  spawns (`src/lingtai/adapters/windows/daemon_supervisor.py`); the three
  entrypoint mirrors adopt the capsule handle to the shared fd wire and
  delegate to the mechanism-free POSIX read/dispatch logic
  (`daemon_supervisor_entrypoint.py`, `daemon_execution_child_entrypoint.py`,
  `daemon_resume_owner_entrypoint.py`).
- `CmdDialect` — the `cmd.exe` shell-language dialect, reachable only through
  the ShellKind classifier (`LINGTAI_SHELL=cmd`, an `init.json` `shell_kind`
  override, or the last-resort fallback when neither `pwsh` nor Git Bash is
  discoverable). Policy extraction normalizes caret escapes, quotes, `,`/`;`/`=`
  delimiters, and `(...)` blocks so the deny-list cannot be bypassed with
  `d^el`, `"del"`, or `if ... (del ...)`; any `%`-expansion fails closed with an
  `__cmd_unsupported__` marker `ShellManager` refuses under a configured policy.
  Over-splitting only ever denies more (`src/lingtai/adapters/windows/cmd.py`).
- `windows_cmd_shim` — trusted `.cmd`/`.bat` shim handling for tools pwsh
  invokes implicitly. `npm`/`npx` resolve to a direct `node <dir>/bin/npm-cli.js`
  call that bypasses the shim entirely; any other PATH-resolved `.cmd`/`.bat`
  first token is wrapped in `cmd.exe /d /s /c` with a single command string, and
  metacharacters unsafe under cmd.exe (`%`, backtick, `^`, `$var`) are rejected
  rather than silently reinterpreted
  (`src/lingtai/adapters/windows/windows_cmd_shim.py`).
- `process_identity` — Windows process-incarnation token
  (`windows:<creation_filetime>`) reached by delegation from
  `adapters/posix/process_identity.py`
  (`src/lingtai/adapters/windows/process_identity.py`).
- `PowerShellDialect` — PowerShell 7 command extraction, invocation shaping,
  and `state_key() == "powershell"` provenance
  (`src/lingtai/adapters/windows/powershell.py`).  Invocations keep the
  command line ASCII-only: `pwsh` runs a fixed bootstrap
  (`_ASCII_BOOTSTRAP`) that forces UTF-8 console encodings, reads the real
  command from stdin, and executes it as a ScriptBlock, so user source never
  crosses the code-page-mangling Windows command line nor its 32,768-character
  limit.  The existing exit-code wrapper travels unchanged as the stdin
  payload (`ShellInvocation.stdin_script`).
- `WindowsShellAsyncProcessAdapter` — Job Object process-tree ownership:
  suspended spawn, job assignment, `NtResumeProcess`, active-process
  accounting, bounded tree cancellation, creation-time process identity.  Its
  Job Object is strict kill-on-close with **no** breakaway escape hatch: no
  descendant can leave the job, so ActiveProcesses accounting stays the exact
  ownership/quiescence source of truth; when the Job kill fails or a
  descendant escapes the job, an identity-gated `taskkill /T /F` fallback
  re-checks the creation-time identity before killing; the fallback sweep and
  root reap are bounded so a fail-closed (identity-mismatch) sweep still
  converges to a terminal commit
  (`src/lingtai/adapters/windows/powershell_process.py`).
- `win32_job` — raw ctypes Job Object primitives shared by the shell
  adapters: kill-on-close + breakaway-ok job creation (Codex parity for the
  sync path — a contained child may opt out with
  `CREATE_BREAKAWAY_FROM_JOB` when it must manage its own job), suspended
  contained spawn (`spawn_into_job`), job terminate with `taskkill /T /F`
  fallback, active-process accounting, and the bounded post-kill pipe drain
  (`drain_pipes`, Codex `io_drain_timeout`)
  (`src/lingtai/adapters/windows/win32_job.py`).
- `WindowsShellStateLockAdapter` — cross-process shell job state lock via
  `msvcrt.locking` byte 0/length 1 on `<job_dir>/.state.lock`
  (`src/lingtai/adapters/windows/powershell_state_lock.py`).

## Connections

The shell adapters are selected by the outer `os.name == "nt"` branches of
`src/lingtai/adapters/shell.py`, `shell_process.py`, and
`shell_state_lock.py`. The workdir lease and refresh watcher are selected by
the `sys.platform == "win32"` branches of
`src/lingtai/adapters/workdir_lease.py` and
`src/lingtai/adapters/refresh_watcher.py`; the process scan by
`src/lingtai/adapters/process_scan.py`; the avatar launcher by the
`os.name == "nt"` branch of `src/lingtai/adapters/avatar_launcher.py`. The
Windows refresh adapter reuses the platform-neutral `build_watcher_env` from
the POSIX sibling as the single source of the env-overwrite policy
translation. Core never imports this package; composition roots reach it only
through those selectors.

## Composition

- **Parent wrapper:** `src/lingtai/ANATOMY.md`.
- **Owning contract:** `src/lingtai/kernel/workdir_lease/CONTRACT.md` (pairing
  owner); shell, avatar, refresh, and runtime promises live in their linked
  capability contracts.
- **POSIX sibling:** `src/lingtai/adapters/posix/ANATOMY.md` maps the POSIX
  implementations of the same Ports.

## State

The state lock and workdir lease own open byte-range-locked file handles while
held (`.state.lock`, `.agent.lock`). The async shell process adapter owns Job
Object handles and per-job `stdout.log`/`stderr.log` streams until release.
The refresh process adapter writes the supervised workdir's `.suspend` file as
its graceful-stop channel and appends replacement stderr to the requested log
path. The avatar launcher owns the child's stderr file handle only during
spawn. No adapter here persists state beyond those capability-owned files.

## Notes

**Console encoding:** the PowerShell wrapper prepends
`[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [System.Text.UTF8Encoding]::new($false)`
so native commands (ipconfig, chcp-sensitive tools) write UTF-8 to the pipe.
Async `stdout.log`/`stderr.log` bytes are decoded at the bash read-back
boundary with `decode_windows_output` (strict UTF-8 first; the OEM codepage
guesses cp437/cp850/cp1252 are applied *per line* so a mostly-UTF-8 log with
one invalid byte run keeps its valid text) instead of `errors="replace"`,
which silently corrupts OEM-encoded output.  The read-back boundary also
restores the universal-newlines translation (CRLF/CR -> LF) the previous
`read_text` provided, so async output never carries stray `\r` characters.

The workdir-lease byte range (`.agent.lock`, byte 0, length 1) is a
cross-repository interop invariant with the TUI duplicate-launch probe; the
normative statement lives in `src/lingtai/kernel/workdir_lease/CONTRACT.md`.
The shell `.state.lock` uses the same mechanism but is a different,
capability-local lock — never evidence for the agent lease. Graceful process
stop on Windows is capability-defined (the refresh adapter's `.suspend`
channel); there is no deliverable SIGTERM, and `Popen.terminate()` is
forceful — adapters document that mapping instead of pretending a graceful
tier exists.

**Git Bash / MSYS pitfalls.** Git Bash (Git for Windows) is a POSIX-grammar
fallback dialect on Windows; `src/lingtai/adapters/windows/gitbash.py`
implements it and `wsl.py` is the separate, opt-in WSL adapter. Spawning Git
Bash correctly is full of traps — record them here so future PRs do not
rediscover them:

- `MSYS_NO_PATHCONV=1` + `MSYS2_ARG_CONV_EXCL=*` — MSYS2 auto-converts
  POSIX-looking arguments (paths, flags like `/p`) into Windows form before
  the child sees them, mangling native argv; set both environment variables
  to disable the conversion for the spawned process.
- `/c/...` path translation — Git Bash mounts Windows drives as
  `/c/Users/...` instead of `C:\Users\...`; every path crossing the Git Bash
  ↔ native-Windows boundary must be translated (`/c/...` ↔ `C:\...`)
  explicitly, in argv and in the environment.
- `usr\bin` PATH prepend — Git for Windows keeps its own coreutils under
  `C:\Program Files\Git\usr\bin`; prepend that directory (ahead of
  `System32`) so the child resolves the MSYS coreutils rather than the
  differently-behaving Windows built-ins (`find`, `sort`, ...).
- Non-login `bash -c` coreutils gap — a non-interactive `bash -c` never runs
  the profile that sets up the MSYS environment, so coreutils/PATH are
  incomplete; use the login form `bash -lc` (as `gitbash.py` does) or set the
  environment explicitly.
- ASLR spawn-failure class (0xc0000142 / 0xc0000005) — Git Bash/Cygwin
  children can fail at spawn with `STATUS_DLL_INIT_FAILED`
  (0xc0000142) or an access violation (0xc0000005) under loader/ASLR
  interference (AV, DEP, DLL-base randomization); the failure is transient,
  so spawns in this class warrant a bounded retry rather than an immediate
  hard error.
- WSL-bash-vs-Git-bash ambiguity — `%SystemRoot%\System32\bash.exe` (and the
  SysWOW64 twin) is the WSL launcher, not Git Bash; on WSL-enabled hosts
  `bash` on PATH silently resolves to WSL bash, which is Linux and behaves
  differently. Never assume which bash is on PATH: `discover_git_bash()`
  rejects the System32/SysWOW64 launcher and WSL stays opt-in (`wsl.py`).
