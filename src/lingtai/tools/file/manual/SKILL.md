---
name: file-manual
description: "Read/write/edit/glob/grep guide for LingTai's `file` tool: safe write/edit discipline, explicit non-UTF-8 workflows, SHOW-only File policy, and pagination routing to read-manual."
version: 0.4.0
tags: [files, read, write, edit, grep, glob, settings, encoding, utf-8]
last_changed_at: "2026-08-29T00:00:00Z"
related_files:
- src/lingtai/tools/file/__init__.py
- src/lingtai/tools/file/CONTRACT.md
- src/lingtai/tools/file/_read.py
- src/lingtai/tools/file/_write.py
- src/lingtai/tools/file/_edit.py
- src/lingtai/tools/file/_glob.py
- src/lingtai/tools/file/_grep.py
- src/lingtai/tools/file/settings.py
- src/lingtai/services/file_io.py
- src/lingtai/services/file_io_sidecar.py
- ENVIRONMENT_VARIABLES.md
- src/lingtai/intrinsic_skills/read-manual/SKILL.md
maintenance: |
  Tracks the tool/capability behavior it teaches; update when that tool's behavior changes.
---

# File Manual — Router

`file` is one model-facing tool over the granted working tree. It has seven
canonical actions: `read`, `write`, `edit`, `glob`, `grep`, `settings`, and
`manual`; the retired names are not public aliases. Use this router for a safe
first call, then open only the deeper guidance needed for the task.

## First call

Every call uses the closed envelope below. `action` selects one operation,
`input` contains only that action's fields, `reasoning` is required audit
metadata, and root `summarize` is optional presentation control.

```python
file(action="read", input={"file_path": "/abs/path/file.txt", "offset": None,
                            "limit": None, "max_chars": None},
     reasoning="read the selected text file")
```

`file_path` may be absolute or relative to the agent working directory. Relative
paths remain under that canonical root. A JSON `null` optional value means
absent: read uses offset **1** (1-based), limit **2000** lines, and
`max_chars` **100 000** characters; grep uses path as the working directory,
`glob` as no filter, and `max_matches` **200**; edit uses `replace_all=false`.

| Need | Call | Result / boundary |
|---|---|---|
| Read text | `action="read"` with `file_path` | Numbered UTF-8 lines; a capped page can be continued with `next_offset`. |
| Create or replace a file | `action="write"` with `file_path`, `content` | Full UTF-8 text write; inspect the receipt. |
| Make an exact change | `action="edit"` with `file_path`, `old_string`, `new_string` | Exact replacement; ambiguity or a missing match leaves the file untouched. |
| Find names | `action="glob"` with `pattern` | Sorted matches; a budget-limited traversal is marked partial. |
| Search contents | `action="grep"` with regex `pattern` | Text matches; glob filters prune before file reads and traversal limits are reported. |
| Inspect policy | `action="settings", input={}` | Read-only complete SHOW inventory; no set/reset form. |
| Load guidance | `action="manual", input={}` | The installed `file-manual` body; no target-file I/O. |

## Boundaries to keep visible

- **Text and encoding:** File reads and writes UTF-8 text only. It does not
  inspect binary, image, or audio formats. For a known non-UTF-8 external file,
  use `bash` with an explicit encoding (`Path(...).read_text(encoding="gbk",
  errors="replace")` or another known codec), and convert durable project files
  to UTF-8 with `iconv` before storing them.
- **Mutations:** `write` creates parent directories and returns
  `{status: "ok", path, bytes}`; `edit` returns
  `{status: "ok", replacements}`. Read the target before an important
  overwrite, use a unique exact match, and set `replace_all=true` only when all
  matches are intended. Neither action reloads or changes the current system
  prompt. A durable prompt-source edit takes effect only at the next canonical
  reconstruction; call `context(action="rebuild", input={}, ...)` explicitly
  when immediate activation is required, never as a side effect of ordinary
  source edits.
- **Read continuation:** A successful result can still be partial. If it has
  `truncated=true`, pass its `next_offset` as the next read's `offset` and
  continue until truncation is absent. `line_truncated=true` means a physical
  line exceeded the cap: only its bounded prefix was returned and the hidden tail
  cannot be recovered by another offset. Use the deep [read-manual
  reference](../../../intrinsic_skills/read-manual/SKILL.md) for cap math,
  metadata preflight, spill artifacts, complete-content loops, and targeted
  `bash`/`sed`/`grep` escape hatches.

