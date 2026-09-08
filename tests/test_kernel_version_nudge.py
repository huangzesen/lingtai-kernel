import json

import pytest

from tests._notification_store_helpers import notification_store_for, snapshot_notifications
from lingtai.kernel.nudge import ENTRY_CHANNEL_RELEASE_VERSION, upsert
from lingtai.kernel.nudge import kernel_version as kv


class _Agent:
    def __init__(self, workdir):
        self._working_dir = workdir
        self._notification_store = notification_store_for(workdir)
        self.logs = []

    def _log(self, event, **fields):
        self.logs.append((event, fields))


def _entries(workdir):
    return (
        snapshot_notifications(workdir)
        .get("nudge", {})
        .get("data", {})
        .get("nudges", [])
    )


def _reset_fast_gate(agent):
    agent._nudge_kernel_version_state["last_probe_ts"] = 0.0


def _probe_and_consume(agent):
    """Start the async fetch, wait for the worker, then consume its result
    with a second check() -- mirroring the old synchronous contract for tests
    that assert post-fetch state."""
    kv.check(agent)
    pending = kv._fetch_slot(agent)
    assert pending is not None, "expected the async fetch to start"
    pending.thread.join()
    _reset_fast_gate(agent)
    kv.check(agent)


def test_installed_runtime_refresh_nudge_does_not_hit_remote(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("remote should not be queried")),
    )

    kv.check(agent)

    entries = _entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "kernel_version"
    assert entry["nudge_channel"] == ENTRY_CHANNEL_RELEASE_VERSION
    assert entry["source"] == "installed-distribution"
    assert entry["running"] == "0.14.1"
    assert entry["installed"] == "0.14.2"
    assert entry["suggested_action"] == "refresh-installed-runtime-if-authorized-and-safe"
    assert "skill" not in entry
    assert "install_url" not in entry
    assert "already on disk" in entry["detail"]
    assert "system(action='refresh')" in entry["detail"]
    assert "skill.md" not in entry["detail"]


@pytest.mark.parametrize(
    ("running", "installed", "source", "action"),
    [
        ("0.16.5", "0.17.0", "installed-distribution", "refresh-installed-runtime-if-authorized-and-safe"),
        ("0.17.0", "0.16.5", "installed-distribution-diagnostic", "inspect-runtime-interpreter-and-import-paths"),
        ("0.17.0", "0.17.0", None, None),
        ("not-a-version", "0.17.0", "installed-distribution-diagnostic", "inspect-runtime-interpreter-and-import-paths"),
    ],
)
def test_runtime_version_direction_is_fail_safe_and_equal_pairs_probe_remote(
    tmp_path, monkeypatch, running, installed, source, action
):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(running_version=running, installed_version=installed, dev_reason=None),
    )
    calls = []

    def latest():
        calls.append(True)
        return installed

    if source is None:
        monkeypatch.setattr(kv, "_fetch_latest_version", latest)
    else:
        monkeypatch.setattr(
            kv,
            "_fetch_latest_version",
            lambda: (_ for _ in ()).throw(AssertionError("remote should not be queried")),
        )
    if source is None:
        kv.check(agent)
        pending = kv._fetch_slot(agent)
        assert pending is not None
        pending.thread.join()
        assert calls == [True]
        assert _entries(tmp_path) == []
    else:
        kv.check(agent)
        entry = _entries(tmp_path)[0]
        assert entry["source"] == source
        assert entry["suggested_action"] == action
        assert "Do not refresh" in entry["detail"] if source.endswith("diagnostic") else "already on disk" in entry["detail"]


