# Kernel Relative Addressing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch all intra-network addresses from absolute filesystem paths to relative directory names in the Python kernel, so agent networks survive directory moves and Syncthing migrations.

**Architecture:** Add a `resolve_address(addr, base_dir)` helper to `handshake.py`. Change all address **producers** (manifest, mail, ledger, events, contacts) to write relative names (just the directory basename). Change all address **consumers** (handshake, mail routing, karma operations, CPR, network discovery) to resolve relative names via the helper before filesystem operations. The `base_dir` (`.lingtai/` path) is derived at runtime from `working_dir.parent`.

**Tech Stack:** Python 3.11+, pytest, pathlib

---

## File Map

### New files
- None — all changes are to existing files

### Modified files (producers — write relative names)

| File | What changes |
|------|-------------|
| `src/lingtai_kernel/base_agent.py:1256` | `_build_manifest()`: write `self._working_dir.name` instead of `str(self._working_dir)` |
| `src/lingtai_kernel/base_agent.py:804` | `_log()`: write `self._working_dir.name` in event address |
| `src/lingtai_kernel/services/mail.py:110` | `address` property: return `self._working_dir.name` |
| `src/lingtai_kernel/intrinsics/mail.py:350` | `_send()`: use `agent._working_dir.name` for `from` field |
| `src/lingtai/capabilities/avatar.py:186` | `_spawn()`: write `avatar_working_dir.name` in ledger |
| `src/lingtai/capabilities/email.py:676-678` | `_send_email()`: use `self._agent._working_dir.name` for `from` field |

### Modified files (consumers — resolve before use)

| File | What changes |
|------|-------------|
| `src/lingtai_kernel/handshake.py` | Add `resolve_address()` helper. Update `is_agent()`, `is_alive()`, `manifest()` to accept both formats. |
| `src/lingtai_kernel/services/mail.py:130` | `send()`: resolve address to Path before filesystem ops |
| `src/lingtai_kernel/intrinsics/mail.py:200-208` | `_is_self_send()`: compare against `agent._working_dir.name` |
| `src/lingtai_kernel/intrinsics/system.py:163-268` | `_check_karma_gate()`, `_lull()`, `_suspend()`, `_cpr()`, `_interrupt()`, `_nirvana()`: resolve address before filesystem ops |
| `src/lingtai/agent.py:241` | `_cpr_agent()`: resolve address before Path construction |
| `src/lingtai/network.py:143-234` | `_discover_agents()`, `_build_avatar_edges()`, `_build_contact_edges()`: resolve addresses |

---

### Task 1: Add `resolve_address()` to handshake.py

**Files:**
- Modify: `src/lingtai_kernel/handshake.py`
- Test: `tests/test_handshake.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_handshake.py`:

```python
def test_resolve_address_relative(tmp_path):
    """Relative name resolves to base_dir / name."""
    from lingtai_kernel.handshake import resolve_address
    result = resolve_address("本我", tmp_path)
    assert result == tmp_path / "本我"


def test_resolve_address_absolute(tmp_path):
    """Absolute path is returned as-is."""
    from lingtai_kernel.handshake import resolve_address
    abs_path = tmp_path / "other" / ".lingtai" / "agent"
    result = resolve_address(str(abs_path), tmp_path)
    assert result == abs_path


def test_resolve_address_path_object(tmp_path):
    """Path objects work too."""
    from lingtai_kernel.handshake import resolve_address
    result = resolve_address(tmp_path / "human", tmp_path)
    assert result == tmp_path / "human"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_handshake.py -k "test_resolve_address" -v`
Expected: FAIL — `resolve_address` not found

- [ ] **Step 3: Add resolve_address to handshake.py**

Add after the imports in `src/lingtai_kernel/handshake.py`:

```python
def resolve_address(address: str | Path, base_dir: str | Path) -> Path:
    """Resolve an agent address to an absolute Path.

    Relative names (e.g. "本我") are joined with base_dir.
    Absolute paths are returned as-is.
    """
    p = Path(address)
    if p.is_absolute():
        return p
    return Path(base_dir) / address
```

- [ ] **Step 4: Update existing handshake functions to accept both formats**

Update `is_agent`, `is_alive`, `is_human`, `manifest` to call through `resolve_address` when a `base_dir` is provided:

No change needed — these functions already accept `str | Path` and call `Path(path)`. They will work with both absolute paths and relative names **as long as the caller resolves first**. The resolve happens at the call site (mail service, karma operations, etc.).

