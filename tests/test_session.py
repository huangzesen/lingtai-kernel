"""Tests for SessionManager — LLM session, token tracking, context pressure."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lingtai.kernel.session import SessionManager
from lingtai.kernel.config import AgentConfig


def make_session_manager(**kw):
    svc = MagicMock()
    svc.model = "test-model"
    mock_session = MagicMock()
    mock_session.context_window.return_value = 100000
    mock_session.interface.estimate_context_tokens.return_value = 5000
    mock_session.interface.current_system_prompt = "test prompt"
    mock_session.send.return_value = MagicMock(
        text="hello", tool_calls=[], thoughts=[], usage=MagicMock(
            input_tokens=100,
            output_tokens=50,
            thinking_tokens=10,
            cached_tokens=20,
            extra={
                "codex_account_id_sha8": "abc12345",
                "codex_auth_path_sha8": "def67890",
                "codex_pool_source_index": 1,
                "unsafe_provider_blob": "should-not-log",
            },
        ),
    )
    svc.create_session.return_value = mock_session
    config = kw.get("config", AgentConfig())
    return SessionManager(
        llm_service=svc,
        config=config,
        agent_name="test",
        streaming=kw.get("streaming", False),
        build_system_prompt_fn=lambda: "test prompt",
        build_tool_schemas_fn=lambda: [],
        logger_fn=kw.get("logger_fn", None),
    ), svc, mock_session


# ------------------------------------------------------------------
# Session lifecycle
# ------------------------------------------------------------------

def test_ensure_session_creates_on_first_call():
    sm, svc, _ = make_session_manager()
    session = sm.ensure_session()
    assert session is not None
    assert sm.chat is not None
    svc.create_session.assert_called_once()


def test_ensure_session_reuses():
    sm, svc, _ = make_session_manager()
    s1 = sm.ensure_session()
    s2 = sm.ensure_session()
    assert s1 is s2
    assert svc.create_session.call_count == 1


def test_ensure_session_passes_interaction_id():
    sm, svc, _ = make_session_manager()
    sm.interaction_id = "int-123"
    sm.ensure_session()
    call_kwargs = svc.create_session.call_args[1]
    assert call_kwargs["interaction_id"] == "int-123"


def test_ensure_session_defaults_thinking_to_high():
    sm, svc, _ = make_session_manager()
    sm.ensure_session()
    call_kwargs = svc.create_session.call_args.kwargs
    assert call_kwargs["thinking"] == "high"


def test_ensure_session_uses_configured_thinking():
    sm, svc, _ = make_session_manager(config=AgentConfig(thinking="xhigh"))
    sm.ensure_session()
    call_kwargs = svc.create_session.call_args.kwargs
    assert call_kwargs["thinking"] == "xhigh"


# ------------------------------------------------------------------
# send() — the core operation
# ------------------------------------------------------------------

def test_send_happy_path():
    sm, svc, mock_session = make_session_manager()
    response = sm.send("hello")
    assert response.text == "hello"
    # Should have created session, called send_with_timeout, tracked usage
    svc.create_session.assert_called_once()


def test_send_tracks_usage():
    sm, _, _ = make_session_manager()
    sm.send("hello")
    usage = sm.get_token_usage()
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["api_calls"] == 1
    latest = sm.latest_token_usage_snapshot()
    assert latest is not None
    assert latest["scope"] == "provider_round"
    assert latest["api_call_index"] == 1
    assert latest["input_tokens"] == 100
    assert latest["cache_miss_tokens"] == 80
    assert latest["output_tokens"] == 50
    assert latest["thinking_tokens"] == 10
    assert latest["cached_tokens"] == 20
    assert latest["cache_rate"] == 0.2
    assert latest["context_tokens"] == 100
    assert latest["context_window"] == 100000
    assert latest["context_usage"] == 0.001
    assert latest["estimated"] is False
    assert latest["api_call_id"].startswith("api_")


def test_send_does_not_log_stale_pre_request_codex_selection():
    """llm_call precedes native selection, so an old chat breadcrumb is omitted."""
    events = []

    def log_fn(event_type, **fields):
        events.append((event_type, fields))

    sm, _, mock_session = make_session_manager(logger_fn=log_fn)
    selection = {
        "source_ref": "codex-auth/work.json",
        "source_index": 0,
        "pool_size": 2,
        "weight": 3,
        "auth_path_sha8": "0abc1234",
    }
    mock_session.codex_pool_selection = selection
    sm.send("hello")

    llm_call = next(fields for event, fields in events if event == "llm_call")
    assert "codex_pool" not in llm_call


def test_send_llm_call_omits_codex_pool_for_other_providers():
    """No dict breadcrumb on the chat -> no codex_pool field on llm_call."""
    events = []

    def log_fn(event_type, **fields):
        events.append((event_type, fields))

    # The default MagicMock chat auto-creates a codex_pool_selection attribute
    # that is NOT a dict — the field must still be omitted (dict-only guard).
    sm, _, _ = make_session_manager(logger_fn=log_fn)
    sm.send("hello")

    llm_call = next(fields for event, fields in events if event == "llm_call")
    assert "codex_pool" not in llm_call


def test_safe_usage_extra_event_filter_allows_pool_model_scope():
    """``codex_pool_model_scope`` (model-classified pools) passes the
    ``llm_response`` usage-extra allowlist; unknown keys still don't."""
    from lingtai.kernel.session import _safe_usage_extra_for_event

    extra = {
        "codex_pool_model_scope": "gpt-5.6-sol",
        "not_allowlisted": "dropped",
    }
    assert _safe_usage_extra_for_event(extra) == {
        "codex_pool_model_scope": "gpt-5.6-sol",
    }


