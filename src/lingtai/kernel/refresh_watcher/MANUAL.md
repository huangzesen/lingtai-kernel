---
related_files:
  - src/lingtai/kernel/refresh_watcher/CONTRACT.md
  - src/lingtai/kernel/refresh_watcher/ANATOMY.md
  - src/lingtai/kernel/refresh_watcher/__init__.py
  - src/lingtai/kernel/refresh_watcher/watcher_program.py
  - src/lingtai/kernel/process_match.py
  - src/lingtai/adapters/posix/refresh_watcher.py
  - src/lingtai/adapters/posix/refresh_watcher_entrypoint.py
maintenance: |
  Keep this manual's what/how/why in sync with RefreshWatcherPort,
  RefreshWatcherRequest, encode_request/decode_request,
  watcher_program.render_watcher_script, the POSIX adapter, and its
  entrypoint module whenever their behavior changes. Update related_files
  when a cited file moves. This manual is linked from both
  refresh_watcher/CONTRACT.md and refresh_watcher/ANATOMY.md per root
  CONTRACT.md Design principle 4 — keep both edges present.
---
# Refresh Watcher Manual

## What

Refresh watcher is the capability that hands a running agent's relaunch off
to a detached process that outlives the current process. When
`_perform_refresh` (`src/lingtai/kernel/base_agent/lifecycle.py`) finishes
the `.refresh`/`.refresh.taken` filesystem handshake, it builds a
`RefreshWatcherRequest` — the handshake paths, the relaunch command, and a
JSON snapshot of the agent's identity fields — and hands it to
`agent._refresh_watcher.spawn_detached(request)`. No raw program source or
caller environment crosses the Port; the request carries only the data the
watcher program needs, and its identity-field snapshot is taken once at
construction so it cannot be changed by anything that happens to the source
data afterward.

## How

1. **Core builds the request.** `_perform_refresh` never builds program text
   or an environment itself; it constructs a `RefreshWatcherRequest`
   (`src/lingtai/kernel/refresh_watcher/__init__.py`) from handshake paths it
   already computed and calls `spawn_detached(request)` exactly once, after
   the ACK invariant is established and before setting `_cancel_event`/`_shutdown`.
   The agent's runtime-identity fields (`agent._runtime_identity_event_fields`)
   are serialized to `identity_fields_json` with `json.dumps(...)` at this
   same call site — not carried as a live dict or a shallow container —
   because the identity payload nests a mutable `kernel_runtime` sub-dict (the
   same object as the process-wide identity cache) that would otherwise stay
   aliased and mutable even inside an "immutable" request.
