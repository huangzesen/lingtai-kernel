"""ACP v1 local-stdio wire, settlement, and composition tests."""
from __future__ import annotations

import io
import json
import queue
import threading
import time
import sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

from lingtai.adapters.acp.server import AcpStdioServer
from lingtai.kernel.base_agent import BaseAgent, StopResult, StopStatus
from lingtai.kernel.turns import TurnOrigin, TurnOutcome, TurnResult, control_from_message
from lingtai.kernel.turn_events import ToolLifecycleEvent, ToolLifecycleState
from lingtai.kernel.turn_permissions import (
    PermissionDecision,
    ToolPermissionRequest,
)


class _Handle:
    def __init__(self, correlation_id: str, result: TurnResult | None = None):
        self.correlation_id = correlation_id
        self._future = Future()
        self._cancel_requested = threading.Event()
        if result is not None:
            self._future.set_result(result)

    def cancel(self):
        if self._future.done():
            return False
        self._cancel_requested.set()
        self._future.set_result(
            TurnResult(self.correlation_id, TurnOutcome.CANCELLED)
        )
        return True

    def cancel_requested(self):
        return self._cancel_requested.is_set()

    def result(self, timeout=None):
        return self._future.result(timeout=timeout)


class _Agent:
    def __init__(self, handle: _Handle):
        self.handle = handle
        self.submissions: list[dict] = []
        self._shutdown = threading.Event()

    def submit_turn(
        self,
        content,
        *,
        sender,
        correlation_id,
        execution_workspace=None,
        tool_observer=None,
        permission_broker=None,
        origin=None,
    ):
        self.submissions.append({
            "content": content,
            "sender": sender,
            "correlation_id": correlation_id,
            "execution_workspace": execution_workspace,
            "tool_observer": tool_observer,
            "permission_broker": permission_broker,
            "origin": origin,
        })
        self.handle.correlation_id = correlation_id
        return self.handle


def _messages(output: io.StringIO):
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _wait_for(output: io.StringIO, count: int):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        messages = _messages(output)
        if len(messages) >= count:
            return messages
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {count} frames: {output.getvalue()!r}")


def _request(server, request_id, method, params):
    server._dispatch({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    })


def _open_session(server, output):
    _request(server, 1, "initialize", {"protocolVersion": 1})
    _request(server, 2, "session/new", {"cwd": "/tmp", "mcpServers": []})
    messages = _wait_for(output, 2)
    assert messages[0]["result"] == {
        "protocolVersion": 1,
        "agentCapabilities": {},
        "agentInfo": {
            "name": "lingtai",
            "title": "LingTai",
            "version": messages[0]["result"]["agentInfo"]["version"],
        },
        "authMethods": [],
    }
    return messages[1]["result"]["sessionId"]


class _Lease:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _SessionMcpAgent(_Agent):
    def __init__(self, handle):
        super().__init__(handle)
        self.configs = None
        self.lease = _Lease()

    def mount_session_mcp_stdio(self, configs):
        self.configs = configs
        return self.lease


def test_session_new_canonicalizes_existing_directory_and_mounts_stdio_mcp(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    agent = _SessionMcpAgent(_Handle("placeholder"))
    agent._working_dir = tmp_path / "agent-identity"
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    _request(server, 1, "initialize", {"protocolVersion": 1})
    _request(server, 2, "session/new", {
        "cwd": str(alias),
        "mcpServers": [{
            "name": "demo",
            "command": sys.executable,
            "args": ["-m", "demo"],
            "env": [{"name": "TOKEN", "value": "value"}],
        }],
    })
    session_id = _wait_for(output, 2)[1]["result"]["sessionId"]
    assert agent.configs[0].name == "demo"
    assert agent.configs[0].env == (("TOKEN", "value"),)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "go"}],
    })
    assert agent.submissions[0]["execution_workspace"].root == workspace.resolve()
    assert agent.submissions[0]["origin"] is TurnOrigin.AUTHENTICATED_ADAPTER
    assert agent._working_dir == tmp_path / "agent-identity"
    server.close()
    server.close()
    assert agent.lease.closed == 1


def test_acp_prompt_queues_canonical_workspace_through_real_base_agent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    agent = object.__new__(BaseAgent)
    agent._shutdown = threading.Event()
    agent.inbox = queue.Queue()
    server = AcpStdioServer(agent, io.StringIO(), io.StringIO())

    session_id = server._new_session({"cwd": str(alias), "mcpServers": []})[
        "sessionId"
    ]
    server._prompt(
        {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": "go"}],
        },
        "prompt-real-facade",
    )

    control = control_from_message(agent.inbox.get_nowait())
    server.close()
    assert control is not None
    assert control.execution_workspace is server._execution_workspace
    assert control.execution_workspace.root == workspace.resolve()
    assert control.tool_observer is not None
    assert control.permission_broker is not None
    assert control.origin is TurnOrigin.AUTHENTICATED_ADAPTER


def test_permission_wire_allows_only_matching_allow_once_then_lifecycle_progresses():
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(broker.request_permission(
            ToolPermissionRequest("safe-trace", "shell")
        ))
    )
    waiter.start()
    messages = _wait_for(output, 4)
    pending, request = messages[2:4]
    safe_id = f"{handle.correlation_id}:safe-trace"
    assert pending["params"]["update"] == {
        "sessionUpdate": "tool_call",
        "toolCallId": safe_id,
        "title": "shell",
        "status": "pending",
    }
    assert request == {
        "jsonrpc": "2.0",
        "id": request["id"],
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": safe_id,
                "title": "shell",
                "status": "pending",
            },
            "options": [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }
    assert "sessionUpdate" not in request["params"]["toolCall"]
    projected = json.dumps((pending, request))
    for forbidden in (
        "arguments", "command", "paths", "env", "results", "content",
        "locations", "rawInput", "rawOutput", "private",
    ):
        assert forbidden not in projected

    server._dispatch({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "_meta": {"source": "client"},
            "outcome": {
                "outcome": "selected",
                "optionId": "allow_once",
                "_meta": {"source": "user"},
            },
        },
    })
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert result == [PermissionDecision.ALLOW]

    broker.on_tool_lifecycle(
        ToolLifecycleEvent("safe-trace", "shell", ToolLifecycleState.STARTED)
    )
    broker.on_tool_lifecycle(
        ToolLifecycleEvent("safe-trace", "shell", ToolLifecycleState.COMPLETED)
    )
    updates = _wait_for(output, 6)[4:6]
    assert [item["params"]["update"]["status"] for item in updates] == [
        "in_progress", "completed"
    ]
    assert all(
        item["params"]["update"]["sessionUpdate"] == "tool_call_update"
        for item in updates
    )


