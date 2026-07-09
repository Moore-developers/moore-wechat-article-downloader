# 代理快照验证计划

## 结论

先验证，再实现。

Exporter 模式只保留公众号搜索、文章列表同步、文章下载。评论、阅读数、点赞数、收藏数、转发数等互动数据从 Exporter 模式移除，不再承诺、不再展示、不再提供导入命令。

合集不是当前刚需。保留现有 best-effort 能力，不继续扩展。

代理模式进入验证阶段。只有真实微信内置浏览器验证通过，才做正式功能。

## 目标

验证代理模式能否稳定拿到一篇公众号文章的完整页面快照，并从快照中提取：

- 文章正文
- 图片资源
- 评论
- 阅读数
- 点赞数
- 在看数
- 收藏数
- 评论数
- 公众号排版风格

收藏数是实验字段。微信页面未暴露时必须标记为 `missing`，不能猜。

## 非目标

- 不绕过登录、权限、付费墙或私密内容。
- 不默认保存完整 cookie、token、key、pass_ticket。
- 不把代理模式做成常驻后台。
- 不在验证前承诺评论或互动数据一定能拿到。
- 不继续扩展 Exporter 合集能力。

## 验证样本

至少选择 5 篇真实文章：

- 有评论的文章
- 无评论的文章
- 图片多的文章
- 普通图文文章
- 已知带合集入口的文章

每篇文章都用微信桌面端内置浏览器打开，确保页面实际加载完成。

## V1：网络快照验证

验证 mitmproxy 能稳定保存文章加载过程中的网络响应。

需要保存：

- `/s` 原始 HTML
- `mp.weixin.qq.com/mp/*` JSON 响应
- URL、状态码、content-type、body hash
- 脱敏后的 query/header 摘要

成功标准：

- 每篇文章都能保存 `/s` HTML。
- 能从 HTML 解析标题、公众号、发布时间、正文和图片。
- 报告列出实际出现过哪些微信接口。

失败处理：

- 如果 `/s` HTML 都抓不完整，停止后续验证。
- 优先修代理生命周期、证书、系统代理恢复，不继续做按钮或解析器。

## V2：DOM 快照按钮验证

验证能否向文章页注入一个本地按钮，让用户主动保存当前页面。

按钮行为：

- 文案：`保存当前页面`
- 采集 `document.documentElement.outerHTML`
- 采集 `document.body.innerText`
- 采集 `#js_content` HTML
- 采集评论区和底部互动区 DOM
- 采集基础样式摘要

提交方式：

- POST 到 `https://mp.weixin.qq.com/__moore_capture`
- mitmproxy 拦截保存
- 不转发给微信服务器

成功标准：

- 微信内置浏览器里按钮可见。
- 点击按钮后本地收到 DOM 快照。
- 不影响文章正常阅读、滚动和评论加载。

失败处理：

- 如果按钮无法注入，退回只做网络快照。
- 不做 JS 注入绕行或高风险页面改写。

## V3：互动指标验证

按来源优先级解析字段：

1. JSON 响应
2. DOM 快照
3. 原始 HTML/JS 变量

字段：

- `read_count`
- `like_count`
- `old_like_count`
- `comment_count`
- `favorite_count`
- `share_count`

成功标准：

- 每个字段都记录来源：`json`、`dom`、`html`、`missing`。
- 不猜数值。
- 至少 5 篇文章里稳定拿到 2 类以上互动指标，才进入正式实现。

## V4：评论验证

同时验证两条路径：

- `/mp/appmsg_comment?action=getcomment`
- DOM 中已经渲染出来的评论块

需要提取：

- 评论内容
- 昵称
- 点赞数
- 创建时间，能拿则拿
- 评论来源：`json` 或 `dom`

成功标准：

- 能区分“文章没有评论”和“抓取失败”。
- 报告明确写出是否出现评论接口、DOM 里是否出现评论块。

## V5：风格验证

从 DOM 和正文区域抽取：

- 正文字号、行高、颜色
- 标题和小标题样式
- 图片尺寸、圆角、间距
- 强调块、引用块、分割线
- 高频颜色

输出：

- `style_profile.json`
- `style_summary.md`

成功标准：

- 能给出稳定的排版风格摘要。
- 摘要只描述可观察样式，不做内容改写。

