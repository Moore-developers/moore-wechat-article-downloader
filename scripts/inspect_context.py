#!/usr/bin/env python3
"""Inspect local runtime readiness for account-history adapters."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


APP_DIR = Path.home() / ".moore" / "wechat-article-downloader"


def main() -> int:
    base = APP_DIR
    context_dir = base / "context"
    marker = context_dir / "account-history-context.json"
    ready = marker.exists()
    payload = {
        "runtime_dir": str(base),
        "account_history_context_ready": ready,
        "context_marker": str(marker),
        "mitmdump_available": bool(shutil.which("mitmdump")),
        "notes": [
            "Use history-proxy-start to capture user-owned WeChat profile_ext?action=getmsg responses.",
            "Only use account-history adapters with explicit user confirmation and a safe local context.",
            "Do not print or store cookies/tokens/pass tickets in repository files.",
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