def test_guessed_allow_before_permission_request_flush_cannot_authorize():
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = _BlockingOutput(block_on=3)
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        broker.request_permission(ToolPermissionRequest("guessed", "shell"))
    ))
    waiter.start()
    assert output.blocked.wait(timeout=5)
    assert "lingtai-permission-1" in server._pending_permissions
    before = len(_messages(output))

    responder = threading.Thread(target=lambda: server._dispatch({
        "jsonrpc": "2.0",
        "id": "lingtai-permission-1",
        "result": {
            "outcome": {"outcome": "selected", "optionId": "allow_once"},
        },
    }))
    responder.start()
    time.sleep(0.02)
    assert responder.is_alive(), "response must wait on in-flight publication"
    assert result == []
    assert len(_messages(output)) == before

    output.release.set()
    responder.join(timeout=2)
    waiter.join(timeout=2)
    assert not responder.is_alive()
    assert not waiter.is_alive()
    assert result == [PermissionDecision.DENY]
    assert not server._pending_permissions
    server.close()


def test_response_captured_before_request_flush_stays_denied_after_publication():
    class ExitGateLock:
        def __init__(self, lock):
            self._lock = lock
            self.captured = threading.Event()
            self.release = threading.Event()
            self.tripped = False

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            self._lock.release()
            if (
                threading.current_thread().name == "preflush-response"
                and not self.tripped
            ):
                self.tripped = True
                self.captured.set()
                assert self.release.wait(timeout=5)
            return False

    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = _BlockingOutput(block_on=4)
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    decisions = []
    waiter = threading.Thread(target=lambda: decisions.append(
        broker.request_permission(ToolPermissionRequest("captured", "shell"))
    ))
    waiter.start()
    assert output.blocked.wait(timeout=5)
    pending = server._pending_permissions["lingtai-permission-1"]
    gate = ExitGateLock(server._state_lock)
    server._state_lock = gate

    responder = threading.Thread(
        name="preflush-response",
        target=lambda: server._dispatch({
            "jsonrpc": "2.0",
            "id": pending.request_id,
            "result": {
                "outcome": {"outcome": "selected", "optionId": "allow_once"},
            },
        }),
    )
    responder.start()
    assert gate.captured.wait(timeout=5)
    assert not pending.published

    output.release.set()
    _wait_until(lambda: pending.published, message="permission publication")
    gate.release.set()
    responder.join(timeout=2)
    waiter.join(timeout=2)

    assert not responder.is_alive()
    assert not waiter.is_alive()
    assert decisions == [PermissionDecision.DENY]
    server.close()


def test_cancel_during_blocked_pending_write_emits_adjacent_failed_terminal():
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = _BlockingOutput(block_on=3)
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    decisions = []
    waiter = threading.Thread(target=lambda: decisions.append(
        broker.request_permission(ToolPermissionRequest("cancelled-write", "shell"))
    ))
    waiter.start()
    assert output.blocked.wait(timeout=5)

    server._dispatch({
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": session_id},
    })
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert decisions == [PermissionDecision.DENY]
    broker.on_tool_lifecycle(ToolLifecycleEvent(
        "cancelled-write", "shell", ToolLifecycleState.DENIED
    ))

    output.release.set()
    messages = _wait_for(output, 5)
    updates = [
        message["params"]["update"]
        for message in messages
        if message.get("method") == "session/update"
    ]
    assert [(update["sessionUpdate"], update["status"]) for update in updates] == [
        ("tool_call", "pending"),
        ("tool_call_update", "failed"),
    ]
    assert not any(
        message.get("method") == "session/request_permission"
        for message in messages
    )
    assert any(
        message.get("id") == 3
        and message.get("result") == {"stopReason": "cancelled"}
        for message in messages
    )
    server.close()


@pytest.mark.parametrize("response", [
    {"result": {"outcome": {
        "outcome": "selected", "optionId": "reject_once",
    }}},
    {"result": {"outcome": {
        "outcome": "selected", "optionId": "allow_always",
    }}},
    {"result": {"outcome": {"outcome": "cancelled"}}},
    {"result": {"outcome": "selected", "optionId": "allow_once"}},
    {"result": {"outcome": {"outcome": "selected"}}},
    {"result": {"outcome": {
        "outcome": "selected", "optionId": "allow_once", "extra": True,
    }}},
    {"result": {
        "outcome": {"outcome": "selected", "optionId": "allow_once"},
        "extra": True,
    }},
    {"error": {"code": -32000, "message": "client failed"}},
])
def test_matching_non_allow_permission_responses_deny_without_response(response):
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        broker.request_permission(ToolPermissionRequest("trace", "file"))
    ))
    waiter.start()
    request = _wait_for(output, 4)[3]
    before = len(_messages(output))
    server._dispatch({"jsonrpc": "2.0", "id": request["id"], **response})
    waiter.join(timeout=2)
    assert result == [PermissionDecision.DENY]
    time.sleep(0.02)
    assert len(_messages(output)) == before
    server.close()


def test_permission_timeout_denies_and_late_allow_cannot_authorize(monkeypatch):
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    monkeypatch.setattr(server, "_PERMISSION_TIMEOUT_SECONDS", 0.01)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    assert broker.request_permission(
        ToolPermissionRequest("timeout-trace", "shell")
    ) is PermissionDecision.DENY
    request = _wait_for(output, 4)[3]
    server._dispatch({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "outcome": {"outcome": "selected", "optionId": "allow_once"},
        },
    })
    assert request["id"] not in server._pending_permissions
    server.close()


@pytest.mark.parametrize("terminal", ["close", "cancel"])
def test_close_and_cancel_wake_permission_waiter_with_denial(terminal):
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        broker.request_permission(ToolPermissionRequest("trace", "file"))
    ))
    waiter.start()
    _wait_for(output, 4)
    if terminal == "close":
        server.close()
    else:
        server._dispatch({
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        })
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert result == [PermissionDecision.DENY]
    server.close()


def test_unknown_response_id_is_ignored_and_cannot_authorize_pending_request():
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        broker.request_permission(ToolPermissionRequest("trace", "file"))
    ))
    waiter.start()
    request = _wait_for(output, 4)[3]
    before = len(_messages(output))
    server._dispatch({
        "jsonrpc": "2.0",
        "id": "unknown-permission",
        "result": {
            "outcome": {"outcome": "selected", "optionId": "allow_once"},
        },
    })
    time.sleep(0.02)
    assert len(_messages(output)) == before
    assert request["id"] in server._pending_permissions
    server._dispatch({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"outcome": {"outcome": "cancelled"}},
    })
    waiter.join(timeout=2)
    assert result == [PermissionDecision.DENY]
    server.close()


