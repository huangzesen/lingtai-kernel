---
name: component-contract-convention
contract_version: 4
related_files:
  - ANATOMY.md
  - BEHAVIORS.md
  - ENVIRONMENT_VARIABLES.md
  - GLOSSARY.md
  - src/lingtai/kernel/event_journal/CONTRACT.md
  - src/lingtai/kernel/mail_transport/CONTRACT.md
  - src/lingtai/kernel/workdir_lease/CONTRACT.md
  - src/lingtai/kernel/notification_store/CONTRACT.md
  - src/lingtai/kernel/agent_presence/CONTRACT.md
  - src/lingtai/kernel/lifecycle_clock/CONTRACT.md
  - src/lingtai/kernel/project/CONTRACT.md
  - src/lingtai/kernel/stream_progress/CONTRACT.md
  - src/lingtai/kernel/refresh_watcher/CONTRACT.md
  - src/lingtai/tools/bash/CONTRACT.md
  - src/lingtai/tools/notification/CONTRACT.md
  - src/lingtai/tools/pad/CONTRACT.md
  - src/lingtai/tools/lingtai/CONTRACT.md
  - src/lingtai/tools/psyche/CONTRACT.md
  - src/lingtai/tools/CONTRACT.md
  - src/lingtai/tools/task_card/CONTRACT.md
  - src/lingtai/tools/web_search/CONTRACT.md
  - src/lingtai/tools/tool_family/CONTRACT.md
  - src/lingtai/kernel/snapshot/CONTRACT.md
  - src/lingtai/mcp_servers/telegram/task_card/CONTRACT.md
  - src/lingtai/kernel/migrate/CONTRACT.md
  - src/lingtai/kernel/daemon_supervisor/CONTRACT.md
  - src/lingtai/kernel/base_agent/CONTRACT.md
  - src/lingtai/adapters/acp/CONTRACT.md
  - src/lingtai/kernel/tool_plugin/CONTRACT.md
  - src/lingtai/CONTRACT.md
  - CONTRIBUTING.md
  - README.md
  - dev-guide-skill/SKILL.md
  - src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md
  - src/lingtai/kernel/meta_block.py
  - tests/test_runtime_guidance_globals.py
  - tests/test_architecture_documents.py
maintenance: |
  This file is the normative root of the distributed code interface definition
  system and the contract-of-contract. Keep the root ANATOMY.md reciprocal. Keep
  each governed child CONTRACT.md linked here exactly once and require every
  child to point back with root_contract: CONTRACT.md and pair with its
  co-located ANATOMY.md. Apply the governed-component pairing and ownership
  rule below; report mismatches and never manufacture or auto-fix empty or
  duplicate Contracts. Change architecture rules, schemas, templates,
  maintenance contracts, and validation together. Revalidate all linked pairs
  whenever this convention changes; bump contract_version for a breaking
  convention change.
---
# Component Contract Convention

## Design principles

These repository-wide design principles are normative for every capability,
Contract, Anatomy, manual, and skill in this repository. Read and apply them
before any change. They stay concise here by design: the deeper how and why live
in the manuals and references they point to (progressive disclosure), not inline.

1. **User-facing-only i18n, gated by human confirmation.** LingTai considers
   internationalization only for genuinely *user-facing* surfaces. Existing
   user-facing i18n is not disabled by this rule. Internal, code-level, or
   agent-only surfaces MUST NOT acquire i18n by default. Before adding or
   expanding i18n on any surface, an agent MUST ask the human to confirm that
   i18n belongs there.
2. **Progressive disclosure wherever possible.** Prefer a concise entry that
   routes to depth over one exhaustive document, especially for agent-consumed
   material for both coding agents and LingTai agents. Each layer states its rule
   once and links onward instead of copying downstream detail upward.
3. **Every capability is taught by a manual.** EVERY capability MUST have a
   corresponding manual that explains what to do, how it works, and why it is
   designed that way. The why and deeper detail MAY route through progressively
   disclosed manual references rather than sitting inline. This does not blur the
   layers: a Contract still defines the capability's obligations and behavior,
   while its manual teaches the procedure to carry them out.
