#!/usr/bin/env python3
"""Offline contract tests for exporter mode.

These tests avoid real WeChat/exporter network calls. They validate the local
SQLite contract, fixture import, article listing, fields, and collections.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wechat_exporter.py"
FIXTURE = ROOT / "evals" / "fixtures" / "exporter_fixture.json"
sys.path.insert(0, str(ROOT / "scripts"))
import wechat_exporter  # noqa: E402


class ExporterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="moore-exporter-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args: str) -> dict:
        env = dict(**os.environ, MOORE_WECHAT_EXPORTER_DISABLE_KEYCHAIN="1")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--runtime-dir", str(self.tmp), *args],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)
        return json.loads(result.stdout)

    def run_cli_allow_fail(self, *args: str) -> tuple[int, dict]:
        env = dict(**os.environ, MOORE_WECHAT_EXPORTER_DISABLE_KEYCHAIN="1")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--runtime-dir", str(self.tmp), *args],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertTrue(result.stdout.strip(), msg=result.stderr)
        return result.returncode, json.loads(result.stdout)

    def mark_account_synced_today(self, account_id: int) -> None:
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.execute(
                "UPDATE target_accounts SET last_sync_at = ?, updated_at = ? WHERE id = ?",
                (wechat_exporter.utc_now(), wechat_exporter.utc_now(), account_id),
            )
            db.commit()
        finally:
            db.close()

    def fixture_account_id(self) -> int:
        return int(self.run_cli("exporter-accounts")["accounts"][0]["id"])

    def test_init_creates_sqlite_schema(self) -> None:
        payload = self.run_cli("exporter-init")
        self.assertTrue(payload["ok"])
        db_path = self.tmp / "exporter.sqlite"
        self.assertTrue(db_path.exists())
        db = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("login_profiles", tables)
            self.assertIn("target_accounts", tables)
            self.assertIn("articles", tables)
            self.assertIn("collections", tables)
            self.assertIn("field_presets", tables)
            self.assertIn("wizard_sessions", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], wechat_exporter.EXPORTER_DB_VERSION)
            self.assertIn("account_biz_mappings", tables)
            self.assertIn("article_contexts", tables)
            self.assertIn("article_metrics", tables)
            self.assertIn("article_comment_replies", tables)
        finally:
            db.close()

    def test_config_does_not_lock_database_and_redacts_auth_key(self) -> None:
        payload = self.run_cli("exporter-config", "--auth-key", "test-auth-key-123456", "--allow-plain-auth-key")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["credential_storage"], "plain")
        self.assertNotIn("test-auth-key-123456", json.dumps(payload, ensure_ascii=False))

        status = self.run_cli("exporter-db-status")
        self.assertEqual(status["counts"]["login_profiles"], 1)

    def test_fixture_import_lists_articles_and_collections(self) -> None:
        imported = self.run_cli("exporter-import-fixture", str(FIXTURE))
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["article_count"], 3)

        accounts = self.run_cli("exporter-accounts")
        self.assertEqual(len(accounts["accounts"]), 1)
        account_id = accounts["accounts"][0]["id"]

        articles = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "10")
        self.assertEqual(articles["count"], 3)
        self.assertIn("AI 工具出海实战", [item["title"] for item in articles["articles"]])

        collections = self.run_cli("exporter-collections", "--account-id", str(account_id))
        titles = {item["title"] for item in collections["collections"]}
        self.assertIn("AI", titles)
        self.assertIn("运营", titles)

    def test_unwrap_items_supports_top_level_articles(self) -> None:
        payload = {"base_resp": {}, "articles": [{"title": "A"}, {"title": "B"}]}
        self.assertEqual(len(wechat_exporter.unwrap_items(payload)), 2)

    def test_render_page_has_account_switcher(self) -> None:
        wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_a",
                "nickname": "账号A",
                "alias": "a",
                "raw_json": "{}",
            },
        )
        account_b = wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_b",
                "nickname": "账号B",
                "alias": "b",
                "raw_json": "{}",
            },
        )["account"]
        html = wechat_exporter.render_page(self.tmp, int(account_b["id"]))
        self.assertIn("name='account_id'", html)
        self.assertIn("账号A", html)
        self.assertIn("账号B", html)
        self.assertIn("selected", html)

    def test_render_login_page_uses_local_qrcode_route(self) -> None:
        login_id = "test-login"
        qrcode_path = wechat_exporter.login_qrcode_path(self.tmp, login_id)
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        qrcode_path.write_bytes(b"fake")
        wechat_exporter.write_json_file(
            wechat_exporter.login_session_path(self.tmp, login_id),
            {
                "login_id": login_id,
                "base_url": "https://down.mptext.top",
                "qrcode_path": str(qrcode_path),
                "qrcode_content_type": "image/png",
            },
        )
        html = wechat_exporter.render_login_page(self.tmp, login_id)
        self.assertIn("/login/qrcode?login_id=test-login", html)
        self.assertIn("/login/status?login_id=", html)

    def test_qrcode_path_uses_real_image_extension(self) -> None:
        jpg = wechat_exporter.login_qrcode_path_with_extension(self.tmp, "login-a", "image/jpeg", b"\xff\xd8\xff")
        png = wechat_exporter.login_qrcode_path_with_extension(self.tmp, "login-b", "application/octet-stream", b"\x89PNG\r\n")

        self.assertEqual(jpg.suffix, ".jpg")
        self.assertEqual(png.suffix, ".png")

    def test_field_preset_rejects_unknown_fields(self) -> None:
        payload = self.run_cli("exporter-fields", "--set", "title,url,publish_time,token")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["visible_fields"], ["title", "url", "publish_time"])

        fields = self.run_cli("exporter-fields")
        default = next(item for item in fields["presets"] if item["name"] == "default")
        self.assertEqual(default["visible_fields"], ["title", "url", "publish_time"])

    def test_wizard_exact_local_match_lists_latest_articles(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        self.mark_account_synced_today(self.fixture_account_id())

        payload = self.run_cli("exporter-wizard", "用 exporter 模式同步「哥飞」，列出最近 2 篇让我选", "--latest", "2", "--list-only")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["account"]["nickname"], "哥飞")
        self.assertEqual(payload["count"], 2)
        self.assertEqual([item["title"] for item in payload["articles"]], ["AI 工具出海实战", "公众号运营专家入门"])

    def test_wizard_multiple_local_matches_requires_choice_and_resume(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        self.mark_account_synced_today(self.fixture_account_id())
        wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_gefei_plus",
                "nickname": "哥飞精选",
                "alias": "gefei-plus",
                "raw_json": "{}",
            },
        )

        payload = self.run_cli("exporter-wizard", "哥", "--list-only")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "need_account_choice")
        self.assertEqual(len(payload["candidates"]), 2)

        resumed = self.run_cli("exporter-wizard", "--resume", payload["session_id"], "--choice", "1", "--list-only", "--latest", "1")
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["account"]["nickname"], "哥飞")
        self.assertEqual([item["title"] for item in resumed["articles"]], ["AI 工具出海实战"])

    def test_wizard_article_url_uses_local_sqlite_before_network(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        self.mark_account_synced_today(self.fixture_account_id())

        payload = self.run_cli(
            "exporter-wizard",
            "根据这篇文章 URL 找公众号并列出历史文章：https://mp.weixin.qq.com/s/demo-exporter-article-1",
            "--list-only",
            "--latest",
            "1",
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["account"]["nickname"], "哥飞")
        self.assertEqual(payload["articles"][0]["title"], "AI 工具出海实战")

    def test_wizard_article_url_lookup_does_not_treat_underscore_as_wildcard(self) -> None:
        account = wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_underscore",
                "nickname": "下划线账号",
                "alias": "underscore",
                "raw_json": "{}",
            },
        )["account"]
        wechat_exporter.upsert_articles(
            self.tmp,
            [
                wechat_exporter.normalize_article(
                    {
                        "title": "含下划线短链",
                        "link": "https://mp.weixin.qq.com/s/a_b",
                        "update_time": 1783000000,
                    },
                    int(account["id"]),
                )
            ],
        )

        code, payload = self.run_cli_allow_fail(
            "exporter-wizard",
            "根据这篇文章 URL 找公众号并列出历史文章：https://mp.weixin.qq.com/s/acb",
            "--list-only",
        )

        self.assertNotEqual(code, 0)
        self.assertNotEqual(payload.get("account", {}).get("nickname"), "下划线账号")

    def test_wizard_scrubs_sensitive_url_from_output_and_session(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        self.mark_account_synced_today(self.fixture_account_id())
        sensitive_url = "https://mp.weixin.qq.com/s/demo-exporter-article-1?token=abc&pass_ticket=secret&scene=1"

        payload = self.run_cli("exporter-wizard", f"根据这篇文章 URL 列出历史：{sensitive_url}", "--list-only", "--latest", "1")

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("pass_ticket", serialized)
        self.assertNotIn("secret", serialized)
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            stored = "\n".join(str(row[0]) for row in db.execute("SELECT target || request_json || result_json FROM wizard_sessions"))
            self.assertNotIn("pass_ticket", stored)
            self.assertNotIn("secret", stored)
        finally:
            db.close()

    def test_wizard_empty_alias_does_not_match_everything(self) -> None:
        wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_unrelated",
                "nickname": "完全无关",
                "alias": "",
                "raw_json": "{}",
            },
        )

        _code, payload = self.run_cli_allow_fail("exporter-wizard", "not-a-real-target", "--list-only")

        self.assertNotEqual(payload.get("account", {}).get("nickname"), "完全无关")
        self.assertIn(payload["state"], {"need_login", "not_found"})

    def test_wizard_rejects_cross_account_article_ids_on_resume(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        self.mark_account_synced_today(self.fixture_account_id())
        second = wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_second",
                "nickname": "第二账号",
                "alias": "second",
                "raw_json": "{}",
            },
        )["account"]
        wechat_exporter.upsert_articles(
            self.tmp,
            [
                wechat_exporter.normalize_article(
                    {
                        "title": "第二账号文章",
                        "link": "https://mp.weixin.qq.com/s/second-account-article",
                        "update_time": 1783000000,
                    },
                    int(second["id"]),
                )
            ],
        )
        other_article_id = self.run_cli("exporter-articles", "--account-id", str(second["id"]), "--limit", "1")["articles"][0]["id"]
        session = self.run_cli("exporter-wizard", "哥飞", "--list-only")

        code, payload = self.run_cli_allow_fail("exporter-wizard", "--resume", session["session_id"], "--article-ids", str(other_article_id))

        self.assertNotEqual(code, 0)
        self.assertIn("do not belong", payload["error"])

    def test_wizard_list_only_requires_sync_when_cache_is_not_from_today(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))

        code, payload = self.run_cli_allow_fail("exporter-wizard", "同步「哥飞」，列出最近 2 篇让我选", "--latest", "2", "--list-only")

        self.assertNotEqual(code, 0)
        self.assertEqual(payload["state"], "need_login")
        self.assertEqual(payload["account"]["nickname"], "哥飞")

    def test_wizard_latest_download_requires_sync_instead_of_stale_cache(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))

        code, payload = self.run_cli_allow_fail("exporter-wizard", "用 exporter 模式下载公众号「哥飞」最新 20 篇", "--latest", "20")

        self.assertNotEqual(code, 0)
        self.assertEqual(payload["state"], "need_login")
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM download_runs").fetchone()[0], 0)
        finally:
            db.close()

    def test_wizard_need_login_session_can_resume_without_account_id(self) -> None:
        code, payload = self.run_cli_allow_fail("exporter-wizard", "用 exporter 模式下载公众号「哥飞」最新 20 篇", "--latest", "20")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["state"], "need_login")

        code, resumed = self.run_cli_allow_fail("exporter-wizard", "--resume", payload["session_id"])

        self.assertNotEqual(code, 0)
        self.assertEqual(resumed["state"], "need_login")
        self.assertNotIn("wizard resume needs", resumed.get("error", ""))

    def test_sync_paginates_when_exporter_response_has_no_total(self) -> None:
        account = wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_paginated",
                "nickname": "分页账号",
                "alias": "paged",
                "raw_json": "{}",
            },
        )["account"]
        original_api_request = wechat_exporter.api_request

        def fake_api_request(base: Path, path: str, params: dict, profile: str = "") -> dict:
            begin = int(params["begin"])
            size = int(params["size"])
            articles = [
                {
                    "title": f"分页文章 {number}",
                    "link": f"https://mp.weixin.qq.com/s/page-{number}",
                    "update_time": 1783000000 - number,
                }
                for number in range(begin + 1, begin + size + 1)
                if number <= 50
            ]
            return {"articles": articles, "base_resp": {"ret": 0, "err_msg": "ok"}}

        try:
            wechat_exporter.api_request = fake_api_request
            payload = wechat_exporter.sync_account_articles(self.tmp, int(account["id"]), 50)
        finally:
            wechat_exporter.api_request = original_api_request

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["fetched_count"], 50)
        self.assertEqual(len(wechat_exporter.list_articles(self.tmp, int(account["id"]), 100)), 50)

    def test_wizard_sync_only_never_creates_download_run(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))

        code, payload = self.run_cli_allow_fail("exporter-wizard", "同步「哥飞」", "--sync-only")

        self.assertNotEqual(code, 0)
        self.assertEqual(payload["state"], "need_login")
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM download_runs").fetchone()[0], 0)
        finally:
            db.close()

    def test_metrics_and_comments_import(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        articles = self.run_cli("exporter-articles", "--limit", "1")
        article = articles["articles"][0]
        metrics = json.dumps({"article_id": article["id"], "read_count": 123, "like_count": 45, "comment_count": 2})
        payload = self.run_cli("exporter-metrics-import", metrics)
        self.assertEqual(payload["updated_count"], 1)

        comment = json.dumps({"article_id": article["id"], "comment_id": "c1", "nick_name": "reader", "content": "有用", "like_count": 3})
        payload = self.run_cli("exporter-comments-import", comment)
        self.assertEqual(payload["inserted_count"], 1)

        comments = self.run_cli("exporter-comments", "--article-id", str(article["id"]))
        self.assertEqual(comments["count"], 1)
        self.assertEqual(comments["comments"][0]["content"], "有用")

        payload = self.run_cli("exporter-comments-import", comment)
        self.assertEqual(payload["inserted_count"], 1)
        comments = self.run_cli("exporter-comments", "--article-id", str(article["id"]))
        self.assertEqual(comments["count"], 1)
        self.assertEqual(comments["comments"][0]["comment_scope"], "elected")

    def test_context_resolution_requires_unique_biz_mapping(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        article = self.run_cli("exporter-articles", "--limit", "1")["articles"][0]
        context = wechat_exporter.resolve_article_context(
            self.tmp,
            int(article["id"]),
            "var comment_id = '12345' || '0';",
            biz="biz-demo",
        )
        self.assertTrue(context["ok"])
        self.assertEqual(context["context"]["comment_id"], "12345")
        self.assertEqual(context["context"]["biz"], "biz-demo")

        account = wechat_exporter.upsert_account(
            self.tmp,
            {"fakeid": "fakeid-other", "nickname": "另一个账号"},
        )["account"]
        wechat_exporter.upsert_articles(
            self.tmp,
            [
                {
                    "account_id": int(account["id"]),
                    "msgid": "other-msgid",
                    "idx": 1,
                    "title": "另一篇文章",
                    "url": "https://mp.weixin.qq.com/s/other",
                    "digest": "",
                    "cover_url": "",
                    "author": "",
                    "publish_time": "",
                    "create_time": "",
                    "is_original": 0,
                    "is_deleted": 0,
                    "article_status": "",
                    "content_downloaded": 0,
                    "collection_title": "",
                    "raw_json": "{}",
                }
            ],
        )
        other = wechat_exporter.list_articles(self.tmp, int(account["id"]))[0]
        conflict = wechat_exporter.resolve_article_context(
            self.tmp,
            int(other["id"]),
            "var comment_id = '67890' || '0';",
            biz="biz-demo",
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["context_status"], "mapping_conflict")

    def test_init_additively_upgrades_exporter_v3_database(self) -> None:
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.executescript(
                """
                CREATE TABLE article_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    comment_id TEXT NOT NULL DEFAULT '',
                    nick_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    like_count INTEGER,
                    create_time TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                INSERT INTO article_comments (article_id, comment_id, created_at) VALUES (1, '', 'old');
                PRAGMA user_version = 3;
                """
            )
            db.commit()
        finally:
            db.close()

        payload = wechat_exporter.init_exporter_db(self.tmp)
        self.assertTrue(payload["ok"])
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(article_comments)")}
            self.assertTrue({"comment_scope", "source", "fetched_at", "complete"}.issubset(columns))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], wechat_exporter.EXPORTER_DB_VERSION)
            self.assertEqual(db.execute("SELECT comment_id FROM article_comments").fetchone()[0], "legacy-1")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