def test_local_refresh_mismatch_does_not_re_emit_same_utc_day(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-30")

    kv.check(agent)
    assert len(_entries(tmp_path)) == 1

    # Agent dismisses the nudge (deletes nudge.json).
    from lingtai.kernel.nudge import remove

    remove(agent, "kernel_version")
    assert _entries(tmp_path) == []

    # A raw remove is only transport cleanup; without a policy dismissal the
    # unresolved finding is eligible to be observed again immediately.
    _reset_fast_gate(agent)
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1


def test_local_refresh_mismatch_re_emits_next_utc_day(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )

    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-30")
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1

    from lingtai.kernel.nudge import remove

    remove(agent, "kernel_version")
    assert _entries(tmp_path) == []

    # Next UTC day, same mismatch still present -> re-emit once for that day.
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-07-01")
    _reset_fast_gate(agent)
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1
    assert _entries(tmp_path)[0]["source"] == "installed-distribution"

    # Raw channel removal is not the Nudge dismissal action; the unresolved
    # finding remains eligible without a recorded policy mute.
    remove(agent, "kernel_version")
    _reset_fast_gate(agent)
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1


def test_local_refresh_match_clears_mismatch_tracking_and_stale_nudge(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-30")

    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1

    # Versions now match (a refresh happened). This must clear the stale
    # nudge and the mismatch tracking so a future mismatch emits again.
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.2",
            installed_version="0.14.2",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_fetch_latest_version", lambda: "0.14.2")
    _reset_fast_gate(agent)
    _probe_and_consume(agent)
    assert _entries(tmp_path) == []

    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert "emitted_for_mismatch" not in state["kernel_version"]
    assert "mismatch_emitted_date" not in state["kernel_version"]

    # A fresh mismatch on the same day must emit again.
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.2",
            installed_version="0.14.3",
            dev_reason=None,
        ),
    )
    _reset_fast_gate(agent)
    kv.check(agent)
    assert len(_entries(tmp_path)) == 1
    assert _entries(tmp_path)[0]["installed"] == "0.14.3"


def test_match_after_refresh_clears_local_nudge_during_remote_outage(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-07-17")
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo("0.17.0", "0.17.0", None),
    )
    upsert(
        agent,
        "kernel_version",
        {
            "title": "LingTai kernel refresh available: 0.16.5 -> 0.17.0",
            "source": "installed-distribution",
            "running": "0.16.5",
            "installed": "0.17.0",
        },
    )
    assert _entries(tmp_path)
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(RuntimeError("both official mirrors unavailable")),
    )

    _probe_and_consume(agent)

    assert _entries(tmp_path) == []
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert "unavailable" in state["kernel_version"]["last_error"]


def test_match_after_refresh_keeps_independent_remote_finding_during_outage(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(kv, "_runtime_info", lambda: kv._RuntimeInfo("0.17.0", "0.17.0", None))
    upsert(
        agent,
        "kernel_version",
        {
            "title": "LingTai kernel update available: 0.17.0 -> 0.18.0",
            "source": kv._GITHUB_SOURCE,
            "running": "0.17.0",
            "installed": "0.17.0",
            "latest": "0.18.0",
        },
    )
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(RuntimeError("both official mirrors unavailable")),
    )

    kv.check(agent)

    assert _entries(tmp_path)[0]["source"] == kv._GITHUB_SOURCE


def test_remote_update_check_is_gated_by_installed_version_and_utc_day(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.1",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-24")

    def latest():
        calls["n"] += 1
        return "0.14.2"

    monkeypatch.setattr(kv, "_fetch_latest_version", latest)

    _probe_and_consume(agent)

    entries = _entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "release-manifest"
    assert entry["nudge_channel"] == ENTRY_CHANNEL_RELEASE_VERSION
    assert entry["latest"] == "0.14.2"
    assert entry["suggested_action"] == "execute-installer-help-then-ask-human"
    assert entry["install_url"] == "https://lingtai.ai/install.sh"
    assert "human" in entry["detail"].lower()
    assert "skill.md" not in entry["detail"]
    assert calls["n"] == 1

    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["last_remote_check_date"] == "2026-06-24"
    assert state["kernel_version"]["checked_installed_version"] == "0.14.1"
    assert state["kernel_version"]["latest_seen"] == "0.14.2"

    # A later same-day probe for the same installed version is suppressed.
    _reset_fast_gate(agent)
    kv.check(agent)
    assert kv._fetch_slot(agent) is None
    assert calls["n"] == 1

    # An in-day installed-version change re-arms the remote observation.
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo("0.14.2", "0.14.2", None),
    )
    _reset_fast_gate(agent)
    kv.check(agent)
    pending = kv._fetch_slot(agent)
    assert pending is not None
    pending.thread.join()
    _reset_fast_gate(agent)
    kv.check(agent)  # consume the completed version-change observation
    assert calls["n"] == 2

    # A UTC-day transition also re-arms the check for the same version.
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-25")
    _reset_fast_gate(agent)
    kv.check(agent)
    pending = kv._fetch_slot(agent)
    assert pending is not None
    pending.thread.join()
    assert calls["n"] == 3


