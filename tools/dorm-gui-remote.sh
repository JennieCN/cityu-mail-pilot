#!/usr/bin/env bash
#
# `dorm gui` 在**宿舍机上**跑的那一段：看一眼它自己的 DSH 网页在不在 / 没在跑就起起来 / 报出网址。
#
# 为什么单独一个文件，而不是写成 dorm.sh 里的一段 heredoc：
#   macOS 自带的是 **bash 3.2**，它解析不了「在 `$( )` 里的 heredoc」（尤其正文里还有引号、
#   续行、中文时）—— 2026-09-21 真踩：远端报 `line 18: syntax error near unexpected token`，
#   本机还把那段正文当代码接着跑，报出一个看起来毫不相干的 `i: unbound variable`。
#   抽成一个文件既绕开那个解析器 bug，也让这几十行能被 `test_shell_scripts` 扫到
#   （`$变量` 紧跟中文标点是另一个坑，见那份测试）。
#
# 参数：$1 = ensure（默认：没在跑就起来）| restart（先重启再报）
# 退出码：0 = 起来了且端口真的在听；1 = 没起来（并把那台自己的日志尾部打出来）
set -u

action="${1:-ensure}"
unit="$HOME/.config/systemd/user/dsh-web.service"

# unit 不在就现写一个 —— 自愈：重建过 WSL、搬过家、误删过都不必再来问人。
if [ ! -f "$unit" ]; then
  mkdir -p "$(dirname "${unit}")"
  printf '%s\n' \
    '[Unit]' \
    'Description=DSH web GUI (CityU Mail Pilot; port 3091)' \
    'After=network.target' \
    '' \
    '[Service]' \
    'Type=simple' \
    'WorkingDirectory=%h/ban' \
    'ExecStart=/usr/bin/dsh web --no-open --port 3091' \
    'Restart=always' \
    'RestartSec=5' \
    '' \
    '[Install]' \
    'WantedBy=default.target' \
    > "$unit"
  systemctl --user daemon-reload
  systemctl --user enable dsh-web.service >/dev/null 2>&1
  echo "（那台上原先没有 dsh-web.service，我刚写了一个并设成开机自启）"
fi

case "${action}" in
  restart) systemctl --user restart dsh-web.service ;;
  *)       systemctl --user is-active --quiet dsh-web.service || systemctl --user start dsh-web.service ;;
esac

# **「服务 active」不等于「网页能开」**，所以再等端口真的在听（最多 30 秒）。
i=0
while [ "$i" -lt 30 ]; do
  ss -ltn 2>/dev/null | grep -q '127\.0\.0\.1:3091' && break
  i=$((i + 1)); sleep 1
done

if systemctl --user is-active --quiet dsh-web.service; then
  echo "服务：active（开机自启：$(systemctl --user is-enabled dsh-web.service 2>/dev/null || echo 没设上)）"
else
  echo "服务：**没起来**"
  journalctl --user -u dsh-web.service --no-pager -n 10 2>/dev/null | tail -10
  exit 1
fi
if ss -ltn 2>/dev/null | grep -q '127\.0\.0\.1:3091'; then
  echo "端口：127.0.0.1:3091 在听"
else
  echo "端口：**3091 没在听**（服务活着但没绑上端口，看上面日志）"
  exit 1
fi
url="$(journalctl --user -u dsh-web.service --no-pager -n 200 2>/dev/null \
  | grep -o 'http://127\.0\.0\.1:3091/?token=[A-Za-z0-9_-]*' | tail -1)"
echo "网址：${url:-（日志里没找到，跑 dorm gui restart 再看）}"
