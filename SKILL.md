---
name: moore-wechat-article-downloader
description: 当用户要下载或管理微信公众号文章时使用本技能。支持三类入口：粘贴单篇/多篇文章 URL 直接下载；提供任意一篇公众号文章 URL，按 qiye45/wechatDownload 风格抓取历史文章列表；或在用户已有微信公众号后台/exporter auth-key 时启用 wechat-article-exporter 风格模式，扫码登录、搜索公众号、SQLite 管理公众号/文章/合集/字段，再下载选中文章。优先本地运行；不要扩展成内容改写、总结、SaaS 或云服务。
---

# Moore 微信公众号文章下载器

本技能只做三件事：

1. 下载已知的单篇或多篇微信公众号文章 URL。
2. 通过任意一篇样例文章 URL，抓取该公众号历史文章列表，让用户选择后下载。
3. 在用户已有公众号后台登录能力时，使用 Exporter 模式搜索、管理、同步公众号文章和合集，再下载选中文章。

不要把本技能扩展成内容改写、总结、标签、云部署或 SaaS。默认不启动 Dashboard 或本地管理页；只有用户明确要求图形化管理公众号、字段或合集时，才把 exporter 管理页作为可选工具。

## 范围

可以做：

- 下载一个公开的 `mp.weixin.qq.com` 文章 URL
- 下载用户粘贴的多个文章 URL，或 `.txt`、`.csv`、`.json` 文件中的 URL
- 从一篇样例文章启动公众号历史文章会话
- 引导用户用微信桌面客户端内置浏览器打开旧版历史入口
- 在安全的本地上下文存在后，抓取、罗列、选择历史文章
- 用普通 URL 下载管线下载选中的历史文章
- Exporter 模式下，通过统一 wizard 或底层命令引导用户扫码登录 exporter、保存 auth-key、搜索公众号、添加公众号、同步文章和合集
- 用 SQLite 保存 Exporter 登录状态、公众号、文章、字段配置、合集、同步任务和下载记录

不要做：

- 绕过登录、付费墙、删除内容、私密内容或平台权限
- 在用户没有明确动作的情况下发明或提取凭据
- 打印 cookie、token、pass_ticket、key 或认证 header
- 把“历史列表抓取”和“已知 URL 下载”混为一谈
- 明文打印 auth-key、cookie、token 或公众号后台凭据

## 输出目录

默认下载目录：

```text
~/Downloads/wechat-articles/<run-id>/
```

主要输出：

```text
index.csv
articles/<seq>-<safe-title>.md
images/<seq>/<image-number>.<ext>
```

内部会话数据保存在：

```text
~/.moore/wechat-article-downloader/
```

## 脚本路径

把当前 `SKILL.md` 所在目录作为 `{baseDir}`。脚本位于 `{baseDir}/scripts/`。

统一入口优先：

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "<自然语言目标>"
python3 {baseDir}/scripts/wechat_wizard.py resume "<task-id>" --choice "<选择>"
python3 {baseDir}/scripts/wechat_wizard.py login-status "<task-id>"
python3 {baseDir}/scripts/wechat_wizard.py login-complete "<task-id>"
python3 {baseDir}/scripts/wechat_wizard.py retry "<task-id>"
python3 {baseDir}/scripts/wechat_wizard.py doctor
python3 {baseDir}/scripts/wechat_wizard.py doctor --clear-stale-locks
python3 {baseDir}/scripts/wechat_wizard.py smoke all-offline
python3 {baseDir}/scripts/wechat_wizard.py smoke url-fixture
python3 {baseDir}/scripts/wechat_wizard.py smoke history-preflight --url "<sample-article-url>"
python3 {baseDir}/scripts/wechat_wizard.py smoke qr-session --task-id "<task-id>"
python3 {baseDir}/scripts/wechat_wizard.py smoke list
python3 {baseDir}/scripts/wechat_wizard.py config
python3 {baseDir}/scripts/wechat_wizard.py config --html-concurrency 2 --max-retries 1 --no-assets
```

当前 `wechat_wizard.py` 已落地 URL 单篇/多篇下载、同 task 重跑跳过已成功 URL、低并发/重试参数、失败项 retry、输出目录锁、stale lock doctor 清理、Exporter 本地账号列表/最近 N 篇下载、未登录自动 QR 会话、QR 状态轮询/完成后恢复父任务、账号/文章选择恢复、History 旧版入口生成与 `need_proxy_confirm` 边界、输入/模式/环境/下载/验证门禁、SQLite task/gate/choice/lock/download run/download item/user preferences/smoke run 记录、run 指标和 doctor。`run`、`resume`、`login-complete`、`retry` 会读取本地下载偏好，`doctor` 会检查最近一次 run 的 manifest 完整性，`smoke` 会把离线/真实流程检查写入 SQLite。底层命令仍作为内部能力，继续按 `docs/plans/2026-07-06-unified-wizard-gates.md` 推进真实 history/QR smoke 和更细的性能观测。

URL 下载模式：

```bash
python3 {baseDir}/scripts/wechat_downloader.py download-url "<url>"
python3 {baseDir}/scripts/wechat_downloader.py download-list "<path-or-text>"
python3 {baseDir}/scripts/wechat_downloader.py download-list "<path-or-text>" --output-dir "<dir>"
```

历史文章模式：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-capture-prepare "<sample-article-url>" --yes
python3 {baseDir}/scripts/wechat_downloader.py history-capture-finish "<session-id>" --yes
python3 {baseDir}/scripts/wechat_downloader.py history-fetch "<session-id>" --limit 50
python3 {baseDir}/scripts/wechat_downloader.py history-preview --session-id "<session-id>"
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --latest 20
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --range "1-20"
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --contains "keyword"
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --titles "title keyword"
python3 {baseDir}/scripts/wechat_downloader.py history-download-selected --session-id "<session-id>"
python3 {baseDir}/scripts/wechat_downloader.py history-download-selected --session-id "<session-id>" --output-dir "<dir>"
```

验证下载结果：

```bash
python3 {baseDir}/scripts/validate_outputs.py "<run-dir>"
```

Exporter 模式：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-init
python3 {baseDir}/scripts/wechat_wizard.py run "用 exporter 模式下载公众号「公众号名」最新 20 篇"
python3 {baseDir}/scripts/wechat_wizard.py run "同步「公众号名」，列出最近 50 篇让我选"
python3 {baseDir}/scripts/wechat_wizard.py resume "<task-id>" --choice "1"
python3 {baseDir}/scripts/wechat_wizard.py resume "<task-id>" --choice "1-3"
python3 {baseDir}/scripts/wechat_wizard.py login-status "<task-id>"
python3 {baseDir}/scripts/wechat_wizard.py login-complete "<task-id>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-server-start --port 8765
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-start --base-url "https://down.mptext.top" --open
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-start --base-url "https://down.mptext.top" --open
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-status "<login-id>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-complete "<login-id>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-config --base-url "https://down.mptext.top" --auth-key "<auth-key>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-auth-check
python3 {baseDir}/scripts/wechat_exporter.py exporter-search "公众号关键词"
python3 {baseDir}/scripts/wechat_exporter.py exporter-add --fakeid "<fakeid>" --nickname "<name>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-accounts
python3 {baseDir}/scripts/wechat_exporter.py exporter-sync --account-id "<id>" --limit 200
python3 {baseDir}/scripts/wechat_exporter.py exporter-articles --account-id "<id>" --limit 100
python3 {baseDir}/scripts/wechat_exporter.py exporter-fields
python3 {baseDir}/scripts/wechat_exporter.py exporter-fields --set "title,url,publish_time,author,digest,content_downloaded"
python3 {baseDir}/scripts/wechat_exporter.py exporter-collections --account-id "<id>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-download --account-id "<id>" --latest 20
python3 {baseDir}/scripts/wechat_exporter.py exporter-download-collection --collection-id "<id>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-metrics-import "<json-or-csv>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-comments-import "<json-or-csv>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-comments --article-id "<id>"
```

## 工作流

### 1. 判断模式

优先尝试统一入口：

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "<用户目标>"
```

如果 `wechat_wizard.py` 返回 `need_login`、`need_proxy_confirm`、`need_account_choice` 或 `need_article_choice`，这是正常交互状态，不要立刻降级。只有它返回 `blocked`、`failed_recoverable` 且明确给出底层命令，才使用下面的专用命令。不要把专用命令当作默认入口。

- **URL 下载模式**：用户提供一篇或多篇文章 URL。
- **历史文章模式**：用户提供一篇样例文章 URL，并希望获取该公众号历史文章列表。
- **Exporter 模式**：用户明确说 exporter、公众号后台、扫码登录、搜索公众号、管理公众号列表、字段配置、合集下载，或已经提供 exporter auth-key。

如果用户只给公众号名，普通历史模式不要搜索公众号；但 Exporter 模式可以用公众号关键词搜索。

### 2. URL 下载模式

单篇下载：

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "下载这篇公众号文章：<url>"
```

多篇或文件下载：

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "下载这些公众号文章：<urls-or-file>"
```

如果用户指定输出目录，追加：

```bash
--output-dir "<dir>"
```

同一个 `task-id` 重跑会默认跳过已成功下载的 URL；要强制重新下载，追加：

```bash
--task-id "<task-id>" --force
```

URL 模式默认保守串行。用户明确要求加速或网络不稳定时，可以配置低并发和重试；上限由脚本强制保护：

```bash
--html-concurrency 2 --max-retries 1
```

如果本地偏好关闭了图片，单次任务可以用 `--download-assets` 覆盖；反向也可以用 `--no-assets`。

如果某次下载有失败项，优先用统一 retry：

```bash
python3 {baseDir}/scripts/wechat_wizard.py retry "<task-id>"
```

下载完成后验证：

```bash
python3 {baseDir}/scripts/validate_outputs.py "<output-dir>"
```

回复用户时只报告：

- 成功数量
- 失败数量
- 失败 URL，如有
- `output_dir`
- `index.csv`

### 3. 历史文章模式

优先启动统一向导：

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "历史文章列表下载：<sample-article-url>"
```

wizard 会生成旧版 `profile_ext?action=home` 入口，并停在 `need_proxy_confirm`。需要底层排障时，再启动本地会话：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-start "<sample-article-url>"
```

生成并复制微信内置浏览器打开链接：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-open "<session-id>"
```

必须优先使用旧版公众号历史入口：

```text
https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=<biz>&scene=124#wechat_redirect
```

如果 `history-open` 无法生成这个链接，先修复 `biz` 提取或让用户换一篇样例文章。不要引导用户进入 `channels.weixin.qq.com/web/pages/mp_profile`，那是视频号/账号壳页，通常不会触发历史文章 `getmsg` 接口。

启动本地代理 adapter：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-capture-prepare "<sample-article-url>" --yes
```

`history-capture-prepare` 会创建历史会话、生成旧版入口、启动/复用 `8899`、临时切换系统代理。默认使用 `--upstream-proxy auto`。在 macOS 上，它会读取当前系统 HTTP 代理；如果当前已经有代理，则 mitmproxy 会串联到这个已有代理：

```text
微信/系统代理 -> mitmproxy 127.0.0.1:8899 -> 当前已有系统代理
```

只有用户明确要求时，才用 `--upstream-proxy http://host:port` 强制指定上游代理；用 `--upstream-proxy none` 可以关闭上游代理。

如果缺少 `mitmdump`，在用户同意后安装：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-proxy-setup --port 8899 --install --yes
```

启用 macOS HTTP/HTTPS 代理前必须确认。确认后执行：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-capture-prepare "<sample-article-url>" --yes
```

告诉用户：

- 公众号名称
- 旧版 `profile_ext?action=home` 历史入口
- 已把旧入口复制到剪贴板
- 把链接发到微信文件传输助手
- 用微信桌面客户端内置浏览器打开
- 看到历史文章列表后向下滚动

结束后或用户要求停止时，必须恢复代理：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-capture-finish "<session-id>" --yes
```

切换公众号或切换历史会话时，不要停止 `8899`。再次运行 `history-capture-prepare "<sample-article-url>" --yes` 会复用已有 mitmproxy 进程，并把 active session 切到新会话；系统代理保持指向 `8899`，上游代理保持为原来的系统代理，例如 `127.0.0.1:10808`。`history-proxy-stop` 如果发现系统代理仍指向 `8899`，会拒绝停止，防止网络指向死端口。

底层命令 `history-start`、`history-open`、`history-proxy-*`、`adapter-watch` 只在调试时使用；正常 Skill 流程只展示 `history-capture-prepare` 和 `history-capture-finish`。

### 4. 历史列表展示和选择

抓取完成后，先获取列表：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-preview --session-id "<session-id>" --limit 100
```

必须在聊天中直接罗列历史文章列表，格式优先使用：

```text
- **YYYY-MM-DD**：文章标题
```

不要只告诉用户 `history_articles.csv/json` 路径。CSV/JSON 是存档，不是交互界面。

不要只给编号。编号可以作为快捷方式，但必须同时展示标题和日期。

让用户可以按这些方式选择：

- 标题或标题关键词
- 日期
- 最新 N 篇
- 范围或编号，作为快捷方式

选择命令示例：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --latest 20
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --range "1-20"
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --contains "keyword"
python3 {baseDir}/scripts/wechat_downloader.py history-select --session-id "<session-id>" --titles "title keyword"
```

下载选中的文章：

```bash
python3 {baseDir}/scripts/wechat_downloader.py history-download-selected --session-id "<session-id>"
```

如果用户指定目录，追加：

```bash
--output-dir "<dir>"
```

### 5. Exporter 模式

Exporter 模式适合用户有微信公众号后台或愿意通过 wechat-article-exporter 登录。默认优先走一句话 wizard，不要让用户背一串命令：

```bash
python3 {baseDir}/scripts/wechat_wizard.py run "用 exporter 模式下载公众号「公众号名」最新 20 篇"
```

wizard 会自动做这些事：

- 检查本地是否已有登录资料和已添加公众号
- 按公众号名、alias 或 fakeid 解析目标公众号
- 命中多个公众号时，返回 `need_account_choice` 和候选列表
- 需要用户选文章时，返回 `need_article_choice` 和文章标题/日期列表
- 如果该公众号今天还没有同步过，先强制同步；无有效登录时返回 `need_login`，不要用旧缓存冒充最新列表
- `need_login` 后用 `login-status <task-id>` 轮询；确认后用 `login-complete <task-id>` 完成登录并恢复原任务
- `need_account_choice` 或 `need_article_choice` 后用 `resume <task-id> --choice "<序号/范围/关键词>"`
- 如果用户说“最新 20 篇”，同步后直接下载，不再二次确认
- 如果用户说“列出/让我选/有哪些”，只列文章，不自动下载

典型最短提示词：

```text
用 exporter 模式下载公众号「哥飞」最新 20 篇
用 exporter 模式同步「哥飞」，列出最近 50 篇让我选
用 exporter 模式根据这篇文章 URL 找公众号并列出历史文章：https://mp.weixin.qq.com/s/...
```

只有用户明确要本地管理页，才启动：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-server-start --port 8765
```

本地管理页支持：

- 本地展示 exporter 二维码并轮询扫码状态
- 登录完成后自动保存 auth-key
- 搜索公众号、添加公众号
- 多公众号切换查看文章
- 字段配置、合集查看和下载

纯 CLI 登录流程：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-start --open
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-status "<login-id>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-qr-complete "<login-id>"
```

如果二维码接口不可用，再降级到手动 auth-key：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-login-start --open
python3 {baseDir}/scripts/wechat_exporter.py exporter-config --auth-key "<auth-key>"
```

告诉用户：

- 扫码时必须选择公众号/服务号，不要选小程序
- auth-key 有效期通常约 4 天，到期需要重新扫码

验证登录：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-auth-check
```

搜索、添加、同步：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-search "关键词"
python3 {baseDir}/scripts/wechat_exporter.py exporter-add --fakeid "<fakeid>" --nickname "<name>"
python3 {baseDir}/scripts/wechat_exporter.py exporter-sync --account-id "<id>" --limit 200
```

同步后必须在聊天中列出公众号或文章的关键字段，不要只给 SQLite 路径。需要复杂管理时，启动本地管理页：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-server-start --port 8765
```

下载仍然复用 Markdown 输出目录：

```bash
python3 {baseDir}/scripts/wechat_exporter.py exporter-download --account-id "<id>" --latest 20
python3 {baseDir}/scripts/wechat_exporter.py exporter-download --article-ids "1,2,3"
python3 {baseDir}/scripts/wechat_exporter.py exporter-download-collection --collection-id "<id>"
```

安全规则：

- 不在聊天中打印完整 auth-key
- 优先把 auth-key 存 macOS Keychain；只有用户明确允许时才明文存 SQLite
- 公众号文章列表、合集和字段配置存在 `~/.moore/wechat-article-downloader/exporter.sqlite`
- 阅读、点赞、留言等增强字段是 best-effort；没有短效 Credentials 时显示为空，不阻塞文章下载。用户已有增强数据 JSON/CSV 时，可用 `exporter-metrics-import` / `exporter-comments-import` 导入 SQLite。

## 本地管理页规则

任何模式默认都不要启动 Dashboard 或本地 Web UI。Exporter 管理页只是可选排障/管理工具；用户没有明确要求时，继续用 Skill 对话和 CLI 状态完成。

## 输出约定

每次结束时报告：

- 使用的模式
- 下载目录或历史会话 ID
- 下载结果的 `index.csv`
- 成功/失败数量
- 失败 URL，如有
- 如果历史上下文未就绪，说明下一步
- Exporter 模式下还要报告 SQLite 路径、本地管理页 URL、auth-key 是否有效、公众号/文章/合集数量

历史文章模式额外要求：

- 抓到历史列表后，必须在聊天中罗列标题和日期
- 再让用户选择要下载哪些文章
- 不要只返回 CSV/JSON 路径

保持回复简洁。详细内容由文件承载。

## 参考文件

- `references/backend-design.md`：两模式架构
- `references/skill-cli-flow.md`：Skill 驱动 CLI 流程
- `references/output-formats.md`：输出文件结构
- `references/compliance.md`：安全和权限规则
- `references/troubleshooting.md`：常见失败和处理