@pytest.mark.parametrize("candidate", ["999-not-a-release", "release-999", "not-a-version"])
def test_malformed_remote_version_cannot_be_promoted_by_numeric_substrings(tmp_path, monkeypatch, candidate):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(kv, "_runtime_info", lambda: kv._RuntimeInfo("0.16.5", "0.16.5", None))
    monkeypatch.setattr(kv, "_fetch_latest_version", lambda: candidate)

    kv.check(agent)

    assert _entries(tmp_path) == []
    assert kv._is_newer(candidate, "0.16.5") is False


def test_dev_or_editable_runtime_skips_and_clears_kernel_nudge(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    upsert(agent, "kernel_version", {"title": "old", "source": "release-manifest"})
    assert _entries(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1.dev0",
            installed_version="0.14.1.dev0",
            dev_reason="editable-install",
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-24")
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("dev mode should skip remote")),
    )

    kv.check(agent)

    assert "nudge" not in snapshot_notifications(tmp_path)
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["last_skip_date"] == "2026-06-24"
    assert state["kernel_version"]["skip_reason"] == "editable-install"


def test_current_remote_version_clears_existing_kernel_nudge(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    upsert(agent, "kernel_version", {"title": "old", "source": "release-manifest"})
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.1",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-06-24")
    monkeypatch.setattr(kv, "_fetch_latest_version", lambda: "0.14.1")

    _probe_and_consume(agent)

    assert "nudge" not in snapshot_notifications(tmp_path)
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["latest_seen"] == "0.14.1"
    assert state["kernel_version"]["emitted_for_latest"] is None


def test_runtime_info_uses_loaded_wrapper_version(tmp_path, monkeypatch):
    """_runtime_info reads running_version from sys.modules['lingtai'] without importing it."""
    import sys
    import types
    from importlib import metadata

    fake_wrapper = types.ModuleType("lingtai")
    fake_wrapper.__version__ = "0.14.1"
    fake_wrapper.__file__ = str(tmp_path / "site-packages" / "lingtai" / "__init__.py")
    monkeypatch.setitem(sys.modules, "lingtai", fake_wrapper)

    class FakeDist:
        version = "0.14.2"

        def read_text(self, name):
            return None

        def locate_file(self, path):
            return tmp_path / "site-packages" / path

    monkeypatch.setattr(
        metadata, "distribution", lambda name: FakeDist() if name == "lingtai" else None
    )

    info = kv._runtime_info()
    assert info.running_version == "0.14.1"
    assert info.installed_version == "0.14.2"
    assert info.dev_reason is None


def test_runtime_info_wrapper_absent_falls_back_to_metadata(tmp_path, monkeypatch):
    """When the wrapper module is not loaded, running_version falls back to installed metadata."""
    import sys
    from importlib import metadata

    monkeypatch.delitem(sys.modules, "lingtai", raising=False)

    class FakeDist:
        version = "0.14.2"

        def read_text(self, name):
            return None

        def locate_file(self, path):
            return tmp_path / "site-packages" / path

    monkeypatch.setattr(
        metadata, "distribution", lambda name: FakeDist() if name == "lingtai" else None
    )

    info = kv._runtime_info()
    assert info.running_version == "0.14.2"
    assert info.installed_version == "0.14.2"


def test_runtime_info_detects_source_checkout_from_wrapper_file(tmp_path, monkeypatch):
    """Source-checkout detection uses the already-loaded wrapper's __file__."""
    import sys
    import types
    from importlib import metadata

    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("")
    wrapper_file = checkout / "src" / "lingtai" / "__init__.py"
    wrapper_file.parent.mkdir(parents=True)
    wrapper_file.write_text("")

    fake_wrapper = types.ModuleType("lingtai")
    fake_wrapper.__version__ = "0.14.1"
    fake_wrapper.__file__ = str(wrapper_file)
    monkeypatch.setitem(sys.modules, "lingtai", fake_wrapper)

    class FakeDist:
        version = "0.14.1"

        def read_text(self, name):
            return None

        def locate_file(self, path):
            return tmp_path / "site-packages" / path

    monkeypatch.setattr(
        metadata, "distribution", lambda name: FakeDist() if name == "lingtai" else None
    )

    info = kv._runtime_info()
    assert info.dev_reason == "source-checkout"


