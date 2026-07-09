# Backend Design

## Final Scope

The runtime has three modes:

1. URL download mode
   - Input: one or many known WeChat article URLs.
   - Output: markdown-only delivery directory with `index.csv`, `articles/`, and `images/`.

2. Account history mode
   - Input: any one article URL from the target public account.
   - Use WeChat desktop client context, fetch history list, let user select, then download selected URLs.

3. Exporter mode
   - Input: exporter auth-key, or user action to scan-login on an exporter instance.
   - Use a WeChat Official Account backend session to search accounts, sync article metadata, manage fields/collections in SQLite, then download selected URLs through the normal Markdown downloader.

Do not add unrelated platform features. Content processing, rewriting, cloud deployment, and SaaS workflows are outside the runtime core. A local management page is allowed only for Exporter mode.

## Layers

### Skill Layer

The Skill classifies the user request:

- URL mode: direct download.
- Account history mode: start history/context flow.

It calls local runtime CLI commands, validates outputs, previews history rows in chat, asks the user to choose rows, and reports paths.

### URL Downloader

Responsibilities:

- validate `mp.weixin.qq.com` URLs
- normalize and dedupe URL lists
- fetch article HTML
- extract metadata
- save one Markdown file per article
- download allowed media assets when possible
- write images under `images/<seq>/`
- write `index.csv`

`--profile archive` may still write raw HTML, normalized HTML, metadata, manifest, and report for debugging.

### Account History Adapter

Responsibilities:

- accept a sample article URL
- extract account clues from the article page
- create a local short-lived history session
- generate and copy the WeChat built-in-browser open link
- instruct the user to send that link to File Transfer and open it in WeChat desktop client
- start a local mitmproxy adapter after explicit user action
- capture user-owned `profile_ext?action=getmsg` responses from WeChat desktop traffic
- parse recent/history article metadata from `general_msg_list`
- write `history_articles.csv/json`
- let the user select rows
- pass selected URLs to the URL downloader

The adapter must not print or persist raw cookies, tokens, pass tickets, keys, or auth headers.

### Exporter Adapter

Responsibilities:

- open an exporter instance for QR login
- optionally run the QR login loop locally: start session, fetch QR image, poll scan status, complete login, and store returned auth-key
- store exporter `auth-key` locally, preferring macOS Keychain over SQLite plaintext
- validate `auth-key` with `GET /api/public/v1/authkey`
- search accounts with `GET /api/public/v1/account`
- optionally resolve account metadata from one article URL
- sync article list with `GET /api/public/v1/article`
- persist accounts, articles, collections, field presets, sync jobs, and download runs in `exporter.sqlite`
- expose a local management page for search/add/sync/list/field/collection workflows
- import enhanced metrics and comments from user-owned JSON/CSV when available
- pass selected article URLs to the existing Markdown-only downloader

Exporter mode treats collections and enhanced metrics as best-effort. Reading count, likes, comments, and shares may require short-lived WeChat article credentials and should not block list sync or downloads. When the user already has enhanced data, import it into SQLite instead of trying to silently capture credentials.

### Skill-Driven Selection

The Skill shows a numbered preview in chat. The user chooses with:

- latest count
- row indices
- row ranges
- keyword filter

## Runtime Storage

Default user-facing output:

```text
~/Downloads/wechat-articles/<run-id>/
```

Shape:

```text
index.csv
articles/<seq>-<safe-title>.md
images/<seq>/<image-number>.<ext>
```

Internal runtime/session storage:

```text
~/.moore/wechat-article-downloader/
```

Shape:

```text
context/<session-id>.json
account-history/<account-id>-<safe-account-name>/history_articles.csv
account-history/<account-id>-<safe-account-name>/history_articles.json
account-history/<account-id>-<safe-account-name>/selected_articles.csv
runs/<run-id>/manifest.json
runs/<run-id>/failed.json
runs/<run-id>/index.csv
runs/<run-id>/report.md
articles/<article-id>-<safe-title>/
exporter.sqlite
```

