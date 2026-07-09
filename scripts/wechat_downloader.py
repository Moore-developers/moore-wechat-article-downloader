#!/usr/bin/env python3
"""Local WeChat article downloader runtime.

Baseline goals:
- download public mp.weixin.qq.com article URLs
- deliver clean Markdown files, local images, and an index CSV by default
- expose CLI commands that the Skill orchestrates directly

This script intentionally uses only Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


APP_DIR = Path.home() / ".moore" / "wechat-article-downloader"
DEFAULT_DELIVERY_DIR = Path.home() / "Downloads" / "wechat-articles"
DEFAULT_PROXY_PORT = 23344
ARTICLE_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/[^\s\"'<>]+", re.I)
IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""([:\w-]+)\s*=\s*(['"])(.*?)\2""", re.S)
MAX_ASSET_BYTES = 15 * 1024 * 1024
ALLOWED_ASSET_HOST_SUFFIXES = (
    "mmbiz.qpic.cn",
    "mmbiz.qlogo.cn",
    "wx.qlogo.cn",
    "res.wx.qq.com",
    "mp.weixin.qq.com",
)
SENSITIVE_QUERY_KEYS = {
    "appmsg_token",
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
HISTORY_FIELDS = [
    "account_name",
    "account_id",
    "title",
    "url",
    "publish_time",
    "digest",
    "cover",
    "source_article_url",
    "fetch_method",
]
WECHAT_HISTORY_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.49 NetType/WIFI Language/zh_CN"
)
WECHAT_RADIUM_DIR = Path.home() / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data" / "Documents" / "app_data" / "radium"
WECHAT_MITM_HOST_RE = (
    r"^(?!(?:.*\.)?"
    r"(?:mp\.weixin\.qq\.com|res\.wx\.qq\.com|mmbiz\.qpic\.cn|support\.weixin\.qq\.com)"
    r"(?::\d+)?$).*$"
)



class DownloadThrottle:
    """Token bucket rate limiter with adaptive concurrency and jitter."""

    # conservative defaults: 20 req/min, burst of 3, 0.8–2.0s inter-article delay
    def __init__(
        self,
        req_per_min: int = 20,
        burst: int = 3,
        inter_delay: tuple[float, float] = (0.8, 2.0),
        init_workers: int = 2,
        max_workers: int = 4,
    ) -> None:
        self._rate = req_per_min / 60.0
        self._tokens = float(burst)
        self._max_tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._inter_delay = inter_delay
        self._success_streak = 0
        self.current_workers = init_workers
        self._max_workers = max_workers

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._max_tokens, self._tokens + (now - self._last_refill) * self._rate)
        self._last_refill = now

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)

    def inter_sleep(self) -> None:
        time.sleep(random.uniform(*self._inter_delay))

    def on_success(self) -> None:
        with self._lock:
            self._success_streak += 1
            if self._success_streak >= 10 and self.current_workers < self._max_workers:
                self.current_workers += 1
                self._success_streak = 0

    def on_rate_error(self) -> None:
        """Call on 429 / connection-reset; backs off and reduces concurrency."""
        with self._lock:
            self._success_streak = 0
            if self.current_workers > 1:
                self.current_workers -= 1
        time.sleep(30)

    @staticmethod
    def backoff_sleep(attempt: int) -> None:
        time.sleep(min(60.0, 2 ** attempt + random.uniform(0.0, 1.0)))


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def make_run_id() -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = hashlib.sha256(f"{stamp}-{time.time()}".encode()).hexdigest()[:8]
    return f"{stamp}-{suffix}"


def runtime_dir(path: str | None) -> Path:
    return Path(path).expanduser().resolve() if path else APP_DIR


def ensure_runtime(base: Path) -> None:
    for rel in ["account-history", "articles", "context", "runs"]:
        (base / rel).mkdir(parents=True, exist_ok=True)
    init_db(base)


def init_db(base: Path) -> None:
    db = sqlite3.connect(base / "app.db")
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                article_id TEXT PRIMARY KEY,
                title TEXT,
                account TEXT,
                author TEXT,
                publish_time TEXT,
                source_url TEXT,
                canonical_url TEXT,
                article_dir TEXT,
                downloaded_at TEXT,
                content_hash TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT,
                run_dir TEXT,
                success_count INTEGER,
                failure_count INTEGER
            )
            """
        )
        db.execute(
            "PRAGMA user_version = 1"
        )
        db.commit()
    finally:
        db.close()


def db_upsert_article(base: Path, meta: dict[str, Any], article_dir: Path) -> None:
    db = sqlite3.connect(base / "app.db")
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO articles (
                article_id, title, account, author, publish_time, source_url,
                canonical_url, article_dir, downloaded_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meta.get("article_id"),
                meta.get("title"),
                meta.get("account"),
                meta.get("author"),
                meta.get("publish_time"),
                meta.get("source_url"),
                meta.get("canonical_url"),
                str(article_dir),
                meta.get("downloaded_at"),
                meta.get("content_hash"),
            ),
        )
        db.commit()
    finally:
        db.close()


def db_insert_run(base: Path, manifest: dict[str, Any]) -> None:
    db = sqlite3.connect(base / "app.db")
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, created_at, run_dir, success_count, failure_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                manifest["run_id"],
                manifest["created_at"],
                manifest["run_dir"],
                manifest["success_count"],
                manifest["failure_count"],
            ),
        )
        db.commit()
    finally:
        db.close()


def db_list_articles(base: Path, limit: int = 100) -> list[dict[str, Any]]:
    db = sqlite3.connect(base / "app.db")
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            """
            SELECT article_id, title, account, author, publish_time, source_url,
                   article_dir, downloaded_at
            FROM articles
            ORDER BY downloaded_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def db_get_article(base: Path, article_id: str) -> dict[str, Any] | None:
    db = sqlite3.connect(base / "app.db")
    db.row_factory = sqlite3.Row
    try:
        row = db.execute(
            "SELECT * FROM articles WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def clean_url(url: str) -> str:
    url = html.unescape(url).strip().rstrip(".,;)")
    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.lower() != "mp.weixin.qq.com":
        raise ValueError(f"not a WeChat article URL: {url}")
    safe_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in SENSITIVE_QUERY_KEYS or "token" in lowered or "ticket" in lowered:
            continue
        safe_query.append((key, value))
    safe_fragment = parsed.fragment if parsed.fragment == "wechat_redirect" else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query, doseq=True), safe_fragment)
    )


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in SENSITIVE_QUERY_KEYS or "token" in lowered or "ticket" in lowered or lowered in {"cookie", "key"}


def sanitize_text_urls(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        try:
            return clean_url(match.group(0))
        except ValueError:
            return match.group(0)

    return ARTICLE_URL_RE.sub(replace, value)


def scrub_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): scrub_payload(item) for key, item in value.items() if not is_sensitive_key(str(key))}
    if isinstance(value, list):
        return [scrub_payload(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_payload(item) for item in value]
    if isinstance(value, str):
        return sanitize_text_urls(value)
    return value


def safe_display_url(url: str) -> str:
    try:
        return clean_url(url)
    except ValueError:
        return sanitize_text_urls(str(url))


def parse_query_values(url: str) -> dict[str, list[str]]:
    parsed = urllib.parse.urlsplit(url)
    values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if parsed.fragment and "=" in parsed.fragment:
        for key, items in urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True).items():
            values.setdefault(key, []).extend(items)
    return values


def first_query_value(values: dict[str, list[str]], key: str) -> str:
    items = values.get(key) or []
    return html.unescape(items[0]).strip() if items else ""


def looks_like_input_path(value: str) -> bool:
    if "\n" in value or "\r" in value:
        return False
    if len(value) > 500:
        return False
    if ARTICLE_URL_RE.search(value):
        return False
    return True


def extract_urls(value: str) -> list[str]:
    text = value
    candidate: Path | None = None
    if looks_like_input_path(value):
        try:
            candidate = Path(value).expanduser()
            exists = candidate.exists()
        except OSError:
            candidate = None
            exists = False
    else:
        exists = False

    if candidate and exists:
        if candidate.suffix.lower() == ".json":
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, list):
                text = "\n".join(str(item) for item in data)
            elif isinstance(data, dict):
                text = json.dumps(data, ensure_ascii=False)
        elif candidate.suffix.lower() == ".csv":
            parts: list[str] = []
            with candidate.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.reader(fh):
                    parts.extend(row)
            text = "\n".join(parts)
        else:
            text = candidate.read_text(encoding="utf-8")

    urls: list[str] = []
    seen: set[str] = set()
    for match in ARTICLE_URL_RE.findall(text):
        try:
            url = clean_url(match)
        except ValueError:
            continue
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def fixture_html_path(url: str) -> Path | None:
    fixture_dir = os.environ.get("MOORE_WECHAT_HTML_FIXTURE_DIR", "").strip()
    if not fixture_dir:
        return None
    base = Path(fixture_dir).expanduser()
    parsed = urllib.parse.urlsplit(url)
    slug = Path(parsed.path).name
    candidates = []
    if slug:
        candidates.append(base / f"{slug}.html")
    candidates.append(base / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.html")
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def fetch_text(url: str, timeout: int = 20, ua: str | None = None) -> str:
    fixture = fixture_html_path(url)
    if fixture:
        return fixture.read_text(encoding="utf-8")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua or WECHAT_HISTORY_USER_AGENT,
            "Referer": "https://mp.weixin.qq.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def attr_map(tag: str) -> dict[str, str]:
    return {name.lower(): html.unescape(value) for name, _quote, value in ATTR_RE.findall(tag)}


def first_regex(patterns: list[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.S | re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def first_valid_biz(candidates: list[str]) -> str:
    for candidate in candidates:
        biz = html.unescape(urllib.parse.unquote(str(candidate or ""))).strip()
        if re.fullmatch(r"[A-Za-z0-9_=+\-/]{6,128}", biz) and "${" not in biz:
            return biz
    return ""


def extract_meta(raw_html: str, url: str) -> dict[str, str]:
    title = first_regex(
        [
            r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
            r'<meta\s+content=["\'](.*?)["\']\s+property=["\']og:title["\']',
            r"<title[^>]*>(.*?)</title>",
            r'var\s+msg_title\s*=\s*["\'](.*?)["\']',
        ],
        raw_html,
    )
    title = re.sub(r"\s+", " ", strip_tags(title)).strip() or "untitled"
    if title.endswith("- 微信公众平台"):
        title = title[: -len("- 微信公众平台")].strip()

    account = first_regex(
        [
            r'var\s+nickname\s*=\s*["\'](.*?)["\']',
            r'<meta\s+property=["\']og:article:author["\']\s+content=["\'](.*?)["\']',
            r'id=["\']js_name["\'][^>]*>(.*?)</',
        ],
        raw_html,
    )
    account = re.sub(r"\s+", " ", strip_tags(account)).strip()

    author = first_regex([r'id=["\']js_author_name["\'][^>]*>(.*?)</'], raw_html)
    author = re.sub(r"\s+", " ", strip_tags(author)).strip()

    publish_time = first_regex(
        [
            r'var\s+publish_time\s*=\s*["\'](.*?)["\']',
            r'id=["\']publish_time["\'][^>]*>(.*?)</',
        ],
        raw_html,
    )
    publish_time = re.sub(r"\s+", " ", strip_tags(publish_time)).strip()

    canonical_url = first_regex(
        [
            r'<meta\s+property=["\']og:url["\']\s+content=["\'](.*?)["\']',
            r'<link\s+rel=["\']canonical["\']\s+href=["\'](.*?)["\']',
        ],
        raw_html,
    ) or url

    read_count = first_regex(
        [
            r'var\s+appmsg_read_num\s*=\s*["\']?(\d+)["\']?',
            r'var\s+read_num\s*=\s*["\']?(\d+)["\']?',
        ],
        raw_html,
    )
    like_count = first_regex(
        [
            r'var\s+appmsg_like_num\s*=\s*["\']?(\d+)["\']?',
            r'var\s+like_num\s*=\s*["\']?(\d+)["\']?',
        ],
        raw_html,
    )

    return {
        "title": title,
        "account": account,
        "author": author,
        "publish_time": publish_time,
        "source_url": url,
        "canonical_url": canonical_url,
        "read_count": read_count,
        "like_count": like_count,
    }


def extract_account_clues(raw_html: str, url: str, meta: dict[str, str]) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    candidates = list(query.get("__biz") or [])
    for pattern in [
        r'var\s+biz\s*=\s*["\'](.*?)["\']',
        r'var\s+__biz\s*=\s*["\'](.*?)["\']',
        r'\bbiz\s*:\s*["\'](.*?)["\']',
        r'__biz=([^"&\']+)',
    ]:
        candidates.extend(match.group(1) for match in re.finditer(pattern, raw_html, re.S | re.I))
    biz = first_valid_biz(candidates)
    account_name = meta.get("account") or meta.get("author") or ""
    account_id = biz or hashlib.sha256((account_name + url).encode("utf-8")).hexdigest()[:12]
    return {
        "account_id": account_id,
        "account_name": account_name or "unknown-account",
        "biz": biz,
    }


def find_article_html(raw_html: str) -> str:
    match = re.search(r'<div\b[^>]*id=["\']js_content["\'][^>]*>', raw_html, re.I)
    if not match:
        body = re.search(r"<body[^>]*>(.*?)</body>", raw_html, re.S | re.I)
        return body.group(1).strip() if body else raw_html

    start = match.start()
    depth = 0
    for token in re.finditer(r"</?div\b[^>]*>", raw_html[start:], re.I):
        tag = token.group(0)
        if tag.startswith("</"):
            depth -= 1
        else:
            depth += 1
        if depth == 0:
            end = start + token.end()
            return raw_html[start:end].strip()
    return raw_html[start:].strip()


def remove_scripts_styles(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", "", value, flags=re.S | re.I)
    value = re.sub(r"<style\b.*?</style>", "", value, flags=re.S | re.I)
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    return value


def strip_tags(value: str) -> str:
    value = remove_scripts_styles(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"</div\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def html_to_markdown(value: str) -> str:
    value = remove_scripts_styles(value)
    value = re.sub(r"<h1[^>]*>(.*?)</h1>", lambda m: "\n# " + strip_tags(m.group(1)) + "\n", value, flags=re.S | re.I)
    value = re.sub(r"<h2[^>]*>(.*?)</h2>", lambda m: "\n## " + strip_tags(m.group(1)) + "\n", value, flags=re.S | re.I)
    value = re.sub(r"<h3[^>]*>(.*?)</h3>", lambda m: "\n### " + strip_tags(m.group(1)) + "\n", value, flags=re.S | re.I)

    def img_to_md(match: re.Match[str]) -> str:
        attrs = attr_map(match.group(0))
        src = attrs.get("data-local-src") or attrs.get("data-src") or attrs.get("src") or ""
        alt = attrs.get("alt") or "image"
        return f"\n![{alt}]({src})\n" if src else ""

    def a_to_md(match: re.Match[str]) -> str:
        tag = match.group(0)
        attrs = attr_map(tag)
        href = attrs.get("href", "")
        text = strip_tags(match.group(1))
        return f"[{text}]({href})" if href and text else text

    value = re.sub(r"<a\b[^>]*>(.*?)</a>", a_to_md, value, flags=re.S | re.I)
    value = IMG_RE.sub(img_to_md, value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"</div\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def safe_name(value: str, max_len: int = 90) -> str:
    value = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:max_len].strip() or "untitled")


def seq_name(index: int) -> str:
    return f"{index:03d}"


def markdown_filename(seq: str, title: str) -> str:
    return f"{seq}-{safe_name(title, 72)}.md"


def yaml_string(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def guess_extension(url: str, content_type: str | None) -> str:
    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
        }
        if ctype in mapping:
            return mapping[ctype]
    path = urllib.parse.urlsplit(url).path
    ext = Path(path).suffix.lower()
    return ext if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"] else ".bin"


def asset_host_allowed(url: str) -> tuple[bool, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "unsupported asset scheme"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False, "missing asset host"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, "blocked private or local asset host"
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return False, "blocked localhost asset host"
    if not any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_ASSET_HOST_SUFFIXES):
        return False, "asset host not in allowlist"
    return True, ""


def download_asset(url: str, assets_dir: Path, timeout: int = 20) -> str | None:
    if not url.startswith(("http://", "https://")):
        return None
    allowed, reason = asset_host_allowed(url)
    if not allowed:
        return None
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mp.weixin.qq.com/",
        },
    )
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.lower().startswith("image/"):
                return None
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ASSET_BYTES:
                return None
            data = resp.read(MAX_ASSET_BYTES + 1)
            if len(data) > MAX_ASSET_BYTES:
                return None
            ext = guess_extension(url, content_type)
    except Exception:
        return None
    assets_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{digest}{ext}"
    (assets_dir / filename).write_bytes(data)
    return f"assets/{filename}"


def download_markdown_image(url: str, image_dir: Path, image_seq: int, timeout: int = 20) -> tuple[str | None, str]:
    if not url.startswith(("http://", "https://")):
        return None, "not an absolute http image URL"
    allowed, reason = asset_host_allowed(url)
    if not allowed:
        return None, reason
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mp.weixin.qq.com/",
        },
    )
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.lower().startswith("image/"):
                return None, "response is not an image"
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ASSET_BYTES:
                return None, "image exceeds size limit"
            data = resp.read(MAX_ASSET_BYTES + 1)
            if len(data) > MAX_ASSET_BYTES:
                return None, "image exceeds size limit"
            ext = guess_extension(url, content_type)
    except Exception as exc:
        return None, str(exc)
    image_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{image_seq:03d}{ext}"
    (image_dir / filename).write_bytes(data)
    return filename, ""


def localize_assets(article_html: str, assets_dir: Path, download_assets: bool) -> tuple[str, list[dict[str, str]]]:
    assets: list[dict[str, str]] = []

    def replace_img(match: re.Match[str]) -> str:
        tag = match.group(0)
        attrs = attr_map(tag)
        src = attrs.get("data-src") or attrs.get("src") or ""
        if not src:
            return tag
        local = download_asset(src, assets_dir) if download_assets else None
        if local:
            assets.append({"source_url": src, "local_path": local})
            if "data-local-src" in tag:
                return tag
            return tag[:-1] + f' data-local-src="{html.escape(local)}">'
        allowed, reason = asset_host_allowed(src) if src.startswith(("http://", "https://")) else (False, "not an absolute http asset URL")
        assets.append({"source_url": src, "local_path": "", "error": reason or "download failed or skipped"})
        return tag

    return IMG_RE.sub(replace_img, article_html), assets


def localize_markdown_images(
    article_html: str,
    image_dir: Path,
    markdown_image_dir: str,
    index_image_dir: str,
    download_assets: bool,
) -> tuple[str, list[dict[str, str]]]:
    assets: list[dict[str, str]] = []
    image_counter = 0

    def replace_img(match: re.Match[str]) -> str:
        nonlocal image_counter
        tag = match.group(0)
        attrs = attr_map(tag)
        src = attrs.get("data-src") or attrs.get("src") or ""
        if not src:
            return tag
        image_counter += 1
        if download_assets:
            filename, error = download_markdown_image(src, image_dir, image_counter)
        else:
            filename, error = None, "image download disabled"
        if filename:
            markdown_path = f"{markdown_image_dir}/{filename}"
            index_path = f"{index_image_dir}/{filename}"
            assets.append({"source_url": src, "local_path": index_path})
            if "data-local-src" in tag:
                return re.sub(r'data-local-src=(["\']).*?\1', f'data-local-src="{html.escape(markdown_path)}"', tag)
            return tag[:-1] + f' data-local-src="{html.escape(markdown_path)}">'
        assets.append({"source_url": src, "local_path": "", "error": error or "download failed"})
        return tag

    return IMG_RE.sub(replace_img, article_html), assets


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def session_path(base: Path, session_id: str) -> Path:
    return base / "context" / f"{session_id}.json"


def load_history_session(base: Path, session_id: str) -> dict[str, Any]:
    path = session_path(base, session_id)
    if not path.exists():
        raise FileNotFoundError(f"history session not found: {session_id}")
    return read_json(path)


def save_history_session(base: Path, session: dict[str, Any]) -> None:
    write_json(session_path(base, session["session_id"]), session)


def history_account_dir(base: Path, account_id: str, account_name: str) -> Path:
    return base / "account-history" / f"{safe_name(account_id, 48)}-{safe_name(account_name, 48)}"


def session_ready_marker(base: Path, session_id: str) -> Path:
    return base / "context" / f"{session_id}.ready.json"


def session_proxy_state_path(base: Path, session_id: str) -> Path:
    return base / "context" / f"{session_id}.proxy.json"


def session_proxy_log_path(base: Path, session_id: str) -> Path:
    return base / "context" / f"{session_id}.proxy.log"


def active_proxy_session_path(base: Path) -> Path:
    return base / "context" / "active-proxy-session.json"


def proxy_service_state_path(base: Path) -> Path:
    return base / "context" / "proxy-service.json"


def system_proxy_state_path(base: Path) -> Path:
    return base / "context" / "system-proxy-state.json"


def proxy_snapshot_dir(base: Path, session_id: str) -> Path:
    return base / "proxy-snapshot-runs" / session_id


def auto_snapshot_root(base: Path) -> Path:
    return base / "proxy-snapshots"


def auto_snapshot_index_path(base: Path) -> Path:
    return auto_snapshot_root(base) / "index.jsonl"


