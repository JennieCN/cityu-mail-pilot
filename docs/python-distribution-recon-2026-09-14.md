# Python 非 Docker 分发方式调研（2026-09-14）

**调研对象**：一个约 1.3 万行、只用标准库 `http.server`（无 FastAPI/Flask）、SQLite 存储、1 个第三方依赖、跑在 Ubuntu + systemd + nginx + Let's Encrypt 单机上的 Python 项目，目标是"陌生人也能装上跑起来"。

**取证方式（只读）**：所有结论都来自我实际打开的页面/文件。GitHub 未认证 API 额度在开工时已被用尽（`x-ratelimit-remaining: 0`，reset = 2026-09-14 04:38:20Z），因此仓库元数据在额度重置后用 `https://api.github.com/repos/{owner}/{repo}` 每仓库调用 1 次抓取；**没有下载或执行任何脚本、二进制、容器或 CI workflow**。体积数字来自 HTTP `HEAD` 的 `Content-Length`，不代表我下载了产物。

**明确打不开 / 未取到的东西**：

| 目标 | 结果 |
|---|---|
| `raw.githubusercontent.com/linkedin/shiv/{main,develop,master}/README.rst` | 全部 404（改用 PyPI 页 `https://pypi.org/project/shiv/`） |
| `https://pex.readthedocs.io/en/latest/` | 跨域重定向 → 改读 `https://docs.pex-tool.org/`（成功） |
| `https://docs.netbox.dev/en/stable/installation/` | 跨域重定向 → 改读 `https://netboxlabs.com/docs/netbox/installation/`（成功） |
| `https://briefcase.readthedocs.io/...` | 跨域重定向 → 改读 `https://briefcase.beeware.org/...`（成功） |
| `https://briefcase.beeware.org/en/stable/reference/platforms/linux/index.html` | **404**；但同级 `.../linux/system.md`（GitHub raw）读到了 |
| `https://nfpm.goreleaser.com/docs/configuration/` | 打开但正文没渲染出来（截断）→ 改用 headscale 的 `.goreleaser.yml` 与 Tailscale 的 `cmd/mkpkg/main.go` 佐证 |
| `raw.githubusercontent.com/jordansissel/fpm/{main,master}/README.md` | 均 404；jsdelivr 的 fpm 1.15.1 文件清单里也没有 README 条目 → **fpm 的维护状态"未查到"** |
| `https://nfpm.goreleaser.com/` 首页 | 打开成功（只说明它能生成 deb/rpm/apk/ipk/arch/msix） |
| Nuitka 的真实项目佐证 | 多轮检索后**未查到**可验证的仓库/发布产物（见第 3 节，我不编） |
| `https://zulip.readthedocs.io/...` 全文 | 页面 200 但正文被超长导航挤掉，改用 `curl` + 文本提取只读关键行 |
| `grafana/build.go` | 404（文件已不存在），改用 `Makefile` 与 `packaging/deb/control/postinst`（都读到） |

---

## 0.5 本次取到的仓库元数据（GitHub API `https://api.github.com/repos/{owner}/{repo}`，2026-09-14 04:38Z 额度重置后每仓库 1 次）

全部仓库都取到了 `pushed_at` / `license.spdx_id` / `stargazers_count`。`NOASSERTION` 表示 GitHub 无法把仓库 LICENSE 映射成 SPDX 标识（不等于没有许可证，需要点进仓库自己确认）。

| 仓库 | stars | License (SPDX) | pushed_at | 语言 |
|---|---|---|---|---|
| `yt-dlp/yt-dlp` | 190,964 | Unlicense | 2026-08-30 | Python |
| `astral-sh/uv` | 89,791 | Apache-2.0 | 2026-09-14 | Rust |
| `syncthing/syncthing` | 88,545 | MPL-2.0 | 2026-09-14 | Go |
| `grafana/grafana` | 76,736 | AGPL-3.0 | 2026-09-14 | TypeScript |
| `pi-hole/pi-hole` | 60,885 | NOASSERTION | 2026-09-12 | Shell |
| `go-gitea/gitea` | 57,970 | MIT | 2026-09-14 | Go |
| `jellyfin/jellyfin` | 57,069 | GPL-2.0 | 2026-09-14 | C# |
| `Homebrew/brew` | 49,561 | BSD-2-Clause | 2026-09-13 | Ruby |
| `mitmproxy/mitmproxy` | 45,040 | MIT | 2026-09-10 | Python |
| `juanfont/headscale` | 43,826 | BSD-3-Clause | 2026-09-10 | Go |
| `searxng/searxng` | 37,017 | AGPL-3.0 | 2026-09-13 | Python |
| `tailscale/tailscale` | 36,439 | BSD-3-Clause | 2026-09-14 | Go |
| `k3s-io/k3s` | 33,953 | Apache-2.0 | 2026-09-12 | Go |
| `ArchiveBox/ArchiveBox` | 28,416 | MIT | 2026-09-11 | Python |
| `zulip/zulip` | 25,893 | Apache-2.0 | 2026-09-11 | Python |
| `navidrome/navidrome` | 23,535 | GPL-3.0 | 2026-09-14 | Go |
| `netbox-community/netbox` | 21,528 | Apache-2.0 | 2026-09-11 | Python |
| `mikf/gallery-dl` | 19,634 | GPL-2.0 | 2026-09-12 | Python |
| `janeczku/calibre-web` | 18,178 | GPL-3.0 | 2026-09-05 | Fluent |
| `goreleaser/goreleaser` | 16,038 | MIT | 2026-09-12 | Go |
| `mail-in-a-box/mailinabox` | 15,418 | CC0-1.0 | 2026-09-01 | Python |
| `Nuitka/Nuitka` | 15,128 | AGPL-3.0 | 2026-09-13 | Python |
| `pyinstaller/pyinstaller` | 13,095 | NOASSERTION | 2026-09-13 | Python |
| `pypa/pipx` | 12,964 | MIT | 2026-09-08 | Python |
| `matrix-org/synapse` | 12,101 | Apache-2.0 | 2024-04-26 | Python |
| `benbusby/whoogle-search` | 11,573 | MIT | 2026-08-14 | Python |
| `jordansissel/fpm` | 11,505 | NOASSERTION | 2026-08-26 | Ruby |
| `miniflux/v2` | 9,689 | Apache-2.0 | 2026-09-12 | Go |
| `rust-lang/rustup` | 7,038 | Apache-2.0 | 2026-09-14 | Rust |
| `pex-tool/pex` | 4,226 | Apache-2.0 | 2026-09-13 | Python |
| `pantsbuild/pants` | 3,827 | Apache-2.0 | 2026-09-13 | Python |
| `beeware/briefcase` | 3,349 | BSD-3-Clause | 2026-09-14 | Python |
| `goreleaser/nfpm` | 2,639 | MIT | 2026-09-10 | Go |
| `borgmatic-collective/borgmatic` | 2,324 | GPL-3.0 | 2026-09-13 | Python |
| `linkedin/shiv` | 1,945 | BSD-2-Clause | 2026-05-22 | Python |
| `pimutils/vdirsyncer` | 1,872 | NOASSERTION | 2026-09-04 | Python |
| `spotify/dh-virtualenv` | 1,628 | GPL-2.0 | 2024-04-27 | Python |

