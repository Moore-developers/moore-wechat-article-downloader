---
name: moore-wechat-article-downloader
description: 当用户要下载或管理微信公众号文章时使用本技能。支持订阅同步、微信收藏和链接导入；互动数据仅在用户微信短时会话有效时补充指标与精选评论。优先本地运行；不要扩展成内容改写、总结、SaaS 或云服务。
---

# 公众号归档器

## 场景判断

| 用户意图 | 场景 | 入口 |
|---------|------|------|
| 提供了完整文章 URL | 场景 1：直接下载 | `wechat_wizard.py run "下载：<url>"` |
| 给公众号名 / 样例 URL，想要历史文章 | 场景 2A：Exporter（默认） | `wechat_wizard.py run "历史文章：<名称或URL>"` |
| 明确说"不登录"或"用代理" | 场景 2B：代理（备选） | `wechat_wizard.py run "代理历史：<url>"` |
| 想抓评论/阅读/点赞/页面风格 | 场景 2C：微信收藏 | `wechat_downloader.py proxy-enhancer-session-start --yes` |
| 管理多个公众号 / 定时同步 | 场景 3：订阅同步 | 见场景 3 |

统一入口优先。wizard 返回以下 gate 时按表处理，不要立刻降级为底层命令：

| Gate | 处理 |
|------|------|
| `need_login` | 引导扫码 → `login-status` 轮询 → `login-complete` 恢复任务 |
| `need_proxy_confirm` | 向用户确认启用系统代理 → 确认后继续 |
| `need_account_choice` | 聊天中列出候选公众号 → `resume --choice "<序号>"` |
| `need_article_choice` | 聊天中列出文章标题+日期 → `resume --choice "<选择>"` |

只有 wizard 返回 `blocked` 或 `failed_recoverable` 并明确给出底层命令时，才使用底层命令。

## 范围

可以做：下载公开的 `mp.weixin.qq.com` 文章、抓取历史文章列表、Exporter 扫码登录和账号管理、定时增量同步。

不要做：绕过登录/付费墙/私密内容；打印 auth-key/cookie/token；把历史列表抓取和 URL 下载混为一谈；扩展成内容改写、总结、SaaS。

## 执行护栏

- **不要把命令成功等同于交付完整**：`success_count` 只表示流程没有抛错。结束前必须检查 `index.csv`、Markdown、图片目录和源页面状态。
- **不要只给二维码文件路径**：生成二维码后优先调用当前操作系统的默认看图程序直接打开。只有无桌面环境或系统打开失败时，才退回聊天图片展示；裸路径只能作为最后兜底。二维码过期后重新生成，不重复打开旧图。
- **扫码流程不能少步骤**：用户说“已扫码”后先运行 `exporter-login-qr-status`；只有状态为 `confirmed` / `ready_to_complete=true` 才运行 `exporter-login-qr-complete`。
- **搜索结果必须精确匹配**：公众号搜索可能返回多个相似名称。优先选择昵称完全一致的结果，并核对简介或 alias；不要默认使用第一个模糊匹配结果。
- **已有明确选择时不要重复询问**：用户已经说“最新 N 篇”或给出标题/日期时，该表达本身就是文章选择；同步后直接执行，但结束时仍要在聊天中列出日期、标题和结果。

## 场景 1：直接下载已知文章

