"""Core-owned declared host-plugin contract for official model-facing tools.

This is the kernel's *shape* for one official tool plugin: a static
:class:`ToolPluginDeclaration` constructed at import time, a least-privilege
:class:`ToolPluginHost` facade built from exactly the host ports a declaration
names in ``requires``, a kernel-owned reserved list of official plugin names,
and a fail-fast registrar that refuses a duplicate or unreserved name **before**
it binds anything and before any tool is mounted.

Three deliberate absences define this module as much as its exports:

- **No family knowledge.** The kernel never imports ``lingtai.tools`` and never
  learns what an official family *does*. It owns the declaration type, the
  ports, the reserved-name list, and the registration order; each family owns
  its own declaration, and ``src/lingtai/agent.py`` remains the Composition
  Root that wires the two together.
- **No discovery.** There is no filesystem scan, entry-point lookup, manifest
  compiler, or plugin-admission engine here. ``OFFICIAL_TOOL_PLUGIN_NAMES``
  below is a hand-edited, auditable literal, and a declaration reaches this
  module only because some caller passed it in.
- **No whole-``Agent`` argument.** :meth:`ToolPluginDeclaration.bind` receives a
  :class:`ToolPluginHost`, never the live ``Agent``. A port the declaration did
  not name is not reachable through the facade, and ``tool_mount`` is never
  grantable at all — an official plugin cannot mount itself.

See the sibling ``CONTRACT.md`` for the normative rules and ``ANATOMY.md`` for
where the production adapter and declared families live.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

__all__ = [
    "MANUAL_ACTION",
    "GRANTABLE_HOST_PORTS",
    "OFFICIAL_TOOL_PLUGIN_NAMES",
    "ToolPluginError",
    "ToolPluginDeclarationError",
    "UnreservedToolPluginNameError",
    "DuplicateToolPluginNameError",
    "OfficialToolNameCollisionError",
    "HostPortError",
    "WorkdirPort",
    "PromptSectionPort",
    "FileGrepMatch",
    "FileTraversalStats",
    "FileIOPort",
    "ContextRuntimePort",
    "AvatarParentPort",
    "DaemonRuntimePort",
    "PluginCatalogState",
    "PluginCatalogPort",
    "PsycheSettingsSnapshotPort",
    "PsycheSettingsPort",
    "NotificationStatePort",
    "NotificationPort",
    "ConfigurationPort",
    "ActiveProviderPort",
    "ProviderIdentityPort",
    "SoulRuntimePort",
    "SystemRuntimePort",
    "IdentityPort",
    "ShutdownPort",
    "TaskCardLifecyclePort",
    "TaskCardNotificationsPort",
    "ToolMountPort",
    "ToolPluginHost",
    "BoundToolPlugin",
    "ToolPluginDeclaration",
    "register_official_tool_plugins",
]


#: The reserved action name every official family appends exactly once, from
#: its own manual. Kept as the kernel's single spelling of the reserved word so
#: the declaration can refuse an operational action that tries to claim it.
#: The reserved-action *rule* itself is normative in
#: ``src/lingtai/tools/CONTRACT.md`` ``### Dispatch and actions``; this constant
#: only lets the declaration enforce it before an Agent exists.
MANUAL_ACTION = "manual"

# Optional read-only discovery action. Its implementation and public row type
# belong to ``lingtai.tools.tool_family``; the kernel declaration needs only
# this reserved spelling to advertise an opted-in family.
_SETTINGS_ACTION = "settings"


def _settings_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


#: Every host port an official declaration may name in ``requires``.
#:
#: Earned, not enumerated: each name below is consumed by a real vertical
#: slice this component ships with (``mcp``, ``avatar``, ``context``, ``daemon``,
#: ``email``, ``file``, ``plugin``, ``psyche``, ``notification``, ``shell``,
#: ``soul``, ``system``, ``task_card``, ``vision``, or ``web``). Plugin
#: consumes only the read-only ``plugin_catalog`` projection; Psyche consumes
#: only its last-applied Pad and prompt-owner configuration through
#: ``psyche_settings``; File consumes ``workdir``/``file_io`` plus its
#: factory-applied bounded
#: ``configuration`` snapshot; Shell consumes
#: ``workdir`` plus its explicit setup ``configuration`` and durable
#: ``notifications`` ports; System consumes its ``system_runtime`` lifecycle
#: vocabulary plus the durable naming ``identity`` port; Task Card consumes
#: ``workdir`` plus its one-predicate ``shutdown`` observation, its
#: current-Agent ``task_card_lifecycle`` manager slot, and the closed
#: operation-native ``task_card_notifications`` port; Vision consumes
#: ``workdir`` plus its read-through ``active_provider`` identity and the same
#: setup-selected ``configuration`` port Shell earned; Web consumes ``workdir``
#: plus its Web-owned typed ``web_runtime`` composition value (browser
#: transport, immutable engine specs, default provenance — granted by its own
#: setup, like Email's ``email_runtime``) and the narrow read-only
#: ``provider_identity`` label that gates its explicit Anthropic/Gemini opt-in.
#: Root ``CONTRACT.md`` rules 10-11
#: forbid a speculative port taxonomy, so a
#: later family adds the port it actually needs together with its own slice.
#:
#: ``tool_mount`` is deliberately absent and MUST stay absent: mounting is the
#: host's own act, performed by :func:`register_official_tool_plugins` after the
#: name checks pass. A declaration that could mount could self-register.
GRANTABLE_HOST_PORTS: tuple[str, ...] = (
    "workdir",
    "prompt_section",
    "avatar_parent",
    "context_runtime",
    "daemon_runtime",
    "email_runtime",
    "file_io",
    "plugin_catalog",
    "psyche_settings",
    "notification_state",
    "notifications",
    "configuration",
    "soul_runtime",
    "system_runtime",
    "identity",
    "shutdown",
    "task_card_lifecycle",
    "task_card_notifications",
    "active_provider",
    "web_runtime",
    "provider_identity",
)


#: The kernel-owned reserved list of official plugin names.
#:
#: This is the auditable static registry of the official model-facing tool
#: namespace. A name here may be claimed by exactly one live declaration; a
#: declaration whose name is absent here is not official and is refused. Adding
#: a name is a reviewed kernel change, which is the point: it is a list, not a
#: discovery mechanism, and it holds names only — never a module path, an
#: import, or any knowledge of what the family does.
OFFICIAL_TOOL_PLUGIN_NAMES: tuple[str, ...] = (
    "mcp", "avatar", "context", "daemon", "email", "file", "plugin", "psyche",
    "notification", "shell", "soul", "system", "task_card", "vision", "web",
)


# Opaque capability used only by the production host adapter's private
# authorized-mount route. Generic ``Agent.add_tool`` never receives this token;
# keeping it kernel-owned prevents the mount boundary from inferring authority
# from a caller-supplied name or declaration.
_OFFICIAL_MOUNT_TOKEN = object()
# Separate issuer capability for registrar-created mount transactions.  Python
# trusted-in-process code can inspect module globals; this is provenance for the
# public/declared and normal extension paths, not an absolute security boundary.
_OFFICIAL_MOUNT_ISSUER = object()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ToolPluginError(Exception):
    """Base class for every declared host-plugin defect.

    Deliberately **not** a ``ValueError``. The Composition Root's capability
    boot loop (``src/lingtai/agent.py``, both ``__init__`` and
    ``_setup_from_init``) wraps each ``_setup_capability`` call in
    ``except (ValueError, ImportError, TypeError)`` and downgrades what it
    catches to one ``capability_skipped`` log line. A reserved-name conflict,
    an unreserved official name, a missing host port, or a declaration that
    disagrees with what it ships would then be *silently* absorbed into a boot
    that comes up with the official tool missing — the exact failure mode this
    component exists to prevent. Skipping a capability is signalled by
    returning ``CAPABILITY_UNAVAILABLE`` (``src/lingtai/tools/registry.py``
    ``setup_capability``), never by raising from here, so these errors
    propagate past that guard and fail the boot loudly.
    """


class ToolPluginDeclarationError(ToolPluginError):
    """A declaration is malformed: bad name, actions, schemas, or requires."""


class UnreservedToolPluginNameError(ToolPluginError):
    """A declaration claims a name the kernel has not reserved as official."""


class DuplicateToolPluginNameError(ToolPluginError):
    """A second, different declaration claims an already-claimed official name."""


class OfficialToolNameCollisionError(ToolPluginError):
    """A generic or external mount attempted to take an official tool name."""


class HostPortError(ToolPluginError):
    """A required host port was not granted, or a granted port is not grantable."""


# ---------------------------------------------------------------------------
# Host ports — capability-native, one narrow promise each
# ---------------------------------------------------------------------------

class WorkdirPort(Protocol):
    """Read-only access to this agent's working directory.

    The whole capability is "where this agent's files live". It grants no
    read, write, listing, or lease operation: the plugin composes its own paths
    below :attr:`path` and uses ordinary filesystem calls, exactly as it did
    when it reached through the Agent.
    """

    @property
    def path(self) -> Path:
        """The agent working directory."""


class PsycheSettingsSnapshotPort(Protocol):
    """Structural view of Psyche's last completely applied owner inputs."""

    pad: str
    pad_file: str | None
    base_prompt: str
    base_prompt_file: str | None
    covenant: str
    covenant_file: str | None
    comment: str
    comment_file: str | None


