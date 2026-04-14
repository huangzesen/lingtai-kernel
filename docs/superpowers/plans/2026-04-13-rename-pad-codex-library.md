# Kernel Rename: memory→pad, library→codex, skills→library

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename three core concepts in the Python kernel to match the TUI-side rename already completed in the `lingtai` repo: memory→pad, library→codex, skills→library.

**Architecture:** Rename tool names, sub-action names, filesystem paths, prompt section names, i18n keys, function/class/variable names, and tests. One atomic change — the TUI already expects the new names.

**Tech Stack:** Python 3, pytest

**Name collision:** Old `library` capability becomes `codex`, old `skills` capability becomes new `library`. The file `capabilities/library.py` becomes codex logic, `capabilities/skills.py` becomes library logic. Rename files to avoid confusion.

**Key mappings:**
- eigen sub-action `"memory"` → `"pad"`, file `system/memory.md` → `system/pad.md`
- psyche sub-action `"memory"` → `"pad"`, file `system/memory_append.json` → `system/pad_append.json`
- prompt section `"memory"` → `"pad"`, `"skills"` → `"library"`
- tool name `"library"` → `"codex"`, dir `library/library.json` → `codex/codex.json`
- tool name `"skills"` → `"library"`, dir `.skills/` → `.library/`
- capability registration key `"library"` → `"codex"`, `"skills"` → `"library"`
- config field `memory=` → `pad=`

---

### Task 1: Rename capability files and registration

**Files:**
- Rename: `src/lingtai/capabilities/library.py` → `src/lingtai/capabilities/codex.py`
- Rename: `src/lingtai/capabilities/skills.py` → `src/lingtai/capabilities/library.py`
- Modify: `src/lingtai/capabilities/__init__.py`

- [ ] **Step 1: Rename capability files**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git mv src/lingtai/capabilities/library.py src/lingtai/capabilities/codex.py
git mv src/lingtai/capabilities/skills.py src/lingtai/capabilities/library.py
```

- [ ] **Step 2: Update `__init__.py` registry**

In `src/lingtai/capabilities/__init__.py`:
```python
# Old:
"library": ".library",
...
"skills": ".skills",

# New:
"codex": ".codex",
...
"library": ".library",
```

Also in `_USER_FACING`:
```python
# Old:
"library": ".library",

# New:
"codex": ".codex",
"library": ".library",
```

- [ ] **Step 3: Update `codex.py` (was library.py)**

- Tool registration: `agent.add_tool("library", ...)` → `agent.add_tool("codex", ...)`
- Class: `LibraryManager` → `CodexManager`
- Setup function param: `library_limit` → `codex_limit`
- Path: `self._library_json = ... / "library" / "library.json"` → `self._codex_json = ... / "codex" / "codex.json"`
- All `self._library_json` → `self._codex_json`
- Error messages: "Library is full" → "Codex is full"
- Error messages: "Unknown library IDs" → "Unknown codex IDs"
- i18n key prefix: `"library."` → `"codex."` for all `t(lang, "library.xxx")` calls
- Docstrings: update "library" → "codex"
- Function: `get_description` uses `t(lang, "library.description")` → `t(lang, "codex.description")`
- Setup function: `def setup(agent, *, library_limit=None)` → `def setup(agent, *, codex_limit=None)`
- Comment: `memory.edit(files=[...])` → `pad.edit(files=[...])`

- [ ] **Step 4: Update `library.py` (was skills.py)**

- Tool registration: `agent.add_tool("skills", ...)` → `agent.add_tool("library", ...)`
- Path: `agent._working_dir.parent / ".skills"` → `agent._working_dir.parent / ".library"`
- Prompt section: `agent.update_system_prompt("skills", ...)` → `agent.update_system_prompt("library", ...)`
- i18n key prefix: `"skills."` → `"library."` for `t(lang, "skills.xxx")` calls
- Docstrings: update `.skills/` → `.library/`, "skills capability" → "library capability"
- Function names: `_resolve_skills_dir` → `_resolve_library_dir`, `_scan_skills` → `_scan_library`, `_scan_skills_recursive` → `_scan_library_recursive`
- Variable names: `skills_dir` → `library_dir`, `handle_skills` → `handle_library`
- Keep: individual "skill" references (SKILL.md, skill folder, etc.) — items are still "skills"
- Git commit message: `"register: update skills"` → keep as is (describes what's inside)

- [ ] **Step 5: Verify imports work**

Run: `python -c "from lingtai.capabilities import codex, library; print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add -A src/lingtai/capabilities/
git commit -m "refactor: rename library→codex, skills→library capability files"
```

---

### Task 2: Eigen intrinsic — memory→pad

**Files:**
- Modify: `src/lingtai_kernel/intrinsics/eigen.py`

- [ ] **Step 1: Update schema enum**

```python
# Old:
"enum": ["memory", "context", "name"],

