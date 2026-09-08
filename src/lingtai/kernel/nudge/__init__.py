"""Per-agent nudges — periodic checks that emit a notification when
something needs the agent's attention.

Each check is a self-contained module exposing ``check(agent) -> None``.
Checks may use a bounded in-memory probe gate, but shared enabled/repeat and
finding-dismissal semantics live here. Most nudges upsert/remove entries in the
shared ``.notification/nudge.json`` payload. Goal reminders are a separately
classified system notification: they read protected ``.notification/goal.json``
and publish short ``goal.reminder`` events into ``.notification/system.json``;
they are not declared Nudge kinds.

Channel ``.notification/nudge.json`` carries a list of active nudges:

    {
      "header": "<rendered by _render_header — e.g. '2 nudges'>",
      "icon": "🔔",
      "priority": "low",
      "instructions": "Call notification(action='dismiss_channel', input={'channel': 'nudge', ...}, reasoning='...') ...",
      "data": {"nudges": [{"kind": "kernel_version", "nudge_channel": "release_version", ...}, ...]}
    }

Each check identifies its slot by a unique ``kind`` string. Built-in producers
are classified by fixed Core-owned kind mapping; unrelated legacy/unknown
entries remain channel-less. ``upsert`` replaces (or appends) one entry;
``remove`` deletes one. When the last entry leaves, the channel file is cleared
so the agent's wire surface drops the notification entirely. The agent
dismisses everything at once with
``notification(action='dismiss_channel', input={'channel': 'nudge',
'force': null, 'reason': null}, reasoning='...')``.

To add a new nudge: drop ``nudge/<name>.py`` exposing ``check(agent)``,
then add an import + dispatch line to :func:`run_checks` below. No
registry, no protocol — keep the surface flat.

Concurrency: each RMW upsert/remove is one Store-owned atomic channel update.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import event_journal_count, folder_size, init_config, kernel_version, goal, source_drift


DEFAULT_ENABLED = True
DEFAULT_REPEAT_INTERVAL_SECONDS = 24 * 60 * 60
ENABLED_ENV = "LINGTAI_NUDGE_ENABLED"
REPEAT_INTERVAL_ENV = "LINGTAI_NUDGE_REPEAT_INTERVAL"
ENVIRONMENT_MANUAL = "system-manual/reference/environment-variables/SKILL.md"
ENTRY_CHANNEL_RELEASE_VERSION = "release_version"
ENTRY_CHANNEL_SOURCE_INTEGRITY = "source_integrity"
ENTRY_CHANNEL_CONFIG_STALENESS = "configuration_staleness"
ENTRY_CHANNEL_STORAGE_SIZE = "storage_size"
_ENTRY_CHANNEL_BY_KIND = {
    "kernel_version": ENTRY_CHANNEL_RELEASE_VERSION,
    "source_drift": ENTRY_CHANNEL_SOURCE_INTEGRITY,
    "init_config_shape": ENTRY_CHANNEL_CONFIG_STALENESS,
    "folder_size": ENTRY_CHANNEL_STORAGE_SIZE,
    "event_journal_line_count": ENTRY_CHANNEL_STORAGE_SIZE,
}

# Hard maximum for one nudge entry's inline, model-visible JSON serialization.
# Entries at or below this size are unchanged; entries above it are
# externalized to a sidecar file under the agent's working directory and
# replaced on the wire by a compact summary (see `_cap_inline_payload`).
INLINE_MAX_CHARS = 10_000

# Bounded shape a `kind` must satisfy before it can participate in
# externalization filename construction. Current built-in kinds
# ("kernel_version", "source_drift", "init_config_shape", …) are short
# identifiers well within this bound; only a pathological/programming-error
# `kind` would ever trip it.
_MAX_KIND_LEN = 100
_KIND_RE = re.compile(r"^[A-Za-z0-9_.-]{1,%d}$" % _MAX_KIND_LEN)


class NudgeExternalizationError(RuntimeError):
    """Raised when an oversized finding cannot be safely externalized.

    Carries a bounded, static message only — never the producer's `kind`,
    title, detail, or any oversized payload content — so a caller that logs
    ``str(exc)`` cannot leak the finding body or an escape-heavy producer
    string.
    """


@dataclass(frozen=True, slots=True)
class NudgePolicy:
    """The shared policy read by every Nudge producer."""

    enabled: bool = DEFAULT_ENABLED
    repeat_interval_seconds: float = DEFAULT_REPEAT_INTERVAL_SECONDS
    enabled_value: str = "on"
    repeat_interval_value: str = "24h"
    invalid_values: tuple[str, ...] = ()

    def message(self) -> str:
        return (
            f"Nudge policy: {ENABLED_ENV}={self.enabled_value} (effective "
            f"{'on' if self.enabled else 'off'}); {REPEAT_INTERVAL_ENV}="
            f"{self.repeat_interval_value} (effective repeat-after-dismiss "
            f"{_format_duration(self.repeat_interval_seconds)}). "
            f"Read {ENVIRONMENT_MANUAL} for scope, reload timing, and invalid-value behavior."
        )

    def payload(self) -> dict[str, Any]:
        return {
            "enabled": "on" if self.enabled else "off",
            "repeat_after_dismiss": _format_duration(self.repeat_interval_seconds),
            "env": {
                "enabled": ENABLED_ENV,
                "repeat_interval": REPEAT_INTERVAL_ENV,
            },
            "documentation": ENVIRONMENT_MANUAL,
        }


def effective_policy(environ: Mapping[str, str] | None = None) -> NudgePolicy:
    """Read both global controls at the start of each Nudge operation.

    Accepted values are case-insensitive ``on``/``off`` for the switch and a
    positive duration such as ``30m``, ``24h``, or ``2d`` for the interval.
    Invalid or missing values fail safe to the documented defaults and are
    reported in ``invalid_values``; a process restart is not required.
    """
    env = os.environ if environ is None else environ
    invalid: list[str] = []
    raw_enabled = str(env.get(ENABLED_ENV, "on")).strip().lower()
    if raw_enabled in {"on", "true", "1"}:
        enabled = True
        enabled_value = "on"
    elif raw_enabled in {"off", "false", "0"}:
        enabled = False
        enabled_value = "off"
    else:
        enabled = DEFAULT_ENABLED
        enabled_value = "on"
        invalid.append(f"{ENABLED_ENV}={raw_enabled!r}")

    raw_interval = str(env.get(REPEAT_INTERVAL_ENV, "24h")).strip().lower()
    seconds = _parse_duration(raw_interval)
    if seconds is None:
        seconds = float(DEFAULT_REPEAT_INTERVAL_SECONDS)
        repeat_value = "24h"
        invalid.append(f"{REPEAT_INTERVAL_ENV}={raw_interval!r}")
    else:
        repeat_value = _format_duration(seconds)

    return NudgePolicy(
        enabled=enabled,
        repeat_interval_seconds=seconds,
        enabled_value=enabled_value,
        repeat_interval_value=repeat_value,
        invalid_values=tuple(invalid),
    )


def _parse_duration(value: str) -> float | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(s|m|h|d)", value)
    if not match:
        return None
    amount = float(match.group(1))
    if amount <= 0:
        return None
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def _format_duration(seconds: float) -> str:
    # Zero is a degenerate interval, but it is still rendered in the smallest
    # unit rather than looking like a one-hour policy value. Preserve the
    # product default's documented spelling (`24h`) for positive values. Use
    # day units only for an exact whole-day interval; 25h/36h/47h must not lose
    # their remaining hours through floor-to-days truncation.
    if seconds == 0:
        return "0s"
    if seconds % 86400 == 0 and seconds > 86400:
        return f"{int(seconds // 86400)}d"
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{seconds:g}s"


__all__ = [
    "DEFAULT_ENABLED",
    "DEFAULT_REPEAT_INTERVAL_SECONDS",
    "ENABLED_ENV",
    "ENTRY_CHANNEL_CONFIG_STALENESS",
    "ENTRY_CHANNEL_RELEASE_VERSION",
    "ENTRY_CHANNEL_SOURCE_INTEGRITY",
    "ENTRY_CHANNEL_STORAGE_SIZE",
    "ENVIRONMENT_MANUAL",
    "INLINE_MAX_CHARS",
    "NudgeExternalizationError",
    "NudgePolicy",
    "REPEAT_INTERVAL_ENV",
    "effective_policy",
    "record_dismissal",
    "run_checks",
    "run_checks_nonblocking",
    "run_system_notifications",
    "upsert",
    "remove",
]


class NudgeObservationOwner:
    """Small per-agent single-flight owner for potentially unbounded probes.

    It has no durable queue: a due task is either running, replaces a same-kind
    pending task, or is discarded as already represented.  This deliberately
    prevents a slow filesystem observation from building an unbounded heartbeat
    backlog.
    """

    def __init__(self, agent) -> None:
        self._agent = agent
        self._lock = threading.Lock()
        self._pending: dict[str, object] = {}
        self._thread: threading.Thread | None = None

    def schedule(self, kind: str, fn) -> bool:
        with self._lock:
            self._pending[kind] = fn
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(
                target=self._drain,
                name="lingtai-nudge-observation",
                daemon=True,
            )
            self._thread.start()
            return True

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._thread = None
                    return
                _, fn = self._pending.popitem()
            try:
                fn()
            except Exception:
                # Each producer runner records its own bounded diagnostic.
                continue


def _policy_for_checks(agent) -> NudgePolicy | None:
    policy = effective_policy()
    if policy.invalid_values:
        _safe_log(agent, "nudge_policy_invalid", values=list(policy.invalid_values))
    if not policy.enabled:
        # Clear stale visible findings immediately, even when a producer's own
        # bounded probe gate would otherwise return without touching the store.
        _modify(agent, lambda entries: [])
        return None
    return policy


def run_checks(agent) -> None:
    """Synchronously run all declared checks for explicit/manual callers.

    Heartbeat code uses :func:`run_checks_nonblocking`; retaining this direct
    entry point makes tests and deliberate callers able to request a completed
    observation rather than a scheduled one.
    """
    if _policy_for_checks(agent) is None:
        return
    _run_one(agent, "kernel_version", kernel_version.check)
    _run_one(agent, "source_drift", source_drift.check)
    _run_one(agent, "init_config_shape", init_config.check)
    _run_one(agent, "folder_size", folder_size.check)
    _run_one(agent, "event_journal_line_count", event_journal_count.check)


def _observation_owner(agent) -> NudgeObservationOwner:
    owner = getattr(agent, "_nudge_observation_owner", None)
    if isinstance(owner, NudgeObservationOwner):
        return owner
    owner = NudgeObservationOwner(agent)
    try:
        setattr(agent, "_nudge_observation_owner", owner)
    except Exception:
        pass
    return owner


def _observe_then_evaluate(agent, kind: str, module) -> None:
    _run_one(agent, f"{kind}_observation", module.observe)
    _run_one(agent, kind, module.evaluate)


def run_checks_nonblocking(agent) -> None:
    """Evaluate persisted facts now and coalesce expensive observations.

    Only normal current-state/policy work stays on the heartbeat. Recursive
    folder walks and journal scans are scheduled under one explicit owner, so
    heartbeat liveness is independent of their storage latency.
    """
    if _policy_for_checks(agent) is None:
        return
    _run_one(agent, "kernel_version", kernel_version.check)
    _run_one(agent, "source_drift", source_drift.check)
    _run_one(agent, "init_config_shape", init_config.check)
    _run_one(agent, "folder_size", folder_size.evaluate)
    _run_one(agent, "event_journal_line_count", event_journal_count.evaluate)

    owner = _observation_owner(agent)
    for kind, module in (("folder_size", folder_size), ("event_journal_line_count", event_journal_count)):
        try:
            due = module.observation_due(agent)
        except Exception as exc:
            _safe_log(agent, "nudge_check_error", kind=f"{kind}_observation_due", error=str(exc)[:200])
            continue
        if due:
            owner.schedule(kind, lambda kind=kind, module=module: _observe_then_evaluate(agent, kind, module))

def run_system_notifications(agent) -> None:
    """Dispatch protected system reminders that are not Nudge findings."""
    _run_one(agent, "goal_system_notification", goal.check)


def _run_one(agent, name: str, fn) -> None:
    try:
        fn(agent)
    except Exception as e:
        try:
            agent._log("nudge_check_error", kind=name, error=str(e)[:200])
        except Exception:
            pass


def upsert(agent, kind: str, body: dict) -> None:
    """Replace or append one finding under the shared global Nudge policy."""
    policy = effective_policy()
    if policy.invalid_values:
        _safe_log(agent, "nudge_policy_invalid", values=list(policy.invalid_values))
    if not policy.enabled:
        _modify(agent, lambda entries: [e for e in entries if e.get("kind") != kind])
        return

    resolved_channel = _entry_channel_for_kind(kind)
    entry = dict(body)
    entry["kind"] = kind
    if resolved_channel is not None:
        entry["nudge_channel"] = resolved_channel
    else:
        entry.pop("nudge_channel", None)
    entry["policy"] = policy.payload()
    entry["policy_message"] = policy.message()
    entry["detail"] = (
        f"{entry.get('detail', '')}\n\n{policy.message()}".strip()
    )
    # Fingerprint identity is computed on the full pre-cap entry so dismissal
    # muting is a property of the finding's facts, not of whether the wire
    # copy happened to be externalized this heartbeat.
    fingerprint = _finding_fingerprint(kind, entry)
    legacy_fingerprint = _finding_fingerprint(kind, entry, include_channel=False)
    now = time.time()
    if (
        _dismissed_until(agent, fingerprint) > now
        or _dismissed_until(agent, legacy_fingerprint) > now
    ):
        return
    # Externalization must complete (or return the entry unchanged) before
    # ANY state mutates. If it raises, neither `.notification/nudge.json`
    # nor `.notification/.nudge_state.json` may be touched, so a later
    # heartbeat retry sees byte-for-byte the same prior state.
    entry = _cap_inline_payload(agent, kind, entry, fingerprint=fingerprint)
    _clear_dismissal(agent, fingerprint)
    if legacy_fingerprint != fingerprint:
        _clear_dismissal(agent, legacy_fingerprint)

    def _publish_unless_dismissed(entries: list) -> list:
        # Runs inside the Store's `nudge` channel transaction — the same
        # compare-update `dismiss_channel` uses to clear the channel — so the
        # cheap pre-check above is repeated here, serialized with any dismissal
        # that landed in between. This is a read-only check (the Store mutator
        # stays pure): a still-future dismissal wins and the entries are
        # returned unchanged, leaving its record in force.
        now = time.time()
        if (
            _dismissed_until(agent, fingerprint) > now
            or _dismissed_until(agent, legacy_fingerprint) > now
        ):
            return entries
        return _replace_kind(entries, kind, entry)

    _modify(agent, _publish_unless_dismissed)


def remove(agent, kind: str) -> None:
    """Drop the nudge entry for ``kind``; resolved findings are not re-emitted."""
    _modify(agent, lambda entries: [e for e in entries if e.get("kind") != kind])


def record_dismissal(agent) -> None:
    """Record dismissal of the currently displayed findings, not resolution.

    Notification remains the transport and calls this policy hook before it
    clears the shared channel.  The local state stores only finding identity
    and an expiry; it is not a migration registry or per-kind cadence.
    """
    policy = effective_policy()
    entries = _current_entries(agent)
    if not entries:
        return
    state = _load_policy_state(agent)
    now = time.time()
    dismissed = state.setdefault("dismissed", {})
    for entry in entries:
        kind = str(entry.get("kind") or "")
        if not kind:
            continue
        fingerprint = _finding_fingerprint(kind, entry)
        dismissed[fingerprint] = {
            "kind": kind,
            "dismissed_at": now,
            "until": now + policy.repeat_interval_seconds,
        }
        if "nudge_channel" not in entry:
            legacy_fingerprint = _finding_fingerprint(kind, entry, include_channel=False)
            if legacy_fingerprint != fingerprint:
                dismissed[legacy_fingerprint] = {
                    "kind": kind,
                    "dismissed_at": now,
                    "until": now + policy.repeat_interval_seconds,
                }
    _save_policy_state(agent, state)
    _safe_log(
        agent,
        "nudge_dismissed",
        findings=len(entries),
        repeat_interval_seconds=policy.repeat_interval_seconds,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_STATE_FILE = Path(".notification") / ".nudge_state.json"


def _finding_fingerprint(kind: str, body: dict, *, include_channel: bool = True) -> str:
    """Hash stable finding facts, excluding policy display bookkeeping.

    When an entry carries a stamped ``_dismiss_fingerprint`` (written by
    :func:`upsert` before inline-cap externalization may rewrite ``title``/
    ``detail``/``source``), that stamped value is authoritative — it is the
    fingerprint of the complete original finding. This keeps dismissal
    identity a property of the finding's facts, not of the persisted
    (possibly capped) wire shape, so a capped finding's dismiss/repeat
    semantics stay correct whether recomputed at upsert time or read back
    from a persisted compact entry.
    """
    stamped = body.get("_dismiss_fingerprint")
    if isinstance(stamped, str) and stamped:
        return stamped
    stable = {
        "kind": kind,
        "title": body.get("title"),
        "detail": body.get("detail"),
        "source": body.get("source"),
        "running": body.get("running"),
        "installed": body.get("installed"),
        "latest": body.get("latest"),
        "drift_signals": body.get("drift_signals"),
    }
    if include_channel:
        stable["nudge_channel"] = body.get("nudge_channel")
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry_inline_chars(entry: dict) -> tuple[str, int]:
    """Return the canonical wire serialization and its character count.

    This is the exact JSON text that would be persisted into
    ``.notification/nudge.json`` for this one entry, measured *after* the
    shared policy fields (``policy``, ``policy_message``, policy-appended
    ``detail``) are already present — so cap enforcement sees what a reader
    actually receives, not the producer's raw pre-policy body.
    """
    text = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
    return text, len(text)


def _findings_dir(agent) -> Path | None:
    working_dir = getattr(agent, "_working_dir", None)
    if working_dir is None:
        return None
    from .. import workdir

    return workdir.workdir_layout(Path(working_dir)).nudge_findings_dir


def _write_sidecar_atomic(target: Path, text: str) -> None:
    """Write ``text`` to a sibling temp file and atomically replace ``target``.

    Narrow, private I/O seam so a test can inject a write failure without
    manipulating real filesystem permissions (which production's own
    directory-permission repair, ``os.chmod(findings_dir, 0o700)``, would
    otherwise undo before the write is attempted).

    The temp file is created owner-only (``0o600``) from the moment it is
    opened — via ``os.open`` with an explicit ``mode``, not ``open()`` at the
    process umask followed by a later ``chmod`` — so the oversized finding
    body is never briefly readable at a broader mode. Content is written as
    exact UTF-8 bytes and flushed/fsynced before the atomic ``os.replace``,
    so a crash between write and replace cannot leave a target that looks
    complete but is actually truncated.

    On any failure the caller is responsible for translating the raised
    ``OSError`` into a bounded ``NudgeExternalizationError``; this helper
    itself never logs or otherwise persists ``text``. No temp-file cleanup is
    performed here — a failed write's partial temp file, already owner-only,
    is left on disk as forensic evidence rather than being deleted.
    """
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(target))


def _cap_inline_payload(agent, kind: str, entry: dict, *, fingerprint: str) -> dict:
    """Enforce the hard inline-payload cap for one fully-assembled entry.

    At or below :data:`INLINE_MAX_CHARS`, ``entry`` is returned completely
    unchanged — no ``_dismiss_fingerprint`` or other cap bookkeeping is
    added, so the ordinary small-finding wire shape is byte-for-byte the
    same as before this cap existed.

    Above the cap, the complete original entry (including policy fields) is
    persisted verbatim as UTF-8 JSON to a content-addressed sidecar file
    under ``<working_dir>/tmp/nudge-findings/`` (an ordinary agent temp
    location, consistent with ``tmp/tool-results/``), and this function
    returns a small compact entry: a bounded ``title``, a short bounded
    ``detail``, the policy fields (so policy display stays intact), and an
    ``externalized`` block naming the absolute file path, its exact
    character/byte counts, and its SHA-256. The caller (``upsert``) stamps
    ``kind`` back onto whatever this returns via ``_replace_kind``.

    Content addressing (filename = ``sha256(bytes)``) means an unchanged
    finding does not create a new sidecar file every heartbeat — the same
    bytes hash to the same path and the write is a cheap no-op check.

    Fail LOUD, not fail-open: if ``kind`` is not a bounded, filesystem-safe
    identifier, or the sidecar write does not durably succeed, this raises
    :class:`NudgeExternalizationError` with a bounded static message. The
    caller (``upsert``) has not yet mutated ``.notification/nudge.json`` at
    the point this is called, so a raise here leaves prior notification
    state completely untouched for a later heartbeat retry. The oversized
    body is never persisted inline as a fallback.
    """
    if not _KIND_RE.match(kind):
        raise NudgeExternalizationError(
            "nudge finding kind failed bounded validation "
            f"(must match {_KIND_RE.pattern!r}); refusing to externalize or "
            "persist this finding"
        )

    text, char_count = _entry_inline_chars(entry)
    if char_count <= INLINE_MAX_CHARS:
        return entry

    raw_bytes = text.encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    byte_count = len(raw_bytes)

    findings_dir = _findings_dir(agent)
    if findings_dir is None:
        raise NudgeExternalizationError(
            "no working_dir configured; cannot durably externalize an "
            "oversized nudge finding"
        )

    target = findings_dir / f"{kind}-{digest}.json"
    if not target.is_file():
        try:
            findings_dir.mkdir(parents=True, exist_ok=True)
            # Always enforce owner-only, even when the directory pre-existed
            # with looser permissions (e.g. created before this cap existed,
            # or by an external process) — a sidecar containing the full
            # oversized finding must never be group/world-readable.
            os.chmod(findings_dir, 0o700)
            _write_sidecar_atomic(target, text)
        except OSError as exc:
            raise NudgeExternalizationError(
                f"failed to durably write nudge finding sidecar file: "
                f"{type(exc).__name__}"
            ) from exc

    # Bound every field sourced from producer-controlled text (title, source)
    # so a pathological producer value cannot itself blow the compact entry
    # past the cap; the fixed-shape fields below (policy, sha256, counts) are
    # small by construction.
    title = str(entry.get("title") or kind)[:200]
    source = entry.get("source")
    if isinstance(source, str):
        source = source[:200]
    file_path = str(target)
    compact: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "source": source,
        "policy": entry.get("policy"),
        "policy_message": entry.get("policy_message"),
        # Stamped only on the capped/compact shape, never on an ordinary
        # unchanged small entry, so `record_dismissal` can recover the exact
        # pre-cap fingerprint later without recomputing it from the
        # (necessarily different) compact title/detail/source.
        "_dismiss_fingerprint": fingerprint,
        "externalized": {
            "reason": (
                f"Full finding is {char_count} chars, over the "
                f"{INLINE_MAX_CHARS}-char inline Nudge cap."
            ),
            "path": file_path,
            "original_char_count": char_count,
            "original_byte_count": byte_count,
            "sha256": digest,
        },
        "detail": (
            f"{title} — full finding exceeds the {INLINE_MAX_CHARS}-char inline "
            f"cap ({char_count} chars). The complete original is at {file_path} "
            f"(SHA-256 {digest}); read that file directly for full detail."
        ),
    }
    if "nudge_channel" in entry:
        compact["nudge_channel"] = entry["nudge_channel"]

    # Defensive: even the bounded fields above could combine to exceed the
    # cap in a degenerate case. Shrink detail/title first — this is
    # defence-in-depth, not a routine path, so a small bounded loop suffices.
    for _ in range(4):
        _, compact_chars = _entry_inline_chars(compact)
        if compact_chars <= INLINE_MAX_CHARS:
            break
        if len(compact.get("detail", "")) > 100:
            compact["detail"] = compact["detail"][:100] + "…"
        elif len(compact.get("title", "")) > 50:
            compact["title"] = compact["title"][:50] + "…"
        else:
            break

    _safe_log(
        agent,
        "nudge_finding_externalized",
        kind=kind,
        original_char_count=char_count,
        cap_chars=INLINE_MAX_CHARS,
        sha256=digest,
    )
    return compact


def _state_path(agent) -> Path | None:
    working_dir = getattr(agent, "_working_dir", None)
    if working_dir is None:
        return None
    return Path(working_dir) / _STATE_FILE


def _load_policy_state(agent) -> dict[str, Any]:
    path = _state_path(agent)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_policy_state(agent, state: dict[str, Any]) -> None:
    path = _state_path(agent)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _record_until(record: object) -> float:
    if not isinstance(record, dict):
        return 0.0
    try:
        return float(record.get("until") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _dismissed_until(agent, fingerprint: str) -> float:
    state = _load_policy_state(agent)
    return _record_until((state.get("dismissed") or {}).get(fingerprint))


def _clear_dismissal(agent, fingerprint: str) -> None:
    """Drop ``fingerprint``'s dismissal record only once it has expired.

    A record still in force can only have been written after ``upsert``'s
    pre-check passed (otherwise the pre-check would have returned), i.e. by a
    dismissal that interleaved with this upsert. That dismissal wins, so the
    record is left untouched for the in-transaction re-check to honour.
    """
    state = _load_policy_state(agent)
    dismissed = state.get("dismissed")
    if not isinstance(dismissed, dict) or fingerprint not in dismissed:
        return
    if _record_until(dismissed.get(fingerprint)) > time.time():
        return
    dismissed.pop(fingerprint, None)
    _save_policy_state(agent, state)


def _current_entries(agent) -> list[dict]:
    try:
        from ..notifications import _get_allow_predicate, _workdir_key
        snapshot = agent._notification_store.snapshot(
            _get_allow_predicate(_workdir_key(agent))
        )
        payload = snapshot.get("nudge")
        data = payload.get("data") if isinstance(payload, dict) else None
        entries = data.get("nudges") if isinstance(data, dict) else None
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    except Exception:
        return []


def _safe_log(agent, event: str, **fields: Any) -> None:
    try:
        agent._log(event, **fields)
    except Exception:
        pass


def _replace_kind(entries: list, kind: str, body: dict) -> list:
    out = [e for e in entries if e.get("kind") != kind]
    entry = dict(body)
    entry["kind"] = kind
    out.append(entry)
    return out


def _entry_channel_for_kind(kind: str) -> str | None:
    return _ENTRY_CHANNEL_BY_KIND.get(kind)


def _modify(agent, mutate) -> None:
    """Apply one pure current-payload nudge mutation atomically."""
    from datetime import datetime, timezone
    from ..notification_store import UNCONDITIONAL

    published_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _mutator(current_payload: dict):
        current = current_payload if isinstance(current_payload, dict) else {}
        data = current.get("data")
        existing = data.get("nudges", []) if isinstance(data, dict) else []
        existing = existing if isinstance(existing, list) else []
        new_entries = mutate(list(existing))
        if new_entries == existing:
            return current_payload, False, None
        if not new_entries:
            return None, True, None
        return {
            "header": _render_header(new_entries),
            "icon": "\U0001f514",
            "priority": "low",
            "published_at": published_at,
            "instructions": (
                "Call notification(action='dismiss_channel', input={'channel': "
                "'nudge', 'force': null, 'reason': null}, reasoning='...') to "
                "acknowledge and clear ALL nudges at once. Individual nudges "
                "may also describe a specific action to take (e.g. "
                "system(action='refresh') for a kernel upgrade)."
            ),
            "data": {"nudges": new_entries},
        }, True, None

    agent._notification_store.compare_update_channel(
        "nudge", UNCONDITIONAL, _mutator
    )


def _render_header(entries: list) -> str:
    n = len(entries)
    return f"{n} nudge{'s' if n != 1 else ''}"