def test_send_error_propagates():
    """Errors from send_with_timeout propagate directly — no retry in SessionManager."""
    sm, _, _ = make_session_manager()

    with patch("lingtai.kernel.session.send_with_timeout", side_effect=ValueError("real error")):
        with pytest.raises(ValueError, match="real error"):
            sm.send("hello")


def test_send_timeout_propagates():
    """TimeoutError propagates directly — caller (AED loop) handles recovery."""
    sm, _, _ = make_session_manager()

    with patch("lingtai.kernel.session.send_with_timeout", side_effect=TimeoutError("timed out")):
        with pytest.raises(TimeoutError, match="timed out"):
            sm.send("hello")


def test_send_preserves_interaction_id():
    sm, _, mock_session = make_session_manager()
    mock_session.interaction_id = "new-id"

    with patch("lingtai.kernel.session.send_with_timeout", return_value=mock_session.send.return_value):
        sm.send("hello")

    assert sm.interaction_id == "new-id"


def test_send_logs_llm_call_with_api_call_id():
    events = []

    def log_fn(event_type, **fields):
        events.append((event_type, fields))

    sm, _, response = make_session_manager(logger_fn=log_fn)
    sm.send("hello")

    event_types = [event for event, _ in events]
    assert "llm_call" in event_types
    assert "llm_response" in event_types

    llm_call = next(fields for event, fields in events if event == "llm_call")
    llm_response = next(fields for event, fields in events if event == "llm_response")
    assert llm_call["api_call_id"].startswith("api_")
    assert llm_response["api_call_id"] == llm_call["api_call_id"]
    assert response.send.return_value.api_call_id == llm_call["api_call_id"]
    assert llm_response["input_tokens"] == 100
    assert llm_response["output_tokens"] == 50
    assert llm_response["thinking_tokens"] == 10
    assert llm_response["cached_tokens"] == 20
    assert llm_response["estimated"] is False
    assert llm_response["usage_extra"] == {
        "codex_account_id_sha8": "abc12345",
        "codex_auth_path_sha8": "def67890",
        "codex_pool_source_index": "1",
    }
    for field in (
        "prompt_build_ms",
        "tool_schema_build_ms",
        "provider_wait_ms",
        "usage_track_ms",
    ):
        assert isinstance(llm_response[field], (int, float))
        assert llm_response[field] >= 0


# ------------------------------------------------------------------
# get_context_pressure()
# ------------------------------------------------------------------

def test_get_context_pressure_no_session():
    sm, svc, _ = make_session_manager()
    # No session yet — should return 0.0
    assert sm.get_context_pressure() == 0.0


def test_get_context_pressure_with_session():
    sm, svc, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.context_window.return_value = 100_000
    # Local estimate is the primary source (reflects current wire state)
    mock_session.interface.estimate_context_tokens.return_value = 85_000
    pressure = sm.get_context_pressure()
    assert pressure == 0.85


def test_get_context_pressure_fallback_to_server_tokens():
    """When local estimate is unavailable (0), fall back to server-reported."""
    sm, svc, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.context_window.return_value = 100_000
    mock_session.interface.estimate_context_tokens.return_value = 0
    sm._latest_input_tokens = 50_000
    pressure = sm.get_context_pressure()
    assert pressure == 0.5