def auto_snapshot_processed_path(base: Path) -> Path:
    return auto_snapshot_root(base) / "processed.jsonl"


def safe_context_status(marker: dict[str, Any]) -> dict[str, Any]:
    safe_keys = ["status", "ready", "ready_at", "adapter", "method", "article_count", "history_csv", "history_json"]
    return {key: marker[key] for key in safe_keys if key in marker}


def safe_ready_marker_for_storage(marker: dict[str, Any]) -> dict[str, Any]:
    safe = safe_context_status(marker)
    for key in ["history_articles", "articles"]:
        rows = marker.get(key)
        if isinstance(rows, list):
            safe[key] = [sanitize_history_row(row) for row in rows if isinstance(row, dict)]
    return safe


def session_status(base: Path, session_id: str) -> dict[str, Any]:
    session = load_history_session(base, session_id)
    expires_at = parse_time(session.get("expires_at", ""))
    expired = bool(expires_at and dt.datetime.now(dt.timezone.utc) > expires_at)
    ready_marker = session_ready_marker(base, session_id)
    context_ready = bool(ready_marker.exists()) and not expired
    if ready_marker.exists():
        marker = read_json(ready_marker)
        if not isinstance(marker, dict):
            marker = {}
        safe_marker = safe_ready_marker_for_storage(marker)
        write_json(ready_marker, safe_marker)
        session["context_status"] = safe_context_status(safe_marker)
    session["context_ready"] = context_ready
    session["expired"] = expired
    session["status"] = "ready" if context_ready else ("expired" if expired else "waiting_for_wechat")
    save_history_session(base, session)
    return session


def start_history_session(sample_url: str, base: Path) -> dict[str, Any]:
    ensure_runtime(base)
    cleaned = clean_url(sample_url)
    raw = fetch_text(cleaned)
    meta = extract_meta(raw, cleaned)
    clues = extract_account_clues(raw, cleaned, meta)
    session_id = make_run_id()
    account_dir = history_account_dir(base, clues["account_id"], clues["account_name"])
    account_dir.mkdir(parents=True, exist_ok=True)
    source_article = {
        "sample_url": cleaned,
        "metadata": meta,
        "account": clues,
        "captured_at": utc_now(),
    }
    write_json(account_dir / "source_article.json", source_article)
    expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
    session = {
        "ok": True,
        "session_id": session_id,
        "mode": "account-history",
        "status": "waiting_for_wechat",
        "context_ready": False,
        "created_at": utc_now(),
        "expires_at": expires_at,
        "sample_url": cleaned,
        "account_id": clues["account_id"],
        "account_name": clues["account_name"],
        "biz": clues["biz"],
        "account_dir": str(account_dir),
        "source_article": str(account_dir / "source_article.json"),
        "history_csv": str(account_dir / "history_articles.csv"),
        "history_json": str(account_dir / "history_articles.json"),
        "selected_csv": str(account_dir / "selected_articles.csv"),
        "wechat_desktop_step": [
            "Open the sample article URL in the WeChat desktop client built-in browser.",
            "Open the public-account history page if needed.",
            "Run history-proxy-start, route WeChat traffic through the local proxy, then scroll the history page.",
        ],
    }
    save_history_session(base, session)
    return session


def build_history_open_url(session: dict[str, Any]) -> tuple[str, str]:
    biz = str(session.get("biz") or "").strip()
    if biz:
        query = urllib.parse.urlencode(
            {
                "action": "home",
                "__biz": biz,
                "scene": "124",
            }
        )
        return f"https://mp.weixin.qq.com/mp/profile_ext?{query}#wechat_redirect", "profile_history"
    raise ValueError("cannot build legacy profile_ext history URL because __biz was not extracted from the sample article")


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            return True, "pbcopy"
        if sys.platform.startswith("win"):
            subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard"], input=text, text=True, check=True)
            return True, "powershell Set-Clipboard"
        for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            try:
                subprocess.run(command, input=text, text=True, check=True)
                return True, " ".join(command)
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
    except Exception as exc:
        return False, str(exc)
    return False, "no clipboard command found"


