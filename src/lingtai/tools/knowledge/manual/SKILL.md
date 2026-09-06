---
name: knowledge-manual
description: >
  Read before creating/organizing private durable knowledge
  (`knowledge/<name>/KNOWLEDGE.md`), nested indexes, or cross-references;
  explains how knowledge differs from portable skills.
version: 1.0.0
last_changed_at: "2026-07-19T00:00:00Z"
related_files:
- src/lingtai/tools/skills/manual/reference/cleanup-footprint-contract.md
- src/lingtai/tools/knowledge/__init__.py
- src/lingtai/tools/knowledge/ANATOMY.md
- src/lingtai/tools/knowledge/CONTRACT.md
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# The Knowledge Capability

Knowledge is an agent's private long-term memory. It is for facts, decisions, observations, local paths, mail context, and operational lessons that are useful to this agent but are not necessarily portable to every other agent.

The private-memory capability is named `knowledge`. It registers **no** public
tool. It owns the private catalog composer that scans `<agent>/knowledge/` and
injects the catalog into your `knowledge` prompt section; the only model-facing
surface is the read-only signpost `psyche(action="knowledge", ...)`, which
returns this manual and nothing else.

## Knowledge vs skills

Knowledge and skills are **isomorphic in layout and progressive disclosure**:
each entry is a folder with a marker file (`KNOWLEDGE.md` here, `SKILL.md`
there), the prompt carries only the catalog, and a top-level entry may act as a
routing/index parent over nested children. They are physically separate stores
with different audiences:

| Term / path | Meaning |
|---|---|
| `knowledge` capability | Private per-agent durable memory catalog (no public tool). |
| `<agent>/knowledge/<name>/KNOWLEDGE.md` | One private knowledge entry. |
| `skills` capability | Catalog of reusable portable procedures (no public tool). |
| `.library/intrinsic`, `.library/custom`, `.library_shared` | Skill shelves holding `SKILL.md` files — never private `knowledge` entries. |

Knowledge is **private, local, and non-portable** by default: it may reference
agent-local paths, mail IDs, and logs. Skills are **reusable, shareable, and
portable**.

**Direction matters:** private knowledge may point outward to skills; shared
skills must not point inward to private knowledge paths, mail IDs, or local
logs. If content is a reusable how-to another agent could apply without your
private context, write a skill instead.

## Layout

Each entry is a folder under `<agent>/knowledge/` with a `KNOWLEDGE.md` file:

```text
<agent>/knowledge/
└── <name>/
    ├── KNOWLEDGE.md
    ├── references/
    ├── scripts/
    ├── assets/
    └── notes/
```

`KNOWLEDGE.md` starts with YAML frontmatter:

```markdown
---
name: <name>
description: One short sentence shown in the prompt catalog.
version: 1.0.0
---

# Title

Full notes live here.
```

Required fields are `name` and `description`. Supporting files are optional and can be any useful text, script, data sample, log, or asset.

## Progressive disclosure

The system prompt only receives a compact catalog: each entry's `name`, `description`, and `location`. The body of `KNOWLEDGE.md` and supporting files stay on disk until you explicitly read them. This keeps the prompt small while still making the memory discoverable.

The catalog is rescanned for you: setup/refresh and every full `context.rebuild` re-read `knowledge/` and recompose the section. Use `read` on the listed `location` when an entry becomes relevant.

## Call shape

Knowledge has one public call, and it just returns this manual:

```text
psyche(action="knowledge", input={}, reasoning="load knowledge guidance")
```

Every call carries exactly four root fields — `action`, `input`, `reasoning`
(all required) and the optional root `summarize`.

It takes a strict-empty `input`: there is no argument to pass, and any field you
put inside `input` is rejected before the action runs. This is a signpost — it
never creates, edits, searches, rescans, or loads entries.

Mutation and loading belong to the generic `file` family:

- author or fully rewrite an entry with
  `file(action="write", input={"file_path": "knowledge/<name>/KNOWLEDGE.md", "content": "..."}, reasoning="...")`;
