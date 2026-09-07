"""LingTai WhatsApp MCP server (personal-account mode).

Exposes exactly one public LTP-v2 family, ``whatsapp``. Actions dispatch to
``WhatsAppManager`` behind the ``handle_whatsapp`` validation boundary, the
same shape ``telegram``/``feishu`` use. Inbound messages flow into the host
agent's inbox via LICC.

Configuration:
    LINGTAI_WHATSAPP_CONFIG — path to a JSON config file (optional; defaults
    are usable for a first run).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

from .._results import json_tool_result as _tool_result
from .._results import text_resource_result as _resource_result
from .._results import unknown_resource_error as _unknown_resource
from .._results import unknown_tool_error as _unknown_tool
from ._family import handle_whatsapp
from .manager import WhatsAppManager, SCHEMA, DESCRIPTION
from .plugin import WHATSAPP_PLUGIN
from .resources import resource_text

log = logging.getLogger("lingtai.mcp_servers.whatsapp")

_SERVER_INSTRUCTIONS = (
    "lingtai-whatsapp: one personal-account WhatsApp Web session via a local, "
    "unofficial whatsapp-web.js bridge (not the Meta Cloud API). Configure via "
    "LINGTAI_WHATSAPP_CONFIG and pair with the QR returned by get_qr (phone: "
    "WhatsApp Settings -> Linked Devices). send/reply/react are real external "
    "side effects; verify targets and call action='manual' for detailed safety, "
    "media, settings, and lifecycle guidance."
)

_PROFILE_MIME = "application/json"
_MARKDOWN_SKILL_MIME = "text/markdown; profile=lingtai-skill"
_MARKDOWN_MIME = "text/markdown"
_JSON_MIME = "application/json"
_HTML_MIME = "text/html"

# Mirrors ``resources.manifest()["resources"]`` one-for-one; ``resource_text``
# must be able to resolve every URI listed here.
_RESOURCE_INDEX = [
    {
        "uri": "lingtai://manifest",
        "name": "LingTai MCP profile manifest",
        "mimeType": _PROFILE_MIME,
        "description": "Machine-readable LingTai profile for this WhatsApp MCP server.",
    },
    {
        "uri": "lingtai://skills/whatsapp",
        "name": "WhatsApp pointer skill",
        "mimeType": _MARKDOWN_SKILL_MIME,
        "description": "Thin agent-facing routing hint for WhatsApp MCP usage.",
    },
    {
        "uri": "lingtai://docs/configuration",
        "name": "WhatsApp configuration guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Config fields for the local whatsapp-web.js bridge and message store.",
    },
    {
        "uri": "lingtai://docs/troubleshooting",
        "name": "WhatsApp troubleshooting guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "Pairing, bridge install, and runtime failure diagnostics.",
    },
    {
        "uri": "lingtai://status",
        "name": "WhatsApp safe status",
        "mimeType": _JSON_MIME,
        "description": "Redacted runtime status derived from the bridge and manager state.",
    },
    {
        "uri": "lingtai://onboarding/whatsapp",
        "name": "WhatsApp onboarding guide",
        "mimeType": _MARKDOWN_MIME,
        "description": "QR-code pairing walkthrough for linking a personal WhatsApp account.",
    },
    {
        "uri": "lingtai://onboarding/html-template",
        "name": "WhatsApp onboarding HTML template",
        "mimeType": _HTML_MIME,
        "description": "Self-contained, secret-free static HTML setup page with a {{SETUP}} placeholder.",
    },
]


def _canonical_resource_uri(uri: object) -> str:
    return str(uri).rstrip("/")


def load_config() -> tuple[dict[str, Any], Path]:
    from .manager import load_config as _load
    return _load()


def build_manager(config: dict[str, Any] | None = None) -> WhatsAppManager:
    config_path: Path | None = None
    if config is None:
        try:
            loaded_config, config_path = load_config()
            config = loaded_config or {}
        except ValueError:
            if os.environ.get("LINGTAI_WHATSAPP_CONFIG"):
                # The env var is set but the config file is missing/malformed:
                # fail loud instead of silently falling back to open defaults
                # (JSONDecodeError subclasses ValueError, so a bad file would
                # otherwise be misreported as "not set").
                raise
            # Personal mode is usable with defaults: no config file is not an
            # error, unlike an unreadable/invalid one (which still propagates).
            log.info("LINGTAI_WHATSAPP_CONFIG not set; using personal-mode defaults")
            config = {}
    return WhatsAppManager(config, config_path=config_path)


def _status_payload(manager: WhatsAppManager | None) -> dict[str, Any]:
    """Redacted status for ``lingtai://status``; never raises, never blocks long."""
    if manager is None:
        return {
            "status": "not_initialized",
            "bridge_alive": False,
            "ready": False,
            "notes": ["WhatsApp manager not initialized — check the MCP server stderr."],
        }
    try:
        payload = dict(manager.action("status"))
    except Exception as exc:  # status must never fail hard
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    payload.setdefault("status", "ok")
    return payload


