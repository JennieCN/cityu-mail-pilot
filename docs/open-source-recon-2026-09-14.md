# 开源方案调研：CityU Mail Pilot 下一步能做什么

> 调研日期：2026-09-14 · 版本基线：v0.15.0
> 方法：按 `open-source-first` 流程，用具体平台/语言/协议/部署约束去 GitHub、PyPI 等检索，
> **逐个打开真实仓库**（GitHub API / raw 源码 / 官方文档 / PyPI 元数据）核对 License、star、
> 最后提交、依赖树、维护状态。全文结论只覆盖**真正打开过**的页面。
> **未执行任何仓库里的脚本、CI、容器或安装命令**；未接触任何凭据或邮件内容。

---

## 0. 一句话结论

**没有可以整体复用的现成项目。** 所有同类产品都假设"可以写邮件服务器状态（标已读）"
或需要引入 Web 框架/重依赖，与本项目的**只读铁律**和**依赖预算**正面冲突。
真正有价值的是**四处代码级借鉴** + **一个标准库新能力**，全都零新增依赖。

---

## 1. 推荐排序（按"价值 ÷ 成本"）

| # | 做什么 | 新增依赖 | 预估成本 | 覆盖的问题 | 状态 |
|---|---|---|---|---|---|
| 1 | **systemd `OnFailure=` 告警**（挂 `certbot.service` / `cityumail-backup.service`） | 无 | ~15 行 + 2 个 drop-in，1 人时 | 备份/TLS **静默失败**（不可自愈） | 已核实落点 |
| 2 | **IMAP IDLE 实时收信**（Python 3.14 stdlib） | 无 | ~1 天 | 最坏 60 s 轮询延迟 | **已实测可行** |
| 3 | **worker 内哨兵**（收信停顿/队列堆积/磁盘/证书） | 无 | ~120 行 + 60 行测试，4–6 人时 | 指标面板"只能你去看" | 数据已在算 |
| 4 | **引用历史剥离**（`mail-parser-reply`） | 1 个（零依赖纯 Python，MIT） | 半天 | token 浪费 + 幻觉面 | 缺口已实证 |
| 5 | **坏 key 熔断**（自写，不引库） | 无 | 50–80 行，2–3 人时 | 坏 key 占满生成槽位 | 方案已定 |
| 6 | 线程归并（自写 RFC 5256 子集） | 无 | ~120 行 | 同话题邮件不成组 | 无可用 OSS |
| 7 | 确定性信任信号（SPF/DKIM/DMARC 喂给模型） | 无 | 半天 | 降幻觉、省 token | 可借鉴 MIT 实现 |

**如果只能做一件**：先做 **#1**。它成本最低、零新进程、**不触碰任何被 275 个测试覆盖的 Python 代码**，
因此不可能弄坏现有系统；而它覆盖的恰恰是**现在完全没人知道**的盲区。
对比：坏 key（#5）只有指数退避，最多 1 小时自愈，属**可恢复的降级**；备份/证书失败**不可恢复**。

---

## 2. 延迟：`imaplib.IMAP4.idle()` —— 本轮最重要的发现

### 事实（已双向核实）

| 项 | 结论 | 证据 |
|---|---|---|
| API 何时进入标准库 | **Python 3.14** | 官方文档 `docs.python.org/3.14/library/imaplib.html` 原文标注 `Added in version 3.14`，实现 RFC 2177 |
| 生产能不能用 | **能** | 实机 `python3 -c "hasattr(imaplib.IMAP4,'idle')"` → `True`（Python 3.14.4） |
| 开发机能不能用 | **不能** | 本机 `.venv-pilot` Python 3.9.6 → `False`，**必须写降级分支回轮询** |
| 两个邮箱支不支持 IDLE | **都支持** | 登录前 `CAPABILITY` 实测（零凭据）：<br>`imap.qq.com` → `... IDLE IMAP4REV1 MOVE NAMESPACE SASL-IR UIDPLUS XAPPLEPUSHSERVICE XLIST`<br>`imap.gmail.com` → `... IDLE IMAP4REV1 NAMESPACE QUOTA SASL-IR UNSELECT X-GM-EXT-1` |