def test_context_pressure_prefers_local_over_server():
    """When both local estimate and server tokens exist, local wins."""
    sm, svc, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.context_window.return_value = 200_000
    mock_session.interface.estimate_context_tokens.return_value = 160_000
    sm._latest_input_tokens = 100_000  # stale server value — ignored
    pressure = sm.get_context_pressure()
    assert pressure == 0.8


def test_get_context_pressure_zero_window():
    sm, svc, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.context_window.return_value = 0
    assert sm.get_context_pressure() == 0.0


# ------------------------------------------------------------------
# _track_usage()
# ------------------------------------------------------------------

def test_track_usage_accumulates():
    sm, _, _ = make_session_manager()
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 20

    sm._track_usage(response)
    usage = sm.get_token_usage()
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["thinking_tokens"] == 10
    assert usage["cached_tokens"] == 20
    assert usage["api_calls"] == 1

    # Second call accumulates
    sm._track_usage(response)
    usage = sm.get_token_usage()
    assert usage["input_tokens"] == 200
    assert usage["api_calls"] == 2


def test_total_tokens_uses_explicit_subset_or_separate_thinking_semantics():
    subset_sm, _, _ = make_session_manager()
    subset = MagicMock()
    subset.usage.input_tokens = 100
    subset.usage.output_tokens = 50
    subset.usage.thinking_tokens = 10
    subset.usage.cached_tokens = 0
    subset.usage.extra = {"thinking_tokens_semantics": "subset"}
    subset_sm._track_usage(subset)
    assert subset_sm.get_token_usage()["total_tokens"] == 150

    separate_sm, _, _ = make_session_manager()
    separate = MagicMock()
    separate.usage.input_tokens = 100
    separate.usage.output_tokens = 50
    separate.usage.thinking_tokens = 10
    separate.usage.cached_tokens = 0
    separate.usage.extra = {"thinking_tokens_semantics": "separate"}
    separate_sm._track_usage(separate)
    assert separate_sm.get_token_usage()["total_tokens"] == 160


def test_custom_or_alias_adapter_without_semantics_marker_fails_safe_to_subset():
    sm, _, _ = make_session_manager()
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 0
    response.usage.extra = {"provider": "custom-openai-alias"}
    sm._track_usage(response)
    assert sm.get_token_usage()["total_tokens"] == 150


@pytest.mark.parametrize(
    ("provider_case", "extra", "streaming", "expected_total"),
    [
        ("openai-compatible", {}, False, 150),
        ("anthropic", {}, False, 150),
        ("gemini-declared-total", {"thinking_tokens_semantics": "separate"}, False, 160),
        ("gemini-streaming", {"thinking_tokens_semantics": "separate"}, True, 160),
        ("openai-alias", {"provider": "openai-compatible-alias"}, False, 150),
        ("custom", {"provider": "custom-adapter"}, False, 150),
    ],
)
def test_provider_semantic_matrix_for_streaming_and_aliases(
    provider_case, extra, streaming, expected_total
):
    """Provider names never infer semantics; adapters must declare ``separate``."""
    sm, _, _ = make_session_manager(streaming=streaming)
    response = MagicMock(text="response", tool_calls=[], thoughts=[], api_call_id=None)
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 0
    response.usage.extra = extra

    if streaming:
        with patch(
            "lingtai.kernel.session.send_with_timeout_stream", return_value=response
        ):
            response = sm._send_streaming("prompt", retry_timeout=1.0)
    sm._track_usage(response)

    assert sm.get_token_usage()["total_tokens"] == expected_total, provider_case


def test_provider_semantic_matrix_missing_usage_is_conservative():
    sm, _, _ = make_session_manager()
    response = MagicMock(text="response", tool_calls=[], thoughts=[], api_call_id=None)
    response.usage = None

    sm._track_usage(response)

    usage = sm.get_token_usage()
    assert usage["total_tokens"] == 0
    assert usage["api_calls"] == 0


def test_provider_semantic_matrix_restore_preserves_declared_gemini_total():
    sm, _, _ = make_session_manager()
    response = MagicMock(text="response", tool_calls=[], thoughts=[], api_call_id=None)
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 0
    response.usage.extra = {"thinking_tokens_semantics": "separate"}
    sm._track_usage(response)
    persisted = sm.get_token_usage()

    restored, _, _ = make_session_manager()
    restored.restore_token_state(persisted)

    assert persisted["separate_thinking_tokens"] == 10
    assert restored.get_token_usage()["total_tokens"] == 160


