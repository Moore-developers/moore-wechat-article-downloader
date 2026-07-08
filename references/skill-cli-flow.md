# Skill CLI Flow

## Principle

The user does not use a Dashboard. The Skill is the interface.

The Skill should:

- classify the user request
- run local CLI commands
- show short results in chat
- show the article list with titles and publish times, then ask the user to choose by title, title keyword, range, latest count, or keyword
- never ask the user to manually run commands unless debugging is needed

Exporter mode is the exception where a local management page can be opened, because the user explicitly needs account search, account management, field configuration, and collection views.

## URL Download Mode

Single URL:

```bash
python3 scripts/wechat_downloader.py download-url "<url>"
```

Multiple URLs or URL file:

```bash
python3 scripts/wechat_downloader.py download-list "<urls-or-file>"
```

The default output profile is `markdown-only`. It writes:

```text
~/Downloads/wechat-articles/<run-id>/
|-- index.csv
|-- articles/
|   `-- 001-<safe-title>.md
`-- images/
    `-- 001/
        `-- 001.<ext>
```

If the user provides a destination, pass:

```bash
--output-dir "<dir>"
```

After download, validate:

```bash
python3 scripts/validate_outputs.py "<output-dir>"
```

Report only:

- success count
- failure count
- failed URLs, if any
- `output_dir`
- `index.csv`

## Account History Mode

Start session:

```bash
python3 scripts/wechat_downloader.py history-start "<sample-article-url>"
```

Generate and copy the WeChat built-in-browser open link:

```bash
python3 scripts/wechat_downloader.py history-open "<session-id>"
```

The copied link must prefer the legacy article-history entry:

```text
https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=<biz>&scene=124#wechat_redirect
```

Do not use `channels.weixin.qq.com/web/pages/mp_profile` as the primary path. It is a video/account shell and normally loads media cards and telemetry, not `profile_ext?action=getmsg` article-history JSON.

Tell the user:

```text
我会启动本地代理。请确认 mitmproxy 证书已信任，并把 WeChat 流量路由到 127.0.0.1:8899。然后把复制的旧版 profile_ext 历史入口发到微信文件传输助手，用微信桌面客户端内置浏览器打开；看到文章历史列表后向下滚动。
```

Start proxy adapter:

```bash
python3 scripts/wechat_downloader.py history-proxy-setup --port 8899
python3 scripts/wechat_downloader.py history-proxy-setup --port 8899 --install --yes
python3 scripts/wechat_downloader.py history-proxy-start "<session-id>" --port 8899 --limit 100
python3 scripts/wechat_downloader.py history-proxy-start "<session-id>" --port 8899 --limit 100 --upstream-proxy auto
python3 scripts/wechat_downloader.py history-proxy-enable --port 8899 --yes
```

`history-proxy-start` uses `--upstream-proxy auto` by default. If the user's current macOS HTTP proxy is another local or remote proxy, mitmproxy chains outbound traffic through it:

```text
WeChat/system proxy -> mitmproxy 127.0.0.1:8899 -> existing system proxy
```

Use `--upstream-proxy http://host:port` only for an explicit override. Use `--upstream-proxy none` only when direct outbound traffic is desired.

Watch for captured history:

```bash
python3 scripts/wechat_downloader.py adapter-watch "<session-id>" --timeout 120
```

When ready, preview:

```bash
python3 scripts/wechat_downloader.py history-preview --session-id "<session-id>" --limit 30
```

Display the returned article titles and dates to the user. Do not present only numeric identifiers.

Selection examples:

```bash
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --latest 20
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --indices "1,3,5"
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --range "1-20"
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --contains "keyword"
python3 scripts/wechat_downloader.py history-select --session-id "<session-id>" --titles "title keyword"
```

Download selected:

```bash
python3 scripts/wechat_downloader.py history-download-selected --session-id "<session-id>"
```

Add `--output-dir "<dir>"` if the user wants a specific destination.

Stop and restore proxy:

```bash
python3 scripts/wechat_downloader.py history-proxy-stop "<session-id>"
python3 scripts/wechat_downloader.py history-proxy-disable --yes
```

## Selection Contract

Prefer title/title-keyword selection from the visible article list. 1-based numbers from `history-preview` are only shortcuts.

