"""OpenAI adapter — wraps the ``openai`` SDK for OpenAI and compatible APIs.

Covers: OpenAI, DeepSeek, Together AI, Groq, Fireworks, Ollama, vLLM,
and any other provider exposing an OpenAI-compatible ``/chat/completions``
endpoint.

This is the **only** module that imports the ``openai`` package.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import openai

from lingtai.kernel.logging import get_logger
from lingtai.kernel.config import (
    THINKING_LEVELS,
    CONTEXT_PRESSURE_FORCED_REBUILD_RATIO,
    CONTEXT_PRESSURE_RECOVERY_TARGET,
)

from lingtai.kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMReplayTerminalError,
    LLMResponse,
    ToolCall,
    UsageMetadata,
    mark_llm_replay_terminal,
    safe_exception_description,
    wire_tool_description,
)
from lingtai.kernel.llm.interface import ToolResultBlock
from lingtai.kernel.llm.reasoning_effort import (
    UNAVAILABLE_CAPABILITY,
    ReasoningEffortCapability,
    ReasoningEffortSnapshot,
)
from lingtai.llm.base import LLMAdapter
from .codex_effort import CodexEffortDescriptor, resolve_codex_effort_descriptor
from lingtai.kernel.llm.interface import ChatInterface, TextBlock, ThinkingBlock, ToolCallBlock
from ..interface_converters import to_openai, to_responses_input
from lingtai.kernel.llm.streaming import OutputProgress, StreamingAccumulator
from lingtai.llm.identity_headers import lingtai_user_agent, merge_lingtai_identity_headers
from lingtai.kernel.token_counter import count_tokens

logger = get_logger()


def _env_bool(name: str, default: bool) -> bool:
    """Parse an env var as a boolean with a default when unset/invalid.

    Accepts 1/true/yes/on (case-insensitive) as true and
    0/false/no/off as false; anything else falls back to ``default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


class _OpenAIToolPairingError(ValueError):
    """Sanitized structural failure at the Chat Completions wire boundary."""

    def __init__(self, reason: str, *, phase: str = "wire_validation"):
        self.reason = reason
        self.phase = phase
        super().__init__(f"tool pairing rejected: reason={reason} phase={phase}")


def _validate_openai_tool_pairing(messages: list[dict]) -> None:
    """Fail closed unless every Chat Completions tool batch is positional.

    A role=tool run is valid only immediately after one assistant tool-call
    batch and only when its IDs cover that batch exactly once.  The final
    assistant tool-call entry may remain as the intentional live tail; the
    existing local placeholder guard normally closes it before this check.
    """
    if not isinstance(messages, list):
        raise _OpenAIToolPairingError("invalid_message_list")

    known_call_ids: set[str] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            raise _OpenAIToolPairingError("invalid_message")
        role = message.get("role")
        if role == "tool":
            result_id = message.get("tool_call_id")
            if not isinstance(result_id, str) or not result_id:
                raise _OpenAIToolPairingError("idless")
            reason = "non_contiguous" if result_id in known_call_ids else "standalone"
            raise _OpenAIToolPairingError(reason)
        if role != "assistant" or not message.get("tool_calls"):
            index += 1
            continue

        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            raise _OpenAIToolPairingError("invalid_call_batch")
        expected: list[str] = []
        for call in calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(call_id, str) or not call_id:
                raise _OpenAIToolPairingError("idless_call")
            if call_id in expected:
                raise _OpenAIToolPairingError("duplicate_call")
            expected.append(call_id)
        known_call_ids.update(expected)

        result_index = index + 1
        if result_index == len(messages):
            # This is the intentional final live tail before closure.
            index = result_index
            continue
        if not isinstance(messages[result_index], dict) or messages[result_index].get("role") != "tool":
            raise _OpenAIToolPairingError("non_contiguous")

        actual: list[str] = []
        while result_index < len(messages):
            result = messages[result_index]
            if not isinstance(result, dict) or result.get("role") != "tool":
                break
            result_id = result.get("tool_call_id")
            if not isinstance(result_id, str) or not result_id:
                raise _OpenAIToolPairingError("idless")
            if result_id in actual:
                raise _OpenAIToolPairingError("duplicate")
            actual.append(result_id)
            result_index += 1
        if set(actual) != set(expected):
            raise _OpenAIToolPairingError("wrong_batch")
        index = result_index


_CODEX_RESPONSES_TRACE_ENV = "LINGTAI_CODEX_RESPONSES_TRACE"
_CODEX_RESPONSES_TRACE_PATH_ENV = "LINGTAI_CODEX_RESPONSES_TRACE_PATH"
_CODEX_RESPONSES_TRACE_FILE = "codex_responses_trace.jsonl"


# Sentinel for "auto-derive a default prompt_cache_key". The adapter accepts
# ``prompt_cache_key=None`` to mean "compute the stable default", an explicit
# string to override it, and ``False`` to disable cache-key emission entirely.
_AUTO_PROMPT_CACHE_KEY = object()


def _is_codex_unverifiable_encrypted_content_error(exc: BaseException) -> bool:
    """Return True for Codex 400s caused by stale encrypted reasoning blobs.

    The ChatGPT Codex Responses endpoint may reject a replayed reasoning item
    with messages like ``The encrypted content for item rs_... could not be
    verified``.  That is a narrow history-state failure: the opaque encrypted
    reasoning blob is no longer accepted, but the visible transcript can usually
    continue if we fall back to summary/plain replay.
    """

    parts = [safe_exception_description(exc)]
    for attr in ("message", "body"):
        value = getattr(exc, attr, None)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            parts.append(json.dumps(value, default=str, ensure_ascii=False))
        else:
            parts.append(str(value))
    text = "\n".join(parts).lower()
    return "encrypted content" in text and "could not be verified" in text


# Codex REST cache-affinity headers (issue #378). The official Codex client
# sends ``session_id`` / ``thread_id`` headers on its
# ``/backend-api/codex/responses`` calls; a probe showed they materially
# improve prompt-cache affinity for full requests and fallback/replay turns.
# REST transport always sends a self-contained converted input. Its
# ``incremental`` mode is a cache/epoch semantic (unchanged prefix, same stable
# cache-affinity headers), not a wire-delta semantic. ``previous_response_id`` is
# WebSocket-only for Codex.
#
# The header keys MUST be the underscore names ``session_id`` / ``thread_id``
# exactly as the Codex backend/CLI sends them. Do NOT "normalise" them into
# hyphenated, HTTP-looking ``session-id`` / ``thread-id``: the Codex backend
# matches the literal underscore key, so a hyphenated spelling silently loses
# cache affinity — every request fragments to a cold slot, exploding cache
# misses and token cost. (This comment block uses the spelling the code must
# emit; keep prose and code in sync.)
#
# For the normal/root main Codex session, the three cache-affinity values are
# byte-identical:
#
#     session_id == thread_id == prompt_cache_key == <8-char (agent-path, molt) hash>
#
# The shared value is a deterministic 8-character lowercase-hex digest of the
# agent's durable identity anchor (the resolved ``init.json`` / agent path)
# COMBINED with the agent's current ``molt_count`` (read from ``.agent.json``).
# It is stable WITHIN a molt segment — ordinary LLM calls, ``api_call_id``
# rotation, refresh/rebuild, and clear (same agent path, same molt_count) all
# leave it unchanged — and it INTENTIONALLY changes at each molt boundary, so a
# molt starts on a fresh cache slot. A different agent path also changes it. We
# do NOT use the latest ``api_call_id``, molt *time*, or generated UUIDs for
# these defaults. Because molt does not rebuild the adapter, the id is derived
# at request time from the live ``molt_count`` — never cached once at
# construction (see ``_resolve_codex_ids`` / ``_default_prompt_cache_key``).
#
# IMPORTANT — these identifiers MUST be per-agent. The value anchors on the
# agent's durable identity (the resolved ``init.json`` path). It must NOT be
# derived from a global, model-only anchor (e.g.
# ``prompt_cache_key=lingtai-codex:{model}:v1``): every agent on the same model
# shares that string, which would collapse all of them onto one
# session/thread and is exactly the wrong behavior.
#
# The adapter layer has no per-agent identity of its own, so the host wiring
# (``lingtai/llm/service.py:build_provider_defaults_from_manifest_llm``) passes
# the agent path down by default as ``codex_session_anchor``: a normal Codex
# agent gets a per-agent (anchor, molt_count) hash used identically for all
# three values. The constructor kwarg below is the seam those defaults flow
# through. There is intentionally NO operator-level fixed-id override: the
# identity is always the anchor+molt hash (or absent when there is no anchor).


def _codex_session_id(anchor: str, molt_count: int) -> str:
    """Derive the 8-char Codex cache-affinity id from ``anchor`` + ``molt_count``.

    ``anchor`` MUST be a per-agent identity string (e.g. the agent's resolved
    ``init.json`` / agent-dir path), NOT a global model-only key. ``molt_count``
    is the agent's current molt count (read from ``.agent.json`` at request
    time). The result is a deterministic 8-character lowercase-hex sha256 prefix
    of ``f"{anchor}\\0{molt_count}"``: the same (anchor, molt_count) pair always
    yields the same id; a different anchor OR a different molt_count yields a
    different id. The same value is used byte-identically for ``session_id``,
    ``thread_id``, and the default ``prompt_cache_key`` on the normal/root path.

    The NUL separator keeps the (anchor, molt_count) encoding unambiguous so two
    distinct pairs can never collide via string concatenation.
    """
    seed = f"{anchor}\0{molt_count}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]

# Codex cache-affinity identity is a per-agent value derived from the agent's
# durable identity anchor (the resolved ``init.json`` / agent-dir path) AND the
# agent's current molt count, used byte-identically for ``session_id`` /
# ``thread_id`` / ``prompt_cache_key``. See :func:`_codex_session_id`.
#
# It is STABLE within a molt segment (no time/epoch/rotation churn within a
# molt) and intentionally CHANGES at each molt boundary so a molt starts on a
# fresh cache slot. The molt path does NOT rebuild the adapter, so the id MUST
# be (re)computed at request time from the live ``.agent.json`` molt_count — it
# is never cached once at construction (see ``_resolve_codex_ids`` /
# ``_default_prompt_cache_key``).
#
# Historically there were two OTHER churn mechanisms here, both REMOVED because
# they were empirically counterproductive (the backend routes the prompt cache
# to a sticky-warm replica off a stable session id; churning it re-rolls the
# routing and discards the warm slot):
#   - epoch-stamping the id on every adapter (re)build, and
#   - a "stalled-cache" rotation that changed the id when the cache rate dipped.
# Both are gone — the only intentional id change is the molt boundary. The
# ``codex-cache-key`` request header (first chars of the prompt key) was part of
# the same churn apparatus and is no longer sent; Codex CLI never sends it
# either.


# Client-identity headers for the Codex ``/backend-api/codex/responses`` path.
#
# Default policy: identify LingTai honestly to ChatGPT Codex:
#   originator: lingtai
#   User-Agent: LingTai/<installed-version>
#
# During the #471 websocket/cache investigation we used an official Codex
# CLI-shaped identity as a local diagnostic. Keep that path as an explicit
# opt-in comparison switch only; do not ship impersonation as the default.
# Caller-supplied ``extra_headers`` still win over this base layer.
#
# Do not log bearer tokens or full request headers while changing this area.
_CODEX_IMPERSONATE_OFFICIAL_CLI = False

# Official Codex CLI app-name identity (version pinned to the installed
# ``codex-cli`` build we inspected). Kept as data so the official-shaped UA is
# a deliberate code switch rather than hidden string literals.
_CODEX_CLI_ORIGINATOR = "codex_cli_rs"
_CODEX_CLI_VERSION = "0.130.0"

# Honest LingTai identity (the shipped default).
_LINGTAI_ORIGINATOR = "lingtai"

# Effective originator for this build. Flipping the switch above swaps both the
# originator and the User-Agent together so they never disagree.
_CODEX_ORIGINATOR = (
    _CODEX_CLI_ORIGINATOR if _CODEX_IMPERSONATE_OFFICIAL_CLI else _LINGTAI_ORIGINATOR
)


def _codex_cli_user_agent() -> str:
    """Return an official-CLI-shaped ``User-Agent``, e.g.
    ``codex_cli_rs/0.130.0 (Darwin 23.4.0; arm64)``.

    Mirrors the official Codex CLI UA shape: ``{originator}/{version} ({os}
    {os_version}; {arch})``. The OS/arch suffix is best-effort — on failure we
    fall back to the bare ``{originator}/{version}`` token rather than raising.
    """
    base = f"{_CODEX_CLI_ORIGINATOR}/{_CODEX_CLI_VERSION}"
    try:
        import platform

        system = platform.system() or "unknown"
        release = platform.release() or ""
        machine = platform.machine() or ""
        return f"{base} ({system} {release}; {machine})".replace("  ", " ").strip()
    except Exception:
        return base


def _lingtai_user_agent() -> str:
    """Return the effective Codex ``User-Agent`` string.

    When ``_CODEX_IMPERSONATE_OFFICIAL_CLI`` is set, this
    returns the official-Codex-CLI-shaped UA so the app name matches what the
    ChatGPT backend recognizes (see the identity-policy note above). When the
    switch is off it returns the honest ``LingTai/<version>`` UA, falling back
    to an unversioned ``LingTai`` token if the package version can't be
    resolved.

    (The function name is retained for back-compat with existing imports/tests;
    it is the single resolver for the Codex identity UA regardless of policy.)
    """
    if _CODEX_IMPERSONATE_OFFICIAL_CLI:
        return _codex_cli_user_agent()
    try:
        return lingtai_user_agent()
    except Exception:
        return "LingTai"


def _codex_installation_id(anchor: str | None) -> str | None:
    """Return a stable, honest LingTai installation id for Codex metadata.

    Codex CLI sends an opaque UUID-shaped ``x-codex-installation-id``. LingTai
    must not borrow ``~/.codex/installation_id`` or impersonate the CLI, so we
    derive our own UUID-shaped identifier from the same non-secret local anchor
    used for Codex cache affinity. The raw path/anchor is never sent.
    """

    if not anchor:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lingtai-codex-installation:{anchor}"))

def _codex_identity_headers() -> dict[str, str]:
    """Return Codex client identity headers.

    The shipped default is explicit LingTai identity. The official-Codex-CLI
    app-name identity is available only when ``_CODEX_IMPERSONATE_OFFICIAL_CLI``
    is explicitly enabled for local protocol comparisons. The originator and
    User-Agent are always resolved together so they agree. See
    ``tests/test_codex_prompt_cache_key.py`` for the request-level guardrails.
    """
    return {"originator": _CODEX_ORIGINATOR, "User-Agent": _lingtai_user_agent()}


# ---------------------------------------------------------------------------
# Codex Responses-over-WebSocket incremental turn state (EXPERIMENTAL, #471).
#
# This mirrors the official Codex CLI source path (repo openai/codex, tag
# ``rust-v0.130.0``, commit 58573da). The high-cache/stateful behavior on the
# ChatGPT Codex backend is NOT server-side Responses storage (``store`` is
# ``false`` there by construction — ``codex-rs/core/src/client.rs:722``); it is
# Responses-over-WebSocket incremental ``response.create`` frames that carry
# ``previous_response_id`` plus only the delta input when the new full request is
# a strict extension of (previous request input + previous response output
# items). See ``get_incremental_items`` (``client.rs:949-985``) and
# ``prepare_websocket_request`` (``client.rs:998-1024``).
#
# These objects are pure Python data + a pure algorithm so the request-shape
# logic is unit-testable without any network. The actual websocket wire goes
# through an injectable transport (``_CodexWebsocketTransport``) so tests can
# substitute a fake.
# ---------------------------------------------------------------------------

# Official websocket beta header value (``client.rs:142``) and the per-turn
# sticky-routing state header (``client.rs:134`` /
# ``responses_websocket.rs:155``). Kept as data so the wire stays auditable.
_CODEX_WS_BETA_HEADER = "OpenAI-Beta"
_CODEX_WS_BETA_VALUE = "responses_websockets=2026-02-06"
_CODEX_TURN_STATE_HEADER = "x-codex-turn-state"

# ---------------------------------------------------------------------------
# Codex continuation TRANSPORT axis (REST vs WebSocket).
#
# Two orthogonal axes drive a Codex turn:
#   A. Continuation/transfer mode — ``full`` vs ``incremental`` (the strict
#      additive ``previous_response_id`` state machine, see
#      ``_codex_plan_continuation``). This is transport-independent.
#   B. Transport — ``rest`` vs ``websocket``: how the planned request is sent.
#
# REST is the normal-runtime transport. WebSocket is an OPT-IN path: the
# runtime never selects it by default, but an operator may flip a Codex agent
# onto the WebSocket wire with the ``LINGTAI_CODEX_TRANSPORT=websocket``
# environment variable (legacy boolean ``LINGTAI_CODEX_WS=1`` also works).
# Explicit ``transport=``/``ws_enabled=`` constructor kwargs still take
# precedence over the environment. Live testing confirmed REST prompt-prefix
# caching is sufficient for the normal path, so WebSocket remains a deliberate
# opt-in for operators who want the delta/``previous_response_id`` wire.
#
# REST runs the SAME full->incremental planner, but only to choose the ``full`` vs
# ``incremental`` label (the cache-epoch semantic): ``full`` marks a
# context/cache-epoch rebuild (first turn / prefix mismatch / epoch reset) and
# ``incremental`` marks an unchanged prefix on the same cache epoch. On REST BOTH
# modes send the same self-contained full converted context and NEVER send
# ``previous_response_id`` — the label only annotates cache affinity, it does not
# change the wire payload.
#
# The WebSocket transport code is retained for tests / opt-in runtime use. It is
# reachable via the explicit ``transport="websocket"`` (or legacy
# ``ws_enabled=True``) constructor kwarg, or via the ``LINGTAI_CODEX_TRANSPORT``
# environment variable (see ``_codex_transport_from_env``). When selected,
# WebSocket ``incremental`` transmits a strict-additive delta plus
# ``previous_response_id`` and WebSocket ``full`` sends the full input frame.
_CODEX_TRANSPORT_DEFAULT = "rest"
# Environment-variable transport selector. ``LINGTAI_CODEX_TRANSPORT=websocket``
# (or legacy ``LINGTAI_CODEX_WS=1``) opts a Codex agent onto the WebSocket wire;
# anything else (unset, ``rest``, invalid) leaves the REST default. Explicit
# constructor kwargs win over this selector (see ``CodexResponsesSession``).
_CODEX_TRANSPORT_ENV = "LINGTAI_CODEX_TRANSPORT"
_CODEX_WS_BOOL_ENV = "LINGTAI_CODEX_WS"
_CODEX_WS_EPOCH_RESET_TURNS_ENV = "LINGTAI_CODEX_WS_EPOCH_RESET_TURNS"
# The mandatory turn-count epoch reset is CANCELLED (Jason, 2026-06-25): there is
# no need to force a fresh full epoch purely by turn count. A full/fresh epoch is
# now caused only by explicit context/history-rewrite actions (summarize/refresh),
# by bootstrap/no-baseline, and by a true prefix-mismatch fallback/diagnostic —
# never by idle->active, notification wake, dismiss, or a periodic countdown.
# ``0`` disables the turn-count reset (see ``_maybe_reset_ws_epoch``). The env var
# and the explicit ``ws_epoch_reset_turns=`` kwarg remain as an opt-in seam for
# tests / experiments, but the runtime default no longer schedules any reset.
_CODEX_WS_EPOCH_RESET_TURNS_DEFAULT = 0

# Non-input request fields that must match between two requests for an
# incremental delta to be valid. Mirrors the official ``get_incremental_items``
# which clones both requests, clears ``.input`` on each, and compares the rest
# for strict equality (``client.rs:960-970``).


def _codex_ws_epoch_reset_turns() -> int:
    """Return the configurable WS response-chain reset interval."""

    raw = os.getenv(_CODEX_WS_EPOCH_RESET_TURNS_ENV, "").strip()
    if not raw:
        return _CODEX_WS_EPOCH_RESET_TURNS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _CODEX_WS_EPOCH_RESET_TURNS_DEFAULT
    return max(0, value)


def _codex_transport_from_env() -> str:
    """Resolve the optional environment-variable transport selector.

    Returns ``"websocket"`` when the operator opted a Codex agent onto the
    WebSocket wire via ``LINGTAI_CODEX_TRANSPORT=websocket`` (or the legacy
    boolean ``LINGTAI_CODEX_WS=1``), otherwise ``"rest"``. This is an OPT-IN
    selector: unset, ``rest``, or any invalid value keeps the REST default.
    Explicit constructor kwargs (``transport=`` / ``ws_enabled=``) are resolved
    by the caller and take precedence over this helper.
    """

    raw = os.getenv(_CODEX_TRANSPORT_ENV, "").strip().lower()
    if raw:
        if raw in {"websocket", "ws"}:
            return "websocket"
        if raw in {"rest", "http", "https"}:
            return "rest"
        # Unknown value — keep the safe REST default.
        return _CODEX_TRANSPORT_DEFAULT
    legacy = os.getenv(_CODEX_WS_BOOL_ENV, "").strip().lower()
    if legacy in {"1", "true", "yes", "on"}:
        return "websocket"
    return _CODEX_TRANSPORT_DEFAULT


@dataclass
class _CodexLastResponse:
    """The previous completed websocket response, for delta computation.

    Mirrors the official ``LastResponse`` (``client.rs:1748-1774``): the
    ``response_id`` becomes the next request's ``previous_response_id``, and
    ``items_added`` are the server-added output items that form part of the
    delta baseline so they are never resent.
    """

    response_id: str
    items_added: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _CodexWebsocketSession:
    """Per-turn websocket state: last full request + last completed response.

    Mirrors the fields the official ``ModelClientSession`` caches for the turn
    (``client.rs:214-226``): ``last_request`` (the full request) and the last
    response. The captured ``turn_state`` token is sticky-routing state replayed
    within the same turn and reset across turns (``client.rs:227-240``).
    """

    last_request: dict[str, Any] | None = None
    last_response: _CodexLastResponse | None = None
    turn_state: str | None = None


def _codex_incremental_items(
    previous_request: dict[str, Any],
    previous_items_added: list[dict[str, Any]],
    request: dict[str, Any],
    *,
    allow_empty_delta: bool,
) -> list[dict[str, Any]] | None:
    """Compute the incremental input delta, or ``None`` to send full input.

    Faithful port of the official ``get_incremental_items``
    (``codex-rs/core/src/client.rs:949-985``):

      1. All non-input request fields must be identical between the previous and
         current request (compare both with ``input`` cleared).
      2. The baseline is ``previous_request.input + previous_items_added``.
      3. The current ``input`` must start with that baseline; the suffix after
         the baseline is the delta. An empty delta is only returned when
         ``allow_empty_delta`` is true (the websocket prewarm/no-op case).

    Returns ``None`` whenever a strict extension cannot be proven, so the caller
    falls back to sending the full input rather than a bad delta.
    """
    delta, _reason = _codex_incremental_diagnose(
        previous_request,
        previous_items_added,
        request,
        allow_empty_delta=allow_empty_delta,
    )
    return delta


def _codex_diff_keys(prev_no_input: dict[str, Any], cur_no_input: dict[str, Any]) -> list[str]:
    """Return the sorted set of non-input request keys that changed.

    Used only for safe diagnostics: it records WHICH non-input field names
    diverged (e.g. ``tools``, ``include``), never their values, so the reason
    string carries no prompt/tool/secret content.
    """
    keys = set(prev_no_input) | set(cur_no_input)
    return sorted(k for k in keys if prev_no_input.get(k) != cur_no_input.get(k))


def _codex_item_safe_diag(item: Any) -> dict[str, str]:
    """Return safe, content-free diagnostics for one Responses input item."""
    if not isinstance(item, dict):
        return {"type": type(item).__name__, "role": "", "keys": "", "hash": ""}
    payload = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return {
        "type": str(item.get("type") or "")[:80],
        "role": str(item.get("role") or "")[:80],
        "keys": ",".join(sorted(str(k) for k in item.keys()))[:160],
        # Short hash only: enough to tell whether two opaque items differ,
        # without leaking prompt/tool/result content into provider metadata.
        "hash": hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:12],
    }