## 验证命令设计

新增验证命令时使用独立入口：

```bash
python3 scripts/wechat_downloader.py proxy-snapshot-verify "<article-url>"
```

验证输出目录：

```text
~/.moore/wechat-article-downloader/proxy-snapshot-runs/<run-id>/
  raw.html
  dom.html
  network.jsonl
  metrics.json
  comments.json
  style_profile.json
  style_summary.md
  report.md
```

验证命令只做证据采集和报告，不写正式文章下载目录，不改 Exporter SQLite。

## 门禁

- V1 失败：不做代理快照功能。
- V2 失败：不做页面按钮，只保留网络快照。
- V3/V4 成功率不足：不承诺评论和互动数据，只输出可用证据。
- 系统代理恢复失败：该功能不得继续执行下一轮验证。

## 手动验证流程

1. Skill 启动验证会话。
2. Skill 明确询问是否临时启用 `127.0.0.1:23344`。
3. 用户确认后，Skill 复用常驻 mitmproxy，并链到当前系统代理。
4. Skill 给出文章 URL 或旧版入口。
5. 用户在微信内置浏览器打开文章。
6. 页面加载完成后，用户点击 `保存当前页面`。
7. 用户回复已完成。
8. Skill 导出验证包。
9. Skill 恢复系统代理；默认不停止常驻 `23344`。
10. Skill 输出 `report.md` 摘要。

## 当前执行顺序

1. 移除 Exporter 模式的评论和互动数据入口。
2. 用子智能体验证 Exporter 下载“向索然”最新 3 篇文章。
3. 跑本地编译和基础命令检查。
4. 进入代理快照验证 PoC，不直接做正式功能。

## 已落地 PoC

新增独立代理文章快照入口，不替换旧代理历史列表。

当前主流程改为常驻增强代理：

```bash
python3 scripts/wechat_downloader.py proxy-enhancer-start --port 23344 --upstream-proxy auto
python3 scripts/wechat_downloader.py proxy-enhancer-status --port 23344
python3 scripts/wechat_downloader.py snapshot-latest
python3 scripts/wechat_downloader.py snapshot-list --limit 10
python3 scripts/wechat_downloader.py snapshot-export "<snapshot-id>"
```

`proxy-enhancer-session-start` 启动/复用 `23344`，切换 active adapter，并把系统 HTTP/HTTPS 代理切到 `127.0.0.1:23344`。这样微信内置浏览器流量会进入增强代理。

关键边界：

- `23344` 常驻和上游串联已经由本地代理负责。
- 进入代理增强模式时由 Skill 自动建立 `WeChat -> 23344 -> 10808 -> 外网`。
- 不再新增 launchd 自启动；当前重点不是“让 23344 常驻”，而是让系统代理在用户明确进入代理增强时切到 23344。
- 完成快照后不自动恢复系统代理。只有用户明确说恢复/结束代理增强，才运行 `proxy-enhancer-session-finish --yes`。

新增检查命令：

```bash
python3 scripts/wechat_downloader.py proxy-enhancer-session-start --port 23344 --upstream-proxy auto --yes
python3 scripts/wechat_downloader.py proxy-enhancer-session-finish --yes
python3 scripts/wechat_downloader.py proxy-enhancer-route-help --port 23344
python3 scripts/wechat_downloader.py proxy-enhancer-check-ingress --port 23344 --minutes 10
```

旧调试入口仍保留，但不作为 Skill 主流程：

```bash
python3 scripts/wechat_downloader.py proxy-snapshot-prepare "<article-url>" --port 23344 --upstream-proxy auto --yes
python3 scripts/wechat_downloader.py proxy-snapshot-status "<session-id>"
python3 scripts/wechat_downloader.py proxy-snapshot-finish "<session-id>" --yes
```

能力边界：

- `history-capture-*` 继续作为历史列表备用，不改原流程。
- `proxy-enhancer-*` 负责常驻文章页增强。
- `proxy-snapshot-*` 降级为调试兼容路径。
- mitmproxy 在文章 HTML 注入 `保存当前页面` 按钮。
- 点击按钮后自动生成 `proxy-snapshots/<snapshot-id>/`，保存 DOM、正文 DOM、评论 DOM、互动 DOM、metrics、network 摘要、style profile 和 report。
