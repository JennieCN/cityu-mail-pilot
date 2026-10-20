# 决策文档：如果要做 App，怎样让 iOS 和安卓用户都能装上

> 生成日期：2026-09-14 · 基线 v0.18.0
> 问题：**若要做 App，iOS 与安卓用户各自怎样才能下载/安装？**
> 每一格都标注了取证状态：**[我核实]**＝我在本机亲自打开原文；**[研究员核实]**＝调研员打开并引用；
> **[未取证]**＝没打开，不给数字。

---

## 0. 结论摘要（先看这段）

**"两个平台都能下载"取决于"下载"指什么。有三种定义，可行性差别是天壤之别：**

| "下载"的定义 | iOS | 安卓 | 成本 | 能否今天做到 |
|---|---|---|---|---|
| **① 装到手机上、有图标、全屏运行** | ✅ 添加到主屏幕 | ✅ Chrome 安装（进应用抽屉） | **0 元** | **✅ 现在就能** |
| **② 从应用商店搜索下载** | ❌ **做不到**（4.2 拒绝网页壳） | ✅ 可做到 | $25 + 凑 12 人 14 天 | 需数周 |
| **③ 扫码/点链接直接装**（不上架） | ⚠️ 仅 Ad Hoc/TestFlight，有硬限制 | ✅ 直接下 APK | iOS $99/年 + Mac | 需数周 |

**一句话**：**安卓三条路都通；iOS 只有①通，②③要么做不到、要么有硬限制。**
如果接受①，"两个平台都能下载"**今天就已经实现了**——不需要写一行代码。

---

## 1. iOS：每条通道的真实状态

| 通道 | 费用 | 覆盖人数 | 硬限制 | 取证 |
|---|---|---|---|---|
| **A. 添加到主屏幕（PWA）** | **0** | 不限 | 用户需手动操作（分享→添加到主屏幕）；不是"从商店下载" | **[我核实]**：Apple 官方把 `standalone` 的 manifest 定义为 "Home Screen web app" |
| **B. Ad Hoc 分发** | $99/年 | **每年每设备类型上限 100 台** | 需要收集**每个用户的设备 UDID**；描述文件**每年失效**要重发；用户要手动信任证书 | [研究员核实]（Apple 设备注册页 JS 壳，正文我用 Playwright 也未取到） |
| **C. TestFlight** | $99/年 | 外部最多 10,000 | **构建 90 天过期**（Apple 原文 *"Your build becomes unavailable for testers after 90 days."*）；**外部测试仍需过一次审核**；需要 Mac + Xcode | [研究员核实，引官方原文] |
| **D. App Store 正式上架** | $99/年 | 不限 | **见下节，本产品形态基本不可能过** | **[我核实]** |
| **E. 企业版（In-House）** | $299/年 | 仅内部员工 | 要求**组织 100+ 员工 + D-U-N-S**；滥用会被吊销 | [研究员核实] |
| **F. EU 侧载 / Web Distribution** | — | 仅欧盟 | 需在欧盟设立实体、**100 万美元备用信用证**等；**香港不适用** | [研究员核实] |

### 1.1 为什么 App Store 这条路对本产品是封死的（**我亲自核实原文**）

**第一道坎：App Review Guidelines 4.2 Minimum Functionality。**
我打开了 `developer.apple.com/distribute/app-review/`，在 "Avoiding common issues" 一节里，Apple 把这种形态**单独列为常见拒绝原因**：

> **Web clippings, content aggregators, or a collection of links**
> "Your app should be engaging and useful, and make the most of the features unique to iOS.
> **Websites served in an iOS app, web content that is not formatted for iOS, and limited web interactions do not make a quality app.**"

同一页另一条：

> **Not enough lasting value**
> "If your app doesn't offer much functionality or content, **or only applies to a small niche market, it may not be approved.**"

**第二道坎（比 4.2 更难过）：Guideline 2.1 App Completeness。**
同一页原文：

> "If some features require signing in, **provide a valid demo account username and password.**"
> "On average, over 40% of unresolved issues are related to guideline 2.1."

本项目登录后连的是**真实学校邮箱的只读 IMAP**。给审核员 demo 账号 ＝ **让外人读一个真实邮箱**；不给 ＝ 卡 2.1。这在现有架构下无法两头兼顾。

