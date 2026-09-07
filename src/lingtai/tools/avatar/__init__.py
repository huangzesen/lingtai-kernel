"""Avatar capability — spawn independent peer agents (分身).

Shallow (初生): Copy init.json to a new working dir, strip name, launch.
    The avatar gets the same LLM config + capabilities but no identity,
    no pad, no history.  A fresh life — but its own, not yours.

Deep (二重身): Copy identity files (system/), knowledge/, and exports/
    plus init.json to a new dir, strip name + history, launch.
    The avatar is a doppelgänger — same character, pad, knowledge —
    but starts a fresh conversation.

Both modes launch `lingtai-agent run <dir>` as a fully detached process.
The avatar is an independent life — its existence does not depend on yours.

Maintains an append-only ledger (delegates/ledger.jsonl) that records
every spawn event.

Usage (LTP v2 envelope — one action, one strict child input):
    Agent(capabilities=["avatar"])
    # avatar(action="spawn", input={"name": "researcher"}, reasoning="...")
    # avatar(action="spawn", input={"name": "clone", "type": "deep"}, reasoning="...")
    # avatar(action="settings", input={}, reasoning="...")
    # avatar(action="manual", input={}, reasoning="...")

Avatar no longer owns a rules-distribution action or an automatic post-spawn
rules fan-out; the shared `.rules` heartbeat signal/consumer described in
`src/lingtai/kernel/base_agent/lifecycle.py` is unchanged, and any agent may
still write a `.rules` file to an explicitly targeted path (e.g. via `shell`).
See `psyche-manual` for that protocol.

The spawn mission brief is root ``reasoning`` (normalized to ``_reasoning`` by
ToolExecutor), never an ``input`` property — see ``handle()``.

This module is the static declared official plugin slice: its binder receives
only the `workdir` and Avatar-specific parent-context ports, while the kernel
registrar alone reserves and mounts the public `avatar` name. The package-local
manual child deliberately keeps Avatar's current local-manual behavior.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from lingtai.kernel.agent_presence import observe_alive as _presence_observe_alive
from lingtai.kernel._fsutil import atomic_write_json
from lingtai.kernel.i18n import t
from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration
from ..tool_family import ChildTool, SettingsProvider, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA
from ._launcher import (
    DERIVED_AVATAR_EXECUTION_ENV,
    DERIVED_AVATAR_STATE,
    AvatarLaunchReceipt,
    AvatarLaunchRequest,
    AvatarLauncherPort,
    derived_avatar_state_path,
)
from .settings import (
    AVATAR_NAME_MAX_CHARACTERS,
    AVATAR_NAME_MIN_CHARACTERS,
    BOOT_POLL_INTERVAL_SECONDS,
    BOOT_STDERR_TAIL_BYTES,
    BOOT_WAIT_SECONDS,
    MISSION_MIN_CHARACTERS,
    MISSION_PLACEHOLDER_PREFIXES,
    SPAWN_COMMENT_DEFAULT,
    SPAWN_CONFIRM_DEFAULT,
    SPAWN_DRY_RUN_DEFAULT,
    SPAWN_TYPE_DEFAULT,
    SPAWN_TYPES,
    AvatarSettingsProvider,
)


def _is_alive(working_dir) -> bool:
    """Foreign-address liveness check via the presence store + Core policy.

    Builds a target-bound POSIX presence adapter for *working_dir* and applies
    the Core freshness/human policy in manifest-first order, replacing the
    former ``handshake.is_alive`` call.
    """
    from lingtai.adapters.posix.agent_presence import PosixAgentPresenceStoreAdapter

    store = PosixAgentPresenceStoreAdapter(working_dir)
    return _presence_observe_alive(store, wall_now=time.time())


# Avatar name doubles as its working-directory basename. Letters (any script,
# including CJK), digits, underscore, and hyphen — no path separators, no
# control chars, no dots. The structural chars are what make this dangerous;
# the script itself is the agent's choice.
_AVATAR_NAME_RE = re.compile(r"^[\w-]+$")  # \w is Unicode-aware in Py3 re


def _mission_looks_unsafe(mission: str) -> tuple[bool, str]:
    """Heuristic mission-quality gate.

    Returns ``(unsafe, reason)``. Used to refuse accidental spawns where the
    mission field is empty, far too short, or matches a debug/test placeholder
    pattern. Caller can override with ``confirm=True``.
    """
    trimmed = (mission or "").strip()
    if not trimmed:
        return True, "mission is empty"
    if len(trimmed) < MISSION_MIN_CHARACTERS:
        return True, f"mission is very short ({len(trimmed)} chars)"
    lower = trimmed.lower()
    if lower in MISSION_PLACEHOLDER_PREFIXES or lower.startswith(
        tuple(f"{word} " for word in MISSION_PLACEHOLDER_PREFIXES)
    ):
        return True, "mission looks like a debug/test placeholder"
    return False, ""


if TYPE_CHECKING:
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.provider_admission import DerivedLaunchEndpointLease
    from lingtai.kernel.tool_plugin import ToolPluginHost

PROVIDERS = {"providers": [], "default": "builtin"}

# Canonical, strict per-action input schemas. Optionals are expressed as
# nullable required properties because that is what strict OpenAI-style
# validators demand of a closed object; null means "absent" to the action
# implementations (see ``_strip_nulls``).
#
# The spawn mission brief is deliberately NOT a property here: it is root
# ``reasoning``, and nested ``input`` must never carry
# ``reasoning``/``_reasoning``/``summarize``.
_SPAWN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Avatar name and sibling-directory basename: one segment of "
                "letters/digits/_/-; 1-64 chars, with no dots or slashes."
            ),
        },
        "type": {
            "type": ["string", "null"],
            "enum": [*SPAWN_TYPES, None],
            "description": (
                "Spawn type: 'shallow' (default) copies init.json plus narrow "
                "Psyche inputs; 'deep' also copies identity/knowledge. Null uses "
                "shallow."
            ),
        },
        "comment": {
            "type": ["string", "null"],
            "description": (
                "Persistent child-prompt note; not inherited. Null or empty means "
                "no note. See avatar-manual for placement and lifetime."
            ),
        },
        "dry_run": {
            "type": ["boolean", "null"],
            "description": (
                "Preview with no files or process created; null defaults to false."
            ),
        },
        "confirm": {
            "type": ["boolean", "null"],
            "description": (
                "Acknowledge the mission review; required for empty/short/"
                "placeholder reasoning. Null defaults to false."
            ),
        },
    },
    "required": ["name", "type", "comment", "dry_run", "confirm"],
    "additionalProperties": False,
}

# Avatar's own action registry. The reserved ``manual`` child is appended by
# this module's official declaration rather than being an operational action.
_DECLARED_CHILD_SPECS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("spawn", _SPAWN_INPUT_SCHEMA),
)

_DESCRIPTION = (
    "Spawn an independent, detached avatar, show its fixed settings, or read "
    "the manual. Use an explicit action and strict input; there is no default. "
    "Read avatar-manual first. Before the first spawn, call "
    "avatar(action='manual', input={}, "
    "reasoning='...'). For spawn, input.name is the canonical sibling-directory "
    "basename, input.type is shallow (default) or deep, and root reasoning is "
    "the required mission/first prompt. Use dry_run to preview and confirm only "
    "after reviewing the mission. settings and manual are read-only."
)


def _manual_payload(_input: Mapping[str, Any]) -> dict:
    """Return Avatar's packaged local manual without touching the host or disk tree."""
    resource = resources.files(__package__).joinpath("manual/SKILL.md")
    try:
        body = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, AttributeError, OSError):
        return {
            "status": "degraded",
            "action": "manual",
            "manual": "",
            "manual_path": str(resource),
            "error": "avatar manual missing",
        }
    return {
        "status": "ok",
        "action": "manual",
        "manual": body,
        "manual_path": str(resource),
    }


