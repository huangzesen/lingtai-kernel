"""Generic internal ToolFamily composition: one model tool per family.

A ``ToolFamily`` groups cohesive child Tools (``ChildTool``) behind exactly
one model-facing tool. The model sees one aggregate schema composed from
each child's own canonical ``input_schema``: a root ``allOf`` of one
``if``/``then`` condition per registered child correlates the sibling
``action`` const with that exact child's ``input`` shape (schema-level
correlation, not just dispatch-time), plus root ``input`` disclosure. Both
surfaces are generated purely from the child registry — no name/schema
mapping table — and both derive from the same deep-copied canonical child
schemas. Root-level correlation via ``allOf``/``if``/``then`` was adopted
after a live non-strict Codex Responses probe on 2026-07-27 accepted a raw
root ``allOf``/``if``/``then`` schema without error on the current route,
superseding the earlier, more conservative assumption that root combinators
were universally rejected (see ``lingtai.llm.openai.adapter``'s
``_scrub_responses_schema`` for the corresponding wire-level change).
Children never consume model tool
slots. Dispatch remains the second, always-authoritative enforcement layer —
an in-process handler lookup keyed by the child's own name, which rejects
any ``input`` key outside the selected child's own declared schema before
the handler runs, fail-closed with no guessing/fallback — so a provider that
does not enforce ``allOf``/``if``/``then`` (or a call bypassing schema
validation entirely) still cannot smuggle a mismatched ``action``/``input``
pairing past dispatch. There is no transport, no external MCP surface, and
no second registry.

This module standardizes only the wire envelope and the composition/dispatch
boilerplate every hand-migrated LTP v2 family (see
``src/lingtai/tools/CONTRACT.md``) would otherwise duplicate. It is an
optional helper, not a mandatory base class: a family may use
:class:`ToolFamily` directly, or hand-write an equivalent ``handle()`` the way
``web`` did before adopting it. Two children in a family are not required to
share anything beyond their registration in one :class:`ToolFamily`.

A ``ChildTool`` may also declare a passive, non-wire ``diagnostics`` sidecar —
static, mechanical :class:`DiagnosticDescriptor` text the *action* owns for a
structural dispatch-time trigger it opts into (today: a foreign key in its
own ``input``). ``handle()`` additively enriches its already fail-closed
rejection with this text plus a generically computed ``location``; it never
inspects/parses prose, guesses a reason, or keeps a central tool-name/message
table, and the sidecar never reaches ``build_schema()`` or any provider wire.
See ``CONTRACT.md`` "Diagnostics sidecar" for the full contract.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .settings import SettingRow, SettingsProvider

__all__ = [
    "ChildTool",
    "DiagnosticDescriptor",
    "SettingRow",
    "SettingsProvider",
    "ToolFamily",
    "ToolFamilyError",
    "TRIGGER_UNSUPPORTED_INPUT_FIELD",
]

# The one reserved child name every family may offer at most once. Colliding
# with it at registration is a defect, not a naming choice to resolve
# silently — see ``tools/CONTRACT.md`` "Every LingTai-owned family MUST offer
# a `manual` action" and the ManualTool stable contract.
RESERVED_MANUAL_NAME = "manual"
RESERVED_SETTINGS_NAME = "settings"

# Envelope root fields admitted alongside ``action``/``input``.
_ROOT_FIELDS = {"action", "input", "reasoning", "_reasoning", "summarize"}

# The one structural trigger a ChildTool may currently declare a diagnostic
# descriptor for: the selected action's own ``input`` carrying a key outside
# its declared schema properties (cross-action or wholly foreign). Additional
# triggers would be added here, never invented ad hoc by a consumer.
TRIGGER_UNSUPPORTED_INPUT_FIELD = "unsupported_input_field"

# A diagnostic field *label* (never the rejected value) is only ever surfaced
# when it looks like a conventional identifier and carries no secret-shaped
# substring — this is a generic safety boundary, not a tool-specific
# allow/deny table, so it applies uniformly to every opted-in child.
_SAFE_FIELD_LABEL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_UNSAFE_FIELD_LABEL_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "private",
    "auth",
    "access_key",
    "session_key",
)


def _is_safe_field_label(label: str) -> bool:
    if not isinstance(label, str) or not _SAFE_FIELD_LABEL_PATTERN.match(label):
        return False
    lowered = label.lower()
    return not any(bad in lowered for bad in _UNSAFE_FIELD_LABEL_SUBSTRINGS)

# Same canonical English reasoning-property description the kernel injects
# for every tool (``kernel/base_agent/tools.py:_REASONING_DESCRIPTION``).
# Declared here too so the composed family schema is self-describing and
# ``reasoning`` is REQUIRED before ``_build_tool_schemas`` merges its own
# (identical) property text in — that merge only overwrites ``properties``,
# never ``required``, so this is the one place a family schema must declare
# ``reasoning`` as required. Kept as a literal copy rather than an import to
# avoid a tools -> kernel dependency; the exact wording is a contract fact
# proven equal by ``tests/test_tool_family_wire_parity.py``.
_REASONING_DESCRIPTION = (
    "Brief explanation of why you are calling this tool "
    "(recorded in your diary)."
)


class ToolFamilyError(ValueError):
    """Raised for a ToolFamily construction defect (bad registry shape)."""


@dataclass(frozen=True, slots=True)
class DiagnosticDescriptor:
    """One action-owned, static, mechanical diagnostic for a structural trigger.

    Declared adjacent to the action it describes — see
    :attr:`ChildTool.diagnostics` — never computed or guessed by the generic
    dispatcher. ``code``, ``expected_form``, ``reason``, and ``fix`` are all
    fixed strings the owning action's author writes once; :class:`ToolFamily`
    only ever supplies the structural ``location`` (family/action/input.field)
    around this verbatim text, and never inspects/parses it.
    """

    code: str
    expected_form: str
    reason: str
    fix: str


@dataclass(frozen=True, slots=True)
class ChildTool:
    """One family-owned child: canonical name, schema, and handler.

    ``name`` is simultaneously the child's registry key, the model-facing
    ``action`` constant, and the dispatch handler key — one name, three
    roles, no mapping layer. ``input_schema`` is this child's own strict
    JSON Schema object for ``input`` (closed, ``additionalProperties: false``
    is the child's own responsibility). ``handler`` receives only the
    validated ``input`` mapping (never ``action``, ``reasoning``, or
    ``summarize``) and returns the child's own raw, canonical result dict.

    ``diagnostics`` is an optional, passive, non-wire sidecar: a mapping from
    structural trigger name (e.g. :data:`TRIGGER_UNSUPPORTED_INPUT_FIELD`) to
    the static :class:`DiagnosticDescriptor` this action owns for that
    trigger. It never reaches ``build_schema()`` or any provider wire — only
    ``handle()`` reads it, to enrich its own already-fail-closed rejection
    with additive, mechanical hint text. A child that declares no entry for a
    trigger gets exactly the legacy failure result for it, unchanged.
    """

    name: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any]], dict[str, Any]]
    title: str | None = None
    diagnostics: Mapping[str, DiagnosticDescriptor] | None = None

    def branch_title(self) -> str:
        return self.title or f"{self.name} input"


class ToolFamily:
    """One model-facing aggregate tool composed from a fixed child registry.

    Construction validates the registry (deterministic order, unique names and
    reserved-name collisions). :meth:`build_schema`
    recomputes the composed model-facing schema on every call rather than
    caching one at construction. :meth:`handle` is the family/Host dispatch
    boilerplate: it validates ``action``, strips and validates ``summarize``,
    rejects unknown envelope fields and cross-branch ``input`` keys, then
    calls the selected child's handler with only its own ``input``. Using
    this dispatcher is optional; a family may implement an equivalent
    ``handle()`` by hand and skip this class entirely.
    """

    def __init__(
        self,
        name: str,
        children: Sequence[ChildTool],
        *,
        settings_provider: SettingsProvider | None = None,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ToolFamilyError("ToolFamily name must be a non-empty string")
        if not children and settings_provider is None:
            raise ToolFamilyError(f"ToolFamily {name!r} must register at least one child")
        seen: dict[str, ChildTool] = {}
        for child in children:
            if not isinstance(child, ChildTool):
                raise ToolFamilyError(f"ToolFamily {name!r} child must be a ChildTool")
            if not child.name or not isinstance(child.name, str):
                raise ToolFamilyError(f"ToolFamily {name!r} child name must be a non-empty string")
            if child.name == RESERVED_SETTINGS_NAME:
                raise ToolFamilyError(
                    f"ToolFamily {name!r} must not register reserved child "
                    f"{RESERVED_SETTINGS_NAME!r}; explicit settings opt-in injects it"
                )
            if child.name in seen:
                raise ToolFamilyError(
                    f"ToolFamily {name!r} has a duplicate child name {child.name!r}"
                )
            seen[child.name] = child
        ordered_children = list(children)
        if settings_provider is not None:
            from .settings import build_settings_child

            if not any(child.name == RESERVED_MANUAL_NAME for child in ordered_children):
                raise ToolFamilyError(
                    f"ToolFamily {name!r} settings require a manual child"
                )
            try:
                settings_child = build_settings_child(settings_provider)
            except ValueError as exc:
                raise ToolFamilyError(f"ToolFamily {name!r} has invalid settings: {exc}") from exc
            manual_index = next(
                (i for i, child in enumerate(ordered_children)
                 if child.name == RESERVED_MANUAL_NAME), len(ordered_children)
            )
            ordered_children.insert(manual_index, settings_child)
        self.name = name
        self._children = {child.name: child for child in ordered_children}
        # Preserve caller-declared order for deterministic schema branches.
        self._order: tuple[str, ...] = tuple(child.name for child in ordered_children)

    @property
    def child_names(self) -> tuple[str, ...]:
        return self._order

    def has_manual(self) -> bool:
        return RESERVED_MANUAL_NAME in self._children

    def build_schema(self) -> dict[str, Any]:
        """Compose the model-facing schema.

        Two enforcement layers, generated purely from the child registry (no
        name/schema mapping table), both built from the same deep-copied
        canonical child schemas so they cannot drift apart:

        1. **Schema-level correlation (``allOf``):** one ``if``/``then``
           condition per registered child. Each ``if`` tests root ``action``
           against that child's own name via ``const`` (guarded by
           ``required: ["action"]``, since a strict ``if`` with a missing
           property vacuously matches); each ``then`` constrains root
           ``input`` to that exact child's ``input_schema``. This lets a
           provider that enforces ``allOf``/``if``/``then`` reject a
           mismatched ``action``/``input`` pairing before the call is even
           made — a live non-strict Codex Responses probe on 2026-07-27
           showed this root construct is accepted, not rejected, by the
           current backend route.
        2. **Typed ``input`` disclosure:** the same per-action branches retained
           for discoverability under an overlap-tolerant ``anyOf`` union. Branch
           annotations such as titles and descriptions are not validation
           discriminators, so semantically equivalent action schemas are valid
           members of the same family.

        The root carries ``action``, ``input``, required ``reasoning``, and
        optional ``summarize`` — exactly the four LTP v2 envelope fields
        (``tools/CONTRACT.md`` "Envelope"); `allOf`'s conditions constrain
        ``action``/``input`` without duplicating ``action`` inside ``input``
        or adding a fifth public field. ``reasoning`` is Host
        InvocationContext/audit metadata, never action input: it is declared
        here as REQUIRED so every family built on this infrastructure is
        model-visible-required by construction, not by relying on Agent
        schema composition (``kernel/base_agent/tools.py:_build_tool_schemas``)
        to add it — that step still re-injects the identical property text
        for every tool uniformly, but only touches ``properties``, never
        ``required``.

        Schema-level correlation is a second, additive layer, not a
        replacement for dispatch: :meth:`handle` remains the always-
        authoritative, fail-closed enforcement boundary regardless of
        whether a given provider actually validates ``allOf``/``if``/``then``
        before invocation.
        """
        input_branches = []
        all_of_conditions = []
        for child_name in self._order:
            child = self._children[child_name]
            # Deep-copy once per child so the disclosure branch and the
            # ``allOf`` condition's ``then.input`` never share a mutable
            # container with each other, the child's own canonical
            # ``input_schema``, or a previous ``build_schema()`` call.
            canonical_input_schema = copy.deepcopy(child.input_schema)

            branch = {"title": child.branch_title()}
            branch.update(copy.deepcopy(canonical_input_schema))
            input_branches.append(branch)

            all_of_conditions.append(
                {
                    "if": {
                        "properties": {"action": {"const": child_name}},
                        "required": ["action"],
                    },
                    "then": {
                        "properties": {"input": copy.deepcopy(canonical_input_schema)},
                    },
                }
            )
        # Input branches are a discovery union, not an action discriminator:
        # annotations do not affect JSON-Schema validation, so distinct actions
        # may legitimately have overlapping constraints (for example, poll and
        # cancel can both require only a string job_id).  Root allOf/if/then
        # conditions already correlate action with its exact input schema, and
        # dispatch remains the fail-closed authority for providers that do not
        # enforce those conditions.
        input_branches_keyword = "anyOf"
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(self._order),
                    "description": f"Required operation within the {self.name} family.",
                },
                "input": {
                    "type": "object",
                    "description": (
                        "Strict action-specific input; the selected action is "
                        "validated again at dispatch."
                    ),
                    input_branches_keyword: input_branches,
                },
                "reasoning": {
                    "type": "string",
                    "description": _REASONING_DESCRIPTION,
                },
                "summarize": {
                    "type": "boolean",
                    "description": (
                        "Root, cross-cutting result post-processing control; "
                        "absent or false by default. Not action input."
                    ),
                },
            },
            "required": ["action", "input", "reasoning"],
            "additionalProperties": False,
            "allOf": all_of_conditions,
        }

    def _allowed_input_keys(self, child: ChildTool) -> set[str]:
        properties = child.input_schema.get("properties") if isinstance(child.input_schema, Mapping) else None
        return set(properties) if isinstance(properties, Mapping) else set()

    def handle(self, args: Mapping[str, Any] | None) -> dict[str, Any]:
        """Validate the envelope, dispatch to the selected child, return its raw result.

        ``summarize`` is stripped and type-checked before the unknown-field
        check so a family opting into this dispatcher gets the same ordering
        ``web`` hand-wrote. Children receive only their own ``input`` mapping
        — never ``action``, ``reasoning``, ``_reasoning``, or ``summarize``.
        Per ``tools/CONTRACT.md`` "Dispatch and actions", schema conformance
        alone is not the dispatch-time authorization boundary: ``input`` keys
        are validated against the selected child's own declared
        ``input_schema`` properties, rejecting any key belonging to another
        action's branch, before the child handler ever runs. That specific
        rejection is additively enriched via ``_build_diagnostics`` when the
        selected child opted in for :data:`TRIGGER_UNSUPPORTED_INPUT_FIELD`
        (see ``ChildTool.diagnostics``); an opted-out child gets the exact
        legacy three-key failure, unchanged.

        Invalid JSON can make ``action`` an unhashable value (e.g. ``[]`` or
        ``{}``) — the issue #513 blocker class. Such an ``action`` simply
        matches no child and renders the stable typed envelope failure below,
        exactly as ``kernel/tool_dispatch.py`` does, rather than raising
        ``TypeError`` out of the dispatcher.

        A root-level key that is neither an envelope field nor a declared
        property of the *selected* action's own input schema is rejected as
        before — including a root-level key that duplicates a name already
        present in ``input``, which stays a hygiene violation exactly as
        before (two conflicting or redundant places for the same value is a
        client bug worth surfacing, not something to silently prefer one
        side of).

        The one narrower case this now rescues instead of rejecting: a
        root-level key that IS a declared property of the selected action
        AND is entirely absent from ``input`` (e.g. ``replace_all`` sent as
        a sibling of ``action``/``input`` instead of nested inside ``input``,
        with no ``replace_all`` in ``input`` at all). That specific shape is
        relocated into ``input`` rather than failing the whole call —
        calling models with strong priors from their own native flat tool
        shapes (e.g. a model's built-in ``Edit(file_path, old_string,
        new_string, replace_all)`` tool) occasionally emit exactly this,
        otherwise-harmless, misplacement. This never widens what ends up in
        ``input`` beyond a key the schema already declares for this action,
        and the existing ``input``-keys check below still runs unchanged
        afterward.
        """
        raw = dict(args or {})
        action = raw.get("action")
        try:
            known_action = action in self._children
        except TypeError:
            known_action = False
        if not known_action:
            return self._envelope_error(
                "ACTION_REQUIRED",
                f"action must be one of {', '.join(self._order)}",
            )
        if "summarize" in raw:
            summarize = raw.pop("summarize")
            if not isinstance(summarize, bool):
                return self._envelope_error("INVALID_ARGUMENT", "summarize must be a boolean")
        child = self._children[action]
        allowed = self._allowed_input_keys(child)
        unknown = set(raw) - _ROOT_FIELDS
        if unknown:
            action_input_probe = raw.get("input")
            already_in_input = (
                set(action_input_probe) if isinstance(action_input_probe, Mapping) else set()
            )
            relocatable = (unknown & allowed) - already_in_input
            if (unknown - relocatable) or not isinstance(action_input_probe, Mapping):
                return self._envelope_error("INVALID_ARGUMENT", f"unsupported {self.name} argument")
            for key in relocatable:
                action_input_probe[key] = raw.pop(key)
        action_input = raw.get("input")
        if not isinstance(action_input, Mapping):
            return self._envelope_error("INVALID_ARGUMENT", "input must be an object")
        foreign_input_fields = set(action_input) - allowed
        if foreign_input_fields:
            return self._envelope_error(
                "INVALID_ARGUMENT",
                f"unsupported {self.name} input field",
                self._build_diagnostics(
                    child, TRIGGER_UNSUPPORTED_INPUT_FIELD, foreign_input_fields
                ),
            )
        return child.handler(action_input)

    def _build_diagnostics(
        self, child: ChildTool, trigger: str, fields: set[str]
    ) -> list[dict[str, str]] | None:
        """Render an additive diagnostic entry per safe field label, if opted in.

        Purely structural: this method computes only the ``location``
        (family/action/input.field) and reads the child's own static
        :class:`DiagnosticDescriptor` for ``trigger`` verbatim — it never
        inspects/parses prose, guesses a reason, or consults a central
        tool-name/message table. A field whose label fails the generic safety
        check (:func:`_is_safe_field_label`) is silently dropped rather than
        surfaced; if every candidate field is dropped, or the child declared
        no descriptor for this trigger, this returns ``None`` and the caller's
        legacy three-key failure result is unaffected.
        """
        descriptor = (child.diagnostics or {}).get(trigger)
        if descriptor is None:
            return None
        entries = [
            {
                "location": f"{self.name}/{child.name}/input.{field}",
                "code": descriptor.code,
                "expected_form": descriptor.expected_form,
                "reason": descriptor.reason,
                "fix": descriptor.fix,
            }
            for field in sorted(fields)
            if _is_safe_field_label(field)
        ]
        return entries or None

    def _envelope_error(
        self, code: str, message: str, diagnostics: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "failed", "error_code": code, "message": message}
        if diagnostics:
            result["diagnostics"] = diagnostics
        return result
