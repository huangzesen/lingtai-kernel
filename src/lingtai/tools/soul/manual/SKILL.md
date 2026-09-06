---
name: soul-manual
description: |
  Read before calling `flow`, changing Soul configuration, or troubleshooting a disabled result; routes the seven-action call shape, settings anchors, and focused flow, configuration, and consultation references.
version: 1.5.0
last_changed_at: "2026-09-06T00:00:00Z"
related_files:
- src/lingtai/tools/soul/__init__.py
- src/lingtai/tools/soul/CONTRACT.md
- src/lingtai/tools/CONTRACT.md
- src/lingtai/tools/soul/flow.py
- src/lingtai/tools/soul/config.py
- src/lingtai/tools/soul/settings.py
- src/lingtai/tools/soul/consultation.py
- src/lingtai/tools/soul/manual/reference/flow.md
- src/lingtai/tools/soul/manual/reference/configuration.md
- src/lingtai/tools/soul/manual/reference/consultation.md
- tests/test_soul_settings.py
maintenance: |
  Tracks the tool/capability behavior it teaches; keep this short router and its focused references aligned with Soul's call, gate, settings, and consultation behavior.
---

# Soul Manual

`soul` is the agent's inner voice. `manual` is directly callable with
`input={}` and performs no Soul operation. Use the action table below as the
first-call router; the linked references own the detailed procedures and
rationale.

## Actions and routes

Use one closed envelope with `action`, that action's own `input`, and required
`reasoning`:

```json
{"action":"inquiry","input":{"inquiry":"What am I avoiding?"},"reasoning":"check my blind spot"}
```

| Action | First call and route |
|---|---|
| `inquiry` | `{"inquiry":"<non-empty question>"}`; synchronous, on-demand reflection answered in the result. See [consultation mechanics](reference/consultation.md). |
| `flow` | `{}`; asynchronous periodic consultation. It is operator opt-in; a disabled result is expected state, not a retry signal. See [flow and the opt-in gate](reference/flow.md). |
| `config` | `{"delay_seconds": <number or null>, "consultation_past_count": <integer or null>}`; send both keys and at least one non-null. It tunes cadence/count only. See [configuration and voices](reference/configuration.md). |
| `voice` | `{"set": <profile or null>, "prompt": <text or null>}`; send both keys; null/null reads. See [configuration and voices](reference/configuration.md). |
| `dismiss` | `{}`; clear only Soul's notification. |
| `settings` | `{}`; fresh, read-only five-row owner inventory. The stable comment anchors are below. See [configuration and voices](reference/configuration.md). |
| `manual` | `{}`; return this installed guide without Soul work. |

`input` is strict and action-local; a field from another action is rejected
before handler I/O. `summarize` is a root-level boolean, not child input; Soul
results are small, so leave it `false`, especially for `manual`. Flow may read
current and past-self context and run `1 + K` LLM calls, so its opt-in protects
cost and privacy. For notification, history, and consultation-pair details,
use [consultation mechanics](reference/consultation.md).

## Settings inventory anchors

Each heading is the stable target of the corresponding `settings` row comment.
The configuration reference owns accepted values, bounds, persistence, source
owners, and change procedures.

### Flow enabled

Live process gate; change it only in the launch environment, then refresh or
restart. `settings` cannot enable it. See [flow and the opt-in gate](reference/flow.md).

### Delay seconds

Enabled-flow cadence owned by `config`; it is not the gate. See [configuration
and voices](reference/configuration.md).

### Consultation past count

`K`, the past-self fan-out count, owned by `config`. See [configuration and
voices](reference/configuration.md) and [consultation mechanics](reference/consultation.md).

### Voice

Flow-voice profile state owned by the `voice` action. See [configuration and
voices](reference/configuration.md).

### Voice prompt

Sensitive custom flow-voice text; `settings` redacts it. See [configuration and
voices](reference/configuration.md).