def read_clipboard() -> tuple[str, str]:
    if sys.platform == "darwin":
        result = subprocess.run(["pbpaste"], text=True, capture_output=True, check=True)
        return result.stdout.strip(), "pbpaste"
    if sys.platform.startswith("win"):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip(), "powershell Get-Clipboard"
    for command in (["wl-paste"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=True)
            return result.stdout.strip(), " ".join(command)
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("no clipboard read command found")


def hydrate_history_session_biz(base: Path, session: dict[str, Any]) -> dict[str, Any]:
    if str(session.get("biz") or "").strip():
        return session
    sample_url = str(session.get("sample_url") or "")
    if not sample_url:
        return session
    raw = fetch_text(clean_url(sample_url))
    meta = extract_meta(raw, sample_url)
    clues = extract_account_clues(raw, sample_url, meta)
    if clues.get("biz"):
        session["biz"] = clues["biz"]
        session["account_id"] = clues["account_id"]
        if clues.get("account_name") and clues["account_name"] != "unknown-account":
            session["account_name"] = clues["account_name"]
        save_history_session(base, session)
    return session


def open_history_link(base: Path, session_id: str, copy: bool = True) -> dict[str, Any]:
    session = load_history_session(base, session_id)
    session = hydrate_history_session_biz(base, session)
    open_url, open_url_type = build_history_open_url(session)
    copied = False
    clipboard_method = ""
    clipboard_error = ""
    if copy:
        copied, message = copy_to_clipboard(open_url)
        if copied:
            clipboard_method = message
        else:
            clipboard_error = message
    session["open_url"] = open_url
    session["open_url_type"] = open_url_type
    session["open_url_created_at"] = utc_now()
    save_history_session(base, session)
    return {
        "ok": True,
        "session_id": session_id,
        "account_name": session.get("account_name", ""),
        "open_url": open_url,
        "open_url_type": open_url_type,
        "copied_to_clipboard": copied,
        "clipboard_method": clipboard_method,
        "clipboard_error": clipboard_error,
        "wechat_step": [
            "Send the copied link to File Transfer in WeChat.",
            "Open it with the WeChat desktop built-in browser.",
            "Open the public-account history page if needed.",
            "Keep the local proxy adapter running, then scroll the history page.",
        ],
    }


def load_history_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".json":
        data = read_json(path)
        if isinstance(data, dict):
            rows = data.get("articles", [])
        else:
            rows = data
        return [sanitize_history_row(row) for row in rows if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [sanitize_history_row(row) for row in csv.DictReader(fh)]


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def resolve_session_file(session: dict[str, Any], value: str, default_key: str) -> Path:
    account_dir = Path(str(session["account_dir"])).expanduser()
    candidate = Path(value).expanduser() if value else Path(str(session[default_key])).expanduser()
    if not path_is_within(candidate, account_dir):
        raise ValueError("history file path must stay inside the account-history session directory")
    return candidate


def write_history_rows_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(sanitize_history_row(row))


def sanitize_history_row(row: dict[str, Any]) -> dict[str, str]:
    cleaned = {field: str(row.get(field, "")) for field in HISTORY_FIELDS}
    if cleaned.get("url"):
        cleaned["url"] = safe_display_url(cleaned["url"])
    if cleaned.get("source_article_url"):
        cleaned["source_article_url"] = safe_display_url(cleaned["source_article_url"])
    return cleaned


def parse_selection_numbers(value: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"[,，\s]+", value.strip()):
        if not part:
            continue
        numbers.append(int(part))
    return numbers


def parse_selection_ranges(value: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"[,，\s]+", value.strip()):
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start > end:
                start, end = end, start
            numbers.extend(range(start, end + 1))
        else:
            numbers.append(int(part))
    return numbers


def filter_history_rows(
    rows: list[dict[str, str]],
    latest: int | None = None,
    contains: str = "",
    indices: str = "",
    ranges: str = "",
    titles: str = "",
) -> list[dict[str, str]]:
    filtered = rows
    if contains:
        needle = contains.lower()
        filtered = [row for row in filtered if needle in (row.get("title", "") + row.get("digest", "")).lower()]
    if titles:
        needles = [part.strip().lower() for part in re.split(r"[,，\n]+", titles) if part.strip()]
        if needles:
            filtered = [
                row
                for row in filtered
                if any(needle in row.get("title", "").lower() for needle in needles)
            ]
    selected_positions: list[int] = []
    if indices:
        selected_positions.extend(parse_selection_numbers(indices))
    if ranges:
        selected_positions.extend(parse_selection_ranges(ranges))
    if selected_positions:
        seen: set[int] = set()
        selected: list[dict[str, str]] = []
        for number in selected_positions:
            if number in seen:
                continue
            seen.add(number)
            index = number - 1
            if 0 <= index < len(filtered):
                selected.append(filtered[index])
        filtered = selected
    if latest is not None:
        filtered = filtered[:latest]
    return filtered


def select_history_rows(
    source_path: Path,
    output_path: Path | None,
    latest: int | None,
    contains: str,
    indices: str = "",
    ranges: str = "",
    titles: str = "",
) -> dict[str, Any]:
    rows = load_history_rows(source_path)
    rows = filter_history_rows(rows, latest, contains, indices, ranges, titles)
    target = output_path or (source_path.parent / "selected_articles.csv")
    write_history_rows_csv(target, rows)
    return {
        "ok": True,
        "selected_count": len(rows),
        "selected_csv": str(target),
        "selected_titles": [row.get("title", "") for row in rows],
    }


def preview_history_rows(source_path: Path, limit: int = 30, contains: str = "") -> dict[str, Any]:
    rows = filter_history_rows(load_history_rows(source_path), None, contains)
    shown = rows[:limit]
    lines = []
    articles = []
    for row in shown:
        date = row.get("publish_time", "")
        title = row.get("title", "")
        digest = row.get("digest", "")
        suffix = f" - {digest[:60]}" if digest else ""
        lines.append(f"{date} | {title}{suffix}".strip())
        articles.append(
            {
                "title": title,
                "publish_time": date,
                "digest": digest,
                "url": row.get("url", ""),
            }
        )
    return {
        "ok": True,
        "source": str(source_path),
        "total_count": len(rows),
        "shown_count": len(shown),
        "preview": lines,
        "articles": articles,
    }


def fetch_history_rows_from_context(base: Path, session: dict[str, Any], limit: int) -> dict[str, Any]:
    marker_path = session_ready_marker(base, session["session_id"])
    marker = read_json(marker_path)
    if not isinstance(marker, dict):
        marker = {}
    marker = safe_ready_marker_for_storage(marker)
    rows = marker.get("history_articles") or marker.get("articles") or []
    if rows:
        rows = [sanitize_history_row(row) for row in rows if isinstance(row, dict)]
    else:
        history_source = str(marker.get("history_json") or marker.get("history_csv") or "")
        if history_source:
            source_path = resolve_session_file(session, history_source, "history_json")
            if source_path.exists():
                rows = load_history_rows(source_path)
        else:
            for key in ["history_json", "history_csv"]:
                source_path = Path(str(session.get(key, ""))).expanduser()
                if source_path.exists():
                    rows = load_history_rows(source_path)
                    break
    if not rows:
        return {
            "ok": False,
            "error": "history context is ready, but no history article list was provided by the adapter",
            "session": session,
            "next_step": "Run history-proxy-start, enable the local proxy for WeChat, then open and scroll the WeChat history page.",
        }
    if limit > 0:
        rows = rows[:limit]
    rows = [sanitize_history_row(row) for row in rows]
    history_csv = Path(str(session["history_csv"])).expanduser()
    history_json = Path(str(session["history_json"])).expanduser()
    write_history_rows_csv(history_csv, rows)
    write_json(history_json, {"articles": rows, "fetched_at": utc_now(), "fetch_method": "wechat-desktop-context"})
    safe_marker = safe_context_status(marker)
    safe_marker["article_count"] = len(rows)
    safe_marker["history_csv"] = str(history_csv)
    safe_marker["history_json"] = str(history_json)
    write_json(marker_path, safe_marker)
    session["history_csv"] = str(history_csv)
    session["history_json"] = str(history_json)
    session["history_count"] = len(rows)
    save_history_session(base, session)
    return {
        "ok": True,
        "session_id": session["session_id"],
        "history_count": len(rows),
        "history_csv": str(history_csv),
        "history_json": str(history_json),
    }


def validate_wechat_context_url(context_url: str) -> tuple[urllib.parse.SplitResult, dict[str, list[str]]]:
    parsed = urllib.parse.urlsplit(html.unescape(context_url.strip()))
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "mp.weixin.qq.com":
        raise ValueError("context URL must be an mp.weixin.qq.com URL copied from WeChat")
    values = parse_query_values(context_url)
    return parsed, values


def build_history_getmsg_url(context_url: str, session: dict[str, Any], offset: int, count: int) -> str:
    _parsed, values = validate_wechat_context_url(context_url)
    biz = first_query_value(values, "__biz") or str(session.get("biz") or "")
    uin = first_query_value(values, "uin")
    key = first_query_value(values, "key")
    pass_ticket = first_query_value(values, "pass_ticket")
    if not biz:
        raise ValueError("context URL is missing __biz; open the public-account history page in WeChat and copy that URL")
    missing = [name for name, value in [("uin", uin), ("key", key), ("pass_ticket", pass_ticket)] if not value]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"context URL is missing {joined}; copy the current URL from the WeChat built-in browser history page")
    query = {
        "action": "getmsg",
        "__biz": biz,
        "f": "json",
        "offset": str(offset),
        "count": str(max(1, min(count, 10))),
        "is_ok": "1",
        "scene": first_query_value(values, "scene") or "124",
        "uin": uin,
        "key": key,
        "pass_ticket": pass_ticket,
        "wxtoken": first_query_value(values, "wxtoken") or "",
        "x5": first_query_value(values, "x5") or "0",
    }
    return "https://mp.weixin.qq.com/mp/profile_ext?" + urllib.parse.urlencode(query)


def fetch_json_url(url: str, referer: str, timeout: int = 20) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": WECHAT_HISTORY_USER_AGENT,
            "Referer": referer,
            "Accept": "application/json,text/javascript,*/*;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("WeChat history endpoint did not return JSON; the context may be expired") from exc
    if not isinstance(data, dict):
        raise RuntimeError("WeChat history endpoint returned an unexpected payload")
    return data


def normalize_history_article_url(url: str) -> str:
    url = html.unescape(str(url or "")).strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://mp.weixin.qq.com" + url
    return safe_display_url(url)


def format_history_publish_time(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def rows_from_general_msg_list(payload: dict[str, Any], session: dict[str, Any]) -> list[dict[str, str]]:
    raw_list = payload.get("general_msg_list") or ""
    if isinstance(raw_list, str):
        try:
            msg_list = json.loads(raw_list)
        except json.JSONDecodeError as exc:
            raise RuntimeError("WeChat history payload contains an invalid article list") from exc
    elif isinstance(raw_list, dict):
        msg_list = raw_list
    else:
        msg_list = {}
    items = msg_list.get("list") if isinstance(msg_list, dict) else []
    if not isinstance(items, list):
        return []

    rows: list[dict[str, str]] = []
    account_name = str(session.get("account_name") or "")
    account_id = str(session.get("account_id") or session.get("biz") or "")
    for item in items:
        if not isinstance(item, dict):
            continue
        comm = item.get("comm_msg_info") if isinstance(item.get("comm_msg_info"), dict) else {}
        publish_time = format_history_publish_time(comm.get("datetime"))
        ext = item.get("app_msg_ext_info") if isinstance(item.get("app_msg_ext_info"), dict) else {}
        article_items = [ext]
        multi = ext.get("multi_app_msg_item_list") if isinstance(ext, dict) else []
        if isinstance(multi, list):
            article_items.extend(part for part in multi if isinstance(part, dict))
        for article in article_items:
            title = str(article.get("title") or "").strip()
            url = normalize_history_article_url(str(article.get("content_url") or ""))
            if not title or not url:
                continue
            rows.append(
                sanitize_history_row(
                    {
                        "account_name": account_name,
                        "account_id": account_id,
                        "title": title,
                        "url": url,
                        "publish_time": publish_time,
                        "digest": str(article.get("digest") or "").strip(),
                        "cover": normalize_history_article_url(str(article.get("cover") or article.get("cover_235_1") or "")),
                        "source_article_url": str(session.get("sample_url") or ""),
                        "fetch_method": "wechat-manual-context-url",
                    }
                )
            )
    return rows


def dedupe_history_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        key = row.get("url") or row.get("title") or json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def fetch_history_rows_with_context_url(
    base: Path,
    session: dict[str, Any],
    context_url: str,
    limit: int,
) -> dict[str, Any]:
    validate_wechat_context_url(context_url)
    rows: list[dict[str, str]] = []
    offset = 0
    page_size = 10
    max_rows = max(limit, 0)
    while True:
        if max_rows and len(rows) >= max_rows:
            break
        getmsg_url = build_history_getmsg_url(context_url, session, offset, page_size)
        payload = fetch_json_url(getmsg_url, context_url)
        ret = payload.get("ret", 0)
        if str(ret) not in {"0", ""}:
            base_resp = payload.get("base_resp") if isinstance(payload.get("base_resp"), dict) else {}
            message = str(payload.get("errmsg") or base_resp.get("errmsg") or "WeChat rejected the history request")
            raise RuntimeError(f"WeChat history request failed: {message}")
        page_rows = rows_from_general_msg_list(payload, session)
        if not page_rows:
            break
        rows.extend(page_rows)
        rows = dedupe_history_rows(rows)
        if max_rows and len(rows) >= max_rows:
            rows = rows[:max_rows]
            break
        can_continue = str(payload.get("can_msg_continue", "0")) == "1"
        next_offset = payload.get("next_offset")
        try:
            next_offset_i = int(next_offset)
        except (TypeError, ValueError):
            next_offset_i = offset + page_size
        if not can_continue or next_offset_i <= offset:
            break
        offset = next_offset_i

    if not rows:
        raise RuntimeError("No history articles were returned; the WeChat context may be expired or not on the account history page")

    history_csv = Path(str(session["history_csv"])).expanduser()
    history_json = Path(str(session["history_json"])).expanduser()
    write_history_rows_csv(history_csv, rows)
    write_json(history_json, {"articles": rows, "fetched_at": utc_now(), "fetch_method": "wechat-manual-context-url"})

    marker_path = session_ready_marker(base, session["session_id"])
    marker = {
        "status": "ready",
        "ready": True,
        "ready_at": utc_now(),
        "adapter": "manual-context-url",
        "method": "wechat-profile-ext-getmsg",
        "article_count": len(rows),
        "history_csv": str(history_csv),
        "history_json": str(history_json),
    }
    write_json(marker_path, marker)
    session["history_csv"] = str(history_csv)
    session["history_json"] = str(history_json)
    session["history_count"] = len(rows)
    session["context_ready"] = True
    session["status"] = "ready"
    save_history_session(base, session)
    return {
        "ok": True,
        "session_id": session["session_id"],
        "history_count": len(rows),
        "history_csv": str(history_csv),
        "history_json": str(history_json),
        "context_stored": False,
        "note": "Credential query parameters were used once in memory and were not written to the ready marker.",
    }


def chrome_time_from_datetime(value: dt.datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    epoch = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
    return int((value.astimezone(dt.timezone.utc) - epoch).total_seconds() * 1_000_000)


def find_wechat_history_dbs(root: Path = WECHAT_RADIUM_DIR) -> list[Path]:
    if not root.exists():
        return []
    dbs: list[Path] = []
    for path in root.glob("web/profiles/**/History*"):
        if path.is_file() and path.name.lower().startswith("history"):
            dbs.append(path)
    return dbs


def read_wechat_history_shortlinks(since_chrome_time: int, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for db_path in find_wechat_history_dbs():
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = Path(tmp.name)
            shutil.copy2(db_path, tmp_path)
            db = sqlite3.connect(tmp_path)
            try:
                for url, title, last_visit_time in db.execute(
                    """
                    SELECT url, title, last_visit_time
                    FROM urls
                    WHERE url LIKE 'https://mp.weixin.qq.com/s/%'
                      AND last_visit_time >= ?
                    ORDER BY last_visit_time DESC
                    LIMIT ?
                    """,
                    (since_chrome_time, max(limit * 3, limit, 50)),
                ):
                    rows.append(
                        {
                            "url": str(url or ""),
                            "title": str(title or ""),
                            "last_visit_time": int(last_visit_time or 0),
                            "source_db": str(db_path),
                        }
                    )
            finally:
                db.close()
        except Exception:
            continue
        finally:
            if tmp_path:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    rows.sort(key=lambda row: int(row.get("last_visit_time") or 0), reverse=True)
    return rows


def import_history_rows_from_wechat_cache(
    base: Path,
    session: dict[str, Any],
    minutes: int,
    limit: int,
    contains: str = "",
) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(minutes=max(minutes, 1))
    created_at = parse_time(str(session.get("created_at") or ""))
    if created_at:
        start = max(start, created_at)
    since = chrome_time_from_datetime(start)
    candidates = read_wechat_history_shortlinks(since, max(limit, 1))
    needle = contains.lower().strip()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in candidates:
        title = str(item.get("title") or "").strip()
        url = normalize_history_article_url(str(item.get("url") or ""))
        if not title or not url:
            continue
        if needle and needle not in title.lower() and needle not in url.lower():
            continue
        if url in seen:
            continue
        seen.add(url)
        rows.append(
            sanitize_history_row(
                {
                    "account_name": str(session.get("account_name") or ""),
                    "account_id": str(session.get("account_id") or session.get("biz") or ""),
                    "title": title,
                    "url": url,
                    "publish_time": "",
                    "digest": "",
                    "cover": "",
                    "source_article_url": str(session.get("sample_url") or ""),
                    "fetch_method": "wechat-webview-history-cache",
                }
            )
        )
        if len(rows) >= max(limit, 1):
            break

    if not rows:
        return {
            "ok": False,
            "error": "no recent WeChat WebView article shortlinks found",
            "session_id": session["session_id"],
            "scanned_since": start.isoformat(),
            "candidate_count": len(candidates),
            "next_step": "Open the target public account history page in WeChat desktop, scroll the list, then retry history-import-wechat-cache.",
        }

    history_csv = Path(str(session["history_csv"])).expanduser()
    history_json = Path(str(session["history_json"])).expanduser()
    write_history_rows_csv(history_csv, rows)
    write_json(
        history_json,
        {
            "articles": rows,
            "fetched_at": utc_now(),
            "fetch_method": "wechat-webview-history-cache",
            "limitations": [
                "Uses recent WeChat WebView shortlinks; it cannot prove all rows belong to the same account.",
                "Open and scroll only the target account history page before importing for best results.",
            ],
        },
    )
    marker_path = session_ready_marker(base, session["session_id"])
    write_json(
        marker_path,
        {
            "status": "ready",
            "ready": True,
            "ready_at": utc_now(),
            "adapter": "wechat-webview-history-cache",
            "method": "local-history-db-shortlinks",
            "article_count": len(rows),
            "history_csv": str(history_csv),
            "history_json": str(history_json),
        },
    )
    session["history_csv"] = str(history_csv)
    session["history_json"] = str(history_json)
    session["history_count"] = len(rows)
    session["context_ready"] = True
    session["status"] = "ready"
    save_history_session(base, session)
    return {
        "ok": True,
        "session_id": session["session_id"],
        "history_count": len(rows),
        "history_csv": str(history_csv),
        "history_json": str(history_json),
        "scanned_since": start.isoformat(),
        "adapter": "wechat-webview-history-cache",
        "warning": "Cache import is a fallback. Preview the list before selecting articles.",
    }


def process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def history_proxy_process_running(pid: int, port: int) -> bool:
    if not process_running(pid):
        return False
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN"],
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return False
    output = result.stdout.lower()
    return result.returncode == 0 and "mitmdump" in output and re.search(rf":{port}\b", output) is not None


def mitm_addon_path() -> Path:
    return Path(__file__).resolve().parent / "wechat_history_mitm_addon.py"


def normalize_upstream_proxy(value: str) -> str:
    value = str(value or "").strip()
    if not value or value.lower() in {"none", "off", "false", "0"}:
        return ""
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise ValueError("upstream proxy must look like http://host:port")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def upstream_from_saved_proxy_state(base: Path | None, port: int) -> str:
    if not base:
        return ""
    path = system_proxy_state_path(base)
    if not path.exists():
        return ""
    try:
        saved = read_json(path)
    except Exception:
        return ""
    previous = saved.get("previous") if isinstance(saved.get("previous"), dict) else {}
    for key in ("web", "secure_web"):
        proxy = previous.get(key) if isinstance(previous.get(key), dict) else {}
        if not proxy.get("enabled_bool"):
            continue
        server = str(proxy.get("server") or "").strip()
        proxy_port = str(proxy.get("port") or "").strip()
        if not server or not proxy_port or proxy_port == "0":
            continue
        if server in {"127.0.0.1", "localhost"} and proxy_port == str(port):
            continue
        try:
            return normalize_upstream_proxy(f"http://{server}:{proxy_port}")
        except ValueError:
            continue
    return ""


def auto_upstream_proxy(port: int, base: Path | None = None) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        service = choose_network_service("")
        state = get_network_proxy_state(service)
    except Exception:
        return ""
    web = state.get("web", {})
    if not web.get("enabled_bool"):
        return ""
    server = str(web.get("server") or "").strip()
    proxy_port = str(web.get("port") or "").strip()
    if not server or not proxy_port or proxy_port == "0":
        return ""
    if server in {"127.0.0.1", "localhost"} and proxy_port == str(port):
        return upstream_from_saved_proxy_state(base, port)
    return normalize_upstream_proxy(f"http://{server}:{proxy_port}")


def resolve_upstream_proxy(value: str, port: int, base: Path | None = None) -> str:
    value = str(value or "").strip()
    if value.lower() == "auto":
        return auto_upstream_proxy(port, base)
    return normalize_upstream_proxy(value)


def write_active_proxy_session(base: Path, session: dict[str, Any], port: int, pid: int, upstream_proxy: str, state_path: Path) -> None:
    write_json(
        active_proxy_session_path(base),
        {
            "session_id": session["session_id"],
            "account_id": session.get("account_id", ""),
            "account_name": session.get("account_name", ""),
            "port": port,
            "pid": pid,
            "upstream_proxy": upstream_proxy,
            "state": str(state_path),
            "updated_at": utc_now(),
        },
    )


def read_active_proxy_session(base: Path) -> dict[str, Any]:
    path = active_proxy_session_path(base)
    if not path.exists():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def running_history_proxy_on_port(base: Path, port: int) -> tuple[dict[str, Any], Path] | tuple[None, None]:
    context_dir = base / "context"
    if not context_dir.exists():
        return None, None
    for path in sorted(context_dir.glob("*.proxy.json")):
        state = read_json(path)
        if state.get("adapter") != "wechat-history-proxy":
            continue
        if int(state.get("port") or 0) != port:
            continue
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if history_proxy_process_running(pid, port):
            return state, path
    return None, None


def stop_proxy_process(pid: int, port: int) -> bool:
    if not history_proxy_process_running(pid, port):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and history_proxy_process_running(pid, port):
        time.sleep(0.2)
    if history_proxy_process_running(pid, port):
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    return True


def write_proxy_service_state(base: Path, state: dict[str, Any]) -> None:
    payload = {
        "status": state.get("status", "running"),
        "pid": state.get("pid", 0),
        "port": state.get("port", DEFAULT_PROXY_PORT),
        "proxy": state.get("proxy", f"127.0.0.1:{state.get('port', DEFAULT_PROXY_PORT)}"),
        "upstream_proxy": state.get("upstream_proxy", ""),
        "mitmdump": state.get("mitmdump", ""),
        "addon": state.get("addon", ""),
        "mitm_scope": state.get("mitm_scope", "wechat-only"),
        "started_at": state.get("started_at", ""),
        "updated_at": utc_now(),
        "active_session_id": read_active_proxy_session(base).get("session_id", ""),
    }
    write_json(proxy_service_state_path(base), payload)


def start_history_proxy(base: Path, session: dict[str, Any], port: int, limit: int, upstream_proxy: str = "auto") -> dict[str, Any]:
    ensure_runtime(base)
    state_path = session_proxy_state_path(base, session["session_id"])
    try:
        upstream = resolve_upstream_proxy(upstream_proxy, port, base)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if state_path.exists():
        state = read_json(state_path)
        try:
            pid = int(state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        state_port = int(state.get("port", port) or port)
        if history_proxy_process_running(pid, state_port):
            if str(state.get("upstream_proxy") or "") != upstream:
                if system_proxy_points_to_port("", "127.0.0.1", state_port):
                    return {
                        "ok": False,
                        "error": "proxy upstream changed while system proxy points to the local proxy",
                        "requires_proxy_restore": True,
                        "current_upstream_proxy": state.get("upstream_proxy", ""),
                        "desired_upstream_proxy": upstream,
                        "next_step": "Restore system proxy first, then restart the local proxy service.",
                    }
                stop_proxy_process(pid, state_port)
            else:
                write_active_proxy_session(base, session, int(state.get("port", port)), pid, str(state.get("upstream_proxy", "")), state_path)
                write_proxy_service_state(base, state)
                return {
                    "ok": True,
                    "already_running": True,
                    "session_id": session["session_id"],
                    "pid": pid,
                    "port": state.get("port", port),
                    "proxy": f"127.0.0.1:{state.get('port', port)}",
                    "upstream_proxy": state.get("upstream_proxy", ""),
                    "state": str(state_path),
                    "log": state.get("log", str(session_proxy_log_path(base, session["session_id"]))),
                    "next_step": "Set HTTP/HTTPS proxy to 127.0.0.1 on this port, trust the mitmproxy certificate, then open the generated WeChat history link.",
                }

    running_state, running_state_path = running_history_proxy_on_port(base, port)
    if running_state and running_state_path:
        try:
            pid = int(running_state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if str(running_state.get("upstream_proxy") or "") != upstream:
            if system_proxy_points_to_port("", "127.0.0.1", port):
                return {
                    "ok": False,
                    "error": "proxy upstream changed while system proxy points to the local proxy",
                    "requires_proxy_restore": True,
                    "current_upstream_proxy": running_state.get("upstream_proxy", ""),
                    "desired_upstream_proxy": upstream,
                    "next_step": "Restore system proxy first, then restart the local proxy service.",
                }
            stop_proxy_process(pid, port)
        else:
            state = {
                **running_state,
                "ok": True,
                "status": "running",
                "session_id": session["session_id"],
                "switched_from_session_id": running_state.get("session_id", ""),
                "active_session_switched": True,
                "state": str(state_path),
                "updated_at": utc_now(),
            }
            write_json(state_path, state)
            write_active_proxy_session(base, session, port, pid, str(running_state.get("upstream_proxy", "")), state_path)
            write_proxy_service_state(base, state)
            return {
                **state,
                "pid": pid,
                "port": port,
                "proxy": f"127.0.0.1:{port}",
                "log": running_state.get("log", str(session_proxy_log_path(base, str(running_state.get("session_id") or session["session_id"])))),
                "reused_existing_proxy": True,
                "next_step": "The existing local proxy process was reused and pointed at this session. Keep the system proxy unchanged and scroll the WeChat history page.",
            }

    mitmdump = shutil.which("mitmdump")
    if not mitmdump:
        return {
            "ok": False,
            "error": "mitmdump not found",
            "install": "Install mitmproxy first, for example: brew install mitmproxy",
            "next_step": "After installing mitmproxy and trusting its certificate, rerun history-proxy-start.",
        }
    addon = mitm_addon_path()
    if not addon.exists():
        return {"ok": False, "error": f"missing mitm addon: {addon}"}
    log_path = session_proxy_log_path(base, session["session_id"])
    env = os.environ.copy()
    env.update(
        {
            "MOORE_WECHAT_RUNTIME_DIR": str(base),
            "MOORE_WECHAT_SESSION_ID": str(session["session_id"]),
            "MOORE_WECHAT_HISTORY_LIMIT": str(max(limit, 1)),
        }
    )
    log_fh = log_path.open("a", encoding="utf-8")
    cmd = [
        mitmdump,
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
        "--set",
        "block_global=false",
        "--ignore-hosts",
        WECHAT_MITM_HOST_RE,
    ]
    if upstream:
        cmd.extend(["--mode", f"upstream:{upstream}"])
    cmd.extend(["-s", str(addon)])
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    log_fh.close()
    state = {
        "ok": True,
        "status": "running",
        "adapter": "wechat-history-proxy",
        "session_id": session["session_id"],
        "pid": proc.pid,
        "port": port,
        "proxy": f"127.0.0.1:{port}",
        "upstream_proxy": upstream,
        "started_at": utc_now(),
        "log": str(log_path),
        "mitmdump": mitmdump,
        "addon": str(addon),
        "mitm_scope": "wechat-only",
    }
    write_json(state_path, state)
    write_active_proxy_session(base, session, port, proc.pid, upstream, state_path)
    write_proxy_service_state(base, state)
    return {
        **state,
        "state": str(state_path),
        "next_step": "Set HTTP/HTTPS proxy to 127.0.0.1 on this port, trust the mitmproxy certificate, then open the generated WeChat history link and scroll.",
        "upstream_note": (
            f"Outgoing traffic is chained through {upstream}."
            if upstream
            else "No upstream proxy is configured; mitmproxy connects directly."
        ),
        "certificate_step": "With the proxy enabled, open http://mitm.it in a browser and install/trust the mitmproxy certificate if you have not done this before.",
    }


def status_history_proxy(base: Path, session_id: str) -> dict[str, Any]:
    state_path = session_proxy_state_path(base, session_id)
    state = read_json(state_path) if state_path.exists() else {}
    pid = 0
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    running = process_running(pid)
    result: dict[str, Any] = {
        "ok": bool(state),
        "session_id": session_id,
        "running": running,
        "pid": pid,
        "port": state.get("port"),
        "proxy": state.get("proxy"),
        "state": str(state_path),
        "log": state.get("log", str(session_proxy_log_path(base, session_id))),
    }
    ready_marker = session_ready_marker(base, session_id)
    if ready_marker.exists():
        marker = read_json(ready_marker)
        if isinstance(marker, dict):
            result["context_ready"] = True
            result["context_status"] = safe_context_status(marker)
    else:
        result["context_ready"] = False
    if not state:
        result["next_step"] = "Run history-proxy-start first."
    elif running and not result["context_ready"]:
        result["next_step"] = "Keep the proxy enabled, open the generated WeChat history link, enter the account history page, and scroll."
    return result


def proxy_state_points_to_port(state: dict[str, Any], host: str, port: int) -> bool:
    expected_port = str(port)
    for key in ("web", "secure_web"):
        proxy = state.get(key) if isinstance(state.get(key), dict) else {}
        if not proxy.get("enabled_bool"):
            continue
        server = str(proxy.get("server") or "").strip()
        proxy_port = str(proxy.get("port") or "").strip()
        if server in {host, "localhost"} and proxy_port == expected_port:
            return True
    return False


def system_proxy_points_to_port(service: str, host: str, port: int) -> bool:
    if sys.platform != "darwin":
        return False
    selected = choose_network_service(service)
    return proxy_state_points_to_port(get_network_proxy_state(selected), host, port)


def stop_history_proxy(base: Path, session_id: str) -> dict[str, Any]:
    state_path = session_proxy_state_path(base, session_id)
    if not state_path.exists():
        return {"ok": True, "session_id": session_id, "stopped": False, "message": "proxy state not found"}
    state = read_json(state_path)
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    active = read_active_proxy_session(base)
    active_session_id = str(active.get("session_id") or "")
    try:
        active_pid = int(active.get("pid") or 0)
    except (TypeError, ValueError):
        active_pid = 0
    port = int(state.get("port") or 0)
    proxy_alive = bool(port and history_proxy_process_running(pid, port))
    if active_session_id and active_session_id != session_id and active_pid == pid and proxy_alive:
        state["status"] = "detached"
        state["detached_at"] = utc_now()
        state["active_session_id"] = active_session_id
        write_json(state_path, state)
        return {
            "ok": True,
            "session_id": session_id,
            "stopped": False,
            "pid": pid,
            "state": str(state_path),
            "message": "proxy process is reused by another active session; not stopped",
            "active_session_id": active_session_id,
        }
    if port and proxy_alive and system_proxy_points_to_port("", "127.0.0.1", port):
        return {
            "ok": False,
            "session_id": session_id,
            "stopped": False,
            "pid": pid,
            "port": port,
            "requires_proxy_restore": True,
            "state": str(state_path),
            "next_step": "Run history-proxy-disable --yes before stopping this proxy; otherwise system traffic may point at a dead 127.0.0.1 port.",
        }
    stopped = False
    if proxy_alive:
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        deadline = time.time() + 5
        while time.time() < deadline and history_proxy_process_running(pid, port):
            time.sleep(0.2)
        if history_proxy_process_running(pid, port):
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        stopped = True
    state["status"] = "stopped"
    state["stopped_at"] = utc_now()
    write_json(state_path, state)
    return {
        "ok": True,
        "session_id": session_id,
        "stopped": stopped,
        "pid": pid,
        "state": str(state_path),
        "next_step": "Turn off the HTTP/HTTPS proxy in system or network settings if you enabled it manually.",
    }


def service_session(base: Path, port: int) -> dict[str, Any]:
    return {
        "session_id": "proxy-service",
        "account_id": "",
        "account_name": "",
        "sample_url": "",
        "history_csv": str(base / "account-history" / "proxy-service" / "history_articles.csv"),
        "history_json": str(base / "account-history" / "proxy-service" / "history_articles.json"),
        "port": port,
    }


def enhancer_session(base: Path, port: int) -> dict[str, Any]:
    root = auto_snapshot_root(base)
    return {
        "session_id": "proxy-enhancer",
        "mode": "proxy-enhancer",
        "status": "running",
        "created_at": utc_now(),
        "account_id": "",
        "account_name": "",
        "sample_url": "",
        "snapshot_root": str(root),
        "snapshot_index": str(auto_snapshot_index_path(base)),
        "network_jsonl": str(root / "network.jsonl"),
        "port": port,
    }


def start_proxy_service(base: Path, port: int = DEFAULT_PROXY_PORT, upstream_proxy: str = "auto") -> dict[str, Any]:
    ensure_runtime(base)
    setup = proxy_setup_status(port)
    if not setup.get("ok"):
        return {
            "ok": False,
            "stage": "setup",
            "setup": setup,
            "next_step": setup.get("next_step", "Install mitmproxy, then retry proxy-service-start."),
        }
    session = service_session(base, port)
    result = start_history_proxy(base, session, port, 1, upstream_proxy)
    if result.get("ok"):
        write_proxy_service_state(base, result)
    return result


def start_proxy_enhancer(base: Path, port: int = DEFAULT_PROXY_PORT, upstream_proxy: str = "auto") -> dict[str, Any]:
    ensure_runtime(base)
    setup = proxy_setup_status(port)
    if not setup.get("ok"):
        return {
            "ok": False,
            "stage": "setup",
            "setup": setup,
            "next_step": setup.get("next_step", "Install mitmproxy, then retry proxy-enhancer-start."),
        }
    session = enhancer_session(base, port)
    auto_snapshot_root(base).mkdir(parents=True, exist_ok=True)
    save_history_session(base, session)
    result = start_history_proxy(base, session, port, 1, upstream_proxy)
    if result.get("ok"):
        result = {
            **result,
            "mode": "proxy-enhancer",
            "snapshot_root": session["snapshot_root"],
            "snapshot_index": session["snapshot_index"],
            "does_not_modify_system_proxy": True,
            "next_step": (
                f"Route WeChat traffic to 127.0.0.1:{port} once. "
                "Then open any WeChat article; the page should show 保存这篇."
            ),
        }
        write_proxy_service_state(base, result)
    return result


def status_proxy_service(base: Path, port: int = DEFAULT_PROXY_PORT) -> dict[str, Any]:
    ensure_runtime(base)
    state_path = proxy_service_state_path(base)
    saved = read_json(state_path) if state_path.exists() else {}
    running_state, running_state_path = running_history_proxy_on_port(base, port)
    active = read_active_proxy_session(base)
    running = False
    pid = 0
    if running_state:
        try:
            pid = int(running_state.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        running = history_proxy_process_running(pid, port)
    return {
        "ok": True,
        "running": running,
        "pid": pid,
        "port": port,
        "proxy": f"127.0.0.1:{port}",
        "upstream_proxy": (running_state or saved).get("upstream_proxy", ""),
        "state": str(state_path),
        "running_state": str(running_state_path) if running_state_path else "",
        "active_session_id": active.get("session_id", ""),
        "mitm_scope": (running_state or saved).get("mitm_scope", "wechat-only"),
        "system_proxy_points_here": system_proxy_points_to_port("", "127.0.0.1", port),
    }


def status_proxy_enhancer(base: Path, port: int = DEFAULT_PROXY_PORT) -> dict[str, Any]:
    status = status_proxy_service(base, port)
    latest = latest_auto_snapshot(base)
    debug_log = auto_snapshot_root(base) / "debug.jsonl"
    return {
        **status,
        "mode": "proxy-enhancer",
        "snapshot_root": str(auto_snapshot_root(base)),
        "snapshot_index": str(auto_snapshot_index_path(base)),
        "debug_log": str(debug_log),
        "latest_snapshot": latest if latest else {},
        "next_step": (
            "If WeChat is already routed to this proxy, open an article and click 保存这篇."
            if status.get("running")
            else "Run proxy-enhancer-start first."
        ),
    }


def parse_jsonl_tail(path: Path, max_lines: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max_lines:]
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def proxy_enhancer_logs(base: Path, hours: int = 24, limit: int = 80) -> dict[str, Any]:
    path = auto_snapshot_root(base) / "debug.jsonl"
    hours = max(1, min(int(hours or 24), 24))
    limit = max(1, min(int(limit or 80), 500))
    rows = parse_jsonl_tail(path, 5000)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    recent: list[dict[str, Any]] = []
    for row in rows:
        at = parse_time(str(row.get("at") or ""))
        if at and at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        if at and at < cutoff:
            continue
        recent.append(row)
    return {
        "ok": True,
        "mode": "proxy-enhancer",
        "log": str(path),
        "exists": path.exists(),
        "retention_hours": 24,
        "prune_interval_hours": 12,
        "window_hours": hours,
        "event_count": len(recent),
        "events": recent[-limit:],
        "next_step": "Reload the WeChat article, then rerun proxy-enhancer-logs if no script/client events appear.",
    }


def proxy_enhancer_check_ingress(base: Path, port: int = DEFAULT_PROXY_PORT, minutes: int = 10) -> dict[str, Any]:
    status = status_proxy_enhancer(base, port)
    network_path = auto_snapshot_root(base) / "network.jsonl"
    rows = parse_jsonl_tail(network_path, 500)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=max(minutes, 1))
    recent_rows: list[dict[str, Any]] = []
    article_rows: list[dict[str, Any]] = []
    for row in rows:
        at = parse_time(str(row.get("at") or ""))
        if at and at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        if at and at < cutoff:
            continue
        recent_rows.append(row)
        markers = row.get("markers") if isinstance(row.get("markers"), list) else []
        if row.get("host") == "mp.weixin.qq.com" and "article-page" in markers:
            article_rows.append(row)
    latest = article_rows[-1] if article_rows else (recent_rows[-1] if recent_rows else {})
    return {
        "ok": True,
        "mode": "proxy-enhancer",
        "proxy_running": bool(status.get("running")),
        "proxy": f"127.0.0.1:{port}",
        "upstream_proxy": status.get("upstream_proxy", ""),
        "system_proxy_points_here": bool(status.get("system_proxy_points_here")),
        "network_log": str(network_path),
        "window_minutes": max(minutes, 1),
        "recent_request_count": len(recent_rows),
        "recent_article_page_count": len(article_rows),
        "wechat_ingress_detected": bool(recent_rows),
        "article_ingress_detected": bool(article_rows),
        "latest_event": latest,
        "next_step": (
            "Ingress OK. Open the article and click 保存这篇."
            if article_rows
            else "No recent WeChat article request reached 23344. Route WeChat traffic to 127.0.0.1:23344, then reopen the article."
        ),
    }


def installed_proxy_apps() -> dict[str, str]:
    candidates = {
        "v2rayN": [Path("/Applications/v2rayN.app"), Path.home() / "Applications" / "v2rayN.app"],
        "Clash Verge": [Path("/Applications/Clash Verge.app"), Path.home() / "Applications" / "Clash Verge.app"],
        "Proxifier": [Path("/Applications/Proxifier.app"), Path.home() / "Applications" / "Proxifier.app"],
        "Surge": [Path("/Applications/Surge.app"), Path.home() / "Applications" / "Surge.app"],
    }
    found: dict[str, str] = {}
    for name, paths in candidates.items():
        for path in paths:
            if path.exists():
                found[name] = str(path)
                break
    return found


def process_matches(pattern: str) -> list[str]:
    result = subprocess.run(["ps", "ax", "-o", "pid,args"], text=True, capture_output=True, check=False)
    rows = []
    regex = re.compile(pattern, re.I)
    for line in result.stdout.splitlines():
        if regex.search(line) and "wechat_downloader.py" not in line:
            rows.append(line.strip())
    return rows[:20]


def current_system_proxy_summary() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"ok": False, "error": "system proxy summary is implemented for macOS only"}
    try:
        service = choose_network_service("")
        state = get_network_proxy_state(service)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    web = state.get("web") if isinstance(state.get("web"), dict) else {}
    secure = state.get("secure_web") if isinstance(state.get("secure_web"), dict) else {}
    return {
        "ok": True,
        "service": service,
        "web": {
            "enabled": bool(web.get("enabled_bool")),
            "server": web.get("server", ""),
            "port": web.get("port", ""),
        },
        "secure_web": {
            "enabled": bool(secure.get("enabled_bool")),
            "server": secure.get("server", ""),
            "port": secure.get("port", ""),
        },
    }


def proxy_enhancer_route_help(base: Path, port: int = DEFAULT_PROXY_PORT) -> dict[str, Any]:
    status = status_proxy_enhancer(base, port)
    apps = installed_proxy_apps()
    system_proxy = current_system_proxy_summary()
    v2ray_processes = process_matches(r"v2ray|sing-box|xray")
    clash_processes = process_matches(r"clash|mihomo|verge")
    return {
        "ok": True,
        "goal": f"system/WeChat -> 127.0.0.1:{port} -> {status.get('upstream_proxy') or 'direct'} -> outside",
        "proxy_enhancer": {
            "running": bool(status.get("running")),
            "proxy": f"127.0.0.1:{port}",
            "upstream_proxy": status.get("upstream_proxy", ""),
            "system_proxy_points_here": bool(status.get("system_proxy_points_here")),
        },
        "installed_apps": apps,
        "system_proxy": system_proxy,
        "running_proxy_processes": {
            "v2ray_or_sing_box": v2ray_processes,
            "clash_or_mihomo": clash_processes,
        },
        "recommended_path": (
            f"Run proxy-enhancer-session-start --port {port} --upstream-proxy auto --yes. "
            f"This routes system HTTP/HTTPS to 127.0.0.1:{port}; 23344 then chains to the detected upstream proxy or direct."
        ),
        "upstream_auto_rule": "Use current system proxy as upstream when it is not 23344; use saved previous proxy when system proxy is already 23344; otherwise direct.",
        "stop_rule": f"Do not stop 127.0.0.1:{port} while the system proxy points to it. Use proxy-enhancer-session-finish --yes first, or proxy-enhancer-restart --yes for reloads.",
    }


def start_proxy_enhancer_session(
    base: Path,
    port: int = DEFAULT_PROXY_PORT,
    upstream_proxy: str = "auto",
    yes: bool = False,
) -> dict[str, Any]:
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "proxy": f"127.0.0.1:{port}",
            "next_step": "Rerun with --yes to route system HTTP/HTTPS proxy to 23344. It will stay there until proxy-enhancer-session-finish is run.",
        }
    start = start_proxy_enhancer(base, port, upstream_proxy)
    if not start.get("ok"):
        return {"ok": False, "stage": "proxy_enhancer_start", "proxy_enhancer": start}
    already_enabled = system_proxy_points_to_port("", "127.0.0.1", port)
    if already_enabled:
        enable = {
            "ok": True,
            "already_enabled": True,
            "proxy": f"127.0.0.1:{port}",
            "message": "system proxy already points to proxy-enhancer",
        }
    else:
        enable = enable_system_proxy(base, "", "127.0.0.1", port, True)
    if not enable.get("ok"):
        return {"ok": False, "stage": "system_proxy_enable", "proxy_enhancer": start, "enable": enable}
    status = status_proxy_enhancer(base, port)
    return {
        "ok": True,
        "mode": "proxy-enhancer-session",
        "proxy": f"127.0.0.1:{port}",
        "upstream_proxy": start.get("upstream_proxy", ""),
        "system_proxy_points_here": bool(status.get("system_proxy_points_here")),
        "proxy_already_enabled": already_enabled,
        "proxy_enhancer": start,
        "enable": enable,
        "next_step": "Open or reload the WeChat article. The system proxy will stay on 23344 until you explicitly run proxy-enhancer-session-finish.",
    }


def finish_proxy_enhancer_session(base: Path, yes: bool = False) -> dict[str, Any]:
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "next_step": "Rerun with --yes to restore the saved system HTTP/HTTPS proxy. The 23344 proxy service will keep running.",
        }
    restore = disable_system_proxy(base, yes=True)
    return {
        "ok": bool(restore.get("ok")),
        "mode": "proxy-enhancer-session",
        "restore": restore,
        "proxy_service_kept_running": True,
        "next_step": "System proxy restored. The 23344 local enhancer remains available.",
    }


def stop_proxy_service(base: Path, port: int = DEFAULT_PROXY_PORT, yes: bool = False) -> dict[str, Any]:
    ensure_runtime(base)
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "proxy": f"127.0.0.1:{port}",
            "next_step": "Rerun with --yes to stop the local proxy service.",
        }
    if system_proxy_points_to_port("", "127.0.0.1", port):
        return {
            "ok": False,
            "requires_proxy_restore": True,
            "proxy": f"127.0.0.1:{port}",
            "next_step": "Restore system proxy before stopping the local proxy service.",
        }
    running_state, running_state_path = running_history_proxy_on_port(base, port)
    if not running_state:
        state_path = proxy_service_state_path(base)
        if state_path.exists():
            saved = read_json(state_path)
            saved["status"] = "stopped"
            saved["stopped_at"] = utc_now()
            write_json(state_path, saved)
        return {"ok": True, "stopped": False, "message": "proxy service is not running", "port": port}
    pid = int(running_state.get("pid") or 0)
    stopped = stop_proxy_process(pid, port)
    running_state["status"] = "stopped"
    running_state["stopped_at"] = utc_now()
    if running_state_path:
        write_json(running_state_path, running_state)
    write_proxy_service_state(base, running_state)
    return {"ok": True, "stopped": stopped, "pid": pid, "port": port, "state": str(proxy_service_state_path(base))}


def restart_proxy_enhancer_safely(
    base: Path,
    port: int = DEFAULT_PROXY_PORT,
    upstream_proxy: str = "auto",
    yes: bool = False,
) -> dict[str, Any]:
    ensure_runtime(base)
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "proxy": f"127.0.0.1:{port}",
            "next_step": "Rerun with --yes. If system proxy points to 23344, it will first be moved to the upstream proxy, then moved back.",
        }

    running_state, running_state_path = running_history_proxy_on_port(base, port)
    pointed_here = system_proxy_points_to_port("", "127.0.0.1", port)
    service = choose_network_service("") if pointed_here else ""
    bypass: dict[str, Any] = {"applied": False}

    if pointed_here:
        endpoint = parse_proxy_endpoint(str((running_state or {}).get("upstream_proxy") or ""))
        if not endpoint:
            endpoint = saved_previous_proxy_endpoint(base)
        if not endpoint:
            return {
                "ok": False,
                "stage": "bypass",
                "proxy": f"127.0.0.1:{port}",
                "error": "cannot find an upstream proxy to keep network alive during restart",
                "next_step": "Run proxy-enhancer-session-finish --yes first, or provide --upstream-proxy http://host:port.",
            }
        bypass = {
            "applied": True,
            "reason": "system proxy pointed to the local enhancer",
            "temporary_proxy": f"{endpoint[0]}:{endpoint[1]}",
            "set": set_system_proxy_host_port(service, endpoint[0], endpoint[1]),
        }

    stopped = False
    old_pid = 0
    if running_state:
        old_pid = int(running_state.get("pid") or 0)
        stopped = stop_proxy_process(old_pid, port)
        running_state["status"] = "stopped"
        running_state["stopped_at"] = utc_now()
        if running_state_path:
            write_json(running_state_path, running_state)

    start = start_proxy_enhancer(base, port, upstream_proxy)
    restored_to_enhancer: dict[str, Any] = {"applied": False}
    if start.get("ok") and pointed_here:
        restored_to_enhancer = {
            "applied": True,
            "set": set_system_proxy_host_port(service, "127.0.0.1", port),
        }

    return {
        "ok": bool(start.get("ok")),
        "mode": "proxy-enhancer",
        "port": port,
        "old_pid": old_pid,
        "stopped_old_process": stopped,
        "bypass": bypass,
        "start": start,
        "restored_to_enhancer": restored_to_enhancer,
        "system_proxy_points_here": system_proxy_points_to_port("", "127.0.0.1", port),
        "next_step": (
            "Reload the WeChat article; the new enhancer code is active."
            if start.get("ok")
            else "The system proxy was left on the temporary upstream instead of a dead 23344 port."
        ),
    }


def run_networksetup(args: list[str]) -> subprocess.CompletedProcess[str]:
    command = shutil.which("networksetup")
    if not command:
        raise RuntimeError("networksetup not found; proxy enable/disable is currently supported on macOS only")
    return subprocess.run([command, *args], text=True, capture_output=True, check=True)


def list_network_services() -> list[str]:
    result = run_networksetup(["-listallnetworkservices"])
    services: list[str] = []
    for line in result.stdout.splitlines():
        value = line.strip()
        if not value or value.startswith("An asterisk"):
            continue
        if value.startswith("*"):
            continue
        services.append(value)
    return services


def choose_network_service(service: str = "") -> str:
    if service:
        return service
    services = list_network_services()
    for candidate in ["Wi-Fi", "Ethernet", "USB 10/100/1000 LAN", "Thunderbolt Bridge"]:
        if candidate in services:
            return candidate
    if not services:
        raise RuntimeError("no active network services found")
    return services[0]


def parse_networksetup_proxy(output: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = key.strip().lower().replace(" ", "_")
        parsed[normalized] = value.strip()
    enabled = str(parsed.get("enabled", "")).lower()
    parsed["enabled_bool"] = enabled in {"yes", "on", "1", "true"}
    return parsed


def get_network_proxy_state(service: str) -> dict[str, Any]:
    web = parse_networksetup_proxy(run_networksetup(["-getwebproxy", service]).stdout)
    secure = parse_networksetup_proxy(run_networksetup(["-getsecurewebproxy", service]).stdout)
    return {
        "service": service,
        "web": web,
        "secure_web": secure,
    }


def set_proxy_from_state(service: str, kind: str, state: dict[str, Any]) -> None:
    getter = "-setwebproxy" if kind == "web" else "-setsecurewebproxy"
    state_flag = "-setwebproxystate" if kind == "web" else "-setsecurewebproxystate"
    enabled = bool(state.get("enabled_bool"))
    server = str(state.get("server") or "")
    port = str(state.get("port") or "0")
    if enabled and server and port != "0":
        run_networksetup([getter, service, server, port])
        run_networksetup([state_flag, service, "on"])
    else:
        run_networksetup([state_flag, service, "off"])


def install_mitmproxy(yes: bool) -> dict[str, Any]:
    if shutil.which("mitmdump"):
        return {"ok": True, "installed": False, "message": "mitmdump already available"}
    brew = shutil.which("brew")
    if not brew:
        return {
            "ok": False,
            "error": "Homebrew not found",
            "install": "Install Homebrew or install mitmproxy manually, then rerun history-proxy-setup.",
        }
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "command": "brew install mitmproxy",
            "next_step": "Rerun history-proxy-setup with --install --yes to install mitmproxy via Homebrew.",
        }
    result = subprocess.run([brew, "install", "mitmproxy"], text=True, capture_output=True)
    return {
        "ok": result.returncode == 0 and bool(shutil.which("mitmdump")),
        "installed": result.returncode == 0,
        "command": "brew install mitmproxy",
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
    }


def proxy_setup_status(port: int, open_cert_page: bool = False, install: bool = False, yes: bool = False) -> dict[str, Any]:
    install_result: dict[str, Any] | None = None
    if install and not shutil.which("mitmdump"):
        install_result = install_mitmproxy(yes)
    mitmdump = shutil.which("mitmdump")
    mitm_dir = Path.home() / ".mitmproxy"
    cert_files = {
        "pem": str(mitm_dir / "mitmproxy-ca-cert.pem"),
        "cer": str(mitm_dir / "mitmproxy-ca-cert.cer"),
        "p12": str(mitm_dir / "mitmproxy-ca-cert.p12"),
    }
    existing = {name: Path(path).exists() for name, path in cert_files.items()}
    opened = False
    open_error = ""
    if open_cert_page:
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "http://mitm.it"], check=True)
                opened = True
            else:
                open_error = "automatic cert-page opening is implemented for macOS only"
        except Exception as exc:
            open_error = str(exc)
    return {
        "ok": bool(mitmdump),
        "mitmdump": mitmdump or "",
        "install": "" if mitmdump else "Install mitmproxy first, for example: brew install mitmproxy",
        "install_result": install_result,
        "proxy": f"127.0.0.1:{port}",
        "cert_page": "http://mitm.it",
        "cert_files": cert_files,
        "cert_files_exist": existing,
        "opened_cert_page": opened,
        "open_error": open_error,
        "next_step": (
            "Start history-proxy-start, enable the proxy, then open http://mitm.it and trust the certificate."
            if mitmdump
            else "Run history-proxy-setup --install --yes, then rerun history-proxy-setup."
        ),
    }


