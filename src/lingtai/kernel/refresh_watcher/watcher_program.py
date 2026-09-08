"""Core renderer for the generated refresh-watcher policy.

The renderer preserves the existing handshake, heartbeat, retry, duplicate,
redaction, artifact, notification, and event behavior while keeping process
ownership behind the watcher-local ``RefreshWatcherProcessPort``. The returned
value is generated Python source executed by the owned entrypoint; this module
only assembles policy source and never constructs a concrete process adapter.
The entrypoint injects that adapter as the ``PROCESS_MECHANISM`` global.

The stale same-agent guard imports the canonical Core matcher at runtime rather
than embedding another matcher, and is reused to decide when a terminated
duplicate has actually released the working directory. Request identity fields
are serialized JSON snapshots and are validated before source generation. The
renderer itself performs no operating-system process operation; file, time,
heartbeat, retry, redaction, and alert policy remain in the generated Core
policy.
"""
from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path

from lingtai.kernel.notification_store._mutation_lock import (
    channel_mutation_scope,
    notification_mutation_lock_path,
)

from . import RefreshWatcherRequest

MAX_ATTEMPTS = 12
HEALTH_CHECK_WAIT = 10
# A relaunched agent boots its MCP stdio servers before the first heartbeat
# write. Production incident 2026-08-19 (spiritual-bliss-attractor/codex) showed
# that boot exceeding a single ``HEALTH_CHECK_WAIT`` sleep on every one of the
# 12 attempts, so the watcher declared a healthy-but-slow agent dead. The health
# check therefore polls for a fresh heartbeat every ``WATCHER_POLL_INTERVAL``
# until ``HEALTH_CHECK_BUDGET`` expires rather than sampling once.
HEALTH_CHECK_BUDGET = 60
WATCHER_POLL_INTERVAL = 0.5
# The same incident showed the other half of the starvation: cleanup sent
# SIGKILL to a stale duplicate and the next attempt started immediately, hitting
# 'another lingtai agent is already running' again because the duplicate had not
# left the process table yet. Bound how long the watcher waits for that exit.
DUPLICATE_EXIT_WAIT = 15
# Once the workdir lease is proved free, a heartbeat left by the dead owner
# proves nothing (incident 2026-09-08: the poisoned `os._exit` followed the
# last tick by seconds). Only a heartbeat that advances past the baseline
# captured at lock release counts; a live owner gets this long to advance it.
ALREADY_ALIVE_OBSERVE = 3.0
STDERR_TAIL_CHARS = 1200
# Set on every exception the generated policy has already recorded and
# settled (including its deliberate exits), so the entrypoint fail-safe does
# not report or settle a second time.
WATCHER_HANDLED_ATTR = "_lingtai_watcher_handled"
DUPLICATE_GUARD_MESSAGE = "another lingtai agent is already running"


def _decode_identity_fields(identity_fields_json: str) -> dict:
    """Decode+validate ``RefreshWatcherRequest.identity_fields_json``.

    Must parse as JSON and decode to a JSON *object* (a Python ``dict``) —
    the rendered program embeds it as a ``identity_fields = {...!r}`` literal
    merged into every logged event via ``**identity_fields``, which requires
    a mapping. Fails loudly (raises) on invalid JSON or a non-object
    top-level value, rather than silently falling back to ``{}`` and
    generating a watcher program whose event logging silently dropped the
    caller's runtime-identity fields.
    """
    try:
        decoded = json.loads(identity_fields_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "RefreshWatcherRequest.identity_fields_json is not valid JSON: "
            f"{identity_fields_json!r}"
        ) from exc
    if not isinstance(decoded, dict):
        raise ValueError(
            "RefreshWatcherRequest.identity_fields_json must decode to a "
            f"JSON object, got {type(decoded).__name__}: {identity_fields_json!r}"
        )
    return decoded


