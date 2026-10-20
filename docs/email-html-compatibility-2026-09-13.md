# 2026 手写高兼容 HTML 邮件技术简报

**场景**：向大学生发送事务型通知邮件（AI 摘要报告）。
**目标客户端**：Outlook (Windows / Word 渲染器)、Gmail (Web/iOS/Android)、Apple Mail (macOS/iOS)、移动端。
**调研日期**：2026-09-13。所有数据来源见文末，仓库文本一律按**不可信数据**处理（未执行任何下载代码）。

---

## 0. 一句话结论

> **单列 `<table role="presentation">` + 全量 inline style + `<head>` 内 `<style>` 只做渐进增强 + 保留 `<!--[if mso]>` 条件注释。**
> 不要用 flex / grid / position / float / CSS 变量 / 远程字体做**结构性**依赖；不要依赖 `<style>`、`@media` 或 `background-image` 承担关键信息。

---

## 1. 逐项目评估

### 1.1 resend/react-email

| 项 | 值 |
|---|---|
| URL | https://github.com/resend/react-email |
| License | **MIT**（GitHub API `license.spdx_id == MIT`，文件 `LICENSE.md`） |
| 维护信号 | 19,732 stars；`pushed_at` **2026-09-09**；34 open issues；未归档 → **活跃维护** |
| 可复用 | 其**兼容性规则文本 + 组件结构约定**（非代码） |

它实际写下的兼容性约束（来自仓库内 `skills/react-email/references/STYLING.md` 与 `skills/react-email/SKILL.md`，按不可信数据处理后摘录）：

- *"Never use flexbox or grid — use `Row`/`Column` components or tables for layouts."*（SKILL.md）
- *"Flexbox/Grid - Use `Row`/`Column` components or tables"*（STYLING.md）
- *"Media queries - `sm:`, `md:`, `lg:`, `xl:` prefixes don't work"*；*"Theme selectors - `dark:`, `light:` prefixes don't work"*
- *"SVG/WEBP images - Use PNG or JPEG only"*
- *"rem units - Use `pixelBasedPreset`"*
- *"Inline styles as fallback - Some clients strip `<style>` tags"*（STYLING.md best practices）
- *"Keep file size under 102KB - Gmail clips larger emails"*
- 布局组件（`<Section>`/`<Row>`/`<Container>`/`<Markdown>` 表格）默认渲染为 `<table role="presentation">`；手写裸 `<table>` 需自己加 `role="presentation"`（SKILL.md / COMPONENTS.md）
- `apps/docs/components/html.mdx`：*"Some email clients strip the `html` or `body` tags, so it's necessary to have both."* → `lang`/`dir` 要同时写在 `<html>` 和 `<body>` 上。
- `apps/docs/components/image.mdx`：*"All email clients can display `.png`, `.gif`, and `.jpg` images. Unfortunately, `.svg` images are not well supported."*

**关于原提问中的两点，需要更正：**

- **"no remote fonts" 不准确。** react-email **有** `<Font>` 组件且支持 `webFont={{url, format}}`。它的立场是 *"not all email clients supports web fonts, this is why it is important to configure your `fallbackFontFamily`"*（`apps/docs/components/font.mdx`）。即：远程字体是**可选增强**，不是禁止项——但必须给 fallback。**本项目建议采纳更严格的做法**（见 §3），因为 AI 摘要报告是纯文本可读性优先。
- **"no `<details>`" 在 react-email 仓库中查无实据。** 我 grep 了 `SKILL.md` / `STYLING.md` / `PATTERNS.md` / `COMPONENTS.md` 全文，**没有任何**关于 `<details>` 的表述；react-email 组件清单里也没有 `<Details>` 组件。`<details>` 的实际证据来自别处（见 §1.3 与 §3），**不要归因给 react-email**。

**拒绝复用的部分**：React / JSX 运行时、`@react-email/tailwind`。原因是本项目要"手写 HTML"，引入 React 渲染链会在"HTML 是否原样送达"上再加一层不确定性（且 Tailwind 的 `rem`/media query 输出需要额外处理）。仅把它的规则当**校验清单**用。

---

### 1.2 leemunroe/responsive-html-email-template

