"""Operator-managed runtime registry for the constrained Puffo ACP profile."""
from __future__ import annotations

from contextlib import contextmanager, suppress
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from lingtai.kernel.turns import TurnAdmissionDecision, TurnOrigin
from lingtai.kernel.provider_admission import (
    DerivedLaunchCapability,
    DerivedLaunchDecision,
    ProviderAdmissionParent,
    ProviderAdmissionState,
    ProviderCallClass,
    ProviderCallDecision,
    RootProviderAdmission,
)


PROFILE_NAME = "puffo-v0"
REGISTRY_VERSION = 4
REVOCATION_LOG_REQUIRED = "required"
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class PuffoV0RuntimePolicy:
    """The full-tool profile's one remaining provider-turn boundary.

    This is intentionally not a tool sandbox. It admits only authenticated
    driving-adapter turns while leaving the operator-managed LingTai tool
    surface intact. State written by other sources may still be read during a
    later admitted turn; the boundary is initiation, not content provenance.
    """

    policy_version: str = "puffo-v0.full-tool-acp-ingress.v1"
    tool_surface: str = "operator_managed_full"

    def admit_turn_origin(self, origin: TurnOrigin) -> TurnAdmissionDecision:
        allowed = origin is TurnOrigin.AUTHENTICATED_ADAPTER
        return TurnAdmissionDecision(
            allowed=allowed,
            origin=origin,
            policy_version=self.policy_version,
            reason_code="allowed" if allowed else "origin_not_authenticated_adapter",
        )

    def authorize_provider_call(
        self,
        parent: ProviderAdmissionParent,
        call_class: ProviderCallClass,
    ) -> ProviderCallDecision:
        """Provide the Core-only structural half of Puffo provider admission.

        The driver-owned socket adapter will replace this root-only policy with
        a per-call host-mediated implementation for daemon/avatar work.  Until
        then, fail closed rather than allowing a derived provider call to use a
        root turn's typed origin as a transferable authority.
        """

        allowed = (
            call_class is ProviderCallClass.ROOT
            and isinstance(parent, RootProviderAdmission)
            and parent.policy_version == self.policy_version
        )
        return ProviderCallDecision(
            state=(
                ProviderAdmissionState.GRANTED
                if allowed
                else ProviderAdmissionState.INDETERMINATE
            ),
            reason_code=(
                "allowed"
                if allowed
                else "derived_admission_port_unconnected"
            ),
        )

    def authorize_derived_launch(
        self,
        _parent: RootProviderAdmission,
        _capability: DerivedLaunchCapability,
    ) -> DerivedLaunchDecision:
        """Refuse launch until the Driver-owned authority transport is wired."""

        return DerivedLaunchDecision(
            ProviderAdmissionState.INDETERMINATE,
            "derived_launch_admission_port_unconnected",
        )


RUNTIME_POLICY = PuffoV0RuntimePolicy()


class PuffoV0RegistryError(ValueError):
    """A registry failure safe to expose as a bounded local startup error."""


@dataclass(frozen=True, slots=True)
class DirectoryBinding:
    """The stable local filesystem identity of one bound directory."""

    device: int
    inode: int
    owner: int
    group: int


@dataclass(frozen=True, slots=True)
class PuffoV0Runtime:
    """One pre-provisioned local identity selected by an opaque runtime id."""

    runtime_id: str
    agent_dir: Path
    workspace: Path
    entry_digest: str
    agent_dir_binding: DirectoryBinding
    workspace_binding: DirectoryBinding
    policy_version: str


class PuffoV0RuntimeState(Enum):
    """Classification of one agent_dir's registry status, keyed by caller action.

    The ``value`` is the stable external status string the discover CLI emits.
    The governing invariant: any two states that require a *different* caller
    response must have different values here, so a consumer never sees the same
    representation for "you may provision" and "stop, this looks tampered".

    Member *declaration* order here carries no meaning and is safe to change.
    The multi-entry precedence for one agent_dir is defined explicitly and
    independently by ``_DISCOVERY_STATE_PRECEDENCE`` (and pinned by tests),
    precisely so that it cannot be altered by a cosmetic reordering here.
    """

    INTEGRITY_FAILED = "integrity_failed"
    SHAPE_MISMATCH = "shape_mismatch"
    POLICY_VERSION_MISMATCH = "policy_version_mismatch"
    STALE_BINDING = "stale_binding"
    ACTIVE = "bound"
    REVOKED = "revoked"
    PROVISIONABLE = "available"


# Precedence for reporting one agent_dir that appears in several registry
# entries: report the state that most constrains the caller.  Lower index wins.
#
# This ordering is SAFETY-LOAD-BEARING and is therefore written out explicitly
# here, NOT derived from the enum's member declaration order.  The reason:
# INTEGRITY_FAILED must outrank REVOKED (and every recoverable state) so that a
# directory holding both a tampered entry and a revoked one is reported as the
# integrity failure ("stop, escalate, never auto-revoke") rather than the
# revoked one ("re-provision under a new id").  If precedence merely mirrored
# the enum's source order, a purely cosmetic edit -- reordering members,
# alphabetising, or appending a new member "where it reads nicely" -- would
# silently reopen the auto-revoke-a-tampered-entry hole with nothing to catch
# it.  ``test_discovery_state_precedence_*`` pin both the exact order and that
# every rankable state appears exactly once; reordering this tuple reds them.
# PROVISIONABLE is excluded: it describes a directory with no entry at all, so
# it never competes with an entry-derived state for the same directory.
_DISCOVERY_STATE_PRECEDENCE: tuple[PuffoV0RuntimeState, ...] = (
    PuffoV0RuntimeState.INTEGRITY_FAILED,
    PuffoV0RuntimeState.SHAPE_MISMATCH,
    PuffoV0RuntimeState.POLICY_VERSION_MISMATCH,
    PuffoV0RuntimeState.STALE_BINDING,
    PuffoV0RuntimeState.ACTIVE,
    PuffoV0RuntimeState.REVOKED,
)


