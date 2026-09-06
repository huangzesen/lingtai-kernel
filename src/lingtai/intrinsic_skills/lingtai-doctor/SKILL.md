---
name: lingtai-doctor
description: >
  Read-only health diagnostics when an agent looks dead but the evidence is
  mixed, or a migration may have left stale MCP/addon paths; run before
  deciding to mail, refresh, CPR, or edit persistent configuration. Includes
  a bundled read-only `doctor.py`.
version: 0.2.0
tags: [doctor, diagnostics, mcp, addons, heartbeat, migration, recovery]
last_changed_at: "2026-08-07T00:00:00Z"
related_files:
- src/lingtai/intrinsic_skills/lingtai-doctor/scripts/doctor.py
maintenance: |
  Tracks the tool/capability behavior it teaches; update when that tool's behavior changes.
---

# LingTai Doctor

`lingtai-doctor` is the first stop when a LingTai agent or bot looks dead but
the evidence is mixed: Telegram/Feishu/WeChat cannot reach it, the TUI says it
is offline, a heartbeat is fresh, MCP configuration points at an old runtime, or
logs/notifications/status files disagree.

**Diagnosis before repair.** The bundled script is read-only: it summarizes local
evidence, redacts secrets, and suggests next steps. It never edits `init.json`,
touches mailboxes, refreshes agents, or kills processes. Repairs belong to the
owning manuals routed below.

## Run it

```bash
# From a source checkout, against any agent workdir:
python3 src/lingtai/intrinsic_skills/lingtai-doctor/scripts/doctor.py \
  --agent-dir /path/to/project/.lingtai/mimo-1

# From inside an agent (installed bundle); --agent-dir defaults to $LINGTAI_AGENT_DIR:
python3 .library/intrinsic/capabilities/lingtai-doctor/scripts/doctor.py

# Add --json for machine-readable output, or --self-test for a packaging check.
```

## What it checks

Layered so one broken surface is not mistaken for a dead agent:

1. **Identity / lifecycle files** — `.agent.json`, `.status.json`, and
   `.agent.heartbeat` freshness.
2. **Process evidence** — best-effort `ps` scan for `lingtai-agent run <agent-dir>` / `python -m lingtai run <agent-dir>`.
3. **Notifications and logs** — channel files plus common logs such as
   `logs/events.jsonl`, `logs/agent.log`, and token ledgers, by mtime/size only.
4. **Internal mail footprint** — inbox/outbox counts without message bodies.
5. **MCP/addon configuration** — `init.json` top-level `mcp` entries and
   `mcp_registry.jsonl` stdio commands, checked for existence and executability
   (or `PATH` resolution). Environment values are redacted; only path-like
   existence facts are reported.
6. **Migration drift hints** — stale Linux `/home/...` paths on macOS-style
   hosts, stale macOS `/Users/...` paths on Linux-style hosts, and likely
   `~/.lingtai-tui/runtime/venv/bin/python` replacements.
7. **First-party MCP server imports** — if a configured stdio command points at a
   usable Python executable, tries importing the configured LingTai curated MCP
   modules (`lingtai.mcp_servers.`
   `telegram`/`feishu`/`wechat`/`whatsapp`/`imap`/`cloud_mail`)
   without reading credentials.

## Reading the result

Top-level severity is **OK** (no obvious local mismatch), **WARN** (at least one
surface looks stale, missing, or inconsistent), or **FAIL** (a critical local
file/config/path is missing or broken).

Triage in this order,
then follow the owning manual rather than improvising a repair:

| Doctor evidence | Do | Owner |
|---|---|---|
| `.agent.heartbeat` is fresh | The agent is probably alive; internal email should wake it even if an external addon is broken. | [`email-manual`](../../tools/email/manual/SKILL.md) |
| Heartbeat and process are both dead | CPR may be appropriate. If the process is alive but status/logs are stale, investigate before CPR. | [`substrate-manual`](../system-manual/reference/substrate-manual/SKILL.md) |
| An MCP stdio command points at a missing runtime | **Back up `init.json` and `mcp_registry.jsonl` first**, then replace the stale command path and refresh the agent. | [`mcp-manual` troubleshooting](../../tools/mcp/skills/mcp-manual/reference/troubleshooting.md) |
| Notifications are stale while the agent is healthy | Clear the producer channel after reading/handling it; generic dismiss only clears a mirror, so do not use it for producer state unless you know it is stale. | [`notification-manual` dismissal safety](../../tools/notification/manual/reference/dismissal-safety/SKILL.md) |

## Scope

Doctor is the shared diagnostic foundation and covers the whole agent footprint,
not only MCP registry syntax — `mcp(action="info")` validates the registry, not
lifecycle, process, log, or mail evidence. TUI `/doctor` should call these
scripts instead of maintaining a separate copy of the logic.