def _codex_incremental_diagnose(
    previous_request: dict[str, Any],
    previous_items_added: list[dict[str, Any]],
    request: dict[str, Any],
    *,
    allow_empty_delta: bool,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Like :func:`_codex_incremental_items` but also return a safe diagnostic.

    The second element is a small metadata dict explaining the decision. It
    records ONLY classes/counts/lengths/short-hash/booleans — never prompt,
    tool-result, reasoning, token, header, or secret content — so it is safe to
    surface in provider metadata / the token ledger:

      * ``reason``: ``ok`` | ``non_input_fields_changed`` | ``prefix_mismatch``
        | ``empty_delta_rejected``
      * ``changed_fields``: list of non-input KEY NAMES that diverged (no values)
      * ``baseline_len`` / ``cur_input_len`` / ``delta_len``: item counts
      * ``mismatch_index``: first baseline index where the prefix diverged (or -1)
    """
    prev_no_input = {k: v for k, v in previous_request.items() if k != "input"}
    cur_no_input = {k: v for k, v in request.items() if k != "input"}

    baseline = list(previous_request.get("input") or [])
    baseline.extend(previous_items_added or [])
    baseline_len = len(baseline)
    cur_input = list(request.get("input") or [])
    cur_len = len(cur_input)

    diag: dict[str, Any] = {
        "reason": "ok",
        "changed_fields": [],
        "baseline_len": baseline_len,
        "cur_input_len": cur_len,
        "delta_len": 0,
        "mismatch_index": -1,
    }

    if prev_no_input != cur_no_input:
        diag["reason"] = "non_input_fields_changed"
        diag["changed_fields"] = _codex_diff_keys(prev_no_input, cur_no_input)
        return None, diag

    # Find the first position where the current input diverges from the baseline.
    prefix = cur_input[:baseline_len]
    if prefix != baseline:
        mismatch = baseline_len  # default: current input is shorter than baseline
        for idx in range(min(len(prefix), baseline_len)):
            if prefix[idx] != baseline[idx]:
                mismatch = idx
                break
        diag["reason"] = "prefix_mismatch"
        diag["mismatch_index"] = mismatch
        if mismatch < baseline_len:
            prev_diag = _codex_item_safe_diag(baseline[mismatch])
            diag["mismatch_prev_type"] = prev_diag.get("type")
            diag["mismatch_prev_role"] = prev_diag.get("role")
            diag["mismatch_prev_keys"] = prev_diag.get("keys")
            diag["mismatch_prev_hash"] = prev_diag.get("hash")
        if mismatch < cur_len:
            cur_diag = _codex_item_safe_diag(cur_input[mismatch])
            diag["mismatch_cur_type"] = cur_diag.get("type")
            diag["mismatch_cur_role"] = cur_diag.get("role")
            diag["mismatch_cur_keys"] = cur_diag.get("keys")
            diag["mismatch_cur_hash"] = cur_diag.get("hash")
        return None, diag

    if not (allow_empty_delta or baseline_len < cur_len):
        diag["reason"] = "empty_delta_rejected"
        return None, diag

    delta = cur_input[baseline_len:]
    diag["delta_len"] = len(delta)
    return delta, diag


def _ws_is_synthesized_orphan_output(item: Any) -> bool:
    """True if ``item`` is the synthesized orphan ``function_call_output`` guard.

    ``to_responses_input`` injects a placeholder ``function_call_output`` for any
    unanswered ``function_call`` (issue #170). That placeholder must not enter the
    websocket delta baseline: the real tool-result continuation replaces it next
    turn, so a baseline containing it can never strict-prefix-match. Detect it by
    the sentinel output string so the baseline builder can trim it.
    """
    from ..interface_converters import _RESPONSES_ORPHAN_OUTPUT_PLACEHOLDER

    return (
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("output") == _RESPONSES_ORPHAN_OUTPUT_PLACEHOLDER
    )


def _strip_synthesized_orphan_outputs(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop every synthesized orphan ``function_call_output`` placeholder.

    The wire-layer guard ``_pair_responses_orphan_function_calls`` injects
    placeholder ``function_call_output`` items (issue #170) for unanswered
    ``function_call``s. Those placeholders are required ON THE WIRE so the
    provider does not 400 on dangling calls, and the guard now appends them as a
    contiguous tail block so they do not split the real ``function_call`` prefix.
    They are still the WRONG thing to record as the continuation BASELINE: the
    real tool results replace them next turn, and any synthesized placeholder
    that becomes a prefix anchor can produce the observed ``prefix_mismatch``
    with ``mismatch_prev_type=function_call_output`` vs
    ``mismatch_cur_type=function_call`` in multi-call turns resolving
    incrementally.

    The fix is to compare and store the baseline with ALL synthesized
    placeholders removed (not just trailing ones), leaving only the real,
    position-stable items. The real outputs always strictly extend that stripped
    prefix, so the continuation stays ``*_incremental``. Pure and content-free:
    returns a new list, never mutates the caller's items.
    """
    return [item for item in items if not _ws_is_synthesized_orphan_output(item)]


def _freeze_responses_outputs(
    items: list[dict[str, Any]],
    frozen: dict[str, str],
) -> list[dict[str, Any]]:
    """Stabilize ``function_call_output.output`` strings across WS replay turns.

    The same ``call_id``'s ``function_call_output.output`` may serialize
    differently on a later turn even though, semantically, the model already
    saw that result: in-place canonical rewrites exist (summarize markers,
    pending→done flips, placeholder overwrites, AED compaction,
    synthesized-pair skeletons) that the shared converter
    (``interface_converters.to_responses_input``) faithfully re-serializes.

    For Codex's stateful WS delta path the next request's converted input must
    strict-prefix-match the prior baseline. A changed older
    ``function_call_output`` (same ``call_id`` and keys, different ``output``
    hash) breaks the prefix and forces ``ws_full`` every turn (the observed
    ``prefix_mismatch``).

    This freezes each output by ``call_id`` at first send for the life of the
    epoch: the first time a ``call_id`` is converted, its ``output`` is
    recorded; every later conversion replays the recorded string. Replay is
    therefore byte-identical regardless of canonical rewrites.

    Fidelity is preserved, not lost: the model already saw the frozen version
    when it was first sent, and the freshest result is *first-seen on its own
    turn*, so it freezes WITH its live meta — live guidance / notifications still
    reach the model on the result that is supposed to carry them. When an epoch
    reset clears the freeze map, the fresh replay re-freezes from the shared
    converter's serialization, which does not strip any historical holder's
    ``_meta`` keys — no adapter-private filter state is involved. Only the
    LATEST holder per family (``agent_meta``/``guidance``,
    ``notifications``/``notification_guidance``) represents current state;
    older holders remain in replay as historical traces the model must not
    act on (see ``lingtai.kernel.meta_block``).

    Pure and content-free: returns a new list (shallow-copying only the rewritten
    items), never mutates the caller's items, and records nothing to diagnostics.
    Non-``function_call_output`` items and outputs missing a ``call_id`` pass
    through untouched.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and isinstance(item.get("call_id"), str)
            # The synthesized orphan placeholder (issue #170 wire guard) is a
            # transient stand-in, NOT the real tool result. Never freeze it:
            # doing so would replay the placeholder once the real continuation
            # arrives, hiding the actual result from the model. Let it pass
            # through so the real output freezes when it first appears.
            and not _ws_is_synthesized_orphan_output(item)
        ):
            call_id = item["call_id"]
            cached = frozen.get(call_id)
            if cached is None:
                frozen[call_id] = item.get("output")
                out.append(item)
            else:
                replayed = dict(item)
                replayed["output"] = cached
                out.append(replayed)
        else:
            out.append(item)
    return out


def _ws_dump_item(item: Any) -> dict[str, Any] | None:
    """Normalize a streamed output item to a plain dict.

    Retained as a small, well-tested normalizer for SDK event items (pydantic
    models -> dict; dicts pass through; anything else -> ``None``). It is NO
    LONGER the delta-baseline source: the server's streamed output items are in
    the Responses *output* schema and never strict-prefix-match the *input*
    schema this session re-derives next turn, which forced ``ws_full`` every
    turn. The baseline is now built from the converter via
    ``CodexResponsesSession._ws_record_baseline_from_interface``. This helper is
    kept for diagnostics / potential reuse and to preserve its unit contract.
    """
    if item is None:
        return None
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump(exclude_none=True)
        except Exception:  # pragma: no cover - defensive
            return None
    if isinstance(item, dict):
        return item
    return None


class _CodexWsFallback(Exception):
    """Raised by a websocket transport to request a fall back to HTTP.

    Mirrors the official ``WebsocketStreamOutcome::FallbackToHttp`` decision
    (``client.rs:1361-1364``): a handshake ``426 UPGRADE_REQUIRED``, a
    connection/handshake failure, an unsupported runtime (e.g. the optional
    ``websockets`` dependency is absent), or any condition under which we cannot
    safely use the websocket path. The caller catches it and replays the full
    input over HTTP with ``store=false``.
    """


def _codex_ws_url(base_url: str | None) -> str:
    """Convert the Codex HTTP base URL to the websocket ``responses`` URL.

    Mirrors ``Provider::websocket_url_for_path`` (``provider.rs:92-103``):
    ``https://chatgpt.com/backend-api/codex`` -> ``wss://.../responses``.
    """
    base = (base_url or "https://chatgpt.com/backend-api/codex").rstrip("/")
    path = base + "/responses"
    if path.startswith("https://"):
        return "wss://" + path[len("https://"):]
    if path.startswith("http://"):
        return "ws://" + path[len("http://"):]
    return path


def _default_codex_ws_transport_factory(url: str, headers: dict[str, str]):
    """Build the real websocket transport, or raise ``_CodexWsFallback``.

    Lazily imports the optional ``websockets`` package; if it is not installed
    (the kernel does not hard-depend on it), the websocket path is treated as an
    unsupported runtime and the caller falls back to HTTP. The real transport is
    intentionally NOT exercised by the unit tests (which inject a fake). The
    WebSocket wire is selected when a Codex session opts in via the explicit
    ``transport="websocket"`` / ``ws_enabled=True`` constructor kwarg or the
    ``LINGTAI_CODEX_TRANSPORT`` / ``LINGTAI_CODEX_WS`` environment selector.
    """
    try:  # pragma: no cover - import guard, exercised only with the dep present
        import websockets  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise _CodexWsFallback(f"websockets unavailable: {exc}") from exc
    # The synchronous wire driver lives in a companion module to keep the import
    # cost and event-loop plumbing out of the hot adapter import path. It is only
    # reached on a live run, never in the mock tests.
    from .codex_ws import SyncCodexWebsocketTransport  # pragma: no cover

    return SyncCodexWebsocketTransport(url=url, headers=headers)  # pragma: no cover


def _base_url_namespace(base_url: str | None) -> str:
    """Return a stable namespace token for an OpenAI-compatible ``base_url``.

    Prefers the URL host (e.g. ``api.vendor.example``) so distinct endpoints
    never share a prompt-cache namespace. Falls back to a short hash of the
    full URL when no host can be parsed, so the result is always deterministic
    and non-empty.
    """
    if not base_url:
        return ""
    host = urlsplit(base_url).hostname or ""
    if host:
        return host
    return "h" + hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:12]


def _parse_codex_base_urls(value: object) -> tuple[str, ...]:
    """Normalize a ``codex_base_urls`` config value into a clean tuple.

    Accepts a list/tuple of strings, or a single comma/newline-separated string
    (whichever the host config layer happens to deliver). Each entry is
    stripped; blank/whitespace-only entries are dropped. Order is preserved and
    duplicates are kept (the caller indexes by molt_count, not by uniqueness).
    Returns ``()`` when nothing valid remains — the caller then falls back to
    the single ``base_url`` behavior (PR #495). NEVER raises and NEVER touches
    the network.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw: list = []
        for item in value:
            # Tolerate a nested comma/newline string inside a list entry.
            if isinstance(item, str) and ("," in item or "\n" in item):
                raw.extend(item.replace("\n", ",").split(","))
            else:
                raw.append(item)
    else:
        return ()
    return tuple(
        s.strip() for s in raw if isinstance(s, str) and s.strip()
    )


def _read_molt_count(agent_json_path: Path) -> int:
    """Read ``molt_count`` from ``<working_dir>/.agent.json``; 0 on any failure.

    The molt path does NOT rebuild the Codex adapter, so the running adapter
    reads this file at request time to observe molt-boundary changes. A missing
    or malformed file, or a non-int ``molt_count``, yields 0 — no exception is
    raised and no network call is made.
    """
    try:
        data = json.loads(agent_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    raw = data.get("molt_count", 0) if isinstance(data, dict) else 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _validate_compact_threshold(value: int | None) -> int | None:
    """Normalize the OpenAI Responses auto-compaction threshold.

    ``None`` intentionally disables Responses ``context_management``.  Any
    concrete value must be a positive integer; reject bool explicitly because
    it is an ``int`` subclass in Python but not a valid token threshold.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("compact_threshold must be a positive int or None")
    if value <= 0:
        raise ValueError("compact_threshold must be > 0 or None")
    return value


def _validate_codex_compact_token_limit(value: int | None) -> int | None:
    """Normalize the Codex standalone-compaction context-token threshold.

    Distinct from ``compact_threshold``/``context_management`` (the generic
    OpenAI Responses auto-compaction the Codex backend rejects — see
    ``_create_responses_session``). ``None`` means "no explicit task limit";
    the caller resolves the effective threshold from the session's
    ``context_window()``. A concrete value must be a positive integer; bool is
    rejected explicitly because it is an ``int`` subclass in Python but not a
    valid token count.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("context_token_limit must be a positive int or None")
    if value <= 0:
        raise ValueError("context_token_limit must be > 0 or None")
    return value


def _estimate_responses_input_tokens(
    instructions: str | None,
    tools: list[dict] | None,
    input_items: list[dict[str, Any]],
) -> int:
    """Estimate tokens for the EXACT rendered Responses ``input`` representation.

    Distinct from ``ChatInterface.estimate_context_tokens()``, which always
    measures the full raw canonical interface regardless of what was actually
    sent on the wire. This helper instead measures ``input_items`` — the
    literal Responses-wire item list a request carries — so the estimate
    reflects the compacted (opaque prefix + strict-additive delta)
    representation when compaction is active, and the full converted
    representation otherwise. Reuses the SAME underlying token-counting
    primitive (``count_tokens``) and the same instructions/tools overhead
    accounting as ``estimate_context_tokens``; only the conversation-body
    input differs (rendered wire items instead of canonical entries).

    Opaque fields (e.g. ``encrypted_content`` inside a ``compaction_summary``
    item) are counted in memory via ``json.dumps`` — never logged or
    persisted; callers of this function must not log ``input_items`` either.
    """
    total = 0
    if instructions:
        total += count_tokens(instructions)
    if tools:
        total += count_tokens(json.dumps(tools, default=str))
    for item in input_items:
        try:
            total += count_tokens(json.dumps(item, default=str))
        except (TypeError, ValueError):
            continue
    return total


def _responses_reasoning_kwargs(thinking: str | None) -> dict[str, dict[str, str]]:
    """Return OpenAI Responses reasoning kwargs for a configured thinking level.

    An omitted/``default`` level maps to the explicit ``xhigh`` effort (the
    kernel's canonical default), matching the Codex adapter's longstanding
    behavior. Explicit levels pass through; ``none`` is sent as
    ``reasoning.effort = "none"``.
    """
    if thinking in (None, "default"):
        thinking = "xhigh"
    if thinking not in THINKING_LEVELS:
        raise ValueError(
            "OpenAI Responses thinking must be one of "
            f"{', '.join(THINKING_LEVELS)}, or default"
        )
    return {"reasoning": {"effort": thinking}}


def _capture_reasoning_application(session: Any, applied: Any) -> Any:
    """Attach the one resolved reasoning decision to the session, if any.

    Observation (``lingtai.kernel.session``) reads this exact object, so what
    gets recorded is the decision the request really carries rather than a
    recomputation from raw config. Sessions on a route with no provider-local
    reasoning policy are left untouched.
    """
    if applied is not None:
        session.reasoning_application = applied
    return session


def _codex_responses_trace_path() -> Path | None:
    """Return the opt-in Codex Responses stream trace path, if enabled."""
    enabled = os.environ.get(_CODEX_RESPONSES_TRACE_ENV, "")
    if enabled.lower() not in {"1", "true", "yes", "on"}:
        return None

    explicit_path = os.environ.get(_CODEX_RESPONSES_TRACE_PATH_ENV)
    if explicit_path:
        return Path(explicit_path).expanduser()

    base_dir = Path(os.environ.get("LINGTAI_AGENT_DIR", ".")).expanduser()
    return base_dir / "logs" / _CODEX_RESPONSES_TRACE_FILE


def _text_fingerprint(text: str | None) -> dict[str, Any]:
    """Return safe metadata for text-like stream deltas without storing content."""
    if text is None:
        return {"present": False, "length": 0}
    encoded = text.encode("utf-8", errors="replace")
    return {
        "present": True,
        "length": len(text),
        "sha256_12": hashlib.sha256(encoded).hexdigest()[:12],
    }


def _codex_responses_trace_record(
    *,
    event: Any,
    accepted_reasoning: bool,
    thoughts_before: list[str],
    thoughts_after: list[str],
    pending_thought_chars_before: int,
    pending_thought_chars_after: int,
    trace_path: Path | None,
) -> None:
    """Append safe diagnostic metadata for one Codex Responses stream event.

    This intentionally records event/item shapes, text lengths, and hashes only;
    it must not store prompt text, raw response text, raw reasoning text, tool
    result content, or API credentials.
    """
    if trace_path is None:
        return

    item = getattr(event, "item", None)
    response = getattr(event, "response", None)
    usage = getattr(response, "usage", None) if response is not None else None
    input_details = getattr(usage, "input_tokens_details", None) if usage is not None else None
    output_details = getattr(usage, "output_tokens_details", None) if usage is not None else None

    summaries = []
    for summary in getattr(item, "summary", None) or []:
        summaries.append({
            "type": getattr(summary, "type", None),
            "text": _text_fingerprint(getattr(summary, "text", None)),
        })

    record = {
        "ts": time.time(),
        "event_type": getattr(event, "type", None),
        "accepted_reasoning": accepted_reasoning,
        "item": None if item is None else {
            "type": getattr(item, "type", None),
            "id": getattr(item, "id", None),
            "call_id": getattr(item, "call_id", None),
            "name": getattr(item, "name", None),
            "summary": summaries,
        },
        "item_id": getattr(event, "item_id", None),
        "delta": _text_fingerprint(getattr(event, "delta", None)),
        "summary_text": _text_fingerprint(getattr(event, "text", None)),
        "thoughts": {
            "before_count": len(thoughts_before),
            "after_count": len(thoughts_after),
            "before_lengths": [len(t) for t in thoughts_before],
            "after_lengths": [len(t) for t in thoughts_after],
            "pending_chars_before": pending_thought_chars_before,
            "pending_chars_after": pending_thought_chars_after,
        },
    }
    if usage is not None:
        record["usage"] = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cached_tokens": getattr(input_details, "cached_tokens", None),
            "reasoning_tokens": getattr(output_details, "reasoning_tokens", None),
        }

    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # pragma: no cover - diagnostics must not break send
        logger.warning("Codex Responses trace write failed: %s", exc)


_READ_TIMEOUT_ENV = "LINGTAI_LLM_READ_TIMEOUT"
_READ_TIMEOUT_DEFAULT = 300.0


def _read_timeout_cap() -> float:
    """Resolve the per-phase HTTP read cap (seconds).

    ``LINGTAI_LLM_READ_TIMEOUT`` overrides the default 300s cap so operators
    can tune how long a thinking model may occupy a read phase before the
    SDK gives up. Values must be positive and finite; anything else falls
    back to the default.
    """
    raw = os.environ.get(_READ_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _READ_TIMEOUT_DEFAULT
        if math.isfinite(value) and value > 0:
            return value
    return _READ_TIMEOUT_DEFAULT


def _build_http_timeout(request_timeout: float | None):
    """Build explicit per-phase HTTP timeout for SDK calls.

    The main-thread watchdog controls total wall-clock time. SDK/httpx
    timeout values are per phase, so cap read waits to keep wedged sockets
    from occupying the worker indefinitely. The read cap defaults to the
    watchdog's ``retry_timeout`` (300s, config.py) so modern thinking models
    (DeepSeek/GLM extended thinking, 60-180s) are not killed mid-thought
    before the watchdog can decide. Operators may tune the read cap with
    ``LINGTAI_LLM_READ_TIMEOUT`` (seconds).
    """
    if request_timeout is None:
        return None
    return httpx.Timeout(
        connect=min(float(request_timeout), 30.0),
        read=min(float(request_timeout), _read_timeout_cap()),
        write=min(float(request_timeout), 30.0),
        pool=10.0,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to OpenAI tool format.

    The wire description comes from ``wire_tool_description``: the constant
    ``WIRE_TOOL_DESCRIPTION`` pointer while the resident ``## tools`` section is
    opted in via ``LINGTAI_TOOL_PROSE_SECTION_ENABLED``, otherwise the full
    ``FunctionSchema.description`` prose (that section is not rendered by
    default, so the wire is where the prose lives).
    """
    if not schemas:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": wire_tool_description(s.description),
                "parameters": s.parameters,
            },
        }
        for s in schemas
    ]


# JSON-Schema keywords still stripped from a function-tool `parameters`
# root for the Responses API. `enum` is only disallowed at the root — it is
# valid (and common) inside individual properties. `anyOf`/`not` at the root
# remain untested against the live backend and are kept out of caution;
# `oneOf`/`allOf` were moved off this list after a live non-strict Codex
# Responses probe on 2026-07-27 accepted a raw root `oneOf` and a raw root
# `allOf`/`if`/`then` without error on the current route — see
# `_scrub_responses_schema`'s root-preservation of both.
_RESPONSES_DISALLOWED_TOP_LEVEL = ("anyOf", "not", "enum")


# Keys that, if present on a schema node, already establish its kind so it
# does not need a synthesized `type`. The Codex backend rejects a property
# schema that carries none of these (a "typeless" property, e.g. one with
# only a `description`), so such nodes are coerced to `{"type": "string"}`.
_SCHEMA_KIND_KEYS = (
    "type", "enum", "const", "$ref",
    "anyOf", "allOf", "oneOf", "not",
    "properties", "items",
)


def _scrub_responses_schema(node: Any, is_root: bool = False) -> Any:
    """Recursively normalize a JSON schema for the Codex Responses backend.

    Empirically, `/backend-api/codex/responses` rejects some constructs even
    when nested inside a property, returning an opaque `server_error`:

      1. Nested `oneOf` / `not` combinators -> nested `oneOf` rewritten to the
         accepted `anyOf`; `not` dropped (no accepted equivalent). This
         nested rewrite is unchanged and still applies at every depth below
         the root.
      2. Typeless property schemas    -> a node with a `description` but no
         type-establishing key (see `_SCHEMA_KIND_KEYS`) gets `type:
         "string"`. This covers the nested `secondary.args.*` fields LingTai
         emits with only a description.
      3. `{"type": "object"}` with no `properties` key (a free-form object,
         e.g. entries in `daemon`'s `tasks[].mcp` array) -> an empty
         `properties: {}` is added. An empty `properties` map is accepted.

    At the schema **root** (`is_root=True`, the top-level `parameters` dict
    passed in by `_build_responses_tools`), `oneOf` and `allOf` are preserved
    as-is rather than rewritten/stripped: a live non-strict Codex Responses
    probe on 2026-07-27 showed the current route accepts a raw root `oneOf`
    and a raw root `allOf`/`if`/`then` without error. Root `anyOf`/`not`/
    `enum` remain untested and are still stripped before this function runs
    (`_RESPONSES_DISALLOWED_TOP_LEVEL`, `_build_responses_tools`).
    `enum`/`anyOf`/`allOf` nested below the root are left untouched as before
    (the backend accepts them nested). Walks dicts and lists so all fixes
    apply at any depth.
    """
    if isinstance(node, dict):
        out: dict = {}
        for key, value in node.items():
            if key == "oneOf" and not is_root:
                out["anyOf"] = [_scrub_responses_schema(v) for v in value]
            elif key == "not":
                continue  # no accepted equivalent — drop it
            elif key in ("oneOf", "allOf") and is_root:
                # Root-level: preserved verbatim (recursing into each
                # branch, which is no longer root), not rewritten/stripped.
                out[key] = [_scrub_responses_schema(v) for v in value]
            else:
                out[key] = _scrub_responses_schema(value)
        # Coerce typeless property schemas: a node describing a value (has a
        # description) but lacking any kind key. Skip bare containers like an
        # empty {} or {"required": [...]} that aren't value descriptors.
        #
        # Guard on the description being a plain string, not merely present:
        # a properties map may legitimately contain a property literally named
        # ``description`` (e.g. notification add/edit hook fields), where the
        # value is a nested schema dict rather than a prose string. Without
        # this guard the scrub would inject ``type: "string"`` into the
        # properties map as a new property key, producing an invalid schema
        # node (``properties.type`` must itself be an object or boolean) that
        # the Codex backend rejects with
        # ``Invalid schema for function '<name>': 'string' is not of type
        # 'object', 'boolean'``.
        if (
            "description" in out
            and isinstance(out["description"], str)
            and not any(k in out for k in _SCHEMA_KIND_KEYS)
        ):
            out["type"] = "string"
        # A typed object with no `properties` is rejected; give it an empty map.
        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}
        return out
    if isinstance(node, list):
        return [_scrub_responses_schema(v) for v in node]
    return node


def _build_responses_tools(schemas: list[FunctionSchema] | None) -> list[dict] | None:
    """Convert FunctionSchema list to Responses API tool format.

    Responses uses a flat shape (`type: function`, fields hoisted) instead
    of Chat Completions' nested `{type: function, function: {...}}`. Strips
    the still-untested `_RESPONSES_DISALLOWED_TOP_LEVEL` keys at the
    parameters root, then runs `_scrub_responses_schema(params, is_root=True)`
    to preserve root `oneOf`/`allOf` verbatim while still fixing the
    constructs the Codex backend rejects even nested in a property (nested
    `oneOf`/`not` combinators and typeless property schemas).
    """
    if not schemas:
        return None
    tools = []
    for s in schemas:
        params = dict(s.parameters or {})
        for key in _RESPONSES_DISALLOWED_TOP_LEVEL:
            params.pop(key, None)
        params = _scrub_responses_schema(params, is_root=True)
        tools.append(
            {
                "type": "function",
                "name": s.name,
                "description": wire_tool_description(s.description),
                "parameters": params,
            }
        )
    return tools


# ---------------------------------------------------------------------------
# Site-specific schema quirks
# ---------------------------------------------------------------------------

# Some OpenAI-compatible sites validate a narrower JSON-Schema dialect. Apply
# only the transformations explicitly registered for the exact base_url host;
# unmatched providers keep the canonical schema byte-for-byte.
#
# See <https://github.com/Lingtai-AI/lingtai/issues/694> for the Kimi API error
# that motivated this boundary.
_SITE_SCHEMA_QUIRKS: dict[str, frozenset[str]] = {
    "api.kimi.com": frozenset({"move_type_into_union_branches"}),
}


def _move_type_into_union_branches(node: Any) -> Any:
    """Move a union node's parent ``type`` into each union branch.

    Kimi's native API rejects ``type`` beside ``anyOf``/``oneOf`` and asks for
    it inside the union items instead. The canonical schema is not mutated.
    """
    if isinstance(node, list):
        return [_move_type_into_union_branches(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {key: _move_type_into_union_branches(value) for key, value in node.items()}
    parent_type = out.get("type")
    union_keys = [
        key for key in ("anyOf", "oneOf") if isinstance(out.get(key), list)
    ]
    if parent_type is None or not union_keys:
        return out

    out.pop("type", None)
    for union_key in union_keys:
        rewritten = []
        for branch in out[union_key]:
            if isinstance(branch, dict) and "type" not in branch:
                branch = _move_type_into_union_branches({"type": parent_type, **branch})
            rewritten.append(branch)
        out[union_key] = rewritten
    return out


def _apply_site_quirks(
    base_url: str | None,
    tools: list[dict] | None,
) -> list[dict] | None:
    """Apply exact-host schema quirks to the outgoing wire copy."""
    if not tools or not base_url:
        return tools
    active = _SITE_SCHEMA_QUIRKS.get((urlsplit(base_url).hostname or "").lower())
    if not active:
        return tools

    result = []
    for tool in tools:
        tool = dict(tool)
        if "function" in tool:
            # Chat Completions shape: {type, function: {name, desc, params}}
            function = dict(tool["function"])
            if "parameters" in function:
                function["parameters"] = _apply_quirk_dict(
                    function["parameters"],
                    active,
                )
            tool["function"] = function
        elif "parameters" in tool:
            # Responses shape: {type, name, desc, params} (flat)
            tool["parameters"] = _apply_quirk_dict(tool["parameters"], active)
        result.append(tool)
    return result


def _apply_quirk_dict(params: dict, active: frozenset[str]) -> dict:
    """Apply the registered quirks to one parameters dict."""
    if "move_type_into_union_branches" in active:
        return _move_type_into_union_branches(params)
    return params


def _parse_tool_calls(raw_tool_calls) -> list[ToolCall]:
    """Parse OpenAI tool calls into our ToolCall dataclass."""
    if not raw_tool_calls:
        return []
    result = []
    for tc in raw_tool_calls:
        try:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        result.append(
            ToolCall(
                name=tc.function.name,
                args=args,
                id=tc.id,
            )
        )
    return result


def _parse_response(raw) -> LLMResponse:
    """Parse a raw OpenAI ChatCompletion into a provider-agnostic LLMResponse."""
    if not hasattr(raw, "choices"):
        # Defense-in-depth: a non-conforming gateway (e.g. an OpenAI-compatible
        # endpoint that returns a scalar string) must surface a typed protocol
        # error instead of failing through a bare attribute dereference. This
        # is deliberately a TypeError, not a ValueError: the response object
        # has the wrong type, not merely an empty payload.
        raise TypeError(
            "OpenAI-compatible endpoint returned a non-ChatCompletion response "
            "(expected an object with a `choices` list); check the provider's "
            "wire_api/endpoint compatibility"
        )
    if not raw.choices:
        return LLMResponse(raw=raw)

    choice = raw.choices[0]
    message = choice.message

    text = message.content or ""
    tool_calls = _parse_tool_calls(message.tool_calls)

    # Extract thinking/reasoning. Field name varies by provider:
    #   OpenAI o-series native        -> message.reasoning_content
    #   OpenRouter (any reasoning mdl) -> message.reasoning
    # We check both so the same parser works across providers. Native
    # providers that don't set either field just produce no thoughts.
    thoughts: list[str] = []
    reasoning = (
        getattr(message, "reasoning_content", None)
        or getattr(message, "reasoning", None)
    )
    if reasoning:
        thoughts.append(reasoning)

    # Token usage
    usage = UsageMetadata()
    if raw.usage:
        cached = getattr(raw.usage, "prompt_tokens_details", None)
        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
        usage = UsageMetadata(
            input_tokens=raw.usage.prompt_tokens or 0,
            output_tokens=raw.usage.completion_tokens or 0,
            thinking_tokens=getattr(raw.usage, "completion_tokens_details", None)
            and getattr(raw.usage.completion_tokens_details, "reasoning_tokens", 0)
            or 0,
            cached_tokens=cached_tokens,
        )

    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        thoughts=thoughts,
        raw=raw,
    )


def _add_responses_reasoning_done_text(
    acc: StreamingAccumulator,
    reasoning_item_id: str | None,
    seen_reasoning_summary_items: set[str],
    text: str | None,
) -> bool:
    """Add final reasoning-summary text only when no delta was seen.

    Responses ``*.done`` events carry the complete text.  When the stream
    already accepted summary text for the same reasoning item from deltas or a
    done fallback, adding ``done.text`` would duplicate the thought.  If a
    provider emits only the done event, use it as a lossless fallback.
    Returns ``True`` when ``text`` was adopted (its sole delivery).
    """
    adopted = bool(
        text and (not reasoning_item_id or reasoning_item_id not in seen_reasoning_summary_items)
    )
    if adopted:
        acc.add_thought(text)
        if reasoning_item_id:
            seen_reasoning_summary_items.add(reasoning_item_id)
    acc.finish_thought()
    return adopted


_NULL_PROGRESS = OutputProgress()
_RESPONSES_ITEM_IDENTITY = ("id", "call_id", "name")


def _count_responses_output(event: Any, progress: OutputProgress) -> None:
    """Output progress for one Responses event: whatever arrived, add its length.

    Every ``*.delta`` counts (text, arguments, reasoning, refusal, any later
    form), keyed so a terminal echo adds nothing; a new item counts its
    identity once.  Terminal payloads that may be a sole delivery are counted
    where they are adopted.  Lifecycle/usage/``completed`` echoes add nothing.
    """
    event_type = getattr(event, "type", None) or ""
    if event_type == "response.output_item.added":
        item = getattr(event, "item", None)
        progress.add(*(getattr(item, field, None) for field in _RESPONSES_ITEM_IDENTITY))
    elif event_type.endswith(".delta"):
        progress.add_stream(
            (event_type, getattr(event, "item_id", None)), getattr(event, "delta", None)
        )


def _adopt_terminal_args(acc: StreamingAccumulator, progress: OutputProgress, arguments: Any) -> None:
    """Terminal complete-arguments payload: adopt and count only as a sole delivery."""
    if acc.set_tool_args_if_empty(arguments):
        progress.add(arguments)


def _handle_responses_reasoning_event(
    event: Any,
    acc: StreamingAccumulator,
    seen_reasoning_summary_items: set[str],
    progress: OutputProgress | None = None,
) -> bool:
    """Feed safe Responses reasoning-summary events into ``acc``.

    We persist summary text, not raw ``response.reasoning_text.*`` events, so
    stateless Codex replay can include documented ``summary_text`` reasoning
    items without storing hidden chain-of-thought.  ``progress`` counts the
    terminal reasoning payloads delivered only here.
    """
    progress = progress or _NULL_PROGRESS
    event_type = getattr(event, "type", None)
    if event_type == "response.reasoning_summary_text.delta":
        delta = getattr(event, "delta", None)
        if delta:
            acc.add_thought(delta)
            item_id = getattr(event, "item_id", None)
            if item_id:
                seen_reasoning_summary_items.add(item_id)
        return True
    if event_type == "response.reasoning_summary_text.done":
        text = getattr(event, "text", None)
        if _add_responses_reasoning_done_text(
            acc, getattr(event, "item_id", None), seen_reasoning_summary_items, text
        ):
            progress.add(text)
        return True
    if event_type == "response.output_item.done" and getattr(event.item, "type", None) == "reasoning":
        summaries = getattr(event.item, "summary", None) or []
        item_id = getattr(event.item, "id", None)
        added_fallback = False
        for summary in summaries:
            if getattr(summary, "type", None) == "summary_text":
                text = getattr(summary, "text", None)
                if text and (not item_id or item_id not in seen_reasoning_summary_items):
                    acc.add_thought(text)
                    progress.add(text)
                    if item_id:
                        seen_reasoning_summary_items.add(item_id)
                    added_fallback = True
        if added_fallback or acc.thoughts:
            acc.finish_thought()
        # Opaque reasoning is output too; repeated terminal items count once.
        progress.add_final(
            ("response.reasoning.encrypted_content", item_id),
            getattr(event.item, "encrypted_content", None),
        )
        progress.add_final(
            ("response.reasoning_text.delta", item_id),
            *(
                c.get("text") if isinstance(c, dict) else getattr(c, "text", None)
                for c in getattr(event.item, "content", None) or []
            ),
        )
        return True
    return False


def _parse_responses_api_response(raw) -> LLMResponse:
    """Parse a raw OpenAI Responses API response into a provider-agnostic LLMResponse."""
    text_parts = []
    tool_calls = []
    thoughts = []

    for item in raw.output or []:
        if item.type == "message":
            for block in item.content or []:
                if block.type == "output_text":
                    text_parts.append(block.text)
        elif item.type == "function_call":
            try:
                args = json.loads(item.arguments) if item.arguments else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=item.name, args=args, id=item.call_id))
        elif item.type == "reasoning":
            for summary in getattr(item, "summary", None) or []:
                if getattr(summary, "type", None) == "summary_text":
                    thoughts.append(summary.text)

    # Token usage
    usage = UsageMetadata()
    if raw.usage:
        cached = getattr(raw.usage, "input_tokens_details", None)
        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
        usage = UsageMetadata(
            input_tokens=getattr(raw.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw.usage, "output_tokens", 0) or 0,
            thinking_tokens=getattr(raw.usage, "output_tokens_details", None)
            and getattr(raw.usage.output_tokens_details, "reasoning_tokens", 0)
            or 0,
            cached_tokens=cached_tokens,
        )

    return LLMResponse(
        text="\n".join(text_parts),
        tool_calls=tool_calls,
        usage=usage,
        thoughts=thoughts,
        raw=raw,
    )


def _responses_json_namespace(value: Any) -> Any:
    """Convert decoded Responses SSE JSON into attribute-style event objects."""
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: _responses_json_namespace(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [_responses_json_namespace(item) for item in value]
    return value


def _decode_responses_sse_text(raw: str) -> list[Any]:
    """Decode an SSE body returned unexpectedly to a non-streaming request.

    Some OpenAI-compatible gateways ignore ``stream=false`` and return a
    complete ``text/event-stream`` body.  The OpenAI SDK exposes that body as a
    plain string, so recover the already-completed response locally instead of
    issuing a second (potentially billable) request.
    """
    events: list[Any] = []
    data_lines: list[str] = []
    event_name: str | None = None
    saw_sse_field = False

    def flush() -> None:
        nonlocal data_lines, event_name
        if not data_lines:
            event_name = None
            return
        payload = "\n".join(data_lines).strip()
        data_lines = []
        if not payload or payload == "[DONE]":
            event_name = None
            return
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TypeError(
                "Responses API returned malformed SSE data to a non-streaming request"
            ) from exc
        if not isinstance(decoded, dict):
            raise TypeError(
                "Responses API returned non-object SSE data to a non-streaming request"
            )
        if not decoded.get("type") and event_name:
            decoded["type"] = event_name
        if not decoded.get("type"):
            raise TypeError(
                "Responses API returned an SSE event without a type"
            )
        events.append(_responses_json_namespace(decoded))
        event_name = None

    for line in raw.splitlines():
        if not line:
            flush()
        elif line.startswith("event:"):
            saw_sse_field = True
            event_name = line[6:].strip() or None
        elif line.startswith("data:"):
            saw_sse_field = True
            data_lines.append(line[5:].lstrip())
        elif line.startswith(":"):
            saw_sse_field = True
    flush()

    if not saw_sse_field or not events:
        raise TypeError(
            "Responses API returned a string instead of a Response object or SSE events"
        )
    return events


def _consume_responses_stream(
    stream: Any,
    on_chunk: Callable[[str], None] | None = None,
    on_output_chars: Callable[[int], None] | None = None,
) -> tuple[LLMResponse, str | None]:
    """Consume typed SDK events or locally decoded Responses SSE events.

    ``on_chunk`` receives visible ``output_text`` deltas only (legacy);
    ``on_output_chars`` receives the length of every output fragment.
    """
    acc = StreamingAccumulator()
    progress = OutputProgress(on_output_chars)
    response_id = None
    usage = UsageMetadata()
    seen_reasoning_summary_items: set[str] = set()

    for event in stream:
        # Any lifecycle event may carry the response id (``response.created``,
        # ``response.in_progress``, ``response.incomplete``, ``response.failed``,
        # ``response.completed``).  Latch the newest one so a stream that ends
        # without ``response.completed`` — which non-conforming gateways do —
        # still yields a continuation id instead of silently dropping the chain.
        latched_id = getattr(getattr(event, "response", None), "id", None)
        if latched_id:
            response_id = latched_id
        _count_responses_output(event, progress)
        if _handle_responses_reasoning_event(event, acc, seen_reasoning_summary_items, progress):
            continue
        if event.type == "response.output_text.delta":
            acc.add_text(event.delta)
            if on_chunk:
                on_chunk(event.delta)
        elif event.type == "response.function_call_arguments.delta":
            acc.add_tool_args(event.delta)
        elif event.type == "response.function_call_arguments.done":
            # Spark may emit complete args without any deltas.
            _adopt_terminal_args(acc, progress, getattr(event, "arguments", None))
        elif event.type == "response.output_item.added":
            if getattr(event.item, "type", None) == "function_call":
                acc.start_tool(id=event.item.call_id, name=event.item.name)
        elif event.type == "response.output_item.done":
            if getattr(event.item, "type", None) == "function_call":
                # Use the final item as a second complete-args fallback.
                _adopt_terminal_args(acc, progress, getattr(event.item, "arguments", None))
                acc.finish_tool()
        elif event.type == "response.completed":
            # Locally decoded gateway SSE is raw JSON, not an SDK model: every
            # field here is optional and must be probed, never dotted.
            raw_usage = getattr(getattr(event, "response", None), "usage", None)
            if raw_usage:
                cached = getattr(raw_usage, "input_tokens_details", None)
                cached_tokens = (
                    (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                )
                details = getattr(raw_usage, "output_tokens_details", None)
                usage = UsageMetadata(
                    input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
                    thinking_tokens=(
                        getattr(details, "reasoning_tokens", 0) or 0 if details else 0
                    ),
                    cached_tokens=cached_tokens,
                )

    return acc.finalize(usage=usage), response_id


# ---------------------------------------------------------------------------
# OpenAIChatSession
# ---------------------------------------------------------------------------


def _fallback_reasoning_for(msg: dict, turn_idx: int) -> str:
    """Build a per-turn-unique reasoning stub for an assistant message.

    Used only when an assistant turn carries no real reasoning_content
    (typically: history entries that predate the ThinkingBlock-preservation
    fix, or turns where the provider returned no reasoning text). The
    string must be byte-different per turn — a constant placeholder
    triggers DeepSeek's cache fast-path to collapse onto it and emit
    empty responses (issue #9).

    DeepSeek validates field presence, not content, so any non-empty
    string is accepted. We inline tool names and the call ids to keep
    the stub naturally unique per turn.
    """
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        names = ",".join(
            (tc.get("function", {}) or {}).get("name", "") for tc in tool_calls
        )
        ids = ",".join(tc.get("id", "") for tc in tool_calls)
        return f"call {names} [{ids}] (turn {turn_idx})"
    # Plain-text post-tool-call turn — content is per-turn unique by nature.
    content = msg.get("content") or ""
    snippet = content[:64].replace("\n", " ")
    return f"reply [{snippet}] (turn {turn_idx})"


def _fallback_responses_reasoning_item(role_text: str, turn_idx: int) -> dict:
    """Build a per-turn-unique Responses ``reasoning`` input item.

    ``role_text`` is the assistant plain-text content for the turn being
    repaired (empty for a tool-call turn). The stub is inline-suffixed
    with the turn index so consecutive missing turns stay byte-different.
    """
    snippet = (role_text or "")[:64].replace("\n", " ")
    label = f"reply [{snippet}]" if snippet else "call"
    return {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": f"{label} (turn {turn_idx})"}],
    }


def _inject_responses_reasoning_fallback(items: list[dict]) -> list[dict]:
    """Inject per-turn-unique ``reasoning`` item fallbacks into a Responses input list.

    Responses items are a flat list; a single assistant turn can span
    multiple items (reasoning, text, function_call). After the first
    ``function_call`` item we must ensure every later assistant turn
    carries a ``reasoning`` item — the Responses analogue of
    ``_inject_chat_reasoning_fallback``. We walk the list, track
    whether a ``function_call`` has been seen, and for each ``assistant``
    text item that follows a call without an immediately-preceding
    reasoning item, insert one before it.
    """
    seen_function_call = False
    turn_idx = 0
    out: list[dict] = []
    for item in items:
        if item.get("type") == "function_call":
            seen_function_call = True
            out.append(item)
            continue
        if seen_function_call and item.get("role") == "assistant":
            turn_idx += 1
            # Insert a fallback reasoning item before this assistant text
            # item unless the immediately preceding item already carries
            # reasoning (real thinking was preserved).
            if not (out and out[-1].get("type") == "reasoning"):
                out.append(
                    _fallback_responses_reasoning_item(
                        item.get("content") or "", turn_idx
                    )
                )
            out.append(item)
            continue
        out.append(item)
    return out


def _inject_chat_reasoning_fallback(messages: list[dict]) -> list[dict]:
    """Inject per-turn-unique ``reasoning_content`` fallbacks into Chat messages.

    Generic default-on version of the former DeepSeek-specific loop: walk the
    messages, track whether an assistant ``tool_calls`` turn has been seen,
    and for assistant turns AFTER the first tool_call that lack
    ``reasoning_content``, set a per-turn-unique fallback stub. Assistant
    turns BEFORE the first tool_call must NOT carry the field (providers
    reject it), and real captured reasoning is never overwritten. Returns
    the same list, mutated in place.
    """
    seen_tool_call = False
    turn_idx = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        if msg.get("tool_calls"):
            seen_tool_call = True
        if seen_tool_call:
            turn_idx += 1
            if not msg.get("reasoning_content"):
                msg["reasoning_content"] = _fallback_reasoning_for(msg, turn_idx)
    return messages


class OpenAIChatSession(ChatSession):
    """Client-managed chat session for OpenAI-compatible APIs.

    Uses ChatInterface as the single source of truth.
    """

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        interface: ChatInterface,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_kwargs: dict,
        client_kwargs: dict | None = None,
        context_window: int = 0,
        prompt_cache_key: str | None = None,
        inject_reasoning_fallback: bool = False,
        base_url: str | None = None,
    ):
        self._client = client
        self._model = model
        self._interface = interface
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._client_kwargs = client_kwargs or {}
        self._context_window = context_window
        # Stable ``prompt_cache_key`` for OpenAI-compatible Chat Completions
        # cross-request prompt caching. Sent only when set; ``None`` leaves it
        # off (so a directly-constructed session is opt-in). The adapter
        # supplies the namespaced default — see ``_default_prompt_cache_key``.
        # ``prompt_cache_retention`` is deliberately never sent (Codex rejects
        # it; we keep the whole OpenAI-compatible surface uniform).
        self._prompt_cache_key = prompt_cache_key
        # Generic ``reasoning_content`` round-trip fallback (the former
        # DeepSeek-specific behavior, now available to any OpenAI-compatible
        # provider): inject a per-turn-unique stub on assistant turns after
        # the first tool_call that lack real reasoning. Default on (env
        # LINGTAI_INJECT_REASONING_FALLBACK to disable); explicit param wins.
        self._inject_reasoning_fallback = bool(inject_reasoning_fallback)
        self._base_url = base_url
        # Per-request HTTP timeout (seconds). Set by send_with_timeout before
        # dispatching the worker so the HTTP client aborts at the same moment
        # the main-thread watchdog gives up. Prevents a race where the worker
        # keeps mutating the shared ChatInterface after AED has already
        # declared a timeout and started recovering.
        self._request_timeout: float | None = None

    @property
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""
        return self._interface

    def request_history_rebuild(self, reason: str = "summarize_rebuild_only") -> bool:
        """Start a fresh full replay on the next model request.

        Chat Completions is client-managed: every request re-serializes the
        canonical ChatInterface, so after ``context(action='rebuild')`` applies
        pending summaries the next send naturally carries the compacted
        history. Return True so callers surface ``rebuild_requested=true``.
        """
        return True

    def _build_messages(self) -> list[dict]:
        """Return the message list to send to the API.

        Default: the canonical OpenAI serialization of the current
        interface. When ``inject_reasoning_fallback`` is enabled, assistant
        turns after the first tool_call that carry no real
        ``reasoning_content`` get a per-turn-unique fallback stub — the
        generic version of the former DeepSeek-specific behavior, required
        by any thinking-mode provider that demands the field on replay.
        """
        messages = to_openai(self._interface)
        if self._inject_reasoning_fallback:
            return _inject_chat_reasoning_fallback(messages)
        return messages

    @staticmethod
    def _is_context_overflow_error(exc: Exception) -> bool:
        """Detect provider 400-class context-length-exceeded errors.

        Covers OpenAI's canonical ``context_length_exceeded`` code plus the
        loose string-match heuristics used by compatible vendors (DeepSeek,
        Together, Groq, etc.) that often only signal via the message body.
        """
        if isinstance(exc, openai.APIError) and not isinstance(exc, openai.BadRequestError):
            # Some compatible endpoints surface this overflow as a generic
            # APIError. Keep this deliberately narrow: arbitrary APIError or
            # generic "context window" text is not a retryable overflow.
            return "input exceeds the context window" in (safe_exception_description(exc) or "").lower()
        if not isinstance(exc, openai.BadRequestError):
            return False
        # Canonical OpenAI code on the body's error object.
        code = None
        try:
            body = getattr(exc, "body", None) or {}
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = err.get("code")
        except Exception:
            pass
        if code == "context_length_exceeded":
            return True
        msg = (safe_exception_description(exc) or "").lower()
        return any(
            needle in msg
            for needle in (
                "context length",
                "context_length_exceeded",
                "maximum context",
                "context window",
                "too many tokens",
                "input is too long",
                "prompt is too long",
            )
        )

    # _trim_context_one_round, _run_with_overflow_recovery, and
    # _inject_overflow_notice are inherited from ChatSession (base class).
    # Only _is_context_overflow_error needs to be provider-specific.

    def _pair_orphan_tool_calls(self, messages: list[dict]) -> list[dict]:
        """Final wire-layer guard: synthesize placeholder tool messages for
        any assistant[tool_calls] that are not immediately followed by
        matching role=tool messages. Does NOT mutate the canonical interface
        — synthesis is local to this serialization pass, re-runs from scratch
        next send.

        This catches several known pathologies:
        - An interleaved entry (e.g. a new system prompt appended because
          identity changed) slipping between an assistant[tool_calls] and
          its tool_results in the canonical interface.
        - A cancelled / partial tool batch where some tool_results never
          made it into the interface.
        - Any future drift we haven't anticipated.

        Once the real tool_result arrives in the interface later, the next
        serialization sees it naturally and no synthesis fires — implicit
        dedup without any stateful replace step.

        Each synthesis logs only the structural missing-result reason and
        phase. The following shared validator rejects any remaining malformed
        row before the SDK call.
        """
        patched: list[dict] = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise _OpenAIToolPairingError("invalid_message", phase="wire_pairing")
            patched.append(msg)
            if msg.get("role") != "assistant":
                continue
            if "tool_calls" not in msg:
                continue
            tool_calls = msg["tool_calls"]
            if not isinstance(tool_calls, list):
                raise _OpenAIToolPairingError("invalid_call_batch", phase="wire_pairing")
            if not tool_calls:
                continue
            # Look ahead in the ORIGINAL input list for role=tool entries
            # immediately following this assistant turn. Synthesize
            # placeholders for any tool_call_id not covered.
            seen_ids: set[str] = set()
            j = i + 1
            while j < len(messages):
                result = messages[j]
                if not isinstance(result, dict):
                    raise _OpenAIToolPairingError("invalid_message", phase="wire_pairing")
                if result.get("role") != "tool":
                    break
                tcid = result.get("tool_call_id")
                if tcid:
                    seen_ids.add(tcid)
                j += 1
            # For each tool_call without a matching tool message, emit a
            # synthesized placeholder immediately after the assistant turn.
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    raise _OpenAIToolPairingError("invalid_call", phase="wire_pairing")
                tcid = tc.get("id")
                if not tcid or tcid in seen_ids:
                    continue
                logger.warning(
                    "[wire-guard] synthesized missing tool result "
                    "placeholder (reason=missing_result phase=wire_pairing)"
                )
                patched.append({
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": "[synthesized placeholder — real result was not in context at send time]",
                })
                seen_ids.add(tcid)
        return patched

    def send(self, message) -> LLMResponse:
        """Send a user message (str), tool results (list of dicts), or
        drive the existing wire forward (``None``).

        For tool results, ``message`` is a list of ToolResultBlock instances
        built by :meth:`OpenAIAdapter.make_tool_result_message`.

        ``None`` is the "continue from wire" signal — the caller has
        already appended whatever needs to land (see
        ``base_agent/turn.py:_handle_tc_wake`` for the notification
        path).  No input append happens here; the existing wire is
        sent as-is.

        Records user input into the interface BEFORE the API call, then
        reverts on error. On success, records the assistant response.
        """
        # 1. Record user input into interface
        if message is None:
            pass  # wire is already prepared by the caller
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            # Tool results — list of ToolResultBlock instances
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # 1b. Pre-request hook — kernel-side splice point for mid-turn
        # tc_inbox drains. Wire tail is user[tool_results] or user[text],
        # so any (call, result) pair the hook splices is appended at a
        # safe boundary and rides along on this same API request.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # 2. Build ephemeral provider messages from interface — re-runs
        #    inside the overflow-recovery loop so each retry sees the
        #    post-trim canonical interface.
        def _build_kwargs() -> dict[str, Any]:
            self._interface.enforce_tool_pairing()
            candidate = self._build_messages()
            # Final wire-layer guard: synthesize placeholder tool messages for
            # any orphan assistant[tool_calls] that aren't immediately followed
            # by matching role=tool entries. Canonical interface untouched.
            candidate = self._pair_orphan_tool_calls(candidate)
            _validate_openai_tool_pairing(candidate)
            kw: dict[str, Any] = {
                "model": self._model,
                "messages": candidate,
                **self._extra_kwargs,
            }
            if self._tools:
                kw["tools"] = self._tools
                kw["parallel_tool_calls"] = True
                if self._tool_choice:
                    kw["tool_choice"] = self._tool_choice
            if self._prompt_cache_key:
                kw["prompt_cache_key"] = self._prompt_cache_key
            if self._request_timeout is not None:
                kw["timeout"] = _build_http_timeout(self._request_timeout)
            return kw

        # 3. Make the API call (with auto-recovery on context overflow);
        #    revert interface on any other error.
        def _do_call():
            return self._client.chat.completions.create(**_build_kwargs())

        try:
            raw, total_dropped, rounds = self._run_with_overflow_recovery(_do_call)
        except Exception as exc:
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            # Terminal overflow failure may already have trimmed entries;
            # surface the loss after the revert (the revert would strip a
            # just-injected user-role notice).
            self._inject_overflow_notice_after_error(exc)
            raise

        # 3b. If recovery fired (entries were dropped), inject the molt notice.
        if rounds > 0:
            self._inject_overflow_notice(total_dropped=total_dropped, rounds=rounds)

        # 4. Record assistant response into interface
        self._record_assistant_response(raw)

        return _parse_response(raw)

    def commit_tool_results(self, tool_results: list) -> None:
        """Append tool results to interface without an API call."""
        if tool_results:
            self._interface.add_tool_results(tool_results)

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        """Replace the tool schemas for subsequent calls in this session."""
        self._tools = (
            _apply_site_quirks(self._base_url, _build_tools(tools)) if tools else None
        )
        tool_dicts = FunctionSchema.list_to_dicts(tools)
        self._interface.add_system(
            self._interface.current_system_prompt or "", tools=tool_dicts,
        )

    def update_system_prompt(self, system_prompt: str) -> None:
        """Replace the system prompt for subsequent calls in this session."""
        self._interface.add_system(system_prompt, tools=self._interface.current_tools)

    def reset(self) -> None:
        """Create a truly fresh session instance while preserving state.

        Reconstructs a new OpenAIChatSession with a fresh HTTP client
        and copies all attributes onto self, giving a clean connection and
        fresh internal state.
        """
        if self._client_kwargs:
            new_client = openai.OpenAI(**self._client_kwargs)
            new_session = OpenAIChatSession(
                client=new_client,
                model=self._model,
                interface=self._interface,
                tools=self._tools,
                tool_choice=self._tool_choice,
                extra_kwargs=self._extra_kwargs,
                client_kwargs=self._client_kwargs,
                context_window=self._context_window,
                prompt_cache_key=self._prompt_cache_key,
                inject_reasoning_fallback=self._inject_reasoning_fallback,
            )
            self.__dict__.update(new_session.__dict__)

    def _record_assistant_response(self, raw) -> None:
        """Parse a raw ChatCompletion and record the assistant response into the interface."""
        choice = raw.choices[0] if raw.choices else None
        blocks: list = []
        if choice and choice.message:
            msg = choice.message
            # Capture reasoning_content (DeepSeek/o-series) or reasoning
            # (OpenRouter) into a ThinkingBlock. Persisting it makes the
            # next request carry real reasoning back to the provider on
            # replay, instead of a constant placeholder. See issue #9.
            reasoning = (
                getattr(msg, "reasoning_content", None)
                or getattr(msg, "reasoning", None)
            )
            if reasoning:
                blocks.append(ThinkingBlock(text=reasoning))
            if msg.content:
                blocks.append(TextBlock(text=msg.content))
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    blocks.append(ToolCallBlock(id=tc.id, name=tc.function.name, args=args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        usage_dict = {}
        if raw.usage:
            details = getattr(raw.usage, "completion_tokens_details", None)
            usage_dict = {
                "input_tokens": raw.usage.prompt_tokens or 0,
                "output_tokens": raw.usage.completion_tokens or 0,
                "thinking_tokens": getattr(details, "reasoning_tokens", 0) or 0 if details else 0,
            }
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="openai",
            usage=usage_dict,
        )

    @staticmethod
    def _response_to_message(raw) -> dict:
        """Convert an OpenAI ChatCompletion response to a message dict for history."""
        choice = raw.choices[0] if raw.choices else None
        if not choice:
            return {"role": "assistant", "content": ""}
        msg = choice.message
        result: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            result["content"] = msg.content
        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        if not msg.content and not msg.tool_calls:
            result["content"] = ""
        return result

    def send_stream(self, message, on_chunk=None, on_output_chars=None) -> LLMResponse:
        """Send a streaming request.  Same shape as :meth:`send` —
        ``str`` / ``list`` / ``None`` (continue from wire).

        Records user input into the interface BEFORE the API call, then
        reverts on error. On success, records the assistant response.
        ``on_chunk`` receives visible content deltas only (legacy);
        ``on_output_chars`` receives the length of every output field a delta
        carries.
        """
        # 1. Record user input into interface
        if message is None:
            pass  # wire is already prepared by the caller
        elif isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # 1b. Pre-request hook — see send() above for contract.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # 2. Build ephemeral provider messages from interface — re-runs
        #    inside the overflow-recovery loop so each retry sees the
        #    post-trim canonical interface.
        def _build_kwargs() -> dict[str, Any]:
            self._interface.enforce_tool_pairing()
            candidate = self._build_messages()
            # Final wire-layer guard — same as non-streaming send().
            candidate = self._pair_orphan_tool_calls(candidate)
            _validate_openai_tool_pairing(candidate)
            kw: dict[str, Any] = {
                "model": self._model,
                "messages": candidate,
                "stream": True,
                "stream_options": {"include_usage": True},
                **self._extra_kwargs,
            }
            if self._tools:
                kw["tools"] = self._tools
                kw["parallel_tool_calls"] = True
                if self._tool_choice:
                    kw["tool_choice"] = self._tool_choice
            if self._prompt_cache_key:
                kw["prompt_cache_key"] = self._prompt_cache_key
            if self._request_timeout is not None:
                kw["timeout"] = _build_http_timeout(self._request_timeout)
            return kw

        acc = StreamingAccumulator()
        progress = OutputProgress(on_output_chars)
        usage = UsageMetadata()

        # Streaming overflow-recovery: most providers raise the 400 either
        # when ``create()`` returns or on the first iteration of the stream
        # — before any content has been emitted to ``on_chunk``. We open the
        # stream and pull the first chunk inside the recovery wrapper; once
        # that succeeds, we hand off to the regular streaming loop.
        def _open_and_first_chunk():
            stream = self._client.chat.completions.create(**_build_kwargs())
            it = iter(stream)
            try:
                first = next(it)
            except StopIteration:
                first = None
            return stream, it, first

        # 3. Stream; revert interface on error
        try:
            (stream, it, first_chunk), total_dropped, rounds = (
                self._run_with_overflow_recovery(_open_and_first_chunk)
            )
            if rounds > 0:
                self._inject_overflow_notice(
                    total_dropped=total_dropped, rounds=rounds,
                )
            # Re-stitch: first chunk + remaining iterator.
            def _chunks():
                if first_chunk is not None:
                    yield first_chunk
                for c in it:
                    yield c
            for chunk in _chunks():
                if not chunk.choices:
                    if chunk.usage:
                        cached = getattr(chunk.usage, "prompt_tokens_details", None)
                        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                        usage = UsageMetadata(
                            input_tokens=chunk.usage.prompt_tokens or 0,
                            output_tokens=chunk.usage.completion_tokens or 0,
                            thinking_tokens=(
                                getattr(
                                    getattr(chunk.usage, "completion_tokens_details", None),
                                    "reasoning_tokens",
                                    0,
                                )
                                or 0
                            ),
                            cached_tokens=cached_tokens,
                        )
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                progress.add(delta.content, getattr(delta, "refusal", None))
                if delta.content:
                    acc.add_text(delta.content)
                    if on_chunk:
                        on_chunk(delta.content)
                # OpenRouter (and OpenAI o-series under some SDKs) streams
                # reasoning text deltas under `reasoning` / `reasoning_content`.
                # Capture into the thoughts channel, never into visible text.
                reasoning_delta = (
                    getattr(delta, "reasoning", None)
                    or getattr(delta, "reasoning_content", None)
                )
                progress.add(reasoning_delta)
                if reasoning_delta:
                    acc.add_thought(reasoning_delta)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        fn = tc.function
                        progress.add(tc.id, fn.name if fn else None, fn.arguments if fn else None)
                        acc.add_tool_delta(
                            tc.index,
                            id=tc.id,
                            name=(fn.name if fn else None),
                            args_delta=(fn.arguments if fn else None),
                        )
        except Exception as exc:
            if message is not None:
                self._interface.drop_trailing(lambda e: e.role == "user")
            # Terminal overflow failure may already have trimmed entries;
            # surface the loss after the revert (see issue #653).
            self._inject_overflow_notice_after_error(exc)
            raise

        # 4. Finalize
        acc.finish_all_tools()
        result = acc.finalize(usage=usage)


        # 5. Record assistant response into interface
        blocks: list = []
        # Persist captured reasoning as a ThinkingBlock so the next request
        # can replay it via reasoning_content (see issue #9).
        if result.thoughts:
            joined = "\n".join(t for t in result.thoughts if t)
            if joined:
                blocks.append(ThinkingBlock(text=joined))
        if result.text:
            blocks.append(TextBlock(text=result.text))
        for tc in result.tool_calls:
            blocks.append(ToolCallBlock(id=tc.id, name=tc.name, args=tc.args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="openai",
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
            },
        )

        return result

    # -- Context compaction ---------------------------------------------------

    def context_window(self) -> int:
        return self._context_window


# ---------------------------------------------------------------------------
# OpenAIResponsesSession
# ---------------------------------------------------------------------------


class OpenAIResponsesSession(ChatSession):
    """Session backed by OpenAI's Responses API with server-side state."""

    def __init__(
        self,
        client: openai.OpenAI,
        model: str,
        instructions: str,
        tools: list[dict] | None,
        tool_choice: str | None,
        extra_kwargs: dict,
        previous_response_id: str | None = None,
        compact_threshold: int | None = None,
        interface: ChatInterface | None = None,
        prompt_cache_key: str | None = None,
        base_url: str | None = None,
        context_window: int = 0,
        stateless_replay: bool = False,
        inject_reasoning_fallback: bool = False,
    ):
        self._client = client
        self._model = model
        self._instructions = instructions
        self._tools = tools
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._response_id: str | None = previous_response_id
        self._stateless_replay = bool(stateless_replay)
        # Generic ``reasoning`` item fallback (the former DeepSeek
        # behavior, now available to any OpenAI-compatible provider): inject
        # a per-turn-unique ``reasoning`` input item on assistant turns after
        # the first ``function_call`` that lack preserved thinking. Default
        # on (env LINGTAI_INJECT_REASONING_FALLBACK to disable); explicit
        # param wins.
        self._inject_reasoning_fallback = bool(inject_reasoning_fallback)
        self._compact_threshold = _validate_compact_threshold(compact_threshold)
        self._interface = interface or ChatInterface()
        # Optional OpenAI Responses ``prompt_cache_key`` — opts the request
        # into cross-request prompt caching keyed by a stable string. Sent
        # only when set; ``None`` leaves it off (default OpenAI behavior).
        # Note: ``prompt_cache_retention`` is deliberately never sent — the
        # Codex backend rejects it (``Unsupported parameter``).
        self._prompt_cache_key = prompt_cache_key
        self._base_url = base_url
        try:
            self._context_window = int(context_window or 0)
        except Exception:
            self._context_window = 0
        # Per-request HTTP timeout (seconds). Set by send_with_timeout before
        # dispatching the worker so the HTTP client aborts at the same moment
        # the main-thread watchdog gives up, instead of keeping the client
        # constructor's independent default while the watchdog has moved on.
        self._request_timeout: float | None = None

    @property
    def interface(self) -> ChatInterface:
        """The canonical ChatInterface for this session."""
        return self._interface

    def context_window(self) -> int:
        return self._context_window

    def _convert_input(self, message) -> list[dict]:
        """Convert messages to Responses API input format.

        ``None`` yields ``[]`` — caller wants the existing
        ``previous_response_id`` chain to continue with no new input.

        A list may carry the canonical kernel shape (``ToolResultBlock``
        instances built by :meth:`OpenAIAdapter.make_tool_result_message` and
        handed down by ``SessionManager.send``) or one of two dictionary shapes:
        a prebuilt Responses ``function_call_output`` item, or a legacy
        Chat-Completions ``{"role": "tool", ...}`` item. Canonical blocks and
        legacy role-tool dictionaries are converted to the Responses
        ``function_call_output`` wire shape here — the same mapping the
        full-replay converter uses (``interface_converters.to_responses_input``)
        — so a plain (non-Codex) Responses session serializes tool-result
        continuations correctly. Prebuilt Responses ``function_call_output``
        dictionaries pass through unchanged.
        """
        if message is None:
            return []
        if isinstance(message, str):
            return [{"role": "user", "content": message}]
        elif isinstance(message, dict):
            return [message]
        elif isinstance(message, list):
            items = []
            for item in message:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call_output"
                ):
                    items.append(item)
                elif isinstance(item, dict) and item.get("role") == "tool":
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": item["tool_call_id"],
                            "output": item["content"],
                        }
                    )
                elif isinstance(item, ToolResultBlock):
                    content = item.content
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": item.id,
                            "output": content
                            if isinstance(content, str)
                            else json.dumps(content, default=str),
                        }
                    )
                else:
                    items.append(item)
            return items
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

    def _snapshot_interface(self, message) -> list[dict] | None:
        if message is None:
            return None
        return self._interface.to_dict()

    def _stage_input(self, message) -> None:
        """Record caller input in canonical history for stateless full replay."""
        if message is None:
            return
        if isinstance(message, str):
            self._interface.add_user_message(message)
        elif isinstance(message, list):
            self._interface.add_tool_results(message)
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

    def _rollback_staged(self, rollback_snapshot: list[dict] | None) -> None:
        if rollback_snapshot is None:
            return
        restored = ChatInterface.from_dict(rollback_snapshot)
        recovery_lookup = self._interface.tool_result_recovery_lookup
        self._interface._entries.clear()
        self._interface._entries.extend(restored._entries)
        self._interface._next_id = restored._next_id
        self._interface._current_system_text = restored._current_system_text
        self._interface._current_tools = restored._current_tools
        self._interface._pending_system = restored._pending_system
        self._interface.tool_result_recovery_lookup = recovery_lookup

    def _record_assistant_response(self, response: LLMResponse) -> None:
        blocks: list = []
        for thought in response.thoughts:
            if thought:
                blocks.append(ThinkingBlock(text=thought))
        if response.text:
            blocks.append(TextBlock(text=response.text))
        for tc in response.tool_calls:
            blocks.append(ToolCallBlock(id=tc.id or "", name=tc.name, args=tc.args))
        if not blocks:
            blocks.append(TextBlock(text=""))
        self._interface.add_assistant_message(
            blocks,
            model=self._model,
            provider="openai",
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "thinking_tokens": response.usage.thinking_tokens,
                "cached_tokens": response.usage.cached_tokens,
            },
        )

    def _replay_input_items(self) -> list[dict]:
        """Return the stateless-replay wire items for the CURRENT interface.

        Extracted seam (not just an inline ``to_responses_input`` call) so a
        subclass with standalone compaction active (e.g.
        ``MimoResponsesSession``) can substitute the opaque compacted-prefix-
        plus-delta representation here instead of a full re-conversion —
        without duplicating ``send``/``send_stream``. Plain (non-compacting)
        Responses sessions keep the original full-conversion behavior. When
        ``inject_reasoning_fallback`` is enabled, assistant turns after the
        first ``function_call`` that lack a preserved ``reasoning`` item get
        a per-turn-unique fallback item injected (generic version of the
        former DeepSeek behavior).
        """
        items = to_responses_input(self._interface)
        if self._inject_reasoning_fallback:
            return _inject_responses_reasoning_fallback(items)
        return items

    def _request_input(self, message) -> list[dict]:
        if not self._stateless_replay:
            return self._convert_input(message)
        self._stage_input(message)
        self._interface.enforce_tool_pairing()
        return self._replay_input_items()

    def send(self, message) -> LLMResponse:
        """Send a user message (str) or tool results (list of dicts)."""
        rollback_snapshot: list[dict] | None = None
        try:
            if self._stateless_replay:
                rollback_snapshot = self._snapshot_interface(message)
            input_items = self._request_input(message)

            # Pre-request hook — in stateless mode the hook mutates the same
            # canonical interface that is serialized below, so spliced entries
            # ride on this request. Stateful mode preserves the historical
            # delta-only behavior.
            if self.pre_request_hook is not None:
                self.pre_request_hook(self._interface)
                if self._stateless_replay:
                    self._interface.enforce_tool_pairing()
                    input_items = self._replay_input_items()

            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": input_items,
                **self._extra_kwargs,
            }
            if self._instructions:
                kwargs["instructions"] = self._instructions
            if self._tools:
                kwargs["tools"] = self._tools
                if self._tool_choice:
                    kwargs["tool_choice"] = self._tool_choice
            if self._response_id and not self._stateless_replay:
                kwargs["previous_response_id"] = self._response_id
            if self._compact_threshold:
                kwargs["context_management"] = [
                    {"type": "compaction", "compact_threshold": self._compact_threshold}
                ]
            if self._prompt_cache_key:
                kwargs["prompt_cache_key"] = self._prompt_cache_key

            raw = self._client.responses.create(**kwargs)
            if isinstance(raw, str):
                logger.warning(
                    "Responses endpoint returned SSE text to a non-streaming request; "
                    "parsing the completed stream locally"
                )
                response, response_id = _consume_responses_stream(
                    _decode_responses_sse_text(raw)
                )
            else:
                response = _parse_responses_api_response(raw)
                response_id = raw.id
            if self._stateless_replay:
                self._record_assistant_response(response)
            else:
                self._adopt_response_id(response_id)
            return response
        except Exception:
            if self._stateless_replay:
                self._rollback_staged(rollback_snapshot)
            raise

    def send_stream(self, message, on_chunk=None, on_output_chars=None) -> LLMResponse:
        """Send a streaming request (``on_output_chars``: count-only progress)."""
        rollback_snapshot: list[dict] | None = None
        try:
            if self._stateless_replay:
                rollback_snapshot = self._snapshot_interface(message)
            input_items = self._request_input(message)

            # Pre-request hook — see send() above for stateless/stateful split.
            if self.pre_request_hook is not None:
                self.pre_request_hook(self._interface)
                if self._stateless_replay:
                    self._interface.enforce_tool_pairing()
                    input_items = self._replay_input_items()

            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": input_items,
                "stream": True,
                **self._extra_kwargs,
            }
            if self._instructions:
                kwargs["instructions"] = self._instructions
            if self._tools:
                kwargs["tools"] = self._tools
                if self._tool_choice:
                    kwargs["tool_choice"] = self._tool_choice
            if self._response_id and not self._stateless_replay:
                kwargs["previous_response_id"] = self._response_id
            if self._compact_threshold:
                kwargs["context_management"] = [
                    {"type": "compaction", "compact_threshold": self._compact_threshold}
                ]
            if self._prompt_cache_key:
                kwargs["prompt_cache_key"] = self._prompt_cache_key

            stream = self._client.responses.create(**kwargs)
            response, response_id = _consume_responses_stream(
                stream, on_chunk, on_output_chars
            )
            if self._stateless_replay:
                self._record_assistant_response(response)
            else:
                self._adopt_response_id(response_id)
            return response
        except Exception:
            if self._stateless_replay:
                self._rollback_staged(rollback_snapshot)
            raise

    def _adopt_response_id(self, response_id: str | None) -> None:
        """Advance the stateful continuation chain, never silently clear it.

        A gateway that ends a stream without any response-carrying event leaves
        ``response_id`` as ``None``. Overwriting the chain with ``None`` would
        drop ``previous_response_id`` from the next request and discard the whole
        server-side conversation with no signal. Keep the last known id and warn.
        """
        if response_id:
            self._response_id = response_id
            return
        logger.warning(
            "Responses request returned no response id; keeping previous "
            "continuation id %r (server-side history may be stale)",
            self._response_id,
        )

    def get_history(self) -> list[dict]:
        """Return minimal state for session persistence (server-side)."""
        if self._stateless_replay:
            return self._interface.to_dict()
        return [{"_response_id": self._response_id}]

    @property
    def session_resume_id(self) -> str | None:
        """Return the response ID for session resumption."""
        if self._stateless_replay:
            return None
        return self._response_id

    def update_tools(self, tools: list[FunctionSchema] | None) -> None:
        if not self._stateless_replay:
            return
        self._tools = _apply_site_quirks(self._base_url, _build_responses_tools(tools))
        self._interface.add_system(
            self._interface.current_system_prompt or "",
            tools=FunctionSchema.list_to_dicts(tools),
        )

    def update_system_prompt(self, system_prompt: str) -> None:
        if not self._stateless_replay:
            return
        self._instructions = system_prompt
        self._interface.add_system(system_prompt, tools=self._interface.current_tools)


