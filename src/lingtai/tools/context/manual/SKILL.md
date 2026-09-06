---
name: context-manual
description: |
  Read before summarize/rebuild/molt, session journaling, consequential handoff or context-loss recovery.
version: 2.1.2
last_changed_at: "2026-09-04T00:00:00Z"
related_files:
- src/lingtai/tools/context/__init__.py
- src/lingtai/tools/context/_molt.py
- src/lingtai/tools/context/_session_journal.py
- src/lingtai/tools/system/summarize.py
- src/lingtai/agent.py
- src/lingtai/tools/psyche/CONTRACT.md
maintenance: |
  This package is the canonical Context manual source and runtime-installed owner.
  Update it when the tool/capability behavior changes.
---

# Context Manual

This manual is the router for `context` operations — `molt`, `summarize`, and `rebuild`. Keep routine guidance here; load the supporting asset or reference only when you need the full scaffold or the detailed summarize/rebuild procedure.

## Reference catalog

| Reference | When to load |
|---|---|
| `reference/summarize-manual/SKILL.md` | Compacting bulky tool results with `context(action='summarize')`, applying them during a full `context(action='rebuild')`, recovery by `tool_call_id`, and summarize-versus-molt tradeoffs |

## Full context rebuild

`context(action="rebuild", ...)` is the **one active full reconstruction operation**: recompose every canonical prompt source → apply pending summaries → provider replay. The tool description and the `rebuild` schema carry the exact contract, including that bare `{}` is valid.

The fact that is only here: **generic durable mutations do not hot-load.** Write with `file.write`/`file.edit`, then rebuild when the change must apply now — that is the only mutation path for all four durable stores (no per-store append, info, or reload action). Passive refresh and molt invoke the same internal reconstruction contract with their own lifecycle effects. Do not loop rebuild.

## Asset catalog

| Asset | When to load | What it contains |
|---|---|---|
| `assets/session-journal-entry-template.md` (read from this skill directory) | Whenever you write the molt-history record for a session segment before a molt | Frontmatter + section template for a `knowledge/session-journal/<YYYY-MM-DD>-molt-<molt-count>-<slug>/KNOWLEDGE.md` entry |
| `assets/molt-template.md` (read from this skill directory) | Consequential molt, long-running task, multiple collaborators, pending human commitments, open worktrees/artifacts, active background jobs, or any successor briefing that would be risky to improvise | 9-section summary scaffold plus pre-molt verification checklist |

## 1. Molt Overview

Molt is yours to perform. The covenant teaches the philosophy (§V); this is the recipe.

Save anything you need to pad, lingtai, knowledge, and skills beforehand, then molt. Molt is not routine housekeeping: do it once context pressure (≥85%), an explicit human request, or conversation confusion actually makes the fresh briefing worth its cost (see `reference/summarize-manual/SKILL.md`) — not merely because the window still has room. Once a molt is actually warranted, tending the stores and molting promptly still saves tokens over a rushed, late one.

**Tend the stores first, every time.** The four stores are the real persistence and the summary is only the briefing on top of them: molt without tending them and the next you wakes with the briefing alone — no character evolution, no pad state, no new knowledge, no new skills.

## 2. Store-Tending Rhythm

For `lingtai` and `knowledge`, tending happens *once* per task, at the end — not mid-task. Hold updates in your head while working, then commit them in a single pass before going idle (or before molting). Mid-task edits create noise and waste tokens. The exception is a long-running task where a crash would genuinely destroy work — checkpoint deliberately in that case.

Pad has a different rhythm — update it whenever the index meaningfully changes. See §5 below.

All four durable stores are taught by manuals loaded through the one `psyche` root (pad + lingtai + knowledge + skills = psyche); generic mutation belongs to `file`. §3 below is the per-store how-to.

## 3. Step 1 — Tend the Four Durable Stores and Session Journal

