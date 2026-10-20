# 报告的详细程度，交给用户自己选（v0.63.28）

> 用户原话：**「要不要精简报告的权利给用户选」**。
> 结论：**给**。而且是**每个用户可切、默认「跟随站点」**——也就是上线当天，谁的邮件都没变。

## 1. 为什么是「默认跟随站点」，而不是选一个更好的默认

这件事有两种做法，差别不在功能而在**切换的那一刻**：

| 做法 | 上线当天会发生什么 |
|---|---|
| 把站点默认改成完整版，用户可切回精简 | **7 个真实用户第二天收到的邮件全变了**（变长、变贵、格式不同） |
| 保持站点默认，把选择权交给用户（**选了这条**） | 没有任何人的邮件改变；想要完整版的人自己去点一下 |

第二种做法把「改别人的东西」从**部署的动作**变成了**用户自己的动作**。
这跟项目里已有的两条规矩是同一条：`/api/profile` 不许被顺手覆盖（§4.4）、
真实用户的操作先说明再动手（§8）。**上线是运维动作，不该改产品行为。**

`report_mode` 用 `''` 表示「跟随站点」，**不是**用某种「未设置」的技巧：
空串是一个**真的、可读的、可回退的**选择（UI 上就写着「跟随站点（现在是：精简）」），
而不是「没填」。用户想回到默认，选它就行。

## 2. 三个值，不是一个开关

```
report_mode = ''      跟随站点（默认）—— 站点此刻由 BRIEF_FIRST / FULL_REPORT 决定
            = 'brief' 只要三段精简版
            = 'full'  只要七段完整版
```

- **站点级**仍是 `BRIEF_FIRST` / `FULL_REPORT`（`INFE_PILOT_BRIEF_FIRST` / `INFE_PILOT_FULL_REPORT`），
  站点级切换就是改这两个环境变量再重启（本实例上还包了一层运营者脚本，它**不在仓库里**，
  所以自建部署的人直接改环境变量就是同一条路）。
  `service.instance_report_mode()` 是**唯一**把这两个布尔值
  翻译成 `'brief' | 'full' | 'both'` 的地方，UI 与 `/api/me` 都用它——**站点模式只有一处定义**。
- **用户级**只在 `''` 时才读站点值。一旦用户显式选了，`want_brief` / `want_full` 就只由那个选择决定。

```python
chosen = str((profile or {}).get("report_mode") or "").strip()
want_brief = chosen == "brief" or (not chosen and BRIEF_FIRST)
want_full  = chosen == "full"  or (not chosen and FULL_REPORT)
```

**刻意不开放的那一档**：「精简 + 完整两封」（站点的 `brief-and-full`）。
它是**站点实验**用的形状（一封信先到你手上、完整版随后），不是一种阅读偏好；
给用户这个选项等于让他们选择「每天收两倍的邮件」。站点实验保留，用户级不提供。

## 3. 写在哪、怎么防呆

- 数据库：`profiles.report_mode TEXT NOT NULL DEFAULT ''`，`initialize()` 的增量 `ALTER TABLE` 列表里有一份
  （老库升级不会丢数据）；`upsert_profile` 的白名单里加了 `report_mode`。
- 接口：**另开** `PUT /api/reports/mode`（`REPORT_MODE_PATH`），**不走 `/api/profile`**——后者会用默认值
  覆盖所有字段（铁律 §4.4），用它改一项会把别的项抹掉。只接受 `''` / `'brief'` / `'full'`，其余一律 **422**；
  没会话 **401**。
- `GET /api/me` 多回一个 `report_mode_default`（= `instance_report_mode()`），UI 拿它写「现在是：精简」。
- UI：「报告与账户」里的一个**折叠**面板（`#panel-report-mode`）。折叠是有意的：这是一个设置，
  不是每次都要看的报告。改了没保存会在旁边写「（未保存）」——**照抄项目里已有的刷新反馈规矩**：
  控件要说自己处于什么状态。

## 4. 成本要写清楚，因为这是唯一的代价

