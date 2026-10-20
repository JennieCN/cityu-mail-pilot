# 坏 key 熔断（v0.63.4–0.63.5，2026-09-15）

> 欠账清单第 8 条：「坏 key 熔断（连续失败挂起该用户并在后台标红；自己写 50–80 行不引库，落库位置已定）」。

## 0. 一句话

一个用户把模型 key 填错之后，**每一封**给他生成报告的邮件都会白白占掉一个生成槽位，
而且他自己什么都不知道——现在连续三次「供应商明确拒绝这把 key」就暂停为他生成，
邮件**原样留在队列里**，后台点名说出是哪个账号、为什么、什么时候重试。

## 1. 先说它解决的问题有多具体

生产上真实发生过的是同一类：**能收信、能入库、生成全失败**。
`process_message` 的失败分支本来就会记 `attempts` 并按 `60 * 2^n`（上限 1 小时）退避，
所以它不会变成死循环。但退避解决的是**频率**，不是**槽位**：

- 每失败一次，`REPORT_WORKERS` 里就有一个槽位被花掉，而这一槽位**注定失败**；
- 每失败一次，用户那边就多一**条**「报告生成失败」；
- 用户不知道是自己的 key 坏了，运营者也不知道该去催谁。

真正的代价是**并发**：2 核 / 2 GB 的机器上，报告槽位是最贵的资源。（这一条是 recon
文档 §10.3 判断「重要程度 2/5」时给的理由的反面——它当时说「该故障从未发生过」，
所以缓做；把它做了是因为清单第 8 条已经定好了形状，且代价只有 50–80 行。）

## 2. 为什么自己写：没有一个 Python 熔断库值得进 `requirements.txt`

逐个查过（`docs/open-source-recon-2026-09-14.md` §4，表格在那边）：**没有一个 Python 熔断库
自带 SQLite / 文件级持久化**。唯一值得一提的 pybreaker 只有 memory 与 redis 两种内置存储；
`resilient-circuit` 只支持 PostgreSQL——为熔断装一个数据库显然是荒谬的。
本项目只有一把第三方运行期依赖（`cryptography`），为这个功能加第二把不划算。

借鉴的是 Hystrix 的三态机的**简化版**：本项目**每个用户的邮件本来就是串行处理的**
（`worker.process_due` 按用户分批），所以不需要「请求量窗口 / 错误率阈值」，
只需要「连续 N 次失败 → OPEN，窗口过后放一条」。

## 3. 落库：一张新表，不动 `connections`

```sql
CREATE TABLE IF NOT EXISTS key_circuits (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                     -- 'model' / 'search'
    failures INTEGER NOT NULL DEFAULT 0,
    open_until TEXT,                        -- NULL = 关着
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, kind)
);
```

recon 当时建议「给 `connections` 加三列，`last_error`/`last_test_at` 可以直接复用」。
**没有那样做**，理由是 `connections` 只在**用户自己配了 key** 时才有一行：

- 「生效的凭证」可能是**平台兜底 key**（用户从没配过），那时根本没有 `connections` 行，
  按 recon 的写法这一类账号**恰好漏掉**——而平台 key 坏掉正是最该熔断的情形（一次影响所有人）；
- 新表是 `CREATE TABLE IF NOT EXISTS`，**不需要 ALTER**，迁移风险为零
  （已经在**生产库的副本**上验过：新表建出来，`users/messages/reports/usage/mailboxes`
  计数一个没变）。

`uptime`/`last_error` 那些展示信息仍然从原处读，这张表只回答一个问题：**现在该不该为他生成**。

## 4. 只认「供应商明确拒绝」，这是整个设计的重心

关键决定：**什么算一次失败**。

第一版写的是「不是瞬时错误就算」——**真机跑一次就抓到它是错的**：

```
第 1 次: SecurityError — API 域名目前无法解析。
      失败计数 = 1
第 3 次: SecurityError — API 域名目前无法解析。
      失败计数 = 3，熔断 = True        ← 一次 DNS 抖动就锁了一个 key 好好的用户
```

`SecurityError` 来自出站 URL 校验闸门（`pilot_app/security.py`），它**会做 DNS 解析**，
所以一次解析失败就是一个 `SecurityError`：既不是 `ProviderError`、也不在瞬时清单里，
于是被「非瞬时就算」的规则冤枉了。同样的陷阱还等着**我们自己的 bug**：
`JSONDecodeError`、`KeyError('choices')`、响应结构变了——把它们记在用户的 key 上，
等于**因为我们的错去锁用户**。

所以规则改成**允许清单**，并且只有一处定义（`PilotService._blames_credential`）：

```python
isinstance(exc, providers.ProviderError) and not isinstance(exc, providers.TransientProviderError)
```

也就是「**远端明确回了一个不可重试的答复**」（HTTP 401/403/404/400、模型名不存在）。
其余一切（超时、429、5xx、断连、DNS、我们自己的解析错）**一次都不记**，
仍然交给队列原有的指数退避。这条不对称是有意的，而且方向是**宁可少熔断**：

> 冤枉一个 key 没问题的用户（他被静默停掉报告），比多花几个槽位严重得多。

反向验证过：把这个允许清单还原成「非瞬时就算」，`test_a_failure_that_is_not_the_credentials_fault_is_never_counted`
当场变红。