# New:
"enum": ["pad", "context", "name"],
```

- [ ] **Step 2: Update dispatch**

```python
# Old:
if obj == "memory":
    ...
    return {"error": f"Unknown memory action: ..."}
...
return {"error": f"Unknown object: {obj}. Use memory, context, or name."}

# New:
if obj == "pad":
    ...
    return {"error": f"Unknown pad action: ..."}
...
return {"error": f"Unknown object: {obj}. Use pad, context, or name."}
```

- [ ] **Step 3: Rename functions and paths**

- `_memory_edit` → `_pad_edit`
- `_memory_load` → `_pad_load`
- `system_dir / "memory.md"` → `system_dir / "pad.md"` (in both functions)
- `agent._log("eigen_memory_edit", ...)` → `agent._log("eigen_pad_edit", ...)`
- `agent._log("eigen_memory_load", ...)` → `agent._log("eigen_pad_load", ...)`
- Prompt section: `write_section("memory", ...)` → `write_section("pad", ...)`
- Prompt section: `delete_section("memory")` → `delete_section("pad")`
- `rel_path = "system/memory.md"` → `rel_path = "system/pad.md"`
- Docstrings: "memory" → "pad", "system/memory.md" → "system/pad.md"

- [ ] **Step 4: Commit**

```bash
git add src/lingtai_kernel/intrinsics/eigen.py
git commit -m "refactor: rename eigen memory→pad sub-action"
```

---

### Task 3: Psyche capability — memory→pad

**Files:**
- Modify: `src/lingtai/capabilities/psyche.py`

- [ ] **Step 1: Update schema enum**

```python
# Old:
"enum": ["lingtai", "memory", "context"],

# New:
"enum": ["lingtai", "pad", "context"],
```

- [ ] **Step 2: Update dispatch table**

```python
# Old:
_VALID_ACTIONS = {
    "lingtai": {"update", "load"},
    "memory": {"edit", "load", "append"},
    "context": {"molt"},
}

# New:
_VALID_ACTIONS = {
    "lingtai": {"update", "load"},
    "pad": {"edit", "load", "append"},
    "context": {"molt"},
}
```

Note: the dispatch uses `getattr(self, f"_{obj}_{action}")` so method names must match: `_pad_edit`, `_pad_load`, `_pad_append`.

- [ ] **Step 3: Rename methods**

- `_memory_edit` → `_pad_edit`
- `_memory_append` → `_pad_append`
- `_memory_load` → `_pad_load`

- [ ] **Step 4: Update paths and constants**

- `_APPEND_LIST_PATH = "system/memory_append.json"` → `_APPEND_LIST_PATH = "system/pad_append.json"`
- In `_pad_edit`: `"object": "memory"` → `"object": "pad"` (delegation to eigen)
- In `_pad_load`: `"object": "memory", "action": "load"` → `"object": "pad", "action": "load"` (delegation to eigen)
- Prompt section: `read_section("memory")` → `read_section("pad")`
- Prompt section: `write_section("memory", ...)` → `write_section("pad", ...)`

- [ ] **Step 5: Update docstrings and comments**

- Module docstring: "memory.edit", "memory.append", "memory.load" → "pad.edit", "pad.append", "pad.load"
- "system/memory_append.json" → "system/pad_append.json"
- "Library is a separate standalone capability" → "Codex is a separate standalone capability"
- Class docstring: "Identity, memory, and context manager" → "Identity, pad, and context manager"
- Setup docstring: "identity, memory, and context management" → "identity, pad, and context management"

- [ ] **Step 6: Update setup function**

- `agent._eigen_owns_memory` → `agent._eigen_owns_pad`
- Comment: "Auto-load character and memory" → "Auto-load character and pad"
- Comment: "Register post-molt hook to reload character + memory" → "character + pad"

- [ ] **Step 7: Commit**

```bash
git add src/lingtai/capabilities/psyche.py
git commit -m "refactor: rename psyche memory→pad sub-action"
```

---

### Task 4: Base agent and workdir — memory→pad

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py`
- Modify: `src/lingtai_kernel/workdir.py`
- Modify: `src/lingtai_kernel/prompt.py`

- [ ] **Step 1: Update `base_agent.py`**