def test_methodless_nonresponse_remains_an_invalid_request():
    output = io.StringIO()
    server = AcpStdioServer(_Agent(_Handle("placeholder")), io.StringIO(), output)

    server._dispatch({"jsonrpc": "2.0", "id": 99})

    assert _wait_for(output, 1) == [{
        "jsonrpc": "2.0",
        "id": 99,
        "error": {"code": -32600, "message": "Invalid Request"},
    }]
    server.close()


@pytest.mark.parametrize("request_id", [None, -(1 << 63), (1 << 63) - 1, "req-1"])
def test_initialize_accepts_and_echoes_valid_request_ids(request_id):
    output = io.StringIO()
    server = AcpStdioServer(_Agent(_Handle("placeholder")), io.StringIO(), output)

    _request(server, request_id, "initialize", {"protocolVersion": 1})

    response = _wait_for(output, 1)[0]
    assert response["id"] == request_id
    assert response["result"]["protocolVersion"] == 1
    server.close()


@pytest.mark.parametrize(
    "request_id",
    [True, False, 1.5, [], {}, -(1 << 63) - 1, 1 << 63],
)
def test_invalid_request_ids_use_null_diagnostic_id(request_id):
    output = io.StringIO()
    server = AcpStdioServer(_Agent(_Handle("placeholder")), io.StringIO(), output)

    _request(server, request_id, "initialize", {"protocolVersion": 1})

    assert _wait_for(output, 1) == [{
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }]
    server.close()


def test_cancelled_turn_cannot_register_a_new_permission_request():
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    handle._cancel_requested.set()
    before = len(_messages(output))

    assert broker.request_permission(
        ToolPermissionRequest("cancelled-before-register", "shell")
    ) is PermissionDecision.DENY
    time.sleep(0.02)
    assert len(_messages(output)) == before
    assert not server._pending_permissions

    broker.on_tool_lifecycle(ToolLifecycleEvent(
        "cancelled-before-register",
        "shell",
        ToolLifecycleState.DENIED,
    ))
    failed = _wait_for(output, before + 1)[-1]["params"]["update"]
    assert failed == {
        "sessionUpdate": "tool_call",
        "toolCallId": (
            f"{handle.correlation_id}:cancelled-before-register"
        ),
        "title": "shell",
        "status": "failed",
    }
    server.close()


def test_tool_lifecycle_projects_ordered_private_free_updates_before_terminal():
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)

    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    observer = agent.submissions[0]["tool_observer"]
    observer.on_tool_lifecycle(
        ToolLifecycleEvent("provider-id", "file", ToolLifecycleState.STARTED)
    )
    observer.on_tool_lifecycle(
        ToolLifecycleEvent("provider-id", "file", ToolLifecycleState.COMPLETED)
    )
    handle._future.set_result(
        TurnResult(handle.correlation_id, TurnOutcome.NORMAL, text="done")
    )

    messages = _wait_for(output, 6)
    prompt_frames = messages[2:]
    assert [
        frame.get("params", {}).get("update", {}).get("sessionUpdate")
        for frame in prompt_frames[:-1]
    ] == ["tool_call", "tool_call_update", "agent_message_chunk"]
    initial = prompt_frames[0]["params"]["update"]
    update = prompt_frames[1]["params"]["update"]
    assert initial == {
        "sessionUpdate": "tool_call",
        "toolCallId": f"{handle.correlation_id}:provider-id",
        "title": "file",
        "status": "in_progress",
    }
    assert update == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": initial["toolCallId"],
        "status": "completed",
    }
    assert prompt_frames[-1] == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"stopReason": "end_turn"},
    }
    projected_wire = json.dumps(prompt_frames[:2])
    for forbidden in (
        "arguments",
        "rawInput",
        "rawOutput",
        "results",
        "content",
        "locations",
        "never-project",
    ):
        assert forbidden not in projected_wire

    before = output.getvalue()
    observer.on_tool_lifecycle(
        ToolLifecycleEvent("late", "shell", ToolLifecycleState.STARTED)
    )
    time.sleep(0.02)
    assert output.getvalue() == before


def _admission_frames(output):
    frames = []
    for msg in _messages(output):
        update = msg.get("params", {}).get("update", {})
        meta = update.get("_meta", {}) if isinstance(update, dict) else {}
        if isinstance(meta, dict) and "puffo.admission/1" in meta:
            frames.append(update)
    return frames


def test_puffo_committed_fact_emits_reliably_idempotently_and_respects_teardown():
    from lingtai.kernel.turn_events import ToolResultsCommittedEvent

    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 5, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "use a tool"}],
    })
    observer = agent.submissions[0]["tool_observer"]

    # Drive the tool lifecycle to a terminal, announced state. For a lifecycle
    # update this marks the call in _terminal/_announced and would suppress any
    # further lifecycle projection — but the committed fact must NOT be dropped
    # by that cosmetic suppression.
    observer.on_tool_lifecycle(
        ToolLifecycleEvent("tc-1", "puffo_tool", ToolLifecycleState.STARTED)
    )
    observer.on_tool_lifecycle(
        ToolLifecycleEvent("tc-1", "puffo_tool", ToolLifecycleState.COMPLETED)
    )
    _wait_for(output, 4)  # initialize + session/new + tool_call + tool_call_update

    binding = "a" * 64
    observer.on_tool_results_committed(
        ToolResultsCommittedEvent("tc-1", binding)
    )
    time.sleep(0.02)
    frames = _admission_frames(output)
    assert len(frames) == 1
    assert frames[0] == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": f"{handle.correlation_id}:tc-1",
        "_meta": {
            "puffo.admission/1": {"toolCallId": "tc-1", "binding": binding},
        },
    }

    # Idempotent: a second committed event for the same toolCallId emits nothing.
    observer.on_tool_results_committed(
        ToolResultsCommittedEvent("tc-1", binding)
    )
    time.sleep(0.02)
    assert len(_admission_frames(output)) == 1

    # Genuine teardown is the only legitimate non-delivery.
    server.close()
    before = len(_admission_frames(output))
    observer.on_tool_results_committed(
        ToolResultsCommittedEvent("tc-2", "b" * 64)
    )
    time.sleep(0.02)
    assert len(_admission_frames(output)) == before


