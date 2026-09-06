---
id: summarize_best_practice
title: Summarize and molt deliberately
kind: meta-guidance-section
summary: >
  Resident guidance for when and how agents summarize consumed tool results and choose molt
  boundaries.
why: >
  This fragment exists because tool-result summarization is a high-attention runtime behavior: it
  keeps the current session efficient without losing recoverability, and it tells agents when molt
  supersedes summarize.
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
  relation needs that link. Keep this resident hook aligned with (not looser than) the gate owned
  by `context/reference/summarize-manual/SKILL.md` §2; move detail there, not back here.
---
Use progressive disclosure for tool results: raw output is for inspection, not routine storage. Prefer a priori `summary=true` before `shell`/`file` (read/glob/grep)/`daemon` when you already know the facts, counts, anchors, or conclusion you need — encode that retention contract in `reasoning` — and delegate noisy or bulky work to daemons before it lands in main context. Treat a posteriori `context(action="summarize")` as a last resort, not routine cleanup: use it only when context is close to overflowing and a molt is unsuitable, batching already-digested results when convenient. Follow any adapter/provider static rules in resident `meta_guidance` too. Summarize is a mini molt for consumed tool results; skip it if you have already decided to molt. Do not molt merely because the current task is complete — molt only when context pressure (≥85%), summarize plus automatic reconstruction still cannot bring context below `0.75 * context_window`, the human explicitly asks for a reset, or conversation confusion makes the fresh briefing worth the cost. See `reference/summarize-manual/SKILL.md` (via `context-manual`) for exact cadences, `summary_effect` savings/lesson-deposit fields, delayed-reconstruction mechanics, and `tool_call_id` recovery.
