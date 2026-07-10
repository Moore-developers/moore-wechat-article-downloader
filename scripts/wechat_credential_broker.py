#!/usr/bin/env python3
"""Ephemeral local broker for WeChat article-session credentials.

Raw credential material never leaves this process. The Unix socket reports only
per-public-account state so a separate control process can decide whether to
queue or resume an engagement run.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable


BROKER_TTL_SECONDS = 25 * 60
MAX_REQUEST_BYTES = 2 * 1024 * 1024
REQUIRED_FIELDS = ("uin", "key", "pass_ticket", "appmsg_token", "cookie")
METRIC_FIELDS = ("read_count", "like_count", "old_like_count", "share_count", "comment_count")
MAX_ARTICLES_PER_REQUEST = 10
MAX_COMMENT_PAGES = 3
MAX_COMMENT_ROWS_PER_ARTICLE = 300


def credential_socket_path(base: Path, session_id: str) -> Path:
    """Keep the Unix socket below the platform path-length limit."""
    seed = f"{Path(base).expanduser().resolve()}|{session_id}".encode("utf-8")
    return Path("/tmp") / f"moore-wechat-{hashlib.sha256(seed).hexdigest()[:24]}.sock"


def credential_capability_path(base: Path, session_id: str) -> Path:
    return Path(base).expanduser() / "context" / f"{session_id}.credential-capability"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class WeChatCredentialBroker:
    """Owns raw values in memory and exposes redacted status over a Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        session_id: str,
        capability: str,
        ttl_seconds: int = BROKER_TTL_SECONDS,
        http_get: Callable[[str, dict[str, str]], str] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.session_id = session_id
        self.capability = capability
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._credentials: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._http_get = http_get or self._default_http_get

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

    def capture(self, request_url: str, request_headers: Any, response_headers: Any = None, response_text: str = "") -> bool:
        parsed = urllib.parse.urlsplit(request_url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        biz = str((query.get("__biz") or [""])[0]).strip()
        if not biz:
            return False
        values = {key: str((query.get(key) or [""])[0]).strip() for key in ("uin", "key", "pass_ticket", "appmsg_token")}
        if not values["appmsg_token"]:
            match = re.search(r"(?:var\s+)?appmsg_token\s*[:=]\s*['\"]([^'\"\\]{6,512})", response_text or "", re.I)
            if match:
                values["appmsg_token"] = match.group(1)
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

    def fetch_engagement(self, biz: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
        """Fetch only elected comments and observable metrics with in-memory credentials."""
        raw = self._credential_for(biz)
        if raw is None:
            return {"ok": False, "status": "waiting_credential", "error": "valid credential is unavailable", "articles": []}
        selected = [item for item in articles if isinstance(item, dict)][:MAX_ARTICLES_PER_REQUEST]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self._fetch_article_engagement, biz, raw, article) for article in selected]
            results = [future.result() for future in as_completed(futures)]
        results.sort(key=lambda item: int(item.get("article_id") or 0))
        return {
            "ok": all(item.get("ok") for item in results),
            "status": "complete",
            "source": "wechat_session_api",
            "articles": results,
        }

    def _credential_for(self, biz: str) -> dict[str, str] | None:
        now_epoch = time.time()
        with self._lock:
            self._purge_expired(now_epoch)
            record = self._credentials.get(biz)
            raw = dict(record.get("raw", {})) if record else {}
        return raw if all(raw.get(field) for field in REQUIRED_FIELDS) else None

    def _fetch_article_engagement(self, biz: str, raw: dict[str, str], article: dict[str, Any]) -> dict[str, Any]:
        article_id = int(article.get("article_id") or 0)
        msgid = str(article.get("msgid") or "")
        comment_id = str(article.get("comment_id") or "")
        url = str(article.get("url") or "")
        if not article_id or not msgid or not comment_id or not self._allowed_article_url(url):
            return {"ok": False, "article_id": article_id, "error": "article context is incomplete"}
        headers = {
            "Cookie": raw["cookie"],
            "Referer": "https://mp.weixin.qq.com/",
            "User-Agent": "Mozilla/5.0 MicroMessenger",
        }
        try:
            article_html = self._http_get(self._with_credential_query(url, biz, raw), headers)
            metrics = self._extract_metrics(article_html)
            comments, complete = self._fetch_elected_comments(biz, raw, msgid, int(article.get("idx") or 0), comment_id, headers)
            return {
                "ok": True,
                "article_id": article_id,
                "metrics": metrics,
                "comments": comments,
                "comments_complete": complete,
                "comment_scope": "elected",
            }
        except urllib.error.HTTPError as exc:
            return {"ok": False, "article_id": article_id, "error": f"http_{exc.code}"}
        except urllib.error.URLError:
            return {"ok": False, "article_id": article_id, "error": "network_error"}
        except TimeoutError:
            return {"ok": False, "article_id": article_id, "error": "timeout"}
        except Exception:
            return {"ok": False, "article_id": article_id, "error": "engagement_request_failed"}

    def _fetch_elected_comments(
        self,
        biz: str,
        raw: dict[str, str],
        msgid: str,
        idx: int,
        comment_id: str,
        headers: dict[str, str],
    ) -> tuple[list[dict[str, Any]], bool]:
        params = {
            "action": "getcomment",
            "__biz": biz,
            "appmsgid": msgid,
            "idx": str(idx),
            "comment_id": comment_id,
            "limit": "100",
            **{key: raw[key] for key in ("uin", "key", "pass_ticket", "appmsg_token")},
        }
        comments: list[dict[str, Any]] = []
        seen: set[str] = set()
        buffer = ""
        complete = True
        for _page in range(MAX_COMMENT_PAGES):
            request_params = dict(params)
            if buffer:
                request_params["buffer"] = buffer
            text = self._http_get("https://mp.weixin.qq.com/mp/appmsg_comment?" + urllib.parse.urlencode(request_params), headers)
            payload = json.loads(text)
            if not isinstance(payload, dict) or int((payload.get("base_resp") or {}).get("ret") or 0) != 0:
                raise RuntimeError("comment endpoint rejected request")
            rows = payload.get("elected_comment") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                normalized = {
                    "comment_id": str(row.get("id") or row.get("comment_id") or ""),
                    "nick_name": str(row.get("nick_name") or row.get("nickname") or ""),
                    "content": str(row.get("content") or ""),
                    "like_count": row.get("like_num") or row.get("like_count") or 0,
                    "create_time": row.get("create_time") or "",
                    "comment_scope": "elected",
                    "complete": 0,
                }
                identity = normalized["comment_id"]
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                comments.append(normalized)
                if len(comments) >= MAX_COMMENT_ROWS_PER_ARTICLE:
                    return comments, False
            continue_flag = int(payload.get("continue_flag") or 0) if isinstance(payload, dict) else 0
            buffer = str(payload.get("buffer") or "") if isinstance(payload, dict) else ""
            if not continue_flag:
                return comments, complete
            if not buffer:
                return comments, False
        return comments, False

    @staticmethod
    def _with_credential_query(url: str, biz: str, raw: dict[str, str]) -> str:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        query["__biz"] = [biz]
        for key in ("uin", "key", "pass_ticket", "appmsg_token"):
            query[key] = [raw[key]]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query, doseq=True), ""))

    @staticmethod
    def _allowed_article_url(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme == "https" and parsed.hostname == "mp.weixin.qq.com" and (
            parsed.path == "/s" or parsed.path.startswith("/s/") or parsed.path == "/mp/appmsg/show"
        )

    @staticmethod
    def _extract_metrics(html_text: str) -> dict[str, int | None]:
        metrics: dict[str, int | None] = {field: None for field in METRIC_FIELDS}
        aliases = {
            "read_count": ("read_num", "appmsg_read_num"),
            "like_count": ("like_num", "appmsg_like_num"),
            "old_like_count": ("old_like_num", "appmsg_old_like_num"),
            "share_count": ("share_count",),
            "comment_count": ("comment_count",),
        }
        for field, keys in aliases.items():
            for key in keys:
                match = re.search(rf"[\"']?{re.escape(key)}[\"']?\s*[:=]\s*[\"']?(\d+)", html_text)
                if match:
                    metrics[field] = int(match.group(1))
                    break
        return metrics

    @staticmethod
    def _default_http_get(url: str, headers: dict[str, str]) -> str:
        request = urllib.request.Request(url, headers=headers)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req: Any, fp: Any, code: int, msg: str, hdrs: Any, newurl: str) -> None:
                return None

        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=12) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise RuntimeError("wechat response exceeds local safety limit")
            return body.decode(charset, errors="replace")

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
                    raw = self._recv_all(client)
                    request = json.loads(raw.decode("utf-8")) if raw else {}
                    if not isinstance(request, dict):
                        response = {"ok": False, "error": "invalid broker request"}
                    elif request.get("op") == "status":
                        response = self.status(str(request.get("biz") or ""))
                    elif request.get("op") == "fetch_engagement":
                        if not self.capability or str(request.get("capability") or "") != self.capability:
                            response = {"ok": False, "error": "broker authorization failed"}
                        else:
                            response = self.fetch_engagement(str(request.get("biz") or ""), request.get("articles") or [])
                    else:
                        response = {"ok": False, "error": "unsupported operation"}
                except Exception:
                    response = {"ok": False, "error": "invalid broker request"}
                client.sendall(json.dumps(response, ensure_ascii=True, sort_keys=True).encode("utf-8"))

    @staticmethod
    def _recv_all(client: socket.socket) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_REQUEST_BYTES:
            chunk = client.recv(min(64 * 1024, MAX_REQUEST_BYTES - total + 1))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
        raise ValueError("broker request too large")


def broker_status(socket_path: Path, biz: str = "", timeout_seconds: float = 1.0) -> dict[str, Any]:
    """Read-only status helper. It cannot request or reveal raw credential values."""
    if not socket_path.exists():
        return {"ok": False, "status": "unavailable", "error": "credential broker is not running"}
    try:
        return broker_request(socket_path, {"op": "status", "biz": biz}, timeout_seconds)
    except OSError:
        return {"ok": False, "status": "unavailable", "error": "credential broker is not running"}


def broker_request(socket_path: Path, payload: dict[str, Any], timeout_seconds: float = 30.0) -> dict[str, Any]:
    """Invoke an allowed broker operation without exposing its in-memory credentials."""
    if not socket_path.exists():
        return {"ok": False, "status": "unavailable", "error": "credential broker is not running"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            client.sendall(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        response = json.loads(b"".join(chunks).decode("utf-8")) if chunks else {}
        return response if isinstance(response, dict) else {"ok": False, "error": "invalid broker response"}
    except OSError:
        return {"ok": False, "status": "unavailable", "error": "credential broker is not running"}
