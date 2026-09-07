"""Unified ``web`` capability: search, static browse, settings, and manual.

This retained package is the composition owner.  Search providers remain lazy
internal adapters, while the tested browser Core/Port stays in
``lingtai.tools.browser`` and is never registered as a separate public tool.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

from lingtai.kernel.tool_plugin import (
    BoundToolPlugin,
    HostPortError,
    ToolPluginDeclaration,
    ToolPluginDeclarationError,
)

from .._manual import load_installed_manual
from ..browser.core import BrowserEngine
from ..tool_family import ChildTool, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child
from ._spill import spill_if_over_threshold
from .settings import (
    OutputSettingsSnapshot,
    SettingsSnapshot,
    WEB_ENGINE_ENV,
    WEB_MAX_CHARS_ENV,
    build_settings_provider,
    current_setting,
    read_output_settings,
    read_settings,
    valid_engine_name,
)

if TYPE_CHECKING:
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.tool_plugin import (
        ProviderIdentityPort,
        ToolPluginHost,
        WorkdirPort,
    )
    from ..browser.port import BrowserPort

# MiniMax and Zhipu are no longer built-in `web` providers (see
# src/lingtai/tools/mcp/skills/mcp-manual/reference/third-party-and-legacy.md for the
# skill-owned MCP route). Anthropic and Gemini are explicit opt-in only,
# gated on canonical backend identity — never an implicit built-in default.
PROVIDERS = {
    "providers": ["duckduckgo", "gemini", "anthropic", "openai"],
    "default": "duckduckgo",
    "fallback_on_inherit": "duckduckgo",
}

# Named provider slugs retired from built-in admission by this product
# decision (as opposed to a genuinely unrecognized/inherited legacy name,
# which keeps the pre-existing DuckDuckGo legacy_fallback_from behavior
# below). Selecting one of these must fail explicitly and actionably at
# composition time — never a silent DuckDuckGo substitution — per Contract
# item 9 and repair item 3.
_RETIRED_PROVIDERS = frozenset({"minimax", "zhipu"})


class RetiredProviderError(ValueError):
    """A composition kwarg named a provider retired from built-in admission.

    Reserved for MiniMax/Zhipu (``_RETIRED_PROVIDERS``) — providers that no
    longer exist as a `web` built-in at all. Anthropic and Gemini are still
    fully active, admitted, canonical providers; a composition kwarg
    attempting to select either through a forbidden route raises the
    distinct :class:`SettingsOnlyProviderError` instead, never this class.
    """


class SettingsOnlyProviderError(ValueError):
    """A composition kwarg tried to select a settings-only canonical provider.

    Raised when ``provider=``/``default_engine=`` (or an ``engines={}``-only
    engine set with no ``duckduckgo``/``openai`` fallback) would otherwise
    select Anthropic or Gemini — both fully active, canonical, currently
    admitted providers, just restricted to explicit opt-in through the
    hot-read ``search.engine`` environment/document setting plus canonical-backend
    eligibility (never this composition-time route). Distinct from
    :class:`RetiredProviderError`, which is reserved for a provider retired
    from admission entirely (MiniMax, Zhipu) — Anthropic/Gemini are never
    "retired" and must never be described as such in error text, tests, or
    docs.
    """


# Explicit-opt-in engines: admitted only through the hot-read search.engine
# environment/document setting, and only when the current Agent's LLM
# backend truthfully IS that same canonical provider. Never selectable via
# the flat ``provider=``/``default_engine=`` composition kwargs — those are
# rejected outright at composition time (see ``_specs_from_kwargs``).
_BACKEND_GATED_ENGINES = frozenset({"anthropic", "gemini"})

# The standard, publicly-documented API-key environment variable for each
# canonical built-in web-search spec
# (``src/lingtai/tools/web_search/__init__.py:_CANONICAL_API_KEY_ENV``). The
# no-config built-in default spec set below reads only these — never the current
# Agent's own live ``agent.service`` credentials or any private LLM-adapter
# attribute.
_CANONICAL_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _same_provider_identity(provider_identity: "ProviderIdentityPort", name: str) -> bool:
    """Return whether the narrow provider port truthfully IS canonical *name*.

    Exact equality against the host-provided canonical provider label — the one
    registered name bound to a provider's own dedicated adapter factory
    (``LLMService.register_adapter`` in ``lingtai.llm._register``). Aliased,
    CLI-login, or wire-compatible names (``claude-code``/``claude_code``,
    ``custom``, ``openrouter``, ``deepseek``, ``glm``/``zhipu``, ``grok``,
    ``qwen``, ``kimi``, ``codex``/``codex-pool``/``codex_pool``) never
    register under ``"anthropic"`` or ``"gemini"``, so exact equality is the
    smallest truthful boundary — no substring, alias, or model-name guess.
    Private to ``web``: only this capability's Anthropic/Gemini opt-in needs
    this predicate today, so it stays unexported rather than becoming a
    speculative cross-tool identity API.
    """
    if name not in _BACKEND_GATED_ENGINES:
        return False
    provider = provider_identity.provider
    return isinstance(provider, str) and provider.lower() == name


_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Query for current web sources."},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_BROWSE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": ["string", "null"],
            "description": "Public HTTP(S) URL; null only when link_ref is supplied.",
        },
        "link_ref": {
            "type": ["string", "null"],
            "description": "Same-Agent search reference; null only when URL is supplied.",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": "Continuation cursor for that URL/link_ref; null when unused.",
        },
        "extract": {
            "type": ["string", "null"],
            "enum": ["article", None],
            "description": "Use article extraction, or null for default.",
        },
        "max_chars": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 100000,
            "description": "Per-call delivery threshold override (1–100000); null uses settings/web.json.",
        },
    },
    "required": ["url", "link_ref", "cursor", "extract", "max_chars"],
    "additionalProperties": False,
}

# The single source of truth for web's operational children: one ``(name, schema,
# title)`` triple per child, consumed both by the module-level schema-only
# family below and by ``WebManager.__init__``, which binds real handlers to
# the same specs. The reserved ``settings`` and ``manual`` children are not
# listed here: settings is injected only through the read-only provider seam;
# manual's schema is the owner-exported ``MANUAL_INPUT_SCHEMA`` and its real
# child comes from ``build_manual_child``.
_CHILD_SPECS: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("search", _SEARCH_INPUT_SCHEMA, "search input"),
    ("browse", _BROWSE_INPUT_SCHEMA, "browse input"),
)


def _schema_only_family() -> ToolFamily:
    # A throwaway ``ToolFamily`` used only to compose the model-facing schema
    # and to prove the public child registry has no duplicate or
    # reserved-name collision (``ToolFamilyError`` would raise here, at
    # import time, rather than shipping silently). ``WebManager`` builds its
    # own per-instance ``ToolFamily`` in ``__init__`` with real handlers
    # bound to that instance; this module-level one never dispatches.
    def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("the module-level schema-only ToolFamily never dispatches")

    def _unused_settings() -> tuple[Any, ...]:
        raise AssertionError("the module-level schema-only settings provider never dispatches")

    return ToolFamily(
        DECLARATION.name,
        [
            *(ChildTool(name, schema, _unused, title=title) for name, schema, title in _CHILD_SPECS),
            ChildTool("manual", DECLARATION.manual_input_schema, _unused, title="manual input"),
        ],
        settings_provider=_unused_settings,
    )


def get_description(lang: str = "en") -> str:
    return (
        "Search current sources with web(action='search', input={'query':'...'}), then browse a "
        "returned link_ref or public HTTP(S) URL with web(action='browse', input={...}). Use "
        "web(action='settings', input={}) for read-only config and web(action='manual', input={}) "
        "for procedures; browse optionals are JSON null when unused and complete content is inline "
        "or a full artifact."
    )


def _deferred_bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Resolve the static declaration's binder after this module defines it."""
    return _bind(host)