@dataclass(frozen=True, slots=True)
class PuffoV0DiscoveryCandidate:
    """One initialized identity found under an operator-selected directory."""

    agent_dir: Path
    workspace: Path | None
    display_name: str
    runtime_id: str | None
    state: PuffoV0RuntimeState
    # Advisory only, and set ONLY on an `available` (PROVISIONABLE) candidate: the
    # runtime_id a registry entry recorded at this exact path, whose provisioned
    # identity is no longer the directory now sitting there (it moved elsewhere or
    # is gone). It does NOT change the action -- the directory is genuinely unbound
    # and provisioning it succeeds -- it hangs a sign that this path was reused, so
    # a same-path replacement is not silently mistaken for a never-bound directory.
    formerly_bound_runtime_id: str | None = None


def default_registry_path() -> Path:
    """Return the one operator-managed registry location for this profile."""

    return Path.home() / ".lingtai" / PROFILE_NAME / "runtime-registry.json"


def _valid_runtime_id(runtime_id: object) -> str:
    if not isinstance(runtime_id, str) or _RUNTIME_ID.fullmatch(runtime_id) is None:
        raise PuffoV0RegistryError("runtime_id must be an opaque local identifier")
    return runtime_id


def _canonical_directory(path: Path, *, field: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        # ValueError covers an embedded NUL in a stored path ("embedded null
        # character"), which resolve() raises as a plain ValueError -- neither OSError
        # nor RuntimeError. Wrapping it here converts it to the bounded registry error
        # at the one chokepoint every path read (discover, guard, resolve) passes
        # through, so a digest-valid NUL path can never leak a raw ValueError.
        raise PuffoV0RegistryError(f"{field} must be an existing directory") from exc
    if not resolved.is_dir():
        raise PuffoV0RegistryError(f"{field} must be an existing directory")
    return resolved


def _directory_binding(path: Path, *, field: str) -> DirectoryBinding:
    """Read the no-symlink directory identity used by the local binding."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PuffoV0RegistryError(f"{field} must be an existing directory") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise PuffoV0RegistryError(f"{field} must be an existing non-symlink directory")
    return DirectoryBinding(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner=metadata.st_uid,
        group=metadata.st_gid,
    )


def _binding_payload(binding: DirectoryBinding) -> dict[str, int]:
    return {
        "device": binding.device,
        "group": binding.group,
        "inode": binding.inode,
        "owner": binding.owner,
    }


def _parse_binding(value: object) -> DirectoryBinding:
    if not isinstance(value, dict) or set(value) != {"device", "group", "inode", "owner"}:
        raise PuffoV0RegistryError("runtime registry entry has an invalid directory binding")
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in value.values()):
        raise PuffoV0RegistryError("runtime registry entry has an invalid directory binding")
    return DirectoryBinding(
        device=value["device"],
        inode=value["inode"],
        owner=value["owner"],
        group=value["group"],
    )


def _is_valid_binding_payload(value: object) -> bool:
    """Whether ``value`` is a well-formed stored directory binding, without I/O.

    A malformed binding payload is a shape defect of the entry itself, decided by
    the pure classifier so all four operations agree on it (discover, guard,
    resolve, revoke), rather than only surfacing later when a binding read fails.
    This validates the payload's *structure*; it does not read the filesystem, so
    a well-formed payload that no longer matches the live directory is a separate,
    liveness concern (STALE_BINDING), not a shape one.
    """

    try:
        _parse_binding(value)
    except PuffoV0RegistryError:
        return False
    return True


def _canonical_entry(
    runtime_id: str,
    agent_dir: Path,
    workspace: Path,
    agent_dir_binding: DirectoryBinding,
    workspace_binding: DirectoryBinding,
) -> dict[str, Any]:
    return {
        "agent_dir": str(agent_dir),
        "agent_dir_binding": _binding_payload(agent_dir_binding),
        "mcp_servers": [],
        "profile": PROFILE_NAME,
        "runtime_id": runtime_id,
        "status": "active",
        "tool_surface": RUNTIME_POLICY.tool_surface,
        "turn_origins": [TurnOrigin.AUTHENTICATED_ADAPTER.value],
        "runtime_policy_version": RUNTIME_POLICY.policy_version,
        "workspace": str(workspace),
        "workspace_binding": _binding_payload(workspace_binding),
    }


def _digest(entry: dict[str, Any]) -> str:
    canonical = json.dumps(entry, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_RUNTIME_ENTRY_KEYS = frozenset({
    "agent_dir", "agent_dir_binding", "entry_digest", "mcp_servers",
    "profile", "runtime_id", "runtime_policy_version", "status", "tool_surface",
    "turn_origins", "workspace", "workspace_binding",
})


def _classify_registry_entry(
    runtime_id: str, entry: object, *, revoked_runtime_ids: frozenset[str]
) -> PuffoV0RuntimeState:
    """Classify one registry entry by the action its state demands of the caller.

    Ordering is load-bearing: integrity (the ``entry_digest`` match) is decided
    before the policy-version comparison and independently of it.  A tampered or
    corrupt entry must never be read as a benign version drift, because the
    caller's recovery for a drift is an automatic revoke + re-provision -- running
    that on a forged entry would let an attacker drive the revocation.  Every
    field examined after the digest check is one the digest signed, so its value
    can be trusted; nothing before it can.

    This is the single source of truth for entry status.  It absorbs the former
    ``_has_runtime_entry_shape`` / ``_is_active_discovery_entry`` pair: discover,
    resolve, and provision all read the entry through this classifier so no two
    of them can disagree about what one entry means.
    """

    if not isinstance(entry, dict):
        return PuffoV0RuntimeState.SHAPE_MISMATCH
    # Integrity is decided first among the content checks, and digest *presence* is
    # part of integrity: a missing or non-string ``entry_digest`` is an integrity
    # failure, not a shape mismatch.  This is tested before the structural key-set
    # comparison on purpose -- a record we cannot authenticate must never read as a
    # merely malformed one, and (critically) revoke re-adds ``entry_digest`` and
    # re-signs, so a dropped digest classified as shape could be laundered into a
    # released state.  Keeping "no/invalid digest => integrity" holds that closed.
    digest = entry.get("entry_digest")
    if not isinstance(digest, str):
        return PuffoV0RuntimeState.INTEGRITY_FAILED
    if set(entry) != _RUNTIME_ENTRY_KEYS:
        return PuffoV0RuntimeState.SHAPE_MISMATCH
    canonical = {key: value for key, value in entry.items() if key != "entry_digest"}
    if _digest(canonical) != digest:
        return PuffoV0RuntimeState.INTEGRITY_FAILED
    # Authentic from here: every field below is one the digest signed, so its value
    # can be trusted.  The value-shape checks -- including an unknown ``status`` and
    # a syntactically invalid runtime id -- are decided BEFORE the revoked gate, and
    # the gate recognizes only an *explicit* revocation.  This is load-bearing: if a
    # value defect were decided after the gate, editing ``status`` to any non-active
    # value would route the entry to REVOKED (released) instead of the blocking
    # state it is -- exactly the laundering that let a foreign-profile or unknown-
    # status entry free its directory.  A malformed binding payload is a shape
    # defect of the entry too (decided here so discover, the guard, resolve, and
    # revoke all agree), distinct from a well-formed binding that no longer matches
    # the live directory (STALE_BINDING, a liveness check the pure classifier does
    # not run).
    if (
        entry.get("runtime_id") != runtime_id
        or _RUNTIME_ID.fullmatch(runtime_id) is None
        or entry.get("profile") != PROFILE_NAME
        or entry.get("mcp_servers") != []
        or entry.get("tool_surface") != RUNTIME_POLICY.tool_surface
        or entry.get("turn_origins") != [TurnOrigin.AUTHENTICATED_ADAPTER.value]
        or not isinstance(entry.get("agent_dir"), str)
        or not isinstance(entry.get("workspace"), str)
        or entry.get("status") not in ("active", "revoked")
        or not _is_valid_binding_payload(entry.get("agent_dir_binding"))
        or not _is_valid_binding_payload(entry.get("workspace_binding"))
    ):
        return PuffoV0RuntimeState.SHAPE_MISMATCH
    if runtime_id in revoked_runtime_ids or entry.get("status") == "revoked":
        return PuffoV0RuntimeState.REVOKED
    if entry.get("runtime_policy_version") != RUNTIME_POLICY.policy_version:
        return PuffoV0RuntimeState.POLICY_VERSION_MISMATCH
    return PuffoV0RuntimeState.ACTIVE


def _require_posix_registry_security() -> None:
    """Fail closed until puffo-v0 has an owner-only Windows ACL adapter.

    POSIX file modes are part of this profile's control-plane confidentiality
    boundary.  Windows cannot provide the equivalent guarantee through chmod,
    so this Phase A registry deliberately has no Windows implementation rather
    than silently creating a broadly readable registry there.
    """

    if os.name != "posix":
        raise PuffoV0RegistryError(
            "puffo-v0 registry requires POSIX owner-only filesystem permissions"
        )


def _secure_registry_directory(path: Path) -> None:
    """Create and harden the registry parent independently of umask."""

    _require_posix_registry_security()
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise PuffoV0RegistryError(
            "puffo-v0 runtime registry directory could not be secured"
        ) from exc


def _secure_registry_file(path: Path) -> bool:
    """Harden an existing registry artifact; return false when it is absent."""

    _require_posix_registry_security()
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry is unavailable or invalid") from exc
    if not stat.S_ISREG(mode):
        raise PuffoV0RegistryError("puffo-v0 runtime registry has an invalid file type")
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry could not be secured") from exc
    return True


def _revocation_log_path(path: Path) -> Path:
    """Return the append-only, owner-only tombstone log beside a registry."""

    return path.with_name(f".{path.name}.revocations.jsonl")


def _initialize_revocation_log(path: Path) -> None:
    """Create the mandatory empty tombstone log before first registry write."""

    _secure_registry_directory(path.parent)
    tombstones = _revocation_log_path(path)
    descriptor: int | None = None
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(tombstones, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise PuffoV0RegistryError(
            "puffo-v0 registry initialization found an unexpected revocation log"
        ) from exc
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 revocation log could not be initialized") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _read_revoked_runtime_ids(path: Path) -> frozenset[str]:
    """Read monotonic revocation tombstones, rejecting malformed local state."""

    tombstones = _revocation_log_path(path)
    if not _secure_registry_file(tombstones):
        raise PuffoV0RegistryError("puffo-v0 revocation log is unavailable or invalid")
    return _parse_revocation_log(tombstones)


def _parse_revocation_log(tombstones: Path) -> frozenset[str]:
    """Parse a tombstone file without changing its permissions or contents."""

    try:
        lines = tombstones.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PuffoV0RegistryError("puffo-v0 revocation log is unavailable or invalid") from exc
    revoked: set[str] = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PuffoV0RegistryError("puffo-v0 revocation log is unavailable or invalid") from exc
        if not isinstance(entry, dict) or set(entry) != {"runtime_id"}:
            raise PuffoV0RegistryError("puffo-v0 revocation log is unavailable or invalid")
        revoked.add(_valid_runtime_id(entry["runtime_id"]))
    return frozenset(revoked)


def _read_revoked_runtime_ids_read_only(path: Path) -> frozenset[str]:
    """Read tombstones for discovery without creating or hardening artifacts."""

    tombstones = _revocation_log_path(path)
    try:
        mode = tombstones.lstat().st_mode
    except FileNotFoundError as exc:
        raise PuffoV0RegistryError("puffo-v0 revocation log is unavailable or invalid") from exc
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 revocation log is unavailable or invalid") from exc
    if not stat.S_ISREG(mode):
        raise PuffoV0RegistryError("puffo-v0 revocation log is unavailable or invalid")
    return _parse_revocation_log(tombstones)


def _append_revocation_tombstone(path: Path, runtime_id: str) -> None:
    """Persist a terminal revocation before the mutable registry is updated."""

    _secure_registry_directory(path.parent)
    tombstones = _revocation_log_path(path)
    descriptor: int | None = None
    try:
        flags = os.O_APPEND | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(tombstones, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps({"runtime_id": runtime_id}, sort_keys=True) + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written == 0:
                raise OSError("short write to puffo-v0 revocation log")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 revocation log could not be written") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


@contextmanager
def _registry_mutation_lock(path: Path) -> Iterator[None]:
    """Serialize one registry read-modify-write across local processes."""

    _secure_registry_directory(path.parent)
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry lock is unavailable") from exc

    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        with suppress(OSError):
            os.close(descriptor)
        raise PuffoV0RegistryError("puffo-v0 runtime registry lock is unavailable") from exc
    try:
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(descriptor)


def _read_registry(path: Path) -> dict[str, Any]:
    _secure_registry_file(path)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry is unavailable or invalid") from exc
    if not isinstance(data, dict) or set(data) != {"revocation_log", "runtimes", "version"}:
        raise PuffoV0RegistryError("puffo-v0 runtime registry has an invalid shape")
    if (
        data["version"] != REGISTRY_VERSION
        or data["revocation_log"] != REVOCATION_LOG_REQUIRED
        or not isinstance(data["runtimes"], dict)
    ):
        raise PuffoV0RegistryError("puffo-v0 runtime registry has an unsupported version")
    return data


def _read_registry_read_only(path: Path) -> dict[str, Any]:
    """Read a registry for discovery without mutating its security metadata."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry is unavailable or invalid") from exc
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry is unavailable or invalid") from exc
    if not stat.S_ISREG(mode):
        raise PuffoV0RegistryError("puffo-v0 runtime registry has an invalid file type")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry is unavailable or invalid") from exc
    if not isinstance(data, dict) or set(data) != {"revocation_log", "runtimes", "version"}:
        raise PuffoV0RegistryError("puffo-v0 runtime registry has an invalid shape")
    if (
        data["version"] != REGISTRY_VERSION
        or data["revocation_log"] != REVOCATION_LOG_REQUIRED
        or not isinstance(data["runtimes"], dict)
    ):
        raise PuffoV0RegistryError("puffo-v0 runtime registry has an unsupported version")
    return data


