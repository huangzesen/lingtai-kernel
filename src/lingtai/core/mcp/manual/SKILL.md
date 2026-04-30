---
name: mcp-manual
description: >
  Operational guide for the `mcp` capability — how to register, activate,
  update, deregister, and troubleshoot MCP (Model Context Protocol) servers
  in your agent. The how-to companion to the canonical spec in
  `lingtai-anatomy reference/mcp-protocol.md`.

  Reach for this manual when:
    - The human asks to install or remove an MCP server. The decision tree
      starts with "is it in the kernel catalog?" — if yes, the curated
      one-liner workflow (add to `addons:`, add `mcp.<name>` activation,
      refresh) is here; if no, the third-party path (fetch homepage README,
      append registry record, activate, refresh) is here too.
    - You want to know what MCPs you currently have. `mcp(action="show")`
      returns the registry plus health; this manual explains what the
      output means.
    - An MCP isn't behaving — the troubleshooting flow (registry validation,
      `problems` list, refresh-after-edit verification) lives here.
    - You're exploring an unfamiliar MCP and want to know what it can do —
      the manual tells you to fetch the `<homepage>` README first
      (canonical install + config + tool surface) before guessing from
      tool descriptions.

  Covers: the three states (catalog → registry → active), the
  curated-vs-third-party install paths, where the registry file
  (`mcp_registry.jsonl`) lives and how to mutate it (write/edit/bash —
  the `mcp` capability is read-only), the `<homepage>` field as primary
  documentation, and the relationship between `init.json`'s `addons:`
  list, `mcp:` activation entries, and the registry.

  Does NOT cover the protocol spec itself: schema validation rules, env
  injection mechanics (the `LINGTAI_AGENT_DIR` / `LINGTAI_MCP_NAME`
  variables), the LICC v1 inbox callback contract, and the validator's
  internal logic all live in `lingtai-anatomy reference/mcp-protocol.md`.
  Read this for *what to do*; read anatomy for *how it works*.
version: 2.0.0
---

# MCP Capability — How To Use It

The `mcp` capability is your interface to the Model Context Protocol (MCP) servers available to this agent. Like the `library` capability, it is **pure presentation**: the registered MCPs are listed in your system prompt under `<registered_mcp>`, and the registry itself is a JSONL file you edit directly with `write` / `edit` / `bash`.

**This manual is a how-to.** The canonical specification — schema, validator behaviour, env injection, LICC contract — lives in the `lingtai-anatomy` skill, in `reference/mcp-protocol.md`. Load that when you need the spec; load this when you want to *do* something.

## Three states of an MCP

For any MCP server, relative to this agent:

1. **In the kernel catalog** — LingTai blesses it. Reference template ships with the kernel. Examples: `imap`, `telegram`, `feishu`, `wechat`.
2. **Officially registered** — appears as a line in `mcp_registry.jsonl` (sibling to `init.json`). The system prompt's `<registered_mcp>` lists it.
3. **Active** — the MCP server subprocess is running, its tools are mounted in your tool surface.

Promotion path: catalog → registry → active. You move things along by editing files and calling `system(action="refresh")`. See `lingtai-anatomy reference/mcp-protocol.md` §1 for the full three-layer model.

## Where to look first when exploring an MCP

Each registered MCP exposes a `<homepage>` field (when known) — typically a GitHub repository URL whose README is the canonical install + config + troubleshooting doc. **As your first step when exploring or installing an MCP, fetch the homepage README** with `web_read` or `bash` + `curl`, unless you have explicit guidance saying otherwise. This README is owned by the MCP author and is always more up-to-date than anything kernel-side.

If a registered MCP has no `<homepage>`, fall back to the MCP's own runtime self-description: once activated, the MCP server provides its tool descriptions, and many servers also publish a server-level `instructions` string at connection time.

## Workflows

### Adding a curated MCP (imap / telegram / feishu / wechat)

Simplest path:

1. Add its name to `addons:` in your `init.json` (e.g., `"addons": ["imap"]`).
2. Add an `init.json` `mcp.<name>` activation entry — see the MCP's README for the exact subprocess spec (typically `python -m lingtai_<name>` plus a `LINGTAI_<NAME>_CONFIG` env var pointing at a config JSON).
3. Run `system(action="refresh")`.

The `mcp` capability decompresses the catalog record into `mcp_registry.jsonl` automatically, the loader spawns the subprocess, and the omnibus tool (`imap`, `telegram`, etc.) appears in your tool surface.

### Adding a third-party / custom MCP

1. Fetch the MCP's homepage README (or other setup doc) to learn the install command, env vars, and config schema.
2. Append a single JSON record to `mcp_registry.jsonl` (one line, atomic write). For the schema, see `lingtai-anatomy reference/file-formats.md` §6.5.
3. Add an `init.json` `mcp.<name>` activation entry.
4. Run `system(action="refresh")`.

### Updating, deregistering, or troubleshooting

- **Update**: edit the matching line in `mcp_registry.jsonl` in place. Same schema. `system(action="refresh")`.
- **Deregister**: remove the matching line. Note: this does NOT stop a running MCP — to deactivate, also remove the entry from `init.json`'s `mcp` section.
- **Troubleshoot**: call `mcp(action="show")` to see the current registry, problems list, and runtime health snapshot. Invalid registry lines are skipped silently with a warning at refresh time, so always verify with `show` after editing.

## Tool surface

One action: `mcp(action="show")`. Returns this manual body, the current registry contents, and a runtime health snapshot (registry path, count, problems).

All registry mutations happen via `write` / `edit` / `bash`. The `mcp` capability never writes to the registry.

## See also

- **Canonical spec**: `lingtai-anatomy reference/mcp-protocol.md` — full three-layer model, env injection, validator schema, **LICC v1** inbox callback contract, reference implementations.
- **File formats**: `lingtai-anatomy reference/file-formats.md` §2.7 (init.json `addons` + `mcp` fields), §6 (`mcp/servers.json` legacy direct mounts), §6.5 (`mcp_registry.jsonl`), §6.6 (`.mcp_inbox/<name>/<id>.json` LICC events).
- **Per-MCP setup docs**: each MCP's homepage README. Fetch first when exploring or installing.