def build_server(manager: WhatsAppManager | None = None) -> Server:
    """Construct the MCP server.

    ``manager`` is None when eager start failed (or when a caller only wants
    the schema surface); in that case ``manual`` still answers, ``settings``
    reports unavailable startup facts, and business actions return a readable
    error explaining why.
    """

    async def _list_resources(
        _ctx: ServerRequestContext,
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=item["uri"],
                    name=item["name"],
                    description=item["description"],
                    mime_type=item["mimeType"],
                )
                for item in _RESOURCE_INDEX
            ],
        )

    async def _read_resource(
        _ctx: ServerRequestContext,
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        resource_uri = _canonical_resource_uri(params.uri)
        try:
            text, mime = resource_text(resource_uri, status=_status_payload(manager))
        except KeyError as exc:
            raise _unknown_resource(resource_uri) from exc
        return _resource_result(resource_uri, text, mime)

    async def _list_tools(
        _ctx: ServerRequestContext,
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        # Stable order is part of the raw MCP contract.
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=WHATSAPP_PLUGIN.name,
                    description=DESCRIPTION,
                    input_schema=SCHEMA,
                ),
            ],
        )

    # The SDK validates the typed request envelope but never applies the
    # advertised per-tool ``input_schema``; ``handle_whatsapp`` is the actual
    # LTP-v2 validation boundary and owns the routing decision.
    async def _call_tool(
        _ctx: ServerRequestContext,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        if params.name != WHATSAPP_PLUGIN.name:
            # A lookup miss is a caller-fixable parameter error (-32602), never
            # a wrapped success payload.
            raise _unknown_tool(params.name)

        try:
            if manager is None:
                # ``manual`` still answers from the bundled SKILL.md.
                result = handle_whatsapp(None, arguments)
                if not result:
                    result = {
                        "status": "error",
                        "error": (
                            "WhatsApp manager not initialized — server boot "
                            "failed. Check stderr for the underlying exception "
                            "(most often an unreadable LINGTAI_WHATSAPP_CONFIG)."
                        ),
                    }
            else:
                # ``handle_whatsapp`` -> manager -> bridge is synchronous and can
                # block for the bridge timeout; keep it off the event loop.
                result = await asyncio.to_thread(handle_whatsapp, manager, arguments)
        except Exception as e:
            # A listed family owning a domain failure keeps it as a readable
            # tool result; only lookup misses above are protocol errors.
            result = {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
            }
        return _tool_result(result)

    server: Server = Server(
        WHATSAPP_PLUGIN.server_name,
        instructions=_SERVER_INSTRUCTIONS,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
        on_list_resources=_list_resources,
        on_read_resource=_read_resource,
    )
    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def serve() -> None:
    """Run the MCP server over stdio.

    The manager is built eagerly so the Node bridge starts (and inbound
    messages reach the agent inbox) without waiting for an unrelated tool call.
    """
    manager: WhatsAppManager | None = None
    try:
        manager = build_manager()
        log.info("WhatsApp manager ready (bridge_alive=%s)", manager.bridge.alive)
    except Exception as e:
        log.error(
            "eager start failed; tool calls will return errors until fixed: %s", e,
        )
        manager = None

    server = build_server(manager)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass
