# 平台兜底模型 key：装上、验上（v0.36.0，2026-09-14）

## 起因：官网说的是假的

v0.30.0 就把机制做完了（`INFE_PILOT_DEFAULT_MODEL_KEY`），但**生产上从来没配过它**。
后果不是"少了个便利功能"，而是**四处公开文案同时不成立**：

| 位置 | 原文 |
|---|---|
| 官网首页 | 「内测期间免费，给结论的那部分由**管理员出钱**」 |
| 隐私政策 | 「默认使用**管理员提供的 key 并由管理员付费**」 |
| 服务条款 | 同一句话 |
| 应用内 | 「内测期间默认用管理员提供的模型 key」 |

而事实是：5 个账号**全部自己带 key**；一个不带 key 的新用户注册、配好邮箱之后，
会在生成报告时撞上 `尚未配置可用的模型 API。`——申请、发码、注册、配邮箱，最后一步断掉。

`test_compliance` 只能保证**四处互相一致**，保证不了**生产真的配了**。
这是"测试全绿 ≠ 文案为真"的一个实例，值得记下来。

## 两个新东西

### 1. `pilot_app/set_platform_key.sh` —— 安全的安装方式

不能让人自己 `export KEY=...` 或手写 `sed -i`：

- `export` 会把 key 留在 shell 历史；`env $(cat pilot.env)` 会把**主密钥**暴露在进程表里
  （AGENTS §5 记的就是这个坑）。脚本用 `read -s`，key 只存在于内存。
- `pilot.env` 里**同时装着主密钥**，手写 sed 改坏了不是"重填一次 key"的代价。
  key 里出现 `/`、`&`、`\` 都是 sed 的语法字符——脚本用 `grep -v` + `printf` 重写整个文件，
  不经过 sed。**测试里专门有一条用 `sk-a/b&c\d=e+f` 验证它逐字保留。**
- 改之前自动备份到 `/root/pilot-predeploy/`，改完立刻重启并自检；自检不过会**告诉你怎么退回**。

用法：

```bash
sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh          # 交互，不回显
sudo bash … --provider volcengine_ark --model doubao-1-5-pro-32k \
             --base-url https://ark.cn-beijing.volces.com/api/v3      # 非 deepseek 必须给 --model
