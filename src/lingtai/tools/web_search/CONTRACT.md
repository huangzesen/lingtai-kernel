---
name: web
contract_version: 7
root_contract: CONTRACT.md
related_files:
  - src/lingtai/tools/web_search/ANATOMY.md
  - src/lingtai/tools/web_search/BEHAVIORS.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/web_search/__init__.py
  - src/lingtai/tools/web_search/settings.py
  - src/lingtai/tools/web_search/_spill.py
  - src/lingtai/tools/web_search/manual/SKILL.md
  - src/lingtai/tools/web_search/manual/reference/operation-contract.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - tests/test_web_official_plugin.py
  - tests/test_web_composition_port.py
  - tests/test_web_settings_action.py
  - src/lingtai/tools/browser/core.py
  - src/lingtai/tools/browser/port.py
  - src/lingtai/adapters/browser_transport.py
  - src/lingtai/services/websearch/__init__.py
  - src/lingtai/services/websearch/openai.py
  - src/lingtai/services/websearch/anthropic.py
  - src/lingtai/services/websearch/gemini.py
  - src/lingtai/kernel/tool_result_summary.py
  - src/lingtai/kernel/tool_result_artifacts.py
  - src/lingtai/kernel/workdir.py
  - src/lingtai/tools/tool_family/CONTRACT.md
maintenance: |
  Keep this unified web Contract and its Anatomy reciprocal. Keep the manual
  edge on both owner twins. Update the Port, adapters, tests, and this Contract
  together when behavior or errors change; retain browser as an internal browse
  subcomponent rather than a second capability. web's schema composition and
  envelope dispatch build on the generic tool_family package; keep that link
  current when either side's boundary changes.
---
# Unified web capability

## Purpose

`web` is exactly one model-facing capability with `search`, `browse`, the
generic provider-injected `settings`, and metadata-only `manual` actions. It is implemented in the retained
`tools.web_search` composition owner; browser and SearchService are internal
subcomponents. `web` is the first family migrated to the LingTai Tool Protocol
v2 shape defined in `src/lingtai/tools/CONTRACT.md`, and the first family to
build its schema composition and envelope dispatch on the generic
`src/lingtai/tools/tool_family/` infrastructure (`ToolFamily`/`ChildTool`);
using it changed no observable promise in this file. `web` is also an official
static declared host plugin: its `DECLARATION` owns the same public name,
`search`/`browse` operational schemas, settings opt-in, and installed `web`
manual destination; its
binder receives only `workdir`, the Web-owned typed `web_runtime` composition
value, and the narrow `provider_identity` label (`requires=("workdir",
"web_runtime", "provider_identity")`). Registration, mounting, and name
reservation stay kernel-owned in `lingtai.kernel.tool_plugin`; no Web code
receives or retains the whole Agent.

## Behavior

Search resolves `LINGTAI_WEB_ENGINE`, then the action-owned
`settings/web.search.json` selector, then its composed fallback on every call;
browse and manual read no selector file. Search and browse both resolve
`LINGTAI_WEB_MAX_CHARS`, then the shared family-owned `settings/web.json`
output-delivery threshold, then 50000 on every applicable call; manual reads
neither settings file. `settings(input={})` inventories those live values plus
applied provider/model/admitted-engine composition and private credential facts
through the generic read-only five-field seam. It never sets, resets, or
otherwise mutates them. Search returns the complete result set the selected provider
returned for this call — no LingTai-imposed result-count cap and no
per-field truncation of provider-returned text — with same-Agent `link_ref`
handles on every URL-bearing result; a synthesized or otherwise URL-less
result stays in the result set without a `link_ref`. Browse consumes a URL
or a search/browse reference through the same BrowserEngine state and
always delivers the complete extracted document for that fetch, never only
a first page. At or below the effective `max_chars` threshold, both actions
return the complete content inline. Above it, both atomically spill the
complete content to a workdir-relative artifact file under the canonical
`<agent-workdir>/tmp/tool-results/` directory (the same directory the
generic preventive spill already owns) and return a compact envelope with
no content preview — see "Output delivery" below. Manual returns the installed
web-manual without provider construction or network I/O. All success and
failure envelopes include `action` and a bounded secret-free
`current_setting` block, which now also carries `output_max_chars` (the
resolved shared-setting value, source, and — on error — a bounded diagnostic).
Explicit `engine` and irrelevant action fields fail loudly. `web`'s own schema
(via `ToolFamily.build_schema()`) declares a
top-level, REQUIRED `reasoning` string property — Host InvocationContext/
audit metadata — with the same description Agent schema composition also
re-injects into every tool's `properties` uniformly (that central injection
never touches `required`, so a family must declare `reasoning` required
itself). ToolExecutor preserves it only as internal `_reasoning` metadata,
which does not enter action input or change dispatch. `web`'s own schema owns
the root `summarize` boolean (LTP v2 is
migrated one family at a time, not by central injection); `handle()` delegates
to a per-instance `ToolFamily.handle()` (`tool_family/CONTRACT.md`), which
validates `summarize` is boolean and strips it before action dispatch — no action implementation
ever receives it.

