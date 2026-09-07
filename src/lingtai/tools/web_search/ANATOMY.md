---
related_files:
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/registry.py
  - src/lingtai/ANATOMY.md
  - src/lingtai/tools/web_search/BEHAVIORS.md
  - src/lingtai/tools/web_search/CONTRACT.md
  - src/lingtai/tools/web_search/__init__.py
  - src/lingtai/tools/web_search/settings.py
  - src/lingtai/tools/web_search/_spill.py
  - src/lingtai/tools/web_search/manual/SKILL.md
  - src/lingtai/tools/web_search/manual/reference/operation-contract.md
  - ENVIRONMENT_VARIABLES.md
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/adapters/tool_plugin_host.py
  - tests/test_web_official_plugin.py
  - tests/test_web_composition_port.py
  - tests/test_web_progressive_disclosure.py
  - tests/test_web_settings_action.py
  - src/lingtai/tools/browser/ANATOMY.md
  - src/lingtai/tools/browser/core.py
  - src/lingtai/tools/browser/port.py
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/services/websearch/ANATOMY.md
  - src/lingtai/kernel/tool_result_artifacts.py
  - src/lingtai/kernel/workdir.py
  - src/lingtai/tools/web_search/glossary-en.md
  - src/lingtai/tools/web_search/glossary-wen.md
  - src/lingtai/tools/web_search/glossary-zh.md
  - src/lingtai/tools/web_search/manual/assets/api-endpoints.json
  - src/lingtai/tools/web_search/manual/assets/css-selectors.json
  - src/lingtai/tools/web_search/manual/assets/extraction-pipeline.json
  - src/lingtai/tools/web_search/manual/assets/regex-patterns.json
  - src/lingtai/tools/web_search/manual/assets/search-providers.json
  - src/lingtai/tools/web_search/manual/assets/site-templates.json
  - src/lingtai/tools/web_search/manual/reference/academic-pipeline.md
  - src/lingtai/tools/web_search/manual/reference/agent-native-browser.md
  - src/lingtai/tools/web_search/manual/reference/maintenance-bundles/SKILL.md
  - src/lingtai/tools/web_search/manual/reference/migration-from-v2.md
  - src/lingtai/tools/web_search/manual/reference/news-and-rss.md
  - src/lingtai/tools/web_search/manual/reference/realtime-data.md
  - src/lingtai/tools/web_search/manual/reference/routing-and-sites/SKILL.md
  - src/lingtai/tools/web_search/manual/reference/search-strategies.md
  - src/lingtai/tools/web_search/manual/reference/social-media.md
  - src/lingtai/tools/web_search/manual/reference/stealth.md
  - src/lingtai/tools/web_search/manual/reference/tier-0-pdf.md
  - src/lingtai/tools/web_search/manual/reference/tier-1-5-trafilatura.md
  - src/lingtai/tools/web_search/manual/reference/tier-1-apis.md
  - src/lingtai/tools/web_search/manual/reference/tier-2-beautifulsoup.md
  - src/lingtai/tools/web_search/manual/reference/tier-3-playwright.md
  - src/lingtai/tools/web_search/manual/reference/tier-4-jina-firecrawl.md
  - src/lingtai/tools/web_search/manual/reference/tier-5-ai-search.md
  - src/lingtai/tools/web_search/manual/reference/tier-quick-refs/SKILL.md
  - src/lingtai/tools/web_search/manual/scripts/cached_get.py
  - src/lingtai/tools/web_search/manual/scripts/extract_page.py
maintenance: |
  Keep this public web Anatomy and its Contract reciprocal, keep the parent
  link bidirectional, and keep the sole web-manual edge on both owner twins.
  Browser is an internal browse subcomponent, not another model-facing node.
  tool_family is generic optional infrastructure this package composes onto;
  web's own instance-bound diagnostics and dispatch wrapper remain here.
  Update this map with structural code changes and verify citations.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# Unified web capability Anatomy

The retained `web_search` package is the public `web` composition owner. It
combines lazy SearchService adapters with the internal browser Core while
exposing one model-facing handler and one per-Agent state boundary. Schema
composition and envelope dispatch delegate to the generic
`tool_family` infrastructure; this package retains ownership of action
implementations, read-only settings resolution, and diagnostics.

## Components

