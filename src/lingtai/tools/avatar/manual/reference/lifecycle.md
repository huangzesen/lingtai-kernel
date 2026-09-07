---
name: avatar-lifecycle-reference
description: |
  Avatar-manual reference for detached lifecycle, ledger/state, derived-child
  authority markers, boot verification, escalation, platform boundaries, and
  footprint safety.
version: 1.0.0
last_changed_at: 2026-09-06T00:00:00Z
related_files:
- src/lingtai/tools/avatar/manual/SKILL.md
- src/lingtai/tools/avatar/__init__.py
- src/lingtai/tools/avatar/_launcher.py
- src/lingtai/tools/avatar/CONTRACT.md
- src/lingtai/adapters/avatar_launcher.py
- src/lingtai/adapters/posix/avatar_launcher.py
- src/lingtai/adapters/windows/avatar_launcher.py
- src/lingtai/cli.py
- src/lingtai/kernel/base_agent/lifecycle.py
- src/lingtai/tools/skills/manual/reference/cleanup-footprint-contract.md
- src/lingtai/intrinsic_skills/psyche-manual/SKILL.md
maintenance: |
  Tracks Avatar's detached-process and care boundaries. Keep it aligned with
  launcher/CLI authority behavior and the router; do not turn lifecycle details
  into schema prose or add a cleanup command here.
---

# Avatar lifecycle and authority

Nested reference for the Avatar Manual. This page covers what happens after the
spawn request passes the identity and mission gates.

## Independent life, not disposable work

An avatar is a fully detached agent process with its own directory, history,
molts, and lifecycle. Its existence does not depend on the parent's context
window. Use `daemon` for an ephemeral emanation that shares the task workdir
and returns a conclusion; use `shell` for a one-off command. After a successful
spawn, communicate through the normal mail/email channels rather than expecting
an in-process handle.

The parent does not manage ongoing sleep, suspend, or retirement through Avatar.
A quiet avatar is not proof of completion: do not send probe mails. Report the
silence to your own parent, who can decide whether to use lifecycle controls or
accept the loss.

## State and ledger

Paths are relative to the parent working directory and its network root:

```text
<parent>/delegates/ledger.jsonl       # append-only spawn audit
<network-root>/<name>/                 # sibling child directory
  init.json
  settings/psyche.json
  .prompt                              # one-time first-turn signal
  logs/spawn.stderr                    # early-boot stderr
  logs/agent.log                       # child runtime log
  .lingtai-derived-child.json         # Driver-derived state, when granted
  system/ knowledge/ exports/ combo.json  # deep payload where applicable
```

A call that reaches provider admission appends an
`avatar_admission_decision` record. A separate full `avatar` record is appended
only after the process launches; it carries the canonical name, sibling-directory
basename, mission, type, pid, boot status, and any bounded boot error. Earlier
validation/gate returns and dry-run have no full spawn record. The ledger is
append-only. Matching-name records are consulted for liveness through their
stored `working_dir` value, while an existing sibling target directory is
separately refused before child creation.

## Derived-child authority boundary

Only a Driver-approved child-endpoint lease permits Avatar to write
`.lingtai-derived-child.json` before launch. The marker is outside the child's
managed `system/` namespace. A legacy `system/derived_child.json` is also
restrictive for upgrade compatibility. Missing both markers is the only relaxed
case; malformed or unexpected state remains restrictive.

The marker is not a credential, parent identity, or authorization bearer. It
protects against accidental launch-path/configuration loss in the trusted
same-user model, not a child that can edit its own directory. The one-use opaque
lease is handed only to the POSIX launcher and is closed if launch does not
reach that port. The launch environment marker is redundant immediate defense,
not authority.

At child boot, `cli.run()` reads the durable marker and turns it into the
restrictive nested-launch requirement. Authority is not smuggled through the
environment, parent prompt, or a public Avatar input. Windows closes and rejects
a supplied child-endpoint lease rather than launching without its approved
endpoint.

## Launch and boot observation

Avatar resolves the interpreter from `init.json` and submits the exact detached
argv `[python, "-m", "lingtai", "run", <dir>]` to the launcher Port. Standard
input/output are disconnected; stderr is captured at `logs/spawn.stderr`.
The production Port returns a positive PID and opaque adapter handle. Polling is
nonblocking and returns the exact child exit code or `None`.

The manager waits up to 5.0 seconds, checking `.agent.heartbeat` every 0.1
seconds. Heartbeat-first precedence wins; if the child exits first, spawn is
`failed` with a bounded final 2,000-byte stderr tail. If neither handshake nor
exit occurs within the window, spawn is `slow` with a warning. Releasing the
parent-side handle after a slow observation never terminates the child.

POSIX uses a new session; `terminate` and `force_terminate` affect exactly the
owned process, never its tree. Windows uses detached creation flags and
`close_fds`; both termination methods are forceful `TerminateProcess` calls.
Unsupported platforms fail loudly rather than silently selecting an adapter.

## Care, escalation, and footprint

After a spawn, record the address, mission, and delegation reason in pad. If an
avatar encounters a blocker, scope ambiguity, broken peer, budget pressure, or
security concern, it must report concretely to its parent: what it tried, what
failed, and what decision is needed. Do not silently retry forever or molt with
an unreported blocker.

Avatar directories and ledger records are lives, not cache files. Never delete
an avatar directory or ledger entry without explicit retirement approval and a
captured handoff. For inspection, use the [shared footprint recipe](../../../skills/manual/reference/cleanup-footprint-contract.md#shared-footprint-check-recipe);
inspection writes nothing. Prefer authorized lifecycle controls such as sleep,
suspend, or nirvana over filesystem deletion.

## Rules route

Avatar does not own rules distribution. `.rules` is an unchanged kernel/Psyche
signal consumed into `system/rules.md` and the protected prompt section. An
agent may explicitly target a `.rules` path with its own permitted tool, but a
spawn does not broadcast it. Read the [Psyche manual](../../../../intrinsic_skills/psyche-manual/SKILL.md)
for replacement, empty/no-op, flush, and canonical/effective verification.
