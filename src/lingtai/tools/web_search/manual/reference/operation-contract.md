---
name: web-manual-operation-contract
description: >
  Detailed web action contract: search-to-browse references and cursors,
  settings ownership, complete inline/artifact delivery, provider fallback,
  public URL boundaries, and the strict manual child.
version: 1.0.0
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
  - src/lingtai/tools/web_search/manual/SKILL.md
  - src/lingtai/tools/web_search/__init__.py
  - src/lingtai/tools/web_search/settings.py
  - src/lingtai/tools/web_search/_spill.py
  - src/lingtai/tools/browser/core.py
  - src/lingtai/tools/web_search/CONTRACT.md
  - tests/test_unified_web_capability.py
  - tests/test_web_output_spill.py
  - tests/test_web_settings_action.py
maintenance: |
  Keep this detailed operation contract aligned with the web schema, settings
  readers, delivery helper, BrowserEngine, and the parent web-manual router.
  The parent stays a first-call router; this reference owns the procedural
  detail and safety boundaries. Preserve complete-output, no-fallback, and
  same-Agent reference semantics when updating either implementation or docs.
---
# Web operation contract

Load this reference after the short [`web-manual`](../SKILL.md) router when a
call needs exact settings, delivery, continuation, provider-routing, or
fallback semantics. It is documentation for the existing `web` action; it does
not add a second tool or a new action.

## Action envelope and first-call rules

The public envelope is closed: `action`, `input`, required `reasoning`, and
optional root `summarize`. `summarize` is not action input, defaults to false,
and must never be nested in `input`; failed results are always returned exactly,
without summarization. Each action receives only its own strict input branch.
The `settings` and `manual` branches are exactly `{}`. Browse optional fields
are present as JSON `null` when unused; dispatch normalizes those nulls to
omission.

Use search first when discovery is needed:

```text
web(action="search", input={"query": "precise question"}, reasoning="discover current sources")
```

Search returns every result supplied by the selected provider for that call,
without a LingTai result-count cap or per-field slice. A result is
`{title,url,snippet}` plus a same-Agent `link_ref` when it has a usable HTTP(S)
URL. A provider-supplied narrative without a citation is retained with an
empty URL and `link_ref: null`; the URL and reference are never fabricated.
`count` is the true returned result count. Search does not fetch page bodies and
never accepts an action-level `engine` field.

Browse a search result by carrying its reference into the next call:

```text
web(action="browse", input={
  "url": null,
  "link_ref": "<link_ref>",
  "cursor": null,
  "extract": null,
  "max_chars": null
}, reasoning="read the selected source")
```

A direct public URL is also valid:

```text
web(action="browse", input={
  "url": "https://example.test/page",
  "link_ref": null,
  "cursor": null,
  "extract": null,
  "max_chars": null
}, reasoning="read the selected source")
```

A browse request must supply exactly one public HTTP(S) URL or same-Agent
`link_ref`; do not invent references or use a private, non-HTTP, local, or
credential-bearing URL. `link_ref` resolves the URL recorded by this Agent's
search/browse state. A same-Agent `cursor` may accompany that same URL/reference
as a compatibility locator for an already fetched cached snapshot, but it is
not a third target and cursor-only input fails `INVALID_TARGET`. The cursor does
not refetch the page or request another partial page. A fresh fetch and a
cursor-bearing call both deliver the complete extracted document for that
fetch. A fresh success never mints `next_cursor`.

Browse is a static, read-only, SSRF-vetted HTTP(S) GET. It preserves links,
provenance, source hash, an untrusted-content marker, and typed failures. It
never provides JavaScript execution, PDF extraction, login, cookies, forms,
hidden search fallback, or a provider-dependent route. If a cached snapshot is
evicted between fetch and delivery, fail with
`BROWSE_SNAPSHOT_UNAVAILABLE`; never return the engine's first page as a
partial substitute.

## Settings and the manual action

