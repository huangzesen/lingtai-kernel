"""Vision capability — image understanding via VisionService.

Adds the ability to analyze images. Requires a VisionService instance,
created either explicitly or via the ``provider``/``api_key`` factory.

Usage:
    agent.add_capability("vision", vision_service=my_svc)
    agent.add_capability("vision", provider="anthropic", api_key="sk-...")

The native mlx pseudo-provider (Apple MLX, on-device) remains available
through explicit ``add_capability(..., provider="mlx")`` opt-in, but it is
intentionally not advertised in ``PROVIDERS`` or first-run/check-caps
surfaces: it is macOS-only and requires an on-device model.

``local`` is a first-class generic local OpenAI-compatible provider: it
points at any OpenAI-compatible vision server on your machine (Ollama, LM
Studio, vLLM, llama.cpp, …) via ``base_url`` (default
``http://localhost:11434/v1``) and requires an explicit ``model``. The
operator-owned endpoint configuration lives in ``settings/vision.json``
(``base_url``, ``model``, optional ``api_key``/``max_tokens``); capability
kwargs override the file. No API key is required — local servers ignore the
value, so a placeholder is synthesized. Configure it with
``add_capability("vision", provider="local", model="<pulled-model>")``, via
``manifest.capabilities.vision``, or via ``settings/vision.json``.

``vision`` is migrated to the LingTai Tool Protocol v2 action-separated shape
(``src/lingtai/tools/CONTRACT.md``): one public ``vision`` tool whose canonical
children are ``analyze``/``check``/``list`` plus the family-owned reserved
``settings``/``manual`` actions, composed and dispatched by the generic
``lingtai.tools.tool_family`` infrastructure. The public tool name and
operational action values are unchanged; generic composition adds the new
reserved ``settings`` action immediately before ``manual``. The call envelope
moved from flat arguments to ``action``/``input``/``reasoning``/``summarize``.
Provider routing, credential/identity resolution, and every action result
shape are untouched by that migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Mapping

from lingtai.kernel.tool_plugin import BoundToolPlugin, ToolPluginDeclaration, ToolPluginDeclarationError

from ..tool_family import ChildTool, SettingRow, SettingsProvider, ToolFamily
from ..tool_family.manual import MANUAL_INPUT_SCHEMA, build_manual_child
from .settings import DEFAULT_LOCAL_BASE_URL, LocalVisionSettings, SettingsError


if TYPE_CHECKING:
    from lingtai.kernel.base_agent import BaseAgent
    from lingtai.kernel.tool_plugin import ActiveProviderPort, ToolPluginHost, WorkdirPort
    from lingtai.services.vision import VisionService


def _setup_failure(provider: str, exc: BaseException) -> str:
    """Build explicit manual guidance without exposing exception contents."""
    return (
        f"Direct vision setup failed for provider {provider!r} "
        f"({type(exc).__name__}); use vision(action='manual', input={{}}, "
        f"reasoning='direct vision setup failed, load the manual route')."
    )


def _consent_guidance() -> str:
    """Build the setup-with-human-consent guidance for a vision failure.

    Installing a local vision server, pulling a model, or editing
    settings/vision.json / the capability manifest are external side effects:
    the agent must obtain explicit human consent before performing them. The
    full steps live in the vision manual skill.
    """
    return (
        "To enable vision, load the vision manual skill for the setup steps: "
        "vision(action='manual', input={}, reasoning='vision is not set up, "
        "load the setup steps'); then ask the human for consent before "
        "installing a local vision server, pulling a model, or editing "
        "settings/vision.json / the capability manifest."
    )


_CODEX_POOL_ALIASES = {"codex-pool", "codex_pool"}
_CODEX_FAMILY = {"codex"} | _CODEX_POOL_ALIASES

# Claude Code CLI vision: all three spellings identify the claude backend
# whose vision route is the operator-installed Claude Code CLI (``claude -p``).
# LingTai does not proxy the CLI's auth, so these providers return explicit
# guidance instead of constructing a service. ``claude-p`` is the explicit
# vision-route alias alongside the LLM registry's two canonical adapter
# spellings (``claude-code``/``claude_code``).
_CLAUDE_CLI_FAMILY = {"claude-p", "claude-code", "claude_code"}

_VISION_SETTING_KEYS = (
    "provider",
    "base_url",
    "model",
    "api_key",
    "api_key_env",
    "max_tokens",
    "api_compat",
    "wire_api",
    "default_headers",
    "token_path",
    "instructions",
    "max_output_tokens",
    "timeout",
)
_VISION_SENSITIVE_SETTINGS = {
    "base_url",
    "api_key",
    "api_key_env",
    "default_headers",
    "token_path",
    "instructions",
}
_MODEL_DEFAULTS = {
    "mlx": "mlx-community/paligemma2-3b-ft-docci-448-8bit",
}
_BASE_URL_DEFAULTS = {
    "local": DEFAULT_LOCAL_BASE_URL,
    "mimo": "https://api.xiaomimimo.com/v1",
    "codex": "https://chatgpt.com/backend-api/codex",
    "codex-pool": "https://chatgpt.com/backend-api/codex",
    "codex_pool": "https://chatgpt.com/backend-api/codex",
}
_MAX_TOKENS_DEFAULTS = {
    "local": 1024,
    "openai": 1024,
    "openrouter": 1024,
    "custom": 1024,
    "deepseek": 1024,
    "zhipu": 1024,
    "glm": 1024,
    "grok": 1024,
    "qwen": 1024,
    "kimi": 1024,
    "anthropic": 1024,
    "minimax": 1024,
    "mimo": 1024,
    "mlx": 512,
}


@dataclass(frozen=True, slots=True)
class _VisionSettingsSnapshot:
    """One applied bind snapshot containing no raw sensitive values."""

    current: tuple[Any, ...]
    default: tuple[Any, ...]
    sensitive: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class _VisionRouteProvenance:
    """Resolver-produced protocol provenance for the successfully bound route."""

    api_compat: str | None = None


def _same_codex_family(requested: str, active: str) -> bool:
    """Return whether both names are Codex-family spellings.

    Provider spelling is only a Codex-family *compatibility gate*: ``codex``,
    ``codex-pool``, and ``codex_pool`` all resolve to the one native Codex
    factory (see ``lingtai/llm/_register.py``). Spelling never selects the
    fixed/direct vs weighted/pool route; that choice is made solely from the
    active provider-default bucket (``_codex_bucket_route``).
    """
    return requested in _CODEX_FAMILY and active in _CODEX_FAMILY


def _vision_endpoint(provider: str | None) -> str:
    """Classify a provider's vision endpoint for the mechanical ``list`` action.

    Pure string classification — never constructs a service, reads a
    credential, or touches the network.
    """
    key = (provider or "").lower()
    if key in _CODEX_FAMILY:
        return "responses"
    if key in _CLAUDE_CLI_FAMILY:
        return "claude-cli"
    if key == "local":
        return "openai-compatible-local"
    if key == "mlx":
        return "mlx-on-device"
    if key in PROVIDERS.get("providers", ()):
        return "provider-service"
    return "unknown"


def _responses_vision(provider: str | None) -> bool:
    """Return whether a provider routes vision through the Responses API."""
    return bool(provider and provider.lower() in _CODEX_FAMILY)


def _canonical_preset_path(ref: str, working_dir: Path) -> str:
    """Return the canonical physical path a preset reference denotes.

    ``~/x.json``, its expanded absolute spelling, and a working-dir-relative
    spelling all name one file, so ``list`` keys its rows on this value (the
    same normalization the kernel's allowed-preset membership test applies).
    A reference that cannot be resolved keys on its own spelling: it is never
    dropped here, and it stays exactly as authorization-bounded as before.
    """
    try:
        path = Path(ref).expanduser()
        if not path.is_absolute():
            path = Path(working_dir) / path
        return str(path.resolve(strict=False))
    except (ValueError, OSError, RuntimeError):
        return ref


def _normalize_codex_auth_path(raw: object) -> str | None:
    """Return a trimmed nonblank Codex auth path, or ``None``.

    Mirrors the canonical Codex factory (``lingtai/llm/_register.py`` ``_codex``),
    which strips ``codex_auth_path`` before constructing ``FixedAccountSource``.
    The single trimmed value is used both to decide the direct route and as the
    propagated ``token_path``, so a space-padded path never routes direct while
    forwarding an invalid, un-normalized value.
    """
    if isinstance(raw, str):
        trimmed = raw.strip()
        if trimmed:
            return trimmed
    return None


def _codex_bucket_route(bucket: dict | None) -> str:
    """Resolve the active Codex route from the provider-default bucket.

    Mirrors the canonical Codex factory: the route is ``"direct"`` iff the
    active bucket carries a nonblank ``codex_auth_path`` (trimmed; Fixed
    account); otherwise it is ``"pool"`` (Weighted account selection). The
    request spelling is irrelevant — an active ``codex-pool`` service that
    configures a ``codex_auth_path`` is a direct/Fixed route, exactly as the
    factory treats it.
    """
    if isinstance(bucket, dict) and _normalize_codex_auth_path(bucket.get("codex_auth_path")):
        return "direct"
    return "pool"


def _same_provider_identity(requested: str, active: str) -> bool:
    """Return whether two provider names identify the same current route."""
    if requested == active:
        return True
    if {requested, active} <= {"glm", "zhipu"}:
        return True
    if {requested, active} <= _CLAUDE_CLI_FAMILY:
        return True
    return _same_codex_family(requested, active)


def _effective_openai_wire(
    wire_api: str | None,
    *,
    use_responses_api: bool,
    base_url: str | None,
) -> str | None:
    """Resolve a supported canonical wire; reject unknown protocols."""
    normalized = wire_api.strip().lower() if isinstance(wire_api, str) else wire_api
    if isinstance(normalized, str):
        if normalized in {"chat_completions", "responses"}:
            return normalized
        if normalized in {"", "auto"}:
            return "responses" if use_responses_api and not base_url else "chat_completions"
    elif normalized is None:
        return "responses" if use_responses_api and not base_url else "chat_completions"
    return None


def _plain_service_value(service: Any, *names: str) -> Any:
    """Read one already-applied scalar without reaching into a client."""
    for name in names:
        try:
            value = getattr(service, name)
        except (AttributeError, TypeError):
            continue
        if isinstance(value, (str, int, float, bool)):
            return value
    return None


def _model_is_path_like(value: str) -> bool:
    """Return whether a model value has an explicit filesystem-path shape."""
    candidate = value.strip()
    return (
        candidate.startswith(("~/", "~\\", "./", ".\\", "../", "..\\", "file://"))
        or PurePosixPath(candidate).is_absolute()
        or PureWindowsPath(candidate).is_absolute()
    )


def _vision_service_kind(provider: str, api_compat: str | None) -> str:
    """Name the concrete Vision service boundary selected by the resolver."""
    if provider in _CODEX_FAMILY:
        return "codex"
    if provider in {"mlx", "local", "anthropic", "gemini", "mimo", "openai"}:
        return provider
    if provider == "minimax" or api_compat == "anthropic":
        return "anthropic"
    return "openai"


def _vision_settings_snapshot(
    configuration: VisionConfiguration,
    provider: str | None,
    active_service: Any,
    vision_service: Any,
    local_settings: LocalVisionSettings | None,
    route_provenance: _VisionRouteProvenance,
) -> _VisionSettingsSnapshot:
    """Project the exact successful bind inputs without retaining secrets."""
    if vision_service is None or not isinstance(provider, str) or not provider.strip():
        raise SettingsError("vision route is unavailable")

    provider = provider.strip()
    provider_key = provider.lower()
    kwargs = dict(configuration.kwargs)
    active_name = getattr(active_service, "provider", "")
    active_key = active_name.lower() if isinstance(active_name, str) else ""
    same_provider = _same_provider_identity(provider_key, active_key)
    defaults = getattr(active_service, "_provider_defaults", None) if same_provider else None
    bucket = defaults.get(active_key) if isinstance(defaults, dict) else None
    bucket = bucket if isinstance(bucket, dict) else {}

    api_compat = route_provenance.api_compat
    service_kind = _vision_service_kind(provider_key, api_compat)
    active_model = _plain_service_value(active_service, "_model") if same_provider else None
    active_base_url = (
        _plain_service_value(active_service, "_base_url") if same_provider else None
    )

    if provider_key == "local":
        base_url = (
            kwargs.get("base_url")
            or (local_settings.base_url if local_settings is not None else None)
            or DEFAULT_LOCAL_BASE_URL
        )
        model = (
            kwargs.get("model")
            or (local_settings.model if local_settings is not None else None)
        )
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is None and local_settings is not None:
            max_tokens = local_settings.max_tokens
    else:
        base_url = (
            kwargs.get("base_url")
            or active_base_url
            or bucket.get("base_url")
            or _BASE_URL_DEFAULTS.get(provider_key)
        )
        model = kwargs.get("model") or active_model or bucket.get("model")
        max_tokens = kwargs.get("max_tokens")

    applied_model = _plain_service_value(
        vision_service, "_model_name", "_model", "model"
    )
    model = applied_model or model or _MODEL_DEFAULTS.get(provider_key)
    if not isinstance(model, str) or not model.strip():
        raise SettingsError("vision model is unavailable")
    model = model.strip()
    model_is_sensitive = _model_is_path_like(model)
    if model_is_sensitive:
        model = True

    applied_base_url = _plain_service_value(vision_service, "_base_url")
    if applied_base_url is not None:
        base_url = applied_base_url

    applied_max_tokens = _plain_service_value(vision_service, "_max_tokens")
    if applied_max_tokens is not None:
        max_tokens = applied_max_tokens
    if max_tokens is None:
        max_tokens = _MAX_TOKENS_DEFAULTS.get(provider_key)
        if max_tokens is None:
            max_tokens = _MAX_TOKENS_DEFAULTS.get(service_kind)

    applied_wire = _plain_service_value(vision_service, "_wire_api")
    if isinstance(applied_wire, str):
        wire_api = applied_wire
    elif service_kind == "codex":
        wire_api = "responses"
    elif service_kind == "mimo":
        wire_api = "chat_completions"
    elif service_kind == "openai" or provider_key == "local":
        wire_api = _effective_openai_wire(
            kwargs.get("wire_api") or bucket.get("wire_api"),
            use_responses_api=bucket.get("use_responses_api") is True,
            base_url=base_url,
        )
    else:
        wire_api = None

    token_manager = getattr(vision_service, "_token_manager", None)
    token_path = (
        kwargs.get("token_path")
        or bucket.get("codex_auth_path")
        or _plain_service_value(token_manager, "_path")
    )
    instructions = (
        _plain_service_value(vision_service, "_instructions")
        or kwargs.get("instructions")
    )
    max_output_tokens = _plain_service_value(
        vision_service, "_max_output_tokens"
    )
    if max_output_tokens is None:
        max_output_tokens = kwargs.get("max_output_tokens")
    timeout = _plain_service_value(vision_service, "_timeout")
    if timeout is None:
        timeout = kwargs.get("timeout")

    uses_api_key = service_kind in {"openai", "anthropic", "gemini", "mimo"}
    if provider_key == "local":
        uses_api_key = True
    uses_headers = service_kind in {"openai", "anthropic"}
    current = {
        "provider": provider,
        "base_url": bool(base_url) if service_kind not in {"gemini", "mlx"} else None,
        "model": model,
        "api_key": True if uses_api_key else None,
        "api_key_env": True if uses_api_key and configuration.api_key_env else None,
        "max_tokens": max_tokens if service_kind != "codex" else None,
        "api_compat": api_compat,
        "wire_api": wire_api,
        "default_headers": (
            True
            if uses_headers
            and bool(kwargs.get("default_headers") or bucket.get("default_headers"))
            else None
        ),
        "token_path": True if service_kind == "codex" and token_path else None,
        "instructions": True if service_kind == "codex" and instructions else None,
        "max_output_tokens": max_output_tokens if service_kind == "codex" else None,
        "timeout": timeout if service_kind == "codex" else None,
    }
    default_wire = None
    if service_kind == "codex":
        default_wire = "responses"
    elif service_kind in {"openai", "mimo"} or provider_key == "local":
        default_wire = "chat_completions"
    default = {
        "provider": None,
        "base_url": bool(_BASE_URL_DEFAULTS.get(provider_key)) or None,
        "model": _MODEL_DEFAULTS.get(provider_key),
        "api_key": True if provider_key == "local" else None,
        "api_key_env": None,
        "max_tokens": (
            None
            if service_kind == "codex"
            else _MAX_TOKENS_DEFAULTS.get(
                provider_key, _MAX_TOKENS_DEFAULTS.get(service_kind)
            )
        ),
        "api_compat": None,
        "wire_api": default_wire,
        "default_headers": None,
        "token_path": None,
        "instructions": True if service_kind == "codex" else None,
        "max_output_tokens": None,
        "timeout": 120.0 if service_kind == "codex" else None,
    }
    return _VisionSettingsSnapshot(
        current=tuple(current[key] for key in _VISION_SETTING_KEYS),
        default=tuple(default[key] for key in _VISION_SETTING_KEYS),
        sensitive=tuple(
            key in _VISION_SENSITIVE_SETTINGS
            or (key == "model" and model_is_sensitive)
            for key in _VISION_SETTING_KEYS
        ),
    )


def _vision_settings_provider(
    configuration: VisionConfiguration,
    provider: str | None,
    active_service: Any,
    vision_service: Any,
    local_settings: LocalVisionSettings | None,
    route_provenance: _VisionRouteProvenance,
    failure: Exception | None,
) -> SettingsProvider:
    """Bind one SHOW-only provider to the already-applied Vision route."""
    try:
        if failure is not None:
            raise failure
        snapshot = _vision_settings_snapshot(
            configuration,
            provider,
            active_service,
            vision_service,
            local_settings,
            route_provenance,
        )
    except Exception:
        return _unavailable_vision_settings

    def provide() -> tuple[SettingRow, ...]:
        return tuple(
            SettingRow(
                key=key,
                current=current,
                default=default,
                configurable=True,
                comment=f"vision-manual#setting-{key.replace('_', '-')}",
                _sensitive=sensitive,
            )
            for key, current, default, sensitive in zip(
                _VISION_SETTING_KEYS,
                snapshot.current,
                snapshot.default,
                snapshot.sensitive,
            )
        )

    return provide


def _unavailable_vision_settings() -> tuple[SettingRow, ...]:
    """Fail closed when no complete applied owner snapshot was bound."""
    raise RuntimeError("vision settings unavailable")


PROVIDERS = {
    "providers": [
        "gemini", "anthropic", "openai", "openrouter", "custom", "deepseek",
        "minimax", "mimo", "glm", "zhipu", "grok", "qwen", "kimi",
        "codex", "codex-pool", "codex_pool", "claude-p", "claude-code", "claude_code",
        "local",
    ],
    "default": None,
    "fallback_on_inherit": None,  # no agnostic fallback for vision
}

_ANALYZE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "image_path": {
            "type": "string",
            "description": "Image file path; relative paths use the workdir",
        },
        "question": {
            # Strict OpenAI object branches express an optional field as a
            # required nullable property. Null means absent, and the analyze
            # handler then applies the same default prompt it always has.
            "type": ["string", "null"],
            "description": "Image question; null uses the default prompt",
        },
        "preset": {
            "type": ["string", "null"],
            "description": "Optional manifest.preset.allowed route to borrow; null uses the default",
        },
    },
    "required": ["image_path", "question"],
    "additionalProperties": False,
}

def _unused(_input: Mapping[str, Any]) -> dict[str, Any]:
    raise AssertionError("the module-level schema-only ToolFamily never dispatches")


_CHECK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "preset": {
            "type": ["string", "null"],
            "description": "Optional manifest.preset.allowed route to check; null checks the default without an image",
        },
    },
    "required": ["preset"],
    "additionalProperties": False,
}

_LIST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class VisionConfiguration:
    """The capability setup input supplied through the configuration port.

    It contains exactly the public ``setup()`` arguments, never an Agent. The
    static declaration owns its validation and interpretation at bind time;
    keeping the value immutable makes a refresh bind from one coherent snapshot.
    The kernel ``ConfigurationPort`` carries a copied mapping (the same shape
    Shell earned); :meth:`port_values` and :meth:`from_port_values` are the
    only translation between that mapping and this typed snapshot.
    """

    vision_service: Any | None
    provider: str | None
    api_key: str | None
    api_key_env: str | None
    kwargs: Mapping[str, Any]

    _PORT_FIELDS = ("vision_service", "provider", "api_key", "api_key_env", "kwargs")

    def port_values(self) -> dict[str, Any]:
        """The mapping handed to the host's ``configuration`` port for this bind."""
        return {
            "vision_service": self.vision_service,
            "provider": self.provider,
            "api_key": self.api_key,
            "api_key_env": self.api_key_env,
            "kwargs": dict(self.kwargs),
        }

    @classmethod
    def from_port_values(cls, values: Any) -> "VisionConfiguration":
        """Rebuild the snapshot from the granted port; refuse any other shape."""
        if not isinstance(values, Mapping) or set(values) != set(cls._PORT_FIELDS):
            raise ToolPluginDeclarationError(
                "vision requires a VisionConfiguration snapshot supplied by "
                "capability setup through its configuration port"
            )
        kwargs = values["kwargs"]
        if not isinstance(kwargs, Mapping):
            raise ToolPluginDeclarationError(
                "vision configuration kwargs must be a mapping"
            )
        return cls(
            vision_service=values["vision_service"],
            provider=values["provider"],
            api_key=values["api_key"],
            api_key_env=values["api_key_env"],
            kwargs=dict(kwargs),
        )


_DESCRIPTION = (
    "Analyze an image on one explicit route. Use vision(action='analyze', "
    "input={'image_path': '...', 'question': null}, reasoning='...'); null "
    "question uses the default image prompt. Use check to verify a route, list "
    "to enumerate allowed routes, settings for the read-only applied snapshot, "
    "and manual for guidance. A non-null preset is an explicit "
    "manifest.preset.allowed borrow and uses that preset's own identity. "
    "Failures are sanitized; no provider, model, credential, preset, or MCP "
    "fallback is automatic."
)


def _build_family(
    analyze_handler: Any = _unused,
    check_handler: Any = _unused,
    list_handler: Any = _unused,
    manual_child: ChildTool | None = None,
    settings_provider: SettingsProvider = _unavailable_vision_settings,
) -> ToolFamily:
    """Build Vision's declared family from its one static declaration.

    The module-level schema-only family and each host-bound dispatcher derive
    their public name, operational schemas, and reserved manual slot from
    :data:`DECLARATION`. This prevents the advertised action inventory from
    drifting away from the declaration the kernel reserves.
    """
    return ToolFamily(
        DECLARATION.name,
        [
            ChildTool(
                action,
                DECLARATION.input_schemas[action],
                handler,
                title=f"{action} input",
            )
            for action, handler in (
                ("analyze", analyze_handler),
                ("check", check_handler),
                ("list", list_handler),
            )
        ]
        + [
            manual_child
            or ChildTool("manual", DECLARATION.manual_input_schema, _unused, title="manual input")
        ],
        settings_provider=settings_provider,
    )


def get_description(lang: str = "en") -> str:
    return _DESCRIPTION


def get_schema(lang: str = "en") -> dict:
    # Composed by generic ToolFamily infrastructure from the declaration-derived
    # schema-only family, never hand-assembled.
    return _FAMILY.build_schema()


class VisionManager:
    """Host-bound Vision dispatcher with no reference to the live Agent."""

    def __init__(
        self,
        workdir: "WorkdirPort",
        active_provider: "ActiveProviderPort",
        vision_service: "VisionService | None",
        manual_reason: str = "",
        settings_provider: SettingsProvider = _unavailable_vision_settings,
    ) -> None:
        self._workdir = workdir
        self._active_provider = active_provider
        self._vision_service = vision_service
        self._manual_reason = manual_reason
        # The declaration-derived child registry gets only this dispatcher's
        # handlers and the workdir-bound packaged manual child.
        self._family = _build_family(
            self._dispatch_analyze,
            self._dispatch_check,
            self._dispatch_list,
            build_manual_child(workdir, DECLARATION.manual),
            settings_provider,
        )

    def __call__(self, args: dict | None) -> dict:
        """Make the manager itself the registrar-published handler."""
        return self.handle(args)

    def _build_service_from_preset(self, preset_ref: str) -> tuple[Any, str]:
        """Borrow another preset's vision service for one call.

        The preset must appear in ``manifest.preset.allowed`` (same
        authorization surface as preset swapping). Its ``manifest.llm`` plus
        ``manifest.capabilities.vision`` provide the provider/model/credential
        identity; ``_resolve_direct_service`` is invoked with an identity shim
        built from that preset so the borrowed provider resolves its own route
        and credentials (e.g. a ``codex-pool`` preset selects its own OAuth
        pool identity) instead of inheriting the active provider's.

        Returns ``(VisionService | None, manual_reason, identity)`` where
        ``identity`` is a dict of the resolved provider/model identity (empty
        on failure).
        """
        import json as _json

        from lingtai.kernel.presets import load_preset, resolve_allowed_presets

        init_path = Path(self._workdir.path) / "init.json"
        if not init_path.is_file():
            return None, "No init.json is available to resolve manifest.preset.allowed.", {}
        try:
            init_data = _json.loads(init_path.read_text(encoding="utf-8"))
        except Exception:
            return None, "init.json could not be parsed while resolving preset.allowed.", {}
        manifest = init_data.get("manifest") or {}
        allowed_paths = {str(p) for p in resolve_allowed_presets(manifest, self._workdir.path)}
        raw_allowed = set(manifest.get("preset", {}).get("allowed") or [])
        resolved_ref = str(Path(preset_ref).expanduser())
        if preset_ref not in raw_allowed and resolved_ref not in allowed_paths:
            return None, (
                f"Preset {preset_ref!r} is not in manifest.preset.allowed; "
                "only authorized presets may be borrowed for vision."
            ), {}
        try:
            preset = load_preset(
                preset_ref,
                working_dir=self._workdir.path,
                # Read-only preset loading: no migration surface for a borrow.
                run_migrations=lambda _path: None,
            )
        except Exception as exc:
            return None, f"Failed to load preset {preset_ref!r}: {type(exc).__name__}.", {}
        pm = preset.get("manifest") or {}
        llm = pm.get("llm") or {}
        vision_cap = (pm.get("capabilities") or {}).get("vision") or {}
        provider = vision_cap.get("provider") or llm.get("provider")
        if not provider:
            return None, f"Preset {preset_ref!r} declares no vision provider.", {}
        identity = {
            "provider": llm.get("provider") or provider,
            "model": llm.get("model"),
            "base_url": llm.get("base_url"),
        }

        class _PresetIdentity:
            provider = llm.get("provider") or provider
            _model = llm.get("model")
            _base_url = llm.get("base_url")
            api_key = None
            _provider_defaults: dict = {}

        kwargs = dict(vision_cap)
        for key in ("model", "base_url", "api_key_env", "api_compat", "wire_api"):
            if key in llm and key not in kwargs:
                kwargs[key] = llm[key]
        api_key = kwargs.pop("api_key", None)
        api_key_env = kwargs.pop("api_key_env", None)
        # ``provider`` is passed positionally below; drop the capability copy so
        # ``_resolve_direct_service`` never receives it twice (TypeError).
        kwargs.pop("provider", None)
        service, service_reason, _route_provenance = _resolve_direct_service(
            self._workdir,
            self._active_provider,
            provider,
            api_key=api_key,
            api_key_env=api_key_env,
            identity_service=_PresetIdentity(),
            **kwargs,
        )
        return service, service_reason, identity

    def _dispatch_analyze(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        """Run the one direct analyze operation on already-validated input.

        The body is the pre-migration ``handle()`` analyze path unchanged:
        same missing-service guard, relative-path resolution, existence check,
        default prompt, and success/failure result shapes. When the call
        carries the optional ``preset`` option, a borrowed service is built for
        this call from that preset's vision configuration instead of the
        default route.
        """
        preset_ref = action_input.get("preset") if isinstance(action_input, Mapping) else None
        if preset_ref:
            borrowed, borrow_reason, _identity = self._build_service_from_preset(preset_ref)
            if borrowed is None:
                return {
                    "status": "error",
                    "message": (
                        f"{borrow_reason} Load the vision manual skill for the "
                        "borrowing steps: vision(action='manual', input={}, "
                        "reasoning='preset vision unavailable, load the setup "
                        "steps'); then ask the human for consent before "
                        "changing preset authorization."
                    ),
                }
            service = borrowed
        else:
            service = self._vision_service
        if service is None:
            reason = self._manual_reason or (
                "Direct vision is unavailable; call vision(action='manual', "
                "input={}, reasoning='no direct vision route, load the "
                "manual alternatives')."
            )
            return {
                "status": "error",
                "message": f"{reason} {_consent_guidance()}",
            }
        image_path = action_input.get("image_path") or ""
        question = action_input.get("question")
        if question is None:
            question = "Describe what you see in this image."

        if not image_path:
            return {"status": "error", "message": "Provide image_path"}

        path = Path(image_path)
        if not path.is_absolute():
            path = self._workdir.path / path

        if not path.is_file():
            return {"status": "error", "message": f"Image file not found: {path}"}

        try:
            analysis = service.analyze_image(str(path), prompt=question)
            if not analysis:
                return {
                    "status": "error",
                    "message": "Vision analysis returned no response.",
                }
            return {"status": "ok", "analysis": analysis}
        except Exception as e:
            if preset_ref:
                route = "borrowed preset vision route"
                hint = (
                    "The borrowed preset's vision service failed for this "
                    "image; verify the preset is authorized and its provider "
                    "supports images."
                )
            else:
                route = "default vision route"
                hint = (
                    "The default route is the current provider's "
                    "Responses-API vision, which may not support images."
                )
            return {
                "status": "error",
                "message": (
                    f"Vision analysis failed on the {route} ({type(e).__name__}). "
                    f"{hint} Alternative vision may be available: the current "
                    "provider's MCP, a borrowed preset via the analyze "
                    "preset option, or a local OpenAI-compatible vision "
                    "server via provider='local'. Load the vision manual skill "
                    "for the setup alternatives: vision(action='manual', "
                    "input={}, reasoning='vision failed, load the setup "
                    "alternatives'); then ask the human for consent "
                    "before setting one up."
                ),
            }

    def _dispatch_check(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve which vision route actually works without sending an image.

        With the optional ``preset`` field, this borrows that preset's vision
        service the same way ``analyze`` would (authorization check, preset
        load, provider identity) and reports the resolved provider/model; the
        service is constructed but no image request is made, so it never costs
        a provider call. Without ``preset``, it reports whether the default
        route (configured service or the active LLM's own Responses API) is
        available. A failure returns a sanitized error pointing at the manual.
        """
        preset_ref = action_input.get("preset") if isinstance(action_input, Mapping) else None
        if preset_ref:
            borrowed, borrow_reason, identity = self._build_service_from_preset(preset_ref)
            if borrowed is None:
                return {
                    "status": "error",
                    "message": (
                        f"{borrow_reason} Load the vision manual skill for the "
                        "borrowing steps: vision(action='manual', input={}, "
                        "reasoning='preset vision unavailable, load the setup "
                        "steps'); then ask the human for consent before "
                        "changing preset authorization."
                    ),
                }
            return {
                "status": "ok",
                "route": f"preset:{preset_ref}",
                "provider": identity.get("provider"),
                "model": identity.get("model"),
            }
        if self._vision_service is None:
            reason = self._manual_reason or (
                "Direct vision is unavailable; call vision(action='manual', "
                "input={}, reasoning='no direct vision route, load the "
                "manual alternatives')."
            )
            return {
                "status": "error",
                "message": f"{reason} {_consent_guidance()}",
            }
        active_service = self._active_provider.service
        return {
            "status": "ok",
            "route": "default",
            "provider": getattr(active_service, "provider", None),
            "model": getattr(active_service, "model", None),
        }

    def _dispatch_list(self, action_input: Mapping[str, Any]) -> dict[str, Any]:
        # mechanical enumeration; never constructs a service or makes a provider call
        import json as _json
        from lingtai.kernel.presets import load_preset

        active_service = self._active_provider.service
        active_provider = getattr(active_service, "provider", None)
        active_model = getattr(active_service, "_model", None)
        default_endpoint = _vision_endpoint(active_provider)
        default = {
            "provider": active_provider,
            "model": active_model,
            "configured": self._vision_service is not None,
            "supports_vision": bool(self._vision_service is not None or default_endpoint != "unknown"),
            "endpoint": default_endpoint,
            "responses_vision": _responses_vision(active_provider),
        }
        allowed: list[str] = []
        init_path = Path(self._workdir.path) / "init.json"
        if init_path.is_file():
            try:
                init_data = _json.loads(init_path.read_text(encoding="utf-8"))
                manifest = init_data.get("manifest") or {}
                # One row per physical preset. A raw ``~/x.json`` entry and
                # its expanded absolute path are the same file, so key on the
                # canonical path and keep the first declared spelling, which
                # is exactly what ``analyze``/``check`` accept as ``preset``.
                by_path: dict[str, str] = {}
                for ref in manifest.get("preset", {}).get("allowed") or []:
                    if isinstance(ref, str) and ref:
                        by_path.setdefault(_canonical_preset_path(ref, self._workdir.path), ref)
                allowed = sorted(by_path.values())
            except Exception:
                allowed = []
        presets: list[dict[str, Any]] = []
        for ref in allowed:
            try:
                preset = load_preset(ref, working_dir=self._workdir.path, run_migrations=lambda _path: None)
            except Exception:
                continue
            pm = preset.get("manifest") or {}
            llm = pm.get("llm") or {}
            vision_cap = (pm.get("capabilities") or {}).get("vision") or {}
            provider = vision_cap.get("provider") or llm.get("provider")
            if not provider:
                continue
            presets.append({
                "preset": ref,
                "provider": provider,
                "model": vision_cap.get("model") or llm.get("model"),
                "endpoint": _vision_endpoint(provider),
                "responses_vision": _responses_vision(provider),
            })
        return {"status": "ok", "default": default, "presets": presets, "count": len(presets)}

    def _adapt_manual_result(self, mcp_result: dict[str, Any]) -> dict[str, Any]:
        # Host-owned flattening of the manual child's canonical result into
        # vision's pre-migration ``status``/``action``/``manual`` shape (plus
        # the loader's ``manual_path``). See ``handle()`` for the ordering rule.
        flat: dict[str, Any] = {
            "status": mcp_result.get("status", "ok"),
            "action": "manual",
            "manual": mcp_result["content"][0]["text"],
            "manual_path": mcp_result["structuredContent"]["manual_path"],
        }
        if "error" in mcp_result:
            flat["error"] = mcp_result["error"]
        return flat

    def manual(self) -> dict:
        """Return only installed guidance; never inspect config or invoke a backend.

        Retained as the family's own public manual entry point (callers and
        tests use it directly). Performs no provider construction, no
        credential read, and no analyze operation.
        """
        return self._adapt_manual_result(self._family.handle({"action": "manual", "input": {}}))

    def handle(self, args: dict | None) -> dict:
        # Canonical statement of this family's dispatch/presentation ordering.
        # The generic ``ToolFamily`` dispatcher validates ``action``,
        # type-checks and strips root ``summarize``, rejects unknown root
        # fields, and rejects ``input`` keys outside the selected action's own
        # declared schema (schema conformance alone is not the dispatch-time
        # authorization boundary — see ``tools/CONTRACT.md`` "Dispatch and
        # actions") before ``_dispatch_analyze`` or the registered ``manual``
        # child's handler ever runs, so every envelope failure lands before any
        # provider I/O. ``self._family.handle`` returns the ``manual`` child's
        # canonical ``content``/``structuredContent`` result verbatim (no double
        # wrap); flattening it to vision's public shape is this method's own
        # Host job, done strictly after dispatch, never inside a registered
        # child. Envelope failures are normalized to vision's long-standing
        # ``{"status": "error", "message": ...}`` shape here, rather than by
        # changing the generic dispatcher's canonical error result.
        action = args.get("action") if isinstance(args, Mapping) else None
        result = self._family.handle(args)
        if action == "manual" and "content" in result:
            return self._adapt_manual_result(result)
        if (
            action != "settings"
            and result.get("status") == "failed"
            and "error_code" in result
        ):
            return {"status": "error", "message": result["message"]}
        return result



def _bind(host: "ToolPluginHost") -> BoundToolPlugin:
    """Compose Vision against only its granted ports; mount nothing.

    Provider resolution retains the previous active-provider behavior, but all
    Agent reads flow through ``workdir`` and ``active_provider``. Explicit
    capability kwargs arrive as one opaque configuration port rather than by
    reaching through the Agent. Construction creates no transport, process, or
    prompt side effect; the kernel registrar alone activates and mounts.
    """
    configuration = VisionConfiguration.from_port_values(host.configuration.values)
    vision_service = configuration.vision_service
    provider = configuration.provider
    manual_reason = ""
    active_service = host.active_provider.service if vision_service is None else None
    if vision_service is None and provider is None:
        active_name = getattr(active_service, "provider", "")
        if isinstance(active_name, str) and active_name.strip():
            provider = active_name

    local_settings: LocalVisionSettings | None = None
    settings_failure: Exception | None = None
    resolved_api_key = configuration.api_key
    resolved_route = vision_service is None
    route_provenance = _VisionRouteProvenance()
    if resolved_route and configuration.api_key_env:
        from lingtai.kernel.config_resolve import resolve_env

        resolved_api_key = resolve_env(
            configuration.api_key, configuration.api_key_env
        )
    if resolved_route and isinstance(provider, str) and provider.lower() == "local":
        from .settings import read_local_settings

        try:
            local_settings = read_local_settings(host.workdir)
        except SettingsError as exc:
            settings_failure = exc
    if vision_service is None and provider is not None:
        vision_service, manual_reason, route_provenance = _resolve_direct_service(
            host.workdir,
            host.active_provider,
            provider,
            api_key=resolved_api_key,
            identity_service=active_service,
            local_settings=local_settings,
            local_settings_error=(
                settings_failure if isinstance(settings_failure, SettingsError) else None
            ),
            **dict(configuration.kwargs),
        )
    elif vision_service is None:
        manual_reason = (
            "No direct vision provider was configured; use vision(action='manual', "
            "input={}, reasoning='no direct vision provider is configured')."
        )
    manager = VisionManager(
        host.workdir,
        host.active_provider,
        vision_service=vision_service,
        manual_reason=manual_reason,
        settings_provider=_vision_settings_provider(
            configuration,
            provider if resolved_route else None,
            active_service,
            vision_service,
            local_settings,
            route_provenance,
            settings_failure,
        ),
    )
    return BoundToolPlugin(
        name=DECLARATION.name,
        schema=get_schema(),
        handler=manager,
        description=DECLARATION.description,
        glossary_package=DECLARATION.glossary_package,
    )


#: Static official declaration. The schema-only family below and every bound
#: manager derive identity, action schemas, and installed manual destination
#: from this one object; the kernel verifies their advertised actions at bind.
DECLARATION = ToolPluginDeclaration(
    name="vision",
    actions=("analyze", "check", "list"),
    input_schemas={
        "analyze": _ANALYZE_INPUT_SCHEMA,
        "check": _CHECK_INPUT_SCHEMA,
        "list": _LIST_INPUT_SCHEMA,
    },
    manual_input_schema=MANUAL_INPUT_SCHEMA,
    manual="vision",
    description=_DESCRIPTION,
    binder=_bind,
    requires=("workdir", "active_provider", "configuration"),
    glossary_package=__package__,
    settings=True,
)


#: Import-time schema-only composition catches a malformed fixed child registry
#: before an Agent exists. Runtime binding builds the same declaration-derived
#: family with real handlers and its installed manual child.
_FAMILY = _build_family()


def _resolve_direct_service(
    workdir: "WorkdirPort",
    active_provider: "ActiveProviderPort",
    provider: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    *,
    identity_service: Any = None,
    local_settings: LocalVisionSettings | None = None,
    local_settings_error: SettingsError | None = None,
    **kwargs: Any,
) -> tuple["VisionService | None", str, _VisionRouteProvenance]:
    """Resolve a direct VisionService from provider + kwargs.

    ``identity_service`` overrides the active-provider port's service used for
    provider identity, model/base_url inheritance, and the Codex pool bucket.
    Preset borrowing passes a lightweight identity shim built from the borrowed
    preset's ``manifest.llm`` so the borrowed provider (e.g. ``codex-pool``)
    resolves its own route and credentials instead of the active provider's.
    """
    vision_service: "VisionService | None" = None
    manual_reason = ""
    applied_api_compat: str | None = None
    if api_key_env:
        from lingtai.kernel.config_resolve import resolve_env
        api_key = resolve_env(api_key, api_key_env)
    provider_key = provider.lower()
    active_service = identity_service if identity_service is not None else active_provider.service
    active_provider = getattr(active_service, "provider", "")
    active_provider_key = active_provider.lower() if isinstance(active_provider, str) else ""
    same_provider = _same_provider_identity(provider_key, active_provider_key)
    active_model = getattr(active_service, "_model", None) if same_provider else None
    active_base_url = getattr(active_service, "_base_url", None) if same_provider else None
    active_api_key = getattr(active_service, "api_key", None) if same_provider else None
    if provider_key == "mlx":
        # Native Apple-MLX on-device vision is an explicit pseudo-provider:
        # keep it out of PROVIDERS/check-caps, but preserve the documented
        # opt-in route. Its constructor accepts only model/max_tokens and
        # needs no key.
        mlx_kwargs = {
            key: kwargs[key]
            for key in ("model", "max_tokens")
            if key in kwargs and kwargs[key] is not None
        }
        from lingtai.services.vision import create_vision_service
        try:
            vision_service = create_vision_service(
                "mlx",
                api_key=None,
                **mlx_kwargs,
            )
        except Exception as exc:
            manual_reason = _setup_failure(provider, exc)
    elif provider_key == "local":
        # Local is a generic OpenAI-compatible vision server on this
        # machine (Ollama, LM Studio, vLLM, llama.cpp, ...). The endpoint
        # is operator-owned and configured in settings/vision.json
        # (base_url/model/api_key/max_tokens); capability kwargs override
        # the file. base_url defaults to the standard local port. model is
        # REQUIRED — no hardcoded default, because a silently assumed
        # model masks misconfiguration; when it is missing we surface
        # guided setup steps instead. api_key is optional: local servers
        # ignore it, so a placeholder satisfies the OpenAI SDK.
        from lingtai.services.vision.openai import OpenAIVisionService
        from .settings import read_local_settings
        try:
            if local_settings_error is not None:
                raise local_settings_error
            if local_settings is None:
                local_settings = read_local_settings(workdir)
        except SettingsError as exc:
            manual_reason = (
                f"Local vision settings are invalid: {exc}; fix "
                "settings/vision.json or pass provider='local' with "
                "base_url/model kwargs; see vision(action='manual', input={}, "
                "reasoning='local vision settings are invalid')."
            )
        else:
            local_base_url = (
                kwargs.get("base_url")
                or local_settings.base_url
                or DEFAULT_LOCAL_BASE_URL
            )
            local_model = kwargs.get("model") or local_settings.model
            if not local_model:
                manual_reason = (
                    "Local vision needs an explicit model. Load the vision "
                    "manual skill: vision(action='manual', input={}, "
                    "reasoning='local vision setup'); then ask the human "
                    "for consent before setting it up."
                )
            else:
                local_key = api_key or local_settings.api_key or "local"
                local_wire = _effective_openai_wire(
                    kwargs.get("wire_api"),
                    use_responses_api=False,
                    base_url=local_base_url,
                )
                svc_kwargs: dict = {
                    "api_key": local_key,
                    "model": local_model,
                    "base_url": local_base_url,
                }
                if local_wire and local_wire != "auto":
                    svc_kwargs["wire_api"] = local_wire
                cap_max_tokens = kwargs.get("max_tokens")
                if cap_max_tokens is None:
                    cap_max_tokens = local_settings.max_tokens
                if cap_max_tokens is not None:
                    svc_kwargs["max_tokens"] = cap_max_tokens
                try:
                    vision_service = OpenAIVisionService(**svc_kwargs)
                except Exception as exc:
                    manual_reason = _setup_failure(provider, exc)
    elif provider_key not in PROVIDERS["providers"]:
        # No dedicated VisionService for this provider (custom relay,
        # OpenRouter, an anthropic-compat local proxy, ...). Route vision
        # through the OpenAI- or Anthropic-compatible service, picking the
        # wire protocol and endpoint from, in order:
        #   1. capability kwargs — explicit init.json override. This lets a
        #      user point vision at a *different*, vision-capable model
        #      (e.g. Kimi-K2.6 on a multi-model proxy) while the main LLM
        #      stays on a text-only model (e.g. GLM-5.1).
        #   2. the main LLM: api_compat from service._provider_defaults
        #      (shaped {provider_name: defaults_dict}), base_url/model from
        #      service._base_url / service._model.
        # If the relay or model can't actually do vision, the call fails at
        # runtime — capability registration never pre-checks.
        bucket = {}
        api_compat = (kwargs.get("api_compat") or "").lower()
        if not api_compat:
            defaults = getattr(active_service, "_provider_defaults", None) if same_provider else None
            if isinstance(defaults, dict):
                # _provider_defaults is dict[provider_name, defaults_dict];
                # read only the active provider's bucket, never another
                # provider's credential/transport configuration.
                bucket = defaults.get(active_provider_key)
                if isinstance(bucket, dict):
                    api_compat = (bucket.get("api_compat") or "").lower()

        cap_model = kwargs.get("model")
        cap_base_url = kwargs.get("base_url")
        cap_max_tokens = kwargs.get("max_tokens")
        bucket = bucket if isinstance(bucket, dict) else {}
        llm_base_url = cap_base_url or active_base_url or bucket.get("base_url")
        llm_model = cap_model or active_model or bucket.get("model")
        api_key = api_key or active_api_key
        headers = kwargs.get("default_headers") or bucket.get("default_headers")
        wire_api = _effective_openai_wire(
            kwargs.get("wire_api") or bucket.get("wire_api"),
            use_responses_api=bucket.get("use_responses_api") is True,
            base_url=llm_base_url,
        )

        if api_compat == "openai":
            applied_api_compat = "openai"
            from lingtai.services.vision.openai import OpenAIVisionService
            svc_kwargs: dict = {
                "api_key": api_key,
                "model": llm_model,
                "base_url": llm_base_url,
            }
            if headers:
                svc_kwargs["default_headers"] = headers
            if wire_api and wire_api != "auto":
                svc_kwargs["wire_api"] = wire_api
            if cap_max_tokens is not None:
                svc_kwargs["max_tokens"] = cap_max_tokens
            if wire_api is None:
                manual_reason = "The active OpenAI-compatible wire is not implemented by the direct vision service; use vision(action='manual', input={}, reasoning='the active OpenAI-compatible wire has no direct vision route')."
            elif not llm_model:
                manual_reason = f"Provider {provider!r} has no resolved current model for direct vision; use vision(action='manual', input={{}}, reasoning='no resolved current model for direct vision')."
            elif not api_key:
                manual_reason = f"Provider {provider!r} has no resolved current credential for direct vision; use vision(action='manual', input={{}}, reasoning='no resolved current credential for direct vision')."
            else:
                try:
                    vision_service = OpenAIVisionService(**svc_kwargs)
                except Exception as exc:
                    manual_reason = _setup_failure(provider, exc)
        elif api_compat == "anthropic":
            applied_api_compat = "anthropic"
            from lingtai.services.vision.anthropic import AnthropicVisionService
            svc_kwargs = {
                "api_key": api_key,
                "model": llm_model,
                "base_url": llm_base_url,
            }
            if headers:
                svc_kwargs["default_headers"] = headers
            if cap_max_tokens is not None:
                svc_kwargs["max_tokens"] = cap_max_tokens
            if not llm_model:
                manual_reason = f"Provider {provider!r} has no resolved current model for direct vision; use vision(action='manual', input={{}}, reasoning='no resolved current model for direct vision')."
            elif not api_key:
                manual_reason = f"Provider {provider!r} has no resolved current credential for direct vision; use vision(action='manual', input={{}}, reasoning='no resolved current credential for direct vision')."
            else:
                try:
                    vision_service = AnthropicVisionService(**svc_kwargs)
                except Exception as exc:
                    manual_reason = _setup_failure(provider, exc)
        else:
            manual_reason = f"No direct vision route is supported for provider {provider!r}; use vision(action='manual', input={{}}, reasoning='this provider has no supported direct vision route')."
    else:
        if provider_key in _CLAUDE_CLI_FAMILY:
            # The claude backend uses the Claude Code CLI for vision. LingTai
            # does not proxy the CLI's own authentication (claude.ai
            # subscription, API key, configured provider), so there is no
            # direct service to construct: the agent is told to run
            # ``claude -p`` and read the vision manual for the exact steps.
            manual_reason = (
                "You are using claude as backend, therefore to use vision run "
                "`claude -p`; see the vision manual for more details: "
                "vision(action='manual', input={}, reasoning='claude vision "
                "details')."
            )
        elif provider_key in _CODEX_FAMILY:
            # Codex vision is a standalone Responses request. It may share
            # the active Codex family's model and endpoint, but never
            # inherits those from an unrelated main provider. The fixed/
            # direct vs weighted/pool credential route is *not* chosen from
            # provider spelling: it follows the active provider-default
            # bucket exactly as the canonical Codex factory does — direct
            # iff the bucket carries a nonblank trimmed ``codex_auth_path``,
            # otherwise pool (see ``lingtai/llm/_register.py``).
            if same_provider:
                if active_model:
                    kwargs.setdefault("model", active_model)
                if active_base_url:
                    kwargs.setdefault("base_url", active_base_url)
            codex_base_url = kwargs.get("base_url")

            defaults = getattr(active_service, "_provider_defaults", None) if same_provider else None
            bucket = defaults.get(active_provider_key) if isinstance(defaults, dict) else None
            if not isinstance(bucket, dict):
                bucket = {}
            # Bucket-driven route: the active Codex service is direct iff its
            # bucket configures a nonblank ``codex_auth_path``, else pool. An
            # unrelated active provider carries an empty bucket → ``"pool"``,
            # and its pool branch stays gated by ``same_provider`` below, so it
            # never reads a default pool and still fails closed.
            codex_route = _codex_bucket_route(bucket)
            # Normalize an explicit capability identity on every route. This
            # preserves a valid independent token path while ensuring a
            # whitespace-only value cannot bypass either fail-closed branch.
            explicit_token_path = _normalize_codex_auth_path(kwargs.pop("token_path", None))
            if explicit_token_path:
                kwargs["token_path"] = explicit_token_path
            if not kwargs.get("model"):
                manual_reason = f"Provider {provider!r} has no resolved current model for direct vision; use vision(action='manual', input={{}}, reasoning='no resolved current model for direct vision')."
            elif codex_route == "direct":
                # A whitespace-only explicit ``token_path`` is not an identity;
                # normalize both it and the inherited bucket path once so the
                # trimmed value drives ``token_path`` exactly like the factory.
                token_path = (
                    explicit_token_path
                    or _normalize_codex_auth_path(bucket.get("codex_auth_path"))
                )
                if token_path:
                    kwargs["token_path"] = token_path
                else:
                    manual_reason = "Codex vision has no explicit current OAuth identity; use vision(action='manual', input={}, reasoning='Codex vision has no explicit current OAuth identity')."
            else:
                # Pool route (bucket has no nonblank ``codex_auth_path``).
                # WeightedAccountSource selects an account from the pool file
                # (thin-wrapper spec v3).  Reads only the non-secret pool;
                # Codex core owns token refresh, quota, retry, and transport.
                # Only an active Codex-family service (``same_provider``) may
                # supply a pool identity; an unrelated active provider never
                # runs the selector and falls through to fail-closed below,
                # so no unrelated/default pool file is read on its behalf.
                if same_provider:
                    from lingtai.auth.codex_pool import (
                        resolve_codex_pool_path,
                        resolve_codex_tui_dir,
                    )
                    from lingtai.auth.codex_account_source import (
                        WeightedAccountSource,
                        NoCandidateError,
                    )
                    tui_dir = resolve_codex_tui_dir()
                    pool_path = resolve_codex_pool_path(bucket)
                    source = WeightedAccountSource(
                        pool_path, tui_dir, model=kwargs.get("model"),
                    )
                    try:
                        candidate = source.select()
                        kwargs["token_path"] = candidate.auth_ref
                    except NoCandidateError:
                        pass
                if not kwargs.get("token_path"):
                    manual_reason = "Codex pool vision has no selected current OAuth identity; use vision(action='manual', input={}, reasoning='Codex pool vision has no selected current OAuth identity')."
            kwargs.pop("api_compat", None)
            kwargs.pop("base_url", None)
            if codex_base_url:
                kwargs["base_url"] = codex_base_url
            if not manual_reason:
                from lingtai.services.vision import create_vision_service
                try:
                    vision_service = create_vision_service("codex", api_key=None, **kwargs)
                except Exception as exc:
                    manual_reason = _setup_failure(provider, exc)
        else:
            service_provider = provider_key
            defaults = getattr(active_service, "_provider_defaults", {}) if same_provider else {}
            bucket = defaults.get(active_provider_key, {}) if isinstance(defaults, dict) else {}
            active_base_url = active_base_url or (bucket.get("base_url") if isinstance(bucket, dict) else None)
            active_headers = bucket.get("default_headers") if isinstance(bucket, dict) else None
            active_compat = kwargs.get("api_compat") or (bucket.get("api_compat") if isinstance(bucket, dict) else "") or ""
            wire_api = _effective_openai_wire(
                kwargs.get("wire_api") or (bucket.get("wire_api") if isinstance(bucket, dict) else None),
                use_responses_api=isinstance(bucket, dict) and bucket.get("use_responses_api") is True,
                base_url=kwargs.get("base_url") or active_base_url,
            )
            if service_provider in {
                "openrouter", "custom", "deepseek", "zhipu", "glm", "grok",
                "qwen", "kimi",
            }:
                service_provider = "anthropic" if active_compat.lower() == "anthropic" else "openai"
                if active_compat.lower() in {"openai", "anthropic"}:
                    applied_api_compat = active_compat.lower()

            # Provider-specific kwarg injection. Each branch is opt-in because
            # vision services have heterogeneous constructor signatures.
            if service_provider == "minimax":
                service_provider = "anthropic"
            if service_provider in {"openai", "anthropic", "gemini", "mimo"}:
                if same_provider and active_model:
                    kwargs.setdefault("model", active_model)
                if (
                    service_provider in {"openai", "anthropic"}
                    and same_provider
                    and active_base_url
                ):
                    kwargs.setdefault("base_url", active_base_url)
            if service_provider == "mimo" and same_provider and active_base_url:
                kwargs.setdefault("base_url", active_base_url)
            if service_provider in {"openai", "mimo"} and wire_api is None:
                manual_reason = "The active OpenAI-compatible wire is not implemented by the direct vision service; use vision(action='manual', input={}, reasoning='the active OpenAI-compatible wire has no direct vision route')."
            elif service_provider == "mimo" and wire_api != "chat_completions":
                manual_reason = "The active MiMo wire is not implemented by the direct vision service; use vision(action='manual', input={}, reasoning='the active MiMo wire has no direct vision route')."
            if service_provider in {"openai", "mimo"} and active_compat == "anthropic":
                manual_reason = "The active preset uses an Anthropic wire that this vision route cannot safely adapt; use vision(action='manual', input={}, reasoning='the active Anthropic wire cannot be safely adapted for direct vision')."
                vision_service = None
            if service_provider == "anthropic" and active_headers:
                kwargs.setdefault("default_headers", active_headers)
            elif service_provider == "openai":
                if active_headers:
                    kwargs.setdefault("default_headers", active_headers)
                if wire_api not in (None, "auto"):
                    kwargs.setdefault("wire_api", wire_api)
            elif service_provider == "mimo":
                # MiMo's standalone constructor intentionally accepts only
                # api_key/model/base_url/max_tokens. Its current direct
                # route is Chat Completions; other wires stay manual-only.
                kwargs.pop("default_headers", None)
                kwargs.pop("wire_api", None)
            resolved_api_key = api_key or active_api_key
            if service_provider not in {"codex", "local"} and not kwargs.get("model"):
                manual_reason = f"Provider {provider!r} has no resolved current model for direct vision; use vision(action='manual', input={{}}, reasoning='no resolved current model for direct vision')."
            elif service_provider not in {"codex", "local"} and not resolved_api_key:
                manual_reason = f"Provider {provider!r} has no resolved current credential for direct vision; use vision(action='manual', input={{}}, reasoning='no resolved current credential for direct vision')."
            # Dedicated vision services do not consume the LLM adapter's
            # transport selector.
            kwargs.pop("api_compat", None)
            if service_provider not in {"openai", "anthropic", "mimo"}:
                kwargs.pop("base_url", None)
            # Lazy import: the provider service lives in ``lingtai.services``.
            from lingtai.services.vision import create_vision_service
            if vision_service is None and not manual_reason:
                try:
                    vision_service = create_vision_service(
                        service_provider,
                        api_key=resolved_api_key,
                        **kwargs,
                    )
                except Exception as exc:
                    manual_reason = _setup_failure(provider, exc)
    return (
        vision_service,
        manual_reason,
        _VisionRouteProvenance(api_compat=applied_api_compat),
    )


def setup(
    agent: "BaseAgent",
    vision_service: "VisionService | None" = None,
    provider: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    **kwargs: Any,
) -> VisionManager:
    """Register Vision through its declared host-plugin route.

    ``vision`` remains always registered. Its public capability kwargs are
    carried as a configuration port; the binder resolves the default active
    provider through its one narrow read port, then the registrar mounts the
    resulting handler under the kernel-reserved ``vision`` name. No generic
    ``Agent.add_tool`` path is available to this official family.
    """
    from lingtai.adapters.tool_plugin_host import (
        StaticConfigurationAdapter,
        register_agent_tool_plugins,
    )

    configuration = StaticConfigurationAdapter(
        VisionConfiguration(
            vision_service=vision_service,
            provider=provider,
            api_key=api_key,
            api_key_env=api_key_env,
            kwargs=dict(kwargs),
        ).port_values()
    )
    (bound,) = register_agent_tool_plugins(
        agent,
        [DECLARATION],
        # The snapshot is granted to this declaration alone, through the same
        # setup-selected seam Shell uses; it is never added to the standard
        # table for every family.
        extra_ports_for=lambda declaration: (
            {"configuration": configuration} if declaration is DECLARATION else {}
        ),
    )
    if not isinstance(bound.handler, VisionManager):  # pragma: no cover - declaration invariant
        raise ToolPluginDeclarationError("vision declaration bound a non-Vision handler")
    return bound.handler
