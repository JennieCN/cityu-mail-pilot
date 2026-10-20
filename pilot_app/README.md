# CityU Mail Pilot

一个面向 3–5 名试点用户的多用户邮件分析 Web/PWA。CityU 邮箱只负责把邮件转发到用户自己的私人邮箱；香港云服务器通过只读 IMAP 获取新邮件，使用用户自己的模型与搜索 API 生成个性化中英双语报告，再从私人邮箱通过 SMTP 发给用户指定的报告地址。Mac 可以关机或断网。

当前版本 **0.2.0**：界面采用「清晰行动版」，即时报告采用「行动优先」，每晚 22:00 日报采用「学生简报版」。

## 界面：清晰行动版（A）

登录后第一屏只回答一个问题：**我下一步该做什么？**

- 顶部一张「你的下一步」卡片，按优先级只显示一个动作：补资料 → 设置转发邮箱 → 检查收信 → 配置模型 → 处理今天的待办 → 全部就绪。
- 四张状态卡明确区分「已验证」和「未验证」：邮箱收信、AI 摘要、联网搜索、每日简报。邮箱卡在**真的做过一次只读连接测试**之后才显示「正常」，并给出验证时间；超过 24 小时会变成「待复查」。界面不会把"已填写"说成"已连通"。
- 「今天要处理的事」列出待办、截止时间和来源邮件；「最近的摘要」给出当天每封邮件的星级与状态。
- 第一步专业/学校邮箱、第二步邮箱（含 Outlook 转发向导）、第三步模型、第四步搜索，分步向导；IMAP/SMTP 服务器、Base URL、Azure 版本等技术字段折叠在「高级」里，默认不出现。
- 手机优先：所有按钮 ≥44px，无横向滚动；高级设置默认收起。
- 错误信息说明"发生了什么、会不会漏信、下一步怎么做"。例如检查失败会明确写「这不会丢邮件；请按提示修正后重试」。

新增只读接口 `GET /api/dashboard`（状态、下一步、今日待办与计数）和 `POST /api/mailbox/verify`（用户主动触发的只读 IMAP 检查，同一用户 60 秒最多一次）。**验证不会移动 UID 游标，也不会标记或删除任何邮件**，因此点这个按钮不可能造成重复或漏发。

## 即时报告：行动优先（A）

每收到一封新邮件，报告从上到下严格是：

1. **重要程度 + 一句话结论**（首屏直接可见，不需要展开）
2. **必须采取的行动 + 截止时间**（有明确日期的会单独标出）
3. 邮件内容摘要
4. 与你的专业、年级、课程和兴趣的相关性
5. 联网搜索后的建议 + 可点击的 `https://` 来源
6. 风险、未知信息、明确标注「AI 推测」的行
7. 简短英文摘要

模型输出会被 `prompts.normalize_report` 强制重排成上面这七段（旧版六段报告仍能正确解析并迁移显示）。报告同时以**纯文本**和 **HTML** 两种形式发送。

HTML 使用保守的邮件兼容结构：单列 600px 表格、`width`/`bgcolor` 属性与行内样式双写、`role="presentation"`、`table-layout:fixed` + `word-break` 防止超长链接撑破手机宽度、保留 `<!--[if mso]>` 条件注释；**没有 JavaScript、没有 `<style>` 块、没有 `@media`、没有 `<details>`、没有远程字体、没有图片**。只有 `https://` 链接会变成可点击的 `<a>`，模型给出的 `http://` 会被显式屏蔽。

## 每日简报：学生简报版（C）

每晚当地时间 22:00（默认香港时间）发送，第一屏是"今天/明天必须处理什么"：

1. **现在就要处理**：最多三条按截止时间排序的动作
2. 关键数字：今日邮件数、需要行动数、最近截止时间、异常/失败数
3. 紧急待办 / 学业相关 / 机会与活动 / 行政通知 / 低优先级与营销 分节
4. 每封邮件保留可追溯的**发件人、主题、收件时间、状态与来源链接**
5. 异常与整体说明：失败邮件、本次未取得可验证来源的邮件、被合并的重复邮件