**触发**：用户提供一篇或多篇完整文章 URL，或 `.txt`/`.csv`/`.json` 文件。

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "下载：<url 或文件路径>"
```

重跑会跳过已成功的 URL。失败项重试：

```bash
python3 {baseDir}/scripts/wechat_wizard.py retry "<task-id>"
```

下载完成后报告：成功数量、失败数量、失败 URL（如有）、`output_dir`、`index.csv`。

## 场景 2：公众号历史文章

### 2A. Exporter 模式（默认首选）

**触发**：用户提供公众号名、关键词或样例文章 URL，想要历史文章列表。

**优势**：API 稳定、无需代理、支持按名称搜索、一次登录 4 天有效、SQLite 持久化可复用。

**边界**：Exporter 模式只做公众号搜索、历史文章同步和文章下载；不获取评论、阅读数、点赞数、收藏数、转发数等互动数据。互动数据只在代理快照模式验证通过后再提供。

**带评论/互动数据的硬规则**：

- 普通下载：`exporter-sync -> exporter-download`，只用于不带评论/互动数据的正文下载。
- 带评论互动下载：`exporter-sync -> engagement batch download`。
- 当用户需求包含“评论 / 互动 / 阅读数 / 点赞 / 在看 / 精选评论”时，禁止先调用普通 `exporter-download`。
- 此时 Exporter 只负责同步文章列表、标题、日期和 URL；正文与互动数据由 `wechat-collection-sync-engagement` / wizard 的 engagement 批量任务处理。
- 文章落盘只发生一次：成功拿到互动数据后生成最终 Markdown；如果某篇互动失败，再降级写正文并标记“互动数据缺失”。

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "获取公众号「<名称>」的历史文章"
```

wizard 自动处理：检查登录 → 搜索公众号 → 同步文章 → 列出结果。

**文章列表展示（强制要求）**：

抓取完成后必须在聊天中直接罗列，格式：

```text
- **YYYY-MM-DD**：文章标题
```

不要只给 CSV/JSON 路径。不要只给编号，必须同时展示标题和日期。

让用户按标题关键词、日期、最新 N 篇或编号范围选择后再下载。

### 2B. 代理模式（备选）

**触发**：用户明确说"不登录"或"用代理"，或 Exporter 模式失败。

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "代理历史：<sample-article-url>"
```

**硬性规则**：
- 必须使用旧版历史入口：`https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=<biz>&scene=124#wechat_redirect`
- 不要引导用户使用 `channels.weixin.qq.com/web/pages/mp_profile`（视频号壳页，不触发 getmsg 接口）
- 启用系统代理前必须向用户确认；结束时必须用 `history-capture-finish` 恢复系统代理
- 默认使用常驻本地代理 `127.0.0.1:23344`；切换公众号时不要停止代理服务，直接运行新的 `history-capture-prepare` 复用进程

告诉用户：把旧版入口发到微信文件传输助手，用桌面客户端内置浏览器打开，看到历史列表后向下滚动。

### 2C. 微信收藏（互动数据与页面保存）

**触发**：用户要评论、阅读数、点赞数、收藏数、在看数、完整页面快照、公众号排版风格。

此模式保存用户当前浏览的文章，也可在短时凭证有效期内恢复同公众号已授权任务的互动同步。历史列表仍默认走 Exporter；旧代理历史列表只作为备用。只同步精选评论，不承诺全量评论或完整回复树。

批量互动任务必须使用已经同步出的文章列表和 URL 创建，不要先用普通 Exporter 下载正文。用户说“下载某公众号最新 N 篇文章的评论和互动数据”时，正确链路是：

```text
exporter-sync -> engagement batch download
```

主流程不再使用 `proxy-snapshot-prepare --yes`。

每次新会话从 `23032-24045` 选择一个空闲随机端口。启动前先固定当前 upstream，再启动增强代理并切换系统 HTTP/HTTPS 代理。端口、PID、upstream 和恢复状态写入活动会话；同一会话安全重载时必须复用这些值。

启动成功后会终止旧的微信 `WeChatAppEx` WebView 进程。微信主进程和登录状态保持不变；用户下次打开文章时，微信会创建使用当前代理的新 WebView。代理增强代码安全重载后也执行同样的 WebView 重置，避免旧进程继续使用缓存页面。

```text
系统/微信 -> 127.0.0.1:<会话端口> -> 会话启动时确定的上游代理或直连 -> 外网
```

启动命令：

```bash
python3 {baseDir}/scripts/wechat_downloader.py proxy-enhancer-session-start --upstream-proxy none --yes
```