def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
    raise AssertionError("the module-level schema-only ToolFamily never dispatches")


def _build_family(
    handlers: Mapping[str, Any] | None = None,
    *,
    settings_provider: SettingsProvider | None = None,
) -> ToolFamily:
    """Compose Avatar's declared actions plus its local, reserved manual child."""
    action_handlers = (
        {name: _unused for name, _ in _DECLARED_CHILD_SPECS}
        if handlers is None
        else handlers
    )
    return ToolFamily(
        DECLARATION.name,
        [
            *[
                ChildTool(name, schema, action_handlers[name], title=f"{name} input")
                for name, schema in _DECLARED_CHILD_SPECS
            ],
            ChildTool(
                "manual",
                DECLARATION.manual_input_schema,
                _manual_payload,
                title="manual input",
            ),
        ],
        settings_provider=(
            settings_provider
            if settings_provider is not None
            else AvatarSettingsProvider()
        ),
    )


def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Compose Avatar against its granted ports; the registrar owns mounting."""
    manager = AvatarManager(host)
    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=manager,
        description=DECLARATION.description,
        glossary_package=DECLARATION.glossary_package,
    )


#: Static official declaration. Avatar consumes only the working directory and
#: its dedicated parent-context port; it has no prompt, mount, or whole-Agent
#: access. ``manual`` names Avatar's own packaged local-manual slot.
DECLARATION = ToolPluginDeclaration(
    name="avatar",
    actions=tuple(name for name, _ in _DECLARED_CHILD_SPECS),
    input_schemas=dict(_DECLARED_CHILD_SPECS),
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="avatar",
    description=_DESCRIPTION,
    binder=_bind,
    requires=("workdir", "avatar_parent"),
    glossary_package=__package__,
    settings=True,
)

# Kept as the compatibility-visible complete action/spec registry, but derived
# from the declaration so schema-only and host-bound families cannot drift.
_CHILD_SPECS: tuple[tuple[str, Mapping[str, Any]], ...] = tuple(
    DECLARATION.public_input_schemas().items()
)
_SUPPORTED_ACTIONS_PHRASE = (
    "".join(f"{action!r}, " for action in DECLARATION.public_actions[:-1])
    + f"or {DECLARATION.public_actions[-1]!r}"
)

# Schema-only family: dispatchable families are built per granted host by
# ``AvatarManager``. Construction catches a malformed child registry at import.
_FAMILY = _build_family()


def get_description(lang: str = "en") -> str:
    return DECLARATION.description


def get_schema(lang: str = "en") -> dict[str, Any]:
    """Compose the LTP v2 model-facing schema for the official ``avatar`` tool."""
    return _FAMILY.build_schema()


class AvatarManager:
    """Spawns avatar (分身) peer agents as detached processes.

    Each avatar gets its own working directory with init.json and is
    launched via `lingtai-agent run`.  No in-process references — liveness
    is checked via the filesystem through the agent-presence store.
    """

    def __init__(self, host: "ToolPluginHost", launcher: AvatarLauncherPort | None = None):
        self._host = host
        if launcher is None:
            from lingtai.adapters.avatar_launcher import select_avatar_launcher
            launcher = select_avatar_launcher()
        self._launcher = launcher
        # The spawn mission brief reaches ``_spawn`` out-of-band via
        # ``self._pending_reasoning``, set by ``handle()`` (see ``handle``).
        self._family = _build_family(
            {
                "spawn": self._dispatch_spawn,
            }
        )
        self._pending_reasoning: str | None = None

    # ------------------------------------------------------------------
    # Handler
    # ------------------------------------------------------------------

    def __call__(self, args: dict | None) -> dict:
        """The official registrar mounts this manager itself as the handler."""
        return self.handle(args)

    def handle(self, args: dict | None) -> dict:
        """Dispatch one action through the family, normalizing avatar's errors.

        Root ``reasoning`` (normalized to ``_reasoning`` by ToolExecutor) is the
        avatar's mission brief and becomes the newborn's first prompt. It is
        envelope metadata, never action input, so it is captured here and handed
        to ``_spawn`` out-of-band rather than smuggled into ``input``, and
        cleared in ``finally`` so a later call cannot inherit a previous call's
        mission.

        Avatar's pre-migration unknown-action envelope is a pinned public
        promise; the generic dispatcher's ``ACTION_REQUIRED`` shape is
        deliberately generic, so it is normalized back here, after dispatch —
        never by changing that dispatcher's own canonical error shape.
        """
        raw = args if isinstance(args, Mapping) else {}
        reasoning = raw.get("_reasoning")
        self._pending_reasoning = reasoning if isinstance(reasoning, str) else None
        try:
            try:
                result = self._family.handle(args)
            except Exception as exc:
                from lingtai.kernel.provider_admission import DerivedLaunchAdmissionError

                if not isinstance(exc, DerivedLaunchAdmissionError):
                    raise
                return {
                    "error": str(exc),
                    "reason_code": exc.decision.reason_code,
                    "audit_id": exc.decision.audit_id,
                }
        finally:
            self._pending_reasoning = None
        if result.get("error_code") == "ACTION_REQUIRED":
            action = raw.get("action", "")
            return {
                "error": (
                    f"unknown action: {action!r}, only "
                    f"{_SUPPORTED_ACTIONS_PHRASE} is supported"
                ),
            }
        return result

    @staticmethod
    def _strip_nulls(action_input: Mapping[str, Any]) -> dict[str, Any]:
        # Strict OpenAI schemas express optional fields as required nullable
        # properties. Null means absent to the internal action handlers, so
        # every default below stays exactly the pre-migration default.
        return {key: value for key, value in action_input.items() if value is not None}

    def _dispatch_spawn(self, action_input: Mapping[str, Any]) -> dict:
        return self._spawn(self._strip_nulls(action_input), self._pending_reasoning)

    # ------------------------------------------------------------------
    # Ledger (append-only JSONL log of avatar spawn events)
    # ------------------------------------------------------------------

    @property
    def _ledger_path(self) -> Path:
        return self._host.workdir.path / "delegates" / "ledger.jsonl"

    def _append_ledger(self, event: str, name: str, **fields) -> None:
        """Append a single event record to the ledger."""
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "event": event, "name": name, **fields}
        with open(self._ledger_path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Core spawn
    # ------------------------------------------------------------------

    def _spawn(self, args: dict, reasoning: str | None = None) -> dict:
        """Create one avatar. ``reasoning`` is the mission brief from the root
        envelope (``handle()``), not an ``input`` property — it becomes the
        newborn's first prompt and gates the mission-quality check."""
        parent_working_dir = self._host.workdir.path
        peer_name = args.get("name")
        avatar_type = args.get("type", SPAWN_TYPE_DEFAULT)
        dry_run = bool(args.get("dry_run", SPAWN_DRY_RUN_DEFAULT))
        confirm = bool(args.get("confirm", SPAWN_CONFIRM_DEFAULT))

        if peer_name is None:
            return {"error": "name is required — pick a true name (真名) for the 他我 (e.g. 'researcher', '学者')"}

        if avatar_type not in SPAWN_TYPES:
            return {"error": "type must be 'shallow' or 'deep'"}

        # Name doubles as working-dir basename. Enforce a safe, single-segment
        # name so an LLM-chosen string cannot traverse, target absolute paths,
        # or nest avatars inside subfolders (which would desync path-identity
        # from the ledger and mail-routing layer).
        if (
            not isinstance(peer_name, str)
            or len(peer_name) < AVATAR_NAME_MIN_CHARACTERS
            or peer_name in (".", "..")
            or peer_name.startswith(".")
            or len(peer_name) > AVATAR_NAME_MAX_CHARACTERS
            or not _AVATAR_NAME_RE.match(peer_name)
        ):
            return {
                "error": (
                    f"Invalid avatar name '{peer_name}': must be a bare directory "
                    f"name — letters (any script), digits, underscore, or hyphen; "
                    f"no slashes, dots, spaces, or leading '.'; "
                    f"{AVATAR_NAME_MIN_CHARACTERS}-{AVATAR_NAME_MAX_CHARACTERS} chars."
                )
            }

        # Mission-quality gate. The reasoning field becomes the avatar's first
        # prompt, so an empty / very-short / debug-placeholder mission almost
        # always means an accidental spawn (a real incident: an agent batched
        # avatar_spawn into a parallel call with mission "test" and a process
        # was created). Refuse unless the caller explicitly passes confirm=True.
        # The dry-run path is exempt — its whole purpose is preview without
        # commitment, and forcing confirm=True there would defeat that.
        if not dry_run and not confirm:
            unsafe, reason = _mission_looks_unsafe(reasoning or "")
            if unsafe:
                preview_mission = (reasoning or "").strip()
                return {
                    "status": "confirmation_needed",
                    "warning": (
                        f"Mission appears short/test-like ({reason}). "
                        f"Pass confirm=true to proceed, or dry_run=true to preview. "
                        f"Each avatar(action='spawn') call creates an independent process — "
                        f"double-check your reasoning field before retrying."
                    ),
                    "reason": reason,
                    "preview": {
                        "name": peer_name,
                        "type": avatar_type,
                        "mission": preview_mission,
                        "mission_chars": len(preview_mission),
                    },
                }

        # Check if this peer already exists and is live
        for record in self._read_ledger():
            if record.get("name") == peer_name:
                wd = record.get("working_dir", "")
                if wd and _is_alive(wd):
                    return {
                        "status": "already_active",
                        "working_dir": wd,
                        "message": (
                            f"'{peer_name}' is already running. "
                            f"Use mail to communicate, or system intrinsic to manage lifecycle."
                        ),
                    }

        # Parent must have init.json
        parent_init_path = parent_working_dir / "init.json"
        if not parent_init_path.is_file():
            return {"error": "parent has no init.json — cannot spawn avatar"}

        try:
            parent_init = json.loads(parent_init_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return {"error": f"failed to read parent init.json: {e}"}
        try:
            from lingtai.tools.psyche.settings import read_prompt_owner_values

            parent_psyche = read_prompt_owner_values(parent_working_dir)
        except Exception:
            return {"error": "failed to read parent Psyche settings"}

        # Dry-run short-circuit. Returns a preview of what would be created,
        # but performs NO filesystem mutation and NO process launch. We've
        # already validated name/type and confirmed parent has a usable
        # init.json, so the preview reflects what a real spawn would do.
        if dry_run:
            avatar_working_dir = parent_working_dir.parent / peer_name
            preview_mission = (reasoning or "").strip()
            unsafe, reason = _mission_looks_unsafe(reasoning or "")
            return {
                "status": "dry_run",
                "preview": {
                    "name": peer_name,
                    "type": avatar_type,
                    "working_dir": str(avatar_working_dir),
                    "address": avatar_working_dir.name,
                    "mission": preview_mission,
                    "mission_chars": len(preview_mission),
                    "mission_unsafe": unsafe,
                    "mission_reason": reason if unsafe else "",
                    "comment": args.get("comment", SPAWN_COMMENT_DEFAULT),
                },
                "message": "Dry run — no process spawned, no files written.",
            }

        launch_decision = self._authorize_derived_launch(peer_name)
        lease_handed_to_launcher = False
        try:

        # Working dir: sibling of parent, named after the avatar. Defense-in-depth
        # scope check — resolve and assert the target's parent equals the network
        # root, so even if peer_name validation is ever loosened, this still
        # prevents writing outside .lingtai/<siblings>/.
            avatar_working_dir = parent_working_dir.parent / peer_name
            network_root = parent_working_dir.parent.resolve()
            try:
                resolved = avatar_working_dir.resolve(strict=False)
            except (OSError, RuntimeError) as e:
                return {"error": f"Cannot resolve avatar path: {e}"}
            if resolved.parent != network_root:
                return {
                    "error": (
                        f"Avatar path '{avatar_working_dir}' escapes the network root "
                        f"'{network_root}' — rejected."
                    )
                }
            if avatar_working_dir.exists():
                return {"error": f"Directory '{peer_name}' already exists. Choose another name."}

        # Prepare the avatar's working directory
            parent_name = self._host.avatar_parent.parent_name or parent_working_dir.name

        # Copy init.json and launch lingtai
            if avatar_type == "deep":
                self._prepare_deep(parent_working_dir, avatar_working_dir)
            else:
                avatar_working_dir.mkdir(parents=True, exist_ok=True)

        # Keep the one remaining active init path meaningful from the child.
        # Psyche owner pointers are resolved by its reader against the parent
        # workdir before the narrow child document is produced below.
            for key in ("env_file",):
                val = parent_init.get(key)
                if val and not os.path.isabs(val):
                    resolved = parent_working_dir / val
                    if resolved.is_file():
                        parent_init[key] = str(resolved)

        # Inherit the parent runtime location through Avatar's narrow host port.
            venv_path = self._host.avatar_parent.venv_path
            if venv_path:
                parent_init["venv_path"] = venv_path

        # Clean stale signal files before launch
            for sig in (".suspend", ".sleep", ".interrupt"):
                sig_file = avatar_working_dir / sig
                if sig_file.is_file():
                    sig_file.unlink(missing_ok=True)

        # Seed the avatar's first turn with a parent-identity prompt + the
        # caller's reasoning (task brief). Written to the avatar's `.prompt`
        # file — picked up by the kernel's signal-file watcher on first poll
        # and delivered as a one-shot system message (consumed-once via unlink).
            parent_address = parent_working_dir.name
            avatar_lang = parent_init.get("manifest", {}).get("language", "en")
            parent_prompt = t(
                avatar_lang, "avatar.parent_prompt",
                parent_name=parent_name,
                parent_address=parent_address,
            )
            first_prompt = parent_prompt
            if reasoning and reasoning.strip():
                first_prompt = f"{parent_prompt}\n\n{reasoning.strip()}"

        # Write avatar's init.json (modified copy of parent's).
            avatar_comment = args.get("comment", SPAWN_COMMENT_DEFAULT)
            avatar_init = self._make_avatar_init(
                parent_init, peer_name, comment=avatar_comment,
                parent_working_dir=parent_working_dir,
            )
            (avatar_working_dir / "init.json").write_text(
                json.dumps(avatar_init, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            avatar_psyche = self._make_avatar_psyche_settings(
                parent_psyche,
                comment=avatar_comment,
            )
            (avatar_working_dir / "settings").mkdir(exist_ok=True)
            (avatar_working_dir / "settings" / "psyche.json").write_text(
                avatar_psyche,
                encoding="utf-8",
            )

            # Only a Driver-granted child endpoint makes this avatar derived.
            # Generic LingTai's historical grant contains no endpoint lease and
            # therefore keeps its existing boot behavior.
            derived_child = launch_decision.child_endpoint_lease is not None
            if derived_child:
                atomic_write_json(
                    derived_avatar_state_path(avatar_working_dir),
                    DERIVED_AVATAR_STATE,
                    sort_keys=True,
                )

        # Drop the spawn prompt as a `.prompt` signal file — the avatar's
        # kernel watcher consumes it on first poll and delivers it once.
            (avatar_working_dir / ".prompt").write_text(first_prompt, encoding="utf-8")

        # Launch as detached process and wait briefly for the child to either
        # write its handshake (.agent.heartbeat) or exit. If the child exits
        # before handshaking, the spawn failed — capture stderr, ledger the
        # failure, and return an error to the caller. Without this check the
        # avatar capability returns "ok" the instant a child forks, even if the
        # child crashes 50ms later (e.g. invalid init.json), and the parent's
        # LLM has no idea anything went wrong.
            proc, stderr_path = self._launch(
                avatar_working_dir,
                authority_lease=launch_decision.child_endpoint_lease,
                derived_child=derived_child,
            )
            lease_handed_to_launcher = True
            pid = proc.pid

            try:
                boot_status, boot_error = self._wait_for_boot(
                    avatar_working_dir, proc, stderr_path,
                )
            finally:
                self._launcher.release(proc.handle)

        # Record in ledger — include boot status so post-mortem can distinguish
        # successful spawns from failed ones without re-checking the filesystem.
            ledger_extra = {"boot_status": boot_status}
            if boot_error:
                ledger_extra["boot_error"] = boot_error
            self._append_ledger(
                "avatar", peer_name,
                working_dir=avatar_working_dir.name,
                mission=reasoning or "",
                type=avatar_type,
                pid=pid,
                **ledger_extra,
            )

            if boot_status == "failed":
                return {
                    "error": (
                        f"avatar {peer_name!r} failed to boot: {boot_error}. "
                        f"See {stderr_path} for details."
                    ),
                    "address": avatar_working_dir.name,
                    "agent_name": peer_name,
                    "pid": pid,
                }

            result = {
                "status": "ok",
                "address": avatar_working_dir.name,
                "agent_name": peer_name,
                "type": avatar_type,
                "pid": pid,
            }
            if boot_status == "slow":
                # Process is still alive but didn't finish handshaking in the
                # window — surface a warning so the caller knows to monitor it.
                result["warning"] = (
                    f"avatar still booting after {self._BOOT_WAIT_SECS}s — "
                    f"check .agent.heartbeat freshness before relying on it"
                )
            return result
        finally:
            if not lease_handed_to_launcher:
                self._close_unconsumed_launch_lease(launch_decision)

    def _wait_for_boot(
        self, working_dir: Path, proc: AvatarLaunchReceipt, stderr_path: Path,
    ) -> tuple[str, str | None]:
        """Wait for the avatar to write .agent.heartbeat or exit.

        Returns (status, error_message):
            - ("ok", None)     — heartbeat appeared before timeout
            - ("failed", msg)  — process exited before handshaking
            - ("slow", None)   — neither happened in BOOT_WAIT_SECS; process
                                 is still alive, caller should monitor
        """
        heartbeat = working_dir / ".agent.heartbeat"
        deadline = time.monotonic() + self._BOOT_WAIT_SECS
        while time.monotonic() < deadline:
            if heartbeat.is_file():
                return ("ok", None)
            rc = self._launcher.poll(proc.handle)
            if rc is not None:
                # Child exited before writing heartbeat. Tail stderr (capped)
                # so the parent's LLM gets a useful, bounded error string.
                stderr_tail = ""
                try:
                    raw = stderr_path.read_bytes()
                    if len(raw) > BOOT_STDERR_TAIL_BYTES:
                        raw = (
                            b"...[truncated]...\n"
                            + raw[-BOOT_STDERR_TAIL_BYTES:]
                        )
                    stderr_tail = raw.decode("utf-8", errors="replace").strip()
                except OSError:
                    pass
                msg = f"process exited with code {rc}"
                if stderr_tail:
                    msg = f"{msg}: {stderr_tail}"
                return ("failed", msg)
            time.sleep(self._BOOT_POLL_INTERVAL)
        return ("slow", None)

    # ------------------------------------------------------------------
    # Init.json construction
    # ------------------------------------------------------------------

    @staticmethod
    def _make_avatar_init(
        parent_init: dict, name: str, *,
        comment: str = "",
        parent_working_dir: "Path | None" = None,
    ) -> dict:
        """Build avatar's init.json from parent's, setting name.

        The spawn brief (parent identity + reasoning) is delivered out-of-band
        via a `.prompt` signal file dropped in the avatar's working dir by the
        caller — see ``_spawn``. Here we only blank the inherited `lingtai`
        character seed so the schema sees a present-but-empty required field (no
        stale identity carried over). The `.prompt` signal file is a runtime
        text-injection channel and is unrelated to the renamed `lingtai` seed.

        Avatars inherit the parent's `manifest.preset.allowed` list verbatim.
        Entries are stored as path strings; if any are relative, they are
        re-rooted against ``parent_working_dir`` (if given) so the avatar's
        own working dir doesn't change their meaning.
        """
        init = json.loads(json.dumps(parent_init))  # deep copy
        init["manifest"]["agent_name"] = name
        # Blank inherited `lingtai` — schema requires the character seed field to
        # exist, but the avatar starts with no inherited 灵台; its actual first
        # prompt arrives via the `.prompt` signal file (a separate runtime
        # channel, not the `lingtai` seed).
        init["lingtai"] = ""
        init.pop("lingtai_file", None)
        # Avatar has no admin privileges
        init["manifest"]["admin"] = {}
        # Psyche owns all configurable prompt pairs. Their old init spellings
        # are deliberately removed rather than copied as inert compatibility
        # data; the spawn-specific owner document is built separately below.
        for key in (
            "base_prompt", "base_prompt_file",
            "covenant", "covenant_file",
            "comment", "comment_file",
            "principle", "principle_file",
            "procedures", "procedures_file",
            "substrate", "substrate_file",
            "brief", "brief_file",
        ):
            init.pop(key, None)
        # Addons (IMAP, Telegram) are not inherited — each agent must be
        # explicitly configured to avoid multiple agents polling the same account
        init.pop("addons", None)
        # Re-root any relative paths in preset.{default,active,allowed}
        # against the parent's working dir so they remain valid from the
        # avatar's different working directory. Absolute and ~-prefixed
        # entries pass through unchanged.
        if parent_working_dir is not None:
            preset_block = init["manifest"].get("preset")
            if isinstance(preset_block, dict):
                def _reroot(s: object) -> object:
                    if not isinstance(s, str) or not s:
                        return s
                    p = Path(s).expanduser()
                    if p.is_absolute():
                        return s
                    return str((Path(parent_working_dir) / p).resolve())
                for key in ("default", "active"):
                    if isinstance(preset_block.get(key), str):
                        preset_block[key] = _reroot(preset_block[key])
                allowed = preset_block.get("allowed")
                if isinstance(allowed, list):
                    preset_block["allowed"] = [_reroot(x) for x in allowed]

        # Avatars always spawn on the parent's DEFAULT preset, not its
        # currently-active one. This keeps the avatar's notion of 'default'
        # well-defined as a peer in the network — auto-fallback targets a
        # stable home base, not whatever transient preset the parent happened
        # to be on at spawn time.
        #
        # Strip materialized llm + capabilities unconditionally so the avatar's
        # _read_init re-materializes from the (possibly-rewritten) active on
        # first boot. Letting the existing materialization path do its job
        # is cleaner than manually re-substituting here.
        preset_block = init["manifest"].get("preset")
        if isinstance(preset_block, dict) and preset_block.get("default"):
            preset_block["active"] = preset_block["default"]
            init["manifest"].pop("llm", None)
            init["manifest"].pop("capabilities", None)

        return init

    @staticmethod
    def _make_avatar_psyche_settings(
        parent_values: Mapping[str, str], *, comment: str,
    ) -> str:
        """Build the narrow child prompt-owner document.

        Only Psyche's base-prompt and covenant pairs carry forward. A parent
        relative pointer has already been anchored by the owner reader, so this
        preserves its source meaning without copying any System runtime policy
        or another owner's settings document. The child always gets its spawn
        comment and never inherits a parent comment pointer.
        """
        from lingtai.tools.psyche.settings import serialize_prompt_owner_document

        return serialize_prompt_owner_document(
            base_prompt=parent_values.get("base_prompt"),
            base_prompt_file=parent_values.get("base_prompt_file"),
            covenant=parent_values.get("covenant"),
            covenant_file=parent_values.get("covenant_file"),
            comment=comment,
        )

    # ------------------------------------------------------------------
    # Deep copy — 二重身
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_deep(src: Path, dst: Path) -> None:
        """Copy identity + knowledge from parent, excluding runtime state.

        Guarded: dst must be a direct sibling of src (same parent). This mirrors
        the path-scope assertion in _spawn so the rmtree() calls below cannot
        reach outside the network root even if _prepare_deep is ever called
        from a future, less-validated path.
        """
        src_resolved = src.resolve(strict=False)
        dst_resolved = dst.resolve(strict=False)
        if dst_resolved.parent != src_resolved.parent:
            raise ValueError(
                f"_prepare_deep refused: dst '{dst}' is not a sibling of src '{src}' "
                f"(parents differ: {dst_resolved.parent} vs {src_resolved.parent})"
            )
        dst.mkdir(parents=True, exist_ok=True)

        # system/ (character, pad, covenant, etc.)
        src_system = src / "system"
        if src_system.is_dir():
            dst_system = dst / "system"
            if dst_system.exists():
                shutil.rmtree(dst_system)
            shutil.copytree(src_system, dst_system)

        # knowledge/
        src_knowledge = src / "knowledge"
        if src_knowledge.is_dir():
            dst_knowledge = dst / "knowledge"
            if dst_knowledge.exists():
                shutil.rmtree(dst_knowledge)
            shutil.copytree(src_knowledge, dst_knowledge)

        # exports/
        src_exports = src / "exports"
        if src_exports.is_dir():
            dst_exports = dst / "exports"
            if dst_exports.exists():
                shutil.rmtree(dst_exports)
            shutil.copytree(src_exports, dst_exports)

        # combo.json
        src_combo = src / "combo.json"
        if src_combo.is_file():
            shutil.copy2(src_combo, dst / "combo.json")

        # Explicitly do NOT copy: history/, mailbox/, delegates/,
        # .agent.json, .agent.heartbeat, logs/

    # ------------------------------------------------------------------
    # Process launch
    # ------------------------------------------------------------------

    # Boot verification — how long to wait for the child to write .agent.heartbeat
    # before we conclude it crashed. Healthy boots finish well under 2s on local
    # disk; 5s is generous enough for slow systems to still pass.
    _BOOT_WAIT_SECS = BOOT_WAIT_SECONDS
    _BOOT_POLL_INTERVAL = BOOT_POLL_INTERVAL_SECONDS

    def _launch(
        self,
        working_dir: Path,
        *,
        authority_lease: "DerivedLaunchEndpointLease | None" = None,
        derived_child: bool = False,
    ) -> tuple[AvatarLaunchReceipt, Path]:
        """Launch `lingtai-agent run <dir>` as a fully detached process.

        Captures stderr to ``logs/spawn.stderr`` so a child that exits before
        writing its handshake leaves a usable diagnostic behind. Returns the
        opaque launch receipt (so callers can poll for early exit) plus the
        stderr path.
        """
        from lingtai.venv_resolve import resolve_venv, venv_python

        # Resolve Python from avatar's init.json → global runtime
        init_path = working_dir / "init.json"
        init_data = None
        if init_path.is_file():
            try:
                init_data = json.loads(init_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        venv_dir = resolve_venv(init_data)
        python = venv_python(venv_dir)
        cmd = (python, "-m", "lingtai", "run", str(working_dir))

        # Ensure logs/ exists for stderr capture; the kernel also creates this
        # on boot, but we need it before the child has run.
        logs_dir = working_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = logs_dir / "spawn.stderr"
        receipt = self._launcher.launch(
            AvatarLaunchRequest(
                argv=cmd,
                stderr_path=stderr_path,
                # Redundant immediate-launch signal only. The durable child
                # state is authoritative for restarts and carries no bearer.
                environment=(
                    {DERIVED_AVATAR_EXECUTION_ENV: "1"} if derived_child else None
                ),
                authority_lease=authority_lease,
            )
        )
        return receipt, stderr_path

    @staticmethod
    def _close_unconsumed_launch_lease(decision: "DerivedLaunchDecision") -> None:
        """Close a Driver endpoint unless ownership reached the launcher Port."""
        lease = decision.child_endpoint_lease
        if lease is not None:
            try:
                lease.close()
            except OSError:
                pass

    def _authorize_derived_launch(self, peer_name: str) -> "DerivedLaunchDecision":
        """Reach the host decision seam before avatar filesystem/process effects."""
        from lingtai.kernel.provider_admission import (
            DerivedLaunchAdmissionError,
            DerivedLaunchCapability,
        )

        decision: DerivedLaunchDecision | None = None
        transferred = False
        try:
            try:
                decision = self._host.avatar_parent.authorize_derived_launch(
                    DerivedLaunchCapability.AVATAR
                )
            except DerivedLaunchAdmissionError as exc:
                decision = exc.decision
                self._append_ledger(
                    "avatar_admission_decision",
                    peer_name,
                    capability=DerivedLaunchCapability.AVATAR.value,
                    state=decision.state.value,
                    reason_code=decision.reason_code,
                    audit_id=decision.audit_id,
                )
                raise
            self._append_ledger(
                "avatar_admission_decision",
                peer_name,
                capability=DerivedLaunchCapability.AVATAR.value,
                state=decision.state.value,
                reason_code=decision.reason_code,
                audit_id=decision.audit_id,
            )
            if not decision.allowed:
                raise DerivedLaunchAdmissionError(decision)
            transferred = True
            return decision
        finally:
            if decision is not None and not transferred:
                self._close_unconsumed_launch_lease(decision)

    # ------------------------------------------------------------------
    # Ledger reading
    # ------------------------------------------------------------------

    def _read_ledger(self) -> list[dict]:
        """Read all ledger records."""
        if not self._ledger_path.is_file():
            return []
        records = []
        for line in self._ledger_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

def setup(agent: "BaseAgent", **_ignored) -> AvatarManager:
    """Register Avatar through the official declared-host-plugin route."""
    from lingtai.adapters.tool_plugin_host import register_agent_tool_plugins

    (bound,) = register_agent_tool_plugins(agent, [DECLARATION])
    if not isinstance(bound.handler, AvatarManager):
        raise RuntimeError("avatar declaration did not bind its AvatarManager")
    return bound.handler
