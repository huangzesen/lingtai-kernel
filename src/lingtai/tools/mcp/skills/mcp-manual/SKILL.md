---
name: mcp-manual
description: >
  Router for the read-only `mcp` capability. It distinguishes catalog,
  registered, and active servers; routes curated and third-party setup to their
  exact references; and documents the safe info/settings/manual preflight.
  Read before changing configuration. It does not teach the MCP protocol
  itself; use `lingtai-kernel-anatomy`'s MCP protocol reference.
version: 3.5.0
last_changed_at: 2026-08-29T00:00:00Z
related_files:
- src/lingtai/tools/skills/manual/reference/cleanup-footprint-contract.md
- src/lingtai/tools/mcp/__init__.py
- src/lingtai/tools/mcp/settings.py
- src/lingtai/tools/mcp/ANATOMY.md
- src/lingtai/tools/mcp/CONTRACT.md
- src/lingtai/tools/mcp/skills/mcp-manual/reference/curated-addons.md
- src/lingtai/tools/mcp/skills/mcp-manual/reference/third-party-and-legacy.md
- src/lingtai/tools/mcp/skills/mcp-manual/reference/troubleshooting.md
- src/lingtai/tools/mcp/skills/mcp-manual/reference/runtime-and-identity.md
- src/lingtai/tools/mcp/skills/mcp-manual/scripts/find_readme.py
- tests/test_mcp_settings.py
maintenance: |
  Tracks the routed source/resources it summarizes; update when the underlying capability or its sub-references change.
---

# MCP capability — router

`mcp` is a read-only presentation capability. It renders the per-agent
`mcp_registry.jsonl` into the protected `<registered_mcp>` prompt section and
reports registry health; it never registers, activates, configures, or
troubleshoots a server. Configuration and registry mutations belong to
explicitly authorized `write`/`edit` calls, not this tool.

## Mandatory preflight

Before registering, updating, deregistering, or troubleshooting a server:

1. Read the route below and the relevant provider/server README. Never guess
   install commands, environment variables, or config fields.
2. Call `mcp(action="info", input={}, reasoning="check MCP registry health")`.
   Treat its `registry_path`, `registered`, and `problems` as the current health
   snapshot.
3. Obtain explicit human authorization before editing. After the edit, call
   `system(action="refresh")`, then call `info` again.

`/addon` is retired. `/mcp` is the only current TUI command for this surface;
it is read-only status/config inspection, not a setup wizard. Do not direct a
human there for addon configuration.

## Three states

Keep these states separate for this agent:

1. **Catalog** — a reference entry shipped by the kernel. Curated names are
   `imap`, `telegram`, `feishu`, `wechat`, `whatsapp`, and `cloud_mail`.
2. **Registered** — a valid record in `mcp_registry.jsonl` (beside `init.json`),
   listed in `<registered_mcp>`.
3. **Active** — a server process is running and its tools are mounted. A record
   or successful `info` call alone is not proof of activity.

The usual promotion is **catalog → registry → active**. File edits plus one
controlled `system(action="refresh")` advance the relevant layer; refresh does
not restart a healthy child.

## Route table

| Need | Read first |
|---|---|
| Set up `imap`, `telegram`, `feishu`, `wechat`, `whatsapp`, or `cloud_mail` | [`reference/curated-addons.md`](reference/curated-addons.md), then exact provider docs |
| Add a third-party `npx`/`uvx`/HTTP server | [`reference/third-party-and-legacy.md`](reference/third-party-and-legacy.md), then its README |
| Use legacy `mcp/servers.json` | [`reference/third-party-and-legacy.md`](reference/third-party-and-legacy.md) |
| Update, deregister, or diagnose failures | [`reference/troubleshooting.md`](reference/troubleshooting.md) |
| Check identity, manual paths, or runtime/venv provenance | [`reference/runtime-and-identity.md`](reference/runtime-and-identity.md) |
| Ask about protocol, env injection, or LICC | `lingtai-kernel-anatomy` → `reference/mcp-protocol.md` |
| Inspect footprint before approved cleanup | `skills-manual` → `reference/cleanup-footprint-contract.md` |

For curated addon setup, read `reference/curated-addons.md` (the exact
curated-addons contract), then the provider docs before editing; never infer
addon fields from memory.

### README gate

A server README is authoritative for installation, config fields, env vars, and
errors. For an installed Python distribution, prefer the bundled script with
the runtime venv's Python:

```bash
<runtime-venv-python> \
  .library/intrinsic/capabilities/mcp/scripts/find_readme.py <distribution-or-module>
```