| 项 | 值 |
|---|---|
| URL | https://github.com/leemunroe/responsive-html-email-template |
| License | **MIT** ✅ 已核实。文件路径为 **`license.txt`（小写）**，非 `LICENSE`——直接用 `raw.../LICENSE` 会 404，我是通过 GitHub `/license` API 取到正文的：`Copyright (c) [2013] [Lee Munroe]` |
| 维护信号 | 13,696 stars / 4,293 forks；`pushed_at` **2024-08-20**；未归档；10 open issues → **近两年无提交，事实静止但未废弃** |
| 默认分支 | `master` |

仓库极简，只有 4 个文件：`email.html`（带 `<style>`）、`email-inlined.html`、`license.txt`、`readme.md`。

**它的单列响应式表格模式（`email.html` 实测结构）：**

```
body
└─ table.body[role=presentation][border=0][cellpadding=0][cellspacing=0]   ← 外层 100%
   └─ tr
      ├─ td                          ← 左侧 &nbsp; 撑出居中留白
      ├─ td.container                ← max-width:600px; width:600px; margin:0 auto
      │  └─ div.content
      │     ├─ span.preheader        ← 隐藏预览文案，带 mso-hide:all
      │     └─ table.main[role=presentation]   ← 白底卡片
      │        └─ tr > td.wrapper    ← padding:24px
      │           ├─ p …
      │           └─ table.btn … td > a   ← 嵌套表格做按钮（可点区域）
      └─ td                          ← 右侧 &nbsp;
```

关键点：**用左右两个 `&nbsp;` 空单元格做居中**，而不是 `margin:auto`；`max-width` 与 `width` 双写；按钮是**嵌套 table**而不是 `<button>`；`class` 全部只服务 `@media` 增强。

**可复用**：这个多列表格 + `td` 留白的居中骨架、`preheader` 隐藏技巧、嵌套表格按钮、`role="presentation"`。
**拒绝复用**：`border-radius:16px`、`border-collapse:separate`（该模板自身用的是 separate，但 Outlook 上 `border-collapse` 是少数 **y** 的属性，见 §2，改成 `collapse` 更稳）；`.btn-primary table td:hover` 之类的 `:hover` 增强（移动端无 hover）；README 里"tested on all major email clients"的截图**是营销声明、无日期、不可作为证据**。
**注意**：模板里 `@media` 断点写的是 `max-width: 640px` 而容器是 `600px`——留了 40px 冗余，这个细节值得照抄。

---

### 1.3 hteumeuleu/email-bugs 与 caniemail

这是两个不同项目，必须分开说。

#### (a) hteumeuleu/email-bugs

| 项 | 值 |
|---|---|
| URL | https://github.com/hteumeuleu/email-bugs |
| License | **无 LICENSE**（GitHub API `license: None`）→ **视为未授权复用代码**；且仓库内**只有 `README.md` + `.github/`，没有任何代码** |
| 维护信号 | 575 stars；`pushed_at` **2023-08-16**；**112 open issues**；未归档 → **实质停滞** |
| 性质 | 它是一个 **issue tracker**（bug 报告库），不是文档集或代码库 |

**可复用**：只能作为**事实引用来源**（issue 编号可引用），不能复用任何代码（没有代码，也没有许可证）。
**拒绝复用**：任何代码（不存在）；以及不要把它当作"权威规范"——它是社区 bug 汇总，条目老旧（多数 issue 停在 2020 前后）。

#### (b) hteumeuleu/caniemail ← **本项目的主要事实依据**

| 项 | 值 |
|---|---|
| URL | https://github.com/hteumeuleu/caniemail ；站点 https://www.caniemail.com |
| License | **MIT** ✅（`LICENSE`：`Copyright (c) 2019 Rémi Parmentier`） |
| 维护信号 | 942 stars；`pushed_at` **2026-08-10**；未归档 → **活跃** |
| 数据新鲜度 | `https://www.caniemail.com/api/data.json` 的 `last_update_date = 2026-08-10`，`api_version 1.0.4`，共 **308** 个特性条目 |

**强烈建议直接消费 `api/data.json`**（MIT，可程序化）——这是唯一可自动化、可版本化的兼容性数据源。

---

### 1.4 postalsys/email-templates / Postmark

**⚠️ 更正：`github.com/postalsys/email-templates` 不存在（404）。** 我核实了 `postalsys` 组织下的相关仓库：

