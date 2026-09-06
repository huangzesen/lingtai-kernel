---
name: soul-configuration-reference
description: Detailed Soul cadence, voice, and read-only settings procedures.
related_files:
- src/lingtai/tools/soul/manual/SKILL.md
- src/lingtai/tools/soul/config.py
- src/lingtai/tools/soul/settings.py
- src/lingtai/tools/soul/flow.py
- src/lingtai/tools/soul/__init__.py
- tests/test_soul_settings.py
- tests/test_soul.py
maintenance: |
  Keep configuration bounds, persistence owners, voice sensitivity, and SHOW behavior aligned with Soul's implementation and router anchors.
---

# Soul configuration and voices

## `config`

Send both nullable keys and make at least one non-null:

```json
{"action":"config","input":{"delay_seconds":300,"consultation_past_count":null},"reasoning":"slow the cadence"}
```

`delay_seconds` is a finite number of at least 30 seconds. It is the interval
between enabled fires, not an off switch. `consultation_past_count` is an
integer from 0 through 5; it is `K`, the number of past-self voices in each
fire. Explicit `null` means leave that knob unchanged. Invalid input changes
nothing. A valid change updates live state and persists `manifest.soul.delay`
and/or `manifest.soul.consultation_past_count` in `init.json`; changing delay
restarts the pending timer when applicable.

`config` does not inspect or modify `LINGTAI_SOUL_FLOW_ENABLED`, and it never
enables or disables flow. While flow is off it still saves valid knobs and
reports `status: "ok"`, `soul_flow_enabled: false`, and a note directing the
operator to the environment gate. Use the [flow reference](flow.md) for the
operator's enable/disable procedure.

## `voice`

For a read, send both fields as null:

```json
{"action":"voice","input":{"set":null,"prompt":null},"reasoning":"inspect the Soul voice"}
```

The built-in profiles are `inner` and `observer`; `custom` requires a non-empty
prompt of at most 4000 characters. A prompt is ignored when selecting a
built-in. A valid selection is persisted atomically under `manifest.soul.voice`
and, for custom, `manifest.soul.voice_prompt`; switching to a built-in clears
any stored custom prompt. Invalid input leaves the prior profile and prompt
unchanged. The selected profile applies to the next consultation, while each
consultation cue still distinguishes current from past-self diary context.

Custom prompt text is sensitive. Do not put it in settings output: `settings`
redacts both current and default for `voice_prompt`. Use the authorized `voice`
read response only when inspecting the resolved prompt is appropriate.

## `settings` and owners

Call `soul(action="settings", input={}, reasoning="inspect Soul settings")` for
a fresh, strict read-only inventory. It returns exactly five rows in this order:
`flow_enabled`, `delay_seconds`, `consultation_past_count`, `voice`, and
`voice_prompt`. Each row has exactly `key`, `current`, `default`,
`configurable`, and `comment`; the comments target the five stable headings in
the top-level `soul-manual`. Any unavailable, non-finite, non-JSON-safe current
value fails the whole action with no partial rows.

There is no `settings/soul.json` or action-specific settings file. The flow gate
belongs to the process environment; cadence, count, voice, and custom prompt
belong to `init.json` under `manifest.soul`. SHOW reads these owners and writes
none of them. The flow gate can be changed only by the authorized launcher or
operator; cadence and count use `config`, and voice uses the atomic `voice`
procedure.
