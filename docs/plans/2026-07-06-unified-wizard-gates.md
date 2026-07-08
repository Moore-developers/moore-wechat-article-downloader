# 微信公众号文章下载 Skill 统一向导计划

## 目标

把 `moore-wechat-article-downloader` 收敛成一个 Skill 入口，让用户只用自然语言触发：

- 下载一篇公众号文章 URL。
- 下载多篇公众号文章 URL。
- 输入公众号名称，列出最近 N 篇文章，让用户选择下载。
- 输入公众号名称，直接下载最近 N 篇文章。
- 输入任意一篇公众号文章 URL，进入历史文章发现流程。

默认入口只保留一个：

```bash
python3 scripts/wechat_wizard.py run "<用户目标>"
```

底层命令继续存在，但作为内部 adapter：

- `scripts/wechat_downloader.py`：单篇、多篇 URL 下载，Markdown 输出。
- `scripts/wechat_history.py` 或现有 history 命令：微信内置浏览器历史列表抓取。
- `scripts/wechat_exporter.py`：Exporter 模式，登录、搜索公众号、同步文章、下载文章。
- `scripts/wechat_wizard.py`：唯一编排层，负责路由、门禁、状态、恢复、验收。

## 设计边界

- **不是 dashboard**：用户通过 Skill 对话使用，不需要 Web 管理界面。
- **不是 SaaS**：所有数据默认本地保存。
- **不是改写工具**：下载后可供二次处理，但本 Skill 不做改写、总结、运营生成。
- **不绕权限**：不处理私密、付费、删除、无权限内容。
- **不靠对话记忆**：任务、候选项、登录会话、下载状态都落到文件或 SQLite。

## 工程契约

这份计划只保留可执行约束，不写角色说明。判断一项能力是否完成，只看本地状态和命令证据。

- **事实源**：任务、候选项、选择、登录状态、同步状态、下载结果写入 SQLite；每次下载写 `run.json`。
- **风险源**：代理、凭据、覆盖、删除、全量重抓必须经过 gate；R3/R4 动作必须有确认来源。
- **完成源**：以测试、SQLite 查询、输出文件、manifest、secret scan 为准；没有证据就按未完成处理。
- **隔离源**：触碰路由、登录、代理、SQLite、输出格式、并发锁、脱敏时，必须有 test/review 子智能体验证。
- **调度边界**：`SKILL.md` 只负责识别用户意图和调用向导；状态机、门禁、重试、输出全部下沉到脚本。

## 改造口径

- **先收敛入口**：用户只需要自然语言；内部统一落到 `wechat_wizard.py run`。
- **先固定契约**：先定 intent、gate、SQLite、manifest，再扩单篇、多篇、Exporter、History。
- **先做失败路径**：登录过期、代理未确认、输出目录锁、重复下载、部分失败都要可恢复。
- **先保守默认**：代理、凭据、并发、覆盖写入默认保守；优化只能通过配置打开。
- **先可复测**：每个交付包必须有 fixture 或 smoke；不靠“我手动试过”当结论。

## 执行架构

`wechat_wizard.py` 是唯一对话入口，但不要变成大泥球。内部拆成小模块或清晰函数边界：

- **IntentRouter**：把自然语言解析成 intent，不碰网络。
- **GateRunner**：按 mode 执行门禁，失败即返回可恢复状态。
- **StateStore**：SQLite 读写、migration、lock、task、choice、run。
- **Adapters**：包装 `wechat_downloader.py`、`wechat_exporter.py`、history 抓取，不让 adapter 直接决定用户流程。
- **OutputWriter**：生成 Markdown、图片目录、`index.csv`、`articles.json`、`errors.json`、`run.json`。
- **RiskGuard**：脱敏、代理确认、凭据存储、输出目录锁、并发上限。
- **Doctor**：只做诊断和修复建议；自动修复只允许处理 stale lock 这类低风险本地状态。

推荐边界：

```text
user prompt
  -> IntentRouter
  -> GateRunner
  -> StateStore
  -> Adapter
  -> OutputWriter
  -> Verify
```

任何 adapter 失败都不能只抛异常结束，必须转成 gate event 或 download item error。

## 执行包契约

后续实现按小包推进。每个包必须同时交付代码、状态、证据，避免大而散。

每个执行包记录：

