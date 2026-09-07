---
name: plugin-manual
description: >
  Router for the per-agent Agent Plugins (agent-plugins.org, v1.0.0) catalog:
  registered vs merely-discovered, install/uninstall mechanics, and
  skipped-component diagnosis. Read before authoring a `plugin.json` /
  `mcp.json`, or troubleshooting a plugin/skill/MCP entry missing from its
  catalog. Does NOT cover generic MCP registration (`mcp-manual`), authoring
  Agent Skills (`skills-manual`), or the Agent Plugins specification prose
  itself (agent-plugins.org).
version: 2.3.0
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/plugin/__init__.py
- src/lingtai/tools/plugin/settings.py
- src/lingtai/tools/plugin/ANATOMY.md
- src/lingtai/tools/plugin/CONTRACT.md
- src/lingtai/tools/plugin/manual/reference/format-and-containment.md
- src/lingtai/tools/plugin/manual/reference/registration-and-lifecycle.md
- src/lingtai/tools/plugin/manual/reference/diagnostics-and-settings.md
- src/lingtai/services/plugin_registry.py
- docs/examples/agent-plugins/hello-lingtai/plugin.json
maintenance: |
  Tracks the capability and service it summarizes; update when the plugin tool
  surface, the manifest validation rules, the registration/uninstall flow, or
  the Agent Plugins version this kernel understands change. Keep the two-tier
  mount contract (declared -> registered, inherited -> discovered) stated in
  this router, the tool description, and the prompt preamble in lockstep -- it
  is the same contract in three places, and the boundary it draws is a security
  boundary, not a presentation choice. Keep the reference links and their
  related_files entries synchronized when details move.
---

# Plugin capability -- manual router

The `plugin` capability is a **read-only** view of Agent Plugins at
[agent-plugins.org](https://agent-plugins.org), version 1.0.0. A plugin bundles
Agent Skills and optional MCP server configuration. This tool reports the boot
snapshot and current scan; it does not install, mount, launch, edit, or remove
anything.

## Read this first

Before inspecting, authoring, installing, or uninstalling a plugin, read this
manual (`plugin(action="manual", input={}, reasoning="read plugin guidance")`),
then call `plugin(action="info", input={}, reasoning="inspect plugin state")`.
The manual is the prerequisite for the contract and safety boundaries; `info`
is the prerequisite for acting on the current registered/skipped state. Read
third-party `plugin.json`, every `SKILL.md`, and especially `mcp.json` before
you trust it. A registered plugin is not thereby trustworthy.

## The identity gate: registered is not discovered

The protected `<registered_plugin>` catalog has two deliberate tiers.
`registered` is declared through a canonical public path; `discovered` is only
inherited visibility:

| Mount | Canonical identity/public path | Effect |
|---|---|---|
| `registered` | Declared in `init.json` `manifest.plugins` (the canonical key), or its retained alias `manifest.capabilities.plugin.paths` | Validated skills are listed in the protected Plugin field, never injected into the vanilla `skills` catalog. Validated `mcp.json` servers create registry records stamped `source="plugin:<name>"`; registration is **not running**. |
| `discovered` | Merely found on inherited `manifest.capabilities.skills.paths` | Metadata and counts are shown in the protected Plugin field only. Nothing is registered and no skills enter the vanilla catalog. |

This declaration gate is a security boundary: finding a directory where Skills
are searched must never silently register a third party's server. The automatic
`<workdir>/plugin` root is also a registration root; see the references for its
scope and how it differs from configured public paths.

## Action surface

Every call uses the standard envelope and an empty input object:

```text
plugin(action="info"|"settings"|"manual", input={}, reasoning="...")
```

`action`, `input`, and `reasoning` are required; the root `summarize` boolean is
optional presentation control. Any field inside `input` is rejected. The three
actions are read-only:

- `info` re-scans configured/discovery paths and reports the boot registration
  snapshot, discovered plugins, per-path health, skipped components, and
  problems. It does not register or return this manual body.
- `settings` returns one redacted inventory row for the configured
  `manifest.plugins` registration roots. It does not scan or mutate.
- `manual` returns this installed body on demand, without scanning or mutating.

A server is registered but not running: registration is registry-level metadata,
not a running process. No action launches a server, copies a skill, edits
`init.json`, changes the environment, or writes a registry file.

## Plugin registration roots

There is no install or uninstall action. An authorized configuration owner edits
the canonical `manifest.plugins` list in `init.json` using the existing `file`
or `shell` procedure, then calls `system(action="refresh")`. To install, add a
plugin root; to uninstall, remove that declaration and refresh. Call `info`
after refresh to verify `registered`, `skipped`, and `problems`. Never delete a
plugin directory as an uninstall step, and never hand-edit the registry. The
alias remains accepted for older configurations; prefer the canonical key in
new edits. Paths may be absolute, tilde-prefixed, or relative to the agent
working directory, and a root may be one plugin or a collection.

## Reference catalog

The router keeps the details available without putting every rule in the first
turn. Read the narrowest reference needed:

| Need | Reference |
|---|---|
| `plugin.json`, `skills/`, `mcp.json`, identity fields, transports, and §4.1 path containment | [`format-and-containment.md`](reference/format-and-containment.md) |
| registered/discovered registration, protected fields, refresh lifecycle, pruning, and cleanup | [`registration-and-lifecycle.md`](reference/registration-and-lifecycle.md) |
| `info`/`settings` result fields, redaction, failure diagnosis, envelope errors, size, and examples | [`diagnostics-and-settings.md`](reference/diagnostics-and-settings.md) |

| Human question | Start here |
|---|---|
| “Is this plugin registered or merely visible?” | `info`, then `registration-and-lifecycle.md` |
| “Can this command, cwd, or argument escape the plugin?” | `format-and-containment.md` |
| “Why was a skill or server skipped?” | `info.problems` and `diagnostics-and-settings.md` |
| “How do I change registration roots?” | `Plugin registration roots` above, then `diagnostics-and-settings.md` |
| “How do I activate a registered server?” | `mcp-manual`; registration here never launches it |

## Non-negotiable safety and presentation rules

- Canonical identity is `manifest.plugins`; the capability-path key is only a
  retained alias. Inherited Skills paths mean discovery only.
- Registration does not imply execution, trust, vanilla-catalog injection, or
  copying. The protected Plugin field is the namespace for plugin skills.
- Every plugin-relative `command`, `cwd`, and `args` value must use `./` (or
  `${PLUGIN_ROOT}/`) and resolve inside the plugin root after symlinks; values
  with `..` are checked through the same gate. Absolute paths, environment
  placeholders, and bare tokens without `..` pass through as documented.
- Keep settings paths redacted. Do not expose secrets from `env`, headers,
  local paths, or private errors in summaries or reports.
- `summarize=false` is required when exact names, paths, or skipped reasons are
  needed; never summarize away the manual's procedure.

## See also

- `src/lingtai/tools/plugin/CONTRACT.md` -- exact tool surface and envelopes.
- `src/lingtai/tools/plugin/ANATOMY.md` -- code ownership and navigation.
- `docs/examples/agent-plugins/hello-lingtai/` -- minimal dependency-free plugin.
- `mcp-manual` -- activation contract for a registered MCP server.
- [Agent Plugins specification](https://agent-plugins.org/specification.md) --
  normative details not repeated by this router.
