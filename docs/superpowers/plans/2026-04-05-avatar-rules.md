# Avatar Rules (`.rules`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `action=rules` mode to the `avatar` tool that lets admin agents (any agent with at least one admin privilege like `karma`) write `.rules` signal files to their own directory and recursively distribute them to all descendants via the avatar tree. Each agent's heartbeat consumes `.rules` (deletes it after reading), diffs the content against the canonical `system/rules.md`, and only refreshes the system prompt when content actually changed. The `rules` section renders after `covenant`, before `tools`.

**Architecture:** The avatar tool gains an `action` parameter (`spawn` default, `rules` new). The `rules` action reads the caller's `delegates/ledger.jsonl` recursively to find all descendant working directories, then writes `.rules` signal files to each. The heartbeat loop in `base_agent.py` consumes `.rules` (like `.prompt` — read then delete), diffs against `system/rules.md`, and if changed, persists to `system/rules.md` and updates the protected `rules` section in the system prompt. After every `avatar(action=spawn)`, the parent automatically re-distributes its rules (if it has `system/rules.md`) so the newborn inherits rules without extra code in the spawn path.

**Relative addressing (IMPORTANT):** As of v0.5.13, `delegates/ledger.jsonl` stores `working_dir` as a **relative directory name** (e.g. `"researcher"`), not an absolute path. Agents live as siblings in `.lingtai/`, so a relative name resolves against the **parent's `.parent`** via `handshake.resolve_address(name, base_dir)`. The tree-walk helper must resolve these relative names to filesystem paths before recursing.

**Tech Stack:** Python 3.10+, pytest, lingtai-kernel internals

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/lingtai/capabilities/avatar.py` | Modify | Add `action` param, `rules` handler, `_walk_avatar_tree` with relative-address resolution, auto-distribute after spawn |
| `src/lingtai_kernel/base_agent.py` | Modify | Add `_check_rules_file()` method, call it in `_heartbeat_loop`, load `system/rules.md` at `__init__` |
| `src/lingtai/agent.py` | Modify | Reload `system/rules.md` in `_setup_from_init()` (covers molt/refresh path) |
| `src/lingtai_kernel/prompt.py` | Modify | Add `rules` to default render order (after `covenant`, before `tools`) |
| `src/lingtai/i18n/en.json` | Modify | Add `avatar.action`, `avatar.rules_content` keys + update `avatar.description` |
| `src/lingtai/i18n/zh.json` | Modify | Add `avatar.action`, `avatar.rules_content` keys + update `avatar.description` |
| `src/lingtai/i18n/wen.json` | Modify | Add `avatar.action`, `avatar.rules_content` keys + update `avatar.description` |
| `tests/test_avatar_rules.py` | Create | All tests for the rules feature |
| `tests/test_prompt.py` | Modify | Test that `rules` renders in correct order |

---

### Task 1: Add `rules` to system prompt render order

**Files:**
- Modify: `src/lingtai_kernel/prompt.py:26`
- Modify: `tests/test_prompt.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_prompt.py`, add:

```python
def test_rules_renders_after_covenant_before_tools():
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    mgr.write_section("rules", "No deleting files.", protected=True)
    mgr.write_section("tools", "### bash\nRun commands.", protected=True)
    prompt = mgr.render()
    cov_pos = prompt.index("Be good.")
    rules_pos = prompt.index("No deleting files.")
    tools_pos = prompt.index("Run commands.")
    assert cov_pos < rules_pos < tools_pos


def test_rules_section_absent_when_empty():
    mgr = SystemPromptManager()
    mgr.write_section("covenant", "Be good.", protected=True)
    mgr.write_section("tools", "### bash\nRun commands.", protected=True)
    prompt = mgr.render()
    assert "## rules" not in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_prompt.py::test_rules_renders_after_covenant_before_tools -v`
Expected: FAIL — `rules` is not in `_DEFAULT_ORDER`, so it renders in the unordered section (between ordered and tail), not between `covenant` and `tools`.

- [ ] **Step 3: Add `rules` to default render order**

In `src/lingtai_kernel/prompt.py`, change line 26:

```python
# Before:
_DEFAULT_ORDER = ["principle", "covenant", "tools", "skills", "identity", "memory", "comment"]