def test_runtime_info_wrapper_without_file_is_not_source_checkout(tmp_path, monkeypatch):
    """A loaded wrapper lacking __file__ must not be misclassified as source-checkout."""
    import sys
    import types
    from importlib import metadata

    fake_wrapper = types.ModuleType("lingtai")
    fake_wrapper.__version__ = "0.14.1"
    # No __file__ attribute.
    monkeypatch.setitem(sys.modules, "lingtai", fake_wrapper)

    class FakeDist:
        version = "0.14.1"

        def read_text(self, name):
            return None

        def locate_file(self, path):
            return tmp_path / "site-packages" / path

    monkeypatch.setattr(
        metadata, "distribution", lambda name: FakeDist() if name == "lingtai" else None
    )

    info = kv._runtime_info()
    assert info.dev_reason is None


def test_runtime_info_loaded_wrapper_triggers_local_refresh_nudge(tmp_path, monkeypatch):
    """A loaded wrapper at X plus installed metadata at Y emits the local refresh nudge."""
    import sys
    import types
    from importlib import metadata

    fake_wrapper = types.ModuleType("lingtai")
    fake_wrapper.__version__ = "0.14.1"
    fake_wrapper.__file__ = str(tmp_path / "site-packages" / "lingtai" / "__init__.py")
    monkeypatch.setitem(sys.modules, "lingtai", fake_wrapper)

    class FakeDist:
        version = "0.14.2"

        def read_text(self, name):
            return None

        def locate_file(self, path):
            return tmp_path / "site-packages" / path

    monkeypatch.setattr(
        metadata, "distribution", lambda name: FakeDist() if name == "lingtai" else None
    )
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(AssertionError("remote should not be queried")),
    )

    agent = _Agent(tmp_path)
    kv.check(agent)

    entries = _entries(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "kernel_version"
    assert entry["source"] == "installed-distribution"
    assert entry["running"] == "0.14.1"
    assert entry["installed"] == "0.14.2"


def _release_manifest(source, version="0.14.2", manifest_sha256="a" * 64, artifact_hashes_sha256="b" * 64):
    return kv._ReleaseManifest(
        source=source,
        kernel_version=version,
        manifest_sha256=manifest_sha256,
        artifact_hashes_sha256=artifact_hashes_sha256,
    )


def test_release_manifest_mirrors_must_agree_before_version_selection(monkeypatch):
    monkeypatch.setattr(
        kv,
        "_fetch_github_manifest",
        lambda: _release_manifest(kv._GITHUB_SOURCE, version="0.14.3"),
    )
    monkeypatch.setattr(
        kv,
        "_fetch_gitee_manifest",
        lambda: _release_manifest(kv._GITEE_SOURCE, version="0.14.2"),
    )

    with pytest.raises(kv._MirrorMismatchError) as exc_info:
        kv._fetch_latest_version()

    assert set(exc_info.value.manifests) == {kv._GITHUB_SOURCE, kv._GITEE_SOURCE}
    assert exc_info.value.manifests[kv._GITHUB_SOURCE].kernel_version == "0.14.3"


def test_release_manifest_uses_one_available_mirror(monkeypatch):
    monkeypatch.setattr(
        kv,
        "_fetch_github_manifest",
        lambda: (_ for _ in ()).throw(RuntimeError("GitHub unavailable")),
    )
    monkeypatch.setattr(
        kv,
        "_fetch_gitee_manifest",
        lambda: _release_manifest(kv._GITEE_SOURCE),
    )

    observation = kv._fetch_latest_version()

    assert observation.version == "0.14.2"
    assert observation.source == kv._GITEE_SOURCE


def test_mirror_mismatch_is_reported_without_selecting_a_version(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo(
            running_version="0.14.1",
            installed_version="0.14.1",
            dev_reason=None,
        ),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-07-17")
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(
            kv._MirrorMismatchError(
                {
                    kv._GITHUB_SOURCE: _release_manifest(kv._GITHUB_SOURCE, "0.14.3"),
                    kv._GITEE_SOURCE: _release_manifest(kv._GITEE_SOURCE, "0.14.2"),
                }
            )
        ),
    )

    _probe_and_consume(agent)

    entry = _entries(tmp_path)[0]
    assert entry["source"] == "release-manifest-mirror-mismatch"
    assert entry["latest"] is None
    assert "do not choose the higher version" in entry["detail"]
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["mirror_mismatch"][kv._GITHUB_SOURCE]["version"] == "0.14.3"


def test_github_manifest_uses_exact_release_asset_endpoint(monkeypatch):
    manifest = {
        "schema": "lingtai.kernel.release/v1",
        "kernel_version": "0.16.4",
        "kernel_tag": "v0.16.4",
        "commit": "a" * 40,
        "generated_at": "2026-07-17T00:00:00Z",
        "sdist_fallback": "lingtai-0.16.4.tar.gz",
        "artifacts": [
            {
                "filename": "lingtai-0.16.4.tar.gz",
                "sha256": "b" * 64,
                "kind": "sdist",
                "python_tag": None,
                "abi_tag": None,
                "platform_tag": None,
            }
        ],
    }
    manifest_url = (
        "https://github.com/Lingtai-AI/lingtai-kernel/releases/download/v0.16.4/"
        "lingtai-kernel-release-manifest.json"
    )
    responses = {
        kv._GITHUB_LATEST_RELEASE_URL: json.dumps(
            {"assets": [{"name": kv._MANIFEST_ASSET_NAME, "browser_download_url": manifest_url}]}
        ).encode(),
        manifest_url: json.dumps(manifest).encode(),
    }
    calls = []

    class Response:
        status = 200
        headers = {}

        def __init__(self, body):
            self.body = body

        def read(self, _limit):
            return self.body

        def getcode(self):
            return self.status

        def close(self):
            pass

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return Response(responses[request.full_url])

    monkeypatch.setattr(kv.urllib.request, "urlopen", fake_urlopen)

    result = kv._fetch_github_manifest()

    assert result.source == kv._GITHUB_SOURCE
    assert result.kernel_version == "0.16.4"
    assert [url for url, _ in calls] == [kv._GITHUB_LATEST_RELEASE_URL, manifest_url]
    assert all(timeout == kv._REMOTE_TIMEOUT_SECONDS for _, timeout in calls)


def _transport_response(body):
    class Response:
        status = 200
        headers = {}

        def read(self, _limit):
            return body

        def getcode(self):
            return self.status

        def close(self):
            pass

    return Response()


def _transport_manifest(version="0.16.4"):
    return {
        "schema": "lingtai.kernel.release/v1",
        "kernel_version": version,
        "kernel_tag": f"v{version}",
        "commit": "a" * 40,
        "generated_at": "2026-07-17T00:00:00Z",
        "sdist_fallback": f"lingtai-{version}.tar.gz",
        "artifacts": [
            {
                "filename": f"lingtai-{version}.tar.gz",
                "sha256": "b" * 64,
                "kind": "sdist",
                "python_tag": None,
                "abi_tag": None,
                "platform_tag": None,
            }
        ],
    }


def test_gitee_manifest_uses_real_attach_files_download_shape(monkeypatch):
    manifest = _transport_manifest()
    manifest_url = "https://gitee.com/huangzesen1997/lingtai-kernel/attach_files/1"
    responses = {
        kv._GITEE_LATEST_RELEASE_URL: json.dumps(
            {"attach_files": [{"name": kv._MANIFEST_ASSET_NAME, "browser_download_url": manifest_url}]}
        ).encode(),
        manifest_url: json.dumps(manifest).encode(),
    }
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return _transport_response(responses[request.full_url])

    monkeypatch.setattr(kv.urllib.request, "urlopen", fake_urlopen)
    result = kv._fetch_gitee_manifest()
    assert result.source == kv._GITEE_SOURCE
    assert result.kernel_version == "0.16.4"
    assert calls == [kv._GITEE_LATEST_RELEASE_URL, manifest_url]


def test_gitee_manifest_asset_extraction_rejects_duplicate_or_ambiguous_entries():
    with pytest.raises(RuntimeError, match="ambiguous duplicate"):
        kv._gitee_manifest_asset_url(
            {
                "attach_files": [
                    {"name": kv._MANIFEST_ASSET_NAME, "download_url": "https://gitee.test/1"},
                    {"name": kv._MANIFEST_ASSET_NAME, "download_url": "https://gitee.test/2"},
                ]
            },
            kv._GITEE_SOURCE,
        )
    with pytest.raises(RuntimeError, match="ambiguous download"):
        kv._gitee_manifest_asset_url(
            {
                "attach_files": [
                    {
                        "name": kv._MANIFEST_ASSET_NAME,
                        "browser_download_url": "https://gitee.test/1",
                        "download_url": "https://gitee.test/2",
                    }
                ]
            },
            kv._GITEE_SOURCE,
        )


def test_gitee_only_transport_fallback_uses_attach_files(monkeypatch):
    manifest = _transport_manifest()
    manifest_url = "https://gitee.com/huangzesen1997/lingtai-kernel/attach_files/2"
    responses = {
        kv._GITEE_LATEST_RELEASE_URL: json.dumps(
            {"attach_files": [{"name": kv._MANIFEST_ASSET_NAME, "download_url": manifest_url}]}
        ).encode(),
        manifest_url: json.dumps(manifest).encode(),
    }
    monkeypatch.setattr(
        kv,
        "_fetch_github_manifest",
        lambda: (_ for _ in ()).throw(RuntimeError("GitHub unavailable")),
    )
    monkeypatch.setattr(kv.urllib.request, "urlopen", lambda request, timeout: _transport_response(responses[request.full_url]))
    observation = kv._fetch_latest_version()
    assert observation.source == kv._GITEE_SOURCE
    assert observation.version == "0.16.4"


def test_github_and_gitee_transport_mismatch_fails_closed(monkeypatch):
    github_manifest = _transport_manifest("0.16.4")
    gitee_manifest = _transport_manifest("0.16.5")
    github_url = "https://github.com/Lingtai-AI/lingtai-kernel/releases/download/v0.16.4/lingtai-kernel-release-manifest.json"
    gitee_url = "https://gitee.com/huangzesen1997/lingtai-kernel/attach_files/3"
    responses = {
        kv._GITHUB_LATEST_RELEASE_URL: json.dumps(
            {"assets": [{"name": kv._MANIFEST_ASSET_NAME, "browser_download_url": github_url}]}
        ).encode(),
        github_url: json.dumps(github_manifest).encode(),
        kv._GITEE_LATEST_RELEASE_URL: json.dumps(
            {"attach_files": [{"name": kv._MANIFEST_ASSET_NAME, "browser_download_url": gitee_url}]}
        ).encode(),
        gitee_url: json.dumps(gitee_manifest).encode(),
    }
    monkeypatch.setattr(kv.urllib.request, "urlopen", lambda request, timeout: _transport_response(responses[request.full_url]))
    with pytest.raises(kv._MirrorMismatchError) as exc_info:
        kv._fetch_latest_version()
    assert set(exc_info.value.manifests) == {kv._GITHUB_SOURCE, kv._GITEE_SOURCE}


def test_release_manifest_same_version_content_or_hash_mismatch_is_not_accepted(monkeypatch):
    monkeypatch.setattr(
        kv,
        "_fetch_github_manifest",
        lambda: _release_manifest(
            kv._GITHUB_SOURCE,
            version="0.14.2",
            manifest_sha256="a" * 64,
            artifact_hashes_sha256="b" * 64,
        ),
    )
    monkeypatch.setattr(
        kv,
        "_fetch_gitee_manifest",
        lambda: _release_manifest(
            kv._GITEE_SOURCE,
            version="0.14.2",
            manifest_sha256="c" * 64,
            artifact_hashes_sha256="d" * 64,
        ),
    )

    with pytest.raises(kv._MirrorMismatchError):
        kv._fetch_latest_version()


def test_check_does_not_block_on_slow_fetch(tmp_path, monkeypatch):
    """#730: a slow remote probe must not hold the 1s heartbeat tick."""
    import threading as _threading
    import time as _time

    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo("0.17.0", "0.17.0", None),
    )
    release = _threading.Event()

    def slow_fetch():
        release.wait(5.0)  # a slow-but-successful probe
        return "0.17.0"

    monkeypatch.setattr(kv, "_fetch_latest_version", slow_fetch)

    started = _time.monotonic()
    kv.check(agent)
    elapsed = _time.monotonic() - started

    assert elapsed < 0.5, f"check() blocked the heartbeat for {elapsed:.2f}s (#730)"
    assert _entries(tmp_path) == []
    assert kv._fetch_slot(agent) is not None
    # Cleanup: let the worker finish so no daemon thread outlives the test.
    release.set()
    kv._fetch_slot(agent).thread.join()