def _write_registry(path: Path, data: dict[str, Any]) -> None:
    _secure_registry_directory(path.parent)
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_temporary)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise PuffoV0RegistryError("puffo-v0 runtime registry could not be written") from exc


def _bound_directory(
    path_value: object,
    binding_value: object,
    *,
    field: str,
) -> tuple[Path, DirectoryBinding]:
    """Resolve one stored path and require its present identity to match."""

    if not isinstance(path_value, str):
        raise PuffoV0RegistryError("runtime registry entry has invalid paths")
    expected = _parse_binding(binding_value)
    stored = Path(path_value)
    resolved = _canonical_directory(stored, field=field)
    if str(resolved) != path_value:
        raise PuffoV0RegistryError(f"{field} binding no longer matches its canonical path")
    observed = _directory_binding(resolved, field=field)
    if observed != expected:
        raise PuffoV0RegistryError(f"{field} binding no longer matches its provisioned identity")
    return resolved, expected


def _binding_matches(entry: dict[str, Any]) -> bool:
    """Return whether an authentic entry's on-disk identity still resolves.

    Discovery calls this ONLY for an entry the classifier already ruled ACTIVE,
    to decide whether ``bound`` is truthful.  It runs the exact same check
    ``resolve_runtime`` enforces -- ``_bound_directory`` for both the agent_dir
    and the workspace -- so ``bound`` from discovery means precisely "resolve
    would succeed here".  Any mismatch (a replaced directory with a new
    device/inode, a path that became a symlink, or a directory that no longer
    exists) makes this false; discovery then downgrades to STALE_BINDING rather
    than promising a runtime that ``resolve_runtime`` will immediately reject.

    Read-only: ``_bound_directory`` only resolves paths and lstats them.  A
    malformed path or binding payload would also make this false, but on an entry
    the classifier already ruled ACTIVE those were signed by the digest and built
    well-formed by ``_canonical_entry``, so the only reachable falsity here is a
    genuine on-disk identity change -- STALE_BINDING never masks a corrupt entry
    (that is INTEGRITY_FAILED/SHAPE_MISMATCH, decided earlier and ranked above).
    """

    try:
        for field in ("agent_dir", "workspace"):
            _bound_directory(entry.get(field), entry.get(f"{field}_binding"), field=field)
    except PuffoV0RegistryError:
        return False
    return True


