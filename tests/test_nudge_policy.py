from __future__ import annotations

import json
import time
from pathlib import Path

from lingtai.init_reader import read_init
from lingtai.kernel.nudge import (
    ENTRY_CHANNEL_CONFIG_STALENESS,
    effective_policy,
    record_dismissal,
    run_checks,
    upsert,
)
from lingtai.kernel.nudge.init_config import check as check_init_config
from lingtai.kernel.notifications import dismiss_channel
from tests._notification_store_helpers import notification_store_for, snapshot_notifications


class _Agent:
    def __init__(self, workdir):
        self._working_dir = workdir
        self._notification_store = notification_store_for(workdir)
        self._notification_fp = ()
        self.logs = []

    def _log(self, event, **fields):
        self.logs.append((event, fields))


def _entries(workdir):
    payload = snapshot_notifications(workdir).get("nudge", {})
    return payload.get("data", {}).get("nudges", [])


def test_global_policy_defaults_and_invalid_values(monkeypatch):
    monkeypatch.delenv("LINGTAI_NUDGE_ENABLED", raising=False)
    monkeypatch.delenv("LINGTAI_NUDGE_REPEAT_INTERVAL", raising=False)
    policy = effective_policy()
    assert policy.enabled is True
    assert policy.repeat_interval_value == "24h"

    monkeypatch.setenv("LINGTAI_NUDGE_ENABLED", "wat")
    monkeypatch.setenv("LINGTAI_NUDGE_REPEAT_INTERVAL", "-2h")
    policy = effective_policy()
    assert policy.enabled is True
    assert policy.repeat_interval_value == "24h"
    assert len(policy.invalid_values) == 2


def test_clear_dismissal_preserves_future_and_removes_only_expired(monkeypatch, tmp_path):
    from lingtai.kernel import nudge as nudge_module

    agent = _Agent(tmp_path)
    clock = [1_000.0]
    monkeypatch.setattr("lingtai.kernel.nudge.time.time", lambda: clock[0])
    state_path = tmp_path / ".notification" / ".nudge_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "dismissed": {
                    "future": {"kind": "example", "until": 1_001.0},
                    "expired": {"kind": "example", "until": 999.0},
                }
            }
        ),
        encoding="utf-8",
    )

    nudge_module._clear_dismissal(agent, "future")
    after_future = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(after_future["dismissed"]) == {"future", "expired"}

    nudge_module._clear_dismissal(agent, "expired")
    after_expired = json.loads(state_path.read_text(encoding="utf-8"))
    assert after_expired["dismissed"] == {
        "future": {"kind": "example", "until": 1_001.0}
    }


def test_emitted_finding_describes_effective_global_policy(monkeypatch, tmp_path):
    agent = _Agent(tmp_path)
    monkeypatch.setenv("LINGTAI_NUDGE_ENABLED", "on")
    monkeypatch.setenv("LINGTAI_NUDGE_REPEAT_INTERVAL", "2h")

    upsert(agent, "example", {"title": "Needs attention", "detail": "read it"})

    entry = _entries(tmp_path)[0]
    assert "nudge_channel" not in entry
    assert entry["policy"] == {
        "enabled": "on",
        "repeat_after_dismiss": "2h",
        "env": {
            "enabled": "LINGTAI_NUDGE_ENABLED",
            "repeat_interval": "LINGTAI_NUDGE_REPEAT_INTERVAL",
        },
        "documentation": "system-manual/reference/environment-variables/SKILL.md",
    }
    assert "LINGTAI_NUDGE_ENABLED=on" in entry["detail"]
    assert "LINGTAI_NUDGE_REPEAT_INTERVAL=2h" in entry["detail"]
    assert "system-manual/reference/environment-variables/SKILL.md" in entry["detail"]


def test_fixed_entry_channel_metadata_is_producer_kind_owned(tmp_path):
    agent = _Agent(tmp_path)

    upsert(
        agent,
        "init_config_shape",
        {
            "title": "Needs attention",
            "detail": "read it",
            "nudge_channel": "not-a-caller-override",
        },
    )
    upsert(agent, "example", {"title": "Legacy", "nudge_channel": "not-a-channel"})

    entries = _entries(tmp_path)
    init = next(entry for entry in entries if entry["kind"] == "init_config_shape")
    legacy = next(entry for entry in entries if entry["kind"] == "example")
    assert init["nudge_channel"] == ENTRY_CHANNEL_CONFIG_STALENESS
    assert "nudge_channel" not in legacy