def test_fetch_result_consumed_on_later_probe(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo("0.14.1", "0.14.1", None),
    )
    monkeypatch.setattr(kv, "_today_utc", lambda: "2026-07-17")
    monkeypatch.setattr(kv, "_fetch_latest_version", lambda: "0.14.2")

    kv.check(agent)  # starts the async fetch; the tick returns immediately
    pending = kv._fetch_slot(agent)
    assert pending is not None
    pending.thread.join()
    _reset_fast_gate(agent)
    kv.check(agent)  # a later probe consumes the finished fetch

    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["latest"] == "0.14.2"
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["latest_seen"] == "0.14.2"
    assert state["kernel_version"]["last_remote_check_date"] == "2026-07-17"
    assert kv._fetch_slot(agent) is None


def test_fetch_error_recorded_asynchronously(tmp_path, monkeypatch):
    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo("0.17.0", "0.17.0", None),
    )
    monkeypatch.setattr(
        kv,
        "_fetch_latest_version",
        lambda: (_ for _ in ()).throw(RuntimeError("both official mirrors unavailable")),
    )

    kv.check(agent)
    pending = kv._fetch_slot(agent)
    assert pending is not None
    pending.thread.join()
    _reset_fast_gate(agent)
    kv.check(agent)

    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert "unavailable" in state["kernel_version"]["last_error"]
    assert state["kernel_version"]["last_remote_check_date"] == kv._today_utc()
    assert kv._fetch_slot(agent) is None