# After:
_DEFAULT_ORDER = ["principle", "covenant", "rules", "tools", "skills", "identity", "memory", "comment"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_prompt.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai_kernel/prompt.py tests/test_prompt.py
git commit -m "feat(prompt): add rules section to render order (after covenant, before tools)"
```

---

### Task 2: Add `.rules` signal consumption in heartbeat loop with diff against `system/rules.md`

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py` — add `_check_rules_file()` method, insert call in `_heartbeat_loop` (around line 764, just before the stamina enforcement block), and load `system/rules.md` in `__init__` (around line 157, right after the covenant `write_section`)
- Modify: `src/lingtai/agent.py` — reload `system/rules.md` in `_setup_from_init()` (around line 534, right after the covenant `write_section`)
- Create: `tests/test_avatar_rules.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_avatar_rules.py`:

```python
"""Tests for .rules signal consumption and system/rules.md persistence."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRulesHeartbeatWatch:
    """Test that the heartbeat loop consumes .rules signal and persists to system/rules.md."""

    def _make_agent(self, tmp_path):
        from lingtai.agent import Agent

        svc = MagicMock()
        svc.get_adapter.return_value = MagicMock()
        svc.provider = "gemini"
        svc.model = "gemini-test"
        wd = tmp_path / "agent"
        agent = Agent(service=svc, agent_name="test", working_dir=wd)
        return agent

    def test_rules_signal_consumed_and_persisted(self, tmp_path):
        """Writing .rules should: inject section, persist to system/rules.md, delete .rules."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # No rules section initially
        assert agent._prompt_manager.read_section("rules") is None

        # Write .rules signal file
        (wd / ".rules").write_text("No deleting files.\nAlways log actions.")

        # Simulate one heartbeat tick
        agent._check_rules_file()

        # Section injected
        assert agent._prompt_manager.read_section("rules") == "No deleting files.\nAlways log actions."
        # Persisted to system/rules.md
        assert (wd / "system" / "rules.md").read_text() == "No deleting files.\nAlways log actions."
        # Signal file consumed (deleted)
        assert not (wd / ".rules").is_file()

    def test_rules_diff_skips_identical(self, tmp_path):
        """If .rules content matches system/rules.md, no prompt refresh."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # Pre-load rules into section and canonical file
        agent._prompt_manager.write_section("rules", "No deleting files.", protected=True)
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("No deleting files.")

        # Write identical .rules signal
        (wd / ".rules").write_text("No deleting files.")

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_not_called()

        # Signal still consumed even if content is identical
        assert not (wd / ".rules").is_file()

    def test_rules_diff_refreshes_on_change(self, tmp_path):
        """If .rules content differs from system/rules.md, prompt is refreshed."""
        agent = self._make_agent(tmp_path)
        wd = agent._working_dir

        # Pre-load old rules
        agent._prompt_manager.write_section("rules", "Old rules.", protected=True)
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("Old rules.")

        # Write new .rules signal
        (wd / ".rules").write_text("New rules.")

        with patch.object(agent, "_flush_system_prompt") as mock_flush:
            agent._check_rules_file()
            mock_flush.assert_called_once()

        assert agent._prompt_manager.read_section("rules") == "New rules."
        assert (system_dir / "rules.md").read_text() == "New rules."
        assert not (wd / ".rules").is_file()

    def test_rules_loaded_from_system_on_init(self, tmp_path):
        """If system/rules.md exists at agent start, rules section should be pre-loaded."""
        wd = tmp_path / "agent"
        system_dir = wd / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "rules.md").write_text("Pre-existing rules.")

        from lingtai.agent import Agent
        svc = MagicMock()
        svc.get_adapter.return_value = MagicMock()
        svc.provider = "gemini"
        svc.model = "gemini-test"
        agent = Agent(service=svc, agent_name="test", working_dir=wd)

        # Rules should be loaded from system/rules.md during init
        assert agent._prompt_manager.read_section("rules") == "Pre-existing rules."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_avatar_rules.py::TestRulesHeartbeatWatch::test_rules_signal_consumed_and_persisted -v`
Expected: FAIL — `_check_rules_file` does not exist.

- [ ] **Step 3: Implement `_check_rules_file` and heartbeat integration**

In `src/lingtai_kernel/base_agent.py`, add the `_check_rules_file` method (place it near `_flush_system_prompt`, around line 1340):

```python
def _check_rules_file(self) -> None:
    """Consume .rules signal file, diff against system/rules.md, update if changed."""
    rules_file = self._working_dir / ".rules"
    if not rules_file.is_file():
        return
    try:
        content = rules_file.read_text().strip()
    except OSError:
        return
    # Always consume the signal file
    try:
        rules_file.unlink()
    except OSError:
        pass
    if not content:
        return
    # Diff against canonical system/rules.md
    canonical = self._working_dir / "system" / "rules.md"
    existing = ""
    if canonical.is_file():
        try:
            existing = canonical.read_text().strip()
        except OSError:
            pass
    if content == existing:
        return
    # Content changed — persist and refresh
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(content)
    self._prompt_manager.write_section("rules", content, protected=True)
    self._flush_system_prompt()
    self._log("rules_loaded", source="signal")
```

In the `_heartbeat_loop` method, insert the `.rules` check after the `.inquiry` block (around line 763, before the stamina enforcement comment `# Stamina enforcement`):

```python
# .rules = network rules signal (consumed → persisted to system/rules.md)
self._check_rules_file()
```

- [ ] **Step 4: Load existing `system/rules.md` at agent init (base_agent.py)**

In `src/lingtai_kernel/base_agent.py`, the covenant loading block sits at lines 132-161. Find the block that ends with:

```python
        # System prompt manager
        self._prompt_manager = SystemPromptManager()
        if covenant:
            self._prompt_manager.write_section("covenant", covenant, protected=True)
        if loaded_memory.strip():
            self._prompt_manager.write_section("memory", loaded_memory)
        if comment:
            self._prompt_manager.write_section("comment", comment)
```

Insert the rules-loading block right after the `covenant` write_section (to keep the render order intuitive — covenant, then rules):

```python
        # Load existing rules from system/rules.md (survives molts, refreshes, and resumes)
        rules_md = system_dir / "rules.md"
        if rules_md.is_file():
            try:
                rules_content = rules_md.read_text().strip()
                if rules_content:
                    self._prompt_manager.write_section("rules", rules_content, protected=True)
            except OSError:
                pass
```

Note: `system_dir` is already defined at line 133 in the same method, so we reuse it.

- [ ] **Step 5: Load existing `system/rules.md` on deep refresh (agent.py)**

In `src/lingtai/agent.py`, `_setup_from_init()` reloads covenant when an agent is refreshed after molt. The covenant reload block lives at roughly lines 521-540. Find:

```python
        # Copy covenant from init.json to system/covenant.md (canonical location)
        if covenant:
            covenant_file.write_text(covenant)
        elif covenant_file.is_file():
            covenant = covenant_file.read_text()
        if covenant:
            self._prompt_manager.write_section("covenant", covenant, protected=True)
```

Add the rules reload block immediately after:

```python
        # Reload rules from system/rules.md (survives molts)
        rules_md = system_dir / "rules.md"
        if rules_md.is_file():
            try:
                rules_content = rules_md.read_text().strip()
                if rules_content:
                    self._prompt_manager.write_section("rules", rules_content, protected=True)
                else:
                    self._prompt_manager.delete_section("rules")
            except OSError:
                pass
        else:
            # No rules file — clear any stale section
            self._prompt_manager.delete_section("rules")
```

The `else` branch is important on refresh: if `system/rules.md` was deleted between the old life and the new, the old section should be cleared. On first init (Step 4 above) we don't need this because the section starts empty.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_avatar_rules.py::TestRulesHeartbeatWatch -v`
Expected: ALL PASS

- [ ] **Step 7: Smoke-test the modules**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "import lingtai_kernel.base_agent; import lingtai.agent; print('OK')"`
Expected: `OK` with no errors.

- [ ] **Step 8: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai_kernel/base_agent.py src/lingtai/agent.py tests/test_avatar_rules.py
git commit -m "feat(heartbeat): consume .rules signal, persist to system/rules.md, diff-based refresh"
```

---

### Task 3: Add `action` parameter and `rules` action to avatar tool

**Files:**
- Modify: `src/lingtai/capabilities/avatar.py`
- Modify: `src/lingtai/i18n/en.json`
- Modify: `src/lingtai/i18n/zh.json`
- Modify: `src/lingtai/i18n/wen.json`
- Modify: `tests/test_avatar_rules.py`

- [ ] **Step 1: Add i18n keys for `action` and `rules_content`**

In `src/lingtai/i18n/en.json`, add after the `"avatar.comment"` entry:

```json
"avatar.action": "Action to perform. 'spawn' (default): create a new 他我. 'rules': set network rules — writes .rules to your directory and distributes to all descendants in the avatar tree. Requires admin privilege (karma). Rules are injected into the system prompt (after covenant, before tools) and persist across molts.",
"avatar.rules_content": "Rules content (required for action='rules'). Plain text — one rule per line. These are non-negotiable constraints distributed to all your descendants. Example: 'Always report findings via email.\\nDo not spawn more than 3 avatars.'",
```

In `src/lingtai/i18n/zh.json`, add after the `"avatar.comment"` entry:

```json
"avatar.action": "执行的动作。'spawn'（默认）：创建新他我。'rules'：设网络规则——写 .rules 到你的目录，并分发给化身树中所有后代。需 admin 权限（karma）。规则注入系统提示（在 covenant 之后、tools 之前），跨凝蜕持久。",
"avatar.rules_content": "规则内容（action='rules' 时必填）。纯文本——每行一条规则。这些是分发给所有后代的不可协商约束。例：'始终通过邮件汇报发现。\\n不要生成超过 3 个化身。'",
```

In `src/lingtai/i18n/wen.json`, add after the `"avatar.comment"` entry:

```json
"avatar.action": "所行之事。'spawn'（默认）：生新他我。'rules'：设网络法则——书 .rules 于汝之目录，分发化身树中一切后代。须 admin 权限（karma）。法则注于系统提示（covenant 之后、tools 之前），跨凝蜕不失。",
"avatar.rules_content": "法则内容（action='rules' 时必填）。纯文——每行一则。此为分发于一切后代之不可议约束。例：'凡发现必以邮报之。\\n化身不得逾三。'",
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_avatar_rules.py`:

```python
from lingtai.capabilities.avatar import AvatarManager


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


class TestAvatarRulesAction:
    """Test avatar(action=rules) distribution."""

    def test_rules_requires_admin(self, tmp_path):
        """Non-admin agent cannot set rules."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="worker",
            working_dir=tmp_path / "worker",
            capabilities=["avatar"],
            admin={},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "No deleting.",
        })
        assert "error" in result

    def test_rules_persists_to_system_rules_md(self, tmp_path):
        """Admin agent should persist rules to system/rules.md (canonical copy)."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="admin",
            working_dir=tmp_path / "admin",
            capabilities=["avatar"],
            admin={"karma": True},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Always log actions.",
        })
        assert result["status"] == "ok"
        # Canonical copy in system/rules.md
        assert (agent._working_dir / "system" / "rules.md").read_text() == "Always log actions."
        # Prompt section updated
        assert agent._prompt_manager.read_section("rules") == "Always log actions."

    def test_rules_distributes_signals_to_descendants(self, tmp_path):
        """Rules should write .rules signal files to all descendant directories.

        IMPORTANT: As of v0.5.13, the ledger stores relative directory names
        (e.g. 'child_a'), not absolute paths. Descendants live as siblings of
        the parent agent in the same `.lingtai/` directory.
        """
        from lingtai.agent import Agent

        # All agents are siblings under tmp_path (mimicking .lingtai/ layout)
        parent_dir = tmp_path / "parent"
        child_a_dir = tmp_path / "child_a"
        child_b_dir = tmp_path / "child_b"
        child_a_dir.mkdir(parents=True)
        child_b_dir.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # Write ledger entries with RELATIVE names (current convention)
        ledger_dir = parent_dir / "delegates"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = ledger_dir / "ledger.jsonl"
        with open(ledger_path, "w") as f:
            f.write(json.dumps({"event": "avatar", "name": "a", "working_dir": "child_a"}) + "\n")
            f.write(json.dumps({"event": "avatar", "name": "b", "working_dir": "child_b"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "No external API calls.",
        })
        assert result["status"] == "ok"
        # Descendants get .rules signal files (their heartbeats will consume them)
        assert (child_a_dir / ".rules").read_text() == "No external API calls."
        assert (child_b_dir / ".rules").read_text() == "No external API calls."
        # Parent gets canonical system/rules.md (not a signal)
        assert (parent_dir / "system" / "rules.md").read_text() == "No external API calls."
        # distributed_to reports relative names
        assert set(result["distributed_to"]) == {"child_a", "child_b"}

    def test_rules_distributes_recursively(self, tmp_path):
        """Rules should propagate to grandchildren (avatars of avatars).

        All three agents are siblings under tmp_path. The ledger records use
        relative names; resolution happens against the parent's parent dir.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        child_dir = tmp_path / "child"
        grandchild_dir = tmp_path / "grandchild"
        for d in (parent_dir, child_dir, grandchild_dir):
            d.mkdir(parents=True)

        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )

        # Parent → child ledger (relative name "child")
        p_ledger = parent_dir / "delegates" / "ledger.jsonl"
        p_ledger.parent.mkdir(parents=True, exist_ok=True)
        p_ledger.write_text(json.dumps({"event": "avatar", "name": "child", "working_dir": "child"}) + "\n")

        # Child → grandchild ledger (relative name "grandchild")
        c_ledger = child_dir / "delegates" / "ledger.jsonl"
        c_ledger.parent.mkdir(parents=True, exist_ok=True)
        c_ledger.write_text(json.dumps({"event": "avatar", "name": "gc", "working_dir": "grandchild"}) + "\n")

        mgr = parent.get_capability("avatar")
        result = mgr.handle({
            "action": "rules",
            "rules_content": "Be concise.",
        })
        assert result["status"] == "ok"
        # All descendants get .rules signal files
        assert (child_dir / ".rules").read_text() == "Be concise."
        assert (grandchild_dir / ".rules").read_text() == "Be concise."

    def test_rules_requires_content(self, tmp_path):
        """action=rules without rules_content should error."""
        from lingtai.agent import Agent

        agent = Agent(
            service=make_mock_service(),
            agent_name="admin",
            working_dir=tmp_path / "admin",
            capabilities=["avatar"],
            admin={"karma": True},
        )
        mgr = agent.get_capability("avatar")
        result = mgr.handle({"action": "rules"})
        assert "error" in result

    def test_spawn_default_action(self, tmp_path):
        """Omitting action should default to spawn (backward compatible).

        NOTE: Real spawning launches a subprocess. We patch _launch to avoid
        that, and pre-create init.json so _spawn reaches the launch path.
        """
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        agent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
        )

        # _spawn requires parent to have init.json
        (parent_dir / "init.json").write_text(
            json.dumps({"manifest": {"agent_name": "parent", "admin": {}}})
        )

        mgr = agent.get_capability("avatar")
        with patch.object(AvatarManager, "_launch", return_value=12345):
            result = mgr.handle({"name": "child", "dir": "child"})
        assert result["status"] == "ok"
        assert result["agent_name"] == "child"
        assert result["address"] == "child"  # relative name (current convention)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_avatar_rules.py::TestAvatarRulesAction::test_rules_requires_admin -v`
Expected: FAIL — `action` parameter not recognized, handler always calls `_spawn`.

- [ ] **Step 4: Update avatar schema to include `action` and `rules_content` params**

In `src/lingtai/capabilities/avatar.py`, replace `get_schema`:

```python
def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["spawn", "rules"],
                "description": t(lang, "avatar.action"),
            },
            "name": {
                "type": "string",
                "description": t(lang, "avatar.name"),
            },
            "type": {
                "type": "string",
                "enum": ["shallow", "deep"],
                "description": t(lang, "avatar.type"),
            },
            "dir": {
                "type": "string",
                "description": t(lang, "avatar.dir"),
            },
            "comment": {
                "type": "string",
                "description": t(lang, "avatar.comment"),
            },
            "rules_content": {
                "type": "string",
                "description": t(lang, "avatar.rules_content"),
            },
        },
        "required": ["name", "dir"],
    }
```

Note: `name` and `dir` remain required for spawn. The `rules` action ignores them. This is acceptable — the LLM will pass dummy values or the handler can tolerate missing values for non-spawn actions. Alternatively, remove them from `required` if the framework supports conditional requirements. Check existing patterns (e.g., daemon tool has no `required` at top level). If removing `required` is cleaner:

```python
        "required": [],
```

Use whichever pattern is consistent with the daemon tool's approach. The daemon tool (`daemon.py`) has `"required": ["action"]` — follow that pattern:

```python
        "required": [],
```

Since `action` defaults to `spawn`, and spawn needs `name`+`dir`, handle validation in the handler.

- [ ] **Step 5: Update the `handle` method to dispatch on action**

In `src/lingtai/capabilities/avatar.py`, replace the `handle` method:

```python
def handle(self, args: dict) -> dict:
    action = args.get("action", "spawn")
    if action == "rules":
        return self._rules(args)
    return self._spawn(args)
```

- [ ] **Step 6: Implement `_rules` method and `_walk_avatar_tree` helper**

In `src/lingtai/capabilities/avatar.py`, add these methods to `AvatarManager`:

```python
# ------------------------------------------------------------------
# Rules distribution
# ------------------------------------------------------------------

def _rules(self, args: dict) -> dict:
    """Set rules and distribute to all descendants via .rules signal files."""
    parent = self._agent
    content = args.get("rules_content", "").strip()
    if not content:
        return {"error": "rules_content is required"}

    # Admin check: at least one admin privilege must be truthy
    admin = getattr(parent, "_admin", {}) or {}
    if not any(admin.values()):
        return {"error": "Not authorized — admin privilege required to set rules"}

    # Persist to own system/rules.md (canonical copy)
    canonical = parent._working_dir / "system" / "rules.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(content)

    # Update own prompt section immediately (no signal needed for self)
    parent._prompt_manager.write_section("rules", content, protected=True)
    parent._flush_system_prompt()

    # Write .rules signal file to all descendants
    distributed: list[str] = []
    for child_dir in self._walk_avatar_tree(parent._working_dir):
        try:
            (child_dir / ".rules").write_text(content)
            distributed.append(child_dir.name)  # report relative name, consistent with addressing convention
        except OSError:
            pass

    return {
        "status": "ok",
        "message": f"Rules set and distributed to {len(distributed)} descendant(s).",
        "distributed_to": distributed,
    }

@staticmethod
def _walk_avatar_tree(root: Path) -> list[Path]:
    """Recursively collect all descendant working-dir Paths from ledger files.

    Ledger entries store relative names (e.g. 'researcher'); we resolve each
    against the *parent agent's parent directory* since avatars live as
    siblings in .lingtai/. Returns absolute Paths of live descendant dirs.
    """
    from lingtai_kernel.handshake import resolve_address

    visited: set[str] = set()
    queue: list[Path] = [Path(root)]
    result: list[Path] = []

    while queue:
        current = queue.pop(0)
        ledger_path = current / "delegates" / "ledger.jsonl"
        if not ledger_path.is_file():
            continue
        try:
            lines = ledger_path.read_text().splitlines()
        except OSError:
            continue
        # Siblings of `current` live in current.parent
        base_dir = current.parent
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") != "avatar":
                continue
            wd = record.get("working_dir", "")
            if not wd:
                continue
            # Resolve relative name to absolute Path
            child_dir = resolve_address(wd, base_dir)
            key = str(child_dir)
            if key in visited:
                continue
            if not child_dir.is_dir():
                continue  # dead avatar, directory gone
            visited.add(key)
            result.append(child_dir)
            queue.append(child_dir)

    return result
```

**Why the base for resolution is `current.parent`, not `current`:** When agent A spawns avatar B, the spawn code runs `avatar_working_dir = parent._working_dir.parent / dir_name` (see `avatar.py:145`). So B is a sibling of A, not a child. When we later look at A's ledger and see `"working_dir": "B"`, we resolve B against A's parent directory. This sibling topology is the key to LingTai's portability — a whole `.lingtai/` tree can be moved because every inter-agent reference is a relative basename. See the reference implementation in `src/lingtai/network.py:_build_avatar_edges`, which uses exactly this pattern.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_avatar_rules.py::TestAvatarRulesAction -v`
Expected: ALL PASS

- [ ] **Step 8: Smoke-test the module**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "import lingtai.capabilities.avatar"`
Expected: No errors.

- [ ] **Step 9: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai/capabilities/avatar.py src/lingtai/i18n/en.json src/lingtai/i18n/zh.json src/lingtai/i18n/wen.json tests/test_avatar_rules.py
git commit -m "feat(avatar): add action=rules for admin-gated network rule distribution"
```

---

### Task 4: Auto-distribute rules after avatar spawn

**Files:**
- Modify: `src/lingtai/capabilities/avatar.py` (in `_spawn`, after ledger append)
- Modify: `tests/test_avatar_rules.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_avatar_rules.py`:

```python
class TestAutoDistributeAfterSpawn:
    """After avatar(action=spawn), parent's rules should be distributed to newborn.

    These tests mock _launch to avoid actually spawning subprocesses, and
    pre-create the parent's init.json so the spawn code path can proceed
    to ledger append and rules distribution.
    """

    def _setup_spawnable_parent(self, tmp_path, with_rules: bool):
        """Build a parent agent with init.json, optionally with system/rules.md."""
        from lingtai.agent import Agent

        parent_dir = tmp_path / "parent"
        parent = Agent(
            service=make_mock_service(),
            agent_name="parent",
            working_dir=parent_dir,
            capabilities=["avatar"],
            admin={"karma": True},
        )
        (parent_dir / "init.json").write_text(
            json.dumps({"manifest": {"agent_name": "parent", "admin": {"karma": True}}})
        )
        if with_rules:
            system_dir = parent_dir / "system"
            system_dir.mkdir(parents=True, exist_ok=True)
            (system_dir / "rules.md").write_text("Always be concise.")
        return parent, parent_dir

    def test_spawn_distributes_existing_rules(self, tmp_path):
        """If parent has system/rules.md, spawning should write .rules to new avatar."""
        parent, parent_dir = self._setup_spawnable_parent(tmp_path, with_rules=True)

        mgr = parent.get_capability("avatar")
        with patch.object(AvatarManager, "_launch", return_value=12345):
            result = mgr.handle({"name": "child", "dir": "child"})
        assert result["status"] == "ok"

        # Child dir is a sibling of parent_dir (avatar_working_dir = parent.parent / dir_name)
        child_dir = parent_dir.parent / "child"
        # Child gets .rules signal file (heartbeat will consume and persist it)
        assert (child_dir / ".rules").read_text() == "Always be concise."

    def test_spawn_without_rules_no_distribution(self, tmp_path):
        """If parent has no system/rules.md, spawn should not create .rules in child."""
        parent, parent_dir = self._setup_spawnable_parent(tmp_path, with_rules=False)

        mgr = parent.get_capability("avatar")
        with patch.object(AvatarManager, "_launch", return_value=12345):
            result = mgr.handle({"name": "child", "dir": "child"})
        assert result["status"] == "ok"

        child_dir = parent_dir.parent / "child"
        assert not (child_dir / ".rules").is_file()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_avatar_rules.py::TestAutoDistributeAfterSpawn::test_spawn_distributes_existing_rules -v`
Expected: FAIL — spawn does not distribute rules.

- [ ] **Step 3: Add auto-distribution after spawn**

In `src/lingtai/capabilities/avatar.py`, in the `_spawn` method, after the ledger append (after `self._append_ledger(...)` block, before the `return` statement), add:

```python
# Auto-distribute rules to newborn — read from canonical system/rules.md
parent_rules_md = parent._working_dir / "system" / "rules.md"
if parent_rules_md.is_file():
    try:
        rules_content = parent_rules_md.read_text()
        if rules_content.strip():
            # Write .rules signal to all descendants (newborn + existing)
            for child_dir in self._walk_avatar_tree(parent._working_dir):
                try:
                    (child_dir / ".rules").write_text(rules_content)
                except OSError:
                    pass
    except OSError:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_avatar_rules.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai/capabilities/avatar.py tests/test_avatar_rules.py
git commit -m "feat(avatar): auto-distribute rules from system/rules.md after spawn"
```

---

### Task 5: Update avatar description to mention rules action

**Files:**
- Modify: `src/lingtai/i18n/en.json`
- Modify: `src/lingtai/i18n/zh.json`
- Modify: `src/lingtai/i18n/wen.json`

- [ ] **Step 1: Update the avatar.description in all three locales**

The current `avatar.description` only describes spawning. Update it to mention the `rules` action.

In `src/lingtai/i18n/en.json`, append to the existing `avatar.description` value (before the closing `"`):

```
 Actions: 'spawn' (default): create a new 他我. 'rules': set network-wide rules distributed to all descendants — requires admin privilege (karma).
```

In `src/lingtai/i18n/zh.json`, append to `avatar.description`:

```
 动作：'spawn'（默认）：创建新他我。'rules'：设网络规则并分发给所有后代——需 admin 权限（karma）。
```

In `src/lingtai/i18n/wen.json`, append to `avatar.description`:

```
 所行：'spawn'（默认）：生新他我。'rules'：设网络法则，分发于一切后代——须 admin 权限（karma）。
```

- [ ] **Step 2: Smoke-test the module to verify JSON is valid**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "import json; json.load(open('src/lingtai/i18n/en.json')); json.load(open('src/lingtai/i18n/zh.json')); json.load(open('src/lingtai/i18n/wen.json')); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai/i18n/en.json src/lingtai/i18n/zh.json src/lingtai/i18n/wen.json
git commit -m "docs(i18n): update avatar description and add rules action keys in en/zh/wen"
```

---

### Task 6: Run full test suite and verify

**Files:** None (verification only)

- [ ] **Step 1: Run all avatar rules tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_avatar_rules.py -v`
Expected: ALL PASS

- [ ] **Step 2: Run prompt tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_prompt.py -v`
Expected: ALL PASS

- [ ] **Step 3: Check baseline for regressions — capture pre-change test state**

**IMPORTANT**: `tests/test_layers_avatar.py` is known to be stale against v0.5.13 (it references `_peers` which was removed in the process-based spawn refactor, and it doesn't pre-create `init.json` so spawns fail). Do **NOT** assert that these tests pass. Instead, run them and compare against the pre-change baseline to ensure we have not made them **worse**.

Run before any code changes (capture as baseline):
```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_layers_avatar.py --tb=no -q 2>&1 | tee /tmp/avatar_baseline.txt
```

Run after all code changes:
```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_layers_avatar.py --tb=no -q 2>&1 | tee /tmp/avatar_post.txt
```

Compare with `diff /tmp/avatar_baseline.txt /tmp/avatar_post.txt` — the pass/fail counts should be identical. Any new failures are regressions.

- [ ] **Step 4: Run full kernel test suite for broader regression check**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest --timeout=30 --tb=no -q 2>&1 | tail -30`
Expected: Total pass/fail count matches pre-change baseline (capture that baseline too if you haven't). New failures must be investigated; pre-existing failures can be ignored.