- Constructor param: `memory: str = ""` → `pad: str = ""`
- Comment: `"Set by psyche capability to prevent stop() from overwriting memory.md"` → `pad.md`
- `self._eigen_owns_memory = False` → `self._eigen_owns_pad = False`
- `memory_file = system_dir / "memory.md"` → `pad_file = system_dir / "pad.md"` (both in __init__ and stop)
- `if memory and not memory_file.is_file():` → `if pad and not pad_file.is_file():`
- `memory_file.write_text(memory)` → `pad_file.write_text(pad)`
- `loaded_memory = ""` → `loaded_pad = ""`
- `loaded_memory = memory_file.read_text()` → `loaded_pad = pad_file.read_text()`
- `if loaded_memory.strip():` → `if loaded_pad.strip():`
- `write_section("memory", loaded_memory)` → `write_section("pad", loaded_pad)`
- In stop(): `if not self._eigen_owns_memory:` → `if not self._eigen_owns_pad:`
- `read_section("memory")` → `read_section("pad")`
- `memory_content` → `pad_content`
- All `memory_file` → `pad_file`

- [ ] **Step 2: Update `workdir.py`**

- `memory_file = system_dir / "memory.md"` → `pad_file = system_dir / "pad.md"` (both occurrences)
- `memory_file.is_file()` → `pad_file.is_file()`
- `memory_file.write_text("")` → `pad_file.write_text("")`

- [ ] **Step 3: Update `prompt.py`**

```python
# Old:
_DEFAULT_ORDER = ["principle", "covenant", "rules", "tools", "procedures", "brief", "skills", "identity", "memory", "comment"]

# New:
_DEFAULT_ORDER = ["principle", "covenant", "rules", "tools", "procedures", "brief", "library", "identity", "pad", "comment"]
```

- [ ] **Step 4: Verify import**

Run: `python -c "import lingtai_kernel; print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add src/lingtai_kernel/base_agent.py src/lingtai_kernel/workdir.py src/lingtai_kernel/prompt.py
git commit -m "refactor: rename memory→pad in base_agent, workdir, prompt"
```

---

### Task 5: i18n — all 6 locale files

**Files:**
- Modify: `src/lingtai_kernel/i18n/en.json`, `zh.json`, `wen.json`
- Modify: `src/lingtai/i18n/en.json`, `zh.json`, `wen.json`

- [ ] **Step 1: Update `lingtai_kernel/i18n/en.json`**

- `eigen.object_description`: `"memory: your working notes (system/memory.md)"` → `"pad: your working notes (system/pad.md)"`
- `eigen.action_description`: `"memory: edit | load"` → `"pad: edit | load"`
- `eigen.content_description`: `"Text content for memory edit."` → `"Text content for pad edit."`
- `eigen.context_forget_summary`: `"library"` → `"codex"`

- [ ] **Step 2: Update `lingtai/i18n/en.json`**

Psyche keys:
- `psyche.object`: `"memory: your working notes (system/memory.md)"` → `"pad: your working notes (system/pad.md)"`
- `psyche.action`: `"memory: edit | load | append"` → `"pad: edit | load | append"`
- `psyche.content`: `"For memory edit"` → `"For pad edit"`, `"memory.md"` → `"pad.md"`
- `psyche.files`: `"For memory edit"` → `"For pad edit"`, `"For memory append"` → `"For pad append"`, `"appended to your memory"` → `"appended to your pad"`

Library→codex keys (rename ALL `"library.*"` keys to `"codex.*"`):
- `library.description` → `codex.description`
- `library.action` → `codex.action` (also update text: `psyche(memory, edit, files=[...])` → `psyche(pad, edit, files=[...])`)
- `library.title` → `codex.title`
- `library.summary` → `codex.summary`
- `library.content` → `codex.content`
- `library.supplementary` → `codex.supplementary`
- `library.ids` → `codex.ids`
- `library.pattern` → `codex.pattern`
- `library.limit` → `codex.limit`
- `library.depth` → `codex.depth`

Skills→library keys (rename ALL `"skills.*"` keys to `"library.*"`):
- `skills.description` → `library.description` (also update text: `.lingtai/.skills/` → `.lingtai/.library/`)
- `skills.action` → `library.action`
- `skills.preamble` → `library.preamble`

Avatar key:
- `avatar.type`: `"memory, and library"` → `"pad, and codex"`

- [ ] **Step 3: Update zh.json and wen.json (both packages)**

Same key renames and value updates with Chinese/文言 equivalents:
- 记忆 → 手记 (for pad), 知识库 → 典集 (for codex, zh), 藏经阁 → 典 (for codex, wen)
- system/memory.md → system/pad.md
- library/library.json → codex/codex.json
- .skills/ → .library/

