---
name: soul-flow-reference
description: Detailed operator and agent guidance for Soul's opt-in flow gate and cadence.
related_files:
- src/lingtai/tools/soul/manual/SKILL.md
- src/lingtai/tools/soul/flow.py
- src/lingtai/tools/soul/__init__.py
- src/lingtai/tools/soul/config.py
- src/lingtai/tools/soul/manual/reference/consultation.md
- tests/test_soul.py
- tests/test_tool_family_soul_migration.py
maintenance: |
  Keep the flow gate, disabled-path, cadence, and operator procedure accurate with Soul's implementation and top-level manual router.
---

# Soul flow and opt-in gate

## Gate

Soul flow has one owner: the process environment variable
`LINGTAI_SOUL_FLOW_ENABLED`. Missing, blank, or unrecognized values mean
`false`; `1`, `true`, `yes`, and `on` mean `true`, ignoring case and surrounding
whitespace. This gate covers both the wall-clock timer and voluntary
`soul(action="flow", input={})`. No Soul action, including `config`, can change
it.

Flow is disabled by default. With the gate off, a voluntary `flow` call returns
`status: "disabled"` before taking the fire lock, waiting for IDLE, or spawning
a thread. This is expected configuration state, not an error; do not retry in a
loop. The result identifies `LINGTAI_SOUL_FLOW_ENABLED` and tells the operator
what must change. A stray timer or caller is also stopped by the defensive gate
inside the fire path.

## Operator procedure

1. Set `LINGTAI_SOUL_FLOW_ENABLED=1` (or `true`, `yes`, `on`) in the agent's
   launch environment.
2. Refresh or restart the agent so the new process environment is loaded.
3. Optionally use `config` to tune cadence and past-self count; verify with
   `settings`.
4. To disable, unset the variable or set it to an unrecognized/false value,
   then refresh or restart. Do not use a large delay as a mute sentinel.

`settings` reports the live gate but cannot set it. If flow is disabled, use
`inquiry` for deliberate self-reflection; `inquiry`, `config`, `voice`,
`dismiss`, `settings`, and `manual` remain available.

## Cadence

`delay_seconds` is only the interval after opt-in. It accepts finite numbers of
at least 30 seconds through `config`, persists under `manifest.soul.delay`, and
restarts a pending timer when changed. A small value does not enable flow; a
large value does not disable it. Enabled timers fire only while the agent is
IDLE and are started from the IDLE transition. The voluntary path waits for
IDLE up to the live delay rather than blocking the tool call; an ongoing fire is
rejected instead of silently duplicated.

Configuration remains meaningful while flow is off: valid cadence/count knobs
are persisted and return `status: "ok"` plus `soul_flow_enabled: false` and an
explanatory note. That result is distinct from `flow`'s `status: "disabled"`.

## Cost and privacy

An enabled fire reads current chat and read-only past-self snapshots and fans
out `M = 1 + K` LLM calls. It can publish involuntary voices into the Soul
notification and synthesized history pair. Keep the operator opt-in explicit
when recurring token cost and reflection from prior snapshots are acceptable;
otherwise use on-demand `inquiry`. See
[consultation mechanics](consultation.md) for persistence and fan-out details.
