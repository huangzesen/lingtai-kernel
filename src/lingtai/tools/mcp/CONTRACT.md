---
name: mcp-contract
tool: mcp
contract_version: 1
related_files:
  - src/lingtai/tools/mcp/__init__.py
  - src/lingtai/tools/mcp/settings.py
  - src/lingtai/tools/mcp/plugin.json
  - src/lingtai/agent.py
  - src/lingtai/tools/mcp/ANATOMY.md
  - src/lingtai/tools/mcp/BEHAVIORS.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_mcp_settings.py
  - tests/test_mcp_builtin_plugin_package.py
  - src/lingtai/tools/mcp/skills/mcp-manual/SKILL.md
  - src/lingtai/tools/mcp/skills/mcp-manual/reference/runtime-and-identity.md
  - src/lingtai/services/mcp_registry.py
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/tool_family/CONTRACT.md
maintenance: |
  Keep related_files as repo-relative paths to real files. If behavior and this
  contract disagree, the code is the source of truth — fix the contract in the
  same change and bump contract_version on breaking contract edits. mcp's schema
  composition and envelope dispatch build on the generic tool_family package;
  keep that link current when either side's boundary changes. mcp is one declared
  slice under the kernel-owned family-generic contract (the current base
  reference); the shared C register also targets other names without claiming
  their candidates merged. Keep §Declared host plugin below and that contract in
  step, and if this family's required host ports change, move the declaration,
  the narrow port set, the adapter, and both contracts together.
  The source package is additionally a built-in Agent Plugins v1.0.0
  documentation package: keep `plugin.json`, its sole owned
  `skills/mcp-manual/` source, `Agent._install_intrinsic_manuals`, and the
  compact packaging test in step. That source layout is not an external
  declaration, transport, or registry route; use `plugin_registry.read_plugin`
  as the only manifest/containment reader and never add a tools-local helper.
---

# MCP capability contract

`mcp` is a read-only tool family: it renders the per-agent MCP server registry
into the protected `mcp` system-prompt section, reports registry health, and
SHOWs the two MCP-owned top-level init settings. It does NOT register, activate,
configure, or troubleshoot MCP servers —
all mutations happen by editing `mcp_registry.jsonl` with `write`/`edit`. The tool
slice lives in `src/lingtai/tools/mcp/__init__.py`; the registry machinery it renders
lives in `src/lingtai/services/mcp_registry.py` (imported lazily). The code is the
source of truth.