- `DECLARATION`, `WebCompositionPort`, `WebComposition`, `_bind()`, `WebManager`, and `setup()` — static
  official `web` identity (the fourteenth declared family,
  `requires=("workdir", "web_runtime", "provider_identity")`) plus explicit
  per-bind search/browser composition. `setup()` retains lazy engine/browser
  composition, folds the `BrowserPort` plus immutable engine specs and default
  provenance into one `WebComposition`, grants that value to the `web`
  declaration alone as the Web-owned `web_runtime` port through
  `register_agent_tool_plugins(..., extra_ports_for=...)`, and returns the
  manager the bind published back through `WebComposition.publish_manager`
  (exactly once). `_bind()` fails closed with `HostPortError` unless
  `host.web_runtime` is granted and is a typed `WebComposition` — no fallback
  carrier, default transport, or default engine set — then constructs a
  per-instance `ToolFamily` (`lingtai.tools.tool_family`) with
  `search`/`browse` handlers, the generic provider-injected `settings` child,
  and a `manual` child from
  `tool_family.manual.build_manual_child`, and returns the bound handler.
  `handle()` delegates envelope validation/dispatch and stamps
  `current_setting`/`action` onto envelope-level failures; no Web object retains
  the whole Agent (`src/lingtai/tools/web_search/__init__.py`).
- `_EngineSpec`, `_specs_from_kwargs`, `_canonical_default_specs()` —
  immutable operator engine wiring. `_specs_from_kwargs` rejects a retired
  provider name (`minimax`, `zhipu` — `_RETIRED_PROVIDERS`) supplied via the
  flat `provider=`/`default_engine=` kwargs with `RetiredProviderError`, a
  composition-time exception, never a silent DuckDuckGo substitution;
  rejects a settings-gated engine name (`anthropic`, `gemini` —
  `_BACKEND_GATED_ENGINES`) supplied the same way with the distinct
  `SettingsOnlyProviderError` — Anthropic/Gemini are active canonical
  providers, never "retired". A retired provider named inside `engines={}`
  is rejected with `RetiredProviderError` the same way, while a genuinely
  unrecognized/inherited legacy provider name keeps the pre-existing
  `legacy_fallback_from`-tagged DuckDuckGo spec. The true no-config path
  (no kwargs at all) calls
  `_canonical_default_specs()`, which composes all four canonical providers
  using each provider's own standard `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/
  `GEMINI_API_KEY` environment variable as `_EngineSpec.api_key_env`
  (`_CANONICAL_API_KEY_ENV`) — never the current Agent's own live
  `agent.service` credentials (`src/lingtai/tools/web_search/__init__.py`).
