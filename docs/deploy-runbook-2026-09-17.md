# 自己上线：一条命令，以及它替你记住的事

> 面向的是**运营者自己**（不是开发者）：改完东西，怎么把它安全地送上去、怎么知道真的上去了、
> 出错怎么退回来。首次安装一台新服务器在 `README.md` 的「快速开始」，这份只管**升级**。
>
> 对应工具：`tools/deploy_prod.sh`（2026-09-17 起）。

## 1. 先分清三种改动，它们的路不一样

这是最容易白忙一场的地方：**不是所有改动都需要上线。**

| 你改了什么 | 它进服务器吗 | 你要做什么 |
|---|---|---|
| `pilot_app/` 里的东西（页面、逻辑、邮件模板、systemd 单元） | **进**（发布包里只有 `pilot_app/`） | `bash tools/deploy_prod.sh` |
| `tools/`、`docs/`、`README.md`、`.github/` | **不进** | 只要推 GitHub：`tools/publish_push.sh` |
| 环境变量（`/etc/cityu-mail-pilot/pilot.env`）、域名、证书 | 不在仓库里 | 手动 `ssh` 改文件 + `systemctl restart`，**脚本不碰它** |

换句话说：**只改了文档和工具，跑上线脚本是白跑一趟**（不会坏，但没必要）。

## 2. 上线前只有两件事要人做

1. **改版本号**（唯一的必填）：`pilot_app/__init__.py` 第 14 行 `__version__ = "0.63.75"`。
   它是 `/health` 返回的那个版本，也是包名、也是"装上去的是不是这一份"的唯一凭据。
2. 想清楚这一版**改了什么**——脚本会在最后把「该量的都量一遍」，但你得知道**该量什么**：
   新功能要自己按一遍（v0.57.1 的 `run_backup` 就是单测与浏览器全绿、线上一次都没成功过）。

浏览器检查（20 个套件）不在这条命令里 —— 它要几分钟，属于"改完 UI 之后"的检查：

```bash
bash tools/run_browser_checks.sh          # 全部；也可以只跑一个套件
```

## 3. 那条命令

```bash
cd <你 clone 下来的目录>          # 这份 runbook 里的路径都相对于仓库根
bash tools/deploy_prod.sh
```

它会按顺序做八件事，**中间任何一步不成立就停**：

| 步 | 做什么 | 为什么必须有 |
|---|---|---|
| 1 | 读版本、检查私钥、`ssh` 试连（BatchMode，不问密码） | 连不上就别打包了 |
| 2 | 跑全部单测，**看退出码** | 单测没过就不上线 |
| 3 | `build_release.sh` 打包 + 生成 sha256 | |
| 4 | `scp` 到服务器 `/tmp`，**在服务器上重算 sha256** | 传输截断过一次的话，解包照样成功、程序少文件 |
| 5 | 解包到 `/tmp/d<版本>`（**不带 `--strip-components`**） | 包里顶层就是 `pilot_app/`，剥一层就找不到安装器 |
| 6 | `sudo bash pilot_app/deploy_pilot.sh --upgrade` | 它自己**先备份数据库与 `pilot.env`**，再换代码、重启 web 与 worker；备份失败它会中止 |
| 7 | 等 `/health` 返回 200 **且版本号等于本地这一版**（最多 15 次 × 2 s） | 刚重启的第一次请求可能是 502；不重试的话每次上线都看到一次假红 |
| 8 | 六个单元 active + 五个静态文件**逐字节**比对 + 首页/应用外壳/匿名边界 | 「页面能打开」不等于「新的那份上去了」 |

想先看不做：

```bash
bash tools/deploy_prod.sh --dry-run       # 打包 + 打印会执行什么，不连生产
bash tools/deploy_prod.sh --verify-only   # 不上线，只把线上现在这一版验收一遍
bash tools/deploy_prod.sh --skip-checks   # 跳过单测（只在你刚刚亲眼见过全绿时用）
```

**每天开机想知道"线上还是不是好的"就跑 `--verify-only`** —— 它量的就是那四件事，
所以"上次是好的"和"现在是好的"不会混起来。

## 4. 它为什么写死在代码里（而不是留给你记）

这几条都是踩过的，每一条删掉一行就会回来：

* **六个单元按真名查**：`systemctl is-active backup.timer` 永远是 `inactive` ——
  不是故障，是少了 `cityu-mail-pilot-` 前缀。
* **判 `active` 要看整个字段**：`is-active` 返回的 `inactive` 里也含 `active`，
  子串判断会把停掉的单元判成好的。
* **`/health` 要重试**：第一次 502 是重启的正常现象。
* **静态文件比字节**，不比"页面能打开"：`index.html`、`app.js`、`landing.js`、
  `theme-boot.js`、`manifest.webmanifest` 是原样下发的；
  `/` 与 `/app` 是**服务端渲染**的（账号数、源码地址），所以只能查"200 + 关键标记"。
* **匿名访问 `/api/admin/users` 必须是 401**：**404 是"登录了但不是管理员"的答案**，
  两者混起来，脚本就会去证明一个它根本没碰过的边界。

## 5. 出问题怎么办

