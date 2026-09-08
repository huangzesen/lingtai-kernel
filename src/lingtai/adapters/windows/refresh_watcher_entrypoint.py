"""Ordinary importable/executable entrypoint for the detached Windows watcher.

``WindowsRefreshWatcherAdapter.spawn_detached`` launches
``sys.executable -m lingtai.adapters.windows.refresh_watcher_entrypoint
<encoded-request>``, where ``<encoded-request>`` is the compact deterministic
JSON payload produced by ``lingtai.kernel.refresh_watcher.encode_request``.
This module mirrors the POSIX entrypoint exactly — a thin decode → render →
exec pipeline with no policy of its own — and differs in exactly one way: it
composes the *Windows* process mechanism,
``WindowsRefreshWatcherProcessAdapter``, bound to the request's working
directory so the graceful-stop ``.suspend`` channel addresses the supervised
agent. The watcher's actual behavior remains entirely owned by the Core
``watcher_program.render_watcher_script`` text, which is platform-neutral.

This module is process/transport mechanism, so it lives beside
``WindowsRefreshWatcherAdapter`` under ``adapters/windows``; it is the only
composition site that injects the Windows process mechanism into the generated
policy.
"""
from __future__ import annotations

import sys

from lingtai.kernel.refresh_watcher import decode_request
from lingtai.kernel.refresh_watcher.watcher_program import (
    render_watcher_script,
    watcher_failure_to_raise,
)
from lingtai.adapters.windows.refresh_watcher_process import (
    WindowsRefreshWatcherProcessAdapter,
)
from lingtai.adapters.windows.workdir_lease import WindowsWorkdirLeaseAdapter


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
            "usage: python -m lingtai.adapters.windows.refresh_watcher_entrypoint "
            "<encoded-request>"
        )
    request = decode_request(argv[0])
    try:
        script = render_watcher_script(request)
        exec(
            compile(script, "<refresh_watcher>", "exec"),
            {
                "__name__": "__main__",
                "PROCESS_MECHANISM": WindowsRefreshWatcherProcessAdapter(
                    request.working_dir
                ),
                # The policy's lock phase probes the platform lease; a
                # lingering `.agent.lock` pathname is not a held lease.
                "WORKDIR_LEASE": WindowsWorkdirLeaseAdapter(request.working_dir),
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
