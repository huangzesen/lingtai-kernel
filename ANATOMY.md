---
related_files:
  - AGENTS.md
  - BEHAVIORS.md
  - CLAUDE.md
  - CODE_OF_CONDUCT.md
  - CONTRACT.md
  - CONTRIBUTING.md
  - ENVIRONMENT_VARIABLES.md
  - GLOSSARY.md
  - LICENSE
  - MANIFEST.in
  - NOTICE
  - README.md
  - RELEASING.md
  - SECURITY.md
  - SUPPORT.md
  - .gitignore
  - docs.yaml
  - .github/PULL_REQUEST_TEMPLATE.md
  - .github/ISSUE_TEMPLATE/bug_report.yml
  - .github/ISSUE_TEMPLATE/config.yml
  - .github/ISSUE_TEMPLATE/feature_request.yml
  - .github/workflows/kernel-macos-smoke-pr.yml
  - .github/workflows/kernel-windows-pr.yml
  - .github/workflows/shell-windows-pr.yml
  - .github/workflows/wheels.yml
  - crates/lingtai-search-sidecar/ANATOMY.md
  - dev-guide-skill/SKILL.md
  - discussions/headless-runtime-contract.md
  - IMPLEMENTATION_REPORT.md
  - docs/examples/agent-plugins/hello-lingtai/mcp.json
  - docs/examples/agent-plugins/hello-lingtai/plugin.json
  - docs/examples/agent-plugins/hello-lingtai/server.py
  - docs/examples/agent-plugins/hello-lingtai/skills/hello-lingtai/SKILL.md
  - docs/plans/2026-06-25-fsutil-migration.md
  - docs/plans/2026-07-14-powershell-adapter-readiness.md
  - docs/readmes/README.wen.md
  - docs/readmes/README.zh.md
  - docs/references/acknowledgements.md
  - docs/references/claude-code-guide.md
  - docs/references/codex-http-anatomy-investigation.md
  - docs/references/licc-notification-wake-runbook.md
  - docs/references/lifecycle-clock.md
  - docs/references/runtime-vs-agent-session-objects.md
  - docs/references/windows-support.md
  - migration/migration.md
  - pyproject.toml
  - reports/ANATOMY.md
  - setup.py
  - scripts/check_docs_governance.py
  - scripts/generate_release_manifest.py
  - scripts/publish_release_assets.py
  - scripts/sync_gitee_mirror.py
  - scripts/lib/release_manifest.py
  - src/lingtai/ANATOMY.md
  - src/lingtai/CONTRACT.md
  - src/lingtai/prompts/ANATOMY.md
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/init.jsonc
  - src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md
  - src/lingtai/kernel/ANATOMY.md
  - src/lingtai/kernel/snapshot/ANATOMY.md
  - tests/ANATOMY.md
  - tests/CONTRACT.md
  - tests/test_architecture_documents.py
maintenance: |
  This file is both the repository-root anatomy and the normative
  anatomy-of-anatomy for the distributed code navigation system. Keep
  related_files repo-relative, duplicate-free, and linked to real files. Keep
  the root CONTRACT.md reciprocal and update the paired conventions together
  when their boundary changes. Code is the structural source of truth: repair
  stale navigation in the same change that moves files, symbols, connections,
  composition, or state. Preserve the child template and its maintenance rule;
  validate the distributed graph before merge.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see ## Maintenance).
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
---
# LingTai Distributed Code Navigation Convention

## Purpose

**ANATOMY is the distributed code navigation system.** Each architectural layer
keeps an `ANATOMY.md` beside the code it maps: files, symbols, responsibilities,
connections, composition, and state. Those local maps link into a graph that an
agent can descend from this repository root to the exact code that answers a
structural question.

This file has two roles. It is the repository's top-level map, and it is the
**anatomy of anatomy**: the normative meaning, template, link rules, and
maintenance contract for the distributed navigation system.

`ANATOMY.md` and [`CONTRACT.md`](CONTRACT.md) are a pair, not duplicates:

- Anatomy describes **where code is and how it is composed**. Code is the
  structural source of truth.
- **CONTRACT is the distributed code interface definition system.** It defines
  **how a layer may be used and what it promises**. The contract is normative
  when implementation behavior disagrees.

[`GLOSSARY.md`](GLOSSARY.md) is the separate glossary-of-glossaries for
distributed tool-glossary resources. It governs model-facing alias/localized
name help; it does not define code structure or interface behavior.

## Navigation model

Navigation is distributed rather than centralized. The root defines the system
and global entry points; each architectural component maps only the layer it
owns; parent/child and related-file links connect the layers. Do not copy every
local fact into this root file.

