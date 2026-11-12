# 发件箱（outbox）设计草案

状态：草案（2026-11-12）。**本文件不描述已实现的代码**，它是 `advisor-plans/008-outbox-design.md`
要求的设计产出；落地按「第一刀」分阶段，见文末。

范围：即时报告（`process_message`）、每日简报（`send_daily`）、两段式的精简版（brief）。
不涉及 Web Push（`push.py` 有自己的 `reminder_deliveries` 状态机，那是另一条通道）。

---

## 1. 为什么要它

现在「发送」的状态散在三处，每一处都只够它自己那个场景用：

| 位置 | 值 | 表达不了的事 |
|---|---|---|
| `reports.status`（`database.py` 的 `reports` 建表） | `generated` / `sent` / `failed` | 「可能已经发出去了」 |
| `reports.last_error` 前缀 `sending:`（`service.py:28`、`:950`、`:1088`、`:1246`） | 一段错误文本兼职状态机 | 结构化查询、统计、过期、并发 |
| `messages.status`（`pending/processing/sent/failed/skipped/held`） | 邮件那一行的收尾 | 与「报告发没发」是两件事 |

两个后果：

1. 每次出现「SMTP 收了、我们没记上」的新组合，都要**再打一个补丁**，而且补丁之间要互相解释
   （005 的注释里已经写着「与下面收尾失败同一个口径」；006 又加了 `messages.brief_sent_at`）。
2. 有些窗口只能靠「假装成功」来关掉：日报路径一旦记账失败，如果不退回 `failed`，
   就只能把 `last_error` 当前缀用（007）。

`pilot_app/mailio.py` 里那句话是准确的：

> 这不是 exactly-once——SMTP 收了而应答丢了那一档本来就无法从这一侧证明，真正的幂等要 outbox。

outbox 改变的是**顺序**：先把「要发这一封」写成一行持久记录（与业务状态同一事务），
再由一个发送循环去兑现。这样「有没有发送意图」「兑现到哪一步」都是结构化状态，
不依赖错误文本，也不依赖进程还活着。

---

## 2. 现成模板：`calendar_reconcile_outbox`

日历索引已经在用一版 outbox，形状可以直接抄（`database.py:1061-1077`）：

```sql
CREATE TABLE IF NOT EXISTS calendar_reconcile_outbox (
    report_id TEXT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_generation INTEGER NOT NULL,
    claimed_generation INTEGER NOT NULL DEFAULT 0,
    processed_generation INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','claimed','done','dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    claim_token TEXT, lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_reconcile_due
    ON calendar_reconcile_outbox(status, available_at, lease_until);
```

三个关键字段各自解决一件事：

- **`requested_generation` / `claimed_generation`**：同一条意图可以被重复请求（索引重算、
  报告重建）。发送这边也有同样的问题：一封报告可能被重新触发（运营手工重发）。
  用 generation 而不是布尔，能区分「我领的是哪一次请求」。
- **`claim_token` + `lease_until`**（`claim_calendar_reconcile`，`database.py:5167`）：
  领取者中途死掉，租约到期后别人可以接手；接手时用 token 做**条件更新**，
  两个发送者不会同时写同一行。
- **`available_at` + `attempts`**（`fail_calendar_reconcile`，`database.py:5265`）：
  退避与放弃阈值都在行上，不靠进程内存里的定时器。

发送与索引的区别只有一处、但很关键：**索引失败可以重算，发信失败不能重算**。
所以 outbox 发送侧多一个状态：`unknown`。

---

## 3. 状态机

```
pending ──claim──▶ claimed ──SMTP 2xx──▶ sent
   ▲                  │
   │                  ├──SMTP 明确拒收/连接失败──▶ pending（available_at 退避）
   │                  │
   │                  ├──进程死/租约过期──▶ pending（重新领取，但见下）
   │                  │
   │                  └──SMTP 之后、记账之前出错──▶ unknown
   │
   └────运营手工重触发────────────────────────────┘
                         unknown ──人工确认「确实发了」──▶ sent
                                └──人工确认「没发」────▶ pending
```