注：`matrix-org/synapse` 是重命名前的旧路径（pushed_at 停在 2024-04-26）；活跃仓库是 `element-hq/synapse`，本轮 60 次额度已用尽，未再单独取数。

---

## 1. `pipx`

**版本**：**1.17.2（PyPI 上传 2026-09-01）**，`Requires-Python >=3.10`（`https://pypi.org/pypi/pipx/json`）。仓库 `pypa/pipx`：★ 12,964 · MIT · pushed 2026-09-08

**适合什么场景**：只发布 CLI 入口点、依赖 PYPI、不想污染系统 Python 的小工具/服务；服务器上"装一个命令"最省心的方式之一。

**官方行为（我读的页面）**：

- `https://pipx.pypa.io/stable/explanation/scope.html`：pipx 只管"有 console entry point 的应用"，明确**不做** library venv、不做系统包管理，也不管 **"frozen pipx executables、operating-system packages、zipapp installers、repositories、signing keys"**。这条直接说明：pipx 不能成为 systemd 单元、`/etc` 配置、.deb、zipapp 的分发方案。
- 状态目录：`$PIPX_HOME/venvs/<name>`；可执行文件软链到 `$PIPX_BIN_DIR`（默认 `~/.local/bin`）；man page 软链到 `$PIPX_MAN_DIR`（`https://pipx.pypa.io/stable/explanation/comparisons.html`）。vdirsyncer 文档给出的实际路径是 `~/.local/pipx/venvs/vdirsyncer`。
- `pipx install --python`：你显式指定的解释器"有最终决定权"（pipx 不会因为包拒绝这个 Python 而替你改）；另有 `--fetch-python=never|missing|always`（`PIPX_FETCH_PYTHON`）决定要不要下载 python-build-standalone，`pipx interpreter list/prune/upgrade` 管理缓存（`https://pipx.pypa.io/stable/how-to/standalone-python.html`）。
- `pipx inject`：默认**不**把被注入包的入口点暴露到 PATH，要 `--include-apps`；`--include-deps` / `--include-resources-from` 才连带暴露依赖的入口点；`--pip-args`、`--index-url`、`--with-suffix` 等（`https://pipx.pypa.io/stable/how-to/inject-packages.html`）。
- `pipx upgrade <pkg>`：**沿用安装时的解释器**；默认**不**升级被 inject 的包，必须 `--include-injected`；pinned 的环境会被跳过（`--skip` 可排除）；要换 Python 得 `pipx reinstall --python` / `pipx reinstall-all --python python3.13`。`pipx uninstall` 会删 venv 并解掉 app/man 软链（`https://pipx.pypa.io/stable/how-to/manage-installed-apps.html`、`.../comparisons.html`）。

**真实项目佐证**：

1. **vdirsyncer**（自托管 CalDAV/CardDAV 同步工具）官方安装文档有专节 *"pipx: The clean, easy way"*，命令是 `pipx install vdirsyncer` / `pipx upgrade vdirsyncer` / `pipx uninstall vdirsyncer`，并有一句关键原话：**"Please note that installing via pipx will not include manual pages nor systemd services."** —— 这正是 pipx + systemd 关系的答案。`https://vdirsyncer.pimutils.org/en/stable/installation.html`。仓库 `pimutils/vdirsyncer`：★ 1,872 · NOASSERTION · pushed 2026-09-04
2. **Whoogle Search**（Flask + systemd 的自托管搜索前端）：README 同时给出 pipx 安装（`pipx install <github zip>`、`pipx run --spec git+... whoogle-search`）与一整份 `/lib/systemd/system/whoogle.service` 模板（`Type=simple`、`User=`、`ExecStart=<python_install_dir>/python3 /home/<user>/.local/bin/whoogle-search --host 127.0.0.1 --port 5000`、`Restart=always`、`WantedBy=multi-user.target`）。`https://github.com/benbusby/whoogle-search`。仓库 `benbusby/whoogle-search`：★ 11,573 · MIT · pushed 2026-08-14（注意：README 顶部声明项目已于 2026-07-24 停止维护，MIT 许可，但作为"pipx + 手写 systemd 单元"的样板仍然有效。）

**对你这个项目的坑**：

- pipx 装在用户家目录（默认），而 systemd **system** 服务不读 shell profile、不会自动有 `~/.local/bin` 的 PATH —— 单元里必须写绝对路径，且要么以该用户 `User=` 运行，要么用 `pipx install --global`（`PIPX_GLOBAL_*`）装到系统路径。
- 升级要显式：`pipx upgrade <pkg> --include-injected`，换 Python 要 `reinstall`。
- `/etc` 配置、secrets、SQLite 数据目录、卸载清理**都不是 pipx 的事**，仍要你自己写。

---

## 2. `uv` / `uv tool install` / `uvx`

**版本**：**uv 0.12.13（PyPI 上传 2026-09-10）**，PyPI 页 `Requires-Python >=3.8`（`https://pypi.org/pypi/uv/json`）。仓库 `astral-sh/uv`：★ 89,791 · Apache-2.0 · pushed 2026-09-14

**适合什么场景**：同一件事想要更快的安装/解析、想要"一个二进制顺带管理 Python 版本"；`uvx` 适合一次性运行，`uv tool install` 适合常驻。

**官方行为（我读的页面）**：

