# 合规面：隐私政策、同意、导出、删除（2026-09-14）

> 这一轮把 `docs/stranger-ready-research-2026-09-14.md` 里排第一的合规缺口补上了。
> 目的不是"看起来合规"，而是**让产品说的话和产品做的事一致**——下面每一条政策声明都有测试盯着。

## 1. 为什么现在做

分发决策选了 **AGPL-3.0 自托管优先**。别人部署在自己的服务器、用自己的邮箱和 key 时，
**他就是资料使用者，我们不处理任何东西**，合规负担基本绕开了。

但**如果我们自己开公开注册**（现在这台服务器），就变成我们替用户处理邮件，
于是三件事必须齐备：

| 要求 | 依据 | 状态 |
|---|---|---|
| 收集时告知**资料可能转移给哪一类人** | PDPO DPP1(3)(b)(i)(B) | ✅ 注册页 + 政策第 3 节点名服务商 |
| 告知保留期与删除途径 | PDPO DPP2、PCPD AI 指引 | ✅ 政策第 4、6 节 |
| 用户能**取回**和**删除**自己的资料 | PDPO s26（**刑事条款**）、DPP6 | ✅ 网页按钮，不是"发邮件给我们" |

PDPO s26 是刑事条款（最高罚 HK$10,000），所以"删除"不能只是承诺。

## 2. 做了什么

### 2.1 两份文书（服务端渲染，不是静态文件）

- `pilot_app/static/privacy.html`
- `pilot_app/static/terms.html`
- 路由：`GET /privacy`、`GET /terms`（挂在 `STATIC_FILES` + `TEMPLATED_STATIC`）

**为什么不做成纯静态文件**：两份文书都要印联系邮箱。写死在模板里意味着
任何一个自托管者都会**公开我们的地址**，或者我们**不小心公开他的地址**。
所以模板里是 `{{CONTACT_LINK}}`，由 `web.render_legal_page()` 在响应时填：

- 优先 `INFE_PILOT_CONTACT_EMAIL`（专门的联系地址，不影响管理员登录权限）
- 退到 `INFE_PILOT_ADMIN_EMAILS` 的第一位（单人部署零配置）
- 都没有 → 页面明写"**尚未配置联系邮箱**"，而不是渲染一个指向空气的 `mailto:`

地址进 HTML 前会 `html.escape(quote=True)`：这个值来自环境变量，
里面一个引号就能从 `href` 里逃出去。

### 2.2 注册时的告知（在采集点，不在页脚）

`#consent-row` 就在注册表单里，写着两件用户事后无法自己发现的事：

1. 报告由 AI 生成，**可能与原邮件不符，以原邮件为准**
2. 生成报告时**邮件正文会发送给用户自己选择的大模型服务商**

勾选框默认**不选中**，且**服务端强制**（`_boolean(payload, "accepted_terms", False)`）——
浏览器里的勾选框如果服务端不检查，那只是装饰。

### 2.3 导出（新）

`GET /api/account/export` → 一份 JSON（`Content-Disposition: attachment`）。

`database.export_user_data()` **故意不导出两类东西**：

- `encrypted_password` / `encrypted_api_key`：这是凭据不是个人资料，
  而且把密文交给用户等于白加密
- 邮件正文：已投递的本来就被清空了，跳过的从来不存

报告正文**会**导出，且**会解密**——那是用户自己的内容。

### 2.4 删除（已存在，本轮补测试与文案）

`PUT /api/account/status/deleted` 早就有，靠 SQLite 外键级联清掉
mailbox/API key/session/profile/messages/reports/feedback，并吊销 cookie。
本轮的改动是：确认文案说清"不可恢复"，并提示先导出。

## 3. 一个真实的不符，以及修法

写完政策我去核对代码，发现**政策会撒谎**：

`service.poll_mailbox()` 原来的顺序是「先加密正文并入库 → 再判断发件人是否在允许域内 → 标记 skipped」。
所以**非本校邮件的正文一直躺在数据库里**，虽然永远不会被处理。

这正是 BetterHelp 那类案子的形状（承诺与现实不符，$7.8M）。两种修法里选了改代码：

