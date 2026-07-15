#!/usr/bin/env python3
"""In-process, short-lived WeChat Channels media registry."""

from __future__ import annotations

import time
from threading import RLock
from typing import Any

from wechat_video_models import VideoDescriptor, descriptors_from_object_desc


class VideoRegistry:
    def __init__(self) -> None:
        self._items: dict[str, VideoDescriptor] = {}
        self._lock = RLock()

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            for key in [key for key, item in self._items.items() if item.expires_at and item.expires_at < now]:
                del self._items[key]

    def register_object_desc(
        self,
        object_desc: dict[str, Any],
        source_url: str = "",
        source_title: str = "",
        request_headers: dict[str, Any] | None = None,
    ) -> list[VideoDescriptor]:
        descriptors = descriptors_from_object_desc(object_desc, source_url, source_title, request_headers)
        with self._lock:
            self.prune()
            for descriptor in descriptors:
                self._items[descriptor.id] = descriptor
        return descriptors

    def get(self, descriptor_id: str) -> VideoDescriptor | None:
        self.prune()
        with self._lock:
            return self._items.get(descriptor_id)

    def list(self, include_images: bool = False) -> list[VideoDescriptor]:
        self.prune()
        with self._lock:
            items = list(self._items.values())
        if not include_images:
            items = [item for item in items if item.is_video]
        return sorted(items, key=lambda item: item.captured_at, reverse=True)

    def public_list(self, include_images: bool = False) -> list[dict[str, Any]]:
        return [item.public_dict() for item in self.list(include_images)]


REGISTRY = VideoRegistry()