For structural questions, descend the anatomy graph: read this file, choose the
relevant component, open its anatomy, and repeat until it points at code. For
enumeration questions such as every callsite or every matching file, use search.
Anatomy is a navigation aid; cited code remains the evidence.

A folder earns an anatomy when a competent agent can reason usefully about it as
an architectural unit without first reading all siblings. Pure helper folders,
single value objects, and trivial leaves do not receive ceremonial anatomies.
If a component owns nested architectural components, each child may own its own
paired anatomy and contract.

The target skeleton for a governed component layer is:

```text
<component>/
├── ANATOMY.md   # distributed code navigation: structure and composition
├── CONTRACT.md  # distributed interface definition: Core/Ports/Adapters
└── ... code
```

Existing anatomy files remain useful navigation during staged migration. A
component enters the paired governed system when its co-located contract is
linked from the root contract. An implementation, Adapter, or navigation-only
Anatomy that owns no separate promise instead points to its one owning
component Contract and explains why no independent local Contract exists. The
full pairing, ownership, progressive-disclosure, and mismatch-reporting rule
lives only in root `CONTRACT.md`; follow it rather than copying it here.

## Frontmatter convention

A root-governed paired component anatomy has exactly two YAML frontmatter
fields, in this order:

1. `related_files`: a non-empty, duplicate-free list of repo-relative regular
   files. It includes the paired `CONTRACT.md` for a governed component, the
   parent and direct-child anatomies needed to traverse the graph, and the code
   files that own the mapped layer.
2. `maintenance`: a concise generic note based on the template below. The root
   uses a root-specific maintenance statement because it also governs the
   system.

Paths MUST be repository-relative, MUST resolve to files, MUST NOT contain `.`
or `..` path segments, and MUST use `/` separators.

## Body convention

A root-governed paired component anatomy starts with one paragraph defining
what the layer is, then uses these five `##` sections once and in this order:

1. `## Components` — files, functions, classes, or child components with
   verified `file:line` citations and one-line purposes.
2. `## Connections` — callers, callees, and data/control flow across the layer.
3. `## Composition` — parent, direct child anatomies, and structurally relevant
   siblings.
4. `## State` — persistent state written and ephemeral state managed.
5. `## Notes` — bounded gotchas or rationale not evident from code.

Root-governed paired component anatomies SHOULD remain near 80 lines. A larger map is evidence that
the layer may contain smaller components. No empty leaf stubs are allowed.
Every structural claim and named symbol in `Components` MUST cite verified code;
links to another anatomy use repo-relative paths.

This root anatomy is the only exception to the component body and size shape: it
also carries the meta-convention and repository-wide entry points.

## Link and pairing semantics

The paired distributed systems obey these structural rules. Root
[`CONTRACT.md`](CONTRACT.md) is the single source for governed-component
pairing, unique implementation/navigation ownership, mutual progressive
disclosure, and fail-loud mismatch reports; do not duplicate that rule here.

1. This root anatomy and root contract list each other in `related_files`.
2. A root-governed component's co-located `ANATOMY.md` and `CONTRACT.md` list
   each other exactly once.
3. Parent/child anatomy links are reciprocal so navigation can descend and
   return. Do not enumerate unrelated downstream callers as graph edges.
4. The component contract owns interface behavior; the component anatomy owns
   structure and composition. Cross-link instead of copying the same rule into
   both files.
5. A structural or composition change updates anatomy in the same PR. A Port,
   Adapter, or behavioral-promise change updates contract and contract tests in
   the same PR. A change affecting both updates the pair together.
6. Orphans, missing targets, duplicate links, one-way pair links, and unpaired
   governed components are defects and MUST fail validation. Every tracked file
   must appear in some `related_files` list so the whole tree climbs from this
   root anatomy; a tracked file in no list is an orphan.
7. A capability's manual is a navigation target linked from **both** owner twins:
   the paired `ANATOMY.md` lists it in `related_files` as a route to the manual,
   and the capability `CONTRACT.md` lists the same manual as its interface owner.
   The normative both-edges requirement is owned by root
   [`CONTRACT.md`](CONTRACT.md) `## Design principles` (principle 4); this anatomy
   only names the navigation edge and does not restate that rule. A manual reached
   from only one twin is a missing-edge defect.

## Components

- [`dev-guide-skill/`](dev-guide-skill/) — the repository-local agent dev kit:
  its skill routes agents into the Anatomy and Contract systems and may grow
  focused scripts, references, templates, or assets as real workflows recur.
