---
related_files:
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/plugin/BEHAVIORS.md
  - src/lingtai/tools/plugin/__init__.py
  - src/lingtai/tools/plugin/settings.py
  - src/lingtai/tools/plugin/CONTRACT.md
  - src/lingtai/tools/plugin/manual/SKILL.md
  - src/lingtai/tools/plugin/manual/reference/format-and-containment.md
  - src/lingtai/tools/plugin/manual/reference/registration-and-lifecycle.md
  - src/lingtai/tools/plugin/manual/reference/diagnostics-and-settings.md
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/adapters/tool_plugin_host.py
  - src/lingtai/tools/plugin/glossary-en.md
  - src/lingtai/tools/plugin/glossary-zh.md
  - src/lingtai/tools/plugin/glossary-wen.md
  - src/lingtai/services/plugin_registry.py
  - src/lingtai/services/mcp_registry.py
  - src/lingtai/services/ANATOMY.md
  - src/lingtai/tools/mcp/ANATOMY.md
  - src/lingtai/tools/skills/__init__.py
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/tools/registry.py
  - src/lingtai/agent.py
  - src/lingtai/init_schema.py
  - tests/test_plugin_tool.py
  - tests/test_tool_plugin_declaration.py
  - docs/examples/agent-plugins/hello-lingtai/plugin.json
  - docs/examples/agent-plugins/hello-lingtai/mcp.json
  - docs/examples/agent-plugins/hello-lingtai/server.py
  - docs/examples/agent-plugins/hello-lingtai/skills/hello-lingtai/SKILL.md
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. The mcp sibling is the pattern this package mirrors — when mcp's
  family composition, manual adaptation, or unknown-action envelope changes,
  re-check this one; when its addon decompression changes, re-check the
  registration half here, which mirrors it. See lingtai-dev-guide for details.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# lingtai/tools/plugin + lingtai/services/plugin_registry (split)

Plugin capability — the per-agent **Agent Plugins** (agent-plugins.org, v1.0.0)
catalog and registration. It scans the configured plugin paths, validates each
`plugin.json`, registers what was declared, and renders the protected Plugin
field as XML into the system prompt. It is the structural twin of
`src/lingtai/tools/mcp/ANATOMY.md` — same tool/service split and lazy back-edge,
plus Plugin's owner-specific reserved settings child — and its registration
half mirrors that package's addon decompression.

**Two tiers, and the line between them is a security boundary.** A plugin
*declared* in `init.json` `manifest.plugins` (or its alias
`manifest.capabilities.plugin.paths`) is **registered**: each of its validated
skill names/count are rendered in the protected Plugin field (never the vanilla
`skills` catalog) and its `mcp.json` servers become `mcp_registry.jsonl`
records stamped `source="plugin:<name>"`. A plugin merely
found on an inherited `manifest.capabilities.skills.paths` directory is
**discovered**: listed in the protected field, with no plugin component registered. Dropping a directory somewhere must
never silently register a third party's MCP server.

**The tool is still read-only.** Registration happens once, in `register_plugins`,
called by `Agent` before capability setup — no model-facing action can reach it,
which is what keeps the capability safe in `CORE_DEFAULTS`. Registration is also
registry-level only: a registered server is registered, never running.

**Declared host composition.** `plugin` is the second official declared family.
Its import-time `DECLARATION` is reserved by the kernel and its bind receives
only `workdir`, a protected prompt-section writer, and a detached
`plugin_catalog` state projection. The projection supplies the registration
snapshot, configured plugin paths, inherited skill paths, and skills
availability; it cannot validate, register, prune, launch, or mount. The
family never receives a whole Agent.

## Components