class PsycheSettingsPort(Protocol):
    """Read Psyche's last completely applied prompt-owner configuration.

    The immutable structural snapshot contains Pad plus the three configurable
    prompt pairs. It grants no prompt mutation, reconstruction, settings write,
    owner-source read, or Agent access.
    """

    def read_snapshot(self) -> PsycheSettingsSnapshotPort:
        """Return the current applied Psyche owner-input snapshot."""


class PromptSectionPort(Protocol):
    """Write this plugin's own protected system-prompt section.

    Deliberately not ``update_system_prompt(section, body, protected=...)``:
    the granted port is bound to the declaring plugin's name at grant time, so
    an official plugin can rewrite its own section and no other, and cannot
    write an unprotected one.
    """

    def write_protected_section(self, body: str) -> None:
        """Replace this plugin's protected prompt section with *body*."""


class FileGrepMatch(Protocol):
    """The three immutable match fields File consumes from bounded grep."""

    path: str
    line_number: int
    line: str


class FileTraversalStats(Protocol):
    """The bounded traversal facts File surfaces for partial glob/grep results."""

    visited: int
    elapsed_ms: int
    truncated_reason: str | None
    files_skipped_size: int
    files_skipped_binary: int
    dirs_pruned: int


class FileIOPort(Protocol):
    """The File family's narrow runtime file-operation capability.

    This is deliberately not ``Agent._file_io`` exposed as an attribute and is
    not a generic filesystem or dispatch port. It is the exact vocabulary the
    official ``file`` family consumes: UTF-8 text read/write, bounded glob/grep,
    the latest traversal facts those searches report, and the active result-size
    ceiling used by its paged reader. Path rooting remains the separate
    :class:`WorkdirPort` capability.
    """

    def read(self, path: str) -> str:
        """Read one UTF-8 text file."""

    def write(self, path: str, content: str) -> None:
        """Create or overwrite one UTF-8 text file."""

    def glob(self, pattern: str, root: str | None = None) -> list[str]:
        """Return sorted paths matching *pattern* below *root*."""

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
        *,
        glob_filter: str | None = None,
    ) -> list[FileGrepMatch]:
        """Return concrete text matches from the bounded search service."""

    @property
    def last_traversal(self) -> FileTraversalStats | None:
        """The latest glob/grep traversal facts, if the service reports them."""

    @property
    def max_result_chars(self) -> int | None:
        """The live executor result limit, if it exposes a positive integer."""


