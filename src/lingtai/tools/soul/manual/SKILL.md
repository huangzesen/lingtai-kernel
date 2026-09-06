---
name: soul-manual
description: |
  Read before calling `flow`, changing Soul configuration, or troubleshooting a `status: disabled` result; explains the seven-action call shape, opt-in gate, cadence, and consultation roles.
version: 1.3.1
last_changed_at: "2026-09-04T00:00:00Z"
related_files:
- src/lingtai/tools/soul/__init__.py
- src/lingtai/tools/soul/CONTRACT.md
- src/lingtai/tools/CONTRACT.md
- src/lingtai/tools/soul/flow.py
- src/lingtai/tools/soul/config.py
- src/lingtai/tools/soul/settings.py
- src/lingtai/tools/soul/consultation.py
- tests/test_soul_settings.py
maintenance: |
  Tracks the tool/capability behavior it teaches; update when that tool's behavior changes.
---

# Soul Manual

`soul` is your inner voice. `inquiry`, `config`, `voice`, `dismiss`, `settings`,
and `manual` are **always available**. `flow` is **opt-in and disabled by
default**.

## 0. How to call it

One tool, seven actions. Every call is `action` + that action's own strict `input`
object + `reasoning`; another action's field is rejected before anything runs:

```json
{"action": "inquiry", "input": {"inquiry": "What am I avoiding?"}, "reasoning": "check my own blind spot"}
```

| Action | `input` |
|---|---|
| `inquiry` | `{"inquiry": "<your question>"}` — required, non-empty |
| `flow` | `{}` |
| `config` | `{"delay_seconds": <num or null>, "consultation_past_count": <int or null>}` — both keys are sent; at least one value must be non-null |
| `voice` | `{"set": <profile or null>, "prompt": <text or null>}` — both null = read |
| `dismiss` | `{}` |
| `settings` | `{}` — read-only; returns the five-field inventory below |
| `manual` | `{}` |

Optional fields are declared nullable rather than omittable, so pass `null` for
the ones you are not setting.

**`summarize`** is a root-level boolean (never inside `input`). Soul's results
are all small, and summarizing risks losing a voice's exact wording — leave it
false, especially for `manual`.

## Settings inventory

Call `soul(action="settings", input={}, reasoning="inspect Soul settings")` to
read the current values. The action has no set/reset API and never writes the
process environment or `init.json`. Every row has exactly `key`, `current`,
`default`, `configurable`, and `comment`; each comment links back to one exact
section below.

### Flow enabled

`flow_enabled` says whether periodic and voluntary Soul flow is currently
enabled. The process gate accepts `1`, `true`, `yes`, or `on`
(case-insensitive, surrounding whitespace ignored); anything else is false.
The only source is the live process environment variable
`LINGTAI_SOUL_FLOW_ENABLED`, with missing or unrecognized input falling back to
the meaningful default `false`. SHOW rereads the same process value as the
actual flow gate on every call. An authorized launcher/operator changes it by
setting or unsetting that variable in the agent launch environment and then
refreshing or restarting the agent; call SHOW again to verify. SHOW itself
cannot enable flow.

### Delay seconds

`delay_seconds` is the live cadence between enabled Soul-flow fires. The
meaningful default is `999999999.0`. At boot the current value is hydrated from
`init.json` key `manifest.soul.delay` when authored, otherwise the default;
after an authorized `config` call, SHOW reads the updated live
`SoulRuntimePort.soul_delay`. There is no environment peer. The supported
change procedure accepts a finite JSON number of at least `30`:
`soul(action="config", input={"delay_seconds":300,"consultation_past_count":null}, reasoning="change Soul cadence")`.
That action validates and persists the value, updates live state, and restarts
the pending timer when applicable; call SHOW again to verify. Invalid config
input returns the existing Soul error and changes nothing. The cadence does not
enable or disable flow.

### Consultation past count