**第三：加推送也救不了。** 调研员找到一封完整的一手拒信（Bubble 论坛，帖主用 webview 包装提交；**安卓版被 Play 无问题接受，iOS 被拒**），Apple 明确写：

> "Including iOS features such as **push notifications**, Core Location, and sharing do not provide a robust enough experience..."
> "...**with tacked-on feature, push notifications, does not bring the app into compliance**..."

而"能做推送"恰好是本项目唯一可能让外壳"原生化"的理由。

### 1.2 iOS 唯一可能的出路：做成**真正有原生功能**的 App

Apple 4.2 没有写"加原生功能即可豁免"，但这是实践上唯一的过审路径：App 必须有**不依赖网页也成立的价值**。对本产品可行的候选：

- 离线阅读/搜索历史报告（本地缓存）
- 系统级分享、Widget 显示最新报告
- 原生通知（Apple 明说**单独加推送不够**）

代价：一门新的技术栈（Swift/SwiftUI）+ 一台 Mac + 每次 iOS 大版本适配 + $99/年 + 2.1 的 demo 账号问题依然存在。
**这不是"包装一下"，是新增一条产品线。**

---

## 2. 安卓：三条路都通

| 通道 | 费用 | 门槛 | 覆盖 | 取证 |
|---|---|---|---|---|
| **A. PWA 安装**（Chrome 自带） | **0** | **无** | 全部 | **[我核实]**：我们的 `manifest.webmanifest` 满足全部可安装条件 |
| **B. 官网直接下 APK** | 0 | 用户会看到"未知来源"+ Play Protect 警告 | 全部 | [研究员核实] |
| **C. Play 免费 Limited Distribution 账号** | **0** | 官方给**学生/爱好者**、**≤20 台设备**、免政府证件 | 小范围刚好够 | [研究员核实，`developer.android.com/developer-verification`] |
| **D. Play 正式上架** | $25 一次性 | **12 名测试者连续 opt-in 14 天**，中途掉人计时重置 | 全部 | **[我核实，见下]** |
| **E. 第三方商店**（Amazon/三星/F-Droid/Obtainium） | 0–少量 | 各自有账号与内容要求 | 各自生态 | [未取证] |

### 2.1 我们对 A 路的核实（这一步等于"已经能做 App"）

我逐条比对了 `pilot_app/static/manifest.webmanifest` 与 PWA 可安装条件：

| 条件 | 我们的值 | 满足 |
|---|---|---|
| `name` / `short_name` | CityU Mail Pilot / Mail Pilot | ✅ |
| `start_url` | `/` | ✅ |
| `display` | `standalone` | ✅ |
| 192px + 512px PNG 图标 | `/icon-192.png`（3.4 KB）、`/icon-512.png`（8.7 KB），文件都在且被服务 | ✅ |
| `prefer_related_applications` | 未设置 | ✅ |
| HTTPS | 已有（Let's Encrypt） | ✅ |

**⇒ Chrome 已经在提供"安装"入口。** 装出来是 **WebAPK**：出现在应用抽屉、应用切换器、可加桌面图标，**体感与原生一致**。零代码、零费用、不需要任何开发者账号。

### 2.2 Play 的 12 人门槛（**我用 Playwright 渲染官方页核实**）

Google Play 帮助页（`support.google.com/a/answer/14151465`，现跳转到 `knowledge.workspace.google.com`）规定：**2023-11-13 之后注册的个人开发者账号**，必须跑封闭测试——**12 名测试者、连续 14 天保持 opt-in**（2024-12 由 20 人下调）；中途有人退出导致低于 12 人，**计时重置**。组织/企业账号或 2023-11-13 前注册的个人账号豁免，但组织账号需要 D-U-N-S 编号。

**对现在 2 个用户、一个人的规模：这是硬阻断，不是"麻烦一点"。**

---

## 3. 一个代码库覆盖两端的技术路径

| 工具 | License | Star | 产出 | iOS |
|---|---|---|---|---|
| **bubblewrap**（Google） | Apache-2.0 | 3,089 | 已签名 APK/AAB | ❌ 完全不碰 iOS |
| **Capacitor**（Ionic） | MIT | 16,657 | Android + **Xcode 工程** | ⚠️ 只给工程，需 Mac 上自己 Archive |
| **PWABuilder**（微软） | MIT | 3,758 | Android + Xcode 工程 ZIP | ⚠️ 同上，官方标注 Experimental |

