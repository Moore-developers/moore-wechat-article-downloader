# Output Formats

## Default User-Facing Download Output

The default profile is `markdown-only`.

```text
<output-dir>/
|-- index.csv
|-- articles/
|   |-- 001-<safe-title>.md
|   `-- 002-<safe-title>.md
`-- images/
    |-- 001/
    |   `-- 001.<ext>
    `-- 002/
        `-- 001.<ext>
```

Default output directory:

```text
~/Downloads/wechat-articles/<run-id>/
```

`index.csv` fields:

```text
seq
article_id
title
account
source_url
markdown_path
image_dir
image_count
status
error
```

Markdown frontmatter includes:

```yaml
seq: "001"
article_id: "..."
title: "..."
account: "..."
author: "..."
publish_time: "..."
source_url: "https://mp.weixin.qq.com/s/..."
downloaded_at: "..."
image_dir: "../images/001"
```

Markdown image paths are relative:

```markdown
![image](../images/001/001.jpg)
```

## Archive Profile

`--profile archive` keeps the older full archive shape under `~/.moore/wechat-article-downloader/`:

```text
runs/<run-id>/manifest.json
runs/<run-id>/failed.json
runs/<run-id>/index.csv
runs/<run-id>/report.md
articles/<article-id>-<safe-title>/
```

Account-history sessions may have:

- `context/<session-id>.json`
- `context/<session-id>.ready.json`
- `account-history/<account-id>-<safe-account-name>/source_article.json`
- `account-history/<account-id>-<safe-account-name>/history_articles.csv`
- `account-history/<account-id>-<safe-account-name>/history_articles.json`
- `account-history/<account-id>-<safe-account-name>/selected_articles.csv`

## Archive Manifest Schema

```json
{
  "run_id": "20260705-120000-a1b2c3d4",
  "created_at": "2026-07-05T12:00:00Z",
  "runtime_dir": "/Users/name/.moore/wechat-article-downloader",
  "run_dir": "/Users/name/.moore/wechat-article-downloader/runs/...",
  "requested_formats": ["html", "md", "txt"],
  "canonical_source_formats": ["html", "md", "txt"],
  "skipped_formats": [],
  "success_count": 1,
  "failure_count": 0,
  "articles": [
    {
      "article_id": "...",
      "title": "...",
      "account": "...",
      "source_url": "...",
      "article_dir": "...",
      "files": {
        "metadata": "...",
        "raw_html": "...",
        "normalized_html": "...",
        "markdown": "...",
        "text": "..."
      }
    }
  ],
  "failed": []
}
```

## Metadata Schema

```json
{
  "article_id": "...",
  "source_url": "...",
  "canonical_url": "...",
  "title": "...",
  "account": "...",
  "author": "...",
  "publish_time": "...",
  "downloaded_at": "...",
  "content_hash": "...",
  "assets": []
}
```

## History Article Schema

```text
account_name
account_id
title
url
publish_time
digest
cover
source_article_url
fetch_method
```

`selected_articles.csv` uses the same fields as `history_articles.csv`.

`context/<session-id>.ready.json` must not contain raw cookies, tokens, pass tickets, keys, or auth headers. It may contain:

- `status`
- `ready`
- `ready_at`
- `adapter`
- `method`
- `article_count`
- `history_csv` or `history_json` paths inside the session account-history directory

Prefer storing only `history_csv`/`history_json` paths in the ready marker. Do not store raw cookies, tokens, pass tickets, keys, auth headers, or full authenticated context URLs.

## Exporter SQLite

Exporter mode stores local state in:

```text
~/.moore/wechat-article-downloader/exporter.sqlite
```

Core tables:

- `login_profiles`: exporter base URL, display name, status, last login time, expiry time
- `credential_store`: Keychain account pointer or explicit plaintext fallback when the user allows it
- `target_accounts`: fakeid, nickname, alias, avatar, description, sync counts
- `articles`: title, URL, digest, cover, author, publish time, downloaded flags, optional metrics
- `collections`: collection title, URL, article count
- `collection_articles`: collection membership and order
- `field_presets`: visible fields for the local management page and export views
- `sync_jobs`: sync progress and errors
- `download_runs`: selected article IDs and final Markdown output directory
- `article_comments`: optional imported comments for articles

Exporter downloads still use the normal Markdown-only delivery shape:

```text
~/Downloads/wechat-articles/<run-id>/
index.csv
articles/<seq>-<safe-title>.md
images/<seq>/<image-number>.<ext>
```