#: Static declaration of the official public ``web`` tool.  The deferred binder
#: keeps this import-time declaration independent of the per-Agent
#: :class:`WebComposition` that ``setup`` grants to this declaration alone as
#: the Web-owned ``web_runtime`` host port (through ``extra_ports_for``, the
#: same declaration-scoped seam Email, File, Shell, and Vision use).
#: ``provider_identity`` is the narrow read-only canonical provider label that
#: gates the explicit Anthropic/Gemini opt-in; ``workdir`` roots settings,
#: artifacts, and the installed manual.
DECLARATION = ToolPluginDeclaration(
    name="web",
    actions=tuple(name for name, _schema, _title in _CHILD_SPECS),
    input_schemas={name: schema for name, schema, _title in _CHILD_SPECS},
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="web",
    description=get_description(),
    binder=_deferred_bind,
    requires=("workdir", "web_runtime", "provider_identity"),
    glossary_package=__package__,
    settings=True,
)


#: Schema-only construction proves the declaration's fixed family inventory at
#: import; it never dispatches and has no per-Agent runtime.
_FAMILY = _schema_only_family()


def get_schema(lang: str = "en") -> dict[str, Any]:
    # Composed by the generic ToolFamily infra from each child's own
    # canonical ``input_schema`` (``_SEARCH_INPUT_SCHEMA`` etc. above), rather
    # than hand-assembled — verified field-equivalent to the pre-migration
    # schema, except the documented authorized differences, by
    # ``tests/test_tool_family_web_migration_parity.py``.
    return _FAMILY.build_schema()


@dataclass(frozen=True, slots=True)
class _EngineSpec:
    name: str
    provider: str | None = None
    service: Any | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    model: str | None = None
    extra: Mapping[str, Any] = ()
    legacy_fallback_from: str | None = None


