"""Shared pytest fixtures for LingTai kernel tests."""

from __future__ import annotations

import pytest

from ._agent_dir_helpers import make_agent_dir as _make_agent_dir


@pytest.fixture
def make_agent_dir():
    """Factory fixture: create a minimal agent working dir.

    Returns the :func:`tests._agent_dir_helpers.make_agent_dir` callable so a
    single test can build several agent dirs with different shapes (heartbeat,
    human, mailbox, …).
    """
    return _make_agent_dir


@pytest.fixture(autouse=True)
def _isolate_llm_adapter_registry():
    """Keep provider registration mutations local to each test."""

    from lingtai.llm.service import LLMService

    snapshot = dict(LLMService._adapter_registry)
    yield
    LLMService._adapter_registry.clear()
    LLMService._adapter_registry.update(snapshot)


@pytest.fixture(autouse=True)
def _isolate_runtime_identity_cache():
    """Keep process-cached runtime identity local to each test."""

    from lingtai.kernel.runtime_identity import runtime_identity

    runtime_identity.cache_clear()
    yield
    runtime_identity.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_cache_miss_budget_env(monkeypatch):
    """Keep the suite hermetic w.r.t. the live cache-miss budget env override.

    The System-owned resolver reads ``LINGTAI_CACHE_MISS_BUDGET`` live, so an
    ambient value (an operator's shell, or an agent's ``env_file``) would
    otherwise leak into budget assertions. Tests that exercise the override opt
    back in with ``monkeypatch.setenv``.
    """

    monkeypatch.delenv("LINGTAI_CACHE_MISS_BUDGET", raising=False)


@pytest.fixture(autouse=True)
def _isolate_streaming_env(monkeypatch):
    """Keep the suite hermetic w.r.t. the live streaming env switch.

    ``LINGTAI_STREAMING`` is read by the System v2 runtime-policy resolver the
    production CLI composes with, so an ambient value would otherwise leak into
    CLI wiring assertions. Tests that exercise the switch opt back in with
    ``monkeypatch.setenv``.
    """

    monkeypatch.delenv("LINGTAI_STREAMING", raising=False)


@pytest.fixture(autouse=True)
def _isolate_notification_dismiss_guards():
    """Keep generic notification-dismiss guard registration test-local."""

    from lingtai.kernel.notifications import _GENERIC_DISMISS_GUARDED

    snapshot = dict(_GENERIC_DISMISS_GUARDED)
    yield
    _GENERIC_DISMISS_GUARDED.clear()
    _GENERIC_DISMISS_GUARDED.update(snapshot)


@pytest.fixture(autouse=True)
def _isolate_notification_hook_registry():
    """Keep the module-level hook registry mirror test-local.

    Hook add/drop/edit actions and ``sync_hook_registry`` mutate the
    process-global ``_REGISTERED_HOOK_CHANNELS`` / ``_HOOK_REGISTRY_SEEDED`` /
    ``_BLOCKED_CHANNEL_WARNED`` state (mirrored from each agent's
    ``.notification/hooks.json``).  Without isolation a hook registered by one
    test would keep its channel allowlisted (and the warn-and-flag dedupe
    marker set) for every later test in the same process.
    """

    from lingtai.kernel.notifications import (
        _BLOCKED_CHANNEL_WARNED,
        _HOOK_REGISTRY_SEEDED,
        _HOOK_REGISTRY_STAT,
        _REGISTERED_HOOK_CHANNELS,
        _invalidate_allow_predicates,
    )

    snapshot_channels = {
        key: set(channels) for key, channels in _REGISTERED_HOOK_CHANNELS.items()
    }
    snapshot_seeded = set(_HOOK_REGISTRY_SEEDED)
    snapshot_stat = dict(_HOOK_REGISTRY_STAT)
    snapshot_warned = {
        key: set(channels) for key, channels in _BLOCKED_CHANNEL_WARNED.items()
    }
    yield
    _REGISTERED_HOOK_CHANNELS.clear()
    _REGISTERED_HOOK_CHANNELS.update(
        {key: set(channels) for key, channels in snapshot_channels.items()}
    )
    _HOOK_REGISTRY_SEEDED.clear()
    _HOOK_REGISTRY_SEEDED.update(snapshot_seeded)
    _HOOK_REGISTRY_STAT.clear()
    _HOOK_REGISTRY_STAT.update(snapshot_stat)
    _BLOCKED_CHANNEL_WARNED.clear()
    _BLOCKED_CHANNEL_WARNED.update(
        {key: set(channels) for key, channels in snapshot_warned.items()}
    )
    _invalidate_allow_predicates()


@pytest.fixture(autouse=True)
def _reset_package_logging():
    """Keep stdlib-logging handler state from leaking across tests.

    ``setup_logging``/``get_logger`` configure a process-global "lingtai"
    logger; without a reset, a FileHandler attached by one test would stay
    attached for every subsequent test in the run (writing to a stale path).
    """

    from lingtai.kernel.logging import reset_logging

    reset_logging()
    yield
    reset_logging()
