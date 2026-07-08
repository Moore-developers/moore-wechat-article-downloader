# Moore WeChat Article Downloader

微信公众号文章下载工具，支持三种模式：直接下载、账号历史抓取、Exporter API 导出。

## 功能概览

| 模式 | 适用场景 | 核心依赖 |
|------|----------|----------|
| **直接下载** | 已有文章 URL 或 URL 列表 | Python 3 标准库 |
| **账号历史** | 抓取某个公众号历史文章列表 | mitmproxy |
| **Exporter** | 通过 API 搜索账号、批量同步文章 | Python 3 标准库 |

## 输出格式

每次下载结果保存至 `~/Downloads/wechat-articles/`：

```
~/Downloads/wechat-articles/
└── <run-id>/
    ├── index.csv             # 文章元数据索引
    ├── articles/
    │   └── <seq>-<title>.md  # Markdown 正文
    └── images/
        └── <seq>/            # 文章配图
```

## 快速开始

### 直接下载单篇文章

```bash
python3 scripts/wechat_wizard.py run "帮我下载这篇文章 https://mp.weixin.qq.com/s/xxx"
```

### 批量下载（URL 列表文件）

```bash
python3 scripts/wechat_wizard.py run "批量下载 urls.txt 里的文章"
```

### 账号历史文章抓取

需先安装 mitmproxy：

```bash
pip install mitmproxy
```

然后：

```bash
python3 scripts/wechat_wizard.py run "抓取这个公众号的历史文章 https://mp.weixin.qq.com/s/xxx"
```

Wizard 会启动本地代理（端口 8899），在手机微信中打开文章列表页，流量会被自动捕获。

### Exporter 模式（搜索 + 批量同步）

```bash
python3 scripts/wechat_wizard.py run "登录 Exporter 并导出「财经」相关公众号的文章"
```

首次使用需扫码登录，凭证存储于 macOS Keychain（降级时存 SQLite）。

## 内部状态存储

工具状态存储于 `~/.moore/wechat-article-downloader/`（不影响用户输出目录）：

```
~/.moore/wechat-article-downloader/
├── context/<session-id>.json          # 会话元数据
├── runs/<run-id>/manifest.json        # 下载任务记录
├── exporter.sqlite                    # 账号与文章数据库
└── account-history/<account-id>/      # 历史抓取缓存
```

## 架构说明

```
wechat_wizard.py          ← 用户意图路由 & 交互流程
    ├── wechat_downloader.py          ← URL 下载 / 历史代理模式
    ├── wechat_exporter.py            ← Exporter API 模式
    └── wechat_history_mitm_addon.py  ← mitmproxy 流量捕获插件
```

- **Wizard** 解析用户意图，判断所需 gate（登录态、代理确认、选择操作），再调用对应后端脚本。
- **Downloader** 验证 `mp.weixin.qq.com` URL，提取 HTML 正文，转换为 Markdown，下载图片。
- **Exporter** 通过认证 API 搜索公众号、同步文章列表、按需下载，账号数据持久化至 SQLite。
- **MITM Addon** 作为 mitmproxy 插件运行，捕获微信 `getmsg` 接口响应，生成文章 CSV/JSON 索引。

## 系统要求

- Python 3.10+（无第三方依赖，标准库即可）
- mitmproxy（仅账号历史模式需要）
- macOS（Keychain 凭证存储；其他平台降级为 SQLite 存储）

## 参考文档

- [`references/backend-design.md`](references/backend-design.md) — 三层架构设计
- [`references/output-formats.md`](references/output-formats.md) — 输出文件格式规范
- [`references/compliance.md`](references/compliance.md) — 安全与权限说明
- [`references/troubleshooting.md`](references/troubleshooting.md) — 常见问题排查
- [`SKILL.md`](SKILL.md) — Skill 意图路由定义