- `https://docs.astral.sh/uv/guides/tools/`：`uvx` = `uv tool run`（临时隔离环境，用完即走）；`uv tool install <pkg>` 持久安装并把可执行文件放进 PATH 的 `bin` 目录，不在 PATH 时提示 `uv tool update-shell`；`uv tool install` 装的是**包**（该包提供的所有可执行文件都会装），`uvx` 面向命令。
- 升级：`uv tool upgrade <tool>` **遵守安装时写下的版本约束**（`uv tool install 'ruff>=0.3,<0.4'` 之后 upgrade 不会越界），要换约束得重新 `uv tool install 'ruff>=0.4'`；`--all` 全升；`--python 3.10` 可在升级时指定解释器；版本选择 `cmd@version` / `--from 'pkg==x'`。
- 隔离：`uv pip install` 与 `uv tool install` 是两回事，装成 tool 后 `import ruff` 仍会失败（文档原话举例），这正是我们要的隔离。
- 安装 uv 本身：`curl -LsSf https://astral.sh/uv/install.sh | sh`；`uv self update` 自更新（会重跑安装器、可能改 shell profile，可 `UV_NO_MODIFY_PATH=1` 禁掉）；卸载是 `rm ~/.local/bin/uv ~/.local/bin/uvx` 加 `uv cache clean` / `rm -r "$(uv tool dir)"`（`https://docs.astral.sh/uv/getting-started/installation/`）。
- 与 pipx 的差异与坑（`https://pipx.pypa.io/stable/explanation/comparisons.html`，pipx 官方对比页）：状态在 `$UV_TOOL_DIR/<name>`、bin 在 `$UV_TOOL_BIN_DIR`（两者默认都是 `~/.local/bin`，与 pipx 相同 → 会互相拒绝覆盖，需 `--force`）；**uv 不暴露 man page**；uv venv **不带 pip**（所以没有 `pipx runpip` 等价物，加依赖只能 `uv tool install --with ...` 重建）；`uvx` 会复用缓存环境，直到 `uv cache clean` / 换版本 / `--refresh`；`uvx` 若发现已有持久安装会优先复用（要 `--isolated` 才强制临时）；`uv tool` **不读项目里的 `.python-version`**；`uv python upgrade` 只升 patch，要 `uv tool upgrade --all -p 3.13` 换大版本。

**真实项目佐证**：uv 官方文档明确推荐用 pipx 装 uv 自身（`pipx install uv`）。`uv tool install` 的经典公开例子是 `uv tool install --with-executables-from ansible-core,ansible-lint ansible`（同上 tools 页）。我**没有**找到一个"自托管服务器应用官方推荐 `uv tool install`"的强佐证，标记为**未查到**（uvx 的公开使用面主要是一次性 CLI）。

**对你这个项目的坑**：和 pipx 完全一样 —— uv tool 也不生成 systemd 单元、不碰 `/etc`；升级粒度是"整个工具环境重建"；`~/.local/bin` 与 systemd 的 PATH 问题同样存在。

---

## 3. `PyInstaller` / `Nuitka` 单二进制

### PyInstaller

仓库 `pyinstaller/pyinstaller`：★ 13,095 · NOASSERTION · pushed 2026-09-13（license 字段是 `NOASSERTION`，因为它其实是 GPLv2-or-later **加一个例外条款**，PyPI 页写明了）。

- 版本与支持范围：**6.22.3（PyPI 上传 2026-09-12）**，"Works out-of-the-box with any Python version **3.8–3.15**"，`Requires-Python: <3.16,>=3.8`，License = **GPLv2-or-later with a special exception**（允许用它打包并分发非自由软件）。`https://pypi.org/project/pyinstaller/`
- Linux 依赖 `ldd`、`objdump`、`objcopy`（binutils）；**不是交叉编译器**，要在目标平台构建。`https://pyinstaller.org/en/stable/requirements.html`
- 单文件（onefile）坑（`https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html`）：
  - 6.0 起 POSIX 产物**大量使用符号链接**，onefile 运行时会自解压到临时目录，**该目录所在文件系统必须支持符号链接**，否则解包失败；onedir 的归档/拷贝也必须保留符号链接（`zip` 要显式 `-y`）。
  - bootloader 会改 `LD_LIBRARY_PATH`（原值存到 `LD_LIBRARY_PATH_ORIG`），**子进程会继承**，所以从冻结程序里 `subprocess` 调系统程序（nginx/openssl 之类）可能因库版本冲突失败，要先清/还原该变量。
  - `multiprocessing` 必须在入口调用 `freeze_support()`，否则**无限自旋生成进程**；其它多进程框架一律不支持。
  - 用 `sys.executable` 起一个要比自己活得久的子进程（自重启场景）必须设 `PYINSTALLER_RESET_ENVIRONMENT=1`（6.9 起）。
  - 6.22.1 起 bootloader 加了对内部环境变量的安全校验，伪装 `sys._MEIPASS` 会直接报 `Security validation failure:`。
- **真实项目 + 体积（同一个项目同时给出 zipapp 与 PyInstaller 两个样本）**：**yt-dlp**。`Makefile` 里 `pyinstaller==6.22.0` 是可选构建依赖（`pyproject.toml` 第 121–122、146–147 行有 `[project.entry-points.pyinstaller40] hook-dirs`）；发布资产 `yt-dlp_linux` 我用 `HEAD` 量到 **40,446,224 B ≈ 38.6 MiB**（`https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux`）。仓库 `yt-dlp/yt-dlp`：★ 190,964 · Unlicense · pushed 2026-08-30
- 另一个常被引用的 PyInstaller 用户是 **mitmproxy**（其发布页提供打包产物），但我在本轮没有实际量到它的资产体积，标**未查到体积**。

### Nuitka

仓库 `Nuitka/Nuitka`：★ 15,128 · AGPL-3.0 · pushed 2026-09-13