1. `service.py`：发件人判断**前移**，不允许的邮件正文压根不加密、不入库（存 `b""`）
   - 顺带发现 `SecretBox.encrypt("")` 会抛 `SecurityError("不能加密空密钥。")`，
     所以空正文不能走加密路径，直接存空 blob（skipped 邮件永远不进队列，没人会去解密它）
2. `database.initialize()`：加一条幂等清理 `UPDATE messages SET body=X'' WHERE status='skipped' AND body!=X''`
   ——**生产库里已有的脏数据**必须一起清掉，否则政策仍然被数据打脸
   - 用 `X''` 而不是 `''`，保持 BLOB 类型与写入路径一致

`decrypt_message()` 加了空值保护（`if not value: return ""`）作为安全网。

## 4. 测试（20 个，`pilot_app/tests/test_compliance.py`）

| 组 | 盯住什么 |
|---|---|
| `LegalPageTests` | 页面 200/text/html；**点名** DeepSeek/OpenAI/火山方舟；写明训练风险、保留期、只读、非本校不入库、7 份备份；联系邮箱来自配置；**缺失时明说**而不是死链；**恶意联系地址无法逃出属性**；首页/页脚可达且采集点有告知 |
| `ConsentTests` | 不勾选→400 且提到隐私政策；勾选→200；`"yes"` 这种**真值字符串不算同意**（422） |
| `ExportTests` | 匿名→401；导出自己的资料；**不含** `encrypted_password`/`encrypted_api_key`/`password_hash`/`token_hash`/主密钥；报告正文解密后可见；**已清空的邮件正文不会被复活** |
| `RetentionTests` | 迁移清掉历史 skipped 正文但**保留元数据**；重复启动无副作用 |

另加 `tools/compliance_check.js`（**24 项真实浏览器断言**）：360px 无横向溢出、
正文字号 ≥13px、点隐私政策新开一页、**未勾选时根本不发注册请求**。

### 踩到的两个坑

1. **测试跨模块污染**（老问题，又犯一次）：我的 `setUp` 里
   `os.environ.pop("INFE_PILOT_ADMIN_EMAILS")` 把 `test_metrics` 在 import 时设的管理员身份也抹了，
   导致它 2 个测试失败。改成 **存旧值 → tearDown 还原**。
   > 「先怀疑工装」：失败的是**别人的**测试，先看是不是我弄脏了环境。
2. 表设计记错：`mailboxes`/`connections` 是 `updated_at`，**没有 `created_at`**。
   导出查询一开始 500，报 `no such column: created_at`。

## 5. 没做的，以及为什么

| 事项 | 状态 |
|---|---|
| 邮箱验证（防冒用他人邮箱注册） | 未做。目前靠**邀请码 + 内测名额**控制，风险可接受 |
| 自助改密码找回 | 未做（改密码已有，忘记密码没有） |
| 出事通知的**具体流程** | 政策写了承诺，但没有演练过 |
| GDPR Art.27 EU 代表、Art.46 传输保障 | 未做。**只有真的有欧盟用户时才是必需的** |
| 数据处理协议（DPA）模板 | 未做。自托管者可能需要 |
| 版本化的政策存档（改版留旧版） | 未做。现在只有"更新日期" |

## 6. 自托管者需要注意

`INFE_PILOT_CONTACT_EMAIL` 不设时，页面会显示"尚未配置联系邮箱"。
**要给别人下载的版本，部署文档里应该要求填这一项**——不然他发布出去的隐私政策没有联系渠道。

本项目的生产实例已设置为 **`contact@example.com`**（2026-09-14）。
它与管理员登录邮箱（`operator@example.com`）**刻意分开**：管理员邮箱是权限凭据，
而文书上的地址是**公开**的，两者混用等于把登录身份印在网页上。
改动方式（不需要改代码、不需要重新部署）：

```bash
sudo cp /etc/cityu-mail-pilot/pilot.env /root/pilot-predeploy/pilot.env-$(date -u +%Y%m%dT%H%M%SZ)
echo "INFE_PILOT_CONTACT_EMAIL=you@example.com" | sudo tee -a /etc/cityu-mail-pilot/pilot.env
sudo systemctl restart cityu-mail-pilot-web
curl -s https://YOUR_HOST/privacy | grep -o 'mailto:[^"]*'
```
