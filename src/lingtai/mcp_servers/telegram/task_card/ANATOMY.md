---
related_files:
  - src/lingtai/mcp_servers/telegram/task_card/BEHAVIORS.md
  - src/lingtai/mcp_servers/telegram/task_card/CONTRACT.md
  - src/lingtai/mcp_servers/telegram/task_card/resident.py
  - src/lingtai/mcp_servers/task_card/resident.py
  - src/lingtai/mcp_servers/telegram/task_card/SKILL.md
  - src/lingtai/mcp_servers/task_card/event_projection.py
  - src/lingtai/mcp_servers/telegram/manager.py
  - src/lingtai/mcp_servers/telegram/service.py
  - src/lingtai/mcp_servers/ANATOMY.md
  - src/lingtai/kernel/base_agent/ANATOMY.md
  - src/lingtai/tools/task_card/ANATOMY.md
  - tests/test_telegram_task_card_programmable.py
  - tests/test_telegram_task_card_toggle.py
  - tests/test_telegram_task_card_event_tail.py
  - tests/test_telegram_task_card_display_expression.py
  - src/lingtai/mcp_servers/telegram/task_card/__init__.py
  - src/lingtai/mcp_servers/telegram/task_card/_family.py
  - src/lingtai/mcp_servers/telegram/task_card/controller.py
  - src/lingtai/mcp_servers/telegram/task_card/interface.py
maintenance: |
  Keep related_files repo-relative, duplicate-free, and linked to real files.
  Keep this Anatomy reciprocal with its paired CONTRACT.md and packaged manual.
  Update it when resident ownership, programmable projection, or the relation to
  the intrinsic producer changes.
  Capability mentions in any document require explicit bidirectional
  related_files mapping to the implementing code (see root ## Maintenance).
---
# Telegram Task Card Projection Anatomy

This package owns Telegram's provider adapter and programmable projection. The
route/slot/delivery state machine is shared under `mcp_servers/task_card/`; the
local `resident.py` remains a compatibility re-export. The public model-facing
`task_card` capability has moved to
[`src/lingtai/tools/task_card/`](../../../tools/task_card/ANATOMY.md), which
produces the agent-local artifact. Telegram reads that artifact and projects it
onto its one tracked resident Task Card target per account+chat.

## Components

- `resident.py` — compatibility re-export of the shared `TaskCardResident`,
  `TaskCardResidentTransport`, and `TaskCardRoute` symbols.
- `../../task_card/resident.py` — provider-neutral route, dual-slot composition,
  route locks, commit-after-success, edit/rotation/delete/send/persist state
  machine, and explicit partial/indeterminate outcomes.
- `manager.py` — the Telegram adapter that tails `events.jsonl`, supplies safe
  events to the shared projection core, and implements compound-ID binding,
  high-water supersession, Telegram API classification, real transport,
  resident persistence, and programmable file projection callbacks.
  `_taskcard_display_expression()` reads the durable declarative display
  expression from `TelegramService` at each automatic projection tick
  (`_broadcast_task_card_event_window`, `_ensure_task_card_resident`) and
  passes it into `TaskCardEventProjection.render_event_groups`.
- `service.py` — besides the enabled/normal_rows/max_refreshes/locale
  presentation preferences, owns the durable `display_expression` field of
  `<agent-workdir>/telegram/taskcard.json`: `taskcard_display_expression()` /
  `set_taskcard_display_expression()`, validated through
  `TaskCardEventProjection.validate_display_expression`, and
  `_maybe_reload_taskcard_state()`, which hot-reloads the whole file (bounded
  to one `stat` per call, re-parsing only on a changed mtime) so a direct
  atomic external edit becomes visible at the next projection tick without a
  process restart. Every persistence setter also calls
  `_maybe_reload_taskcard_state()` under `self._taskcard_lock` before
  deriving the siblings it writes back, so a setter invoked with no
  preceding getter can never overwrite an unseen external edit with a stale
  in-memory copy of the other fields.
- `../../task_card/event_projection.py` — the channel-neutral pure core for safe
  event allowlisting, redaction, API-call grouping, budgets, metadata, and text
  rendering, including compact per-call output/thinking/cache metrics from the
  normalized current-call carrier or `llm_response` fallback. It owns no journal
  I/O, route, resident, or transport state.
  `DISPLAY_SLOTS`/`DEFAULT_DISPLAY_EXPRESSION`/`validate_display_expression`/
  `compose_display` define and enforce the small declarative display-expression
  grammar: an ordered, allowlisted selection of the fragments
  (`header`/`rows`/`blank`/`footer`/`divider`/`metadata`/`time`/`ask_agent`)
  `format_rows_task_card_text` already renders, never arbitrary interpolated
  data.
- `SKILL.md` — packaged Telegram-facing manual/procedure material for this
  component.
- Retained legacy files in this package (`controller.py`, `_family.py`,
  `interface.py`, `__init__.py`) are no longer the public ownership path for
  `task_card` in this slice. They remain on disk because this migration does not
  delete or rename pre-existing paths.

## Connections

- The intrinsic producer writes `<workdir>/taskcard/status` and
  `<workdir>/taskcard/taskcard.md`.
- `TelegramManager` alone tails `<workdir>/logs/events.jsonl`; it delegates only
  pure event projection/grouping/rendering to `TaskCardEventProjection` and
  keeps the existing private helpers as compatibility wrappers.
- `TelegramManager` constructs `TaskCardResidentTransport` with dynamic provider
  callbacks. The shared core never imports Telegram, reads its state file, or
  classifies Bot API errors.
- `TelegramManager._broadcast_programmable_task_card_file()` reads
  `taskcard/status` first: exact `active` reads the body and projects it
  (diff-only against the last committed programmable frame); exact `inactive`
  calls `_clear_programmable_task_card_frame()` to exclude only the
  programmable frame from the resident, idempotently; any other status is
  unchanged.
- The shared `TaskCardResident` composes the programmable frame with the existing
  automatic frame under one tracked resident message and serializes delivery.

## Composition

- Parent: [`src/lingtai/mcp_servers/ANATOMY.md`](../../ANATOMY.md)
- Paired contract: [`CONTRACT.md`](CONTRACT.md)
- Producer owner: [`src/lingtai/tools/task_card/ANATOMY.md`](../../../tools/task_card/ANATOMY.md)
- Shared projection core: [`src/lingtai/mcp_servers/task_card/event_projection.py`](../../task_card/event_projection.py)
- Shared resident core: [`src/lingtai/mcp_servers/task_card/resident.py`](../../task_card/resident.py)

## State

- In-memory resident channel frames and per-route delivery locks owned by the
  shared core instance
- Durable Telegram resident message ids in each account's `task_cards` map
- Durable agent-wide presentation preferences (`taskcard` enabled,
  `normal_rows`, `max_refreshes`, `locale`, `display_expression`) in
  `<agent-workdir>/telegram/taskcard.json`, owned by `TelegramService`;
  hot-reloaded (mtime-bounded) so an external edit lands at the next
  automatic projection tick. This file is distinct from the bootstrap
  `.secrets/telegram.json` account/token config, which never carries
  presentation settings.
- No programmable renderer state of its own; producer state lives under
  `<workdir>/taskcard/`

## Notes

- Missing, unreadable, or `active`-with-blank/missing-body producer state is a
  Telegram no-op that preserves the last good projected programmable frame.
- Exact `inactive` producer state instead excludes only the programmable frame
  from the resident (idempotently); it never touches the resident message, the
  automatic frame, or the local producer body.
- Telegram-specific transport, diff-only updates, and toggle behavior belong
  here, not in the intrinsic producer contract.
