"""Regression tests for issue #724: _GatedSession must forward attribute writes.

``_GatedSession`` (``src/lingtai/llm/base.py``) is a transparent proxy: reads
fall through to the inner adapter session via ``__getattr__`` and writes must
forward to it via ``__setattr__`` (keeping only the proxy's own ``_inner`` /
``_gate`` slots). Without write-forwarding, ``LLMService.create_session``'s
``session_id`` / ``_agent_type`` / ``_tracked`` writes and the kernel's
``pre_request_hook`` install land on the proxy's ``__dict__``, while the inner
session's internal reads (``ChatSession.get_state``, every adapter's
``send()``/``send_stream()`` hook fire) keep seeing stale class defaults.

Five tests fail on pre-fix main (``test_setattr_forwards_to_inner_session``,
``test_get_state_reflects_service_assigned_identity``,
``test_untracked_session_reports_untracked``,
``test_delattr_forwards_to_inner_session``,
``test_create_session_identity_survives_gating``);
``test_pre_request_hook_fires_inside_gated_send`` passes both versions because
the named hook property already landed on main, and
``test_proxy_slots_stay_on_proxy`` guards the slot invariant.
"""
from __future__ import annotations

from typing import Any

from lingtai.llm.base import LLMAdapter, _GatedSession
from lingtai.llm.api_gate import APICallGate
from lingtai.llm.service import LLMService
from lingtai.kernel.llm.base import ChatSession, LLMResponse
from lingtai.kernel.llm.interface import ChatInterface


class _FakeSession(ChatSession):
    """Minimal concrete ChatSession with a canonical interface."""

    def __init__(self) -> None:
        self._interface = ChatInterface()
        self._interface.add_user_message("hi")
        self.hook_fires: list[str] = []

    @property
    def interface(self) -> ChatInterface:
        return self._interface

    def send(self, message) -> LLMResponse:
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)
        return LLMResponse(text="ok")


def _wrapped() -> tuple[_FakeSession, _GatedSession, APICallGate]:
    inner = _FakeSession()
    gate = APICallGate(60)
    proxy = _GatedSession(inner, gate)
    return inner, proxy, gate


def test_setattr_forwards_to_inner_session() -> None:
    inner, proxy, gate = _wrapped()
    try:
        proxy.session_id = "abc"
        # The write must land on the inner session, whose get_state() reads it.
        assert inner.session_id == "abc"
        assert "session_id" not in object.__getattribute__(proxy, "__dict__")
    finally:
        gate.shutdown()


def test_proxy_slots_stay_on_proxy() -> None:
    inner, proxy, gate = _wrapped()
    try:
        assert set(proxy.__dict__) == {"_inner", "_gate"}
        assert proxy._inner is inner
        old_gate = proxy._gate
        assert old_gate is gate
    finally:
        gate.shutdown()


def test_pre_request_hook_fires_inside_gated_send() -> None:
    inner, proxy, gate = _wrapped()
    try:
        def _hook(iface: ChatInterface) -> None:
            inner.hook_fires.append("fired")

        proxy.pre_request_hook = _hook
        # Gated send blocks until the inner send (and therefore the hook) ran.
        response = proxy.send("hi")
        assert response.text == "ok"
        assert inner.hook_fires == ["fired"]
    finally:
        gate.shutdown()


def test_gated_stream_omits_progress_for_legacy_inner() -> None:
    """A gate must not make the optional progress callback break legacy sessions."""

    class _LegacyStreamSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[object, object]] = []

        def send_stream(self, message, on_chunk=None):
            self.calls.append((message, on_chunk))
            return LLMResponse(text="ok")

    inner = _LegacyStreamSession()
    gate = APICallGate(60)
    proxy = _GatedSession(inner, gate)
    try:
        counts: list[int] = []
        assert proxy.send_stream("hi", on_output_chars=counts.append).text == "ok"
        assert inner.calls == [("hi", None)]
        assert counts == []
    finally:
        gate.shutdown()


def test_get_state_reflects_service_assigned_identity() -> None:
    inner, proxy, gate = _wrapped()
    try:
        proxy.session_id = "abc"
        proxy._agent_type = "worker"
        proxy._tracked = True
        state = proxy.get_state()
        assert state["session_id"] == "abc"
        assert state["metadata"]["agent_type"] == "worker"
        assert state["metadata"]["tracked"] is True
        assert inner.session_id == "abc"
    finally:
        gate.shutdown()


def test_untracked_session_reports_untracked() -> None:
    inner, proxy, gate = _wrapped()
    try:
        proxy.session_id = ""
        proxy._tracked = False
        state = proxy.get_state()
        assert state["session_id"] == ""
        assert state["metadata"]["tracked"] is False
        assert inner._tracked is False
    finally:
        gate.shutdown()


def test_delattr_forwards_to_inner_session() -> None:
    inner, proxy, gate = _wrapped()
    try:
        proxy.session_id = "abc"
        # The write must live on the inner session, not the proxy __dict__.
        assert inner.__dict__["session_id"] == "abc"
        assert "session_id" not in proxy.__dict__
        del proxy.session_id
        # Deleted on the inner session: the instance attribute is gone (the
        # ChatSession class default "" remains visible).
        assert "session_id" not in inner.__dict__
        assert inner.session_id == ""  # class default restored
    finally:
        gate.shutdown()


def test_create_session_identity_survives_gating() -> None:
    """LLMService.create_session stamps identity that get_state() must report."""

    class _StubAdapter(LLMAdapter):
        def __init__(self, max_rpm: int = 0) -> None:
            super().__init__()
            if max_rpm > 0:
                self._setup_gate(max_rpm)

        def create_chat(self, *args: Any, **kwargs: Any) -> ChatSession:
            # Mirror real adapters: pass the concrete session through the gate.
            return self._wrap_with_gate(_FakeSession())

        def generate(self, *args: Any, **kwargs: Any) -> LLMResponse:
            raise NotImplementedError

        def make_tool_result_message(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        def is_quota_error(self, exc: BaseException) -> bool:
            return False

    def _stub_factory(*, model=None, defaults=None, max_rpm: int = 0, **kw: Any) -> _StubAdapter:
        return _StubAdapter(max_rpm=max_rpm)

    LLMService.register_adapter("stub-proxy-test", _stub_factory)
    svc = LLMService(
        provider="stub-proxy-test",
        model="m",
        api_key="sk-test",
        provider_defaults={"stub-proxy-test": {"max_rpm": 60}},
    )
    adapter = svc.get_adapter("stub-proxy-test")
    try:
        assert adapter._gate is not None  # default-configured sessions are gated
        chat = svc.create_session(system_prompt="sys", tools=[], agent_type="worker")
        assert chat.session_id  # stamped by the service
        assert chat.get_state()["session_id"] == chat.session_id
        assert chat.get_state()["metadata"]["agent_type"] == "worker"
        assert svc.get_session(chat.session_id) is chat
    finally:
        adapter._gate.shutdown()
