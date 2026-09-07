"""Tests for the avatar capability."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai.tools.bash import BashManager
from lingtai.tools.avatar import AvatarManager
from lingtai.tools.avatar._launcher import AvatarLaunchReceipt
from tests._service_helpers import make_gemini_mock_service as make_mock_service




@pytest.fixture
def fake_avatar_launch():
    """Patches AvatarManager._launch and _wait_for_boot so spawn-path tests
    don't actually fork a child process.

    Also wraps lingtai.agent.Agent so that every test agent gets a minimal
    init.json written to its working_dir on construction — required by
    AvatarManager._spawn's ``parent has no init.json`` gate.

    The launcher contract returns (AvatarLaunchReceipt, stderr_path);
    _wait_for_boot returns (status, error). We synthesize a shape-correct
    receipt that lets the manager's success branch run and exposes release.
    """
    proc = MagicMock(pid=12345)
    proc.poll.return_value = None
    receipt = AvatarLaunchReceipt(pid=12345, handle=proc)
    fake_stderr = Path("/tmp/avatar_stderr.log")

    from lingtai.agent import Agent as _OrigAgent

    class _AutoInitAgent(_OrigAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            init_path = self._working_dir / "init.json"
            if not init_path.is_file():
                init_path.parent.mkdir(parents=True, exist_ok=True)
                # Reflect the agent's actual capability list/kwargs into the
                # init.json manifest so AvatarManager._make_avatar_init can
                # propagate them to the spawned child.
                cap_dict = {}
                for cap_entry in (self._capabilities or []):
                    if isinstance(cap_entry, tuple) and len(cap_entry) == 2:
                        cap_dict[cap_entry[0]] = cap_entry[1] or {}
                    elif isinstance(cap_entry, str):
                        cap_dict[cap_entry] = {}
                init_path.write_text(json.dumps({
                    "manifest": {
                        "agent_name": self.agent_name,
                        "admin": dict(self._admin) if self._admin else {},
                        "capabilities": cap_dict,
                    },
                }))

    with patch.object(AvatarManager, "_launch", return_value=(receipt, fake_stderr)), \
         patch.object(AvatarManager, "_wait_for_boot", return_value=("ok", None)), \
         patch("lingtai.agent.Agent", _AutoInitAgent):
        yield proc


class TestAvatarManager:
    @pytest.fixture(autouse=True)
    def _autopatch(self, fake_avatar_launch):
        """Apply launch patch automatically to every test in this class."""
        yield

    def test_spawn_returns_address(self, tmp_path, fake_avatar_launch):
        """Spawn should return a valid address."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "helper", "confirm": True}})
        assert result["status"] == "ok"
        assert "address" in result
        assert result["address"]  # filesystem path (non-empty string)
        assert result["agent_name"] == "helper"
        fake_avatar_launch.poll.assert_called_once_with()

    def test_spawn_inherits_capabilities(self, tmp_path):
        """Spawned agent's init.json should carry all of parent's capabilities."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities={"bash": {"yolo": True}, "avatar": {}})
        result = parent._tool_handlers["avatar"]({"action": "spawn", "input": {"name": "child", "confirm": True}})
        assert result["status"] == "ok"
        # New architecture: avatars run as their own processes; introspection
        # is via the avatar's on-disk init.json, not an in-process _peers map.
        child_init_path = parent._working_dir.parent / "child" / "init.json"
        assert child_init_path.is_file()
        child_init = json.loads(child_init_path.read_text())
        child_caps = child_init.get("manifest", {}).get("capabilities", {})
        assert "shell" in child_caps
        assert "bash" not in child_caps
        assert "avatar" in child_caps

    @pytest.mark.parametrize("avatar_type", ["shallow", "deep"])
    def test_generic_spawn_does_not_persist_driver_derived_child_state(self, tmp_path, avatar_type):
        """A legacy avatar spawn must not become Driver-derived by itself."""
        from lingtai.agent import Agent
        from lingtai.tools.avatar._launcher import derived_avatar_state_path

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=tmp_path / "test",
            capabilities=["avatar"],
        )
        result = parent.get_capability("avatar").handle(
            {
                "action": "spawn",
                "input": {"name": "child", "type": avatar_type, "confirm": True},
            }
        )

        assert result["status"] == "ok"
        assert not derived_avatar_state_path(parent._working_dir.parent / "child").exists()

    def test_spawn_inherits_covenant(self, tmp_path):
        """Spawned agent should inherit parent's covenant."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"], covenant="Be helpful and concise.")
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "helper", "confirm": True}})
        assert result["status"] == "ok"

    def test_spawn_no_admin(self, tmp_path):
        """Avatar should never get admin privileges, even if parent has them."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"], admin={"karma": True})
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "helper", "confirm": True}})
        assert result["status"] == "ok"
        child_init = json.loads((parent._working_dir.parent / "helper" / "init.json").read_text())
        child_admin = child_init.get("manifest", {}).get("admin", {})
        assert child_admin == {}

    def test_spawn_duplicate_name_error(self, tmp_path):
        """Spawning a name that already exists on disk should return an error.

        Two duplicate cases collapse to the same outward signal in the current
        implementation: (1) the directory pre-exists, (2) the peer is live.
        Both produce a non-ok result. (The "already_active" return path also
        exists for live peers, but the ledger lookup uses a basename-only
        working_dir, so is_alive() can't currently find the heartbeat —
        tracked as a separate bug.)
        """
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        r1 = mgr.handle({"action": "spawn", "input": {"name": "helper", "confirm": True}})
        assert r1["status"] == "ok"
        r2 = mgr.handle({"action": "spawn", "input": {"name": "helper", "confirm": True}})
        assert "error" in r2 or r2.get("status") == "already_active"

    def test_spawn_does_not_copy_identity_files(self, tmp_path):
        """Spawning an avatar should not copy parent character/pad/knowledge.
        (The legacy ``mirror=True`` identity-copy behavior was removed.)"""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        # Write identity files to parent
        system_dir = parent._working_dir / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "character.md").write_text("I am the parent")
        (system_dir / "pad.md").write_text("Parent pad")
        knowledge_dir = parent._working_dir / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        (knowledge_dir / "knowledge.json").write_text('{"entries": []}')

        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "blank", "confirm": True}})
        assert result["status"] == "ok"
        child_dir = parent._working_dir.parent / "blank"
        # Character and knowledge should NOT be copied
        assert not (child_dir / "system" / "character.md").is_file()
        assert not (child_dir / "knowledge" / "knowledge.json").is_file()

    def test_ledger_records_spawn(self, tmp_path):
        """Ledger should record the spawn event with name + boot_status."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                        capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        mgr.handle({"action": "spawn", "input": {"name": "clone", "confirm": True}})
        ledger = (parent._working_dir / "delegates" / "ledger.jsonl").read_text().strip()
        records = [json.loads(line) for line in ledger.splitlines()]
        assert records[0]["event"] == "avatar_admission_decision"
        assert records[0]["reason_code"] == "legacy_default"
        record = records[-1]
        assert record["name"] == "clone"
        assert record["boot_status"] == "ok"

    def test_profile_avatar_launch_reaches_structured_admission_before_spawn(
        self, tmp_path, monkeypatch
    ):
        """The real AvatarManager/Agent adapter path reaches the 2a seam."""
        from lingtai.adapters.acp.puffo_v0 import RUNTIME_POLICY
        from lingtai.kernel.provider_admission import (
            DerivedLaunchCapability,
            DerivedLaunchDecision,
            ProviderAdmissionState,
            RootProviderAdmission,
            bind_provider_admission,
            clear_provider_admission,
        )
        from lingtai.agent import Agent

        class _Lease:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _DenyingPort:
            def __init__(self):
                self.calls = []

            def authorize_derived_launch(self, parent, capability):
                self.calls.append((parent, capability))
                return DerivedLaunchDecision(
                    ProviderAdmissionState.DENIED,
                    "derived_launch_denied_by_test",
                    audit_id="audit-avatar-2a",
                    child_endpoint_lease=lease,
                )

        lease = _Lease()
        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=tmp_path / "parent",
            capabilities=["avatar"],
            _turn_origin_policy=RUNTIME_POLICY,
            derived_launch_admission_port=_DenyingPort(),
        )
        port = parent._derived_launch_admission_port
        root = RootProviderAdmission("root-avatar-2a", RUNTIME_POLICY.policy_version)
        token = bind_provider_admission(root)
        try:
            result = parent.get_capability("avatar").handle(
                {"action": "spawn", "input": {"name": "child", "confirm": True}}
            )
        finally:
            clear_provider_admission(token)

        assert "derived_launch_denied_by_test" in result["error"]
        assert lease.closed is True
        assert port.calls == [(root, DerivedLaunchCapability.AVATAR)]
        assert not (parent._working_dir.parent / "child").exists()
        records = [
            json.loads(line)
            for line in (parent._working_dir / "delegates" / "ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert records == [
            {
                "ts": records[0]["ts"],
                "event": "avatar_admission_decision",
                "name": "child",
                "capability": "avatar",
                "state": "denied",
                "reason_code": "derived_launch_denied_by_test",
                "audit_id": "audit-avatar-2a",
            }
        ]

    def test_profile_avatar_admission_error_closes_untransferred_lease(self, tmp_path):
        """A port-raised rejection releases the lease before Avatar returns."""
        from lingtai.adapters.acp.puffo_v0 import RUNTIME_POLICY
        from lingtai.kernel.provider_admission import (
            DerivedLaunchAdmissionError,
            DerivedLaunchDecision,
            ProviderAdmissionState,
            RootProviderAdmission,
            bind_provider_admission,
            clear_provider_admission,
        )
        from lingtai.agent import Agent

        class _Lease:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        lease = _Lease()

        class _RaisingPort:
            def authorize_derived_launch(self, _parent, _capability):
                raise DerivedLaunchAdmissionError(
                    DerivedLaunchDecision(
                        ProviderAdmissionState.INDETERMINATE,
                        "driver_unavailable",
                        child_endpoint_lease=lease,
                    )
                )

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=tmp_path / "parent",
            capabilities=["avatar"],
            _turn_origin_policy=RUNTIME_POLICY,
            derived_launch_admission_port=_RaisingPort(),
        )
        token = bind_provider_admission(
            RootProviderAdmission("root-avatar-raised-lease", RUNTIME_POLICY.policy_version)
        )
        try:
            result = parent.get_capability("avatar").handle(
                {"action": "spawn", "input": {"name": "child", "confirm": True}}
            )
        finally:
            clear_provider_admission(token)

        assert "driver_unavailable" in result["error"]
        assert lease.closed is True

    def test_profile_avatar_ledger_failure_closes_untransferred_lease(self, tmp_path, monkeypatch):
        """Ledger I/O cannot strand a grant before spawn owns its lease."""
        from lingtai.adapters.acp.puffo_v0 import RUNTIME_POLICY
        from lingtai.kernel.provider_admission import (
            DerivedLaunchDecision,
            ProviderAdmissionState,
            RootProviderAdmission,
            bind_provider_admission,
            clear_provider_admission,
        )
        from lingtai.agent import Agent

        class _Lease:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        lease = _Lease()

        class _GrantingPort:
            def authorize_derived_launch(self, _parent, _capability):
                return DerivedLaunchDecision(
                    ProviderAdmissionState.GRANTED,
                    "allowed",
                    child_endpoint_lease=lease,
                )

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=tmp_path / "parent",
            capabilities=["avatar"],
            _turn_origin_policy=RUNTIME_POLICY,
            derived_launch_admission_port=_GrantingPort(),
        )
        manager = parent.get_capability("avatar")
        monkeypatch.setattr(manager, "_append_ledger", MagicMock(side_effect=OSError("disk full")))
        token = bind_provider_admission(
            RootProviderAdmission("root-avatar-ledger-lease", RUNTIME_POLICY.policy_version)
        )
        try:
            with pytest.raises(OSError, match="disk full"):
                manager._authorize_derived_launch("child")
        finally:
            clear_provider_admission(token)

        assert lease.closed is True

    def test_profile_avatar_grant_reaches_the_same_launch_recorder(
        self, tmp_path, monkeypatch
    ):
        """The zero-avatar-spawn assertion has a same-recorder positive control."""
        from lingtai.adapters.acp.puffo_v0 import RUNTIME_POLICY
        from lingtai.kernel.provider_admission import (
            DerivedLaunchCapability,
            DerivedLaunchDecision,
            ProviderAdmissionState,
            RootProviderAdmission,
            bind_provider_admission,
            clear_provider_admission,
        )
        from lingtai.agent import Agent

        lease = object()

        class _GrantingPort:
            def __init__(self):
                self.calls = []

            def authorize_derived_launch(self, parent, capability):
                self.calls.append((parent, capability))
                return DerivedLaunchDecision(
                    ProviderAdmissionState.GRANTED,
                    "derived_launch_allowed_by_test",
                    audit_id="audit-avatar-positive-2a",
                    child_endpoint_lease=lease,
                )

        launch_calls = []
        proc = MagicMock(pid=12345)
        proc.poll.return_value = None
        receipt = AvatarLaunchReceipt(pid=12345, handle=proc)
        # Scope this override inside the autouse fixture's AvatarManager patch.
        # A test-level monkeypatch restored after that fixture would otherwise
        # leave its stale mock behind for later launcher-contract tests.
        with monkeypatch.context() as launch_patch:
            launch_patch.setattr(
                AvatarManager,
                "_launch",
                lambda _self, working_dir, **kwargs: (
                    launch_calls.append((working_dir, kwargs))
                    or (receipt, Path("/tmp/avatar-positive-2a.stderr"))
                ),
            )
            parent = Agent(
                service=make_mock_service(),
                agent_name="parent",
                working_dir=tmp_path / "parent",
                capabilities=["avatar"],
                _turn_origin_policy=RUNTIME_POLICY,
                derived_launch_admission_port=_GrantingPort(),
            )
            port = parent._derived_launch_admission_port
            root = RootProviderAdmission("root-avatar-positive-2a", RUNTIME_POLICY.policy_version)
            token = bind_provider_admission(root)
            try:
                result = parent.get_capability("avatar").handle(
                    {"action": "spawn", "input": {"name": "child", "confirm": True}}
                )
            finally:
                clear_provider_admission(token)

        assert result["status"] == "ok"
        assert port.calls == [(root, DerivedLaunchCapability.AVATAR)]
        assert launch_calls == [
            (
                parent._working_dir.parent / "child",
                {"authority_lease": lease, "derived_child": True},
            )
        ]
        from lingtai.tools.avatar._launcher import derived_avatar_state_path
        assert derived_avatar_state_path(parent._working_dir.parent / "child").is_file()
        records = [
            json.loads(line)
            for line in (parent._working_dir / "delegates" / "ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert records[0]["event"] == "avatar_admission_decision"
        assert records[0]["state"] == "granted"
        assert records[0]["audit_id"] == "audit-avatar-positive-2a"

    def test_profile_avatar_early_return_closes_unhanded_driver_lease(self, tmp_path):
        """A Driver lease is closed when spawn aborts before the launcher Port."""
        from lingtai.adapters.acp.puffo_v0 import RUNTIME_POLICY
        from lingtai.kernel.provider_admission import (
            DerivedLaunchDecision,
            ProviderAdmissionState,
            RootProviderAdmission,
            bind_provider_admission,
            clear_provider_admission,
        )
        from lingtai.agent import Agent

        class _Lease:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _GrantingPort:
            def __init__(self, lease):
                self.lease = lease

            def authorize_derived_launch(self, _parent, _capability):
                return DerivedLaunchDecision(
                    ProviderAdmissionState.GRANTED,
                    "derived_launch_allowed_by_test",
                    child_endpoint_lease=self.lease,
                )

        lease = _Lease()
        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=tmp_path / "parent",
            capabilities=["avatar"],
            _turn_origin_policy=RUNTIME_POLICY,
            derived_launch_admission_port=_GrantingPort(lease),
        )
        (parent._working_dir.parent / "child").mkdir()
        token = bind_provider_admission(
            RootProviderAdmission("root-avatar-lease-close", RUNTIME_POLICY.policy_version)
        )
        try:
            result = parent.get_capability("avatar").handle(
                {"action": "spawn", "input": {"name": "child", "confirm": True}}
            )
        finally:
            clear_provider_admission(token)

        assert result == {"error": "Directory 'child' already exists. Choose another name."}
        assert lease.closed is True


class TestMissionQualityGate:
    """Issue #33 — mission/dry_run/confirm guardrails on avatar_spawn."""

    @pytest.fixture(autouse=True)
    def _autopatch(self, fake_avatar_launch):
        yield

    def _parent(self, tmp_path):
        from lingtai.agent import Agent
        return Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=tmp_path / "parent",
            capabilities=["avatar"],
        )

    def test_helper_rejects_empty(self):
        from lingtai.tools.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("")
        assert unsafe and "empty" in reason

    def test_helper_rejects_short(self):
        from lingtai.tools.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("too short")
        assert unsafe and "short" in reason

    def test_helper_rejects_test_word(self):
        from lingtai.tools.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("test")
        assert unsafe

    def test_helper_rejects_test_prefix(self):
        from lingtai.tools.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe("debug something something something")
        assert unsafe and "placeholder" in reason

    def test_helper_accepts_real_mission(self):
        from lingtai.tools.avatar import _mission_looks_unsafe
        unsafe, reason = _mission_looks_unsafe(
            "Investigate the heartbeat regression in the kernel and report findings"
        )
        assert not unsafe and reason == ""

    def test_spawn_with_no_mission_returns_confirmation_needed(self, tmp_path):
        """Spawn with no _reasoning and no confirm should be refused with a preview."""
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "helper"}})
        assert result["status"] == "confirmation_needed"
        assert "warning" in result
        assert "preview" in result
        assert result["preview"]["name"] == "helper"
        # No working dir created
        assert not (parent._working_dir.parent / "helper").exists()
        # No ledger entry
        assert not (parent._working_dir / "delegates" / "ledger.jsonl").exists()

    def test_spawn_with_short_mission_returns_confirmation_needed(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "helper"}, "_reasoning": "test"})
        assert result["status"] == "confirmation_needed"
        assert result["preview"]["mission"] == "test"
        assert result["preview"]["mission_chars"] == 4

    def test_spawn_with_confirm_bypasses_gate(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        # No mission, but confirm=True acknowledges the risk.
        result = mgr.handle({"action": "spawn", "input": {"name": "helper", "confirm": True}})
        assert result["status"] == "ok"
        assert (parent._working_dir.parent / "helper").is_dir()

    def test_spawn_with_real_mission_proceeds(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "spawn",
            "input": {"name": "helper"},
            "_reasoning": "Investigate the heartbeat regression and report back via mail",
        })
        assert result["status"] == "ok"

    def test_dry_run_returns_preview_without_spawning(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "helper", "dry_run": True}})
        assert result["status"] == "dry_run"
        assert result["preview"]["name"] == "helper"
        assert result["preview"]["type"] == "shallow"
        assert result["preview"]["address"] == "helper"
        # The preview reports that an empty mission would have tripped the gate.
        assert result["preview"]["mission_unsafe"] is True
        # No working dir, no ledger.
        assert not (parent._working_dir.parent / "helper").exists()
        assert not (parent._working_dir / "delegates" / "ledger.jsonl").exists()

    def test_dry_run_does_not_require_confirm(self, tmp_path):
        """Dry-run is preview-only; mission gate must not block it."""
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        # Empty mission + no confirm + dry_run=True → returns dry_run, not confirmation_needed.
        result = mgr.handle({"action": "spawn", "input": {"name": "helper", "dry_run": True}})
        assert result["status"] == "dry_run"

    def test_dry_run_preview_reports_real_mission_safe(self, tmp_path):
        parent = self._parent(tmp_path)
        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "spawn",
            "input": {"name": "helper", "dry_run": True},
            "_reasoning": "Investigate the heartbeat regression and report back via mail",
        })
        assert result["status"] == "dry_run"
        assert result["preview"]["mission_unsafe"] is False
        assert result["preview"]["mission_reason"] == ""

    def test_schema_exposes_dry_run_and_confirm(self):
        """The dry_run/confirm gates stay model-visible after the LTP v2 migration.

        Under the action-separated envelope they live inside the ``spawn``
        branch of ``input.anyOf`` rather than at the root, but they must still
        be declared with their gate types so the model can actually reach
        them.
        """
        from lingtai.tools.avatar import get_schema
        sch = get_schema("en")
        branches = {b["title"]: b for b in sch["properties"]["input"]["anyOf"]}
        spawn_props = branches["spawn input"]["properties"]
        assert "dry_run" in spawn_props
        assert spawn_props["dry_run"]["type"] == ["boolean", "null"]
        assert "confirm" in spawn_props
        assert spawn_props["confirm"]["type"] == ["boolean", "null"]
        assert "rules input" not in branches
        assert sch["required"] == ["action", "input", "reasoning"]
        assert sch["properties"]["action"]["enum"] == [
            "spawn", "settings", "manual"
        ]

    def test_description_points_to_avatar_manual_after_prompt_compaction(self):
        """The terse tool description should route safety guidance to the manual.

        Prompt-token compaction moved verbose WARNING copy out of the always-on
        tool description and into avatar-manual. The safety contract now lives
        in the schema gates (dry_run/confirm, now inside the spawn branch of
        ``input.anyOf``) plus the manual pointer, not in a long description
        string.
        """
        from lingtai.tools.avatar import get_description, get_schema
        desc = get_description("en")
        schema = get_schema("en")
        assert "avatar-manual" in desc
        assert "Before the first spawn, call avatar(action='manual'" in desc
        assert "WARNING" not in desc
        manual = (
            Path(__file__).parents[1] / "src/lingtai/tools/avatar/manual/SKILL.md"
        ).read_text(encoding="utf-8")
        assert "Before the first spawn" in manual
        assert "reference/spawn.md" in manual
        assert "reference/lifecycle.md" in manual
        spawn_props = {
            b["title"]: b for b in schema["properties"]["input"]["anyOf"]
        }["spawn input"]["properties"]
        assert "confirm" in spawn_props
        assert "dry_run" in spawn_props
        assert "action" in schema["properties"]



