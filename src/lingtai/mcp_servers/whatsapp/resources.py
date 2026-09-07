"""LingTai profile resources for WhatsApp MCP (personal-account mode)."""
from __future__ import annotations

import json
from typing import Any

from .plugin import WHATSAPP_ACTIONS, WHATSAPP_PLUGIN


def manifest() -> dict[str, Any]:
    return {
        "name": WHATSAPP_PLUGIN.server_name,
        "profile": "lingtai-mcp-v1",
        "summary": "Personal-account WhatsApp MCP for LingTai via a local whatsapp-web.js bridge (QR-code pairing).",
        "transport": "stdio MCP plus a local Node child process (whatsapp-web.js)",
        "backend": "personal_account_whatsapp_web",
        # Sourced from the plugin descriptor so the advertised action list
        # cannot drift from the family the server actually serves (``manual``
        # included, because the plugin always appends it).
        "tools": {"name": WHATSAPP_PLUGIN.name, "actions": list(WHATSAPP_ACTIONS)},
        "resources": [
            "lingtai://manifest", "lingtai://skills/whatsapp", "lingtai://docs/configuration", "lingtai://docs/troubleshooting", "lingtai://status", "lingtai://onboarding/whatsapp", "lingtai://onboarding/html-template",
        ],
        "agent_entrypoints": {"skill": "lingtai://skills/whatsapp", "onboarding": "lingtai://onboarding/whatsapp", "onboarding_html_template": "lingtai://onboarding/html-template"},
    }


SKILL = """# WhatsApp MCP skill\n\nUse this MCP when the human wants LingTai to communicate over a personal WhatsApp account. This implementation drives WhatsApp Web through a local whatsapp-web.js bridge; pairing is done by scanning a QR code from the phone (WhatsApp Settings -> Linked Devices -> Link a Device).\n\nKey constraints:\n- the bridge uses the unofficial whatsapp-web.js library, which violates WhatsApp's Terms of Service; account bans are possible. Use for personal/experimental purposes only;\n- the Node bridge and Puppeteer/Chromium must be available on the host (npm install in the bridge directory);\n- inbound messages are pushed into the agent inbox; outbound sends/replies/reacts go through the bridge.\n\nTypical setup flow: install bridge deps, write config (use `allowed_wa_ids` to restrict inbound senders), start the MCP, call the `get_qr` action, scan the QR with the phone, verify `status`, then send a test message.\n"""

CONFIGURATION = """# WhatsApp MCP configuration\n\nSet `LINGTAI_WHATSAPP_CONFIG` to a JSON file. Optional keys:\n- `node_path`: Node executable (default: `node` on PATH)\n- `bridge_dir`: path to the Node bridge directory (default: bundled `bridge/`)\n- `session_dir`: path to store the whatsapp-web.js session (default: `<workdir>/.wwebjs_auth`)\n- `store_dir`: path to store message/contact metadata (default: `<workdir>/whatsapp`)\n- `allowed_wa_ids`: optional list of WhatsApp IDs allowed to trigger inbound pushes; omit or use an empty list to allow all senders\n- `allowed_users`: legacy alias for `allowed_wa_ids`\n\nEntries may be bare digits (`15551234567`) or full JIDs (`15551234567@c.us`); formatting such as `+1 (555) 123-4567` is normalized before matching.\n\nExample:\n{\"session_dir\": \"C:\\\\Users\\\\me\\.wwebjs_auth\", \"allowed_wa_ids\": [\"15551234567@c.us\"]}\n"""

TROUBLESHOOTING = """# WhatsApp MCP troubleshooting\n\n- `get_qr` says no QR available: the bridge may still be starting (Puppeteer/Chromium first launch is slow) or the session is already authenticated; call `status` first.\n- Authentication fails: delete the session directory and scan a new QR.\n- `node` not found: install Node.js >= 18 and/or set `node_path` in config.\n- Bridge `npm install` missing: run `npm install` inside the bridge directory.\n- Inbound messages from a stranger do not wake the agent: set `allowed_wa_ids` to a list containing the sender's normalized WhatsApp ID, then restart/refresh the MCP; omit the key or use `[]` to allow all senders.\n- Sends fail: failures are usually transient network, bridge, authentication, or rate-limit issues; inspect the returned error before retrying.\n- Account ban risk: do not send automated bulk messages; prefer responding to incoming messages.\n"""

ONBOARDING = """# WhatsApp onboarding (personal account)\n\n1. Install Node.js >= 18 and run `npm install` in the bridge directory.\n2. Write `LINGTAI_WHATSAPP_CONFIG` (optional; defaults are fine for first use).\n3. Start the MCP server; wait for the bridge to boot (first launch downloads/launches Chromium).\n4. Call the `get_qr` action and scan the QR with the phone: WhatsApp Settings -> Linked Devices -> Link a Device.\n5. Confirm `status` shows `ready` and, when configured, `allowed_wa_ids_count` is correct.\n6. Send a test message from a WhatsApp user listed in `allowed_wa_ids`; omit the option to allow all senders.\n\nThe session persists across restarts in the session directory.\n"""

HTML_TEMPLATE = """<!doctype html><html><head><meta charset='utf-8'><title>LingTai WhatsApp MCP setup</title><style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.5}.warn{background:#fff3cd;border:1px solid #ffe08a;padding:12px;border-radius:8px}.box{background:#f6f8fa;padding:14px;border-radius:8px;white-space:pre-wrap}</style></head><body><h1>WhatsApp MCP setup (personal account)</h1><p class='warn'>Unofficial whatsapp-web.js bridge: violating WhatsApp ToS may lead to account bans. Personal/experimental use only.</p><div class='box'>{{SETUP}}</div></body></html>"""


def resource_text(uri: str, status: dict[str, Any] | None = None) -> tuple[str, str]:
    if uri == "lingtai://manifest":
        return json.dumps(manifest(), ensure_ascii=False, indent=2), "application/json"
    if uri == "lingtai://skills/whatsapp":
        return SKILL, "text/markdown; profile=lingtai-skill"
    if uri == "lingtai://docs/configuration":
        return CONFIGURATION, "text/markdown"
    if uri == "lingtai://docs/troubleshooting":
        return TROUBLESHOOTING, "text/markdown"
    if uri == "lingtai://status":
        return json.dumps(status or {"status": "not_initialized"}, ensure_ascii=False, indent=2), "application/json"
    if uri == "lingtai://onboarding/whatsapp":
        return ONBOARDING, "text/markdown"
    if uri == "lingtai://onboarding/html-template":
        return HTML_TEMPLATE, "text/html"
    raise KeyError(uri)