def test_single_flight(tmp_path, monkeypatch):
    import threading as _threading

    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo("0.17.0", "0.17.0", None),
    )
    starts = []
    original_start = kv._start_fetch

    def counting_start(a):
        starts.append(a)
        return original_start(a)

    monkeypatch.setattr(kv, "_start_fetch", counting_start)
    release = _threading.Event()

    def slow_fetch():
        release.wait(5.0)
        return "0.17.0"

    monkeypatch.setattr(kv, "_fetch_latest_version", slow_fetch)

    kv.check(agent)
    _reset_fast_gate(agent)
    kv.check(agent)
    _reset_fast_gate(agent)
    kv.check(agent)

    assert len(starts) == 1  # repeated due probes never spawn a second worker
    release.set()
    kv._fetch_slot(agent).thread.join()


def test_fetch_deadline_abandons_stuck_worker(tmp_path, monkeypatch):
    import time as _time

    agent = _Agent(tmp_path)
    monkeypatch.setattr(
        kv,
        "_runtime_info",
        lambda: kv._RuntimeInfo("0.17.0", "0.17.0", None),
    )
    # Simulate a worker started past the deadline whose ``done`` never fires
    # (e.g. a wedged DNS lookup).
    agent._nudge_kernel_fetch = kv._PendingFetch(
        thread=None,
        started_ts=_time.time() - kv._FETCH_DEADLINE_SECONDS - 1.0,
    )

    kv.check(agent)

    assert kv._fetch_slot(agent) is None
    state = json.loads((tmp_path / ".notification" / ".nudge_state.json").read_text())
    assert state["kernel_version"]["last_error"] == "fetch timed out"
    assert state["kernel_version"]["last_remote_check_date"] == kv._today_utc()
