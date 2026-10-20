#!/usr/bin/env bash
# 真卸一次：装 → 验 → 卸 → 验留下了什么 → 再装 → purge → 再装一次。
#
# 为什么要有这个脚本
# ------------------
# 「`--uninstall` 到底删了什么、留下了什么」只能在真机器上回答，而**生产上不能试**
# ——那会把 7 个真实用户的服务停掉。所以它跑在一台可丢弃的 Linux 上：CI 的
# `uninstall-drill` 作业（GitHub runner），或者你自己愿意承担风险的一台 Ubuntu。
#
# 它盯的是几件**读代码看不出来**的事：
#   * 在一台**从没装过**的机器上能不能装上（服务账号是不是真的被创建了）；
#   * `--uninstall` 之后**数据还在不在**（/etc、/var/lib、/var/backups、/opt）；
#   * `--uninstall` 有没有留下什么**还在生效**的东西（nginx 站点）；
#   * 装 → 卸 → 再装，数据库是不是**同一个文件**（不是被重建了）；
#   * 输错 purge 的确认词时，是不是真的什么都没删；
#   * `--purge` 之后**还能不能再装一次**（它删掉了服务账号——那是一道单向门）。
#
# 用法：
#   sudo bash tools/uninstall_drill.sh /path/to/unpacked-release
#   sudo bash tools/uninstall_drill.sh --ci /path/to/unpacked-release   # 在 CI 里：失败时发 check-run 注解
#
# 安全闸：机器上**已经有**这套东西（存在 /opt/cityu-mail-pilot 或
# /etc/cityu-mail-pilot/pilot.env）时它拒绝运行，除非显式给
# --i-know-this-wipes-this-machine。这条闸门的存在理由是这个脚本会 purge——
# 一个能删掉生产数据库的脚本不该长得像「随便跑跑」。
set -uo pipefail

FORCE=0
CI_ANNOTATE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --i-know-this-wipes-this-machine) FORCE=1; shift ;;
    # Explicit, because the drill runs under sudo and sudo resets the
    # environment (env_reset is the default): GITHUB_ACTIONS does not survive
    # `sudo bash …`, so a check on it silently produced no annotation at all --
    # the failure was invisible from outside the repository. The flag is passed
    # as an argument, which sudo cannot strip.
    --ci) CI_ANNOTATE=1; shift ;;
    *) break ;;
  esac
done
RELEASE="${1:-}"
PORT=8787
FAILED=0
FAILURES=()

say()   { printf '\n== %s\n' "$*"; }
note()  { printf '   %s\n' "$*"; }
check() { # check <0|1> <说明> [细节]
  if [[ "$1" == 1 ]]; then
    printf '  ok   %s%s\n' "$2" "${3:+ — $3}"
  else
    printf '  FAIL %s%s\n' "$2" "${3:+ — $3}"
    FAILED=1
    FAILURES+=("$2${3:+（$3）}")
  fi
}
check_code()   { [[ "$1" -eq 0 ]] && check 1 "$2" "${3:-}" || check 0 "$2" "exit=$1 ${3:-}"; }

# When the installer fails, "it failed" is not a diagnosis -- the installer's own
# last lines are. They go into the annotation so the reason travels with the
# failure instead of staying inside a log only a repository admin can download.
note_log_tail() {
  local log="$1" prefix="$2" line
  [[ -f "$log" ]] || { FAILURES+=("${prefix}（没有日志 ${log}）"); return 0; }
  while IFS= read -r line; do
    [[ -n "$line" ]] && FAILURES+=("$prefix$line")
  done < <(tail -n 8 "$log")
}
check_installer() { # check_installer <exit code> <说明> <日志>
  check_code "$1" "$2" "日志 $3"
  [[ "$1" -ne 0 ]] && note_log_tail "$3" "安装器说："
  return 0
}

# The diagnosis has to survive *any* exit path, including the ones that never
# reach the summary at the bottom -- an unbound variable under `set -u`, a
# missing file, a bug in this script. So the annotation is emitted from an EXIT
# trap, and the normal path marks it as already done.
ANNOTATED=0
annotate() {
  [[ "$CI_ANNOTATE" == 1 && "$ANNOTATED" == 0 ]] || return 0
  ANNOTATED=1
  local body
  if [[ "${#FAILURES[@]}" -gt 0 ]]; then
    body="$(printf '%s\n' "${FAILURES[@]}" | tr -d '\r' | sed -e 's/%/%25/g' \
            | awk '{ if (NR > 1) printf "%%0A"; printf "%s", $0 }')"
  else
    body="（没有断言失败，脚本自己提前退出了；看作业日志的 drill-*.log）"
  fi
  # One line only: a real newline ends the workflow command, and then the
  # annotation shows just its first line.
  echo "::error title=卸载演练失败（${#FAILURES[@]} 条断言）::$body"
}
on_exit() {
  local code=$?
  [[ "$code" -ne 0 ]] && annotate
  exit "$code"
}
trap on_exit EXIT
check_exists() { [[ -e "$1" ]] && check 1 "$2（还在）" || check 0 "$2（不见了：$1）"; }
check_gone()   { [[ -e "$1" ]] && check 0 "$2（还在：$1）" || check 1 "$2（已删除）"; }
check_active() { systemctl is-active --quiet "$1" && check 1 "$2" "$1 active" || check 0 "$2" "$1 不是 active"; }

