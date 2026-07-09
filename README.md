# Moore 微信公众号文章下载器

用自然语言下载、同步、整理微信公众号文章。默认输出 Markdown、图片和 `index.csv`，方便后续二次处理。

它不是内容改写工具，也不是 SaaS 平台；核心目标是把公开公众号文章和必要的页面现场数据保存到本地。

## 最常用说法

```text
下载这篇：https://mp.weixin.qq.com/s/xxx

下载这些文章：
https://mp.weixin.qq.com/s/aaa
https://mp.weixin.qq.com/s/bbb

列出「某公众号」最近 50 篇让我选

下载「某公众号」最近 3 篇

开启代理增强

已保存，帮我整理

取消代理增强
```

## 三种主要场景

### 1. 已有文章 URL

不需要登录。直接下载正文和图片，按公众号名保存到本地。

你会得到：

- `articles/*.md`
- `images/*`
- `index.csv`

### 2. 获取公众号历史文章

默认使用 Exporter 模式。需要扫码登录你自己的微信公众号后台，但不需要拥有目标公众号。

Skill 会先列出文章标题和日期，让你选择后再下载；不会默认全量下载。

如果你不想登录，可以明确说：

```text
我不登录，用代理模式获取这个公众号的历史文章：https://mp.weixin.qq.com/s/xxx
```

代理历史模式需要你在微信桌面客户端内置浏览器里打开旧版历史入口并滚动，只能拿到实际加载出来的文章。

### 3. 保存这篇：补充页面数据

用于已经打开的单篇文章，补充普通批量下载拿不到的信息。

开启代理增强后，在微信文章页点击：

```text
保存这篇
```

保存后可以补充：

- 页面已加载评论
- 阅读数、点赞数、评论数、在看等页面可见数据
- 正文 DOM、互动区域 DOM
- 图片 URL
- 页面排版和风格信息

不保证拿到：

- 全量评论
- 收藏数
- 页面没有显示的数据

连续保存多篇文章后，可以说：

```text
已保存，帮我整理
```

Skill 会处理所有未整理的保存记录：已下载过的文章会补到对应目录，没下载过的文章会从保存记录生成 Markdown。
评论会先结构化成昵称、地区/时间、点赞、回复和内容，再写进文章 Markdown 的 `页面数据` 区块；原始 JSON 产物仍会保留。

## 输出位置

默认保存到：

```text
~/Downloads/wechat-articles/<公众号名>/
```

主要文件：

- `articles/*.md`
- `images/*`
- `index.csv`
- `snapshots/*`：通过“保存这篇”保留的原始评论、结构化评论、互动数据和页面风格 JSON

## 登录和代理

- **已知文章 URL**：不需要登录。
- **获取历史文章**：通常需要 Exporter 扫码登录。
- **评论、阅读数、点赞数**：需要代理增强。
- **首次使用代理增强**：需要按提示安装并信任 mitmproxy 证书。
- **取消代理增强**：恢复系统代理，不删除已保存文章；如果要完全停止服务，明确说“停止代理增强服务”。

## 安全边界

- 不绕过登录、付费墙、私密内容
- 不打印 cookie、token、auth-key、pass_ticket
- 代理增强只保存脱敏后的页面信息
- 页面没有暴露的数据标记为 `missing`，不猜

## 更多文档

- [`SKILL.md`](SKILL.md)：Skill 执行规则
- [`references/output-formats.md`](references/output-formats.md)：输出结构
- [`references/troubleshooting.md`](references/troubleshooting.md)：排障
- [`references/compliance.md`](references/compliance.md)：安全与权限