def _state_releases_directory(state: PuffoV0RuntimeState) -> bool:
    """Whether an entry in this state has released the directory it recorded.

    The single roster both occupancy checks consult, so the set of states that
    "no longer hold their directory" cannot diverge between them: the provision
    guard skips such an entry (it never blocks a new binding) and discovery skips
    it when its binding is unreadable (its directory is genuinely reusable, so
    reporting `available` is truthful). Only a REVOKED entry releases -- every
    other state still holds its directory and must block / fail closed. Route
    both ``_active_binding_conflicts`` and ``_discovery_records`` through this so a
    state added later cannot be released on one side but not the other.
    """

    return state is PuffoV0RuntimeState.REVOKED


def _binding_conflict_message(
    field: str, runtime_id: str, state: PuffoV0RuntimeState, stored_path: object
) -> str:
    """Explain a provisioning conflict by the recovery its state actually needs.

    Every message names (a) the runtime_id that causes the rejection, (b) the path
    the conflicting entry recorded, and (c) the operation that actually helps.  The
    truth of the tampered/malformed cases reduces to one rule -- ``revoke_runtime``
    refuses an entry that does not classify as a live or policy-drifted runtime --
    so no message makes a per-subtype capability claim ("cannot be cleared by
    revoke") that a later subtype could falsify.  It names the escalation instead.
    """

    location = f" (recorded at {stored_path!r})" if isinstance(stored_path, str) else ""
    if state is PuffoV0RuntimeState.POLICY_VERSION_MISMATCH:
        return (
            f"{field} is bound to runtime {runtime_id!r}{location} provisioned under "
            "a different policy version; revoke it before re-provisioning"
        )
    if state is PuffoV0RuntimeState.INTEGRITY_FAILED:
        return (
            f"{field} is bound to runtime {runtime_id!r}{location} whose registry entry "
            "failed its integrity check; revoke is refused for it (revoking would "
            "re-sign the tampered entry and erase the integrity signal) -- escalate "
            "for review and repair it out of band"
        )
    if state is PuffoV0RuntimeState.SHAPE_MISMATCH:
        return (
            f"{field} is bound to runtime {runtime_id!r}{location} whose registry entry "
            "does not match the puffo-v0 profile; revoke is refused for it -- escalate "
            "for review and repair it out of band"
        )
    return (
        f"{field} is already bound to active runtime {runtime_id!r}{location}; "
        "revoke it before re-provisioning"
    )


