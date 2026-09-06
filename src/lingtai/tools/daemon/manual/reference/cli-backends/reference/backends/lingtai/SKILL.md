---
name: daemon-backend-lingtai
description: >
  Nested daemon-cli-backends reference for the built-in `lingtai` daemon
  backend (the in-process ChatSession default). Read this when routing a
  daemon task to the built-in backend: it has no external CLI and no
  `backend_options` flag surface; this page routes you to the live
  authorities for preset selection/inspection, lingtai/tools/skills/MCP inheritance,
  and the daemon completion contract. It is not a rules catalog.
version: 0.1.0
last_changed_at: 2026-07-19T00:00:00Z
related_files:
- src/lingtai/tools/daemon/manual/reference/cli-backends/SKILL.md
- src/lingtai/tools/daemon/manual/SKILL.md
- src/lingtai/intrinsic_skills/system-manual/reference/substrate-manual/SKILL.md
maintenance: |
  Tracks the built-in lingtai daemon backend topic it documents; update when that integration changes.
---

# LingTai Daemon Backend — Knowledge Entrypoint

`backend="lingtai"` is the built-in default — an in-process ChatSession run
loop, not a wrapped external CLI. There is no installed binary whose help
output could be consulted, and `backend_options` is ignored (there is no CLI
process to forward argv to), so this backend has no flag surface to discover
or translate. Its behavior is owned by live LingTai configuration, manuals,
and source — route to the current authority instead of memorizing snapshots.

## Where the knowledge lives

1. **Task shape and behavior contract** — the `daemon-manual` router
   ([`manual/SKILL.md`](../../../../../SKILL.md)): `task` vs `prompt` vs
   `tools` vs `skills` vs `mcp` semantics, and the shared parent
   [`reference/cli-backends/SKILL.md`](../../../SKILL.md) ("LingTai backend
   tool surface") for how this backend curates tools.
2. **Preset selection and inspection** — run `system(action="presets")` for
   the live tier/connectivity/capability listing (guidance:
   `system-manual` → `reference/substrate-manual/SKILL.md`). A per-task
   `preset` must be a `.json`/`.jsonc` path exactly as returned by that
   listing; an unloadable or unreachable preset refuses the whole batch at
   emanate time. Omit `preset` to inherit the parent's regular (non-MCP)
   tool surface.
3. **Tools/skills/MCP inheritance** — parent MCP tools are **not**
   auto-inherited: pass full one-run `mcp` registrations per task. `skills`
   entries become a compact prompt catalog (paths, not pasted bodies).
   `email` is daemon-eligible but opt-in via `tools`. Details live in the
   parent router's "LingTai backend tool surface" section.
4. **Completion contract** — the built-in `daemon_common` MCP is added
   automatically and `finish(status="done")` is the only terminal-success
   signal. The maintainer-facing architecture invariants are
   `src/lingtai/tools/daemon/CONTRACT.md`; the current tool schema
   description is the caller-facing authority.

## `context_token_limit`

`context_token_limit` is a positive provider-compaction threshold, not a context
or window setting. It is honored only for native LingTai LLM providers
`Codex` and `mimo`; other native providers and every external CLI backend ignore
it. An
explicit preset resolves the comparison window from canonical
`manifest.llm.context_limit`; without a preset, use the inherited parent
effective window. Codex Responses uses `context_management` with
stateless/full-history replay; compaction failure is non-fatal. Native mimo
compaction failure is a hard failure. The external `mimo`/`mimocode` CLI alias
is unrelated.

## Example: explicit preset, tools, skills, and MCP

```jsonc
{
  "action": "emanate",
  "backend": "lingtai",
  "tasks": [{
    "task": "Summarize reports/audit.md into reports/audit-summary.md.",
    "tools": ["file"],
    "preset": "~/.lingtai-tui/presets/saved/cheap.json",
    "skills": ["src/lingtai/tools/daemon/manual"],
    "mcp": [{"name": "local-docs", "transport": "stdio",
             "command": "python", "args": ["-m", "local_docs_mcp"]}]
  }]
}
```

Every field above resolves live: the preset path comes from
`system(action="presets")`, skill paths resolve against the parent working
directory, and the MCP registration is started as a task-scoped client whose
tools appear only for this run (secret `env`/`headers` values are redacted
in prompts).

## Harness boundary

There is nothing to tune at the process-spawn layer: no reserved flags, no
argv, no `backend_argv`/`backend_harness_argv` in `daemon.json`. Model and
tool shape are chosen through `preset`; behavior is guided through `task`;
capability comes from `tools`/`skills`/`mcp`. LingTai-backend tool calls still
pass the kernel ToolExecutor/ToolCallGuard gate — a daemon run does not bypass
normal execution policy.