def enable_system_proxy(base: Path, service: str, host: str, port: int, yes: bool) -> dict[str, Any]:
    selected = choose_network_service(service)
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "service": selected,
            "proxy": f"{host}:{port}",
            "next_step": "Rerun with --yes to modify macOS HTTP/HTTPS proxy settings and save the previous state.",
        }
    state_path = system_proxy_state_path(base)
    previous = get_network_proxy_state(selected)
    payload = {
        "saved_at": utc_now(),
        "service": selected,
        "previous": previous,
        "new": {"host": host, "port": port},
    }
    write_json(state_path, payload)
    run_networksetup(["-setwebproxy", selected, host, str(port)])
    run_networksetup(["-setsecurewebproxy", selected, host, str(port)])
    run_networksetup(["-setwebproxystate", selected, "on"])
    run_networksetup(["-setsecurewebproxystate", selected, "on"])
    return {
        "ok": True,
        "service": selected,
        "proxy": f"{host}:{port}",
        "state": str(state_path),
        "next_step": "Open WeChat desktop history page and scroll. Run history-proxy-disable when finished.",
    }


def disable_system_proxy(base: Path, service: str = "", yes: bool = False) -> dict[str, Any]:
    state_path = system_proxy_state_path(base)
    if not state_path.exists():
        selected = choose_network_service(service)
        if not yes:
            return {
                "ok": False,
                "requires_confirmation": True,
                "service": selected,
                "next_step": "No saved state found. Rerun with --yes to turn HTTP/HTTPS proxy off for this service.",
            }
        run_networksetup(["-setwebproxystate", selected, "off"])
        run_networksetup(["-setsecurewebproxystate", selected, "off"])
        return {"ok": True, "service": selected, "restored": False, "message": "proxy disabled; no saved state was available"}
    saved = read_json(state_path)
    selected = service or str(saved.get("service") or "")
    if not selected:
        selected = choose_network_service("")
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "service": selected,
            "state": str(state_path),
            "next_step": "Rerun with --yes to restore saved HTTP/HTTPS proxy settings.",
        }
    previous = saved.get("previous") if isinstance(saved.get("previous"), dict) else {}
    web = previous.get("web") if isinstance(previous.get("web"), dict) else {}
    secure = previous.get("secure_web") if isinstance(previous.get("secure_web"), dict) else {}
    set_proxy_from_state(selected, "web", web)
    set_proxy_from_state(selected, "secure_web", secure)
    saved["restored_at"] = utc_now()
    write_json(state_path, saved)
    return {
        "ok": True,
        "service": selected,
        "restored": True,
        "state": str(state_path),
    }


def set_system_proxy_host_port(service: str, host: str, port: int) -> dict[str, Any]:
    selected = choose_network_service(service)
    run_networksetup(["-setwebproxy", selected, host, str(port)])
    run_networksetup(["-setsecurewebproxy", selected, host, str(port)])
    run_networksetup(["-setwebproxystate", selected, "on"])
    run_networksetup(["-setsecurewebproxystate", selected, "on"])
    return {"ok": True, "service": selected, "proxy": f"{host}:{port}"}


def parse_proxy_endpoint(value: str) -> tuple[str, int] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.hostname or not parsed.port:
        return None
    return parsed.hostname, int(parsed.port)


def saved_previous_proxy_endpoint(base: Path) -> tuple[str, int] | None:
    state_path = system_proxy_state_path(base)
    if not state_path.exists():
        return None
    try:
        saved = read_json(state_path)
    except Exception:
        return None
    previous = saved.get("previous") if isinstance(saved.get("previous"), dict) else {}
    for key in ("web", "secure_web"):
        item = previous.get(key) if isinstance(previous.get(key), dict) else {}
        if not item.get("enabled_bool"):
            continue
        host = str(item.get("server") or "").strip()
        try:
            port = int(item.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if host and port:
            return host, port
    return None


def write_index_csv(run_dir: Path, articles: list[dict[str, Any]]) -> None:
    with (run_dir / "index.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["article_id", "title", "account", "author", "publish_time", "source_url", "article_dir"],
        )
        writer.writeheader()
        for article in articles:
            writer.writerow(
                {
                    "article_id": article.get("article_id", ""),
                    "title": article.get("title", ""),
                    "account": article.get("account", ""),
                    "author": article.get("author", ""),
                    "publish_time": article.get("publish_time", ""),
                    "source_url": article.get("source_url", ""),
                    "article_dir": article.get("article_dir", ""),
                }
            )


def report_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        "# WeChat Article Download Report",
        "",
        "## Summary",
        "",
        f"- Runtime directory: `{manifest['runtime_dir']}`",
        f"- Run directory: `{manifest['run_dir']}`",
        f"- Success: {manifest['success_count']}",
        f"- Failed: {manifest['failure_count']}",
    ]
    if manifest.get("skipped_formats"):
        lines.append(f"- Skipped optional formats: {', '.join(manifest['skipped_formats'])}")
    lines.extend(["", "## Articles", "", "| Title | Account | URL | Local ID |", "|---|---|---|---|"])
    for article in manifest["articles"]:
        lines.append(
            f"| {article.get('title', '')} | {article.get('account', '')} | "
            f"{article.get('source_url', '')} | `{article.get('article_id', '')}` |"
        )
    lines.extend(["", "## Failures", "", "| URL | Error |", "|---|---|"])
    for item in manifest["failed"]:
        lines.append(f"| {item.get('url', '')} | {item.get('error', '')} |")
    lines.extend(
        [
            "",
            "## Next Actions",
            "",
            "- For known URL batches, use the run manifest and article folders as the source of truth.",
            "- For account-history work, fetch the history list, select rows, then download selected URLs through the normal batch path.",
            "",
        ]
    )
    return "\n".join(lines)