**简报由本地确定性合成**（`reports.build_digest`），不是让模型"再总结一遍当天所有邮件"。原因：模型重排无法保证不丢信。现在"任何邮件都不得静默丢弃"由结构保证——重复主题会合并成一行并注明「另有 N 封同类」（数量仍计入总数），无法归类的邮件进入低优先级分节，没有生成摘要的邮件进入失败分节，每条都保留发件人和主题。因此 22:00 简报**不再调用模型**，更快也不会因模型故障而整封丢失。

## 已实现

- 注册（**2026-09-22 起完全开放**：不再需要邀请码，按 IP 限速、按名额上限封顶）、登录、暂停、恢复和完整账户删除。
- 每位用户独立设置专业、年级、课程、兴趣、职业目标、重点/低兴趣主题、自定义要求、时区和每日时间。
- 每位用户独立的 CityU 学校邮箱字段（仅接受 `@cityu.edu.hk` 及其子域），与私人转发邮箱分开保存、互相同步。
- 每位用户独立设置私人邮箱、报告接收地址和应用专用密码。
- 邮箱上手引导：只需选「我用哪个邮箱」（QQ / 网易 / Gmail / iCloud / Outlook / Yahoo / 其它），服务器与端口自动填好；IMAP、SMTP、端口、授权码这些术语折叠在可展开的说明里，用大白话解释，并给出各服务商获取授权码的分步指引与官方帮助链接。
- 模型 BYOK：OpenAI、Anthropic、Gemini、火山方舟 Agent Plan/标准接口、DeepSeek、OpenRouter、Groq、Mistral、xAI、Together、Qwen、智谱、Moonshot、Azure OpenAI 和自定义 OpenAI 兼容接口。
- 搜索 BYOK：豆包联网搜索、Tavily 和 Brave Search。
- 收到邮件后即时双语报告，以及用户当地时间每天 22:00（可改）汇总。
- 联网事实必须附实际 URL，事实和推测分开；没有可验证来源时明确写「本次未取得可验证来源」，不伪造引用。
- IMAP `BODY.PEEK[]` 只读、UIDVALIDITY 感知去重、游标推进、指数退避重试、生成邮件循环过滤。
- 最多 3 个轻量并发任务，适合当前 2 vCPU / 2 GB RAM 服务器。
- AES-256-GCM 加密邮箱授权码、API key、待处理正文与报告；发送成功后清空原始正文。
- 自定义 API 与邮箱主机的公网地址校验，防止 SSRF/内网探测。
- 安全 Cookie、Origin 检查、登录限速、反向代理限速示例、服务沙箱和内存上限。
- SQLite WAL、每日一致性备份、7 份滚动保留。

## 联网搜索：能用模型自带的就不必再买一个

选择自带联网搜索的模型供应商时，程序会在**同一次模型调用**里打开该供应商的搜索工具并解析它返回的引用来源，「联网搜索」那一步整段可以跳过。

| 供应商 | 原生搜索 | 实现 |
|---|---|---|
| OpenAI | ✅ | Responses API `tools: [{"type":"web_search"}]`，读取 `annotations[].url_citation` |
| Anthropic Claude | ✅ | Messages API `tools: [{"type":"web_search_20250305","name":"web_search"}]`，读取 `web_search_tool_result` 与文本块 `citations[]`（不需要 beta 头） |
| Google Gemini | ✅ | `generateContent` `tools: [{"google_search":{}}]`，读取 `groundingMetadata.groundingChunks[].web` |
| DeepSeek / Qwen / 智谱 / Moonshot / OpenRouter / Groq / Mistral / Together / Azure / 自定义 | ❌ | 需要「联网搜索」那一步的外部搜索 API（豆包 / Tavily / Brave） |

降级链——任何一步失败都不会让邮件摘要失败：

1. 供应商有原生搜索 → 用它；
2. 原生搜索调用失败 → 改用用户自己配置的外部搜索 API；
3. 外部搜索也没有或失败 → 不带来源正常生成摘要，报告里写明「本次未完成联网核实」。

已知取舍：

