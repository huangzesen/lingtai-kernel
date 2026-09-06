---
id: token_efficiency
title: Token efficiency state
kind: meta-guidance-section
summary: >
  Resident guidance for interpreting the `_meta.agent_meta.agent_state.token_usage` block — nested into a
  `current_call` half (this provider call's own facts) and a `session` half (since-last-molt
  cumulative token/cache economy plus current context state, surviving refresh).
why: >
  This fragment exists because token/caching numbers are dynamic runtime scalars; agents need a
  stable interpretation hook without repeating the full token-efficiency procedure in each tool
  result.
related_files:
  - "src/lingtai/prompts/principle/principle.md"
  - "src/lingtai/prompts/meta_guidance/catalog/INDEX.md"
maintenance: >
  When editing this file, treat related_files as maintained inner links for the prompt/guidance
  source graph. Before changing behavior or prose, crawl the listed files, update any affected
  reciprocal link on the other side (principle links to each prompt/guidance source; each such
  source links back to principle; guidance INDEX links to each guidance section and each section
  links back to INDEX), and keep this list generous enough for future maintainers to find adjacent
  prompt layers. Do not list tests merely because they validate the contract; add loaders,
  manifests, or package metadata only when this file actually discusses them or the prompt-source
  relation needs that link.
---
Read `_meta.agent_meta.agent_state.token_usage` as the single home for all token diagnostics and current context state; immutable execution facts remain under `_meta.tool_meta`. It has two explicitly named halves so `input` (this call) and `input_tokens` (session total) are never ambiguous. `token_usage.current_call` is ONLY this result's own provider call: `input`, `cache_miss`, `cache_rate` (cached/input, 0-1), `output`, `thinking`. `token_usage.session` is the **since-last-molt cumulative** aggregate — it survives refresh/restart, is not a since-refresh delta, and carries `session_cache_rate`, `api_calls`, `input_tokens`, `cached_tokens`, `avg_input_tokens_per_api_call`, plus current context state (`context_tokens`, `context_window`, `context_usage` = tokens/window) and ALWAYS-ON cache-miss telemetry: `cache_miss_tokens` (= `max(input_tokens - cached_tokens, 0)`), `cache_miss_budget`, and `cache_miss_remaining_tokens` (= `max(cache_miss_budget - cache_miss_tokens, 0)`; the budget pair appears only when a budget is configured). A short top-level `ref` field (`See meta_guidance.token_efficiency for details.`) hooks back here. Each half is omitted (not left empty) when unavailable, and missing inner values are omitted rather than invented.

Apply the resident token-efficiency/progressive-disclosure principle when deciding to pre-summarize, delegate to daemons, or molt. At a completed task boundary, molt only when context pressure (≥85%), explicit human request, or conversation confusion makes it worth the cost. The soft **cache-miss budget** guards the same since-last-molt `cache_miss_tokens` total — read the live value via `system(action="settings", input={})`, see `system-manual#cache-miss-budget`, rather than assuming a fixed number. Once reached, `_meta.agent_meta.agent_state.context.molt` carries a `cache miss budget {N} reached, molt now` reminder; it is a soft cap, not a block, so molt to restore cache efficiency rather than using summarize to reconstruct (which itself creates a large cache miss). Watch `token_usage.session.cache_miss_remaining_tokens` to see how close you are before the guard trips.
