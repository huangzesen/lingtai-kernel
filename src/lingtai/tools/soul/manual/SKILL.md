---
name: soul-manual
description: |
  Read before calling `flow`, changing Soul configuration, or troubleshooting a disabled result; routes the seven-action call shape, settings anchors, and focused flow, configuration, and consultation references.
version: 1.4.0
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

`soul` is the agent's inner voice. Read this router before changing flow or
configuration; `manual` remains directly callable with `input={}` and performs
no Soul operation. The schema is the first-call summary; the references below
hold the detailed procedures and rationale.

## Call shape

Use one closed envelope with `action`, that action's own `input`, and required
`reasoning`:

```json
{"action":"inquiry","input":{"inquiry":"What am I avoiding?"},"reasoning":"check my blind spot"}
```

| Action | First call data and meaning |
|---|---|
| `inquiry` | `{"inquiry":"<non-empty question>"}`; on-demand reflection, answered in the result |
| `flow` | `{}`; mechanical asynchronous consultation, not on-demand inquiry |
| `config` | `{"delay_seconds": <number or null>, "consultation_past_count": <integer or null>}`; send both keys and at least one non-null |
| `voice` | `{"set": <profile or null>, "prompt": <text or null>}`; send both keys; null/null reads |
| `dismiss` | `{}`; clear only Soul's notification |
| `settings` | `{}`; read-only five-row SHOW |
| `manual` | `{}`; return this installed guide without Soul work |

`input` is strict and action-local; fields from another action are rejected
before handler I/O. `summarize` is a root-level boolean, not an input field.
Soul results are small, so leave it `false`, especially for `manual` so exact
instructions remain available.

## Choose a path

- **Need a deliberate answer now?** Use `inquiry`; it runs a synchronous mirror
  session. It is independent of the periodic flow gate.
- **Need mechanical periodic reflection?** Use `flow` only after an operator
  opts in. Its disabled result is expected state, not an error: do not retry it
  until the operator changes the environment and refreshes. Read
  [flow and the opt-in gate](reference/flow.md).
- **Need cadence or fan-out changes?** Use `config`; it persists knobs but can
  never enable or disable flow. See [configuration and voices](reference/configuration.md).
- **Need a flow voice profile?** Use `voice`; it reads or atomically sets
  `inner`, `observer`, or `custom` (with a prompt). See the configuration
  reference.
- **Need to clear Soul's panel notice?** Use `dismiss`; it only clears the
  `soul` channel.
- **Need current owner truth?** Use `settings`; it never writes state and
  returns exactly the five rows described below.

Flow reads current chat and past-self snapshots and may run `1 + K` LLM calls,
so it is opt-in for both cost and privacy. For its notification, history, and
consultation-pair details, read [consultation mechanics](reference/consultation.md).

## Settings inventory anchors

Each heading is the stable target of the corresponding `settings` row comment.
For accepted values, source precedence, persistence, and change procedures,
follow [configuration and voices](reference/configuration.md).

### Flow enabled

`flow_enabled` is false unless the operator sets
`LINGTAI_SOUL_FLOW_ENABLED` to `1`, `true`, `yes`, or `on` (case-insensitive),
then refreshes or restarts. The `settings` action cannot enable it.

### Delay seconds

`delay_seconds` is the enabled-flow cadence in seconds, with a minimum of 30;
`null` leaves it unchanged. It is not an off switch and has no effect while the
environment gate is off.

### Consultation past count

`consultation_past_count` is `K`, the number of past-self voices per fire, from
0 through 5; `null` leaves it unchanged. Each fire fans out to `1 + K` calls.

### Voice

`voice` selects `inner`, `observer`, or `custom`; null reads the current
profile and resolved prompt. A built-in selection clears a stored custom prompt.

### Voice prompt

`voice_prompt` is the custom flow-voice system prompt, capped at 4000
characters and shown redacted by `settings`. It is required for `custom` and is
sensitive; use the authorized `voice` read mode for its actual resolved text.

## Reference map

- [Flow and opt-in gate](reference/flow.md) — accepted gate values, disabled/no-retry behavior, operator procedure, cadence, and cost/privacy rationale.
- [Configuration and voices](reference/configuration.md) — nullable input rules, bounds, persistence, profiles, prompt sensitivity, and the five-row SHOW owner.
- [Consultation mechanics](reference/consultation.md) — inquiry versus flow, fan-out, IDLE timing, snapshots, notifications, history pairs, and append-only records.
