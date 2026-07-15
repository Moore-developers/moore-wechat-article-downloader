#!/usr/bin/env python3
"""Unified target-driven wizard for Moore WeChat article downloader.

P0a focuses on URL-mode routing, gates, SQLite task state, and manifest
verification. Exporter and history modes are intentionally routed into
recoverable states for later milestones instead of pretending they are done.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from wechat_downloader import (  # noqa: E402
    DEFAULT_DELIVERY_DIR,
    DEFAULT_PROXY_PORT,
    active_proxy_port,
    build_history_open_url,
    choose_network_service,
    extract_urls,
    get_network_proxy_state,
    history_account_dir,
    make_run_id,
    open_history_link,
    plan_markdown_account_groups,
    run_markdown_only_download_by_account,
    run_markdown_only_download,
    runtime_dir,
    sanitize_text_urls,
    save_history_session,
    start_history_session,
    scrub_payload,
    system_proxy_state_path,
    utc_now,
)
import wechat_exporter  # noqa: E402


WIZARD_DB_VERSION = 6
HISTORY_WORDS = ("历史", "列表", "最近", "全部", "往期", "过往", "文章列表", "让我选", "列出")
DOWNLOAD_WORDS = ("下载", "保存", "导出")
EXPORTER_WORDS = ("exporter", "公众号", "同步", "搜索")
SYNC_WORDS = ("同步", "刷新", "更新")
LIST_WORDS = ("列出", "列表", "让我选", "有哪些", "最近")
ENGAGEMENT_WORDS = ("评论", "互动", "阅读", "阅读数", "点赞", "分享", "精选留言", "精选评论", "在看", "收藏数")
SENSITIVE_TEXT_RE = re.compile(r"\b(auth-key|pass_ticket|appmsg_token|sessionid|token|cookie|uin|key)=\S+", re.I)
DEFAULT_DOWNLOAD_PREFERENCES = {
    "output_dir": "",
    "download_assets": True,
    "html_concurrency": 1,
    "max_retries": 0,
    "overwrite_policy": "skip",
}
MODE_CONFIDENCE_THRESHOLD = 0.7


def keychain_available() -> bool:
    return sys.platform == "darwin" and bool(shutil.which("security"))


def wizard_db_path(base: Path) -> Path:
    return base / "wizard.sqlite"


def connect_wizard_db(base: Path) -> sqlite3.Connection:
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(wizard_db_path(base))
    db.row_factory = sqlite3.Row
    return db


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def read_json_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def write_json_response(payload: Any) -> None:
    print(json.dumps(scrub_for_storage(payload), ensure_ascii=False, indent=2))


def scrub_sensitive_text(value: str) -> str:
    text = sanitize_text_urls(value)
    return SENSITIVE_TEXT_RE.sub("<redacted>", text)


def scrub_for_storage(value: Any) -> Any:
    if isinstance(value, dict):
        scrubbed = scrub_payload(value)
        return {str(key): scrub_for_storage(item) for key, item in scrubbed.items()}
    if isinstance(value, list):
        return [scrub_for_storage(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_for_storage(item) for item in value]
    if isinstance(value, str):
        return scrub_sensitive_text(value)
    return value


def init_wizard_db(base: Path) -> dict[str, Any]:
    db = connect_wizard_db(base)
    wal_enabled = False
    try:
        try:
            wal_enabled = str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
        except sqlite3.Error:
            wal_enabled = False
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS wizard_tasks (
                id TEXT PRIMARY KEY,
                intent_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT '',
                output_dir TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wizard_gate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                gate TEXT NOT NULL,
                state TEXT NOT NULL,
                ok INTEGER NOT NULL,
                recoverable INTEGER NOT NULL DEFAULT 1,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES wizard_tasks(id)
            );

            CREATE TABLE IF NOT EXISTS wizard_choices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                choice_type TEXT NOT NULL,
                choices_json TEXT NOT NULL DEFAULT '[]',
                selected_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES wizard_tasks(id)
            );

            CREATE TABLE IF NOT EXISTS wizard_locks (
                name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS download_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                source_mode TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                status TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS download_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                article_id TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                markdown_path TEXT NOT NULL DEFAULT '',
                image_dir TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                retries INTEGER NOT NULL DEFAULT 0,
                previous_run_id TEXT NOT NULL DEFAULT '',
                previous_task_id TEXT NOT NULL DEFAULT '',
                previous_output_dir TEXT NOT NULL DEFAULT '',
                previous_markdown_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES download_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                name TEXT PRIMARY KEY,
                value_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS smoke_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                ok INTEGER NOT NULL,
                status TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        ensure_column(db, "download_runs", "duration_ms", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "download_runs", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "download_items", "previous_run_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "download_items", "previous_task_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "download_items", "previous_output_dir", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "download_items", "previous_markdown_path", "TEXT NOT NULL DEFAULT ''")
        now = utc_now()
        db.execute(
            """
            INSERT OR IGNORE INTO user_preferences (name, value_json, created_at, updated_at)
            VALUES ('download', ?, ?, ?)
            """,
            (json_dumps(DEFAULT_DOWNLOAD_PREFERENCES), now, now),
        )
        db.execute(f"PRAGMA user_version = {WIZARD_DB_VERSION}")
        db.commit()
    finally:
        db.close()
    return {"ok": True, "db": str(wizard_db_path(base)), "wal_enabled": wal_enabled, "user_version": WIZARD_DB_VERSION}


def ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_download_preferences(value: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_DOWNLOAD_PREFERENCES)
    merged.update({key: item for key, item in value.items() if key in merged})
    merged["output_dir"] = str(merged.get("output_dir") or "")
    merged["download_assets"] = bool(merged.get("download_assets"))
    merged["html_concurrency"] = max(1, min(safe_int(merged.get("html_concurrency"), 1), 4))
    merged["max_retries"] = max(0, min(safe_int(merged.get("max_retries"), 0), 3))
    if str(merged.get("overwrite_policy") or "") not in {"skip", "force"}:
        merged["overwrite_policy"] = "skip"
    return merged


def get_download_preferences(base: Path) -> dict[str, Any]:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        row = db.execute("SELECT value_json FROM user_preferences WHERE name = 'download'").fetchone()
        data = read_json_text(str(row["value_json"])) if row else {}
        return clamp_download_preferences(data if isinstance(data, dict) else {})
    finally:
        db.close()


def save_download_preferences(base: Path, updates: dict[str, Any]) -> dict[str, Any]:
    init_wizard_db(base)
    current = get_download_preferences(base)
    current.update({key: value for key, value in updates.items() if value is not None})
    preferences = clamp_download_preferences(current)
    now = utc_now()
    db = connect_wizard_db(base)
    try:
        db.execute(
            """
            INSERT INTO user_preferences (name, value_json, created_at, updated_at)
            VALUES ('download', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (json_dumps(preferences), now, now),
        )
        db.commit()
        return preferences
    finally:
        db.close()


def resolve_download_options(base: Path, args: argparse.Namespace) -> dict[str, Any]:
    preferences = get_download_preferences(base)
    arg_download_assets = getattr(args, "download_assets", None)
    if arg_download_assets is None and hasattr(args, "no_assets"):
        arg_download_assets = not bool(getattr(args, "no_assets"))
    force_from_preference = str(preferences.get("overwrite_policy") or "skip") == "force"
    force_from_cli = bool(getattr(args, "force", False))
    return {
        "output_dir": str(getattr(args, "output_dir", "") or preferences.get("output_dir") or ""),
        "no_assets": not arg_download_assets if arg_download_assets is not None else not bool(preferences.get("download_assets", True)),
        "html_concurrency": (
            getattr(args, "html_concurrency")
            if getattr(args, "html_concurrency", None) is not None
            else int(preferences.get("html_concurrency") or 1)
        ),
        "max_retries": (
            getattr(args, "max_retries")
            if getattr(args, "max_retries", None) is not None
            else int(preferences.get("max_retries") or 0)
        ),
        "force": force_from_cli or force_from_preference,
        "force_source": "cli" if force_from_cli else ("preference" if force_from_preference else "none"),
    }


def new_task_id(goal: str) -> str:
    digest = hashlib.sha256(f"{goal}-{dt.datetime.now().isoformat()}-{os.getpid()}".encode("utf-8")).hexdigest()[:16]
    return "task_" + digest


