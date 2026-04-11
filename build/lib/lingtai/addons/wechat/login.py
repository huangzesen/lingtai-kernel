"""QR code login flow for WeChat iLink Bot API.

Provides cli_login() as a synchronous entry point for the setup skill.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from . import api

LOGIN_TIMEOUT = 300  # 5 minutes
POLL_INTERVAL = 2.0


def cli_login(addon_dir: str) -> None:
    """CLI entry point for WeChat QR login.

    Called by the setup skill via:
        python -c "from lingtai.addons.wechat.login import cli_login; cli_login('.lingtai/.addons/wechat')"

    Creates config.json with defaults if missing, runs QR login,
    saves credentials.json on success.
    """
    addon_path = Path(addon_dir)
    addon_path.mkdir(parents=True, exist_ok=True)

    # Create default config if not present
    config_path = addon_path / "config.json"
    if not config_path.is_file():
        config_path.write_text(json.dumps({
            "base_url": api.DEFAULT_BASE_URL,
            "cdn_base_url": api.CDN_BASE_URL,
            "poll_interval": 1.0,
            "allowed_users": [],
        }, indent=2), encoding="utf-8")
        print(f"Created default config at {config_path}")

    # Read base_url from config
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    base_url = cfg.get("base_url", api.DEFAULT_BASE_URL)

    try:
        result = asyncio.run(_login_flow(base_url))
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        sys.exit(1)

    if result is None:
        print("Login failed — QR code expired or error occurred.")
        sys.exit(1)

    # Save credentials
    creds_path = addon_path / "credentials.json"
    creds = {
        "bot_token": result["bot_token"],
        "user_id": result["user_id"],
        "base_url": result["base_url"],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    creds_path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    os.chmod(creds_path, 0o600)
    print(f"Connected as {result['user_id']}")
    print(f"Credentials saved to {creds_path}")


async def _login_flow(base_url: str) -> dict | None:
    """Run the QR login flow. Returns credentials dict or None on failure."""
    # Step 1: Get QR code
    print("Fetching QR code...")
    qr_data = await api.get_qrcode(base_url)
    qrcode_str = qr_data.get("qrcode")
    if not qrcode_str:
        print("Error: failed to get QR code from server.")
        return None

    # Step 2: Display QR code in terminal
    try:
        import qrcode as qr_lib
        qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L)
        qr.add_data(qr_data.get("qrcode_img_content", qrcode_str))
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(f"QR code data: {qrcode_str}")
        print("(Install 'qrcode' package for visual QR display)")

    print("\nScan this QR code with WeChat on your phone.")
    print("Waiting for confirmation (5 minute timeout)...")

    # Step 3: Poll for confirmation
    start = time.time()
    current_base_url = base_url
    while time.time() - start < LOGIN_TIMEOUT:
        try:
            status = await api.poll_qr_status(current_base_url, qrcode_str)
        except Exception as e:
            print(f"Poll error: {e}, retrying...")
            await asyncio.sleep(POLL_INTERVAL)
            continue

        s = status.get("status", "")
        if s == "wait":
            pass  # Still waiting for scan
        elif s == "scaned":
            print("QR code scanned — confirm on your phone...")
        elif s == "confirmed":
            return {
                "bot_token": status["bot_token"],
                "user_id": status.get("ilink_user_id", status.get("ilink_bot_id", "")),
                "base_url": status.get("baseurl", current_base_url),
            }
        elif s == "expired":
            print("QR code expired.")
            return None
        elif s == "scaned_but_redirect":
            redirect_host = status.get("redirect_host", "")
            if redirect_host:
                current_base_url = f"https://{redirect_host}"
                print(f"Redirecting to {current_base_url}...")
            continue
        else:
            print(f"Unknown status: {s}")

        await asyncio.sleep(POLL_INTERVAL)

    print("Login timed out (5 minutes).")
    return None