sudo bash … --remove                                                  # 回到"人人自带 key"
printf '%s\n' "$KEY" | sudo bash … --stdin                            # 从密码管理器喂进来
```

`--no-restart` / `--no-verify` 是给测试用的：判断能不能写靠 **`-w "$ENV_FILE"`**，
不是"是不是 root"，这样在临时目录里也能跑（在正式路径上它自然要求 sudo）。

### 2. `python -m pilot_app.manage check-model` —— 唯一真正试过这把 key 的东西

这个 key 装在环境文件里，**界面上没有任何按钮会用到它**。在此之前，"key 配好了没有"
只能等某个真实用户的报告失败才知道。现在一条最小调用就能回答，并且：

- **绝不打印 key**，也不打印任何可能含 key 的东西。供应商的错误信息有时会把请求原样回显，
  所以每一行输出都过一遍 `_scrub`（把 key 换成 `***已隐藏***`）。**一个会泄露它所检查的
  凭据的检查器，比没有检查器更糟。** 测试里有一条专门断言"错误信息里含 key 时也不出现在 stdout"。
- 失败时明确说下一步查什么（余额 / key 是否禁用 / 供应商与模型名是否匹配）。
- **不需要数据库**：它在打开数据库之前就分派了。第一版写在 `db.initialize()` 之后，
  于是在"还没装完的机器上"跑它会报权限错——而那正是有人要配 key 的时刻。

## 这一轮抓到的三个真问题

1. **`LC_CTYPE=C` 下 `$VAR（` 会吃掉一个字节。** 脚本里写 `echo "已写入 $ENV_FILE（…）"`，
   紧跟变量的是全角括号（`\xEF\xBC\x88`）。在 `LC_CTYPE=C` 的 shell 里，bash 把 `\xEF`
   当成变量名的一部分 → `ENV_FILE?: unbound variable` → 在 `set -u` 下**整段中止**。
   在我自己的交互终端里（UTF-8 locale）它是好的，在 `bash` 工具里（C locale）它坏了——
   同一份脚本，两种行为。**修法：中文紧跟变量时一律写 `${VAR}`。**
   这个是测试抓出来的（测试特意把子进程设成 `LC_CTYPE=C`），不是我肉眼看出来的。
2. **`chown root:root` 让整次写入无声失败。** macOS 连 root 组都没有，chown 失败 → `set -e`
   中止 → 脚本只打印了备份那一行就退出，key 根本没写。现在改成**保留原文件属主**且失败不致命。
3. **改 `providers.MODEL_TIMEOUT_SECONDS` 泄漏到了别的测试模块。** 为了不让三 token 的探针
   继承 300 秒预算，我改了模块全局——它随后污染了 `test_providers` 里一条既有断言
   （`MODEL_TIMEOUT_SECONDS >= 180`）。改成 `try/finally` 用完即还原。
   **这是本项目第 n 次"共享模块状态跨测试泄漏"**；顺带说明那条断言写得值。

## 生产状态

- v0.36.0 已部署，`check-model` 在生产上正确报告"没有配置平台兜底 key"。
- **配 key 这一步刻意没有代劳**：那需要运营者的新凭据与付款，属于必须由人做的决定。
  命令已交给运营者，配完用 `check-model` 验证。
- 配好之后，四处文案**不需要改一个字**——它们会从"假话"变成"真话"。
  反之若决定不配，那四处（+ `test_compliance`）需要一起改成"请自备 key"。

## 追加：搜索 key 也能实例级兜底（v0.37.0）

用户接着说「我还要填写豆包搜索的key」。查了一下发现一件更该说的事：

**4 个账号的 model key 长度全是 87、search key 长度全是 83**——几乎可以肯定运营者
把同一把 DeepSeek key 和同一把豆包搜索 key **分别手发给了每个人**。所以"管理员出钱"
这件事**事实上成立**，只是**机制**上靠人工分发，而官网写的是"默认用管理员提供的 key"
（那说的是平台兜底开关，它没开）。后果依旧是真实的：**运营者不发，新用户就没有**；
而且**搜索是可选步骤**，一个没拿到搜索 key 的人只是**安静地失去联网核实**，
界面上什么都不会说。

### 做了什么

| 层 | 变化 |
|---|---|
| `providers.py` | `INFE_PILOT_DEFAULT_SEARCH_KEY` / `_PROVIDER`（默认 `doubao`）/ `_BASE_URL`；`platform_search_default()`、`platform_search_key()` |
| `service.py` | `connection_key()` **按 kind 选变量**（模型 key 与搜索 key 是两家供应商的两个账号，一个变量装不下两个）；新增 `search_connection()`；**三处**搜索取值都改走它——包括生产实际在用的 **brief 路径**和「测试搜索」按钮 |
| `web.py` | `/api/me` 与看板对 `search` 做与 `model` 一样的"这是运营者的 key"处理；否则一个引用正常工作的用户会被告诉"未完成第 4 步" |
| 四处文案 | 官网披露块、隐私政策（数据表 + 数据流向段）、服务条款、应用内搜索面板各加一句：**检索词会发给搜索服务商，用管理员的搜索 key 时查询记录同样落在管理员账号下**。`test_compliance` 新增 4 条盯着 |
| `set_platform_key.sh` | 新增 `--search`（同一套安全写法）；`--search` 不接受 `--model`（搜索供应商没有模型名） |
| `manage.py` | 新增 `check-search`；查询词是**固定诊断串**，绝不取用户邮件里的内容——诊断本身不能成为泄露主题行的那件事 |

**仍然只有实例级两把 key、两个开关**，互不替代：只配模型不配搜索时，搜索仍显示"未配置"。

### 这一轮撞出来的最严重的一个 bug

`--search` 第一版把 `grep -vE` 的模式拼成 `^(A|B||C)=`——模型那组有四个变量名，
搜索那组只有三个，`NAME_VAR` 为空就留下一个**空的分支**。grep 直接报
`empty (sub)expression` 并退出，而模式后面跟着 `|| true`，于是**语法错误被当成
"没有匹配行"吞掉**：`$TMP` 是空的，`mv` 之后 **`pilot.env` 只剩新写的那一行——
主密钥、管理员邮箱、所有配置全没了**。

它没上过生产：`test_setting_search_does_not_touch_an_existing_model_key` 先撞上了。
修法两条，都要留着：

1. 模式**只由非空的变量名拼成**；
2. **不再 `|| true`**——区分 grep 的 0/1（正常）与 ≥2（真出错）并中止。
   `|| true` 用在这里等于"把读取配置文件失败当成文件是空的"。

这条值得单独记住：**在"读进来再整体写回去"的流程里，一个被吞掉的读取错误
就是一次数据销毁**。

---

## 追加：火山方舟的「原生联网搜索」（v0.63.27，2026-09-16）

### 它是哪条路，不是哪条路

方舟的**联网内容插件**（`web_search`）**只存在于 `/api/v3/responses`**。
同一个 `v3` 底座上的 OpenAI 兼容端点 `/chat/completions` **没有**这个服务端工具——
兼容层只翻译基础对话。所以：

| 预设 | 协议 | 能原生搜索吗 |
|---|---|---|
| `volcengine_ark`（Plan） | `ark` | ✗ |
| `volcengine_ark_openai`（标准 OpenAI 兼容） | `openai_chat` | ✗ |
| **`volcengine_ark_responses`（新）** | `openai_responses` | **✓** |

**不要把 `native_search` 加到前两个上**：它们的请求体里带了 `tools` 只会被拒或忽略，
而用户会以为自己在用搜索。`test_providers` 里有一条专门盯着这件事。

### 请求与返回

请求就是我们既有 Responses 路径的形状：

```json
{"model": "<你在方舟控制台开通的模型 ID 或接入点 ID>",
 "store": false, "max_output_tokens": 4000,
 "input": "<提示词>",
 "tools": [{"type": "web_search"}]}