def _unreadable_binding_conflict_message(
    field: str, runtime_id: str, state: PuffoV0RuntimeState
) -> str:
    """Explain a fail-closed provisioning block on an entry whose binding is unreadable."""

    return (
        f"cannot rule out a {field} conflict: runtime {runtime_id!r} has an unreadable "
        f"binding ({state.value}); escalate for review and repair it out of band"
    )


# A conflict on an entry whose stored binding cannot even be parsed outranks every
# readable occupant: "cannot rule out that it holds the target" is stricter than any
# known state.  It sorts below _state_rank's smallest index (0), so it always wins.
_UNREADABLE_CONFLICT_RANK = -1


@dataclass(frozen=True, slots=True)
class _BindingConflict:
    """One reason a provision is blocked, ranked so selection is order-independent."""

    sort_key: tuple[int, int, str]
    message: str


def _active_binding_conflicts(
    runtimes: dict[str, Any],
    *,
    agent_dir_binding: DirectoryBinding,
    workspace_binding: DirectoryBinding,
    revoked_runtime_ids: frozenset[str],
) -> None:
    """Require the Phase A active binding to remain one-to-one.

    The accept/reject set is by physical identity: any non-released entry occupying
    the target ``agent_dir`` or ``workspace`` device/inode blocks provisioning, as
    does any non-released entry whose binding cannot be parsed (fail closed -- a
    skip would be the allow direction).  The criterion is device/inode, never the
    path string, so a directory renamed away behind a symlink cannot be bound twice.

    Conflicts are AGGREGATED and then the most-constraining one is chosen by the
    SAME precedence discovery uses, rather than raising on the first entry the
    iteration happens to reach.  Registry insertion order therefore cannot decide
    which of several occupants of one directory is named: a directory held by both
    an integrity-failed and an active entry always reports the integrity failure
    (whose recovery is "escalate"), never the active one (whose recovery is
    "revoke") -- so the guard and discover never hand the caller contradictory
    guidance for the same directory.  Only a revoked entry (which released its
    directory) is skipped, shared with discovery via ``_state_releases_directory``.
    """

    targets = (
        ("agent_dir", agent_dir_binding, 0),
        ("workspace", workspace_binding, 1),
    )
    conflicts: list[_BindingConflict] = []
    for existing_runtime_id, entry in runtimes.items():
        if not isinstance(existing_runtime_id, str) or not isinstance(entry, dict):
            raise PuffoV0RegistryError("runtime registry entry has an invalid shape")
        state = _classify_registry_entry(
            existing_runtime_id, entry, revoked_runtime_ids=revoked_runtime_ids
        )
        if _state_releases_directory(state):
            continue
        for field, target, field_priority in targets:
            try:
                stored = _parse_binding(entry.get(f"{field}_binding"))
            except PuffoV0RegistryError:
                conflicts.append(
                    _BindingConflict(
                        sort_key=(_UNREADABLE_CONFLICT_RANK, field_priority, existing_runtime_id),
                        message=_unreadable_binding_conflict_message(
                            field, existing_runtime_id, state
                        ),
                    )
                )
                continue
            if (stored.device, stored.inode) == (target.device, target.inode):
                conflicts.append(
                    _BindingConflict(
                        sort_key=(_state_rank(state), field_priority, existing_runtime_id),
                        message=_binding_conflict_message(
                            field, existing_runtime_id, state, entry.get(field)
                        ),
                    )
                )
    if conflicts:
        raise PuffoV0RegistryError(min(conflicts, key=lambda conflict: conflict.sort_key).message)


def provision_runtime(
    runtime_id: str,
    agent_dir: Path,
    workspace: Path,
    *,
    registry_path: Path | None = None,
) -> PuffoV0Runtime:
    """Bind one existing persistent agent identity to a local runtime id.

    This is an operator control-plane operation.  The ACP data-plane accepts
    only the resulting id and never accepts either filesystem path.
    """

    runtime_id = _valid_runtime_id(runtime_id)
    agent_dir = _canonical_directory(agent_dir, field="agent_dir")
    workspace = _canonical_directory(workspace, field="workspace")
    agent_dir_binding = _directory_binding(agent_dir, field="agent_dir")
    workspace_binding = _directory_binding(workspace, field="workspace")
    if not (agent_dir / "init.json").is_file():
        raise PuffoV0RegistryError("agent_dir must contain init.json")
    path = registry_path or default_registry_path()
    with _registry_mutation_lock(path):
        if path.exists():
            revoked_runtime_ids = _read_revoked_runtime_ids(path)
            registry = _read_registry(path)
        else:
            _initialize_revocation_log(path)
            revoked_runtime_ids = frozenset()
            registry = {
                "revocation_log": REVOCATION_LOG_REQUIRED,
                "version": REGISTRY_VERSION,
                "runtimes": {},
            }
        if runtime_id in revoked_runtime_ids:
            raise PuffoV0RegistryError("runtime_id is revoked and cannot be provisioned again")
        runtimes = registry["runtimes"]
        if runtime_id in runtimes:
            raise PuffoV0RegistryError("runtime_id is already provisioned")
        _active_binding_conflicts(
            runtimes,
            agent_dir_binding=agent_dir_binding,
            workspace_binding=workspace_binding,
            revoked_runtime_ids=revoked_runtime_ids,
        )
        entry = _canonical_entry(
            runtime_id,
            agent_dir,
            workspace,
            agent_dir_binding,
            workspace_binding,
        )
        entry["entry_digest"] = _digest(entry)
        runtimes[runtime_id] = entry
        _write_registry(path, registry)
    return PuffoV0Runtime(
        runtime_id,
        agent_dir,
        workspace,
        entry["entry_digest"],
        agent_dir_binding,
        workspace_binding,
        RUNTIME_POLICY.policy_version,
    )


