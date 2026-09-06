---
related_files:
- src/lingtai/tools/mcp/skills/mcp-manual/SKILL.md
- src/lingtai/tools/mcp/__init__.py
- src/lingtai/services/mcp_registry.py
- src/lingtai/tools/mcp/CONTRACT.md
- tests/test_mcp_identity_discovery.py
- tests/test_tool_family_mcp_migration_parity.py
maintenance: |
  Owns the detailed identity, installed-manual path, and runtime/venv provenance gates routed from mcp-manual; update it when identity projection, curated launcher ownership, or manual-path behavior changes.
---

# MCP identity and runtime provenance

This reference owns details intentionally kept out of the model-facing schema
and the short `mcp-manual` router. It is diagnostic guidance, not an MCP
registration or server-management API.

## Identity projection

`mcp(action="info", input={}, reasoning="inspect MCP identity")` reads the
addon-published, non-secret document
`system/mcp_identities/<name>.json`. The document uses schema
`lingtai.mcp.identity.v1`; it is a cached filesystem record and does not make a
network request.

An `identity` block is attached only to a matching `mcp_registry.jsonl` record
when the identity has a non-empty `accounts` list. The `info` projection
allowlists the account alias, provider username/ID/display name, bot marker,
and non-secret routing counts. It drops tokens, passwords, app secrets,
refresh/access tokens, headers, and unknown fields. The prompt's
`<registered_mcp>` XML narrows the projection again and excludes volatile
`last_verified_at` so prompt content remains cache-stable. A missing, unrelated,
or empty identity record produces no `identity` member. For richer account
information, use the addon's own `accounts` action; never read private config as
a shortcut.

## Manual and public-path gate

The reserved `manual` action reads exactly the installed
`.library/intrinsic/capabilities/mcp/SKILL.md` below the granted agent workdir.
It performs no registry scan, identity read, rescan, or mutation. Its public
result is the existing flat shape:

```json
{"status":"ok","mcp_manual":"<full body>","manual_path":"<host-local path>"}
```

A missing skill returns `status: "degraded"`, an empty `mcp_manual`, the
truthful host-local `manual_path`, and an error; it never falls back to another
manual. `manual_path` and `registry_path` are diagnostic host-local values, not
configuration inputs or proof of an active child. Do not copy private absolute
paths into public docs, and do not infer a server's activation from either path.

## Curated versus non-curated child ownership

A curated addon activated through `init.json`'s `mcp.<name>` entry is owned by
the running kernel's own `mcp_catalog.json`. That catalog derives the stdio
type, the running Agent's `sys.executable`, the module args, and the child's
entire `PYTHONPATH` from the Agent's current imported source root. The curated
entry's non-launch `env` (for example `LINGTAI_IMAP_CONFIG`) passes through;
legacy `type`/`command`/`args`/`env.PYTHONPATH` fields are accepted for
read-compatibility but ignored for launch with one bounded warning. If the
catalog has no safe stdio launcher, do not fall back to those legacy fields.

A non-curated third-party `init.json` entry and the legacy
`mcp/servers.json` route own their own `type`/`command`/`args`/`env` activation
spec. Daemon task/plugin MCPs are a separate route owned by that task or
plugin's configuration; they never spawn from `mcp_registry.jsonl` or the
main-agent `init.json` entry. HTTP entries have no local interpreter. Never
assume that a child shares the main Agent's interpreter, site-packages, source
root, or environment unless the effective provenance is checked.

## Authorized venv swap and registry truth

A venv swap never rewrites the registry automatically. After an authorized
swap, an operator may explicitly reconcile canonical curated records whose
`source` is `lingtai-curated` and whose stored `command` differs from the new
interpreter. That is an atomic, order-preserving rewrite of existing valid
records only; it is not addon activation and it never appends duplicates. Use
ordinary authorized `write`/`edit` operations and stop before writing if
`read_registry()` reports invalid or duplicate lines. Do not apply this to an
intentional command override.

The registry rewrite is only durable registry truth. It is not the spawn source
for non-curated, legacy, daemon-launched, or plugin-launched children. For a
curated main-agent child, the running kernel still derives its effective launch
from its own catalog, so a full Agent relaunch (not `refresh`) is required for a
healthy child to load a new venv. Refresh retries failed children and does not
restart healthy ones.

## Fail-closed provenance check

After a venv or source change, run one one-shot probe using the child's
**effective** command and environment. It must print and compare:

- `sys.executable`;
- `lingtai.__file__`; and
- `lingtai.mcp_servers.<name>.__file__`.

For a curated main-agent entry, effective means the Agent's own interpreter,
its catalog-derived module args, and its own source root as the child's entire
`PYTHONPATH`—never strings stored in `init.json` or the registry. For a
non-curated/legacy child, use that entry's configured command and environment;
for a daemon child, use its task/plugin configuration. If the probe still
resolves the old venv or source, the gate fails: report **requires relaunch**
and do not claim the change is live. Process inspection alone is corroboration,
not proof of imported module provenance.