class TestAddCapability:
    def test_add_capability_avatar(self, tmp_path):
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                           capabilities=["avatar"])
        mgr = agent.get_capability("avatar")
        assert isinstance(mgr, AvatarManager)
        assert "avatar" in agent._tool_handlers
        assert "avatar" in {s.name for s in agent._tool_schemas}
        assert "avatar_spawn" not in agent._tool_handlers
        assert "avatar_spawn" not in {s.name for s in agent._tool_schemas}
        assert "avatar_rules" not in agent._tool_handlers
        assert "avatar_rules" not in {s.name for s in agent._tool_schemas}

    def test_add_capability_unknown(self, tmp_path):
        """Unknown capability is logged + skipped (not raised) so a bad name
        in init.json doesn't kill agent boot. The capability simply
        doesn't appear in the agent's tool surface."""
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test",
                      working_dir=tmp_path / "test",
                      capabilities=["nonexistent"])
        assert "nonexistent" not in agent._tool_handlers

    def test_add_multiple_capabilities_separately(self, tmp_path):
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                           capabilities={"bash": {"yolo": True}, "avatar": {}})
        bash_mgr = agent.get_capability("bash")
        avatar_mgr = agent.get_capability("avatar")
        assert isinstance(bash_mgr, BashManager)
        assert isinstance(avatar_mgr, AvatarManager)

    def test_capabilities_log(self, tmp_path):
        """Agent should record (name, kwargs) in _capabilities.

        Core defaults are recorded too — the assertions here verify that
        explicit caller-supplied kwargs land in `_capabilities` with the
        expected merged shape.
        """
        from lingtai.agent import Agent
        agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
                           capabilities={"bash": {"yolo": True}, "avatar": {}})
        caps_by_name = {name: kwargs for name, kwargs in agent._capabilities}
        # Legacy caller input is normalized to the canonical public `shell`
        # capability before recording/inheritance.
        assert caps_by_name.get("shell") == {"yolo": True}
        assert "bash" not in caps_by_name
        assert caps_by_name.get("avatar") == {}