- **Scope**：本包只改哪些能力，不顺手重构无关模块。
- **Files**：预计触碰文件；不包含 `skills/moore-dev-goal/`。
- **State impact**：新增或变更的 SQLite 表、manifest 字段、配置项。
- **Risk level**：最高风险等级，是否需要用户确认。
- **Evidence**：测试命令、fixture、SQLite 查询、输出文件。
- **Rollback**：失败后如何恢复，是否影响既有下载结果。

执行顺序：

1. 写或补契约测试。
2. 实现最小代码。
3. 跑本包测试和全局轻量验证。
4. 用 review 子智能体查风险边界。
5. 主智能体复跑最终命令。
6. 小提交保存。

## 用户级流程

### 流程 A：单篇 / 多篇 URL 下载

输入：

```text
下载这些公众号文章：
https://mp.weixin.qq.com/s/xxx
https://mp.weixin.qq.com/s/yyy
```

执行：

1. `wechat_wizard.py run` 解析 URL。
2. 判定为 `url` 模式。
3. 去重、脱敏、检查输出目录。
4. 调用 URL downloader。
5. 验证 Markdown、图片、`index.csv`、`run.json`。

输出结构：

```text
~/Downloads/wechat-articles/<run-id>/
  articles/001.md
  articles/002.md
  images/001/
  images/002/
  index.csv
  articles.json
  errors.json
  run.json
```

### 流程 B：Exporter 模式列出公众号文章

输入：

```text
用 exporter 模式同步「哥飞」，列出最近 50 篇让我选
```

执行：

1. 判断为 `exporter` 模式。
2. 检查本地 SQLite 是否已有该公众号。
3. 如果今天已同步，直接列出本地文章。
4. 如果未同步或缓存过期，检查登录。
5. 未登录则自动生成二维码登录会话。
6. 登录完成后恢复同步。
7. 返回可读文章列表，并把完整列表写入 JSON/CSV。

### 流程 C：Exporter 模式直接下载

输入：

```text
下载「哥飞」最近 20 篇
```

执行：

1. 本地账号匹配。
2. 缓存新鲜度检查。
3. 自动同步缺失数据。
4. 自动选择最近 20 篇。
5. 调用下载器。
6. 验证输出。

### 流程 D：文章 URL 发现历史文章

输入：

```text
历史文章列表下载：https://mp.weixin.qq.com/s/xxx
```

执行：

1. 判断为 `history` 模式，而不是普通 URL 下载。
2. 生成旧版公众号历史入口 URL。
3. 启动本地抓取会话。
4. 如果需要代理，停在 `need_proxy_confirm`。
5. 用户确认后才允许改系统代理。
6. 捕获历史接口数据后，落库并列出文章。

注意：

- history 模式默认走旧版公众号历史入口，因为这是 `qiye45/wechatDownload` 类流程的核心。
- 系统代理不是小事，默认不自动改。
- 如果未来支持 per-app 代理，只作为可选增强，不作为 P0 依赖。

## 意图模型

`wechat_wizard.py` 必须把用户输入解析成结构化 intent：

```json
{
  "mode": "url|history|exporter|ambiguous",
  "action": "download|list|sync|select|resume",
  "urls": [],
  "account_query": "",
  "latest": null,
  "selection": "",
  "output_dir": "",
  "requires_user_choice": false
}
```

### 路由规则

- **URL 下载**：有文章 URL，且没有“历史、列表、最近、全部、同步”等词。
- **多 URL 下载**：同一输入里有多个文章 URL，或传入 URL 文件。
- **History 模式**：文章 URL + 历史/列表/最近/全部/主页等意图。
- **Exporter 模式**：公众号名称、搜索、同步、列出、最近 N 篇、二维码登录等意图。
- **Ambiguous**：同时满足多个模式且置信度不足，返回 `need_mode_choice`。

### Mode Decision Gate

输入解析后必须先过 `Gate 0.5`：

```json
{
  "gate": "mode_decision",
  "ok": true,
  "mode": "exporter",
  "mode_confidence": 0.92,
  "why": "用户输入公众号名称并要求列出最近文章",
  "why_not_other_modes": {
    "url": "没有文章 URL",
    "history": "没有要求通过文章 URL 发现历史入口"
  }
}
```

如果低置信度，不继续执行，返回候选模式和原因。

## 门禁契约

所有 gate 都返回统一结构：

```json
{
  "ok": true,
  "gate": "input|mode_decision|environment|auth|proxy|account|sync|choice|download|verify",
  "state": "ready|need_mode_choice|need_login|need_proxy_confirm|need_account_choice|need_article_choice|blocked|done",
  "recoverable": true,
  "next_action": "",
  "evidence": {}
}
```