- 版本与支持范围：**4.2.1（PyPI 上传 2026-09-05）**；自述支持 **Python 2.6/2.7 与 Python 3.4–3.14**。License = **AGPL-3.0 + runtime exception**（`LICENSE-RUNTIME.txt`）。`https://pypi.org/project/Nuitka/`
- 必须有 C 编译器（gcc ≥5.1 或 clang；MinGW64 **不支持 Python 3.13+**）；`--mode=standalone` 产出目录、`--mode=onefile` 产出单文件；**onefile 会在目标机临时目录自解压**（可 `--onefile-tempdir-spec` 指定缓存路径）。
- 官方列出的典型坑（同上 PyPI 长文档）：
  - **Linux standalone 很难跨发行版**："主要是 glibc 版本被编进二进制，必须在你最老的受支持 OS 上构建"；Nuitka 自己是靠商业版提供容器构建方案（CentOS 7 基线）。
  - `--deployment`（默认开）会拦"程序用 `-m` 参数自调用"，可能和 multiprocessing/joblib/loky 打架（严重时 fork bomb），需 `--no-deployment-flag=self-execution`；另有一段 `NUITKA_LAUNCH_TOKEN` 的自保代码示例。
  - 动态导入必须显式 `--include-module` / `--include-package` / `--include-plugin-directory`；数据文件用 `--include-data-files` / `--include-data-dir` / `--include-package-data`；**`.py/.pyc/.so/.dll` 不能当 data file 塞**（表格里逐条说明）。
- **真实项目佐证：未查到**可验证的仓库/发布产物（我试过官方 README、Nuitka-Action 方向与多轮检索，均未拿到能打开并验证的强样本）。因此 Nuitka 的"产物体积"也标**未查到**。

**适合什么场景**：想给最终用户一个"不含 Python 环境"的 CLI/桌面程序。**对你（无 GUI 的后台服务 + nginx + SQLite + systemd）不划算**：体积从几 MiB 涨到几十 MiB（yt-dlp 实测 2.93 MiB zipapp → 38.6 MiB PyInstaller，约 13×），还要处理符号链接/临时目录/子进程库污染，而 systemd/nginx/证书这一整套一点都没省。

---

## 4. `zipapp` / `.pyz` 单文件（stdlib 自带）

- 用法：`python -m zipapp myapp -m "myapp:main" -p "/usr/bin/env python3" -c`，产出可直接执行单文件；本质是标准 zip + 可选 shebang + 根目录 `__main__.py`。`https://docs.python.org/3/library/zipapp.html`
- **官方 Caveats（关键限制）**：**含 C 扩展的依赖不能从 zip 里运行**（OS 加载器必须在文件系统上找到可执行代码），只能把 C 扩展排除、随包附带并按 `sys.path` 挂上对应架构的二进制。此外 shebang 无法表达"python X.Y 或更高"，写死版本会挑用户环境。pipx 的 scope 页也明确把 **zipapp installers** 排除在 pipx 管理范围外。
- **真实项目 + 体积**：**yt-dlp** 的 `yt-dlp` 发布资产就是 zipapp —— 我读了它的前 130 字节：`#!/usr/bin/env python3` 紧跟 `PK`；`Makefile` 第 106–130 行手工 `zip` 出 `yt-dlp.zip` 再 `cat yt-dlp.zip >> yt-dlp`（先写 shebang）。体积 **3,072,469 B ≈ 2.93 MiB**（HEAD 实测）。仓库 `yt-dlp/yt-dlp`：★ 190,964 · Unlicense · pushed 2026-08-30
- 另一个现实样本：pipx 文档提到 *"You can even create a pyz of shiv using shiv"*（shiv 的 PyPI 页），说明这类工具生态是互通的。

**适合什么场景**：**纯 stdlib 或纯 Python 依赖**的 CLI。你这个项目"只用 stdlib + 1 个依赖"，恰好是 zipapp 的甜点区：产物只有几 MiB、零额外工具链、`python3 xxx.pyz` 即跑。**局限**：仍然要求目标机有 Python 3.14（或兼容版本）；不生成 systemd 单元/`/etc` 配置/卸载逻辑；依赖版本不锁（要自己 `pip install --target` 或 vendor 进去）。

---

## 5. `shiv` / `pex`

### shiv

- **1.0.8，PyPI 上传 2024-11-01**（此后无新版本）；`Requires-Python >=3.6`，classifiers 只到 3.11；License 徽章为 **BSD 2-Clause**。`https://pypi.org/project/shiv/`（README 三个分支的 raw 都 404，见开头说明）。仓库 `linkedin/shiv`：★ 1,945 · BSD-2-Clause · pushed 2026-05-22
- 用法：`shiv -c flake8 -o ~/bin/flake8 flake8`，接受几乎所有 `pip install` 参数。
- **明确写出的坑（PyPI 页原文）**：(1) 产物**不能保证跨架构**（含 C 扩展时）；(2) **zipapp 会自解压到 `~/.shiv`**，除非用 `SHIV_ROOT` 覆盖，"装多了要偶尔清理"。

### pex

- **2.102.0，PyPI 上传 2026-09-13**（维护活跃）；`Requires-Python: >=2.7,<3.16`；来自 **twitter/commons**，"PEX 文件自 2011 年起被 Twitter 用于生产部署"。`https://docs.pex-tool.org/`
- 自家发布产物（自举的 `.pex`）体积：**5,318,386 B ≈ 5.07 MiB**（`HEAD https://github.com/pex-tool/pex/releases/download/v2.102.0/pex`）。仓库 `pex-tool/pex`：★ 4,226 · Apache-2.0 · pushed 2026-09-13
- **长跑守护进程的坑（官方 recipes）**：非 `--venv`/非 `--layout loose` 的 PEX 会**二次 exec 到 `PEX_ROOT` 里解压出的版本**，`ps` 里看不到原始 pex 路径（文档给了 `ps -o command | grep pex` 的实际输出），建议把 `setproctitle` 作为依赖打进 PEX；配 gunicorn/uvicorn 时要把应用服务器本身当依赖并设入口点（PEX 不能"进到 zip 里"取 WSGI 对象）。
- **真实项目佐证**：pex 文档有专节 *Using Pants*（`pantsbuild/pants` 用 PEX 打包 Python 目标），且其文档自述 Twitter 生产使用 2011 年至今。`pantsbuild/pants`：★ 3,827 · Apache-2.0 · pushed 2026-09-13

**适合什么场景**：需要"一个文件 = 一个已解析好的依赖环境"、且希望有 venv/loose 布局以便调试。两者都**不提供** systemd/`/etc`/升级/卸载，且 PEX/shiv 的解压目录（`PEX_ROOT`/`~/.shiv`）会成为第 N 个需要清理的状态。

