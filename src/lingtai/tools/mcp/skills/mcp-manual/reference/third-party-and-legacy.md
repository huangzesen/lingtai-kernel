---
related_files:
- src/lingtai/tools/mcp/skills/mcp-manual/SKILL.md
maintenance: |
  Third-party/legacy MCP wiring reference routed to from mcp/skills/mcp-manual/SKILL.md; update it whenever the npx/uvx/HTTP server wiring path or the legacy mcp/servers.json mechanism changes.
---

# Third-party and legacy MCP routes

Two routes for non-curated MCPs: the **registry route** (recommended, gated by `mcp_registry.jsonl`) and the **legacy `mcp/servers.json` route** (ungated, kept for quick experiments).

## Registry route (recommended)

For any non-curated MCP — typically `npx`/`uvx`-launched servers from the broader MCP ecosystem.

1. **Fetch the MCP's setup doc.** If it's pip-installed, use the bundled
   `find_readme.py` script; otherwise (npx/uvx servers) `web_read` the homepage
   URL. Both routes use the top-level [README gate](../SKILL.md#readme-gate). Either way, get
   the install command, env vars, and config schema before writing any config.
2. Append a single JSON record to `mcp_registry.jsonl` (one line, atomic write). For the schema, see `lingtai-kernel-anatomy reference/file-formats.md` §6.5.
3. Add an `init.json` `mcp.<name>` activation entry.
4. Run `system(action="refresh")`.

Benefits: gives you the `<homepage>` field used by the top-level [README gate](../SKILL.md#readme-gate) as its fallback URL, allow-listing, and registry health diagnostics via `mcp(action="info", input={}, reasoning="check registry health")`.

## Legacy `mcp/servers.json` route

A second route still exists: `<working_dir>/mcp/servers.json`. The kernel loads it on startup with no registry validation — useful for quick experiments or for personal MCPs you don't want to register globally. Same JSON shape as the registry route, but mounted directly without the catalog → registry → active promotion.

```json
{
  "vision": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@z_ai/mcp-server"],
    "env": {
      "Z_AI_API_KEY": "your-key",
      "Z_AI_MODE": "ZAI"
    }
  },
  "web-search": {
    "type": "http",
    "url": "https://api.z.ai/api/mcp/web_search_prime/mcp",
    "headers": {
      "Authorization": "Bearer your-key"
    }
  }
}
```

MiniMax's own `web_search` tool wires the same way, as a stdio server:

```json
{
  "minimax-web-search": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "minimax-coding-plan-mcp"],
    "env": {
      "MINIMAX_API_KEY": "your-key"
    }
  }
}
```

Use the registry route for anything you want to keep. Use `mcp/servers.json` when you just want to wire up a single server quickly.

MiniMax and Zhipu's own search results are no longer available through the
built-in `web` capability's `search` action; wire either MCP server through
one of the two routes above and call its `web_search`/`web_search_prime` tool
directly.

## Server config fields (both routes)

| Field      | stdio                       | http                              |
|------------|-----------------------------|-----------------------------------|
| `type`     | `"stdio"` (default)         | `"http"` (required)               |
| `command`  | executable (e.g. `npx`)     | —                                 |
| `args`     | command-line arguments      | —                                 |
| `env`      | env vars for the subprocess | —                                 |
| `url`      | —                           | streamable-http endpoint          |
| `headers`  | —                           | HTTP headers (typically auth)     |

## API keys and secrets

Plaintext credentials in `mcp_registry.jsonl` or `mcp/servers.json` are the simplest path. For sensitive keys:

- **stdio servers**: env vars in the `env` field referencing values from `.env` (the agent's `env_file`). Some servers, including curated integrations such as `imap`, require literal credentials in a separate config file pointed at by an env var — see the relevant setup docs before writing secrets.
- **http servers**: keys go in the `headers` field (typically `Authorization: Bearer ...`).
- Never commit `mcp/servers.json` or addon config files to version control if they contain secrets.
