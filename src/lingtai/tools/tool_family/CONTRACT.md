---
name: tool-family
contract_version: 4
root_contract: CONTRACT.md
related_files:
  - src/lingtai/tools/tool_family/ANATOMY.md
  - src/lingtai/tools/tool_family/BEHAVIORS.md
  - src/lingtai/tools/tool_family/__init__.py
  - src/lingtai/tools/tool_family/settings.py
  - src/lingtai/intrinsic_skills/system-manual/reference/tool-plugin-settings/SKILL.md
  - src/lingtai/tools/tool_family/manual.py
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/psyche/CONTRACT.md
  - src/lingtai/tools/_manual.py
  - src/lingtai/tools/web_search/CONTRACT.md
  - src/lingtai/tools/web_search/__init__.py
  - src/lingtai/tools/mcp/CONTRACT.md
  - src/lingtai/tools/mcp/__init__.py
  - src/lingtai/tools/plugin/CONTRACT.md
  - src/lingtai/tools/plugin/__init__.py
  - src/lingtai/tools/avatar/CONTRACT.md
  - src/lingtai/tools/avatar/__init__.py
  - src/lingtai/tools/avatar/settings.py
  - src/lingtai/tools/soul/CONTRACT.md
  - src/lingtai/tools/soul/__init__.py
  - src/lingtai/tools/skills/CONTRACT.md
  - src/lingtai/tools/skills/__init__.py
  - src/lingtai/tools/notification/CONTRACT.md
  - src/lingtai/tools/system/CONTRACT.md
  - src/lingtai/tools/daemon/CONTRACT.md
  - src/lingtai/tools/daemon/_tool_family.py
  - src/lingtai/tools/email/CONTRACT.md
  - src/lingtai/tools/email/__init__.py
  - src/lingtai/tools/context/CONTRACT.md
  - src/lingtai/tools/pad/CONTRACT.md
  - src/lingtai/tools/lingtai/CONTRACT.md
  - tests/test_tool_family_generic.py
  - tests/test_tool_settings_contract.py
  - tests/test_tool_family_wire_parity.py
  - tests/test_tool_family_manual_contract.py
  - tests/test_tool_family_system_migration.py
  - tests/test_tool_family_daemon_migration.py
maintenance: |
  This component contract is governed by the root CONTRACT.md. Keep
  related_files complete and repo-relative, including the paired ANATOMY.md,
  the ChildTool/ToolFamily Port, the web_search production Adapter, contract
  tests, and the ManualTool manual reference. Update this contract, the
  paired Anatomy, and affected families together when the envelope, schema
  composition, or dispatch boundary changes. Adding a family MAY adopt this
  package's ToolFamily/handle(); it MUST NOT be required to, per
  src/lingtai/tools/CONTRACT.md "Implementation independence".
---
# ToolFamily generic infrastructure

## Purpose

Generic, optional internal infrastructure implementing the LingTai Tool
Protocol v2 envelope (`../CONTRACT.md`) so a family need not hand-write its
own schema composition and dispatch-validation boilerplate. `ChildTool` +
`ToolFamily` compose one model-facing aggregate tool from a fixed registry of
internal, MCP-compatible-in-shape children; `manual.py` upgrades the existing
`#1058` `load_installed_manual()` return shape into the reusable ManualTool
stable contract. **Root acceptance requirement:** the ManualTool child's
actual, dispatched-to result (what `ToolFamily.handle()` returns verbatim for
`action="manual"`, per the no-double-wrap rule below) MUST carry the full
installed `SKILL.md` body at `content[0].text` and the model-visible
host-local path at `structuredContent.manual_path` — never only in an unused
presentational helper, and never only in a `_meta`-style side channel. A
family wanting a different public result shape (as `web` does — see
Adapters) owns that adaptation itself, in its own Host/presentation layer,
after dispatch. This package owns no transport, no external MCP surface, and
no second registry: it is a Host-process-internal composition helper only.

## Behavior

A `ToolFamily` is constructed from an ordered, fixed list of `ChildTool`
descriptors. Construction is where correctness is enforced: a duplicate child
name, or more than one child named the reserved `manual`, raises
`ToolFamilyError` immediately rather than registering silently. Once
constructed, `build_schema()` deterministically composes the model-facing
schema with two enforcement layers correlating `action` with `input`,
generated purely from the child registry (no name/schema mapping table),
both built from the same deep-copied canonical child schemas:

1. **Schema-level (`allOf`):** one `if`/`then` condition per child — each
   `if` tests root `action` via `const` against that child's own registry
   name (guarded by `required: ["action"]`); each `then` constrains root
   `input` to that exact child's canonical `input_schema`. Adopted after a
   live non-strict Codex Responses probe on 2026-07-27 accepted a raw root
   `allOf`/`if`/`then` schema without error on the current route (see
   `_scrub_responses_schema` in `../../llm/openai/adapter.py` for the
   corresponding wire-level change and its own scope note).