def download_one(url: str, base: Path, formats: set[str], download_assets: bool) -> dict[str, Any]:
    cleaned = clean_url(url)
    raw = fetch_text(cleaned)
    meta = extract_meta(raw, cleaned)
    article_body = find_article_html(raw)
    article_body = remove_scripts_styles(article_body)
    content_hash = hashlib.sha256(article_body.encode("utf-8")).hexdigest()
    article_id = hashlib.sha256((meta["canonical_url"] + content_hash).encode("utf-8")).hexdigest()[:16]
    title_dir = safe_name(meta["title"])
    article_dir = base / "articles" / f"{article_id}-{title_dir}"
    assets_dir = article_dir / "assets"
    article_dir.mkdir(parents=True, exist_ok=True)

    normalized_html, assets = localize_assets(article_body, assets_dir, download_assets)
    markdown = html_to_markdown(normalized_html)
    text = strip_tags(normalized_html)

    full_meta: dict[str, Any] = {
        **meta,
        "article_id": article_id,
        "downloaded_at": utc_now(),
        "content_hash": content_hash,
        "assets": assets,
    }

    (article_dir / "raw.html").write_text(raw, encoding="utf-8")
    (article_dir / "normalized.html").write_text(normalized_html, encoding="utf-8")
    # Canonical source files are always written so later user-owned processing
    # has stable Markdown/Text inputs even when optional formats are skipped.
    (article_dir / "content.md").write_text(markdown + "\n", encoding="utf-8")
    (article_dir / "content.txt").write_text(text + "\n", encoding="utf-8")
    write_json(article_dir / "metadata.json", full_meta)
    db_upsert_article(base, full_meta, article_dir)

    return {
        "article_id": article_id,
        "title": full_meta.get("title", ""),
        "account": full_meta.get("account", ""),
        "author": full_meta.get("author", ""),
        "publish_time": full_meta.get("publish_time", ""),
        "source_url": cleaned,
        "article_dir": str(article_dir),
        "files": {
            "metadata": str(article_dir / "metadata.json"),
            "raw_html": str(article_dir / "raw.html"),
            "normalized_html": str(article_dir / "normalized.html"),
            "markdown": str(article_dir / "content.md"),
            "text": str(article_dir / "content.txt"),
        },
    }


def markdown_frontmatter(meta: dict[str, Any], seq: str, image_dir: str) -> str:
    fields = {
        "seq": seq,
        "article_id": meta.get("article_id", ""),
        "title": meta.get("title", ""),
        "account": meta.get("account", ""),
        "author": meta.get("author", ""),
        "publish_time": meta.get("publish_time", ""),
        "source_url": meta.get("source_url", ""),
        "downloaded_at": meta.get("downloaded_at", ""),
        "image_dir": image_dir,
        "read_count": meta.get("read_count", ""),
        "like_count": meta.get("like_count", ""),
    }
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {yaml_string(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def download_one_markdown_only(
    url: str,
    output_dir: Path,
    seq: str,
    download_assets: bool,
    filename_stem: str = "",
) -> dict[str, Any]:
    cleaned = clean_url(url)
    raw = fetch_text(cleaned)
    meta = extract_meta(raw, cleaned)
    article_body = remove_scripts_styles(find_article_html(raw))
    content_hash = hashlib.sha256(article_body.encode("utf-8")).hexdigest()
    article_id = hashlib.sha256((meta["canonical_url"] + content_hash).encode("utf-8")).hexdigest()[:16]
    if filename_stem:
        safe_stem = safe_name(filename_stem, 90)
        article_rel = f"articles/{safe_stem}.md"
        image_rel = f"images/{safe_stem}"
    else:
        article_rel = f"articles/{markdown_filename(seq, meta['title'])}"
        image_rel = f"images/{seq}"
    article_path = output_dir / article_rel
    image_dir = output_dir / image_rel
    normalized_html, assets = localize_markdown_images(article_body, image_dir, f"../{image_rel}", image_rel, download_assets)
    markdown = markdown_frontmatter(
        {
            **meta,
            "article_id": article_id,
            "downloaded_at": utc_now(),
            "content_hash": content_hash,
        },
        seq,
        f"../images/{seq}",
    )
    markdown += html_to_markdown(normalized_html).strip() + "\n"
    article_path.parent.mkdir(parents=True, exist_ok=True)
    article_path.write_text(markdown, encoding="utf-8")
    image_count = len([asset for asset in assets if asset.get("local_path")])
    image_errors = [asset.get("error", "") for asset in assets if asset.get("error")]
    return {
        "seq": seq,
        "article_id": article_id,
        "title": meta.get("title", ""),
        "account": meta.get("account", ""),
        "source_url": cleaned,
        "markdown_path": article_rel,
        "image_dir": image_rel,
        "image_count": image_count,
        "read_count": meta.get("read_count", ""),
        "like_count": meta.get("like_count", ""),
        "status": "success",
        "error": "; ".join(error for error in image_errors if error),
        "absolute_markdown_path": str(article_path),
        "absolute_image_dir": str(image_dir),
    }


def write_markdown_only_index(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    fields = ["seq", "article_id", "title", "account", "source_url", "markdown_path", "image_dir", "image_count", "read_count", "like_count", "status", "error"]
    with (output_dir / "index.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def run_markdown_only_download(
    urls: list[str],
    output_dir: Path,
    download_assets: bool,
    input_payload: dict[str, Any],
    run_id: str | None = None,
    html_concurrency: int = 1,
    max_retries: int = 0,
    req_per_min: int = 20,
    aggressive: bool = False,
) -> dict[str, Any]:
    run_id = run_id or make_run_id()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "articles").mkdir(parents=True, exist_ok=True)
    (output_dir / "images").mkdir(parents=True, exist_ok=True)
    retry_limit = max(0, min(int(max_retries or 0), 3))
    file_stems = input_payload.get("file_stems") if isinstance(input_payload.get("file_stems"), dict) else {}

    if aggressive:
        throttle = DownloadThrottle(req_per_min=req_per_min or 30, burst=5, inter_delay=(0.5, 1.2), init_workers=3, max_workers=5)
    else:
        throttle = DownloadThrottle(req_per_min=req_per_min or 20, burst=3, inter_delay=(0.8, 2.0), init_workers=2, max_workers=4)

    # html_concurrency overrides throttle's init if explicitly > 1
    if html_concurrency and html_concurrency > 1:
        throttle.current_workers = max(1, min(int(html_concurrency), throttle._max_workers))

    def download_at(index: int, url: str) -> tuple[int, dict[str, Any], bool]:
        seq = seq_name(index)
        last_error = ""
        for attempt in range(retry_limit + 1):
            throttle.acquire()
            try:
                article = download_one_markdown_only(url, output_dir, seq, download_assets, str(file_stems.get(url) or ""))
                if attempt:
                    article["retry_attempts"] = attempt
                throttle.on_success()
                throttle.inter_sleep()
                return index, article, True
            except urllib.error.HTTPError as exc:
                last_error = sanitize_text_urls(str(exc))
                is_rate = exc.code in (429, 503)
                if attempt < retry_limit:
                    if is_rate:
                        throttle.on_rate_error()
                    else:
                        DownloadThrottle.backoff_sleep(attempt)
            except Exception as exc:
                last_error = sanitize_text_urls(str(exc))
                if attempt < retry_limit:
                    DownloadThrottle.backoff_sleep(attempt)
        return index, {
            "seq": seq,
            "article_id": "",
            "title": "",
            "account": "",
            "source_url": safe_display_url(url),
            "markdown_path": "",
            "image_dir": "",
            "image_count": 0,
            "status": "failed",
            "error": last_error,
            "retry_attempts": retry_limit,
        }, False

    results: list[tuple[int, dict[str, Any], bool]] = []
    worker_count = throttle.current_workers
    if worker_count == 1 or len(urls) <= 1:
        results = [download_at(index, url) for index, url in enumerate(urls, 1)]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(download_at, index, url): index
                for index, url in enumerate(urls, 1)
            }
            for future in as_completed(futures):
                results.append(future.result())

    rows: list[dict[str, Any]] = []
    articles: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for _index, row, ok in sorted(results, key=lambda item: item[0]):
        if ok:
            articles.append(row)
        else:
            failed.append(row)
        rows.append(row)
    write_markdown_only_index(output_dir, rows)
    return {
        "ok": not failed,
        "profile": "markdown-only",
        "run_id": run_id,
        "output_dir": str(output_dir),
        "index": str(output_dir / "index.csv"),
        "success_count": len(articles),
        "failure_count": len(failed),
        "html_concurrency": throttle.current_workers,
        "max_retries": retry_limit,
        "retry_attempt_count": sum(int(item.get("retry_attempts") or 0) for item in rows),
        "articles": articles,
        "failed": failed,
        "input": scrub_payload(input_payload),
    }


def run_download(
    urls: list[str],
    base: Path,
    formats: set[str],
    download_assets: bool,
    input_payload: dict[str, Any],
    req_per_min: int = 20,
    aggressive: bool = False,
) -> dict[str, Any]:
    ensure_runtime(base)
    run_id = make_run_id()
    run_dir = base / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "input.json", scrub_payload(input_payload))

    if aggressive:
        throttle = DownloadThrottle(req_per_min=req_per_min or 30, burst=5, inter_delay=(0.5, 1.2), init_workers=1, max_workers=1)
    else:
        throttle = DownloadThrottle(req_per_min=req_per_min or 20, burst=3, inter_delay=(0.8, 2.0), init_workers=1, max_workers=1)

    articles: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for url in urls:
        throttle.acquire()
        try:
            articles.append(download_one(url, base, formats, download_assets))
            throttle.on_success()
            throttle.inter_sleep()
        except urllib.error.HTTPError as exc:
            failed.append({"url": safe_display_url(url), "error": sanitize_text_urls(str(exc))})
            if exc.code in (429, 503):
                throttle.on_rate_error()
        except Exception as exc:
            failed.append({"url": safe_display_url(url), "error": sanitize_text_urls(str(exc))})

    core_formats = {"html", "md", "txt"}
    manifest = {
        "run_id": run_id,
        "created_at": utc_now(),
        "runtime_dir": str(base),
        "run_dir": str(run_dir),
        "requested_formats": sorted(formats),
        "canonical_source_formats": ["html", "md", "txt"],
        "skipped_formats": sorted(fmt for fmt in formats if fmt not in core_formats),
        "success_count": len(articles),
        "failure_count": len(failed),
        "articles": articles,
        "failed": failed,
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(run_dir / "failed.json", failed)
    write_index_csv(run_dir, articles)
    (run_dir / "report.md").write_text(report_markdown(manifest), encoding="utf-8")
    db_insert_run(base, manifest)
    return manifest


def parse_formats(value: str | None) -> set[str]:
    if not value:
        return {"html", "md", "txt"}
    aliases = {"markdown": "md", "text": "txt"}
    return {aliases.get(item.strip().lower(), item.strip().lower()) for item in value.split(",") if item.strip()}


def delivery_dir(path: str, run_id: str) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return (DEFAULT_DELIVERY_DIR / run_id).expanduser().resolve()


def account_delivery_dir(root: str, account: str) -> Path:
    base = Path(root).expanduser().resolve() if root else DEFAULT_DELIVERY_DIR.expanduser().resolve()
    return (base / safe_name(account or "微信文章", 90)).resolve()


def plan_markdown_account_groups(urls: list[str], output_root: str = "") -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for url in urls:
        cleaned = safe_display_url(url)
        account = "微信文章"
        title = ""
        try:
            cleaned = clean_url(url)
            raw = fetch_text(cleaned)
            meta = extract_meta(raw, cleaned)
            account = meta.get("account") or account
            title = meta.get("title") or ""
        except Exception:
            pass
        key = safe_name(account, 90)
        group = groups.setdefault(
            key,
            {
                "account": account,
                "output_dir": str(account_delivery_dir(output_root, account)),
                "urls": [],
                "file_stems": {},
            },
        )
        group["urls"].append(cleaned)
        if title:
            group["file_stems"][cleaned] = safe_name(title, 90)
    for group in groups.values():
        stems = group.get("file_stems", {})
        counts: dict[str, int] = {}
        for stem in stems.values():
            counts[str(stem)] = counts.get(str(stem), 0) + 1
        for url, stem in list(stems.items()):
            if counts.get(str(stem), 0) > 1:
                suffix = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:8]
                stems[url] = safe_name(f"{stem}-{suffix}", 90)
    return list(groups.values())


def run_markdown_only_download_by_account(
    urls: list[str],
    output_root: str,
    download_assets: bool,
    input_payload: dict[str, Any],
    run_id: str | None = None,
    html_concurrency: int = 1,
    max_retries: int = 0,
    req_per_min: int = 20,
    aggressive: bool = False,
) -> dict[str, Any]:
    run_id = run_id or make_run_id()
    groups = plan_markdown_account_groups(urls, output_root)
    if len(groups) == 1:
        group = groups[0]
        return run_markdown_only_download(
            group["urls"],
            Path(group["output_dir"]),
            download_assets,
            {**input_payload, "account": group["account"], "file_stems": group["file_stems"]},
            run_id,
            html_concurrency,
            max_retries,
            req_per_min,
            aggressive,
        )
    results = []
    articles: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for group in groups:
        result = run_markdown_only_download(
            group["urls"],
            Path(group["output_dir"]),
            download_assets,
            {**input_payload, "account": group["account"], "file_stems": group["file_stems"]},
            make_run_id(),
            html_concurrency,
            max_retries,
            req_per_min,
            aggressive,
        )
        result["account"] = group["account"]
        results.append(result)
        articles.extend(result.get("articles", []))
        failed.extend(result.get("failed", []))
    root = Path(output_root).expanduser().resolve() if output_root else DEFAULT_DELIVERY_DIR.expanduser().resolve()
    return {
        "ok": not failed,
        "profile": "markdown-only-multi-account",
        "run_id": run_id,
        "output_dir": str(root),
        "index": "",
        "success_count": len(articles),
        "failure_count": len(failed),
        "html_concurrency": max(1, min(int(html_concurrency or 1), 4)),
        "max_retries": max(0, min(int(max_retries or 0), 3)),
        "retry_attempt_count": sum(int(item.get("retry_attempt_count") or 0) for item in results),
        "articles": articles,
        "failed": failed,
        "results": results,
        "output_dirs": [item.get("output_dir") for item in results],
        "indexes": [item.get("index") for item in results],
        "input": scrub_payload(input_payload),
    }


