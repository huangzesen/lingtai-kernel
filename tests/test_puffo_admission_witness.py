"""Puffo admission witness: receipt extraction, binding, and the settle-point
wire-scan that emits the reliable committed fact.

The witness fires when a receipt-bearing tool result is *durably present on the
real wire* at a settle point.  These tests therefore drive the actual
``ChatInterface`` state (present vs. absent entry) rather than any bookkeeping
of "what was passed to send", because the adapter rollback layer sits between
the caller and the wire.
"""
from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

import lingtai.kernel.base_agent.turn as turn_mod
from lingtai.kernel.base_agent.turn import (
    begin_admission_witness_scope,
    end_admission_witness_scope,
    scan_and_emit_committed_facts,
    _process_response,
    _restore_tool_results_after_continuation_failure,
)
from lingtai.kernel.llm.interface import (
    ChatInterface,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai.kernel.loop_guard import LoopGuard
from lingtai.kernel.puffo_admission_witness import (
    admission_binding,
    extract_admission_receipt,
)
from lingtai.kernel.turn_events import (
    ToolResultsCommittedEvent,
    bind_turn_tool_observer,
    notify_tool_results_committed,
    reset_turn_tool_observer,
)
from lingtai.services.mcp import _decode_tool_result


def _marker(raw: str) -> str:
    return f"[puffo:model-visible-read:{raw}]"


# --------------------------------------------------------------------------- #
# 1. Receipt extraction — the three deterministic rules + exclusions.
# --------------------------------------------------------------------------- #


def test_rule1_structured_field_wins_over_decoy_marker_in_text():
    # A dict result carries the real receipt in the dedicated field; a decoy
    # marker is smuggled into a sibling text field. Rule ① consults ONLY the
    # dedicated field.
    block = ToolResultBlock(
        id="tc-1",
        name="puffo_tool",
        content={
            "admission_receipt": _marker("REAL"),
            "text": f"quoted body {_marker('DECOY')}",
        },
    )
    assert extract_admission_receipt(block) == "REAL"


def test_rule1_second_field_name_and_precedence():
    # ``tool_result_admission`` is honoured when ``admission_receipt`` is absent.
    block = ToolResultBlock(
        id="tc-1", name="t",
        content={"tool_result_admission": _marker("VIA_SECOND")},
    )
    assert extract_admission_receipt(block) == "VIA_SECOND"
    # When both are present, ``admission_receipt`` takes precedence.
    both = ToolResultBlock(
        id="tc-1", name="t",
        content={
            "admission_receipt": _marker("FIRST"),
            "tool_result_admission": _marker("SECOND"),
        },
    )
    assert extract_admission_receipt(both) == "FIRST"


def test_rule1_field_not_wellformed_marker_returns_none():
    # The dedicated field value must be EXACTLY a marker (fullmatch). Extra text
    # around it, a non-string value, or garbage => None (fail-closed), and a
    # marker elsewhere is NOT consulted as a fallback.
    for value in [
        f"prefix {_marker('R')}",
        f"{_marker('R')} suffix",
        "[puffo:model-visible-read:]",  # empty raw
        "not a marker",
        123,
        None,
    ]:
        block = ToolResultBlock(
            id="tc-1", name="t",
            content={"admission_receipt": value, "note": _marker("ELSEWHERE")},
        )
        assert extract_admission_receipt(block) is None, value


def test_rule1_dict_without_receipt_field_returns_none():
    # A structured result with no dedicated receipt field carries no fact, even
    # if a marker is buried elsewhere.
    block = ToolResultBlock(
        id="tc-1", name="t",
        content={"output": f"stuff {_marker('BURIED')}"},
    )
    assert extract_admission_receipt(block) is None


def test_rule2_plain_text_takes_last_marker():
    # The genuine receipt is appended at the end; an earlier quoted marker-like
    # string must NOT win.
    content = (
        f"The tool quoted an example {_marker('QUOTED')} in its body.\n"
        f"{_marker('APPENDED')}"
    )
    block = ToolResultBlock(id="tc-1", name="t", content=content)
    assert extract_admission_receipt(block) == "APPENDED"


def test_rule3_absent_or_malformed_marker_returns_none():
    assert extract_admission_receipt(
        ToolResultBlock(id="tc", name="t", content="no marker here")
    ) is None
    assert extract_admission_receipt(
        ToolResultBlock(id="tc", name="t", content="[puffo:model-visible-read:]")
    ) is None
    # raw may not contain whitespace or ']'
    assert extract_admission_receipt(
        ToolResultBlock(id="tc", name="t", content="[puffo:model-visible-read:has space]")
    ) is None
    # non-str / non-dict content
    assert extract_admission_receipt(
        ToolResultBlock(id="tc", name="t", content=["list"])
    ) is None


def test_synthesized_block_is_never_a_witness():
    block = ToolResultBlock(
        id="tc-1", name="t", content=_marker("R"), synthesized=True,
    )
    assert extract_admission_receipt(block) is None
    # Same for a structured synthesized block.
    block2 = ToolResultBlock(
        id="tc-1", name="t",
        content={"admission_receipt": _marker("R")}, synthesized=True,
    )
    assert extract_admission_receipt(block2) is None


def test_read_inbox_style_no_marker_returns_none():
    # read_inbox results carry no receipt marker — the intended explicit
    # exclusion of the inbox read from the witness signal.
    inbox_like = ToolResultBlock(
        id="tc-inbox", name="read_inbox",
        content={"messages": [{"seq": 1, "content": "hi"}], "has_next": False},
    )
    assert extract_admission_receipt(inbox_like) is None


# --------------------------------------------------------------------------- #
# 1a. Extraction against the REAL decoded shape per carrier class.
#
# MCP results pass through services/mcp.py::_decode_tool_result BEFORE reaching
# the kernel/interface. At least one test per carrier class builds the block
# content by actually calling that decoder rather than hand-writing the dict —
# that is the shape production actually produces.
# --------------------------------------------------------------------------- #


def _fake_mcp_result(*, text=None, structured_content=None, is_error=False):
    """Minimal stand-in for an SDK v2 CallToolResult for the decoder."""
    content = [SimpleNamespace(text=text)] if text is not None else []
    return SimpleNamespace(
        content=content,
        structured_content=structured_content,
        is_error=is_error,
    )


def _decoded_block(call_id="tc-1", name="puffo_tool", **result_kwargs):
    decoded = _decode_tool_result(_fake_mcp_result(**result_kwargs))
    return ToolResultBlock(id=call_id, name=name, content=decoded), decoded


def test_decoded_plain_text_tail_marker_extracts():
    # read_history / get_post append the marker to a NON-JSON plain-text tail.
    # _decode_tool_result wraps it as {"status":"success","text": <tail>} — the
    # class that previously produced zero facts.
    block, decoded = _decoded_block(
        text=f"conversation line one\nconversation line two\n{_marker('TXTRAW')}"
    )
    assert decoded["status"] == "success"  # confirm we hit the envelope branch
    assert "admission_receipt" not in decoded
    assert extract_admission_receipt(block) == "TXTRAW"


def test_decoded_structured_content_dict_with_admission_receipt_extracts():
    block, decoded = _decoded_block(
        structured_content={"ok": True, "admission_receipt": _marker("STRUCTRAW")}
    )
    assert isinstance(decoded, dict)
    assert extract_admission_receipt(block) == "STRUCTRAW"


def test_decoded_json_text_dict_with_tool_result_admission_extracts():
    payload = {"result": "sent", "tool_result_admission": _marker("JSONRAW")}
    block, decoded = _decoded_block(text=json.dumps(payload))
    assert isinstance(decoded, dict)
    assert extract_admission_receipt(block) == "JSONRAW"


def test_decoded_json_array_text_returns_none():
    # A JSON ARRAY text decodes to a list, which extraction fail-closes to None.
    # TODO(peter): confirm no Puffo carrier returns an array-with-marker — an
    # array tail carrying a receipt is a silent-drop risk that must be nailed
    # per-tool on the Puffo side (there is no dedicated field on a bare list).
    block, decoded = _decoded_block(text=json.dumps(["item", _marker("ARRRAW")]))
    assert isinstance(decoded, list)
    assert extract_admission_receipt(block) is None


def test_decoded_error_envelope_returns_none():
    # A plain-text error tail decodes to {"status":"error","message": <text>}.
    block, decoded = _decoded_block(
        text=f"boom {_marker('ERRRAW')}", is_error=True,
    )
    assert decoded["status"] == "error"
    assert extract_admission_receipt(block) is None


def test_success_envelope_without_marker_returns_none():
    block, decoded = _decoded_block(text="just some output, no receipt here")
    assert decoded == {"status": "success", "text": "just some output, no receipt here"}
    assert extract_admission_receipt(block) is None


# --------------------------------------------------------------------------- #
# 1b. Binding — stable and matches the exact formula for a known vector.
# --------------------------------------------------------------------------- #


def test_binding_matches_formula_known_vector():
    tc_id = "toolu_abc123"
    raw = "R-42"
    expected = hashlib.sha256(b"toolu_abc123\x00R-42").hexdigest()
    assert admission_binding(tc_id, raw) == expected
    # Stable across calls.
    assert admission_binding(tc_id, raw) == admission_binding(tc_id, raw)
    # The NUL separator makes the split unambiguous: (a, bc) != (ab, c).
    assert admission_binding("a", "bc") != admission_binding("ab", "c")


# --------------------------------------------------------------------------- #
# 2. Event + reliable notify.
# --------------------------------------------------------------------------- #


class _Observer:
    def __init__(self, *, raises: bool = False):
        self.lifecycle = []
        self.committed = []
        self.raises = raises

    def on_tool_lifecycle(self, event):
        self.lifecycle.append(event)

    def on_tool_results_committed(self, event):
        self.committed.append(event)
        if self.raises:
            raise RuntimeError("boom")


class _LifecycleOnlyObserver:
    """A structural observer that does NOT implement the committed method."""

    def __init__(self):
        self.lifecycle = []

    def on_tool_lifecycle(self, event):
        self.lifecycle.append(event)


def test_event_is_metadata_only():
    ev = ToolResultsCommittedEvent("tc-1", "deadbeef")
    assert ev.tool_call_id == "tc-1"
    assert ev.binding == "deadbeef"
    # Metadata only: exactly the id + binding, no args/results fields (matching
    # the module's "arguments and results intentionally absent" safety property).
    assert ToolResultsCommittedEvent.__slots__ == ("tool_call_id", "binding")


def test_notify_returns_delivered_bool():
    ev = ToolResultsCommittedEvent("tc-1", "b")
    # No observer bound.
    assert notify_tool_results_committed(ev) is False

    obs = _Observer()
    token = bind_turn_tool_observer(obs)
    try:
        assert notify_tool_results_committed(ev) is True
        assert obs.committed == [ev]
    finally:
        reset_turn_tool_observer(token)

    raising = _Observer(raises=True)
    token = bind_turn_tool_observer(raising)
    try:
        # Guarded: does not raise into Core, but reports non-delivery.
        assert notify_tool_results_committed(ev) is False
    finally:
        reset_turn_tool_observer(token)

    # Structural observer missing the method: non-delivery, not a crash.
    lifecycle_only = _LifecycleOnlyObserver()
    token = bind_turn_tool_observer(lifecycle_only)
    try:
        assert notify_tool_results_committed(ev) is False
    finally:
        reset_turn_tool_observer(token)


# --------------------------------------------------------------------------- #
# 3. Settle-point wire-scan — the discriminating cases.
# --------------------------------------------------------------------------- #


class _FakeChat:
    def __init__(self, interface):
        self.interface = interface

    def commit_tool_results(self, results):
        self.interface.add_tool_results(results)


class _FakeAgent:
    def __init__(self, interface):
        self._chat = _FakeChat(interface)
        self.logs = []

    def _log(self, event, **kw):
        self.logs.append((event, kw))

    def _save_chat_history(self, **kw):
        pass


def _open_call(iface: ChatInterface, call_id: str, name: str = "puffo_tool"):
    iface.add_assistant_message(content=[ToolCallBlock(id=call_id, name=name, args={})])


def _commit_result(iface, call_id, raw=None, *, synthesized=False, name="puffo_tool"):
    content = _marker(raw) if raw is not None else "plain result, no receipt"
    block = ToolResultBlock(
        id=call_id, name=name, content=content, synthesized=synthesized,
    )
    iface.add_tool_results([block])
    return block


def _fresh_scope():
    iface = ChatInterface()
    agent = _FakeAgent(iface)
    obs = _Observer()
    token = bind_turn_tool_observer(obs)
    begin_admission_witness_scope(agent)
    return iface, agent, obs, token


def test_a_commit_interrupted_no_fact():
    # (a) The tool COMPLETED but its result never reached the wire (commit
    # interrupted): only a dangling assistant tool_call exists, no user result.
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-1")  # no add_tool_results
        scan_and_emit_committed_facts(agent)
        assert obs.committed == []
    finally:
        reset_turn_tool_observer(token)


def test_aprime_success_fires_and_is_not_cosmetically_suppressed():
    # (a′) commit/send success => fact emitted. The scan does not consult any
    # of the lifecycle observer's _announced/_terminal/publication-lock state.
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-1")
        _commit_result(iface, "tc-1", "R1")
        scan_and_emit_committed_facts(agent)
        assert len(obs.committed) == 1
        ev = obs.committed[0]
        assert ev.tool_call_id == "tc-1"
        assert ev.binding == admission_binding("tc-1", "R1")
    finally:
        reset_turn_tool_observer(token)


def test_bprime_two_parallel_same_name_tools_two_correct_facts():
    # (b′) two parallel same-name calls => two facts, each with the correct
    # (toolCallId, binding). A swapped construction is distinguishable.
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-a")
        _open_call(iface, "tc-b")
        _commit_result(iface, "tc-a", "RA")
        _commit_result(iface, "tc-b", "RB")
        scan_and_emit_committed_facts(agent)
        got = {ev.tool_call_id: ev.binding for ev in obs.committed}
        assert got == {
            "tc-a": admission_binding("tc-a", "RA"),
            "tc-b": admission_binding("tc-b", "RB"),
        }
        # Distinguishable from the swapped construction.
        assert got["tc-a"] != admission_binding("tc-a", "RB")
    finally:
        reset_turn_tool_observer(token)


def test_cprime_no_fact_after_scope_closed():
    # (c′) A scan after the observer/scope is torn down fires nothing — a fact
    # never lands after the observer that would deliver it is gone.
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-1")
        _commit_result(iface, "tc-1", "R1")
        end_admission_witness_scope(agent)
        scan_and_emit_committed_facts(agent)
        assert obs.committed == []
    finally:
        reset_turn_tool_observer(token)


def test_read_inbox_result_on_wire_fires_no_fact():
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-inbox", name="read_inbox")
        block = ToolResultBlock(
            id="tc-inbox", name="read_inbox",
            content={"messages": [], "has_next": False},
        )
        iface.add_tool_results([block])
        scan_and_emit_committed_facts(agent)
        assert obs.committed == []
    finally:
        reset_turn_tool_observer(token)


def test_synthesized_result_on_wire_fires_no_fact():
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-1")
        _commit_result(iface, "tc-1", "R1", synthesized=True)
        scan_and_emit_committed_facts(agent)
        assert obs.committed == []
    finally:
        reset_turn_tool_observer(token)


def test_i_and_ii_rolled_back_vs_survived():
    # (i)/(ii) hinge entirely on wire presence.
    #
    # rolled-back: the failed send's adapter dropped the user entry -> the
    # receipt-bearing block is ABSENT from the wire -> no fact.
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-1")  # assistant tool_call remains dangling
        scan_and_emit_committed_facts(agent)  # settle after the failed send
        assert obs.committed == []
    finally:
        reset_turn_tool_observer(token)

    # survived: the entry is still on the wire after the failed send (no
    # rollback, or shielded from drop_trailing) -> fires exactly once.
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-1")
        _commit_result(iface, "tc-1", "R1")
        scan_and_emit_committed_facts(agent)  # settle after the failed send
        assert [ev.tool_call_id for ev in obs.committed] == ["tc-1"]
    finally:
        reset_turn_tool_observer(token)


def test_iii_same_id_across_settle_points_fires_once():
    # (iii) emitted-set idempotency across multiple settle points in one turn.
    iface, agent, obs, token = _fresh_scope()
    try:
        _open_call(iface, "tc-1")
        _commit_result(iface, "tc-1", "R1")
        scan_and_emit_committed_facts(agent)
        scan_and_emit_committed_facts(agent)
        scan_and_emit_committed_facts(agent)
        assert len(obs.committed) == 1
    finally:
        reset_turn_tool_observer(token)


def test_iv_two_turn_watermark_no_refire():
    # (iv) An entry that fired in turn 1 must NOT fire again at any settle point
    # in turn 2 — the turn-2 watermark excludes turn-1 history.
    iface = ChatInterface()
    agent = _FakeAgent(iface)
    obs = _Observer()
    token = bind_turn_tool_observer(obs)
    try:
        # Turn 1.
        begin_admission_witness_scope(agent)
        _open_call(iface, "tc-1")
        _commit_result(iface, "tc-1", "R1")
        scan_and_emit_committed_facts(agent)
        assert [ev.tool_call_id for ev in obs.committed] == ["tc-1"]
        end_admission_witness_scope(agent)

        # Turn 2 — the tc-1 entry is still on the wire, but below the new
        # watermark. A fresh receipt-bearing entry (tc-2) fires; tc-1 does not.
        begin_admission_witness_scope(agent)
        _open_call(iface, "tc-2")
        _commit_result(iface, "tc-2", "R2")
        scan_and_emit_committed_facts(agent)
        assert [ev.tool_call_id for ev in obs.committed] == ["tc-1", "tc-2"]
    finally:
        reset_turn_tool_observer(token)


def test_restore_path_settle_point_emits_for_survivor():
    # The real ``_restore_tool_results_after_continuation_failure`` settle point:
    # after a failed continuation send rolled back the user entry (leaving the
    # assistant tool_call pending), restore re-commits the real result and the
    # scan inside it fires the fact.
    iface = ChatInterface()
    agent = _FakeAgent(iface)
    obs = _Observer()
    token = bind_turn_tool_observer(obs)
    try:
        begin_admission_witness_scope(agent)
        _open_call(iface, "tc-1")  # dangling: adapter rolled the result back
        result = ToolResultBlock(
            id="tc-1", name="puffo_tool", content=_marker("R1"),
        )
        assert iface.has_pending_tool_calls()
        did = _restore_tool_results_after_continuation_failure(
            agent, [result], ledger_source="test",
        )
        assert did is True
        assert [ev.tool_call_id for ev in obs.committed] == ["tc-1"]
        assert obs.committed[0].binding == admission_binding("tc-1", "R1")
    finally:
        reset_turn_tool_observer(token)


# --------------------------------------------------------------------------- #
# 4. Real-path settle-point coverage — drive the ACTUAL in-code
#    `scan_and_emit_committed_facts` call sites via `_process_response`, so
#    deleting any settle-point call turns at least one test RED. These do NOT
#    call the scan function directly.
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, *, tool_calls=(), text="", api_call_id="api-1"):
        self.tool_calls = list(tool_calls)
        self.text = text
        self.thoughts = []
        self.raw = None
        self.usage = SimpleNamespace(output_tokens=1, thinking_tokens=0)
        self.api_call_id = api_call_id


