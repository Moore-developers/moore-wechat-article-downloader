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
from wechat_credential_broker import WeChatCredentialBroker, broker_status  # noqa: E402


class CredentialBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="moore-credential-broker-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_is_redacted_and_socket_is_owner_only(self) -> None:
        path = self.tmp / "credential.sock"
        broker = WeChatCredentialBroker(path, "proxy-enhancer-test")
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
        broker = WeChatCredentialBroker(self.tmp / "expired.sock", "session", ttl_seconds=60)
        broker.capture("https://mp.weixin.qq.com/s/demo?__biz=MzDemo&uin=12", {"cookie": "x"})
        broker._credentials["MzDemo"]["expires_at_epoch"] = 0
        self.assertEqual(broker.status()["credentials"], [])


if __name__ == "__main__":
    unittest.main()
