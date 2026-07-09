# Moore 微信公众号文章下载器

本项目是一个本地优先的 Codex Skill，用自然语言下载、同步、整理微信公众号文章。

它不是内容改写工具，也不是 SaaS 平台。核心目标只有一个：把公众号文章和必要的页面现场数据整理成本地可二次处理的文件。

## 适合做什么

- **下载文章正文**：单篇、多篇 URL，输出 Markdown 和图片
- **获取历史文章**：按公众号搜索、同步历史列表，让用户选择下载
- **订阅公众号**：多个公众号长期增量同步
- **补充页面现场数据**：对重点文章保存评论、阅读数、点赞数、页面风格
- **整理快照收件箱**：用户连续保存多个快照后，一次性归档到文章库

## 场景速查

| 用户自然语言 | 对应场景 | 默认模式 | Skill 应做什么 |
|---|---|---|---|
| “下载这篇文章 URL” | 单篇 URL 下载 | URL 下载 | 直接下载正文、图片、写入公众号目录 |
| “下载这些 URL” | 多篇 URL 下载 | URL 下载 | 按公众号拆目录批量下载 |
| “下载 urls.txt 里的文章” | 文件批量下载 | URL 下载 | 读取文件中的 URL 后批量下载 |
| “获取某公众号历史文章” | 历史列表 | Exporter | 扫码登录、搜索公众号、同步文章、列出标题让用户选 |
| “下载 AI观察站最近 20 篇” | 历史列表 + 选择下载 | Exporter | 同步列表后下载最新 N 篇 |
| “列出最近 50 篇让我选” | 历史列表预览 | Exporter | 只列文章标题和日期，不自动全量下载 |
| “订阅这些公众号” | 多公众号订阅 | Exporter | 添加账号到本地 SQLite |
| “同步所有订阅号的新文章” | 增量同步 | Exporter | 同步并下载新增文章 |
| “我没有公众号后台 / 不想登录” | 历史列表备用 | 代理历史 | 生成旧版历史入口，让用户用微信内置浏览器打开滚动 |
| “我要评论、阅读数、点赞数” | 单篇深度快照 | 代理增强 | 启动常驻代理，页面注入“保存这篇”按钮 |
| “已保存 / 处理刚才保存的快照” | 快照收件箱归档 | 代理增强 | 处理所有未归档快照，写回文章库 |
| “恢复代理” | 代理恢复 | 代理增强 | 恢复系统代理，不停止常驻服务 |

## 自然语言触发示例

### 1. 单篇 / 多篇 URL 下载

适合只要正文、图片、Markdown 的场景。

```text
下载这篇公众号文章：https://mp.weixin.qq.com/s/xxx
```

```text
下载这些公众号文章：
https://mp.weixin.qq.com/s/aaa
https://mp.weixin.qq.com/s/bbb
https://mp.weixin.qq.com/s/ccc
```

```text
把 /Users/me/Desktop/urls.txt 里的公众号文章全部下载
```

Skill 行为：

- 识别 URL
- 下载正文和图片
- 按公众号名写入 `~/Downloads/wechat-articles/<公众号名>/`
- 生成 `index.csv`
- 已下载过的文章尽量跳过，缺文件时重新下载

### 2. 公众号历史文章列表

默认走 Exporter 模式。适合你已经有微信公众号后台账号，并愿意扫码登录。

```text
获取公众号「AI观察站」的历史文章，列出最近 50 篇让我选
```

```text
下载「产品手记」最近 3 篇文章
```

```text
同步「技术周报」，列出最近 20 篇
```

Skill 行为：

- 检查 Exporter 登录状态
- 未登录时给二维码
- 搜索公众号
- 同步历史文章到 SQLite
- 在聊天里列出标题和日期
- 等用户选择后再下载

边界：

- Exporter 模式不获取评论、阅读数、点赞数、收藏数
- 不要把历史列表同步等同于全量下载，默认必须先列出来让用户选

### 3. 多公众号订阅和增量同步

适合长期追踪多个公众号。

```text
订阅「AI观察站」「产品手记」「设计参考」
```

```text
同步所有订阅公众号的新文章
```

```text
每天同步一次我订阅的公众号
```

Skill 行为：

- 把公众号写入本地 SQLite
- 每次同步只补新增文章
- 下载结果仍然按公众号拆目录

### 4. 代理历史列表备用模式

适合不想用 Exporter、没有后台账号、或 Exporter 不可用时。

```text
用代理模式获取这个公众号的历史文章：https://mp.weixin.qq.com/s/xxx
```

```text
我不登录，帮我抓这个公众号历史列表
```

Skill 行为：

