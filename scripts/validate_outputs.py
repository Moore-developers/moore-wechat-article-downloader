#!/usr/bin/env python3
"""Validate a moore-wechat-article-downloader run directory."""

from __future__ import annotations

import json
import csv
import sys
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(run_dir: Path) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not run_dir.exists():
        return False, [f"run directory does not exist: {run_dir}"]

    manifest_path = run_dir / "manifest.json"
    markdown_index_path = run_dir / "index.csv"
    if not manifest_path.exists() and (run_dir / "articles").exists():
        return validate_markdown_only(run_dir)
    failed_path = run_dir / "failed.json"
    report_path = run_dir / "report.md"
    index_path = markdown_index_path

    for path in [manifest_path, failed_path, report_path, index_path]:
        if not path.exists():
            issues.append(f"missing required run file: {path.name}")

    if not manifest_path.exists():
        return False, issues

    manifest = load_json(manifest_path)
    articles = manifest.get("articles", [])
    failed = manifest.get("failed", [])
    if manifest.get("success_count") != len(articles):
        issues.append("success_count does not match articles length")
    if manifest.get("failure_count") != len(failed):
        issues.append("failure_count does not match failed length")

    for article in articles:
        article_id = article.get("article_id", "<missing>")
        files = article.get("files", {})
        for key in ["metadata", "raw_html", "normalized_html", "markdown", "text"]:
            value = files.get(key)
            if not value:
                issues.append(f"{article_id}: missing file mapping for {key}")
                continue
            if not Path(value).exists():
                issues.append(f"{article_id}: mapped file does not exist: {value}")

    return len(issues) == 0, issues


def validate_markdown_only(output_dir: Path) -> tuple[bool, list[str]]:
    issues: list[str] = []
    index_path = output_dir / "index.csv"
    articles_dir = output_dir / "articles"
    images_dir = output_dir / "images"
    for path in [index_path, articles_dir, images_dir]:
        if not path.exists():
            issues.append(f"missing required markdown-only path: {path.name}")
    if not index_path.exists():
        return False, issues
    with index_path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    required = ["seq", "title", "source_url", "markdown_path", "image_dir", "status"]
    for field in required:
        if field not in (rows[0].keys() if rows else []):
            issues.append(f"index.csv missing field: {field}")
    for row in rows:
        if row.get("status") != "success":
            continue
        markdown_path = output_dir / row.get("markdown_path", "")
        image_dir = output_dir / row.get("image_dir", "")
        if not markdown_path.exists():
            issues.append(f"{row.get('seq')}: markdown file does not exist: {markdown_path}")
        if row.get("image_count") not in {"", "0"} and not image_dir.exists():
            issues.append(f"{row.get('seq')}: image directory does not exist: {image_dir}")
    return len(issues) == 0, issues


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("Usage: validate_outputs.py <run-dir>", file=sys.stderr)
        return 2
    ok, issues = validate(Path(args[0]).expanduser().resolve())
    print(json.dumps({"ok": ok, "issues": issues}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