The script tries an editable source README, then wheel `METADATA`; pass
`--module <importable-module>` when needed. If no local README exists, use the
registered `<homepage>` with `web_read`; runtime self-description is last resort.

## Tool surface

All actions use the strict-empty LTP v2 envelope; `summarize` is root-only
presentation metadata:

```text
mcp(action="info", input={}, reasoning="inspect registry")
mcp(action="settings", input={}, reasoning="show MCP settings")
mcp(action="manual", input={}, reasoning="read the MCP router")
```

- `info` re-reads registry/identities, re-renders the protected prompt section,
  and returns health without the manual body.
- `settings` is a bounded SHOW of exactly `init.addons` and `init.mcp`.
- `manual` returns this body as `mcp_manual` plus the installed `manual_path`,
  with no registry I/O or mutation.

Leave `summarize=false` for `settings` and `manual`, and for `info` whenever
exact names, IDs, problems, or paths matter. Summarization is only a
presentation choice; errors are never summarized.

## Identity and public paths

`info` attaches `identity` only for a matching registry record with a non-empty
addon-published `accounts` list. The cached source is
`system/mcp_identities/<name>.json` (`lingtai.mcp.identity.v1`), not a network
call. Only allowlisted non-secret account alias/provider name or ID/display
name and routing counts are exposed; tokens, passwords, app/refresh/access
secrets, headers, and unknown fields are dropped. Prompt XML narrows the
projection again and excludes volatile verification timestamps. Use the addon's
`accounts` action for richer detail. See
[`reference/runtime-and-identity.md`](reference/runtime-and-identity.md).

`manual_path` is the truthful host-local installed-skill path, not a config path
or proof that a server is active. A missing skill returns a degraded result
with an empty body, the path, and an error; it never falls back to another
manual. Do not copy private diagnostic paths into public setup instructions.

## Configuration settings

`mcp(action="settings", input={}, reasoning="show MCP settings")` is SHOW-only.
Success is exactly `{"settings": [...]}`; each row has exactly `key`,
`current`, `default`, `configurable`, and `comment`, in that order. The whole
inventory is bounded to 65,536 UTF-8 bytes. Any source/read/serialization
failure returns one fixed no-row failure, never a partial inventory or exception
text. There is no set/reset form and this action writes nothing.

| Key | Meaning | Default / projection | Authorized change |
|---|---|---|---|
| `init.addons` | Fresh canonical effective addon list used by boot/refresh, not the decompressed registry or live health. | `[]`, shown as configured | Edit top-level `init.json` `addons`, refresh, then SHOW again. Removing a name does not delete its registry row. |
| `init.mcp` | Fresh canonical effective activation object. | Logically `{}`, but both `current` and `default` are `<redacted>`; no names, commands, paths, env, or credentials. | Edit top-level `init.json` `mcp`, refresh, then use `info` and the addon's action. A healthy child needs a full relaunch for a changed launch spec. |

These rows exclude the registry, identity files, legacy `mcp/servers.json`,
curated private config/session data, Task Cards, agent identity, and live
process state. Registry membership still gates top-level `init.mcp` activation.

## Cleanup / Footprint

MCP leaves `mcp_registry.jsonl`, optional `mcp/servers.json`, and
`.mcp_inbox/<name>/...` event files. Curated addons may own additional stores;
read their manual before including them. Never delete credentials, message or
audit records, active registry/process state, or recovery evidence blindly.

Load the [shared inspection recipe](../../../skills/manual/reference/cleanup-footprint-contract.md#shared-footprint-check-recipe)
and combine it with this MCP-owned selection; the default check writes and
deletes nothing:

```python
agent = Path.cwd()  # relevant agent directory, not a repository root
items = [p for p in (agent / "mcp_registry.jsonl", agent / "mcp", agent / ".mcp_inbox") if p.exists()]
rows, total = footprint_check(items, tool="mcp", top_n=None)
```

Run the check after adding/removing servers, when `.mcp_inbox` grows, and before
sharing a project. Show the dry-run report and obtain explicit user consent
before any archive or deletion; without consent, stop. Prefer an authorized
registry/activation edit followed by `system(action="refresh")` over deleting
state. Any separately selected audit/apply step records timestamp, tool, mode,
candidate count/bytes/path summary, and approval in `logs/cleanup.jsonl`; do not
call that audit write read-only.

Runtime/venv swap and child provenance are in
[`reference/runtime-and-identity.md`](reference/runtime-and-identity.md). Curated
and third-party schemas are in [`reference/curated-addons.md`](reference/curated-addons.md)
and [`reference/third-party-and-legacy.md`](reference/third-party-and-legacy.md);
update/failure recovery is in [`reference/troubleshooting.md`](reference/troubleshooting.md).