| 仓库 | License | Stars | Last push | 实质 |
|---|---|---|---|---|
| [postalsys/templates](https://github.com/postalsys/templates) | `package.json` 声明 **ISC**；但 GitHub API 报 `license: None`（仓库根目录**无 LICENSE 文件**）→ **许可状态不一致，谨慎** | 0 | 2026-08-12 | 是 EmailEngine 的**模板语言**（Handlebars + marked + moment），`lib/templates.js`。**与 HTML 邮件排版无关** |
| [postalsys/email-guide](https://github.com/postalsys/email-guide) | **无 LICENSE** | 4 | 2024-01-03 | Docusaurus 文档站，内容是 SPF/DKIM/DMARC、SMTP、投递——**不涉及 HTML/CSS 兼容性** |
| [postalsys/emailengine](https://github.com/postalsys/emailengine) | NOASSERTION | 2,227 | 2026-09-12 | IMAP/SMTP 网关服务，非模板 |

**结论：postalsys 这条线没有可复用的 HTML 邮件排版先例。** 找不到就是找不到。它唯一的价值是把"模板渲染"和"HTML 兼容性"这两件事解耦（Handlebars 渲染 → 再 inline → 再发送），这个**架构分层**可以借鉴。

**真正的 Postmark 参考是 [ActiveCampaign/postmark-templates](https://github.com/ActiveCampaign/postmark-templates)：**

| 项 | 值 |
|---|---|
| License | **MIT** ✅（`LICENSE`：`Copyright (c) 2015 Wildbit`） |
| 维护信号 | 3,194 stars；`pushed_at` **2023-04-02**；我 shallow clone 后 `git log -1` = **`2022-08-25`（PR #29）** → **已静止约 4 年，但未归档** |
| 结构 | `templates/`（head 内 `<style>`，便于编辑）+ `templates-inlined/`（已内联，可直接发）；每种邮件有 `basic` / `basic-full` / `plain` 三种版式 |

**其单列骨架（`templates-inlined/basic/receipt/content.html` 实测，正是可直接复用的模式）：**

```
table.email-wrapper[width=100%][role=presentation]
└─ td[align=center]
   └─ table.email-content[width=100%][role=presentation]
      ├─ td.email-masthead[align=center][padding:25px 0]        ← 品牌头
      └─ td.email-body[width=570]
         └─ table.email-body_inner[align=center][width=570][bgcolor=#FFFFFF]
            └─ td.content-cell[padding:45px]                     ← 正文单列
```

值得照抄的细节：
- `align="center"` **HTML 属性** + `width="570"` **HTML 属性** 与 CSS 双写（Outlook 读属性，现代客户端读 CSS）。
- `-premailer-width: 570px; -premailer-cellpadding: 0;` 这类 `-premailer-*` 属性是**给内联器读的指令**，会在内联后被剥离——如果你用 Premailer，这是让内联结果带上 HTML 属性的关键技巧。
- `<span class="preheader" style="display:none !important; visibility:hidden; mso-hide:all; font-size:1px; line-height:1px; max-height:0; max-width:0; opacity:0; overflow:hidden;">` — 预览文案隐藏的标准写法（比 leemunroe 的版本更完整）。
- **它的 head 里仍然保留了 `@import url("https://fonts.googleapis.com/...")`** —— 即 Postmark 官方模板也只用远程字体做增强，靠 `Helvetica, Arial, sans-serif` 兜底。

**拒绝复用**：`box-shadow`（Outlook 不支持，见 §2）、`border-radius`（同理，仅当装饰）、`{{#each}}` 的 Mustachio 语法（模板引擎耦合）、以及它 `<style>` 里的 `@import` 远程字体（我们场景不必要，且会拖慢首屏并可能被 Gmail 丢弃）。

---

### 1.5 关于"Gmail/Outlook 剥离 `<style>`"的权威依据

caniemail（已核实，2026-08-10 数据）对 `<style>` 元素的判定：

| 客户端 | 判定 | Notes（原文） |
|---|---|---|
| **Outlook Windows 2007/2010/2013/2016/2019** | **Partial / Buggy** | *"Buggy. `<style>` elements need to be declared before their rules are used."* |
| **Outlook Windows Mail** (2020-01, 2023-01) | Partial / Buggy | 同上 |
| **Gmail Desktop Webmail** (2023-01) | **Partial** | *"Partial. Not supported inside the `<body>`."* + *"The size of the `<style>` tag is limited to 16 KB"* |
| **Gmail iOS / Android** (2023-01) | **Partial** | *"Partial. Not supported inside the `<body>`."* + *"Partial. Not supported with non Google accounts."* |
| **Apple Mail macOS / iOS** | **Supported** | — |
| **Yahoo Android** | Buggy | *"The first `<head>` in the HTML is removed, so `<style>` elements need to be in a second `<head>` element."* |
| **Yahoo Desktop** | Buggy | *"A CSS rule following a CSS comment is ignored."*（→ [email-bugs#25](https://github.com/hteumeuleu/email-bugs/issues/25)） |

16 KB 限制的原始出处：[email-bugs#90 "Gmail limits `<style>` to 16 kB"](https://github.com/hteumeuleu/email-bugs/issues/90)（已核实该 issue 存在并以此为题）。

**Gmail 移动端的关键陷阱**（最容易踩）：**Gmail App 用非 Google 账号（IMAP/POP，例如学校邮箱托管在 Gmail App 里）拉取时，`<head>` 里的 `<style>` 整体不生效。** 同样地，`display:flex` 在该场景被标记为 *"Not supported with non Google accounts."* ——这直接说明**为什么必须把关键样式内联**。

**→ 工程结论：把 `<style>` 当作 0 收益的负担来对待。关键样式 100% 内联；`<style>` 里只放 `@media` 响应式微调和渐进增强。**

---

## 2. 2026 年必须知道的变化：Outlook Classic 退役

**这是本简报中唯一不能只靠 caniemail 得出的结论，来源为一个商业站点（[Emailens](https://emailens.dev/email-css/outlook-classic-deprecation)，数据标注 "Data sourced from caniemail.com"，last synced 2026-09-02），属于二手来源，请按此权重使用：**

- Outlook Classic（**Word 渲染器**的那个 Outlook）在 **2026 年 4 月** 停止作为 Windows 默认；Microsoft 支持其到 **至少 2029 年**。
- 替代品 **Outlook (New)** 改用 **Outlook Web (OWA) 渲染管线**，不再是 Word。
- 但：298 个受跟踪特性中 **276 个行为完全相同**，10 个改善、5 个退化。
- **依旧不支持的（两边都不支持）**：`border-radius`、`@media`、`display:flex`、`flex-direction`、`flex-wrap`、`display:grid`、`position`、`background-size`。
- **退化的 5 个**：`[height]` 属性、`[width]` 属性、`doctype`、`height`、`width`（supported → partial）。→ **用 HTML 的 `width`/`height` 属性而不是 CSS 属性，才是耐久写法。**
- **改善的 10 个**：`<base>`、`[aria-hidden]`、`[aria-label]`、`[aria-live]`、`[background]` 属性、`[role]`、`@font-face`、image-maps、`text-justify`、`word-break`。

它的迁移建议第 1 条原文：*"Keep your MSO conditionals and VML. `<!--[if mso]>` targets the Word engine; the new Outlook ignores it entirely."*

**⚠️ 一处来源冲突，必须披露：** caniemail 自身的 JSON 对 **Outlook.com desktop-webmail** 的 `display:flex` 记录是 **`y`（support，2019-02 测试）**；而 Emailens 声称新 Outlook 不支持 `display:flex`。caniemail 这条数据**已 7 年未重测**。**冲突下取保守值：不要用 flex。**

---

## 3. 安全子集清单（2026）

图例：**✅ 安全** / **⚠️ 增强（可降级）** / **❌ 避免**

### 3.1 HTML 标签与属性

| 项 | Outlook (Word) | Gmail Web | Gmail iOS/Android | Apple Mail | 结论 |
|---|---|---|---|---|---|
| `<!DOCTYPE html>` 或 XHTML 1.0 Transitional | ✅ | ✅ | ✅ | ✅ | **✅**（Emailens 称新 Outlook 下 doctype 退化，但仍用 XHTML Transitional 最稳） |
| `<table>` / `<tr>` / `<td>` / `<th>` | ✅ | ✅ | ✅ | ✅ | **✅ 布局唯一原语** |
| `role="presentation"` | ✅ (y #1, 2019) | ✅ | ✅ | ✅ | **✅**（注意 Outlook Windows Mail 报 `n`，属可忽略的降级） |
| `cellpadding` / `cellspacing` | ✅ | ✅ | ✅ | ✅ | **✅** |
| `border="0"` | ✅ | ✅ | ✅ | ✅ | **✅** |
| `align` 属性 | ✅ | ✅ | ✅ | ✅ | **✅** |
| `valign` 属性 | ✅ | ✅ | ✅ | ✅ | **✅** |
| `width` / `height` **属性** (table/img/td) | ✅ (partial) | ✅ | ✅ | ✅ | **✅ 首选**（比 CSS 更耐久） |
| `<img>` + `alt` + `width` + `height` + `border="0"` | ✅ | ✅ | ✅ | ✅ | **✅ 四个属性缺一不可** |
| `<style>` in `<head>` | ⚠️ 有顺序 bug，必须在规则使用前声明 | ⚠️ ≤16KB，不可放 body | ⚠️ 非 Google 账号不生效 | ✅ | **⚠️ 仅做增强** |
| `<style>` inside `<body>` | ❌ | ❌ | ❌ | ⚠️ | **❌** |
| `@media` 查询 | ❌ | ⚠️ partial | ⚠️ partial | ✅ | **⚠️ 只用于移动端微调，不能承担布局** |
| `<div>` / `<span>` / `<p>` / `<h1>`–`<h6>` | ✅ | ✅ | ✅ | ✅ | **✅**（但 block 元素垂直间距要内联） |
| `<a>` | ✅ | ✅ | ✅ | ✅ | **✅**（别只靠颜色区分，同时加 `text-decoration:underline`） |
| `<ul>` / `<ol>` | ✅ | ✅ | ✅ | ✅ | **⚠️ Outlook 会重置缩进/符号，列表项建议退化为 `<p>` + 手动符号** |
| `<hr>` | ✅ | ✅ | ✅ | ✅ | **⚠️ 建议用 `<td>` 的 `border-top` 代替** |
| `<button>` / `<input type=submit>` | ❌ | ❌ | ❌ | ❌ | **❌ 用 `<a>` 做成嵌套 table 按钮** |
| `<form>` | ❌ | ❌ | ❌ | ❌ | **❌** |
| `<script>` / JS | ❌ | ❌ | ❌ | ❌ | **❌** 明确说明：本次调研**未在 caniemail 数据集里找到 `<script>` 特性条目**，无法给出逐客户端矩阵；"所有主流客户端剥离脚本"是行业共识但**我未核实到单一权威出处**。react-email 的立场是不使用 JS。按 ❌ 处理 |
| `<details>` / `<summary>` | ❌ | ❌ | ❌ | ⚠️ | **❌** 据 [Microsoft Q&A：`<details>`/`<summary>` not working in outlook](https://learn.microsoft.com/en-gb/answers/questions/2113739/html-tag-(details-)-and-(summary-)-not-working-in)（社区问答，非官方文档）。caniemail 数据集中**没有** `<details>` 条目 → 无人系统测试。**折叠交互在邮件里没有可靠实现，不要用** |
| `<picture>` / `srcset` | ❌ | ❌ | ❌ | ⚠️/✅ | **❌** |
| `<svg>` 内联 | ❌ | ❌ | ❌ | ⚠️ | **❌ 用 PNG** |
| `<!--[if mso]>` 条件注释 | ✅（仅 Word 引擎） | 忽略 | 忽略 | 忽略 | **✅ 保留**（对 Word 引擎是唯一出口，且支持到 ≥2029） |
| `<!--[if !mso]><!-->…<!--<![endif]-->` | ✅ | ✅ | ✅ | ✅ | **✅ 渐进增强的标准手法** |
| HTML 注释 | ✅ | ✅ | ✅ | ✅ | **✅**（但 Yahoo 有"注释后第一条规则被忽略"的 bug，注释别紧贴 `<style>` 规则） |

### 3.2 CSS 属性

| 属性 | Outlook (Word) | Gmail Web | Gmail iOS/Android | Apple Mail | 结论 |
|---|---|---|---|---|---|
| `background-color` (含 `bgcolor` 属性) | ✅ | ✅ | ✅ | ✅ | **✅** |
| `color` | ✅ | ✅ | ✅ | ✅ | **✅** |
| `font-family`（**系统字体栈**） | ✅ | ✅ | ✅ | ✅ | **✅** |
| `font-size`（**px，非 rem**） | ⚠️ partial | ✅ | ✅ | ✅ | **✅** |
| `font-weight` | ⚠️ partial | ✅ | ✅ | ✅ | **✅** |
| `line-height` | ⚠️ partial | ✅ | ✅ | ✅ | **✅** |
| `text-align` | ⚠️ partial | ✅ | ✅ | ✅ | **✅** |
| `text-decoration` | ⚠️ partial | ✅ | ✅ | ✅ | **✅** |
| `letter-spacing` | ⚠️ partial | ✅ | ✅ | ✅ | **⚠️ 仅装饰** |
| `text-transform` | ⚠️ partial | ✅ | ✅ | ✅ | **⚠️ 仅装饰** |
| `padding` | ⚠️ partial (#1 #2) | ✅ | ✅ | ✅ | **✅**（作用在 `td` 上最稳） |
| `margin` | ⚠️ partial (#1–#4) | ⚠️ partial | ⚠️ partial | ✅ | **⚠️ 优先用 `td` 的 `padding` 替代 `p` 的 `margin`；`p{margin:0}` 必须显式写** |
| `width` / `height` (CSS) | ⚠️ partial | ✅ | ✅ | ✅ | **⚠️ 与 HTML 属性双写** |
| `max-width` | ⚠️ partial | ✅ | ✅ | ✅ | **⚠️ 桌面端有效，Outlook 忽略；必须配 `width` 属性** |
| `border`（**必须写全 `border-style` + `border-width` + `border-color`**） | ✅ | ✅ | ✅ | ✅ | **✅**（只写 `border:0` 简写在 Outlook 上不可靠） |
| `border-collapse` | ✅ | ✅ | ✅ | ✅ | **✅ 少见的高兼容属性，用 `collapse`** |
| `vertical-align` | ✅ | ✅ | ✅ | ✅ | **✅** |
| `border-radius` | ❌ | ✅ | ✅ | ✅ | **⚠️ 纯装饰；Outlook 出直角** |
| `box-shadow` | ❌ | ❌/⚠️ | ⚠️ | ✅ | **❌ 不要用阴影做层次，用边框/底色** |
| `background-image` | ❌ | ✅ | ✅ | ✅ | **❌ 不作为信息载体**（AI 报告不要用背景图承载内容） |
| `@font-face` / 远程字体 | ⚠️ partial | ❌ | ❌ | ✅ | **⚠️ 只用系统字体栈**（见下） |
| `display:flex` / `inline-flex` | ❌ | ✅/⚠️ | ⚠️ **非 Google 账号不支持** | ✅ | **❌** |
| `display:grid` | ❌ | ✅/⚠️ | ⚠️ 非 Google 账号 | ✅ | **❌** |
| `position` | ❌ | ❌ | ❌ | ⚠️ partial | **❌** |
| `float` | ❌ | ✅ | ⚠️ | ✅ | **❌ 用 table 分栏** |
| `transform` | ❌ | ❌ | ❌ | ✅ | **❌** |
| `CSS 变量` `var()` | ❌ | ❌ | ❌ | ✅ | **❌** |
| `!important` | ⚠️ partial | ⚠️ partial | ⚠️ partial | ✅ | **⚠️ 仅用于 `@media` 内覆盖，不要用在结构样式上** |
| `display:none` | ⚠️ partial | ✅ | ✅ | ✅ | **⚠️ 隐藏 preheader 时务必叠加 `mso-hide:all` + `max-height:0` + `overflow:hidden`** |
| `table-layout` | ❌ | ✅ | ✅ | ✅ | **❌ 不要依赖** |
| `word-break: break-word` | ⚠️（新 Outlook 改善） | ✅ | ✅ | ✅ | **✅ 对长 URL / 长英文摘要有用** |
| `color-scheme` / `prefers-color-scheme` | ❌ | ❌ | ❌ | ✅ | **⚠️ 只做增强；不要假设暗色模式生效** |
| `rem` / `vw` / `vh` 单位 | ❌ | ❌ | ❌ | ⚠️ | **❌ 一律用 `px` / `pt` / `%`** |

**字体栈建议**（只依赖系统字体，全球客户端都能命中）：

```css
font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
             "Helvetica Neue", Arial, "PingFang SC", "Hiragino Sans GB",
             "Microsoft YaHei", sans-serif;
```

**中文场景额外注意**：中文字体族差异极大，**绝对不要**用远程 webfont 承载中文（体积巨大、Gmail 直接丢、Outlook 不支持）。用上面的系统栈即可。

---

## 4. 可直接复用的安全单列骨架（inline style 版）

这是综合 leemunroe（居中骨架 / preheader / 嵌套按钮）与 Postmark（属性+CSS 双写 / `align="center"` / `bgcolor`）提炼出的安全子集版本。**宽 600px、单列、全内联、零 `<style>` 依赖。**

```html
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
  "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="zh-CN" xml:lang="zh-CN">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="x-apple-disable-message-reformatting" />
  <meta name="color-scheme" content="light dark" />
  <title>AI 摘要报告</title>
</head>
<body lang="zh-CN" style="margin:0; padding:0; width:100% !important;
      background-color:#f4f5f6; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%;">

  <!-- preheader：隐藏预览文案 -->
  <span style="display:none !important; visibility:hidden; mso-hide:all;
        font-size:1px; line-height:1px; max-height:0; max-width:0;
        opacity:0; overflow:hidden;">您的本周 AI 摘要报告已生成</span>

  <!-- ① 外层 100% 表格 -->
  <table role="presentation" border="0" cellpadding="0" cellspacing="0"
         width="100%" bgcolor="#f4f5f6"
         style="width:100%; background-color:#f4f5f6; border-collapse:collapse;">
    <tr>
      <td align="center" style="padding:24px 12px;">

        <!-- ② 固定宽 600 的内容表格，带 mso 条件注释兜底 -->
        <!--[if mso]>
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="600"><tr><td>
        <![endif]-->
        <table role="presentation" border="0" cellpadding="0" cellspacing="0"
               width="600" bgcolor="#ffffff"
               style="width:600px; max-width:600px; background-color:#ffffff;
                      border-collapse:collapse; border:1px solid #eaebed;">
          <tr>
            <!-- ③ 单一内容列 -->
            <td style="padding:24px; font-family:-apple-system,BlinkMacSystemFont,
                       'Segoe UI',Roboto,'Helvetica Neue',Arial,'PingFang SC',
                       'Microsoft YaHei',sans-serif; font-size:16px; line-height:1.6;
                       color:#1f2328; vertical-align:top;">

              <p style="margin:0 0 16px; font-size:16px; line-height:1.6; color:#1f2328;">
                同学你好，
              </p>
              <p style="margin:0 0 16px; font-size:16px; line-height:1.6; color:#1f2328;">
                以下是你的本周课程摘要……
              </p>

              <!-- ④ 嵌套 table 按钮：不要用 <button>，padding 放在 <a> 上 -->
              <table role="presentation" border="0" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" bgcolor="#0867ec"
                      style="background-color:#0867ec; border-radius:4px;">
                    <a href="https://example.edu/report/123"
                       style="display:inline-block; padding:12px 24px; font-size:16px;
                              font-weight:bold; color:#ffffff; text-decoration:none;
                              border:2px solid #0867ec; border-radius:4px;">查看完整报告</a>
                  </td>
                </tr>
              </table>

              <p style="margin:16px 0 0; font-size:13px; line-height:1.5; color:#6b7280;">
                若按钮无法点击，请复制链接：<br />
                <a href="https://example.edu/report/123"
                   style="color:#0867ec; text-decoration:underline;
                          word-break:break-word;">https://example.edu/report/123</a>
              </p>

            </td>
          </tr>
        </table>
        <!--[if mso]>
        </td></tr></table>
        <![endif]-->

      </td>
    </tr>
  </table>
</body>
</html>
```

**骨架设计要点（逐条都有依据）：**

1. **`width="100%"` 表格 + `td[align=center]`** → 替代 `margin:auto`（Outlook 忽略 `margin`）。
2. **`width="600"` 属性 + `width:600px; max-width:600px` CSS 双写** → 属性给 Outlook，CSS 给现代客户端；`max-width` 单独不可靠。
3. **`bgcolor` 属性 + `background-color` CSS 双写** → 同上。
4. **`border-collapse:collapse`** → caniemail 里该属性在 **全部 4 个目标客户端均为 ✅**，是本表中罕见的全绿项。
5. **所有 `border` 写全三要素**（`border:2px solid #0867ec`），不写 `border:0` 简写单独出现。
6. **`<a>` 做按钮 + 父 `td` 上 `bgcolor`** → 避免 Outlook 不支持 `<button>`，也避免 `<a>` padding 在 Outlook 上被吃掉的经典问题。
7. **纯文字兜底链接** → AI 报告场景下，即便按钮渲染失败，信息仍可达。
8. **`<!--[if mso]>` 固定宽表格包裹** → 本轮唯一"为非标准客户端写代码"的地方，成本极低、收益明确（Word 引擎支持到 ≥2029）。
9. **`<style>` 块完全移除** → 本骨架不依赖任何 `<style>`，从根本上规避 Gmail 非 Google 账号 / 16KB 限制 / Outlook 声明顺序 bug。

---

## 5. 明确未能核实的事项（不要当成事实使用）

1. **`postalsys/email-templates` 不存在**（404）。同组织的 `postalsys/templates` 是 ISC 声明的**模板语言库**，且仓库内**无 LICENSE 文件**（GitHub API 报 `license: None`），许可状态不干净；`postalsys/email-guide` **无许可证**。**均不建议复用**。
2. **react-email 仓库中没有任何关于 `<details>` 的表述** —— 该说法不能归因于 react-email。
3. **react-email 并不禁止远程字体** —— 它提供 `<Font>` 组件并要求配 `fallbackFontFamily`。原提问中 "no remote fonts" 的表述与仓库内容不符。
4. **`<script>` 的逐客户端支持矩阵无法给出** —— caniemail 数据集中无此条目，本次未找到单一权威出处。
5. **`<details>` 的客户端矩阵不完整** —— caniemail 无此条目；仅有 Microsoft Q&A 社区帖证明 Outlook 不支持。
6. **Emailens 的 "Outlook Classic 2026-04 退役" 数据是二手来源**（商业站点，自称数据源自 caniemail）。我**未能**从 Microsoft 官方文档直接核实"2026 年 4 月停用默认"与"支持到 2029"的原始公告。
7. **caniemail 中 Outlook.com 的 `display:flex = y` 与其自身 2019 年测试日期** —— 该结论已陈旧，且与 Emailens 的 2026 结论冲突，**不要采信**。
8. **leemunroe 仓库最后 `pushed_at` 为 2024-08-20** 是 GitHub API 值；我未逐条审计其提交内容的新旧。
9. **Emailens 提供的"298 特性中 276 相同"等统计数字**未经独立复核。

---

## 6. 来源清单

**一级（仓库 / 数据，已逐一核实 license 与维护信号）**

- https://github.com/resend/react-email — MIT，19,732★，pushed 2026-09-09
- https://github.com/leemunroe/responsive-html-email-template — MIT（`license.txt`），13,696★，pushed 2024-08-20
- https://github.com/hteumeuleu/caniemail — MIT，942★，pushed 2026-08-10
- https://www.caniemail.com/api/data.json — `last_update_date 2026-08-10`，308 特性
- https://github.com/hteumeuleu/email-bugs — **无 LICENSE**，575★，pushed 2023-08-16（issue tracker，无代码）
- https://github.com/ActiveCampaign/postmark-templates — MIT（Wildbit 2015），3,194★，最后 commit 2022-08-25
- https://github.com/postalsys/templates — ISC 声明 / 无 LICENSE 文件，0★，pushed 2026-08-12（非 HTML 模板）
- https://github.com/postalsys/email-guide — **无 LICENSE**，4★，pushed 2024-01-03（非 HTML 兼容性内容）

**二级（文档 / 社区，按较低权重使用）**

- https://react.email/docs/components/font — web fonts + fallback 立场
- https://react.email/docs/components/html — "Some email clients strip the `html` or `body` tags"
- https://react.email/docs/components/image — PNG/GIF/JPG 可用，SVG 支持差
- https://postmarkapp.com/support/article/786-using-a-postmark-starter-template — Postmark 投递时自动内联 CSS
- https://github.com/hteumeuleu/email-bugs/issues/90 — Gmail `<style>` 16KB 限制
- https://github.com/hteumeuleu/email-bugs/issues/25 — Yahoo CSS 注释后规则被忽略
- https://learn.microsoft.com/en-gb/answers/questions/2113739/html-tag-(details-)-and-(summary-)-not-working-in — `<details>`/`<summary>` 在 Outlook 失效
- https://emailens.dev/email-css/outlook-classic-deprecation — Outlook Classic 退役二手分析（2026-09-02 同步）

---

## 7. 给实现者的 5 条硬规则

1. **宽度只用 `600px` 一种尺寸**，全部单列；< 600px 时靠 `width:100%` + `max-width` 自然收缩，不依赖 `@media`。
2. **关键样式 100% 内联**；`<style>` 块可以完全不写（本简报骨架即是零 `<style>`）。
3. **所有尺寸/颜色双写**：HTML 属性（`width` / `height` / `bgcolor` / `align` / `valign` / `cellpadding` / `cellspacing` / `border`）+ inline CSS。
4. **凡是"布局"一律 table；凡是"交互"一律 `<a>`** —— 邮件里没有可靠的 `flex` / `grid` / `button` / `details`。
5. **发送前必做**：Gmail 网页 + Gmail 手机 App（**并用一个非 Google 账号**）+ Outlook Windows + Apple Mail iOS 四端真机/截图测试；总量控制在 **102KB 以内**（Gmail 截断阈值，react-email 亦如此建议）。