4. **Manuals are discoverable from both owner twins.** ALL manuals MUST be
   connected through the corresponding capability `CONTRACT.md` **and** its
   paired/owning `ANATOMY.md` — both edges, not either one. Where those documents
   carry a `related_files` schema, the manual (or its manual reference) MUST
   appear in the `related_files` of both the capability Contract and its paired
   Anatomy, so an agent descending either the interface graph or the navigation
   graph reaches it. A missing Contract→manual edge or a missing Anatomy→manual
   edge is a defect; global reachability through only one side does not satisfy
   this rule.
5. **The dev guide enforces these principles.** `dev-guide-skill/SKILL.md` MUST
   strongly emphasize reading and applying this section before every development
   task and route each change to the manual that teaches the capability it
   touches.
6. **Runtime prompt and metadata changes require executable startup proof.** Any
   change to runtime-guidance loading, `_meta` ownership/projection, or system-
   prompt assembly MUST be accepted only after focused regression tests plus a
   hermetic real `Agent` construction and complete system-prompt build on the
   exact candidate tree. Compilation alone is insufficient: missing globals and
   incomplete wiring can compile successfully while making every refreshed or
   relaunched agent fail before heartbeat.

## Purpose

**CONTRACT is the distributed code interface definition system.** Each
governed architectural component keeps a `CONTRACT.md` beside the code whose
interface it owns: Core/use cases, inbound and outbound Ports, Adapters,
expected agent
behavior, errors, ordering, state semantics, and conformance tests. Local
contracts link into a
graph that an agent can descend from this repository root to the exact interface
promise relevant to a change.

This file is the repository's Ports & Adapters foundation and the
**contract of contract**: the normative meaning, child template, link rules, versioning, and
maintenance contract for that distributed system. Existing specialized
contracts are governed only when this file lists them as children.

[`ANATOMY.md`](ANATOMY.md) is the paired distributed code navigation system. It
describes where code is and how it is composed; this contract defines how a
layer may be used and what it promises. They cross-link instead of duplicating
each other's content.