如果修改了代理增强代码或需要重载，只能使用安全重载。该命令自动读取并复用活动端口与 upstream：

```bash
python3 {baseDir}/scripts/wechat_downloader.py proxy-enhancer-restart --upstream-proxy none --yes
```

不要在系统代理指向活动端口时直接 `kill`、`proxy-enhancer-stop` 或 `stop && start`；这会让系统代理指向死端口。安全重载会临时恢复上游或直连，使用原端口重启，再切回活动端口。

启动后常见链路：

```text
系统/微信 -> 127.0.0.1:<会话端口> -> 启动时确定的上游代理 -> 外网
```

以下命令自动读取活动端口，不要手工猜端口：

```bash
python3 {baseDir}/scripts/wechat_downloader.py proxy-enhancer-route-help
python3 {baseDir}/scripts/wechat_downloader.py proxy-enhancer-check-ingress --minutes 10
python3 {baseDir}/scripts/wechat_downloader.py proxy-enhancer-logs --hours 24 --limit 80
python3 {baseDir}/scripts/wechat_downloader.py proxy-enhancer-video-links --hours 24 --limit 100
```

增强代理常驻期间会额外捕获公开的 `channels.weixin.qq.com` 视频号页面地址，写入 `proxy-snapshots/video-links.jsonl`。只提供页面链接，不下载视频；禁止保存或展示 `finder.video.qq.com` 临时媒体地址以及 token、ticket、exportkey、session、auth 等签名凭证。

视频号链接捕获规则：

- **必须先启动再浏览**：常驻拦截只能捕获启动后的流量，不能追溯之前打开过的视频号页面。
- **先确认流量进入代理**：启动后运行 `proxy-enhancer-check-ingress`；确认微信流量到达，再让用户在微信里打开视频号卡片或页面。
- **只保存可复用页面链接**：仅保留脱敏后的 `channels.weixin.qq.com` 页面 URL；`finder.video.qq.com` 通常是短时签名媒体地址，不作为结果。
- **没有结果不代表没有视频**：可能是页面未重新打开、流量未经过代理、链接只存在于客户端内部状态，或公开链接依赖已过滤的临时凭证。报告时必须说明是哪一层没有观察到。
- **结束时恢复系统代理**：用户明确结束常驻拦截后运行 `proxy-enhancer-session-finish --yes`，避免系统继续指向已停止的本地端口。

视频下载入口：

- **Exporter 直接下载**：先运行常驻增强代理并打开目标视频页完成短时媒体凭证捕获，再执行 `python3 {baseDir}/scripts/wechat_exporter.py exporter-download --account-id <id> --latest <N> --include-video --video-quality highest`。输出在账号目录 `videos/` 下，文件名前缀为 `[视频]`。
- **视频号页面按钮**：常驻增强代理会尝试在 `channels.weixin.qq.com/web/pages/...` 页面注入“下载视频”按钮；按钮只在用户当前有权限播放、页面 JS 暴露 `objectDesc.media[]` 后可下载。
- **无凭证必须明示**：Exporter 未发现活跃代理或匹配的短时媒体描述时返回 `needs_capture`，不得报告视频下载成功。处理方式是保持/启动 `proxy-enhancer-session-start --yes`，重新打开目标视频页，等按钮显示“下载视频”后再重跑。
- **敏感字段只留内存**：`urlToken`、`decodeKey`、签名媒体 URL、Cookie 和请求头只在代理进程内短时保存和下载使用；禁止写入 SQLite、Markdown、JSONL、调试日志或聊天输出。结果只报告页面 URL、descriptor id、文件路径、大小、是否解密。
- **能力边界**：不绕过私密、付费或 DRM 内容；只下载用户当前会话能播放的公开视频。加密视频会按 `decodeKey` 生成 128 KiB key stream 并 XOR 文件头，失败时标记 `failed` / `decrypt_failed`。