`consultation_past_count` is `K`, the number of past-snapshot voices in each
enabled fire; total fan-out is `1 + K`. The meaningful default is `0`. At boot
the current value is hydrated from `init.json` key
`manifest.soul.consultation_past_count` when authored, otherwise the default;
SHOW then reads that live config value. There is no environment peer. The
supported change procedure accepts integers from `0` through `5`:
`soul(action="config", input={"delay_seconds":null,"consultation_past_count":2}, reasoning="change Soul fan-out")`.
The action rejects out-of-range input without changing state, persists valid
input, and applies it to the next fire; call SHOW again to verify. The init
loader type-checks authored values but does not reapply the config action's
range rule, so SHOW reports the effective live integer rather than silently
normalizing it.

### Voice

`voice` selects the consultation profile. The supported `voice` action accepts
`inner`, `observer`, or `custom`; the meaningful default is `inner`. At boot
the current value is hydrated from `init.json` key `manifest.soul.voice` when
authored, otherwise the default; SHOW reads that live config value. There is no
environment peer. Change a built-in with
`soul(action="voice", input={"set":"observer","prompt":null}, reasoning="change Soul voice")`,
or use the atomic custom procedure in the next section. Unknown action input
returns Soul's existing error and changes nothing. The selection is persisted,
applies to the next consultation, and should be verified with another SHOW.

### Voice prompt

`voice_prompt` is the custom consultation system prompt. The supported `voice`
action accepts a non-empty string of at most `4000` characters when `voice` is
`custom`; there is no meaningful prompt default. At boot the live value is
hydrated from `init.json` key `manifest.soul.voice_prompt`; there is no
environment peer. Because prompt text is sensitive, SHOW renders both
`current` and `default` as `<redacted>` and never exposes the private
sensitivity flag. Change profile and prompt atomically through
`soul(action="voice", input={"set":"custom","prompt":"<private prompt>"}, reasoning="set my Soul framing")`.
Switching to a built-in clears the stored custom prompt. Invalid input changes
nothing; a valid change applies to the next consultation. Call SHOW again to
verify the redacted row, and use the `voice` read mode only when authorized to
inspect the actual resolved prompt.

## 1. The soul-flow gate

**Soul flow does not run unless an operator turns it on.** It is gated by one
environment variable, `LINGTAI_SOUL_FLOW_ENABLED` — see "Flow enabled" above
for the exact accepted values and default.

The gate governs **both** firing paths:

1. **The wall-clock timer** — the periodic cadence that would otherwise fire
   every `delay_seconds` while you are IDLE. When disabled, no timer is armed.
2. **Voluntary `soul(action='flow', input={})`** — a call you make yourself. When
   disabled, it returns immediately and never spawns a fire.

A defensive last-line check inside the fire itself means even a stray residual
caller cannot fire while the gate is off.

## 2. Calling `flow` while disabled

`soul(action='flow', input={})` returns, **before** taking any lock or spawning any
thread:

```json
{"status": "disabled", "enabled": false, "env_var": "LINGTAI_SOUL_FLOW_ENABLED", "message": "..."}
```

**This is expected configuration state, not an error.** Do **not** retry it in
a loop — the result will not change until an operator sets the env var. If you
want soul flow, ask the operator to enable it (§4); otherwise use `inquiry` for
on-demand self-reflection.

## 3. `delay_seconds` is cadence, not an off switch

After the env opt-in, `delay_seconds` (set via
`soul(action='config', input={'delay_seconds': ..., 'consultation_past_count': null})`) controls **how often** the timer
fires — e.g. `300` = every 5 minutes, `7200` = every 2 hours; minimum `30`.
That is *all* it does:

- A **large** `delay_seconds` does not suppress flow — the env gate decides
  whether flow runs at all.
- A **small** `delay_seconds` does not enable flow — with the env var unset, no
  fires occur regardless of the delay.
