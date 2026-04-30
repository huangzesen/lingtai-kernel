---
name: lingtai-anatomy
description: >
  Canonical specification of how LingTai is built — the under-the-hood
  protocols, file formats, and runtime mechanics that tools and capabilities
  rest on. Reach for this when the tool description and tool manual aren't
  enough: when you need the exact JSON schema of a file you're about to
  write, the precise contract a subsystem follows, or the reason something
  behaves the way it does.

  This skill is an index file pointing to 10 topical reference documents.
  Load the index first to scan the topic table, then load only the
  reference(s) you need — the bundle is large (~3000 lines total) and
  unloading the whole thing burns context. References are spec-grade with
  line numbers into kernel source (`lingtai_kernel/base_agent.py`,
  `lingtai_kernel/intrinsics/eigen.py`, etc.) so you can verify any claim
  against the actual code.

  The 10 references:
    file-formats.md       — JSON schemas for .agent.json, init.json,
                            .status.json, mailbox messages, mcp_registry.jsonl,
                            LICC inbox events, signals
    filesystem-layout.md  — directory trees inside .lingtai/, per-agent
                            layout, where logs/secrets/registry live
    mail-protocol.md      — atomic mail delivery, self-send, wake-on-mail,
                            peer/abs modes, pseudo-agent outboxes
    mcp-protocol.md       — MCP capability + LICC v1 spec (LingTai Inbox
                            Callback Contract — the filesystem protocol
                            that lets out-of-process MCP subprocesses push
                            events back into the agent's inbox), catalog →
                            registry → activation chain, env injection,
                            reference implementations
    memory-system.md      — six-layer durability hierarchy (chat history →
                            soul → codex → library → pad → kernel-managed),
                            psyche dispatch, daemon system
    molt-protocol.md      — context-reset ritual, 70%/95% warning ladder,
                            four-store ritual, what survives molt
    network-topology.md   — avatar spawn mechanics, three-edge model,
                            contacts, rules propagation, network discovery
    runtime-loop.md       — turn cycle, AED recovery, tool dispatch,
                            five-state lifecycle, signal consumption,
                            heartbeat
    glossary.md           — full bilingual map of 文言 (literary Chinese)
                            terms used in covenants and procedures →
                            English technical names → kernel layer
    changelog.md          — chronicle of breaking changes, renames, and
                            migrations newest-first; check here FIRST when
                            an old name doesn't match your current tools or
                            an error message references behaviour you
                            don't recognize

  Use this skill when:
    - You hit an error that mentions a file path, schema field, or behaviour
      you don't recognize — chances are the changelog has the rename or
      migration that explains it.
    - You're writing code that produces or consumes a LingTai file
      (any .json, .jsonl, signal, or mailbox artifact) — file-formats.md
      and the relevant protocol doc carry the spec.
    - You're debugging a runtime issue (mail not arriving, agent stuck,
      molt mid-task, avatar not spawning) — the protocol docs tell you
      what *should* have happened so you can compare to what did.
    - You're building a third-party MCP — mcp-protocol.md is the canonical
      contract; LICC v1 lets your MCP push events back into the host
      agent's inbox.
    - Someone asks "how does X work in LingTai" and you want to answer
      from spec, not guess.

  Relationship to per-tool manuals: tool manuals (e.g. `daemon-manual`,
  `mcp-manual`, `library-manual`) are how-to guides — operational steps,
  worked examples, common pitfalls. lingtai-anatomy references are the
  canonical specs underneath. A manual says "to register an MCP, do this";
  the anatomy spec says "the registry file format is exactly this; the
  validator enforces these constraints; here is the line in agent.py that
  reads it." Read the manual to *do*, the anatomy to *understand* or
  *verify*.

  Cross-references between anatomy files are common — mcp-protocol.md
  cites file-formats.md §6.5 for the registry schema, runtime-loop.md
  cites molt-protocol.md for warning thresholds. If your question crosses
  a boundary, you may need two references at once.
version: 2.1.0
---

# LingTai Anatomy

Canonical architecture documentation. Three-layer system, ten topical references. Read what you need; skip the rest.

## Architecture at a Glance

```
lingtai_kernel (pip package)
  └── BaseAgent (~1.7K lines) — turn loop, five-state machine, molt, soul, signals
lingtai (wrapper)
  └── Agent (~860 lines) — capabilities, MCP catalog/registry/loader, LICC, refresh, CPR
User customization
  └── init.json + system/ files — model, prompts, capabilities, addons (curated MCPs)
First-party MCP repos (separate)
  └── lingtai-imap / lingtai-telegram / lingtai-feishu / lingtai-wechat
      — addon protocol implementations, run as MCP subprocesses
```

Source code:
- `lingtai-kernel/src/lingtai_kernel/base_agent.py`
- `lingtai/src/lingtai/agent.py`
- `lingtai/src/lingtai/network.py`
- `lingtai/src/lingtai/core/mcp/` (capability + LICC inbox poller + catalog)

## Quick Reference: Where to Look

| "I want to understand…" | Read this reference | Key content |
|---|---|---|
| How memory persists across molts | `reference/memory-system.md` | 6-layer durability hierarchy, psyche tool dispatch, daemon system |
| What files live where on disk | `reference/filesystem-layout.md` | Directory trees, orchestrator identification, boot chain |
| Exact JSON schemas of key files | `reference/file-formats.md` | .agent.json, init.json, .status.json, mailbox, MCP registry / inbox, signals |
| How each turn runs | `reference/runtime-loop.md` | Turn cycle, five-state machine, signal lifecycle, soul flow |
| How molting works | `reference/molt-protocol.md` | Triggers, warning ladder (70%/95%), four-store ritual, refresh |
| How mail gets delivered | `reference/mail-protocol.md` | Atomic delivery, advanced features, self-send, wake-on-mail |
| How the avatar tree works | `reference/network-topology.md` | Spawn mechanics, three-edge model, contacts, rules propagation |
| **How MCP / LICC works** (or how to write a third-party MCP) | `reference/mcp-protocol.md` | Catalog → registry → activation, env injection, LICC v1 spec, reference impls |
| What changed and when (breaking changes, renames, migrations) | `reference/changelog.md` | Living chronicle, newest-first; load when an old name doesn't match current tools |
| What a 文言 term means in English | `reference/glossary.md` | Full bilingual term map (kernel layer + wrapper tool name) |

## Version History

- **v2.1.0** (2026-04-29): Added `mcp-protocol.md` (canonical MCP capability + LICC v1 spec). Absorbed standalone `lingtai-changelog` skill into `reference/changelog.md`. Stale references updated: `file-formats.md` §2.7 / §6 rewritten + new §6.5 (registry) + §6.6 (LICC events); `filesystem-layout.md` drops legacy `.lingtai/.addons/`, adds per-agent `mcp_registry.jsonl` + `.mcp_inbox/`; `molt-protocol.md` MCP persistence row updated.
- **v2.0.0** (2026-04): Modular rewrite. 8 independent references replace the monolithic 474-line file. 4 errata corrected, 7 missing topics covered.
- **v1.2.0**: Original monolithic SKILL.md (474 lines / 31KB / ~8K tokens).
