---
name: skill-for-skill
description: How to use your library — find, read, load, author, and publish skills. Read this first.
version: 1.0.0
---

# Your Library

Every skill listed in `<available_skills>` in your system prompt is reachable right now. Each entry has `name`, `description`, and `location`. The library capability scans the following sources and injects the catalog:

- `<agent>/.library/intrinsic/` — kernel-shipped skills (including this one). Rewritten on every setup; do not edit.
- `<agent>/.library/custom/` — your own authored skills. This is your territory.
- Every path in `init.json` `manifest.capabilities.library.paths` — typically `../.library_shared/` (the network-shared library) and `~/.lingtai-tui/utilities/` (operational utilities shipped by the TUI).

## How the catalog works

The XML catalog in your prompt lists every skill. To read a skill's body, use `read` on the file at `<location>`. That gives you the full Markdown for that one turn.

## Loading a skill into active working memory

If you plan to use a skill across many turns or need it to survive a molt, pin its `SKILL.md` into your pad:

```
psyche({"object": "pad", "action": "append", "files": ["<location>"]})
```

The body is appended to your pad's read-only reference section, which is part of the cached system-prompt prefix. To unpin, call the same action with a new `files` list that omits the path (or `files: []` to clear everything).

Pinning is cheap per-token over a session because the pad sits in the cached prefix — repeated `read`s of the same file do NOT benefit from that cache.

## Authoring a new skill

Create a folder under `<agent>/.library/custom/<skill-name>/` with a `SKILL.md` starting with YAML frontmatter:

```
---
name: <skill-name>
description: One-line description of what this skill does
version: 1.0.0
---

Full instructions in Markdown below...
```

Required frontmatter: `name`, `description`. Optional: `version`, `author`, `tags`.

After writing, call `system({"action": "refresh"})` so the library capability rescans and re-injects the catalog.

## Publishing to the network-shared library

If a custom skill is worth sharing with every agent in the network:

```
bash({"command": "cp -r .library/custom/<name> ../.library_shared/<name>"})
```

Then `system.refresh`. Do **not** overwrite an existing entry in `.library_shared/` — if the name collides, rename your skill or consult the admin agent.

## Admin curation of `.library_shared/`

If you are the admin agent, you may edit, consolidate, rename, or remove entries in `.library_shared/` using `edit`/`write`/`rm` as needed.

If you are not the admin agent, **do not modify** `.library_shared/` beyond adding new entries with `cp`. Editing or removing existing entries is admin's stewardship. This is a norm, not a mechanical lock.

## Adding a new library path

To expand your library with another source directory:

1. `edit` `init.json` under `manifest.capabilities.library.paths`. Append your new path (absolute or relative to your working dir; `~/` expansion honored).
2. `system.refresh`.

`init.json` is the ground truth. There is no runtime state — whatever is in `paths` at setup time is the exact set scanned.

## Name collision discipline

Two skills with the same `name` in the catalog would collide. Before authoring a new skill in `custom/` or publishing to shared, grep the existing catalog:

```
bash({"command": "grep -rh '^name:' .library/ ../.library_shared/ ~/.lingtai-tui/utilities/"})
```

If you hit a collision: rename, or adapt the existing skill instead of forking a second one.

## Health check

Call `library({"action": "info"})` to verify your library is wired correctly. It returns this SKILL.md body plus a runtime snapshot: `catalog_size`, resolved paths with exist/skill-count info, and any `problems` (invalid frontmatter, unreadable folders). If `status` is `"degraded"`, the error message tells you what needs fixing.
