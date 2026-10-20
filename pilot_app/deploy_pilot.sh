#!/usr/bin/env bash
#
# CityU Mail Pilot installer / upgrader / uninstaller.
#
#   sudo bash pilot_app/deploy_pilot.sh                 # 不带参数 = 只装文件与单元（与旧行为一致）
#   sudo bash pilot_app/deploy_pilot.sh --dry-run       # 先看它会做什么，什么都不改
#   sudo bash pilot_app/deploy_pilot.sh --origin https://mail.example.com --admin-email you@example.com
#   sudo bash pilot_app/deploy_pilot.sh --upgrade       # 换代码 + 重启；绝不碰配置与数据
#   sudo bash pilot_app/deploy_pilot.sh --uninstall     # 停服务并移除单元，保留配置与数据
#
# Why the flags look like this: n8n's one-line installer established the contract
# that people now expect — running it twice is safe, `--upgrade` changes the
# version and nothing else, and there is a documented way back out. Copying that
# semantics is cheaper and less surprising than inventing our own.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Never let the directory we were invoked from leak into anything. Python puts
# the current directory first on sys.path, so running the packaged python from
# inside an unpacked release would import *that* copy of pilot_app instead of the
# installed one — the backup step of an upgrade did exactly this, and wrote
# root-owned __pycache__ into the release tree while it was at it. Every path
# below is absolute, so stepping out of the way costs nothing.
cd /
APP_DIR=/opt/cityu-mail-pilot
CONFIG_DIR=/etc/cityu-mail-pilot
DATA_DIR=/var/lib/cityu-mail-pilot
BACKUP_DIR=/var/backups/cityu-mail-pilot
SERVICE_NAME=cityu-mail-pilot
PORT=8787
UNITS=(
  cityu-mail-pilot-web.service
  cityu-mail-pilot-worker.service
  cityu-mail-pilot-backup.service
  cityu-mail-pilot-backup.timer
  cityu-mail-pilot-backup-request.path
  cityu-mail-pilot-alert@.service
)
# The OnFailure= drop-ins go on the *services*, not the timers: a timer only
# activates its same-named service, and a failed state does not bubble up from
# the service to the timer, so a timer drop-in would silently never fire.
DROPIN_UNITS=(certbot.service cityu-mail-pilot-backup.service)

ORIGIN=""
ADMIN_EMAIL=""
DO_PROXY=0
DRY_RUN=0
UPGRADE=0
UNINSTALL=0
PURGE=0

usage() {
  cat <<'EOF'
用法：sudo bash pilot_app/deploy_pilot.sh [选项]

不带任何选项时：安装/更新程序文件与 systemd 单元，然后打印后续步骤。

  --origin URL         对外访问地址，例如 https://mail.example.com
                       不填则尝试用本机公网 IP 生成 https://<ip-用短横线>.sslip.io
  --admin-email EMAIL  管理员邮箱（只有首次安装、且 pilot.env 还不存在时写入）
  --proxy              配置 Nginx 站点并申请 Let's Encrypt 证书（需要 --origin）
  --dry-run            只检查并打印将要做的改动，不写入任何文件
  --upgrade            只替换代码并重启；绝不修改 pilot.env 与 /var/lib，升级前自动备份
  --uninstall          停止服务并移除 systemd 单元；默认保留 /etc 与 /var/lib
  --purge              与 --uninstall 同用：连配置与数据一起删除（需要手动确认）
  -h, --help           显示这段说明
EOF
}