## 5. 邮件是「推迟」，不是「丢弃」

这是最容易被写错、也最不能写错的一条。熔断生效时：

- `due_messages()` 用一个 `NOT EXISTS` 子句**不再把这些邮件交给 worker**；
- 但那些行的 `status` 仍然是 `pending`，`attempts` 与 `last_error` **一个字段都没动**
  （真机上验过：`('pending', 0, '')`）。

所以「熔断」与「失败」在数据库里是两件不同的事。一个把邮件标成 failed 的熔断器，
会把一次凭证问题变成数据丢失。

## 6. 半开探测：窗口过期只放**一条**进去

`open_until` 过去之后，那个账号的邮件重新变成「到期」，于是拿到一次真实的探测机会。
但 `worker.process_due` 是**先取一批再逐条处理**的——如果只靠 SQL 过滤，
窗口一过就会把该用户的**整个积压**一次取出来，第一条失败后又把熔断重新打开，
后面几条**白花**。

所以取值处（`due_messages`）之外还有第二道闸门：`run_batch` 与顺序路径在**每条消息之前**
重新问一次 `key_circuit_open`，一旦发现已经重新打开就跳过剩下的。
这正是 recon 说的「半开探测必须真的花一次槽位」。

这道闸门写成了 `is True` 而不是真值判断，因为**它必须失败朝「照做」的方向**：
如果检查答不上来（测试里的替身、还没迁移的库），答案必须是「照常处理这封信」，
绝不能是「跳过这个人的邮件」——后者与这个功能要防的症状**一模一样**（邮件对着空气，且没有日志）。
名字就叫 `test_the_queue_gate_fails_open_when_the_check_cannot_answer`。

## 7. 换 key 立刻恢复

熔断是关于**某一把具体的凭证**的判断，凭证被换掉，判断的依据就消失了。
所以 `Database.upsert_connection()` 在**同一个事务里**清掉这张表——
放在那里而不是两个 HTTP handler 里，是因为自助修改与管理员代改**都走这一个方法**，
将来多一条保存路径也不会漏。

用户刚贴完新 key，不必再等 30 分钟窗口。

## 8. 后台看得见

`_service_health()` 多了 `suspended_accounts` 与 `suspended_detail`（账号、失败次数、
重试时间、原因），健康卡上写成一整句，例如：

> 有 1 个账号的模型 key 连续被拒绝，已暂停为它们生成报告（邮件都还在队列里，
> 换一把能用的 key 立刻恢复）：stalled@example.com（连续失败 3 次，9月15日 19:17:27 (GMT+8) 后重试）。

**顺手修的一处**：健康卡的状态行本来是 `if / else if` 链，**谁在前面谁把后面的顶掉**。
加了熔断这句之后，「有邮箱登不进去」就会被它静默盖住——运营者被告知了他没法处理的那件事，
而没被告知他能处理的那件。现在三条警告**并列**成一句话，`error` 只要有一条就赢。
浏览器断言里有一条专门盯这个：**两条警告同时在**。

## 9. 验收

| 项 | 结果 |
|---|---|
| 单元测试 | **1080 过**（新增 17 条：8 条表/队列行为、9 条分类） |
| 真库迁移 | 拿**生产库的副本**跑 `initialize()`：`key_circuits` 建出来，`users/messages/reports/token_usage/mailboxes` 计数完全不变，`due_messages()` 行为与升级前一致（当时 0 封到期） |
| 真机 · 真的坏 key | 用真的 DeepSeek 端点 + 故意写错的 key：3 次 `ProviderError`（HTTP 401）→ 熔断打开，日志点名账号；插入的 pending 邮件仍在库里且 `due_messages()` 不再返回它，状态 `('pending', 0, '')`；保存新 key 后立刻恢复且邮件重新出现 |
| 真机 · 真的网络故障 | 真的解析不了的域名：`SecurityError` → **计数不动、不熔断**（这一条第一版是红的，见 §4） |
| 真机 · 真的 5xx | `_json_request` 打真实 503：映射成 `TransientProviderError`，不计数 |
| 浏览器 | `admin_edit_check` 新增 4 条断言（说了什么、点名了谁、说清邮件没丢、两条警告同时在），全套 **18/18** |
| 反向验证 | 把允许清单还原成「非瞬时就算」，分类测试当场红 |

## 10. 有意没做

- **没有动 `users.status='paused'`**（recon 明确警告过）：那是管理员所有、不会自动过期的语义。
  把熔断写进去，「这个用户为什么被暂停」就无法回答，也无法自动恢复。
- **搜索 key 不熔断**：它不占生成槽位，且搜索失败是**可降级**的（没有联网核对也照样出报告）。
  表里留了 `kind`，需要时可以按同一条规则加，但现在没有这个需求。
- **没有给 `open_until` 做指数增长**：固定 30 分钟。原因是恢复路径比省这点探测更重要——
  用户换完 key 最多等 30 分钟就能自己好，而换 key 会立刻清零（§7）。
- **没接进告警哨兵**：运营者已经有健康卡这一处明确的红字，
  而告警一天 74 封那件事（v0.56.0）刚修过——再加一条会重蹈覆辙。真要加，
  应该并进现有的 T1/T2/T3 分档，那是另一件事。