class _FakeExecutor:
    def __init__(self, guard, tool_results, *, intercepted=False,
                 intercept_text="", on_execute=None):
        self.guard = guard
        self._tool_results = tool_results
        self._intercepted = intercepted
        self._intercept_text = intercept_text
        self._on_execute = on_execute

    def execute(self, tool_calls, **kwargs):
        if self._on_execute is not None:
            self._on_execute()
        return (self._tool_results, self._intercepted, self._intercept_text)


class _FakeSession:
    """Mirrors the real adapter: a carrying send appends tool_results to the
    canonical interface BEFORE the API result (so a raise leaves them on the
    wire as a survivor)."""

    def __init__(self, interface, *, mode="success"):
        self.interface = interface
        self.mode = mode
        self.sends = 0

    def send(self, message):
        self.sends += 1
        if isinstance(message, list):
            self.interface.add_tool_results(message)  # commit-before-API
        if self.mode == "raise":
            raise RuntimeError("continuation API failed")
        return _FakeResponse(text="done")  # terminates the tool loop


class _ProcAgent(_FakeAgent):
    def __init__(self, interface, executor, session):
        super().__init__(interface)
        self._executor = executor
        self._session = session
        self._cancel_event = threading.Event()
        self._notification_live_holder = None
        self._taskcard_live_holder = None
        self._runtime_live_holder = None
        self._on_tool_result_hook = None
        self._on_tool_pre_dispatch_hook = None
        self._intermediate_text_streamed = False
        self._last_usage = None
        self.agent_name = "test"

    def _log_notification_block_injected(self, *a, **k):
        pass