---

## 6. `briefcase` / `fpm` / `dh-virtualenv` / `nfpm`（→ .deb / .rpm）

### briefcase

- **0.4.5，PyPI 上传 2026-09-08**，`Requires-Python >=3.11`。仓库 `beeware/briefcase`：★ 3,349 · BSD-3-Clause · pushed 2026-09-14
- Linux 目标格式（`docs/en/reference/platforms/linux/system.md`，已读）：**`.deb`、`.rpm`、`.pkg.tar.zst`**，以及 AppImage、Flatpak。**关键坑（原文）**：system 包"**会用操作系统自带的 Python3 和标准库**"，与宿主 Python 版本无关，需要额外做平台测试；签名分别依赖 `debsigs` / `rpmsign` / `gpg`；构建可用 Docker 基础镜像（`debian:bookworm`、`ubuntu:22.04`…）。
- 定位是**桌面/移动 App 打包**（desktop entry、splash、app bundle 那一套）。对一个"无 GUI 的后台服务"是绕路：你要的 systemd 单元、`/etc` 归属、服务用户、卸载时的数据保留策略，briefcase 并不替你做。

### fpm

- Ruby gem，历史上是"一条命令打 deb/rpm"的万能工具。**本次未取到 README/维护状态**（`main`/`master` raw 均 404，jsdelivr 1.15.1 清单里也没有 README）→ 维护状态**未查到**。仓库 `jordansissel/fpm`：★ 11,505 · NOASSERTION · pushed 2026-08-26
- **真实项目佐证（强）**：**Grafana** 至今用 fpm 打 deb/rpm —— `Makefile` 第 427–453 行：`build-deb: $(DEB_FILE) ## Build a .deb package from a tar.gz (requires fpm)` → `bash scripts/build-deb.sh`，`build-rpm` 同理。我打开了它的 `packaging/deb/control/postinst`：按 `$1 = configure` 判定，`$2` 非空代表升级（`IS_UPGRADE`），并 source `/etc/default/grafana-server`。仓库 `grafana/grafana`：★ 76,736 · AGPL-3.0 · pushed 2026-09-14

### dh-virtualenv

- 官方文档 1.2.2（文档页脚版权 2013–2018，**项目本身明显偏老**）：把预先构建好的 venv 塞进 deb，插入 debhelper 序列（替换 `dh_auto_install` 等）。`https://dh-virtualenv.readthedocs.io/en/latest/`。仓库 `spotify/dh-virtualenv`：★ 1,628 · GPL-2.0 · pushed 2024-04-27
- **真实项目佐证（强，且是 Python 服务）**：**Matrix Synapse** 的 `debian/rules` 第一行注释就是 `# Build Debian package using https://github.com/spotify/dh-virtualenv`，并且有 `override_dh_installsystemd: dh_installsystemd --name=matrix-synapse` —— 即 **dh-virtualenv 负责 Python 环境、debhelper 负责 systemd 单元**。仓库 `matrix-org/synapse`（现重定向到 element-hq）：★ 12,101 · Apache-2.0 · pushed 2024-04-26（旧路径 matrix-org/synapse 的数据；活跃仓库为 element-hq/synapse，本轮 API 额度已尽未单独取数）
- 官方 *Real-World Projects Show-Case* 还列了 **debianized-sentry**（把 Sentry 的 Django/uWSGI 打包成 deb，**含 systemd 集成**与默认配置）、**debianized-jupyterhub**、**configsite**（ARM 交叉打包）。`https://dh-virtualenv.readthedocs.io/en/latest/examples.html`

### nfpm

- 官方首页（已读）：单个 Go 二进制，生成 **deb、rpm、apk、ipk、arch linux、msix**，"no Ruby, no tar, no external dependencies"。`https://nfpm.goreleaser.com/`。仓库 `goreleaser/nfpm`：★ 2,639 · MIT · pushed 2026-09-10
- **真实项目佐证 1 —— headscale（自托管 Tailscale 控制端）**：`.goreleaser.yml` 的 `nfpms:` 段落我逐行读了，模式非常完整，几乎可以直接抄：
  - `contents:` 把 `config-example.yaml` 装到 `/etc/headscale/config.yaml`，类型 **`config|noreplace`**（升级不覆盖用户改过的配置）；
  - systemd 单元装到 `/usr/lib/systemd/system/headscale.service`（**不是** `/etc/systemd/system`，这是包管理器的规范位置）；
  - `/var/lib/headscale` 用 `type: dir` 建数据目录；
  - `scripts: postinstall/postremove/preremove` 挂 Debian maintainer scripts；
  - `deb.lintian_overrides`。
  仓库 `juanfont/headscale`：★ 43,826 · BSD-3-Clause · pushed 2026-09-10
- **真实项目佐证 2 —— Tailscale**：自己写了 `cmd/mkpkg/main.go` 作为 nfpm v2 的薄封装（import `github.com/goreleaser/nfpm/v2`、`/deb`、`/rpm`、`/files`），`-type deb|rpm`，并把"普通文件"与 **`files.TypeConfig`** 分开处理 → 配置文件在升级时会走 dpkg 的 conffile 逻辑。仓库 `tailscale/tailscale`：★ 36,439 · BSD-3-Clause · pushed 2026-09-14
- **真实项目佐证 3 —— Miniflux**（Go，但它的 deb 用经典 debhelper，是"install 脚本 + systemd + /etc"最干净的参考）：
  - `packaging/debian/control`：`Build-Depends: debhelper (>= 9.20160709) | dh-systemd`，`Depends: ${misc:Depends}, ${shlibs:Depends}, adduser`；
  - `packaging/debian/miniflux.postinst`：`case "$1" in configure) adduser --system --disabled-password --disabled-login --home /var/empty --no-create-home --quiet --force-badname --group miniflux`；
  - `packaging/debian/miniflux.dirs`：`etc`、`usr/bin`；
  - `packaging/systemd/miniflux.service`：`EnvironmentFile=/etc/miniflux.conf`、`User=miniflux`、`Type=notify` + `WatchdogSec=60s`、`Restart=always`，以及一整段硬化：`ProtectSystem=strict`、`ProtectHome=yes`、`PrivateTmp=yes`、`PrivateDevices=yes`、`NoNewPrivileges=yes`、`RestrictNamespaces=yes`、`ProtectKernelModules/Tunables/Logs/Clock`、`RestrictRealtime=yes`、`LockPersonality=yes`、`MemoryDenyWriteExecute=yes`、`SystemCallArchitectures=native`。
  仓库 `miniflux/v2`：★ 9,689 · Apache-2.0 · pushed 2026-09-12