## Presentation and manual entry

`read`, `grep`, and `glob` can be bulky; `summarize=true` is useful when the
exact payload is not needed. Keep it false for `write` and `edit` receipts and
for `manual` procedure text. Summarization does not alter the raw result.

Call `file(action="manual", input={})` once before a careful or unfamiliar
workflow. It returns the package-owned body from the established public install
path `capabilities/file-manual/SKILL.md`; its result performs no target-file
operation. After it returns, continue with the requested action instead of
repeating the identical manual call.

## Settings — SHOW only

`action="settings"` accepts strict empty input and returns exactly the complete
five-field rows `key`, `current`, `default`, `configurable`, `comment`, in order.
It never writes a settings file or changes policy. The first eleven rows are
immutable File policy; only the two construction-time backend rows are
configurable before a new service/Agent is built. Current construction values
are snapshotted at bind time; SHOW does not reread ambient environment.

The following stable headings are the exact anchors used by the inventory's
`comment` fields. Each heading gives the row's meaning, source/timing, and
change boundary; the deeper implementation remains in the linked source files
in this manual's frontmatter.

### read default line limit

`read.default_line_limit` is **2000** lines. Null or omitted `read.limit` uses
this immutable source default; a per-call limit does not reconfigure it.

### read default max chars

`read.default_max_chars` is **100000** characters. Null or invalid `max_chars`
uses this immutable per-call budget; a call may narrow or raise its own cap.

### read runtime max chars

`read.runtime_max_chars` is the fresh effective ceiling
`min(FileIOPort.max_result_chars, 200000)`, or **200000** without a positive Host
cap. It is observed on each SHOW and has no File owner change procedure.

### glob max results

`glob.max_results` is the immutable **2000**-match traversal limit from the
canonical File service; there is no File-level writer.

### grep default max matches

`grep.default_max_matches` is the immutable **200** match default. A call's
`max_matches` applies only to that invocation.

### grep max file bytes

`grep.max_file_bytes` is the immutable **4194304**-byte scan limit; larger files
are skipped and traversal evidence reports that fact.

### search max visited

`search.max_visited` is the immutable **20000**-entry limit for a recursive
search traversal.

### search walltime seconds

`search.walltime_seconds` is the immutable **8.0**-second traversal budget.

### search excluded directories

`search.excluded_directories` is the sorted immutable `DEFAULT_EXCLUDED_DIRS`
list, pruned during ordinary recursive search.

### search sidecar timeout seconds

`search.sidecar_timeout_seconds` is the immutable **30.0**-second timeout for a
short native sidecar request.

### text encoding

`text.encoding` is the immutable **utf-8** File policy. Use the explicit encoding
workflow above for external non-UTF-8 files; File does not guess locale codecs.

### backend mode

`backend.mode` reports the normalized mode (`auto`, `rust`, or `python`) captured
when the service was constructed. An explicit factory argument wins, then
`LINGTAI_FILE_IO_BACKEND`, then `auto`; change it only before constructing a
new Agent/service. Later environment changes do not alter SHOW.

### backend sidecar

`backend.sidecar` is one sensitive construction-time override. The canonical
`LINGTAI_FILE_IO_SIDECAR` wins over legacy `LINGTAI_SEARCH_SIDECAR`; both current
and default are always `<redacted>`, so no executable path is disclosed. Set the
canonical source before constructing a new Agent; `python` mode does not use a
sidecar.

## What to load next

- Large or complete reads, cap calculations, `next_offset`, `line_truncated`,
  or spill recovery → [read-manual](../../../intrinsic_skills/read-manual/SKILL.md).
- Non-UTF-8 input or binary inspection → the explicit `bash`/Python workflow in
  **Boundaries to keep visible**, with a domain tool for media.
- Settings source truth and construction precedence → the anchored **Settings**
  headings above, then `settings.py` and `file_io_sidecar.py` when source-level
  evidence is required.
- Search or edit discipline → the action table and **Mutations** boundary;
  operation-specific result/error details are normative in `file/CONTRACT.md`.