**关键事实：三个工具没有任何一个直接产出可提交的 IPA。** 涉及 iOS 的都要 Mac + Xcode。

**TWA 的额外成本**：域名根要放 `/.well-known/assetlinks.json` 做双向验证，否则顶部会留着地址栏。HTTPS 已有，加一个静态 JSON 即可。

---

## 4. 各方案成本汇总

| 方案 | 一次性工作量 | 现金 | 覆盖 | 长期负担 |
|---|---|---|---|---|
| **PWA 双端（现状 + 一句安装引导）** | **~1 小时** | **0** | iOS + 安卓全部 | 无 |
| 安卓 APK 直下 / Limited Distribution | 半天 | 0 | 安卓全部 | 重新签名、Play Protect 误报处理 |
| 安卓 Play 正式上架 | 数天 + 数周等待 | $25 | 安卓全部 | 年度 target API 升级、下架风险 |
| iOS TestFlight | 数天（需 Mac） | $99/年 | ≤10,000，**但 90 天过期** | 每 90 天重新构建分发 |
| iOS Ad Hoc | 数天（需 Mac） | $99/年 | **≤100 台/年** | 每年重签、收集 UDID |
| **iOS 上架（真原生化）** | **数周起，新代码线** | $99/年 + Mac | 全部 | Swift 适配、2.1 demo 账号问题 |

---

## 5. 不管选哪条，都先要解决的三件事

这三条与"做不做 App"无关，但**决定能不能给别人用**：

1. **合规文书（不做就是违法）**
   PDPO DPP1(3)(b)(i)(B) 明文要求收集时告知**资料会转移给哪些类别的人**——这就是"把邮件正文发给第三方大模型"的合规落点，必须在注册那一刻点名（DeepSeek），藏在隐私政策深处不算。第 26 条删除义务是**刑事条款**（最高罚 HK$1 万）。且 DeepSeek 自己的 ToS §3.3 也要求我们向终端用户披露。
   → 现在**全仓库没有任何隐私政策、服务条款、删除通道**。

2. **身份锚点：邮箱验证 + 找回密码 + 对外通告渠道**
   没有验证过的邮箱 ＝ 无法可靠联系任何用户。GDPR 要求 72 小时内通知事故；执行删除也要先确认身份。

3. **载荷护栏（加用户之前，不是之后）**
   **Gmail 官方原文（我用 Playwright 渲染核实）**：
   > "**When the limit is reached, the account is temporarily suspended.**" / "suspension typically lasts an hour, but can last up to **24 hours**"
   > "set it up to check for new messages less frequently. **We recommend once every 15 minutes.**"

   我们 `POLL_SECONDS=60`，**比官方建议频繁 15 倍**。目前没出事是运气。其余：SQLite 单写者、`http.server` 官方标注 "not recommended for production"、`/health` 会在进程卡死时仍返回 200。

4. **Gmail 这条腿的"许可证"不在我们手上**
   我们现在走 **IMAP + 应用专用密码**，**当前不需要 CASA**（第二位 Gmail 用户实测可用即是证据）。但：
   - Google 官方原文（**[我核实，Playwright 渲染]**）："Apps that access restricted scopes are required to complete a security assessment **every 12 months**."
   - 一旦改为 OAuth（或应用专用密码被关），**只读 IMAP 也不豁免**；机构报价 **$675–$8,000+/年**，且**通过验证前项目有 100 个用户终生上限**——低于任何"扩大用户"的目标
   - 已有独立开发者因这条直接关停（Skyler，2025-12）
   → **在对外宣称"支持任意邮箱"之前，先决定 Gmail 用户的去留。**

5. **成本结构会随用户数恶化（Newton Mail 的死因）**
   Newton Mail 团队 2026 年重建公告亲口复盘第三次死亡：
   > "that backend got more expensive the more people used it. **So the better the app did, the more it cost to keep the lights on.** That's a bad shape for a business, and eventually the numbers caught up with us."

   我们现在是**固定成本单机 + BYOK（模型费用户自己付）**，短期安全。**但一旦为了实时性去加常驻连接或服务端缓存，就是走 Newton 的老路**——这也是"不要为了延迟改客户端"的又一条理由。

