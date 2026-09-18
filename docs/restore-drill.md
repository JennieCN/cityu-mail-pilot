# 从备份恢复：演练步骤与真实步骤

> 备份每天都在跑（`backup.timer`，每天 03:2x），保留最新 7 份，存在 `/var/backups/cityu-mail-pilot/`。
> 但**"有备份"和"能恢复"是两件事**。这份文档给的是后者。

---

## 0. 先做演练（不改动任何东西，随时可跑）

```bash
sudo bash -c 'cd /opt/cityu-mail-pilot && set -a && . /etc/cityu-mail-pilot/pilot.env && set +a \
  && .venv/bin/python -m pilot_app.manage restore-drill'
```

它会在**临时目录里的副本**上做三件事，绝不碰线上数据库：

1. `PRAGMA integrity_check` —— 文件结构是否完整
2. 用**当前主密钥**解密其中一份邮箱授权码 —— 这一条才是关键
3. 打印备份与线上的行数对比

**为什么第 2 条最关键**：在另一把主密钥下生成的备份，**完整性检查会通过，但它毫无用处**。
只看结构检查会得出"备份没问题"的错误结论。演练会明确拒绝这种备份：

```
✓ 完整性检查：ok
✗ 无法用当前主密钥解密 u@example.com 的邮箱授权码：SecurityError
✗ 这份备份解不开——很可能它是在另一把主密钥下生成的。
```

**建议频率**：每次升级后跑一次；没有升级时每月一次。
Vaultwarden、Immich、Home Assistant 三家都在官方文档里明确要求定期演练，理由相同。

查看可用的备份：

```bash
sudo ls -lt /var/backups/cityu-mail-pilot/ | head
# 指定某一份：... restore-drill --backup /var/backups/cityu-mail-pilot/pilot-20260914T032500Z.sqlite3
```

---

## 1. 真的需要恢复时

**先判断要恢复到什么程度。** 三种情况，代价差别很大：

| 情况 | 做法 | 代价 |
|---|---|---|
| 只是数据被误删/误改 | 见 §1.1（换库） | 几分钟 |
| 整个服务坏了 | 见 §1.1 + 必要时重装 | 十几分钟 |
| 服务器都没了 | 先按 `deploy_pilot.sh` 重装，再 §1.1 | 半小时 |

### 1.1 换库（最常用）

```bash
# 0) 记下当前状态，事后用来对比
curl -s https://<你的域名>/health
sudo ls -l /var/lib/cityu-mail-pilot/

# 1) 停掉两个会写数据库的服务（只停 worker 不够，web 也会写）
sudo systemctl stop cityu-mail-pilot-worker cityu-mail-pilot-web

# 2) 把现在这份留在原地（不要删，后面可能还要拿它对照）
sudo cp -a /var/lib/cityu-mail-pilot/pilot.sqlite3 \
           /var/lib/cityu-mail-pilot/pilot.sqlite3.before-restore-$(date -u +%Y%m%dT%H%M%SZ)

# 3) 用备份覆盖。注意：SQLite 的 -wal / -shm 是上一份数据库的残留，必须一起清掉，
#    否则新库会带着旧库的 WAL 打开，是最容易踩的坑。
sudo rm -f /var/lib/cityu-mail-pilot/pilot.sqlite3-wal \
           /var/lib/cityu-mail-pilot/pilot.sqlite3-shm
sudo cp /var/backups/cityu-mail-pilot/pilot-<时间戳>.sqlite3 \
        /var/lib/cityu-mail-pilot/pilot.sqlite3
sudo chown cityumail:cityumail /var/lib/cityu-mail-pilot/pilot.sqlite3
sudo chmod 0600 /var/lib/cityu-mail-pilot/pilot.sqlite3

# 4) 起服务
sudo systemctl start cityu-mail-pilot-web cityu-mail-pilot-worker
sleep 5

# 5) 核对（四条都要看）
curl -s https://<你的域名>/health                 # version 正确
curl -s https://<你的域名>/health >/dev/null && echo ok
sudo journalctl -u cityu-mail-pilot-worker -n 20 --no-pager   # 没有 traceback
sudo bash -c 'cd /opt/cityu-mail-pilot && set -a && . /etc/cityu-mail-pilot/pilot.env && set +a \
  && .venv/bin/python -m pilot_app.manage restore-drill'      # 换了库之后再演练一次
```

**核对要点**：恢复的是**数据库**。如果备份比现在旧，那段时间内注册的账号和生成的报告会消失——
这是备份的语义，不是故障。用户若在这段时间发过信，`messages` 里那条记录没了，**worker 会把它当成新邮件重新处理并重发一次报告**（UID 游标也在库里）。

### 1.2 如果连 `pilot.env` 也丢了

没有主密钥，数据库里的邮箱授权码和 API key **全部解不开**，演练会报 `SecurityError`。
这时只有两条路：

- 找到 `pilot.env` 的备份（部署前会自动备份到 `/root/pilot-predeploy/pilot.env.<时间戳>`）
- 找不到就只能让用户重新填一次邮箱授权码和 API key

**这就是为什么部署前必须 `cp -a` 一份 `pilot.env`。**

---

## 2. 回退一次恢复

换库前第 2 步留下的 `pilot.sqlite3.before-restore-*` 就是退回用的：

```bash
sudo systemctl stop cityu-mail-pilot-worker cityu-mail-pilot-web
sudo rm -f /var/lib/cityu-mail-pilot/pilot.sqlite3-wal /var/lib/cityu-mail-pilot/pilot.sqlite3-shm
sudo cp /var/lib/cityu-mail-pilot/pilot.sqlite3.before-restore-<时间戳> \
        /var/lib/cityu-mail-pilot/pilot.sqlite3
sudo chown cityumail:cityumail /var/lib/cityu-mail-pilot/pilot.sqlite3
sudo systemctl start cityu-mail-pilot-web cityu-mail-pilot-worker
```

---

## 3. 这套备份的边界（必须知道）

- **只备份数据库**，不含：`pilot.env`（主密钥）、邮件正文（发完即清空，本来就不留）、
  背景图与图标（在代码包里，可重新部署）
- **保留按时间，不按份数**：`pilot_app/backup.py` 的 `prune()` 删的是
  `(now - made).days > BACKUP_KEEP_DAYS`（默认 **7 天**，2026-09-18 从 14 改）且有下限
  `BACKUP_KEEP_MIN=7` 份、上限 `BACKUP_KEEP_MAX=60` 份。每天一份 → 稳定在
  **8 份、最多覆盖约 7 天**。要更长的历史就调 `INFE_PILOT_BACKUP_KEEP_DAYS`，
  不要改代码——这个值也是隐私政策里写明的那个窗口。
  （这段原本写的是「保留 7 份，按文件名倒序删除」和 `copies[7:]`，
  那是 v0.40.0 之前按**数量**保留时的实现，早已不存在。）
- 备份文件权限 `0600`、属主 `cityumail`，与数据库同级敏感——**它能解密出所有用户的邮箱授权码**
- 备份与数据库在**同一台机器**上。机器没了就都没了——异地副本目前**没有做**