def test_denied_tool_projects_one_initial_failed_update_and_close_drops_events():
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 4, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "denied"}],
    })
    observer = agent.submissions[0]["tool_observer"]
    observer.on_tool_lifecycle(
        ToolLifecycleEvent("denied-id", "shell", ToolLifecycleState.DENIED)
    )
    messages = _wait_for(output, 3)
    assert messages[2]["params"]["update"] == {
        "sessionUpdate": "tool_call",
        "toolCallId": f"{handle.correlation_id}:denied-id",
        "title": "shell",
        "status": "failed",
    }

    server.close()
    before = output.getvalue()
    observer.on_tool_lifecycle(
        ToolLifecycleEvent("after-close", "file", ToolLifecycleState.STARTED)
    )
    time.sleep(0.02)
    assert output.getvalue() == before


@pytest.mark.parametrize("cwd,mcp_servers", [
    ("missing", []),
    (None, []),
    ("valid", [{"type": "http", "name": "x", "url": "https://example"}]),
    ("valid", [{"name": "x", "command": "relative", "args": [], "env": []}]),
    ("valid", [{"name": "x", "command": "/bin/x", "args": [1], "env": []}]),
    ("valid", [{"name": "x", "command": "/bin/x", "args": [], "env": {}}]),
    ("valid", [
        {"name": "x", "command": "/bin/x", "args": [], "env": []},
        {"name": "x", "command": "/bin/y", "args": [], "env": []},
    ]),
])
def test_session_new_rejects_malformed_workspace_and_mcp(tmp_path, cwd, mcp_servers):
    valid = tmp_path / "valid"
    valid.mkdir()
    raw_cwd = str(valid) if cwd == "valid" else (
        str(tmp_path / "missing") if cwd == "missing" else cwd
    )
    output = io.StringIO()
    server = AcpStdioServer(_Agent(_Handle("x")), io.StringIO(), output)
    _request(server, 1, "initialize", {"protocolVersion": 1})
    _request(server, 2, "session/new", {"cwd": raw_cwd, "mcpServers": mcp_servers})
    assert _wait_for(output, 2)[1]["error"]["code"] == -32602


def test_close_during_session_mcp_start_returns_and_late_lease_is_closed(tmp_path):
    class _BlockingAgent(_SessionMcpAgent):
        def __init__(self, handle):
            super().__init__(handle)
            self.started = threading.Event()
            self.release = threading.Event()

        def mount_session_mcp_stdio(self, configs):
            self.configs = configs
            self.started.set()
            assert self.release.wait(timeout=5)
            return self.lease

    agent = _BlockingAgent(_Handle("x"))
    server = AcpStdioServer(agent, io.StringIO(), io.StringIO())
    errors = []

    def create_session():
        try:
            server._new_session({
                "cwd": str(tmp_path),
                "mcpServers": [{
                    "name": "demo",
                    "command": sys.executable,
                    "args": [],
                    "env": [],
                }],
            })
        except Exception as exc:  # private RPC error asserted structurally
            errors.append(exc)

    worker = threading.Thread(target=create_session)
    worker.start()
    assert agent.started.wait(timeout=5)
    started = time.monotonic()
    server.close()
    assert time.monotonic() - started < 0.5
    agent.release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert getattr(errors[0], "code", None) == -32004
    assert agent.lease.closed == 1
    assert server._session_id is None
    assert server._session_mcp_lease is None


def test_session_new_rejects_nonempty_additional_directories(tmp_path):
    output = io.StringIO()
    server = AcpStdioServer(_Agent(_Handle("x")), io.StringIO(), output)
    _request(server, 1, "initialize", {"protocolVersion": 1})
    _request(server, 2, "session/new", {
        "cwd": str(tmp_path),
        "mcpServers": [],
        "additionalDirectories": [str(tmp_path / "extra")],
    })
    assert _wait_for(output, 2)[1]["error"]["code"] == -32004



def test_eof_closes_session_mcp_lease(tmp_path):
    agent = _SessionMcpAgent(_Handle("x"))
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {
            "cwd": str(tmp_path),
            "mcpServers": [{"name": "x", "command": sys.executable, "args": [], "env": []}],
        }},
    ]
    input_stream = io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames))
    server = AcpStdioServer(agent, input_stream, io.StringIO())
    server.serve()
    assert agent.lease.closed == 1


def test_acp_normal_turn_emits_one_message_update_then_end_turn():
    handle = _Handle(
        "placeholder",
        TurnResult("placeholder", TurnOutcome.NORMAL, text="hello from LingTai"),
    )
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)

    _request(server, "prompt-1", "session/prompt", {
        "sessionId": session_id,
        "prompt": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": " world"},
        ],
    })
    messages = _wait_for(output, 4)

    assert agent.submissions[0]["content"] == "hello world"
    assert messages[2] == {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello from LingTai"},
            },
        },
    }
    assert messages[3] == {
        "jsonrpc": "2.0",
        "id": "prompt-1",
        "result": {"stopReason": "end_turn"},
    }
    # Every stdout line is independently parseable JSON and the serializer
    # escaped content instead of emitting embedded physical newlines.
    for line in output.getvalue().splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"
    server.close()


def test_session_cancel_notification_settles_original_prompt_cancelled():
    handle = _Handle("placeholder")
    output = io.StringIO()
    server = AcpStdioServer(_Agent(handle), io.StringIO(), output)
    session_id = _open_session(server, output)

    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "wait"}],
    })
    server._dispatch({
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": session_id},
    })

    messages = _wait_for(output, 3)
    assert messages[-1] == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"stopReason": "cancelled"},
    }
    assert all(message.get("id") is not None for message in messages)
    server.close()


def test_failure_busy_second_session_and_unsupported_inputs_are_explicit():
    handle = _Handle("placeholder")
    output = io.StringIO()
    server = AcpStdioServer(_Agent(handle), io.StringIO(), output)
    session_id = _open_session(server, output)

    _request(server, 3, "session/new", {"cwd": "/tmp", "mcpServers": []})
    _request(server, 4, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "wait"}],
    })
    _request(server, 5, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "second"}],
    })
    messages = _wait_for(output, 4)
    errors = {message.get("id"): message.get("error") for message in messages}
    assert errors[3]["code"] == -32004
    assert errors[5]["code"] == -32010
    assert errors[5]["code"] not in {-32000, -32001, -32002}

    handle._future.set_result(
        TurnResult(handle.correlation_id, TurnOutcome.FAILED, error="secret detail")
    )
    messages = _wait_for(output, 5)
    assert messages[-1] == {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {"code": -32603, "message": "LingTai turn failed"},
    }
    assert "secret detail" not in output.getvalue()
    server.close()

    for request_id, new_params in (
        (10, {"cwd": "/tmp", "mcpServers": [{"name": "x"}]}),
        (11, {"cwd": "relative", "mcpServers": []}),
    ):
        out = io.StringIO()
        fresh = AcpStdioServer(_Agent(_Handle("x")), io.StringIO(), out)
        _request(fresh, 1, "initialize", {"protocolVersion": 1})
        _request(fresh, request_id, "session/new", new_params)
        assert _wait_for(out, 2)[-1]["error"]["code"] in {-32004, -32602}
        fresh.close()


