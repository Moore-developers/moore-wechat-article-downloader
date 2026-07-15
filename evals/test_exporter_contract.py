#!/usr/bin/env python3
"""Offline contract tests for exporter mode.

These tests avoid real WeChat/exporter network calls. They validate the local
SQLite contract, fixture import, article listing, fields, and collections.
"""

from __future__ import annotations

import json
import os
import csv
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

    def fixture_account_and_article(self) -> tuple[dict, dict]:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account = self.run_cli("exporter-accounts")["accounts"][0]
        article = self.run_cli("exporter-articles", "--account-id", str(account["id"]), "--limit", "1")["articles"][0]
        return account, article

    def prepare_markdown_index(self, account: dict, article: dict, root: Path, exists: bool = True) -> Path:
        out_dir = wechat_exporter.account_output_dir(str(root), str(account["nickname"]))
        article_dir = out_dir / "articles"
        article_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = article_dir / "demo.md"
        if exists:
            markdown_path.write_text(
                "# Demo\n\n"
                f"{wechat_exporter.PAGE_DATA_START}\n\n"
                "## 页面数据\n\n旧数据\n\n"
                f"{wechat_exporter.PAGE_DATA_END}\n",
                encoding="utf-8",
            )
        index_path = out_dir / "index.csv"
        with index_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["db_article_id", "source_url", "markdown_path", "status", "title"])
            writer.writeheader()
            writer.writerow(
                {
                    "db_article_id": article["id"],
                    "source_url": article["url"],
                    "markdown_path": "articles/demo.md",
                    "status": "success",
                    "title": article["title"],
                }
            )
        return markdown_path

    def insert_engagement_rows(self, article_id: int) -> None:
        now = wechat_exporter.utc_now()
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.execute(
                """
                INSERT INTO article_metrics
                    (run_id, article_id, source, captured_at, read_count, like_count, old_like_count, share_count, comment_count, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("run-test", article_id, "wechat_session_api", now, 123, 9, 3, None, 1, "{}"),
            )
            db.execute(
                """
                INSERT INTO article_comments
                    (article_id, comment_id, nick_name, content, like_count, create_time, raw_json, created_at,
                     comment_scope, source, fetched_at, complete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (article_id, "comment-test", "读者", "有价值评论", 7, "2026-07-10", "{}", now, "elected", "wechat_session_api", now, 1),
            )
            db.commit()
        finally:
            db.close()

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

    def test_open_local_file_uses_native_macos_opener(self) -> None:
        image = self.tmp / "qr code.png"
        image.write_bytes(b"fake")
        original_platform = wechat_exporter.sys.platform
        original_run = wechat_exporter.subprocess.run
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> object:
            calls.append(command)
            return type("Completed", (), {"returncode": 0, "stderr": ""})()

        try:
            wechat_exporter.sys.platform = "darwin"
            wechat_exporter.subprocess.run = fake_run
            result = wechat_exporter.open_local_file(image)
        finally:
            wechat_exporter.sys.platform = original_platform
            wechat_exporter.subprocess.run = original_run

        self.assertTrue(result["opened"])
        self.assertEqual(result["open_method"], "macos-open")
        self.assertEqual(calls, [["open", str(image.resolve())]])

    def test_qr_start_opens_by_default_and_supports_headless_opt_out(self) -> None:
        parser = wechat_exporter.build_parser()
        default_args = parser.parse_args(["exporter-login-qr-start"])
        headless_args = parser.parse_args(["exporter-login-qr-start", "--no-open"])

        self.assertTrue(default_args.open)
        self.assertFalse(headless_args.open)

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

    def test_wizard_engagement_request_skips_regular_download(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account_id = self.fixture_account_id()
        self.mark_account_synced_today(account_id)
        articles = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "2")["articles"]
        for article in articles:
            wechat_exporter.resolve_article_context(
                self.tmp,
                int(article["id"]),
                "var comment_id = '12345';",
                biz="fakeid_gefei_demo",
            )
        calls: list[str] = []
        original_download = wechat_exporter.download_articles
        original_engagement = wechat_exporter.sync_engagement_for_articles
        original_sync = wechat_exporter.sync_account_articles

        def fail_download(*_args: object, **_kwargs: object) -> dict:
            calls.append("download")
            raise AssertionError("regular exporter download must not run for engagement requests")

        def fake_engagement(_base: Path, _account_id: int, article_ids: list[int], **_kwargs: object) -> dict:
            calls.append("engagement")
            return {"ok": True, "status": "complete", "article_count": len(article_ids)}

        def fake_sync(_base: Path, _account_id: int, _limit: int, _keyword: str = "", _profile: str = "") -> dict:
            calls.append("sync")
            return {"ok": True, "fetched_count": 2, "upserted_count": 0}

        try:
            wechat_exporter.download_articles = fail_download
            wechat_exporter.sync_engagement_for_articles = fake_engagement
            wechat_exporter.sync_account_articles = fake_sync
            result = wechat_exporter.run_wizard_after_account(
                self.tmp,
                "wizard-engagement-test",
                {
                    "target": "下载「哥飞」最新 2 篇文章的评论和互动数据",
                    "latest": 2,
                    "limit": 50,
                    "keyword": "",
                    "download": True,
                    "list_only": False,
                    "sync_only": False,
                    "output_dir": "",
                    "no_assets": False,
                    "profile": "",
                    "engagement_mode": "elected",
                },
                dict(wechat_exporter.get_account_row(self.tmp, account_id=account_id)),
            )
        finally:
            wechat_exporter.download_articles = original_download
            wechat_exporter.sync_engagement_for_articles = original_engagement
            wechat_exporter.sync_account_articles = original_sync

        self.assertTrue(result["ok"])
        self.assertEqual(result["flow"], "exporter-sync -> engagement batch download")
        self.assertIsNone(result["download"])
        self.assertEqual(calls, ["sync", "engagement"])

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

    def test_exporter_download_persists_only_non_sensitive_article_context(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        article_id = int(self.run_cli("exporter-articles", "--limit", "1")["articles"][0]["id"])
        article = wechat_exporter.get_article_download_rows(self.tmp, [article_id])[0]
        original = wechat_exporter.run_markdown_only_download

        def fake_download(urls: list[str], output_dir: Path, _assets: bool, _payload: dict, run_id: str) -> dict:
            self.assertEqual(urls, [article["url"]])
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "ok": True,
                "run_id": run_id,
                "output_dir": str(output_dir),
                "index": str(output_dir / "index.csv"),
                "success_count": 1,
                "failure_count": 0,
                "articles": [
                    {
                        "seq": "001",
                        "article_id": "local-article",
                        "source_url": article["url"],
                        "status": "success",
                        "article_context": {"biz": "biz-local", "comment_id": "12345"},
                    }
                ],
                "failed": [],
            }

        try:
            wechat_exporter.run_markdown_only_download = fake_download
            result = wechat_exporter.download_account_articles(self.tmp, [article], str(self.tmp / "delivery"))
        finally:
            wechat_exporter.run_markdown_only_download = original

        self.assertTrue(result["ok"])
        self.assertEqual(result["article_contexts"][0]["context_status"], "ready")
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            context = db.execute(
                "SELECT biz, comment_id, source FROM article_contexts WHERE article_id = ?", (article["id"],)
            ).fetchone()
            serialized = "\n".join(str(value) for row in db.execute("SELECT * FROM article_contexts") for value in row)
        finally:
            db.close()
        self.assertEqual(context, ("biz-local", "12345", "public_html"))
        self.assertNotIn("pass_ticket", serialized)

    def test_exporter_download_force_overwrites_existing_markdown(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        article_id = int(self.run_cli("exporter-articles", "--limit", "1")["articles"][0]["id"])
        article = wechat_exporter.get_article_download_rows(self.tmp, [article_id])[0]
        root = self.tmp / "delivery"
        out_dir = wechat_exporter.account_output_dir(str(root), str(article["account_name"]))
        markdown = out_dir / "articles" / "existing.md"
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text("# old\n", encoding="utf-8")
        wechat_exporter.write_account_index(
            out_dir,
            [article],
            {
                "articles": [
                    {
                        "seq": "001",
                        "source_url": article["url"],
                        "status": "success",
                        "markdown_path": "articles/existing.md",
                        "image_dir": "",
                        "image_count": 0,
                    }
                ],
                "failed": [],
            },
            "run-existing",
        )
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.execute("UPDATE articles SET content_downloaded = 1 WHERE id = ?", (article_id,))
            db.commit()
        finally:
            db.close()
        article["content_downloaded"] = 1
        skipped = wechat_exporter.download_account_articles(self.tmp, [article], str(root))
        self.assertEqual(skipped["skipped_count"], 1)

        calls: list[list[str]] = []
        original = wechat_exporter.run_markdown_only_download

        def fake_download(urls: list[str], output_dir: Path, _assets: bool, _payload: dict, run_id: str) -> dict:
            calls.append(urls)
            markdown.write_text("# new\n", encoding="utf-8")
            return {
                "ok": True,
                "run_id": run_id,
                "output_dir": str(output_dir),
                "index": str(output_dir / "index.csv"),
                "success_count": 1,
                "failure_count": 0,
                "articles": [
                    {
                        "seq": "001",
                        "source_url": article["url"],
                        "status": "success",
                        "markdown_path": "articles/existing.md",
                        "image_dir": "",
                        "image_count": 0,
                    }
                ],
                "failed": [],
            }

        try:
            wechat_exporter.run_markdown_only_download = fake_download
            forced = wechat_exporter.download_account_articles(self.tmp, [article], str(root), force=True)
        finally:
            wechat_exporter.run_markdown_only_download = original

        self.assertEqual(calls, [[article["url"]]])
        self.assertTrue(forced["ok"])
        self.assertEqual(forced["redownload_count"], 1)
        self.assertEqual(markdown.read_text(encoding="utf-8"), "# new\n")

    def test_article_context_parser_returns_only_safe_identifiers(self) -> None:
        context = wechat_exporter.extract_wechat_article_context(
            "var __biz = 'MzIxNTA1MDEwNg=='; var comment_id = '12345'; var key = 'secret';",
            "https://mp.weixin.qq.com/s/demo?pass_ticket=secret",
        )
        self.assertEqual(context, {"biz": "MzIxNTA1MDEwNg==", "comment_id": "12345"})

    def test_wechat_collection_sync_persists_metrics_and_elected_comments(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account_id = self.fixture_account_id()
        article = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "1")["articles"][0]
        context = wechat_exporter.resolve_article_context(
            self.tmp,
            int(article["id"]),
            "var comment_id = '12345';",
            biz="biz-sync",
        )
        self.assertTrue(context["ok"])
        context_dir = self.tmp / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "active-proxy-session.json").write_text(json.dumps({"session_id": "proxy-enhancer-test"}), encoding="utf-8")
        capability_path = wechat_exporter.credential_capability_path(self.tmp, "proxy-enhancer-test")
        capability_path.write_text("capability-test\n", encoding="utf-8")
        original = wechat_exporter.broker_request

        def fake_broker_request(socket_path: Path, payload: dict, timeout_seconds: float) -> dict:
            self.assertTrue(socket_path.name.startswith("moore-wechat-"))
            self.assertTrue(socket_path.name.endswith(".sock"))
            self.assertEqual(payload["op"], "fetch_engagement")
            self.assertEqual(payload["biz"], "biz-sync")
            self.assertEqual(payload["capability"], "capability-test")
            return {
                "ok": True,
                "status": "complete",
                "articles": [
                    {
                        "ok": True,
                        "article_id": article["id"],
                        "metrics": {"read_count": 20, "like_count": 4, "old_like_count": 2, "share_count": None, "comment_count": 1},
                        "comments": [{"comment_id": "c-sync", "nick_name": "读者", "content": "有收获", "like_count": 2}],
                        "comments_complete": True,
                    }
                ],
            }

        try:
            wechat_exporter.broker_request = fake_broker_request
            result = wechat_exporter.sync_engagement(self.tmp, account_id, limit=1)
        finally:
            wechat_exporter.broker_request = original

        self.assertTrue(result["ok"])
        self.assertEqual(result["comment_scope"], "elected")
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            metric = db.execute("SELECT read_count, like_count, old_like_count, comment_count FROM article_metrics").fetchone()
            comment = db.execute("SELECT comment_scope, source, complete FROM article_comments WHERE comment_id = 'c-sync'").fetchone()
            run = db.execute("SELECT status, success_count, failed_count FROM engagement_runs").fetchone()
        finally:
            db.close()
        self.assertEqual(metric, (20, 4, 2, 1))
        self.assertEqual(comment, ("elected", "wechat_session_api", 1))
        self.assertEqual(run, ("complete", 1, 0))

    def test_write_engagement_to_markdown_replaces_single_page_data_block(self) -> None:
        account, article = self.fixture_account_and_article()
        article_id = int(article["id"])
        markdown_path = self.prepare_markdown_index(account, article, self.tmp / "library")
        self.insert_engagement_rows(article_id)

        first = wechat_exporter.write_engagement_to_markdown(self.tmp, [article_id], str(self.tmp / "library"))
        second = wechat_exporter.write_engagement_to_markdown(self.tmp, [article_id], str(self.tmp / "library"))

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        content = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(content.count(wechat_exporter.PAGE_DATA_START), 1)
        self.assertIn("微信短时会话接口", content)
        self.assertIn("精选评论", content)
        self.assertIn("读者", content)
        self.assertIn("有价值评论", content)
        self.assertNotIn("旧数据", content)

    def test_write_engagement_missing_markdown_logs_diagnostic_without_crashing(self) -> None:
        account, article = self.fixture_account_and_article()
        article_id = int(article["id"])
        self.prepare_markdown_index(account, article, self.tmp / "library", exists=False)
        self.insert_engagement_rows(article_id)

        result = wechat_exporter.write_engagement_to_markdown(self.tmp, [article_id], str(self.tmp / "library"))
        events = wechat_exporter.list_evolution_events(self.tmp, 5)

        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(events[0]["stage"], "markdown_writeback")
        self.assertEqual(events[0]["code"], "markdown_missing")

    def test_diagnostics_can_export_sanitized_evolution_fixture(self) -> None:
        wechat_exporter.log_evolution_event(
            self.tmp,
            "broker",
            "shape_changed",
            "warning",
            detail={
                "token": "secret-token",
                "pass_ticket": "secret-ticket",
                "auth-key": "secret-auth",
                "safe_shape": {"articles": 1},
            },
        )

        payload = self.run_cli("wechat-collection-diagnostics", "--export-fixture", "fixtures/diagnostics.json")
        fixture_path = Path(payload["fixture"]["fixture"])
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        serialized = json.dumps(fixture, ensure_ascii=False)

        self.assertTrue(payload["ok"])
        self.assertTrue(fixture_path.exists())
        self.assertEqual(fixture["event_count"], 1)
        self.assertIn("reference_project_checklist", fixture)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("secret-ticket", serialized)
        self.assertNotIn("secret-auth", serialized)

    def test_engagement_sync_uses_account_biz_mapping_for_legacy_incomplete_context(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account = self.run_cli("exporter-accounts")["accounts"][0]
        account_id = int(account["id"])
        account_biz = str(account["fakeid"])
        article = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "1")["articles"][0]
        self.assertTrue(
            wechat_exporter.resolve_article_context(
                self.tmp, int(article["id"]), biz=account_biz, comment_id="legacy-comment"
            )["ok"]
        )
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.execute(
                "UPDATE article_contexts SET biz = '', context_status = 'incomplete' WHERE article_id = ?",
                (int(article["id"]),),
            )
            db.commit()
        finally:
            db.close()
        context_dir = self.tmp / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "active-proxy-session.json").write_text(json.dumps({"session_id": "proxy-enhancer-test"}), encoding="utf-8")
        wechat_exporter.credential_capability_path(self.tmp, "proxy-enhancer-test").write_text("capability-test\n", encoding="utf-8")
        original = wechat_exporter.broker_request

        def fake_broker_request(_socket: Path, payload: dict, timeout_seconds: float) -> dict:
            self.assertEqual(payload["biz"], account_biz)
            self.assertEqual([item["article_id"] for item in payload["articles"]], [article["id"]])
            self.assertEqual(payload["articles"][0]["biz"], account_biz)
            return {
                "ok": True,
                "articles": [
                    {
                        "ok": True,
                        "article_id": article["id"],
                        "metrics": {"read_count": 11},
                        "comments": [],
                        "comments_complete": True,
                    }
                ],
            }

        try:
            wechat_exporter.broker_request = fake_broker_request
            result = wechat_exporter.sync_engagement(self.tmp, account_id, limit=1)
        finally:
            wechat_exporter.broker_request = original

        self.assertTrue(result["ok"])
        self.assertEqual(result["success_count"], 1)

    def test_waiting_engagement_run_resumes_without_creating_another_run(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account_id = self.fixture_account_id()
        article = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "1")["articles"][0]
        self.assertTrue(
            wechat_exporter.resolve_article_context(
                self.tmp, int(article["id"]), "var comment_id = '12345';", biz="biz-resume"
            )["ok"]
        )
        context_dir = self.tmp / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "active-proxy-session.json").write_text(json.dumps({"session_id": "proxy-enhancer-test"}), encoding="utf-8")
        wechat_exporter.credential_capability_path(self.tmp, "proxy-enhancer-test").write_text("capability-test\n", encoding="utf-8")
        created = wechat_exporter.create_engagement_run(self.tmp, account_id, 1)
        original = wechat_exporter.broker_request

        def fake_broker_request(_socket: Path, payload: dict, timeout_seconds: float) -> dict:
            self.assertEqual(timeout_seconds, 180)
            self.assertEqual(payload["biz"], "biz-resume")
            return {
                "ok": True,
                "articles": [
                    {
                        "ok": True,
                        "article_id": article["id"],
                        "metrics": {"read_count": 9},
                        "comments": [],
                        "comments_complete": True,
                    }
                ],
            }

        try:
            wechat_exporter.broker_request = fake_broker_request
            resumed = wechat_exporter.resume_waiting_engagement_runs(self.tmp, "biz-resume")
        finally:
            wechat_exporter.broker_request = original

        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["run_count"], 1)
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            runs = db.execute("SELECT run_id, status FROM engagement_runs").fetchall()
        finally:
            db.close()
        self.assertEqual(runs, [(created["run_id"], "complete")])

    def test_engagement_writeback_replaces_page_data_section(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account_id = self.fixture_account_id()
        article = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "1")["articles"][0]
        out_dir = self.tmp / "delivery" / "哥飞"
        article_dir = out_dir / "articles"
        article_dir.mkdir(parents=True, exist_ok=True)
        markdown = article_dir / "AI 工具出海实战.md"
        markdown.write_text(
            "# AI 工具出海实战\n\n正文\n\n"
            f"{wechat_exporter.PAGE_DATA_START}\n\n## 页面数据\n\n旧数据\n\n{wechat_exporter.PAGE_DATA_END}\n",
            encoding="utf-8",
        )
        with (out_dir / "index.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["db_article_id", "source_url", "status", "markdown_path", "image_dir", "image_count"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "db_article_id": article["id"],
                    "source_url": article["url"],
                    "status": "success",
                    "markdown_path": "articles/AI 工具出海实战.md",
                    "image_dir": "",
                    "image_count": "0",
                }
            )
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.execute(
                """
                INSERT INTO article_metrics
                    (run_id, article_id, source, captured_at, read_count, like_count, old_like_count, share_count, comment_count, raw_json)
                VALUES ('run-writeback', ?, 'wechat_session_api', '2026-07-10T00:00:00Z', 123, 4, 2, 8, 1, '{}')
                """,
                (int(article["id"]),),
            )
            db.execute(
                """
                INSERT INTO article_comments
                    (article_id, comment_id, nick_name, content, like_count, create_time, raw_json, created_at, comment_scope, source, fetched_at, complete)
                VALUES (?, 'comment-writeback', '读者A', '这个选题能打', 7, '2026-07-10', '{}', 'now', 'elected', 'wechat_session_api', 'now', 1)
                """,
                (int(article["id"]),),
            )
            db.commit()
        finally:
            db.close()

        result = wechat_exporter.write_engagement_to_markdown(self.tmp, [int(article["id"])], str(self.tmp / "delivery"))

        self.assertTrue(result["ok"])
        text = markdown.read_text(encoding="utf-8")
        self.assertEqual(text.count(wechat_exporter.PAGE_DATA_START), 1)
        self.assertIn("微信短时会话接口", text)
        self.assertIn("精选评论", text)
        self.assertIn("这个选题能打", text)
        self.assertNotIn("旧数据", text)

    def test_library_verify_repairs_missing_downloaded_markdown_and_logs_event(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account_id = self.fixture_account_id()
        article = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "1")["articles"][0]
        out_dir = self.tmp / "delivery" / "哥飞"
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "index.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["db_article_id", "source_url", "status", "markdown_path", "image_dir", "image_count"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "db_article_id": article["id"],
                    "source_url": article["url"],
                    "status": "success",
                    "markdown_path": "articles/missing.md",
                    "image_dir": "",
                    "image_count": "0",
                }
            )
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.execute("UPDATE articles SET content_downloaded = 1 WHERE id = ?", (int(article["id"]),))
            db.commit()
        finally:
            db.close()

        result = wechat_exporter.verify_account_library(self.tmp, account_id, out_dir)

        self.assertFalse(result["ok"])
        self.assertGreaterEqual(result["fixed_count"], 1)
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            downloaded = db.execute("SELECT content_downloaded FROM articles WHERE id = ?", (int(article["id"]),)).fetchone()[0]
            event_count = db.execute("SELECT COUNT(*) FROM evolution_events WHERE code = 'library_inconsistent'").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(downloaded, 0)
        self.assertEqual(event_count, 1)

    def test_diagnostics_exports_sanitized_fixture(self) -> None:
        wechat_exporter.log_evolution_event(
            self.tmp,
            "unit",
            "sample_failure",
            "warning",
            detail={
                "url": "https://mp.weixin.qq.com/s/demo?pass_ticket=secret-ticket&key=secret-key",
                "cookie": "secret-cookie",
            },
        )
        fixture = self.tmp / "diagnostics" / "fixture.json"
        payload = self.run_cli("wechat-collection-diagnostics", "--export-fixture", str(fixture))

        self.assertTrue(payload["ok"])
        self.assertTrue(fixture.exists())
        text = fixture.read_text(encoding="utf-8")
        self.assertIn("reference_project_checklist", text)
        for secret in ("secret-ticket", "secret-key", "secret-cookie"):
            self.assertNotIn(secret, text)

    def test_exporter_wizard_waiting_collection_resume_uses_existing_run(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account_id = self.fixture_account_id()
        article = self.run_cli("exporter-articles", "--account-id", str(account_id), "--limit", "1")["articles"][0]
        self.assertTrue(
            wechat_exporter.resolve_article_context(
                self.tmp, int(article["id"]), "var comment_id = '12345';", biz="biz-wizard"
            )["ok"]
        )
        created = wechat_exporter.create_engagement_run(self.tmp, account_id, 1)
        session_id = "wiz_contract_waiting"
        request = {
            "target": "下载哥飞最新1篇，需要评论和互动数据",
            "account_query": "哥飞",
            "download": True,
            "latest": 1,
            "limit": 1,
            "output_dir": "",
            "no_assets": False,
            "engagement_mode": "elected",
        }
        result = {
            "ok": True,
            "state": "waiting_wechat_collection",
            "session_id": session_id,
            "selected_article_ids": [int(article["id"])],
            "engagement": {"run_id": created["run_id"], "biz": "biz-wizard", "status": "waiting_credential"},
        }
        wechat_exporter.save_wizard_session(
            self.tmp,
            session_id,
            request["target"],
            "waiting_wechat_collection",
            request,
            selected_account_id=account_id,
            selected_article_ids=[int(article["id"])],
            result=result,
        )
        context_dir = self.tmp / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "active-proxy-session.json").write_text(json.dumps({"session_id": "proxy-enhancer-test"}), encoding="utf-8")
        wechat_exporter.credential_capability_path(self.tmp, "proxy-enhancer-test").write_text("capability-test\n", encoding="utf-8")
        original = wechat_exporter.broker_request

        def fake_broker_request(_socket: Path, payload: dict, timeout_seconds: float) -> dict:
            self.assertEqual(timeout_seconds, 180)
            self.assertEqual(payload["biz"], "biz-wizard")
            return {
                "ok": True,
                "articles": [
                    {
                        "ok": True,
                        "article_id": article["id"],
                        "metrics": {"read_count": 42},
                        "comments": [],
                        "comments_complete": True,
                    }
                ],
            }

        try:
            wechat_exporter.broker_request = fake_broker_request
            resumed = wechat_exporter.resume_wizard_wechat_collection(self.tmp, session_id, wechat_exporter.load_wizard_session(self.tmp, session_id), request)
        finally:
            wechat_exporter.broker_request = original

        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["state"], "done")
        self.assertEqual(resumed["resumed_from"], "waiting_wechat_collection")
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            runs = db.execute("SELECT run_id, status FROM engagement_runs").fetchall()
        finally:
            db.close()
        self.assertEqual(runs, [(created["run_id"], "complete")])

    def test_library_dataset_manifest_is_local_and_contains_article_state(self) -> None:
        self.run_cli("exporter-import-fixture", str(FIXTURE))
        account_id = self.fixture_account_id()
        result = wechat_exporter.create_dataset_manifest(self.tmp, account_id, "test-dataset", str(self.tmp / "library"))
        self.assertTrue(result["ok"])
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset_id"], "test-dataset")
        self.assertEqual(manifest["article_count"], 3)
        self.assertIn("content_status", manifest["articles"][0])
        self.assertTrue(Path(result["csv"]).exists())

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
