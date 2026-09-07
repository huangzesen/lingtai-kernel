---
related_files:
  - src/lingtai/tools/ANATOMY.md
  - src/lingtai/tools/vision/BEHAVIORS.md
  - src/lingtai/tools/vision/__init__.py
  - src/lingtai/tools/vision/CONTRACT.md
  - src/lingtai/tools/vision/glossary-en.md
  - src/lingtai/tools/vision/glossary-zh.md
  - src/lingtai/tools/vision/glossary-wen.md
  - src/lingtai/tools/vision/manual/SKILL.md
  - src/lingtai/tools/vision/manual/reference/actions.md
  - src/lingtai/tools/vision/manual/reference/backends.md
  - src/lingtai/tools/vision/manual/reference/routing.md
  - src/lingtai/tools/vision/manual/reference/settings.md
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/services/vision/ANATOMY.md
  - src/lingtai/tools/vision/settings.py
  - src/lingtai/kernel/tool_plugin/ANATOMY.md
  - src/lingtai/adapters/tool_plugin_host.py
  - tests/test_tool_plugin_declaration.py
  - tests/test_vision_settings.py
maintenance: |
  Keep related_files as repo-relative paths to real files and keep anatomy links
  reciprocal. Update citations with structural code changes and run the document
  validators after edits. tool_family is generic optional infrastructure this
  package composes onto; Vision owns its provider routing, action results, and
  preset authorization boundary.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# src/lingtai/tools/vision/

The `vision` package is one declaration-derived public family with five model-facing
children: `analyze`, `check`, `list`, the opted-in reserved `settings`, and the
reserved `manual`. The generic
`tool_family` package owns schema composition and envelope dispatch; this package
owns provider identity, allowed-preset borrowing, route classification, and every
Vision result shape.

## Components

- `__init__.py:197-212` — `_canonical_preset_path` resolves one allowed preset
  reference (`~`, absolute, or workdir-relative spelling) to its canonical
  physical path; `list` keys its rows on it so one file appears once.
- `__init__.py:540-584` — strict declaration-owned input schemas: `analyze`
  requires `image_path` and nullable `question` and accepts nullable `preset`,
  `check` requires nullable `preset`, and `list` is strict empty input.
- `__init__.py:588-685` — immutable `VisionConfiguration` (with
  `port_values`/`from_port_values`, the only translation to and from the kernel
  `ConfigurationPort` mapping), static description, and declaration-derived
  `_build_family`; import-time and host-bound families therefore expose the
  same three operational children plus generic `settings` and `manual`.
- `__init__.py:698-721` — `VisionManager` retains only the granted workdir and
  live active-provider ports, the resolved service/reason, and the installed
  manual child; it does not retain an Agent.
- `__init__.py:727-808` — `_build_service_from_preset` checks
  `manifest.preset.allowed`, loads the authorized preset read-only, and passes
  that preset's provider/model/credential identity to the direct resolver.
- `__init__.py:810-897` — `_dispatch_analyze` resolves relative image paths,
  performs one request on either the default or explicitly borrowed service, and
  returns the exact success/error shapes.
- `__init__.py:899-946` — `_dispatch_check` constructs/resolves the selected route
  and reports provider/model without sending an image request.
- `__init__.py:948-1001` — `_dispatch_list` mechanically classifies the active route
  and only the authorized preset definitions, one row per physical preset in its
  declared spelling; it constructs no provider service.
- `__init__.py:1017-1024` — `manual` reads the installed package manual through the
  reserved child, then the host flattens its canonical body/path result once.
  `manual/SKILL.md` is the short operational router; its `reference/actions.md`,
  `routing.md`, `settings.md`, and `backends.md` children own the progressively
  disclosed action, route, setting, and backend depth.
- `__init__.py:144-526` and `1162-1543` — owner-local settings
  snapshot/projection turns the successfully bound route and resolver-produced
  protocol provenance into 13 exact `SettingRow` values. Sensitive inputs,
  including path-like models, become presence markers before the generic
  redaction boundary; an unavailable route raises into the generic
  all-or-nothing failure.
- `__init__.py:1056-1153` and `1546-1588` — `_bind`, `DECLARATION`, and `setup`
  compose Vision through the official registrar with `workdir`,
  `active_provider`, and opaque `configuration` ports: `setup` hands the
  registrar `StaticConfigurationAdapter(VisionConfiguration(...).port_values())`
  through `extra_ports_for` for the `vision` declaration alone, and `_bind`
  rebuilds the snapshot with `VisionConfiguration.from_port_values`, resolves the
  service once, and binds one read-only settings provider.
- `settings.py:1-188` — bounded, stable, duplicate/unknown-field-rejecting
  `settings/vision.json` reader retained for the local route; it has no writer.

## Connections

- Schema composition, strict action/input correlation, and canonical manual-child
  loading descend through [`src/lingtai/tools/tool_family/ANATOMY.md`](../tool_family/ANATOMY.md).
- `_bind` receives the host's live active-provider read-through and one immutable
  `VisionConfiguration` snapshot; it never reaches through to an Agent.
- Direct routes call the service implementations under `lingtai.services.vision`.
  Provider aliases and Codex route selection stay inside this family boundary.
- Generic settings input enforcement, exact five-field projection, redaction,
  failure, and response bounding stay in `tool_family`; Vision supplies only
  the applied bind snapshot and stable owner-manual pointers.
- Preset borrowing reads only an explicitly authorized preset and uses that
  preset's own provider/model/credential route for the one requested call. It
  does not switch the active preset and does not invoke MCP automatically.
- The reserved `manual` child reads the installed `capabilities/vision/SKILL.md`;
  `manual_path` is therefore host-local and truthful.

## Composition

`setup` creates `VisionConfiguration` and delegates one official declaration to the
kernel registrar. The registrar claims and mounts exactly one public `vision` root;
`_bind` creates `VisionManager`, its bound settings provider, and its
declaration-derived family. The package
manual is the operational source, while the retained service package supplies
provider adapters and this package supplies routing policy.

## State

The manager retains ephemeral service, route reason, workdir, active-provider port,
family references, and one closure over the applied settings snapshot. SHOW writes
nothing and never rereads the owner file. Preset files and local settings remain
owner-managed inputs to the existing bind/refresh path. Vision analyses are not
persisted; manual content is bundled and installed into the agent's intrinsic
capability library.

## Notes

Default provider failures and unsupported routes fail closed with sanitized guidance;
there is no automatic provider or MCP fallback. A caller may explicitly request an
authorized preset on `analyze` or `check`. Manual and list do not construct a
provider or read a credential; check may construct the selected route but never
sends an image request.