step() { printf '\n== %s\n' "$*"; }
log()  { printf '   %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*" >&2; }
die()  { printf '\n错误：%s\n' "$*" >&2; exit 1; }
# Everything that changes the machine goes through this, so --dry-run is honest
# rather than approximate.
run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '   [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

host_of() { printf '%s' "${1#*://}" | cut -d/ -f1 | cut -d: -f1; }

public_ip() {
  curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null \
    || curl -fsS --max-time 6 https://ifconfig.me 2>/dev/null
}

# Our own production install uses exactly this trick: sslip.io resolves
# <ip-with-dashes>.sslip.io back to the address, so a stranger needs no domain
# and no DNS work before Let's Encrypt will issue a certificate.
derive_origin() {
  local ip
  ip="$(public_ip)" || return 1
  [[ "$ip" =~ ^[0-9.]+$ ]] || return 1
  printf 'https://%s.sslip.io' "${ip//./-}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --origin)      ORIGIN="${2:-}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --proxy)       DO_PROXY=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --upgrade)     UPGRADE=1; shift ;;
    --uninstall)   UNINSTALL=1; shift ;;
    --purge)       PURGE=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             usage >&2; die "未知选项：$1" ;;
  esac
done

# Argument problems are reported before the privilege check, so a typo comes back
# as "unknown option" rather than "run me with sudo" — and so these can be tested
# without root.
[[ "$PURGE" == 1 && "$UNINSTALL" != 1 ]] && die "--purge 只能和 --uninstall 一起用。"
[[ "$UPGRADE" == 1 && "$UNINSTALL" == 1 ]] && die "--upgrade 和 --uninstall 不能同时用。"
if [[ "$DO_PROXY" == 1 && -z "$ORIGIN" ]]; then
  ORIGIN="$(derive_origin || true)"
  [[ -z "$ORIGIN" ]] && die "--proxy 需要 --origin（自动推导也失败了，请手动指定域名）。"
fi

if [[ "${EUID}" -ne 0 ]]; then
  die "请使用 sudo 运行此脚本。"
fi

# ---------------------------------------------------------------- preflight
# A stranger's install must fail with an explanation, not halfway through.
# Immich's install page does this well: it predicts the exact error messages
# people hit and prints a fix next to each one.
preflight() {
  local problems=0
  ok()   { printf '   ✓ %s\n' "$*"; }
  bad()  { printf '   ✗ %s\n' "$*" >&2; problems=$((problems + 1)); }

  step "环境自检"

  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
      ubuntu|debian) ok "系统：${PRETTY_NAME:-$ID}" ;;
      *) bad "这套脚本按 Ubuntu/Debian 编写，当前是 ${PRETTY_NAME:-未知系统}。请改用对应的命令或手动安装。" ;;
    esac
  else
    bad "读不到 /etc/os-release，无法确认系统类型。"
  fi

  if command -v python3 >/dev/null 2>&1; then
    local py
    py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      ok "Python：$py"
    else
      bad "Python 需要 3.9 或更高，当前是 ${py}。"
    fi
  else
    bad "找不到 python3。安装：sudo apt install python3 python3-venv"
  fi

  command -v python3 >/dev/null 2>&1 || true
  if python3 -c 'import venv' >/dev/null 2>&1; then
    ok "python3-venv 可用"
  else
    bad "缺少 venv 模块。安装：sudo apt install python3-venv"
  fi

  for c in openssl systemctl useradd curl; do
    if command -v "$c" >/dev/null 2>&1; then ok "命令：$c"; else bad "缺少命令 ${c}。"; fi
  done

  if [[ "$DO_PROXY" == 1 ]]; then
    for c in nginx certbot; do
      if command -v "$c" >/dev/null 2>&1; then
        ok "命令：$c"
      else
        bad "缺少 ${c}。安装：sudo apt install nginx certbot python3-certbot-nginx"
      fi
    done
    local host
    host="$(host_of "$ORIGIN")"
    local ip
    ip="$(public_ip || true)"
    if [[ -n "$ip" ]]; then
      local resolved
      resolved="$(getent hosts "$host" | awk '{print $1}' | head -1 || true)"
      if [[ "$resolved" == "$ip" ]]; then
        ok "域名解析：$host → $ip"
      elif [[ -z "$resolved" ]]; then
        bad "$host 解析不到任何地址。先把这个域名指向本机公网 IP ${ip}，否则 Let's Encrypt 无法验证。"
      else
        bad "$host 解析到 ${resolved}，但本机公网 IP 是 ${ip}。证书申请会失败。"
      fi
    else
      warn "取不到本机公网 IP，跳过 DNS 校验。"
    fi
  fi

  local free_kb
  free_kb="$(df -Pk /var 2>/dev/null | awk 'NR==2{print $4}' || echo 0)"
  if [[ "${free_kb:-0}" -gt 1048576 ]]; then
    ok "磁盘：/var 剩余 $((free_kb / 1024)) MB"
  else
    bad "/var 剩余空间不足 1 GB（实际 $((free_kb / 1024)) MB）。"
  fi

  if [[ "$problems" -gt 0 ]]; then
    printf '\n有 %d 项没通过。修好上面的问题再运行一次；脚本没有改动任何东西。\n' "$problems" >&2
    exit 1
  fi
}