- **`unknown` 是这套设计里最重要的状态**：它表示「调用过 SMTP，但无法证明结果」。
  自动流程**不**把 `unknown` 变成 `pending`——那正是现在 `sending:` 前缀的语义，
  也是 005/007 的取舍（宁可少发一封，不可重复发一封）。
- **租约过期 ≠ 可以重发**。过期只能说明「领取者没了」，不能说明「SMTP 没被调用」。
  所以领取者必须在**调用 SMTP 之前**把 `claimed_at` / 尝试序号写进行里；
  租约过期后接手的那一方看到的若是「已过 SMTP 点」，一律转 `unknown`，不重发。
  这一条要求行里有一个 `smtp_started_at`（或 `attempts_smtp`）字段，是模板之外要加的。
- `dead` 是人工放弃（例如收件地址长期失效），不再自动重试，但保留在后台可见。

## 4. 表结构草案

```sql
CREATE TABLE IF NOT EXISTS send_outbox (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('immediate','daily','brief')),
    report_id TEXT REFERENCES reports(id) ON DELETE CASCADE,   -- immediate / daily
    message_id TEXT REFERENCES messages(id) ON DELETE CASCADE, -- brief（以及便于追溯）
    status TEXT NOT NULL CHECK(status IN ('pending','claimed','sent','unknown','dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    smtp_started_at TEXT,                     -- 过 SMTP 点的唯一证据
    available_at TEXT NOT NULL,
    claim_token TEXT, lease_until TEXT,
    message_id_header TEXT NOT NULL DEFAULT '',  -- stable_message_id() 的结果
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_send_outbox_subject
    ON send_outbox(user_id, kind, COALESCE(report_id,''), COALESCE(message_id,''));  -- 幂等键
CREATE INDEX IF NOT EXISTS idx_send_outbox_due
    ON send_outbox(status, available_at, lease_until);
```

- **幂等键**取 `(user_id, kind, report_id | message_id)`：一封报告、一封简报、一封简报的
  精简版各只有一行；重复入队是 `ON CONFLICT DO NOTHING`，不是第二行。
- `message_id_header` 落库的意义：重试时沿用同一个 `Message-ID`（`mailio.stable_message_id()`），
  至少让收件端与日志能对上号。

## 5. 一次发送的时序

```
业务事务（同一个 BEGIN IMMEDIATE）
  ├─ create_report(...) / 日报行复用
  └─ INSERT INTO send_outbox(status='pending', available_at=now)
COMMIT                      ← 到这里，「要发」已经是持久事实

发送循环（worker 的一轮）
  ├─ claim：UPDATE ... SET status='claimed', claim_token=?, lease_until=?
  │         WHERE id=(SELECT ... status='pending' AND available_at<=now)
  │           AND status='pending' RETURNING *          ← 条件更新，防双领
  ├─ 若 smtp_started_at 非空（上个领取者已过了 SMTP 点）→ status='unknown'，不发送
  ├─ UPDATE ... SET smtp_started_at=now WHERE id=? AND claim_token=?   ← **过点标记**
  ├─ mailio.send_report(...)                            ← 外部副作用只在这里
  │    ├─ 2xx/正常返回 → status='sent' + 业务收尾（report.sent、messages 收尾）
  │    └─ 明确失败     → status='pending'（available_at 退避） + 业务记 failed
  └─ 任何「SMTP 之后」的异常 → status='unknown' + last_error
```

与现有代码的对应关系：

- 「过点标记」取代今天的 `mark_report_sending()`（`database.py` 的 005 版本）；
- `status='unknown'` 取代 `last_error` 的 `sending:` 前缀（`service.py:28` 那个常量可以删）；
- 收尾仍然用 `finish_message_unrecorded()` / `finish_message()`，但它们由发送循环在
  `sent`/`unknown` 之后调用，而不是散在三个业务流程里；
- 日报的 `DIGEST_RETRY_BACKOFF`（`service.py:48`）退化为 outbox 的 `available_at` 计算，
  业务侧不再自己算重试间隔。

## 6. 迁移与回滚

**分三段，每段都能独立回滚：**