- [`scripts/`](scripts/) — root utility/checker scripts, including the
  docs-governance validator described below and the release-manifest
  generator/publisher/Gitee-mirror-sync scripts described in
  [`RELEASING.md`](RELEASING.md). `wheels.yml`'s `release-manifest` job
  invokes the generator and the publisher (always with `--skip-gitee`;
  no path in that workflow touches Gitee) with `--execute` on a real
  `release.published` event (or an explicit `workflow_dispatch` with
  `publish: true`) — every other trigger shape stays dry-run. Only after that
  publish step actually executes, the job's "Notify lingtai-web download
  mirror" step dispatches a `repository_dispatch` to `Lingtai-AI/lingtai-web`
  so it can mirror the same bytes for download acceleration (see
  `RELEASING.md` "Download-mirror dispatch"); this never runs on a dry run
  and never touches the GitHub release itself.
- [`.github/`](.github/) — GitHub Actions, issue templates, and pull request
  templates. `workflows/wheels.yml` is the release build/verify/manifest
  pipeline; `kernel-windows-pr.yml` and `shell-windows-pr.yml` are the native
  Windows PR gates and `kernel-macos-smoke-pr.yml` is the macOS smoke tier.
  `ISSUE_TEMPLATE/{bug_report,feature_request}.yml` plus
  `config.yml` and `PULL_REQUEST_TEMPLATE.md` are the contributor intake forms —
  the pull-request template carries its governance metadata in an HTML comment
  rather than a `---` fence, because GitHub injects its raw bytes into every new
  PR description (`docs.yaml` `metadata_mode_overrides`).
- [`crates/lingtai-search-sidecar/`](crates/lingtai-search-sidecar/) — Rust file
  search sidecar packaged with the Python runtime; descend through
  [`crates/lingtai-search-sidecar/ANATOMY.md`](crates/lingtai-search-sidecar/ANATOMY.md).
- [`docs/`](docs/) — durable documentation, plans, language-specific readmes,
  long-form references, and example plugins: `plans/` holds dated migration
  plans, `readmes/` the translated `README.{zh,wen}.md`, `references/` the
  long-form guides (`claude-code-guide.md`,
  `codex-http-anatomy-investigation.md`, `lifecycle-clock.md`,
  `licc-notification-wake-runbook.md`, `runtime-vs-agent-session-objects.md`,
  `windows-support.md`, `acknowledgements.md`), and `examples/` the
  `agent-plugins/hello-lingtai` sample plugin.
- [`discussions/`](discussions/) and [`migration/`](migration/) — narrative
  design records kept beside the code they argue about:
  `discussions/headless-runtime-contract.md` and `migration/migration.md`.
- [`reports/`](reports/) — append-only archive of generated release bundles and
  standalone investigation explainers; descend through
  [`reports/ANATOMY.md`](reports/ANATOMY.md).
- [`src/lingtai/`](src/lingtai/) — public package, compatibility surfaces,
  services, and the kernel implementation; descend through
  [`src/lingtai/ANATOMY.md`](src/lingtai/ANATOMY.md). Kernel sub-components
  descend one level further through
  [`src/lingtai/kernel/ANATOMY.md`](src/lingtai/kernel/ANATOMY.md).
- [`tests/`](tests/) — pytest suite for runtime, services, tools, packaging, and
  architecture-document validation; descend through
  [`tests/ANATOMY.md`](tests/ANATOMY.md). Its
  [`CONTRACT.md`](tests/CONTRACT.md) is a methodology charter that deliberately
  stays outside the governed contract graph, and that anatomy is a
  navigation-only inventory rather than its governed twin.

## Root files

- [`ANATOMY.md`](ANATOMY.md) — this repository map and anatomy-of-anatomy.
- [`CONTRACT.md`](CONTRACT.md) — the distributed code interface definition root
  and contract-of-contract.
