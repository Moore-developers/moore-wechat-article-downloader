#!/usr/bin/env python3
"""Ephemeral local broker for WeChat article-session credentials.

Raw credential material never leaves this process. The Unix socket reports only
per-public-account state so a separate control process can decide whether to
queue or resume an engagement run.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any


BROKER_TTL_SECONDS = 25 * 60
MAX_REQUEST_BYTES = 16 * 1024
REQUIRED_FIELDS = ("uin", "key", "pass_ticket", "appmsg_token", "cookie")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class WeChatCredentialBroker:
    """Owns raw values in memory and exposes redacted status over a Unix socket."""

    def __init__(self, socket_path: Path, session_id: str, ttl_seconds: int = BROKER_TTL_SECONDS) -> None:
        self.socket_path = Path(socket_path)
        self.session_id = session_id
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._credentials: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.25)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="moore-wechat-credential-broker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def capture(self, request_url: str, request_headers: Any, response_headers: Any = None) -> bool:
        parsed = urllib.parse.urlsplit(request_url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        biz = str((query.get("__biz") or [""])[0]).strip()
        if not biz:
            return False
        values = {key: str((query.get(key) or [""])[0]).strip() for key in ("uin", "key", "pass_ticket", "appmsg_token")}
        cookie = self._header_value(request_headers, "cookie")
        set_cookie = self._header_value(response_headers, "set-cookie")
        if set_cookie:
            cookie = "; ".join(part for part in (cookie, set_cookie) if part)
        if not any(values.values()) and not cookie:
            return False
        now_epoch = time.time()
        with self._lock:
            previous = self._credentials.get(biz, {})
            merged = dict(previous.get("raw", {}))
            merged.update({key: value for key, value in values.items() if value})
            if cookie:
                merged["cookie"] = cookie
            self._credentials[biz] = {
                "captured_at": utc_now(),
                "expires_at_epoch": now_epoch + self.ttl_seconds,
                "raw": merged,
            }
        return True

    def status(self, biz: str = "") -> dict[str, Any]:
        now_epoch = time.time()
        with self._lock:
            self._purge_expired(now_epoch)
            items = []
            for stored_biz, record in sorted(self._credentials.items()):
                if biz and stored_biz != biz:
                    continue
                raw = record.get("raw", {})
                remaining = max(0, int(record["expires_at_epoch"] - now_epoch))
                present = [field for field in REQUIRED_FIELDS if raw.get(field)]
                items.append(
                    {
                        "biz": stored_biz,
                        "status": "valid" if len(present) == len(REQUIRED_FIELDS) else "partial",
                        "captured_at": record["captured_at"],
                        "expires_at": dt.datetime.fromtimestamp(record["expires_at_epoch"], dt.timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "expires_in_seconds": remaining,
                        "available_fields": present,
                    }
                )
        return {"ok": True, "session_id": self.session_id, "credentials": items}

    @staticmethod
    def _header_value(headers: Any, name: str) -> str:
        if not headers:
            return ""
        try:
            value = headers.get(name, "")
        except AttributeError:
            value = ""
        return str(value or "").strip()

    def _purge_expired(self, now_epoch: float) -> None:
        expired = [biz for biz, record in self._credentials.items() if record.get("expires_at_epoch", 0) <= now_epoch]
        for biz in expired:
            self._credentials.pop(biz, None)

    def _serve(self) -> None:
        assert self._server is not None
        while not self._closed.is_set():
            try:
                client, _address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                client.settimeout(1)
                try:
                    raw = client.recv(MAX_REQUEST_BYTES)
                    request = json.loads(raw.decode("utf-8")) if raw else {}
                    if not isinstance(request, dict) or request.get("op") != "status":
                        response = {"ok": False, "error": "unsupported operation"}
                    else:
                        response = self.status(str(request.get("biz") or ""))
                except Exception:
                    response = {"ok": False, "error": "invalid broker request"}
                client.sendall(json.dumps(response, ensure_ascii=True, sort_keys=True).encode("utf-8"))


def broker_status(socket_path: Path, biz: str = "", timeout_seconds: float = 1.0) -> dict[str, Any]:
    """Read-only status helper. It cannot request or reveal raw credential values."""
    if not socket_path.exists():
        return {"ok": False, "status": "unavailable", "error": "credential broker is not running"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            client.sendall(json.dumps({"op": "status", "biz": biz}, ensure_ascii=True).encode("utf-8"))
            raw = client.recv(MAX_REQUEST_BYTES)
        response = json.loads(raw.decode("utf-8")) if raw else {}
        return response if isinstance(response, dict) else {"ok": False, "error": "invalid broker response"}
    except OSError:
        return {"ok": False, "status": "unavailable", "error": "credential broker is not running"}