精简版 3 段、完整版 7 段（多出「与我的关系 / 联网搜索后的建议与来源 / 风险未知与推测 / English summary」）。
实际测过的数字（生产 deepseek-flash，`pricing.DEFAULT_PRICES`，含峰值倍率）：

| | 输入 tokens | 输出 tokens | 单封估算 |
|---|---|---|---|
| 精简版 | 2057（11 次调用合计） | 273 | ≈ $0.00047 |
| 完整版 | 1850 | 676 | ≈ $0.00068 |

**完整版 ≈ 精简版的 1.45 倍**，每封多 ~$0.0002（≈ ¥0.0015）。7 人 × 3 封/天 × 30 天：
精简 ≈ $0.30/月，完整 ≈ $0.43/月。**UI 里如实写这个倍数**，不写「几乎不花钱」。

这条也决定了它的产品定位：**这是阅读偏好，不是省钱开关**。想省钱的做法是别开完整版，
而不是「开了完整版然后指望它便宜」。

## 5. 测试（1284 全过）

- `test_appearance.ReportModeTests`（8 条）：默认是 `''`；存 `full` 后用**另一个 cookie jar** 读回来还是 `full`
  （证明落了库、不是只回显）；能改回 `''`；`verbose` / `../../etc` / **`both`** 都 422；
  改这一项**不动** profile 里别的字段；没会话 401；`instance_report_mode()` 全项目只有一处定义；
  面板标记存在；**老库（没有这一列）能迁移**。
- `test_triage.PerUserReportModeTests`：选 `full` → 只发 1 封完整版；选 `brief` → 只发 1 封精简版；
  `''` → 跟随站点（站点精简发 1 封、站点两段发 2 封）；**显式选择永远不是两段式**。
- `test_web.WebServiceNameTests`：`web.service` 不是模块、且源码里不许出现裸 `from . import service`（见 §6）。
- `tools/refresh_feedback_check.js` +8 条：面板存在、默认 `''`、说明文字写着「跟随站点」与「现在是」、
  改动后出现「未保存」、保存后落库、说明跟着更新、能改回 `''`。

### 一个测试自己的坑（值得记下来）

`ReportModeTests` 第一版**每个测试注册一个账号**。这个仓库的测试**共用一个库**，而内测名额上限是 50——
8 个测试当场把名额吃掉一块，**别的模块开始报「当前试点名额已满」**，看起来像注册坏了。
改成**一个类共用一个账号、每个测试用 API 把自己那一项重置回 `''`**。
**廉价测试用掉稀缺资源就不廉价。**

## 6. 顺手修掉的一个真 bug：`web.service` 被模块遮蔽

新端点要调 `service.instance_report_mode()`，第一版写的是 `from . import service`——
而 `web.py` 里那个名字**同时**是「子模块」和「懒加载的单例」（文件末尾的模块级 `__getattr__`
就是为了让 `from pilot_app.web import db, service` 拿到单例）。这一行 import 把单例**遮蔽成模块**，
于是 `service.secrets` 之类的调用在**别处**炸开：17 个 error + 7 个 failure 出现在
`test_tasks` / `test_compliance` / `test_multiuser_scope`，**没有一条提 `web.py`**。
改成 `from . import service as service_mod`，并加 `test_web.WebServiceNameTests` 钉住
（`web.service` 必须是带 `process_message` 的东西，源码里不许再出现裸 `from . import service`）。
**同一个名字不能有两个含义**——这条在 §4.1 已经写过一次（`/api/me` 剥掉裸 `is_admin`），这次是它换了个地方再犯。

## 7. 上线验收

- 部署前备份数据库 + `pilot.env`；部署 v0.63.28；六个单元 active、`/health` 200、匿名边界 401。
- **必须真查一遍「谁的邮件都没变」**：生产库里 `SELECT report_mode, COUNT(*) FROM profiles GROUP BY report_mode`
  应当只有一行 `''`（迁移默认值）——这才是「默认跟随站点」这句话的证据，不是「代码里写着 `DEFAULT ''`」。
- 再按一次真功能：拿一个真实账号把 `report_mode` 存成 `full`、用另一个 cookie jar 读回来、再改回 `''`。
  **改完必须还原**——真实用户不该因为验收而换格式。