- `--latest 20`: first 20 rows in the current history list
- `--indices "1,3,5"`: specific rows
- `--range "1-20"`: contiguous rows
- `--contains "keyword"`: title or digest contains keyword
- `--titles "keyword A,keyword B"`: title contains one of the provided fragments

Options can combine. Filtering order is:

1. `--contains`
2. `--titles`
3. `--indices` / `--range`
4. `--latest`

## Adapter Boundary

`history-proxy-start` starts a mitmproxy adapter that captures user-owned WeChat `profile_ext?action=getmsg` responses and materializes `history_articles.csv/json`.

It must not write or print raw cookies, tokens, pass tickets, keys, or auth headers. It writes only article rows and a safe ready marker.

`history-proxy-enable` and `history-proxy-disable` modify macOS HTTP/HTTPS proxy settings only with `--yes`. The runtime saves previous proxy settings under the local runtime directory before enabling the proxy so it can restore them.

`history-proxy-setup --install --yes` installs mitmproxy with Homebrew when `mitmdump` is missing. Without `--yes`, setup only reports the install command and does not modify the machine.

`history-import-wechat-cache` is a fallback that reads recent `mp.weixin.qq.com/s/...` shortlinks recorded by WeChat's WebView. It is not a perfect account-history API: always preview the rows and ask the user to confirm they are the target account's articles before downloading.

`history-import-context --from-clipboard` remains an optional path for environments where an authenticated `profile_ext` URL can be copied. Most WeChat desktop history pages do not expose an address bar, so do not rely on it as the main flow.

`adapter-watch` waits for the proxy adapter's safe ready marker and then materializes the history files.

## Exporter Mode

Use Exporter mode when the user mentions `wechat-article-exporter`, exporter auth-key, QR login, searching public accounts by keyword, managing followed target accounts, field configuration, or collection download.

Initialize local SQLite:

```bash
python3 scripts/wechat_exporter.py exporter-init
```

Start local QR login:

```bash
python3 scripts/wechat_exporter.py exporter-login-qr-start --open
python3 scripts/wechat_exporter.py exporter-login-qr-status "<login-id>"
python3 scripts/wechat_exporter.py exporter-login-qr-complete "<login-id>"
```

The local management page can do the same QR flow from its “本地扫码登录” button. It starts the exporter session, displays the QR image locally, polls scan status, and completes login by saving the returned auth-key.

If the QR flow fails against the configured exporter instance, fall back to manual auth-key:

```bash
python3 scripts/wechat_exporter.py exporter-login-start --open
python3 scripts/wechat_exporter.py exporter-config --auth-key "<auth-key>"
python3 scripts/wechat_exporter.py exporter-auth-check
```

Search and add accounts:

```bash
python3 scripts/wechat_exporter.py exporter-search "哥飞" --size 10
python3 scripts/wechat_exporter.py exporter-add --fakeid "<fakeid>" --nickname "<name>"
python3 scripts/wechat_exporter.py exporter-accounts
```

Sync and list articles:

```bash
python3 scripts/wechat_exporter.py exporter-sync --account-id "<id>" --limit 200
python3 scripts/wechat_exporter.py exporter-articles --account-id "<id>" --limit 100
```

Fields and collections:

```bash
python3 scripts/wechat_exporter.py exporter-fields
python3 scripts/wechat_exporter.py exporter-fields --set "title,url,publish_time,author,digest,content_downloaded"
python3 scripts/wechat_exporter.py exporter-collections --account-id "<id>"
```

Download:

```bash
python3 scripts/wechat_exporter.py exporter-download --account-id "<id>" --latest 20
python3 scripts/wechat_exporter.py exporter-download --article-ids "1,2,3"
python3 scripts/wechat_exporter.py exporter-download-collection --collection-id "<id>"
```

For complex management, start the local page:

```bash
python3 scripts/wechat_exporter.py exporter-server-start --port 8765
```

The local page must support switching between saved target accounts. The page URL may use `?account_id=<id>` to select the active account.

Do not print the full auth-key. The runtime stores it in macOS Keychain when available; SQLite stores the profile metadata, accounts, article metadata, field presets, collections, sync jobs, and download run records.
