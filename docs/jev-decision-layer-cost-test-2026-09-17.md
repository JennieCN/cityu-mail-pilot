# Jev 决策层成本测试设计（2026-09-17）

问题：**如果让 Jev 充当决策层，能比现有方案节约多少成本？**

先给结论，再给可执行的实验设计。

---

## 0. 结论（先看这个）

**在当前架构下，引入 Jev 决策层不会省钱，只会加钱——除非它同时替换掉报告生成层。**

算术依据（全部来自仓库实测数据，出处见 §1）：

| | 现有方案 | 现有 + Jev 决策层 |
|---|---|---|
| 决策成本/封 | **$0**（`triage.py` 纯规则，零模型调用） | + Jev 每封调用费 |
| 报告成本/封 | $0.00047（精简版实测） | 不变 |
| **合计/封** | **$0.00047** | **$0.00047 + Jev 费用** |

现有决策层的成本已经是理论下限（零），任何「额外加一层 Jev 决策」都只能往上加钱。
Jev 每封哪怕只花 $0.00001，也是 100% 的增量。

**Jev 能省钱的唯一位置**：把「该不该为这封邮件生成报告」的判断做对，从而**跳过**
报告调用。但现状是所有本校来信（`@cityu.edu.hk` 白名单）都已经生成报告——真正的
省钱杠杆是「用决策层砍掉不值得报告的邮件」，而不是「换掉一个本来就不花钱的决策层」。

因此正确的问题是：**「Jev 决策层 + 跳过报告」相比「全量报告」能省多少？**
盈亏平衡条件（每封）：

```
省下额 = p_skip × C_report  −  C_jev  −  p_skip × C_jev  >  0
```

- `p_skip`：Jev 判定为「不值得报告」的邮件比例
- `C_report`：被跳过的报告单封成本（精简版 $0.00047；完整版 $0.00068）
- `C_jev`：Jev 单封调用成本

**C_jev 的先验估计（2026-09-18 官方价更新）**：Jev 只按输入计费，$42/Btok =
$0.000000042/token。决策调用的 `state` 是邮件正文 + 规则提示（参照报告输入
~2000 token），加 questions 定义约 300 token，估计 **~2300 输入 token/封 →
C_jev ≈ $0.000097 ≈ $0.0001**（输出免费）。按报告成本的 ~20% 计。

代入实测值（精简版）：

| p_skip | C_jev 盈亏平衡上限 | C_jev ≈ $0.0001 时的单封净省 |
|---|---|---|
| 30% | $0.000141 | +$0.000041（省 8.7%）|
| 50% | $0.000235 | +$0.000135（省 29%）|
| 70% | $0.000329 | +$0.000229（省 49%）|

**先验结论：按官方价，C_jev 大概率低于全部三档盈亏平衡线——只要 p_skip ≥ 30%
且中文质量达标，省钱方向成立。** 但注意两点：① 输入 token 估计用的是报告调用的
输入量，Jev 决策 prompt 可以更短，实际以实验一实测为准；② 决定成败的已经不是
价格而是**质量**——CJK 支持不均衡是官方自己标注的风险。

规模参考（7 用户 × 3 封/天 × 30 天 ≈ 630 封/月，来自 `docs/report-mode-2026-09-15.md` §4；
220 人满载 ≈ 2 万封/月，`capacity.py PILOT_HARD_CEILING`）：

| 方案 | 月成本（精简版） |
|---|---|
| 现状：全量报告 | ≈ $0.30 |
| Jev 决策层，p_skip=50%，C_jev=$0.0001 | 630×(0.5×0.00047+0.0001) ≈ **$0.21**（省 ~30%）|
| 220 人满载同参数 | 20000×同式 ≈ **$6.7 vs $9.4**（省 ~$2.7/月）|

绝对金额仍然很小，但这个项目的设计原则（`pricing.py` 模块注释）是「不假装知道
不知道的数」——所以实验设计的目标是**把 p_skip 测出来、把 C_jev 的 token 估计
变成实测**，而不是先验地宣称省钱。

---

## 1. 已知事实（全部可复核）

### 1.1 现有决策层是零成本规则

`pilot_app/triage.py` 开头注释明说：*"Deterministic mail triage — no model call, so
it is instant"*。采用 crewai-email-triage 的三段式（classify → summarise → rank）
纯规则实现，中英文关键词级联 + 发件人权威度 + 时效加权，纯 stdlib，零 API 成本。

决策结果现在被喂给报告模型当提示（`service.py:61` `INCLUDE_TRIAGE_HINT`，默认开），
注释里的原话：*"the model is a heavy reasoner and re-deriving the category is part
of that hidden cost. Only kept if a real A/B shows it does not hurt quality or
latency."* ——仓库里已经预留了一个「决策/生成成本互相作用」的实验位。

### 1.2 报告生成的实测成本（生产环境）

`docs/report-mode-2026-09-15.md` §4，生产 deepseek-flash 实测（含峰值倍率）：