- 从样例文章 URL 提取 `__biz`
- 生成旧版历史入口
- 启动或复用本地代理
- 让用户把入口发到微信文件传输助手
- 用户在微信桌面客户端内置浏览器里打开并滚动
- Skill 捕获已滚动加载出来的历史文章
- 列出文章，让用户选择下载

边界：

- 只能拿到用户实际滚动加载过的文章
- 这个模式是备用，不是默认历史列表方案

### 5. 代理增强快照

适合已经打开某篇文章，想补充批量下载拿不到的数据。

```text
我要下载这篇文章的评论、阅读数和点赞数
```

```text
开启代理增强
```

```text
我已经打开文章了，页面上点了保存这篇
```

Skill 行为：

- 启动常驻代理增强
- 微信文章页自动注入 `保存这篇` 按钮
- 用户点击后保存页面现场
- 用户说“已保存”后，Skill 处理快照收件箱

快照能补充：

- 页面已加载评论
- 阅读数、点赞数、评论数、在看等页面可见互动数据
- 正文 DOM、互动区域 DOM
- 图片 URL
- 页面排版和风格特征

快照不保证：

- 全量评论
- 收藏数
- 页面没有暴露的隐藏字段

### 6. 快照收件箱归档

用户可能连续看多篇文章，连续点很多次 `保存这篇`，最后只说一句“已保存”。

```text
已保存，帮我整理刚才保存的快照
```

```text
处理今天保存的快照
```

```text
把保存的快照更新到文章库
```

Skill 行为：

- 查看快照收件箱
- 默认处理所有未处理快照，不只处理 latest
- 已下载过的文章：把快照挂到对应文章目录
- 没下载过的文章：从快照正文生成 Markdown 并写入文章库
- 同一篇文章多次保存：保留所有快照，更新 `latest.json` 和 `metrics_history.jsonl`
- 只归档结构化产物，不复制原始调试 DOM

## 输出目录

默认输出到：

```text
~/Downloads/wechat-articles/
```

文章下载结构：

```text
~/Downloads/wechat-articles/<公众号名>/
├── index.csv
├── articles/
│   └── <文章标题>.md
└── images/
    └── <文章标题>/
        └── 001.jpg
```

快照增强结构：

```text
~/Downloads/wechat-articles/<公众号名>/
├── articles/
│   └── <文章标题>.md
└── snapshots/
    └── <文章标题或文章key>/
        ├── latest.json
        ├── metrics_history.jsonl
        └── snapshots/
            └── <snapshot-id>/
                ├── article.md
                ├── comments.json
                ├── metrics.json
                ├── style_profile.json
                ├── image_urls.json
                ├── engagement.html
                └── report.md
```

## 模式选择原则

- **只要正文**：URL 下载
- **要某公众号历史列表**：Exporter
- **没有后台账号 / 不想登录**：代理历史备用
- **要评论、阅读数、点赞数、风格**：代理增强快照
- **连续保存了很多页面**：快照收件箱归档
- **长期追踪多个公众号**：订阅同步

## 登录和代理

### Exporter 登录

Exporter 模式需要微信公众号后台登录。首次使用时 Skill 会生成二维码，用户扫码确认后，登录信息保存在本地。

适合：

- 搜索公众号
- 同步历史文章列表
- 管理订阅公众号
- 下载选中的历史文章

不适合：

- 评论
- 阅读数
- 点赞数
- 收藏数

### 代理增强

代理增强用于当前微信文章页面的现场快照。

常见链路：

```text
系统/微信 -> 127.0.0.1:23344 -> 当前上游代理或直连 -> 外网
```

只有用户明确要求恢复代理时，才恢复系统代理。

## 安全边界

- 不绕过登录、付费墙、私密内容
- 不打印 cookie、token、auth-key、pass_ticket
- 代理增强只保存脱敏后的页面信息
- 快照归档只复制结构化产物，不复制原始调试 DOM
- 页面未暴露的数据标记为 `missing`，不猜

## 安装要求

基础下载和 Exporter：

```bash
python3 --version
```

代理模式需要：

```bash
pip install mitmproxy
```

## 参考文档

- [`SKILL.md`](SKILL.md)：Skill 场景和执行规则
- [`references/skill-cli-flow.md`](references/skill-cli-flow.md)：CLI 流程
- [`references/backend-design.md`](references/backend-design.md)：架构设计
- [`references/output-formats.md`](references/output-formats.md)：输出格式
- [`references/troubleshooting.md`](references/troubleshooting.md)：排障
- [`references/compliance.md`](references/compliance.md)：安全与权限

## License

MIT. See [`LICENSE`](LICENSE).
