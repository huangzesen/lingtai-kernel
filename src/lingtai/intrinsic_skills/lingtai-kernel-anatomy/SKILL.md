---
name: lingtai-kernel-anatomy
description: >
  Enter the Python kernel Anatomy/Contract graph before source navigation or
  maintaining architecture maps; explains how the ANATOMY.md and CONTRACT.md
  graphs pair, and what to do when code and navigation disagree.
version: 0.5.0
last_changed_at: "2026-08-07T00:00:00Z"
related_files:
- ANATOMY.md
- src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/scripts/check_anatomy_drift.py
- src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/scripts/bench_agent_session_rebuild.py
- src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/reference/mcp-protocol.md
maintenance: |
  Tracks the ANATOMY.md/CONTRACT.md convention it routes into; update when the root anatomy-of-anatomy or the pairing/link rules it summarizes change. Capability mentions in any repository document require explicit bidirectional related_files mapping to the implementing code (root ANATOMY.md ## Maintenance); update this router when that rule or its summary changes.
---

# LingTai Kernel Anatomy — Navigation Router

## Canonical source

The repository-root [`ANATOMY.md`](../../../../ANATOMY.md) is the normative
**anatomy of anatomy**: the template, frontmatter and body conventions,
component-grain gate, link/pairing semantics, and maintenance contract. Root
[`CONTRACT.md`](../../../../CONTRACT.md) owns the governed-component pairing,
ownership, mutual-progressive-disclosure, and fail-loud mismatch rule. Read
them there; do not maintain a competing convention in this skill — including
the ANATOMY-versus-CONTRACT distinction, which root `ANATOMY.md` states.

When this skill is read from an installed package without a source checkout,
locate the checkout you intend to modify and read *its* root `ANATOMY.md`
before editing. A packaged copy is routing help, not evidence that an arbitrary
local checkout follows the same revision.

## Navigation workflow

Descend the root `ANATOMY.md` through child anatomies until one points at an
exact `file.py:line` citation, then open the cited code — anatomy is navigation,
code is evidence. For enumeration questions (every callsite, matching file, or
import), search once anatomy has identified the territory.

The kernel implementation descends [`src/lingtai/ANATOMY.md`](../../ANATOMY.md)
→ [`src/lingtai/kernel/ANATOMY.md`](../../kernel/ANATOMY.md).

## Maintenance direction

Who repairs drift depends on which agent you are:

- **Coding agents** update the affected anatomy in the same commit as the code
  change that moved files, symbols, ownership, connections, composition, or
  state.
- **LingTai agents** report drift as issues, mail, or PR proposals. Do not
  silently fix.

Root `ANATOMY.md` → "## Maintenance" owns the repair rules for code-vs-anatomy
and code-vs-contract, and the requirement to verify every touched citation. It
also requires that any capability mention in any repository document be mapped
to implementing code via explicit bidirectional `related_files` entries in the
owning anatomy — a prose mention without a mapping is drift, not documentation.
Run the repository's architecture-document validator and the drift checker below.

## Drift checker

This skill owns the canonical advisory citation-rot checker. Run it from the
repository root (cwd is taken as the repo root):

```bash
# Report only; exits 0 even when drift is found.
python src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/scripts/check_anatomy_drift.py
# CI / pre-commit gate: exits 1 if any drift is found.
python src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/scripts/check_anatomy_drift.py --check
# Narrow the scan (default: src).
python src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/scripts/check_anatomy_drift.py --root src/lingtai/kernel
```

It catches only mechanical citation rot — a `file.py:line` target that is
missing or past end-of-file. An in-range citation can still point at the wrong
code, so an agent must open the cited line to confirm the claim.

`scripts/bench_agent_session_rebuild.py` is the companion benchmark for the
tiered `rebuild_agent_session_from_events()` path; `src/lingtai/kernel/ANATOMY.md`
cites it and owns its usage.

## Reference sidecars

Open one of these only when the question is actually about it; the router above
is enough for ordinary navigation.

- `reference/mcp-protocol.md` — the supported MCP SDK range and negotiated
  protocol version, the SDK-versus-LingTai ownership split, the low-level server
  handler shape, the tool-metadata sidecar, and the stdio config/env-injection
  boundary. It routes onward for LICC and registry details.

## Fallback essentials

If the root convention cannot yet be opened: one anatomy per architectural layer
beside its code, components citing verified `repo/relative/file.py:line-line`
evidence, reciprocal parent/child and Anatomy/Contract links, and no pure
implementation detail. Missing files, out-of-range citations, and one-way links
are defects — but only reading the cited code confirms semantic correctness.

Return to the root `ANATOMY.md` as soon as the checkout is available; it is the
source of truth for the complete template and rules.