def test_initialize_negotiates_v1_and_enforces_rpc_method_kinds():
    output = io.StringIO()
    server = AcpStdioServer(_Agent(_Handle("x")), io.StringIO(), output)

    # Stable ACP negotiation returns the Agent's latest supported version when
    # the requested integer version is unsupported; this process remains v1-only.
    _request(server, 1, "initialize", {"protocolVersion": 2})
    assert _wait_for(output, 1)[0]["result"]["protocolVersion"] == 1

    _request(server, 2, "session/cancel", {"sessionId": "missing"})
    assert _wait_for(output, 2)[-1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": -32600,
            "message": "session/cancel must be a notification",
        },
    }
    server.close()

    # A request-only method sent as a notification gets no JSON-RPC response and
    # must not mutate initialization state.
    fresh_output = io.StringIO()
    fresh = AcpStdioServer(_Agent(_Handle("x")), io.StringIO(), fresh_output)
    fresh._dispatch({
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {"protocolVersion": 1},
    })
    _request(fresh, 3, "session/new", {"cwd": "/tmp", "mcpServers": []})
    assert _wait_for(fresh_output, 1) == [{
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32011, "message": "server is not initialized"},
    }]
    assert _messages(fresh_output)[0]["error"]["code"] not in {
        -32000, -32001, -32002
    }
    fresh.close()


def test_serve_uses_newline_delimited_strict_json_and_clean_eof():
    input_stream = io.StringIO(
        "not json\n"
        + json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": 1},
        })
        + "\n"
    )
    output = io.StringIO()
    server = AcpStdioServer(_Agent(_Handle("x")), input_stream, output)

    server.serve()

    messages = _wait_for(output, 2)
    assert messages[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert messages[1]["result"]["protocolVersion"] == 1
    assert output.getvalue().endswith("\n")


def test_cli_composition_quarantines_application_stdout_and_stops_agent(
    tmp_path, monkeypatch
):
    import lingtai.adapters.acp as acp_package
    import lingtai.cli as cli
    import lingtai.cli_acp as cli_acp
    import lingtai.kernel.logging as kernel_logging
    import lingtai.venv_resolve as venv_resolve

    class FakeAgent:
        def __init__(self):
            self.started = False
            self.stopped = False
            self._llm_worker_interface_poisoned = False

        def start(self):
            self.started = True
            print("boot noise")

        def stop(self, timeout=0):
            self.stopped = True
            print("stop noise")
            return StopResult(StopStatus.STOPPED, False, False)

    fake_agent = FakeAgent()

    class FakeServer:
        def __init__(self, agent, input_stream, output_stream):
            self.agent = agent
            self.output = output_stream

        def serve(self):
            print("runtime noise")
            self.output.write('{"jsonrpc":"2.0","id":1,"result":{}}\n')

        def close(self):
            pass

    monkeypatch.setattr(cli, "_check_duplicate_process", lambda _path: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda _path: None)
    monkeypatch.setattr(cli, "load_init", lambda _path: {})
    monkeypatch.setattr(cli, "build_agent", lambda _data, _path: fake_agent)
    monkeypatch.setattr(cli, "_force_exit_if_worker_poisoned", lambda _agent: None)
    monkeypatch.setattr(kernel_logging, "setup_logging", lambda **_kw: None)
    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda _data: tmp_path / "venv")
    monkeypatch.setattr(acp_package, "AcpStdioServer", FakeServer)

    wire = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(cli_acp.sys, "stderr", stderr)
    cli_acp.run_acp(tmp_path, input_stream=io.StringIO(), output_stream=wire)

    assert fake_agent.started and fake_agent.stopped
    assert _messages(wire) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert "boot noise" in stderr.getvalue()
    assert "runtime noise" in stderr.getvalue()
    assert "stop noise" in stderr.getvalue()

    class BrokenPipeServer(FakeServer):
        def serve(self):
            raise BrokenPipeError("client closed stdout")

    fake_agent.started = False
    fake_agent.stopped = False
    monkeypatch.setattr(acp_package, "AcpStdioServer", BrokenPipeServer)
    cli_acp.run_acp(
        tmp_path,
        input_stream=io.StringIO(),
        output_stream=io.StringIO(),
    )
    assert fake_agent.started and fake_agent.stopped


@pytest.mark.parametrize(
    ("forced_disable", "expected_allow_session_mcp"),
    [
        (None, True),
        (frozenset(), True),
        (frozenset({"avatar"}), True),
        (frozenset({"mcp"}), False),
    ],
)
def test_cli_forced_disable_preserves_generic_session_mcp_semantics(
    tmp_path, monkeypatch, forced_disable, expected_allow_session_mcp
):
    """Generic ACP disables session MCP only when its forced policy names MCP."""

    import lingtai.adapters.acp as acp_package
    import lingtai.cli as cli
    import lingtai.cli_acp as cli_acp
    import lingtai.kernel.logging as kernel_logging
    import lingtai.venv_resolve as venv_resolve

    class FakeAgent:
        _llm_worker_interface_poisoned = False

        def start(self):
            return None

        def stop(self, timeout=0):
            return StopResult(StopStatus.STOPPED, False, False)

    observed = {}

    class FakeServer:
        def __init__(
            self,
            _agent,
            _input,
            _output,
            *,
            fixed_execution_workspace=None,
            allow_session_mcp=True,
        ):
            observed["fixed_execution_workspace"] = fixed_execution_workspace
            observed["allow_session_mcp"] = allow_session_mcp

        def serve(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(cli, "_check_duplicate_process", lambda _path: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda _path: None)
    monkeypatch.setattr(cli, "load_init", lambda _path: {})
    monkeypatch.setattr(cli, "build_agent", lambda _data, _path, **_kw: FakeAgent())
    monkeypatch.setattr(cli, "_force_exit_if_worker_poisoned", lambda _agent: None)
    monkeypatch.setattr(kernel_logging, "setup_logging", lambda **_kw: None)
    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda _data: tmp_path / "venv")
    monkeypatch.setattr(acp_package, "AcpStdioServer", FakeServer)

    cli_acp.run_acp(
        tmp_path,
        input_stream=io.StringIO(),
        output_stream=io.StringIO(),
        forced_disable=forced_disable,
    )

    assert observed["allow_session_mcp"] is expected_allow_session_mcp