2. **Core renders the program text.** `watcher_program.render_watcher_script(request)`
   (`src/lingtai/kernel/refresh_watcher/watcher_program.py`) first decodes
   `request.identity_fields_json` back to a `dict` via `_decode_identity_fields`
   — raising `ValueError` before generating any source if the snapshot is
   invalid JSON or not a JSON object — then renders complete Python program
   source independent of the parent process's live objects: the ACK/lock handshake poll, the
   12-attempt relaunch loop whose health check polls `.agent.heartbeat` every
   0.5s for up to 60s (long enough for an agent still spawning its MCP stdio
   servers to write a first heartbeat, and short-circuited as soon as that
   attempt's own stderr shows the duplicate guard), stale same-agent
   duplicate-process cleanup (SIGTERM → 5s grace → SIGKILL → up to 15s waiting
   for the duplicate to stop matching the same-agent guard before the next
   attempt), redacted event logging, and the terminal failure
   artifact/notification. Every wait is bounded, so the loop still ends in at
   most 12 attempts. This module
   performs no OS calls itself. Executing the rendered source requires an
   importable LingTai package for the kernel's redaction helper and canonical
   Core process-command matcher. The duplicate-process guard imports
   `from lingtai.kernel.process_match import match_agent_run` at runtime
   instead of embedding a second watcher copy of that policy — the same function
   the CLI's own `_check_duplicate_process` uses.
3. **The POSIX adapter encodes the request and launches the entrypoint.**
   `PosixRefreshWatcherAdapter.spawn_detached(request)`
   (`src/lingtai/adapters/posix/refresh_watcher.py`) calls
   `refresh_watcher.encode_request(request)` for the compact deterministic
   JSON wire payload, builds the launched process's environment via
   `build_watcher_env` (captures `os.environ`, applies the `env_overwrite`
   request field as `LINGTAI_REFRESH_ENV_OVERWRITE=1`), and launches
   `[sys.executable, "-m", "lingtai.adapters.posix.refresh_watcher_entrypoint",
   payload]` via `subprocess.Popen` with all three streams to `DEVNULL` and
   `start_new_session=True`. Unlike the previous transport, the ~480-line
   generated program text is never placed on argv — only the small encoded
   request is.
4. **The entrypoint module decodes, renders, and runs.**
   `refresh_watcher_entrypoint.main(argv)`
   (`src/lingtai/adapters/posix/refresh_watcher_entrypoint.py`) — the
   process's actual `__main__` when launched via `-m` — decodes the single
   argument via `refresh_watcher.decode_request`, calls
   `render_watcher_script(request)` to produce the exact same program text
   the previous transport embedded directly, and `exec`s it in a fresh
   namespace. `main` is itself ordinary importable/executable code (directly
   callable in tests, independent of a real subprocess), but it performs no
   watcher policy of its own — it is a thin decode→render→exec pipeline, so
   the watcher's actual runtime behavior remains entirely owned by
   `render_watcher_script`. The namespace also carries `WORKDIR_LEASE`, the
   platform workdir-lease adapter: the lock phase waits for the *lease* to be
   free (a `.agent.lock` pathname left by a dead holder is probed, not
   trusted), treats only a heartbeat that advances past the lock-release
   baseline as a live owner or child, clears `.refresh.taken` at every
   terminal outcome, and exits nonzero on every failure (see the Contract's
   rule 12).
5. **The watcher program runs independently of the parent's live objects.**
   Once running it gets every request-derived value from literals embedded by
   `render_watcher_script` and outlives the parent process. Its runtime imports
   still require the LingTai package that supplied the entrypoint, redaction
   helper, and canonical process-command matcher.

## Why

The Port used to accept a raw `script: str` and a full `env: Mapping[str,
str]` — Core built ~365 lines of Python source as an inline string literal
buried inside `_perform_refresh`'s control flow, indistinguishable from
ordinary lifecycle logic and impossible to read, diff, or call as a unit
without scrolling past unrelated handshake code. Splitting the request
(typed data), the renderer (`watcher_program.py`, Core-owned, pure,
*directly callable* to produce the program text), and the adapter (POSIX
process launch mechanism) follows the Contract's Core/Port/Adapter boundary:
Core owns *what data the watcher needs* and *what text describes its
policy*, never *how a process gets started* or *what the full OS environment
looks like*.

This is a text-generation extraction, not a code-shape change to the
watcher's own policy: `render_watcher_script(request)` is directly callable
and its *output text* can be asserted on (as the existing pinned-substring
and AST-structural tests do), but the watcher's actual runtime behavior —
the ACK/lock poll, the relaunch retry loop, stale-duplicate cleanup — is
still only exercisable by running the rendered text as a real detached
subprocess; it did not become ordinary in-process importable/unit-testable
module code, and this slice does not claim otherwise. `render_watcher_script`
remaining a pure string-render function (rather than a templating engine or
code-generation framework) is deliberate — the smallest change that makes
the program *text* directly producible and inspectable without redesigning
the watcher's retry/heartbeat/duplicate-cleanup policy, or introducing a
process-supervision Port, both of which stay explicit non-goals for a later
slice.

A later slice replaced only the *transport* that carries a request across
that process boundary: the adapter used to place the entire ~480-line
rendered program text directly on argv (`sys.executable -c <script>`), which
is unusual as an OS-level invocation (an opaque blob on the command line,
awkward to observe via `ps`, and not what "the interpreter runs a program"
ordinarily looks like) even though the *rendering* was already a normal
function call. Now the adapter launches an ordinary owned module via
`-m` (`lingtai.adapters.posix.refresh_watcher_entrypoint`) with a small
compact JSON-encoded request as its one argument; the entrypoint's `main`
is itself ordinary importable/executable code directly callable in tests.
This is still not a redesign of the watcher's policy or a claim that the
ACK/lock/retry/cleanup behavior became independently unit-testable — `main`
still renders the same generated text via `render_watcher_script` and
`exec`s it, so the watcher's actual behavior is exercised exactly the same
way as before (running rendered text as a real subprocess). It is a
transport-shape improvement — replacing "raw generated source on argv" with
"an ordinary module invocation carrying compact typed data" — not a Process/
Clock/Filesystem/Publication Port abstraction, a Windows adapter, or a
broader process-supervision redesign, all of which remain explicit non-goals
for a later slice.

A later slice replaced the rendered program's embedded, duplicated
`match_agent_run` definition with an import of the canonical Core policy
(`lingtai.kernel.process_match.match_agent_run`) that the CLI's own
duplicate-process check already used. Before this, the CLI and watcher runtime
consumers carried two hand-synchronized copies of the launch-form matching
rule set — the CLI's and one baked
into every rendered watcher program — kept in parity only by a test that
string-sliced the generated source back into an executable function. Import
removes only the watcher duplicate: the CLI and watcher now share one
importable Core implementation. The standalone `lingtai-doctor` bundle
intentionally retains its stdlib-only copy, kept in parity by
`tests/test_process_match.py`. This changes neither the matching policy nor the
watcher's own retry/heartbeat/duplicate-cleanup behavior or anything else this
Port renders.
