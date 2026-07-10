# 微信收藏实施任务拆解

## 目标

将“订阅同步”和“微信收藏”落到同一本地资料库。Exporter 负责文章发现和范围，公开 URL 下载负责正文，微信收藏在用户打开文章后提供当前页收藏和同公众号的互动补充。

“下载某公众号最新 20 篇”和“准备最新 20 篇给外部机制拆解”仅是验收案例，不是产品的强制任务编排。

## 已验证的现状

- 现有增强代理可注入“收藏到本地”、保存 DOM 快照、被动保存用户页面实际请求到的精选评论。
- 现有 Exporter 有文章 `msgid`、`idx`、基础指标字段和评论导入表。
- 现有系统代理会话有随机端口、固定 upstream、安全重启、WebView 重置和恢复逻辑。
- 当前没有 raw credential broker、主动互动请求、`comment_id` 解析、`__biz -> Exporter account` 映射、启动守护或跨进程事件协议。

因此，不能仅将现有“代理增强”重命名为“微信收藏”后就宣称支持批量互动同步。

## 不可变约束

- 正文下载不等待短时 credential；互动任务可处于 `waiting_credential` 并稍后恢复。
- raw credential 只存在于本地 broker 进程内存；SQLite、日志、Markdown、快照、错误对象、缓存和崩溃产物均不得保存。
- 评论必须标记 `scope=elected`；回复必须记录是否完整；不得承诺全量评论。
- `__biz` 未能唯一关联本地账号、文章缺 `comment_id` 或 credential 过期时，不得请求互动接口。
- 页面收藏与互动 worker 不能并发替换同一篇 Markdown 的页面数据区块；必须使用单一 writer 或每篇文章锁。
- 常驻系统路由只能在守护、健康检查、事务恢复和真实 macOS 演练通过后成为默认。
- 旧历史代理仅是人工、被动的兼容/诊断兜底；主动历史同步不属于本期既有能力。
- 外部 Wiki/分析机制只消费本地数据集，本 Skill 不实现拆解或选题生成。

## 交付顺序

```text
S0 契约、迁移、文章上下文
 ├─> S1 手动会话 credential broker + 正文/互动闭环
 │     └─> S2 本地资料库单写入和数据集清单
 └─> S3 代理路由事务 -> launchd/watchdog -> 常驻默认
                                │
                                └─> S4 文档迁移和完整验收
```

## S0：契约、迁移和文章上下文

### T0：版本化 schema migration 与无敏感任务 contract

- 为 `exporter.sqlite` 建立显式递增 migration；不能依赖 `CREATE TABLE IF NOT EXISTS` 更新旧库。
- 定义无敏感任务状态：`content_downloading`、`waiting_credential`、`engagement_syncing`、`completed_with_gaps`。
- 定义无敏感事件 contract：任务 ID、账号 ID、`__biz` 状态、到期时间、运行状态和错误码。
- 增加幂等数据结构：互动运行记录、指标历史、评论来源/范围、回复表和每篇文章页面数据写锁。

**完成标准**：重复 migration 安全；旧数据库可升级；没有任何 contract 含 credential 字段。

### T0a：文章上下文与账号关联

- 新建 `article_context`，最小字段：`article_id`、`account_id`、`fakeid`、`__biz`、`msgid`、`idx`、`comment_id`、规范化 URL、来源、解析时间、状态。
- 从已下载 HTML 解析 `comment_id`，并校验/补齐 `msgid`、`idx`。
- 定义 `__biz -> account_id` 的唯一映射和冲突处理：同名账号、URL 归属改变、映射缺失或冲突时全部停止主动互动请求。

**完成标准**：评论 worker 只接收已验证 context；缺字段只产生可读失败原因。

## S1：先验证手动会话闭环

### T1：本地 credential broker protocol

- 在 mitm addon 所在进程实现 broker；只受 loopback Unix socket 或受限本地 HTTP 访问。
- 使用随机 capability token；对外只提供 `get_status` 与 `run_engagement(account_id, run_id)`，worker 永不读取或打印 raw credential。
- 捕获来自请求 query/header/cookie 和响应 `Set-Cookie` 的最小必需字段，仅在进程内按 `__biz` 保存。
- broker 重启即使所有 credential 失效；等待任务回退为 `waiting_credential`，不得从 SQLite 恢复。

**完成标准**：打开 A 公众号文章不能授权 B 公众号任务；过期、重启和 capability 错误均可测试。

### T2：正文与互动任务编排

- Exporter 同步并回显目标标题列表；用户已明确 N 时，正文立即下载，不再增加选择 gate。
- 同时创建互动子任务；无 credential 时保留 `waiting_credential`，复制范围内一篇代表 URL，提示用户粘贴到微信文件传输助手并打开。
- 仅在 T1 端到端验证通过并受 feature flag 控制时，broker 事件自动恢复匹配 `__biz` 的互动任务。
- 正文下载失败、credential 缺失和互动失败必须独立计数和重试。

