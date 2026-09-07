---
name: vision-contract
tool: vision
contract_version: 2
related_files:
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/ANATOMY.md
  - src/lingtai/tools/vision/BEHAVIORS.md
  - src/lingtai/tools/vision/settings.py
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/vision/manual/reference/actions.md
  - src/lingtai/tools/vision/manual/reference/backends.md
  - src/lingtai/tools/vision/manual/reference/routing.md
  - src/lingtai/tools/vision/manual/reference/settings.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/tool_family/CONTRACT.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/adapters/tool_plugin_host.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_tool_family_vision_migration.py
  - tests/test_vision_capability.py
  - tests/test_vision_settings.py
  - tests/test_inherit_fallback.py
maintenance: |
  Keep this contract aligned with the Vision declaration, owner Anatomy,
  Behaviors/LABTs, installed manual, and family-specific tests. Bump the version
  only for a repository-policy-required breaking contract change. Vision's
  schema composition and envelope dispatch build on the generic tool_family
  package; keep that link current when either boundary changes.
---
# Vision capability contract

`vision` is an always-registered, action-separated capability. It has one public
root and five canonical children: `analyze`, `check`, `list`, the opted-in
reserved `settings`, and the reserved family-owned `manual`. Direct provider
setup, unsupported routes, and request failures fail closed with sanitized
guidance; the capability never changes the active preset or automatically
invokes another provider or MCP.

## Scope and declaration