| 模式 | 输入 tokens | 输出 tokens | 单封成本 |
|---|---|---|---|
| 精简版 brief | 2057 | 273 | ≈ $0.00047 |
| 完整版 full | 1850 | 676 | ≈ $0.00068 |

价格表 `pilot_app/pricing.py`：deepseek-flash 输入 $0.15/M（缓存命中 $0.003）、输出
$0.6/M，峰值（UTC 周一至五 01–04、06–10 点）×2。

### 1.3 管线与规模假设

- 邮件白名单：只处理 `@cityu.edu.hk` 来信（`service.py` `ALLOWED_SENDER_DOMAINS`）。
- 每封**处理一次**：`status='pending' → processing → sent/skipped/failed`，带重试
  （`service.py:_generate_with_retry`，transient 才重试，坏 key 熔断）。
- 规模假设：`capacity.py` `DEFAULT_MAILS_PER_USER_DAY = 3.0`；内测规模 7 人
  （report-mode 文档）、容量上限 220 人（`capacity.py` `PILOT_HARD_CEILING`）。
- token_usage 表（`database.py:186`）按 user/report 记录 input/output/缓存/成本，
  是天然的实验数据采集器，不需要新埋点。

### 1.4 Jev 是什么（官方文档核实于 2026-09-18，docs.typesafe.ai）

TypeSafe AI 的「System One 决策模型」（前 OpenAI 研究员 Diogo Almeida 创办）。
以下来自官方文档，不再是宣传口径：

- **协议形态：私有，非 OpenAI 兼容**。唯一端点 `POST https://api.typesafe.ai/v1/systemone`，
  `Authorization: Bearer <key>`。请求体是 `{state, model, questions}`，questions 支持
  三种原语：`noul`（真伪 0–1）、`choice`（带 criteria 的选项）、`score`（带 legend 的评分），
  响应带概率分布与 confidence。**所以上一轮说的「方案一 custom_openai 零代码接入」不成立**，
  接入必须走新 preset + `providers.py generate()` 新分支（原方案二）。
- **价格（官方 Models 页，jev-1.13.0）**：**$42/Btok，只按输入 token 计费，输出免费**，
  即 $0.042/Mtok。无峰值倍率、无缓存价差——比 DeepSeek 的三段价简单。
- **用量回报**：响应 `usage.input_tokens / output_tokens`，口径与本项目 `token_usage`
  表对得上（缺 cached 列，记 0 即可）。
- **限额**：250k tokens/秒、1200 请求/分钟、单请求 64k tokens——对本项目每封邮件
  一次调用的规模，限额毫无压力。
- **一次调用可并行多个问题**：官方称加问题几乎不增加响应时间，且批量提问比逐条
  调用「便宜 12.2 倍、快 10.0 倍」（cookbooks/parallel_questions）——决策层应该把
  「是否紧急 / 是否需要行动 / 分类」放进**同一次调用**。
- **模型名**：`jev-latest` = `jev-1.13.0`（当前同一权重）。官方建议在代码里钉版本号。
- **⚠️ CJK 支持不均衡**：官方原话 *"English works best; other languages (including CJK)
  are supported but unevenly — testing is advised"*。而 CityU 邮件是中英混排——这条
  直接把「中文邮件质量测试」升格为实验二的**第一道闸门**。
- 仍属 early access，控制台发 key（console.typesafe.ai），API key 管理在 settings/keys。

上一版本把 C_jev 当最大未知数；现在价格已知，实验一的焦点改为**接入验证 + 每封邮件
决策调用的真实 token 用量**（见 §2 实验一的更新）。

---

## 2. 实验设计

### 实验一：接入验证与 token 用量实测（更新于 2026-09-18）

**目的**：验证 Jev 接入形态，把 C_jev 从估计值变成实测值。

1. **接入**（形态已确认是私有协议，见 §1.4）：
   - 实验阶段不动 `providers.py`，写独立脚本 `tools/jev_probe.py`（仿
     `manage.py check-model` 的口径）直接 POST `/v1/systemone`；
   - key 从 console.typesafe.ai 取，放 `pilot.env` 的私有段落或 shell 环境变量，
     **绝不进 git**；
   - 决策 questions 一次调用打包三个：`worth_reporting`（noul）、`priority`
     （choice: urgent/academic/opportunity/administrative/low）、`action_needed`
     （noul）——利用官方「加问题几乎不加时延且批量更便宜」的特性。
2. **实测 token**：用生产导出的 ≥50 封真实邮件（中英混合）各打一次，记录
   `usage.input_tokens` 分布（P50/P95）→ 换算 C_jev 实测值（单价 $0.042/Mtok，
   输出免费）。
3. **同步验证**：延迟 P50/P95（官方宣传 ~70ms，实测为准）、429/529 频率、
   `model` 回显字段（确认 `jev-latest` 实际解析到的版本）。