- **maintainer script 的标准模板（headscale `packaging/deb/postinst`，我完整读过）**：建系统组/用户（`groupadd --force --system`、`useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/headscale`）→ `deb-systemd-helper --quiet was-enabled` 判断后 `enable`，否则只 `update-state`（**已禁用的服务不会因为升级而被重新启用**）→ `systemctl --system daemon-reload` → **`$2` 非空（升级）走 `deb-systemd-invoke restart`，否则 `start`**。这就是"重新安装新版本而不丢 /etc 与数据"的完整答案。

**适合什么场景**：想让陌生人 `apt install ./app.deb`（或加官方源 `apt install app`）就能拿到"单元 + 服务用户 + /etc 配置 + dpkg 卸载"。代价：要维护 deb/rpm 两套（或至少一套 + 源）、要处理 conffile 提示、要签包。

---

## 7. `systemd` 用户单元（`systemctl --user` + `loginctl enable-linger`） vs system 单元

**事实（Arch Wiki `https://wiki.archlinux.org/title/Systemd/User`，我通篇读过）**：

- user 单元放 `~/.config/systemd/user/`（另有 `/usr/lib/systemd/user/`、`/etc/systemd/user/` 按优先级生效）；`systemctl --user enable unit` 自启；单元 `[Install] WantedBy=default.target`（**不是** `multi-user.target`）；root 可 `systemctl --global enable` 对所有用户生效。
- `systemd --user` 实例是**按用户**而不是按会话；**user 单元不能引用/依赖 system 单元，也不能依赖别的用户的单元**；文档提醒"需要跑在会话内的程序在 user 服务里多半会坏"。
- 默认用户实例在**最后一个会话关闭时被杀**；要不登录也常驻、开机自启，必须 **`loginctl enable-linger [username]`**（给自己可以 `loginctl enable-linger`，给别人/无 polkit 时要 root）。撤销是 `loginctl disable-linger`。
- user 单元**不继承** `.bashrc`/`.profile` 的环境变量；要在 `~/.config/environment.d/*.conf`、`/etc/systemd/user.conf` 的 `DefaultEnvironment`、或 `user@UID.service.d/*.conf` 里设；`PATH` 可用 `systemctl --user import-environment PATH` 导入（且只影响之后启动的单元）。
- 日志：`journalctl --user -u myunit`；**UID < 1000 的用户不会写 user journal**（全部进 system journal）。

**真实项目佐证**：**Syncthing** 同时发布 user 与 system 单元 —— 仓库里存在 `etc/linux-systemd/user/syncthing.service` 与 `etc/linux-systemd/system/syncthing@.service`（jsdelivr 文件清单 + 我读了 user 单元内容：`ExecStart=/usr/bin/syncthing serve --no-browser --no-restart`、`SuccessExitStatus=3 4`，以及 `NoNewPrivileges=true`、`MemoryDenyWriteExecute=true`、`SystemCallArchitectures=native` 等硬化）。仓库 `syncthing/syncthing`：★ 88,545 · MPL-2.0 · pushed 2026-09-14

**优点**：不需要 root、不碰系统目录、装卸全在家目录、天然多用户。
**对你的场景的坑（关键）**：你的服务栈里 **nginx（80/443）、certbot 续期、防火墙、`/etc/letsencrypt` 都是 system 侧**；陌生安装者反正要给 sudo。"user 单元 + linger"只在"这个人本来就是这台机器的主人、且服务只需监听高端口"时才省事；而且 `loginctl enable-linger <别人>` 本身就要 root/polkit，等于没省掉 sudo。**结论：你要的应该是 system 单元**；user 单元只在你打算把服务做成"每用户一份"时才有价值（Syncthing 的 `syncthing@.service` 模板就是这个用途）。

---

## 8. Homebrew / 源码 tar 包 + `install.sh` 惯例

### Homebrew

- `brew services` 现在是跨平台的：homebrew-services 的 README 原话是 *"manage background services using the daemon manager `launchctl` on macOS or **`systemctl` on Linux**"*（`https://github.com/Homebrew/homebrew-services`）；`brew services [start|stop|restart|run|list] [sudo]` 的语法在 `https://docs.brew.sh/Manpage` 里。仓库 `Homebrew/homebrew-services` 未单独抓元数据；主仓库 `Homebrew/brew`：★ 49,561 · BSD-2-Clause · pushed 2026-09-13
- **真实 Python 打包方式（公式）**：`beets` 公式用一堆 `resource "..." do` 钉住每个 PyPI 依赖 + `depends_on "python@3.14"`（即 Homebrew 自己复刻了一份 lockfile），这是"用 brew 发 Python 应用"的标准代价。
- **真实服务写法（公式里带 service 块）**：`grafana` 公式的 `service do run [opt_bin/"grafana", "server", "--config", etc/"grafana/grafana.ini", "--homepath", opt_pkgshare, "--packaging=brew", "cfg:default.paths.logs=#{var}/log/grafana", "cfg:default.paths.data=#{var}/lib/grafana", "cfg:default.paths.plugins=#{var}/lib/grafana/plugins"] keep_alive true ... working_dir var/"lib/grafana" end` —— 配置在 `etc/`、数据在 `var/lib/`、日志在 `var/log/`，这套目录约定与你想要的 `/etc/<app>` + `/var/lib/<app>` 完全一致。
- **真实 Python CLI 用 brew 发**：vdirsyncer 的安装文档把 `https://formulae.brew.sh/formula/vdirsyncer` 列为 macOS 的官方途径。
- 局限：Linux 上要装 Linuxbrew（另一套 prefix，和系统 Python/依赖隔离），服务默认以安装用户身份运行；要发自己的包得建 tap；**服务器运维者通常不用 brew 装系统服务**。

### `install.sh` 惯例（我实际读了这些脚本的源码）