def _neutralize_turn_meta(monkeypatch):
    """Silence the per-batch meta/notification/housekeeping machinery so the
    test isolates the settle-point emission, not the meta engine."""
    for name in (
        "attach_active_notifications",
        "attach_active_taskcard",
        "attach_active_runtime",
        "finalize_two_axis_sidecars",
        "_check_external_send",
        "_turn_boundary_housekeeping",
        "_report_api_error_to_task_card",
        "_close_pending_tool_calls_after_poll_backoff",
    ):
        monkeypatch.setattr(turn_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(turn_mod, "_batch_includes_context_molt", lambda *a, **k: False)
    monkeypatch.setattr(turn_mod, "_check_poll_backoff", lambda *a, **k: False)


def _build_turn(monkeypatch, *, mode="success", intercepted=False,
                intercept_text="", on_execute=None, call_id="c1", raw="R1"):
    _neutralize_turn_meta(monkeypatch)
    iface = ChatInterface()
    result_block = ToolResultBlock(id=call_id, name="puffo_tool", content=_marker(raw))
    guard = LoopGuard(max_total_calls=100, dup_free_passes=3, dup_hard_block=8)
    executor = _FakeExecutor(
        guard, [result_block], intercepted=intercepted,
        intercept_text=intercept_text, on_execute=on_execute,
    )
    session = _FakeSession(iface, mode=mode)
    agent = _ProcAgent(iface, executor, session)
    obs = _Observer()
    token = bind_turn_tool_observer(obs)
    begin_admission_witness_scope(agent)
    initial = _FakeResponse(tool_calls=[ToolCallBlock(id=call_id, name="puffo_tool", args={})])
    return iface, agent, obs, token, initial


def test_realpath_normal_send_success_fires_once(monkeypatch):
    # (A) The carrying send returns; the fact fires once at the post-send
    # settle point (turn.py ~2897).
    iface, agent, obs, token, initial = _build_turn(monkeypatch, mode="success")
    try:
        _process_response(agent, initial)
        assert [ev.tool_call_id for ev in obs.committed] == ["c1"]
        assert obs.committed[0].binding == admission_binding("c1", "R1")
    finally:
        reset_turn_tool_observer(token)


def test_realpath_send_raises_survivor_fires_at_except_settle_point(monkeypatch):
    # (B) The carrying send raises but the receipt-bearing entry survives on the
    # wire; the fact fires at that send's exception settle point (turn.py ~2915).
    iface, agent, obs, token, initial = _build_turn(monkeypatch, mode="raise")
    try:
        with pytest.raises(RuntimeError):
            _process_response(agent, initial)
        assert [ev.tool_call_id for ev in obs.committed] == ["c1"]
    finally:
        reset_turn_tool_observer(token)


def test_realpath_intercept_commit_fires(monkeypatch):
    # (C) A no-API terminal commit_tool_results path (intercept) fires
    # (turn.py ~2837).
    iface, agent, obs, token, initial = _build_turn(
        monkeypatch, intercepted=True, intercept_text="stopped",
    )
    try:
        result = _process_response(agent, initial)
        assert result["text"] == "stopped"
        assert [ev.tool_call_id for ev in obs.committed] == ["c1"]
        assert agent._session.sends == 0  # no API send happened
    finally:
        reset_turn_tool_observer(token)


def test_realpath_cancel_commit_fires(monkeypatch):
    # (C-cancel) The mid-batch cancel commit path fires (turn.py ~2858).
    holder = {}

    def _cancel():
        agent._cancel_event.set()

    iface, agent, obs, token, initial = _build_turn(
        monkeypatch, on_execute=_cancel,
    )
    try:
        _process_response(agent, initial)
        assert [ev.tool_call_id for ev in obs.committed] == ["c1"]
        assert agent._session.sends == 0
    finally:
        reset_turn_tool_observer(token)


def test_realpath_poll_backoff_commit_fires(monkeypatch):
    # (C-backoff) The poll-backoff terminal commit path fires (turn.py ~2873).
    iface, agent, obs, token, initial = _build_turn(monkeypatch)
    monkeypatch.setattr(turn_mod, "_check_poll_backoff", lambda *a, **k: True)
    try:
        _process_response(agent, initial)
        assert [ev.tool_call_id for ev in obs.committed] == ["c1"]
        assert agent._session.sends == 0
    finally:
        reset_turn_tool_observer(token)


def test_realpath_initial_send_drained_pair_fires_at_request_settle_point(monkeypatch):
    # (D) A receipt-bearing (call, result) pair spliced onto the wire DURING
    # the initial request send — the adapter's pre-request tc_inbox drain rides
    # any API request, including the very first one — fires at the initial-send
    # settle point inside _handle_request (turn.py ~2088).  The response
    # carries no tool calls, so no _process_response settle point ever runs:
    # deleting the initial-send scan must turn exactly this test red.
    _neutralize_turn_meta(monkeypatch)
    iface = ChatInterface()

    class _DrainingSession(_FakeSession):
        """A send that splices a drained pair before the API result returns,
        mirroring pre_request_hook running inside the adapter send."""

        def __init__(self, interface, drain_pair):
            super().__init__(interface, mode="success")
            self._drain_pair = drain_pair

        def send(self, message):
            self.sends += 1
            if self._drain_pair is not None:
                call, result = self._drain_pair
                self.interface.add_assistant_message(content=[call])
                self.interface.add_tool_results([result])
                self._drain_pair = None
            return _FakeResponse(text="done")  # no tool calls

    drained = (
        ToolCallBlock(id="d1", name="puffo_tool", args={}),
        ToolResultBlock(id="d1", name="puffo_tool", content=_marker("RD")),
    )
    session = _DrainingSession(iface, drained)
    guard = LoopGuard(max_total_calls=100, dup_free_passes=3, dup_hard_block=8)
    executor = _FakeExecutor(guard, [])
    agent = _ProcAgent(iface, executor, session)
    agent._drain_tc_inbox = lambda: None
    agent._pre_request = lambda msg: "hello"
    agent._post_request = lambda msg, result: None

    monkeypatch.setattr(turn_mod, "is_worker_interface_poisoned", lambda a: False)
    monkeypatch.setattr(turn_mod, "_get_guard_limits", lambda a: (100, 3, 8))
    monkeypatch.setattr(turn_mod, "_make_tool_executor", lambda a, g: executor)
    monkeypatch.setattr(turn_mod, "build_meta", lambda a: {})
    monkeypatch.setattr(turn_mod, "render_meta", lambda a, m: "")

    obs = _Observer()
    token = bind_turn_tool_observer(obs)
    begin_admission_witness_scope(agent)
    try:
        result = turn_mod._handle_request(agent, SimpleNamespace(type="request"))
        assert result["text"] == "done"
        assert session.sends == 1
        assert [ev.tool_call_id for ev in obs.committed] == ["d1"]
        assert obs.committed[0].binding == admission_binding("d1", "RD")
    finally:
        reset_turn_tool_observer(token)
