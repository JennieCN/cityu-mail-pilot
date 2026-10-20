# Open-source review

检索日期：2026-09-13（第二轮针对邮件排版与每日简报补充检索）。所有第三方仓库都先按许可证、维护状态、安全边界和与需求的贴合度进行筛选；没有执行任何第三方仓库脚本，仓库文本一律按不可信输入处理。

本轮检索的完整技术简报（含许可证核实表、未核实事项专章与可粘贴骨架）保存在 `../docs/email-html-compatibility-2026-09-13.md`。

## 本轮（视觉与报告重构）新增的开源检索

### 邮件 HTML 兼容性

- [resend/react-email](https://github.com/resend/react-email) — MIT，活跃。其仓库内的渲染规则被当作**校验清单**使用：不要 flex/grid、媒体查询不可靠、`rem` 要换成 px、样式必须内联(`<style>` 会被部分客户端剥离)、单封邮件控制在 102 KB 以内(Gmail 会截断)。**未引入** React/JSX 运行时。
  需要更正两点常见误传：react-email **并不禁止**远程字体（它提供 `<Font>` 组件并要求配 `fallbackFontFamily`）；其仓库文档里**没有**关于 `<details>` 的表述。
- [leemunroe/responsive-html-email-template](https://github.com/leemunroe/responsive-html-email-template) — MIT（注意许可证文件名是小写 `license.txt`），13.7k★，最后提交 2024-08。采用其**用 `&nbsp;` 空单元格居中 + `width` 属性与 CSS 双写 + 隐藏 preheader**的单列骨架思路；未复制其 `border-radius`/`:hover` 等增强写法。
- [hteumeuleu/caniemail](https://github.com/hteumeuleu/caniemail) — MIT，活跃（数据更新至 2026-08），本轮**主要事实依据**。据此确认：Outlook(Word 渲染器) 不支持 `border-radius`、`@media`、flex、grid、`position`、`background-image`；Gmail App 用**非 Google 账号**收取时 `<head>` 里的 `<style>` 整体失效 —— 这是"关键样式必须 100% 内联"的硬性理由。
- [hteumeuleu/email-bugs](https://github.com/hteumeuleu/email-bugs) — **无 LICENSE**，且仓库内只有 README 与 issue 模板、没有代码，因此**只作事实引用，未复用任何内容**。
- [ActiveCampaign/postmark-templates](https://github.com/ActiveCampaign/postmark-templates) — MIT，3200★，但已静止约 4 年。参考其**属性与行内样式双写**、`email-wrapper > content > body_inner` 的单列分层。
  更正：`postalsys/email-templates` **不存在(404)**；同组织的 `postalsys/templates` 是 Handlebars 模板语言库且根目录无 LICENSE 文件，与 HTML 排版无关，因此本次没有采用 postalsys 一线。

落地方式：`pilot_app/reports.py` 手写保守 HTML —— 单列 600px table、`width`/`bgcolor` 属性与行内 CSS 双写、`role="presentation"`、`table-layout:fixed` + `word-break` 防超长链接撑破手机宽度、保留 `<!--[if mso]>` 条件注释、**零 `<style>`/零 `<script>`/零 `@media`/无远程字体/无图片**。并用真实浏览器在 360/390/768/1280 宽度下自动检查横向溢出。

### 每日简报的"不漏信"结构

- [avihaymenahem/velo](https://github.com/avihaymenahem/velo) — 分类与可配置密度：参考其"先分类、再按类型分组"的信息架构。
- [slurdge/goeland](https://github.com/slurdge/goeland) — 每日摘要分组：参考"按天聚合 + 分节"的组织方式。
- [wanleung/summermail](https://github.com/wanleung/summermail) — GPL-3.0：**只作流程与界面参考，未复制代码**。
- [syafi-dev/MailSwitch](https://github.com/syafi-dev/MailSwitch) — 仓库未显示明确许可证：**只参考向导结构，未复制代码**。

落地方式：**没有找到"能保证不丢信"的现成实现可以直接复用**，因此每日简报改由本项目**本地确定性合成**（`reports.build_digest`）：从当天已存储的即时报告聚合，重复主题合并但计数保留，无法归类的邮件落到低优先级分节并保留发件人/主题/收件时间，未生成摘要的邮件单独列为失败项。这样"任何邮件都不得静默丢弃"由结构保证，而不是靠提示词约束模型。

## 第三轮（延迟与分级）新增的开源检索

目标：解决"邮件到收到报告接近 5 分钟"的感知延迟。检索关键词围绕"用户可感知延迟""分级路由""无模型的邮件分级"。

- [danieleschmidt/crewai-email-triage](https://github.com/danieleschmidt/crewai-email-triage) — **MIT**，纯标准库。**采用其模式**：三段式 `TriageAgent`（关键词 + 发件人信誉规则分级）→ `SummaryAgent`（1–2 句抽取式摘要）→ `PriorityRanker`（类别基准分 + 时效 + 关键词强度 + 发件人权威度，0–10 分），全部**不调用 LLM**，因此是毫秒级。本项目落地为 `pilot_app/triage.py` + `pilot_app/alerts.py`：到达后立刻发一封"已收到 + 类别 + 紧急度 + 抽取式提要 + 判定依据"的回执，完整报告随后照旧。**未复制其代码**，并按本项目场景（CityU 学生邮件、中英关键词、学生分类词表）重写了词表与判定。
- [Shubham-Jitendra-Bhadra/inference-router](https://github.com/Shubham-Jitendra-Bhadra/inference-router) — **MIT**。参考其**分级路由与可观测性**思路：按复杂度把请求分派到不同模型档位（ComplexityStrategy 打分 → 档位映射）、跟踪各档位滚动 p90 延迟并在超 SLA 时降级（LatencyStrategy）、每次调用回传 `latency_ms` / `tokens` / `cost_usd` / `tier_used`。**未引入其 SDK**（会增加 httpx 等依赖，且本项目是单进程轻量服务）。落地为本项目自己的：`_analyse` 每次打印 `search=..s generate=..s prompt=..chars answer=..chars tokens={...}`、`--measure` / `--measure-model` 量测模式、`INFE_PILOT_REPORT_MAX_TOKENS` 可调。**暂未做多档位路由**，因为当前 plan key 只允许一个模型（已实测），但要保留"主模型 + 快速模型"双配置的接口位置。
- [BerriAI/litellm issue #34819](https://github.com/BerriAI/litellm/issues/34819) — 记录了一个与本项目相关的真实问题：**长 time-to-first-token 期间 SSE 流没有任何字节，空闲超时会把连接掐断**。这解释了本项目实测到的"接口连接被中断（长回答可能超时）"，也说明流式并不能降低总时长，只能防止连接被判空闲。本项目因此保留非流式 + 瞬时故障自动重试。
- [gusaiani/ai-engineering 07-streaming-production](https://github.com/gusaiani/ai-engineering/blob/main/07-streaming-production/README.md) — 仅作流式生产化清单参考；本项目是邮件（离线发送），不需要流式。
- [Data-Wise/flow-cli: SPEC-email-dispatcher](https://github.com/Data-Wise/flow-cli/blob/dev/docs/specs/SPEC-email-dispatcher.md) — 只读参考其"告警/派发分离"的职责划分，未复用代码。

### 本轮**拒绝**的方案及理由

| 方案 | 拒绝理由 |
|---|---|
| 引入 inference-router / LiteLLM 作为常驻路由层 | 依赖与内存成本（2 GB 服务器），且当前只有一个可用模型，路由没有可选项 |
| 流式输出（SSE）降低延迟 | 邮件是离线投递，流式不能减少总生成时间（litellm#34819 只说明它防超时） |
| 缩短输出/限制 `max_output_tokens` | 实测无效：设 1500 时模型仍写 3408 字符、耗时反而 220.9s |
| 再调一次模型做"快速摘要" | 会变成每封两封 AI 邮件，违背"一封原邮件 + 一封报告"的既定产品定义，且第二次仍要等重型模型 |
| 用确定性规则**替代**模型报告 | 会低于用户已确认的七段报告要求；规则只用于**分级与回执**，不冒充分析 |

## 第四轮（会话安全、危险操作确认、审计）新增的开源检索

- [forgejo/forgejo issue #13464](https://codeberg.org/forgejo/forgejo/issues/13464) — Codeberg 上的活跃项目（5.5k star）。讨论"要求手输资源名确认危险操作"的 UX。**采用其结论的一部分**：确认手输只用于**唯一不可逆**的操作（删除用户），可逆操作保持一次点击；理由是手机上输入成本高、且强制输入会诱导复制粘贴从而失去确认意义。**未复制任何代码**。
- [ListenUpApp/server issue #69](https://github.com/ListenUpApp/server/issues/69) — 记录同类系统"管理员操作后用户会话未失效"的缺陷。作为"改密码必须吊销会话"的旁证；本项目自行实现服务端会话删除。
- jwt-allauth（Django/JWT token 白名单）等 —— **只作概念参考，未引入**。本项目是服务端会话表 + SQLite，`DELETE FROM sessions` 比 JWT 版本化/白名单简单且无需新依赖。
- 审计日志：参考"append-only 审计"通用做法（只提供列出接口，无修改/删除入口），自行实现单表。

## 采用的设计参考（第一轮）

- [Wangnov/mailpilot](https://github.com/Wangnov/mailpilot) — MIT。参考 IMAP IDLE/轮询、UID 去重、重试、首轮基线、多供应商回退和提示注入防护的设计；本项目未复制其 Go 代码。
- [Kheil-Z/elenchus](https://github.com/Kheil-Z/elenchus) — MIT。参考多用户 BYOK、每用户密钥隔离、自定义 OpenAI 兼容 Base URL 与模型配置方式；本项目使用独立的 Python/SQLite 实现。
- [BerriAI/litellm](https://github.com/BerriAI/litellm) — 核心为 MIT，enterprise 目录另行许可。参考统一供应商接口、预算与速率限制理念。试点未引入常驻 LiteLLM 代理，避免增加 2 GB 服务器负担；用户增长后可替换当前轻量适配层。
- [dhellmann/gmail-inbox-summary](https://github.com/dhellmann/gmail-inbox-summary) — MIT。参考 IMAP、HTML 报告、密钥安全和并发处理思路。
- [parthamehta123/email-intelligence](https://github.com/parthamehta123/email-intelligence) — MIT。参考 UID 状态、失败重试和减少原始正文留存的思路。
- [tonykipkemboi/gmail-imap-mcp](https://github.com/tonykipkemboi/gmail-imap-mcp) — MIT。参考通用 IMAP 连接和只读邮箱访问模式。
- [smalibary/pi-native-search](https://github.com/smalibary/pi-native-search) — MIT。参考「每个供应商一个原生搜索后端 + 能力标志 + 外部回退」的路由模式：优先用模型供应商自带的搜索（Anthropic `web_search_20250305`、Gemini `google_search`、OpenAI Responses `web_search`），没有原生能力才回退到外部搜索。本项目把它改造成单次生成调用里的能力判断与降级链，未引入其 TypeScript 代码。
- [Monogramm/autodiscover-email-settings](https://github.com/Monogramm/autodiscover-email-settings) — MIT。参考「服务器参数做成预设数据 + 另外提供给人看的手工设置支持页」的拆分方式（Thunderbird autoconfig 思路）。本项目的邮箱上手引导据此把 IMAP/SMTP 主机与端口做成按服务商自动填充的预设，并配上大白话术语解释与获取授权码的分步指引，未复用其代码或容器镜像。
- [sinedied/imapforward](https://github.com/sinedied/imapforward) — MIT。其在线配置生成器和“源邮箱 → 目标邮箱”清晰分层适合作为转发上手流程参考；本项目只采用显示目标地址、逐步引导和连接后验证的交互思路，未复制 Go 代码。

## 仅研究，未复制或引入

- [wanleung/summermail](https://github.com/wanleung/summermail) — GPL-3.0。功能接近，但许可证会影响分发选择；只作为流程和界面参考。
- [OlehDatsyk/email-agent](https://github.com/OlehDatsyk/email-agent) — 仓库未发现明确许可证；不复制代码。
- [guozhijian611/submail](https://github.com/guozhijian611/submail) — 仓库明确说明暂时没有项目级许可证；只参考其 AES-GCM、审计和队列安全清单，不复制代码。
- [max-ramas/rms-mail-public](https://github.com/max-ramas/rms-mail-public) — 多用户邮件/PWA 架构相关，但许可和商业边界不够清晰；不引入。
- `openensemble/openensemble`、`wildlifechorus/condenseit` 和 `byok-ai` — 有多用户、偏好学习或 BYOK 的相关思路，但在本次评估中未确认到足以支持直接复用的许可证/维护/安全组合，因此未引入。
- [syafi-dev/MailSwitch](https://github.com/syafi-dev/MailSwitch) — 有清晰的三步向导、连接验证和进度反馈，但仓库页面未显示明确许可证，而且它面向批量 IMAP 迁移并要求源邮箱凭据；仅参考其向导结构，不复制代码，也不引入 CityU 密码。

## 独立实现的关键差异

- 使用 CityU 外部转发到私人邮箱，完全绕开 Microsoft Graph 管理员同意依赖。
- 每用户模型 API 和搜索 API 分开配置；搜索只能看到脱敏后的主题。
- API key、邮箱授权码、待处理正文、报告都用 AES-256-GCM 按用户上下文加密。
- 自定义模型 Base URL、IMAP 和 SMTP 主机均阻止非公网目标，降低 SSRF 风险。
- 报告固定分成重要程度与结论、必须采取的行动与截止时间、邮件内容总结、个人相关性、联网建议与来源、风险/未知/推测和英文总结，**行动优先**。
- 每日 22:00 简报由本地确定性合成（分组、合并同类、计数、失败项），不依赖模型二次摘要，避免丢信。
- 先用「标准库 http.server + SQLite WAL + 单机轻量 worker」服务 3–5 人，避免过早引入 PostgreSQL、Redis、LiteLLM 网关等常驻组件。原先选择的 FastAPI/uvicorn 因锁定的 Starlette 分支存在未修复公告且无兼容修复版，已整体移除，见 README 的「Web 层」一节。
- 邮件 HTML 手写保守结构（table + 行内样式 + MSO 条件注释），未引入任何邮件模板框架或构建工具链。