- [ ] **Step 5: Run tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_handshake.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
git add src/lingtai_kernel/handshake.py tests/test_handshake.py
git commit -m "feat(handshake): add resolve_address() for relative addressing"
```

---

### Task 2: Producers — manifest and events write relative names

**Files:**
- Modify: `src/lingtai_kernel/base_agent.py:1256,804`
- Modify: `src/lingtai_kernel/services/mail.py:108-110`
- Test: `tests/test_base_agent.py`, `tests/test_services_mail.py`

- [ ] **Step 1: Update `_build_manifest()` in base_agent.py**

At line 1256, change:
```python
"address": str(self._working_dir),
```
to:
```python
"address": self._working_dir.name,
```

Also at line 1266-1267, keep the `_mail_service.address` override — it will now return the relative name too (after we change the mail service property).

- [ ] **Step 2: Update `_log()` in base_agent.py**

At line 804, change:
```python
"address": str(self._working_dir),
```
to:
```python
"address": self._working_dir.name,
```

- [ ] **Step 3: Update `FilesystemMailService.address` property**

In `src/lingtai_kernel/services/mail.py`, change the `address` property (line 108-110):
```python
@property
def address(self) -> str:
    """Return the working directory name as this agent's mail address."""
    return self._working_dir.name
```

- [ ] **Step 4: Update `FilesystemMailService.send()` to resolve addresses**

In `src/lingtai_kernel/services/mail.py`, at line 130, change:
```python
recipient_dir = Path(address)
```
to:
```python
from .handshake import resolve_address  # at top of method or file
```

Wait — `handshake` is in `lingtai_kernel`, which is the same package. The import is already at top of file: `from ..handshake import is_agent, is_alive, manifest`. Add `resolve_address` to that import.

Then change `send()`:
```python
def send(self, address: str, message: dict) -> str | None:
    base_dir = self._working_dir.parent  # .lingtai/ directory
    recipient_dir = resolve_address(address, base_dir)

    # --- handshake ---
    if not is_agent(recipient_dir):
        return f"No agent at {address}"
    if not is_alive(recipient_dir):
        return f"Agent at {address} is not running"
    # ... rest unchanged, but use recipient_dir instead of Path(address)
```

Note: the handshake calls `is_agent(recipient_dir)` and `is_alive(recipient_dir)` now receive the resolved absolute Path.

- [ ] **Step 5: Smoke-test imports**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "import lingtai_kernel.services.mail"`
Expected: No errors

- [ ] **Step 6: Run related tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_base_agent.py tests/test_services_mail.py tests/test_filesystem_mail.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/lingtai_kernel/base_agent.py src/lingtai_kernel/services/mail.py
git commit -m "feat(kernel): producers write relative directory names"
```

---

### Task 3: Mail intrinsic — relative from/to and self-send check

**Files:**
- Modify: `src/lingtai_kernel/intrinsics/mail.py:200-208,350`
- Test: `tests/test_mail_intrinsic.py`

- [ ] **Step 1: Update `_is_self_send()` — compare by name**

In `src/lingtai_kernel/intrinsics/mail.py`, lines 200-209:

```python
def _is_self_send(agent, address: str) -> bool:
    """Check if the address matches this agent (by directory name or full path)."""
    # Match by directory name (relative address)
    if address == agent._working_dir.name:
        return True
    # Match by full working directory path (legacy absolute)
    if address == str(agent._working_dir):
        return True
    # Match by mail service address
    if agent._mail_service is not None and agent._mail_service.address:
        if address == agent._mail_service.address:
            return True
    return False
```

- [ ] **Step 2: Update `_send()` — use relative name for `from` field**

In `src/lingtai_kernel/intrinsics/mail.py`, line 350, change:

```python
"from": (agent._mail_service.address if agent._mail_service is not None and agent._mail_service.address else str(agent._working_dir)),
```
to:
```python
"from": (agent._mail_service.address if agent._mail_service is not None and agent._mail_service.address else agent._working_dir.name),
```

Since `agent._mail_service.address` now returns `.name` too (from Task 2), this will always produce a relative name.

- [ ] **Step 3: Run mail tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_mail_intrinsic.py tests/test_mail_identity.py tests/test_intrinsics_comm.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/lingtai_kernel/intrinsics/mail.py
git commit -m "feat(mail): relative from/to addresses and name-based self-send check"
```

---

### Task 4: Email capability — relative sender address

**Files:**
- Modify: `src/lingtai/capabilities/email.py:676-678`
- Test: `tests/test_layers_email.py`

- [ ] **Step 1: Update `_send_email()` sender address**

In `src/lingtai/capabilities/email.py`, lines 676-678, change:

```python
sender = (self._agent._mail_service.address
          if self._agent._mail_service is not None and self._agent._mail_service.address
          else str(self._agent._working_dir))
```
to:
```python
sender = (self._agent._mail_service.address
          if self._agent._mail_service is not None and self._agent._mail_service.address
          else self._agent._working_dir.name)
```

