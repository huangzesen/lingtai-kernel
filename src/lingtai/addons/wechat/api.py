"""HTTP wrappers for the 5 iLink Bot API endpoints."""
from __future__ import annotations

import json
import logging
import struct
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

import httpx

from .types import (
    GetUpdatesResp, GetUploadUrlResp, GetConfigResp,
    WeixinMessage, msg_from_dict, msg_to_dict,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_LONG_POLL_TIMEOUT = 35.0
DEFAULT_SEND_TIMEOUT = 15.0

# Package version for channel_version header
_PKG_VERSION = "1.0.0"


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _auth_headers(token: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
    }
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def _base_info() -> dict:
    return {"channel_version": _PKG_VERSION}


async def get_qrcode(base_url: str = DEFAULT_BASE_URL) -> dict:
    """Fetch a QR code for WeChat login.

    Returns dict with 'qrcode' (str) and 'qrcode_img_content' (str) keys.
    """
    url = _ensure_trailing_slash(base_url) + "ilink/bot/get_bot_qrcode?bot_type=3"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15.0)
        resp.raise_for_status()
        return resp.json()


async def poll_qr_status(base_url: str, qrcode: str) -> dict:
    """Poll QR code login status. Returns dict with 'status' key.

    Status values: 'wait', 'scaned', 'confirmed', 'expired', 'scaned_but_redirect'.
    On 'confirmed': also has 'bot_token', 'ilink_bot_id', 'baseurl', 'ilink_user_id'.
    """
    url = (
        _ensure_trailing_slash(base_url)
        + f"ilink/bot/get_qrcode_status?qrcode={qrcode}"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=DEFAULT_LONG_POLL_TIMEOUT + 5)
        resp.raise_for_status()
        return resp.json()


async def get_updates(
    base_url: str,
    token: str,
    get_updates_buf: str = "",
    timeout: float = DEFAULT_LONG_POLL_TIMEOUT,
) -> GetUpdatesResp:
    """Long-poll for incoming messages.

    Returns GetUpdatesResp with msgs list and updated get_updates_buf cursor.
    On client-side timeout, returns empty response to allow retry.
    """
    url = _ensure_trailing_slash(base_url) + "ilink/bot/getupdates"
    body = {
        "get_updates_buf": get_updates_buf,
        "base_info": _base_info(),
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json=body,
                headers=_auth_headers(token),
                timeout=timeout + 5,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        # Server didn't respond in time — return empty to retry
        return GetUpdatesResp(
            ret=0, msgs=[], get_updates_buf=get_updates_buf,
        )

    msgs = [msg_from_dict(m) for m in data.get("msgs", [])]
    return GetUpdatesResp(
        ret=data.get("ret"),
        errcode=data.get("errcode"),
        errmsg=data.get("errmsg"),
        msgs=msgs,
        get_updates_buf=data.get("get_updates_buf", get_updates_buf),
        longpolling_timeout_ms=data.get("longpolling_timeout_ms"),
    )


async def send_message(
    base_url: str,
    token: str,
    msg: WeixinMessage,
) -> None:
    """Send a message (text or media)."""
    url = _ensure_trailing_slash(base_url) + "ilink/bot/sendmessage"
    body = {
        "msg": msg_to_dict(msg),
        "base_info": _base_info(),
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=body,
            headers=_auth_headers(token),
            timeout=DEFAULT_SEND_TIMEOUT,
        )
        resp.raise_for_status()


async def get_upload_url(
    base_url: str,
    token: str,
    *,
    media_type: int,
    to_user_id: str,
    rawsize: int,
    rawfilemd5: str,
    filesize: int,
    aeskey: str | None = None,
) -> GetUploadUrlResp:
    """Get a pre-signed CDN upload URL."""
    url = _ensure_trailing_slash(base_url) + "ilink/bot/getuploadurl"
    body: dict[str, Any] = {
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": rawsize,
        "rawfilemd5": rawfilemd5,
        "filesize": filesize,
        "base_info": _base_info(),
    }
    if aeskey:
        body["aeskey"] = aeskey
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=body,
            headers=_auth_headers(token),
            timeout=DEFAULT_SEND_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    return GetUploadUrlResp(
        upload_param=data.get("upload_param"),
        upload_full_url=data.get("upload_full_url"),
    )


async def get_config(base_url: str, token: str) -> GetConfigResp:
    """Get bot config (typing ticket etc.)."""
    url = _ensure_trailing_slash(base_url) + "ilink/bot/getconfig"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={"base_info": _base_info()},
            headers=_auth_headers(token),
            timeout=DEFAULT_SEND_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    return GetConfigResp(
        ret=data.get("ret"),
        errmsg=data.get("errmsg"),
        typing_ticket=data.get("typing_ticket"),
    )