---

## 6. 我的建议

### 如果"两个平台都能下载"是硬需求

**做 A（PWA 双端），不做 App。** 它今天就已经成立：安卓进应用抽屉、iOS 加到主屏幕，零成本、无审批、无过期、无年度续费。需要补的只是**一句安装引导**（在页面上告诉用户"菜单 → 安装应用 / 添加到主屏幕"）。

### 如果一定要"从应用商店下载"的形态

**安卓可行，iOS 不可行**——除非把 App 做成真有原生价值的东西（离线缓存、Widget、分享），那是**新增一条产品线**，不是包装。

**折中方案（我推荐这个，如果你要"能装 App"的实感）**：
- **安卓**：bubblewrap 出 TWA，走 Google 的**免费 Limited Distribution 账号（≤20 设备）**，绕开 12 人门槛
- **iOS**：保持"添加到主屏幕"，**不要注册 Apple 开发者账号**
- 两端用**同一份网页**，不新增代码线

### 顺序建议

1. **先做第 5 节的三件事**（合规 + 身份 + 护栏）——不做这些，给陌生人用是拿别人的邮箱冒险
2. **再做 PWA 安装引导**（1 小时，立刻让"两个平台都能装"成真）
3. 等真有人用了、而且明确说"我要一个 App"，再评估安卓 TWA
4. **iOS 上架留到最后**，且要先想清楚 2.1 的 demo 账号怎么解决

---

## 6.5 补充（2026-09-14 晚）：如果走"自托管分发"而不是 App

"给别人下载"还有第四条路：**别人下载源码/脚本，装到自己的服务器上**。这条路上有一条必须先看的先例。

### Sandstorm：目标和我们最像、而且失败了的项目

| 项 | 值 |
|---|---|
| License | **Apache-2.0**（**[我核实]** `LICENSE` 第一行原文："Sandstorm is licensed under the Apache License, version 2.0."） |
| Star | 7,078 |
| 目标 | **"像手机装 App 一样装自托管应用"** —— 与本项目"让别人也能用上"的意图高度重合 |
| 结局 | 作者 Kenton Varda 2024-01-14 官方回顾自述：**"Over time, even just basic maintenance became difficult"**、**"In early 2023, I gave up pushing monthly releases"**、称它是 **"the project that felt like a failure"** |

**失败原因是维护人力，不是技术。** 一个 7,000+ 星、Apache-2.0、目标就是"让自托管像装 App 一样简单"的项目，死在没人维护上。

**两条红线：**

1. **不要承诺"装了就会自动升级"。** 升级必须由装机的人用 `--upgrade` 自己触发；我们只保证"不碰 `pilot.env` 与数据库 + 升级前自动备份"。这与 Kuma（"你自己再跑一遍命令"）、n8n（`--upgrade` 只改版本号）的契约一致。
2. **必须能知道"谁装了"。** 现在 2 个用户可以靠 `OnFailure=` + 巡检哨兵覆盖。一旦陌生人装到**作者不知道的机器**上，"出事有人兜"这条就断了——而 PikaPods / Elestio / Cloudron 赚的正是这笔钱。**所以放开安装前必须有明确的"哪些机器在谁的告警范围内"名单；否则宁可先只给能一起看日志的 1–2 个同学。**

### 量化："一键部署按钮"的真实成材率

Railway 模板页自带统计（[研究员核实页面]）：

| 模板 | 总项目 | 仍活跃 | 比例 | 有无持久卷声明 |
|---|---|---|---|---|
| Uptime Kuma（第三方作者） | 1,451 | 672 | **46%** | ❌ 依赖里没有持久卷 |
| PocketBase（2026-06） | 5 | 5 | 100% | ✅ 明确声明 volume 并写明"部署后自己访问 `/_/` 建管理员" |

**约一半的人点完按钮就不再用。** 负责任的按钮与不负责任的按钮，差别就是**有没有声明持久卷 + 有没有写清部署后第一步**。

### 最小可行的一步（如果要做自托管分发）