def test_cli_poison_force_exit_skips_log_after_successful_stop_releases_lease(
    tmp_path, monkeypatch
):
    import lingtai.adapters.acp as acp_package
    import lingtai.cli as cli
    import lingtai.cli_acp as cli_acp
    import lingtai.kernel.logging as kernel_logging
    import lingtai.venv_resolve as venv_resolve

    class FakeAgent:
        def __init__(self):
            self._llm_worker_interface_poisoned = True
            self._llm_worker_poison_artifact = "history/unfinished_turns/test.json"
            self._workdir_lease_acquired = True
            self.stopped = False
            self.log_attempts = 0

        def start(self):
            return None

        def stop(self, timeout=0):
            self.stopped = True
            self._workdir_lease_acquired = False
            return StopResult(StopStatus.STOPPED, False, False)

        def _log(self, _name, **_fields):
            self.log_attempts += 1
            if not self._workdir_lease_acquired:
                raise AssertionError("workdir log attempted after lease release")

    fake_agent = FakeAgent()

    class FakeServer:
        def __init__(self, _agent, _input_stream, _output_stream):
            pass

        def serve(self):
            return None

        def close(self):
            return None

    exit_codes: list[int] = []

    def fake_exit(code):
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(cli, "_check_duplicate_process", lambda _path: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda _path: None)
    monkeypatch.setattr(cli, "load_init", lambda _path: {})
    monkeypatch.setattr(cli, "build_agent", lambda _data, _path: fake_agent)
    monkeypatch.setattr(cli.os, "_exit", fake_exit)
    monkeypatch.setattr(kernel_logging, "setup_logging", lambda **_kw: None)
    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda _data: tmp_path / "venv")
    monkeypatch.setattr(acp_package, "AcpStdioServer", FakeServer)
    monkeypatch.setattr(cli_acp.sys, "stderr", io.StringIO())

    original_stdout = cli_acp.sys.stdout
    try:
        with pytest.raises(SystemExit) as exc_info:
            cli_acp.run_acp(
                tmp_path,
                input_stream=io.StringIO(),
                output_stream=io.StringIO(),
            )
    finally:
        # Production os._exit never returns; the sentinel must not leave the
        # test process with run_acp's deliberate stdout quarantine installed.
        cli_acp.sys.stdout = original_stdout

    assert exc_info.value.code == 0
    assert exit_codes == [0]
    assert fake_agent.stopped
    assert not fake_agent._workdir_lease_acquired
    assert fake_agent.log_attempts == 0


def test_resource_link_baseline_is_validated_and_projected_into_core_text():
    handle = _Handle(
        "placeholder",
        TurnResult("placeholder", TurnOutcome.NORMAL, text="linked"),
    )
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)

    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{
            "type": "resource_link",
            "uri": "file:///tmp/example.py",
            "name": "example.py",
            "mimeType": "text/x-python",
            "size": 42,
        }],
    })
    _wait_for(output, 4)

    projected = json.loads(agent.submissions[0]["content"].strip())
    assert projected == {
        "type": "resource_link",
        "uri": "file:///tmp/example.py",
        "name": "example.py",
        "mimeType": "text/x-python",
        "size": 42,
    }
    server.close()

    invalid_output = io.StringIO()
    invalid_server = AcpStdioServer(
        _Agent(_Handle("invalid")),
        io.StringIO(),
        invalid_output,
    )
    invalid_session = _open_session(invalid_server, invalid_output)
    _request(invalid_server, 4, "session/prompt", {
        "sessionId": invalid_session,
        "prompt": [{"type": "resource_link", "uri": "file:///tmp/missing-name"}],
    })
    assert _wait_for(invalid_output, 3)[-1]["error"]["code"] == -32602
    invalid_server.close()


def test_agent_shutdown_unblocks_open_stdin_and_suppresses_late_prompt_output():
    class BlockingInput:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def __iter__(self):
            return self

        def __next__(self):
            self.entered.set()
            assert self.release.wait(timeout=5)
            raise StopIteration

    blocking_input = BlockingInput()
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    output = io.StringIO()
    server = AcpStdioServer(agent, blocking_input, output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "wait"}],
    })

    worker = threading.Thread(target=server.serve)
    worker.start()
    assert blocking_input.entered.wait(timeout=5)
    # Production ordering is shutdown first, then correlated-handle settlement,
    # before the server's 100ms poll necessarily observes either condition.
    agent._shutdown.set()
    handle.cancel()
    worker.join(timeout=2)
    blocking_input.release.set()

    assert not worker.is_alive(), "Agent stop must not wait for client EOF"
    assert handle.result(timeout=1).outcome is TurnOutcome.CANCELLED
    time.sleep(0.05)
    assert all(message.get("id") != 3 for message in _messages(output))


def test_invalid_utf8_reader_failure_returns_parse_error_without_traceback():
    class InvalidUtf8Input:
        def __iter__(self):
            return self

        def __next__(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    output = io.StringIO()
    server = AcpStdioServer(
        _Agent(_Handle("invalid-utf8")),
        InvalidUtf8Input(),
        output,
    )

    server.serve()

    assert _wait_for(output, 1) == [{
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }]


def test_blocked_terminal_stdout_does_not_hold_close_or_agent_teardown_lock():
    class BlockingOutput(io.StringIO):
        def __init__(self):
            super().__init__()
            self.write_count = 0
            self.blocked = threading.Event()
            self.release = threading.Event()

        def write(self, value):
            self.write_count += 1
            if self.write_count >= 3:
                self.blocked.set()
                assert self.release.wait(timeout=5)
            return super().write(value)

    handle = _Handle(
        "placeholder",
        TurnResult("placeholder", TurnOutcome.NORMAL, text="blocking output"),
    )
    output = BlockingOutput()
    server = AcpStdioServer(_Agent(handle), io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "respond"}],
    })
    assert output.blocked.wait(timeout=5)

    closer = threading.Thread(target=server.close)
    closer.start()
    closer.join(timeout=1)
    output.release.set()

    assert not closer.is_alive(), "blocked client output must not hold state teardown"
    messages = _wait_for(output, 3)
    assert any(message.get("method") == "session/update" for message in messages)
    assert all(message.get("id") != 3 for message in messages)


