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


class TestPostLlmCallHook:
    """The LLM-call hot path must invoke _post_llm_call after every
    successful response so the turn-count cadence ticks correctly."""

    def _make_real_agent(self, tmp_path, *, interval: int):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        agent._config.consultation_interval = interval
        return agent

    def test_post_llm_call_invokes_maybe_fire(self, tmp_path):
        agent = self._make_real_agent(tmp_path, interval=10)
        with patch.object(agent, "_maybe_fire_consultation") as m:
            agent._post_llm_call()
        assert m.call_count == 1

    def test_post_llm_call_swallows_exceptions(self, tmp_path):
        agent = self._make_real_agent(tmp_path, interval=10)
        agent.logged = []
        original_log = agent._log
        def capture_log(event, **kw):
            agent.logged.append((event, kw))
            return original_log(event, **kw)
        agent._log = capture_log

        with patch.object(
            agent, "_maybe_fire_consultation",
            side_effect=RuntimeError("boom"),
        ):
            agent._post_llm_call()  # must not raise
        events = [e for e, _ in agent.logged]
        assert "post_llm_call_error" in events

    def test_default_interval_is_ten(self):
        from lingtai_kernel.config import AgentConfig
        assert AgentConfig().consultation_interval == 10

    def test_default_past_count_is_two(self):
        from lingtai_kernel.config import AgentConfig
        assert AgentConfig().consultation_past_count == 2


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


# ---------------------------------------------------------------------------
# soul_flow.jsonl schema (schema_version=2) — fire + voice records
# ---------------------------------------------------------------------------


