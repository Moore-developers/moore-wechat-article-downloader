# 公众号归档器

一个本地优先的 Skill：批量归档微信公众号文章，持续跟踪关注的公众号，也能在浏览时随手保存文章、评论和页面数据。

```text
把这些公众号文章连同图片归档到本地：https://mp.weixin.qq.com/s/xxx
汇总并列出所有已订阅公众号最近 50 篇文章，按公众号和日期整理
开启代理增强，我要边看公众号文章，边保存有价值的文章和评论
```

文章最终保存为 Markdown、图片和索引，方便搜索、整理和长期积累。已知文章 URL 无需登录；批量跟踪公众号默认使用 Exporter；浏览收藏用于保存当前页面已经加载的文章、评论和互动数据。

## 安装

把项目克隆到 Codex 或 Claude Code 的 Skills 目录：

```bash
# Codex
mkdir -p ~/.codex/skills
git clone https://github.com/Moore-developers/moore-wechat-article-downloader.git \
  ~/.codex/skills/moore-wechat-article-downloader

# Claude Code
mkdir -p ~/.claude/skills
git clone https://github.com/Moore-developers/moore-wechat-article-downloader.git \
  ~/.claude/skills/moore-wechat-article-downloader
```

重新打开 Codex / Claude Code 对话后，直接用自然语言提出下载或同步需求。基础下载只需要 Python 3，不需要 Dashboard、浏览器插件或微信登录。

按需依赖：

- Exporter 历史列表需要扫码登录自己的微信公众号后台；不需要拥有目标公众号。
- 代理历史和代理增强需要 macOS 微信桌面客户端、mitmproxy 及受信任的本地证书。
- 自动切换和恢复系统 HTTP/HTTPS 代理目前只支持 macOS。

## 能做什么

| 应用场景 | 你可以做什么 | 默认方案 | 登录 | 代理 |
|---|---|---|---:|---:|
| 收藏了几篇好文章，想永久留存 | 下载一个或多个已知 URL，连同正文和图片归档 | 直接下载 | 否 | 否 |
| 想系统整理一个公众号 | 获取历史列表，按标题、日期或范围选择下载 | Exporter | 是 | 否 |
| 长期关注一批公众号 | 订阅多个公众号，定时同步并下载新文章 | Exporter | 是 | 否 |
| 没有公众号后台，仍想查看历史文章 | 从微信内置浏览器抓取实际加载的历史列表 | 代理历史 | 否 | 是 |
| 阅读时发现好文章或高价值评论 | 边浏览边保存正文、已加载评论和页面互动数据 | 浏览收藏（代理增强） | 否 | 是 |

## 快速开始

### 下载已知文章

```text
下载这些文章：
https://mp.weixin.qq.com/s/aaa
https://mp.weixin.qq.com/s/bbb
```

也可以提供包含文章 URL 的 `.txt`、`.csv` 或 `.json` 文件。重复运行会跳过已经成功且本地文件仍然存在的文章；失败项可以单独重试。

### 获取公众号历史文章

```text
列出「某公众号」最近 50 篇让我选
```

默认流程：

1. 未登录时生成二维码。
2. 扫码登录自己的微信公众号后台。
3. Skill 搜索公众号并同步历史列表。
4. 在对话中列出文章标题和日期。
5. 按标题、日期、编号范围或最新 N 篇选择下载。

登录只用于搜索公众号和读取历史文章列表。Exporter 不获取评论、阅读数、点赞数或收藏数。

### 订阅和增量同步

```text
搜索并添加公众号「某公众号」
同步所有已订阅公众号的新文章
下载所有尚未下载的新文章
```

订阅信息和文章状态保存在本地 SQLite 中。需要定时执行时，可以让 Codex / Claude Code 生成调用 `exporter-daily-run` 的 launchd 或 crontab 配置。

## 不登录获取历史列表

只有在明确不想登录，或 Exporter 不可用时，才使用代理历史模式：

```text
我不登录，用代理获取这个公众号的历史文章：https://mp.weixin.qq.com/s/xxx
```

Skill 会生成公众号旧版历史入口。用微信桌面客户端内置浏览器打开并向下滚动后，Skill 只会列出实际加载到的文章，因此不能保证覆盖全部历史。

此流程会修改系统代理。启用前必须明确确认，结束时由 Skill 恢复原来的代理设置。

## 边浏览边收藏文章和评论

这个场景适合日常阅读：在微信里正常浏览公众号文章，遇到值得保留的内容时，随手把文章、已加载评论和页面数据保存到本地资料库。

开始浏览前说：

```text
开启代理增强
```

Skill 会开启代理增强，并重置微信的文章 WebView 进程以加载最新注入代码；微信主进程和登录状态不会退出。在微信文章页等待正文和评论加载完成，然后点击注入的 `收藏到本地` 按钮。你可以继续浏览并收藏多篇，不需要每篇都回到对话操作。成功注入的文章页会禁用本地缓存，后续重载不会继续复用旧按钮代码。

浏览结束后说：

```text
已保存，帮我整理
```

Skill 会一次处理所有尚未整理的收藏：已归档文章会补充页面数据，新文章会生成 Markdown。可保存内容包括：

- 页面已经加载的评论及其昵称、地区或时间、点赞和回复。
- 页面当前暴露的阅读数、点赞数、评论数和在看数。
- 正文、图片 URL 和可观察的页面排版信息。

这些数据受微信页面结构和加载状态影响：评论不保证完整，未暴露的指标会标记为 `missing`，不会猜测。

结束增强模式时说：

```text
取消代理增强
```

Skill 会恢复原来的系统代理，停止本次随机端口的代理进程，并保留已收藏的数据。详细的代理安装、路由检查和安全停止方式见 [`references/troubleshooting.md`](references/troubleshooting.md)。

## 输出

默认按公众号保存：

```text
~/Downloads/wechat-articles/<公众号名>/
├── articles/<文章标题>.md
├── images/<文章标题>/
├── snapshots/              # 使用代理增强时生成
└── index.csv
```

`index.csv` 记录标题、公众号、原文 URL、本地 Markdown、图片数量和下载状态。运行状态、登录资料和任务记录保存在 `~/.moore/wechat-article-downloader/`，不会混入交付目录。

## 平台与边界

- 直接下载和 Exporter 是主要能力；代理历史是备用方案，代理增强是可选能力。
- 只处理用户有权访问的公开文章，不绕过登录、付费墙或私密内容。
- 不在对话中打印 cookie、auth-key、token、pass_ticket 等敏感凭据。
- Exporter 凭据优先保存在 macOS Keychain，内部状态保存在本地 SQLite。
- 页面快照和调试日志会脱敏；不会递归复制原始快照到文章库。
- 项目不提供内容改写、云端托管或 SaaS 服务。

## 开发与文档

三个 CLI 分工如下：

- `scripts/wechat_wizard.py`：自然语言任务、登录和选择 gate 的统一入口。
- `scripts/wechat_exporter.py`：公众号搜索、历史同步、订阅和增量下载。
- `scripts/wechat_downloader.py`：文章下载、代理历史和页面快照归档。

运行离线契约测试：

```bash
python3 -m unittest discover -s evals -p 'test_*contract.py'
```

更多细节：

- [`SKILL.md`](SKILL.md)：Skill 的执行规则和完整入口。
- [`references/skill-cli-flow.md`](references/skill-cli-flow.md)：各场景的 CLI 流程。
- [`references/output-formats.md`](references/output-formats.md)：输出格式和字段。
- [`references/troubleshooting.md`](references/troubleshooting.md)：安装与故障排查。
- [`references/compliance.md`](references/compliance.md)：安全和权限边界。