def render_watcher_script(request: RefreshWatcherRequest) -> str:
    """Render the complete watcher program source.

    The returned text embeds every request-derived value it needs (handshake
    paths, relaunch command, identity fields), and carries no reference to this
    process's live objects. Execution requires an importable LingTai package for
    the kernel's redaction helper and canonical process-command matcher in
    addition to the Python standard library.
    """
    taken_path = request.taken_path
    lock_path = request.lock_path
    events_path = request.events_path
    stderr_log = request.stderr_log
    working_dir_str = request.working_dir
    cmd = list(request.cmd)
    agent_name = request.agent_name
    address = request.address
    identity_fields = _decode_identity_fields(request.identity_fields_json)
    scoped_system_lock_name = notification_mutation_lock_path(
        Path(working_dir_str) / ".notification",
        channel_mutation_scope("system"),
    ).name

    return (
        "import time, os, sys, json\n"
        "try:\n"
        "    import fcntl as _fcntl\n"
        "except ImportError:\n"
        "    _fcntl = None\n"
        # The generated watcher merges .notification/system.json from a separate
        # process. The in-agent Notification Store serializes every channel
        # mutation with an advisory flock on the same sidecar (see
        # src/lingtai/kernel/notification_store/_mutation_lock.py), and the
        # watcher must participate in that lock or its terminal alert can be
        # silently lost against a concurrent agent merge (issue #742).
        "_NOTIFICATION_LOCK_TIMEOUT = 5.0\n"
        "def _process_mechanism():\n"
        "    try:\n"
        "        return PROCESS_MECHANISM\n"
        "    except NameError as exc:\n"
        "        raise RuntimeError('refresh watcher process mechanism was not injected') from exc\n"
        # The Core workdir lease Port (platform adapter injected by the
        # entrypoint): lock-file existence is not authority, the OS lease is.
        "def _workdir_lease():\n"
        "    try:\n"
        "        return WORKDIR_LEASE\n"
        "    except NameError as exc:\n"
        "        raise RuntimeError('refresh watcher workdir lease was not injected') from exc\n"
        f"HANDLED = {WATCHER_HANDLED_ATTR!r}\n"
        "_deliberate = False\n"
        "def _exit(code):\n"
        "    global _deliberate\n"
        "    _deliberate = True\n"
        "    sys.exit(code)\n"
        # `.refresh.taken` is what observers read as 'Refreshing'; every
        # terminal watcher outcome settles it (the CLI child consumes it
        # before its first heartbeat, so success can never race that).
        "def _settle_taken(reason):\n"
        "    if not os.path.exists(taken):\n"
        "        return\n"
        "    try:\n"
        "        os.unlink(taken)\n"
        "        log('refresh_taken_marker_cleared', reason=reason)\n"
        "    except OSError as e:\n"
        "        log('refresh_taken_marker_clear_failed', reason=reason, error=_bounded(str(e), 200))\n"
        "from datetime import datetime, timezone\n"
        f"taken = {taken_path!r}\n"
        f"lock = {lock_path!r}\n"
        f"events = {events_path!r}\n"
        f"stderr_log = {stderr_log!r}\n"
        f"wd = {working_dir_str!r}\n"
        f"cmd = {cmd!r}\n"
        f"name = {agent_name!r}\n"
        f"addr = {address!r}\n"
        f"identity_fields = {identity_fields!r}\n"
        f"MAX_ATTEMPTS = {MAX_ATTEMPTS}\n"
        f"HEALTH_CHECK_WAIT = {HEALTH_CHECK_WAIT}\n"
        f"HEALTH_CHECK_BUDGET = {HEALTH_CHECK_BUDGET}\n"
        f"WATCHER_POLL_INTERVAL = {WATCHER_POLL_INTERVAL}\n"
        f"DUPLICATE_EXIT_WAIT = {DUPLICATE_EXIT_WAIT}\n"
        f"ALREADY_ALIVE_OBSERVE = {ALREADY_ALIVE_OBSERVE}\n"
        f"DUPLICATE_GUARD_MESSAGE = {DUPLICATE_GUARD_MESSAGE!r}\n"
        # The watcher writes events.jsonl through its own log() below, bypassing
        # the in-process CompositeLoggingService.redact_for_trajectory. Secret-
        # shaped values reach these events via stderr_tail (relaunched-process
        # stderr, e.g. a config traceback echoing a token), cmdline, and error
        # strings, so redact the whole event dict here before persisting. Use the
        # kernel's redact_for_trajectory (not just redact_text value-walking) so
        # the watcher gets the same key-aware redaction as normal trajectory
        # logging: values under secret-named keys are removed even when they do
        # not match a known token shape. The kernel redactor is the single source
        # of truth; fail open to identity if it cannot be imported so the watcher
        # never crashes over redaction, but record a non-secret marker so the
        # degradation is diagnosable rather than silent.
        "try:\n"
        "    from lingtai.kernel.trace_redaction import redact_for_trajectory as _redact_for_trajectory\n"
        "    _REDACTOR_IMPORT_OK = True\n"
        "except Exception:\n"
        "    def _redact_for_trajectory(value):\n"
        "        return value\n"
        "    _REDACTOR_IMPORT_OK = False\n"
        # Terminal-failure visibility (PR #292): when all relaunch attempts are
        # exhausted the watcher writes logs/refresh_failed_permanent.json and a
        # high-priority system notification carrying this failure_state so the
        # dead agent is diagnosable rather than silently gone. failure_state is
        # mutated in place across attempts by the relaunch loop and cleanup
        # helpers below.
        f"STDERR_TAIL_CHARS = {STDERR_TAIL_CHARS}\n"
        "failure_artifact = os.path.join(wd, 'logs', 'refresh_failed_permanent.json')\n"
        "RECOVERY_GUIDANCE = [\n"
        "    'Inspect logs/refresh_relaunch.log and logs/events.jsonl for the relaunch failure.',\n"
        "    'Run system(action=\"cpr\") or manually restart the agent after resolving the blocker.',\n"
        "    'If a duplicate PID is listed, verify it is this same agent before terminating it.',\n"
        "    'Do not delete .agent.lock by path; the kernel lock is advisory fd-based.',\n"
        "]\n"
        "failure_state = {\n"
        "    'attempts': MAX_ATTEMPTS,\n"
        "    'last_pid': None,\n"
        "    'last_duplicate_pid': None,\n"
        "    'last_relaunch_pid': None,\n"
        "    'last_heartbeat_age': None,\n"
        "    'last_heartbeat_status': 'unknown',\n"
        "    'last_stderr_tail': '',\n"
        "    'last_cleanup_action': 'not_attempted',\n"
        "    'last_cleanup_result': 'not_attempted',\n"
        "    'last_cleanup_error': None,\n"
        "    'last_relaunch_error': None,\n"
        "    'stderr_log': stderr_log,\n"
        "    'recovery_guidance': RECOVERY_GUIDANCE,\n"
        "}\n"
        "def log(typ, **kw):\n"
        "    entry = {'type': typ, 'address': addr, 'agent_name': name, 'ts': time.time(), **identity_fields, **kw}\n"
        "    if not _REDACTOR_IMPORT_OK:\n"
        "        entry['redaction_unavailable'] = True\n"
        "    else:\n"
        "        try:\n"
        "            entry = _redact_for_trajectory(entry)\n"
        "        except Exception:\n"
        "            entry = {'type': typ, 'address': addr, 'agent_name': name,\n"
        "                     'ts': entry.get('ts'), 'redaction_unavailable': True,\n"
        "                     'redaction_error': True}\n"
        "    try:\n"
        "        with open(events, 'a') as f:\n"
        "            f.write(json.dumps(entry) + '\\n')\n"
        "    except OSError:\n"
        "        pass\n"
        "def _now_iso():\n"
        "    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')\n"
        "def _bounded(text, limit=STDERR_TAIL_CHARS):\n"
        "    if text is None:\n"
        "        return ''\n"
        "    text = str(text)\n"
        "    return text[-limit:]\n"
        "def _write_json_atomic(path, payload):\n"
        "    os.makedirs(os.path.dirname(path), exist_ok=True)\n"
        "    tmp = f'{path}.tmp.{os.getpid()}'\n"
        "    with open(tmp, 'w', encoding='utf-8') as f:\n"
        "        json.dump(payload, f, ensure_ascii=False)\n"
        "        f.write('\\n')\n"
        "    os.replace(tmp, path)\n"
        "def _heartbeat_snapshot():\n"
        "    age = heartbeat_age()\n"
        "    if age is None:\n"
        "        return None, 'missing'\n"
        "    if age < 30:\n"
        "        return age, 'fresh'\n"
        "    return age, 'stale'\n"
        "def _read_stderr_tail():\n"
        "    try:\n"
        "        with open(stderr_log, encoding='utf-8', errors='replace') as f:\n"
        "            return _bounded(f.read())\n"
        "    except OSError:\n"
        "        return ''\n"
        "def _redact_bounded(text):\n"
        "    text = _bounded(text)\n"
        "    if not text:\n"
        "        return text\n"
        "    if _REDACTOR_IMPORT_OK:\n"
        "        try:\n"
        "            return _redact_for_trajectory(text)\n"
        "        except Exception:\n"
        "            return '<REDACTED:redaction-error>'\n"
        "    return '<REDACTED:redaction-unavailable>'\n"
        "def _failure_metadata():\n"
        "    meta = dict(failure_state)\n"
        "    meta['attempts'] = MAX_ATTEMPTS\n"
        "    meta['last_stderr_tail'] = _redact_bounded(meta.get('last_stderr_tail'))\n"
        "    meta['last_cleanup_error'] = _redact_bounded(meta.get('last_cleanup_error'))\n"
        "    meta['last_relaunch_error'] = _redact_bounded(meta.get('last_relaunch_error'))\n"
        "    if any(\n"
        "        v == '<REDACTED:redaction-error>' or v == '<REDACTED:redaction-unavailable>'\n"
        "        for v in (meta['last_stderr_tail'], meta['last_cleanup_error'], meta['last_relaunch_error'])\n"
        "    ):\n"
        "        meta['redaction_unavailable'] = True\n"
        "    meta['artifact_path'] = failure_artifact\n"
        "    return meta\n"
        "def _acquire_system_notification_lock():\n"
        "    notif_dir = os.path.join(wd, '.notification')\n"
        "    scope = 'channel:system'\n"
        f"    scoped_name = {scoped_system_lock_name!r}\n"
        "    try:\n"
        "        os.makedirs(os.path.join(notif_dir, '.locks'), exist_ok=True)\n"
        "    except OSError:\n"
        "        pass\n"
        "    if _fcntl is None:\n"
        "        # Native Windows has no POSIX shared-lock bridge. Its upgrade is\n"
        "        # intentionally quiesced; preserve the existing fail-open alert.\n"
        "        log('refresh_failed_permanent_lock_unavailable', reason='windows_quiesced_cutover')\n"
        "        return None\n"
        "    legacy_fd = None\n"
        "    try:\n"
        "        legacy_fd = open(os.path.join(notif_dir, '.store.lock'), 'a+b')\n"
        "        scoped_fd = open(os.path.join(notif_dir, '.locks', scoped_name), 'a+b')\n"
        "    except OSError as exc:\n"
        "        if legacy_fd is not None:\n"
        "            legacy_fd.close()\n"
        "        log('refresh_failed_permanent_lock_unavailable', reason='open_failed',\n"
        "            error=_bounded(str(exc), 200))\n"
        "        return None\n"
        "    deadline = time.time() + _NOTIFICATION_LOCK_TIMEOUT\n"
        "    while True:\n"
        "        try:\n"
        "            _fcntl.flock(legacy_fd.fileno(), _fcntl.LOCK_SH | _fcntl.LOCK_NB)\n"
        "            _fcntl.flock(scoped_fd.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)\n"
        "            return (legacy_fd, scoped_fd)\n"
        "        except OSError:\n"
        "            try:\n"
        "                _fcntl.flock(scoped_fd.fileno(), _fcntl.LOCK_UN)\n"
        "                _fcntl.flock(legacy_fd.fileno(), _fcntl.LOCK_UN)\n"
        "            except OSError:\n"
        "                pass\n"
        "            if time.time() >= deadline:\n"
        "                legacy_fd.close()\n"
        "                scoped_fd.close()\n"
        "                log('refresh_failed_permanent_lock_timeout')\n"
        "                return None\n"
        "            time.sleep(0.1)\n"
        "def _release_system_notification_lock(handles):\n"
        "    if handles is None:\n"
        "        return\n"
        "    legacy_fd, scoped_fd = handles\n"
        "    try:\n"
        "        _fcntl.flock(scoped_fd.fileno(), _fcntl.LOCK_UN)\n"
        "        _fcntl.flock(legacy_fd.fileno(), _fcntl.LOCK_UN)\n"
        "    except OSError:\n"
        "        pass\n"
        "    finally:\n"
        "        scoped_fd.close()\n"
        "        legacy_fd.close()\n"
        "def _append_system_notification(meta):\n"
        "    fd = _acquire_system_notification_lock()\n"
        "    try:\n"
        "        return _append_system_notification_unlocked(meta)\n"
        "    finally:\n"
        "        _release_system_notification_lock(fd)\n"
        "def _append_system_notification_unlocked(meta):\n"
        "    notif_dir = os.path.join(wd, '.notification')\n"
        "    target = os.path.join(notif_dir, 'system.json')\n"
        "    current = {}\n"
        "    try:\n"
        "        with open(target, encoding='utf-8') as f:\n"
        "            current = json.load(f)\n"
        "    except (OSError, ValueError, TypeError):\n"
        "        current = {}\n"
        "    if not isinstance(current, dict):\n"
        "        current = {}\n"
        "    events_list = current.get('data', {}).get('events', [])\n"
        "    if not isinstance(events_list, list):\n"
        "        events_list = []\n"
        "    event_id = f'evt_refresh_{int(time.time()*1000):x}_{os.getpid()}'\n"
        "    body = (\n"
        "        f'Refresh failed permanently after {MAX_ATTEMPTS} attempts. '\n"
        "        'Inspect logs/refresh_relaunch.log and restart with system(action=\"cpr\") '\n"
        "        'or a manual launch after resolving the blocker.'\n"
        "    )\n"
        "    events_list.append({\n"
        "        'event_id': event_id,\n"
        "        'source': 'refresh',\n"
        "        'ref_id': 'refresh_failed_permanent',\n"
        "        'body': body,\n"
        "        'at': _now_iso(),\n"
        "        'metadata': meta,\n"
        "    })\n"
        "    events_list = events_list[-20:]\n"
        "    payload = {\n"
        "        'header': f'{len(events_list)} system notification' + ('' if len(events_list) == 1 else 's'),\n"
        "        'icon': '!',\n"
        "        'priority': 'high',\n"
        "        'published_at': _now_iso(),\n"
        "        'instructions': 'Read the refresh event metadata, inspect the relaunch log, then recover with cpr or a manual restart.',\n"
        "        'data': {'events': events_list},\n"
        "    }\n"
        "    _write_json_atomic(target, payload)\n"
        "    return event_id\n"
        "def _publish_refresh_failed_permanent():\n"
        "    meta = _failure_metadata()\n"
        "    artifact = {\n"
        "        'type': 'refresh_failed_permanent',\n"
        "        'address': addr,\n"
        "        'agent_name': name,\n"
        "        'created_at': _now_iso(),\n"
        "        'metadata': meta,\n"
        "    }\n"
        "    alert_id = None\n"
        "    alert_error = None\n"
        "    try:\n"
        "        _write_json_atomic(failure_artifact, artifact)\n"
        "    except Exception as e:\n"
        "        alert_error = str(e)\n"
        "    try:\n"
        "        alert_id = _append_system_notification(meta)\n"
        "    except Exception as e:\n"
        "        alert_error = str(e) if alert_error is None else alert_error + '; ' + str(e)\n"
        "    if alert_error:\n"
        "        log('refresh_failed_permanent_alert_error', error=_bounded(alert_error, 500),\n"
        "            artifact_path=failure_artifact)\n"
        "    else:\n"
        "        log('refresh_failed_permanent_alert_published', alert_id=alert_id,\n"
        "            artifact_path=failure_artifact)\n"
        "    return alert_id, meta\n"
        "def heartbeat_ts():\n"
        "    try:\n"
        "        return float(open(os.path.join(wd, '.agent.heartbeat')).read().strip())\n"
        "    except (ValueError, OSError):\n"
        "        return None\n"
        "def heartbeat_age():\n"
        "    ts = heartbeat_ts()\n"
        "    return None if ts is None else time.time() - ts\n"
        # Baseline = the heartbeat present when the lease was proved free;
        # only a heartbeat newer than it can prove a live owner or child.
        "hb_baseline = None\n"
        "def advanced_heartbeat_age():\n"
        "    ts = heartbeat_ts()\n"
        "    if ts is None or (hb_baseline is not None and ts <= hb_baseline):\n"
        "        return None\n"
        "    return time.time() - ts\n"
        "def is_alive():\n"
        "    age = advanced_heartbeat_age()\n"
        "    return age is not None and age < 30\n"
        # The attempt marker written before every start_agent lets the health
        # check attribute a duplicate-guard line to *this* attempt. Read only the
        # tail so a multi-hundred-megabyte relaunch log stays cheap to poll, and
        # report False whenever this attempt's marker is not inside that window,
        # so a stale guard line from an earlier attempt can never end the poll.
        "def _attempt_marker(attempt):\n"
        "    return f'--- relaunch attempt {attempt} ---'\n"
        "def _duplicate_guard_seen(attempt):\n"
        "    try:\n"
        "        with open(stderr_log, 'rb') as f:\n"
        "            try:\n"
        "                f.seek(0, os.SEEK_END)\n"
        "                f.seek(max(0, f.tell() - 8192))\n"
        "            except OSError:\n"
        "                pass\n"
        "            window = f.read().decode('utf-8', 'replace')\n"
        "    except OSError:\n"
        "        return False\n"
        "    marker = _attempt_marker(attempt)\n"
        "    if marker not in window:\n"
        "        return False\n"
        "    return DUPLICATE_GUARD_MESSAGE in window.rsplit(marker, 1)[1]\n"
        # Poll for a fresh heartbeat instead of sampling once after a fixed
        # sleep: a slow MCP boot writes its first heartbeat well after
        # HEALTH_CHECK_WAIT (incident 2026-08-19) and was being declared dead.
        # The poll is bounded by HEALTH_CHECK_BUDGET so the watcher still
        # terminates, and returns early once this attempt's own stderr proves
        # the launch was refused by the duplicate guard and no heartbeat is
        # coming, after one settle interval so that launch finishes flushing.
        # Freshness is safe to test immediately: the attempt only
        # reaches here after the is_alive() gate proved the heartbeat was
        # missing or at least 30s old, so anything younger than
        # HEALTH_CHECK_WAIT + 10 was written by the process just started.
        "def _await_fresh_heartbeat(attempt):\n"
        "    started = time.time()\n"
        "    deadline = started + HEALTH_CHECK_BUDGET\n"
        "    guard_seen = False\n"
        "    while True:\n"
        "        age = advanced_heartbeat_age()\n"
        "        if age is not None and age < HEALTH_CHECK_WAIT + 10:\n"
        "            return round(time.time() - started, 3)\n"
        "        if guard_seen:\n"
        "            return None\n"
        "        if _duplicate_guard_seen(attempt):\n"
        # Settle for one poll interval before giving up: the refused launch
        # writes its 'PID <n>' line immediately after the guard line, and the
        # caller reads that tail to identify the duplicate.
        "            guard_seen = True\n"
        "            time.sleep(WATCHER_POLL_INTERVAL)\n"
        "            continue\n"
        "        remaining = deadline - time.time()\n"
        "        if remaining <= 0:\n"
        "            return None\n"
        "        time.sleep(min(WATCHER_POLL_INTERVAL, remaining))\n"
        "def _extract_duplicate_pid(stderr_tail):\n"
        "    for line in stderr_tail.splitlines():\n"
        "        line = line.strip()\n"
        "        if not line.startswith('PID '):\n"
        "            continue\n"
        "        parts = line.split(None, 2)\n"
        "        if len(parts) >= 2 and parts[1].rstrip(':').isdigit():\n"
        "            return int(parts[1].rstrip(':'))\n"
        "    return None\n"
        "from lingtai.kernel.process_match import match_agent_run\n"
        "def _process_observation(pid):\n"
        "    return _process_mechanism().observe(pid)\n"
        "def _is_same_agent_run(pid, observation=None):\n"
        "    if not pid or pid == os.getpid():\n"
        "        return False\n"
        "    if observation is None:\n"
        "        observation = _process_observation(pid)\n"
        "    if observation is None or not _process_mechanism().is_alive(observation):\n"
        "        return False\n"
        "    cmdline = observation.command_line\n"
        "    return match_agent_run(cmdline, wd) is not None\n"
        "def _cleanup_stale_duplicate(stderr_tail, attempt):\n"
        "    pid = _extract_duplicate_pid(stderr_tail)\n"
        "    failure_state['last_pid'] = pid\n"
        "    failure_state['last_duplicate_pid'] = pid\n"
        "    failure_state['last_cleanup_action'] = 'inspect_duplicate_guard'\n"
        "    observation = _process_observation(pid) if pid and pid != os.getpid() else None\n"
        "    if not _is_same_agent_run(pid, observation):\n"
        "        failure_state['last_cleanup_result'] = 'skipped_not_same_agent'\n"
        "        return False\n"
        "    age = heartbeat_age()\n"
        "    failure_state['last_heartbeat_age'] = age\n"
        "    failure_state['last_heartbeat_status'] = 'fresh' if age is not None and age < 30 else ('stale' if age is not None else 'missing')\n"
        "    if advanced_heartbeat_age() is not None and age < 60:\n"
        "        log('refresh_watcher_duplicate_alive', attempt=attempt, pid=pid, heartbeat_age=age)\n"
        "        failure_state['last_cleanup_result'] = 'skipped_fresh_heartbeat'\n"
        "        return False\n"
        "    cmdline = observation.command_line\n"
        "    log('refresh_watcher_stale_duplicate_terminate', attempt=attempt, pid=pid,\n"
        "        heartbeat_age=age, cmdline=cmdline[-300:])\n"
        "    failure_state['last_cleanup_action'] = 'terminate_stale_duplicate'\n"
        "    try:\n"
        "        _process_mechanism().graceful_stop(observation)\n"
        "    except Exception as e:\n"
        "        log('refresh_watcher_stale_duplicate_term_error', attempt=attempt,\n"
        "            pid=pid, error=str(e))\n"
        "        failure_state['last_cleanup_result'] = 'sigterm_error'\n"
        "        failure_state['last_cleanup_error'] = str(e)\n"
        "        return False\n"
        "    deadline = time.time() + 5\n"
        "    while time.time() < deadline:\n"
        "        if not _process_mechanism().is_alive(observation):\n"
        "            log('refresh_watcher_stale_duplicate_gone', attempt=attempt, pid=pid)\n"
        "            failure_state['last_cleanup_result'] = 'terminated'\n"
        "            return True\n"
        "        time.sleep(0.2)\n"
        "    try:\n"
        "        _process_mechanism().force_stop(observation)\n"
        "        log('refresh_watcher_stale_duplicate_killed', attempt=attempt, pid=pid)\n"
        "        failure_state['last_cleanup_result'] = 'sigkill_sent'\n"
        "        return True\n"
        "    except Exception as e:\n"
        "        log('refresh_watcher_stale_duplicate_kill_error', attempt=attempt,\n"
        "            pid=pid, error=str(e))\n"
        "        failure_state['last_cleanup_result'] = 'sigkill_error'\n"
        "        failure_state['last_cleanup_error'] = str(e)\n"
        "        return False\n"
        # graceful_stop/force_stop only *request* the duplicate's exit. Starting
        # the next relaunch while the duplicate still holds the working directory
        # reproduces the same guard and burns another attempt (incident
        # 2026-08-19), so wait a bounded DUPLICATE_EXIT_WAIT before retrying.
        #
        # The blocking condition is re-checked with the same canonical
        # same-agent-run guard cleanup used, not with a bare liveness probe: the
        # duplicate is frequently a process this very watcher launched on an
        # earlier attempt, and the watcher never reaps its children, so after a
        # SIGKILL its PID survives as a zombie that a liveness probe would report
        # alive forever. A zombie's process-table command line no longer matches
        # an agent run for this working directory, so it correctly reads as gone.
        #
        # If a live duplicate outlives the wait, record that in failure_state and
        # let the loop retry anyway rather than hanging.
        "def _await_duplicate_exit(attempt, pid):\n"
        "    if not pid:\n"
        "        return True\n"
        "    deadline = time.time() + DUPLICATE_EXIT_WAIT\n"
        "    while True:\n"
        "        if not _is_same_agent_run(pid):\n"
        "            return True\n"
        "        remaining = deadline - time.time()\n"
        "        if remaining <= 0:\n"
        "            log('refresh_watcher_stale_duplicate_still_alive', attempt=attempt,\n"
        "                pid=pid, waited=DUPLICATE_EXIT_WAIT)\n"
        "            failure_state['last_cleanup_result'] = 'still_alive'\n"
        "            return False\n"
        "        time.sleep(min(WATCHER_POLL_INTERVAL, remaining))\n"
    ) + _render_body()