- `WebManager._default_engine_now()` — the live, per-call built-in default
  resolver: canonical OpenAI Responses Web Search when genuinely available
  (present in the operator's engine set and `_status() == "available"`),
  else `duckduckgo` if composed, else `None` if the only remaining candidate
  is a settings-gated engine (never lands the built-in default on
  `anthropic`/`gemini`); only applies when no operator `default_engine`/
  `provider` was explicitly chosen (`_default_source == "built_in_default"`)
  (`src/lingtai/tools/web_search/__init__.py`).
- `_same_provider_identity()` — the truthful, exact-match canonical-provider
  identity check gating explicit Anthropic/Gemini opt-in against the granted
  `ProviderIdentityPort.provider`; module-private to `web_search`
  (`src/lingtai/tools/web_search/__init__.py`) — only this capability's policy
  needs it, so it is not a cross-tool API.
- `WebManager._openai_duckduckgo_fallback()`/`_duckduckgo_fallback()` — the
  one automatic runtime fallback, triggered only by the exact
  `OpenAISearchError` subclass (never a bare `SearchProviderError` or
  `Exception`, so an `AnthropicSearchError`/`GeminiSearchError` and a
  manager/programming defect both fail normally instead of retrying):
  exactly one DuckDuckGo attempt, comment line plus bounded
  `openai_failure_class`/`duckduckgo_failure_class` provenance, no second
  retry, no fallback for any other engine. `WebManager._search`'s exception
  handler also recognizes the shared `SearchProviderError` base for any
  other typed provider failure and stamps a bounded `provider_failure_class`
  onto the `SEARCH_FAILED` result (`src/lingtai/tools/web_search/__init__.py`).
- `build_settings_provider()` — binds Web's applied composition, live
  credential routes, engine selector, and output threshold to exact five-field
  rows. Credential rows are private and the provider owns no writer
  (`src/lingtai/tools/web_search/settings.py`).
- `read_settings()` — `LINGTAI_WEB_ENGINE`, then bounded strict-v1
  `settings/web.search.json`, then the composed runtime fallback
  (`src/lingtai/tools/web_search/settings.py`).
- `read_output_settings()` — `LINGTAI_WEB_MAX_CHARS`, then bounded strict-v1
  `settings/web.json`, then 50000, shared identically by `search` and `browse`
  (`src/lingtai/tools/web_search/settings.py`).
- `spill_if_over_threshold()` — the shared Web-owned inline-vs-artifact
  decision and envelope builder, called by both `_deliver_search` and
  `_deliver_browse`; atomically writes complete content via the kernel's
  `write_artifact_file` under the canonical `WorkdirLayout.tool_results_dir`
  (the same directory the generic preventive spill owns), and stamps the
  envelope with the kernel's `WEB_ARTIFACT_MARKER` so `is_spill_manifest`
  recognizes it explicitly
  (`src/lingtai/tools/web_search/_spill.py`).
- `BrowserEngine` — internal static browse use case, provenance, refs, cursors,
  SSRF policy, and typed failures (`src/lingtai/tools/browser/core.py:126-327`).
- `SearchService` adapters — provider implementations behind the internal
  service boundary (`src/lingtai/services/websearch/__init__.py:20-70`).
- `manual/SKILL.md` — sole installed `web-manual` route
  (`src/lingtai/tools/web_search/manual/SKILL.md:1-239`). It progressively
  discloses into three sidecar trees, all packaged and all enumerated in
  `related_files`: `manual/reference/*.md` (the detailed public
  `operation-contract`, per-tier extraction guides `tier-0-pdf` …
  `tier-5-ai-search`, plus the strategy/domain notes `academic-pipeline`,
  `agent-native-browser`, `news-and-rss`, `realtime-data`, `search-strategies`,
  `social-media`, `stealth`, and `migration-from-v2`),
  the nested sub-skills `manual/reference/{maintenance-bundles,
  routing-and-sites,tier-quick-refs}/SKILL.md`, `manual/assets/*.json`
  (provider, selector, endpoint, regex, pipeline, and site-template data), and
  `manual/scripts/{cached_get,extract_page}.py` procedure fallbacks.

## Connections

`src/lingtai/tools/registry.py` maps public `web` to this package and maps legacy input
`web_search` one-way to `web`. `setup()` sends static `DECLARATION` plus its
explicit `WebComposition` (as the declaration-scoped `web_runtime` port,
through `extra_ports_for`) through `lingtai.adapters.tool_plugin_host` and the
kernel registrar; the host builds only the narrow read-through
`provider_identity` label (`AgentProviderIdentityAdapter`, one closure over
`Agent.service.provider`) in its standard table for `web`, and the manager
retains only the granted workdir and provider-identity ports.
`WebManager` calls only `SearchService` for search and
only `BrowserEngine` for browse; neither path crosses into the other transport.
Agent manual installation maps this retained package's `manual/` to
`capabilities/web/` and skips the retained browser manual.

## Composition

The parent [`src/lingtai/tools/ANATOMY.md`](../ANATOMY.md) owns capability
registry composition. The internal browse child
[`src/lingtai/tools/browser/ANATOMY.md`](../browser/ANATOMY.md) owns static-page
structure but has no public registration. The generic
[`src/lingtai/tools/tool_family/ANATOMY.md`](../tool_family/ANATOMY.md) owns
the reusable schema-composition/dispatch infrastructure this package builds
its `ToolFamily` instances from; it has no knowledge of web's own settings or
diagnostics. The shared
[`src/lingtai/tools/CONTRACT.md`](../CONTRACT.md) owns the future canonical public
call shape. The paired [`CONTRACT.md`](CONTRACT.md) specializes
that promise for web's actions, behavior, and evidence.

## State

Each manager owns immutable engine specs and applied owner-setting snapshots, a
lazy per-engine service cache, one browser engine, and its bounded
ref/snapshot/cursor stores. Engine/output settings and their canonical env peers
are read on every applicable call. The `settings` action writes no state; real
changes remain in the launcher/composition/file procedures taught by
`web-manual`. Separately, `spill_if_over_threshold()` writes artifacts under the
canonical `<agent-workdir>/tmp/tool-results/` directory when a call's
complete content exceeds the shared threshold — the same directory the
kernel's generic preventive spill already owns, not a second web-owned
directory. Those are ephemeral output artifacts, not settings state, and
unrelated to `<agent-workdir>/settings/*.json` above. Credentials stay in
operator wiring or process configuration and are projected only through
redacted rows; no call mutates environment state.

## Notes

`web_search` remains a physical implementation path and a read-only config
alias only. Provider-native wire names such as an API's `web_search` remain
unchanged. The manual's legacy scripts are procedure fallbacks, not public
handlers or additional catalog entries.
