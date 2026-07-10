#!/usr/bin/env python3
"""Contract tests for the in-memory WeChat credential broker."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from wechat_credential_broker import WeChatCredentialBroker, broker_status, credential_socket_path  # noqa: E402


class CredentialBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="moore-credential-broker-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_is_redacted_and_socket_is_owner_only(self) -> None:
        path = self.tmp / "credential.sock"
        broker = WeChatCredentialBroker(path, "proxy-enhancer-test", "capability-test")
        broker.start()
        try:
            captured = broker.capture(
                "https://mp.weixin.qq.com/s/demo?__biz=MzDemo&uin=12&key=key-secret&pass_ticket=ticket-secret&appmsg_token=token-secret",
                {"cookie": "wap_sid2=cookie-secret"},
                {"set-cookie": "session=another-secret"},
            )
            self.assertTrue(captured)
            status = broker_status(path)
        finally:
            broker.close()

        self.assertTrue(status["ok"])
        self.assertEqual(status["credentials"][0]["status"], "valid")
        self.assertEqual(status["credentials"][0]["biz"], "MzDemo")
        serialized = str(status)
        for value in ("key-secret", "ticket-secret", "token-secret", "cookie-secret", "another-secret"):
            self.assertNotIn(value, serialized)
        self.assertEqual(path.exists(), False)

    def test_expired_credential_is_removed(self) -> None:
        broker = WeChatCredentialBroker(self.tmp / "expired.sock", "session", "capability-test", ttl_seconds=60)
        broker.capture("https://mp.weixin.qq.com/s/demo?__biz=MzDemo&uin=12", {"cookie": "x"})
        broker._credentials["MzDemo"]["expires_at_epoch"] = 0
        self.assertEqual(broker.status()["credentials"], [])

    def test_fetch_engagement_returns_elected_comments_without_credentials(self) -> None:
        calls: list[str] = []

        def fake_get(url: str, _headers: dict[str, str]) -> str:
            calls.append(url)
            if "/mp/appmsg_comment" in url:
                return '{"elected_comment":[{"id":"c1","nick_name":"读者","content":"有价值","like_num":3}],"continue_flag":0}'
            return "var appmsg_read_num = '12'; var appmsg_like_num = '3'; var comment_count = '1';"

        broker = WeChatCredentialBroker(self.tmp / "engagement.sock", "session", "capability-test", http_get=fake_get)
        broker.capture(
            "https://mp.weixin.qq.com/s/demo?__biz=MzDemo&uin=12&key=key-secret&pass_ticket=ticket-secret&appmsg_token=token-secret",
            {"cookie": "wap_sid2=cookie-secret"},
        )
        result = broker.fetch_engagement(
            "MzDemo",
            [{"article_id": 7, "msgid": "msg-7", "idx": 1, "comment_id": "comment-7", "url": "https://mp.weixin.qq.com/s/demo"}],
        )

        self.assertTrue(result["ok"])
        row = result["articles"][0]
        self.assertEqual(row["metrics"]["read_count"], 12)
        self.assertEqual(row["comments"][0]["comment_scope"], "elected")
        self.assertTrue(row["comments_complete"])
        serialized = str(result)
        for value in ("key-secret", "ticket-secret", "token-secret", "cookie-secret"):
            self.assertNotIn(value, serialized)
        self.assertEqual(len(calls), 2)

    def test_capture_reads_appmsg_token_only_from_in_memory_response_text(self) -> None:
        broker = WeChatCredentialBroker(self.tmp / "capture.sock", "session", "capability-test")
        captured = broker.capture(
            "https://mp.weixin.qq.com/s/demo?__biz=MzDemo&uin=12&key=key-secret&pass_ticket=ticket-secret",
            {"cookie": "wap_sid2=cookie-secret"},
            response_text="var appmsg_token = 'token-from-html';",
        )
        self.assertTrue(captured)
        status = broker.status("MzDemo")
        self.assertEqual(status["credentials"][0]["status"], "valid")
        self.assertNotIn("token-from-html", str(status))

    def test_rejects_non_wechat_url_and_comment_api_error(self) -> None:
        calls: list[str] = []

        def fake_get(url: str, _headers: dict[str, str]) -> str:
            calls.append(url)
            if "/mp/appmsg_comment" in url:
                return '{"base_resp":{"ret":-1},"elected_comment":[],"continue_flag":0}'
            return "var appmsg_read_num = '12';"

        broker = WeChatCredentialBroker(self.tmp / "safe.sock", "session", "capability-test", http_get=fake_get)
        broker.capture(
            "https://mp.weixin.qq.com/s/demo?__biz=MzDemo&uin=12&key=key-secret&pass_ticket=ticket-secret&appmsg_token=token-secret",
            {"cookie": "wap_sid2=cookie-secret"},
        )
        invalid = broker.fetch_engagement(
            "MzDemo", [{"article_id": 1, "msgid": "m", "idx": 1, "comment_id": "c", "url": "https://example.com/collect"}]
        )
        rejected = broker.fetch_engagement(
            "MzDemo", [{"article_id": 2, "msgid": "m", "idx": 1, "comment_id": "c", "url": "https://mp.weixin.qq.com/s/demo"}]
        )

        self.assertFalse(invalid["articles"][0]["ok"])
        self.assertFalse(rejected["articles"][0]["ok"])
        self.assertEqual(len(calls), 2)

    def test_short_socket_path_supports_long_runtime_directory(self) -> None:
        long_base = self.tmp / ("nested-" * 25)
        path = credential_socket_path(long_base, "proxy-enhancer-" + "x" * 48)
        self.assertLess(len(str(path).encode("utf-8")), 100)
        broker = WeChatCredentialBroker(path, "session", "capability-test")
        broker.start()
        try:
            self.assertTrue(path.exists())
        finally:
            broker.close()


if __name__ == "__main__":
    unittest.main()