| 阶段 | 做什么 | 回滚 |
|---|---|---|
| P1 dual-write | 三条发送路径在**原有标记之外**多写一行 outbox；发送仍走老路径；一个只读的对账任务每 5 分钟比较「outbox 行 vs 实际报告状态」并记录差异 | 删掉写 outbox 的那一句，表留着不用 |
| P2 切换 | 发送循环接管 immediate；`mark_report_sending` 与 `sending:` 前缀**保留但不再写**（只读兼容）；老路径保留在一个开关后面 | 环境变量 `INFE_PILOT_OUTBOX_SEND=0` → 循环停止领取，老路径恢复 |
| P3 退役 | 删掉 `SENDING_MARKER` 分支、`mark_report_sending`、`brief_sent_at`；加一条测试断言源码里不再出现 `sending:` 前缀 | 回滚需要 revert 该提交；此时 outbox 已是唯一路径 |

P1 的对账任务就是「可观测性」那一节的第一条指标，不做它就不要进 P2。

**已在 `unknown` 的行怎么回滚**：保持 `unknown`，不回退成 `pending`。运维在后台看到
「不确定投递」时，按 SMTP 日志判断，然后手工选择「确认已发（→sent）」或「重发（→pending）」。

## 7. 可观测性

后台「下发情况」与哨兵需要看三件：

1. **各状态条数**（`pending` / `claimed` / `sent` / `unknown` / `dead`）；
2. **最老的 `pending` 年龄**（积压）；**最老的 `claimed` 是否超过租约两倍**（循环卡住）；
3. **`unknown` 的数量与最近一条的时间**——它代表「真的要人看一眼」的那一档，
   与现有的失败计数分开显示（现有的「失败报告」口径见 `failed_reports_summary`）。

告警阈值建议：`unknown` 新增即告警（一天不超过几条）；最老 `pending` > 1 小时告警
（今天日报退避最坏是 2 小时，所以阈值要按这个量级定，不要按分钟）。

---

## 8. 已知残差（不要声称这让发送变成 exactly-once）

`send_report()` 返回成功与「对方真的收下」之间永远有一个窗口：SMTP 2xx 之后断网、
进程被杀、继电器拒收但应答丢失。outbox 把这个窗口从「散落的补丁」收敛成**一行 `unknown`**，
并保证它不会被自动重发；但它不能把窗口消灭。任何声称「outbox 之后不会重复发信」的说法都是错的。

---

## 9. 第一刀的验收口径（只接即时报告）

生产者：`process_message` 生成报告后，同一事务写一行 `kind='immediate'` 的 outbox。
消费者：worker 每轮领一行，按第 5 节时序发送。日报与 brief 保持现状（`send_daily` 与 006 的
`brief_sent_at` 不动），这样第一刀可以单独回滚。

必须有的测试（故障注入，全部在真实 SQLite 上）：

1. **SMTP 成功后记账失败** → outbox 行停在 `claimed` 或 `unknown`，**不会**变成 `pending`；
   下一轮（租约过期）接手后**不发送第二次**，最终 `unknown`。
2. **SMTP 明确失败** → `pending` 且 `available_at` 后退；业务侧报告 `failed`；下一轮再试能成功。
3. **两个消费者并发 claim 同一行** → 只有一个拿到（条件更新 + token），另一个空转。
4. **领取者中途死掉**（不调用 finish，只让租约过期）→ 接手者看 `smtp_started_at` 为空才发送，
   非空则转 `unknown`。
5. **重复入队** → 唯一键挡住，行数仍为 1。

验收：这 5 条全过，且全量套件失败集合与基线一致；把发送循环开关关掉（`INFE_PILOT_OUTBOX_SEND=0`）
后，老路径仍然全绿。

---

## 10. 开放问题（明确留待，不是遗漏）

- **brief 要不要进 outbox**：它今天靠 `messages.brief_sent_at`（006）。若进，`kind='brief'`
  的 outbox 行必须与报告生成同一事务，否则仍会出现「发了没记上」。建议 P3 一并处理。
- **日报的「空日报」**：007 之后空日报也会推进覆盖进度。outbox 化以后，「今天这封日报发了没有」
  由 outbox 行回答，`daily_report_exists()`（只认 `sent`）需要一起重定义。
- **发送循环放在哪个单元**：建议先放 worker 主循环（已经每轮租约、已有 `recover_inflight`），
  2 GB 机器上不要开第二个进程池；等积压真的成为问题时再拆。