### Settings ownership

Guarded by: [W003](BEHAVIORS.md#behavior-w003)

The declaration sets only this family's boolean settings opt-in, so the generic
child appears immediately before `manual`. Normal success has no top-level
`status` and every row contains exactly `key`, `current`, `default`,
`configurable`, and `comment`. The exact ordered keys are `provider`, `model`,
`api_key`, `engines`, `search.engine`, `output.max_chars`, and the three
`credentials.{openai,anthropic,gemini}_api_key` rows. Every `comment` points to
the exact `web-manual` section that owns meaning, accepted values, source and
precedence, address, apply timing, authorization/sensitivity notes, and the real
external change procedure.

Provider, model, API-key composition, and admitted engines are applied
startup/launcher snapshots. The singular flat provider defaults to
`"automatic"`; its model defaults to `"provider-default"`; multi-engine or
injected composition reports JSON `null` for singular provider/model facts; and
the admitted-engine default is the sorted canonical four-engine list. The four
credential-bearing rows use private `SettingRow(..., _sensitive=True)` facts,
so both public values render as `<redacted>` and no secret, env indirection, or
private flag is projected. Credential-route truth comes from the manager: the
declared route before lazy construction and the cached service afterward.

`search.engine` resolves `LINGTAI_WEB_ENGINE`, then
`settings/web.search.json`, then the composed runtime fallback;
`output.max_chars` resolves `LINGTAI_WEB_MAX_CHARS`, then `settings/web.json`,
then 50000. If either current value is unavailable, the provider raises and the
generic seam returns one fixed bounded failure with no partial rows.
`input={}` is the only operation: there is no settings set/reset/mutation API,
receipt, writer, compatibility shim, or process-environment mutation.
`configurable` means the manual names an authorized procedure outside SHOW; a
second SHOW can verify that procedure's result.

## Port

Search uses the existing internal `SearchService.search(query)` boundary.
Browse uses the existing Core-owned `BrowserPort` implemented by the pinned
transport adapter. The public dispatcher never invokes search from browse or
browser transport from search. The declared host plugin additionally receives
only `WorkdirPort` (settings, artifacts, and installed manual), the typed
`WebCompositionPort` (browser transport, immutable engine specs, default
provenance, and one manager-publication operation), and `ProviderIdentityPort`
(the one canonical label needed for Anthropic/Gemini eligibility); it never
receives the Agent or its LLM service/credentials. `WebCompositionPort` is the
Protocol behind the kernel grant name `web_runtime` (family-owned, like Email's
`email_runtime`): `setup()` composes one `WebComposition` and grants it to the
`web` declaration alone through `extra_ports_for`; it is never built in the
standard host table. `_bind()` MUST fail closed — raising the kernel's
`HostPortError`, which the Composition Root cannot absorb as
`capability_skipped` — unless `host.web_runtime` is granted and is a typed
`WebComposition`; there is no fallback to another carrier, no default browser
transport, and no default engine set constructed at bind. `setup()` publishes
the bound `WebManager` back through the composition exactly once and returns it,
so the public setup/manager compatibility is unchanged. `ProviderIdentityPort`
is read-only and read through on every access; it exposes only a string label
(or `None`), never the service, credentials, model configuration, or Agent.

## Provider ownership and routing

Guarded by: [W001](BEHAVIORS.md#behavior-w001), [W002](BEHAVIORS.md#behavior-w002)

Built-in Search admits exactly four engines: canonical first-party OpenAI
Responses Web Search, canonical first-party Anthropic server-side Web Search,
canonical first-party Gemini Google Search grounding, and DuckDuckGo. MiniMax
and Zhipu are retired from built-in admission entirely (`_RETIRED_PROVIDERS`
in `web_search/__init__.py`). Their `SearchService` implementations were
deleted 2026-07-28 (Jason authorized the exact two-path deletion, issue
11114) — `src/lingtai/services/websearch/minimax.py` and `.../zhipu.py` no
longer exist, and `create_search_service()`'s factory branches for both were
removed with them, so an unrecognized `"minimax"`/`"zhipu"` name now raises
the factory's own documented `ValueError` like any other unknown provider,
never an uncaught `ModuleNotFoundError`. Wire either provider through a
third-party MCP server instead — see
`src/lingtai/tools/mcp/skills/mcp-manual/reference/third-party-and-legacy.md`, the
skill-owned procedure route. Naming either via `provider=`, `default_engine=`,
or `engines={}` at `web` composition time raises `RetiredProviderError` — a
composition-time, actionable failure, never a silent DuckDuckGo substitution
and never reaching the factory at all. This is distinct from the pre-existing
`legacy_fallback_from`-tagged DuckDuckGo behavior, which remains in force
only for a genuinely unrecognized/inherited legacy provider name that was
never a deliberately-retired built-in.

`RetiredProviderError` is reserved exactly for MiniMax/Zhipu. Anthropic and
Gemini are fully active, currently-admitted canonical providers — never
described as "retired" anywhere in code, docs, or error text — restricted only
to a settings-only selection route; naming either through a forbidden
composition route raises the distinct `SettingsOnlyProviderError` (see below).

The real no-config `setup(agent)` path (no `engines=`, `provider=`,
`default_engine=`, or `search_service=`) composes the true built-in spec set:
all four canonical providers, with `openai`/`anthropic`/`gemini` each reading
only their own standard, publicly-documented API-key environment variable
(`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GEMINI_API_KEY` — as declared by
`src/lingtai/tools/web_search/__init__.py:_CANONICAL_API_KEY_ENV`) — never the
current Agent's own live `agent.service` credentials or any private LLM-adapter
attribute. The built-in default engine is resolved live, per call: canonical
OpenAI when its standard credential is genuinely present, else DuckDuckGo.
Anthropic and Gemini are present in this spec set (so their status is
honestly reported in `current_setting`) but are never the *selected* default;
only a valid explicit `LINGTAI_WEB_ENGINE` or `settings/web.search.json`
selection can select them.

Anthropic and Gemini are explicit opt-in **only** through the hot-read
`search.engine` setting: a valid `LINGTAI_WEB_ENGINE` or
`settings/web.search.json` selection. A composition-time `default_engine=`/
`provider=` naming either one is rejected outright with
`SettingsOnlyProviderError` at `setup()` time (an `engines={}` mapping may
still declare a bounded spec for one of them — credential/service injection
for tests/integration — without that composition selecting it as the
default). Once selected through either setting source, the call fails loudly with
`PROVIDER_BACKEND_INELIGIBLE` — no provider construction, no search call —
unless the current Agent's live LLM backend truthfully IS that same canonical
provider, per the module-private `_same_provider_identity()` predicate in
`web_search/__init__.py` (exact match against the declared
`ProviderIdentityPort.provider`; Claude Code, `custom`, `openrouter`, and every
other aliased/wire-compatible provider name are never treated as canonical
Anthropic/Gemini identity, regardless of API compatibility). This predicate
is private to `web` — no cross-tool identity API was created for one policy.
A runtime failure of an explicitly selected Anthropic/Gemini engine reports
`SEARCH_FAILED`; it is never silently substituted with DuckDuckGo or any
other engine — see the error hierarchy below for exactly how each provider's
own adapter reports that failure.

**Provider error hierarchy.** `src/lingtai/services/websearch/__init__.py`
defines `SearchProviderError(provider, failure_class)`, a shared, narrow base
raised by all three canonical adapters on a runtime failure — bounded to a
provider name and a failure class, never raw SDK exception text, request
bodies, or credentials in the message, logs, or any returned structure. Each
adapter's own subclass carries the same shape: `OpenAISearchError`,
`AnthropicSearchError`, `GeminiSearchError`. None of the three ever swallows
a genuine SDK/HTTP failure to `[]` — `[]` is reserved for a genuine
successful provider response with no content/result. `AnthropicSearchService`
additionally detects Anthropic's official in-body HTTP-200
`web_search_tool_result_error` (the API can return `status_code=200` while
the web search tool itself failed — evidence doc: "the Claude API still
returns a 200 (success) response") and raises `AnthropicSearchError`
carrying only the bounded `error_code`, never the raw block or any other
response content.

OpenAI is the sole engine with an automatic runtime fallback, and only for
the exact `OpenAISearchError` subclass (timeout, rate limit, HTTP/SDK
error). On that specific exception type, `web` executes exactly one
DuckDuckGo search and returns `status: "ok"` with `engine: "openai"`
(selected) and `actual_engine: "duckduckgo"` (actual), a top-level `comment`
line stating that OpenAI failed and DuckDuckGo was used, and bounded,
secret-free `openai_failure_class`/`duckduckgo_failure_class` provenance. If
the DuckDuckGo fallback also fails, the call fails with `SEARCH_FAILED` and
both bounded failure classes; there is no second retry and no recursive
fallback. A typed `AnthropicSearchError`/`GeminiSearchError` (or any other
`SearchProviderError` on a non-OpenAI engine) fails with `SEARCH_FAILED` and
a bounded `provider_failure_class`, never touching DuckDuckGo. Any non-typed
exception (a manager/programming defect — a `TypeError` or `AttributeError`
from malformed data, never raised by the adapters themselves) fails normally
with `SEARCH_FAILED` and no provenance field, and also never touches
DuckDuckGo. No engine other than OpenAI has an automatic fallback.

Each canonical provider's `SearchService` extracts real, official per-source
citation URLs from its own provider response — OpenAI's Responses `output[]`
message `annotations[].url_citation`, Anthropic's `web_search_result_location`
text citations (falling back to raw `web_search_tool_result` items), and
Gemini's `grounding_metadata.grounding_chunks[].web` (field names verified
2026-07-28 read-only against the installed `google-genai` 2.10.0 package
source, `google/genai/types.py`) — never an invented URL. When a provider
genuinely returns a nonempty search-grounded narrative with no citation (a
legally valid response shape for all three official APIs), exactly
one bounded narrative `SearchResult` with `url=""` is preserved rather than
silently discarded; `WebManager` never fabricates a `link_ref` for it
(`link_ref: null` in that one case only — every other result with a real URL
gets a real `link_ref`).

## Adapters

Operator setup supplies immutable per-Agent engine specs, optional injected
SearchService instances, and browser ports. Provider construction is lazy and
cached per selected engine. The engine-selector env/document is not a
credential or provider-installation channel and no request mutates `os.environ`. Existing browser SSRF, deadline,
provenance, source-hash, cursor, snapshot, reference, and typed-failure rules
remain in force. `OpenAISearchService` uses the canonical Responses API
(`client.responses.create(tools=[{"type": "web_search"}])`), not the retired
Chat Completions `gpt-4o-search-preview` route, and raises `OpenAISearchError`
(a bounded failure-class carrier, never raw SDK exception text) on failure
instead of swallowing to an empty result — the one provider adapter whose
failure the Web use-case policy must observe to drive the DuckDuckGo fallback
above.

## Contract rules

Guarded by: [W002](BEHAVIORS.md#behavior-w002)

- The public name is `web`; no browser or web_search registry, schema, prompt,
  check-caps, catalog, or installed manual entry exists.
- `web` is the first real implementation of the shared LTP v2 contract in
  `src/lingtai/tools/CONTRACT.md`: the final model-facing root is exactly
  `action`, `input`, `reasoning`, and `summarize`. There is no public
  `parameters`, `parameter`, `summary`, or other compatibility alias;
  `_reasoning` is internal only.
- `action`, nested `input`, and top-level `reasoning` are required by the
  capability schema (`required: [action, input, reasoning]`). `action` is
  one of `search`, `browse`, `settings`, or `manual`; `input` uses strict action-specific
  object branches. Each branch is closed, every declared branch field is
  required, and browse optionals use JSON null, matching OpenAI strict-object
  conventions. No branch admits `reasoning`, `_reasoning`, or `summarize`.
- `summarize` is a root-only optional boolean, absent or false by default. It
  is envelope metadata, not action input: `handle()` validates its type
  (non-boolean fails loudly with `INVALID_ARGUMENT`) and strips it before
  dispatching to `search`/`browse`/`settings`/`manual`. `src/lingtai/kernel/
  tool_result_summary.py` recognizes canonical root `summarize=true` for
  `web` specifically (scoped by tool name, alongside the legacy literal
  `summary` flag it preserves for genuinely unmigrated callers) and treats
  `web`'s own canonical `status: "failed"` envelope as an unsummarizable error
  result, exactly like the kernel-wide `status: "error"` convention — scoped
  to migrated LTP v2 families so an unrelated tool's non-error `"failed"`-named
  domain value is never reinterpreted.
- Engine settings v1 is the direct, action-owned strict schema
  `{"schema_version":1,"engine":"<admitted-name>"}`, read from
  `settings/web.search.json` (a direct child of `<agent-dir>/settings/`; no
  nested `search` object). There is no `settings/web.browse.json` or
  `settings/web.manual.json`, no cross-read of any old or sibling settings
  path, and no compatibility fallback, overlay, or merge between
  `settings/web.search.json` and `settings/web.json` (below). The separate
  `LINGTAI_WEB_ENGINE` peer has higher precedence. Only an
  operator-admitted engine name is permitted. Missing files use the
  operator/built-in default; malformed, unknown, disallowed, unavailable, or
  credential-missing selections fail search without substitution. Invalid
  settings use error code `WEB_SETTINGS_INVALID`; a selected or
  initialization-unavailable engine uses `SEARCH_ENGINE_UNAVAILABLE`; a
  selected Anthropic/Gemini engine on a non-canonical backend uses
  `PROVIDER_BACKEND_INELIGIBLE`. Browse and manual remain fully usable —
  including when `settings/web.search.json` is invalid — and never construct
  a search provider.
- `settings/web.json` is a separate, family-owned strict schema
  `{"schema_version":1,"max_chars":<integer 1..100000>}`, default
  `max_chars` 50000, below the higher-precedence `LINGTAI_WEB_MAX_CHARS`, and
  consumed identically by `search` and `browse` for the
  same call's inline-vs-artifact delivery decision. Manual never reads it.
  Missing file uses the default; a present malformed/unknown-field/
  wrong-schema-version/non-integer/boolean/out-of-range value fails loud with
  error code `WEB_OUTPUT_SETTINGS_INVALID` before any provider call or page
  fetch — never clamped, coerced, or silently defaulted. Browse's existing
  nullable per-call `input.max_chars` (unchanged 1..100000 range) may still
  override the shared setting for that call only; it no longer selects a
  pagination page size but the delivery threshold. Every search/browse
  success and failure envelope's `current_setting.output_max_chars` echoes
  the effective value, its source (`environment` / `default` / `settings/web.json` /
  `call_override`), and a bounded diagnostic on error.
- Owner-document reads reject symlinks, non-regular files, unstable snapshots,
  oversize/wrong-UTF-8 data, unknown fields, duplicate fields, and wrong
  schema. Present invalid env input also fails loud and never falls through. A
  changed file or environment value is observed on the next applicable call
  (hot-read, no caching).
  Diagnostics contain source, selected engine/null, bounded available statuses,
  revision/hash, and the exact change hint `Use web(action='settings', input={},
  reasoning='inspect web settings'); engine/output changes apply on the next
  applicable web call; use web(action='manual', input={}, reasoning='load web
  guidance') for schema.`; secrets and absolute paths never
  appear.
- Search results are `{title,url,snippet}` objects, plus `link_ref` on any
  result carrying a usable HTTP(S) `url`; a synthesized or URL-less result is
  preserved in the result set without `link_ref`. `link_ref` is `null` only
  for the one bounded citation-free narrative result a canonical provider
  may legally return; every result with a real `url` gets a real `link_ref`,
  never a fabricated one for an empty `url`. `title`/`url`/`snippet` are the
  exact finite strings the selected provider returned — no LingTai-imposed
  per-field character slice and no LingTai-imposed result-count cap: `count`
  reflects the true, uncapped number of results the selected provider
  returned for that call. Provider adapters receive no LingTai count request
  in this mode (`max_results=None`, or the provider's own request parameter
  omitted where supported); a provider's own native finite result limit is
  unchanged and reported as provider-native metadata, never as a global
  completeness or total claim. `SearchService.search` is contracted to
  return a finite list (the `SearchService` ABC docstring in
  `services/websearch/__init__.py`, recorded in `services/websearch/ANATOMY.md`); there is no
  LingTai-side iteration ceiling or partial-success shape defending against
  an out-of-contract non-terminating iterable — provider/service call
  deadlines and fail-loud cancellation are the only operational bound,
  exactly as the provider boundary already requires.
- Composing `web` with a retired provider (`minimax`, `zhipu`) via
  `provider=`, `default_engine=`, or `engines={}` raises
  `RetiredProviderError` at `setup()` time. Composing with a settings-only
  provider (`anthropic`, `gemini`) via `provider=` or `default_engine=`
  raises the distinct `SettingsOnlyProviderError` instead — both are
  composition-time, actionable Python exceptions, not a runtime search
  result; the two classes are never conflated, since Anthropic/Gemini are
  active canonical providers and MiniMax/Zhipu are not. `engines={}` may
  still declare a bounded spec for `anthropic`/`gemini` (credential/service
  injection) without that composition selecting either as the default.
- Browse remains static public HTTP(S) only with its existing SSRF/DNS,
  extraction, provenance, cursor, snapshot, deadline, and typed-failure rules,
  and stays provider/network independent of the search settings file (though
  it shares `settings/web.json` with search). A fresh Browse success always
  delivers the complete extracted document for that fetch — never only a
  first page — and never mints a `next_cursor`. An existing `cursor` input is
  still accepted only as a compatibility locator for the cached snapshot; it
  resolves without a refetch but returns the same complete-document delivery
  policy, never another partial page. If the fetched/continued snapshot is no
  longer resolvable when the delivery decision runs (only possible under
  extreme concurrent pressure evicting the tiny process-local snapshot LRU
  between the engine's success and this decision), Browse fails loud with
  `error_code: "BROWSE_SNAPSHOT_UNAVAILABLE"` rather than falling back to the
  engine's own internally-paginated page — a partial/first-page body with
  `partial`/`next_cursor` would silently violate the complete-output policy
  above, so no degraded "best effort" success is ever returned here.
- Above the effective `output_max_chars` threshold, both `search` and
  `browse` atomically write the exact complete canonical content (never a
  truncated prefix) to a workdir-relative file under the canonical
  `<agent-workdir>/tmp/tool-results/` directory and return a compact
  artifact envelope in place of the inline body: `delivery: "artifact"`,
  `artifact` (the namespaced marker `lingtai_web_output_artifact/v1`),
  `content_scope` (`provider_response` for search, `fetched_static_document`
  for browse), `content_kind`, `format`/`encoding`, `file_path`
  (workdir-relative only, readable via the existing public `file.read`
  tool), exact `content_chars` and `content_sha256` of the artifact file,
  `output_setting_source`/`output_setting_revision`/`output_setting_hash`
  (the shared setting state that produced this threshold), and an explicit
  instruction that the artifact is the complete result with nothing
  omitted. Search's `results` array and Browse's
  `blocks`/`partial`/`next_cursor`/`returned_chars` are omitted entirely
  when spilled — never a lossy subset alongside the artifact. Inline
  (at-or-below-threshold) responses add `delivery: "inline"` and
  `content_chars` but otherwise keep every existing successful field. An
  artifact write failure returns `status: "failed"`, `error_code:
  "ARTIFACT_WRITE_FAILED"` — never a silent fallback to a lossy inline
  truncation.

  The inline-vs-artifact threshold is measured against the exact canonical
  serialization of the content that would actually be returned inline, not
  merely against a compact file-representation proxy for it. For search
  these are the same value (the rendered JSON result list is both the
  decision content and the file content). For browse they can differ
  substantially: the threshold is measured against the JSON serialization of
  the complete structured `blocks` array (what is actually returned inline),
  while the artifact file itself — if spilled — still holds the smaller
  plain joined-text document. A page with many small blocks can have joined
  text well under the default 50000-char threshold while its structured
  `blocks` form is several times larger, large enough to cross the threshold
  (and, left undetected, even the unrelated generic 200000-char preventive
  ceiling) — this is measured explicitly rather than left to be silently
  caught later by the generic mechanism's own lossy preview. When the
  decision length differs from the file's own `content_chars`, the artifact
  envelope adds `delivery_decision_chars` (the structured-serialization
  length that triggered the spill) and `delivery_decision_basis`
  (`"structured_blocks"` for browse) — `content_chars`/`content_sha256`
  always describe the file actually written, never implying that the file
  itself exceeded the threshold when the real trigger was the larger
  structured form. An inline Browse response's own `content_chars` likewise
  reflects the structured `blocks` serialization actually returned, not the
  joined-text length.

  The artifact writer shares the kernel's one atomic-write primitive
  (`kernel/tool_result_artifacts.write_artifact_file`) and the kernel's one
  canonical artifact directory (`WorkdirLayout.tool_results_dir`) with the
  generic preventive spill (`spill_oversized_result`, still the unrelated,
  unchanged 200000-char outer safety net) — there is exactly one atomic-write
  code path and one artifact directory, not two parallel conventions. The web
  artifact envelope is explicitly recognized by the kernel's
  `is_spill_manifest` via its own namespaced `artifact` marker and required
  structural fields (`file_path`, `content_chars`, `content_sha256`) — a
  dedicated recognition branch independent of the generic manifest's
  `status: "spilled"` shape, since a web artifact's own `status` is the
  family's "ok"/"failed" value, never "spilled". This explicit recognition,
  not envelope smallness, is what stops the generic preventive spill from
  re-spilling an already-built web artifact, and holds even if the envelope
  is padded past the generic 200000-char ceiling.

## Contract tests

Focused direct checks cover canonical and legacy configuration normalization,
opaque dependency identity, schema/prompt/catalog uniqueness, lazy provider
construction, action-owned settings file states (missing, valid, malformed,
wrong schema, unknown/duplicate fields, disallowed selector, symlink/non-
regular file, changed-file-observed-next-call), no old-path cross-read,
explicit argument rejection, environment immutability, search-to-browse
continuation, and manual/browse operation with invalid settings and no
provider construction. Existing browser Core/Port and SearchService contract
tests remain applicable. `tests/test_web_settings_action.py` additionally
proves declaration/action order, exact row-key and five-field equality,
current/default/configurable values, exact manual targets, full private
credential redaction, one fixed no-row failure when current truth is
unavailable, no mutation input, and unchanged ordinary search behavior.
Provider ownership/routing checks cover: the real
no-config `setup(agent)` path composes all four canonical specs and
genuinely selects OpenAI via its standard `OPENAI_API_KEY` env var when set
(proved with real environment isolation, not a test-only injected `engines=`
set standing in for the default), else DuckDuckGo, without overriding an
explicit operator default; MiniMax/Zhipu are absent from `PROVIDERS` and
raise `RetiredProviderError` from the flat-`provider=`, `default_engine=`,
and map-shaped `engines={}` composition paths alike — never a DuckDuckGo
substitution — while a genuinely unrecognized/inherited legacy provider name
keeps the pre-existing `legacy_fallback_from` DuckDuckGo behavior; a
composition-time `default_engine=`/`provider=` naming `anthropic`/`gemini`
raises the distinct `SettingsOnlyProviderError` (never `RetiredProviderError`
— both are still active canonical providers), and only a valid hot-read
`search.engine` env/document selection (live-changed, no refresh required) can
select either, subject to canonical-backend eligibility that succeeds for a
truthfully-canonical backend and fails `PROVIDER_BACKEND_INELIGIBLE` (no
provider construction, no search call) on every non-canonical backend
including Claude Code and `custom`/aliased providers; a settings-selected
Anthropic/Gemini runtime failure raises the adapter's own typed
`AnthropicSearchError`/`GeminiSearchError` (including Anthropic's official
in-body `web_search_tool_result_error`) and reports `SEARCH_FAILED` with a
bounded `provider_failure_class`, proved end-to-end through the real adapter
class plus `WebManager`, never invoking DuckDuckGo; an OpenAI runtime
failure raising the typed `OpenAISearchError` falls back to exactly one
DuckDuckGo search with a comment line and bounded dual failure-class
provenance, a non-`OpenAISearchError` exception (a programming defect) fails
normally without touching DuckDuckGo, and a non-OpenAI engine's runtime
failure never triggers that fallback; all three canonical providers'
`SearchService.search()` are proved, using provider-shaped fake Responses/
Anthropic/Gemini objects passed through the real extraction code, to return
nonempty results with real link refs when official citations/grounding
chunks/result blocks are present, and exactly one bounded narrative
result with `link_ref: null` (never a fabricated one) when the official API
legally returns a citation-free grounded narrative. A real
fresh Agent startup must prove exactly
`action` / `input` / `reasoning` / `summarize` at the root, no cross-cutting
field in any nested input branch, internal `_reasoning` dispatch,
resident/batched prompts, and both Chat and Responses tool wires. Executor-
level evidence proves the raw result is durably logged before any visible
`summarize=true` replacement on both the sequential and a controlled-parallel
path, and that search/browse `status: "failed"` results stay byte/content
exact and unsummarized under `summarize=true`.

Additional focused checks cover: `settings/web.json` missing/default (with a
deterministic revision/hash for the default case)/valid-override/boundary/
out-of-range/wrong-type/unknown-field states and its
`WEB_OUTPUT_SETTINGS_INVALID` failure before any provider call or fetch;
manual and the settings-isolation spy tests extended to assert
`read_output_settings` is never called from manual; search with a large
(hundreds-of-item) finite provider result set preserved completely with no
count cap, no per-field truncation, and no adapter receiving a LingTai count
parameter; a synthesized/URL-less result preserved in the result set without
`link_ref`; inline and artifact delivery for both search and browse,
including exact `content_chars`/`content_sha256` verified against the written
file, the artifact's `output_setting_source`/`revision`/`hash` fields,
Unicode character accounting, threshold boundary on both sides, atomic
unique artifact filenames under the canonical `tmp/tool-results/` directory
across rapid calls, no content preview when spilled, `ARTIFACT_WRITE_FAILED`
on a simulated write failure, an end-to-end spill-then-`file.read` round
trip, Browse's per-call `max_chars` override of the shared threshold, a
legacy `cursor` locator returning the complete document under the same
policy, and — directly, not by inference from envelope size — that the
kernel's `is_spill_manifest` recognizes a web artifact envelope via its own
explicit marker and that neither `spill_oversized_result` nor a full
`ToolExecutor.execute()` pass re-spills an already-built web artifact result,
even when the envelope is deliberately padded past the generic 200000-char
preventive ceiling.

A dedicated production-shaped regression (a page with thousands of small
extracted blocks whose joined plain text stays under the default threshold
while the structured `blocks` JSON that would actually be returned inline
does not) proves the threshold decision is made against the structured
serialization: the case spills to Web's own complete, no-preview artifact
with truthful `delivery_decision_chars`/`delivery_decision_basis` fields and
a `content_chars` describing only the written file, and a companion test
proves the generic preventive spill never substitutes a lossy preview for
it even though the structured decision length also exceeds the generic
200000-char ceiling. A matching small-page control proves ordinary pages
stay inline under both measurements, and an exact-boundary test pins the
decision to the structured length precisely. A separate deterministic test
evicts the fetched snapshot between the engine's success and the delivery
decision and proves the result is `error_code: "BROWSE_SNAPSHOT_UNAVAILABLE"`
with no `blocks`/`partial`/`next_cursor`/`delivery` field ever present —
never a degraded first-page success.

## Maintenance

Keep this Contract and `ANATOMY.md` reciprocal and keep the web-manual edge on
both. Physical legacy modules, provider-native wire strings, and internal
browser files remain retained; they are not additional model-facing surfaces.