- make a bounded exact change with
  `file(action="edit", input={"file_path": "knowledge/<name>/KNOWLEDGE.md", "old_string": "...", "new_string": "...", "replace_all": null}, reasoning="...")`;
- load a body on demand with
  `file(action="read", input={"file_path": "<location>", "offset": null,
  "limit": null, "max_chars": null}, reasoning="...")`.

Writing an entry does not hot-load the catalog. Apply it with one
`context(action="rebuild", input={}, reasoning="...")`, or let passive
refresh/molt reconstruction apply it.

### `summarize`

Loading this manual follows the **short-result** profile: root `summarize` is
available but normally unnecessary — leave it false, so exact procedure and
constraints are not summarized away. `summarize` is a root field; it is never
part of `input`.

### Settings

Knowledge owns no settings surface. Psyche's separate `settings/psyche.json`
does not configure Knowledge, and no `settings/psyche.knowledge.json` exists.
Nothing here reads settings.

## Nesting and sub-knowledge

A top-level entry may act as a **routing/index parent** with nested children that
hold the substance — exactly the nested-reference pattern documented in the
skills manual.

For the routing/index parent pattern — when to use it, how the parent stays a
short scannable index, how children carry the detail, relative child paths,
keeping the catalog in sync — **read the skills manual's nested reference
section** (`.library/intrinsic/capabilities/skills/SKILL.md`, "Nested
skill/reference pattern for umbrella manuals") and apply the same shape with
`KNOWLEDGE.md` in place of `SKILL.md`.

Compact example of a routing parent with sub-knowledge children — a project's
incident log:

```text
knowledge/project-x-incidents/
├── KNOWLEDGE.md                                   # routing/index ONLY
├── 2026-05-13-cache-stampede/KNOWLEDGE.md         # child — the detail
└── 2026-05-21-token-leak/KNOWLEDGE.md             # child
```

The parent `KNOWLEDGE.md` is a short index: one line per child with a hook and the
child's relative path; each child holds the full write-up. Keep names
filesystem-safe and descriptive, point at children by relative path, and use
nesting to group related entries, not to hide information.

## Cross-references

Knowledge entries may reference one another by relative path or by catalog name. Prefer links that remain valid if the whole agent directory moves:

```markdown
See also: ../architecture/KNOWLEDGE.md
See also: ../../people/reviewers/KNOWLEDGE.md
```

Knowledge may also reference skills when a reusable procedure exists:

```markdown
For the repeatable workflow, read `.library/intrinsic/capabilities/skills/SKILL.md`.
```

## When to create knowledge

Create or update a knowledge entry when the information is useful beyond the current turn but is not a portable procedure:

- project-specific decisions and rationale;
- collaborator preferences and review history;
- local repo paths, branch relationships, and known gotchas;
- incident notes and debugging evidence;
- conclusions from research that are specific to this agent's work.

## Cleanup / Footprint

Knowledge entries live under `knowledge/<name>/KNOWLEDGE.md` plus supporting
files. They are durable memory, not cache. Cleanup usually means consolidation,
renaming, or archiving stale entries after review; never delete knowledge just to
save space unless the user explicitly agrees after a dry-run report and the content is backed up or no
longer useful.

Footprint check: load the [shared inspection recipe](../../skills/manual/reference/cleanup-footprint-contract.md#shared-footprint-check-recipe)
through `skills-manual` → `reference/cleanup-footprint-contract.md`. Combine
its definitions with this tool-specific selection in one task-owned script;
the selection is not a standalone executable. Inspection writes nothing.
Appending `logs/cleanup.jsonl` is the separate, explicitly selected audit step
in that recipe; retain this manual's cleanup/approval rules below.

```python
agent = Path.cwd()  # the relevant agent directory, not a repository root
root = agent / "knowledge"
items = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
rows, total = footprint_check(items, tool="knowledge", top_n=30)
```

Recommended cadence: before molt if knowledge sprawl is confusing, after major
projects, and monthly for long-lived agents. If cleanup is approved with explicit user consent, record the
entries consolidated/removed in `logs/cleanup.jsonl` and apply the catalog change
with `context(action="rebuild", input={}, reasoning="refresh after cleanup")`.