def _render_body() -> str:
    """Handshake, relaunch loop, and terminal publication, in one ``try``.

    Deliberate exits go through ``_exit``; anything else that escapes —
    including an unexpected ``SystemExit`` from an injected mechanism — is
    recorded once, settles ``.refresh.taken``, is tagged for the entrypoint,
    and keeps a nonzero status (zero/None would be a false success).
    """
    body = (
        "log('refresh_watcher_start')\n"
        "# Phase 1: wait for .refresh.taken\n"
        "while not os.path.exists(taken) and time.time() < deadline:\n"
        "    time.sleep(0.5)\n"
        "if not os.path.exists(taken):\n"
        "    log('refresh_watcher_timeout', phase='ack')\n"
        "    _settle_taken('ack_timeout')\n"
        "    _exit(1)\n"
        "log('refresh_watcher_ack')\n"
        "# Phase 2: wait for the workdir lease to be free. A lingering lock\n"
        "# pathname is not proof it is held (a poisoned hard exit dies before\n"
        "# release), so probe the lease Port while the path remains.\n"
        "released = None\n"
        "while time.time() < deadline and released is None:\n"
        "    if not os.path.exists(lock):\n"
        "        released = 'path_cleared'\n"
        "    else:\n"
        "        lease = _workdir_lease()\n"
        "        try:\n"
        "            lease.acquire(0)\n"
        "        except RuntimeError:\n"
        "            time.sleep(0.5)\n"
        "        else:\n"
        "            lease.release()\n"
        "            released = 'lease_probe'\n"
        "if released is None:\n"
        "    log('refresh_watcher_timeout', phase='lock')\n"
        "    _settle_taken('lock_timeout')\n"
        "    _exit(1)\n"
        "log('refresh_watcher_lock_released', via=released)\n"
        "hb_baseline = heartbeat_ts()\n"
        "# Phase 3: relaunch with health check and retry\n"
        "for attempt in range(1, MAX_ATTEMPTS + 1):\n"
        "    # Check if already alive before relaunching\n"
        # A young baseline may be a live owner this watcher did not launch:
        # give it one window to advance before treating it as dead.
        "    if attempt == 1 and hb_baseline is not None and time.time() - hb_baseline < 30:\n"
        "        until = time.time() + ALREADY_ALIVE_OBSERVE\n"
        "        while not is_alive() and time.time() < until:\n"
        "            time.sleep(WATCHER_POLL_INTERVAL)\n"
        "    if is_alive():\n"
        "        log('refresh_watcher_already_alive', attempt=attempt)\n"
        "        _settle_taken('already_alive')\n"
        "        _exit(0)\n"
        "    # Clean signal files so the new process boots cleanly (like CPR)\n"
        "    for sig in ('.suspend', '.sleep', '.interrupt'):\n"
        "        try:\n"
        "            os.unlink(os.path.join(wd, sig))\n"
        "        except OSError:\n"
        "            pass\n"
        "    log('refresh_watcher_relaunch', attempt=attempt)\n"
        "    try:\n"
        "        with open(stderr_log, 'a') as serr:\n"
        "            serr.write(_attempt_marker(attempt) + '\\n')\n"
        "            serr.flush()\n"
        "        proc = _process_mechanism().start_agent(cmd, stderr_log)\n"
        "    except Exception as e:\n"
        "        log('refresh_watcher_relaunch_error', attempt=attempt, error=str(e))\n"
        "        hb_age, hb_status = _heartbeat_snapshot()\n"
        "        failure_state['last_heartbeat_age'] = hb_age\n"
        "        failure_state['last_heartbeat_status'] = hb_status\n"
        "        failure_state['last_stderr_tail'] = _read_stderr_tail()\n"
        "        failure_state['last_cleanup_action'] = 'not_applicable'\n"
        "        failure_state['last_cleanup_result'] = 'launch_error'\n"
        "        failure_state['last_cleanup_error'] = None\n"
        "        failure_state['last_relaunch_error'] = str(e)\n"
        "        if attempt < MAX_ATTEMPTS:\n"
        "            time.sleep(HEALTH_CHECK_WAIT)\n"
        "        continue\n"
        "    log('refresh_watcher_relaunched', attempt=attempt, pid=proc.pid)\n"
        "    failure_state['last_relaunch_pid'] = proc.pid\n"
        "    failure_state['last_relaunch_error'] = None\n"
        "    # Wait for the new process to start writing heartbeat\n"
        "    heartbeat_wait = _await_fresh_heartbeat(attempt)\n"
        "    if heartbeat_wait is not None:\n"
        "        log('refresh_watcher_success', attempt=attempt, pid=proc.pid,\n"
        "            heartbeat_wait=heartbeat_wait)\n"
        "        _settle_taken('relaunch_success')\n"
        "        _exit(0)\n"
        "    # Process not alive — log failure and retry\n"
        "    stderr_tail = ''\n"
        "    try:\n"
        "        with open(stderr_log, encoding='utf-8', errors='replace') as f:\n"
        "            lines = f.readlines()\n"
        "            stderr_tail = ''.join(lines[-20:])\n"
        "    except OSError:\n"
        "        pass\n"
        "    hb_age, hb_status = _heartbeat_snapshot()\n"
        "    failure_state['last_heartbeat_age'] = hb_age\n"
        "    failure_state['last_heartbeat_status'] = hb_status\n"
        "    failure_state['last_stderr_tail'] = _bounded(stderr_tail)\n"
        "    failure_state['last_cleanup_action'] = 'not_applicable'\n"
        "    failure_state['last_cleanup_result'] = 'no_duplicate_guard'\n"
        "    failure_state['last_cleanup_error'] = None\n"
        "    log('refresh_watcher_relaunch_dead', attempt=attempt, pid=proc.pid,\n"
        "        stderr_tail=stderr_tail[-500:])\n"
        "    if DUPLICATE_GUARD_MESSAGE in stderr_tail:\n"
        "        if _cleanup_stale_duplicate(stderr_tail, attempt):\n"
        "            _await_duplicate_exit(attempt, failure_state['last_duplicate_pid'])\n"
        "alert_id, meta = _publish_refresh_failed_permanent()\n"
        "_settle_taken('refresh_failed_permanent')\n"
        "log('refresh_failed_permanent', alert_id=alert_id, **meta)\n"
        "_exit(1)\n"
    )
    return (
        "deadline = time.time() + 60\n"
        "try:\n"
        + textwrap.indent(body, "    ")
        + "except SystemExit as _e:\n"
        "    setattr(_e, HANDLED, True)\n"
        "    if _deliberate:\n"
        "        raise\n"
        "    log('refresh_watcher_exception', phase='policy', exception='SystemExit',\n"
        "        exit_code=_redact_bounded(repr(_e.code)))\n"
        "    _settle_taken('watcher_exception')\n"
        "    if not _e.code:\n"
        "        _failed = SystemExit(1)\n"
        "        setattr(_failed, HANDLED, True)\n"
        "        raise _failed from _e\n"
        "    raise\n"
        "except BaseException as _e:\n"
        "    setattr(_e, HANDLED, True)\n"
        "    log('refresh_watcher_exception', phase='policy', exception=type(_e).__name__,\n"
        "        error=_redact_bounded(repr(_e)))\n"
        "    _settle_taken('watcher_exception')\n"
        "    raise\n"
    )