def print_download_summary(manifest: dict[str, Any]) -> None:
    if manifest.get("profile") == "markdown-only-multi-account":
        print(json.dumps(
            {
                "ok": manifest["failure_count"] == 0,
                "profile": manifest["profile"],
                "run_id": manifest["run_id"],
                "success_count": manifest["success_count"],
                "failure_count": manifest["failure_count"],
                "output_dirs": manifest.get("output_dirs", []),
                "indexes": manifest.get("indexes", []),
                "failed": manifest["failed"],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return
    if manifest.get("profile") == "markdown-only":
        print(json.dumps(
            {
                "ok": manifest["failure_count"] == 0,
                "profile": "markdown-only",
                "run_id": manifest["run_id"],
                "output_dir": manifest["output_dir"],
                "index": manifest["index"],
                "success_count": manifest["success_count"],
                "failure_count": manifest["failure_count"],
                "html_concurrency": manifest.get("html_concurrency", 1),
                "max_retries": manifest.get("max_retries", 0),
                "retry_attempt_count": manifest.get("retry_attempt_count", 0),
                "articles": [
                    {
                        "seq": item["seq"],
                        "title": item["title"],
                        "account": item["account"],
                        "markdown_path": item["absolute_markdown_path"],
                        "image_dir": item["absolute_image_dir"],
                        "image_count": item["image_count"],
                    }
                    for item in manifest["articles"]
                ],
                "failed": manifest["failed"],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return
    print(json.dumps(
        {
            "ok": manifest["failure_count"] == 0,
            "run_id": manifest["run_id"],
            "run_dir": manifest["run_dir"],
            "report": str(Path(manifest["run_dir"]) / "report.md"),
            "success_count": manifest["success_count"],
            "failure_count": manifest["failure_count"],
            "article_ids": [item["article_id"] for item in manifest["articles"]],
        },
        ensure_ascii=False,
        indent=2,
    ))


def run_download_for_args(urls: list[str], args: argparse.Namespace, input_payload: dict[str, Any]) -> dict[str, Any]:
    profile = getattr(args, "profile", "markdown-only")
    req_per_min = getattr(args, "req_per_min", 20)
    aggressive = getattr(args, "aggressive", False)
    if profile == "archive":
        base = runtime_dir(args.runtime_dir)
        formats = parse_formats(args.formats)
        return run_download(urls, base, formats, not args.no_assets, {**input_payload, "profile": profile, "formats": sorted(formats)}, req_per_min, aggressive)
    run_id = make_run_id()
    return run_markdown_only_download_by_account(
        urls,
        getattr(args, "output_dir", ""),
        not args.no_assets,
        {**input_payload, "profile": "markdown-only", "output_root": getattr(args, "output_dir", "")},
        run_id,
        getattr(args, "html_concurrency", 1),
        getattr(args, "max_retries", 0),
        req_per_min,
        aggressive,
    )


def command_download_url(args: argparse.Namespace) -> int:
    manifest = run_download_for_args(
        [args.url],
        args,
        {"mode": "download-url", "url": args.url},
    )
    print_download_summary(manifest)
    return 0 if manifest["failure_count"] == 0 else 1


def command_download_list(args: argparse.Namespace) -> int:
    urls = extract_urls(args.input)
    if not urls:
        print("No WeChat article URLs found.", file=sys.stderr)
        return 2
    manifest = run_download_for_args(
        urls,
        args,
        {"mode": "download-list", "input": args.input, "urls": urls},
    )
    print_download_summary(manifest)
    return 0 if manifest["failure_count"] == 0 else 1


def command_list(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    ensure_runtime(base)
    print(json.dumps({"articles": db_list_articles(base, args.limit)}, ensure_ascii=False, indent=2))
    return 0


def command_history_start(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = start_history_session(args.sample_url, base)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_history_open(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = open_history_link(base, args.session_id, not args.no_copy)
    except ValueError as exc:
        result = {
            "ok": False,
            "session_id": args.session_id,
            "error": str(exc),
            "next_step": "Use a sample article whose HTML exposes __biz, or open the article once through WeChat desktop and retry history-start.",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_adapter_watch(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    deadline = time.time() + max(args.timeout, 0)
    last_status: dict[str, Any] | None = None
    while True:
        try:
            last_status = session_status(base, args.session_id)
            if last_status.get("context_ready"):
                result = fetch_history_rows_from_context(base, last_status, args.limit)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result.get("ok") else 4
        except FileNotFoundError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        if args.timeout <= 0 or time.time() >= deadline:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "adapter context not ready",
                        "session": last_status,
                        "next_step": "Run history-proxy-start, enable the local proxy for WeChat, then open and scroll the WeChat history page.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 3
        time.sleep(max(args.interval, 1))


def command_history_status(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = session_status(base, args.session_id)
    print(json.dumps({"ok": True, "session": result}, ensure_ascii=False, indent=2))
    return 0 if result.get("context_ready") else 1


def command_history_proxy_start(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        session = session_status(base, args.session_id)
        if session.get("expired"):
            print(json.dumps({"ok": False, "error": "history session expired", "session_id": args.session_id}, ensure_ascii=False, indent=2))
            return 3
        result = start_history_proxy(base, session, args.port, args.limit, args.upstream_proxy)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 4
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


def command_history_proxy_setup(args: argparse.Namespace) -> int:
    result = proxy_setup_status(args.port, args.open_cert_page, args.install, args.yes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 4


def command_history_proxy_enable(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = enable_system_proxy(base, args.service or "", args.host, args.port, args.yes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 3
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 4


def command_history_proxy_disable(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = disable_system_proxy(base, args.service or "", args.yes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 3
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 4


def command_history_proxy_status(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = status_history_proxy(base, args.session_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def command_history_proxy_stop(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = stop_history_proxy(base, args.session_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_service_start(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = start_proxy_service(base, args.port, args.upstream_proxy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 4


def command_proxy_service_status(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = status_proxy_service(base, args.port)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def command_proxy_service_stop(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = stop_proxy_service(base, args.port, args.yes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_enhancer_start(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = start_proxy_enhancer(base, args.port, args.upstream_proxy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_enhancer_status(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = status_proxy_enhancer(base, args.port)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_enhancer_stop(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = stop_proxy_service(base, args.port, args.yes)
    result["mode"] = "proxy-enhancer"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_enhancer_restart(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = restart_proxy_enhancer_safely(base, args.port, args.upstream_proxy, args.yes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_enhancer_check_ingress(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = proxy_enhancer_check_ingress(base, args.port, args.minutes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("proxy_running") and result.get("article_ingress_detected") else 1


def command_proxy_enhancer_logs(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = proxy_enhancer_logs(base, args.hours, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_proxy_enhancer_route_help(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = proxy_enhancer_route_help(base, args.port)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_proxy_enhancer_session_start(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = start_proxy_enhancer_session(base, args.port, args.upstream_proxy, args.yes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_enhancer_session_finish(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = finish_proxy_enhancer_session(base, args.yes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_snapshot_list(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = list_auto_snapshots(base, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_snapshot_latest(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    latest = latest_auto_snapshot(base)
    result = {
        "ok": bool(latest),
        "mode": "proxy-enhancer",
        "snapshot": latest,
        "next_step": "Open an article through the enhancer and click 保存这篇." if not latest else "Use snapshot-extract latest to create structured files.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if latest else 3


def command_snapshot_export(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = export_auto_snapshot(base, args.snapshot_id, args.output_dir)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "snapshot_id": args.snapshot_id}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_snapshot_extract(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = extract_auto_snapshot(base, args.snapshot_id, args.output_dir)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "snapshot_id": args.snapshot_id}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def prepare_history_capture(
    base: Path,
    sample_url: str,
    port: int,
    limit: int,
    upstream_proxy: str,
    yes: bool,
    copy: bool = True,
) -> dict[str, Any]:
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "command": "history-capture-prepare <sample-article-url> --yes",
            "next_step": f"Rerun with --yes to start the local capture proxy and temporarily route HTTP/HTTPS traffic through 127.0.0.1:{port}.",
        }
    setup = proxy_setup_status(port)
    if not setup.get("ok"):
        return {
            "ok": False,
            "stage": "setup",
            "setup": setup,
            "next_step": setup.get("next_step", "Install mitmproxy, then retry history-capture-prepare."),
        }
    session = start_history_session(sample_url, base)
    open_result = open_history_link(base, session["session_id"], copy)
    session = session_status(base, session["session_id"])
    proxy = start_history_proxy(base, session, port, limit, upstream_proxy)
    if not proxy.get("ok"):
        return {"ok": False, "stage": "proxy_start", "session_id": session["session_id"], "proxy": proxy}
    proxy_already_enabled = system_proxy_points_to_port("", "127.0.0.1", port)
    if proxy_already_enabled:
        enable = {
            "ok": True,
            "already_enabled": True,
            "proxy": f"127.0.0.1:{port}",
            "message": "system proxy already points to the local history proxy",
        }
    else:
        enable = enable_system_proxy(base, "", "127.0.0.1", port, True)
    if not enable.get("ok"):
        return {"ok": False, "stage": "proxy_enable", "session_id": session["session_id"], "proxy": proxy, "enable": enable}
    return {
        "ok": True,
        "mode": "history-capture",
        "state": "waiting_for_wechat_scroll",
        "session_id": session["session_id"],
        "account_name": session.get("account_name", ""),
        "account_id": session.get("account_id", ""),
        "open_url": open_result["open_url"],
        "copied_to_clipboard": open_result.get("copied_to_clipboard", False),
        "proxy": f"127.0.0.1:{port}",
        "upstream_proxy": proxy.get("upstream_proxy", ""),
        "proxy_already_enabled": proxy_already_enabled,
        "history_csv": session.get("history_csv", ""),
        "history_json": session.get("history_json", ""),
        "next_step": "Open the URL in the WeChat desktop built-in browser, scroll the history list, then run history-capture-finish with this session_id.",
        "wechat_step": [
            "Send open_url to WeChat File Transfer.",
            "Open it with the WeChat desktop built-in browser.",
            "Scroll the account history list.",
            "Reply when finished so the Skill can run history-capture-finish.",
        ],
        "evidence": {
            "mitmdump": setup.get("mitmdump", ""),
            "proxy_pid": proxy.get("pid"),
            "proxy_reused": bool(proxy.get("reused_existing_proxy")),
            "proxy_restore_state": enable.get("state", ""),
        },
    }


def finish_history_capture(base: Path, session_id: str, limit: int, yes: bool, stop_proxy: bool = False) -> dict[str, Any]:
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "command": "history-capture-finish <session-id> --yes",
            "next_step": "Rerun with --yes to restore the system proxy and stop the local capture proxy.",
        }
    status: dict[str, Any] = {}
    preview: dict[str, Any] = {"ok": False, "error": "history context is not ready"}
    try:
        status = session_status(base, session_id)
        if status.get("context_ready"):
            source = Path(str(status.get("history_json") or status.get("history_csv") or "")).expanduser()
            if source.exists():
                preview = preview_history_rows(source, limit)
    except Exception as exc:
        status = {"session_id": session_id, "error": str(exc), "context_ready": False}

    restore: dict[str, Any]
    stop: dict[str, Any] = {"ok": True, "stopped": False, "kept_running": True}
    try:
        restore = disable_system_proxy(base, yes=True)
    except Exception as exc:
        restore = {"ok": False, "error": str(exc)}
    if stop_proxy:
        try:
            stop = stop_history_proxy(base, session_id)
        except Exception as exc:
            stop = {"ok": False, "error": str(exc), "session_id": session_id}

    return {
        "ok": bool(preview.get("ok")) and bool(restore.get("ok")) and bool(stop.get("ok")),
        "mode": "history-capture",
        "session_id": session_id,
        "account_name": status.get("account_name", ""),
        "context_ready": bool(status.get("context_ready")),
        "article_count": preview.get("total_count", 0) if preview.get("ok") else 0,
        "shown_count": preview.get("shown_count", 0) if preview.get("ok") else 0,
        "articles": preview.get("articles", []) if preview.get("ok") else [],
        "history_csv": status.get("history_csv", ""),
        "history_json": status.get("history_json", ""),
        "restore": {
            "ok": bool(restore.get("ok")),
            "service": restore.get("service", ""),
            "restored": restore.get("restored", False),
            "error": restore.get("error", ""),
        },
        "proxy": {
            "ok": bool(stop.get("ok")),
            "stopped": bool(stop.get("stopped")),
            "kept_running": bool(stop.get("kept_running")),
            "error": stop.get("error", ""),
        },
        "stop": {
            "ok": bool(stop.get("ok")),
            "stopped": stop.get("stopped", False),
            "pid": stop.get("pid", 0),
            "error": stop.get("error", ""),
        },
        "next_step": (
            "Preview the history list and select articles to download."
            if preview.get("ok")
            else "No article rows were captured. Re-run prepare, open the old profile_ext URL in WeChat desktop, and scroll the list."
        ),
    }


def create_proxy_snapshot_session(base: Path, article_url: str) -> dict[str, Any]:
    ensure_runtime(base)
    cleaned = clean_url(article_url)
    meta: dict[str, str] = {}
    try:
        raw = fetch_text(cleaned)
        meta = extract_meta(raw, cleaned)
    except Exception:
        meta = {"title": "", "account": "", "author": "", "publish_time": "", "canonical_url": cleaned}
    session_id = make_run_id()
    run_dir = proxy_snapshot_dir(base, session_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "ok": True,
        "session_id": session_id,
        "mode": "proxy-snapshot",
        "status": "waiting_for_wechat_snapshot",
        "created_at": utc_now(),
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat(),
        "article_url": cleaned,
        "sample_url": cleaned,
        "title": meta.get("title", ""),
        "account_name": meta.get("account", ""),
        "author": meta.get("author", ""),
        "publish_time": meta.get("publish_time", ""),
        "run_dir": str(run_dir),
        "raw_html": str(run_dir / "raw.html"),
        "dom_html": str(run_dir / "dom.html"),
        "body_txt": str(run_dir / "body.txt"),
        "js_content_html": str(run_dir / "js_content.html"),
        "comments_dom_html": str(run_dir / "comments_dom.html"),
        "engagement_dom_html": str(run_dir / "engagement_dom.html"),
        "snapshot_json": str(run_dir / "snapshot.json"),
        "network_jsonl": str(run_dir / "network.jsonl"),
        "metrics_json": str(run_dir / "metrics.json"),
        "comments_json": str(run_dir / "comments.json"),
        "style_profile_json": str(run_dir / "style_profile.json"),
        "style_summary_md": str(run_dir / "style_summary.md"),
        "report_md": str(run_dir / "report.md"),
    }
    save_history_session(base, session)
    return session


def prepare_proxy_snapshot(
    base: Path,
    article_url: str,
    port: int,
    upstream_proxy: str,
    yes: bool,
    copy: bool = True,
) -> dict[str, Any]:
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "command": "proxy-snapshot-prepare <article-url> --yes",
            "next_step": f"Rerun with --yes to start/reuse 127.0.0.1:{port} and temporarily route HTTP/HTTPS traffic through it.",
        }
    setup = proxy_setup_status(port)
    if not setup.get("ok"):
        return {
            "ok": False,
            "stage": "setup",
            "setup": setup,
            "next_step": setup.get("next_step", "Install mitmproxy, then retry proxy-snapshot-prepare."),
        }
    session = create_proxy_snapshot_session(base, article_url)
    proxy = start_history_proxy(base, session, port, 1, upstream_proxy)
    if not proxy.get("ok"):
        return {"ok": False, "stage": "proxy_start", "session_id": session["session_id"], "proxy": proxy}
    proxy_already_enabled = system_proxy_points_to_port("", "127.0.0.1", port)
    if proxy_already_enabled:
        enable = {
            "ok": True,
            "already_enabled": True,
            "proxy": f"127.0.0.1:{port}",
            "message": "system proxy already points to the local WeChat proxy",
        }
    else:
        enable = enable_system_proxy(base, "", "127.0.0.1", port, True)
    if not enable.get("ok"):
        return {"ok": False, "stage": "proxy_enable", "session_id": session["session_id"], "proxy": proxy, "enable": enable}
    copied = False
    clipboard_error = ""
    if copy:
        copied, message = copy_to_clipboard(session["article_url"])
        if not copied:
            clipboard_error = message
    return {
        "ok": True,
        "mode": "proxy-snapshot",
        "state": "waiting_for_button_click",
        "session_id": session["session_id"],
        "article_url": session["article_url"],
        "title": session.get("title", ""),
        "account_name": session.get("account_name", ""),
        "run_dir": session["run_dir"],
        "copied_to_clipboard": copied,
        "clipboard_error": clipboard_error,
        "proxy": f"127.0.0.1:{port}",
        "upstream_proxy": proxy.get("upstream_proxy", ""),
        "proxy_already_enabled": proxy_already_enabled,
        "next_step": "Open article_url in the WeChat desktop built-in browser, wait until comments/metrics finish loading if needed, click 保存当前页面, then run proxy-snapshot-finish.",
        "wechat_step": [
            "Send article_url to WeChat File Transfer.",
            "Open it with the WeChat desktop built-in browser.",
            "Wait for the article, comments, and bottom interaction area to load.",
            "Click the injected 保存当前页面 button.",
            "Reply when finished so the Skill can run proxy-snapshot-finish.",
        ],
        "files": {
            "snapshot_json": session["snapshot_json"],
            "network_jsonl": session["network_jsonl"],
            "report_md": session["report_md"],
        },
    }


def proxy_snapshot_status(base: Path, session_id: str) -> dict[str, Any]:
    session = load_history_session(base, session_id)
    run_dir = Path(str(session.get("run_dir") or proxy_snapshot_dir(base, session_id))).expanduser()
    ready_path = session_ready_marker(base, session_id)
    marker = read_json(ready_path) if ready_path.exists() else {}
    files = {
        "raw_html": run_dir / "raw.html",
        "dom_html": run_dir / "dom.html",
        "snapshot_json": run_dir / "snapshot.json",
        "network_jsonl": run_dir / "network.jsonl",
        "metrics_json": run_dir / "metrics.json",
        "comments_json": run_dir / "comments.json",
        "style_profile_json": run_dir / "style_profile.json",
        "report_md": run_dir / "report.md",
    }
    return {
        "ok": True,
        "mode": "proxy-snapshot",
        "session_id": session_id,
        "ready": bool(marker.get("ready")),
        "status": marker.get("status", "waiting_for_button_click"),
        "run_dir": str(run_dir),
        "files": {name: str(path) for name, path in files.items() if path.exists()},
        "missing_files": [name for name, path in files.items() if not path.exists()],
        "report_md": str(files["report_md"]),
        "next_step": (
            "Run proxy-snapshot-finish to restore the system proxy."
            if marker.get("ready")
            else "Open the article in WeChat built-in browser and click 保存当前页面."
        ),
    }


def finish_proxy_snapshot(base: Path, session_id: str, yes: bool, stop_proxy: bool = False) -> dict[str, Any]:
    if not yes:
        return {
            "ok": False,
            "requires_confirmation": True,
            "command": "proxy-snapshot-finish <session-id> --yes",
            "next_step": "Rerun with --yes to restore the system proxy. The local proxy service stays running by default.",
        }
    status = proxy_snapshot_status(base, session_id)
    try:
        restore = disable_system_proxy(base, yes=True)
    except Exception as exc:
        restore = {"ok": False, "error": str(exc)}
    stop: dict[str, Any] = {"ok": True, "stopped": False, "kept_running": True}
    if stop_proxy:
        try:
            stop = stop_history_proxy(base, session_id)
        except Exception as exc:
            stop = {"ok": False, "error": str(exc), "session_id": session_id}
    return {
        "ok": bool(restore.get("ok")) and bool(stop.get("ok")),
        **status,
        "restore": {
            "ok": bool(restore.get("ok")),
            "service": restore.get("service", ""),
            "restored": restore.get("restored", False),
            "error": restore.get("error", ""),
        },
        "proxy": {
            "ok": bool(stop.get("ok")),
            "stopped": bool(stop.get("stopped")),
            "kept_running": bool(stop.get("kept_running")),
            "error": stop.get("error", ""),
        },
    }


def load_auto_snapshots(base: Path) -> list[dict[str, Any]]:
    index_path = auto_snapshot_index_path(base)
    if not index_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    rows.sort(key=lambda row: str(row.get("captured_at") or row.get("created_at") or ""), reverse=True)
    return rows


def latest_auto_snapshot(base: Path) -> dict[str, Any]:
    rows = load_auto_snapshots(base)
    return rows[0] if rows else {}


def list_auto_snapshots(base: Path, limit: int) -> dict[str, Any]:
    rows = load_auto_snapshots(base)
    shown = rows[: max(limit, 1)]
    return {
        "ok": True,
        "mode": "proxy-enhancer",
        "snapshot_root": str(auto_snapshot_root(base)),
        "snapshot_index": str(auto_snapshot_index_path(base)),
        "total_count": len(rows),
        "shown_count": len(shown),
        "snapshots": shown,
    }


def get_auto_snapshot(base: Path, snapshot_id: str) -> dict[str, Any]:
    for row in load_auto_snapshots(base):
        if str(row.get("snapshot_id") or "") == snapshot_id:
            return row
    raise FileNotFoundError(f"snapshot not found: {snapshot_id}")


def export_auto_snapshot(base: Path, snapshot_id: str, output_dir: str = "") -> dict[str, Any]:
    row = get_auto_snapshot(base, snapshot_id)
    source_dir = Path(str(row.get("run_dir") or "")).expanduser()
    if not source_dir.exists() or not source_dir.is_dir():
        raise FileNotFoundError(f"snapshot directory not found: {source_dir}")
    if not output_dir:
        return {
            "ok": True,
            "mode": "proxy-enhancer",
            "snapshot_id": snapshot_id,
            "run_dir": str(source_dir),
            "copied": False,
            "files": sorted(path.name for path in source_dir.iterdir() if path.is_file()),
        }
    destination_root = Path(output_dir).expanduser()
    destination = destination_root / snapshot_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, destination, dirs_exist_ok=True)
    return {
        "ok": True,
        "mode": "proxy-enhancer",
        "snapshot_id": snapshot_id,
        "run_dir": str(source_dir),
        "output_dir": str(destination),
        "copied": True,
    }


def image_sources_from_html(value: str) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for match in IMG_RE.finditer(value or ""):
        attrs = attr_map(match.group(0))
        src = attrs.get("data-local-src") or attrs.get("data-src") or attrs.get("src") or ""
        src = html.unescape(src).strip()
        if not src or src in seen:
            continue
        seen.add(src)
        sources.append(src)
    return sources


def comments_from_dom(value: str) -> dict[str, Any]:
    text = strip_tags(value or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "complete": False,
        "source": "dom",
        "reason": "only_comments_loaded_in_the_page_at_snapshot_time_are_available",
        "html_present": bool((value or "").strip()),
        "text_line_count": len(lines),
        "text_lines": lines,
        "html": value or "",
    }


COMMENT_UI_NOISE = {
    "保存中...",
    "转发",
    "翻译",
    "不喜欢",
    "投诉",
    "首评",
    "暂无留言",
    "已无更多数据",
}
COMMENT_SUMMARY_RE = re.compile(r"^(?:留言\s*)?\d+\s*$")
COMMENT_MESSAGE_SUMMARY_RE = re.compile(r"^\d+\s*条留言$")
COMMENT_LIKE_RE = re.compile(r"^赞\s*(\d*)$")
COMMENT_REPLY_RE = re.compile(r"^(\d+)\s*条回复$")
COMMENT_LOCATION_TIME_RE = re.compile(
    r"^([\u4e00-\u9fa5A-Za-z]{1,12})"
    r"((?:\d{4}年)?\d{1,2}月\d{1,2}日|昨天|今天|\d+\s*(?:分钟前|小时前|天前))$"
)
COMMENT_AUTHOR_TIME_RE = re.compile(
    r"^作者\s*((?:\d{4}年)?\d{1,2}月\d{1,2}日|昨天|今天|刚刚|\d+\s*(?:分钟前|小时前|天前))$"
)


def split_comment_location_time(value: str) -> tuple[str, str] | None:
    stripped = value.strip()
    author_match = COMMENT_AUTHOR_TIME_RE.match(stripped)
    if author_match:
        return "", author_match.group(1).replace(" ", "")
    match = COMMENT_LOCATION_TIME_RE.match(stripped)
    if not match:
        return None
    return match.group(1), match.group(2).replace(" ", "")


def is_comment_noise(line: str) -> bool:
    if line in COMMENT_UI_NOISE:
        return True
    if COMMENT_SUMMARY_RE.match(line):
        return True
    if COMMENT_MESSAGE_SUMMARY_RE.match(line):
        return True
    if line.startswith("留言 ") and COMMENT_SUMMARY_RE.match(line.replace("留言", "", 1).strip()):
        return True
    return False


def parse_comment_like(line: str) -> int | None:
    match = COMMENT_LIKE_RE.match(line.strip())
    if not match:
        return None
    return int(match.group(1) or "0")


def parse_comment_reply_count(line: str) -> int | None:
    match = COMMENT_REPLY_RE.match(line.strip())
    return int(match.group(1)) if match else None


def structured_comments_from_text_lines(lines: list[Any]) -> dict[str, Any]:
    cleaned = [clean_comment_line(line) for line in lines]
    cleaned = [line for line in cleaned if line]
    comments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    seen: set[str] = set()

    def flush() -> None:
        nonlocal current
        if not current:
            return
        content = str(current.get("content") or "").strip()
        author = str(current.get("author") or "").strip()
        if not content or not author:
            current = None
            return
        key = "|".join(
            [
                author,
                str(current.get("location") or ""),
                str(current.get("time") or ""),
                str(current.get("like_count") if current.get("like_count") is not None else ""),
                content,
            ]
        )
        if key not in seen:
            seen.add(key)
            comments.append(current)
        current = None

    i = 0
    while i < len(cleaned):
        line = cleaned[i]
        if is_comment_noise(line):
            i += 1
            continue
        location_time = split_comment_location_time(line)
        if location_time and i > 0:
            author = cleaned[i - 1]
            if author and not is_comment_noise(author) and parse_comment_like(author) is None:
                flush()
                location, time_value = location_time
                current = {
                    "author": author,
                    "location": location,
                    "time": time_value,
                    "like_count": 0,
                    "reply_count": 0,
                    "content": "",
                    "is_author": line.strip().startswith("作者"),
                    "replies": [],
                }
            i += 1
            continue
        if not current:
            i += 1
            continue
        if i + 1 < len(cleaned) and split_comment_location_time(cleaned[i + 1]):
            flush()
            continue
        like_count = parse_comment_like(line)
        if like_count is not None:
            current["like_count"] = like_count
            i += 1
            continue
        reply_count = parse_comment_reply_count(line)
        if reply_count is not None:
            current["reply_count"] = reply_count
            i += 1
            continue
        if line == "作者":
            current["is_author"] = True
            i += 1
            continue
        if split_comment_location_time(line):
            flush()
            continue
        existing = str(current.get("content") or "").strip()
        current["content"] = f"{existing}\n{line}".strip() if existing else line
        i += 1
    flush()
    return {
        "complete": False,
        "source": "text_lines_heuristic",
        "reason": "only_comments_loaded_in_the_page_at_snapshot_time_are_available",
        "count": len(comments),
        "comments": comments,
    }


def read_text_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def snapshot_id_or_latest(base: Path, snapshot_id: str) -> str:
    if snapshot_id and snapshot_id != "latest":
        return snapshot_id
    latest = latest_auto_snapshot(base)
    resolved = str(latest.get("snapshot_id") or "")
    if not resolved:
        raise FileNotFoundError("no snapshots found")
    return resolved


def extract_auto_snapshot(base: Path, snapshot_id: str = "latest", output_dir: str = "") -> dict[str, Any]:
    resolved_id = snapshot_id_or_latest(base, snapshot_id)
    row = get_auto_snapshot(base, resolved_id)
    run_dir = Path(str(row.get("run_dir") or "")).expanduser()
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"snapshot directory not found: {run_dir}")
    out_dir = Path(output_dir).expanduser().resolve() if output_dir else run_dir / "extracted"
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = run_dir / "snapshot.json"
    snapshot = read_json(snapshot_path) if snapshot_path.exists() else {}
    js_content_html = str(snapshot.get("js_content_html") or read_text_if_exists(run_dir / "js_content.html"))
    comments_html = str(snapshot.get("comments_dom_html") or read_text_if_exists(run_dir / "comments_dom.html"))
    engagement_html = str(snapshot.get("engagement_dom_html") or read_text_if_exists(run_dir / "engagement_dom.html"))
    title = str(snapshot.get("title") or row.get("title") or "微信文章").strip()
    account_name = str(snapshot.get("account_name") or row.get("account_name") or "").strip()
    author = str(snapshot.get("author") or row.get("author") or "").strip()
    publish_time = str(snapshot.get("publish_time") or row.get("publish_time") or "").strip()
    raw_url = str(snapshot.get("url") or row.get("url") or "")
    try:
        url = clean_url(raw_url) if raw_url else ""
    except ValueError:
        url = safe_display_url(raw_url)

    metrics_path = run_dir / "metrics.json"
    style_path = run_dir / "style_profile.json"
    metrics = read_json(metrics_path) if metrics_path.exists() else {}
    style_profile = read_json(style_path) if style_path.exists() else {}
    comments = comments_from_dom(comments_html)
    structured_comments = structured_comments_from_text_lines(comments.get("text_lines", []))
    image_sources = image_sources_from_html(js_content_html)

    article_md = "\n".join(
        [
            "---",
            f"title: {title}",
            f"account: {account_name}",
            f"author: {author}",
            f"publish_time: {publish_time}",
            f"url: {url}",
            f"snapshot_id: {resolved_id}",
            "---",
            "",
            f"# {title}",
            "",
            html_to_markdown(js_content_html).strip(),
            "",
        ]
    )
    (out_dir / "article.md").write_text(article_md, encoding="utf-8")
    write_json(out_dir / "comments.json", comments)
    write_json(out_dir / "comments_structured.json", structured_comments)
    write_json(out_dir / "metrics.json", metrics)
    write_json(out_dir / "style_profile.json", style_profile)
    write_json(out_dir / "image_urls.json", {"count": len(image_sources), "images": image_sources})
    (out_dir / "engagement.html").write_text(engagement_html, encoding="utf-8")
    report = "\n".join(
        [
            "# 快照提取报告",
            "",
            f"- 标题：{title or 'missing'}",
            f"- 公众号：{account_name or 'missing'}",
            f"- 发布时间：{publish_time or 'missing'}",
            f"- URL：{url or 'missing'}",
            f"- Snapshot ID：{resolved_id}",
            "",
            "## 产物",
            "",
            "- article.md：正文 Markdown",
            "- comments.json：已加载评论文本和原始评论 DOM",
            "- comments_structured.json：结构化评论列表",
            "- metrics.json：可观察互动数据",
            "- style_profile.json：页面风格特征",
            "- image_urls.json：正文图片 URL 列表",
            "- engagement.html：互动区域 DOM",
            "",
            "## 边界",
            "",
            "- 评论只包含点击保存这篇时页面已加载的内容。",
            "- 图片默认只提取 URL，不在 extract 阶段下载。",
            "- 互动数据只来自页面 DOM/文本可观察结果，缺失字段不推断。",
            "",
        ]
    )
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    return {
        "ok": True,
        "mode": "proxy-enhancer",
        "snapshot_id": resolved_id,
        "run_dir": str(run_dir),
        "output_dir": str(out_dir),
        "files": {
            "article_md": str(out_dir / "article.md"),
            "comments_json": str(out_dir / "comments.json"),
            "comments_structured_json": str(out_dir / "comments_structured.json"),
            "metrics_json": str(out_dir / "metrics.json"),
            "style_profile_json": str(out_dir / "style_profile.json"),
            "image_urls_json": str(out_dir / "image_urls.json"),
            "engagement_html": str(out_dir / "engagement.html"),
            "report_md": str(out_dir / "report.md"),
        },
        "comments_complete": False,
        "structured_comment_count": structured_comments.get("count", 0),
        "image_count": len(image_sources),
    }


def load_processed_snapshots(base: Path) -> dict[str, dict[str, Any]]:
    path = auto_snapshot_processed_path(base)
    if not path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            snapshot_id = str(row.get("snapshot_id") or "")
            if snapshot_id:
                rows[snapshot_id] = row
    return rows


def append_processed_snapshot(base: Path, row: dict[str, Any]) -> None:
    path = auto_snapshot_processed_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(scrub_payload(row), ensure_ascii=False, sort_keys=True) + "\n")


def wechat_url_identity(url: str) -> str:
    try:
        cleaned = clean_url(url)
    except ValueError:
        cleaned = safe_display_url(url)
    parsed = urllib.parse.urlsplit(cleaned)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    parts = []
    for key in ["__biz", "mid", "idx", "sn"]:
        value = (qs.get(key) or [""])[0]
        if value:
            parts.append(f"{key}={value}")
    if parts:
        return "&".join(parts)
    if parsed.path.startswith("/s/"):
        return parsed.path
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def snapshot_metadata(base: Path, snapshot_id: str) -> dict[str, Any]:
    row = get_auto_snapshot(base, snapshot_id)
    run_dir = Path(str(row.get("run_dir") or "")).expanduser().resolve()
    snapshot_root = auto_snapshot_root(base).expanduser().resolve()
    expected_dir = snapshot_root / snapshot_id
    if run_dir != expected_dir:
        raise ValueError(f"snapshot run_dir does not match snapshot_id: {snapshot_id}")
    try:
        run_dir.relative_to(snapshot_root)
    except ValueError as exc:
        raise ValueError(f"snapshot run_dir is outside snapshot root: {run_dir}") from exc
    ready_path = run_dir / "ready.json"
    if not ready_path.exists():
        raise FileNotFoundError(f"snapshot is not ready: {snapshot_id}")
    ready = read_json(ready_path)
    if not isinstance(ready, dict) or not ready.get("ready"):
        raise ValueError(f"snapshot is not marked ready: {snapshot_id}")
    snapshot_path = run_dir / "snapshot.json"
    snapshot = read_json(snapshot_path) if snapshot_path.exists() else {}
    raw_url = str(snapshot.get("url") or row.get("url") or "")
    try:
        url = clean_url(raw_url) if raw_url else ""
    except ValueError:
        url = safe_display_url(raw_url)
    return {
        "snapshot_id": snapshot_id,
        "run_dir": str(run_dir),
        "title": str(snapshot.get("title") or row.get("title") or "微信文章").strip(),
        "account_name": str(snapshot.get("account_name") or row.get("account_name") or "").strip(),
        "author": str(snapshot.get("author") or row.get("author") or "").strip(),
        "publish_time": str(snapshot.get("publish_time") or row.get("publish_time") or "").strip(),
        "captured_at": str(snapshot.get("captured_at") or row.get("captured_at") or "").strip(),
        "url": url,
        "url_identity": wechat_url_identity(url) if url else "",
    }


def snapshot_rows_with_status(base: Path) -> list[dict[str, Any]]:
    processed = load_processed_snapshots(base)
    rows = []
    for row in load_auto_snapshots(base):
        snapshot_id = str(row.get("snapshot_id") or "")
        status = processed.get(snapshot_id)
        item = dict(row)
        item["processed"] = bool(status)
        if status:
            item["processed_status"] = status.get("status", "")
            item["attached_at"] = status.get("attached_at", "")
            item["article_dir"] = status.get("article_dir", "")
            item["match_method"] = status.get("match_method", "")
        rows.append(item)
    return rows


def list_snapshot_inbox(base: Path, limit: int, include_processed: bool = False) -> dict[str, Any]:
    rows = snapshot_rows_with_status(base)
    if not include_processed:
        rows = [row for row in rows if not row.get("processed")]
    shown = rows[: max(limit, 1)]
    return {
        "ok": True,
        "mode": "proxy-enhancer",
        "snapshot_root": str(auto_snapshot_root(base)),
        "processed_index": str(auto_snapshot_processed_path(base)),
        "total_count": len(rows),
        "shown_count": len(shown),
        "snapshots": shown,
    }


def read_index_rows(index_path: Path) -> list[dict[str, str]]:
    if not index_path.exists():
        return []
    with index_path.open("r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def write_index_rows(index_path: Path, rows: list[dict[str, Any]]) -> None:
    base_fields = [
        "seq",
        "article_id",
        "title",
        "account",
        "publish_time",
        "source_url",
        "markdown_path",
        "image_dir",
        "image_count",
        "read_count",
        "like_count",
        "status",
        "error",
        "downloaded_at",
        "source_mode",
        "snapshot_id",
    ]
    fields = list(base_fields)
    for row in rows:
        for key in row.keys():
            if key not in fields:
                fields.append(key)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def find_existing_markdown_for_snapshot(output_root: Path, meta: dict[str, Any]) -> dict[str, Any] | None:
    account_name = str(meta.get("account_name") or "")
    candidate_dirs = []
    if account_name:
        candidate_dirs.append(output_root / safe_name(account_name, 90))
    if output_root.exists():
        candidate_dirs.extend(path for path in output_root.iterdir() if path.is_dir() and path not in candidate_dirs)
    target_url = str(meta.get("url") or "")
    target_identity = str(meta.get("url_identity") or "")
    title = str(meta.get("title") or "")
    publish_time = str(meta.get("publish_time") or "")
    for account_dir in candidate_dirs:
        index_path = account_dir / "index.csv"
        rows = read_index_rows(index_path)
        for row in rows:
            source_url = str(row.get("source_url") or "")
            markdown_rel = str(row.get("markdown_path") or "")
            if not markdown_rel:
                continue
            source_identity = wechat_url_identity(source_url) if source_url else ""
            exact_url = target_url and source_url and safe_display_url(source_url) == safe_display_url(target_url)
            exact_identity = target_identity and source_identity and source_identity == target_identity
            title_match = (
                account_name
                and title
                and str(row.get("account") or "") == account_name
                and str(row.get("title") or "") == title
                and (not publish_time or publish_time == str(row.get("publish_time") or ""))
            )
            if exact_url or exact_identity or title_match:
                markdown_path = account_dir / markdown_rel
                if not markdown_path.exists():
                    continue
                return {
                    "account_dir": account_dir,
                    "index_path": index_path,
                    "markdown_rel": markdown_rel,
                    "markdown_path": markdown_path,
                    "index_row": row,
                    "match_method": "url" if exact_url else ("wechat_id" if exact_identity else "title"),
                }
    return None


def next_index_seq(rows: list[dict[str, Any]]) -> str:
    max_seq = 0
    for row in rows:
        try:
            max_seq = max(max_seq, int(str(row.get("seq") or "0")))
        except ValueError:
            continue
    return seq_name(max_seq + 1)


def metrics_flat(metrics: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, dict):
            flat[key] = value.get("value")
        else:
            flat[key] = value
    return flat


PAGE_DATA_START = "<!-- moore-wechat-page-data:start -->"
PAGE_DATA_END = "<!-- moore-wechat-page-data:end -->"


def metric_value_and_source(metrics: dict[str, Any], key: str) -> tuple[str, str]:
    value = metrics.get(key)
    if isinstance(value, dict):
        raw_value = value.get("value")
        source = str(value.get("source") or "")
    else:
        raw_value = value
        source = ""
    display = "missing" if raw_value is None or raw_value == "" else str(raw_value)
    return display, source or ("missing" if display == "missing" else "snapshot")


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def clean_comment_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def render_page_data_markdown(
    meta: dict[str, Any],
    metrics: dict[str, Any],
    comments: dict[str, Any],
    structured_comments: dict[str, Any],
    attached_at: str,
) -> str:
    metric_labels = [
        ("read_count", "阅读数"),
        ("like_count", "点赞数"),
        ("old_like_count", "在看数"),
        ("comment_count", "评论数"),
        ("favorite_count", "收藏数"),
        ("share_count", "分享数"),
    ]
    lines = [
        PAGE_DATA_START,
        "",
        "## 页面数据",
        "",
        f"- 保存时间：{attached_at or 'missing'}",
        f"- 快照 ID：{meta.get('snapshot_id') or 'missing'}",
        "- 数据边界：只包含点击“保存这篇”时页面已经加载和暴露的内容；页面没暴露的数据标记为 `missing`。",
        "",
        "### 互动数据",
        "",
        "| 字段 | 值 | 来源 |",
        "|---|---:|---|",
    ]
    for key, label in metric_labels:
        value, source = metric_value_and_source(metrics, key)
        lines.append(f"| {label} | {markdown_cell(value)} | {markdown_cell(source)} |")

    text_lines = comments.get("text_lines") if isinstance(comments, dict) else []
    if not isinstance(text_lines, list):
        text_lines = []
    cleaned_lines = [
        line
        for line in (clean_comment_line(item) for item in text_lines)
        if line and not is_comment_noise(line)
    ]
    structured_items = structured_comments.get("comments") if isinstance(structured_comments, dict) else []
    if not isinstance(structured_items, list):
        structured_items = []
    line_count = comments.get("text_line_count", len(cleaned_lines)) if isinstance(comments, dict) else len(cleaned_lines)
    complete = bool(comments.get("complete")) if isinstance(comments, dict) else False
    reason = str(comments.get("reason") or "") if isinstance(comments, dict) else ""

    lines.extend(
        [
            "",
            "### 已加载评论",
            "",
            f"- 完整性：{'完整' if complete else '不完整，仅页面已加载'}",
            f"- 结构化评论数：{len(structured_items)}",
            f"- 原始文本行数：{line_count}",
        ]
    )
    if reason:
        lines.append(f"- 说明：{reason}")
    if structured_items:
        lines.extend(
            [
                "",
                "| 序号 | 昵称 | 地区/时间 | 点赞 | 回复 | 评论 |",
                "|---:|---|---|---:|---:|---|",
            ]
        )
        for index, item in enumerate(structured_items, start=1):
            if not isinstance(item, dict):
                continue
            location_time = " · ".join(
                part
                for part in [str(item.get("location") or "").strip(), str(item.get("time") or "").strip()]
                if part
            )
            author = str(item.get("author") or "missing").strip()
            if item.get("is_author"):
                author = f"{author}（作者）"
            like_count = item.get("like_count")
            reply_count = item.get("reply_count")
            content = str(item.get("content") or "").strip()
            content = re.sub(r"\s*\n\s*", "<br>", content)
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(index),
                        markdown_cell(author),
                        markdown_cell(location_time or "missing"),
                        markdown_cell(0 if like_count in {None, ""} else like_count),
                        markdown_cell(0 if reply_count in {None, ""} else reply_count),
                        markdown_cell(content or "missing"),
                    ]
                )
                + " |"
            )
    elif cleaned_lines:
        lines.extend(["", "> 未能稳定解析评论结构，以下为清洗后的页面文本。", ""])
        lines.append("")
        for line in cleaned_lines:
            lines.append(f"- {line}")
    else:
        lines.extend(["", "- missing"])
    lines.extend(["", PAGE_DATA_END, ""])
    return "\n".join(lines)


def upsert_page_data_section(markdown_path: Path, page_data: str) -> bool:
    if not markdown_path.exists():
        return False
    current = markdown_path.read_text(encoding="utf-8")
    block_re = re.compile(
        rf"\n*{re.escape(PAGE_DATA_START)}.*?{re.escape(PAGE_DATA_END)}\n*",
        re.S,
    )
    if block_re.search(current):
        updated = block_re.sub("\n\n" + page_data.strip() + "\n", current).rstrip() + "\n"
    else:
        updated = current.rstrip() + "\n\n" + page_data.strip() + "\n"
    if updated != current:
        markdown_path.write_text(updated, encoding="utf-8")
    return True


SNAPSHOT_ATTACH_FILES = [
    "article.md",
    "comments.json",
    "comments_structured.json",
    "metrics.json",
    "style_profile.json",
    "image_urls.json",
    "engagement.html",
    "report.md",
]


def copy_attached_snapshot_files(source_dir: Path, destination: Path) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in SNAPSHOT_ATTACH_FILES:
        source = source_dir / name
        if not source.exists() or not source.is_file():
            continue
        shutil.copy2(source, destination / name)
        copied.append(name)
    return copied


def attach_auto_snapshot(
    base: Path,
    snapshot_id: str,
    output_root: str = "",
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    processed = load_processed_snapshots(base)
    if snapshot_id in processed and not force:
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "status": "skipped",
            "reason": "already_processed",
            **processed[snapshot_id],
        }
    meta = snapshot_metadata(base, snapshot_id)
    extracted = extract_auto_snapshot(base, snapshot_id)
    extracted_dir = Path(str(extracted["output_dir"]))
    output_base = Path(output_root).expanduser().resolve() if output_root else DEFAULT_DELIVERY_DIR.expanduser().resolve()
    account_name = str(meta.get("account_name") or "_unknown").strip() or "_unknown"
    account_dir = account_delivery_dir(str(output_base), account_name)
    existing = find_existing_markdown_for_snapshot(output_base, meta)
    title = str(meta.get("title") or "微信文章")
    article_key = str(meta.get("url_identity") or meta.get("url") or title)
    article_hash = hashlib.sha256(article_key.encode("utf-8")).hexdigest()[:8]
    created_from_snapshot = False

    if existing:
        account_dir = Path(existing["account_dir"])
        markdown_path = Path(existing["markdown_path"])
        markdown_rel = str(existing["markdown_rel"])
        article_stem = markdown_path.stem
        match_method = str(existing["match_method"])
    else:
        article_stem = safe_name(f"{title}--{article_hash}", 90)
        markdown_rel = f"articles/{article_stem}.md"
        markdown_path = account_dir / markdown_rel
        if not dry_run:
            account_dir.mkdir(parents=True, exist_ok=True)
            (account_dir / "articles").mkdir(parents=True, exist_ok=True)
            source_article_md = extracted_dir / "article.md"
            if not markdown_path.exists():
                shutil.copy2(source_article_md, markdown_path)
            index_path = account_dir / "index.csv"
            rows = read_index_rows(index_path)
            rows.append(
                {
                    "seq": next_index_seq(rows),
                    "article_id": article_hash,
                    "title": title,
                    "account": account_name if account_name != "_unknown" else "",
                    "publish_time": meta.get("publish_time", ""),
                    "source_url": meta.get("url", ""),
                    "markdown_path": markdown_rel,
                    "image_dir": "",
                    "image_count": extracted.get("image_count", 0),
                    "read_count": "",
                    "like_count": "",
                    "status": "snapshot",
                    "error": "",
                    "downloaded_at": utc_now(),
                    "source_mode": "snapshot",
                    "snapshot_id": snapshot_id,
                }
            )
            write_index_rows(index_path, rows)
        match_method = "created_from_snapshot"
        created_from_snapshot = True

    snapshot_root = account_dir / "snapshots" / article_stem
    snapshot_target = snapshot_root / "snapshots" / snapshot_id
    copied_files: list[str] = []
    if not dry_run:
        copied_files = copy_attached_snapshot_files(extracted_dir, snapshot_target)

    metrics_path = extracted_dir / "metrics.json"
    metrics = read_json(metrics_path) if metrics_path.exists() else {}
    comments_path = extracted_dir / "comments.json"
    comments = read_json(comments_path) if comments_path.exists() else {}
    structured_comments_path = extracted_dir / "comments_structured.json"
    structured_comments = (
        read_json(structured_comments_path)
        if structured_comments_path.exists()
        else structured_comments_from_text_lines(comments.get("text_lines", []) if isinstance(comments, dict) else [])
    )
    attached_at = utc_now()
    page_data = render_page_data_markdown(meta, metrics, comments, structured_comments, attached_at)
    markdown_embedded = False
    latest = {
        "snapshot_id": snapshot_id,
        "attached_at": attached_at,
        "captured_at": meta.get("captured_at", ""),
        "title": title,
        "account_name": account_name,
        "url": meta.get("url", ""),
        "markdown_path": str(markdown_path),
        "snapshot_dir": str(snapshot_target),
        "files": copied_files,
        "metrics": metrics,
        "comments_complete": bool(comments.get("complete")),
        "loaded_comment_line_count": comments.get("text_line_count", 0),
        "structured_comment_count": structured_comments.get("count", 0) if isinstance(structured_comments, dict) else 0,
        "markdown_embedded": markdown_embedded,
    }
    if dry_run:
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "status": "planned",
            "url": meta.get("url", ""),
            "url_identity": meta.get("url_identity", ""),
            "account_name": account_name,
            "title": title,
            "captured_at": meta.get("captured_at", ""),
            "article_dir": str(account_dir),
            "markdown_path": str(markdown_path),
            "snapshot_dir": str(snapshot_target),
            "files": list(SNAPSHOT_ATTACH_FILES),
            "match_method": match_method,
            "created_from_snapshot": created_from_snapshot,
            "missing": [key for key, value in metrics.items() if isinstance(value, dict) and value.get("source") == "missing"],
            "metrics": metrics,
            "loaded_comment_line_count": comments.get("text_line_count", 0),
            "structured_comment_count": structured_comments.get("count", 0) if isinstance(structured_comments, dict) else 0,
            "markdown_embedded": False,
        }
    markdown_embedded = upsert_page_data_section(markdown_path, page_data)
    latest["markdown_embedded"] = markdown_embedded
    write_json(snapshot_root / "latest.json", latest)
    history_row = {
        "snapshot_id": snapshot_id,
        "captured_at": meta.get("captured_at", ""),
        "attached_at": latest["attached_at"],
        **metrics_flat(metrics),
    }
    with (snapshot_root / "metrics_history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(history_row, ensure_ascii=False, sort_keys=True) + "\n")

    processed_row = {
        "snapshot_id": snapshot_id,
        "url": meta.get("url", ""),
        "url_identity": meta.get("url_identity", ""),
        "account_name": account_name,
        "title": title,
        "captured_at": meta.get("captured_at", ""),
        "attached_at": latest["attached_at"],
        "status": "attached",
        "article_dir": str(account_dir),
        "markdown_path": str(markdown_path),
        "snapshot_dir": str(snapshot_target),
        "files": copied_files,
        "match_method": match_method,
        "created_from_snapshot": created_from_snapshot,
        "missing": [key for key, value in metrics.items() if isinstance(value, dict) and value.get("source") == "missing"],
        "markdown_embedded": markdown_embedded,
    }
    append_processed_snapshot(base, processed_row)
    return {
        "ok": True,
        **processed_row,
        "metrics": metrics,
        "loaded_comment_line_count": comments.get("text_line_count", 0),
        "structured_comment_count": structured_comments.get("count", 0) if isinstance(structured_comments, dict) else 0,
        "markdown_embedded": markdown_embedded,
    }


def select_snapshots_for_attach(base: Path, args: argparse.Namespace) -> list[str]:
    rows = snapshot_rows_with_status(base)
    if args.all_unprocessed:
        return [str(row.get("snapshot_id")) for row in rows if row.get("snapshot_id") and not row.get("processed")]
    if args.since:
        cutoff = parse_time(args.since)
        if not cutoff:
            raise ValueError(f"invalid --since datetime: {args.since}")
        selected = []
        for row in rows:
            captured = parse_time(str(row.get("captured_at") or row.get("created_at") or ""))
            if captured and captured.tzinfo and cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=captured.tzinfo)
            if captured and captured >= cutoff and (args.include_processed or not row.get("processed")):
                selected.append(str(row.get("snapshot_id")))
        return selected
    if args.snapshot_id == "latest":
        return [snapshot_id_or_latest(base, "latest")]
    return [args.snapshot_id]


def attach_snapshots_for_args(base: Path, args: argparse.Namespace) -> dict[str, Any]:
    snapshot_ids = select_snapshots_for_attach(base, args)
    attached = []
    failed = []
    for snapshot_id in snapshot_ids:
        try:
            attached.append(attach_auto_snapshot(base, snapshot_id, args.output_dir, args.force, args.dry_run))
        except Exception as exc:
            failed.append({"snapshot_id": snapshot_id, "error": sanitize_text_urls(str(exc))})
    created = [item for item in attached if item.get("created_from_snapshot")]
    linked = [item for item in attached if item.get("status") in {"attached", "planned"} and not item.get("created_from_snapshot")]
    skipped = [item for item in attached if item.get("status") == "skipped"]
    return {
        "ok": not failed,
        "mode": "proxy-enhancer",
        "dry_run": bool(args.dry_run),
        "requested_count": len(snapshot_ids),
        "attached_count": len([item for item in attached if item.get("status") == "attached"]),
        "planned_count": len([item for item in attached if item.get("status") == "planned"]),
        "linked_existing_count": len(linked),
        "created_from_snapshot_count": len(created),
        "skipped_count": len(skipped),
        "failure_count": len(failed),
        "output_root": str(Path(args.output_dir).expanduser().resolve() if args.output_dir else DEFAULT_DELIVERY_DIR.expanduser().resolve()),
        "attached": attached,
        "failed": failed,
    }


def command_snapshot_inbox(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = list_snapshot_inbox(base, args.limit, args.include_processed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_snapshot_attach(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = attach_snapshots_for_args(base, args)
    except Exception as exc:
        result = {"ok": False, "mode": "proxy-enhancer", "error": sanitize_text_urls(str(exc))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_history_capture_prepare(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = prepare_history_capture(base, args.sample_url, args.port, args.limit, args.upstream_proxy, args.yes, not args.no_copy)
    except (ValueError, RuntimeError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_history_capture_finish(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = finish_history_capture(base, args.session_id, args.limit, args.yes, args.stop_proxy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_snapshot_prepare(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = prepare_proxy_snapshot(base, args.article_url, args.port, args.upstream_proxy, args.yes, not args.no_copy)
    except (ValueError, RuntimeError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_snapshot_status(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        result = proxy_snapshot_status(base, args.session_id)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "session_id": args.session_id}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_proxy_snapshot_finish(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = finish_proxy_snapshot(base, args.session_id, args.yes, args.stop_proxy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 3


def command_history_import_context(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        context_url = args.context_url or ""
        clipboard_method = ""
        if args.from_clipboard:
            context_url, clipboard_method = read_clipboard()
        if not context_url:
            raise ValueError("context URL is required; copy it in WeChat and retry with --from-clipboard")
        session = session_status(base, args.session_id)
        if session.get("expired"):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "history session expired",
                        "session_id": args.session_id,
                        "next_step": "Start a new history session and copy a fresh WeChat built-in-browser context URL.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 3
        result = fetch_history_rows_with_context_url(base, session, context_url, args.limit)
        if clipboard_method:
            result["context_source"] = "clipboard"
            result["clipboard_method"] = clipboard_method
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (FileNotFoundError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "session_id": args.session_id,
                    "next_step": "In WeChat desktop built-in browser, open the public-account history page, copy its current URL, and retry history-import-context --from-clipboard.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 4


def command_history_import_wechat_cache(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        session = session_status(base, args.session_id)
        result = import_history_rows_from_wechat_cache(base, session, args.minutes, args.limit, args.contains or "")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 4
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


def command_history_fetch(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    session = session_status(base, args.session_id)
    if not session.get("context_ready"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "history context is not ready",
                    "session": session,
                    "next_step": "Run history-proxy-start, enable the local proxy for WeChat, then open and scroll the WeChat history page.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 3
    result = fetch_history_rows_from_context(base, session, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 4


def resolve_history_source(base: Path, session_id: str, input_path: str) -> tuple[dict[str, Any] | None, Path, Path | None]:
    if session_id:
        session = load_history_session(base, session_id)
        default_source = "history_json" if Path(str(session.get("history_json", ""))).expanduser().exists() else "history_csv"
        source = resolve_session_file(session, input_path, default_source)
        output = resolve_session_file(session, "", "selected_csv")
        return session, source, output
    if not input_path:
        raise ValueError("history input path is required when --session-id is not provided")
    source = Path(input_path).expanduser()
    return None, source, None


def command_history_preview(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        _session, source, _output = resolve_history_source(base, args.session_id, args.input or "")
        result = preview_history_rows(source, args.limit, args.contains or "")
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_history_select(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    try:
        session, source, default_output = resolve_history_source(base, args.session_id, args.input or "")
        if session and args.output:
            output = resolve_session_file(session, args.output, "selected_csv")
        else:
            output = Path(args.output).expanduser() if args.output else default_output
        result = select_history_rows(source, output, args.latest, args.contains or "", args.indices or "", args.range or "", args.titles or "")
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_history_download_selected(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    selected = args.selected_csv
    if not selected and args.session_id:
        selected = load_history_session(base, args.session_id).get("selected_csv", "")
    selected_path = Path(str(selected)).expanduser()
    try:
        rows = load_history_rows(selected_path)
    except Exception:
        rows = []
    urls = []
    seen: set[str] = set()
    for row in rows:
        try:
            url = clean_url(row.get("url", ""))
        except ValueError:
            continue
        if url not in seen:
            urls.append(url)
            seen.add(url)
    if not urls:
        urls = extract_urls(str(selected))
    if not urls:
        print("No selected WeChat article URLs found.", file=sys.stderr)
        return 2
    manifest = run_download_for_args(
        urls,
        args,
        {"mode": "history-download-selected", "session_id": args.session_id, "selected_csv": selected},
    )
    print_download_summary(manifest)
    return 0 if manifest["failure_count"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local WeChat article downloader runtime")
    parser.add_argument("--runtime-dir", default=None, help="Override runtime directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("download-url", help="Download one public WeChat article URL")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("url")
    p.add_argument("--profile", choices=["markdown-only", "archive"], default="markdown-only")
    p.add_argument("--output-dir", default="", help="Markdown-only output directory")
    p.add_argument("--formats", default="html,md,txt")
    p.add_argument("--no-assets", action="store_true")
    p.add_argument("--html-concurrency", type=int, default=1, help="Markdown-only article fetch concurrency, capped at 4")
    p.add_argument("--max-retries", type=int, default=0, help="Markdown-only per-article retry count, capped at 3")
    p.add_argument("--req-per-min", type=int, default=20, help="Token bucket rate limit in requests/minute (default 20)")
    p.add_argument("--aggressive", action="store_true", help="Higher concurrency and shorter delays (30 req/min, workers up to 5)")
    p.set_defaults(func=command_download_url)

    p = sub.add_parser("download-list", help="Download URLs from text, .txt, .csv, or .json")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("input")
    p.add_argument("--profile", choices=["markdown-only", "archive"], default="markdown-only")
    p.add_argument("--output-dir", default="", help="Markdown-only output directory")
    p.add_argument("--formats", default="html,md,txt")
    p.add_argument("--no-assets", action="store_true")
    p.add_argument("--html-concurrency", type=int, default=1, help="Markdown-only article fetch concurrency, capped at 4")
    p.add_argument("--max-retries", type=int, default=0, help="Markdown-only per-article retry count, capped at 3")
    p.add_argument("--req-per-min", type=int, default=20, help="Token bucket rate limit in requests/minute (default 20)")
    p.add_argument("--aggressive", action="store_true", help="Higher concurrency and shorter delays (30 req/min, workers up to 5)")
    p.set_defaults(func=command_download_list)

    p = sub.add_parser("list", help="List downloaded articles")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=command_list)

    p = sub.add_parser("history-start", help="Start account-history session from one sample article URL")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("sample_url")
    p.set_defaults(func=command_history_start)

    p = sub.add_parser("history-open", help="Generate and copy WeChat built-in-browser history open link")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("--no-copy", action="store_true", help="Print the link without copying it to clipboard")
    p.set_defaults(func=command_history_open)

    p = sub.add_parser("history-capture-prepare", help="Prepare one guided WeChat history capture session")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("sample_url")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument(
        "--upstream-proxy",
        default="auto",
        help="Upstream HTTP proxy for mitmproxy. Use auto to chain the current macOS HTTP proxy.",
    )
    p.add_argument("--yes", action="store_true", help="Actually start the proxy and modify macOS proxy settings")
    p.add_argument("--no-copy", action="store_true", help="Print the old history URL without copying it to clipboard")
    p.add_argument("--use-service", action="store_true", help="Use the persistent local proxy service")
    p.set_defaults(func=command_history_capture_prepare)

    p = sub.add_parser("history-capture-finish", help="Finish guided WeChat history capture and restore proxy")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--yes", action="store_true", help="Actually restore macOS proxy settings")
    p.add_argument("--stop-proxy", action="store_true", help="Also stop the local proxy process")
    p.set_defaults(func=command_history_capture_finish)

    p = sub.add_parser("proxy-snapshot-prepare", help="Deprecated debug path: prepare one guided article snapshot session")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("article_url")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument(
        "--upstream-proxy",
        default="auto",
        help="Upstream HTTP proxy for mitmproxy. Use auto to chain the current macOS HTTP proxy.",
    )
    p.add_argument("--yes", action="store_true", help="Deprecated: start/reuse the proxy and modify macOS proxy settings")
    p.add_argument("--no-copy", action="store_true", help="Print the article URL without copying it to clipboard")
    p.set_defaults(func=command_proxy_snapshot_prepare)

    p = sub.add_parser("proxy-snapshot-status", help="Check one WeChat article DOM snapshot session")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.set_defaults(func=command_proxy_snapshot_status)

    p = sub.add_parser("proxy-snapshot-finish", help="Finish WeChat article DOM snapshot and restore proxy")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("--yes", action="store_true", help="Actually restore macOS proxy settings")
    p.add_argument("--stop-proxy", action="store_true", help="Also stop the local proxy process")
    p.set_defaults(func=command_proxy_snapshot_finish)

    p = sub.add_parser("proxy-enhancer-start", help="Start persistent WeChat article enhancer proxy without changing system proxy")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument(
        "--upstream-proxy",
        default="auto",
        help="Upstream HTTP proxy. Use auto to chain the current macOS HTTP proxy.",
    )
    p.set_defaults(func=command_proxy_enhancer_start)

    p = sub.add_parser("proxy-enhancer-status", help="Check persistent WeChat article enhancer proxy")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.set_defaults(func=command_proxy_enhancer_status)

    p = sub.add_parser("proxy-enhancer-stop", help="Stop persistent WeChat article enhancer proxy")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument("--yes", action="store_true", help="Actually stop the local proxy service")
    p.set_defaults(func=command_proxy_enhancer_stop)

    p = sub.add_parser("proxy-enhancer-restart", help="Safely restart enhancer without leaving system proxy on a dead port")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument(
        "--upstream-proxy",
        default="auto",
        help="Upstream HTTP proxy. Use auto to chain the current macOS HTTP proxy after temporary bypass.",
    )
    p.add_argument("--yes", action="store_true", help="Actually restart the local proxy service")
    p.set_defaults(func=command_proxy_enhancer_restart)

    p = sub.add_parser("proxy-enhancer-check-ingress", help="Check whether WeChat article traffic reached 23344")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument("--minutes", type=int, default=10, help="Recent window to inspect")
    p.set_defaults(func=command_proxy_enhancer_check_ingress)

    p = sub.add_parser("proxy-enhancer-logs", help="Show recent proxy-enhancer debug events")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--hours", type=int, default=24, help="Recent window to inspect, capped at 24 hours")
    p.add_argument("--limit", type=int, default=80, help="Maximum events to print")
    p.set_defaults(func=command_proxy_enhancer_logs)

    p = sub.add_parser("proxy-enhancer-route-help", help="Show local routing facts for WeChat -> 23344 -> upstream")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.set_defaults(func=command_proxy_enhancer_route_help)

    p = sub.add_parser("proxy-enhancer-session-start", help="Route system HTTP/HTTPS proxy to the persistent enhancer")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument(
        "--upstream-proxy",
        default="auto",
        help="Upstream HTTP proxy. Use auto to chain the current macOS HTTP proxy.",
    )
    p.add_argument("--yes", action="store_true", help="Actually modify macOS HTTP/HTTPS proxy settings")
    p.set_defaults(func=command_proxy_enhancer_session_start)

    p = sub.add_parser("proxy-enhancer-session-finish", help="Restore system HTTP/HTTPS proxy after enhancer session")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--yes", action="store_true", help="Actually restore saved macOS HTTP/HTTPS proxy settings")
    p.set_defaults(func=command_proxy_enhancer_session_finish)

    p = sub.add_parser("snapshot-list", help="List recent snapshots captured by proxy-enhancer")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=command_snapshot_list)

    p = sub.add_parser("snapshot-inbox", help="List unprocessed proxy-enhancer snapshots")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--include-processed", action="store_true", help="Include already attached snapshots")
    p.set_defaults(func=command_snapshot_inbox)

    p = sub.add_parser("snapshot-latest", help="Show the latest snapshot captured by proxy-enhancer")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.set_defaults(func=command_snapshot_latest)

    p = sub.add_parser("snapshot-export", help="Export or inspect one proxy-enhancer snapshot")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("snapshot_id")
    p.add_argument("--output-dir", default="", help="Optional destination directory")
    p.set_defaults(func=command_snapshot_export)

    p = sub.add_parser("snapshot-extract", help="Extract one proxy-enhancer snapshot into structured files")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("snapshot_id", nargs="?", default="latest", help="Snapshot id, or latest")
    p.add_argument("--output-dir", default="", help="Optional destination directory")
    p.set_defaults(func=command_snapshot_extract)

    p = sub.add_parser("snapshot-attach", help="Attach proxy-enhancer snapshots into the article library")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("snapshot_id", nargs="?", default="latest", help="Snapshot id, or latest")
    p.add_argument("--all-unprocessed", action="store_true", help="Attach all snapshots not yet processed")
    p.add_argument("--since", default="", help="Attach snapshots captured since this ISO datetime")
    p.add_argument("--include-processed", action="store_true", help="With --since, include already processed snapshots")
    p.add_argument("--force", action="store_true", help="Re-attach snapshots even if already processed")
    p.add_argument("--dry-run", action="store_true", help="Preview attach plan without writing article-library files or processed state")
    p.add_argument("--output-dir", default="", help="Optional article library root")
    p.set_defaults(func=command_snapshot_attach)

    p = sub.add_parser("adapter-watch", help="Wait for WeChat desktop context and fetch history rows when ready")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--interval", type=int, default=2)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=command_adapter_watch)

    p = sub.add_parser("history-status", help="Check account-history session status")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.set_defaults(func=command_history_status)

    p = sub.add_parser("history-proxy-start", help="Start mitmproxy adapter for WeChat history requests")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument(
        "--upstream-proxy",
        default="auto",
        help="Upstream HTTP proxy for mitmproxy, e.g. http://host:port. Use auto to chain the current macOS HTTP proxy, or none to disable.",
    )
    p.set_defaults(func=command_history_proxy_start)

    p = sub.add_parser("history-proxy-setup", help="Check mitmproxy and certificate setup")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument("--install", action="store_true", help="Install mitmproxy with Homebrew if mitmdump is missing")
    p.add_argument("--yes", action="store_true", help="Actually run brew install mitmproxy when --install is set")
    p.add_argument("--open-cert-page", action="store_true", help="Open http://mitm.it in the default browser")
    p.set_defaults(func=command_history_proxy_setup)

    p = sub.add_parser("history-proxy-enable", help="Enable macOS HTTP/HTTPS proxy after explicit confirmation")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--service", default="", help="macOS network service, defaults to Wi-Fi when available")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument("--yes", action="store_true", help="Actually modify network proxy settings")
    p.set_defaults(func=command_history_proxy_enable)

    p = sub.add_parser("history-proxy-disable", help="Restore or disable macOS HTTP/HTTPS proxy")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--service", default="", help="macOS network service, defaults to saved service or Wi-Fi")
    p.add_argument("--yes", action="store_true", help="Actually restore or disable network proxy settings")
    p.set_defaults(func=command_history_proxy_disable)

    p = sub.add_parser("history-proxy-status", help="Check mitmproxy adapter status")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.set_defaults(func=command_history_proxy_status)

    p = sub.add_parser("history-proxy-stop", help="Stop mitmproxy adapter")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.set_defaults(func=command_history_proxy_stop)

    p = sub.add_parser("proxy-service-start", help="Start or refresh persistent local mitmproxy service")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument(
        "--upstream-proxy",
        default="auto",
        help="Upstream HTTP proxy. Use auto to chain the current macOS HTTP proxy.",
    )
    p.set_defaults(func=command_proxy_service_start)

    p = sub.add_parser("proxy-service-status", help="Check persistent local proxy service")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.set_defaults(func=command_proxy_service_status)

    p = sub.add_parser("proxy-service-stop", help="Stop persistent local proxy service")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
    p.add_argument("--yes", action="store_true", help="Actually stop the local proxy service")
    p.set_defaults(func=command_proxy_service_stop)

    p = sub.add_parser("history-import-context", help="Fetch history rows from a WeChat built-in-browser context URL")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("context_url", nargs="?", help="Current profile_ext URL copied from WeChat desktop built-in browser")
    p.add_argument("--from-clipboard", action="store_true", help="Read the context URL from the local clipboard")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=command_history_import_context)

    p = sub.add_parser("history-import-wechat-cache", help="Import recent article shortlinks from WeChat WebView history cache")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("--minutes", type=int, default=30, help="Look back this many minutes, bounded by session creation time")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--contains", default="", help="Optional title or URL keyword filter")
    p.set_defaults(func=command_history_import_wechat_cache)

    p = sub.add_parser("history-fetch", help="Fetch account-history list after context is ready")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("session_id")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=command_history_fetch)

    p = sub.add_parser("history-preview", help="Print numbered summary of history rows for Skill display")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("input", nargs="?")
    p.add_argument("--session-id", default="")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--contains", default="")
    p.set_defaults(func=command_history_preview)

    p = sub.add_parser("history-select", help="Select rows from a history CSV/JSON")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("input", nargs="?")
    p.add_argument("--session-id", default="")
    p.add_argument("--indices", default="", help="1-based article numbers, comma or space separated")
    p.add_argument("--range", default="", help="1-based article ranges, for example 1-20 or 1-5,8-10")
    p.add_argument("--latest", type=int, default=None)
    p.add_argument("--contains", default="")
    p.add_argument("--titles", default="", help="Title keywords or fragments, separated by comma or newline")
    p.add_argument("--output", default="")
    p.set_defaults(func=command_history_select)

    p = sub.add_parser("history-download-selected", help="Download selected history rows")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS, help="Override runtime directory")
    p.add_argument("--session-id", default="")
    p.add_argument("--selected-csv", default="")
    p.add_argument("--profile", choices=["markdown-only", "archive"], default="markdown-only")
    p.add_argument("--output-dir", default="", help="Markdown-only output directory")
    p.add_argument("--formats", default="html,md,txt")
    p.add_argument("--no-assets", action="store_true")
    p.set_defaults(func=command_history_download_selected)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