class TestSoulFlowPersistenceSchema:
    """End-to-end: drive _run_consultation_fire with a mocked batch, inspect
    the on-disk records in logs/soul_flow.jsonl."""

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def _read_records(self, agent) -> list[dict]:
        path = agent._working_dir / "logs" / "soul_flow.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _seed_diary(self, agent, *texts: str) -> None:
        log_dir = agent._working_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w", encoding="utf-8") as f:
            for t in texts:
                f.write(json.dumps({"type": "diary", "text": t}) + "\n")

    def test_writes_fire_and_voice_records_with_linked_fire_id(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "did X", "noticed Y")

        voices = [
            {"source": "insights", "voice": "step back: Z",
             "thinking": [{"text": "considering"}]},
            {"source": "snapshot:snapshot_3_1735", "voice": "I tried that once",
             "thinking": []},
        ]
        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=voices,
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        assert len(records) == 3, f"expected 1 fire + 2 voices, got {len(records)}"

        fires = [r for r in records if r["kind"] == "fire"]
        voice_recs = [r for r in records if r["kind"] == "voice"]
        assert len(fires) == 1
        assert len(voice_recs) == 2

        fire = fires[0]
        assert fire["schema_version"] == 2
        assert fire["fire_id"].startswith("fire_")
        assert fire["tc_id"] == fire["fire_id"]
        assert fire["outcome"] == "ok"
        assert "did X" in fire["diary"] and "noticed Y" in fire["diary"]
        assert set(fire["sources"]) == {"insights", "snapshot:snapshot_3_1735"}
        assert "ts" in fire and fire["ts"].endswith("Z")

        # All voice records link back to the same fire_id.
        for v in voice_recs:
            assert v["fire_id"] == fire["fire_id"]
            assert v["schema_version"] == 2
            assert "ts" in v and v["ts"].endswith("Z")
            assert v["consultation_kind"] in ("insights", "past")

        # consultation_kind matches source type.
        by_src = {v["source"]: v for v in voice_recs}
        assert by_src["insights"]["consultation_kind"] == "insights"
        assert by_src["insights"]["voice"] == "step back: Z"
        assert by_src["insights"]["thinking"] == [{"text": "considering"}]
        assert by_src["snapshot:snapshot_3_1735"]["consultation_kind"] == "past"
        assert by_src["snapshot:snapshot_3_1735"]["thinking"] == []

    def test_empty_fire_still_writes_fire_record_with_empty_outcome(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "stuck thinking")

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=[],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        assert len(records) == 1
        fire = records[0]
        assert fire["kind"] == "fire"
        assert fire["outcome"] == "empty"
        assert fire["sources"] == []
        # Diary still captured even when no voices came back.
        assert "stuck thinking" in fire["diary"]
        # No tc_inbox enqueue happens on empty fires.
        assert len(agent._tc_inbox) == 0

    def test_synthetic_pair_call_id_matches_fire_id(self, tmp_path):
        """The chat-side call_id and the soul_flow.jsonl fire_id are the
        same string — that's what makes cross-referencing trivial."""
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "diary text")

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=[{"source": "insights", "voice": "v", "thinking": []}],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        fire = next(r for r in records if r["kind"] == "fire")

        # The pair landed on tc_inbox; pull it out and check its call.id
        assert len(agent._tc_inbox) == 1
        items = agent._tc_inbox.drain()
        assert items[0].call.id == fire["fire_id"]
        assert items[0].result.id == fire["fire_id"]

    def test_fire_record_written_even_on_exception(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "before crash")

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            side_effect=RuntimeError("boom from batch"),
        ):
            agent._run_consultation_fire()  # must not raise

        records = self._read_records(agent)
        assert len(records) == 1
        fire = records[0]
        assert fire["kind"] == "fire"
        assert fire["outcome"] == "error"
        assert "boom from batch" in fire["error"]
        # No voices on a hard-crash fire.
        assert "fire_id" in fire

    def test_diary_empty_still_recorded(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        # No events.jsonl — diary will be empty string.

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=[{"source": "insights", "voice": "v", "thinking": []}],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        fire = next(r for r in records if r["kind"] == "fire")
        assert fire["diary"] == ""
        # Voice record still produced.
        voices = [r for r in records if r["kind"] == "voice"]
        assert len(voices) == 1

    def test_appends_across_multiple_fires(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        self._seed_diary(agent, "d1")

        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=[{"source": "insights", "voice": "v1", "thinking": []}],
        ):
            agent._run_consultation_fire()
            # Drain so the second fire doesn't coalesce in tc_inbox terms
            # (it would still write its own log records regardless).
            agent._tc_inbox.drain()
        with patch(
            "lingtai_kernel.intrinsics.soul._run_consultation_batch",
            return_value=[{"source": "snapshot:s1", "voice": "v2", "thinking": []}],
        ):
            agent._run_consultation_fire()

        records = self._read_records(agent)
        # 2 fires + 2 voices.
        assert len(records) == 4
        fires = [r for r in records if r["kind"] == "fire"]
        assert len(fires) == 2
        # Each fire gets a distinct id.
        assert fires[0]["fire_id"] != fires[1]["fire_id"]


class TestPersistSoulEntryUnchanged:
    """Inquiry path still uses the legacy schema — make sure we didn't break it."""

    def _make_real_agent(self, tmp_path):
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc,
            agent_name="t",
            working_dir=tmp_path / "agent",
        )
        return agent

    def test_inquiry_persistence_writes_legacy_shape(self, tmp_path):
        agent = self._make_real_agent(tmp_path)
        agent._persist_soul_entry(
            {"prompt": "what should I do?", "voice": "rest", "thinking": []},
            mode="inquiry",
            source="agent",
        )
        path = agent._working_dir / "logs" / "soul_inquiry.jsonl"
        assert path.is_file()
        rec = json.loads(path.read_text().strip())
        assert rec["mode"] == "inquiry"
        assert rec["source"] == "agent"
        assert rec["prompt"] == "what should I do?"
        assert rec["voice"] == "rest"
        assert rec["thinking"] == []
        assert "ts" in rec
        # Legacy shape: no kind/schema_version/fire_id fields.
        assert "kind" not in rec
        assert "schema_version" not in rec


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

    def test_soul_whisper_delegates_to_consultation_fire(self, tmp_path):
        """The wall-clock soul timer (driven by config.soul_delay) now fires
        past-self consultation instead of the legacy diary+mirror-session
        flow. Verifies _soul_whisper -> _run_consultation_fire wiring."""
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc, agent_name="t", working_dir=tmp_path / "agent",
        )
        with patch.object(agent, "_run_consultation_fire") as mock_fire, \
             patch.object(agent, "_start_soul_timer") as mock_resched:
            agent._soul_whisper()
        assert mock_fire.call_count == 1
        assert mock_resched.call_count == 1

    def test_soul_whisper_swallows_consultation_fire_error(self, tmp_path):
        """Errors in the consultation fire must not break the cadence —
        the timer reschedules itself in finally regardless."""
        from lingtai_kernel import BaseAgent
        svc = MagicMock(); svc.model = "test-model"
        agent = BaseAgent(
            service=svc, agent_name="t", working_dir=tmp_path / "agent",
        )
        with patch.object(agent, "_run_consultation_fire",
                          side_effect=RuntimeError("boom")), \
             patch.object(agent, "_start_soul_timer") as mock_resched:
            agent._soul_whisper()  # must not raise
        assert mock_resched.call_count == 1

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