def test_track_usage_triggers_decomposition_update():
    sm, _, _ = make_session_manager()
    assert sm.token_decomp_dirty  # starts dirty
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 0
    response.usage.cached_tokens = 0
    sm._track_usage(response)
    assert not sm.token_decomp_dirty  # updated during track_usage


# ------------------------------------------------------------------
# Token usage
# ------------------------------------------------------------------

def test_get_token_usage_default():
    sm, _, _ = make_session_manager()
    usage = sm.get_token_usage()
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert usage["api_calls"] == 0


def test_restore_token_state():
    sm, _, _ = make_session_manager()
    sm.restore_token_state({
        "input_tokens": 500, "output_tokens": 200,
        "thinking_tokens": 50, "cached_tokens": 100, "api_calls": 3,
    })
    usage = sm.get_token_usage()
    assert usage["input_tokens"] == 500
    assert usage["output_tokens"] == 200
    assert usage["api_calls"] == 3


# ------------------------------------------------------------------
# Runtime-session token usage — the CLEARLY NAMED since-refresh/process-start
# delta getter. ``get_runtime_session_token_usage`` is the canonical name;
# ``get_current_session_token_usage`` remains only as a deprecated compat alias
# that delegates to it (its old ambiguous name read like "since last molt",
# which is the injected ``token_usage.session`` semantic instead — that lives on
# the cumulative ``get_token_usage`` totals).
# ------------------------------------------------------------------





def test_get_current_session_token_usage_is_deprecated_alias_of_runtime():
    # The old ambiguous name must remain ONLY as a compatibility alias that
    # returns exactly what the clearly-named runtime getter returns.
    sm, _, _ = make_session_manager()
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 20
    sm._track_usage(response)

    assert (
        sm.get_current_session_token_usage()
        == sm.get_runtime_session_token_usage()
    )


# ------------------------------------------------------------------
# Runtime-session token usage (since-refresh deltas, not since-molt session totals)
# ------------------------------------------------------------------

def test_runtime_session_token_usage_default_zero():
    sm, _, _ = make_session_manager()
    usage = sm.get_runtime_session_token_usage()
    assert usage["api_calls"] == 0
    assert usage["input_tokens"] == 0
    assert usage["cached_tokens"] == 0
    assert usage["session_cache_rate"] == 0.0
    assert usage["avg_input_tokens_per_api_call"] == 0


def test_runtime_session_token_usage_tracks_runtime_deltas():
    sm, _, _ = make_session_manager()
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 20
    sm._track_usage(response)
    sm._track_usage(response)

    usage = sm.get_runtime_session_token_usage()
    assert usage["api_calls"] == 2
    assert usage["input_tokens"] == 200
    assert usage["cached_tokens"] == 40
    assert usage["avg_input_tokens_per_api_call"] == 100
    assert usage["session_cache_rate"] == 0.2


def test_runtime_session_token_usage_excludes_restored_session_totals():
    # Restored since-molt session totals (huge persisted numbers) must NOT leak
    # into the runtime-session deltas: the restore re-baselines so that
    # post-restore usage starts counting from zero.
    sm, _, _ = make_session_manager()
    sm.restore_token_state({
        "input_tokens": 5_000_000_000, "output_tokens": 1_000_000,
        "thinking_tokens": 500_000, "cached_tokens": 4_000_000_000,
        "api_calls": 27_863,
    })

    # Immediately after restore, the runtime session is empty.
    before = sm.get_runtime_session_token_usage()
    assert before["api_calls"] == 0
    assert before["input_tokens"] == 0
    assert before["cached_tokens"] == 0

    # One real provider round adds only its own delta on top of the baseline.
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 20
    sm._track_usage(response)

    after = sm.get_runtime_session_token_usage()
    assert after["api_calls"] == 1
    assert after["input_tokens"] == 100
    assert after["cached_tokens"] == 20
    # Session getter still reflects the restored since-molt totals.
    assert sm.get_token_usage()["input_tokens"] == 5_000_000_000 + 100
    assert sm.get_token_usage()["api_calls"] == 27_863 + 1


def test_runtime_session_token_usage_clamps_negative_deltas_to_zero():
    # A restore to LOWER totals than the live counters must not produce negatives.
    sm, _, _ = make_session_manager()
    response = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    response.usage.thinking_tokens = 10
    response.usage.cached_tokens = 20
    sm._track_usage(response)
    # Baseline now above live counters.
    sm.restore_token_state({
        "input_tokens": 10_000, "output_tokens": 0,
        "thinking_tokens": 0, "cached_tokens": 10_000, "api_calls": 50,
    })
    usage = sm.get_runtime_session_token_usage()
    assert usage["api_calls"] == 0
    assert usage["input_tokens"] == 0
    assert usage["cached_tokens"] == 0