| 项目 | 脚本 | 我读到的惯例 |
|---|---|---|
| Tailscale | `https://tailscale.com/install.sh`（740 行） | `uname` + `/etc/os-release` 组合探测发行版（读 `VERSION_ID`/`VERSION_CODENAME`），按发行版分流 `apt/dnf/yum/zypper`，**添加官方仓库后再装包**（systemd 单元由 deb/rpm 携带，脚本自己不写单元）；支持 `TAILSCALE_VERSION=`、`TRACK=unstable` 环境变量 |
| k3s | `https://get.k3s.io`（1218 行） | 一整套 `INSTALL_K3S_*` 环境变量（`INSTALL_K3S_VERSION`、`INSTALL_K3S_SKIP_START`、`INSTALL_K3S_SKIP_ENABLE`、`INSTALL_K3S_SYSTEMD_DIR` 默认 `/etc/systemd/system`…），下载单个二进制 → 写 systemd 单元+env 文件 → enable/start，并**附带 uninstall 脚本**；`INSTALL_K3S_EXEC` 决定 server/agent 参数 |
| uv | `https://astral.sh/uv/install.sh` | 装到 `~/.local/bin`，`UV_NO_MODIFY_PATH=1` 控制是否改 profile，安装后 `uv self update` 自更新；卸载 = 删二进制 + `uv cache clean` + 删 `uv tool dir` |
| Pi-hole | `https://raw.githubusercontent.com/pi-hole/pi-hole/master/automated%20install/basic-install.sh`（2539 行） | 配置全在 **`/etc/pihole/`**（`setupVars.conf`、`adlists.list`、`install.log`），有 `/etc/pihole/migration_backup_v6` 这类**升级前迁移备份目录**，脚本里大量 `systemctl stop/restart/enable`，并安装自己的 `uninstall.sh` |
| Mail-in-a-Box | `setup/start.sh` 等 `setup/*.sh` | 参数/全局选项落到 **`/etc/mailinabox.conf`**（升级时先读旧文件再写回），有 `STORAGE_ROOT`，用 `openssl` 生成证书/密钥，按模块分脚本（`dns.sh`、`ssl.sh`、`management.sh`、`web.sh`…） |
| **Zulip**（最接近你的形态） | `zulip-server-*/scripts/setup/install` | 发布 tar 包 + 安装脚本；配置在 **`/etc/zulip`**（deployment 页正文直接出现 `/etc/zulip`、`[zulip_notify]` 段落、`bot user in /etc/zulip`，并有 secrets 相关章节）；部署目录是 **`/home/zulip/deployments/<version>/` + `current` 软链**，升级跑 **`/home/zulip/deployments/current/scripts/upgrade-zulip`**；进程管理用 **supervisord + Puppet**（不是 systemd）—— 这是"Python 服务不用 systemd 也能成熟运维"的现成反例。仓库 `zulip/zulip`：★ 25,893 · Apache-2.0 · pushed 2026-09-11 |

### 非 Docker 世界"systemd 单元 + 配置目录 + 密钥 + 升级 + 卸载"的成熟写法（可照抄的清单）

1. **配置与密钥分离**：`/etc/<app>/<app>.conf`（或 `/etc/<app>.conf`）归 root/服务组，权限 640（Gitea 文档：`chmod 750 /etc/gitea; chmod 640 /etc/gitea/app.ini`）；secrets 单独文件（Zulip 的 `/etc/zulip` 系列；NetBox 用 `generate_secret_key.py` 生成 `SECRET_KEY` 写进 `configuration.py`；Mail-in-a-Box 用 openssl 写入 `/etc/mailinabox.conf`）。
2. **数据目录**：`/var/lib/<app>`（headscale/`var/lib/gitea`/`var/lib/grafana`），单元里用 `WorkingDirectory=` 指向它；SQLite 放这里，升级脚本**永不删**。
3. **服务用户**：`adduser --system --group <app>`（NetBox）或 `useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/<app>`（headscale postinst）；单元里 `User=`/`Group=`。
4. **单元硬化**：直接抄 miniflux 那段（`ProtectSystem=strict`、`ProtectHome`、`PrivateTmp`、`NoNewPrivileges`、`MemoryDenyWriteExecute`…），再按你的程序需要 `ReadWritePaths=/var/lib/<app>`。
5. **升级 = 新版本目录 + 切软链 + 显式保留配置**：NetBox 的做法是解压到 `/opt/netbox-4.5.0` → `ln -sfn` 到 `/opt/netbox` → **显式 `cp` `configuration.py`、`local_requirements.txt`、自定义 scripts/reports/media** → 跑 `./upgrade.sh`（内部做依赖安装/迁移/静态文件/重启）；Gitea 是"换二进制 + 保留 `/etc/gitea/app.ini` + `/var/lib/gitea`"；Zulip 是 deployments 目录 + `upgrade-zulip`。**这三种都不依赖 Docker 卷，靠的是"代码目录可丢弃、`/etc` 与 `/var/lib` 不可丢弃"这条纪律。**
6. **卸载**：deb 用 `prerm`/`postrm` + `deb-systemd-helper`（headscale）；`install.sh` 流派自带 `uninstall.sh`（k3s、Pi-hole）；**默认不删数据目录**，把删数据的决定留给用户。
7. **重装/升级时不要覆盖用户配置**：dpkg 用 conffile 机制，nfpm 用 `config|noreplace`，你自己的 install.sh 用"文件存在则跳过 + 打印合并提示"。

---

### 可照抄的最小骨架（按 miniflux / headscale / NetBox / k3s 的写法拼，**未实测**，仅供改造）

