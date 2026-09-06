---
id: review_delegation_instruction_check
title: Review delegation instruction check
kind: meta-guidance-section
summary: >
  Resident guidance requiring agents to re-anchor recent human instructions before delegating
  reviews or implementation checks.
why: >
  This fragment exists because review daemons can amplify stale scope or authorization mistakes
  unless the parent frames them with the latest human contract.
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
Before sending a PR, diff, or implementation to any reviewer or review daemon, re-check recent human-channel instructions for missed scope, boundary, or authorization changes — via the producer channel, not memory or a notification digest alone, using any exact window the human specified (e.g. the last 30 Telegram messages). Then frame the reviewer with the latest contract: what changed, what is out of scope, what side effects are unauthorized, and which instructions were checked. This is system/procedure discipline, not a personal standing rule.
