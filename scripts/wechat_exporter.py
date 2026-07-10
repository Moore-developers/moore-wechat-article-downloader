#!/usr/bin/env python3
"""Exporter-mode runtime for Moore WeChat article downloader.

This module integrates the public API exposed by wechat-article-exporter style
services. It keeps all user state local in SQLite and reuses the existing
Markdown downloader for final article delivery.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from wechat_downloader import (  # noqa: E402
    DEFAULT_DELIVERY_DIR,
    PAGE_DATA_END,
    PAGE_DATA_START,
    clean_url,
    copy_to_clipboard,
    extract_wechat_article_context,
    make_run_id,
    markdown_cell,
    run_markdown_only_download,
    runtime_dir,
    safe_display_url,
    safe_name,
    sanitize_text_urls,
    scrub_payload,
    start_proxy_enhancer_session,
    utc_now,
)
from wechat_credential_broker import broker_request, credential_capability_path, credential_socket_path  # noqa: E402


DEFAULT_BASE_URL = "https://down.mptext.top"
EXPORTER_DB_VERSION = 5
ARTICLE_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/[^\s\"'<>]+", re.I)
DEFAULT_VISIBLE_FIELDS = [
    "title",
    "url",
    "publish_time",
    "author",
    "digest",
    "cover",
    "content_downloaded",
]
ALL_FIELDS = [
    "id",
    "url",
    "title",
    "cover",
    "digest",
    "create_time",
    "publish_time",
    "is_deleted",
    "article_status",
    "content_downloaded",
    "author",
    "is_original",
    "collection_title",
]
ARTICLE_FIELDS = [
    "id",
    "account_id",
    "msgid",
    "idx",
    "title",
    "url",
    "digest",
    "cover_url",
    "author",
    "publish_time",
    "create_time",
    "is_original",
    "is_deleted",
    "article_status",
    "content_downloaded",
    "collection_title",
    "raw_json",
    "created_at",
    "updated_at",
]

def login_dir(base: Path) -> Path:
    return base / "exporter-login"


def login_session_path(base: Path, login_id: str) -> Path:
    return login_dir(base) / f"{safe_name(login_id, 80)}.json"


def login_cookie_path(base: Path, login_id: str) -> Path:
    return login_dir(base) / f"{safe_name(login_id, 80)}.cookies.txt"


def login_qrcode_path(base: Path, login_id: str) -> Path:
    return login_dir(base) / f"{safe_name(login_id, 80)}.qrcode"


def login_qrcode_path_with_extension(base: Path, login_id: str, content_type: str, raw: bytes) -> Path:
    content_type = (content_type or "").lower()
    if "png" in content_type or raw.startswith(b"\x89PNG"):
        suffix = ".png"
    elif "gif" in content_type or raw.startswith(b"GIF"):
        suffix = ".gif"
    else:
        suffix = ".jpg"
    return login_dir(base) / f"{safe_name(login_id, 80)}{suffix}"


def app_db_path(base: Path) -> Path:
    return base / "exporter.sqlite"


def connect_db(base: Path) -> sqlite3.Connection:
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(app_db_path(base))
    db.row_factory = sqlite3.Row
    return db


def write_json_response(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10,13}", value.strip())):
        number = int(value)
        if number > 10_000_000_000:
            number //= 1000
        try:
            return dt.datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            return str(value)
    text = str(value).strip()
    return text


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_json_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def ensure_exporter_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_exporter_db(db: sqlite3.Connection) -> None:
    """Apply additive, idempotent migrations for local-only article metadata."""
    ensure_exporter_column(db, "article_comments", "comment_scope", "TEXT NOT NULL DEFAULT 'elected'")
    ensure_exporter_column(db, "article_comments", "source", "TEXT NOT NULL DEFAULT 'import'")
    ensure_exporter_column(db, "article_comments", "fetched_at", "TEXT NOT NULL DEFAULT ''")
    ensure_exporter_column(db, "article_comments", "complete", "INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE article_comments SET comment_scope = 'elected' WHERE TRIM(comment_scope) = ''")
    db.execute("UPDATE article_comments SET source = 'import' WHERE TRIM(source) = ''")
    db.execute("UPDATE article_comments SET fetched_at = created_at WHERE TRIM(fetched_at) = ''")
    db.execute("UPDATE article_comments SET comment_id = 'legacy-' || id WHERE TRIM(comment_id) = ''")
    db.execute(
        """
        DELETE FROM article_comments
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM article_comments
            GROUP BY article_id, comment_id, comment_scope
        )
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_article_comments_identity "
        "ON article_comments(article_id, comment_id, comment_scope)"
    )
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_biz_mappings (
            account_id INTEGER PRIMARY KEY,
            biz TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'verified',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES target_accounts(id)
        );

        CREATE TABLE IF NOT EXISTS article_contexts (
            article_id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL,
            biz TEXT NOT NULL DEFAULT '',
            msgid TEXT NOT NULL DEFAULT '',
            idx INTEGER NOT NULL DEFAULT 0,
            comment_id TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            context_status TEXT NOT NULL DEFAULT 'missing',
            source TEXT NOT NULL DEFAULT '',
            resolved_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id),
            FOREIGN KEY(account_id) REFERENCES target_accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_article_contexts_account_status
            ON article_contexts(account_id, context_status);

        CREATE TABLE IF NOT EXISTS engagement_runs (
            run_id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            scope_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            requested_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            credential_expires_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES target_accounts(id)
        );

        CREATE TABLE IF NOT EXISTS article_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL DEFAULT '',
            article_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            read_count INTEGER,
            like_count INTEGER,
            old_like_count INTEGER,
            share_count INTEGER,
            comment_count INTEGER,
            raw_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(article_id) REFERENCES articles(id),
            UNIQUE(run_id, article_id, source)
        );

        CREATE TABLE IF NOT EXISTS article_comment_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_comment_id INTEGER NOT NULL,
            reply_id TEXT NOT NULL,
            nick_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            like_count INTEGER,
            create_time TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL DEFAULT '',
            complete INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(article_comment_id) REFERENCES article_comments(id),
            UNIQUE(article_comment_id, reply_id)
        );

        CREATE TABLE IF NOT EXISTS article_write_locks (
            article_id INTEGER PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(article_id) REFERENCES articles(id)
        );

        CREATE TABLE IF NOT EXISTS evolution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            stage TEXT NOT NULL,
            code TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            account_id INTEGER NOT NULL DEFAULT 0,
            article_id INTEGER NOT NULL DEFAULT 0,
            run_id TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
    )


def init_exporter_db(base: Path) -> dict[str, Any]:
    db = connect_db(base)
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS exporter_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS login_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL DEFAULT 'default',
                mp_account_name TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT '',
                last_login_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS credential_store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                keychain_account TEXT NOT NULL DEFAULT '',
                encrypted_payload TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(profile_id) REFERENCES login_profiles(id)
            );

            CREATE TABLE IF NOT EXISTS target_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fakeid TEXT NOT NULL UNIQUE,
                nickname TEXT NOT NULL,
                alias TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                article_count INTEGER NOT NULL DEFAULT 0,
                synced_count INTEGER NOT NULL DEFAULT 0,
                last_sync_at TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                msgid TEXT NOT NULL DEFAULT '',
                idx INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                digest TEXT NOT NULL DEFAULT '',
                cover_url TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                publish_time TEXT NOT NULL DEFAULT '',
                create_time TEXT NOT NULL DEFAULT '',
                is_original INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                article_status TEXT NOT NULL DEFAULT '',
                content_downloaded INTEGER NOT NULL DEFAULT 0,
                comment_downloaded INTEGER NOT NULL DEFAULT 0,
                read_count INTEGER,
                like_count INTEGER,
                share_count INTEGER,
                favorite_count INTEGER,
                comment_count INTEGER,
                collection_title TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES target_accounts(id),
                UNIQUE(account_id, url)
            );

            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                collection_url TEXT NOT NULL DEFAULT '',
                article_count INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES target_accounts(id),
                UNIQUE(account_id, title)
            );

            CREATE TABLE IF NOT EXISTS collection_articles (
                collection_id INTEGER NOT NULL,
                article_id INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(collection_id, article_id),
                FOREIGN KEY(collection_id) REFERENCES collections(id),
                FOREIGN KEY(article_id) REFERENCES articles(id)
            );

            CREATE TABLE IF NOT EXISTS field_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                visible_fields_json TEXT NOT NULL,
                default_export_format TEXT NOT NULL DEFAULT 'markdown',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(account_id) REFERENCES target_accounts(id)
            );

            CREATE TABLE IF NOT EXISTS download_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_ids_json TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                success_count INTEGER NOT NULL,
                failed_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS article_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                comment_id TEXT NOT NULL DEFAULT '',
                nick_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                like_count INTEGER,
                create_time TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(article_id) REFERENCES articles(id)
            );

            CREATE TABLE IF NOT EXISTS wizard_sessions (
                id TEXT PRIMARY KEY,
                target TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                request_json TEXT NOT NULL DEFAULT '{}',
                candidates_json TEXT NOT NULL DEFAULT '[]',
                selected_account_id INTEGER NOT NULL DEFAULT 0,
                selected_article_ids_json TEXT NOT NULL DEFAULT '[]',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        now = utc_now()
        db.execute(
            """
            INSERT OR IGNORE INTO field_presets
                (name, visible_fields_json, default_export_format, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("default", json_dumps(DEFAULT_VISIBLE_FIELDS), "markdown", now, now),
        )
        migrate_exporter_db(db)
        db.execute(f"PRAGMA user_version = {EXPORTER_DB_VERSION}")
        db.commit()
    finally:
        db.close()
    return {"ok": True, "db": str(app_db_path(base))}


def set_config(base: Path, key: str, value: str) -> None:
    db = connect_db(base)
    try:
        now = utc_now()
        db.execute(
            "INSERT OR REPLACE INTO exporter_config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
        db.commit()
    finally:
        db.close()


def get_config(base: Path, key: str, default: str = "") -> str:
    db = connect_db(base)
    try:
        row = db.execute("SELECT value FROM exporter_config WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default
    finally:
        db.close()


def keychain_available() -> bool:
    if os.environ.get("MOORE_WECHAT_EXPORTER_DISABLE_KEYCHAIN"):
        return False
    return sys.platform == "darwin" and bool(shutil.which("security"))


def keychain_account(profile_id: int, display_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", display_name).strip("-") or "default"
    return f"moore-wechat-exporter-{profile_id}-{safe}"


def keychain_set(account: str, secret: str) -> None:
    service = "moore-wechat-article-downloader"
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", secret],
        text=True,
        capture_output=True,
        check=True,
    )


def keychain_get(account: str) -> str:
    service = "moore-wechat-article-downloader"
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def upsert_login_profile(
    base: Path,
    base_url: str,
    auth_key: str,
    display_name: str,
    expires_at: str,
    allow_plain: bool,
) -> dict[str, Any]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        now = utc_now()
        row = db.execute(
            "SELECT * FROM login_profiles WHERE display_name = ? ORDER BY id LIMIT 1",
            (display_name,),
        ).fetchone()
        if row:
            profile_id = int(row["id"])
            db.execute(
                """
                UPDATE login_profiles
                SET base_url = ?, expires_at = ?, last_login_at = ?, status = 'configured', updated_at = ?
                WHERE id = ?
                """,
                (base_url, expires_at, now, now, profile_id),
            )
        else:
            cur = db.execute(
                """
                INSERT INTO login_profiles
                    (display_name, base_url, expires_at, last_login_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'configured', ?, ?)
                """,
                (display_name, base_url, expires_at, now, now, now),
            )
            profile_id = int(cur.lastrowid)

        account = keychain_account(profile_id, display_name)
        storage_kind = "keychain"
        encrypted_payload = ""
        if keychain_available():
            keychain_set(account, auth_key)
        elif allow_plain:
            storage_kind = "plain"
            encrypted_payload = auth_key
        else:
            raise RuntimeError("macOS Keychain unavailable; rerun with --allow-plain-auth-key to store locally in SQLite")

        db.execute("DELETE FROM credential_store WHERE profile_id = ? AND kind IN ('auth-key', 'auth-key-plain')", (profile_id,))
        db.execute(
            """
            INSERT INTO credential_store
                (profile_id, kind, keychain_account, encrypted_payload, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                "auth-key" if storage_kind == "keychain" else "auth-key-plain",
                account if storage_kind == "keychain" else "",
                encrypted_payload,
                expires_at,
                now,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()
    set_config(base, "base_url", base_url)
    set_config(base, "active_profile", display_name)
    return {
        "ok": True,
        "profile_id": profile_id,
        "display_name": display_name,
        "base_url": base_url,
        "expires_at": expires_at,
        "credential_storage": storage_kind,
        "auth_key_preview": redact_secret(auth_key),
    }


def redact_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * max(len(value) - 8, 4) + value[-4:]


def get_active_profile(base: Path, display_name: str = "") -> sqlite3.Row | None:
    init_exporter_db(base)
    target = display_name or get_config(base, "active_profile", "default")
    db = connect_db(base)
    try:
        row = db.execute(
            "SELECT * FROM login_profiles WHERE display_name = ? ORDER BY id LIMIT 1",
            (target,),
        ).fetchone()
        if row:
            return row
        return db.execute("SELECT * FROM login_profiles ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        db.close()


def get_auth_key(base: Path, profile_name: str = "") -> tuple[sqlite3.Row, str]:
    profile = get_active_profile(base, profile_name)
    if not profile:
        raise RuntimeError("exporter auth-key is not configured; run exporter-config or exporter-login-start first")
    db = connect_db(base)
    try:
        cred = db.execute(
            "SELECT * FROM credential_store WHERE profile_id = ? ORDER BY updated_at DESC LIMIT 1",
            (int(profile["id"]),),
        ).fetchone()
    finally:
        db.close()
    if not cred:
        raise RuntimeError("exporter credential not found; run exporter-config again")
    if str(cred["kind"]) == "auth-key":
        return profile, keychain_get(str(cred["keychain_account"]))
    return profile, str(cred["encrypted_payload"] or "")


def normalize_base_url(value: str) -> str:
    value = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def login_cookie_jar(base: Path, login_id: str) -> http.cookiejar.MozillaCookieJar:
    path = login_cookie_path(base, login_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    jar = http.cookiejar.MozillaCookieJar(str(path))
    if path.exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    return jar


def save_login_cookie_jar(jar: http.cookiejar.MozillaCookieJar) -> None:
    jar.save(ignore_discard=True, ignore_expires=True)


def request_with_cookie_jar(
    base_url: str,
    path: str,
    jar: http.cookiejar.MozillaCookieJar,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[bytes, dict[str, Any], list[str], str]:
    url = normalize_base_url(base_url) + path
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json,text/plain,image/*,*/*",
            "User-Agent": "Moore-WeChat-Exporter/1.0",
        },
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(req, timeout=30) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace") if raw else ""
        content_type = resp.headers.get("Content-Type", "")
        set_cookies = resp.headers.get_all("Set-Cookie") or []
    save_login_cookie_jar(jar)
    payload: dict[str, Any] = {}
    if "json" in content_type or text.strip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            payload = parsed if isinstance(parsed, dict) else {"data": parsed}
        except json.JSONDecodeError:
            payload = {"text": text}
    return raw, payload, set_cookies, content_type


def extract_cookie_value(set_cookies: list[str], name: str) -> str:
    for header in set_cookies:
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            continue
        if name in cookie:
            return str(cookie[name].value)
    return ""


def start_qr_login(base: Path, base_url: str) -> dict[str, Any]:
    init_exporter_db(base)
    base_url = normalize_base_url(base_url or get_config(base, "base_url", DEFAULT_BASE_URL))
    set_config(base, "base_url", base_url)
    login_id = uuid.uuid4().hex
    sid = str(int(time.time() * 1000)) + str(int(time.time_ns() % 100))
    jar = login_cookie_jar(base, login_id)
    _, session_payload, _session_cookies, _session_type = request_with_cookie_jar(
        base_url,
        f"/api/web/login/session/{urllib.parse.quote(sid)}",
        jar,
        method="POST",
        body=b"",
    )
    base_resp = session_payload.get("base_resp") if isinstance(session_payload, dict) else {}
    if isinstance(base_resp, dict) and str(base_resp.get("ret", "0")) not in {"0", ""}:
        raise RuntimeError(str(base_resp.get("err_msg") or "start login session failed"))
    raw_qrcode, _qr_payload, _qr_cookies, content_type = request_with_cookie_jar(
        base_url,
        f"/api/web/login/getqrcode?rnd={time.time()}",
        jar,
    )
    if not raw_qrcode:
        raise RuntimeError("empty QR code response")
    qrcode_path = login_qrcode_path_with_extension(base, login_id, content_type, raw_qrcode)
    qrcode_path.write_bytes(raw_qrcode)
    expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()
    session = {
        "login_id": login_id,
        "sid": sid,
        "base_url": base_url,
        "qrcode_path": str(qrcode_path),
        "qrcode_content_type": content_type or "image/jpeg",
        "status": "waiting_for_scan",
        "created_at": utc_now(),
        "expires_at": expires_at,
    }
    write_json_file(login_session_path(base, login_id), session)
    return {
        "ok": True,
        "login_id": login_id,
        "base_url": base_url,
        "qrcode_path": str(qrcode_path),
        "expires_at": expires_at,
        "next_step": "Scan the QR code with WeChat, confirm login, then run exporter-login-qr-status and exporter-login-qr-complete.",
    }


def load_qr_login_session(base: Path, login_id: str) -> dict[str, Any]:
    path = login_session_path(base, login_id)
    if not path.exists():
        raise RuntimeError(f"QR login session not found: {login_id}")
    data = read_json_file(path)
    if not isinstance(data, dict):
        raise RuntimeError("invalid QR login session file")
    return data


def qr_login_status(base: Path, login_id: str) -> dict[str, Any]:
    session = load_qr_login_session(base, login_id)
    jar = login_cookie_jar(base, login_id)
    _raw, payload, _cookies, _ctype = request_with_cookie_jar(
        str(session["base_url"]),
        "/api/web/login/scan",
        jar,
    )
    status_code = safe_int(payload.get("status"), -1)
    status_map = {
        0: "waiting_for_scan",
        1: "confirmed",
        2: "expired",
        3: "expired",
        4: "scanned_waiting_confirm",
        5: "account_not_bound_email",
        6: "scanned_waiting_confirm",
    }
    status = status_map.get(status_code, "unknown")
    session["status"] = status
    session["last_scan_payload"] = {k: v for k, v in payload.items() if k not in {"uuid", "token", "cookie", "key"}}
    session["updated_at"] = utc_now()
    write_json_file(login_session_path(base, login_id), session)
    return {
        "ok": True,
        "login_id": login_id,
        "status": status,
        "status_code": status_code,
        "acct_size": payload.get("acct_size"),
        "message": payload.get("msg") or payload.get("err_msg") or "",
        "ready_to_complete": status == "confirmed",
    }


def complete_qr_login(base: Path, login_id: str, profile: str = "") -> dict[str, Any]:
    session = load_qr_login_session(base, login_id)
    jar = login_cookie_jar(base, login_id)
    _raw, payload, set_cookies, _ctype = request_with_cookie_jar(
        str(session["base_url"]),
        "/api/web/login/bizlogin",
        jar,
        method="POST",
        body=b"",
    )
    if isinstance(payload, dict) and payload.get("err"):
        raise RuntimeError(str(payload.get("err")))
    auth_key = extract_cookie_value(set_cookies, "auth-key")
    if not auth_key:
        raise RuntimeError("auth-key was not returned by exporter bizlogin")
    nickname = str(payload.get("nickname") or payload.get("nick_name") or profile or "default")
    expires_at = str(payload.get("expires") or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=4)).isoformat())
    result = upsert_login_profile(base, str(session["base_url"]), auth_key, profile or nickname or "default", expires_at, False)
    db = connect_db(base)
    try:
        db.execute(
            "UPDATE login_profiles SET mp_account_name = ?, status = 'valid', updated_at = ? WHERE id = ?",
            (nickname, utc_now(), int(result["profile_id"])),
        )
        db.commit()
    finally:
        db.close()
    session["status"] = "complete"
    session["completed_at"] = utc_now()
    session["profile_id"] = result["profile_id"]
    write_json_file(login_session_path(base, login_id), session)
    result["nickname"] = nickname
    result["avatar"] = payload.get("avatar", "")
    return result


def api_request(base: Path, path: str, params: dict[str, Any] | None = None, profile: str = "") -> Any:
    login, auth_key = get_auth_key(base, profile)
    base_url = normalize_base_url(str(login["base_url"] or get_config(base, "base_url", DEFAULT_BASE_URL)))
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None and str(v) != ""})
    url = f"{base_url}{path}"
    if query:
        url += "?" + query
    req = urllib.request.Request(
        url,
        headers={
            "X-Auth-Key": auth_key,
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Moore-WeChat-Exporter/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"code": 0, "data": text}


def unwrap_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    candidates: list[Any] = []
    candidates.append(payload.get("data"))
    candidates.append(payload.get("list"))
    candidates.append(payload.get("items"))
    candidates.append(payload.get("records"))
    candidates.append(payload.get("articles"))
    candidates.append(payload.get("accounts"))
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("list"), data.get("items"), data.get("records"), data.get("articles"), data.get("accounts")])
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def unwrap_total(payload: Any, fallback: int) -> int:
    if not isinstance(payload, dict):
        return fallback
    data = payload.get("data")
    for obj in [payload, data if isinstance(data, dict) else {}]:
        for key in ["total", "count", "app_msg_cnt", "article_count"]:
            try:
                value = int(obj.get(key))
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass
    return fallback