### 为什么贴合本项目

- 现状：`INFE_PILOT_POLL_SECONDS=60`，最坏 **60 s** 才知道有新邮件；再叠加报告生成时间。
- 现有代码已经是 `client.select("INBOX", readonly=True)` + `uid("fetch", ..., "(BODY.PEEK[])")`
  （`pilot_app/mailio.py:103,126`），IDLE 可以**无缝插在这条 select 之后**，只读铁律不受任何影响。
- 官方用法（3.14 文档）：
  ```python
  with M.idle(duration=29 * 60) as idler:
      for typ, data in idler:
          ...
  ```
  `duration` 上限建议 ≤ 29 分钟，避免服务器 inactivity timeout。

### 实施要点与坑

1. **降级分支是硬要求**：开发机 3.9 没有该 API。用 `hasattr(imaplib.IMAP4, "idle")` 探测，
   没有就走现有轮询路径——这同时保证本机 275 个测试仍能跑。
2. **要新开线程模型**：IDLE 是长连接、阻塞式，每个邮箱占一个线程（当前 2 个邮箱，`MAX_USERS=5`，
   最多 5 个常驻连接，可接受）。不能复用现在"线程池轮询一轮就返回"的结构。
3. **QQ 没有 `UNSELECT`**：退出 IDLE 后不能 unselect。只读场景下直接 `logout()` 即可，
   **不要**用 `close()`（`close()` 在有删除标记时会 expunge，虽然我们从不标记删除，但从语义上更该避开）。
4. **断线重连必须做**：IDLE 连接可能被服务器单方面关闭，重连后要**补一次普通轮询**
   （用现有 UID 游标逻辑），否则断线期间的邮件会丢。
5. **IDLE 只是"知道有信"，仍要复用现有取信链路**：收到 `EXISTS` 后走原来的 `uid search` + `BODY.PEEK[]`。

### 参考

- Python 3.14 `imaplib` 官方文档：<https://docs.python.org/3.14/library/imaplib.html>
- RFC 2177 (IMAP4 IDLE)：<https://datatracker.ietf.org/doc/html/rfc2177>

---

## 3. 正文质量：引用历史剥离（实证缺口）

### 缺口是实测出来的，不是猜的

`pilot_app/mailio.py` 的 `normalize_message` 今天**只做**"优先取 `text/plain`，否则 `_strip_html`"，
全文 grep `quote` / `reply` / `原始邮件` **零命中**。城大邮件里大量是"转发 + 回复链"，
也就是每次都在把整条 `----- 原始邮件 -----` 连同几十行 `>` 引用一起喂给模型——**既烧 token 又扩大幻觉面**。

### 候选