`web(action="settings", input={}, reasoning="inspect effective web settings")`
is SHOW-only. Success is exactly `{"settings": [...]}` with rows containing
only `key`, `current`, `default`, `configurable`, and `comment`. Credentials
are always `<redacted>`. An unavailable hot-read source returns one bounded
`SETTINGS_UNAVAILABLE` failure with no partial rows. `configurable: true`
points to an authorized procedure; it does not grant this action write access.
There is no set/reset/mutation API, receipt, generic writer, or environment
mutation. Call SHOW again after the authorized external change.

The settings rows and their manual anchors are:

### provider

Startup snapshot of singular flat setup composition, not the per-call engine;
`automatic` is its meaningful default and multi-engine composition is `null`.
Change only through authorized setup/manifest composition and rebuild or
relaunch as that procedure requires.

### model

Startup snapshot of the flat `model=` choice; `provider-default` is the
meaningful fallback and multi-engine composition is `null`. Change through the
existing authorized setup/manifest procedure, never through SHOW.

### api-key

Private startup composition route (`api_key`/`api_key_env`). Both values are
redacted and there is no generic `LINGTAI_WEB_API_KEY`; update only an
authorized launcher/secret store or Web composition and never put a secret in
a call, prompt, report, or settings file.

### engines

Sorted names admitted by immutable Web composition. The no-config set is
`anthropic`, `duckduckgo`, `gemini`, and `openai`. Admission is not proof that
an engine is usable; change it only through authorized setup/manifest
composition and rebuild or relaunch.

### search-engine

Hot selector precedence is `LINGTAI_WEB_ENGINE`, then the exact
`<agent-workdir>/settings/web.search.json` document, then the composed runtime
fallback. Sources are read for every search and SHOW. The file is a strict v1
selector containing only `{"schema_version": 1, "engine": "<admitted>"}`;
it does not install providers or carry credentials. A present invalid,
unknown, unavailable, or credential-missing selection fails loudly rather than
falling through. Browse and manual never read this search-only file.

### output-max-chars

Shared search/browse delivery threshold. Precedence is
`LINGTAI_WEB_MAX_CHARS`, then the exact `<agent-workdir>/settings/web.json`
strict v1 document, then `50000`. Accepted values are integers `1..100000`;
Browse's per-call `input.max_chars` overrides one call only. Manual never reads
this file, and output tuning grants no access.

### openai-api-key

`credentials.openai_api_key` is the admitted OpenAI credential route. Both
values are redacted; the canonical no-config route is `OPENAI_API_KEY`. Update
only the authorized private launcher/secret-store or engine composition.

### anthropic-api-key

`credentials.anthropic_api_key` is redacted and uses `ANTHROPIC_API_KEY` in
the canonical no-config composition. It does not bypass canonical-backend
eligibility or settings-only selection.

### gemini-api-key

`credentials.gemini_api_key` is redacted and uses `GEMINI_API_KEY` in the
canonical no-config composition. It does not bypass canonical-backend
eligibility or settings-only selection.

The manual action is the zero-input, no-network route:

```text
web(action="manual", input={}, reasoning="load web guidance")
```

It reads the installed `capabilities/web/SKILL.md` and performs no provider
construction, browser request, or settings read. It remains usable when
settings are malformed. Missing installation returns an honest degraded result
with the expected path; non-empty input is `INVALID_ARGUMENT`.

## Output size and complete artifacts

Search and browse build their complete canonical content before applying one
hot-read threshold: `LINGTAI_WEB_MAX_CHARS`, then `settings/web.json`, then
`50000`. A present invalid environment value or file fails with
`WEB_OUTPUT_SETTINGS_INVALID` before a provider call or page fetch. The strict
file is:

```json
{"schema_version": 1, "max_chars": 50000}
```

`schema_version` must be integer `1`; `max_chars` must be an integer (not a
boolean, float, or string) from `1` through `100000`. Unknown or duplicate
fields, malformed or non-UTF-8 JSON, symlinks, non-regular files, oversize or
unstable files are invalid. The two settings files are separate concerns:
`web.search.json` selects an engine and `web.json` selects delivery; they are
never merged, overlaid, or cross-read. Manual reads neither.

