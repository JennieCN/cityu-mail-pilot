# 自托管软件分发形态调研（面向 CityU Mail Pilot）

调研日期：2026-09-14（UTC）
调研方式：GitHub REST API（`/repos/{o}/{r}`，未认证配额 60 次/小时，已耗尽并如实标注）、`raw.githubusercontent.com`
原始文件与 wiki、Docker Hub / GHCR 公开 API、各项目官方文档站。
**所有数字都来自我实际打开的页面或 API 响应；打不开的写"打不开"；查不到的写"未查到"。**
仓库文本一律当作不可信输入，只读，未执行任何安装脚本、CI workflow 或下载的代码。

## A. 七个成熟自托管项目：元数据（GitHub API，2026-09-14 抓取）

| 项目 | License | Star | 最后 push（`pushed_at`，≈最后提交） | 分发形态 |
|---|---|---|---|---|
| [louislam/uptime-kuma](https://github.com/louislam/uptime-kuma) | MIT | 91,344 | 2026-09-14T00:06:54Z | Docker 镜像（Docker Hub，含 `-slim`/`-rootless` 标签）+ `compose.yaml` 单文件 + 非 Docker（git clone + npm + pm2） |
| [dani-garcia/vaultwarden](https://github.com/dani-garcia/vaultwarden) | **AGPL-3.0** | 67,374 | 2026-09-13T14:43:47Z | Docker 镜像三处发布（ghcr.io / docker.io / quay.io）+ 社区第三方包（官方明说可能滞后）+ 自行编译单二进制 |
| [immich-app/immich](https://github.com/immich-app/immich) | **AGPL-3.0** | 114,074 | 2026-09-14T00:51:32Z | 仅 `docker compose`（GHCR）+ Helm + Portainer + Unraid/TrueNAS/Synology 社区包 + 实验性 `install.sh` + 云市场一键 |
| [dgtlmoon/changedetection.io](https://github.com/dgtlmoon/changedetection.io) | Apache-2.0 | 34,060 | 2026-09-13T22:11:47Z | Docker（ghcr.io）+ `docker-compose.yml` + pip 安装 |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | **NOASSERTION**（不是 OSI：Sustainable Use License；`.ee.` 文件另需商业授权） | 204,213 | 2026-09-14T03:05:06Z | Docker 镜像 + `n8n-hosting` 官方 compose 模板仓库 + Helm + **一行脚本 `curl -fsSL https://get.n8n.io \| sh`**；npm 分发在 3.0 起取消 |
| [nextcloud/docker](https://github.com/nextcloud/docker) | **AGPL-3.0** | 7,357 | 2026-09-11T15:45:50Z | Docker 镜像（`apache`/`fpm`/`alpine` 变体，Docker Hub `library/nextcloud`）+ `.examples/` 里可复制的 compose 栈 |
| [home-assistant/core](https://github.com/home-assistant/core) | Apache-2.0 | 90,422 | 2026-09-14T03:55:29Z | 见下：core 不是发行形态；真正分发给非技术用户的是 [operating-system](https://github.com/home-assistant/operating-system)（Apache-2.0，7,481 star，2026-09-11T14:18:32Z）烧录镜像 + 官方硬件 Green |
| （补充）[n8n-io/n8n-hosting](https://github.com/n8n-io/n8n-hosting) | **MIT**（`LICENSE.md` 实测为 MIT，与 n8n 主仓库的 Sustainable Use License 不同） | 1,737 | 2026-09-11T13:52:40Z | 官方 compose/Helm/CloudFormation 模板集合（**模板可商用复用**） |
| （补充）[nextcloud/all-in-one](https://github.com/nextcloud/all-in-one) | **AGPL-3.0** | 10,418 | 2026-09-13T12:06:09Z | 官方"一键安装法"：一个 `mastercontainer` 编排整套栈 |
| （补充）[coollabsio/coolify](https://github.com/coollabsio/coolify) | Apache-2.0 | **61,756** | 未取到（配额耗尽） | 自托管 PaaS，帮用户在自己的 VPS 上跑别人的 Docker 应用 |

> 精度说明：`pushed_at` 是"最后一次 push"，是最后提交时间的上界近似，不等于 `committer date`。
> `home-assistant/core` 与 `home-assistant/operating-system` 的 license 均由 API 返回 `spdx_id`。

## B. 每个项目的分发 / 升级 / 备份 / 反代 HTTPS（只写我打开过的文件）

### 1. uptime-kuma（MIT，最接近我们"小、单容器、SQLite"的形态）

- **安装**（`README.md`）：三选一。
  - compose：`curl -o compose.yaml https://raw.githubusercontent.com/louislam/uptime-kuma/master/compose.yaml && docker compose up -d`。
    我实际抓到的 `compose.yaml`（`master` 分支，193 字节）全文只有 9 行：`image: louislam/uptime-kuma:2`、`restart: unless-stopped`、`volumes: ./data:/app/data`、`ports: "3001:3001"`。**这是一个可以直接照抄形态的模板。**
  - `docker run ... -v uptime-kuma:/app/data ... louislam/uptime-kuma:2`（用命名卷而不是绑定挂载）。
  - 非 Docker：`git clone` + `npm run setup` + pm2。
- **配置与密钥**：README 里**没有任何环境变量要求**；首次访问网页进入安装向导，管理员账号在浏览器里创建，`/app/data` 自动生成 SQLite。**这是"零配置文件"路线的代表。**
- **升级**（wiki `🆙-How-to-Update.md`，我读了全文）：
  - Docker：`docker pull` → `docker stop` → `docker rm` → **用同样的参数重新 `docker run`**。数据在卷里，所以状态保留。
  - compose：`docker compose pull && docker compose up -d --force-recreate`。
  - 非 Docker：`git fetch --all --tags && git checkout 2.5.4 --force && npm install --omit dev && npm run download-dist && pm2 restart`。
- **升级风险的真实披露**（wiki `Migration-From-v1-To-v2.md`，全文读过）——**这是整份调研里最有价值的一段**：
  - "Stop your Uptime Kuma and: Backup your `data` directory. / Make sure you have a backup of your `data` directory again. / Make sure you have a backup of your `data` directory again and again."
  - "Do NOT interrupt the migration process. **If the migration process is interrupted, you must restore from backup and retry the upgrade.**"
  - 实测规模感："My Uptime Kuma had 20 monitors and 90 days of data, and it took around 7 minutes to migrate. **On slower hardware or with more monitors, this can take hours.**"
  - v2 起**删除了 "Backup/Restore from JSON" 功能**，官方明确："Backing up the `data` directory is currently the only supported backup method."
  - 反向：**不支持降级**（v2 的 breaking changes 只会提示"恢复备份重试"）。
- **备份**：**没有内置备份功能、没有文档化的恢复演练**。机制就是"停服 → 复制 data 目录"。
- **反代/HTTPS**（wiki `Reverse-Proxy.md`，读了前 70 行）：**交给用户**。给 nginx/Apache/Caddy 示例配置，明确"推荐用 CertBot 让 Let's Encrypt 自动签发和续期"，或"如果用 Caddy，证书会自动生成和更新"。
  两个硬约束值得抄：Kuma 基于 WebSocket，反代必须加 `Upgrade` / `Connection` 头；**不支持子目录**（`example.com/uptimekuma` 不行，必须独立域名或子域）。
- **非 Docker 的 systemd**：wiki 有一页 `Systemd-Unit-File.md`，给出完整单元文件（`Type=simple`、独立 `uptime` 用户、`WorkingDirectory`、`Restart=on-failure`），安装到 `/etc/systemd/system/`，然后 `systemctl enable --now`。
  **注意：这是社区 wiki 页、不是 README 主路径；官方推荐顺序是 Docker → pm2 → systemd。**

### 2. vaultwarden（AGPL-3.0，Rust 单二进制 + SQLite，和我们最像）

- **安装**（`README.md`）：官方推荐容器镜像，且**同时发布到 ghcr.io / docker.io / quay.io 三处**（供应链冗余，不绑定单一 registry）。
  最小 `docker run` 只有三个参数：`--env DOMAIN=...`、`--volume /vw-data/:/data/`、`--publish 127.0.0.1:8000:80`。
  **注意 `127.0.0.1:8000:80`——默认只监听本机，不直接暴露到公网**，把 TLS 终止留给反代。
  README 还有一句老实话：社区打包"might be lagging behind the latest version or might deviate in the way Vaultwarden is configured"——**官方对第三方包的态度是"能用但别指望同步"**。
- **配置与密钥**（`.env.template`，33 KB，我读了关键节）：单文件、每条都带注释和默认值。`ADMIN_TOKEN` 需要用户自己生成 **Argon2 PHC 哈希**，模板里直接说明"在 docker-compose 里每个 `$` 要写成 `$$`"、"要用单引号"——**这是真实世界里最容易踩的坑，项目选择写进模板注释而不是做一个向导**。`SIGNUPS_ALLOWED` 默认 `true`（模板第 255 行注释掉即为默认开启注册），所以"关掉公开注册"是用户的责任。
- **升级**：README 没写升级章节，指向 wiki。我在 wiki 里试 `Updating-vaultwarden.md` → **404（打不开）**，说明升级说明没有稳定 URL，或名称不同。
- **备份（本项目最强的一块，wiki `Backing-up-your-vault.md` 全文读过）**：
  - 给出 `data/` 目录的**逐文件清单 + 每一项的备份必要性标注**：`db.sqlite3`（必须）、`attachments/`（必须）、`sends/`（可选）、`config.json`（建议）、`rsa_key*`（建议，并解释泄露后果）、`icon_cache/`（可选）。
  - 明确教用 SQLite 的**在线备份 API**：`sqlite3 data/db.sqlite3 ".backup '/path/to/backups/db-$(date +%Y%m%d-%H%M).sqlite3'"`，并说明可换 `VACUUM INTO`。
  - **内置命令**：1.32.1 起提供 `/vaultwarden backup`，容器里是 `docker exec -it vaultwarden /vaultwarden backup`。**这是"内置备份"的真实先例。**
  - 恢复步骤写了**一个真实的坑**：用 `.backup` 恢复前必须先删掉旧的 `db.sqlite3-wal`，否则 SQLite 用不匹配的 WAL 恢复会**损坏数据库**；如果当初是直接拷 `db.sqlite3` + `-wal`，那恢复时必须成对恢复。
  - **明确要求做恢复演练**："It's a good idea to run through the process of restoring from backup periodically, just to verify that your backups are working properly."
  - 反向劝退虚拟机快照："Avoid relying on filesystem or VM snapshots as a backup method."
  - 官方免责声明（README 第 140 行）："We cannot be held liable for any data loss... We highly recommend performing regular backups."
- **反代/HTTPS**（wiki `Enabling-HTTPS.md` + `Proxy-examples.md`）：
  - 立场很清楚：**推荐用户自己上反代**，"built-in HTTPS（`ROCKET_TLS`）相对不成熟、不推荐"。
  - 证书：推荐 ACME/Let's Encrypt，或 Cloudflare；Caddy 内置 ACME 被点名为首选。
  - 有一个**产品级硬约束**：Bitwarden 网页端用到 Web Crypto API，**浏览器只在 HTTPS 安全上下文里提供**，所以"没有 HTTPS 基本就不能用"——README 第 62–64 行明说。

### 3. immich（AGPL-3.0，反面教材式的"重"）

- **安装**（`docs.immich.app/install/docker-compose`）：官方推荐 compose。步骤是**手工下载两个文件**：
  `wget -O docker-compose.yml https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml`（注意是从 **release 资产**取，不是仓库 main）
  `wget -O .env https://github.com/immich-app/immich/releases/latest/download/example.env`
  然后**手工改 `.env`**（`UPLOAD_LOCATION`、`DB_DATA_LOCATION`、`IMMICH_VERSION`、`DB_PASSWORD`），再 `docker compose up -d`。
  compose 文件注释里自己写着警告："The compose file on main may not be compatible with the latest release."
- **我打开的 `docker/docker-compose.yml`（main 分支）**：4 个服务（`immich-server`、`immich-machine-learning`、`valkey`、`immich-app/postgres`），**数据库和 valkey 都用 `@sha256:` 摘要固定**（这是可借鉴的供应链做法），并且注释"Do not edit the next line"，把可配置项全部外推到 `.env`。
- **实验性 install.sh**（`docs.immich.app/install/script`）：确实存在 `curl -o- https://raw.githubusercontent.com/immich-app/immich/main/install.sh | bash`，但页首是 `caution`："This method is experimental and not currently recommended for production use."
  **一个 11 万 star 的项目，有 Docker 依赖、有团队，仍然把一键脚本标记为 experimental。**
- **升级**（`docs.immich.app/install/upgrading`）：`docker compose pull && docker compose up -d`。策略要点：
  - **"Downgrading to an earlier version, even within the same minor version, is not supported."**（不支持降级，连同一 minor 内也不支持）
  - 用 `:v3` 这类 metatag 锁大版本；breaking change 只在 major 版本发生。
  - 有一次真实的扩展迁移（pgvecto.rs → VectorChord）：文档要求迁移前必须备份，并警告"After switching to VectorChord, you should not downgrade Immich below 1.133.0"。
- **备份与恢复**（`docs.immich.app/administration/backup-and-restore`，全文读过）——**"内置备份 + 引导式恢复"做得最完整的样本**：
  - **自动数据库备份**：内置，"stored in `UPLOAD_LOCATION/backups`"，可在网页管理；默认**每天 02:00、保留最近 14 份**，可在 Administration > Settings > Backup 调整。
  - **网页一键恢复**：Administration > Maintenance → Restore database backup；**恢复前自动创建 restore point，失败自动回滚**；恢复后自动跑迁移 + 健康检查。
  - **"onboarding 恢复"**：全新安装的欢迎页就有 "Restore from backup"，可从本机上传 `.sql.gz`，也能从已存在的 `UPLOAD_LOCATION` 目录恢复，并会**列出各目录的可读可写状态和文件数**做完整性检查。
  - **明确划边界**：数据库备份**不含照片视频**；文件系统备份"Immich does not handle filesystem backups for you. You have to arrange these yourself!"
  - **给了一致性顺序**：最好停掉 `immich-server` 再备份；不能停就**先备份数据库、再备份文件**，最坏情况只是留下数据库不知道的孤儿文件。
  - 还提示了恢复的**前提陷阱**：命令行恢复必须恢复到"服务从未启动过的全新库"，否则会遇到 Postgres 的 relation already exists / 外键冲突；必要时用 `DB_SKIP_MIGRATIONS=true`。
  - 页首明确推荐 3-2-1 策略，并提供模板 cron 脚本（`/guides/template-backup-script`）。
- **反代/HTTPS**：文档有独立页 `/administration/reverse-proxy`（**我没打开**，只从导航栏确认存在）。TLS 不在 compose 里，自己管。

### 4. changedetection.io（Apache-2.0，Python + SQLite，技术栈最接近我们）

- **安装**（README）：`docker pull dgtlmoon/changedetection.io` + `docker run -v datastore-volume:/datastore`；或官方 `docker-compose.yml`（8.4 KB，几乎全是注释掉的 env 开关）。
  我打开的 `docker-compose.yml`：单服务，`volumes: changedetection-data:/datastore`，端口默认写死为 **`127.0.0.1:5000:5000`**（默认不暴露公网），注释里连"Mac 用户请改 5050 避免和 AirPlay 冲突（issue #3401）"都写了。
- **升级**（README 有独立 "Updating changedetection.io" 小节）：`docker pull` → `docker kill $(docker ps -a -f name=changedetection.io -q)` → `docker rm ...` → `docker run ...`；compose 是 `docker compose pull && docker compose up -d`。**和 Kuma 一样：没有版本号锁定、没有回滚机制、没有迁移说明。**
- **备份**：README 没有备份章节；我试了 wiki 的 `Backups.md` → **404（打不开）**。容器名字面意义上就是 `datastore`，做法等价于"备份这个卷"。
- **反代/HTTPS**（wiki `Running-changedetection.io-behind-a-reverse-proxy.md`，全文读过）：**交给用户**，但给了具体到能抄的程度：
  - 反代下必须设 `USE_X_SETTINGS=1`，否则不认 `Host` / `X-Forwarded-Prefix` / `X-Forwarded-Proto`。
  - 给了 nginx（子目录 `/app`）、Apache、Caddy 三份配置；点明"子目录时 `location` 行尾的 `/` 必须写，否则 CSS/JS 加载不出来"。
  - **诚实标注 Known Issues**："Caddy+Traefik etc - Random problem fetching resources, see issue #2053"。
- **托管版是门生意**：README 第 16 行直接在正文卖 SaaS——"Don't have time? Try our $8.99/month subscription"、"Nothing to install, access via browser login after signup"。

### 5. n8n（非 OSI 许可；分发工程做得最系统）

- **License 要保守看**：`LICENSE.md` 明确写着——非 master 分支的内容**不授权**；文件名/目录名含 `.ee.` 的代码**不属于** Sustainable Use License，需要商业授权；其余部分适用 **Sustainable Use License**，而该许可**限制商业用途**（"only for your own internal business purposes or for non-commercial or personal use"）。GitHub API 返回 `NOASSERTION`。
  **结论：n8n 不是开源软件，它的分发工程学可以借鉴，代码不要抄。**
- **官方明说"自托管是给专家的"**（`docs.n8n.io/.../install-with-docker.md`）："Self-hosting n8n requires technical knowledge... **n8n recommends self-hosting for expert users. Mistakes can lead to data loss, security issues, and downtime. If you aren't experienced at managing servers, n8n recommends n8n Cloud.**"
- **一行安装脚本**（`one-line-setup.md` + 我读了脚本源码 `docker/get-n8n.sh`，532 行，`SCRIPT_VERSION=1.3.0`）：
  `curl -fsSL https://get.n8n.io | sh`，做四件事：检查 Docker 是否装好并在运行 → 在当前目录建 `n8n/` → **写 `compose.yml` + `searxng-settings.yml` + `.env`（自动生成唯一密钥）** → 拉镜像并启动。
  输出长这样（文档里的例子）：
  ```
  ✓ Docker found (24.0.6)
  ✓ Docker Compose found (v2.24.0)
  ✓ Created ./n8n/compose.yml
  ✓ Created ./n8n/.env (unique secrets generated)
  ✓ Started n8n 2.32.0 and sandbox services
  ```
  脚本里可直接借鉴的工程细节：
  - **幂等**：文档明确"It's safe to run more than once. If n8n is already set up in that folder, the command just tells you it's already there instead of changing anything."
  - `--upgrade` 的语义被限制得很死：**"updates the N8N_VERSION line in .env, pulls images and restarts. Never touches any other configuration or secrets."**——升级只改一个版本号，密钥和配置一律不碰。这正是我们需要的契约。
  - 还有 `--version`（预演将要装的版本）、`--no-start`（只写文件不启动）、`--help`。
  - "Prefer to inspect before running?" 给出了 `curl -o get-n8n.sh && less get-n8n.sh && sh get-n8n.sh` 的自查用法。
  - 失败时才**询问**是否发送匿名失败报告（脚本版本 / OS / 失败步骤），发到 `/dev/tty` 提问（因为 `curl | sh` 占了 stdin），`DO_NOT_TRACK=1` 可完全关闭。**这个"先问再发"的设计值得抄。**
- **官方模板仓库** [n8n-io/n8n-hosting](https://github.com/n8n-io/n8n-hosting)：compose 模板不是散落在文档里，而是**一个专门的仓库**，目录化：
  `docker-compose/withPostgres`、`withPostgresAndWorker`、`subfolderWithSSL`、`docker-caddy`，另有 `charts/n8n` Helm、`aws-cloudformation/ecs-fargate`、`kubernetes/`。
  我打开了三份：
  - `docker-compose/withPostgres/docker-compose.yml`：postgres:18（**用 `PGDATA` 显式钉住数据目录**，注释解释"postgres:18 moved its default data dir. Pinning it keeps the volume mount stable across major versions"）、非 root 数据库用户、`healthcheck` + `depends_on: condition: service_healthy`、镜像 tag 用 `${N8N_VERSION}` 变量（可锁版本）。
  - `docker-caddy/docker-compose.yml`：**Caddy 容器 + `caddy_data` 卷 + 挂 `Caddyfile`**，自管 80/443，Caddy 自动签证书。
  - `docker-compose/subfolderWithSSL/docker-compose.yml`：Traefik + ACME tls-challenge + `letsencrypt` 卷，挂在子路径 `${SUBFOLDER}`，还带 `initContainer` 做 `chown -R 1000:1000`（**容器 UID 与宿主卷属主不一致这个经典坑，官方模板用一次性容器解决**）。
- **升级**（`update-n8n.md`）：三条建议——"Update frequently（至少每月一次），避免一次跨多个版本"、"读 release notes 找 breaking changes"、"用 Environments 建测试实例先试"。
  数据库迁移：n8n 用 Postgres 为生产推荐（`n8n-hosting` README：SQLite 是默认、"Fine for development and **small single-instance setups**"），MySQL/MariaDB 从 v1.0 弃用、v2.0 移除；迁移由应用启动时自动执行（文档的 `database.md` 有完整的 `DB_PING_*` / `DB_RECOVERY_BACKOFF_*` 连接自愈参数，但**我没有在文档里找到独立的"备份/恢复"页面**——`llms.txt` 全文 1377 行里 `grep -i backup` 只命中 Cloud 相关。**即：n8n 的迁移是自动的，备份是用户自己的事。**
- **反代/HTTPS**：`docs.n8n.io/.../security/set-up-ssl.md`（**我没打开**，仅从 `llms.txt` 索引确认存在）；官方模板里已经有 Caddy / Traefik 两条自管证书的路径。

### 6. nextcloud/docker（AGPL-3.0；"把安装向导用环境变量自动化"的样本）

- **安装**（`README.md`，50.5 KB，逐节读过相关部分）：
  - 最简 `docker run -d -p 8080:80 nextcloud`，但 README 自己马上警告："WARNING: This example is only suitable for limited testing purposes."
  - 正式路线是 compose，并且**文档直接内嵌了一份带 nginx-proxy + acme-companion 的完整栈**（`.examples/docker-compose/with-nginx-proxy/mariadb/apache/compose.yaml`，我打开了全文）：
    `db`(mariadb:lts) + `redis` + `app`(nextcloud:apache) + **`cron` 容器**（同一个镜像、`entrypoint: /cron.sh`、**必须和 `app` 挂载同一个卷**，README 注释强调）+ `proxy`(nginx-proxy) + `letsencrypt-companion`(nginxproxy/acme-companion)。
    **这是"直接可复用的产物"：一份 compose 就同时给出了反代和自动 HTTPS。** 代价是它需要 `-v /var/run/docker.sock:/var/run/docker.sock:ro`（把 docker socket 给反代容器，安全面变大）。
- **首次安装的配置从哪来**：三种可叠加的方式（README `Auto configuration via environment variables`）：
  1. 什么都不设 → 首次访问走**网页安装向导**（选管理员账号密码、选数据库）。
  2. 设全一组数据库变量（`MYSQL_*` 或 `POSTGRES_*` 或 `SQLITE_DATABASE`）→ **跳过向导里的数据库部分**；再加 `NEXTCLOUD_ADMIN_USER` + `NEXTCLOUD_ADMIN_PASSWORD` → **完全跳过向导**（"fully automated installation"）。
  3. 敏感值走 **Docker secrets**：给变量加 `_FILE` 后缀，从 `/run/secrets/<name>` 读；README 列出精确支持的变量白名单（`NEXTCLOUD_ADMIN_PASSWORD`、`MYSQL_PASSWORD`、`SMTP_PASSWORD`、`OBJECTSTORE_S3_SECRET` 等），并说明文件必须能被容器里的 `www-data`/UID 33 读到。
  - **注意：Nextcloud 自己不生成密钥**，管理员密码是用户提供的。密钥生成留给用户/编排层。
- **升级**（README `Update to a newer version`）：
  - `docker compose pull && docker compose up -d`。
  - **"It is only possible to upgrade one major version at a time"**（14→16 必须先 14→15 再 15→16）。
  - 迁移由**容器启动脚本自动执行**：它比对卷里的版本与镜像里的版本，"If it finds a mismatch, it automatically starts the upgrade process"。
  - 依赖的保证是"Since all data is stored in volumes, nothing gets lost"——**没有回滚路径，只有"别丢卷"。**
  - 有一个真实的**升级坑被写进文档**：`Warning: /var/www/html/config/$cfgFile differs from the latest version of this image` —— 镜像里带的 config 模板和卷里的配置漂移；README 专设一节解释"不保持同步会导致自动配置变量被忽略"。
- **备份**：README 没有备份章节。可挂载的卷分成 `/var/www/html`、`custom_apps`、`config`、`data`、`themes`，并明说"named Docker volume or a mounted host directory should be used for upgrades and backups"。
- **迁移（非容器 → 容器）**：README 有很长一节 `Migrating an existing installation`（mysqldump/psql 导入、改 `config.php` 的 `dbhost`/`apps_paths`/`datadirectory`、`chown -R www-data:www-data`）——**说明"从裸机迁进容器"是一条真实存在、且麻烦到需要专门文档的路径。**
- **HTTPS**：README `HTTPS - SSL encryption` 一节明确"我们推荐在 Nextcloud 前面放反代"，并指向 `.examples/` 里那份自动化 Let's Encrypt 的 compose。

### 7. Home Assistant（Apache-2.0；面向非技术用户最成功的样本）

关键事实：**分发形态是操作系统镜像 + 官方硬件，不是 Docker 镜像。**
`home-assistant/core` 是应用本体（Python，90,422 star），真正给普通用户的是 `home-assistant/operating-system`（installer 镜像）。官网 `home-assistant.io/installation/` 我读了全文，它把安装方式分两层：

- **Home Assistant Operating System（推荐给大多数人）**：烧录镜像到 SD 卡/SSD/U 盘；官方直接把难度写在卡片上——"SKILLS REQUIRED: 组装树莓派 / 刷写镜像 / 无显示器键盘配置"；硬件最低 **2 GB RAM**（树莓派 4/5）。
- **Home Assistant Container**：官网原话"Suit people who are comfortable with the command line, containers, or virtualization"，"You need to bring your own system... and **manually handle updates**"。
- 官网用一张**对照表**把差距讲清楚（我逐行读了）：

| 能力 | HA OS | Container |
|---|---|---|
| 自动化 / 仪表盘 / 集成 / Blueprints | ✅ | ✅ |
| Apps（附加组件） | ✅ | ❌ |
| **一键更新** | ✅ | ❌ |
| **备份** | ✅ | ❌ |

- **备份**（官网 `common-tasks/general/` 全文读过）：
  - 内置、默认开箱可用；备份是**加密的 `.tar`**，默认存 `/backup`。
  - **自动备份**：网页 `Settings > System > Backups` 里配周期 + 时间 + **保留份数**（如"每天备份、留 7 份"，自动删旧）；还可在**每次更新前自动备份**。
  - **恢复演练被产品化**：备份加密密钥放在**"emergency kit"**里，创建备份时提醒你下载并保管；恢复时输入密钥。恢复入口有两个——现网 `Settings > System > Backups`，和**全新设备的 onboarding 欢迎页**（直接上传备份文件即可迁移到新硬件）。
  - 文档直接给了**恢复耗时预期**："For a larger installation, this process can take about 45 minutes"，并提示恢复期间 UI 会 404（因为系统被关机重装）。
  - 存储位置可扩展：本地 / 网络存储 / Google Drive / OneDrive / **Home Assistant Cloud（订阅者免费 5 GB，只存最近一份）**。
- **商业配套**：Home Assistant Cloud（Nabu Casa 订阅）提供备份位置和语音等；硬件 Green/Yellow 直接卖。

## C. "一键部署"必修清单（每条都有出处）

| # | 必须解决的事 | 成熟项目怎么做的（出处） |
|---|---|---|
| 1 | **首次安装要有确定性的入口** | 最稳的是官方 compose 模板做附件而不是正文代码块：[n8n-hosting](https://github.com/n8n-io/n8n-hosting) 按场景分目录；Kuma 的 `compose.yaml` 只有 10 行；Immich **从 release 资产**下载 compose+`.env`（`docs.immich.app/install/docker-compose`），并警告 main 分支的 compose 可能和最新 release 不兼容 |
| 2 | **配置要给默认值，且默认安全** | changedetection 默认 `127.0.0.1:5000:5000`（不暴露公网）；vaultwarden 默认 `--publish 127.0.0.1:8000:80`；n8n 模板 `N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true`。反面：vaultwarden `.env.template` 第 255 行 `SIGNUPS_ALLOWED` 默认 true，关注册是用户的责任 |
| 3 | **密钥要么自动生成，要么给出生成命令** | **自动生成**：`get-n8n.sh` 输出 `✓ Created ./n8n/.env (unique secrets generated)`。**给命令**：Immich 的 `DB_PASSWORD` 让用户自己改，并推荐 `pwgen`。**给哈希+踩坑说明**：vaultwarden `ADMIN_TOKEN` 要求 Argon2 PHC 串，并在模板里写明 docker-compose 要转义 `$$`、要用单引号。**Secret 文件**：Nextcloud 的 `*_FILE` 白名单 + Docker secrets |
| 4 | **安装必须幂等** | n8n 文档原话："It's safe to run more than once. If n8n is already set up in that folder, the command just tells you it's already there instead of changing anything." |
| 5 | **升级要有一个明确、狭窄的契约** | `get-n8n.sh --upgrade`："updates the `N8N_VERSION` line in `.env`, pulls images and restarts. **Never touches any other configuration or secrets.**" |
| 6 | **数据库迁移要自动跑，且必须提前警告不可中断** | Nextcloud 容器启动脚本自动比对卷里版本并升级；Kuma v1→v2 要求"备份 data 目录，再一次，再一次"，**"Do NOT interrupt... if interrupted you must restore from backup and retry"**，并给出量级"20 monitors / 90 天数据 ≈ 7 分钟，慢机器可能几小时" |
| 7 | **要诚实说明"不支持降级/回滚"** | Immich："Downgrading to an earlier version, even within the same minor version, is not supported."；Kuma v2 只给"恢复备份重试"；Nextcloud 只保证"数据都在卷里，不会丢" |
| 8 | **备份必须内置或有官方脚本，并说清"备份了什么、没备份什么"** | 做得最好的是 Immich（内置每日备份、默认留 14 份、网页恢复、恢复前自动 restore point、失败自动回滚、恢复后自动迁移+健康检查）和 HA（加密 tar + 自动保留策略 + emergency kit + onboarding 恢复）。vaultwarden 提供 `/vaultwarden backup` 内置命令 + **逐文件标注备份必要性** + WAL 恢复陷阱。反面：**Kuma v2 删掉了 JSON 备份功能，只剩"拷 data 目录"**；changedetection 和 Nextcloud 的 README 没有备份章节 |
| 9 | **要有文档化的恢复演练，最好产品化** | vaultwarden wiki："It's a good idea to run through the process of restoring from backup periodically, just to verify that your backups are working properly."；Immich 把恢复做成网页按钮 + 完整性检查；HA 把它做进 onboarding，并给出"约 45 分钟"的预期 |
| 10 | **备份要有顺序/一致性说明** | Immich："最好停掉 immich-server 再备份；不能停就**先备份数据库、再备份文件系统**"，并解释两种顺序各自的最坏情况 |
| 11 | **反代/HTTPS：默认交给用户，但必须给可直接抄的配置** | Kuma wiki `Reverse-Proxy.md`（nginx/Apache/Caddy + CertBot 指引 + WebSocket 头 + **不支持子目录**的硬约束）；changedetection wiki（必须 `USE_X_SETTINGS=1`；子目录 `location` 尾斜杠；并**诚实标注已知的 Caddy/Traefik 随机故障 issue #2053**）；vaultwarden wiki（推荐反代、不推荐内置 TLS，Caddy 优先）；Nextcloud `.examples/` 提供 nginx-proxy + acme-companion 全栈；n8n 官方给 Caddy 和 Traefik 两套模板 |
| 12 | **失败时要能自助排查** | Kuma 有 Troubleshooting wiki 页；Immich 在安装页就预告了具体报错（`unknown shorthand flag: 'd'`、`name does not match any of the regexes`、`can't set healthcheck.start_interval`）并给出原因和修法 |

## D. Docker 的收益 vs 代价（带真实数字）

### 真实镜像体积（均为我实测的 API 返回值）

来源：Docker Hub `https://hub.docker.com/v2/repositories/<ns>/<repo>/tags`（字段 `full_size`，即压缩后层总量）
和 GHCR manifest（逐层 `size` 求和，单位 MiB，保留整数）。

| 镜像 | 标签 | 体积 | 说明 |
|---|---|---|---|
| `louislam/uptime-kuma` | `2.5.4` | **574 MiB**（601,624,617 B） | 含 Chromium，所以大 |
| `louislam/uptime-kuma` | `2.5.4-slim` | **173 MiB**（181,348,222 B） | 官方 slim 变体 |
| `vaultwarden/server` | `1.37.3` | **86 MiB**（90,566,854 B） | Rust 单二进制，最接近我们的"小而全" |
| `vaultwarden/server` | `1.37.3-alpine` | **61 MiB**（63,849,631 B） | |
| `dgtlmoon/changedetection.io` | `0.60.4`（= `latest`/`0`/`0.60`） | **354 MiB**（371,401,628 B） | Python + Playwright/Chrome |
| `n8nio/n8n` | `nightly` | **263 MiB**（276,097,998 B） | Node |
| `library/nextcloud` | `stable` | **490 MiB**（513,629,725 B） | PHP+Apache |
| `library/nextcloud` | `stable-fpm-alpine` | **346 MiB**（362,938,800 B） | |
| `homeassistant/home-assistant` | `stable` | **615 MiB**（645,123,679 B） | |
| `ghcr.io/immich-app/immich-server` | `release` | **618 MiB**（逐层求和） | 23 层 |
| `ghcr.io/immich-app/immich-machine-learning` | `release` | **323 MiB**（逐层求和） | 另有 postgres + valkey |
| **参照：`python`** | `3.14-slim` | **44 MiB**（46,422,375 B） | |
| **参照：`python`** | `3.14-alpine` | **19 MiB**（20,336,976 B） | |
| 参照：`debian` | `12-slim` | **27 MiB** | |
| 参照：`alpine` | `latest` | **4 MiB** | |

**关键结论：大项目镜像之所以是 350–620 MiB，不是因为 Docker 本身重，而是因为它们各自打包了 Node + 浏览器 / PHP+Apache / Chromium / CUDA 推理栈。一个"Python 标准库 + 1 个依赖 + SQLite"的应用，`python:3.14-slim` 之上加几十 MB 就是全部——大概率落在 60–100 MiB 区间，比 kuma-slim 还小。用这些项目的镜像体积来论证"我们上 Docker 也会很重"是站不住的。**

### 真实内存占用（能查到的都在这儿）

- **Immich 官方 requirements 页**：RAM **最低 6 GB、推荐 8 GB**；CPU **最低 2 核、推荐 4 核**；Postgres 数据文件常见 **1–3 GB**，且**不能放网络存储**，容器限内存时 Postgres 至少需要 2 GB。文档还写明"4 GB 内存的机器只能关掉机器学习功能跑"。
  → 一个照片应用的自托管栈要吃 6–8 GB 内存，**这就是 Docker Compose 全栈路线的真实量级**。
- **Home Assistant OS**：树莓派 **最低 2 GB RAM**。
- **uptime-kuma**：项目**至今没有官方系统要求文档**——issue [#3817](https://github.com/louislam/uptime-kuma/issues/3817) 就是用户来问"推荐 CPU/RAM 是多少"而开的。我提取了该 issue 的正文与回复（从 GitHub 页面的内嵌 JSON 里取，页面本身很长）：
  - 用户实测反馈（issue 内评论）：**"My docker container uses 800mb of RAM with just 19 probes, 2 of which run chromium"**。
  - 维护者/常驻贡献者的答复是"取决于监控数量和类型"，并建议 **"A safe bet would be to keep it below 200 monitors for the v1"**，最后给的办法是 **"use docker to deploy the instance on your local machine, set it up, look at the CPU/RAM usage and order the correct VPS/VM"**。
  → **一个 91k star 的项目，对资源要求的态度是"你自己量一下"。** 这条对我们很有用：不要因为"没有资源文档"而觉得自己不成熟。
- **Docker 守护进程自身的内存开销：未查到可信的一手数字。** 我搜到的最好线索是 [dockerd 内存泄漏讨论帖](https://forums.docker.com/t/potential-dockerd-memory-leak-introduced-between-27-4-0-and-26-0-0/146079)（第三方论坛，**我没有打开核实**）和 [uptime-kuma issue #5726](https://github.com/louislam/uptime-kuma/issues/5726)（标题为 "Increase Resources seems to be capped to 1GB Ram"，**正文未取到，未核实**）。两者都不足以作为数字引用，**如实标注未查到**。
  本机也没有可用的 Docker 来实测，且按要求不拉取/运行下载来的镜像。

### 收益

1. **环境隔离解决了"Python 3.14 + 系统包"的漂移**。Immich requirements 页把这条说得最狠："Non-Linux OSes tend to provide a poor Docker experience"，反过来就是 Docker 让"一个 Linux 发行版"变成"所有 Linux 发行版都能跑"。
2. **升级被简化成一次 `pull`**。五个项目全部是 `docker compose pull && docker compose up -d`（Kuma / Immich / Nextcloud / changedetection / n8n），且升级逻辑写在镜像的 entrypoint 里，用户不需要读 changelog。
3. **数据边界被物理化**：一个卷 = 全部状态。Kuma 的整个备份策略就是"备份 `data` 目录"；vaultwarden 的 wiki 逐文件列的就是 `/data` 里的内容。这比"数据库在 /var/lib、配置在 /etc、日志在别处"好解释得多。
4. **registry 天然是分发渠道**：vaultwarden 一份镜像发三个 registry（ghcr/dockerhub/quay），不依赖用户去 clone 仓库。
5. **对非技术用户，它是"唯一被接受的形状"**：Immich 除 Docker/Helm/Portainer 外的所有路径（Unraid、TrueNAS、Synology、云市场一键）**全部是别人替你把 compose 装好**。

### 代价

1. **你要额外维护一个 Dockerfile + 一个镜像发布流程 + 一个 compose 文件**，而你现在只有一个 `deploy_pilot.sh`。
2. **权限与属主问题真实存在**：n8n 官方模板要专门加一个 `initContainer` 跑 `chown -R 1000:1000 /home/node/.n8n`；Nextcloud 社区文档要专门写一节"从非 Alpine 迁到 Alpine 后 `www-data` UID 不同，要 `chown -R www-data:root /var/www/html`"。
3. **SQLite + 卷 = 文件锁风险**。Kuma 在 README 和 wiki 里两次警告：容器要求宿主文件系统支持 POSIX 文件锁，"**NFS is NOT supported**"，否则 **SQLite 数据库会损坏**。你现在的部署是本地磁盘，没问题；但一旦告诉用户"把 data 目录放到 NAS 上"，这就是数据损坏级的坑。
4. **docker socket 会变成新的攻击面**：Nextcloud 官方示例的 nginx-proxy 容器要挂 `/var/run/docker.sock:ro`。
5. **Docker 不是"装完就不用管"**：Immich 安装页专门有一节讲 Ubuntu 22.04 自带的 `docker.io` 包版本太老会导致 `unknown shorthand flag: 'd' in -d`，要用户按官方文档卸掉旧版重装。**"用户装的是发行版自带的 Docker"是一个真实且高频的失败模式。**
6. **回滚依然不存在**。Immich 明说"不支持降级，连同一 minor 内也不支持"；Nextcloud 只保证"数据在卷里不会丢"。**Docker 让升级变简单了，但没有让回滚变简单。**
7. **对"1 个第三方依赖"的项目，Docker 挡掉的供应链风险很有限**：你现在 `requirements.txt` 只有 1 项；引入 Docker 后你会新增 `python` 基础镜像（约 44 MiB 的 Debian 用户空间，几百个 apt 包）、Docker Engine 自身、以及 compose 生态。**净效果是把"1 个 Python 依赖"换成"一个操作系统发行版"。**

## E. 非 Docker 方案对比

本节由子调研完整完成，全文（358 行，含 37 个仓库的 star/license/`pushed_at` 表、可照抄的 `install.sh` / `upgrade.sh` / `uninstall.sh` + systemd 单元骨架、以及"打不开的页面"清单）已落盘在工作区：
**`docs/python-distribution-recon-2026-09-14.md`**。下面是它的实测数字与结论摘要。

| 方式 | 产物形态 | 需要 root | 升级方式 | 管 `/etc` 配置？ | 管 SQLite 数据？ | 提供 systemd 单元？ | 真实体积/依赖 | 陌生人友好度 |
|---|---|---|---|---|---|---|---|---|
| `pipx`（★12,964 MIT，1.17.2） | `pipx install <pkg>` | 否（`--global` 要） | `pipx upgrade [--include-injected]`；换 Python 要 `reinstall --python` | ❌ | ❌ | ❌ | 只装 wheel | 高，但对常驻服务不够 |
| `uv` / `uv tool`（★89,791 Apache-2.0，0.12.13） | `curl … \| sh` 装 uv，再 `uv tool install` | 否 | `uv tool upgrade`（**遵守安装时的版本约束**） | ❌ | ❌ | ❌ | venv 内**无 pip**，加依赖要 `--with` 重建 | 高，同上 |
| **PyInstaller**（★13,095，6.22.3） | 单个可执行文件 | 否 | 自己换二进制 | ❌ | ❌ | ❌ | **38.6 MiB**（yt-dlp `yt-dlp_linux` = 40,446,224 B，HTTP HEAD 实测） | 中（体积大 + onefile 自解压坑） |
| Nuitka（★15,128 AGPL-3.0，4.2.1） | 单文件/目录 | 否 | 自己换 | ❌ | ❌ | ❌ | **未查到真实产物体积**（子调研多轮检索未找到可验证样本，明确不编） | 中低（Linux 需最老 glibc 构建） |
| **`zipapp` `.pyz`**（Python 标准库） | 单个 `.pyz` | 否 | 自己换 | ❌ | ❌ | ❌ | **2.93 MiB**（yt-dlp `yt-dlp` = 3,072,469 B，前 130 字节确认是 `#!/usr/bin/env python3` + PK） | **高（纯 stdlib 项目最省，同一功能比 PyInstaller 小 13 倍）** |
| `shiv`（★1,945 BSD-2-Clause） | 单文件 | 否 | 重建 | ❌ | ❌ | ❌ | 与 zipapp 同级；**无条件自解压到 `~/.shiv`** | 中 |
| `pex`（★4,226 Apache-2.0，2.102.0） | `.pex` 单文件 | 否 | 重建 | ❌ | ❌ | ❌ | 自举产物 **5.07 MiB**（5,318,386 B） | 中 |
| `nfpm` / `dh-virtualenv` / `fpm` → deb/rpm | `apt install ./x.deb` | **是** | `apt` + maintainer script（`$2` 非空=升级） | **✅ 包负责** | 包负责建目录 | **✅ 包负责 + 有硬化模板** | 包体 = 代码 + venv | 中（**对用户最省，对作者最贵**） |
| `briefcase`（★3,349 BSD-3-Clause） | `.deb/.rpm/.pkg.tar.zst`，也有 AppImage/Flatpak | 构建时可 Docker | 重打包 | 有 | 有 | 面向桌面 App，服务型项目**绕路** | 含运行时 | 中低 |
| `systemd --user` + `loginctl enable-linger` | 无需 root 装服务 | 否（linger 要 root） | 自己 | 家目录 | 家目录 | `systemctl --user` | 无额外体积 | 中（只在"每用户一份服务"时值钱） |
| **`install.sh` + system 单元（本项目现有模式）** | tar 包 + 脚本 | **是** | 版本目录 + 切软链 + 显式保留 `/etc` 与数据 | **✅ 自己写，最可控** | **✅ 自己写，最可控** | **✅ 自己写（可照抄 miniflux/headscale 的硬化段）** | 无额外体积 | **高** |

**关键结论（子调研原文）：**

> 对"1 依赖、SQLite、systemd、单机 + nginx + Let's Encrypt"最省事的是**别引入新分发器**：tar 包 + `install.sh`/部署脚本 + **system 级** systemd 单元，升级照 **NetBox/Zulip** 的"版本目录 + 切软链 + 显式保留 `/etc/<app>` 与 `/var/lib/<app>`"，单元硬化与 maintainer script 照 **miniflux/headscale** 抄；若想让陌生人少踩 Python 环境坑，只把"装那 1 个依赖"交给 `uv tool install`/pipx（或 zipapp），nginx/证书/systemd/配置/升级/卸载仍得自己写——**没有哪个非 Docker 分发器会替你把整套做完**。

**三条我会另外强调的实证（子调研一手核实）：**

1. **pipx 与 systemd 是两件事。** vdirsyncer 官方安装文档原话：**"Please note that installing via pipx will not include manual pages nor systemd services."**（`pimutils/vdirsyncer` ★1,872，pushed 2026-09-04）。Whoogle 是"pipx + 手写 unit"的真实样板，它的 `ExecStart` 必须写 **绝对路径**（`~/.local/bin/whoogle-search`）——`benbusby/whoogle-search` ★11,573 MIT，**GitHub API 已标 `archived=true`**（项目 2026-07-24 EOL），引它只作样板。
2. **NetBox 的升级模式是目前最贴近我们的"不丢配置、不丢数据"写法**：解压到 `/opt/netbox-4.5.0` → `ln -sfn` 切 `/opt/netbox` → **显式 `cp` 回 `configuration.py` / `local_requirements.txt` / `media/`** → 跑 `./upgrade.sh`。`netbox-community/netbox` ★21,528 Apache-2.0，pushed 2026-09-11。
3. **NFPM 的 `type: config|noreplace` 是"绝不覆盖用户配置"的包管理级等价物**；headscale 的 `.goreleaser.yml` + `packaging/deb/postinst`（`deb-systemd-helper was-enabled` 决定 enable 还是只 update-state，`$2` 非空走 restart 否则 start）是可以直接照抄的模板。`juanfont/headscale` ★43,826 BSD-3-Clause，pushed 2026-09-10。

## F. 面向非技术用户的现实

### F.1 Nextcloud All-in-One（AIO）——"官方一键安装法"，成功但有明确代价

[nextcloud/all-in-one](https://github.com/nextcloud/all-in-one)：**AGPL-3.0，10,418 star，2026-09-13T12:06:09Z push**。README 第一句就是它对自己的定位：
"The official Nextcloud installation method. Nextcloud AIO provides easy deployment and maintenance with most features included in this one Nextcloud instance."

**它把"安装"压缩成了一条 `docker run`**（我读了 README 的完整命令，它不是一行，但确实是一条命令）：

```
sudo docker run --init --sig-proxy=false --name nextcloud-aio-mastercontainer --restart always \
  --publish 80:80 --publish 8080:8080 --publish 8443:8443 \
  --volume nextcloud_aio_mastercontainer:/mnt/docker-aio-config \
  --volume /var/run/docker.sock:/var/run/docker.sock:ro \
  ghcr.io/nextcloud-releases/all-in-one:latest
```

**为了非技术用户，它做的妥协/加分项（README 逐条列出，我逐条读了）：**

| 一般的自托管要用户自己干的事 | AIO 的替代方案 |
|---|---|
| 自己选数据库/缓存/反代 | 一个 `mastercontainer` 编排 Postgres + Redis + APCu + PHP-FPM + Caddy，全部内置 |
| 自己签 Let's Encrypt | **"Automatic TLS included (by using Let's Encrypt)"**，Caddy 在容器内签发 |
| 自己解决域名和 DNS | **"Free deSEC dynamic-DNS domain (`*.dedyn.io`) can be registered directly from the AIO interface — no external domain needed"**，AIO 自动注册域名、保持 DNS 记录更新、并自动启用 Caddy 社区容器 |
| 自己写备份脚本和恢复流程 | **BorgBackup 内置**，且 "Borg backup can be completely managed from the AIO interface, including backup creation, backup restore, backup integrity check and integrity-repair"；"Instance restore from backup archive via the AIO interface included (you only need the archive and the password in order to restore the whole instance on a new AIO instance)" |
| 自己读 changelog 升级 | "Daily backups can be enabled from the AIO interface which also allows updating all containers, Nextcloud and its apps afterwards automatically"；"Easy updates included"；"Update and backup notifications included" |
| 自己配 nginx/子路径/上传限制 | 单域名即可；"Only one domain and not multiple domains are required for everything to work"（README 明说这"usually you would need one domain for each service"）；上传 10 GB、PHP 512 MB 内存上限等都有界面开关 |

**代价（全在 README 里明说）：**

1. **它必须挂 `/var/run/docker.sock:ro`** 才能管理兄弟容器。README 给了退路（`manual-install` 目录、"Can be installed without a container having access to the docker socket"），但默认路径就是给容器 docker socket。
2. **AIO 界面本身要先接受一个自签名证书**：入口是 `https://<IP>:8080`，README 用 `CAUTION` 强调 **必须用 IP 而不是域名**访问 8080，"Accessing via a domain may work temporarily but is likely to break later due to HSTS"。
3. **它把系统形态钉死了**：`--name nextcloud-aio-mastercontainer` 和卷名 `nextcloud_aio_mastercontainer` 都写着 "Do not change this name; mastercontainer updates rely on it."
4. **真实世界的环境坑长得离谱**，README 用专门一节 `Disrecommended VPS providers` 列出来：
   - 老的 Strato/Virtuozzo VPS 有 `numproc` 限制，**AIO 会很快撞到**（附 issue discussion）；
   - **Hostinger 的 VPS 缺一个内核特性**，AIO 跑不起来（附论坛帖）；
   - 官方建议用 **KVM/非虚拟化** 的 VPS（"Docker should work best on them"）；
   - **SD 卡不推荐**（"cripple the performance and they are not meant for many write operations which is needed for the database"）；
   - **Snap 版 Docker 不支持**，README 给出检测命令 `sudo docker info | grep "Docker Root Dir" | grep "/var/snap/docker/"`，并警告**要确认没有在跑的容器之后才能 `sudo snap remove docker`**；
   - SELinux 打开时要加 `--security-opt label:disable`；
   - 还要处理 Docker API 版本不匹配（`DOCKER_API_VERSION=1.44`）。
5. **连"友好"的 AIO 也在 README 里劝退**：`You're encouraged to skim the attached FAQ. While we've tried to make things straightforward, **Nextcloud is a large and flexible platform.** Reading the FAQ will save you time`。
6. 镜像体积：mastercontainer `latest` = **97 MiB**；但它是编排器，真正的大头在 `aio-apache`（**56 MiB**）、`aio-postgresql`（**105 MiB**）等一堆兄弟容器。

**判断：成功，但成功的方式是"把 N 个决定替用户做掉 + 自建域名 + 自建 TLS + 自建备份界面"，也就是把复杂度从用户身上搬到了项目自己身上（要维护 20+ 个容器镜像）。**

### F.2 Home Assistant——非技术用户自托管最成功的样本，靠的是"卖操作系统和硬件"

`home-assistant.io/installation/` 我读了全文。它的核心手法：

1. **不把自己的应用交给用户去装**。给普通用户的是 `home-assistant/operating-system`（**Apache-2.0，7,481 star，2026-09-11T14:18:32Z**）——一个**烧录镜像**；最高配是直接卖硬件 **Home Assistant Green**："comes with Home Assistant Operating System already installed. Connect power and your network, and you're up and running. **No assembly or flashing required.**"
2. **它把每一步难度写在卡片上**，让用户自我筛选。例如 Green 的卡片写 "SKILLS REQUIRED: 兴趣 / 无显示器键盘配置"；树莓派写 "组装树莓派 / 刷写镜像 / 无显示器键盘配置"；Container 那条直接写 **"Using Docker" + "Using Linux command line"**。
3. **它用一张对照表把"便利性"明码标价**（我逐行读了）：

| 能力 | HA OS | Container |
|---|---|---|
| 自动化 / 仪表盘 / 集成 / Blueprints | ✅ | ✅ |
| Apps（附加组件） | ✅ | ❌ |
| **一键更新** | ✅ | ❌ |
| **备份** | ✅ | ❌ |

4. **它把备份做成开关，而不是一份文档**（`common-tasks/general/` 全文读过）：加密 `.tar` 默认存 `/backup`；界面上配周期、时间、**保留份数**（每日 + 留 7 份会自动删旧）、以及**更新前自动备份**；密钥放进 **"emergency kit"** 并要求用户下载保管；**恢复入口同时存在于"现有系统"和"全新设备的 onboarding 欢迎页"**；文档还给出恢复耗时预期 **"about 45 minutes"**，并预先解释恢复期间 UI 会 404。
5. **它有商业闭环**：Home Assistant Cloud（Nabu Casa 订阅）提供 5 GB 备份位置（"always encrypted"）、远程访问、语音。
6. **代价**：Container 用户拿不到 Apps、一键更新和备份——**换句话说，HA 把"好用"和"用官方 OS"绑定了**。这是产品策略，不是技术必然，对一个单人项目来说不可复制。

### F.3 平台化方案：Railway 模板可行，Render 免费层对我们这种应用是灾难

#### Render 免费层（官方文档，我读了 `render.com/docs/free` 的正文）

`https://render.com/docs/free` 是一份非常直白的劝退文档，逐条原文：

- **"Like all Render services, Free web services have an ephemeral filesystem. This means that any changes to your web service's filesystem (uploaded images, local SQLite databases, etc.) are lost every time the service redeploys, restarts, or spins down."**
- **"Paid services can preserve local filesystem changes by attaching a persistent disk, but Free web services cannot."**
- "Render spins down a Free web service that goes 15 minutes without receiving any inbound traffic... Whenever a service spins down, any changes to its local filesystem are lost."重新拉起要 **约一分钟**，期间显示 loading 页。
- **"Free instances have important limitations... Do not use them for production applications."**
- Free Postgres **"expire 30 days after creation"**。
- Free web service **不支持 persistent disks / SSH shell / one-off jobs / 横向扩容**。
- **"Service-initiated traffic threshold"**：如果服务主动发起大量外部流量（文档举的例子就是 **"Accessing an external database" / "Invoking external APIs"**），Render 可能**直接挂起**该服务，只有切付费才能恢复。
- 免费层**确实**支持 custom domains 和 managed TLS certificates——所以"HTTPS 自动化"不是问题，**持久化和"打扰用户"才是**。

对我们意味着什么（逐条对照 CityU Mail Pilot）：

| 我们的行为 | Render 免费层的结果 |
|---|---|
| SQLite 单文件存用户/邮件/报告 | **每次重新部署、重启或 spin-down 全部丢失** |
| worker 每 30 秒读 IMAP、每封信调一次大模型 API | 命中 "service-initiated traffic" 条款，**可能被挂起**；而且 worker 是常驻进程，不是 web service，而**"free instances are not available" 于 background workers** |
| 用户靠邮件里的链接回来登录 | 冷启动 **~1 分钟**，且期间数据是空的 |
| 需要用户自己拿 API key 和邮箱授权码 | 平台模板给不了，仍要用户填 |

**结论：Render 免费层不能用于任何带 SQLite 状态的自托管应用。** 官方的替代建议是"用 Render Postgres"（但免费实例 30 天过期）或"升付费加 persistent disk"。**这不是"妥协一下能跑"，是数据必然损坏。**

#### Railway 模板（我打开了一个真实的、形态和我们最像的模板）

[`railway.com/deploy/pocketbase--pocketbase-4`](https://railway.com/deploy/pocketbase--pocketbase-4)（模板作者 `yunyu950908/pocketbase-railway-template`，创建于 **2026-06-06**，页面显示"5 total projects / 5 active / **100% success on recent deploys**"）。
PocketBase 是"单个 Go 二进制 + SQLite + 管理员 UI"，**和我们的形态几乎一样**，所以这个模板的内容就是我们如果要"一键云部署"需要准备的清单：

页面明确列出的内容（原文要点）：
- **一个 app service + 一个 Railway Volume 挂在 `/pb/pb_data`**，"so PocketBase state survives redeploys"；
- 从**官方 GitHub release 产物**构建，并 **"verifies the release checksum during the Docker build"**；
- Dockerfile builder，**pin 上游版本 v0.39.1**；
- `healthcheck path` = `/api/health`（我们已经有 `/health`，天然对齐）；
- 监听平台注入的 `$PORT`；
- **`RAILWAY_RUN_UID=0`**——为了让服务能写 Railway 的 root 挂载卷，模板显式让容器**以 root 运行**（这是一个真实的安全代价，模板自己写出来了）；
- **部署后仍需用户自己一步**："open the generated Railway domain and visit `/_/` to create the first PocketBase admin account." —— **一键按钮并没有替用户建管理员账号。**

**判断：Railway 模板是"可行"的，成本是：一个 Dockerfile（pin 版本 + 校验和）+ 一个接受 `$PORT` 的配置项 + 文档化"部署后第一步"。** 但它要求我们把应用容器化（也就是我们前面论证过要拒绝的那条路），并且**用户的 API key / 邮箱授权码仍然只能自己在网页里填**——按钮省掉的只是"买服务器 + 装系统"。

#### 平台与托管型自托管的元数据

| 项目 | License | Star | 最后 push | 说明 |
|---|---|---|---|---|
| [YunoHost/yunohost](https://github.com/YunoHost/yunohost) | **AGPL-3.0** | 2,977 | 2026-09-13T06:01:01Z | 面向非技术用户的"自托管发行版"：把应用做成 YunoHost 包、统一 SSO 和备份。文档原话 **"Applications must be packaged manually by application packagers/maintainers"**，并有 0–8 质量分级、掉线就**从目录隐藏并冻结升级**。**2,977 star 说明这条路能走通但走不大**——它要求每个应用额外维护一份打包 |
| [getumbrel/umbrel](https://github.com/getumbrel/umbrel) | **PolyForm Noncommercial License 1.0.0**（我实测 `LICENSE.md` 第一行：「Umbrel is licensed under the PolyForm Noncommercial License 1.0.0」；GitHub API 返回 `NOASSERTION`）→ **不是 OSI 开源，商业使用需授权** | 11,951 | 2026-09-02T22:27:44Z | 靠"卖硬件 + App Store"降低门槛。它的 App Store 治理要求（"without SSH, CLI access, log scraping, or manual file edits"）**值得当验收清单抄**——但那也意味着应用必须放弃一堆自救能力 |
| [coollabsio/coolify](https://github.com/coollabsio/coolify) | Apache-2.0 | 61,756 | 2026-09-13T21:41:59Z | "自托管的 Heroku/Vercel"：帮用户把别人的 Docker 应用部署到自己的 VPS 上。**它的存在本身就是最好的证据**——如果自助安装不难，就不会有一个 6 万 star 的项目专门来解决"帮我在自己服务器上跑别人的应用" |
| [sandstorm-io/sandstorm](https://github.com/sandstorm-io/sandstorm) | **Apache-2.0**（我实测 `LICENSE` 第一行：「Sandstorm is licensed under the Apache License, version 2.0.」；GitHub API 返回 `NOASSERTION`） | 7,078 | 未取到（配额耗尽） | **最重要的失败案例，见 F.4 第 4 条** |
| [nextcloud/all-in-one](https://github.com/nextcloud/all-in-one) | AGPL-3.0 | 10,418 | 2026-09-13T12:06:09Z | 见 F.1 |
| Cloudron | 闭源商业 | — | — | **无公开源码仓库**（子调研查 `cloudron-io/box`、`cloudron-io/cloudron-docs` 均 404；官网只称 Cloudron code 为 "source available"）。**我本人没有打开它的官网**，此条来自子调研 |

#### 一键云按钮：真实成材率（这是唯一拿到的量化数据）

我打开了两份 Railway 模板页，它们**自带部署统计**：

| 模板 | 创建时间/作者 | 总项目 | 活跃项目 | 近期部署成功率 | 内容清单里**有没有**持久卷 |
|---|---|---|---|---|---|
| [Uptime Kuma](https://railway.com/deploy/uptime-kuma) | 2023-06-24 / Brody（第三方） | **1,451** | **672（46%）** | 88% | **没有**（Deployment Dependencies 只列了镜像 `louislam/uptime-kuma:2`） |
| [PocketBase](https://railway.com/deploy/pocketbase--pocketbase-4) | 2026-06-06 / yunyu950908（第三方） | 5 | 5 | 100% | **有**（"One persistent Railway Volume mounted at `/pb/pb_data`"） |

**读法**：Kuma 模板是"一键按钮"最典型的形态——**一个容器、零卷声明**，1,451 个人点过，**一半以上在几天内就不再活跃**（46% active）。这不代表 Uptime Kuma 不好（它自己的 README 推荐 `-v uptime-kuma:/app/data` 命名卷，本来没问题），而是说明**按钮把"容器起来了"当成成功，用户的持久数据与后续维护没人管**。PocketBase 那份相反：明确声明卷、pin 版本、构建时校验 checksum、还写明"部署后自己访问 `/_/` 建管理员"——**这就是"负责任的按钮"该有的样子**。

#### 免费 PaaS 的真实条款

- **Render 免费层**（官方 `docs/free`，我读了正文）：见本节前文详细引用。**"Do not use them for production applications."**
- **Railway**：2023-06-02 公告 —— **"there will no longer be recurring $5 monthly credits for Starter plan users"**，2023-08-01 生效。**免费额度不是长期承诺。**（此条来自子调研，我未亲自打开该公告页。）
- **Fly.io**：官方 `fly.io/docs/about/discontinued-plans/` —— **"Fly.io has deprecated plans as of October 7, 2024"**，原免费额度含 3 GB 卷。（此条来自子调研，我未亲自打开该页。）

#### 按钮清单里可复用与不可复用的（子调研核实）

- **可复用**：AnythingLLM（`Mintplex-Labs/anything-llm`）README 挂了 9 个平台按钮，含 Render 与 Railway 模板 —— **但它的 Railway 链接带 `referralCode`（返利码），照抄前必须删掉**。Chatwoot/Baserow 有 Heroku 按钮；Appwrite 只给 DO/Akamai/AWS Marketplace 按钮。
- **有分量的反向信号**：**uptime-kuma 官方 README 里没有任何云按钮**，反而明确写 `❌ Replit / Heroku`。一个 91k star 的项目主动拒绝云按钮。

### F.4 非技术用户自托管的真实成功率（判断）

把上面所有证据放在一起，可以说得非常具体：

1. **"非技术用户自己装一个带数据库的服务"基本上没有成功过——成功的是"别人替他装"。**
   - HA 的成功来自**卖装了系统的硬件**（Green："No assembly or flashing required"）。
   - Umbrel 的成功来自**卖硬件 + 应用商店**。
   - AIO 的成功来自**用 20+ 个容器替用户做完所有决定，还替用户注册域名**。
   - Coolify（61,756 star）的存在、PikaPods/Elestio 这类"托管开源自托管"生意的存在，说明**市场愿意为"别让我自己装"付钱**。
   - n8n 官方在 Docker 安装文档里直接写："**n8n recommends self-hosting for expert users. Mistakes can lead to data loss, security issues, and downtime. If you aren't experienced at managing servers, n8n recommends n8n Cloud.**"
   - changedetection.io 在 README 正文卖 **$8.99/月** 的托管版，卖点就是 **"Nothing to install"**。
2. **一键云按钮的真实边界**：它省掉的是"买机器 + 装系统 + 装 Docker + 配 HTTPS"，**没有省掉**"注册账号、填 API key、创建管理员、理解数据存在哪"。PocketBase 模板的最后一句话就是"部署后自己去 `/_/` 建管理员"。对 Render 免费层，它连 SQLite 都保不住（"local SQLite databases ... are lost"）。**Railway 的 Uptime Kuma 模板给了唯一的量化答案：1,451 次部署 → 672 个活跃（46%），而且那个模板连持久卷都没声明。**
3. **对一个单人项目，正确的目标不是"非技术用户能装"，而是"技术半吊子（会 ssh、会复制粘贴、不会 debug 系统）的学生能装"**——kuma（91,344 star）、vaultwarden（67,374 star）服务的正是这批人，而它们的安装路径就是**一条 `docker run` 或一份 9 行 compose**。这是唯一被反复验证过的"陌生人能跑起来"的形态。
4. **最重要的失败案例：Sandstorm（7,078 star，Apache-2.0）**。它的产品定位就是"像手机装 App 一样装自托管应用"——正是我们想做的事的极端版本。作者 Kenton Varda 在 2024-01-14 的官方回顾（`sandstorm.io/news`，《Sandstorm now belongs to Sandstorm.org》）里自述：
   - **"Over time, even just basic maintenance became difficult... Sandstorm is something that thousands of users have installed and expect to auto-update. Most of them aren't even aware they are running Mongo."**
   - **"In early 2023, I gave up pushing monthly releases."**
   - 他称它是 **"the project that felt like a failure"**。
   子调研核对 `commits/master.atom`：最近一次实质提交是 **2026-05-26 的 "Remove Google Groups and IRC"**（即只剩清理动作）。
   **失败原因是长期维护的人力，不是技术。** 这是整份调研里对我们最有威胁的一条证据：**"让陌生人也能装"会把"每次上游变动都要有人兜底"的成本永久绑定到作者身上，而我们是单人、2 个真实用户。** 这条直接支持 H 节那个"只做一步、不做平台"的结论。
   （Sandstorm 的 license：我实测 `LICENSE`（无扩展名）第一行确认 Apache-2.0；GitHub API 返回 `NOASSERTION`，与 n8n 一样属于"API 未映射≠无许可"。）
5. **"成功"的定义要收紧**：装得上 ≠ 活得久。三条分水岭是 ① 升级不坏 ② 备份能恢复 ③ **出事有人兜**。第三条是所有纯自助方案都缺的，也正是 PikaPods（"from just $1.80/month"）、Elestio（"No DevOps required"）、Cloudron、Coolify Cloud 全部收入的来源。**所以对我们唯一的现实目标不是"让人人都能装"，而是"让少数几个学生能装上，并且作者知道谁装了、能收到故障告警"。**

### F.5 本节取证说明

F.1（AIO，读了 `readme.md` 132 KB 正文的关键节）、F.2（HA，读了 `installation/` 与 `common-tasks/general/` 全文）、F.3（Render `docs/free` 的正文、Railway PocketBase **与 Uptime Kuma** 两份模板页、Umbrel `LICENSE.md`、Sandstorm `LICENSE`）、F.4 的判断，都是**我自己逐页打开核实的**。

**未取证、因此不给数字的**：Cloudron 官网（我**没有打开**）、PikaPods、Elestio（**均未打开**）；Cloudron 无公开源码仓库这一条来自子调研。

**由并行子调研产出、我本人未逐页核实、已明确标注的**：Railway 2023 取消 $5 额度、Fly.io 2024-10-07 弃用计划、AnythingLLM 的 9 个按钮与 referralCode、YunoHost 的打包与质量分级机制、Cloudron 无源码仓库、PikaPods/Elestio 的定价话术、Sandstorm 的作者自述引文（**引文来自子调研；我只独立核实了它的 license 与"最近提交只剩清理动作"这一点**）。这些结论的子调研全文在 `docs/selfhost-nontechnical-recon-2026-09-14.md`。

**未查到、我没有替它下结论的**：Nextcloud 官方是否用过 "non-technical users" 这个字面说法（子调研在 README 全文、nextcloud.com/install、2021 发布博客里都没找到；我在 AIO README 里找到的实际表述是 "easy deployment and maintenance"，所以本报告 F.1 **不引用**"官方称面向非技术用户"这一说法）；PeerTube "不做一键部署"的原句（未找到）。

## G. 可以直接复用 / 只值得借鉴 / 应该拒绝

| 归类 | 对象 | 许可证 | 具体怎么用 |
|---|---|---|---|
| **可以直接复用的产物** | [n8n-hosting](https://github.com/n8n-io/n8n-hosting) 的 `docker-caddy/docker-compose.yml`（Caddy + `caddy_data` 卷 + Caddyfile，自动签证书）与 `docker-compose/withPostgres/docker-compose.yml` 里的 **`initContainer` 一次性 `chown -R 1000:1000` 模式** | **MIT**（该模板仓库 `LICENSE.md` 是 MIT；注意 n8n 主仓库是 Sustainable Use License，两者不同） | 模板本身 MIT，可合法抄结构与注释；`initContainer chown` 和"把可配置项全部外推到 `.env`"两条做法直接用于我们的 compose/安装脚本（如果我们做的话） |
| **可以直接复用的产物** | [uptime-kuma `compose.yaml`](https://github.com/louislam/uptime-kuma/blob/master/compose.yaml)（9 行、193 字节） | MIT | "单服务 + 单卷 + 单端口 + `restart: unless-stopped`"的最小 compose 形态，是我们这种单进程应用的正确形状 |
| **可以直接复用的产物** | n8n 官方 `docker/get-n8n.sh` 的**参数语义**（`--version` 预演 / `--no-start` / `--upgrade` 只改版本号 / 失败报告先问后发 / "先 `less` 再执行"提示） | 脚本随 n8n 主仓库（非 MIT）→ **只抄语义，不要抄代码** | 我们的 `install.sh` 直接采用同款 flag 命名与契约 |
| **可以直接复用的产物** | vaultwarden 的备份 wiki 结构：**逐文件标注"必须/建议/可选"备份 + SQLite 在线备份命令 + WAL 恢复陷阱 + "定期做恢复演练"** | AGPL-3.0 → **文档结构可学，正文不要整段抄** | 我们的 `docs/` 里应该有对应的一页，且必须补上现在缺失的"恢复" |
| **可以直接复用的产物** | **Umbrel App Store 的验收句**："without SSH, CLI access, log scraping, or manual file edits"（子调研核实） | 概念，无版权 | 当**反面对照表**用：我们的安装器不满足这条（我们的设计恰恰依赖"能 ssh 上去看 systemd 日志"）——**这提醒我们：目标用户必须能 ssh，别假装不是。** |
| **可以直接复用的产物** | **Render 的 `render.yaml` + Deploy 按钮机制**（如果想要一个"托管演示实例"按钮） | 平台机制 | 低成本；但仅适用于**无状态演示**，正式数据仍必须落盘（见 F.3 Render 条款） |
| **只值得借鉴的设计** | Nextcloud `.examples/` 的 **nginx-proxy + acme-companion** 全栈（反代 + 自动 Let's Encrypt 一份 compose 搞定） | AGPL-3.0 | 借鉴"把 TLS 自动化做成栈的一部分"；但它要挂 `/var/run/docker.sock`，对我们 2 GB 单机是净负担 |
| **只值得借鉴的设计** | AIO 的 **BorgBackup 一键备份/恢复模型**（界面里能做 create / restore / integrity check / integrity-repair，且恢复只需"归档 + 密钥"） | AGPL-3.0 | 这是"备份产品化"的标杆；与我们"只差一个恢复入口"的现状直接对应 |
| **只值得借鉴的设计** | AIO 用 **DockerSocketProxy**（而非裸 socket）来换取网页升级能力 | AGPL-3.0 | 借鉴"要能力就给受限代理而不是裸权限"这个思路；但我们不需要，因为我们的安装器直接跑在宿主上 |
| **只值得借鉴的设计** | YunoHost 的**人工打包 + 0–8 质量分级 + 掉线即从目录隐藏并冻结升级** | AGPL-3.0 | 如果哪天要支持多校/多实例，这是"用制度而不是代码保证质量"的现成范式 |
| **只值得借鉴的设计** | Immich 的**内置每日备份（默认留 14 份）+ 网页恢复 + 恢复前自动 restore point + 失败自动回滚 + 恢复后自动迁移与健康检查** | AGPL-3.0 | 这是"备份要产品化、不要只给文档"的最佳范例；我们的 `backup.py` 已经有"SQLite 在线备份 + 留 7 份"，缺的是**恢复入口** |
| **只值得借鉴的设计** | Home Assistant 的**"备份加密 + emergency kit + onboarding 恢复 + 更新前自动备份 + 保留份数"**与它的**难度标签卡片**（把"需要会 Docker / 会用命令行"写在选择页上） | Apache-2.0 | 难度标签这个做法成本极低、收益极高，我们可以在安装文档开头直接照做 |
| **只值得借鉴的设计** | **NetBox 的"版本目录 + 切软链 + 显式保留 `/etc` 与数据 + `upgrade.sh`"**；**headscale 的 `config\|noreplace` + `postinst`（`$2` 非空=升级→restart）** | Apache-2.0 / BSD-3-Clause | 都是宽松许可，可以逐行改成我们的 `install.sh` / `upgrade.sh` / `uninstall.sh` |
| **应该拒绝的** | **为了这个项目引入 Docker / Compose** | — | 真实数字：生产机是 **2 vCPU / 2 GB**，而 Immich 官方要求 **6–8 GB RAM**、Nextcloud `stable` 镜像 **490 MiB**、HA `stable` **615 MiB**。Docker 让升级变简单却**不提供回滚**（Immich："Downgrading… is not supported"），还会新增 docker.sock、宿主卷属主、NFS+SQLite 损坏等一整类新故障。收益换不回代价。 |
| **应该拒绝的** | **以 kuma/changedetection 那种"没有回滚、没有迁移说明、备份=拷目录"的做法为准** | — | kuma v2 删掉了内置 JSON 备份；changedetection 的 wiki `Backups.md` 我实测 **404**。我们已有的 `backup.py`（在线备份 + 保留 7 份）比它们都规整，不要往下对齐。 |
| **应该拒绝的** | **把 n8n 的代码/文档正文当可复用素材** | Sustainable Use License（限制商业用途） | 目录名含 `.ee.` 的文件需商业授权；GitHub API 返回 `NOASSERTION`。只借鉴工程语义。 |
| **应该拒绝的** | **pipx / uv tool 作为"分发方案"** | — | 它们只管 venv：vdirsyncer 官方文档原话是 pipx 安装**不含 systemd 服务**；`/etc` 配置、密钥、SQLite 数据目录、卸载清理一概不管。对我们这种"要装 systemd 单元 + nginx 站点 + 证书"的服务，省下的只是 `pip install` 那一行。 |
| **应该拒绝的** | **把 docker socket 交给 Web 层** | — | AIO 的做法（且已用 DockerSocketProxy 缓解），但争议是真的：nextcloud/app_api issue #531 原话 **"Attaching a docker socket to a web service is a significant security risk which home users are likely to take unknowingly"**、**"This is effective root access on the Docker host."** 我们的 Web 层是标准库 `http.server`，**没有任何理由让它摸到宿主 root**。 |
| **应该拒绝的** | **不透明的 referralCode 按钮** | — | AnythingLLM README 的 Railway 链接带返利码。**要放按钮就不许带返利**——否则"帮你部署"变成"赚你钱"，目标用户无法分辨。 |
| **应该拒绝的** | **把免费 PaaS 免费档当"非技术用户托管"** | — | Render 官方："Do not use them for production applications."；免费层无持久盘、15 分钟休眠且休眠即丢本地文件、免费 Postgres 30 天过期、无 background worker、主动外部流量可能被挂起（我们正是这种负载）。Railway 2023-08-01 起取消循环额度；Fly.io 2024-10-07 弃用免费计划。 |
| **应该拒绝的** | **把关键路径押在单一维护者身上（含我们自己）** | — | Sandstorm 7,078 star、Apache-2.0、定位就是"让自托管像装 App 一样简单"，作者自述 **"I gave up pushing monthly releases"**、称它是 **"the project that felt like a failure"**，原因是维护人力不是技术。**这一条是对本项目最直接的警告：不要承诺"任何人装了都能自动升级"，因为兜底的人只有你一个。** |

## H. 结论：最小可行的一步

**这一步是：把 `pilot_app/deploy_pilot.sh` 结尾那句"下一步：修改 `pilot.env` 的域名，配置 Nginx/HTTPS，然后创建邀请码"变成脚本自己干完的事——把域名做成参数（默认从本机公网 IP 推导出 `<ip>.sslip.io`），写进 `INFE_PILOT_ORIGIN`，用仓库里**已经存在但从未被脚本安装过**的 `pilot_app/nginx-cityu-mail-pilot.conf.example` 渲染出真实 nginx 站点并 `enable`，然后跑一次 certbot 拿证书。**

### 为什么是这一步，而不是别的

1. **它是唯一一处"陌生人的安装会停在半路"的地方，而且只有这一处。**
   我逐行读了 `deploy_pilot.sh`（82 行）。它已经做了：建系统用户 `cityumail`、装 `/opt/cityu-mail-pilot`、建 `/etc/cityu-mail-pilot`（0700）、**用 `openssl rand -base64 32` 自动生成主密钥**、建 `/var/lib` 与 `/var/backups`、安装 5 个 systemd 单元 + certbot/backup 的 `OnFailure=` drop-in、`daemon-reload` + `enable --now`。
   也就是说"安装"这件事**已经做完了 80%**，而且它的第 39 行 `if [[ ! -f "$CONFIG_DIR/pilot.env" ]]` 已经实现了 n8n 那条最关键的契约——**重跑不覆盖配置和密钥**。
   它唯一没做的，是**让服务变成可用的**：装完停在 `127.0.0.1:8787` + `INFE_PILOT_ORIGIN=https://mail.example.com`（占位符）+ `INFE_PILOT_COOKIE_SECURE=1`。我 grep 过全仓库：**没有任何脚本调用过 certbot，`nginx-cityu-mail-pilot.conf.example` 也从未被安装到 `/etc/nginx`。**

2. **"反代交给用户"这个惯例，对我们只能部分套用——而且这恰恰说明了为什么值得自动化。**
   先把话说清楚，避免自欺：
   - kuma / Immich / changedetection 把 TLS 推给用户是**没有代价**的，因为它们的应用不依赖 HTTPS：kuma README 直接说 "Uptime Kuma is now running on all network interfaces (e.g. http://localhost:3001)"，改 `-p 127.0.0.1:3001:3001` 就能只看本机并进安装向导；Immich 的 compose 是 `'2283:2283'`，装完开 `http://<ip>:2283` 就有欢迎页。changedetection 官方 compose 写死 `127.0.0.1:5000:5000`；vaultwarden README 的 `docker run` 是 `--publish 127.0.0.1:8000:80`。
   - **但 vaultwarden 是一个诚实的反例，而且它和我们的约束一模一样。** 它 README 第 62–64 行明说：网页端要用 Web Crypto API，"will only work if you enable HTTPS"。**它同样"没有 HTTPS 就不能用"，但它依然把这一步交给用户**——代价是它必须维护三份 Wiki：`Enabling-HTTPS.md`（10.6 KB）、`Proxy-examples.md`（27 KB，覆盖 Caddy/nginx/Apache/Traefik 等）、外加 `Using-Docker-Compose` 里的 Caddy 示例，并且明确"如果你不熟悉反代，**优先考虑 Caddy**，因为它内置 Let's Encrypt"。
   - 所以业界的成熟答案其实是**两条**：(a) 把 nginx + certbot 做进安装器；(b) 不做进安装器，但把它做成**不可跳过的、有可复制命令的、装完自检的前置条件**（vaultwarden 走的是 (b)，而且做得极其彻底）。
   - **对我们，(a) 更划算，理由是成本不对称**：vaultwarden 要支持任意发行版 + 任意反代 + 任意拓扑（Docker/裸机/K8s/Cloudflare），所以它只能写文档；而我们的目标形态是**固定的**（Ubuntu + nginx + certbot + 单机 + sslip.io 已在本项目生产环境验证过），所以自动化是几十行 shell，而文档是"每次都要用户自己 debug 的坑"。**我们比 vaultwarden 简单，所以我们可以做到比它更好的安装体验——这才是本项目该拿的便宜。**

3. **"学生没有域名"这个最硬的坎，本项目已经跨过去了，只是没意识到可以复用。**
   AIO 为了非技术用户做的最大让步之一是：**"Free deSEC dynamic-DNS domain (`*.dedyn.io`) can be registered directly from the AIO interface — no external domain needed"**，并自动配好 Caddy 反代。
   本项目的生产环境用的是**完全同一招**：`pilot.example.com`。这条路径在仓库里有记录且已被验证——`VERIFICATION-legacy-migration-2026-09-13.md:282` 写的是"`sslip.io` 通配解析（`<ip>.sslip.io` → 该 IP），**无需购买域名**即可申请证书"，`HANDOVER-BRIEF.md:64` 记录 Let's Encrypt 证书已签发、`certbot renew --dry-run` 成功。
   所以默认值不是"让用户去买域名"，而是"**你什么都不用给，我用你的公网 IP 生成一个能签证书的主机名**"；有域名的用户用 `--origin https://mail.example.edu` 覆盖。
   **AIO 用一整套 mastercontainer 编排才换来的这条体验，我们用一个 `ip -4 addr` + 字符串替换 + certbot 就能拿到——因为我们的技术栈本来就轻。**

### 具体改动清单（一个 PR 的大小）

```bash
# 目标形态（三条命令，全部在一台干净的 Ubuntu 上）
curl -LO https://github.com/<owner>/cityu-mail-pilot/releases/download/v0.19.0/cityu-mail-pilot-0.19.0.tar.gz
shasum -a 256 -c cityu-mail-pilot-0.19.0.tar.gz.sha256
tar xzf cityu-mail-pilot-0.19.0.tar.gz && sudo bash pilot_app/install.sh --admin-email you@example.com
```

`install.sh` = 现在的 `deploy_pilot.sh` + 四件事：

1. **参数与默认值**：`--origin`（不给就探测公网 IP → `https://<ip-dashed>.sslip.io`）、`--admin-email`（写进 `INFE_PILOT_ADMIN_EMAILS`）、`--port`、`--dry-run`（只打印将要做的事）、`--no-start`、`--upgrade`、`--uninstall [--purge]`。
   保留现有的 `if [[ ! -f pilot.env ]]` 守卫，并把 `--upgrade` 的语义明确成 n8n 那句话的等价物：**"只替换 `/opt/cityu-mail-pilot/pilot_app` 代码目录并重启单元，绝不碰 `/etc/cityu-mail-pilot/pilot.env` 与 `/var/lib/cityu-mail-pilot/` 里的任何东西"**（现在脚本事实上就是这么做的，只是没人写下来）。升级前先调用一次已有的 `python -m pilot_app.backup`，失败就中止升级。
2. **preflight（装之前先失败，而不是装完才失败）**：Ubuntu 版本、`python3 --version` 是否满足 `pilot_app/README.md:117` 写的 **3.9+**、`command -v openssl nginx certbot`、80/443 是否被占、`sslip.io` 是否解析到本机（`getent hosts <ip>.sslip.io`）、能否连到 Let's Encrypt。任何一条不过就打印确切修法后退出——Immich 安装页就是靠"预告具体报错 + 给修法"来兜底的。
3. **把 nginx 站点与证书做完**：把 `nginx-cityu-mail-pilot.conf.example` 里的 `server_name mail.example.com` 换成真实主机名、装到 `/etc/nginx/sites-available/` 并 link 到 `sites-enabled/`、`nginx -t && systemctl reload nginx`、`certbot --nginx -d <host> --non-interactive --agree-tos -m <admin-email>`（`certbot.timer` 的 `OnFailure=` 已经挂好了，续期失败会被哨兵捕获——这一环我们比多数项目都完整）。
4. **补上卸载与恢复**：`--uninstall` 默认**保留** `/etc/cityu-mail-pilot` 与 `/var/lib/cityu-mail-pilot`，只有 `--purge` 才删（headscale/k3s/Pi-hole 都是这个约定）；再在 `docs/` 里加一页**恢复演练**步骤（`systemctl stop` 两个服务 → 从 `/var/backups/cityu-mail-pilot/pilot-*.sqlite3` 挑一份 → `cp` 回 `INFE_PILOT_DB` → 起服务 → 核对 `/health` 与用户数），因为全仓库现在 grep 不到任何恢复流程，而 vaultwarden/Immich/HA 三家都明确要求"定期演练恢复"。

**为什么不是别的"一步"：**
- **不是 Docker**：见 G 表。2 vCPU / 2 GB 的机器上，Immich 类 compose 栈要求 6–8 GB；kuma 单容器实测就 800 MB。Docker 只让升级更顺，**不提供回滚**，换来的是一整套新的故障面（docker.sock、卷属主、NFS+SQLite 损坏），而我们的 1 个直接依赖（锁文件 4 个包）本来就没有环境漂移问题。
- **不是 pipx / uv**：它们只管 venv。对我们的场景（systemd 单元 + `/etc` 密钥 + nginx + 证书 + SQLite 数据 + 卸载）省下的只有 `pip install -r requirements.lock` 这一行。
- **不是 deb/rpm**：用户体验确实最好（`apt install`、conffile 保配置、`dpkg -r` 卸载），但要引入 `dh-virtualenv`/`nfpm` + 打包流水线 + 签名 + 一个 apt 源，**作者成本远高于现在这一步**，而我们只有 2 个真实用户。等用户到 20 个再考虑。
- **不是 PyInstaller / zipapp**：它们解决的是"目标机没有 Python"，而我们的前置条件是"Ubuntu + 能跑 systemd"，Python 本来就在；`zipapp` 甚至装不下 `cryptography` 这个 C 扩展（zipapp 官方 Caveats：C 扩展不能从 zip 里加载）。

### 这一步的边界（比这一步本身更重要）

**做完这一步就停。** 不要顺手做"应用商店 / 一键升级所有人 / 多租户托管面板"。

理由是 Sandstorm（7,078 star、Apache-2.0、目标与我们高度重合）留下的实证：作者自述 **"I gave up pushing monthly releases"**、称它是 **"the project that felt like a failure"**，而失败原因是**长期维护的人力**，不是技术。落到我们身上就是两条具体红线：

1. **不承诺"装了就会自动升级"**。升级必须由装的人自己触发（`--upgrade`），因为只有你知道自己的数据有多重要；我们只保证"升级不碰 `pilot.env` 与数据库，升级前自动备份"。这和 Kuma 的做法一致（"升级=你自己把命令再跑一遍"），也和 n8n 的 `--upgrade` 契约一致。
2. **要能知道"谁装了"**。现在只有 2 个用户，作者仍能靠 `OnFailure=` 告警 + 巡检哨兵兜底。一旦陌生人装到作者不知道的机器上，**"出事有人兜"这条就断了**（PikaPods/Elestio/Cloudron 赚的正是这笔钱）。所以在放开安装之前或同时，必须有一个明确的"这三台机器在谁的告警范围内"的名单，否则宁可先只发给 1–2 个愿意跟你一起看日志的同学——**这正是 kuma/vaultwarden 那类项目不需要管的事，而我们必须管，因为我们没有团队。**

