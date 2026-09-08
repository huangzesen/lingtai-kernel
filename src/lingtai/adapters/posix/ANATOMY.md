---
related_files:
  - src/lingtai/ANATOMY.md
  - src/lingtai/kernel/event_journal/ANATOMY.md
  - src/lingtai/kernel/event_journal/CONTRACT.md
  - src/lingtai/kernel/mail_transport/ANATOMY.md
  - src/lingtai/kernel/refresh_watcher/ANATOMY.md
  - src/lingtai/kernel/services/ANATOMY.md
  - src/lingtai/services/ANATOMY.md
  - src/lingtai/adapters/posix/__init__.py
  - src/lingtai/adapters/posix/event_journal.py
  - src/lingtai/adapters/posix/git_cli.py
  - src/lingtai/adapters/posix/mail.py
  - src/lingtai/adapters/posix/workdir_lease.py
  - src/lingtai/adapters/posix/refresh_watcher.py
  - src/lingtai/adapters/posix/refresh_watcher_process.py
  - src/lingtai/adapters/posix/refresh_watcher_entrypoint.py
  - src/lingtai/adapters/posix/process_scan.py
  - src/lingtai/adapters/posix/bash.py
  - src/lingtai/adapters/posix/bash_process.py
  - src/lingtai/adapters/posix/bash_state_lock.py
  - src/lingtai/adapters/posix/interactive_terminal.py
  - src/lingtai/tools/daemon/interactive_terminal/CONTRACT.md
  - src/lingtai/tools/daemon/interactive_terminal/ANATOMY.md
  - src/lingtai/adapters/posix/avatar_launcher.py
  - src/lingtai/adapters/posix/daemon_capsule.py
  - src/lingtai/adapters/posix/daemon_manager.py
  - src/lingtai/adapters/posix/daemon_manager_entrypoint.py
  - src/lingtai/tools/avatar/ANATOMY.md
  - src/lingtai/tools/avatar/CONTRACT.md
  - src/lingtai/tools/bash/ANATOMY.md
  - src/lingtai/adapters/posix/notification_store.py
  - src/lingtai/adapters/posix/agent_presence.py
  - src/lingtai/adapters/posix/migration_workspace.py
  - src/lingtai/kernel/agent_presence/ANATOMY.md
  - src/lingtai/kernel/workdir_lease/ANATOMY.md
  - src/lingtai/kernel/workdir_lease/CONTRACT.md
  - src/lingtai/kernel/snapshot/ANATOMY.md
  - src/lingtai/kernel/snapshot/CONTRACT.md
  - src/lingtai/kernel/migrate/ANATOMY.md
  - src/lingtai/kernel/migrate/CONTRACT.md
  - src/lingtai/kernel/services/logging.py
  - ENVIRONMENT_VARIABLES.md
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
# POSIX Adapter Anatomy