| 仓库 | License | Star | 最后提交 | 运行时依赖 | 结论 |
|---|---|---|---|---|---|
| **[alfonsrv/mail-parser-reply](https://github.com/alfonsrv/mail-parser-reply)** | **MIT** | 84 | 2025-12-01 | **零**（`setup.py` 无 `install_requires`；`parser.py` 只 import `logging/re/dataclasses/itertools/typing`） | **唯一推荐**，13 语言含中文（作者标注 zh 为 untested，需自测） |
| [zapier/email-reply-parser](https://github.com/zapier/email-reply-parser) | MIT | 530 | repo 2024-07-22（PyPI 2020） | 零 | 英文为主，停更 |
| [github/email_reply_parser](https://github.com/github/email_reply_parser) | MIT | 708 | 活跃 | — | **Ruby，不是 Python** |
| SpamScope/mail-parser | Apache-2.0 | 455 | 2026-09-10 | 零 | 解析/容错强，但**不做**引用剥离，也不做 HTML→text |
| mailgun/talon | Apache-2.0 | 1,344 | PyPI 1.4.4 = 2017 | lxml/regex/numpy/scipy/scikit-learn/cssselect/six/html5lib/joblib | 依赖灾难，否决 |
| mailgun/flanker | Apache-2.0 | 1,649 | PyPI 2019 | 10 个 | 停更 + 重，否决 |

> 诚实标注：`mailparser/parser.py` 的 raw 路径返回 404，**没有逐行读源码**，其结论基于 README 字段说明。

### 关于 HTML→纯文本

现成件全都要加依赖：`inscriptis`（lxml+requests）、`html-text`（lxml）、`selectolax`（原生扩展）、
`html2text`（**GPL-3.0-or-later，只能参考**）。**标准库 `html.parser` 写约 60 行够用**，不必引库。

### 建议

引入 `mail-parser-reply`（零依赖 + MIT = 只多一份可审计源码，兼容 3.9/3.14），
把正文归一后取 `latest_reply`。**约半天**。若坚决不加任何依赖，照它的正则自写约 150 行。

---

## 4. 可靠性 A：坏 key 熔断 —— 自己写，不引库

### 结论：没有任何一个 Python 熔断库值得进 `requirements.txt`

逐个验证（**不是推断**）：**没有一个 Python 熔断库自带 SQLite/文件级持久化**。

| 仓库 | License | Star | 最后提交 | 持久化 | 结论 |
|---|---|---|---|---|---|
| [danielfm/pybreaker](https://github.com/danielfm/pybreaker) | BSD-3-Clause | 693 | 2026-07-04 | 有 `CircuitBreakerStorage` ABC，但内置只有 memory + redis，**无 sqlite** | 唯一值得考虑，仍不划算 |
| [fabfuel/circuitbreaker](https://github.com/fabfuel/circuitbreaker) | BSD-3-Clause | 523 | 2025-03-31 | 状态是私有 `self._state`，无钩子 | 拒绝 |
| rodmena-limited/resilient-circuit | Apache-2.0 | 2 | 2026-08-09 | 唯一持久化优先，但只有 **PostgreSQL** | 为熔断装 PG 显然荒谬 |
| arlyon/aiobreaker | BSD-3-Clause | 30 | 2021-12-27 | 内存 | 拒绝（停更 5 年 + asyncio-only） |
| mardiros/purgatory | MIT | 4 | 2024-11-07 | redis | 拒绝（1616 行 + 4 star） |
| [Netflix/Hystrix](https://github.com/Netflix/Hystrix) | Apache-2.0 | 24,479 | README 明写 maintenance mode | — | **只借鉴设计** |
| [sony/gobreaker](https://github.com/sony/gobreaker) | MIT | 3,694 | 2026-02-07 | Go | **只借鉴设计** |

### 值得借鉴的设计

- **Hystrix 三态机**（读的是 raw wiki `How-it-Works.md`）：CLOSED →（错误率超阈值）→ OPEN →
  （sleep window 后**只放一个**请求）→ HALF-OPEN → 成功回 CLOSED / 失败回 OPEN。
  本项目**每用户本来就串行**，可简化成"连续 N 次失败 → OPEN"，不需要请求量/错误率窗口。
- **pybreaker 的 `success_threshold`**：HALF-OPEN 要连续成功若干次才真正关闭，比"一次成功就恢复"稳。
- **半开探测必须真的花一次槽位**——这正是"临时挂起"与"指数退避"的语义差别。

### 落地要点

- 现状退避在 `pilot_app/service.py:503-506`：`retry_seconds = min(3600, 60 * (2 ** min(attempts, 6)))`。
  该公式与 systemd `RestartSec`/`RestartSteps`/`RestartMaxDelaySec` 的思路一致，**本身不用改**。
- 加一个表（`CREATE TABLE IF NOT EXISTS`，**不要 ALTER 已有 CHECK**）或给 `connections`
  加 `circuit_state` / `circuit_failures` / `circuit_open_until` 三列。`connections` 表**已有**
  `last_error` / `last_test_at` 可直接复用。
- **绝对不要写 `users.status='paused'`**：那是管理员所有、不自动过期的语义，
  混用会让"为什么这个用户被暂停"无法解释，也无法自动恢复。
- 队列过滤点现成：`database.py:1111 due_messages()`。

**成本：50–80 行 + 3 个单测，2–3 人时。**

---

## 5. 可靠性 B：定时告警哨兵 —— 不引入任何常驻监控进程

### 结论：现成监控系统全部超预算（2 vCPU / 2 GB / 2 真实用户）

| 项目 | License | Star | 核实到的资源占用 | 结论 |
|---|---|---|---|---|
| [healthchecks/healthchecks](https://github.com/healthchecks/healthchecks) | BSD-3-Clause | 10,327 | 官方未给 RAM；要 Python 3.12+ + Django + **独立 PostgreSQL**，15 个直接依赖 | 不用 |
| [louislam/uptime-kuma](https://github.com/louislam/uptime-kuma) | MIT | 91,339 | 无官方规格；issue #3817 社区实测**容器 800 MB**（19 探针，含 2 个 chromium）= 本机内存的 40% | 不用 |
| [prometheus](https://github.com/prometheus/prometheus) + node_exporter + alertmanager | Apache-2.0 | 66k / 13.7k / 8.6k | 3 个常驻进程；WAL 官方建议**至少保留 3×128 MB** | 不用 |
| [netdata/netdata](https://github.com/netdata/netdata) | **GPL-3.0** | 80,509 | 官方 README：默认 ~5% CPU / **150 MiB RAM**（= 本机 7.3%） | 不用（且 GPL） |
| [TwiN/gatus](https://github.com/TwiN/gatus) | Apache-2.0 | 12,062 | 官方只说 "negligibly small"，未给数字 | 唯一可考虑的框架，但仍是新进程 |
| [henrygd/beszel](https://github.com/henrygd/beszel) | MIT | 25,358 | 官方只说 "Lightweight" | 不用 |
| [zabbix](https://github.com/zabbix/zabbix) | **AGPL-3.0** | 6,370 | server + DB + agent | 不用 |
| [caronc/apprise](https://github.com/caronc/apprise) | BSD-2-Clause | 17,312 | **6 个运行时依赖** + 传递依赖 | 不用（超预算） |
| [bdd/runitor](https://github.com/bdd/runitor) | 0BSD | 356 | 极小 | **不能单独用**：自己不发邮件，须配 Healthchecks 服务端 |
| tildeslash/monit | GPL（官方文档站） | — | 未查到 | **命中 3/4 条需求但仍不建议**，见下 |

**monit 单独说明**：官方文档逐条核对确认它原生支持 `CHECK FILESYSTEM`（磁盘）、
`CERTIFICATE VALID for <n> DAYS`（**证书剩余天数**，正是想要的）、`SET MAIL-SERVER`（只发邮件）。
但它**给不了**"收信超过 X 分钟没成功"和"队列持续堆积"——只有本项目的 SQLite 知道，
得靠 `CHECK PROGRAM` 反过来调自己的 CLI。既然那个 CLI 无论如何都要写，
monit 剩下的价值只有磁盘 + 证书，而这两条用标准库 + **已有的 `cryptography`**
（`x509.load_pem_x509_certificate`）+ 已经算好的 `host_metrics()` 约 40 行就够。

### 好消息：要的数据**已经在算了**

实测确认（读本地代码，非推断）：

- `web.py:1034 _service_health()` 已经算出 `stale_mailboxes` / `pending_messages` / `failed_reports`
- `metrics.py host_metrics()` 已经算出 `disk.percent` / `memory.percent`
- **4 个条件里 3 个已经在算，只差"比阈值 + 发信 + 去重"**

### systemd 原生能力（本题最实用的答案）

读的是上游 man 源文件 `raw.githubusercontent.com/systemd/systemd/main/man/systemd.unit.xml`：

1. `OnFailure=`（systemd.unit(5)，v201+）：*"A space-separated list of one or more units that are
   activated when this unit enters the **failed** state."*
2. 官方 EXAMPLES 直接给了模板写法：`failure-handler@.service`（`Type=oneshot`）+
   顶层 drop-in `/etc/systemd/system/service.d/10-all.conf` 写 `OnFailure=failure-handler@%N.service`。
3. **关键拐点**：`systemd.timer(5)` 只负责"激活同名 service"，失败状态**不会从 service 冒泡回 timer**。
   → `OnFailure=` 必须挂在 **`.service`** 上，**挂 `.timer` 没用**。
4. systemd **不提供**邮件发送（man 全文无 email 示例），handler 要自己给。
5. 社区标准做法（Arch Wiki `systemd#Notifying with e-mail`，**非官方规范**）给了 12 行
   `systemd-email` 脚本骨架，但它依赖 `sendmail`；**本项目已有 smtplib，应让 handler 调自己的 CLI**。

**已在本项目核实落点**（线上实测）：

```
certbot.service                       loaded inactive dead    ← certbot.timer 的目标，可挂 OnFailure
cityu-mail-pilot-backup.service       loaded inactive dead    ← Type=oneshot（已读 unit 文件确认）
```

### 建议：两层，都是加法

1. **systemd `OnFailure=` handler**：~15 行 handler + 2 个 drop-in。零新依赖、零新进程、
   **不动任何 Python 代码**，因此不可能弄坏现有 275 个测试覆盖的逻辑。**1 人时。**
2. **worker 内哨兵**：~120 行 stdlib，复用上面已算好的字段 + 新增证书剩余天数
   （`cryptography.x509`，零新依赖），比阈值 → `smtplib` 发管理员 →
   **必须带"已告警/静默期"去重状态**（否则每轮都发）。+ 60 行测试。**4–6 人时。**

---

## 6. 同类产品：没有可整体复用的

| 仓库 | License | Star | 最后提交 | 贴合度与结论 |
|---|---|---|---|---|
| [RutkayAzizAksu/mail-triage-agent](https://github.com/RutkayAzizAksu/mail-triage-agent) | **MIT** | 1 | 2026-09 | **高**。值得抄 `trust.py`（89 行，SPF/DKIM/DMARC + Reply-To/显示名不一致的**确定性**检查）、`_decode()`（中文 RFC 2047 主题）、`smtp_sender.py`（UTF-8 中文正文 + 线程头）。**必须拒绝**它 `(RFC822)` 抓取（**会隐式置 `\Seen`**）和 `mark_seen()`；`filters.py` 用子串匹配，`cityu` 会命中 `notcityu.com`，**必须改成精确域后缀** |
| [ivni/imap-mcp](https://github.com/ivni/imap-mcp) | **无 LICENSE 文件**（raw `LICENSE` 404，spdxId 空；pyproject 自称 MIT）→ **视为未授权，只读不抄** | 1 | 2026-06 | 低（作为项目）/ 高（作为参考）。其 `fetch(["BODY.PEEK[]"])` 是本项目铁律的**正确写法参考**；`fetch_thread()` 给线程归并第二方案；`sanitize_html_for_quoting()` 给 HTML 清洗白名单策略 |
| [ContextualWisdomLab/threadweave](https://github.com/ContextualWisdomLab/threadweave) | **Apache-2.0** | 0 | 2026-09 | 中。`subject.py` 是完整 RFC 5256 base-subject 归一化（线程归并第一方案）。**只照算法自实现，不引依赖**（star 0、仅 1 个 release、需 ≥3.10） |
| benoitg/jwzthreading | BSD-3-Clause | 0 | 2013-11 | 低。Python 2 时代代码，**只能当算法读物** |
| G4brym/askemail | MIT | 18 | 2026-03 | 低。Cloudflare Workers，架构完全不同 |
| jaypetez/glean | MIT | 7 | 2026-09 | 低。**27 个依赖含 FastAPI**，与"迁回标准库"决策正面对撞 |
| Vibe-Coding-X/mailmind-ai-email-copilot | Apache-2.0 | 2 | 2026-08 | 低。FastAPI+Next.js+PG+Redis+Celery。仅借鉴 urgency/importance 打分 |
| AnshMNSoni/email-agent | MIT | 15 | 2026-09 | 低。LangChain 全家桶 + Gmail OAuth |

**明确否决**：`slashtechno/llmail`（**AGPL-3.0 且已 archived**）、`Peuqui/AIfred-Intelligence`
（**AGPL-3.0**，私有/内测无法满足合规路径）、`SykikXO/sparrow` 与 `LiteObject/inbox-ai`
（**无有效 LICENSE**）、`Nidhish-Balasubramanya/Intelligent-Email-Assistant`（FastAPI+Streamlit+PG）。
另：一切 `mark_as_read` / `store(+FLAGS \Seen)` / `RFC822` / `BODY[]` 抓取**一律否决**（会隐式置 `\Seen`）。

---

## 7. HTML 邮件渲染与日报：明确不做

| 方向 | 结论 |
|---|---|
| **CSS 内联化** | **不引入**。本项目是手写模板 + 全 inline CSS，**根本没有 `<style>` 块要内联**。候选要么停更（premailer 2021、inlinestyler 2018、pynliner 2017），要么拖 lxml + `cssutils`（**LGPL-3.0**）；唯一活跃的 [css-inline](https://github.com/Stranger6667/css-inline)（MIT, 316★）是 Rust 原生扩展、`requires-python>=3.10`，**开发机 3.9 装不上**，无法本地验证 |
| **邮件 HTML 兼容性数据** | **可暂缓**。[caniemail](https://github.com/hteumeuleu/caniemail)（**MIT**, 942★, 2026-08）有机器可读的官方 API `https://www.caniemail.com/api/data.json`（656 KB，308 条 feature）。但现有硬规则测试已把不变量钉死，其增量价值只在"要不要用某个新 CSS 属性"时出现 |
| **抽取式摘要库** | **不做**。`sumy`（Apache-2.0, 3,701★）等**全是单文档**摘要，既不跨邮件也不提供覆盖性保证，还把依赖树从 1 个拉到 10+ 个（lxml + NLTK + 语料）。**没有任何项目解决"跨邮件语义归纳 + 绝不漏信"**——现有"确定性清单兜底 + 额外调一次 LLM"已是该问题的最优形态 |
| **`@media` / 零 JS 约束** | 维持现状。MJML（MIT, 18k★）是 Node 编译产物，与"零 JS/单列表格"路线相反 |

---

## 8. 许可证与出处声明

- 本轮**没有任何库被引入 `requirements.txt`**。`requirements.txt` 仍然只有 1 个依赖 `cryptography`。
- 唯一建议新增的依赖是 **`mail-parser-reply`（MIT，零运行时依赖）**——若采纳，需在仓库保留其 MIT 版权声明。
- **只借鉴设计/算法、不复制代码**（各自保留出处）：Hystrix（Apache-2.0）三态机、systemd
  `failure-handler@.service` 官方模板、threadweave（Apache-2.0）RFC 5256 归一化规则、
  mail-triage-agent（MIT）`trust.py` 的确定性信任检查。
- **拒绝复制**：`ivni/imap-mcp`（无 LICENSE 文件 → 未授权）、`slashtechno/llmail` 与
  `Peuqui/AIfred-Intelligence`（AGPL-3.0）、`SykikXO/sparrow` 与 `LiteObject/inbox-ai`（无有效 LICENSE）。
- **数据源**：caniemail 的 `data.json`（MIT）；Python 官方文档（PSF）；systemd man page。

---

## 9. 建议的落地顺序

1. **systemd `OnFailure=` 告警**（1 人时，零风险）——补齐备份/证书盲区
2. **worker 内哨兵**（4–6 人时）——收信停顿、队列堆积、磁盘、证书
3. **IMAP IDLE 实时收信**（~1 天）——延迟从 60 s 降到近实时，含 3.9 降级分支
4. **引用历史剥离**（半天）——省 token、降幻觉
5. **坏 key 熔断**（2–3 人时）——保护并发槽位，后台标红
6. 线程归并、确定性信任信号（按需）

**每一项都必须遵守既有铁律**：只读 `BODY.PEEK[]`、不暴露密钥、部署前备份、
改完跑 275 个测试全过、真机验证后再部署。

---

## 10. 候选项目评分（2026-09-14 补，依据实测）

用户要求对三个候选"算重要程度、成功率和效果"。下面是按**实测数字**算的，不是估算感觉。

**综合期望值 = 重要程度(1–5) × 效果(1–5) × 成功率(0–1)**；每 人时 = 期望值 ÷ 预估人时。

| 排名 | 项目 | 重要程度 | 成功率 | 效果 | 期望值 | 预估人时 | 每 人时 |
|---|---|---|---|---|---|---|---|
| **1** | **IMAP IDLE 实时收信** | 4 | 80% | 5 | **16.0** | 8 | **2.00** |
| 2 | 引用历史剥离 | 3 | 70% | 2 | 4.2 | 4 | 1.05 |
| 3 | 坏 key 熔断 | 2 | 95% | 1 | 1.9 | 2.5 | 0.76 |

### 10.1 IMAP IDLE —— 效果 5/5：轮询是延迟的主体

生产库与代码实测：

| 组成 | 今天 | IDLE 后 |
|---|---|---|
| 轮询等待 | 均值 30 s，最坏 60 s（`POLL_SECONDS=60`） | ~1 s |
| 报告生成 | **5–9 s**（最近 6 封实测，全部 `attempts=1`） | 不变 |
| SMTP | ~1–2 s | 不变 |
| **我方可控总延迟** | **均值 ≈37 s，最坏 ≈70 s** | **≈8 s，最坏 ≈10 s** |

**轮询占我方可控延迟的 83–86%**，是唯一"改一处、砍掉绝大部分"的项目。

已实测的技术前提：生产 Python **3.14.4** 有 `imaplib.IMAP4.idle`（3.14 新增，RFC 2177）；
`imap.qq.com` 与 `imap.gmail.com` 登录前 `CAPABILITY` **都返回 `IDLE`**。

残余 20% 风险：① 开发机 3.9 无此 API，必须写降级分支；② 长连接是**新的故障模式**
（静默断线 → 漏信），必须重连 + 补一次普通轮询；③ QQ 无 `UNSELECT`，退出 IDLE 只能 `logout()`；
④ 线程模型要改。

附带收益：现每 60 s 对 2 个邮箱各登录一次 = **每天 2880 次 IMAP 登录**，IDLE 后可大幅降低。

#### ⚠️ 10.1.1 实现完成后的实测结论：**在 QQ 上不可用，已默认关闭**（v0.17.2）

上面那张"IDLE 后"的收益表**没有实现出来**。代码写完了、测试全过、也部署过（v0.17.0 / v0.17.1），
但真机验收给出了**否证**结果：

| IDLE 窗口 | 同时存在的其它连接 | 收到推送？ |
|---|---|---|
| 9 秒，两次轮询之间 | 轮询器在跑 | **收到**，2 秒 |
| 200 秒，跨 3 个轮询周期 | 轮询器在跑 | **没收到** |
| 190 秒，每 45 秒重新进入 IDLE | 轮询器在跑 | **没收到** |

**机制**：QQ 似乎只把 unsolicited `EXISTS` 投递给"最近一次 SELECT 该邮箱的那个连接"。
我们自己的 60 秒轮询器每分钟用一个新连接 select 一次，于是长连接 IDLE **在第一次轮询之后就永久失聪**；
而单纯重新进入 IDLE（DONE + IDLE）**抢不回通道**，因为那不是 `SELECT`。

**为什么不做"让 IDLE 独占连接"**：那等于放弃轮询器，也就放弃了"一个轮询周期内必定发现邮件"的保证。
推送一旦静默失效，最坏情况会从 **60 秒**变成 **`IDLE_SECONDS`（300 秒）**。
对一个"最坏失败是用户错过截止日期"的产品，用已证明的界限去换未证明的界限、只为在常见情况下快一点，
方向是反的。

**因此**：`INFE_PILOT_IDLE` 默认 **0**（opt-in），模块保留（已测试、无害、重估成本低），
docstring 里记录了完整的三次对照测量。要启用必须先在**实际使用的供应商**上重跑上面的测量。

**这次失败的教训（比结果本身重要）**：
1. **"API 存在 + 服务器声称支持"不等于"功能可用"**。两件事都实测为真，功能仍然不可用。
2. 第一次验收之所以"看起来还行"，是因为测试信恰好被 60 秒轮询器抓到——**测量窗口选错会得出相反结论**。
3. 关键证据来自 `faulthandler` 打线程栈：它证明监听器确实正常阻塞在 IDLE 里，
   从而把怀疑方向从"我的代码"正确地转向了"服务器行为"。
4. 中途我还写错了两次探针（Python 3.14 的 `capabilities` 元素是 `str` 不是 `bytes`；测试信因 QQ 限流没送达），
   **又是"先怀疑工装"**。

### 10.2 引用历史剥离 —— 效果只有 2/5（修正上一版的暗示）

代码事实成立：`mailio.normalize_message` 确实**零引用处理**（grep 确认）。但实际开销量出来很小：

- 每份精简报告输入 token 实测：**926 / 1008 / 1850 / 1941 / 2879**（中位 **1850**）
- 固定提示词约 1112 字符（≈600–700 token），正文占 72–78%（正文上限 20000 字符，实际没截断）
- 平均**每份报告成本 $0.000465** —— 即使砍掉一半输入，省下的是 **$0.0001 量级**

**不确定性（必须诚实标注）**：正文在 `finish_message` 里被清空（隐私设计），本地 `.e2e/` 副本也基本清空，
所以**真实的引用占比无法测量**。能确定的只有"输入 token 不大（中位 1850）"，
说明本试点收到的大多是**短邮件**而非长回复链。

**真实价值是质量而非成本**：降低"把引用历史里的旧事当成新事"这类幻觉。
成功率给 70%（不是更高）是因为 `mail-parser-reply` 的**中文被作者标注为 untested**，
而城大邮件是中英混排 + 中文引用头（"在…写道："）；**剥离错了就是丢信息**，
与"绝不漏信"原则冲突，必须做"剥离后太短就退回原文"的兜底。

### 10.3 坏 key 熔断 —— 重要程度 2/5：该故障从未发生过

最反直觉的实测发现：

- 全部 80 封邮件里 **`status='failed'` 为 0 条**
- 16 封 `attempts≥3` 的**全部集中在 `2026-09-13T07:21:10` 这一个时刻** —— doubao 时代的批量回填，
  而那个慢模型**已经不用了**
- **deepseek 时代每封都是首次即成功**（最近 6 封实测 `attempts=1`）

指数退避（`min(3600, 60·2ⁿ)`）工作正常，最坏 1 小时自愈。真发生时影响也有限：
2 用户、`REPORT_WORKERS=6`，一个坏 key 占 1 个槽位 ≈ **17% 容量损失**。

成功率 95%（纯逻辑、可完全离线测试、零依赖），期望值低是因为**分子太小**：
它是"为未来用户变多而准备"的项目，不是现在的痛点。

### 10.4 结论（v0.17.2 更新：第 1 条已被实测推翻）

1. ~~**先做 IMAP IDLE**~~ —— **做了，然后在真机验收中被否证**。
   见 §10.1.1：在 QQ 上，我们自己的 60 秒轮询器会把推送通道永久抢走，IDLE 收不到任何通知。
   代码保留但 **默认关闭**（`INFE_PILOT_IDLE=1` 才启用）。
   **教训**：评审时"API 存在 + 供应商声称支持"两条都实测为真，却仍然得出错误结论——
   因为**没人验证过"服务器会不会真的推给我们"**。以后凡涉及第三方行为的功能，
   验收标准必须是"观察到端到端事件"，不是"能力探测通过"。
2. **引用剥离降级为"质量修复"**：先做零风险的一步 —— 只在"剥离后正文 ≥ 原 50%"时才采用，
   否则退回原文；这样失败模式永远是"没省"，不会是"漏信"。（**未做**）
3. **熔断暂缓**：等真实用户数超过 5，或第一次真的出现坏 key 再做。（**未做**）

**已落地并验证的是另一个方向**：v0.16.0 的**告警体系**（巡检哨兵 + systemd `OnFailure=`）——
它不改善延迟，但消灭了"备份/证书静默失败"这个此前完全无人知晓的盲区。
延迟问题的下一步应该回到**供应商能力**（换一个有快模型的 key），而不是继续在客户端做文章。
