"""Kernel runtime/update nudges.

This check is deliberately read-only. It surfaces the observed running and
installed versions, plus the latest release-manifest facts published on the
official GitHub/Gitee mirrors, through the shared ``nudge`` notification
channel. It never mutates an installation.

Cadence note: the remote probe runs on a daemon worker thread and is consumed
on a later probe, so the nudge surfaces up to one probe (~60s) after the
fetch completes. This is intentional: the check runs on the 1s heartbeat
thread, which must never block on the network (#730).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..release_manifest import ManifestError, manifest_from_dict
from .prompts import (
    INSTALL_ROUTE,
    NudgeFacts,
    NudgeSituation,
    render_nudge_payload,
)

_FAST_INTERVAL_SECONDS = 60.0
_REMOTE_TIMEOUT_SECONDS = 3.0
# Hard ceiling for one background probe. Longer than the worst-case sequential
# mirror fetch (4 bounded requests x 3.0s); a worker still running after this
# (e.g. a stalled DNS lookup) is abandoned so the slot never pins forever.
_FETCH_DEADLINE_SECONDS = 30.0
_MAX_REMOTE_BYTES = 256 * 1024
_MANIFEST_ASSET_NAME = "lingtai-kernel-release-manifest.json"
_GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/Lingtai-AI/lingtai-kernel/releases/latest"
)
_GITEE_LATEST_RELEASE_URL = (
    "https://gitee.com/api/v5/repos/huangzesen1997/lingtai-kernel/releases/latest"
)
_GITHUB_SOURCE = "github-release-manifest"
_GITEE_SOURCE = "gitee-release-manifest"
_STATE_FILE = Path(".notification") / ".nudge_state.json"
_KIND = "kernel_version"
# Backward-compatible module constant for callers that inspected the old hint.
_SKILL_HINT = INSTALL_ROUTE


@dataclass(frozen=True)
class _RuntimeInfo:
    running_version: str
    installed_version: str
    dev_reason: str | None = None

    @property
    def dev_mode(self) -> bool:
        return self.dev_reason is not None


@dataclass(frozen=True)
class _ReleaseManifest:
    source: str
    kernel_version: str
    manifest_sha256: str
    artifact_hashes_sha256: str


@dataclass(frozen=True)
class _ReleaseObservation:
    version: str
    source: str
    manifest_sha256: str | None = None
    artifact_hashes_sha256: str | None = None


class _MirrorMismatchError(RuntimeError):
    def __init__(self, manifests: Mapping[str, _ReleaseManifest]):
        self.manifests = dict(manifests)
        versions = ", ".join(
            f"{source}={manifest.kernel_version}"
            for source, manifest in sorted(self.manifests.items())
        )
        super().__init__(f"release-manifest mirrors disagree: {versions}")


@dataclass
class _PendingFetch:
    """Process-local slot for one in-flight remote probe.

    ``result``/``error`` are written only by the daemon worker thread and read
    by the heartbeat thread after ``done`` is set; ``done`` is the publication
    barrier. The worker never touches ``.nudge_state.json`` or the
    notification store, preserving the heartbeat thread's single-writer
    property over persistent state.
    """

    thread: threading.Thread | None
    started_ts: float
    result: Any = None
    error: BaseException | None = None
    done: threading.Event = field(default_factory=threading.Event)


def _fetch_slot(agent) -> _PendingFetch | None:
    """Return the in-flight fetch slot for ``agent``, if any."""
    return getattr(agent, "_nudge_kernel_fetch", None)


def _start_fetch(agent) -> None:
    """Start the remote probe on a daemon worker and return immediately.

    The heartbeat tick must never block on the network (#730): a slow or
    stalled fetch would hold the 1s tick past the regression's 2s heartbeat-cadence
    responsiveness bound and make a live agent look dead. That test/cadence budget is
    independent of the configurable ``is_alive`` liveness policy. Single-flight is guaranteed because a
    slot exists from spawn until consume/abandon, and ``check`` is gated by
    the 60s fast interval.
    """
    pending = _PendingFetch(thread=None, started_ts=time.time())

    def _worker() -> None:
        try:
            pending.result = _fetch_latest_version()
        except Exception as exc:  # keep the slot consumable on any failure
            pending.error = exc
        finally:
            pending.done.set()

    try:
        worker = threading.Thread(
            target=_worker,
            daemon=True,
            name="nudge-kernel-version-fetch",
        )
    except Exception:
        # Cannot even spawn the worker; stay inert and let the next probe
        # re-arm the remote check.
        return
    pending.thread = worker
    agent._nudge_kernel_fetch = pending
    worker.start()


def check(agent) -> None:
    """Emit or clear the kernel-version nudge for ``agent``."""

    state = _state(agent)
    now = time.time()
    if now - float(state.get("last_probe_ts") or 0.0) < _FAST_INTERVAL_SECONDS:
        return
    state["last_probe_ts"] = now

    try:
        from . import remove, upsert

        info = _runtime_info()
    except Exception as e:  # pragma: no cover - defensive: nudge must be inert
        _log(agent, "kernel_version_probe_error", error=str(e)[:200])
        return

    if info.dev_mode:
        remove(agent, _KIND)
        _store_kernel_state(
            agent,
            {
                "last_skip_date": _today_utc(),
                "skip_reason": info.dev_reason,
                "checked_installed_version": info.installed_version,
                "last_error": None,
            },
        )
        return

    comparison = _compare_versions(info.running_version, info.installed_version)
    if comparison is None or comparison != 0:
        # Nudge policy owns dismissal/repeat semantics globally. This producer
        # reports only the current runtime fact; it does not add a per-kind day,
        # fingerprint, or process cadence. Only an installed version that is
        # semantically newer may recommend a safe refresh.
        today = _today_utc()
        situation = (
            NudgeSituation.INSTALLED_RUNTIME_MISMATCH
            if comparison is not None and comparison < 0
            else NudgeSituation.RUNTIME_MISMATCH_DIAGNOSTIC
        )
        upsert(
            agent,
            _KIND,
            render_nudge_payload(
                situation,
                NudgeFacts(
                    running=info.running_version,
                    installed=info.installed_version,
                    checked_at_date=today,
                ),
            ),
        )
        _log(
            agent,
            "nudge_emitted",
            kind=_KIND,
            running=info.running_version,
            installed=info.installed_version,
            source=(
                "installed-distribution"
                if situation is NudgeSituation.INSTALLED_RUNTIME_MISMATCH
                else "installed-distribution-diagnostic"
            ),
        )
        return

    # A local refresh finding is resolved by the matching pair itself. Clear
    # only that local source before probing remote mirrors; an independently
    # established remote-update finding remains visible during an outage.
    _clear_resolved_local_refresh_nudge(agent)
    persistent = _load_persistent_state(agent)
    kernel_state = persistent.setdefault(_KIND, {})
    today = _today_utc()
    # The 60-second probe is only a bounded observation cost. Product repeat
    # behavior belongs to the shared global Nudge policy, not this producer.

    # The remote probe must never run on the heartbeat thread (#730): a slow
    # or stalled fetch would hold the 1s tick past the regression's 2s heartbeat-cadence
    # responsiveness bound and make a live agent look dead. That test/cadence budget is
    # independent of the configurable is_alive liveness policy. The fetch runs on a daemon worker;
    # each tick either starts one or consumes a finished one.
    pending = _fetch_slot(agent)
    if pending is None:
        if not _remote_check_due(kernel_state, info.installed_version, today):
            return
        _start_fetch(agent)
        return
    if not pending.done.is_set():
        if time.time() - pending.started_ts > _FETCH_DEADLINE_SECONDS:
            # Abandon a wedged worker (e.g. a stalled DNS lookup); the
            # orphaned daemon thread dies on its own. Record the failure so
            # the day is not retried in a tight loop.
            kernel_state.update(
                {
                    "last_remote_check_date": today,
                    "checked_installed_version": info.installed_version,
                    "last_error": "fetch timed out",
                }
            )
            _save_persistent_state(agent, persistent)
            _log(agent, "kernel_version_update_check_error", error="fetch timed out")
            agent._nudge_kernel_fetch = None
        return
    agent._nudge_kernel_fetch = None
    if pending.error is not None:
        error = pending.error
        if isinstance(error, _MirrorMismatchError):
            mismatch = _mirror_mismatch_payload(error.manifests)
            kernel_state.update(
                {
                    "last_remote_check_date": today,
                    "checked_installed_version": info.installed_version,
                    "latest_seen": None,
                    "latest_source": None,
                    "mirror_mismatch": mismatch,
                    "last_error": None,
                }
            )
            _save_persistent_state(agent, persistent)
            upsert(
                agent,
                _KIND,
                render_nudge_payload(
                    NudgeSituation.MIRROR_MISMATCH,
                    NudgeFacts(
                        running=info.running_version,
                        installed=info.installed_version,
                        checked_at_date=today,
                        mirror_mismatch=mismatch,
                    ),
                ),
            )
            _log(
                agent,
                "kernel_version_mirror_mismatch",
                kind=_KIND,
                sources=sorted(error.manifests),
            )
            return
        kernel_state.update(
            {
                "last_remote_check_date": today,
                "checked_installed_version": info.installed_version,
                "last_error": str(error)[:200],
            }
        )
        _save_persistent_state(agent, persistent)
        _log(agent, "kernel_version_update_check_error", error=str(error)[:200])
        return

    try:
        observation = _coerce_observation(pending.result)
    except Exception as e:
        kernel_state.update(
            {
                "last_remote_check_date": today,
                "checked_installed_version": info.installed_version,
                "last_error": str(e)[:200],
            }
        )
        _save_persistent_state(agent, persistent)
        _log(agent, "kernel_version_update_check_error", error=str(e)[:200])
        return

    kernel_state.update(
        {
            "last_remote_check_date": today,
            "checked_installed_version": info.installed_version,
            "latest_seen": observation.version,
            "latest_source": observation.source,
            "latest_manifest_sha256": observation.manifest_sha256,
            "latest_artifact_hashes_sha256": observation.artifact_hashes_sha256,
            "mirror_mismatch": None,
            "last_error": None,
        }
    )

    if _is_newer(observation.version, info.installed_version):
        kernel_state["emitted_for_latest"] = observation.version
        _save_persistent_state(agent, persistent)
        upsert(
            agent,
            _KIND,
            render_nudge_payload(
                NudgeSituation.PACKAGE_UPDATE_AVAILABLE,
                NudgeFacts(
                    running=info.running_version,
                    installed=info.installed_version,
                    latest=observation.version,
                    checked_at_date=today,
                    source=observation.source,
                ),
            ),
        )
        _log(
            agent,
            "nudge_emitted",
            kind=_KIND,
            installed=info.installed_version,
            latest=observation.version,
            source=observation.source,
        )
        return

    kernel_state["emitted_for_latest"] = None
    _save_persistent_state(agent, persistent)
    remove(agent, _KIND)


def _runtime_info() -> _RuntimeInfo:
    from importlib import metadata

    # The kernel must not import the lingtai wrapper. When the wrapper is
    # already loaded (the normal Agent path), read its in-memory __version__
    # and __file__ so we can distinguish the *running* code from the
    # *installed* distribution metadata on disk. This detects in-place upgrades
    # that happened after the current process started.
    import sys

    wrapper = sys.modules.get("lingtai")
    try:
        dist = metadata.distribution("lingtai")
        installed = str(dist.version)
    except metadata.PackageNotFoundError:
        return _RuntimeInfo(
            running_version=getattr(wrapper, "__version__", "unknown"),
            installed_version="unknown",
            dev_reason="no-installed-distribution",
        )

    if wrapper is not None:
        running = str(getattr(wrapper, "__version__", installed))
        module_file = str(getattr(wrapper, "__file__", ""))
    else:
        # Wrapper not loaded: fall back to distribution metadata. In this path
        # running and installed are identical, so only dev/editable detection
        # can produce a nudge; no runtime-upgrade nudge is possible.
        running = installed
        module_file = str(dist.locate_file("lingtai/__init__.py"))

    return _RuntimeInfo(
        running_version=running,
        installed_version=installed,
        dev_reason=_dev_install_reason(dist, module_file, running, installed),
    )


def _dev_install_reason(dist: Any, module_file: str, running: str, installed: str) -> str | None:
    if _direct_url_is_editable(dist):
        return "editable-install"
    if _looks_like_dev_version(running) or _looks_like_dev_version(installed):
        return "dev-version"
    if _module_from_source_checkout(module_file):
        return "source-checkout"
    return None


def _direct_url_is_editable(dist: Any) -> bool:
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    return bool(data.get("dir_info", {}).get("editable"))


def _looks_like_dev_version(version: str) -> bool:
    v = (version or "").lower()
    return ".dev" in v or "+" in v or "editable" in v


def _module_from_source_checkout(module_file: str) -> bool:
    if not module_file:
        return False
    try:
        path = Path(module_file).resolve()
    except Exception:
        return False
    if any(part in {"site-packages", "dist-packages"} for part in path.parts):
        return False
    return any((parent / ".git").exists() and (parent / "pyproject.toml").exists() for parent in path.parents)


def _remote_check_due(kernel_state: dict[str, Any], installed_version: str, today: str) -> bool:
    """Return whether this installed version has not been checked on this UTC day.

    State is deliberately fail-open for malformed or missing values: a new
    process must still get one observation. A completed probe (including a
    failure) records both fields, so retries are suppressed until either the
    UTC day or installed version changes. Nudge dismissal/repeat remains a
    separate global policy concern.
    """
    return not (
        kernel_state.get("last_remote_check_date") == today
        and kernel_state.get("checked_installed_version") == installed_version
    )


def _fetch_latest_version() -> _ReleaseObservation:
    """Read the latest manifest asset from both official release mirrors.

    A missing or malformed mirror is unavailable; the other mirror may still
    establish the observation. When both are usable, their manifest bytes and
    declared artifact hashes must agree before either can be trusted.
    """
    manifests: dict[str, _ReleaseManifest] = {}
    errors: dict[str, str] = {}
    for source, fetch in (
        (_GITHUB_SOURCE, _fetch_github_manifest),
        (_GITEE_SOURCE, _fetch_gitee_manifest),
    ):
        try:
            manifests[source] = fetch()
        except Exception as exc:
            errors[source] = str(exc)[:200]

    if len(manifests) == 2:
        values = tuple(manifests.values())
        if not _manifests_agree(values[0], values[1]):
            raise _MirrorMismatchError(manifests)
    if not manifests:
        details = "; ".join(f"{source}: {error}" for source, error in sorted(errors.items()))
        raise RuntimeError(f"official release manifests unavailable: {details}")

    source, manifest = sorted(manifests.items())[0]
    return _ReleaseObservation(
        version=manifest.kernel_version,
        source=source,
        manifest_sha256=manifest.manifest_sha256,
        artifact_hashes_sha256=manifest.artifact_hashes_sha256,
    )


def _fetch_github_manifest() -> _ReleaseManifest:
    release = _fetch_json(
        _GITHUB_LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "lingtai-kernel-nudge/1",
        },
    )
    url = _github_manifest_asset_url(release, _GITHUB_SOURCE)
    return _fetch_manifest_asset(url, _GITHUB_SOURCE)


def _fetch_gitee_manifest() -> _ReleaseManifest:
    release = _fetch_json(
        _GITEE_LATEST_RELEASE_URL,
        headers={"User-Agent": "lingtai-kernel-nudge/1"},
    )
    url = _gitee_manifest_asset_url(release, _GITEE_SOURCE)
    return _fetch_manifest_asset(url, _GITEE_SOURCE)


def _fetch_json(url: str, headers: Mapping[str, str] | None = None) -> Any:
    raw = _fetch_bytes(url, headers=headers)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{url} did not return valid JSON") from exc


def _fetch_bytes(url: str, headers: Mapping[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    response = urllib.request.urlopen(request, timeout=_REMOTE_TIMEOUT_SECONDS)
    try:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if status != 200:
            raise RuntimeError(f"release source returned HTTP {status}")
        content_length = None
        response_headers = getattr(response, "headers", None)
        if response_headers is not None:
            try:
                content_length = int(response_headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                content_length = None
        if content_length and content_length > _MAX_REMOTE_BYTES:
            raise RuntimeError("release source response exceeded the bounded size limit")
        raw = response.read(_MAX_REMOTE_BYTES + 1)
    finally:
        response.close()
    if len(raw) > _MAX_REMOTE_BYTES:
        raise RuntimeError("release source response exceeded the bounded size limit")
    return raw


def _github_manifest_asset_url(release: Any, source: str) -> str:
    return _manifest_asset_url(
        release,
        source,
        list_key="assets",
        url_keys=("browser_download_url", "browserDownloadUrl"),
    )


def _gitee_manifest_asset_url(release: Any, source: str) -> str:
    return _manifest_asset_url(
        release,
        source,
        list_key="attach_files",
        url_keys=("browserDownloadUrl", "browser_download_url", "download_url", "url"),
    )


def _manifest_asset_url(
    release: Any,
    source: str,
    *,
    list_key: str | None = None,
    url_keys: tuple[str, ...] | None = None,
) -> str:
    if list_key is None or url_keys is None:
        if source == _GITHUB_SOURCE:
            list_key = "assets"
            url_keys = ("browser_download_url", "browserDownloadUrl")
        elif source == _GITEE_SOURCE:
            list_key = "attach_files"
            url_keys = ("browserDownloadUrl", "browser_download_url", "download_url", "url")
        else:
            raise RuntimeError(f"{source} has no release asset contract")
    if not isinstance(release, dict):
        raise RuntimeError(f"{source} latest release response was not an object")
    assets = release.get(list_key)
    if not isinstance(assets, list):
        raise RuntimeError(f"{source} latest release response has no {list_key!r} list")
    matches = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise RuntimeError(f"{source} latest release response has a malformed asset entry")
        if asset.get("name") == _MANIFEST_ASSET_NAME:
            matches.append(asset)
    if not matches:
        raise RuntimeError(f"{source} latest release has no {_MANIFEST_ASSET_NAME!r} asset")
    if len(matches) != 1:
        raise RuntimeError(f"{source} latest release has ambiguous duplicate {_MANIFEST_ASSET_NAME!r} assets")

    asset = matches[0]
    present_urls = [asset[key] for key in url_keys if key in asset]
    if not present_urls or any(not isinstance(url, str) or not url.startswith("https://") for url in present_urls):
        raise RuntimeError(f"{source} release manifest asset has no usable HTTPS URL")
    if len(set(present_urls)) != 1:
        raise RuntimeError(f"{source} release manifest asset has ambiguous download URLs")
    return present_urls[0]


def _fetch_manifest_asset(url: str, source: str) -> _ReleaseManifest:
    raw = _fetch_bytes(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "lingtai-kernel-nudge/1",
        },
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{source} release manifest was not valid JSON") from exc
    manifest = _validate_release_manifest(data, source)
    artifacts = [
        (artifact.filename, artifact.sha256)
        for artifact in manifest.artifacts
    ]
    artifact_bytes = json.dumps(
        sorted(artifacts), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _ReleaseManifest(
        source=source,
        kernel_version=manifest.kernel_version,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        artifact_hashes_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
    )


def _validate_release_manifest(data: Any, source: str):
    try:
        return manifest_from_dict(data)
    except ManifestError as exc:
        raise RuntimeError(f"{source} release manifest is invalid: {exc}") from exc


def _manifests_agree(left: _ReleaseManifest, right: _ReleaseManifest) -> bool:
    return (
        left.kernel_version == right.kernel_version
        and left.manifest_sha256 == right.manifest_sha256
        and left.artifact_hashes_sha256 == right.artifact_hashes_sha256
    )


def _coerce_observation(value: Any) -> _ReleaseObservation:
    """Keep the old string test seam usable while production returns provenance."""
    if isinstance(value, _ReleaseObservation):
        return value
    if isinstance(value, str):
        return _ReleaseObservation(version=value, source="release-manifest")
    if isinstance(value, Mapping) and isinstance(value.get("version"), str):
        return _ReleaseObservation(
            version=value["version"],
            source=str(value.get("source") or "release-manifest"),
            manifest_sha256=value.get("manifest_sha256"),
            artifact_hashes_sha256=value.get("artifact_hashes_sha256"),
        )
    raise RuntimeError("release manifest observation had no kernel_version")


def _mirror_mismatch_payload(manifests: Mapping[str, _ReleaseManifest]) -> dict[str, dict[str, str]]:
    return {
        source: {
            "version": manifest.kernel_version,
            "manifest_sha256": manifest.manifest_sha256,
            "artifact_hashes_sha256": manifest.artifact_hashes_sha256,
        }
        for source, manifest in sorted(manifests.items())
    }


def _parse_version(value: object):
    if not isinstance(value, str) or not value:
        return None
    try:
        from packaging.version import InvalidVersion, Version

        return Version(value)
    except (InvalidVersion, TypeError, ImportError):
        return None


def _compare_versions(running: object, installed: object) -> int | None:
    running_version = _parse_version(running)
    installed_version = _parse_version(installed)
    if running_version is None or installed_version is None:
        return None
    return (running_version > installed_version) - (running_version < installed_version)


def _is_newer(candidate: str, current: str) -> bool:
    candidate_version = _parse_version(candidate)
    current_version = _parse_version(current)
    return candidate_version is not None and current_version is not None and candidate_version > current_version


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _clear_resolved_local_refresh_nudge(agent) -> None:
    try:
        from . import _current_entries, remove

        if any(
            entry.get("kind") == _KIND
            and entry.get("source") in {"installed-distribution", "installed-distribution-diagnostic"}
            for entry in _current_entries(agent)
        ):
            remove(agent, _KIND)
    except Exception:
        # Clearing a stale transport mirror is best-effort; remote observation
        # remains fail-closed if a test double or store cannot be inspected.
        pass


def _persistent_path(agent) -> Path:
    return Path(agent._working_dir) / _STATE_FILE


def _load_persistent_state(agent) -> dict[str, Any]:
    path = _persistent_path(agent)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_persistent_state(agent, state: dict[str, Any]) -> None:
    path = _persistent_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _store_kernel_state(agent, fields: dict[str, Any]) -> None:
    persistent = _load_persistent_state(agent)
    kernel_state = persistent.setdefault(_KIND, {})
    if all(kernel_state.get(k) == v for k, v in fields.items()):
        return
    kernel_state.update(fields)
    _save_persistent_state(agent, persistent)


def _state(agent) -> dict:
    state = getattr(agent, "_nudge_kernel_version_state", None)
    if not isinstance(state, dict):
        state = {}
        setattr(agent, "_nudge_kernel_version_state", state)
    return state


def _log(agent, event: str, **fields: Any) -> None:
    try:
        agent._log(event, **fields)
    except Exception:
        pass
