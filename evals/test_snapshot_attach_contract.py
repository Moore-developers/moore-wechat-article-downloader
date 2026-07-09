#!/usr/bin/env python3
"""Offline contract tests for proxy snapshot attachment."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import wechat_downloader  # noqa: E402


class SnapshotAttachContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="moore-snapshot-attach-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_snapshot(self, snapshot_id: str = "snapshot-a") -> None:
        run_dir = wechat_downloader.auto_snapshot_root(self.tmp) / snapshot_id
        run_dir.mkdir(parents=True, exist_ok=True)
        wechat_downloader.write_json(run_dir / "ready.json", {"ready": True})
        wechat_downloader.write_json(
            run_dir / "snapshot.json",
            {
                "title": "页面数据测试",
                "account_name": "测试号",
                "author": "测试作者",
                "publish_time": "2026年7月9日 20:00",
                "captured_at": "2026-07-09T12:00:00Z",
                "url": "https://mp.weixin.qq.com/s?__biz=testbiz&mid=1&idx=1&sn=abc",
                "js_content_html": "<p>正文第一段</p>",
                "comments_dom_html": (
                    "<div>张三</div><div>北京6月27日</div><div>转发</div>"
                    "<div>不喜欢</div><div>投诉</div><div>赞2</div>"
                    "<div>这是一条评论</div><div>1条回复</div>"
                    "<div>李四</div><div>上海6月28日</div><div>转发</div>"
                    "<div>不喜欢</div><div>投诉</div><div>赞</div><div>第二条评论</div>"
                    "<div>测试号</div><div>作者刚刚</div><div>转发</div>"
                    "<div>不喜欢</div><div>投诉</div><div>赞</div><div>作者评论</div>"
                ),
                "engagement_dom_html": "<div>阅读 123</div>",
            },
        )
        wechat_downloader.write_json(
            run_dir / "metrics.json",
            {
                "read_count": {"value": "123", "source": "snapshot"},
                "like_count": {"value": "4", "source": "snapshot"},
                "favorite_count": {"value": None, "source": "missing"},
            },
        )
        row = {
            "snapshot_id": snapshot_id,
            "run_dir": str(run_dir),
            "captured_at": "2026-07-09T12:00:00Z",
            "title": "页面数据测试",
            "account_name": "测试号",
            "url": "https://mp.weixin.qq.com/s?__biz=testbiz&mid=1&idx=1&sn=abc",
        }
        index_path = wechat_downloader.auto_snapshot_index_path(self.tmp)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_attach_embeds_metrics_and_comments_into_markdown(self) -> None:
        self.write_snapshot()
        output = self.tmp / "out"

        result = wechat_downloader.attach_auto_snapshot(self.tmp, "snapshot-a", str(output))

        self.assertTrue(result["ok"])
        self.assertTrue(result["markdown_embedded"])
        markdown_path = Path(result["markdown_path"])
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("## 页面数据", markdown)
        self.assertIn("| 阅读数 | 123 | snapshot |", markdown)
        self.assertIn("| 点赞数 | 4 | snapshot |", markdown)
        self.assertIn("| 收藏数 | missing | missing |", markdown)
        self.assertIn("| 1 | 张三 | 北京 · 6月27日 | 2 | 1 | 这是一条评论 |", markdown)
        self.assertIn("| 2 | 李四 | 上海 · 6月28日 | 0 | 0 | 第二条评论 |", markdown)
        self.assertIn("| 3 | 测试号（作者） | 刚刚 | 0 | 0 | 作者评论 |", markdown)
        self.assertNotIn("- 转发", markdown)
        structured_path = (
            Path(result["snapshot_dir"])
            / "comments_structured.json"
        )
        structured = json.loads(structured_path.read_text(encoding="utf-8"))
        self.assertEqual(structured["count"], 3)
        self.assertEqual(structured["comments"][0]["content"], "这是一条评论")
        self.assertEqual(structured["comments"][1]["content"], "第二条评论")
        self.assertTrue(structured["comments"][2]["is_author"])

        forced = wechat_downloader.attach_auto_snapshot(self.tmp, "snapshot-a", str(output), force=True)
        self.assertTrue(forced["markdown_embedded"])
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(markdown.count(wechat_downloader.PAGE_DATA_START), 1)
        self.assertEqual(markdown.count("## 页面数据"), 1)


if __name__ == "__main__":
    unittest.main()