def normalize_account(item: dict[str, Any]) -> dict[str, Any]:
    fakeid = str(item.get("fakeid") or item.get("fakeId") or item.get("id") or "").strip()
    return {
        "fakeid": fakeid,
        "nickname": str(item.get("nickname") or item.get("name") or item.get("title") or "").strip(),
        "alias": str(item.get("alias") or item.get("username") or item.get("wechat_id") or item.get("wechatId") or "").strip(),
        "avatar_url": str(item.get("avatar") or item.get("avatar_url") or item.get("round_head_img") or item.get("head_img") or "").strip(),
        "description": str(item.get("description") or item.get("signature") or item.get("desc") or "").strip(),
        "article_count": safe_int(item.get("article_count") or item.get("app_msg_cnt") or item.get("count")),
        "raw_json": json_dumps(item),
    }


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_article(item: dict[str, Any], account_id: int) -> dict[str, Any]:
    url = str(
        item.get("url")
        or item.get("link")
        or item.get("content_url")
        or item.get("appmsg_url")
        or item.get("contentUrl")
        or ""
    ).strip()
    try:
        safe_url = clean_url(url)
    except ValueError:
        safe_url = safe_display_url(url)
    title = str(item.get("title") or item.get("name") or "").strip()
    return {
        "account_id": account_id,
        "msgid": str(item.get("msgid") or item.get("msg_id") or item.get("appmsgid") or item.get("aid") or "").strip(),
        "idx": safe_int(item.get("idx") or item.get("itemidx") or item.get("item_idx")),
        "title": title,
        "url": safe_url,
        "digest": str(item.get("digest") or item.get("summary") or item.get("desc") or "").strip(),
        "cover_url": str(item.get("cover") or item.get("cover_url") or item.get("coverUrl") or item.get("pic_url") or "").strip(),
        "author": str(item.get("author") or item.get("author_name") or "").strip(),
        "publish_time": parse_time(item.get("publish_time") or item.get("publishTime") or item.get("update_time") or item.get("datetime")),
        "create_time": parse_time(item.get("create_time") or item.get("createTime")),
        "is_original": 1 if str(item.get("is_original") or item.get("copyright_stat") or "").lower() in {"1", "true", "original"} else 0,
        "is_deleted": 1 if str(item.get("is_deleted") or item.get("deleted") or "").lower() in {"1", "true"} else 0,
        "article_status": str(item.get("article_status") or item.get("status") or "").strip(),
        "content_downloaded": 0,
        "collection_title": str(item.get("collection_title") or item.get("album_title") or item.get("tag_name") or "").strip(),
        "raw_json": json_dumps(item),
    }


def maybe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_comment_id_from_html(value: str) -> str:
    return extract_wechat_article_context(value, "").get("comment_id", "")


def biz_from_article_url(value: str) -> str:
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query, keep_blank_values=True)
    except ValueError:
        return ""
    return html.unescape(str((values.get("__biz") or [""])[0])).strip()