**完成标准**：credential 到达不重跑正文；跨账号事件不能恢复错误任务。

### T3：主动互动同步 worker

- broker 代表 worker 执行请求；worker 只接收结构化结果。
- 最大并发 2；支持指标解析、精选评论分页、有限回复、超时、重试、部分成功和 credential 到期中止。
- 每次结果写入幂等 upsert：指标历史、`scope=elected` 的评论、回复完整性和数据来源。
- `favorite_count` 不作为 worker 承诺字段，除非先建立可验证来源。

**完成标准**：在手动会话下验证“打开一篇文章 -> 同公众号 N 篇文章获取正文、可用指标和精选评论”闭环；不宣称常驻。

## S2：本地资料库收敛

### T4：单一文章写入和数据集清单

- 抽出 `article identity resolver`，供 URL 下载、Exporter、快照 attach 和互动 worker 共同使用。
- 抽出唯一 Markdown 页面数据 writer；同一文章锁定后再替换区块，避免快照与互动 worker 互相覆盖。
- 写出 `<account-dir>/datasets/<dataset-id>/manifest.json` 与 `manifest.csv`，仅包含本地路径、文章元数据、正文状态、互动路径、来源和版本。
- 正文完成即可发布 manifest 初版；互动补齐后递增版本或更新条目。

**完成标准**：同一文章多来源不会重复归档；外部机制只靠 manifest 即可读取本地数据。

## S3：安全常驻服务

### T5a：系统代理事务与恢复闸门

- 为多条 `networksetup` 修改建立阶段 journal、启动前 health check 和可恢复状态。
- 只在系统当前仍指向本服务 `host:port` 时恢复；网络服务切换、VPN/公司代理变更后不得盲目覆盖用户新配置。
- 启动/停止任一步失败时，输出明确的人工恢复路径；不假设系统代理修改是原子操作。

**完成标准**：端口被占、部分 `networksetup` 失败、上游变化和网络服务切换均有定向测试。

### T5b：launchd 与 watchdog

- 在 T5a 通过后，再增加 launchd `KeepAlive`、独立 watchdog、心跳、连续失败阈值和 pause/resume/disable 命令。
- 开机或 broker 重启后只恢复服务，不恢复 credential；是否恢复系统路由取决于服务健康和保存路由是否仍适用。
- 真实 macOS 演练覆盖进程崩溃、守护重启、端口抢占、网络服务切换和人工暂停。

**完成标准**：通过全部真实演练后，微信收藏才可成为默认常驻路由。

## S4：文档迁移和集成验收

### T6：命令、Skill 与 README

- 用户语言统一为“订阅同步”“微信收藏”“链接导入”。
- 旧 `proxy-enhancer-*`、`history-capture-*` 仅保留兼容/诊断说明。
- 只有 T1/T3/T5 的实际验证通过后，才写“打开文章自动获得短时数据权限”或“默认常驻”。
- 明确：打开文章用于短时权限，点击“收藏到本地”用于保存当前页高保真快照；精选评论不等于全部评论。

### T7：回归和真实验证

- contract/fixture：无 credential、有效/过期 credential、broker 重启、`__biz` 映射冲突、`comment_id` 缺失、部分互动失败和 Markdown 写锁。
- 安全 fixture：请求 URL、请求/响应 headers、HTML 内嵌 JSON、异常 repr、debug JSONL、SQLite WAL/SHM 和崩溃产物均不含 raw credential。
- 端到端：下载某公众号最新 20 篇、准备 20 篇本地数据集、当前页收藏、手动会话互动同步、代理事务故障演练。
- 旧 history-capture 仅在 Exporter 不可用时验证其人工、被动兜底流程。

## 并行安排

| 阶段 | 可并行 | 不可并行 |
| --- | --- | --- |
| S0 | T0 与 T0a 的 schema/fixture 设计可分工 | migration contract 合并前不得启动后续实现 |
| S1 | T2 的正文调度可与 T1 broker 协议开发并行 | T2 自动恢复、T3 worker均依赖 T1；T1/T5 不可同时改代理状态 |
| S2 | manifest 输出可与 T3 后半段并行 | 共享 Markdown writer 与互动写入必须统一所有权 |
| S3 | T5a 的纯模拟测试可与资料库工作并行 | 系统代理代码合并和真实手测必须单人串行；T5a 先于 T5b |
| S4 | 文档草稿可并行 | 用户承诺和主 README 必须等待真实验证结果冻结 |

## 子代理分工

- **验证代理**：已完成，只读核验现有代理、SQLite、文章上下文和常驻前置能力。
- **Review 代理**：已完成，复核依赖、IPC、迁移、写入竞争和系统代理安全。
- **实施代理**：从 S0 起按文件边界领取任务；代理 addon、`wechat_downloader.py` 会话状态和真实网络验证始终由单一负责人串行执行。