This narrow package contains production filesystem and process adapters for
Core-owned Ports: the structured event journal, mail transport, notification
store, workdir lease, refresh watcher, agent presence, the fixed-command
snapshot/source-revision Git capability, and the migration workspace. It also
houses the capability-owned POSIX avatar launcher adapter. It is an
implementation-only Anatomy with no independent local Contract; for the
Anatomy/Contract pairing rule its unique owning Core component Contract is
`src/lingtai/kernel/event_journal/CONTRACT.md` (this Anatomy is listed in that
Contract's `related_files`). It is also linked bidirectionally with the
capability-local `src/lingtai/tools/daemon/interactive_terminal/CONTRACT.md`
(see this Anatomy's own `related_files` above), which owns the
`PosixInteractiveTerminalAdapter` promise. Each Core adapter implements its owning Port
rather than defining a separate behavioral promise; the mail adapter's promises
are owned by `src/lingtai/kernel/mail_transport/CONTRACT.md`, the workdir-lease
adapter's by `src/lingtai/kernel/workdir_lease/CONTRACT.md`, the refresh-watcher
adapter's by `src/lingtai/kernel/refresh_watcher/CONTRACT.md`, and the
notification-store adapter's by
`src/lingtai/kernel/notification_store/CONTRACT.md`, each of which links its
adapter code file directly. The avatar launcher promise is owned by
`src/lingtai/tools/avatar/CONTRACT.md`; Port structure is navigated through the
co-located owning ANATOMY.md files.

## Components

- `PosixInteractiveTerminalAdapter` implements the daemon-local
  `InteractiveTerminalPort` for the hidden interactive Claude route. It owns
  only `pty.openpty`, 120x40 terminal sizing, slave stdio, raw master byte
  reads/writes, `start_new_session`, bounded process-group TERM/KILL, reaping,
  and terminal-only release (`src/lingtai/adapters/posix/interactive_terminal.py`).

- `PosixJsonlEventJournalAdapter` constructs the existing JSONL primary and
  SQLite sidecar primitives under `<working_dir>/logs/`
  (`src/lingtai/adapters/posix/event_journal.py:15-36`).
- `append()` delegates the ordered/redacted durable write and translates storage
  metadata into `JournalPosition`
  (`src/lingtai/adapters/posix/event_journal.py:38-45`).
- `close()` delegates resource release to the composed logging service
  (`src/lingtai/adapters/posix/event_journal.py:47-48`).
- `PosixFilesystemMailAdapter` implements `MailTransportPort` by delivering
  messages as files into a recipient's inbox and polling its own inbox plus
  subscribed pseudo-agent outboxes. Its adapter-only
  `pseudo_agent_subscriptions` property returns the effective paths resolved
  once at construction for Email's fully redacted SHOW provider; it does not
  widen the Core mail transport Port (`src/lingtai/adapters/posix/mail.py`).
- `send()` handshakes, injects mailbox metadata, validates every attachment
  path up front, stages the full message (attachments + `message.json`) in a
  hidden `.<id>.staging` dir under the inbox, and publishes it with a single
  atomic `os.replace` — the recipient never observes a partial entry and any
  failure removes the sender-owned staging dir
  (`send()`, `src/lingtai/adapters/posix/mail.py:102`; publish at :199);
  `listen()`/`stop()` own the 0.5-second daemon poll loop with pseudo-outbox
  priority, dot-prefixed staging skips, and per-phase `OSError` isolation; the
  own-inbox dedupe set `_seen` is pruned to ids whose inbox directories still
  exist after each complete inbox pass, so archived/deleted messages drop out
  instead of accumulating forever.
- `PosixWorkdirLeaseAdapter` implements `WorkdirLeasePort` by holding an exclusive
  non-blocking `fcntl.flock` on `<workdir>/.agent.lock`
  (`src/lingtai/adapters/posix/workdir_lease.py:27-95`); `acquire()` polls at
  250 ms to a monotonic deadline and raises the exact contention `RuntimeError`,
  `release()` unlocks then guarantees the handle is closed in a `finally` (even if
  the explicit `LOCK_UN` raises) before a best-effort unlink, swallows the
  specified `OSError`s, resets its handle, and is idempotent.
- `PosixRefreshWatcherAdapter` implements `RefreshWatcherPort` by encoding a
  `RefreshWatcherRequest` to its compact deterministic JSON wire form
  (`refresh_watcher.encode_request`), building the process environment via its
  own `build_watcher_env` (`src/lingtai/adapters/posix/refresh_watcher.py:41-64`:
  `os.environ` capture plus `LINGTAI_REFRESH_ENV_OVERWRITE=1` when
  `request.env_overwrite`), and launching
  `[sys.executable, "-m", ENTRYPOINT_MODULE, payload]` via `subprocess.Popen`
  with all three standard streams set to `DEVNULL` and
  `start_new_session=True` (`src/lingtai/adapters/posix/refresh_watcher.py:80-90`);
  the call returns once the process has been started and does not wait for or
  track it.
- `PosixRefreshWatcherProcessAdapter` implements the watcher-local
  `RefreshWatcherProcessPort`: it owns `ps` command-line observation, liveness,
  replacement launch, graceful stop, and forced stop
  (`src/lingtai/adapters/posix/refresh_watcher_process.py:26-87`).
- `PosixAgentProcessScanAdapter` implements the CLI duplicate-launch guard's
  `AgentProcessScanPort` with one bounded `ps -eo pid=,command=` invocation,
  yielding `(pid, command_line)` and yielding nothing when `ps` is unavailable
  (`src/lingtai/adapters/posix/process_scan.py`).
- `PosixBashAsyncProcessAdapter` implements the Bash-local async process Port:
  detached supervisor launch, `ShellInvocation` command spawn (including the
  UTF-8 stdin delivery of `stdin_script` payloads), neutral
  identity observation, exact owned waits, and bounded process-tree cancellation
  (`src/lingtai/adapters/posix/bash_process.py:111-185`).
- `PosixBashStateLockAdapter` implements the Bash-local state-lock Port with an
  exclusive per-job lock file (`src/lingtai/adapters/posix/bash_state_lock.py:9-18`).
- `daemon_manager.py` owns the resident central manager's queue and one-shot
  Unix capsule socket. When an agent path exceeds the Unix socket-path limit,
  `_capsule_socket_path` selects the fixed
  `/tmp/lingtai-dm-<uid>-<digest>/capsule.sock` fallback, while
  `_prepare_capsule_socket_path` permits bind/stale-socket reuse only inside a
  real owner-owned mode-0700 directory and refuses symlink, ownership, type, or
  private-mode mismatches before unlink or bind
  (`src/lingtai/adapters/posix/daemon_manager.py:42-116`). `_ensure_manager`
  holds one exclusive `fcntl.flock` on `daemon/manager/manager.lock` around
  `_ensure_manager_locked`'s complete observe/identity/reserve/spawn sequence so
  concurrent submitters cannot both act on the same pre-lock state; each later
  caller re-checks the persisted reservation while stale-start recovery remains
  unchanged
  (`src/lingtai/adapters/posix/daemon_manager.py:197-273`).
- `refresh_watcher_entrypoint.main(argv)` is the owned ordinary
  importable/executable module the launched process runs
  (`src/lingtai/adapters/posix/refresh_watcher_entrypoint.py`). It decodes the
  single encoded-request argument via `refresh_watcher.decode_request`,
  renders the Core-owned watcher program text via
  `watcher_program.render_watcher_script(request)`, and `exec`s it in a fresh
  namespace — this is the only place the previously argv-embedded
  generated program text is materialized, replacing the earlier
  `sys.executable -c <script>` transport; the namespace injects
  `PosixRefreshWatcherProcessAdapter` as `PROCESS_MECHANISM` and
  `PosixWorkdirLeaseAdapter(request.working_dir)` as `WORKDIR_LEASE`, and an
  unhandled failure goes through `watcher_failure_to_raise`. `main` performs no watcher policy
  itself and is directly callable in tests independent of a real subprocess.
- `PosixGitCliAdapter` implements both `SnapshotPort` and `SourceRevisionPort`
  through fixed Git command families. Separate composed instances target the
  agent workdir and running source; no arbitrary argv/process/result object is
  exposed.
- `PosixNotificationStoreAdapter` implements all eight `NotificationStorePort`
  persistence families on `.notification/<channel>.json`, including typed
  compare-update and atomic acknowledgement/hook-manifest mutation. It also
  exposes its composed `NotificationMutationLockPort` for Core's private delay
  transaction (`src/lingtai/adapters/posix/notification_store.py`). Its internal
  lock spans each complete mutation; atomic writes use the shared `_fsutil`
  primitive.
- `PosixAgentPresenceStoreAdapter` implements all four `AgentPresenceStorePort`
  operations on one working directory's `.agent.json` / `.agent.heartbeat`
  (`src/lingtai/adapters/posix/agent_presence.py`): tri-state manifest/heartbeat
  observation, byte-exact `str(wall_seconds)`-no-newline heartbeat publication,
  and best-effort idempotent withdrawal. Bound to a `WorkdirLayout` at
  construction; holds no long-lived handle or lock.
- `PosixMigrationWorkspaceAdapter`
  (`src/lingtai/adapters/posix/migration_workspace.py`) implements all seven
  `MigrationWorkspacePort` families for one bound `MigrationDomain`/root: availability,
  entry→path mapping, raw reads, preset enumeration, PID-suffixed atomic replace
  (every replacement, incl. preset m001/m002), version files, `system/migrations/`
  archive + SHA-256 evidence, and best-effort `logs/events.jsonl` audit.
- `PosixAvatarLauncherAdapter` implements the avatar-local launcher Port with
  inherited cwd/environment, disconnected stdio, binary-write stderr,
  `start_new_session`, exact `poll()` truth, one-process TERM/KILL, and
  non-killing release. For a Driver-approved avatar only, it consumes the
  opaque one-shot AF_UNIX child endpoint, passes exactly that descriptor with
  `close_fds=True`, then closes its parent copy
  (`src/lingtai/adapters/posix/avatar_launcher.py`).
- `daemon_capsule.py` owns the bounded, one-shot daemon capsule wire. On POSIX
  it can attach exactly one live descriptor with `SCM_RIGHTS`; the receiving
  `ReceivedDaemonCapsule` owns that duplicate until it is transferred once or
  closed. The central manager, detached supervisor, and execution child share
  this mechanism without placing descriptor contents in argv, environment
  values, or durable state.

## Connections

The event-journal adapter depends inward on `EventJournalPort` and
`JournalPosition`, and on the existing logging primitives for byte serialization,
redaction, primary-first ordering, and SQLite fail-open behavior
(`src/lingtai/adapters/posix/event_journal.py:7-12`). The mail adapter depends
inward on `MailTransportPort`, Core `agent_presence` liveness policy,
`handshake.resolve_address`, and the kernel-owned `_new_mailbox_id`
(`src/lingtai/adapters/posix/mail.py:27-33`). The workdir-lease adapter depends
inward on the kernel-owned `workdir_layout` for the `.agent.lock` path and on
`WorkdirLeasePort` (`src/lingtai/adapters/posix/workdir_lease.py:23-24`). The
notification-store adapter depends inward on `NotificationStorePort` and the
kernel `_fsutil.atomic_write_json` helper
(`src/lingtai/adapters/posix/notification_store.py:13-25`). The migration-workspace
adapter depends inward on `MigrationWorkspacePort` and the migrate value objects and
reuses the Core `meta_filename()` name. It is imported by
explicit composition modules, not exported from the package facade. Agent, CLI,
and Telegram-server roots construct these adapters; the CLI `load_init`, the
wrapper `load_preset` / `_run_preset_library_migrations`, and `Agent._read_init`
build a domain/root-bound `PosixMigrationWorkspaceAdapter` for the Core runners.
The refresh-watcher selector in `src/lingtai/adapters/refresh_watcher.py` is an
outer composition module and imports this package only after confirming POSIX;
Core never imports this package. The refresh entrypoint composes
`PosixRefreshWatcherProcessAdapter` and passes it into the generated Core policy.
The daemon manager and supervisor adapters use `daemon_capsule.py` only as a
process-mechanism boundary: the manager holds any received descriptor in memory,
then transfers it manager → supervisor → execution child. Each sending API
adopts its input immediately and closes its copy after transfer or failure;
discarded/replaced/cancelled capsules close their received copy before removal.

## Composition

- **Parent wrapper:** `src/lingtai/ANATOMY.md`.
- **Port components:** `src/lingtai/kernel/event_journal/ANATOMY.md`,
  `src/lingtai/kernel/mail_transport/ANATOMY.md`,
  `src/lingtai/kernel/workdir_lease/ANATOMY.md`,
  `src/lingtai/kernel/snapshot/ANATOMY.md`,
  `src/lingtai/kernel/notification_store/ANATOMY.md`,
  `src/lingtai/kernel/agent_presence/ANATOMY.md`, and
  `src/lingtai/kernel/migrate/ANATOMY.md`.
- **Storage primitives:** `src/lingtai/kernel/services/ANATOMY.md`.

## State

The event-journal adapter owns the open primary handle and derived-index
lifecycle through its composite
(`src/lingtai/adapters/posix/event_journal.py:24-36`); it writes
`logs/events.jsonl` and the rebuildable `logs/log.sqlite` sidecar. The mail
adapter owns the daemon poll thread and the in-memory `_seen` set, and writes
`mailbox/{inbox,outbox,sent}/<id>/message.json` plus `attachments/`
(`src/lingtai/adapters/posix/mail.py:67-69`). The workdir-lease adapter owns the
open `.agent.lock` file handle while the lease is held; release resets adapter
state, attempts unlock and close, and unlinks only after closure is confirmed so
an uncertain live descriptor cannot create split-inode authority
(`src/lingtai/adapters/posix/workdir_lease.py:38-96`). The notification-store
adapter owns the internal `threading.Lock` (set in `__init__`,
`src/lingtai/adapters/posix/notification_store.py:68-76`) and the workdir path,
and writes `.notification/<channel>.json` plus
`.notification/large_result_acks.json` via `load_ack_refs`/`update_ack_refs`
(`src/lingtai/adapters/posix/notification_store.py:238`, `:248-268`).
The migration-workspace adapter owns only its bound `(domain, root)` pair and
writes the domain's `_kernel_meta.json` version file, `system/migrations/` archive
artifacts, and best-effort `logs/events.jsonl` audit through PID-suffixed temp + replace; it holds no long-lived handle or lock.
The daemon capsule transport owns no durable state. Its optional descriptor is
process-local, paired with one in-memory capsule, and invalidated by manager
process death; a manager restart can recover only the durable queue record and
therefore follows the existing missing-capsule failure path.

## Notes

These are the only production adapters for their respective Ports. The package
contains no adapter registry, default factory, query surface, rebuild policy, or
network sink. Notification channel and acknowledgement transaction locks are
Store-owned; no concrete notification persistence remains in Core. The mail
adapter is a faithful move of the former
`kernel/services/mail.py` mechanism; no concrete mail transport remains in Core.
The workdir-lease adapter is a faithful move of the former
`WorkingDir.acquire_lock`/`release_lock` flock mechanism; no concrete lock
authority remains in Core, and platform selection with fail-loud unsupported
handling lives in `src/lingtai/adapters/workdir_lease.py`.

### macOS shell decisions (bash/shell capability)

macOS has no cgroups, no Job Objects, and no `/usr/bin/timeout`; the only
reliable tree primitive is the process group. The POSIX shell path therefore:

- **Spawns every command in its own process group** (`start_new_session=True`)
  and cancels/times out by signaling the group with SIGTERM then SIGKILL
  after a short grace period (`os.killpg` in `bash_process.py` and the sync
  `_run_sync_posix_grouped` path) — the Hermes `os.killpg` reaping pattern.
  `subprocess.run`'s timeout kills only the direct child and leaks
  grandchildren, so timeouts are enforced by this supervisor in-process, never
  by shelling out to `timeout`.
- **Detects the user's login shell on macOS** (`$SHELL` → `dscl . -read
  /Users/<user> UserShell` → `/bin/zsh` → `/bin/bash`) and spawns
  `[shell, "-lc", script]` (never `shell=True` string concatenation) so
  `.zprofile`/`.zshrc` state is restored for GUI-launched apps — the Codex
  `shell_detect`/`derive_exec_args` pattern.
- **Guarantees the Homebrew PATH for GUI-launched sessions**: the child env
  (`ShellInvocation.env`, serialized into durable async state) prepends
  `/opt/homebrew/bin` and `/usr/local/bin` and strips credential-shaped
  variables (provider API keys, auth/OAuth tokens, secrets, passwords) so a
  desktop-launched agent never leaks credentials into commands — the Claude
  Code `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` model.

Linux keeps the historical `shell=True` spawn form byte-for-byte; the Darwin
branch is gated on `platform.system() == "Darwin"` at invocation build time.