## Routing Card
Guarded by: [MC001](BEHAVIORS.md#behavior-mc001)


**Use this when:**
- You are editing the mcp tool slice's action dispatch or the reconciliation that
  builds the prompt XML.
- You need to confirm which fields `info` surfaces and that the tool never mutates
  MCP configuration.

**Do not use this for:**
- Registry validation, JSONL I/O, catalog load, identity projection, or addon
  decompression: those are the service at
  `src/lingtai/services/mcp_registry.py`.
- Code navigation only: read `src/lingtai/tools/mcp/ANATOMY.md`.
- Actually registering an MCP: edit `mcp_registry.jsonl` with `write`/`edit`,
  then call `system(action="refresh")`.

**Fast paths:** tool schema and the LTP v2 envelope -> §Tool surface; registry
file location & writers -> §State & storage; the `lingtai.tools → lingtai` lazy
back-edge -> §Scope; the generic composition/dispatch infrastructure ->
`src/lingtai/tools/tool_family/CONTRACT.md`.

## Scope

- Canonical tool name: `mcp`.
- Registered via `capabilities=["mcp"]` or via init.json.
- Symmetric to `skills` / `knowledge`: a per-agent presentation capability with a
  protected prompt section.
- Non-goals: this tool never writes configuration or the registry, never
  launches or configures a server, and never troubleshoots one. `info` keeps
  its re-render + health behavior, `settings` is SHOW-only, and `manual` keeps
  returning the umbrella manual body. External MCP registration remains
  entirely outside this tool.
- Ownership boundary: the module is the agent-callable tool slice only. The
  registry service is imported lazily inside `_reconcile` and the handlers, per
  the `lingtai.tools → lingtai` lazy-back-edge rule, as is the host adapter
  inside `setup`.

## Tool surface

`mcp` is an LTP v2 action-separated family (`src/lingtai/tools/CONTRACT.md`
"Envelope") built on the generic `src/lingtai/tools/tool_family/`
infrastructure. The public tool name stays `mcp`; its public action values are
exactly `info` / `settings` / `manual`, with settings immediately before the
reserved manual child. Exactly three read-only actions; the handler is
`handle_mcp`, which delegates envelope validation and dispatch to a per-Agent
`ToolFamily`.

**Envelope.** The model-facing schema is `get_schema()` =
`ToolFamily.build_schema()` with the existing `info`/`manual` signpost meaning
preserved and `settings` described additively. Root properties are exactly `action`, `input`,
`reasoning`, and `summarize`; `required` is `[action, input, reasoning]` and the
root is closed (`additionalProperties: false`). `reasoning` is required Host
InvocationContext/audit metadata declared by the family itself, never action
input. `summarize` is the optional root presentation control, validated as
boolean and stripped before dispatch. All actions take **no arguments**, so
all use the canonical strict-empty `input`
(`{type: object, properties: {}, additionalProperties: false}`) — the
`MANUAL_INPUT_SCHEMA` literal exported by `tool_family.manual` and reused here
as `_EMPTY_INPUT`, rather than hand-copied per action, so the schema-only and
dispatching families cannot advertise different shapes. The root
`allOf`/`if`/`then` correlates each `action` const with its own `input` branch,
and `input.anyOf` discloses all branches with titles
`info input` / `settings inventory input` / `manual input`.

| Action | Required inputs | Optional inputs | Success output | Error shapes |
|---|---|---|---|---|
| `info` | `action="info"`, `input={}`, `reasoning` | `summarize` | reconciles registry, re-injects prompt XML, returns `{status: "ok", registry_path, registered_count, registered: [{name, summary, identity?}], problems}` | see below |
| `settings` | `action="settings"`, `input={}`, `reasoning` | `summarize` | `{settings: [{key, current, default, configurable, comment}, ...]}` for exactly `init.addons`, `init.mcp`; the latter row's current and default are fully redacted | fixed bounded no-row failures from the shared SHOW seam |
| `manual` | `action="manual"`, `input={}`, `reasoning` | `summarize` | `{status: "ok", mcp_manual, manual_path}` | degraded shape below |

**Settings ownership.** `MCPSettingsProvider` fresh-reads the same canonical
materialized/resolved init composition used by boot and refresh. Row order is
exactly `init.addons`, then `init.mcp`; both are configurable, with logical
defaults `[]` and `{}`. `init.mcp` sets the private sensitive marker so both
current and default become `<redacted>` before serialization. Registry/catalog
contents, identity, curated-addon config/session data, Task Cards, agent
identity, legacy `mcp/servers.json`, and live client/process state are excluded.
Provider/read/row/serialization failure makes the whole inventory unavailable;
the shared 65,536-byte bound never returns partial rows. No writer exists.

Each `registered` entry is `{name, summary}` and carries `identity` only when a
matching identity record with non-empty `accounts` exists. `manual` returns
`status: "degraded"` with an empty `mcp_manual` and an `error` string when
`.library/intrinsic/capabilities/mcp/SKILL.md` is missing.

`manual` is the family-owned reserved child, registered directly from
`tool_family.manual.build_manual_child(host.workdir, DECLARATION.manual)`.
`ToolFamily.handle()` returns that child's canonical `content`/`structuredContent`
result verbatim (no double wrap); mcp's pre-migration flat public shape — body
under the tool-specific key `mcp_manual`, not the generic `manual` — is
reconstructed by the Host-owned `_flatten_manual_result` strictly *after*
dispatch returns, never inside a registered child. `manual` performs no registry
read, rescan, or mutation.

**Error shapes** (plain dicts):
- Unknown action: `{"status": "error", "message": "unknown action: <action>, only 'info', 'settings', or 'manual' is supported"}`. This envelope is Host-owned, so `handle_mcp` renders it *before* delegating: it preserves the empty-string default for a missing `action` key and routes an unhashable `action` (`[]`, `{}` from invalid JSON — issue #513) here instead of into the generic dispatcher's dict lookup. An unknown action is rejected before any input validation or handler I/O.
- Invalid envelope/input (from the generic dispatcher, canonical and unwrapped): `{"status": "failed", "error_code": "INVALID_ARGUMENT", "message": ...}` for a non-object `input` (`input must be an object`), an unknown root field (`unsupported mcp argument`), a non-boolean `summarize` (`summarize must be a boolean`), or any `input` key outside the selected action's strict-empty schema (`unsupported mcp input field`). Because all actions declare an empty `input`, any extra input field fails **before** provider/registry/manual I/O.

## Declared host plugin

`mcp` is the first family declared under the kernel-owned declared host-plugin
contract, [`src/lingtai/kernel/tool_plugin/CONTRACT.md`](../../kernel/tool_plugin/CONTRACT.md).
This section is this family's own conformance evidence for that contract; the
normative rules stay there.

- **Static declaration.** `DECLARATION` is a module-level
  `ToolPluginDeclaration` built at import, before any `Agent` exists. It
  declares `actions=("info",)` and `settings=True` — the reserved SHOW child
  and `manual` are appended by the
  declaration and never declared as an operational action — one strict-empty
  `input` schema per action, `manual="mcp"` (the installed manual destination
  the reserved child reads), the canonical description, and
  `glossary_package="lingtai.tools.mcp"`. It reaches the registrar because
  `setup` hands it over, never because anything was scanned.
- **Reserved official name.** `mcp` is in the kernel's static
  `OFFICIAL_TOOL_PLUGIN_NAMES`. A second, different declaration of `mcp` is
  refused before any bind, activate, or official mount, and cannot replace the live
  claim. Re-registering the same declaration on refresh is idempotent.
- **Required host ports.** `requires=("workdir", "prompt_section")`, and
  nothing else is reachable. `workdir` supplies the working directory used by
  `_reconcile` and by the reserved `manual` child; `prompt_section` is the
  declaration-bound port that writes this plugin's own protected section;
  `mcp` can neither
  address another section nor write an unprotected one. `mcp` never receives
  the `Agent`, and never receives the mount port.
- **Bind versus activate versus mount.** `_bind(host)` composes the per-host
  `ToolFamily` and the `handle_mcp` wrapper and returns a `BoundToolPlugin`; it
  performs no I/O and mounts nothing. The boot reconcile — the first registry
  read and protected prompt render — is the separately declared `activate`
  step, which the kernel registrar runs after the name check and immediately
  before mounting. `setup(agent)` is composition wiring only.
- **Bounded additive public change.** The only new public action is the generic
  reserved `settings` child immediately before `manual`. Tool name, closed
  root envelope, `info`'s health snapshot, and `manual`'s flat
  `status`/`mcp_manual`/`manual_path` shape (including the degraded shape) stay
  unchanged.
- **No transport.** Nothing here spawns a process, opens a connection, or
  publishes a catalog record. The curated MCP server packages and
  `src/lingtai/mcp_catalog.json` remain a separate external-transport concern
  that this family does not use.
- **Packaged manual, not plugin admission.** `plugin.json` and its one owned
  `skills/mcp-manual/` Agent Skill are read by the existing
  `plugin_registry.read_plugin` inside `Agent._install_intrinsic_manuals`, then
  copied under the manifest/declaration name `mcp`. This is only the source of
  the reserved child’s installed manual. It neither calls `register_plugins`
  nor writes `mcp_registry.jsonl`; `mcp.json` is forbidden for this package, so
  packaging cannot add or activate an MCP server. The static `DECLARATION`
  remains the only model-facing mount route.

## State & storage

The capability reads (never writes) the per-agent registry:

```text
<agent>/mcp_registry.jsonl      # one JSON record per line, sibling to init.json
<agent>/init.json               # canonical source for init.addons/init.mcp SHOW
```

Writers of this file are OUTSIDE this tool: the agent (`write`/`edit`) and the
boot-time addon decompression (`decompress_addons`, run by the Agent initializer,
which appends catalog entries named in init.json's `addons: [...]`, append-only
and idempotent). Identity records are read separately via `read_identities`. `mcp`
only reads, validates, and renders; `info` re-reads and re-injects on demand.

## Cross-platform invariants

Do not change any of the following; documented for reviewers only.

- **Registry path:** resolved by `_registry_path(working_dir)` in the service; the
  file sits beside `init.json` in the agent working dir.
- **Prompt injection:** `_reconcile(host)` writes the registry XML through the
  declaration-granted `host.prompt_section.write_protected_section(xml)` port,
  which is bound to `mcp` and `protected=True`; it does not receive a whole
  Agent or call `agent.update_system_prompt`.
- **Lazy import:** `src/lingtai/services/mcp_registry.py` is imported lazily
  inside `_reconcile`; `setup` only imports the production host adapter and
  hands `DECLARATION` to the kernel registrar.
- **Identity safety:** identity projection strips secret fields before they can
  reach the prompt; only allowlisted, non-secret account fields are surfaced.

## Anchored claims

| Claim | Source | Test |
|---|---|---|
| The capability renders the registry into the `mcp` prompt section | `src/lingtai/tools/mcp/__init__.py` (`_reconcile`) | `tests/test_mcp_capability.py::test_mcp_capability_renders_registry_into_prompt` |
| `info` returns a health snapshot | `src/lingtai/tools/mcp/__init__.py` (`_reconcile`) | `tests/test_mcp_capability.py::test_mcp_show_action_returns_health_snapshot`, `tests/test_tool_family_mcp_migration_parity.py::test_info_returns_health_snapshot_without_manual_body` |
| Unknown actions return a `{status: error}` dict | `src/lingtai/tools/mcp/__init__.py` (`handle_mcp`) | `tests/test_mcp_capability.py::test_mcp_show_unknown_action_returns_error`, `tests/test_tool_family_mcp_migration_parity.py::test_unknown_action_envelope_preserves_shape_with_current_actions` |
| Public name/actions and the LTP v2 envelope are exact on both wires | `src/lingtai/tools/mcp/__init__.py` (`get_schema`) | `tests/test_tool_family_mcp_migration_parity.py::test_schema_exposes_exact_public_actions_and_envelope`, `::test_schema_survives_chat_and_responses_wires` |
| All actions declare a strict-empty `input`; extra input fails before handler I/O | `src/lingtai/tools/mcp/__init__.py` (`_EMPTY_INPUT`, `_build_family`) | `tests/test_tool_family_mcp_migration_parity.py::test_all_actions_declare_canonical_strict_empty_input`, `::test_extra_input_field_is_rejected_before_any_io`, `::test_schema_only_and_dispatching_families_declare_identical_children` |
| `manual` returns the exact body/path with no registry rescan and no double wrap | `src/lingtai/tools/mcp/__init__.py` (`_flatten_manual_result`) | `tests/test_tool_family_mcp_migration_parity.py::test_manual_returns_exact_body_and_path`, `::test_manual_performs_no_registry_rescan_or_mutation`, `::test_manual_result_is_not_double_wrapped` |
| `settings` returns only the two MCP-owned rows, fully redacts `init.mcp`, and writes nothing | `src/lingtai/tools/mcp/settings.py` | `tests/test_mcp_settings.py` |
| init.json `addons: [...]` triggers append-only decompression | `src/lingtai/services/mcp_registry.py` | `tests/test_mcp_capability.py::test_addons_list_triggers_decompression`, `::test_decompress_is_idempotent` |
| Duplicate / invalid registry lines are dropped | `src/lingtai/services/mcp_registry.py` | `tests/test_mcp_capability.py::test_registry_drops_duplicates_by_name`, `::test_registry_drops_invalid_lines` |
| `mcp` is declared statically, with a reserved official name and only two required host ports | `src/lingtai/tools/mcp/__init__.py` (`DECLARATION`) | `tests/test_tool_plugin_declaration.py::test_mcp_declaration_is_static_and_needs_no_agent`, `::test_mcp_is_reserved_and_declares_only_the_ports_it_consumes` |
| Boot claims the official name and mounts exactly one `mcp` tool; a foreign declaration cannot take it | `src/lingtai/tools/mcp/__init__.py` (`setup`) | `tests/test_tool_plugin_declaration.py::test_boot_claims_the_official_name_and_mounts_exactly_one_mcp_tool`, `::test_a_foreign_declaration_cannot_take_the_live_mcp_name` |
| The family never receives the `Agent`; binding alone activates and mounts nothing | `src/lingtai/tools/mcp/__init__.py` (`_bind`, `_reconcile`) | `tests/test_tool_plugin_declaration.py::test_host_facade_and_bound_plugin_never_expose_the_agent`, `::test_bind_alone_activates_nothing_and_mounts_nothing` |
| Identity is attached only when present and secrets are stripped | `src/lingtai/tools/mcp/__init__.py` (`_registered_entry`) / service | `tests/test_mcp_identity_discovery.py::test_show_action_includes_identity_when_present`, `::test_secret_fields_are_stripped_from_accounts` |

## Verification matrix

| Invariant | Automated test | Manual check | Risk if broken |
|---|---|---|---|
| Registry renders into the prompt | `tests/test_mcp_capability.py::test_mcp_capability_renders_registry_into_prompt` | Add a registry line, inspect the `mcp` prompt section | Registered MCPs invisible to the model |
| Tool is read-only (no mutation) | `tests/test_mcp_capability.py::test_mcp_show_action_returns_health_snapshot` | Call `info`, confirm `mcp_registry.jsonl` unchanged | Signpost promise violated; surprise mutations |
| Unknown actions handled | `tests/test_mcp_capability.py::test_mcp_show_unknown_action_returns_error` | Call `mcp(action="foo")` | Silent mis-dispatch |
| Public action order is exact | `tests/test_tool_family_mcp_migration_parity.py::test_real_agent_registers_exactly_one_public_mcp_tool` | Inspect the composed `mcp` schema's `action.enum` | Duplicate, misplaced, or renamed model-facing actions |
| Invalid input fails before any registry/manual I/O | `tests/test_tool_family_mcp_migration_parity.py::test_extra_input_field_is_rejected_before_any_io` | Call `mcp` with a bogus `input` field, confirm no registry read | Signpost promise violated; wasted or surprising I/O |
| Settings is five-field SHOW only | `tests/test_mcp_settings.py` | Call settings twice around an authorized init edit; verify ordered rows and redaction | Secret/config leakage or a hidden writer |
| Addon decompression is idempotent | `tests/test_mcp_capability.py::test_decompress_is_idempotent` | Boot twice with the same `addons`, diff the registry | Duplicate registry growth |
| Secrets never reach the prompt | `tests/test_mcp_identity_discovery.py::test_secret_fields_are_stripped_from_accounts` | Add an identity with a secret field, inspect `info` output | Credential leakage into the prompt |

Run before merging:

```bash
python -m pytest tests/test_mcp_settings.py tests/test_mcp_capability.py tests/test_mcp_identity_discovery.py \
  tests/test_tool_family_mcp_migration_parity.py tests/test_signpost_tool_descriptions.py \
  tests/test_tool_plugin_declaration.py -q
```

## Schema and glossary ownership

- **Canonical identifiers:** function names, JSON property names, action/enum
  values, required fields, defaults, and bounds are canonical English literals.
  The schema (`get_schema()`) and description (`get_description()`) are
  language-independent; the optional `lang` argument is accepted for source
  compatibility but ignored.
- **Provider wire:** provider adapters resolve the top-level tool description
  through `wire_tool_description`: the global `WIRE_TOOL_DESCRIPTION` pointer
  while the resident `## tools` section is opted in via
  `LINGTAI_TOOL_PROSE_SECTION_ENABLED`, otherwise the full
  `FunctionSchema.description` prose (that section is off by default, so the
  wire is where the canonical prose lands). Nested parameter descriptions are
  unchanged either way.
- **Glossary resources:** this package owns `glossary-en.md`, `glossary-zh.md`,
  and `glossary-wen.md`. Each has strict YAML frontmatter
  (`kind: tool-glossary`, `schema_version: 1`, `tool_package: tools.<pkg>`,
  `language: <lang>`). English body is empty; zh/wen bodies contain concise
  terminology mappings that quote immutable English identifiers and never offer
  localized aliases.
- **Fallback:** exact normalized language lookup, then English, then no
  appendix. Fail-closed for localized text; fail-open for tool availability.
- **Update triggers:** changing a function name, action/enum value, property
  name, or user-visible concept requires reviewing all three glossary files in
  the same PR.
- **Validation:** `python -m lingtai.tools.glossary_validator --check`.