def test_legacy_entries_without_channel_metadata_remain_visible(tmp_path):
    agent = _Agent(tmp_path)

    def _seed_legacy(_current):
        return {
            "header": "1 nudge",
            "icon": "\U0001f514",
            "priority": "low",
            "data": {"nudges": [{"kind": "legacy", "title": "old"}]},
        }, True, None

    from lingtai.kernel.notification_store import UNCONDITIONAL

    agent._notification_store.compare_update_channel("nudge", UNCONDITIONAL, _seed_legacy)
    upsert(agent, "example", {"title": "new"})

    entries = _entries(tmp_path)
    legacy = next(entry for entry in entries if entry["kind"] == "legacy")
    new = next(entry for entry in entries if entry["kind"] == "example")
    assert "nudge_channel" not in legacy
    assert "nudge_channel" not in new


def test_legacy_state_dismissal_fingerprint_mutes_rewritten_channel_entry(tmp_path):
    agent = _Agent(tmp_path)
    body = {"title": "Needs attention", "detail": "same finding"}
    from lingtai.kernel import nudge as nudge_module

    policy = effective_policy()
    legacy_fingerprint = nudge_module._finding_fingerprint(
        "example",
        {
            "kind": "example",
            "title": body["title"],
            "detail": f"{body['detail']}\n\n{policy.message()}",
            "policy": policy.payload(),
            "policy_message": policy.message(),
        },
        include_channel=False,
    )
    state_path = tmp_path / ".notification" / ".nudge_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "dismissed": {
                    legacy_fingerprint: {
                        "kind": "example",
                        "dismissed_at": time.time(),
                        "until": time.time() + 3600,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    upsert(agent, "example", body)

    assert _entries(tmp_path) == []


def test_visible_legacy_entry_dismissed_after_upgrade_mutes_first_classified_rewrite(tmp_path):
    agent = _Agent(tmp_path)
    body = {"title": "Needs attention", "detail": "same finding", "source": "release-manifest"}

    def _seed_legacy(_current):
        return {
            "header": "1 nudge",
            "icon": "\U0001f514",
            "priority": "low",
            "data": {
                "nudges": [
                    {
                        "kind": "kernel_version",
                        "title": body["title"],
                        "detail": f"{body['detail']}\n\n{effective_policy().message()}",
                        "source": body["source"],
                        "policy": effective_policy().payload(),
                        "policy_message": effective_policy().message(),
                    }
                ]
            },
        }, True, None

    from lingtai.kernel.notification_store import UNCONDITIONAL

    agent._notification_store.compare_update_channel("nudge", UNCONDITIONAL, _seed_legacy)
    record_dismissal(agent)
    dismiss_channel(agent, "nudge", invoked_by="notification", force=True)

    upsert(agent, "kernel_version", body)

    assert _entries(tmp_path) == []


def test_disabled_policy_suppresses_all_nudge_kinds(monkeypatch, tmp_path):
    agent = _Agent(tmp_path)
    monkeypatch.setenv("LINGTAI_NUDGE_ENABLED", "off")
    upsert(agent, "example", {"title": "Needs attention"})
    assert _entries(tmp_path) == []


def test_run_checks_off_clears_visible_nudge_before_producer_gates(monkeypatch, tmp_path):
    agent = _Agent(tmp_path)
    upsert(agent, "already-visible", {"title": "old finding"})
    assert len(_entries(tmp_path)) == 1

    monkeypatch.setenv("LINGTAI_NUDGE_ENABLED", "off")
    run_checks(agent)

    assert _entries(tmp_path) == []


def test_run_checks_dev_kernel_version_silent_but_source_and_config_persist(monkeypatch, tmp_path):
    agent = _Agent(tmp_path)
    agent._source_revision_port = object()
    agent._runtime_fingerprint = {
        "git_rev": "startup111",
        "source_digest": "startup222",
        "captured_at": "t1",
    }
    upsert(agent, "kernel_version", {"title": "old", "source": "release-manifest"})

    init_payload = {
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "capabilities": {"bash": {"yolo": True}},
        },
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init = tmp_path / "init.json"
    init.write_text(json.dumps(init_payload), encoding="utf-8")
    agent._last_init_read_outcome = read_init(tmp_path)

    from lingtai.kernel.nudge import kernel_version as kv

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
    monkeypatch.setattr(
        "lingtai.kernel.base_agent.lifecycle._capture_runtime_fingerprint",
        lambda _port: {
            "git_rev": "disk333",
            "source_digest": "disk444",
            "captured_at": "t2",
        },
    )

    run_checks(agent)

    entries = _entries(tmp_path)
    by_kind = {entry["kind"]: entry for entry in entries}
    assert "kernel_version" not in by_kind
    assert by_kind["source_drift"]["nudge_channel"] == "source_integrity"
    assert by_kind["init_config_shape"]["nudge_channel"] == ENTRY_CHANNEL_CONFIG_STALENESS
    assert by_kind["source_drift"]["startup_fingerprint"]["git_rev"] == "startup111"
    assert by_kind["source_drift"]["disk_fingerprint"]["git_rev"] == "disk333"
    assert by_kind["init_config_shape"]["shape_decision"] == "NUDGE"


def test_dismissal_mutes_unresolved_finding_then_global_interval_allows_repeat(monkeypatch, tmp_path):
    agent = _Agent(tmp_path)
    monkeypatch.setenv("LINGTAI_NUDGE_REPEAT_INTERVAL", "0.001s")
    clock = [1_000.0]
    monkeypatch.setattr("lingtai.kernel.nudge.time.time", lambda: clock[0])
    body = {"title": "Needs attention", "detail": "same unresolved finding"}
    upsert(agent, "example", body)
    assert len(_entries(tmp_path)) == 1

    result = dismiss_channel(agent, "nudge", invoked_by="notification", force=True)
    assert result["status"] == "ok"
    assert _entries(tmp_path) == []

    # Dismissal is mute, so the producer cannot immediately recreate it.
    upsert(agent, "example", body)
    assert _entries(tmp_path) == []
    clock[0] += 0.01
    upsert(agent, "example", body)
    assert len(_entries(tmp_path)) == 1


def test_config_shape_nudge_consumes_outcome_and_clears_after_explicit_repair(tmp_path):
    agent = _Agent(tmp_path)
    required = {
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "capabilities": {"bash": {"yolo": True}},
        },
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init = tmp_path / "init.json"
    init.write_text(json.dumps(required), encoding="utf-8")
    outcome = read_init(tmp_path)
    check_init_config(agent, outcome)

    entry = _entries(tmp_path)[0]
    assert entry["kind"] == "init_config_shape"
    assert entry["nudge_channel"] == ENTRY_CHANNEL_CONFIG_STALENESS
    assert entry["shape_decision"] == "NUDGE"
    assert entry["compatibility_paths"][0]["raw_path"] == "manifest.capabilities.bash"
    assert entry["effective_outcome"]["effective_config"]["redacted"] is True
    assert "LINGTAI_NUDGE_ENABLED" in entry["policy"]["env"]["enabled"]

    required["manifest"]["capabilities"] = {"shell": {"yolo": True}}
    init.write_text(json.dumps(required), encoding="utf-8")
    repaired = read_init(tmp_path)
    check_init_config(agent, repaired)
    assert _entries(tmp_path) == []


def _seed_finding(tmp_path, agent):
    """Seed an init_config_shape finding via the real consumer/store path."""
    from lingtai.kernel.nudge.init_config import check as check_init_config
    required = {
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "capabilities": {"bash": {"yolo": True}},
        },
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init = tmp_path / "init.json"
    init.write_text(json.dumps(required), encoding="utf-8")
    check_init_config(agent, read_init(tmp_path))
    return init


def _assert_persisted_non_benign(tmp_path, expected_stage, expect_mapping=False):
    """Assert the persisted init_config_shape entry is the non-benign UNKNOWN
    classification with the exact stage/behavior sentence, optional
    supplemental mapping, and untouched raw bytes."""
    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["kind"] == "init_config_shape"
    assert entries[0]["title"] == "init.json configuration shape could not be classified"
    assert "READ_FAILED" in entries[0]["detail"]
    assert f"Failure stage: {expected_stage}; behavior: KEEP_PREVIOUS_EFFECTIVE." in entries[0]["detail"]
    if expect_mapping:
        assert any(
            p.get("raw_path") == "manifest.capabilities.bash"
            for p in entries[0].get("compatibility_paths", [])
        )
    else:
        # Isolated axis must NOT fabricate a supplemental mapping: the
        # persisted paths stay empty and the detail renders the none marker.
        assert entries[0].get("compatibility_paths", []) == []
        assert "Compatibility mapping: none." in entries[0]["detail"]
    return entries[0]


def test_failed_preset_lifecycle_preserves_and_clears(tmp_path):
    """PRESET failure, mixed evidence, seeded finding: same-kind entry is
    replaced by the non-benign classification with exact stage/behavior and
    supplemental mapping; raw bytes untouched; repair clears."""
    from lingtai.kernel.nudge.init_config import check as check_init_config
    from lingtai.init_reader import reader_callbacks

    agent = _Agent(tmp_path)
    init = _seed_finding(tmp_path, agent)
    seeded_raw = init.read_text(encoding="utf-8")

    def broken_load_preset(name, working_dir=None):
        raise KeyError(name)

    materialize, prepare = reader_callbacks(tmp_path, load_preset=broken_load_preset)
    manifest = json.loads(init.read_text(encoding="utf-8"))["manifest"]
    manifest["preset"] = {"active": "missing", "default": "missing", "allowed": ["missing"]}
    manifest["llm"]["context_limit"] = 500000
    manifest["context_limit"] = 350000
    bad = json.dumps({"manifest": manifest, **{
        "covenant": "operator contract", "pad": "durable state"}})
    init.write_text(bad, encoding="utf-8")

    failed = read_init(tmp_path, materialize=materialize, prepare=prepare,
                       failure_behavior="KEEP_PREVIOUS_EFFECTIVE")
    assert failed.status.value == "READ_FAILED"
    assert failed.stage == "PRESET"
    assert failed.finding_decision.value == "UNKNOWN"

    check_init_config(agent, failed)
    _assert_persisted_non_benign(tmp_path, "PRESET", expect_mapping=True)
    assert init.read_text(encoding="utf-8") == bad
    assert seeded_raw != bad

    # Repair to a canonical init.json -> PASS clears the finding.
    repaired_required = {
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "capabilities": {"shell": {"yolo": True}},
        },
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init.write_text(json.dumps(repaired_required), encoding="utf-8")
    check_init_config(agent, read_init(tmp_path))
    assert _entries(tmp_path) == []


def test_failed_preset_lifecycle_isolated_seeded(tmp_path):
    """PRESET failure, isolated (no bash->shell mixing), from a seeded
    finding: the same-kind entry is replaced with exact non-benign detail."""
    from lingtai.kernel.nudge.init_config import check as check_init_config
    from lingtai.init_reader import reader_callbacks

    agent = _Agent(tmp_path)
    init = tmp_path / "init.json"
    required = {
        "manifest": {"llm": {"provider": "openai", "model": "gpt-4o"}},
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init.write_text(json.dumps(required), encoding="utf-8")
    # Seed the same kind so the failure replaces rather than inserts.
    upsert(agent, "init_config_shape", {"title": "seed", "detail": "stale"})
    assert len(_entries(tmp_path)) == 1
    seeded_raw = init.read_text(encoding="utf-8")

    def broken_load_preset(name, working_dir=None):
        raise KeyError(name)

    materialize, prepare = reader_callbacks(tmp_path, load_preset=broken_load_preset)
    manifest = json.loads(init.read_text(encoding="utf-8"))["manifest"]
    manifest["preset"] = {"active": "missing", "default": "missing", "allowed": ["missing"]}
    bad = json.dumps({"manifest": manifest, **{
        "covenant": "operator contract", "pad": "durable state"}})
    init.write_text(bad, encoding="utf-8")
    failed = read_init(tmp_path, materialize=materialize, prepare=prepare,
                       failure_behavior="KEEP_PREVIOUS_EFFECTIVE")
    assert failed.status.value == "READ_FAILED"
    assert failed.stage == "PRESET"
    assert failed.finding_decision.value == "UNKNOWN"
    check_init_config(agent, failed)
    _assert_persisted_non_benign(tmp_path, "PRESET", expect_mapping=False)
    assert init.read_text(encoding="utf-8") == bad
    assert seeded_raw != bad


def test_failed_resolve_lifecycle_seeded(tmp_path, monkeypatch):
    """RESOLVE failure via a scoped monkeypatch seam, seeded finding: exact
    stage/behavior sentence + raw no-write, without touching module globals."""
    from lingtai.kernel.nudge.init_config import check as check_init_config
    from lingtai import init_reader

    agent = _Agent(tmp_path)
    init = _seed_finding(tmp_path, agent)

    def broken_resolve(data, working_dir):
        raise OSError("deliberate resolve failure")

    seeded_raw = init.read_text(encoding="utf-8")
    monkeypatch.setattr(init_reader, "resolve_paths", broken_resolve)
    failed = read_init(tmp_path, failure_behavior="KEEP_PREVIOUS_EFFECTIVE")
    assert failed.status.value == "READ_FAILED"
    assert failed.stage == "RESOLVE"
    assert failed.finding_decision.value == "UNKNOWN"
    check_init_config(agent, failed)
    _assert_persisted_non_benign(tmp_path, "RESOLVE", expect_mapping=True)
    # Raw bytes still equal the seeded file (resolver never wrote).
    assert init.read_text(encoding="utf-8") == seeded_raw


def test_failed_validate_lifecycle_seeded_mixed(tmp_path):
    """VALIDATE failure with mixed bash->shell evidence, seeded same-kind:
    exact stage sentence, supplemental mapping preserved, raw untouched."""
    from lingtai.kernel.nudge.init_config import check as check_init_config

    agent = _Agent(tmp_path)
    required = {
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "capabilities": {"bash": {"yolo": True}},
        },
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init = tmp_path / "init.json"
    init.write_text(json.dumps(required), encoding="utf-8")
    upsert(agent, "init_config_shape", {"title": "seed", "detail": "stale"})
    assert len(_entries(tmp_path)) == 1

    bad = dict(required)
    bad["manifest"]["llm"]["provider"] = 5
    raw_bad = json.dumps(bad)
    init.write_text(raw_bad, encoding="utf-8")
    failed = read_init(tmp_path, failure_behavior="KEEP_PREVIOUS_EFFECTIVE")
    assert failed.status.value == "READ_FAILED"
    assert failed.stage == "VALIDATE"
    assert failed.finding_decision.value == "UNKNOWN"
    check_init_config(agent, failed)
    _assert_persisted_non_benign(tmp_path, "VALIDATE", expect_mapping=True)
    assert init.read_text(encoding="utf-8") == raw_bad


def test_failed_resolve_lifecycle_seeded_isolated(tmp_path, monkeypatch):
    """RESOLVE failure isolated (no bash->shell mixing), seeded same-kind:
    exact stage sentence, raw untouched via scoped monkeypatch seam."""
    from lingtai.kernel.nudge.init_config import check as check_init_config
    from lingtai import init_reader

    agent = _Agent(tmp_path)
    required = {
        "manifest": {"llm": {"provider": "openai", "model": "gpt-4o"}},
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init = tmp_path / "init.json"
    init.write_text(json.dumps(required), encoding="utf-8")
    upsert(agent, "init_config_shape", {"title": "seed", "detail": "stale"})
    assert len(_entries(tmp_path)) == 1
    seeded_raw = init.read_text(encoding="utf-8")

    def broken_resolve(data, working_dir):
        raise OSError("deliberate resolve failure")

    monkeypatch.setattr(init_reader, "resolve_paths", broken_resolve)
    failed = read_init(tmp_path, failure_behavior="KEEP_PREVIOUS_EFFECTIVE")
    assert failed.status.value == "READ_FAILED"
    assert failed.stage == "RESOLVE"
    assert failed.finding_decision.value == "UNKNOWN"
    check_init_config(agent, failed)
    _assert_persisted_non_benign(tmp_path, "RESOLVE", expect_mapping=False)
    assert init.read_text(encoding="utf-8") == seeded_raw


def test_failed_validate_lifecycle_seeded_isolated(tmp_path):
    """VALIDATE failure, isolated, seeded finding: exact stage/behavior
    sentence and raw bytes untouched."""
    from lingtai.kernel.nudge.init_config import check as check_init_config

    agent = _Agent(tmp_path)
    required = {
        "manifest": {"llm": {"provider": "openai", "model": "gpt-4o"}},
        "covenant": "operator contract",
        "pad": "durable state",
    }
    init = tmp_path / "init.json"
    init.write_text(json.dumps(required), encoding="utf-8")
    # Seed the same kind (init_config_shape) so replacement is observable.
    upsert(agent, "init_config_shape", {"title": "seed", "detail": "stale"})
    assert len(_entries(tmp_path)) == 1

    bad = dict(required)
    bad["manifest"] = {"llm": {"provider": 5, "model": "gpt-4o"},
                       "context_limit": "not-an-int"}
    raw_bad = json.dumps(bad)
    init.write_text(raw_bad, encoding="utf-8")
    failed = read_init(tmp_path, failure_behavior="KEEP_PREVIOUS_EFFECTIVE")
    assert failed.status.value == "READ_FAILED"
    assert failed.stage == "VALIDATE"
    assert failed.finding_decision.value == "UNKNOWN"
    check_init_config(agent, failed)
    _assert_persisted_non_benign(tmp_path, "VALIDATE", expect_mapping=False)
    assert init.read_text(encoding="utf-8") == raw_bad


def test_blocked_lifecycle_renders_blocked_title_and_preserves_raw(tmp_path):
    """A BLOCKED shape conflict through the real consumer/store path renders
    the blocked title with exact stage/behavior and does not touch raw
    init.json (seeded finding replaced in place)."""
    from lingtai.kernel.nudge.init_config import check as check_init_config

    agent = _Agent(tmp_path)
    init = _seed_finding(tmp_path, agent)

    required = {
        "manifest": {
            "llm": {"provider": "openai", "model": "gpt-4o"},
            "capabilities": {"bash": {"yolo": False}, "shell": {"yolo": True}},
        },
        "covenant": "operator contract",
        "pad": "durable state",
    }
    raw = json.dumps(required)
    init.write_text(raw, encoding="utf-8")
    failed = read_init(tmp_path, failure_behavior="KEEP_PREVIOUS_EFFECTIVE")
    assert failed.status.value == "READ_FAILED"
    assert failed.shape_decision.value == "BLOCKED"
    assert failed.finding_decision.value == "BLOCKED"

    check_init_config(agent, failed)
    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["title"] == "init.json configuration shape is blocked by a conflict"
    assert "Failure stage: CONFLICT; behavior: KEEP_PREVIOUS_EFFECTIVE." in entries[0]["detail"]
    assert init.read_text(encoding="utf-8") == raw


def _frontmatter_related_files(text):
    """Parse the leading YAML frontmatter and return the related_files list."""
    import re
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing frontmatter"
    block = m.group(1)
    lines = [ln.strip() for ln in block.splitlines()]
    in_related = False
    files = []
    for ln in lines:
        if ln.startswith("related_files:"):
            in_related = True
            continue
        if in_related:
            if ln.startswith("- "):
                files.append(ln[2:].strip())
            elif ln and not ln.startswith("-") and not ln.startswith("  -"):
                break
    return files


def test_runtime_policy_ownership_model_is_documented_across_normative_sources():
    """Init must not regain an ordinary runtime-policy source by accident."""
    root = Path(__file__).parents[1]
    contract_path = root / "src/lingtai/CONTRACT.md"
    anatomy_path = root / "src/lingtai/ANATOMY.md"
    init_jsonc_path = root / "src/lingtai/init.jsonc"
    router_path = root / "src/lingtai/intrinsic_skills/system-manual/SKILL.md"
    substrate_path = root / "src/lingtai/intrinsic_skills/system-manual/reference/substrate-manual/SKILL.md"
    contract = contract_path.read_text(encoding="utf-8")
    anatomy = anatomy_path.read_text(encoding="utf-8")
    init_jsonc = init_jsonc_path.read_text(encoding="utf-8")
    router = router_path.read_text(encoding="utf-8")
    substrate = substrate_path.read_text(encoding="utf-8")

    router_ref = "src/lingtai/intrinsic_skills/system-manual/SKILL.md"
    substrate_ref = "src/lingtai/intrinsic_skills/system-manual/reference/substrate-manual/SKILL.md"

    # 1. Owner-twin manual-edge parity: both Contract and Anatomy declare the
    #    router and substrate reference as frontmatter related_files, and the
    #    router actually reaches the substrate reference.
    for text, name in ((contract, "CONTRACT.md"), (anatomy, "ANATOMY.md")):
        rf = _frontmatter_related_files(text)
        assert router_ref in rf, f"{name} missing router in related_files"
        assert substrate_ref in rf, f"{name} missing substrate reference in related_files"
    assert substrate_ref in router or "reference/substrate-manual/SKILL.md" in router, \
        "system-manual router does not reach substrate reference"

    # 2. Ownership vocabulary in all normative sources.
    for text, name in ((contract, "CONTRACT.md"), (anatomy, "ANATOMY.md"), (substrate, "substrate")):
        assert "manifest.llm.context_limit" in text, f"{name} missing nested preset location"
    assert "manifest.context_limit" in contract
    assert "context_limit" not in init_jsonc

    # 3. Source-specific direction assertions.
    assert "fixed defaults" in contract
    assert "materialization discards it" in anatomy
    assert "discard a preset context" in substrate

    # 4. The old "open implementation question" framing must not regress.
    assert "root-vs-LLM `context_limit` semantics" not in substrate