def _revoke_refusal_message(runtime_id: str, state: PuffoV0RuntimeState) -> str:
    """Explain why revoke refuses an entry, naming an action that actually helps.

    revoke is a state change, not a repair.  For a tampered or malformed entry the
    only honest guidance is out-of-band repair -- naming revoke as the recovery
    would be a dead end (and re-signing would destroy the very signal that must be
    preserved).  The truth of each message reduces to this one admission rule, so
    no message makes a per-subtype claim that a later subtype could falsify.
    """

    if state is PuffoV0RuntimeState.INTEGRITY_FAILED:
        return (
            f"runtime {runtime_id!r} failed its integrity check; revoke is refused "
            "so the tampered record is not re-signed (which would erase the "
            "integrity signal) -- escalate for review and repair it out of band"
        )
    if state is PuffoV0RuntimeState.SHAPE_MISMATCH:
        return (
            f"runtime {runtime_id!r} does not match the puffo-v0 profile; revoke is "
            "refused for a malformed entry -- escalate for review and repair it out "
            "of band"
        )
    if state is PuffoV0RuntimeState.REVOKED:
        return f"runtime {runtime_id!r} is already revoked"
    return (
        f"runtime {runtime_id!r} is not in a revocable state ({state.value}); "
        "escalate for review"
    )


def revoke_runtime(runtime_id: str, *, registry_path: Path | None = None) -> None:
    """Mark a provisioned profile identity unavailable for future ACP spawns."""

    runtime_id = _valid_runtime_id(runtime_id)
    path = registry_path or default_registry_path()
    with _registry_mutation_lock(path):
        registry = _read_registry(path)
        entry = registry["runtimes"].get(runtime_id)
        if not isinstance(entry, dict):
            raise PuffoV0RegistryError("runtime_id is not provisioned")
        revoked_runtime_ids = _read_revoked_runtime_ids(path)
        # Admission BEFORE the first persistence.  revoke may only act on an entry
        # that already classifies as a live or policy-drifted runtime.  Two reasons
        # it must refuse everything else here, before writing anything:
        #   * revoke re-signs the entry (and re-adds a dropped ``entry_digest`` or
        #     ``status``), so on a tampered/malformed entry it would erase the
        #     tamper signal or launder a shape defect into a released state.
        #   * the tombstone is appended first and a tombstone ALONE releases the
        #     directory (the classifier honours the revocation record), so any check
        #     placed after the append would release identity even on a "failed"
        #     call -- irreversibly, because the log is append-only.
        state = _classify_registry_entry(
            runtime_id, entry, revoked_runtime_ids=revoked_runtime_ids
        )
        if state not in (
            PuffoV0RuntimeState.ACTIVE,
            PuffoV0RuntimeState.POLICY_VERSION_MISMATCH,
        ):
            raise PuffoV0RegistryError(_revoke_refusal_message(runtime_id, state))
        if runtime_id not in revoked_runtime_ids:
            _append_revocation_tombstone(path, runtime_id)
        entry["status"] = "revoked"
        canonical = {key: value for key, value in entry.items() if key != "entry_digest"}
        entry["entry_digest"] = _digest(canonical)
        _write_registry(path, registry)


@dataclass(frozen=True, slots=True)
class _DiscoveryRecord:
    """One registry entry's classification as attributed to its agent_dir."""

    state: PuffoV0RuntimeState
    runtime_id: str | None
    workspace: Path | None


@dataclass(frozen=True, slots=True)
class _DiscoveryIndex:
    """The two lookups discovery needs, both built read-only from the registry.

    ``by_identity`` attributes an entry to the physical directory it holds, keyed
    by device/inode -- the authoritative classification for a walked directory.
    ``formerly_bound`` maps a stored canonical agent_dir path to the runtime_id
    that recorded it, for the option-C advisory hint: it is consulted ONLY for a
    directory that came back unbound, where the fact that identity attribution
    missed already proves the recorded entry no longer holds this path.
    """

    by_identity: dict[tuple[int, int], _DiscoveryRecord]
    formerly_bound: dict[str, str]


def _state_rank(state: PuffoV0RuntimeState) -> int:
    return _DISCOVERY_STATE_PRECEDENCE.index(state)


def _authentic_workspace(entry: object, state: PuffoV0RuntimeState) -> Path | None:
    """Return the entry's workspace only when it names a LIVE, signed binding.

    A workspace is meaningful only for a directory that is currently held.  The
    two states that report one differ in how liveness was established:

    * ACTIVE reached this point only after the ``_binding_matches`` check in
      discovery already proved both the agent_dir and the workspace still resolve
      to their provisioned identity (otherwise it would have been downgraded to
      STALE_BINDING), so its workspace is known live.
    * POLICY_VERSION_MISMATCH runs no such downgrade, so its workspace liveness is
      unverified here.  Verify it directly and return ``None`` if the workspace no
      longer resolves to its provisioned identity -- otherwise discovery would
      report a drifted entry's workspace path that no longer exists, which breaks
      the promise that a reported workspace names a live binding.

    For every other (revoked, tampered, foreign) state there is no binding to
    report.
    """

    if not isinstance(entry, dict):
        return None
    if state is PuffoV0RuntimeState.ACTIVE:
        workspace_value = entry.get("workspace")
        if isinstance(workspace_value, str):
            return Path(workspace_value)
        return None
    if state is PuffoV0RuntimeState.POLICY_VERSION_MISMATCH:
        try:
            resolved, _binding = _bound_directory(
                entry.get("workspace"), entry.get("workspace_binding"), field="workspace"
            )
        except PuffoV0RegistryError:
            return None
        return resolved
    return None


def _entry_identity_key(entry: object) -> tuple[int, int]:
    """Return the stored ``(device, inode)`` an entry names, for attribution.

    Discovery keys records by this and looks them up by the walked directory's
    live device/inode (not by path string).  That closes two holes a path key
    cannot: a stored path whose parent became a symlink, and a directory renamed
    away with a symlink left behind -- both keep the physical device/inode, so
    identity still attributes the entry to the directory that actually holds it.

    On an authentic entry the device/inode come from the digest-signed
    ``agent_dir_binding``.  On a corrupt one the binding may be missing or
    malformed; this raises then, and the caller must fail closed rather than drop
    the entry, because a dropped entry's directory would be reported ``available``
    -- the dangerous direction.
    """

    if not isinstance(entry, dict):
        raise PuffoV0RegistryError("runtime registry entry has an invalid shape")
    binding = _parse_binding(entry.get("agent_dir_binding"))
    return (binding.device, binding.inode)