# ------------------------------------------------------------------ actions
do_uninstall() {
  step "停止并移除服务"
  for unit in "${UNITS[@]}"; do
    run systemctl disable --now "$unit" >/dev/null 2>&1 || true
  done
  run rm -f "${UNITS[@]/#//etc/systemd/system/}"
  for unit in "${DROPIN_UNITS[@]}"; do
    run rm -rf "/etc/systemd/system/${unit}.d"
  done
  run systemctl daemon-reload
  log "systemd 单元已移除。"

  # `--proxy` writes a vhost here. Removing the units but leaving the site
  # behind means nginx keeps serving a host whose backend no longer exists --
  # and `nginx -t` still passes, so nothing complains. Found by the drill
  # (tools/uninstall_drill.sh) on a disposable runner, 2026-09-16.
  if [[ -e /etc/nginx/sites-enabled/cityu-mail-pilot || -e /etc/nginx/sites-available/cityu-mail-pilot ]]; then
    run rm -f /etc/nginx/sites-enabled/cityu-mail-pilot \
              /etc/nginx/sites-available/cityu-mail-pilot
    if systemctl is-active --quiet nginx 2>/dev/null; then
      run systemctl reload nginx
      log "nginx 站点已移除并重新加载（否则它会继续代理到一个已经没有后端的地址）。"
    else
      log "nginx 站点已移除（nginx 没在运行，未重载）。"
    fi
  else
    log "没有 nginx 站点需要移除（安装时没用 --proxy）。"
  fi

  if [[ "$PURGE" == 1 ]]; then
    step "清除配置与数据"
    log "将要删除：${CONFIG_DIR}（含主密钥）· ${DATA_DIR}（含数据库）· $BACKUP_DIR · $APP_DIR"
    if [[ "$DRY_RUN" == 1 ]]; then
      log "[dry-run] 跳过确认与删除"
    else
      printf '   如果确定，请输入 purge 并回车：'
      local answer=""
      read -r answer || true
      [[ "$answer" == "purge" ]] || die "已取消，什么都没有删除。"
      rm -rf "$CONFIG_DIR" "$DATA_DIR" "$BACKUP_DIR" "$APP_DIR"
      userdel cityumail >/dev/null 2>&1 || true
      groupdel cityumail >/dev/null 2>&1 || true
      log "已清除（用户与用户组 cityumail 一并删除）。"
      # Say what was deliberately left, and why. A silent leftover is how a
      # machine ends up with something nobody can account for.
      log "刻意没删：Let's Encrypt 的证书与续期配置（/etc/letsencrypt 里以你的域名为名的那些）——"
      log "  删证书不可逆，而且 Let's Encrypt 有申请频率限制。确定不再需要就自己删。"
    fi
  else
    log "配置（${CONFIG_DIR}）与数据（${DATA_DIR}）已保留。"
    log "要连它们一起删掉：sudo bash $0 --uninstall --purge"
  fi
}