调试日志位于 `~/.moore/wechat-article-downloader/proxy-snapshots/debug.jsonl`，只记录脱敏事件：请求路径、响应状态、是否识别为文章页、是否注入脚本、页面脚本是否执行、按钮是否放置成功。日志只保留 24 小时；清理动作每 12 小时最多执行一次。

成功识别并注入的文章 HTML 响应必须写入 `Cache-Control: no-store`，并移除 `ETag` / `Last-Modified`，防止微信 WebView 在下一次会话继续复用未更新的注入代码。已禁用的系统代理状态视为没有 endpoint；不要保存或展示其残留的 server/port。

不要在用户点击保存后自动恢复系统代理。只有用户明确要求恢复时才运行：

```bash
python3 {baseDir}/scripts/wechat_downloader.py proxy-enhancer-session-finish --yes
```

该命令先恢复系统代理，再停止并回收本次会话的随机端口进程。

用户之后正常打开公众号文章。页面加载完成后应自动出现注入按钮：顶部元信息行一个，评论区标题旁一个，点击任意一个都会保存当前文章现场。

```text
收藏到本地
```

用户点击后读取最近快照：

```bash
python3 {baseDir}/scripts/wechat_downloader.py snapshot-latest
python3 {baseDir}/scripts/wechat_downloader.py snapshot-list --limit 10
```

如果用户说“已保存”“处理刚才保存的快照”“整理今天保存的快照”，默认不要只处理 latest。先看快照收件箱，再把所有未处理快照归档到文章库：

```bash
python3 {baseDir}/scripts/wechat_downloader.py snapshot-inbox --limit 50
python3 {baseDir}/scripts/wechat_downloader.py snapshot-attach --all-unprocessed
```

归档规则：

- 已下载过的文章：按 URL/微信文章参数匹配，增强数据放到对应公众号目录。
- 没下载过的文章：从快照正文生成 Markdown，也放到 `~/Downloads/wechat-articles/<公众号名>/`。
- 评论、阅读数、点赞数、收藏数等页面数据必须写进文章 Markdown 的 `## 页面数据` 区块；同一篇文章重复归档时替换旧区块，不重复追加。
- 同一篇文章多次保存：保留每次快照，并更新 `latest.json` 和 `metrics_history.jsonl`。
- 不递归复制原始快照目录；只归档结构化提取产物，避免把敏感或调试文件带进文章库。

把最近快照提取成后续处理用的结构化文件：

```bash
python3 {baseDir}/scripts/wechat_downloader.py snapshot-extract latest
```

也可以指定快照：

```bash
python3 {baseDir}/scripts/wechat_downloader.py snapshot-extract <snapshot-id> --output-dir <dir>
```

输出位于：

```text
~/.moore/wechat-article-downloader/proxy-snapshots/<snapshot-id>/
```

主要文件：

- `snapshot.json`：DOM 快照和基础字段
- `dom.html`：脱敏后的完整页面 DOM
- `js_content.html`：正文 DOM
- `comments_dom.html`：页面已渲染评论 DOM
- `engagement_dom.html`：互动区域 DOM
- `metrics.json`：阅读、点赞、在看、评论、收藏、分享的可观察结果
- `network.jsonl`：脱敏后的网络响应摘要
- `style_profile.json` / `style_summary.md`：页面风格摘要
- `report.md`：本次快照报告

`snapshot-extract` 默认输出到 `<snapshot-dir>/extracted/`：

- `article.md`：正文 Markdown
- `comments.json`：已加载评论文本和原始评论 DOM，`complete=false`
- `comments_structured.json`：结构化评论，包含昵称、地区/时间、点赞、回复数、内容
- `metrics.json`：可观察互动数据
- `style_profile.json`：页面风格特征
- `image_urls.json`：正文图片 URL 列表
- `engagement.html`：互动区域 DOM
- `report.md`：提取报告

规则：不承诺一定拿到所有互动字段；页面未暴露时必须标记 `missing`。不要保存或打印 cookie、auth-key、token、pass_ticket、key 等敏感值。

