"""WeChat addon — iLink Bot API integration.

Connects agents to WeChat via QR code login. Supports text,
images, voice (with transcription), video, and files.

Usage (config file):
    agent = Agent(
        addons={"wechat": {"config": ".lingtai/.addons/wechat/config.json"}},
    )

Prerequisites:
    1. Run the login command to scan a QR code:
       python -c "from lingtai.addons.wechat.login import cli_login; cli_login('.lingtai/.addons/wechat')"
    2. credentials.json is created automatically after scanning.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .manager import WechatManager, SCHEMA, DESCRIPTION

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

log = logging.getLogger(__name__)


def setup(
    agent: "BaseAgent",
    *,
    config: str | Path | None = None,
    base_url: str | None = None,
    cdn_base_url: str | None = None,
    bot_token: str | None = None,
    user_id: str | None = None,
    poll_interval: float | None = None,
    allowed_users: list[str] | None = None,
    **kwargs,
) -> WechatManager:
    """Set up WeChat addon — registers wechat tool, starts long-poll.

    Args:
        config: Path to config.json. Credentials are loaded from
                credentials.json in the same directory.

    Listeners are NOT started here — they start in WechatManager.start(),
    which is called by Agent.start() via the addon lifecycle.
    """
    from . import api

    if config is not None:
        config_path = Path(config)
        if not config_path.is_file():
            raise FileNotFoundError(f"WeChat config not found: {config_path}")

        file_cfg = json.loads(config_path.read_text(encoding="utf-8"))

        # Load credentials from sibling file
        creds_path = config_path.parent / "credentials.json"
        if not creds_path.is_file():
            raise FileNotFoundError(
                f"WeChat credentials not found: {creds_path}. "
                "Run the login command first: "
                'python -c "from lingtai.addons.wechat.login import cli_login; '
                f"cli_login('{config_path.parent}')\""
            )
        creds = json.loads(creds_path.read_text(encoding="utf-8"))

        if base_url is None:
            base_url = creds.get("base_url") or file_cfg.get(
                "base_url", api.DEFAULT_BASE_URL
            )
        if cdn_base_url is None:
            cdn_base_url = file_cfg.get("cdn_base_url", api.CDN_BASE_URL)
        if bot_token is None:
            bot_token = creds.get("bot_token")
        if user_id is None:
            user_id = creds.get("user_id")
        if poll_interval is None:
            poll_interval = file_cfg.get("poll_interval", 1.0)
        if allowed_users is None:
            allowed_users = file_cfg.get("allowed_users", [])

    if not bot_token:
        raise ValueError(
            "WeChat addon requires a bot_token. "
            "Run the login command to authenticate via QR code."
        )
    if not user_id:
        raise ValueError("WeChat addon requires a user_id from login.")

    working_dir = Path(agent._working_dir)

    mgr = WechatManager(
        agent=agent,
        base_url=base_url or api.DEFAULT_BASE_URL,
        cdn_base_url=cdn_base_url or api.CDN_BASE_URL,
        token=bot_token,
        user_id=user_id,
        poll_interval=poll_interval or 1.0,
        allowed_users=allowed_users if allowed_users else None,
        working_dir=working_dir,
    )

    agent.add_tool(
        "wechat", schema=SCHEMA, handler=mgr.handle, description=DESCRIPTION,
    )

    log.info("WeChat addon configured for %s", user_id)
    return mgr