4. **中文质量快检**（提前到实验一，因为官方标注 CJK 不均衡）：抽 20 封纯中文/
   中英混排邮件，人工核对三个问题的答案是否合理。**如果中文邮件的判断明显不可靠，
   实验二不必做，直接终止**——质量是比价格更硬的闸门。

**判定门槛**：实测 C_jev > $0.00033 → 终止（比报告成本还贵的 70% 以上，无意义）；
中文快检不合格 → 终止（或只对英文邮件启用，但 CityU 邮件主体是中英混合，意义有限）。

**接入提示（若进入实施阶段）**：Jev 响应无 `cached_input` 概念，`token_usage` 表
记 0 即可；`ProviderError`/`TransientProviderError` 分类照抄现有结构，429/529 归
transient（官方推荐指数退避，SDK 自带 RetryPolicy，但实验用裸 HTTP 即可）。

### 实验二：跳过率与误判率测定（p_skip + 质量护栏）

**目的**：测 Jev 决策的「可跳过比例」和「跳错的代价」，这是省钱故事的成立前提。

**数据集**：从生产库导出近 30 天 `messages` 表（`subject`、`sender_*`、`body`、
现有 `triage` 分类、`importance`），按现有规则分桶抽样 ≥200 封，人工标一层金标准：
- 每封标注「值得报告 / 不值得报告」（两人标注，分歧仲裁）；
- 同时保留现有 `triage.py` 的规则判定作为第三个对照。

**三臂对比（同一数据集）**：

| 臂 | 决策方式 | 产出 |
|---|---|---|
| A（现状） | 无决策，全量报告 | 基线 |
| B（规则） | `triage.py` 规则分低价值桶（如 low/administrative 且无 action） | 免费决策的上限 |
| C（Jev） | Jev 分类「值得报告？」 | p_skip、混淆矩阵 |

**质量护栏（先于成本）**：对 C 臂计算
- **漏报率**（该报告的被跳过）：目标 <2%。漏一封 DDL 邮件的代价 > 一个月的模型费，
  这是「不静默丢弃」产品承诺（README）的直接延伸。混淆矩阵里重点看
  URGENT/ACTION 类邮件有没有被判成 low；
- 与臂 B 的差异：如果规则免费的 p_skip 和 Jev 差不多，Jev 没有存在价值。

**产出**：`p_skip`、混淆矩阵、漏报清单（人工复核每一封漏报）。

### 实验三：在线 A/B（成本 + 延迟终测）

只有实验一（C_jev 达标）和实验二（漏报率 <2%）都通过才做。

**设计**：按用户 hash 分流（不改代码，用两个部署实例或 env 开关灰度）：
- 对照组：现状全量报告；
- 实验组：Jev 决策前置，判定「不值得报告」的邮件走 `status='skipped'`、
  `skip_reason='jev-low-value'`（表结构现成，`messages.status` 已有 skipped 态）。

**采集口径**（全部现成）：
- 成本：`token_usage` 按天聚合，平台 key 与 BYOK 分开（`on_platform` 列）；
- 延迟：报告生成 gap（`capacity.py` 的 `generation_gaps` 口径）；
- 投诉：日报 skipped 计数 + 用户反馈（「我的邮件怎么没报告」是致命信号）。

**时长与样本**：≥2 周，覆盖至少一个周末（学生邮件周中/周末分布不同）；实验组
≥5 用户或 ≥400 封邮件，否则 p_skip 置信区间太宽。

**终止条件**（任一触发即回滚实验组）：漏报投诉 >0；实验组 skipped 率比实验二
预估高 20 个百分点以上（说明真实分布和离线集不同）。

### 明确不测的东西

- **不测「Jev 替换报告生成」**：若 Jev 真的不能生成文本（公开资料如此声称），这条
  路径不存在；若后续发现它能生成，另立实验，不与本实验混。
- **不测 digest 综述**（`digest_synthesis.py`，每用户每天 ≤1 次、400 token 上限），
  占比可忽略。
- **不测 sentinel agent**（`agent.py`）：那是运维侧按需触发的，不在每封邮件路径上。

---

## 3. 汇报格式

实验完成后，用这张表回答「省多少」：

```
C_jev（实测单封，$0.042/Mtok × 实测输入 token） = $______
p_skip（实测跳过率）     = ____%
漏报率（护栏）           = ____%（目标 <2%）
单封净省                 = p_skip × 0.00047 − C_jev = $______
月省（630 封规模）       = $______
220 人满载（≈20k 封/月） = $______
```

以及一句必须如实写的话：**绝对金额在当前内测规模下是每月几毛钱量级；这个实验的
价值在于拿到 p_skip 与 C_jev 的实测数，为规模化（>100 用户）与完整版用户占比
上升后的成本结构做决策，而不是立刻省出一笔可观的钱。**

若 C_jev 或漏报率不达标，结论同样有价值：现有零成本规则决策层没有替换必要，
把「省钱」的讨论转向真正有效的杠杆（精简版 vs 完整版、平台 key 预算上限）。
