"""Tests for the past-self consultation infrastructure in intrinsics/soul.py.

Covers the mechanical scaffold landed alongside the appendix tool-call-pair
design. Does NOT exercise live LLM calls — those are mocked. Production cue
prompt is deferred and tested separately.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.intrinsics.soul import (
    _clone_current_chat_for_insights,
    _fit_interface_to_window,
    _list_snapshot_paths,
    _load_snapshot_interface,
    _run_consultation_batch,
    build_consultation_pair,
)
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConfig:
    language = "en"
    consultation_past_count = 2
    context_limit = 200_000  # consulted by _run_consultation when no live chat is attached
    retry_timeout = 1.0
    model = None


class _FakeAgent:
    """Minimal stand-in for BaseAgent — exposes just the attributes the
    consultation helpers read."""

    def __init__(self, tmp_path: Path, with_chat: bool = True):
        self._working_dir = tmp_path
        self._working_dir.mkdir(parents=True, exist_ok=True)
        self._config = _FakeConfig()
        self.service = MagicMock()
        self.service.model = "test-model"
        self._chat = None
        if with_chat:
            iface = ChatInterface()
            iface.add_system("test sys")
            iface.add_user_message("user said something")
            iface.add_assistant_message([
                ThinkingBlock(text="thinking it through"),
                TextBlock(text="agent reply"),
            ])
            mock_chat = MagicMock()
            mock_chat.interface = iface
            self._chat = mock_chat
        self.logged: list[tuple[str, dict]] = []

    def _log(self, event: str, **kw) -> None:
        self.logged.append((event, kw))


def _write_snapshot(workdir: Path, *, molt_count: int, unix_ts: int,
                    entries: list[dict] | None = None) -> Path:
    """Write a snapshot file in the same shape as
    psyche._write_molt_snapshot produces."""
    snaps = workdir / "history" / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    if entries is None:
        # Build a minimal valid interface with a system + a user turn.
        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("frozen user message")
        iface.add_assistant_message([TextBlock(text="frozen reply")])
        entries = iface.to_dict()
    payload = {
        "schema_version": 1,
        "molt_count": molt_count,
        "created_at": "2026-05-01T00:00:00Z",
        "before_tokens": 12345,
        "agent_name": "test-agent",
        "agent_id": "test-id",
        "molt_summary": "test molt",
        "molt_source": "agent",
        "interface": entries,
    }
    path = snaps / f"snapshot_{molt_count}_{unix_ts}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _load_snapshot_interface
# ---------------------------------------------------------------------------


class TestLoadSnapshotInterface:

    def test_loads_valid_snapshot(self, tmp_path):
        path = _write_snapshot(tmp_path, molt_count=3, unix_ts=1714567890)
        iface = _load_snapshot_interface(path)
        assert iface is not None
        assert len(iface.entries) > 0

    def test_missing_file_returns_none(self, tmp_path):
        bogus = tmp_path / "nope.json"
        assert _load_snapshot_interface(bogus) is None

    def test_bad_json_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert _load_snapshot_interface(path) is None

    def test_missing_schema_version_returns_none(self, tmp_path):
        path = tmp_path / "noschema.json"
        path.write_text(json.dumps({"interface": []}), encoding="utf-8")
        assert _load_snapshot_interface(path) is None

    def test_non_int_schema_returns_none(self, tmp_path):
        path = tmp_path / "wrongschema.json"
        path.write_text(
            json.dumps({"schema_version": "1", "interface": []}),
            encoding="utf-8",
        )
        assert _load_snapshot_interface(path) is None

    def test_non_list_interface_returns_none(self, tmp_path):
        path = tmp_path / "wrongiface.json"
        path.write_text(
            json.dumps({"schema_version": 1, "interface": {"oops": True}}),
            encoding="utf-8",
        )
        assert _load_snapshot_interface(path) is None

    def test_payload_not_dict_returns_none(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert _load_snapshot_interface(path) is None


# ---------------------------------------------------------------------------
# _fit_interface_to_window
# ---------------------------------------------------------------------------


class TestFitInterfaceToWindow:

    def test_already_fits_returns_clone(self):
        iface = ChatInterface()
        iface.add_system("sys")
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="ok")])
        out = _fit_interface_to_window(iface, 1_000_000)
        # Same content, but distinct object (clone via to_dict round-trip).
        assert len(out.entries) == len(iface.entries)
        assert out is not iface
        # Mutating the clone must not affect the source.
        out._entries.clear()
        assert len(iface.entries) == 3

    def test_zero_target_returns_empty(self):
        iface = ChatInterface()
        iface.add_user_message("hi")
        out = _fit_interface_to_window(iface, 0)
        assert len(out.entries) == 0

    def test_negative_target_returns_empty(self):
        iface = ChatInterface()
        iface.add_user_message("hi")
        out = _fit_interface_to_window(iface, -100)
        assert len(out.entries) == 0

    def test_empty_interface_returns_empty(self):
        iface = ChatInterface()
        out = _fit_interface_to_window(iface, 1000)
        assert len(out.entries) == 0

    def test_preserves_system_at_head(self):
        iface = ChatInterface()
        iface.add_system("frozen sys prompt to preserve")
        # Add a long body that forces trimming.
        for i in range(20):
            iface.add_user_message(f"user {i} " * 200)
            iface.add_assistant_message([TextBlock(text=f"reply {i} " * 200)])
        # Aim at small target so most of body must be dropped.
        out = _fit_interface_to_window(iface, 5_000)
        # System entry preserved at position 0.
        assert out.entries[0].role == "system"

    def test_drops_orphan_tool_results_at_head(self):
        """If the natural cutoff lands on a user{tool_result} whose matching
        assistant{tool_call} got dropped, the orphan is removed too."""
        iface = ChatInterface()
        iface.add_user_message("setup")
        iface.add_assistant_message([
            ToolCallBlock(id="tc_orphan", name="dropped", args={}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_orphan", name="dropped", content="x"),
        ])
        # Add a clean tail entry that fits a small budget by itself.
        iface.add_assistant_message([TextBlock(text="tail thought")])

        # Compute the rough size of the tail-only kept set, then pick a
        # target that keeps tail but excludes the tool_call entry. The
        # function must not return the tool_result entry as the head of
        # the suffix (orphaned).
        tail_only = ChatInterface()
        tail_only.add_assistant_message([TextBlock(text="tail thought")])
        tail_size = tail_only.estimate_context_tokens()

        out = _fit_interface_to_window(iface, tail_size + 5)

        # No entry in `out` should be a user with only ToolResultBlocks
        # whose call_id was excluded from `out`.
        present_call_ids: set[str] = set()
        for e in out.entries:
            for b in e.content:
                if isinstance(b, ToolCallBlock):
                    present_call_ids.add(b.id)
        for e in out.entries:
            if e.role != "user":
                continue
            if not e.content:
                continue
            if all(isinstance(b, ToolResultBlock) for b in e.content):
                for b in e.content:
                    assert b.id in present_call_ids, (
                        "orphan tool_result kept without matching tool_call"
                    )


# ---------------------------------------------------------------------------
# _list_snapshot_paths
# ---------------------------------------------------------------------------


class TestListSnapshotPaths:

    def test_no_dir_returns_empty(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        assert _list_snapshot_paths(agent) == []

    def test_lists_snapshots(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        _write_snapshot(tmp_path, molt_count=1, unix_ts=1)
        _write_snapshot(tmp_path, molt_count=2, unix_ts=2)
        _write_snapshot(tmp_path, molt_count=3, unix_ts=3)
        paths = _list_snapshot_paths(agent)
        assert len(paths) == 3

    def test_ignores_non_snapshot_files(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        _write_snapshot(tmp_path, molt_count=1, unix_ts=1)
        # Drop an unrelated file in the snapshots dir.
        snaps = tmp_path / "history" / "snapshots"
        (snaps / "stray.txt").write_text("ignore me")
        paths = _list_snapshot_paths(agent)
        assert len(paths) == 1
        assert paths[0].name == "snapshot_1_1.json"


# ---------------------------------------------------------------------------
# _clone_current_chat_for_insights
# ---------------------------------------------------------------------------


class TestCloneCurrentChatForInsights:

    def test_strips_system_and_tool_blocks(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        # Augment current chat with a tool pair that should be filtered out.
        iface = agent._chat.interface
        iface.add_assistant_message([
            ToolCallBlock(id="tc1", name="bash", args={"cmd": "ls"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc1", name="bash", content="files"),
        ])
        cloned = _clone_current_chat_for_insights(agent)
        for entry in cloned.entries:
            assert entry.role != "system"
            for block in entry.content:
                assert isinstance(block, (TextBlock, ThinkingBlock))

    def test_no_chat_returns_empty(self, tmp_path):
        agent = _FakeAgent(tmp_path, with_chat=False)
        cloned = _clone_current_chat_for_insights(agent)
        assert len(cloned.entries) == 0

    def test_keeps_thinking_and_text_blocks(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        cloned = _clone_current_chat_for_insights(agent)
        # The fixture seeded an assistant turn with thinking + text.
        assert any(
            isinstance(b, ThinkingBlock)
            for e in cloned.entries
            for b in e.content
        )
        assert any(
            isinstance(b, TextBlock)
            for e in cloned.entries
            for b in e.content
        )


# ---------------------------------------------------------------------------
# _run_consultation_batch
# ---------------------------------------------------------------------------


class TestRunConsultationBatch:

    def test_empty_pool_runs_only_insights(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation"
        ) as mock_run:
            mock_run.return_value = {
                "source": "insights",
                "voice": "the insight voice",
                "thinking": [],
            }
            voices = _run_consultation_batch(agent)
        assert len(voices) == 1
        # Exactly one consultation call: insights.
        assert mock_run.call_count == 1
        sources = [c.kwargs.get("source") or c.args[2] for c in mock_run.call_args_list]
        assert sources == ["insights"]

    def test_with_snapshots_samples_K(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        # Five snapshots — should sample K=2.
        for i in range(5):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation"
        ) as mock_run:
            def fake_run(_agent, _iface, source):
                return {"source": source, "voice": f"v from {source}", "thinking": []}
            mock_run.side_effect = fake_run
            voices = _run_consultation_batch(agent)

        # 1 insights + min(K=2, 5) = 3 work items total.
        assert mock_run.call_count == 3
        # One must be insights; the other two are snapshot:* labels.
        sources = [v["source"] for v in voices]
        assert "insights" in sources
        snapshot_sources = [s for s in sources if s.startswith("snapshot:")]
        assert len(snapshot_sources) == 2

    def test_K_zero_runs_only_insights(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        agent._config.consultation_past_count = 0
        for i in range(3):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation"
        ) as mock_run:
            mock_run.return_value = {"source": "insights", "voice": "v", "thinking": []}
            voices = _run_consultation_batch(agent)

        assert mock_run.call_count == 1
        assert len(voices) == 1
        assert voices[0]["source"] == "insights"

    def test_failed_consultations_filtered(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        for i in range(3):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation"
        ) as mock_run:
            # First call (insights) succeeds, the snapshot calls fail.
            def maybe_fail(_agent, _iface, source):
                if source == "insights":
                    return {"source": "insights", "voice": "ok", "thinking": []}
                return None
            mock_run.side_effect = maybe_fail
            voices = _run_consultation_batch(agent)

        assert len(voices) == 1
        assert voices[0]["source"] == "insights"

    def test_thread_exception_filtered(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        for i in range(2):
            _write_snapshot(tmp_path, molt_count=i + 1, unix_ts=1700000000 + i)

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation"
        ) as mock_run:
            def maybe_raise(_agent, _iface, source):
                if source == "insights":
                    return {"source": "insights", "voice": "ok", "thinking": []}
                raise RuntimeError("boom")
            mock_run.side_effect = maybe_raise
            voices = _run_consultation_batch(agent)

        # Insights survives; raising threads logged and filtered.
        assert len(voices) == 1
        events = [e for e, _ in agent.logged]
        assert "consultation_thread_error" in events

    def test_no_chat_no_snapshots_returns_empty(self, tmp_path):
        agent = _FakeAgent(tmp_path, with_chat=False)
        voices = _run_consultation_batch(agent)
        assert voices == []


# ---------------------------------------------------------------------------
# build_consultation_pair
# ---------------------------------------------------------------------------


class TestBuildConsultationPair:

    def test_pair_carries_appendix_note(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "insights", "voice": "first", "thinking": []}]
        call, result = build_consultation_pair(agent, voices)
        assert "appendix_note" in result.content
        assert isinstance(result.content["appendix_note"], str)
        assert result.content["appendix_note"] != ""

    def test_pair_call_and_result_share_id(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "x", "voice": "y"}]
        call, result = build_consultation_pair(agent, voices)
        assert call.id == result.id

    def test_pair_uses_soul_flow_action(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "x", "voice": "y"}]
        call, result = build_consultation_pair(agent, voices)
        assert call.name == "soul"
        assert call.args == {"action": "flow"}
        assert result.name == "soul"

    def test_voices_array_strips_thinking(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [
            {"source": "a", "voice": "v1", "thinking": ["lots", "of", "thoughts"]},
            {"source": "b", "voice": "v2", "thinking": []},
        ]
        _, result = build_consultation_pair(agent, voices)
        rendered = result.content["voices"]
        assert len(rendered) == 2
        for entry in rendered:
            assert set(entry.keys()) == {"source", "voice"}
            assert "thinking" not in entry

    def test_empty_voice_filtered(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [
            {"source": "a", "voice": "real"},
            {"source": "b", "voice": ""},
            {"source": "c"},  # missing voice
        ]
        _, result = build_consultation_pair(agent, voices)
        sources = [v["source"] for v in result.content["voices"]]
        assert sources == ["a"]

    def test_consecutive_calls_get_distinct_ids(self, tmp_path):
        agent = _FakeAgent(tmp_path)
        voices = [{"source": "x", "voice": "y"}]
        call1, _ = build_consultation_pair(agent, voices)
        time.sleep(0.001)
        call2, _ = build_consultation_pair(agent, voices)
        assert call1.id != call2.id


# ---------------------------------------------------------------------------
# BaseAgent: _maybe_fire_consultation, _run_consultation_fire,
# _rehydrate_appendix_tracking
# ---------------------------------------------------------------------------


class TestMaybeFireConsultation:

    def _make_real_agent(self, tmp_path, *, interval: int):
        """Build a real BaseAgent so we exercise the actual cadence path."""
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        agent._config.consultation_interval = interval
        return agent

    def test_zero_interval_disables(self, tmp_path):
        agent = self._make_real_agent(tmp_path, interval=0)
        agent._maybe_fire_consultation()
        agent._maybe_fire_consultation()
        agent._maybe_fire_consultation()
        # Counter never moved.
        assert agent._consultation_turn_counter == 0

    def test_negative_interval_disables(self, tmp_path):
        agent = self._make_real_agent(tmp_path, interval=-1)
        agent._maybe_fire_consultation()
        assert agent._consultation_turn_counter == 0

    def test_fires_every_n_turns(self, tmp_path):
        agent = self._make_real_agent(tmp_path, interval=3)
        with patch.object(agent, "_run_consultation_fire") as mock_fire:
            for _ in range(8):
                agent._maybe_fire_consultation()
        # interval=3 → fires on turns 3 and 6 → 2 fires
        # (each fire spawns a daemon thread that calls _run_consultation_fire)
        # Wait briefly for threads to call the method.
        import threading
        for t in threading.enumerate():
            if t.name.startswith("consult-"):
                t.join(timeout=2.0)
        assert mock_fire.call_count == 2

    def test_does_not_fire_on_first_turn(self, tmp_path):
        agent = self._make_real_agent(tmp_path, interval=5)
        with patch.object(agent, "_run_consultation_fire") as mock_fire:
            agent._maybe_fire_consultation()
        # First call: counter goes to 1, no fire.
        assert agent._consultation_turn_counter == 1
        assert mock_fire.call_count == 0


class TestRunConsultationFire:

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def test_empty_voices_is_noop(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=[],
        ):
            agent._run_consultation_fire()
        assert len(agent._tc_inbox) == 0

    def test_voices_enqueue_replace_in_history_item(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=[{"source": "insights", "voice": "hello"}],
        ):
            agent._run_consultation_fire()
        assert len(agent._tc_inbox) == 1
        items = agent._tc_inbox.drain()
        assert items[0].source == "soul.flow"
        assert items[0].coalesce is True
        assert items[0].replace_in_history is True

    def test_exception_swallowed_and_logged(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        agent.logged = []
        original_log = agent._log

        def capture_log(event, **kw):
            agent.logged.append((event, kw))
            return original_log(event, **kw)
        agent._log = capture_log

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            side_effect=RuntimeError("boom"),
        ):
            agent._run_consultation_fire()  # should not raise
        events = [e for e, _ in agent.logged]
        assert "consultation_fire_error" in events


class TestRehydrateAppendixTracking:

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def test_no_chat_is_noop(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        agent._chat = None
        agent._rehydrate_appendix_tracking()
        assert agent._appendix_ids_by_source == {}

    def test_finds_existing_pair(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        # Inject a chat history containing a soul.flow pair.
        iface = ChatInterface()
        iface.add_user_message("user")
        iface.add_assistant_message([TextBlock(text="reply")])
        iface.add_assistant_message([
            ToolCallBlock(id="tc_recover_me", name="soul",
                          args={"action": "flow"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_recover_me", name="soul",
                            content={"voices": []}),
        ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert agent._appendix_ids_by_source.get("soul.flow") == "tc_recover_me"

    def test_ignores_non_soul_pairs(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        iface = ChatInterface()
        iface.add_user_message("user")
        iface.add_assistant_message([
            ToolCallBlock(id="tc_other", name="bash", args={"cmd": "ls"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_other", name="bash", content="file"),
        ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert "soul.flow" not in agent._appendix_ids_by_source

    def test_ignores_inquiry_action(self, tmp_path):
        """A soul(action='inquiry') pair would only ever appear via the
        synchronous inquiry path which doesn't go through tc_inbox; defensive
        check that we don't track it as a flow appendix."""
        agent = self._make_real_agent(tmp_path)
        iface = ChatInterface()
        iface.add_user_message("user")
        iface.add_assistant_message([
            ToolCallBlock(id="tc_inq", name="soul",
                          args={"action": "inquiry"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_inq", name="soul", content={"voice": "x"}),
        ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert "soul.flow" not in agent._appendix_ids_by_source

    def test_tracks_first_match_only(self, tmp_path):
        """Defensive: if somehow the history contains two soul.flow pairs
        (shouldn't happen post-design but tolerate it), only the first
        match is tracked. Caller can clean up subsequent matches manually."""
        agent = self._make_real_agent(tmp_path)
        iface = ChatInterface()
        for tc_id in ["tc_first", "tc_second"]:
            iface.add_assistant_message([
                ToolCallBlock(id=tc_id, name="soul",
                              args={"action": "flow"}),
            ])
            iface.add_tool_results([
                ToolResultBlock(id=tc_id, name="soul",
                                content={"voices": []}),
            ])
        mock_chat = MagicMock()
        mock_chat.interface = iface
        agent._chat = mock_chat

        agent._rehydrate_appendix_tracking()
        assert agent._appendix_ids_by_source["soul.flow"] == "tc_first"