2. **Typed `input.anyOf` disclosure:** `input` explicitly declares the common
   `type: object` constraint required of every action, then embeds the same
   per-child `input_schema`s verbatim under a `title` for model discoverability
   of every action's exact shape in one place. `anyOf` is intentional: titles
   and descriptions are annotations, not validation discriminators, so two
   actions may have validation-equivalent input schemas. The direct type is
   redundant for a complete JSON Schema validator, but keeps the object
   contract unambiguous for model/provider schema consumers that inspect only
   the immediate node.

Both layers expose the envelope root `action`, `input`, required
`reasoning`, and optional `summarize` — exactly the four public fields;
`allOf` constrains them without adding a fifth field or duplicating `action`
inside `input`. Dispatch (`handle()`, below) remains the second,
always-authoritative enforcement layer regardless of whether a given
provider actually validates `allOf`/`if`/`then` schema-side before
invocation — it is additive, not a replacement.
`reasoning` is Host InvocationContext/audit metadata, so `build_schema()`
declares it itself (same property text Agent schema composition also
re-injects into every tool's `properties` uniformly, but that step never
touches `required` — a family must declare `reasoning` required itself to be
correct even before Agent composition runs). `build_schema()` always
advertises `summarize` to the model regardless of family; whether the kernel
actually honors it is a separate, per-family allowlist decision
(`kernel/tool_result_summary.py` `_LTP_V2_MIGRATED_FAMILIES`) that this
package does not own or enforce. Today `web`, `mcp`, `knowledge`, `file`,
`vision`, `avatar`, `soul`, `shell`, `skills`, `notification`, `system`, and
`daemon` are on that allowlist, so `summarize` is
meaningful for the families that use this infrastructure; a family adopting
`ToolFamily` without also joining the kernel allowlist would advertise a
model-visible `summarize` control that the kernel silently ignores —
joining it is part of a migration, not something this package does on a
family's behalf. Calling
`handle()` is optional: it validates the envelope (unknown `action`,
non-boolean `summarize`, unknown root fields — with one narrow relocation
exception, see "Contract rules" below — `input` keys outside the
selected child's own declared schema) before invoking exactly that child's
handler with only its own `input` mapping. A family MAY skip `handle()`
entirely and dispatch by hand — `web` uses it internally but still owns its
own outer `handle()` to stamp family-specific diagnostics onto envelope
failures, which this package has no knowledge of.

`build_manual_child` builds the reserved `manual` `ChildTool`: strict empty
input — the module-level `MANUAL_INPUT_SCHEMA` literal, exported so a family
that also composes a schema-only `ToolFamily` advertises the identical object
rather than a hand-copied near-duplicate (`soul` does; `web` predates the
export and still declares a local copy — collapsing that is `web`'s owner's
call, not a conformance failure), and so a family supplying its own
`manual` handler entirely (as `avatar` does) can reference the same literal
instead of restating it, keeping the two from drifting apart; its handler
loads the existing `load_installed_manual()` shape
(`status`, `manual` full body, `manual_path`, optionally `error`) and maps it
to the canonical, actually-dispatched result: `content=[{"type": "text",
"text": <full body>}]` and `structuredContent={"manual_path": <path>}`, with
`status`/`error` preserved verbatim as truthful loader facts. This mapping is
not a second wrapper — it IS this child's own canonical result. A family
MUST register this `ChildTool` directly, unwrapped, in its own `ToolFamily`
(see Adapters): `ToolFamily.handle()` then returns it verbatim for
`action="manual"`, and any family-specific public shape adaptation happens
strictly after that call returns, in the family's own Host/presentation
layer — never inside this builder, its handler, or a wrapping `ChildTool`.

## Port

### Optional settings provider

The optional `SettingsProvider` callable returns fresh `SettingRow` display
facts for the injected read-only action; the [owner manual](../../intrinsic_skills/system-manual/reference/tool-plugin-settings/SKILL.md)
teaches the seam, and [T011](BEHAVIORS.md#behavior-t011) guards it.
Every successful row contains exactly `key`, `current`, `default`,
`configurable`, and `comment`; `comment` is the exact owner-manual section
pointer where all other setting detail and change procedure live. A provider
raises rather than returning a row when current truth is unavailable.

The provider-neutral boundary is `ChildTool.input_schema` (each child's own
canonical JSON Schema for `input`) and `ChildTool.handler`
(`Callable[[Mapping], dict]`, receiving only validated `input`). `ToolFamily`
composes these into one `FunctionSchema.parameters`-compatible dict via
`build_schema()` and, optionally, dispatches through `handle()`. Neither
method is a required interface a family must implement against — a
conforming family may satisfy the same wire shape without ever importing this
package, per `../CONTRACT.md` "Implementation independence".

`ChildTool.diagnostics` is a third, optional Port surface: a passive,
non-wire sidecar mapping a structural trigger name to the static
`DiagnosticDescriptor` (`code`, `expected_form`, `reason`, `fix`) that action
owns for it. It never participates in `build_schema()` and is read only by
`handle()`.

## Diagnostics sidecar

Guarded by: [T010](BEHAVIORS.md#behavior-t010).

Compiler-style, agent-visible hints for a failed tool call are declared
**adjacent to the relevant action's own definition**, not centralized in this
generic package, and rendered generically. Today's one structural trigger is
`TRIGGER_UNSUPPORTED_INPUT_FIELD`: a selected action's own `input` carrying a
key outside its declared schema `properties` (cross-action or wholly
foreign) — the case `handle()` was already rejecting fail-closed as
`INVALID_ARGUMENT: unsupported <family> input field` before any handler I/O,
per "Contract rules" below.

- A `ChildTool` opts in by declaring `diagnostics={TRIGGER_UNSUPPORTED_INPUT_FIELD:
  <DiagnosticDescriptor>}`. Declining (the default, `diagnostics=None` or no
  entry for a trigger) yields the exact pre-existing legacy `status`/
  `error_code`/`message` failure result for that trigger — unchanged,
  byte-for-byte.
- `DiagnosticDescriptor` is a frozen, fully static value: `code`,
  `expected_form`, `reason`, and `fix` are fixed strings the owning action's
  author writes once, next to that action's own `input_schema`. This package
  never inspects/parses prose, never guesses a tool-specific reason, and
  keeps no central tool-name/message table mapping actions to text — the
  owning action supplies the text; the generic dispatcher supplies only
  structure.
- On a recognized trigger for an opted-in child, `handle()` computes a
  **structural fact and location only** — `<family>/<child>/input.<field>` —
  and pairs it with the child's own descriptor text verbatim, added as an
  **additive** `diagnostics: [...]` array alongside the unchanged legacy
  three-key failure result. One entry is emitted per foreign field.
- A field *label* is only ever surfaced when it is conventional-identifier-
  shaped and carries no secret-shaped substring (`_is_safe_field_label`); an
  unsafe-shaped or non-identifier label is silently dropped from the
  `diagnostics` array rather than surfaced, and if every candidate field is
  dropped this way, no `diagnostics` key is added at all — the legacy result
  is untouched. This package never emits a raw rejected value, a raw path, an
  argument, an exception string, or a JSON blob in a diagnostic; only the
  vetted field label, structural location, and the descriptor's own static
  text.
- The sidecar is passive and non-wire by construction: `diagnostics` is never
  read by `build_schema()`, so it can never reach a provider's Chat
  Completions or Responses tool schema. It performs no I/O and changes no
  control flow — the fail-closed rejection order, the "no handler I/O for a
  cross-action/unknown `input` key" guarantee, and every other envelope rule
  below are unaffected; this is presentation enrichment of an
  already-computed rejection, not a new enforcement path.
- `context.molt` (`../context/CONTRACT.md`) is the first concrete
  declaration: it owns a `DiagnosticDescriptor` for
  `TRIGGER_UNSUPPORTED_INPUT_FIELD` stating the allowed `input` field set
  (`summary`, `session_journal_path`, `keep_tool_calls`, `keep_last`) and
  that molt rejects foreign action input before it can shed context. It does
  not, and must not, claim `session_journal_path` must be relative — the
  existing in-workdir-absolute-normalizes-to-relative policy for that field
  is unrelated and unchanged by this sidecar.

## Adapters

`web_search/__init__.py` is the first production Adapter/consumer:
`WebManager.__init__` builds a per-instance `ToolFamily` with
`search`/`browse` handlers bound to that instance, and registers
`manual.build_manual_child(agent, "web")`'s returned `ChildTool` *directly* —
unwrapped — as the family's `manual` child. `WebManager.handle()` calls
`self._family.handle(args)`, which therefore returns that child's canonical
`content`/`structuredContent` result verbatim for `action="manual"` (no
double wrap). Strictly *after* that call returns, `handle()` detects a
successfully dispatched manual result (`"content" in result`) and calls
`self._adapt_manual_result(result)` — a Host/presentation-only method that
flattens the canonical result to Web's pre-migration public shape (`status`,
`manual`, `manual_path`, `action`, `current_setting`), preserving the
`#1058` public result exactly. This adaptation belongs to `web`'s own
`handle()`, not to the generic child or any wrapper registered in place of
it. `WebManager.handle()` also stamps `current_setting` onto any
envelope-level failure result (search/browse/unknown-action) before
returning, unchanged from before.

`mcp/__init__.py` (`../mcp/CONTRACT.md`) is the second production Adapter and
the minimal shape of one: a two-child family (`info`, `manual`) keeping its
exact public tool name and action values, where both children declare the
canonical strict-empty `input`. It registers `build_manual_child(agent,
"mcp")` directly and unwrapped, and flattens the canonical child result to
`mcp`'s own `status`/`mcp_manual`/`manual_path` public shape post-dispatch in
`_flatten_manual_result`. It also establishes how a consumer keeps a
pre-migration public *error* envelope that this package's dispatcher does not
reproduce: `handle_mcp` renders `mcp`'s exact unknown-action envelope itself,
before delegating, covering the missing-action empty-string default and
unhashable `action` values (`[]`/`{}` from invalid JSON) that
`ToolFamily.handle`'s `action not in self._children` dict lookup would raise
`TypeError` on. It routes on `child_names`, the public ordered tuple, whose
`in` compares by `==` and never hashes — so the unhashable case is handled by
construction, with no exception handler needed. Per "Implementation
independence", the fix belongs in the consumer; this package's canonical
`ACTION_REQUIRED` shape is never widened to accommodate one family's legacy
envelope.

`knowledge/__init__.py` is the third production Adapter/consumer:
one `_build_family(agent | None)` registers `info` and `manual` children from a
single `_CHILD_SPECS` source, both with the canonical strict-empty
`input_schema`; passing `None` yields the module-level schema-only family
behind `get_schema()`. It does **not** use
`manual.build_manual_child`, because knowledge's public manual result has
always been keyed `knowledge_manual` rather than the generic
`content`/`structuredContent` shape; registering its own `manual` child means
`ToolFamily.handle()` returns that family's canonical result verbatim with no
double wrap and no round-trip through a shape it never exposes — the same
no-double-wrap rule, satisfied without a Host adapter. Its outer `handle()`
normalizes only the generic `ACTION_REQUIRED` envelope failure back to
knowledge's exact pre-migration unknown-action result.

`avatar/__init__.py` is the fourth production Adapter/consumer to touch this
contract (after `file` and `vision`, which adopt this package per
`../CONTRACT.md` without a dedicated Adapter paragraph here):
`AvatarManager.__init__` builds a per-instance `ToolFamily` with a `spawn`
handler bound to that instance (the former `rules` handler was removed, not
relocated — avatar CONTRACT.md contract_version 9), the generic `settings`
child bound to the static no-I/O `AvatarSettingsProvider`, and Avatar's local
`manual` handler. `settings` is injected immediately before `manual`, and
`AvatarManager.handle()` calls `self._family.handle(args)`. It is a deliberate
**partial** adoption, which this package permits: `avatar` reuses `ChildTool`
and `ToolFamily` but *not* `build_manual_child`, because its manual ships inside
its own package (`avatar/manual/SKILL.md`) rather than the agent's installed
`.library` intrinsic catalog — `build_manual_child` would report a `.library`
`manual_path` that family never reads. Its `manual` child is therefore its own
`ChildTool` returning `avatar`'s own canonical flat result (`status`, `action`,
`manual`, `manual_path`), which `ToolFamily.handle()` returns verbatim with no
double wrap and no post-dispatch adaptation. Strictly *after* dispatch,
`AvatarManager.handle()` normalizes this package's generic `ACTION_REQUIRED`
envelope failure back to avatar's own pinned unknown-action error string — the
same Host/presentation-layer ownership boundary `web` uses for
`current_setting`, and never a change to this package's canonical error shape.
`avatar` also threads its root `_reasoning` (the spawn mission brief) to its
`spawn` handler out-of-band, because this package correctly refuses to pass any
envelope field to a child.

`soul/__init__.py` is a declared-host-plugin consumer and the first intrinsic
in this composition account. Its production binder `_bind(host)` grants the
five operational children only `host.soul_runtime` (`SoulRuntimePort`) and
passes `host.workdir` to the reserved `manual` child via
`build_manual_child(host.workdir, DECLARATION.manual)`. `DECLARATION` owns the
child registry and input schemas used by both the schema-only family and bound
dispatch, so duplicate or reserved child names fail loudly and cannot be
resolved by scan order.

Whole-Agent `handle(agent, args)` and `_coerce_runtime()` are compatibility-only
at Soul's package root for kernel lifecycle and legacy callers. They are not the
production composition boundary. The post-dispatch `_adapt_manual_result`
intentionally restores Soul's historical flat `status`/`manual`/`manual_path`
shape; the operational children remain bound to `SoulRuntimePort`. Soul drops
the kernel-injected `_tc_id` at this root compatibility boundary, rather than
widening the shared envelope or passing transport metadata to a child.

`skills/__init__.py` (`../skills/CONTRACT.md`) is the sixth production
Adapter/consumer. One `_build_family(agent, paths)` builder is its single
canonical child registry, registering an `info` child and
`manual.build_manual_child(agent, "skills")` directly — unwrapped; both
`get_schema()` (through an import-time `agent=None` instance whose handlers are
unreachable) and `setup()` obtain their `ToolFamily` from that one builder, so
the composed schema advertises exactly the child `input_schema`s dispatch
registers. Its `handle_skills` wrapper adapts only a successfully
dispatched manual result (`"content" in result`) to that capability's public
`skills_manual`/`library_manual`/`manual_path` shape, post-dispatch. Unlike
`web`, it returns this package's canonical envelope-failure result verbatim,
having no family-specific diagnostic block to stamp on; both of its children
declare the canonical strict-empty `input_schema`, so `handle()`'s
allowed-key check rejects every `input` key on either action.

`system/__init__.py` (`../system/CONTRACT.md`) is the seventh production
Adapter/consumer named here, and the third that is an intrinsic. It follows
`soul`'s module-level composition shape exactly — a module-level schema-only
`ToolFamily` behind `get_schema()` whose import-time construction is the
registry's duplicate/reserved-name collision check, an agent-bound family built
per `handle(agent, args)` call, `build_manual_child(agent, "system-manual")`
registered directly and unwrapped with a post-dispatch `_adapt_manual_result`
flattening to the family's pinned flat `status`/`manual`/`manual_path` shape,
the kernel-injected `_tc_id` dropped at its own Host boundary, and the generic
`ACTION_REQUIRED` failure normalized back to its pinned unknown-action string.
It is this package's largest consumer at eleven children, and the one where the
allowed-key check carries the most weight: `system`'s privilege classes are
per action, so rejecting an `input` key outside the selected child's own schema
is what stops a smuggled `address` on a non-karma action from reaching a
lifecycle handler at all.

`daemon/_tool_family.py` (`../daemon/CONTRACT.md`) is the eighth production
Adapter/consumer named here, and the largest-engine one: it repeats the
`shell` division — a dedicated `_tool_family.py` module owning the package's
single public `get_schema`/`get_description` pair and a
`DaemonFamilyDispatcher` that translates one envelope call into
`DaemonManager.handle()`'s unchanged legacy flat shape — so the ~13k-line
engine (batch emanation, backend routing, run directories, the detached
supervisor, completion signaling, cancellation, timeouts, terminal
notifications) and every pre-migration suite exercising it stay untouched. It
is the first consumer whose child `input_schema` carries a deep nested
structure: `emanate`'s `tasks[]` items keep their full eight-property task
object (including the open-ended `backend_options` argv passthrough)
byte-for-byte, and that object is deliberately left *open* — the engine's own
strict per-task validation owns that boundary and returns domain-specific
errors a schema rejection would replace with a generic one, which this package
neither requires nor prevents. It registers `build_manual_child(agent,
"daemon")` directly and unwrapped and returns that canonical result verbatim,
with no post-dispatch adaptation; the engine's own retained flat
`action="manual"` branch is internal-only and never the model-facing path. Its
one Host normalization is narrowing the generic `ACTION_REQUIRED` message to
daemon's exact six actions, the same boundary `shell` uses. Because it also
replaces a pre-migration flat `summary` boolean with the canonical root
`summarize`, its migration joins `kernel/tool_result_summary.py`'s
`_LTP_V2_MIGRATED_FAMILIES` in the same change — the allowlist step this
contract notes is part of a migration, not something this package does on a
family's behalf.
`email/__init__.py` (`../email/CONTRACT.md`) is the ninth production
Adapter/consumer, and the largest child registry this package composes: one
`_build_family(agent)` registers fourteen children — thirteen action children
whose handlers re-enter the unchanged `EmailManager.handle`, plus
`manual.build_manual_child(agent, "email")` directly and unwrapped — while an
import-time `_schema_only_family()` (whose handlers are unreachable) backs
`get_schema()`, the same module-level shape `soul` and `notification`
established for an intrinsic. Both come from one `ACTION_ORDER`/
`INPUT_SCHEMAS` registry in `../email/_family_schema.py`, so the composed
schema advertises exactly the children dispatch registers. Its
`manual` child declares this package's exported `MANUAL_INPUT_SCHEMA` literal
rather than a local copy, and `email`'s own `handle()` flattens the canonical
manual result to that family's pinned `status`/`manual`/`manual_path` shape
strictly post-dispatch. It also shows a Host boundary the earlier consumers
did not need: a **reserved non-public action**. `email(action='unread')` is
kernel-synthesized digest state, deliberately absent from the child registry,
and its exact pre-migration rejection is rendered by `email`'s own `handle()`
*before* delegating — the generic `ACTION_REQUIRED` envelope is never widened
to carry a family's reserved-name semantics. `email` additionally restores its
own unknown- versus absent-action results (`"Unknown email action: <x>"` vs
`"action is required"`), which this package's single envelope failure
deliberately collapses.

`context/__init__.py` (`../context/CONTRACT.md`) follows the intrinsic
module-level composition shape: a schema-only `ToolFamily` at import time
(which is also the registry collision check) and an agent-bound one per
`handle(agent, args)` call, both from one `_CHILD_SPECS` source. It registers
`build_manual_child(agent, "context-manual")` directly and unwrapped. It is
also the first concrete declaration of the "Diagnostics sidecar" above:
`molt`'s `ChildTool` carries a `diagnostics={TRIGGER_UNSUPPORTED_INPUT_FIELD:
...}` entry stating molt's own allowed `input` field set and refusal reason,
declared adjacent to `_MOLT_INPUT_SCHEMA` in `context/__init__.py`; the
sibling `summarize`, `rebuild`, and `manual` children declare none, so a
foreign `input` key on those still renders the exact legacy failure.

It also exercises a boundary the earlier intrinsics could only half-prove.
`soul`, `notification`, `system`, and `email` merely *drop* the kernel-injected
`_tc_id` at their Host boundaries; `context` genuinely **consumes** it because
`molt` locates its own ToolCallBlock by that wire id for replay into the fresh
session. Context strips it from the closed root and threads it to that single
child out-of-band — the same seam `avatar` uses for root `_reasoning` — rather
than widening `_ROOT_FIELDS` or relaxing the rule that no envelope field
reaches a child. The sibling `pad` and manual-only `lingtai` roots compose their
own final child inventories independently.

`plugin/__init__.py` (`../plugin/CONTRACT.md`) is the newest Adapter/consumer,
and the first that was *born* on this package rather than migrated onto it. It
is deliberately a copy of `mcp`'s minimal shape, not a variation on it, and it
now consumes the same host-bound builder `mcp` does: one
`_build_family(host: ToolPluginHost | None)` registering an `info` child and
`build_manual_child(host.workdir, DECLARATION.manual)` directly and unwrapped,
both declaring the per-action strict-empty `input` schemas read back out of
`DECLARATION`; an import-time `host=None`
instance backing `get_schema()` whose construction is the registry's
duplicate/reserved-name collision check; a post-dispatch `_flatten_manual_result`
producing the family's own flat `plugin_manual` public shape; and an outer
`handle_plugin` rendering the same pre-`ToolFamily` unknown-action envelope
`mcp` pins, including the missing-action empty-string default and the unhashable
`action` case routed by `child_names` tuple membership. Being new, it had no
pre-migration envelope to preserve — it adopts `mcp`'s because the two tools are
deliberate twins, which is a consumer's choice under "Implementation
independence", not a requirement this package imposes. Its migration joins
`kernel/tool_result_summary.py`'s `_LTP_V2_MIGRATED_FAMILIES` in the same change,
per the allowlist step noted above.

Every other built-in family remains fully independent of this package until
its own scoped migration.

## Contract rules

Guarded by: [T006](BEHAVIORS.md#behavior-t006) and
[T011](BEHAVIORS.md#behavior-t011).

- A `ToolFamily`'s child registry MUST be validated at construction: duplicate
  child names and more than one child named the reserved `manual` MUST raise
  `ToolFamilyError`, not register silently or resolve by precedence.
- A reserved `settings` child MUST be injected only for an explicit provider,
  immediately before `manual`, and accept exactly `{}`. Normal success MUST be
  only `{"settings": [...]}`; every row MUST expose exactly `key`, `current`,
  `default`, `configurable`, and `comment`, with JSON `null` as the default when
  none is meaningful. A private sensitivity flag MAY redact `current` and
  `default` to `<redacted>` but MUST NOT be projected. Provider exceptions,
  unavailable current truth, malformed rows, and unserializable values MUST
  return one fixed bounded failure with no partial rows. Consumption MUST stop
  incrementally at the 65,536-byte complete-response bound with one fixed
  no-row failure, and the action MUST offer no mutation operation.
- `build_schema()` MUST declare the aggregate `input` property as direct
  `type: object`, then embed each child's own object `input_schema` verbatim
  (no copy-and-reshape) under a branch pairing it with that child's `title`.
  The branch keyword is always `anyOf`, including opted-out families. Input
  branch annotations are not validation discriminators, so this avoids
  rejecting a valid call when two actions have overlapping constraints (while
  the root `allOf` correlation and fail-closed dispatch retain action safety).
  It MUST declare a root `reasoning` string property and include
  `reasoning` in the root `required` list — `reasoning` is Host
  InvocationContext/audit metadata, not left to Agent schema composition's
  property-only re-injection, which never touches `required`.
- `build_schema()` MUST also compose a root `allOf` with exactly one
  `if`/`then` condition per registered child, generated purely from the
  child registry: `if.properties.action.const` MUST equal that child's own
  registry name, `if.required` MUST be `["action"]`, and
  `then.properties.input` MUST be that exact child's own canonical
  `input_schema` (the same deep-copied schema the disclosure branch embeds, not
  a separately-maintained copy). This correlates `action` with `input` at
  the schema level without adding a fifth public root field or duplicating
  `action` inside `input`.
- `handle()`, when used, MUST validate `action` against the registry, type-
  check and strip root `summarize` before any child handler runs, and reject
  `input` keys outside the selected child's own declared schema `properties`
  — schema conformance alone is not the sole enforcement boundary; dispatch
  remains always-authoritative and fail-closed regardless of whether a given
  provider validates the root `allOf`/`if`/`then` schema-side
  (`../CONTRACT.md` "Dispatch and actions").
- `handle()` MUST reject an unknown root field UNLESS it is both (a) a
  property declared in the *selected* action's own `input_schema` and (b)
  entirely absent from `input` — in which case `handle()` MUST relocate it
  into `input` (add it as that key) rather than reject the call, and MUST
  continue validating exactly as if the caller had nested it correctly. A
  root field whose name duplicates a key already present in `input` — even
  with an identical value — MUST still be rejected; the relocation exception
  exists only for a genuinely misplaced-and-otherwise-absent field (observed
  cause: a calling model's own native flat tool shape, e.g. an
  `Edit(file_path, old_string, new_string, replace_all)`-style tool, leaking
  `replace_all` to root instead of nesting it under `input` for the `file`
  family's `edit` action), not for tolerating a redundant or conflicting
  duplicate. This exception applies per-family, automatically, to every
  family built on this dispatcher — it is not something an individual family
  opts into or configures.
- `handle()` MUST treat an unhashable `action` (e.g. `[]` or `{}`, reachable
  when invalid JSON survives to dispatch — the issue #513 blocker class) as
  simply matching no child, rendering the stable typed `ACTION_REQUIRED`
  envelope failure exactly as `kernel/tool_dispatch.py` does, rather than
  raising `TypeError` out of the dispatcher.
- A child handler MUST receive only its own validated `input` mapping — never
  `action`, `reasoning`, `_reasoning`, or `summarize`.
- `handle()`'s dispatch result IS the child's own raw/canonical result;
  `ToolFamily` MUST NOT wrap it a second time.
- This package MUST NOT require inheritance from a shared base/port class, a
  shared handler, common request/result types, or a universal domain result
  shape from any consumer family, matching `../CONTRACT.md` "Implementation
  independence" verbatim.
- `manual.build_manual_child`'s child MUST use the reserved name `manual`, the
  exported `manual.MANUAL_INPUT_SCHEMA` strict-empty `input_schema` — the one
  canonical spelling (`required: []` stated explicitly; families MUST NOT
  restate it locally), which any family supplying its own `manual` child
  entirely SHOULD also reference rather than restate — and its handler's
  actual return value — what
  `ToolFamily.handle()` dispatches back verbatim — MUST be the canonical
  `content[0].text` (full body) / `structuredContent.manual_path` (host-local
  path) shape, never the pre-mapping flat `load_installed_manual()` dict.
  `status`/`error` loader facts MUST be preserved truthfully alongside those
  two fields, not dropped or double-wrapped.
- A family MUST register `build_manual_child`'s returned `ChildTool` directly
  in its own `ToolFamily` — never wrapped in another handler that adapts or
  reshapes the result before `ToolFamily.handle()` returns it. Any
  family-specific public-shape adaptation (as `web` needs) MUST happen
  strictly after the family's own outer dispatch call returns, in that
  family's own Host/presentation layer, per the no-double-wrap rule above.
- This package owns no external MCP mounting, registry, adapter, schema, or
  test; it MUST NOT be extended to add one.
- `handle()`'s foreign-`input`-field rejection MAY be additively enriched
  with a `diagnostics` array, per "Diagnostics sidecar" above, but the
  legacy `status`/`error_code`/`message` failure result MUST remain
  byte-for-byte unchanged for a child that declares no descriptor for the
  trigger. `ChildTool.diagnostics` MUST NOT be read by `build_schema()` or
  reach any provider-facing schema; this package MUST NOT keep a central
  tool-name/message table or infer descriptor text — only the owning
  `ChildTool` supplies it. A diagnostic field label MUST pass the generic
  safety check before being surfaced; a rejected value, path, argument,
  exception string, or JSON blob MUST NEVER appear in a diagnostic.

## Contract tests

T011 runs `tests/test_tool_settings_contract.py`; production suites remain the
schema/dispatch non-regression evidence.

`tests/test_tool_family_generic.py` proves the infrastructure is generic using
a fake `widget` family unrelated to `web`: deterministic registration order,
duplicate-name and reserved-`manual`-collision failures, overlap-tolerant
`anyOf` schema composition with root `reasoning` REQUIRED and no unconstrained generic
`input` object, dispatch selecting the correct child and passing only its
`input`, unknown-action/non-boolean-summarize/unknown-root-field/cross-branch-
key rejection, no double result wrapping, and two dedicated proofs that
`reasoning`/`summarize` never reach a child handler and never appear in any
child's own canonical `input_schema`. It also proves the root `allOf`
correlation directly: every condition's `action` const matches the child
registry name, `then.input` exactly matches that child's own canonical
schema, a standards validator when available (with a faithful local
fallback and no added dependency) shows the schema itself rejects a mismatched
`action`/`input` pairing, `handle()` remains authoritative and fail-closed
regardless, and both the `allOf` conditions and the `anyOf` branches are
mutation-isolated from each other and from a child's own canonical schema.
It also proves the "Diagnostics sidecar" contract using an opted-in fake
`widget` child: the exact owner-declared `DiagnosticDescriptor` and
mechanically derived `<family>/<action>/input.<field>` location are returned
for a recognized foreign-`input`-field trigger; an opted-out child yields the
byte-for-byte legacy three-key failure with no `diagnostics` key; an
unknown/cross-action key is still rejected before any handler I/O regardless
of opt-in; and an unsafe- or non-identifier-shaped field label (and any raw
rejected value) never appears in a `diagnostics` entry.
`tests/test_tool_family_wire_parity.py`
proves the composed schema (including required `reasoning`) survives both
Chat Completions and Responses wires (including a real Agent
startup for `web`) at the existing OpenAI adapter seam with zero adapter code
changes, and that `ChildTool.diagnostics` text never appears anywhere in
either wire's tool schema. `tests/test_tool_family_manual_contract.py` invokes the actual
generic manual child handler (not an unused presentational helper) and
proves the ManualTool reserved name, strict empty input, the canonical
`content[0].text`/`structuredContent.manual_path` return shape,
missing-manual degraded case, and the reserved-name collision. It also
proves the registration/adaptation ownership boundary directly:
`manager._family.handle(...)` — the real `ToolFamily` `web` registers its
`manual` child in, unwrapped — returns the canonical
`content`/`structuredContent` result verbatim for a manual call, with none of
Web's legacy flat fields; `manager.handle(...)` on the identical envelope
returns Web's exact pre-migration public flat shape (`status`, `manual`,
`manual_path`, `action`, `current_setting`) with no canonical fields, for
both the success and missing-manual/degraded cases, via
`WebManager._adapt_manual_result`'s post-dispatch adaptation.
`tests/test_tool_family_web_migration_parity.py` snapshots `web`'s
pre-migration schema and proves the now-generated schema is field-equivalent
except the three authorized differences (`anyOf` → `oneOf`, required
`reasoning`, and the added root `allOf`), and separately proves `web`'s own
`allOf` correlates every real action's `const` with its exact branch schema.
`tests/test_tool_family_generic_summarize_executor.py` proves the raw-logged-
before-summary executor mechanism needs no family-specific kernel wiring,
using the fake `widget` family. `web`'s own existing suite
(`tests/test_unified_web_capability.py`,
`tests/test_web_ltp_v2_summarize_executor.py`, `tests/test_wire_tool_description.py`
— the last of which now also proves root `allOf` correlation survives
identically on both Chat Completions and Responses wires)
remains this migration's Web-specific evidence per `../web_search/CONTRACT.md`.
`tests/test_tool_family_daemon_migration.py` is the family-specific evidence
for `daemon` (`../daemon/CONTRACT.md`): one model tool slot proven against a
real Agent's composed tool list, all six child schemas and their exact field
ownership, the complete nested `emanate` task schema, cross-action and
unknown-root rejection before any engine I/O, read-only vs side-effectful
receipt truth, the reserved `manual` child's no-double-wrap result and its
separation from the engine's retained internal flat branch, and the composed
schema (including the nested task object) surviving both wires.

`tests/test_tool_family_context_migration.py` and
`tests/test_context_ownership_redesign.py` are the family-specific evidence for
`context` (`../context/CONTRACT.md`): the final action inventory, strict branch
isolation, `_tc_id` isolation on the consume-rather-than-drop molt path,
refusal-before-shed journal gates, the manual child, full reconstruction
ordering, and `molt`'s own `TRIGGER_UNSUPPORTED_INPUT_FIELD` declaration —
proving a foreign `input` field on `molt` (e.g. `files`) yields the
`CTX_MOLT_UNSUPPORTED_INPUT_FIELD` diagnostic at `context/molt/input.files`
with molt's own allowed-field-set text, while the sibling `summarize` action
still gets the plain legacy failure for a cross-action `session_journal_path`
key, and no diagnostic ever claims `session_journal_path` must be relative.
`tests/test_pad_lingtai_split.py` independently pins the two sibling
families' narrower public inventories.

`tests/test_tool_family_soul_migration.py` is the equivalent family-specific
evidence for `soul` (`../soul/CONTRACT.md`), and independently exercises this
package against an intrinsic consumer: all six child schemas and handlers, the
closed root on both wires, wrong-branch rejection before handler I/O, envelope
metadata isolation including `_tc_id`, and the reserved `manual` child's
no-double-wrap result.

## Maintenance

Keep this Contract and `ANATOMY.md` reciprocal. Update the Port
(`ChildTool`/`ToolFamily`), the `web_search` Adapter, and contract tests
together when the envelope or dispatch boundary changes. Do not add a second
family here merely because it exists — a family joins this contract's
Adapters list only when it actually migrates onto this package, per
`../CONTRACT.md` "Migration is one family at a time."