- **lingtai** — carry forward your complete identity in `system/lingtai.md` using `file.write` (full rewrite) or `file.edit` (exact replacement). Read `psyche(action="lingtai", input={}, reasoning="...")` for forced/self-evolve behavior.
- **pad** — keep the living index in `system/pad.md` via `file.write`/`file.edit`; edit `system/pad_append.json` the same way for the durable pinned-reference list. See `psyche(action="pad", input={}, reasoning="...")`.
- **knowledge** — write to `knowledge/<name>/KNOWLEDGE.md` for long-term private context using `file.write`/`file.edit`.
- **skills** — write `.library/custom/<name>/SKILL.md` (with YAML frontmatter: `name`, `description`, `version`) for any reusable procedure the next you (or a peer) might need, then rebuild to refresh the catalog. Share by sending the skill source/artifact so peers install it into their own `.library/custom/<name>/` and refresh; use `../.library_shared/<name>/` only as an explicit opt-in local-network shared root.
- **session journal** — append a substantial sub-entry under `knowledge/session-journal/` describing what you did this session. See §4 for the full practice.

All five happen *before* the molt call. They are not optional. Without them, the molt sheds everything.

## 4. Session Journal

The four stores capture *who you are*, *what you're working on*, *verifiable truths*, and *reusable procedures*. None of them captures the *story* of a session. The session journal is that missing layer — it is also your **molt history**: each sub-entry is the record of one session segment that you write *before* you molt, so the chain of entries reconstructs how you got here across many molts.

Write it as a **routing parent with sub-knowledge children** under
`knowledge/session-journal/` — the routing/index shape from the knowledge manual's
"Nesting and sub-knowledge" section. Do **not** create each session as its own
top-level knowledge entry; that floods the catalog. The parent is routing-only;
the children carry the substance:

```
knowledge/session-journal/
├── KNOWLEDGE.md                                            # top-level routing/index ONLY
├── 2026-05-13-molt-7-nudge-service/KNOWLEDGE.md            # sub-knowledge — one session
├── 2026-05-13-molt-8-procedures-to-kernel/KNOWLEDGE.md     # sub-knowledge — same day
└── 2026-05-14-molt-9-wechat-fixes/KNOWLEDGE.md             # sub-knowledge — ...
```

Because `session-journal/` has its own `KNOWLEDGE.md`, the knowledge scanner
treats it as a single entry and does **not** descend into the children — they are
reachable only through the parent index. That is why the parent must list every
child explicitly. See the knowledge manual's "Nesting and sub-knowledge" section
(`.library/intrinsic/capabilities/knowledge/SKILL.md`) for the structural rule.

The directory name is `<YYYY-MM-DD>-molt-<molt-count>-<slug>`. Read `<molt-count>` from your resident system prompt's identity section — "You have undergone N molts since birth." Use that N: the entry records the pre-molt segment. Embedding the count keeps chronology stable when you molt more than once on the same date, which the date alone cannot order.

**The parent `knowledge/session-journal/KNOWLEDGE.md` is routing-only** — short,
scannable, progressive-disclosure. It is a table of contents, not a journal. One
line per sub-entry: date, slug, one-sentence hook, and the child's *relative* path
(`2026-05-13-molt-7-nudge-service/KNOWLEDGE.md`), never an absolute local path.
Never let narrative leak into the parent — if a line grows past its hook, the
detail belongs in the child.

**The sub-entry `<YYYY-MM-DD>-molt-<molt-count>-<slug>/KNOWLEDGE.md` is the substance** — write it as the molt-history record of the segment, *before* you molt, via `write`/`edit` directly. Read `assets/session-journal-entry-template.md` from this skill directory for the frontmatter (including `molt_count`, the required `type: session-journal` marker, and the YAML block-scalar `description` that keeps a `: ` in the text from breaking the gate) and the section layout. It is a journal, not a transcript. Several thousand tokens is fine when the segment was rich; keep it concise when it was small.