文章落盘规则：标题和 Markdown 文件名必须带 `[图文]` 或 `[贴图]` 前缀；微信 `item_show_type=8` 判定为贴图，其他文章判定为图文。贴图 Markdown 先集中输出全部图片，再输出文字内容。

### 文章类型与图片完整性

- **类型判定只看微信字段**：`item_show_type=8` 是贴图；不要根据图片数量、正文长度或是否存在 `content_noencode` 猜类型。普通图文也可能包含 `content_noencode`。
- **正文提取顺序**：优先使用非空 `#js_content`；为空或缺失时安全解码 `content_noencode`，禁止执行页面 JavaScript。
- **贴图图片兜底**：贴图正文没有 `<img>` 时，优先读取 `share_imageinfo[].cdn_url` 中公开的 `mmbiz.qpic.cn` 原图地址。排除 `watermark_info`、`cover_url`、头像、封面和同图不同尺寸派生项，再按规范化 URL 或内容哈希去重。该兜底只用于贴图，避免普通图文误收装饰资源。
- **标题来源优先级**：页面标题有效时使用页面标题；页面返回 `untitled`、空标题或删除提示时，回退到 Exporter 已同步的数据库标题。frontmatter、`index.csv` 和文件名必须一致。
- **删除文章单独标记**：页面出现“该内容已被发布者删除”等提示时，不得报告为“全文下载成功”；保留历史标题和 URL，并明确标记正文不可用。
- **贴图验收**：至少检查 `content_type=贴图`、`item_show_type=8`、`image_count>0`、所有本地图片存在，以及第一个图片引用位于正文内容之前。还要抽查图片确为文章内容图，不能只看数量；若 `image_count=0`，必须继续排查元数据图片清单，不能直接结束。
- **图文验收**：检查标题/文件名 `[图文]` 前缀、正文非空、Markdown 图片引用对应本地文件。图片下载失败要在 `error` 中保留原因。

下载后建议按以下顺序验收：

```text
命令结果 -> index.csv 类型/数量 -> Markdown 标题与排序 -> 图片文件存在 -> 删除/风控页面检查 -> 聊天中报告真实结果
```

## 场景 3：多账号订阅 + 定时同步

**触发**：用户需要长期跟踪多个公众号，希望每天自动同步并下载新文章。

**已有能力**：

```bash
# 搜索并添加公众号
python3 {baseDir}/scripts/wechat_exporter.py exporter-search "关键词"
python3 {baseDir}/scripts/wechat_exporter.py exporter-add --fakeid "<fakeid>" --nickname "<name>"

# 查看已订阅的公众号列表
python3 {baseDir}/scripts/wechat_exporter.py exporter-accounts

# 同步单个账号文章
python3 {baseDir}/scripts/wechat_exporter.py exporter-sync --account-id "<id>" --limit 200

# 下载指定账号最新 N 篇
python3 {baseDir}/scripts/wechat_exporter.py exporter-download --account-id "<id>" --latest 20
```

```bash
# 同步所有已订阅账号（默认每个账号最新 50 篇）
python3 {baseDir}/scripts/wechat_exporter.py exporter-sync-all
python3 {baseDir}/scripts/wechat_exporter.py exporter-sync-all --per-account-limit 100

# 下载所有尚未下载的新文章
python3 {baseDir}/scripts/wechat_exporter.py exporter-download-new

# 每日一键运行：sync-all + download-new（适合 cron）
python3 {baseDir}/scripts/wechat_exporter.py exporter-daily-run
```

设置 cron 定时任务时，用 AI 帮用户生成 launchd plist 或 crontab 条目，指定绝对路径调用 `exporter-daily-run`。

## Exporter 登录

**QR 流程（首选）**：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-start --open
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-status "<login-id>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-complete "<login-id>"
```

**降级：手动 auth-key**（QR 不可用时）：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-start --open
python3 {baseDir}/scripts/wechat_exporter.py exporter-config --auth-key "<auth-key>"
```

规则：扫码时必须选公众号/服务号，不要选小程序；auth-key 有效期约 4 天；不在聊天中打印完整 auth-key；auth-key 优先存 macOS Keychain。

二维码交互要求：

1. 运行 `exporter-login-qr-start --open` 生成新二维码并尝试直接打开；不要省略 `--open`。
2. 如果二维码没有自动打开，确认 `qrcode_path` 文件存在且为 `.png` / `.jpg` / `.jpeg`，再按平台调用系统打开命令。
3. 用户确认扫码后运行 `exporter-login-qr-status`，不要靠等待时间猜测状态。
4. 状态确认后运行 `exporter-login-qr-complete`，只向用户报告登录是否成功和有效期，不展示 auth-key 或其预览。

系统打开命令：

| 环境 | 首选命令 | 备用命令 |
|------|----------|----------|
| macOS | `open "<qrcode_path>"` | `open "file://<absolute-path>"` |
| Windows PowerShell | `Start-Process -FilePath "<qrcode_path>"` | `explorer.exe "<qrcode_path>"` |
| Windows CMD | `start "" "<qrcode_path>"` | `explorer.exe "<qrcode_path>"` |
| Linux 桌面 | `xdg-open "<qrcode_path>"` | `gio open "<qrcode_path>"` |
| WSL | `explorer.exe "$(wslpath -w '<qrcode_path>')"` | `cmd.exe /c start "" "$(wslpath -w '<qrcode_path>')"` |

执行规则：

- **本地路径优先**：二维码是本地图片，直接打开路径比转换成网页 URL 更稳定。
- **禁止拼接不可信 shell 字符串**：代码实现应使用参数数组调用进程；Windows Python 优先使用 `os.startfile()`，避免 `cmd /c start` 的引号陷阱。
- **无桌面环境要降级**：SSH、容器或 Linux 没有 `DISPLAY` / `WAYLAND_DISPLAY` 时不要反复调用打开命令，改为聊天展示二维码图片。
- **打开后仍要提示动作**：系统窗口弹出后明确告诉用户“请用微信扫码并在手机端确认”，不能只执行命令后沉默。

本地管理页（仅用户明确要求时启动）：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-server-start --port 8765
```

## 路径和输出

`{baseDir}` = 本 `SKILL.md` 所在目录，脚本位于 `{baseDir}/scripts/`。

内部会话数据：`~/.moore/wechat-article-downloader/`

默认下载目录：

- URL 模式：`~/Downloads/wechat-articles/<公众号名>/`；多公众号 URL 会按公众号拆分目录
- Exporter 单公众号模式：`~/Downloads/wechat-articles/<公众号名>/`
- Exporter 多公众号模式：按公众号拆分为多个 `~/Downloads/wechat-articles/<公众号名>/`

输出结构：

```text
index.csv
articles/<safe-title>.md
images/<safe-title>/<image-number>.<ext>
```

URL 模式和 Exporter 模式都不再使用可见的 `<run-id>` 交付目录。`run_id` 只保存在运行记录里。Exporter 下载会先检查账号目录里的 `index.csv` 和实际 Markdown/图片文件；SQLite 标记为已下载但文件缺失时会重新下载。

## 输出约定

每次结束报告：使用的模式、成功/失败数量、失败 URL（如有）、`output_dir`、`index.csv`。

报告中的“成功”必须区分：正文完整、图片完整、源文章已删除。不要把 HTTP 请求成功或 Markdown 文件生成成功描述成“全文和图片均已获取”。

Exporter 模式额外报告：auth-key 是否有效、公众号/文章数量、SQLite 路径。

历史文章模式：必须先在聊天中列出文章标题和日期，让用户选择，不要自动全量下载。

保持回复简洁，详细内容由文件承载。

## 参考文件

- `references/backend-design.md`：两模式架构
- `references/skill-cli-flow.md`：Skill 驱动 CLI 流程
- `references/output-formats.md`：输出文件结构
- `references/compliance.md`：安全和权限规则
- `references/troubleshooting.md`：常见失败和处理