### Gate 0：输入

- 提取 `mp.weixin.qq.com` URL。
- 拒绝非微信文章 URL。
- URL 脱敏后再写 stdout、SQLite、manifest。
- 按 canonical URL 去重。
- 至少需要 URL、公众号名称、选择项、resume id 之一。

### Gate 0.5：模式判定

- 生成 `mode`、置信度、原因。
- 不允许靠调用方猜模式。
- 低置信度返回 `need_mode_choice`。

### Gate 1：环境

- SQLite 可打开、可写。
- 输出目录可创建、可写。
- Keychain 可用性已知。
- Exporter base URL 已配置或能给出明确缺失项。
- History 模式检查 `mitmdump` 是否存在。

### Gate 2：认证

- URL 模式不需要认证。
- Exporter 远程搜索/同步需要登录。
- 未登录时自动创建二维码登录会话，不让用户手动拼命令。
- QR 文件必须有真实 `.jpg` / `.png` 后缀。
- CLI 不长期阻塞，默认 120 秒轮询窗口。
- 过期后标记 `expired`，同一任务可重新生成二维码。

### Gate 3：代理

只服务 history 模式：

- 改系统代理前必须得到用户确认。
- 修改前保存原始代理设置。
- 启动失败、下载失败、超时、进程退出时都尝试恢复。
- 代理端口、上游代理、恢复状态写入 SQLite gate event。

### Gate 4：公众号选择

- 本地精确匹配：继续。
- 远程强匹配：添加并继续。
- 多候选：返回候选列表，状态为 `need_account_choice`。
- 候选列表写入 SQLite，用户后续回复编号或名称都能恢复。

### Gate 5：同步新鲜度

- 如果今天没有同步过，自动同步。
- 如果用户明确说“同步/刷新”，即使今天同步过也强制同步。
- 缺少目标日期范围时，只补缺失窗口，不重复全量。
- 分页不能依赖 `total` 字段。
- 停止条件：空页、重复页、达到 limit、连续错误超过阈值。

### Gate 6：文章选择

- “最近 N 篇”：自动选择。
- “列出/让我选”：返回标题、时间、链接摘要，同时写入 JSON/CSV。
- 支持选择方式：序号、范围、逗号、文章 id、标题关键词、日期。
- 必须校验文章属于当前公众号。

### Gate 7：下载

下载前生成计划：

- selected article count
- already downloaded count
- output dir
- image policy
- concurrency

默认策略：

- 已下载文章默认跳过。
- HTML 并发低，图片并发可略高。
- 每篇文章单独记录成功、失败、跳过。

### Gate 8：验收

必须验证：

- `run.json` 存在。
- `index.csv` 存在。
- `download_items` 中成功项对应的 Markdown 路径真实存在。
- 跳过项不要求本次重新生成文件，但必须能指向历史成功记录或已有目标文件。
- `articles.json` 可解析。
- 图片失败写入 `errors.json`，默认不让整批失败。

### Gate 实现要求

- 每个 gate 只能做本 gate 的判断，不能偷偷执行下游动作。
- gate 失败必须写入 `wizard_gate_events`，并返回 `state`、`next_action`、`evidence`。
- 同一个 task 的 gate 顺序必须可追踪，不能只保留最后状态。
- adapter 抛出的异常必须转换成结构化错误，错误内容先脱敏再落库。
- `verify` 失败时保留输出目录和 SQLite 记录，返回 `failed_recoverable`。

## 风险分级

程序门禁按风险等级执行，不能靠提示词“提醒自己”：

| 等级 | 场景 | 默认行为 | 必要证据 |
| --- | --- | --- | --- |
| R0 | URL 解析、列表展示、读取本地 SQLite | 可自动执行 | task、intent、gate event |
| R1 | 创建输出目录、写 manifest、写 SQLite | 可自动执行，但必须可恢复 | output dir、run id、写入结果 |
| R2 | 登录二维码、远程同步、下载文章 | 可自动执行，但不能泄露凭据 | login id、profile id、脱敏日志 |
| R3 | 修改系统代理、启动 mitmdump、恢复代理 | 必须用户确认 | 原代理快照、确认记录、恢复结果 |
| R4 | 覆盖已有文件、删除数据、清空库、全量重抓 | 默认拒绝或需要显式 `--force` | force 来源、影响范围、回滚建议 |

硬规则：