# ---------------------------------------------------------------------------
# OpenAIAdapter
# ---------------------------------------------------------------------------


class OpenAIAdapter(LLMAdapter):
    """Adapter that wraps the ``openai`` SDK for OpenAI and compatible APIs."""

    # Session class for the Chat Completions path. MiMo and Zhipu subclasses
    # override this to inject provider-specific behavior. Shared-adapter routes
    # such as DeepSeek configure behavior through constructor hooks instead.
    # Responses-API sessions use OpenAIResponsesSession unconditionally
    # since that path is OpenAI-only.
    _session_class: type = OpenAIChatSession

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_ms: int = 300_000,
        use_responses: bool = False,
        force_responses: bool = False,
        wire_api: str | None = None,
        max_rpm: int = 0,
        default_headers: dict | None = None,
        compact_threshold: int | None = 100_000,
        prompt_cache_key: str | bool | None = None,
        responses_stateless_replay: bool = False,
        inject_reasoning_fallback: bool | None = None,
        reasoning_effort_vocab: str = "openai",
        prompt_cache_namespace: str | None = None,
        reasoning_policy: Callable[..., Any] | None = None,
    ):
        self.base_url = base_url
        self._use_responses = use_responses
        self._force_responses = force_responses
        # Canonical wire selection: ``auto`` delegates to the legacy
        # ``use_responses``/``force_responses`` heuristics; explicit
        # ``chat_completions``/``responses`` force that path regardless of
        # base URL or legacy flags. ``None`` is treated as ``auto`` so
        # existing callers keep their current behavior.
        if wire_api is not None and wire_api not in {"auto", "chat_completions", "responses"}:
            raise ValueError(
                f"wire_api must be one of auto/chat_completions/responses, got {wire_api!r}"
            )
        self._wire_api = wire_api or "auto"
        # Prompt-cache-key policy for this adapter's OpenAI-compatible sessions:
        #   None  -> auto-derive a stable, namespaced default per model
        #   str   -> use this exact key for every session (override)
        #   False -> disable; never send prompt_cache_key
        # Default-on is intentional: every OpenAI-compatible endpoint LingTai
        # talks to accepted the field in the compat probe, and a stable key is
        # what lets successive agent turns hit the cross-request prompt cache.
        if prompt_cache_key is False:
            self._prompt_cache_key_policy: object = False
        elif prompt_cache_key is None:
            self._prompt_cache_key_policy = _AUTO_PROMPT_CACHE_KEY
        else:
            self._prompt_cache_key_policy = prompt_cache_key
        # Responses-API auto-compaction threshold (input tokens). The host
        # injects its resolved config value via the adapter factory
        # (lingtai/llm/_register.py:_openai reads provider defaults); when
        # unset we fall back to the intended 100k default. ``None`` disables
        # compaction entirely. Config is injected at construction here, never
        # read from a global module — see lingtai.kernel.config's contract.
        self._compact_threshold = _validate_compact_threshold(compact_threshold)
        self._responses_stateless_replay = bool(responses_stateless_replay)
        # Generic ``reasoning_content`` round-trip fallback (the former
        # DeepSeek-specific behavior, now available to any OpenAI-compatible
        # provider). On by default: real thinking is already passed back via
        # ThinkingBlock, and this only injects a per-turn-unique stub on
        # assistant tool-call turns that lack preserved thinking — required
        # by thinking-mode endpoints (DeepSeek V4, opencode.ai zen/go) and
        # harmlessly ignored by endpoints that don't know the field. The
        # explicit bool param (from provider config) wins; when unset, the
        # LINGTAI_INJECT_REASONING_FALLBACK env var controls it (default on).
        # Forwarded into both session classes.
        if inject_reasoning_fallback is None:
            inject_reasoning_fallback = _env_bool(
                "LINGTAI_INJECT_REASONING_FALLBACK", default=True
            )
        self._inject_reasoning_fallback = bool(inject_reasoning_fallback)
        # Chat Completions ``reasoning_effort`` vocabulary: ``openai`` (default)
        # maps kernel levels onto OpenAI's high/low surface; ``seven_tier`` is
        # the retained compatibility path that passes THINKING_LEVELS through.
        self._reasoning_effort_vocab = reasoning_effort_vocab
        # Optional provider-local reasoning owner. A route may install a
        # callable ``policy(model=..., wire=..., thinking=...)`` that returns
        # the reasoning decision for that request; when it is installed it
        # decides the reasoning fields for BOTH wires and the generic
        # vocabulary projections below are never consulted. When it is absent
        # — every route except deepseek — this adapter behaves exactly as
        # before. This transport holds no provider's models, levels, aliases,
        # or defaults; see ``lingtai/llm/deepseek/policy.py``.
        self._reasoning_policy = reasoning_policy
        # Optional fixed provider namespace for the auto-derived
        # ``prompt_cache_key`` (e.g. ``deepseek`` -> ``lingtai-deepseek:{model}:v1``).
        self._prompt_cache_namespace = prompt_cache_namespace
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        kwargs["timeout"] = timeout_ms / 1000.0  # openai SDK uses seconds
        kwargs["default_headers"] = merge_lingtai_identity_headers(default_headers)
        self._client_kwargs = dict(kwargs)  # store for session reset
        self._client = openai.OpenAI(**kwargs)
        self._setup_gate(max_rpm)

    # -- Prompt cache key ------------------------------------------------------

    def _default_prompt_cache_key(self, model: str) -> str:
        """Return the auto-derived, namespaced default prompt_cache_key.

        Namespacing keeps incompatible endpoints from sharing a cache slot:
          * explicit ``prompt_cache_namespace`` -> ``lingtai-{namespace}:{model}:v1``
          * official OpenAI (no base_url) -> ``lingtai-openai:{model}:v1``
          * custom/compatible base_url    -> ``lingtai-openai-compat:{host}:{model}:v1``

        A configured ``prompt_cache_namespace`` gives a fixed provider identity
        (DeepSeek, Zhipu, MiMo, Codex) a clean provider namespace instead of
        the base_url host, without needing a subclass override.
        """
        if self._prompt_cache_namespace:
            return f"lingtai-{self._prompt_cache_namespace}:{model}:v1"
        if not self.base_url:
            return f"lingtai-openai:{model}:v1"
        return f"lingtai-openai-compat:{_base_url_namespace(self.base_url)}:{model}:v1"

    def _resolve_prompt_cache_key(self, model: str) -> str | None:
        """Resolve the effective prompt_cache_key for a session on ``model``.

        Honors the adapter's policy: ``False`` disables (returns ``None``), an
        explicit string overrides, and the auto sentinel derives the default.
        """
        policy = self._prompt_cache_key_policy
        if policy is False:
            return None
        if policy is _AUTO_PROMPT_CACHE_KEY:
            return self._default_prompt_cache_key(model)
        return policy  # explicit override string

    def _should_use_responses(self) -> bool:
        """Return True if the selected wire API is the Responses path.

        Canonical ``wire_api`` wins over legacy ``use_responses``/
        ``force_responses`` heuristics:
          * ``chat_completions`` -> always False
          * ``responses``        -> always True, even for custom base URLs
          * ``auto``             -> legacy behavior: ``use_responses`` AND
            (no base URL OR ``force_responses``)
        """
        if self._wire_api == "chat_completions":
            return False
        if self._wire_api == "responses":
            return True
        # auto
        return self._use_responses and (not self.base_url or self._force_responses)

    # -- LLMAdapter interface --------------------------------------------------

    def create_chat(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        *,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        interaction_id: str | None = None,  # ignored — Gemini-specific
        context_window: int = 0,
    ) -> ChatSession:
        # Create interface if not provided
        tool_dicts = FunctionSchema.list_to_dicts(tools)
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=tool_dicts)

        # Select the wire path. Canonical ``wire_api`` wins over legacy flags.
        if self._should_use_responses():
            session = self._create_responses_session(
                model,
                system_prompt,
                tools,
                json_schema,
                force_tool_call,
                interface,
                thinking,
                context_window=context_window,
            )
        else:
            # Fallback: Chat Completions for compatible providers
            session = self._create_completions_session(
                model, system_prompt, tools, json_schema, force_tool_call, interface, thinking,
                context_window=context_window,
            )
        return self._wrap_with_gate(session)

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        context_window: int = 0,
    ) -> OpenAIResponsesSession:
        # Create interface if not provided
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _apply_site_quirks(self.base_url, _build_responses_tools(tools))
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}

        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        # Responses API takes `reasoning: { effort: ... }`, not the Chat
        # Completions SDK's flat `reasoning_effort`. Sending the wrong shape
        # silently drops the field on the OpenAI Responses endpoint and 400s
        # on Codex's `/backend-api/codex/responses`.
        #
        # A route with a provider-local reasoning policy owns this decision
        # entirely — including whether any field is sent at all — and may raise
        # before the SDK is ever touched.
        applied = self._apply_reasoning_policy(
            model=model, wire="responses", thinking=thinking, extra_kwargs=extra_kwargs
        )
        if applied is None:
            extra_kwargs.update(_responses_reasoning_kwargs(thinking))

        session = OpenAIResponsesSession(
            client=self._client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=self._compact_threshold,
            interface=interface,
            prompt_cache_key=self._resolve_prompt_cache_key(model),
            context_window=context_window,
            stateless_replay=self._responses_stateless_replay,
            inject_reasoning_fallback=self._inject_reasoning_fallback,
        )
        return _capture_reasoning_application(session, applied)

    def _apply_reasoning_policy(
        self,
        *,
        model: str,
        wire: str,
        thinking: Any,
        extra_kwargs: dict[str, Any],
    ) -> Any:
        """Let an installed provider-local policy own the reasoning fields.

        Returns the resolved application (for capture on the session), or
        ``None`` when no policy is installed so the caller keeps the generic
        OpenAI projection. Transport-neutral: nothing here knows any provider's
        models, levels, aliases, or defaults.
        """
        policy = self._reasoning_policy
        if policy is None:
            return None
        applied = policy(model=model, wire=wire, thinking=thinking)
        payload = applied.request_kwargs()
        # ``extra_body`` is a shared channel — a provider extension, a subclass
        # contribution and a caller override can all want a key in it — so it
        # composes instead of overwriting; unrelated existing keys survive.
        extra_body = payload.pop("extra_body", None)
        extra_kwargs.update(payload)
        if extra_body:
            extra_kwargs["extra_body"] = {
                **(extra_kwargs.get("extra_body") or {}),
                **extra_body,
            }
        return applied

    def _create_completions_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        context_window: int = 0,
    ) -> OpenAIChatSession:
        # Create interface if not provided
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        openai_tools = _apply_site_quirks(self.base_url, _build_tools(tools))
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        # Extra kwargs for the completions call
        extra_kwargs: dict[str, Any] = {}

        # JSON schema enforcement (OpenAI Structured Outputs)
        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        # Generic Chat Completions reasoning projection. The retained
        # ``seven_tier`` compatibility vocabulary passes kernel levels through
        # unchanged, while the default ``openai`` vocabulary clamps
        # ``xhigh``/``max`` to ``high`` and omits the field for the
        # omitted/``default`` sentinel so the upstream v1 default applies.
        # A route with a provider-local reasoning policy owns this decision
        # entirely (DeepSeek, for instance, also emits its own ``thinking``
        # switch and rejects levels its model does not really have); the
        # generic vocabulary projection is then never consulted.
        applied = self._apply_reasoning_policy(
            model=model,
            wire="chat_completions",
            thinking=thinking,
            extra_kwargs=extra_kwargs,
        )
        if applied is None:
            effort = self._chat_reasoning_effort(thinking)
            if effort is not None:
                extra_kwargs["reasoning_effort"] = effort

        # Subclass-provided extra_body (e.g. OpenRouter's reasoning include).
        # Merge rather than overwrite so callers adding their own extra_body
        # via extra_kwargs aren't clobbered.
        sub_extra_body = self._adapter_extra_body()
        if sub_extra_body:
            existing = extra_kwargs.get("extra_body") or {}
            extra_kwargs["extra_body"] = {**sub_extra_body, **existing}

        session = self._session_class(
            client=self._client,
            model=model,
            interface=interface,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            client_kwargs=self._client_kwargs,
            context_window=context_window,
            prompt_cache_key=self._resolve_prompt_cache_key(model),
            inject_reasoning_fallback=self._inject_reasoning_fallback,
            base_url=self.base_url,
        )
        return _capture_reasoning_application(session, applied)

    def _adapter_extra_body(self) -> dict:
        """Return extra_body JSON fields to include on every request.

        Default is empty. Subclasses override to inject provider-specific
        kwargs (e.g. OpenRouter needs `reasoning: {include: true}` to
        surface reasoning text on reasoning-capable models).
        """
        return {}

    def _chat_reasoning_effort(self, thinking: str | None) -> str | None:
        """Map a kernel thinking level to the Chat Completions reasoning_effort value.

        This is the v1 projection of the Responses semantics (which sends
        ``reasoning: {effort: <level>}`` verbatim and maps an omitted/default
        level to explicit ``xhigh``). The vocabulary is selected by
        ``reasoning_effort_vocab``:

          * ``openai`` (default) projects the Responses vocabulary onto OpenAI
            v1's official set ``minimal | low | medium | high``: explicit
            ``minimal``/``low``/``medium``/``high`` pass through, ``xhigh`` and
            ``max`` clamp to ``high`` (v1 has no higher tier), ``none`` maps to
            ``None`` so the field is omitted (no reasoning control), and the
            omitted/``default`` sentinel maps to ``None`` so the upstream v1
            default applies (OpenAI v1 has no ``xhigh``; omission is the
            graceful v1 projection of the Responses xhigh default).
          * ``seven_tier`` is the retained compatibility projection: it passes
            the kernel THINKING_LEVELS through unchanged and maps the omitted/
            ``default`` sentinel to explicit ``xhigh``. Provider-specific routes
            with a different model/wire contract use a provider-local policy.

        Returns ``None`` so the field is not sent.
        """
        from lingtai.kernel.config import THINKING_LEVELS

        if self._reasoning_effort_vocab == "seven_tier":
            if thinking in (None, "default"):
                return "xhigh"
            if thinking not in THINKING_LEVELS:
                raise ValueError(
                    "thinking must be one of "
                    f"{', '.join(THINKING_LEVELS)}, or default"
                )
            return thinking
        if thinking in (None, "default", "none"):
            return None
        if thinking not in THINKING_LEVELS:
            raise ValueError(
                "thinking must be one of "
                f"{', '.join(THINKING_LEVELS)}, or default"
            )
        return "high" if thinking in ("xhigh", "max") else thinking

    def generate(
        self,
        model: str,
        contents: str | list,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        # One-shot generation must follow the same wire path as session creation.
        if self._should_use_responses():
            return self._generate_responses(
                model,
                contents,
                system_prompt=system_prompt,
                temperature=temperature,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
            )
        return self._generate_chat_completions(
            model,
            contents,
            system_prompt=system_prompt,
            temperature=temperature,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )

    def _generate_chat_completions(
        self,
        model: str,
        contents: str | list,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # contents can be a string or a list of content blocks
        if isinstance(contents, str):
            messages.append({"role": "user", "content": contents})
        elif isinstance(contents, list):
            messages.append({"role": "user", "content": contents})
        else:
            messages.append({"role": "user", "content": str(contents)})

        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_output_tokens is not None:
            kwargs["max_tokens"] = max_output_tokens

        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        raw = self._gated_call(lambda: self._client.chat.completions.create(**kwargs))
        return _parse_response(raw)

    def _generate_responses(
        self,
        model: str,
        contents: str | list,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        # contents can be a string or a list of content blocks
        if isinstance(contents, str):
            input_items = [{"role": "user", "content": contents}]
        elif isinstance(contents, list):
            input_items = [{"role": "user", "content": contents}]
        else:
            input_items = [{"role": "user", "content": str(contents)}]

        kwargs: dict[str, Any] = {"model": model, "input": input_items}
        if system_prompt:
            kwargs["instructions"] = system_prompt
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens

        if json_schema is not None:
            # The Responses API selects structured output via ``text.format``
            # (openai>=2.x). It has NO ``response_format`` kwarg — unlike Chat
            # Completions — so passing one raises on the installed SDK.
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": json_schema.get("title", "response"),
                    "schema": json_schema,
                    "strict": True,
                },
            }

        raw = self._gated_call(lambda: self._client.responses.create(**kwargs))
        return _parse_responses_api_response(raw)

    def make_tool_result_message(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None
    ) -> ToolResultBlock:
        """Build a canonical ToolResultBlock."""
        return ToolResultBlock(
            id=tool_call_id or f"call_{uuid.uuid4().hex[:24]}",
            name=tool_name,
            content=result,
        )

    def is_quota_error(self, exc: Exception) -> bool:
        """Check if the exception is an OpenAI rate-limit error."""
        return isinstance(exc, openai.RateLimitError)

    # -- Convenience properties ------------------------------------------------

    @property
    def client(self):
        """Escape hatch — the underlying ``openai.OpenAI`` client."""
        return self._client


# ---------------------------------------------------------------------------
# CodexResponsesSession — stateless variant for ChatGPT-OAuth backend
# ---------------------------------------------------------------------------


# The hard forced-rebuild boundary (1.0). At/above this context usage the runtime
# forces a fresh provider-context replay on the next request regardless of whether
# pending summaries exist.
_CODEX_FORCED_REBUILD_THRESHOLD_RATIO = CONTEXT_PRESSURE_FORCED_REBUILD_RATIO


class _StandaloneCompactionMixin:
    """Shared standalone ``/responses/compact`` machinery.

    Owns the provider-agnostic half of standalone Responses compaction: the
    projected-token trigger (``_maybe_compact_before_send``), the opaque
    compacted-prefix-plus-strict-additive-delta replay basis
    (``_compacted_replay_input``), and the turn-aware boundary selection that
    builds the next compact request's ``input`` (``_prepare_compact_request``).
    First extracted from ``CodexResponsesSession`` (PR #926) so a second
    provider (MiMo) can reuse it without duplicating the calibration/boundary
    logic — see PR #926 for the original design rationale and
    ``tests/test_codex_standalone_compaction.py`` for the behavior contract.

    Subclasses differ in exactly two ways, both left as seams:
      * ``_compaction_prefix_input(entries)`` — how a list of canonical
        ``ChatInterface`` entries converts to Responses-wire items for the
        compact request / delta replay. Codex routes this through its
        per-session tool-result output freezing; a plain stateless session
        uses the ordinary converter.
      * ``_compact_now()`` — the actual ``client.responses.compact()`` call
        and its failure policy. Codex treats any failure as non-fatal (skip
        compaction for this turn); MiMo treats it as a hard failure (see
        ``MimoResponsesSession._compact_now``). This mixin does not call
        ``client.responses.compact`` itself; it only prepares the request.

    Requires the host class to also provide (already true of
    ``OpenAIResponsesSession``): ``self._interface``, ``self._model``,
    ``self._instructions``, ``self._tools``, ``self._client``,
    ``self._prompt_cache_key``, ``self.context_window()``.
    """

    def _init_standalone_compaction(self, compact_token_limit: int | None) -> None:
        self._compact_token_limit = _validate_codex_compact_token_limit(compact_token_limit)
        self._compacted_items: list[dict[str, Any]] | None = None
        self._compacted_at_entry_count: int = 0
        # Calibration pair for ``_projected_provider_tokens``: the provider's
        # actual reported input-token count and a LOCAL estimate of the exact
        # rendered representation that produced it, captured together right
        # after a successful response (see each host session's own
        # post-response hook — ``CodexResponsesSession.send_stream`` /
        # ``MimoResponsesSession._record_calibration_sample``). Both ``None``
        # until the first successful provider response.
        self._last_provider_input_tokens: int | None = None
        self._last_local_estimate_tokens: int | None = None

    def _compaction_prefix_input(self, entries: list[Any]) -> list[dict[str, Any]]:
        """Convert canonical entries to Responses-wire items for compaction.

        Default: the plain converter over a temporary interface (no
        provider-specific freezing). Subclasses override for provider-specific
        wire shaping.
        """
        if not entries:
            return []
        temp_interface = ChatInterface()
        temp_interface.entries.extend(entries)
        return to_responses_input(temp_interface)

    def _effective_compact_token_limit(self) -> int | None:
        """Return the resolved compaction threshold, or ``None`` if disabled.

        An explicit per-task ``context_token_limit`` (``self._compact_token_limit``)
        wins. When the task omitted it, the effective threshold falls back to
        this session's resolved ``context_window()`` — the parent service's
        context window, per daemon task contract. A session with no window
        configured (``context_window() <= 0``) has compaction disabled.

        This value is always the public, unmodified ``context_token_limit``
        (or its context-window fallback) — an UPPER BOUND the provider-visible
        input must stay under. ``_maybe_compact_before_send`` supplies the
        margin by comparing a PROJECTED provider-token count against this
        bound, rather than shrinking the bound itself by a fixed fraction.
        """
        if self._compact_token_limit is not None:
            return self._compact_token_limit
        try:
            window = int(self.context_window())
        except Exception:
            return None
        return window if window > 0 else None

    def _current_request_representation(
        self, prebuilt_items: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the EXACT wire representation the next request would send.

        While standalone compaction is active, this is the opaque
        ``_compacted_items`` verbatim plus the strict-additive delta (the SAME
        shape ``_compacted_replay_input`` returns); otherwise it is the full
        converted canonical interface. ``prebuilt_items`` — pre-built
        Responses-wire dicts or legacy ``role=tool`` items a caller passed
        directly to ``send`` — are appended in both cases. This exists so the
        compaction trigger can estimate tokens over what will ACTUALLY be
        sent, not a representation that silently drops dict/list legacy input
        items.
        """
        compacted_replay = self._compacted_replay_input()
        if compacted_replay is not None:
            items = compacted_replay
        else:
            items = self._compaction_prefix_input(self._interface.entries)
        if prebuilt_items:
            items = [*items, *prebuilt_items]
        return items

    def _projected_provider_tokens(
        self, prebuilt_items: list[dict[str, Any]] | None = None,
    ) -> int | None:
        """Project the provider-visible input-token count for the NEXT request.

        Calibrates a local estimate of the EXACT rendered request
        representation (``_current_request_representation``) against the last
        real provider round-trip: ``_last_local_estimate_tokens`` is the local
        estimate computed for the SAME rendered representation that was
        actually sent for the request whose reported actual is
        ``_last_provider_input_tokens`` (both captured together right after a
        successful response, using the SAME representation that request's
        ``kwargs["input"]`` held). ``calibration = provider_actual /
        local_estimate`` for that one paired sample; the current live local
        estimate is scaled by that ratio to project what the provider would
        currently report.

        This is deliberately NOT calibrated against
        ``ChatInterface.estimate_context_tokens()`` (the full raw canonical
        history) — before compaction the two are roughly the same
        representation, but after compaction they diverge. See PR #926 Sol
        source-audit finding.

        Falls back to the raw current representation estimate (an implicit
        1:1 calibration) when no paired sample exists yet.

        Returns ``None`` when even the raw representation estimate cannot be
        computed (fail-safe: no projection, no trigger).
        """
        try:
            current_items = self._current_request_representation(prebuilt_items)
            current_estimate = _estimate_responses_input_tokens(
                self._instructions, self._tools, current_items,
            )
        except Exception:
            return None
        if current_estimate <= 0:
            return current_estimate
        provider_actual = self._last_provider_input_tokens
        local_sample = self._last_local_estimate_tokens
        if provider_actual and local_sample and local_sample > 0:
            calibration = provider_actual / local_sample
            return int(current_estimate * calibration)
        return current_estimate

    def _maybe_compact_before_send(
        self, prebuilt_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """Compact provider context via standalone ``/responses/compact`` if due.

        Triggers from a PROJECTED provider-visible token count, not from
        cumulative session spend and not by waiting for a past request to have
        already crossed the limit. A no-op when: no threshold is configured or
        the projection stays below the threshold.

        When compaction is ALREADY active, this re-arms rather than blocking
        outright: if the post-compaction delta has itself grown enough for
        ``find_compaction_boundary`` to find a NEW boundary strictly past the
        current one, compaction re-fires using the existing opaque
        ``_compacted_items`` plus the delta up to that new boundary as input.
        If no new boundary exists yet, this is a no-op.
        """
        limit = self._effective_compact_token_limit()
        if limit is None:
            return
        projected = self._projected_provider_tokens(prebuilt_items)
        if projected is None or projected < limit:
            return
        self._compact_now()

    def _prepare_compact_request(self) -> tuple[int, list[dict[str, Any]]] | None:
        """Resolve the compaction boundary and build the compact request's input.

        Compacts only the OLDER portion of canonical history, determined by
        ``ChatInterface.find_compaction_boundary(keep_turns=1)`` — deliberately
        called with ``keep_turns=1`` (not the library's generic default of 3):
        fold everything safely old, keep only the ONE newest complete live
        turn (and any tool_call/tool_result pair it carries) uncompacted.
        When there isn't yet enough history for a safe boundary
        (``find_compaction_boundary`` returns ``None``), returns ``None`` —
        compaction is skipped entirely for this turn rather than guessing a
        boundary.

        Re-arm: if ``_compacted_items`` is already active, this finds a NEW
        boundary against the full current entry list. If that boundary has
        not moved past the existing ``_compacted_at_entry_count``, returns
        ``None`` — nothing safe to fold yet. Otherwise the returned input is
        the existing opaque ``_compacted_items`` verbatim, followed by the
        delta entries between the OLD and NEW boundary.

        Returns ``(boundary_index, full_input)``, or ``None`` when there is
        nothing safe/new to compact.
        """
        boundary_id = self._interface.find_compaction_boundary(keep_turns=1)
        if boundary_id is None:
            return None
        entries = self._interface.entries
        boundary_index = next(
            (i for i, entry in enumerate(entries) if entry.id == boundary_id),
            None,
        )
        if boundary_index is None or boundary_index <= 0:
            return None

        already_compacted = self._compacted_items is not None
        if already_compacted:
            if boundary_index <= self._compacted_at_entry_count:
                return None
            delta_entries = entries[self._compacted_at_entry_count:boundary_index]
            delta_items = self._compaction_prefix_input(delta_entries)
            full_input = [*self._compacted_items, *delta_items]
        else:
            full_input = self._compaction_prefix_input(entries[:boundary_index])
        if not full_input:
            return None
        return boundary_index, full_input

    def _compacted_replay_input(self) -> list[dict[str, Any]] | None:
        """Return the compacted replay basis for THIS turn, or ``None`` if inactive.

        The replay is the opaque compacted items verbatim, followed by the
        strict-additive delta: canonical interface entries at/after the
        compaction boundary (``_compacted_at_entry_count``, the newest kept
        turn plus everything appended since). Because the boundary always
        keeps at least one complete turn, this delta is never empty once
        compaction has fired — the live turn that triggered compaction rides
        here, verbatim.
        """
        if self._compacted_items is None:
            return None
        delta_entries = self._interface.entries[self._compacted_at_entry_count:]
        delta_items = self._compaction_prefix_input(delta_entries)
        return [*self._compacted_items, *delta_items]


@dataclass
class _CodexAccountContext:
    """Mutable Codex account state owned by one canonical chat context.

    ``CodexOpenAIAdapter`` instances are cached by ``LLMService`` and therefore
    may create unrelated chats concurrently.  Credentials, headers, failover
    exclusions, and the authenticated client must not live on that shared
    adapter.  A context is attached to its ``ChatInterface`` so an AED rebuild
    can continue the same exclusion chain without making another live chat
    share state accidentally.
    """

    owner: object
    model: str
    client: Any
    binding: dict[str, Any] = field(default_factory=dict)
    binding_generation: int = 0
    selection: dict[str, Any] = field(default_factory=dict)
    bound_molt_count: int | None = None
    excluded_accounts: set[str] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class CodexResponsesSession(_StandaloneCompactionMixin, OpenAIResponsesSession):
    """Responses session for Codex's `/backend-api/codex/responses`.

    Differences from the parent:
      * Each turn is planned as `full` or `incremental` independent of
        transport. REST sends the full converted interface for both modes;
        `incremental` only means the prefix/cache epoch is unchanged. WebSocket
        incremental sends the strict-additive delta plus `previous_response_id`.
      * Transport is selectable. REST is the default; WebSocket remains an
        explicit opt-in compatibility/performance path.
      * `store=False` is forced because Codex rejects `store=true`.
      * Streaming is forced (`stream=True` on send/send_stream alike) —
        non-streaming Codex requests return data the SDK can't unmarshal.
      * Optional stable ``session_id`` / ``thread_id`` request headers are
        sent for REST prompt-cache affinity (issue #378). They are HTTP
        headers (``extra_headers``), not request-body fields, and are
        independent of ``prompt_cache_key`` (both may be sent together). The
        keys are underscored (``session_id`` / ``thread_id``) to match the
        Codex backend literally — a hyphenated spelling loses cache affinity.
    """

    def __init__(
        self,
        *args,
        session_id: str | None = None,
        thread_id: str | None = None,
        account_id: str | None = None,
        codex_auth_path_sha8: str | None = None,
        codex_auth_path_source: str | None = None,
        codex_pool_selection: dict[str, Any] | None = None,
        codex_account_error_callback: Callable[[Exception, bool], None] | None = None,
        codex_account_success_callback: Callable[[], dict[str, Any] | None] | None = None,
        codex_account_request_callback: Callable[
            [Callable[[dict[str, Any]], None]], dict[str, Any]
        ] | None = None,
        codex_token_expired_callback: Callable[
            [
                Exception,
                int | None,
                str | None,
                str | None,
                Callable[[dict[str, Any]], None],
                Callable[[], Any],
            ],
            Any | None,
        ] | None = None,
        codex_account_epoch_reset_callback: Callable[[str], None] | None = None,
        installation_id: str | None = None,
        metadata_sandbox: str = "lingtai",
        transport: str | None = None,
        ws_enabled: bool | None = None,
        ws_epoch_reset_turns: int | None = None,
        ws_transport_factory: "Callable[[str, dict[str, str]], Any] | None" = None,
        base_url: str | None = None,
        api_key: str | None = None,
        compact_token_limit: int | None = None,
        effort_descriptor: "CodexEffortDescriptor | None" = None,
        **kwargs,
    ):
        super().__init__(*args, base_url=base_url, **kwargs)
        # Runtime reasoning-effort route (issue #1197 K1a). ``None`` -> this
        # session is NOT on the one authorized active route (wrong model, empty
        # endpoint, wrong wire, or invalid construction effort), so the capability
        # stays unavailable and request kwargs are never touched. See ``codex_effort.py``.
        self._effort_descriptor = effort_descriptor
        # Owner-installed per-dispatch policy provider. Called at most ONCE per
        # logical dispatch, from the single capture point in ``send_stream``.
        self._effort_policy_provider: (
            "Callable[[], ReasoningEffortSnapshot | None] | None"
        ) = None
        # Safe evidence for the most recent dispatch (see
        # ``last_reasoning_effort_dispatch``). Never carries prompts, bodies,
        # auth material, or raw provider objects.
        self._last_effort_dispatch: dict[str, Any] | None = None
        # Standalone Codex compaction (daemon task ``context_token_limit``).
        # Distinct axis from ``compact_threshold``/``context_management``,
        # which Codex never receives (see ``_create_responses_session``).
        # ``None`` -> no explicit task limit; the effective threshold falls
        # back to ``context_window()`` at check time (see
        # ``_effective_compact_token_limit``). Validated once here (in the
        # shared mixin init) so an invalid daemon task value fails at session
        # construction. See ``_StandaloneCompactionMixin`` for
        # ``_compacted_items`` / ``_compacted_at_entry_count`` /
        # ``_last_local_estimate_tokens`` field docs.
        self._init_standalone_compaction(compact_token_limit)
        # Transport axis (REST vs WebSocket). Both transports run the SAME
        # full->incremental planner (``_codex_plan_continuation``); this only
        # selects HOW the planned request is sent. Resolution priority:
        #   * explicit ``transport=`` kwarg (``rest``/``websocket``) wins;
        #   * else the legacy ``ws_enabled=`` kwarg (True -> websocket) for
        #     back-compat with existing tests/wiring;
        #   * else the environment-variable OPT-IN selector
        #     (``LINGTAI_CODEX_TRANSPORT=websocket`` / legacy ``LINGTAI_CODEX_WS=1``);
        #   * else the hardcoded normal-runtime default: ``rest``.
        # The env selector is deliberately opt-in: unset or any invalid value
        # keeps REST, so an inherited variable can never accidentally flip a
        # Codex agent onto the WebSocket wire.
        # ``ws_transport_factory`` is an injection seam used by the mock tests;
        # when ``None`` and the websocket transport is selected, the real factory
        # is used (and itself falls back to HTTP if ``websockets`` is missing).
        # ``_ws_session`` holds the per-turn last_request/last_response (+ captured
        # ``x-codex-turn-state`` on the WS path) used to compute incremental deltas
        # exactly like the official ``ModelClientSession`` (``client.rs:214-240``).
        if transport is not None:
            self._transport = "websocket" if str(transport).strip().lower() in {"websocket", "ws"} else "rest"
        elif ws_enabled is not None:
            self._transport = "websocket" if bool(ws_enabled) else "rest"
        else:
            self._transport = _codex_transport_from_env()
        # True when the WebSocket wire is selected. The REST transport leaves this
        # False but STILL runs the full/incremental state machine below.
        self._ws_enabled = self._transport == "websocket"
        # The full->incremental continuation state machine runs for BOTH
        # transports. Kept as a separate flag (always True today) so the gating
        # reads as "continuation enabled" rather than "websocket enabled", and a
        # future stateless-only mode could flip it without touching transport.
        self._continuation_enabled = True
        self._ws_transport_factory = ws_transport_factory
        self._ws_base_url = base_url
        self._ws_api_key = api_key if isinstance(api_key, str) and api_key else None
        self._ws_session = _CodexWebsocketSession()
        # Set while a websocket request is in flight: the full converted input we
        # sent this turn, used by ``_ws_record_baseline_from_interface`` to derive
        # a converter-stable delta baseline once the assistant turn is recorded.
        self._ws_pending_baseline_input: list[dict[str, Any]] | None = None
        # Per-session freeze of model-facing ``function_call_output.output`` strings
        # keyed by ``call_id``. A result's converted ``output`` can change after
        # it was first sent (summary markers, placeholder overwrites, AED spill
        # manifests), which would break the
        # strict-prefix WS delta baseline on replay. Freezing the first-seen
        # output per call_id keeps ordinary replay byte-identical while the
        # freshest result still carries live meta (it is first-seen on its own
        # turn). See ``_freeze_responses_outputs``.
        self._ws_frozen_outputs: dict[str, str] = {}
        # Last websocket delta decision diagnostic (safe metadata only): why the
        # request went ``ws_incremental`` vs ``ws_full``. Surfaced in usage.extra.
        self._ws_last_diag: dict[str, Any] = {}
        self._ws_cache_ledger: deque[dict[str, Any]] = deque(maxlen=20)
        self._ws_cache_call_seq = 0
        # One Codex websocket connection may carry multiple ``response.create``
        # frames. Live smoke showed ChatGPT Codex resolves
        # ``previous_response_id`` only when the follow-up request stays on the
        # same websocket session. Keep the transport alive across sequential
        # sends in this ChatSession, and close/reset it on transport failures.
        self._ws_transport = None
        self._ws_epoch_reset_turns_explicit = ws_epoch_reset_turns is not None
        if ws_epoch_reset_turns is None:
            self._ws_epoch_reset_turn_limit = _codex_ws_epoch_reset_turns()
        else:
            self._ws_epoch_reset_turn_limit = max(0, int(ws_epoch_reset_turns))
        self._ws_turns_since_epoch_reset = 0
        self._ws_epoch_reset_reason_pending: str | None = None
        self._forced_rebuild_threshold_ratio = _CODEX_FORCED_REBUILD_THRESHOLD_RATIO
        self._summarize_effect_delayed_pending_ids: set[str] = set()
        self._summarize_effect_delayed_last_context: dict[str, Any] = {}
        self._summarize_effect_delayed_last_release_reason: str | None = None
        # One-shot pending reconstruction event (channel A). Set when an actual
        # delayed-summarize reconstruction fires (_reset_ws_epoch with reason
        # "summarize_delayed"); captures the before-context (A) plus fixed
        # trigger/recovery metadata. The kernel pops it via
        # take_pending_reconstruction_event() and attaches it once to the next
        # visible tool result's _meta.tool_meta (permanent evidence), filling the
        # after-context (B) and any molt warning at attach time.
        self._pending_reconstruction_event: dict[str, Any] | None = None
        # Last real provider request size (``_last_provider_input_tokens``,
        # initialized by ``_init_standalone_compaction`` above). Codex delayed
        # summarize release must be keyed to the previous request's input
        # tokens, not ChatInterface's local estimate, because the latter can
        # omit provider-visible prompt and tool-result mass. See PR #535 live
        # probe.
        # One automatic forced provider-context rebuild per continuous
        # hard-boundary episode (Jason, 2026-07-12; separate PR after #896).
        # ``_hb_rebuild_fired`` latches True when the automatic forced rebuild
        # fires at provider usage ``>= 1.0`` so it does NOT fire again while usage
        # stays ``>= 1.0`` (including exactly 1.0); it re-arms (back to False) only
        # after a later provider usage drops STRICTLY below 1.0. Shared by BOTH
        # automatic paths — the pre-request boundary check
        # (``_maybe_force_rebuild_at_boundary``) and the immediate
        # ``on_history_summarized`` release — via ``_fire_boundary_forced_rebuild``.
        # ``_hb_rebuild_awaiting_verify`` stays True from the fire until the first
        # post-rebuild provider response is observed, so the persistent
        # ``Forced Rebuilt Failed`` overflow warning is withheld until the forced
        # fresh replay's own provider input is known (a failed forced request never
        # updates ``_last_provider_input_tokens``, so verification stays pending).
        # See ``_observe_provider_usage_for_boundary`` / ``context_overflow_status``.
        # Explicit ``request_history_rebuild`` is independent and never touches
        # these flags.
        self._hb_rebuild_fired = False
        self._hb_rebuild_awaiting_verify = False
        # The user's own ChatGPT account id (decoded upstream from their OAuth
        # auth data). When present it is sent as the ``ChatGPT-Account-ID`` HTTP
        # Account routing is a ChatGPT-account concern and intentionally
        # orthogonal to the app-name identity (``originator``/``User-Agent``),
        # which remains honest LingTai by default (see
        # ``_codex_identity_headers`` / ``_CODEX_IMPERSONATE_OFFICIAL_CLI``).
        # It is a non-secret account identifier and is never copied into usage
        # metadata or logs.
        self._account_id = account_id if isinstance(account_id, str) and account_id else None
        # Native Codex account selection is owned by this session's provider
        # request path.  The callbacks never receive canonical history or
        # provider policy; they only refresh auth and report a safe selection /
        # structural failure to the ordinary Codex adapter.
        self._codex_pool_selection = (
            dict(codex_pool_selection) if isinstance(codex_pool_selection, dict) else {}
        )
        self.codex_pool_selection = dict(self._codex_pool_selection)
        # A deliberate context epoch reset already rebases the continuation
        # state. If the fresh draw chooses another account, do not emit a second
        # technical account-switch reset and overwrite the approved boundary
        # reason in diagnostics.
        self._codex_account_epoch_boundary_pending = False
        self._codex_account_error_callback = codex_account_error_callback
        self._codex_account_success_callback = codex_account_success_callback
        self._codex_account_request_callback = codex_account_request_callback
        self._codex_token_expired_callback = codex_token_expired_callback
        self._codex_account_epoch_reset_callback = codex_account_epoch_reset_callback
        self._codex_binding_generation: int | None = None
        self._codex_binding_api_key: str | None = None
        self._codex_token_expired_recovery_attempted = False
        self._codex_partial_output = False
        self._codex_account_error_reported = False
        self._codex_request_deadline: float | None = None
        # Non-secret attribution for token/debug ledgers. The raw account id is
        # already needed for the ChatGPT-Account-ID request header, but it must
        # never be copied into UsageMetadata.extra / token_ledger / events.
        # Store only caller-provided safe labels/hashes for later serialization.
        self._codex_auth_path_sha8 = (
            str(codex_auth_path_sha8) if codex_auth_path_sha8 else None
        )
        self._codex_auth_path_source = (
            str(codex_auth_path_source) if codex_auth_path_source else None
        )
        self._installation_id = installation_id
        self._metadata_sandbox = metadata_sandbox or "lingtai"
        # Codex REST cache-affinity identity: ONE stable per-agent value used
        # byte-identically for ``prompt_cache_key`` / ``session_id`` /
        # ``thread_id`` on EVERY request, and NEVER changed for the life of the
        # session. There is no epoch-stamping and no rotation — the id is a pure
        # deterministic hash of the agent path (resolved upstream by the adapter
        # and passed in here). Priority for the single value: ``prompt_cache_key``
        # (explicit request-body cache key) > ``session_id`` > ``thread_id``.
        #
        # The header carve-out (issue #378): ``session_id`` / ``thread_id``
        # headers route the backend cache slot and MUST be per-agent. The
        # model-only fallback ``prompt_cache_key`` form
        # (``lingtai-codex:{model}:v1``) is shared by every agent on a model, so
        # it is NEVER promoted to headers (that would collapse all agents onto one
        # slot). Headers are emitted only when an explicit ``session_id`` /
        # ``thread_id`` was supplied (the per-agent path or a direct-construction
        # test); a cache-key-only construction (bare/no-anchor) keeps its body
        # ``prompt_cache_key`` and sends NO headers.
        self._current_id = self._prompt_cache_key or session_id or thread_id
        self._prompt_cache_key = self._current_id
        self._has_header_identity = bool(session_id or thread_id)
        if self._has_header_identity:
            self._session_id = self._current_id
            self._thread_id = self._current_id
        else:
            self._session_id = None
            self._thread_id = None

    def _cache_affinity_headers(self) -> dict[str, str]:
        """Return the stable ``session_id`` / ``thread_id`` headers, if any.

        The header names use UNDERSCORES (``session_id`` / ``thread_id``) to match
        what the official Codex CLI sends on its
        ``/backend-api/codex/responses`` calls (verified by capturing real Codex
        CLI traffic, 2026-06). This spelling is load-bearing: the Codex backend
        matches the literal underscore key, so emitting hyphenated
        ``session-id`` / ``thread-id`` would silently lose cache affinity and
        fragment every request onto a cold slot (cache/cost explosion). Do NOT
        rename these to HTTP-looking hyphenated forms. The backend routes the
        prompt-cache slot to a sticky-warm replica off a STABLE session id; we
        send one fixed per-agent value for the life of the session and never
        change it.
        """
        headers: dict[str, str] = {}
        if self._session_id:
            headers["session_id"] = self._session_id
        if self._thread_id:
            headers["thread_id"] = self._thread_id
        return headers

    def _codex_metadata_headers(self) -> dict[str, str]:
        """Return honest LingTai Codex metadata headers for this request."""

        if not (self._session_id and self._thread_id):
            return {}
        turn_metadata = {
            "session_id": self._session_id,
            "thread_id": self._thread_id,
            "turn_id": str(uuid.uuid4()),
            "sandbox": self._metadata_sandbox,
            "turn_started_at_unix_ms": int(time.time() * 1000),
        }
        return {
            "x-client-request-id": str(uuid.uuid4()),
            "x-codex-window-id": f"{self._session_id}:0",
            "x-codex-turn-metadata": json.dumps(turn_metadata, separators=(",", ":"), sort_keys=True),
        }

    def _codex_client_metadata(self) -> dict[str, str]:
        if not self._installation_id:
            return {}
        return {"x-codex-installation-id": self._installation_id}

    def _effective_affinity(self) -> tuple[str | None, dict[str, str]]:
        """Resolve this request's (prompt_cache_key, headers) pair.

        Always the single stable per-agent id — fixed for the life of the
        session, used byte-identically for ``prompt_cache_key`` / ``session_id``
        / ``thread_id`` on every request. No rotation, no epoch, no time
        dependence.
        """
        return self._prompt_cache_key, self._cache_affinity_headers()

    @staticmethod
    def _transfer_mode_of(request_mode: str | None) -> str | None:
        """Map a transport-qualified ``request_mode`` to a generic transfer mode.

        Both ``ws_full`` and ``rest_full`` (and the ``*_fallback`` full re-sends)
        map to ``full``; ``ws_incremental`` / ``rest_incremental`` map to
        ``incremental``. This is the transport-neutral axis surfaced in the ledger
        as ``codex_transfer_mode`` so a REST request never reads as ``ws_*``.
        """
        if not request_mode:
            return None
        if "incremental" in request_mode:
            return "incremental"
        if "full" in request_mode:
            return "full"
        return None

    # -- Runtime reasoning effort (issue #1197 K1a) --------------------------

    def reasoning_effort_capability(self) -> ReasoningEffortCapability:
        """This session's exact active effort route, or truthfully unavailable."""
        if self._effort_descriptor is None:
            return UNAVAILABLE_CAPABILITY
        return self._effort_descriptor.to_capability()

    def set_reasoning_effort_policy(
        self,
        provider: "Callable[[], ReasoningEffortSnapshot | None] | None",
    ) -> bool:
        """Bind the owner's snapshot provider; no-op off the authorized route.

        Deliberately a dedicated slot: the single ``pre_request_hook`` is owned
        by the kernel Task Card inbox drain and is neither read nor overwritten
        here.
        """
        if self._effort_descriptor is None:
            return False
        self._effort_policy_provider = provider
        return True

    def last_reasoning_effort_dispatch(self) -> dict | None:
        return dict(self._last_effort_dispatch) if self._last_effort_dispatch else None

    def _construction_effort(self) -> str | None:
        """The effort this session was CONSTRUCTED with (the aliased baseline)."""
        reasoning = self._extra_kwargs.get("reasoning")
        if isinstance(reasoning, dict):
            value = reasoning.get("effort")
            if isinstance(value, str):
                return value
        return None

    def _capture_effort_snapshot(self) -> str | None:
        """Capture exactly ONE immutable effort decision for this dispatch.

        Called once per logical dispatch, after the pre-request stage and before
        any request-kwargs / interface / account mutation. Returns the value to
        emit, or ``None`` to leave the request exactly as it is today.

        Everything downstream (REST, the WebSocket frame, the encrypted-reasoning
        self-heal retry, and the incremental fallback) reuses the returned value
        through the request kwargs, so a concurrent set/clear can only affect the
        NEXT not-yet-captured dispatch — never this one or its retries.
        """
        descriptor = self._effort_descriptor
        if descriptor is None:
            # Off the authorized route: create NO record at all. An unavailable
            # route must stay unchanged AND unexposed — it may not start
            # answering the self-facing dispatch query just because the feature
            # exists elsewhere.
            self._last_effort_dispatch = None
            return None
        construction = self._construction_effort()
        record: dict[str, Any] = {
            "route": descriptor.to_capability().route,
            "fingerprint": descriptor.fingerprint,
            "baseline": descriptor.construction_baseline,
            "requested": None,
            "effective": construction,
            "source": "construction",
            "revision": None,
            "applied": False,
            "completed": False,
        }
        provider = self._effort_policy_provider
        if provider is None:
            self._last_effort_dispatch = record
            return None
        try:
            snapshot = provider()
        except Exception:
            # A broken owner policy must never break the request; fall back to
            # the construction baseline and say so.
            logger.warning("codex.effort.policy_provider_failed", exc_info=True)
            record["rejected_reason"] = "policy_error"
            self._last_effort_dispatch = record
            return None
        if snapshot is None:
            self._last_effort_dispatch = record
            return None
        if snapshot.fingerprint != descriptor.fingerprint:
            # Route drift: the snapshot was chosen for a different route.
            record["rejected_reason"] = "route_fingerprint_mismatch"
            self._last_effort_dispatch = record
            return None
        if not descriptor.supports(snapshot.effective):
            record["rejected_reason"] = "unsupported_value"
            self._last_effort_dispatch = record
            return None
        record.update(
            effective=snapshot.effective,
            source=snapshot.source,
            revision=snapshot.revision,
            requested=snapshot.requested,
            applied=True,
        )
        self._last_effort_dispatch = record
        return snapshot.effective

    def _effort_usage_extra(self) -> dict[str, str]:
        """Safe completion-side evidence for the token ledger / event seam.

        Route-scoped: a session that is NOT on the one authorized active route
        contributes nothing, so its usage extra (and the kernel event derived
        from it) stays byte-identical to before this feature existed.
        """
        if self._effort_descriptor is None:
            return {}
        record = self._last_effort_dispatch
        if not record or not record.get("effective"):
            return {}
        extra = {
            "codex_reasoning_effort": str(record["effective"]),
            "codex_reasoning_effort_source": str(record.get("source") or "construction"),
        }
        if record.get("revision") is not None:
            extra["codex_reasoning_effort_revision"] = str(record["revision"])
        return extra

    def _usage_extra(
        self,
        affinity_headers: dict[str, str],
        cache_key: str | None,
        *,
        request_mode: str | None = None,
        transport: str | None = None,
        previous_response_id: str | None = None,
        store: bool | None = None,
        fallback_error_type: str | None = None,
        fallback_error_message: str | None = None,
        ws_diag: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Build the token-ledger ``UsageMetadata.extra`` for this request.

        Surfaces the ACTUAL current ids used so a stalled-cache rotation is
        visible in ``token_ledger.jsonl`` alongside the pre-rotation requests.
        Only the short non-secret affinity ids ride here — no prompt body, no
        tokens, no OAuth secret.

        Two orthogonal axes are recorded: ``codex_transport`` (``rest`` /
        ``websocket``) and ``codex_transfer_mode`` (``full`` / ``incremental``),
        alongside the transport-qualified ``codex_request_mode`` (e.g.
        ``rest_incremental`` / ``ws_full``) kept for back-compat.

        ``ws_diag`` carries the full/incremental decision — ONLY safe metadata
        (reason class, item counts, changed non-input KEY names, a mismatch
        index). It explains why a turn went ``full`` vs ``incremental``
        (``no_baseline`` / ``missing_response_id`` / ``missing_output_items`` /
        ``prefix_mismatch`` / ``non_input_fields_changed`` / ``ok`` …). No prompt,
        tool-result, reasoning, token, header, or secret content is included.
        """
        extra: dict[str, str] = {}
        if self._account_id:
            extra["codex_account_id_sha8"] = hashlib.sha256(
                self._account_id.encode("utf-8", "replace")
            ).hexdigest()[:8]
        if self._codex_auth_path_sha8:
            extra["codex_auth_path_sha8"] = self._codex_auth_path_sha8[:64]
        if self._codex_auth_path_source:
            extra["codex_auth_path_source"] = self._codex_auth_path_source[:64]
        pool_selection = getattr(self, "codex_pool_selection", None)
        if isinstance(pool_selection, dict):
            pool_fields = {
                "source_ref": "codex_pool_source_ref",
                "source_index": "codex_pool_source_index",
                "pool_size": "codex_pool_size",
                "weight": "codex_pool_weight",
                "auth_path_sha8": "codex_auth_path_sha8",
                "quota_left": "codex_pool_quota_left",
                "model_scope": "codex_pool_model_scope",
                "failover": "codex_pool_failover",
                "fallback": "codex_pool_fallback",
            }
            for source_key, ledger_key in pool_fields.items():
                value = pool_selection.get(source_key)
                if value is not None and ledger_key not in extra:
                    extra[ledger_key] = str(value)[:240]
        if affinity_headers.get("session_id"):
            extra["codex_session_id"] = affinity_headers["session_id"]
        if affinity_headers.get("thread_id"):
            extra["codex_thread_id"] = affinity_headers["thread_id"]
        if cache_key:
            extra["codex_prompt_cache_key"] = cache_key
        if transport:
            extra["codex_transport"] = transport
        if request_mode:
            extra["codex_request_mode"] = request_mode
            transfer_mode = CodexResponsesSession._transfer_mode_of(request_mode)
            if transfer_mode:
                extra["codex_transfer_mode"] = transfer_mode
        if previous_response_id:
            extra["codex_previous_response_id"] = previous_response_id
        if store is not None:
            extra["codex_store"] = str(bool(store)).lower()
        if fallback_error_type:
            extra["codex_fallback_error_type"] = fallback_error_type
        if fallback_error_message:
            extra["codex_fallback_error_message"] = fallback_error_message[:240]
        if ws_diag:
            reason = ws_diag.get("reason")
            if reason:
                extra["codex_ws_delta_reason"] = str(reason)
            changed = ws_diag.get("changed_fields")
            if changed:
                # Key NAMES only (e.g. "tools,include"), never their values.
                extra["codex_ws_changed_fields"] = ",".join(str(k) for k in changed)[:240]
            for key in (
                "baseline_len",
                "cur_input_len",
                "delta_len",
                "mismatch_index",
                "mismatch_prev_type",
                "mismatch_prev_role",
                "mismatch_prev_keys",
                "mismatch_prev_hash",
                "mismatch_cur_type",
                "mismatch_cur_role",
                "mismatch_cur_keys",
                "mismatch_cur_hash",
                "epoch_reset_reason",
                "epoch_reset_turns",
                "turns_since_epoch_reset",
            ):
                if ws_diag.get(key) is not None:
                    extra[f"codex_ws_{key}"] = str(ws_diag[key])[:240]
        if self._compacted_items is not None:
            extra["codex_compacted"] = "true"
            extra["codex_compacted_delta_entries"] = str(
                max(len(self._interface.entries) - self._compacted_at_entry_count, 0)
            )
        # Exact emitted effort / source / controller revision for THIS request,
        # taken from the same immutable snapshot the wire actually used.
        extra.update(self._effort_usage_extra())
        return extra

    def send(self, message) -> LLMResponse:
        # Force the streaming path — Codex doesn't serve non-streaming JSON.
        return self.send_stream(message, on_chunk=None)

    # -- Experimental Responses-over-WebSocket transport (#471) ---------------

    # Request-body keys that are NOT part of the websocket ``response.create``
    # frame's comparable shape — SDK transport-only kwargs. They are excluded
    # when building the request dict used for the delta-extension check (the
    # official algorithm compares the request struct, which never contains
    # transport headers). ``input`` is compared separately by the algorithm.
    _WS_NON_FRAME_KEYS = ("extra_headers", "extra_body", "timeout")

    def _ws_frame_request(self, kwargs: dict[str, Any], input_items: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the comparable ``response.create`` request dict from ``kwargs``.

        Drops SDK transport-only keys (headers/body/timeout) and forces
        ``store=false`` (the ChatGPT Codex backend rejects ``store=true`` —
        ``client.rs:722``). ``client_metadata`` from ``extra_body`` is folded in
        as a body field so it participates in the non-input equality check, like
        the official request struct's ``client_metadata`` (``common.rs:189``).
        """
        frame: dict[str, Any] = {
            k: v for k, v in kwargs.items() if k not in self._WS_NON_FRAME_KEYS
        }
        frame["type"] = "response.create"
        frame["store"] = False
        frame["stream"] = True
        frame["input"] = list(input_items)
        extra_body = kwargs.get("extra_body") or {}
        if isinstance(extra_body, dict) and extra_body.get("client_metadata"):
            frame["client_metadata"] = extra_body["client_metadata"]
        return frame

    def _codex_plan_continuation(
        self,
        full_request: dict[str, Any],
        full_replay_input_items: list[dict[str, Any]],
        *,
        full_mode: str,
        incremental_mode: str,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        """Decide ``full`` vs ``incremental`` for THIS turn (transport-neutral).

        This is the shared full->incremental planner used by BOTH the REST and the
        WebSocket transports. It mirrors the official
        ``prepare_websocket_request`` (``client.rs:998-1024``): when the new full
        request is a strict additive extension of the previous request + its
        server-added output items, only the suffix (delta) is sent together with
        ``previous_response_id``; otherwise the full input is sent with no previous
        id (first request / prefix mismatch / epoch reset).

        Returns ``(request_mode, transmit_input_items, previous_response_id)``.
        ``request_mode`` is the transport-qualified label (e.g. ``rest_full`` /
        ``ws_incremental``) built from ``full_mode`` / ``incremental_mode`` so
        metadata never says ``ws_*`` on a REST request. The safe decision
        diagnostic is stored on ``self._ws_last_diag`` for the token ledger (only
        classes / counts / key-names / a short hash — never prompt/secret content).
        """
        previous_response_id: str | None = None
        transmit_input = list(full_replay_input_items)
        request_mode = full_mode
        diag: dict[str, Any]
        epoch_reset_reason = self._ws_epoch_reset_reason_pending
        self._ws_epoch_reset_reason_pending = None
        last = self._ws_session.last_response
        if last is None:
            diag = {"reason": "no_baseline"}
        elif not last.response_id:
            diag = {"reason": "missing_response_id"}
        elif self._ws_session.last_request is None:
            diag = {"reason": "missing_baseline_request"}
        else:
            delta, diag = _codex_incremental_diagnose(
                self._ws_session.last_request,
                last.items_added,
                full_request,
                allow_empty_delta=True,
            )
            if delta is None and not last.items_added:
                # Baseline lacks the server's output items (e.g. the previous turn
                # completed but produced no recordable output item). Make the
                # reason explicit rather than a generic prefix mismatch.
                if diag.get("reason") == "prefix_mismatch" and diag.get("baseline_len") == len(
                    self._ws_session.last_request.get("input") or []
                ):
                    diag = {**diag, "reason": "missing_output_items"}
            if delta is not None:
                previous_response_id = last.response_id
                transmit_input = list(delta)
                request_mode = incremental_mode
        if epoch_reset_reason:
            diag = {
                "reason": "epoch_reset",
                "epoch_reset_reason": epoch_reset_reason,
                "epoch_reset_turns": self._ws_epoch_reset_turn_limit,
                "turns_since_epoch_reset": self._ws_turns_since_epoch_reset,
                "cur_input_len": len(full_replay_input_items),
            }
        self._ws_last_diag = dict(diag)
        return request_mode, transmit_input, previous_response_id

    def _codex_ws_open(
        self,
        kwargs: dict[str, Any],
        *,
        full_replay_input_items: list[dict[str, Any]],
    ) -> tuple[Any, str, str | None]:
        """Open a websocket ``response.create`` stream, or raise ``_CodexWsFallback``.

        Returns ``(event_iterable, request_mode, previous_response_id_or_None)``.

        Mirrors the official path: build the full request, run the shared
        full->incremental planner (``_codex_plan_continuation``) to decide whether
        to send only the delta input with ``previous_response_id`` (when the new
        full request is a strict extension of the previous request + its output
        items) or the full input with no previous id (first request / mismatch).
        The connection captures ``x-codex-turn-state`` from the handshake and
        replays it within the turn; ``response.processed`` is sent after the
        completed response (``responses_websocket.rs:208-240``).
        """
        # The full request shape (full input) is what we record as
        # ``last_request`` for the NEXT turn's delta baseline, regardless of what
        # we actually transmit this turn.
        full_request = self._ws_frame_request(kwargs, full_replay_input_items)

        request_mode, frame_input, previous_response_id = self._codex_plan_continuation(
            full_request,
            full_replay_input_items,
            full_mode="ws_full",
            incremental_mode="ws_incremental",
        )

        # Build the transmitted frame (delta or full).
        frame = dict(full_request)
        frame["input"] = list(frame_input)
        if previous_response_id:
            frame["previous_response_id"] = previous_response_id

        # Handshake headers: the per-request Codex headers plus the websocket
        # beta header, plus the captured per-turn ``x-codex-turn-state`` (only
        # after it has been captured earlier in the same turn).
        headers = dict(kwargs.get("extra_headers") or {})
        if self._ws_api_key and not any(k.lower() == "authorization" for k in headers):
            bearer = self._ws_api_key.strip()
            if not bearer.lower().startswith("bearer "):
                bearer = f"Bearer {bearer}"
            headers["Authorization"] = bearer
        headers[_CODEX_WS_BETA_HEADER] = _CODEX_WS_BETA_VALUE
        if self._ws_session.turn_state:
            headers[_CODEX_TURN_STATE_HEADER] = self._ws_session.turn_state

        transport = self._ws_transport
        if transport is None:
            factory = self._ws_transport_factory or _default_codex_ws_transport_factory
            url = _codex_ws_url(self._ws_base_url)
            transport = factory(url, headers)

            # connect() may raise _CodexWsFallback (426/connect/auth); let it
            # propagate to the caller which falls back to HTTP.
            captured_turn_state = transport.connect(headers=headers)
            if captured_turn_state and not self._ws_session.turn_state:
                # Capture once per turn. With a persistent websocket connection
                # there may be no second handshake to replay this header on, but
                # keep it for reconnects within the same turn.
                self._ws_session.turn_state = captured_turn_state
            self._ws_transport = transport

        # Record the FULL request as the next delta baseline before streaming.
        # If streaming fails before a completed response, restore the previous
        # baseline so a later retry does not compare against an unaccepted turn.
        previous_last_request = self._ws_session.last_request
        previous_last_response = self._ws_session.last_response
        self._ws_session.last_request = full_request
        # Park the baseline for THIS request's input length so the post-stream
        # baseline (recomputed from the canonical interface in ``send_stream``)
        # knows where the server-added output items begin. The previous-baseline
        # values above are restored on any stream failure.
        self._ws_pending_baseline_input = list(full_replay_input_items)

        def _events():
            response_id_local: str | None = None
            try:
                for event in transport.stream(frame):
                    etype = getattr(event, "type", None)
                    if etype == "response.completed":
                        response_id_local = getattr(getattr(event, "response", None), "id", None)
                    yield event
            except _CodexWsFallback:
                self._ws_session.last_request = previous_last_request
                self._ws_session.last_response = previous_last_response
                self._ws_pending_baseline_input = None
                self._close_ws_transport(transport)
                raise
            except Exception:
                self._ws_session.last_request = previous_last_request
                self._ws_session.last_response = previous_last_response
                self._ws_pending_baseline_input = None
                self._close_ws_transport(transport)
                raise
            # Record the completed response so the next request can delta off it,
            # then notify the server it was processed (official posts
            # ``response.processed`` after handling a completed response).
            #
            # NOTE: ``items_added`` is intentionally left EMPTY here. The server's
            # streamed ``response.output_item.done`` items are in the Responses
            # *output* schema (``{"type":"message","id":...,"status":...,
            # "content":[{"type":"output_text",...}]}``), which does NOT compare
            # equal to the *input* schema this session re-derives next turn via
            # ``to_responses_input`` (``{"role":"assistant","content":<str>}`` +
            # ``{"type":"reasoning","summary":[...]}``). Using the raw output items
            # as the delta baseline therefore failed the strict-prefix check on
            # EVERY follow-up turn, collapsing every real agent turn to ``ws_full``.
            # The correct, converter-stable baseline is filled in by
            # ``_ws_record_baseline_from_interface`` after ``send_stream`` records
            # the assistant turn into the canonical interface (#471 delta fix).
            if response_id_local:
                self._ws_session.last_response = _CodexLastResponse(
                    response_id=response_id_local,
                    items_added=[],
                )
                try:
                    transport.send_response_processed(response_id_local)
                except Exception as exc:  # pragma: no cover - best-effort ack
                    logger.debug("codex ws response.processed failed: %s", exc)

        return _events(), request_mode, previous_response_id

    def _ws_record_baseline_from_interface(self) -> None:
        """Fill the WS delta baseline from the converter, not raw server output.

        Called by ``send_stream`` AFTER the just-completed assistant turn has been
        recorded into the canonical interface. The next turn's full input is
        ``to_responses_input(interface)`` (plus that turn's new user/tool items),
        so the only baseline that can ever strict-prefix-match it is one expressed
        in the SAME converter schema. We therefore derive ``items_added`` as the
        suffix of the current full converted input beyond the input we actually
        sent this turn — i.e. exactly the assistant turn the server added,
        rendered in input schema. This is the conservative fix for the
        ``ws_full``-every-turn root cause: it makes the baseline and the next full
        request byte-comparable by construction. No prompt/secret content leaves
        the process; this only rearranges in-memory request dicts.
        """
        pending = getattr(self, "_ws_pending_baseline_input", None)
        last = self._ws_session.last_response
        if pending is None or last is None or not last.response_id:
            self._ws_pending_baseline_input = None
            return
        # Compare and record the baseline with EVERY synthesized orphan
        # ``function_call_output`` placeholder removed — not just trailing ones.
        # The placeholder is a wire-only stand-in for an unanswered call; its
        # position drifts relative to the real output next turn (an assistant turn
        # that emits several calls resolving incrementally), which previously broke
        # the strict prefix and forced ``*_full`` (the observed ``prefix_mismatch``
        # ``function_call_output`` vs ``function_call``). Stripping placeholders
        # from both sides leaves only the real, position-stable items, so the real
        # tool-result continuation strictly extends the baseline and stays
        # ``*_incremental``. See ``_strip_synthesized_orphan_outputs``.
        pending_real = _strip_synthesized_orphan_outputs(pending)
        full_now = _strip_synthesized_orphan_outputs(
            self._frozen_responses_input(self._interface)
        )
        # Park the placeholder-stripped baseline as ``last_request.input`` so the
        # next turn's ``_codex_incremental_diagnose`` prefix check compares the
        # stable, placeholder-free shape against the next full converted input.
        if isinstance(last_request := self._ws_session.last_request, dict):
            last_request["input"] = list(pending_real)
        base_len = len(pending_real)
        # Only treat the tail as server-added output when the interface still
        # strictly extends what we sent (it always should: we appended an
        # assistant turn). If it does not, leave ``items_added`` empty so the next
        # turn falls back to ``*_full`` with a ``missing_output_items`` reason
        # rather than chaining off a baseline we cannot prove.
        if full_now[:base_len] == pending_real and base_len <= len(full_now):
            last.items_added = full_now[base_len:]
        else:
            last.items_added = []
        self._ws_pending_baseline_input = None

    def _close_ws_transport(self, transport=None) -> None:
        current = self._ws_transport
        if transport is not None and current is not transport:
            return
        self._ws_transport = None
        if current is not None:
            try:
                current.close()
            except Exception:
                pass


    def _refresh_ws_epoch_reset_turn_limit(self) -> None:
        if getattr(self, "_ws_epoch_reset_turns_explicit", False):
            return
        self._ws_epoch_reset_turn_limit = _codex_ws_epoch_reset_turns()

    def _reset_ws_epoch(self, reason: str) -> None:
        """Start a fresh websocket response-id epoch.

        The Codex backend cannot delete already-accepted ``previous_response_id``
        state. Forcing a full request from the current local history, while
        clearing the frozen tool-output map and old response id, rebases the remote
        chain on the rewritten local history. This is triggered by explicit
        context/history-rewrite actions (``summarize``) — and, only if an operator
        opts in via ``ws_epoch_reset_turns``/the env var, by ``turn_count``. The
        runtime no longer schedules a periodic turn-count reset by default.
        """

        # Account selection follows the product context epoch, not every
        # transport/continuation reset. Only an applied summarize/reconstruction
        # starts a new account epoch here; technical resets below deliberately do
        # not redraw the account.
        approved_account_epoch_reset = reason in {
            "summarize_delayed",
            "summarize_rebuild_only",
        }
        if approved_account_epoch_reset:
            self._codex_account_epoch_boundary_pending = True
            callback = self._codex_account_epoch_reset_callback
            if callback is not None:
                callback(reason)

        # Capture the before-context (A) for the reconstruction event BEFORE the
        # epoch state is torn down, but only for an actual delayed-summarize
        # reconstruction (not a turn_count reset, which carries no summarized
        # history to apply). This is the one moment the provider context is
        # genuinely rebuilt around the compacted history.
        if reason in {"summarize_delayed", "summarize_rebuild_only"}:
            self._record_reconstruction_event(reason=reason)
            # The compacted history is now being applied to provider context, so
            # flip every still-pending summarize marker to done. The manual
            # rebuild=true path already marked its batch done in the intrinsic
            # (this is idempotent); the 1.0 hard forced rebuild has no other place
            # to do it, so this is that hook. When no pending markers exist the flip
            # is a no-op — the forced rebuild still rerenders the provider replay
            # through the shared converter, which preserves every historical
            # timely transient ``_meta`` copy. Kept tiny and defensive — never
            # let bookkeeping abort the rebuild.
            try:
                from lingtai.tools.system.summarize import (
                    mark_pending_summaries_done,
                )

                mark_pending_summaries_done(self._interface)
            except Exception:
                pass

        # Invalidate any active standalone compaction: every reset reason here
        # (turn_count / summarize_delayed / summarize_rebuild_only /
        # encrypted_reasoning_self_heal) means local history was rewritten or
        # the remote epoch chain was rebased, so a previously compacted prefix
        # can no longer be trusted as a valid replay basis. Fail safe by
        # rebuilding from the full canonical interface (existing full-replay
        # behavior) rather than silently keeping a stale compacted prefix;
        # compaction re-triggers fresh once the threshold is reached again.
        self._compacted_items = None
        self._compacted_at_entry_count = 0

        self._close_ws_transport()
        self._ws_session = _CodexWebsocketSession()
        self._ws_pending_baseline_input = None
        # Clearing the freeze map is what makes the fresh replay effective: the
        # next full request re-freezes from the shared converter's serialization
        # (``to_responses_input``), which applies deliberate canonical rewrites
        # (e.g. summarize marker/status flips) and does not strip any
        # historical holder's timely transient ``_meta`` keys.
        self._ws_frozen_outputs.clear()
        self._ws_turns_since_epoch_reset = 0
        self._ws_epoch_reset_reason_pending = reason
        # Record the release reason for the summarize/rebuild reasons even when no
        # ids were pending (a 1.0 forced rebuild runs regardless of pending); clear
        # any pending set that WAS present.
        if reason in {"summarize_delayed", "summarize_rebuild_only"}:
            self._summarize_effect_delayed_last_release_reason = reason
        if self._summarize_effect_delayed_pending_ids:
            self._summarize_effect_delayed_pending_ids.clear()

    def _record_reconstruction_event(self, *, reason: str = "summarize_delayed") -> None:
        """Record the one-shot summarize/rebuild reconstruction event (channel A).

        Captures the before-context (A) from the last real provider request and
        the fixed trigger/recovery metadata. The after-context (B) and any molt
        warning are filled by the kernel when it attaches the event to the next
        tool result's ``_meta.tool_meta``. If a prior event is still pending
        (rare back-to-back reconstructions before a tool result was emitted), the
        newer reconstruction supersedes it — the latest A→ is the relevant one.
        """
        ctx = self._summarize_delay_context()
        before_tokens = ctx.get("current_context_tokens")
        before_usage = ctx.get("current_context_usage")
        context_window = ctx.get("context_window")
        event_type = (
            "summarize_rebuild_only_reconstruction"
            if reason == "summarize_rebuild_only"
            else "delayed_summarize_reconstruction"
        )
        self._pending_reconstruction_event = {
            "type": event_type,
            "reason": event_type,
            "trigger_threshold": self._forced_rebuild_threshold_ratio,
            "recovery_target": CONTEXT_PRESSURE_RECOVERY_TARGET,
            "context_window": context_window,
            "before": {
                "context_tokens": before_tokens,
                "usage": before_usage,
            },
        }

    def take_pending_reconstruction_event(self) -> dict | None:
        """Pop the one-shot reconstruction event (channel A). See base class."""
        event = self._pending_reconstruction_event
        self._pending_reconstruction_event = None
        return event

    def _summarize_delay_context(self) -> dict[str, Any]:
        current_tokens: int | None = None
        context_window: int | None = None
        usage: float | None = None
        try:
            current_tokens = int(self._last_provider_input_tokens) if self._last_provider_input_tokens else None
        except Exception:
            current_tokens = None
        try:
            context_window = int(self.context_window())
        except Exception:
            context_window = None
        if current_tokens is not None and context_window and context_window > 0:
            usage = current_tokens / context_window
        return {
            "current_context_tokens": current_tokens,
            "context_window": context_window,
            "current_context_source": "last_provider_input_tokens" if current_tokens is not None else None,
            "threshold_ratio": self._forced_rebuild_threshold_ratio,
            "threshold_context_tokens": (
                int(context_window * self._forced_rebuild_threshold_ratio)
                if context_window and context_window > 0
                else None
            ),
            "current_context_usage": usage,
        }

    # -- Standalone Codex compaction (daemon task ``context_token_limit``) ---
    #
    # Separate axis from the hard-boundary forced-rebuild machinery above:
    # forced rebuild discards local pressure by re-sending the FULL canonical
    # history (no server-side help); standalone compaction asks Codex itself
    # (``POST /responses/compact``) to fold prior context into an opaque
    # ``compaction_summary`` + trailing ``message`` pair, which is then
    # replayed as the new provider-context prefix, with only strict-additive
    # entries appended on top — never ``context_management`` (Codex rejects
    # it; see ``_create_responses_session``). The projected-token trigger,
    # boundary selection, and opaque replay basis are shared with any other
    # standalone-compaction session via ``_StandaloneCompactionMixin``; only
    # the wire-shaping (``_compaction_prefix_input``, Codex's per-session
    # tool-result output freezing) and the failure policy (``_compact_now``,
    # non-fatal for Codex) are provider-specific.

    def _compaction_prefix_input(self, entries: list[Any]) -> list[dict[str, Any]]:
        """Codex routes compaction wire-shaping through the WS output-freeze map
        (``_frozen_responses_input`` / ``_interface_entries_to_responses_input``)
        so a compact request's items stay byte-identical to what the ordinary
        strict-additive delta would have sent for the same entries."""
        if not entries:
            return []
        prefix_interface = ChatInterface()
        prefix_interface.entries.extend(entries)
        return self._frozen_responses_input(prefix_interface)

    def _compact_now(self) -> None:
        """Call standalone Codex compaction and store the opaque replay basis.

        Builds the request via ``_prepare_compact_request`` (the shared
        turn-aware boundary/re-arm logic — see
        ``_StandaloneCompactionMixin._prepare_compact_request``). On any
        failure (network, malformed output, no safe boundary yet), compaction
        is skipped for this turn (falls back to the existing full-replay/
        current compacted-replay behavior) rather than raising — compaction is
        an optimization for Codex, not a correctness requirement, and a
        transient failure must not block the turn. Never logs or persists
        ``encrypted_content``/opaque summary text; only structural item
        types/counts are safe to record.
        """
        prepared = self._prepare_compact_request()
        if prepared is None:
            return
        boundary_index, full_input = prepared
        already_compacted = self._compacted_items is not None
        try:
            compacted = self._client.responses.compact(
                model=self._model,
                input=full_input,
                instructions=self._instructions,
                # No ``store`` kwarg: unlike ``responses.create``, the standalone
                # ``responses.compact`` SDK method is keyword-only with no
                # ``store`` parameter at all (it has no wire equivalent) —
                # passing it raises ``TypeError`` before the request is even
                # built, silently defeating every compaction attempt.
                prompt_cache_key=self._prompt_cache_key,
                extra_headers={
                    **_codex_identity_headers(),
                    **self._cache_affinity_headers(),
                },
            )
        except Exception as exc:
            logger.info(
                "codex.compact_failed",
                extra={
                    "error_type": type(exc).__name__,
                    "boundary_index": boundary_index,
                    "rearm": already_compacted,
                },
            )
            return
        output_items = list(getattr(compacted, "output", None) or [])
        if not output_items:
            return
        normalized: list[dict[str, Any]] = []
        for item in output_items:
            if hasattr(item, "model_dump"):
                normalized.append(item.model_dump(mode="json", exclude_none=True))
            elif isinstance(item, dict):
                normalized.append(dict(item))
        if not normalized:
            return
        self._compacted_items = normalized
        self._compacted_at_entry_count = boundary_index
        logger.info(
            "codex.compact_applied",
            extra={
                "output_item_types": [item.get("type") for item in normalized],
                "boundary_index": boundary_index,
                "total_entries": len(self._interface.entries),
                "rearm": already_compacted,
            },
        )

    def _fire_boundary_forced_rebuild(self) -> bool:
        """Fire the once-per-episode automatic forced rebuild, honoring the latch.

        Shared by the pre-request boundary path (``_maybe_force_rebuild_at_boundary``)
        and the immediate ``on_history_summarized`` release so the two cannot
        double-fire within one continuous ``>= 1.0`` episode. Returns True iff this
        call performed the rebuild; False if the one-shot has already fired for the
        current episode. On fire it latches the one-shot, marks verification pending
        (the persistent overflow warning stays withheld until the first post-rebuild
        provider response is observed), and rebases the epoch via
        ``_reset_ws_epoch("summarize_delayed")`` — which also records the one-shot
        channel-A reconstruction event and applies any pending summaries.
        """
        if self._hb_rebuild_fired:
            return False
        self._hb_rebuild_fired = True
        self._hb_rebuild_awaiting_verify = True
        self._reset_ws_epoch("summarize_delayed")
        return True

    def _observe_provider_usage_for_boundary(self) -> None:
        """Update the one-shot latch from the latest successful provider usage.

        Called once per successful provider response (right after
        ``_last_provider_input_tokens`` is set). Two transitions:

          * provider usage STRICTLY below 1.0 -> the continuous ``>= 1.0`` episode
            is over: re-arm the one-shot (and drop any pending verification) so a
            future crossing forces exactly once again;
          * provider usage ``>= 1.0`` while a forced rebuild is awaiting its first
            post-rebuild provider response -> THIS response is that verification
            boundary; clear the pending flag so ``context_overflow_status`` can
            surface the warning when usage is strictly ``> 1.0``.

        A failed forced request never reaches here (no usage is recorded on the
        error path), so verification correctly stays pending until a successful
        provider-usage result exists. Unknown usage (no provider input yet) is a
        no-op — it neither re-arms nor verifies.
        """
        usage = self._summarize_delay_context().get("current_context_usage")
        if usage is None:
            return
        if usage < self._forced_rebuild_threshold_ratio:
            self._hb_rebuild_fired = False
            self._hb_rebuild_awaiting_verify = False
            return
        if self._hb_rebuild_awaiting_verify:
            self._hb_rebuild_awaiting_verify = False

    def context_overflow_status(self) -> dict | None:
        """Persistent hard-boundary overflow status. See ``ChatSession`` base.

        Returns ``{"usage": <float>}`` only when the automatic one-shot forced
        rebuild has fired for the current episode, its first post-rebuild provider
        response has been observed (verification complete), and the current
        provider-reported usage remains STRICTLY above 1.0 — the forced rebuild
        failed to clear the overflow. Returns ``None`` otherwise: not fired,
        verification still pending, or usage at/below 1.0 (exactly 1.0 carries no
        overflow warning; the warning is for strictly ``> 1.0``). Pure, idempotent
        read (``build_meta`` calls it repeatedly within a batch), keyed to the same
        provider-input ruler as the forced rebuild itself.
        """
        if not self._hb_rebuild_fired or self._hb_rebuild_awaiting_verify:
            return None
        usage = self._summarize_delay_context().get("current_context_usage")
        if usage is None or usage <= self._forced_rebuild_threshold_ratio:
            return None
        return {"usage": usage}

    def _maybe_force_rebuild_at_boundary(self) -> bool:
        # HARD 1.0 boundary: once context usage reaches the forced-rebuild ratio,
        # force a fresh provider-context replay REGARDLESS of whether pending
        # summaries exist. If pending markers exist they are applied and marked
        # done (via _reset_ws_epoch's marking hook); if none exist the rebuild
        # still runs so the fresh replay re-serializes through the shared
        # converter, which does not strip any historical holder's timely
        # transient _meta keys (agent_meta, guidance, notifications,
        # notification_guidance) and picks up other deliberate canonical
        # rewrites. This is why the pending-set guard that used to gate the
        # pre-1.0 delayed release is gone.
        #
        # ONE-SHOT (Jason, 2026-07-12): the automatic forced rebuild fires at most
        # once per continuous >= 1.0 episode. Delegating to
        # _fire_boundary_forced_rebuild honors the shared latch, so a request that
        # is still overflowed after the rebuild does NOT re-force. We still return
        # True whenever the boundary is reached (fired now OR already fired this
        # episode) so _maybe_reset_ws_epoch does not additionally schedule a
        # turn_count reset at the boundary.
        ctx = self._summarize_delay_context()
        self._summarize_effect_delayed_last_context = ctx
        usage = ctx.get("current_context_usage")
        if usage is None or usage < self._forced_rebuild_threshold_ratio:
            return False
        self._fire_boundary_forced_rebuild()
        return True

    def _maybe_reset_ws_epoch(self) -> None:
        self._refresh_ws_epoch_reset_turn_limit()
        if self._maybe_force_rebuild_at_boundary():
            return
        limit = self._ws_epoch_reset_turn_limit
        if limit <= 0:
            return
        if self._ws_turns_since_epoch_reset < limit:
            return
        self._reset_ws_epoch("turn_count")

    def on_history_summarized(self, summarized_ids: list[str]) -> None:
        # Runs for BOTH transports. Summarize rewrites local visible history, but
        # Codex continuation keeps the remote previous_response_id epoch alive
        # until local context pressure justifies a full replay. The summarized
        # local history becomes model-facing when the delayed reset fires.
        if not summarized_ids or not self._continuation_enabled:
            return
        self._summarize_effect_delayed_pending_ids.update(str(s) for s in summarized_ids)
        self._summarize_effect_delayed_last_context = self._summarize_delay_context()
        self._summarize_effect_delayed_last_release_reason = None
        # If the last real provider request is already at/above the 1.0 hard
        # boundary, schedule the forced fresh replay immediately rather than waiting
        # for the next _maybe_reset_ws_epoch. The replay itself happens on the next
        # provider request because summarize runs as a tool result after the current
        # model call has already completed. Below 1.0 the summary stays pending; the
        # agent applies it via a manual rebuild=true or waits for the 1.0 boundary.
        #
        # This shares the ONE-SHOT latch with the pre-request boundary path via
        # _fire_boundary_forced_rebuild: if the automatic forced rebuild already
        # fired for the current >= 1.0 episode, this immediate release is a no-op
        # (the just-added summarized ids stay pending until the next actual rebuild)
        # so the runtime never force-rebuilds twice while usage stays >= 1.0.
        usage = self._summarize_effect_delayed_last_context.get("current_context_usage")
        if usage is not None and usage >= self._forced_rebuild_threshold_ratio:
            self._fire_boundary_forced_rebuild()

    def request_history_rebuild(self, reason: str = "summarize_rebuild_only") -> bool:
        # Explicit summarize rebuild=true: any history compression already ran in the
        # summarize intrinsic before this hook, so no compression happens here — the
        # agent asked to discard the remote continuation prefix and replay the
        # current local history on the next model request. This is the manual path
        # advertised at 85%; the hard forced rebuild remains at the 1.0 boundary. The
        # ``reason`` default keeps the internal ``summarize_rebuild_only`` epoch label.
        if not self._continuation_enabled:
            return False
        self._summarize_effect_delayed_last_context = self._summarize_delay_context()
        self._summarize_effect_delayed_last_release_reason = reason
        self._reset_ws_epoch(reason)
        return True

    def on_notification_dismissed(self, channel: str | None = None) -> None:
        # Notification dismiss is high-frequency housekeeping, not context
        # compaction. It should not break the Codex previous_response_id
        # chain; only summarize rewrites old tool-result payloads enough to
        # require a fresh ws_full epoch.
        return None

    @staticmethod
    def _ws_cache_rate(input_tokens: int, cached_tokens: int) -> float | None:
        if input_tokens <= 0:
            return None
        return round(cached_tokens / input_tokens, 2)

    @staticmethod
    def _ws_tokens_k(tokens: int) -> float:
        return round(max(0, int(tokens or 0)) / 1000, 1)

    @staticmethod
    def _ws_request_mode_code(request_mode: str | None) -> str:
        # Transport-neutral one-letter code: any *_incremental -> "I", any *_full
        # (incl. *_full_fallback) -> "F". Keeps the ledger column identical across
        # REST and WebSocket so cache-rate rows stay comparable.
        if request_mode:
            if "incremental" in request_mode:
                return "I"
            if "full" in request_mode:
                return "F"
        return str(request_mode or "unknown")[:12]

    @staticmethod
    def _ws_reason_code(ws_diag: dict[str, Any] | None) -> str:
        if not isinstance(ws_diag, dict):
            return ""
        reason = str(ws_diag.get("reason") or "")
        if not reason or reason == "ok":
            return ""
        if reason == "epoch_reset":
            reset_reason = str(ws_diag.get("epoch_reset_reason") or "")
            reset_codes = {
                "summarize": "sum",
                "summarize_delayed": "sumd",
                "summarize_rebuild_only": "sumr",
                "turn_count": "turns",
            }
            if reset_reason in reset_codes:
                return reset_codes[reset_reason]
            return f"epoch:{reset_reason}" if reset_reason else "epoch"
        reason_codes = {
            "prefix_mismatch": "pm",
            "no_baseline": "nb",
            "missing_response_id": "no_prev",
            "missing_baseline_request": "no_base",
            "missing_output_items": "no_out",
        }
        return reason_codes.get(reason, reason[:24])

    def _record_ws_cache_ledger(
        self,
        *,
        request_mode: str | None,
        usage: UsageMetadata,
        ws_diag: dict[str, Any] | None,
    ) -> None:
        # Record any continuation request (either transport). The *_fallback full
        # re-sends are also continuation turns and belong in the cache ledger.
        if not (request_mode and ("full" in request_mode or "incremental" in request_mode)):
            return
        input_tokens = max(0, int(getattr(usage, "input_tokens", 0) or 0))
        cached_tokens = max(0, int(getattr(usage, "cached_tokens", 0) or 0))
        cached_tokens = min(cached_tokens, input_tokens)
        miss_tokens = max(0, input_tokens - cached_tokens)
        mode = self._ws_request_mode_code(request_mode)
        self._ws_cache_call_seq += 1
        self._ws_cache_ledger.append(
            {
                "seq": self._ws_cache_call_seq,
                "mode": mode,
                "cache": self._ws_cache_rate(input_tokens, cached_tokens),
                "input_tokens": input_tokens,
                "cached_tokens": cached_tokens,
                "miss_tokens": miss_tokens,
                "reason": self._ws_reason_code(ws_diag) if mode == "F" else "",
            }
        )

    def _ws_cache_ledger_comment(self) -> dict[str, Any]:
        entries = list(self._ws_cache_ledger)
        latest_seq = int(entries[-1]["seq"]) if entries else 0
        rows = [
            [
                latest_seq - int(entry["seq"]),
                entry["mode"],
                entry["cache"],
                self._ws_tokens_k(int(entry["input_tokens"])),
                self._ws_tokens_k(int(entry["miss_tokens"])),
                entry["reason"],
            ]
            for entry in entries
        ]
        total_input = sum(int(entry["input_tokens"]) for entry in entries)
        total_cached = sum(int(entry["cached_tokens"]) for entry in entries)
        total_miss = sum(int(entry["miss_tokens"]) for entry in entries)
        last_ws_full = next(
            (entry for entry in reversed(entries) if entry["mode"] == "F"),
            None,
        )
        if last_ws_full is None:
            last_ws_full_comment = {
                "api_calls_ago": None,
                "reason": "not_seen" if entries else "not_recorded",
            }
        else:
            last_ws_full_comment = {
                "api_calls_ago": latest_seq - int(last_ws_full["seq"]),
                "reason": last_ws_full["reason"] or "full",
            }
        full_count = sum(1 for entry in entries if entry["mode"] == "F")
        incremental_count = sum(1 for entry in entries if entry["mode"] == "I")
        return {
            "window_api_calls": 20,
            "recorded_api_calls": len(entries),
            "cols": ["ago", "mode", "cache", "in_k", "miss_k", "reason"],
            "rows": rows,
            "summary": {
                "api_calls": len(entries),
                "cache_rate": self._ws_cache_rate(total_input, total_cached),
                "full_count": full_count,
                "incremental_count": incremental_count,
                "full_to_incremental_ratio": self._ws_full_to_incremental_ratio(
                    full_count, incremental_count
                ),
                # Compatibility alias for older prompt/diagnostic consumers.
                "ws_full_count": full_count,
                "miss_k": self._ws_tokens_k(total_miss),
            },
            "last_full": last_ws_full_comment,
            # Compatibility alias for older prompt/diagnostic consumers.
            "last_ws_full": last_ws_full_comment,
            "legend": {
                "I": "incremental",
                "F": "full",
                "sum": "epoch_reset:summarize",
                "sumd": "epoch_reset:summarize_delayed",
                "sumr": "epoch_reset:summarize_rebuild_only",
                "turns": "epoch_reset:turn_count",
                "pm": "prefix_mismatch",
                "nb": "no_baseline",
            },
        }

    @staticmethod
    def _ws_full_to_incremental_ratio(
        full_count: int, incremental_count: int
    ) -> str:
        """Render the full:incremental count as a ``"1:N"``-style ratio string.

        Normalized so the full side is ``1`` whenever any full was recorded
        (``"1:10"``); ``"0:N"`` when no full has been seen yet, and ``"1:0"`` when
        only fulls were recorded.
        """
        if full_count <= 0:
            return f"0:{int(incremental_count)}"
        per_full = incremental_count / full_count
        return f"1:{per_full:.1f}".replace(".0", "")

    @staticmethod
    def _ws_maintenance_hint(
        *,
        full_count: int,
        incremental_count: int,
    ) -> dict[str, Any]:
        """Ratio-oriented summarize-economy hint for the dynamic adapter comment.

        Replaces the older interval/countdown guidance ("wait N API calls after
        the last full epoch"), which was misleading: summarize is an investment,
        not a fixed cooldown. Each summarize spends a fresh ``full`` epoch (a
        cache miss) now to buy future context/token savings, so it only pays off
        when those savings exceed the miss. A healthy continuation keeps fulls
        rare relative to incremental turns: the target is a full:incremental
        ratio at or below ``1:10`` (≈ at most one full per ten incremental
        turns). When fulls are too frequent the actionable fix is to summarize
        less often / batch more so each full epoch earns its cost.
        """
        target_ratio = "1:10"
        # full:incremental <= 1:10  <=>  full_count <= incremental_count / 10.
        target_max_full_per_incremental = 0.1
        if full_count <= 0:
            return {
                "summarize_economy": "ok" if incremental_count > 0 else "unknown",
                "full_count": int(full_count),
                "incremental_count": int(incremental_count),
                "full_to_incremental_ratio": (
                    CodexResponsesSession._ws_full_to_incremental_ratio(
                        full_count, incremental_count
                    )
                ),
                "target_full_to_incremental_ratio": target_ratio,
                "reason": (
                    "no full epoch in the last 20 Codex API calls; summarize "
                    "frequency is healthy"
                    if incremental_count > 0
                    else "no Codex continuation cache ledger entries yet"
                ),
            }
        full_per_incremental = full_count / max(incremental_count, 1)
        ratio_ok = (
            incremental_count > 0
            and full_per_incremental <= target_max_full_per_incremental
        )
        hint = {
            "summarize_economy": "ok" if ratio_ok else "reduce_summarize_frequency",
            "full_count": int(full_count),
            "incremental_count": int(incremental_count),
            "full_to_incremental_ratio": (
                CodexResponsesSession._ws_full_to_incremental_ratio(
                    full_count, incremental_count
                )
            ),
            "target_full_to_incremental_ratio": target_ratio,
        }
        if ratio_ok:
            hint["reason"] = (
                f"full:incremental is {hint['full_to_incremental_ratio']} over the "
                f"last 20 Codex API calls, within the {target_ratio} target; "
                "summarize frequency is healthy"
            )
        else:
            hint["reason"] = (
                f"full:incremental is {hint['full_to_incremental_ratio']} over the "
                f"last 20 Codex API calls, worse than the {target_ratio} target; "
                "each full epoch is a cache miss, and summarize is an investment "
                "that must buy back more than it spends — reduce summarize "
                "frequency (defer/batch non-urgent summarize until the expected "
                "savings justify the miss, or molt if context pressure stays high)"
            )
        return hint

    def static_adapter_comment(self):
        """Codex adapter comments are intentionally disabled.

        Generic summarize/reconstruction guidance lives in substrate/procedures
        and in the ``system.summarize`` tool result; Codex should not inject a
        separate agent-facing policy block.
        """
        return None

    def dynamic_adapter_comment(self):
        """Codex exposes no adapter-specific tail guidance.

        Runtime context/summarize guidance is generic and should come from
        resident substrate/procedures plus ordinary tool results.
        """
        return None

    def adapter_comment(self):
        """Legacy compatibility hook; Codex adapter comments are disabled."""
        return None

    def _strip_codex_encrypted_reasoning_items(self) -> int:
        """Remove replay-only encrypted reasoning blobs from canonical history.

        Codex encrypted reasoning items are opaque provider state.  When the
        backend says one cannot be verified, replaying the same blobs will fail
        forever (AED loops).  Keep the assistant transcript/tool calls intact and
        preserve any summary text, but drop the raw encrypted replay anchors so
        ``to_responses_input`` emits summary_text-only reasoning or omits empty
        thought blocks.
        """

        stripped = 0
        for entry in self._interface.entries:
            if entry.role != "assistant":
                continue
            for block in entry.content:
                if not isinstance(block, ThinkingBlock):
                    continue
                provider_data = block.provider_data
                if not isinstance(provider_data, dict):
                    continue
                raw_item = provider_data.get("openai_responses_reasoning_item")
                encrypted_content = (
                    raw_item.get("encrypted_content")
                    if isinstance(raw_item, dict)
                    else None
                )
                if not (
                    isinstance(raw_item, dict)
                    and raw_item.get("type") == "reasoning"
                    and isinstance(encrypted_content, str)
                    and encrypted_content
                    and encrypted_content != "<REDACTED:secret>"
                ):
                    continue
                provider_data.pop("openai_responses_reasoning_item", None)
                stripped += 1
        if stripped:
            # Baselines/previous_response_id chains may still contain the removed
            # raw reasoning items.  Rebase the next request from sanitized local
            # history instead of trying to continue the poisoned epoch.
            self._reset_ws_epoch("encrypted_reasoning_self_heal")
        return stripped

    def reset_provider_turn_state(self) -> None:
        self.reset_ws_turn()

    def reset_ws_turn(self) -> None:
        """Reset per-turn websocket state at a new user turn boundary.

        The official ``x-codex-turn-state`` is per-turn volatile: captured on the
        first request of a turn, replayed within the turn, and reset for the next
        user turn (``client.rs:227-240`` / ``turn_state.rs`` tests). Callers that
        track turn boundaries invoke this between user turns; within a tool loop
        it must NOT be called so the token (and incremental chain) persist.
        """
        self._ws_session.turn_state = None

    def _frozen_responses_input(self, iface: ChatInterface) -> list[dict[str, Any]]:
        """``to_responses_input`` with per-session tool-result output freezing.

        Routes every Codex WS conversion through ``_freeze_responses_outputs`` so
        the model-facing ``function_call_output.output`` for a given ``call_id``
        stays byte-identical across turns, even when canonical content was
        rewritten in place (summarize markers, placeholder overwrites). All
        three WS conversion sites (full replay, per-turn delta, baseline tail)
        share ``self._ws_frozen_outputs`` so the baseline and the next full
        request remain strict-prefix comparable. After an epoch reset the
        cleared freeze map re-freezes from ``to_responses_input``'s shared
        serialization, so the rebuilt replay does not strip any historical
        ``_meta.agent_meta`` / ``guidance`` / ``notifications`` /
        ``notification_guidance`` copy, without mutating canonical history.
        """
        return _freeze_responses_outputs(
            to_responses_input(iface),
            self._ws_frozen_outputs,
        )

    def _interface_entries_to_responses_input(self, entries: list[Any]) -> list[dict[str, Any]]:
        """Serialize newly-added ChatInterface entries for stateful Codex turns."""

        if not entries:
            return []
        delta_interface = ChatInterface()
        # ChatInterface.entries intentionally exposes the mutable backing list;
        # populate a temporary interface so the normal converter preserves
        # reasoning/tool-result shapes and pairing behavior for the delta.
        delta_interface.entries.extend(entries)
        return self._frozen_responses_input(delta_interface)

    def _codex_report_account_error(self, exc: Exception) -> Exception:
        """Report one structural failure and return its replay-safe form."""
        reported = exc
        if self._codex_partial_output:
            reported = mark_llm_replay_terminal(
                reported,
                partial_stream=True,
                message="Provider output was partially delivered",
            )
        if self._codex_account_error_reported:
            return reported
        self._codex_account_error_reported = True
        callback = self._codex_account_error_callback
        if callback is not None:
            callback_exc = (
                object.__getattribute__(reported, "original")
                if type(reported) is LLMReplayTerminalError
                else reported
            )
            try:
                callback(callback_exc, self._codex_partial_output)
            except Exception:
                # Account telemetry is secondary.  It may inspect the original
                # provider error, but cannot mutate or replace the replay-safe
                # kernel wrapper that must reach BaseAgent.
                pass
        return reported

    def _codex_apply_account_binding(self, binding: dict[str, Any]) -> None:
        """Apply one owned binding to both REST and websocket auth state."""
        previous_sha8 = self._codex_auth_path_sha8
        previous_api_key = getattr(self, "_ws_api_key", None)
        next_sha8 = binding.get("auth_path_sha8")
        next_api_key = binding.get("api_key")
        account_changed = bool(
            previous_sha8 and next_sha8 and previous_sha8 != next_sha8
        )
        credential_changed = bool(
            self._codex_binding_generation is not None
            and previous_api_key
            and next_api_key
            and previous_api_key != next_api_key
        )
        if not self._codex_account_epoch_boundary_pending:
            # A websocket transport authenticates only at connect time. Never
            # retain that connection or its continuation chain across an account
            # switch or an in-place credential replacement.
            if account_changed:
                self._reset_ws_epoch("codex_account_switch")
            elif credential_changed:
                self._reset_ws_epoch("codex_token_refresh")
        self._codex_account_epoch_boundary_pending = False

        self._client.api_key = next_api_key
        if hasattr(self, "_ws_api_key"):
            self._ws_api_key = next_api_key
        self._account_id = binding.get("account_id")
        self._codex_auth_path_sha8 = next_sha8
        self._codex_auth_path_source = binding.get("auth_path_source")
        generation = binding.get("binding_generation")
        self._codex_binding_generation = (
            generation if isinstance(generation, int) and not isinstance(generation, bool) else None
        )
        self._codex_binding_api_key = (
            next_api_key if isinstance(next_api_key, str) and next_api_key else None
        )
        selection = binding.get("selection")
        if isinstance(selection, dict):
            self._codex_pool_selection = dict(selection)
            self.codex_pool_selection = dict(selection)

    def _codex_refresh_account_for_request(self) -> None:
        """Bind and publish the current epoch account as one owned transaction."""
        self._codex_account_error_reported = False
        self._codex_partial_output = False
        self._codex_token_expired_recovery_attempted = False
        # One absolute deadline for the whole logical request: the first wire
        # call, token recovery (auth-file lock + OAuth HTTP), and the bounded
        # retry all consume the SAME watchdog budget rather than each
        # restarting it.
        self._codex_request_deadline = (
            time.monotonic() + self._request_timeout
            if self._request_timeout is not None
            else None
        )
        callback = self._codex_account_request_callback
        if callback is None:
            return
        binding = callback(self._codex_apply_account_binding)
        if not isinstance(binding, dict):
            raise TypeError("Codex account request callback returned non-dict binding")

    @staticmethod
    def _codex_mark_provider_recovery_terminal(exc: Exception) -> Exception:
        """Return an exception that verifiably fail-closes outer retries."""
        return mark_llm_replay_terminal(
            exc,
            no_aed_retry=True,
            message="Provider recovery failed after bounded retry",
        )

    def _codex_remaining_wire_budget(self) -> float | None:
        """Remaining absolute-deadline budget for the next wire operation.

        The deadline is armed before account binding, which may spend the
        whole budget on token-file locks and OAuth work. A wire call started
        past the deadline with a fresh full timeout can deliver visible output
        and commit the assistant response after the main-thread watchdog has
        already expired, so fail closed here with the exact no-AED terminal
        wrapper instead of starting the call at all.
        """
        deadline = self._codex_request_deadline
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            exhausted = TimeoutError(
                "Codex logical request deadline exhausted before the provider "
                "call could start"
            )
            raise self._codex_mark_provider_recovery_terminal(
                exhausted
            ) from exhausted
        return remaining

    def _codex_fail_closed_after_recovery(
        self,
        exc: Exception,
        *,
        drop_trailing_user: bool = False,
        committed_entry_ids: frozenset[int] | None = None,
        reset_continuation: bool = False,
    ) -> Exception:
        """Terminalize recovery fallout without letting cleanup reopen AED."""
        escaped = self._codex_mark_provider_recovery_terminal(exc)
        try:
            escaped = self._codex_report_account_error(escaped)
        except Exception:
            # Account telemetry is secondary to preserving the terminal marker.
            pass
        try:
            if committed_entry_ids is not None:
                self._interface.drop_trailing(
                    lambda entry: entry.id not in committed_entry_ids,
                )
            elif drop_trailing_user:
                self._interface.drop_trailing(lambda entry: entry.role == "user")
        except Exception:
            # Interface cleanup must never replace the marked provider failure.
            pass
        if reset_continuation:
            try:
                self._reset_ws_epoch("provider_recovery_terminal")
            except Exception:
                # The agent will sleep on the marked failure. Preserve that
                # invariant even if best-effort continuation cleanup fails.
                pass
            self._response_id = None
        return escaped

    @staticmethod
    def _codex_reraise_terminalized(original: Exception, terminal: Exception) -> None:
        """Raise a fail-closed wrapper only when the original rejected metadata."""
        if terminal is original:
            raise original
        raise terminal from original

    def _codex_apply_recovered_binding(
        self, binding: dict[str, Any], request_kwargs: dict[str, Any]
    ) -> None:
        """Publish transport auth and the account header under context ownership."""
        self._codex_apply_account_binding(binding)
        headers = dict(request_kwargs.get("extra_headers") or {})
        if self._account_id:
            headers["ChatGPT-Account-ID"] = self._account_id
        else:
            headers.pop("ChatGPT-Account-ID", None)
        if headers:
            request_kwargs["extra_headers"] = headers
        else:
            request_kwargs.pop("extra_headers", None)

    def _codex_try_token_expired_recovery(
        self, exc: Exception, request_kwargs: dict[str, Any]
    ) -> Any | None | Exception:
        """Refresh once and create the retry stream inside context ownership."""
        if self._codex_partial_output:
            return None
        from lingtai.auth.codex import _is_token_expired_error

        if not _is_token_expired_error(exc):
            return None
        if self._codex_token_expired_recovery_attempted:
            return self._codex_mark_provider_recovery_terminal(exc)
        callback = self._codex_token_expired_callback
        if callback is None:
            return self._codex_mark_provider_recovery_terminal(exc)

        # Consume the request-local budget before calling out. Every ordinary
        # exception after this point is terminal for this logical request.
        self._codex_token_expired_recovery_attempted = True
        apply_binding = lambda binding: self._codex_apply_recovered_binding(
            binding, request_kwargs
        )

        def retry_request():
            # Recovery may have spent most of the logical budget on the
            # auth-file lock and OAuth refresh. Never start the second wire
            # call past the absolute deadline, and give it only the remaining
            # per-call budget. Raising here stays inside the callback's
            # fail-closed terminalization, so the escape is the exact no-AED
            # wrapper rather than a fresh transient-looking timeout.
            deadline = self._codex_request_deadline
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Codex token recovery exhausted the logical request "
                        "deadline before the bounded provider retry"
                    )
                request_kwargs["timeout"] = _build_http_timeout(remaining)
            return self._client.responses.create(**request_kwargs)
        try:
            retry_stream = callback(
                exc,
                self._codex_binding_generation,
                self._codex_binding_api_key,
                self._codex_auth_path_sha8,
                apply_binding,
                retry_request,
            )
        except Exception as callback_exc:
            terminal = self._codex_mark_provider_recovery_terminal(callback_exc)
            if terminal is callback_exc:
                raise
            raise terminal from callback_exc
        if retry_stream is None:
            return self._codex_mark_provider_recovery_terminal(exc)
        return retry_stream

    def _codex_rest_stream_with_token_recovery(
        self, request_kwargs: dict[str, Any]
    ):
        """Create and iterate one REST stream with at most one safe auth replay."""
        post_recovery_stream = False
        # The FIRST attempt shares the absolute deadline with token recovery:
        # never start a wire call past it, and give the call only the
        # remaining per-call budget rather than the original full timeout.
        remaining = self._codex_remaining_wire_budget()
        if remaining is not None:
            request_kwargs["timeout"] = _build_http_timeout(remaining)
        try:
            stream = self._client.responses.create(**request_kwargs)
        except Exception as exc:
            recovery = self._codex_try_token_expired_recovery(exc, request_kwargs)
            if recovery is None:
                raise
            if isinstance(recovery, Exception):
                self._codex_reraise_terminalized(exc, recovery)
            stream = recovery
            post_recovery_stream = True

        def _events():
            first_attempt_event_observed = False
            try:
                for event in stream:
                    # Set before yielding: once the caller can mutate its stream
                    # accumulator, this first attempt can never be replayed.
                    first_attempt_event_observed = True
                    yield event
            except Exception as exc:
                if post_recovery_stream:
                    terminal = self._codex_mark_provider_recovery_terminal(exc)
                    if terminal is exc:
                        raise
                    raise terminal from exc
                if first_attempt_event_observed:
                    from lingtai.auth.codex import _is_token_expired_error

                    if _is_token_expired_error(exc):
                        terminal = self._codex_mark_provider_recovery_terminal(exc)
                        if terminal is exc:
                            raise
                        raise terminal from exc
                    raise
                recovery = self._codex_try_token_expired_recovery(exc, request_kwargs)
                if recovery is None:
                    raise
                if isinstance(recovery, Exception):
                    self._codex_reraise_terminalized(exc, recovery)
                try:
                    yield from recovery
                except Exception as retry_exc:
                    terminal = self._codex_mark_provider_recovery_terminal(retry_exc)
                    if terminal is retry_exc:
                        raise
                    raise terminal from retry_exc

        return _events()

    def send_stream(self, message, on_chunk=None, on_output_chars=None) -> LLMResponse:
        # ``on_chunk`` receives visible ``output_text`` deltas only (legacy);
        # the count-only ``on_output_chars`` receives the length of every
        # output fragment this loop sees — a tool-call-only generation still
        # reports progress.
        #
        # Maintain the canonical interface for local recovery and full-replay
        # fallback, but capture the entries added by this turn so subsequent
        # stored-response requests can send only the incremental delta.
        interface_start = len(self._interface.entries)
        trailing_entries = 0
        if message is None:
            pass
        elif isinstance(message, str):
            self._interface.add_user_message(message)
            trailing_entries = 1
        elif isinstance(message, list):
            # ToolResultBlock list, the canonical kernel shape coming back
            # from ToolExecutor via _make_tool_result_fn.
            if message and all(isinstance(b, ToolResultBlock) for b in message):
                self._interface.add_tool_results(message)
                trailing_entries = len(message)
            else:
                # Pre-built wire dicts (legacy / tests). Fall back to the
                # parent's converter below so behavior matches what callers
                # passing dicts expect.
                pass
        elif isinstance(message, dict):
            pass
        else:
            raise TypeError(f"Unsupported message type: {type(message)}")

        # Pre-request hook — kernel-side splice point. Include hook-spliced
        # entries in the delta so notification wake pairs remain visible in the
        # same request even when previous_response_id is active.
        if self.pre_request_hook is not None:
            self.pre_request_hook(self._interface)

        # Capture EXACTLY ONE immutable runtime-effort decision for this logical
        # dispatch — after the pre-request stage above, before any request
        # kwargs / interface / account mutation below. Every downstream path
        # (REST, the WebSocket frame, the encrypted-reasoning self-heal retry,
        # the incremental fallback) reads the captured value from the request
        # kwargs and never re-reads the controller, so a concurrent set/clear
        # only reaches the NEXT dispatch.
        effort_effective = self._capture_effort_snapshot()

        # Resolve hard/explicit provider-context boundaries before account
        # binding.  A 100% forced rebuild is a true rebuild even when no summary
        # marker exists, so its own request must receive the fresh account draw.
        if self._continuation_enabled:
            self._maybe_reset_ws_epoch()
        self._codex_refresh_account_for_request()

        try:
            self._interface.enforce_tool_pairing()
            prebuilt_items: list[dict[str, Any]] = []
            if isinstance(message, dict):
                prebuilt_items.append(message)
            elif isinstance(message, list) and not (
                message and all(isinstance(b, ToolResultBlock) for b in message)
            ):
                prebuilt_items.extend(self._convert_input(message))

            self._maybe_compact_before_send(prebuilt_items)
            compacted_replay = self._compacted_replay_input()
            if compacted_replay is not None:
                # Standalone compaction is active: replay the opaque compacted
                # prefix plus the strict-additive delta instead of re-converting
                # the full canonical interface. Compaction and the WebSocket
                # delta/`previous_response_id` machinery both assume the full
                # converted interface as their comparison baseline, so route a
                # compacted turn through the REST full-replay path (REST is the
                # hardcoded normal-runtime transport; WebSocket is a test-only
                # opt-in — see ``_CODEX_TRANSPORT_DEFAULT``).
                full_replay_input_items = compacted_replay
                full_replay_input_items.extend(prebuilt_items)
                ws_enabled_this_turn = False
            else:
                full_replay_input_items = self._frozen_responses_input(self._interface)
                full_replay_input_items.extend(prebuilt_items)
                ws_enabled_this_turn = self._ws_enabled

            delta_entries = self._interface.entries[interface_start:]
            delta_input_items = self._interface_entries_to_responses_input(delta_entries)
            delta_input_items.extend(prebuilt_items)

            # Build the request from the FULL input first. Both transports then
            # run the SAME full->incremental planner against the assembled request
            # (WS inside ``_codex_ws_open``; REST at the dispatch site below) so the
            # planner's non-input equality check compares like-for-like request
            # shapes. Starting from full keeps the first turn / mismatch / epoch
            # reset path correct. On WS, ``incremental`` downgrades the frame to a
            # strict delta + ``previous_response_id``; on REST the label is a
            # cache-epoch annotation only and the full input is always sent.
            previous_response_id: str | None = None
            input_items = full_replay_input_items
            request_mode = "ws_full" if ws_enabled_this_turn else "rest_full"

            kwargs: dict[str, Any] = {
                "model": self._model,
                "input": input_items,
                "stream": True,
                **self._extra_kwargs,
            }
            # The ChatGPT Codex endpoint explicitly rejects store=True with
            # `{'detail': 'Store must be set to false'}`. Keep store=false on EVERY
            # request and every transport. REST never sends ``previous_response_id``
            # (both REST modes replay the full self-contained context); on the
            # WebSocket path the strict-additive ``incremental`` continuation is what
            # carries ``previous_response_id``, also with ``store=false``.
            kwargs["store"] = False
            # Align the wire timeout with the main-thread watchdog, exactly as
            # the Chat Completions builder does. Without this the SDK keeps the
            # constructor default and the worker can outlive the watchdog by
            # minutes. ``timeout`` is an SDK transport kwarg: the WS/REST
            # continuation planners exclude it (``_WS_NON_FRAME_KEYS``).
            if self._request_timeout is not None:
                kwargs["timeout"] = _build_http_timeout(self._request_timeout)
            # Apply the captured effort snapshot to the ONE request-kwargs seam.
            # A FRESH dict is assigned: ``**self._extra_kwargs`` above aliases
            # the session's construction ``reasoning`` object, and mutating it
            # in place would permanently rewrite the construction baseline for
            # every later turn. ``dict(kwargs)`` copies in the retry/fallback
            # paths below therefore inherit exactly this object.
            if effort_effective is not None:
                kwargs["reasoning"] = {
                    **(self._extra_kwargs.get("reasoning") or {}),
                    "effort": effort_effective,
                }
            # Ensure reasoning.encrypted_content is requested so the raw
            # reasoning item can be preserved for prompt-cache-stable replay.
            existing_include = kwargs.get("include") or []
            if isinstance(existing_include, str):
                existing_include = [existing_include]
            else:
                try:
                    existing_include = list(existing_include)
                except TypeError:
                    existing_include = [existing_include]
            if "reasoning.encrypted_content" not in existing_include:
                kwargs["include"] = existing_include + ["reasoning.encrypted_content"]
            if self._instructions:
                kwargs["instructions"] = self._instructions
            if self._tools:
                kwargs["tools"] = self._tools
                if self._tool_choice:
                    kwargs["tool_choice"] = self._tool_choice
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            if self._compact_threshold:
                kwargs["context_management"] = [
                    {"type": "compaction", "compact_threshold": self._compact_threshold}
                ]
            # Resolve this request's cache-affinity values — the single stable
            # per-agent id (a pure hash of the agent path). All three levers
            # (prompt_cache_key / session_id / thread_id) carry the same value on
            # every request and never change for the life of the session.
            effective_cache_key, affinity_headers = self._effective_affinity()
            # Opt into Codex prompt caching with the resolved key. We send only
            # `prompt_cache_key`; the Codex backend rejects `prompt_cache_retention`
            # (Unsupported parameter), so it is deliberately never sent.
            if effective_cache_key:
                kwargs["prompt_cache_key"] = effective_cache_key
            # REST cache-affinity headers (issue #378). Sent as HTTP headers via
            # the SDK's per-request ``extra_headers``, never as request-body
            # fields. ``session_id`` / ``thread_id`` route the per-agent cache
            # slot and are a single stable per-agent value (never rotated).
            # Client identity (#436 → #471 experiment) forms the base;
            # cache-affinity and caller-supplied headers layer on top so they
            # always win. The default app-name identity is honest LingTai;
            # stable affinity/metadata headers remain LingTai-owned.
            # see ``_codex_identity_headers`` / ``_CODEX_IMPERSONATE_OFFICIAL_CLI``.
            extra_headers = {
                **_codex_identity_headers(),
                **affinity_headers,
                **self._codex_metadata_headers(),
            }
            # The user's own ChatGPT account id, when available. Canonical
            # official spelling ``ChatGPT-Account-ID`` (HTTP header names are
            # case-insensitive). Attributes the request to the right ChatGPT
            # account; orthogonal to the app-name identity above. Omitted
            # entirely when no account id is known.
            if self._account_id:
                extra_headers["ChatGPT-Account-ID"] = self._account_id
            if extra_headers:
                kwargs["extra_headers"] = {
                    **extra_headers,
                    **kwargs.get("extra_headers", {}),
                }
            client_metadata = self._codex_client_metadata()
            if client_metadata:
                extra_body = dict(kwargs.get("extra_body") or {})
                existing_client_metadata = dict(extra_body.get("client_metadata") or {})
                extra_body["client_metadata"] = {**existing_client_metadata, **client_metadata}
                kwargs["extra_body"] = extra_body
            acc = StreamingAccumulator()
            progress = OutputProgress(on_output_chars)
            response_id = None
            usage = UsageMetadata()
            seen_reasoning_summary_items: set[str] = set()
            # Raw reasoning item dicts for replay, in provider output order.
            raw_reasoning_items: list[dict[str, Any]] = []
            trace_path = _codex_responses_trace_path()
            fallback_error_type: str | None = None
            fallback_error_message: str | None = None

            request_store = bool(kwargs.get("store"))
            # Tracks whether THIS turn ran the full/incremental continuation
            # machine (either transport), so the post-turn baseline/ledger update
            # fires for REST as well as WebSocket.
            continuation_turn_recorded = False

            # WebSocket transport (#471). When selected, try to send a Responses
            # ``response.create`` frame over the websocket with an incremental
            # delta + ``previous_response_id`` (or full input on the first request
            # / on any mismatch). On ANY websocket problem (handshake 426,
            # connect/auth error, missing runtime, delta mismatch) we raise
            # ``_CodexWsFallback`` internally and drop to the HTTP path below.
            # ``store`` stays ``false``.
            ws_stream = None
            if ws_enabled_this_turn:
                # The websocket connect authenticates on open; it must obey
                # the same absolute deadline as the REST wire call below.
                self._codex_remaining_wire_budget()
                try:
                    ws_stream, ws_mode, ws_prev_id = self._codex_ws_open(
                        kwargs,
                        full_replay_input_items=full_replay_input_items,
                    )
                except _CodexWsFallback as exc:
                    logger.info(
                        "Codex websocket path unavailable; using HTTP full replay: %s",
                        safe_exception_description(exc)[:240],
                    )
                    ws_stream = None
                else:
                    request_mode = ws_mode
                    previous_response_id = ws_prev_id
                    request_store = False
                    continuation_turn_recorded = True

            ws_stream_was_used = ws_stream is not None
            if ws_stream is not None:
                stream = ws_stream
            else:
                # REST transport. Run the SHARED full->incremental planner against
                # the fully-assembled request (so the planner's non-input equality
                # check compares the same request shape that gets parked as the
                # next baseline). The planner only LABELS the turn ``rest_full`` vs
                # ``rest_incremental`` (the cache-epoch semantic); it does NOT change
                # the REST wire payload. Both REST modes send the full self-contained
                # converted input and NEVER send ``previous_response_id`` — that
                # strict delta + ``previous_response_id`` continuation is a WebSocket
                # transport behavior only. ``store`` stays false on every request.
                # The FULL request is parked as the next baseline BEFORE the call
                # (exactly like the WebSocket path), and restored on failure so a bad
                # turn never poisons the chain.
                rest_continuation = (
                    self._transport == "rest" and self._continuation_enabled
                )
                rest_prev_last_request = self._ws_session.last_request
                rest_prev_last_response = self._ws_session.last_response
                if rest_continuation:
                    rest_full_request = self._ws_frame_request(
                        kwargs, full_replay_input_items
                    )
                    if compacted_replay is not None:
                        # A freshly-compacted (or still-active) replay basis is
                        # a new provider-context epoch by construction — it
                        # does not extend the pre-compaction ``last_request``
                        # baseline, so label it ``rest_full`` rather than
                        # running the strict prefix-match planner against an
                        # unrelated baseline.
                        request_mode = "rest_full"
                    else:
                        request_mode, _planned_delta_input, _planned_previous_response_id = (
                            self._codex_plan_continuation(
                                rest_full_request,
                                full_replay_input_items,
                                full_mode="rest_full",
                                incremental_mode="rest_incremental",
                            )
                        )
                    # REST incremental is a cache/epoch semantic, not a wire-delta
                    # semantic: the REST API still receives the full converted input
                    # so it is self-contained, while WebSocket incremental is the
                    # transport that carries delta + previous_response_id.
                    kwargs["input"] = full_replay_input_items
                    previous_response_id = None
                    kwargs.pop("previous_response_id", None)
                    # Park the FULL request (not the delta) as the next baseline.
                    self._ws_session.last_request = rest_full_request
                    self._ws_pending_baseline_input = list(full_replay_input_items)
                    continuation_turn_recorded = True
                try:
                    stream = self._codex_rest_stream_with_token_recovery(kwargs)
                except Exception as exc:
                    if not (previous_response_id or request_store):
                        # No continuation was in flight (first full turn): usually a
                        # real error, not a recoverable incremental rejection.  The
                        # Codex encrypted-reasoning verifier is the narrow exception:
                        # a stale opaque reasoning blob can brick every replay until
                        # it is removed from local history, while visible transcript
                        # text/tool calls remain valid.
                        if _is_codex_unverifiable_encrypted_content_error(exc):
                            stripped_reasoning_items = self._strip_codex_encrypted_reasoning_items()
                            if stripped_reasoning_items:
                                fallback_error_type = type(exc).__name__
                                fallback_error_message = safe_exception_description(exc)
                                logger.info(
                                    "Codex encrypted reasoning replay failed; stripped %s raw reasoning item(s) and retrying full replay",
                                    stripped_reasoning_items,
                                )
                                if compacted_replay is not None:
                                    # The rejected replay carried the compacted
                                    # prefix; a compacted ``compaction_summary``
                                    # item's own encrypted_content cannot be
                                    # stripped (it isn't a raw ``reasoning`` item),
                                    # so this retry falls back to full local
                                    # history. Invalidate rather than silently
                                    # diverge: the next turn must re-derive its
                                    # replay basis from what was actually sent.
                                    self._compacted_items = None
                                    self._compacted_at_entry_count = 0
                                retry_full_replay_input_items = self._frozen_responses_input(
                                    self._interface
                                )
                                retry_full_replay_input_items.extend(prebuilt_items)
                                retry_kwargs = dict(kwargs)
                                retry_kwargs["input"] = retry_full_replay_input_items
                                retry_kwargs["store"] = False
                                retry_kwargs.pop("previous_response_id", None)
                                request_mode = (
                                    "rest_full_self_heal"
                                    if self._transport == "rest"
                                    else "stateless_full_self_heal"
                                )
                                previous_response_id = None
                                request_store = False
                                full_replay_input_items = retry_full_replay_input_items
                                if rest_continuation:
                                    self._ws_session.last_request = self._ws_frame_request(
                                        retry_kwargs, retry_full_replay_input_items
                                    )
                                    self._ws_pending_baseline_input = list(
                                        retry_full_replay_input_items
                                    )
                                    continuation_turn_recorded = True
                                stream = self._codex_rest_stream_with_token_recovery(
                                    retry_kwargs
                                )
                            else:
                                if rest_continuation:
                                    self._ws_session.last_request = rest_prev_last_request
                                    self._ws_session.last_response = rest_prev_last_response
                                    self._ws_pending_baseline_input = None
                                raise
                        else:
                            if rest_continuation:
                                self._ws_session.last_request = rest_prev_last_request
                                self._ws_session.last_response = rest_prev_last_response
                                self._ws_pending_baseline_input = None
                            raise
                    else:
                        fallback_error_type = type(exc).__name__
                        fallback_error_message = safe_exception_description(exc)
                        logger.info(
                            "Codex incremental Responses request failed; falling back to full replay store=false: %s: %s",
                            fallback_error_type,
                            fallback_error_message[:240],
                        )
                        # Safe fallback for any future REST stateful variant that
                        # carries continuation state on the wire: re-send the FULL input
                        # with no ``previous_response_id`` and ``store=false``. Current
                        # REST incremental is already a full-input/cache-epoch request,
                        # so ordinary REST errors are allowed to surface instead of being
                        # hidden by an identical retry. The reason rides into the token
                        # ledger via ``codex_fallback_error_type`` / ``codex_request_mode``.
                        fallback_kwargs = dict(kwargs)
                        fallback_kwargs["input"] = full_replay_input_items
                        fallback_kwargs["store"] = False
                        fallback_kwargs.pop("previous_response_id", None)
                        request_mode = "rest_full_fallback" if self._transport == "rest" else "stateless_full_fallback"
                        previous_response_id = None
                        request_store = False
                        if rest_continuation:
                            # Re-park the baseline against the FULL request we actually
                            # sent, so the next turn can still chain off this full turn.
                            self._ws_session.last_request = self._ws_frame_request(
                                fallback_kwargs, full_replay_input_items
                            )
                            self._ws_pending_baseline_input = list(full_replay_input_items)
                        stream = self._codex_rest_stream_with_token_recovery(
                            fallback_kwargs
                        )
            for event in stream:
                _count_responses_output(event, progress)
                thoughts_before = acc.thoughts
                pending_thought_chars_before = len("".join(acc._thought_parts))
                accepted_reasoning = _handle_responses_reasoning_event(
                    event,
                    acc,
                    seen_reasoning_summary_items,
                    progress,
                )
                _codex_responses_trace_record(
                    event=event,
                    accepted_reasoning=accepted_reasoning,
                    thoughts_before=thoughts_before,
                    thoughts_after=acc.thoughts,
                    pending_thought_chars_before=pending_thought_chars_before,
                    pending_thought_chars_after=len("".join(acc._thought_parts)),
                    trace_path=trace_path,
                )
                if accepted_reasoning:
                    # Capture raw reasoning item when output_item.done carries
                    # encrypted_content, so it can be replayed verbatim next turn.
                    if (
                        event.type == "response.output_item.done"
                        and getattr(event.item, "type", None) == "reasoning"
                    ):
                        enc = getattr(event.item, "encrypted_content", None)
                        if enc:
                            item_id = getattr(event.item, "id", None) or ""
                            summaries = []
                            for s in getattr(event.item, "summary", None) or []:
                                summaries.append({
                                    "type": getattr(s, "type", None),
                                    "text": getattr(s, "text", None),
                                })
                            content = []
                            for c in getattr(event.item, "content", None) or []:
                                if hasattr(c, "model_dump"):
                                    content.append(c.model_dump(exclude_none=True))
                                elif isinstance(c, dict):
                                    content.append(c)
                                else:
                                    logger.warning(
                                        "codex.responses.reasoning_content_ignored",
                                        extra={
                                            "item_id": item_id,
                                            "content_type": type(c).__name__,
                                        },
                                    )
                            raw_reasoning_items.append({
                                "type": "reasoning",
                                "id": item_id,
                                "summary": summaries,
                                "content": content,
                                "encrypted_content": enc,
                            })
                    continue
                if event.type == "response.output_text.delta":
                    self._codex_partial_output = True
                    acc.add_text(event.delta)
                    if on_chunk:
                        on_chunk(event.delta)
                elif event.type == "response.function_call_arguments.delta":
                    self._codex_partial_output = True
                    acc.add_tool_args(event.delta)
                elif event.type == "response.function_call_arguments.done":
                    self._codex_partial_output = True
                    # Spark may emit complete args without any deltas.
                    _adopt_terminal_args(acc, progress, getattr(event, "arguments", None))
                elif event.type == "response.output_item.added":
                    if getattr(event.item, "type", None) == "function_call":
                        self._codex_partial_output = True
                        acc.start_tool(id=event.item.call_id, name=event.item.name)
                elif event.type == "response.output_item.done":
                    if getattr(event.item, "type", None) == "function_call":
                        self._codex_partial_output = True
                        # Use the final item as a second complete-args fallback.
                        _adopt_terminal_args(
                            acc, progress, getattr(event.item, "arguments", None)
                        )
                        acc.finish_tool()
                elif event.type == "response.completed":
                    response_id = event.response.id
                    # REST continuation: record the completed response so the NEXT
                    # turn can delta off it (the WebSocket path does the equivalent
                    # inside its own event generator). ``items_added`` is filled in
                    # post-turn from the canonical interface by
                    # ``_ws_record_baseline_from_interface``.
                    if (
                        ws_stream is None
                        and self._transport == "rest"
                        and self._continuation_enabled
                        and response_id
                    ):
                        self._ws_session.last_response = _CodexLastResponse(
                            response_id=response_id,
                            items_added=[],
                        )
                    if event.response.usage:
                        cached = getattr(event.response.usage, "input_tokens_details", None)
                        cached_tokens = (getattr(cached, "cached_tokens", 0) or 0) if cached else 0
                        input_tokens = getattr(event.response.usage, "input_tokens", 0) or 0
                        usage = UsageMetadata(
                            input_tokens=input_tokens,
                            output_tokens=getattr(event.response.usage, "output_tokens", 0) or 0,
                            thinking_tokens=getattr(
                                event.response.usage, "output_tokens_details", None
                            )
                            and getattr(
                                event.response.usage.output_tokens_details,
                                "reasoning_tokens",
                                0,
                            )
                            or 0,
                            cached_tokens=cached_tokens,
                            extra=self._usage_extra(
                                affinity_headers,
                                effective_cache_key,
                                request_mode=request_mode,
                                transport=self._transport,
                                previous_response_id=previous_response_id,
                                store=request_store,
                                fallback_error_type=fallback_error_type,
                                fallback_error_message=fallback_error_message,
                                ws_diag=(self._ws_last_diag if self._continuation_enabled else None),
                            ),
                        )
        except Exception as exc:
            # Once provider-owned auth recovery consumed this logical request's
            # budget, every later event-processing/stream exception is terminal
            # before outer transient/AED replay. Partial-stream marking remains
            # higher priority in the BaseAgent handler.
            if self._codex_token_expired_recovery_attempted:
                escaped = self._codex_fail_closed_after_recovery(
                    exc,
                    drop_trailing_user=True,
                )
            else:
                escaped = exc
                # Only the final exception escaping all built-in Codex fallback
                # paths may affect account exclusion. Intermediate fallbacks must
                # not poison the AED chain.
                escaped = self._codex_report_account_error(escaped)
                # Revert the trailing user entry we just added so the next retry
                # doesn't double-record it. Mirrors OpenAIChatSession.send's
                # error path. ToolResultBlock entries also revert — the executor
                # will re-supply them when AED rebuilds the loop.
                try:
                    self._interface.drop_trailing(lambda e: e.role == "user")
                except Exception:
                    # Interface cleanup must never replace the reported failure:
                    # a partial-stream wrapper that already marked visible
                    # output has to stay the escaping exception, or BaseAgent
                    # would replay delivered content through transient/AED.
                    pass
            self._codex_reraise_terminalized(exc, escaped)

        try:
            result = acc.finalize(usage=usage)
            # The provider dispatch actually completed. A rejected attempt
            # deliberately does NOT reach here, so its dispatch-start evidence
            # stays on record with ``completed`` false rather than vanishing
            # because no completion usage was ever produced. Mark only after
            # fresh-main's fail-closed finalization succeeds.
            if self._last_effort_dispatch is not None:
                self._last_effort_dispatch["completed"] = True
        except Exception as exc:
            if not self._codex_token_expired_recovery_attempted:
                raise
            escaped = self._codex_fail_closed_after_recovery(
                exc,
                drop_trailing_user=True,
            )
            self._codex_reraise_terminalized(exc, escaped)
        committed_entry_ids: frozenset[int] | None = None
        try:
            committed_entry_ids = frozenset(
                entry.id for entry in self._interface.entries
            )
            try:
                provider_input_tokens = int(usage.input_tokens or 0)
            except Exception:
                provider_input_tokens = 0
            self._last_provider_input_tokens = provider_input_tokens if provider_input_tokens > 0 else None
            # Local/provider calibration sample (HIGH-2 fix; corrected per PR #926
            # Sol source-audit finding): capture a local estimate of the EXACT
            # rendered request representation that was ACTUALLY sent for this
            # successful response — ``full_replay_input_items``, the same
            # opaque-compacted-prefix-plus-delta or full-conversion list that was
            # placed in ``kwargs["input"]`` (and kept in sync across the
            # encrypted-reasoning self-heal retry / incremental-fallback paths
            # above, which reassign it to whatever was actually resent) — paired
            # with the provider-reported actual above. This is deliberately NOT
            # ``ChatInterface.estimate_context_tokens()`` (the full raw canonical
            # history): before compaction the two representations are roughly the
            # same, but after compaction the raw canonical estimate keeps growing
            # with every pre-compaction turn forever (compaction never deletes
            # canonical entries) while the actual request shrinks to the opaque
            # prefix + live suffix. Calibrating against the raw estimate would
            # divide by an artificially large, ever-growing denominator and
            # silently under-project the next live delta. ``_projected_provider_tokens``
            # divides these two same-representation numbers to derive a
            # calibration ratio so the compaction trigger can PROJECT
            # provider-visible size from the CURRENT rendered representation
            # rather than only reacting after a past request already crossed the
            # limit. Opaque content (e.g. ``encrypted_content``) is counted in
            # memory only — never logged or persisted.
            if self._last_provider_input_tokens is not None:
                try:
                    self._last_local_estimate_tokens = _estimate_responses_input_tokens(
                        self._instructions, self._tools, full_replay_input_items,
                    )
                except Exception:
                    self._last_local_estimate_tokens = None
            # Successful provider response observed: re-arm the one-shot forced-rebuild
            # latch when usage dropped strictly below 1.0, or clear pending verification
            # when this is the first post-rebuild response (the boundary for the
            # persistent overflow warning). A failed request never reaches here.
            self._observe_provider_usage_for_boundary()

            # Record assistant response into the interface so it rides along on
            # the next request. Without this, the stateless backend would never
            # see the assistant's own prior turns.
            blocks: list = []
            raw_items = raw_reasoning_items
            if result.thoughts or raw_items:
                joined = "\n".join(t for t in result.thoughts if t)
                if raw_items:
                    # Attach every raw reasoning item (with encrypted_content), even
                    # when the provider returned no summary_text. Codex commonly
                    # returns summary=[] with encrypted_content; dropping the block
                    # in that case would lose the cache-stable replay state.
                    for idx, raw_item in enumerate(raw_items):
                        item_summary_text = "\n".join(
                            str(s.get("text"))
                            for s in raw_item.get("summary", [])
                            if isinstance(s, dict)
                            and s.get("type") == "summary_text"
                            and s.get("text")
                        )
                        blocks.append(
                            ThinkingBlock(
                                text=item_summary_text or (joined if idx == 0 else ""),
                                provider_data={
                                    "openai_responses_reasoning_item": raw_item,
                                },
                            )
                        )
                elif joined:
                    blocks.append(ThinkingBlock(text=joined))
            if result.text:
                blocks.append(TextBlock(text=result.text))
            for tc in result.tool_calls:
                blocks.append(ToolCallBlock(id=tc.id, name=tc.name, args=tc.args))
            if not blocks:
                blocks.append(TextBlock(text=""))
            self._interface.add_assistant_message(
                blocks,
                model=self._model,
                provider="codex",
                usage={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "thinking_tokens": usage.thinking_tokens,
                },
            )

            # Now that the assistant turn is in the canonical interface, recompute the
            # delta baseline in the SAME converter schema the next full request will
            # use, so a strict prefix can actually match next turn. Runs for BOTH
            # transports (it fixed the ``full``-every-turn root cause). On a turn that
            # produced no chainable response it leaves ``items_added`` empty and the
            # next turn falls back to full. See ``_ws_record_baseline_from_interface``.
            if self._continuation_enabled:
                self._ws_record_baseline_from_interface()
                # Count the turn + record the cache ledger whenever the continuation
                # machine actually drove a request this turn — the WebSocket wire
                # (``ws_stream_was_used``) OR the REST transport.
                if locals().get("ws_stream_was_used") or locals().get("continuation_turn_recorded"):
                    self._ws_turns_since_epoch_reset += 1
                    self._record_ws_cache_ledger(
                        request_mode=request_mode,
                        usage=usage,
                        ws_diag=self._ws_last_diag,
                    )

            # Stateless: don't persist the response_id beyond this single turn.
            # Stored only as a transient debug aid; never threaded into the next
            # request.
            self._response_id = response_id
            if self._codex_account_success_callback is not None:
                # Success only closes the current AED exclusion chain. The current
                # account binding remains sticky until the next approved boundary.
                self._codex_account_success_callback()
            return result
        except Exception as exc:
            if not self._codex_token_expired_recovery_attempted:
                raise
            escaped = self._codex_fail_closed_after_recovery(
                exc,
                committed_entry_ids=committed_entry_ids,
                reset_continuation=True,
            )
            self._codex_reraise_terminalized(exc, escaped)


class CodexOpenAIAdapter(OpenAIAdapter):
    """OpenAIAdapter variant that builds CodexResponsesSession instead of the
    standard server-stateful OpenAIResponsesSession.

    Use this with `provider=codex` only. Always set `use_responses=True,
    force_responses=True`. `base_url` defaults to the official Codex endpoint
    (`https://chatgpt.com/backend-api/codex`) but is configurable — the `codex`
    factory forwards an explicit `manifest.llm['base_url']`. Account selection
    remains inside this adapter; aliases do not create a second implementation.
    """

    def __init__(
        self,
        *args,
        codex_session_anchor: str | None = None,
        codex_thread_salt: str | None = None,
        codex_account_id: str | None = None,
        codex_auth_path_sha8: str | None = None,
        codex_auth_path_source: str | None = None,
        codex_base_urls: object = None,
        codex_molt_count: int | None = None,
        codex_compact_token_limit: int | None = None,
        codex_service_tier: str | None = None,
        codex_account_source: Any = None,
        codex_token_manager_factory: Callable[..., Any] | None = None,
        codex_fallback_auth_path: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # service_tier: normalized wire value from the common Codex boundary
        # (e.g. user ``fast`` → wire ``priority``).  ``None`` omits the field.
        self._codex_service_tier: str | None = (
            str(codex_service_tier) if codex_service_tier else None
        )
        # Standalone Codex compaction threshold (daemon task
        # ``context_token_limit``), Codex-only and orthogonal to the generic
        # ``compact_threshold``/``context_management`` this adapter always
        # forces to ``None`` in ``_create_responses_session`` (Codex rejects
        # that parameter). ``None`` here means "no explicit override"; the
        # session resolves its effective threshold from its own
        # ``context_window()`` at check time. Validated eagerly so an invalid
        # daemon task value fails at adapter construction, before any request.
        self._codex_compact_token_limit = _validate_codex_compact_token_limit(
            codex_compact_token_limit
        )
        # Codex REST cache-affinity identity: ONE per-agent value used
        # byte-identically for ``session_id``, ``thread_id``, and the default
        # ``prompt_cache_key``. It is a deterministic hash of the agent's durable
        # identity anchor (the resolved ``init.json`` / agent-dir path) AND the
        # agent's current molt count — no epoch, no time, no rotation, no operator
        # override. It is STABLE within a molt segment and CHANGES at each molt
        # boundary, so a molt starts on a fresh cache slot. Because the molt path
        # does NOT rebuild the adapter, the id is NOT computed once here — it is
        # (re)derived at request time from the live ``.agent.json`` molt_count
        # (see ``_resolve_codex_ids`` / ``_default_prompt_cache_key``). The
        # adapter has no per-agent identity of its own; the host wiring passes the
        # anchor down by default via these kwargs:
        #
        #   codex_session_anchor=str -> hash (anchor, current molt_count) into the
        #                               id for all three, refreshed per request
        #   (not set)                -> no session_id/thread_id (bare/test path)
        #
        # ``codex_thread_salt`` is accepted only as a legacy manifest
        # pass-through; it is intentionally NOT used to derive a separate thread
        # id. The root/main thread tracks the session id exactly so the three
        # values stay byte-identical.
        self._codex_session_anchor = (
            str(codex_session_anchor) if codex_session_anchor else None
        )
        self._codex_thread_salt = codex_thread_salt  # legacy pass-through; unused
        # The installation id is a stable identification token (not a cache-slot
        # router): anchor it on the agent path only, so it does NOT churn at molt
        # boundaries the way the cache-affinity id does.
        self._codex_installation_id = _codex_installation_id(self._codex_session_anchor)
        # The user's own ChatGPT account id, resolved upstream from their OAuth
        # auth data (explicit ``account_id`` field or decoded id_token claim).
        # Mutable so the token-refresh path can keep it current if refreshed
        # auth data changes it. ``None`` -> no ``ChatGPT-Account-ID`` header.
        self.codex_account_id: str | None = (
            str(codex_account_id) if codex_account_id else None
        )
        self.codex_auth_path_sha8: str | None = (
            str(codex_auth_path_sha8) if codex_auth_path_sha8 else None
        )
        self.codex_auth_path_source: str | None = (
            str(codex_auth_path_source) if codex_auth_path_source else None
        )
        # Account selection is a native Codex adapter concern.  The source is
        # deliberately retained as a live object so a weighted pool re-reads its
        # snapshot for every request; no session manager or pool chat wrapper is
        # involved.
        self._codex_account_source = codex_account_source
        self._codex_token_manager_factory = codex_token_manager_factory
        self._codex_fallback_auth_path = codex_fallback_auth_path
        self._codex_account_resolution_enabled = not (
            codex_account_source is None
            and codex_token_manager_factory is None
            and codex_fallback_auth_path is None
        )
        # Account state is owned by ``_CodexAccountContext`` instances created
        # below, never by this cached adapter.  This exclusion set is retained
        # only as a construction-time compatibility seam for older callers;
        # live failover state is copied into and kept on each context.
        self._codex_excluded_accounts: set[str] = set()
        self._codex_current_selection: dict[str, Any] = {}
        self._codex_selection_lock = threading.Lock()
        self._codex_context_owner = object()
        # Optional Codex-only endpoint POOL (molt-boundary shuffle). When this
        # carries 2+ valid entries, ``create_chat`` chooses one at request time
        # by (stable per-agent offset + current molt_count) so the endpoint is
        # stable within a molt segment and rotates only at a molt boundary. An
        # empty pool -> the single ``base_url`` resolved at construction (PR #495
        # behavior, untouched). This is orthogonal to the cache identity
        # (``session_id`` / ``thread_id`` / ``prompt_cache_key``), which is
        # derived from the anchor + current molt_count at request time — see
        # ``_resolve_codex_ids``.
        self._codex_base_urls: tuple[str, ...] = _parse_codex_base_urls(codex_base_urls)
        # ``base_url`` resolved at construction — the fall-back when the pool is
        # empty, and the value ``create_chat`` compares against to detect a
        # molt-driven endpoint change. (``self.base_url`` is mutated in place on
        # a switch; this fixed copy is the legacy single-endpoint anchor.)
        self._codex_fixed_base_url = self.base_url
        # Explicit molt_count override (tests / hosts). When set, used instead of
        # reading ``<working_dir>/.agent.json``.
        self._codex_molt_count_override = codex_molt_count
        # ``<working_dir>/.agent.json`` is the sibling of the per-agent anchor
        # (the resolved ``init.json`` path). Derived once; read fresh per request
        # so a live process observes molt_count changes without a rebuild.
        self._codex_agent_json_path: Path | None = (
            Path(self._codex_session_anchor).parent / ".agent.json"
            if self._codex_session_anchor
            else None
        )
        # Stable per-agent offset so different agents distribute across the pool.
        # Prefer the agent-path anchor; fall back to a fixed constant for a
        # bare/no-identity adapter (degenerate but deterministic). Molt-independent
        # on purpose: the offset must not move with molt_count or the pool index
        # would advance twice per molt.
        offset_seed = self._codex_session_anchor or "codex"
        self._codex_pool_offset = int(
            hashlib.sha256(offset_seed.encode("utf-8")).hexdigest(), 16
        )

    def _new_codex_token_manager(self, auth_path: str | None = None):
        factory = self._codex_token_manager_factory
        if factory is None:
            from lingtai.auth.codex import CodexTokenManager
            factory = CodexTokenManager
        if auth_path:
            return factory(token_path=auth_path)
        return factory()

    def _new_codex_account_context(
        self, model: str, interface: ChatInterface
    ) -> _CodexAccountContext:
        """Return the account context attached to this product conversation."""
        context = getattr(interface, "_lingtai_codex_account_context", None)
        if (
            isinstance(context, _CodexAccountContext)
            and context.owner is self._codex_context_owner
            and context.model == model
        ):
            return context
        context = _CodexAccountContext(
            owner=self._codex_context_owner,
            model=model,
            client=self._clone_codex_client(),
            excluded_accounts=set(self._codex_excluded_accounts),
        )
        # A rebuilt ChatSession normally reuses its ChatInterface.  Carrying the
        # context on that interface preserves AED exclusions and sticky binding,
        # while unrelated interfaces from the cached adapter remain isolated.
        interface._lingtai_codex_account_context = context
        return context

    def _clone_codex_client(self):
        """Create a client whose mutable authentication belongs to one context."""
        if isinstance(openai.OpenAI, type) and isinstance(self._client, openai.OpenAI):
            return openai.OpenAI(**dict(self._client_kwargs))
        # Unit/integration fakes replace ``_client`` (or patch ``openai.OpenAI``
        # itself, e.g. with a ``MagicMock``) after construction.  Keep their
        # response capture seam while still giving each chat its own mutable
        # api_key/account-bearing client object.
        return copy.copy(self._client)

    @staticmethod
    def _set_codex_account_binding(
        context: _CodexAccountContext, binding: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace this context's binding and publish a new ownership generation."""
        context.binding_generation += 1
        owned = dict(binding)
        owned["binding_generation"] = context.binding_generation
        context.binding = owned
        selection = owned.get("selection")
        context.selection = dict(selection) if isinstance(selection, dict) else {}
        return dict(owned)

    @staticmethod
    def _clear_codex_account_binding(context: _CodexAccountContext) -> None:
        """Forget only this context's account for its next approved epoch."""
        context.binding_generation += 1
        context.binding = {}
        context.selection = {}
        context.bound_molt_count = None

    def _codex_account_epoch_reset(
        self, context: _CodexAccountContext, reason: str
    ) -> None:
        """Start a fresh account epoch at an approved context boundary."""
        if reason in {"summarize_delayed", "summarize_rebuild_only"}:
            with context.lock:
                self._clear_codex_account_binding(context)

    @staticmethod
    def _valid_codex_quota_percent(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == value
            and 0.0 <= float(value) <= 100.0
        )

    @staticmethod
    def _codex_snapshot_identity(candidate: object) -> str | None:
        identity = getattr(candidate, "sha8", None)
        if not isinstance(identity, str):
            identity = getattr(candidate, "auth_path_sha8", None)
        return identity if isinstance(identity, str) else None

    @classmethod
    def _codex_count_snapshot_matches(
        cls,
        snapshot: object,
        identities: set[str],
    ) -> int:
        if not isinstance(snapshot, (list, tuple)):
            return 0
        return sum(
            cls._codex_snapshot_identity(candidate) in identities
            for candidate in snapshot
        )

    @classmethod
    def _codex_no_candidate_diagnostics(
        cls,
        *,
        snapshot: object,
        context: _CodexAccountContext,
        zero_accounts: set[str],
        quota_target_count: int,
        quota_observed_count: int,
        quota_read_error_count: int,
        quota_invalid_count: int,
        quota_left: dict[str, float] | None,
        fallback_auth_path: str | None,
    ) -> dict[str, int | bool]:
        combined_exclusions = context.excluded_accounts | zero_accounts
        if snapshot is None:
            pool_size = 1
            excluded = min(len(combined_exclusions), 1)
            zero_quota = 0
        else:
            pool_size = len(snapshot) if isinstance(snapshot, (list, tuple)) else 0
            excluded = cls._codex_count_snapshot_matches(
                snapshot, combined_exclusions
            )
            zero_quota = cls._codex_count_snapshot_matches(snapshot, zero_accounts)
        return {
            "codex_account_pool_size": pool_size,
            "codex_account_excluded_count": excluded,
            "codex_account_zero_quota_count": zero_quota,
            "codex_account_eligible_count": max(pool_size - excluded, 0),
            "codex_account_quota_target_count": quota_target_count,
            "codex_account_quota_observed_count": quota_observed_count,
            "codex_account_quota_read_error_count": quota_read_error_count,
            "codex_account_quota_invalid_count": quota_invalid_count,
            "codex_account_quota_snapshot_complete": quota_left is not None,
            "codex_account_legacy_fallback_allowed": (
                fallback_auth_path is not None
                and isinstance(snapshot, (list, tuple))
                and not snapshot
            ),
        }

    def _refresh_codex_bound_quota(
        self, context: _CodexAccountContext
    ) -> dict[str, Any]:
        """Refresh safe quota telemetry without redrawing this context."""
        binding = dict(context.binding)
        selection = dict(binding.get("selection") or {})
        auth_ref = binding.get("auth_ref")
        if auth_ref:
            try:
                from lingtai.llm.openai.codex_quota import read_remaining_percent
                percent = read_remaining_percent(auth_ref)
            except Exception:
                percent = None
            if self._valid_codex_quota_percent(percent):
                selection["quota_left"] = round(float(percent), 3)
            else:
                selection.pop("quota_left", None)
        binding["selection"] = selection
        context.binding = binding
        context.selection = dict(selection)
        return binding

    def _publish_legacy_codex_binding(self, binding: dict[str, Any]) -> None:
        """Preserve the old private direct-selection test seam.

        Normal chat requests never use this path; they keep credentials on their
        context client.  A few callers historically invoked
        ``_select_codex_account(model)`` directly before endpoint switching.
        """
        self._client.api_key = binding.get("api_key")
        self._client_kwargs["api_key"] = binding.get("api_key")
        self.codex_account_id = binding.get("account_id")
        self.codex_auth_path_sha8 = binding.get("auth_path_sha8")
        self.codex_auth_path_source = binding.get("auth_path_source")

    def _select_codex_account(
        self, context: _CodexAccountContext | str
    ) -> dict[str, Any]:
        """Select and bind one account to this context's current epoch."""
        legacy_direct = isinstance(context, str)
        if legacy_direct:
            context = _CodexAccountContext(
                owner=self._codex_context_owner,
                model=context,
                client=self._client,
                excluded_accounts=set(self._codex_excluded_accounts),
            )
        model = context.model
        # AccountSource/token refresh may touch shared pool/auth files. Serialize
        # that narrow operation, but keep all resulting state on ``context``.
        with self._codex_selection_lock:
            source = self._codex_account_source
            if (
                source is None
                and self._codex_fallback_auth_path is None
                and self._codex_token_manager_factory is None
            ):
                binding = {
                    "api_key": self._client_kwargs.get("api_key"),
                    "account_id": self.codex_account_id,
                    "auth_path_sha8": self.codex_auth_path_sha8,
                    "auth_path_source": self.codex_auth_path_source,
                    "selection": dict(context.selection),
                }
                binding = self._set_codex_account_binding(context, binding)
                context.bound_molt_count = self._current_molt_count()
                if legacy_direct:
                    self._publish_legacy_codex_binding(binding)
                return binding
            if source is None:
                from lingtai.auth.codex_account_source import FixedAccountSource
                source = FixedAccountSource(self._codex_fallback_auth_path or "")

            quota_left: dict[str, float] | None = None
            observed_quota: dict[str, float] = {}
            zero_accounts: set[str] = set()
            quota_target_count = 0
            quota_read_error_count = 0
            quota_invalid_count = 0
            snapshot = None
            if callable(getattr(source, "snapshot", None)):
                snapshot = source.snapshot()
                targets = source.quota_targets(
                    exclude=context.excluded_accounts,
                    snapshot=snapshot,
                )
                quota_target_count = len(targets)
                complete = bool(targets)
                quota_left = {}
                try:
                    from lingtai.llm.openai.codex_quota import read_remaining_percent
                    for auth_ref, auth_sha8 in targets:
                        try:
                            percent = read_remaining_percent(auth_ref)
                        except Exception:
                            quota_read_error_count += 1
                            complete = False
                            continue
                        if not self._valid_codex_quota_percent(percent):
                            quota_invalid_count += 1
                            complete = False
                            continue
                        fraction = float(percent) / 100.0
                        observed_quota[auth_sha8] = fraction
                        quota_left[auth_sha8] = fraction
                        if fraction <= 0.0:
                            zero_accounts.add(auth_sha8)
                except Exception:
                    complete = False
                if not complete:
                    quota_left = None

            excluded = context.excluded_accounts | zero_accounts
            try:
                if snapshot is None:
                    candidate = source.select(exclude=excluded or None)
                    pool_size = 1
                else:
                    candidate = source.select(
                        exclude=excluded or None,
                        quota_left_snapshot=quota_left,
                        snapshot=snapshot,
                    )
                    pool_size = len(snapshot)
            except Exception as exc:
                # Only a truly empty configured pool may use the legacy account.
                if (
                    self._codex_fallback_auth_path is None
                    or snapshot is None
                    or not isinstance(snapshot, (list, tuple))
                    or snapshot
                ):
                    from lingtai.auth.codex_account_source import NoCandidateError

                    if isinstance(exc, NoCandidateError):
                        diagnostics = self._codex_no_candidate_diagnostics(
                            snapshot=snapshot,
                            context=context,
                            zero_accounts=zero_accounts,
                            quota_target_count=quota_target_count,
                            quota_observed_count=len(observed_quota),
                            quota_read_error_count=quota_read_error_count,
                            quota_invalid_count=quota_invalid_count,
                            quota_left=quota_left,
                            fallback_auth_path=self._codex_fallback_auth_path,
                        )
                        raise exc.with_diagnostics(diagnostics) from exc
                    raise
                from lingtai.auth.codex_account_source import FixedAccountSource
                fallback = FixedAccountSource(self._codex_fallback_auth_path)
                candidate = fallback.select()
                pool_size = 1
                quota_left = None
                selection_fallback = "legacy_default"
            else:
                selection_fallback = None

            manager = self._new_codex_token_manager(candidate.auth_ref)
            access_token = manager.get_access_token()
            account_id = manager.get_account_id()
            auth_source = "configured" if selection_fallback is None else selection_fallback
            selection: dict[str, Any] = {
                "source_ref": candidate.source_ref,
                "source_index": candidate.source_index,
                "pool_size": pool_size,
                "weight": candidate.weight,
                "auth_path_sha8": candidate.auth_path_sha8,
                "model_scope": model if pool_size > 1 else None,
            }
            fraction = (quota_left or observed_quota).get(candidate.auth_path_sha8)
            if fraction is not None:
                selection["quota_left"] = round(fraction * 100.0, 3)
            if selection_fallback is not None:
                selection["fallback"] = selection_fallback
            context.client.api_key = access_token
            binding = self._set_codex_account_binding(
                context,
                {
                    "api_key": access_token,
                    "account_id": account_id,
                    "auth_path_sha8": candidate.auth_path_sha8,
                    "auth_path_source": auth_source,
                    "auth_ref": candidate.auth_ref,
                    "selection": dict(selection),
                },
            )
            context.bound_molt_count = self._current_molt_count()
            if legacy_direct:
                self._publish_legacy_codex_binding(binding)
            return binding

    def _codex_account_request(
        self,
        context: _CodexAccountContext,
        apply_binding: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Select and publish this context's sticky account under one lock."""
        with context.lock:
            current_molt = self._current_molt_count()
            if context.binding and context.bound_molt_count != current_molt:
                self._clear_codex_account_binding(context)
            if not context.binding:
                binding = self._select_codex_account(context)
            else:
                binding = self._refresh_codex_bound_quota(context)
            if apply_binding is not None:
                apply_binding(binding)
            return binding

    def _codex_token_expired(
        self,
        context: _CodexAccountContext,
        exc: Exception,
        expected_generation: int | None,
        rejected_access_token: str | None,
        expected_auth_path_sha8: str | None,
        apply_binding: Callable[[dict[str, Any]], None] | None = None,
        retry_request: Callable[[], Any] | None = None,
    ) -> Any | None:
        """Compare, publish, and optionally create one retry under ownership."""
        from lingtai.auth.codex import _is_token_expired_error

        if (
            not _is_token_expired_error(exc)
            or not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or not rejected_access_token
            or not expected_auth_path_sha8
        ):
            return None

        with context.lock:
            current = dict(context.binding)
            same_account = (
                current.get("auth_path_sha8") == expected_auth_path_sha8
            )
            if not same_account:
                # An approved boundary selected another account after this request
                # started. The late failure owns neither that binding nor a retry.
                return None
            if current.get("binding_generation") != expected_generation:
                # A same-account peer already replaced the rejected generation.
                # Reuse only that newer credential; never clear it or refresh again.
                current_token = current.get("api_key")
                if not current_token or current_token == rejected_access_token:
                    return None
                recovered = current
            elif current.get("api_key") != rejected_access_token:
                recovered = current
            else:
                auth_ref = current.get("auth_ref")
                if not isinstance(auth_ref, str) or not auth_ref:
                    return None
                manager = self._new_codex_token_manager(auth_ref)
                access_token = manager.refresh_access_token(rejected_access_token)
                account_id = manager.get_account_id()

                refreshed = dict(current)
                refreshed["api_key"] = access_token
                refreshed["account_id"] = account_id
                context.client.api_key = access_token
                recovered = self._set_codex_account_binding(context, refreshed)

            # The comparison, REST/WS/header/epoch publication, and retry wire
            # creation are one context-owned transaction. A newer owner cannot
            # interleave a different client key with this request's account header.
            if apply_binding is not None:
                apply_binding(recovered)
            if retry_request is not None:
                return retry_request()
            return recovered

    def _codex_account_error(
        self, context: _CodexAccountContext, exc: Exception, partial_output: bool
    ) -> None:
        # The session owns replay-safe partial marking before this callback.
        # Partial output must not mutate account exclusion state.
        if partial_output:
            return
        from lingtai.auth.codex import _is_usage_limit_reached_error
        if _is_usage_limit_reached_error(exc):
            with context.lock:
                identity = context.selection.get("auth_path_sha8")
                if identity:
                    context.excluded_accounts.add(str(identity))
                # AED rebuild/replay may reuse this interface/context and will
                # draw from the remaining candidates without touching other chats.
                self._clear_codex_account_binding(context)

    def _codex_account_success(self, context: _CodexAccountContext) -> None:
        with context.lock:
            context.excluded_accounts.clear()

    def generate(self, *args, **kwargs) -> LLMResponse:
        """Run one-shot generation through the native Codex request builder."""
        model = kwargs.get("model") or (args[0] if args else "")
        contents = kwargs.get("contents")
        if contents is None and len(args) > 1:
            contents = args[1]
        if contents is None:
            raise TypeError("Codex generate() requires contents")
        if not self._should_use_responses():
            raise RuntimeError("Codex one-shot generation requires the Responses API")

        self._repoint_client_if_needed(self._select_codex_endpoint())
        system_prompt = kwargs.get("system_prompt")
        interface = ChatInterface()
        if system_prompt:
            interface.add_system(system_prompt)
        session = self._create_responses_session(
            str(model),
            system_prompt or "",
            interface=interface,
        )
        if kwargs.get("temperature") is not None:
            session._extra_kwargs["temperature"] = kwargs["temperature"]
        if kwargs.get("max_output_tokens") is not None:
            session._extra_kwargs["max_output_tokens"] = kwargs["max_output_tokens"]
        json_schema = kwargs.get("json_schema")
        if json_schema is not None:
            session._extra_kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": json_schema.get("title", "response"),
                    "schema": json_schema,
                    "strict": True,
                }
            }
        one_shot_input = (
            [{"role": "user", "content": contents}]
            if isinstance(contents, list)
            else contents
        )
        return self._gated_call(lambda: session.send(one_shot_input))

    def _current_molt_count(self) -> int:
        """Current molt_count: explicit override, else ``.agent.json``, else 0."""
        if self._codex_molt_count_override is not None:
            try:
                return int(self._codex_molt_count_override)
            except (TypeError, ValueError):
                return 0
        if self._codex_agent_json_path is not None:
            return _read_molt_count(self._codex_agent_json_path)
        return 0

    def _select_codex_endpoint(self) -> str | None:
        """Pick this request's Codex endpoint from the pool (or the fixed one).

        - Empty pool   -> the single ``base_url`` resolved at construction
                          (``None`` means the official endpoint, set by the
                          factory; kept verbatim).
        - 1 valid entry -> always that entry.
        - 2+ entries   -> ``pool[(offset + molt_count) % len]`` — stable while
                          molt_count is unchanged, rotates to an adjacent slot at
                          each molt boundary (so adjacent molts actually move).
        """
        pool = self._codex_base_urls
        if not pool:
            return self._codex_fixed_base_url
        if len(pool) == 1:
            return pool[0]
        idx = (self._codex_pool_offset + self._current_molt_count()) % len(pool)
        return pool[idx]

    def _repoint_client_if_needed(self, endpoint: str | None) -> None:
        """Re-point the OpenAI client at ``endpoint`` if it changed.

        Called at request time (``create_chat``) so a molt-driven endpoint
        change takes effect on a LIVE adapter without a service rebuild. The old
        client is replaced wholesale: any websocket / ``previous_response_id`` /
        continuation state owned by sessions built against the previous endpoint
        is on the old client object and is dropped, so it can never cross
        endpoints. The cache identity is untouched (it is endpoint-independent).

        The Codex factory's pre-call OAuth refresh hook mutates
        ``self._client.api_key`` in place each turn; carry that LIVE token onto
        the rebuilt client (and into ``_client_kwargs``, which the session reads
        for the WS path) so a switch never reverts to the stale boot token.
        """
        if endpoint == self.base_url:
            return
        live_api_key = getattr(self._client, "api_key", None)
        self.base_url = endpoint
        new_kwargs = dict(self._client_kwargs)
        if live_api_key is not None:
            new_kwargs["api_key"] = live_api_key
        if endpoint:
            new_kwargs["base_url"] = endpoint
        else:
            new_kwargs.pop("base_url", None)
        self._client_kwargs = new_kwargs
        self._client = openai.OpenAI(**new_kwargs)

    def create_chat(self, *args, **kwargs) -> ChatSession:
        # Chat construction owns only the ordinary Codex session/interface and
        # endpoint. AccountSource is consulted later, immediately before each
        # actual provider request.
        self._repoint_client_if_needed(self._select_codex_endpoint())
        return super().create_chat(*args, **kwargs)

    def _current_codex_id(self) -> str | None:
        """The current effective Codex cache-affinity id, or ``None``.

        Computed FRESH on every call (never cached at construction) so a molt —
        which advances ``.agent.json`` ``molt_count`` WITHOUT rebuilding the
        adapter — changes the outgoing id on the next request:

          * an anchor -> ``hash(anchor, current molt_count)``, stable within a
            molt segment and changing at each molt boundary;
          * no anchor -> ``None`` (bare/test adapter: no per-agent identity).
        """
        if self._codex_session_anchor:
            return _codex_session_id(
                self._codex_session_anchor, self._current_molt_count()
            )
        return None

    def static_adapter_comment(self):
        """Codex adapter comments are intentionally disabled before chat creation."""
        return None

    def _resolve_codex_ids(self, model: str) -> tuple[str | None, str | None]:
        """Resolve the (session_id, thread_id) headers for ``model``.

        Returns ``(None, None)`` only when no per-agent identity was passed in
        (e.g. a bare adapter built directly in a test). In the normal host path
        the agent path is always supplied, so both ids are the same per-agent
        hash of ``(anchor, current molt_count)`` — the thread id tracks the
        session id exactly. Computed at request time, so a molt that advances
        ``.agent.json`` ``molt_count`` changes both ids on the next request even
        though molt does not rebuild the adapter.

        Both ids are sent on every request because the official Codex CLI source
        path depends on a consistent ``session_id`` / ``thread_id`` /
        ``prompt_cache_key`` identity: the websocket incremental
        ``previous_response_id`` path and per-turn ``x-codex-turn-state`` sticky
        routing all ride on top of the session/thread (see
        ``codex-rs/core/src/client.rs:863-864, 873`` — ``build_session_headers``
        with both ids on every request). Dropping the headers would defeat the
        official path this experiment is mirroring.
        """
        current = self._current_codex_id()
        return current, current

    def _default_prompt_cache_key(self, model: str) -> str:
        # On the normal/root path the cache key is the SAME per-agent value as
        # session_id / thread_id — byte-identical, so all three cache-affinity
        # levers point at one slot. The value is derived from the agent path AND
        # the current molt_count, so it is stable within a molt segment and moves
        # at each molt boundary. Computed FRESH here (not a stale id stored at
        # construction) so a live molt_count change is reflected without an
        # adapter rebuild. Never paired with `prompt_cache_retention` (Codex
        # rejects it).
        #
        # The model-keyed ``lingtai-codex:{model}:v1`` form survives only for the
        # truly bare/no-anchor path (e.g. a standalone unit test), where the
        # adapter has no per-agent identity to hash.
        current = self._current_codex_id()
        if current:
            return current
        return f"lingtai-codex:{model}:v1"

    def _create_responses_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        interface: ChatInterface | None = None,
        thinking: str = "default",
        context_window: int = 0,
    ) -> CodexResponsesSession:
        if interface is None:
            interface = ChatInterface()
            interface.add_system(system_prompt, tools=FunctionSchema.list_to_dicts(tools))

        context = self._new_codex_account_context(model, interface)
        openai_tools = _apply_site_quirks(self.base_url, _build_responses_tools(tools))
        tool_choice: str | None = None
        if force_tool_call and openai_tools:
            tool_choice = "required"

        extra_kwargs: dict[str, Any] = {}

        if json_schema is not None:
            extra_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        # Omitted/``default`` thinking sends an explicit
        # ``reasoning.effort = "xhigh"`` instead of omitting the field
        # (omitting it would fall back to the Codex backend's own, lower
        # default). Explicit levels pass through unchanged; the generic OpenAI
        # Responses path shares the same explicit ``xhigh`` default (see
        # ``_responses_reasoning_kwargs``).
        if thinking in (None, "default"):
            thinking = "xhigh"
        extra_kwargs.update(_responses_reasoning_kwargs(thinking))

        # Codex's backend doesn't accept context_management compaction —
        # leave compact_threshold unset.
        # service_tier: common Codex capability (REST + WS).  Omitted when None.
        if self._codex_service_tier is not None:
            extra_kwargs["service_tier"] = self._codex_service_tier

        session_id, thread_id = self._resolve_codex_ids(model)
        return CodexResponsesSession(
            client=context.client,
            model=model,
            instructions=system_prompt,
            tools=openai_tools,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            previous_response_id=None,
            compact_threshold=None,
            interface=interface,
            # On the normal/root path this resolves to the SAME per-agent
            # (anchor, molt_count) hash as session_id / thread_id (see
            # ``_default_prompt_cache_key``); it honors an explicit override or a
            # ``prompt_cache_key=False`` disable passed to the adapter. Only the
            # bare/no-anchor path falls back to ``lingtai-codex:{model}:v1``.
            prompt_cache_key=self._resolve_prompt_cache_key(model),
            context_window=context_window,
            # REST cache-affinity headers: both the per-agent (anchor, molt_count)
            # hash, byte-identical, passed down by the host; ``(None, None)`` only
            # for a bare/test adapter. Stable within a molt segment, refreshed at
            # each molt boundary (resolved fresh per request above).
            session_id=session_id,
            thread_id=thread_id,
            # The user's own ChatGPT account id (read fresh from the adapter so a
            # token refresh that changes it is reflected on newly built sessions).
            account_id=self.codex_account_id,
            codex_auth_path_sha8=self.codex_auth_path_sha8,
            codex_auth_path_source=self.codex_auth_path_source,
            codex_pool_selection=context.selection,
            codex_account_error_callback=(
                (lambda exc, partial: self._codex_account_error(context, exc, partial))
                if self._codex_account_resolution_enabled
                else None
            ),
            codex_account_success_callback=(
                (lambda: self._codex_account_success(context))
                if self._codex_account_resolution_enabled
                else None
            ),
            codex_account_request_callback=(
                (lambda apply: self._codex_account_request(context, apply))
                if self._codex_account_resolution_enabled
                else None
            ),
            codex_token_expired_callback=(
                (
                    lambda exc, generation, token, identity, apply, retry: self._codex_token_expired(
                        context, exc, generation, token, identity, apply, retry
                    )
                )
                if self._codex_account_resolution_enabled
                else None
            ),
            codex_account_epoch_reset_callback=(
                (lambda reason: self._codex_account_epoch_reset(context, reason))
                if self._codex_account_resolution_enabled
                else None
            ),
            installation_id=self._codex_installation_id,
            base_url=self.base_url,
            api_key=getattr(context.client, "api_key", None),
            compact_token_limit=self._codex_compact_token_limit,
            # The runtime-effort active route for THIS session (issue #1197).
            # Resolved per session from the exact model, the endpoint this
            # session will actually talk to, AND the effort it is actually being
            # constructed with — read back from ``extra_kwargs`` so it is the
            # already-normalized wire value (the omitted/``default`` sentinel has
            # become ``xhigh`` above; an explicit ``high`` stays ``high``). That
            # value becomes the capability baseline, so binding a controller can
            # never move an otherwise-unchanged request. ``None`` off that route
            # — or for a construction effort this route does not authorize —
            # leaves every request byte-identical to today.
            effort_descriptor=resolve_codex_effort_descriptor(
                model=model,
                base_url=self.base_url,
                construction_effort=(extra_kwargs.get("reasoning") or {}).get("effort"),
            ),
        )
