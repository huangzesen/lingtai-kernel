---
name: soul-consultation-reference
description: Detailed Soul inquiry and periodic consultation data flow, fan-out, and storage.
related_files:
- src/lingtai/tools/soul/manual/SKILL.md
- src/lingtai/tools/soul/manual/reference/flow.md
- src/lingtai/tools/soul/consultation.py
- src/lingtai/tools/soul/flow.py
- src/lingtai/tools/soul/inquiry.py
- src/lingtai/tools/soul/__init__.py
- tests/test_soul_consultation.py
- tests/test_tool_family_soul_migration.py
maintenance: |
  Keep consultation roles, read-only snapshot use, notification/history semantics, and append-only storage aligned with Soul's implementation and router.
---

# Soul consultation mechanics

## Inquiry versus flow

`inquiry` is a deliberate, synchronous mirror session. The submitted non-empty
question is asked of a deep copy of the current self; the result returns in the
tool response as `voice`, or `"(silence)"` when no voice is produced. It records
an entry with `mode: "inquiry"` and does not require the periodic flow opt-in.

`flow` is mechanical and asynchronous. The voluntary action only acknowledges
a trigger; voices arrive later through the same synthesized Soul-flow history
shape as timer fires. The gate, no-retry disabled result, and cadence procedure
are in [the flow reference](flow.md).

## Fire data and fan-out

For `K = consultation_past_count`, each enabled fire runs `M = 1 + K` parallel
LLM calls: one stepped-back reader of the current diary and `K` voices sampled
from earlier snapshots. `K=0` is the cheapest insights-only fire; larger values
increase token cost and history content, up to six calls. Snapshot files are
read-only consultation substrate; Soul does not create or mutate snapshots.

The configured voice prompt is shared by current and past-self consultations,
while per-fire cue text tells each call which diary context it is reading. A
late result after a state change is discarded rather than injected into the new
context. Calls run as daemon threads and are gated on the agent reaching IDLE;
no subprocess or PTY is involved.

## Notifications and history

Flow voices are published to `.notification/soul.json`. The kernel's
notification synchronization surfaces them through the synthesized
`notification(action="check")` pair. Soul keeps at most its current flow
notification through the shared notification operations; `dismiss` clears only
the `soul` channel.

Each flow or inquiry entry appends to `logs/soul_flow.jsonl`. The `mode` field
distinguishes `flow` from `inquiry`; there is no separate inquiry log. Flow
fires also append a synthesized `(ToolCallBlock, ToolResultBlock)` pair to chat
history. The call block uses the current envelope:
`action: "flow"`, `input: {}`, and host-authored `reasoning` explaining that
the agent did not initiate it. This keeps replayed history consistent with the
closed schema.

Relative to the agent working directory, the relevant paths are:

```text
.notification/soul.json
logs/soul_flow.jsonl
history/snapshots/
init.json (owned by configuration procedures, not consultation)
```

Flow reads current chat and snapshots but does not create snapshots. Manual and
settings calls do not start consultation, change timers, write configuration,
or publish notifications.