def _record_formerly_bound(
    formerly: dict[str, tuple[PuffoV0RuntimeState, str]],
    entry: object,
    *,
    state: PuffoV0RuntimeState,
    runtime_id: str,
) -> None:
    """Note that ``runtime_id`` recorded ``entry``'s stored path, for the hint.

    Sourced from the PATH, not the state: any non-revoked entry with a readable
    stored agent_dir is a candidate to have its path reused.  We do NOT pre-filter
    to only the STALE downgrade -- a POLICY_VERSION_MISMATCH or SHAPE_MISMATCH
    entry whose directory was replaced at the same path never gets that downgrade,
    yet its path reuse is exactly what the hint must flag.  The "is it still here"
    test is deferred to discovery: the hint is read only for a directory that came
    back unbound, where identity attribution having missed already proves this
    entry no longer holds the path.  A revoked entry released its directory on
    purpose, so reusing its path is expected and needs no sign.  When two entries
    name one path, the most-constraining state supplies the hint (same rule as the
    identity-keyed collision), decided here rather than by dict order.
    """

    if _state_releases_directory(state) or not isinstance(entry, dict):
        return
    stored_path = entry.get("agent_dir")
    if not isinstance(stored_path, str):
        return
    existing = formerly.get(stored_path)
    if existing is None or _state_rank(state) < _state_rank(existing[0]):
        formerly[stored_path] = (state, runtime_id)


def _discovery_records(path: Path) -> _DiscoveryIndex:
    """Classify every registry entry, keyed by the device/inode it binds.

    Reads only -- never creates a lock, initializes a registry, or rewrites
    permissions.  When several entries name the same directory, the most
    constraining state wins (``_DISCOVERY_STATE_PRECEDENCE``) so discovery never
    reports a directory as usable while a corrupt or drifted entry for it is
    outstanding.  The historical guard -- two *active* runtimes may not share a
    directory -- is preserved as hard corruption.  Also returns the
    ``formerly_bound`` path->runtime_id map for the advisory reuse hint.
    """

    try:
        path.lstat()
    except FileNotFoundError:
        # A registry that is absent yet still has its mandatory revocation log is a
        # broken control-plane state: provision refuses to re-initialize over an
        # orphaned tombstone (O_EXCL), so a directory here is NOT provisionable.
        # Discovery must not report it as `available`; fail closed with a distinct
        # message keyed to that recovery (restore the registry or remove the log).
        try:
            _revocation_log_path(path).lstat()
        except FileNotFoundError:
            return _DiscoveryIndex(by_identity={}, formerly_bound={})
        except OSError as exc:
            raise PuffoV0RegistryError(
                "puffo-v0 runtime registry is unavailable or invalid"
            ) from exc
        raise PuffoV0RegistryError(
            "puffo-v0 revocation log exists without its runtime registry"
        )
    except OSError as exc:
        raise PuffoV0RegistryError("puffo-v0 runtime registry is unavailable or invalid") from exc

    revoked_runtime_ids = _read_revoked_runtime_ids_read_only(path)
    registry = _read_registry_read_only(path)
    records: dict[tuple[int, int], _DiscoveryRecord] = {}
    formerly: dict[str, tuple[PuffoV0RuntimeState, str]] = {}
    for runtime_id, entry in registry["runtimes"].items():
        if not isinstance(runtime_id, str):
            # JSON object keys are strings, so this is structurally unreachable
            # from a parsed registry; fail closed rather than silently drop it,
            # because a dropped entry's directory reads as `available`.
            raise PuffoV0RegistryError("puffo-v0 runtime registry has a non-string runtime id")
        state = _classify_registry_entry(
            runtime_id, entry, revoked_runtime_ids=revoked_runtime_ids
        )
        if (
            state is PuffoV0RuntimeState.ACTIVE
            and isinstance(entry, dict)
            and not _binding_matches(entry)
        ):
            # Authentic and active in the registry, but the on-disk directory it
            # names has been replaced/moved (different device/inode, now a symlink,
            # or gone). resolve_runtime would reject it, so `bound` would be a lie.
            state = PuffoV0RuntimeState.STALE_BINDING
        # Note this entry's stored path for the reuse hint; whether it surfaces is
        # decided at lookup time (only for a directory that comes back unbound). The
        # helper skips revoked and path-less entries.
        _record_formerly_bound(formerly, entry, state=state, runtime_id=runtime_id)
        try:
            identity = _entry_identity_key(entry)
            # Parity with the provision guard's field set. The guard parses BOTH
            # stored bindings (agent_dir AND workspace) before any identity
            # comparison and fails closed on either, so a single unreadable
            # workspace_binding makes it reject EVERY provision -- the parse
            # precedes the target comparison. Reading only agent_dir here would
            # report other directories `available` while that entry silently blocks
            # all provisioning, so discover must fail closed on the same both-field
            # set. ``_entry_identity_key`` covers agent_dir; this covers workspace.
            _parse_binding(entry.get("workspace_binding"))
        except PuffoV0RegistryError as exc:
            # A non-revoked entry with no readable device/inode cannot be attributed
            # to a walked directory, and dropping it would let a directory it holds
            # read `available`, so fail closed. A REVOKED entry cannot reach here: a
            # malformed binding payload classifies SHAPE_MISMATCH ahead of the revoked
            # gate (see ``_classify_registry_entry``), so a REVOKED verdict implies
            # both bindings parse -- discover's fail-closed set therefore still equals
            # the guard's block set without a released-state special case here. The
            # message names the action that actually helps: an unreadable binding is
            # repaired out of band, not by resolve (which rejects it) or revoke (which
            # is refused for it).
            raise PuffoV0RegistryError(
                f"puffo-v0 runtime registry entry {runtime_id!r} has an unreadable "
                "directory binding; escalate for review and repair it out of band"
            ) from exc
        record = _DiscoveryRecord(
            state=state,
            runtime_id=runtime_id,
            workspace=_authentic_workspace(entry, state),
        )
        existing = records.get(identity)
        if existing is None:
            records[identity] = record
            continue
        if existing.state is PuffoV0RuntimeState.ACTIVE and state is PuffoV0RuntimeState.ACTIVE:
            raise PuffoV0RegistryError("multiple active runtimes bind the same agent_dir")
        if _state_rank(state) < _state_rank(existing.state):
            records[identity] = record
    return _DiscoveryIndex(
        by_identity=records,
        formerly_bound={stored: rid for stored, (_state, rid) in formerly.items()},
    )


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def discover_runtimes(
    root: Path,
    *,
    registry_path: Path | None = None,
) -> list[PuffoV0DiscoveryCandidate]:
    """List initialized agents below one user-selected root without side effects.

    Directory symlinks are never followed.  The registry is read directly rather
    than through its mutation/security-hardening helpers so discovery cannot
    create a lock, initialize a registry, or rewrite permissions.
    """

    _require_posix_registry_security()
    canonical_root = _canonical_directory(root, field="root")
    index = _discovery_records(registry_path or default_registry_path())
    candidates: list[PuffoV0DiscoveryCandidate] = []

    def _ignore_walk_error(_error: OSError) -> None:
        return None

    for raw_current, directory_names, _file_names in os.walk(
        canonical_root,
        topdown=True,
        followlinks=False,
        onerror=_ignore_walk_error,
    ):
        current = Path(raw_current)
        directory_names[:] = [
            name
            for name in directory_names
            if not name.startswith(".") and not (current / name).is_symlink()
        ]
        try:
            agent_dir = current.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not _is_within_root(agent_dir, canonical_root):
            continue
        try:
            initialized = (agent_dir / "init.json").is_file()
        except OSError:
            continue
        if not initialized:
            continue
        try:
            live_binding = _directory_binding(agent_dir, field="agent_dir")
        except PuffoV0RegistryError:
            # Cannot read this directory's identity to look it up; skip listing it
            # rather than report an identity we could not verify. This is the safe
            # direction (it never claims an occupied directory is `available`).
            continue
        record = index.by_identity.get((live_binding.device, live_binding.inode))
        if record is None:
            # Unbound now. If a registry entry recorded this exact path, its
            # identity is no longer here (or attribution would have hit above), so
            # hang the option-C reuse sign -- the directory stays `available` and
            # provisionable, but the caller learns the path was previously bound.
            candidates.append(
                PuffoV0DiscoveryCandidate(
                    agent_dir=agent_dir,
                    workspace=None,
                    display_name=agent_dir.name,
                    runtime_id=None,
                    state=PuffoV0RuntimeState.PROVISIONABLE,
                    formerly_bound_runtime_id=index.formerly_bound.get(str(agent_dir)),
                )
            )
        else:
            candidates.append(
                PuffoV0DiscoveryCandidate(
                    agent_dir=agent_dir,
                    workspace=record.workspace,
                    display_name=agent_dir.name,
                    runtime_id=record.runtime_id,
                    state=record.state,
                )
            )
    return sorted(candidates, key=lambda candidate: str(candidate.agent_dir))


