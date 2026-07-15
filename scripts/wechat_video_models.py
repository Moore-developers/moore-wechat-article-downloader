#!/usr/bin/env python3
"""Short-lived WeChat Channels video descriptors.

This module ports the media-shape handling from res-downloader's qq.com plugin.
Only public, non-sensitive descriptor fields should leave process memory.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any


SENSITIVE_DESCRIPTOR_FIELDS = {
    "decodeKey",
    "decode_key",
    "headers",
    "signedUrl",
    "signed_url",
    "urlToken",
    "url_token",
}
SENSITIVE_QUERY_KEYS = {
    "appmsg_token",
    "auth-key",
    "auth_key",
    "cookie",
    "exportkey",
    "key",
    "pass_ticket",
    "sessionid",
    "ticket",
    "token",
    "uin",
    "wxtoken",
}


def safe_text(value: Any, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def sanitize_source_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [(key, item) for key, item in query if key.lower() not in SENSITIVE_QUERY_KEYS]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query), ""))


@dataclass
class VideoDescriptor:
    id: str
    raw_url: str
    signed_url: str
    media_type: int = 0
    size: int = 0
    cover_url: str = ""
    decode_key: str = ""
    description: str = ""
    source_url: str = ""
    source_title: str = ""
    object_id: str = ""
    file_formats: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    captured_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    @property
    def is_video(self) -> bool:
        return self.media_type != 9

    @property
    def suffix(self) -> str:
        return ".mp4" if self.is_video else ".png"

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "media_type": self.media_type,
            "classify": "video" if self.is_video else "image",
            "size": self.size,
            "cover_url": self.cover_url,
            "description": self.description,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "object_id": self.object_id,
            "has_decode_key": bool(self.decode_key),
            "file_formats": list(self.file_formats),
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }

    def download_url(self, quality: str = "source") -> str:
        url = self.signed_url
        if quality in {"source", "", "0"} or not self.file_formats:
            return url
        index = {"lowest": 0, "middle": len(self.file_formats) // 2, "highest": len(self.file_formats) - 1}.get(quality)
        if index is None:
            return url
        flag = self.file_formats[max(0, min(index, len(self.file_formats) - 1))]
        if not flag:
            return url
        separator = "&" if urllib.parse.urlsplit(url).query else "?"
        return f"{url}{separator}X-snsvideoflag={urllib.parse.quote(flag)}"


def safe_download_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("User-Agent", "Referer", "Origin"):
        value = str((headers or {}).get(key) or (headers or {}).get(key.lower()) or "").strip()
        if value:
            result[key] = value[:500]
    return result


def descriptors_from_object_desc(
    object_desc: dict[str, Any],
    source_url: str = "",
    source_title: str = "",
    request_headers: dict[str, Any] | None = None,
    ttl_seconds: int = 20 * 60,
) -> list[VideoDescriptor]:
    media = object_desc.get("media")
    if not isinstance(media, list):
        return []
    object_id = safe_text(object_desc.get("objectId") or object_desc.get("id") or object_desc.get("object_id"), 100)
    description = safe_text(object_desc.get("description") or object_desc.get("desc"), 500)
    safe_source_url = sanitize_source_url(source_url)
    result: list[VideoDescriptor] = []
    now = time.time()
    for item in media:
        if not isinstance(item, dict):
            continue
        raw_url = str(item.get("url") or "").strip()
        if not raw_url:
            continue
        url_token = str(item.get("urlToken") or "")
        signed_url = raw_url + url_token
        formats: list[str] = []
        spec = item.get("spec")
        if isinstance(spec, list):
            for spec_item in spec:
                if isinstance(spec_item, dict) and spec_item.get("fileFormat"):
                    formats.append(str(spec_item.get("fileFormat")))
        descriptor_id = stable_id(json.dumps([raw_url, object_id, safe_source_url], ensure_ascii=False, sort_keys=True))
        result.append(
            VideoDescriptor(
                id=descriptor_id,
                raw_url=raw_url,
                signed_url=signed_url,
                media_type=safe_int(item.get("mediaType")),
                size=safe_int(item.get("fileSize")),
                cover_url=str(item.get("coverUrl") or "").strip(),
                decode_key=str(item.get("decodeKey") or "").strip(),
                description=description,
                source_url=safe_text(safe_source_url, 500),
                source_title=safe_text(source_title, 200),
                object_id=object_id,
                file_formats=formats,
                headers=safe_download_headers(request_headers),
                captured_at=now,
                expires_at=now + max(60, ttl_seconds),
            )
        )
    return result


def scrub_video_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[redacted]" if key in SENSITIVE_DESCRIPTOR_FIELDS else scrub_video_payload(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_video_payload(item) for item in value]
    return value