[[ "$(id -u)" == 0 ]] || { echo "请用 sudo 运行（安装器要写 /etc 与 /var/lib）。" >&2; exit 2; }
[[ -n "$RELEASE" && -f "$RELEASE/pilot_app/deploy_pilot.sh" ]] \
  || { echo "用法：sudo bash tools/uninstall_drill.sh /path/to/unpacked-release" >&2; exit 2; }
INSTALLER="$RELEASE/pilot_app/deploy_pilot.sh"

if [[ "$FORCE" != 1 ]]; then
  if [[ -e /opt/cityu-mail-pilot || -e /etc/cityu-mail-pilot/pilot.env ]]; then
    echo "这台机器上已经装了这套东西（/opt/cityu-mail-pilot 或 /etc/cityu-mail-pilot/pilot.env 存在）。" >&2
    echo "这个演练会 purge 掉数据与主密钥，所以拒绝在已安装的机器上运行。" >&2
    echo "真要在一次性的机器上跑：加 --i-know-this-wipes-this-machine。" >&2
    exit 2
  fi
fi

say "起点"
[[ -e /opt/cityu-mail-pilot ]] && note "注意：/opt/cityu-mail-pilot 已存在" || note "/opt 下没有旧安装（起点干净）"
id -u cityumail >/dev/null 2>&1 && note "注意：cityumail 账号已存在" || note "cityumail 账号不存在（起点干净）"

# ---------------------------------------------------------------- 1. 装
say "1/7 安装（不带 --proxy，所以不需要域名与证书）"
bash "$INSTALLER" --admin-email drill@example.com > /tmp/drill-install.log 2>&1
check_installer $? "安装退出码为 0" /tmp/drill-install.log
tail -3 /tmp/drill-install.log | sed 's/^/       /'

for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$PORT/health" && break; sleep 0.5; done

# ---------------------------------------------------------------- 2. 验
say "2/7 装完了对不对"
for unit in cityu-mail-pilot-web cityu-mail-pilot-worker cityu-mail-pilot-backup.timer cityu-mail-pilot-backup-request.path; do
  check_active "$unit" "$unit 在跑"
done
health="$(curl -s --max-time 5 "http://127.0.0.1:$PORT/health" || true)"
[[ "$health" == *'"status":"ok"'* ]] && check 1 "/health 返回 ok" "${health:0:60}" \
  || check 0 "/health 返回 ok" "${health:0:60}"
check_exists /etc/cityu-mail-pilot/pilot.env "配置文件"
check_exists /var/lib/cityu-mail-pilot/pilot.sqlite3 "数据库文件"
id -u cityumail >/dev/null 2>&1 && check 1 "cityumail 账号存在" || check 0 "cityumail 账号存在"
db_before="$(stat -c '%i %s' /var/lib/cityu-mail-pilot/pilot.sqlite3 2>/dev/null || echo '')"

# ---------------------------------------------------------------- 3. 埋一个 nginx 站点
say "3/7 假装用 --proxy 装过（埋一个 nginx 站点，看 --uninstall 收不收拾）"
# The runner has no nginx installed; create the directories so the files can be
# planted. Otherwise the two "nginx site gone" checks below pass by accident --
# they would be asserting that a file we never managed to write is absent.
mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
printf '# drill\n' > /etc/nginx/sites-available/cityu-mail-pilot
ln -sf /etc/nginx/sites-available/cityu-mail-pilot /etc/nginx/sites-enabled/cityu-mail-pilot
note "已埋：/etc/nginx/sites-{available,enabled}/cityu-mail-pilot"

# ---------------------------------------------------------------- 4. 卸
say "4/7 --uninstall（应当保留配置与数据）"
bash "$INSTALLER" --uninstall > /tmp/drill-uninstall.log 2>&1
check_installer $? "--uninstall 退出码为 0" /tmp/drill-uninstall.log
for unit in cityu-mail-pilot-web.service cityu-mail-pilot-worker.service cityu-mail-pilot-alert@.service; do
  check_gone "/etc/systemd/system/$unit" "单元 $unit"