def save_task(
    base: Path,
    task_id: str,
    intent: dict[str, Any],
    state: str,
    mode: str = "",
    output_dir: str = "",
    result: dict[str, Any] | None = None,
) -> None:
    init_wizard_db(base)
    now = utc_now()
    db = connect_wizard_db(base)
    try:
        db.execute(
            """
            INSERT INTO wizard_tasks
                (id, intent_json, state, mode, output_dir, result_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                intent_json = excluded.intent_json,
                state = excluded.state,
                mode = excluded.mode,
                output_dir = excluded.output_dir,
                result_json = excluded.result_json,
                updated_at = excluded.updated_at
            """,
            (
                task_id,
                json_dumps(scrub_for_storage(intent)),
                state,
                mode,
                output_dir,
                json_dumps(scrub_for_storage(result or {})),
                now,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()


def load_task(base: Path, task_id: str) -> dict[str, Any]:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        row = db.execute("SELECT * FROM wizard_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise RuntimeError(f"wizard task not found: {task_id}")
        item = dict(row)
        item["intent"] = read_json_text(item.pop("intent_json")) or {}
        item["result"] = read_json_text(item.pop("result_json")) or {}
        return item
    finally:
        db.close()


def save_choices(
    base: Path,
    task_id: str,
    choice_type: str,
    choices: list[dict[str, Any]],
    selected: list[dict[str, Any]] | None = None,
) -> None:
    init_wizard_db(base)
    now = utc_now()
    db = connect_wizard_db(base)
    try:
        db.execute("DELETE FROM wizard_choices WHERE task_id = ? AND choice_type = ?", (task_id, choice_type))
        db.execute(
            """
            INSERT INTO wizard_choices
                (task_id, choice_type, choices_json, selected_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                choice_type,
                json_dumps(scrub_for_storage(choices)),
                json_dumps(scrub_for_storage(selected or [])),
                now,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()


def load_choices(base: Path, task_id: str, choice_type: str) -> list[dict[str, Any]]:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        row = db.execute(
            """
            SELECT choices_json FROM wizard_choices
            WHERE task_id = ? AND choice_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id, choice_type),
        ).fetchone()
        if not row:
            return []
        data = read_json_text(str(row["choices_json"])) or []
        return data if isinstance(data, list) else []
    finally:
        db.close()


def mark_choices_selected(base: Path, task_id: str, choice_type: str, selected: list[dict[str, Any]]) -> None:
    choices = load_choices(base, task_id, choice_type)
    save_choices(base, task_id, choice_type, choices, selected)


def record_gate(
    base: Path,
    task_id: str,
    gate: str,
    state: str,
    ok: bool,
    evidence: dict[str, Any] | None = None,
    error: str = "",
    recoverable: bool = True,
) -> dict[str, Any]:
    init_wizard_db(base)
    event = {
        "ok": ok,
        "gate": gate,
        "state": state,
        "recoverable": recoverable,
        "next_action": "",
        "evidence": scrub_for_storage(evidence or {}),
        "error": scrub_sensitive_text(error),
    }
    db = connect_wizard_db(base)
    try:
        db.execute(
            """
            INSERT INTO wizard_gate_events
                (task_id, gate, state, ok, recoverable, evidence_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                gate,
                state,
                1 if ok else 0,
                1 if recoverable else 0,
                json_dumps(event["evidence"]),
                event["error"],
                utc_now(),
            ),
        )
        db.commit()
    finally:
        db.close()
    return event


def record_smoke(base: Path, name: str, ok: bool, status: str, evidence: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    init_wizard_db(base)
    payload = {
        "name": name,
        "ok": ok,
        "status": status,
        "evidence": scrub_for_storage(evidence or {}),
        "error": scrub_sensitive_text(error),
        "created_at": utc_now(),
    }
    db = connect_wizard_db(base)
    try:
        db.execute(
            """
            INSERT INTO smoke_runs (name, ok, status, evidence_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload["name"],
                1 if ok else 0,
                payload["status"],
                json_dumps(payload["evidence"]),
                payload["error"],
                payload["created_at"],
            ),
        )
        db.commit()
    finally:
        db.close()
    return payload


def latest_smoke_runs(base: Path, limit: int = 20) -> list[dict[str, Any]]:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        rows = db.execute(
            """
            SELECT id, name, ok, status, evidence_json, error, created_at
            FROM smoke_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["ok"] = bool(item["ok"])
            item["evidence"] = read_json_text(str(item.pop("evidence_json"))) or {}
            result.append(item)
        return result
    finally:
        db.close()


def gate_summary(base: Path, task_id: str) -> dict[str, Any]:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        rows = db.execute(
            """
            SELECT gate, state, ok, created_at
            FROM wizard_gate_events
            WHERE task_id = ?
            ORDER BY id
            """,
            (task_id,),
        ).fetchall()
        counts: dict[str, int] = {}
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            gate = str(row["gate"])
            counts[gate] = counts.get(gate, 0) + 1
            latest[gate] = {
                "state": str(row["state"]),
                "ok": bool(row["ok"]),
                "created_at": str(row["created_at"]),
            }
        return {"count": len(rows), "gates": counts, "latest": latest}
    finally:
        db.close()


def lock_expired(expires_at: str) -> bool:
    parsed = parse_iso_time(expires_at)
    return parsed is None or parsed <= dt.datetime.now(dt.timezone.utc)


def acquire_lock(base: Path, name: str, owner: str, ttl_seconds: int = 900) -> dict[str, Any]:
    init_wizard_db(base)
    now = utc_now()
    expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl_seconds)).isoformat()
    db = connect_wizard_db(base)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM wizard_locks WHERE expires_at <= ?", (now,))
        row = db.execute("SELECT * FROM wizard_locks WHERE name = ?", (name,)).fetchone()
        if row:
            db.commit()
            return {
                "ok": False,
                "name": name,
                "owner": str(row["owner"]),
                "expires_at": str(row["expires_at"]),
                "stale": lock_expired(str(row["expires_at"])),
            }
        db.execute(
            """
            INSERT INTO wizard_locks (name, owner, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, owner, expires_at, now, now),
        )
        db.commit()
        return {"ok": True, "name": name, "owner": owner, "expires_at": expires_at}
    finally:
        db.close()


def release_lock(base: Path, name: str, owner: str) -> None:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        db.execute("DELETE FROM wizard_locks WHERE name = ? AND owner = ?", (name, owner))
        db.commit()
    finally:
        db.close()


def list_stale_locks(base: Path) -> list[dict[str, Any]]:
    init_wizard_db(base)
    now = utc_now()
    db = connect_wizard_db(base)
    try:
        rows = db.execute("SELECT * FROM wizard_locks WHERE expires_at <= ? ORDER BY expires_at", (now,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def clear_stale_locks(base: Path) -> int:
    init_wizard_db(base)
    now = utc_now()
    db = connect_wizard_db(base)
    try:
        cur = db.execute("DELETE FROM wizard_locks WHERE expires_at <= ?", (now,))
        db.commit()
        return int(cur.rowcount or 0)
    finally:
        db.close()


def goal_digest(goal: str) -> str:
    return hashlib.sha256(sanitize_text_urls(goal).encode("utf-8")).hexdigest()[:16]


def parse_intent(goal: str) -> dict[str, Any]:
    text = sanitize_text_urls(goal.strip())
    urls = extract_urls(text)
    has_history_words = any(word in text for word in HISTORY_WORDS)
    has_download_words = any(word in text for word in DOWNLOAD_WORDS)
    has_exporter_words = any(word.lower() in text.lower() for word in EXPORTER_WORDS)
    has_sync_words = any(word in text for word in SYNC_WORDS)
    has_list_words = any(word in text for word in LIST_WORDS)
    has_engagement_words = any(word in text for word in ENGAGEMENT_WORDS)
    account_query = extract_account_query(text, urls) if has_download_words or has_exporter_words or has_sync_words or has_list_words else ""
    latest = None
    for marker in ("最新", "最近", "前"):
        if marker in text:
            after = text.split(marker, 1)[1]
            digits = "".join(ch for ch in after[:8] if ch.isdigit())
            if digits:
                latest = int(digits)
                break
    return {
        "mode": "ambiguous",
        "action": "download" if has_download_words else ("list" if has_list_words else "download"),
        "urls": urls,
        "account_query": account_query,
        "latest": latest,
        "selection": "",
        "output_dir": "",
        "requires_user_choice": False,
        "requires_engagement": has_engagement_words,
        "goal": text,
        "input_digest": goal_digest(text),
        "signals": {
            "has_history_words": has_history_words,
            "has_download_words": has_download_words,
            "has_exporter_words": has_exporter_words,
            "has_sync_words": has_sync_words,
            "has_list_words": has_list_words,
            "has_engagement_words": has_engagement_words,
            "has_account_query": bool(account_query),
            "url_count": len(urls),
        },
    }


def extract_account_query(text: str, urls: list[str]) -> str:
    for left, right in [("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'), ("'", "'")]:
        if left in text and right in text:
            start = text.find(left) + len(left)
            end = text.find(right, start)
            if end > start:
                return text[start:end].strip()
    cleaned = text
    for url in urls:
        cleaned = cleaned.replace(url, " ")
    for word in [
        "用",
        "exporter",
        "模式",
        "同步",
        "刷新",
        "更新",
        "列出",
        "历史",
        "列表",
        "往期",
        "过往",
        "主页",
        "下载",
        "公众号",
        "文章",
        "最近",
        "最新",
        "前",
        "篇",
        "条",
        "让我选",
        "有哪些",
    ]:
        cleaned = cleaned.replace(word, " ")
    cleaned = "".join(" " if ch.isdigit() else ch for ch in cleaned)
    parts = [part.strip(" ，。,;；:：") for part in cleaned.split() if part.strip(" ，。,;；:：")]
    return parts[0] if parts else ""


def decide_mode(intent: dict[str, Any]) -> dict[str, Any]:
    signals = intent["signals"]
    urls = intent["urls"]
    if urls and not signals["has_history_words"]:
        intent["mode"] = "url"
        intent["action"] = "download"
        return {
            "mode": "url",
            "mode_confidence": 0.95,
            "why": "WeChat article URL input without history/list wording",
            "why_not_other_modes": {
                "history": "no history/list/latest wording",
                "exporter": "URL download does not require exporter auth",
            },
        }
    if urls and signals["has_history_words"]:
        intent["mode"] = "history"
        intent["requires_user_choice"] = True
        return {
            "mode": "history",
            "mode_confidence": 0.72,
            "why": "article URL with history/list wording",
            "why_not_other_modes": {
                "url": "history/list wording changes the goal from single article download",
                "exporter": "sample article URL points to WeChat desktop history capture flow",
            },
        }
    if signals["has_exporter_words"] or signals["has_account_query"]:
        intent["mode"] = "exporter"
        intent["requires_user_choice"] = intent["action"] == "list"
        return {
            "mode": "exporter",
            "mode_confidence": 0.8 if signals["has_account_query"] else 0.65,
            "why": "account/exporter/sync wording without direct URL download",
            "why_not_other_modes": {
                "url": "no article URLs found",
                "history": "no sample article URL found",
            },
        }
    return {
        "mode": "ambiguous",
        "mode_confidence": 0.2,
        "why": "no supported URL-mode input found",
        "why_not_other_modes": {
            "url": "no WeChat article URLs found",
            "history": "no sample article URL found",
            "exporter": "no public-account query found",
        },
    }


def gate_input(base: Path, task_id: str, intent: dict[str, Any]) -> dict[str, Any]:
    if intent["urls"] or intent["account_query"]:
        return record_gate(
            base,
            task_id,
            "input",
            "ready",
            True,
            {
                "url_count": len(intent["urls"]),
                "account_query": intent["account_query"],
                "input_digest": intent["input_digest"],
            },
        )
    event = record_gate(
        base,
        task_id,
        "input",
        "blocked",
        False,
        {
            "url_count": len(intent["urls"]),
            "account_query": intent["account_query"],
            "input_digest": intent["input_digest"],
        },
        "No supported WeChat article URLs or public-account query found in wizard input",
    )
    event["next_action"] = "Provide one or more mp.weixin.qq.com article URLs or a public-account query."
    return event


def gate_environment(base: Path, task_id: str, output_dir: Path) -> dict[str, Any]:
    try:
        init_wizard_db(base)
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".wizard-write-", dir=str(output_dir), delete=True) as fh:
            fh.write(b"ok")
        return record_gate(
            base,
            task_id,
            "environment",
            "ready",
            True,
            {"db": str(wizard_db_path(base)), "output_dir": str(output_dir)},
        )
    except Exception as exc:
        event = record_gate(base, task_id, "environment", "blocked", False, {"output_dir": str(output_dir)}, str(exc))
        event["next_action"] = "Choose a writable output directory or fix local filesystem permissions."
        return event


def mitmdump_install_command() -> str:
    return f"python3 {SCRIPT_DIR / 'wechat_downloader.py'} history-proxy-setup --port {DEFAULT_PROXY_PORT} --install --yes"


def gate_history_environment(base: Path, task_id: str) -> dict[str, Any]:
    init_wizard_db(base)
    mitmdump_path = shutil.which("mitmdump") or ""
    evidence = {
        "db": str(wizard_db_path(base)),
        "mitmdump_available": bool(mitmdump_path),
        "mitmdump_path": mitmdump_path,
        "install_command": mitmdump_install_command(),
    }
    event = record_gate(base, task_id, "environment", "ready", True, evidence)
    if not mitmdump_path:
        event["next_action"] = "Install mitmdump before enabling the local history proxy."
    return event


def write_wizard_run_files(output_dir: Path, task_id: str, intent: dict[str, Any], manifest: dict[str, Any]) -> dict[str, str]:
    articles_json = output_dir / "articles.json"
    errors_json = output_dir / "errors.json"
    run_json = output_dir / "run.json"
    articles_json.write_text(json.dumps(scrub_for_storage(manifest.get("articles", [])), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    errors_json.write_text(json.dumps(scrub_for_storage(manifest.get("failed", [])), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_json.write_text(
        json.dumps(
            scrub_for_storage(
                {
                    "run_id": manifest.get("run_id", ""),
                    "task_id": task_id,
                    "source_mode": intent.get("mode", ""),
                    "input_digest": intent.get("input_digest", ""),
                    "output_dir": manifest.get("output_dir", str(output_dir)),
                    "started_at": manifest.get("created_at", ""),
                    "finished_at": utc_now(),
                    "success_count": manifest.get("success_count", 0),
                    "failed_count": manifest.get("failure_count", 0),
                    "skipped_count": manifest.get("skipped_count", 0),
                    "duration_ms": manifest.get("duration_ms", 0),
                    "html_fetch_count": manifest.get("html_fetch_count", 0),
                    "image_fetch_count": manifest.get("image_fetch_count", 0),
                    "retry_count": manifest.get("retry_count", 0),
                    "retry_attempt_count": manifest.get("retry_attempt_count", 0),
                    "html_concurrency": manifest.get("html_concurrency", 1),
                    "max_retries": manifest.get("max_retries", 0),
                    "gate_summary": manifest.get("gate_summary", {}),
                    "index": manifest.get("index", ""),
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"articles_json": str(articles_json), "errors_json": str(errors_json), "run_json": str(run_json)}


def record_download_manifest(base: Path, task_id: str, source_mode: str, manifest: dict[str, Any], status: str) -> None:
    init_wizard_db(base)
    now = utc_now()
    run_id = str(manifest.get("run_id") or "")
    if not run_id:
        return
    db = connect_wizard_db(base)
    try:
        db.execute(
            """
            INSERT INTO download_runs
                (run_id, task_id, source_mode, output_dir, status, success_count, failed_count, skipped_count, duration_ms, retry_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                task_id = excluded.task_id,
                source_mode = excluded.source_mode,
                output_dir = excluded.output_dir,
                status = excluded.status,
                success_count = excluded.success_count,
                failed_count = excluded.failed_count,
                skipped_count = excluded.skipped_count,
                duration_ms = excluded.duration_ms,
                retry_count = excluded.retry_count,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                task_id,
                source_mode,
                str(manifest.get("output_dir") or manifest.get("run_dir") or ""),
                status,
                int(manifest.get("success_count") or 0),
                int(manifest.get("failure_count") or 0),
                int(manifest.get("skipped_count") or 0),
                int(manifest.get("duration_ms") or 0),
                int(manifest.get("retry_count") or 0),
                now,
                now,
            ),
        )
        db.execute("DELETE FROM download_items WHERE run_id = ?", (run_id,))
        rows = list(manifest.get("articles") or []) + list(manifest.get("failed") or []) + list(manifest.get("skipped") or [])
        for item in rows:
            db.execute(
                """
                INSERT INTO download_items
                    (
                        run_id, article_id, url, status, markdown_path, image_dir, error, retries,
                        previous_run_id, previous_task_id, previous_output_dir, previous_markdown_path,
                        created_at, updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(item.get("article_id") or ""),
                    sanitize_text_urls(str(item.get("source_url") or item.get("url") or "")),
                    str(item.get("status") or ("failed" if item.get("error") else "success")),
                    str(item.get("absolute_markdown_path") or item.get("markdown_path") or ""),
                    str(item.get("absolute_image_dir") or item.get("image_dir") or ""),
                    scrub_sensitive_text(str(item.get("error") or "")),
                    int(item.get("retries") or 0),
                    str(item.get("previous_run_id") or ""),
                    str(item.get("previous_task_id") or ""),
                    str(item.get("previous_output_dir") or ""),
                    str(item.get("previous_markdown_path") or ""),
                    now,
                    now,
                ),
            )
        db.commit()
    finally:
        db.close()


def previous_success_records(base: Path, task_id: str, urls: list[str]) -> dict[str, dict[str, Any]]:
    if not urls:
        return {}
    init_wizard_db(base)
    cleaned = [sanitize_text_urls(url) for url in urls]
    placeholders = ",".join(["?"] * len(cleaned))
    db = connect_wizard_db(base)
    try:
        rows = db.execute(
            f"""
            SELECT
                di.url,
                di.markdown_path,
                di.image_dir,
                dr.run_id,
                dr.task_id,
                dr.output_dir,
                di.updated_at
            FROM download_items di
            JOIN download_runs dr ON dr.run_id = di.run_id
            WHERE di.status = 'success'
              AND di.url IN ({placeholders})
            ORDER BY
                CASE WHEN dr.task_id = ? THEN 0 ELSE 1 END,
                di.updated_at DESC
            """,
            [*cleaned, task_id],
        ).fetchall()
        records: dict[str, dict[str, Any]] = {}
        for row in rows:
            url = str(row["url"])
            if url in records:
                continue
            previous_markdown = str(row["markdown_path"] or "")
            previous_output_dir = str(row["output_dir"] or "")
            previous_markdown_path = previous_markdown
            if previous_markdown and previous_output_dir and not Path(previous_markdown).is_absolute():
                previous_markdown_path = str(Path(previous_output_dir) / previous_markdown)
            previous_markdown_exists = bool(previous_markdown_path and Path(previous_markdown_path).exists())
            if not previous_markdown_exists:
                continue
            records[url] = {
                "source_url": url,
                "previous_run_id": str(row["run_id"] or ""),
                "previous_task_id": str(row["task_id"] or ""),
                "previous_output_dir": previous_output_dir,
                "previous_markdown_path": previous_markdown_path,
                "previous_image_dir": str(row["image_dir"] or ""),
                "previous_markdown_exists": previous_markdown_exists,
            }
        return records
    finally:
        db.close()


def failed_urls_for_task(base: Path, task_id: str) -> list[str]:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        rows = db.execute(
            """
            SELECT di.url, MAX(di.updated_at) AS last_seen
            FROM download_items di
            JOIN download_runs dr ON dr.run_id = di.run_id
            WHERE dr.task_id = ?
              AND di.status = 'failed'
              AND di.url != ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM download_items ok
                  JOIN download_runs okr ON okr.run_id = ok.run_id
                  WHERE okr.task_id = dr.task_id
                    AND ok.url = di.url
                    AND ok.status = 'success'
              )
            GROUP BY di.url
            ORDER BY last_seen DESC
            """,
            (task_id,),
        ).fetchall()
        return [str(row["url"]) for row in rows]
    finally:
        db.close()


def skipped_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for index, record in enumerate(records, 1):
        rows.append(
            {
                "seq": f"skip-{index:03d}",
                "article_id": "",
                "title": "",
                "account": "",
                "source_url": sanitize_text_urls(str(record.get("source_url") or "")),
                "markdown_path": "",
                "image_dir": "",
                "image_count": 0,
                "status": "skipped",
                "error": "already downloaded previously",
                "skip_reason": "previous_success",
                "previous_run_id": str(record.get("previous_run_id") or ""),
                "previous_task_id": str(record.get("previous_task_id") or ""),
                "previous_output_dir": str(record.get("previous_output_dir") or ""),
                "previous_markdown_path": str(record.get("previous_markdown_path") or ""),
                "previous_markdown_exists": bool(record.get("previous_markdown_exists")),
            }
        )
    return rows


def json_file_is_type(path: Path, expected_type: type) -> bool:
    if not path.exists():
        return False
    parsed = read_json_text(path.read_text(encoding="utf-8"))
    return isinstance(parsed, expected_type)


def skipped_item_db_evidence_exists(base: Path, item: dict[str, Any]) -> bool:
    previous_run_id = str(item.get("previous_run_id") or "")
    source_url = sanitize_text_urls(str(item.get("source_url") or ""))
    if not previous_run_id or not source_url:
        return False
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        row = db.execute(
            """
            SELECT 1
            FROM download_items
            WHERE run_id = ?
              AND url = ?
              AND status = 'success'
            LIMIT 1
            """,
            (previous_run_id, source_url),
        ).fetchone()
        return row is not None
    finally:
        db.close()


def skipped_item_has_evidence(output_dir: Path, item: dict[str, Any], base: Path | None = None) -> bool:
    if base and skipped_item_db_evidence_exists(base, item):
        return True
    for key in ("previous_markdown_path", "markdown_path"):
        value = str(item.get(key) or "")
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = output_dir / path
        if path.exists():
            return True
    return False


def verify_run(output_dir: Path, manifest: dict[str, Any], base: Path | None = None) -> dict[str, Any]:
    index_path = Path(str(manifest.get("index") or output_dir / "index.csv"))
    run_path = output_dir / "run.json"
    articles_path = output_dir / "articles.json"
    errors_path = output_dir / "errors.json"
    markdown_files = list((output_dir / "articles").glob("*.md"))
    success_count = int(manifest.get("success_count") or 0)
    if manifest.get("profile") == "markdown-only-multi-account":
        result_indexes = [Path(str(item.get("index") or "")) for item in manifest.get("results", []) if item.get("index")]
        index_ok = all(path.exists() for path in result_indexes)
        expected_markdown = [
            Path(str(item.get("absolute_markdown_path") or ""))
            for item in list(manifest.get("articles") or [])
            if str(item.get("absolute_markdown_path") or "")
        ]
        markdown_ok = all(path.exists() for path in expected_markdown) if expected_markdown else success_count == 0
        articles_json_ok = json_file_is_type(articles_path, list)
        errors_json_ok = json_file_is_type(errors_path, list)
        skipped = list(manifest.get("skipped") or [])
        skipped_evidence_ok = all(skipped_item_has_evidence(output_dir, item, base) for item in skipped)
        ok = run_path.exists() and articles_json_ok and errors_json_ok and index_ok and markdown_ok and skipped_evidence_ok
        return {
            "ok": ok,
            "index_exists": index_ok,
            "run_json_exists": run_path.exists(),
            "articles_json_ok": articles_json_ok,
            "errors_json_ok": errors_json_ok,
            "markdown_count": len(expected_markdown),
            "expected_markdown_count": len(expected_markdown),
            "success_count": success_count,
            "skipped_count": len(skipped),
            "skipped_evidence_ok": skipped_evidence_ok,
        }
    expected_markdown = [
        output_dir / str(item.get("markdown_path") or "")
        for item in list(manifest.get("articles") or [])
        if str(item.get("markdown_path") or "")
    ]
    skipped = list(manifest.get("skipped") or [])
    markdown_ok = all(path.exists() for path in expected_markdown) if expected_markdown else success_count == 0
    articles_json_ok = json_file_is_type(articles_path, list)
    errors_json_ok = json_file_is_type(errors_path, list)
    skipped_evidence_ok = all(skipped_item_has_evidence(output_dir, item, base) for item in skipped)
    ok = index_path.exists() and run_path.exists() and articles_json_ok and errors_json_ok and markdown_ok and skipped_evidence_ok
    return {
        "ok": ok,
        "index_exists": index_path.exists(),
        "run_json_exists": run_path.exists(),
        "articles_json_ok": articles_json_ok,
        "errors_json_ok": errors_json_ok,
        "markdown_count": len(markdown_files),
        "expected_markdown_count": len(expected_markdown),
        "success_count": success_count,
        "skipped_count": len(skipped),
        "skipped_evidence_ok": skipped_evidence_ok,
    }


def run_url_mode(
    base: Path,
    task_id: str,
    intent: dict[str, Any],
    output_dir_arg: str,
    no_assets: bool,
    dry_run: bool,
    force: bool = False,
    html_concurrency: int = 1,
    max_retries: int = 0,
    force_source: str = "none",
) -> dict[str, Any]:
    run_id = make_run_id()
    groups = plan_markdown_account_groups(intent["urls"], output_dir_arg)
    if len(groups) == 1:
        out_dir = Path(str(groups[0]["output_dir"])).expanduser().resolve()
    else:
        out_dir = Path(output_dir_arg).expanduser().resolve() if output_dir_arg else DEFAULT_DELIVERY_DIR.expanduser().resolve()
    env_gate = gate_environment(base, task_id, out_dir)
    if not env_gate["ok"]:
        save_task(base, task_id, intent, env_gate["state"], "url", str(out_dir), env_gate)
        return {"ok": False, "state": env_gate["state"], "task_id": task_id, "gate": env_gate}

    previous_success = {} if force else previous_success_records(base, task_id, intent["urls"])
    urls_to_download = [url for url in intent["urls"] if sanitize_text_urls(url) not in previous_success]
    skipped = skipped_rows([previous_success[sanitize_text_urls(url)] for url in intent["urls"] if sanitize_text_urls(url) in previous_success])
    plan = {
        "selected_article_count": len(urls_to_download),
        "skipped_already_downloaded_count": len(skipped),
        "output_dir": str(out_dir),
        "download_assets": not no_assets,
        "force": force,
        "force_source": force_source if force else "none",
        "risk_level": "R4" if force else "R2",
        "html_concurrency": max(1, min(int(html_concurrency or 1), 4)),
        "max_retries": max(0, min(int(max_retries or 0), 3)),
    }
    record_gate(base, task_id, "download", "ready", True, plan)
    if dry_run:
        result = {"ok": True, "state": "ready", "task_id": task_id, "mode": "url", "download_plan": plan}
        save_task(base, task_id, intent, "ready", "url", str(out_dir), result)
        return result

    owner = f"{task_id}:{os.getpid()}"
    lock_scope_dir = Path(output_dir_arg).expanduser().resolve() if output_dir_arg else out_dir
    lock_name = "download:" + hashlib.sha256(str(lock_scope_dir).encode("utf-8")).hexdigest()[:16]
    lock = acquire_lock(base, lock_name, owner)
    if not lock["ok"]:
        event = record_gate(base, task_id, "download", "failed_recoverable", False, lock, "download output directory is locked")
        event["next_action"] = "Wait for the other download to finish, or run doctor --clear-stale-locks if the lock is stale."
        result = {"ok": False, "state": "failed_recoverable", "task_id": task_id, "mode": "url", "gate": event}
        save_task(base, task_id, intent, "failed_recoverable", "url", str(out_dir), result)
        return result

    try:
        started_monotonic = time.monotonic()
        started_at = utc_now()
        manifest = run_markdown_only_download_by_account(
            urls_to_download,
            output_dir_arg,
            not no_assets,
            {"mode": "wechat-wizard-url", "task_id": task_id, "urls": urls_to_download, "goal": intent["goal"]},
            run_id,
            plan["html_concurrency"],
            plan["max_retries"],
        ) if urls_to_download else {
            "ok": True,
            "profile": "markdown-only",
            "run_id": run_id,
            "output_dir": str(out_dir),
            "index": str(out_dir / "index.csv"),
            "success_count": 0,
            "failure_count": 0,
            "articles": [],
            "failed": [],
            "input": scrub_for_storage({"mode": "wechat-wizard-url", "task_id": task_id, "urls": [], "goal": intent["goal"]}),
        }
        manifest["created_at"] = manifest.get("created_at") or started_at
        manifest["duration_ms"] = int((time.monotonic() - started_monotonic) * 1000)
        manifest["html_fetch_count"] = len(urls_to_download)
        manifest["image_fetch_count"] = sum(int(item.get("image_count") or 0) for item in manifest.get("articles") or [])
        manifest["retry_count"] = int(intent.get("retry_count") or 0)
        manifest["html_concurrency"] = plan["html_concurrency"]
        manifest["max_retries"] = plan["max_retries"]
        manifest["retry_attempt_count"] = int(manifest.get("retry_attempt_count") or 0)
        if skipped:
            manifest["skipped"] = skipped
            manifest["skipped_count"] = len(skipped)
        else:
            manifest["skipped_count"] = 0
        if not urls_to_download:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "articles").mkdir(parents=True, exist_ok=True)
            (out_dir / "images").mkdir(parents=True, exist_ok=True)
            index_path = out_dir / "index.csv"
            if not index_path.exists():
                index_path.write_text(
                    "seq,article_id,title,account,source_url,markdown_path,image_dir,image_count,status,error\n",
                    encoding="utf-8",
                )
        extra_files = write_wizard_run_files(out_dir, task_id, intent, manifest)
        verification = verify_run(out_dir, manifest, base)
        verify_gate = record_gate(base, task_id, "verify", "done" if verification["ok"] else "failed_recoverable", verification["ok"], verification)
        manifest["gate_summary"] = gate_summary(base, task_id)
        extra_files = write_wizard_run_files(out_dir, task_id, intent, manifest)
        record_download_manifest(base, task_id, "url", manifest, "done" if verification["ok"] else "failed_recoverable")
        result = {
            "ok": bool(manifest.get("ok")) and verification["ok"],
            "state": "done" if verification["ok"] else "failed_recoverable",
            "task_id": task_id,
            "mode": "url",
            "run_id": manifest.get("run_id"),
            "output_dir": manifest.get("output_dir"),
            "index": manifest.get("index"),
            "articles_json": extra_files["articles_json"],
            "errors_json": extra_files["errors_json"],
            "run_json": extra_files["run_json"],
            "success_count": manifest.get("success_count", 0),
            "failure_count": manifest.get("failure_count", 0),
            "skipped_count": manifest.get("skipped_count", 0),
            "failed": manifest.get("failed", []),
            "verify": verify_gate,
        }
        save_task(base, task_id, intent, result["state"], "url", str(out_dir), result)
        return result
    finally:
        release_lock(base, lock_name, owner)


def compact_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": account.get("id"),
        "fakeid": account.get("fakeid"),
        "nickname": account.get("nickname"),
        "alias": account.get("alias"),
        "description": account.get("description", ""),
        "synced_count": account.get("synced_count", 0),
        "last_sync_at": account.get("last_sync_at", ""),
        "source": account.get("source", "local"),
        "score": account.get("score", 0),
        "reason": account.get("reason", ""),
    }


def compact_article(article: dict[str, Any]) -> dict[str, Any]:
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


def choose_account_candidate(candidates: list[dict[str, Any]], choice: str) -> dict[str, Any]:
    text = str(choice or "").strip()
    if not candidates:
        raise RuntimeError("no account choices are available for this task")
    if not text:
        raise RuntimeError("account choice is required")
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(candidates):
            return candidates[index]
    normalized = wechat_exporter.normalize_match_text(text)
    exact = [
        item
        for item in candidates
        if normalized
        and normalized
        in {
            wechat_exporter.normalize_match_text(item.get("id")),
            wechat_exporter.normalize_match_text(item.get("fakeid")),
            wechat_exporter.normalize_match_text(item.get("nickname")),
            wechat_exporter.normalize_match_text(item.get("alias")),
        }
    ]
    if len(exact) == 1:
        return exact[0]
    contains = [
        item
        for item in candidates
        if normalized
        and (
            normalized in wechat_exporter.normalize_match_text(item.get("nickname"))
            or normalized in wechat_exporter.normalize_match_text(item.get("alias"))
            or normalized in wechat_exporter.normalize_match_text(item.get("fakeid"))
        )
    ]
    if len(contains) == 1:
        return contains[0]
    raise RuntimeError("account choice did not match exactly one candidate")


def parse_latest_count(text: str) -> int | None:
    match = re.search(r"(?:最近|最新|latest)\s*(\d+)", text, re.I)
    return int(match.group(1)) if match else None


def choose_article_ids(articles: list[dict[str, Any]], choice: str) -> list[int]:
    text = str(choice or "").strip()
    if not articles:
        raise RuntimeError("no article choices are available for this task")
    if not text:
        raise RuntimeError("article choice is required")
    latest = parse_latest_count(text)
    if latest is not None:
        return [int(item["id"]) for item in articles[:latest]]

    selected: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(item: dict[str, Any]) -> None:
        article_id = int(item["id"])
        if article_id not in seen:
            selected.append(item)
            seen.add(article_id)

    for start_text, end_text in re.findall(r"(\d+)\s*-\s*(\d+)", text):
        start = int(start_text)
        end = int(end_text)
        if start > end:
            start, end = end, start
        for index in range(start, end + 1):
            if 1 <= index <= len(articles):
                add(articles[index - 1])
    without_ranges = re.sub(r"\d+\s*-\s*\d+", " ", text)
    for part in re.split(r"[,，\s]+", without_ranges):
        if not part:
            continue
        if part.isdigit():
            number = int(part)
            if 1 <= number <= len(articles):
                add(articles[number - 1])
                continue
            matches = [item for item in articles if int(item["id"]) == number]
            if len(matches) == 1:
                add(matches[0])
                continue
        lowered = part.lower()
        matches = [
            item
            for item in articles
            if lowered in str(item.get("title") or "").lower() or lowered in str(item.get("publish_time") or "").lower()
        ]
        for item in matches:
            add(item)
    if not selected:
        whole = text.lower()
        matches = [
            item
            for item in articles
            if whole in str(item.get("title") or "").lower() or whole in str(item.get("publish_time") or "").lower()
        ]
        for item in matches:
            add(item)
    if not selected:
        raise RuntimeError("article choice did not match any listed article")
    return [int(item["id"]) for item in selected]


def parse_iso_time(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def active_qr_login_session(base: Path, task_id: str = "") -> dict[str, Any] | None:
    directory = wechat_exporter.login_dir(base)
    if not directory.exists():
        return None
    now = dt.datetime.now(dt.timezone.utc)
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            session = wechat_exporter.read_json_file(path)
        except Exception:
            continue
        if not isinstance(session, dict):
            continue
        if task_id and str(session.get("task_id") or "") != task_id:
            continue
        status = str(session.get("status") or "")
        expires_at = parse_iso_time(str(session.get("expires_at") or ""))
        if status in {"waiting_for_scan", "scanned_waiting_confirm"} and expires_at and expires_at > now:
            return session
    return None


def qr_login_evidence(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "login_id": session.get("login_id", ""),
        "qrcode_path": session.get("qrcode_path", ""),
        "expires_at": session.get("expires_at", ""),
        "poll_after_seconds": 5,
        "base_url": session.get("base_url", ""),
        "task_id": session.get("task_id", ""),
    }


def gate_exporter_auth(base: Path, task_id: str, required: bool) -> dict[str, Any]:
    profile = wechat_exporter.get_active_profile(base)
    if profile:
        return record_gate(
            base,
            task_id,
            "auth",
            "ready",
            True,
            {"profile": profile["display_name"], "expires_at": profile["expires_at"]},
        )
    base_url = wechat_exporter.get_config(base, "base_url", wechat_exporter.DEFAULT_BASE_URL)
    qr_session = active_qr_login_session(base, task_id)
    qr_error = ""
    qr_auto_disabled = os.environ.get("MOORE_WECHAT_WIZARD_DISABLE_QR_AUTO") == "1"
    if not qr_session and not qr_auto_disabled:
        try:
            started = wechat_exporter.start_qr_login(base, base_url, open_qrcode=True)
            qr_session = {
                "login_id": started.get("login_id", ""),
                "base_url": started.get("base_url", base_url),
                "qrcode_path": started.get("qrcode_path", ""),
                "expires_at": started.get("expires_at", ""),
                "status": "waiting_for_scan",
                "task_id": task_id,
            }
            try:
                session_path = wechat_exporter.login_session_path(base, str(started.get("login_id") or ""))
                session_data = wechat_exporter.read_json_file(session_path)
                if isinstance(session_data, dict):
                    session_data["task_id"] = task_id
                    wechat_exporter.write_json_file(session_path, session_data)
            except Exception:
                pass
        except Exception as exc:
            qr_error = str(exc)
    evidence = {"base_url": base_url}
    if qr_session:
        evidence.update(qr_login_evidence(qr_session))
    if qr_auto_disabled:
        evidence["qr_auto_disabled"] = True
    if qr_error:
        evidence["qr_start_error"] = qr_error
    event = record_gate(
        base,
        task_id,
        "auth",
        "need_login",
        not required,
        evidence,
        "exporter auth-key is not configured" + (f"; QR login start failed: {qr_error}" if qr_error else ""),
    )
    event["next_action"] = "Scan the QR code, complete exporter login, then resume this task."
    return event


def resolve_exporter_account(base: Path, task_id: str, intent: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    query = str(intent.get("account_query") or "").strip()
    local = wechat_exporter.local_account_candidates(base, query, 5)
    exact = [item for item in local if int(item.get("score") or 0) >= 100]
    if len(exact) == 1:
        event = record_gate(base, task_id, "choice", "ready", True, {"source": "local", "account": compact_account(exact[0])})
        return exact[0], event
    if len(local) == 1 and int(local[0].get("score") or 0) >= 70:
        event = record_gate(base, task_id, "choice", "ready", True, {"source": "local", "account": compact_account(local[0])})
        return local[0], event
    if len(local) > 1:
        candidates = [compact_account(item) for item in local]
        save_choices(base, task_id, "account", candidates)
        event = record_gate(
            base,
            task_id,
            "choice",
            "need_account_choice",
            False,
            {"source": "local", "candidates": candidates},
        )
        event["next_action"] = "Choose one account candidate by index, nickname, alias, or fakeid."
        return None, event

    auth_gate = gate_exporter_auth(base, task_id, required=True)
    if not auth_gate["ok"]:
        return None, auth_gate

    try:
        remote = wechat_exporter.search_accounts(base, query, 0, 5).get("accounts", [])
    except Exception as exc:
        event = record_gate(base, task_id, "choice", "need_login", False, {"query": query}, str(exc))
        event["next_action"] = "Refresh exporter login and resume this task."
        return None, event

    exact_remote = [
        item
        for item in remote
        if str(item.get("nickname") or "").strip() == query or str(item.get("alias") or "").strip() == query
    ]
    if len(exact_remote) == 1:
        account = wechat_exporter.upsert_account(base, exact_remote[0])["account"]
        event = record_gate(base, task_id, "choice", "ready", True, {"source": "remote", "account": compact_account(account)})
        return account, event
    if remote:
        candidates = [compact_account(item) for item in remote]
        save_choices(base, task_id, "account", candidates)
        event = record_gate(
            base,
            task_id,
            "choice",
            "need_account_choice",
            False,
            {"source": "remote", "candidates": candidates},
        )
        event["next_action"] = "Choose one account candidate by index, nickname, alias, or fakeid."
        return None, event

    event = record_gate(base, task_id, "choice", "blocked", False, {"query": query}, "No exporter account candidates found")
    event["next_action"] = "Check the account name or provide one sample article URL."
    return None, event


def run_exporter_mode(base: Path, task_id: str, intent: dict[str, Any], output_dir_arg: str, no_assets: bool, dry_run: bool) -> dict[str, Any]:
    account, choice_gate = resolve_exporter_account(base, task_id, intent)
    if not account:
        result = {
            "ok": choice_gate["state"] in {"need_account_choice"},
            "state": choice_gate["state"],
            "task_id": task_id,
            "mode": "exporter",
            "gate": choice_gate,
        }
        save_task(base, task_id, intent, choice_gate["state"], "exporter", result=result)
        return result

    account_id = int(account["id"])
    force_sync = bool(intent["signals"].get("has_sync_words"))
    stale = not wechat_exporter.is_synced_today(account.get("last_sync_at"))
    latest = intent.get("latest") or 50
    sync_required = force_sync or stale
    if sync_required:
        auth_gate = gate_exporter_auth(base, task_id, required=True)
        if not auth_gate["ok"]:
            result = {
                "ok": False,
                "state": "need_login",
                "task_id": task_id,
                "mode": "exporter",
                "account": compact_account(account),
                "gate": auth_gate,
            }
            save_task(base, task_id, intent, "need_login", "exporter", result=result)
            return result
        sync_result = wechat_exporter.sync_account_articles(base, account_id, max(int(latest), 20))
        if not sync_result.get("ok"):
            event = record_gate(base, task_id, "sync", "failed_recoverable", False, sync_result, "; ".join(sync_result.get("errors") or []))
            result = {"ok": False, "state": "failed_recoverable", "task_id": task_id, "mode": "exporter", "gate": event}
            save_task(base, task_id, intent, "failed_recoverable", "exporter", result=result)
            return result
        record_gate(base, task_id, "sync", "ready", True, sync_result)
    else:
        record_gate(base, task_id, "sync", "ready", True, {"used_cache": True, "last_sync_at": account.get("last_sync_at", "")})

    rows = wechat_exporter.list_articles(base, account_id=account_id, limit=int(latest))
    articles = [compact_article(item) for item in rows]
    if intent["action"] == "list" or intent.get("requires_user_choice"):
        save_choices(base, task_id, "article", articles)
        result = {
            "ok": True,
            "state": "need_article_choice",
            "task_id": task_id,
            "mode": "exporter",
            "account": compact_account(dict(wechat_exporter.get_account_row(base, account_id=account_id))),
            "count": len(articles),
            "articles": articles,
            "next_step": "Choose articles by range, comma-separated ids, title keyword, or ask for latest N download.",
        }
        record_gate(base, task_id, "choice", "need_article_choice", True, {"count": len(articles)})
        save_task(base, task_id, intent, "need_article_choice", "exporter", result=result)
        return result

    selected_ids = [int(item["id"]) for item in articles]
    record_gate(
        base,
        task_id,
        "download",
        "ready",
        True,
        {"selected_article_count": len(selected_ids), "output_dir": output_dir_arg or str(DEFAULT_DELIVERY_DIR), "download_assets": not no_assets},
    )
    if dry_run:
        download_plan = {"selected_article_count": len(selected_ids), "output_dir": output_dir_arg or str(DEFAULT_DELIVERY_DIR), "download_assets": not no_assets}
        result = {
            "ok": True,
            "state": "ready",
            "task_id": task_id,
            "mode": "exporter",
            "account": compact_account(account),
            "selected_article_ids": selected_ids,
            "download_plan": download_plan,
        }
        save_task(base, task_id, intent, "ready", "exporter", result=result)
        return result

    if intent.get("requires_engagement"):
        engagement = wechat_exporter.sync_engagement_for_articles(base, account_id, selected_ids, output_root=output_dir_arg)
        state = "waiting_wechat_collection" if engagement.get("status") == "waiting_credential" else ("done" if engagement.get("ok") else "failed_recoverable")
        result = {
            "ok": bool(engagement.get("ok") or engagement.get("status") == "waiting_credential"),
            "state": state,
            "task_id": task_id,
            "mode": "exporter",
            "account": compact_account(dict(wechat_exporter.get_account_row(base, account_id=account_id))),
            "selected_article_ids": selected_ids,
            "download": None,
            "engagement": engagement,
            "flow": "exporter-sync -> engagement batch download",
        }
        if engagement.get("status") == "waiting_credential":
            result["next_step"] = engagement.get("next_step") or "已复制代表文章链接，请粘贴到微信客户端并打开；打开后回复“已打开”。"
        record_gate(base, task_id, "engagement", state, bool(result["ok"]), engagement)
        save_task(base, task_id, intent, state, "exporter", output_dir_arg, result)
        return result

    nickname = account.get("nickname", "") if not output_dir_arg else ""
    downloaded = wechat_exporter.download_articles(
        base,
        selected_ids,
        output_dir_arg,
        no_assets,
        nickname,
        force=True,
    )
    result = {
        "ok": downloaded.get("ok", False),
        "state": "done" if downloaded.get("ok") else "failed_recoverable",
        "task_id": task_id,
        "mode": "exporter",
        "account": compact_account(dict(wechat_exporter.get_account_row(base, account_id=account_id))),
        "selected_article_ids": selected_ids,
        "download": downloaded,
    }
    record_gate(base, task_id, "verify", result["state"], bool(downloaded.get("ok")), downloaded)
    save_task(base, task_id, intent, result["state"], "exporter", downloaded.get("output_dir", ""), result)
    return result


def biz_from_url(url: str) -> str:
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    return str((values.get("__biz") or [""])[0]).strip()


def create_history_session_from_url_clue(base: Path, sample_url: str) -> dict[str, Any] | None:
    biz = biz_from_url(sample_url)
    if not biz:
        return None
    session_id = make_run_id()
    account_name = "unknown-account"
    account_dir = history_account_dir(base, biz, account_name)
    account_dir.mkdir(parents=True, exist_ok=True)
    expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
    session = {
        "ok": True,
        "session_id": session_id,
        "mode": "account-history",
        "status": "waiting_for_wechat",
        "context_ready": False,
        "created_at": utc_now(),
        "expires_at": expires_at,
        "sample_url": sample_url,
        "account_id": biz,
        "account_name": account_name,
        "biz": biz,
        "account_dir": str(account_dir),
        "source_article": str(account_dir / "source_article.json"),
        "history_csv": str(account_dir / "history_articles.csv"),
        "history_json": str(account_dir / "history_articles.json"),
        "selected_csv": str(account_dir / "selected_articles.csv"),
        "wechat_desktop_step": [
            "Send the legacy profile_ext link to File Transfer in WeChat.",
            "Open it with the WeChat desktop built-in browser.",
            "Confirm proxy setup before routing WeChat traffic through mitmproxy.",
        ],
    }
    save_history_session(base, session)
    return session


def run_history_mode(base: Path, task_id: str, intent: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    sample_url = intent["urls"][0]
    try:
        history_env_gate = gate_history_environment(base, task_id)
        if dry_run:
            session = create_history_session_from_url_clue(base, sample_url)
            if not session:
                event = record_gate(
                    base,
                    task_id,
                    "history",
                    "failed_recoverable",
                    False,
                    {"sample_url": sample_url},
                    "dry-run history mode requires a sample URL carrying __biz",
                )
                event["next_action"] = "Run without --dry-run so the wizard can fetch the article HTML and extract __biz."
                result = {"ok": False, "state": "failed_recoverable", "task_id": task_id, "mode": "history", "gate": event}
                save_task(base, task_id, intent, "failed_recoverable", "history", result=result)
                return result
            open_url, open_url_type = build_history_open_url(session)
            open_result = {
                "ok": True,
                "session_id": session["session_id"],
                "account_name": session.get("account_name", ""),
                "open_url": open_url,
                "open_url_type": open_url_type,
                "copied_to_clipboard": False,
                "clipboard_method": "",
                "clipboard_error": "",
            }
        else:
            session = start_history_session(sample_url, base)
            open_result = open_history_link(base, session["session_id"], True)

        proxy_gate = record_gate(
            base,
            task_id,
            "proxy",
            "need_proxy_confirm",
            False,
            {
                "session_id": open_result["session_id"],
                "mitmdump_available": bool(history_env_gate["evidence"].get("mitmdump_available")),
                "mitmdump_path": str(history_env_gate["evidence"].get("mitmdump_path") or ""),
                "proxy_port": DEFAULT_PROXY_PORT,
                "open_url_type": open_result.get("open_url_type", ""),
            },
        )
        if history_env_gate["evidence"].get("mitmdump_available"):
            proxy_gate["next_action"] = "Confirm before enabling the local proxy, then open the legacy profile_ext URL in WeChat desktop."
        else:
            proxy_gate["next_action"] = "Install mitmdump, then confirm before enabling the local proxy and opening the legacy profile_ext URL in WeChat desktop."
        result = {
            "ok": True,
            "state": "need_proxy_confirm",
            "task_id": task_id,
            "mode": "history",
            "session_id": open_result["session_id"],
            "open_url": open_result["open_url"],
            "open_url_type": open_result.get("open_url_type", ""),
            "copied_to_clipboard": open_result.get("copied_to_clipboard", False),
            "account_name": open_result.get("account_name", ""),
            "gate": proxy_gate,
            "environment_gate": history_env_gate,
            "next_step": "Open the profile_ext history URL in WeChat desktop, then confirm proxy setup before capture.",
        }
        save_task(base, task_id, intent, "need_proxy_confirm", "history", result=result)
        return result
    except Exception as exc:
        event = record_gate(base, task_id, "history", "failed_recoverable", False, {"sample_url": sample_url}, str(exc))
        event["next_action"] = "Use a sample article whose HTML exposes __biz, or open the article once through WeChat desktop and retry."
        result = {"ok": False, "state": "failed_recoverable", "task_id": task_id, "mode": "history", "gate": event}
        save_task(base, task_id, intent, "failed_recoverable", "history", result=result)
        return result


def login_id_for_task(base: Path, task_id: str, explicit_login_id: str = "") -> str:
    if explicit_login_id:
        return explicit_login_id
    task = load_task(base, task_id)
    result = task.get("result") or {}
    gate = result.get("gate") or {}
    evidence = gate.get("evidence") or {}
    login_id = str(evidence.get("login_id") or "")
    if login_id:
        return login_id
    session = active_qr_login_session(base, task_id)
    if session and session.get("login_id"):
        return str(session["login_id"])
    raise RuntimeError("login_id not found for this task; pass --login-id explicitly if the QR session was started outside the wizard")


def exporter_download_manifest(base: Path, article_ids: list[int], downloaded: dict[str, Any]) -> dict[str, Any]:
    pairs = wechat_exporter.get_article_urls(base, article_ids)
    failed = list(downloaded.get("failed") or [])
    failed_urls = {str(item.get("source_url") or item.get("url") or "") for item in failed}
    articles = [
        {"article_id": str(article_id), "source_url": url, "status": "success", "markdown_path": "", "image_dir": ""}
        for article_id, url in pairs
        if url not in failed_urls
    ]
    return {
        "run_id": downloaded.get("run_id", ""),
        "output_dir": downloaded.get("output_dir", ""),
        "success_count": downloaded.get("success_count", 0),
        "failure_count": downloaded.get("failure_count", 0),
        "articles": articles,
        "failed": failed,
    }


def resume_account_choice(
    base: Path,
    task_id: str,
    task: dict[str, Any],
    choice: str,
    output_dir: str,
    no_assets: bool,
    dry_run: bool,
) -> dict[str, Any]:
    candidates = load_choices(base, task_id, "account")
    selected = choose_account_candidate(candidates, choice)
    if not selected.get("id"):
        selected = wechat_exporter.upsert_account(base, selected)["account"]
    mark_choices_selected(base, task_id, "account", [compact_account(selected)])
    intent = dict(task["intent"])
    intent["account_query"] = str(selected.get("fakeid") or selected.get("nickname") or selected.get("alias") or "")
    result = run_exporter_mode(base, task_id, intent, output_dir, no_assets, dry_run)
    result["resumed_from"] = "need_account_choice"
    save_task(base, task_id, intent, str(result.get("state") or "ready"), "exporter", str(result.get("output_dir") or ""), result)
    return result


def resume_article_choice(
    base: Path,
    task_id: str,
    task: dict[str, Any],
    choice: str,
    output_dir: str,
    no_assets: bool,
    dry_run: bool,
) -> dict[str, Any]:
    articles = load_choices(base, task_id, "article")
    if not articles:
        articles = list((task.get("result") or {}).get("articles") or [])
    selected_ids = choose_article_ids(articles, choice)
    result_json = task.get("result") or {}
    account = result_json.get("account") or {}
    account_id = int(account.get("id") or 0)
    if account_id:
        wechat_exporter.ensure_article_ids_belong_to_account(base, selected_ids, account_id)
    selected_articles = [item for item in articles if int(item.get("id") or 0) in set(selected_ids)]
    mark_choices_selected(base, task_id, "article", selected_articles)
    record_gate(
        base,
        task_id,
        "download",
        "ready",
        True,
        {"selected_article_count": len(selected_ids), "output_dir": output_dir or str(DEFAULT_DELIVERY_DIR), "download_assets": not no_assets},
    )
    if dry_run:
        download_plan = {"selected_article_count": len(selected_ids), "output_dir": output_dir or str(DEFAULT_DELIVERY_DIR), "download_assets": not no_assets}
        result = {
            "ok": True,
            "state": "ready",
            "task_id": task_id,
            "mode": "exporter",
            "resumed_from": "need_article_choice",
            "account": account,
            "selected_article_ids": selected_ids,
            "download_plan": download_plan,
        }
        save_task(base, task_id, task["intent"], "ready", "exporter", result=result)
        return result

    if (task.get("intent") or {}).get("requires_engagement"):
        engagement = wechat_exporter.sync_engagement_for_articles(base, account_id, selected_ids, output_root=output_dir)
        state = "waiting_wechat_collection" if engagement.get("status") == "waiting_credential" else ("done" if engagement.get("ok") else "failed_recoverable")
        result = {
            "ok": bool(engagement.get("ok") or engagement.get("status") == "waiting_credential"),
            "state": state,
            "task_id": task_id,
            "mode": "exporter",
            "resumed_from": "need_article_choice",
            "account": account,
            "selected_article_ids": selected_ids,
            "download": None,
            "engagement": engagement,
            "flow": "exporter-sync -> engagement batch download",
        }
        if engagement.get("status") == "waiting_credential":
            result["next_step"] = engagement.get("next_step") or "已复制代表文章链接，请粘贴到微信客户端并打开；打开后回复“已打开”。"
        record_gate(base, task_id, "engagement", state, bool(result["ok"]), engagement)
        save_task(base, task_id, task["intent"], state, "exporter", output_dir, result)
        return result

    nickname = str(account.get("nickname", "")) if not output_dir else ""
    downloaded = wechat_exporter.download_articles(
        base,
        selected_ids,
        output_dir,
        no_assets,
        nickname,
        force=True,
    )
    state = "done" if downloaded.get("ok") else "failed_recoverable"
    manifest = exporter_download_manifest(base, selected_ids, downloaded)
    record_download_manifest(base, task_id, "exporter", manifest, state)
    result = {
        "ok": downloaded.get("ok", False),
        "state": state,
        "task_id": task_id,
        "mode": "exporter",
        "resumed_from": "need_article_choice",
        "account": account,
        "selected_article_ids": selected_ids,
        "download": downloaded,
    }
    record_gate(base, task_id, "verify", state, bool(downloaded.get("ok")), downloaded)
    save_task(base, task_id, task["intent"], state, "exporter", downloaded.get("output_dir", ""), result)
    return result


def resume_wechat_collection(base: Path, task_id: str, task: dict[str, Any]) -> dict[str, Any]:
    previous = dict(task.get("result") or {})
    engagement = dict(previous.get("engagement") or {})
    run_id = str(engagement.get("run_id") or "")
    biz = str((engagement.get("biz") or "") or "")
    if not biz and run_id:
        run, _contexts = wechat_exporter.contexts_for_engagement_run(base, run_id)
        if run:
            scope = wechat_exporter.load_json_text(str(run.get("scope_json") or "{}")) or {}
            biz = str(scope.get("biz") or "")
    resumed = wechat_exporter.resume_waiting_engagement_runs(base, biz, run_id)
    state = "done" if resumed.get("ok") else "waiting_wechat_collection"
    result = {
        **previous,
        "ok": bool(resumed.get("ok")),
        "state": state,
        "task_id": task_id,
        "mode": "exporter",
        "resumed_from": "waiting_wechat_collection",
        "engagement_resume": resumed,
    }
    if not resumed.get("ok"):
        result["next_step"] = "请确认代表文章已在微信客户端打开并加载评论区，然后再次恢复本任务。"
    save_task(base, task_id, dict(task["intent"]), state, "exporter", str(previous.get("output_dir") or ""), result)
    return result


def resume_task(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    task = load_task(base, args.task_id)
    state = str(task.get("state") or "")
    options = resolve_download_options(base, args)
    try:
        if state == "need_account_choice":
            result = resume_account_choice(base, args.task_id, task, args.choice, options["output_dir"], options["no_assets"], args.dry_run)
        elif state == "need_article_choice":
            result = resume_article_choice(base, args.task_id, task, args.choice, options["output_dir"], options["no_assets"], args.dry_run)
        elif state == "need_login":
            result = run_exporter_mode(base, args.task_id, dict(task["intent"]), options["output_dir"], options["no_assets"], args.dry_run)
            result["resumed_from"] = "need_login"
            save_task(base, args.task_id, dict(task["intent"]), str(result.get("state") or "need_login"), "exporter", str(result.get("output_dir") or ""), result)
        elif state == "waiting_wechat_collection":
            result = resume_wechat_collection(base, args.task_id, task)
        else:
            result = {
                "ok": False,
                "state": "blocked",
                "task_id": args.task_id,
                "error": f"task state does not support resume: {state}",
            }
    except (RuntimeError, ValueError) as exc:
        choice_type = "account" if state == "need_account_choice" else ("article" if state == "need_article_choice" else "")
        choices = load_choices(base, args.task_id, choice_type) if choice_type else []
        result = {
            "ok": False,
            "state": state if state in {"need_account_choice", "need_article_choice"} else "failed_recoverable",
            "task_id": args.task_id,
            "error": scrub_sensitive_text(str(exc)),
            "choices": choices[:20],
            "next_step": "Choose again by index, range, id, title keyword, or date.",
        }
    write_json_response(result)
    return 0 if result.get("ok") else 1


def login_status(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    login_id = login_id_for_task(base, args.task_id, args.login_id)
    result = wechat_exporter.qr_login_status(base, login_id)
    ready = bool(result.get("ready_to_complete"))
    status = str(result.get("status") or "need_login")
    terminal_bad = status in {"expired", "unknown", "account_not_bound_email"}
    gate_state = "ready" if ready else ("need_login" if not terminal_bad else status)
    event = record_gate(base, args.task_id, "auth", gate_state, ready, result, "" if not terminal_bad else status)
    payload = {
        "ok": ready,
        "state": gate_state,
        "task_id": args.task_id,
        "login": result,
        "gate": event,
        "next_step": "Run login-complete after WeChat confirms the QR login." if not ready and not terminal_bad else "",
    }
    write_json_response(payload)
    return 0 if ready else 1


def login_complete(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    options = resolve_download_options(base, args)
    login_id = login_id_for_task(base, args.task_id, args.login_id)
    completed = wechat_exporter.complete_qr_login(base, login_id, args.profile)
    record_gate(base, args.task_id, "auth", "ready", True, {"login_id": login_id, "profile_id": completed.get("profile_id")})
    task = load_task(base, args.task_id)
    resumed = run_exporter_mode(base, args.task_id, dict(task["intent"]), options["output_dir"], options["no_assets"], args.dry_run)
    resumed["login"] = completed
    resumed["resumed_from"] = "need_login"
    save_task(base, args.task_id, dict(task["intent"]), str(resumed.get("state") or "ready"), "exporter", str(resumed.get("output_dir") or ""), resumed)
    write_json_response(resumed)
    return 0 if resumed.get("ok") else 1


def retry_failed(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    options = resolve_download_options(base, args)
    task = load_task(base, args.task_id)
    urls = failed_urls_for_task(base, args.task_id)
    if not urls:
        result = {
            "ok": True,
            "state": "done",
            "task_id": args.task_id,
            "mode": "url",
            "retried_count": 0,
            "message": "No failed URL items remain for this task.",
        }
        write_json_response(result)
        return 0
    intent = dict(task.get("intent") or {})
    intent["mode"] = "url"
    intent["action"] = "download"
    intent["urls"] = urls
    intent["goal"] = "retry failed WeChat article downloads"
    intent["input_digest"] = goal_digest("\n".join(urls))
    intent["retry_count"] = int(task.get("result", {}).get("retried_count") or 0) + 1
    result = run_url_mode(
        base,
        args.task_id,
        intent,
        options["output_dir"],
        options["no_assets"],
        args.dry_run,
        True,
        options["html_concurrency"],
        options["max_retries"],
        "retry",
    )
    result["retried_count"] = len(urls)
    if result.get("run_id"):
        db = connect_wizard_db(base)
        try:
            db.execute("UPDATE download_items SET retries = retries + 1 WHERE run_id = ?", (str(result["run_id"]),))
            db.commit()
        finally:
            db.close()
    save_task(base, args.task_id, intent, str(result.get("state") or "failed_recoverable"), "url", str(result.get("output_dir") or ""), result)
    write_json_response(result)
    return 0 if result.get("ok") else 1


def run_goal(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    init_wizard_db(base)
    options = resolve_download_options(base, args)
    task_id = args.task_id or new_task_id(args.goal)
    intent = parse_intent(args.goal)
    save_task(base, task_id, intent, "new", intent.get("mode", ""))
    input_gate = gate_input(base, task_id, intent)
    if not input_gate["ok"]:
        result = {"ok": False, "state": input_gate["state"], "task_id": task_id, "gate": input_gate}
        save_task(base, task_id, intent, input_gate["state"], intent.get("mode", ""), result=result)
        write_json_response(result)
        return 2

    mode_decision = decide_mode(intent)
    supported_mode = mode_decision["mode"] in {"url", "exporter", "history"} and float(mode_decision.get("mode_confidence") or 0) >= MODE_CONFIDENCE_THRESHOLD
    mode_gate = record_gate(base, task_id, "mode_decision", "ready" if supported_mode else "need_mode_choice", supported_mode, mode_decision)
    if mode_decision["mode"] == "exporter" and supported_mode:
        result = run_exporter_mode(base, task_id, intent, options["output_dir"], options["no_assets"], args.dry_run)
        write_json_response(result)
        return 0 if result.get("ok") else 1
    if mode_decision["mode"] == "history" and supported_mode:
        result = run_history_mode(base, task_id, intent, args.dry_run)
        write_json_response(result)
        return 0 if result.get("ok") else 1
    if mode_decision["mode"] != "url" or not supported_mode:
        result = {
            "ok": True,
            "state": "need_mode_choice",
            "task_id": task_id,
            "mode_decision": mode_decision,
            "gate": mode_gate,
            "intent": intent,
            "next_step": "Clarify whether this is URL download, account history capture, or exporter account sync.",
        }
        save_task(base, task_id, intent, "need_mode_choice", mode_decision["mode"], result=result)
        write_json_response(result)
        return 0

    result = run_url_mode(
        base,
        task_id,
        intent,
        options["output_dir"],
        options["no_assets"],
        args.dry_run,
        options["force"],
        options["html_concurrency"],
        options["max_retries"],
        options["force_source"],
    )
    write_json_response(result)
    return 0 if result.get("ok") else 1


def latest_download_run(base: Path) -> dict[str, Any] | None:
    init_wizard_db(base)
    db = connect_wizard_db(base)
    try:
        row = db.execute(
            """
            SELECT *
            FROM download_runs
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def check_latest_run_manifest(base: Path) -> dict[str, Any]:
    run = latest_download_run(base)
    if not run:
        return {"ok": True, "state": "no_runs"}
    output_dir = Path(str(run.get("output_dir") or "")).expanduser()
    run_json = output_dir / "run.json"
    index_csv = output_dir / "index.csv"
    articles_json = output_dir / "articles.json"
    errors_json = output_dir / "errors.json"
    parsed_run = read_json_text(run_json.read_text(encoding="utf-8")) if run_json.exists() else None
    files = {
        "output_dir": output_dir.exists(),
        "run_json": run_json.exists() and isinstance(parsed_run, dict),
        "index_csv": index_csv.exists(),
        "articles_json": articles_json.exists(),
        "errors_json": errors_json.exists(),
    }
    run_id_matches = isinstance(parsed_run, dict) and str(parsed_run.get("run_id") or "") == str(run.get("run_id") or "")
    ok = all(files.values()) and run_id_matches
    return {
        "ok": ok,
        "state": "complete" if ok else "incomplete",
        "run_id": run.get("run_id", ""),
        "task_id": run.get("task_id", ""),
        "output_dir": str(output_dir),
        "files": files,
        "run_id_matches": run_id_matches,
    }


def smoke_history_preflight(base: Path, sample_url: str) -> dict[str, Any]:
    url = sample_url or "https://mp.weixin.qq.com/s/offline-smoke?__biz=MzIxNTA1MDEwNg=="
    task_id = "smoke_history_" + hashlib.sha256(f"{url}-{utc_now()}".encode("utf-8")).hexdigest()[:12]
    intent = parse_intent(f"历史文章列表下载：{url}")
    decide_mode(intent)
    save_task(base, task_id, intent, "new", "history")
    result = run_history_mode(base, task_id, intent, True)
    ok = bool(result.get("ok")) and result.get("state") == "need_proxy_confirm" and "profile_ext" in str(result.get("open_url") or "")
    status = "need_proxy_confirm" if ok else str(result.get("state") or "failed")
    return record_smoke(
        base,
        "history-preflight",
        ok,
        status,
        {
            "task_id": task_id,
            "session_id": result.get("session_id", ""),
            "open_url_type": result.get("open_url_type", ""),
            "gate": result.get("gate", {}),
        },
        "" if ok else json_dumps(result),
    )


def smoke_qr_session(base: Path, task_id: str) -> dict[str, Any]:
    session = active_qr_login_session(base, task_id)
    if not session:
        return record_smoke(base, "qr-session", False, "missing", {"task_id": task_id}, "No active task-bound QR session found")
    qrcode_path = Path(str(session.get("qrcode_path") or "")).expanduser()
    ok = qrcode_path.exists() and qrcode_path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    return record_smoke(
        base,
        "qr-session",
        ok,
        "ready" if ok else "invalid_qrcode",
        qr_login_evidence(session) | {"qrcode_exists": qrcode_path.exists(), "qrcode_suffix": qrcode_path.suffix},
        "" if ok else "QR session exists but qrcode file is missing or has a non-image suffix",
    )


def smoke_latest_run(base: Path) -> dict[str, Any]:
    evidence = check_latest_run_manifest(base)
    return record_smoke(base, "latest-run-manifest", bool(evidence.get("ok")), str(evidence.get("state") or "unknown"), evidence)


def smoke_url_fixture(base: Path) -> dict[str, Any]:
    fixture_dir = SCRIPT_DIR.parent / "evals" / "fixtures" / "html"
    if not fixture_dir.exists():
        return record_smoke(base, "url-fixture", False, "missing_fixture", {"fixture_dir": str(fixture_dir)}, "Offline HTML fixtures are missing")
    task_id = "smoke_url_" + hashlib.sha256(utc_now().encode("utf-8")).hexdigest()[:12]
    output_dir = base / "smoke" / "url-fixture" / task_id
    url = "https://mp.weixin.qq.com/s/demo-url-article-1"
    intent = parse_intent(f"下载这篇公众号文章：{url}")
    decide_mode(intent)
    save_task(base, task_id, intent, "new", "url")
    original_fixture_dir = os.environ.get("MOORE_WECHAT_HTML_FIXTURE_DIR")
    os.environ["MOORE_WECHAT_HTML_FIXTURE_DIR"] = str(fixture_dir)
    try:
        result = run_url_mode(base, task_id, intent, str(output_dir), True, False)
    finally:
        if original_fixture_dir is None:
            os.environ.pop("MOORE_WECHAT_HTML_FIXTURE_DIR", None)
        else:
            os.environ["MOORE_WECHAT_HTML_FIXTURE_DIR"] = original_fixture_dir
    ok = bool(result.get("ok")) and result.get("state") == "done" and int(result.get("success_count") or 0) == 1
    return record_smoke(
        base,
        "url-fixture",
        ok,
        "done" if ok else str(result.get("state") or "failed"),
        {
            "task_id": task_id,
            "run_id": result.get("run_id", ""),
            "output_dir": result.get("output_dir", ""),
            "index": result.get("index", ""),
            "run_json": result.get("run_json", ""),
            "success_count": result.get("success_count", 0),
            "failure_count": result.get("failure_count", 0),
        },
        "" if ok else json_dumps(result),
    )


def smoke_all_offline(base: Path) -> dict[str, Any]:
    checks = [smoke_url_fixture(base), smoke_history_preflight(base, ""), smoke_latest_run(base)]
    ok = all(item["ok"] for item in checks)
    return record_smoke(base, "all-offline", ok, "done" if ok else "partial", {"checks": checks})


def smoke(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    if args.smoke_name == "history-preflight":
        result = smoke_history_preflight(base, args.url)
    elif args.smoke_name == "url-fixture":
        result = smoke_url_fixture(base)
    elif args.smoke_name == "qr-session":
        result = smoke_qr_session(base, args.task_id)
    elif args.smoke_name == "latest-run":
        result = smoke_latest_run(base)
    elif args.smoke_name == "all-offline":
        result = smoke_all_offline(base)
    elif args.smoke_name == "list":
        result = {"ok": True, "smoke_runs": latest_smoke_runs(base, args.limit)}
    else:
        result = {"ok": False, "status": "blocked", "error": f"unknown smoke: {args.smoke_name}"}
    write_json_response(result)
    return 0 if result.get("ok") else 1


def proxy_endpoint(proxy: dict[str, Any]) -> str:
    if not proxy.get("enabled_bool"):
        return ""
    server = str(proxy.get("server") or "").strip()
    port = str(proxy.get("port") or "").strip()
    if not server or not port or port == "0":
        return ""
    return f"{server}:{port}"


def normalize_proxy_endpoint(value: str) -> str:
    if ":" not in value:
        return value
    host, port = value.rsplit(":", 1)
    if host == "localhost":
        host = "127.0.0.1"
    return f"{host}:{port}"


def load_system_proxy_restore_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = read_json_text(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def saved_proxy_state_matches(saved: dict[str, Any], service: str, endpoint: str) -> bool:
    new_state = saved.get("new") if isinstance(saved.get("new"), dict) else {}
    previous = saved.get("previous") if isinstance(saved.get("previous"), dict) else {}
    previous_web = previous.get("web") if isinstance(previous.get("web"), dict) else None
    previous_secure = previous.get("secure_web") if isinstance(previous.get("secure_web"), dict) else None
    host = str(new_state.get("host") or "").strip()
    port = str(new_state.get("port") or "").strip()
    saved_service = str(saved.get("service") or "").strip()
    if previous_web is None or previous_secure is None:
        return False
    if not host or not port or port == "0" or not saved_service:
        return False
    return saved_service == service and normalize_proxy_endpoint(f"{host}:{port}") == normalize_proxy_endpoint(endpoint)


def current_project_proxy_ports(base: Path, default_port: int = DEFAULT_PROXY_PORT) -> list[int]:
    ports: list[int] = []
    try:
        if default_port:
            ports.append(int(default_port))
    except (TypeError, ValueError):
        pass
    try:
        active = active_proxy_port(base)
        if active:
            ports.append(int(active))
    except Exception:
        pass
    deduped: list[int] = []
    for item in ports:
        if item not in deduped:
            deduped.append(item)
    return deduped


def check_system_proxy_recoverability(base: Path, port: int = DEFAULT_PROXY_PORT) -> dict[str, Any]:
    state_path = system_proxy_state_path(base)
    saved_state_exists = state_path.exists()
    saved_state = load_system_proxy_restore_state(state_path)
    if sys.platform != "darwin":
        return {
            "ok": True,
            "platform": sys.platform,
            "supported": False,
            "saved_state_exists": saved_state_exists,
            "state_path": str(state_path),
        }
    try:
        service = choose_network_service("")
        state = get_network_proxy_state(service)
    except Exception as exc:
        return {
            "ok": False,
            "platform": sys.platform,
            "supported": True,
            "saved_state_exists": saved_state_exists,
            "state_path": str(state_path),
            "error": scrub_sensitive_text(str(exc)),
            "next_action": "Check macOS networksetup permissions or restore proxy settings manually.",
        }

    web_endpoint = proxy_endpoint(state.get("web", {}))
    secure_endpoint = proxy_endpoint(state.get("secure_web", {}))
    monitored_ports = current_project_proxy_ports(base, port)
    monitored_endpoints = {
        endpoint
        for item in monitored_ports
        for endpoint in (f"127.0.0.1:{item}", f"localhost:{item}")
    }
    points_to_history_proxy = web_endpoint in monitored_endpoints or secure_endpoint in monitored_endpoints
    matching_endpoint = web_endpoint if web_endpoint in monitored_endpoints else secure_endpoint
    saved_state_matches = bool(points_to_history_proxy and saved_proxy_state_matches(saved_state, service, matching_endpoint))
    ok = not points_to_history_proxy or saved_state_matches
    recoverability = "not_needed"
    if points_to_history_proxy and saved_state_matches:
        recoverability = "saved_state"
    elif points_to_history_proxy:
        recoverability = "unknown"
    next_action = ""
    if points_to_history_proxy and saved_state_matches:
        next_action = "Run history-proxy-disable --yes to restore the saved system proxy state."
    elif points_to_history_proxy:
        next_action = "System proxy points to the history proxy port, but no matching saved state was found. Confirm manually before changing proxy settings."
    return {
        "ok": ok,
        "platform": sys.platform,
        "supported": True,
        "service": service,
        "web_proxy": web_endpoint,
        "secure_web_proxy": secure_endpoint,
        "points_to_history_proxy": points_to_history_proxy,
        "monitored_ports": monitored_ports,
        "saved_state_exists": saved_state_exists,
        "saved_state_matches": saved_state_matches,
        "recoverability": recoverability,
        "state_path": str(state_path),
        "next_action": next_action,
    }


def doctor(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    checks: dict[str, Any] = {}
    try:
        checks["sqlite"] = init_wizard_db(base)
    except Exception as exc:
        checks["sqlite"] = {"ok": False, "error": str(exc)}
    default_dir = DEFAULT_DELIVERY_DIR.expanduser()
    try:
        default_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".wizard-doctor-", dir=str(default_dir), delete=True) as fh:
            fh.write(b"ok")
        checks["output_dir"] = {"ok": True, "path": str(default_dir)}
    except Exception as exc:
        checks["output_dir"] = {"ok": False, "path": str(default_dir), "error": str(exc)}
    checks["keychain"] = {"ok": keychain_available()}
    db_path = wizard_db_path(base)
    checks["wizard_db"] = {"ok": db_path.exists(), "path": str(db_path)}
    mitmdump_path = shutil.which("mitmdump")
    checks["mitmdump"] = {
        "ok": bool(mitmdump_path),
        "path": mitmdump_path or "",
        "install_command": "" if mitmdump_path else mitmdump_install_command(),
    }
    try:
        profile = wechat_exporter.get_active_profile(base)
        checks["exporter_auth"] = {
            "ok": bool(profile),
            "profile": profile.get("display_name", "") if profile else "",
            "expires_at": profile.get("expires_at", "") if profile else "",
        }
    except Exception as exc:
        checks["exporter_auth"] = {"ok": False, "error": str(exc)}
    proxy_states = sorted((base / "context").glob("*.proxy.json")) if (base / "context").exists() else []
    checks["proxy_state"] = check_system_proxy_recoverability(base)
    checks["proxy_state"]["active_state_files"] = [str(path) for path in proxy_states]
    stale_locks = list_stale_locks(base)
    cleared_locks = clear_stale_locks(base) if getattr(args, "clear_stale_locks", False) else 0
    remaining_stale_locks = list_stale_locks(base)
    checks["stale_locks"] = {
        "ok": not remaining_stale_locks,
        "count": len(remaining_stale_locks),
        "initial_count": len(stale_locks),
        "cleared_count": cleared_locks,
        "locks": remaining_stale_locks[:20],
    }
    checks["latest_run_manifest"] = check_latest_run_manifest(base)
    ok = all(bool(item.get("ok")) for item in checks.values())
    write_json_response({"ok": ok, "checks": checks})
    return 0 if ok else 1


def config_download(args: argparse.Namespace) -> int:
    base = runtime_dir(args.runtime_dir)
    updates: dict[str, Any] = {}
    if args.output_dir is not None:
        updates["output_dir"] = args.output_dir
    if args.html_concurrency is not None:
        updates["html_concurrency"] = args.html_concurrency
    if args.max_retries is not None:
        updates["max_retries"] = args.max_retries
    if args.overwrite_policy is not None:
        updates["overwrite_policy"] = args.overwrite_policy
    if args.download_assets is not None:
        updates["download_assets"] = args.download_assets
    preferences = save_download_preferences(base, updates) if updates else get_download_preferences(base)
    write_json_response({"ok": True, "preferences": preferences})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified Moore WeChat article wizard")
    parser.add_argument("--runtime-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="Run a natural-language WeChat article goal")
    p.add_argument("goal")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--task-id", default="")
    p.add_argument("--output-dir", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--html-concurrency", type=int, default=None, help="URL mode article fetch concurrency, capped at 4")
    p.add_argument("--max-retries", type=int, default=None, help="URL mode per-article retry count, capped at 3")
    assets = p.add_mutually_exclusive_group()
    assets.add_argument("--download-assets", dest="download_assets", action="store_true", default=None)
    assets.add_argument("--no-assets", dest="download_assets", action="store_false")
    p.set_defaults(func=run_goal)

    p = sub.add_parser("doctor", help="Check local wizard prerequisites")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--clear-stale-locks", action="store_true")
    p.set_defaults(func=doctor)

    p = sub.add_parser("smoke", help="Run commandized wizard smoke checks and persist evidence")
    p.add_argument("smoke_name", choices=["all-offline", "history-preflight", "latest-run", "qr-session", "url-fixture", "list"])
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--url", default="", help="Sample article URL for history-preflight")
    p.add_argument("--task-id", default="", help="Task id for qr-session smoke")
    p.add_argument("--limit", type=int, default=20, help="Number of recent smoke rows for smoke list")
    p.set_defaults(func=smoke)

    p = sub.add_parser("config", help="Show or update local wizard download preferences")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--html-concurrency", type=int, default=None)
    p.add_argument("--max-retries", type=int, default=None)
    p.add_argument("--overwrite-policy", choices=["skip", "force"], default=None)
    assets = p.add_mutually_exclusive_group()
    assets.add_argument("--download-assets", dest="download_assets", action="store_true", default=None)
    assets.add_argument("--no-assets", dest="download_assets", action="store_false")
    p.set_defaults(func=config_download)

    p = sub.add_parser("resume", help="Resume a waiting wizard task with a user choice or after login")
    p.add_argument("task_id")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--choice", default="")
    p.add_argument("--output-dir", default="")
    p.add_argument("--dry-run", action="store_true")
    assets = p.add_mutually_exclusive_group()
    assets.add_argument("--download-assets", dest="download_assets", action="store_true", default=None)
    assets.add_argument("--no-assets", dest="download_assets", action="store_false")
    p.set_defaults(func=resume_task)

    p = sub.add_parser("login-status", help="Poll exporter QR login status for a wizard task")
    p.add_argument("task_id")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--login-id", default="")
    p.set_defaults(func=login_status)

    p = sub.add_parser("login-complete", help="Complete exporter QR login and resume the parent wizard task")
    p.add_argument("task_id")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--login-id", default="")
    p.add_argument("--profile", default="")
    p.add_argument("--output-dir", default="")
    p.add_argument("--dry-run", action="store_true")
    assets = p.add_mutually_exclusive_group()
    assets.add_argument("--download-assets", dest="download_assets", action="store_true", default=None)
    assets.add_argument("--no-assets", dest="download_assets", action="store_false")
    p.set_defaults(func=login_complete)

    p = sub.add_parser("retry", help="Retry failed URL download items for a wizard task")
    p.add_argument("task_id")
    p.add_argument("--runtime-dir", default=argparse.SUPPRESS)
    p.add_argument("--output-dir", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--html-concurrency", type=int, default=None, help="URL mode article fetch concurrency, capped at 4")
    p.add_argument("--max-retries", type=int, default=None, help="URL mode per-article retry count, capped at 3")
    assets = p.add_mutually_exclusive_group()
    assets.add_argument("--download-assets", dest="download_assets", action="store_true", default=None)
    assets.add_argument("--no-assets", dest="download_assets", action="store_false")
    p.set_defaults(func=retry_failed)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, ValueError, sqlite3.Error, OSError) as exc:
        write_json_response({"ok": False, "state": "failed_recoverable", "error": scrub_sensitive_text(str(exc))})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