- [`ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md) — the canonical
  environment-variable registry; use it for enumeration and per-variable
  behavior rather than duplicating a table here.
- [`docs.yaml`](docs.yaml) — the canonical, machine-readable all-doc
  metadata contract and authoring template; validated by
  [`scripts/check_docs_governance.py`](scripts/check_docs_governance.py)
  and [`tests/test_docs_governance.py`](tests/test_docs_governance.py).
- [`README.md`](README.md) — public English network entry point; translated
  readmes live under `docs/readmes/`.
- [`RELEASING.md`](RELEASING.md) — kernel release process: how
  `.github/workflows/wheels.yml` builds, verifies, and manifests wheel/sdist
  assets, and how an authorized run publishes them to the GitHub release
  (no path in that workflow synchronizes or publishes to Gitee).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — public contributor entry point.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), [`SECURITY.md`](SECURITY.md), and
  [`SUPPORT.md`](SUPPORT.md) — community and safety entry points.
- [`CLAUDE.md`](CLAUDE.md) — short Claude Code entry point; full guidance is
  [`docs/references/claude-code-guide.md`](docs/references/claude-code-guide.md).
- [`AGENTS.md`](AGENTS.md) — agent-facing definition of done: build/test
  commands and the validation checklist coding agents must satisfy before a
  change is called done.
- [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE) — legal metadata.
- [`.gitignore`](.gitignore) — the tracked/untracked boundary. It matters to
  this system because docs governance discovers *untracked-but-not-ignored*
  Markdown too, so what this file ignores decides what must carry frontmatter.
  `docs/` and `reports/` are ignored by default: durable files there are tracked
  only because they were force-added.
- [`pyproject.toml`](pyproject.toml), [`setup.py`](setup.py), and
  [`MANIFEST.in`](MANIFEST.in) — Python packaging and Rust-sidecar build hooks.

## Composition

`pyproject.toml` declares Python package metadata and delegates sidecar build
hooks to `setup.py`. `MANIFEST.in` connects Rust sources and packaged Markdown
resources to source distributions. Runtime source begins under `src/lingtai/`;
long-form material that is not a root entry point remains under `docs/`.

README exposes the repository knowledge network to humans and agents. The repository-local [kernel development skill](dev-guide-skill/SKILL.md)
supplies the workflow and routes each task into this Anatomy graph, the Contract
graph, focused tests, and narrower manuals. The
distributed navigation graph starts here and descends through the anatomies
listed in `related_files`; the distributed interface graph starts at
`CONTRACT.md`. A governed component joins both graphs through its co-located
pair.

Every paired document carries a maintenance frontmatter entry that routes
back to its normative root. Keep each entry concise, preserve the root's
maintenance and ownership guidance, and update the pair when structure or
normative behavior changes. [`tests/test_architecture_documents.py`](tests/test_architecture_documents.py)
checks the graph and path safety; the maintenance prose remains normative
documentation rather than a byte-identical snapshot.

## State

These architecture documents write no runtime state. Git history records their
changes. Each anatomy describes the persistent and ephemeral state owned by its
mapped component; if a code change moves state ownership or changes a schema,
the relevant anatomy changes in the same PR.

## Maintenance

Maintenance is part of reading:

- If code and anatomy disagree structurally, code is normally the current fact;
  repair the anatomy before leaving the change. If the code move itself is a
  defect, report or fix the code and keep the mismatch visible until resolved.
- If code and contract disagree behaviorally, do **not** rewrite the contract to
  match accidental behavior. Treat the implementation as defective unless an
  authorized contract change updates the Port, adapters, version, and tests.
- Verify every touched citation after moves, renames, splits, or ownership
  changes. The anatomy drift checker catches missing/out-of-range targets, not
  semantic misdescription.
- Keep parent/child and Anatomy/Contract pair or owner links reciprocal. Update
  the root convention, its validator, root development skill, README entry,
  and bundled anatomy router together when this system changes.
- **Capability mentions require explicit related files.** Whenever any
  repository document (README, guides, skills, release notes, blogs, or this
  anatomy's own prose) names a capability, behavior, or feature, the owning
  component anatomy MUST list in `related_files` the repo-relative code files
  that implement it, and the link MUST be bidirectional: the anatomy entry
  maps the mention to code, and the owning anatomy is reachable from that
  mention's document via the navigation graph. A prose mention without a
  `related_files` mapping is drift, not documentation; repair it in the same
  change that introduces the mention.
- Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.

## Template

```markdown
---
related_files:
  - <repo-relative paired CONTRACT.md>
  - <repo-relative parent ANATOMY.md>
  - <repo-relative direct-child ANATOMY.md, when any>
  - <repo-relative mapped code file>
maintenance: |
  Keep related_files repo-relative, duplicate-free, and linked to real files.
  Keep this component's ANATOMY.md and CONTRACT.md reciprocal and keep
  parent/child anatomy links bidirectional. Code is the structural source of
  truth: update this anatomy in the same change that moves files, symbols,
  connections, composition, or state. Verify every changed citation and run the
  architecture-document validation before merge.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
  Follow the root Anatomy/Contract pairing rule, report mismatches, and do not duplicate or auto-fix the rule here.
---
# <Component Name> Anatomy

<One paragraph defining the architectural layer this folder embodies.>

## Components

- `<symbol>` — purpose (`repo/relative/file.py:line-line`).

## Connections

## Composition

## State

## Notes
```