`deploy_pilot.sh` **已经是 80% 的通用安装器**（[研究员逐行核实]：已自动生成主密钥、建用户与目录、装 5 个 systemd 单元、挂 `OnFailure=`，且第 39 行 `if [[ ! -f pilot.env ]]` 已实现"重跑不覆盖配置与密钥"；`rm -rf pilot_app` + 重拷本身就是升级机制）。

**它唯一没做的是"让服务可用"**：装完停在 `127.0.0.1:8787` + 占位域名 `mail.example.com` + `COOKIE_SECURE=1`。全仓库 grep 确认：**没有脚本调用过 certbot，`nginx-cityu-mail-pilot.conf.example` 从未被安装到 `/etc/nginx`。**

要补的（一个 PR 大小）：`--origin/--dry-run/--upgrade/--uninstall` 参数、preflight 检查、渲染并 enable 现成的 nginx 示例、跑 certbot、以及一份**恢复演练文档**（全仓库现在 grep 不到任何恢复流程）。

**为什么这条路便宜**：vaultwarden 与我们的约束一模一样（没 HTTPS 就不能用）却只能维护 27 KB 的 wiki，因为它要支持任意发行版/反代/拓扑。**我们的目标形态是固定的（Ubuntu + nginx + certbot + 单机），所以几十行 shell 就能做到比它更好的安装体验。** 生产环境已经在用 `sslip.io` 免域名方案，仓库里有验证记录——默认值不该是"让用户买域名"，而是"你什么都不用给，我用你的公网 IP 生成一个能签证书的主机名"。

**不要引入 Docker**：理由不是"重"（`python:3.14-slim` 实测仅 44 MiB，我们镜像大概 50–60 MiB），而是——生产机只有 2 vCPU / 2 GB；Docker **让升级更简单但没有让回滚更简单**（Immich 明说不支持降级）；以及 NFS + SQLite 会损坏（uptime-kuma 两次警告）。对我们"1 个直接依赖、本来没有环境漂移"的项目，Docker 是把"1 个 Python 依赖"换成"一整个操作系统发行版"。

---

## 7. 出处

| 结论 | 来源 | 取证 |
|---|---|---|
| Apple 4.2 拒绝网页壳 + "Not enough lasting value" | `developer.apple.com/distribute/app-review/` | **[我核实]** |
| Apple 2.1 需提供 demo 账号 | 同上 | **[我核实]** |
| Gmail IMAP 建议 15 分钟 + 超限封停 1–24h | `knowledge.workspace.google.com/admin/gmail/gmail-server-request-limits` | **[我核实，Playwright 渲染]** |
| 我们 manifest 满足 PWA 安装条件 | 本机 `pilot_app/static/manifest.webmanifest` + 图标文件 | **[我核实]** |
| Play 12 人 × 14 天 | `support.google.com/googleplay/android-developer/answer/14151465` | **[我核实，Playwright 渲染]** |
| TestFlight 90 天过期 | Apple 官方，由调研员引用 | [研究员核实] |
| 各打包工具 License / Star / 产出 | GitHub API + LICENSE 文件 | [研究员核实] |
| iOS 一手拒信原文 | Bubble 论坛帖 | [研究员核实] |
| Google Play 免费 Limited Distribution（≤20 设备） | `developer.android.com/developer-verification` | [研究员核实，我未打开] |
| Ad Hoc 100 台/年 | Apple 文档 | [未取证] |
| 第三方安卓商店 | — | [未取证] |
| Google 强制年度安全评估（restricted scopes） | `support.google.com/cloud/answer/13465431`、`13463816` | **[我核实，Playwright 渲染]** |
| 100 用户终生上限 | `support.google.com/cloud/answer/13463816` | [研究员核实，我未取到该行] |
| Newton Mail 成本结构复盘 | `newtonhq.com/blogs/the-calm-inbox-returns-newton-in-2026` | [研究员核实] |
| Sandstorm 许可证、Umbrel 许可证 | 各自仓库的 `LICENSE` / `LICENSE.md` 正文 | **[我核实]** |
| Sandstorm 作者自述停更 | 作者 2024-01-14 回顾 | [研究员核实] |
| Railway 模板成材率 46% / 100% | Railway 模板页自带统计 | [研究员核实] |
| `deploy_pilot.sh` 已完成 80% | 本机 `pilot_app/deploy_pilot.sh` 全文 | **[我核实]** |
