---
related_files:
- src/lingtai/tools/plugin/ANATOMY.md
- src/lingtai/tools/plugin/manual/SKILL.md
- src/lingtai/tools/plugin/__init__.py
- src/lingtai/tools/plugin/settings.py
- src/lingtai/tools/plugin/CONTRACT.md
- src/lingtai/services/plugin_registry.py
- tests/test_plugin_tool.py
maintenance: |
  Keep these result, redaction, and error descriptions aligned with the plugin
  family and generic tool-family envelope. Do not add mutation inputs or expose
  private paths and secrets while expanding diagnostics.
---

# Plugin diagnostics and settings

Use this reference after the `plugin-manual` prerequisites when auditing a
registration or reading the declaration policy. The actions are read-only;
registration and refresh remain outside this tool surface.

## `info`: current audit snapshot

Call the complete envelope:

```text
plugin(action="info", input={}, reasoning="inspect plugin state", summarize=false)
```

On success, the result is:

```text
{
  status: "ok",
  declared,
  registered_count,
  registered,
  discovered_count,
  discovered,
  mcp_appended,
  mcp_pruned,
  paths,
  problems
}
```

`registered` is the boot/refresh snapshot, one entry per registered plugin. Its
entries contain `name`, `version`, `summary`, `source`, optional `homepage`,
`skills`, `skill_count`, `skills_mounted`, `mcp_servers`,
`mcp_server_count`, `mcp_registered`, and `skipped`. `skipped` is a list of
`{component, reason}` for every component not represented, including a bad
containment path, invalid server name, name collision, disabled Skills
capability, or other validation failure.

`discovered` is the discovery-only tier and contains `name`, `version`,
`summary`, `skill_count`, `mcp_server_count`, `source`, and optional `homepage`.
Discovery does not add registry records or vanilla Skills entries.

`paths` is keyed by each raw configured/discovery path and reports
`{resolved, exists, plugins}`. `problems` contains `{plugin, path, error}`
entries for whole-plugin and component failures. `mcp_appended` and
`mcp_pruned` describe the last boot/refresh registration operation, not an
`info`-initiated mutation. A normal `info` re-scan can update the protected
prompt section but does not change registration state.

Leave `summarize` absent or false when exact names, sources, paths, registry
facts, or rejection reasons are needed. `summarize=true` is presentation-only
and may shorten a deliberately large catalog; raw host output remains recorded.

## Diagnosis flow

1. Read the installed manual first, then call `info` with `summarize=false`.
2. If the plugin is absent from `registered` and `discovered`, inspect the path
   report and `problems`. A missing/unreadable/invalid `plugin.json` is a
   whole-plugin failure.
3. If it is under `discovered`, find the inherited Skills path that exposed it;
   add an authorized canonical `manifest.plugins` declaration and refresh if
   registration is intended. Discovery itself is not approval.
4. If it is registered but a component is absent, inspect that entry's
   `skipped`: path containment, symlink escapes, `..` segments, malformed
   fields, invalid names, collisions, or a disabled Skills capability are
   reported there.
5. After an authorized edit, refresh, call `info` again, and compare the
   registered tier and registry facts. Do not infer execution from a registry
   record; registration is not running.

Do not include secrets from plugin `env` or HTTP headers in a summary. Treat
third-party manifests and skill files as untrusted until read. Keep local source
paths out of public reports unless the operator specifically needs the exact
path for diagnosis.

## `settings`: redacted registration policy

Call:

```text
plugin(action="settings", input={}, reasoning="inspect plugin registration roots", summarize=false)
```

Success returns exactly one row:

```json
{
  "settings": [{
    "key": "manifest.plugins",
    "current": "<redacted>",
    "default": "<redacted>",
    "configurable": true,
    "comment": "plugin-manual#plugin-registration-roots"
  }]
}
```

The row represents the ordered configured registration roots from the detached
boot/refresh snapshot. Both path-list values are always redacted because they
can reveal local filesystem layout or trust boundaries. The automatic
`<workdir>/plugin` root and inherited Skills discovery paths are not included
in this row. `configurable: true` means an authorized configuration owner may
use the existing `init.json` edit plus `system(action="refresh")` procedure; it
does not grant this caller write authority.

Settings performs no path scan, does not inspect plugin files, does not expose a
set/reset form, does not edit `init.json`, and does not change the environment.
A successful settings read leaves registry records unchanged. Its complete
UTF-8 response is subject to the generic 65,536-byte bound, although this
family's single-row result is normally small.

If the detached provider is unavailable, malformed, or cannot be serialized,
the whole action fails without a row or private detail:

```json
{
  "status": "failed",
  "error_code": "SETTINGS_UNAVAILABLE",
  "message": "settings inventory is unavailable"
}
```

An over-size response uses the fixed `SETTINGS_RESPONSE_TOO_LARGE` failure.

## Envelope and validation failures

All three public actions require the standard closed envelope. `action`,
`input`, and `reasoning` are required; `input` must be `{}`; `summarize`, when
present, must be boolean. Unknown actions are rejected before filesystem I/O:

```json
{
  "status": "error",
  "message": "unknown action: 'install', only 'info' or 'settings' or 'manual' is supported"
}
```

Malformed root or action input uses the fixed generic shape:

```json
{
  "status": "failed",
  "error_code": "INVALID_ARGUMENT",
  "message": "..."
}
```

Extra input fields, unknown root fields, non-object input, and non-boolean
`summarize` fail before paths are scanned, settings are read, or the manual is
loaded. There is no action input for installation, uninstallation, refresh,
launch, or file editing.

`manual` success returns `{status: "ok", plugin_manual, manual_path}` and does
not scan. If the installed manual is missing, it returns a degraded result with
an empty body and error rather than silently substituting a different document.
Keep its `summarize` value false so that safety and lifecycle instructions are
not shortened.

## Authorized changes and verification

Settings is an inventory, not a configuration writer. An authorized owner may
edit only the canonical declaration in `init.json` through the established
`file`/`shell` workflow, then call `system(action="refresh")`. Verify with:

```text
plugin(action="info", input={}, reasoning="verify refreshed plugin registration", summarize=false)
plugin(action="settings", input={}, reasoning="verify configured plugin roots", summarize=false)
```

The first call diagnoses registered/discovered state and skipped components;
the second confirms that the redacted policy snapshot is available. Do not hand
edit registry records, delete plugin directories, or treat `settings` output as
permission to disclose path values.

## See also

- [`format-and-containment.md`](format-and-containment.md) -- exact manifest and
  path-gate rules.
- [`registration-and-lifecycle.md`](registration-and-lifecycle.md) -- tier
  semantics, refresh, ownership, and convergence.
- `src/lingtai/tools/plugin/CONTRACT.md` -- source-of-truth action contract.