class TestUnifiedAvatarTool:
    """Regression coverage for the avatar_spawn + avatar_rules → avatar merge,
    and for the later removal of the ``rules`` action and its automatic
    post-spawn fan-out (Option B)."""

    def test_schema_is_ltp_v2_action_separated_envelope(self):
        """The root is exactly the LTP v2 envelope, not the old flat object.

        Pre-migration the schema was a plain object with every action's fields
        merged at the root and no top-level combinators. It is now the
        action-separated envelope: four root fields, three of them required,
        closed to anything else, with one ``input.anyOf`` branch per action.
        """
        from lingtai.tools.avatar import get_schema
        sch = get_schema("en")
        assert sch["type"] == "object"
        assert set(sch["properties"]) == {"action", "input", "reasoning", "summarize"}
        assert sch["required"] == ["action", "input", "reasoning"]
        assert sch["additionalProperties"] is False
        assert sch["properties"]["action"]["type"] == "string"
        assert sch["properties"]["action"]["enum"] == [
            "spawn", "settings", "manual"
        ]
        assert len(sch["properties"]["input"]["anyOf"]) == 3

    def test_spawn_dispatch_preserves_behavior_and_reasoning(self, tmp_path, fake_avatar_launch):
        """action='spawn' preserves outputs and _reasoning → first-prompt propagation."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent", working_dir=tmp_path / "test",
                            capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "spawn",
            "input": {"name": "helper"},
            "_reasoning": "Investigate the heartbeat regression and report back via mail",
        })
        assert result["status"] == "ok"
        assert result["agent_name"] == "helper"
        prompt_path = parent._working_dir.parent / "helper" / ".prompt"
        assert "Investigate the heartbeat regression" in prompt_path.read_text(encoding="utf-8")

    def test_rules_action_is_unknown_regardless_of_admin(self, tmp_path):
        """action='rules' no longer exists — it fails for admin and non-admin alike."""
        from lingtai.agent import Agent
        no_admin = Agent(service=make_mock_service(), agent_name="worker",
                          working_dir=tmp_path / "worker", capabilities=["avatar"], admin={})
        mgr = no_admin.get_capability("avatar")
        result = mgr.handle({"action": "rules", "input": {"rules_content": "No deleting."}})
        assert "error" in result
        assert not (no_admin._working_dir / ".rules").exists()

        admin = Agent(service=make_mock_service(), agent_name="admin",
                       working_dir=tmp_path / "admin", capabilities=["avatar"],
                       admin={"karma": True})
        mgr2 = admin.get_capability("avatar")
        admin_result = mgr2.handle({"action": "rules", "input": {"rules_content": "Be concise."}})
        assert admin_result == {
            "error": (
                "unknown action: 'rules', only 'spawn', 'settings', "
                "or 'manual' is supported"
            ),
        }
        assert not (admin._working_dir / ".rules").exists()

    def test_spawn_never_required_admin(self, tmp_path, fake_avatar_launch):
        """A non-admin agent can spawn — unaffected by the rules action's removal."""
        from lingtai.agent import Agent
        no_admin = Agent(service=make_mock_service(), agent_name="worker",
                          working_dir=tmp_path / "worker", capabilities=["avatar"], admin={})
        mgr = no_admin.get_capability("avatar")
        result = mgr.handle({"action": "spawn", "input": {"name": "helper", "confirm": True}})
        assert result["status"] == "ok"

    def test_manual_returns_exact_body_and_performs_no_mutation(self, tmp_path):
        """action='manual' is read-only: returns the exact SKILL.md body, no fs mutation."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent",
                        working_dir=tmp_path / "test", capabilities=["avatar"])
        mgr = parent.get_capability("avatar")

        manual_source = (
            Path(__file__).resolve().parents[1]
            / "src" / "lingtai" / "tools" / "avatar" / "manual" / "SKILL.md"
        ).read_text(encoding="utf-8")

        result = mgr.handle({"action": "manual", "input": {}})
        assert result["status"] == "ok"
        assert result["manual"] == manual_source

        # No spawn side effects: no sibling directory, no ledger.
        assert not (parent._working_dir.parent / "helper").exists()
        assert not (parent._working_dir / "delegates" / "ledger.jsonl").exists()
        # No rules side effects: no .rules signal written.
        assert not (parent._working_dir / ".rules").exists()

    def test_daemon_excludes_avatar_from_child_surface(self, tmp_path):
        """The daemon's emanation blacklist covers the canonical 'avatar' name only."""
        from lingtai.tools.daemon import EMANATION_BLACKLIST
        assert "avatar" in EMANATION_BLACKLIST
        assert "avatar_spawn" not in EMANATION_BLACKLIST
        assert "avatar_rules" not in EMANATION_BLACKLIST

    def test_invalid_action_fails_deterministically(self, tmp_path):
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent",
                        working_dir=tmp_path / "test", capabilities=["avatar"])
        mgr = parent.get_capability("avatar")
        result = mgr.handle({"action": "bogus", "input": {}})
        assert "error" in result
        assert "bogus" in result["error"]

    def test_missing_action_fails_deterministically_regardless_of_payload_shape(
        self, tmp_path, fake_avatar_launch,
    ):
        """Missing 'action' must fail the same way no matter which action's
        fields happen to be present — it must never be inferred from payload
        shape, and must mutate nothing (no spawn, no ledger, no .rules)."""
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent",
                        working_dir=tmp_path / "test", capabilities=["avatar"],
                        admin={"karma": True})
        mgr = parent.get_capability("avatar")
        expected_error = (
            "unknown action: '', only 'spawn', 'settings', "
            "or 'manual' is supported"
        )

        # Payload shaped like a formerly-valid rules call, but action omitted.
        rules_shaped = mgr.handle({"rules_content": "Be concise."})
        assert rules_shaped["error"] == expected_error
        assert rules_shaped.get("status") != "ok"
        assert not (parent._working_dir / ".rules").exists()

        # Payload shaped like a valid spawn call, but action omitted.
        spawn_shaped = mgr.handle({"name": "helper3", "confirm": True})
        assert spawn_shaped["error"] == expected_error
        assert spawn_shaped.get("status") != "ok"
        assert not (parent._working_dir.parent / "helper3").exists()
        assert not (parent._working_dir / "delegates" / "ledger.jsonl").exists()

        # Entirely empty payload.
        empty = mgr.handle({})
        assert empty["error"] == expected_error
        assert empty.get("status") != "ok"
        fake_avatar_launch.poll.assert_not_called()

    def test_spawn_missing_name_fails_without_affecting_other_actions(self, tmp_path):
        from lingtai.agent import Agent
        parent = Agent(service=make_mock_service(), agent_name="parent",
                        working_dir=tmp_path / "test", capabilities=["avatar"],
                        admin={"karma": True})
        mgr = parent.get_capability("avatar")

        spawn_result = mgr.handle({"action": "spawn", "input": {}})
        assert "error" in spawn_result
        assert "name is required" in spawn_result["error"]

        manual_result = mgr.handle({"action": "manual", "input": {}})
        assert manual_result["status"] == "ok"