class ContextRuntimePort(Protocol):
    """Run the Context family's live lifecycle operations.

    This is intentionally a capability-shaped operation port rather than the
    Agent or a bag of its private state. ``molt`` preserves the live chat
    selection/wipe/replay transaction; ``summarize`` preserves record-only
    history compaction; ``rebuild`` preserves full prompt composition before
    summary application and provider replay. Context receives no other live
    Agent authority through this port.
    """

    def molt(self, args: dict) -> dict:
        """Run one agent-initiated Context molt with validated action arguments."""

    def summarize(self, args: dict) -> dict:
        """Record Context summaries without reconstructing provider context."""

    def rebuild(self, args: dict) -> dict:
        """Reconstruct prompt/context, then apply summaries and provider replay."""


class AvatarParentPort(Protocol):
    """The parent facts Avatar needs to spawn and control its own subtree.

    This is deliberately Avatar-specific rather than a second whole-Agent
    facade: spawning needs only the parent identity and inherited runtime
    location. It grants no mutable administration surface, no generic
    configuration, and no tool mounting capability.
    """

    @property
    def parent_name(self) -> str:
        """The parent identity to put in the newborn's first prompt."""

    @property
    def venv_path(self) -> str | None:
        """Optional parent runtime location inherited by a newborn avatar."""

    def authorize_derived_launch(self, capability: Any) -> Any:
        """Decide one avatar-derived process launch before side effects."""


class DaemonRuntimePort(Protocol):
    """Daemon's capability-native view of its selected host runtime.

    Daemon needs more than a directory: a host may supply an inherited model
    service/tool surface or require direct presets, plus preset loading, one
    notification route, compact runtime settings, and event logging. Those
    facts are exposed as named operations rather than as a whole ``Agent``.
    The port deliberately owns no model-facing mount operation; that remains
    registrar-only through :class:`ToolMountPort`.
    """

    @property
    def service(self) -> Any:
        """The optional parent service used only by inherited no-preset bindings."""

    @property
    def tool_schemas(self) -> tuple[Any, ...]:
        """Current parent dynamic schemas, in their host order."""

    @property
    def tool_handlers(self) -> Mapping[str, Callable[[dict], dict]]:
        """Current parent dynamic dispatch handlers by public tool name."""

    @property
    def mcp_tool_names(self) -> frozenset[str]:
        """Names occupied by parent MCP tools, which Daemon never auto-inherits."""

    @property
    def language(self) -> str:
        """Resolved parent prompt language."""

    @property
    def max_aed_attempts(self) -> int:
        """Resolved parent empty-response retry limit."""

    @property
    def tool_call_guard(self) -> Any:
        """The parent guard supplied to daemon-local tool execution, if any."""

    @property
    def manager_options(self) -> Mapping[str, Any]:
        """Resolved construction options for this daemon manager binding."""

    @property
    def requires_derived_launch_admission(self) -> bool:
        """Whether this binding must fail closed for derived launches.

        This is policy state, not an authority decision or a grant.  Daemon
        uses it to reject external CLI execution before it can ask Driver for
        an endpoint that this backend cannot consume.
        """

    def authorize_derived_launch(self, capability: Any) -> Any:
        """Decide one daemon-derived process launch before side effects."""

    def setup_preset_capability(
        self, name: str, kwargs: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Callable[[dict], dict]]]:
        """Build one preset capability's isolated tool surface without mounting it."""

    @property
    def requires_explicit_preset(self) -> bool:
        """Whether every native LingTai task must supply a preset path."""

    def is_preset_authorized(self, name: str, working_dir: Any) -> bool:
        """Authorize one explicit preset reference for this runtime binding."""

    def load_preset(self, name: str) -> dict:
        """Load one authorized or directly supplied preset through the canonical route."""

    def enqueue_daemon_notification(
        self,
        *,
        source: str,
        ref_id: str,
        body: str,
        idempotency_key: str | None,
        skip_if_idempotency_key_exists: bool,
        extra: Mapping[str, Any],
        channel: str,
    ) -> None:
        """Publish one parent-facing Daemon notification through the host."""

    def has_active_task_card_watch(self) -> bool | None:
        """Whether a present Task Card host has a live watch, else ``None``."""

    def attach_daemon_manager(self, manager: Any) -> None:
        """Retain this binding's manager for the capability setup return value."""

    def now_iso(self) -> str:
        """Render the host-configured current timestamp for a daemon prompt."""

    def log(self, event_type: str, **fields: Any) -> None:
        """Record one Daemon lifecycle event through the host journal."""


