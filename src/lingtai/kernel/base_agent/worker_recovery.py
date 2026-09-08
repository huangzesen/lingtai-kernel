"""WorkerStillRunning recovery helpers.

These helpers keep the fail-closed safety rule in one place: once a worker
thread is still running after timeout + grace, the live ChatInterface is
poisoned for this process and recovery must happen from durable on-disk state
after refresh/relaunch.

The artifact also carries a compact ``redo`` block so the relaunched process
can redo the interrupted turn (never the poisoned in-process interface):
``pending -> provider_started -> completed | abandoned``, or born
``unavailable``. A pending redo is enqueued once per boot; ``provider_started``
is persisted immediately before the fresh process's provider call, after which
a later boot fails closed. At-most-once per recorded provider start, not
exactly-once end to end. Incident: Runyuan 2026-09-08 (300 s timeout ->
poison -> relaunch that never redid the call).

Design reference: Lingtai-AI/lingtai-kernel#298 (rebuilt for current main).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..message import (
    MSG_CORRELATED_TURN,
    MSG_REQUEST,
    MSG_TC_WAKE,
    MSG_USER_INPUT,
    Message,
)
from ..trace_redaction import redact_text


MAX_PREVIEW_CHARS = 500
_ARTIFACT_GLOB = "worker_still_running_*.json"
# Artifact filenames are derived only from this id shape (fixed containment).
_ARTIFACT_ID_RE = re.compile(r"^worker_still_running_\d{8}T\d{6}Z_[A-Za-z0-9-]{1,32}$")
# A `request` redo carries the exact original text; bound both characters and
# UTF-8 bytes, and report anything larger as unavailable rather than truncate.
MAX_REDO_CONTENT_CHARS = 200_000
MAX_REDO_CONTENT_BYTES = 1_000_000
_REDO_TERMINAL = frozenset({"completed", "abandoned", "unavailable"})
_ISSUE_REFS = ["Lingtai-AI/lingtai-kernel#195", "Lingtai-AI/lingtai-kernel#238"]
_SAFETY_INVARIANT = (
    "Worker future still alive after timeout + grace; poisoned ChatInterface "
    "must not be retried, healed, serialized, or saved."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def _fsync_directory(directory: Path, *, os_name: str | None = None) -> None:
    """Durability barrier for a directory-entry replacement.

    POSIX directory descriptors carry the barrier: open read-only, fsync,
    close (same pattern as ``tools/bash/_async_supervisor._write_state_atomic``).
    Windows cannot ``os.open`` a directory; the file fsync plus atomic
    replace is the portable boundary there. Errors propagate.
    """
    if (os_name or os.name) != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Restrictive, durable artifact write (the artifact may carry replay text).

    ``0700`` directory, exclusive random ``0600`` temp file, fsync, atomic
    replace, then the parent-directory barrier (``_fsync_directory``) so a
    transition such as ``provider_started`` is durable before the caller acts
    on it. Raises on failure; callers decide how to report it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


_LOCK_CREATE = threading.Lock()


def _lock(agent) -> threading.RLock:
    """Serialize artifact transitions across this process's threads (boot,
    notification dismissal, run loop); the workdir lease excludes processes."""
    lock = getattr(agent, "_worker_hang_recovery_lock", None)
    if lock is None:
        with _LOCK_CREATE:
            lock = getattr(agent, "_worker_hang_recovery_lock", None) or threading.RLock()
            agent._worker_hang_recovery_lock = lock
    return lock


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _artifact_id_from_relpath(artifact_relpath: str | None) -> str:
    if not artifact_relpath:
        return "unknown"
    return Path(artifact_relpath).stem


def _artifact_ref_id(artifact_relpath: str | None) -> str:
    return f"worker_still_running:{_artifact_id_from_relpath(artifact_relpath)}"


def _message_preview(msg: Message) -> dict:
    """Bounded, redacted preview of a request message.

    Captures length + content hash for provenance, but only a redacted,
    truncated prefix of the body — never the raw prompt.
    """
    raw = _safe_text(getattr(msg, "content", ""))
    redacted = redact_text(raw)
    return {
        "content_chars": len(raw),
        "content_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "content_preview_redacted": redacted[:MAX_PREVIEW_CHARS],
    }


def _collect_notification_metadata(agent) -> dict:
    """Safe metadata about live notifications (sources + ref ids only)."""
    try:
        from ..notifications import _get_allow_predicate, _workdir_key

        store = agent._notification_store
        allow = _get_allow_predicate(workdir=_workdir_key(agent))
        notifications = store.snapshot(allow)
    except Exception:
        return {"notification_sources": [], "notification_ref_ids": []}

    ref_ids: list[str] = []
    for payload in notifications.values():
        if not isinstance(payload, dict):
            continue
        candidates = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)
            events = data.get("events")
            if isinstance(events, list):
                candidates.extend(ev for ev in events if isinstance(ev, dict))
        for candidate in candidates:
            for key in ("ref_id", "event_id", "id"):
                value = candidate.get(key)
                if value is not None:
                    ref_ids.append(str(value)[:200])
    return {
        "notification_sources": sorted(str(k) for k in notifications.keys()),
        "notification_ref_ids": ref_ids[:20],
    }


def _plan_redo(agent, msg: Message, *, prior_attempt: int, request_persisted: bool) -> dict:
    """The compact ``redo`` block: how the fresh process may redo this turn.

    ``request`` — the first round-trip never reached durable history, so the
    exact bounded text is the only faithful redo; ``continuation`` — the
    round-trip was saved, so the redo is the localized ``system.stuck_revive``
    AED request (no request copy); ``tc_wake`` — the notification pair was
    saved before ``MSG_TC_WAKE``, so an explicit ``MSG_TC_WAKE`` with the
    original id redrives the ready wire; ``unavailable`` — correlated turn
    (its caller already received terminal settlement), budget, size, type.
    """
    try:
        max_attempts = max(int(getattr(agent._config, "max_aed_attempts", 3) or 3), 1)
    except Exception:
        max_attempts = 3
    attempt = int(prior_attempt or 0) + 1
    kind = getattr(msg, "type", "unknown")
    redo: dict[str, Any] = {
        "status": "unavailable", "mode": "unavailable", "reason": None,
        "attempt": attempt, "max_attempts": max_attempts,
    }
    message = {
        "type": kind,
        "sender": str(getattr(msg, "sender", ""))[:200],
        "id": str(getattr(msg, "id", "") or "")[:64],
    }
    content = getattr(msg, "content", None)
    if attempt > max_attempts:
        redo["reason"] = "aed_redo_budget_exhausted"
    elif kind == MSG_CORRELATED_TURN:
        redo["reason"] = "correlated_turn_settled"
    elif kind in (MSG_REQUEST, MSG_USER_INPUT, MSG_TC_WAKE) and request_persisted:
        redo.update(status="pending", mode="continuation", message=message)
    elif kind == MSG_TC_WAKE:
        redo.update(status="pending", mode="tc_wake", message=message)
    elif kind not in (MSG_REQUEST, MSG_USER_INPUT):
        redo["reason"] = "unknown_entry"
    elif not isinstance(content, str):
        redo["reason"] = "non_text_request"
    elif len(content) > MAX_REDO_CONTENT_CHARS or len(content.encode("utf-8")) > MAX_REDO_CONTENT_BYTES:
        redo["reason"] = "request_too_large"
    else:
        message.update(content=content, content_chars=len(content), content_sha256=_sha256(content))
        redo.update(status="pending", mode="request", message=message)
    return redo


def build_worker_hang_context(
    agent,
    msg: Message,
    exc: BaseException,
    *,
    prior_attempt: int = 0,
    request_persisted: bool = False,
) -> dict:
    """Collect only safe, bounded turn/error metadata for the artifact.

    Reads from the message and exception locals — never from the poisoned
    ChatInterface — so it cannot race the still-alive worker thread.
    """
    message_type = getattr(msg, "type", "unknown")
    if message_type in (MSG_REQUEST, MSG_USER_INPUT, MSG_CORRELATED_TURN):
        entry = "request"
    elif message_type == MSG_TC_WAKE:
        entry = "tc_wake_wire"
    else:
        entry = "unknown"

    context = {
        "turn": {
            "entry": entry,
            "message_type": message_type,
            "sender": str(getattr(msg, "sender", ""))[:200],
        },
        "error": {
            "class": type(exc).__name__,
            "message": (str(exc) or repr(exc))[:500],
            "elapsed_s": getattr(exc, "elapsed", None),
            "grace_s": getattr(exc, "grace", None),
            "agent_name": getattr(exc, "agent_name", getattr(agent, "agent_name", None)),
        },
    }
    if message_type in (MSG_REQUEST, MSG_USER_INPUT, MSG_CORRELATED_TURN):
        context["request"] = _message_preview(msg)
    elif message_type == MSG_TC_WAKE:
        context["tc_wake"] = {
            "mode": "wire_drive",
            **_collect_notification_metadata(agent),
        }
    context["redo"] = _plan_redo(
        agent, msg, prior_attempt=prior_attempt, request_persisted=request_persisted
    )
    return context


def write_worker_hang_artifact(agent, exc: BaseException, context: dict) -> str | None:
    """Write the bounded/redacted unfinished-turn artifact.

    Returns the working-dir-relative path, or None on write failure.  The
    artifact intentionally contains NO raw chat history, tool args, or tool
    results — only the bounded/redacted previews built in ``context`` plus,
    for a ``request`` redo, the exact bounded request text under ``redo``
    (declared by ``privacy.redo_request_text_included``; stripped to
    length/hash on every terminal redo status).
    """
    created_at = _now_iso()
    artifact_id = f"worker_still_running_{_now_stamp()}_{secrets.token_hex(3)}"
    relpath = f"history/unfinished_turns/{artifact_id}.json"
    path = agent._working_dir / relpath
    redo = context.get("redo") or {
        "status": "unavailable", "mode": "unavailable", "reason": "message_unavailable",
    }
    payload = {
        "schema_version": 1,
        "type": "worker_still_running_recovery",
        "status": "open",
        "created_at": created_at,
        "issue_refs": _ISSUE_REFS,
        "safety_invariant": _SAFETY_INVARIANT,
        "error": context.get("error", {}),
        "turn": context.get("turn", {}),
        "recovery": {
            "poison_flag_set": True,
            "refresh_requested": True,
            "chat_history_saved_after_error": False,
            "notification_ref_id": f"worker_still_running:{artifact_id}",
        },
        "redo": redo,
        "privacy": {
            "raw_chat_history_included": False,
            "raw_tool_args_included": False,
            "raw_tool_results_included": False,
            "previews_redacted": True,
            "max_preview_chars": MAX_PREVIEW_CHARS,
            "redo_request_text_included": "content" in (redo.get("message") or {}),
        },
    }
    for key in ("request", "tc_wake", "predecessor_tools"):
        if key in context:
            payload[key] = context[key]
    try:
        _write_json_atomic(path, payload)
    except Exception as artifact_err:
        try:
            agent._log(
                "worker_hang_artifact_write_failed",
                error=(str(artifact_err) or repr(artifact_err))[:300],
            )
        except Exception:
            pass
        return None
    return relpath


def mark_worker_interface_poisoned(
    agent,
    exc: BaseException,
    *,
    context: dict | None = None,
    artifact_relpath: str | None = None,
) -> None:
    """Set process-local poison state on the agent.

    Process-local only — the flag lives in this Python process and is never
    persisted. The persisted recovery state is the artifact + notification.
    """
    context = context or {}
    poisoned_at = _now_iso()
    agent._llm_worker_interface_poisoned = True
    # Retain the detached provider Future as an execution-quiescence witness.
    # Correlated handle settlement cannot prove that this worker stopped touching
    # the shared ChatInterface, so lifecycle teardown must also wait/check it.
    agent._llm_worker_poison_future = getattr(exc, "future", None)
    agent._llm_worker_poison_reason = (str(exc) or repr(exc))[:500]
    agent._llm_worker_poison_artifact = artifact_relpath
    agent._llm_worker_poisoned_at = poisoned_at
    turn = context.get("turn") if isinstance(context.get("turn"), dict) else {}
    agent._llm_worker_poison_turn_entry = turn.get("entry")
    try:
        agent._log(
            "llm_worker_interface_poisoned",
            artifact=artifact_relpath,
            poisoned_at=poisoned_at,
            turn_entry=turn.get("entry"),
        )
    except Exception:
        pass


def is_worker_interface_poisoned(agent) -> bool:
    return bool(getattr(agent, "_llm_worker_interface_poisoned", False))


def publish_worker_hang_notification(
    agent,
    artifact_relpath: str | None,
    context: dict | None = None,
) -> str | None:
    """Publish a high-priority `kernel.llm_worker_hang` system notification.

    Idempotent on ref_id — if an event for this artifact already exists,
    returns None instead of re-publishing.
    """
    context = context or {}
    ref_id = _artifact_ref_id(artifact_relpath)
    turn = context.get("turn") if isinstance(context.get("turn"), dict) else {}
    error = context.get("error") if isinstance(context.get("error"), dict) else {}
    body = (
        "Previous LLM worker exceeded timeout plus grace and the interface was "
        "poisoned. Kernel skipped unsafe chat save and requested refresh. "
        f"Recovery artifact: {artifact_relpath or 'unavailable'}. Continue only "
        "from restored history/artifact; do not assume the abandoned LLM response exists."
    )
    extra = {
        "severity": "high",
        "artifact": artifact_relpath,
        "turn_entry": turn.get("entry"),
        "elapsed_s": error.get("elapsed_s"),
        "grace_s": error.get("grace_s"),
        "recommended_action": "wait_for_refresh_then_continue_from_restored_history",
    }
    try:
        enqueue = getattr(agent, "_enqueue_system_notification")
        event_id = enqueue(
            source="kernel.llm_worker_hang",
            ref_id=ref_id,
            body=body,
            priority="high",
            extra=extra,
            skip_if_ref_id_exists=True,
        )
        return event_id or None
    except Exception as notif_err:
        try:
            agent._log(
                "worker_hang_notification_publish_failed",
                ref_id=ref_id,
                error=(str(notif_err) or repr(notif_err))[:300],
            )
        except Exception:
            pass
        return None


def request_worker_hang_refresh(
    agent,
    *,
    artifact_relpath: str | None = None,
    source: str,
) -> None:
    """Idempotently request a forced refresh that skips poisoned chat save."""
    if getattr(agent, "_llm_worker_refresh_requested", False):
        try:
            agent._log(
                "worker_hang_refresh_already_requested",
                source=source,
                artifact=artifact_relpath or getattr(agent, "_llm_worker_poison_artifact", None),
            )
        except Exception:
            pass
        return
    agent._llm_worker_refresh_requested = True
    agent._llm_worker_refresh_source = source
    try:
        agent._log(
            "worker_hang_refresh_requested",
            source=source,
            artifact=artifact_relpath,
        )
    except Exception:
        pass
    try:
        agent._perform_refresh(
            skip_chat_history_save=True,
            skip_save_reason="worker_still_running_interface_unsafe",
        )
    except Exception as refresh_err:
        try:
            agent._log(
                "worker_hang_refresh_request_failed",
                source=source,
                artifact=artifact_relpath,
                error=(str(refresh_err) or repr(refresh_err))[:300],
            )
        except Exception:
            pass


def _open_artifacts(agent) -> list[tuple[str, Path, dict]]:
    """Return open (unresolved) recovery artifacts, newest first."""
    directory = agent._working_dir / "history" / "unfinished_turns"
    if not directory.is_dir():
        return []
    out: list[tuple[str, Path, dict]] = []
    for path in directory.glob(_ARTIFACT_GLOB):
        if not _ARTIFACT_ID_RE.match(path.stem):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("status") != "open" or payload.get("resolved_at"):
            continue
        created_at = str(payload.get("created_at") or "")
        out.append((created_at, path, payload))
    return sorted(out, key=lambda item: (item[0], item[1].name), reverse=True)


def is_worker_hang_ref(ref_id: str | None) -> bool:
    """Return True iff *ref_id* names a worker_still_running recovery event."""
    return isinstance(ref_id, str) and ref_id.startswith("worker_still_running:")


def resolve_worker_hang_artifact(agent, ref_id: str, *, reason: str = "") -> bool:
    """Durably mark the recovery artifact behind *ref_id* as resolved.

    The dismissed system event is only the transient surface. The artifact under
    history/unfinished_turns is the rehydration source, so it must be marked
    handled without being deleted.
    """
    if not is_worker_hang_ref(ref_id):
        return False
    directory = agent._working_dir / "history" / "unfinished_turns"
    if not directory.is_dir():
        return False
    with _lock(agent):
        return _resolve_locked(agent, directory, ref_id, reason)


def _resolve_locked(agent, directory: Path, ref_id: str, reason: str) -> bool:
    for path in sorted(directory.glob(_ARTIFACT_GLOB)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        recovery = payload.get("recovery")
        artifact_ref = (
            recovery.get("notification_ref_id") if isinstance(recovery, dict) else None
        ) or _artifact_ref_id(path.name)
        if artifact_ref != ref_id:
            continue
        if payload.get("status") != "open" or payload.get("resolved_at"):
            return False
        payload["status"] = "resolved"
        payload["resolved_at"] = _now_iso()
        payload["resolved_reason"] = (str(reason) or "dismissed")[:500]
        # A dismissed recovery must never replay later: a still-pending redo
        # is abandoned in the same write. An in-flight one (provider_started)
        # is left for the run loop to settle.
        if (payload.get("redo") or {}).get("status") == "pending":
            _finish_redo(payload, "abandoned", reason="artifact_resolved")
        try:
            _write_json_atomic(path, payload)
        except Exception as write_err:
            try:
                agent._log(
                    "worker_hang_resolve_failed",
                    ref_id=ref_id,
                    error=(str(write_err) or repr(write_err))[:300],
                )
            except Exception:
                pass
            return False
        try:
            agent._log(
                "worker_hang_artifact_resolved",
                ref_id=ref_id,
                artifact=path.name,
                reason=payload["resolved_reason"],
            )
        except Exception:
            pass
        return True
    return False


def rehydrate_worker_hang_recovery(agent) -> int:
    """On startup, re-surface the newest open artifact as a notification.

    Returns the number of notifications published (0 or 1).  Idempotent on
    ref_id so a relaunch loop does not stack duplicate events.
    """
    artifacts = _open_artifacts(agent)
    if not artifacts:
        return 0
    _created_at, path, payload = artifacts[0]
    try:
        artifact_relpath = path.relative_to(agent._working_dir).as_posix()
    except ValueError:
        artifact_relpath = str(path)
    ref_id = payload.get("recovery", {}).get("notification_ref_id") or _artifact_ref_id(artifact_relpath)
    event_id = publish_worker_hang_notification(agent, artifact_relpath, payload)
    return 1 if event_id else 0


def _pending_recovery_prompt_artifacts(agent) -> list[tuple[str, Path, dict]]:
    """Open artifacts whose one-shot recovery notice has not been delivered."""
    return [
        item for item in _open_artifacts(agent)
        if not item[2].get("prompt_injected_at")
    ]


def has_pending_worker_hang_recovery_prompt(agent) -> bool:
    """Return True iff a poison recovery is still pending its one-shot notice.

    Read-only; exactly the predicate ``maybe_prepend_worker_hang_recovery_prompt``
    fires on. This is the durable discriminator between a relaunch that exists
    only to discard a poisoned in-process interface and an ordinary user/System
    refresh: the artifact is written before the forced refresh and is marked
    ``prompt_injected_at`` only by the next safe text request in the fresh
    process. Any read failure answers ``False`` (treat as ordinary), so callers
    fail toward the normal wake path, never toward silence.
    """
    try:
        return bool(_pending_recovery_prompt_artifacts(agent))
    except Exception:
        return False


def baseline_notifications_for_pending_worker_recovery(agent) -> bool:
    """Keep a poison-recovery relaunch ASLEEP through its first notification sync.

    Called by ``lifecycle._start`` after ``rehydrate_worker_hang_recovery`` and
    before the heartbeat is allowed to sync. A fresh process starts with an
    empty ``_notification_fp``, so the very first sync would read every
    already-present channel — including the rehydrated ``kernel.llm_worker_hang``
    event — as a change and wake the agent (ASLEEP → IDLE + ``MSG_TC_WAKE``)
    with no new external input. The relaunch exists only to discard the unsafe
    interface; the new logical agent must wait for a genuinely later wake.

    While a pending recovery exists, take one coherent observation the same
    way ``_sync_notifications`` does and, only when that observation is
    *solely* the worker-hang recovery event, seed both the masked
    (wake-deciding) and raw fingerprints to it. Nothing is dismissed, cleared,
    hidden, or delivered: the next real notification change differs from this
    baseline and wakes with the full current payload, worker-hang event
    included.

    Anything else already pending at boot is a real wake that must be
    delivered on the first tick, not deferred: an external channel (mail,
    Telegram, ...) that arrived during the old process's hang, a system event
    that is not a WorkerStillRunning reference, or any masked entry beyond the
    virtual quiet baseline. In that case, as for an unstable read or a Store
    failure, nothing is seeded (the heartbeat's own sync then decides, i.e.
    fails toward waking) and the reason is logged.

    Returns True iff the baseline was seeded.
    """
    if not has_pending_worker_hang_recovery_prompt(agent):
        return False
    try:
        from ..notifications import (
            _system_events,
            _workdir_key,
            coherent_attention_read,
            is_channel_allowed,
            masked_empty_attention_fp,
            sync_hook_registry,
        )

        workdir = _workdir_key(agent)
        # Same channel view as `_sync_notifications`: registered hook channels
        # must be part of the observation, or the first tick would see a
        # different fingerprint and wake anyway. The consumer-delay timer is
        # deliberately NOT armed here — the run loop does not exist yet and
        # `coherent_attention_read` already reconciles an elapsed persisted
        # delay; the ordinary first heartbeat arms the timer.
        sync_hook_registry(agent)
        observed = coherent_attention_read(
            agent._notification_store,
            lambda channel: is_channel_allowed(channel, workdir=workdir),
            workdir,
        )
        quiet_baseline = masked_empty_attention_fp(workdir)
    except Exception as read_err:
        try:
            agent._log(
                "worker_hang_notification_baseline_skipped",
                reason="read_failed",
                error=(str(read_err) or repr(read_err))[:300],
            )
        except Exception:
            pass
        return False
    if not observed.stable:
        try:
            agent._log(
                "worker_hang_notification_baseline_skipped",
                reason="unstable_read",
            )
        except Exception:
            pass
        return False
    system_payload = observed.payloads.get("system")
    system_events = _system_events(system_payload)
    only_worker_hang = (
        set(observed.payloads.keys()) == {"system"}
        and isinstance(system_payload, dict)
        and bool(system_events)
        and all(
            isinstance(event, dict) and is_worker_hang_ref(event.get("ref_id"))
            for event in system_events
        )
        and tuple(
            entry for entry in observed.masked_fp
            if not (entry and entry[0] == "system.json")
        ) == tuple(quiet_baseline)
    )
    if not only_worker_hang:
        try:
            agent._log(
                "worker_hang_notification_baseline_skipped",
                reason="foreign_notifications_pending",
                channels=sorted(str(k) for k in observed.payloads.keys())[:20],
            )
        except Exception:
            pass
        return False
    agent._notification_fp = observed.masked_fp
    agent._notification_raw_fp = observed.raw_fp
    try:
        agent._log(
            "worker_hang_notification_baselined",
            channels=sorted(str(k) for k in observed.payloads.keys())[:20],
        )
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Fresh-process AED redo: boot claim/enqueue, provider-start mark, settlement
# ---------------------------------------------------------------------------


def _finish_redo(payload: dict, status: str, **fields) -> None:
    """Move the redo to ``status``; a terminal status strips the replay text."""
    redo = payload.setdefault("redo", {})
    redo["status"] = status
    redo.update(fields)
    if status in _REDO_TERMINAL:
        message = redo.get("message")
        if isinstance(message, dict) and "content" in message:
            del message["content"]
            message["content_stripped_at"] = _now_iso()
        payload.setdefault("privacy", {})["redo_request_text_included"] = False


def _invalid_redo(redo: dict) -> str | None:
    """Return a defect reason, or None when the pending redo may be replayed."""
    mode, message = redo.get("mode"), redo.get("message")
    if mode not in ("request", "continuation", "tc_wake") or not isinstance(message, dict):
        return "shape"
    mid = message.get("id")
    if not (isinstance(mid, str) and 0 < len(mid) <= 64 and mid.isprintable()):
        return "message_id"
    attempt, max_attempts = redo.get("attempt"), redo.get("max_attempts")
    if not (isinstance(attempt, int) and isinstance(max_attempts, int) and 1 <= attempt <= max_attempts):
        return "attempt"
    if mode == "request":
        content = message.get("content")
        if message.get("type") not in (MSG_REQUEST, MSG_USER_INPUT) or not isinstance(content, str):
            return "content_type"
        if len(content) > MAX_REDO_CONTENT_CHARS or len(content.encode("utf-8")) > MAX_REDO_CONTENT_BYTES:
            return "content_bounds"
        if message.get("content_chars") != len(content) or message.get("content_sha256") != _sha256(content):
            return "content_hash"
    return None


def _redo_message(agent, redo: dict) -> Message:
    message = redo["message"]
    mode = redo["mode"]
    if mode == "request":
        return Message(type=message["type"], sender=str(message.get("sender") or "user"),
                       content=message["content"], id=message["id"])
    if mode == "tc_wake":
        return Message(type=MSG_TC_WAKE, sender="system", content="", id=message["id"])
    from ..i18n import t as _t
    from ..time_veil import now_iso

    try:
        ts = now_iso(agent)
    except Exception:
        ts = _now_iso()
    language = getattr(getattr(agent, "_config", None), "language", "en") or "en"
    text = _t(language, "system.stuck_revive", ts=ts,
              err_desc="LLM worker still running after timeout plus grace")
    return Message(type=MSG_REQUEST, sender="system", content=text, id=message["id"])


def _artifact_by_id(agent, artifact_id: str) -> tuple[Path, dict] | None:
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.match(artifact_id):
        return None
    path = agent._working_dir / "history" / "unfinished_turns" / f"{artifact_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return (path, payload) if isinstance(payload, dict) else None


def redrive_worker_hang_redo(agent) -> str:
    """Enqueue the newest open artifact's pending redo once for this boot.

    Returns ``"enqueued"``, ``"none"``, or ``"error"`` (discovery failed; the
    host must not treat that as an ordinary refresh). A ``provider_started``
    redo found at boot means the previous process died after provider work
    may have begun: it is abandoned, never replayed, and stays visible through
    the open artifact and its notification. A defective block is abandoned as
    ``invalid:<reason>``. Nothing durable marks "enqueued": if this process
    dies before ``provider_started`` is persisted, the next boot enqueues again
    because no provider side effect began.
    """
    with _lock(agent):
        try:
            artifacts = _open_artifacts(agent)
        except Exception as err:
            _log(agent, "worker_hang_redo_discovery_failed", error=(str(err) or repr(err))[:300])
            return "error"
        if not artifacts:
            return "none"
        _created_at, path, payload = artifacts[0]
        redo = payload.get("redo")
        if not isinstance(redo, dict) or redo.get("status") in _REDO_TERMINAL:
            return "none"
        if redo.get("status") == "provider_started":
            reason = "provider_started_before_crash"
        elif redo.get("status") != "pending":
            reason = "invalid:status"
        else:
            defect = _invalid_redo(redo)
            reason = f"invalid:{defect}" if defect else None
        if reason:
            _finish_redo(payload, "abandoned", reason=reason)
            try:
                _write_json_atomic(path, payload)
            except Exception as err:
                _log(agent, "worker_hang_redo_mark_failed", artifact=path.name,
                     error=(str(err) or repr(err))[:300])
            _log(agent, "worker_hang_redo_skipped", reason=reason, artifact=path.name)
            return "none"
        message = _redo_message(agent, redo)
        agent._llm_worker_redo_in_flight = {
            "artifact_id": path.stem, "mode": redo["mode"], "attempt": redo["attempt"],
            "max_attempts": redo["max_attempts"], "message_id": message.id,
        }
        agent.inbox.put(message)
    wake = getattr(agent, "_wake_nap", None)
    if callable(wake):
        wake("worker_hang_redo")
    _log(agent, "worker_hang_redo_enqueued", mode=redo["mode"], attempt=redo["attempt"],
         max_attempts=redo["max_attempts"], message_type=message.type,
         message_id=message.id, artifact=path.name)
    return "enqueued"


def match_in_flight_worker_hang_redo(agent, msg: Message) -> dict | None:
    """Bind the in-flight redo to a dequeued turn by its stable message id."""
    in_flight = getattr(agent, "_llm_worker_redo_in_flight", None)
    bound = None
    if isinstance(in_flight, dict) and getattr(msg, "id", None) == in_flight.get("message_id"):
        bound = in_flight
    agent._llm_worker_redo_turn = bound
    return bound


def mark_worker_hang_redo_provider_started(agent) -> bool:
    """Persist ``provider_started`` for the bound redo immediately before the
    provider call. False means the record could not be written: the caller
    must fail closed and skip the provider call."""
    bound = getattr(agent, "_llm_worker_redo_turn", None)
    if not isinstance(bound, dict):
        return True
    with _lock(agent):
        loaded = _artifact_by_id(agent, bound.get("artifact_id"))
        status = (loaded[1].get("redo") or {}).get("status") if loaded else None
        if status == "provider_started":
            return True
        if status != "pending":
            _log(agent, "worker_hang_redo_mark_failed", artifact=bound.get("artifact_id"),
                 error=f"unexpected_status:{status}")
            bound["provider_start_failed"] = True
            return False
        path, payload = loaded
        _finish_redo(payload, "provider_started", provider_started_at=_now_iso())
        try:
            _write_json_atomic(path, payload)
        except Exception as err:
            # The replace may already have exposed `provider_started` on disk
            # (e.g. the directory barrier failed afterwards); the caller skips
            # the provider call, and settlement reports that truthfully from
            # this process-local flag rather than from the directory entry.
            _log(agent, "worker_hang_redo_mark_failed", artifact=path.name,
                 error=(str(err) or repr(err))[:300])
            bound["provider_start_failed"] = True
            return False
    _log(agent, "worker_hang_redo_provider_started", artifact=path.name, attempt=bound.get("attempt"))
    return True


def settle_worker_hang_redo(agent, redo_ref: dict | None, *, outcome: str) -> bool:
    """Terminally settle the bound redo once its turn has ended (strips text).
    A redo that never reached ``provider_started`` settles ``no_provider_call``.
    An already-terminal redo (e.g. ``abandoned`` by a dismissal that won the
    race) is authoritative and is never rewritten."""
    if not isinstance(redo_ref, dict):
        return False
    with _lock(agent):
        if getattr(agent, "_llm_worker_redo_in_flight", None) is redo_ref:
            agent._llm_worker_redo_in_flight = None
        agent._llm_worker_redo_turn = None
        loaded = _artifact_by_id(agent, redo_ref.get("artifact_id"))
        ok = False
        if loaded:
            path, payload = loaded
            status = (payload.get("redo") or {}).get("status")
            if status in _REDO_TERMINAL:
                _log(agent, "worker_hang_redo_settled", outcome=f"already_{status}",
                     artifact=redo_ref.get("artifact_id"), attempt=redo_ref.get("attempt"),
                     persisted=False)
                return False
            # In-memory truth wins over what the directory entry happens to
            # show: a failed mark (unexpected status, write, or barrier
            # failure after the replace) means no provider call happened,
            # even if `provider_started` became visible on disk.
            if status == "pending" or redo_ref.get("provider_start_failed"):
                outcome = "no_provider_call"
            _finish_redo(payload, "completed", outcome=str(outcome)[:200], completed_at=_now_iso())
            try:
                _write_json_atomic(path, payload)
                ok = True
            except Exception as err:
                _log(agent, "worker_hang_redo_mark_failed", artifact=path.name,
                     error=(str(err) or repr(err))[:300])
    _log(agent, "worker_hang_redo_settled", outcome=outcome, artifact=redo_ref.get("artifact_id"),
         attempt=redo_ref.get("attempt"), persisted=ok)
    return ok


def _log(agent, event: str, **fields) -> None:
    try:
        agent._log(event, **fields)
    except Exception:
        pass


def maybe_prepend_worker_hang_recovery_prompt(agent, content: str) -> str:
    """Prepend one concise recovery notice to the next safe text request.

    Only fires once per artifact (marked via ``prompt_injected_at``).  Returns
    ``content`` unchanged when there is nothing open to recover. When the
    request is the kernel's own AED redo, the notice says so.
    """
    if not isinstance(content, str):
        return content
    # Select, mutate, and write under the recovery lock: a concurrent
    # dismissal (resolve_worker_hang_artifact) must never be overwritten by a
    # stale open/pending payload carrying only the notice mark.
    with _lock(agent):
        artifacts = _pending_recovery_prompt_artifacts(agent)
        if not artifacts:
            return content
        _created_at, path, payload = artifacts[0]
        try:
            artifact_relpath = path.relative_to(agent._working_dir).as_posix()
        except ValueError:
            artifact_relpath = str(path)
        injected_at = _now_iso()
        payload["prompt_injected_at"] = injected_at
        payload["prompt_injected_on"] = "next_safe_text_request"
        try:
            _write_json_atomic(path, payload)
        except Exception as mark_err:
            try:
                agent._log(
                    "worker_hang_prompt_mark_failed",
                    artifact=artifact_relpath,
                    error=(str(mark_err) or repr(mark_err))[:300],
                )
            except Exception:
                pass
    bound = getattr(agent, "_llm_worker_redo_turn", None)
    redo_line = (
        f"The kernel is now automatically redoing that interrupted call (AED redo "
        f"attempt {bound.get('attempt')} of {bound.get('max_attempts')}); the request "
        "below is that redo, not a new instruction. "
        if isinstance(bound, dict) and bound.get("artifact_id") == path.stem else ""
    )
    notice = (
        "[Kernel recovery notice]\n"
        "A previous LLM call was abandoned because its worker was still running "
        "after timeout plus grace. The kernel skipped saving the unsafe chat "
        "interface and refreshed/rebuilt from the last safe on-disk history. "
        f"Do not assume the abandoned LLM response exists. {redo_line}If task context is "
        f"missing, inspect {artifact_relpath}, current notifications, mail, "
        "and pad, then continue or ask for direction.\n"
        "[/Kernel recovery notice]"
    )
    return f"{notice}\n\n{content}"