- **失败默认停住**：门禁失败时返回 `state` 和 `next_action`，不偷偷降级到另一个模式。
- **危险动作可审计**：R3/R4 必须写入 `wizard_gate_events`。
- **代理一定有恢复计划**：启动前保存快照，退出时恢复，恢复失败要进入 `blocked`。
- **凭据不进普通日志**：stdout、SQLite、manifest、异常字符串都走同一个 scrubber。

## SQLite 状态模型

SQLite 是可恢复任务的事实源。能写进 SQLite 的状态，不靠对话记忆。

必需表：

- `wizard_tasks`：task id、intent json、mode、state、created/updated。
- `wizard_gate_events`：task id、gate、state、evidence json、error。
- `wizard_choices`：候选账号、候选文章、用户选择。
- `exporter_profiles`：登录 profile 元信息，不存明文敏感值。
- `target_accounts`：公众号信息。
- `articles`：文章元数据。
- `download_runs`：run id、task id、mode、output dir、统计。
- `download_items`：article id/url、status、path、error、retry count。
- `wizard_locks`：lock key、owner task、pid、created/updated、stale timeout。
- `user_preferences`：输出目录、图片策略、并发、覆盖策略。

敏感凭据默认进 macOS Keychain，不进 SQLite。

状态写入规则：

- task 创建后才能执行 adapter。
- 每个 gate 至少写一次 event。
- 长任务开始、完成、失败都写 `download_runs` 或 `download_items`。
- lock 必须原子获取；不能先查再写。
- retry 必须新建 run，同时保留原失败 item 的 retries 计数。

## Migration 策略

- 用 `PRAGMA user_version` 管理 schema 版本。
- migration 必须幂等。
- 只做 additive migration，正常升级不 drop、不重写用户表。
- migration 失败返回结构化 gate failure。
- 初始化时尝试开启 WAL；失败只 warning，不阻塞。
- 测试必须覆盖：新库初始化、旧库升级、重复初始化。

## 输出契约

默认输出目录：

```text
~/Downloads/wechat-articles/<run-id>/
```

文件结构：

```text
articles/001.md
articles/002.md
images/001/
images/002/
index.csv
articles.json
errors.json
run.json
```

`index.csv` 字段：

```text
seq,article_id,title,account,publish_time,url,markdown_path,image_dir,status,error
```

`run.json` 字段：

- `run_id`
- `task_id`
- `source_mode`
- `input_digest`
- `output_dir`
- `started_at`
- `finished_at`
- `success_count`
- `failed_count`
- `skipped_count`
- `duration_ms`
- `html_fetch_count`
- `image_fetch_count`
- `retry_count`
- `gate_summary`

## 性能策略

- URL 解析、去重、脱敏先于网络请求。
- P0 默认串行或低并发，先保证状态一致。
- P3 起支持可配置并发：HTML 默认 2，图片默认 6，上限写死保护。
- 同 host 限速，降低微信风控概率。
- SQLite 批量写入 upsert 和下载状态。
- 列表命令返回紧凑视图，完整数据写 JSON/CSV。
- 不为 P0 引入 Playwright、httpx、asyncio 或新运行时依赖；需要并发时优先用标准库或现有 downloader 能力。
- 每次 run 记录 `duration_ms`、请求数、成功/失败/跳过数，后续优化只看这些指标，不凭感觉。

## 安全策略

- 脱敏边界：stdout、SQLite、manifest、错误日志。
- 不打印完整 cookie、token、auth-key、pass_ticket、uin、key。
- 改系统代理前必须确认。
- 代理恢复失败必须明确提示，不假装成功。
- 所有生成数据默认本地保存。
- 拒绝无权限内容。

## 并发策略

- 每个公众号一个 sync lock。
- 每个输出目录一个 download lock。
- history 代理会话按端口互斥。
- lock 写入 SQLite，并带 stale timeout。
- `doctor` 可检查进程存活后清理 stale lock。
- lock 获取必须是原子事务。
- 下载锁覆盖从 adapter 调用到 manifest 验收的完整周期，不能下载完就提前释放。
- 同一 URL 在同一 task 内只出现一次；跨 task 命中已有成功记录时默认跳过。

## 恢复策略

每个可恢复状态都必须返回：

- `task_id`
- `state`
- `next_action`
- `evidence`

典型状态：

- `need_login`：二维码文件、login id、过期时间。
- `need_proxy_confirm`：代理修改计划、原始代理快照状态。
- `need_account_choice`：候选公众号列表已落库。
- `need_article_choice`：候选文章列表已落库。
- `failed_recoverable`：失败项、重试入口。

## Doctor 命令

