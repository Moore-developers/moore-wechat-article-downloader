#!/usr/bin/env python3
"""Offline contract tests for the unified wizard P0a."""

from __future__ import annotations

import json
import hashlib
import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from argparse import Namespace
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wechat_wizard.py"
FIXTURE = ROOT / "evals" / "fixtures" / "exporter_fixture.json"
HTML_FIXTURES = ROOT / "evals" / "fixtures" / "html"
sys.path.insert(0, str(ROOT / "scripts"))
import wechat_exporter  # noqa: E402
import wechat_downloader  # noqa: E402
import wechat_wizard  # noqa: E402


class WizardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="moore-wizard-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args: str, allow_fail: bool = False, extra_env: dict[str, str] | None = None) -> tuple[int, dict]:
        env = dict(os.environ, MOORE_WECHAT_WIZARD_DISABLE_QR_AUTO="1")
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--runtime-dir", str(self.tmp), *args],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertTrue(result.stdout.strip(), msg=result.stderr)
        payload = json.loads(result.stdout)
        if not allow_fail:
            self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)
        return result.returncode, payload

    def test_init_creates_wizard_schema(self) -> None:
        payload = wechat_wizard.init_wizard_db(self.tmp)
        self.assertTrue(payload["ok"])
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("wizard_tasks", tables)
            self.assertIn("wizard_gate_events", tables)
            self.assertIn("wizard_choices", tables)
            self.assertIn("wizard_locks", tables)
            self.assertIn("download_runs", tables)
            self.assertIn("download_items", tables)
            self.assertIn("user_preferences", tables)
            self.assertIn("smoke_runs", tables)
            columns = {row[1] for row in db.execute("PRAGMA table_info(download_runs)")}
            self.assertIn("duration_ms", columns)
            self.assertIn("retry_count", columns)
            item_columns = {row[1] for row in db.execute("PRAGMA table_info(download_items)")}
            self.assertIn("previous_run_id", item_columns)
            self.assertIn("previous_markdown_path", item_columns)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], wechat_wizard.WIZARD_DB_VERSION)
            prefs = json.loads(db.execute("SELECT value_json FROM user_preferences WHERE name = 'download'").fetchone()[0])
            self.assertEqual(prefs["html_concurrency"], 1)
            self.assertEqual(prefs["max_retries"], 0)
            self.assertTrue(prefs["download_assets"])
        finally:
            db.close()

    def test_init_additively_upgrades_old_wizard_schema(self) -> None:
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            db.executescript(
                """
                CREATE TABLE download_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version = 3;
                """
            )
            db.commit()
        finally:
            db.close()

        payload = wechat_wizard.init_wizard_db(self.tmp)
        self.assertTrue(payload["ok"])
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(download_runs)")}
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("duration_ms", columns)
            self.assertIn("retry_count", columns)
            self.assertIn("user_preferences", tables)
            self.assertIn("smoke_runs", tables)
            item_columns = {row[1] for row in db.execute("PRAGMA table_info(download_items)")}
            self.assertIn("previous_run_id", item_columns)
            self.assertIn("previous_markdown_path", item_columns)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], wechat_wizard.WIZARD_DB_VERSION)
        finally:
            db.close()

    def test_url_goal_dry_run_persists_task_and_gates(self) -> None:
        code, payload = self.run_cli(
            "run",
            "下载这些文章：https://mp.weixin.qq.com/s/a https://mp.weixin.qq.com/s/a https://mp.weixin.qq.com/s/b",
            "--dry-run",
        )

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["mode"], "url")
        self.assertEqual(payload["download_plan"]["selected_article_count"], 2)
        self.assertEqual(payload["download_plan"]["html_concurrency"], 1)
        self.assertEqual(payload["download_plan"]["max_retries"], 0)

        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            task_count = db.execute("SELECT COUNT(*) FROM wizard_tasks").fetchone()[0]
            gates = [row[0] for row in db.execute("SELECT gate FROM wizard_gate_events ORDER BY id")]
        finally:
            db.close()
        self.assertEqual(task_count, 1)
        self.assertEqual(gates, ["input", "mode_decision", "environment", "download"])

    def test_url_goal_downloads_single_article_from_html_fixture(self) -> None:
        output_dir = self.tmp / "single-output"
        _code, payload = self.run_cli(
            "run",
            "下载这篇公众号文章：https://mp.weixin.qq.com/s/demo-url-article-1",
            "--output-dir",
            str(output_dir),
            "--no-assets",
            extra_env={"MOORE_WECHAT_HTML_FIXTURE_DIR": str(HTML_FIXTURES)},
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["failure_count"], 0)
        self.assertTrue((output_dir / "index.csv").exists())
        self.assertTrue((output_dir / "articles.json").exists())
        self.assertTrue((output_dir / "errors.json").exists())
        run_json = json.loads((output_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run_json["success_count"], 1)
        markdown_files = sorted((output_dir / "articles").glob("*.md"))
        self.assertEqual(len(markdown_files), 1)
        markdown = markdown_files[0].read_text(encoding="utf-8")
        self.assertIn("离线 fixture 文章正文", markdown)
        self.assertIn("测试公众号", markdown)

    def test_url_goal_downloads_multiple_articles_and_records_items(self) -> None:
        output_dir = self.tmp / "multi-output"
        _code, payload = self.run_cli(
            "run",
            "下载这些公众号文章：https://mp.weixin.qq.com/s/demo-url-article-1 https://mp.weixin.qq.com/s/demo-url-article-2",
            "--output-dir",
            str(output_dir),
            "--no-assets",
            extra_env={"MOORE_WECHAT_HTML_FIXTURE_DIR": str(HTML_FIXTURES)},
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["success_count"], 2)
        self.assertEqual(len(list((output_dir / "articles").glob("*.md"))), 2)
        articles = json.loads((output_dir / "articles.json").read_text(encoding="utf-8"))
        self.assertEqual([item["title"] for item in articles], ["离线单篇下载测试", "离线多篇下载测试"])
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            items = db.execute(
                """
                SELECT status, COUNT(*)
                FROM download_items
                WHERE run_id = ?
                GROUP BY status
                """,
                (payload["run_id"],),
            ).fetchall()
        finally:
            db.close()
        self.assertEqual(items, [("success", 2)])

    def test_url_goal_dry_run_caps_concurrency_and_retries(self) -> None:
        _code, payload = self.run_cli(
            "run",
            "下载这些文章：https://mp.weixin.qq.com/s/a https://mp.weixin.qq.com/s/b",
            "--dry-run",
            "--html-concurrency",
            "99",
            "--max-retries",
            "99",
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["download_plan"]["html_concurrency"], 4)
        self.assertEqual(payload["download_plan"]["max_retries"], 3)

    def test_config_preferences_drive_url_download_plan(self) -> None:
        _code, config = self.run_cli(
            "config",
            "--html-concurrency",
            "3",
            "--max-retries",
            "2",
            "--overwrite-policy",
            "force",
            "--no-assets",
        )
        self.assertTrue(config["ok"])
        self.assertEqual(config["preferences"]["html_concurrency"], 3)
        self.assertEqual(config["preferences"]["max_retries"], 2)
        self.assertFalse(config["preferences"]["download_assets"])
        self.assertEqual(config["preferences"]["overwrite_policy"], "force")

        _code, payload = self.run_cli(
            "run",
            "下载这些文章：https://mp.weixin.qq.com/s/a https://mp.weixin.qq.com/s/b",
            "--dry-run",
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["download_plan"]["html_concurrency"], 3)
        self.assertEqual(payload["download_plan"]["max_retries"], 2)
        self.assertFalse(payload["download_plan"]["download_assets"])
        self.assertTrue(payload["download_plan"]["force"])
        self.assertEqual(payload["download_plan"]["force_source"], "preference")
        self.assertEqual(payload["download_plan"]["risk_level"], "R4")

        _code, overridden = self.run_cli(
            "run",
            "下载这些文章：https://mp.weixin.qq.com/s/a https://mp.weixin.qq.com/s/b",
            "--dry-run",
            "--download-assets",
            "--force",
        )

        self.assertTrue(overridden["download_plan"]["download_assets"])
        self.assertEqual(overridden["download_plan"]["force_source"], "cli")

    def test_corrupt_download_preferences_fall_back_to_safe_defaults(self) -> None:
        wechat_wizard.init_wizard_db(self.tmp)
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            db.execute(
                "UPDATE user_preferences SET value_json = ? WHERE name = 'download'",
                (json.dumps({"html_concurrency": "abc", "max_retries": "bad", "overwrite_policy": "delete"}),),
            )
            db.commit()
        finally:
            db.close()

        prefs = wechat_wizard.get_download_preferences(self.tmp)

        self.assertEqual(prefs["html_concurrency"], 1)
        self.assertEqual(prefs["max_retries"], 0)
        self.assertEqual(prefs["overwrite_policy"], "skip")

    def test_markdown_downloader_retries_and_preserves_input_order(self) -> None:
        original = wechat_downloader.download_one_markdown_only
        attempts: dict[str, int] = {}

        def fake_download(url: str, output_dir: Path, seq: str, download_assets: bool) -> dict:
            attempts[url] = attempts.get(url, 0) + 1
            if url.endswith("/b") and attempts[url] == 1:
                raise RuntimeError("temporary network failure")
            return {
                "seq": seq,
                "article_id": seq,
                "title": f"title-{seq}",
                "account": "account",
                "source_url": url,
                "markdown_path": f"articles/{seq}.md",
                "image_dir": f"images/{seq}",
                "image_count": 0,
                "status": "success",
                "error": "",
                "absolute_markdown_path": str(output_dir / "articles" / f"{seq}.md"),
                "absolute_image_dir": str(output_dir / "images" / seq),
            }

        try:
            wechat_downloader.download_one_markdown_only = fake_download
            manifest = wechat_downloader.run_markdown_only_download(
                ["https://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com/s/b"],
                self.tmp / "parallel",
                False,
                {"mode": "test"},
                "run_parallel",
                html_concurrency=2,
                max_retries=1,
            )
        finally:
            wechat_downloader.download_one_markdown_only = original

        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["html_concurrency"], 2)
        self.assertEqual(manifest["max_retries"], 1)
        self.assertEqual(manifest["retry_attempt_count"], 1)
        self.assertEqual([item["seq"] for item in manifest["articles"]], ["001", "002"])

    def test_sensitive_url_params_are_scrubbed_from_output_and_sqlite(self) -> None:
        _code, payload = self.run_cli(
            "run",
            "下载：https://mp.weixin.qq.com/s/a?token=abc&pass_ticket=secret&scene=1",
            "--dry-run",
        )

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("pass_ticket", serialized)
        self.assertNotIn("secret", serialized)

        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            stored = "\n".join(str(row[0]) for row in db.execute("SELECT intent_json || result_json FROM wizard_tasks"))
        finally:
            db.close()
        self.assertNotIn("pass_ticket", stored)
        self.assertNotIn("secret", stored)

    def test_history_wording_returns_proxy_confirmation_boundary(self) -> None:
        _code, payload = self.run_cli(
            "run",
            "用这篇文章 URL 获取公众号历史文章：https://mp.weixin.qq.com/s/a?__biz=MzIxNTA1MDEwNg==",
            "--dry-run",
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "need_proxy_confirm")
        self.assertEqual(payload["mode"], "history")
        self.assertIn("profile_ext", payload["open_url"])
        self.assertTrue(payload["open_url"].endswith("#wechat_redirect"))
        self.assertEqual(payload["gate"]["gate"], "proxy")
        self.assertEqual(payload["environment_gate"]["gate"], "environment")
        self.assertIn("mitmdump_available", payload["environment_gate"]["evidence"])
        self.assertIn("history-proxy-setup", payload["environment_gate"]["evidence"]["install_command"])

        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            gates = [
                row[0]
                for row in db.execute(
                    "SELECT gate FROM wizard_gate_events WHERE task_id = ? ORDER BY id",
                    (payload["task_id"],),
                ).fetchall()
            ]
        finally:
            db.close()
        self.assertEqual(gates, ["input", "mode_decision", "environment", "proxy"])

    def test_history_environment_gate_reports_missing_mitmdump(self) -> None:
        original_which = wechat_wizard.shutil.which
        try:
            wechat_wizard.shutil.which = lambda name: None if name == "mitmdump" else original_which(name)
            event = wechat_wizard.gate_history_environment(self.tmp, "task_no_mitm")
        finally:
            wechat_wizard.shutil.which = original_which

        self.assertTrue(event["ok"])
        self.assertFalse(event["evidence"]["mitmdump_available"])
        self.assertEqual(event["evidence"]["mitmdump_path"], "")
        self.assertIn("history-proxy-setup", event["evidence"]["install_command"])
        self.assertIn("Install mitmdump", event["next_action"])

    def test_smoke_history_preflight_persists_evidence(self) -> None:
        _code, payload = self.run_cli(
            "smoke",
            "history-preflight",
            "--url",
            "https://mp.weixin.qq.com/s/a?__biz=MzIxNTA1MDEwNg==",
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], "history-preflight")
        self.assertEqual(payload["status"], "need_proxy_confirm")
        self.assertEqual(payload["evidence"]["open_url_type"], "profile_history")

        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            count = db.execute("SELECT COUNT(*) FROM smoke_runs WHERE name = 'history-preflight' AND ok = 1").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)

    def test_smoke_all_offline_and_list(self) -> None:
        _code, payload = self.run_cli("smoke", "all-offline")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], "all-offline")
        self.assertEqual(len(payload["evidence"]["checks"]), 3)
        self.assertEqual(payload["evidence"]["checks"][0]["name"], "url-fixture")

        _code, listed = self.run_cli("smoke", "list", "--limit", "5")
        self.assertTrue(listed["ok"])
        self.assertGreaterEqual(len(listed["smoke_runs"]), 4)

    def test_smoke_url_fixture_downloads_article_and_persists_evidence(self) -> None:
        _code, payload = self.run_cli("smoke", "url-fixture")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], "url-fixture")
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["evidence"]["success_count"], 1)
        self.assertTrue(Path(payload["evidence"]["run_json"]).exists())
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            count = db.execute("SELECT COUNT(*) FROM smoke_runs WHERE name = 'url-fixture' AND ok = 1").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)

    def test_no_url_returns_blocked_input_gate(self) -> None:
        code, payload = self.run_cli("run", "请帮我处理一下", allow_fail=True)

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["gate"]["gate"], "input")

    def test_generic_history_without_url_or_account_is_not_routed_to_exporter(self) -> None:
        code, payload = self.run_cli("run", "下载公众号历史", allow_fail=True)

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["gate"]["gate"], "input")
        self.assertEqual(payload["gate"]["evidence"]["url_count"], 0)

    def import_exporter_fixture_synced_today(self) -> int:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        account = wechat_exporter.upsert_account(self.tmp, wechat_exporter.normalize_account(data["account"]))["account"]
        rows = [wechat_exporter.normalize_article(item, int(account["id"])) for item in data.get("articles", [])]
        wechat_exporter.upsert_articles(self.tmp, rows)
        db = sqlite3.connect(self.tmp / "exporter.sqlite")
        try:
            db.execute(
                "UPDATE target_accounts SET last_sync_at = ?, synced_count = ?, updated_at = ? WHERE id = ?",
                (wechat_exporter.utc_now(), len(rows), wechat_exporter.utc_now(), int(account["id"])),
            )
            db.commit()
        finally:
            db.close()
        return int(account["id"])

    def test_exporter_list_uses_today_cache_and_returns_article_choice(self) -> None:
        self.import_exporter_fixture_synced_today()

        _code, payload = self.run_cli("run", "列出「哥飞」最近 2 篇让我选")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "need_article_choice")
        self.assertEqual(payload["mode"], "exporter")
        self.assertEqual(payload["account"]["nickname"], "哥飞")
        self.assertEqual([item["title"] for item in payload["articles"]], ["AI 工具出海实战", "公众号运营专家入门"])

    def test_exporter_download_dry_run_selects_latest_from_today_cache(self) -> None:
        self.import_exporter_fixture_synced_today()

        _code, payload = self.run_cli("run", "下载「哥飞」最近 2 篇", "--dry-run")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["mode"], "exporter")
        self.assertEqual(len(payload["selected_article_ids"]), 2)

    def test_exporter_stale_cache_requires_login_before_sync(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        account = wechat_exporter.upsert_account(self.tmp, wechat_exporter.normalize_account(data["account"]))["account"]
        rows = [wechat_exporter.normalize_article(item, int(account["id"])) for item in data.get("articles", [])]
        wechat_exporter.upsert_articles(self.tmp, rows)

        code, payload = self.run_cli("run", "列出「哥飞」最近 2 篇让我选", allow_fail=True)

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "need_login")
        self.assertEqual(payload["mode"], "exporter")

    def test_exporter_account_choice_can_resume_to_article_choice(self) -> None:
        self.import_exporter_fixture_synced_today()
        wechat_exporter.upsert_account(
            self.tmp,
            {
                "fakeid": "fakeid_gefei_plus",
                "nickname": "哥飞精选",
                "alias": "gefei-plus",
                "raw_json": "{}",
            },
        )

        _code, payload = self.run_cli("run", "列出「哥」最近 1 篇让我选")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "need_account_choice")
        task_id = payload["task_id"]
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wizard_choices WHERE task_id = ? AND choice_type = 'account'", (task_id,)).fetchone()[0], 1)
        finally:
            db.close()

        _code, resumed = self.run_cli("resume", task_id, "--choice", "1", "--dry-run")

        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["state"], "need_article_choice")
        self.assertEqual(resumed["resumed_from"], "need_account_choice")
        self.assertEqual(resumed["account"]["nickname"], "哥飞")

    def test_exporter_article_choice_can_resume_with_range(self) -> None:
        self.import_exporter_fixture_synced_today()
        _code, payload = self.run_cli("run", "列出「哥飞」最近 3 篇让我选")

        _code, resumed = self.run_cli("resume", payload["task_id"], "--choice", "1-2", "--dry-run")

        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["state"], "ready")
        self.assertEqual(resumed["resumed_from"], "need_article_choice")
        self.assertEqual(len(resumed["selected_article_ids"]), 2)

    def test_resume_uses_download_preferences_and_allows_asset_override(self) -> None:
        self.import_exporter_fixture_synced_today()
        output_dir = self.tmp / "preferred-resume-output"
        _code, _config = self.run_cli("config", "--output-dir", str(output_dir), "--no-assets")
        _code, payload = self.run_cli("run", "列出「哥飞」最近 2 篇让我选")

        _code, resumed = self.run_cli("resume", payload["task_id"], "--choice", "1", "--dry-run")

        self.assertEqual(resumed["download_plan"]["output_dir"], str(output_dir))
        self.assertFalse(resumed["download_plan"]["download_assets"])

        _code, payload = self.run_cli("run", "列出「哥飞」最近 2 篇让我选")
        _code, overridden = self.run_cli("resume", payload["task_id"], "--choice", "1", "--dry-run", "--download-assets")
        self.assertTrue(overridden["download_plan"]["download_assets"])

    def test_bad_article_choice_preserves_waiting_state(self) -> None:
        self.import_exporter_fixture_synced_today()
        _code, payload = self.run_cli("run", "列出「哥飞」最近 3 篇让我选")

        code, resumed = self.run_cli("resume", payload["task_id"], "--choice", "不存在的标题", allow_fail=True)

        self.assertEqual(code, 1)
        self.assertFalse(resumed["ok"])
        self.assertEqual(resumed["state"], "need_article_choice")
        self.assertIn("choices", resumed)

    def test_exporter_auth_gate_starts_qr_login_session(self) -> None:
        original_start = wechat_wizard.wechat_exporter.start_qr_login

        def fake_start_qr_login(base: Path, base_url: str) -> dict:
            qrcode_path = base / "login" / "fake-login.qrcode.png"
            qrcode_path.parent.mkdir(parents=True, exist_ok=True)
            qrcode_path.write_bytes(b"\x89PNG\r\n")
            return {
                "ok": True,
                "login_id": "fake-login",
                "base_url": base_url,
                "qrcode_path": str(qrcode_path),
                "expires_at": "2099-01-01T00:00:00+00:00",
            }

        try:
            wechat_wizard.wechat_exporter.start_qr_login = fake_start_qr_login
            event = wechat_wizard.gate_exporter_auth(self.tmp, "task_qr", required=True)
        finally:
            wechat_wizard.wechat_exporter.start_qr_login = original_start

        self.assertFalse(event["ok"])
        self.assertEqual(event["state"], "need_login")
        self.assertEqual(event["evidence"]["login_id"], "fake-login")
        self.assertTrue(event["evidence"]["qrcode_path"].endswith(".png"))
        self.assertEqual(event["evidence"]["poll_after_seconds"], 5)

    def test_gate_evidence_scrubs_sensitive_free_text(self) -> None:
        event = wechat_wizard.record_gate(
            self.tmp,
            "task_sensitive",
            "auth",
            "need_login",
            False,
            {"message": "bad auth-key=abc token=secret pass_ticket=hidden"},
            "cookie=oops",
        )
        serialized = json.dumps(event, ensure_ascii=False)
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            stored = "\n".join(str(row[0]) for row in db.execute("SELECT evidence_json || error FROM wizard_gate_events"))
        finally:
            db.close()
        self.assertNotIn("auth-key", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("pass_ticket", stored)
        self.assertNotIn("cookie", stored)

    def test_login_status_waiting_is_not_success_gate(self) -> None:
        original_status = wechat_wizard.wechat_exporter.qr_login_status

        def fake_status(base: Path, login_id: str) -> dict:
            return {"ok": True, "login_id": login_id, "status": "waiting_for_scan", "ready_to_complete": False}

        try:
            wechat_wizard.wechat_exporter.qr_login_status = fake_status
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = wechat_wizard.login_status(Namespace(runtime_dir=str(self.tmp), task_id="task_wait", login_id="fake-login"))
        finally:
            wechat_wizard.wechat_exporter.qr_login_status = original_status

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "need_login")
        self.assertFalse(payload["gate"]["ok"])

    def test_login_id_lookup_requires_task_bound_session(self) -> None:
        path = wechat_exporter.login_session_path(self.tmp, "login-other")
        path.parent.mkdir(parents=True, exist_ok=True)
        wechat_exporter.write_json_file(
            path,
            {
                "login_id": "login-other",
                "task_id": "other-task",
                "status": "waiting_for_scan",
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )
        wechat_wizard.save_task(self.tmp, "task_without_login", {"mode": "exporter"}, "need_login", "exporter", result={})

        with self.assertRaises(RuntimeError):
            wechat_wizard.login_id_for_task(self.tmp, "task_without_login")

    def test_login_complete_resumes_parent_task_after_auth(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        account = wechat_exporter.upsert_account(self.tmp, wechat_exporter.normalize_account(data["account"]))["account"]
        rows = [wechat_exporter.normalize_article(item, int(account["id"])) for item in data.get("articles", [])]
        wechat_exporter.upsert_articles(self.tmp, rows)
        code, payload = self.run_cli("run", "列出「哥飞」最近 2 篇让我选", allow_fail=True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["state"], "need_login")

        original_complete = wechat_wizard.wechat_exporter.complete_qr_login
        original_sync = wechat_wizard.wechat_exporter.sync_account_articles

        def fake_complete(base: Path, login_id: str, profile: str = "") -> dict:
            result = wechat_exporter.upsert_login_profile(
                base,
                wechat_exporter.DEFAULT_BASE_URL,
                "fixture-login-value",
                profile or "default",
                "2099-01-01T00:00:00+00:00",
                True,
            )
            return {"ok": True, "profile_id": result["profile_id"], "login_id": login_id}

        def fake_sync(base: Path, account_id: int, limit: int, keyword: str = "", profile: str = "") -> dict:
            db = sqlite3.connect(base / "exporter.sqlite")
            try:
                db.execute(
                    "UPDATE target_accounts SET last_sync_at = ?, synced_count = ?, updated_at = ? WHERE id = ?",
                    (wechat_exporter.utc_now(), len(rows), wechat_exporter.utc_now(), account_id),
                )
                db.commit()
            finally:
                db.close()
            return {"ok": True, "account_id": account_id, "fetched_count": len(rows), "upserted_count": len(rows)}

        try:
            wechat_wizard.wechat_exporter.complete_qr_login = fake_complete
            wechat_wizard.wechat_exporter.sync_account_articles = fake_sync
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = wechat_wizard.login_complete(
                    Namespace(
                        runtime_dir=str(self.tmp),
                        task_id=payload["task_id"],
                        login_id="fake-login",
                        profile="default",
                        output_dir="",
                        no_assets=False,
                        dry_run=True,
                    )
                )
        finally:
            wechat_wizard.wechat_exporter.complete_qr_login = original_complete
            wechat_wizard.wechat_exporter.sync_account_articles = original_sync

        resumed = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["state"], "need_article_choice")
        self.assertEqual(resumed["resumed_from"], "need_login")
        self.assertEqual(resumed["login"]["login_id"], "fake-login")
        task = wechat_wizard.load_task(self.tmp, payload["task_id"])
        self.assertEqual(task["result"]["resumed_from"], "need_login")

    def test_login_complete_uses_download_preferences_for_direct_download_resume(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        account = wechat_exporter.upsert_account(self.tmp, wechat_exporter.normalize_account(data["account"]))["account"]
        rows = [wechat_exporter.normalize_article(item, int(account["id"])) for item in data.get("articles", [])]
        wechat_exporter.upsert_articles(self.tmp, rows)
        output_dir = self.tmp / "preferred-login-output"
        self.run_cli("config", "--output-dir", str(output_dir), "--no-assets")
        code, payload = self.run_cli("run", "下载「哥飞」最近 2 篇", allow_fail=True)
        self.assertEqual(code, 1)
        self.assertEqual(payload["state"], "need_login")

        original_complete = wechat_wizard.wechat_exporter.complete_qr_login
        original_sync = wechat_wizard.wechat_exporter.sync_account_articles

        def fake_complete(base: Path, login_id: str, profile: str = "") -> dict:
            result = wechat_exporter.upsert_login_profile(
                base,
                wechat_exporter.DEFAULT_BASE_URL,
                "fixture-login-value",
                profile or "default",
                "2099-01-01T00:00:00+00:00",
                True,
            )
            return {"ok": True, "profile_id": result["profile_id"], "login_id": login_id}

        def fake_sync(base: Path, account_id: int, limit: int, keyword: str = "", profile: str = "") -> dict:
            db = sqlite3.connect(base / "exporter.sqlite")
            try:
                db.execute(
                    "UPDATE target_accounts SET last_sync_at = ?, synced_count = ?, updated_at = ? WHERE id = ?",
                    (wechat_exporter.utc_now(), len(rows), wechat_exporter.utc_now(), account_id),
                )
                db.commit()
            finally:
                db.close()
            return {"ok": True, "account_id": account_id, "fetched_count": len(rows), "upserted_count": len(rows)}

        try:
            wechat_wizard.wechat_exporter.complete_qr_login = fake_complete
            wechat_wizard.wechat_exporter.sync_account_articles = fake_sync
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = wechat_wizard.login_complete(
                    Namespace(
                        runtime_dir=str(self.tmp),
                        task_id=payload["task_id"],
                        login_id="fake-login",
                        profile="default",
                        output_dir="",
                        download_assets=None,
                        dry_run=True,
                    )
                )
        finally:
            wechat_wizard.wechat_exporter.complete_qr_login = original_complete
            wechat_wizard.wechat_exporter.sync_account_articles = original_sync

        resumed = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(resumed["state"], "ready")
        self.assertEqual(resumed["download_plan"]["output_dir"], str(output_dir))
        self.assertFalse(resumed["download_plan"]["download_assets"])

    def test_download_manifest_records_run_and_items(self) -> None:
        manifest = {
            "run_id": "run_test",
            "output_dir": str(self.tmp / "out"),
            "success_count": 1,
            "failure_count": 1,
            "articles": [
                {
                    "article_id": "article-ok",
                    "source_url": "https://mp.weixin.qq.com/s/ok",
                    "status": "success",
                    "markdown_path": "articles/001.md",
                    "image_dir": "images/001",
                }
            ],
            "failed": [
                {
                    "source_url": "https://mp.weixin.qq.com/s/fail?pass_ticket=secret",
                    "status": "failed",
                    "error": "bad pass_ticket=secret",
                }
            ],
        }

        wechat_wizard.record_download_manifest(self.tmp, "task_run", "url", manifest, "failed_recoverable")

        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            run = db.execute("SELECT status, success_count, failed_count FROM download_runs WHERE run_id = 'run_test'").fetchone()
            item_count = db.execute("SELECT COUNT(*) FROM download_items WHERE run_id = 'run_test'").fetchone()[0]
            stored = "\n".join(str(row[0]) for row in db.execute("SELECT url || error FROM download_items"))
        finally:
            db.close()
        self.assertEqual(run, ("failed_recoverable", 1, 1))
        self.assertEqual(item_count, 2)
        self.assertNotIn("pass_ticket", stored)
        self.assertNotIn("secret", stored)

    def test_url_mode_skips_already_downloaded_for_same_task(self) -> None:
        task_id = "task_skip"
        url = "https://mp.weixin.qq.com/s/already"
        wechat_wizard.record_download_manifest(
            self.tmp,
            task_id,
            "url",
            {
                "run_id": "run_previous",
                "output_dir": str(self.tmp / "previous"),
                "success_count": 1,
                "failure_count": 0,
                "articles": [{"source_url": url, "status": "success"}],
                "failed": [],
            },
            "done",
        )
        intent = wechat_wizard.parse_intent(f"下载：{url}")
        wechat_wizard.decide_mode(intent)
        existing_article = self.tmp / "out" / "articles" / "previous.md"
        existing_article.parent.mkdir(parents=True, exist_ok=True)
        existing_article.write_text("already here\n", encoding="utf-8")

        result = wechat_wizard.run_url_mode(self.tmp, task_id, intent, str(self.tmp / "out"), False, False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        run_json = json.loads((self.tmp / "out" / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run_json["skipped_count"], 1)
        self.assertIn("duration_ms", run_json)
        self.assertEqual(run_json["html_fetch_count"], 0)
        self.assertEqual(run_json["image_fetch_count"], 0)
        self.assertEqual(run_json["retry_count"], 0)
        self.assertIn("verify", run_json["gate_summary"]["gates"])
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            skipped = db.execute(
                """
                SELECT COUNT(*), MAX(previous_run_id)
                FROM download_items di
                JOIN download_runs dr ON dr.run_id = di.run_id
                WHERE dr.task_id = ? AND di.status = 'skipped'
                """,
                (task_id,),
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(skipped, (1, "run_previous"))

    def test_url_mode_skips_cross_task_previous_success_with_evidence(self) -> None:
        url = "https://mp.weixin.qq.com/s/cross-task"
        previous_dir = self.tmp / "previous-output"
        previous_article = previous_dir / "articles" / "001.md"
        previous_article.parent.mkdir(parents=True, exist_ok=True)
        previous_article.write_text("already downloaded\n", encoding="utf-8")
        wechat_wizard.record_download_manifest(
            self.tmp,
            "task_previous",
            "url",
            {
                "run_id": "run_previous_cross",
                "output_dir": str(previous_dir),
                "success_count": 1,
                "failure_count": 0,
                "articles": [{"source_url": url, "status": "success", "markdown_path": "articles/001.md"}],
                "failed": [],
            },
            "done",
        )
        intent = wechat_wizard.parse_intent(f"下载：{url}")
        wechat_wizard.decide_mode(intent)

        result = wechat_wizard.run_url_mode(self.tmp, "task_new", intent, str(self.tmp / "out"), False, False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            row = db.execute(
                """
                SELECT di.status, di.error, di.url
                FROM download_items di
                JOIN download_runs dr ON dr.run_id = di.run_id
                WHERE dr.task_id = 'task_new'
                """
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(row, ("skipped", "already downloaded previously", url))
        run_json = json.loads((self.tmp / "out" / "run.json").read_text(encoding="utf-8"))
        self.assertTrue(run_json["gate_summary"]["latest"]["verify"]["ok"])

    def test_verify_run_requires_sidecar_json_and_skip_evidence(self) -> None:
        output_dir = self.tmp / "verify-output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "index.csv").write_text("seq,status\n", encoding="utf-8")
        (output_dir / "run.json").write_text("{}\n", encoding="utf-8")
        (output_dir / "articles.json").write_text("{}\n", encoding="utf-8")
        (output_dir / "errors.json").write_text("[]\n", encoding="utf-8")

        result = wechat_wizard.verify_run(output_dir, {"success_count": 0, "skipped": [{"source_url": "https://mp.weixin.qq.com/s/no-proof"}]})

        self.assertFalse(result["ok"])
        self.assertFalse(result["articles_json_ok"])
        self.assertFalse(result["skipped_evidence_ok"])

    def test_verify_run_rejects_fake_previous_run_id_without_db_or_file_evidence(self) -> None:
        output_dir = self.tmp / "verify-fake-previous"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "index.csv").write_text("seq,status\n", encoding="utf-8")
        (output_dir / "run.json").write_text("{}\n", encoding="utf-8")
        (output_dir / "articles.json").write_text("[]\n", encoding="utf-8")
        (output_dir / "errors.json").write_text("[]\n", encoding="utf-8")

        result = wechat_wizard.verify_run(
            output_dir,
            {
                "success_count": 0,
                "skipped": [
                    {
                        "source_url": "https://mp.weixin.qq.com/s/no-proof",
                        "previous_run_id": "fake-run",
                    }
                ],
            },
            self.tmp,
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["skipped_evidence_ok"])

    def test_verify_run_accepts_previous_run_id_with_db_success_evidence(self) -> None:
        output_dir = self.tmp / "verify-real-previous"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "index.csv").write_text("seq,status\n", encoding="utf-8")
        (output_dir / "run.json").write_text("{}\n", encoding="utf-8")
        (output_dir / "articles.json").write_text("[]\n", encoding="utf-8")
        (output_dir / "errors.json").write_text("[]\n", encoding="utf-8")
        url = "https://mp.weixin.qq.com/s/proof"
        wechat_wizard.record_download_manifest(
            self.tmp,
            "task_previous_proof",
            "url",
            {
                "run_id": "run_previous_proof",
                "output_dir": str(self.tmp / "previous-proof"),
                "success_count": 1,
                "failure_count": 0,
                "articles": [{"source_url": url, "status": "success"}],
                "failed": [],
            },
            "done",
        )

        result = wechat_wizard.verify_run(
            output_dir,
            {
                "success_count": 0,
                "skipped": [
                    {
                        "source_url": url,
                        "previous_run_id": "run_previous_proof",
                    }
                ],
            },
            self.tmp,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped_evidence_ok"])

    def test_retry_failed_urls_uses_failed_items(self) -> None:
        task_id = "task_retry"
        url = "https://mp.weixin.qq.com/s/retry-me"
        intent = wechat_wizard.parse_intent(f"下载：{url}")
        wechat_wizard.save_task(self.tmp, task_id, intent, "failed_recoverable", "url", result={})
        wechat_wizard.record_download_manifest(
            self.tmp,
            task_id,
            "url",
            {
                "run_id": "run_failed",
                "output_dir": str(self.tmp / "failed"),
                "success_count": 0,
                "failure_count": 1,
                "articles": [],
                "failed": [{"source_url": url, "status": "failed", "error": "boom"}],
            },
            "failed_recoverable",
        )
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = wechat_wizard.retry_failed(
                Namespace(runtime_dir=str(self.tmp), task_id=task_id, output_dir=str(self.tmp / "retry"), no_assets=False, dry_run=True)
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["retried_count"], 1)
        self.assertEqual(payload["download_plan"]["selected_article_count"], 1)
        task = wechat_wizard.load_task(self.tmp, task_id)
        self.assertEqual(task["result"]["retried_count"], 1)

    def test_output_dir_lock_blocks_download_and_doctor_clears_stale(self) -> None:
        output_dir = self.tmp / "locked-output"
        lock_name = "download:" + hashlib.sha256(str(output_dir.resolve()).encode("utf-8")).hexdigest()[:16]
        wechat_wizard.init_wizard_db(self.tmp)
        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            db.execute(
                "INSERT INTO wizard_locks (name, owner, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (lock_name, "other", "2099-01-01T00:00:00+00:00", wechat_exporter.utc_now(), wechat_exporter.utc_now()),
            )
            db.commit()
        finally:
            db.close()
        intent = wechat_wizard.parse_intent("下载：https://mp.weixin.qq.com/s/locked")
        wechat_wizard.decide_mode(intent)

        result = wechat_wizard.run_url_mode(self.tmp, "task_locked", intent, str(output_dir), False, False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed_recoverable")
        self.assertEqual(result["gate"]["gate"], "download")

        db = sqlite3.connect(self.tmp / "wizard.sqlite")
        try:
            db.execute("UPDATE wizard_locks SET expires_at = ? WHERE name = ?", ("2000-01-01T00:00:00+00:00", lock_name))
            db.commit()
        finally:
            db.close()
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = wechat_wizard.doctor(Namespace(runtime_dir=str(self.tmp), clear_stale_locks=True))
        payload = json.loads(stdout.getvalue())
        self.assertIn(code, {0, 1})
        self.assertGreaterEqual(payload["checks"]["stale_locks"]["cleared_count"], 1)

    def test_doctor_checks_local_prerequisites(self) -> None:
        code, payload = self.run_cli("doctor", allow_fail=True)

        self.assertIn(code, {0, 1})
        self.assertIn("sqlite", payload["checks"])
        self.assertIn("output_dir", payload["checks"])
        self.assertIn("keychain", payload["checks"])
        self.assertIn("mitmdump", payload["checks"])
        self.assertIn("exporter_auth", payload["checks"])
        self.assertIn("proxy_state", payload["checks"])
        self.assertIn("latest_run_manifest", payload["checks"])

    def test_doctor_system_proxy_allows_external_local_proxy(self) -> None:
        original_platform = wechat_wizard.sys.platform
        original_choose = wechat_wizard.choose_network_service
        original_get = wechat_wizard.get_network_proxy_state
        try:
            wechat_wizard.sys.platform = "darwin"
            wechat_wizard.choose_network_service = lambda service="": "Wi-Fi"
            wechat_wizard.get_network_proxy_state = lambda service: {
                "service": service,
                "web": {"enabled_bool": True, "server": "127.0.0.1", "port": "10808"},
                "secure_web": {"enabled_bool": True, "server": "127.0.0.1", "port": "10808"},
            }

            result = wechat_wizard.check_system_proxy_recoverability(self.tmp)
        finally:
            wechat_wizard.sys.platform = original_platform
            wechat_wizard.choose_network_service = original_choose
            wechat_wizard.get_network_proxy_state = original_get

        self.assertTrue(result["ok"])
        self.assertFalse(result["points_to_history_proxy"])
        self.assertEqual(result["web_proxy"], "127.0.0.1:10808")

    def test_doctor_system_proxy_flags_history_proxy_without_saved_state(self) -> None:
        original_platform = wechat_wizard.sys.platform
        original_choose = wechat_wizard.choose_network_service
        original_get = wechat_wizard.get_network_proxy_state
        try:
            wechat_wizard.sys.platform = "darwin"
            wechat_wizard.choose_network_service = lambda service="": "Wi-Fi"
            wechat_wizard.get_network_proxy_state = lambda service: {
                "service": service,
                "web": {"enabled_bool": True, "server": "127.0.0.1", "port": "8899"},
                "secure_web": {"enabled_bool": True, "server": "127.0.0.1", "port": "8899"},
            }

            result = wechat_wizard.check_system_proxy_recoverability(self.tmp)
        finally:
            wechat_wizard.sys.platform = original_platform
            wechat_wizard.choose_network_service = original_choose
            wechat_wizard.get_network_proxy_state = original_get

        self.assertFalse(result["ok"])
        self.assertTrue(result["points_to_history_proxy"])
        self.assertFalse(result["saved_state_exists"])

    def test_doctor_system_proxy_marks_history_proxy_recoverable_with_saved_state(self) -> None:
        original_platform = wechat_wizard.sys.platform
        original_choose = wechat_wizard.choose_network_service
        original_get = wechat_wizard.get_network_proxy_state
        state_path = wechat_downloader.system_proxy_state_path(self.tmp)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"service": "Wi-Fi", "new": {"host": "127.0.0.1", "port": 8899}, "previous": {"web": {}, "secure_web": {}}}),
            encoding="utf-8",
        )
        try:
            wechat_wizard.sys.platform = "darwin"
            wechat_wizard.choose_network_service = lambda service="": "Wi-Fi"
            wechat_wizard.get_network_proxy_state = lambda service: {
                "service": service,
                "web": {"enabled_bool": True, "server": "localhost", "port": "8899"},
                "secure_web": {"enabled_bool": False, "server": "", "port": "0"},
            }

            result = wechat_wizard.check_system_proxy_recoverability(self.tmp)
        finally:
            wechat_wizard.sys.platform = original_platform
            wechat_wizard.choose_network_service = original_choose
            wechat_wizard.get_network_proxy_state = original_get

        self.assertTrue(result["ok"])
        self.assertTrue(result["points_to_history_proxy"])
        self.assertTrue(result["saved_state_exists"])
        self.assertTrue(result["saved_state_matches"])
        self.assertEqual(result["recoverability"], "saved_state")
        self.assertIn("history-proxy-disable", result["next_action"])

    def test_doctor_system_proxy_does_not_trust_stale_saved_state(self) -> None:
        original_platform = wechat_wizard.sys.platform
        original_choose = wechat_wizard.choose_network_service
        original_get = wechat_wizard.get_network_proxy_state
        state_path = wechat_downloader.system_proxy_state_path(self.tmp)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"service": "Ethernet", "new": {"host": "127.0.0.1", "port": 8888}, "previous": {"web": {}, "secure_web": {}}}),
            encoding="utf-8",
        )
        try:
            wechat_wizard.sys.platform = "darwin"
            wechat_wizard.choose_network_service = lambda service="": "Wi-Fi"
            wechat_wizard.get_network_proxy_state = lambda service: {
                "service": service,
                "web": {"enabled_bool": True, "server": "127.0.0.1", "port": "8899"},
                "secure_web": {"enabled_bool": False, "server": "", "port": "0"},
            }

            result = wechat_wizard.check_system_proxy_recoverability(self.tmp)
        finally:
            wechat_wizard.sys.platform = original_platform
            wechat_wizard.choose_network_service = original_choose
            wechat_wizard.get_network_proxy_state = original_get

        self.assertFalse(result["ok"])
        self.assertTrue(result["points_to_history_proxy"])
        self.assertTrue(result["saved_state_exists"])
        self.assertFalse(result["saved_state_matches"])
        self.assertEqual(result["recoverability"], "unknown")
        self.assertNotIn("history-proxy-disable --yes", result["next_action"])

    def test_doctor_system_proxy_requires_previous_restore_shape(self) -> None:
        original_platform = wechat_wizard.sys.platform
        original_choose = wechat_wizard.choose_network_service
        original_get = wechat_wizard.get_network_proxy_state
        state_path = wechat_downloader.system_proxy_state_path(self.tmp)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"service": "Wi-Fi", "new": {"host": "127.0.0.1", "port": 8899}, "previous": {}}),
            encoding="utf-8",
        )
        try:
            wechat_wizard.sys.platform = "darwin"
            wechat_wizard.choose_network_service = lambda service="": "Wi-Fi"
            wechat_wizard.get_network_proxy_state = lambda service: {
                "service": service,
                "web": {"enabled_bool": True, "server": "127.0.0.1", "port": "8899"},
                "secure_web": {"enabled_bool": False, "server": "", "port": "0"},
            }

            result = wechat_wizard.check_system_proxy_recoverability(self.tmp)
        finally:
            wechat_wizard.sys.platform = original_platform
            wechat_wizard.choose_network_service = original_choose
            wechat_wizard.get_network_proxy_state = original_get

        self.assertFalse(result["ok"])
        self.assertTrue(result["points_to_history_proxy"])
        self.assertTrue(result["saved_state_exists"])
        self.assertFalse(result["saved_state_matches"])
        self.assertEqual(result["recoverability"], "unknown")
        self.assertNotIn("history-proxy-disable --yes", result["next_action"])

    def test_doctor_reports_mitmdump_install_command_when_missing(self) -> None:
        original_which = wechat_wizard.shutil.which
        try:
            wechat_wizard.shutil.which = lambda name: None if name == "mitmdump" else original_which(name)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = wechat_wizard.doctor(Namespace(runtime_dir=str(self.tmp), clear_stale_locks=False))
        finally:
            wechat_wizard.shutil.which = original_which

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["checks"]["mitmdump"]["ok"])
        self.assertIn("history-proxy-setup", payload["checks"]["mitmdump"]["install_command"])

    def test_doctor_output_scrubs_sensitive_profile_fields(self) -> None:
        original_get_active_profile = wechat_wizard.wechat_exporter.get_active_profile
        try:
            wechat_wizard.wechat_exporter.get_active_profile = lambda base: {
                "display_name": "default auth-key=abc token=secret pass_ticket=hidden",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
            stdout = StringIO()
            with redirect_stdout(stdout):
                wechat_wizard.doctor(Namespace(runtime_dir=str(self.tmp), clear_stale_locks=False))
        finally:
            wechat_wizard.wechat_exporter.get_active_profile = original_get_active_profile

        serialized = stdout.getvalue()
        self.assertNotIn("auth-key", serialized)
        self.assertNotIn("pass_ticket", serialized)
        self.assertNotIn("secret", serialized)

    def test_doctor_checks_latest_run_manifest_completeness(self) -> None:
        output_dir = self.tmp / "latest-run"
        wechat_wizard.record_download_manifest(
            self.tmp,
            "task_latest",
            "url",
            {
                "run_id": "run_latest",
                "output_dir": str(output_dir),
                "success_count": 1,
                "failure_count": 0,
                "articles": [{"source_url": "https://mp.weixin.qq.com/s/a", "status": "success"}],
                "failed": [],
            },
            "done",
        )

        missing = wechat_wizard.check_latest_run_manifest(self.tmp)
        self.assertFalse(missing["ok"])
        self.assertFalse(missing["files"]["run_json"])

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "run.json").write_text(json.dumps({"run_id": "run_latest"}), encoding="utf-8")
        (output_dir / "index.csv").write_text("seq,status\n", encoding="utf-8")
        (output_dir / "articles.json").write_text("[]\n", encoding="utf-8")
        (output_dir / "errors.json").write_text("[]\n", encoding="utf-8")

        complete = wechat_wizard.check_latest_run_manifest(self.tmp)
        self.assertTrue(complete["ok"])
        self.assertTrue(complete["run_id_matches"])

    def test_history_proxy_enable_requires_confirmation(self) -> None:
        original_choose = wechat_downloader.choose_network_service
        try:
            wechat_downloader.choose_network_service = lambda service="": "Wi-Fi"
            payload = wechat_downloader.enable_system_proxy(self.tmp, "", "127.0.0.1", 8899, False)
        finally:
            wechat_downloader.choose_network_service = original_choose

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["service"], "Wi-Fi")

    def test_history_proxy_restore_uses_saved_previous_state(self) -> None:
        original_choose = wechat_downloader.choose_network_service
        original_get = wechat_downloader.get_network_proxy_state
        original_run = wechat_downloader.run_networksetup
        commands: list[list[str]] = []

        def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        try:
            wechat_downloader.choose_network_service = lambda service="": service or "Wi-Fi"
            wechat_downloader.get_network_proxy_state = lambda service: {
                "service": service,
                "web": {"enabled_bool": True, "server": "127.0.0.1", "port": "10808"},
                "secure_web": {"enabled_bool": False, "server": "", "port": "0"},
            }
            wechat_downloader.run_networksetup = fake_run

            enabled = wechat_downloader.enable_system_proxy(self.tmp, "", "127.0.0.1", 8899, True)
            restored = wechat_downloader.disable_system_proxy(self.tmp, yes=True)
        finally:
            wechat_downloader.choose_network_service = original_choose
            wechat_downloader.get_network_proxy_state = original_get
            wechat_downloader.run_networksetup = original_run

        self.assertTrue(enabled["ok"])
        self.assertTrue(restored["ok"])
        self.assertTrue(restored["restored"])
        self.assertIn(["-setwebproxy", "Wi-Fi", "127.0.0.1", "8899"], commands)
        self.assertIn(["-setwebproxy", "Wi-Fi", "127.0.0.1", "10808"], commands)
        self.assertIn(["-setsecurewebproxystate", "Wi-Fi", "off"], commands)
        saved = json.loads(wechat_downloader.system_proxy_state_path(self.tmp).read_text(encoding="utf-8"))
        self.assertIn("restored_at", saved)

    def test_history_proxy_start_reuses_existing_port_for_new_session(self) -> None:
        old_session = {
            "session_id": "old-session",
            "account_id": "old",
            "account_name": "Old Account",
            "history_csv": str(self.tmp / "old.csv"),
            "history_json": str(self.tmp / "old.json"),
        }
        new_session = {
            "session_id": "new-session",
            "account_id": "new",
            "account_name": "New Account",
            "history_csv": str(self.tmp / "new.csv"),
            "history_json": str(self.tmp / "new.json"),
        }
        wechat_downloader.save_history_session(self.tmp, old_session)
        wechat_downloader.save_history_session(self.tmp, new_session)
        old_state_path = wechat_downloader.session_proxy_state_path(self.tmp, "old-session")
        wechat_downloader.write_json(
            old_state_path,
            {
                "ok": True,
                "status": "running",
                "adapter": "wechat-history-proxy",
                "session_id": "old-session",
                "pid": 1234,
                "port": 8899,
                "proxy": "127.0.0.1:8899",
                "upstream_proxy": "http://127.0.0.1:10808",
                "log": str(self.tmp / "old.proxy.log"),
            },
        )
        original_process_running = wechat_downloader.process_running
        try:
            wechat_downloader.process_running = lambda pid: int(pid) == 1234
            result = wechat_downloader.start_history_proxy(self.tmp, new_session, 8899, 100, "auto")
        finally:
            wechat_downloader.process_running = original_process_running

        self.assertTrue(result["ok"])
        self.assertTrue(result["reused_existing_proxy"])
        self.assertEqual(result["pid"], 1234)
        self.assertEqual(result["session_id"], "new-session")
        active = wechat_downloader.read_active_proxy_session(self.tmp)
        self.assertEqual(active["session_id"], "new-session")
        new_state = wechat_downloader.read_json(wechat_downloader.session_proxy_state_path(self.tmp, "new-session"))
        self.assertTrue(new_state["active_session_switched"])
        self.assertEqual(new_state["switched_from_session_id"], "old-session")

    def test_history_proxy_stop_does_not_kill_process_reused_by_active_session(self) -> None:
        old_session = {"session_id": "old-session", "account_id": "old", "account_name": "Old"}
        new_session = {"session_id": "new-session", "account_id": "new", "account_name": "New"}
        old_state_path = wechat_downloader.session_proxy_state_path(self.tmp, "old-session")
        wechat_downloader.write_json(
            old_state_path,
            {"adapter": "wechat-history-proxy", "session_id": "old-session", "pid": 1234, "port": 8899},
        )
        wechat_downloader.write_active_proxy_session(
            self.tmp,
            new_session,
            8899,
            1234,
            "http://127.0.0.1:10808",
            wechat_downloader.session_proxy_state_path(self.tmp, "new-session"),
        )
        original_process_running = wechat_downloader.process_running
        try:
            wechat_downloader.process_running = lambda pid: int(pid) == 1234
            result = wechat_downloader.stop_history_proxy(self.tmp, old_session["session_id"])
        finally:
            wechat_downloader.process_running = original_process_running

        self.assertTrue(result["ok"])
        self.assertFalse(result["stopped"])
        self.assertEqual(result["active_session_id"], "new-session")
        old_state = wechat_downloader.read_json(old_state_path)
        self.assertEqual(old_state["status"], "detached")

    def test_history_proxy_stop_refuses_when_system_proxy_points_to_port(self) -> None:
        state_path = wechat_downloader.session_proxy_state_path(self.tmp, "active-session")
        wechat_downloader.write_json(
            state_path,
            {"adapter": "wechat-history-proxy", "session_id": "active-session", "pid": 1234, "port": 8899},
        )
        original_platform = wechat_downloader.sys.platform
        original_choose = wechat_downloader.choose_network_service
        original_get = wechat_downloader.get_network_proxy_state
        original_process_running = wechat_downloader.process_running
        try:
            wechat_downloader.sys.platform = "darwin"
            wechat_downloader.choose_network_service = lambda service="": "Wi-Fi"
            wechat_downloader.get_network_proxy_state = lambda service: {
                "service": service,
                "web": {"enabled_bool": True, "server": "127.0.0.1", "port": "8899"},
                "secure_web": {"enabled_bool": True, "server": "127.0.0.1", "port": "8899"},
            }
            wechat_downloader.process_running = lambda pid: int(pid) == 1234
            result = wechat_downloader.stop_history_proxy(self.tmp, "active-session")
        finally:
            wechat_downloader.sys.platform = original_platform
            wechat_downloader.choose_network_service = original_choose
            wechat_downloader.get_network_proxy_state = original_get
            wechat_downloader.process_running = original_process_running

        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_proxy_restore"])
        self.assertIn("history-proxy-disable", result["next_step"])

    def test_history_mitm_addon_reads_active_session_pointer(self) -> None:
        session_a = {
            "session_id": "session-a",
            "account_id": "a",
            "account_name": "A",
            "history_csv": str(self.tmp / "a.csv"),
            "history_json": str(self.tmp / "a.json"),
        }
        session_b = {
            "session_id": "session-b",
            "account_id": "b",
            "account_name": "B",
            "history_csv": str(self.tmp / "b.csv"),
            "history_json": str(self.tmp / "b.json"),
        }
        wechat_downloader.save_history_session(self.tmp, session_a)
        wechat_downloader.save_history_session(self.tmp, session_b)
        wechat_downloader.write_json(
            wechat_downloader.active_proxy_session_path(self.tmp),
            {"session_id": "session-b", "pid": 1234, "port": 8899},
        )
        original_runtime = os.environ.get("MOORE_WECHAT_RUNTIME_DIR")
        original_session = os.environ.get("MOORE_WECHAT_SESSION_ID")
        original_limit = os.environ.get("MOORE_WECHAT_HISTORY_LIMIT")
        try:
            os.environ["MOORE_WECHAT_RUNTIME_DIR"] = str(self.tmp)
            os.environ["MOORE_WECHAT_SESSION_ID"] = "session-a"
            os.environ["MOORE_WECHAT_HISTORY_LIMIT"] = "100"
            addon = importlib.import_module("wechat_history_mitm_addon")
            addon = importlib.reload(addon)
            capture = addon.WeChatHistoryCapture()
            context = capture.session_context()
        finally:
            if original_runtime is None:
                os.environ.pop("MOORE_WECHAT_RUNTIME_DIR", None)
            else:
                os.environ["MOORE_WECHAT_RUNTIME_DIR"] = original_runtime
            if original_session is None:
                os.environ.pop("MOORE_WECHAT_SESSION_ID", None)
            else:
                os.environ["MOORE_WECHAT_SESSION_ID"] = original_session
            if original_limit is None:
                os.environ.pop("MOORE_WECHAT_HISTORY_LIMIT", None)
            else:
                os.environ["MOORE_WECHAT_HISTORY_LIMIT"] = original_limit

        self.assertEqual(context["session_id"], "session-b")
        self.assertEqual(context["session"]["account_name"], "B")

    def test_history_mitm_addon_extracts_embedded_msg_list_html(self) -> None:
        original_runtime = os.environ.get("MOORE_WECHAT_RUNTIME_DIR")
        original_session = os.environ.get("MOORE_WECHAT_SESSION_ID")
        original_limit = os.environ.get("MOORE_WECHAT_HISTORY_LIMIT")
        try:
            os.environ["MOORE_WECHAT_RUNTIME_DIR"] = str(self.tmp)
            os.environ["MOORE_WECHAT_SESSION_ID"] = "session-a"
            os.environ["MOORE_WECHAT_HISTORY_LIMIT"] = "100"
            addon = importlib.import_module("wechat_history_mitm_addon")
            addon = importlib.reload(addon)
            payload = {
                "list": [
                    {
                        "comm_msg_info": {"datetime": 1783333333},
                        "app_msg_ext_info": {
                            "title": "首页首篇",
                            "content_url": "https://mp.weixin.qq.com/s/abc?token=secret&idx=1",
                            "digest": "摘要",
                            "cover": "https://mmbiz.qpic.cn/cover",
                            "multi_app_msg_item_list": [
                                {
                                    "title": "首页次篇",
                                    "content_url": "https://mp.weixin.qq.com/s/def?pass_ticket=hidden&idx=2",
                                }
                            ],
                        },
                    }
                ]
            }
            html_text = "<script>var msgList = '" + json.dumps(payload, ensure_ascii=False) + "';</script>"
            extracted = addon.extract_embedded_history_payload(html_text)
            rows = addon.rows_from_any_payload(
                extracted,
                {"account_name": "测试号", "account_id": "biz", "sample_url": "https://mp.weixin.qq.com/s/source"},
                "biz",
            )
        finally:
            if original_runtime is None:
                os.environ.pop("MOORE_WECHAT_RUNTIME_DIR", None)
            else:
                os.environ["MOORE_WECHAT_RUNTIME_DIR"] = original_runtime
            if original_session is None:
                os.environ.pop("MOORE_WECHAT_SESSION_ID", None)
            else:
                os.environ["MOORE_WECHAT_SESSION_ID"] = original_session
            if original_limit is None:
                os.environ.pop("MOORE_WECHAT_HISTORY_LIMIT", None)
            else:
                os.environ["MOORE_WECHAT_HISTORY_LIMIT"] = original_limit

        self.assertIsInstance(extracted, dict)
        self.assertEqual([row["title"] for row in rows], ["首页首篇", "首页次篇"])
        self.assertNotIn("token", rows[0]["url"])
        self.assertNotIn("pass_ticket", rows[1]["url"])

    def test_history_capture_prepare_requires_confirmation(self) -> None:
        result = wechat_downloader.prepare_history_capture(
            self.tmp,
            "https://mp.weixin.qq.com/s/sample",
            8899,
            100,
            "auto",
            False,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_confirmation"])
        self.assertIn("history-capture-prepare", result["command"])

    def test_history_capture_prepare_wraps_proxy_setup_and_open_url(self) -> None:
        original_setup = wechat_downloader.proxy_setup_status
        original_start_session = wechat_downloader.start_history_session
        original_open = wechat_downloader.open_history_link
        original_status = wechat_downloader.session_status
        original_start_proxy = wechat_downloader.start_history_proxy
        original_points = wechat_downloader.system_proxy_points_to_port
        original_enable = wechat_downloader.enable_system_proxy
        try:
            session = {
                "session_id": "session-a",
                "account_id": "biz",
                "account_name": "测试号",
                "history_csv": str(self.tmp / "history.csv"),
                "history_json": str(self.tmp / "history.json"),
            }
            wechat_downloader.proxy_setup_status = lambda port: {"ok": True, "mitmdump": "/opt/homebrew/bin/mitmdump"}
            wechat_downloader.start_history_session = lambda sample_url, base: session
            wechat_downloader.open_history_link = lambda base, session_id, copy=True: {
                "ok": True,
                "session_id": session_id,
                "open_url": "https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=biz&scene=124#wechat_redirect",
                "copied_to_clipboard": copy,
            }
            wechat_downloader.session_status = lambda base, session_id: session
            wechat_downloader.start_history_proxy = lambda base, session, port, limit, upstream_proxy: {
                "ok": True,
                "pid": 1234,
                "upstream_proxy": "http://127.0.0.1:10808",
                "reused_existing_proxy": False,
            }
            wechat_downloader.system_proxy_points_to_port = lambda service, host, port: False
            wechat_downloader.enable_system_proxy = lambda base, service, host, port, yes: {
                "ok": True,
                "service": "Wi-Fi",
                "state": str(self.tmp / "system-proxy-state.json"),
            }

            result = wechat_downloader.prepare_history_capture(
                self.tmp,
                "https://mp.weixin.qq.com/s/sample",
                8899,
                100,
                "auto",
                True,
            )
        finally:
            wechat_downloader.proxy_setup_status = original_setup
            wechat_downloader.start_history_session = original_start_session
            wechat_downloader.open_history_link = original_open
            wechat_downloader.session_status = original_status
            wechat_downloader.start_history_proxy = original_start_proxy
            wechat_downloader.system_proxy_points_to_port = original_points
            wechat_downloader.enable_system_proxy = original_enable

        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], "session-a")
        self.assertEqual(result["account_name"], "测试号")
        self.assertIn("profile_ext?action=home", result["open_url"])
        self.assertEqual(result["upstream_proxy"], "http://127.0.0.1:10808")
        self.assertFalse(result["proxy_already_enabled"])

    def test_history_capture_finish_returns_rows_and_restores_proxy(self) -> None:
        account_dir = self.tmp / "account-history" / "biz-test"
        history_json = account_dir / "history_articles.json"
        history_csv = account_dir / "history_articles.csv"
        session = {
            "session_id": "session-a",
            "account_id": "biz",
            "account_name": "测试号",
            "account_dir": str(account_dir),
            "history_csv": str(history_csv),
            "history_json": str(history_json),
            "context_ready": True,
            "status": "ready",
        }
        wechat_downloader.save_history_session(self.tmp, session)
        rows = [
            {
                "account_name": "测试号",
                "account_id": "biz",
                "title": "历史文章",
                "url": "https://mp.weixin.qq.com/s/a",
                "publish_time": "2026-07-06 10:00:00",
                "digest": "",
                "cover": "",
                "source_article_url": "https://mp.weixin.qq.com/s/source",
                "fetch_method": "wechat-history-proxy",
            }
        ]
        wechat_downloader.write_history_rows_csv(history_csv, rows)
        wechat_downloader.write_json(history_json, {"articles": rows})
        wechat_downloader.write_json(
            wechat_downloader.session_ready_marker(self.tmp, "session-a"),
            {
                "status": "ready",
                "ready": True,
                "adapter": "wechat-history-proxy",
                "method": "test",
                "article_count": 1,
                "history_csv": str(history_csv),
                "history_json": str(history_json),
            },
        )
        original_disable = wechat_downloader.disable_system_proxy
        original_stop = wechat_downloader.stop_history_proxy
        try:
            wechat_downloader.disable_system_proxy = lambda base, service="", yes=False: {
                "ok": True,
                "service": "Wi-Fi",
                "restored": True,
            }
            wechat_downloader.stop_history_proxy = lambda base, session_id: {
                "ok": True,
                "stopped": True,
                "pid": 1234,
            }
            result = wechat_downloader.finish_history_capture(self.tmp, "session-a", 50, True)
        finally:
            wechat_downloader.disable_system_proxy = original_disable
            wechat_downloader.stop_history_proxy = original_stop

        self.assertTrue(result["ok"])
        self.assertTrue(result["context_ready"])
        self.assertEqual(result["article_count"], 1)
        self.assertEqual(result["articles"][0]["title"], "历史文章")
        self.assertTrue(result["restore"]["restored"])
        self.assertTrue(result["stop"]["stopped"])


if __name__ == "__main__":
    unittest.main()
