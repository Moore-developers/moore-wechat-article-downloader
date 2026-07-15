#!/usr/bin/env python3
"""Downloader for registered WeChat Channels videos."""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from wechat_video_crypto import xor_decrypt_file_header
from wechat_video_models import VideoDescriptor


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MIN_PART_SIZE = 1024 * 1024


ProgressCallback = Callable[[int, int], None]


def default_headers(descriptor: VideoDescriptor) -> dict[str, str]:
    headers = dict(descriptor.headers)
    headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    parsed = urllib.parse.urlsplit(descriptor.raw_url)
    if parsed.scheme and parsed.netloc:
        headers.setdefault("Referer", f"{parsed.scheme}://{parsed.netloc}/")
    return headers


def request(url: str, method: str, headers: dict[str, str], timeout: int = 30) -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers=headers)


def open_no_proxy(req: urllib.request.Request, timeout: int = 30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def remote_size(url: str, headers: dict[str, str], timeout: int = 30) -> tuple[int, bool]:
    try:
        with open_no_proxy(request(url, "HEAD", headers, timeout), timeout) as resp:
            size = int(resp.headers.get("Content-Length") or 0)
            ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            return size, ranges
    except Exception:
        return 0, False


def download_range(url: str, path: Path, headers: dict[str, str], start: int, end: int, timeout: int = 60) -> int:
    range_headers = {**headers, "Range": f"bytes={start}-{end}"}
    req = request(url, "GET", range_headers, timeout)
    with open_no_proxy(req, timeout) as resp:
        if resp.status not in {200, 206}:
            raise RuntimeError(f"unexpected status code: {resp.status}")
        offset = start
        written = 0
        with path.open("r+b") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.seek(offset)
                fh.write(chunk)
                offset += len(chunk)
                written += len(chunk)
        return written


def download_single(url: str, path: Path, headers: dict[str, str], timeout: int = 120, progress: ProgressCallback | None = None) -> int:
    req = request(url, "GET", headers, timeout)
    total = 0
    with open_no_proxy(req, timeout) as resp:
        if resp.status not in {200, 206}:
            raise RuntimeError(f"unexpected status code: {resp.status}")
        with path.open("wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                if progress:
                    progress(total, 0)
    return total


def download_video_descriptor(
    descriptor: VideoDescriptor,
    output_path: Path,
    quality: str = "source",
    tasks: int = 4,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    if not descriptor.is_video:
        raise ValueError("descriptor is not a video")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    url = descriptor.download_url(quality)
    headers = default_headers(descriptor)
    size, supports_ranges = remote_size(url, headers)
    if size > 0:
        with output_path.open("wb") as fh:
            fh.truncate(size)
    if supports_ranges and size > MIN_PART_SIZE and tasks > 1:
        each = max(MIN_PART_SIZE, size // max(1, tasks))
        ranges: list[tuple[int, int]] = []
        start = 0
        while start < size:
            end = min(size - 1, start + each - 1)
            ranges.append((start, end))
            start = end + 1
        downloaded = 0
        for start, end in ranges:
            downloaded += download_range(url, output_path, headers, start, end)
            if progress:
                progress(downloaded, size)
    else:
        downloaded = download_single(url, output_path, headers, progress=progress)
    if descriptor.decode_key:
        xor_decrypt_file_header(output_path, descriptor.decode_key)
    final_size = output_path.stat().st_size if output_path.exists() else 0
    return {
        "ok": True,
        "id": descriptor.id,
        "path": str(output_path),
        "bytes": final_size,
        "expected_bytes": size or descriptor.size,
        "quality": quality,
        "decrypted": bool(descriptor.decode_key),
    }


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot allocate unique file path near {path}")


def chmod_user_only(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