class _QueuedInput:
    def __init__(self):
        self._items: queue.Queue[str | None] = queue.Queue()

    def send(self, message):
        line = message if isinstance(message, str) else json.dumps(message)
        self._items.put(line if line.endswith("\n") else line + "\n")

    def eof(self):
        self._items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._items.get(timeout=5)
        if item is None:
            raise StopIteration
        return item


class _BlockingOutput(io.StringIO):
    def __init__(self, *, block_on: int):
        super().__init__()
        self.block_on = block_on
        self.write_count = 0
        self.blocked = threading.Event()
        self.release = threading.Event()

    def write(self, value):
        self.write_count += 1
        if self.write_count == self.block_on:
            self.blocked.set()
            assert self.release.wait(timeout=5)
        return super().write(value)


def _wait_until(predicate, *, timeout=5, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message}")


def test_blocked_coordinator_initialize_write_cannot_hold_shutdown():
    input_stream = _QueuedInput()
    output = _BlockingOutput(block_on=1)
    agent = _Agent(_Handle("unused"))
    server = AcpStdioServer(agent, input_stream, output)
    coordinator = threading.Thread(target=server.serve)
    coordinator.start()
    input_stream.send({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": 1},
    })
    assert output.blocked.wait(timeout=5)

    agent._shutdown.set()
    coordinator.join(timeout=1)

    assert not coordinator.is_alive(), "coordinator must never wait on stdout"
    assert not output.release.is_set(), "test must prove return while write is blocked"
    output.release.set()


def test_blocked_prompt_write_does_not_block_later_coordinator_response_or_shutdown():
    handle = _Handle(
        "placeholder",
        TurnResult("placeholder", TurnOutcome.NORMAL, text="first frame blocks"),
    )
    agent = _Agent(handle)
    output = _BlockingOutput(block_on=3)
    input_stream = _QueuedInput()
    server = AcpStdioServer(agent, input_stream, output)
    session_id = _open_session(server, output)
    coordinator = threading.Thread(target=server.serve)
    coordinator.start()

    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "respond"}],
    })
    assert output.blocked.wait(timeout=5)
    input_stream.send({
        "jsonrpc": "2.0",
        "id": 99,
        "method": "unknown/method",
        "params": {},
    })
    _wait_until(lambda: server._outbound.qsize() >= 1, message="queued coordinator error")

    agent._shutdown.set()
    coordinator.join(timeout=1)
    assert not coordinator.is_alive()
    assert not output.release.is_set()

    output.release.set()
    messages = _wait_for(output, 4)
    assert any(message.get("id") == 99 for message in messages)
    assert all(message.get("id") != 3 for message in messages)


def test_prompt_terminal_batch_is_fifo_adjacent_before_later_coordinator_response():
    handle = _Handle(
        "placeholder",
        TurnResult("placeholder", TurnOutcome.NORMAL, text="atomic update"),
    )
    output = _BlockingOutput(block_on=3)
    server = AcpStdioServer(_Agent(handle), io.StringIO(), output)
    session_id = _open_session(server, output)

    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "respond"}],
    })
    assert output.blocked.wait(timeout=5)
    _request(server, 99, "unknown/method", {})
    _wait_until(
        lambda: server._outbound.qsize() >= 1,
        message="later coordinator response queued",
    )

    output.release.set()
    messages = _wait_for(output, 5)

    assert messages[2]["method"] == "session/update"
    assert messages[3] == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"stopReason": "end_turn"},
    }
    assert messages[4]["id"] == 99
    assert messages[4]["error"]["code"] == -32601
    server.close()


def test_close_after_terminal_claim_before_enqueue_suppresses_entire_prompt_batch(monkeypatch):
    handle = _Handle("placeholder")
    output = io.StringIO()
    server = AcpStdioServer(_Agent(handle), io.StringIO(), output)
    session_id = _open_session(server, output)
    claimed = threading.Event()
    proceed = threading.Event()
    original_enqueue = server._enqueue_messages

    def gated_enqueue(
        messages, *, generation=None, active=None, settles_active=False
    ):
        if active is not None:
            claimed.set()
            assert proceed.wait(timeout=5)
        return original_enqueue(
            messages,
            generation=generation,
            active=active,
            settles_active=settles_active,
        )

    monkeypatch.setattr(server, "_enqueue_messages", gated_enqueue)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "respond"}],
    })
    handle._future.set_result(
        TurnResult(handle.correlation_id, TurnOutcome.NORMAL, text="late")
    )
    assert claimed.wait(timeout=5)

    server.close()
    proceed.set()
    _wait_until(lambda: not server._prompt_threads, message="prompt waiter exit")

    assert len(_messages(output)) == 2
    assert all(message.get("id") != 3 for message in _messages(output))


def test_outbound_queue_full_aborts_transport_and_cancels_active_prompt(monkeypatch):
    monkeypatch.setattr(AcpStdioServer, "_OUTBOUND_QUEUE_BATCHES", 1)
    output = _BlockingOutput(block_on=1)
    handle = _Handle("placeholder")
    agent = _Agent(handle)
    server = AcpStdioServer(agent, io.StringIO(), output)

    _request(server, 1, "initialize", {"protocolVersion": 1})
    assert output.blocked.wait(timeout=5)
    _request(server, 2, "session/new", {"cwd": "/tmp", "mcpServers": []})
    _request(server, 3, "session/prompt", {
        "sessionId": server._session_id,
        "prompt": [{"type": "text", "text": "wait"}],
    })
    broker = agent.submissions[0]["permission_broker"]
    assert broker.request_permission(
        ToolPermissionRequest("queue-full", "file")
    ) is PermissionDecision.DENY

    assert server._aborted and server._closing
    assert server._active is None
    assert handle.result(timeout=1).outcome is TurnOutcome.CANCELLED
    output.release.set()


def test_serialization_failure_aborts_transport_and_cancels_active_prompt():
    handle = _Handle("placeholder")
    output = io.StringIO()
    server = AcpStdioServer(_Agent(handle), io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "wait"}],
    })
    _wait_until(lambda: server._active is not None, message="active prompt")

    accepted = server._enqueue_messages(({
        "jsonrpc": "2.0",
        "id": 99,
        "result": {"not_json": object()},
    },))

    assert not accepted
    assert server._aborted and server._closing
    assert server._active is None
    assert handle.result(timeout=1).outcome is TurnOutcome.CANCELLED