```

**返回的形状我们接受两种**（`providers._openai_sources`）：

1. OpenAI 那种：`output[].content[].annotations[]` 里的 `url_citation`；
2. `web_search_call` 这一项里挂的来源列表（`sources` / `results` / `citations`，
   条目里的 `url`/`link`、`title`/`name` 都认）。

**为什么两种都收**：写这个功能的时候**没有方舟的 key 可以真机验证**。拿真端点试过：
本机 `pilot.env` 里的三把 key（模型、搜索、主密钥）打到
`POST https://ark.cn-beijing.volces.com/api/v3/responses` **全部**返回
`{"error":{"code":"AuthenticationError","message":"The API key format is incorrect…"}}`
——**端点是对的**（错路径会 404），但**没有一把是方舟的 key**。
押注单一形状一旦猜错，表现和「这个供应商不会搜索」一模一样，所以按文档形状写、
顺带认下邻居形状。

### 怎么自己验证（一条命令）

```bash
# 在有这把 key 的机器上；--model 填方舟控制台里的模型 ID / 接入点 ID
sudo systemd-run --uid=cityumail --property=EnvironmentFile=/etc/cityu-mail-pilot/pilot.env \
  --working-directory=/opt/cityu-mail-pilot --pipe --wait --collect \
  /opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage check-native-search \
  --provider volcengine_ark_responses --model <模型 ID>
```

它的判据不是「调用成功」，而是**带回了几条引用来源**：模型不搜索也能答得很好看，
**零引用 = 这次通话什么都没证明**（退出码非零，并把来源数打出来）。
固定诊断查询，绝不取任何用户邮件里的内容；key 只打印长度。

### 可选参数 `search_max_keyword`

方舟的插件接受一个单轮关键词上限（官方工具说明：1–50，默认 5）。它**是可选的**，
所以只在连接配置里显式写了合法值时才发这个字段——发一个供应商不认识的字段，
代价是整个请求失败。范围外的值（`0`、`51`、`many`）按「没配」处理而不是报错：
连接编辑器是自由文本，为了一个手滑打错的数字把联网搜索整个关掉更糟。

## 复核（2026-09-22）：为什么报告走**原生搜索**，而不是我们自己发检索词

那天我看到隐私政策里那句「开启联网核实时还会**从邮件主题提取检索词**发给搜索服务商（豆包）」，
一度以为「原生搜索」不合规，差点把两个报告入口（`_analyse` / `_brief`）都改成
「我们自己发检索词」。**这个方向是错的**，而且错在没先读这份文档与
`docs/open-items-2026-09-14.md` §24——那里写着 v0.63.27 的选择：

> 联网搜索以「供应商原生搜索」的形式做了（**不是我们自己发检索词，那条路仍然拒绝：
> 模型选出来的检索词本身就是外泄通道**）。

两条路的隐私形状是**反的**：

| | 谁执行检索 | 谁看到检索词 | 我们能不能看见 |
|---|---|---|---|
| **供应商原生搜索**（默认走这条） | 模型供应商（服务端工具） | **只有模型供应商** —— 而它本来就拿着整封信 | 看不见 |
| 我们自己发检索词 | 另一家搜索服务商（豆包/Tavily/Brave） | **多了一个第三方** | 看得见，所以只允许 `public_search_query()` 从**主题**派生 |

也就是说：原生搜索**不增加任何看到邮件内容的第三方**；而「我们自己发检索词」那条路，
恰恰因为多了一个第三方，才必须把检索词限制成主题派生、并且写进隐私政策。

**给下一个人的三条**：

1. 隐私政策那句话描述的是**第二条路**（我们自己发检索词），不是对原生搜索的约束；
2. `test_service.test_a_model_chosen_query_is_never_sent_to_a_search_provider` 现在钉着
   这个决定：支持原生搜索的供应商**一个外部检索词都不发**；不支持时发出去的词必须是
   主题派生的，且带不走正文里的任何字符串（测试里用一个金丝雀串验证）；
3. 真要改，先读 `open-items-2026-09-14.md` §24 与这份文档——**别把一条有意的安全决定
   当成 bug 修掉**。