def watcher_failure_to_raise(request: RefreshWatcherRequest, exc: BaseException) -> BaseException:
    """Entrypoint fail-safe for a failure the policy did not handle itself.

    Only reached with a decoded request (before ``decode_request`` succeeds
    there is no trusted ``taken_path`` and nothing is cleaned). A failure the
    policy already recorded/settled (tagged ``WATCHER_HANDLED_ATTR``) is
    returned untouched. Otherwise the marker is settled, one redacted
    ``refresh_watcher_exception`` event is appended, and the exception to
    raise is returned — a zero/None ``SystemExit`` becomes ``SystemExit(1)``
    so an unexpected terminal failure never reports success. Never raises.
    """
    if getattr(exc, WATCHER_HANDLED_ATTR, False):
        return exc
    try:
        from lingtai.kernel.trace_redaction import redact_for_trajectory

        entry = {
            "type": "refresh_watcher_exception", "phase": "entrypoint",
            "address": request.address, "agent_name": request.agent_name,
            "ts": time.time(), "exception": type(exc).__name__,
            "error": repr(exc)[-STDERR_TAIL_CHARS:],
        }
        try:
            entry = redact_for_trajectory(entry)
        except Exception:
            entry = {k: v for k, v in entry.items() if k != "error"} | {"redaction_unavailable": True}
        events = [entry]
        try:
            os.unlink(request.taken_path)
            events.append({"type": "refresh_taken_marker_cleared", "reason": "watcher_exception",
                           "address": request.address, "agent_name": request.agent_name,
                           "ts": time.time()})
        except OSError:
            pass
        os.makedirs(os.path.dirname(request.events_path), exist_ok=True)
        with open(request.events_path, "a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
    except Exception:
        pass
    if isinstance(exc, SystemExit) and not exc.code:
        return SystemExit(1)
    return exc


__all__ = [
    "render_watcher_script",
    "watcher_failure_to_raise",
    "WATCHER_HANDLED_ATTR",
    "ALREADY_ALIVE_OBSERVE",
    "MAX_ATTEMPTS",
    "HEALTH_CHECK_WAIT",
    "HEALTH_CHECK_BUDGET",
    "WATCHER_POLL_INTERVAL",
    "DUPLICATE_EXIT_WAIT",
    "STDERR_TAIL_CHARS",
    "DUPLICATE_GUARD_MESSAGE",
]