This sub-entry's path is what you pass as `context(action='molt', input={'session_journal_path': ...})`, and the kernel validates it before letting the molt proceed (see §6).

Updating the parent index at each session is part of the practice — append one line referencing the new sub-entry. Then write the successor summary (§6), which points back at this entry's path.

## 5. Tending the Pad

**Pad has its own manual — read `psyche(action="pad", input={}, reasoning="load Pad guidance")` for the full practice.** It owns what belongs in the pad and what does not, the tending rhythm, how the `system/pad_append.json` pinned list works, and how to archive a completed pad.

What matters here is only the molt-relevant fact: pad is one of the four durable stores, it survives the molt and is reloaded into the fresh session's system prompt, so it must be accurate **before** you molt. A stale pad is the fastest way to make the next you lose the thread.

Your 灵台 is likewise taught by a manual — call `psyche(action="lingtai", input={}, reasoning="load identity guidance")` for identity modes and file/rebuild guidance.

## 6. Step 2 — Write the Summary and Molt

```
context(
    action="molt",
    input={
        "summary": <your charge to the next you>,
        "session_journal_path": "knowledge/session-journal/<entry>/KNOWLEDGE.md",
        "keep_tool_calls": null,
        "keep_last": null,
    },
    reasoning="why you are molting now",
)
```

Every `context` call uses this one envelope: a single `action`, that action's
own strict `input` object, and a root `reasoning`. The four actions are `molt`,
`summarize`, `rebuild`, and `manual`. Leave the root `summarize` **boolean**
false: `context` results are small (short-result profile), and summarizing a
`manual` call would drop the exact procedure you called it for.

(The action-`summarize` versus root-`summarize` distinction is stated in the
resident tool description; no action takes `summarize` as input.)

`context` owns only your context. Your name is `system(action='name_set')` /
`system(action='name_nickname')`. Your four durable stores share one read-only
root. `psyche(action="pad"|"lingtai"|"knowledge"|"skills"|"manual", input={},
reasoning="...")` returns the matching manual;
`psyche(action="settings", input={}, reasoning="...")` returns the two fully
redacted Psyche-owned Pad configuration rows. Use `file.write`/`file.edit` for
durable files, then rebuild explicitly when needed.

**Required pre-molt order (enforced by the kernel):** write the session journal
sub-entry first (§4) → pass its path as `session_journal_path` → the kernel
validates it → only then does the molt proceed. `session_journal_path` is a
**mandatory** structured argument for agent-initiated molt. If it is missing or
the journal fails validation, the molt is **refused before any context is shed**
and your `molt_count`/history are untouched — you get an actionable recovery
message instead. The validator (a signpost, not a grader) checks what the schema
describes: path inside your workdir resolving to
`knowledge/session-journal/<entry>/KNOWLEDGE.md` (the per-segment sub-entry, not
the parent index and not a scratch file), non-empty UTF-8, frontmatter with
`name` + `description`, and the marker `type: session-journal` or
`session_journal: true` — see the template in §4.

The accepted path is recorded in the molt result, the persisted summary
frontmatter (`session_journal_path:`), and the post-molt notification, so later
recovery and audits can see which journal backed each molt.

The `summary` is the only *conversation-layer* thing the next you will see. Aim for the ~10,000 tokens the schema suggests. The summary is not a recap of conversation. It is your charge to the self that comes after you — anchored in the four stores, which are already waiting in the fresh session.

For a routine molt, include:

- **What you are working on** — current task, current state, the next concrete step
- **What you have accomplished** — completed pieces, key decisions made
- **What remains** — pending items, blockers, open questions
- **Who to contact** — collaborators, who is waiting on what
- **Which knowledge entries and skills matter** — paths the next you should load
- **The session journal sub-entry path** — so the next you can read the full narrative
- **Anything else worth carrying forward** — insights, gotchas

Quick routing:

| Need | Use |
|---|---|
| Routine molt | The short bullet list above. |
| Consequential molt / successor handoff — long-running task, multiple collaborators, pending human commitments, open worktrees/artifacts, or any handoff the next you could not reconstruct quickly | Read `assets/molt-template.md` from this skill directory; use its full scaffold and checklist. Fill every section; write `None` rather than omitting one. |
| Unsure whether the handoff is complex | Use the asset; extra structure is cheaper than a bad handoff. |

Before you call `context(action="molt", ...)`, verify at minimum:

- The session-journal sub-entry for the just-finished segment exists and was
  written *before* the summary (§4) — it is the narrative the summary points
  back to, and its path is the validated `session_journal_path`.
- Durable stores were tended before the summary was written.
- The first five minutes after wake are obvious.

`assets/molt-template.md` carries the full pre-molt verification checklist
(outstanding tasks, collaborators, background work, key paths).

**`keep_tool_calls`** — see the schema description for the exact semantics (refusal on an unknown ID, replay order). Keep the list short: the durable stores are the primary persistence.

**`keep_last`** — see the schema description (default 20, backward expansion to keep a tool-call batch whole, `0` to archive everything, dedupe against `keep_tool_calls`).

## 7. Context Pressure Reminder

Context pressure is agent state, not a dismissible notification. Tool results surface a natural-language reminder under `_meta.agent_meta.agent_state.context.molt` only after context has stayed high for several consecutive fresh provider rounds (the sustained-pressure threshold is 85%). It rides on the current `agent_meta` snapshot (carried on the designated final result of each batch; restamped there while active) so the reminder persists. The field name is historical: the reminder is a context-pressure action, not an early staged molt order or a machine-readable tag block.

When this reminder appears, follow the urgent cadence in `reference/summarize-manual/SKILL.md` (which owns the summarize cadence, rebuild semantics, and recovery target). The molt decision is yours: if a batched summarize/rebuild pass still leaves context above 85%, stop repeating summarize, tend durable stores, and molt deliberately. If context falls below 85% but stays above the recovery target, continue only when the current task still needs the carried context; otherwise molt at a natural task boundary.

### Cache-miss budget

The soft **cache-miss token budget** (default 2,000,000, System-owned via `<agent-workdir>/settings/system.json`, cumulative since your last molt, surfaced as `cache miss budget {N} reached, molt now`) is owned by the resident `meta_guidance` token-efficiency guidance. Two details only documented here:

- It is **overridable at runtime** by `LINGTAI_CACHE_MISS_BUDGET` (positive int, read live at every budget resolution, so `env_file` + refresh applies it without a file edit; an invalid or non-positive value silently falls back to the System file, then the fixed default). Legacy `manifest.cache_miss_budget` in `init.json` is ignored. `_meta.agent_meta.agent_state.context` reports the effective `cache_miss_budget` and the current `cache_miss_tokens`. The `system-manual` owns the file format.
- The total **survives a refresh/restart** — it is not the since-refresh runtime delta, so refreshing does not reset the remaining budget. If the sustained context-pressure reminder is also active, both warnings are preserved in `context.molt`.

## 8. Post-Wipe Recovery

If you wake up after a *system-performed* molt (triggered by karma, `.clear`, or operator — NOT by context-pressure reminders), the post-molt notification points at a system-authored summary in `system/summaries/`. All canonical prompt sources (including character and Pad) were reconstructed, and recent conversation may be gone except for any entries the system explicitly kept. To reconstruct:

1. Read the `summary_path` from the post-molt notification
2. `email(check)` — see what arrived while you were down
3. Check `knowledge/session-journal/KNOWLEDGE.md` — your session history index
4. Read the `skills` section of your system prompt — it lists every skill you have
5. `shell({"command": "tail -n 200 logs/events.jsonl | grep ..."})` — surgical reads if needed

Reconstruct your situation from these sources.

If you ever need to retrieve specific prior context, the full activity log is at `logs/events.jsonl` — read tactically (grep/tail/filter), not whole.