@dataclass(frozen=True)
class PluginCatalogState:
    """Read-only facts the official ``plugin`` family presents.

    This is intentionally a projection, not an Agent Plugins implementation:
    the host owns registration, the service owns manifest/component validation,
    and the family merely needs the latest boot snapshot plus the three path/
    availability inputs that define discovery.  The state has no mutation,
    lifecycle, launch, or filesystem operation.
    """

    registration: Mapping[str, Any]
    configured_paths: tuple[str, ...]
    skill_paths: tuple[str, ...]
    skills_enabled: bool


class PluginCatalogPort(Protocol):
    """Read the current Agent Plugins catalog presentation inputs.

    The official ``plugin`` family needs exactly this one read-only projection to
    preserve its pre-declaration behavior: the registration snapshot produced at
    boot, its own configured discovery paths, inherited skill paths, and whether
    the skills catalog is enabled.  It cannot register, prune, launch, or alter
    any of those facts through this port.
    """

    def read_state(self) -> PluginCatalogState:
        """Return the current detached catalog presentation state."""


class NotificationStatePort(Protocol):
    """Notification Core operations bound to one live agent's real state.

    The notification family may ask Core to manipulate notification-owned
    mirrors and hook registration, but it never receives the Agent, its Store,
    delivery fingerprints, or producer state directly. The host adapter binds
    each operation to the real agent before the plugin is composed, preserving
    Core's allowlist, producer-guard, stale-version, acknowledgement, timer,
    and Store semantics rather than recreating a parallel local state machine.
    """

    def dismiss(
        self,
        channel: str,
        *,
        force: bool,
        reason: str | None,
        event_id: str | None = None,
        ref_id: str | None = None,
    ) -> dict[str, Any]:
        """Ask Notification Core to clear one permitted mirror target."""

    def delay(self, channel: str, seconds: int) -> dict[str, Any]:
        """Apply consumer-only delay policy without mutating producer state."""

    def add_hook(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Add one hook manifest through Notification Core and its Store."""

    def drop_hook(self, name: str) -> dict[str, Any]:
        """Drop one hook manifest through Notification Core and its Store."""

    def edit_hook(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Edit one hook manifest through Notification Core and its Store."""

    def list_hooks(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Read hook manifests through Notification Core and its Store."""

    def read_settings(self) -> tuple[int, int]:
        """Read the effective payload cap and delay ceiling for this agent."""

    def log(self, event_type: str, **fields: Any) -> None:
        """Record a bounded notification action diagnostic."""


class NotificationPort(Protocol):
    """Publish a bounded durable notification without reaching the Agent.

    The Shell slice needs exactly two already-existing notification operations:
    an idempotent append to the durable system-event stream for its async
    watchdog, and a latest-channel completion publication.  The port carries no
    prompt, tool, lifecycle, or arbitrary filesystem operation; an adapter owns
    the Agent/store details and preserves those existing notification semantics.
    It is deliberately distinct from :class:`NotificationStatePort`, which grants
    the ``notification`` family Core's mirror/hook administration instead.
    """

    def publish_system(
        self,
        *,
        source: str,
        ref_id: str,
        body: str,
        skip_if_ref_id_exists: bool = False,
    ) -> bool:
        """Append one durable system event; return whether publication completed."""

    def publish_channel(
        self,
        channel: str,
        payload: Mapping[str, Any],
        *,
        ref_id: str,
    ) -> bool:
        """Publish one idempotent latest-channel payload for *channel*."""


class ConfigurationPort(Protocol):
    """Read the immutable capability configuration selected by composition.

    A declaration is static, while capability setup supplies its policy and
    platform overrides at boot.  This port exposes only that explicit copied
    mapping; it is not an Agent configuration API and does not permit writes.
    File, Shell, and Vision each consume it for their own setup snapshot; the
    kernel gives the mapping no schema — the consuming family owns its
    interpretation.
    """

    @property
    def values(self) -> Mapping[str, Any]:
        """The static, copied configuration mapping for this plugin binding."""


class ActiveProviderPort(Protocol):
    """Read the current provider service selected by the host.

    The service is intentionally opaque to the kernel: a family that genuinely
    shares its active provider may inspect its provider/model/credential route,
    but receives neither the Agent nor a generic route to another capability.
    The adapter reads through on every access so refresh cannot leave a plugin
    with a stale provider identity. Vision is the one consumer today.
    """

    @property
    def service(self) -> Any:
        """The current active provider service, or ``None`` when absent."""


class ProviderIdentityPort(Protocol):
    """Read only the current canonical LLM provider *label*, if one exists.

    Deliberately narrower than :class:`ActiveProviderPort`: Web needs one
    truthful string to decide whether an explicit Anthropic/Gemini opt-in is
    eligible, and nothing else. The port grants neither the provider service,
    its credentials or model configuration, the Agent, nor any provider
    registry; the adapter reads the label through on every access so a refresh
    never leaves a stale identity. Web is the one consumer today.
    """

    @property
    def provider(self) -> str | None:
        """The current canonical provider name, or ``None`` when unavailable."""


class SoulRuntimePort(Protocol):
    """Soul's bounded live-self and soul-flow runtime surface.

    This port exists because Soul's public actions are real self-state
    operations, not signposts: they inspect the current conversation, mutate
    only ``manifest.soul``-backed cadence/voice state, use the existing
    consultation lock/timer, and publish or dismiss Soul's own notification.
    The explicit members below are the complete vocabulary the Soul package
    consumes; it receives neither the live Agent nor a generic attribute
    escape hatch. The production adapter owns translations to the Agent's
    private storage and kernel notification helpers.
    """

    @property
    def working_dir(self) -> Path: ...

    @property
    def config(self) -> Any: ...

    @property
    def service(self) -> Any: ...

    @property
    def chat(self) -> Any: ...

    @property
    def session(self) -> Any: ...

    @property
    def agent_name(self) -> str: ...

    @property
    def state(self) -> Any: ...

    @property
    def idle_event(self) -> Any: ...

    @property
    def shutdown(self) -> Any: ...

    @property
    def soul_delay(self) -> float: ...

    @soul_delay.setter
    def soul_delay(self, value: float) -> None: ...

    @property
    def soul_timer(self) -> Any: ...

    @soul_timer.setter
    def soul_timer(self, value: Any) -> None: ...

    @property
    def fire_lock(self) -> Any: ...

    @property
    def notification_store(self) -> Any: ...

    @property
    def notification_fingerprint(self) -> Any: ...

    @property
    def appendix_ids_by_source(self) -> dict[str, str]: ...

    def log(self, event: str, **fields: Any) -> None: ...

    def restart_soul_timer(self) -> None: ...

    def run_consultation_fire(self) -> None: ...

    def sync_notifications(self) -> None: ...

    def wake_nap(self, reason: str) -> None: ...

    def persist_soul_entry(
        self, result: dict, mode: str = "flow", source: str = "agent"
    ) -> None: ...

    def append_soul_flow_record(self, record: dict) -> None: ...

    def publish_notification(self, channel: str, **kwargs: Any) -> None: ...

    def clear_notification(self, channel: str) -> None: ...

    def dismiss_notification(self, channel: str, *, invoked_by: str) -> dict: ...


class SystemRuntimePort(Protocol):
    """The System family's bounded runtime/lifecycle vocabulary.

    This is intentionally not an Agent-shaped object.  It supplies exactly the
    existing lifecycle, preset, audit, authority, and self-sleep operations
    System already needs; the adapter composes each operation from a narrow
    Agent callback.  Agent identity is deliberately absent and lives on
    :class:`IdentityPort` instead.

    The four sleep members are evidence/effects only: the sleep *policy*
    (fingerprint comparison, refusal/force, receipts, audit events) is owned by
    ``lingtai.tools.system.karma.sleep_use_case``, which this port never
    duplicates.  ``sleep_alarm_lock``/``arm_sleep_alarm`` carry the persisted
    one-shot ``sleep(delay=...)`` alarm effect so arming and the ASLEEP
    transition stay under the host's one heartbeat-shared lock.
    """

    @property
    def admin(self) -> Mapping[str, Any]: ...

    @property
    def language(self) -> str: ...

    def log(self, event: str, **fields: Any) -> None: ...

    def token_usage(self) -> Mapping[str, Any]: ...

    def load_preset(self, name: str) -> dict: ...

    def activate_preset(self, name: str) -> None: ...

    def activate_default_preset(self) -> None: ...

    def retry_failed_mcps(self) -> Mapping[str, Any]: ...

    def perform_refresh(self) -> None: ...

    def resuscitate(self, address: str) -> Any: ...

    def sleep_attention_fingerprints(
        self,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]: ...

    def transition_to_asleep(self) -> None: ...

    def sleep_alarm_lock(self) -> Any: ...

    def arm_sleep_alarm(self, delay_seconds: Any) -> str: ...


class IdentityPort(Protocol):
    """The System family's live, durable naming operations only."""

    @property
    def name(self) -> str | None: ...

    def set_name(self, name: str) -> None: ...

    def set_nickname(self, nickname: str) -> None: ...


class ShutdownPort(Protocol):
    """Observe whether this Agent is stopping.

    A Task Card watch polls this one predicate between renderer runs so an
    agent stop ends its thread promptly.  It grants no lifecycle transition,
    join, or event mutation.
    """

    def is_set(self) -> bool:
        """Return true after the current Agent has begun shutdown."""


class TaskCardLifecyclePort(Protocol):
    """Retain the one Task Card manager for this current Agent.

    The public producer needs exactly one persistent in-process owner across
    refreshes so the BaseAgent lifecycle can stop/re-persist the real watch.
    This is intentionally not a generic state bag: it only reads or replaces
    this family's manager and records its one boot-resume diagnostic.
    """

    def current_manager(self) -> Any | None:
        """Return this Agent's existing Task Card manager, if any."""

    def retain_manager(self, manager: Any) -> None:
        """Make *manager* the current Agent's Task Card lifecycle owner."""

    def report_resume_failure(self, error: str) -> None:
        """Record a bounded diagnostic when persisted-watch resume fails."""


class TaskCardNotificationsPort(Protocol):
    """Emit only the Task Card producer's established current-Agent notices.

    Five closed, operation-native methods and nothing else: there is no
    generic ``enqueue``, no ``**kwargs``, and no ``source``/``channel``/
    ``extra`` argument.  The production adapter pins the established
    ``task_card.error``/``task_card.limit`` sources, the ``system`` channel,
    priority, idempotency skip, and the bounded ``extra`` projection
    internally, so a holder of this port can neither publish a foreign source
    nor address another channel.  The kernel imports nothing from the family;
    the signatures are scalar so the dependency direction stays inward.
    """

    def publish_error(
        self,
        watch_id: str,
        body: str,
        code: str,
        retryable: bool | str,
        idempotency_key: str,
        last_valid_body_at: str | None = None,
    ) -> None:
        """Queue one deduplicated ``task_card.error`` event in state ``error``."""

    def publish_recovered(self, watch_id: str, body: str, idempotency_key: str) -> None:
        """Queue one deduplicated ``task_card.error`` event in state ``recovered``."""

    def publish_limit(
        self,
        watch_id: str,
        body: str,
        idempotency_key: str,
        used: int,
        max_refreshes: int,
        last_valid_body_at: str | None = None,
    ) -> None:
        """Queue one deduplicated ``task_card.limit`` refresh-exhaustion event."""

    def submit_reminder(self, turns: int) -> None:
        """Publish the producer's absent/stale Task Card reminder."""

    def clear_reminder(self) -> None:
        """Clear the producer's current Task Card reminder."""


class ToolMountPort(Protocol):
    """Mount one registrar transaction onto the live model-facing surface.

    Host-only. It is never granted to a declaration (see
    :data:`GRANTABLE_HOST_PORTS`); only
    :func:`register_official_tool_plugins` calls it, and only after every name
    check has passed. The transaction binds the authorization to the exact
    declaration and bound plugin produced by that registration.
    """

    def mount_tool(self, transaction: "_OfficialMountTransaction") -> None:
        """Publish the registrar-created *transaction*."""


# ---------------------------------------------------------------------------
# The least-privilege host facade
# ---------------------------------------------------------------------------

class ToolPluginHost:
    """Exactly the host ports one declaration named in ``requires``.

    Attribute access is the whole surface: a granted port is an attribute, and
    anything else raises :class:`AttributeError`. The facade holds no reference
    to the live ``Agent`` — the Composition Root's adapters do, and they expose
    only their own port operation.

    Python cannot make a live object deeply unreachable, and this class does not
    pretend otherwise: an adapter's private attributes are still private
    attributes. The promise is about the *declared argument surface*. A plugin
    that reaches around a port into adapter internals violates this contract,
    exactly as a plugin reaching into ``agent._prompt_manager`` does today.
    """

    __slots__ = ("_plugin_name", "_ports")

    def __init__(self, plugin_name: str, ports: Mapping[str, Any]) -> None:
        self._plugin_name = plugin_name
        self._ports = dict(ports)

    @property
    def plugin_name(self) -> str:
        """The official plugin name this facade was granted to."""
        return self._plugin_name

    @property
    def granted(self) -> tuple[str, ...]:
        """The granted port names, in declaration order."""
        return tuple(self._ports)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._ports[name]
        except KeyError:
            raise AttributeError(
                f"tool plugin {self._plugin_name!r} did not require host port "
                f"{name!r}; granted ports are {sorted(self._ports)}"
            ) from None

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"ToolPluginHost(plugin_name={self._plugin_name!r}, "
            f"granted={self.granted!r})"
        )

    @classmethod
    def grant(
        cls,
        declaration: "ToolPluginDeclaration",
        ports: Mapping[str, Any],
    ) -> "ToolPluginHost":
        """Build the facade for *declaration* from the host's port table.

        Every name in ``declaration.requires`` must be present in *ports*;
        nothing else from *ports* is granted. A missing port is a wiring defect
        and fails loudly rather than degrading into a half-privileged plugin.
        """
        missing = [name for name in declaration.requires if name not in ports]
        if missing:
            raise HostPortError(
                f"tool plugin {declaration.name!r} requires host port(s) "
                f"{missing} that the host did not provide"
            )
        return cls(
            declaration.name,
            {name: ports[name] for name in declaration.requires},
        )


# ---------------------------------------------------------------------------
# Declaration and its bound result
# ---------------------------------------------------------------------------

def _advertised_actions(schema: Any) -> tuple[str, ...] | None:
    """The action inventory a composed model-facing schema advertises.

    This is the **one** structural fact the kernel reads out of a composed
    schema. It composes no schema and validates no other part of the LTP
    envelope — that stays owned by ``src/lingtai/tools/CONTRACT.md`` and
    ``lingtai.tools.tool_family`` — but the advertised action inventory *is*
    the model-facing identity a reserved official name was granted for, so
    :meth:`ToolPluginDeclaration.bind` compares it against the declaration
    rather than trusting the two to agree. Returns ``None`` when the schema
    advertises no enum at all, which :meth:`~ToolPluginDeclaration.bind`
    treats as a defect rather than as permission to skip the check.
    """
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    action = properties.get("action") if isinstance(properties, Mapping) else None
    enum = action.get("enum") if isinstance(action, Mapping) else None
    if isinstance(enum, (list, tuple)):
        return tuple(enum)
    return None


@dataclass(frozen=True)
class BoundToolPlugin:
    """One declaration bound to a host: the mountable model-facing surface.

    ``activate`` is the plugin's explicit, separate boot presentation step —
    the thing registration is *not*. Binding composes and validates; activation
    runs only when the registrar reaches it, after every name check. Neither
    starts a process, a server, or a transport.
    """

    name: str
    schema: Mapping[str, Any]
    handler: Callable[[dict], dict]
    description: str = ""
    glossary_package: str | None = None
    activate: Callable[[], None] | None = None


class _OfficialMountTransaction:
    """One-use registrar-issued authorization for one official mount.

    Construction is intentionally not a public ``(declaration, plugin)``
    operation. The registrar issues this object with the module-local issuer
    after ``declaration.bind()`` succeeds; the host mount route then consumes
    the exact declaration/bound result carried by that issuance. The issuer is
    provenance, not an absolute security boundary: trusted in-process Python
    can inspect private module state. It does ensure that public/declared and
    normal extension paths cannot manufacture a foreign handler/schema by
    calling this constructor or passing an arbitrary ``BoundToolPlugin``.
    """

    __slots__ = (
        "_declaration",
        "_plugin",
        "_issuer",
        "_consumed",
        "_mounted_agent",
    )

    def __init__(
        self,
        declaration: "ToolPluginDeclaration | None" = None,
        plugin: BoundToolPlugin | None = None,
        *,
        _issuer: object | None = None,
    ) -> None:
        if _issuer is not _OFFICIAL_MOUNT_ISSUER:
            raise PermissionError(
                "official mount transactions are issued only by the kernel registrar"
            )
        if not isinstance(declaration, ToolPluginDeclaration):
            raise TypeError("official mount transaction requires a declaration")
        if not isinstance(plugin, BoundToolPlugin):
            raise TypeError("official mount transaction requires a bound plugin")
        self._declaration = declaration
        self._plugin = plugin
        self._issuer = _issuer
        self._consumed = False
        self._mounted_agent = None

    @classmethod
    def issue(
        cls,
        declaration: "ToolPluginDeclaration",
        plugin: BoundToolPlugin,
    ) -> "_OfficialMountTransaction":
        return cls(declaration, plugin, _issuer=_OFFICIAL_MOUNT_ISSUER)

    @property
    def declaration(self) -> "ToolPluginDeclaration":
        return self._declaration

    @property
    def plugin(self) -> BoundToolPlugin:
        return self._plugin

    @property
    def mounted_agent(self) -> Any:
        return self._mounted_agent

    def consume(self) -> None:
        if self._consumed:
            raise PermissionError("official mount authorization was already consumed")
        self._consumed = True

    def mark_mounted(self, agent: Any) -> None:
        if not self._consumed or self._mounted_agent is not None:
            raise PermissionError("official mount transaction was not consumed for this mount")
        self._mounted_agent = agent


@dataclass(frozen=True)
class ToolPluginDeclaration:
    """One official model-facing tool family, declared statically.

    Constructible at import time, before any ``Agent`` exists, and validated at
    construction so a packaging defect fails loudly at import instead of
    shipping silently.

    ``actions`` are operational. Optional ``settings`` and reserved ``manual``
    are added by :attr:`public_actions`, with ``settings`` immediately before
    ``manual`` when its boolean opt-in is true. Their schemas come from
    :attr:`public_input_schemas`,
    mirroring ``lingtai.mcp_servers._plugin.CuratedMcpPlugin.actions``. The
    family still owns the manual child's handler and its packaged/installed
    source.

    ``binder`` is how this family composes itself against a granted host. It is
    called only through :meth:`bind`, which builds nothing itself.
    """

    name: str
    actions: tuple[str, ...]
    input_schemas: Mapping[str, Mapping[str, Any]]
    manual_input_schema: Mapping[str, Any]
    manual: str
    description: str
    binder: Callable[[ToolPluginHost], BoundToolPlugin]
    requires: tuple[str, ...] = ()
    glossary_package: str | None = None
    settings: bool = False

    def __post_init__(self) -> None:
        for attribute in ("name", "manual", "description"):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not value.strip():
                raise ToolPluginDeclarationError(
                    f"ToolPluginDeclaration {attribute!r} must be a non-empty string"
                )
        if type(self.settings) is not bool:
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} settings must be a boolean"
            )
        if not isinstance(self.actions, tuple) or (not self.actions and not self.settings):
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} must declare actions as a "
                "tuple and may be empty only with settings opt-in"
            )
        reserved = [
            action for action in (_SETTINGS_ACTION, MANUAL_ACTION) if action in self.actions
        ]
        if reserved:
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} must not declare the "
                f"reserved {reserved[0]!r} action; reserved actions are added "
                "by the generic declaration contract"
            )
        if len(set(self.actions)) != len(self.actions):
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} declared a duplicate action"
            )
        declared_schemas = set(self.input_schemas)
        if declared_schemas != set(self.actions):
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} must declare exactly one "
                f"input schema per action; actions={sorted(self.actions)} "
                f"schemas={sorted(declared_schemas)}"
            )
        unknown = [name for name in self.requires if name not in GRANTABLE_HOST_PORTS]
        if unknown:
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} requires non-grantable host "
                f"port(s) {unknown}; grantable ports are "
                f"{list(GRANTABLE_HOST_PORTS)}"
            )
        if len(set(self.requires)) != len(self.requires):
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} requires a duplicate host port"
            )
        if not callable(self.binder):
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} binder must be callable"
            )

    @property
    def public_actions(self) -> tuple[str, ...]:
        """Public actions, adding opted-in ``settings`` immediately before manual."""
        if not self.settings:
            return (*self.actions, MANUAL_ACTION)
        return (*self.actions, _SETTINGS_ACTION, MANUAL_ACTION)

    def public_input_schemas(self) -> dict[str, Mapping[str, Any]]:
        """Return declared schemas plus the generically composed reserved schemas."""
        schemas: dict[str, Mapping[str, Any]] = dict(self.input_schemas)
        if self.settings:
            schemas[_SETTINGS_ACTION] = _settings_input_schema()
        schemas[MANUAL_ACTION] = self.manual_input_schema
        return schemas

    def bind(self, host: ToolPluginHost) -> BoundToolPlugin:
        """Compose this family against a granted host facade.

        Pure composition: it must not mount, start, spawn, or connect anything.
        Two declaration gates run on every boot: the bound plugin's name must
        match the reserved name, and its advertised action inventory must equal
        :attr:`public_actions`.
        Declared-versus-shipped agreement is enforced here in the registrar's
        own path, not merely asserted once in a test.
        """
        if not isinstance(host, ToolPluginHost):
            raise HostPortError(
                f"tool plugin {self.name!r} must be bound to a ToolPluginHost, "
                f"not {type(host).__name__}"
            )
        if host.plugin_name != self.name:
            raise HostPortError(
                f"tool plugin {self.name!r} was handed a host granted to "
                f"{host.plugin_name!r}"
            )
        bound = self.binder(host)
        if not isinstance(bound, BoundToolPlugin):
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} binder returned "
                f"{type(bound).__name__}, expected BoundToolPlugin"
            )
        if bound.name != self.name:
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} bound itself as "
                f"{bound.name!r}"
            )
        advertised = _advertised_actions(bound.schema)
        if advertised is None:
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} bound a plugin whose "
                "schema advertises no action enum; an official plugin must "
                "ship the public actions it declared"
            )
        if advertised != self.public_actions:
            raise ToolPluginDeclarationError(
                f"ToolPluginDeclaration {self.name!r} declared public actions "
                f"{list(self.public_actions)} but bound a plugin advertising "
                f"{list(advertised)}"
            )
        return bound


