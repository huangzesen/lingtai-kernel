---
id: summarize_reconstruction_threshold
title: Delayed summarization reconstruction threshold
kind: meta-guidance-section
summary: >
  Resident guidance explaining that summarize records compact history immediately but
  provider-context reconstruction happens later at the threshold.
why: >
  This fragment exists so agents do not waste calls trying to force summarize reconstruction, do
  not assume raw blocks vanished too early, and know when to molt instead.
related_files:
  - "src/lingtai/prompts/principle/principle.md"
  - "src/lingtai/prompts/meta_guidance/catalog/INDEX.md"
  - "src/lingtai/tools/context/manual/reference/summarize-manual/SKILL.md"
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
Summarize records an agent-authored replacement immediately; provider-context reconstruction happens later at the threshold — the raw block does not vanish before then. Runtime token/context state, rebuild/molt hints, and the one-shot reconstruction event are under `_meta.agent_meta.agent_state`; immutable per-execution facts stay under `_meta.tool_meta`. The latest whole `_meta.agent_meta` is current and every older holder is a historical trace; a fresh replay preserves those traces without rewriting canonical history (only the summarize replacement changes a historical tool-result body). At 0.85, deliberate rebuild is permitted; at 1.0, one forced rebuild occurs per continuous overflow episode, followed by recovery/molt guidance when still needed.
