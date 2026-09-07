---
related_files:
- src/lingtai/tools/mcp/skills/mcp-manual/SKILL.md
- src/lingtai/tools/mcp/__init__.py
- src/lingtai/services/mcp_registry.py
- src/lingtai/tools/mcp/CONTRACT.md
- tests/test_mcp_identity_discovery.py
- tests/test_tool_family_mcp_migration_parity.py
maintenance: |
  Owns the detailed identity, installed-manual path, and runtime/venv provenance gates routed from mcp-manual; update it when identity projection, curated launcher ownership, or manual-path behavior changes.
---

# MCP identity and runtime provenance

This reference owns details intentionally kept out of the model-facing schema
and the short `mcp-manual` router. It is diagnostic guidance, not an MCP
registration or server-management API.

## Identity projection

`mcp(action="info", input={}, reasoning="inspect MCP identity")` reads the
addon-published, non-secret document
`system/mcp_identities/<name>.json`. The document uses schema
`lingtai.mcp.identity.v1`; it is a cached filesystem record and does not make a
network request.

An `identity` block is attached only to a matching `mcp_registry.jsonl` record
when the identity has a non-empty `accounts` list. The `info` projection
allowlists the account alias, provider username/ID/display name, bot marker,
and non-secret routing counts. It drops tokens, passwords, app secrets,
refresh/access tokens, headers, and unknown fields. The prompt's
`<registered_mcp>` XML narrows the projection again and excludes volatile
`last_verified_at` so prompt content remains cache-stable. A missing, unrelated,
or empty identity record produces no `identity` member. For richer account
information, use the addon's own `accounts` action; never read private config as
a shortcut.

## Manual and public-path gate

The reserved `manual` action reads exactly the installed
`.library/intrinsic/capabilities/mcp/SKILL.md` below the granted agent workdir.
It performs no registry scan, identity read, rescan, or mutation. Its public
result is the existing flat shape:

```json
{"status":"ok","mcp_manual":"<full body>","manual_path":"<host-local path>"}
```

A missing skill returns `status: "degraded"`, an empty `mcp_manual`, the
truthful host-local `manual_path`, and an error; it never falls back to another
manual. `manual_path` and `registry_path` are diagnostic host-local values, not
configuration inputs or proof of an active child. Do not copy private absolute
paths into public docs, and do not infer a server's activation from either path.

## Curated versus non-curated child ownership

A curated addon activated through `init.json`'s `mcp.<name>` entry is owned by
the running kernel's own `mcp_catalog.json`. That catalog derives the stdio
type, the running Agent's `sys.executable`, the module args, and the child's
entire `PYTHONPATH` from the Agent's current imported source root. The curated
entry's non-launch `env` (for example `LINGTAI_IMAP_CONFIG`) passes through;
legacy `type`/`command`/`args`/`env.PYTHONPATH` fields are accepted for
read-compatibility but ignored for launch with one bounded warning. If the
catalog has no safe stdio launcher, do not fall back to those legacy fields.

A non-curated third-party `init.json` entry and the legacy
`mcp/servers.json` route own their own `type`/`command`/`args`/`env` activation
spec. Daemon task/plugin MCPs are a separate route owned by that task or
plugin's configuration; they never spawn from `mcp_registry.jsonl` or the
main-agent `init.json` entry. HTTP entries have no local interpreter. Never
assume that a child shares the main Agent's interpreter, site-packages, source
root, or environment unless the effective provenance is checked.

## Authorized venv swap and registry truth

A venv swap never rewrites the registry automatically. After an authorized
swap, reconcile canonical curated records explicitly with the **new** runtime
interpreter. There is no kernel helper: use an authorized `shell` call (or an
equivalent reviewed `file.write`/`file.edit` operation). This self-contained
recipe is fail-closed, atomic, order-preserving, and never appends records:

```bash
NEW_PYTHON=/absolute/path/to/new/venv/bin/python
AGENT_DIR=/absolute/path/to/agent/dir

"$NEW_PYTHON" - "$AGENT_DIR" <<'PY'
import json, os, sys, tempfile
from pathlib import Path

agent_dir = Path(sys.argv[1])
if not agent_dir.is_absolute():
    raise SystemExit("AGENT_DIR must be an absolute path")
registry = agent_dir / "mcp_registry.jsonl"
print(f"target registry: {registry}")
print(f"interpreter: {sys.executable}")
if not registry.is_file():
    raise SystemExit(f"no registry at {registry}")

from lingtai.services.mcp_registry import read_registry
_valid, problems = read_registry(agent_dir)
if problems:
    for problem in problems:
        print(f"problem line {problem['line']}: {problem['error']}")
    raise SystemExit("registry has problems; aborting without writing")

lines = registry.read_text(encoding="utf-8").splitlines(keepends=True)
seen: set[str] = set()
changed: list[str] = []
for index, raw in enumerate(lines):
    if not raw.strip():
        continue
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        continue  # defensive; the preflight already rejected invalid lines
    name = record.get("name")
    if not name or name in seen:
        continue
    seen.add(name)
    if record.get("source") == "lingtai-curated" and record.get("command") != sys.executable:
        record["command"] = sys.executable
        ending = "\n" if raw.endswith("\n") else ""
        lines[index] = json.dumps(record, ensure_ascii=False) + ending
        changed.append(name)

if changed:
    fd, temporary = tempfile.mkstemp(
        dir=str(registry.parent), prefix=".mcp_registry.jsonl.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, registry.stat().st_mode & 0o777)
        os.replace(temporary, registry)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    print(
        f"reconciled {len(changed)} curated command(s): "
        f"{', '.join(changed)} -> {sys.executable}"
    )
else:
    print("no curated command needed reconciling")
PY
```

Run it only after inspecting `mcp(action="info")` and obtaining explicit
registry-write authorization. It rewrites every canonical `lingtai-curated`
record whose stored `command` differs, and aborts before writing if
`read_registry()` reports any invalid or duplicate line. An intentional
independent-interpreter override must be excluded or left unreconciled.

This changes durable registry truth only. It is not activation and is never the
spawn source for a non-curated, legacy, daemon-launched, or plugin-launched
child. A curated main-agent child still derives `type`, `command`, `args`, and
its entire `PYTHONPATH` from the running kernel's own catalog and runtime, not
from stored registry or `init.json` launch fields. A healthy curated child picks
up a new venv only after a full Agent relaunch: `refresh` retries failed children
but does not restart healthy ones. Apply the provenance gate below before
claiming the swap is live.

## Fail-closed provenance check

After a venv or source change, run one one-shot probe using the child's
**effective** command and environment. It must print and compare:

- `sys.executable`;
- `lingtai.__file__`; and
- `lingtai.mcp_servers.<name>.__file__`.

For a curated main-agent entry, effective means the Agent's own interpreter,
its catalog-derived module args, and its own source root as the child's entire
`PYTHONPATH`—never strings stored in `init.json` or the registry. For a
non-curated/legacy child, use that entry's configured command and environment;
for a daemon child, use its task/plugin configuration. If the probe still
resolves the old venv or source, the gate fails: report **requires relaunch**
and do not claim the change is live. Process inspection alone is corroboration,
not proof of imported module provenance.