do_upgrade() {
  [[ -f "$CONFIG_DIR/pilot.env" ]] || die "找不到 $CONFIG_DIR/pilot.env，说明还没安装过。请先做一次不带 --upgrade 的安装。"
  step "升级前备份"
  if [[ "$DRY_RUN" == 1 ]]; then
    log "[dry-run] 会先运行 python -m pilot_app.backup"
  else
    # A backup that fails must stop the upgrade: this is the one moment where a
    # half-applied change is worse than no change.
    # Run it the way the systemd unit does — from $APP_DIR — so it is the
    # installed code and the installed venv that produce the backup.
    ( cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" -m pilot_app.backup ) \
      || die "备份失败，升级已中止（代码与数据都未改动）。"
    # The installer runs as root (it has to, to read EnvironmentFile), but a
    # backup owned by root and mode 0600 is unreadable by the account that owns
    # every other backup in the same directory — so hand it back. This is how
    # the directory ended up with two different owners.
    find "$BACKUP_DIR" -maxdepth 1 -user root -name 'pilot-*.sqlite3' \
      -exec chown cityumail:cityumail {} + 2>/dev/null || true
    log "备份完成。"
  fi
}

# The service account, created if it is missing.
#
# Why this exists: the pilot installer was forked from the older single-user
# deploy script (cloud_deploy/deploy.sh), which *did* create this user -- and
# this one never did, while its preflight still checks that `useradd` is
# available. On this machine nobody noticed for months, because it inherited a
# box where the old script had already made the account. On a machine that has
# never run that script -- anybody following the README from a fresh clone --
# `install -d -o cityumail` fails with "install: invalid user" and the ERR trap
# aborts the install. Verified on Ubuntu 26.04 (GNU coreutils), 2026-09-16.
#
# It is also half of a one-way door: `--purge` deletes this user, so without
# this the machine can never be installed on again.
ensure_service_user() {
  step "服务账号"
  if getent group cityumail >/dev/null 2>&1; then
    log "用户组 cityumail 已存在。"
  else
    run groupadd --system cityumail
    log "已创建用户组 cityumail。"
  fi
  if id -u cityumail >/dev/null 2>&1; then
    log "用户 cityumail 已存在。"
  else
    # --gid is spelled out on purpose: whether `useradd --system` invents a
    # same-named group depends on USERGROUPS_ENAB in /etc/login.defs, and the
    # units say Group=cityumail. Depending on a distro default here would work
    # until it didn't.
    run useradd --system --gid cityumail --home-dir "$DATA_DIR" \
                --shell /usr/sbin/nologin cityumail
    log "已创建用户 cityumail。"
  fi
}

do_install_files() {
  step "安装程序文件"
  run install -d -m 0755 "$APP_DIR"
  run install -d -o cityumail -g cityumail -m 0700 "$DATA_DIR" "$BACKUP_DIR"
  run install -d -m 0700 "$CONFIG_DIR"

  run rm -rf "$APP_DIR/pilot_app"
  run cp -a "$SOURCE_DIR/pilot_app" "$APP_DIR/pilot_app"
  if [[ "$DRY_RUN" != 1 ]]; then
    find "$APP_DIR/pilot_app" -type d -name __pycache__ -prune -exec rm -rf {} +
    rm -rf "$APP_DIR/pilot_app/tests" "$APP_DIR/pilot_app/systemd"
    # Source archives can preserve an author's restrictive umask (for example a
    # module stored 0600). Runtime code is root-owned and immutable to the
    # service account, but every module and asset must stay readable by it.
    find "$APP_DIR/pilot_app" -type d -exec chmod 0755 {} +
    find "$APP_DIR/pilot_app" -type f -exec chmod 0644 {} +
    chmod 0755 "$APP_DIR/pilot_app/deploy_pilot.sh"
  fi
  # Optional extras must never be able to abort an install: by this point the
  # code directory has already been replaced, so dying here leaves the machine
  # half-upgraded (new code on disk, old process in memory).
  #
  # LICENSE travels with the code because AGPL requires it to; the README goes
  # with it because whoever administers this box is not necessarily whoever
  # installed it, and "what is this and how do I run it" should be on the box.
  for extra in LICENSE README.md; do
    if [[ -f "$SOURCE_DIR/$extra" ]]; then
      run install -m 0644 "$SOURCE_DIR/$extra" "$APP_DIR/$extra"
    else
      warn "发布包里没有 ${extra}，跳过（不影响运行）。"
    fi
  done
  log "代码已更新到 ${APP_DIR}。"

  run python3 -m venv "$APP_DIR/.venv"
  run "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check \
    -r "$APP_DIR/pilot_app/requirements.lock"
}

do_install_config() {
  # Idempotence lives here, and it is the single most important line in the
  # file: re-running the installer must never regenerate the master key or drop
  # a configured domain, because either would lock every existing account out.
  if [[ -f "$CONFIG_DIR/pilot.env" ]]; then
    log "已存在 $CONFIG_DIR/pilot.env，保持原样（不会重写密钥或域名）。"
    return
  fi
  step "生成配置"
  if [[ -z "$ORIGIN" ]]; then
    ORIGIN="$(derive_origin || true)"
    [[ -n "$ORIGIN" ]] && log "未指定 --origin，根据公网 IP 推导为 $ORIGIN"
  fi
  local origin_value="${ORIGIN:-https://mail.example.com}"
  local admin_value="${ADMIN_EMAIL:-}"

  if [[ "$DRY_RUN" == 1 ]]; then
    log "[dry-run] 会写入 $CONFIG_DIR/pilot.env（主密钥随机生成，不打印）"
    log "[dry-run] INFE_PILOT_ORIGIN=$origin_value"
    return
  fi

  local master_key
  master_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
  (
    umask 077
    {
      echo "INFE_PILOT_DB=$DATA_DIR/pilot.sqlite3"
      echo "INFE_PILOT_MASTER_KEY=$master_key"
      echo "INFE_PILOT_ORIGIN=$origin_value"
      echo "INFE_PILOT_COOKIE_SECURE=1"
      echo "INFE_PILOT_POLL_SECONDS=60"
      echo "INFE_PILOT_POLL_WORKERS=4"
      echo "INFE_PILOT_REPORT_WORKERS=6"
      echo "INFE_PILOT_USER_BATCH=3"
      echo "INFE_PILOT_MAX_USERS=5"
      echo "INFE_PILOT_MODEL_TIMEOUT=300"
      echo "INFE_PILOT_SEARCH_TIMEOUT=45"
      echo "INFE_PILOT_INITIAL_LOOKBACK_HOURS=48"
      echo "INFE_PILOT_BACKUP_DIR=$BACKUP_DIR"
      echo "LOG_LEVEL=INFO"
      if [[ -n "$admin_value" ]]; then
        echo "INFE_PILOT_ADMIN_EMAILS=$admin_value"
      fi
    } > "$CONFIG_DIR/pilot.env"
  )
  unset master_key
  chmod 0600 "$CONFIG_DIR/pilot.env"
  log "配置已写入 $CONFIG_DIR/pilot.env（主密钥已随机生成，权限 0600）。"
  # `[[ ... ]] && warn ...` as the last line made this function return 1 whenever
  # the test was false -- i.e. exactly when the caller *did* pass --admin-email,
  # which is the documented happy path. Under `set -e` the ERR trap then aborted
  # the install right after the config was written, before any unit was
  # installed: "错误：第 490 行失败，安装已中止。"
  #
  # Nobody had hit it because every machine that ran this installer either
  # already had pilot.env (the function returns early) or was set up by the older
  # single-user script. The first run of tools/uninstall_drill.sh on a clean
  # machine found it immediately.
  if [[ -z "$admin_value" ]]; then
    warn "没有 --admin-email，管理后台暂时无人可进。之后补进 INFE_PILOT_ADMIN_EMAILS 再重启 web 即可。"
  fi
}

do_install_units() {
  step "安装 systemd 单元"
  for unit in "${UNITS[@]}"; do
    run install -m 0644 "$SOURCE_DIR/pilot_app/systemd/$unit" /etc/systemd/system/
  done
  for unit in "${DROPIN_UNITS[@]}"; do
    run install -d -m 0755 "/etc/systemd/system/${unit}.d"
    run install -m 0644 "$SOURCE_DIR/pilot_app/systemd/dropins/${unit}.d/onfailure.conf" \
      "/etc/systemd/system/${unit}.d/onfailure.conf"
  done
  run chown -R root:root "$APP_DIR"
  run systemctl daemon-reload
  run systemctl enable \
    cityu-mail-pilot-web.service cityu-mail-pilot-worker.service cityu-mail-pilot-backup.timer \
    cityu-mail-pilot-backup-request.path
  # On a fresh install the units are not running yet, so starting is enough. On
  # an upgrade they ARE running and hold the old code in memory, so only a
  # restart makes the new version take effect — `enable --now` would silently
  # leave the previous process running and the operator would conclude the
  # upgrade did nothing.
  if [[ "$UPGRADE" == 1 ]]; then
    run systemctl restart cityu-mail-pilot-web.service cityu-mail-pilot-worker.service
  else
    run systemctl start cityu-mail-pilot-web.service cityu-mail-pilot-worker.service
  fi
  run systemctl start cityu-mail-pilot-backup.timer cityu-mail-pilot-backup-request.path
  log "单元已启用并启动。"
}

do_proxy() {
  step "配置 Nginx 与 HTTPS"
  local host
  host="$(host_of "$ORIGIN")"
  local site="/etc/nginx/sites-available/cityu-mail-pilot"
  local enabled="/etc/nginx/sites-enabled/cityu-mail-pilot"

  if [[ "$DRY_RUN" == 1 ]]; then
    log "[dry-run] 会用 $host 渲染 $site 并启用，然后 nginx -t && systemctl reload nginx"
    log "[dry-run] 会运行 certbot --nginx -d $host --non-interactive --agree-tos -m ${ADMIN_EMAIL:-<admin>} --redirect"
    return
  fi

  sed -e "s/mail\.example\.com/$host/g" -e "s|127\.0\.0\.1:8787|127.0.0.1:$PORT|g" \
    "$SOURCE_DIR/pilot_app/nginx-cityu-mail-pilot.conf.example" > "$site"
  ln -sfn "$site" "$enabled"
  nginx -t || die "nginx 配置检查失败，站点已写入 $site 但未生效。"
  systemctl reload nginx
  log "Nginx 站点已启用：$host → 127.0.0.1:$PORT"

  if [[ -z "$ADMIN_EMAIL" ]]; then
    warn "没有 --admin-email，跳过证书申请。补上后重跑：sudo bash $0 --origin $ORIGIN --admin-email you@example.com --proxy"
    return
  fi
  certbot --nginx -d "$host" --non-interactive --agree-tos -m "$ADMIN_EMAIL" --redirect \
    || die "证书申请失败。常见原因：域名没解析到本机、80/443 被占用、或 Let's Encrypt 限流。修好后重跑本命令。"
  log "HTTPS 已启用（certbot.timer 会自动续期，失败会发邮件告警）。"
}

# --------------------------------------------------------------------- main
trap 'die "第 $LINENO 行失败，安装已中止。"' ERR

if [[ "$UNINSTALL" == 1 ]]; then
  do_uninstall
  step "完成"
  exit 0
fi

[[ "$DRY_RUN" == 1 ]] && step "dry-run：只会打印将要做的改动"
preflight
[[ "$UPGRADE" == 1 ]] && do_upgrade

ensure_service_user
do_install_files
do_install_config
do_install_units
[[ "$DO_PROXY" == 1 ]] && do_proxy

step "完成"
if [[ "$DRY_RUN" == 1 ]]; then
  log "上面是全部改动，实际上什么都没做。去掉 --dry-run 再运行一次即可执行。"
elif [[ "$UPGRADE" == 1 ]]; then
  log "已升级并重启。配置与数据未被改动；刚才的备份在 ${BACKUP_DIR}。"
else
  log "服务监听 127.0.0.1:${PORT}。"
  if [[ "$DO_PROXY" == 1 ]]; then
    log "对外地址：$ORIGIN"
  else
    log "还没有配置 Nginx/HTTPS。最简单的做法："
    log "  sudo bash $0 --origin ${ORIGIN:-https://你的域名} --admin-email 你的邮箱 --proxy"
  fi
  log "创建邀请码：sudo -u cityumail $APP_DIR/.venv/bin/python -m pilot_app.manage create-invite"
fi
