"""The WhatsApp curated MCP plugin descriptor.

One place where this package states who it is: its registry name, the MCP
server identity, the stdio declaration the curated catalog publishes for it,
the bundled ``SKILL.md`` its ``manual`` action returns, and the actions it
*itself* owns. Both reserved actions are deliberately absent from
:data:`WHATSAPP_DECLARED_ACTIONS` — :class:`~lingtai.mcp_servers._plugin.CuratedMcpPlugin`
appends owner-bound ``settings`` followed by the packaged ``manual`` and
rejects any attempt to declare either here.

Mirrors ``lingtai.mcp_servers.telegram.plugin`` (the reference slice for
``_plugin.py``). ``_family.py`` consumes this for the public schema and
dispatch, ``server.py`` for its server identity, and ``manager.py`` for the
manual payload. The shipped ``lingtai/mcp_catalog.json`` entry must equal
:meth:`~lingtai.mcp_servers._plugin.CuratedMcpPlugin.mcp_declaration`; the
catalog file itself stays the runtime source the host reads.
"""
from __future__ import annotations

from .._plugin import CuratedMcpPlugin

WHATSAPP_PLUGIN = CuratedMcpPlugin(
    name="whatsapp",
    package=__package__,
    server_name="lingtai-whatsapp",
    summary="Personal-account WhatsApp Web client via a local whatsapp-web.js bridge with LICC inbox delivery.",
    homepage="https://github.com/Lingtai-AI/lingtai-whatsapp",
    skill_name="whatsapp-mcp-manual",
    settings=True,
)

#: WhatsApp's own public actions, in stable model-facing order. The reserved
#: ``settings``/``manual`` actions are appended by the plugin, never declared here.
WHATSAPP_DECLARED_ACTIONS: tuple[str, ...] = (
    "send", "check", "read", "reply", "react", "search", "contacts",
    "add_contact", "remove_contact", "get_qr", "logout", "status",
)

#: The complete public action list: declared actions, ``settings``, ``manual``.
WHATSAPP_ACTIONS: tuple[str, ...] = WHATSAPP_PLUGIN.actions(WHATSAPP_DECLARED_ACTIONS)
