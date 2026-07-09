---
name: moore-wechat-article-downloader
description: 当用户要下载或管理微信公众号文章时使用本技能。支持三个场景：直接下载已知文章 URL；获取公众号历史文章并选择下载（默认 Exporter 模式，备选代理模式）；订阅多个公众号并定时增量同步。优先本地运行；不要扩展成内容改写、总结、SaaS 或云服务。
---

# Moore 微信公众号文章下载器

## 场景判断

| 用户意图 | 场景 | 入口 |
|---------|------|------|
| 提供了完整文章 URL | 场景 1：直接下载 | `wechat_wizard.py run "下载：<url>"` |
| 给公众号名 / 样例 URL，想要历史文章 | 场景 2A：Exporter（默认） | `wechat_wizard.py run "历史文章：<名称或URL>"` |
| 明确说"不登录"或"用代理" | 场景 2B：代理（备选） | `wechat_wizard.py run "代理历史：<url>"` |
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

Exporter 模式额外报告：auth-key 是否有效、公众号/文章数量、SQLite 路径。

历史文章模式：必须先在聊天中列出文章标题和日期，让用户选择，不要自动全量下载。

保持回复简洁，详细内容由文件承载。

## 参考文件

- `references/backend-design.md`：两模式架构
- `references/skill-cli-flow.md`：Skill 驱动 CLI 流程
- `references/output-formats.md`：输出文件结构
- `references/compliance.md`：安全和权限规则
- `references/troubleshooting.md`：常见失败和处理
