---
name: read-manual
description: "File pagination, truncation and long-line recovery: `next_offset` continuation, `line_truncated`, default/hard-cap limits, and when to switch to bash/grep/sed instead. Use when ordinary `file.read` cannot expose complete content."
version: 0.2.0
tags: [read, files, continuation, truncation, cap, pagination]
last_changed_at: "2026-07-19T00:00:00Z"
related_files:
- src/lingtai/tools/file/_read.py
- src/lingtai/tools/file/__init__.py
- src/lingtai/tools/file/manual/SKILL.md
maintenance: |
  Tracks the tool/capability behavior it teaches; update when that tool's behavior changes.
---

# Read Manual

Complete workflow for reading files with the `file` tool's `read` action. Load
it for large files, complete-content workflows, truncation, or `line_truncated`
results.

This is a nested reference under `file-manual`, not a separate manual action:
`file(action="manual")` returns `file-manual`, which points here for read depth.
`file-manual` owns basic action choice, the UTF-8 policy, the `summarize`
guidance, and the manual-versus-ordinary-call rule (including that repeating an
identical manual call is an error loop, not progress).

## Two caps

| Cap | Value | Configurable |
|---|---|---|
| `read` per-call page budget | **100 000 chars** (default) | yes, via per-call `max_chars` |
| Runtime tool-result hard ceiling | **200 000 chars** | no — not by agents or prompts |

`max_chars` requests a smaller or larger chunk for one call. Values above the
hard ceiling are clamped to 200 000; the effective value appears as `cap_chars`
when the result is truncated.

These two caps act at different layers:

1. **Read-level pagination** — exceeding the effective per-call budget returns
   `truncated=true` plus continuation metadata. You page on with `next_offset`.
2. **Runtime preventive ceiling** — `ToolExecutor` applies the non-configurable
   200k cap to every tool result just before it reaches the LLM wire. A result
   still over the ceiling is written to `<workdir>/tmp/tool-results/<…>` and
   replaced on the wire by a compact manifest containing `status="spilled"`,
   `spill_path`, `artifact`, `preview`, and `original_char_count`.

A well-formed `read` result normally stays under the outer ceiling because
`max_chars` is clamped to 200k. If you still see a spill manifest from `read`,
inspect the `spill_path` artifact, then re-call `read` with a smaller
`limit`/`max_chars` or process the artifact via `bash`/`grep`/Python.

## Metadata/stats preflight

For unknown or large files, inspect cheap metadata before reading big chunks.
This replaces a dedicated `read(dry_run=true)` mode.

```bash
python - <<'PY'
from pathlib import Path
p = Path('/path/to/file')
count = max_len = max_line = 0
with p.open('r', encoding='utf-8', errors='replace') as f:
    for i, line in enumerate(f, 1):
        count = i
        if len(line) > max_len:
            max_len, max_line = len(line), i
print({'bytes': p.stat().st_size, 'lines': count,
       'longest_line': max_line, 'longest_chars': max_len})
PY
```

Use the result to choose the window: `offset` (1-based start/resume), `limit`
(lines requested), `max_chars` (per-call budget).

## Complete-content workflow

For any file that may exceed the cap, page with `offset=next_offset` and the
same `limit` until `truncated` is absent or false:

```python
offset = 1
while True:
    r = file(action="read", input={"file_path": path, "offset": offset, "limit": 200},
             reasoning="page through the file")
    process(r["content"])
    if not r.get("truncated"):
        break
    offset = r["next_offset"]
```

## Continuation metadata fields

When `truncated=true` the result includes:

| Field | Meaning |
|---|---|
| `truncated` | `true` — content was cut |
| `cap_chars` | effective character cap used for this call |
| `returned_chars` | characters actually returned |
| `requested_offset` | 1-based start line you passed |
| `requested_limit` | line limit you passed |
| `last_returned_line` | 1-based line number of the last line shown |
| `next_offset` | pass this as `offset` on the next call to continue |
| `remaining_lines_estimate` | approximate lines still unread |
| `line_truncated` | `true` only when a single physical line exceeded the cap |

## Handling line_truncated=true

`line_truncated=true` appears when a single physical line is longer than the cap.
Then:

- The result contains only a **prefix** of that line (bounded by the cap).
- `next_offset` points to the **next line**, not to a mid-line continuation.
- The hidden tail of the long line is **not recoverable** through further `read`
  calls.

To inspect a long line fully, use targeted local processing instead of `read`:

```bash
sed -n '42p' /path/to/file                        # print one specific line
awk '{print NR, length($0)}' /path/to/file | head -20   # characters per line
grep -n "pattern" /path/to/file                   # search within a long line
```

## Quick checklist

Before calling `read`:

- Large file? Probe with `limit=100`–`200`, or run the preflight above.
- Need the whole file? Use the continuation loop.
- Escape hatches: `line_truncated=true` → `bash`/`grep`/`sed`; `status=spilled`
  → read the `spill_path` artifact or reduce `limit`.
- Need a specific region? Pass `offset` and a tight `limit`.

## Manual versus ordinary reads

`file-manual` owns this rule: `file(action="manual")` is a one-time entry.
After it returns, continue the original task with an ordinary read; repeating the
same manual call is an error loop, not progress.