```bash
python3 scripts/wechat_wizard.py doctor
```

检查项：

- SQLite 可打开、可写。
- Keychain 可读写。
- Exporter 登录是否存在、是否有效。
- `mitmdump` 是否安装。
- 当前系统代理是否干净或可恢复。
- 默认输出目录是否可写。
- 是否有 stuck lock。
- 最近一次 run 的 manifest 是否完整。

## 子智能体验证契约

触碰以下边界时，必须启用 test/review 子智能体：

- intent router
- QR 登录
- 系统代理
- SQLite migration
- 下载输出格式
- 并发锁
- 敏感信息脱敏

分工：

- **test 子智能体**：运行测试命令、smoke command、fixture。
- **review 子智能体**：检查状态机、恢复路径、安全边界、误匹配风险。

主智能体负责整合结论，并在本地再跑最终验证命令。

子智能体输出必须落到结论，不接受泛泛评价：

- **test**：列出实际运行命令、退出码、失败日志摘要、未覆盖项。
- **review**：只报 P0/P1/P2 风险；每条风险要指向文件、函数或状态转移。
- **主智能体**：决定修复、接受风险或降级范围，并复跑关键命令。

## 证据矩阵

每个能力完成时，都要能拿出对应证据：

| 能力 | 文件证据 | 命令证据 |
| --- | --- | --- |
| 单篇 URL 下载 | `run.json`、`index.csv`、`articles/001.md` | `test_wizard_contract.py` |
| 多篇 URL 下载 | 多条 `download_items`、去重后的 `articles.json` | URL fixture 测试 |
| Exporter 列表 | `wizard_choices`、articles JSON/CSV | `test_exporter_contract.py` |
| Exporter 下载 | `download_runs`、成功/跳过 item | wizard + exporter contract |
| 二维码登录 | QR 图片、login task、profile 状态 | QR fixture 或 smoke |
| History 入口 | legacy home URL、`need_proxy_confirm` event | history preflight fixture |
| 代理恢复 | 原代理快照、恢复结果 event | proxy restore fixture |
| 重试失败项 | 新 run、原 item retries 增加 | retry fixture |
| 脱敏 | scrubbed stdout/SQLite/manifest | secret scan 命令 |

没有文件证据的能力，视为没完成。这个判断有点冷酷，但很省命。

## Eval 覆盖

至少覆盖：

- 单 URL 下载。
- 多 URL 下载和去重。
- 敏感 URL 参数脱敏。
- 公众号名称 -> 本地缓存命中 -> 列表。
- 公众号名称 -> 缓存过期 -> 自动同步。
- 未登录 -> 自动 QR flow。
- 多公众号候选 -> `need_account_choice`。
- 最近 N 篇自动选择。
- 用户选择文章后恢复下载。
- Exporter 分页无 `total`。
- History 模式代理 preflight。
- 图片失败仍生成 Markdown。
- 文章 id 跨公众号校验失败。
- 输出 manifest 校验。
- 离线 HTML fixture 下的单篇真实解析。
- 离线 HTML fixture 下的多篇真实解析和 `download_items` 写入。

## 实施里程碑

### P0a：URL Wizard

交付：

- `wechat_wizard.py run` 支持单篇、多篇 URL。
- 先创建 task，再执行下载。
- 写入 `wizard_tasks` 和 `wizard_gate_events`。
- 复用现有 URL downloader。
- 输出 `articles.json`、`errors.json`、`run.json`。
- 验收失败时返回 `failed_recoverable`，保留已有输出。

验收命令：

```bash
python3 skills/moore-wechat-article-downloader/evals/test_wizard_contract.py
python3 -m ruff check skills/moore-wechat-article-downloader/scripts/wechat_wizard.py
```

### P0b：Exporter Wizard

交付：

- 自然语言公众号查询路由到 exporter。
- 今天已同步时不强制远程同步。
- 缓存过期时自动同步。
- 列表模式返回可读列表，并写 JSON/CSV。
- 下载模式自动选择最近 N 篇。
- 远程同步前必须检查登录；未登录返回 QR flow。

验收命令：

```bash
python3 skills/moore-wechat-article-downloader/evals/test_wizard_contract.py
python3 skills/moore-wechat-article-downloader/evals/test_exporter_contract.py
```

### P0c：History Wizard Boundary

交付：

- URL + 历史意图路由到 history。
- 生成旧版公众号历史入口。
- 没有确认时停在 `need_proxy_confirm`。
- 不自动改系统代理。
- 明确提示用户用微信内置浏览器打开 legacy 入口。