done
systemctl is-active --quiet cityu-mail-pilot-web \
  && check 0 "web 已经停下" || check 1 "web 已经停下"
check_exists /etc/cityu-mail-pilot/pilot.env "配置（含主密钥）"
check_exists /var/lib/cityu-mail-pilot/pilot.sqlite3 "数据库"
check_exists /opt/cityu-mail-pilot "程序目录"
check_gone /etc/nginx/sites-enabled/cityu-mail-pilot "nginx 站点（enabled）"
check_gone /etc/nginx/sites-available/cityu-mail-pilot "nginx 站点（available）"

say "4b/7 再卸一次（幂等：已经卸过了不该报错）"
bash "$INSTALLER" --uninstall > /tmp/drill-uninstall2.log 2>&1
check_installer $? "第二次 --uninstall 退出码为 0" /tmp/drill-uninstall2.log

# ---------------------------------------------------------------- 5. 再装
say "5/7 再装一次（数据必须还在原处，不是被重建）"
bash "$INSTALLER" --admin-email drill@example.com > /tmp/drill-reinstall.log 2>&1
check_installer $? "重装退出码为 0" /tmp/drill-reinstall.log
db_after="$(stat -c '%i %s' /var/lib/cityu-mail-pilot/pilot.sqlite3 2>/dev/null || echo '')"
[[ -n "$db_before" && "$db_before" == "$db_after" ]] \
  && check 1 "还是同一个数据库文件（inode 与大小都没变）" "$db_before → $db_after" \
  || check 0 "还是同一个数据库文件" "$db_before → $db_after"

# ---------------------------------------------------------------- 6. purge
say "6/7 --purge（连配置与数据一起删；要手输 purge 确认）"
printf 'purge\n' | bash "$INSTALLER" --uninstall --purge > /tmp/drill-purge.log 2>&1
check_installer $? "--purge 退出码为 0" /tmp/drill-purge.log
grep -q "刻意没删" /tmp/drill-purge.log && check 1 "明说了刻意没删什么（证书）" \
  || check 0 "明说了刻意没删什么（证书）"
for path in /etc/cityu-mail-pilot /var/lib/cityu-mail-pilot /var/backups/cityu-mail-pilot /opt/cityu-mail-pilot; do
  check_gone "$path" "purge 删掉 $path"
done
id -u cityumail >/dev/null 2>&1 && check 0 "cityumail 账号已删除" || check 1 "cityumail 账号已删除"
getent group cityumail >/dev/null 2>&1 && check 0 "cityumail 用户组已删除" || check 1 "cityumail 用户组已删除"

say "6b/7 输错确认词时必须什么都没删"
bash "$INSTALLER" --admin-email drill@example.com > /tmp/drill-reinstall2.log 2>&1
printf 'no\n' | bash "$INSTALLER" --uninstall --purge > /tmp/drill-abort.log 2>&1
abort_code=$?
[[ "$abort_code" -ne 0 ]] && check 1 "输错确认词时非零退出" "exit=$abort_code" \
  || check 0 "输错确认词时非零退出" "exit=$abort_code"
check_exists /var/lib/cityu-mail-pilot/pilot.sqlite3 "数据没被删"

# ---------------------------------------------------------------- 7. purge 后还能不能再装
say "7/7 purge 之后再装一次（这是那道单向门：purge 删掉了服务账号）"
printf 'purge\n' | bash "$INSTALLER" --uninstall --purge > /dev/null 2>&1
bash "$INSTALLER" --admin-email drill@example.com > /tmp/drill-after-purge.log 2>&1
check_installer $? "purge 之后还能装（服务账号会被重新创建）" /tmp/drill-after-purge.log
id -u cityumail >/dev/null 2>&1 && check 1 "服务账号被重新创建" || check 0 "服务账号被重新创建"

say "收尾：卸掉，把机器还回去"
printf 'purge\n' | bash "$INSTALLER" --uninstall --purge > /dev/null 2>&1
rm -f /etc/nginx/sites-enabled/cityu-mail-pilot /etc/nginx/sites-available/cityu-mail-pilot

echo
if [[ "$FAILED" == 1 ]]; then
  echo "════ 有断言没通过 ════"
  # Readable without a GitHub login: step *logs* need admin rights on the API,
  # check-run annotations do not. (Also emitted by the EXIT trap if something
  # goes wrong earlier; annotate() is idempotent.)
  annotate
else
  echo "════ 全部通过：装、卸、再装、purge、再装，都符合预期 ════"
fi
exit "$FAILED"
