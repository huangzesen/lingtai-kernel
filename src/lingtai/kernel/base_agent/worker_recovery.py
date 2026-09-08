"""WorkerStillRunning recovery helpers.

These helpers keep the fail-closed safety rule in one place: once a worker
thread is still running after timeout + grace, the live ChatInterface is
poisoned for this process and recovery must happen from durable on-disk state
after refresh/relaunch.

Design reference: Lingtai-AI/lingtai-kernel#298 (rebuilt for current main).
"""
from __future__ import annotations

import hashlib
import json
import secrets
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


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


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


def build_worker_hang_context(agent, msg: Message, exc: BaseException) -> dict:
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
    return context


def write_worker_hang_artifact(agent, exc: BaseException, context: dict) -> str | None:
    """Write the bounded/redacted unfinished-turn artifact.

    Returns the working-dir-relative path, or None on write failure.  The
    artifact intentionally contains NO raw chat history, tool args, or tool
    results — only the bounded/redacted previews built in ``context``.
    """
    created_at = _now_iso()
    artifact_id = f"worker_still_running_{_now_stamp()}_{secrets.token_hex(3)}"
    relpath = f"history/unfinished_turns/{artifact_id}.json"
    path = agent._working_dir / relpath
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
        "privacy": {
            "raw_chat_history_included": False,
            "raw_tool_args_included": False,
            "raw_tool_results_included": False,
            "previews_redacted": True,
            "max_preview_chars": MAX_PREVIEW_CHARS,
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
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
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


def maybe_prepend_worker_hang_recovery_prompt(agent, content: str) -> str:
    """Prepend one concise recovery notice to the next safe text request.

    Only fires once per artifact (marked via ``prompt_injected_at``).  Returns
    ``content`` unchanged when there is nothing open to recover.
    """
    if not isinstance(content, str):
        return content
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
    notice = (
        "[Kernel recovery notice]\n"
        "A previous LLM call was abandoned because its worker was still running "
        "after timeout plus grace. The kernel skipped saving the unsafe chat "
        "interface and refreshed/rebuilt from the last safe on-disk history. "
        "Do not assume the abandoned LLM response exists. If task context is "
        f"missing, inspect {artifact_relpath}, current notifications, mail, "
        "and pad, then continue or ask for direction.\n"
        "[/Kernel recovery notice]"
    )
    return f"{notice}\n\n{content}"