# ---------------------------------------------------------------------------
# The fail-fast registrar
# ---------------------------------------------------------------------------

def register_official_tool_plugins(
    declarations: Sequence[ToolPluginDeclaration],
    *,
    ports_for: Callable[[ToolPluginDeclaration], Mapping[str, Any]],
    mount: ToolMountPort,
    claimed: Mapping[str, ToolPluginDeclaration],
    claim: Callable[[_OfficialMountTransaction], None] | None = None,
    authorize: Callable[[ToolPluginDeclaration], None] | None = None,
    record_bound: Callable[[ToolPluginDeclaration, BoundToolPlugin], None] | None = None,
) -> tuple[BoundToolPlugin, ...]:
    """Register official declarations, refusing name conflicts before any bind.

    Order is the promise. Every name in *declarations* is checked against the
    kernel-owned reserved list, against the rest of the batch, and against
    *claimed* — the live official namespace — **before** the first
    :meth:`ToolPluginDeclaration.bind`, the first ``activate``, and the first
    :meth:`ToolMountPort.mount_tool`. A conflict therefore leaves the live tool
    surface exactly as it was: there is no last-registration-wins path here, and
    a name conflict never leaves a partially mounted batch.

    That promise is scoped to *names*, exactly. The second loop below mounts and
    claims each member as it goes, so a failure raised by ``ports_for``,
    ``grant``, ``bind``, ``activate``, or ``mount_tool`` on member *N* leaves
    members 1..*N*-1 mounted and claimed, and propagates. Rolling those back
    would require an unmount port this component deliberately does not own; the
    honest statement is that a *name* conflict is refused as a unit, while a
    binder or host defect fails loudly mid-batch.

    Re-registering the *same* declaration object for an already-claimed name is
    idempotent, because ``_setup_from_init`` re-runs the whole boot on every
    refresh. A *different* declaration claiming a live name is the collision
    this function exists to refuse.

    *ports_for* builds one declaration's full grantable port table; the
    declaration is then granted only the subset it named in ``requires``. It is
    a factory rather than a single table because a port may legitimately be
    bound to the declaring plugin's identity — ``prompt_section`` is bound to
    that plugin's own section name. *mount* is host-only and is never granted.
    """
    batch: list[ToolPluginDeclaration] = list(declarations)

    seen: set[str] = set()
    for declaration in batch:
        name = declaration.name
        if name not in OFFICIAL_TOOL_PLUGIN_NAMES:
            raise UnreservedToolPluginNameError(
                f"{name!r} is not a reserved official tool plugin name; "
                f"reserved names are {list(OFFICIAL_TOOL_PLUGIN_NAMES)}"
            )
        if name in seen:
            raise DuplicateToolPluginNameError(
                f"official tool plugin name {name!r} was declared twice in one "
                "registration batch"
            )
        seen.add(name)
        live = claimed.get(name)
        if live is not None and live is not declaration:
            raise DuplicateToolPluginNameError(
                f"official tool plugin name {name!r} is already claimed by a "
                "different declaration; official names are reserved first and "
                "are not overwritable"
            )

    # A live Agent supplies this callback to keep the canonical declaration
    # independent of its mutable live-claim view. It runs only after the whole
    # name-conflict pass, so a foreign declaration cannot reach bind/mount.
    if authorize is not None:
        for declaration in batch:
            authorize(declaration)

    bound_plugins: list[BoundToolPlugin] = []
    for declaration in batch:
        host = ToolPluginHost.grant(declaration, ports_for(declaration))
        bound = declaration.bind(host)
        if record_bound is not None:
            # The host records the exact bind result before issuance; the mount
            # route later rejects a transaction for any other plugin object.
            record_bound(declaration, bound)
        if bound.activate is not None:
            bound.activate()
        transaction = _OfficialMountTransaction.issue(declaration, bound)
        mount.mount_tool(transaction)
        if claim is None:
            # The standalone kernel registrar remains usable with a plain
            # mutable mapping in tests and other kernel-owned composition code.
            claimed[declaration.name] = declaration  # type: ignore[index]
        else:
            # Agent claims are accepted only for this registrar-issued
            # transaction after the mount seam has marked it mounted.
            claim(transaction)
        bound_plugins.append(bound)
    return tuple(bound_plugins)
