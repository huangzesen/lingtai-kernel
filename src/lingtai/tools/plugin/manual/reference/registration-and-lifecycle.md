---
related_files:
- src/lingtai/tools/plugin/manual/SKILL.md
- src/lingtai/services/plugin_registry.py
- src/lingtai/tools/plugin/__init__.py
- src/lingtai/tools/plugin/CONTRACT.md
- src/lingtai/adapters/tool_plugin_host.py
- docs/examples/agent-plugins/hello-lingtai/plugin.json
maintenance: |
  Keep the two-tier registration contract and authorized refresh workflow in
  lockstep with plugin_registry and the plugin tool. Preserve the distinction
  between configured declarations, the automatic root, inherited discovery,
  and the protected Plugin prompt field.
---

# Plugin registration and lifecycle

This reference explains how a validated plugin becomes visible to an agent. It
is reached from `plugin-manual` after the read-only and trust prerequisites.
Registration is a boot/refresh operation, not a model-facing action.

## The public-path and identity gates

`manifest.plugins` in `init.json` is the canonical declaration key.
`manifest.capabilities.plugin.paths` is a retained compatibility alias with the
same registration meaning. The canonical list is considered first; duplicate
entries across the key and alias are scanned once. Prefer `manifest.plugins` in
new edits. A declaration is the public identity gate for registration.

`manifest.capabilities.skills.paths` is different: it is inherited for
**discovery only**. A plugin found there is visible in the protected Plugin
field but cannot register its skills or servers. This prevents a directory
merely placed in an existing Skills search path from silently gaining trust.

Each configured entry may be an absolute path, a tilde-prefixed path, or a path
relative to the agent working directory. It may name a single directory carrying
`plugin.json` or a collection whose immediate children are plugin roots. The
automatic `<workdir>/plugin` root is scanned as a registration root as well,
but is derived and is not included in the configured setting row. The successful
snapshot keeps exact configured declarations separate from operational roots.

The identity gate is therefore two-dimensional: the plugin's canonical manifest
`name` identifies the catalog entry, and its path must come from an explicit
registration root to receive the `registered` tier. Inherited discovery never
substitutes for either gate.

## What each tier does

| Tier | Source | Protected Plugin field | Registry |
|---|---|---|---|
| `registered` | Canonical `manifest.plugins`, retained alias, or automatic root | Validated skill names/count and registration facts | Validated `mcp.json` servers are recorded with `source="plugin:<name>"` |
| `discovered` | Inherited `manifest.capabilities.skills.paths` only | Metadata and counts for visibility | No server or component is registered |

Plugin skills are a closed namespace. Even in the registered tier, validated
skill paths remain in the plugin and their names/counts are rendered only in
the protected Plugin field; they are not copied into `.library/` and are not
injected into the vanilla `skills` catalog. A discovered plugin is even more
limited: its metadata is informational and none of its components are mounted.

Registration is registry-level only. A server record means registered, **not
running**; no subprocess is spawned. To activate a registered server, an
operator must provide a matching top-level `mcp` entry in `init.json` and
refresh. Consult `mcp-manual` for activation rather than treating registration
as execution.

## Boot and refresh

At boot or refresh, the existing registration service reads declarations,
validates manifests/components, records the registered snapshot, writes only
plugin-owned registry records, and supplies validated per-skill paths to the
protected prompt-section writer. The `plugin` actions cannot invoke this path:

- `info` re-scans and reports the snapshot; it never registers newly found data.
- `settings` reads a detached snapshot; it never scans or changes declarations.
- `manual` reads the installed manual; it never scans or changes declarations.

This separation is why the default-on, read-only capability cannot be used as an
installation mechanism. After an authorized declaration edit, `system(action="refresh")`
is the operation that applies it; call `info` afterward to audit the result.

## Authorized install and uninstall

There is deliberately no install or uninstall action. Only an authorized
configuration owner should use the existing `file` or `shell` procedure to edit
`init.json`:

1. **Install:** read the plugin and this manual, add its directory to canonical
   `manifest.plugins`, then call `system(action="refresh")`. For example:

   ```json
   {
     "manifest": {
       "plugins": ["./plugins/hello-lingtai"]
     }
   }
   ```

2. **Verify:** call
   `plugin(action="info", input={}, reasoning="confirm plugin registration")`;
   inspect `registered`, `registered[].skipped`, `mcp_registered`, and
   `problems`.
3. **Uninstall:** remove the declaration from `manifest.plugins`, then call
   `system(action="refresh")` and verify that the entry is gone.

Use the alias only when maintaining an older configuration. Never hand-edit
`mcp_registry.jsonl` as part of this workflow and never delete the plugin
directory: the declaration is the installation state, while the directory
belongs to the person or package that provided it.

## Convergence and ownership

Registration first prunes plugin-owned records that the current declarations
no longer imply, then appends valid current records. Consequently:

- removing a plugin declaration removes its `source="plugin:<name>"` records;
- removing one server from a still-declared `mcp.json` removes only that record;
- changing a server specification replaces its stale record rather than
  accumulating duplicates;
- running the same refresh twice is idempotent;
- hand-written, addon-owned, foreign-source, blank, and unparseable lines are
  preserved because the service removes only records it owns.

A collision with an existing registry name skips the plugin server and leaves
the existing record untouched. Between two plugin declarations, the first
wins. A skipped component is absent from the corresponding protected field or
registry, not merely labeled while still being used.

## Protected prompt representation

The `<registered_plugin>` section is protected and contains each visible
plugin's identity, summary, source, mount tier, skill facts, and MCP facts. For
registered entries, `<mount>registered</mount>` distinguishes the explicit
trust tier; discovered entries carry `<mount>discovered</mount>`. A registered
entry's `skills_mounted` says only whether validated plugin skill paths were
accepted while the Skills capability was enabled. It never means that those
skills entered the vanilla catalog.

`info` can reconcile this protected section from live discovery while retaining
the boot registration distinction. If a component was skipped during boot or
scan, the reason is exposed through the diagnostic fields described in the
companion reference.

## Further reading

- [`format-and-containment.md`](format-and-containment.md) -- manifest,
  component, and path-gate rules.
- [`diagnostics-and-settings.md`](diagnostics-and-settings.md) -- exact info
  and settings fields and failure diagnosis.
- `src/lingtai/tools/plugin/CONTRACT.md` -- source-of-truth public contract.