- **Gemini** 返回的来源 URL 通常是 Google 跳转链接（`vertexaisearch.cloud.google.com/grounding-api-redirect/...`），不是发布方原始地址，`title` 也常只有域名。
- **火山方舟（Ark）暂未启用原生搜索**：其 Responses 联网插件的 `url_citation` 具体结构在官方主参考中没有文档（只在开发者社区文章里出现），为避免把未经验证的解析写进生产路径，Ark 仍走外部搜索。补齐官方依据后可以再开启。
- Anthropic 的搜索错误会以 **HTTP 200** 返回错误对象（`web_search_tool_result_error`）；解析器只认 `web_search_result`，因此错误会被当作「无来源」并触发降级，而不是中断摘要。
- OpenAI 的 `annotations` 可能为空（搜索跑过但没有引用），这被当作正常结果处理。

## Web 层

网页层是 **纯 Python 标准库**（`http.server.ThreadingHTTPServer`），没有 FastAPI/uvicorn/Starlette。原因：依赖漏洞审计发现原先锁定的 Starlette 分支带有公开公告，而镜像里还没有兼容的修复版；为了不带着已知问题上线、也不在 2 GB 服务器上常驻多余框架，改为标准库实现。

保留了同等的行为：路由、`{"detail": ...}` 错误格式、会话 Cookie（HttpOnly/SameSite=Lax/Secure）、同源 Origin 拒绝、登录限速（同一邮箱 15 分钟内 8 次失败后锁定）、请求体 64 KB 上限、安全响应头（CSP/X-Frame-Options/nosniff/Referrer-Policy）、静态资源仅白名单路径、SIGTERM 优雅退出。

运行时依赖只剩 `cryptography`（AES-256-GCM 加密）。`requirements.lock` 从 23 个包降到 4 个。

## 数据路径

```text
CityU mailbox -> external forwarding -> private mailbox (IMAP, read-only)
                                            |
                                            v
                                      encrypted queue
                                            |
                      public-safe subject -> user's Search API
                      email + profile + sources -> user's Model API
                                            |
                                            v
                              encrypted report -> SMTP -> report address
```

搜索查询仅从脱敏后的主题生成。密码、验证码、账户、付款、学号、电话号码等敏感主题会直接跳过联网搜索。完整正文不会发送给搜索供应商，但会按用户选择发送给模型供应商用于摘要。

## 本地开发

要求 Python 3.9+。

```bash
python3 -m venv .venv-pilot
.venv-pilot/bin/pip install -r pilot_app/requirements-dev.txt
export INFE_PILOT_DB=/tmp/cityu-mail-pilot.sqlite3
export INFE_PILOT_MASTER_KEY="$($PWD/.venv-pilot/bin/python -m pilot_app.manage generate-master-key)"
export INFE_PILOT_COOKIE_SECURE=0
export INFE_PILOT_ORIGIN=http://127.0.0.1:8787
.venv-pilot/bin/python -m pilot_app.manage create-invite --label local
.venv-pilot/bin/python -m pilot_app.web --host 127.0.0.1 --port 8787
```

在另一个终端启动 worker：

```bash
.venv-pilot/bin/python -m pilot_app.worker
```

测试：

```bash
.venv-pilot/bin/python -m unittest discover -s pilot_app/tests -p 'test_*.py' -v
```

### 界面预览与真实浏览器响应式检查

`tools/generate_preview.py` 会在一个**全新的一次性数据库**里造出示例用户、示例邮件和两份报告预览，再配合 `tools/browser_check.js`（Playwright）在 360 / 390 / 768 / 1280 四种宽度下真实打开页面，检查横向溢出、文字截断、控制台错误和首屏"下一步"是否存在：

```bash
# 1) 造预览数据（不会碰任何真实数据库或邮箱）
INFE_PILOT_MASTER_KEY="$(.venv-pilot/bin/python -m pilot_app.manage generate-master-key)" \
  .venv-pilot/bin/python tools/generate_preview.py --db /tmp/pilot-ui/ui.sqlite3 --out /tmp/pilot-ui --fresh

# 2) 用预览库启动网页
INFE_PILOT_DB=/tmp/pilot-ui/ui.sqlite3 INFE_PILOT_MASTER_KEY=<同上> \
  INFE_PILOT_COOKIE_SECURE=0 INFE_PILOT_ORIGIN=http://127.0.0.1:8790 \
  .venv-pilot/bin/python -m pilot_app.web --host 127.0.0.1 --port 8790

# 3) 真实浏览器检查（任意目录安装 playwright 后）
npm install playwright && npx playwright install chromium
node tools/browser_check.js http://127.0.0.1:8790 /tmp/pilot-ui/shots
```