Guarded by: [VN001](BEHAVIORS.md#behavior-vn001)

The static `DECLARATION` owns the three operational actions, their input schemas,
and the Vision settings opt-in; generic composition contributes `settings`
immediately before the manual builder's final reserved `manual` child. The
public root is the strict Tool Protocol v2 envelope:

```text
action + input + reasoning + optional summarize
required: action, input, reasoning
additionalProperties: false
```

`reasoning` is invocation/audit metadata and `summarize` is host presentation
control. Neither is child input. The root exposes one strict input branch per
child and correlates each action const with that action's input before handler
I/O. Unknown actions, unknown root fields, non-object input, and cross-action
fields fail before image, provider, credential, or manual-child work.

The exact child schemas are:

- `analyze`: strict object; required `image_path: string` and
  `question: string | null`; optional `preset: string | null`; no other fields.
  Null `question` applies `Describe what you see in this image.`. A non-null
  `preset` is an explicit one-call borrow request.
- `check`: strict object; required `preset: string | null`; no other fields.
  `null` checks the default route. A string resolves an explicitly authorized
  borrowed route without sending an image.
- `list`: strict empty object (`properties: {}`, `required: []`,
  `additionalProperties: false`).
- `settings`: strict empty input; it shows the applied bind snapshot and has no
  set/reset/write form.
- `manual`: the generic strict empty manual input schema and the installed
  Vision manual body/path result.

The child names, declaration schemas, schema-only family, and bound family must
remain one source of truth. There is exactly one public model-facing `vision`
root; children do not consume separate tool slots.

## Ports and composition

Guarded by: [VN006](BEHAVIORS.md#behavior-vn006)

The binder receives only these granted host ports:

- `workdir` — the read-only granted working-directory path, used for relative
  images, preset files, local settings, and the installed manual.
- `active_provider` — a live read-through of the current service identity. It is
  consulted at bind/route time; Vision does not snapshot an Agent or retain one.
- `configuration` — one immutable `VisionConfiguration` snapshot containing the
  public setup arguments (`vision_service`, provider, `api_key`, `api_key_env`,
  and opaque provider kwargs). It travels as the kernel `ConfigurationPort`'s
  copied `values` mapping (`VisionConfiguration.port_values()` on the way in,
  `VisionConfiguration.from_port_values()` at bind; any other mapping shape is
  refused with `ToolPluginDeclarationError`). It is not an Agent and is
  interpreted only by the Vision binder.

`setup` creates that configuration and delegates `DECLARATION` to the official
registrar. The registrar owns claim/authorization/mount lifecycle; `_bind`
creates the `VisionManager` and returns the one public plugin. The serialized
host/registry/port implementation and its shared fixture are integration-owned;
this family contract only specifies the Vision-side requirements above.

## Settings discovery

Guarded by: [VN007](BEHAVIORS.md#behavior-vn007)

Vision inventories exactly these owner-route facts, in order: `provider`,
`base_url`, `model`, `api_key`, `api_key_env`, `max_tokens`, `api_compat`,
`wire_api`, `default_headers`, `token_path`, `instructions`,
`max_output_tokens`, and `timeout`. A success row is exactly `key`, `current`,
`default`, `configurable`, and `comment`; every comment is a stable
`vision-manual#setting-...` pointer.

The provider is bound to the same immutable `VisionConfiguration`, local-file
snapshot, active-provider facts, and successfully constructed service used by
the running manager. SHOW creates fresh row objects from that applied snapshot;
it does not reread files or environment, inspect provider clients, or construct
a service. A later owner-file or launcher change is prospective until the
existing refresh/relaunch bind. A failed/manual-only route, an invalid local
document, or an opaque injected `vision_service` outside the owner resolver
makes the whole inventory unavailable rather than fabricating values.

`base_url`, `api_key`, `api_key_env`, `default_headers`, `token_path`,
`instructions`, and any path-like `model` are reduced to private presence
markers before becoming `SettingRow(..., _sensitive=True)`. Generic projection
therefore replaces both current and default with `<redacted>` and never projects
the marker. Every row is `configurable: true` because an existing Vision owner
procedure can change it through capability/preset composition, the local owner
file where supported, or the launcher/secret route named by `api_key_env`; that
never grants SHOW mutation authority.

## Routing and preset authorization

Guarded by: [VN002](BEHAVIORS.md#behavior-vn002),
[VN003](BEHAVIORS.md#behavior-vn003), and
[VN006](BEHAVIORS.md#behavior-vn006)

Without a `preset`, direct routing uses the configured Vision service or the
active provider's own compatible identity. Provider/model/base URL/credential
identity is inherited only from that same active provider (including the
supported GLM/Zhipu and Codex-family aliases). `PROVIDERS["fallback_on_inherit"]`
is `None`: an unsupported or failed active route remains a manual/error result;
Vision does not silently switch to a different provider, legacy credential, or
MCP route.

With a non-null `preset`, Vision first requires the reference to be present in
`manifest.preset.allowed` and then loads it read-only. The borrowed route uses
the allowed preset's own `manifest.llm` and `manifest.capabilities.vision`
identity: its provider/model/base URL and, where declared, its own
`api_key`/`api_key_env` or Codex OAuth-pool identity. Resolving that explicitly
allowed preset credential is an intentional consequence of the caller's borrow
request; it is not an active-preset switch and is not an automatic fallback.
An unlisted, unreadable, or incomplete preset fails closed with a sanitized
manual pointer. A direct request failure likewise offers alternatives only as
explicit instructions; it does not invoke them.

`check` may construct the selected service so it can report provider/model, but
never sends an image or calls `analyze_image`. `list` mechanically reports the
active provider/model and classifies only the allowed preset definitions; it
constructs no provider service and reads no credential. `manual` reads only the
installed manual child; it constructs no service, reads no credential/configured
route, and performs no provider or image operation.

## Provider and wire boundaries

Codex spellings (`codex`, `codex-pool`, `codex_pool`) are one family gate; spelling
does not choose direct versus pool. The active Codex default bucket chooses the
route: a nonblank trimmed `codex_auth_path` is direct, otherwise the active
Codex pool selects its current OAuth identity. An unrelated active provider may
not lend its model, endpoint, or credential to a Codex request. Claude-family
vision is manual-only guidance to the explicit Claude CLI. OpenAI-compatible
routes preserve their current endpoint/model/wire, and unsupported wires remain
manual-only. Local vision requires an explicit model and uses its operator-owned
settings/manifest values; no hidden model or credential default is invented.

## Results, errors, and state

Guarded by: [VN002](BEHAVIORS.md#behavior-vn002),
[VN003](BEHAVIORS.md#behavior-vn003),
[VN004](BEHAVIORS.md#behavior-vn004), and
[VN005](BEHAVIORS.md#behavior-vn005)

- `analyze` success is exactly `{"status": "ok", "analysis": text}`.
  Missing image, empty response, setup failure, request failure, and denied
  preset are structured errors. Provider exception contents, credentials, and
  unsanitized endpoints never appear in a result.
- `check` success is exactly `{"status": "ok", "route": route,
  "provider": provider, "model": model}`. It reports the default or explicitly
  borrowed route and never performs an image request.
- `list` success is `{"status": "ok", "default": default,
  "presets": presets, "count": count}`. Each preset entry is derived only from
  an allowed preset declaration and contains route identity/classification, not
  credentials.
- `manual` success is exactly `{"status": "ok", "action": "manual",
  "manual": body, "manual_path": path}`. A missing installed manual is
  `degraded` with an empty body, truthful host-local path, and loader error.
  The canonical manual child result is flattened once by the host; it is never
  nested or double-wrapped.
- `settings` success is `{"settings": [...]}` with only the five projected row
  fields. Non-empty input fails before provider invocation. Unavailable,
  malformed, or non-JSON truth yields the generic fixed no-row failure, and the
  complete response is incrementally bounded to 65,536 UTF-8 bytes.

## Invariants and evidence

- [VN001](BEHAVIORS.md#behavior-vn001) guards the five-action declaration,
  strict branches, action/input correlation, and pre-handler rejection:
  `tests/test_tool_family_vision_migration.py`.
- [VN002](BEHAVIORS.md#behavior-vn002) guards analyze success/failure shapes,
  image-path handling, same-provider identity, and no automatic fallback:
  `tests/test_tool_family_vision_migration.py` and
  `tests/test_vision_capability.py`.
- [VN003](BEHAVIORS.md#behavior-vn003) guards default/borrowed checks, denied
  preset references, and no image/provider call: the migration test file.
- [VN004](BEHAVIORS.md#behavior-vn004) guards mechanical list enumeration and
  no service/credential construction: the migration test file.
- [VN005](BEHAVIORS.md#behavior-vn005) guards installed manual body/path,
  degraded loading, no config/provider reads, and single host adaptation: the
  migration test file.
- [VN006](BEHAVIORS.md#behavior-vn006) guards the active-provider/configuration
  port seam and allowed-preset credential identity: declaration and migration
  tests, plus serialized shared-host tests at integration time.
- [VN007](BEHAVIORS.md#behavior-vn007) guards exact settings order and field
  order, effective current/default values, configurable semantics, stable manual
  anchors, complete redaction, no-write behavior, whole-inventory failure, and
  ordinary analyze non-regression: `tests/test_vision_settings.py` plus the
  shared generic settings contract test.

Run the focused Vision capability, service, migration, preset-routing, and
manual-contract tests with bytecode and pytest cache disabled; run the shared
manual fixture and declaration tests only after the serialized host integration.
