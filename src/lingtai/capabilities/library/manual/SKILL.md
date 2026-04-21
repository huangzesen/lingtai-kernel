---
name: library-manual
description: How your library works — the on-disk layout, the catalog, loading, authoring, publishing. Read this first.
version: 1.0.0
---

# The Library Capability

This is the library capability's own manual. It documents how the library works from your side: the on-disk layout, the XML catalog, and the authoring/publishing workflow. The library capability scans `.library/` plus any extra paths declared in `init.json`, builds the `<available_skills>` XML catalog, and injects it into your system prompt.

## On-disk layout

Your library lives at `<agent>/.library/`:

```
<agent>/.library/
├── intrinsic/
│   ├── capabilities/
│   │   └── <cap>/<manual files>
│   └── addons/
│       └── <addon>/<manual files>
└── custom/
```

- `intrinsic/` — **CLI-managed.** Wiped and rewritten from kernel-shipped manual bundles on every `system({"action": "refresh"})`. Do not edit — your edits will be erased. Read-only territory.
- `intrinsic/capabilities/<cap>/` — manual for each loaded capability (e.g. `library/`, `email/`, `psyche/`).
- `intrinsic/addons/<addon>/` — manual for each loaded addon (e.g. `imap/`, `telegram/`, `feishu/`).
- `custom/` — **your territory.** Authored skills live here. The CLI never touches this directory.

Additional paths come from `init.json` at `manifest.capabilities.library.paths` — typically `../.library_shared/` (the network-shared library) and `~/.lingtai-tui/utilities/` (operational utilities shipped by the TUI).

If the library capability is NOT loaded, the files still exist on disk — you just don't get an XML catalog in your prompt. You can still reach the manuals via `read`, `grep`, `ls`.

## How the catalog works

Every skill listed in `<available_skills>` in your system prompt is reachable right now. Each entry has `name`, `description`, and `location`. To read a skill's body, use `read` on the file at `<location>`. That gives you the full Markdown for that one turn.

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

Then call `system({"action": "refresh"})`. Do **not** overwrite an existing entry in `.library_shared/` — if the name collides, rename your skill or consult the admin agent.

## Admin curation of `.library_shared/`

If you are the admin agent, you may edit, consolidate, rename, or remove entries in `.library_shared/` using `edit`/`write`/`rm` as needed.

If you are not the admin agent, **do not modify** `.library_shared/` beyond adding new entries with `cp`. Editing or removing existing entries is admin's stewardship. This is a norm, not a mechanical lock.

## Adding a new library path

To expand your library with another source directory:

1. `edit` `init.json` under `manifest.capabilities.library.paths`. Append your new path (absolute or relative to your working dir; `~/` expansion honored).
2. Call `system({"action": "refresh"})`.

`init.json` is the ground truth. There is no runtime state — whatever is in `paths` at setup time is the exact set scanned.

## Name collision discipline

Two skills with the same `name` in the catalog would collide. Before authoring a new skill in `custom/` or publishing to shared, grep the existing catalog:

```
bash({"command": "grep -rh '^name:' .library/ ../.library_shared/ ~/.lingtai-tui/utilities/ 2>/dev/null"})
```

If you hit a collision: rename, or adapt the existing skill instead of forking a second one.

## Health check

Call `library({"action": "info"})` to verify your library is wired correctly. It returns this SKILL.md body plus a runtime snapshot: `library_dir`, `catalog_size`, resolved paths with exist/skill-count info, and any `problems` (invalid frontmatter, unreadable folders). If `status` is `"degraded"`, the error message tells you what needs fixing — typically a missing manual under `intrinsic/capabilities/library/`, which means the initializer didn't install manuals correctly.