`/tmp/pilot-ui/report-preview.html` 与 `report-preview-long.html` 是**只用于预览的独立 HTML 文件**（含一份故意超长的内容压力样本），不会被当作邮件发送。

### 真实链路端到端验证（不会重复发信）

`verify-e2e` 用真实邮箱和真实 API 跑完整条链路，同时保证不制造重复邮件：

```bash
INFE_PILOT_DB=<数据库> INFE_PILOT_MASTER_KEY=<主密钥> \
  .venv-pilot/bin/python -m pilot_app.manage verify-e2e \
  --user-email user@example.com --limit 1          # 默认：只抓信+生成+渲染，不发信
```

安全保证：

- **绝不重复发送**：任何已有 `status='sent'` 即时报告的消息都会被跳过并明确打印「不会重发」。只有显式加 `--force-resend` 才允许重发（仅供验收取证）。
- **绝不动 UID 游标**：只读抓取后不调用 `update_mailbox_poll`，worker 的视图完全不变。
- **绝不打印秘密**：输出只有掩码后的地址、数量、耗时、字节数和分节名；需要看正文必须显式加 `--show-body`。

常用参数：`--send` 真的发送、`--send-digest` 把当天日报也发一次、`--pause` 验证期间临时禁用该邮箱（结束后自动恢复）以防第二个 worker 同时消费、`--pull N` 忽略游标取最近 N 封真实邮件做验证。

## 腾讯轻量服务器部署

不要覆盖或停止当前的 `infe-mail-assistant-imap.service`。试点版使用不同的目录、端口、数据库和 systemd 服务，可以先并行安装，但同一个 QQ 邮箱只应由一个 worker 消费，正式迁移时才停旧 worker。

1. 把整个项目上传到服务器的临时目录。
2. 运行 `sudo bash pilot_app/deploy_pilot.sh`。
3. 准备一个指向服务器公网 IP 的域名。
4. 把 `pilot_app/nginx-cityu-mail-pilot.conf.example` 复制到 Nginx 配置，替换 `mail.example.com`。
5. 修改 `/etc/cityu-mail-pilot/pilot.env` 中的 `INFE_PILOT_ORIGIN` 为真实 HTTPS 域名，然后重启两个服务。
6. 使用 Certbot 给域名启用 HTTPS。没有 HTTPS 时不要输入邮箱授权码或 API key。
7. （**只在要办封闭内测时才做**）注册默认是开放的，任何人都能在首页直接注册，所以这一
   步不是必需：`create-invite` 生成的码仍然有效、仍然能被注册时认领，但注册**不再要求**它。
   真正拦人的是后台面板里的**名额上限**（默认 220）与按 IP 的限速。要发码时：

```bash
sudo -u cityumail INFE_PILOT_DB=/var/lib/cityu-mail-pilot/pilot.sqlite3 \
  /opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage create-invite --label pilot-user-1
```

8. 确认服务：

```bash
sudo systemctl status cityu-mail-pilot-web cityu-mail-pilot-worker
curl http://127.0.0.1:8787/health
sudo journalctl -u cityu-mail-pilot-worker -n 100 --no-pager
```

部署脚本第一次运行会生成唯一主密钥。必须把 `/etc/cityu-mail-pilot/pilot.env` 单独、安全地离线备份；丢失主密钥后，数据库中的授权码、API key、邮件和报告无法恢复。不要把该文件上传到 GitHub、聊天或普通云盘。

## 零中断迁移顺序

1. 新 Web 服务、HTTPS 和备份先上线。
2. 使用一个与当前生产不同的测试邮箱完成注册、IMAP、模型、搜索和 SMTP 测试。
3. 发送一封包含已知内容的测试邮件，核对即时摘要、来源 URL、双语排版和报告地址。
4. 把每日时间临时设为当前时间后两分钟，核对每日汇总，再改回 22:00。
5. 在新网页账户中先点「暂停」，再接入原先的私人收件邮箱；运行「只读连接测试」，记下页面显示的 `UIDVALIDITY`。
6. 停止旧 worker 并复制它的非敏感 UID 状态（不复制授权码或 API key）：