[`ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md) is the single canonical
registry for environment names and their configuration behavior. Route
enumeration and per-variable detail there; this contract does not duplicate its
table.

[`GLOSSARY.md`](GLOSSARY.md) is a sibling root governance document for
distributed tool glossaries. It owns model-facing alias/localized-name guidance
only; Contracts continue to own behavior, obligations, state, and tests.

## Architecture foundation

Normative rules:

1. LingTai components MUST be reasoned about as **Core / Use Cases**,
   **Ports / Contracts**, and **Adapters**. Core owns domain decisions,
   orchestration, and policy. Ports are technology-neutral boundaries owned by
   Core. Adapters translate concrete operating systems, providers, protocols,
   SDKs, processes, filesystems, or UIs into Ports.
2. The allowed conceptual dependency is:

   ```text
   Adapter -> Port <- Core
   ```

   Core and adapters may depend on the Port. Core MUST NOT depend on, import,
   construct, branch on, or name a concrete adapter.
3. The target direction is exactly: **Core owns Ports; adapters live outside**.
   A Port is placed with the Core boundary it protects; production adapters are
   placed outside that Core package and depend inward.
4. Core technology ignorance is mandatory. Core MUST NOT know POSIX vs Windows,
   OpenAI vs another model provider, Telegram vs another channel, or equivalent
   concrete technology identities. Platform/provider/channel types, exceptions,
   configuration keys, protocol payloads, and branch conditions belong in
   adapters unless translated into technology-neutral Port vocabulary.
5. A Port is more than a Python interface. Its component `CONTRACT.md` owns
   units, ordering, errors, state/time domains, and observable guarantees;
   adapters and Core use cases are tested against those same rules.
6. One small outer **Composition Root** MAY read deployment configuration,
   select concrete adapters, construct them, and inject them into Core. It MUST
   own wiring only. It MUST NOT contain business decisions, use-case policy,
   provider-specific behavior that belongs in an adapter, or a service-locator
   mechanism that lets Core fetch implementations implicitly.
7. Components MAY be nested. A component can present one capability to its
   parent while internally owning smaller Core/use-case, Port, and Adapter
   boundaries. A parent Core MUST depend on a child component through the
   child's Port, not reach through it to its internal implementation. Each
   component contract states the boundary and viewpoint it governs.
8. Concrete technology belongs only in the Adapter at the boundary where that
   technology actually varies. POSIX or Windows belongs at operating-system
   boundaries; OpenAI or another provider at model boundaries; Telegram or
   another transport at message boundaries. These identities MUST NOT leak up
   through otherwise technology-neutral parent Ports.
9. A component migration is complete only when its existing responsibility is
   actually separated into a Core-owned Port and one or more outside Adapters,
   Core no longer imports or constructs the concrete mechanism, the Composition
   Root wires the chosen Adapter, and shared contract tests prove conformance.
   New directory names or an unused interface alone do not satisfy this rule.
10. Migration MUST proceed one real boundary/vertical slice at a time: one use
    case, its Port and contract, one real production adapter, composition
    wiring, and contract tests. Do not perform a one-shot repository
    rearrangement, create speculative empty Port/adapter taxonomies, or claim
    unmigrated code already obeys the target architecture.
11. Ports are earned by architectural boundaries, not by file count. Pure
    algorithms, value objects, and ordinary internal helpers SHOULD remain
    ordinary code unless they own an independently meaningful promise, isolate
    a concrete mechanism or side effect, or require substitutable
    implementations.

### Capability-native interfaces

LingTai follows **semantic standardization, syntactic specialization**. A Port
MUST use the smallest domain vocabulary that precisely expresses its capability;
unrelated capabilities MUST NOT be forced into generic method names or one
universal service shape merely for visual uniformity or human memorability.
Uniform interface syntax is not itself an architectural virtue.

The standardized surface is instead the system for understanding, proving, and
evolving each interface:

1. Every capability boundary MUST have one Core owner, one explicit Port, and an
   adjacent normative component `CONTRACT.md` that defines semantics beyond the
   type signature: units, ordering, errors, state/time and concurrency domains,
   durability, unsupported capabilities, compatibility, and non-goals.
2. Port vocabulary MAY differ across capabilities. Adapters for the same Port
   MUST conform to the same Contract and shared contract tests; semantic
   mismatch MUST fail loudly rather than be hidden behind superficially uniform
   names.
3. The Composition Root and the reciprocal Anatomy/Contract/code link graph
   MUST make each specialized Port discoverable and its production Adapter
   explicit. Coding agents may traverse heterogeneous interfaces quickly, but
   context is finite, so Contracts MUST remain local, concise, and progressively
   disclosed rather than becoming one global specification.
4. At genuine cross-organization or ecosystem interoperability boundaries,
   established standards such as HTTP, SQL, MCP, or POSIX SHOULD remain the
   shared vocabulary. Capability-native specialization applies inside those
   boundaries; it is not permission to replace useful external protocols with
   private invention.

Inbound ports versus outbound ports:

An **inbound port** is how an external driver asks Core to execute a use case.
It points into Core. The use-case implementation is in Core; a driving adapter
translates an external event or request into the inbound Port.

An **outbound port** is how Core asks the outside world for a capability. It
points out of Core conceptually, while the source dependency still points inward
because an outer adapter implements the Core-owned Port.

The Composition Root belongs at the outer application/startup edge:

```text
read deployment config -> construct selected adapters -> inject Ports -> start Core
```

Choosing which adapter is configured is wiring. Deciding what an agent should
do, when a message is handled, how a use case interprets time, or what fallback
policy applies is Core/use-case policy and MUST remain outside the Composition
Root.

Non-normative wall-socket analogy:

As explanatory prose only, Core is like a house whose rooms rely on wall
sockets without knowing the power station or appliance manufacturer. The
Port/Contract is the socket shape, voltage, and safety agreement; an adapter is
the plug/transformer that connects a particular external technology; the
Composition Root decides what is plugged in. The analogy is not normative, does
not define Python placement, and must not replace the inbound/outbound or
dependency rules above.

## Behavior

Every contract includes an expected-agent-behavior agreement. It states
observable obligations and prohibitions for LingTai agents and coding agents
that use, inspect, or modify the governed component. It does not duplicate a
manual's commands or troubleshooting recipes: **Behavior defines what agents
must do; manuals and skills explain how to do it.**

Root behavior rules:

1. Before development, agents MUST follow the repository-local
   `dev-guide-skill/SKILL.md`; before reasoning
   about or changing a governed component, they MUST read the nearest
   `ANATOMY.md` to navigate its code and the paired `CONTRACT.md` to learn its
   interface and behavior promises.
2. LingTai agents that observe runtime behavior MUST compare evidence with the
   contract, surface mismatches, and preserve uncertainty. They MUST NOT hide an
   implementation defect by weakening the written promise. They may report or
   propose a contract change, but changing the product promise requires explicit
   authorization.
3. Coding agents MUST keep implementation, the Anatomy/Contract pair, Ports,
   affected Adapters, and shared contract tests synchronized in the same PR
   whenever their governed facts or promises change.
4. Agents MUST traverse YAML `related_files` as the distributed graph and repair
   missing, stale, duplicate, one-way, or orphaned edges they touch. They MUST
   NOT invent a second registry or copy the same normative rule into multiple
   layers.
5. Agents MUST keep concrete technology outside Core, wire implementations only
   at the Composition Root, and reject unused interfaces or directory-only
   reshuffles as evidence of a completed migration.
6. A component's local `Behavior` section MAY add stricter obligations specific
   to that boundary, including safe handling of retries, cancellation, unknown
   side effects, ordering, recovery, or sensitive data. It MUST NOT contradict
   this root behavior contract.

The behavior agreement is jointly maintained: LingTai agents contribute runtime
observations and drift evidence; coding agents update code and architecture
documents; shared tests and review supply conformance evidence.

## Frontmatter contract

The root contract has exactly `name`, `contract_version`, `related_files`, and
`maintenance` frontmatter keys. It omits `root_contract` because it is the root.

Every governed child contract has exactly these frontmatter keys, in this
order:

1. `name`: non-empty kebab-case identity, unique among root-linked children.
2. `contract_version`: positive YAML integer.
3. `root_contract`: literal repo-relative path `CONTRACT.md`.
4. `related_files`: non-empty duplicate-free list of repo-relative regular
   files. It includes the co-located paired `ANATOMY.md`, the Port, every
   production Adapter, contract tests, public exports, directly relevant
   component contracts, and — per Design principles 3 and 4 — the corresponding
   manual(s) or manual reference for every capability the governed component
   exposes. Every exposed capability MUST have such a manual (a component that
   exposes no capability need not invent one, but no actual capability may opt
   out). The paired `ANATOMY.md` MUST link the same manual(s) so both owner twins
   carry the edge; the Contract lists the manual as the capability's interface
   owner, while the Anatomy lists it as a navigation target, without copying the
   manual's content into either.
5. `maintenance`: a concise maintenance note for the governed component. It
   MUST preserve the root guidance: keep `related_files` complete, keep the
   Anatomy/Contract and ownership links reciprocal, and update the pair when
   structure or normative behavior changes. The note may route back to this
   root contract; it is documentation, not a byte-identical child snapshot.

## Body contract

The root body headings are exactly the ten `##` sections in this file, in
this order, beginning with `## Design principles`.

Every governed child body has these `##` headings, once and in this order:

1. `## Purpose`
2. `## Behavior`
3. `## Port`
4. `## Adapters`
5. `## Contract rules`
6. `## Contract tests`
7. `## Maintenance`

Child contracts describe behavior and maintenance obligations. They do not use
the `ANATOMY.md` structural section template and do not require line citations.

## Link semantics

YAML `related_files` is the single graph-wiring mechanism; do not introduce a
second registry. This root contract and root anatomy list each other exactly
once. The governed child `CONTRACT.md` entries in root `related_files` form the
canonical paired-component index.

### Governed component pairing and ownership

The unit of pairing is a **governed architectural component**, not every
directory. Every governed component MUST have co-located, reciprocal
`ANATOMY.md` and `CONTRACT.md` twins. Do not create an empty or duplicate
Contract merely to make filenames symmetrical.

An implementation, Adapter, or navigation-only Anatomy MAY omit a local
Contract only when it owns no independent behavioral promise. Its
`related_files` MUST identify exactly one owning governed component Contract,
its body MUST name that owner and explain why no independent local Contract
exists, and the owner Contract MUST link back to that Anatomy. This unique
ownership preserves one normative behavioral source.

The twins provide mutual progressive disclosure without copying each other:
Anatomy answers where code lives and how it composes, then points to Contract
for promises and boundaries; Contract states the normative promises, then
points to Anatomy for code locations, composition, and call chains. An owned
implementation Anatomy points to its owning component Contract, which points
back to the implementation structure.

A maintainer who finds a pairing or ownership mismatch MUST fail loud and
report it rather than ignore, normalize, or auto-fix it. The report MUST name
the component or directory, the actual `ANATOMY.md` / `CONTRACT.md` pair
state, the violated rule, the expected unique owning component Contract, any
missing, duplicate, or wrong reciprocal links, and a suggested action. The
suggestion is not authorization to create, delete, move, or rewrite files.

Each governed child appears exactly once there, points back with
`root_contract: CONTRACT.md`, and lists its co-located `ANATOMY.md`. That anatomy
lists the child contract in return. Child `related_files` also lists the Port,
every production Adapter, contract tests, public exports, and related component
contracts that own the boundary. Contract-to-contract links are reciprocal when
either contract depends on the other's normative rules. Unrelated children do
not link to each other or copy each other's promises.

Every capability's corresponding manual — required to exist by Design principle 3
— carries that manual on **both** owner twins per Design principle 4: the
capability `CONTRACT.md` `related_files` lists it as the interface owner, and the
paired `ANATOMY.md` `related_files` lists the same manual as a navigation target.
Missing either edge is a defect. The two jobs stay distinct — Contract owns the
promise, Anatomy owns the route — and neither twin copies the manual's procedural
content.

## Maintenance contract

Every code change MUST assess both distributed systems. If files, symbols,
connections, composition, or state ownership change, update Anatomy in the same
change. If a Port, Adapter, behavioral promise, error, ordering, or state
semantic changes, update Contract and contract tests in the same change. If
neither changes, review evidence may record that the pair was checked rather
than manufacture meaningless document churn.

The repair direction differs. Code is normally the structural source of truth
for Anatomy, so stale navigation follows verified code. Contract is normative
for behavior: if implementation and a governed contract disagree, treat that as
a defect and do not silently rewrite the promise to match accidental behavior.
Only an authorized contract change may deliberately change the promise.

A breaking Port-contract change is a change that makes a previously conforming
Port consumer or Adapter no longer conform: removed or renamed operation,
changed domain, units, ordering, error semantics, narrowed guarantee, or newly
required behavior. Breaking Port-contract changes bump `contract_version` and
update the Port, affected Adapters, shared contract tests, and paired Anatomy
when structure or composition also changes.

## Validation

`tests/test_architecture_documents.py` validates parseable frontmatter,
non-empty duplicate-free safe repo-relative `related_files`, the reciprocal root
Anatomy/Contract link, governed child `root_contract` and twin pairing, unique
owners for linked implementation-only Anatomies, and reciprocal graph links. A
component enters the paired governed system only when root `related_files` lists
its contract. Legacy or staged documents outside that index remain outside
automated pair enforcement; audits MUST report them and migrate them
component-by-component, not silently normalize them. Other frontmatter key,
ordering, naming, and version conventions, heading wording, and Maintenance prose
are normative documentation reviewed by maintainers, while behavioral truth
remains in each component's shared contract tests and code review.

## Template

```markdown
---
name: <kebab-case-component-name>
contract_version: 1
root_contract: CONTRACT.md
related_files:
  - <repo-relative paired ANATOMY.md>
  - <repo-relative Port file>
  - <repo-relative production Adapter file>
  - <repo-relative contract-test file>
  - <repo-relative capability manual or manual reference>
maintenance: |
  This component contract is governed by the root CONTRACT.md. Keep
  related_files complete and repo-relative, including the paired ANATOMY.md,
  Port, production Adapters, contract tests, and relevant manuals. Update the
  Port, affected Adapters, tests, and this contract together when a boundary or
  normative behavior changes; update the paired Anatomy when structure changes.
  Follow the root Anatomy/Contract pairing and ownership rules, report mismatches,
  and do not duplicate or auto-fix the rule here.
---
# <Component Name>

## Purpose

## Behavior

<State observable obligations and prohibitions for LingTai agents and coding
agents. Link to manuals/skills for procedures instead of duplicating them.>

## Port

## Adapters

## Contract rules

## Contract tests

## Maintenance
```