- [ ] **Step 4: Validate JSON**

Run: `python3 -c "import json; [json.load(open(f)) for f in ['src/lingtai_kernel/i18n/en.json','src/lingtai_kernel/i18n/zh.json','src/lingtai_kernel/i18n/wen.json','src/lingtai/i18n/en.json','src/lingtai/i18n/zh.json','src/lingtai/i18n/wen.json']]; print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add src/lingtai_kernel/i18n/ src/lingtai/i18n/
git commit -m "feat(i18n): rename memory→pad, library→codex, skills→library"
```

---

### Task 6: Tests — update all test files

**Files:**
- Modify: `tests/test_eigen.py`
- Modify: `tests/test_memory.py` (consider renaming to `test_pad.py`)
- Modify: `tests/test_psyche.py`
- Modify: `tests/test_library.py` (consider renaming to `test_codex.py`)
- Modify: `tests/test_workdir.py`
- Modify: `tests/test_git_init.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_layers_avatar.py`

- [ ] **Step 1: Rename test files**

```bash
git mv tests/test_memory.py tests/test_pad.py
git mv tests/test_library.py tests/test_codex.py
```

- [ ] **Step 2: Update `test_eigen.py`**

- `test_eigen_memory_edit` → `test_eigen_pad_edit`
- `test_eigen_memory_load` → `test_eigen_pad_load`
- `{"object": "memory", ...}` → `{"object": "pad", ...}`
- `system_dir / "memory.md"` → `system_dir / "pad.md"`
- All variable names: `mem_path` → `pad_path`

- [ ] **Step 3: Update `test_pad.py` (was test_memory.py)**

- All `system/memory.md` → `system/pad.md`
- All `memory_file` → `pad_file`
- Test function names: update to reflect "pad"
- Constructor arg: `memory=` → `pad=`
- Docstrings

- [ ] **Step 4: Update `test_psyche.py`**

- `"object": "memory"` → `"object": "pad"`
- `system/memory.md` → `system/pad.md`
- `memory_append.json` → `pad_append.json`

- [ ] **Step 5: Update `test_codex.py` (was test_library.py)**

- `library/library.json` → `codex/codex.json`
- `capabilities=["library"]` → `capabilities=["codex"]`
- Tool calls: `agent.call_tool("library", ...)` → `agent.call_tool("codex", ...)`

- [ ] **Step 6: Update remaining test files**

- `test_workdir.py`: `system/memory.md` → `system/pad.md`
- `test_git_init.py`: `system/memory.md` → `system/pad.md`
- `test_agent.py`: `system/memory.md` → `system/pad.md`, `memory=` → `pad=`
- `test_layers_avatar.py`: `library/library.json` → `codex/codex.json`, `capabilities=["library"]` → `capabilities=["codex"]`

- [ ] **Step 7: Run all tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/ -v`

- [ ] **Step 8: Commit**

```bash
git add tests/
git commit -m "test: rename memory→pad, library→codex, skills→library in tests"
```

---

### Task 7: Documentation — READMEs and docs

**Files:**
- Modify: `README.md`
- Modify: `README.zh.md`
- Modify: `README.wen.md`
- Modify: `docs/intrinsics-draft.md`

- [ ] **Step 1: Update READMEs**

- `memory.md ← working notes` → `pad.md ← working notes`
- `library` capability references → `codex`
- `skills` capability references → `library`
- Chinese/文言 equivalents

- [ ] **Step 2: Update docs**

- `docs/intrinsics-draft.md`: `system/memory.md` → `system/pad.md`

- [ ] **Step 3: Commit**

```bash
git add README.md README.zh.md README.wen.md docs/
git commit -m "docs: rename memory→pad, library→codex, skills→library"
```

---

### Task 8: Final verification

- [ ] **Step 1: Run all tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/ -v`

- [ ] **Step 2: Smoke-test imports**

```bash
python -c "from lingtai.capabilities import codex, library, psyche; from lingtai_kernel.intrinsics import eigen; print('OK')"
```

- [ ] **Step 3: Grep for orphans**

```bash
rg 'system/memory\.md' src/ tests/
rg 'library/library\.json' src/ tests/
rg '\.skills/' src/ tests/ --type py
rg '"memory"' src/ tests/ --type py  # should only be in non-agent contexts
rg '"library"' src/ tests/ --type py  # should only be the NEW library (skill capability)
rg '"skills"' src/ tests/ --type py   # should be zero as capability name
```

- [ ] **Step 4: Fix any orphans and commit**