```bash
sudo systemctl stop infe-mail-assistant-imap.service
sudo install -o cityumail -g cityumail -m 0600 \
  /var/lib/infe-mail-assistant/imap-state.json \
  /var/lib/cityu-mail-pilot/legacy-imap-state.json
```

7. 先预览，再实际迁移。把示例邮箱和 `123456` 换成网页登录邮箱及第 5 步显示的值：

```bash
sudo -u cityumail bash -lc 'set -a; source /etc/cityu-mail-pilot/pilot.env; set +a; \
  /opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage migrate-legacy-imap-state \
  --user-email user@example.com \
  --state /var/lib/cityu-mail-pilot/legacy-imap-state.json \
  --uid-validity 123456'

# 预览中的数量和 UID 范围正确后，原命令末尾加：
# --apply
```

迁移器不会简单地取最大 UID，因为旧集合里可能存在失败空洞。它会精确登记每个已成功处理的 UID，并让新 worker 安全重扫一段窗口：旧邮件由唯一键去重，空洞中的遗漏邮件会补做。

**UIDVALIDITY 会被强制校验（v0.2.0 起）。** 迁移命令会自己只读连接邮箱、读取服务器真实的 UIDVALIDITY 与传入值比对：不一致直接拒绝，读不到（网络/凭据问题）也拒绝——因为占位记录按 UIDVALIDITY 参与唯一键，填错会让去重整体失效并把历史邮件全部重发。确需强制继续时用 `--allow-unverified-uid-validity`；数据库层会记录本次是「服务器校验」还是「操作者强制」，不声明就直接拒绝写入。

**空洞会被告知。** 命令会算出「邮箱里存在但旧集合没有」的 UID 数量，并区分它们是否落在回扫窗口内。若有空洞落在窗口外，默认 48 小时不会补做，输出会提示把 `INFE_PILOT_INITIAL_LOOKBACK_HOURS` 调大（例如 720），或用 `--lookback-hours` 预览该值的影响。

账户未暂停、文件格式异常、UIDVALIDITY 不一致时会拒绝写入。

8. 在网页恢复账户，只发送一封切换测试邮件。观察 24 小时无重复/漏发后，保留旧服务文件但禁用自动启动。出现问题可立即暂停新账户并恢复旧服务。

## 当前边界

- 第一阶段只处理正文和附件名称，不上传或解析附件内容。附件解析要增加文件类型检测、恶意文件隔离和更严格的留存策略后再开放。
- SQLite + 单机 worker 面向 3–5 人完整体验。增长到约 20–50 名活跃用户前，应迁移 PostgreSQL、独立任务队列和对象存储，并增加邮箱域名级并发/限速。
- 用户必须在私人邮箱提供商中开启 IMAP/SMTP，并创建应用专用密码或授权码。普通网页登录密码不应使用。
- Qwen、智谱和 Moonshot 会因地域、业务空间或套餐类型使用不同 Base URL，界面允许覆盖默认值。用户必须选择允许后端自动化调用的 API 计费方案；某些 Coding/Token Plan 只允许交互式开发工具，不能用于本服务。
- Web 端**没有自助密码重置，这是刻意的**：往注册邮箱发重置链接等于把只读收取的那个邮箱变成登录凭据，而学校邮箱不是我们的（理由见 `docs/password-reset-2026-09-19.md`）。忘了密码由运营者重设——后台「用户」面板那一行的「重设密码」按钮，或 `pilot_app.manage reset-password`。隐私政策、使用条款、数据导出与账户删除都已上线（`/privacy`、`/terms`）。

## 开源取舍

完整记录见 [OPEN_SOURCE_REVIEW.md](OPEN_SOURCE_REVIEW.md)。实现采用了架构思想而非复制第三方业务代码；LiteLLM 暂未作为常驻网关，以降低 2 GB 服务器的内存和运维成本。