# ------------------------------------------------------------------
# Session persistence
# ------------------------------------------------------------------

def test_get_chat_state_empty():
    sm, _, _ = make_session_manager()
    assert sm.get_chat_state() == {}


def test_get_chat_state_with_session():
    sm, _, mock_session = make_session_manager()
    sm.ensure_session()
    mock_session.interface.to_dict.return_value = [{"role": "user", "content": "hi"}]
    state = sm.get_chat_state()
    assert "messages" in state
    assert state["messages"] == [{"role": "user", "content": "hi"}]


def test_rebuild_session_uses_current_prompt_and_tools():
    sm, svc, _ = make_session_manager()
    mock_interface = MagicMock()
    mock_rebuilt = MagicMock()
    svc.create_session.return_value = mock_rebuilt
    sm._rebuild_session(mock_interface)
    call_kw = svc.create_session.call_args.kwargs
    assert call_kw["system_prompt"] == "test prompt"
    assert call_kw["tools"] is None  # [] is falsy → or None
    assert call_kw["model"] == "test-model"
    assert call_kw["thinking"] == "high"
    assert call_kw["tracked"] is True
    assert call_kw["agent_type"] == "test"
    assert call_kw["provider"] is None
    assert call_kw["interface"] is mock_interface
    assert sm.chat is mock_rebuilt


def test_rebuild_session_uses_configured_thinking():
    sm, svc, _ = make_session_manager(config=AgentConfig(thinking="medium"))
    mock_interface = MagicMock()
    sm._rebuild_session(mock_interface)
    call_kw = svc.create_session.call_args.kwargs
    assert call_kw["thinking"] == "medium"


def test_rebuild_session_tracked_false():
    sm, svc, _ = make_session_manager()
    mock_interface = MagicMock()
    svc.create_session.return_value = MagicMock()
    sm._rebuild_session(mock_interface, tracked=False)
    call_kw = svc.create_session.call_args.kwargs
    assert call_kw["tracked"] is False


def test_restore_chat_with_state():
    sm, svc, _ = make_session_manager()
    mock_rebuilt = MagicMock()
    svc.create_session.return_value = mock_rebuilt
    sm.restore_chat({"messages": [
        {"id": 0, "role": "system", "system": "old prompt", "timestamp": 0.0},
        {"id": 1, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1.0},
    ]})
    assert sm.chat is mock_rebuilt
    call_kw = svc.create_session.call_args.kwargs
    assert call_kw["interface"] is not None


def test_restore_chat_fallback_on_error():
    sm, svc, _ = make_session_manager()
    call_count = [0]
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1 and kwargs.get("interface") is not None:
            raise ValueError("bad state")
        return MagicMock()
    svc.create_session.side_effect = side_effect
    sm.restore_chat({"messages": [
        {"id": 0, "role": "system", "system": "prompt", "timestamp": 0.0},
        {"id": 1, "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": 1.0},
    ]})
    assert sm.chat is not None
    assert svc.create_session.call_count == 2


def test_restore_chat_uses_current_config():
    sm, svc, _ = make_session_manager()
    mock_rebuilt = MagicMock()
    svc.create_session.return_value = mock_rebuilt
    saved_state = {"messages": [
        {"id": 0, "role": "system", "system": "OLD stale prompt", "timestamp": 0.0,
         "tools": [{"name": "old_tool", "description": "gone", "parameters": {}}]},
        {"id": 1, "role": "user", "content": [{"type": "text", "text": "hello"}], "timestamp": 1.0},
    ]}
    sm.restore_chat(saved_state)
    call_kw = svc.create_session.call_args.kwargs
    assert call_kw["system_prompt"] == "test prompt"
    assert call_kw["interface"] is not None
    assert len(call_kw["interface"].entries) == 2


def test_restore_chat_empty_state():
    sm, svc, _ = make_session_manager()
    sm.restore_chat({})
    # Should call ensure_session
    assert sm.chat is not None
    svc.create_session.assert_called_once()


# ------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------

def test_token_decomp_dirty_flag():
    sm, _, _ = make_session_manager()
    assert sm.token_decomp_dirty
    sm.token_decomp_dirty = False
    assert not sm.token_decomp_dirty