## CLI Contract

URL mode:

```bash
python3 scripts/wechat_downloader.py download-url "<url>" [--output-dir "<dir>"]
python3 scripts/wechat_downloader.py download-list "<urls-or-file>" [--output-dir "<dir>"]
```

Account history mode:

```bash
python3 scripts/wechat_downloader.py history-start "<sample-article-url>"
python3 scripts/wechat_downloader.py history-open "<session-id>"
python3 scripts/wechat_downloader.py history-status "<session-id>"
python3 scripts/wechat_downloader.py history-proxy-setup --port 8899
python3 scripts/wechat_downloader.py history-proxy-setup --port 8899 --install --yes
python3 scripts/wechat_downloader.py history-proxy-start "<session-id>" --port 8899 --limit 100
python3 scripts/wechat_downloader.py history-proxy-enable --port 8899 --yes
python3 scripts/wechat_downloader.py adapter-watch "<session-id>" --timeout 120
python3 scripts/wechat_downloader.py history-proxy-stop "<session-id>"
python3 scripts/wechat_downloader.py history-proxy-disable --yes
python3 scripts/wechat_downloader.py history-fetch "<session-id>" --limit 50
python3 scripts/wechat_downloader.py history-preview --session-id "<session-id>"
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --latest 20
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --indices "1,3,5"
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --range "1-20"
python3 scripts/wechat_downloader.py history-download-selected --session-id "<session-id>" [--output-dir "<dir>"]
```

Exporter mode:

```bash
python3 scripts/wechat_exporter.py exporter-init
python3 scripts/wechat_exporter.py exporter-server-start --port 8765
python3 scripts/wechat_exporter.py exporter-login-start --open
python3 scripts/wechat_exporter.py exporter-login-qr-start --open
python3 scripts/wechat_exporter.py exporter-login-qr-status "<login-id>"
python3 scripts/wechat_exporter.py exporter-login-qr-complete "<login-id>"
python3 scripts/wechat_exporter.py exporter-config --auth-key "<auth-key>"
python3 scripts/wechat_exporter.py exporter-auth-check
python3 scripts/wechat_exporter.py exporter-search "keyword"
python3 scripts/wechat_exporter.py exporter-add --fakeid "<fakeid>" --nickname "<name>"
python3 scripts/wechat_exporter.py exporter-sync --account-id "<id>" --limit 200
python3 scripts/wechat_exporter.py exporter-articles --account-id "<id>"
python3 scripts/wechat_exporter.py exporter-fields --set "title,url,publish_time,author,digest"
python3 scripts/wechat_exporter.py exporter-collections --account-id "<id>"
python3 scripts/wechat_exporter.py exporter-download --account-id "<id>" --latest 20
python3 scripts/wechat_exporter.py exporter-download-collection --collection-id "<id>"
python3 scripts/wechat_exporter.py exporter-metrics-import "<json-or-csv>"
python3 scripts/wechat_exporter.py exporter-comments-import "<json-or-csv>"
python3 scripts/wechat_exporter.py exporter-comments --article-id "<id>"
```

There is no Dashboard or local web UI for URL/history flows. Exporter mode can use the local management page.

## Security Rules

- Account-history mode requires explicit user action.
- Context sessions expire.
- Do not bypass login, paywalls, deleted content, or permission checks.
- Never commit or print credential material.
- If context is missing, stop and show the WeChat desktop step.
- Never print full exporter auth-key. Store credentials in Keychain when available.
- If auth-key is expired, ask the user to scan-login again.

## Phases

| Phase | Goal |
|---|---|
| 1 | Clean scope to two modes |
| 2 | Keep direct URL single/batch download stable |
| 3 | Add history session skeleton |
| 4 | Add WeChat desktop context adapter |
| 5 | Fetch history list and selection |
| 6 | Download selected history articles through URL pipeline |
| 7 | Add Exporter SQLite runtime and auth-key config |
| 8 | Add Exporter account search, account management, article sync, fields, collections, and local management page |