| 现象 | 多半是什么 | 怎么办 |
|---|---|---|
| 卡在第 1 步「连不上」 | 网络、私钥权限（`chmod 600`）、`PILOT_HOST`/`PILOT_SSH_KEY` 写错 | 修好再跑；脚本不会问密码 |
| 单测没过 | 代码真坏了 | 修代码。**不要 `--skip-checks`** |
| 第 8 步「版本对不上」 | 装的是别的包 | 去服务器 `/tmp/d<版本>` 看 tar 的 sha256；重跑一次 |
| 第 8 步「某个静态文件不同」 | 没换成功（缓存/打包漏文件） | 重跑一次；还不行就 `ssh` 上去看 `/opt/cityu-mail-pilot/pilot_app/static/` |
| 页面 502 / 打不开 | web 起不来 | `sudo journalctl -u cityu-mail-pilot-web -n 100`；必要时回退 |
| 服务没事但功能不对 | 新功能的逻辑问题 | 按 **§6 回退**，别在线上改 |

## 6. 回退

数据库与配置**每次升级前都自动备份**在服务器的 `/var/backups/cityu-mail-pilot/`。
代码回退 = 拿上一版的 `dist/cityu-mail-pilot-<旧版本>.tar.gz` 再走一遍同样的路：

```bash
scp -i ~/.ssh/pilot-deploy dist/cityu-mail-pilot-<旧版本>.tar.gz{,.sha256} ubuntu@<主机>:/tmp/
ssh -i ~/.ssh/pilot-deploy ubuntu@<主机> \
  'cd /tmp && sha256sum -c cityu-mail-pilot-<旧版本>.tar.gz.sha256 \
   && rm -rf /tmp/dold && mkdir /tmp/dold && tar -xzf cityu-mail-pilot-<旧版本>.tar.gz -C /tmp/dold \
   && cd /tmp/dold && sudo bash pilot_app/deploy_pilot.sh --upgrade'
```

**什么时候只需要回代码、什么时候要连数据一起恢复**：代码回退永远只动 `pilot_app/`；
只有数据本身被写坏了才需要从 `/var/backups/cityu-mail-pilot/` 恢复数据库，
而那一步要**先停下来**、并且手上有主密钥（备份是用它加密的，见 `docs/backup-2026-09-14.md`
与 `docs/restore-drill.md`）。

## 7. 这套东西不负责什么

* **不改 `pilot.env`**：换域名、换平台 key、加管理员邮箱都走 `set_platform_key.sh` 或手改文件 + 重启（装 key 的完整命令见 `docs/platform-key-2026-09-14.md`）。
* **不推 GitHub**：仓库里会公开的东西改完，另跑 `tools/publish_push.sh`（它有自己的两道闸门）。
* **不造数据、不发通知**：真实用户的操作（发广播、替别人改配置、发提醒）永远要人点头。
* **不保证"新功能对"**：它保证"新的那份确实上去了、服务是活的、边界没漏"。功能对不对，
  只能自己去点上两下 —— 这也是唯一不能自动化的那一步。

## 8. 服务器上的临时目录：每次部署都会留，曾经攒到 781 MB（2026-09-26）

**症状**：那天在服务器 `/tmp` 里解一个 12 MB 的包，`tar` 报
`Cannot write: Disk quota exceeded` —— 看起来像包坏了，其实是**盘满了**。

**为什么**：每次部署都会在 `/tmp` 留下两样东西，**没有任何东西会删它们**：

* 一棵解开的树 `/tmp/d<版本>/`（约 7 MB、**一万五千个文件**）；
* 一份 `cityu-mail-pilot-<版本>.tar.gz`（3.6 MB）与它的 `.sha256`。

到 2026-09-26 那次数的时候：**21 棵**（`d1.5.0` 一路到 `d1.5.21`）+ **50 份 tar.gz** = **781 MB**。
而**`/tmp` 是 tmpfs** —— 那 781 MB 是**这台 2 GB 机器的内存**（也解释了它为什么在 swap 里
蹲着 500–700 MB）。清掉之后 `/tmp` 从 **80% → 10%**（781 MB → 97 MB），内存跟着松开。

**怎么防**：`tools/deploy_prod.sh` 现在最后多一步「打扫服务器上的临时目录」——
**验收通过之后**删掉 `/tmp/d<版本>/`，tar.gz 只留最近 3 份
（回退按 §6 是拿**本机** `dist/` 那一份再来一遍，服务器这份只是就近的保险）。
**验收失败时不打扫**：那时留着现场给人看（脚本在那之前就 `exit 1` 了）。

**手工清一遍**（要保留正在用的那一版，别整个 `rm -rf /tmp/d*`）：

```bash
ssh -i ~/.ssh/pilot-deploy ubuntu@203.0.113.10 \
  "sudo sh -c 'ls -d /tmp/d* | grep -v \"^/tmp/d1.5.21\$\" | xargs rm -rf'"
ssh -i ~/.ssh/pilot-deploy ubuntu@203.0.113.10 \
  "sudo sh -c 'ls /tmp/cityu-mail-pilot-*.tar.gz | grep -v 1.5.21 | xargs rm -f'"
# 核一下：df -h /tmp 与 du -sh /tmp 要对得上（对不上就还有别的东西在占）
```

**这条为什么值得写进手册**：它不影响任何功能，只在**某一次部署**上突然变成「装不上去」，
而报错信息（`Disk quota exceeded`）指向的是包而不是盘。