- [ ] **Step 2: Run email capability tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_layers_email.py tests/test_three_agent_email.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/lingtai/capabilities/email.py
git commit -m "feat(email): use relative sender address"
```

---

### Task 5: Avatar capability — relative working_dir in ledger

**Files:**
- Modify: `src/lingtai/capabilities/avatar.py:184-190`
- Test: `tests/test_layers_avatar.py`

- [ ] **Step 1: Update `_spawn()` — write relative name in ledger**

In `src/lingtai/capabilities/avatar.py`, lines 184-190, change:

```python
self._append_ledger(
    "avatar", peer_name,
    working_dir=str(avatar_working_dir),
    mission=reasoning or "",
    type=avatar_type,
    pid=pid,
)

return {
    "status": "ok",
    "working_dir": str(avatar_working_dir),
    "agent_name": peer_name,
    "type": avatar_type,
    "pid": pid,
}
```
to:
```python
self._append_ledger(
    "avatar", peer_name,
    working_dir=avatar_working_dir.name,
    mission=reasoning or "",
    type=avatar_type,
    pid=pid,
)

return {
    "status": "ok",
    "working_dir": avatar_working_dir.name,
    "agent_name": peer_name,
    "type": avatar_type,
    "pid": pid,
}
```

- [ ] **Step 2: Run avatar tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_layers_avatar.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add src/lingtai/capabilities/avatar.py
git commit -m "feat(avatar): write relative working_dir in ledger"
```

---

### Task 6: System intrinsic — resolve addresses in karma operations

**Files:**
- Modify: `src/lingtai_kernel/intrinsics/system.py:157-268`
- Test: `tests/test_karma.py`

- [ ] **Step 1: Update `_check_karma_gate()` to resolve address**

In `src/lingtai_kernel/intrinsics/system.py`, add import at top:
```python
from ..handshake import resolve_address
```

Update `_check_karma_gate()` (lines 157-170):
```python
def _check_karma_gate(agent, action: str, args: dict) -> dict | None:
    from ..handshake import is_agent
    if action in _KARMA_ACTIONS and not agent._admin.get("karma"):
        return {"error": True, "message": f"Not authorized for {action} (requires admin.karma=True)"}
    if action in _NIRVANA_ACTIONS and not (agent._admin.get("karma") and agent._admin.get("nirvana")):
        return {"error": True, "message": f"Not authorized for {action} (requires admin.nirvana=True)"}
    address = args.get("address")
    if not address:
        return {"error": True, "message": f"{action} requires an address"}
    # Resolve relative address to absolute path
    base_dir = agent._working_dir.parent
    resolved = resolve_address(address, base_dir)
    if str(resolved) == str(agent._working_dir):
        return {"error": True, "message": f"Cannot {action} self"}
    if not is_agent(resolved):
        return {"error": True, "message": f"No agent at {address}"}
    # Store resolved path back for downstream use
    args["_resolved_address"] = resolved
    return None
```

- [ ] **Step 2: Update karma actions to use resolved path**

Update `_lull()`, `_suspend()`, `_interrupt()`, `_nirvana()` to use `args["_resolved_address"]` for filesystem operations:

```python
def _lull(agent, args: dict) -> dict:
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "lull", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running — already asleep?"}
    (resolved / ".sleep").write_text("")
    agent._log("karma_lull", target=address)
    return {"status": "asleep", "address": address}
```

Apply the same pattern to `_suspend()`, `_cpr()`, `_interrupt()`, `_nirvana()` — use `resolved` for `is_alive()`, `Path()` operations, and signal file writes. Keep `address` (the original relative name) for log messages and return values.

For `_cpr()`:
```python
def _cpr(agent, args: dict) -> dict:
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "cpr", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is already running"}
    resuscitated = agent._cpr_agent(str(resolved))
    if resuscitated is None:
        return {"error": True, "message": "CPR not supported — no _cpr_agent handler"}
    agent._log("karma_cpr", target=address)
    return {"status": "resuscitated", "address": address}
```

For `_nirvana()`:
```python
def _nirvana(agent, args: dict) -> dict:
    import shutil
    from ..handshake import is_alive
    err = _check_karma_gate(agent, "nirvana", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if is_alive(resolved):
        (resolved / ".sleep").write_text("")
        import time as _time
        deadline = _time.time() + 10.0
        while _time.time() < deadline:
            if not is_alive(resolved):
                break
            _time.sleep(0.5)
        else:
            if is_alive(resolved):
                return {"error": True, "message": f"Agent at {address} did not sleep within timeout"}
    shutil.rmtree(resolved)
    agent._log("karma_nirvana", target=address)
    return {"status": "nirvana", "address": address}
```