class WebManager:
    """One per-Agent dispatcher and search/browse state owner."""

    def __init__(
        self,
        workdir: "WorkdirPort",
        provider_identity: "ProviderIdentityPort",
        browser_port: "BrowserPort | None" = None,
        *,
        specs: Mapping[str, _EngineSpec] | None = None,
        default_engine: str | None = None,
        default_source: str = "built_in_default",
        search_service: Any | None = None,
        legacy_fallback_from: str | None = None,
        provider_current: str | None = None,
        model_current: str | None = None,
        api_key_configured: bool | None = None,
    ) -> None:
        self._workdir = workdir
        self._provider_identity = provider_identity
        if browser_port is None:
            from lingtai.adapters.browser_transport import VettedHttpTransport
            browser_port = VettedHttpTransport()
        self._engine = BrowserEngine(browser_port)
        if specs is None:
            specs = {"duckduckgo": _EngineSpec("duckduckgo", provider="duckduckgo", service=search_service)}
        self._specs = dict(specs)  # immutable spec values; service cache is local
        self._default_engine = default_engine or next(iter(self._specs), None)
        self._default_source = default_source
        self._legacy_fallback_from = legacy_fallback_from
        self._services: dict[str, Any] = {}
        self._service_errors: dict[str, str] = {}
        settings_provider = build_settings_provider(
            workdir,
            self._specs,
            self._default_engine_now,
            self._default_source,
            provider_current=provider_current,
            model_current=model_current,
            api_key_configured=api_key_configured,
            credential_configured=self._credential_configured,
        )
        handlers = {"search": self._dispatch_search, "browse": self._dispatch_browse}
        self._family = ToolFamily(
            DECLARATION.name,
            [
                *(
                    ChildTool(name, schema, handlers[name], title=title)
                    for name, schema, title in _CHILD_SPECS
                ),
                # Registered directly, unwrapped: ``ToolFamily.handle()`` must
                # dispatch this child's own canonical MCP-compatible result
                # verbatim for ``action="manual"`` (no double wrap). Web's
                # flat public shape is reconstructed from that canonical
                # result strictly *after* ``self._family.handle(...)``
                # returns, in ``handle()`` below — never inside a registered
                # child.
                build_manual_child(workdir, DECLARATION.manual),
            ],
            settings_provider=settings_provider,
        )

    @property
    def browser_engine(self) -> BrowserEngine:
        return self._engine

    def _status(self, spec: _EngineSpec) -> str:
        if spec.service is not None or spec.provider == "duckduckgo":
            return "available"
        if spec.api_key:
            return "available"
        if spec.api_key_env and os.environ.get(spec.api_key_env):
            return "available"
        if spec.provider and spec.provider != "duckduckgo":
            return "credential_missing"
        return "unavailable"

    def _credential_configured(self, provider: str) -> bool:
        """Report the route the manager would use now, or its cached service."""
        for engine_name, spec in self._specs.items():
            if engine_name != provider and spec.provider != provider:
                continue
            if engine_name in self._services or spec.service is not None:
                return True
            if spec.api_key_env is not None and os.environ.get(spec.api_key_env):
                return True
            if spec.api_key:
                return True
        return False

    @staticmethod
    def _output_setting_block(snapshot: OutputSettingsSnapshot) -> dict[str, Any]:
        block: dict[str, Any] = {
            "value": snapshot.max_chars,
            "source": snapshot.source,
            "settings_revision": snapshot.revision,
            "settings_hash": snapshot.digest,
        }
        if snapshot.error:
            block["settings_error"] = snapshot.error
        return block

    def _diagnostics(self, snapshot: SettingsSnapshot, output_snapshot: OutputSettingsSnapshot) -> dict[str, Any]:
        statuses = {
            name: (
                "initialization_failed"
                if name in self._service_errors
                else self._status(spec)
            )
            for name, spec in self._specs.items()
        }
        block = current_setting(snapshot, self._specs, statuses)
        if self._legacy_fallback_from:
            block["legacy_fallback_from"] = self._legacy_fallback_from[:64]
            block["legacy_fallback"] = "operator-config-only"
        block["output_max_chars"] = self._output_setting_block(output_snapshot)
        return block

    def _default_engine_now(self) -> str | None:
        # The built-in default (no operator ``default_engine``/``provider``
        # and no settings-file selection) resolves live, per call: canonical
        # OpenAI Responses Web Search when genuinely available, else
        # DuckDuckGo. An operator-chosen ``default_engine``/``provider`` (a
        # non-``built_in_default`` source) is never overridden here.
        if self._default_source != "built_in_default":
            return self._default_engine
        if "openai" in self._specs and self._status(self._specs["openai"]) == "available":
            return "openai"
        if "duckduckgo" in self._specs:
            return "duckduckgo"
        if self._default_engine in _BACKEND_GATED_ENGINES:
            # The built-in default must never land on a settings-gated
            # engine (Contract item 3/repair item 2) — even one that
            # happened to be first in an operator's ``engines={}`` mapping
            # with no explicit ``default_engine``/``provider`` choice and no
            # ``duckduckgo`` spec composed at all.
            return None
        return self._default_engine

    def _resolve_output_settings(self) -> OutputSettingsSnapshot:
        # Shared by search and browse: both actions consume the same
        # family-owned settings/web.json snapshot for the same call. Manual
        # must never call this — it stays zero-settings-I/O.
        return read_output_settings(self._workdir)

    def _resolve(self) -> tuple[str | None, SettingsSnapshot, OutputSettingsSnapshot, dict[str, Any]]:
        snapshot = read_settings(
            self._workdir, self._specs, self._default_engine_now(), self._default_source
        )
        output_snapshot = self._resolve_output_settings()
        return snapshot.engine, snapshot, output_snapshot, self._diagnostics(snapshot, output_snapshot)

    def _no_settings_diagnostic(self) -> dict[str, Any]:
        # Zero-settings-I/O diagnostic: used by manual (which never reads
        # either settings file) and by every envelope-level/pre-dispatch
        # failure path (invalid argument, unknown action) that never reaches
        # a real action handler. Neither settings/web.search.json nor
        # settings/web.json is read to build this block.
        snapshot = SettingsSnapshot(None, "not_applicable", "not_read", None)
        output_snapshot = OutputSettingsSnapshot(None, "not_applicable", "not_read", None)
        return self._diagnostics(snapshot, output_snapshot)

    def _browse_diagnostic(self, output_snapshot: OutputSettingsSnapshot) -> dict[str, Any]:
        # Browse never reads settings/web.search.json (engine selection is a
        # search-only concern) but does read the shared settings/web.json.
        snapshot = SettingsSnapshot(None, "not_applicable", "not_read", None)
        return self._diagnostics(snapshot, output_snapshot)

    def _failure(self, action: str, snapshot: SettingsSnapshot | None, diagnostic: dict[str, Any], code: str, message: str, **extra: Any) -> dict[str, Any]:
        result = {"status": "failed", "action": action, "error_code": code, "message": message, "current_setting": diagnostic}
        result.update(extra)
        return result

    def _service_for(self, name: str, spec: _EngineSpec) -> Any | None:
        if name in self._services:
            return self._services[name]
        if name in self._service_errors:
            return None
        if spec.service is not None:
            self._services[name] = spec.service
            return spec.service
        if self._status(spec) != "available":
            self._service_errors[name] = "credential_missing"
            return None
        try:
            # The provider factory is deliberately imported and called only on
            # the selected search path; manual/browse never construct one.
            from lingtai.services.websearch import create_search_service
            key = spec.api_key
            if spec.api_key_env:
                key = os.environ.get(spec.api_key_env)
            kwargs = dict(spec.extra) if isinstance(spec.extra, Mapping) else {}
            service = create_search_service(spec.provider or name, api_key=key, model=spec.model, **kwargs)
            self._services[name] = service
            return service
        except Exception as exc:
            self._service_errors[name] = type(exc).__name__[:64]
            return None

    @staticmethod
    def _result_fields(item: Any) -> tuple[str, str, str]:
        if isinstance(item, Mapping):
            return (str(item.get("title", "")), str(item.get("url", item.get("link", ""))), str(item.get("snippet", item.get("content", ""))))
        return (str(getattr(item, "title", "")), str(getattr(item, "url", "")), str(getattr(item, "snippet", "")))

    def _run_service(self, service: Any, query: str) -> list[dict[str, str]]:
        # ``max_results=None`` and no local slicing/per-field truncation: the
        # locked complete-output contract forbids any LingTai-imposed
        # result-count cap or character slice on provider-returned text.
        # ``SearchService.search`` is contracted to return a finite list
        # (services/websearch/__init__.py), so no local iteration ceiling is
        # needed here either — provider/service deadlines and fail-loud
        # cancellation are the only operational bound.
        raw_results = service.search(query, max_results=None)
        if raw_results is None:
            raw_results = []
        results: list[dict[str, str]] = []
        for item in raw_results:
            title, url, snippet = self._result_fields(item)
            if not url:
                # No official source URL for this item (e.g. a provider's
                # bounded synthesized-narrative fallback result with no
                # citation). Preserve it — real, nonempty provider output
                # must stay visible to the Agent — but never fabricate a
                # link_ref for a URL that does not exist.
                if snippet or title:
                    results.append({"title": title, "url": "", "snippet": snippet, "link_ref": None})
                continue
            results.append({"title": title, "url": url, "snippet": snippet, "link_ref": self._engine.refs.add_link_ref(url)})
        return results

    def _duckduckgo_fallback(self, query: str) -> tuple[list[dict[str, str]], str | None]:
        # The one automatic runtime fallback: exactly one DuckDuckGo attempt,
        # for a typed OpenAI provider failure only. DuckDuckGo takes no
        # credentials, so this never touches provider construction/service
        # caching for another engine. If DuckDuckGo itself fails, that is
        # reported as a bounded failure class, never a second retry.
        try:
            spec = self._specs.get("duckduckgo")
            service = spec.service if spec is not None and spec.service is not None else None
            if service is None:
                from lingtai.services.websearch.duckduckgo import DuckDuckGoSearchService
                service = DuckDuckGoSearchService()
            return self._run_service(service, query), None
        except Exception as exc:
            return [], type(exc).__name__[:64]

    def _openai_duckduckgo_fallback(
        self, query: str, openai_failure_class: str, output_snapshot: OutputSettingsSnapshot, diagnostic: dict[str, Any]
    ) -> dict[str, Any]:
        ddg_results, ddg_failure_class = self._duckduckgo_fallback(query)
        if ddg_failure_class is not None:
            return {
                "status": "failed", "action": "search", "error_code": "SEARCH_FAILED",
                "message": "OpenAI web search failed and the DuckDuckGo fallback also failed",
                "current_setting": diagnostic,
                "openai_failure_class": openai_failure_class, "duckduckgo_failure_class": ddg_failure_class,
            }
        comment = f"# OpenAI web search failed ({openai_failure_class}); DuckDuckGo was used as the fallback."
        payload = {
            "status": "ok", "action": "search", "query": query[:2000],
            "comment": comment,
            "engine": "openai", "actual_engine": "duckduckgo",
            "openai_failure_class": openai_failure_class,
            "results": ddg_results, "count": len(ddg_results), "current_setting": diagnostic,
        }
        return self._deliver_search(payload, output_snapshot)

    def _search(
        self,
        args: dict[str, Any],
        snapshot: SettingsSnapshot,
        output_snapshot: OutputSettingsSnapshot,
        diagnostic: dict[str, Any],
    ) -> dict[str, Any]:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._failure("search", snapshot, diagnostic, "INVALID_QUERY", "query must be a non-empty string")
        if output_snapshot.error:
            message = (
                f"{WEB_MAX_CHARS_ENV} is invalid; no search was performed"
                if output_snapshot.source == "environment_error"
                else "settings/web.json is invalid; no search was performed"
            )
            return self._failure(
                "search", snapshot, diagnostic, "WEB_OUTPUT_SETTINGS_INVALID",
                message,
            )
        name = snapshot.engine
        if snapshot.error:
            message = (
                f"{WEB_ENGINE_ENV} is invalid; no search engine was selected"
                if snapshot.source == "environment_error"
                else "settings/web.search.json is invalid; no search engine was selected"
            )
            return self._failure(
                "search", snapshot, diagnostic, "WEB_SETTINGS_INVALID", message
            )
        if not name or name not in self._specs:
            return self._failure("search", snapshot, diagnostic, "SEARCH_ENGINE_UNAVAILABLE", "the selected search engine is unavailable")
        if name in _BACKEND_GATED_ENGINES:
            if snapshot.source not in {"settings/web.search.json", "environment"}:
                # Anthropic/Gemini are explicit opt-in through a valid
                # hot-read settings/web.search.json or LINGTAI_WEB_ENGINE
                # selection only. A
                # composition-time default_engine/provider can never select
                # them (rejected outright in _specs_from_kwargs), and the
                # no-config built-in default never picks them either — this
                # branch is the last-resort guard against any other route
                # reaching a gated engine name.
                return self._failure(
                    "search", snapshot, diagnostic, "PROVIDER_BACKEND_INELIGIBLE",
                    f"engine {name!r} is explicit opt-in only through Web's engine setting",
                )
            if not _same_provider_identity(self._provider_identity, name):
                # Explicit Anthropic/Gemini opt-in fails explicitly when the
                # current Agent's own LLM backend is not truthfully that same
                # canonical provider — no provider construction, no search
                # call, no silent substitution (settings-selected, not the
                # automatic OpenAI-only runtime fallback in Contract item 7).
                return self._failure(
                    "search", snapshot, diagnostic, "PROVIDER_BACKEND_INELIGIBLE",
                    f"engine {name!r} requires the Agent's own LLM backend to be the canonical {name} API provider",
                )
        spec = self._specs[name]
        if self._status(spec) != "available":
            return self._failure("search", snapshot, diagnostic, "SEARCH_ENGINE_UNAVAILABLE", "the selected search engine is unavailable")
        service = self._service_for(name, spec)
        if service is None:
            # `_service_for` records a bounded internal failure marker. Rebuild
            # diagnostics so this same result does not contradict its error by
            # still advertising the engine as available.
            diagnostic = self._diagnostics(snapshot, output_snapshot)
            return self._failure("search", snapshot, diagnostic, "SEARCH_ENGINE_UNAVAILABLE", "the selected search engine could not be initialized")
        try:
            results = self._run_service(service, query)
            payload = {
                "status": "ok", "action": "search", "query": query[:2000],
                "engine": name, "actual_engine": name, "results": results,
                "count": len(results), "current_setting": diagnostic,
            }
            return self._deliver_search(payload, output_snapshot)
        except Exception as exc:
            from lingtai.services.websearch import SearchProviderError
            from lingtai.services.websearch.openai import OpenAISearchError
            if name == "openai" and isinstance(exc, OpenAISearchError):
                # The one automatic runtime fallback: a *provider-typed*
                # OpenAI failure only (timeout, rate limit, HTTP/SDK error —
                # everything OpenAISearchService itself catches and raises
                # as OpenAISearchError). A bug inside
                # _run_service/_result_fields (a TypeError, an
                # AttributeError from malformed data, ...) is a programming
                # defect, not a provider failure, and falls through to the
                # generic SEARCH_FAILED return below — never silently
                # retried against DuckDuckGo.
                return self._openai_duckduckgo_fallback(query, exc.failure_class, output_snapshot, diagnostic)
            if isinstance(exc, SearchProviderError):
                # A typed Anthropic/Gemini (or any other) provider failure —
                # including Anthropic's official in-body HTTP-200
                # web_search_tool_result_error — never triggers a fallback
                # for any engine except the one explicit OpenAI case above.
                # Only the bounded failure class is exposed; never the raw
                # exception text, request body, or credentials.
                return self._failure(
                    "search", snapshot, diagnostic, "SEARCH_FAILED",
                    "the selected search engine failed",
                    provider_failure_class=exc.failure_class,
                )
            # A non-provider exception (a manager/programming defect) fails
            # the same way but carries no provider-specific failure class.
            return self._failure("search", snapshot, diagnostic, "SEARCH_FAILED", "the selected search engine failed")

    def _deliver_search(self, payload: dict[str, Any], output_snapshot: OutputSettingsSnapshot) -> dict[str, Any]:
        assert output_snapshot.max_chars is not None  # guarded by the caller's output_snapshot.error check
        results = payload["results"]
        serialized = json.dumps(results, ensure_ascii=False, indent=2)
        working_dir = self._workdir.path
        artifact = spill_if_over_threshold(
            content=serialized,
            output_setting=output_snapshot,
            working_dir=working_dir,
            action="search",
            content_scope="provider_response",
            content_kind="search_results",
            format="json",
            extra={
                "query": payload["query"],
                "engine": payload["engine"],
                "actual_engine": payload["actual_engine"],
            },
        )
        if artifact is None:
            payload["delivery"] = "inline"
            payload["content_chars"] = len(serialized)
            return payload
        if artifact.get("status") == "failed":
            failure = {
                "status": "failed", "action": "search", "error_code": "ARTIFACT_WRITE_FAILED",
                "message": artifact["message"], "current_setting": payload["current_setting"],
                "query": payload["query"], "engine": payload["engine"],
            }
            return failure
        spilled: dict[str, Any] = {
            "status": "ok", "action": "search", "query": payload["query"],
            "engine": payload["engine"], "actual_engine": payload["actual_engine"],
            "count": payload["count"], "current_setting": payload["current_setting"],
        }
        for key in ("comment", "openai_failure_class"):
            # The OpenAI->DuckDuckGo fallback promises a top-level comment
            # and bounded openai_failure_class with no spill carve-out
            # (CONTRACT.md, runtime-fallback section): informed substitution
            # must survive the artifact envelope, not just the inline one.
            if key in payload:
                spilled[key] = payload[key]
        spilled.update(artifact)
        return spilled

    def manual(self, diagnostic: dict[str, Any]) -> dict[str, Any]:
        # This path never reads settings/web.search.json and performs no
        # provider construction or search operation, even when settings are
        # malformed: manual does not own that file.
        loaded = load_installed_manual(self._workdir, DECLARATION.manual)
        loaded.update({"action": "manual", "current_setting": diagnostic})
        return loaded

    def _adapt_manual_result(self, mcp_result: dict[str, Any]) -> dict[str, Any]:
        # ``self._family.handle(...)`` has already dispatched to the
        # registered ``manual`` child (``build_manual_child``) and returned
        # its canonical result *verbatim* (no double wrap) — full body at
        # ``content[0].text``, host-local path at
        # ``structuredContent.manual_path`` (the two approved v0.4 ManualTool
        # acceptance fields), plus the loader's truthful ``status``/``error``
        # facts. Web's own public result shape predates that generic
        # contract and must stay exactly
        # ``status``/``manual``/``manual_path``/``action``/``current_setting``
        # (#1058), so this Host-owned adapter runs strictly *after* dispatch,
        # here in ``handle()``, to flatten the canonical child result back to
        # it — never inside a registered child, and never touching
        # search/browse.
        flat: dict[str, Any] = {
            "status": mcp_result.get("status", "ok"),
            "manual": mcp_result["content"][0]["text"],
            "manual_path": mcp_result["structuredContent"]["manual_path"],
            "action": "manual",
            "current_setting": self._no_settings_diagnostic(),
        }
        if "error" in mcp_result:
            flat["error"] = mcp_result["error"]
        return flat

    def _strip_nulls(self, action_args: Mapping[str, Any]) -> dict[str, Any]:
        # Strict OpenAI schemas express optional fields as required nullable
        # properties. Null means absent to the internal action handlers.
        return {key: value for key, value in action_args.items() if value is not None}

    def _dispatch_search(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        dispatch_args = self._strip_nulls(action_input)
        _, snapshot, output_snapshot, diagnostic = self._resolve()
        return self._search(dispatch_args, snapshot, output_snapshot, diagnostic)

    def _dispatch_browse(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        browse_args = self._strip_nulls(action_input)
        output_snapshot = self._resolve_output_settings()
        if output_snapshot.error:
            diagnostic = self._browse_diagnostic(output_snapshot)
            message = (
                f"{WEB_MAX_CHARS_ENV} is invalid; no browse was performed"
                if output_snapshot.source == "environment_error"
                else "settings/web.json is invalid; no browse was performed"
            )
            return self._failure(
                "browse", None, diagnostic, "WEB_OUTPUT_SETTINGS_INVALID",
                message,
            )
        # A present ``max_chars`` is validated by ``BrowserEngine`` itself
        # (its own 1..100000 range, via ``validate_max_chars``) before any
        # call can succeed; if invalid, the call fails with
        # ``INVALID_MAX_CHARS`` below and this override is never applied.
        # Absent/None keeps the shared setting; present overrides the
        # delivery threshold for this call only, per the locked contract.
        call_override = browse_args.get("max_chars")
        delivery_snapshot = (
            output_snapshot if call_override is None
            # The override value comes from this call's own validated input,
            # not from settings/web.json, so it must not carry that file's
            # revision/hash forward — those describe the *shared* setting
            # state, which this call is deliberately not using.
            else replace(
                output_snapshot, max_chars=call_override, source="call_override",
                revision="call_override", digest=None,
            )
        )
        diagnostic = self._browse_diagnostic(delivery_snapshot)
        try:
            result = self._engine.handle(browse_args)
        except Exception:
            result = {"status": "failed", "error_code": "BROWSE_FAILED", "message": "browse failed safely"}
        result["action"] = "browse"
        result["current_setting"] = diagnostic
        if result.get("status") == "ok":
            result = self._deliver_browse(result, delivery_snapshot)
        return result

    def _deliver_browse(self, result: dict[str, Any], output_snapshot: OutputSettingsSnapshot) -> dict[str, Any]:
        assert output_snapshot.max_chars is not None  # guarded by the caller's output_snapshot.error check
        snapshot = self._engine.snapshots.get(result["snapshot_id"])
        if snapshot is None:
            # The snapshot was evicted between the engine's fetch/continue
            # success and this delivery decision (only possible under
            # extreme concurrent pressure on the tiny max_snapshots LRU).
            # The locked complete-output policy forbids ever returning a
            # partial/first-page body, so falling back to the engine's own
            # paginated result (which may carry partial=true/next_cursor)
            # would silently violate it. Fail loud instead: this is a typed,
            # explicit failure, not a degraded success.
            return {
                "status": "failed", "action": "browse",
                "error_code": "BROWSE_SNAPSHOT_UNAVAILABLE",
                "message": (
                    "The fetched page snapshot was no longer available when "
                    "building the complete delivery response; no partial or "
                    "cached content was returned."
                ),
                "current_setting": result["current_setting"],
                "request_id": result.get("request_id"), "snapshot_id": result.get("snapshot_id"),
            }
        complete_text = "".join(block.text for block in snapshot.blocks)
        structured_blocks = [{"id": b.id, "kind": b.kind, "text": b.text} for b in snapshot.blocks]
        # The threshold decision must be measured against the exact canonical
        # serialization of what would actually be returned inline — the
        # structured `blocks` array — not the compact joined-text artifact
        # file representation. Many small blocks accumulate substantial JSON
        # field/structure overhead per block, so the structured serialization
        # can be many times larger than the plain joined text even though the
        # file (written below, if spilled) stays the smaller plain-text form.
        structured_chars = len(json.dumps(structured_blocks, ensure_ascii=False))
        working_dir = self._workdir.path
        artifact = spill_if_over_threshold(
            content=complete_text,
            decision_chars=structured_chars,
            decision_basis="structured_blocks",
            output_setting=output_snapshot,
            working_dir=working_dir,
            action="browse",
            content_scope="fetched_static_document",
            content_kind="page_text",
            format="text",
            extra={
                "requested_url": result.get("requested_url"),
                "final_url": result.get("final_url"),
            },
        )
        if artifact is None:
            # A fresh Browse success must never deliver only a prefix/first
            # page: replace the engine's internally-paginated window with the
            # complete block set and clear pagination fields, so "inline"
            # always means the whole document, not whichever slice the
            # per-call max_chars pagination window happened to produce.
            result = dict(result)
            result["blocks"] = structured_blocks
            result["partial"] = False
            result["next_cursor"] = None
            result["returned_chars"] = len(complete_text)
            result["delivery"] = "inline"
            result["content_chars"] = structured_chars
            return result
        if artifact.get("status") == "failed":
            return {
                "status": "failed", "action": "browse", "error_code": "ARTIFACT_WRITE_FAILED",
                "message": artifact["message"], "current_setting": result["current_setting"],
                "request_id": result.get("request_id"), "snapshot_id": result.get("snapshot_id"),
            }
        # Cursor/pagination concepts stop applying once the complete document
        # is available in one artifact: omit blocks/partial/next_cursor/
        # returned_chars rather than mixing a spilled envelope with a
        # continuable-but-partial inline shape.
        spilled: dict[str, Any] = {
            key: value
            for key, value in result.items()
            if key not in {"blocks", "partial", "next_cursor", "returned_chars"}
        }
        spilled.update(artifact)
        return spilled

    def handle(self, args: dict[str, Any] | None) -> dict[str, Any]:
        # The generic ``ToolFamily`` dispatcher validates ``action``,
        # type-checks and strips root ``summarize``, rejects unknown root
        # fields, and rejects ``input`` keys outside the selected action's own
        # declared schema (schema conformance alone is not the dispatch-time
        # authorization boundary — see ``tools/CONTRACT.md`` "Dispatch and
        # actions") before calling ``_dispatch_search``/``_dispatch_browse``/
        # the registered ``manual`` child's own handler with only that
        # action's own ``input``. ``self._family.handle(...)`` therefore
        # returns the ``manual`` child's canonical
        # ``content``/``structuredContent`` result verbatim (no double wrap)
        # for a successfully dispatched ``action="manual"`` call; adapting
        # that to Web's pre-migration public flat shape is this method's own
        # Host/presentation job, done strictly after dispatch, never inside
        # the registered child. An envelope-level failure (raised before any
        # action handler runs) has no web-specific ``current_setting``
        # diagnostic yet; this stamps one on, matching every action-level
        # failure/success result. The generic dispatcher's own
        # ``ACTION_REQUIRED`` envelope error is genuinely generic (its
        # message lists whatever children a given family registered, and it
        # never had a web-specific ``action`` to echo); Web's pre-migration
        # public contract instead always reported the fixed values below,
        # regardless of the arbitrary string a caller sent, so that
        # normalization happens here — never by changing the generic
        # dispatcher's own canonical error shape.
        action = args.get("action") if isinstance(args, Mapping) else None
        result = self._family.handle(args)
        if action == "settings":
            # The generic SHOW seam owns this exact success/failure ABI; Web's
            # operational presentation fields must not be stamped onto it.
            return result
        if action == "manual" and "content" in result:
            result = self._adapt_manual_result(result)
        elif result.get("error_code") == "ACTION_REQUIRED":
            result["action"] = "unknown"
            result["message"] = "action must be one of search, browse, settings, or manual"
            result["current_setting"] = self._no_settings_diagnostic()
        elif result.get("status") == "failed" and "current_setting" not in result:
            result["action"] = action if isinstance(action, str) else "unknown"
            result["current_setting"] = self._no_settings_diagnostic()
        return result


def _canonical_default_specs() -> dict[str, _EngineSpec]:
    # The real no-config built-in spec set: all four canonical providers,
    # using only each provider's own standard, publicly-documented API-key
    # env var (_CANONICAL_API_KEY_ENV) as the credential source — never the
    # current Agent's own live LLM service credentials or any private
    # LLM-adapter attribute. DuckDuckGo needs no credential. Anthropic/Gemini
    # are present as selectable specs (so their status is honestly reported
    # in diagnostics) but are never chosen by the default resolver — only an
    # explicit search.engine environment/document selection plus canonical-backend
    # eligibility can select them (see WebManager._search).
    return {
        "duckduckgo": _EngineSpec("duckduckgo", provider="duckduckgo"),
        "openai": _EngineSpec("openai", provider="openai", api_key_env=_CANONICAL_API_KEY_ENV["openai"]),
        "anthropic": _EngineSpec("anthropic", provider="anthropic", api_key_env=_CANONICAL_API_KEY_ENV["anthropic"]),
        "gemini": _EngineSpec("gemini", provider="gemini", api_key_env=_CANONICAL_API_KEY_ENV["gemini"]),
    }


def _specs_from_kwargs(
    *, search_service: Any | None, provider: str | None, api_key: str | None,
    api_key_env: str | None, model: str | None, default_engine: str | None,
    engines: Mapping[str, Any] | None, kwargs: Mapping[str, Any],
) -> tuple[dict[str, _EngineSpec], str | None, str, str | None]:
    specs: dict[str, _EngineSpec] = {}
    legacy_fallback_from: str | None = None
    if default_engine is not None and not valid_engine_name(default_engine):
        raise ValueError("web default_engine must be a bounded engine name")
    if default_engine in _RETIRED_PROVIDERS or provider in _RETIRED_PROVIDERS:
        # Retired-by-product-decision providers (minimax, zhipu) must fail
        # explicitly and actionably — never a silent DuckDuckGo substitution
        # (Contract item 9, repair item 3). This is distinct from the
        # pre-existing legacy_fallback_from path below, which covers a
        # genuinely unrecognized/inherited legacy provider name, not one of
        # these two deliberately-retired, previously-admitted names.
        raise RetiredProviderError(
            f"provider {(default_engine or provider)!r} is retired from built-in web search admission; "
            "wire it as a third-party MCP server instead (see "
            "src/lingtai/tools/mcp/skills/mcp-manual/reference/third-party-and-legacy.md)"
        )
    if default_engine in _BACKEND_GATED_ENGINES or provider in _BACKEND_GATED_ENGINES:
        # Anthropic/Gemini are active, fully-admitted canonical providers —
        # never retired — restricted to explicit opt-in through
        # search.engine environment/document setting only; a composition-time
        # default_engine/provider must never select them, even when the
        # composed spec set would otherwise be eligible (Contract item 3,
        # g1 repair item 2). engines={...} may still declare a bounded spec
        # for one of them (credential/service injection for
        # tests/integration) without selecting it as the default.
        raise SettingsOnlyProviderError(
            f"engine {(default_engine or provider)!r} is a canonical provider explicit opt-in "
            "only through Web's search.engine setting; it cannot be selected via default_engine= or provider="
        )
    if engines is not None:
        if not isinstance(engines, Mapping) or not engines:
            raise ValueError("web.engines must be a non-empty mapping")
        retired_fallback: _EngineSpec | None = None
        for name, raw in engines.items():
            if not valid_engine_name(name):
                raise ValueError("web engine names must use the bounded selector grammar")
            explicit_provider = raw.get("provider", name) if isinstance(raw, Mapping) else name
            if explicit_provider in _RETIRED_PROVIDERS:
                raise RetiredProviderError(
                    f"provider {explicit_provider!r} is retired from built-in web search admission; "
                    "wire it as a third-party MCP server instead (see "
                    "src/lingtai/tools/mcp/skills/mcp-manual/reference/third-party-and-legacy.md)"
                )
            if explicit_provider not in PROVIDERS["providers"]:
                # Retain the pre-existing legacy_fallback_from behavior for a
                # genuinely unrecognized/inherited legacy provider name only
                # (not minimax/zhipu, rejected explicitly above). Held aside
                # rather than written into ``specs`` immediately, so a
                # genuine ``duckduckgo`` entry elsewhere in the same mapping
                # is never silently overwritten regardless of dict order.
                legacy_fallback_from = explicit_provider
                retired_fallback = _EngineSpec(
                    "duckduckgo", provider="duckduckgo",
                    service=raw.get("search_service") if isinstance(raw, Mapping) else raw,
                    legacy_fallback_from=explicit_provider,
                )
                continue
            if isinstance(raw, Mapping):
                data = raw
                specs[name] = _EngineSpec(
                    name, provider=data.get("provider", name), service=data.get("search_service"),
                    api_key=data.get("api_key"), api_key_env=data.get("api_key_env"),
                    model=data.get("model"), extra={k: v for k, v in data.items() if k not in {"provider", "search_service", "api_key", "api_key_env", "model"}},
                )
            else:
                specs[name] = _EngineSpec(name, provider=name, service=raw)
        if retired_fallback is not None and "duckduckgo" not in specs:
            specs["duckduckgo"] = retired_fallback
        if default_engine is not None and default_engine not in specs:
            raise ValueError("web default_engine must name an admitted engine")
    elif search_service is not None or provider is not None or api_key is not None or api_key_env is not None or model is not None:
        # Retain the old operator-config fallback for an inherited/unknown
        # provider only. Explicit settings never enter this branch and therefore
        # can never be silently substituted.
        if provider and provider not in PROVIDERS["providers"]:
            legacy_fallback_from = provider
            specs["duckduckgo"] = _EngineSpec(
                "duckduckgo", provider="duckduckgo", service=search_service,
                legacy_fallback_from=provider,
            )
        else:
            name = default_engine or provider or "duckduckgo"
            if not valid_engine_name(name):
                raise ValueError("web engine names must use the bounded selector grammar")
            specs[name] = _EngineSpec(name, provider=provider or name, service=search_service, api_key=api_key, api_key_env=api_key_env, model=model, extra=kwargs)
    else:
        # True no-config path: build the real canonical spec set (all four
        # providers) rather than a single bare duckduckgo spec, so the
        # runtime default resolver (WebManager._default_engine_now) can
        # actually see and select OpenAI when its standard credential env
        # var is genuinely set — the ordinary, no-operator-config runtime
        # path, not a test-only injected engine set.
        specs = _canonical_default_specs()
    if default_engine is not None and default_engine not in specs:
        raise ValueError("web default_engine must name an admitted engine")
    chosen = default_engine or (provider if provider in specs else next(iter(specs), None))
    # ``source`` distinguishes an operator's *explicit* default pick
    # (``default_engine``/``provider``) from mere engine-set composition
    # (``engines=``/``search_service=`` alone, or the true no-config path):
    # the latter still leaves the engine choice itself to the runtime
    # built-in default resolver (``WebManager._default_engine_now`` —
    # canonical OpenAI when genuinely available, else DuckDuckGo), so it
    # must not be misreported as an operator override that resolution
    # should never touch.
    source = "operator_default" if default_engine or provider else "built_in_default"
    return specs, chosen, source, legacy_fallback_from


@runtime_checkable
class WebCompositionPort(Protocol):
    """The narrow, Web-owned setup boundary for one official bind.

    This is the Protocol behind the ``web_runtime`` grant name: like Email's
    ``email_runtime``, the kernel reserves only the name, and the family owns
    the vocabulary. ``setup`` grants one :class:`WebComposition` value to the
    ``web`` declaration alone through ``extra_ports_for``; it names only the
    browser transport and immutable engine composition Web consumes, plus the
    one publication operation needed to retain setup -> WebManager
    compatibility. It never exposes an Agent, an LLM service, or provider
    credentials, and it is never built in the standard host table.
    """

    @property
    def browser_port(self) -> "BrowserPort": ...

    @property
    def specs(self) -> Mapping[str, _EngineSpec]: ...

    @property
    def default_engine(self) -> str | None: ...

    @property
    def default_source(self) -> str: ...

    @property
    def legacy_fallback_from(self) -> str | None: ...

    @property
    def provider_current(self) -> str | None: ...

    @property
    def model_current(self) -> str | None: ...

    @property
    def api_key_configured(self) -> bool | None: ...

    def publish_manager(self, manager: WebManager) -> None: ...


@dataclass(slots=True)
class WebComposition:
    """Explicit per-bind Web dependencies, supplied by capability setup only."""

    browser_port: "BrowserPort"
    specs: Mapping[str, _EngineSpec]
    default_engine: str | None
    default_source: str
    legacy_fallback_from: str | None
    provider_current: str | None = None
    model_current: str | None = None
    api_key_configured: bool | None = None
    manager: WebManager | None = None

    def publish_manager(self, manager: WebManager) -> None:
        if self.manager is not None and self.manager is not manager:
            raise ToolPluginDeclarationError(
                "web composition manager was published twice"
            )
        self.manager = manager


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Compose Web against only its granted host ports; mount nothing.

    Fail closed: the bind refuses to proceed unless the host granted
    ``web_runtime`` *and* that grant is the typed :class:`WebComposition`
    value ``setup`` composed. There is no fallback to any other carrier, no
    default browser transport, and no default engine set constructed here —
    a missing or mistyped grant is a wiring defect, raised as a
    ``ToolPluginError`` so the Composition Root's capability loop cannot
    absorb it as ``capability_skipped``.
    """
    try:
        composition = host.web_runtime
    except AttributeError as exc:
        raise HostPortError(
            "web plugin requires the granted 'web_runtime' host port carrying "
            "its typed WebComposition; none was granted"
        ) from exc
    if not isinstance(composition, WebComposition):
        raise HostPortError(
            "web plugin requires host port 'web_runtime' to carry a typed "
            f"WebComposition, not {type(composition).__name__}"
        )
    manager = WebManager(
        host.workdir,
        host.provider_identity,
        composition.browser_port,
        specs=composition.specs,
        default_engine=composition.default_engine,
        default_source=composition.default_source,
        legacy_fallback_from=composition.legacy_fallback_from,
        provider_current=composition.provider_current,
        model_current=composition.model_current,
        api_key_configured=composition.api_key_configured,
    )
    composition.publish_manager(manager)
    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=manager.handle,
        description=DECLARATION.description,
        glossary_package=DECLARATION.glossary_package,
    )


def setup(
    agent: "BaseAgent", search_service: Any | None = None, provider: str | None = None,
    api_key: str | None = None, api_key_env: str | None = None, model: str | None = None,
    default_engine: str | None = None, engines: Mapping[str, Any] | None = None,
    browser_port: "BrowserPort | None" = None, **kwargs: Any,
) -> WebManager:
    """Compose Web's explicit composition, then mount its static declaration.

    This is capability composition only: provider/browser wiring remains the
    exact existing lazy setup semantics (the ``BrowserPort`` plus immutable
    engine specs and default provenance become one :class:`WebComposition`),
    while the host owns registration, activation (none for Web), and mounting
    through the official namespace. The composition value is granted as the
    ``web_runtime`` port to this declaration alone through ``extra_ports_for``
    — never added to the standard table for every family — and the bound
    ``WebManager`` is published back through it exactly once, so callers keep
    receiving the manager as before.
    """
    if browser_port is None:
        from lingtai.adapters.browser_transport import VettedHttpTransport
        browser_port = VettedHttpTransport()
    specs, chosen, source, legacy_fallback_from = _specs_from_kwargs(
        search_service=search_service, provider=provider, api_key=api_key,
        api_key_env=api_key_env, model=model, default_engine=default_engine,
        engines=engines, kwargs=kwargs,
    )
    if (
        engines is None
        and search_service is None
        and provider is None
        and api_key is None
        and api_key_env is None
        and model is None
        and default_engine is None
    ):
        provider_current: str | None = "automatic"
        model_current: str | None = "provider-default"
        api_key_configured: bool | None = False
    elif (
        engines is None
        and search_service is None
        and default_engine is None
        and any(
            value is not None
            for value in (provider, api_key, api_key_env, model)
        )
    ):
        selected_spec = specs.get(chosen or "")
        provider_current = (
            selected_spec.provider if selected_spec is not None else None
        )
        model_current = (
            selected_spec.model or "provider-default"
            if selected_spec is not None
            else None
        )
        api_key_configured = (
            bool(selected_spec.api_key)
            or bool(
                selected_spec.api_key_env
                and os.environ.get(selected_spec.api_key_env)
            )
            if selected_spec is not None
            else None
        )
    else:
        # Engine-map, injected-service, and default-engine composition has no
        # singular flat provider/model/API-key fact to project.
        provider_current = None
        model_current = None
        api_key_configured = None
    composition = WebComposition(
        browser_port=browser_port,
        specs=specs,
        default_engine=chosen,
        default_source=source,
        legacy_fallback_from=legacy_fallback_from,
        provider_current=provider_current,
        model_current=model_current,
        api_key_configured=api_key_configured,
    )
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    register_agent_tool_plugins(
        agent,
        [DECLARATION],
        extra_ports_for=lambda declaration: (
            {"web_runtime": composition} if declaration is DECLARATION else {}
        ),
    )
    if composition.manager is None:  # pragma: no cover - declaration invariant
        raise ToolPluginDeclarationError("web official plugin did not publish a manager")
    return composition.manager