```text
目录布局（"代码可丢弃、/etc 与 /var/lib 不可丢弃"）
  /opt/<app>/releases/<version>/        代码 + .venv（可丢弃）
  /opt/<app>/current -> releases/x.y.z  软链，升级只切这里
  /etc/<app>/app.conf                   root:<app> 0640
  /etc/<app>/secrets.env                root:<app> 0640（只有这里放密钥）
  /var/lib/<app>/app.sqlite3            <app>:<app> 0700（永不删）
  /usr/local/bin/<app>-manage -> /opt/<app>/current/bin/<app>-manage

/etc/systemd/system/<app>.service
  [Unit]
  Description=<app>
  After=network-online.target
  Wants=network-online.target
  [Service]
  Type=simple
  User=<app>
  Group=<app>
  WorkingDirectory=/var/lib/<app>
  EnvironmentFile=/etc/<app>/secrets.env
  ExecStart=/opt/<app>/current/.venv/bin/python -m <pkg>.web --host 127.0.0.1 --port 8787
  Restart=always
  RestartSec=3
  # 以下硬化逐条抄自 miniflux packaging/systemd/miniflux.service
  NoNewPrivileges=yes
  PrivateTmp=yes
  PrivateDevices=yes
  ProtectSystem=strict
  ProtectHome=yes
  ReadWritePaths=/var/lib/<app>
  ProtectKernelTunables=yes
  ProtectKernelModules=yes
  ProtectControlGroups=yes
  RestrictNamespaces=yes
  RestrictRealtime=yes
  LockPersonality=yes
  MemoryDenyWriteExecute=yes
  SystemCallArchitectures=native
  [Install]
  WantedBy=multi-user.target

install.sh 步骤（Tailscale / k3s / Pi-hole 的共同套路）
  1) 用 /etc/os-release 探测发行版，非 Ubuntu 就明确报错退出
  2) 幂等建服务用户：id -u <app> >/dev/null 2>&1 || useradd --system --home /var/lib/<app> ...
  3) 解包到 /opt/<app>/releases/$VERSION 并建/切 /opt/<app>/current 软链
  4) /etc/<app>/secrets.env 不存在才生成（umask 077 + python3 -c 'import secrets;print(secrets.token_urlsafe(48))'），
     已存在则原样保留并打印提示（等价于 deb 的 conffile / nfpm 的 config|noreplace）
  5) 安装单元到 /etc/systemd/system/，systemctl daemon-reload && systemctl enable --now <app>
  6) 全程不碰 /var/lib/<app>

upgrade.sh（NetBox/Zulip 模式）
  解新版本目录 → 切 current → 跑 current/scripts/migrate（只做 SQLite/表结构迁移）
  → systemctl restart <app> → /health 自检；**不删、不覆盖 /etc/<app> 与 /var/lib/<app>**

uninstall.sh
  systemctl disable --now <app>；删单元与 /opt/<app>；**默认保留 /etc/<app> 与 /var/lib/<app>**，
  只有 --purge 才删（deb 的 prerm/postrm、k3s 与 Pi-hole 的 uninstall.sh 都是这个约定）
```

---

## 9. 对比表

| 方式 | 安装形态 | 需要 root | 升级方式 | `/etc` 配置 | SQLite/数据 | systemd 单元 | 产物体积/依赖 | 陌生人友好度 |
|---|---|---|---|---|---|---|---|---|
| **pipx** | `pipx install <pkg>` | 否（`--global` 要） | `pipx upgrade [--include-injected]`；换 Python 要 `reinstall --python` | 不管 | 不管 | **不提供**（Whoogle 式手写 unit + 绝对路径） | 只装 wheel；占用 = 依赖大小 | 高（但对服务不够） |
| **uv / uv tool** | `curl … install.sh \| sh` 后 `uv tool install` | 否 | `uv tool upgrade [--all] [-p X]`（遵约束） | 不管 | 不管 | **不提供** | 同上，venv 无 pip | 高（同上） |
| **PyInstaller** | 下载单文件 | 否 | 自己换二进制 | 不管 | 不管 | 不提供 | **38.6 MiB**（yt-dlp 实测） | 中（体积大、onefile 自解压坑） |
| **Nuitka** | 下载单文件/目录 | 否 | 自己换 | 不管 | 不管 | 不提供 | 未查到；glibc 基线限制 | 中低（Linux 可移植性差） |
| **zipapp `.pyz`** | 下载单文件（需系统 Python） | 否 | 自己换 | 不管 | 不管 | 不提供 | **2.93 MiB**（yt-dlp 实测，纯 stdlib 场景≈项目本身大小） | **高（纯 stdlib 项目最省）** |
| **shiv** | `shiv -c app -o bin app` | 否 | 重建 | 不管 | 不管 | 不提供 | 与 zipapp 同级；解压到 `~/.shiv` | 中 |
| **pex** | `pex … -o app.pex` | 否 | 重建 | 不管 | 不管 | 不提供 | 工具自身 5.07 MiB + 依赖 | 中 |
| **deb/rpm（nfpm / dh-virtualenv / fpm / debhelper）** | `apt install ./x.deb` | **是** | `apt` + maintainer script（`$2` 非空=升级；conffile/`config\|noreplace` 保配置） | **包负责** | 包负责建目录 | **包负责 + 硬化模板可抄** | 包体 = 代码+venv | 中（对用户最省，对作者最贵） |
| **briefcase** | 生成 `.deb/.rpm/.pkg.tar.zst`（也可 AppImage/Flatpak） | 构建时可 Docker | 重打包 | 有（system 包） | 有 | 面向桌面 App，服务不顺手 | 含运行时 | 中低（对服务是绕路） |
| **user systemd 单元 + linger** | 无需 root 装服务 | 否（linger 要） | 自己 | 家目录 | 家目录 | `systemctl --user` | 无额外体积 | 中（多用户场景好，服务器场景无优势） |
| **安装脚本 + system 单元（你现在的模式）** | `curl \| sh` 或 tar 包 + `install.sh` | **是** | 版本目录+symlink 或换二进制 + `upgrade.sh` | **自己写，最可控** | **自己写，最可控** | **自己写（可照抄 miniflux/headscale）** | 无额外体积 | **高** |

---

## 10. 一句话结论

对"1 个依赖、SQLite、systemd、单机 + nginx + Let's Encrypt"这种小项目，**最省事的是别引入新的分发器：继续发 tar 包 + `install.sh`/部署脚本 + system 级 systemd 单元，把升级写成 NetBox/Zulip 那种"新版本目录 + 切软链 + 显式保留 `/etc/<app>` 与 `/var/lib/<app>` 数据"的脚本**（单元硬化与 maintainer script 直接照 miniflux/headscale 抄）；如果确实想让陌生人少踩 Python 环境坑，就把"装那 1 个依赖"这层交给 `uv tool install`/pipx（或者干脆 zipapp），**但 nginx、证书、systemd、`/etc` 配置与升级/卸载这一整套仍然要自己写** —— 没有哪个非 Docker 分发器会替你把这套做完。