- [ ] **Step 3: Run karma tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_karma.py tests/test_system.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add src/lingtai_kernel/intrinsics/system.py
git commit -m "feat(system): resolve relative addresses in karma operations"
```

---

### Task 7: Agent CPR — resolve address

**Files:**
- Modify: `src/lingtai/agent.py:241`

- [ ] **Step 1: Update `_cpr_agent()` to resolve address**

In `src/lingtai/agent.py`, at line 241, the `address` parameter is already resolved by `_cpr()` in system.py (Task 6 passes `str(resolved)`). But for safety, also handle relative names here:

```python
def _cpr_agent(self, address: str) -> "Agent | None":
    from lingtai_kernel.handshake import is_agent, resolve_address

    base_dir = self._working_dir.parent
    target = resolve_address(address, base_dir)
    if not is_agent(target):
        return None
    # ... rest uses target (Path) instead of Path(address)
```

Replace all `Path(address)` with `target` in this method.

- [ ] **Step 2: Smoke-test**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "import lingtai.agent"`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add src/lingtai/agent.py
git commit -m "feat(agent): resolve address in CPR"
```

---

### Task 8: Network topology — resolve addresses

**Files:**
- Modify: `src/lingtai/network.py:143-234`
- Test: `tests/test_network.py`

- [ ] **Step 1: Update `_discover_agents()` — use relative addresses as keys**

In `src/lingtai/network.py`, the `address` from `.agent.json` is now a relative name. The `working_dir` from the filesystem scan is absolute. The node dict should use the relative address as key (matching how the Go side works):

```python
def _discover_agents(base_dir: Path) -> dict[str, AgentNode]:
    nodes: dict[str, AgentNode] = {}
    if not base_dir.is_dir():
        return nodes
    for child in base_dir.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / ".agent.json"
        manifest = _read_json(manifest_path)
        if manifest is None or not isinstance(manifest, dict):
            continue
        address = manifest.get("address", "")
        if not address:
            continue
        nodes[address] = AgentNode(
            address=address,
            agent_name=manifest.get("agent_name", ""),
            working_dir=child,
        )
    return nodes
```

This is actually unchanged — the address is whatever `.agent.json` says, which is now relative. The `working_dir` stays absolute (from the filesystem). Good.

- [ ] **Step 2: Update `_build_avatar_edges()` — resolve child addresses**

The `child_address` from `ledger.jsonl` is now relative. When adding ghost nodes, resolve to get the working dir:

```python
def _build_avatar_edges(nodes: dict[str, AgentNode]) -> list[AvatarEdge]:
    edges: list[AvatarEdge] = []
    for parent_address, node in list(nodes.items()):
        if node.working_dir is None:
            continue
        base_dir = node.working_dir.parent  # .lingtai/
        ledger_path = node.working_dir / "delegates" / "ledger.jsonl"
        if not ledger_path.is_file():
            continue
        # ... read lines as before ...
            child_address = record.get("working_dir", "")
            if not child_address:
                continue
            # child_address is now relative — use as-is for node key
            if child_address not in nodes:
                # Resolve to get filesystem path for ghost node
                from lingtai_kernel.handshake import resolve_address
                child_dir = resolve_address(child_address, base_dir)
                nodes[child_address] = AgentNode(
                    address=child_address,
                    agent_name=record.get("name", ""),
                    working_dir=child_dir if child_dir.is_dir() else None,
                )
            edges.append(AvatarEdge(
                parent_address=parent_address,
                child_address=child_address,
                # ... rest unchanged
            ))
    return edges
```

- [ ] **Step 3: Run network tests**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/test_network.py -v`
Expected: All pass (tests may need fixture updates — if they fail, update fixtures to use relative addresses)

- [ ] **Step 4: Commit**

```bash
git add src/lingtai/network.py
git commit -m "feat(network): handle relative addresses in topology discovery"
```

---

### Task 9: Run full test suite and fix failures

**Files:** Various test files that may need fixture updates

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/ -x -v 2>&1 | tail -60`

- [ ] **Step 2: Fix any failing tests**

Most likely failures:
- Tests that assert `agent.address == str(working_dir)` → change to `agent.address == working_dir.name`
- Tests that create mock agents with absolute address strings → change to relative names
- Tests that compare `msg["from"]` against absolute paths → change to directory name

For each failing test: read the test, understand what it asserts, update the expected value to match the new relative format.

- [ ] **Step 3: Run full suite again**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: update fixtures for relative addressing"
```

---

### Task 10: Smoke test — verify import and basic flow

- [ ] **Step 1: Verify all imports**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -c "import lingtai; import lingtai_kernel; import lingtai.agent; import lingtai.network; import lingtai.capabilities.avatar; import lingtai.capabilities.email"`
Expected: No errors

- [ ] **Step 2: Run full test suite one final time**

Run: `cd /Users/huangzesen/Documents/GitHub/lingtai-kernel && python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 3: Commit any remaining fixes**

```bash
git add -A && git commit -m "chore: final cleanup for relative addressing"
```
