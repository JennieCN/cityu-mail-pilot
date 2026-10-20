#!/usr/bin/env bash
# 设置（或移除）异地备份目标。
#
# 与 set_platform_key.sh 同源，理由也一样：
#
#   * **密码不能出现在 argv、shell 历史或任何对话记录里**。`read -s` 让这三处都拿不到它。
#   * `pilot.env` 里同时装着主密钥，手写 `sed -i` 改坏了不是"重填一次"的代价；
#     密码里出现 `/`、`&`、`\` 都会毁掉 sed 的表达式。这里用 grep -v + printf 整体重写。
#   * **改完必须当场验证**。`systemctl start cityu-mail-pilot-backup.service` 用的正是
#     每天那份 unit：同样的用户、同样的 EnvironmentFile、同样的 ProtectSystem 与出网限制。
#     手跑 `python -m pilot_app.backup` 验证不了"那个 unit 在沙箱里能不能连出去"。
#
# 用法（在服务器上，root）：
#     sudo bash /opt/cityu-mail-pilot/pilot_app/set_backup_target.sh
#     sudo bash … --url https://app.koofr.net/dav/Koofr --user me@example.com
#     sudo bash … --remove          # 回到「只在本机备份」
#     sudo bash … --no-verify       # 只写配置，不跑那一次备份
set -euo pipefail

ENV_FILE="${INFE_PILOT_ENV_FILE:-/etc/cityu-mail-pilot/pilot.env}"
SERVICE="cityu-mail-pilot-backup.service"
BACKUP_DIR="${INFE_PILOT_PREDEPLOY_DIR:-/root/pilot-predeploy}"

URL=""
USER_NAME=""
PASSWORD=""
REMOVE=0
FROM_STDIN=0
NO_VERIFY=0

URL_VAR="INFE_PILOT_BACKUP_WEBDAV_URL"
USER_VAR="INFE_PILOT_BACKUP_WEBDAV_USER"
PASSWORD_VAR="INFE_PILOT_BACKUP_WEBDAV_PASSWORD"

while [ $# -gt 0 ]; do
  case "$1" in
    --url)      URL="${2:-}"; shift 2 ;;
    --user)     USER_NAME="${2:-}"; shift 2 ;;
    --env-file) ENV_FILE="${2:-}"; shift 2 ;;
    --remove)   REMOVE=1; shift ;;
    --stdin)    FROM_STDIN=1; shift ;;
    --no-verify) NO_VERIFY=1; shift ;;
    -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "找不到 ${ENV_FILE}。自托管安装时它由 deploy_pilot.sh 生成。" >&2
  exit 1
fi
if [ ! -w "$ENV_FILE" ]; then
  echo "没有写权限：${ENV_FILE} —— 请用 sudo 跑这个脚本。" >&2
  exit 1
fi

if [ "$REMOVE" = "0" ]; then
  if [ -z "$URL" ]; then
    printf 'WebDAV 地址（例如 https://app.koofr.net/dav/Koofr）：' >&2
    IFS= read -r URL
  fi
  case "$URL" in
    http://*|https://*) ;;
    *) echo "地址必须以 http:// 或 https:// 开头。" >&2; exit 1 ;;
  esac
  if [ -z "$USER_NAME" ]; then
    printf 'WebDAV 用户名（通常是登录邮箱）：' >&2
    IFS= read -r USER_NAME
  fi
  if [ -z "$USER_NAME" ]; then
    echo "用户名不能为空。" >&2
    exit 1
  fi
  if [ "$FROM_STDIN" = "1" ]; then
    IFS= read -r PASSWORD || true
  else
    printf '应用密码（不回显、不进历史；不是登录密码）：' >&2
    IFS= read -rs PASSWORD
    printf '\n' >&2
  fi
  PASSWORD="$(printf '%s' "$PASSWORD" | tr -d '\r\n')"
  if [ -z "$PASSWORD" ]; then
    echo "没有读到密码，什么都没改。" >&2
    exit 1
  fi
  case "$PASSWORD" in
    *[[:space:]]*) echo "密码里含空白字符，多半是多粘了东西，什么都没改。" >&2; exit 1 ;;
  esac
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$ENV_FILE" "$BACKUP_DIR/pilot-$STAMP.env"
echo "已备份原文件：$BACKUP_DIR/pilot-$STAMP.env"

# 一次性重写。模式只由**非空**变量名拼成——空分支会让 grep 报
# "empty (sub)expression"，而如果那之后还跟着 `|| true`，整个 pilot.env
# （连主密钥）就会被替换成新写的那一行。这个坑在 set_platform_key.sh 里踩过一次。
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
PATTERN="^(${URL_VAR}|${USER_VAR}"
[ -n "$PASSWORD_VAR" ] && PATTERN="${PATTERN}|${PASSWORD_VAR}"
PATTERN="${PATTERN})="
set +e
grep -vE "$PATTERN" "$ENV_FILE" > "$TMP"
grep_status=$?
set -e
if [ "$grep_status" -gt 1 ]; then
  echo "读取 ${ENV_FILE} 失败（grep 退出码 ${grep_status}），什么都没改。" >&2
  exit 1
fi
if [ "$REMOVE" = "0" ]; then
  {
    printf '%s=%s\n' "$URL_VAR" "$URL"
    printf '%s=%s\n' "$USER_VAR" "$USER_NAME"
    printf '%s=%s\n' "$PASSWORD_VAR" "$PASSWORD"
  } >> "$TMP"
fi
chmod 600 "$TMP"
OWNER="$(stat -c '%u:%g' "$ENV_FILE" 2>/dev/null || stat -f '%u:%g' "$ENV_FILE" 2>/dev/null || true)"
if [ -n "$OWNER" ]; then
  chown "$OWNER" "$TMP" 2>/dev/null || true
fi
mv "$TMP" "$ENV_FILE"
trap - EXIT

if [ "$REMOVE" = "1" ]; then
  echo "已移除异地备份配置 —— 备份回到「只在本机」的状态（本地备份与保留策略不变）。"
  exit 0
fi

echo "已写入 ${ENV_FILE}（密码长度 ${#PASSWORD}，内容不显示）。"
echo "  地址：${URL}"
echo "  用户：${USER_NAME}"
unset PASSWORD

if [ "$NO_VERIFY" = "1" ]; then
  echo "按 --no-verify 要求，没有立刻跑一次备份。"
  exit 0
fi

echo
echo "== 用每天那份 unit 真跑一次（同样的用户、环境与沙箱）=="
# Type=oneshot：`systemctl start` 会等到它结束，所以退出码可信；日志仍然打出来，
# 因为 WebDAV 的报错原文只在那里。
if systemctl start "$SERVICE"; then
  start_code=0
else
  start_code=$?
fi
journalctl -u "$SERVICE" -n 8 --no-pager || true
echo
STATE="${INFE_PILOT_BACKUP_DIR:-/var/backups/cityu-mail-pilot}/offsite-state.json"
if [ -r "$STATE" ]; then
  echo "异地状态文件：${STATE}"
  cat "$STATE"
  echo
fi
if [ "$start_code" -ne 0 ] || systemctl is-failed --quiet "$SERVICE"; then
  cat >&2 <<EOF

这次推送**失败**。配置已经写进 ${ENV_FILE}，你可以改完密码重跑本脚本；
要退回原样：
  sudo cp $BACKUP_DIR/pilot-$STAMP.env $ENV_FILE
本地备份不受影响——异地失败从不会让本地那份丢失。
EOF
  exit 1
fi
echo "结论：异地推送成功，本地备份同时正常。"
