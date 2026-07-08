# Troubleshooting

## URL Rejected

The runtime accepts only `mp.weixin.qq.com` URLs by default. Check that the URL was copied from a WeChat official account article and not from an unrelated page.

## Fetch Failed

Common causes:

- network timeout
- article deleted or restricted
- WeChat blocks non-browser requests
- URL requires a context not available to the local runtime

Try again later or open the URL in a browser to confirm it is public.

## Content Looks Empty

WeChat pages can change markup. If extraction fails:

- inspect the generated Markdown first
- retry with `--profile archive` only when debugging needs raw HTML
- consider a future browser renderer for difficult pages

## Images Missing

Some image URLs require headers or expire. The runtime should still write Markdown and metadata. Failed asset downloads should not fail the entire article unless the user explicitly requires asset completeness.

## Account History Unsupported

Known URL batches are core. Account-history pagination requires valid user-owned WeChat context and is intentionally behind an adapter. Ask for explicit confirmation before attempting it.

## History Open Link Not Copied

Check:

- `history-open` output
- whether `copied_to_clipboard` is `true`
- whether the printed `open_url` can be copied manually

If clipboard copy fails, copy the `open_url` from JSON manually, send it to WeChat File Transfer, and open it with the WeChat desktop built-in browser.

## Proxy Adapter Not Installed

If `history-proxy-start` reports `mitmdump not found`, install mitmproxy first. On macOS, Homebrew users can run:

```bash
brew install mitmproxy
```

Or let the runtime do the Homebrew install explicitly:

```bash
python3 scripts/wechat_downloader.py history-proxy-setup --install --yes
```

Then start the proxy and install/trust the mitmproxy certificate. The certificate step is explicit because HTTPS capture will not work until the user trusts the local certificate.

`history-proxy-enable --yes` saves current macOS HTTP/HTTPS proxy settings before routing traffic to the local adapter. `history-proxy-disable --yes` restores the saved settings.

## Proxy Captures Nothing

Check:

- WeChat traffic is routed through `127.0.0.1:<port>`
- `history-proxy-enable --yes` was run for the active macOS network service, or the proxy was set manually
- the mitmproxy certificate is trusted
- the link is opened in WeChat desktop, not Chrome or Safari
- the user entered the public-account history page and scrolled
- `history-proxy-status <session-id>` shows the proxy process is running

## Context Import Fails

`history-import-context --from-clipboard` is optional. Most WeChat desktop history pages do not expose a copyable authenticated URL, so prefer the proxy adapter.

## Exporter Auth-Key Fails

Run:

```bash
python3 scripts/wechat_exporter.py exporter-auth-check
```

If it reports expired or invalid, ask the user to scan-login again and copy a fresh auth-key. Auth-key validity is usually short-lived and tied to the exporter instance that generated it.

## Exporter QR Login Fails

Run:

```bash
python3 scripts/wechat_exporter.py exporter-login-qr-start --open
python3 scripts/wechat_exporter.py exporter-login-qr-status "<login-id>"
```

If the QR session cannot start or never reaches `confirmed`, use the fallback:

```bash
python3 scripts/wechat_exporter.py exporter-login-start --open
python3 scripts/wechat_exporter.py exporter-config --auth-key "<auth-key>"
```

The QR flow depends on the configured exporter instance exposing `/api/web/login/*`. Public and private deployments can differ, so keep manual auth-key as the escape hatch.

## Exporter Search Has No Results

Check:

- the auth-key is valid
- the target account allows search by name
- try a more exact account nickname
- try `exporter-account-by-url "<article-url>"` when the user has one known article URL

## Exporter Collections Are Empty

Collections are best-effort. They depend on album fields returned by article-list sync or internal exporter endpoints. First run:

```bash
python3 scripts/wechat_exporter.py exporter-sync --account-id "<id>" --limit 200
python3 scripts/wechat_exporter.py exporter-collections --account-id "<id>"
```

If still empty, the synced articles may not include album metadata. Users can still download by account, latest count, title keyword, or explicit article IDs.
