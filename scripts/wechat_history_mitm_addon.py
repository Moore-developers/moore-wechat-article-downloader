#!/usr/bin/env python3
"""mitmproxy addon for user-owned WeChat public-account history capture.

The addon extracts article metadata from WeChat history responses. It keeps the
old profile_ext?action=getmsg path, but also scans WeChat WebView responses for
article-list shaped payloads because desktop WeChat can route profile pages
through newer endpoints. It intentionally writes only safe article rows and
small endpoint observations; credential query parameters are never persisted.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any


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
HISTORY_HOSTS = {"mp.weixin.qq.com", "channels.weixin.qq.com"}
OBSERVE_HOSTS = {
    "mp.weixin.qq.com",
    "channels.weixin.qq.com",
    "finder.video.qq.com",
    "wxa.wxs.qq.com",
    "wxsmw.wxs.qq.com",
    "wximg.wxs.qq.com",
    "support.weixin.qq.com",
}
IGNORED_RESPONSE_PATHS = {
    "/mp/tts",
    "/mp/jsmonitor",
    "/mp/searchkeywordreport",
    "/mp/relatedsearchword",
    "/mp/frontendcommstore",
    "/mp/audiolyrics",
}
ARTICLE_MARKERS = (
    "general_msg_list",
    "app_msg_ext_info",
    "multi_app_msg_item_list",
    "content_url",
    "mp.weixin.qq.com/s",
    "mp.weixin.qq.com/mp/appmsg",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean_mp_url(url: str) -> str:
    url = html.unescape(str(url or "")).strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://mp.weixin.qq.com" + url
    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.lower() != "mp.weixin.qq.com":
        return url
    safe_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in SENSITIVE_QUERY_KEYS or "token" in lowered or "ticket" in lowered:
            continue
        safe_query.append((key, value))
    return urllib.parse.urlunsplit(
        (parsed.scheme or "https", parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query, doseq=True), "")
    )


def safe_path_for_log(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return parsed.path or "/"


def format_publish_time(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def sanitize_row(row: dict[str, Any]) -> dict[str, str]:
    cleaned = {field: str(row.get(field, "")) for field in HISTORY_FIELDS}
    cleaned["url"] = clean_mp_url(cleaned["url"])
    cleaned["cover"] = clean_mp_url(cleaned["cover"])
    cleaned["source_article_url"] = clean_mp_url(cleaned["source_article_url"])
    return cleaned


def rows_from_payload(payload: dict[str, Any], session: dict[str, Any], biz: str) -> list[dict[str, str]]:
    raw_list = payload.get("general_msg_list") or ""
    if isinstance(raw_list, str):
        msg_list = json.loads(raw_list)
    elif isinstance(raw_list, dict):
        msg_list = raw_list
    else:
        msg_list = {}
    items = msg_list.get("list") if isinstance(msg_list, dict) else []
    if not isinstance(items, list):
        return []

    rows: list[dict[str, str]] = []
    account_name = str(session.get("account_name") or "")
    account_id = biz or str(session.get("account_id") or session.get("biz") or "")
    for item in items:
        if not isinstance(item, dict):
            continue
        comm = item.get("comm_msg_info") if isinstance(item.get("comm_msg_info"), dict) else {}
        publish_time = format_publish_time(comm.get("datetime"))
        ext = item.get("app_msg_ext_info") if isinstance(item.get("app_msg_ext_info"), dict) else {}
        article_items = [ext]
        multi = ext.get("multi_app_msg_item_list") if isinstance(ext, dict) else []
        if isinstance(multi, list):
            article_items.extend(part for part in multi if isinstance(part, dict))
        for article in article_items:
            title = re.sub(r"\s+", " ", str(article.get("title") or "")).strip()
            url = clean_mp_url(str(article.get("content_url") or ""))
            if not title or not url:
                continue
            rows.append(
                sanitize_row(
                    {
                        "account_name": account_name,
                        "account_id": account_id,
                        "title": title,
                        "url": url,
                        "publish_time": publish_time,
                        "digest": str(article.get("digest") or "").strip(),
                        "cover": str(article.get("cover") or article.get("cover_235_1") or ""),
                        "source_article_url": str(session.get("sample_url") or ""),
                        "fetch_method": "wechat-history-proxy",
                    }
                )
            )
    return rows


def article_url_from_value(value: Any) -> str:
    url = clean_mp_url(str(value or ""))
    if not url:
        return ""
    if "mp.weixin.qq.com/" not in url:
        return ""
    return url


def row_from_article(article: dict[str, Any], session: dict[str, Any], biz: str, publish_time: str) -> dict[str, str] | None:
    title = re.sub(r"\s+", " ", str(article.get("title") or "")).strip()
    url = ""
    for key in ("content_url", "appmsg_url", "url", "link"):
        url = article_url_from_value(article.get(key))
        if url:
            break
    if not title or not url:
        return None
    return sanitize_row(
        {
            "account_name": str(session.get("account_name") or ""),
            "account_id": biz or str(session.get("account_id") or session.get("biz") or ""),
            "title": title,
            "url": url,
            "publish_time": publish_time,
            "digest": str(article.get("digest") or article.get("summary") or "").strip(),
            "cover": str(article.get("cover") or article.get("cover_235_1") or article.get("pic_url") or ""),
            "source_article_url": str(session.get("sample_url") or ""),
            "fetch_method": "wechat-history-proxy",
        }
    )


def rows_from_msg_item(item: dict[str, Any], session: dict[str, Any], biz: str) -> list[dict[str, str]]:
    comm = item.get("comm_msg_info") if isinstance(item.get("comm_msg_info"), dict) else {}
    publish_time = format_publish_time(
        comm.get("datetime")
        or item.get("datetime")
        or item.get("publish_time")
        or item.get("create_time")
    )
    ext = item.get("app_msg_ext_info") if isinstance(item.get("app_msg_ext_info"), dict) else item
    article_items = [ext]
    multi = ext.get("multi_app_msg_item_list") if isinstance(ext, dict) else []
    if isinstance(multi, list):
        article_items.extend(part for part in multi if isinstance(part, dict))
    rows: list[dict[str, str]] = []
    for article in article_items:
        row = row_from_article(article, session, biz, publish_time)
        if row:
            rows.append(row)
    return rows


def rows_from_any_payload(payload: Any, session: dict[str, Any], biz: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    visited = 0

    def walk(value: Any) -> None:
        nonlocal visited
        visited += 1
        if visited > 3000:
            return
        if isinstance(value, str):
            stripped = value.strip()
            if len(stripped) < 2 or stripped[0] not in "[{":
                return
            try:
                walk(json.loads(stripped))
            except Exception:
                return
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return

        if "general_msg_list" in value:
            try:
                rows.extend(rows_from_payload(value, session, biz))
            except Exception:
                pass

        if isinstance(value.get("app_msg_ext_info"), dict) or (
            value.get("title") and any(value.get(key) for key in ("content_url", "appmsg_url", "url", "link"))
        ):
            rows.extend(rows_from_msg_item(value, session, biz))

        for item in value.values():
            walk(item)

    walk(payload)
    return dedupe_rows(rows)


def decode_js_string(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    if not value:
        return ""
    if "\\u" not in value and "\\x" not in value:
        return value
    try:
        return bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return value


def extract_embedded_history_payload(text: str) -> Any:
    assignments = [
        r"general_msg_list\s*=\s*(['\"])(.*?)\1",
        r"(?:var\s+)?msgList\s*=\s*(['\"])(.*?)\1",
    ]
    for pattern in assignments:
        match = re.search(pattern, text, re.S)
        if not match:
            continue
        raw = decode_js_string(match.group(2))
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if pattern.startswith("general_msg_list"):
            return {"general_msg_list": payload}
        return payload
    return None


def load_existing_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        data = read_json(path)
        rows = data.get("articles", []) if isinstance(data, dict) else []
        return [sanitize_row(row) for row in rows if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [sanitize_row(row) for row in csv.DictReader(fh)]


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for row in rows:
        key = row.get("url") or row.get("title") or json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def write_rows_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(sanitize_row(row) for row in rows)


class WeChatHistoryCapture:
    def __init__(self) -> None:
        runtime_dir = os.environ.get("MOORE_WECHAT_RUNTIME_DIR", "")
        session_id = os.environ.get("MOORE_WECHAT_SESSION_ID", "")
        self.limit = int(os.environ.get("MOORE_WECHAT_HISTORY_LIMIT", "100") or "100")
        if not runtime_dir or not session_id:
            raise RuntimeError("MOORE_WECHAT_RUNTIME_DIR and MOORE_WECHAT_SESSION_ID are required")
        self.base = Path(runtime_dir).expanduser()
        self.fallback_session_id = session_id
        self.active_session_path = self.base / "context" / "active-proxy-session.json"

    def active_session_id(self) -> str:
        if self.active_session_path.exists():
            try:
                active = read_json(self.active_session_path)
            except Exception:
                active = {}
            session_id = str(active.get("session_id") or "").strip()
            if session_id:
                return session_id
        return self.fallback_session_id

    def session_context(self) -> dict[str, Any]:
        session_id = self.active_session_id()
        session_path = self.base / "context" / f"{session_id}.json"
        session = read_json(session_path)
        return {
            "session_id": session_id,
            "session": session,
            "ready_path": self.base / "context" / f"{session_id}.ready.json",
            "observe_path": self.base / "context" / f"{session_id}.observed.jsonl",
            "history_csv": Path(str(session["history_csv"])).expanduser(),
            "history_json": Path(str(session["history_json"])).expanduser(),
        }

    def request(self, flow: Any) -> None:
        request = flow.request
        if request.host not in HISTORY_HOSTS:
            return
        # WeChat desktop often serves the profile/history WebView from cache.
        # Force a fresh response so the response hook can inspect article rows.
        for header in ("if-none-match", "if-modified-since"):
            if header in request.headers:
                del request.headers[header]
        request.headers["cache-control"] = "no-cache"
        request.headers["pragma"] = "no-cache"

    def observe(self, flow: Any, markers: list[str], rows_count: int = 0, observe_path: Path | None = None) -> None:
        request = flow.request
        path = observe_path or (self.base / "context" / f"{self.fallback_session_id}.observed.jsonl")
        content_length = 0
        try:
            content_length = len(flow.response.content or b"") if flow.response else 0
        except Exception:
            content_length = 0
        payload = {
            "at": utc_now(),
            "host": request.host,
            "path": safe_path_for_log(request.url),
            "status_code": getattr(flow.response, "status_code", 0) if flow.response else 0,
            "content_type": flow.response.headers.get("content-type", "")[:120] if flow.response else "",
            "content_length": content_length,
            "markers": markers,
            "rows_count": rows_count,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def write_rows(self, context: dict[str, Any], new_rows: list[dict[str, str]], method: str) -> None:
        history_json = Path(context["history_json"])
        history_csv = Path(context["history_csv"])
        rows = dedupe_rows(load_existing_rows(history_json) + new_rows)
        if self.limit > 0:
            rows = rows[: self.limit]
        write_rows_csv(history_csv, rows)
        write_json(
            history_json,
            {
                "articles": rows,
                "fetched_at": utc_now(),
                "fetch_method": "wechat-history-proxy",
            },
        )
        marker = {
            "status": "ready",
            "ready": True,
            "ready_at": utc_now(),
            "adapter": "wechat-history-proxy",
            "method": method,
            "article_count": len(rows),
            "history_csv": str(history_csv),
            "history_json": str(history_json),
            "observed": str(context["observe_path"]),
        }
        write_json(Path(context["ready_path"]), marker)

    def response(self, flow: Any) -> None:
        request = flow.request
        if request.host not in OBSERVE_HOSTS:
            return
        try:
            context = self.session_context()
        except Exception:
            return
        session = context["session"]
        observe_path = Path(context["observe_path"])
        path = safe_path_for_log(request.url)
        if request.host not in HISTORY_HOSTS:
            self.observe(flow, ["endpoint-summary"], 0, observe_path)
            return
        if not flow.response:
            return
        try:
            text = flow.response.get_text(strict=False)
        except Exception:
            self.observe(flow, ["endpoint-summary"], 0, observe_path)
            return
        markers = [marker for marker in ARTICLE_MARKERS if marker in text]
        if path in IGNORED_RESPONSE_PATHS:
            self.observe(flow, ["ignored-endpoint"], 0, observe_path)
            return
        is_profile_getmsg = (
            request.host == "mp.weixin.qq.com"
            and request.path.split("?", 1)[0] == "/mp/profile_ext"
            and (urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query, keep_blank_values=True).get("action") or [""])[0]
            == "getmsg"
        )
        if not markers and not is_profile_getmsg:
            self.observe(flow, ["endpoint-summary"], 0, observe_path)
            return
        if len(text) > 6_000_000:
            self.observe(flow, markers or ["large-response-skipped"], observe_path=observe_path)
            return
        try:
            payload = json.loads(text)
        except Exception:
            payload = extract_embedded_history_payload(text)
            if payload is None:
                self.observe(flow, markers or ["non-json-marker"], observe_path=observe_path)
                return
        if not isinstance(payload, dict):
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query, keep_blank_values=True)
        biz = (query.get("__biz") or [str(session.get("biz") or "")])[0]
        try:
            if is_profile_getmsg:
                new_rows = rows_from_payload(payload, session, biz)
                method = "mitmproxy-profile-ext-getmsg"
            else:
                new_rows = rows_from_any_payload(payload, session, biz)
                method = "mitmproxy-broad-history-scan"
        except Exception:
            return
        self.observe(flow, markers or ["profile-getmsg"], len(new_rows), observe_path)
        if not new_rows:
            return
        self.write_rows(context, new_rows, method)


addons = [WeChatHistoryCapture()]