# ---------------------------------------------------------------------------
# _render_current_diary — concatenate diary entries from events.jsonl
# ---------------------------------------------------------------------------


class TestRenderCurrentDiary:

    def _write_events(self, workdir: Path, records: list[dict]) -> None:
        log_dir = workdir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "events.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_returns_empty_when_no_log(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        assert _render_current_diary(agent) == ""

    def test_returns_empty_when_log_has_no_diary(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "boot", "ts": 1},
            {"type": "tool_call", "name": "psyche"},
        ])
        assert _render_current_diary(agent) == ""

    def test_concatenates_diary_entries_in_order(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "diary", "text": "first turn thoughts"},
            {"type": "boot", "ts": 1},
            {"type": "diary", "text": "second turn thoughts"},
            {"type": "diary", "text": "third turn thoughts"},
        ])
        out = _render_current_diary(agent)
        assert "first turn thoughts" in out
        assert "second turn thoughts" in out
        assert "third turn thoughts" in out
        # Order preserved, with paragraph break separator.
        assert out.index("first") < out.index("second") < out.index("third")
        assert "\n\n" in out

    def test_skips_blank_and_non_string_text(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        agent = _FakeAgent(tmp_path, with_chat=False)
        self._write_events(tmp_path, [
            {"type": "diary", "text": "valid"},
            {"type": "diary", "text": "   "},   # whitespace only — skip
            {"type": "diary", "text": None},     # not a string — skip
            {"type": "diary"},                    # missing text — skip
            {"type": "diary", "text": "second valid"},
        ])
        out = _render_current_diary(agent)
        assert "valid" in out
        assert "second valid" in out
        # Whitespace-only entry should not contribute its blanks
        assert out.count("\n\n") == 1   # only one separator between two entries

    def test_tolerates_malformed_lines(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _render_current_diary
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w", encoding="utf-8") as f:
            f.write('{"type": "diary", "text": "good"}\n')
            f.write("not json at all\n")
            f.write("\n")
            f.write('{"type": "diary", "text": "still good"}\n')
        agent = _FakeAgent(tmp_path, with_chat=False)
        out = _render_current_diary(agent)
        assert "good" in out
        assert "still good" in out


# ---------------------------------------------------------------------------
# _load_snapshot_interface tool stripping — past selves see no tools
# ---------------------------------------------------------------------------


class TestSnapshotToolStripping:

    def test_strips_tool_call_and_tool_result_blocks(self, tmp_path):
        # Build a snapshot with mixed content: text, thinking, tool_call,
        # tool_result. After load, only text+thinking should survive.
        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("a question from user")
        iface.add_assistant_message([
            ThinkingBlock(text="reasoning"),
            TextBlock(text="I'll call a tool"),
            ToolCallBlock(id="tc_1", name="psyche", args={"action": "show"}),
        ])
        iface.add_tool_results([
            ToolResultBlock(id="tc_1", name="psyche", content="result"),
        ])
        iface.add_assistant_message([TextBlock(text="final answer")])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None

        # Walk every block; should be none of ToolCallBlock or ToolResultBlock.
        all_blocks = []
        for entry in loaded.entries:
            all_blocks.extend(entry.content)
        for block in all_blocks:
            assert not isinstance(block, ToolCallBlock), \
                f"ToolCallBlock leaked through strip: {block}"
            assert not isinstance(block, ToolResultBlock), \
                f"ToolResultBlock leaked through strip: {block}"

    def test_drops_entries_that_empty_after_strip(self, tmp_path):
        # User entry that is purely a tool_result has nothing left after
        # stripping — it must be dropped, not kept as an empty user entry
        # (which would be a malformed wire shape).
        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("question")
        iface.add_assistant_message([
            TextBlock(text="calling"),
            ToolCallBlock(id="tc_1", name="psyche", args={}),
        ])
        # User entry that's only a tool_result — strip it down to nothing
        iface.add_tool_results([
            ToolResultBlock(id="tc_1", name="psyche", content="data"),
        ])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None

        # No empty entries should remain.
        for entry in loaded.entries:
            assert len(entry.content) > 0, f"empty entry survived: {entry}"

    def test_preserves_system_entry(self, tmp_path):
        iface = ChatInterface()
        iface.add_system("THE FROZEN SYSTEM PROMPT")
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello back")])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None
        sys_entries = [e for e in loaded.entries if e.role == "system"]
        assert len(sys_entries) == 1
        assert sys_entries[0].content[0].text == "THE FROZEN SYSTEM PROMPT"

    def test_keeps_thinking_blocks(self, tmp_path):
        iface = ChatInterface()
        iface.add_system("sys")
        iface.add_user_message("question")
        iface.add_assistant_message([
            ThinkingBlock(text="careful reasoning"),
            TextBlock(text="answer"),
        ])
        path = _write_snapshot(tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict())

        loaded = _load_snapshot_interface(path)
        assert loaded is not None
        all_blocks = [b for e in loaded.entries for b in e.content]
        assert any(isinstance(b, ThinkingBlock) for b in all_blocks)

    def test_strips_tool_schema_list_from_system_entry(self, tmp_path):
        # Past self had a real tool schema list bound to its system entry.
        # After thaw, the system entry's text must survive verbatim, but
        # both the entry-level _tools and the rebuilt interface's
        # _current_tools must be None — otherwise an adapter could pick
        # them up and re-emit the past life's tools on the consultation
        # wire payload.
        frozen_tools = [
            {
                "name": "psyche",
                "description": "molt yourself",
                "input_schema": {"type": "object"},
            },
            {
                "name": "soul",
                "description": "inner voice",
                "input_schema": {"type": "object"},
            },
        ]
        iface = ChatInterface()
        iface.add_system("FROZEN PROMPT WITH TOOL PROSE", tools=frozen_tools)
        iface.add_user_message("hi")
        iface.add_assistant_message([TextBlock(text="hello")])

        # Sanity: the source interface really did carry tools.
        assert iface.current_tools == frozen_tools
        sys_src = next(e for e in iface.entries if e.role == "system")
        assert sys_src._tools == frozen_tools

        path = _write_snapshot(
            tmp_path, molt_count=1, unix_ts=1, entries=iface.to_dict()
        )
        loaded = _load_snapshot_interface(path)
        assert loaded is not None

        # System text is preserved verbatim — that's the past self's
        # frozen identity / job description.
        sys_loaded = next(e for e in loaded.entries if e.role == "system")
        assert sys_loaded.content[0].text == "FROZEN PROMPT WITH TOOL PROSE"

        # But both the entry-level schema list and the interface-level
        # current_tools must be wiped.
        assert sys_loaded._tools is None, \
            "frozen tool schema list leaked through snapshot thaw"
        assert loaded.current_tools is None, \
            "ChatInterface._current_tools leaked through snapshot thaw"


# ---------------------------------------------------------------------------
# _kind_for_source / _build_consultation_cue / dispatch
# ---------------------------------------------------------------------------


class TestKindDispatch:

    def test_insights_source_maps_to_insights_kind(self):
        from lingtai_kernel.intrinsics.soul import _kind_for_source
        assert _kind_for_source("insights") == "insights"

    def test_snapshot_source_maps_to_past_kind(self):
        from lingtai_kernel.intrinsics.soul import _kind_for_source
        assert _kind_for_source("snapshot:snapshot_3_1735") == "past"

    def test_other_source_maps_to_past(self):
        from lingtai_kernel.intrinsics.soul import _kind_for_source
        # Defaults to past for unknown labels — past is the more general
        # frame and the safer default.
        assert _kind_for_source("anything else") == "past"


class TestBuildConsultationCue:

    def test_insights_cue_includes_diary(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        cue = _build_consultation_cue(agent, "insights", "I built X today.")
        assert "I built X today." in cue
        # Insights cue should not frame as "your future self"
        assert "future self" not in cue.lower()

    def test_past_cue_includes_diary_and_future_self_frame(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        cue = _build_consultation_cue(agent, "past", "I built X today.")
        assert "I built X today." in cue
        assert "future self" in cue.lower()

    def test_empty_diary_uses_placeholder(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        cue = _build_consultation_cue(agent, "past", "")
        assert "no diary yet" in cue

    def test_zh_cue_renders(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        agent._config.language = "zh"
        cue = _build_consultation_cue(agent, "past", "今日做了 X。")
        assert "今日做了 X。" in cue
        assert "未来" in cue   # zh-localized "future self" framing

    def test_wen_cue_renders(self, tmp_path):
        from lingtai_kernel.intrinsics.soul import _build_consultation_cue
        agent = _FakeAgent(tmp_path, with_chat=False)
        agent._config.language = "wen"
        cue = _build_consultation_cue(agent, "past", "今日造 X。")
        assert "今日造 X。" in cue


class TestRunConsultationDispatchesByKind:
    """Confirms _run_consultation picks the right system prompt and cue
    based on the source label. Mocks the LLM session so we can read what
    was sent."""

    def _run(self, tmp_path, source: str):
        from lingtai_kernel.intrinsics.soul import _run_consultation

        agent = _FakeAgent(tmp_path)
        # Seed a tiny diary
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "events.jsonl", "w") as f:
            f.write(json.dumps({"type": "diary", "text": "DIARY MARKER"}) + "\n")

        captured = {}

        class _MockResponse:
            text = "voice text"
            thoughts = []
            class _Usage:
                input_tokens = 0
                output_tokens = 0
                thinking_tokens = 0
                cached_tokens = 0
            usage = _Usage()

        class _MockSession:
            def send(self, content):
                captured["sent_content"] = content
                return _MockResponse()

        def _create_session(*, system_prompt, **kw):
            captured["system_prompt"] = system_prompt
            return _MockSession()

        agent.service.create_session.side_effect = _create_session

        iface = ChatInterface()
        iface.add_system("frozen sys")
        iface.add_user_message("frozen user")
        iface.add_assistant_message([TextBlock(text="frozen reply")])

        result = _run_consultation(agent, iface, source)
        return captured, result

    def test_past_dispatch_uses_past_prompt_and_cue(self, tmp_path):
        captured, result = self._run(tmp_path, "snapshot:snapshot_3_1735")
        assert result is not None
        assert "DIARY MARKER" in captured["sent_content"]
        # past system prompt should mention molt/past life
        assert "past life" in captured["system_prompt"].lower()
        # cue should call out future self
        assert "future self" in captured["sent_content"].lower()

    def test_insights_dispatch_uses_insights_prompt(self, tmp_path):
        captured, result = self._run(tmp_path, "insights")
        assert result is not None
        assert "DIARY MARKER" in captured["sent_content"]
        # insights prompt frames as "soul flow voice"
        assert "soul flow" in captured["system_prompt"].lower()
        # insights cue should not frame as future-self letter
        assert "future self" not in captured["sent_content"].lower()