@pytest.mark.parametrize("failure", ("write", "flush"))
def test_stdout_write_or_flush_failure_aborts_and_cancels_active_prompt(failure):
    class FailingOutput(io.StringIO):
        def __init__(self):
            super().__init__()
            self.write_count = 0
            self.flush_count = 0

        def write(self, value):
            self.write_count += 1
            if failure == "write" and self.write_count == 3:
                raise OSError("ACP stdout write failed")
            return super().write(value)

        def flush(self):
            self.flush_count += 1
            if failure == "flush" and self.flush_count == 3:
                raise OSError("ACP stdout flush failed")
            return super().flush()

    handle = _Handle("placeholder")
    output = FailingOutput()
    agent = _Agent(handle)
    server = AcpStdioServer(agent, io.StringIO(), output)
    session_id = _open_session(server, output)
    _request(server, 3, "session/prompt", {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": "wait"}],
    })
    _wait_until(lambda: server._active is not None, message="active prompt")
    broker = agent.submissions[0]["permission_broker"]
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        broker.request_permission(ToolPermissionRequest("transport-failure", "shell"))
    ))
    waiter.start()

    _wait_until(lambda: server._aborted, message=f"{failure} abort")
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert result == [PermissionDecision.DENY]
    assert server._closing
    assert server._active is None
    assert handle.result(timeout=1).outcome is TurnOutcome.CANCELLED


def test_short_stdout_write_fatally_aborts_transport():
    class ShortWriteOutput(io.StringIO):
        def write(self, value):
            super().write(value[:-1])
            return len(value) - 1

    server = AcpStdioServer(
        _Agent(_Handle("unused")),
        io.StringIO(),
        ShortWriteOutput(),
    )
    _request(server, 1, "initialize", {"protocolVersion": 1})
    _wait_until(lambda: server._aborted, message="short-write abort")
    assert server._closing


def test_cli_real_server_reaches_typed_stop_while_stdout_writer_is_blocked(
    tmp_path, monkeypatch
):
    import lingtai.adapters.acp as acp_package
    import lingtai.cli as cli
    import lingtai.cli_acp as cli_acp
    import lingtai.kernel.logging as kernel_logging
    import lingtai.venv_resolve as venv_resolve

    class FakeAgent:
        def __init__(self):
            self._shutdown = threading.Event()
            self._llm_worker_interface_poisoned = False
            self.stop_called = threading.Event()

        def start(self):
            pass

        def stop(self, timeout=0):
            self.stop_called.set()
            return StopResult(StopStatus.STOPPED, False, False)

    fake_agent = FakeAgent()
    input_stream = _QueuedInput()
    output = _BlockingOutput(block_on=1)
    monkeypatch.setattr(cli, "_check_duplicate_process", lambda _path: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda _path: None)
    monkeypatch.setattr(cli, "load_init", lambda _path: {})
    monkeypatch.setattr(cli, "build_agent", lambda _data, _path: fake_agent)
    monkeypatch.setattr(cli, "_force_exit_if_worker_poisoned", lambda _agent: None)
    monkeypatch.setattr(kernel_logging, "setup_logging", lambda **_kw: None)
    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda _data: tmp_path / "venv")
    monkeypatch.setattr(acp_package, "AcpStdioServer", AcpStdioServer)

    def feed_and_stop():
        input_stream.send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": 1},
        })
        assert output.blocked.wait(timeout=5)
        fake_agent._shutdown.set()

    feeder = threading.Thread(target=feed_and_stop)
    feeder.start()
    cli_acp.run_acp(tmp_path, input_stream=input_stream, output_stream=output)
    feeder.join(timeout=1)

    assert fake_agent.stop_called.is_set()
    assert not output.release.is_set(), "CLI stop must be reachable with stdout blocked"
    output.release.set()


def test_cli_timeout_stop_hard_exits_before_releasing_ownership(tmp_path, monkeypatch):
    import lingtai.adapters.acp as acp_package
    import lingtai.cli as cli
    import lingtai.cli_acp as cli_acp
    import lingtai.kernel.logging as kernel_logging
    import lingtai.venv_resolve as venv_resolve

    class ForceExit(BaseException):
        def __init__(self, code):
            self.code = code

    class FakeAgent:
        _llm_worker_interface_poisoned = False

        def __init__(self):
            self.lease_released = False
            self.logged = []

        def start(self):
            pass

        def stop(self, timeout=0):
            return StopResult(StopStatus.TIMED_OUT, True, True)

        def _log(self, event, **fields):
            self.logged.append((event, fields))

    class FakeServer:
        def __init__(self, *args):
            pass

        def serve(self):
            return

        def close(self):
            return

    fake_agent = FakeAgent()
    monkeypatch.setattr(cli, "_check_duplicate_process", lambda _path: None)
    monkeypatch.setattr(cli, "_clean_signal_files", lambda _path: None)
    monkeypatch.setattr(cli, "load_init", lambda _path: {})
    monkeypatch.setattr(cli, "build_agent", lambda _data, _path: fake_agent)
    monkeypatch.setattr(cli, "_force_exit_if_worker_poisoned", lambda _agent: None)
    monkeypatch.setattr(kernel_logging, "setup_logging", lambda **_kw: None)
    monkeypatch.setattr(venv_resolve, "resolve_venv", lambda _data: tmp_path / "venv")
    monkeypatch.setattr(acp_package, "AcpStdioServer", FakeServer)
    monkeypatch.setattr(cli_acp.os, "_exit", lambda code: (_ for _ in ()).throw(ForceExit(code)))

    original_stdout = cli_acp.sys.stdout
    try:
        try:
            cli_acp.run_acp(
                tmp_path,
                input_stream=io.StringIO(),
                output_stream=io.StringIO(),
            )
        except ForceExit as exc:
            assert exc.code == 70
        else:  # pragma: no cover - safety failure
            raise AssertionError("incomplete ACP stop must process-terminate")
    finally:
        cli_acp.sys.stdout = original_stdout

    assert not fake_agent.lease_released
    assert fake_agent.logged[-1][0] == "acp_force_exit_after_incomplete_stop"


def test_incomplete_stop_skips_workdir_log_after_lease_release(monkeypatch):
    import lingtai.cli_acp as cli_acp

    class ForceExit(BaseException):
        pass

    def forbidden_log(*_args, **_kwargs):
        raise AssertionError("must not write workdir state after lease release")

    agent = SimpleNamespace(
        _workdir_lease_acquired=False,
        _log=forbidden_log,
    )
    monkeypatch.setattr(
        cli_acp.os,
        "_exit",
        lambda _code: (_ for _ in ()).throw(ForceExit()),
    )

    with pytest.raises(ForceExit):
        cli_acp._force_exit_after_incomplete_stop(
            agent,
            stop_result=None,
            stop_error=RuntimeError("cleanup failed after quiescence"),
        )