def test_interaction_id_property():
    sm, _, _ = make_session_manager()
    assert sm.interaction_id is None
    sm.interaction_id = "int-456"
    assert sm.interaction_id == "int-456"


def test_intermediate_text_streamed_property():
    sm, _, _ = make_session_manager()
    assert not sm.intermediate_text_streamed
    sm.intermediate_text_streamed = True
    assert sm.intermediate_text_streamed


# ------------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------------

def test_close_shuts_down_pool():
    sm, _, _ = make_session_manager()
    sm.close()
    # Should not raise on second close
    sm.close()


# ------------------------------------------------------------------
# Health check (pre-send invariant)
# ------------------------------------------------------------------

def test_health_check_heals_pending_tool_calls_on_user_message():
    """Before dispatch, if the canonical tail has unanswered tool_calls and
    the next message is a user-text string, close the dangling calls and
    log a structured health_check event. Adapter never sees the broken state."""
    events: list[tuple[str, dict]] = []
    sm, _, mock_session = make_session_manager(
        logger_fn=lambda evt, **kw: events.append((evt, kw))
    )
    mock_session.interface.has_pending_tool_calls.return_value = True

    sm.send("follow-up user text")

    mock_session.interface.close_pending_tool_calls.assert_called_once_with(
        reason="health_check:pre_send_pairing"
    )
    health_events = [(e, kw) for e, kw in events if e == "health_check"]
    assert health_events, f"expected a health_check event, got {events}"
    _, fields = health_events[0]
    assert fields["check"] == "pre_send_pairing"
    assert fields["action"] == "auto_heal"


def test_health_check_noop_when_tail_is_clean():
    """No heal, no warning event, no close_pending_tool_calls call when
    the canonical tail has no pending tool_calls."""
    events: list[tuple[str, dict]] = []
    sm, _, mock_session = make_session_manager(
        logger_fn=lambda evt, **kw: events.append((evt, kw))
    )
    mock_session.interface.has_pending_tool_calls.return_value = False

    sm.send("normal user text")

    mock_session.interface.close_pending_tool_calls.assert_not_called()
    assert not [e for e, _ in events if e == "health_check"]


def test_health_check_noop_when_message_is_tool_results():
    """Tool-results sends are the legitimate answer to pending calls —
    don't synthesize placeholders that would compete with real results."""
    events: list[tuple[str, dict]] = []
    sm, _, mock_session = make_session_manager(
        logger_fn=lambda evt, **kw: events.append((evt, kw))
    )
    mock_session.interface.has_pending_tool_calls.return_value = True

    # ToolResultBlock-shaped message (non-string)
    sm.send([MagicMock()])

    mock_session.interface.close_pending_tool_calls.assert_not_called()
    assert not [e for e, _ in events if e == "health_check"]

def test_reset_runtime_session_token_usage_rebaselines_deltas():
    sm, _, _ = make_session_manager()

    sm.send("hello")
    current = sm.get_runtime_session_token_usage()
    assert current["input_tokens"] == 100
    assert current["cached_tokens"] == 20
    assert current["api_calls"] == 1

    sm.reset_runtime_session_token_usage()

    current = sm.get_runtime_session_token_usage()
    assert current["input_tokens"] == 0
    assert current["cached_tokens"] == 0
    assert current["api_calls"] == 0
    assert current["session_cache_rate"] == 0.0
    assert current["avg_input_tokens_per_api_call"] == 0

    # Session totals remain intact; only the runtime-session
    # baseline moves forward.
    totals = sm.get_token_usage()
    assert totals["input_tokens"] == 100
    assert totals["cached_tokens"] == 20
    assert totals["api_calls"] == 1


def test_reset_session_token_usage_starts_fresh_since_molt_counter():
    sm, _, _ = make_session_manager()

    sm.send("hello")
    assert sm.get_token_usage()["input_tokens"] == 100
    assert sm.get_token_usage()["cached_tokens"] == 20
    assert sm.get_token_usage()["api_calls"] == 1

    sm.reset_session_token_usage(context_tokens=321)

    totals = sm.get_token_usage()
    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 0
    assert totals["thinking_tokens"] == 0
    assert totals["cached_tokens"] == 0
    assert totals["api_calls"] == 0
    assert totals["ctx_total_tokens"] == 321

    runtime = sm.get_runtime_session_token_usage()
    assert runtime["input_tokens"] == 0
    assert runtime["cached_tokens"] == 0
    assert runtime["api_calls"] == 0