# Resolution rejection messages, one per non-active state.  Distinct strings are
# required, not cosmetic: the caller's recovery differs per state (see the
# discover status table), and the invariant forbids two different recoveries
# sharing one representation.
#
# STALE_BINDING is discover-only: ``_classify_registry_entry`` never returns it
# (it is synthesized in ``_discovery_records`` from a live filesystem check that
# the pure classifier deliberately does not run), so ``resolve_runtime`` cannot
# reach this entry -- resolve enforces the same binding directly via
# ``_bound_directory`` and raises its richer per-field message.  The entry is
# kept here defensively and to satisfy the completeness guard, not because
# resolve emits it.
_RESOLVE_STATE_MESSAGES: dict[PuffoV0RuntimeState, str] = {
    PuffoV0RuntimeState.REVOKED: (
        "runtime id has been revoked; provision a new runtime id"
    ),
    PuffoV0RuntimeState.POLICY_VERSION_MISMATCH: (
        "runtime was provisioned under a different policy version; "
        "revoke it and re-provision the same directory under a new id"
    ),
    PuffoV0RuntimeState.STALE_BINDING: (
        "runtime's provisioned directory identity changed on disk; revoke it and "
        "re-provision after verifying the directory"
    ),
    PuffoV0RuntimeState.INTEGRITY_FAILED: (
        "runtime registry entry failed its integrity check; do not reuse it, and do "
        "not revoke it (revoke is refused for a tampered entry so its integrity "
        "signal is preserved) -- escalate for review and repair it out of band"
    ),
    PuffoV0RuntimeState.SHAPE_MISMATCH: (
        "runtime registry entry does not match the puffo-v0 profile; revoke is "
        "refused for it -- escalate for review and repair it out of band"
    ),
}


def resolve_runtime(
    runtime_id: str, *, registry_path: Path | None = None
) -> PuffoV0Runtime:
    """Resolve one active runtime id into an immutable local spawn specification."""

    runtime_id = _valid_runtime_id(runtime_id)
    path = registry_path or default_registry_path()
    _secure_registry_directory(path.parent)
    revoked_runtime_ids = _read_revoked_runtime_ids(path)
    registry = _read_registry(path)
    entry = registry["runtimes"].get(runtime_id)
    if not isinstance(entry, dict):
        raise PuffoV0RegistryError("runtime_id is not provisioned")
    state = _classify_registry_entry(
        runtime_id, entry, revoked_runtime_ids=revoked_runtime_ids
    )
    if state is not PuffoV0RuntimeState.ACTIVE:
        raise PuffoV0RegistryError(_RESOLVE_STATE_MESSAGES[state])
    agent_dir, agent_dir_binding = _bound_directory(
        entry.get("agent_dir"), entry.get("agent_dir_binding"), field="agent_dir"
    )
    workspace, workspace_binding = _bound_directory(
        entry.get("workspace"), entry.get("workspace_binding"), field="workspace"
    )
    if not (agent_dir / "init.json").is_file():
        raise PuffoV0RegistryError("registered agent identity is no longer initialized")
    return PuffoV0Runtime(
        runtime_id,
        agent_dir,
        workspace,
        entry["entry_digest"],
        agent_dir_binding,
        workspace_binding,
        RUNTIME_POLICY.policy_version,
    )


__all__ = [
    "DirectoryBinding",
    "PROFILE_NAME",
    "PuffoV0DiscoveryCandidate",
    "PuffoV0RegistryError",
    "PuffoV0Runtime",
    "PuffoV0RuntimePolicy",
    "PuffoV0RuntimeState",
    "RUNTIME_POLICY",
    "default_registry_path",
    "discover_runtimes",
    "provision_runtime",
    "resolve_runtime",
    "revoke_runtime",
]
