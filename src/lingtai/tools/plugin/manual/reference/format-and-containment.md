---
related_files:
- src/lingtai/tools/plugin/ANATOMY.md
- src/lingtai/tools/plugin/manual/SKILL.md
- src/lingtai/services/plugin_registry.py
- src/lingtai/tools/plugin/CONTRACT.md
- docs/examples/agent-plugins/hello-lingtai/plugin.json
- docs/examples/agent-plugins/hello-lingtai/mcp.json
maintenance: |
  Keep this reference aligned with the Agent Plugins v1.0.0 validation and §4.1
  containment implementation. It is the detailed destination for format and
  path rules routed from plugin-manual; update its links when files move.
---

# Plugin format and path containment

Use this reference after reading `plugin-manual` and before authoring or
reviewing a plugin. It covers the Agent Plugins v1.0.0 shape, component
validation, and the path gate. Registration remains registry-level metadata;
none of these declarations launches a process.

## Plugin directory and canonical identity

A plugin is a directory with this shape:

```text
my-plugin/
├── plugin.json                       # required
├── skills/                           # optional Agent Skills
│   └── some-skill/SKILL.md
├── mcp.json                          # optional server declarations
├── com.example.client/               # optional extension namespace
└── LICENSE
```

The required `plugin.json` pair is:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "my-plugin"
}
```

`$schema` is compared as the opaque, exact v1.0.0 identifier; the kernel never
fetches it. A missing, unreadable, malformed, or unsupported manifest rejects
the whole plugin and puts its reason in `info.problems`. `name` is 1--64
characters of lowercase letters, digits, `.` and `-`; it starts and ends with an
alphanumeric character and cannot contain `--` or `..`. For example,
`my-plugin`, `acme.tools`, and `lint3r` are valid; `My-Plugin`, `-leading`,
`trailing-`, `a--b`, and `a..b` are not.

Optional manifest fields are type-checked when present: `version`,
`description`, `homepage`, `repository`, and `license` are strings; `keywords`
is a list of strings; `author` and `extensions` are objects. The manifest
`description` becomes the catalog `<summary>`, truncated at 200 characters.

## Skills

Each subdirectory of `skills/` carrying `SKILL.md` is an Agent Skill. Grouping
directories are traversed, so `skills/group/nested/SKILL.md` is reported as
`group/nested`. A directory with loose files but no `SKILL.md` is corrupt and is
reported as skipped. Dot-directories and invalid entries are not treated as
validated skills.

Every skill directory is containment-checked independently. The reported names,
count, and validated paths are the same set: a rejected skill contributes no
path and is not rendered in the protected Plugin field. Registered plugin
skills remain inside that protected namespace; they are not copied to
`.library/` and are never injected into the vanilla `skills` catalog. For a
plugin that is only discovered, names/counts are informational in the
protected field and no skill is mounted.

## `mcp.json` declarations

The optional file has the v1.0.0 identifier and a `mcpServers` object:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "my-server": {
      "type": "stdio",
      "command": "python3",
      "args": ["${PLUGIN_ROOT}/server.py"]
    }
  }
}
```

Supported transports are `stdio` (required `command`; optional `args`, `env`,
`cwd`) and URL-addressed `streamable-http` or `sse` (required `url`; optional
`headers`). `env` and `headers` must map strings to strings. URL-addressed
transports are represented as registry transport `http`; `stdio` remains
`stdio`. For a registered plugin, declared `env`/`cwd` or `headers` survive in
the registry record along with `command` and `args`; `cwd` is resolved to an
absolute path.

A server name is also a registry name and must satisfy
`^[a-z][a-z0-9_-]{0,30}$`. An invalid name is skipped with a reason, never
silently renamed. A name already held by a hand-written or other owned record
is skipped and the existing record survives; between plugins, the first
canonical declaration wins. A server's exposed MCP tool names must remain
unique within a daemon run; a collision is a dispatch failure that requires
deduplication or renaming.

The server record is registration metadata stamped `source="plugin:<name>"`.
It is not a launch specification and does not run the server. Activation still
requires an operator-written top-level `mcp` entry in `init.json` and a refresh;
use `mcp-manual` for that activation contract.

## §4.1 path containment

Every plugin-relative path must start with `./` or the equivalent
`${PLUGIN_ROOT}/` form and must resolve inside the filesystem-resolved plugin
root. The gate covers `command`, `cwd`, and every `args` entry. It follows
symlinks before checking containment, so a symlink to an outside directory is
rejected just like `./../escape`.

Any relative value containing a `..` segment goes through the same gate even
when it omits `./`: `../../bin/sh` and `../x` cannot bypass validation. A
relative path such as `bin/../bin/serve` is normalized and accepted when it
still stays inside the root. Values that are not plugin-relative paths pass
through unchanged because §4.1 does not identify them as paths:

- an absolute path such as `/usr/bin/env`;
- an unexpanded `${ENV_VAR}` value;
- a bare token with no `..` segment such as `node` or `-c`.

This is a plugin-directory containment boundary, not a process sandbox. It
constrains declared plugin-relative locations while leaving activation and
execution to the explicit operator configuration.

## Failure boundaries and next step

An invalid `plugin.json` is a **whole-plugin** failure: the plugin is absent
from the catalog, registers nothing, and its reason appears in `problems`. An
invalid or escaping skill directory or MCP server is a **per-component**
failure: the rest of the plugin remains represented, while the component is
named in `info.problems` and `registered[].skipped`. A rejected server never
reaches the registry; a rejected skill never reaches the protected Plugin
field.

After any change or unexpected result, call
`plugin(action="info", input={}, reasoning="inspect plugin validation")` and
read both `problems` and each registered entry's `skipped` list. Keep
`summarize` absent or false when exact paths and reasons matter.

## Further reading

- [`registration-and-lifecycle.md`](registration-and-lifecycle.md) -- when a
  valid declaration becomes registered and how refresh converges it.
- [`diagnostics-and-settings.md`](diagnostics-and-settings.md) -- result fields,
  redaction, and troubleshooting.
- `docs/examples/agent-plugins/hello-lingtai/` -- a minimal working example.
- [Agent Plugins specification](https://agent-plugins.org/specification.md) --
  normative details such as placeholder expansion and extensions.