验收命令：

```bash
python3 skills/moore-wechat-article-downloader/evals/test_wizard_contract.py
```

### P1：登录闭环和选择恢复

交付：

- 未登录自动生成二维码。
- 轮询登录状态。
- 登录成功后恢复原任务。
- 账号/文章候选项支持后续回复恢复。
- resume 命令支持 `task_id`、最近未完成任务、用户选择文本。

验收：

- fixture 测试覆盖 QR session。
- 本地 smoke 流程能从 `need_login` 恢复到列表。
- 登录过期后能重新生成二维码，而不是让用户重跑全流程。

### P2：程序门禁和 Doctor

交付：

- 所有 gate 统一结构化返回。
- `doctor` 检查环境、登录、代理、输出目录、stale lock。
- SQLite migration 使用 `PRAGMA user_version`。
- R3/R4 风险动作必须有确认来源和 gate event。
- stale lock 清理后重新查询并展示剩余 lock。

验收：

- 初始化新库、升级旧库、重复初始化均通过。
- `doctor` 输出不泄露敏感字段。
- secret scan 对 staged diff 通过。

### P3：下载任务引擎

交付：

- `download_runs`、`download_items`。
- 跳过已下载。
- 支持 force。
- 并发和重试可配置，但必须有上限。
- 失败项可恢复。
- 输出目录 lock 覆盖下载、manifest、verify、SQLite 保存全过程。

验收：

- 重跑同一任务不会重复下载。
- 部分失败后可重试失败项。
- 复用旧输出目录时，验收只检查本 run 期望的成功 Markdown，不被目录里历史文件干扰。

### P4：硬化和性能

交付：

- stale lock 清理。
- compact output。
- 性能指标写入 `run.json`。
- 安全脱敏测试。
- 历史模式代理恢复测试。
- 并发上限、重试次数、图片策略进入 `user_preferences`。
- 失败统计可读：stdout 简洁，完整错误在 `errors.json`。

验收：

- 全量 eval 通过。
- `git diff --check` 通过。
- review 子智能体无 P0/P1 问题。
- smoke 输出里只展示摘要、输出目录、下一步，不刷屏。

## 最终验收命令

每次合并前至少跑：

```bash
python3 -m py_compile \
  skills/moore-wechat-article-downloader/scripts/wechat_wizard.py \
  skills/moore-wechat-article-downloader/scripts/wechat_downloader.py \
  skills/moore-wechat-article-downloader/scripts/wechat_exporter.py \
  skills/moore-wechat-article-downloader/evals/test_wizard_contract.py \
  skills/moore-wechat-article-downloader/evals/test_exporter_contract.py

python3 skills/moore-wechat-article-downloader/evals/test_wizard_contract.py
python3 skills/moore-wechat-article-downloader/evals/test_exporter_contract.py
python3 -m ruff check \
  skills/moore-wechat-article-downloader/scripts/wechat_wizard.py \
  skills/moore-wechat-article-downloader/scripts/wechat_downloader.py \
  skills/moore-wechat-article-downloader/scripts/wechat_exporter.py \
  skills/moore-wechat-article-downloader/evals/test_wizard_contract.py \
  skills/moore-wechat-article-downloader/evals/test_exporter_contract.py
python3 -m json.tool skills/moore-wechat-article-downloader/evals/evals.json >/dev/null
git diff --check -- skills/moore-wechat-article-downloader
```

提交前 secret scan：

```bash
git diff --staged | rg '^\+' | rg -i "(auth-key|pass_ticket|password|secret|token|cookie|api_key|sessionid|uin)"
```

期望结果：没有输出。若有输出，先确认是否测试假数据；真实凭据一律不能提交。

## 完成标准

这套 Skill 达到可用，不看“说得像不像”，只看这些事实：

- 用户一句话能下载单篇文章。
- 用户粘贴多篇 URL 能批量下载。
- 用户说公众号名能列出最近文章。
- 用户能从列表选择文章并下载。
- 未登录时能自动进入二维码登录。
- History 模式不会未经确认修改系统代理。
- 输出目录只保留文章、图片、索引、manifest。
- stdout、SQLite、manifest 没有敏感凭据。
- 重跑任务默认跳过已下载内容。
- 测试覆盖主要失败和恢复路径。

## 非目标

- 不做 dashboard。
- 不做云端服务。
- 不做内容改写。
- 不抓无权限内容。
- 不把系统代理修改做成默认隐式动作。
