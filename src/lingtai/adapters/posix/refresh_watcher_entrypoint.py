"""Ordinary importable/executable entrypoint for the detached refresh watcher.

``PosixRefreshWatcherAdapter.spawn_detached`` used to launch the current
interpreter with the ~480-line generated watcher-program source passed
directly on argv (``sys.executable -c <script>``). This module replaces that
transport: the adapter now launches
``sys.executable -m lingtai.adapters.posix.refresh_watcher_entrypoint
<encoded-request>``, where ``<encoded-request>`` is the compact deterministic
JSON payload produced by ``lingtai.kernel.refresh_watcher.encode_request``.
This module is the only thing on argv; it decodes the request, renders the
same Core-owned watcher-program text
(``lingtai.kernel.refresh_watcher.watcher_program.render_watcher_script``),
and executes it in a fresh namespace — reproducing the exact same runtime
behavior (ACK/lock deadlines, relaunch retry, stale-duplicate cleanup,
bounded redaction, terminal-failure artifact/notification) the previous
``-c``-embedded source did.

This module is process/transport mechanism, not technology-neutral Core
logic, so it lives beside ``PosixRefreshWatcherAdapter`` under
``adapters/posix`` rather than in the kernel: it is only ever invoked as a
subprocess entrypoint via ``python -m``, and it is the adapter package that
already owns the concrete interpreter invocation, stream detachment, and
session mechanics for this Port (see ``refresh_watcher.py``). It performs no
policy of its own — ``main`` is a thin decode -> render -> exec pipeline —
so the watcher's actual behavior remains entirely owned by
``watcher_program.render_watcher_script``.
"""
from __future__ import annotations

import sys

from lingtai.kernel.refresh_watcher import decode_request
from lingtai.kernel.refresh_watcher.watcher_program import (
    render_watcher_script,
    watcher_failure_to_raise,
)
from lingtai.adapters.posix.refresh_watcher_process import (
    PosixRefreshWatcherProcessAdapter,
)
from lingtai.adapters.posix.workdir_lease import PosixWorkdirLeaseAdapter


def main(argv: list[str]) -> int:
    """Decode the single encoded-request argument and run the watcher program.

    Expects exactly one argument: the ``encode_request`` payload. Fails
    loudly (propagates ``decode_request``'s ``ValueError``) on a malformed
    payload rather than silently doing nothing, so a transport defect is
    immediately visible instead of producing a watcher process that spawned
    but never actually supervised anything.
    """
    if len(argv) != 1:
        raise SystemExit(
            "usage: python -m lingtai.adapters.posix.refresh_watcher_entrypoint "
            "<encoded-request>"
        )
    request = decode_request(argv[0])
    try:
        script = render_watcher_script(request)
        exec(
            compile(script, "<refresh_watcher>", "exec"),
            {
                "__name__": "__main__",
                "PROCESS_MECHANISM": PosixRefreshWatcherProcessAdapter(),
                # The policy's lock phase probes the platform lease; a
                # lingering `.agent.lock` pathname is not a held lease.
                "WORKDIR_LEASE": PosixWorkdirLeaseAdapter(request.working_dir),
            },
        )
    except BaseException as exc:
        # Only failures the policy did not already record/settle are handled
        # here (render/compile/exec setup); decode failures raise above with
        # no trusted taken_path, so nothing is cleaned for them.
        failure = watcher_failure_to_raise(request, exc)
        if failure is exc:
            raise
        raise failure from exc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


__all__ = ["main"]
