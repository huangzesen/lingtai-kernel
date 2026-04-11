"""Media download/upload helpers for WeChat addon."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx

from . import api
from .types import (
    CDNMedia, UploadMediaType, MessageItemType,
    ImageItem, VoiceItem, FileItem, VideoItem, MessageItem,
)

log = logging.getLogger(__name__)

# Extension → UploadMediaType mapping
_UPLOAD_TYPE_MAP = {
    ".jpg": UploadMediaType.IMAGE,
    ".jpeg": UploadMediaType.IMAGE,
    ".png": UploadMediaType.IMAGE,
    ".gif": UploadMediaType.IMAGE,
    ".webp": UploadMediaType.IMAGE,
    ".bmp": UploadMediaType.IMAGE,
    ".mp4": UploadMediaType.VIDEO,
    ".avi": UploadMediaType.VIDEO,
    ".mov": UploadMediaType.VIDEO,
    ".mkv": UploadMediaType.VIDEO,
    ".wav": UploadMediaType.VOICE,
    ".mp3": UploadMediaType.VOICE,
    ".ogg": UploadMediaType.VOICE,
    ".silk": UploadMediaType.VOICE,
    ".amr": UploadMediaType.VOICE,
}

# UploadMediaType → MessageItemType mapping
_ITEM_TYPE_MAP = {
    UploadMediaType.IMAGE: MessageItemType.IMAGE,
    UploadMediaType.VIDEO: MessageItemType.VIDEO,
    UploadMediaType.VOICE: MessageItemType.VOICE,
    UploadMediaType.FILE: MessageItemType.FILE,
}


async def download_media(
    cdn_media: CDNMedia,
    dest_dir: str | Path,
    filename: str = "media",
) -> str:
    """Download media from CDN. Returns local file path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = cdn_media.full_url
    if not url:
        raise ValueError("CDN media has no full_url")

    dest_path = dest_dir / filename
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=60.0)
        resp.raise_for_status()
        dest_path.write_bytes(resp.content)

    return str(dest_path)


def decode_voice(silk_path: str | Path, out_path: str | Path) -> str:
    """Decode Silk audio to WAV. Returns output path.

    Requires the `pilk` package: pip install pilk
    """
    try:
        import pilk
    except ImportError:
        log.warning("pilk not installed — cannot decode Silk voice. pip install pilk")
        return str(silk_path)

    silk_path = str(silk_path)
    out_path = str(out_path)
    pilk.decode(silk_path, out_path)
    return out_path


def detect_upload_type(file_path: str | Path) -> UploadMediaType:
    """Detect UploadMediaType from file extension. Defaults to FILE."""
    ext = Path(file_path).suffix.lower()
    return _UPLOAD_TYPE_MAP.get(ext, UploadMediaType.FILE)


async def upload_media(
    file_path: str | Path,
    base_url: str,
    token: str,
    to_user_id: str,
) -> CDNMedia:
    """Upload a file to WeChat CDN. Returns CDNMedia reference for sendMessage."""
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    data = file_path.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    media_type = detect_upload_type(file_path)

    # Get upload URL
    upload_resp = await api.get_upload_url(
        base_url, token,
        media_type=int(media_type),
        to_user_id=to_user_id,
        rawsize=len(data),
        rawfilemd5=md5,
        filesize=len(data),
    )

    upload_url = upload_resp.upload_full_url
    if not upload_url:
        raise RuntimeError("Server did not return an upload URL")

    # Upload to CDN
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            upload_url,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120.0,
        )
        resp.raise_for_status()

    return CDNMedia(
        encrypt_query_param=upload_resp.upload_param,
    )


def make_media_item(cdn_media: CDNMedia, file_path: Path) -> MessageItem:
    """Create a MessageItem for sending uploaded media."""
    upload_type = detect_upload_type(file_path)
    item_type = _ITEM_TYPE_MAP.get(upload_type, MessageItemType.FILE)

    item = MessageItem(type=int(item_type))
    if item_type == MessageItemType.IMAGE:
        item.image_item = ImageItem(media=cdn_media)
    elif item_type == MessageItemType.VIDEO:
        item.video_item = VideoItem(media=cdn_media)
    elif item_type == MessageItemType.VOICE:
        item.voice_item = VoiceItem(media=cdn_media)
    else:
        item.file_item = FileItem(media=cdn_media, file_name=file_path.name)

    return item