- `config` itself never enables flow. It still runs while flow is disabled: it
  tunes and persists the knobs (`delay_seconds`, `consultation_past_count`) to
  `init.json`, returns `status: "ok"`, and adds `soul_flow_enabled: false` plus a
  `note` explaining that the saved knobs produce no fires until the operator
  enables `LINGTAI_SOUL_FLOW_ENABLED` and refreshes. This is not the
  `status: "disabled"` result reserved for `flow` (§2). Enabling is an
  **operator** action.

`delay_seconds` is cadence only. A huge sentinel value (e.g. `999999999`) used to
be the trust-based mute, but it silenced only the **timer** — the voluntary path
stayed live and could loop against the sleep gate. The env gate replaced that
fragile convention with an explicit opt-in covering both paths.

## 4. How to enable / disable

Enabling is an operator/deployment action, not something the agent does to
itself:

1. Set `LINGTAI_SOUL_FLOW_ENABLED=1` (or `true`/`yes`/`on`) in the agent's
   runtime environment.
2. Refresh/restart the agent so the new environment is loaded.
3. (Optional) tune cadence and voice count with
   `soul(action='config', input={'delay_seconds': 300, 'consultation_past_count': 2})`.

To **disable** again: unset the variable (or set it to `0`/`false`) and
refresh/restart. No `delay_seconds` sentinel is needed — the gate is the off
switch.

## 5. Checking the current state

- **Read without changing anything:** Call
  `soul(action="settings", input={}, reasoning="check Soul state")`. Its five
  rows report the current flow gate, cadence, consultation count, voice, and
  redacted prompt. If any current truth is unavailable or not JSON-safe, the
  whole action fails with `SETTINGS_UNAVAILABLE`; it never returns partial or
  placeholder rows.
- **Check the env from a shell:** use the model-facing shell envelope:
  `shell(action="run", input={"command": "printenv LINGTAI_SOUL_FLOW_ENABLED"}, reasoning="check Soul flow opt-in")`.
  Empty output means unset (disabled).
- **Enabled but no fires?** Fires only happen while you are IDLE and only after
  `delay_seconds` elapses. Confirm `delay_seconds` is a small, sane value and
  that you actually reach IDLE between turns.

## 6. Actions that always work (flow disabled or not)

None of these depend on the env gate.

- **`inquiry`** — ask a deep copy of yourself a question; the answer returns in
  the tool result. Use this for deliberate, on-demand self-reflection instead
  of waiting on flow. Requires the `inquiry` field.
- **`config`** — tune `delay_seconds` / `consultation_past_count`; persists to
  `init.json`. (Does not enable flow — see §3.)
- **`voice`** — read or set how your own soul-flow voice sounds
  (`inner`/`observer`/`custom`). Yours to choose; persists to `init.json`.
- **`dismiss`** — clear the current soul-flow notification from the panel.
- **`settings`** — show exactly five projected fields for each owned setting;
  strict empty input, no mutation, and no partial-row success.
- **`manual`** — return this manual. Reads one file and performs **no** soul
  operation: no timer change, no consultation, no config/voice/notification
  write.

## 7. Settings files

`soul` has **no** settings file at either LTP level — there is no
`settings/soul.json` and no `settings/soul.<action>.json`. Cadence and voice
live in `init.json` under `manifest.soul` (written by `config`/`voice`), and
the flow gate lives in the process environment. The read-only `settings`
action inventories those existing owners; it neither implies nor reads a
`settings/` file.

## 8. Privacy and cost rationale

Soul flow is **off by default** because each fire runs `M = 1 + K` parallel LLM
calls — a recurring silent token cost — and because it reads your current chat
and past-self snapshots to inject involuntary voices into your history. Opt-in
means an operator consciously decides to spend those tokens and surface that
reflection. Enable it when the reflection is worth the cost; otherwise reach for
`inquiry` when you specifically want a considered pause.