def resolve_article_context(
    base: Path,
    article_id: int,
    html_text: str = "",
    biz: str = "",
    source: str = "html",
    comment_id: str = "",
) -> dict[str, Any]:
    """Store only non-sensitive article identifiers needed by a future worker."""
    init_exporter_db(base)
    db = connect_db(base)
    try:
        row = db.execute(
            "SELECT id, account_id, msgid, idx, url FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "article not found", "article_id": article_id}
        account_id = int(row["account_id"])
        account_mapping = db.execute("SELECT biz FROM account_biz_mappings WHERE account_id = ?", (account_id,)).fetchone()
        explicit_biz = str(biz or biz_from_article_url(str(row["url"] or ""))).strip()
        resolved_biz = explicit_biz or (str(account_mapping["biz"] or "").strip() if account_mapping else "")
        mapped = db.execute("SELECT account_id FROM account_biz_mappings WHERE biz = ?", (resolved_biz,)).fetchone() if resolved_biz else None
        if mapped and int(mapped["account_id"]) != account_id:
            return {
                "ok": False,
                "article_id": article_id,
                "context_status": "mapping_conflict",
                "error": "__biz is already mapped to another local account",
            }
        if account_mapping and resolved_biz and str(account_mapping["biz"]) != resolved_biz:
            return {
                "ok": False,
                "article_id": article_id,
                "context_status": "mapping_conflict",
                "error": "local account is already mapped to another __biz",
            }
        now = utc_now()
        if resolved_biz and not mapped and not account_mapping:
            db.execute(
                """
                INSERT INTO account_biz_mappings (account_id, biz, source, status, created_at, updated_at)
                VALUES (?, ?, ?, 'verified', ?, ?)
                """,
                (account_id, resolved_biz, source, now, now),
            )
        resolved_comment_id = str(comment_id or extract_comment_id_from_html(html_text)).strip()
        msgid = str(row["msgid"] or "")
        status = "ready" if resolved_biz and msgid and resolved_comment_id else "incomplete"
        db.execute(
            """
            INSERT INTO article_contexts
                (article_id, account_id, biz, msgid, idx, comment_id, url, context_status, source, resolved_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                account_id = excluded.account_id,
                biz = CASE WHEN excluded.biz <> '' THEN excluded.biz ELSE article_contexts.biz END,
                msgid = excluded.msgid,
                idx = excluded.idx,
                comment_id = CASE WHEN excluded.comment_id <> '' THEN excluded.comment_id ELSE article_contexts.comment_id END,
                url = excluded.url,
                context_status = excluded.context_status,
                source = excluded.source,
                resolved_at = excluded.resolved_at,
                updated_at = excluded.updated_at
            """,
            (
                article_id,
                account_id,
                resolved_biz,
                msgid,
                int(row["idx"] or 0),
                resolved_comment_id,
                str(row["url"] or ""),
                status,
                source,
                now,
                now,
            ),
        )
        db.commit()
        context = db.execute("SELECT * FROM article_contexts WHERE article_id = ?", (article_id,)).fetchone()
        return {"ok": status == "ready", "article_id": article_id, "context_status": status, "context": dict(context)}
    finally:
        db.close()


def comment_identity(row: dict[str, Any]) -> str:
    value = str(row.get("comment_id") or row.get("id") or "").strip()
    if value:
        return value
    fingerprint = "\n".join(
        [
            str(row.get("nick_name") or row.get("nickname") or row.get("user") or ""),
            str(row.get("content") or row.get("comment") or ""),
            str(row.get("create_time") or row.get("time") or ""),
        ]
    )
    return "derived-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]


def upsert_comment_row(db: sqlite3.Connection, article_id: int, row: dict[str, Any], source: str = "import") -> None:
    now = utc_now()
    scope = str(row.get("comment_scope") or row.get("scope") or "elected").strip() or "elected"
    db.execute(
        """
        INSERT INTO article_comments
            (article_id, comment_id, nick_name, content, like_count, create_time, raw_json, created_at,
             comment_scope, source, fetched_at, complete)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_id, comment_id, comment_scope) DO UPDATE SET
            nick_name = excluded.nick_name,
            content = excluded.content,
            like_count = excluded.like_count,
            create_time = excluded.create_time,
            raw_json = excluded.raw_json,
            source = excluded.source,
            fetched_at = excluded.fetched_at,
            complete = excluded.complete
        """,
        (
            article_id,
            comment_identity(row),
            str(row.get("nick_name") or row.get("nickname") or row.get("user") or ""),
            str(row.get("content") or row.get("comment") or ""),
            maybe_int(row.get("like_count") or row.get("like")),
            parse_time(row.get("create_time") or row.get("time")),
            json_dumps(scrub_payload(row)),
            now,
            scope,
            str(row.get("source") or source),
            str(row.get("fetched_at") or now),
            1 if bool(row.get("complete")) else 0,
        ),
    )


def search_accounts(base: Path, keyword: str, begin: int = 0, size: int = 10, profile: str = "") -> dict[str, Any]:
    payload = api_request(base, "/api/public/v1/account", {"keyword": keyword, "begin": begin, "size": size}, profile)
    items = [normalize_account(item) for item in unwrap_items(payload)]
    items = [item for item in items if item.get("fakeid")]
    return {"ok": True, "keyword": keyword, "begin": begin, "size": size, "count": len(items), "accounts": items, "raw_code": payload.get("code") if isinstance(payload, dict) else None}


def account_by_url(base: Path, url: str, profile: str = "") -> dict[str, Any]:
    payload = api_request(base, "/api/public/v1/accountbyurl", {"url": clean_url(url)}, profile)
    items = unwrap_items(payload)
    if not items and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        items = [payload["data"]]
    accounts = [normalize_account(item) for item in items]
    return {"ok": bool(accounts), "accounts": accounts, "raw_code": payload.get("code") if isinstance(payload, dict) else None}


def upsert_account(base: Path, account: dict[str, Any]) -> dict[str, Any]:
    init_exporter_db(base)
    fakeid = str(account.get("fakeid") or "").strip()
    if not fakeid:
        raise ValueError("fakeid is required")
    now = utc_now()
    db = connect_db(base)
    try:
        db.execute(
            """
            INSERT INTO target_accounts
                (fakeid, nickname, alias, avatar_url, description, article_count, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fakeid) DO UPDATE SET
                nickname = excluded.nickname,
                alias = excluded.alias,
                avatar_url = excluded.avatar_url,
                description = excluded.description,
                article_count = CASE WHEN excluded.article_count > 0 THEN excluded.article_count ELSE target_accounts.article_count END,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            (
                fakeid,
                str(account.get("nickname") or fakeid),
                str(account.get("alias") or ""),
                str(account.get("avatar_url") or ""),
                str(account.get("description") or ""),
                safe_int(account.get("article_count")),
                str(account.get("raw_json") or json_dumps(account)),
                now,
                now,
            ),
        )
        row = db.execute("SELECT * FROM target_accounts WHERE fakeid = ?", (fakeid,)).fetchone()
        db.commit()
        return {"ok": True, "account": dict(row)}
    finally:
        db.close()


def get_account_row(base: Path, account_id: int = 0, fakeid: str = "") -> sqlite3.Row:
    db = connect_db(base)
    try:
        if account_id:
            row = db.execute("SELECT * FROM target_accounts WHERE id = ?", (account_id,)).fetchone()
        elif fakeid:
            row = db.execute("SELECT * FROM target_accounts WHERE fakeid = ?", (fakeid,)).fetchone()
        else:
            row = None
        if not row:
            raise RuntimeError("target account not found")
        return row
    finally:
        db.close()


def list_accounts(base: Path) -> list[dict[str, Any]]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        rows = db.execute(
            """
            SELECT id, fakeid, nickname, alias, avatar_url, description, article_count,
                   synced_count, last_sync_at, created_at, updated_at
            FROM target_accounts
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def insert_sync_job(base: Path, account_id: int | None, kind: str) -> int:
    db = connect_db(base)
    try:
        cur = db.execute(
            "INSERT INTO sync_jobs (account_id, kind, status, progress, started_at) VALUES (?, ?, 'running', 0, ?)",
            (account_id, kind, utc_now()),
        )
        db.commit()
        return int(cur.lastrowid)
    finally:
        db.close()


def finish_sync_job(base: Path, job_id: int, status: str, progress: int, error: str = "", raw: Any = None) -> None:
    db = connect_db(base)
    try:
        db.execute(
            """
            UPDATE sync_jobs
            SET status = ?, progress = ?, finished_at = ?, error = ?, raw_json = ?
            WHERE id = ?
            """,
            (status, progress, utc_now(), error, json_dumps(raw or {}), job_id),
        )
        db.commit()
    finally:
        db.close()


def sync_account_articles(base: Path, account_id: int, limit: int, keyword: str = "", profile: str = "") -> dict[str, Any]:
    account = get_account_row(base, account_id=account_id)
    job_id = insert_sync_job(base, account_id, "account-articles")
    begin = 0
    size = 20
    total_seen = 0
    inserted = 0
    errors: list[str] = []
    raw_last: Any = {}
    seen_page_keys: set[str] = set()
    try:
        while True:
            if limit > 0 and total_seen >= limit:
                break
            page_size = min(size, limit - total_seen) if limit > 0 else size
            payload = api_request(
                base,
                "/api/public/v1/article",
                {"fakeid": account["fakeid"], "begin": begin, "size": page_size, "keyword": keyword},
                profile,
            )
            raw_last = payload
            items = unwrap_items(payload)
            if not items:
                break
            new_items = []
            for item in items:
                key = str(
                    item.get("url")
                    or item.get("link")
                    or item.get("content_url")
                    or item.get("appmsg_url")
                    or item.get("contentUrl")
                    or item.get("msgid")
                    or item.get("msg_id")
                    or item.get("appmsgid")
                    or item.get("aid")
                    or json_dumps(item)
                )
                if key in seen_page_keys:
                    continue
                seen_page_keys.add(key)
                new_items.append(item)
            if not new_items:
                break
            rows = [normalize_article(item, account_id) for item in new_items]
            rows = [row for row in rows if row.get("title") and row.get("url")]
            inserted += upsert_articles(base, rows)
            total_seen += len(new_items)
            total = unwrap_total(payload, 0)
            if len(items) < page_size or (total and begin + len(items) >= total):
                break
            begin += len(items)
        db = connect_db(base)
        try:
            synced_count = db.execute("SELECT COUNT(*) AS c FROM articles WHERE account_id = ?", (account_id,)).fetchone()["c"]
            db.execute(
                """
                UPDATE target_accounts
                SET synced_count = ?, last_sync_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(synced_count), utc_now(), utc_now(), account_id),
            )
            db.commit()
        finally:
            db.close()
        ensure_collections_from_articles(base, account_id)
        finish_sync_job(base, job_id, "success", total_seen, raw=raw_last)
    except Exception as exc:
        errors.append(str(exc))
        finish_sync_job(base, job_id, "failed", total_seen, str(exc), raw_last)
    return {
        "ok": not errors,
        "job_id": job_id,
        "account_id": account_id,
        "account": account["nickname"],
        "fetched_count": total_seen,
        "upserted_count": inserted,
        "errors": errors,
    }


def upsert_articles(base: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = utc_now()
    db = connect_db(base)
    changed = 0
    try:
        for row in rows:
            db.execute(
                f"""
                INSERT INTO articles ({", ".join(ARTICLE_FIELDS)})
                VALUES ({", ".join(["?"] * len(ARTICLE_FIELDS))})
                ON CONFLICT(account_id, url) DO UPDATE SET
                    msgid = excluded.msgid,
                    idx = excluded.idx,
                    title = excluded.title,
                    digest = excluded.digest,
                    cover_url = excluded.cover_url,
                    author = excluded.author,
                    publish_time = excluded.publish_time,
                    create_time = excluded.create_time,
                    is_original = excluded.is_original,
                    is_deleted = excluded.is_deleted,
                    article_status = excluded.article_status,
                    collection_title = excluded.collection_title,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                tuple(row.get(field) if field not in {"id", "created_at", "updated_at"} else (None if field == "id" else now) for field in ARTICLE_FIELDS),
            )
            changed += 1
        db.commit()
    finally:
        db.close()
    return changed


def ensure_collections_from_articles(base: Path, account_id: int) -> None:
    db = connect_db(base)
    try:
        rows = db.execute(
            """
            SELECT collection_title, COUNT(*) AS c
            FROM articles
            WHERE account_id = ? AND collection_title != ''
            GROUP BY collection_title
            """,
            (account_id,),
        ).fetchall()
        now = utc_now()
        for row in rows:
            title = str(row["collection_title"])
            db.execute(
                """
                INSERT INTO collections (account_id, title, article_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, title) DO UPDATE SET
                    article_count = excluded.article_count,
                    updated_at = excluded.updated_at
                """,
                (account_id, title, int(row["c"]), now, now),
            )
            collection_id = db.execute(
                "SELECT id FROM collections WHERE account_id = ? AND title = ?",
                (account_id, title),
            ).fetchone()["id"]
            article_rows = db.execute(
                """
                SELECT id
                FROM articles
                WHERE account_id = ? AND collection_title = ?
                ORDER BY publish_time DESC, id DESC
                """,
                (account_id, title),
            ).fetchall()
            for order, article in enumerate(article_rows, 1):
                db.execute(
                    """
                    INSERT OR REPLACE INTO collection_articles (collection_id, article_id, sort_order)
                    VALUES (?, ?, ?)
                    """,
                    (collection_id, int(article["id"]), order),
                )
        db.commit()
    finally:
        db.close()


def list_articles(
    base: Path,
    account_id: int = 0,
    limit: int = 100,
    keyword: str = "",
    collection_id: int = 0,
    downloaded: str = "",
) -> list[dict[str, Any]]:
    init_exporter_db(base)
    where = []
    params: list[Any] = []
    join = ""
    if collection_id:
        join = "JOIN collection_articles ca ON ca.article_id = a.id"
        where.append("ca.collection_id = ?")
        params.append(collection_id)
    if account_id:
        where.append("a.account_id = ?")
        params.append(account_id)
    if keyword:
        where.append("(a.title LIKE ? OR a.digest LIKE ? OR a.author LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    if downloaded == "yes":
        where.append("a.content_downloaded = 1")
    elif downloaded == "no":
        where.append("a.content_downloaded = 0")
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    order = "ca.sort_order ASC" if collection_id else "a.publish_time DESC, a.id DESC"
    params.append(limit)
    db = connect_db(base)
    try:
        rows = db.execute(
            f"""
            SELECT a.*, t.nickname AS account_name, t.fakeid
            FROM articles a
            JOIN target_accounts t ON t.id = a.account_id
            {join}
            {sql_where}
            ORDER BY {order}
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def preview_article(base: Path, article_id: int) -> dict[str, Any]:
    db = connect_db(base)
    try:
        row = db.execute(
            """
            SELECT a.*, t.nickname AS account_name
            FROM articles a
            JOIN target_accounts t ON t.id = a.account_id
            WHERE a.id = ?
            """,
            (article_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("article not found")
        item = dict(row)
        return {
            "ok": True,
            "article": {
                "id": item["id"],
                "account": item["account_name"],
                "title": item["title"],
                "publish_time": item["publish_time"],
                "author": item["author"],
                "digest": item["digest"],
                "url": item["url"],
                "cover_url": item["cover_url"],
                "collection_title": item["collection_title"],
            },
        }
    finally:
        db.close()


def open_original(base: Path, article_id: int) -> dict[str, Any]:
    article = preview_article(base, article_id)["article"]
    opened = False
    try:
        webbrowser.open(article["url"])
        opened = True
    except Exception:
        opened = False
    return {"ok": True, "opened": opened, "url": article["url"], "title": article["title"]}


def get_article_download_rows(base: Path, article_ids: list[int]) -> list[dict[str, Any]]:
    if not article_ids:
        return []
    placeholders = ",".join(["?"] * len(article_ids))
    db = connect_db(base)
    try:
        rows = db.execute(
            f"""
            SELECT
                a.id,
                a.account_id,
                a.msgid,
                a.idx,
                a.title,
                a.url,
                a.publish_time,
                a.content_downloaded,
                t.nickname AS account_name
            FROM articles a
            JOIN target_accounts t ON t.id = a.account_id
            WHERE a.id IN ({placeholders})
            ORDER BY a.publish_time DESC, a.id DESC
            """,
            article_ids,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def get_article_urls(base: Path, article_ids: list[int]) -> list[tuple[int, str]]:
    rows = get_article_download_rows(base, article_ids)
    return [(int(row["id"]), str(row["url"])) for row in rows]


def select_article_ids(
    base: Path,
    account_id: int = 0,
    article_ids: str = "",
    latest: int | None = None,
    titles: str = "",
    keyword: str = "",
    collection_id: int = 0,
) -> list[int]:
    if article_ids:
        return [int(part) for part in re.split(r"[,，\s]+", article_ids.strip()) if part.strip()]
    rows = list_articles(base, account_id, max(latest or 1000, 1), keyword, collection_id)
    if titles:
        needles = [part.strip().lower() for part in re.split(r"[,，\n]+", titles) if part.strip()]
        rows = [row for row in rows if any(needle in str(row["title"]).lower() for needle in needles)]
    if latest is not None:
        rows = rows[:latest]
    return [int(row["id"]) for row in rows]


def _safe_dir_name(name: str) -> str:
    safe = re.sub(r'[/\\:*?"<>|]', "_", name).strip()
    return safe or "account"


def unique_article_file_stems(rows: list[dict[str, Any]]) -> dict[str, str]:
    counts: dict[str, int] = {}
    for row in rows:
        stem = _safe_dir_name(str(row.get("title") or "untitled"))
        counts[stem] = counts.get(stem, 0) + 1
    result: dict[str, str] = {}
    for row in rows:
        stem = _safe_dir_name(str(row.get("title") or "untitled"))
        if counts.get(stem, 0) > 1:
            suffix = str(row.get("msgid") or row.get("id") or "").strip()
            stem = f"{stem}-{suffix}" if suffix else stem
        result[str(row.get("url") or "")] = stem
    return result


def write_account_index(output_dir: Path, rows: list[dict[str, Any]], manifest: dict[str, Any], run_id: str) -> None:
    index_path = output_dir / "index.csv"
    fields = [
        "seq",
        "db_article_id",
        "msgid",
        "title",
        "account",
        "publish_time",
        "source_url",
        "markdown_path",
        "image_dir",
        "image_count",
        "status",
        "error",
        "downloaded_at",
        "run_id",
        "content_article_id",
    ]
    existing: dict[str, dict[str, Any]] = {}
    if index_path.exists():
        with index_path.open("r", encoding="utf-8-sig", newline="") as fh:
            for item in csv.DictReader(fh):
                key = str(item.get("db_article_id") or item.get("source_url") or "")
                if key:
                    existing[key] = dict(item)
    row_by_url = {str(row.get("url") or ""): row for row in rows}
    downloaded_at = utc_now()
    for item in manifest.get("articles", []):
        source_url = str(item.get("source_url") or "")
        row = row_by_url.get(source_url)
        if not row:
            continue
        key = str(row.get("id"))
        existing.pop(source_url, None)
        existing[key] = {
            "seq": item.get("seq", ""),
            "db_article_id": row.get("id", ""),
            "msgid": row.get("msgid", ""),
            "title": row.get("title", item.get("title", "")),
            "account": row.get("account_name", item.get("account", "")),
            "publish_time": row.get("publish_time", ""),
            "source_url": source_url,
            "markdown_path": item.get("markdown_path", ""),
            "image_dir": item.get("image_dir", ""),
            "image_count": item.get("image_count", ""),
            "status": item.get("status", "success"),
            "error": item.get("error", ""),
            "downloaded_at": downloaded_at,
            "run_id": run_id,
            "content_article_id": item.get("article_id", ""),
        }
    for item in manifest.get("failed", []):
        source_url = str(item.get("source_url") or "")
        row = row_by_url.get(source_url)
        key = str(row.get("id")) if row else source_url
        if not key:
            continue
        if row:
            existing.pop(source_url, None)
        existing[key] = {
            **{field: "" for field in fields},
            "seq": item.get("seq", ""),
            "db_article_id": row.get("id", "") if row else "",
            "msgid": row.get("msgid", "") if row else "",
            "title": row.get("title", item.get("title", "")) if row else item.get("title", ""),
            "account": row.get("account_name", item.get("account", "")) if row else item.get("account", ""),
            "publish_time": row.get("publish_time", "") if row else "",
            "source_url": source_url,
            "status": "failed",
            "error": item.get("error", ""),
            "downloaded_at": downloaded_at,
            "run_id": run_id,
        }
    ordered = sorted(existing.values(), key=lambda item: (str(item.get("publish_time") or ""), str(item.get("db_article_id") or "")), reverse=True)
    with index_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for item in ordered:
            writer.writerow({field: item.get(field, "") for field in fields})


def read_account_index(output_dir: Path) -> dict[str, dict[str, str]]:
    index_path = output_dir / "index.csv"
    if not index_path.exists():
        return {}
    rows: dict[str, dict[str, str]] = {}
    with index_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for item in csv.DictReader(fh):
            key = str(item.get("db_article_id") or item.get("source_url") or "")
            if key:
                rows[key] = dict(item)
            source_url = str(item.get("source_url") or "")
            if source_url:
                rows[source_url] = dict(item)
    return rows


def account_index_file_exists(output_dir: Path, row: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    indexed = read_account_index(output_dir)
    item = indexed.get(str(row.get("id") or "")) or indexed.get(str(row.get("url") or ""))
    if not item or item.get("status") != "success":
        return False, item or {}
    markdown = str(item.get("markdown_path") or "")
    if not markdown:
        return False, item
    markdown_path = Path(markdown)
    if not markdown_path.is_absolute():
        markdown_path = output_dir / markdown_path
    if not markdown_path.exists():
        return False, item
    try:
        image_count = int(item.get("image_count") or 0)
    except (TypeError, ValueError):
        image_count = 0
    image_dir = str(item.get("image_dir") or "")
    if image_count > 0:
        image_path = Path(image_dir)
        if not image_path.is_absolute():
            image_path = output_dir / image_path
        if not image_path.exists():
            return False, item
        try:
            image_files = [path for path in image_path.iterdir() if path.is_file()]
        except OSError:
            return False, item
        if len(image_files) < image_count:
            return False, item
    return True, item


def account_output_dir(root: str, account_name: str) -> Path:
    safe_account = _safe_dir_name(account_name)
    if root:
        root_path = Path(root).expanduser().resolve()
        if root_path.name == safe_account:
            return root_path
        return (root_path / safe_account).resolve()
    return (DEFAULT_DELIVERY_DIR / safe_account).expanduser().resolve()


def log_evolution_event(
    base: Path,
    stage: str,
    code: str,
    severity: str = "info",
    account_id: int = 0,
    article_id: int = 0,
    run_id: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_exporter_db(base)
    event_id = "evo-" + make_run_id()
    safe_detail = scrub_payload(detail or {})
    db = connect_db(base)
    try:
        db.execute(
            """
            INSERT INTO evolution_events
                (event_id, stage, code, severity, account_id, article_id, run_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                stage,
                code,
                severity,
                int(account_id or 0),
                int(article_id or 0),
                run_id,
                json_dumps(safe_detail),
                utc_now(),
            ),
        )
        db.commit()
    finally:
        db.close()
    return {"event_id": event_id, "stage": stage, "code": code}


def list_evolution_events(base: Path, limit: int = 50) -> list[dict[str, Any]]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        rows = db.execute(
            """
            SELECT event_id, stage, code, severity, account_id, article_id, run_id, detail_json, created_at
            FROM evolution_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit or 50), 500)),),
        ).fetchall()
    finally:
        db.close()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = load_json_text(str(item.pop("detail_json") or "{}")) or {}
        result.append(item)
    return result


def export_evolution_fixture(base: Path, output_path: str, limit: int = 50) -> dict[str, Any]:
    events = list_evolution_events(base, limit)
    payload = scrub_payload(
        {
            "version": 1,
            "created_at": utc_now(),
            "source": "wechat-collection-diagnostics",
            "event_count": len(events),
            "events": events,
            "reference_project_checklist": [
                "确认竞品是否仅获取精选评论，不宣称全量评论。",
                "确认互动接口参数来源：__biz、msgid、idx、comment_id 与短时会话凭证。",
                "确认失败事件是否能复现到本地 fixture，且不包含 cookie/token/key/pass_ticket/auth-key。",
            ],
        }
    )
    path = Path(output_path).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "fixture": str(path), "event_count": len(events)}


def verify_account_library(base: Path, account_id: int, output_dir: Path) -> dict[str, Any]:
    init_exporter_db(base)
    indexed = read_account_index(output_dir)
    issues: list[dict[str, Any]] = []
    fixed = 0
    db = connect_db(base)
    try:
        rows = db.execute(
            """
            SELECT id, title, url, content_downloaded
            FROM articles
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchall()
        for row in rows:
            item = dict(row)
            index_item = indexed.get(str(item["id"])) or indexed.get(str(item["url"] or ""))
            markdown_rel = str((index_item or {}).get("markdown_path") or "")
            markdown_exists = False
            if markdown_rel:
                markdown_path = Path(markdown_rel)
                if not markdown_path.is_absolute():
                    markdown_path = output_dir / markdown_path
                markdown_exists = markdown_path.exists()
            if int(item.get("content_downloaded") or 0) and not markdown_exists:
                issues.append(
                    {
                        "article_id": int(item["id"]),
                        "code": "downloaded_markdown_missing",
                        "title": str(item.get("title") or ""),
                    }
                )
                db.execute("UPDATE articles SET content_downloaded = 0, updated_at = ? WHERE id = ?", (utc_now(), int(item["id"])))
                fixed += 1
            if index_item and str(index_item.get("status") or "") == "success" and not markdown_exists:
                issues.append(
                    {
                        "article_id": int(item["id"]),
                        "code": "index_markdown_missing",
                        "title": str(item.get("title") or ""),
                    }
                )
        db.commit()
    finally:
        db.close()
    if issues:
        log_evolution_event(
            base,
            "library_verify",
            "library_inconsistent",
            "warning",
            account_id=account_id,
            detail={"issue_count": len(issues), "fixed_count": fixed, "issue_codes": sorted({item["code"] for item in issues})},
        )
    return {"ok": not issues, "issue_count": len(issues), "fixed_count": fixed, "issues": issues[:20]}


def download_account_articles(
    base: Path,
    rows: list[dict[str, Any]],
    output_root: str = "",
    no_assets: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    if not rows:
        raise RuntimeError("no articles selected")
    account_name = str(rows[0].get("account_name") or "account")
    out_dir = account_output_dir(output_root, account_name)
    skipped: list[dict[str, Any]] = []
    rows_to_download: list[dict[str, Any]] = []
    redownload_count = 0
    missing_downloaded_ids: list[int] = []
    for row in rows:
        exists, index_item = account_index_file_exists(out_dir, row)
        if exists and not force:
            skipped.append({
                "article_id": int(row["id"]),
                "title": row.get("title", ""),
                "source_url": row.get("url", ""),
                "markdown_path": index_item.get("markdown_path", ""),
                "image_dir": index_item.get("image_dir", ""),
                "status": "skipped",
                "skip_reason": "file_exists",
            })
        else:
            if exists or int(row.get("content_downloaded") or 0):
                redownload_count += 1
            if int(row.get("content_downloaded") or 0):
                missing_downloaded_ids.append(int(row["id"]))
            rows_to_download.append(row)
    if not rows_to_download:
        library_check = verify_account_library(base, int(rows[0].get("account_id") or 0), out_dir)
        return {
            "ok": True,
            "run_id": "",
            "account": account_name,
            "account_id": int(rows[0].get("account_id") or 0),
            "output_dir": str(out_dir),
            "index": str(out_dir / "index.csv"),
            "selected_count": len(rows),
            "success_count": 0,
            "failure_count": 0,
            "skipped_count": len(skipped),
            "redownload_count": 0,
            "skipped": skipped,
            "failed": [],
            "library_check": library_check,
        }
    pairs = [(int(row["id"]), str(row["url"])) for row in rows_to_download]
    urls = [url for _article_id, url in pairs]
    article_ids = [article_id for article_id, _url in pairs]
    if missing_downloaded_ids:
        db = connect_db(base)
        try:
            for article_id in missing_downloaded_ids:
                db.execute("UPDATE articles SET content_downloaded = 0, updated_at = ? WHERE id = ?", (utc_now(), article_id))
            db.commit()
        finally:
            db.close()
    run_id = make_run_id()
    file_stems = unique_article_file_stems(rows_to_download)
    manifest = run_markdown_only_download(
        urls,
        out_dir,
        not no_assets,
        {"mode": "exporter-download", "article_ids": article_ids, "account": account_name, "file_stems": file_stems},
        run_id,
    )
    context_by_url = {
        str(item.get("source_url") or ""): item.get("article_context")
        for item in manifest.get("articles", [])
        if isinstance(item.get("article_context"), dict)
    }
    write_account_index(out_dir, rows_to_download, manifest, run_id)
    success_urls = {item.get("source_url") for item in manifest.get("articles", [])}
    db = connect_db(base)
    try:
        for article_id, url in pairs:
            if url in success_urls:
                db.execute("UPDATE articles SET content_downloaded = 1, updated_at = ? WHERE id = ?", (utc_now(), article_id))
        db.execute(
            """
            INSERT INTO download_runs (article_ids_json, output_dir, success_count, failed_count, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (json_dumps(article_ids), str(out_dir), int(manifest["success_count"]), int(manifest["failure_count"]), utc_now()),
        )
        db.commit()
    finally:
        db.close()
    context_results = []
    for article_id, url in pairs:
        context = context_by_url.get(url)
        if context:
            context_results.append(
                resolve_article_context(
                    base,
                    article_id,
                    biz=str(context.get("biz") or ""),
                    comment_id=str(context.get("comment_id") or ""),
                    source="public_html",
                )
            )
    library_check = verify_account_library(base, int(rows[0].get("account_id") or 0), out_dir)
    return {
        "ok": manifest["failure_count"] == 0,
        "run_id": manifest["run_id"],
        "account": account_name,
        "account_id": int(rows[0].get("account_id") or 0),
        "output_dir": manifest["output_dir"],
        "index": manifest["index"],
        "selected_count": len(rows),
        "success_count": manifest["success_count"],
        "failure_count": manifest["failure_count"],
        "skipped_count": len(skipped),
        "redownload_count": redownload_count,
        "skipped": skipped,
        "failed": manifest["failed"],
        "article_contexts": context_results,
        "library_check": library_check,
    }


def download_articles(
    base: Path,
    article_ids: list[int],
    output_dir: str = "",
    no_assets: bool = False,
    account_nickname: str = "",
    force: bool = False,
) -> dict[str, Any]:
    rows = get_article_download_rows(base, article_ids)
    if not rows:
        raise RuntimeError("no articles selected")
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row.get("account_id") or 0), []).append(row)
    if len(grouped) == 1:
        only_rows = next(iter(grouped.values()))
        return download_account_articles(base, only_rows, output_dir, no_assets, force)
    results = [download_account_articles(base, group_rows, output_dir, no_assets, force) for _account_id, group_rows in sorted(grouped.items())]
    return {
        "ok": all(result.get("ok") for result in results),
        "mode": "multi-account",
        "account_count": len(results),
        "selected_count": sum(int(result.get("selected_count") or 0) for result in results),
        "success_count": sum(int(result.get("success_count") or 0) for result in results),
        "failure_count": sum(int(result.get("failure_count") or 0) for result in results),
        "skipped_count": sum(int(result.get("skipped_count") or 0) for result in results),
        "redownload_count": sum(int(result.get("redownload_count") or 0) for result in results),
        "results": results,
        "output_dirs": [result.get("output_dir") for result in results],
        "indexes": [result.get("index") for result in results],
        "skipped": [item for result in results for item in result.get("skipped", [])],
        "failed": [item for result in results for item in result.get("failed", [])],
    }


def latest_metrics_by_article(base: Path, article_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    db = connect_db(base)
    try:
        rows = db.execute(
            f"""
            SELECT m.*
            FROM article_metrics m
            JOIN (
                SELECT article_id, MAX(captured_at) AS captured_at
                FROM article_metrics
                WHERE article_id IN ({placeholders})
                GROUP BY article_id
            ) latest
              ON latest.article_id = m.article_id
             AND latest.captured_at = m.captured_at
            """,
            article_ids,
        ).fetchall()
        return {int(row["article_id"]): dict(row) for row in rows}
    finally:
        db.close()


def elected_comments_by_article(base: Path, article_ids: list[int], limit_per_article: int = 100) -> dict[int, list[dict[str, Any]]]:
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    db = connect_db(base)
    try:
        rows = db.execute(
            f"""
            SELECT *
            FROM article_comments
            WHERE article_id IN ({placeholders})
              AND comment_scope = 'elected'
            ORDER BY article_id, like_count DESC, create_time DESC, id DESC
            """,
            article_ids,
        ).fetchall()
    finally:
        db.close()
    result: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        article_id = int(row["article_id"])
        items = result.setdefault(article_id, [])
        if len(items) < max(1, limit_per_article):
            items.append(dict(row))
    return result


def render_engagement_page_data_markdown(metrics: dict[str, Any] | None, comments: list[dict[str, Any]], captured_at: str = "") -> str:
    metrics = metrics or {}
    metric_labels = [
        ("read_count", "阅读"),
        ("like_count", "点赞"),
        ("old_like_count", "在看"),
        ("comment_count", "评论数"),
        ("favorite_count", "收藏数"),
        ("share_count", "分享"),
    ]
    lines = [
        PAGE_DATA_START,
        "",
        "## 页面数据",
        "",
        f"- 抓取时间：{captured_at or metrics.get('captured_at') or utc_now()}",
        "- 数据来源：微信短时会话接口",
        "- 数据边界：只包含接口返回的互动指标和精选评论；不等于全量评论或完整回复树。",
        "",
        "### 互动数据",
        "",
        "| 字段 | 值 | 来源 |",
        "|---|---:|---|",
    ]
    for key, label in metric_labels:
        value = metrics.get(key)
        display = "missing" if value is None or value == "" else str(value)
        source = str(metrics.get("source") or "wechat_session_api") if display != "missing" else "missing"
        lines.append(f"| {label} | {markdown_cell(display)} | {markdown_cell(source)} |")
    lines.extend(
        [
            "",
            "### 精选评论",
            "",
            f"- 评论范围：精选评论",
            f"- 评论数量：{len(comments)}",
        ]
    )
    if comments:
        lines.extend(["", "| 序号 | 昵称 | 时间 | 点赞 | 评论 |", "|---:|---|---|---:|---|"])
        for index, item in enumerate(comments, start=1):
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(index),
                        markdown_cell(item.get("nick_name") or "missing"),
                        markdown_cell(item.get("create_time") or "missing"),
                        markdown_cell(item.get("like_count") if item.get("like_count") is not None else "missing"),
                        markdown_cell(item.get("content") or "missing"),
                    ]
                )
                + " |"
            )
    else:
        lines.extend(["", "- missing"])
    lines.extend(["", PAGE_DATA_END, ""])
    return "\n".join(lines)


def upsert_markdown_section(markdown_path: Path, section: str) -> bool:
    if not markdown_path.exists():
        return False
    current = markdown_path.read_text(encoding="utf-8")
    block_re = re.compile(rf"\n*{re.escape(PAGE_DATA_START)}.*?{re.escape(PAGE_DATA_END)}\n*", re.S)
    if block_re.search(current):
        updated = block_re.sub("\n\n" + section.strip() + "\n", current).rstrip() + "\n"
    else:
        updated = current.rstrip() + "\n\n" + section.strip() + "\n"
    if updated != current:
        markdown_path.write_text(updated, encoding="utf-8")
    return True


def write_engagement_to_markdown(base: Path, article_ids: list[int], output_root: str = "") -> dict[str, Any]:
    rows = get_article_download_rows(base, article_ids)
    if not rows:
        return {"ok": False, "error": "no articles selected", "updated_count": 0, "missing_count": 0}
    metrics_by_article = latest_metrics_by_article(base, [int(row["id"]) for row in rows])
    comments_by_article = elected_comments_by_article(base, [int(row["id"]) for row in rows])
    updated: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    by_account: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_account.setdefault(int(row.get("account_id") or 0), []).append(row)
    for _account_id, account_rows in by_account.items():
        account_name = str(account_rows[0].get("account_name") or "account")
        out_dir = account_output_dir(output_root, account_name)
        index = read_account_index(out_dir)
        for row in account_rows:
            article_id = int(row["id"])
            indexed = index.get(str(article_id)) or index.get(str(row.get("url") or ""))
            markdown_rel = str((indexed or {}).get("markdown_path") or "")
            markdown_path = Path(markdown_rel) if markdown_rel else Path()
            if markdown_rel and not markdown_path.is_absolute():
                markdown_path = out_dir / markdown_path
            if not markdown_rel or not markdown_path.exists():
                missing.append({"article_id": article_id, "title": row.get("title", ""), "code": "markdown_missing"})
                continue
            section = render_engagement_page_data_markdown(
                metrics_by_article.get(article_id),
                comments_by_article.get(article_id, []),
            )
            if upsert_markdown_section(markdown_path, section):
                updated.append({"article_id": article_id, "title": row.get("title", ""), "markdown_path": str(markdown_path)})
    if missing:
        log_evolution_event(
            base,
            "markdown_writeback",
            "markdown_missing",
            "warning",
            account_id=int(rows[0].get("account_id") or 0),
            detail={"missing_count": len(missing), "article_count": len(rows)},
        )
    return {
        "ok": not missing,
        "updated_count": len(updated),
        "missing_count": len(missing),
        "updated": updated,
        "missing": missing,
    }


def list_field_presets(base: Path) -> list[dict[str, Any]]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        rows = db.execute("SELECT * FROM field_presets ORDER BY name").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["visible_fields"] = load_json_text(item.pop("visible_fields_json")) or []
            result.append(item)
        return result
    finally:
        db.close()


def set_field_preset(base: Path, name: str, fields: list[str], default_format: str = "markdown") -> dict[str, Any]:
    init_exporter_db(base)
    fields = [field for field in fields if field in ALL_FIELDS]
    if not fields:
        raise ValueError("at least one known field is required")
    now = utc_now()
    db = connect_db(base)
    try:
        db.execute(
            """
            INSERT INTO field_presets (name, visible_fields_json, default_export_format, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                visible_fields_json = excluded.visible_fields_json,
                default_export_format = excluded.default_export_format,
                updated_at = excluded.updated_at
            """,
            (name, json_dumps(fields), default_format, now, now),
        )
        db.commit()
    finally:
        db.close()
    return {"ok": True, "name": name, "visible_fields": fields, "default_export_format": default_format}


def list_collections(base: Path, account_id: int = 0) -> list[dict[str, Any]]:
    init_exporter_db(base)
    where = "WHERE c.account_id = ?" if account_id else ""
    params: list[Any] = [account_id] if account_id else []
    db = connect_db(base)
    try:
        rows = db.execute(
            f"""
            SELECT c.*, t.nickname AS account_name
            FROM collections c
            JOIN target_accounts t ON t.id = c.account_id
            {where}
            ORDER BY c.updated_at DESC, c.title
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def add_collection(base: Path, account_id: int, title: str, article_ids: list[int], collection_url: str = "") -> dict[str, Any]:
    init_exporter_db(base)
    now = utc_now()
    db = connect_db(base)
    try:
        db.execute(
            """
            INSERT INTO collections (account_id, title, collection_url, article_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, title) DO UPDATE SET
                collection_url = excluded.collection_url,
                article_count = excluded.article_count,
                updated_at = excluded.updated_at
            """,
            (account_id, title, collection_url, len(article_ids), now, now),
        )
        collection_id = int(db.execute("SELECT id FROM collections WHERE account_id = ? AND title = ?", (account_id, title)).fetchone()["id"])
        db.execute("DELETE FROM collection_articles WHERE collection_id = ?", (collection_id,))
        for order, article_id in enumerate(article_ids, 1):
            db.execute(
                "INSERT OR REPLACE INTO collection_articles (collection_id, article_id, sort_order) VALUES (?, ?, ?)",
                (collection_id, article_id, order),
            )
            db.execute("UPDATE articles SET collection_title = ?, updated_at = ? WHERE id = ?", (title, now, article_id))
        db.commit()
    finally:
        db.close()
    return {"ok": True, "collection_id": collection_id, "title": title, "article_count": len(article_ids)}


def db_status(base: Path) -> dict[str, Any]:
    init_exporter_db(base)
    db_file = app_db_path(base)
    db = connect_db(base)
    try:
        counts = {}
        for table in ["login_profiles", "target_accounts", "articles", "collections", "article_comments", "sync_jobs", "download_runs"]:
            counts[table] = int(db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
        profile = get_active_profile(base)
        active = dict(profile) if profile else None
        if active:
            active.pop("id", None)
    finally:
        db.close()
    return {
        "ok": True,
        "db": str(db_file),
        "db_size_bytes": db_file.stat().st_size if db_file.exists() else 0,
        "counts": counts,
        "active_profile": active,
    }


def load_structured_rows(path_or_json: str) -> list[dict[str, Any]]:
    path = Path(path_or_json).expanduser()
    if path.exists():
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                return [dict(row) for row in csv.DictReader(fh)]
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = json.loads(path_or_json)
    if isinstance(data, dict):
        for key in ["items", "rows", "articles", "comments", "data"]:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def find_article_id_for_metric(db: sqlite3.Connection, row: dict[str, Any]) -> int | None:
    article_id = safe_int(row.get("article_id") or row.get("id"), 0)
    if article_id:
        found = db.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if found:
            return int(found["id"])
    url = str(row.get("url") or row.get("article_url") or "").strip()
    if url:
        try:
            url = clean_url(url)
        except ValueError:
            url = safe_display_url(url)
        found = db.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
        if found:
            return int(found["id"])
    return None


def import_metrics(base: Path, path_or_json: str) -> dict[str, Any]:
    init_exporter_db(base)
    rows = load_structured_rows(path_or_json)
    updated = 0
    missing = 0
    db = connect_db(base)
    try:
        for row in rows:
            article_id = find_article_id_for_metric(db, row)
            if not article_id:
                missing += 1
                continue
            db.execute(
                """
                UPDATE articles
                SET read_count = COALESCE(?, read_count),
                    like_count = COALESCE(?, like_count),
                    share_count = COALESCE(?, share_count),
                    favorite_count = COALESCE(?, favorite_count),
                    comment_count = COALESCE(?, comment_count),
                    comment_downloaded = CASE WHEN ? IS NOT NULL THEN 1 ELSE comment_downloaded END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    maybe_int(row.get("read_count") or row.get("read")),
                    maybe_int(row.get("like_count") or row.get("like")),
                    maybe_int(row.get("share_count") or row.get("share")),
                    maybe_int(row.get("favorite_count") or row.get("favorite")),
                    maybe_int(row.get("comment_count") or row.get("comment")),
                    maybe_int(row.get("comment_count") or row.get("comment")),
                    utc_now(),
                    article_id,
                ),
            )
            updated += 1
        db.commit()
    finally:
        db.close()
    return {"ok": True, "updated_count": updated, "missing_count": missing}


def import_comments(base: Path, path_or_json: str) -> dict[str, Any]:
    init_exporter_db(base)
    rows = load_structured_rows(path_or_json)
    inserted = 0
    missing = 0
    db = connect_db(base)
    try:
        for row in rows:
            article_id = find_article_id_for_metric(db, row)
            if not article_id:
                missing += 1
                continue
            upsert_comment_row(db, article_id, row)
            inserted += 1
        db.commit()
    finally:
        db.close()
    return {"ok": True, "inserted_count": inserted, "missing_count": missing}


def flush_captured_comments(base: Path) -> dict[str, Any]:
    capture_dir = base / "comments-capture"
    if not capture_dir.exists():
        return {"ok": True, "files_processed": 0, "inserted": 0, "missing_articles": 0, "errors": []}
    imported_dir = capture_dir / "imported"
    imported_dir.mkdir(parents=True, exist_ok=True)
    init_exporter_db(base)
    db = connect_db(base)
    files_processed = 0
    total_inserted = 0
    total_missing = 0
    files_skipped = 0
    errors: list[str] = []
    try:
        for path in sorted(capture_dir.glob("*.json")):
            appmsgid = path.stem
            try:
                comments = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if not isinstance(comments, list):
                continue
            row = db.execute("SELECT id FROM articles WHERE msgid = ?", (appmsgid,)).fetchone()
            if not row:
                total_missing += len(comments)
                files_skipped += 1
                continue
            article_id = int(row["id"])
            inserted = 0
            for c in comments:
                if not isinstance(c, dict):
                    continue
                try:
                    upsert_comment_row(db, article_id, c, source="passive_page")
                    inserted += 1
                except Exception as exc:
                    errors.append(f"{path.name} comment insert: {exc}")
            if inserted > 0:
                db.execute("UPDATE articles SET comment_downloaded = 1, updated_at = ? WHERE id = ?", (utc_now(), article_id))
            db.commit()
            total_inserted += inserted
            files_processed += 1
            shutil.move(str(path), str(imported_dir / path.name))
    finally:
        db.close()
    return {
        "ok": True,
        "files_processed": files_processed,
        "files_skipped": files_skipped,
        "inserted": total_inserted,
        "missing_articles": total_missing,
        "errors": errors,
    }


def ready_engagement_contexts(base: Path, account_id: int, limit: int) -> list[dict[str, Any]]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        rows = db.execute(
            """
            SELECT c.article_id, c.account_id, COALESCE(NULLIF(c.biz, ''), m.biz) AS biz, c.msgid, c.idx, c.comment_id, c.url
            FROM article_contexts c
            JOIN articles a ON a.id = c.article_id
            LEFT JOIN account_biz_mappings m ON m.account_id = c.account_id
            WHERE c.account_id = ?
              AND COALESCE(NULLIF(c.biz, ''), m.biz, '') <> ''
              AND c.msgid <> ''
              AND c.comment_id <> ''
            ORDER BY a.publish_time DESC, c.article_id DESC
            LIMIT ?
            """,
            (account_id, max(1, min(int(limit or 50), 100))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def selected_engagement_contexts(base: Path, account_id: int, article_ids: list[int]) -> list[dict[str, Any]]:
    init_exporter_db(base)
    if not article_ids:
        return []
    placeholders = ",".join("?" for _ in article_ids)
    params: list[Any] = [account_id, *article_ids]
    db = connect_db(base)
    try:
        rows = db.execute(
            f"""
            SELECT
                a.id AS article_id,
                a.account_id,
                COALESCE(NULLIF(c.biz, ''), m.biz, '') AS biz,
                a.msgid,
                a.idx,
                COALESCE(NULLIF(c.comment_id, ''), '') AS comment_id,
                a.url
            FROM articles a
            LEFT JOIN article_contexts c ON c.article_id = a.id
            LEFT JOIN account_biz_mappings m ON m.account_id = a.account_id
            WHERE a.account_id = ?
              AND a.id IN ({placeholders})
              AND COALESCE(NULLIF(c.biz, ''), m.biz, '') <> ''
              AND a.msgid <> ''
            """,
            params,
        ).fetchall()
        by_id = {int(row["article_id"]): dict(row) for row in rows}
        return [by_id[article_id] for article_id in article_ids if article_id in by_id]
    finally:
        db.close()


def active_collection_broker(base: Path) -> tuple[Path, str] | None:
    active_path = base / "context" / "active-proxy-session.json"
    if not active_path.exists():
        return None
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    session_id = str(active.get("session_id") or "").strip() if isinstance(active, dict) else ""
    if not session_id:
        return None
    capability_path = credential_capability_path(base, session_id)
    try:
        capability = capability_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return credential_socket_path(base, session_id), capability


def create_engagement_run(base: Path, account_id: int, limit: int = 50) -> dict[str, Any]:
    contexts = ready_engagement_contexts(base, account_id, limit)
    if not contexts:
        return {"ok": False, "status": "missing_context", "error": "no ready article context for this account", "article_count": 0}
    biz_values = {str(item["biz"]) for item in contexts if item.get("biz")}
    if len(biz_values) != 1:
        return {"ok": False, "status": "mapping_conflict", "error": "article contexts do not resolve to one __biz", "article_count": len(contexts)}
    biz = next(iter(biz_values))
    now = utc_now()
    run_id = "engagement-" + make_run_id()
    db = connect_db(base)
    try:
        db.execute(
            """
            INSERT INTO engagement_runs
                (run_id, account_id, scope_json, status, requested_count, created_at, updated_at)
            VALUES (?, ?, ?, 'waiting_credential', ?, ?, ?)
            """,
            (run_id, account_id, json_dumps({"biz": biz, "article_ids": [item["article_id"] for item in contexts]}), len(contexts), now, now),
        )
        db.commit()
    finally:
        db.close()
    return {"ok": True, "run_id": run_id, "biz": biz, "contexts": contexts}


def create_engagement_run_for_articles(base: Path, account_id: int, article_ids: list[int]) -> dict[str, Any]:
    selected_ids = [int(value) for value in article_ids if int(value or 0)]
    if not selected_ids:
        return {"ok": False, "status": "missing_context", "error": "no articles selected", "article_count": 0}
    contexts = selected_engagement_contexts(base, account_id, selected_ids)
    found_ids = {int(item["article_id"]) for item in contexts}
    missing_ids = [article_id for article_id in selected_ids if article_id not in found_ids]
    if missing_ids:
        return {
            "ok": False,
            "status": "missing_context",
            "error": "selected articles are missing engagement context; sync the account again",
            "article_count": len(contexts),
            "missing_article_ids": missing_ids,
        }
    biz_values = {str(item["biz"]) for item in contexts if item.get("biz")}
    if len(biz_values) != 1:
        return {"ok": False, "status": "mapping_conflict", "error": "article contexts do not resolve to one __biz", "article_count": len(contexts)}
    biz = next(iter(biz_values))
    now = utc_now()
    run_id = "engagement-" + make_run_id()
    db = connect_db(base)
    try:
        db.execute(
            """
            INSERT INTO engagement_runs
                (run_id, account_id, scope_json, status, requested_count, created_at, updated_at)
            VALUES (?, ?, ?, 'waiting_credential', ?, ?, ?)
            """,
            (run_id, account_id, json_dumps({"biz": biz, "article_ids": selected_ids}), len(contexts), now, now),
        )
        db.commit()
    finally:
        db.close()
    return {"ok": True, "run_id": run_id, "biz": biz, "contexts": contexts}


def contexts_for_engagement_run(base: Path, run_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    db = connect_db(base)
    try:
        row = db.execute("SELECT * FROM engagement_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None, []
        run = dict(row)
        scope = load_json_text(run.get("scope_json")) or {}
        ids = [int(value) for value in scope.get("article_ids", []) if str(value).isdigit()]
        if not ids:
            return run, []
        placeholders = ",".join("?" for _ in ids)
        rows = db.execute(
            f"""
            SELECT c.article_id, c.account_id, COALESCE(NULLIF(c.biz, ''), m.biz) AS biz, c.msgid, c.idx, c.comment_id, c.url
            FROM article_contexts c
            LEFT JOIN account_biz_mappings m ON m.account_id = c.account_id
            WHERE c.article_id IN ({placeholders})
              AND COALESCE(NULLIF(c.biz, ''), m.biz, '') <> ''
              AND c.msgid <> ''
              AND c.comment_id <> ''
            """,
            ids,
        ).fetchall()
        by_id = {int(item["article_id"]): dict(item) for item in rows}
        return run, [by_id[article_id] for article_id in ids if article_id in by_id]
    finally:
        db.close()


def persist_engagement_payload(db: sqlite3.Connection, run_id: str, payload: dict[str, Any]) -> tuple[int, int, list[str]]:
    successes = 0
    failures = 0
    errors: list[str] = []
    for item in payload.get("articles", []):
        article_id = int(item.get("article_id") or 0)
        if not item.get("ok") or not article_id:
            failures += 1
            error = str(item.get("error") or "engagement_request_failed")
            if error not in errors:
                errors.append(error)
            continue
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        captured_at = utc_now()
        db.execute(
            """
            INSERT INTO article_metrics
                (run_id, article_id, source, captured_at, read_count, like_count, old_like_count, share_count, comment_count, raw_json)
            VALUES (?, ?, 'wechat_session_api', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, article_id, source) DO UPDATE SET
                captured_at = excluded.captured_at,
                read_count = excluded.read_count,
                like_count = excluded.like_count,
                old_like_count = excluded.old_like_count,
                share_count = excluded.share_count,
                comment_count = excluded.comment_count,
                raw_json = excluded.raw_json
            """,
            (
                run_id,
                article_id,
                captured_at,
                maybe_int(metrics.get("read_count")),
                maybe_int(metrics.get("like_count")),
                maybe_int(metrics.get("old_like_count")),
                maybe_int(metrics.get("share_count")),
                maybe_int(metrics.get("comment_count")),
                json_dumps(metrics),
            ),
        )
        for comment in item.get("comments", []):
            if isinstance(comment, dict):
                upsert_comment_row(db, article_id, {**comment, "complete": 1 if item.get("comments_complete") else 0}, source="wechat_session_api")
        successes += 1
    return successes, failures, errors


def execute_engagement_run(base: Path, run_id: str) -> dict[str, Any]:
    """Resume one user-authorized run without exposing credentials outside the broker."""
    db = connect_db(base)
    try:
        claimed = db.execute(
            "UPDATE engagement_runs SET status = 'engagement_syncing', error = '', updated_at = ? WHERE run_id = ? AND status = 'waiting_credential'",
            (utc_now(), run_id),
        ).rowcount
        db.commit()
    finally:
        db.close()
    if not claimed:
        return {"ok": False, "run_id": run_id, "status": "not_waiting"}
    run, contexts = contexts_for_engagement_run(base, run_id)
    if not run or not contexts:
        db = connect_db(base)
        try:
            db.execute(
                "UPDATE engagement_runs SET status = 'completed_with_gaps', error = 'article context is no longer ready', updated_at = ? WHERE run_id = ?",
                (utc_now(), run_id),
            )
            db.commit()
        finally:
            db.close()
        return {"ok": False, "run_id": run_id, "status": "missing_context"}
    biz = str((load_json_text(run["scope_json"]) or {}).get("biz") or "")
    broker = active_collection_broker(base)
    if not broker:
        payload = {"ok": False, "status": "unavailable", "error": "credential broker is not running"}
    else:
        payload = None
    successes = 0
    failures = 0
    failure_errors: list[str] = []
    for offset in range(0, len(contexts), 10):
        if payload is not None:
            break
        payload = broker_request(
            broker[0],
            {"op": "fetch_engagement", "biz": biz, "articles": contexts[offset : offset + 10], "capability": broker[1]},
            timeout_seconds=180,
        )
        if payload.get("status") in {"waiting_credential", "unavailable"} or not payload.get("articles"):
            break
        db = connect_db(base)
        try:
            added_successes, added_failures, added_errors = persist_engagement_payload(db, run_id, payload)
            successes += added_successes
            failures += added_failures
            failure_errors.extend(error for error in added_errors if error not in failure_errors)
            db.commit()
        finally:
            db.close()
        payload = None
    if payload is not None:
        error = str(payload.get("error") or "valid credential is unavailable")
        log_evolution_event(
            base,
            "engagement_sync",
            str(payload.get("status") or "waiting_credential"),
            "warning",
            account_id=int(run.get("account_id") or 0) if run else 0,
            run_id=run_id,
            detail={"article_count": len(contexts), "error": error},
        )
        db = connect_db(base)
        try:
            db.execute("UPDATE engagement_runs SET status = 'waiting_credential', error = ?, updated_at = ? WHERE run_id = ?", (error, utc_now(), run_id))
            db.commit()
        finally:
            db.close()
        return {"ok": False, "run_id": run_id, "status": "waiting_credential", "article_count": len(contexts), "error": error}
    db = connect_db(base)
    try:
        status = "complete" if failures == 0 else "partial"
        db.execute(
            """
            UPDATE engagement_runs
            SET status = ?, success_count = ?, failed_count = ?, error = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (status, successes, failures, "; ".join(failure_errors), utc_now(), run_id),
        )
        db.commit()
    finally:
        db.close()
    return {
        "ok": failures == 0,
        "run_id": run_id,
        "status": "complete" if failures == 0 else "partial",
        "article_count": len(contexts),
        "success_count": successes,
        "failed_count": failures,
        "errors": failure_errors,
        "comment_scope": "elected",
    }


def sync_engagement(
    base: Path,
    account_id: int,
    limit: int = 50,
    auto_start_collection: bool = True,
    output_root: str = "",
) -> dict[str, Any]:
    created = create_engagement_run(base, account_id, limit)
    if not created.get("ok"):
        return created
    collection_session: dict[str, Any] | None = None
    if auto_start_collection and not active_collection_broker(base):
        collection_session = start_proxy_enhancer_session(base, upstream_proxy="none", yes=True)
        if not collection_session.get("ok"):
            log_evolution_event(
                base,
                "wechat_collection_start",
                str(collection_session.get("stage") or collection_session.get("error") or "start_failed"),
                "error",
                account_id=account_id,
                run_id=str(created["run_id"]),
                detail={"status": collection_session.get("mode") or "", "requires_confirmation": collection_session.get("requires_confirmation", False)},
            )
    result = execute_engagement_run(base, str(created["run_id"]))
    representative_url = str(created["contexts"][0].get("url") or "")
    if result.get("status") != "waiting_credential" or not representative_url:
        writeback = (
            write_engagement_to_markdown(base, [int(item["article_id"]) for item in created["contexts"]], output_root)
            if result.get("status") in {"complete", "partial"}
            else None
        )
        return {**result, "representative_url": representative_url, "collection_session": collection_session, "markdown_writeback": writeback}
    copied, clipboard_method = copy_to_clipboard(representative_url)
    return {
        **result,
        "representative_url": representative_url,
        "copied_to_clipboard": copied,
        "clipboard_method": clipboard_method if copied else "",
        "clipboard_error": "" if copied else clipboard_method,
        "collection_session": collection_session,
        "next_step": "已复制代表文章链接，请粘贴到微信客户端并打开；打开后继续恢复本任务。",
    }


def sync_engagement_for_articles(
    base: Path,
    account_id: int,
    article_ids: list[int],
    auto_start_collection: bool = True,
    output_root: str = "",
) -> dict[str, Any]:
    created = create_engagement_run_for_articles(base, account_id, article_ids)
    if not created.get("ok"):
        return created
    collection_session: dict[str, Any] | None = None
    if auto_start_collection and not active_collection_broker(base):
        collection_session = start_proxy_enhancer_session(base, upstream_proxy="none", yes=True)
        if not collection_session.get("ok"):
            log_evolution_event(
                base,
                "wechat_collection_start",
                str(collection_session.get("stage") or collection_session.get("error") or "start_failed"),
                "error",
                account_id=account_id,
                run_id=str(created["run_id"]),
                detail={"status": collection_session.get("mode") or "", "requires_confirmation": collection_session.get("requires_confirmation", False)},
            )
    result = execute_engagement_run(base, str(created["run_id"]))
    representative_url = str(created["contexts"][0].get("url") or "")
    selected_ids = [int(item["article_id"]) for item in created["contexts"]]
    if result.get("status") != "waiting_credential" or not representative_url:
        writeback = (
            write_engagement_to_markdown(base, selected_ids, output_root)
            if result.get("status") in {"complete", "partial"}
            else None
        )
        return {**result, "representative_url": representative_url, "collection_session": collection_session, "markdown_writeback": writeback}
    copied, clipboard_method = copy_to_clipboard(representative_url)
    return {
        **result,
        "representative_url": representative_url,
        "copied_to_clipboard": copied,
        "clipboard_method": clipboard_method if copied else "",
        "clipboard_error": "" if copied else clipboard_method,
        "collection_session": collection_session,
        "next_step": "已复制代表文章链接，请粘贴到微信客户端并打开；打开后继续恢复本任务。",
    }


def resume_waiting_engagement_runs(base: Path, biz: str = "", run_id: str = "", output_root: str = "") -> dict[str, Any]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        if run_id:
            rows = db.execute(
                "SELECT run_id, scope_json FROM engagement_runs WHERE status = 'waiting_credential' AND run_id = ?",
                (run_id,),
            ).fetchall()
        else:
            rows = db.execute("SELECT run_id, scope_json FROM engagement_runs WHERE status = 'waiting_credential' ORDER BY created_at").fetchall()
    finally:
        db.close()
    run_ids = [str(row["run_id"]) for row in rows if not biz or str((load_json_text(row["scope_json"]) or {}).get("biz") or "") == biz]
    results = [execute_engagement_run(base, run_id) for run_id in run_ids]
    article_ids: list[int] = []
    for row in rows:
        if str(row["run_id"]) not in set(run_ids):
            continue
        scope = load_json_text(str(row["scope_json"] or "{}")) or {}
        article_ids.extend(int(value) for value in scope.get("article_ids", []) if str(value).isdigit())
    terminal_statuses = {"complete", "partial"}
    writeback = (
        write_engagement_to_markdown(base, article_ids, output_root)
        if article_ids and all(str(result.get("status") or "") in terminal_statuses for result in results)
        else None
    )
    return {"ok": all(result.get("ok") for result in results), "run_count": len(results), "results": results, "markdown_writeback": writeback}


def create_dataset_manifest(base: Path, account_id: int, dataset_id: str = "", output_root: str = "") -> dict[str, Any]:
    """Publish a local-only dataset descriptor for downstream reading and analysis."""
    init_exporter_db(base)
    db = connect_db(base)
    try:
        account = db.execute("SELECT nickname FROM target_accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            return {"ok": False, "error": "account not found"}
        rows = db.execute(
            """
            SELECT a.id, a.title, a.url, a.publish_time, a.content_downloaded,
                   c.context_status, c.biz,
                   (SELECT captured_at FROM article_metrics m WHERE m.article_id = a.id ORDER BY m.captured_at DESC LIMIT 1) AS metrics_captured_at,
                   (SELECT source FROM article_metrics m WHERE m.article_id = a.id ORDER BY m.captured_at DESC LIMIT 1) AS metrics_source,
                   (SELECT COUNT(*) FROM article_comments ac WHERE ac.article_id = a.id AND ac.comment_scope = 'elected') AS elected_comment_count
            FROM articles a
            LEFT JOIN article_contexts c ON c.article_id = a.id
            WHERE a.account_id = ?
            ORDER BY a.publish_time DESC, a.id DESC
            """,
            (account_id,),
        ).fetchall()
    finally:
        db.close()
    account_name = str(account["nickname"] or "account")
    account_dir = account_output_dir(output_root, account_name)
    index = read_account_index(account_dir)
    dataset_id = safe_name(dataset_id or f"dataset-{make_run_id()}", 80)
    dataset_dir = account_dir / "datasets" / dataset_id
    dataset_dir.mkdir(parents=True, exist_ok=True)
    articles = []
    for row in rows:
        item = dict(row)
        indexed = index.get(str(item["id"])) or index.get(str(item["url"])) or {}
        markdown = str(indexed.get("markdown_path") or "")
        markdown_path = str((account_dir / markdown).resolve()) if markdown and not Path(markdown).is_absolute() else markdown
        articles.append(
            {
                "article_id": int(item["id"]),
                "title": str(item["title"] or ""),
                "url": str(item["url"] or ""),
                "publish_time": str(item["publish_time"] or ""),
                "markdown_path": markdown_path,
                "content_status": "ready" if item["content_downloaded"] and markdown_path else "missing",
                "context_status": str(item["context_status"] or "missing"),
                "engagement": {
                    "source": str(item["metrics_source"] or "missing"),
                    "captured_at": str(item["metrics_captured_at"] or ""),
                    "comment_scope": "elected",
                    "elected_comment_count": int(item["elected_comment_count"] or 0),
                },
            }
        )
    manifest = {
        "version": 1,
        "dataset_id": dataset_id,
        "account_id": account_id,
        "account_name": account_name,
        "created_at": utc_now(),
        "article_count": len(articles),
        "articles": articles,
    }
    manifest_path = dataset_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    csv_path = dataset_dir / "manifest.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["article_id", "title", "url", "publish_time", "markdown_path", "content_status", "context_status", "metrics_source", "metrics_captured_at", "elected_comment_count"])
        writer.writeheader()
        for item in articles:
            writer.writerow({
                "article_id": item["article_id"], "title": item["title"], "url": item["url"], "publish_time": item["publish_time"],
                "markdown_path": item["markdown_path"], "content_status": item["content_status"], "context_status": item["context_status"],
                "metrics_source": item["engagement"]["source"], "metrics_captured_at": item["engagement"]["captured_at"],
                "elected_comment_count": item["engagement"]["elected_comment_count"],
            })
    return {"ok": True, "dataset_id": dataset_id, "manifest": str(manifest_path), "csv": str(csv_path), "article_count": len(articles)}


def list_comments(base: Path, article_id: int, limit: int = 100) -> list[dict[str, Any]]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        rows = db.execute(
            """
            SELECT id, article_id, comment_id, nick_name, content, like_count, create_time
                   , comment_scope, source, fetched_at, complete
            FROM article_comments
            WHERE article_id = ?
            ORDER BY create_time DESC, id DESC
            LIMIT ?
            """,
            (article_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def auth_check(base: Path, profile: str = "") -> dict[str, Any]:
    try:
        payload = api_request(base, "/api/public/v1/authkey", {}, profile)
        code = payload.get("code") if isinstance(payload, dict) else None
        ok = str(code) in {"0", "None"} or code is None
        status = "valid" if ok else "expired"
        active, _auth_key = get_auth_key(base, profile)
        db = connect_db(base)
        try:
            db.execute("UPDATE login_profiles SET status = ?, updated_at = ? WHERE id = ?", (status, utc_now(), int(active["id"])))
            db.commit()
        finally:
            db.close()
        return {"ok": ok, "status": status, "code": code, "profile": active["display_name"], "expires_at": active["expires_at"]}
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc)}


def html_escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def render_page(base: Path, selected_account_id: int = 0) -> str:
    status = db_status(base)
    accounts = list_accounts(base)
    account_ids = {int(account["id"]) for account in accounts}
    selected_account = selected_account_id if selected_account_id in account_ids else (accounts[0]["id"] if accounts else 0)
    articles = list_articles(base, selected_account, 100) if selected_account else []
    collections = list_collections(base, selected_account) if selected_account else []
    presets = list_field_presets(base)
    active = status.get("active_profile") or {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Moore Exporter</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #172033; background: #f6f7f9; }}
    header {{ height: 64px; display: flex; align-items: center; justify-content: space-between; padding: 0 24px; background: #fff; border-bottom: 1px solid #dce1e8; }}
    main {{ display: grid; grid-template-columns: 360px 1fr; min-height: calc(100vh - 65px); }}
    aside {{ background: #fff; border-right: 1px solid #dce1e8; padding: 18px; overflow: auto; }}
    section {{ padding: 18px 22px; overflow: auto; }}
    h1 {{ font-size: 24px; margin: 0; }}
    h2 {{ font-size: 18px; margin: 22px 0 10px; }}
    input, select {{ height: 36px; border: 1px solid #cfd6df; border-radius: 6px; padding: 0 10px; font-size: 14px; background: #fff; }}
    button, .button {{ height: 36px; border: 0; border-radius: 6px; padding: 0 12px; background: #2563eb; color: #fff; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; }}
    button.secondary {{ background: #eef2f7; color: #172033; }}
    .row {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .status {{ color: #667085; font-size: 14px; }}
    .account {{ display: grid; grid-template-columns: 42px 1fr; gap: 10px; padding: 10px; border: 1px solid #e3e7ee; border-radius: 8px; margin-bottom: 8px; background: #fbfcfe; color: inherit; text-decoration: none; }}
    .avatar {{ width: 42px; height: 42px; border-radius: 50%; background: #d9dee8; object-fit: cover; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dce1e8; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e7ebf0; text-align: left; font-size: 14px; vertical-align: top; }}
    th {{ background: #f9fafb; white-space: nowrap; }}
    .small {{ font-size: 12px; color: #667085; }}
    .field-grid {{ display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)); gap: 8px; background: #fff; border: 1px solid #dce1e8; padding: 12px; }}
    label {{ display: flex; align-items: center; gap: 6px; }}
    .warn {{ color: #d92d20; }}
    @media (max-width: 900px) {{ main {{ grid-template-columns: 1fr; }} aside {{ border-right: 0; border-bottom: 1px solid #dce1e8; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Exporter 模式</h1>
    <div class="status">登录：{html_escape(active.get("display_name", "未配置"))} ｜ 过期：{html_escape(active.get("expires_at", "")) or "未知"} ｜ DB：{status["db_size_bytes"]} bytes</div>
  </header>
  <main>
    <aside>
      <h2>登录</h2>
      <p><a class="button" href="/login/start">本地扫码登录</a></p>
      <form method="post" action="/config">
        <div class="row"><input name="base_url" value="{html_escape(get_config(base, "base_url", DEFAULT_BASE_URL))}" style="width: 230px"><button>保存</button></div>
        <p class="small">先打开 exporter 网站扫码登录，再把 API 页 auth-key 粘贴到这里。本地会优先存入 macOS Keychain。</p>
        <div class="row"><input name="auth_key" placeholder="auth-key" style="width: 230px"><button>配置 auth-key</button></div>
      </form>
      <p><a class="button secondary" href="{html_escape(get_config(base, "base_url", DEFAULT_BASE_URL))}" target="_blank">打开扫码登录页</a></p>

      <h2>搜索公众号</h2>
      <form method="get" action="/search" class="row">
        <input name="keyword" placeholder="输入关键词" style="width: 230px"><button>搜索</button>
      </form>

      <h2>已添加公众号</h2>
      {''.join(render_account_card(account, selected_account) for account in accounts) or '<p class="small">暂无公众号</p>'}
    </aside>
    <section>
      <div class="row">
        {render_account_switcher(accounts, selected_account)}
        <form method="post" action="/sync"><input name="account_id" value="{selected_account}" hidden><button>同步当前公众号</button></form>
        <form method="post" action="/download"><input name="account_id" value="{selected_account}" hidden><input name="latest" value="20" style="width: 60px"><button>下载最新 N 篇</button></form>
      </div>

      <h2>文章列表</h2>
      {render_articles_table(articles)}

      <h2>合集</h2>
      {render_collections_table(collections)}

      <h2>字段配置</h2>
      {render_fields(presets[0]["visible_fields"] if presets else DEFAULT_VISIBLE_FIELDS)}
    </section>
  </main>
</body>
</html>"""


def render_account_switcher(accounts: list[dict[str, Any]], selected_account: int) -> str:
    if not accounts:
        return ""
    options = []
    for account in accounts:
        selected = "selected" if int(account["id"]) == int(selected_account) else ""
        label = f"{account.get('nickname') or account.get('fakeid')} ({account.get('synced_count') or 0}篇)"
        options.append(f"<option value='{int(account['id'])}' {selected}>{html_escape(label)}</option>")
    return (
        "<form method='get' action='/' class='row'>"
        "<select name='account_id' onchange='this.form.submit()'>"
        + "".join(options)
        + "</select><noscript><button>切换</button></noscript></form>"
    )


def render_account_card(account: dict[str, Any], selected_account: int = 0) -> str:
    avatar = account.get("avatar_url") or ""
    img = f'<img class="avatar" src="{html_escape(avatar)}">' if avatar else '<div class="avatar"></div>'
    progress = "0%"
    if int(account.get("article_count") or 0) > 0:
        progress = f"{int(account.get('synced_count') or 0) * 100 // int(account.get('article_count') or 1)}%"
    selected_style = " style='border-color:#2563eb;background:#eff6ff'" if int(account.get("id") or 0) == int(selected_account) else ""
    return f"""
<a class="account" href="/?account_id={int(account.get("id") or 0)}"{selected_style}>
  {img}
  <div>
    <strong>{html_escape(account.get("nickname"))}</strong>
    <div class="small">微信号：{html_escape(account.get("alias") or "未设置")}</div>
    <div class="small">已同步：{html_escape(account.get("synced_count"))} ｜ 进度：{progress}</div>
  </div>
</a>"""


def render_articles_table(articles: list[dict[str, Any]]) -> str:
    if not articles:
        return '<p class="small">暂无文章</p>'
    rows = []
    for item in articles:
        rows.append(
            "<tr>"
            f"<td><input form='download-selected' type='checkbox' name='article_ids' value='{int(item['id'])}'></td>"
            f"<td>{int(item['id'])}</td>"
            f"<td>{html_escape(item['title'])}<div class='small'>{html_escape(item['digest'])}</div></td>"
            f"<td>{html_escape(item['publish_time'])}</td>"
            f"<td>{'是' if item['content_downloaded'] else '否'}</td>"
            f"<td><a href='{html_escape(item['url'])}' target='_blank'>原文</a></td>"
            "</tr>"
        )
    return (
        "<form id='download-selected' method='post' action='/download-selected'></form>"
        "<table><thead><tr><th></th><th>ID</th><th>标题</th><th>发布时间</th><th>内容已下载</th><th>操作</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table><p><button form='download-selected'>下载选中</button></p>"
    )


def render_collections_table(collections: list[dict[str, Any]]) -> str:
    if not collections:
        return '<p class="small">暂无合集。同步文章后，如 API 返回 collection/album 字段会自动归集；也可用 CLI 手动创建。</p>'
    rows = []
    for item in collections:
        rows.append(
            "<tr>"
            f"<td>{int(item['id'])}</td>"
            f"<td>{html_escape(item['title'])}</td>"
            f"<td>{html_escape(item['account_name'])}</td>"
            f"<td>{html_escape(item['article_count'])}</td>"
            f"<td><form method='post' action='/download-collection'><input name='collection_id' value='{int(item['id'])}' hidden><button>下载合集</button></form></td>"
            "</tr>"
        )
    return "<table><thead><tr><th>ID</th><th>合集</th><th>公众号</th><th>文章数</th><th>操作</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def render_fields(visible: list[str]) -> str:
    checks = []
    for field in ALL_FIELDS:
        checked = "checked" if field in visible else ""
        checks.append(f"<label><input type='checkbox' name='fields' value='{field}' {checked}>{html_escape(field)}</label>")
    return "<form method='post' action='/fields'><div class='field-grid'>" + "".join(checks) + "</div><p><button>保存字段</button></p></form>"


def render_login_page(base: Path, login_id: str) -> str:
    load_qr_login_session(base, login_id)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>扫码登录</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #172033; }}
    main {{ width: min(420px, calc(100vw - 32px)); background: #fff; border: 1px solid #dce1e8; border-radius: 8px; padding: 24px; }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    img {{ width: 320px; max-width: 100%; display: block; margin: 16px auto; border-radius: 8px; }}
    button, a.button {{ height: 36px; border: 0; border-radius: 6px; padding: 0 12px; background: #2563eb; color: #fff; font-weight: 600; display: inline-flex; align-items: center; text-decoration: none; cursor: pointer; }}
    .secondary {{ background: #eef2f7 !important; color: #172033 !important; }}
    .status {{ color: #667085; margin: 10px 0 16px; }}
  </style>
</head>
<body>
  <main>
    <h1>扫码登录微信公众号</h1>
    <p class="status" id="status">等待扫码...</p>
    <img src="/login/qrcode?login_id={html_escape(login_id)}" alt="login qrcode">
    <form method="post" action="/login/complete" id="complete-form">
      <input type="hidden" name="login_id" value="{html_escape(login_id)}">
      <button type="submit">我已确认，完成登录</button>
      <a class="button secondary" href="/">返回</a>
    </form>
  </main>
  <script>
    const loginId = {json.dumps(login_id)};
    const statusEl = document.getElementById('status');
    const form = document.getElementById('complete-form');
    let completed = false;
    async function poll() {{
      if (completed) return;
      try {{
        const resp = await fetch('/login/status?login_id=' + encodeURIComponent(loginId));
        const data = await resp.json();
        const text = {{
          waiting_for_scan: '等待扫码...',
          scanned_waiting_confirm: '已扫码，等待手机确认...',
          confirmed: '已确认，正在完成登录...',
          expired: '二维码已过期，请返回重新开始。',
          account_not_bound_email: '该账号尚未绑定邮箱。'
        }}[data.status] || (data.error || data.status || '未知状态');
        statusEl.textContent = text;
        if (data.ready_to_complete) {{
          completed = true;
          form.submit();
          return;
        }}
      }} catch (err) {{
        statusEl.textContent = '状态检查失败：' + err;
      }}
      setTimeout(poll, 2000);
    }}
    setTimeout(poll, 1200);
  </script>
</body>
</html>"""


def parse_form(body: bytes) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)


class ExporterHandler(BaseHTTPRequestHandler):
    server_version = "MooreExporter/1.0"

    @property
    def runtime_base(self) -> Path:
        return self.server.runtime_base  # type: ignore[attr-defined]

    def send_html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, content_type: str = "application/octet-stream", status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            account_id = safe_int((qs.get("account_id") or ["0"])[0])
            self.send_html(render_page(self.runtime_base, account_id))
            return
        if parsed.path == "/login/start":
            try:
                base_url = (qs.get("base_url") or [get_config(self.runtime_base, "base_url", DEFAULT_BASE_URL)])[0]
                result = start_qr_login(self.runtime_base, base_url)
                self.redirect(f"/login?login_id={urllib.parse.quote(result['login_id'])}")
            except Exception as exc:
                self.send_html(f"<p>启动扫码登录失败：{html_escape(exc)}</p><p><a href='/'>返回</a></p>", 500)
            return
        if parsed.path == "/login":
            login_id = (qs.get("login_id") or [""])[0]
            self.send_html(render_login_page(self.runtime_base, login_id))
            return
        if parsed.path == "/login/qrcode":
            login_id = (qs.get("login_id") or [""])[0]
            try:
                session = load_qr_login_session(self.runtime_base, login_id)
                qrcode_path = Path(str(session["qrcode_path"]))
                self.send_bytes(qrcode_path.read_bytes(), str(session.get("qrcode_content_type") or "image/jpeg"))
            except Exception as exc:
                self.send_html(f"二维码不存在：{html_escape(exc)}", 404)
            return
        if parsed.path == "/login/status":
            login_id = (qs.get("login_id") or [""])[0]
            try:
                self.send_json(qr_login_status(self.runtime_base, login_id))
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if parsed.path == "/search":
            keyword = (qs.get("keyword") or [""])[0]
            try:
                result = search_accounts(self.runtime_base, keyword)
                self.send_html(render_search_page(keyword, result.get("accounts", [])))
            except Exception as exc:
                self.send_html(f"<p>搜索失败：{html_escape(exc)}</p><p><a href='/'>返回</a></p>", 500)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        form = parse_form(self.rfile.read(length))
        try:
            if self.path == "/config":
                base_url = normalize_base_url((form.get("base_url") or [DEFAULT_BASE_URL])[0])
                auth_key = (form.get("auth_key") or [""])[0].strip()
                set_config(self.runtime_base, "base_url", base_url)
                if auth_key:
                    expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=4)).isoformat()
                    upsert_login_profile(self.runtime_base, base_url, auth_key, "default", expires_at, False)
                self.redirect("/")
                return
            if self.path == "/login/complete":
                login_id = (form.get("login_id") or [""])[0]
                complete_qr_login(self.runtime_base, login_id)
                self.redirect("/")
                return
            if self.path == "/add-account":
                account = {
                    "fakeid": (form.get("fakeid") or [""])[0],
                    "nickname": (form.get("nickname") or [""])[0],
                    "alias": (form.get("alias") or [""])[0],
                    "avatar_url": (form.get("avatar_url") or [""])[0],
                    "description": (form.get("description") or [""])[0],
                    "raw_json": (form.get("raw_json") or ["{}"])[0],
                }
                upsert_account(self.runtime_base, account)
                self.redirect("/")
                return
            if self.path == "/sync":
                sync_account_articles(self.runtime_base, int((form.get("account_id") or ["0"])[0]), 200)
                self.redirect("/")
                return
            if self.path == "/download":
                account_id = int((form.get("account_id") or ["0"])[0])
                latest = int((form.get("latest") or ["20"])[0])
                ids = select_article_ids(self.runtime_base, account_id=account_id, latest=latest)
                download_articles(self.runtime_base, ids)
                self.redirect("/")
                return
            if self.path == "/download-selected":
                ids = [int(item) for item in form.get("article_ids", []) if item]
                download_articles(self.runtime_base, ids)
                self.redirect("/")
                return
            if self.path == "/download-collection":
                collection_id = int((form.get("collection_id") or ["0"])[0])
                ids = select_article_ids(self.runtime_base, collection_id=collection_id)
                download_articles(self.runtime_base, ids)
                self.redirect("/")
                return
            if self.path == "/fields":
                fields = form.get("fields", [])
                set_field_preset(self.runtime_base, "default", fields)
                self.redirect("/")
                return
        except Exception as exc:
            self.send_html(f"<p>操作失败：{html_escape(exc)}</p><p><a href='/'>返回</a></p>", 500)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def render_search_page(keyword: str, accounts: list[dict[str, Any]]) -> str:
    rows = []
    for item in accounts:
        raw = html_escape(item.get("raw_json") or json_dumps(item))
        rows.append(
            "<tr>"
            f"<td>{html_escape(item.get('nickname'))}<div class='small'>{html_escape(item.get('description'))}</div></td>"
            f"<td>{html_escape(item.get('alias') or '未设置')}</td>"
            f"<td>{html_escape(item.get('fakeid'))}</td>"
            "<td><form method='post' action='/add-account'>"
            f"<input name='fakeid' value='{html_escape(item.get('fakeid'))}' hidden>"
            f"<input name='nickname' value='{html_escape(item.get('nickname'))}' hidden>"
            f"<input name='alias' value='{html_escape(item.get('alias'))}' hidden>"
            f"<input name='avatar_url' value='{html_escape(item.get('avatar_url'))}' hidden>"
            f"<input name='description' value='{html_escape(item.get('description'))}' hidden>"
            f"<input name='raw_json' value='{raw}' hidden>"
            "<button>添加</button></form></td>"
            "</tr>"
        )
    table = "<table><thead><tr><th>名称</th><th>微信号</th><th>fakeid</th><th>操作</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    return f"<!doctype html><meta charset='utf-8'><title>搜索公众号</title><style>body{{font-family:-apple-system,sans-serif;padding:24px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:10px;text-align:left}}button{{padding:8px 12px}}</style><h1>搜索：{html_escape(keyword)}</h1><p><a href='/'>返回</a></p>{table if rows else '<p>没有结果</p>'}"


def serve_dashboard(base: Path, host: str, port: int, open_browser: bool) -> dict[str, Any]:
    init_exporter_db(base)
    server = ThreadingHTTPServer((host, port), ExporterHandler)
    server.runtime_base = base  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        print(json.dumps({"ok": True, "url": url, "db": str(app_db_path(base)), "message": "Press Ctrl-C to stop."}, ensure_ascii=False, indent=2), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"ok": True, "stopped": True}


def command_init(args: argparse.Namespace) -> int:
    write_json_response(init_exporter_db(runtime_dir(args.runtime_dir)))
    return 0


def command_server(args: argparse.Namespace) -> int:
    serve_dashboard(runtime_dir(args.runtime_dir), args.host, args.port, not args.no_open)
    return 0


def command_login_start(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    init_exporter_db(base)
    base_url = normalize_base_url(args.base_url or get_config(base, "base_url", DEFAULT_BASE_URL))
    set_config(base, "base_url", base_url)
    if args.open:
        webbrowser.open(base_url)
    write_json_response(
        {
            "ok": True,
            "base_url": base_url,
            "opened": bool(args.open),
            "login_flow": [
                "在打开的 exporter 页面扫码登录。",
                "必须选择公众号或服务号，不要选小程序。",
                "登录成功后进入 API 页面复制 auth-key。",
                "再运行 exporter-config --auth-key '<auth-key>' 保存到本地 SQLite/Keychain。",
            ],
        }
    )
    return 0


def command_login_qr_start(args: argparse.Namespace) -> int:
    result = start_qr_login(runtime_dir(args.runtime_dir), args.base_url)
    if args.open:
        qrcode_path = Path(str(result["qrcode_path"]))
        if sys.platform == "darwin":
            subprocess.run(["open", str(qrcode_path)], check=False)
        else:
            webbrowser.open(qrcode_path.as_uri())
    write_json_response(result)
    return 0


def command_login_qr_status(args: argparse.Namespace) -> int:
    result = qr_login_status(runtime_dir(args.runtime_dir), args.login_id)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_login_qr_complete(args: argparse.Namespace) -> int:
    result = complete_qr_login(runtime_dir(args.runtime_dir), args.login_id, args.profile)
    write_json_response(result)
    return 0


def command_config(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    base_url = normalize_base_url(args.base_url or get_config(base, "base_url", DEFAULT_BASE_URL))
    auth_key = args.auth_key.strip()
    if not auth_key:
        raise SystemExit("auth-key is required")
    expires_at = args.expires_at or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=4)).isoformat()
    result = upsert_login_profile(base, base_url, auth_key, args.profile, expires_at, args.allow_plain_auth_key)
    write_json_response(result)
    return 0


def command_auth_check(args: argparse.Namespace) -> int:
    result = auth_check(runtime_dir(args.runtime_dir), args.profile)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_db_status(args: argparse.Namespace) -> int:
    write_json_response(db_status(runtime_dir(args.runtime_dir)))
    return 0


def command_logout(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    profile = get_active_profile(base, args.profile)
    if not profile:
        write_json_response({"ok": True, "logged_out": False})
        return 0
    db = connect_db(base)
    try:
        creds = db.execute("SELECT * FROM credential_store WHERE profile_id = ?", (int(profile["id"]),)).fetchall()
        for cred in creds:
            account = str(cred["keychain_account"] or "")
            if account and keychain_available():
                subprocess.run(["security", "delete-generic-password", "-s", "moore-wechat-article-downloader", "-a", account], text=True, capture_output=True)
        db.execute("DELETE FROM credential_store WHERE profile_id = ?", (int(profile["id"]),))
        db.execute("UPDATE login_profiles SET status = 'logged_out', updated_at = ? WHERE id = ?", (utc_now(), int(profile["id"])))
        db.commit()
    finally:
        db.close()
    write_json_response({"ok": True, "logged_out": True, "profile": profile["display_name"]})
    return 0


def command_search(args: argparse.Namespace) -> int:
    write_json_response(search_accounts(runtime_dir(args.runtime_dir), args.keyword, args.begin, args.size, args.profile))
    return 0


def command_account_by_url(args: argparse.Namespace) -> int:
    write_json_response(account_by_url(runtime_dir(args.runtime_dir), args.url, args.profile))
    return 0


def command_add(args: argparse.Namespace) -> int:
    if args.from_json:
        data = json.loads(Path(args.from_json).expanduser().read_text(encoding="utf-8")) if Path(args.from_json).expanduser().exists() else json.loads(args.from_json)
        account = normalize_account(data)
    else:
        account = {
            "fakeid": args.fakeid,
            "nickname": args.nickname or args.fakeid,
            "alias": args.alias,
            "avatar_url": args.avatar_url,
            "description": args.description,
            "raw_json": json_dumps({"fakeid": args.fakeid, "nickname": args.nickname, "alias": args.alias}),
        }
    write_json_response(upsert_account(runtime_dir(args.runtime_dir), account))
    return 0


def command_accounts(args: argparse.Namespace) -> int:
    write_json_response({"ok": True, "accounts": list_accounts(runtime_dir(args.runtime_dir))})
    return 0


def command_sync(args: argparse.Namespace) -> int:
    write_json_response(sync_account_articles(runtime_dir(args.runtime_dir), args.account_id, args.limit, args.keyword, args.profile))
    return 0


def sync_all_accounts(base: Path, per_account_limit: int = 50) -> dict[str, Any]:
    accounts = list_accounts(base)
    if not accounts:
        return {"ok": True, "accounts_synced": 0, "results": []}
    results = []
    for account in accounts:
        result = sync_account_articles(base, int(account["id"]), per_account_limit)
        results.append({
            "account_id": account["id"],
            "nickname": account["nickname"],
            "ok": result.get("ok", False),
            "inserted": result.get("inserted", 0),
            "error": result.get("error", ""),
        })
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "ok": ok_count == len(results),
        "accounts_synced": ok_count,
        "accounts_failed": len(results) - ok_count,
        "results": results,
    }


def download_new_articles(base: Path, output_dir: str = "", no_assets: bool = False) -> dict[str, Any]:
    rows = list_articles(base, account_id=0, limit=5000, downloaded="no")
    if not rows:
        return {"ok": True, "message": "no new articles to download", "success_count": 0, "failure_count": 0}
    ids = [int(row["id"]) for row in rows]
    return download_articles(base, ids, output_dir, no_assets)


def daily_run(base: Path, per_account_limit: int = 50, output_dir: str = "", no_assets: bool = False) -> dict[str, Any]:
    sync_result = sync_all_accounts(base, per_account_limit)
    download_result = download_new_articles(base, output_dir, no_assets)
    return {
        "ok": sync_result["ok"] and download_result.get("ok", True),
        "sync": sync_result,
        "download": download_result,
    }


def command_sync_all(args: argparse.Namespace) -> int:
    write_json_response(sync_all_accounts(runtime_dir(args.runtime_dir), args.per_account_limit))
    return 0


def command_download_new(args: argparse.Namespace) -> int:
    result = download_new_articles(runtime_dir(args.runtime_dir), args.output_dir, args.no_assets)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_daily_run(args: argparse.Namespace) -> int:
    result = daily_run(runtime_dir(args.runtime_dir), args.per_account_limit, args.output_dir, args.no_assets)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_articles(args: argparse.Namespace) -> int:
    rows = list_articles(runtime_dir(args.runtime_dir), args.account_id, args.limit, args.keyword, args.collection_id, args.downloaded)
    legacy_engagement_fields = {
        "comment_downloaded",
        "read_count",
        "like_count",
        "share_count",
        "favorite_count",
        "comment_count",
    }
    rows = [{key: value for key, value in row.items() if key not in legacy_engagement_fields} for row in rows]
    write_json_response({"ok": True, "count": len(rows), "articles": rows})
    return 0


def command_preview(args: argparse.Namespace) -> int:
    write_json_response(preview_article(runtime_dir(args.runtime_dir), args.article_id))
    return 0


def command_open_original(args: argparse.Namespace) -> int:
    write_json_response(open_original(runtime_dir(args.runtime_dir), args.article_id))
    return 0


def command_download(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    ids = select_article_ids(
        base,
        account_id=args.account_id,
        article_ids=args.article_ids,
        latest=args.latest,
        titles=args.titles,
        keyword=args.keyword,
        collection_id=args.collection_id,
    )
    result = download_articles(base, ids, args.output_dir, args.no_assets, force=args.force)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_fields(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    if args.set:
        fields = [item.strip() for item in re.split(r"[,，\s]+", args.set) if item.strip()]
        result = set_field_preset(base, args.name, fields, args.default_format)
    else:
        result = {"ok": True, "all_fields": ALL_FIELDS, "presets": list_field_presets(base)}
    write_json_response(result)
    return 0


def command_collections(args: argparse.Namespace) -> int:
    write_json_response({"ok": True, "collections": list_collections(runtime_dir(args.runtime_dir), args.account_id)})
    return 0


def command_collection_add(args: argparse.Namespace) -> int:
    ids = [int(part) for part in re.split(r"[,，\s]+", args.article_ids.strip()) if part.strip()]
    write_json_response(add_collection(runtime_dir(args.runtime_dir), args.account_id, args.title, ids, args.collection_url))
    return 0


def command_download_collection(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    ids = select_article_ids(base, collection_id=args.collection_id)
    result = download_articles(base, ids, args.output_dir, args.no_assets, force=args.force)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_metrics_import(args: argparse.Namespace) -> int:
    result = import_metrics(runtime_dir(args.runtime_dir), args.input)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_comments_import(args: argparse.Namespace) -> int:
    result = import_comments(runtime_dir(args.runtime_dir), args.input)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def command_comments(args: argparse.Namespace) -> int:
    rows = list_comments(runtime_dir(args.runtime_dir), args.article_id, args.limit)
    write_json_response({"ok": True, "count": len(rows), "comments": rows})
    return 0


def command_article_context(args: argparse.Namespace) -> int:
    html_text = ""
    if args.html_file:
        html_text = Path(args.html_file).expanduser().read_text(encoding="utf-8")
    result = resolve_article_context(
        runtime_dir(args.runtime_dir), args.article_id, html_text, args.biz, args.source, args.comment_id
    )
    write_json_response(scrub_payload(result))
    return 0 if result.get("ok") else 1


def command_wechat_collection_sync_engagement(args: argparse.Namespace) -> int:
    result = sync_engagement(runtime_dir(args.runtime_dir), args.account_id, args.limit, output_root=args.output_dir)
    write_json_response(scrub_payload(result))
    return 0 if result.get("ok") else 1


def command_wechat_collection_resume_engagement(args: argparse.Namespace) -> int:
    result = resume_waiting_engagement_runs(runtime_dir(args.runtime_dir), args.biz, args.run_id, args.output_dir)
    write_json_response(scrub_payload(result))
    return 0 if result.get("ok") else 1


def command_wechat_collection_writeback(args: argparse.Namespace) -> int:
    ids = [int(part) for part in re.split(r"[,，\s]+", args.article_ids.strip()) if part.strip()]
    result = write_engagement_to_markdown(runtime_dir(args.runtime_dir), ids, args.output_dir)
    write_json_response(scrub_payload(result))
    return 0 if result.get("ok") else 1


def command_wechat_collection_diagnostics(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    result = {"ok": True, "events": list_evolution_events(base, args.limit)}
    if args.export_fixture:
        result["fixture"] = export_evolution_fixture(base, args.export_fixture, args.limit)
    write_json_response(scrub_payload(result))
    return 0


def command_library_dataset(args: argparse.Namespace) -> int:
    result = create_dataset_manifest(runtime_dir(args.runtime_dir), args.account_id, args.dataset_id, args.output_dir)
    write_json_response(scrub_payload(result))
    return 0 if result.get("ok") else 1


def normalize_match_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def local_account_candidates(base: Path, target: str, limit: int = 5) -> list[dict[str, Any]]:
    needle = normalize_match_text(target)
    rows = list_accounts(base)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        nickname = normalize_match_text(row.get("nickname"))
        alias = normalize_match_text(row.get("alias"))
        fakeid = normalize_match_text(row.get("fakeid"))
        score = 0
        reason = ""
        if needle and needle in {nickname, alias, fakeid}:
            score = 100
            reason = "exact"
        elif needle and (needle in nickname or needle in alias or needle in fakeid):
            score = 80
            reason = "contains"
        elif needle and ((nickname and nickname in needle) or (alias and alias in needle)):
            score = 70
            reason = "fuzzy"
        if score:
            item = dict(row)
            item["source"] = "local"
            item["score"] = score
            item["reason"] = reason
            candidates.append(item)
    candidates.sort(key=lambda item: (-int(item["score"]), str(item.get("nickname") or "")))
    return candidates[:limit]


def wizard_session_id() -> str:
    return "wiz_" + uuid.uuid4().hex[:16]


def save_wizard_session(
    base: Path,
    session_id: str,
    target: str,
    state: str,
    request: dict[str, Any],
    candidates: list[dict[str, Any]] | None = None,
    selected_account_id: int = 0,
    selected_article_ids: list[int] | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    init_exporter_db(base)
    safe_target = sanitize_text_urls(target)
    safe_request = scrub_payload(request)
    safe_candidates = scrub_payload(candidates or [])
    safe_result = scrub_payload(result or {})
    now = utc_now()
    db = connect_db(base)
    try:
        db.execute(
            """
            INSERT INTO wizard_sessions
                (id, target, state, request_json, candidates_json, selected_account_id,
                 selected_article_ids_json, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target = excluded.target,
                state = excluded.state,
                request_json = excluded.request_json,
                candidates_json = excluded.candidates_json,
                selected_account_id = excluded.selected_account_id,
                selected_article_ids_json = excluded.selected_article_ids_json,
                result_json = excluded.result_json,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                safe_target,
                state,
                json_dumps(safe_request),
                json_dumps(safe_candidates),
                selected_account_id,
                json_dumps(selected_article_ids or []),
                json_dumps(safe_result),
                now,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()


def load_wizard_session(base: Path, session_id: str) -> dict[str, Any]:
    init_exporter_db(base)
    db = connect_db(base)
    try:
        row = db.execute("SELECT * FROM wizard_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise RuntimeError(f"wizard session not found: {session_id}")
        item = dict(row)
        item["request"] = load_json_text(item.pop("request_json")) or {}
        item["candidates"] = load_json_text(item.pop("candidates_json")) or []
        item["selected_article_ids"] = load_json_text(item.pop("selected_article_ids_json")) or []
        item["result"] = load_json_text(item.pop("result_json")) or {}
        return item
    finally:
        db.close()


def parse_wizard_request(args: argparse.Namespace) -> dict[str, Any]:
    text = sanitize_text_urls(str(args.target or "").strip())
    query = text
    quoted = re.search(r"[「『“\"]([^」』”\"]{1,80})[」』”\"]", text)
    if quoted:
        query = quoted.group(1).strip()
    else:
        after_account = re.search(r"公众号\s*([^\s，。,；;：:]{1,80})", text)
        if after_account:
            query = after_account.group(1).strip()
    latest = args.latest
    if latest is None:
        match = re.search(r"(?:最新|前|近)\s*(\d+)\s*(?:篇|条)?", text)
        if match:
            latest = int(match.group(1))
    urls = []
    for url in ARTICLE_URL_RE.findall(text):
        try:
            urls.append(clean_url(url))
        except ValueError:
            urls.append(safe_display_url(url))
    wants_list = args.list_only or bool(re.search(r"列出|有哪些|让我选|先看|列表", text))
    wants_sync = args.sync_only
    wants_download = not wants_list and not args.sync_only
    wants_engagement = bool(re.search(r"评论|互动|阅读数|点赞|在看|精选留言|精选评论", text))
    return {
        "target": text,
        "account_query": query,
        "urls": urls,
        "latest": latest,
        "limit": args.limit,
        "keyword": args.keyword,
        "list_only": wants_list,
        "sync_only": wants_sync,
        "download": wants_download,
        "output_dir": args.output_dir,
        "no_assets": args.no_assets,
        "profile": args.profile,
        "auto_add": args.auto_add,
        "engagement_mode": "elected" if wants_engagement else "none",
    }


def wizard_login_required(base: Path, session_id: str, request: dict[str, Any], account: dict[str, Any] | None, error: str = "") -> dict[str, Any]:
    """Create or reuse a QR artifact so login is visible in the task result."""
    try:
        existing = load_wizard_session(base, session_id).get("result", {}) if session_id else {}
    except RuntimeError:
        existing = {}
    login_id = str(existing.get("login_id") or "") if isinstance(existing, dict) else ""
    login: dict[str, Any]
    try:
        if login_id:
            previous = load_qr_login_session(base, login_id)
            if str(previous.get("status")) == "waiting_for_scan":
                login = {"login_id": login_id, "qrcode_path": previous.get("qrcode_path", ""), "expires_at": previous.get("expires_at", "")}
            else:
                login = start_qr_login(base, str(request.get("base_url") or ""))
        else:
            login = start_qr_login(base, str(request.get("base_url") or ""))
    except Exception as exc:
        return {"ok": False, "state": "need_login", "session_id": session_id, "error": str(exc) or error}
    qrcode_path = str(login.get("qrcode_path") or "")
    result = {
        "ok": False,
        "state": "need_login",
        "session_id": session_id,
        "account": slim_account(account) if account else {},
        "login_id": str(login.get("login_id") or login_id),
        "expires_at": str(login.get("expires_at") or ""),
        "artifacts": [{"type": "image", "path": qrcode_path, "alt": "微信扫码登录"}] if qrcode_path else [],
        "error": error,
        "next_step": "请扫描结果中的二维码并确认登录；确认后恢复同一任务。",
    }
    save_wizard_session(base, session_id, str(request.get("target") or ""), "need_login", request, selected_account_id=int(account.get("id") or 0) if account else 0, result=result)
    return result


def slim_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": account.get("id"),
        "fakeid": account.get("fakeid"),
        "nickname": account.get("nickname"),
        "alias": account.get("alias"),
        "description": account.get("description", ""),
        "article_count": account.get("article_count", 0),
        "synced_count": account.get("synced_count", 0),
        "source": account.get("source", "local"),
        "score": account.get("score", 0),
        "reason": account.get("reason", ""),
    }


def slim_article(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(article["id"]),
        "title": article["title"],
        "publish_time": article["publish_time"],
        "author": article["author"],
        "url": article["url"],
        "digest": article["digest"],
        "collection_title": article["collection_title"],
        "content_downloaded": bool(article["content_downloaded"]),
    }


def local_account_by_article_url(base: Path, url: str) -> dict[str, Any] | None:
    try:
        canonical = clean_url(url)
    except ValueError:
        canonical = safe_display_url(url)
    parsed = urllib.parse.urlsplit(canonical)
    canonical_without_query = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    canonical_prefix = canonical_without_query + "?"
    init_exporter_db(base)
    db = connect_db(base)
    try:
        row = db.execute(
            """
            SELECT t.*
            FROM articles a
            JOIN target_accounts t ON t.id = a.account_id
            WHERE a.url = ?
               OR a.url = ?
               OR substr(a.url, 1, ?) = ?
            ORDER BY a.updated_at DESC
            LIMIT 1
            """,
            (canonical, canonical_without_query, len(canonical_prefix), canonical_prefix),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def is_synced_today(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        synced_at = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if synced_at.tzinfo is not None:
        synced_date = synced_at.astimezone().date()
    else:
        synced_date = synced_at.date()
    return synced_date == dt.datetime.now().astimezone().date()


def ensure_article_ids_belong_to_account(base: Path, article_ids: list[int], account_id: int) -> None:
    if not article_ids:
        return
    placeholders = ",".join(["?"] * len(article_ids))
    db = connect_db(base)
    try:
        rows = db.execute(
            f"SELECT id, account_id FROM articles WHERE id IN ({placeholders})",
            article_ids,
        ).fetchall()
        found = {int(row["id"]): int(row["account_id"]) for row in rows}
    finally:
        db.close()
    missing = [article_id for article_id in article_ids if article_id not in found]
    wrong = [article_id for article_id, owner in found.items() if owner != account_id]
    if missing:
        raise RuntimeError(f"article ids not found: {','.join(str(item) for item in missing)}")
    if wrong:
        raise RuntimeError(f"article ids do not belong to selected account: {','.join(str(item) for item in wrong)}")


def resolve_wizard_account(base: Path, request: dict[str, Any], account_id: int = 0) -> dict[str, Any]:
    if account_id:
        return {"state": "resolved", "account": dict(get_account_row(base, account_id=account_id)), "candidates": []}

    target = str(request.get("account_query") or request.get("target") or "").strip()
    urls = request.get("urls") or []
    if urls:
        local = local_account_by_article_url(base, str(urls[0]))
        if local:
            return {"state": "resolved", "account": local, "candidates": []}
        try:
            result = account_by_url(base, str(urls[0]), str(request.get("profile") or ""))
            accounts = [slim_account(item) for item in result.get("accounts", [])]
            if len(accounts) == 1:
                account = upsert_account(base, accounts[0])["account"]
                return {"state": "resolved", "account": account, "candidates": []}
            if len(accounts) > 1:
                return {"state": "need_account_choice", "candidates": accounts[:5]}
        except Exception as exc:
            return {"state": "need_login", "error": str(exc), "candidates": []}

    candidates = local_account_candidates(base, target, 5)
    exact = [item for item in candidates if int(item.get("score") or 0) >= 100]
    if len(exact) == 1:
        return {"state": "resolved", "account": exact[0], "candidates": []}
    if len(candidates) == 1 and int(candidates[0].get("score") or 0) >= 70:
        return {"state": "resolved", "account": candidates[0], "candidates": []}
    if len(candidates) > 1:
        return {"state": "need_account_choice", "candidates": [slim_account(item) for item in candidates]}

    try:
        result = search_accounts(base, target, 0, 5, str(request.get("profile") or ""))
        remote = [slim_account({**item, "source": "remote", "score": 60, "reason": "search"}) for item in result.get("accounts", [])]
    except Exception as exc:
        return {"state": "need_login", "error": str(exc), "candidates": []}
    if len(remote) == 1 and request.get("auto_add"):
        account = upsert_account(base, remote[0])["account"]
        return {"state": "resolved", "account": account, "candidates": []}
    if remote:
        return {"state": "need_account_choice", "candidates": remote}
    return {"state": "not_found", "candidates": []}


def choose_wizard_candidate(base: Path, session: dict[str, Any], choice: str) -> dict[str, Any]:
    candidates = list(session.get("candidates") or [])
    if not candidates:
        raise RuntimeError("wizard session has no account candidates")
    selected: dict[str, Any] | None = None
    if choice:
        if re.fullmatch(r"\d+", choice):
            number = int(choice)
            if 1 <= number <= len(candidates):
                selected = candidates[number - 1]
            else:
                selected = next((item for item in candidates if int(item.get("id") or 0) == number), None)
        if not selected:
            selected = next(
                (
                    item
                    for item in candidates
                    if str(item.get("fakeid") or "") == choice
                    or str(item.get("nickname") or "") == choice
                    or str(item.get("alias") or "") == choice
                ),
                None,
            )
    if not selected:
        raise RuntimeError("account choice did not match any candidate")
    if selected.get("id"):
        return dict(get_account_row(base, account_id=int(selected["id"])))
    return upsert_account(base, selected)["account"]


def run_wizard_after_account(
    base: Path,
    session_id: str,
    request: dict[str, Any],
    account: dict[str, Any],
    article_ids: str = "",
) -> dict[str, Any]:
    account_id = int(account["id"])
    synced = None
    if request.get("sync_only") or request.get("download") or request.get("list_only"):
        existing = list_articles(base, account_id=account_id, limit=max(int(request.get("latest") or 0), 1))
        has_profile = get_active_profile(base, str(request.get("profile") or "")) is not None
        fresh_download = bool(request.get("download")) and request.get("latest") is not None and not article_ids
        stale_cache = not is_synced_today(account.get("last_sync_at"))
        needs_sync = bool(request.get("sync_only")) or not existing or has_profile or fresh_download or stale_cache
        if needs_sync:
            synced = sync_account_articles(
                base,
                account_id,
                int(request.get("limit") or 200),
                str(request.get("keyword") or ""),
                str(request.get("profile") or ""),
            )
            if not synced.get("ok") and (request.get("sync_only") or not existing or fresh_download or stale_cache):
                return wizard_login_required(base, session_id, request, account, "; ".join(synced.get("errors") or []) or "sync failed")
    if request.get("sync_only"):
        result = {
            "ok": True,
            "state": "done",
            "session_id": session_id,
            "account": slim_account(dict(get_account_row(base, account_id=account_id))),
            "sync": synced,
            "download": None,
        }
        save_wizard_session(base, session_id, str(request.get("target") or ""), "done", request, selected_account_id=account_id, result=result)
        return result

    latest = request.get("latest")
    selected_ids = select_article_ids(
        base,
        account_id=account_id,
        article_ids=article_ids,
        latest=int(latest) if latest is not None else None,
        keyword=str(request.get("keyword") or ""),
    )
    ensure_article_ids_belong_to_account(base, selected_ids, account_id)
    articles = [slim_article(item) for item in list_articles(base, account_id=account_id, limit=int(request.get("limit") or 100), keyword=str(request.get("keyword") or ""))]
    if latest is not None:
        articles = articles[: int(latest)]

    if request.get("list_only") or not request.get("download"):
        state = "need_article_choice" if not article_ids and not latest else "done"
        result = {
            "ok": True,
            "state": state,
            "session_id": session_id,
            "account": slim_account(dict(get_account_row(base, account_id=account_id))),
            "sync": synced,
            "count": len(articles),
            "articles": articles,
            "selected_article_ids": selected_ids,
            "next_step": "选好后运行 exporter-wizard --resume <session-id> --article-ids '1,2,3' 下载。" if state == "need_article_choice" else "",
        }
        save_wizard_session(base, session_id, str(request.get("target") or ""), state, request, selected_account_id=account_id, selected_article_ids=selected_ids, result=result)
        return result

    if not selected_ids:
        result = {
            "ok": True,
            "state": "need_article_choice",
            "session_id": session_id,
            "account": slim_account(dict(get_account_row(base, account_id=account_id))),
            "articles": articles,
            "next_step": "没有自动选中文章。请用 --article-ids 指定要下载的文章 ID。",
        }
        save_wizard_session(base, session_id, str(request.get("target") or ""), "need_article_choice", request, selected_account_id=account_id, result=result)
        return result

    output_root = str(request.get("output_dir") or "")
    if request.get("engagement_mode") == "elected":
        engagement = sync_engagement_for_articles(base, account_id, selected_ids, output_root=output_root)
        state = "waiting_wechat_collection" if engagement.get("status") == "waiting_credential" else ("done" if engagement.get("ok") else "failed_recoverable")
        result = {
            "ok": bool(engagement.get("ok") or engagement.get("status") == "waiting_credential"),
            "state": state,
            "session_id": session_id,
            "account": slim_account(dict(get_account_row(base, account_id=account_id))),
            "sync": synced,
            "selected_article_ids": selected_ids,
            "download": None,
            "engagement": engagement,
            "flow": "exporter-sync -> engagement batch download",
        }
        if engagement.get("status") == "waiting_credential":
            result["next_step"] = engagement.get("next_step") or "已复制代表文章链接，请粘贴到微信客户端并打开；打开后回复“已打开”。"
        save_wizard_session(base, session_id, str(request.get("target") or ""), state, request, selected_account_id=account_id, selected_article_ids=selected_ids, result=result)
        return result

    downloaded = download_articles(base, selected_ids, output_root, bool(request.get("no_assets")), force=True)
    engagement = None
    state = "done"
    result = {
        "ok": downloaded.get("ok", False),
        "state": state,
        "session_id": session_id,
        "account": slim_account(dict(get_account_row(base, account_id=account_id))),
        "sync": synced,
        "selected_article_ids": selected_ids,
        "download": downloaded,
        "engagement": engagement,
    }
    if engagement and engagement.get("status") == "waiting_credential":
        result["next_step"] = engagement.get("next_step") or "已复制代表文章链接，请粘贴到微信客户端并打开；打开后回复“已打开”。"
    save_wizard_session(base, session_id, str(request.get("target") or ""), state, request, selected_account_id=account_id, selected_article_ids=selected_ids, result=result)
    return result


def resume_wizard_wechat_collection(base: Path, session_id: str, session: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    previous = dict(session.get("result") or {})
    engagement = dict(previous.get("engagement") or {})
    run_id = str(engagement.get("run_id") or "")
    biz = str(engagement.get("biz") or "")
    if not biz and run_id:
        run, _contexts = contexts_for_engagement_run(base, run_id)
        if run:
            scope = load_json_text(str(run.get("scope_json") or "{}")) or {}
            biz = str(scope.get("biz") or "")
    resumed = resume_waiting_engagement_runs(base, biz, run_id, str(request.get("output_dir") or ""))
    state = "done" if resumed.get("ok") else "waiting_wechat_collection"
    result = {
        **previous,
        "ok": bool(resumed.get("ok")),
        "state": state,
        "session_id": session_id,
        "resumed_from": "waiting_wechat_collection",
        "engagement_resume": resumed,
    }
    if not resumed.get("ok"):
        result["next_step"] = "请确认代表文章已在微信客户端打开并加载评论区，然后再次恢复本任务。"
    save_wizard_session(
        base,
        session_id,
        str(request.get("target") or ""),
        state,
        request,
        selected_account_id=int(session.get("selected_account_id") or 0),
        selected_article_ids=[int(value) for value in session.get("selected_article_ids", []) if str(value).isdigit()],
        result=result,
    )
    return result


def command_wizard(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    init_exporter_db(base)
    if args.resume:
        session = load_wizard_session(base, args.resume)
        request = dict(session.get("request") or {})
        session_id = args.resume
        if session.get("state") in {"waiting_wechat_collection", "waiting_wechat_credential"}:
            result = resume_wizard_wechat_collection(base, session_id, session, request)
            write_json_response(scrub_payload(result))
            return 0 if result.get("ok") else 1
        if session.get("state") == "need_login":
            login_id = str((session.get("result") or {}).get("login_id") or "")
            if login_id:
                try:
                    login_status = qr_login_status(base, login_id)
                    if login_status.get("ready_to_complete"):
                        complete_qr_login(base, login_id, str(request.get("profile") or ""))
                    else:
                        result = {**dict(session.get("result") or {}), "login_status": login_status}
                        write_json_response(scrub_payload(result))
                        return 1
                except Exception as exc:
                    result = wizard_login_required(base, session_id, request, None, str(exc))
                    write_json_response(scrub_payload(result))
                    return 1
        if args.latest is not None:
            request["latest"] = args.latest
        if args.limit:
            request["limit"] = args.limit
        if args.keyword:
            request["keyword"] = args.keyword
        if args.output_dir:
            request["output_dir"] = args.output_dir
        if args.no_assets:
            request["no_assets"] = True
        if args.list_only:
            request["list_only"] = True
            request["download"] = False
        if args.sync_only:
            request["sync_only"] = True
            request["download"] = False
        if session.get("state") == "need_account_choice":
            account = choose_wizard_candidate(base, session, args.choice)
        else:
            account_id = args.account_id or int(session.get("selected_account_id") or 0)
            if account_id:
                account = dict(get_account_row(base, account_id=account_id))
            else:
                resolved = resolve_wizard_account(base, request, 0)
                state = str(resolved.get("state"))
                if state == "resolved":
                    account = dict(resolved["account"])
                elif state == "need_account_choice":
                    candidates = [slim_account(item) for item in resolved.get("candidates", [])]
                    result = {
                        "ok": True,
                        "state": "need_account_choice",
                        "session_id": session_id,
                        "target": request["target"],
                        "candidates": candidates,
                        "next_step": "回复要选的公众号名称，或运行 exporter-wizard --resume <session-id> --choice <序号/fakeid>。",
                    }
                    save_wizard_session(base, session_id, request["target"], state, request, candidates, result=result)
                    write_json_response(scrub_payload(result))
                    return 0
                else:
                    result = wizard_login_required(base, session_id, request, None, str(resolved.get("error") or ""))
                    write_json_response(scrub_payload(result))
                    return 1
        if args.article_ids:
            request["download"] = True
            request["list_only"] = False
        result = run_wizard_after_account(base, session_id, request, account, args.article_ids)
        write_json_response(scrub_payload(result))
        return 0 if result.get("ok") else 1

    request = parse_wizard_request(args)
    session_id = wizard_session_id()
    resolved = resolve_wizard_account(base, request, args.account_id)
    state = str(resolved.get("state"))
    if state == "resolved":
        result = run_wizard_after_account(base, session_id, request, dict(resolved["account"]), args.article_ids)
        write_json_response(scrub_payload(result))
        return 0 if result.get("ok") else 1
    if state == "need_account_choice":
        candidates = [slim_account(item) for item in resolved.get("candidates", [])]
        result = {
            "ok": True,
            "state": "need_account_choice",
            "session_id": session_id,
            "target": request["target"],
            "candidates": candidates,
            "next_step": "回复要选的公众号名称，或运行 exporter-wizard --resume <session-id> --choice <序号/fakeid>。",
        }
        save_wizard_session(base, session_id, request["target"], state, request, candidates, result=result)
        write_json_response(scrub_payload(result))
        return 0
    result = wizard_login_required(base, session_id, request, None, str(resolved.get("error") or "")) if state == "need_login" else {
        "ok": False, "state": state, "session_id": session_id, "target": request["target"], "error": resolved.get("error", "")
    }
    if state != "need_login":
        save_wizard_session(base, session_id, request["target"], state, request, result=result)
    write_json_response(scrub_payload(result))
    return 1


def command_import_fixture(args: argparse.Namespace) -> int:
    """Test helper: import local account/article fixture without network."""
    base = runtime_dir(args.runtime_dir)
    data = json.loads(Path(args.path).expanduser().read_text(encoding="utf-8"))
    account = upsert_account(base, normalize_account(data["account"]))["account"]
    rows = [normalize_article(item, int(account["id"])) for item in data.get("articles", [])]
    upsert_articles(base, rows)
    ensure_collections_from_articles(base, int(account["id"]))
    write_json_response({"ok": True, "account": account, "article_count": len(rows)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exporter-mode runtime for WeChat article downloader")
    parser.add_argument("--runtime-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("exporter-init")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.set_defaults(func=command_init)

    p = sub.add_parser("exporter-server-start")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=command_server)

    p = sub.add_parser("exporter-login-start")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--base-url", default="")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=command_login_start)

    p = sub.add_parser("exporter-login-qr-start")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--base-url", default="")
    p.add_argument("--open", action="store_true", help="Open the QR image after fetching it")
    p.set_defaults(func=command_login_qr_start)

    p = sub.add_parser("exporter-login-qr-status")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("login_id")
    p.set_defaults(func=command_login_qr_status)

    p = sub.add_parser("exporter-login-qr-complete")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("login_id")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_login_qr_complete)

    p = sub.add_parser("exporter-config")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--auth-key", required=True)
    p.add_argument("--profile", default="default")
    p.add_argument("--expires-at", default="")
    p.add_argument("--allow-plain-auth-key", action="store_true")
    p.set_defaults(func=command_config)

    p = sub.add_parser("exporter-auth-check")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_auth_check)

    p = sub.add_parser("exporter-db-status")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.set_defaults(func=command_db_status)

    p = sub.add_parser("exporter-logout")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_logout)

    p = sub.add_parser("exporter-search")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("keyword")
    p.add_argument("--begin", type=int, default=0)
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_search)

    p = sub.add_parser("exporter-account-by-url")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("url")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_account_by_url)

    p = sub.add_parser("exporter-add")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--fakeid", default="")
    p.add_argument("--nickname", default="")
    p.add_argument("--alias", default="")
    p.add_argument("--avatar-url", default="")
    p.add_argument("--description", default="")
    p.add_argument("--from-json", default="")
    p.set_defaults(func=command_add)

    p = sub.add_parser("exporter-accounts")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.set_defaults(func=command_accounts)

    p = sub.add_parser("exporter-sync")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--keyword", default="")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_sync)

    p = sub.add_parser("exporter-sync-all")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--per-account-limit", type=int, default=50)
    p.set_defaults(func=command_sync_all)

    p = sub.add_parser("exporter-download-new")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--output-dir", default="")
    p.add_argument("--no-assets", action="store_true")
    p.set_defaults(func=command_download_new)

    p = sub.add_parser("exporter-daily-run")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--per-account-limit", type=int, default=50)
    p.add_argument("--output-dir", default="")
    p.add_argument("--no-assets", action="store_true")
    p.set_defaults(func=command_daily_run)

    p = sub.add_parser("exporter-articles")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--account-id", type=int, default=0)
    p.add_argument("--collection-id", type=int, default=0)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--keyword", default="")
    p.add_argument("--downloaded", choices=["", "yes", "no"], default="")
    p.set_defaults(func=command_articles)

    p = sub.add_parser("exporter-preview")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--article-id", type=int, required=True)
    p.set_defaults(func=command_preview)

    p = sub.add_parser("exporter-open-original")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--article-id", type=int, required=True)
    p.set_defaults(func=command_open_original)

    p = sub.add_parser("exporter-download")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--account-id", type=int, default=0)
    p.add_argument("--collection-id", type=int, default=0)
    p.add_argument("--article-ids", default="")
    p.add_argument("--latest", type=int, default=None)
    p.add_argument("--titles", default="")
    p.add_argument("--keyword", default="")
    p.add_argument("--output-dir", default="")
    p.add_argument("--no-assets", action="store_true")
    p.add_argument("--force", action="store_true", help="Overwrite existing local Markdown instead of skipping")
    p.set_defaults(func=command_download)

    p = sub.add_parser("exporter-fields")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--name", default="default")
    p.add_argument("--set", default="", help="Comma/space separated visible fields")
    p.add_argument("--default-format", default="markdown")
    p.set_defaults(func=command_fields)

    p = sub.add_parser("exporter-collections")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--account-id", type=int, default=0)
    p.set_defaults(func=command_collections)

    p = sub.add_parser("exporter-collection-add")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--article-ids", required=True)
    p.add_argument("--collection-url", default="")
    p.set_defaults(func=command_collection_add)

    p = sub.add_parser("exporter-download-collection")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--collection-id", type=int, required=True)
    p.add_argument("--output-dir", default="")
    p.add_argument("--no-assets", action="store_true")
    p.add_argument("--force", action="store_true", help="Overwrite existing local Markdown instead of skipping")
    p.set_defaults(func=command_download_collection)

    p = sub.add_parser("exporter-metrics-import")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("input", help="JSON/CSV path or inline JSON payload")
    p.set_defaults(func=command_metrics_import)

    p = sub.add_parser("exporter-comments-import")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("input", help="JSON/CSV path or inline JSON payload")
    p.set_defaults(func=command_comments_import)

    p = sub.add_parser("exporter-comments")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--article-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=command_comments)

    p = sub.add_parser("exporter-article-context")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--article-id", type=int, required=True)
    p.add_argument("--html-file", default="", help="Local article HTML used only to parse non-sensitive context")
    p.add_argument("--biz", default="", help="Explicit public-account __biz; not a credential")
    p.add_argument("--comment-id", default="", help="Explicit non-sensitive article comment ID")
    p.add_argument("--source", default="html", choices=["html", "snapshot", "manual", "public_html"])
    p.set_defaults(func=command_article_context)

    p = sub.add_parser("wechat-collection-sync-engagement")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--output-dir", default="")
    p.set_defaults(func=command_wechat_collection_sync_engagement)

    p = sub.add_parser("wechat-collection-resume-engagement")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--biz", default="")
    p.add_argument("--run-id", default="")
    p.add_argument("--output-dir", default="")
    p.set_defaults(func=command_wechat_collection_resume_engagement)

    p = sub.add_parser("wechat-collection-writeback")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--article-ids", required=True)
    p.add_argument("--output-dir", default="")
    p.set_defaults(func=command_wechat_collection_writeback)

    p = sub.add_parser("wechat-collection-diagnostics")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--export-fixture", default="")
    p.set_defaults(func=command_wechat_collection_diagnostics)

    p = sub.add_parser("library-dataset")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--account-id", type=int, required=True)
    p.add_argument("--dataset-id", default="")
    p.add_argument("--output-dir", default="")
    p.set_defaults(func=command_library_dataset)

    p = sub.add_parser("exporter-wizard")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("target", nargs="?", default="", help="公众号名、搜索关键词、或包含文章 URL 的自然语言目标")
    p.add_argument("--latest", type=int, default=None)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--keyword", default="")
    p.add_argument("--list-only", action="store_true")
    p.add_argument("--sync-only", action="store_true")
    p.add_argument("--auto-add", action="store_true", help="远端搜索只有一个结果时自动添加")
    p.add_argument("--resume", default="")
    p.add_argument("--choice", default="", help="恢复 need_account_choice 会话时选择序号、fakeid、名称或 alias")
    p.add_argument("--account-id", type=int, default=0)
    p.add_argument("--article-ids", default="")
    p.add_argument("--output-dir", default="")
    p.add_argument("--no-assets", action="store_true")
    p.add_argument("--profile", default="")
    p.set_defaults(func=command_wizard)

    p = sub.add_parser("exporter-import-fixture")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("path")
    p.set_defaults(func=command_import_fixture)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError, sqlite3.Error, urllib.error.URLError, subprocess.CalledProcessError) as exc:
        write_json_response({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