- `plugin/__init__.py` — the tool slice (~300 lines). One model-facing LTP v2
  family: public tool name `plugin`, public actions
  `info`/`settings`/`manual`, carried in
  the canonical `action` + `input` + `reasoning` + `summarize` envelope composed
  by the generic `lingtai.tools.tool_family` infrastructure
  (`src/lingtai/tools/tool_family/ANATOMY.md`).
  `get_description` (`src/lingtai/tools/plugin/__init__.py:228`),
  `get_schema` (`src/lingtai/tools/plugin/__init__.py:232`) returns
  `ToolFamily.build_schema()` with the family's own `action` description
  substituted, and `_build_family`
  (`src/lingtai/tools/plugin/__init__.py:203`) is the single source of the
  child registry — called with `None` at import for the module-level
  schema-only family `_FAMILY` (`src/lingtai/tools/plugin/__init__.py:274`),
  which fails loudly on a duplicate/reserved-name collision and never
  dispatches, and called with the granted `ToolPluginHost` from `_bind` for the
  real dispatching family, whose `manual` child receives only `host.workdir`
  through `tool_family.manual.build_manual_child`. The explicitly registered
  `info`/`manual` children share the one `_EMPTY_INPUT` literal
  (`src/lingtai/tools/plugin/__init__.py:188`)
  re-exported from `tool_family.manual.MANUAL_INPUT_SCHEMA`, so the advertised
  and validated shapes cannot drift. `_reconcile`
  (`src/lingtai/tools/plugin/__init__.py:109`) backs the `info` child: it reads
  the detached `host.plugin_catalog` state plus `host.workdir`, re-scans
  discovery live, splits the registered and discovered tiers, and renders only
  through `host.prompt_section`.
  It never registers — that is what makes `info` safe to call.
  `_registered_entries` (`src/lingtai/tools/plugin/__init__.py:81`) projects the
  snapshot and the catalog state's `skills_enabled` fact, so a plugin whose
  skills capability is disabled says so in `skipped`; the protected Plugin field
  still reports validated skill facts without claiming vanilla-catalog composition
  that did not happen. `_catalog_entry` (`src/lingtai/tools/plugin/__init__.py:66`) projects a
  discovered record down to catalog facts. `_collect_paths`
  (`src/lingtai/tools/plugin/__init__.py:48`) is where this capability differs
  from mcp — it unions its own configured `manifest.capabilities.plugin.paths`,
  the snapshot's declared paths (the canonical `manifest.plugins`, which does not
  otherwise reach this capability), and the inherited skills paths from
  `host.plugin_catalog`, in that order and de-duplicated. `_flatten_manual_result`
  (`src/lingtai/tools/plugin/__init__.py:144`) is the Host-owned adapter that
  turns the manual child's canonical `content`/`structuredContent` result back
  into the flat `plugin_manual` public shape strictly *after* dispatch (no
  double wrap), and `handle_plugin` owns the unknown-action envelope —
  including the missing-action empty-string default and unhashable `action`
  values (issue #513), routed by tuple membership against `child_names`, which
  compares by `==` and never hashes — before delegating to `ToolFamily.handle`.
  This slice implements [PL001](BEHAVIORS.md#behavior-pl001). All three actions
  declare a strict-empty `input`, so any extra input field
  fails before paths are re-scanned, settings are read, or the manual is loaded.
- `plugin/settings.py` — the one-setting owner slice.
  `plugin_setting_rows` (`src/lingtai/tools/plugin/settings.py:9`) reads only
  `registration["configured_declared"]` from
  the detached catalog and returns one sensitive five-field `manifest.plugins`
  row; unavailable current truth raises for the generic whole-action failure
  guarded by [PL001](BEHAVIORS.md#behavior-pl001).
- `plugin/manual/` — the `plugin-manual` skill (`SKILL.md`). Installed to
  `.library/intrinsic/capabilities/plugin/SKILL.md` by the Agent initializer's
  generic `install_from(tools_pkg, "capabilities")` sweep, which picks up any
  tool package carrying a `manual/` directory — no per-package wiring.
- The service lives at `src/lingtai/services/plugin_registry.py` and splits in
  two halves. **Discovery:** `resolve_contained`
  (`src/lingtai/services/plugin_registry.py:110`) is the §4.1 path-containment
  gate — `./` prefix required syntactically, containment checked *after*
  `Path.resolve()` so a symlink escape is rejected exactly like a literal `../`
  escape; `validate_manifest`
  (`src/lingtai/services/plugin_registry.py:145`) enforces the two required
  manifest fields against the pinned `PLUGIN_SCHEMA_URL`
  (`src/lingtai/services/plugin_registry.py:58`) and the transcribed v1.0.0
  `name` grammar `_NAME_RE` (`src/lingtai/services/plugin_registry.py:69`);
  `_scan_skills` (`src/lingtai/services/plugin_registry.py:273`) enumerates the
  plugin's skills through `_walk_skills`
  (`src/lingtai/services/plugin_registry.py:195`), whose traversal deliberately
  validates each skill directory individually so the reported set is exactly the
  protected Plugin-field set and returns per-skill validated paths for the host
  registration snapshot; the vanilla `skills` catalog never receives the parent
  `skills/` or any Plugin path; and
  `_scan_mcp_servers`
  (`src/lingtai/services/plugin_registry.py:412`) reads `mcp.json` into validated
  `{name, spec}` entries, each applying the per-component failure boundary.
  Every server passes through the one gate `resolve_server_spec`
  (`src/lingtai/services/plugin_registry.py:344`), which validates the transport
  and resolves each plugin-relative `command`/`cwd`/`args` value via
  `_expand_plugin_path` (`src/lingtai/services/plugin_registry.py:300`) — which
  also normalizes any relative value carrying a `..` segment through the same
  gate, so `../x` cannot slip past by omitting the `./` — so
  discovery and registration cannot disagree about what is acceptable, and a path
  smuggled through an argument is checked exactly like one in `command`.
  `read_plugin` (`src/lingtai/services/plugin_registry.py:473`) applies the
  whole-plugin boundary; `resolve_path`
  (`src/lingtai/services/plugin_registry.py:535`) mirrors the skills capability's
  path resolution; `scan_plugin_root`
  (`src/lingtai/services/plugin_registry.py:547`) treats a configured path as a
  collection directory unless it carries `plugin.json` itself; `read_plugins`
  (`src/lingtai/services/plugin_registry.py:580`) is the multi-path entry point
  returning `(records, problems, report)` with duplicate names flagged
  first-wins. **Registration:** `declared_plugin_paths`
  (`src/lingtai/services/plugin_registry.py:629`) resolves the canonical key and
  its alias into one ordered, de-duplicated list; `to_registry_record`
  (`src/lingtai/services/plugin_registry.py:665`) translates one resolved server
  into a registry record through `mcp_registry.validate_record` — the exact
  validator the addon path uses, so a plugin cannot introduce a shape the
  registry would reject; `prune_plugin_records`
  (`src/lingtai/services/plugin_registry.py:725`) is the uninstall mechanism,
  dropping only `source="plugin:*"` lines that the current declaration no longer
  implies and preserving blank, unparseable, and foreign-source lines byte for
  byte; `register_plugins`
  (`src/lingtai/services/plugin_registry.py:779`) is the one mutation point,
  pruning first and appending second so a changed spec is replaced rather than
  duplicated. Its snapshot keeps exact authored `configured_declared` roots
  separate from `declared`, which may also contain the automatic
  `<workdir>/plugin` root. **Render:** `_plugin_xml`
  (`src/lingtai/services/plugin_registry.py:946`) stamps each plugin with its
  `<mount>` tier and `_build_registry_xml`
  (`src/lingtai/services/plugin_registry.py:984`) renders the
  `<registered_plugin>` section whose preamble states the two-tier contract in
  the same words as the tool description.
- Boot wiring lives in `src/lingtai/agent.py`: `_register_declared_plugins`
  (`src/lingtai/agent.py:579`) runs on both boot paths immediately after addon
  decompression and before capability setup, and records `_plugin_skill_paths`
  and `_plugin_registration` on the agent. It runs with an empty declaration list
  too — that empty run *is* the uninstall path. `__init__` calls it only when it
  was given `capabilities=` or `plugins=`, though: the CLI's minimal construction
  declares neither and defers to `_setup_from_init`, which always calls it, so
  the boot flow registers once instead of pruning and re-appending every record.
- The vanilla skills capability remains a separate closed namespace. Its
  `_compose_paths` (`src/lingtai/tools/skills/__init__.py:105`) intentionally
  excludes Plugin paths; Plugin skill names/counts are rendered only by the
  protected field through `plugin_registry._plugin_xml`
  (`src/lingtai/services/plugin_registry.py:946`).
- The canonical config key is declared in `src/lingtai/init_schema.py`
  (`MANIFEST_OPTIONAL["plugins"]`), with a shape-only warning for malformed
  entries; per-plugin validation happens at registration.
- A minimal working plugin ships at
  `docs/examples/agent-plugins/hello-lingtai/` — one skill, one stdio MCP server
  (stdlib only, so it stands in for genuinely third-party code), pinned by
  `tests/test_plugin_tool.py::test_the_shipped_example_plugin_registers_end_to_end`.

## Public API

The `plugin` tool exposes three read-only actions through the LTP v2 envelope
`plugin(action=..., input=..., reasoning="...")`; `summarize` is the optional
root presentation control:

| Action | Description |
|--------|-------------|
| `info` | Re-scan the configured plugin paths and return the boot registration snapshot: `declared`, `registered_count`, `registered` (per plugin: name, version, summary, source, skills, skill_count, skills_mounted, mcp_servers, mcp_server_count, mcp_registered, skipped), `discovered_count`, `discovered`, `mcp_appended`, `mcp_pruned`, a per-path `paths` report, and `problems`. No manual body, and no mounting. |
| `settings` | Return one redacted `manifest.plugins` row with exactly `key`, `current`, `default`, `configurable`, and `comment`. The automatic workdir root and inherited discovery roots are excluded; the action has no mutation API. |
| `manual` | Return the `plugin-manual` skill body on demand, with no scan or mutation. |

Installing and uninstalling are not actions: both are edits to
`manifest.plugins` followed by `system(action="refresh")`.

## Agent Plugins v1.0.0 shape

```
my-plugin/
├── plugin.json          # required: $schema + name
├── skills/<name>/SKILL.md   # optional — listed in the protected Plugin field when declared; not vanilla-catalog injected
├── mcp.json             # optional — becomes registry records when declared
└── com.example.client/  # optional reverse-domain extension namespace (ignored)
```

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "one-line summary rendered as <summary>"
}
```

## Internal Module Layout

```
plugin/__init__.py
  ├── Path collection
  │   └── _collect_paths()          — own ∪ declared ∪ skills paths, in that order, deduped
  │
  ├── Reconciliation
  │   ├── _catalog_entry()          — one discovered record → the catalog facts
  │   ├── _registered_entries()     — catalog-state snapshot → the registered tier
  │   └── _reconcile()              — report snapshot + re-scan discovery + render prompt
  │
  ├── Manual adaptation
  │   └── _flatten_manual_result()  — canonical child result → flat plugin_manual shape
  │
  └── Tool surface
      ├── _build_family()           — info + injected settings + manual registry
      ├── handle_plugin()           — preserve Plugin unknown/manual envelopes
      ├── get_description/schema()  — module-level, backed by the schema-only _FAMILY
      ├── _bind()                   — pure host-bound family composition
      ├── DECLARATION               — import-time official identity and ports
      └── setup()                   — registrar wiring only; activation reconciles

services/plugin_registry.py
  ├── §4.1 containment
  │   └── resolve_contained()       — './' prefix + post-resolve containment (symlinks followed)
  │
  ├── Manifest validation
  │   └── validate_manifest()       — required $schema + name, typed optional fields
  │
  ├── Component discovery
  │   ├── _scan_skills()            — immediate skills/<name>/SKILL.md subdirs
  │   ├── _expand_plugin_path()     — './' and ${PLUGIN_ROOT}/ → absolute, containment-checked
  │   ├── resolve_server_spec()     — the one gate: transport + every referenced path
  │   └── _scan_mcp_servers()       — mcp.json mcpServers → [{name, spec}]
  │
  ├── Plugin / path scan
  │   ├── read_plugin()             — one plugin dir → (record | None, problems)
  │   ├── resolve_path()            — tilde / absolute / working-dir-relative
  │   ├── scan_plugin_root()        — collection dir, or single plugin if it has plugin.json
  │   └── read_plugins()            — all configured paths → (records, problems, report)
  │
  ├── Declaration + registration
  │   ├── declared_plugin_paths()   — manifest.plugins ∪ capabilities.plugin.paths alias
  │   ├── plugin_source()           — the "plugin:<name>" ownership stamp
  │   ├── to_registry_record()      — resolved server → record, via mcp_registry.validate_record
  │   ├── prune_plugin_records()    — drop owned records the declaration no longer implies
  │   └── register_plugins()        — the one mutation point: prune, then append
  │
  └── XML builder
      ├── _escape_xml()             — XML entity escaping
      ├── _plugin_xml()             — one <plugin> element, stamped with its <mount> tier
      └── _build_registry_xml()     — both tiers → <registered_plugin> prompt section
```

## Key Invariants

- **Declaration gates registration:** an inherited skills path is scanned and listed
  but never registers anything. Only `manifest.plugins` and its alias mount.
- **No model-facing mutation:** no action registers, unregisters, copies, or
  spawns anything. Settings has no set/reset or other mutation shape;
  `register_plugins` is boot/refresh-only and unreachable from the tool surface,
  which is why default-on is safe.
- **Registration never executes:** a registered MCP server holds a registry
  record and nothing more, exactly as a decompressed addon does. Running it
  still requires an `init.json` top-level `mcp` entry.
- **Plugin skills stay in the protected namespace:** a registered plugin's
  skill names/counts are rendered with a `source` inside the plugin. Nothing
  is written under `.library/` or injected into the vanilla `skills` catalog,
  which is why uninstall needs no file deletion.
- **Registration owns only its own stamp:** a name already held by a
  hand-written or addon record skips the plugin's server rather than
  overwriting it; between two plugins, first declared wins. Pruning likewise
  touches only `source="plugin:*"` lines and leaves blank, unparseable, and
  foreign-source lines byte for byte.
- **Idempotent and convergent:** running registration twice leaves the same
  registry as once, and the registry converges on the current declaration —
  removed plugins, removed servers, and changed specs all resolve.
- **Path containment (§4.1):** every plugin-relative path MUST begin with `./`
  (or the equivalent `${PLUGIN_ROOT}/` form) and MUST resolve inside the
  filesystem-resolved plugin root — `command`, `cwd`, and every entry of `args`
  alike. Because containment is checked after `Path.resolve()`, a `./symlink`
  pointing outside is rejected identically to `./../escape`, and it is enforced
  at registration, so an escaping server never reaches the registry.
- **Two failure boundaries:** an unreadable or invalid `plugin.json` rejects the
  whole plugin (absent from the catalog, registers nothing, reason in
  `problems`); an individual escaping or malformed skill directory / MCP server
  is skipped while the rest of the plugin remains listed, with the reason in that
  plugin's `skipped` list.
- **`$schema` is a local identifier, never a fetch:** the pinned v1.0.0 URLs are
  compared as opaque strings; the kernel makes no network call during a scan.
- **Duplicate names are first-wins:** a second plugin with an already-seen name
  is dropped with a problem entry, mirroring the mcp registry's duplicate
  handling.
- **Paths are inherited, not duplicated:** the skills capability's configured
  paths are scanned for plugins too, so operators declare a directory once for
  discovery — but inheritance never registers.

## Dependencies

- `lingtai.kernel.tool_plugin` — `ToolPluginDeclaration`, `BoundToolPlugin`, and
  the narrow read-only catalog-state contract
- `lingtai.tools.tool_family` — `ChildTool` / `ToolFamily` / `build_manual_child`
  plus the five-field `SettingRow`
- `lingtai.services.plugin_registry` — lazily imported inside `_reconcile`
  (the `lingtai.tools → lingtai` back-edge)
- `lingtai.services.mcp_registry` — lazily imported inside the registration
  functions for `validate_record` / `read_registry` / `_append_record` /
  `_registry_path`, so plugin-sourced records pass the same gate as addons
- `lingtai.kernel.base_agent.BaseAgent` — agent type (TYPE_CHECKING only)
- stdlib only in the service (`json`, `re`, `logging`, `pathlib`)

## Composition

- **Parent:** `src/lingtai/tools/` (tool slice); the service sibling lives in
  `src/lingtai/services/`.
- **Siblings:** `mcp/` (the pattern this package mirrors, on both the family and
  the decompression sides), `skills/` (whose configured paths this capability
  inherits for discovery; its vanilla catalog intentionally excludes Plugin
  skills), `knowledge/`, `daemon`.
- **Manual:** `plugin/manual/SKILL.md` — the `plugin-manual` router.
- **Kernel hooks:** `setup()` is called during capability initialization from
  `src/lingtai/tools/registry.py`; boot-time registration is called earlier, by
  `Agent._register_declared_plugins` on both the constructor and
  `_setup_from_init` paths, alongside addon decompression.