The threshold compares the exact canonical serialization that would be returned
inline. Search measures its rendered JSON result list. Browse measures the JSON
serialization of the complete structured `blocks` array, not the smaller
joined-text file representation. At or below the threshold the complete result
is inline with `delivery: "inline"` and exact `content_chars` (for browse, the
structured serialization length). Above it, the complete content is atomically
written under the canonical workdir-relative `<agent-workdir>/tmp/tool-results/`
directory and the response carries `delivery: "artifact"`, the namespaced
Web artifact marker, workdir-relative `file_path`, exact file
`content_chars`/`content_sha256`, `content_kind`/`format`/`encoding`, and the
output-setting source/revision/hash. Search omits `results`; browse omits
`blocks`, `partial`, `next_cursor`, and `returned_chars` from a spilled
response. There is no preview or lossy prefix. Browse adds
`delivery_decision_chars` and `delivery_decision_basis: "structured_blocks"`
when its decision length differs from the written plain-text file length. A
write failure is `ARTIFACT_WRITE_FAILED`, never a lossy inline fallback. Read a
spilled file in full with the existing `file.read` action.

## Provider routing and explicit fallback

Built-in admission is exactly `openai`, `anthropic`, `gemini`, and
`duckduckgo`. Anthropic and Gemini are active canonical providers but may be
selected only by a valid hot-read engine setting and only when the current
Agent backend is exactly the corresponding canonical identity. Aliases,
Claude Code, `custom`, OpenRouter, and other wire-compatible names do not pass
that identity gate. A composition kwarg naming Anthropic/Gemini raises the
distinct `SettingsOnlyProviderError`; MiniMax/Zhipu are retired and raise
`RetiredProviderError`. No provider credential or selection can be inferred
from another Agent service.

With no explicit operator default, the built-in selector chooses canonical
OpenAI only when its admitted spec is genuinely available; otherwise it chooses
DuckDuckGo when composed, or no default when neither is usable. Anthropic and
Gemini are never selected as the built-in default.

Only a typed `OpenAISearchError` triggers an automatic **runtime retry**:
exactly one DuckDuckGo attempt. A successful retry reports the selected OpenAI
engine, `actual_engine: "duckduckgo"`, a comment, and bounded
`openai_failure_class`; if DuckDuckGo fails, return `SEARCH_FAILED` with both
bounded failure classes. Typed Anthropic/Gemini failures, other provider
failures, and manager/programming exceptions fail without retry; raw SDK text,
request bodies, credentials, and exception details never enter the result. A
genuinely successful provider response with no results is `[]`, not an error.

Separate from that runtime retry, the retained composition compatibility path
maps a genuinely unrecognized/inherited legacy provider name to one
DuckDuckGo spec tagged with `legacy_fallback_from`. This happens before a search
call and performs no failed-provider attempt. Deliberately retired MiniMax or
Zhipu names instead raise `RetiredProviderError`; they never use this mapping.

## One explicit legacy fallback and public boundary

Static browse intentionally does not become an interactive browser or a tier
chain. For a typed unsupported-content or `NO_TEXT_BLOCKS` failure, choose one
named procedure: `scripts/extract_page.py --tier 0` for a PDF, a
source-specific API for structured data, or the documented Playwright/academic
reference under `reference/`. Do not advertise or silently chain another
public capability. The compact tier index is
[`tier-quick-refs/SKILL.md`](tier-quick-refs/SKILL.md); site routing and known
limitations are in [`routing-and-sites/SKILL.md`](routing-and-sites/SKILL.md).

When a documented HTTP fallback is authorized for a public page and the default
`curl` request is empty or clearly incomplete, retry once with a search/AI
crawler User-Agent such as `OAI-SearchBot`, `Claude-User`, or `Bytespider`.
Keep this public, read-only, rate-respecting, and limited to the page's public
representation. Never impersonate a person, bypass a login, paywall, robots
directive, or any other access control; never chain identities.

For forms, logins, JS-heavy SPAs, uploads, or verification requiring a real
browser, load [`agent-native-browser.md`](agent-native-browser.md) and use its
explicit Chrome DevTools MCP procedure. That route is not a `web` action.
