#!/usr/bin/env python3
"""Shared service functions for WeChat Channels video capture/download."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wechat_video_downloader import download_video_descriptor, unique_path
from wechat_video_models import scrub_video_payload
from wechat_video_registry import REGISTRY


DEFAULT_VIDEO_DIR = Path.home() / "Downloads" / "wechat-articles" / "视频号" / "videos"


def safe_filename(value: str, suffix: str = ".mp4") -> str:
    import re

    name = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "-", str(value or "")).strip(" .")
    if not name:
        name = "wechat-video"
    if not name.endswith(suffix):
        name += suffix
    return name[:160]


def register_video_payload(payload: dict[str, Any], request_headers: dict[str, Any] | None = None) -> dict[str, Any]:
    object_desc = payload.get("objectDesc") if isinstance(payload.get("objectDesc"), dict) else payload
    if not isinstance(object_desc, dict):
        return {"ok": False, "error": "objectDesc must be an object"}
    descriptors = REGISTRY.register_object_desc(
        object_desc,
        source_url=str(payload.get("source_url") or payload.get("url") or ""),
        source_title=str(payload.get("source_title") or payload.get("title") or ""),
        request_headers=request_headers,
    )
    videos = [item for item in descriptors if item.is_video]
    return {
        "ok": True,
        "registered_count": len(descriptors),
        "video_count": len(videos),
        "descriptors": [item.public_dict() for item in descriptors],
    }


def video_status() -> dict[str, Any]:
    items = REGISTRY.public_list()
    return {"ok": True, "count": len(items), "videos": items}


def download_registered_video(payload: dict[str, Any]) -> dict[str, Any]:
    descriptor_id = str(payload.get("id") or payload.get("descriptor_id") or "").strip()
    if not descriptor_id:
        return {"ok": False, "error": "missing video descriptor id"}
    descriptor = REGISTRY.get(descriptor_id)
    if not descriptor:
        return {"ok": False, "status": "needs_capture", "error": "video descriptor expired or was not captured"}
    output_dir = Path(str(payload.get("output_dir") or DEFAULT_VIDEO_DIR)).expanduser().resolve()
    filename = safe_filename(str(payload.get("filename") or descriptor.description or descriptor.source_title or descriptor.id), ".mp4")
    output_path = unique_path(output_dir / filename)
    quality = str(payload.get("quality") or "source")
    try:
        result = download_video_descriptor(descriptor, output_path, quality=quality)
    except Exception as exc:
        return {"ok": False, "status": "failed", "id": descriptor_id, "error": str(exc)}
    return {**result, "descriptor": scrub_video_payload(descriptor.public_dict())}


def json_response(payload: dict[str, Any]) -> bytes:
    return (json.dumps(scrub_video_payload(payload), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
