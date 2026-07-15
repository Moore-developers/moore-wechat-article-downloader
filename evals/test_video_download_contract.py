import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import wechat_exporter  # noqa: E402
from wechat_video_models import descriptors_from_object_desc, scrub_video_payload  # noqa: E402


class VideoDownloadContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_descriptor_public_output_does_not_expose_short_lived_credentials(self) -> None:
        descriptors = descriptors_from_object_desc(
            {
                "description": "演示视频",
                "objectId": "object-1",
                "media": [
                    {
                        "url": "https://finder.video.qq.com/video.mp4?encfilekey=a",
                        "urlToken": "&token=secret",
                        "decodeKey": "123456789",
                        "fileSize": "1024",
                        "spec": [{"fileFormat": "sd"}, {"fileFormat": "hd"}],
                    }
                ],
            },
            source_url="https://channels.weixin.qq.com/web/pages/feed?finderUsername=demo&exportkey=secret&token=hidden",
            source_title="页面标题",
        )

        self.assertEqual(len(descriptors), 1)
        public = descriptors[0].public_dict()
        self.assertTrue(public["has_decode_key"])
        serialized = json.dumps(scrub_video_payload(public), ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertNotIn("123456789", serialized)
        self.assertNotIn("signed_url", serialized)
        self.assertIn("X-snsvideoflag=hd", descriptors[0].download_url("highest"))

    def test_exporter_include_video_returns_needs_capture_without_active_proxy(self) -> None:
        wechat_exporter.init_exporter_db(self.tmp)
        account = wechat_exporter.upsert_account(self.tmp, {"fakeid": "fakeid-video", "nickname": "视频账号"})["account"]
        wechat_exporter.upsert_articles(
            self.tmp,
            [
                {
                    "account_id": int(account["id"]),
                    "msgid": "m1",
                    "idx": 1,
                    "title": "视频文章",
                    "url": "https://mp.weixin.qq.com/s/video-demo",
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
                    "raw_json": json.dumps({"item_show_type": 5, "media_duration": "3:30"}),
                }
            ],
        )
        row = wechat_exporter.get_article_download_rows(self.tmp, [1])[0]

        result = wechat_exporter.download_exporter_videos(self.tmp, [row], self.tmp / "out")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "needs_capture")
        self.assertEqual(result["needs_capture_count"], 1)

    def test_history_addon_contains_video_button_and_bundle_hooks(self) -> None:
        original_runtime = os.environ.get("MOORE_WECHAT_RUNTIME_DIR")
        original_session = os.environ.get("MOORE_WECHAT_SESSION_ID")
        try:
            os.environ["MOORE_WECHAT_RUNTIME_DIR"] = str(self.tmp)
            os.environ["MOORE_WECHAT_SESSION_ID"] = "proxy-enhancer-test"
            addon = importlib.import_module("wechat_history_mitm_addon")
            addon = importlib.reload(addon)
        finally:
            if original_runtime is None:
                os.environ.pop("MOORE_WECHAT_RUNTIME_DIR", None)
            else:
                os.environ["MOORE_WECHAT_RUNTIME_DIR"] = original_runtime
            if original_session is None:
                os.environ.pop("MOORE_WECHAT_SESSION_ID", None)
            else:
                os.environ["MOORE_WECHAT_SESSION_ID"] = original_session

        html = addon.inject_channels_video_button("<html><body><main></main></body></html>")
        self.assertIn("__mooreVideoInstalled", html)
        self.assertIn("/__moore_video_download", html)
        bundle = addon.inject_channels_bundle_hooks("class A{get media(){return this.objectDesc}}")
        self.assertIn("__mooreRegisterObjectDesc", bundle)
        self.assertIn("/__moore_video_register", bundle)


if __name__ == "__main__":
    unittest.main()
